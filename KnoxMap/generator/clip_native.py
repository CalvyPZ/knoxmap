"""The clip DLL calls: decide what falls inside a selection and write the cut.

Once a daily has been filed into the area grid, a later map does not walk
that file again. It decides which saved ways, places, and relations meet the
box and writes them out. That decision is the slow part — every vertex was a
Python float — so the helper does it in one call.

What crosses the boundary:

* in - the grid directory, the selection, and which of ways, places, and
  relations this call should write;
* out - the clip directory the rest of the program already reads.

A missing or older library is reported and the caller cuts the area itself.
"""
from __future__ import annotations

import ctypes

from . import ways_native

ABI_VERSION = 1
PART_WAYS = 1
PART_NODES = 2
PART_RELATIONS = 4
STOPPED = 5

_READY = False
_WHY = ""


class Unavailable(RuntimeError):
    """No usable library. The caller cuts the selection itself."""


class WalkNote(ctypes.Structure):
    """How far a daily walk has got. The helper writes this while it runs.

    phase 1 is the harbour pass, 2 is the roads, 3 is the cut. done and total
    are bytes of the daily during a pass, and features during the cut. A
    total of zero means the sentence has changed and the percent is not known
    yet.
    """

    _fields_ = [
        ("phase", ctypes.c_uint32),
        ("done", ctypes.c_uint64),
        ("total", ctypes.c_uint64),
    ]


def load():
    """The helper library, with the clip calls declared. Raises Unavailable."""
    global _READY, _WHY
    try:
        lib = ways_native.load()
    except ways_native.Unavailable as exc:
        _WHY = str(exc)
        raise Unavailable(_WHY) from exc
    if _READY:
        return lib
    if _WHY:
        raise Unavailable(_WHY)
    try:
        _declare(lib)
        abi = lib.knox_c_abi_version()
    except AttributeError as exc:
        _WHY = "the helper library has no clip calls"
        raise Unavailable(_WHY) from exc
    if abi != ABI_VERSION:
        _WHY = (f"the helper was built for clip format {abi}, "
                f"this program speaks {ABI_VERSION}")
        raise Unavailable(_WHY)
    _READY = True
    return lib


def _declare(lib) -> None:
    u32 = ctypes.c_uint32
    f64 = ctypes.c_double
    void = ctypes.c_void_p
    lib.knox_c_abi_version.restype = u32
    lib.knox_c_abi_version.argtypes = []
    lib.knox_c_error_text.restype = ctypes.c_char_p
    lib.knox_c_error_text.argtypes = [u32]
    lib.knox_c_cut.restype = u32
    lib.knox_c_cut.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p,
        f64, f64, f64, f64,
        f64, f64, f64, f64,
        f64,
        u32, u32, u32, u32, u32, u32, u32, u32,
        void, ctypes.c_size_t,
    ]


def walk_ready() -> bool:
    """True when this library can read a daily itself."""
    try:
        lib = load()
    except Unavailable:
        return False
    try:
        getattr(lib, "knox_c_walk")
    except AttributeError:
        return False
    return True


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
        return _WHY or f"clip error {code}"
    text = lib.knox_c_error_text(int(code))
    return text.decode("utf-8", "replace") if text else f"clip error {code}"


def cut(span_dir: str, out_dir: str,
        box: tuple[float, float, float, float],
        region: tuple[float, float, float, float],
        deg: float, span_rows: int, span_cols: int, span_wide: int,
        out_rows: int, out_cols: int,
        filters_version: int, clip_version: int,
        parts: int, node_rules: bytes) -> int:
    """Write the selection into ``out_dir``. Returns zero when the cut worked."""
    lib = load()
    south, west, north, east = box
    rs, rw, rn, re = region
    rules = node_rules or b""
    held = (ctypes.c_uint8 * len(rules)).from_buffer_copy(rules) if rules else None
    rules_ptr = ctypes.cast(held, ctypes.c_void_p) if held is not None else None
    span_path = span_dir.encode("utf-8")
    out_path = out_dir.encode("utf-8")
    return int(lib.knox_c_cut(
        span_path, out_path,
        south, west, north, east, rs, rw, rn, re, float(deg),
        int(span_rows), int(span_cols), int(span_wide),
        int(out_rows), int(out_cols),
        int(filters_version), int(clip_version), int(parts),
        rules_ptr, len(rules)))


def walk(pbf: str, span_dir: str, out_dir: str,
         box: tuple[float, float, float, float],
         region: tuple[float, float, float, float],
         deg: float, span_wide: int,
         out_rows: int, out_cols: int,
         filters_version: int, clip_version: int, span_version: int,
         mtime_ms: int, rules: bytes, stop, note: WalkNote) -> int:
    """Read the daily, file the regional grid, and write the clip.

    ``rules`` is ``kind/key`` or ``kind/key=value`` lines. ``stop`` is a
    ``c_uint32`` set from another thread when the walk should abandon.
    ``note`` is filled in as the walk moves, so that same thread can show
    which pass is running. Returns zero when the clip was written.
    """
    lib = load()
    fn = lib.knox_c_walk
    if not getattr(fn, "_knox_set", False):
        u32 = ctypes.c_uint32
        f64 = ctypes.c_double
        fn.restype = u32
        fn.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
            f64, f64, f64, f64,
            f64, f64, f64, f64,
            f64,
            u32, u32, u32, u32, u32, u32,
            ctypes.c_int64,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(u32),
            ctypes.POINTER(WalkNote),
        ]
        fn._knox_set = True
    south, west, north, east = box
    rs, rw, rn, re = region
    raw = rules or b""
    held = (ctypes.c_uint8 * len(raw)).from_buffer_copy(raw) if raw else None
    rules_ptr = ctypes.cast(held, ctypes.c_void_p) if held is not None else None
    pbf_path = pbf.encode("utf-8")
    span_path = span_dir.encode("utf-8")
    out_path = out_dir.encode("utf-8")
    return int(fn(
        pbf_path, span_path, out_path,
        south, west, north, east, rs, rw, rn, re, float(deg),
        int(span_wide), int(out_rows), int(out_cols),
        int(filters_version), int(clip_version), int(span_version),
        int(mtime_ms),
        rules_ptr, len(raw),
        ctypes.byref(stop), ctypes.byref(note)))
