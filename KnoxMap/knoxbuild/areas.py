"""What a building's surroundings say it is.

OSM rarely tags what a building is used for: in the town this was built on,
3015 of 3079 buildings were plain building=yes. The land they stand on is
tagged far more often - an industrial estate, a school's grounds, a hospital
site, a cemetery - and a building inside one of those is almost always part of
it. Reading that turns an anonymous box on an industrial estate into a works,
and the shed in a schoolyard into part of the school, instead of the suburban
house every untagged building used to become.
"""
from __future__ import annotations

import json
import os

from shapely import STRtree
from shapely.errors import GEOSException
from shapely.geometry import Point, Polygon

from .edits import Edits, feature_id

# Area category -> building kind, for buildings OSM says nothing about.
# Residential and parking are left out on purpose: a building in a
# residential area is exactly what the house and flats logic already decides,
# and one in a car park is a kiosk not worth guessing at.
KIND_FOR_AREA = {
    "industrial": "industrial",
    "commercial": "shop",
    "schoolyard": "school",
    "hospital_grounds": "medical",
    "military": "military",
    "worship_grounds": "church",
    "cemetery": "church",
    "sports": "civic",
    # A hangar on an airfield, a goods shed in a railway yard: the building
    # is almost never tagged, and the grounds are what OSM marked.
    "airport": "industrial",
    "railway": "industrial",
}
# Below this many tiles, a building on institutional grounds is an outbuilding
# rather than the institution itself.
OUTBUILDING = 120


def kind_for_category(category, tiles: int) -> str | None:
    """The building kind a category gives, or None when it names none.

    A small building on school or hospital grounds is an outbuilding. Drawn
    zones use this same table, so a zone and a painted area agree.
    """
    kind = KIND_FOR_AREA.get(category)
    if kind in {"school", "medical"} and tiles < OUTBUILDING:
        return "civic"
    return kind


def _polygons_px(geom, proj) -> list:
    """Outer rings of a GeoJSON polygon, in tile pixels. Bad rings are skipped."""
    if not isinstance(geom, dict):
        return []
    polys = geom.get("coordinates") or []
    if not isinstance(polys, list):
        return []
    if geom.get("type") == "Polygon":
        polys = [polys]
    shapes = []
    for poly in polys:
        if not isinstance(poly, list) or not poly or len(poly[0]) < 3:
            continue
        try:
            ring = [proj.to_px(lat, lon) for lon, lat in poly[0]]
            shape = Polygon(ring)
            if not shape.is_valid:
                shape = shape.buffer(0)
        except (TypeError, ValueError, KeyError, GEOSException):
            continue
        if shape.is_empty or shape.area <= 0:
            continue
        shapes.append(shape)
    return shapes


class AreaIndex:
    def __init__(self, polygons: list[tuple[Polygon, dict]], edits=None):
        self._items = polygons
        self._tree = STRtree([p for p, _ in polygons]) if polygons else None
        self._edits = edits

    @classmethod
    def load(cls, out_dir: str, map_name: str, proj) -> "AreaIndex":
        edits = Edits.load(out_dir, map_name)
        # zone_at reads this cache. An empty overlay still counts as checked.
        edits.regions_px(proj)
        items: list[tuple[Polygon, dict]] = []
        path = os.path.join(out_dir, f"{map_name}_areas.geojson")
        data: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        for index, feat in enumerate(data.get("features") or []):
            if not isinstance(feat, dict):
                continue
            rec = edits.area(feature_id(feat, index)) or {}
            if rec.get("deleted"):
                continue
            props = dict(feat.get("properties") or {})
            if "category" in rec:
                props["category"] = rec["category"]
            if "name" in rec:
                props["name"] = rec["name"]
            for shape in _polygons_px(feat.get("geometry") or {}, proj):
                items.append((shape, props))
        for added in edits.added_areas:
            props = {}
            if "category" in added:
                props["category"] = added["category"]
            if "name" in added:
                props["name"] = added["name"]
            for shape in _polygons_px(added.get("geometry") or {}, proj):
                items.append((shape, props))
        return cls(items, edits)

    def around(self, x: float, y: float) -> dict | None:
        """The most specific area containing a point: the smallest one.

        Areas nest - a school inside a residential district - and the inner
        one is the one that says what is actually there.
        """
        if self._tree is None:
            return None
        point = Point(x, y)
        best = None
        for i in self._tree.query(point):
            shape, props = self._items[int(i)]
            if shape.contains(point) and (best is None or shape.area < best[0]):
                best = (shape.area, props)
        return best[1] if best else None

    def kind_for(self, x: float, y: float, tiles: int) -> str | None:
        # A drawn zone re-zones the ground under it, including where an older
        # area polygon said something else. A category with no kind (a
        # residential zone) clears that polygon rather than leaving it.
        if self._edits is not None:
            category = self._edits.zone_at(x, y)
            if category:
                return kind_for_category(category, tiles)
        props = self.around(x, y)
        if not props:
            return None
        return kind_for_category(props.get("category"), tiles)

    def category_at(self, x: float, y: float) -> str | None:
        props = self.around(x, y)
        return props.get("category") if props else None
