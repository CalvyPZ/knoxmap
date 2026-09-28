"""Edits layered on a generated map, without rewriting it.

Generate writes the GeoJSON once: footprints, areas, streets, places. The Edit
tab never changes those files. What gets renamed, moved, deleted or drawn by
hand goes in `<name>_edits.json` beside them, and the build reads that file
on top. A re-generate leaves the edits where they are, and deleting one entry
puts that feature back the way the map drew it.

Moving or re-zoning a building changes which footprints collide, so the town
has to be placed again. Changing a seed, or drawing a region that only
re-rolls interiors, does not: each building's rooms come from its own seed.
`layout_only` is how Apply tells those two apart.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.prepared import prep

from . import mapstate

VERSION = 1

# Keys written in this order so a hand-opened file reads like the schema.
_BUILDING_FIELDS = ("name", "kind", "levels", "style", "seed", "offset", "deleted")
_AREA_FIELDS = ("category", "name", "deleted")
_PLACE_FIELDS = ("name", "population", "deleted")
_WAY_FIELDS = ("name",)
# Placement changes. A name or a seed is deliberately not in this list: a name
# does not move the footprint, and a seed only re-rolls that building's rooms.
_PLACEMENT = ("deleted", "offset", "kind", "levels", "style")


def feature_id(feat, index: int) -> str:
    """The id an edit is stored under: the feature's own, or i<index>."""
    if isinstance(feat, dict):
        fid = feat.get("id")
        if fid not in (None, ""):
            return str(fid)
    return "i" + str(index)


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value, 10)
        except ValueError:
            return None
    return None


def _as_text(value, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    if value == "" and not allow_empty:
        return None
    return value


def _as_offset(value) -> list[int] | None:
    """A whole-tile nudge. [0, 0] is not stored: it does not move anything."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    dx, dy = _as_int(value[0]), _as_int(value[1])
    if dx is None or dy is None or (dx == 0 and dy == 0):
        return None
    return [dx, dy]


def _clears_offset(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    dx, dy = _as_int(value[0]), _as_int(value[1])
    return dx == 0 and dy == 0


def _clean_building_field(key: str, value):
    if key == "name":
        return _as_text(value, allow_empty=True)
    if key in ("kind", "style"):
        return _as_text(value)
    if key in ("levels", "seed"):
        return _as_int(value)
    if key == "offset":
        return _as_offset(value)
    if key == "deleted":
        return True if value is True else None
    return None


def _building_clears(key: str, value) -> bool:
    if key == "deleted" and value is False:
        return True
    if key == "offset" and _clears_offset(value):
        return True
    if key in ("kind", "style") and value == "":
        return True
    return False


def _clean_area_field(key: str, value):
    if key == "category":
        return _as_text(value)
    if key == "name":
        return _as_text(value, allow_empty=True)
    if key == "deleted":
        return True if value is True else None
    return None


def _area_clears(key: str, value) -> bool:
    if key == "deleted" and value is False:
        return True
    if key == "category" and value == "":
        return True
    return False


def _clean_place_field(key: str, value):
    if key == "name":
        return _as_text(value, allow_empty=True)
    if key == "population":
        return _as_int(value)
    if key == "deleted":
        return True if value is True else None
    return None


def _place_clears(key: str, value) -> bool:
    return key == "deleted" and value is False


def _clean_way_field(key: str, value):
    if key == "name":
        return _as_text(value, allow_empty=True)
    return None


def _way_clears(key: str, value) -> bool:
    return False


def _take(raw: dict, fields, clean) -> dict:
    out = {}
    for key in fields:
        if key not in raw:
            continue
        cleaned = clean(key, raw[key])
        if cleaned is not None:
            out[key] = cleaned
    return out


def _merge_fields(current: dict, patch: dict, fields, clean, clears) -> dict:
    """Overlay patch onto one record. Null, false and a zero offset drop a field.

    An unrecognised value leaves the previous one in place, so a bad key in a
    patch cannot wipe an edit the file already has.
    """
    merged = {key: current[key] for key in fields if key in current}
    for key, value in patch.items():
        if key not in fields:
            continue
        if value is None or clears(key, value):
            merged.pop(key, None)
            continue
        cleaned = clean(key, value)
        if cleaned is not None:
            merged[key] = cleaned
    return merged


def _geo(value) -> dict | None:
    """A Polygon or MultiPolygon, copied so the caller's dict stays theirs."""
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    coords = value.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon") or not isinstance(coords, list):
        return None
    try:
        return json.loads(json.dumps({"type": kind, "coordinates": coords}))
    except (TypeError, ValueError):
        return None


def _norm_added(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    geometry = _geo(raw.get("geometry"))
    if geometry is None:
        return None
    rec = {"geometry": geometry}
    if raw.get("id") not in (None, ""):
        rec["id"] = str(raw["id"])
    category = _as_text(raw.get("category"))
    if category:
        rec["category"] = category
    name = raw.get("name")
    if isinstance(name, str):
        rec["name"] = name
    return {key: rec[key] for key in ("id", "category", "name", "geometry") if key in rec}


def _norm_region(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    action = raw.get("action")
    if action not in ("reroll", "rebuild", "zone"):
        return None
    shape = _geo(raw.get("shape"))
    if shape is None:
        return None
    rec: dict = {"action": action, "shape": shape}
    if raw.get("id") not in (None, ""):
        rec["id"] = str(raw["id"])
    if action == "reroll":
        seed = _as_int(raw.get("seed"))
        if seed is not None:
            rec["seed"] = seed
    elif action == "zone":
        category = _as_text(raw.get("category"))
        if category:
            rec["category"] = category
    order = ("id", "action", "seed", "category", "shape")
    return {key: rec[key] for key in order if key in rec}


def _hide_names(items) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for item in items:
        if not isinstance(item, str) or item == "" or item in seen:
            continue
        seen.add(item)
        names.append(item)
    return names


def _shape_px(shape, proj):
    """A GeoJSON polygon (lon/lat rings) in tile pixels, or None.

    Same convention as generator.renderer.shape_px: coordinates are
    [lon, lat], and Projector.to_px takes (lat, lon). A ring that does not
    project is dropped, so one bad region cannot fail the build.
    """
    if not isinstance(shape, dict):
        return None
    kind = shape.get("type")
    coords = shape.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon") or not isinstance(coords, list):
        return None
    polys = coords if kind == "MultiPolygon" else [coords]
    parts = []
    for rings in polys:
        if not isinstance(rings, list) or not rings:
            continue
        try:
            outer_ring = rings[0]
            if not isinstance(outer_ring, list) or len(outer_ring) < 3:
                continue
            outer = [proj.to_px(lat, lon) for lon, lat in outer_ring]
            holes = [
                [proj.to_px(lat, lon) for lon, lat in ring]
                for ring in rings[1:]
                if isinstance(ring, list) and len(ring) >= 3
            ]
            poly = Polygon(outer, holes)
        except (TypeError, ValueError, KeyError, GEOSException):
            continue
        if poly.is_empty:
            continue
        try:
            fixed = poly if poly.is_valid else poly.buffer(0)
        except (ValueError, GEOSException):
            continue
        if fixed.is_empty:
            continue
        parts.append(fixed)
    if not parts:
        return None
    if len(parts) == 1:
        merged = parts[0]
    else:
        flat = []
        for part in parts:
            geoms = getattr(part, "geoms", None)
            flat.extend(geoms if geoms is not None else [part])
        try:
            merged = MultiPolygon(flat)
        except (TypeError, ValueError, GEOSException):
            return None
    if not merged.is_valid:
        try:
            merged = merged.buffer(0)
        except (ValueError, GEOSException):
            return None
    if merged.is_empty or not merged.area:
        return None
    return merged


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _copy_record(rec: dict) -> dict:
    out = dict(rec)
    offset = out.get("offset")
    if isinstance(offset, list):
        out["offset"] = list(offset)
    return out


class Edits:
    """The `<map_name>_edits.json` overlay for one map folder."""

    feature_id = staticmethod(feature_id)

    def __init__(self, out_dir: str, map_name: str):
        self.out_dir = os.fspath(out_dir)
        self.map_name = str(map_name)
        self.saved_at: str | None = None
        self.buildings: dict[str, dict] = {}
        self.areas: dict[str, dict] = {}
        self.added_areas: list[dict] = []
        self.streets: dict = {}
        self.places: dict[str, dict] = {}
        self.regions: list[dict] = []
        # Tile-space regions from the last regions_px. None until then, so a
        # hit test before projection does not pretend the map was checked.
        self._px_hits: list[tuple[dict, object]] | None = None
        self._px_proj = None

    @classmethod
    def load(cls, out_dir: str, map_name: str) -> "Edits":
        """Read the overlay. A missing or unreadable file is an empty one."""
        edits = cls(out_dir, map_name)
        try:
            with open(edits.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            # Missing is the normal case: the map has never been edited. A
            # file that does not parse must not stop the build either; the
            # GeoJSON still stands on its own.
            return edits
        if isinstance(data, dict):
            edits._read(data)
        return edits

    @property
    def path(self) -> str:
        return os.path.join(self.out_dir, f"{self.map_name}_edits.json")

    def save(self) -> "Edits":
        """Write the overlay atomically and stamp saved_at."""
        stamp = datetime.now().replace(microsecond=0).isoformat()
        previous = self.saved_at
        self.saved_at = stamp
        self._invalidate()
        tmp = self.path + ".tmp"
        written = False
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._as_dict(), handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(tmp, self.path)
            written = True
        finally:
            if not written:
                self.saved_at = previous
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return self

    def merge(self, patch: dict) -> "Edits":
        """Fold a partial overlay into this one.

        A null under buildings, areas, places or streets.ways drops that one
        feature's override, so the map's own value comes back. A null rename
        drops that one renaming. hide, added_areas and regions are replaced
        wholesale when the patch includes them: the editor sends the whole
        list. ``clear`` drops a section, or everything but the version when
        it is ``all``, and it is applied before the rest of the patch so one
        request can wipe a section and set its replacement.
        """
        if not isinstance(patch, dict):
            return self
        self._invalidate()
        clear = patch.get("clear")
        if clear == "all":
            self._blank()
        elif clear == "buildings":
            self.buildings = {}
        elif clear == "areas":
            self.areas = {}
        elif clear == "streets":
            self.streets = {}
        elif clear == "places":
            self.places = {}
        elif clear == "regions":
            self.regions = []
        elif clear == "added_areas":
            self.added_areas = []
        if "buildings" in patch:
            self._merge_ids(self.buildings, patch["buildings"], _BUILDING_FIELDS,
                            _clean_building_field, _building_clears)
        if "areas" in patch:
            self._merge_ids(self.areas, patch["areas"], _AREA_FIELDS,
                            _clean_area_field, _area_clears)
        if "places" in patch:
            self._merge_ids(self.places, patch["places"], _PLACE_FIELDS,
                            _clean_place_field, _place_clears)
        if "added_areas" in patch:
            self._replace_list("added_areas", patch["added_areas"], _norm_added)
        if "regions" in patch:
            self._replace_list("regions", patch["regions"], _norm_region)
        if "streets" in patch:
            self._merge_streets(patch["streets"])
        return self

    def building(self, fid) -> dict | None:
        rec = self.buildings.get(str(fid))
        return _copy_record(rec) if rec else None

    def area(self, fid) -> dict | None:
        rec = self.areas.get(str(fid))
        return dict(rec) if rec else None

    def place(self, key) -> dict | None:
        rec = self.places.get(str(key))
        return dict(rec) if rec else None

    def regions_px(self, proj) -> list[dict]:
        """Regions as shapely polygons in this projector's tile pixels.

        Invalid and empty shapes are left out. The result is cached until
        merge or save; seed_nudge_at and zone_at test against that cache, so
        call this with the map's projector before either of them.
        """
        if self._px_hits is None or self._px_proj is not proj:
            self._px_hits = self._project(proj)
            self._px_proj = proj
        return [
            {
                "id": item["id"],
                "action": item["action"],
                "seed": item["seed"],
                "category": item["category"],
                "shape": item["shape"],
            }
            for item, _prepared in self._px_hits
        ]

    def seed_nudge_at(self, x: float, y: float) -> int:
        """Sum of the reroll seeds covering a tile point.

        A reroll region with no seed of its own counts as 1. Zero when none
        cover the point, or when regions_px has not been called yet.
        """
        if not self._px_hits:
            return 0
        point = Point(x, y)
        total = 0
        found = False
        for item, prepared in self._px_hits:
            if item.get("action") != "reroll" or not prepared.contains(point):
                continue
            found = True
            seed = item.get("seed")
            total += 1 if seed is None else int(seed)
        return total if found else 0

    def zone_at(self, x: float, y: float) -> str | None:
        """The category of the smallest zone region covering a tile point.

        Nested zones use the same rule as AreaIndex.around: the inner one
        wins. None when the point is outside every zone, or regions_px has
        not been called yet.
        """
        if not self._px_hits:
            return None
        point = Point(x, y)
        best_area = None
        best_category = None
        for item, prepared in self._px_hits:
            if item.get("action") != "zone" or not prepared.contains(point):
                continue
            area = item["shape"].area
            if best_area is None or area < best_area:
                best_area = area
                best_category = item.get("category")
        return best_category or None

    def needs(self) -> set[str]:
        """Which Apply stages this overlay actually requires.

        ``reroll`` — a building seed, or a reroll region. Interiors only.
        ``rebuild`` — something that changes placement: a building deleted,
        nudged, re-kinded, re-storeyed or re-styled; an area category or
        deletion; a drawn area; a zone or rebuild region.
        ``labels`` — a street or place edit, or a building or area renamed.
        The world map and the street list are rewritten; layouts are not.
        ``repaint`` — an area category change, an area deletion, or a drawn
        area. The ground colour is painted again.

        A name does not move a footprint or repaint the ground, so a rename
        on its own stays on the fast path with the other label edits.
        """
        stages: set[str] = set()
        for rec in self.buildings.values():
            if "seed" in rec:
                stages.add("reroll")
            if any(key in rec for key in _PLACEMENT):
                stages.add("rebuild")
            if "name" in rec:
                stages.add("labels")
        for rec in self.areas.values():
            if "category" in rec or rec.get("deleted"):
                stages.add("rebuild")
                stages.add("repaint")
            if "name" in rec:
                stages.add("labels")
        if self.added_areas:
            stages.add("rebuild")
            stages.add("repaint")
        for region in self.regions:
            action = region.get("action")
            if action == "reroll":
                stages.add("reroll")
            elif action in ("rebuild", "zone"):
                stages.add("rebuild")
        if self.streets or self.places:
            stages.add("labels")
        return stages

    def layout_only(self) -> bool:
        """True when Apply can take the fast path and skip re-placement.

        That is every overlay whose needs() do not include rebuild or
        repaint: reroll, labels, names, or nothing at all.
        """
        stages = self.needs()
        return "rebuild" not in stages and "repaint" not in stages

    def pending_stages(self, map_dir: str) -> list[str]:
        """Stages still to run, newest saved_at against the build stamp.

        Empty when the overlay changes nothing, or when knoxmap_map.json's
        build.at is the same age or newer — that build already read these
        edits. A non-empty overlay with no saved_at is pending. The list is
        needs(), sorted.
        """
        if self.is_empty():
            return []
        state = mapstate.read(map_dir)
        stage = (state.get("stages") or {}).get("build") or {}
        built = _parse_time(stage.get("at") if isinstance(stage, dict) else None)
        saved = _parse_time(self.saved_at)
        if saved is not None and built is not None and saved <= built:
            return []
        return sorted(self.needs())

    def is_empty(self) -> bool:
        """True when the overlay changes nothing."""
        return not (
            self.buildings or self.areas or self.added_areas
            or self.streets or self.places or self.regions
        )

    def explicit_reroll_ids(self) -> set[str]:
        """Building ids that carry their own seed override."""
        return {fid for fid, rec in self.buildings.items() if "seed" in rec}

    def affected_building_ids(self) -> set[str]:
        """Building ids with a seed override. Same set as explicit_reroll_ids."""
        return self.explicit_reroll_ids()

    def _blank(self) -> None:
        self.buildings = {}
        self.areas = {}
        self.added_areas = []
        self.streets = {}
        self.places = {}
        self.regions = []

    def _invalidate(self) -> None:
        self._px_hits = None
        self._px_proj = None

    def _read(self, data: dict) -> None:
        saved = data.get("saved_at")
        self.saved_at = saved if isinstance(saved, str) else None
        self.buildings = self._read_ids(data.get("buildings"), _BUILDING_FIELDS,
                                        _clean_building_field)
        self.areas = self._read_ids(data.get("areas"), _AREA_FIELDS, _clean_area_field)
        self.places = self._read_ids(data.get("places"), _PLACE_FIELDS, _clean_place_field)
        self.added_areas = self._read_list(data.get("added_areas"), _norm_added)
        self.regions = self._read_list(data.get("regions"), _norm_region)
        self._read_streets(data.get("streets"))

    @staticmethod
    def _read_ids(raw, fields, clean) -> dict[str, dict]:
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict] = {}
        for fid, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            taken = _take(rec, fields, clean)
            if taken:
                out[str(fid)] = taken
        return out

    @staticmethod
    def _read_list(raw, normalise) -> list:
        if not isinstance(raw, list):
            return []
        items = []
        for entry in raw:
            item = normalise(entry)
            if item:
                items.append(item)
        return items

    def _read_streets(self, raw) -> None:
        self.streets = {}
        if not isinstance(raw, dict):
            return
        rename = raw.get("rename")
        if isinstance(rename, dict):
            cleaned = {}
            for old, new in rename.items():
                if not isinstance(old, str) or old == "":
                    continue
                text = _as_text(new, allow_empty=True)
                if text is not None:
                    cleaned[old] = text
            if cleaned:
                self.streets["rename"] = cleaned
        hide = raw.get("hide")
        if isinstance(hide, list):
            names = _hide_names(hide)
            if names:
                self.streets["hide"] = names
        ways = raw.get("ways")
        if isinstance(ways, dict):
            cleaned_ways = self._read_ids(ways, _WAY_FIELDS, _clean_way_field)
            if cleaned_ways:
                self.streets["ways"] = cleaned_ways

    def _merge_ids(self, store: dict, patch, fields, clean, clears) -> None:
        if patch is None:
            store.clear()
            return
        if not isinstance(patch, dict):
            return
        for fid, value in patch.items():
            key = str(fid)
            if value is None:
                store.pop(key, None)
                continue
            if not isinstance(value, dict):
                continue
            merged = _merge_fields(store.get(key) or {}, value, fields, clean, clears)
            if merged:
                store[key] = merged
            else:
                store.pop(key, None)

    def _replace_list(self, attr: str, patch, normalise) -> None:
        if patch is None:
            setattr(self, attr, [])
            return
        if not isinstance(patch, list):
            return
        items = []
        for entry in patch:
            item = normalise(entry)
            if item:
                items.append(item)
        setattr(self, attr, items)

    def _merge_streets(self, patch) -> None:
        if patch is None:
            self.streets = {}
            return
        if not isinstance(patch, dict):
            return
        if "rename" in patch:
            self._merge_rename(patch["rename"])
        if "hide" in patch:
            hide = patch["hide"]
            if hide is None:
                self.streets.pop("hide", None)
            elif isinstance(hide, list):
                names = _hide_names(hide)
                if names:
                    self.streets["hide"] = names
                else:
                    self.streets.pop("hide", None)
        if "ways" in patch:
            ways = self.streets.get("ways")
            if not isinstance(ways, dict):
                ways = {}
            self._merge_ids(ways, patch["ways"], _WAY_FIELDS, _clean_way_field, _way_clears)
            if ways:
                self.streets["ways"] = ways
            else:
                self.streets.pop("ways", None)
        self._prune_streets()

    def _merge_rename(self, patch) -> None:
        if patch is None:
            self.streets.pop("rename", None)
            return
        if not isinstance(patch, dict):
            return
        rename = dict(self.streets.get("rename") or {})
        for old, new in patch.items():
            if not isinstance(old, str) or old == "":
                continue
            if new is None:
                rename.pop(old, None)
                continue
            text = _as_text(new, allow_empty=True)
            if text is not None:
                rename[old] = text
        if rename:
            self.streets["rename"] = rename
        else:
            self.streets.pop("rename", None)

    def _prune_streets(self) -> None:
        for key in ("rename", "hide", "ways"):
            if not self.streets.get(key):
                self.streets.pop(key, None)

    def _project(self, proj) -> list[tuple[dict, object]]:
        hits = []
        for region in self.regions:
            shape = _shape_px(region.get("shape"), proj)
            if shape is None:
                continue
            item = {
                "id": region.get("id"),
                "action": region.get("action"),
                "seed": region.get("seed"),
                "category": region.get("category"),
                "shape": shape,
            }
            try:
                prepared = prep(shape)
            except (GEOSException, ValueError):
                continue
            hits.append((item, prepared))
        return hits

    def _as_dict(self) -> dict:
        streets = {}
        rename = self.streets.get("rename")
        if rename:
            streets["rename"] = dict(rename)
        hide = self.streets.get("hide")
        if hide:
            streets["hide"] = list(hide)
        ways = self.streets.get("ways")
        if ways:
            streets["ways"] = {
                fid: {key: rec[key] for key in _WAY_FIELDS if key in rec}
                for fid, rec in ways.items()
            }
        payload: dict = {"version": VERSION}
        if self.saved_at:
            payload["saved_at"] = self.saved_at
        payload["buildings"] = {
            fid: {key: rec[key] for key in _BUILDING_FIELDS if key in rec}
            for fid, rec in self.buildings.items()
        }
        payload["areas"] = {
            fid: {key: rec[key] for key in _AREA_FIELDS if key in rec}
            for fid, rec in self.areas.items()
        }
        payload["added_areas"] = [
            {key: item[key] for key in ("id", "category", "name", "geometry") if key in item}
            for item in self.added_areas
        ]
        payload["streets"] = streets
        payload["places"] = {
            key: {field: rec[field] for field in _PLACE_FIELDS if field in rec}
            for key, rec in self.places.items()
        }
        payload["regions"] = [
            {key: item[key] for key in ("id", "action", "seed", "category", "shape") if key in item}
            for item in self.regions
        ]
        return payload
