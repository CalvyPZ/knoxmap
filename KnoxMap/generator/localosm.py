"""Map features from local Geofabrik dailies, one box at a time.

The regional PBF stays on disk. A harbour such as Sydney Harbour is hundreds
of open shore ways, and those relations sit at the end of the file. Their
ids are collected on a second thread while this read fills the node index,
so the shores are known before the first way and the file is not walked
twice in a row. Untagged nodes are dropped in C++. A way that would not be
painted, and is not one of those shores, is dropped before its nodes are
read.

Every remaining way is one bounding box, taken in one pass from the
coordinates already packed on its nodes. A box that misses the selection
is dropped there — a small area does not then inspect each vertex. A box
that meets the selection keeps those same coordinates for the precise test,
so a kept road is not read a second time. A water shore keeps the packed
pairs. Its coordinate list is built only when that lake or harbour meets
the selection, from the pairs still in memory at the end of this read.

The boxes, coordinates and tags are also stored in a grid for the whole
region. The next selection in that daily reads the cells it touches and
does not open the state file. The reader decompresses on a pool of threads.
pyosmium is a library in the program file, the same way Flask and Pillow are:
generating a map does not shell out to a second install.
"""
from __future__ import annotations

import array
import ctypes
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import knoxstop

from .geofabrik import Region, _Rate, ensure_pbf, load_index, cover
from .osm import (
    FILTERS_VERSION, OSMFeature, OVERPASS_FILTERS, RAIL_GROUNDS, assemble_rings,
)

# Local clips older than this still used the closed-ring rule and dropped
# open-sided water (Sydney Harbour stayed the climate's forest). The filter
# list is unchanged, so this is not FILTERS_VERSION.
_CLIP_VERSION = 3
# One grid for a regional daily. A later selection reads cells, not the PBF.
_SPAN_VERSION = 1
_SPAN_DEG = 0.25
# A way this many cells across is stored once, not copied into every cell.
_SPAN_WIDE = 4

_KIND = {"way": "w", "node": "n", "relation": "r"}
_LINE_KEYS = ("highway", "railway", "barrier")
_LINE_WATERWAYS = frozenset({
    "river", "stream", "canal", "ditch", "drain", "pressurised", "tidal_channel",
    "fairway", "dam", "weir", "lock_gate",
})


def _require():
    """The reader ships inside the program. A checkout gets it from requirements."""
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError(
            "KnoxMap's map reader is missing from this copy. "
            "Download the program file again. A git checkout installs it with "
            "the other libraries in KnoxMap/requirements.txt.") from exc
    return osmium


class _Stop:
    """Look for a stop often enough to answer, without a call per object."""

    def __init__(self, should_stop):
        self.should_stop = should_stop
        self.n = 0

    def tick(self) -> None:
        self.n += 1
        if self.n % 8192 == 0:
            knoxstop.check(self.should_stop, "the download")
            # The scan stays in the reader between these checks. Give the
            # progress poll a turn so the percent and the time left can move.
            time.sleep(0)


def osmium_expressions(filters=None) -> list[str]:
    """Overpass tag filters as the tag expressions the reader matches."""
    import re
    tag = re.compile(r'\["([^"]+)"(?:(~|=)"([^"]*)")?\]')
    alt = re.compile(r"^\^\((.*)\)\$$")
    out: list[str] = []
    for expr in filters if filters is not None else OVERPASS_FILTERS:
        kind = expr.split("[", 1)[0]
        letters = list(_KIND[kind]) if kind in _KIND else ["n", "w", "r"]
        parts = tag.findall(expr)
        if not parts:
            continue
        key, op, value = parts[0]
        values = _values(op, value, alt)
        for letter in letters:
            if not values:
                out.append(f"{letter}/{key}")
            else:
                out.extend(f"{letter}/{key}={item}" for item in values)
    # Stable and unique; each expression is an alternative.
    return list(dict.fromkeys(out))


def _values(op: str, value: str, alt) -> list[str]:
    if not op:
        return []
    if op == "=":
        return [value]
    match = alt.match(value)
    if match:
        return [item for item in match.group(1).split("|") if item]
    return []


def _rules(filters=None) -> list[tuple[str, str, str | None]]:
    rules = []
    for expr in osmium_expressions(filters):
        kind, rest = expr.split("/", 1)
        if "=" in rest:
            key, value = rest.split("=", 1)
            rules.append((kind, key, value))
        else:
            rules.append((kind, rest, None))
    return rules


def _rule_table(rules) -> dict[str, dict[str, set[str] | None]]:
    """Rules by kind, then key: the values that count, or None for any value."""
    table: dict[str, dict[str, set[str] | None]] = {"n": {}, "w": {}, "r": {}}
    for letter, key, value in rules:
        slot = table.setdefault(letter, {})
        if value is None:
            slot[key] = None
        elif key not in slot:
            slot[key] = {value}
        elif slot[key] is not None:
            slot[key].add(value)
    return table


def _matches(kind: str, tags, table) -> bool:
    for key, values in table.get(kind, {}).items():
        value = tags.get(key)
        if value is None:
            continue
        if values is None or value in values:
            return True
    return False


def _tag_dict(tags) -> dict:
    return {str(key): str(value) for key, value in dict(tags).items()}


def _member_kind(member) -> str:
    raw = member.type
    if raw in ("n", "w", "r"):
        return raw
    text = str(raw).lower()
    if text in ("n", "w", "r"):
        return text
    if "way" in text:
        return "w"
    if "node" in text:
        return "n"
    if "relation" in text:
        return "r"
    return ""


def _in_box(loc, south: float, west: float, north: float, east: float) -> bool:
    try:
        if not loc.valid():
            return False
    except Exception:
        return False
    return south <= loc.lat <= north and west <= loc.lon <= east


_INDEX = threading.Lock()
_PROGRESS = threading.Lock()


class _Halt:
    """Stop every region walk when one of them fails or the window asks."""

    def __init__(self, should_stop):
        self.should_stop = should_stop
        self.failed = False

    def __call__(self) -> bool:
        if self.failed:
            return True
        return bool(self.should_stop and self.should_stop())


def _report(progress, *args) -> None:
    if progress is None:
        return
    with _PROGRESS:
        progress(*args)


class _ScanMeter:
    """How far the reader has got through one daily, as a percent and a time left.

    Untagged nodes are dropped in C++ and never reach this thread, so counting
    the objects Python sees stays near zero for most of the walk. The reader
    still advances through the file. That position is what the page shows,
    the same way a download shows bytes arrived. It is only read, never moved,
    and a missing position leaves the step on its label.
    """

    def __init__(self, path: str, on_bytes):
        self.path = os.path.realpath(path)
        self.on_bytes = on_bytes
        try:
            self.total = os.path.getsize(path)
        except OSError:
            self.total = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last = 0.0
        self._seen = 0
        self._rate = _Rate()
        self._win_handle: int | None = None

    def start(self) -> None:
        if self.on_bytes is None or self.total <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name="knox-scan", daemon=True)
        self._thread.start()

    def stop(self, complete: bool = False) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._thread = None
        if complete and self.on_bytes is not None and self.total > 0:
            self.on_bytes(self.total, self.total, 0.0)
            return
        self._sample(force=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._sample()
            except Exception:
                pass
            if self._stop.wait(0.4):
                break

    def _sample(self, force: bool = False) -> None:
        if self.on_bytes is None or self.total <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if not force and self._last and now - self._last < 0.45:
                return
            try:
                pos = _read_pos(self.path, self)
            except Exception:
                return
            if pos is None:
                return
            if pos < self._seen:
                # The water pass reads the file to the end, then the clip
                # opens it again. Keeping the old offset left the page at
                # 100% for that whole second walk.
                self._rate = _Rate()
            self._seen = pos
            speed = self._rate.add(now, pos)
            self._last = now
            done = min(pos, self.total)
            total = self.total
        self.on_bytes(done, total, speed)


def _read_pos(path: str, meter: _ScanMeter) -> int | None:
    """Bytes the kernel has already handed the reader. None when it can't be seen."""
    if os.name == "nt":
        return _read_pos_windows(path, meter)
    if sys.platform == "linux":
        return _read_pos_linux(path)
    if sys.platform == "darwin":
        return _read_pos_darwin(path)
    return None


def _read_pos_linux(path: str) -> int | None:
    best = None
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return None
    for name in names:
        fd_path = f"/proc/self/fd/{name}"
        try:
            if os.path.realpath(fd_path) != path:
                continue
            with open(f"/proc/self/fdinfo/{name}", encoding="ascii", errors="replace") as fh:
                for line in fh:
                    if not line.startswith("pos:"):
                        continue
                    pos = int(line.split(":", 1)[1].strip())
                    if best is None or pos > best:
                        best = pos
                    break
        except (OSError, ValueError):
            continue
    return best


def _read_pos_darwin(path: str) -> int | None:
    """File offset from the process fd table. fcntl and proc_pidfdinfo only read it."""
    import ctypes
    import ctypes.util

    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        return None
    libc = ctypes.CDLL(libc_name, use_errno=True)
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    libc.fcntl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
    libc.fcntl.restype = ctypes.c_int
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidfdinfo.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libproc.proc_pidfdinfo.restype = ctypes.c_int

    proc_pidlistfds = 1
    fdinfo_size = 8
    vnode_path_info = 2
    vnode = 1
    f_getpath = 50
    maxpath = 1024
    pid = os.getpid()
    need = libproc.proc_pidinfo(pid, proc_pidlistfds, 0, None, 0)
    if need <= 0:
        return None
    table = ctypes.create_string_buffer(need + fdinfo_size * 8)
    got = libproc.proc_pidinfo(pid, proc_pidlistfds, 0, table, len(table))
    if got <= 0:
        return None
    pathbuf = ctypes.create_string_buffer(maxpath)
    info = ctypes.create_string_buffer(8192)
    best = None
    for index in range(got // fdinfo_size):
        base = index * fdinfo_size
        fd = int.from_bytes(table.raw[base:base + 4], "little", signed=True)
        kind = int.from_bytes(table.raw[base + 4:base + 8], "little")
        if kind != vnode or fd < 0:
            continue
        if libc.fcntl(fd, f_getpath, pathbuf) < 0:
            continue
        try:
            found = os.path.realpath(pathbuf.value.decode("utf-8", "replace"))
        except OSError:
            continue
        if found != path:
            continue
        # proc_fileinfo starts the vnode record: two uint32s, then the offset.
        if libproc.proc_pidfdinfo(pid, fd, vnode_path_info, info, len(info)) < 16:
            continue
        pos = int.from_bytes(info.raw[8:16], "little", signed=True)
        if pos < 0:
            continue
        if best is None or pos > best:
            best = pos
    return best


_WIN = None


def _win_apis():
    """kernel32 and ntdll, loaded once.

    Reloading them for every handle made each progress sample rebuild the
    DLL bindings, and that work ran on the same interpreter as the clip.
    """
    global _WIN
    if _WIN is not None:
        return _WIN
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    ntdll.NtQuerySystemInformation.argtypes = [
        ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
    ]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_ulong
    ntdll.NtQueryInformationFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
    ]
    ntdll.NtQueryInformationFile.restype = ctypes.c_ulong
    _WIN = (kernel32, ntdll)
    return _WIN


def _read_pos_windows(path: str, meter: _ScanMeter) -> int | None:
    """Current byte offset of the daily, queried and not seeked.

    A second handle at offset 0 (a scanner, this process opening the same
    path) is ignored once the reader has moved. Until then every match is
    rechecked, so the cached handle is the one that is actually reading.
    """
    if meter._win_handle is not None:
        pos = _win_offset(meter._win_handle)
        if pos is None:
            meter._win_handle = None
        elif pos > 0:
            return pos
    found = _win_scan(path)
    if not found:
        return None
    found.sort(key=lambda item: item[1])
    handle, pos = found[-1]
    if len(found) == 1 or pos > 0:
        meter._win_handle = handle
    return pos


def _win_scan(path: str) -> list[tuple[int, int]]:
    import ctypes

    ntdll = _win_apis()[1]

    class Entry(ctypes.Structure):
        _fields_ = [
            ("Object", ctypes.c_void_p),
            ("UniqueProcessId", ctypes.c_size_t),
            ("HandleValue", ctypes.c_size_t),
            ("GrantedAccess", ctypes.c_ulong),
            ("CreatorBackTraceIndex", ctypes.c_ushort),
            ("ObjectTypeIndex", ctypes.c_ushort),
            ("HandleAttributes", ctypes.c_ulong),
            ("Reserved", ctypes.c_ulong),
        ]

    # SystemExtendedHandleInformation. Grow until the table fits.
    size = 1 << 20
    buf = None
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        retlen = ctypes.c_ulong(0)
        status = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(retlen))
        if status == 0:
            break
        size = max(size * 2, int(retlen.value) + 65536)
        if size > 32 << 20:
            return []
        buf = None
    if buf is None:
        return []

    base = ctypes.addressof(buf)
    count = ctypes.c_size_t.from_address(base).value
    entry_size = ctypes.sizeof(Entry)
    header = ctypes.sizeof(ctypes.c_size_t) * 2
    if count > 1_000_000 or header + count * entry_size > len(buf):
        return []
    pid = os.getpid()
    matches = []
    for index in range(count):
        entry = Entry.from_address(base + header + index * entry_size)
        if int(entry.UniqueProcessId) != pid or not entry.HandleValue:
            continue
        pos = _win_query(int(entry.HandleValue), path)
        if pos is not None:
            matches.append((int(entry.HandleValue), pos))
    return matches


def _win_dup(handle_value: int):
    """A duplicated disk-file handle, or None. The caller closes it."""
    import ctypes
    from ctypes import wintypes

    kernel32, _ntdll = _win_apis()
    current = kernel32.GetCurrentProcess()
    dup = wintypes.HANDLE()
    if not kernel32.DuplicateHandle(
            current, wintypes.HANDLE(handle_value), current,
            ctypes.byref(dup), 0, False, 0x2):
        return None
    if kernel32.GetFileType(dup) != 1:  # FILE_TYPE_DISK
        kernel32.CloseHandle(dup)
        return None
    return dup


def _win_file_pos(dup) -> int | None:
    """Byte offset of a handle already known to be a disk file."""
    import ctypes

    _kernel32, ntdll = _win_apis()

    class IoStatus(ctypes.Structure):
        _fields_ = [("Pointer", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class Position(ctypes.Structure):
        _fields_ = [("CurrentByteOffset", ctypes.c_longlong)]

    info = Position()
    iosb = IoStatus()
    # FilePositionInformation. The query copies the offset out.
    status = ntdll.NtQueryInformationFile(
        dup,
        ctypes.c_void_p(ctypes.addressof(iosb)),
        ctypes.c_void_p(ctypes.addressof(info)),
        ctypes.sizeof(info),
        14,
    )
    if status != 0 or info.CurrentByteOffset < 0:
        return None
    return int(info.CurrentByteOffset)


def _win_offset(handle_value: int) -> int | None:
    """Offset of a handle already matched to the daily. No path check."""
    kernel32, _ntdll = _win_apis()
    dup = _win_dup(handle_value)
    if dup is None:
        return None
    try:
        return _win_file_pos(dup)
    finally:
        kernel32.CloseHandle(dup)


def _win_query(handle_value: int, path: str) -> int | None:
    """Position of this handle when it is the daily. None for any other handle."""
    import ctypes

    kernel32, _ntdll = _win_apis()
    dup = _win_dup(handle_value)
    if dup is None:
        return None
    try:
        wide = ctypes.create_unicode_buffer(4096)
        nchars = kernel32.GetFinalPathNameByHandleW(dup, wide, len(wide), 0)
        if not nchars or nchars >= len(wide):
            return None
        if not _same_file(wide.value, path):
            return None
        return _win_file_pos(dup)
    finally:
        kernel32.CloseHandle(dup)


def _same_file(left: str, right: str) -> bool:
    text = left
    if text.startswith("\\\\?\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text.startswith("\\\\?\\"):
        text = text[4:]
    try:
        return os.path.samefile(text, right)
    except OSError:
        return os.path.normcase(os.path.abspath(text)) == os.path.normcase(os.path.abspath(right))


def _mtime_ms(path: str) -> int:
    return int(os.path.getmtime(path) * 1000)


def _clips_root(root: str) -> str:
    from .geofabrik import cache_dir
    path = os.path.join(cache_dir(root), "clips")
    os.makedirs(path, exist_ok=True)
    return path


def _digest(region: Region, source: str, box: tuple[float, float, float, float]) -> str:
    south, west, north, east = box
    raw = (f"{region.id}|{south:.7f}|{west:.7f}|{north:.7f}|{east:.7f}|"
           f"{_mtime_ms(source)}|{FILTERS_VERSION}")
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _clip_dir(root: str, region: Region, source: str,
              box: tuple[float, float, float, float]) -> str:
    return os.path.join(_clips_root(root), _digest(region, source, box))


def _read_meta(folder: str) -> dict | None:
    path = os.path.join(folder, "meta.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _covers(outer: tuple[float, float, float, float],
            inner: tuple[float, float, float, float], eps: float = 1e-5) -> bool:
    return (outer[0] <= inner[0] + eps and outer[1] <= inner[1] + eps
            and outer[2] + eps >= inner[2] and outer[3] + eps >= inner[3])


def _same_box(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float], eps: float = 1e-6) -> bool:
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def _clip_ready(folder: str, box: tuple[float, float, float, float]) -> bool:
    if not os.path.isfile(os.path.join(folder, "complete")):
        return False
    meta = _read_meta(folder)
    if not meta or meta.get("filters") != FILTERS_VERSION:
        return False
    if meta.get("clip") != _CLIP_VERSION:
        return False
    try:
        clip = tuple(float(v) for v in meta["box"])
    except (KeyError, TypeError, ValueError):
        return False
    return len(clip) == 4 and _covers(clip, box)


def _index_path(root: str, region: Region) -> str:
    return os.path.join(_clips_root(root), region.slug + ".idx.json")


def _load_items(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_items(path: str, items: list) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh)
    os.replace(tmp, path)


def _sync_records(root: str, region: Region, stamp: int) -> list:
    """Clips of this region made from the daily that is on disk now."""
    path = _index_path(root, region)
    kept = []
    changed = False
    for item in _load_items(path):
        if not isinstance(item, dict):
            changed = True
            continue
        folder = os.path.join(_clips_root(root), str(item.get("dir") or ""))
        try:
            clip_box = tuple(float(v) for v in item.get("box") or ())
        except (TypeError, ValueError):
            clip_box = ()
        stale = (item.get("mtime_ms") != stamp or item.get("filters") != FILTERS_VERSION
                 or len(clip_box) != 4 or not _clip_ready(folder, clip_box))
        if stale:
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
            changed = True
            continue
        kept.append(item)
    if changed:
        _write_items(path, kept)
    return kept


def _remember(root: str, region: Region, source: str,
              box: tuple[float, float, float, float], folder: str) -> None:
    stamp = _mtime_ms(source)
    digest = os.path.basename(folder)
    with _INDEX:
        kept = [item for item in _sync_records(root, region, stamp)
                if item.get("dir") != digest]
        kept.append({
            "mtime_ms": stamp,
            "filters": FILTERS_VERSION,
            "box": [box[0], box[1], box[2], box[3]],
            "dir": digest,
        })
        _write_items(_index_path(root, region), kept)


def _find_clip(root: str, region: Region, source: str,
               box: tuple[float, float, float, float]) -> str | None:
    exact = _clip_dir(root, region, source, box)
    if _clip_ready(exact, box):
        return exact
    best = None
    best_area = None
    with _INDEX:
        items = _sync_records(root, region, _mtime_ms(source))
    for item in items:
        clip_box = tuple(item["box"])
        if not _covers(clip_box, box):
            continue
        area = max(0.0, clip_box[2] - clip_box[0]) * max(0.0, clip_box[3] - clip_box[1])
        if best_area is None or area < best_area:
            best_area = area
            best = os.path.join(_clips_root(root), item["dir"])
    return best


def _local_pbf(root: str, region: Region) -> str:
    from .geofabrik import pbf_path
    path = pbf_path(root, region)
    if os.path.isfile(path) and os.path.getsize(path) >= 1000:
        return path
    return ensure_pbf(root, region)


def _walk_plan(sources: list[str]) -> tuple[int, int]:
    """How many regions to walk at once, and how many threads each reader gets.

    Decompression is the parallel part. Two whole-state node tables at once
    is a lot of memory, so a large daily is walked on its own and the threads
    go to reading that one file.
    """
    cpus = os.cpu_count() or 1
    biggest = 0
    for path in sources:
        try:
            biggest = max(biggest, os.path.getsize(path))
        except OSError:
            pass
    if len(sources) <= 1 or biggest >= 800_000_000:
        return 1, cpus
    jobs = min(len(sources), 2, cpus)
    return jobs, max(1, cpus // jobs)


def _grid_shape(box: tuple[float, float, float, float]) -> tuple[int, int]:
    south, west, north, east = box
    rows = min(16, max(1, int(round(max(0.0, north - south) / 0.2))))
    cols = min(16, max(1, int(round(max(0.0, east - west) / 0.2))))
    return rows, cols


def _location_storage(source: str, scratch: str) -> str:
    try:
        size = os.path.getsize(source)
    except OSError:
        size = 0
    # The node coordinate table is the memory cost of the one walk. flex_mem
    # answers way lookups from RAM. Past a couple of GB a mapped file is what
    # still fits, and it is filled during this same read.
    if size <= 2_000_000_000:
        return "flex_mem"
    return "sparse_mmap_array," + os.path.join(scratch, "nodes.store")


def _seg_hits(lat1: float, lon1: float, lat2: float, lon2: float,
              south: float, west: float, north: float, east: float) -> bool:
    """True when the segment meets the box, including along its edge."""
    left, right, bottom, top = 1, 2, 4, 8

    def code(lat: float, lon: float) -> int:
        bits = 0
        if lon < west:
            bits |= left
        elif lon > east:
            bits |= right
        if lat < south:
            bits |= bottom
        elif lat > north:
            bits |= top
        return bits

    c1 = code(lat1, lon1)
    c2 = code(lat2, lon2)
    for _ in range(8):
        if c1 == 0 or c2 == 0:
            return True
        if c1 & c2:
            return False
        out = c1 or c2
        if out & top:
            if lat2 == lat1:
                return False
            lon = lon1 + (lon2 - lon1) * (north - lat1) / (lat2 - lat1)
            lat = north
        elif out & bottom:
            if lat2 == lat1:
                return False
            lon = lon1 + (lon2 - lon1) * (south - lat1) / (lat2 - lat1)
            lat = south
        elif out & right:
            if lon2 == lon1:
                return False
            lat = lat1 + (lat2 - lat1) * (east - lon1) / (lon2 - lon1)
            lon = east
        else:
            if lon2 == lon1:
                return False
            lat = lat1 + (lat2 - lat1) * (west - lon1) / (lon2 - lon1)
            lon = west
        if out == c1:
            lat1, lon1 = lat, lon
            c1 = code(lat, lon)
        else:
            lat2, lon2 = lat, lon
            c2 = code(lat, lon)
    return False


def _separated(lat: float, lon: float, prev: tuple[float, float],
               south: float, west: float, north: float, east: float) -> bool:
    plat, plon = prev
    if lon < west and plon < west:
        return True
    if lon > east and plon > east:
        return True
    if lat < south and plat < south:
        return True
    if lat > north and plat > north:
        return True
    return False


def _pip(coords: list[tuple[float, float]], lat: float, lon: float) -> bool:
    """Ray cast. coords are (lat, lon), closed or not."""
    inside = False
    count = len(coords)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        lati, loni = coords[i]
        latj, lonj = coords[j]
        if (lati > lat) != (latj > lat):
            span = latj - lati
            if span != 0.0:
                cross = (lonj - loni) * (lat - lati) / span + loni
                if lon < cross:
                    inside = not inside
        j = i
    return inside


def _covers_query(coords: list[tuple[float, float]],
                  south: float, west: float, north: float, east: float) -> bool:
    """A closed ring that has the whole box inside it."""
    if len(coords) < 4 or coords[0] != coords[-1]:
        return False
    lats = [lat for lat, _lon in coords]
    lons = [lon for _lat, lon in coords]
    if min(lats) > south or max(lats) < north or min(lons) > west or max(lons) < east:
        return False
    return _pip(coords, (south + north) / 2.0, (west + east) / 2.0)


def _ring_hits(coords: list[tuple[float, float]],
               south: float, west: float, north: float, east: float) -> bool:
    prev = None
    for lat, lon in coords:
        if south <= lat <= north and west <= lon <= east:
            return True
        if prev is not None and not _separated(lat, lon, prev, south, west, north, east):
            if _seg_hits(prev[0], prev[1], lat, lon, south, west, north, east):
                return True
        prev = (lat, lon)
    return False


def _feature_hits(feature: OSMFeature,
                  south: float, west: float, north: float, east: float) -> bool:
    if feature.role_geoms:
        rings = feature.role_geoms
    else:
        rings = (("outer", feature.geometry),)
    # A vertex inside the box is a hit, the same as _ring_hits. A ring whose
    # points all sit clear of the box cannot cross it or contain it, so the
    # segment walk is skipped for everything a smaller piece does not touch.
    try:
        min_lat = min_lon = float("inf")
        max_lat = max_lon = float("-inf")
        for _role, ring in rings:
            if not ring:
                continue
            for lat, lon in ring:
                if south <= lat <= north and west <= lon <= east:
                    return True
                if lat < min_lat:
                    min_lat = lat
                if lat > max_lat:
                    max_lat = lat
                if lon < min_lon:
                    min_lon = lon
                if lon > max_lon:
                    max_lon = lon
    except (TypeError, ValueError):
        pass
    else:
        if max_lat < south or min_lat > north or max_lon < west or min_lon > east:
            return False
    outers = []
    inners = []
    for role, ring in rings:
        if ring and _ring_hits(ring, south, west, north, east):
            return True
        if role == "inner":
            inners.append(ring)
        else:
            outers.append(ring)
    # No edge met the box. A ring can still contain it: the shore of a lake
    # that is bigger than this piece.
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0
    for outer in outers:
        if not outer or len(outer) < 4 or outer[0] != outer[-1]:
            continue
        if not _pip(outer, mid_lat, mid_lon):
            continue
        if any(inner and len(inner) >= 4 and _pip(inner, mid_lat, mid_lon) for inner in inners):
            continue
        return True
    return False


def _classify_way(obj, south: float, west: float, north: float, east: float):
    """'hit', 'cover', or 'miss', and the way's (lat, lon) points when kept.

    A hit crosses the box. A cover is a closed ring with the box inside it,
    which is how a lake larger than the selection still gets drawn. Anything
    else is dropped without keeping its coordinates.
    """
    nodes = obj.nodes
    count = len(nodes)
    if count < 2:
        return "miss", None
    hit = False
    prev = None
    min_lat = 90.0
    max_lat = -90.0
    min_lon = 180.0
    max_lon = -180.0
    first = None
    last = None
    for node in nodes:
        loc = node.location
        try:
            if not loc.valid():
                return "miss", None
        except Exception:
            return "miss", None
        lat = loc.lat
        lon = loc.lon
        if lat < min_lat:
            min_lat = lat
        if lat > max_lat:
            max_lat = lat
        if lon < min_lon:
            min_lon = lon
        if lon > max_lon:
            max_lon = lon
        if first is None:
            first = (lat, lon)
        last = (lat, lon)
        if not hit:
            if south <= lat <= north and west <= lon <= east:
                hit = True
            elif prev is not None and not _separated(lat, lon, prev, south, west, north, east):
                if _seg_hits(prev[0], prev[1], lat, lon, south, west, north, east):
                    hit = True
        prev = (lat, lon)
    closed = count >= 4 and (nodes[0].ref == nodes[-1].ref or first == last)
    if not hit:
        if not closed or min_lat > south or max_lat < north or min_lon > west or max_lon < east:
            return "miss", None
    coords = [(node.location.lat, node.location.lon) for node in nodes]
    if not hit:
        if not _pip(coords, (south + north) / 2.0, (west + east) / 2.0):
            return "miss", None
        return "cover", coords
    return "hit", coords


def _way_coords(obj) -> list[tuple[float, float]] | None:
    """All coordinates of a retained relation member, wherever it lies."""
    coords = []
    for node in obj.nodes:
        try:
            if not node.location.valid():
                return None
        except Exception:
            return None
        coords.append((node.location.lat, node.location.lon))
    return coords if len(coords) >= 2 else None


def _lonlat(coords: list[tuple[float, float]]) -> list:
    return [[lon, lat] for lat, lon in coords]


def _feature_line(kind: str, osm_id: int, tags: dict, geometry: dict) -> str:
    props = dict(tags)
    props["@id"] = osm_id
    props["@type"] = kind
    body = json.dumps(
        {"type": "Feature", "geometry": geometry, "properties": props},
        separators=(",", ":"))
    return "\x1e" + body + "\n"


class _ClipWriter:
    """One geojsonseq file per grid cell, plus cover.jsonl for rings that
    contain the whole selection. A later piece reads only the cells it touches.
    """

    def __init__(self, folder: str, box: tuple[float, float, float, float],
                 rows: int, cols: int):
        self.folder = folder
        self.box = box
        self.rows = rows
        self.cols = cols
        self._fh: dict[str, object] | None = {}
        os.makedirs(folder, exist_ok=True)

    def _bucket(self, lat: float) -> int:
        south, _west, north, _east = self.box
        if self.rows <= 1 or north <= south:
            return 0
        i = int((lat - south) / (north - south) * self.rows)
        if i < 0:
            return 0
        if i >= self.rows:
            return self.rows - 1
        return i

    def _slice(self, lon: float) -> int:
        _south, west, _north, east = self.box
        if self.cols <= 1 or east <= west:
            return 0
        i = int((lon - west) / (east - west) * self.cols)
        if i < 0:
            return 0
        if i >= self.cols:
            return self.cols - 1
        return i

    def _open(self, name: str):
        assert self._fh is not None
        fh = self._fh.get(name)
        if fh is None:
            fh = open(os.path.join(self.folder, name), "w", encoding="utf-8", newline="\n")
            self._fh[name] = fh
        return fh

    def add(self, kind: str, osm_id: int, tags: dict, geometry: dict,
            coords: list[tuple[float, float]], cover: bool) -> None:
        line = _feature_line(kind, osm_id, tags, geometry)
        if not cover:
            rows = []
            cols = []
            for lat, lon in coords:
                rows.append(self._bucket(lat))
                cols.append(self._slice(lon))
            if not rows:
                cover = True
            else:
                targets = [(row, col)
                           for row in range(min(rows), max(rows) + 1)
                           for col in range(min(cols), max(cols) + 1)]
                if len(targets) > 32:
                    cover = True
                else:
                    for row, col in targets:
                        self._open(f"r{row}c{col}.jsonl").write(line)
                    return
        self._open("cover.jsonl").write(line)

    def abort(self) -> None:
        if not self._fh:
            self._fh = None
            return
        for fh in self._fh.values():
            fh.close()
        self._fh = None

    def finish(self) -> None:
        self.abort()
        south, west, north, east = self.box
        with open(os.path.join(self.folder, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "box": [south, west, north, east],
                "rows": self.rows,
                "cols": self.cols,
                "filters": FILTERS_VERSION,
                "clip": _CLIP_VERSION,
            }, fh)
        with open(os.path.join(self.folder, "complete"), "w", encoding="utf-8") as fh:
            fh.write("ok\n")


def _plain_members(obj) -> list[tuple[str, int, str]]:
    """(kind, id, role) copied off the relation before the reader moves on."""
    out = []
    for member in obj.members:
        role = str(member.role or "").strip().lower()
        out.append((_member_kind(member), int(member.ref), role))
    return out


def _member_lines_plain(members, stored: dict) -> list[tuple[str, list[tuple[float, float]]]]:
    """Member ways this walk kept, open or closed, with outer/inner roles."""
    lines = []
    for kind, ref, role in members:
        if kind != "w":
            continue
        coords = stored.get(int(ref))
        if not coords or len(coords) < 2:
            continue
        lines.append(("inner" if role == "inner" else "outer", coords))
    return lines


def _member_lines(obj, stored: dict) -> list[tuple[str, list[tuple[float, float]]]]:
    return _member_lines_plain(_plain_members(obj), stored)


def _closed_role_rings(lines: list[tuple[str, list[tuple[float, float]]]]
                       ) -> tuple[list, list] | None:
    """Outers and inners that actually close.

    ``assemble_rings`` keeps an unclosed chain when the pieces do not meet.
    Painted as a polygon that chain is a wedge, which is how a harbour used
    to come out as a few slivers of water. Those chains are dropped here.
    """
    outers = []
    inners = []
    for role, ring in assemble_rings(lines):
        if len(ring) < 4 or ring[0] != ring[-1]:
            continue
        if role == "inner":
            inners.append(ring)
        else:
            outers.append(ring)
    if not outers:
        return None
    return outers, inners


def _member_envelope_misses(lines, south: float, west: float, north: float,
                            east: float) -> bool:
    """True when every stored vertex lies strictly outside the box.

    Joining those ways cannot add a point outside their envelope, so the
    ring cannot meet the selection or contain it. The shapely join is what
    made a state-sized extract sit at 100% after the file had been read.
    """
    min_lat = 90.0
    max_lat = -90.0
    min_lon = 180.0
    max_lon = -180.0
    seen = False
    for _role, coords in lines:
        for lat, lon in coords:
            seen = True
            if lat < min_lat:
                min_lat = lat
            if lat > max_lat:
                max_lat = lat
            if lon < min_lon:
                min_lon = lon
            if lon > max_lon:
                max_lon = lon
    if not seen:
        return True
    return max_lat < south or min_lat > north or max_lon < west or min_lon > east


def _relation_geometry(obj, stored: dict, box: tuple[float, float, float, float]):
    """One polygon for a multipolygon, including a harbour of open shores."""
    members = _plain_members(obj)
    water = obj.tags.get("natural") == "water"
    ways = sum(kind == "w" for kind, _ref, _role in members)
    return _relation_geometry_plain(obj.tags, members, stored, box, water, ways)


def _relation_geometry_plain(tags, members, stored: dict,
                             box: tuple[float, float, float, float],
                             water: bool, way_members: int):
    """Same assembly as ``_relation_geometry``, from values already copied.

    Closed member ways are joined as before. Open ways are stitched end to
    end, which is how Port Jackson is mapped: relation 15522136 is
    natural=water, and its outer is 484 shoreline ways, most of them with no
    tags, rather than one ring. Water is painted only from a completed ring;
    member-way direction is not a safe indication of which side is water.
    """
    south, west, north, east = box
    lines = _member_lines_plain(members, stored)
    if not any(role != "inner" for role, _ring in lines):
        return None
    if water and len(lines) != way_members:
        return None
    if _member_envelope_misses(lines, south, west, north, east):
        return None
    closed = _closed_role_rings(lines)
    if closed is None:
        return None
    outers, inners = closed
    if not any(_ring_hits(outer, south, west, north, east)
               or _covers_query(outer, south, west, north, east)
               for outer in outers):
        return None
    if len(outers) == 1:
        groups = [(outers[0], inners)]
    else:
        assigned = [[] for _ in outers]
        for inner in inners:
            lat, lon = inner[0]
            placed = False
            for index, outer in enumerate(outers):
                if _pip(outer, lat, lon):
                    assigned[index].append(inner)
                    placed = True
                    break
            if not placed:
                assigned[0].append(inner)
        groups = list(zip(outers, assigned))
    polygons = []
    flat: list[tuple[float, float]] = []
    cover = False
    for outer, holes in groups:
        flat.extend(outer)
        for hole in holes:
            flat.extend(hole)
        if _covers_query(outer, south, west, north, east):
            cover = True
        polygons.append([_lonlat(outer), *[_lonlat(hole) for hole in holes]])
    if len(polygons) == 1:
        geometry = {"type": "Polygon", "coordinates": polygons[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": polygons}
    return geometry, flat, cover


# Asked before the rest of a way's tags. Roads and buildings are most of a
# daily, and a direct lookup does not build a Tag for every key on the way.
_PAINT_HOT = ("highway", "building", "natural", "landuse", "barrier", "waterway")
_PAINT_HOT_SET = set(_PAINT_HOT)


def _way_paint(tags, slot) -> bool:
    """Same answer as ``_matches`` for a way.

    ``slot`` maps a key to the values that count, or None when any value
    counts. A missing key is the sentinel False so None (any value) stays
    distinct. The common keys are read by name; anything else is still one
    pass over the tags the way actually carries.
    """
    for key in _PAINT_HOT:
        wanted = slot.get(key, False)
        if wanted is False:
            continue
        value = tags.get(key)
        if value is None:
            continue
        if wanted is None or value in wanted:
            return True
    for item in tags:
        if item.k in _PAINT_HOT_SET:
            continue
        wanted = slot.get(item.k, False)
        if wanted is False:
            continue
        if wanted is None or item.v in wanted:
            return True
    return False


def _closed_ring(way) -> bool:
    """True when ``_relation_geometry`` could use this way as a ring.

    That needs four or more nodes and matching ends. Both tests are libosmium
    calls on the way: ``is_closed`` compares the end ids, and
    ``ends_have_same_location`` compares the two end coordinates. Neither
    walks the node list in Python.
    """
    if len(way.nodes) < 4:
        return False
    return way.is_closed() or way.ends_have_same_location()


# WKB from this pyosmium build uses the machine's endian. Swap only when the
# blob disagrees, so a little-endian linestring is not flipped on Windows.
_NATIVE_LITTLE = array.array("H", b"\x01\x00")[0] == 1


# OSM stores a coordinate as an int, 1e-7 degrees. Two units is about 2 cm,
# wider than the rounding between that int and the double the linestring
# path compares, so a way the integer test calls a miss is a miss there too.
_LOC_SCALE = 10000000.0
# Most ways in a daily are a handful of nodes. A hex linestring of those
# costs more than reading the locations. Longer ways stay in libosmium.
_ENVELOPE_WALK = 24
# libosmium's invalid location is this x (and y). A real longitude is not.
_INVALID_X = -2147483648


def _envelope_limits(south: float, west: float, north: float, east: float):
    return (
        south * _LOC_SCALE - 2.0,
        north * _LOC_SCALE + 2.0,
        west * _LOC_SCALE - 2.0,
        east * _LOC_SCALE + 2.0,
    )


def _span_overlaps(min_x: int, min_y: int, max_x: int, max_y: int, limits) -> bool:
    south_lo, north_hi, west_lo, east_hi = limits
    return (max_y >= south_lo and min_y <= north_hi
            and max_x >= west_lo and min_x <= east_hi)


def _location_span(nodes, indexes):
    """Min and max of the integer locations, or None when one is missing."""
    min_x = min_y = 2147483647
    max_x = max_y = -2147483648
    for i in indexes:
        loc = nodes[i].location
        x = loc.x
        y = loc.y
        if x == _INVALID_X or y == _INVALID_X:
            return None
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
    return min_x, min_y, max_x, max_y


# pyosmium builds a Python NodeRef for every vertex it returns. libosmium
# already stores the way's vertices packed: int64 id, then the same int32 x
# and y the location object reports, 16 bytes apart, after an 8-byte header.
# The slot of that array inside the node-list object is learned once, and
# kept only when every coordinate matches those objects. Anything else keeps
# the location walk. Several clips run at once, so the learned slot is shared.
_SPAN_TRIES = 8
_span_lock = threading.Lock()
_span_layout = None
_span_tries = 0
_I32P = ctypes.POINTER(ctypes.c_int32)
_span_kernel32 = None


class _SpanRegion(ctypes.Structure):
    _fields_ = (
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    )


def _span_qword_ptrs(addr: int, nbytes: int):
    count = nbytes // 8
    raw = (ctypes.c_uint64 * count).from_address(addr)
    for index in range(count):
        ptr = raw[index]
        if 0x10000 <= ptr < (1 << 57) and ptr % 8 == 0:
            yield index * 8, ptr


def _span_xy_match(addr: int, known) -> bool:
    data = ctypes.cast(ctypes.c_void_p(addr), _I32P)
    for index, (x, y) in enumerate(known):
        slot = index * 4
        if data[slot + 2] != x or data[slot + 3] != y:
            return False
    return True


def _span_win_readable(addr: int, nbytes: int) -> bool:
    global _span_kernel32
    if ctypes.sizeof(_SpanRegion) != 48:
        return False
    if _span_kernel32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.VirtualQuery.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
        lib.VirtualQuery.restype = ctypes.c_size_t
        _span_kernel32 = lib
    region = _SpanRegion()
    got = _span_kernel32.VirtualQuery(
        ctypes.c_void_p(addr), ctypes.byref(region), ctypes.sizeof(region))
    if not got or region.State != 0x1000 or region.Protect & 0x100:
        return False
    if (region.Protect & 0xFF) not in (0x02, 0x04, 0x08, 0x20, 0x40, 0x80):
        return False
    base = region.BaseAddress or 0
    if base < 0:
        base += 1 << 64
    return base <= addr and addr + nbytes <= base + region.RegionSize


def _span_match_base(ptr: int, known, need: int, readable) -> int | None:
    # 8 is libosmium's item header. The other offsets are only accepted when
    # they still land on the same coordinates.
    for off in (8, 0, 16, 24):
        addr = ptr + off
        if readable(addr, need) and _span_xy_match(addr, known):
            return off
    return None


def _span_find_layout(base: int, size: int, known, need: int, readable, hop: bool):
    for py_off, ptr in _span_qword_ptrs(base, size):
        if not hop:
            off = _span_match_base(ptr, known, need, readable)
            if off is not None:
                return (py_off, -1, off)
            continue
        # The object may hold a pointer to the way-node list rather than the
        # list itself. Each slot is checked on its own so a short allocation
        # is not read past its end.
        for hop_off in (0, 8, 16, 24):
            slot = ptr + hop_off
            if not readable(slot, 8):
                continue
            inner = ctypes.c_uint64.from_address(slot).value
            if inner < 0x10000 or inner % 8 or inner >= (1 << 57):
                continue
            off = _span_match_base(inner, known, need, readable)
            if off is not None:
                return (py_off, hop_off, off)
    return None


def _probe_span(blob, known):
    """Layout tuple, None when this object did not match, False when this
    process cannot look at the array safely."""
    need = len(known) * 16
    base = id(blob)
    size = sys.getsizeof(blob)
    if os.name == "nt":
        readable = _span_win_readable
        found = _span_find_layout(base, size, known, need, readable, False)
        if found is not None:
            return found
        return _span_find_layout(base, size, known, need, readable, True)
    mem = "/proc/self/mem"
    if not os.path.exists(mem):
        return False
    try:
        fd = os.open(mem, os.O_RDONLY)
    except OSError:
        return False

    def readable(addr, nbytes, _fd=fd):
        try:
            os.pread(_fd, nbytes, addr)
        except OSError:
            return False
        return True

    try:
        found = _span_find_layout(base, size, known, need, readable, False)
        if found is not None:
            return found
        return _span_find_layout(base, size, known, need, readable, True)
    finally:
        os.close(fd)


def _span_sample(nodes, count: int):
    if count < 2:
        return None
    known = []
    nontrivial = False
    invalid = _INVALID_X
    for i in range(count):
        loc = nodes[i].location
        x = loc.x
        y = loc.y
        if x == invalid or y == invalid:
            return None
        if x != 0 or y != 0:
            nontrivial = True
        known.append((x, y))
    if not nontrivial:
        return None
    return known


def _learn_span(nodes, count: int) -> str:
    """'ready' once the packed layout is known, 'invalid' when this way has
    no locations, 'again' when another way has to be tried, 'unable' when
    the array cannot be read in this process."""
    global _span_layout, _span_tries
    if _span_layout is False:
        return "unable"
    if _span_layout is not None:
        return "ready"
    known = _span_sample(nodes, count)
    if known is None:
        return "invalid"
    with _span_lock:
        if _span_layout is False:
            return "unable"
        if _span_layout is not None:
            return "ready"
        try:
            found = _probe_span(nodes._list, known)
        except (MemoryError, knoxstop.Stopped):
            raise
        except Exception:
            found = None
        if found:
            _span_layout = found
            return "ready"
        if found is False:
            _span_layout = False
            return "unable"
        _span_tries += 1
        if _span_tries >= _SPAN_TRIES:
            _span_layout = False
            return "unable"
        return "again"


def _packed_miss(nodes, count: int, limits, layout) -> bool | None:
    """Same answer as ``_envelope_walk_nodes``, from the packed array."""
    py_off, hop_off, data_off = layout
    blob = nodes._list
    if py_off < 0 or py_off + 8 > sys.getsizeof(blob):
        return None
    ptr = ctypes.c_uint64.from_address(id(blob) + py_off).value
    if ptr < 0x10000 or ptr % 8 or ptr >= (1 << 57):
        return None
    if hop_off >= 0:
        ptr = ctypes.c_uint64.from_address(ptr + hop_off).value
        if ptr < 0x10000 or ptr % 8 or ptr >= (1 << 57):
            return None
    data = ctypes.cast(ctypes.c_void_p(ptr + data_off), _I32P)
    south_lo, north_hi, west_lo, east_hi = limits
    min_x = min_y = 2147483647
    max_x = max_y = -2147483648
    invalid = _INVALID_X
    for i in range(count):
        slot = i * 4
        x = data[slot + 2]
        y = data[slot + 3]
        if x == invalid or y == invalid:
            return True
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        # Same comparison as ``_span_overlaps``.
        if (max_y >= south_lo and min_y <= north_hi
                and max_x >= west_lo and min_x <= east_hi):
            return False
    return True


def _envelope_walk_nodes(nodes, count: int, limits) -> bool:
    """True when the integer envelope misses. False once it cannot miss.

    Stops at the first node that pulls the running box over the selection,
    which is the usual way a kept way shows up. A way that misses is only
    reported after every node has been seen.
    """
    min_x = min_y = 2147483647
    max_x = max_y = -2147483648
    for i in range(count):
        loc = nodes[i].location
        x = loc.x
        y = loc.y
        if x == _INVALID_X or y == _INVALID_X:
            return True
        if x < min_x:
            min_x = x
        if x > max_x:
            max_x = x
        if y < min_y:
            min_y = y
        if y > max_y:
            max_y = y
        if _span_overlaps(min_x, min_y, max_x, max_y, limits):
            return False
    return True


def _envelope_walk(nodes, count: int, limits) -> bool:
    """True when the integer envelope misses. False once it cannot miss.

    Stops at the first node that pulls the running box over the selection,
    which is the usual way a kept way shows up. A miss is reported only
    after every node has been seen. The packed array is the same coordinates
    without building a NodeRef per vertex; a layout this process could not
    learn falls back to that walk.
    """
    layout = _span_layout
    if layout is None:
        _learn_span(nodes, count)
        layout = _span_layout
    if layout:
        try:
            packed = _packed_miss(nodes, count, limits, layout)
        except (knoxstop.Stopped, MemoryError):
            raise
        except Exception:
            packed = None
        if packed is not None:
            return packed
    return _envelope_walk_nodes(nodes, count, limits)


def _wkb_envelope_misses(nodes, factory, use_all, south: float, west: float,
                         north: float, east: float) -> bool:
    """True when libosmium's linestring envelope misses the box.

    ``WKBFactory.create_linestring`` walks the node locations inside
    libosmium (pyosmium 4.3 builds a hex-encoded WKB linestring: byte order,
    uint32 type, uint32 count, then lon/lat doubles). Used for long ways,
    where that one C++ walk is cheaper than a Python node per vertex. A
    ring that contains the box still overlaps, so the precise hit/cover
    test can run later. Fewer than two points, or a node with no location,
    is a miss, the same as ``_classify_way``.
    """
    try:
        # WayNodeList, not the Way: create_linestring walks that list in
        # libosmium. Passing the Way itself is only special-cased when the
        # caster sees an osmium::Way, which this build's Way wrapper is not.
        encoded = factory.create_linestring(nodes, use_all)
    except (knoxstop.Stopped, MemoryError, TypeError, AttributeError):
        raise
    except Exception:
        return True
    try:
        raw = encoded if isinstance(encoded, (bytes, bytearray)) else bytes.fromhex(encoded)
    except ValueError:
        return True
    if len(raw) < 9 or raw[0] not in (0, 1):
        return True
    order = "little" if raw[0] == 1 else "big"
    count = int.from_bytes(raw[5:9], order)
    need = 9 + count * 16
    if count < 2 or len(raw) < need:
        return True
    coords = array.array("d")
    try:
        coords.frombytes(raw[9:need])
    except (BufferError, ValueError):
        return True
    if len(coords) != count * 2:
        return True
    if (raw[0] == 1) != _NATIVE_LITTLE:
        coords.byteswap()
    lons = coords[0::2]
    lats = coords[1::2]
    return max(lats) < south or min(lats) > north or max(lons) < west or min(lons) > east


def _envelope_misses(way, factory, use_all, south: float, west: float,
                     north: float, east: float, limits) -> bool:
    """True when the way's node envelope misses the box."""
    try:
        nodes = way.nodes
        count = len(nodes)
        if count < 2:
            return True
        if count <= _ENVELOPE_WALK:
            return _envelope_walk(nodes, count, limits)
        # The two ends already cover the box often enough (a road that
        # crosses it) that the linestring is not needed.
        ends = _location_span(nodes, (0, count - 1))
        if ends is not None and _span_overlaps(*ends, limits):
            return False
        return _wkb_envelope_misses(
            nodes, factory, use_all, south, west, north, east)
    except (knoxstop.Stopped, MemoryError, TypeError, AttributeError):
        raise
    except Exception:
        return True


def _numpy():
    global _np
    if _np is None:
        import numpy as np
        _np = np
    return _np


_np = None
_WAY_REST = struct.Struct("<qiiiiI")
_NODE_REST = struct.Struct("<qiiI")
_REL_REST = struct.Struct("<qiiiiBI")
_MEM_HEAD = struct.Struct("<BqI")


def _measure_packed(nodes, count: int, layout):
    """Bounding box and packed x/y, from the way's own coordinate array.

    One copy, then the min and max run in compiled code. A miss does not
    become a Python value per vertex. False when a node has no location
    (the way is a miss). None when the array cannot be read, so the caller
    falls back for that way and does not publish the grid.
    """
    if count < 2 or not layout:
        return None
    py_off, hop_off, data_off = layout
    blob = nodes._list
    if py_off < 0 or py_off + 8 > sys.getsizeof(blob):
        return None
    ptr = ctypes.c_uint64.from_address(id(blob) + py_off).value
    if ptr < 0x10000 or ptr % 8 or ptr >= (1 << 57):
        return None
    if hop_off >= 0:
        ptr = ctypes.c_uint64.from_address(ptr + hop_off).value
        if ptr < 0x10000 or ptr % 8 or ptr >= (1 << 57):
            return None
    try:
        raw = ctypes.string_at(ptr + data_off, count * 16)
    except (ValueError, OSError):
        return None
    if len(raw) != count * 16:
        return None
    buf = _numpy().frombuffer(raw, dtype="i4")
    if int(buf.size) != count * 4:
        return None
    xs = buf[2::4]
    ys = buf[3::4]
    invalid = _INVALID_X
    # The invalid sentinel is the lowest int32, so it shows up as the minimum.
    if int(xs.min()) == invalid or int(ys.min()) == invalid:
        return False
    xy = _numpy().empty(count * 2, dtype="i4")
    xy[0::2] = xs
    xy[1::2] = ys
    return (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()),
            xy.tobytes())


def _coords_from_xy(xy: bytes) -> list[tuple[float, float]]:
    """(lat, lon) from the packed x/y pairs, in the order the way stored them."""
    if not xy:
        return []
    buf = _numpy().frombuffer(xy, dtype="i4")
    scale = _LOC_SCALE
    lats = (buf[1::2] / scale).tolist()
    lons = (buf[0::2] / scale).tolist()
    return list(zip(lats, lons))


def _classify_coords(coords: list[tuple[float, float]],
                     south: float, west: float, north: float, east: float) -> str:
    """'hit', 'cover', or 'miss' from coordinates already in hand.

    The same tests as ``_classify_way``. A ring that only contains the box
    is a cover, so a lake larger than the selection is still drawn.
    """
    count = len(coords)
    if count < 2:
        return "miss"
    hit = False
    prev = None
    min_lat = 90.0
    max_lat = -90.0
    min_lon = 180.0
    max_lon = -180.0
    for lat, lon in coords:
        if lat < min_lat:
            min_lat = lat
        if lat > max_lat:
            max_lat = lat
        if lon < min_lon:
            min_lon = lon
        if lon > max_lon:
            max_lon = lon
        if not hit:
            if south <= lat <= north and west <= lon <= east:
                hit = True
            elif prev is not None and not _separated(lat, lon, prev, south, west, north, east):
                if _seg_hits(prev[0], prev[1], lat, lon, south, west, north, east):
                    hit = True
        prev = (lat, lon)
    if hit:
        return "hit"
    closed = count >= 4 and coords[0] == coords[-1]
    if not closed or min_lat > south or max_lat < north or min_lon > west or max_lon < east:
        return "miss"
    if not _pip(coords, (south + north) / 2.0, (west + east) / 2.0):
        return "miss"
    return "cover"


def _emit_way(writer, osm_id: int, tags: dict, coords, status: str) -> None:
    closed = len(coords) >= 4 and coords[0] == coords[-1]
    if closed and _is_area_way(tags, True):
        geometry = {"type": "Polygon", "coordinates": [_lonlat(coords)]}
    elif len(coords) >= 2:
        geometry = {"type": "LineString", "coordinates": _lonlat(coords)}
    else:
        return
    writer.add("way", osm_id, tags, geometry, coords, status == "cover")


def _span_root(root: str) -> str:
    path = os.path.join(_clips_root(root), "spans")
    os.makedirs(path, exist_ok=True)
    return path


def _span_digest(region: Region, source: str) -> str:
    raw = f"{region.id}|{_mtime_ms(source)}|{FILTERS_VERSION}|{_SPAN_VERSION}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _span_dir(root: str, region: Region, source: str) -> str:
    return os.path.join(_span_root(root), _span_digest(region, source))


def _span_ready(folder: str, source: str) -> bool:
    if not os.path.isfile(os.path.join(folder, "complete")):
        return False
    meta = _read_meta(folder)
    if not meta or meta.get("span") != _SPAN_VERSION:
        return False
    if meta.get("filters") != FILTERS_VERSION:
        return False
    try:
        return int(meta.get("mtime_ms") or -1) == _mtime_ms(source)
    except (TypeError, ValueError, OSError):
        return False


def _grid_of(box: tuple[float, float, float, float]):
    south, west, north, east = box
    deg = _SPAN_DEG
    rows = max(1, math.ceil(max(north - south, 1e-6) / deg))
    cols = max(1, math.ceil(max(east - west, 1e-6) / deg))
    return deg, rows, cols


def _cells_touched(bounds, region, deg: float, rows: int, cols: int):
    """Cell coordinates the way's box meets, or None when it is too long
    to copy into each of them."""
    minx, miny, maxx, maxy = bounds
    south, west, _north, _east = region

    def cell(lat: float, lon: float) -> tuple[int, int]:
        r = int((lat - south) / deg) if deg else 0
        c = int((lon - west) / deg) if deg else 0
        if r < 0:
            r = 0
        elif r >= rows:
            r = rows - 1
        if c < 0:
            c = 0
        elif c >= cols:
            c = cols - 1
        return r, c

    r0, c0 = cell(miny / _LOC_SCALE, minx / _LOC_SCALE)
    r1, c1 = cell(maxy / _LOC_SCALE, maxx / _LOC_SCALE)
    if r0 > r1:
        r0, r1 = r1, r0
    if c0 > c1:
        c0, c1 = c1, c0
    if (r1 - r0 + 1) * (c1 - c0 + 1) > _SPAN_WIDE:
        return None
    return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def _query_cells(box, region, deg: float, rows: int, cols: int):
    south, west, north, east = box
    return _cells_touched(
        (int(west * _LOC_SCALE), int(south * _LOC_SCALE),
         int(east * _LOC_SCALE), int(north * _LOC_SCALE)),
        region, deg, rows, cols) or []


class _SpanOut:
    """Ways, places and relations for the whole daily, filed by grid cell.

    Built during the one read that also cuts the current selection. A later
    selection opens the cells it touches.
    """

    def __init__(self, region_box: tuple[float, float, float, float]):
        self.region = region_box
        self.deg, self.rows, self.cols = _grid_of(region_box)
        self.cells: dict[tuple[int, int], bytearray] = {}
        self.wide = bytearray()
        self.relations = bytearray()
        self.trusted = True

    def add_way(self, wid: int, bounds, xy: bytes, tags: dict) -> None:
        if not self.trusted:
            return
        record = _way_record(wid, bounds, xy, tags)
        cells = _cells_touched(bounds, self.region, self.deg, self.rows, self.cols)
        if cells is None:
            self.wide += record
            return
        for key in cells:
            buf = self.cells.get(key)
            if buf is None:
                buf = bytearray()
                self.cells[key] = buf
            buf += record

    def add_node(self, nid: int, x: int, y: int, tags: dict) -> None:
        if not self.trusted:
            return
        record = _node_record(nid, x, y, tags)
        cells = _cells_touched((x, y, x, y), self.region, self.deg, self.rows, self.cols)
        key = cells[0] if cells else (0, 0)
        buf = self.cells.get(key)
        if buf is None:
            buf = bytearray()
            self.cells[key] = buf
        buf += record

    def add_relation(self, rel_id: int, tags: dict, members, water: bool,
                     bounds, water_xy: dict) -> None:
        if not self.trusted:
            return
        self.relations += _relation_record(rel_id, tags, members, water, bounds, water_xy)

    def finish(self, folder: str, source: str) -> None:
        if not self.trusted:
            return
        part = folder + f".part-{os.getpid()}"
        if os.path.isdir(part):
            shutil.rmtree(part, ignore_errors=True)
        os.makedirs(part, exist_ok=True)
        try:
            for (row, col), blob in self.cells.items():
                _write_span(os.path.join(part, f"r{row}c{col}.bin"), blob)
            if self.wide:
                _write_span(os.path.join(part, "wide.bin"), self.wide)
            if self.relations:
                _write_span(os.path.join(part, "relations.bin"), self.relations)
            south, west, north, east = self.region
            with open(os.path.join(part, "meta.json"), "w", encoding="utf-8") as fh:
                json.dump({
                    "span": _SPAN_VERSION,
                    "filters": FILTERS_VERSION,
                    "mtime_ms": _mtime_ms(source),
                    "box": [south, west, north, east],
                    "deg": self.deg,
                    "rows": self.rows,
                    "cols": self.cols,
                }, fh)
            with open(os.path.join(part, "complete"), "w", encoding="utf-8") as fh:
                fh.write("ok\n")
            if os.path.isdir(folder):
                shutil.rmtree(folder, ignore_errors=True)
            os.replace(part, folder)
        except Exception:
            shutil.rmtree(part, ignore_errors=True)
            raise
        finally:
            self.cells.clear()
            self.wide = bytearray()
            self.relations = bytearray()


def _write_span(path: str, blob: bytearray) -> None:
    with open(path, "wb") as fh:
        fh.write(b"KNXS")
        fh.write(blob)


def _way_record(wid: int, bounds, xy: bytes, tags: dict) -> bytes:
    tag_b = json.dumps(tags, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    minx, miny, maxx, maxy = bounds
    return (b"\x01" + _WAY_REST.pack(wid, minx, miny, maxx, maxy, len(xy))
            + xy + struct.pack("<I", len(tag_b)) + tag_b)


def _node_record(nid: int, x: int, y: int, tags: dict) -> bytes:
    tag_b = json.dumps(tags, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return b"\x02" + _NODE_REST.pack(nid, x, y, len(tag_b)) + tag_b


def _relation_record(rel_id: int, tags: dict, members, water: bool,
                     bounds, water_xy: dict) -> bytes:
    tag_b = json.dumps(tags, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if bounds is None:
        minx = miny = maxx = maxy = 0
        known = 0
    else:
        minx, miny, maxx, maxy = bounds
        known = 1
    way_members = [(ref, role, water_xy.get(ref, b"") if water else b"")
                   for kind, ref, role in members if kind == "w"]
    out = bytearray()
    out += b"\x03"
    out += _REL_REST.pack(rel_id, minx, miny, maxx, maxy,
                          (1 if water else 0) | (2 if known else 0), len(tag_b))
    out += tag_b
    out += struct.pack("<I", len(way_members))
    for ref, role, xy in way_members:
        code = 1 if role == "inner" else 0
        out += _MEM_HEAD.pack(code, ref, len(xy))
        if xy:
            out += xy
    return bytes(out)


def _load_span_files(paths, limits, ways: dict, nodes: list, should_stop) -> None:
    """Ways whose box meets ``limits``, and every place, from these cells."""
    seen = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        if not blob.startswith(b"KNXS"):
            continue
        i = 4
        n = len(blob)
        while i < n:
            seen += 1
            if seen % 8192 == 0:
                knoxstop.check(should_stop, "the download")
            kind = blob[i]
            i += 1
            if kind == 1:
                if i + _WAY_REST.size > n:
                    return
                wid, minx, miny, maxx, maxy, xy_len = _WAY_REST.unpack_from(blob, i)
                i += _WAY_REST.size
                xy = blob[i:i + xy_len]
                i += xy_len
                if i + 4 > n:
                    return
                tag_len = struct.unpack_from("<I", blob, i)[0]
                i += 4
                tag_b = blob[i:i + tag_len]
                i += tag_len
                if xy_len and not _span_overlaps(minx, miny, maxx, maxy, limits):
                    continue
                if wid in ways:
                    continue
                try:
                    tags = json.loads(tag_b)
                except ValueError:
                    continue
                if not isinstance(tags, dict):
                    continue
                ways[wid] = (xy, {str(k): str(v) for k, v in tags.items()})
            elif kind == 2:
                if i + _NODE_REST.size > n:
                    return
                nid, x, y, tag_len = _NODE_REST.unpack_from(blob, i)
                i += _NODE_REST.size
                tag_b = blob[i:i + tag_len]
                i += tag_len
                try:
                    tags = json.loads(tag_b)
                except ValueError:
                    continue
                if isinstance(tags, dict):
                    nodes.append((nid, x, y, {str(k): str(v) for k, v in tags.items()}))
            else:
                return


def _load_relations(path: str, limits, should_stop):
    if not os.path.isfile(path):
        return
    with open(path, "rb") as fh:
        blob = fh.read()
    if not blob.startswith(b"KNXS"):
        return
    i = 4
    n = len(blob)
    seen = 0
    while i < n:
        seen += 1
        if seen % 1024 == 0:
            knoxstop.check(should_stop, "the download")
        kind = blob[i]
        i += 1
        if kind != 3 or i + _REL_REST.size > n:
            return
        rel_id, minx, miny, maxx, maxy, flags, tag_len = _REL_REST.unpack_from(blob, i)
        i += _REL_REST.size
        tag_b = blob[i:i + tag_len]
        i += tag_len
        if i + 4 > n:
            return
        count = struct.unpack_from("<I", blob, i)[0]
        i += 4
        water = bool(flags & 1)
        known = bool(flags & 2)
        if water and known and not _span_overlaps(minx, miny, maxx, maxy, limits):
            for _ in range(count):
                if i + _MEM_HEAD.size > n:
                    return
                _code, _ref, xy_len = _MEM_HEAD.unpack_from(blob, i)
                i += _MEM_HEAD.size + xy_len
            continue
        try:
            tags = json.loads(tag_b)
        except ValueError:
            tags = None
        members = []
        embedded = {}
        for _ in range(count):
            if i + _MEM_HEAD.size > n:
                return
            code, ref, xy_len = _MEM_HEAD.unpack_from(blob, i)
            i += _MEM_HEAD.size
            xy = blob[i:i + xy_len]
            i += xy_len
            role = "inner" if code == 1 else "outer"
            members.append(("w", int(ref), role))
            if xy:
                embedded[int(ref)] = xy
        if not isinstance(tags, dict):
            continue
        yield (int(rel_id), {str(k): str(v) for k, v in tags.items()},
               members, water, embedded)


def _clip_from_span(root: str, region: Region, source: str,
                    box: tuple[float, float, float, float],
                    should_stop) -> str:
    """Cut ``box`` from the regional grid. The daily is not opened."""
    folder = _span_dir(root, region, source)
    meta = _read_meta(folder) or {}
    try:
        region_box = tuple(float(v) for v in meta["box"])
        deg = float(meta.get("deg") or _SPAN_DEG)
        rows = int(meta.get("rows") or 1)
        cols = int(meta.get("cols") or 1)
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("The saved area index for this daily could not be read.")
    dest = _clip_dir(root, region, source, box)
    partial = dest + ".part"
    if os.path.isdir(partial):
        shutil.rmtree(partial, ignore_errors=True)
    os.makedirs(partial, exist_ok=True)
    south, west, north, east = box
    limits = _envelope_limits(*box)
    rows_out, cols_out = _grid_shape(box)
    writer = _ClipWriter(partial, box, rows_out, cols_out)
    try:
        knoxstop.check(should_stop, "the download")
        paths = [os.path.join(folder, "wide.bin")]
        # A selection can sit on a grid line. The cells the box touches are
        # enough; a way that spans more than a handful of cells is in wide.bin.
        touched = _cells_touched(
            (int(west * _LOC_SCALE) - 2, int(south * _LOC_SCALE) - 2,
             int(east * _LOC_SCALE) + 2, int(north * _LOC_SCALE) + 2),
            region_box, deg, rows, cols)
        for row, col in touched or []:
            paths.append(os.path.join(folder, f"r{row}c{col}.bin"))
        # _cells_touched returns None for a huge box. Read every cell then.
        if touched is None:
            for name in os.listdir(folder):
                if name.startswith("r") and name.endswith(".bin"):
                    paths.append(os.path.join(folder, name))
        ways: dict[int, tuple[bytes, dict]] = {}
        nodes: list = []
        node_rules = _rule_table(_rules())
        _load_span_files(paths, limits, ways, nodes, should_stop)
        stored: dict[int, list] = {}
        for wid, (xy, tags) in ways.items():
            coords = _coords_from_xy(xy)
            stored[wid] = coords
            status = _classify_coords(coords, south, west, north, east)
            if status == "miss":
                continue
            _emit_way(writer, wid, tags, coords, status)
        for nid, x, y, tags in nodes:
            lat = y / _LOC_SCALE
            lon = x / _LOC_SCALE
            if not (south <= lat <= north and west <= lon <= east):
                continue
            if not _matches("n", tags, node_rules):
                continue
            writer.add("node", nid, tags,
                       {"type": "Point", "coordinates": [lon, lat]},
                       [(lat, lon)], False)
        for rel_id, tags, members, water, embedded in _load_relations(
                os.path.join(folder, "relations.bin"), limits, should_stop):
            if not water and not any(
                    kind == "w" and ref in stored for kind, ref, _role in members):
                continue
            if water:
                rel_stored = {ref: _coords_from_xy(xy) for ref, xy in embedded.items()}
            else:
                rel_stored = stored
            way_members = sum(kind == "w" for kind, _ref, _role in members)
            made = _relation_geometry_plain(
                tags, members, rel_stored, box, water, way_members)
            if made is None:
                continue
            geometry, flat, cover = made
            writer.add("relation", rel_id, tags, geometry, flat, cover)
        writer.finish()
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(partial, dest)
    except Exception:
        writer.abort()
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return dest


class _WayGate:
    """Drop ways inside the file reader, before the clip loop sees them.

    A pyosmium handler filter returns True to drop the object and False to
    keep it. Paint tags are tested before any node is touched. Water shores
    are already listed (the other thread finished during the node index, or
    this waits once, on the first way). Each remaining way is one bounding
    box from the packed coordinates. A miss is dropped on that box. A hit
    keeps those coordinates, so the clip loop does not walk the nodes again.
    """

    def __init__(self, table, box, stopper: _Stop, ids_event, ids_box, span):
        self.ways = table.get("w", {})
        self.south, self.west, self.north, self.east = box
        self.limits = _envelope_limits(*box)
        self.stopper = stopper
        self._ids_event = ids_event
        self._ids_box = ids_box
        self.needed: set[int] = set()
        self._ids_done = False
        self.span = span
        self.kept: dict[int, tuple[list, str]] = {}
        self.water_coords: dict[int, list] = {}
        self.water_xy: dict[int, bytes] = {}
        self.water_bounds: dict[int, tuple] = {}
        self.trusted = True
        self._pulled = False
        self._factory = None
        self._use_all = None

    def _abandon(self) -> None:
        self.trusted = False
        if self.span is not None:
            self.span.trusted = False

    def _ensure_ids(self) -> None:
        if self._ids_done:
            return
        self._ids_event.wait()
        self._ids_done = True
        err = self._ids_box.get("error")
        if err is not None:
            raise err
        self.needed = self._ids_box.get("ids") or set()

    def pull_water(self, stored: dict) -> None:
        """Shores collected on the way pass, still in memory for relations."""
        if self._pulled:
            return
        self._ensure_ids()
        stored.update(self.water_coords)
        self._pulled = True

    def way(self, way) -> bool:
        self.stopper.tick()
        self._ensure_ids()
        wid = int(way.id)
        needed = wid in self.needed
        paint = _way_paint(way.tags, self.ways)
        # A closed ring that is not painted and not a water shore was read
        # and then thrown away. Drop it before the node array is touched.
        if not paint and not needed:
            return True
        nodes = way.nodes
        count = len(nodes)
        if count < 2:
            return True
        learned = _learn_span(nodes, count)
        if learned == "invalid":
            return True
        if learned != "ready":
            # A real way could not be packed. The grid would be missing it.
            self._abandon()
            return self._slow(way, needed)
        measured = _measure_packed(nodes, count, _span_layout)
        if measured is False:
            return True
        if measured is None:
            self._abandon()
            return self._slow(way, needed)
        minx, miny, maxx, maxy, xy = measured
        bounds = (minx, miny, maxx, maxy)
        if needed:
            # Keep the packed pairs. The coordinate list is built later, and
            # only for a water relation whose box meets this selection.
            self.water_bounds[wid] = bounds
            self.water_xy[wid] = xy
        tags = _tag_dict(way.tags) if paint else None
        if paint and self.span is not None and self.trusted and tags is not None:
            self.span.add_way(wid, bounds, xy, tags)
        if not _span_overlaps(minx, miny, maxx, maxy, self.limits):
            return True
        if not paint or tags is None:
            return True
        coords = self.water_coords.get(wid) or _coords_from_xy(xy)
        status = _classify_coords(coords, self.south, self.west, self.north, self.east)
        if status == "miss":
            return True
        self.kept[wid] = (coords, status, tags)
        return False

    def _slow(self, way, needed: bool) -> bool:
        """Packed coordinates could not be read. The old envelope test."""
        if needed:
            return False
        if self._factory is None:
            lib = _require()
            self._factory = lib.geom.WKBFactory()
            self._use_all = lib.geom.ALL
        if not _way_paint(way.tags, self.ways) and not _closed_ring(way):
            return True
        return _envelope_misses(
            way, self._factory, self._use_all,
            self.south, self.west, self.north, self.east, self.limits)


def _water_way_ids(source: str, table, should_stop, threads: int) -> set[int]:
    """Way ids of natural=water multipolygons in this file.

    Relations are stored after ways. A harbour outline is a chain of open
    ways, often with no paint tag of their own (Sydney Harbour's outers are
    administrative boundaries reused as the shore). This walks relations
    only, on its own thread, while the main read fills the node index.
    Only water areas are collected: taking every forest relation in a
    country file would walk the edge of every wood.
    """
    lib = _require()
    needed: set[int] = set()
    stopper = _Stop(should_stop)
    pool = lib.io.ThreadPool(max(1, threads))
    proc = lib.FileProcessor(source, lib.osm.RELATION, thread_pool=pool)
    try:
        for obj in proc:
            stopper.tick()
            if not _matches("r", obj.tags, table):
                continue
            rel_type = obj.tags["type"] if "type" in obj.tags else ""
            if rel_type not in ("multipolygon", "boundary"):
                continue
            if obj.tags.get("natural") != "water":
                continue
            for member in obj.members:
                if _member_kind(member) == "w":
                    needed.add(int(member.ref))
    finally:
        proc = None
        pool = None
    return needed


def _union_bounds(ids, boxes):
    """Box around every member, or None when one member was not measured.

    A partial box would be stored as if it were the whole lake, and a later
    selection inside the missing part would skip the harbour.
    """
    if not ids:
        return None
    minx = miny = 2147483647
    maxx = maxy = -2147483648
    for ident in ids:
        bounds = boxes.get(ident)
        if not bounds:
            return None
        x0, y0, x1, y1 = bounds
        if x0 < minx:
            minx = x0
        if y0 < miny:
            miny = y0
        if x1 > maxx:
            maxx = x1
        if y1 > maxy:
            maxy = y1
    return minx, miny, maxx, maxy


def _build_clip(root: str, region: Region, source: str,
                box: tuple[float, float, float, float],
                should_stop, threads: int) -> str:
    """Tagged nodes and the ways that meet the box, in one read.

    Untagged nodes never enter Python: the key filter drops them after the
    C++ location index has recorded their coordinates. Water-shore ids are
    collected on another thread while that index fills, and the way gate
    waits for them once. A way that is not painted and not a shore is dropped
    before its nodes are read. Anything else is one bounding box from the
    packed coordinates; a miss stops there. Relations come last, and a
    harbour is assembled from the shore coordinates still in memory.
    """
    dest = _clip_dir(root, region, source, box)
    if os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    partial = dest + ".part"
    if os.path.isdir(partial):
        shutil.rmtree(partial, ignore_errors=True)
    os.makedirs(partial, exist_ok=True)
    south, west, north, east = box
    rows, cols = _grid_shape(box)
    lib = _require()
    rules = _rules()
    table = _rule_table(rules)
    keys = sorted({key for _kind, key, _value in rules})
    # Nodes and relations must carry one of the keys KnoxMap paints. Ways are
    # not in this filter: KeyFilter is a conjunction, and an untagged ring
    # would never reach the multipolygon that uses it. A C++ drop in front of
    # the way callback would also hide those ways from _Stop. _WayGate does
    # the tag-or-ring test and the envelope, and ticks every way. Nodes are
    # not given a Python callback; the location index stays in C++.
    keep = lib.filter.KeyFilter(*keys).enable_for(lib.osm.NODE | lib.osm.RELATION)
    writer = _ClipWriter(partial, box, rows, cols)
    stopper = _Stop(should_stop)
    stored: dict[int, list[tuple[float, float]]] = {}
    proc = None
    pool = None
    gate = None
    span = _SpanOut(region.bbox)
    span_folder = _span_dir(root, region, source)
    # The shore list and the node index both read the daily. Running them
    # together means the way pass does not wait out a finished first walk.
    water_threads = max(1, threads // 4)
    main_threads = max(1, threads - water_threads)
    ids_box: dict = {"ids": set(), "error": None}
    ids_event = threading.Event()

    def _shores():
        try:
            ids_box["ids"] = _water_way_ids(source, table, should_stop, water_threads)
        except Exception as exc:
            ids_box["error"] = exc
        finally:
            ids_event.set()

    threading.Thread(target=_shores, name="knox-water", daemon=True).start()
    try:
        knoxstop.check(should_stop, "the download")
        pool = lib.io.ThreadPool(max(1, main_threads))
        storage = _location_storage(source, partial)
        gate = _WayGate(table, box, stopper, ids_event, ids_box, span)
        proc = (lib.FileProcessor(source, thread_pool=pool)
                .with_locations(storage)
                .with_filter(keep)
                .with_filter(gate))
        for obj in proc:
            stopper.tick()
            kind = obj.type_str()
            if kind == "n":
                if not _matches("n", obj.tags, table):
                    continue
                tags = _tag_dict(obj.tags)
                loc = obj.location
                try:
                    nx = int(loc.x)
                    ny = int(loc.y)
                except Exception:
                    nx = ny = _INVALID_X
                if span.trusted and nx != _INVALID_X and ny != _INVALID_X:
                    span.add_node(int(obj.id), nx, ny, tags)
                if not _in_box(loc, south, west, north, east):
                    continue
                lat = loc.lat
                lon = loc.lon
                writer.add(
                    "node", int(obj.id), tags,
                    {"type": "Point", "coordinates": [lon, lat]},
                    [(lat, lon)], False)
                continue
            if kind == "w":
                osm_id = int(obj.id)
                kept = gate.kept.get(osm_id)
                if kept is None:
                    # Packed coordinates were unreadable for this way. The
                    # gate left it for the same test the clip used to run.
                    if osm_id in gate.needed and osm_id not in gate.water_coords:
                        coords = _way_coords(obj)
                        if coords is not None:
                            stored[osm_id] = coords
                    if not _matches("w", obj.tags, table):
                        continue
                    status, coords = _classify_way(obj, south, west, north, east)
                    if coords is None:
                        continue
                    stored[osm_id] = coords
                    _emit_way(writer, osm_id, _tag_dict(obj.tags), coords, status)
                    continue
                coords, status, tags = kept
                stored[osm_id] = coords
                _emit_way(writer, osm_id, tags, coords, status)
                continue
            if kind != "r":
                continue
            gate.pull_water(stored)
            if not _matches("r", obj.tags, table):
                continue
            rel_type = obj.tags["type"] if "type" in obj.tags else ""
            if rel_type not in ("multipolygon", "boundary"):
                continue
            members = _plain_members(obj)
            water = obj.tags.get("natural") == "water"
            way_refs = [ref for kind_m, ref, _role in members if kind_m == "w"]
            bounds = _union_bounds(way_refs, gate.water_bounds) if water else None
            if span.trusted:
                span.add_relation(int(obj.id), _tag_dict(obj.tags), members,
                                  water, bounds, gate.water_xy)
            # A lake in another part of the state never becomes a coordinate
            # list. Its shores stay packed until a selection meets the lake.
            if water and bounds is not None and not _span_overlaps(
                    bounds[0], bounds[1], bounds[2], bounds[3], gate.limits):
                continue
            if water:
                for ref in way_refs:
                    if ref in stored:
                        continue
                    xy = gate.water_xy.get(ref)
                    if xy:
                        stored[ref] = _coords_from_xy(xy)
            made = _relation_geometry_plain(
                obj.tags, members, stored, box, water,
                sum(kind_m == "w" for kind_m, _ref, _role in members))
            if made is None:
                continue
            geometry, flat, cover = made
            writer.add("relation", int(obj.id), _tag_dict(obj.tags), geometry, flat, cover)
        if gate is not None:
            gate.pull_water(stored)
            gate.water_xy.clear()
            gate.water_bounds.clear()
            gate.water_coords.clear()
        stored.clear()
        del proc
        del pool
        proc = None
        pool = None
        store = os.path.join(partial, "nodes.store")
        if os.path.isfile(store):
            try:
                os.remove(store)
            except OSError:
                pass
        writer.finish()
        os.replace(partial, dest)
    except Exception:
        writer.abort()
        # Drop the reader before removing its scratch file, or Windows keeps
        # the directory locked until this function returns.
        proc = None
        pool = None
        shutil.rmtree(partial, ignore_errors=True)
        raise
    finally:
        ids_event.wait()
    if span.trusted and (gate is None or gate.trusted):
        try:
            span.finish(span_folder, source)
        except Exception:
            span.trusted = False
    return dest


def _ensure_clip(root: str, region: Region, source: str,
                 box: tuple[float, float, float, float], should_stop,
                 progress, index: int, total: int, threads: int) -> str:
    folder = _clip_dir(root, region, source, box)
    if _clip_ready(folder, box):
        _remember(root, region, source, box, folder)
        return folder
    span_folder = _span_dir(root, region, source)
    if _span_ready(span_folder, source):
        _report(progress, region.name, index, total, "filter")
        try:
            folder = _clip_from_span(root, region, source, box, should_stop)
        except knoxstop.Stopped:
            raise
        except Exception:
            shutil.rmtree(span_folder, ignore_errors=True)
        else:
            _remember(root, region, source, box, folder)
            return folder
    _report(progress, region.name, index, total, "filter")

    def on_bytes(done, total_bytes, speed, name=region.name, index=index, total=total):
        _report(progress, name, index, total, "filter", (done, total_bytes, speed))

    meter = _ScanMeter(source, on_bytes if progress else None)
    meter.start()
    try:
        folder = _build_clip(root, region, source, box, should_stop, threads)
    except Exception:
        meter.stop()
        raise
    meter.stop(complete=True)
    _remember(root, region, source, box, folder)
    return folder


def _cells_for_query(meta: dict, box: tuple[float, float, float, float]):
    south, west, north, east = (float(v) for v in meta["box"])
    rows = int(meta.get("rows") or 1)
    cols = int(meta.get("cols") or 1)
    qs, qw, qn, qe = box

    def idx(value: float, lo: float, hi: float, n: int) -> int:
        if n <= 1 or hi <= lo:
            return 0
        i = int((value - lo) / (hi - lo) * n)
        if i < 0:
            return 0
        if i >= n:
            return n - 1
        return i

    r0 = idx(qs, south, north, rows)
    r1 = idx(qn, south, north, rows)
    c0 = idx(qw, west, east, cols)
    c1 = idx(qe, west, east, cols)
    for row in range(min(r0, r1), max(r0, r1) + 1):
        for col in range(min(c0, c1), max(c0, c1) + 1):
            yield row, col


def _iter_clip(folder: str, box: tuple[float, float, float, float], highways_only: bool):
    meta = _read_meta(folder) or {}
    try:
        clip_box = tuple(float(v) for v in meta["box"])
    except (KeyError, TypeError, ValueError):
        return
    whole = not highways_only and _same_box(clip_box, box)
    paths = []
    cover = os.path.join(folder, "cover.jsonl")
    if os.path.isfile(cover):
        paths.append(cover)
    if whole or "rows" not in meta:
        for name in os.listdir(folder):
            if name.endswith(".jsonl") and name != "cover.jsonl":
                paths.append(os.path.join(folder, name))
    else:
        for row, col in _cells_for_query(meta, box):
            path = os.path.join(folder, f"r{row}c{col}.jsonl")
            if os.path.isfile(path):
                paths.append(path)
    merged: dict[tuple[str, int], OSMFeature] = {}
    south, west, north, east = box
    for path in paths:
        for feature in _read_geojsonseq(path):
            if highways_only and "highway" not in feature.tags:
                continue
            if not whole and not _feature_hits(feature, south, west, north, east):
                continue
            merged[(feature.kind, feature.osm_id)] = feature
    # Cached features stay as read. A later pass replaces geometry on the
    # objects it was given, so each read hands out its own copies.
    for feature in merged.values():
        yield _fresh(feature)


def ensure_regions(root: str, south: float, west: float, north: float, east: float,
                   progress=None, should_stop=None) -> list[Region]:
    """Latest dailies for the smallest regions covering this box, then one cut.

    progress(name, index, count, step, download) is called with step "check"
    as each region starts, "download" with download = (bytes done, bytes
    total, bytes a second) while its file comes down, and "filter" while the
    roads and buildings inside the box are picked out of a daily that is new
    or not yet cut to this box. During that pick, download is how far through
    the daily the reader has got, in the same shape, so the page can show a
    percent and a time left. A later map inside the same cached area reuses
    that cut.
    """
    knoxstop.check(should_stop, "the download")
    _require()
    regions = cover(load_index(root), south, west, north, east)
    box = (south, west, north, east)
    sources = []
    total = len(regions)
    for index, region in enumerate(regions, start=1):
        knoxstop.check(should_stop, "the download")
        _report(progress, region.name, index, total, "check")

        def on_bytes(done, total_bytes, speed, name=region.name, index=index, total=total):
            _report(progress, name, index, total, "download", (done, total_bytes, speed))

        sources.append(ensure_pbf(root, region, progress=on_bytes if progress else None))

    pending = []
    for index, (region, source) in enumerate(zip(regions, sources), start=1):
        # A previous build may have cut a larger area from this same daily.
        # Reuse it just as features_for_bbox does; checking only the exact
        # digest here needlessly walked the full regional PBF again whenever
        # the next selection had different coordinates.
        if _find_clip(root, region, source, box) is not None:
            continue
        pending.append((index, region, source))
    if not pending:
        return regions

    jobs, threads = _walk_plan([source for _index, _region, source in pending])
    halt = _Halt(should_stop)

    def one(index, region, source):
        _ensure_clip(root, region, source, box, halt, progress, index, total, threads)

    if jobs == 1:
        for index, region, source in pending:
            one(index, region, source)
        return regions

    error = None
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futs = [pool.submit(one, index, region, source)
                for index, region, source in pending]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                halt.failed = True
                if error is None:
                    error = exc
    if error is not None:
        raise error
    return regions


def _is_area_way(tags: dict, closed: bool) -> bool:
    """A closed building is a footprint. A closed roundabout is still a road.

    area=yes / area=no wins. Otherwise a highway, railway, barrier, coastline
    or a linear waterway stays a line, and every other closed way is filled.
    """
    if not closed or not tags:
        return False
    area = tags.get("area")
    if area == "yes":
        return True
    if area == "no":
        return False
    # A station, platform or yard is an area even though railway=* is
    # otherwise a centre line. The rails themselves stay lines.
    if tags.get("railway") in RAIL_GROUNDS:
        return True
    if tags.get("natural") == "coastline":
        return False
    if any(key in tags for key in _LINE_KEYS):
        return False
    if tags.get("man_made") in ("pier", "breakwater", "groyne"):
        return False
    waterway = tags.get("waterway")
    if waterway in _LINE_WATERWAYS:
        return False
    return True


def features_for_bbox(root: str, regions: list[Region],
                      south: float, west: float, north: float, east: float,
                      highways_only: bool = False,
                      should_stop=None) -> list[OSMFeature]:
    """Features inside this box, from the cut made for the regions' daily.

    The cut is the box ensure_regions walked. A smaller piece, or highways
    only, is read back out of that cut and does not open the regional file.
    """
    box = (south, west, north, east)
    merged: dict[tuple[str, int], OSMFeature] = {}
    for region in regions:
        knoxstop.check(should_stop, "the download")
        source = _local_pbf(root, region)
        folder = _find_clip(root, region, source, box)
        if folder is None:
            folder = _ensure_clip(
                root, region, source, box, should_stop, None, 1, 1,
                os.cpu_count() or 1)
        for feature in _iter_clip(folder, box, highways_only):
            merged[(feature.kind, feature.osm_id)] = feature
    return list(merged.values())


# One cut is read for the road angle and again for every map piece. Parsing
# each geojsonseq line was most of that time, so a file's features are kept
# while its size and timestamp are unchanged. Only the last few cuts stay.
_SEQ_CACHE: dict[str, tuple[int, int, list]] = {}
_SEQ_FOLDERS: list[str] = []
_SEQ_KEEP = 4
_SEQ_WAIT: dict[str, threading.Event] = {}
_READ_LOCK = threading.Lock()


def _seq_store(path: str, mtime_ns: int, size: int, features: list) -> None:
    folder = os.path.dirname(path)
    try:
        _SEQ_FOLDERS.remove(folder)
    except ValueError:
        pass
    _SEQ_FOLDERS.append(folder)
    while len(_SEQ_FOLDERS) > _SEQ_KEEP:
        old = _SEQ_FOLDERS.pop(0)
        prefix = old + os.sep
        for key in [key for key in _SEQ_CACHE if key.startswith(prefix)]:
            _SEQ_CACHE.pop(key, None)
    _SEQ_CACHE[path] = (mtime_ns, size, features)


def _fresh(feature: OSMFeature) -> OSMFeature:
    """Copy the caller may change. The cached feature stays as read."""
    tags = dict(feature.tags)
    roles = feature.role_geoms
    if roles:
        roles = [(role, list(ring)) for role, ring in roles]
        geom = [ring for _role, ring in roles]
        return OSMFeature(feature.osm_id, feature.kind, tags, geom, roles)
    return OSMFeature(feature.osm_id, feature.kind, tags, list(feature.geometry))


def _rings(coords) -> list[tuple[float, float]]:
    """(lat, lon) points. A bad position is dropped, same as a type check."""
    if not coords:
        return []
    fl = float
    try:
        count = len(coords)
        out = [(fl(pair[1]), fl(pair[0])) for pair in coords
               if type(pair) is list or type(pair) is tuple]
        if len(out) != count:
            raise TypeError
        return out
    except (TypeError, ValueError, IndexError, KeyError):
        out = []
        for pair in coords:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2 and not isinstance(pair[0], (list, tuple)):
                out.append((fl(pair[1]), fl(pair[0])))
        return out


def _parse_geojsonseq(path: str) -> list[OSMFeature]:
    loads = json.loads
    to_feature = _to_feature
    features: list[OSMFeature] = []
    add = features.append
    next_id = -1
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")
    del text
    for line in lines:
        line = line.lstrip("\x1e").strip()
        if not line or line.startswith("{") and '"FeatureCollection"' in line[:40]:
            continue
        try:
            obj = loads(line)
        except ValueError:
            continue
        feature = to_feature(obj, next_id)
        if feature is None:
            continue
        if feature.osm_id < 0:
            next_id -= 1
        add(feature)
    return features


def _read_geojsonseq(path: str) -> list[OSMFeature]:
    """Features in this file, in order. Reused while the file is unchanged.

    Several map pieces read the same cut at once. One of them parses it;
    the others wait for that copy instead of each reading the file again.
    """
    try:
        st = os.stat(path)
    except OSError:
        return _parse_geojsonseq(path)
    token = (st.st_mtime_ns, st.st_size)
    while True:
        with _READ_LOCK:
            hit = _SEQ_CACHE.get(path)
            if hit is not None and (hit[0], hit[1]) == token:
                return hit[2]
            slot = _SEQ_WAIT.get(path)
            if slot is None:
                slot = threading.Event()
                _SEQ_WAIT[path] = slot
                owner = True
            else:
                owner = False
        if not owner:
            slot.wait()
            continue
        try:
            features = _parse_geojsonseq(path)
            try:
                st = os.stat(path)
            except OSError:
                return features
            if (st.st_mtime_ns, st.st_size) != token:
                return features
            with _READ_LOCK:
                _seq_store(path, token[0], token[1], features)
            return features
        finally:
            with _READ_LOCK:
                _SEQ_WAIT.pop(path, None)
            slot.set()


def _to_feature(obj: dict, fallback_id: int) -> OSMFeature | None:
    props = obj.get("properties") or {}
    tags = {}
    for key, value in props.items():
        if type(key) is not str:
            key = str(key)
        if key.startswith("@"):
            continue
        if type(value) is not str:
            value = str(value)
        tags[key] = value
    raw_id = props.get("@id", props.get("id"))
    if type(raw_id) is int:
        osm_id = raw_id
    else:
        if isinstance(raw_id, str) and "/" in raw_id:
            raw_id = raw_id.rsplit("/", 1)[-1]
        try:
            osm_id = int(raw_id)
        except (TypeError, ValueError):
            osm_id = fallback_id
    raw_kind = props.get("@type")
    if type(raw_kind) is str:
        kind = raw_kind.lower()
    else:
        kind = str(raw_kind or "way").lower()
    if kind not in ("node", "way", "relation"):
        kind = "way"
    geom = obj.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        ring = _rings((coords,))
        return OSMFeature(osm_id, "node", tags, ring) if ring else None
    if gtype == "LineString":
        ring = _rings(coords)
        return OSMFeature(osm_id, kind if kind != "node" else "way", tags, ring) if len(ring) >= 2 else None
    if gtype == "Polygon":
        return _polygon(osm_id, kind, tags, coords)
    if gtype == "MultiPolygon":
        roles = []
        for polygon in coords or []:
            made = _polygon(osm_id, kind, tags, polygon)
            if made is not None:
                roles.extend(made.role_geoms or [("outer", made.geometry)])
        if not roles:
            return None
        return OSMFeature(osm_id, "relation", tags, [ring for _role, ring in roles], roles)
    return None


def _polygon(osm_id: int, kind: str, tags: dict, coords) -> OSMFeature | None:
    if not coords:
        return None
    outer = _rings(coords[0])
    if len(outer) < 4:
        return None
    roles = [("outer", outer)]
    for hole in coords[1:]:
        ring = _rings(hole)
        if len(ring) >= 4:
            roles.append(("inner", ring))
    if kind == "relation" or len(roles) > 1:
        return OSMFeature(osm_id, "relation", tags, [ring for _role, ring in roles], roles)
    return OSMFeature(osm_id, "way", tags, outer)
