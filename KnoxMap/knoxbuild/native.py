"""The building DLL: one memory pool, native threads, no interpreter lock.

Room layout used to run in worker *processes*, because CPython's lock means two
threads never lay out two buildings at the same time. A process cannot share the
catalog, so every worker loaded its own copy of catalog.py - 1.4 MB of tables as
millions of objects - and the worker count was decided by free memory rather
than by the number of processors.

The helper library does the same work in Rust. The tables are serialised once into a
single buffer, the library copies that buffer once, and every native thread reads the
same bytes with no lock and no second copy. This module is the whole Python side:
find the library, turn the jobs into the buffer, hand it over, and read the
answers back.

What crosses the boundary:

* in - the catalog as data (tile entries, furniture, room definitions, styles),
  then one 48-byte record per building and its footprint as a bitmap;
* out - per building: how many storeys and rooms it came to, how much furniture,
  its outside doors, and the reason if it failed.

The .tbx files themselves are written by the worker threads. Nothing calls back
into Python while the pool is running.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading

MAGIC = b"KNOXBLD1"
ABI_VERSION = 1
JOB_BYTES = 48
N_SECTIONS = 10
HEADER_BYTES = 16 + N_SECTIONS * 16
CATALOG_VERSION = 1

# Statuses, as results.rs writes them.
PENDING, ACTIVE, DONE, FAILED, UNSUPPORTED, CANCELLED = range(6)

DOOR_SLOTS = 6


class Unavailable(RuntimeError):
    """No usable DLL. The caller lays the buildings out itself."""


class ResultRec(ctypes.Structure):
    """Mirrors results.rs ResultRec. The field order is the interface."""
    _fields_ = [
        ("status", ctypes.c_uint8),
        ("door_n", ctypes.c_uint8),
        ("storeys", ctypes.c_uint16),
        ("rooms", ctypes.c_uint32),
        ("furniture", ctypes.c_uint32),
        ("error_off", ctypes.c_uint32),
        ("error_len", ctypes.c_uint32),
        ("doors", ctypes.c_int16 * (DOOR_SLOTS * 4)),
    ]


class Progress(ctypes.Structure):
    """Mirrors pool.rs Progress."""
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("done", ctypes.c_uint64),
        ("failed", ctypes.c_uint64),
        ("unsupported", ctypes.c_uint64),
        ("active", ctypes.c_uint64),
        ("claimed", ctypes.c_uint64),
        ("workers", ctypes.c_uint32),
        ("stopped", ctypes.c_uint32),
        ("scratch_bytes", ctypes.c_uint64),
    ]


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
        out.append(frozen)                      # inside the exe
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    out.append(root)                            # repo root
    out.append(os.path.join(root, "Helpers", "target", "release"))
    out.append(os.path.dirname(os.path.abspath(sys.argv[0] or ".")))
    for key in ("KNOXMAP_HELPER_DLL", "KNOXMAP_BUILDINGS_DLL"):
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
            _LIB_WHY = "the building library is not next to the program"
            raise Unavailable(_LIB_WHY)
        try:
            lib = ctypes.CDLL(found)
        except OSError as exc:
            _LIB_WHY = f"{found}: {exc}"
            raise Unavailable(_LIB_WHY) from exc
        try:
            _declare(lib)
            abi = lib.knox_abi_version()
        except AttributeError as exc:
            _LIB_WHY = f"{found}: not a building library ({exc})"
            raise Unavailable(_LIB_WHY) from exc
        if abi != ABI_VERSION:
            # A library from another build of the program. Laying buildings out
            # against the wrong record layout would write nonsense.
            _LIB_WHY = (f"{found}: built for format {abi}, "
                        f"this program speaks {ABI_VERSION}")
            raise Unavailable(_LIB_WHY)
        _LIB = lib
        return lib


def _declare(lib) -> None:
    lib.knox_abi_version.restype = ctypes.c_uint32
    lib.knox_abi_version.argtypes = []
    lib.knox_error_message.restype = ctypes.c_char_p
    lib.knox_error_message.argtypes = [ctypes.c_uint32]
    lib.knox_open.restype = ctypes.c_void_p
    lib.knox_open.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_uint32)]
    lib.knox_job_count.restype = ctypes.c_uint64
    lib.knox_job_count.argtypes = [ctypes.c_void_p]
    lib.knox_start.restype = ctypes.c_uint32
    lib.knox_start.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    lib.knox_progress.restype = ctypes.c_uint32
    lib.knox_progress.argtypes = [ctypes.c_void_p, ctypes.POINTER(Progress)]
    lib.knox_statuses.restype = ctypes.c_size_t
    lib.knox_statuses.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.knox_cancel.restype = None
    lib.knox_cancel.argtypes = [ctypes.c_void_p]
    lib.knox_wait.restype = ctypes.c_uint32
    lib.knox_wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.knox_finish.restype = ctypes.c_uint32
    lib.knox_finish.argtypes = [ctypes.c_void_p]
    lib.knox_results.restype = ctypes.c_uint32
    lib.knox_results.argtypes = [ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.POINTER(ResultRec)),
                                 ctypes.POINTER(ctypes.c_size_t)]
    lib.knox_error_text.restype = ctypes.c_uint32
    lib.knox_error_text.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
                                    ctypes.POINTER(ctypes.c_size_t)]
    lib.knox_close.restype = None
    lib.knox_close.argtypes = [ctypes.c_void_p]


def reason() -> str:
    """Why the library is not being used, for the log."""
    return _LIB_WHY


def ready() -> bool:
    try:
        load()
    except Unavailable:
        return False
    return True


# ---------------------------------------------------------------- the strings

class Strings:
    """The one string table. Every tile name, room name and path is interned, so
    a name the catalog repeats ten thousand times is sent once."""

    def __init__(self) -> None:
        self.at: dict[str, int] = {}
        self.parts: list[bytes] = []
        self.index = bytearray()
        self._off = 0

    def add(self, text: str) -> int:
        if text is None:
            text = ""
        held = self.at.get(text)
        if held is not None:
            return held
        raw = text.encode("utf-8")
        which = len(self.at)
        self.at[text] = which
        self.parts.append(raw)
        self.index += struct.pack("<II", self._off, len(raw))
        self._off += len(raw)
        return which

    def blob(self) -> bytes:
        return b"".join(self.parts)

    def fork(self) -> "Strings":
        """A copy to add this run's own labels and paths to, so the shared table
        does not grow by every path of every piece of a whole town."""
        out = Strings()
        out.at = dict(self.at)
        out.parts = list(self.parts)
        out.index = bytearray(self.index)
        out._off = self._off
        return out


# ---------------------------------------------------------------- the catalog

# The rooms the Rust layout knows (plan.rs RoomKind), and the catalog key each
# one takes its colour, floors and furniture from. A corridor is furnished and
# named as a hall; a lift shaft is a closet nobody can reach.
ROOM_SOURCE = {
    "livingroom": "livingroom",
    "kitchen": "kitchen",
    "openplan": "openplan",
    "bedroom": "bedroom",
    "kidsbedroom": "kidsbedroom",
    "bathroom": "bathroom",
    "dining": "dining",
    "hall": "hall",
    "storage": "storage",
    "closet": "closet",
    "corridor": "hall",
    "landing": "hall",
    "office": "office",
    # A shop with nothing recorded about what it sells is a general store:
    # shelves, a counter and a fridge. As "shop" - which no table has - it
    # would have fallen back to storage, and every shop in every town would
    # have come out as a stockroom.
    "shop": "generalstore",
    "storeroom": "storage",
    "lobby": "lobby",
    "classroom": "classroom",
    # A hospital ward is a clinic room - a bed, a sink, a counter - and not
    # somebody's bedroom with a wardrobe in it.
    "ward": "clinic",
    "concourse": "concourse",
    "warehouse": "warehouse",
    "garage": "garage",
    "laundry": "laundry",
    # A school or works canteen: rows of tables, a servery counter, vending.
    "canteen": "diningroom",
    "barroom": "bar",
    "restroom": "bathroom",
}

# Pieces a room has one of however big it is (layout.ONCE).
_ONCE_PREFIX = ("sofa_", "double_bed", "bed_", "wardrobe", "erika_vending")
# Lie flat and block nothing.
_FLAT_LAYERS = ("FloorFurniture",)
_TABLES = ("round_table", "dining_table")

_CATALOG = None
_CATALOG_LOCK = threading.Lock()


class _Catalog:
    """The serialised tables, and the indexes needed to write the jobs."""

    def __init__(self, strings: Strings, body: bytes, n_entries: int,
                 style_at: dict[str, int], room_at: dict[str, int]) -> None:
        self.strings = strings
        self.body = body
        self.n_entries = n_entries
        # Style name -> its index in the serialised table.
        self.style_at = style_at
        # Room key -> its index in the serialised table. A building's uses are
        # sent as these, 1-based, which is how a room records what it really is.
        self.room_at = room_at


def _entry_key(entry: dict) -> tuple:
    tiles = entry.get("tiles") or {}
    return (entry.get("category", ""), tuple(sorted(tiles.items())))


class _Entries:
    """The tile-entry table being built: the shared rows, then every row a style
    or a floor list needs. Rust indexes these 1-based, as the .tbx does."""

    def __init__(self, strings: Strings) -> None:
        self.strings = strings
        self.rows: list[bytes] = []
        self.at: dict[tuple, int] = {}
        self.n_base = 0

    def add(self, entry: dict | None) -> int:
        if not entry:
            return 0
        held = self.at.get(_entry_key(entry))
        if held is not None:
            return held
        return self.append(entry)

    def append(self, entry: dict) -> int:
        """A row of its own, even where the same tiles are in the table already.

        The shared rows go in this way: catalog.py numbers them as constants
        (EXTERIOR_WALL = 1, DOOR = 2 and so on), and collapsing two that happen
        to hold the same tiles would shift every constant after them.
        """
        key = _entry_key(entry)
        tiles = entry.get("tiles") or {}
        out = bytearray()
        out += struct.pack("<II", self.strings.add(entry.get("category", "")),
                           len(tiles))
        for role, tile in tiles.items():
            out += struct.pack("<II", self.strings.add(str(role)),
                               self.strings.add(str(tile)))
        self.rows.append(bytes(out))
        which = len(self.rows)
        # The first copy of a fingerprint is the one lookups resolve to.
        self.at.setdefault(key, which)
        return which

    def blob(self) -> bytes:
        return b"".join(self.rows)


def _style_record(style: dict, entries: _Entries, strings: Strings) -> bytes:
    """One building style as the fixed record catalog.rs reads."""
    PEAKED, ROOF30, HAS_CURTAINS = 1, 2, 4
    roof = style.get("roof") or {}
    caps = (roof.get("caps") or {}).get("tiles") or {}
    slopes = (roof.get("slopes") or {}).get("tiles") or {}
    flags = 0
    if roof.get("peaked"):
        flags |= PEAKED
    # 30-degree roofs only where the gable ends have 30-degree tiles.
    if "CapPeak30S1" in caps and "Slope30S1" in slopes:
        flags |= ROOF30
    if "curtains" in style:
        flags |= HAS_CURTAINS
    front, front_curtains = (style.get("shop_front") or (None, None))
    by_levels = style.get("windows_by_levels") or ()
    out = bytearray()
    out += struct.pack(
        "<15I",
        strings.add(str(style.get("name", ""))),
        entries.add(style.get("exterior")),
        entries.add(style.get("interior")),
        entries.add(style.get("floor")),
        entries.add(style.get("window")),
        entries.add(style.get("curtains")),
        entries.add(style.get("trim")),
        entries.add(style.get("shutters")),
        entries.add(style.get("grime")),
        entries.add(roof.get("slopes")),
        entries.add(roof.get("tops")),
        entries.add(roof.get("caps")),
        entries.add(front),
        entries.add(front_curtains),
        flags,
    )
    out += struct.pack("<I", len(by_levels))
    for levels, window, curtains in by_levels:
        out += struct.pack("<III", int(levels), entries.add(window),
                           entries.add(curtains))
    return bytes(out)


def _furn_flags(role: str, layer: str, once: set[str]) -> int:
    ONCE, WALL, FLAT, TABLE = 1, 2, 4, 8
    flags = 0
    if role in once or role.startswith(_ONCE_PREFIX):
        flags |= ONCE
    if layer not in ("Furniture", "FloorFurniture"):
        # Hung on a wall. Leaving the layer off a wall piece drops it onto the
        # floor layer, which is how a painting ends up standing mid-room.
        flags |= WALL
    if layer in _FLAT_LAYERS:
        flags |= FLAT
    if role in _TABLES:
        flags |= TABLE
    return flags


def _groups_for(kind: str, furn_at: dict[str, int], layout) -> bytes:
    """The centre group a room wants, then the smaller sets to fall back on."""
    out = bytearray()
    wanted = []
    spec = layout.CENTRE_GROUPS.get(kind)
    if spec:
        group, repeat = spec
        wanted.append((repeat, group))
    for smaller in layout.FALLBACK_GROUPS.get(kind, ()):
        wanted.append((1, smaller))
    kept = []
    for repeat, group in wanted:
        pieces = [(furn_at[role], dx, dy, "WNES".index(orient))
                  for role, dx, dy, orient in group if role in furn_at]
        if pieces:
            kept.append((repeat, pieces))
    out += struct.pack("<I", len(kept))
    for repeat, pieces in kept:
        out += struct.pack("<II", int(repeat), len(pieces))
        for furn, dx, dy, orient in pieces:
            out += struct.pack("<Ihhh", furn, int(dx), int(dy), int(orient))
    return bytes(out)


def catalog() -> _Catalog:
    """The tables, serialised once per process."""
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    with _CATALOG_LOCK:
        if _CATALOG is not None:
            return _CATALOG
        _CATALOG = _build_catalog()
        return _CATALOG


def _build_catalog() -> _Catalog:
    from . import catalog as C
    from . import layout

    strings = Strings()
    strings.add("")                           # id 0 is the empty string
    entries = _Entries(strings)
    for entry in C.TILE_ENTRIES:
        entries.append(entry)
    entries.n_base = len(entries.rows)

    # Furniture, in catalog order, so an index is stable for the whole run.
    once = set(layout.ONCE)
    furn_at: dict[str, int] = {}
    furn_rows = bytearray()
    for role, by_orient in C.FURNITURE.items():
        layer = C.FURNITURE_LAYERS.get(role, "Furniture")
        furn_at[role] = len(furn_at)
        furn_rows += struct.pack("<III", strings.add(role), strings.add(layer),
                                 _furn_flags(role, layer, once))
        for orient in ("W", "N", "E", "S"):
            tiles = by_orient.get(orient) or {}
            furn_rows += struct.pack("<I", len(tiles))
            for key, tile in tiles.items():
                dx, dy = (int(part) for part in str(key).split(","))
                furn_rows += struct.pack("<hhI", dx, dy, strings.add(str(tile)))

    # Rooms: what each kind is called in the game, its colour, its floors and
    # what goes in it. The layout's own kinds come first, in ROOM_SOURCE order,
    # then every other room any table can name - the pizza kitchen, the gun
    # store, the prison cells - so that what the map says is on a street can be
    # handed to a room as it is.
    CEILING = 1
    room_rows = bytearray()
    room_at: dict[str, int] = {}

    def _room_row(key: str, source: str) -> None:
        style = layout.ROOM_STYLE.get(source) or layout.ROOM_STYLE.get("storage")
        if not style or key in room_at:
            return
        floor_idx, _label, wish = style
        game = layout.ROOM_NAME.get(source, source)
        color = C.ROOM_COLORS.get(key) or C.ROOM_COLORS.get(source) or "200 200 200"
        flags = 0 if key == "concourse" else CEILING
        floors = [entries.add(C.floor_entry(name))
                  for name in layout.FLOOR_CHOICES.get(source, ())
                  if name in C.EXTRA_FLOORS]
        roles = [furn_at[role] for role in wish if role in furn_at]
        room_at[key] = len(room_at)
        room_rows.extend(struct.pack(
            "<IIIII", strings.add(key), strings.add(game),
            strings.add(str(color)), int(floor_idx), flags))
        room_rows.extend(struct.pack("<I", len(floors)))
        room_rows.extend(b"".join(struct.pack("<I", which) for which in floors))
        room_rows.extend(struct.pack("<I", len(roles)))
        room_rows.extend(b"".join(struct.pack("<I", which) for which in roles))
        room_rows.extend(_groups_for(source, furn_at, layout))

    for key, source in ROOM_SOURCE.items():
        _room_row(key, source)
    for key in layout.ROOM_STYLE:
        _room_row(key, key)
    n_rooms = len(room_at)

    # The trades a shop front becomes where the map says nothing about it.
    from . import interiors
    store_rows = b"".join(
        struct.pack("<II", room_at[name] + 1, int(weight))
        for name, weight in interiors.STORE_WEIGHTS.items() if name in room_at)
    n_stores = len(store_rows) // 8

    # Floors for the rooms of one sort of building: a station's corridor is not
    # somebody's hallway.
    kind_rows = bytearray()
    n_kinds = 0
    for kind, per_room in layout.KIND_FLOOR_CHOICES.items():
        rows = []
        for room, names in per_room.items():
            if room not in room_at:
                # A room no table the library reads can name. Its floors would
                # never be asked for.
                continue
            floors = [entries.add(C.floor_entry(name)) for name in names
                      if name in C.EXTRA_FLOORS]
            if floors:
                rows.append((room, floors))
        if not rows:
            continue
        kind_rows += struct.pack("<II", strings.add(str(kind)), len(rows))
        for room, floors in rows:
            kind_rows += struct.pack("<II", strings.add(room), len(floors))
            kind_rows += b"".join(struct.pack("<I", which) for which in floors)
        n_kinds += 1

    # The interior wall sets a building paints its rooms from.
    int_walls = [entries.add(entry) for entry in C.INTERIOR_WALLS]

    # Every style any building may be given, keyed by name. build.py composes a
    # special building's variants on the fly (wall_variants), so the dicts are
    # not the catalog's own objects; their names are what identifies them, and
    # a variant is named for what it is made of.
    style_at: dict[str, int] = {}
    style_rows = bytearray()
    for style in _all_styles():
        name = str(style.get("name", ""))
        if name in style_at:
            continue
        style_at[name] = len(style_at)
        style_rows += _style_record(style, entries, strings)

    # Erika's shop fronts, where the mod tiles are there to use: a wall of glass
    # in a painted frame instead of windows set in brick. The hanging signs are
    # not sent: nothing reads them yet, and a table the workers never look at is
    # only bytes to copy.
    front_rows = bytearray()
    n_fronts = 0
    if layout._erika_ready():
        for ext, inside, door in C.ERIKA_STOREFRONTS:
            front_rows += struct.pack("<III", entries.add(ext),
                                      entries.add(inside), entries.add(door))
            n_fronts += 1
    # The header holds a count and an offset for each table, then the entry
    # indexes catalog.py keeps as constants and the roles the writer names.
    head_bytes = 4 * (1 + 17 + 12 + 5)
    entry_blob = entries.blob()
    parts = [entry_blob, bytes(furn_rows), bytes(room_rows), bytes(kind_rows),
             b"".join(struct.pack("<I", which) for which in int_walls),
             bytes(style_rows), bytes(front_rows), store_rows]
    offsets = []
    at = head_bytes
    for part in parts:
        offsets.append(at)
        at += len(part)
    head = struct.pack(
        "<18I",
        CATALOG_VERSION,
        len(entries.rows), offsets[0], entries.n_base,
        len(furn_at), offsets[1],
        n_rooms, offsets[2],
        n_kinds, offsets[3],
        len(int_walls), offsets[4],
        len(style_at), offsets[5],
        n_fronts, offsets[6],
        n_stores, offsets[7],
    )
    head += struct.pack(
        "<12I",
        C.EXTERIOR_WALL, C.DOOR, C.DOOR_FRAME, C.WINDOW, C.CURTAINS, C.STAIRS,
        C.INTERIOR_WALL, C.ROOF_CAP, C.ROOF_SLOPE, C.ROOF_TOP, C.CEILING,
        C.INTERIOR_WALL_TRIM,
    )
    head += struct.pack(
        "<5I",
        strings.add("West"), strings.add("North"), strings.add("CapGapE3"),
        strings.add("CapGapS3"), strings.add("Floor"),
    )
    if len(head) != head_bytes:
        raise Unavailable("the catalog header came out the wrong size")
    return _Catalog(strings, head + b"".join(parts), len(entries.rows),
                    style_at, room_at)


def _all_styles() -> list[dict]:
    """Every style a building may be given, in a stable order.

    The special kinds' variants are asked of build.py rather than rebuilt here,
    so the two cannot drift: what it hands a building is what is in this table.
    The import is late because build.py imports this module.
    """
    from . import catalog as C
    from . import build as B

    out: list[dict] = list(C.HOUSE_STYLES)
    for kind in sorted(C.SPECIAL_STYLES):
        out.extend(B.wall_variants(kind))
    return out


# ------------------------------------------------------------------- the jobs

# What build.py decided a building is, as blob.rs Kind numbers them. The keys
# are SPECIAL_BY_VALUE's values - what `special` actually holds - and nothing
# else; a name missing here would quietly come out as a house. A castle and a
# stadium have no kind of their own in the layout, so they go in as civic: the
# rooms their recipes ask for are the civic ones. A barn is a warehouse rather
# than a shed, so it comes out as one big space instead of a row of cupboards.
KIND_ID = {
    None: 0,
    "apartment": 1,
    "school": 5,
    "medical": 6,
    "church": 7,
    "civic": 8, "castle": 8, "stadium": 8,
    "mall": 9,
    "restaurant": 11,
    "shop": 13,
    "police": 15,
    "fire": 16,
    "military": 17,
    "library": 18,
    "industrial": 19,
    "barn": 4,
}
STREET_ID = {None: 0, "N": 1, "S": 2, "E": 3, "W": 4}
COMMERCIAL, RETAIL, HOTEL, HAS_MASK = 1, 2, 4, 8


def _mask_bits(mask, width: int, height: int, out: bytearray) -> int:
    """One footprint as a bitmap, rows padded to whole bytes so a row starts on
    a byte boundary. Returns the bit the footprint starts at."""
    start = len(out) * 8
    row_bytes = (width + 7) // 8
    if mask is None:
        out += b"\xff" * (row_bytes * height)
        return start
    import numpy as np
    bits = np.packbits(np.asarray(mask, dtype=bool)[:height, :width],
                       axis=-1, bitorder="little")
    if bits.shape[1] < row_bytes:
        bits = np.pad(bits, ((0, 0), (0, row_bytes - bits.shape[1])))
    if bits.shape[0] < height:
        bits = np.pad(bits, ((0, height - bits.shape[0]), (0, 0)))
    out += bits.tobytes()
    return start


def blob(jobs: list[tuple], pieces: list[tuple[int, int]]) -> bytes:
    """Every building of every piece in one buffer.

    `jobs` are build.py's layout tuples with the piece they belong to and where
    they sit in it appended:

        (w, h, levels, commercial, seed, special, mask, settings, style, label,
         path, street, retail, uses, hotel, piece, x0, y0)

    `pieces` is (width, height) per piece, which is what the shared-wall pass
    buckets the footprints over.
    """
    cat = catalog()
    strings = cat.strings.fork()

    masks = bytearray()
    records = bytearray()
    costs = []
    # What the map says is on each building's ground floor, as catalog rooms:
    # one (offset, count) pair per building, then the pairs themselves.
    use_index = bytearray()
    use_pairs = bytearray()
    # One settings record per distinct room size: the only setting the layout
    # reads, and every building of a run usually shares it.
    settings_at: dict[float, int] = {}
    settings_rows = bytearray()
    for at, job in enumerate(jobs):
        (w, h, levels, commercial, seed, special, mask, settings, style, label,
         path, street, retail, uses, hotel, piece, x0, y0) = job[:18]
        flags = 0
        if commercial:
            flags |= COMMERCIAL
        if retail:
            flags |= RETAIL
        if hotel:
            flags |= HOTEL
        mask_bit = 0
        if mask is not None:
            flags |= HAS_MASK
            mask_bit = _mask_bits(mask, int(w), int(h), masks)
        kept = 0
        use_index += struct.pack("<II", len(use_pairs) // 8, 0)
        for pair in (uses or ()):
            try:
                front, back = pair
            except (TypeError, ValueError):
                continue
            front_at = cat.room_at.get(str(front), -1)
            if front_at < 0:
                continue
            use_pairs += struct.pack("<II", front_at + 1,
                                     cat.room_at.get(str(back), -1) + 1)
            kept += 1
        # The count is only known once the unusable ones have been dropped.
        use_index[-4:] = struct.pack("<I", kept)
        room_size = float(getattr(settings, "room_size", 24) or 24)
        held = settings_at.get(room_size)
        if held is None:
            held = len(settings_at)
            settings_at[room_size] = held
            settings_rows += struct.pack("<f", room_size)
        records += struct.pack(
            "<HHHHIHHHHIIIHHBB10x",
            int(w), int(h), max(1, int(levels)),
            KIND_ID.get(special, 0xFFFF) & 0xFFFF,
            int(seed) & 0xFFFFFFFF,
            (cat.style_at.get(str(style.get("name", "")), 0xFFFF)
             if style else 0xFFFF) & 0xFFFF,
            held,
            kept if kept else 0xFFFF,
            int(piece) & 0xFFFF,
            strings.add(str(label or "")),
            strings.add(str(path or "")),
            mask_bit,
            int(x0) & 0xFFFF, int(y0) & 0xFFFF,
            STREET_ID.get(street, 0), flags,
        )
        costs.append((int(w) * int(h) * max(1, int(levels)), at))

    # The costliest building first, so the pool hands out the slow ones while
    # every thread is still free.
    costs.sort(key=lambda pair: (-pair[0], pair[1]))
    order = b"".join(struct.pack("<I", at) for _cost, at in costs)

    piece_rows = b"".join(struct.pack("<II", int(pw), int(ph))
                          for pw, ph in pieces)
    # A style travels as an index into the catalog's own table, so the styles
    # section carries nothing; it keeps its place so the header never shifts.
    sections = [strings.blob(), bytes(strings.index), cat.body,
                b"", bytes(settings_rows), bytes(use_index + use_pairs),
                bytes(masks), piece_rows, bytes(records), order]
    out = bytearray()
    out += MAGIC
    out += struct.pack("<II", ABI_VERSION, len(jobs))
    at = HEADER_BYTES
    body = bytearray()
    for part in sections:
        out += struct.pack("<QQ", at, len(part))
        body += part
        at += len(part)
    out += body
    return bytes(out)


# ---------------------------------------------------------------- the session

class Session:
    """One pool of buildings, from opening to the answers."""

    def __init__(self, data: bytes) -> None:
        self.lib = load()
        code = ctypes.c_uint32(0)
        handle = self.lib.knox_open(data, len(data), ctypes.byref(code))
        if not handle:
            text = self.lib.knox_error_message(code.value) or b""
            raise Unavailable(f"the building library refused the work: "
                              f"{text.decode('utf-8', 'replace')}")
        self.handle = ctypes.c_void_p(handle)
        self.n = int(self.lib.knox_job_count(self.handle))
        self._statuses = ctypes.create_string_buffer(max(1, self.n))

    def start(self, workers: int = 0, low_priority: bool = True) -> int:
        return int(self.lib.knox_start(self.handle, int(workers),
                                       1 if low_priority else 0))

    def progress(self) -> Progress:
        out = Progress()
        self.lib.knox_progress(self.handle, ctypes.byref(out))
        return out

    def statuses(self) -> bytes:
        got = int(self.lib.knox_statuses(self.handle, self._statuses, self.n))
        return self._statuses.raw[:got]

    def cancel(self) -> None:
        self.lib.knox_cancel(self.handle)

    def wait(self, timeout_ms: int = 50) -> bool:
        """True once every worker has stopped."""
        return bool(self.lib.knox_wait(self.handle, int(timeout_ms)))

    def results(self) -> list[tuple]:
        """(storeys, rooms, furniture, error, doors) per building, in job order."""
        if self.lib.knox_finish(self.handle) != 0:
            return []
        recs = ctypes.POINTER(ResultRec)()
        n = ctypes.c_size_t(0)
        if self.lib.knox_results(self.handle, ctypes.byref(recs),
                                 ctypes.byref(n)) != 0:
            return []
        text = ctypes.POINTER(ctypes.c_uint8)()
        text_n = ctypes.c_size_t(0)
        self.lib.knox_error_text(self.handle, ctypes.byref(text),
                                 ctypes.byref(text_n))
        reasons = bytes(bytearray(text[i] for i in range(text_n.value))) \
            if text and text_n.value else b""
        out = []
        for at in range(n.value):
            rec = recs[at]
            why = ""
            if rec.error_len:
                raw = reasons[rec.error_off:rec.error_off + rec.error_len]
                why = raw.decode("utf-8", "replace")
            elif rec.status == UNSUPPORTED:
                why = "the building library does not do this building yet"
            elif rec.status in (PENDING, ACTIVE, CANCELLED):
                why = "stopped before this building was laid out"
            doors = []
            for slot in range(min(rec.door_n, DOOR_SLOTS)):
                ox, oy, ix, iy = rec.doors[slot * 4:slot * 4 + 4]
                doors.append((int(ox), int(oy), int(ix), int(iy)))
            out.append((int(rec.storeys), int(rec.rooms), int(rec.furniture),
                        why, doors))
        return out

    def close(self) -> None:
        if getattr(self, "handle", None):
            self.lib.knox_close(self.handle)
            self.handle = None

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def run(jobs: list[tuple], pieces: list[tuple[int, int]], *,
        should_stop=None, on_done=None, on_state=None,
        workers: int = 0) -> list[tuple]:
    """Generate every building, and report as they come in.

    Returns the same shape build.py's own layout pass does:
    (storeys, rooms, furniture, error, doors) per building, in job order.

    `on_state` is handed the buildings being worked on and the ones finished, as
    the live preview wants them. It is read from the status bytes, which the
    workers write without taking any lock.
    """
    if not jobs:
        return []
    data = blob(jobs, pieces)
    with Session(data) as session:
        live = session.start(workers)
        if live == 0:
            raise Unavailable("the building library could not start a worker")

        def report() -> None:
            if on_done is not None:
                held = session.progress()
                on_done(int(held.done + held.failed + held.unsupported),
                        int(held.total), int(held.active), int(held.workers))
            if on_state is not None:
                raw = session.statuses()
                on_state({at for at, held in enumerate(raw) if held == ACTIVE},
                         {at for at, held in enumerate(raw)
                          if held in (DONE, FAILED, UNSUPPORTED)})

        while not session.wait(40):
            if should_stop is not None and should_stop():
                session.cancel()
                session.wait(2000)
                break
            report()
        report()
        return session.results()
