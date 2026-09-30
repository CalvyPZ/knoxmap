"""Turn a Knoxify output folder into furnished .tbx buildings + a WorldEd project.

    python -m knoxbuild output/mytown

Reads <name>_info.json and <name>_buildings.geojson, reprojects every OSM
footprint with Knoxify's own Projector so the buildings line up with the
landscape BMP exactly, generates a floor plan for each, and writes:

    <dir>/buildings/<name>_NNN.tbx
    <dir>/<name>.pzw          WorldEd project with every building placed
    <dir>/<name>_placements.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import os
import sys
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait,
)

import numpy as np
from shapely.geometry import Polygon

from generator.renderer import Projector

import knoxstop

from .areas import AreaIndex, kind_for_category
from .edits import Edits, feature_id
from .fences import build_fences
from .footprint import place
from .layout import build_building
from .uses import USE_KEYS, is_hotel, uses_of
from .context import Context, style_fits
from .population import build_spawn_map, official_population, save_footprints
from .settings import PRESETS, Settings
from .tbx import render_tbx
from .world import Placement, project_cells, render_pzw
from . import worldmap

# OSM building tags that should get a commercial room mix rather than a house.
COMMERCIAL_TAGS = {
    "retail", "commercial", "industrial", "warehouse", "office", "shop",
    "supermarket", "school", "hospital", "church", "civic", "public",
    "government", "hotel", "service", "garage", "garages", "kiosk",
}


# building=* values that are not walled buildings at all. A carport or a petrol
# station canopy is a roof on posts; ruins and tanks have no rooms. Built as
# houses they would be solid boxes standing where the real place is open.
NOT_BUILDINGS = {
    "roof", "carport", "canopy", "ruins", "collapsed", "demolished", "no",
    "bridge", "storage_tank", "tank", "silo", "transformer_tower", "chimney",
    "tower", "grandstand", "construction",
}
# Outbuildings: one room, one storey, nobody living there.
SHED_VALUES = {
    "shed", "garage", "garages", "hut", "service", "toilets", "boathouse",
    "allotment_house", "bunker", "garbage_shed", "guardhouse", "gatehouse",
}
# An untagged building this small is a shed, a garage or a kiosk, not a home:
# 30 square metres of house would be one living room with a sofa filling it.
# Mappers tag the small houses that do exist (building=house), and those
# stay houses. Real square metres, so a map drawn at 4 m a tile does not turn
# its houses into sheds.
SHED_MAX_M2 = 30
HOUSE_TAGS = ("house", "detached", "bungalow", "semidetached_house", "cabin")
# An untagged building among tagged blocks of flats is taken for one when it
# is at least this big, in square metres; smaller ones are the shops and
# garages between them.
NEIGHBOUR_FLATS_M2 = 60
# Kinds whose height follows the tagged buildings around them. A school or a
# church is its own shape whatever the street is like.
FOLLOWS_NEIGHBOURS = {"house", "apartment", "shop", "civic", "restaurant"}
HOUSE_MAX_LEVELS = 3


# Named buildings worth a label on the paper map: the public ones people give
# directions by. Hotels, banks and offices are "civic" for their room plan,
# but a label on each buries the map in brand names.
NOTABLE_AMENITY = {"townhall", "police", "fire_station", "library", "courthouse",
                   "post_office", "community_centre", "theatre", "hospital",
                   "school", "university", "college", "place_of_worship",
                   "marketplace", "prison", "arts_centre"}


def is_notable(tags: dict, kind: str | None) -> bool:
    if kind in ("school", "church", "medical"):
        return True
    return (tags.get("amenity") in NOTABLE_AMENITY
            or tags.get("tourism") in ("museum", "attraction")
            or tags.get("historic") not in (None, "", "no")
            or tags.get("building") in ("government", "public", "civic", "townhall")
            or "wikidata" in tags or "wikipedia" in tags)


# Theatres are a hall a few storeys high.
THEATRE_MAX_LEVELS = 3

# OSM values that identify a building as something other than a house. Checked
# against the building/amenity/shop/leisure/tourism/healthcare tags in turn.
SPECIAL_BY_VALUE = {
    "school": "school", "kindergarten": "school", "college": "school",
    "university": "school", "childcare": "school", "library": "library",
    "church": "church", "chapel": "church", "cathedral": "church",
    "mosque": "church", "synagogue": "church", "temple": "church",
    "place_of_worship": "church",
    "restaurant": "restaurant", "fast_food": "restaurant", "cafe": "restaurant",
    "bar": "restaurant", "pub": "restaurant", "food_court": "restaurant",
    "retail": "shop", "commercial": "shop", "supermarket": "shop",
    "convenience": "shop", "mall": "shop", "kiosk": "shop",
    "department_store": "shop", "shop": "shop", "marketplace": "shop",
    "industrial": "industrial", "warehouse": "industrial",
    "factory": "industrial", "works": "industrial", "manufacture": "industrial",
    "barn": "barn", "farm": "barn", "farm_auxiliary": "barn",
    "greenhouse": "barn", "stable": "barn", "cowshed": "barn",
    "hospital": "medical", "clinic": "medical", "doctors": "medical",
    "dentist": "medical", "pharmacy": "medical", "veterinary": "medical",
    "apartments": "apartment", "residential": "apartment",
    "dormitory": "apartment", "terrace": "apartment",
    "civic": "civic", "public": "civic", "government": "civic",
    "townhall": "civic",
    # A police station, a library and a fire station have rooms of their own
    # in the game, with the loot that goes in them. As "civic" they came out
    # as an office block with a storeroom, which is what players reported
    # when a station they knew had nothing in it.
    "police": "police", "prison": "police", "fire_station": "fire",
    "hotel": "civic", "office": "civic", "courthouse": "civic",
    "museum": "civic", "bank": "civic", "post_office": "civic",
    "theatre": "civic", "cinema": "civic", "arts_centre": "civic",
    # Bases: barracks, armouries, the offices and stores of a military site.
    "military": "military", "barracks": "military", "bunker": "military",
    "armory": "military", "armoury": "military",
}

# Last resort when the tags say nothing useful but the name is obvious.
SPECIAL_BY_NAME = [
    ("school", "school"), ("academy", "school"), ("college", "school"),
    ("university", "school"), ("church", "church"), ("chapel", "church"),
    ("cathedral", "church"), ("hospital", "medical"), ("clinic", "medical"),
    ("pharmacy", "medical"), ("warehouse", "industrial"),
    ("factory", "industrial"), ("mill", "industrial"), ("plant", "industrial"),
    ("market", "shop"), ("mall", "shop"), ("store", "shop"), ("shop", "shop"),
    ("diner", "restaurant"), ("restaurant", "restaurant"), ("grill", "restaurant"),
    ("cafe", "restaurant"), ("bar ", "restaurant"), ("barn", "barn"),
    ("library", "library"), ("bibliot", "library"), ("kütüphane", "library"),
    ("police", "police"), ("polizei", "police"), ("polic", "police"),
    ("karakol", "police"), ("politie", "police"), ("gendarm", "police"),
    ("fire station", "fire"), ("feuerwehr", "fire"), ("itfaiye", "fire"),
    ("caserne de pompiers", "fire"), ("brandweer", "fire"),
    ("bank", "civic"), ("city hall", "civic"),
    ("apartment", "apartment"), ("apartmani", "apartment"),
    ("residence", "apartment"), ("towers", "apartment"), ("blok", "apartment"),
]

# Storeys, when OSM does not say. A town of nothing but bungalows reads as a
# film set - the skyline is what tells you whether you are downtown or in the
# suburbs, and every building here was one floor tall.
HOUSE_TWO_STOREY_CHANCE = 0.3
DEFAULT_LEVELS = {
    "industrial": (1, 1),
    "barn": (1, 1),
    "shed": (1, 1),
    "church": (1, 1),
    "apartment": (3, 5),
    "civic": (2, 3),
    "school": (2, 3),
    "medical": (2, 4),
    "shop": (1, 2),
    "restaurant": (1, 2),
    "military": (1, 2),
    "police": (1, 2),
    "library": (1, 2),
    "fire": (1, 1),
}

# Rows of units under one outline.
#
# OpenStreetMap maps a parade of shops, a strip mall or a terrace of houses as
# one polygon more often than not - the mapper drew the block, not the seven
# front doors in it - and built as one building it came out as a single shed
# with one door and one enormous room, whatever country the map was of. A
# footprint this much longer than it is deep, and no deeper than a shop unit
# is, is cut into units instead (footprint.split_row).
#
# Everything here is in metres, not tiles, so a map drawn at 2 m a tile splits
# the same row the same way as one drawn at half a metre.
ROW_KINDS = {None, "house", "apartment", "shop", "restaurant"}
ROW_RATIO = 2.2          # long side over short side
ROW_MIN_DEPTH_M = 5.0    # any shallower and a unit has no room in it
ROW_MAX_DEPTH_M = 32.0   # any deeper and it is a big shop, not a row
ROW_MIN_LENGTH_M = 22.0
# Frontages: a terraced house is narrower than a shop unit.
UNIT_FRONTAGE_M = {"house": 9.0, "apartment": 9.0}
SHOP_FRONTAGE_M = 13.0
# building=* values that say "row of houses" outright, in the countries whose
# mappers use them.
TERRACE_TAGS = {"terrace", "terraced", "semidetached_house", "row_house"}


# And a building too big to be one building whatever it is.
#
# A room may be no bigger than the game will fill (layout.MAX_ROOM_AREA), so a
# footprint of forty thousand tiles came out as five hundred rooms in one
# .tbx - more than any hand-made building in the game has, and slow to lay
# out. Past this it is cut into blocks that stand wall to wall, which is what
# a shopping centre or a works of that size is anyway.
BIG_BUILDING_M2 = 6000.0


def _cut_big(units: list, metres_per_tile: float) -> list:
    """Cut anything left that is still too big to be one building."""
    import math

    from .footprint import split_row

    side = max(8, int(math.sqrt(BIG_BUILDING_M2) / max(0.05, metres_per_tile)))
    limit = side * side
    out = list(units)
    for _ in range(4):
        if all(u.width * u.height <= limit for u in out):
            break
        nxt = []
        for u in out:
            nxt.extend(split_row(u, side) if u.width * u.height > limit else [u])
        if len(nxt) == len(out):
            break
        out = nxt
    return out


def row_units(fp, special: str | None, btag: str, n_uses: int,
              metres_per_tile: float):
    """One footprint per unit of a row, or the footprint as it stands."""
    from .footprint import split_row

    if special not in ROW_KINDS and btag not in TERRACE_TAGS:
        return _cut_big([fp], metres_per_tile)
    long_side = max(fp.width, fp.height) * metres_per_tile
    short_side = min(fp.width, fp.height) * metres_per_tile
    if short_side < ROW_MIN_DEPTH_M or long_side < ROW_MIN_LENGTH_M:
        return _cut_big([fp], metres_per_tile)
    terrace = btag in TERRACE_TAGS
    if not terrace:
        if short_side > ROW_MAX_DEPTH_M or long_side < short_side * ROW_RATIO:
            return _cut_big([fp], metres_per_tile)
    frontage = UNIT_FRONTAGE_M.get(special or "house", SHOP_FRONTAGE_M)
    if n_uses > 1:
        # The shops mapped inside it say how many units there really are;
        # keep the frontage sane so two shops in a long parade do not become
        # two units of fifty metres.
        frontage = min(max(frontage, long_side / n_uses), frontage * 2)
    return _cut_big(split_row(fp, max(5, int(round(frontage / metres_per_tile)))),
                    metres_per_tile)


# Kinds with no walls and windows of their own (knoxbuild/catalog.py
# SPECIAL_STYLES), dressed as the nearest kind that has: an army base is
# built like a works, a station or a library like any other public building.
STYLE_AS = {"military": "industrial", "fire": "industrial",
            "police": "civic", "library": "civic"}


# A footprint this big, with nothing but building=yes on it, is a block of
# flats rather than somebody's house.
#
# This has to be a guess because the data gives nothing else to go on: of 3079
# buildings in a real Turkish town, 3015 were tagged building=yes and not one
# carried building:levels. Taking those at face value produced 2902 detached
# houses and two apartment blocks - a suburb where a town should be.
def looks_like_apartment(tags: dict, area_tiles: int, rng,
                         settings: Settings) -> bool:
    """Whether an untagged building should be treated as a block of flats."""
    # Three storeys up is a block of flats and one storey is not. Two says
    # nothing on its own: a terrace is two and so is a bungalow with an attic,
    # so the footprint decides.
    measured = levels_from_tags(tags, settings)
    if measured is not None and measured >= 3:
        return True
    if measured == 1:
        return False
    if area_tiles < settings.apartment_footprint:
        return False
    if (tags.get("building") or "").lower() in ("house", "detached", "bungalow"):
        return False
    return rng.random() < settings.apartment_chance


# Metres of building per storey, for turning a height= into a floor count.
# OSM's own convention, and close enough to the game's floor spacing.
METRES_PER_LEVEL = 3.0


def _number(raw: str | None) -> float | None:
    """A leading number out of an OSM measurement, in metres.

    Values in the wild are "12", "12 m", "12.5", "12,5" and occasionally
    "40'" or "3;4" where two mappers disagreed. Feet are converted; a
    semicolon list takes the first entry.
    """
    if not raw:
        return None
    text = str(raw).strip().split(";")[0].strip().replace(",", ".")
    feet = text.endswith("'") or text.endswith("ft")
    text = text.rstrip("'").removesuffix("ft").removesuffix("m").strip()
    try:
        value = float(text)
    except ValueError:
        return None
    return value * 0.3048 if feet else value


def levels_from_tags(tags: dict, settings: Settings) -> int | None:
    """Storeys according to OSM, or None where it does not say.

    Preference order is how confident each source is. building:levels is a
    mapper counting floors, so it is taken as given. height is a measurement -
    of the roof ridge, not the top floor - so it is divided by a nominal storey
    and rounded down, which is why a 10 m building comes out as three floors
    rather than four.
    """
    for key in ("building:levels", "levels"):
        value = _number(tags.get(key))
        if value is not None and value >= 1:
            levels = int(value)
            # A roof level is habitable space in OSM's model, so an attic
            # conversion counts - but only when the mapper recorded one.
            roof = _number(tags.get("roof:levels"))
            if roof and roof >= 1:
                levels += int(roof)
            return max(1, min(settings.max_levels, levels))

    for key in ("height", "building:height", "est_height"):
        metres = _number(tags.get(key))
        if metres and metres > 0:
            # A building:part sitting on a podium starts partway up.
            base = _number(tags.get("min_height")) or 0.0
            usable = max(metres - base, metres * 0.5)
            return max(1, min(settings.max_levels,
                              int(usable // METRES_PER_LEVEL)))
    return None


def building_levels(tags: dict, kind: str | None, area_tiles: int,
                    rng, settings: Settings) -> tuple[int, bool]:
    """How many storeys this building gets, and whether OSM said so."""
    measured = levels_from_tags(tags, settings)
    if measured is not None:
        return measured, True
    if kind is None:
        # Most houses are a single storey under a pitched roof; an even split
        # of one and two storeys made a suburb a street of tall boxes.
        return (2 if rng.random() < HOUSE_TWO_STOREY_CHANCE else 1), False
    lo, hi = DEFAULT_LEVELS.get(kind or "", (1, 2))
    return rng.randint(min(lo, settings.max_levels),
                       min(hi, settings.max_levels)), False


def classify_building(tags: dict) -> str | None:
    """The special kind for this building, or None for an ordinary one."""
    for key in ("amenity", "shop", "healthcare", "leisure", "tourism",
                "office", "industrial", "craft", "military", "building"):
        value = (tags.get(key) or "").strip().lower()
        if not value:
            continue
        if value in SPECIAL_BY_VALUE:
            return SPECIAL_BY_VALUE[value]
        # shop=* with an unlisted value is still a shop.
        if key == "shop" and value not in ("no", "vacant"):
            return "shop"
        if key == "healthcare":
            return "medical"
        # military=* is the key mappers use for everything on a base -
        # armory, barracks, checkpoint, office, hangar, depot - and it was
        # not read at all, so an armoury came out as somebody's house.
        if key == "military" and value != "no":
            return "military"
    name = (tags.get("name") or "").lower()
    for needle, kind in SPECIAL_BY_NAME:
        if needle in name:
            return kind
    return None


# Kinds that shipped with a single style, so every church in a county was the
# same church. The exterior wall is taken from a house style instead, whole -
# the entry carries its own window and door tiles, so nothing is mixed.
MORE_WALLS = {
    "church": ("brick", "stucco", "render", "painted"),
    "industrial": ("brick", "panel", "stucco"),
    "barn": ("timber", "panel", "clapboard"),
}
# ...and the walls inside. Every variant of a kind shared one interior wall,
# so a whole county of schools was painted the same colour indoors however
# many materials the outside came in. The house styles carry nine different
# ones between them and each is taken whole, window and door tiles with it.
MORE_INSIDE = {
    "school": ("brick", "painted", "stucco"),
    "church": ("timber", "painted", "clapboard"),
    "civic": ("painted", "stucco", "panel"),
    "shop": ("painted", "panel"),
    "apartment": ("brick", "painted", "stucco", "render"),
    "industrial": ("panel", "stucco"),
    "barn": ("timber", "panel"),
    "medical": ("painted", "stucco"),
    "restaurant": ("painted", "panel"),
}
# Of the buildings in a block, how many keep the block's own style. A street
# built at one time is mostly one material with later infills between; every
# house in 110 tiles being identical is what read as an estate rather than a
# street.
BLOCK_SHARE = 55
# A public building with no wall style of its own borrows another kind's.
# Police and a library are civic. The call site still dresses a barracks or
# a fire station as industrial, via STYLE_AS, before this is consulted.
BORROWED_STYLE = {"police": "civic", "library": "civic", "fire": "civic",
                  "military": "civic"}


def wall_variants(kind: str) -> list[dict]:
    """Every style a building of this kind may be built in."""
    from . import catalog as C

    base = C.SPECIAL_STYLES[kind]
    out = list(getattr(C, "SPECIAL_STYLE_VARIANTS", {}).get(kind) or [base])
    houses = {s["name"]: s for s in C.HOUSE_STYLES}
    for name in MORE_WALLS.get(kind, ()):
        donor = houses.get(name)
        if not donor:
            continue
        variant = dict(base)
        variant["exterior"] = donor["exterior"]
        variant["name"] = f"{base['name']}_{name}"
        out.append(variant)
    inside = [(n, houses[n]["interior"]) for n in MORE_INSIDE.get(kind, ())
              if n in houses]
    if inside:
        grown = []
        for variant in out:
            grown.append(variant)
            for name, entry in inside:
                other = dict(variant)
                other["interior"] = entry
                other["name"] = f"{variant['name']}~{name}"
                grown.append(other)
        out = grown
    return out


def pick_style(kind: str | None, tile_x: int, tile_y: int, rng,
               settings: Settings, density: float = 0.0) -> dict:
    """Materials for one building: its own if special, else its block's.

    Only styles that suit how built-up the place is are in the running, so the
    old town gets render and brick and the log cabins stay in the countryside.
    """
    from . import catalog as C

    kind = BORROWED_STYLE.get(kind or "", kind)
    if kind and kind in C.SPECIAL_STYLES:
        variants = wall_variants(kind)
        # By the building's own position, so neighbours differ and a rebuild
        # picks the same again.
        return variants[(tile_x * 73856093 ^ tile_y * 19349663) % len(variants)]

    styles = [s for s in C.HOUSE_STYLES if style_fits(s["name"], density)] \
        or C.HOUSE_STYLES
    size = settings.neighbourhood_tiles
    block = (tile_x // size, tile_y // size)
    # Deterministic per block, so re-running gives the same town.
    idx = (block[0] * 73856093 ^ block[1] * 19349663) % len(styles)
    # ...and then per building, so a block is a street rather than one house
    # built over and over.
    own = (tile_x * 83492791 ^ tile_y * 297121507) & 0x7FFFFFFF
    if len(styles) > 1 and own % 100 >= BLOCK_SHARE:
        idx = (own // 100) % len(styles)
    if len(styles) > 1 and rng.random() < settings.style_oddity:
        idx = (idx + 1 + rng.randrange(len(styles) - 1)) % len(styles)
    return styles[idx]


def _catalog_style(name: str) -> dict | None:
    """The catalog entry called `name`, or None when nothing is called that.

    pick_style returns one of these dicts. An override names one; the first
    match is used, and a name the catalog does not have is left unused so a
    typo cannot fail the build.
    """
    from . import catalog as C

    for style in C.HOUSE_STYLES:
        if isinstance(style, dict) and style.get("name") == name:
            return style
    for style in C.SPECIAL_STYLES.values():
        if isinstance(style, dict) and style.get("name") == name:
            return style
    for group in getattr(C, "SPECIAL_STYLE_VARIANTS", {}).values():
        for style in group:
            if isinstance(style, dict) and style.get("name") == name:
                return style
    return None


# Strided kerbside rows per thread band, and parking lots per band. The
# random draw stays on one thread after the bands join, in row-major order.
_ZONE_ROW_BAND = 32
_ZONE_LOT_BAND = 8
_STALL_W, _STALL_H = 3, 5


def _empty_coords():
    return tuple(np.empty(0, dtype=np.int32) for _ in range(4))


def _run_bands(bands, fn, should_stop):
    """Run `fn` on each band. Stop is checked between bands, in band order.

    NumPy and Shapely release the GIL, so the bands share a thread pool.
    The caller draws any random numbers after this returns, on one thread.
    """
    if not bands:
        return []
    if len(bands) == 1:
        knoxstop.check(should_stop, "the buildings")
        return [fn(bands[0])]
    ex = ThreadPoolExecutor(max_workers=min(os.cpu_count() or 1, len(bands)),
                            thread_name_prefix="knox-zones")
    futures = [ex.submit(fn, band) for band in bands]
    out = []
    try:
        for fut in futures:
            knoxstop.check(should_stop, "the buildings")
            out.append(fut.result())
    except Exception:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        ex.shutdown(wait=True, cancel_futures=False)
    return out


def _sample_shifted(mask: np.ndarray, ys: np.ndarray, xs: np.ndarray,
                    dy: int, dx: int, height: int, width: int) -> np.ndarray:
    """`mask` at the strided candidates, moved by `(dy, dx)`. Off the bitmap is false."""
    out = np.zeros((ys.size, xs.size), dtype=bool)
    if ys.size == 0 or xs.size == 0:
        return out
    yy = ys + np.int32(dy)
    xx = xs + np.int32(dx)
    y_ok = (yy >= 0) & (yy < height)
    x_ok = (xx >= 0) & (xx < width)
    if not y_ok.any() or not x_ok.any():
        return out
    y_idx = np.flatnonzero(y_ok)
    x_idx = np.flatnonzero(x_ok)
    out[np.ix_(y_idx, x_idx)] = mask[yy[y_idx][:, None], xx[x_idx][None, :]]
    return out


def _lot_stall_coords(lot, hard: np.ndarray, height: int, width: int):
    """Stall origins that sit inside `lot` on a hard surface. No random draw."""
    x0, y0, x1, y1 = (int(v) for v in lot.bounds)
    across = (x1 - x0) >= (y1 - y0)
    sw = _STALL_W if across else _STALL_H
    sh = _STALL_H if across else _STALL_W
    lane = 17
    span_a = (y1 - y0) if across else (x1 - x0)
    span_b = (x1 - x0) if across else (y1 - y0)
    a_vals = np.arange(0, max(span_a, 0), lane, dtype=np.int32)
    b_vals = np.arange(0, max(span_b, 0), _STALL_W, dtype=np.int32)
    if a_vals.size == 0 or b_vals.size == 0:
        return _empty_coords()
    rows = np.array([0, _STALL_H], dtype=np.int32)
    bb, aa, rr = np.meshgrid(b_vals, a_vals, rows, indexing="ij")
    if across:
        xs = (x0 + bb).ravel()
        ys = (y0 + aa + rr).ravel()
    else:
        xs = (x0 + aa + rr).ravel()
        ys = (y0 + bb).ravel()
    ok = (xs >= 0) & (xs + sw <= width) & (ys >= 0) & (ys + sh <= height)
    if not ok.any():
        return _empty_coords()
    xs = xs[ok]
    ys = ys[ok]
    from shapely import box as sbox
    from shapely import contains

    boxes = sbox(xs, ys, xs + sw, ys + sh)
    inside = np.asarray(contains(lot, boxes), dtype=bool)
    if not inside.any():
        return _empty_coords()
    xs = xs[inside]
    ys = ys[inside]
    solid = (hard[ys, xs] & hard[ys + sh - 1, xs]
             & hard[ys, xs + sw - 1] & hard[ys + sh - 1, xs + sw - 1])
    if not solid.any():
        return _empty_coords()
    n = int(solid.sum())
    return (xs[solid], ys[solid],
            np.full(n, sw, dtype=np.int32), np.full(n, sh, dtype=np.int32))


def _concat_coords(parts):
    parts = [p for p in parts if p[0].size]
    if not parts:
        return _empty_coords()
    return tuple(np.concatenate([p[i] for p in parts]) for i in range(4))


def _drop_building_overlaps(xs, ys, sw, sh, placements):
    """One STRtree query for every stall against every placed building."""
    if xs.size == 0 or not placements:
        return xs, ys, sw, sh
    from shapely import STRtree
    from shapely import box as sbox

    stalls = np.atleast_1d(sbox(xs, ys, xs + sw, ys + sh))
    bx = np.fromiter((p.tile_x for p in placements), dtype=np.float64,
                     count=len(placements))
    by = np.fromiter((p.tile_y for p in placements), dtype=np.float64,
                     count=len(placements))
    bw = np.fromiter((p.width for p in placements), dtype=np.float64,
                     count=len(placements))
    bh = np.fromiter((p.height for p in placements), dtype=np.float64,
                     count=len(placements))
    tree = STRtree(np.atleast_1d(sbox(bx, by, bx + bw, by + bh)))
    hits = np.asarray(tree.query(stalls, predicate="intersects"))
    if hits.size == 0:
        return xs, ys, sw, sh
    # One stall queried as a scalar comes back as tree indices. Any hit drops it.
    if hits.ndim == 1:
        if xs.shape[0] == 1:
            return _empty_coords()
        stall_idx = hits
    else:
        stall_idx = hits[0]
    bad = np.zeros(xs.shape[0], dtype=bool)
    bad[np.asarray(stall_idx, dtype=np.int64)] = True
    keep = ~bad
    return xs[keep], ys[keep], sw[keep], sh[keep]


def _car_park_stalls(areas, placements, hard, height, width, should_stop):
    """Stall boxes on mapped car parks, row-major, with buildings removed."""
    lots = [shape for shape, props in areas._items
            if props.get("category") == "parking"]
    if not lots:
        return _empty_coords()
    bands = [lots[i:i + _ZONE_LOT_BAND] for i in range(0, len(lots), _ZONE_LOT_BAND)]

    def run(band):
        return _concat_coords([_lot_stall_coords(lot, hard, height, width)
                               for lot in band])

    xs, ys, sw, sh = _concat_coords(_run_bands(bands, run, should_stop))
    xs, ys, sw, sh = _drop_building_overlaps(xs, ys, sw, sh, placements)
    if xs.size == 0:
        return _empty_coords()
    order = np.lexsort((np.arange(xs.shape[0]), xs, ys))
    return xs[order], ys[order], sw[order], sh[order]


def _kerb_band(asphalt, height, width, y0, y1, xs, keep_clear):
    """Kerbside and in-tarmac candidates on strided rows `[y0, y1)`, no random draw."""
    ys = np.arange(y0, y1, 14, dtype=np.int32)
    empty_b = np.empty(0, dtype=bool)
    if ys.size == 0 or xs.size == 0:
        return (np.empty(0, np.int32), np.empty(0, np.int32), empty_b, empty_b)
    center = _sample_shifted(asphalt, ys, xs, 0, 0, height, width)
    near = (_sample_shifted(asphalt, ys, xs, 0, -4, height, width)
            & _sample_shifted(asphalt, ys, xs, 0, 4, height, width)
            & _sample_shifted(asphalt, ys, xs, -4, 0, height, width)
            & _sample_shifted(asphalt, ys, xs, 4, 0, height, width))
    far = (_sample_shifted(asphalt, ys, xs, 0, -8, height, width)
           & _sample_shifted(asphalt, ys, xs, 0, 8, height, width)
           & _sample_shifted(asphalt, ys, xs, -8, 0, height, width)
           & _sample_shifted(asphalt, ys, xs, 8, 0, height, width))
    # Deep inside a wide expanse of tarmac: no carriageway is 16 tiles
    # across, so this is a car park, and its middle is exactly where the
    # cars go.
    kerbside = center & ~near
    car_park = center & far
    keep = kerbside | car_park
    if not keep.any():
        return (np.empty(0, np.int32), np.empty(0, np.int32), empty_b, empty_b)
    iy, ix = np.nonzero(keep)
    y = ys[iy]
    x = xs[ix]
    vertical = (_sample_shifted(asphalt, ys, xs, -2, 0, height, width)
                | _sample_shifted(asphalt, ys, xs, 2, 0, height, width))
    vert = vertical[iy, ix]
    cp = car_park[iy, ix]
    sw = np.where(vert, _STALL_W, _STALL_H)
    sh = np.where(vert, _STALL_H, _STALL_W)
    if keep_clear:
        blocked = np.zeros(y.shape[0], dtype=bool)
        for fx, fy, fw, fh in keep_clear:
            blocked |= ((x < fx + fw) & (fx < x + sw)
                        & (y < fy + fh) & (fy < y + sh))
        if blocked.any():
            free = ~blocked
            y, x, vert, cp = y[free], x[free], vert[free], cp[free]
    return y, x, vert, cp


def _kerb_survivors(asphalt, height, width, keep_clear, should_stop):
    """Kerbside candidates on `[4:h-9:14, 4:w-9:14]`, row-major."""
    ys_all = np.arange(4, height - 9, 14, dtype=np.int32)
    xs = np.arange(4, width - 9, 14, dtype=np.int32)
    if ys_all.size == 0 or xs.size == 0:
        return (np.empty(0, np.int32), np.empty(0, np.int32),
                np.empty(0, dtype=bool), np.empty(0, dtype=bool))
    bands = []
    for i in range(0, ys_all.size, _ZONE_ROW_BAND):
        chunk = ys_all[i:i + _ZONE_ROW_BAND]
        bands.append((int(chunk[0]), int(chunk[-1]) + 14))

    def run(band):
        return _kerb_band(asphalt, height, width, band[0], band[1], xs, keep_clear)

    parts = [p for p in _run_bands(bands, run, should_stop) if p[0].size]
    if not parts:
        return (np.empty(0, np.int32), np.empty(0, np.int32),
                np.empty(0, dtype=bool), np.empty(0, dtype=bool))
    return (np.concatenate([p[0] for p in parts]),
            np.concatenate([p[1] for p in parts]),
            np.concatenate([p[2] for p in parts]),
            np.concatenate([p[3] for p in parts]))


def _claim_stalls(rng, zones, taken, xs, ys, sws, shs, draw, should_stop):
    """Draw once per stall that is not already in an 8x8 `taken` cell.

    `draw(i)` returns true when this survivor is kept. Callers walk `xs`/`ys`
    in row-major order so the shared random stream does not depend on threads.
    """
    from .world import Zone

    last_y = None
    for i, (x, y, sw, sh) in enumerate(zip(xs.tolist(), ys.tolist(),
                                           sws.tolist(), shs.tolist())):
        if y != last_y:
            knoxstop.check(should_stop, "the buildings")
            last_y = y
        key = (x // 8, y // 8)
        if key in taken:
            continue
        if not draw(i):
            continue
        taken.add(key)
        zones.append(Zone("ParkingStall", x, y, sw, sh))


def _junction_clear(out_dir: str, map_name: str) -> list:
    """Rectangles from the intersection pass that must not get a parked car.

    Missing on a map drawn before junctions were written. That map keeps the
    forecourts and nothing else, which is what it had before.
    """
    path = os.path.join(out_dir, f"{map_name}_junctions.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    rects = []
    for junction in data.get("junctions") or ():
        if not isinstance(junction, dict):
            continue
        for rect in junction.get("no_parking") or ():
            if isinstance(rect, (list, tuple)) and len(rect) == 4:
                try:
                    rects.append(tuple(int(v) for v in rect))
                except (TypeError, ValueError):
                    continue
    return rects


def _detect_zones(landscape_path: str, placements, rng_seed: int = 7,
                  settings: Settings | None = None, areas=None, drives=(),
                  keep_clear=(), should_stop=None):
    """Parking stalls along the roads, town zones over built-up ground.

    Without these the streets are bare: vehicles only ever spawn inside
    ParkingStall zones, and TownZone is what marks ground as urban. Vanilla
    Knox County ships 9694 of the former and 2715 of the latter.

    Stalls are found by scanning the landscape bitmap for asphalt that sits on
    a road *edge* — a stall in the middle of a carriageway would block it.
    """
    import random

    from PIL import Image

    from generator import pz_colors as C

    from .grids import colour_mask
    from .world import Zone

    # The three street shades plus the two pothole shades that weather them.
    # Kerbs and paving slabs are deliberately out of the kerbside test:
    # parking a car on the pavement is where stalls ended up once roads grew
    # kerbs, because any grey pixel in a wide range counted as road.
    asphalt_colours = (C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT,
                       C.DARK_POTHOLE, C.LIGHT_POTHOLE)
    hard_colours = asphalt_colours + (C.PALE_CONCRETE, C.PAVING)

    settings = settings or Settings()
    rng = random.Random(rng_seed)
    zones: list[Zone] = []
    taken: set[tuple[int, int]] = set()

    rgb = np.asarray(Image.open(landscape_path).convert("RGB"))
    height, width = rgb.shape[:2]
    asphalt = colour_mask(rgb, asphalt_colours)
    hard = colour_mask(rgb, hard_colours)

    # A car on most drives, as in Knox County. These draws come first so the
    # rest of the stream stays in one order whatever the thread pool does.
    for x, y, sw, sh in drives:
        if rng.random() <= min(1.0, 0.6 * settings.parking_density):
            taken.add((x // 8, y // 8))
            zones.append(Zone("ParkingStall", x, y, sw, sh))

    # Car parks OpenStreetMap maps as such: rows of stalls across the whole of
    # each, two rows back to back and then a lane, the way a real one is laid
    # out. Sampling the tarmac every 14 tiles found at most a stall or two in
    # anything smaller than a supermarket's, so most car parks had no cars.
    if areas is not None:
        xs, ys, sws, shs = _car_park_stalls(
            areas, placements, hard, height, width, should_stop)
        park_limit = min(1.0, 0.55 * settings.parking_density)

        def keep_park(_i, _limit=park_limit):
            return not (rng.random() > _limit)

        _claim_stalls(rng, zones, taken, xs, ys, sws, shs, keep_park, should_stop)

    ky, kx, vertical, car_park = _kerb_survivors(
        asphalt, height, width, keep_clear, should_stop)
    sws = np.where(vertical, _STALL_W, _STALL_H).astype(np.int32)
    shs = np.where(vertical, _STALL_H, _STALL_W).astype(np.int32)
    density = settings.parking_density

    def keep_kerb(i, _density=density, _car=car_park):
        chance = (0.75 if _car[i] else 0.45) * _density
        return not (rng.random() > chance)

    _claim_stalls(rng, zones, taken, kx, ky, sws, shs, keep_kerb, should_stop)

    # One town zone per cell that holds buildings, covering their extent with a
    # margin so gardens and the street frontage count as town too.
    by_cell: dict[tuple[int, int], list] = {}
    for p in placements:
        by_cell.setdefault((p.cell_x, p.cell_y), []).append(p)
    for (cx, cy), group in by_cell.items():
        x0 = max(0, min(p.tile_x for p in group) - 8)
        y0 = max(0, min(p.tile_y for p in group) - 8)
        x1 = min(width, max(p.tile_x + p.width for p in group) + 8)
        y1 = min(height, max(p.tile_y + p.height for p in group) + 8)
        # Clamp into the owning cell so the rectangle stays where it is written.
        x0 = max(x0, cx * 300)
        y0 = max(y0, cy * 300)
        x1 = min(x1, (cx + 1) * 300)
        y1 = min(y1, (cy + 1) * 300)
        if x1 - x0 > 4 and y1 - y0 > 4:
            zones.append(Zone("TownZone", x0, y0, x1 - x0, y1 - y0))

    return zones


def _ring_points(geom: dict) -> list[list[float]]:
    """Every [lon, lat] in a Polygon or MultiPolygon outer ring."""
    if geom["type"] == "Polygon":
        return geom["coordinates"][0]
    pts: list[list[float]] = []
    for poly in geom["coordinates"]:
        pts.extend(poly[0])
    return pts


STREET_LOOK_TILES = 40
RETAIL_DENSITY = 0.28


def _directional_distances(paved: np.ndarray):
    """How many steps N, S, W, E until paved ground, not counting the cell itself.

    Each array is `STREET_LOOK_TILES + 1` where nothing paved lies in that
    direction within the look distance. Built with a running index so a later
    side check is a handful of reads.
    """
    sent = STREET_LOOK_TILES + 1
    height, width = paved.shape
    rows = np.arange(height, dtype=np.int32)[:, None]
    cols = np.arange(width, dtype=np.int32)[None, :]

    def cap(dist, valid):
        bad = (~valid) | (dist > STREET_LOOK_TILES) | (dist < 1)
        return np.where(bad, sent, dist).astype(np.int16)

    north_idx = np.where(paved, rows, np.int32(-1))
    north_acc = np.maximum.accumulate(north_idx, axis=0)
    north_above = np.full((height, width), np.int32(-1), dtype=np.int32)
    if height > 1:
        north_above[1:] = north_acc[:-1]
    north = cap(rows - north_above, north_above >= 0)

    south_idx = np.where(paved, rows, np.int32(height))
    south_acc = np.minimum.accumulate(south_idx[::-1], axis=0)[::-1]
    south_below = np.full((height, width), np.int32(height), dtype=np.int32)
    if height > 1:
        south_below[:-1] = south_acc[1:]
    south = cap(south_below - rows, south_below < height)

    west_idx = np.where(paved, cols, np.int32(-1))
    west_acc = np.maximum.accumulate(west_idx, axis=1)
    west_left = np.full((height, width), np.int32(-1), dtype=np.int32)
    if width > 1:
        west_left[:, 1:] = west_acc[:, :-1]
    west = cap(cols - west_left, west_left >= 0)

    east_idx = np.where(paved, cols, np.int32(width))
    east_acc = np.minimum.accumulate(east_idx[:, ::-1], axis=1)[:, ::-1]
    east_right = np.full((height, width), np.int32(width), dtype=np.int32)
    if width > 1:
        east_right[:, :-1] = east_acc[:, 1:]
    east = cap(east_right - cols, east_right < width)
    return north, south, west, east


def _street_finder(out_dir: str, map_name: str):
    """A function giving the side ("N", "S", "W" or "E") of a footprint that
    faces the nearest pavement or road, or None if none is near."""
    from PIL import Image

    from generator import pz_colors as PC

    from .grids import colour_mask

    path = os.path.join(out_dir, f"{map_name}_ground_base.bmp")
    if not os.path.exists(path):
        path = os.path.join(out_dir, f"{map_name}.bmp")
    if not os.path.exists(path):
        return lambda *a: None
    ground = np.asarray(Image.open(path).convert("RGB"))
    paved = colour_mask(ground, (PC.PALE_CONCRETE, PC.MEDIUM_ASPHALT,
                                 PC.DARKEST_ASPHALT))
    height, width = paved.shape
    dist_n, dist_s, dist_w, dist_e = _directional_distances(paved)
    sent = STREET_LOOK_TILES + 1

    def nearest(dist, starts) -> int:
        best = sent
        for sx, sy in starts:
            if 0 <= sy < height and 0 <= sx < width:
                d = int(dist[sy, sx])
                if d < best:
                    best = d
        return best

    def side(x0: int, y0: int, w: int, h: int) -> str | None:
        # N, S, W, E — the same order the rays used to walk. A tie keeps the
        # earlier side, because the old test only replaced a strictly nearer hit.
        samples = (
            ("N", dist_n, [(x0 + w * f // 4, y0) for f in (1, 2, 3)]),
            ("S", dist_s, [(x0 + w * f // 4, y0 + h - 1) for f in (1, 2, 3)]),
            ("W", dist_w, [(x0, y0 + h * f // 4) for f in (1, 2, 3)]),
            ("E", dist_e, [(x0 + w - 1, y0 + h * f // 4) for f in (1, 2, 3)]),
        )
        best, best_d = None, sent
        for name, dist, starts in samples:
            d = nearest(dist, starts)
            if d < best_d:
                best, best_d = name, d
        return best

    return side


def _road_weight(out_dir: str, map_name: str, proj) -> np.ndarray | None:
    """2 on carriageway, 1 on pavement, 0 elsewhere, from the ground as the
    renderer drew it."""
    from PIL import Image

    from generator import pz_colors as C

    path = os.path.join(out_dir, f"{map_name}_ground_base.bmp")
    if not os.path.exists(path):
        path = os.path.join(out_dir, f"{map_name}.bmp")
    if not os.path.exists(path):
        return None
    ground = np.array(Image.open(path).convert("RGB"))[:proj.height, :proj.width]
    weight = np.zeros(ground.shape[:2], dtype=np.int8)
    for colour in (C.PALE_CONCRETE,):
        weight[np.all(ground == colour, axis=2)] = 1
    for colour in (C.MEDIUM_ASPHALT, C.DARKEST_ASPHALT, C.DARK_POTHOLE, C.LIGHT_POTHOLE):
        weight[np.all(ground == colour, axis=2)] = 2
    return weight


def _points_of_use(out_dir: str, info: dict, map_name: str, proj) -> dict:
    """Shops, restaurants, offices and the like mapped as points, from the
    map's OpenStreetMap download, as {(x // 16, y // 16): [(x, y, tags)]} in
    tiles. Empty for a download made before they were asked for."""
    from generator import osm
    bbox = info.get("bbox") or {}
    cache = os.path.join(out_dir, info["osm_cache"]) if info.get("osm_cache")         else osm.cache_path(out_dir, map_name)
    wanted = tuple(info["osm_bbox"]) if info.get("osm_bbox") else         (bbox.get("south"), bbox.get("west"), bbox.get("north"), bbox.get("east"))
    grid: dict = {}
    feats = osm.load_cache(cache, wanted) or []
    if info.get("straight_roads") and feats:
        # Where the renderer moved them to, with the roads straightened.
        from generator.octilinear import straighten_roads
        from generator.renderer import _is_polygon, classify
        straighten_roads(feats, proj, classify, _is_polygon)
    for feat in feats:
        if feat.kind != "node" or not any(k in feat.tags for k in USE_KEYS):
            continue
        lat, lon = feat.geometry[0]
        x, y = proj.to_px(lat, lon)
        grid.setdefault((int(x) // 16, int(y) // 16), []).append((x, y, feat.tags))
    return grid


class _UsePoints:
    """One STRtree of mapped shops, restaurants and offices."""

    def __init__(self, grid: dict):
        keys = []
        tags = []
        xy = []
        for cell in sorted(grid):
            for x, y, tag in grid[cell]:
                xy.append((float(x), float(y)))
                keys.append((x, y))
                tags.append(tag)
        self.keys = keys
        self.tags = tags
        self.tree = None
        if not keys:
            return
        from shapely import STRtree
        from shapely import points as shapely_points

        self.tree = STRtree(shapely_points(np.asarray(xy, dtype=np.float64)))


def _points_inside(index: _UsePoints, px: list, taken: set) -> list[dict]:
    """The tags of the points inside a footprint (or just outside its wall,
    where a mapper put the shop's entrance), each point used once.

    Query hits are sorted by tree index, which is cell order then the order
    the points were mapped, so overlapping footprints keep the same winner.
    """
    if index.tree is None:
        return []
    poly = Polygon(px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty:
        return []
    zone = poly.buffer(1.0)
    if zone.is_empty:
        return []
    found = index.tree.query(zone, predicate="contains")
    if len(found) == 0:
        return []
    out = []
    for i in np.sort(np.asarray(found, dtype=np.int64)).tolist():
        key = index.keys[i]
        if key not in taken:
            taken.add(key)
            out.append(index.tags[i])
    return out


def _party_walls(owner: np.ndarray, me: int, x0: int, y0: int, mask: np.ndarray,
                 levels: list[int]) -> dict:
    """{(x, y, "N" or "W"): storeys of the neighbour} for each outside wall
    of building `me` with another building's tile beyond it, in the
    building's own coordinates, as doors and windows name wall edges."""
    mask = np.asarray(mask, dtype=bool)
    h, w = mask.shape
    mh, mw = owner.shape
    level_of = np.asarray(levels, dtype=np.int32)
    out: dict = {}
    if h == 0 or w == 0 or level_of.size == 0:
        return out

    # Same edges as the four neighbour masks: a tile whose neighbour is not
    # in the footprint, including the side that falls off the array. One
    # walk, then one owner read per direction. max() makes visit order
    # irrelevant when one edge is seen twice.
    rows = mask.tolist()
    north_x: list[int] = []
    north_y: list[int] = []
    north_wx: list[int] = []
    north_wy: list[int] = []
    west_x: list[int] = []
    west_y: list[int] = []
    west_wx: list[int] = []
    west_wy: list[int] = []
    for y in range(h):
        row = rows[y]
        above = rows[y - 1] if y else None
        below = rows[y + 1] if y + 1 < h else None
        for x in range(w):
            if not row[x]:
                continue
            if above is None or not above[x]:
                north_x.append(x)
                north_y.append(y)
                north_wx.append(x0 + x)
                north_wy.append(y0 + y - 1)
            if below is None or not below[x]:
                north_x.append(x)
                north_y.append(y + 1)
                north_wx.append(x0 + x)
                north_wy.append(y0 + y + 1)
            if x == 0 or not row[x - 1]:
                west_x.append(x)
                west_y.append(y)
                west_wx.append(x0 + x - 1)
                west_wy.append(y0 + y)
            if x + 1 == w or not row[x + 1]:
                west_x.append(x + 1)
                west_y.append(y)
                west_wx.append(x0 + x + 1)
                west_wy.append(y0 + y)

    def commit(ex, ey, wx, wy, side):
        if not ex:
            return
        wx_a = np.asarray(wx, dtype=np.int32)
        wy_a = np.asarray(wy, dtype=np.int32)
        ok = (wx_a >= 0) & (wx_a < mw) & (wy_a >= 0) & (wy_a < mh)
        if not ok.any():
            return
        other = owner[wy_a[ok], wx_a[ok]]
        good = (other >= 0) & (other != me) & (other < level_of.shape[0])
        if not good.any():
            return
        kept = np.flatnonzero(ok)[good]
        storeys = level_of[other[good]]
        for i, storey in zip(kept.tolist(), storeys.tolist()):
            key = (ex[i], ey[i], side)
            out[key] = max(out.get(key, 0), storey)

    commit(north_x, north_y, north_wx, north_wy, "N")
    commit(west_x, west_y, west_wx, west_wy, "W")
    return out


def _kinds_at(areas, xs, ys, tiles) -> list:
    """`areas.kind_for` for many points, from one STRtree query.

    The smallest containing area wins. An equal area keeps the earlier hit,
    which is what walking the query results and taking a strictly smaller
    area did one point at a time. A zone region covering the point replaces
    that polygon, as AreaIndex.kind_for does.
    """
    n = len(xs)
    out: list = [None] * n
    if n == 0:
        return out
    tree = getattr(areas, "_tree", None)
    items = getattr(areas, "_items", None) or []
    if tree is not None and items:
        from shapely import points as shapely_points

        pts = shapely_points(np.asarray(xs, dtype=np.float64),
                             np.asarray(ys, dtype=np.float64))
        hits = np.asarray(tree.query(pts, predicate="contains"))
        if hits.size:
            if hits.ndim == 1 and n == 1:
                # A single query geometry comes back as tree indices only.
                t_idx = hits.astype(np.int64)
                p_idx = np.zeros(t_idx.shape[0], dtype=np.int64)
            else:
                p_idx = np.asarray(hits[0], dtype=np.int64)
                t_idx = np.asarray(hits[1], dtype=np.int64)
            item_area = np.fromiter((shape.area for shape, _props in items),
                                    dtype=np.float64, count=len(items))
            area = item_area[t_idx]
            order = np.lexsort((np.arange(t_idx.shape[0]), area, p_idx))
            p_sorted = p_idx[order]
            first = np.empty(order.shape[0], dtype=bool)
            first[0] = True
            if order.shape[0] > 1:
                first[1:] = p_sorted[1:] != p_sorted[:-1]
            tiles_a = np.asarray(tiles)
            for k in order[first]:
                pi = int(p_idx[k])
                _shape, props = items[int(t_idx[k])]
                out[pi] = kind_for_category(props.get("category"), int(tiles_a[pi]))
    edits = getattr(areas, "_edits", None)
    if edits is not None and any(region.get("action") == "zone" for region in edits.regions):
        for pi in range(n):
            category = edits.zone_at(float(xs[pi]), float(ys[pi]))
            if category is None:
                continue
            out[pi] = kind_for_category(category, int(tiles[pi]))
    return out


def _prefetch_thin_kinds(order, features, areas, m2_per_tile) -> dict:
    """Land-use kinds for the thinning pass, keyed by feature index."""
    xs, ys, tiles, idxs = [], [], [], []
    for _neg_area, i, px in order:
        tags = features[i].get("properties", {})
        if classify_building(tags) is not None:
            continue
        btag = (tags.get("building") or "").strip().lower()
        if not (-_neg_area * m2_per_tile > SHED_MAX_M2 and btag not in SHED_VALUES):
            continue
        centre = Polygon(px).centroid
        if centre.is_empty:
            continue
        xs.append(centre.x)
        ys.append(centre.y)
        tiles.append(int(-_neg_area))
        idxs.append(i)
    kinds = _kinds_at(areas, xs, ys, tiles)
    return dict(zip(idxs, kinds))


# Set in each layout process. The parent flips it the moment Stop is asked,
# and terminates anything that does not notice.
_WORKER_STOP = None


def _bind_worker_stop(event) -> None:
    global _WORKER_STOP
    _WORKER_STOP = event


def _init_layout_worker(event, import_lock) -> None:
    """Import layout, tbx and the catalog once per worker, then watch Stop.

    Workers inherit the window's output pipe. Filling that pipe waits forever,
    and nothing reads it while rooms are laid out. SciPy's libraries also wait
    forever if every worker loads them at the same moment, so one worker
    imports at a time.
    """
    _bind_worker_stop(event)
    devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = devnull
    sys.stderr = devnull
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    with import_lock:
        from . import catalog, layout, tbx
        catalog.HOUSE_STYLES
        layout.build_building
        tbx.render_tbx


def _worker_stopped() -> bool:
    event = _WORKER_STOP
    return event is not None and event.is_set()


def _make_one(job: tuple, should_stop=None) -> tuple:
    """Lay out one building and write its .tbx. Returns (storeys, rooms,
    furniture, error): a building that cannot be laid out is left out with the
    reason, instead of stopping the other two thousand."""
    stop = should_stop if should_stop is not None else _worker_stopped
    if stop():
        raise knoxstop.Stopped("the buildings")
    (w, h, levels, commercial, seed, kind, mask, settings, style, label, path, street, retail,
     uses, hotel, party) = job
    try:
        plan = build_building(w, h, levels=levels, commercial=commercial, seed=seed,
                              kind=kind, mask=mask, settings=settings, street=street,
                              retail=retail, uses=uses, hotel=hotel, party=party,
                              should_stop=stop)
        text = render_tbx(plan, label, style)
    except knoxstop.Stopped:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        return (0, 0, 0, f"{os.path.basename(path)} ({kind or 'house'}, {w}x{h}, "
                         f"{levels} storeys): {traceback.format_exc()}")
    if stop():
        raise knoxstop.Stopped("the buildings")
    partial = path + ".part"
    with open(partial, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(partial, path)
    return (len(plan.storeys), len(plan.rooms),
            sum(len(s.furniture) for s in plan.storeys), None)


def _make_batch(batch: list) -> list:
    """Several buildings in one process task. Stop is still per building."""
    return [_make_one(job) for job in batch]


# Below this many buildings, starting workers costs more than it saves.
PARALLEL_FROM = 60

# How long to wait for the first finished building. Packed workers have sat
# at no CPU and finished nothing; past this the same layout runs here.
_LAYOUT_STALL_S = 90

# Buildings handed to a worker in one task. Two keeps enough tasks in flight
# to occupy high-core-count CPUs while still amortising process IPC.
_LAYOUT_BATCH = 2

# Share of a mod's building pass. Used only to turn elapsed time into an ETA;
# the estimate corrects itself as each stage actually finishes.
_STAGE_WEIGHT = {
    "load": 4,
    "index": 8,
    "thin": 6,
    "categorise": 12,
    "walls": 4,
    "layout": 52,
    "paths": 3,
    "pumps": 2,
    "props": 2,
    "fences": 4,
    "finalise": 7,
    "write": 1,
}


def _blas_one_thread() -> None:
    """Keep numeric libraries from starting a thread team inside every worker.

    Set only when the variable is absent, so an explicit override stands.
    Workers inherit the environment at spawn, which is before they import NumPy.
    """
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        if name not in os.environ:
            os.environ[name] = "1"


def _worker_count() -> int:
    """How many buildings to lay out at once.

    KNOXBUILD_WORKERS=1 forces one worker. The room layout is pure Python, so
    it runs in processes (threads cannot share that work across cores). The
    footprint scan and the shared-wall pass run on threads. By default every
    logical processor is available; the environment override can reserve
    capacity on machines that need to stay responsive during a build.
    """
    raw = os.environ.get("KNOXBUILD_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return max(1, os.cpu_count() or 1)


def _pool_size(count: int) -> int:
    workers = _worker_count()
    if workers <= 1 or count < PARALLEL_FROM:
        return 1
    return max(1, min(workers, count))


def _trace(message: str) -> None:
    """One line in the log, written out immediately.

    Building a mod can sit inside one call for a long time. The line before
    that call is what shows where it is; the line after is how long it took.
    """
    try:
        import knoxlog
        knoxlog.setup("knoxbuild")
        knoxlog.log.info("knoxbuild %s", message)
        knoxlog.flush()
    except Exception:  # noqa: BLE001
        pass


class _Reporter:
    """One mod's stages, with a counter and an ETA for whichever is running."""

    def __init__(self, fn, should_stop):
        self.fn = fn
        self.should_stop = should_stop
        self.t0 = time.time()
        self.stage = ""
        self.done = 0
        self.total = 0
        self.processes = 1
        self.message = ""
        self._closed: set[str] = set()
        self._stage_t = time.time()
        self._stage_open = False

    def start(self, stage: str, total: int, message: str, processes: int = 1) -> None:
        self._log_stage()
        if self.stage and self.stage != stage:
            self._closed.add(self.stage)
        self.stage = stage
        self.done = 0
        self.total = max(0, int(total))
        self.processes = max(1, int(processes))
        self.message = message
        self._stage_t = time.time()
        self._stage_open = True
        if self.total <= 0:
            self._closed.add(stage)
        busy = min(self.processes, self.total) if self.total else 1
        self._send(busy)
        _trace(f"stage {stage} starting — {message} ({self.total})")

    def _log_stage(self) -> None:
        if not self._stage_open or not self.stage:
            return
        self._stage_open = False
        elapsed = time.time() - self._stage_t
        _trace(f"stage {self.stage} finished in {elapsed:.1f}s "
               f"({self.done}/{self.total})")

    def finish(self) -> None:
        self._log_stage()

    def __del__(self):
        try:
            self.finish()
        except Exception:  # noqa: BLE001
            pass

    def advance(self, done: int, process: int | None = None,
                processes: int | None = None) -> None:
        self.done = done
        if processes:
            self.processes = max(1, int(processes))
        self._send(self.processes if process is None else process)

    def update(self, message: str) -> None:
        """Change the description without resetting this stage's counter."""
        self.message = message
        self._send(min(self.processes, self.total - self.done) if self.total else 1)

    def _fraction(self) -> tuple[float, float]:
        total_w = float(sum(_STAGE_WEIGHT.values()))
        done_w = 0.0
        for name, weight in _STAGE_WEIGHT.items():
            if name == self.stage:
                if name in self._closed or self.total <= 0:
                    done_w += weight
                elif self.total:
                    done_w += weight * min(1.0, self.done / self.total)
                break
            if name in self._closed:
                done_w += weight
            else:
                break
        return done_w, total_w

    def _eta(self) -> float | None:
        done_w, total_w = self._fraction()
        if done_w <= 0:
            return None
        if done_w >= total_w:
            return 0
        elapsed = time.time() - self.t0
        return elapsed / done_w * (total_w - done_w)

    def _share(self) -> float:
        """How much of this mod is done, across every stage, from 0 to 1.

        A stage's own counter restarts at zero. This does not, so the window
        can draw one bar for the whole pass.
        """
        done_w, total_w = self._fraction()
        if total_w <= 0:
            return 0.0
        return max(0.0, min(1.0, done_w / total_w))

    def _send(self, process: int) -> None:
        if self.total and self.done >= self.total:
            self._closed.add(self.stage)
        if self.fn is not None:
            self.fn(self.stage, self.done, self.total, self.message,
                    max(0, int(process)), int(self.processes), self._eta(),
                    self._share())
        knoxstop.check(self.should_stop, "the buildings")


def _shutdown(pool, wait: bool, cancel: bool) -> None:
    try:
        pool.shutdown(wait=wait, cancel_futures=cancel)
    except TypeError:
        pool.shutdown(wait=wait)


def _alive(proc) -> bool:
    try:
        return bool(proc.is_alive())
    except Exception:  # noqa: BLE001
        return False


def _kill_processes(pool, event) -> None:
    """Stop is stuck if the pool is left to finish the queue. Cancel what has
    not started, then terminate whatever is still inside one building."""
    event.set()
    _shutdown(pool, wait=False, cancel=True)
    procs = list((getattr(pool, "_processes", None) or {}).values())
    deadline = time.time() + 0.5
    while time.time() < deadline and any(_alive(proc) for proc in procs):
        time.sleep(0.05)
    for proc in procs:
        if _alive(proc):
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass


def _run_ordered(fn, items, workers: int, should_stop, on_done, *, processes: bool):
    """Run `fn` over `items` on threads, results in input order.

    Room layout does not come through here. It uses `LayoutPool`, one process
    pool for the whole buildings run.
    """
    if processes:
        raise RuntimeError("layout processes use LayoutPool")
    items = list(items)
    n = len(items)
    if n == 0:
        return []
    pool_size = 1 if workers <= 1 or n < PARALLEL_FROM else min(workers, n)
    if pool_size <= 1:
        out = []
        for i, item in enumerate(items, 1):
            knoxstop.check(should_stop, "the buildings")
            out.append(fn(item))
            on_done(i, n, 1, 1)
        return out

    results = [None] * n
    stopped = False
    pool = ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="knoxbuild")
    try:
        stopped = _drain(pool, fn, items, results, pool_size, should_stop, on_done, None)
    finally:
        _shutdown(pool, wait=not stopped, cancel=stopped)
    if stopped:
        raise knoxstop.Stopped("the buildings")
    return results


class LayoutWorkersStalled(RuntimeError):
    """The process pool took jobs and finished none of them."""


def _drain(pool, fn, items, results, pool_size, should_stop, on_done, event,
           weights=None, total=None, on_state=None, stall_s=None) -> bool:
    """Fill `results` in order. Returns True when Stop was asked.

    `weights` is how many buildings each item stands for, so a batch of
    layouts still moves the counter by buildings rather than by tasks.
    `stall_s` raises LayoutWorkersStalled when that many seconds pass with
    nothing finished.
    """
    n = len(items)
    report_total = n if total is None else total
    in_flight: dict = {}
    completed: set[int] = set()
    next_i = 0
    done_n = 0
    started = time.monotonic()
    while next_i < n or in_flight:
        if stall_s is not None and done_n == 0 and (
                time.monotonic() - started >= stall_s):
            raise LayoutWorkersStalled()
        if (event is not None and event.is_set()) or (
                should_stop is not None and should_stop()):
            if event is not None:
                event.set()
            return True
        while next_i < n and len(in_flight) < pool_size:
            in_flight[pool.submit(fn, items[next_i])] = next_i
            next_i += 1
        if on_state is not None:
            on_state(set(in_flight.values()), completed)
        if not in_flight:
            break
        finished, _pending = wait(set(in_flight), timeout=0.25,
                                  return_when=FIRST_COMPLETED)
        for fut in finished:
            idx = in_flight.pop(fut)
            try:
                results[idx] = fut.result()
            except knoxstop.Stopped:
                if event is not None:
                    event.set()
                return True
            except Exception:
                if (event is not None and event.is_set()) or (
                        should_stop is not None and should_stop()):
                    return True
                raise
            completed.add(idx)
            done_n += 1 if weights is None else weights[idx]
            try:
                on_done(done_n, report_total,
                        len(in_flight) or (1 if done_n < report_total else pool_size),
                        pool_size)
            except knoxstop.Stopped:
                if event is not None:
                    event.set()
                return True
    return False


def _make_serial(jobs, should_stop, on_done, on_state=None) -> list:
    out = []
    n = len(jobs)
    for i, job in enumerate(jobs, 1):
        knoxstop.check(should_stop, "the buildings")
        if on_state is not None:
            on_state({i - 1}, set(range(i - 1)))
        out.append(_make_one(job, should_stop=should_stop))
        on_done(i, n, 1, 1)
        if on_state is not None:
            on_state(set(), set(range(i)))
    return out


class LayoutPool:
    """One process pool for every mod in a single buildings run.

    The buildings route creates it, passes it into each `build`, and closes
    it when the run ends. Stop kills the workers. A killed or broken pool is
    not reused; the next run constructs a new one.
    """

    def __init__(self, should_stop=None):
        self.should_stop = should_stop
        self.executor = None
        self.event = None
        self.workers = 0
        self.broken = False
        self._closed = False
        self._watch_done = None

    def ensure(self, workers: int) -> bool:
        if self.broken or self._closed:
            return False
        if self.executor is not None:
            return True
        _blas_one_thread()
        import multiprocessing

        self.workers = max(1, int(workers))
        self.event = multiprocessing.Event()
        self._import_lock = multiprocessing.Lock()
        self._watch_done = threading.Event()
        stop = self.should_stop
        event = self.event
        done = self._watch_done

        def watch() -> None:
            while not done.wait(0.2):
                try:
                    if stop is not None and stop():
                        event.set()
                        return
                except Exception:  # noqa: BLE001
                    return

        threading.Thread(target=watch, name="knoxbuild-stop", daemon=True).start()
        try:
            self.executor = ProcessPoolExecutor(
                max_workers=self.workers,
                initializer=_init_layout_worker,
                initargs=(self.event, self._import_lock),
            )
            _trace(f"layout pool ready ({self.workers} workers)")
        except OSError:
            self.broken = True
            self.executor = None
            if self._watch_done is not None:
                self._watch_done.set()
            return False
        return True

    def kill(self) -> None:
        """Cancel queued work and terminate workers. Do not submit again."""
        self.broken = True
        if self._watch_done is not None:
            self._watch_done.set()
        executor = self.executor
        self.executor = None
        if executor is None:
            return
        event = self.event
        if event is not None:
            event.set()
        _kill_processes(executor, event)

    def close(self) -> None:
        """Wait for workers to finish. A pool that was killed stays dead."""
        if self._closed and self.executor is None:
            return
        self._closed = True
        if self.broken or self.executor is None:
            if self.executor is not None:
                self.kill()
            elif self._watch_done is not None:
                self._watch_done.set()
            return
        if self._watch_done is not None:
            self._watch_done.set()
        executor = self.executor
        self.executor = None
        _shutdown(executor, wait=True, cancel=False)


def _layout_on_pool(pool: LayoutPool, jobs, should_stop, on_done,
                    on_state=None) -> list:
    batches = [jobs[i:i + _LAYOUT_BATCH]
               for i in range(0, len(jobs), _LAYOUT_BATCH)]
    weights = [len(batch) for batch in batches]
    results: list = [None] * len(batches)
    limit = max(1, min(pool.workers, len(batches)))

    def report_state(active_batches, completed_batches):
        if on_state is None:
            return
        active = {i for batch in active_batches
                  for i in range(batch * _LAYOUT_BATCH,
                                 min((batch + 1) * _LAYOUT_BATCH, len(jobs)))}
        completed = {i for batch in completed_batches
                     for i in range(batch * _LAYOUT_BATCH,
                                    min((batch + 1) * _LAYOUT_BATCH, len(jobs)))}
        on_state(active, completed)

    stopped = _drain(pool.executor, _make_batch, batches, results, limit,
                     should_stop, on_done, pool.event,
                     weights=weights, total=len(jobs), on_state=report_state,
                     stall_s=_LAYOUT_STALL_S)
    if stopped:
        raise knoxstop.Stopped("the buildings")
    out = []
    for part in results:
        out.extend(part)
    return out


def _make_all(jobs: list[tuple], should_stop=None, on_done=None,
              pool: LayoutPool | None = None, on_state=None) -> list:
    """Every building, in order. Room layout runs in processes; Stop cancels
    the ones not started and terminates the one in progress.

    `pool`, when given, is reused across mods. This function does not close
    a pool it did not create. A pool that breaks or is stopped is killed and
    left unusable.
    """
    from concurrent.futures.process import BrokenProcessPool

    if on_done is None:
        on_done = lambda done, total, busy, pool_n: None
    n = len(jobs)
    if n == 0 or _pool_size(n) <= 1:
        return _make_serial(jobs, should_stop, on_done, on_state)

    own = pool is None
    if own:
        pool = LayoutPool(should_stop)
    if not pool.ensure(_worker_count()):
        print("  (worker processes unavailable: could not start; "
              "building in one process)")
        if own:
            pool.close()
        return _make_serial(jobs, should_stop, on_done, on_state)
    try:
        return _layout_on_pool(pool, jobs, should_stop, on_done, on_state)
    except knoxstop.Stopped:
        pool.kill()
        raise
    except LayoutWorkersStalled:
        _trace("layout workers produced no rooms; laying out in this process")
        print("  (layout workers produced no rooms; laying out in this process)")
        pool.kill()
        return _make_serial(jobs, should_stop, on_done, on_state)
    except (BrokenProcessPool, OSError) as exc:
        print(f"  (worker processes unavailable: {exc}; building in one process)")
        pool.kill()
        return _make_serial(jobs, should_stop, on_done, on_state)
    finally:
        if own:
            pool.close()


def _ground_preview(out_dir: str, map_name: str):
    """The ground bitmap with trees on it, for the window. None if it is not drawn yet."""
    bmp = os.path.join(out_dir, f"{map_name}.bmp")
    if not os.path.exists(bmp):
        return None
    from PIL import Image

    from generator.renderer import _build_preview

    land = Image.open(bmp).convert("RGB")
    veg_path = os.path.join(out_dir, f"{map_name}_veg.bmp")
    if not os.path.exists(veg_path):
        return land
    return _build_preview(land, Image.open(veg_path).convert("RGB"))


def _saved_layouts(path: str) -> dict[str, tuple]:
    """Room counts from the last placements csv, keyed by the .tbx file name.

    A row that does not parse is left out, and that building is laid out
    again rather than copied from a number we cannot read.
    """
    if not os.path.isfile(path):
        return {}
    saved: dict[str, tuple] = {}
    try:
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                fname = row.get("file")
                if not fname:
                    continue
                try:
                    saved[fname] = (int(row["levels"]), int(row["rooms"]),
                                    int(row["furniture"]), None)
                except (KeyError, TypeError, ValueError):
                    continue
    except (OSError, csv.Error):
        return {}
    return saved


def build(out_dir: str, seed: int | None = None, min_size: int | None = None,
          max_size: int | None = None, settings: Settings | None = None,
          should_stop=None, on_progress=None, on_view=None,
          on_overlay=None, pool: LayoutPool | None = None,
          relayout_ids: set[str] | None = None,
          paper_map: bool = True, on_phase=None) -> int:
    """Generate every building for a rendered map.

    The explicit seed/min_size/max_size arguments are kept so the command line
    can override one value without composing a whole Settings.

    `relayout_ids` left unset lays every building out, after clearing the
    previous .tbx files. Passed a set, only those feature ids are laid out
    (plus any whose .tbx or placements row is already gone); the rest keep
    their files and their room counts are read back from the placements csv.
    """
    settings = settings or Settings()
    overrides = {k: v for k, v in (("seed", seed), ("min_size", min_size),
                                   ("max_size", max_size)) if v is not None}
    if overrides:
        settings = Settings.from_dict({**settings.to_dict(), **overrides})
    seed = settings.seed
    min_size = settings.min_size
    max_size = settings.max_size
    names = [f for f in os.listdir(out_dir) if f.endswith("_info.json")]
    if not names:
        print(f"no <name>_info.json in {out_dir}", file=sys.stderr)
        return 2
    map_name = names[0][: -len("_info.json")]

    def _step(text: str) -> float:
        _trace(f"{map_name}: {text}")
        if on_phase is not None:
            try:
                on_phase(text)
            except Exception:  # noqa: BLE001
                pass
        return time.time()

    started = _step("reading map info")
    info_path = os.path.join(out_dir, f"{map_name}_info.json")
    with open(info_path, encoding="utf-8") as f:
        info = json.load(f)
    _trace(f"{map_name}: map info {os.path.getsize(info_path)} bytes "
           f"in {time.time() - started:.1f}s")

    started = _step("reading building footprints")
    geo_path = os.path.join(out_dir, f"{map_name}_buildings.geojson")
    geo_bytes = os.path.getsize(geo_path) if os.path.isfile(geo_path) else 0
    with open(geo_path, encoding="utf-8") as f:
        geo = json.load(f)
    features = geo.get("features") or []
    geo["features"] = features
    _trace(f"{map_name}: {len(features)} footprints, {geo_bytes} bytes "
           f"in {time.time() - started:.1f}s")
    edits = Edits.load(out_dir, map_name)

    bbox = info["bbox"]
    grid = info.get("grid") or {}
    if grid.get("epsg"):
        proj = Projector.from_grid(grid)
    else:
        proj = Projector.build(bbox["south"], bbox["west"], bbox["north"],
                               bbox["east"], info["meters_per_tile"],
                               info.get("rotation") or 0.0)
    if (proj.width, proj.height) != (info["width_tiles"], info["height_tiles"]):
        _trace(f"{map_name}: projection {proj.width}x{proj.height} does not "
               f"match the bitmap {info['width_tiles']}x{info['height_tiles']}")
        print("projection does not match the rendered BMP - is this folder "
              "from a different Knoxify version?", file=sys.stderr)
        return 2
    edits.regions_px(proj)

    # Where this map stands in the world, beside any other KnoxMap map on
    # this PC rather than on top of it (knoxbuild/world.py). Everything
    # below - the paper map, the zones, the compiled cells - is written from
    # here on, so it is settled first.
    from .world import choose_origin, origin, set_origin
    forced = info.get("world_origin")
    if isinstance(forced, (list, tuple)) and len(forced) == 2:
        set_origin((int(forced[0]), int(forced[1])))
    else:
        set_origin(choose_origin(out_dir, info["cells_x"], info["cells_y"]))

    from generator.biomes import write_biome_maps
    started = _step("writing biome maps")
    biome_cells = write_biome_maps(out_dir, map_name, info, origin())
    _trace(f"{map_name}: {biome_cells} biome cells in {time.time() - started:.1f}s")

    # Record what this build actually used, whoever started it. Without this
    # a map generated from the command line cannot be reproduced, and the app
    # and the CLI disagree about what a folder was built with.
    try:
        with open(os.path.join(out_dir, "settings.json"), "w",
                  encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2)
    except OSError:
        pass

    bdir = os.path.join(out_dir, "buildings")
    os.makedirs(bdir, exist_ok=True)
    # Clear out the previous build. Building numbers follow the footprints, so
    # a rebuild does not overwrite the same set of files, and leftovers from an
    # earlier run sit in the folder looking like part of the map. A relayout
    # keeps the files it is not regenerating; deleting them would send every
    # building through layout, which is the slow part.
    if relayout_ids is None:
        for stale in os.listdir(bdir):
            if stale.endswith(".tbx") or stale.endswith(".part"):
                os.remove(os.path.join(bdir, stale))
    # WorldEd writes into these but will not create them: BMP to TMX fails with
    # "Could not open file for writing" if the export directory is absent.
    os.makedirs(os.path.join(out_dir, "tmx"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "lots"), exist_ok=True)

    import random as _random
    style_rng = _random.Random(seed ^ 0x5EED)

    placements: list[Placement] = []
    rows = []
    skipped = {"small": 0, "large": 0, "outside": 0, "taken": 0,
               "not a building": 0, "removed": 0}
    sheds = 0        # outbuildings given a single storage room
    from_near = 0    # storeys borrowed from tagged neighbours
    from_osm = 0   # buildings whose storey count came from the data
    from_area = 0  # buildings whose kind came from the land around them
    squared = 0    # buildings close enough to the grid to square up
    rows_split = 0   # rows of shops or terraces cut into their units
    units_made = 0   # and how many buildings those became

    reporter = _Reporter(on_progress, should_stop)
    features = geo["features"]
    reporter.start("load", 4, "Loading area data", 1)

    started = _step("loading area index")
    areas = AreaIndex.load(out_dir, map_name, proj)
    _trace(f"{map_name}: area index in {time.time() - started:.1f}s")
    reporter.advance(1, 1)
    reporter.update("Loading mapped uses")
    started = _step("loading mapped uses")
    points = _points_of_use(out_dir, info, map_name, proj)
    _trace(f"{map_name}: mapped uses in {time.time() - started:.1f}s")
    reporter.advance(2, 1)
    use_points = _UsePoints(points)
    points_taken: set = set()
    with_uses = 0
    # (x0, y0, mask, storeys, kind) for the population estimate.
    peopled: list[tuple[int, int, np.ndarray, int, str]] = []
    # Real outlines of the buildings placed, for the in-game paper map.
    outlines: list[tuple[list[tuple[float, float]], str, str]] = []
    _trace(f"{map_name}: occupancy grid {proj.width}x{proj.height}")
    occupied = np.zeros((proj.height, proj.width), dtype=bool)
    # ...and the ground covered by a lot rectangle rather than by a building's
    # own tiles, which is more ground whenever the footprint is not a rectangle
    # (footprint._clear_box). Lots may not overlap: WorldEd writes each of them
    # into the same cell layers, so where two cover a square only one survives.
    lots = np.zeros((proj.height, proj.width), dtype=bool)
    # Knox County roads: every building upright, and stood clear of the roads.
    straight = bool(settings.straight_roads or info.get("straight_roads"))
    reporter.update("Loading road clearance")
    started = _step("loading road clearance" if straight else "roads left as drawn")
    road_weight = _road_weight(out_dir, map_name, proj) if straight else None
    _trace(f"{map_name}: road clearance in {time.time() - started:.1f}s")
    reporter.advance(3, 1)

    # Biggest footprints claim their tiles first. Where two real buildings
    # share a wall, one of them has to give up that row of tiles, and it should
    # not be the town hall giving way to the shed behind it.
    order = []
    jobs: list[tuple] = []       # what each building needs to lay itself out
    reporter.update("Indexing nearby streets")
    started = _step("indexing nearby streets")
    street_side = _street_finder(out_dir, map_name)
    _trace(f"{map_name}: street index in {time.time() - started:.1f}s")
    reporter.advance(4, 1)
    stations: list[tuple] = []   # petrol stations, for their pumps
    gunshops: set[str] = set()   # and the buildings OSM says sell weapons
    canopies: list[list] = []    # and the canopies over their forecourts
    decided: list[tuple] = []    # and what the map needs to know about it
    surroundings: list[tuple[float, float, float, int | None]] = []

    def _measure(item):
        i, feat = item
        pts = _ring_points(feat.get("geometry") or {})
        if len(pts) < 3:
            return None
        rec = edits.building(feature_id(feat, i)) or {}
        if rec.get("deleted"):
            return ("removed",)
        tags = feat.get("properties") or {}
        px = [proj.to_px(lat, lon) for lon, lat in pts]
        offset = rec.get("offset")
        if offset:
            dx, dy = offset
            px = [(x + dx, y + dy) for x, y in px]
        if (tags.get("building") or "").strip().lower() in NOT_BUILDINGS:
            canopy = None
            if tags.get("amenity") == "fuel":
                # A petrol station's canopy: its forecourt, pumps underneath.
                canopy = px
            return ("skip", canopy)
        poly = Polygon(px)
        if not poly.is_valid:
            poly = poly.buffer(0)
        centre = poly.centroid
        spot = None
        if not centre.is_empty:
            spot = (centre.x, centre.y, poly.area, levels_from_tags(tags, settings))
        return ("keep", (-poly.area, i, px), spot)

    reporter.start("index", len(features), "Reading footprints",
                   _pool_size(len(features)))
    measured = _run_ordered(
        _measure, list(enumerate(features)), _worker_count(), should_stop,
        lambda done, total, busy, pool: reporter.advance(done, busy),
        processes=False)
    for rec in measured:
        if not rec:
            continue
        if rec[0] == "removed":
            skipped["removed"] += 1
            continue
        if rec[0] == "skip":
            skipped["not a building"] += 1
            if rec[1] is not None:
                canopies.append(rec[1])
            continue
        order.append(rec[1])
        if rec[2] is not None:
            surroundings.append(rec[2])
    order.sort()
    _trace(f"{map_name}: {len(order)} footprints kept, "
           f"{len(surroundings)} with a centre")
    # A town laid out for the game rather than copied from the survey: about
    # half the ordinary houses left out and what stays grown to a size worth
    # walking into, landmarks kept whatever and placed first so the ground a
    # police station needs is still free (knoxbuild/procedural.py). With
    # true_map on, nothing here runs and the order is the one it always was.
    thinning: dict = {}
    if not settings.true_map:
        from .procedural import HOUSING, Candidate, plan
        cands = []
        m2_per_tile = info["meters_per_tile"] ** 2
        thin_kinds = _prefetch_thin_kinds(order, geo["features"], areas, m2_per_tile)
        reporter.start("thin", len(order) + 1, "Categorising buildings", 1)
        zone_regions = any(region.get("action") == "zone" for region in edits.regions)
        for n, (_neg_area, i, px) in enumerate(order):
            feat = geo["features"][i]
            tags = feat.get("properties", {})
            kind = classify_building(tags)
            rec = edits.building(feature_id(feat, i)) or {}
            if "kind" in rec:
                kind = rec["kind"]
            elif kind is None:
                zone_cat = None
                if zone_regions:
                    centre = Polygon(px).centroid
                    if not centre.is_empty:
                        zone_cat = edits.zone_at(centre.x, centre.y)
                if zone_cat is not None:
                    kind = kind_for_category(zone_cat, int(-_neg_area))
                elif i in thin_kinds:
                    # What the land around it says, as the placement loop asks
                    # below. Without this the huts on an army base and the wings
                    # of a hospital look like untagged houses here and are
                    # thinned away, and the base loses the building the whole
                    # map's rifles were going to spawn in. Sheds are left out of
                    # it for the same reason the loop leaves them out: a garage
                    # on an industrial estate is a garage, not a works.
                    kind = thin_kinds[i]
            notable = is_notable(tags, kind)
            # Only housing can be thinned, so only housing has to be checked
            # for the shop or surgery mapped inside it; anything else is a
            # landmark already. A throwaway `taken` set, because the real one
            # is filled below, as each building claims its points for good.
            has_use = (not notable and kind in HOUSING
                       and bool(_points_inside(use_points, px, set())))
            cands.append(Candidate(i, px, -_neg_area, kind, notable, has_use))
            reporter.advance(n + 1, 1)
        started = _step(f"thinning {len(cands)} buildings")
        kept, thinning = plan(cands, max_size, should_stop)
        _trace(f"{map_name}: thinning kept {len(kept)} in {time.time() - started:.1f}s")
        order = [(-c.area, c.index, c.px) for c in kept]
        reporter.advance(reporter.total, 1)
    else:
        reporter.start("thin", 0, "Categorising buildings", 1)
        _trace(f"{map_name}: true map, every footprint stays")
    metres_per_tile = info["meters_per_tile"]
    started = _step(f"reading the neighbourhood of {len(surroundings)} buildings")
    context = Context(proj.width, proj.height, surroundings, metres_per_tile)
    _trace(f"{map_name}: neighbourhood in {time.time() - started:.1f}s")

    reporter.start("categorise", max(len(order) * 2, 1), "Categorising and placing", 1)
    # Plots, then the buildings that fill them, drawn on a copy of the ground
    # the window already has. The game bitmap stays without footprints; those
    # used to leave a brown fringe wherever a wall and the paint disagreed.
    view_image = None
    view_boxes: list[tuple] = []
    view_states: list[str] = []
    view_last = 0.0

    def show_view(stage: str, force: bool = False, reload: bool = False,
                  interval: float = 1.2) -> None:
        nonlocal view_image, view_last
        if on_view is None and on_overlay is None:
            return
        now = time.monotonic()
        if not force and now - view_last < interval:
            return
        if view_image is None or reload:
            fresh = _ground_preview(out_dir, map_name)
            if fresh is None:
                return
            view_image = fresh
        if view_boxes:
            from PIL import Image, ImageDraw
            colours = {
                "pending": (205, 48, 43),
                "active": (238, 132, 35),
                "complete": (62, 166, 82),
            }
            if on_view is not None:
                frame = view_image.copy()
                draw = ImageDraw.Draw(frame)
            else:
                frame = None
                draw = None
            # Finished buildings, in red, on their own layer so the window
            # can show or hide them without repainting the ground.
            overlay = None
            shade = None
            if on_overlay is not None:
                overlay = Image.new("RGBA", view_image.size, (0, 0, 0, 0))
                shade = ImageDraw.Draw(overlay)
            painted = False
            for (x, y, w, h), state in zip(view_boxes, view_states):
                box = [x, y, x + max(int(w), 1) - 1, y + max(int(h), 1) - 1]
                # Filled, not stroked: a one-tile outline disappears once the
                # window scales the bitmap down.
                if draw is not None:
                    draw.rectangle(box, fill=colours[state])
                if shade is not None and state == "complete":
                    painted = True
                    shade.rectangle(box, fill=(205, 48, 43, 170))
        else:
            frame = view_image.copy() if on_view is not None else None
            overlay = None
            painted = False
        view_last = now
        if on_view is not None and frame is not None:
            on_view(frame, stage)
        if on_overlay is not None and overlay is not None and painted:
            on_overlay(overlay)
    placed_rows = []
    kind_x, kind_y, kind_tiles = [], [], []
    for placed_so_far, (_neg_area, i, px) in enumerate(order):
        # Between buildings: nothing is written until the layouts run, so
        # stopping here drops the pass cleanly.
        reporter.advance(placed_so_far, 1)
        feat = geo["features"][i]
        fp, reason = place(px, occupied, min_side=min_size, max_side=max_size,
                           snap_degrees=45 if straight else settings.square_buildings,
                           avoid=road_weight, lots=lots)
        if fp is None:
            skipped[reason] += 1
            continue
        if fp.angle <= 8.0:
            squared += 1
        tags = feat.get("properties", {})
        # What is really in it: its own tags and the shops, restaurants and
        # offices mapped as points inside it. Claimed in footprint order,
        # before any style draw, so the taken set does not depend on threads.
        inside = [tags] + _points_inside(use_points, px, points_taken)
        kind_x.append(fp.x0 + fp.width / 2)
        kind_y.append(fp.y0 + fp.height / 2)
        kind_tiles.append(fp.tiles)
        placed_rows.append((i, px, fp, inside))
    started = _step(f"matching {len(placed_rows)} buildings to the land around them")
    area_kinds = _kinds_at(areas, kind_x, kind_y, kind_tiles)
    _trace(f"{map_name}: land match in {time.time() - started:.1f}s")
    reporter.update("Deciding rooms and storeys")
    for decided_n, ((i, px, fp, inside), around0) in enumerate(
            zip(placed_rows, area_kinds)):
        reporter.advance(len(order) + decided_n, 1)
        knoxstop.check(should_stop, "the buildings")
        x0, y0, w, h = fp.x0, fp.y0, fp.width, fp.height
        tags = inside[0]
        btag = (tags.get("building") or "").strip().lower()
        cx = x0 + w / 2
        cy = y0 + h / 2
        real_m2 = fp.tiles * metres_per_tile * metres_per_tile
        tagged = classify_building(tags)
        special = tagged
        uses = uses_of(inside)
        hotel = is_hotel(inside)
        if ("gasstore", "storage") in uses:
            stations.append((x0, y0, w, h, street_side(x0, y0, w, h)))
        offices_only = bool(uses) and all(u == ("office", "office") for u in uses)
        if uses:
            with_uses += 1
        if offices_only and special in (None, "house"):
            special = "civic"          # an office building
        elif uses and not offices_only and special in (None, "house", "shed"):
            special = "shop"           # flats over it, below, if it is tall
        if special is None and (btag in SHED_VALUES or (
                btag in ("", "yes") and real_m2 <= SHED_MAX_M2)):
            special = "shed"
            sheds += 1
        nearby = (context.neighbour_levels(cx, cy)
                  if levels_from_tags(tags, settings) is None else None)
        if special is None and nearby is not None and nearby >= 3 \
                and real_m2 >= NEIGHBOUR_FLATS_M2 and btag not in HOUSE_TAGS:
            # Among tagged blocks of flats, a big untagged building is one more.
            special = "apartment"
        fid = feature_id(geo["features"][i], i)
        rec = edits.building(fid) or {}
        land_kind = False
        if special is None:
            around = around0
            # A tall building in a shopping street is flats over shops; the
            # mapper's height says so more reliably than the zoning does.
            if around == "shop" and (levels_from_tags(tags, settings) or 0) >= 3:
                around = None
            if around:
                special = around
                from_area += 1
                land_kind = True
        if "kind" in rec:
            if land_kind:
                from_area -= 1
            special = rec["kind"]
        elif tagged is None:
            zone_cat = edits.zone_at(cx, cy)
            if zone_cat is not None:
                zkind = kind_for_category(zone_cat, fp.tiles)
                if zkind and not land_kind:
                    from_area += 1
                elif land_kind and not zkind:
                    from_area -= 1
                special = zkind
        commercial = special is not None or tags.get("building") in COMMERCIAL_TAGS
        if special is None and looks_like_apartment(tags, fp.tiles, style_rng,
                                                    settings):
            special = "apartment"
            commercial = True
        levels, measured = building_levels(tags, special, fp.tiles,
                                           style_rng, settings)
        counted_osm = False
        counted_near = False
        if measured:
            from_osm += 1
            counted_osm = True
        elif nearby is not None and (special or "house") in FOLLOWS_NEIGHBOURS:
            top = settings.max_levels
            if special is None:
                top = min(top, HOUSE_MAX_LEVELS)
            levels = max(1, min(top, int(round(nearby)) +
                                style_rng.choice((-1, 0, 0, 1))))
            from_near += 1
            counted_near = True
        if "kind" not in rec:
            if hotel:
                special = "apartment"      # its flats are guest rooms
            elif any(back == "theatre" for _front, back in uses):
                # A theatre is its auditorium behind a foyer on the street, and a
                # few storeys of hall, not a tower.
                special = "civic"
                levels = min(levels, THEATRE_MAX_LEVELS)
            elif special in ("shop", "restaurant") and uses and levels >= 3 and \
                    btag not in ("retail", "commercial", "supermarket", "shop", "kiosk"):
                # A pizza place in a five-storey building is flats over a pizza
                # place, not a five-storey pizza place - unless the building is
                # offices: a company inside it, or a name like "Americas Tower".
                offices = ("office", "office") in uses or btag == "office" or re.search(
                    r"\b(tower|building|plaza|center|centre|exchange)\b", tags.get("name") or "", re.I)
                special = "civic" if offices else "apartment"
        if "levels" in rec:
            # building_levels already drew, so the buildings after this one
            # stay on the same random stream.
            if counted_osm:
                from_osm -= 1
            if counted_near:
                from_near -= 1
            levels = int(rec["levels"])
            measured = False
        # Hotels are dressed as the city's big buildings, army bases as works,
        # and the public buildings that have no walls of their own as civic.
        style = pick_style(STYLE_AS.get("civic" if hotel else special, special),
                           x0, y0, style_rng, settings,
                           density=context.density(cx, cy))
        if "style" in rec:
            chosen = _catalog_style(rec["style"])
            if chosen is not None:
                style = chosen

        name = tags.get("name") or ""
        if "name" in rec:
            name = rec["name"]
            real_name = name
        else:
            real_name = name if is_notable(tags, special) else ""
        # A row of shops or a terrace of houses is one polygon here; built as
        # one building it is the "uber building" players reported. Each unit
        # becomes its own building, standing wall to wall with the next.
        units = row_units(fp, special, btag, len(uses), metres_per_tile)
        if len(units) > 1:
            rows_split += 1
            units_made += len(units)
        # 9176 * 0 is 0, so a building with no nudge keeps the seed it has
        # always had. A per-building seed and a reroll region both land here.
        if "seed" in rec:
            nudge = int(rec["seed"])
        else:
            nudge = edits.seed_nudge_at(cx, cy)
        for n, unit in enumerate(units):
            ux0, uy0 = unit.x0, unit.y0
            uw, uh = unit.width, unit.height
            umask = unit.mask
            # Each unit keeps one of the uses found in the whole row, in the
            # order they were found, so a parade of shops is a parade and not
            # seven copies of the same one.
            unit_uses = [uses[n % len(uses)]] if uses and len(units) > 1 else uses
            fname = f"{map_name}_{i:04d}.tbx" if len(units) == 1 else \
                f"{map_name}_{i:04d}_{n:02d}.tbx"
            label = (f"{name} {n + 1}" if name and len(units) > 1 else
                     name or f"{map_name} building {i}")
            if ("gunstore", "storage") in unit_uses:
                gunshops.add(fname)
            jobs.append((uw, uh, levels, commercial,
                         seed + i * 31 + n + 9176 * nudge, special, umask,
                         settings, style, label, os.path.join(bdir, fname),
                         street_side(ux0, uy0, uw, uh),
                         # Shops under flats where the town is built up.
                         context.density(cx, cy) >= RETAIL_DENSITY,
                         # What the ground floor really is, and a hotel's rooms.
                         unit_uses, hotel))
            outline = px if len(units) == 1 else [
                (ux0, uy0), (ux0 + uw, uy0), (ux0 + uw, uy0 + uh), (ux0, uy0 + uh)]
            decided.append((fname, label, ux0, uy0, uw, uh, unit, outline, special,
                            measured, commercial, style, umask,
                            real_name if n == 0 else "", fid))
            view_boxes.append((ux0, uy0, uw, uh))
            view_states.append("pending")
            show_view("Categorising and placing")
    reporter.advance(max(len(order) * 2, 1), 1)
    show_view("Categorising and placing", force=True)

    # Walls shared with the building next door, now every building has its
    # tiles: no window, shop window or door goes in one, up to the height of
    # the neighbour. Laid out on its own footprint, a terrace of shops had
    # glass shop fronts and windows looking into the next shop.
    owner = np.full((proj.height, proj.width), -1, dtype=np.int32)
    for j, d in enumerate(decided):
        dx0, dy0, dw, dh, dfp = d[2], d[3], d[4], d[5], d[6]
        view = owner[dy0:dy0 + dh, dx0:dx0 + dw]
        view[dfp.mask[:view.shape[0], :view.shape[1]]] = j
    storeys_of = [job[2] for job in jobs]

    def _one_party(j):
        d = decided[j]
        return _party_walls(owner, j, d[2], d[3], d[6].mask, storeys_of)

    reporter.start("walls", len(decided), "Matching shared walls",
                   _pool_size(len(decided)))
    parties = _run_ordered(
        _one_party, range(len(decided)), _worker_count(), should_stop,
        lambda done, total, busy, pool: reporter.advance(done, busy),
        processes=False)
    for j, party in enumerate(parties):
        # The feature id rides after the layout arguments. _make_one unpacks
        # a fixed tuple and never sees it; the .tbx name stays the feature index.
        jobs[j] = jobs[j] + (party, decided[j][-1])

    # Every decision above is made in order, from one random stream, so the
    # town comes out the same each time. What is left - laying out rooms and
    # writing the files - depends only on each building's own seed, so it runs
    # across processes. Stop cancels the queue and terminates a building that
    # is still being laid out, which is what used to leave the button on
    # Stopping until the whole town had been written.
    # A relayout skips a building whose file is already there and whose id was
    # not selected. Its room counts come back from the placements csv, matched
    # on the file name; a missing row is laid out anyway.
    saved_layouts = (_saved_layouts(os.path.join(out_dir, f"{map_name}_placements.csv"))
                     if relayout_ids is not None else {})
    layout_jobs = []
    layout_at = []
    reused: dict[int, tuple] = {}
    for j, job in enumerate(jobs):
        fid = job[16]
        fname = decided[j][0]
        if (relayout_ids is not None and fid not in relayout_ids
                and os.path.isfile(job[10]) and fname in saved_layouts):
            reused[j] = saved_layouts[fname]
            continue
        layout_jobs.append(job[:16])
        layout_at.append(j)
    for j in reused:
        view_states[j] = "complete"

    def show_layout_state(active, completed):
        active = set(active)
        completed = set(completed)
        for layout_i, decided_i in enumerate(layout_at):
            view_states[decided_i] = ("complete" if layout_i in completed else
                                      "active" if layout_i in active else
                                      "pending")
        # Encoding a progress PNG runs on the coordinator. During layout the
        # workers continue, but completed batches cannot be replaced until it
        # returns, so a slower cadence keeps the process pool fed.
        show_view("Laying out rooms", interval=3.0)

    reporter.start("layout", len(layout_jobs), "Laying out rooms",
                   _pool_size(len(layout_jobs)))
    show_view("Laying out rooms", force=True)
    fresh = _make_all(
        layout_jobs, should_stop=should_stop,
        on_done=lambda done, total, busy, pool_n: reporter.advance(
            done, busy, pool_n),
        pool=pool, on_state=show_layout_state)
    laid: list = [None] * len(jobs)
    for j, stats in reused.items():
        laid[j] = stats
    for j, stats in zip(layout_at, fresh):
        laid[j] = stats
    failed_buildings = []
    for (fname, label, x0, y0, w, h, fp, px, special, measured, commercial,
         style, mask, real_name, _fid), (storeys, rooms, furniture, error) in zip(decided, laid):
        if error:
            failed_buildings.append(error)
            continue
        p = Placement(f"buildings/{fname}", x0, y0, w, h)
        placements.append(p)
        peopled.append((x0, y0, fp.mask, storeys, special or "house"))
        outlines.append((px, special or "house", real_name))
        rows.append({
            "file": fname, "name": label,
            "tile_x": x0, "tile_y": y0, "width": w, "height": h,
            "cell_x": p.cell_x, "cell_y": p.cell_y,
            "offset_x": p.offset_x, "offset_y": p.offset_y,
            "levels": storeys,
            "levels_from_osm": int(measured),
            "rooms": rooms,
            "furniture": furniture,
            "commercial": int(commercial),
            "kind": special or "house",
            "style": style["name"],
            "shaped": int(not np.all(mask)),
            "angle": round(fp.angle, 1),
        })
    rows.sort(key=lambda r: r["file"])
    for j, stats in enumerate(laid):
        view_states[j] = "complete" if stats and not stats[3] else "pending"
    show_view("Laying out rooms", force=True)

    # One military rifle somewhere on the map, whatever this town turned out
    # to be: an army building, else the police station, else a gun shop, else
    # a house on the edge of town (knoxbuild/guns.py). A real place has no
    # checkpoints in it, so without this the game's rifles have nowhere at all
    # they could spawn.
    reporter.start("paths", 1, "Paths and yards", 1)
    from . import guns
    from .world import CELL_SIZE
    gun_cache = guns.write(
        out_dir, map_name,
        guns.choose(rows, gunshops) if settings.guaranteed_rifle else None,
        origin(), CELL_SIZE)
    from .yards import paint_paths
    drives: list = []
    porch_lights: list = []
    paths, yard_fences = paint_paths(out_dir, map_name, rows, occupied, drives,
                                     porch_lights)
    # The lights stand outside the houses, past the edge of their own .tbx.
    from .structures import pack_loose
    light_placements = pack_loose(bdir, map_name, "lights", porch_lights,
                                  on_top=True)
    show_view("Paths and yards", force=True, reload=True)
    reporter.advance(1, 1)

    # Pumps on a forecourt at each petrol station, including those mapped as
    # a point with no building of their own.
    reporter.start("pumps", 1, "Petrol pumps", 1)
    from .pumps import place_pumps
    loose_fuel = [(x, y) for group in points.values() for x, y, t in group
                  if t.get("amenity") == "fuel" and (x, y) not in points_taken]
    forecourts: list = []
    pump_placements, n_pumps = place_pumps(out_dir, map_name, bdir, occupied,
                                           stations, loose_fuel, forecourts, canopies)
    show_view("Petrol pumps", force=True, reload=True)
    reporter.advance(1, 1)

    # Headstones in the churchyards, stores on the army bases: land uses that
    # are neither a building nor a colour of ground.
    reporter.start("props", 1, "Cemetery graves and military stores", 1)
    from .props import place_props
    prop_placements, prop_counts = place_props(out_dir, map_name, bdir, occupied,
                                               areas, metres_per_tile,
                                               seed=seed + 5)
    show_view("Graves and stores", force=True, reload=True)
    reporter.advance(1, 1)

    reporter.start("fences", 1, "Fences", 1)
    fence_placements, fence_tiles = build_fences(out_dir, map_name, proj,
                                                 occupied, areas, bdir,
                                                 extra=yard_fences,
                                                 should_stop=should_stop)
    show_view("Fences", force=True)
    reporter.advance(1, 1)

    # These outputs only read the finished building decisions and write
    # separate files, so they can run together. Painting and placement above
    # stay ordered because they share the same occupancy and bitmap state.
    def finish_population():
        spawn_img, result = build_spawn_map(
            peopled, proj.width, proj.height, info["meters_per_tile"],
            os.path.join(out_dir, f"{map_name}.bmp"), settings)
        spawn_img.save(os.path.join(out_dir, f"{map_name}_ZombieSpawnMap.bmp"),
                       format="BMP")
        save_footprints(os.path.join(out_dir, f"{map_name}_footprints.npz"), peopled)
        result["official"] = [p for p in official_population(out_dir, map_name)
                              if p.get("inside")][:5]
        with open(os.path.join(out_dir, f"{map_name}_population.json"), "w",
                  encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return result

    def finish_paper():
        if paper_map:
            return worldmap.write(out_dir, map_name, proj, info, outlines,
                                  should_stop=should_stop, edits=edits)
        for filename in ("worldmap.xml", "worldmap.xml.bin", "streets.xml",
                         "worldmap-annotations.lua"):
            try:
                os.remove(os.path.join(out_dir, filename))
            except FileNotFoundError:
                pass
        return None

    def finish_zones():
        return _detect_zones(
            os.path.join(out_dir, f"{map_name}.bmp"), placements,
            settings=settings, areas=areas, drives=drives,
            keep_clear=list(forecourts) + _junction_clear(out_dir, map_name),
            should_stop=should_stop)

    def finish_structures():
        from .structures import build_structures
        return build_structures(out_dir, map_name, bdir)

    final_jobs = {
        "Population and zombie map": finish_population,
        "Paper map" if paper_map else "Paper map disabled": finish_paper,
        "Town and parking zones": finish_zones,
        "Bridges and monuments": finish_structures,
    }
    reporter.start("finalise", len(final_jobs), "Finalising map",
                   len(final_jobs))
    final_results = {}
    with ThreadPoolExecutor(max_workers=len(final_jobs),
                            thread_name_prefix="knox-final") as executor:
        pending = {executor.submit(fn): name for name, fn in final_jobs.items()}
        done_n = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                name = pending.pop(future)
                final_results[name] = future.result()
                done_n += 1
                reporter.message = f"Finished {name}"
                reporter.advance(done_n, len(pending))

    population = final_results["Population and zombie map"]
    paper_stats = final_results[
        "Paper map" if paper_map else "Paper map disabled"]
    zones = final_results["Town and parking zones"]
    structure_placements, raised = final_results["Bridges and monuments"]
    # Fences and structures go into the project alongside the buildings, but
    # not into town-zone detection: their lots can span a whole cell.
    placements = (placements + fence_placements + structure_placements +
                  pump_placements + prop_placements + light_placements)

    reporter.start("write", 1, "Writing the project", 1)
    from .mapped_cells import cells_to_build

    build_cells = cells_to_build(out_dir, map_name, info, placements, zones, proj)
    total_cells = int(info["cells_x"]) * int(info["cells_y"])
    omitted = total_cells - len(build_cells)
    pzw_path = os.path.join(out_dir, f"{map_name}.pzw")
    with open(pzw_path, "w", encoding="utf-8") as f:
        f.write(render_pzw(info["cells_x"], info["cells_y"],
                           f"{map_name}.bmp", placements, map_name,
                           project_dir=out_dir, zones=zones,
                           build_cells=build_cells))
    # Cells the game can fill itself are left out of the project. Compile
    # writes those back as empty map pointers so it can skip converting the
    # bitmap on every batch; this list is the cells that still need lots.
    with open(os.path.join(out_dir, "compile_cells.json"), "w", encoding="utf-8") as f:
        json.dump({"cells": [list(cell) for cell in sorted(project_cells(pzw_path))]}, f)

    csv_path = os.path.join(out_dir, f"{map_name}_placements.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wtr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                             ["file"])
        wtr.writeheader()
        wtr.writerows(rows)
    reporter.advance(1, 1)

    total_rooms = sum(r["rooms"] for r in rows)
    total_furn = sum(r["furniture"] for r in rows)
    print(f"footprints in geojson : {len(geo['features'])}")
    print(f"  shops, food, offices: {with_uses} buildings from the map's points and tags")
    print(f"  too small (<{min_size})     : {skipped['small']}")
    print(f"  too large (>{max_size})   : {skipped['large']}")
    print(f"  outside the map     : {skipped['outside']}")
    print(f"  swallowed by others : {skipped['taken']}")
    print(f"  not buildings       : {skipped['not a building']} (roofs, ruins, tanks)")
    print(f"  removed by edits    : {skipped['removed']}")
    import collections as _c
    kinds = _c.Counter(r["kind"] for r in rows)
    styles = _c.Counter(r["style"] for r in rows)
    print(f"buildings generated   : {len(rows)}")
    print(f"  kinds               : {dict(kinds)}")
    print(f"  styles              : {dict(styles)}")
    shaped = sum(r['shaped'] for r in rows)
    print(f"  on real footprint   : {len(rows) - squared} turned, "
          f"{squared} squared up ({shaped} with irregular outlines)")
    print(f"  kind from land use  : {from_area}")
    print(f"  sheds and garages   : {sheds}")
    print(f"  rows cut into units : {rows_split} rows -> {units_made} buildings")
    if thinning:
        print(f"  town laid out for PZ: {thinning['thinned']} houses left out, "
              f"{thinning['grown']} buildings grown, "
              f"{thinning['landmarks']} landmarks kept and placed first")
    print(f"  storeys from nearby : {from_near}")
    storeys = _c.Counter(r["levels"] for r in rows)
    print(f"  storeys             : {dict(sorted(storeys.items()))}")
    pct = 100.0 * from_osm / len(rows) if rows else 0.0
    print(f"  heights from OSM    : {from_osm} of {len(rows)} ({pct:.1f}%), "
          f"rest inferred from footprint")
    print(f"  rooms               : {total_rooms}")
    print(f"  furniture pieces    : {total_furn}")
    if gun_cache:
        print(f"guaranteed rifle      : {gun_cache['kind']} - "
              f"{gun_cache['name'] or gun_cache['building']}")
    print(f"world origin          : cell {origin()[0]},{origin()[1]}")
    if omitted:
        print(f"cells omitted         : {omitted} of {total_cells} "
              f"(empty or woodland — filled in by the game)")
    climate = (info.get("biome") or {}).get("climate") or "unknown"
    print(f"biome maps            : {biome_cells} cells ({climate})")
    print(f"wrote {pzw_path}")
    print(f"wrote {csv_path}")
    n_park = sum(1 for z in zones if z.kind == "ParkingStall")
    n_town = sum(1 for z in zones if z.kind == "TownZone")
    print(f"zones                 : {n_park} parking, {n_town} town")
    print(f"petrol stations       : {n_pumps} pumps at {len(stations)} stations"
          f", {len(canopies)} canopies and {len(loose_fuel)} points")
    print(f"front paths, yards    : {paths} houses, {len(yard_fences)} back yards")
    print(f"porch lights         : {len(porch_lights)} by front doors")
    print(f"graves, army stores  : {prop_counts['graves']} graves, "
          f"{prop_counts['dumps']} stacks of stores")
    print(f"fences                : {fence_tiles} fence tiles in "
          f"{len(fence_placements)} lots")
    print(f"bridges, monuments    : {raised['bridges']} bridges, "
          f"{raised['monuments']} monuments, {raised['tiles']} tiles in "
          f"{len(structure_placements)} lots")
    if paper_stats is None:
        print("paper map             : disabled")
    else:
        print(f"paper map             : {paper_stats['map_features']} features in "
              f"{paper_stats['map_cells']} cells, {paper_stats['streets']} named streets, "
              f"{paper_stats['labels']} labels")
    print(f"population            : {population['residents']:,} residents, "
          f"{population['daytime_occupants']:,} at work or school, "
          f"{population.get('on_the_street', 0):,} out on the street")
    print(f"zombie spawn map      : {population['share_with_zombies']:.1%} of chunks "
          f"populated, peak {population['peak_value']} (cap {population['horde_cap']}), "
          f"{population['chunks_at_cap']} chunks at the cap")
    # Not "place": that name is the footprint placer imported above, and a
    # loop variable of the same name makes it local to all of build().
    for town in population["official"]:
        print(f"  OSM says {town['name'] or town['place']}: "
              f"population {town['population']:,} ({town['place']})")
    print(f"wrote {len(rows)} .tbx files in {bdir}")
    if failed_buildings:
        print(f"left out {len(failed_buildings)} buildings that could not be laid out:")
        for error in failed_buildings[:20]:
            print(f"  {error}")
        try:
            import knoxlog
            for error in failed_buildings:
                knoxlog.log.warning("building left out: %s", error)
        except Exception:  # noqa: BLE001 - the printout is the record then
            pass
    reporter.finish()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Place names can be in any script, and a Windows console using a legacy
    # code page cannot print most of them - "OSM says Kadıköy" crashed a build
    # on cp1252. Print what it can and mark the rest, rather than dying.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("output_dir", help="a Knoxify output/<mapname> folder")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--min-size", type=int, default=None,
                    help="skip footprints smaller than this many tiles")
    ap.add_argument("--max-size", type=int, default=None,
                    help="skip footprints larger than this many tiles")
    ap.add_argument("--preset", choices=sorted(PRESETS),
                    help="suburb / town / city / rural")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override one setting, repeatable "
                         "(e.g. --set max_levels=12)")
    args = ap.parse_args(argv)

    # A saved settings.json in the map folder is the starting point, so the
    # command line and the app agree on what a given map was built with.
    out = args.output_dir
    saved = os.path.join(out, "settings.json")
    base = {}
    if os.path.exists(saved):
        try:
            with open(saved, encoding="utf-8") as f:
                base = json.load(f)
        except (OSError, ValueError):
            pass
    if args.preset:
        # A preset named on the command line replaces what was saved rather
        # than sitting underneath it. Merged the other way, every saved value
        # outranked the preset and --preset town quietly rebuilt a suburb.
        base = {"preset": args.preset}
    for item in args.set:
        key, _, value = item.partition("=")
        if not _:
            ap.error(f"--set wants KEY=VALUE, got {item!r}")
        base[key.strip()] = value.strip()
    settings = Settings.from_dict(base)
    return build(args.output_dir, seed=args.seed,
                 min_size=args.min_size, max_size=args.max_size,
                 settings=settings)


if __name__ == "__main__":
    raise SystemExit(main())
