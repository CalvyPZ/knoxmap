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
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

# The nogeom index has no geometry and no bbox, so every region would be
# skipped. The full index is a few MB and carries each region's outline.
INDEX_URL = "https://download.geofabrik.de/index-v1.json"
_HEADERS = {"User-Agent": "KnoxMap/1.0 (+https://github.com/CalvyPZ/knoxmap) local map generator"}


@dataclass
class Region:
    id: str
    name: str
    parent: str | None
    url: str
    bbox: tuple[float, float, float, float]  # south, west, north, east
    geometry: dict | None = field(default=None, repr=False, compare=False)

    @property
    def slug(self) -> str:
        return self.id.replace("/", "_").replace("\\", "_")


def cache_dir(root: str) -> str:
    path = os.path.join(root, "cache", "geofabrik")
    os.makedirs(path, exist_ok=True)
    return path


def _outline_box(coords) -> tuple[float, float, float, float] | None:
    lons: list[float] = []
    lats: list[float] = []
    stack = [coords]
    while stack:
        item = stack.pop()
        if not isinstance(item, (list, tuple)) or not item:
            continue
        if isinstance(item[0], (int, float)):
            if len(item) >= 2:
                lons.append(float(item[0]))
                lats.append(float(item[1]))
            continue
        stack.extend(item)
    if not lons:
        return None
    return min(lats), min(lons), max(lats), max(lons)


def _box_of(feature: dict) -> tuple[float, float, float, float] | None:
    raw = feature.get("bbox") or (feature.get("properties") or {}).get("bbox")
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        west, south, east, north = (float(v) for v in raw)
        box = (south, west, north, east)
    else:
        box = _outline_box((feature.get("geometry") or {}).get("coordinates"))
        if box is None:
            return None
    south, west, north, east = box
    if not south < north or not west < east:
        return None
    return box


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
            geometry=feature.get("geometry")
                     if (feature.get("geometry") or {}).get("type")
                     in ("Polygon", "MultiPolygon") else None,
        ))
    return out


def load_index(root: str, refresh: bool = True, timeout: int = 60) -> list[Region]:
    """The published region list, refreshed when the server copy is newer."""
    folder = cache_dir(root)
    path = os.path.join(folder, "index-v1.json")
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


_CONTINENT_IDS = {
    "africa": "africa",
    "asia": "asia",
    "europe": "europe",
    "north america": "north-america",
    "south america": "south-america",
    "oceania": "australia-oceania",
    "australia and oceania": "australia-oceania",
}


def continent_outline(root: str, name: str) -> dict | None:
    """Geofabrik's OSM extract outline for a named continent.

    OSM represents continents as points rather than administrative boundary
    relations. Geofabrik's published OSM region index supplies the polygon
    used by its continent extracts, so continent selection can still have an
    outline instead of degenerating to Nominatim's rectangular extent.
    """
    wanted = _CONTINENT_IDS.get(" ".join((name or "").lower().split()))
    if not wanted:
        return None
    for region in load_index(root):
        if region.id == wanted:
            return region.geometry
    return None


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


# A "...-latest" name is not the file. Following every redirect runs into a
# loop, so these are read one hop at a time and only from a GET.
_REDIRECTS = (301, 302, 303, 307, 308)


def _https(url: str) -> str:
    """The redirect names an http URL. The same path is served on https."""
    marker = "://download.geofabrik.de/"
    if url.startswith("http" + marker):
        return "https" + url[4:]
    return url


def _file_url(url: str) -> str:
    """A dated extract is published with a trailing slash, and that URL 404s.

    The "...-latest" name with a slash is a real hop, so that slash stays.
    """
    url = _https(url)
    if url.endswith(".osm.pbf/") and not url.endswith("-latest.osm.pbf/"):
        return url[:-1]
    return url


def _probe_headers() -> dict:
    """Bypass the proxy cache. A HEAD of a latest name is cached as a 301 to
    itself, and the next GET is then served that loop instead of the dated file."""
    headers = dict(_HEADERS)
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    return headers


def _settle(url: str, timeout: int) -> str:
    """The URL that holds the bytes.

    A "...-latest" extract answers GET with 301 to the same path plus a
    slash, and that slashed name answers with the dated file. Following
    either hop automatically runs into the cached slash loop, which is the
    "Exceeded 30 redirects" failure. Each hop is read on its own. A HEAD is
    used only once the name is already a real file; heading the latest name
    is what poisons the cache.
    """
    current = _file_url(url)
    seen: set[str] = set()
    for _ in range(5):
        if current in seen:
            break
        seen.add(current)
        if "-latest." not in current and not current.endswith("/"):
            head = requests.head(
                current, headers=_HEADERS, timeout=timeout, allow_redirects=False)
            try:
                if head.status_code not in _REDIRECTS:
                    head.raise_for_status()
                    return current
            finally:
                head.close()
        response = requests.get(
            current, headers=_probe_headers(), timeout=timeout,
            allow_redirects=False, stream=True)
        try:
            if response.status_code not in _REDIRECTS:
                response.raise_for_status()
                return current
            location = (response.headers.get("Location") or "").strip()
        finally:
            response.close()
        if not location:
            return current
        nxt = _file_url(urljoin(current, location))
        if nxt == current:
            slashed = current if current.endswith("/") else current + "/"
            if slashed in seen:
                return current
            current = slashed
            continue
        current = nxt
    return current


def _same_file(url: str, meta: dict, timeout: int) -> bool | None:
    """Whether the settled file is the one already recorded.

    True when the stamp matches, False when it does not, None when the
    check itself failed. Redirects are not followed: a dated file that has
    started redirecting is a different file.
    """
    try:
        head = requests.head(url, headers=_HEADERS, timeout=timeout, allow_redirects=False)
        if head.status_code in _REDIRECTS:
            head.close()
            return False
        head.raise_for_status()
    except requests.RequestException:
        return None
    remote = _stamp(head)
    head.close()
    if meta.get("last_modified") and meta.get("last_modified") == remote["last_modified"]:
        return True
    if not remote["last_modified"] and meta.get("etag") and meta.get("etag") == remote["etag"]:
        return True
    if not remote["last_modified"] and not remote["etag"]:
        # No stamp to compare. A file fetched today is this daily.
        if meta.get("fetched") and time.time() - float(meta["fetched"]) < 20 * 3600:
            return True
    return False


class _Rate:
    """Bytes a second over the last few seconds, so one stall or burst does
    not swing the estimate."""

    WINDOW = 4.0

    def __init__(self):
        self.samples: deque[tuple[float, int]] = deque()

    def add(self, now: float, done: int) -> float:
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
            self.samples.popleft()
        first_time, first_done = self.samples[0]
        span = now - first_time
        return (done - first_done) / span if span > 0 else 0.0


def _refresh(url: str, path: str, meta_path: str, timeout: int,
             on_bytes=None) -> bool:
    """Download url over path when the server copy is newer. Returns True
    when the file on disk changed.

    on_bytes(done, total, bytes_per_second) is called about twice a second
    while the file comes down. total is 0 when the server does not say.
    """
    meta = _meta(meta_path)
    try:
        settled = _settle(url, timeout)
    except requests.RequestException:
        if os.path.exists(path):
            return False
        raise
    # The dated name is the daily. The same name is the same file, so a
    # later build does not download it again.
    if os.path.exists(path) and meta.get("url") == settled:
        return False
    # A copy saved before the dated name was remembered. Its stamp still
    # matches the settled file when that file is the one already on disk.
    if os.path.exists(path) and meta and not meta.get("url"):
        same = _same_file(settled, meta, timeout)
        if same is None:
            return False
        if same:
            meta["url"] = settled
            with open(meta_path, "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
            return False
    tmp = path + ".part"
    try:
        with requests.get(settled, headers=_HEADERS, timeout=timeout,
                          stream=True, allow_redirects=False) as response:
            if response.status_code in _REDIRECTS:
                raise requests.HTTPError(
                    f"The extract at {settled} is still a redirect.", response=response)
            response.raise_for_status()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            done = 0
            rate = _Rate()
            last = 0.0
            if on_bytes:
                on_bytes(0, total, 0.0)
            with open(tmp, "wb") as fh:
                for chunk in response.iter_content(1 << 16):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if on_bytes:
                        now = time.monotonic()
                        speed = rate.add(now, done)
                        if now - last >= 0.5:
                            last = now
                            on_bytes(done, total, speed)
            if on_bytes:
                on_bytes(done, total or done, 0.0)
            stamp = _stamp(response)
            stamp["url"] = settled
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
    """The region's latest daily PBF, downloaded when the cache is older.

    progress(done, total, bytes_per_second) follows the download, if there
    is one; a copy that is already current never calls it.
    """
    path = pbf_path(root, region)
    meta_path = path + ".meta.json"
    changed = _refresh(region.url, path, meta_path, timeout, on_bytes=progress)
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
