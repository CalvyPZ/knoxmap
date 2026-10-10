"""The terrain DLL: one memory pool, native threads, no interpreter lock.

Drawing the ground is millions of polygon fills into three bitmaps, and in
Python that work happened inside PIL with the interpreter lock held. More cores
only helped by drawing more maps at once, and each of those held another full set
of bitmaps - PIL keeps an RGB image at four bytes a pixel, and there are three of
them - so the worker count was decided by free memory rather than by the number
of processors. A county map is a quarter of a billion tiles.

The helper library does the same drawing in Rust. Python still decides what is on the
ground: it reads the OSM extract, works out that a way is a residential street,
cuts junctions and bridges and house plots with GEOS, and projects every vertex
to tiles. What it no longer does is put the pixels down. It records an ordered
painting program instead, hands every piece of ground of every map and mod over
in one buffer, and the library draws all of them at once - one allocation of
about seven bytes a tile a piece, every core drawing into its own band of rows.

What crosses the boundary:

* in - one buffer: the colour sets, the procedural passes, and for each piece of
  ground its painting program in the order Python would have painted it;
* out - the finished bitmaps, written by the library itself, and the biome plane
  of each piece, handed back one piece at a time because the cell PNGs cut from
  it are still written here.

Nothing calls back into Python while the pool is running, and a finished piece's
memory is given back as soon as its PNGs are written, which is what keeps the
memory flat however many maps are queued.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import threading

MAGIC = b"KNOXTER1"
ABI_VERSION = 1

PIECE_BYTES = 64
CMD_BYTES = 32
RING_BYTES = 12
POINT_BYTES = 8
PASS_BYTES = 24
PICK_BYTES = 16
CELL_BYTES = 8
BAND_BYTES = 12
N_SECTIONS = 12
HEADER_BYTES = 16 + N_SECTIONS * 16

# Points go over as sixteenths of a tile: finer than the ground can show, and
# enough that a kerb line lands on the right side of a tile every time.
POINT_SCALE = 16

NO_COLOUR = 0xFFFFFFFF
NO_BIOME = 0xFFFF
ANY_SET = 0xFF

# Shapes, as blob.rs reads them.
FILL, STROKE, RECT, RELATION, DISC, PASS = range(1, 7)

# Which pictures a command writes.
TO_GROUND = 1 << 0
TO_VEG = 1 << 1
TO_BIOME = 1 << 2
TO_WATER = 1 << 3
TO_LAKE = 1 << 4
TO_MASK = 1 << 5

# Pass kinds.
SCATTER, GRID, WOODLAND, SHORES, SPAWN, SOFTEN, PAINT_MASK, KERBS, NEAR = range(1, 10)

# A scatter or a grid that only lands where the scratch mask holds `extra`.
PASS_IN_MASK = 1 << 0
# Leave a tile alone when something is already growing there.
PASS_BARE = 1 << 1

ROLE_OUTER, ROLE_INNER = 0, 1

# Piece states, as pool.rs writes them.
WAITING, DRAWING, DONE, FAILED, STOPPED = range(5)


class Unavailable(RuntimeError):
    """No usable DLL. The caller draws the terrain itself."""


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
    for key in ("KNOXMAP_HELPER_DLL", "KNOXMAP_TERRAIN_DLL"):
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
            _LIB_WHY = "the terrain library is not next to the program"
            raise Unavailable(_LIB_WHY)
        try:
            lib = ctypes.CDLL(found)
        except OSError as exc:
            _LIB_WHY = f"{found}: {exc}"
            raise Unavailable(_LIB_WHY) from exc
        try:
            _declare(lib)
            abi = lib.knox_t_abi_version()
        except AttributeError as exc:
            _LIB_WHY = f"{found}: not a terrain library ({exc})"
            raise Unavailable(_LIB_WHY) from exc
        if abi != ABI_VERSION:
            # A library from another build of the program. Drawing against the
            # wrong record layout would paint nonsense.
            _LIB_WHY = (f"{found}: built for format {abi}, "
                        f"this program speaks {ABI_VERSION}")
            raise Unavailable(_LIB_WHY)
        _LIB = lib
        return lib


def _declare(lib) -> None:
    u32 = ctypes.c_uint32
    u64 = ctypes.c_uint64
    lib.knox_t_abi_version.restype = u32
    lib.knox_t_abi_version.argtypes = []
    lib.knox_t_cores.restype = u32
    lib.knox_t_cores.argtypes = []
    lib.knox_t_open.restype = ctypes.c_void_p
    lib.knox_t_open.argtypes = [ctypes.c_char_p, ctypes.c_size_t, u32, u64,
                                ctypes.POINTER(u32)]
    lib.knox_t_start.restype = u32
    lib.knox_t_start.argtypes = [ctypes.c_void_p]
    lib.knox_t_wait.restype = u32
    lib.knox_t_wait.argtypes = [ctypes.c_void_p, u64]
    lib.knox_t_cancel.restype = None
    lib.knox_t_cancel.argtypes = [ctypes.c_void_p]
    lib.knox_t_progress.restype = None
    lib.knox_t_progress.argtypes = [ctypes.c_void_p, ctypes.POINTER(u64),
                                    ctypes.POINTER(u64)]
    lib.knox_t_piece_count.restype = u32
    lib.knox_t_piece_count.argtypes = [ctypes.c_void_p]
    lib.knox_t_status.restype = u32
    lib.knox_t_status.argtypes = [ctypes.c_void_p, u32]
    lib.knox_t_fault.restype = u32
    lib.knox_t_fault.argtypes = [ctypes.c_void_p, u32]
    lib.knox_t_next.restype = u32
    lib.knox_t_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(u32),
                                ctypes.POINTER(u32), ctypes.POINTER(u32),
                                ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
                                ctypes.POINTER(ctypes.c_size_t)]
    lib.knox_t_release.restype = u32
    lib.knox_t_release.argtypes = [ctypes.c_void_p, u32]
    lib.knox_t_memory.restype = None
    lib.knox_t_memory.argtypes = [ctypes.c_void_p, ctypes.POINTER(u64),
                                  ctypes.POINTER(u64)]
    lib.knox_t_plan.restype = None
    lib.knox_t_plan.argtypes = [ctypes.c_void_p, ctypes.POINTER(u32),
                                ctypes.POINTER(u32)]
    lib.knox_t_error_text.restype = ctypes.c_char_p
    lib.knox_t_error_text.argtypes = [u32]
    lib.knox_t_close.restype = None
    lib.knox_t_close.argtypes = [ctypes.c_void_p]


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
        return f"terrain error {code}"
    text = lib.knox_t_error_text(code)
    return text.decode("utf-8", "replace") if text else f"terrain error {code}"


# ---------------------------------------------------------------- the strings

class Strings:
    """The one string table. Every name and directory is interned, so a path
    repeated by every piece of a town is sent once."""

    def __init__(self) -> None:
        self.at: dict[str, int] = {}
        self.parts: list[bytes] = []
        self.index = bytearray()
        self._off = 0

    def add(self, text) -> int:
        if text is None:
            text = ""
        text = str(text)
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


def packed(colour) -> int:
    """A palette colour as the library reads it: 0x00RRGGBB, or no colour."""
    if colour is None:
        return NO_COLOUR
    if isinstance(colour, int):
        return colour & 0xFFFFFF
    r, g, b = colour[0], colour[1], colour[2]
    return ((int(r) & 0xFF) << 16) | ((int(g) & 0xFF) << 8) | (int(b) & 0xFF)


# ---------------------------------------------------------------- the program

class Program:
    """One piece of ground's painting program, in painting order.

    The order is the whole point. Every thread of the library walks this list
    from the start and draws only its own rows, so a drive over a lawn, a kerb
    over the carriageway and a tree over both all land the way they did when PIL
    drew them one after another. Handing shapes out to whichever thread was free
    would not keep that.

    Each command carries the rows it can reach, so a thread drawing rows four
    thousand to four thousand five hundred of a city skips everything else
    without looking at a point.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = int(width)
        self.height = int(height)
        self.cmds = bytearray()
        self.rings = bytearray()
        self.points = bytearray()
        self.n_cmds = 0
        self.n_rings = 0
        self.n_points = 0

    # -- the shapes ---------------------------------------------------------

    def fill(self, rings, ground=None, veg=None, biome=None, *,
             water=False, lake=False, mask=None) -> None:
        """Every ring filled on its own, as ``ImageDraw.polygon`` did: a lake
        drawn as two rings is two lakes, not one with a hole in it."""
        self._shape(FILL, rings, 0, ground, veg, biome, water, lake, mask)

    def relation(self, outers, inners, ground=None, veg=None, biome=None, *,
                 water=False, lake=False, mask=None) -> None:
        """Inside an outer ring and outside every inner one: a courtyard, a lake
        with an island, a field in a wood."""
        rings = [(ring, ROLE_OUTER) for ring in outers]
        rings += [(ring, ROLE_INNER) for ring in inners]
        if not any(role == ROLE_OUTER for _ring, role in rings):
            return
        self._shape(RELATION, rings, 0, ground, veg, biome, water, lake, mask)

    def stroke(self, rings, width, ground=None, veg=None, biome=None, *,
               water=False, lake=False, mask=None) -> None:
        """A line of that width with round bends, as ``draw.line`` with a curve
        joint and a circle at every vertex came to."""
        self._shape(STROKE, rings, width, ground, veg, biome, water, lake, mask)

    def rect(self, box, ground=None, veg=None, biome=None, *,
             water=False, lake=False, mask=None) -> None:
        """Two corners, both included, as ``draw.rectangle`` draws it."""
        x0, y0, x1, y1 = box
        self._shape(RECT, [[(x0, y0), (x1, y1)]], 0,
                    ground, veg, biome, water, lake, mask)

    def disc(self, point, diameter, ground=None, veg=None, biome=None, *,
             water=False, lake=False, mask=None) -> None:
        """A filled circle on that point: a single mapped tree, a bollard."""
        self._shape(DISC, [[point]], diameter,
                    ground, veg, biome, water, lake, mask)

    def run_pass(self, which: int) -> None:
        """Run a procedural pass here, in this order. Everything before it is
        finished first, because a pass asks about the tile next door."""
        self._put(PASS, 0, 0, int(which), 0,
                  NO_COLOUR, NO_COLOUR, NO_BIOME, 0, 0, self.height - 1)

    # -- what goes in the buffer -------------------------------------------

    def _shape(self, shape, rings, width, ground, veg, biome,
               water, lake, mask) -> None:
        to = 0
        if ground is not None:
            to |= TO_GROUND
        if veg is not None:
            to |= TO_VEG
        if biome is not None:
            to |= TO_BIOME
        if water:
            to |= TO_WATER
        if lake:
            to |= TO_LAKE
        if mask is not None:
            to |= TO_MASK
        if not to:
            return
        least = 2 if shape in (FILL, RELATION) else 1
        at = self.n_rings
        kept = 0
        top, bottom = self.height, -1
        left, right = self.width, -1
        for ring in rings:
            role = ROLE_OUTER
            if (isinstance(ring, tuple) and len(ring) == 2
                    and isinstance(ring[1], int)
                    and not isinstance(ring[0], (int, float))):
                ring, role = ring
            points = [(float(x), float(y)) for x, y in ring]
            if len(points) <= least:
                continue
            if shape == FILL and len(points) < 3:
                continue
            self._ring(points, role)
            kept += 1
            for x, y in points:
                top = min(top, y)
                bottom = max(bottom, y)
                left = min(left, x)
                right = max(right, x)
        if not kept or bottom < 0:
            self._forget(at, kept)
            return
        # A line or a circle reaches half its width out from its centre, and the
        # round end caps reach the same, so the rows it can touch are wider than
        # the rows its points are on.
        reach = (int(width) // 2) + 1 if width else 1
        first = max(0, int(top) - reach)
        last = min(self.height - 1, int(bottom) + reach + 1)
        if last < 0 or first >= self.height or right < -reach or left > self.width + reach:
            self._forget(at, kept)
            return
        self._put(shape, to, int(width), at, kept,
                  packed(ground), packed(veg),
                  NO_BIOME if biome is None else int(biome) & 0xFFFF,
                  0 if mask is None else int(mask) & 0xFFFF,
                  first, last)

    def _ring(self, points, role) -> None:
        at = self.n_points
        for x, y in points:
            self.points += struct.pack("<ii", round(x * POINT_SCALE),
                                       round(y * POINT_SCALE))
        self.n_points += len(points)
        self.rings += struct.pack("<III", at, len(points), int(role))
        self.n_rings += 1

    def _forget(self, at, kept) -> None:
        """Drop rings written for a command that turned out to paint nothing, so
        a map full of features outside the frame costs nothing to send."""
        if not kept:
            return
        first = at * RING_BYTES
        head = self.rings[first:first + RING_BYTES]
        point_at = struct.unpack_from("<I", head)[0]
        del self.rings[first:]
        del self.points[point_at * POINT_BYTES:]
        self.n_rings = at
        self.n_points = point_at

    def _put(self, shape, to, width, ring_at, ring_n,
             ground, veg, biome, mask, top, bottom) -> None:
        self.cmds += struct.pack("<BBHIIIIHHHH4x", shape, to, width & 0xFFFF,
                                 ring_at, ring_n, ground, veg, biome, mask,
                                 max(0, top) & 0xFFFF, max(0, bottom) & 0xFFFF)
        self.n_cmds += 1


def _is_point(thing) -> bool:
    """Whether a two-long tuple is a point rather than a (ring, role) pair."""
    return all(isinstance(part, (int, float)) for part in thing)


# ---------------------------------------------------------------- the painter

class Painter:
    """A stand-in for ``ImageDraw.Draw`` that records instead of drawing.

    The renderer draws through a handful of calls - polygon, line, ellipse,
    rectangle - and most of its stages do nothing else. Handing them one of these
    instead of a real draw object is what lets a stage move to the library
    without being rewritten: the calls are the same, the colour goes to whichever
    pictures this painter was opened on, and what comes out is a program rather
    than pixels.
    """

    def __init__(self, program: Program, target: str = "ground",
                 biome: int | None = None, mask: int | None = None,
                 water: bool = False, lake: bool = False) -> None:
        self.program = program
        self.target = target
        self.biome = biome
        self.mask = mask
        self.water = water
        self.lake = lake

    def _where(self, fill):
        ground = fill if self.target == "ground" else None
        veg = fill if self.target == "veg" else None
        biome = self.biome
        if self.target == "biome" and fill is not None:
            biome = int(fill)
        return ground, veg, biome

    def polygon(self, ring, fill=None, outline=None, width=0) -> None:
        ground, veg, biome = self._where(fill)
        self.program.fill([ring], ground, veg, biome,
                          water=self.water, lake=self.lake, mask=self.mask)

    def line(self, ring, fill=None, width=1, joint=None) -> None:
        ground, veg, biome = self._where(fill)
        self.program.stroke([ring], max(1, int(width)), ground, veg, biome,
                            water=self.water, lake=self.lake, mask=self.mask)

    def ellipse(self, box, fill=None, outline=None, width=0) -> None:
        x0, y0, x1, y1 = box
        middle = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
        across = max(abs(x1 - x0), abs(y1 - y0))
        ground, veg, biome = self._where(fill)
        self.program.disc(middle, int(round(across)) + 1, ground, veg, biome,
                          water=self.water, lake=self.lake, mask=self.mask)

    def rectangle(self, box, fill=None, outline=None, width=0) -> None:
        ground, veg, biome = self._where(fill)
        self.program.rect(box, ground, veg, biome,
                          water=self.water, lake=self.lake, mask=self.mask)

    def point(self, xy, fill=None) -> None:
        points = [xy] if _is_point(xy) else list(xy)
        ground, veg, biome = self._where(fill)
        for point in points:
            self.program.rect((point[0], point[1], point[0], point[1]),
                              ground, veg, biome, water=self.water,
                              lake=self.lake, mask=self.mask)


# ---------------------------------------------------------------- the job

class Job:
    """Every piece of ground of every map and mod in the run, as one buffer."""

    def __init__(self) -> None:
        self.strings = Strings()
        self.sets: list[tuple[int, ...]] = []
        self.set_at: dict[tuple[int, ...], int] = {}
        self.passes = bytearray()
        self.n_passes = 0
        self.picks = bytearray()
        self.n_picks = 0
        self.bands = bytearray()
        self.n_bands = 0
        self.cells = bytearray()
        self.n_cells = 0
        self.pieces: list[dict] = []
        self.cmds = bytearray()
        self.rings = bytearray()
        self.points = bytearray()
        self.n_cmds = 0
        self.n_rings = 0
        self.n_points = 0

    # -- the colour sets ---------------------------------------------------

    def colour_set(self, colours) -> int:
        """Intern a set of ground colours: what a pass is allowed over, or has
        to keep off. Returns the id a pass refers to it by."""
        key = tuple(sorted({packed(colour) for colour in colours}))
        if not key:
            return ANY_SET
        held = self.set_at.get(key)
        if held is not None:
            return held
        which = len(self.sets)
        if which >= ANY_SET:
            raise ValueError("too many colour sets for one terrain job")
        self.set_at[key] = which
        self.sets.append(key)
        return which

    # -- the passes --------------------------------------------------------

    def add_pass(self, kind: int, *, over=ANY_SET, avoid=ANY_SET, radius=0,
                 chance=0.0, salt=0, spacing=0, picks=(), extra=0,
                 flags=0) -> int:
        at = self.n_picks
        for pick in picks:
            ground, veg, biome, weight = _pick(pick)
            self.picks += struct.pack("<IIHH4x", ground, veg, biome, weight)
            self.n_picks += 1
        which = self.n_passes
        self.passes += struct.pack("<BBBBfHHIHHI", int(kind), int(over) & 0xFF,
                                   int(avoid) & 0xFF, int(radius) & 0xFF,
                                   float(chance), int(salt) & 0xFFFF,
                                   int(spacing) & 0xFFFF, at,
                                   self.n_picks - at, int(flags) & 0xFFFF,
                                   int(extra) & 0xFFFFFFFF)
        self.n_passes += 1
        return which

    def spawn_bands(self, bands) -> tuple[int, int]:
        """The zombie density of each kind of ground, tried in order: the first
        band whose colours match a tile is the one it gets."""
        at = self.n_bands
        for band in bands:
            which, chance, lo, hi, salt = band
            self.bands += struct.pack("<BxHHHH2x", int(which) & 0xFF,
                                      _share(chance), _share(lo), _share(hi),
                                      int(salt) & 0xFFFF)
            self.n_bands += 1
        return at, self.n_bands - at

    def keep_cells(self, cells) -> tuple[int, int]:
        at = self.n_cells
        for cell_x, cell_y in cells:
            self.cells += struct.pack("<ii", int(cell_x), int(cell_y))
            self.n_cells += 1
        return at, self.n_cells - at

    # -- the pieces --------------------------------------------------------

    def piece(self, program: Program, *, name: str, folder: str, seed: int,
              grass, nothing, open_biome: int, spawn_density: int,
              spawn_scale: int = 10, spawn_bands=(0, 0), keep=(0, 0)) -> int:
        """Add one piece of ground, with its program."""
        cmd_at = self.n_cmds
        self._shift(program)
        which = len(self.pieces)
        self.pieces.append({
            "record": struct.pack(
                "<9I8xBB2xIIIHH",
                program.width, program.height, int(seed) & 0xFFFFFFFF,
                self.strings.add(name), self.strings.add(folder),
                cmd_at, program.n_cmds, keep[0], keep[1],
                int(open_biome) & 0xFF, max(1, int(spawn_density)) & 0xFF,
                packed(grass), packed(nothing),
                spawn_bands[0], spawn_bands[1] & 0xFFFF,
                max(1, int(spawn_scale)) & 0xFFFF),
            "tiles": program.width * program.height,
        })
        return which

    def _shift(self, program: Program) -> None:
        """Append one piece's program, moving its ring and point references on to
        where they have landed in the shared tables."""
        ring_base = self.n_rings
        point_base = self.n_points
        for at in range(program.n_cmds):
            head = at * CMD_BYTES
            (shape, to, width, ring_at, ring_n, ground, veg, biome, mask,
             top, bottom) = struct.unpack_from("<BBHIIIIHHHH4x", program.cmds, head)
            if shape != PASS:
                ring_at += ring_base
            self.cmds += struct.pack("<BBHIIIIHHHH4x", shape, to, width,
                                     ring_at, ring_n, ground, veg, biome, mask,
                                     top, bottom)
        self.n_cmds += program.n_cmds
        for at in range(program.n_rings):
            head = at * RING_BYTES
            point_at, count, role = struct.unpack_from("<III", program.rings, head)
            self.rings += struct.pack("<III", point_at + point_base, count, role)
        self.n_rings += program.n_rings
        self.points += program.points
        self.n_points += program.n_points

    # -- the buffer --------------------------------------------------------

    def pack(self) -> bytes:
        """The whole job as one buffer, laid out the way blob.rs reads it."""
        order = sorted(range(len(self.pieces)),
                       key=lambda at: -self.pieces[at]["tiles"])
        sections = [
            self.strings.blob(),
            bytes(self.strings.index),
            self._sets(),
            b"".join(piece["record"] for piece in self.pieces),
            bytes(self.cmds),
            bytes(self.rings),
            bytes(self.points),
            bytes(self.passes),
            bytes(self.picks),
            bytes(self.cells),
            b"".join(struct.pack("<I", at) for at in order),
            bytes(self.bands),
        ]
        head = bytearray(MAGIC)
        head += struct.pack("<II", ABI_VERSION, len(self.pieces))
        at = HEADER_BYTES
        for part in sections:
            head += struct.pack("<QQ", at, len(part))
            at += len(part)
        return bytes(head) + b"".join(sections)

    def _sets(self) -> bytes:
        """How many sets there are, where each one's colours start, then the
        colours."""
        if not self.sets:
            return b""
        out = bytearray(struct.pack("<I", len(self.sets)))
        at = 0
        for colours in self.sets:
            out += struct.pack("<II", at, len(colours))
            at += len(colours)
        for colours in self.sets:
            for colour in colours:
                out += struct.pack("<I", colour)
        return bytes(out)


def _pick(pick) -> tuple[int, int, int, int]:
    """What a pass may put down on a tile: ground, vegetation, biome, weight."""
    if isinstance(pick, dict):
        ground = packed(pick.get("ground"))
        veg = packed(pick.get("veg"))
        biome = pick.get("biome")
        weight = pick.get("weight", 1)
    else:
        parts = list(pick) + [None] * (4 - len(pick))
        ground, veg, biome, weight = parts[0], parts[1], parts[2], parts[3]
        ground, veg = packed(ground), packed(veg)
        weight = 1 if weight is None else weight
    return (ground, veg,
            NO_BIOME if biome is None else int(biome) & 0xFFFF,
            max(0, min(0xFFFF, int(weight))))


def _share(value) -> int:
    """A fraction as the library reads it: sixteen bits of nought to one."""
    return max(0, min(0xFFFF, int(round(float(value) * 65535.0))))


# ---------------------------------------------------------------- the session

class Finished:
    """One drawn piece of ground: its bitmaps are on disc, and its biome plane is
    still in the library's memory until `release`."""

    def __init__(self, index: int, width: int, height: int, biome: memoryview):
        self.index = index
        self.width = width
        self.height = height
        self.biome = biome


class Session:
    """One run of the library."""

    def __init__(self, buffer: bytes, threads: int = 0, budget: int = 0) -> None:
        self.lib = load()
        # The library reads this buffer in place for its whole life, so the
        # reference is held here rather than left to the caller.
        self.buffer = buffer
        fault = ctypes.c_uint32(0)
        handle = self.lib.knox_t_open(buffer, len(buffer), int(threads),
                                      int(budget), ctypes.byref(fault))
        if not handle:
            raise RuntimeError(why(fault.value))
        self.handle = ctypes.c_void_p(handle)
        self.closed = False

    def start(self) -> None:
        code = self.lib.knox_t_start(self.handle)
        if code:
            raise RuntimeError(why(code))

    def wait(self, ms: int = 50) -> bool:
        return bool(self.lib.knox_t_wait(self.handle, int(ms)))

    def cancel(self) -> None:
        self.lib.knox_t_cancel(self.handle)

    def progress(self) -> tuple[int, int]:
        done = ctypes.c_uint64(0)
        total = ctypes.c_uint64(0)
        self.lib.knox_t_progress(self.handle, ctypes.byref(done),
                                 ctypes.byref(total))
        return done.value, total.value

    def memory(self) -> tuple[int, int]:
        peak = ctypes.c_uint64(0)
        now = ctypes.c_uint64(0)
        self.lib.knox_t_memory(self.handle, ctypes.byref(peak), ctypes.byref(now))
        return peak.value, now.value

    def plan(self) -> tuple[int, int]:
        threads = ctypes.c_uint32(0)
        at_once = ctypes.c_uint32(0)
        self.lib.knox_t_plan(self.handle, ctypes.byref(threads),
                             ctypes.byref(at_once))
        return threads.value, at_once.value

    def piece_count(self) -> int:
        return int(self.lib.knox_t_piece_count(self.handle))

    def status(self, at: int) -> int:
        return int(self.lib.knox_t_status(self.handle, at))

    def fault(self, at: int) -> int:
        return int(self.lib.knox_t_fault(self.handle, at))

    def next_piece(self) -> Finished | None:
        index = ctypes.c_uint32(0)
        width = ctypes.c_uint32(0)
        height = ctypes.c_uint32(0)
        bytes_at = ctypes.POINTER(ctypes.c_uint8)()
        count = ctypes.c_size_t(0)
        got = self.lib.knox_t_next(self.handle, ctypes.byref(index),
                                   ctypes.byref(width), ctypes.byref(height),
                                   ctypes.byref(bytes_at), ctypes.byref(count))
        if not got:
            return None
        view = ctypes.cast(
            bytes_at, ctypes.POINTER(ctypes.c_uint8 * count.value)).contents
        return Finished(index.value, width.value, height.value, memoryview(view))

    def release(self, piece: Finished) -> None:
        piece.biome = memoryview(b"")
        self.lib.knox_t_release(self.handle, piece.index)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.lib.knox_t_close(self.handle)
        self.handle = ctypes.c_void_p(0)

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def run(buffer: bytes, *, threads: int = 0, budget: int = 0,
        on_piece=None, on_progress=None, should_stop=None, poll_ms: int = 50):
    """Draw every piece in the buffer. Returns the status of each piece.

    `on_piece` is called with each finished piece as it comes back, on this
    thread, so that the cell PNGs are written while the library is still drawing
    the rest of the maps - and the piece's memory is given back as soon as that
    call returns, which is what keeps the memory flat.
    """
    with Session(buffer, threads=threads, budget=budget) as session:
        session.start()
        while True:
            done = session.wait(poll_ms)
            while True:
                piece = session.next_piece()
                if piece is None:
                    break
                try:
                    if on_piece is not None:
                        on_piece(piece)
                finally:
                    session.release(piece)
            if on_progress is not None:
                on_progress(*session.progress())
            if should_stop is not None and should_stop():
                session.cancel()
            if done:
                break
        return [session.status(at) for at in range(session.piece_count())], \
               [session.fault(at) for at in range(session.piece_count())]
