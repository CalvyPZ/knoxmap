"""Rasterize OSM features into Project Zomboid bitmaps.

Output contract (per the Mapping Guide):
  <name>.bmp              — landscape (11 palette colors)
  <name>_veg.bmp          — vegetation (must be same size as landscape)
  <name>_ZombieSpawnMap.bmp — grayscale, 1/10th resolution

Dimensions are snapped up to the next multiple of 300 (PZ cell size). The
requested real-world bbox is expanded symmetrically to fit that grid so the
meters-per-tile scale stays consistent.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Iterable

import pyproj
from PIL import Image, ImageDraw, ImageFilter

import knoxstop

from . import biomes
from . import intersections
from . import pz_colors as C
from . import structures
from .osm import (
    AIRPORT_PAVED, FENCE_BARRIERS, OSMFeature, classify, is_airport_area,
    is_rail_area,
)


# --- projection ------------------------------------------------------------

@dataclass
class Projector:
    """Latitude/longitude → pixel coordinate in the output bitmap.

    Uses the UTM zone covering the bbox center so distance in meters maps
    nearly linearly to pixels. The projected bbox is expanded up to the next
    300-tile cell multiple.
    """
    south: float
    west: float
    north: float
    east: float
    meters_per_tile: float
    width: int   # pixels (tile count)
    height: int  # pixels
    min_x_m: float
    min_y_m: float
    _transformer: pyproj.Transformer
    # Degrees the map is turned, counter-clockwise, so its main street grid
    # runs along the tile grid. See dominant_road_angle.
    rotation: float = 0.0
    # UTM metres the turn is about. A window of a larger map keeps the
    # parent's center, so neighbouring pieces share an edge. None means this
    # projector's own center (a map saved before the center was recorded).
    rot_cx_m: float | None = None
    rot_cy_m: float | None = None

    @classmethod
    def build(cls, south: float, west: float, north: float, east: float,
              meters_per_tile: float, rotation: float = 0.0) -> "Projector":
        lon_c = (west + east) / 2
        lat_c = (south + north) / 2
        utm_zone = int((lon_c + 180) / 6) + 1
        epsg = (32600 if lat_c >= 0 else 32700) + utm_zone
        t = pyproj.Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}",
                                        always_xy=True)
        # Project the four corners so we pick up any distortion at the edges.
        corners = [(west, south), (east, south), (east, north), (west, north)]
        xs, ys = zip(*(t.transform(lo, la) for lo, la in corners))
        raw_w = max(xs) - min(xs)
        raw_h = max(ys) - min(ys)
        tiles_w = max(C.CELL_SIZE, _ceil_to(raw_w / meters_per_tile, C.CELL_SIZE))
        tiles_h = max(C.CELL_SIZE, _ceil_to(raw_h / meters_per_tile, C.CELL_SIZE))
        # Re-center: expand bbox in meters to match the rounded-up tile count.
        cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
        half_w_m = (tiles_w * meters_per_tile) / 2
        half_h_m = (tiles_h * meters_per_tile) / 2
        return cls(south, west, north, east, meters_per_tile,
                   tiles_w, tiles_h,
                   cx - half_w_m, cy - half_h_m,
                   t, rotation, cx, cy)

    def _rot_center(self) -> tuple[float, float]:
        if self.rot_cx_m is None or self.rot_cy_m is None:
            w_m = self.width * self.meters_per_tile
            h_m = self.height * self.meters_per_tile
            return self.min_x_m + w_m / 2, self.min_y_m + h_m / 2
        return self.rot_cx_m, self.rot_cy_m

    def to_px(self, lat: float, lon: float) -> tuple[float, float]:
        x_m, y_m = self._transformer.transform(lon, lat)
        rot = self.rotation
        if rot:
            trig = getattr(self, "_px_trig", None)
            if trig is None or trig[0] != rot:
                ang = math.radians(rot)
                trig = (rot, math.cos(ang), math.sin(ang))
                self._px_trig = trig
            cx_m = self.rot_cx_m
            cy_m = self.rot_cy_m
            if cx_m is None or cy_m is None:
                cx, cy = self._rot_center()
            else:
                cx, cy = cx_m, cy_m
            cos_a, sin_a = trig[1], trig[2]
            dx = x_m - cx
            dy = y_m - cy
            x_m = cx + dx * cos_a - dy * sin_a
            y_m = cy + dx * sin_a + dy * cos_a
        mpt = self.meters_per_tile
        px = (x_m - self.min_x_m) / mpt
        # Image Y grows downward; UTM Y grows northward → flip.
        py = self.height - (y_m - self.min_y_m) / mpt
        return px, py

    def to_latlon(self, px: float, py: float) -> tuple[float, float]:
        """The inverse of to_px."""
        back = getattr(self, "_back", None)
        if back is None:
            back = pyproj.Transformer.from_crs(self._transformer.target_crs,
                                               "EPSG:4326", always_xy=True)
            object.__setattr__(self, "_back", back)
        x_m = self.min_x_m + px * self.meters_per_tile
        y_m = self.min_y_m + (self.height - py) * self.meters_per_tile
        if self.rotation:
            cx, cy = self._rot_center()
            a = math.radians(-self.rotation)
            dx, dy = x_m - cx, y_m - cy
            x_m = cx + dx * math.cos(a) - dy * math.sin(a)
            y_m = cy + dx * math.sin(a) + dy * math.cos(a)
        lon, lat = back.transform(x_m, y_m)
        return lat, lon

    def latlon_bbox(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) covering the whole map, turned or not."""
        lats, lons = [], []
        for px, py in ((0, 0), (self.width, 0), (self.width, self.height), (0, self.height)):
            lat, lon = self.to_latlon(px, py)
            lats.append(lat)
            lons.append(lon)
        return min(lats), min(lons), max(lats), max(lons)

    def cell_grid(self) -> tuple[int, int]:
        return self.width // C.CELL_SIZE, self.height // C.CELL_SIZE

    def window(self, x0: int, y0: int, tiles_w: int, tiles_h: int) -> "Projector":
        """A tile rectangle of this map, on the same metre grid.

        Adjacent windows share the edge between them, so two mods drawn from
        them meet when both are enabled. The turn stays about this projector's
        center, not the window's own, or the pieces fan apart on the preview.
        """
        mpt = self.meters_per_tile
        north_m = self.min_y_m + self.height * mpt
        rcx, rcy = self._rot_center()
        return Projector(
            self.south, self.west, self.north, self.east, mpt,
            tiles_w, tiles_h,
            self.min_x_m + x0 * mpt,
            north_m - (y0 + tiles_h) * mpt,
            self._transformer, self.rotation, rcx, rcy)

    def grid_dict(self) -> dict:
        epsg = self._transformer.target_crs.to_epsg()
        return {
            "min_x_m": self.min_x_m,
            "min_y_m": self.min_y_m,
            "width_tiles": self.width,
            "height_tiles": self.height,
            "meters_per_tile": self.meters_per_tile,
            "epsg": int(epsg or 0),
            "rotation": self.rotation,
            "rot_cx_m": self._rot_center()[0],
            "rot_cy_m": self._rot_center()[1],
        }

    @classmethod
    def from_grid(cls, grid: dict) -> "Projector":
        """The projector a mod was drawn with, so a later build lands on it."""
        epsg = int(grid["epsg"])
        transformer = pyproj.Transformer.from_crs(
            "EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        rcx, rcy = grid.get("rot_cx_m"), grid.get("rot_cy_m")
        try:
            rcx = float(rcx) if rcx is not None else None
            rcy = float(rcy) if rcy is not None else None
        except (TypeError, ValueError):
            rcx, rcy = None, None
        if rcx is None or rcy is None:
            rcx, rcy = None, None
        return cls(0.0, 0.0, 0.0, 0.0, float(grid["meters_per_tile"]),
                   int(grid["width_tiles"]), int(grid["height_tiles"]),
                   float(grid["min_x_m"]), float(grid["min_y_m"]),
                   transformer, float(grid.get("rotation") or 0.0), rcx, rcy)


# Roads that count towards a town's street grid. Paths wander, and a motorway
# slicing through at its own angle should not turn the town around it.
GRID_ROADS = {"road_minor", "road_medium", "road_service", "pedestrian"}
# How strongly the streets must agree on a direction before the map is turned
# to it: 1 is a perfect grid, 0 no preference. Measured on test maps, gridded
# towns sit well above this and old organic centres below it.
ALIGN_MIN_STRENGTH = 0.25


# Roads that carry on past the edge of a drawn shape. Cutting every road at the
# line would leave the town an island in a field; the main roads out of it
# stay, the side streets and driveways of places not chosen do not.
THROUGH_ROADS = ("road_major", "road_medium")


def shape_px(shape: dict | None, proj: Projector):
    """A drawn selection (GeoJSON Polygon or MultiPolygon, lon/lat) in tile
    coordinates, or None for a plain rectangle."""
    if not shape:
        return None
    from shapely.geometry import MultiPolygon, Polygon

    polys = shape["coordinates"] if shape.get("type") == "MultiPolygon" else [shape["coordinates"]]
    parts = []
    for rings in polys:
        if not rings or len(rings[0]) < 3:
            continue
        outer = [proj.to_px(lat, lon) for lon, lat in rings[0]]
        holes = [[proj.to_px(lat, lon) for lon, lat in r] for r in rings[1:] if len(r) >= 3]
        poly = Polygon(outer, holes)
        parts.append(poly if poly.is_valid else poly.buffer(0))
    if not parts:
        return None
    merged = parts[0] if len(parts) == 1 else MultiPolygon(
        [g for p in parts for g in (getattr(p, "geoms", None) or [p])])
    return merged if merged.is_valid else merged.buffer(0)


def _inside(feat: OSMFeature, proj: Projector, shape) -> bool:
    """Whether a feature belongs to the drawn shape: its middle is inside."""
    from shapely.geometry import LineString, Point, Polygon

    if feat.kind == "relation":
        rings = [ring for role, ring in feat.role_geoms if role != "inner" and len(ring) >= 3]
        if not rings:
            return False
        geom = Polygon([proj.to_px(la, lo) for la, lo in rings[0]])
        geom = geom if geom.is_valid else geom.buffer(0)
        return not geom.is_empty and shape.contains(geom.representative_point())
    pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
    if not pts:
        return False
    if len(pts) >= 3 and pts[0] == pts[-1]:
        geom = Polygon(pts)
        spot = geom.representative_point() if geom.is_valid else Point(pts[0])
        return shape.contains(spot)
    if len(pts) >= 2:
        return shape.intersects(LineString(pts))
    return shape.contains(Point(pts[0]))


def _clip_to_shape(landscape: Image.Image, shape, buckets: dict[str, list[OSMFeature]],
                   proj: Projector):
    """Outside the drawn shape, the ground goes back to countryside.

    Water stays - a river does not stop at a line on the map - and so do the
    main roads through it, with their pavements. Everything else outside, the
    yards and car parks and side streets of the neighbourhood next door,
    becomes grass.
    """
    import numpy as np

    w, h = landscape.size
    inside = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(inside)
    for part in getattr(shape, "geoms", None) or [shape]:
        d.polygon(list(part.exterior.coords), fill=255)
        for hole in part.interiors:
            d.polygon(list(hole.coords), fill=0)
    keep = inside.copy()
    kd = ImageDraw.Draw(keep)
    mpt = proj.meters_per_tile
    for cat in THROUGH_ROADS:
        for feat in buckets.get(cat, []):
            if _is_polygon(feat):
                continue
            width = (_way_width_m(feat, cat) + 2 * SIDEWALK_M[cat]) / mpt
            _draw_line(kd, _feature_coords_px(feat, proj), 255, int(width))
    for cat in ("water", "pool", "coastline"):
        for feat in buckets.get(cat, []):
            if _is_polygon(feat):
                for ring in _feature_coords_px(feat, proj):
                    if len(ring) >= 3:
                        kd.polygon(ring, fill=255)
    strip = 1024
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        outside = np.asarray(keep.crop((0, y0, w, y1))) == 0
        if not outside.any():
            continue
        ground = np.asarray(landscape.crop((0, y0, w, y1)))
        water = (ground[:, :, 0] == C.WATER[0]) & (ground[:, :, 1] == C.WATER[1]) \
            & (ground[:, :, 2] == C.WATER[2])
        outside &= ~water
        if outside.any():
            grass = Image.new("RGB", (w, y1 - y0), C.DARK_GRASS)
            landscape.paste(grass, (0, y0), Image.fromarray(outside.astype(np.uint8) * 255))
    return keep


def cover_bbox(south: float, west: float, north: float, east: float,
               meters_per_tile: float) -> tuple[float, float, float, float]:
    """A (south, west, north, east) box holding the map however it is turned.

    The map is a rectangle of whole cells round the selection; turned, it
    sweeps a circle through its corners. The box round that circle holds every
    feature the map could show at any angle, so one download serves both
    measuring the street grid and drawing the turned map.
    """
    proj = Projector.build(south, west, north, east, meters_per_tile)
    w_m = proj.width * meters_per_tile
    h_m = proj.height * meters_per_tile
    cx, cy = proj.min_x_m + w_m / 2, proj.min_y_m + h_m / 2
    r = math.hypot(w_m, h_m) / 2 + 50       # a margin for ways just outside
    back = pyproj.Transformer.from_crs(proj._transformer.target_crs, "EPSG:4326",
                                       always_xy=True)
    lons, lats = zip(*(back.transform(cx + dx, cy + dy)
                       for dx in (-r, r) for dy in (-r, r)))
    return min(lats), min(lons), max(lats), max(lons)


def bbox_for_cells(south: float, west: float, north: float, east: float,
                   meters_per_tile: float) -> tuple[float, float, float, float]:
    """The selection plus one cell.

    The map is rounded up to whole cells, so its edges sit a little outside
    the box that was drawn. The cut has to include that margin or each edge
    piece would walk the regional file again.
    """
    dlat = (meters_per_tile * C.CELL_SIZE) / 111320.0
    mid = math.radians((south + north) / 2.0)
    dlon = dlat / max(0.2, math.cos(mid))
    return (max(-90.0, south - dlat), max(-180.0, west - dlon),
            min(90.0, north + dlat), min(180.0, east + dlon))


def dominant_road_angle(features: Iterable[OSMFeature], south: float, west: float,
                        north: float, east: float) -> tuple[float, float]:
    """The main direction of a town's streets, and how strongly they share it.

    Tiles are square, so a street at 20 degrees becomes a staircase with a step
    every few tiles - kerbs zigzagging along it and blends failing at every
    corner. Most towns have a grid, even a loose one; turning the whole map so
    that grid runs along the tiles straightens every street on it. Buildings,
    fences and the paper map are projected the same way, so nothing is
    misaligned - only north is no longer straight up.

    Returns (angle, strength): the angle in degrees in (-45, 45], counter-
    clockwise from east, and the strength in [0, 1]. Directions are averaged
    with a 90-degree period, so a north-south street and an east-west one
    agree; each segment weighs by its length.
    """
    proj = Projector.build(south, west, north, east, 1.0)
    sin_sum = cos_sum = total = 0.0
    for feat in features:
        if feat.kind != "way" or classify(feat.tags, _is_polygon(feat)) not in GRID_ROADS:
            continue
        pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            dx, dy = bx - ax, ay - by      # tile y grows south; flip to north-up
            length = math.hypot(dx, dy)
            if length < 1:
                continue
            a = 4 * math.atan2(dy, dx)
            sin_sum += length * math.sin(a)
            cos_sum += length * math.cos(a)
            total += length
    if total == 0:
        return 0.0, 0.0
    angle = math.degrees(math.atan2(sin_sum, cos_sum) / 4)
    return angle, math.hypot(sin_sum, cos_sum) / total


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


# --- feature painting ------------------------------------------------------

# Paint order for landscape: later categories overwrite earlier ones, so this
# is effectively painted bottom-to-top.
LANDSCAPE_ORDER = [
    # Broad land use first, so anything more specific inside it - a pitch in a
    # schoolyard, a car park on an industrial estate - is painted over it.
    "residential",    # gardens and yards
    "commercial",     # pavement in front of shops
    "industrial",     # gravel yards
    "airport",        # airfield grass; runways and aprons are painted later
    "military",
    "schoolyard",
    "hospital_grounds",
    "worship_grounds",
    "cemetery",
    "orchard",
    "farmland",       # light grass
    "grass",          # medium grass
    "park",           # medium grass w/ trees added by vegetation pass
    "sports",
    "wetland",
    "sand",
    "dirt",
    "playground",
    "track",
    # Water under the ways that cross it: a river polygon painted last erased
    # every bridge, leaving no way over. Above land use, which it still wins.
    "water",
    "pool",
    "railway",        # gravel track bed; roads cross it at level crossings
    "dirt_path",      # walking path: dirt tiles
    "paved_path",     # footway, pavement, cycleway
    "road_track",     # farm track: gravel track tiles, dirt when tagged so
    "pier",           # jetties and breakwaters, over the water
    "parking",        # car park tarmac
    "plaza",          # paved pedestrian square
    "pedestrian",     # pedestrian zone, paved wall to wall
    "road_service",   # alleys and driveways
    "road_minor",
    "road_medium",
    "road_major",     # widest, so it wins at junctions
    # Building footprints are not painted at all. They used to be dirt, which
    # showed as a brown fringe wherever the placed building and the painted
    # footprint disagreed by a tile - and the building's own floor covers the
    # ground under it anyway.
]

# Land and water, painted before any street. The window shows this pass on
# its own, then the roads on top of it.
_GROUND_FIRST = {
    "residential", "commercial", "industrial", "airport", "military", "schoolyard",
    "hospital_grounds", "worship_grounds", "cemetery", "orchard", "farmland",
    "grass", "park", "sports", "wetland", "sand", "dirt", "playground",
    "track", "water", "pool",
}

# Waterways drawn from a centre line, when no area is mapped around them.
# A river is usually mapped with its banks too, which paints over this.
WATERWAY_WIDTH_M = {"river": 12.0, "canal": 8.0, "stream": 2.0}

# Road widths in meters. Converted to pixels by dividing by meters_per_tile.
#
# These are the fallbacks. A way carrying lanes= or width= is measured from
# those instead, because every street in a class being identically wide is what
# made the towns read as a printed circuit rather than a place - real ones have
# a four-lane high street feeding two-lane side roads feeding one-car alleys.
ROAD_WIDTHS_M = {
    "road_major": 12.0,
    "road_medium": 8.0,
    "road_minor": 6.0,
    "road_service": 3.5,
    # A pedestrian zone is as wide as the square it paves, not as wide as
    # a lane; mapped as lines with no area round them, this is what fills
    # the space between the buildings.
    "pedestrian": 9.0,
    "dirt_path": 2.5,
    "paved_path": 2.5,
    "road_track": 4.0,
    "pier": 3.0,
    "railway": 4.0,
}

# Centre lines written to _roads.geojson. worldmap.HIGHWAY_VALUE draws these
# as highways; NAMED_CLASSES is the named subset. Pier and railway are drawn
# on the paper map, but not as highways.
ROAD_EXPORT_CATEGORIES = (
    "road_major", "road_medium", "road_minor", "road_service",
    "dirt_path", "paved_path", "road_track",
)

# Metres of kerb either side. Town streets in the vanilla game sit in a band of
# pale concrete; without it the asphalt runs straight into grass and every road
# looks like it was dropped on the landscape rather than built into it.
SIDEWALK_M = {
    "road_major": 2.5,
    "road_medium": 3.5,
    "road_minor": 3.0,
}
# Of that, the strip of grass between the kerb and the pavement on a
# residential street - Knox County's streets run kerb, verge, pavement, lawn.
# In a built-up block the verge is paved over with the rest of the ground.
VERGE_M = {
    "road_medium": 1.5,
    "road_minor": 1.5,
}

LANDSCAPE_FILL = {
    "water": C.WATER,
    "sand": C.SAND,
    "dirt": C.DIRT,
    "dirt_path": C.DIRT,
    "paved_path": C.PALE_CONCRETE,
    # lightgravel (blends_street): the track tiles. A car treats them as a
    # road. A track tagged dirt/earth/mud is painted with the dirt tiles
    # instead; see _route_fill.
    "road_track": C.LIGHT_ASPHALT,
    "pier": C.PALE_CONCRETE,
    "railway": C.LIGHT_ASPHALT,
    "grass": C.MEDIUM_GRASS,
    "park": C.MEDIUM_GRASS,
    "farmland": C.LIGHT_GRASS,
    # street/street2/street4 in Rules.txt. road_minor used to paint
    # lightgravel, which put a gravel track through the middle of every
    # residential street in town.
    # Service lanes were gravel, which next to slab pavements read as more
    # pavement. Main roads get the worn speckled tarmac so they stand apart
    # from the smooth tarmac of ordinary streets; both blend at the edges.
    "road_service": C.DARKEST_ASPHALT,
    "pedestrian": C.PAVING,
    "road_minor": C.MEDIUM_ASPHALT,
    "road_medium": C.MEDIUM_ASPHALT,
    "road_major": C.DARKEST_ASPHALT,
    "parking": C.DARK_ASPHALT,
    "plaza": C.PAVING,
    "residential": C.MEDIUM_GRASS,
    "commercial": C.PALE_CONCRETE,
    "industrial": C.LIGHT_ASPHALT,
    "airport": C.MEDIUM_GRASS,
    "military": C.DIRT,
    "schoolyard": C.PALE_CONCRETE,
    "hospital_grounds": C.MEDIUM_GRASS,
    "worship_grounds": C.PAVING,
    "cemetery": C.LIGHT_GRASS,
    "orchard": C.MEDIUM_GRASS,
    "sports": C.MEDIUM_GRASS,
    "wetland": C.DARK_GRASS,
    "playground": C.SAND,
    "track": C.CLAY,
    "pool": C.WATER,
}

# A track tagged as bare dirt takes the dirt tiles. Anything else, including
# an untagged track, takes the gravel track tiles.
_DIRT_TRACK_SURFACES = {"dirt", "earth", "ground", "mud", "sand", "grass", "soil"}


def _route_fill(cat: str, tags: dict):
    """Ground colour for one way. Tracks follow the surface tag."""
    if cat == "road_track" and (tags.get("surface") or "").lower() in _DIRT_TRACK_SURFACES:
        return C.DIRT
    return LANDSCAPE_FILL.get(cat)

# Categories knoxbuild needs as areas, to tell what a building standing in
# them probably is: an untagged building on an industrial estate is a works,
# not a house.
AREA_CATEGORIES = {"residential", "commercial", "industrial", "military",
                   "schoolyard", "hospital_grounds", "worship_grounds",
                   "cemetery", "parking", "sports", "airport", "railway"}
# Land use drawn on the generate-tab zoning overlay. Wider than AREA_CATEGORIES
# on purpose: a park does not change what a building is, but it is still a zone.
ZONE_CATEGORIES = (
    "residential", "commercial", "industrial", "military",
    "schoolyard", "hospital_grounds", "worship_grounds",
    "cemetery", "parking", "sports", "airport", "railway",
    "park", "grass", "farmland", "forest", "scrub",
    "orchard", "wetland", "playground", "plaza",
)
# Drawn onto the vegetation bitmap rather than the ground.
VEG_CATEGORIES = {"forest", "scrub", "tree_single", "hedge", "orchard",
                  "cemetery", "wetland"}


def _way_width_m(feat: OSMFeature, cat: str) -> float:
    """Carriageway width for this way, from its own tags where it has them."""
    base = ROAD_WIDTHS_M[cat]
    raw = feat.tags.get("width") or feat.tags.get("est_width")
    if raw:
        # OSM widths are metres unless suffixed; "7", "7 m" and "7.5" all occur.
        try:
            return max(2.0, min(30.0, float(str(raw).split()[0].replace(",", "."))))
        except ValueError:
            pass
    lanes = feat.tags.get("lanes")
    if lanes:
        try:
            n = max(1, min(8, int(str(lanes).split(";")[0])))
        except ValueError:
            n = 0
        if n:
            # 3.2 m a lane, plus a little for the shoulder and markings.
            width = n * 3.2 + 1.0
            if feat.tags.get("oneway") == "yes":
                width = max(width, 3.5)
            return width
    return base


def _aeroway_width_m(feat: OSMFeature) -> float:
    """Paved width of a runway or taxiway mapped as a centre line.

    OSM's width is in metres. A runway with no width is the usual 45 m; a
    taxiway is a lane and a half. The road cap of 30 m would draw a runway
    as a street.
    """
    kind = feat.tags.get("aeroway")
    if kind == "runway":
        base, cap = 45.0, 90.0
    elif kind == "taxiway":
        base, cap = 18.0, 45.0
    else:
        base, cap = 23.0, 60.0
    raw = feat.tags.get("width") or feat.tags.get("est_width")
    if raw:
        try:
            return max(4.0, min(cap, float(str(raw).split()[0].replace(",", "."))))
        except ValueError:
            pass
    return base


def _paint_filled(draw: ImageDraw.ImageDraw, image: Image.Image, feat: OSMFeature,
                  proj: Projector, fill: tuple[int, int, int]) -> None:
    """Fill one area. Open ways are left to the line passes."""
    if not _is_polygon(feat):
        return
    if feat.kind == "relation":
        _paint_multipolygon(image, feat, proj, fill)
    else:
        _draw_polygon(draw, _feature_coords_px(feat, proj), fill)


def _feature_coords_px(feat: OSMFeature, proj: Projector) -> list[list[tuple[float, float]]]:
    """Project every ring/linestring in this feature to pixel space."""
    if feat.kind == "way":
        return [[proj.to_px(la, lo) for la, lo in feat.geometry]]
    if feat.kind == "relation":
        rings: list[list[tuple[float, float]]] = []
        for _role, coords in feat.role_geoms:
            rings.append([proj.to_px(la, lo) for la, lo in coords])
        return rings
    if feat.kind == "node":
        la, lo = feat.geometry[0]
        return [[proj.to_px(la, lo)]]
    return []


def _is_polygon(feat: OSMFeature) -> bool:
    """Treat as polygon if first/last point match (way) or it's a relation.

    A closed roundabout is still the ring of the road. Painted as an area it
    became a solid disc of tarmac, with no pavement and no kerb. area=yes
    wins, the same rule as the local extract reader.
    """
    if feat.kind == "relation":
        return True
    if feat.kind == "way" and len(feat.geometry) >= 3 and feat.geometry[0] == feat.geometry[-1]:
        if feat.tags.get("area") == "yes":
            return True
        if feat.tags.get("junction") in {"roundabout", "circular"}:
            return False
        return True
    return False


def _paint_multipolygon(image: Image.Image, feat: OSMFeature, proj: Projector,
                        fill: tuple[int, int, int]) -> None:
    """Fill a relation's outer rings and leave its inner rings as they were.

    An island in a lake or a clearing in a wood is an inner ring. Drawn like
    the outers it was flooded or planted over; here it is cut out of a mask
    the size of the relation, so whatever lies under it shows through.
    """
    rings = [(role, [proj.to_px(la, lo) for la, lo in ring])
             for role, ring in feat.role_geoms if len(ring) >= 3]
    if not rings:
        return
    xs = [x for _r, ring in rings for x, _y in ring]
    ys = [y for _r, ring in rings for _x, y in ring]
    x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
    x1, y1 = min(image.width, int(max(xs)) + 2), min(image.height, int(max(ys)) + 2)
    if x1 <= x0 or y1 <= y0:
        return
    mask = Image.new("L", (x1 - x0, y1 - y0), 0)
    md = ImageDraw.Draw(mask)
    for role, ring in sorted(rings, key=lambda r: r[0] == "inner"):
        md.polygon([(x - x0, y - y0) for x, y in ring],
                   fill=0 if role == "inner" else 255)
    image.paste(Image.new("RGB", mask.size, fill), (x0, y0), mask)


def _draw_polygon(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
                  fill: tuple[int, int, int]) -> None:
    for ring in rings:
        if len(ring) >= 3:
            draw.polygon(ring, fill=fill)


def _draw_line(draw: ImageDraw.ImageDraw, rings: list[list[tuple[float, float]]],
               fill: tuple[int, int, int], width_px: int) -> None:
    w = max(1, int(round(width_px)))
    for ring in rings:
        if len(ring) >= 2:
            draw.line(ring, fill=fill, width=w, joint="curve")
            # Round line caps so intersections look right.
            r = w // 2
            if r > 0:
                for x, y in ring:
                    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def _paint_watch(on_view, every: float = 1.2):
    """Hand the bitmap to on_view as it is painted, at most once a second or so.

    A new `stage` is sent at once, so the window can show each pass (terrain,
    streets, and what follows) instead of waiting out the throttle.
    """
    last = 0.0
    shown = None

    def show(image, force: bool = False, stage: str | None = None) -> None:
        nonlocal last, shown
        if on_view is None:
            return
        now = time.monotonic()
        if stage is not None and stage != shown:
            force = True
        if not force and now - last < every:
            return
        last = now
        if stage is not None:
            shown = stage
        on_view(image, shown)

    return show


# --- main entry point ------------------------------------------------------

@dataclass
class RenderResult:
    landscape_path: str
    vegetation_path: str
    spawn_map_path: str
    preview_path: str
    buildings_geojson_path: str
    meta_path: str
    width: int
    height: int
    cells_x: int
    cells_y: int


def render(features: Iterable[OSMFeature], south: float, west: float,
           north: float, east: float, meters_per_tile: float,
           output_dir: str, map_name: str,
           spawn_density: int = 10,
           tree_density: float = 1.0,
           rotation: float = 0.0,
           osm_cache: str | None = None,
           osm_bbox: tuple[float, float, float, float] | None = None,
           shape: dict | None = None,
           straight_roads: bool = False,
           should_stop=None,
           proj: "Projector | None" = None,
           on_view=None) -> RenderResult:
    if proj is None:
        proj = Projector.build(south, west, north, east, meters_per_tile, rotation)
    # Read more than once below - the buildings pass goes back over the
    # address points - so never leave this as a generator.
    features = list(features)
    # Junctions are read off the roads as mapped, then carried onto wherever
    # straightening moves those roads. The nodes are still in `features` here;
    # the paint loop below drops every node that is not a tree.
    junctions = intersections.collect(
        features, classify, _is_polygon, _way_width_m, SIDEWALK_M)
    moved_junctions: dict = {}
    if straight_roads:
        # Every road in straight runs at 45-degree steps (octilinear.py).
        from .octilinear import straighten_roads
        straighten_roads(features, proj, classify, _is_polygon, moved_junctions)
    junctions.relocate(moved_junctions, proj)
    junctions.measure(meters_per_tile)
    # A drawn polygon, circle or real outline rather than a rectangle: the map
    # still covers its bounding box in whole cells, but only what lies inside
    # the shape is built.
    clip = shape_px(shape, proj)
    landscape = Image.new("RGB", (proj.width, proj.height), C.DARK_GRASS)
    vegetation = Image.new("RGB", (proj.width, proj.height), C.VEG_NOTHING)
    # Biome index for Build 42's biomemap PNGs. Same paint order as the
    # ground, so a wood, a field and a street land on the pixels the game
    # reads back as biome and foraging zone.
    cover = biomes.Cover(
        (proj.width, proj.height), (south + north) / 2.0, (west + east) / 2.0)
    l_draw = ImageDraw.Draw(landscape)
    # The window watches this bitmap while it is painted. Throttled so a
    # long road pass still redraws, without a copy on every feature.
    show = _paint_watch(on_view)
    # The grass sheet is the first frame worth showing. Land use paints onto
    # it next; streets wait until that pass has had its own frame.
    show(landscape, force=True, stage="Terrain and biomes")

    # Bucket features so we paint in a deterministic order.
    buckets: dict[str, list[OSMFeature]] = {}
    vegetation_feats: list[OSMFeature] = []
    building_feats: list[OSMFeature] = []
    fence_feats: list[OSMFeature] = []
    place_feats: list[OSMFeature] = []
    monument_feats: list[OSMFeature] = []
    for feat in features:
        # Fences and hedges are collected from any outline that carries one,
        # before and independently of what the outline is: the fence around a
        # schoolyard is still a fence when the way is also the schoolyard.
        if feat.kind == "node" and "population" in feat.tags and "place" in feat.tags:
            place_feats.append(feat)
            continue
        if structures.monument_kind(feat.tags):
            monument_feats.append(feat)
        if feat.kind == "node" and "natural" not in feat.tags:
            continue      # shops and cafes inside buildings: knoxbuild/uses.py
        barrier = feat.tags.get("barrier")
        if barrier in FENCE_BARRIERS and feat.kind == "way":
            fence_feats.append(feat)
        elif barrier == "hedge" and feat.kind == "way":
            vegetation_feats.append(feat)
        cat = classify(feat.tags, _is_polygon(feat))
        if cat is None:
            continue
        if cat in {"fence", "hedge"}:
            continue          # the line itself is collected below
        if cat in VEG_CATEGORIES:
            vegetation_feats.append(feat)
            if cat in {"forest", "scrub", "tree_single", "hedge"}:
                continue
        if cat == "building":
            if clip is not None and not _inside(feat, proj, clip):
                continue
            building_feats.append(feat)
        buckets.setdefault(cat, []).append(feat)

    # Bridges lifted over the roads they cross, and monuments. See
    # generator/structures.py; what they leave out of the ground is cut here.
    lifted = structures.Plan()
    structures.plan_bridges(buckets, proj, meters_per_tile, _way_width_m, lifted)
    structures.plan_monuments(monument_feats, proj, meters_per_tile, lifted)
    if lifted.not_buildings:
        building_feats = [f for f in building_feats if id(f) not in lifted.not_buildings]
        buckets["building"] = [f for f in buckets.get("building", [])
                               if id(f) not in lifted.not_buildings]

    # Streets where every home is an address point and not a drawn building.
    addressed = _houses_from_addresses(features, building_feats, proj)
    if addressed:
        building_feats = building_feats + addressed
        buckets["building"] = buckets.get("building", []) + addressed
    # The same streets again where neither source drew a roof: a house on
    # each side, kept off the carriageway and off anything already mapped.
    streeted = _houses_along_streets(buckets, building_feats, proj, clip)
    if streeted:
        building_feats = building_feats + streeted
        buckets["building"] = buckets.get("building", []) + streeted

    def ground_rings(feat: OSMFeature) -> list[list[tuple[float, float]]]:
        rings = _feature_coords_px(feat, proj)
        cut = lifted.cut.get(id(feat))
        if cut is None:
            return rings
        from shapely.geometry import LineString as _Line
        left = _Line(rings[0]).difference(cut)
        return [list(g.coords) for g in getattr(left, "geoms", [left])
                if g.geom_type == "LineString" and not g.is_empty]

    # Area edits change the bitmaps only. _areas.geojson stays the OSM
    # export, and added polygons are not written into it. A missing or
    # broken overlay paints the OSM data as it stands.
    paint_buckets = buckets
    paint_veg = vegetation_feats
    try:
        loaded = _load_edits(output_dir, map_name)
        if loaded is not None:
            paint_buckets, paint_veg = _areas_for_paint(
                buckets, vegetation_feats, loaded)
    except Exception:
        paint_buckets = buckets
        paint_veg = vegetation_feats

    def _roles(feat: OSMFeature):
        return [(role, [proj.to_px(la, lo) for la, lo in ring])
                for role, ring in feat.role_geoms]

    def _mark(cat: str, feat: OSMFeature, rings, width: int | None = None) -> None:
        cover.stamp(
            cat, feat.tags, rings, width=width,
            relation_rings=_roles(feat) if feat.kind == "relation" else None)

    # The sea first, under everything: a pier or a beach mapped over it
    # paints on top.
    for sea in sea_polygons(buckets.get("coastline", []), proj):
        for part in getattr(sea, "geoms", None) or [sea]:
            if hasattr(part, "exterior"):
                l_draw.polygon(list(part.exterior.coords), fill=C.WATER)
                cover.polygon([list(part.exterior.coords)], biomes.WATER)
                for hole in part.interiors:
                    l_draw.polygon(list(hole.coords), fill=C.DARK_GRASS)
                    cover.polygon([list(hole.coords)], cover.open)

    paint_stage = "Terrain and biomes"
    for cat in LANDSCAPE_ORDER:
        if paint_stage == "Terrain and biomes" and cat not in _GROUND_FIRST:
            show(landscape, force=True, stage="Terrain and biomes")
            paint_stage = "Streets"
        if cat == "railway":
            # Pavements, as one pass over every road class and after the land
            # use: painted first, a park or a lawn mapped up to the kerb erased
            # the pavement and the tarmac met the grass. One pass so a side
            # street's pavement cannot cut across the high street it joins.
            for road in ("road_minor", "road_medium", "road_major"):
                margin = SIDEWALK_M[road]
                for feat in paint_buckets.get(road, []):
                    if _is_polygon(feat):
                        continue
                    width_px = (_way_width_m(feat, road) + 2 * margin) / meters_per_tile
                    _draw_line(l_draw, ground_rings(feat),
                               C.PALE_CONCRETE, int(width_px))
                    cover.line(ground_rings(feat), biomes.DIRT, int(width_px))
                    show(landscape, stage=paint_stage)
            for road, verge in VERGE_M.items():
                for feat in paint_buckets.get(road, []):
                    if _is_polygon(feat):
                        continue
                    width_px = (_way_width_m(feat, road) + 2 * verge) / meters_per_tile
                    rings = ground_rings(feat)
                    _draw_line(l_draw, rings, C.DARK_GRASS, int(width_px))
                    cover.line(rings, biomes.TOWN, int(width_px))
                    show(landscape, stage=paint_stage)
        fill = LANDSCAPE_FILL.get(cat)
        if fill is None:
            continue
        for feat in paint_buckets.get(cat, []):
            # Paved aeroways are drawn after the roads, or a grass polygon
            # mapped over the infield would cover the runway. Railway yards
            # are land use and are drawn with the industrial pass, under the
            # pavements; the rails themselves stay here so a road can cross.
            if cat == "airport" and feat.tags.get("aeroway") in AIRPORT_PAVED:
                continue
            if cat == "railway" and is_rail_area(feat.tags) and _is_polygon(feat):
                continue
            rings = _feature_coords_px(feat, proj)
            colour = _route_fill(cat, feat.tags)
            if cat in ROAD_WIDTHS_M and not _is_polygon(feat):
                width_px = _way_width_m(feat, cat) / meters_per_tile
                drawn = ground_rings(feat)
                _draw_line(l_draw, drawn, colour, int(width_px))
                _mark(cat, feat, drawn, int(width_px))
            elif feat.kind == "relation":
                _paint_multipolygon(landscape, feat, proj, colour)
                _mark(cat, feat, rings)
            elif _is_polygon(feat):
                _draw_polygon(l_draw, rings, colour)
                _mark(cat, feat, rings)
            else:
                # A river or canal mapped only as its centre line.
                metres = WATERWAY_WIDTH_M.get(feat.tags.get("waterway"), 3.0)
                width_px = max(1, int(metres / meters_per_tile))
                _draw_line(l_draw, rings, fill, width_px)
                _mark(cat, feat, rings, width_px)
            show(landscape, stage=paint_stage)
        if cat == "industrial":
            yard = LANDSCAPE_FILL["railway"]
            for feat in paint_buckets.get("railway", []):
                if not (is_rail_area(feat.tags) and _is_polygon(feat)):
                    continue
                _paint_filled(l_draw, landscape, feat, proj, yard)
                _mark("railway", feat, _feature_coords_px(feat, proj))
                show(landscape, stage=paint_stage)

    # Runways, taxiways and aprons. After every land-cover polygon, so the
    # infield grass does not erase them, and before the shape clip.
    for feat in paint_buckets.get("airport", []):
        if feat.tags.get("aeroway") not in AIRPORT_PAVED:
            continue
        if _is_polygon(feat):
            _paint_filled(l_draw, landscape, feat, proj, C.DARK_ASPHALT)
            _mark("parking", feat, _feature_coords_px(feat, proj))
        else:
            width_px = _aeroway_width_m(feat) / meters_per_tile
            drawn = ground_rings(feat)
            _draw_line(l_draw, drawn, C.DARK_ASPHALT, max(1, int(width_px)))
            cover.line(drawn, biomes.DIRT, max(1, int(width_px)))
        show(landscape, stage=paint_stage)

    # Bridges over water squared to the tiles (generator/structures.py).
    for deck, cat in lifted.straight:
        l_draw.rectangle(deck.bounds, fill=LANDSCAPE_FILL.get(cat, C.MEDIUM_ASPHALT))
        cover.rectangle(deck.bounds, biomes.pixel_for(cat, {}, cover.climate))
    # Corners, roundabout islands and turning circles, before the kerbs are
    # read off the edge between tarmac and pavement. A junction on a deck is
    # not a junction on the ground.
    building_rings = [ring for feat in building_feats
                      for ring in _feature_coords_px(feat, proj) if len(ring) >= 3]
    intersections.shape_ground(
        landscape, junctions, proj, cover, building_rings, set(lifted.cut),
        [deck.bounds for deck, _cat in lifted.straight])
    blocks = _pave_dense_ground(landscape, building_feats, paint_buckets, proj,
                                cover=cover)
    for paved in lifted.paving:
        l_draw.polygon(list(paved.exterior.coords), fill=C.PAVING)
        cover.polygon([list(paved.exterior.coords)], biomes.TOWN)
    keep = None
    if clip is not None:
        keep = _clip_to_shape(landscape, clip, paint_buckets, proj)
    _weather_roads(landscape, proj)
    # Streets already went out while they were drawn. This frame is the
    # ground between them, when any block was paved, weathered roads included.
    show(landscape, force=True, stage="City blocks" if blocks else "Streets")

    knoxstop.check(should_stop, "the terrain")
    _paint_vegetation(vegetation, landscape, paint_veg, proj,
                      density=tree_density)
    _stamp_biome_woods(cover, paint_veg, proj)
    cover.flush_woods()
    cover.apply_shores()
    if keep is not None:
        cover.apply_keep(keep)
    _paint_gardens(vegetation, landscape, building_feats, proj, density=tree_density)
    _paint_wild_growth(vegetation, landscape, proj, density=tree_density)
    _clear_building_vegetation(vegetation, building_feats, proj)
    # An airfield is mown grass. Wild growth and garden trees treat that
    # colour as open country, which would plant a wood across the runway.
    _clear_building_vegetation(
        vegetation,
        [f for f in paint_buckets.get("airport", []) if _is_polygon(f)],
        proj)
    _clear_route_vegetation(vegetation, paint_buckets, proj)
    show(_build_preview(landscape, vegetation), force=True, stage="Vegetation")
    show(None, force=True, stage="Road markings and kerbs")
    _paint_road_details(vegetation, landscape, buckets, proj, junctions.skip_rects())
    show(None, force=True, stage="Intersections")
    reserved = intersections.paint_controls(vegetation, landscape, junctions, proj)
    show(None, force=True, stage="Street furniture")
    _paint_street_furniture(vegetation, landscape, reserved)
    # Nothing grows through a deck or a ramp, and no lamp stands on one.
    veg_px = vegetation.load()
    for x, y in lifted.clear_veg:
        if 0 <= x < proj.width and 0 <= y < proj.height:
            veg_px[x, y] = C.VEG_NOTHING
    structures.rail_water(lifted, landscape, C.WATER)
    ground_px = landscape.load()
    under = {C.WATER, C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT,
             C.LIGHT_ASPHALT, C.PALE_CONCRETE}
    structures.settle_posts(lifted, lambda x, y: 0 <= x < proj.width and 0 <= y < proj.height
                            and ground_px[x, y] not in under)

    # The spawn density image and the visual preview only read the finished
    # bitmaps, so compose them at the same time.
    spawn_w = proj.width // C.SPAWN_MAP_SCALE
    spawn_h = proj.height // C.SPAWN_MAP_SCALE
    show(None, force=True, stage="Compositing output images")
    with ThreadPoolExecutor(max_workers=2,
                            thread_name_prefix="knox-compose") as executor:
        spawn_future = executor.submit(
            _build_spawn_map, landscape, spawn_w, spawn_h, spawn_density)
        preview_future = executor.submit(_build_preview, landscape, vegetation)
        spawn_map = spawn_future.result()
        preview = preview_future.result()
    show(preview, force=True, stage="Writing map files")

    # --- output files ---
    os.makedirs(output_dir, exist_ok=True)
    landscape_path = os.path.join(output_dir, f"{map_name}.bmp")
    veg_path = os.path.join(output_dir, f"{map_name}_veg.bmp")
    spawn_path = os.path.join(output_dir, f"{map_name}_ZombieSpawnMap.bmp")
    preview_path = os.path.join(output_dir, f"{map_name}_preview.png")
    buildings_path = os.path.join(output_dir, f"{map_name}_buildings.geojson")
    meta_path = os.path.join(output_dir, f"{map_name}_info.json")

    cells_x, cells_y = proj.cell_grid()
    metadata = {
        "map_name": map_name,
        "bbox": {"south": south, "west": west, "north": north, "east": east},
        "rotation": rotation,
        "osm_cache": osm_cache,
        "straight_roads": bool(straight_roads),
        "osm_bbox": list(osm_bbox) if osm_bbox else None,
        "shape": shape,
        "meters_per_tile": meters_per_tile,
        "width_tiles": proj.width,
        "height_tiles": proj.height,
        "cells_x": cells_x,
        "cells_y": cells_y,
        "spawn_density_max": spawn_density,
        "building_count": len(building_feats),
        "feature_count": sum(len(v) for v in buckets.values()),
        "houses_from_addresses": len(addressed),
        "houses_from_streets": len(streeted),
        "guide_reference": "Thuztor Mapping Guide v0.2",
        "grid": proj.grid_dict(),
        # Climate class used to pick Build 42 biomes. The cell PNGs are
        # written later, once the map has a world origin (knoxbuild).
        "biome": {
            "climate": cover.climate,
            "open": cover.open,
            "cell_tiles": biomes.BIOME_CELL,
        },
    }

    def save_base(image, path, base_path):
        import shutil
        image.save(path, format="BMP")
        # Building generation paints paths into the working bitmap. Its base
        # copy must stay byte-for-byte identical to this original.
        shutil.copyfile(path, base_path)

    def save_image(image, path, image_format):
        image.save(path, format=image_format)

    def save_json(path, make_data, **options):
        # json.dump walks the pure-Python encoder, one dict and list at a
        # time. dumps() is that same encoder in C, so the text matches and
        # the file is one write. Text mode stays, so an indented file still
        # gets the platform newline translation dump() applied.
        text = json.dumps(make_data(), **options)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    roads = [feat for cat in ROAD_EXPORT_CATEGORIES
             for feat in buckets.get(cat, [])]
    output_jobs = {
        "landscape": lambda: save_base(
            landscape, landscape_path,
            os.path.join(output_dir, f"{map_name}_ground_base.bmp")),
        "vegetation": lambda: save_base(
            vegetation, veg_path,
            os.path.join(output_dir, f"{map_name}_veg_base.bmp")),
        "zombie map": lambda: save_image(spawn_map, spawn_path, "BMP"),
        "preview": lambda: save_image(preview, preview_path, "PNG"),
        "buildings": lambda: save_json(
            buildings_path, lambda: _buildings_geojson(building_feats)),
        "areas": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_areas.geojson"),
            lambda: _areas_geojson(buckets)),
        "roads": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_roads.geojson"),
            lambda: _roads_geojson(roads, classify)),
        "junctions": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_junctions.json"),
            lambda: intersections.to_json(junctions)),
        "fences": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_fences.geojson"),
            lambda: _lines_geojson([f for f in fence_feats
                                    if clip is None or _inside(f, proj, clip)])),
        "structures": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_structures.json"),
            lambda: {"bridges": lifted.bridges, "monuments": lifted.monuments,
                     "tiles": lifted.rows()}),
        "places": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_places.json"),
            lambda: _places(place_feats, proj), ensure_ascii=False),
        "metadata": lambda: save_json(meta_path, lambda: metadata, indent=2),
        "biomes": lambda: _write_biomes(
            cover, output_dir, map_name),
        "zones": lambda: save_json(
            os.path.join(output_dir, f"{map_name}_zones.geojson"),
            lambda: _zones_geojson(buckets)),
    }
    workers = min(len(output_jobs), os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="knox-output") as executor:
        pending = {executor.submit(job): name for name, job in output_jobs.items()}
        completed = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                pending.pop(future)
                future.result()
                completed += 1
                show(None, force=True,
                     stage=f"Writing map files ({completed} of {len(output_jobs)})")

    return RenderResult(
        landscape_path=landscape_path,
        vegetation_path=veg_path,
        spawn_map_path=spawn_path,
        preview_path=preview_path,
        buildings_geojson_path=buildings_path,
        meta_path=meta_path,
        width=proj.width,
        height=proj.height,
        cells_x=cells_x,
        cells_y=cells_y,
    )


# A city block is paved when buildings cover at least this share of it - the
# same line knoxbuild draws between a city and a suburb.
# Measured: central Paris, Kadikoy and a Tokyo neighbourhood sit at 0.4-0.5, a
# US suburb and a French village around 0.16.
DENSE_COVERAGE = 0.28
# Mapped green space keeps its grass however built-up the area around it is.
GREEN_CATEGORIES = {"park", "grass", "sports", "cemetery", "orchard", "farmland",
                    "wetland", "hospital_grounds", "airport"}


def sea_polygons(feats: list[OSMFeature], proj: Projector) -> list:
    """The sea inside the map, as shapely polygons in tile coordinates.

    OSM maps the sea only by its shore: natural=coastline ways, each running
    with the land on its left and the water on its right. The map rectangle is
    cut along every shore line into faces, and each face is sea or land by
    which side of its nearest stretch of shore it lies on. That handles a
    headland, a bay, an island - any mix, as long as the shore is in view.
    A map with no shore in it is taken to be land.
    """
    from shapely.geometry import LineString, Point, box
    from shapely.ops import nearest_points, polygonize, unary_union

    w, h = proj.width, proj.height
    frame = box(0, 0, w, h)
    shores = []
    for feat in feats:
        if feat.kind != "way" or feat.tags.get("natural") != "coastline":
            continue
        pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
        if len(pts) >= 2:
            shores.append(LineString(pts))
    if not shores:
        return []
    # Join the shore into as few lines as possible, then carry any end that
    # stops inside the map on in its own direction to beyond the edge. The
    # download holds only coast that touches the area, so a shore that wanders
    # out of it and back arrives in pieces - and a line that does not cross
    # the map cannot split it, which left whole bays as dry land.
    from shapely.ops import linemerge
    merged = linemerge(unary_union(shores)) if len(shores) > 1 else shores[0]
    pieces = [merged] if isinstance(merged, LineString) else list(getattr(merged, "geoms", []))
    far = 3 * (w + h)
    inner = frame.buffer(-1)
    shores = []
    for line in pieces:
        coords = list(line.coords)
        if len(coords) < 2:
            continue
        if coords[0] == coords[-1]:
            shores.append(line)          # an island: closed, nothing to extend
            continue
        for end, before in ((0, 1), (-1, -2)):
            ex, ey = coords[end]
            if inner.contains(Point(ex, ey)):
                bx, by = coords[before]
                dx, dy = ex - bx, ey - by
                length = math.hypot(dx, dy) or 1.0
                tip = (ex + dx / length * far, ey + dy / length * far)
                coords = [tip] + coords if end == 0 else coords + [tip]
        shores.append(LineString(coords))
    # Clip the shore to a little beyond the frame, so lines that only graze
    # the edge still split it, and the faces stay inside the map.
    reach = frame.buffer(2)
    clipped = [s.intersection(reach) for s in shores]
    lines = [g for c in clipped for g in (getattr(c, "geoms", None) or [c])
             if not g.is_empty and g.length > 0]
    if not lines:
        return []
    faces = list(polygonize(unary_union(lines + [frame.exterior])))

    def seaward(face) -> bool:
        spot = face.representative_point()
        best, best_d = None, None
        for line in lines:
            d = line.distance(spot)
            if best_d is None or d < best_d:
                best, best_d = line, d
        coords = list(best.coords)
        at = best.project(nearest_points(best, spot)[0])
        # The segment the nearest point falls on.
        run = 0.0
        for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
            seg = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if run + seg >= at or (x2, y2) == coords[-1]:
                break
            run += seg
        cross = (x2 - x1) * (spot.y - y1) - (y2 - y1) * (spot.x - x1)
        # Tile y grows southward, which mirrors the plane: "right of the line"
        # has a positive cross product here, where it is negative on a map.
        return cross > 0

    return [f.intersection(frame) for f in faces
            if f.intersection(frame).area > 1 and seaward(f)]


def _real_kerbs(sides):
    """Keep kerbs that run along a straight edge; drop the steps of a staircase.

    Where a road crosses the tile grid at an angle its edge steps one tile
    every row, and a corner kerb on every step drew the edge as a row of
    teeth. A straight kerb stays when it is part of a run of at least three
    along the same edge (a corner may end the run); a corner stays only where straight
    kerbs lead into it on both arms. Staircase edges are left to the ground
    blends, which soften them.
    """
    import numpy as np

    W, N, S, E = 1, 2, 4, 8
    NW, SW, NE, SE = W | N, S | W, N | E, S | E
    padded = np.pad(sides, 2)
    core = (slice(2, -2), slice(2, -2))
    left, right = padded[2:-2, 1:-3], padded[2:-2, 3:-1]
    left2, right2 = padded[2:-2, :-4], padded[2:-2, 4:]
    up, down = padded[1:-3, 2:-2], padded[3:-1, 2:-2]
    up2, down2 = padded[:-4, 2:-2], padded[4:, 2:-2]
    del core

    def run3(here, a, a2, b, b2, allowed):
        # At least three in a row along the edge, this tile included.
        ia, ia2, ib, ib2 = (np.isin(x, allowed) for x in (a, a2, b, b2))
        return here & ((ia & ib) | (ia & ia2) | (ib & ib2))

    keep = np.zeros(sides.shape, dtype=bool)
    keep |= run3(sides == N, left, left2, right, right2, (N, NW, NE))
    keep |= run3(sides == S, left, left2, right, right2, (S, SW, SE))
    keep |= run3(sides == W, up, up2, down, down2, (W, NW, SW))
    keep |= run3(sides == E, up, up2, down, down2, (E, NE, SE))
    keep |= (sides == NW) & (right == N) & (down == W)
    keep |= (sides == NE) & (left == N) & (down == E)
    keep |= (sides == SW) & (right == S) & (up == W)
    keep |= (sides == SE) & (left == S) & (up == E)
    return np.where(keep, sides, 0).astype(sides.dtype)


def _smooth_road_edges(landscape: Image.Image, asphalt, is_colour) -> None:
    """Fill one-tile notches along the edge of the road, and shave one-tile bumps.

    A street a degree or two off the tile grid rasterises with its edge
    stepping back and forth by a tile; kerbs follow every step, and the edge
    came out as a row of teeth. A pavement tile with road on both sides of it
    along a line becomes road, and a road tile with pavement on both sides
    becomes pavement.
    """
    import numpy as np

    w, h = landscape.size
    strip = 1024
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        top, bottom = max(0, y0 - 1), min(h, y1 + 1)
        ground = np.asarray(landscape.crop((0, top, w, bottom))).copy()
        for _ in range(2):
            road = is_colour(ground, asphalt)
            pave = is_colour(ground, (C.PALE_CONCRETE,))
            notch = np.zeros_like(road)
            notch[1:-1, :] |= road[:-2, :] & road[2:, :]
            notch[:, 1:-1] |= road[:, :-2] & road[:, 2:]
            notch &= pave
            bump = np.zeros_like(road)
            bump[1:-1, :] |= pave[:-2, :] & pave[2:, :]
            bump[:, 1:-1] |= pave[:, :-2] & pave[:, 2:]
            bump &= road
            if not notch.any() and not bump.any():
                break
            # A filled notch takes the tarmac of the road beside it.
            ys, xs = np.nonzero(notch)
            for y, x in zip(ys, xs):
                if y > 0 and road[y - 1, x]:
                    ground[y, x] = ground[y - 1, x]
                elif x > 0 and road[y, x - 1]:
                    ground[y, x] = ground[y, x - 1]
                elif y + 1 < road.shape[0] and road[y + 1, x]:
                    ground[y, x] = ground[y + 1, x]
                else:
                    ground[y, x] = ground[y, x + 1]
            ground[bump] = C.PALE_CONCRETE
        rows = slice(y0 - top, y0 - top + (y1 - y0))
        landscape.paste(Image.fromarray(ground[rows]), (0, y0))


def _paint_road_details(veg: Image.Image, landscape: Image.Image,
                        buckets: dict[str, list[OSMFeature]], proj: Projector,
                        skip_rects=()) -> None:
    """Kerbs where pavement meets the road, and lines down two-lane roads.

    Asphalt ran straight into the pavement with nothing between them, and a
    road wider than a car had nothing painted on it. The vanilla map has both
    on every street - laid by hand - and without them a street reads as a grey
    patch rather than a road.
    """
    import numpy as np

    w, h = landscape.size
    asphalt = (C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT,
               C.DARK_POTHOLE, C.LIGHT_POTHOLE)

    def is_colour(ground, colours):
        mask = np.zeros(ground.shape[:2], dtype=bool)
        for colour in colours:
            same = ground[:, :, 0] == colour[0]
            same &= ground[:, :, 1] == colour[1]
            same &= ground[:, :, 2] == colour[2]
            mask |= same
        return mask

    _smooth_road_edges(landscape, asphalt, is_colour)

    # Kerbs, a strip at a time, each strip read with a row of overlap so the
    # tiles either side of a seam still see their neighbours.
    kerb_for = {1: C.KERB_W, 2: C.KERB_N, 4: C.KERB_S, 8: C.KERB_E,
                3: C.KERB_NW, 5: C.KERB_SW, 10: C.KERB_NE, 12: C.KERB_SE}
    strip = 1024
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        top, bottom = max(0, y0 - 1), min(h, y1 + 1)
        ground = np.asarray(landscape.crop((0, top, w, bottom)))
        road = is_colour(ground, asphalt)
        pave = is_colour(ground, (C.PALE_CONCRETE,))
        sides = np.zeros(road.shape, dtype=np.uint8)
        sides[:, 1:] |= road[:, :-1] * np.uint8(1)     # road to the west
        sides[1:, :] |= road[:-1, :] * np.uint8(2)     # road to the north
        sides[:-1, :] |= road[1:, :] * np.uint8(4)     # road to the south
        sides[:, :-1] |= road[:, 1:] * np.uint8(8)     # road to the east
        sides[~pave] = 0
        sides = _real_kerbs(sides)
        rows = slice(y0 - top, y0 - top + (y1 - y0))
        sides = sides[rows]
        existing = np.asarray(veg.crop((0, y0, w, y1)))
        free = (existing.reshape(-1, 3).max(axis=1) == 0).reshape(sides.shape)
        out = existing.copy()
        for bits, colour in kerb_for.items():
            out[(sides == bits) & free] = colour
        veg.paste(Image.fromarray(out), (0, y0))

    # Centre lines, on roads wide enough for two lanes and straight enough to
    # run along one axis of the tile grid; a line stepping round a diagonal
    # reads as a zigzag, so those are left plain.
    mpt = proj.meters_per_tile
    marks = []
    for cat, style in (("road_major", "yellow"), ("road_medium", "white")):
        for feat in buckets.get(cat, []):
            if _is_polygon(feat):
                continue
            # A roundabout is one-way whether or not the tag says so, and a
            # line around the ring would run through the island.
            if feat.tags.get("junction") in {"roundabout", "circular"}:
                continue
            width = _way_width_m(feat, cat) / mpt
            if width < 6 or feat.tags.get("oneway") in ("yes", "1", "-1"):
                continue
            for ring in _feature_coords_px(feat, proj):
                for (ax, ay), (bx, by) in zip(ring, ring[1:]):
                    dx, dy = bx - ax, by - ay
                    if abs(dy) <= 0.2 * abs(dx):
                        marks.append((style, "N", ax, ay, bx, by, width))
                    elif abs(dx) <= 0.2 * abs(dy):
                        marks.append((style, "W", ax, ay, bx, by, width))
    if not marks:
        return
    # Junction boxes, so a centre line stops at the stop line instead of
    # running through the crossroads. Built once; the line walk is per tile.
    blocked = set()
    for x0, y0, x1, y1 in skip_rects:
        for y in range(max(0, int(y0)), min(h, int(y1))):
            for x in range(max(0, int(x0)), min(w, int(x1))):
                blocked.add((x, y))
    colour_for = {("yellow", "N"): C.LINE_YELLOW_N, ("yellow", "W"): C.LINE_YELLOW_W,
                  ("white", "N"): C.LINE_WHITE_N, ("white", "W"): C.LINE_WHITE_W}
    ground_px = landscape.load()
    veg_px = veg.load()

    def road_at(x, y):
        return 0 <= x < w and 0 <= y < h and ground_px[x, y] in asphalt

    for style, edge, ax, ay, bx, by, width in marks:
        steps = int(max(abs(bx - ax), abs(by - ay)))
        for i in range(steps + 1):
            t = i / steps if steps else 0
            # The line sits on the edge between two tiles, so the road's
            # middle is rounded to the nearest tile boundary.
            x = int(round(ax + (bx - ax) * t))
            y = int(round(ay + (by - ay) * t))
            along = x if edge == "N" else y
            if style == "white" and (along // 3) % 2:
                continue
            if (x, y) in blocked:
                continue
            if not (road_at(x, y) and (road_at(x, y - 1) if edge == "N" else road_at(x - 1, y))):
                continue
            # At a junction the tarmac runs on across the line; a line through
            # the middle of a crossroads is wrong, so stop short of it.
            run = 0
            for k in range(1, int(width) + 4):
                if edge == "N":
                    run += road_at(x, y - k) + road_at(x, y + k - 1)
                else:
                    run += road_at(x - k, y) + road_at(x + k - 1, y)
            if run > width + 3:
                continue
            if veg_px[x, y] == C.VEG_NOTHING:
                veg_px[x, y] = colour_for[(style, edge)]


# How often the vanilla streets have each, measured on their squares
# (tools/building_stats.py's neighbourhoods): grime on 9% of the tarmac, cracks
# on under 0.5%, a lamp per ~290 squares of street, and so on. Spacings are in
# tiles along the kerb.
GRIME_SHARE = 0.14
# Roads at least this wide get a faded edge line down each side.
EDGE_LINE_MIN_WIDTH = 5
CRACK_SHARE = 0.012
LITTER_SHARE = 0.002
LAMP_EVERY = 26
# A speed limit sign every so many tiles along a main street's kerb (Erika's
# Tiles only), with the limit going by how wide the carriageway is.
SPEED_SIGN_EVERY = 90
SPEED_BY_WIDTH = ((12, 45), (8, 35), (0, 25))


def _mod_tiles_ready() -> bool:
    """Whether maps may use Erika's Tiles: installed and set up, and not
    turned off with KNOXMAP_NO_MOD_TILES=1."""
    if os.environ.get("KNOXMAP_NO_MOD_TILES") == "1":
        return False
    try:
        import knoxpaths
        if not knoxpaths.erikas_tiles_ready():
            return False
        # Tools set up before the signs existed have no rule for them, and a
        # colour without a rule is dropped with a warning: run Setup again.
        rules = knoxpaths.mapping_tools_dir() / "config" / "Rules.txt"
        return "KnoxMap road Speed limit 25 S" in rules.read_text(encoding="utf-8", errors="replace")
    except Exception:      # noqa: BLE001 - no tools, no mod tiles
        return False
HYDRANT_EVERY = 70
DRAIN_EVERY = 34


def _paint_street_furniture(veg: Image.Image, landscape: Image.Image,
                            reserved=None) -> dict:
    """Lamps, hydrants and drains along the kerbs; grime, cracks and litter.

    A generated street was clean tarmac, kerb and pavement and nothing else;
    Knox County's have all of these, and a road without them reads as a
    diagram. They go by the kerbs already painted: a lamp stands on the
    pavement one tile back from a straight kerb with its arm over the road, a
    hydrant likewise, a drain in the gutter in front of it.
    """
    import numpy as np

    rng = np.random.default_rng(4321)
    ground = np.asarray(landscape.convert("RGB"))
    vp = np.array(veg.convert("RGB"))
    h, w = ground.shape[:2]

    def is_colour(arr, colours):
        mask = np.zeros(arr.shape[:2], dtype=bool)
        for c in colours:
            mask |= np.all(arr == c, axis=2)
        return mask

    empty = np.all(vp == 0, axis=2)
    asphalt = is_colour(ground, (C.MEDIUM_ASPHALT, C.DARKEST_ASPHALT))
    pavement = is_colour(ground, (C.PALE_CONCRETE,))
    counts = {}

    # Edge lines: tarmac with the road's edge on exactly one side and enough
    # road across from it to be a carriageway, not a car park aisle's end.
    # (Dark tarmac, 100, is car parks and yards, and is left out.)
    k = EDGE_LINE_MIN_WIDTH
    edge_lines = 0
    for colour, (dx, dy) in ((C.EDGE_LINE_W, (-1, 0)), (C.EDGE_LINE_E, (1, 0)),
                             (C.EDGE_LINE_N, (0, -1)), (C.EDGE_LINE_S, (0, 1))):
        outside = np.zeros_like(asphalt)
        across = np.ones_like(asphalt)
        if dx:
            if dx < 0:
                outside[:, 1:] = ~asphalt[:, :-1]
                for i in range(1, k):
                    across[:, :-i] &= asphalt[:, i:]
                    across[:, -i:] = False
            else:
                outside[:, :-1] = ~asphalt[:, 1:]
                for i in range(1, k):
                    across[:, i:] &= asphalt[:, :-i]
                    across[:, :i] = False
            side_a = np.zeros_like(asphalt); side_b = np.zeros_like(asphalt)
            side_a[1:, :] = asphalt[:-1, :]; side_b[:-1, :] = asphalt[1:, :]
        else:
            if dy < 0:
                outside[1:, :] = ~asphalt[:-1, :]
                for i in range(1, k):
                    across[:-i, :] &= asphalt[i:, :]
                    across[-i:, :] = False
            else:
                outside[:-1, :] = ~asphalt[1:, :]
                for i in range(1, k):
                    across[i:, :] &= asphalt[:-i, :]
                    across[:i, :] = False
            side_a = np.zeros_like(asphalt); side_b = np.zeros_like(asphalt)
            side_a[:, 1:] = asphalt[:, :-1]; side_b[:, :-1] = asphalt[:, 1:]
        # Straight edges only: the road continues along the line both ways.
        pick = asphalt & outside & across & side_a & side_b & empty
        vp[pick] = colour
        empty &= ~pick
        edge_lines += int(pick.sum())
    counts["edge lines"] = edge_lines

    # Along each straight edge of the carriageway, road on one side: the lamp
    # stands a tile or two off the tarmac (past the kerb or on the verge) with
    # its arm reaching back over the road, the drain sits in the gutter.
    # (edge colour just painted, step away from the road, lamp colour)
    straight = [(C.EDGE_LINE_W, (-1, 0), C.LAMP_E), (C.EDGE_LINE_E, (1, 0), C.LAMP_W),
                (C.EDGE_LINE_N, (0, -1), C.LAMP_S), (C.EDGE_LINE_S, (0, 1), C.LAMP_N)]
    taken: dict[str, set] = {"lamp": set(), "hydrant": set(), "drain": set(), "speed": set()}
    out = vp.copy()
    # A signalised junction gets a lamp on each pavement corner, before the
    # regular spacing claims the cell.
    if reserved is not None:
        for lx, ly, colour in reserved.lamps:
            if not (0 <= lx < w and 0 <= ly < h):
                continue
            if not empty[ly, lx] or asphalt[ly, lx]:
                continue
            out[ly, lx] = colour
            empty[ly, lx] = False
            taken["lamp"].add((lx // LAMP_EVERY, ly // LAMP_EVERY, None))
            counts["lamp"] = counts.get("lamp", 0) + 1
    # Speed signs stand where drivers keeping right see their faces: on the
    # east kerb of a north-south road facing south, the north kerb of an
    # east-west one facing east. Erika's signs only face those two ways.
    speed_side = {C.EDGE_LINE_E: "S", C.EDGE_LINE_N: "E"} if _mod_tiles_ready() else {}

    def carriageway(gx, gy, sx, sy):
        n = 0
        while n < 24 and 0 <= gx - n * sx < w and 0 <= gy - n * sy < h \
                and asphalt[gy - n * sy, gx - n * sx]:
            n += 1
        return n

    for edge, (sx, sy), lamp in straight:
        ys, xs = np.nonzero(np.all(vp == edge, axis=2))
        order = rng.permutation(len(xs))
        for i in order.tolist():
            gx, gy = int(xs[i]), int(ys[i])     # the gutter square itself
            bx, by = gx + 2 * sx, gy + 2 * sy   # past the kerb or the verge
            if not (0 <= bx < w and 0 <= by < h):
                continue
            facing = speed_side.get(edge)
            speed = None
            if facing:
                across = carriageway(gx, gy, sx, sy)
                limit = next(v for width, v in SPEED_BY_WIDTH if across >= width)
                speed = C.SPEED_SIGNS[(limit, facing)]
            for what, every, colour, at, need in (
                    ("speed", SPEED_SIGN_EVERY, speed, (bx, by), None),
                    ("lamp", LAMP_EVERY, lamp, (bx, by), None),
                    ("hydrant", HYDRANT_EVERY, C.HYDRANT, (bx, by), None),
                    ("drain", DRAIN_EVERY, C.STORM_DRAIN, (gx, gy), None)):
                px, py = at
                if colour is None or not (0 <= px < w and 0 <= py < h):
                    continue
                # Signs, lines and the junction box are already spoken for.
                if reserved is not None and reserved.covers(px, py):
                    continue
                # Speed signs keep a spacing per facing: the north-south roads,
                # met first, took every cell and left east-west ones none.
                cell = (px // every, py // every, facing if what == "speed" else None)
                if cell in taken[what]:
                    continue
                # A drain takes the gutter square from its edge line; a lamp
                # or hydrant needs an empty square off the road.
                if what != "drain" and (not empty[py, px] or asphalt[py, px]):
                    continue
                taken[what].add(cell)
                out[py, px] = colour
                empty[py, px] = False
                counts[what] = counts.get(what, 0) + 1
                break

    # Wear comes in patches, as it does on a real road and on the vanilla
    # map; picked square by square it came out as a chequerboard. Smooth noise
    # - coarse random values, blurred up to full size - gives the patches.
    coarse = Image.fromarray((rng.random((max(1, h // 5), max(1, w // 5))) * 255).astype(np.uint8))
    noise = np.asarray(coarse.resize((w, h), Image.BICUBIC)
                       .filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    level = np.quantile(noise[asphalt], 1 - GRIME_SHARE) if asphalt.any() else 256
    pick = asphalt & empty & (noise >= level)
    out[pick] = C.ASPHALT_GRIME
    empty &= ~pick
    counts["grime"] = int(pick.sum())
    # Scattered things: each square rolls once, and each kind has its own band
    # of the roll, so no square gets two.
    roll = rng.random((h, w))
    # Litter stays off the junction. Grime and cracks may still land there:
    # wear does not stop for a stop line.
    held = np.zeros((h, w), dtype=bool)
    if reserved is not None:
        for x0, y0, x1, y1 in reserved.rects:
            held[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
        for tx, ty in reserved.tiles:
            if 0 <= tx < w and 0 <= ty < h:
                held[ty, tx] = True
    lo = 0.0
    for mask, share, colour, name in (
            (asphalt, CRACK_SHARE, C.ASPHALT_CRACKS, "cracks"),
            (pavement, LITTER_SHARE, C.LITTER, "litter")):
        pick = mask & empty & (roll >= lo) & (roll < lo + share)
        if name == "litter":
            pick &= ~held
        lo += share
        out[pick] = colour
        empty &= ~pick
        counts[name] = int(pick.sum())
    veg.paste(Image.fromarray(out), (0, 0))
    return counts


def _pave_dense_ground(landscape: Image.Image, building_feats: list[OSMFeature],
                       buckets: dict[str, list[OSMFeature]], proj: Projector,
                       cover=None):
    """Pave the unmapped ground of built-up city blocks.

    Land OSM says nothing about is painted as wild grass, which is right in the
    countryside and wrong in an old town, where it is courtyards, alleys and
    the gaps between blocks. Central Paris came out 77% meadow.

    The decision is made per city block - the ground the streets enclose - on
    how much of that block is under buildings. It used to be made per patch of
    ground on the density nearby, which near the threshold flickered between
    paved and grass across a single block and left it blotched like camouflage.
    A block is paved whole or not at all, as real ones are. Mapped parks,
    gardens and pitches keep their grass inside a paved block; a residential
    area's lawns do not, since between buildings packed this tight they are
    yards.
    """
    import numpy as np
    from shapely import STRtree
    from shapely.geometry import LineString, Polygon, box
    from shapely.ops import polygonize, unary_union

    w, h = landscape.size
    frame = box(0, 0, w, h)
    streets = []
    for cat in ("road_major", "road_medium", "road_minor", "road_service",
                "pedestrian"):
        for feat in buckets.get(cat, []):
            if feat.kind == "way" and not _is_polygon(feat):
                pts = [proj.to_px(la, lo) for la, lo in feat.geometry]
                if len(pts) >= 2:
                    streets.append(LineString(pts))
    if not streets:
        return
    blocks = [b.intersection(frame) for b in
              polygonize(unary_union(streets + [frame.exterior]))]
    blocks = [b for b in blocks if not b.is_empty and b.area > 50]

    footprints = []
    for feat in building_feats:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                poly = Polygon(ring)
                footprints.append(poly if poly.is_valid else poly.buffer(0))
    if not footprints:
        return
    tree = STRtree(footprints)

    paved = Image.new("L", (w, h), 0)
    pd = ImageDraw.Draw(paved)
    any_paved = False
    for block in blocks:
        covered = sum(footprints[i].intersection(block).area
                      for i in tree.query(block))
        if covered / block.area < DENSE_COVERAGE:
            continue
        for part in getattr(block, "geoms", None) or [block]:
            if hasattr(part, "exterior"):
                pd.polygon(list(part.exterior.coords), fill=255)
                for hole in part.interiors:
                    pd.polygon(list(hole.coords), fill=0)
                any_paved = True
    if not any_paved:
        return

    keep = Image.new("L", (w, h), 0)
    kd = ImageDraw.Draw(keep)
    for cat in GREEN_CATEGORIES:
        for feat in buckets.get(cat, []):
            if _is_polygon(feat):
                for ring in _feature_coords_px(feat, proj):
                    if len(ring) >= 3:
                        kd.polygon(ring, fill=255)

    # Strip by strip, so the colour tests never hold the whole map at once.
    strip = 1024
    for y0 in range(0, h, strip):
        y1 = min(h, y0 + strip)
        mask = np.asarray(paved.crop((0, y0, w, y1))) > 0
        mask &= np.asarray(keep.crop((0, y0, w, y1))) == 0
        if not mask.any():
            continue
        ground = np.asarray(landscape.crop((0, y0, w, y1)))
        grass = np.zeros(mask.shape, dtype=bool)
        for colour in (C.DARK_GRASS, C.MEDIUM_GRASS):
            same = ground[:, :, 0] == colour[0]
            same &= ground[:, :, 1] == colour[1]
            same &= ground[:, :, 2] == colour[2]
            grass |= same
        mask &= grass
        if mask.any():
            paint = Image.new("RGB", (w, y1 - y0), C.PALE_CONCRETE)
            landscape.paste(paint, (0, y0), Image.fromarray(mask.astype(np.uint8) * 255))
            if cover is not None:
                cover.paint_mask(mask, biomes.TOWN, y0)
    return True


def _weather_roads(landscape: Image.Image, proj: Projector) -> None:
    """Break up the tarmac with worn patches.

    Every road in a class is otherwise a single flat colour from kerb to kerb
    for its whole length, which is the main reason a generated town looks
    printed rather than driven on. Rules.txt has two pothole shades that blend
    into asphalt, so scattering small blots of them costs nothing in tiles and
    gives the surface some age.

    Patches go on asphalt only - they are skipped over grass, water, pavement
    and building footprints, so nothing outside the carriageway is touched.
    """
    import random

    asphalt = {C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT}
    px = landscape.load()
    rng = random.Random(20250913)
    # One patch per 1500 tiles of map. Denser than this and the roads read as
    # bombed rather than worn.
    attempts = max(1, (proj.width * proj.height) // 1500)
    for _ in range(attempts):
        cx = rng.randrange(proj.width)
        cy = rng.randrange(proj.height)
        if px[cx, cy] not in asphalt:
            continue
        shade = rng.choice((C.DARK_POTHOLE, C.LIGHT_POTHOLE))
        radius = rng.randint(1, 3)
        for y in range(max(0, cy - radius), min(proj.height, cy + radius + 1)):
            for x in range(max(0, cx - radius), min(proj.width, cx + radius + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 > radius * radius:
                    continue
                if px[x, y] in asphalt:
                    px[x, y] = shade


def _stamp_biome_woods(cover, feats: list[OSMFeature], proj: Projector) -> None:
    """Forest and scrub polygons become the climate's woodland biome.

    Roads, water and bare ground are left alone inside ``flush_woods``. A
    single mapped tree is a small patch of the same woodland, not a forest
    zone the size of a field.
    """
    for feat in feats:
        cat = classify(feat.tags)
        rings = _feature_coords_px(feat, proj)
        if cat in {"forest", "scrub"} and _is_polygon(feat):
            holes = ()
            if feat.kind == "relation":
                outers = []
                holes = []
                for role, ring in feat.role_geoms:
                    projected = [proj.to_px(la, lo) for la, lo in ring]
                    if len(projected) < 3:
                        continue
                    if role == "inner":
                        holes.append(projected)
                    else:
                        outers.append(projected)
                rings = outers
            cover.queue_wood(rings, feat.tags, scrub=cat == "scrub", holes=holes)
        elif cat == "tree_single" and rings and rings[0]:
            radius = max(1, int(2 / proj.meters_per_tile))
            cover.queue_wood(rings, feat.tags, radius=radius)


def _paint_vegetation(veg: Image.Image, landscape: Image.Image,
                      feats: list[OSMFeature], proj: Projector,
                      density: float = 1.0) -> None:
    _paint_vegetation_extras(veg, landscape, feats, proj)
    _paint_woodland(veg, landscape,
                    [f for f in feats if classify(f.tags, _is_polygon(f)) in
                     {"forest", "scrub", "tree_single"}], proj, density)


def _paint_woodland(veg: Image.Image, landscape: Image.Image,
                    feats: list[OSMFeature], proj: Projector,
                    density: float = 1.0) -> None:
    """Paint trees on the vegetation bitmap.

    Forest polygons get full density (TREES), scrub becomes bushes+trees,
    single-tree nodes become small dots. Forest edges get downgraded to a
    mix with dark grass so the transition isn't a hard rectangle.

    `density` below 1 thins the woodland by stepping every tile down one
    grade - full trees become a mix with dark grass, a mix becomes sparse,
    sparse becomes nothing - and above 1 it does the reverse. Which matters
    because trees are cover: a map buried in forest plays completely
    differently from the same map with hedgerows.
    """
    mask = Image.new("L", veg.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    scrub_mask = Image.new("L", veg.size, 0)
    scrub_draw = ImageDraw.Draw(scrub_mask)

    for feat in feats:
        cat = classify(feat.tags, _is_polygon(feat))
        rings = _feature_coords_px(feat, proj)
        if cat == "forest":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        mask_draw.polygon(ring, fill=255)
        elif cat == "scrub":
            if _is_polygon(feat):
                for ring in rings:
                    if len(ring) >= 3:
                        scrub_draw.polygon(ring, fill=255)
        elif cat == "tree_single":
            if rings and rings[0]:
                x, y = rings[0][0]
                r = max(1, int(2 / proj.meters_per_tile))
                mask_draw.ellipse((x - r, y - r, x + r, y + r), fill=255)

    # Build a "border band" of the forest mask so we can paint edges lighter.
    eroded = mask.filter(ImageFilter.MinFilter(5))

    # Thickest to thinnest. `density` shifts every tile along this ladder.
    # The shift depends only on density, so it is resolved once: at 1.0 the
    # two forest colours are used as they stand, the way graded() returned
    # its argument unchanged.
    GRADES = [C.VEG_NOTHING, C.SPARSE_TREES, C.TREES_DARK_GRASS, C.TREES]
    if density == 1.0:
        full_shade, edge_shade = C.TREES, C.TREES_DARK_GRASS
    else:
        shift = round((density - 1.0) * 2)
        limit = len(GRADES) - 1
        full_shade = GRADES[min(max(GRADES.index(C.TREES) + shift, 0), limit)]
        edge_shade = GRADES[min(max(
            GRADES.index(C.TREES_DARK_GRASS) + shift, 0), limit)]

    import numpy as np

    # load() treats 0 as false. Scrub is written first, including on water,
    # and a forest shade overwrites it. Water, and a VEG_NOTHING grade, skip
    # that write and leave the scrub (and the ground) as they are.
    veg_px = np.array(veg, copy=True)
    land_px = np.array(landscape, copy=True)
    scrub_hit = np.asarray(scrub_mask) != 0
    forest_hit = np.asarray(mask) != 0
    core = np.asarray(eroded) != 0
    veg_px[scrub_hit] = C.BUSHES_TREES_DARK_GRASS

    # Only paint trees on grass / dirt. Skip water, roads, buildings.
    water = np.all(land_px == C.WATER, axis=2)
    paintable = forest_hit & ~water
    soften = (np.all(land_px == C.MEDIUM_GRASS, axis=2)
              | np.all(land_px == C.LIGHT_GRASS, axis=2))
    for shade, where in ((full_shade, paintable & core),
                         (edge_shade, paintable & ~core)):
        if shade == C.VEG_NOTHING:
            continue
        veg_px[where] = shade
        # Trees on a grass tile → switch the landscape to DARK_GRASS
        # so the PZ renderer is happy (trees sit on dark grass best).
        land_px[where & soften] = C.DARK_GRASS

    veg.paste(Image.fromarray(veg_px), (0, 0))
    landscape.paste(Image.fromarray(land_px), (0, 0))


def _paint_vegetation_extras(veg: Image.Image, landscape: Image.Image,
                             feats: list[OSMFeature], proj: Projector) -> None:
    """Hedges, orchards, cemeteries and wetland, which are not woodland.

    Hedges are rows of bushes a metre or two wide along their line. Orchard
    trees go in on a regular grid - what makes an orchard recognisable from
    the air is that its trees stand in rows. Cemeteries get scattered flowers
    and the odd tree, wetland patches of dense bush.
    """
    import random

    rng = random.Random(0x5EED)
    draw = ImageDraw.Draw(veg)
    land_draw = ImageDraw.Draw(landscape)
    vp = veg.load()
    lp = landscape.load()
    w, h = veg.size

    def polygon_mask(feat):
        mask = Image.new("1", veg.size, 0)
        md = ImageDraw.Draw(mask)
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                md.polygon(ring, fill=1)
        return mask

    for feat in feats:
        cat = classify(feat.tags, _is_polygon(feat))
        if feat.tags.get("barrier") == "hedge":
            width = max(1, int(round(1.5 / proj.meters_per_tile)))
            for ring in _feature_coords_px(feat, proj):
                if len(ring) >= 2:
                    draw.line(ring, fill=C.BUSHES, width=width)
        elif cat in {"orchard", "cemetery", "wetland"} and _is_polygon(feat):
            mask = polygon_mask(feat)
            bbox = mask.getbbox()
            if not bbox:
                continue
            mp = mask.load()
            x0, y0, x1, y1 = bbox
            spacing = max(2, int(round(4 / proj.meters_per_tile)))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if not mp[x, y] or lp[x, y] == C.WATER:
                        continue
                    if cat == "orchard":
                        if x % spacing == 0 and y % spacing == 0:
                            vp[x, y] = C.TREES
                            lp[x, y] = C.DARK_GRASS
                    elif cat == "cemetery":
                        # A few flowers between the graves, not instead of
                        # them: the headstones themselves go in later, as
                        # props (knoxbuild/props.py).
                        roll = rng.random()
                        if roll < 0.012:
                            vp[x, y] = C.TREES
                            lp[x, y] = C.DARK_GRASS
                        elif roll < 0.04:
                            vp[x, y] = C.FLOWERS
                    else:
                        if rng.random() < 0.35:
                            vp[x, y] = C.DENSE_BUSHES_GRASS
                            lp[x, y] = C.DARK_GRASS


# Garden planting. Trees came only from mapped woods, orchards and single-tree
# nodes, and suburbs rarely map their garden trees, so a real leafy suburb
# came out as houses standing on bare lawn. Yards - grass within reach of a
# house, clear of roads and paths - get a tree about every GARDEN_TREE_EVERY
# tiles each way, and a few shrubs against the house walls.
GARDEN_REACH_TILES = 18        # how far from a house its yard reaches
GARDEN_TREE_EVERY = 9
GARDEN_TREE_CHANCE = 0.6
GARDEN_CLEAR_OF_HOUSE = 3      # a tree trunk this far off the wall at least
GARDEN_CLEAR_OF_PAVING = 2     # and off the kerb, drive or path
SHRUB_CHANCE = 0.12            # per tile of lawn along a house wall
# Tufts over open grass. A lawn of one flat grass tile looked like felt beside
# the vanilla map, whose grass is broken up by clumps everywhere.
SHORT_GRASS_SHARE = 0.2
# Long grass is off: its rule mixes in flowerbed tiles, which came out as pink
# and blue squares all over every lawn.
LONG_GRASS_SHARE = 0.0
GRASS_COLOURS = (C.DARK_GRASS, C.MEDIUM_GRASS, C.LIGHT_GRASS)


# Rows of the map worked on at a time in _box_any. The reach used to be a
# summed-area table, four bytes a tile twice over - a 6000 x 5400 map needed
# a third of a gigabyte for one call, which ran a 32-bit Python out of memory.
# Strips keep the scratch to a few megabytes, for the same answer.
BOX_STRIP_ROWS = 512


def _window_any(values, size: int, axis: int):
    """True where any value in a centered window along `axis` is nonzero.

    Outside the array counts as zero, the same border a constant-0 filter uses.
    `size` is odd, so the window sits on the tile rather than between two.
    """
    import numpy as np

    radius = size // 2
    moved = np.moveaxis(values, axis, -1)
    padded = np.pad(moved, [(0, 0)] * (moved.ndim - 1) + [(radius, radius)])
    acc = np.cumsum(padded, axis=-1, dtype=np.int32)
    n = moved.shape[-1]
    hi = acc[..., size - 1:size - 1 + n]
    lo = np.zeros(hi.shape, dtype=np.int32)
    lo[..., 1:] = acc[..., :n - 1]
    return np.moveaxis((hi - lo) > 0, -1, axis)


def _box_any(mask, radius: int):
    """True wherever `mask` is true within `radius` tiles (a square reach)."""
    import numpy as np

    h, w = mask.shape
    # A 1x1 window is the mask. Same answer as the summed-area test at radius 0.
    if radius == 0:
        return np.array(mask, dtype=bool)
    out = np.zeros((h, w), dtype=bool)
    if h == 0 or w == 0:
        return out
    # Any set tile in the square is a row-wise window, then the same down the
    # columns. Outside the map counts as empty, which is what the old padding
    # stood for.
    size = 2 * radius + 1
    for top in range(0, h, BOX_STRIP_ROWS):
        bottom = min(h, top + BOX_STRIP_ROWS)
        y0, y1 = max(0, top - radius), min(h, bottom + radius)
        block = np.ascontiguousarray(mask[y0:y1], dtype=np.uint8)
        horiz = _window_any(block, size, 1)
        vert = _window_any(horiz, size, 0)
        out[top:bottom] = vert[top - y0:bottom - y0]
    return out


def _paint_gardens(veg: Image.Image, landscape: Image.Image,
                   building_feats: list[OSMFeature], proj: Projector,
                   density: float = 1.0) -> int:
    """Trees in yards and shrubs along house walls. Returns trees planted."""
    import numpy as np

    if not building_feats or density <= 0:
        return 0
    houses = Image.new("1", veg.size, 0)
    hd = ImageDraw.Draw(houses)
    for feat in building_feats:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                hd.polygon(ring, fill=1)
    house = np.array(houses, dtype=bool)
    del houses
    ground = np.array(landscape.convert("RGB"))
    # One packed key, then a compare per grass colour. Same tiles as matching
    # the three channels separately.
    key = (ground[..., 0].astype(np.uint32) << 16
           | ground[..., 1].astype(np.uint32) << 8
           | ground[..., 2].astype(np.uint32))
    grass = np.zeros(house.shape, dtype=bool)
    for colour in GRASS_COLOURS:
        packed = (colour[0] << 16) | (colour[1] << 8) | colour[2]
        grass |= key == packed
    del ground, key                 # a town's worth of pixels; let it go
    vegp = np.array(veg.convert("RGB"))
    empty = np.all(vegp == 0, axis=2)
    del vegp
    lawn = grass & empty & ~house
    del empty
    paved_near = _box_any(~grass & ~house, GARDEN_CLEAR_OF_PAVING)
    del grass

    yard = lawn & _box_any(house, GARDEN_REACH_TILES)         & ~_box_any(house, GARDEN_CLEAR_OF_HOUSE) & ~paved_near
    rng = np.random.default_rng(1234)
    step = GARDEN_TREE_EVERY
    chance = min(1.0, GARDEN_TREE_CHANCE * density)
    h, w = house.shape
    ncy = (h + step - 1) // step
    ncx = (w + step - 1) // step
    ncells = ncy * ncx
    # One pass over the yard, then each grid cell's pixels in the order
    # np.nonzero used to hand back for that cell alone. The integer draw
    # therefore picks the same tile it used to.
    ys_all, xs_all = np.nonzero(yard)
    del yard
    starts = np.zeros(ncells + 1, dtype=np.int64)
    if len(xs_all):
        cid = (ys_all // step) * ncx + (xs_all // step)
        order = np.argsort(cid, kind="stable")
        ys_all = ys_all[order]
        xs_all = xs_all[order]
        counts = np.bincount(cid[order], minlength=ncells)
        del cid, order
        np.cumsum(counts, out=starts[1:])
        occ = np.flatnonzero(counts)
        del counts
    else:
        occ = np.empty(0, dtype=np.int64)

    def burn(n):
        # Same draws as n calls to rng.random(), then dropped. An empty cell
        # never asks for an integer, whatever its roll was, so a batch of
        # those rolls leaves every later draw where it was. In pieces, because
        # one array for a whole town is a quarter of a gigabyte of floats.
        piece = 1 << 18
        while n > piece:
            rng.random(piece)
            n -= piece
        if n:
            rng.random(n)

    tree_ys = np.empty(len(occ), dtype=ys_all.dtype)
    tree_xs = np.empty(len(occ), dtype=xs_all.dtype)
    planted = 0
    prev = 0
    for i in occ:
        i = int(i)
        gap = i - prev
        if gap:
            burn(gap)
        prev = i + 1
        # The skip is rolled before the cell is looked at, same as the old
        # loop. Only a cell that passed and actually has yard consumes an
        # integer, and that integer is the index in the cell's nonzero list.
        if rng.random() >= chance:
            continue
        a = int(starts[i])
        b = int(starts[i + 1])
        pick = int(rng.integers(b - a))
        tree_ys[planted] = ys_all[a + pick]
        tree_xs[planted] = xs_all[a + pick]
        planted += 1
    tail = ncells - prev
    if tail:
        burn(tail)
    del starts, occ, ys_all, xs_all

    # Shrubs: lawn right against a wall, not in front of the paving.
    # The dice are rolled for the tiles in question, not for every tile on the
    # map: one roll each way over a town is a quarter of a gigabyte of floats.
    beside = lawn & _box_any(house, 1) & ~house & ~paved_near
    bys, bxs = np.nonzero(beside)
    keep = rng.random(len(bxs)) < SHRUB_CHANCE * density

    # Radius 0 is the house mask itself. Lawn is already clear of it.
    open_grass = lawn & ~beside & ~house
    del beside, paved_near, lawn, house
    gys, gxs = np.nonzero(open_grass)
    del open_grass
    roll = rng.random(len(gxs))

    veg_arr = np.array(veg.convert("RGB"))
    if planted:
        veg_arr[tree_ys[:planted], tree_xs[:planted]] = C.TREES
    if keep.any():
        veg_arr[bys[keep], bxs[keep]] = C.BUSHES
    for colour, share, lo in ((C.SHORT_GRASS, SHORT_GRASS_SHARE, 0.0),
                              (C.GRASS_ON_DARK, LONG_GRASS_SHARE, SHORT_GRASS_SHARE)):
        pick = (roll >= lo) & (roll < lo + share)
        if not pick.any():
            continue
        sy = gys[pick]
        sx = gxs[pick]
        still = np.all(veg_arr[sy, sx] == C.VEG_NOTHING, axis=1)
        if still.any():
            veg_arr[sy[still], sx[still]] = colour
    veg.paste(Image.fromarray(veg_arr, "RGB"))
    return planted


# Growth on land nobody mapped.
#
# Trees came only from mapped woods, orchards, single-tree nodes and the
# gardens of houses, so any ground OpenStreetMap says nothing about came out
# as an unbroken lawn - "more often than not it's the program getting
# confused and not just a field of nothing". Real open country has scrub and
# stands of trees in it wherever nobody is farming or mowing.
#
# It is kept off light grass, which is what farmland and churchyards are
# painted, so a wheat field stays a wheat field. And it clumps: one roll per
# tile puts a tree every so often everywhere, which is an orchard, while a
# slow noise field over the map gives thickets here and open ground there.
WILD_TREE_EVERY_M = 11.0
WILD_CLUMP_M = 70.0            # how far a thicket or a clearing runs
WILD_TREE_CHANCE = 0.75        # at the thickest, per patch of that size
WILD_BUSH_SHARE = 0.45         # of what grows, this much is scrub
WILD_CLEAR_OF_PAVING = 2       # tiles off a kerb, a drive or a path
WILD_GROUND = (C.DARK_GRASS, C.MEDIUM_GRASS)


def _paint_wild_growth(veg: Image.Image, landscape: Image.Image,
                       proj: Projector, density: float = 1.0) -> int:
    """Scatter trees and scrub over open grass. Returns what was planted."""
    import numpy as np

    if density <= 0:
        return 0
    ground = np.array(landscape.convert("RGB"))
    open_ground = np.zeros(ground.shape[:2], dtype=bool)
    for colour in WILD_GROUND:
        open_ground |= np.all(ground == colour, axis=2)
    del ground
    vegp = np.array(veg.convert("RGB"))
    bare = np.all(vegp == 0, axis=2)
    del vegp
    free = open_ground & bare
    del bare
    # Not up against a road, a pavement or a building: those tiles are the
    # verge, and the road details pass has its own plans for them.
    free &= ~_box_any(~open_ground, WILD_CLEAR_OF_PAVING)
    del open_ground

    h, w = free.shape
    step = max(2, int(round(WILD_TREE_EVERY_M / proj.meters_per_tile)))
    clump = max(step * 2, int(round(WILD_CLUMP_M / proj.meters_per_tile)))
    rng = np.random.default_rng(9871)
    field = rng.random(((h // clump) + 2, (w // clump) + 2)) ** 1.6
    px = veg.load()
    planted = 0
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            thickness = field[y0 // clump, x0 // clump]
            if rng.random() >= min(1.0, WILD_TREE_CHANCE * thickness * density):
                continue
            ys, xs = np.nonzero(free[y0:y0 + step, x0:x0 + step])
            if len(xs) == 0:
                continue
            i = int(rng.integers(len(xs)))
            px[int(x0 + xs[i]), int(y0 + ys[i])] = (
                C.BUSHES if rng.random() < WILD_BUSH_SHARE else C.TREES)
            planted += 1
    return planted


def _clear_route_vegetation(veg: Image.Image, buckets: dict, proj: Projector) -> None:
    """No trees on a walking path or a track.

    Woodland is painted over the whole polygon, path included, so a trail
    through a wood came out as trees standing in the dirt.
    """
    mask = Image.new("1", veg.size, 0)
    draw = ImageDraw.Draw(mask)
    hit = False
    for cat in ("dirt_path", "paved_path", "road_track"):
        for feat in buckets.get(cat, []):
            if _is_polygon(feat):
                continue
            width = max(1, int(_way_width_m(feat, cat) / proj.meters_per_tile))
            for ring in _feature_coords_px(feat, proj):
                if len(ring) < 2:
                    continue
                draw.line(ring, fill=1, width=width)
                hit = True
    if hit:
        veg.paste(Image.new("RGB", veg.size, C.VEG_NOTHING), (0, 0), mask)


def _clear_building_vegetation(veg: Image.Image, feats: list[OSMFeature],
                               proj: Projector) -> None:
    """No trees or bushes inside a building.

    Woodland polygons and single-tree nodes are mapped independently of the
    buildings standing in them, so a tree painted under a house came out as a
    tree growing through its living-room floor.
    """
    mask = Image.new("1", veg.size, 0)
    md = ImageDraw.Draw(mask)
    for feat in feats:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) >= 3:
                md.polygon(ring, fill=1)
    blank = Image.new("RGB", veg.size, C.VEG_NOTHING)
    veg.paste(blank, (0, 0), mask)


def _places(feats: list[OSMFeature], proj: Projector) -> list[dict]:
    """Named places with an official population, and where they sit."""
    out = []
    for f in feats:
        raw = str(f.tags.get("population", "")).replace(",", "").replace(" ", "")
        try:
            population = int(float(raw.split(";")[0]))
        except ValueError:
            continue
        la, lo = f.geometry[0]
        x, y = proj.to_px(la, lo)
        out.append({"name": f.tags.get("name", ""), "place": f.tags.get("place"),
                    "population": population, "tile_x": round(x), "tile_y": round(y),
                    "inside": 0 <= x < proj.width and 0 <= y < proj.height})
    return out


def feature_id(feat, index: int) -> str:
    """Stable id for an exported feature. Ways and relations keep their OSM id
    so an edit survives a rebuild of the file; a feature with no id falls back
    to its position, which is stable until the map is generated again."""
    osm_id = getattr(feat, "osm_id", None)
    kind = getattr(feat, "kind", "")
    if osm_id:
        prefix = "r" if kind == "relation" else "w"
        return f"{prefix}{osm_id}"
    return f"i{index}"


def _tagged_feature(feat, features: list, properties: dict, geometry: dict) -> None:
    """Append a GeoJSON Feature. The id is its place in that feature list."""
    fid = feature_id(feat, len(features))
    props = {"fid": fid}
    props.update({k: v for k, v in properties.items() if k != "fid"})
    features.append({
        "type": "Feature",
        "id": fid,
        "properties": props,
        "geometry": geometry,
    })


def _area_polygons(feat: OSMFeature):
    """Outer rings as GeoJSON polygons, or None when this feature is not an area."""
    if not _is_polygon(feat):
        return None
    if feat.kind == "way":
        rings = [[[lon, lat] for lat, lon in feat.geometry]]
    else:
        rings = [[[lon, lat] for lat, lon in ring]
                 for role, ring in feat.role_geoms if role != "inner"]
    polys = [[ring] for ring in rings if len(ring) >= 3]
    return polys or None


def _area_exports(buckets: dict[str, list[OSMFeature]]):
    """Area polygons in the order `_areas.geojson` writes them.

    Edit fids are assigned from this same walk, so a category override finds
    the feature the file named.
    """
    yield from _categorised_areas(buckets, AREA_CATEGORIES)


def _categorised_areas(buckets: dict[str, list[OSMFeature]], categories):
    for cat in categories:
        for feat in buckets.get(cat, []):
            # Track centre lines share the railway category with the yards,
            # and runways share the airport category with the field. Only the
            # grounds are an area a building can stand in.
            if cat == "railway" and not is_rail_area(feat.tags):
                continue
            if cat == "airport" and not is_airport_area(feat.tags):
                continue
            polys = _area_polygons(feat)
            if polys:
                yield cat, feat, polys


def _zones_geojson(buckets: dict[str, list[OSMFeature]]) -> dict:
    """Land-use polygons for the generate-tab zoning overlay."""
    features = []
    for cat, feat, polys in _categorised_areas(buckets, ZONE_CATEGORIES):
        _tagged_feature(feat, features,
                        {"category": cat,
                         **{k: v for k, v in feat.tags.items() if k in AREA_TAGS}},
                        {"type": "MultiPolygon", "coordinates": polys})
    return {"type": "FeatureCollection", "features": features}


def _write_biomes(cover, output_dir: str, map_name: str) -> None:
    """The game's biome index, plus the coloured overlay the window shows."""
    cover.save(os.path.join(output_dir, f"{map_name}_biome.png"))
    cover.save_overlay(os.path.join(output_dir, f"{map_name}_biome_overlay.png"))
    with open(os.path.join(output_dir, f"{map_name}_biome_legend.json"),
              "w", encoding="utf-8") as handle:
        json.dump(biomes.overlay_legend(), handle)


def _roads_geojson(feats, classify) -> dict:
    """Highway centrelines in lon/lat, for the classes the paper map draws."""
    features = []
    for feat in feats:
        if _is_polygon(feat) or feat.kind != "way" or len(feat.geometry) < 2:
            continue
        cat = classify(feat.tags)
        if cat not in ROAD_EXPORT_CATEGORIES:
            continue
        coords = [[lon, lat] for lat, lon in feat.geometry]
        if len(coords) < 2:
            continue
        _tagged_feature(feat, features, {
            "name": feat.tags.get("name") or "",
            "category": cat,
            "width": _way_width_m(feat, cat),
        }, {"type": "LineString", "coordinates": coords})
    return {"type": "FeatureCollection", "features": features}


# Tags that classify() returns as this category, and nothing earlier in that
# function. Vegetation paint calls classify() again, so a recategorised area
# has to carry these instead of its original tags. Ground paint uses the
# bucket, which is set to the same category.
_PAINT_TAGS = {
    "residential": {"landuse": "residential"},
    "commercial": {"landuse": "commercial"},
    "industrial": {"landuse": "industrial"},
    "military": {"landuse": "military"},
    "schoolyard": {"amenity": "school"},
    "hospital_grounds": {"amenity": "hospital"},
    "worship_grounds": {"amenity": "place_of_worship"},
    "cemetery": {"landuse": "cemetery"},
    "parking": {"amenity": "parking"},
    "sports": {"leisure": "sports_centre"},
    "airport": {"aeroway": "aerodrome"},
    "railway": {"landuse": "railway"},
    "orchard": {"landuse": "orchard"},
    "forest": {"landuse": "forest"},
    "scrub": {"natural": "scrub"},
    "tree_single": {"natural": "tree"},
    "hedge": {"barrier": "hedge"},
    "wetland": {"natural": "wetland"},
    "park": {"leisure": "park"},
    "grass": {"landuse": "grass"},
    "farmland": {"landuse": "farmland"},
    "water": {"natural": "water"},
    "sand": {"natural": "sand"},
    "dirt": {"landuse": "brownfield"},
    "playground": {"leisure": "playground"},
    "track": {"leisure": "track"},
    "pool": {"leisure": "swimming_pool"},
    "plaza": {"place": "square"},
}


def _edit_field(rec, key):
    if isinstance(rec, dict):
        return rec.get(key)
    if rec is None:
        return None
    return getattr(rec, key, None)


def _edit_deleted(rec) -> bool:
    flag = _edit_field(rec, "deleted")
    return flag is True or flag == 1


def _edit_category(rec) -> str:
    raw = _edit_field(rec, "category")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _edit_dict(edits, key) -> dict:
    if isinstance(edits, dict):
        raw = edits.get(key)
    else:
        raw = getattr(edits, key, None)
    return raw if isinstance(raw, dict) else {}


def _edit_list(edits, key) -> list:
    if isinstance(edits, dict):
        raw = edits.get(key)
    else:
        raw = getattr(edits, key, None)
    if isinstance(raw, list):
        return raw
    if isinstance(raw, tuple):
        return list(raw)
    return []


def _read_edits_file(out_dir, map_name):
    """areas and added_areas from the overlay file, when edits.py cannot load."""
    path = os.path.join(out_dir, f"{map_name}_edits.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_edits(out_dir, map_name):
    """The edit overlay, or None when there is nothing to apply.

    knoxbuild.edits is imported here, not at startup, so a generate still
    finishes while that module is being written. If the import or the load
    fails, the JSON is read directly and only its areas are used.
    """
    try:
        from knoxbuild.edits import Edits
        return Edits.load(out_dir, map_name)
    except Exception:
        return _read_edits_file(out_dir, map_name)


def _latlon_ring(ring) -> list[tuple[float, float]] | None:
    """A GeoJSON ring (lon, lat) as the (lat, lon) pairs the painter expects."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return None
    out = []
    for pt in ring:
        if not isinstance(pt, (list, tuple)) or len(pt) < 2:
            return None
        try:
            lon, lat = float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            return None
        out.append((lat, lon))
    if out[0] != out[-1]:
        out.append(out[0])
    if len(out) < 4:
        return None
    return out


def _polygon_roles(rings) -> list[tuple[str, list]]:
    if not isinstance(rings, (list, tuple)) or not rings:
        return []
    # A bare ring (points) rather than a polygon (list of rings).
    first = rings[0]
    if (isinstance(first, (list, tuple)) and first
            and isinstance(first[0], (int, float))):
        rings = [rings]
    roles = []
    for i, ring in enumerate(rings):
        pts = _latlon_ring(ring)
        if pts is None:
            continue
        roles.append(("outer" if i == 0 else "inner", pts))
    return roles


def _polygon_feature(tags: dict, coords) -> OSMFeature | None:
    roles = _polygon_roles(coords)
    outers = [ring for role, ring in roles if role == "outer"]
    inners = [ring for role, ring in roles if role == "inner"]
    if not outers:
        return None
    if len(outers) == 1 and not inners:
        return OSMFeature(osm_id=0, kind="way", tags=dict(tags), geometry=outers[0])
    return OSMFeature(osm_id=0, kind="relation", tags=dict(tags),
                      geometry=[], role_geoms=roles)


def _feature_for_area(cat: str, geometry) -> OSMFeature | None:
    tags = _PAINT_TAGS.get(cat)
    if not tags or not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    kind = geometry.get("type")
    if kind == "Polygon":
        return _polygon_feature(tags, coords)
    if kind == "MultiPolygon" and isinstance(coords, list):
        roles = []
        for poly in coords:
            roles.extend(_polygon_roles(poly))
        if not any(role == "outer" for role, _ring in roles):
            return None
        return OSMFeature(osm_id=0, kind="relation", tags=dict(tags),
                          geometry=[], role_geoms=roles)
    return None


def _retag(feat: OSMFeature, cat: str) -> OSMFeature:
    return OSMFeature(osm_id=feat.osm_id, kind=feat.kind, tags=dict(_PAINT_TAGS[cat]),
                      geometry=feat.geometry, role_geoms=feat.role_geoms)


def _place_for_paint(buckets: dict[str, list[OSMFeature]],
                     veg: list[OSMFeature], feat: OSMFeature, cat: str) -> None:
    """Where the bucketing loop would have put a feature of `cat`.

    Woodland and hedges are vegetation only. Cemetery, orchard and wetland
    are a ground fill and a vegetation pass.
    """
    if cat in VEG_CATEGORIES:
        veg.append(feat)
        if cat in {"forest", "scrub", "tree_single", "hedge"}:
            return
    buckets.setdefault(cat, []).append(feat)


def _areas_for_paint(buckets: dict[str, list[OSMFeature]],
                     vegetation_feats: list[OSMFeature], edits):
    """Ground and vegetation lists with area edits applied.

    Returns the original lists when nothing about areas changed, so an
    overlay that only renames buildings paints exactly as before. Otherwise
    the lists are copies: `buckets` is what `_areas.geojson` is written from.
    """
    overlay = _edit_dict(edits, "areas")
    added = _edit_list(edits, "added_areas")
    if not overlay and not added:
        return buckets, vegetation_feats

    drop: set[int] = set()
    moved: dict[int, tuple[OSMFeature, str]] = {}
    for index, (cat, feat, _polys) in enumerate(_area_exports(buckets)):
        rec = overlay.get(feature_id(feat, index))
        if not rec:
            continue
        if _edit_deleted(rec):
            drop.add(id(feat))
            continue
        new_cat = _edit_category(rec)
        if new_cat and new_cat != cat and new_cat in _PAINT_TAGS:
            moved[id(feat)] = (feat, new_cat)

    fresh = []
    for item in added:
        try:
            cat = _edit_category(item)
            feat = _feature_for_area(cat, _edit_field(item, "geometry"))
        except Exception:
            continue
        if feat is not None:
            fresh.append((feat, cat))
    if not drop and not moved and not fresh:
        return buckets, vegetation_feats

    skip = drop | set(moved)
    paint_buckets = {
        cat: [feat for feat in feats if id(feat) not in skip]
        for cat, feats in buckets.items()
    }
    paint_veg = [feat for feat in vegetation_feats if id(feat) not in skip]
    for feat, cat in moved.values():
        _place_for_paint(paint_buckets, paint_veg, _retag(feat, cat), cat)
    for feat, cat in fresh:
        _place_for_paint(paint_buckets, paint_veg, feat, cat)
    return paint_buckets, paint_veg


AREA_TAGS = {"landuse", "amenity", "leisure", "aeroway", "railway", "name", "religion"}


def _areas_geojson(buckets: dict[str, list[OSMFeature]]) -> dict:
    """Land-use polygons, with the category each was painted as."""
    features = []
    for cat, f, polys in _area_exports(buckets):
        _tagged_feature(f, features,
                        {"category": cat,
                         **{k: v for k, v in f.tags.items() if k in AREA_TAGS}},
                        {"type": "MultiPolygon", "coordinates": polys})
    return {"type": "FeatureCollection", "features": features}


LINE_TAGS = {"barrier", "fence_type", "material", "height", "wall"}


def _lines_geojson(feats: list[OSMFeature]) -> dict:
    """Fence and wall lines for knoxbuild to turn into fence tiles."""
    features = []
    for f in feats:
        if f.kind != "way" or len(f.geometry) < 2:
            continue
        _tagged_feature(f, features,
                        {k: v for k, v in f.tags.items() if k in LINE_TAGS},
                        {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in f.geometry]})
    return {"type": "FeatureCollection", "features": features}


def _build_spawn_map(landscape: Image.Image, w: int, h: int,
                     max_density: int) -> Image.Image:
    """Grayscale spawn map. Dense in built-up areas, zero over water.

    The red channel is read as a raw zombie density, and Build 42 reads it on a
    much smaller scale than you would guess from a 0-255 byte. Sampling the
    vanilla Knox County spawn map: values run 1..10 and 97% of the map is 0.
    Writing 96 here - a quarter of the byte range - buries the map in thousands
    of zombies. Everything below is expressed as a fraction of `max_density`,
    which now defaults to vanilla's ceiling of 10.
    """
    scaled = landscape.resize((w, h), Image.Resampling.BILINEAR)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    sp = scaled.load()
    op = out.load()
    rng = random.Random(0xABBA)

    def band(chance: float, lo: float, hi: float) -> int:
        """Density between two fractions of the ceiling, `chance` of the time.

        Sparsity matters as much as the ceiling. Vanilla leaves 97% of the map
        at zero and averages about 0.06 per pixel; filling every pixel with a
        mid value - even a small one - still produces a wall of zombies. Town
        maps are denser than county-wide wilderness, but the shape should be
        the same: crowds on the streets, almost nothing in the fields.
        """
        if rng.random() > chance:
            return 0
        return int(round(rng.uniform(max_density * lo, max_density * hi)))

    for y in range(h):
        for x in range(w):
            r, g, b = sp[x, y]
            if r == C.WATER[0] and g == C.WATER[1] and b == C.WATER[2]:
                v = 0
            elif (75 <= r <= 175 and abs(r - g) < 30 and abs(g - b) < 30)                     or (r, g, b) == C.PAVING:
                # Asphalt, pavement and paved squares: where crowds belong.
                # The low end has to reach 75 now that the widest roads are
                # painted street4 at 80,80,80 - a range starting at 95 left
                # every main road through town as quiet as a field.
                v = band(0.40, 0.4, 1.0)
            elif r > g and r > 90 and g < 110:
                # Dirt tracks, yards, building footprints.
                v = band(0.15, 0.2, 0.5)
            elif g > r and g > 80:
                # Open grass: the odd wanderer, nothing more.
                v = band(0.03, 0.1, 0.2)
            else:
                v = band(0.08, 0.1, 0.3)
            op[x, y] = (v, v, v)
    return out


def _build_preview(landscape: Image.Image, vegetation: Image.Image) -> Image.Image:
    """Human-friendly PNG: landscape with trees painted dark green."""
    import numpy as np

    preview = landscape.copy()
    width, height = preview.size

    def pack(colour: tuple[int, int, int]) -> int:
        return (colour[0] << 16) | (colour[1] << 8) | colour[2]

    # One integer per tile holds the three channels, so equality is the same
    # test as the RGB triple. Full trees are the darker green; every other
    # tree colour is the lighter one. Bands keep the scratch arrays small.
    trees = pack(C.TREES)
    lighter = (
        pack(C.TREES_DARK_GRASS),
        pack(C.SPARSE_TREES),
        pack(C.BUSHES_TREES_DARK_GRASS),
    )
    for top in range(0, height, 512):
        bottom = min(height, top + 512)
        band = np.array(vegetation.crop((0, top, width, bottom)), dtype=np.uint8)
        key = np.empty((bottom - top, width), dtype=np.uint32)
        key[:] = band[..., 0]
        key <<= 8
        key += band[..., 1]
        key <<= 8
        key += band[..., 2]
        pale = (key == lighter[0]) | (key == lighter[1]) | (key == lighter[2])
        dark = key == trees
        if not pale.any() and not dark.any():
            continue
        out = np.array(preview.crop((0, top, width, bottom)), dtype=np.uint8)
        out[pale] = (65, 95, 45)
        out[dark] = (40, 75, 35)
        preview.paste(Image.fromarray(out, "RGB"), (0, top))
    return preview


# What survives into the geojson knoxbuild reads. amenity/shop/leisure carry
# what a building actually is far more reliably than the building tag alone,
# and they pick its materials and room plan.
#
# building:levels earns its place twice over: it is the only thing in OSM that
# says how tall a building is, and dropping it meant every block of flats in a
# town was rebuilt as a bungalow no matter what the mapper had recorded.
# Houses where the map has an address but no building.
#
# In whole countries - and in most American suburbs - the houses are not drawn
# at all; what the survey left behind is one node per home carrying
# addr:housenumber. Those streets came out as roads through empty grass. A
# node with an address on it is a building by definition, so one of a
# household's size is put there, upright on the grid; the rest of the build
# treats it like any other footprint, so it is skipped if a real building
# already covers the spot and it moves off the road like the others do.
ADDRESS_HOUSE_M = (9.0, 11.0)    # frontage, depth: a small detached house
ADDRESS_CLEAR_M = 4.0            # keep this far off a mapped building
ADDRESS_APART_M = 7.0            # and this far from the next address point
MAX_ADDRESS_HOUSES = 40000
# How far the near wall of the house stands back from the edge of the road.
#
# An address is a point on a street, and mappers put it anywhere from the
# doorstep to the middle of the carriageway. A house dropped where the point
# is therefore sat on the road often enough to read as roads going missing.
# Each one is pushed away from the nearest road until it is clear of it, and
# dropped if it cannot be.
ADDRESS_SETBACK_M = 2.0
ADDRESS_PUSH_M = 18.0            # as far as a house is moved to get clear
# Every road a house has to stand clear of: the ones with a carriageway. A
# footpath or a drive may run right past the door.
ADDRESS_AVOIDS_ROADS = {"road_major", "road_medium", "road_minor", "road_service",
                         "road_track", "dirt_path", "pedestrian"}
# A residential street with no roof in OpenStreetMap and none from Overture
# still reads as a suburb. One house each side, stepped along the centre
# line. The step clears an upright 9 by 11 m house on a diagonal street.
STREET_HOUSE_STEP_M = 16.0
STREET_HOUSE_END_M = 14.0       # leave the junction for the cross street
STREET_FILL_HIGHWAYS = {"residential", "living_street"}
MAX_STREET_HOUSES = 100000
STREET_HOUSE_FIRST_ID = -3_000_000_000
# Land that is already something else. A house does not go in it.
STREET_HOUSE_AVOIDS = {
    "park", "grass", "playground", "sports", "water", "forest", "scrub",
    "wetland", "farmland", "orchard", "cemetery", "sand", "dirt",
    "schoolyard", "hospital_grounds", "commercial", "industrial",
    "military", "airport", "railway", "worship_grounds", "parking", "plaza",
}


def _off_the_road(x: float, y: float, need: float, roads, road_half,
                  road_tree, push_limit: float):
    """Move a point away from the road nearest it until the house round it
    clears the carriageway. None when it cannot be cleared."""
    from shapely.geometry import Point

    for _ in range(6):
        here = Point(x, y)
        worst, gap = None, 0.0
        for i in road_tree.query(here.buffer(need + max(road_half, default=0.0))):
            i = int(i)
            short = need + road_half[i] - roads[i].distance(here)
            if short > gap:
                worst, gap = i, short
        if worst is None:
            return x, y
        # Straight away from the road it is closest to.
        near = roads[worst].interpolate(roads[worst].project(here))
        dx, dy = x - near.x, y - near.y
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            return None                  # on the centre line; no way to know
        step = gap + 0.5
        if step > push_limit:
            return None
        x += dx / length * step
        y += dy / length * step
        push_limit -= step
        if push_limit <= 0:
            return None
    return None


def _houses_from_addresses(feats: list["OSMFeature"], buildings: list["OSMFeature"],
                           proj: "Projector") -> list["OSMFeature"]:
    """A house-sized footprint for every address that has no building."""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.strtree import STRtree

    points = [f for f in feats
              if f.kind == "node" and f.geometry and f.tags.get("addr:housenumber")
              and not f.tags.get("building")]
    if not points:
        return []
    shapes = []
    for f in buildings:
        rings = _feature_coords_px(f, proj)
        for ring in rings:
            if len(ring) >= 3:
                poly = Polygon(ring)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if not poly.is_empty:
                    shapes.append(poly)
    tree = STRtree(shapes) if shapes else None
    # The roads themselves, each as a band of its own width, so a house can
    # be pushed off the one it belongs to.
    roads, road_half = [], []
    for f in feats:
        cat = classify(f.tags, _is_polygon(f))
        if cat not in ADDRESS_AVOIDS_ROADS:
            continue
        for ring in _feature_coords_px(f, proj):
            if len(ring) >= 2:
                roads.append(LineString(ring))
                road_half.append(_way_width_m(f, cat) / proj.meters_per_tile / 2)
    road_tree = STRtree(roads) if roads else None
    setback = ADDRESS_SETBACK_M / proj.meters_per_tile
    push_limit = ADDRESS_PUSH_M / proj.meters_per_tile
    clear = ADDRESS_CLEAR_M / proj.meters_per_tile
    apart = ADDRESS_APART_M / proj.meters_per_tile
    half_w = (ADDRESS_HOUSE_M[0] / proj.meters_per_tile) / 2
    half_h = (ADDRESS_HOUSE_M[1] / proj.meters_per_tile) / 2
    taken: dict[tuple[int, int], list] = {}
    cell = max(1.0, apart)
    made = []
    for f in points:
        if len(made) >= MAX_ADDRESS_HOUSES:
            break
        lat, lon = f.geometry[0]
        x, y = proj.to_px(lat, lon)
        if not (-half_w <= x < proj.width + half_w and -half_h <= y < proj.height + half_h):
            continue
        here = Point(x, y)
        if tree is not None:
            near = tree.query(here.buffer(clear))
            if any(shapes[int(i)].distance(here) <= clear for i in near):
                continue
        if road_tree is not None:
            spot = _off_the_road(x, y, max(half_w, half_h) + setback,
                                 roads, road_half, road_tree, push_limit)
            if spot is None:
                continue                 # nowhere off the road to put it
            x, y = spot
            here = Point(x, y)
        key = (int(x // cell), int(y // cell))
        crowd = [p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                 for p in taken.get((key[0] + dx, key[1] + dy), ())]
        if any(abs(px - x) < apart and abs(py - y) < apart for px, py in crowd):
            continue
        taken.setdefault(key, []).append((x, y))
        ring = [(x - half_w, y - half_h), (x + half_w, y - half_h),
                (x + half_w, y + half_h), (x - half_w, y + half_h),
                (x - half_w, y - half_h)]
        tags = {k: v for k, v in f.tags.items() if k in BUILDING_TAGS}
        tags.setdefault("building", "house")
        made.append(OSMFeature(osm_id=f.osm_id, kind="way", tags=tags,
                               geometry=[proj.to_latlon(px, py) for px, py in ring]))
    return made


def _area_polygons_px(feat: "OSMFeature", proj: "Projector") -> list:
    """This area as shapely polygons in tile space, holes kept."""
    from shapely.geometry import Polygon

    made = []
    if not _is_polygon(feat):
        return made
    if feat.kind == "way":
        ring = [proj.to_px(la, lo) for la, lo in feat.geometry]
        candidates = [(ring, [])] if len(ring) >= 4 else []
    else:
        holes, outers = [], []
        for role, ring in feat.role_geoms:
            projected = [proj.to_px(la, lo) for la, lo in ring]
            if len(projected) < 4:
                continue
            if role == "inner":
                holes.append(projected)
            else:
                outers.append(projected)
        candidates = []
        for outer in outers:
            try:
                shell = Polygon(outer)
            except (ValueError, TypeError):
                continue
            if not shell.is_valid:
                shell = shell.buffer(0)
            use = []
            for hole in holes:
                try:
                    hp = Polygon(hole)
                except (ValueError, TypeError):
                    continue
                if not hp.is_empty and shell.contains(hp.representative_point()):
                    use.append(hole)
            candidates.append((outer, use))
    for outer, holes in candidates:
        try:
            poly = Polygon(outer, holes) if holes else Polygon(outer)
        except (ValueError, TypeError):
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly is not None and not poly.is_empty:
            made.append(poly)
    return made


def _houses_along_streets(buckets: dict, buildings: list["OSMFeature"],
                          proj: "Projector", clip) -> list["OSMFeature"]:
    """Houses along residential streets that no source has a roof for.

    OpenStreetMap and Overture both leave whole blocks blank. The street is
    still there, so a house goes on each side, clear of the pavement and of
    every footprint and land use already mapped.
    """
    from shapely.geometry import LineString, Point, Polygon, box
    from shapely.prepared import prep
    from shapely.strtree import STRtree

    roads, road_half = [], []
    streets = []
    for cat in ADDRESS_AVOIDS_ROADS:
        for feat in buckets.get(cat, []):
            if feat.kind != "way" or len(feat.geometry) < 2:
                continue
            if feat.tags.get("area") == "yes":
                continue
            if feat.tags.get("junction") in {"roundabout", "circular"}:
                continue
            line = LineString([proj.to_px(la, lo) for la, lo in feat.geometry])
            if line.is_empty or line.length <= 0:
                continue
            half = _way_width_m(feat, cat) / proj.meters_per_tile / 2
            roads.append(line)
            road_half.append(half)
            highway = feat.tags.get("highway")
            if cat != "road_minor" or highway not in STREET_FILL_HIGHWAYS:
                continue
            if feat.tags.get("tunnel") in {"yes", "building_passage"}:
                continue
            if feat.tags.get("bridge") in {"yes", "viaduct", "aqueduct"}:
                continue
            streets.append((line, half))
    if not streets:
        return []

    shapes = []
    for feat in buildings:
        for ring in _feature_coords_px(feat, proj):
            if len(ring) < 3:
                continue
            try:
                poly = Polygon(ring)
            except (ValueError, TypeError):
                continue
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                shapes.append(poly)
    tree = STRtree(shapes) if shapes else None

    blocked = []
    for cat in STREET_HOUSE_AVOIDS:
        for feat in buckets.get(cat, []):
            blocked.extend(_area_polygons_px(feat, proj))
    block_tree = STRtree(blocked) if blocked else None
    road_tree = STRtree(roads) if roads else None
    inside = prep(clip) if clip is not None else None

    mpt = proj.meters_per_tile
    step = STREET_HOUSE_STEP_M / mpt
    margin = STREET_HOUSE_END_M / mpt
    setback = ADDRESS_SETBACK_M / mpt
    clear = 2.0 / mpt
    sidewalk = SIDEWALK_M.get("road_minor", 3.0) / mpt
    half_w = (ADDRESS_HOUSE_M[0] / mpt) / 2
    half_h = (ADDRESS_HOUSE_M[1] / mpt) / 2
    reach = max(half_w, half_h)
    apart = math.hypot(ADDRESS_HOUSE_M[0], ADDRESS_HOUSE_M[1]) / mpt
    taken: dict[tuple[int, int], list] = {}
    cell = max(1.0, apart)
    made = []
    next_id = STREET_HOUSE_FIRST_ID

    for line, half in streets:
        if len(made) >= MAX_STREET_HOUSES:
            break
        length = line.length
        if length < step + margin:
            continue
        offset = half + sidewalk + setback + reach
        dist = margin
        while dist <= length - margin:
            if len(made) >= MAX_STREET_HOUSES:
                break
            at = line.interpolate(dist)
            ahead = line.interpolate(min(length, dist + 2.0))
            dx, dy = ahead.x - at.x, ahead.y - at.y
            norm = math.hypot(dx, dy)
            if norm < 1e-6:
                dist += step
                continue
            px, py = -dy / norm, dx / norm
            for side in (-1.0, 1.0):
                if len(made) >= MAX_STREET_HOUSES:
                    break
                x = at.x + px * offset * side
                y = at.y + py * offset * side
                if (x - half_w < 0 or y - half_h < 0
                        or x + half_w >= proj.width or y + half_h >= proj.height):
                    continue
                here = Point(x, y)
                if inside is not None and not inside.contains(here):
                    continue
                house = box(x - half_w, y - half_h, x + half_w, y + half_h)
                if block_tree is not None and any(
                        blocked[int(i)].intersects(house)
                        for i in block_tree.query(house)):
                    continue
                if tree is not None and any(
                        shapes[int(i)].distance(house) <= clear
                        for i in tree.query(house.buffer(clear))):
                    continue
                if road_tree is not None and any(
                        roads[int(i)].distance(house) < road_half[int(i)] + sidewalk
                        for i in road_tree.query(house.buffer(offset))):
                    continue
                key = (int(x // cell), int(y // cell))
                crowd = [p for gx in (-1, 0, 1) for gy in (-1, 0, 1)
                         for p in taken.get((key[0] + gx, key[1] + gy), ())]
                if any((qx - x) ** 2 + (qy - y) ** 2 < apart * apart
                       for qx, qy in crowd):
                    continue
                taken.setdefault(key, []).append((x, y))
                ring = [(x - half_w, y - half_h), (x + half_w, y - half_h),
                        (x + half_w, y + half_h), (x - half_w, y + half_h),
                        (x - half_w, y - half_h)]
                made.append(OSMFeature(
                    osm_id=next_id, kind="way",
                    tags={"building": "house"},
                    geometry=[proj.to_latlon(rx, ry) for rx, ry in ring]))
                next_id -= 1
            dist += step
    return made


BUILDING_TAGS = {
    "building", "building:levels", "building:part", "building:material",
    "building:use", "height", "levels", "roof:levels", "roof:shape",
    "name", "addr:housenumber", "addr:street", "addr:flats",
    "amenity", "shop", "leisure", "tourism", "industrial",
    "healthcare", "office", "craft", "man_made", "residential", "cuisine",
}


def _buildings_geojson(feats: list[OSMFeature]) -> dict:
    """Export building footprints so the user knows where to drop .tbx lots."""
    features = []
    for f in feats:
        if f.kind == "way":
            coords = [[lon, lat] for lat, lon in f.geometry]
            if len(coords) >= 3:
                # amenity/shop/leisure carry what a building actually is
                # far more reliably than the building tag alone, and
                # knoxbuild uses them to pick materials and room plans.
                _tagged_feature(
                    f, features,
                    {k: v for k, v in f.tags.items() if k in BUILDING_TAGS},
                    {"type": "Polygon", "coordinates": [coords]})
        elif f.kind == "relation":
            polys = []
            for role, ring in f.role_geoms:
                if role == "inner":
                    continue
                coords = [[lon, lat] for lat, lon in ring]
                if len(coords) >= 3:
                    polys.append([coords])
            if polys:
                _tagged_feature(
                    f, features,
                    {k: v for k, v in f.tags.items() if k in BUILDING_TAGS},
                    {"type": "MultiPolygon", "coordinates": polys})
    return {"type": "FeatureCollection", "features": features}
