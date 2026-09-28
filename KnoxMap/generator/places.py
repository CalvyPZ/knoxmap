"""Finding places: search by name, and list landmarks inside a bbox.

Two different services, for two different jobs:

* Nominatim answers "where is Lexington High School" - free-text geocoding,
  used to jump the map somewhere and pre-set the selection rectangle.
* Overpass answers "what is inside this rectangle" - the named schools, shops
  and civic buildings the area actually contains, which is what decides whether
  an area is worth turning into a map.

Both are public OSM services with usage policies. They require a real
User-Agent (the default python-requests string gets a 403), and Nominatim asks
for at most one request per second, which `_throttle` enforces process-wide.
"""
from __future__ import annotations

import threading
import time

import requests

HEADERS = {
    "User-Agent": "KnoxMap/1.0 (+https://github.com/spytheeuclidean-a11y/knoxmap) local map generator",
    "Accept-Language": "en",
}

# Nominatim's public server is shared and has a usage policy every client must
# follow: https://operations.osmfoundation.org/policies/nominatim/
# One request a second (see _throttle), a real User-Agent (HEADERS), results
# cached (app.py), no bulk use. The page waits briefly after typing before it
# searches, and a newer query replaces one still in flight.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Nominatim uses both place types and administrative-boundary records for
# regions. The exact admin_level meaning varies by country, so it is useful for
# a human label but not as the only test for whether a result is a region.
REGION_ADDRESS_TYPES = {
    "continent", "country", "state", "state_district", "province", "region",
    "county", "municipality", "city", "city_district", "town", "borough",
    "district", "village",
}
REGION_PLACE_TYPES = REGION_ADDRESS_TYPES | {"administrative"}
MAX_OUTLINE_POINTS = 18000

_lock = threading.Lock()
_last_call = 0.0
MIN_INTERVAL = 1.0


def _throttle() -> None:
    """Nominatim's usage policy allows one request per second."""
    global _last_call
    with _lock:
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def search(query: str, viewbox: tuple[float, float, float, float] | None = None,
           bounded: bool = False, limit: int = 8,
           regions_only: bool = False) -> list[dict]:
    """Geocode `query`. viewbox is (south, west, north, east) to bias results."""
    query = (query or "").strip()
    if not query:
        return []

    request_limit = 20 if regions_only else limit
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(max(1, min(request_limit, 20))),
        "addressdetails": "1",
        "extratags": "1",
        # Real outlines for towns, districts and parks, simplified to about ten
        # metres so a city boundary does not arrive as megabytes of points.
        "polygon_geojson": "1",
        "polygon_threshold": "0.0001",
    }
    if viewbox:
        s, w, n, e = viewbox
        # Nominatim wants <left>,<top>,<right>,<bottom>.
        params["viewbox"] = f"{w},{n},{e},{s}"
        if bounded:
            params["bounded"] = "1"

    _throttle()
    r = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()

    out = []
    for item in r.json():
        bb = item.get("boundingbox") or []
        try:
            south, north, west, east = (float(bb[0]), float(bb[1]),
                                        float(bb[2]), float(bb[3]))
        except (ValueError, IndexError):
            continue
        outline = normalise_outline(item.get("geojson"))
        category = item.get("category") or item.get("class") or ""
        place_type = item.get("type") or ""
        address_type = item.get("addresstype") or ""
        extras = item.get("extratags") or {}
        admin_level = str(extras.get("admin_level") or "")
        is_region = _is_region(category, place_type, address_type, admin_level)
        if regions_only and not is_region:
            continue
        out.append({
            "name": item.get("name") or item.get("display_name", "").split(",")[0],
            "display_name": item.get("display_name", ""),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "bbox": [south, west, north, east],
            "category": category,
            "type": place_type,
            "address_type": address_type,
            "admin_level": admin_level,
            "region": is_region,
            "region_type": _region_type(place_type, address_type, admin_level)
                           if is_region else "",
            "outline": outline,
        })
    return out[:max(1, limit)]


def _is_region(category: str, place_type: str, address_type: str,
               admin_level: str) -> bool:
    """Whether a Nominatim result represents a whole named region."""
    category = category.lower()
    place_type = place_type.lower()
    address_type = address_type.lower()
    if category == "boundary" and place_type == "administrative":
        return True
    if address_type in REGION_ADDRESS_TYPES:
        return True
    return category in {"place", "boundary"} and place_type in REGION_PLACE_TYPES \
        and bool(admin_level or place_type != "administrative")


def _region_type(place_type: str, address_type: str, admin_level: str) -> str:
    """A concise, internationally useful label for an OSM region."""
    kind = (address_type or place_type).lower()
    labels = {
        "continent": "Continent", "country": "Country", "state": "State",
        "province": "Province", "region": "Region", "state_district": "Region",
        "county": "County", "municipality": "Council / municipality",
        "city": "City", "city_district": "City district", "town": "Town",
        "borough": "Borough", "district": "District", "village": "Village",
    }
    if kind in labels:
        return labels[kind]
    try:
        level = int(admin_level)
    except (TypeError, ValueError):
        return "Administrative region"
    if level <= 2:
        return "Country"
    if level <= 4:
        return "State / region"
    if level <= 6:
        return "Council / county"
    if level <= 8:
        return "City / municipality"
    return "Town / district"


def _point_count(geojson: dict) -> int:
    kind = geojson.get("type")
    polygons = ([geojson.get("coordinates") or []] if kind == "Polygon"
                else geojson.get("coordinates") or [])
    return sum(len(ring) for polygon in polygons for ring in polygon)


def normalise_outline(geojson: dict | None) -> dict | None:
    """The place's own boundary, when it has one worth drawing a map in."""
    if not geojson or geojson.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    if _point_count(geojson) > MAX_OUTLINE_POINTS:
        # Detailed coastlines can exceed the renderer's safe request size even
        # after Nominatim's simplification. Preserve topology while reducing
        # detail instead of silently removing the Select region action.
        try:
            from shapely.geometry import mapping, shape
            geometry = shape(geojson)
            minx, miny, maxx, maxy = geometry.bounds
            tolerance = max(maxx - minx, maxy - miny) / 100000
            for _ in range(12):
                geometry = geometry.simplify(tolerance, preserve_topology=True)
                candidate = mapping(geometry)
                if (candidate.get("type") in ("Polygon", "MultiPolygon")
                        and _point_count(candidate) <= MAX_OUTLINE_POINTS):
                    return candidate
                tolerance *= 2
        except (ImportError, TypeError, ValueError):
            return None
        return None
    return geojson


# Tags worth listing as landmarks. Anything named under these keys is something
# a mapper would recognise on the ground.
LANDMARK_KEYS = ("amenity", "shop", "leisure", "tourism", "historic",
                 "office", "healthcare", "building")

# Buildings are tagged building=yes in bulk, so only list named ones whose
# value says something.
BORING_BUILDING_VALUES = {"yes", "residential", "house", "detached", "garage",
                          "shed", "hut", "roof", "apartments"}


def _build_query(south: float, west: float, north: float, east: float,
                 timeout: int = 30) -> str:
    bbox = f"{south},{west},{north},{east}"
    parts = []
    for key in LANDMARK_KEYS:
        for kind in ("node", "way", "relation"):
            parts.append(f'{kind}["{key}"]["name"]({bbox});')
    body = "\n  ".join(parts)
    return f"[out:json][timeout:{timeout}];\n(\n  {body}\n);\nout center tags;\n"


def landmarks(south: float, west: float, north: float, east: float,
              limit: int = 300) -> list[dict]:
    """Named landmarks inside the bbox, most specific category first."""
    query = _build_query(south, west, north, east)
    last_err: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(endpoint, data={"data": query},
                              headers=HEADERS, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                last_err = RuntimeError(f"{endpoint} -> {r.status_code}")
                continue
            r.raise_for_status()
            return _parse(r.json(), limit)
        except (requests.RequestException, ValueError) as exc:
            last_err = exc
            continue
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def _parse(payload: dict, limit: int) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for el in payload.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:
            centre = el.get("center") or {}
            lat, lon = centre.get("lat"), centre.get("lon")
        if lat is None or lon is None:
            continue

        kind = value = None
        for key in LANDMARK_KEYS:
            if key in tags:
                if key == "building" and tags[key] in BORING_BUILDING_VALUES:
                    continue
                kind, value = key, tags[key]
                break
        if not kind:
            continue

        dedupe = (name, value)
        if dedupe in seen:
            continue
        seen.add(dedupe)

        out.append({
            "name": name,
            "kind": kind,
            "value": value,
            "lat": float(lat),
            "lon": float(lon),
            "_rank": _notability(kind, value, tags),
        })

    # Most notable first, then cut. Sorting by name of category before the
    # cut meant a busy street's alcohol shops, bakeries and bars filled the
    # list and every museum, church and school - later in the alphabet - was
    # silently dropped.
    out.sort(key=lambda d: (-d["_rank"], d["value"], d["name"].lower()))
    kept = out[:limit]
    for d in kept:
        del d["_rank"]
    kept.sort(key=lambda d: (d["value"], d["name"].lower()))
    return kept


# What a player would navigate by. Weighted by key, raised for the values that
# are landmarks in any town, and again when the place has its own Wikipedia or
# Wikidata entry - a fair sign that people outside the street have heard of it.
KEY_WEIGHT = {"historic": 6, "tourism": 5, "leisure": 3, "amenity": 3,
              "healthcare": 2, "building": 2, "office": 1, "shop": 1}
NOTABLE_VALUES = {
    "place_of_worship": 4, "church": 4, "mosque": 4, "cathedral": 5,
    "synagogue": 4, "temple": 4, "school": 3, "university": 4, "college": 3,
    "hospital": 4, "townhall": 4, "police": 3, "fire_station": 3,
    "library": 3, "museum": 5, "attraction": 4, "castle": 5, "monument": 4,
    "park": 3, "stadium": 4, "marketplace": 3, "theatre": 3, "cinema": 2,
    "train_station": 4, "bus_station": 2, "supermarket": 2, "mall": 3,
    "department_store": 2, "fuel": 2, "pharmacy": 1, "post_office": 2,
    "prison": 4, "courthouse": 3, "hotel": 2,
}


def _notability(kind: str, value: str, tags: dict) -> int:
    score = KEY_WEIGHT.get(kind, 0) + NOTABLE_VALUES.get(value, 0)
    if "wikidata" in tags or "wikipedia" in tags:
        score += 4
    return score
