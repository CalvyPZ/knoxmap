"""Assign Build 42 biome-map pixels from real-world land cover.

Project Zomboid reads one PNG per 256-tile cell:

    media/maps/<map>/maps/biomemap_<cellX>_<cellY>.png

The image is 256 by 256, one pixel per world tile. The red channel is the
biome and the green channel is the foraging zone. Both are looked up in
``biome_map_config`` (``BiomeMapConfig.lua``), so a pixel stores the same
index in R, G and B. The game then loads that row's biome, including its
sub-biomes (the grass, bush and sapling fill around a jumbo tree), and
registers the row's zone for foraging.

Cell coordinates are world tiles divided by 256, not the 300-tile source
cells the WorldEd project uses. A map placed at source cell 70,0 therefore
starts at world tile 21000, which is biome cell 82.

Pixels that are not in the config are left unused. Every index written here
is one the shipped config defines.
"""
from __future__ import annotations

import os

# Before numpy. OpenBLAS's worker pool can wait forever when drawing runs
# off the main thread.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# biome_map_config pixel -> (biome, zone). Ore comes along with the row.
WATER = 0            # zone Water
CLAY_SHORE = 59      # clay_shore, Forest — river and sea banks
CLAY_LAKE = 79       # clay_lake, Forest — lake and wetland margins
TRAILER = 102        # townhouse, TrailerPark
TOWN = 115           # townhouse, TownZone
FARM = 128           # farmmix_forest, Farm
FARMLAND = 141       # farmmix_forest, FarmLand
PH_FOREST = 153      # ph_forest (pine, dry bush), PHForest
PR_FOREST = 179      # pr_forest (open hardwood), PRForest
FARM_MIX = 192       # farmmix_forest, FarmMixForest
FARM_FOREST = 204    # farm_forest, FarmForest
BIRCH = 217          # birch_forest, BirchForest
BIRCH_MIX = 230      # birchmix_forest, BirchMixForest
ORGANIC = 243        # organic_forest, OrganicForest
DIRT = 254           # dirt, ForagingNav — paved or bare, spawns nothing
PRIMARY = 255        # primary_forest, DeepForest

# Colours for the generate-tab overlay. Each row is one shipped index:
# the biome and the foraging zone the game reads from that same pixel.
# Keep the hex values in static/js/app.js (BIOME_LEGEND) in step with these.
OVERLAY_ROWS = (
    (WATER, (46, 120, 186), "Water", "Water"),
    (CLAY_SHORE, (196, 168, 112), "Clay shore", "Forest"),
    (CLAY_LAKE, (120, 156, 138), "Clay lake", "Forest"),
    (TRAILER, (214, 156, 140), "Trailer park", "TrailerPark"),
    (TOWN, (176, 96, 84), "Town", "TownZone"),
    (FARM, (214, 176, 72), "Farm", "Farm"),
    (FARMLAND, (196, 196, 96), "Farmland", "FarmLand"),
    (PH_FOREST, (46, 110, 72), "Pine forest", "PHForest"),
    (PR_FOREST, (122, 156, 64), "Hardwood forest", "PRForest"),
    (FARM_MIX, (154, 176, 84), "Farm mix", "FarmMixForest"),
    (FARM_FOREST, (72, 140, 72), "Farm forest", "FarmForest"),
    (BIRCH, (168, 196, 92), "Birch forest", "BirchForest"),
    (BIRCH_MIX, (112, 168, 96), "Birch mix", "BirchMixForest"),
    (ORGANIC, (56, 120, 64), "Organic forest", "OrganicForest"),
    (DIRT, (140, 124, 104), "Bare ground", "ForagingNav"),
    (PRIMARY, (28, 72, 40), "Deep forest", "DeepForest"),
)

BIOME_CELL = 256
SOURCE_CELL = 300

# Paved, bare and open water. Woodland polygons must not paint over them.
# Town yards stay paintable so a mapped wood inside a neighbourhood wins.
_HARD = frozenset({WATER, DIRT, TRAILER})
# A clay bank stops at pavement, a beach and the water itself.
_SHORE_BLOCK = frozenset({WATER, DIRT, TOWN, TRAILER})

_PINE = frozenset({
    "pine", "spruce", "fir", "larch", "cedar", "conifer", "hemlock",
    "juniper", "cypress",
})
_BIRCH = frozenset({"birch", "aspen", "poplar", "alder"})
_LAKE_WATER = frozenset({"lake", "reservoir", "pond", "lagoon"})


def climate_band(lat: float, lon: float) -> str:
    """A coarse climate class for choosing which shipped biome is closest.

    The game's tree set is eastern North American. This only picks among
    those biomes: pine and dry scrub, birch, mixed birch, open hardwood,
    closed deciduous, or the deep mixed forest. A woodland's own leaf and
    species tags override the class.
    """
    if _arid(lat, lon):
        return "arid"
    if _mediterranean(lat, lon):
        return "mediterranean"
    a = abs(lat)
    if a >= 58:
        return "boreal"
    if a >= 46:
        return "cool"
    if a >= 32:
        return "temperate"
    if a >= 18:
        return "subtropical"
    return "tropical"


def open_pixel(climate: str) -> int:
    """Unmapped ground: the open country of that climate, not closed forest."""
    return {
        "boreal": BIRCH_MIX,
        "cool": BIRCH_MIX,
        "temperate": FARM_MIX,
        "mediterranean": PR_FOREST,
        "subtropical": FARM_MIX,
        "tropical": ORGANIC,
        "arid": PH_FOREST,
    }.get(climate, FARM_MIX)


def park_pixel(climate: str) -> int:
    """A park is open trees, not deep forest."""
    if climate in {"boreal", "cool"}:
        return BIRCH
    if climate in {"arid", "mediterranean"}:
        return PH_FOREST
    return PR_FOREST


def pixel_for(category: str, tags: dict | None, climate: str) -> int:
    """The biome-map index for one mapped category at this climate."""
    tags = tags or {}
    if category in {"water", "pool"}:
        return WATER
    if category == "sand":
        return DIRT
    if category == "wetland":
        return CLAY_LAKE
    if category in {
        "dirt", "dirt_path", "playground", "track", "military",
        "industrial", "parking", "railway", "pier", "road_track",
        "road_major", "road_medium", "road_minor", "road_service",
        "paved_path",
    }:
        return DIRT
    if category == "plaza":
        return TOWN
    if category == "airport":
        return open_pixel(climate)
    if category == "residential":
        if (tags.get("residential") or "") in {"trailer_park", "static_caravan", "caravan"}:
            return TRAILER
        return TOWN
    if category in {"commercial", "schoolyard", "hospital_grounds", "worship_grounds"}:
        return TOWN
    if category == "farmland":
        if tags.get("landuse") == "farmyard":
            return FARM
        return FARMLAND
    if category == "orchard":
        return FARM_FOREST
    if category == "cemetery":
        return FARM_MIX
    if category in {"grass", "sports"}:
        return open_pixel(climate)
    if category == "park":
        return park_pixel(climate)
    if category == "scrub":
        return scrub_pixel(tags, climate)
    if category == "forest":
        return forest_pixel(tags, climate)
    return open_pixel(climate)


def forest_pixel(tags: dict | None, climate: str) -> int:
    """Closed canopy. Species tags win; otherwise the climate class."""
    tags = tags or {}
    leaf, species = _leaf(tags)
    if species & _PINE or leaf == "needleleaved":
        return PH_FOREST
    if species & _BIRCH:
        return BIRCH if leaf != "mixed" and climate == "boreal" else BIRCH_MIX
    if climate in {"arid", "mediterranean"}:
        return PH_FOREST if leaf == "needleleaved" else PR_FOREST
    if climate == "boreal":
        return BIRCH if leaf != "mixed" else BIRCH_MIX
    if climate == "cool":
        return BIRCH_MIX
    if climate == "temperate":
        if leaf == "mixed":
            return BIRCH_MIX
        # Deciduous broadleaf (oak, beech, maple). Evergreen mixed in this
        # band is the mesophytic forest the primary biome was built for.
        if (tags.get("leaf_cycle") or "").lower() == "deciduous":
            return ORGANIC
        return PRIMARY
    if climate in {"subtropical", "tropical"}:
        return PRIMARY
    return ORGANIC


def scrub_pixel(tags: dict | None, climate: str) -> int:
    tags = tags or {}
    leaf, species = _leaf(tags)
    if species & _PINE or leaf == "needleleaved" or climate in {"arid", "mediterranean"}:
        return PH_FOREST
    if climate in {"boreal", "cool"} or species & _BIRCH:
        return BIRCH_MIX
    return PR_FOREST


def is_lake(tags: dict | None) -> bool:
    tags = tags or {}
    return (
        (tags.get("water") or "") in _LAKE_WATER
        or tags.get("landuse") in {"reservoir", "basin"}
    )


class Cover:
    """Per-tile biome index, painted in the same order as the landscape."""

    def __init__(self, size: tuple[int, int], lat: float, lon: float):
        self.climate = climate_band(lat, lon)
        self.open = open_pixel(self.climate)
        self.image = Image.new("L", size, self.open)
        self.draw = ImageDraw.Draw(self.image)
        self._lakes = Image.new("L", size, 0)
        self._lake_draw = ImageDraw.Draw(self._lakes)
        self._woods: list = []
        # Junction shaping paints thousands of corners. They share one array
        # (begin_mask_batch) instead of rebuilding this image on every corner.
        self._mask_batch = None
        self._mask_batch_used = False

    def stamp(self, category: str, tags: dict | None, rings,
              *, width: int | None = None, relation_rings=None) -> None:
        """One feature, using the same fill the landscape pass uses for it."""
        if category in {"water", "pool"}:
            # A river drawn from its centre line is a stroke, not a filled ring.
            if width is not None and relation_rings is None:
                self.line(rings, WATER, width)
                return
            self.water(rings, tags, relation_rings=relation_rings)
            return
        value = pixel_for(category, tags, self.climate)
        if relation_rings is not None:
            self.relation(relation_rings, value)
        elif width is not None:
            self.line(rings, value, width)
        else:
            self.polygon(rings, value)

    def polygon(self, rings, value: int) -> None:
        for ring in rings:
            if len(ring) >= 3:
                self.draw.polygon(ring, fill=value)

    def line(self, rings, value: int, width: int) -> None:
        w = max(1, int(width))
        for ring in rings:
            if len(ring) >= 2:
                self.draw.line(ring, fill=value, width=w)

    def rectangle(self, bounds, value: int) -> None:
        self.draw.rectangle(bounds, fill=value)

    def relation(self, role_rings, value: int) -> None:
        """Outer rings filled. Inner rings are left as whatever was under them."""
        rings = [(role, ring) for role, ring in role_rings if len(ring) >= 3]
        if not rings:
            return
        xs = [x for _r, ring in rings for x, _y in ring]
        ys = [y for _r, ring in rings for _x, y in ring]
        x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
        x1 = min(self.image.width, int(max(xs)) + 2)
        y1 = min(self.image.height, int(max(ys)) + 2)
        if x1 <= x0 or y1 <= y0:
            return
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        md = ImageDraw.Draw(mask)
        for role, ring in sorted(rings, key=lambda item: item[0] == "inner"):
            md.polygon([(x - x0, y - y0) for x, y in ring],
                       fill=0 if role == "inner" else 255)
        self.image.paste(Image.new("L", mask.size, value), (x0, y0), mask)
        self.draw = ImageDraw.Draw(self.image)

    def water(self, rings, tags: dict | None, *, relation_rings=None) -> None:
        if relation_rings is not None:
            self.relation(relation_rings, WATER)
        else:
            self.polygon(rings, WATER)
        if is_lake(tags):
            if relation_rings is not None:
                for role, ring in relation_rings:
                    if role != "inner" and len(ring) >= 3:
                        self._lake_draw.polygon(ring, fill=255)
            else:
                self.polygon_on(self._lake_draw, rings)

    def queue_wood(self, rings, tags: dict | None, *, scrub: bool = False,
                   radius: int = 0, holes=None) -> None:
        pixel = scrub_pixel(tags, self.climate) if scrub else forest_pixel(tags, self.climate)
        self._woods.append((pixel, rings, radius, holes or ()))

    def flush_woods(self) -> None:
        """Paint queued woodland onto ground that is not paved or water.

        Later woods overwrite earlier ones. A hole keeps whatever wood was
        under it, or the ground if there was none. Zero on the layer means
        "not a wood".
        """
        if not self._woods:
            return
        layer = Image.new("L", self.image.size, 0)
        draw = ImageDraw.Draw(layer)
        for pixel, rings, radius, holes in self._woods:
            if radius and rings and rings[0]:
                x, y = rings[0][0]
                r = max(1, radius)
                draw.ellipse((x - r, y - r, x + r, y + r), fill=pixel)
                continue
            snapshot = np.asarray(layer).copy() if holes else None
            for ring in rings:
                if len(ring) >= 3:
                    draw.polygon(ring, fill=pixel)
            if not holes:
                continue
            cut = Image.new("L", self.image.size, 0)
            cut_draw = ImageDraw.Draw(cut)
            for hole in holes:
                if len(hole) >= 3:
                    cut_draw.polygon(hole, fill=255)
            current = np.asarray(layer).copy()
            hole_px = np.asarray(cut) > 0
            current[hole_px] = snapshot[hole_px]
            layer = Image.fromarray(current, mode="L")
            draw = ImageDraw.Draw(layer)
        src = np.asarray(self.image).copy()
        wood = np.asarray(layer)
        soft = ~np.isin(src, list(_HARD))
        mark = (wood > 0) & soft
        src[mark] = wood[mark]
        self._replace(src)
        self._woods.clear()

    def apply_shores(self) -> None:
        """One tile of clay where land meets a river, a lake or the sea."""
        src = np.asarray(self.image)
        water = src == WATER
        if not water.any():
            return
        wet = np.asarray(Image.fromarray(water.astype(np.uint8) * 255).filter(
            ImageFilter.MaxFilter(3))) > 0
        shore = wet & ~water & ~np.isin(src, list(_SHORE_BLOCK))
        if not shore.any():
            return
        lakes = np.asarray(self._lakes) > 0
        if lakes.any():
            lake_edge = np.asarray(Image.fromarray(lakes.astype(np.uint8) * 255).filter(
                ImageFilter.MaxFilter(3))) > 0
        else:
            lake_edge = np.zeros(src.shape, dtype=bool)
        out = src.copy()
        out[shore & lake_edge] = CLAY_LAKE
        out[shore & ~lake_edge] = CLAY_SHORE
        self._replace(out)

    def paint_mask(self, mask: np.ndarray, value: int, y0: int) -> None:
        """Set rows ``y0`` onward where ``mask`` is true. Used by city paving."""
        src = np.asarray(self.image).copy()
        y1 = y0 + mask.shape[0]
        block = src[y0:y1]
        block[mask] = value
        self._replace(src)

    def begin_mask_batch(self) -> None:
        """Copy the biome index once so many small paints share it.

        ``paint_mask`` copies the whole map and builds a new image every
        call. Pair this with ``end_mask_batch``. City paving still calls
        ``paint_mask`` and is left on that path.
        """
        self._mask_batch = np.asarray(self.image).copy()
        self._mask_batch_used = False

    def paint_batch(self, mask: np.ndarray, value: int, x0: int, y0: int) -> None:
        """Set ``mask`` on the open batch at tile (x0, y0).

        True cells become ``value`` and false cells stay. That is the write
        ``paint_mask`` does after the window has been padded to the full
        image width: only the true cells inside the window change.
        """
        src = self._mask_batch
        block = src[y0:y0 + mask.shape[0], x0:x0 + mask.shape[1]]
        block[mask] = value
        self._mask_batch_used = True

    def end_mask_batch(self) -> None:
        """Rebuild the biome image from the batch, when any cell changed."""
        batch = self._mask_batch
        used = self._mask_batch_used
        self._mask_batch = None
        self._mask_batch_used = False
        if used and batch is not None:
            self._replace(batch)

    def apply_keep(self, keep) -> None:
        """Outside the drawn shape, countryside again. Water and the kept
        roads stay, matching the landscape clip."""
        src = np.asarray(self.image).copy()
        held = np.asarray(keep) > 0
        # The sea is not a mapped polygon, and the landscape clip keeps it
        # by colour. Hold those tiles too, or the coast becomes countryside.
        held |= src == WATER
        src[~held] = self.open
        self._replace(src)

    def _replace(self, array: np.ndarray) -> None:
        self.image = Image.fromarray(array.astype(np.uint8), mode="L")
        self.draw = ImageDraw.Draw(self.image)

    @staticmethod
    def polygon_on(draw, rings) -> None:
        for ring in rings:
            if len(ring) >= 3:
                draw.polygon(ring, fill=255)

    def save(self, path: str) -> None:
        self.image.save(path, format="PNG")

    def save_overlay(self, path: str) -> None:
        """A see-through colour copy of the biome index, for the map window."""
        index = np.asarray(self.image)
        rgba = np.zeros((index.shape[0], index.shape[1], 4), dtype=np.uint8)
        for value, rgb, _biome, _zone in OVERLAY_ROWS:
            mask = index == value
            rgba[mask, 0] = rgb[0]
            rgba[mask, 1] = rgb[1]
            rgba[mask, 2] = rgb[2]
            rgba[mask, 3] = 148
        Image.fromarray(rgba, mode="RGBA").save(path, format="PNG")


def overlay_legend() -> list[dict]:
    """Biome name, foraging zone and colour, in the order the overlay paints."""
    rows = []
    for _index, rgb, biome, zone in OVERLAY_ROWS:
        rows.append({
            "color": "#%02x%02x%02x" % rgb,
            "biome": biome,
            "zone": zone,
        })
    return rows


def write_biome_maps(out_dir: str, map_name: str, info: dict,
                     origin: tuple[int, int]) -> int:
    """Slice the local biome index into world-cell PNGs the game loads.

    ``origin`` is the map's place in 300-tile source cells. Returns how many
    cell images were written.
    """
    path = os.path.join(out_dir, f"{map_name}_biome.png")
    if not os.path.isfile(path):
        return 0
    # The file is one this program just wrote. PIL's default opener refuses
    # anything over about 179 million pixels, and a 15000-tile piece is
    # 225 million, so the building pass died before it could slice the biomes.
    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with Image.open(path) as im:
            cover = im.convert("L")
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    arr = np.asarray(cover)
    h, w = arr.shape
    ox = int(origin[0]) * SOURCE_CELL
    oy = int(origin[1]) * SOURCE_CELL
    # Tiles of a biome cell that fall outside the map stay the open country
    # of this place. A city-heavy map must not paint that fringe as town.
    climate = (info.get("biome") or {}).get("climate")
    if not climate:
        bbox = info.get("bbox") or {}
        try:
            climate = climate_band(
                (float(bbox["south"]) + float(bbox["north"])) / 2.0,
                (float(bbox["west"]) + float(bbox["east"])) / 2.0)
        except (KeyError, TypeError, ValueError):
            climate = "temperate"
    default = open_pixel(climate)
    maps = os.path.join(out_dir, "maps")
    os.makedirs(maps, exist_ok=True)
    for name in os.listdir(maps):
        if name.startswith("biomemap_") and name.endswith(".png"):
            os.remove(os.path.join(maps, name))

    cx0, cx1 = ox // BIOME_CELL, (ox + w - 1) // BIOME_CELL
    cy0, cy1 = oy // BIOME_CELL, (oy + h - 1) // BIOME_CELL
    written = 0
    for cy in range(cy0, cy1 + 1):
        for cx in range(cx0, cx1 + 1):
            tile = np.full((BIOME_CELL, BIOME_CELL), default, dtype=np.uint8)
            wx0, wy0 = cx * BIOME_CELL, cy * BIOME_CELL
            lx0, ly0 = max(0, wx0 - ox), max(0, wy0 - oy)
            lx1, ly1 = min(w, wx0 + BIOME_CELL - ox), min(h, wy0 + BIOME_CELL - oy)
            if lx1 <= lx0 or ly1 <= ly0:
                continue
            dx0, dy0 = lx0 + ox - wx0, ly0 + oy - wy0
            tile[dy0:dy0 + (ly1 - ly0), dx0:dx0 + (lx1 - lx0)] = arr[ly0:ly1, lx0:lx1]
            rgb = np.dstack((tile, tile, tile))
            Image.fromarray(rgb, "RGB").save(
                os.path.join(maps, f"biomemap_{cx}_{cy}.png"))
            written += 1
    return written


def _leaf(tags: dict) -> tuple[str, set[str]]:
    leaf = (tags.get("leaf_type") or "").lower()
    raw = tags.get("wood") or tags.get("trees") or ""
    species = {part for part in _split(raw)}
    return leaf, species


def _split(value: str) -> list[str]:
    text = value.lower().replace(",", " ").replace(";", " ")
    return [part for part in text.split() if part]


def _box(lat: float, lon: float, south: float, north: float,
         west: float, east: float) -> bool:
    return south <= lat <= north and west <= lon <= east


def _arid(lat: float, lon: float) -> bool:
    boxes = (
        (18, 32, -17, 35),     # Sahara
        (15, 32, 35, 60),      # Arabia
        (22, 30, 68, 76),      # Thar
        (38, 48, 90, 116),     # Gobi
        (-32, -18, 118, 145),  # Australian interior
        (-28, -18, 14, 26),    # Kalahari and Namib
        (-28, -18, -72, -68),  # Atacama
        (28, 38, -118, -106),  # Sonora and Chihuahua
        (-50, -38, -72, -64),  # Patagonian steppe
    )
    return any(_box(lat, lon, *b) for b in boxes)


def _mediterranean(lat: float, lon: float) -> bool:
    boxes = (
        (32, 44, -10, 36),     # Mediterranean basin
        (32, 42, -124, -116),  # California
        (-38, -30, -74, -70),  # central Chile
        (-35, -32, 18, 26),    # Cape
        (-35, -31, 115, 125),  # southwest Australia
    )
    return any(_box(lat, lon, *b) for b in boxes)
