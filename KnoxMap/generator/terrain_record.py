"""Recording the ground instead of drawing it.

The renderer's drawing stages all go through a handful of calls: fill this
outline, draw this line that wide, put a circle here. This module gives them
somewhere else to send those calls - a painting program for the helper library - so a
stage moves to the library without being rewritten around it.

Two things are recorded rather than drawn:

* the shapes, through `Painter`, which answers to the same calls as PIL's
  `ImageDraw` and the same ones `biomes.Cover` offers;
* the procedural passes, which were tile-by-tile loops with a random number
  generator in Python and are a record apiece here - what they put down, how
  often, and over what ground. The walking is the library's.

The colour sets are the other half of it. A pass in Python asked "is this tile
asphalt" by comparing against a few constants; the library is told those sets up
front and asks the same question against them. They are built from pz_colors, so
a palette change still only has to happen in one place.
"""
from __future__ import annotations

from . import biomes
from . import pz_colors as C
from . import terrain_native as native

# The ground a pass may be interested in. Each becomes one colour set.
ASPHALT = (C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT)
HARD = (C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT, C.LIGHT_ASPHALT,
        C.PALE_CONCRETE, C.PAVING, C.PAVING_STONE, C.CLAY, C.WATER)
GRASS = (C.DARK_GRASS, C.MEDIUM_GRASS, C.LIGHT_GRASS)
# Where something can grow: grass, and the bare ground a wood can stand on.
SOFT = (C.DARK_GRASS, C.MEDIUM_GRASS, C.LIGHT_GRASS, C.DIRT, C.SAND)
WATER = (C.WATER,)
# Dirt tracks, yards and building footprints, for the spawn map: red over green.
YARDS = (C.DIRT, C.CLAY, C.LIGHT_ASPHALT, C.PAVING, C.PAVING_STONE)

class Palette:
    """The colour sets of one job, interned once."""

    def __init__(self, job: native.Job) -> None:
        self.job = job
        self.asphalt = job.colour_set(ASPHALT)
        self.hard = job.colour_set(HARD)
        self.grass = job.colour_set(GRASS)
        self.soft = job.colour_set(SOFT)
        self.water = job.colour_set(WATER)
        self.yards = job.colour_set(YARDS)
        self.concrete = job.colour_set((C.PALE_CONCRETE,))
        self.wild = job.colour_set((C.DARK_GRASS, C.MEDIUM_GRASS))


class Painter:
    """A stand-in for `ImageDraw.Draw`, recording onto one of the pictures."""

    def __init__(self, record: "Recorder", target: str) -> None:
        self._p = native.Painter(record.program, target)

    def polygon(self, ring, fill=None, outline=None, width=0) -> None:
        self._p.polygon(ring, fill=fill)

    def line(self, ring, fill=None, width=1, joint=None) -> None:
        self._p.line(ring, fill=fill, width=width)

    def ellipse(self, box, fill=None, outline=None, width=0) -> None:
        self._p.ellipse(box, fill=fill)

    def rectangle(self, box, fill=None, outline=None, width=0) -> None:
        self._p.rectangle(box, fill=fill)

    def point(self, xy, fill=None) -> None:
        self._p.point(xy, fill=fill)


class Cover:
    """A stand-in for `biomes.Cover`, recording onto the biome plane.

    The calls are the ones the renderer makes, and the index each feature gets is
    still worked out by `biomes`, so the climate and the tag rules stay where they
    are. What changes is that a water polygon also marks the water masks - the
    library needs to know where the water is to find its banks - and that the
    woods are queued as marks in the scratch mask rather than painted into a
    layer image.
    """

    def __init__(self, record: "Recorder", lat: float, lon: float) -> None:
        self.record = record
        self.climate = biomes.climate_band(lat, lon)
        self.open = biomes.open_pixel(self.climate)
        self._woods: list[tuple[int, int, list, int, tuple]] = []
        self._marks: dict[tuple[int, int], int] = {}

    # -- the shapes --------------------------------------------------------

    def stamp(self, category, tags, rings, *, width=None, relation_rings=None) -> None:
        if category in {"water", "pool"}:
            if width is not None and relation_rings is None:
                self.line(rings, biomes.WATER, width, water=True,
                          lake=biomes.is_lake(tags))
                return
            self.water(rings, tags, relation_rings=relation_rings)
            return
        value = biomes.pixel_for(category, tags, self.climate)
        if relation_rings is not None:
            self.relation(relation_rings, value)
        elif width is not None:
            self.line(rings, value, width)
        else:
            self.polygon(rings, value)

    def polygon(self, rings, value, *, water=False, lake=False) -> None:
        keep = [ring for ring in rings if len(ring) >= 3]
        if keep:
            self.record.program.fill(keep, biome=value, water=water, lake=lake)

    def line(self, rings, value, width, *, water=False, lake=False) -> None:
        keep = [ring for ring in rings if len(ring) >= 2]
        if keep:
            self.record.program.stroke(keep, max(1, int(width)), biome=value,
                                       water=water, lake=lake)

    def rectangle(self, bounds, value) -> None:
        self.record.program.rect(bounds, biome=value)

    def relation(self, role_rings, value, *, water=False, lake=False) -> None:
        outers = [ring for role, ring in role_rings
                  if role != "inner" and len(ring) >= 3]
        inners = [ring for role, ring in role_rings
                  if role == "inner" and len(ring) >= 3]
        if outers:
            self.record.program.relation(outers, inners, biome=value,
                                         water=water, lake=lake)

    def water(self, rings, tags, *, relation_rings=None) -> None:
        lake = biomes.is_lake(tags)
        if relation_rings is not None:
            self.relation(relation_rings, biomes.WATER, water=True, lake=lake)
        else:
            self.polygon(rings, biomes.WATER, water=True, lake=lake)

    # -- the woods ---------------------------------------------------------

    def queue_wood(self, rings, tags, *, scrub=False, radius=0, holes=None) -> None:
        """Mark a wood in the scratch plane. The shade its tiles get, and whether
        a tile is deep inside it or near its edge, are settled by the woodland
        pass once every wood is marked - which is the same order Python used, and
        for the same reason: an edge is a question about the neighbours."""
        pixel = (biomes.scrub_pixel(tags, self.climate) if scrub
                 else biomes.forest_pixel(tags, self.climate))
        self._woods.append((pixel, 1 if scrub else 0, list(rings), int(radius),
                            tuple(holes or ())))

    def flush_woods(self) -> None:
        """Lay the marks down and add one woodland pass for each kind of wood.

        A kind is a biome index: pine on the hills and birch along the river are
        two kinds, and they get a pair of marks each so that neither is shaded as
        the other where they meet.
        """
        if not self._woods:
            return
        record = self.record
        kinds: dict[int, list] = {}
        for pixel, is_scrub, rings, radius, holes in self._woods:
            kinds.setdefault(pixel, []).append((is_scrub, rings, radius, holes))
        for pixel, group in kinds.items():
            forest_mark, scrub_mark = record.wood_marks(pixel)
            for is_scrub, rings, radius, holes in group:
                mark = scrub_mark if is_scrub else forest_mark
                if radius and rings and rings[0]:
                    x, y = rings[0][0]
                    record.program.disc((x, y), max(1, radius) * 2, mask=mark)
                    continue
                keep = [ring for ring in rings if len(ring) >= 3]
                if not keep:
                    continue
                if holes:
                    record.program.relation(
                        keep, [h for h in holes if len(h) >= 3], mask=mark)
                else:
                    record.program.fill(keep, mask=mark)
            record.woodland_pass(pixel, forest_mark, scrub_mark)
        self._woods.clear()

    def apply_shores(self) -> None:
        """One tile of clay where land meets a river, a lake or the sea."""
        self.record.shores_pass()

    def apply_keep(self, keep) -> None:
        # The cells the selection dropped are blanked by the library, from the
        # keep list the piece carries. Nothing to record here.
        return

    # -- the things only the Python path needs ----------------------------

    def paint_mask(self, mask, value, y0) -> None:          # pragma: no cover
        raise NotImplementedError("the terrain library draws from shapes, not masks")

    def begin_mask_batch(self) -> None:
        return

    def end_mask_batch(self) -> None:
        return


class Recorder:
    """One piece of ground being recorded: its program, and the passes it runs."""

    def __init__(self, job: native.Job, palette: Palette, width: int, height: int,
                 lat: float, lon: float, *, tree_density: float = 1.0,
                 metres_per_tile: float = 2.0) -> None:
        self.job = job
        self.palette = palette
        self.program = native.Program(width, height)
        self.width = width
        self.height = height
        self.tree_density = float(tree_density)
        self.metres_per_tile = float(metres_per_tile)
        self.ground = Painter(self, "ground")
        self.veg = Painter(self, "veg")
        self.cover = Cover(self, lat, lon)
        self._wood_marks: dict[int, tuple[int, int]] = {}
        # Woods start above the marks the ground passes use: paving, houses,
        # clearing, and one mark apiece for orchards, cemeteries and wetland.
        self._next_mark = 16

    # -- the marks ---------------------------------------------------------

    def wood_marks(self, pixel: int) -> tuple[int, int]:
        """The pair of scratch marks one kind of wood uses."""
        held = self._wood_marks.get(pixel)
        if held is not None:
            return held
        if self._next_mark + 1 > 255:
            # More kinds of wood than the mask can tell apart. The last pair is
            # shared, which at worst softens an edge between two kinds.
            return self._wood_marks.setdefault(pixel, (254, 255))
        pair = (self._next_mark, self._next_mark + 1)
        self._next_mark += 2
        self._wood_marks[pixel] = pair
        return pair

    # -- the passes --------------------------------------------------------

    def woodland_pass(self, pixel: int, forest_mark: int, scrub_mark: int) -> None:
        """The woods: full shade deep inside, the lighter mix at the edge, scrub
        its own shade, nothing over water or tarmac, and dark grass underneath
        because trees sit best on it.

        `tree_density` below one thins the wood by stepping every tile down one
        grade and above one does the reverse, which is the ladder the Python pass
        walked. It matters because trees are cover: a map buried in forest plays
        completely differently from the same map with hedgerows.
        """
        grades = [C.VEG_NOTHING, C.SPARSE_TREES, C.TREES_DARK_GRASS, C.TREES]
        shift = int(round((self.tree_density - 1.0) * 2))
        full = grades[min(max(grades.index(C.TREES) + shift, 0), len(grades) - 1)]
        edge = grades[min(max(grades.index(C.TREES_DARK_GRASS) + shift, 0),
                          len(grades) - 1)]
        which = self.job.add_pass(
            native.WOODLAND,
            over=self.palette.soft,
            avoid=self.palette.hard,
            spacing=2,
            extra=(forest_mark & 0xFF) | ((scrub_mark & 0xFF) << 8),
            picks=[
                (None, full, pixel, 1),
                (None, edge, pixel, 1),
                (None, C.BUSHES_TREES_DARK_GRASS, pixel, 1),
            ])
        self.program.run_pass(which)

    def shores_pass(self) -> None:
        which = self.job.add_pass(
            native.SHORES,
            over=self.palette.soft,
            spacing=1,
            picks=[
                (None, None, biomes.CLAY_SHORE, 1),
                (None, None, biomes.CLAY_LAKE, 1),
            ])
        self.program.run_pass(which)

    def weather_pass(self) -> None:
        """Worn patches on the tarmac. Every road in a class is otherwise one flat
        colour from kerb to kerb for its whole length, which is the main reason a
        generated town looks printed rather than driven on.

        One patch per fifteen hundred tiles, as the Python pass scattered them,
        and only on asphalt. Where Python picked a centre and gave up if it was
        not tarmac, every tile of a patch is tested here instead: a patch is a
        function of its own position, so each thread can work out the ones that
        reach into its rows without asking another thread what it painted.
        """
        which = self.job.add_pass(
            native.SCATTER,
            over=self.palette.asphalt,
            radius=3,
            chance=1.0 / 1500.0,
            salt=0x5713,
            picks=[(C.DARK_POTHOLE, None, None, 1),
                   (C.LIGHT_POTHOLE, None, None, 1)])
        self.program.run_pass(which)

    def tufts_pass(self, chance: float = 0.2) -> None:
        """Clumps over open grass. A lawn of one flat grass tile looked like felt
        beside the vanilla map, whose grass is broken up everywhere."""
        which = self.job.add_pass(
            native.SCATTER,
            over=self.palette.grass,
            chance=chance,
            salt=0x6A71,
            flags=native.PASS_BARE,
            picks=[(None, C.SHORT_GRASS, None, 1)])
        self.program.run_pass(which)

    def scatter_pass(self, *, over, chance, picks, salt, radius=0) -> None:
        """Any of the one-in-so-many passes: flowers between graves, dense bush
        over a marsh, shrubs along a wall."""
        which = self.job.add_pass(
            native.SCATTER, over=over, chance=chance, salt=salt,
            radius=radius, picks=picks)
        self.program.run_pass(which)

    def grid_pass(self, *, over, spacing, picks, salt, chance=1.0) -> None:
        """Trees in rows: what makes an orchard read as an orchard from the air."""
        which = self.job.add_pass(
            native.GRID, over=over, spacing=max(2, int(spacing)),
            chance=chance, salt=salt, picks=picks)
        self.program.run_pass(which)

    def fill_mask(self, rings, mark: int) -> None:
        """Paint `mark` into the scratch mask. A later pass reads it back."""
        keep = [ring for ring in rings if len(ring) >= 3]
        if keep:
            self.program.fill(keep, mask=int(mark))

    def stroke_mask(self, rings, width: int, mark: int) -> None:
        keep = [ring for ring in rings if len(ring) >= 2]
        if keep:
            self.program.stroke(keep, max(1, int(width)), mask=int(mark))

    def paint_mask_pass(self, mark: int, *, over=native.ANY_SET,
                        ground=None, veg=None, biome=None) -> None:
        """Where the mask holds `mark` and the ground is allowed, put this down."""
        which = self.job.add_pass(
            native.PAINT_MASK, over=over, extra=int(mark) & 0xFF,
            picks=[(ground, veg, biome, 1)])
        self.program.run_pass(which)

    def masked_pass(self, kind: int, mark: int, *, over=native.ANY_SET,
                    spacing=0, chance=1.0, salt=0, picks=(), bare=False) -> None:
        """A scatter or a grid that only lands inside one mark."""
        which = self.job.add_pass(
            kind, over=over, spacing=spacing, chance=chance, salt=salt,
            extra=int(mark) & 0xFF,
            flags=native.PASS_IN_MASK | (native.PASS_BARE if bare else 0),
            picks=picks)
        self.program.run_pass(which)

    def kerbs_pass(self) -> None:
        """Pavement beside a road becomes the kerb that faces the road."""
        colours = (C.KERB_W, C.KERB_N, C.KERB_S, C.KERB_E,
                   C.KERB_NW, C.KERB_SW, C.KERB_NE, C.KERB_SE)
        which = self.job.add_pass(
            native.KERBS, over=self.palette.concrete, avoid=self.palette.asphalt,
            picks=[(None, colour, None, 1) for colour in colours])
        self.program.run_pass(which)

    def near_pass(self, *, mark: int, reach: int, clear: int, paving: int,
                  spacing: int, chance: float, salt: int, picks, over,
                  avoid=native.ANY_SET) -> None:
        """On a grid, near a mark and not on it."""
        extra = ((int(mark) & 0xFF)
                 | ((int(reach) & 0xFF) << 8)
                 | ((int(clear) & 0xFF) << 16)
                 | ((int(paving) & 0xFF) << 24))
        which = self.job.add_pass(
            native.NEAR, over=over, avoid=avoid, spacing=max(1, int(spacing)),
            chance=chance, salt=salt, extra=extra, flags=native.PASS_BARE,
            picks=picks)
        self.program.run_pass(which)

    def soften_pass(self) -> None:
        """Dark grass under anything growing."""
        which = self.job.add_pass(
            native.SOFTEN,
            over=self.job.colour_set((C.MEDIUM_GRASS, C.LIGHT_GRASS)),
            picks=[(C.DARK_GRASS, None, None, 1)])
        self.program.run_pass(which)

    def spawn_pass(self) -> None:
        """The zombie spawn map. Crowds on the streets, the odd wanderer in a
        field, nobody on the water. The shares are the Python pass's: vanilla
        leaves nearly all of the map empty, and the sparsity matters as much as
        the ceiling."""
        at, count = self.job.spawn_bands([
            (self.palette.water, 0.0, 0.0, 0.0, 0x1001),
            (self.palette.hard, 0.40, 0.4, 1.0, 0x1002),
            (self.palette.yards, 0.15, 0.2, 0.5, 0x1003),
            (self.palette.grass, 0.03, 0.1, 0.2, 0x1004),
            (native.ANY_SET, 0.08, 0.1, 0.3, 0x1005),
        ])
        which = self.job.add_pass(native.SPAWN)
        self.program.run_pass(which)
        return at, count

    # -- handing the piece over -------------------------------------------

    def add(self, *, name: str, folder: str, seed: int, spawn_density: int,
            keep_cells=None, spawn_bands=(0, 0)) -> int:
        keep = (0, 0)
        if keep_cells:
            keep = self.job.keep_cells(sorted(keep_cells))
        return self.job.piece(
            self.program, name=name, folder=folder, seed=seed,
            grass=C.DARK_GRASS, nothing=C.VEG_NOTHING,
            open_biome=self.cover.open, spawn_density=spawn_density,
            spawn_scale=C.SPAWN_MAP_SCALE, spawn_bands=spawn_bands, keep=keep)
