"""The way-processing calls in the helper library.

Picking the roads and buildings out of a selection used to measure every
painted way in Python: a copy of its packed coordinates, a bounding box, the
hit or cover test, and a record in the regional grid. The helper does that
work in Rust. This module finds the library, hands each way across, and reads
the grid back when the read is finished.

What crosses the boundary:

* in - the selection, the region the grid covers, then one way at a time: its
  libosmium node records and the JSON of its tags;
* out - whether the selection keeps the way, and the grid cells to file beside
  the places and relations Python still writes.

A missing or older library is reported and the caller does the same work
itself. Nothing calls back into Python while a way is being measured.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading

ABI_VERSION = 1

MISS, HIT, COVER = 0, 1, 2
INVALID, UNREADABLE = -1, -2


class Unavailable(RuntimeError):
    """No usable library. The caller measures the ways itself."""


class WayAnswer(ctypes.Structure):
    """Mirrors WayAnswer. The field order is the interface."""
    _fields_ = [
        ("code", ctypes.c_int32),
        ("min_x", ctypes.c_int32),
        ("min_y", ctypes.c_int32),
        ("max_x", ctypes.c_int32),
        ("max_y", ctypes.c_int32),
        ("area", ctypes.c_uint32),
        ("xy", ctypes.POINTER(ctypes.c_uint8)),
        ("xy_len", ctypes.c_size_t),
    ]


class Answer:
    """One way, copied out of the library before the next call reuses it."""

    def __init__(self, code: int, min_x: int, min_y: int, max_x: int, max_y: int, xy: bytes):
        self.code = code
        self.min_x = min_x
        self.min_y = min_y
        self.max_x = max_x
        self.max_y = max_y
        self.xy = xy


_LIB = None
_LIB_LOCK = threading.Lock()
_LIB_WHY = ""


def _names() -> tuple[str, ...]:
    if sys.platform == "win32":
        return ("helper.dll",)
    if sys.platform == "darwin":
        return ("libhelper.dylib",)
    return ("libhelper.so",)


def _places() -> list[str]:
    """Where the library may be, in the order it is looked for."""
    out = []
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        out.append(frozen)
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    out.append(root)
    out.append(os.path.join(root, "Helpers", "target", "release"))
    out.append(os.path.dirname(os.path.abspath(sys.argv[0] or ".")))
    for key in ("KNOXMAP_HELPER_DLL", "KNOXMAP_WAYS_DLL"):
        told = os.environ.get(key)
        if told:
            out.insert(0, told if os.path.isdir(told) else os.path.dirname(told))
    return out


def path() -> str | None:
    for folder in _places():
        for name in _names():
            full = os.path.join(folder, name)
            if os.path.isfile(full):
                return full
    return None


def load():
    """The loaded library, or raise Unavailable. Asked once per process."""
    global _LIB, _LIB_WHY
    if _LIB is not None:
        return _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        if _LIB_WHY:
            raise Unavailable(_LIB_WHY)
        found = path()
        if not found:
            _LIB_WHY = "the way library is not next to the program"
            raise Unavailable(_LIB_WHY)
        try:
            lib = ctypes.CDLL(found)
        except OSError as exc:
            _LIB_WHY = f"{found}: {exc}"
            raise Unavailable(_LIB_WHY) from exc
        try:
            _declare(lib)
            abi = lib.knox_w_abi_version()
        except AttributeError as exc:
            _LIB_WHY = f"{found}: not a way library ({exc})"
            raise Unavailable(_LIB_WHY) from exc
        if abi != ABI_VERSION:
            _LIB_WHY = (f"{found}: built for way format {abi}, "
                        f"this program speaks {ABI_VERSION}")
            raise Unavailable(_LIB_WHY)
        _LIB = lib
        return lib


def _declare(lib) -> None:
    u32 = ctypes.c_uint32
    i32 = ctypes.c_int32
    f64 = ctypes.c_double
    void = ctypes.c_void_p
    lib.knox_w_abi_version.restype = u32
    lib.knox_w_abi_version.argtypes = []
    lib.knox_w_error_text.restype = ctypes.c_char_p
    lib.knox_w_error_text.argtypes = [u32]
    lib.knox_w_open.restype = void
    lib.knox_w_open.argtypes = [f64, f64, f64, f64, f64, f64, f64, f64, f64, u32,
                                ctypes.POINTER(u32)]
    lib.knox_w_offer.restype = u32
    lib.knox_w_offer.argtypes = [
        void, ctypes.c_int64, void, u32, void, ctypes.c_size_t, u32, u32,
        ctypes.POINTER(WayAnswer),
    ]
    lib.knox_w_abandon.restype = None
    lib.knox_w_abandon.argtypes = [void]
    lib.knox_w_cell_count.restype = u32
    lib.knox_w_cell_count.argtypes = [void]
    lib.knox_w_cell.restype = u32
    lib.knox_w_cell.argtypes = [
        void, u32, ctypes.POINTER(i32), ctypes.POINTER(i32),
        ctypes.POINTER(void), ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.knox_w_wide.restype = u32
    lib.knox_w_wide.argtypes = [void, ctypes.POINTER(void), ctypes.POINTER(ctypes.c_size_t)]
    lib.knox_w_classify.restype = i32
    lib.knox_w_classify.argtypes = [void, ctypes.c_size_t, f64, f64, f64, f64]
    lib.knox_w_is_area.restype = u32
    lib.knox_w_is_area.argtypes = [void, ctypes.c_size_t, u32]
    lib.knox_w_close.restype = None
    lib.knox_w_close.argtypes = [void]


def reason() -> str:
    """Why the library is not being used, for the log."""
    return _LIB_WHY


def ready() -> bool:
    try:
        load()
    except Unavailable:
        return False
    return True


def why(code: int) -> str:
    try:
        lib = load()
    except Unavailable:
        return f"way error {code}"
    text = lib.knox_w_error_text(code)
    return text.decode("utf-8", "replace") if text else f"way error {code}"


def _keep(data: bytes):
    """A pointer to these bytes that stays valid while the caller holds it."""
    if not data:
        return None, None
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    return buf, ctypes.cast(buf, ctypes.c_void_p)


def classify(xy: bytes, south: float, west: float, north: float, east: float) -> int:
    """0 miss, 1 hit, 2 cover, from packed x/y pairs."""
    lib = load()
    held, ptr = _keep(xy)
    return int(lib.knox_w_classify(ptr, len(xy), south, west, north, east))


def is_area(tags: dict, closed: bool) -> bool:
    """True when a closed way with these tags is a footprint."""
    if not closed or not tags:
        return False
    raw = json.dumps(tags, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    lib = load()
    held, ptr = _keep(raw)
    return bool(lib.knox_w_is_area(ptr, len(raw), 1))


class Session:
    """One selection. Closed when the clip has filed the grid."""

    def __init__(self, box: tuple[float, float, float, float],
                 region: tuple[float, float, float, float],
                 deg: float, wide: int):
        self._lib = load()
        fault = ctypes.c_uint32()
        south, west, north, east = box
        rs, rw, rn, re = region
        self._p = self._lib.knox_w_open(
            south, west, north, east, rs, rw, rn, re, float(deg), int(wide),
            ctypes.byref(fault))
        if not self._p:
            raise Unavailable(why(int(fault.value)))

    def offer(self, wid: int, ptr: int, count: int, tags: bytes,
              paint: bool, needed: bool) -> Answer | None:
        """Measure one way. None when the call itself failed."""
        out = WayAnswer()
        held, tag_ptr = _keep(tags)
        rc = self._lib.knox_w_offer(
            self._p, int(wid), ctypes.c_void_p(ptr), int(count),
            tag_ptr, len(tags), 1 if paint else 0, 1 if needed else 0,
            ctypes.byref(out))
        if rc != 0:
            return None
        xy = b""
        if out.xy_len:
            addr = ctypes.cast(out.xy, ctypes.c_void_p).value
            if addr:
                xy = ctypes.string_at(addr, out.xy_len)
        return Answer(int(out.code), int(out.min_x), int(out.min_y),
                      int(out.max_x), int(out.max_y), xy)

    def take_selection(self) -> bytes:
        """Ways already filed that meet this selection, for a handoff.

        Empty when this library build has no such call. The bytes are a count
        and then each way's id, status, coordinates, and tags.
        """
        if not self._p:
            return b""
        fn = getattr(self._lib, "knox_w_take_selection", None)
        if fn is None:
            return b""
        if not getattr(fn, "_knox_set", False):
            fn.restype = ctypes.c_uint32
            fn.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_size_t),
            ]
            fn._knox_set = True
        raw = ctypes.c_void_p()
        length = ctypes.c_size_t()
        rc = fn(self._p, ctypes.byref(raw), ctypes.byref(length))
        if rc != 0 or not length.value or not raw.value:
            return b""
        return ctypes.string_at(raw.value, length.value)

    def abandon(self) -> None:
        if self._p:
            self._lib.knox_w_abandon(self._p)

    def pour(self, span) -> None:
        """Append this selection's way records onto the grid Python is filing."""
        if not self._p:
            return
        count = int(self._lib.knox_w_cell_count(self._p))
        for index in range(count):
            row = ctypes.c_int32()
            col = ctypes.c_int32()
            raw = ctypes.c_void_p()
            length = ctypes.c_size_t()
            rc = self._lib.knox_w_cell(
                self._p, index, ctypes.byref(row), ctypes.byref(col),
                ctypes.byref(raw), ctypes.byref(length))
            if rc != 0 or not length.value or not raw.value:
                continue
            key = (int(row.value), int(col.value))
            buf = span.cells.get(key)
            if buf is None:
                buf = bytearray()
                span.cells[key] = buf
            buf += ctypes.string_at(raw.value, length.value)
        raw = ctypes.c_void_p()
        length = ctypes.c_size_t()
        rc = self._lib.knox_w_wide(self._p, ctypes.byref(raw), ctypes.byref(length))
        if rc == 0 and length.value and raw.value:
            span.wide += ctypes.string_at(raw.value, length.value)

    def close(self) -> None:
        if self._p:
            self._lib.knox_w_close(self._p)
            self._p = None
