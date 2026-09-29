"""The in-game paper map and its street names.

Project Zomboid draws the map a player opens with M from two files in the map
folder: worldmap.xml, polygons tagged with what they are, and streets.xml,
named centre lines. Without them a generated map opens as a blank sheet - the
one place in the game where the real town should be most recognisable.

Both are written the way the vanilla Muldraugh files are laid out (Build 42):

worldmap.xml   <world version="1.0"> of <cell x y> blocks, one per 300-tile
               cell in world cell numbers. Each <feature> is a polygon in
               that cell's own coordinates, 0-300, with a property the map
               style filters on: building=Residential/RetailAndCommercial/...,
               highway=primary/secondary/tertiary/trail, water=river,
               natural=forest. Everything is a polygon - roads included, as
               strips as wide as the road.
streets.xml    <streets version="1"> of <street name width> with <point x y>
               in absolute world tiles.
worldmap-annotations.lua
               a function adding text labels through the map's symbols API,
               in absolute world tiles: the town's name, its parks and
               squares, rivers and lakes, and its best-known buildings.
"""
from __future__ import annotations

import json
import os
from xml.sax.saxutils import quoteattr

import knoxstop
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Polygon, box
from shapely.ops import linemerge, unary_union

from generator import osm
from generator.renderer import (THROUGH_ROADS, _is_polygon, _way_width_m,
                                sea_polygons, shape_px)

from .world import origin

CELL = 300
# Cells clipped in one shapely.intersection call, and how many of those
# calls share a wave. Stop is checked between waves. A heavy wave runs on
# threads; results are applied in wave order, which is feature order.
_CELL_BATCH = 128
_WAVE = 32
_THREAD_CELLS = 48

# What each generated building kind is on the map. These are the categories
# the vanilla style colours; a shed is drawn as a plain building.
BUILDING_VALUE = {
    "house": "Residential", "apartment": "Residential",
    "shop": "RetailAndCommercial", "restaurant": "RestaurantsAndEntertainment",
    "school": "CommunityServices", "civic": "CommunityServices",
    "church": "CommunityServices", "medical": "Medical",
    "industrial": "Industrial", "barn": "Industrial", "shed": "yes",
}
# Road classes as the map style knows them. It has no class for a service
# lane, so those draw as the smallest street.
HIGHWAY_VALUE = {
    "road_major": "primary", "road_medium": "secondary",
    "road_minor": "tertiary", "road_service": "tertiary",
    "pedestrian": "tertiary",
    "dirt_path": "trail", "paved_path": "trail", "road_track": "trail",
}
RAIL_WIDTH_M = 4.0
AREA_VALUE = {"water": ("water", "river"), "pool": ("water", "river"),
              "forest": ("natural", "forest")}
# Streets worth a name on the map. Footpaths carry names too, but labelling
# every alley and pavement buries the streets people navigate by.
NAMED_CLASSES = {"road_major", "road_medium", "road_minor", "road_service",
                 "road_track", "pedestrian"}
SIMPLIFY = 0.5     # tiles; the map is drawn far smaller than one tile per pixel


# Labels: the style layer each goes on and how large, as the vanilla
# Muldraugh annotations use them.
TOWN_SCALE = {"city": 4.5, "town": 3.5, "village": 2.5, "suburb": 2.0,
              "quarter": 1.5, "neighbourhood": 1.2, "hamlet": 1.2}
PLACE_SCALE = 0.6
BUILDING_SCALE = 0.6
# Only buildings people would give directions by, and not every one of them:
# a label per corner shop buries the map.
LABELLED_KINDS = {"school", "church", "medical", "civic", "industrial", "shop",
                  "restaurant", "house", "apartment"}
MIN_LABELLED_TILES = 150
MAX_BUILDING_LABELS = 40
MIN_PLACE_TILES = 400
PLACE_CATEGORIES = {"park", "plaza", "cemetery", "sports", "grass"}


def _named_grounds(cat: str, feat) -> bool:
    """A named area worth a label: a park, an airfield, a railway yard."""
    if cat in PLACE_CATEGORIES:
        return True
    if cat == "airport":
        return osm.is_airport_ground(feat.tags)
    if cat == "railway":
        return osm.is_rail_area(feat.tags)
    return False


def _osm_feature_id(feat) -> str | None:
    """Way and relation ids, the same scheme as generator.renderer.feature_id.

    A node has no per-way street override. A feature with no OSM id is skipped
    rather than guessed, so a rename cannot land on the wrong road.
    """
    osm_id = getattr(feat, "osm_id", None)
    if not osm_id:
        return None
    kind = getattr(feat, "kind", "")
    if kind == "relation":
        return "r" + str(osm_id)
    if kind == "way":
        return "w" + str(osm_id)
    return None


def write(out_dir: str, map_name: str, proj, info: dict,
          buildings: list[tuple[list[tuple[float, float]], str, str]],
          should_stop=None, edits=None) -> dict:
    """Write worldmap.xml and streets.xml into `out_dir`. Returns counts.

    `buildings` holds each placed building's projected outline and its kind.
    Roads, water and woodland come from the OSM download cached beside the
    map; a map folder without one still gets its buildings.

    `edits`, when given, renames and hides streets and place labels. Building
    names are already the ones passed in `buildings`.
    """
    metres_per_tile = info["meters_per_tile"]
    width, height = proj.width, proj.height
    features: list[tuple[Polygon, str, str]] = []

    labels: list[tuple[str, str, float, float, float]] = []
    named_buildings = []
    for outline, kind, name in buildings:
        poly = _clean(Polygon(outline))
        if poly is not None:
            features.append((poly, "building", BUILDING_VALUE.get(kind, "yes")))
            if name and kind in LABELLED_KINDS and poly.area >= MIN_LABELLED_TILES:
                named_buildings.append((poly.area, name, poly))
    named_buildings.sort(key=lambda b: -b[0])
    for _area, name, poly in named_buildings[:MAX_BUILDING_LABELS]:
        spot = poly.representative_point()
        labels.append((name, "text-building", BUILDING_SCALE, spot.x, spot.y))

    bbox = info.get("bbox") or {}
    cache = os.path.join(out_dir, info["osm_cache"]) if info.get("osm_cache") \
        else osm.cache_path(out_dir, map_name)
    wanted = tuple(info["osm_bbox"]) if info.get("osm_bbox") else \
        (bbox.get("south"), bbox.get("west"), bbox.get("north"), bbox.get("east"))
    feats = osm.load_cache(cache, wanted) or []
    if info.get("straight_roads"):
        # The streets as the map drew them, not as they are.
        from generator.octilinear import straighten_roads
        from generator.renderer import classify
        straighten_roads(feats, proj, classify, _is_polygon)
    clip = shape_px(info.get("shape"), proj)
    streets: dict[str, list[tuple[LineString, float]]] = {}
    way_names: dict = {}
    rename: dict = {}
    hidden: set[str] = set()
    if edits is not None:
        street_edits = edits.streets or {}
        way_names = street_edits.get("ways") or {}
        rename = street_edits.get("rename") or {}
        hidden = set(street_edits.get("hide") or ())
    for n, feat in enumerate(feats):
        if n % 64 == 0:
            knoxstop.check(should_stop, "the buildings")
        if feat.kind == "node":
            continue
        cat = osm.classify(feat.tags, _is_polygon(feat))
        if cat in HIGHWAY_VALUE and not _is_polygon(feat):
            line = _line(feat, proj)
            if line is None:
                continue
            if clip is not None and cat not in THROUGH_ROADS:
                # The drawn shape's side streets only, as on the ground.
                line = line.intersection(clip)
                if line.is_empty or line.length < 1:
                    continue
                if not isinstance(line, LineString):
                    parts = [g for g in getattr(line, "geoms", []) if isinstance(g, LineString)]
                    if not parts:
                        continue
                    line = max(parts, key=lambda g: g.length)
            width_tiles = _way_width_m(feat, cat) / metres_per_tile
            strip = line.buffer(width_tiles / 2, cap_style=2, join_style=2)
            poly = _clean(strip)
            if poly is not None:
                features.append((poly, "highway", HIGHWAY_VALUE[cat]))
            name = (feat.tags.get("name") or "").strip()
            way_id = _osm_feature_id(feat)
            if way_id is not None and way_id in way_names and "name" in way_names[way_id]:
                name = (way_names[way_id]["name"] or "").strip()
            if name in rename:
                name = (rename[name] or "").strip()
            # Hidden streets stay on the map as road polygons; they just
            # lose the name a player would read.
            if name and name not in hidden and cat in NAMED_CLASSES:
                streets.setdefault(name, []).append((line, width_tiles))
        elif cat == "railway" and not _is_polygon(feat):
            line = _line(feat, proj)
            if line is not None:
                poly = _clean(line.buffer(RAIL_WIDTH_M / metres_per_tile / 2,
                                          cap_style=2, join_style=2))
                if poly is not None:
                    features.append((poly, "railway", "rail"))
        elif cat in AREA_VALUE and _is_polygon(feat):
            key, value = AREA_VALUE[cat]
            for ring in _outer_rings(feat, proj):
                poly = _clean(Polygon(ring))
                if poly is not None:
                    features.append((poly, key, value))
                    name = (feat.tags.get("name") or "").strip()
                    if name and key == "water" and poly.area >= MIN_PLACE_TILES:
                        spot = poly.representative_point()
                        labels.append((name, "text-water-medium", 1.0, spot.x, spot.y))
        elif _named_grounds(cat, feat) and _is_polygon(feat) and "highway" not in feat.tags:
            name = (feat.tags.get("name") or "").strip()
            rings = _outer_rings(feat, proj)
            if name and rings:
                poly = _clean(Polygon(rings[0]))
                if poly is not None and poly.area >= MIN_PLACE_TILES:
                    spot = poly.representative_point()
                    labels.append((name, "text-place", PLACE_SCALE, spot.x, spot.y))

    for sea in sea_polygons(feats, proj):
        for part in getattr(sea, "geoms", None) or [sea]:
            poly = _clean(part) if hasattr(part, "exterior") else None
            if poly is not None:
                features.append((poly, "water", "river"))

    cells = _write_worldmap(os.path.join(out_dir, "worldmap.xml"), features,
                            width, height, should_stop)
    named = _write_streets(os.path.join(out_dir, "streets.xml"), streets)

    places_path = os.path.join(out_dir, f"{map_name}_places.json")
    if os.path.exists(places_path):
        with open(places_path, encoding="utf-8") as f:
            for place in json.load(f):
                original = place.get("name") or ""
                rec = edits.place(original) if edits is not None else None
                if rec and rec.get("deleted"):
                    continue
                # Population is not a label. The spawn map reads _places.json;
                # this loop does not rewrite that file.
                label = rec["name"] if rec and "name" in rec else original
                if place.get("inside") and label:
                    labels.append((label, "text-town",
                                   TOWN_SCALE.get(place.get("place"), 2.0),
                                   place["tile_x"], place["tile_y"]))
    # OpenStreetMap attribution in the game itself, in a corner of the map
    # players open, as the ODbL attribution guidelines ask of produced works.
    labels.append(("Map data (c) OpenStreetMap contributors", "text-note", 0.35,
                   min(width - 1, 150), min(height - 1, 12)))
    _write_annotations(os.path.join(out_dir, "worldmap-annotations.lua"),
                       labels, width, height)
    return {"map_features": len(features), "map_cells": cells, "streets": named,
            "labels": len(labels)}

def _lua_string(text: str) -> str:
    escaped = (text.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", " ").replace("\r", " "))
    return f'"{escaped}"'


def _write_annotations(path: str, labels: list[tuple[str, str, float, float, float]],
                       width: int, height: int) -> None:
    """Text on the paper map, the way vanilla's worldmap-annotations.lua adds it."""
    ox, oy = origin()[0] * CELL, origin()[1] * CELL
    seen = set()
    lines = ["return function(mapUI)",
             "\tlocal mapAPI = mapUI.javaObject:getAPIv3()",
             "\tlocal symbolsAPI = mapAPI:getSymbolsAPIv2()",
             "\tlocal symbol"]
    for name, layer, scale, x, y in labels:
        if not (0 <= x < width and 0 <= y < height) or (name, layer) in seen:
            continue
        seen.add((name, layer))
        lines += [
            f"\tsymbol = symbolsAPI:addUntranslatedText({_lua_string(name)}, "
            f'"{layer}", {round(x + ox)}, {round(y + oy)})',
            "\tsymbol:setRGBA(0.000, 0.000, 0.000, 0.000)",
            f"\tsymbol:setScale({scale:.3f})",
            "\tsymbol:setAnchor(0.50, 0.50)",
            "\tsymbol:setRotation(0.0)",
            "\tsymbol:setMatchPerspective(true)",
            "\tsymbol:setApplyZoom(true)",
            "\tsymbol:setMinZoom(0.00)",
            "\tsymbol:setMaxZoom(24.00)",
            "\tsymbol:setUserDefined(false)",
            ""]
    lines.append("end")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _clean(poly) -> Polygon | None:
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
    poly = poly.simplify(SIMPLIFY, preserve_topology=True)
    return None if poly.is_empty or poly.area < 1 else poly


def _line(feat, proj) -> LineString | None:
    pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
    return LineString(pts) if len(pts) >= 2 else None


def _outer_rings(feat, proj) -> list[list[tuple[float, float]]]:
    if feat.kind == "way":
        return [[proj.to_px(la, lo) for la, lo in feat.geometry]]
    return [[proj.to_px(la, lo) for la, lo in ring]
            for role, ring in feat.role_geoms
            if role in ("outer", "") and len(ring) >= 3]


def _parts(geom) -> list[Polygon]:
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]


def _sorted_unique(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(cx, cy) in the order the old cell walk used: cy, then cx."""
    cells.sort(key=lambda c: (c[1], c[0]))
    out = []
    prev = None
    for cell in cells:
        if cell != prev:
            out.append(cell)
            prev = cell
    return out


def _cells_for_features(features, width: int, height: int) -> list[list[tuple[int, int]]]:
    """Cells each feature's bounds cross, from an STRtree of those bounds.

    The span is the same cell range the per-cell loop walked. The tree
    decides which of those cells the bounds actually meet.
    """
    n = len(features)
    assigned: list[list[tuple[int, int]]] = [[] for _ in range(n)]
    if n == 0 or width // CELL <= 0 or height // CELL <= 0:
        return assigned
    spans = []
    keys: list[tuple[int, int]] = []
    index: dict[tuple[int, int], int] = {}
    bounds = np.empty((n, 4), dtype=np.float64)
    for i, (poly, _key, _value) in enumerate(features):
        minx, miny, maxx, maxy = poly.bounds
        bounds[i] = (minx, miny, maxx, maxy)
        cy0 = max(0, int(miny // CELL))
        cy1 = min(height // CELL, int(maxy // CELL) + 1)
        cx0 = max(0, int(minx // CELL))
        cx1 = min(width // CELL, int(maxx // CELL) + 1)
        spans.append((cx0, cx1, cy0, cy1))
        if cy0 >= cy1 or cx0 >= cx1:
            continue
        for cy in range(cy0, cy1):
            for cx in range(cx0, cx1):
                key = (cx, cy)
                if key not in index:
                    index[key] = len(keys)
                    keys.append(key)
    if not keys:
        return assigned
    xs = np.array([cx * CELL for cx, _cy in keys], dtype=np.float64)
    ys = np.array([cy * CELL for _cx, cy in keys], dtype=np.float64)
    cell_boxes = shapely.box(xs, ys, xs + CELL, ys + CELL)
    try:
        tree = STRtree(shapely.box(bounds[:, 0], bounds[:, 1], bounds[:, 2], bounds[:, 3]))
        pairs = np.asarray(tree.query(cell_boxes, predicate="intersects"))
    except Exception:  # noqa: BLE001 - the span below is the same cell walk
        pairs = np.empty((2, 0), dtype=np.int64)
    # One query box can come back as a flat list of tree hits.
    if pairs.ndim == 1:
        pairs = np.vstack((np.zeros(pairs.size, dtype=np.int64), pairs))
    if pairs.size:
        for ci, fi in zip(pairs[0].tolist(), pairs[1].tolist()):
            cx, cy = keys[ci]
            cx0, cx1, cy0, cy1 = spans[fi]
            if cx0 <= cx < cx1 and cy0 <= cy < cy1:
                assigned[fi].append((cx, cy))
    for i, cells in enumerate(assigned):
        cx0, cx1, cy0, cy1 = spans[i]
        span_n = max(0, cx1 - cx0) * max(0, cy1 - cy0)
        unique = _sorted_unique(cells) if cells else []
        if len(unique) == span_n:
            assigned[i] = unique
        elif span_n:
            # The tree missed a cell of this span. Clip the whole span, which
            # is what the per-cell walk visited; an empty overlap writes nothing.
            assigned[i] = [(cx, cy) for cy in range(cy0, cy1) for cx in range(cx0, cx1)]
        else:
            assigned[i] = []
    return assigned


def _feature_xml(part, x0: int, y0: int, key: str, value: str) -> str:
    ring = list(part.exterior.coords)[:-1]
    pts = "\n".join(
        f'     <point x="{round(x - x0)}" y="{round(y - y0)}"/>'
        for x, y in ring)
    return (
        '  <feature>\n   <geometry type="Polygon">\n'
        f'    <coordinates>\n{pts}\n    </coordinates>\n'
        '   </geometry>\n   <properties>\n'
        f'    <property name="{key}" value={quoteattr(value)}/>\n'
        '   </properties>\n  </feature>'
    )


def _intersect_cells(poly, cells: list[tuple[int, int]]):
    """One geometry per cell: the polygon clipped to that cell's box."""
    x0 = np.array([cx * CELL for cx, _cy in cells], dtype=np.float64)
    y0 = np.array([cy * CELL for _cx, cy in cells], dtype=np.float64)
    boxes = shapely.box(x0, y0, x0 + CELL, y0 + CELL)
    try:
        geoms = np.asarray(shapely.intersection(poly, boxes), dtype=object).ravel()
        if geoms.size == len(cells):
            return list(geoms)
    except Exception:  # noqa: BLE001 - scalar clips below keep a bad outline usable
        pass
    return [poly.intersection(box(cx * CELL, cy * CELL, (cx + 1) * CELL, (cy + 1) * CELL))
            for cx, cy in cells]


def _clip_chunk(poly, key: str, value: str, cells: list[tuple[int, int]]):
    out = []
    for geom, (cx, cy) in zip(_intersect_cells(poly, cells), cells):
        x0, y0 = cx * CELL, cy * CELL
        for part in _parts(geom):
            if part.area < 1:
                continue
            out.append((cx, cy, _feature_xml(part, x0, y0, key, value)))
    return out


def _run_clip_wave(features, batch):
    def one(job):
        fi, cells = job
        poly, key, value = features[fi]
        return _clip_chunk(poly, key, value, cells)

    load = sum(len(cells) for _fi, cells in batch)
    if len(batch) > 1 and load >= _THREAD_CELLS:
        from concurrent.futures import ThreadPoolExecutor

        workers = min(len(batch), os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="worldmap") as pool:
            return list(pool.map(one, batch))
    return [one(job) for job in batch]


def _fill_worldmap(by_cell, features, width: int, height: int, should_stop) -> None:
    assigned = _cells_for_features(features, width, height)
    jobs = []
    for fi, cells in enumerate(assigned):
        for start in range(0, len(cells), _CELL_BATCH):
            jobs.append((fi, cells[start:start + _CELL_BATCH]))
    for start in range(0, len(jobs), _WAVE):
        knoxstop.check(should_stop, "the buildings")
        batch = jobs[start:start + _WAVE]
        for items in _run_clip_wave(features, batch):
            for cx, cy, xml in items:
                by_cell.setdefault((cx, cy), []).append(xml)


def _write_worldmap(path: str, features: list[tuple[Polygon, str, str]],
                    width: int, height: int, should_stop=None) -> int:
    """Clip every feature to the cells it crosses, in cell coordinates."""
    knoxstop.check(should_stop, "the buildings")
    ox, oy = origin()
    by_cell: dict[tuple[int, int], list[str]] = {}
    _fill_worldmap(by_cell, features, width, height, should_stop)
    knoxstop.check(should_stop, "the buildings")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<world version="1.0">\n')
        for (cx, cy) in sorted(by_cell, key=lambda c: (c[1], c[0])):
            f.write(f' <cell x="{ox + cx}" y="{oy + cy}">\n')
            f.write("\n".join(by_cell[(cx, cy)]))
            f.write("\n </cell>\n")
        f.write("</world>\n")
    return len(by_cell)


def _write_streets(path: str, streets: dict[str, list[tuple[LineString, float]]]) -> int:
    """One entry per continuous stretch of each named street."""
    ox, oy = origin()[0] * CELL, origin()[1] * CELL
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('<streets version="1">\n')
        for name in sorted(streets):
            pieces = streets[name]
            width = max(3, round(max(w for _l, w in pieces)))
            joined = unary_union([l for l, _w in pieces])
            merged = linemerge(joined) if isinstance(joined, MultiLineString) else joined
            lines = [merged] if isinstance(merged, LineString) else \
                list(getattr(merged, "geoms", []))
            for line in lines:
                line = line.simplify(SIMPLIFY)
                if line.length < 10:
                    continue
                pts = "\n".join(f'            <point x="{x + ox:.1f}" y="{y + oy:.1f}"/>'
                                for x, y in line.coords)
                f.write(f'    <street name={quoteattr(name)} width="{width}">\n'
                        f'        <points>\n{pts}\n        </points>\n    </street>\n')
                count += 1
        f.write("</streets>\n")
    return count
