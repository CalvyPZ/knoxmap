"""The smallest Geofabrik regions that cover a selection, kept up to date.

Geofabrik publishes a daily PBF for continents, countries, and — where it
has them — states and smaller divisions. A build takes the finest of those
that still cover the box: one state when the box sits inside it, several
states when it crosses their borders, the country when the children do not
cover the box. The files are cached and replaced when the daily on the
server is newer than the copy here.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import requests

INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
_HEADERS = {"User-Agent": "KnoxMap/1.0 (+https://github.com/CalvyPZ/knoxmap) local map generator"}


@dataclass
class Region:
    id: str
    name: str
    parent: str | None
    url: str
    bbox: tuple[float, float, float, float]  # south, west, north, east

    @property
    def slug(self) -> str:
        return self.id.replace("/", "_").replace("\\", "_")


def cache_dir(root: str) -> str:
    path = os.path.join(root, "cache", "geofabrik")
    os.makedirs(path, exist_ok=True)
    return path


def _box_of(feature: dict) -> tuple[float, float, float, float] | None:
    raw = feature.get("bbox") or (feature.get("properties") or {}).get("bbox")
    if not (isinstance(raw, (list, tuple)) and len(raw) == 4):
        return None
    west, south, east, north = (float(v) for v in raw)
    if not south < north or not west < east:
        return None
    return south, west, north, east


def _parse_index(payload: dict) -> list[Region]:
    out = []
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        url = ((props.get("urls") or {}).get("pbf") or "").strip()
        ident = str(props.get("id") or "").strip()
        box = _box_of(feature)
        if not ident or not url or box is None:
            continue
        parent = props.get("parent")
        out.append(Region(
            id=ident,
            name=str(props.get("name") or ident),
            parent=str(parent) if parent else None,
            url=url,
            bbox=box,
        ))
    return out


def load_index(root: str, refresh: bool = True, timeout: int = 60) -> list[Region]:
    """The published region list, refreshed when the server copy is newer."""
    folder = cache_dir(root)
    path = os.path.join(folder, "index-v1-nogeom.json")
    meta_path = path + ".meta.json"
    if refresh:
        _refresh(INDEX_URL, path, meta_path, timeout)
    if not os.path.exists(path):
        raise RuntimeError(
            "The Geofabrik region list could not be downloaded. "
            "Check the network and start the build again.")
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    regions = _parse_index(payload)
    if not regions:
        raise RuntimeError("The Geofabrik region list had no downloadable areas.")
    return regions


def _meta(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _stamp(response: requests.Response) -> dict:
    return {
        "last_modified": response.headers.get("Last-Modified") or "",
        "etag": response.headers.get("ETag") or "",
        "fetched": time.time(),
    }


def _remote_changed(url: str, meta: dict, timeout: int) -> requests.Response | None:
    """HEAD the file. None when the local stamp still matches."""
    try:
        head = requests.head(url, headers=_HEADERS, timeout=timeout, allow_redirects=True)
        head.raise_for_status()
    except requests.RequestException:
        return None
    remote = _stamp(head)
    if meta.get("last_modified") and meta.get("last_modified") == remote["last_modified"]:
        return None
    if not remote["last_modified"] and meta.get("etag") and meta.get("etag") == remote["etag"]:
        return None
    if not remote["last_modified"] and not remote["etag"]:
        # No stamp to compare. A file fetched today is this daily.
        if meta.get("fetched") and time.time() - float(meta["fetched"]) < 20 * 3600:
            return None
    return head


def _refresh(url: str, path: str, meta_path: str, timeout: int) -> bool:
    """Download url over path when the server copy is newer. Returns True
    when the file on disk changed."""
    meta = _meta(meta_path)
    if os.path.exists(path) and _remote_changed(url, meta, timeout) is None and meta:
        return False
    tmp = path + ".part"
    try:
        with requests.get(url, headers=_HEADERS, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "wb") as fh:
                for chunk in response.iter_content(1 << 16):
                    if chunk:
                        fh.write(chunk)
            stamp = _stamp(response)
    except requests.RequestException:
        if os.path.exists(tmp):
            os.remove(tmp)
        if os.path.exists(path):
            return False
        raise
    os.replace(tmp, path)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(stamp, fh)
    return True


def pbf_path(root: str, region: Region) -> str:
    return os.path.join(cache_dir(root), region.slug + ".osm.pbf")


def ensure_pbf(root: str, region: Region, timeout: int = 120,
               progress=None) -> str:
    """The region's latest daily PBF, downloaded when the cache is older."""
    path = pbf_path(root, region)
    meta_path = path + ".meta.json"
    if progress:
        progress(region.name)
    changed = _refresh(region.url, path, meta_path, timeout)
    if changed:
        folder = cache_dir(root)
        for name in os.listdir(folder):
            if name.startswith(region.slug + ".f") and name.endswith(".osm.pbf"):
                os.remove(os.path.join(folder, name))
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        raise RuntimeError(f"Could not download the daily extract for {region.name}.")
    return path


def _intersects(box: tuple[float, float, float, float],
                query: tuple[float, float, float, float]) -> bool:
    south, west, north, east = box
    qs, qw, qn, qe = query
    return not (north < qs or south > qn or east < qw or west > qe)


def _contains(box: tuple[float, float, float, float],
              query: tuple[float, float, float, float]) -> bool:
    south, west, north, east = box
    qs, qw, qn, qe = query
    return south <= qs and west <= qw and north >= qn and east >= qe


def _contains_point(box: tuple[float, float, float, float],
                    lat: float, lon: float) -> bool:
    south, west, north, east = box
    return south <= lat <= north and west <= lon <= east


def _area(box: tuple[float, float, float, float]) -> float:
    south, west, north, east = box
    return max(0.0, north - south) * max(0.0, east - west)


def _uncovered(query: tuple[float, float, float, float], kids: list[Region]) -> float:
    """Share of a coarse grid over the query that no child box contains."""
    qs, qw, qn, qe = query
    if qn <= qs or qe <= qw or not kids:
        return 1.0
    miss = 0
    steps = 8
    for i in range(steps):
        for j in range(steps):
            lat = qs + (qn - qs) * (i + 0.5) / steps
            lon = qw + (qe - qw) * (j + 0.5) / steps
            if not any(_contains_point(kid.bbox, lat, lon) for kid in kids):
                miss += 1
    return miss / (steps * steps)


def cover(regions: list[Region],
          south: float, west: float, north: float, east: float) -> list[Region]:
    """The smallest published regions whose boxes cover this selection.

    One region when the box fits inside it. The children, each followed down
    the same way, when the selection crosses them and they actually cover it.
    """
    query = (south, west, north, east)
    by_id = {region.id: region for region in regions}
    children: dict[str, list[Region]] = {}
    for region in regions:
        if region.parent and region.parent in by_id:
            children.setdefault(region.parent, []).append(region)

    containers = [region for region in regions if _contains(region.bbox, query)]
    if not containers:
        touching = [region for region in regions if _intersects(region.bbox, query)]
        if not touching:
            raise RuntimeError("No Geofabrik region covers that selection.")
        touching.sort(key=lambda region: _area(region.bbox))
        start = touching[0]
    else:
        containers.sort(key=lambda region: _area(region.bbox))
        start = containers[0]

    chosen: list[Region] = []

    def descend(node: Region) -> None:
        kids = [kid for kid in children.get(node.id, []) if _intersects(kid.bbox, query)]
        holders = [kid for kid in kids if _contains(kid.bbox, query)]
        if holders:
            holders.sort(key=lambda region: _area(region.bbox))
            descend(holders[0])
            return
        if kids and _uncovered(query, kids) <= 0.02:
            for kid in kids:
                descend(kid)
            return
        chosen.append(node)

    descend(start)
    seen: set[str] = set()
    out = []
    for region in chosen:
        if region.id in seen:
            continue
        seen.add(region.id)
        out.append(region)
    if not out:
        raise RuntimeError("No Geofabrik region covers that selection.")
    return out
