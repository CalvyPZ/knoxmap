"""Map features from local Geofabrik dailies, one box at a time.

The regional PBF stays on disk. Water multipolygons are read first, because
those relations sit at the end of the file and a harbour such as Sydney
Harbour is hundreds of open shore ways, not a closed ring. The main read
then keeps node coordinates in a C++ index and drops untagged nodes before
they reach Python. A way KnoxMap would not paint, and that is not one of
those shores or a closed ring a multipolygon can use, is dropped before its
nodes are read. What remains is dropped when its node envelope misses the
box. A short way is decided from the coordinates packed on its nodes, and
a long way whose ends already meet the box is kept without a linestring;
only a long way still asks libosmium for that hex envelope.

Only features that meet the box are written, in cells so a later piece of
the same map does not read the regional file again. Complete water outlines
are held while reading but discarded unless their finished area meets the
box. The reader decompresses the file on a pool of threads.
pyosmium is a library in the program file, the same way Flask and Pillow are:
generating a map does not shell out to a second install.
"""
from __future__ import annotations

import array
import ctypes
import hashlib
import json
import os
import shutil
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
                pos = self._seen
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


def _win_apis():
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
    return kernel32, ntdll


def _read_pos_windows(path: str, meter: _ScanMeter) -> int | None:
    """Current byte offset of the daily, queried and not seeked.

    A second handle at offset 0 (a scanner, this process opening the same
    path) is ignored once the reader has moved. Until then every match is
    rechecked, so the cached handle is the one that is actually reading.
    """
    if meter._win_handle is not None:
        pos = _win_query(meter._win_handle, path)
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


def _win_query(handle_value: int, path: str) -> int | None:
    """Position of this handle when it is the daily. None for any other handle."""
    import ctypes
    from ctypes import wintypes

    kernel32, ntdll = _win_apis()
    current = kernel32.GetCurrentProcess()
    dup = wintypes.HANDLE()
    if not kernel32.DuplicateHandle(
            current, wintypes.HANDLE(handle_value), current,
            ctypes.byref(dup), 0, False, 0x2):
        return None
    try:
        if kernel32.GetFileType(dup) != 1:  # FILE_TYPE_DISK
            return None
        wide = ctypes.create_unicode_buffer(4096)
        nchars = kernel32.GetFinalPathNameByHandleW(dup, wide, len(wide), 0)
        if not nchars or nchars >= len(wide):
            return None
        if not _same_file(wide.value, path):
            return None

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


def _member_lines(obj, stored: dict) -> list[tuple[str, list[tuple[float, float]]]]:
    """Member ways this walk kept, open or closed, with outer/inner roles."""
    lines = []
    for member in obj.members:
        if _member_kind(member) != "w":
            continue
        coords = stored.get(int(member.ref))
        if not coords or len(coords) < 2:
            continue
        role = "inner" if str(member.role or "").strip().lower() == "inner" else "outer"
        lines.append((role, coords))
    return lines


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


def _relation_geometry(obj, stored: dict, box: tuple[float, float, float, float]):
    """One polygon for a multipolygon, including a harbour of open shores.

    Closed member ways are joined as before. Open ways are stitched end to
    end, which is how Port Jackson is mapped: relation 15522136 is
    natural=water, and its outer is 484 shoreline ways, most of them with no
    tags, rather than one ring. Water is painted only from a completed ring;
    member-way direction is not a safe indication of which side is water.
    """
    south, west, north, east = box
    lines = _member_lines(obj, stored)
    if not any(role != "inner" for role, _ring in lines):
        return None
    if obj.tags.get("natural") == "water":
        expected = sum(_member_kind(member) == "w" for member in obj.members)
        if len(lines) != expected:
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


def _learn_span(nodes, count: int) -> None:
    global _span_layout, _span_tries
    if _span_layout is not None:
        return
    known = _span_sample(nodes, count)
    if known is None:
        return
    with _span_lock:
        if _span_layout is not None:
            return
        try:
            found = _probe_span(nodes._list, known)
        except (MemoryError, knoxstop.Stopped):
            raise
        except Exception:
            found = None
        if found:
            _span_layout = found
            return
        if found is False:
            _span_layout = False
            return
        _span_tries += 1
        if _span_tries >= _SPAN_TRIES:
            _span_layout = False


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
    after every node has been seen.
    """
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


class _WayGate:
    """Drop ways inside the file reader, before the clip loop sees them.

    A pyosmium handler filter returns True to drop the object and False to
    keep it. Chained ``KeyFilter`` objects are a conjunction, so a way-level
    key filter would also drop the untagged rings a multipolygon is drawn
    from, and it would drop them before this callback could see a stop.
    This gate is the superset the writer needs: a paint tag ``_way_paint``
    accepts (the same rules as ``_matches``), a closed ring
    ``_relation_geometry`` can attach, or an open shore of a water
    multipolygon collected in ``needed``.
    Every needed water member is kept so its relation can form a complete
    ring. Anything else is dropped with no node walk unless its envelope
    meets the box: the packed coordinates of a short way, the linestring
    for a long one.
    Every way is ticked, same as the old scan. There is no callback per node.
    """

    def __init__(self, table, factory, use_all, box, stopper: _Stop,
                 needed: set[int]):
        self.ways = table.get("w", {})
        self.factory = factory
        self.use_all = use_all
        self.south, self.west, self.north, self.east = box
        self.limits = _envelope_limits(*box)
        self.stopper = stopper
        self.needed = needed

    def way(self, way) -> bool:
        self.stopper.tick()
        if int(way.id) in self.needed:
            return False
        if not _way_paint(way.tags, self.ways) and not _closed_ring(way):
            return True
        return _envelope_misses(
            way, self.factory, self.use_all,
            self.south, self.west, self.north, self.east, self.limits)


def _water_way_ids(source: str, table, should_stop, threads: int) -> set[int]:
    """Way ids of natural=water multipolygons in this file.

    Relations are stored after ways. A harbour outline is a chain of open
    ways, often with no paint tag of their own (Sydney Harbour's outers are
    administrative boundaries reused as the shore). The way pass would drop
    them, and the relation would then have nothing to draw. The ids are
    collected first so every shore in a relevant relation can form a closed
    ring. Completed relations that miss the box are discarded. Only water
    areas are collected: taking every forest relation in a country file
    would walk the edge of every wood.
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


def _build_clip(root: str, region: Region, source: str,
                box: tuple[float, float, float, float],
                should_stop, threads: int) -> str:
    """Tagged nodes and the ways that can fall in the box, after water shores
    have been listed.

    Untagged nodes never enter Python: the key filter drops them after the
    C++ location index has recorded their coordinates, which is what a way
    needs in order to know whether it meets the box. Water multipolygon
    members are collected before this read, so every open harbour shore is
    kept until its complete relation is assembled. Anything else is dropped
    in the way gate before a node list is walked unless it meets the box. A
    short way that remains is tested from the coordinates packed on its
    nodes; a long one still uses the linestring envelope. The reader's thread
    pool decompresses the file ahead of that test. Relations come last in the
    file, so a multipolygon is assembled from the ways this same pass kept.
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
    try:
        knoxstop.check(should_stop, "the download")
        needed = _water_way_ids(source, table, should_stop, threads)
        pool = lib.io.ThreadPool(max(1, threads))
        storage = _location_storage(source, partial)
        gate = _WayGate(table, lib.geom.WKBFactory(), lib.geom.ALL, box, stopper, needed)
        proc = (lib.FileProcessor(source, thread_pool=pool)
                .with_locations(storage)
                .with_filter(keep)
                .with_filter(gate))
        for obj in proc:
            stopper.tick()
            kind = obj.type_str()
            if kind == "n":
                if not _in_box(obj.location, south, west, north, east):
                    continue
                if not _matches("n", obj.tags, table):
                    continue
                tags = _tag_dict(obj.tags)
                lat = obj.location.lat
                lon = obj.location.lon
                writer.add(
                    "node", int(obj.id), tags,
                    {"type": "Point", "coordinates": [lon, lat]},
                    [(lat, lon)], False)
                continue
            if kind == "w":
                osm_id = int(obj.id)
                if osm_id in needed:
                    coords = _way_coords(obj)
                    if coords is not None:
                        stored[osm_id] = coords
                if not _matches("w", obj.tags, table):
                    continue
                status, coords = _classify_way(obj, south, west, north, east)
                if coords is None:
                    continue
                stored[osm_id] = coords
                tags = _tag_dict(obj.tags)
                closed = len(coords) >= 4 and coords[0] == coords[-1]
                if closed and _is_area_way(tags, True):
                    geometry = {"type": "Polygon", "coordinates": [_lonlat(coords)]}
                elif len(coords) >= 2:
                    geometry = {"type": "LineString", "coordinates": _lonlat(coords)}
                else:
                    continue
                writer.add("way", int(obj.id), tags, geometry, coords, status == "cover")
                continue
            if kind != "r" or not _matches("r", obj.tags, table):
                continue
            rel_type = obj.tags["type"] if "type" in obj.tags else ""
            if rel_type not in ("multipolygon", "boundary"):
                continue
            made = _relation_geometry(obj, stored, box)
            if made is None:
                continue
            geometry, flat, cover = made
            writer.add("relation", int(obj.id), _tag_dict(obj.tags), geometry, flat, cover)
        stored.clear()
        del proc
        del pool
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
    return dest


def _ensure_clip(root: str, region: Region, source: str,
                 box: tuple[float, float, float, float], should_stop,
                 progress, index: int, total: int, threads: int) -> str:
    folder = _clip_dir(root, region, source, box)
    if _clip_ready(folder, box):
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
    """Features in this file, in order. Reused while the file is unchanged."""
    try:
        st = os.stat(path)
    except OSError:
        return _parse_geojsonseq(path)
    token = (st.st_mtime_ns, st.st_size)
    with _READ_LOCK:
        hit = _SEQ_CACHE.get(path)
        if hit is not None and (hit[0], hit[1]) == token:
            return hit[2]
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
