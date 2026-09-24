"""The things that make a piece of land what it is, once the ground is painted.

Some land uses are not buildings and not terrain, so they fell through both:
a cemetery came out as a lawn with flowers on it, and an army base as a patch
of bare dirt. What is missing in each case is the furniture of the place -
the headstones in rows, the supply crates and drums behind the wire - and
that is what this puts there.

Everything here is driven by the land-use polygons the renderer wrote
(<map>_areas.geojson, read through knoxbuild/areas.py), which come from
OpenStreetMap tags used the world over: landuse=cemetery and amenity=grave_yard
anywhere there are graves, landuse=military and the military=* tags anywhere
there is a base. Spacing is worked out in metres, so a map drawn at two metres
a tile gets the same graveyard as one drawn at half a metre.

The props go into tile-only lots, one per map cell, the way the fuel pumps and
the bridges do (knoxbuild/structures.py render_tiles_tbx).
"""
from __future__ import annotations

import os
import random

import numpy as np
from PIL import Image

from generator import pz_colors as C

from .structures import CELL, render_tiles_tbx

# location_community_cemetary_01: headstones, wooden crosses and the angel on
# her plinth. Several shapes, because a churchyard where every stone is the
# same reads as a car park.
HEADSTONES = ["location_community_cemetary_01_8", "location_community_cemetary_01_9",
              "location_community_cemetary_01_11", "location_community_cemetary_01_12",
              "location_community_cemetary_01_13", "location_community_cemetary_01_16",
              "location_community_cemetary_01_17"]
CROSSES = ["location_community_cemetary_01_30", "location_community_cemetary_01_31",
           "location_community_cemetary_01_44", "location_community_cemetary_01_46"]
ANGELS = ["location_community_cemetary_01_19", "location_community_cemetary_01_20",
          "location_community_cemetary_01_21", "location_community_cemetary_01_22"]
CROSS_SHARE = 0.12         # of the graves, a wooden cross instead of a stone
ANGEL_SHARE = 0.006        # and once in a while a monument
GRAVE_SPACING_M = 2.0      # along a row
GRAVE_ROW_M = 3.0          # between rows
PATH_EVERY_ROWS = 8        # a way through, so it is not one solid block
MAX_GRAVES = 40000         # a whole county of churchyards, and no more

# location_military_generic_01: ammunition crates, fuel drums and lockers.
# Nothing with English writing on it - the same base has to read as a base in
# any country the map is of.
CRATES = ["location_military_generic_01_8", "location_military_generic_01_9",
          "location_military_generic_01_16", "location_military_generic_01_17",
          "location_military_generic_01_0", "location_military_generic_01_1"]
DRUMS = ["location_military_generic_01_14", "location_military_generic_01_15",
         "location_military_generic_01_22", "location_military_generic_01_23"]
DUMP_EVERY_M = 26.0        # a stack of stores about this often across the site
DUMP_MIN_M2 = 400          # sites smaller than this get nothing
MAX_DUMPS = 600

LAYER = "Furniture"
# Ground a prop never stands on: the carriageway, and water.
ROAD = {C.MEDIUM_ASPHALT, C.DARKEST_ASPHALT, C.DARK_POTHOLE, C.LIGHT_POTHOLE}
BLOCKED = ROAD | {C.WATER}


def _blocked_mask(ground: np.ndarray) -> np.ndarray:
    out = np.zeros(ground.shape[:2], dtype=bool)
    for colour in BLOCKED:
        out |= np.all(ground == colour, axis=2)
    return out


def _polygons(areas, category: str) -> list:
    """The land-use polygons of one category, in tiles."""
    return [shape for shape, props in getattr(areas, "_items", [])
            if (props.get("category") or "") == category and not shape.is_empty]


def place_props(out_dir: str, map_name: str, bdir: str, occupied: np.ndarray,
                areas, metres_per_tile: float,
                seed: int = 0x6FADE) -> tuple[list, dict]:
    """Headstones in the cemeteries and stores on the army bases.

    Returns (placements, counts). Tiles already taken by a building, a road or
    water are left alone, and anything put down is cleared of trees so a
    headstone does not grow out of a pine.
    """
    from shapely.geometry import Point

    from .world import Placement

    bmp = os.path.join(out_dir, f"{map_name}.bmp")
    counts = {"graves": 0, "dumps": 0}
    if not os.path.exists(bmp) or areas is None:
        return [], counts
    cemeteries = _polygons(areas, "cemetery")
    bases = _polygons(areas, "military")
    if not cemeteries and not bases:
        return [], counts

    ground = np.array(Image.open(bmp).convert("RGB"))
    height, width = ground.shape[:2]
    blocked = _blocked_mask(ground)
    blocked |= occupied[:height, :width]
    veg_path = os.path.join(out_dir, f"{map_name}_veg.bmp")
    veg = np.array(Image.open(veg_path).convert("RGB")) if os.path.exists(veg_path) else None
    rng = random.Random(seed)
    tiles: list[tuple[int, int, int, str, str]] = []

    def put(x: int, y: int, tile: str) -> bool:
        if not (0 <= x < width and 0 <= y < height) or blocked[y, x]:
            return False
        blocked[y, x] = True
        occupied[y, x] = True
        if veg is not None:
            veg[y, x] = C.VEG_NOTHING
        tiles.append((x, y, 0, LAYER, tile))
        return True

    def step(metres: float) -> int:
        return max(1, int(round(metres / max(0.05, metres_per_tile))))

    across, down = step(GRAVE_SPACING_M), step(GRAVE_ROW_M)
    for poly in cemeteries:
        if counts["graves"] >= MAX_GRAVES:
            break
        minx, miny, maxx, maxy = (int(v) for v in poly.bounds)
        row = 0
        for y in range(miny + down, maxy, down):
            row += 1
            if row % PATH_EVERY_ROWS == 0:
                continue                       # a way through the plots
            for x in range(minx + across, maxx, across):
                if counts["graves"] >= MAX_GRAVES:
                    break
                if not poly.contains(Point(x + 0.5, y + 0.5)):
                    continue
                roll = rng.random()
                if roll < ANGEL_SHARE:
                    tile = rng.choice(ANGELS)
                elif roll < ANGEL_SHARE + CROSS_SHARE:
                    tile = rng.choice(CROSSES)
                else:
                    tile = rng.choice(HEADSTONES)
                if put(x, y, tile):
                    counts["graves"] += 1

    gap = step(DUMP_EVERY_M)
    for poly in bases:
        if counts["dumps"] >= MAX_DUMPS:
            break
        if poly.area * metres_per_tile * metres_per_tile < DUMP_MIN_M2:
            continue
        minx, miny, maxx, maxy = (int(v) for v in poly.bounds)
        for y in range(miny + gap, maxy, gap):
            for x in range(minx + gap, maxx, gap):
                if counts["dumps"] >= MAX_DUMPS:
                    break
                if not poly.contains(Point(x + 0.5, y + 0.5)):
                    continue
                # A stack of stores: a short row of crates with a drum or two
                # at the end of it, as they are stacked on a real apron.
                run = rng.randint(2, 5)
                horizontal = rng.random() < 0.5
                placed = 0
                for k in range(run):
                    tile = rng.choice(DRUMS if k == run - 1 and rng.random() < 0.4
                                      else CRATES)
                    px = x + (k if horizontal else 0)
                    py = y + (0 if horizontal else k)
                    placed += bool(put(px, py, tile))
                if placed:
                    counts["dumps"] += 1

    if not tiles:
        return [], counts
    if veg is not None:
        Image.fromarray(veg).save(veg_path, format="BMP")

    by_cell: dict[tuple[int, int], list] = {}
    for t in tiles:
        by_cell.setdefault((t[0] // CELL, t[1] // CELL), []).append(t)
    placements = []
    for (cx, cy), group in sorted(by_cell.items()):
        gx0 = min(t[0] for t in group)
        gy0 = min(t[1] for t in group)
        gw = max(t[0] for t in group) - gx0 + 1
        gh = max(t[1] for t in group) - gy0 + 1
        fname = f"{map_name}_props_{cx}_{cy}.tbx"
        with open(os.path.join(bdir, fname), "w", encoding="utf-8") as f:
            f.write(render_tiles_tbx(gw, gh, [(x - gx0, y - gy0, z, layer, tile)
                                              for x, y, z, layer, tile in group]))
        placements.append(Placement(f"buildings/{fname}", gx0, gy0, gw, gh))
    return placements, counts
