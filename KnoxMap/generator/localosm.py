"""Map features from local Geofabrik dailies, one box at a time.

The regional PBF stays on disk. osmium keeps the tags KnoxMap paints, then
cuts the box this mod actually draws, and only that clip is turned into
features. A second build of the same box reuses the clip.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time

import knoxstop

from .geofabrik import Region, ensure_pbf, load_index, cover
from .osm import FILTERS_VERSION, OSMFeature, OVERPASS_FILTERS

_KIND = {"way": "w", "node": "n", "relation": "r"}
_TAG = re.compile(r'\["([^"]+)"(?:(~|=)"([^"]*)")?\]')
_ALT = re.compile(r"^\^\((.*)\)\$$")


def osmium_bin() -> str:
    exe = shutil.which("osmium")
    if not exe:
        raise RuntimeError(
            "KnoxMap reads local OpenStreetMap extracts with osmium. "
            "Install osmium-tool from https://osmcode.org/osmium-tool/ "
            "so it is on PATH, then start the build again.")
    return exe


def osmium_expressions(filters=None) -> list[str]:
    """Overpass tag filters as the osmium tags-filter expressions they are."""
    out: list[str] = []
    for expr in filters if filters is not None else OVERPASS_FILTERS:
        kind = expr.split("[", 1)[0]
        letters = list(_KIND[kind]) if kind in _KIND else ["n", "w", "r"]
        parts = _TAG.findall(expr)
        if not parts:
            continue
        key, op, value = parts[0]
        values = _values(op, value)
        for letter in letters:
            if not values:
                out.append(f"{letter}/{key}")
            else:
                out.extend(f"{letter}/{key}={item}" for item in values)
    # Stable and unique; osmium treats each argument as an alternative.
    return list(dict.fromkeys(out))


def _values(op: str, value: str) -> list[str]:
    if not op:
        return []
    if op == "=":
        return [value]
    match = _ALT.match(value)
    if match:
        return [item for item in match.group(1).split("|") if item]
    return []


def _run(cmd: list[str], should_stop=None, timeout: int = 6 * 3600) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    started = time.time()
    while True:
        try:
            _out, err = proc.communicate(timeout=0.4)
            break
        except subprocess.TimeoutExpired:
            if should_stop is not None and should_stop():
                proc.terminate()
                raise knoxstop.Stopped("the download")
            if time.time() - started > timeout:
                proc.kill()
                raise TimeoutError("osmium did not finish.")
    if proc.returncode != 0:
        text = (err or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(text[-600:] or f"osmium exited {proc.returncode}")


def _filtered_path(root: str, region: Region) -> str:
    from .geofabrik import cache_dir
    return os.path.join(cache_dir(root), f"{region.slug}.f{FILTERS_VERSION}.osm.pbf")


def ensure_filtered(root: str, region: Region, source: str, should_stop=None) -> str:
    """One tags-filter of the daily, reused until that daily is replaced."""
    dest = _filtered_path(root, region)
    if os.path.exists(dest) and os.path.getmtime(dest) >= os.path.getmtime(source):
        return dest
    tmp = dest + ".part"
    expressions = osmium_expressions()
    _run([osmium_bin(), "tags-filter", source, *expressions, "-o", tmp, "--overwrite"],
         should_stop=should_stop)
    os.replace(tmp, dest)
    return dest


def ensure_regions(root: str, south: float, west: float, north: float, east: float,
                   progress=None, should_stop=None) -> list[Region]:
    """Latest dailies for the smallest regions covering this box, filtered."""
    knoxstop.check(should_stop, "the download")
    osmium_bin()
    regions = cover(load_index(root), south, west, north, east)
    ready = []
    for index, region in enumerate(regions, start=1):
        knoxstop.check(should_stop, "the download")
        if progress:
            progress(region.name, index, len(regions))
        source = ensure_pbf(root, region)
        ensure_filtered(root, region, source, should_stop=should_stop)
        ready.append(region)
    return ready


def _clip_path(root: str, region: Region, box: tuple[float, float, float, float],
               highways_only: bool) -> str:
    from .geofabrik import cache_dir
    filtered = _filtered_path(root, region)
    stamp = os.path.getmtime(filtered) if os.path.exists(filtered) else 0
    raw = f"{region.id}|{box}|{highways_only}|{stamp}|{FILTERS_VERSION}"
    digest = hashlib.sha1(raw.encode()).hexdigest()[:20]
    folder = os.path.join(cache_dir(root), "clips")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, digest + ".jsonl")


def _extract_features(root: str, region: Region,
                      south: float, west: float, north: float, east: float,
                      highways_only: bool, should_stop=None) -> str:
    """geojsonseq for this box, clipped from the region's filtered daily."""
    dest = _clip_path(root, region, (south, west, north, east), highways_only)
    if os.path.exists(dest):
        return dest
    filtered = _filtered_path(root, region)
    folder = os.path.dirname(dest)
    pbf = os.path.join(folder, os.path.basename(dest) + ".osm.pbf")
    box = f"{west},{south},{east},{north}"
    _run([osmium_bin(), "extract", "-b", box, "--strategy", "smart",
          filtered, "-o", pbf, "--overwrite"], should_stop=should_stop)
    # A box in the sea, or past the edge of this region, is a valid empty piece.
    if os.path.getsize(pbf) < 500:
        with open(dest, "w", encoding="utf-8"):
            pass
        os.remove(pbf)
        return dest
    source = pbf
    if highways_only:
        roads = pbf + ".roads.pbf"
        _run([osmium_bin(), "tags-filter", pbf, "w/highway", "-o", roads, "--overwrite"],
             should_stop=should_stop)
        source = roads
    tmp = dest + ".part"
    _run([osmium_bin(), "export", "-f", "geojsonseq", source, "-o", tmp, "--overwrite"],
         should_stop=should_stop)
    os.replace(tmp, dest)
    for leftover in (pbf, pbf + ".roads.pbf"):
        if os.path.exists(leftover):
            os.remove(leftover)
    return dest


def features_for_bbox(root: str, regions: list[Region],
                      south: float, west: float, north: float, east: float,
                      highways_only: bool = False,
                      should_stop=None) -> list[OSMFeature]:
    """Features inside this box, from every region the build downloaded."""
    merged: dict[tuple[str, int], OSMFeature] = {}
    for region in regions:
        knoxstop.check(should_stop, "the download")
        path = _extract_features(root, region, south, west, north, east,
                                 highways_only, should_stop=should_stop)
        for feature in _read_geojsonseq(path):
            merged[(feature.kind, feature.osm_id)] = feature
    return list(merged.values())


def _rings(coords) -> list[tuple[float, float]]:
    out = []
    for pair in coords or []:
        if isinstance(pair, (list, tuple)) and len(pair) >= 2 and not isinstance(pair[0], (list, tuple)):
            out.append((float(pair[1]), float(pair[0])))
    return out


def _read_geojsonseq(path: str):
    next_id = -1
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.lstrip("\x1e").strip()
            if not line or line.startswith("{" ) and '"FeatureCollection"' in line[:40]:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            feature = _to_feature(obj, next_id)
            if feature is None:
                continue
            if feature.osm_id < 0:
                next_id -= 1
            yield feature


def _to_feature(obj: dict, fallback_id: int) -> OSMFeature | None:
    props = obj.get("properties") or {}
    tags = {str(key): str(value) for key, value in props.items()
            if not str(key).startswith("@")}
    raw_id = props.get("@id", props.get("id"))
    if isinstance(raw_id, str) and "/" in raw_id:
        raw_id = raw_id.rsplit("/", 1)[-1]
    try:
        osm_id = int(raw_id)
    except (TypeError, ValueError):
        osm_id = fallback_id
    kind = str(props.get("@type") or "way").lower()
    if kind not in ("node", "way", "relation"):
        kind = "way"
    geom = obj.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Point":
        ring = _rings([coords])
        return OSMFeature(osm_id, "node", tags, ring) if ring else None
    if gtype == "LineString":
        ring = _rings(coords)
        return OSMFeature(osm_id, kind if kind != "node" else "way", tags, ring) if len(ring) >= 2 else None
    if gtype == "Polygon":
        return _polygon(osm_id, kind, tags, coords)
    if gtype == "MultiPolygon":
        roles = []
        for polygon in coords or []:
            made = _polygon(osm_id, kind, tags, polygon)
            if made is not None:
                roles.extend(made.role_geoms or [("outer", made.geometry)])
        if not roles:
            return None
        return OSMFeature(osm_id, "relation", tags, [ring for _role, ring in roles], roles)
    return None


def _polygon(osm_id: int, kind: str, tags: dict, coords) -> OSMFeature | None:
    if not coords:
        return None
    outer = _rings(coords[0])
    if len(outer) < 4:
        return None
    roles = [("outer", outer)]
    for hole in coords[1:]:
        ring = _rings(hole)
        if len(ring) >= 4:
            roles.append(("inner", ring))
    if kind == "relation" or len(roles) > 1:
        return OSMFeature(osm_id, "relation", tags, [ring for _role, ring in roles], roles)
    return OSMFeature(osm_id, "way", tags, outer)
