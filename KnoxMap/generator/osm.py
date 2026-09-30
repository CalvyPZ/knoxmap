"""Query OpenStreetMap via the Overpass API.

We only pull tags that map cleanly onto PZ terrain categories — everything
else is ignored. The query asks for a single bbox and returns ways/relations
with their full geometry so we can rasterize without a second roundtrip.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import requests

import knoxstop

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    # A third, because the other two go busy together: when one instance
    # is queueing the people it turned away are queueing on the next.
    # It is last because it is the one that answers an ordinary query with an
    # empty result - see EMPTY_NEEDS_SECOND below.
    "https://overpass.osm.ch/api/interpreter",
]
# overpass.openstreetmap.fr answers every query with 403 "This service is only
# available to white-listed usages". It was tried once per tile, and on the
# tiles whose turn it was it pushed the work onto whatever came next.

# Whether an empty answer has to be confirmed by another instance before the
# tile is taken as empty ground (fetch_features), and how many have to agree.
# The flag is off only for tests, which would otherwise ask the real servers.
EMPTY_NEEDS_SECOND = True
BLANKS_TO_BELIEVE = 2

# OSM's usage policy requires a real identifying User-Agent; the mirrors return
# 403 for the default "python-requests/x.y" string.
HEADERS = {"User-Agent": "KnoxMap/1.0 (+https://github.com/spytheeuclidean-a11y/knoxmap) local map generator"}

# Tag filters — each line becomes one part of the Overpass union query.
# Order doesn't matter here; the rasterizer picks priority at paint time.
OVERPASS_FILTERS: Sequence[str] = (
    # water
    # The sea has no polygon in OSM, only the line of the shore, drawn with the
    # land on its left. Without it every coastal town was a meadow to the
    # horizon - see renderer.sea_polygons.
    'way["natural"="coastline"]',
    # Piers and breakwaters stand on the water; without them a marina's
    # jetties vanish and anything built on one floats.
    'way["man_made"~"^(pier|breakwater|groyne)$"]',
    # Railways: the lines a town grew along, and on the paper map. Trams run
    # in the street and are left to the road under them.
    'way["railway"~"^(rail|light_rail|narrow_gauge|disused|preserved)$"]',
    'way["natural"="water"]',
    'way["waterway"]',
    'relation["natural"="water"]',
    'way["landuse"="reservoir"]',
    'way["landuse"="basin"]',
    # forest / trees
    'way["landuse"="forest"]',
    'way["natural"="wood"]',
    'relation["landuse"="forest"]',
    'relation["natural"="wood"]',
    'way["natural"="scrub"]',
    'way["natural"="heath"]',
    'node["natural"="tree"]',
    # grass / parks / farms
    'way["landuse"="grass"]',
    'way["landuse"="meadow"]',
    'way["landuse"="farmland"]',
    'way["landuse"="farmyard"]',
    'way["leisure"="park"]',
    'way["leisure"="garden"]',
    'way["leisure"="pitch"]',
    # sand / beach
    'way["natural"="beach"]',
    'way["natural"="sand"]',
    # dirt
    'way["landuse"="brownfield"]',
    'way["landuse"="construction"]',
    'way["landuse"="quarry"]',
    # roads
    'way["highway"]',
    'way["area:highway"]',
    'way["place"="square"]',
    # What controls a junction. Ways come back as coordinates with no node
    # ids, so the signal, the stop and the crossing have to be their own
    # nodes (generator/intersections.py). A way crossing a tile is already
    # complete; these nodes are not on it.
    'node["highway"~"^(traffic_signals|stop|give_way|crossing|mini_roundabout|'
    'turning_circle|turning_loop)$"]',
    # Monuments, statues and fountains: generator/structures.py
    'node["historic"~"^(monument|memorial)$"]',
    'way["historic"~"^(monument|memorial)$"]',
    'nwr["man_made"="obelisk"]',
    'node["tourism"="artwork"]',
    'nwr["amenity"="fountain"]',
    # Towers: a water tower, a lighthouse, a windmill or a clock tower is a
    # landmark somebody navigates by, not a house. Without this they arrive
    # only when the mapper also tagged building=*, and then as a bungalow.
    'nwr["man_made"~"^(water_tower|lighthouse|windmill|tower)$"]',
    # buildings
    'way["building"]',
    'relation["building"]',
    # What the ground between buildings is used for. Without these a factory
    # yard, a schoolyard and a back garden all came out as the same wild grass,
    # which is most of why a generated town looked like nowhere in particular.
    'way["landuse"~"^(residential|commercial|retail|industrial|railway|garages|'
    'military|cemetery|orchard|vineyard|allotments)$"]',
    'relation["landuse"~"^(residential|commercial|retail|industrial|railway|'
    'military|cemetery|orchard|vineyard)$"]',
    # Airfields. The aerodrome is the grounds; runways, taxiways and aprons
    # are the paved part of it, usually a line with a width or a closed way.
    'way["aeroway"~"^(aerodrome|apron|runway|taxiway|helipad|heliport|terminal|hangar)$"]',
    'relation["aeroway"~"^(aerodrome|apron|runway|taxiway|helipad|heliport|terminal|hangar)$"]',
    # Station grounds, platforms and yards when they are not landuse=railway.
    # The rails themselves are the railway filter further up.
    'way["railway"~"^(station|halt|platform|yard)$"]',
    'relation["railway"~"^(station|halt|platform|yard)$"]',
    'way["amenity"~"^(parking|school|university|college|kindergarten|hospital|'
    'clinic|bus_station|grave_yard|marketplace|place_of_worship)$"]',
    'relation["amenity"~"^(parking|school|university|college|hospital|'
    'grave_yard|marketplace)$"]',
    'way["leisure"~"^(playground|swimming_pool|sports_centre|stadium|track)$"]',
    'way["natural"~"^(grassland|wetland)$"]',
    # Property lines. Fences and walls become real fences in game; hedges
    # become rows of bushes.
    'way["barrier"~"^(fence|wall|hedge|retaining_wall|city_wall)$"]',
    # Official head counts, where mappers recorded one, to check the
    # population estimate against.
    'node["place"~"^(city|town|village|suburb|quarter|neighbourhood|hamlet)$"]'
    '["population"]',
    # What the ground floors are: the pizza place, the bank, the pharmacy.
    # Mapped as points inside the building far more often than as the
    # building's own tags (knoxbuild/uses.py).
    'node["shop"]',
    'node["amenity"~"^(restaurant|fast_food|food_court|cafe|ice_cream|bar|pub|nightclub|'
    'biergarten|bank|bureau_de_change|post_office|pharmacy|dentist|doctors|clinic|'
    'veterinary|library|cinema|theatre|arts_centre|police|childcare|kindergarten|fuel|car_repair)$"]',
    'node["office"]',
    'node["craft"]',
    'node["healthcare"]',
    'node["leisure"~"^(fitness_centre|sports_centre|dance|bowling_alley)$"]',
    'node["tourism"~"^(hotel|motel|hostel|guest_house|museum|gallery)$"]',
    # Bases, armouries, barracks: mapped with military=* as often as with
    # landuse=military, and without these an armoury was somebody's house.
    'way["military"]',
    'relation["military"]',
    # Homes that were surveyed but never drawn. In whole countries, and in
    # most American suburbs, a house is one node with its number on it and
    # nothing else; those streets came out as roads through empty grass
    # (generator/renderer.py _houses_from_addresses).
    'node["addr:housenumber"]',
)

# Bumped whenever the filters above change, so a cached download made with
# the old list is fetched again instead of silently lacking the new features.
FILTERS_VERSION = 12


@dataclass
class OSMFeature:
    osm_id: int
    kind: str             # "way" or "relation" or "node"
    tags: dict
    geometry: list        # for way: list of (lat, lon); relation: list of ring lists
    role_geoms: list = field(default_factory=list)  # relation members with roles


def _build_query(south: float, west: float, north: float, east: float,
                 timeout: int = 60) -> str:
    # Catch an out-of-range bbox here rather than letting Overpass answer with
    # a static error, which arrives as an XHTML page and costs a round trip to
    # all three servers to learn the same thing.
    for name, value, limit in (("south", south, 90.0), ("north", north, 90.0),
                               ("west", west, 180.0), ("east", east, 180.0)):
        if not -limit <= value <= limit:
            raise OverpassError(
                f"{name}={value:g} is outside ±{limit:g}. The map widget "
                f"reports coordinates unwrapped after panning across a world "
                f"copy; they need folding back before use.")
    bbox = f"{south},{west},{north},{east}"
    parts = [f"{f}({bbox});" for f in OVERPASS_FILTERS]
    body = "\n  ".join(parts)
    return (
        f"[out:json][timeout:{timeout}];\n"
        f"(\n  {body}\n);\n"
        f"out geom;\n"
    )


class OverpassError(RuntimeError):
    """A failed query, with whether a smaller bbox would plausibly succeed."""

    def __init__(self, message: str, too_big: bool = False,
                 timed_out: bool = False):
        super().__init__(message)
        self.too_big = too_big
        # Nothing answered at all, as opposed to answering with a refusal.
        self.timed_out = timed_out


# Overpass says "query timed out" and "out of memory" with HTTP 400, the same
# status it uses for a syntax error. Reading the status alone turns "your area
# is too big for this server" into "Bad Request", which sends you looking for a
# bug in a query that is perfectly valid. The reason is in the body.
_TOO_BIG = ("timed out", "out of memory", "runtime error")
# ...except when the query really is malformed. Splitting the bbox cannot fix
# a syntax error, it just asks four times and fails four times.
_MY_FAULT = ("parse error", "unknown type", "static error")

_TAGS = re.compile(r"<[^>]+>")
_PARAS = re.compile(r"<p>(.*?)</p>", re.S | re.I)


def _error_text(body: str) -> str:
    """The human-readable complaint out of an Overpass error response.

    Errors come back as a full XHTML page whose first few hundred characters
    are a DOCTYPE and a namespace declaration. Truncating that to make it fit
    in a message shows the reader the doctype and nothing else, which is
    exactly what a failed town looked like: three endpoints, three identical
    walls of XHTML boilerplate, no reason among them.
    """
    if "<html" not in body[:400].lower():
        return " ".join(body.split())[:400]
    paragraphs = []
    for raw in _PARAS.findall(body):
        text = " ".join(_TAGS.sub(" ", raw).split())
        # The copyright notice is on every page, error or not.
        if not text or "openstreetmap.org" in text.lower():
            continue
        paragraphs.append(text)
    said = [p for p in paragraphs if "error" in p.lower()] or paragraphs
    return " | ".join(said)[:400] or " ".join(body.split())[:400]


def _ask(endpoint: str, query: str, timeout: int) -> list[OSMFeature]:
    r = requests.post(endpoint, data={"data": query}, headers=HEADERS,
                      timeout=timeout + 10)
    if r.status_code == 200:
        payload = r.json()
        # A query that runs out of time or memory part way through does not
        # fail: Overpass sends HTTP 200 with whatever it had gathered and puts
        # the reason in "remark". Reading only the status took that for a
        # finished tile, so a town downloaded "successfully" with half its
        # streets missing and nothing anywhere said so.
        remark = str(payload.get("remark") or "")
        if any(s in remark.lower() for s in _TOO_BIG):
            raise OverpassError(f"partial answer — {' '.join(remark.split())}",
                                too_big=True)
        return _parse(payload)
    # Classify on the whole body, report a trimmed version of it. Doing both
    # from the trimmed text is what stopped over-large areas re-splitting: the
    # markers sit well past the doctype, so nothing ever looked too big.
    whole = r.text.lower()
    message = _error_text(r.text)
    too_big = (any(s in whole for s in _TOO_BIG)
               and not any(s in whole for s in _MY_FAULT))
    raise OverpassError(f"HTTP {r.status_code}"
                        + (f" — {message}" if message else ""),
                        too_big=too_big)


def fetch_features(south: float, west: float, north: float, east: float,
                   timeout: int = 60, first: int = 0) -> list[OSMFeature]:
    """Run the Overpass query against the first endpoint that answers.

    `first` rotates which endpoint is tried first, so concurrent tiles spread
    themselves over the public instances instead of queueing on one.

    Every endpoint's complaint is kept, not just the last one. The instances
    fail in different ways at the same moment - one 504s, one stops answering,
    one says the query was too heavy - and reporting only the last of those
    describes the least interesting failure as though it were the whole story.
    """
    query = _build_query(south, west, north, east, timeout=timeout)
    errors: list[str] = []
    too_big = False
    timed_out = False
    blank = 0
    order = OVERPASS_ENDPOINTS[first % len(OVERPASS_ENDPOINTS):]         + OVERPASS_ENDPOINTS[:first % len(OVERPASS_ENDPOINTS)]
    for endpoint in order:
        host = endpoint.split("/")[2]
        try:
            feats = _ask(endpoint, query, timeout)
        except OverpassError as exc:
            errors.append(f"{host}: {exc}")
            too_big = too_big or exc.too_big
        except requests.Timeout:
            errors.append(f"{host}: no answer within {timeout + 10}s")
            timed_out = True
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{host}: {exc}")
        else:
            if feats or not EMPTY_NEEDS_SECOND:
                return feats
            # An instance that answers 200 with nothing in it looks exactly
            # like open farmland, and the tiles it was handed went into the map
            # as empty ground. A city came out a meadow with its river still
            # in it, because the tiles that did download held the river.
            # Nothing is only believed when a second instance agrees.
            blank += 1
            errors.append(f"{host}: answered with nothing")
        time.sleep(1)
    # Two instances have to say the tile is empty before it is. One saying so
    # while the others never answered at all is not agreement, it is the one
    # broken instance again - and taking it at its word is what quietly
    # emptied the map in the first place. Raising sends the tile back round
    # fetch_features_tiled's retry, and if it really will not download the
    # download says so instead of handing back a meadow.
    if blank >= BLANKS_TO_BELIEVE:
        return []
    raise OverpassError("every Overpass endpoint failed — "
                        + "; ".join(errors), too_big=too_big,
                        timed_out=timed_out)


def _area_km2(south: float, west: float, north: float, east: float) -> float:
    lat_mid = math.radians((south + north) / 2)
    return abs((north - south) * 111.32 *
               (east - west) * 111.32 * math.cos(lat_mid))


def fetch_features_tiled(south: float, west: float, north: float, east: float,
                         max_tile_km2: float = 30.0, timeout: int = 90,
                         progress=None, should_stop=None) -> list[OSMFeature]:
    """Fetch a large bbox as a grid of smaller Overpass queries.

    One query over a big area either times out or gets refused - that, not the
    renderer, is what used to cap map size. Splitting into tiles of at most
    `max_tile_km2` keeps every individual query the size Overpass is happy
    with.

    The tiles are deliberately large. They used to be 12 km2, sized so that no
    tile could ever be refused, which meant a town was fetched as dozens of
    small requests when three or four big ones would have done. Now that a
    refused tile quarters itself and retries, guessing high costs one wasted
    request on the rare tile that overshoots and saves many on every tile that
    does not.

    A way crossing a tile boundary is returned in full by every tile it touches
    (`out geom` gives complete geometry regardless of clipping), so features are
    de-duplicated on (kind, osm_id) and keep their whole shape.
    """
    area = _area_km2(south, west, north, east)
    steps = max(1, math.ceil(math.sqrt(area / max_tile_km2)))
    if steps == 1:
        return _fetch_splitting(south, west, north, east, timeout)

    d_lat = (north - south) / steps
    d_lon = (east - west) / steps
    merged: dict[tuple[str, int], OSMFeature] = {}
    total = steps * steps

    tiles = []
    for i in range(steps):
        for j in range(steps):
            s0 = south + i * d_lat
            n0 = north if i == steps - 1 else s0 + d_lat
            w0 = west + j * d_lon
            e0 = east if j == steps - 1 else w0 + d_lon
            tiles.append((s0, w0, n0, e0))

    # One tile at a time meant the whole map waited on whichever instance was
    # busiest, one request after another, with a courtesy second between each.
    # There are three independent servers; a tile is handed to each in turn and
    # they work at the same time, so the download takes about as long as the
    # slowest tile rather than the sum of all of them.
    done = [0]
    lock = threading.Lock()

    def run(args):
        index, (s0, w0, n0, e0) = args
        # Between tiles is the one place a download can be dropped without
        # leaving a half-written cache behind; the tiles already in flight
        # finish and are thrown away with the rest.
        knoxstop.check(should_stop, "the download")
        try:
            return index, _fetch_splitting(s0, w0, n0, e0, timeout,
                                           first=index), None
        except OverpassError as exc:
            return index, None, exc
        finally:
            with lock:
                done[0] += 1
                if progress:
                    progress(min(done[0], total), total)

    # One tile failing used to lose the map: the whole download was thrown
    # away and nothing was cached, so a town that had fetched forty tiles and
    # missed one started again from nothing. The tiles that arrived are kept
    # and only the ones that did not are asked for again - a public instance
    # that was busy a moment ago usually is not a minute later.
    workers = min(len(OVERPASS_ENDPOINTS), total)
    left = list(enumerate(tiles))
    problems: dict[int, OverpassError] = {}
    for attempt in range(TILE_ATTEMPTS):
        if attempt:
            for _ in range(RETRY_PAUSE_S):
                knoxstop.check(should_stop, "the download")
                time.sleep(1.0)
        done[0] = total - len(left)
        problems = {}
        again = []
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for index, feats, exc in pool.map(run, left):
                if exc is not None:
                    problems[index] = exc
                    again.append((index, tiles[index]))
                    continue
                for feat in feats:
                    merged[(feat.kind, feat.osm_id)] = feat
        left = again
        if not left:
            break

    if left:
        worst = problems[left[0][0]]
        raise OverpassError(
            f"{len(left)} of {total} map tiles would not download. The public "
            f"Overpass servers are shared and go busy; waiting a few minutes "
            f"and generating again usually works, and a smaller area always "
            f"does. {worst}",
            too_big=any(e.too_big for e in problems.values()),
            timed_out=all(e.timed_out for e in problems.values()))

    return list(merged.values())


# Below this, a tile is small enough that a refusal is the server's problem
# rather than the area's, and splitting further only multiplies the requests.
MIN_SPLIT_KM2 = 0.5
# A tile nothing answered in time is usually a tile too heavy to answer, so it
# is quartered like a refused one - but only while it is big enough for that to
# be the reason, and only one level down. Past that it is the network rather
# than the area, and splitting only takes four times as long to say so.
TIMEOUT_SPLIT_KM2 = 4.0
TIMEOUT_SPLIT_DEPTH = 1
# How many passes over the tiles a map gets, and how long to leave the servers
# alone between them.
TILE_ATTEMPTS = 2
RETRY_PAUSE_S = 20


def _fetch_splitting(south: float, west: float, north: float, east: float,
                     timeout: int, depth: int = 0,
                     first: int = 0) -> list[OSMFeature]:
    """Fetch one tile, quartering it if the servers say it is too heavy.

    How much a bbox costs depends on what is inside it, not its size, so a
    fixed tile grid is always wrong somewhere: the tile holding a dense town
    centre can blow the server's limit while its neighbours over farmland
    return instantly. Giving up there loses the whole map. Splitting only the
    tile that failed costs three extra requests and keeps everything else.
    """
    try:
        return fetch_features(south, west, north, east, timeout=timeout,
                              first=first)
    except OverpassError as exc:
        area = _area_km2(south, west, north, east)
        heavy = exc.too_big or (exc.timed_out and depth < TIMEOUT_SPLIT_DEPTH
                                and area > TIMEOUT_SPLIT_KM2)
        if not heavy or depth >= 3 or area <= MIN_SPLIT_KM2:
            raise
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    out: list[OSMFeature] = []
    for s0, n0 in ((south, mid_lat), (mid_lat, north)):
        for w0, e0 in ((west, mid_lon), (mid_lon, east)):
            out += _fetch_splitting(s0, w0, n0, e0, timeout, depth + 1, first)
            time.sleep(1)
    return out


def cache_path(output_dir: str, map_name: str) -> str:
    return os.path.join(output_dir, f"{map_name}_osm.json.gz")


def save_cache(path: str, bbox: tuple[float, float, float, float],
               feats: list[OSMFeature]) -> None:
    """Keep the Overpass result next to the map it produced.

    Re-rendering is otherwise gated on a fresh download of the whole town,
    which for a real one is a few hundred tiled queries with a second of
    courtesy between each. Every change to how roads or ground are painted
    then costs that download again, for data that has not moved.
    """
    payload = {
        "filters": FILTERS_VERSION,
        "bbox": list(bbox),
        "features": [
            {"osm_id": f.osm_id, "kind": f.kind, "tags": f.tags,
             "geometry": f.geometry, "role_geoms": f.role_geoms}
            for f in feats
        ],
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)


def load_cache(path: str,
               bbox: tuple[float, float, float, float]) -> list[OSMFeature] | None:
    """Cached features for exactly this bbox, or None."""
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if payload.get("filters") != FILTERS_VERSION:
        return None
    cached = payload.get("bbox") or []
    if len(cached) != 4 or any(abs(a - b) > 1e-9 for a, b in zip(cached, bbox)):
        return None
    out = []
    for d in payload.get("features", []):
        out.append(OSMFeature(
            d["osm_id"], d["kind"], d.get("tags") or {},
            [tuple(c) for c in d.get("geometry") or []],
            [(role, [tuple(c) for c in ring])
             for role, ring in d.get("role_geoms") or []],
        ))
    return out


def assemble_rings(role_geoms: list[tuple[str, list[tuple[float, float]]]]
                   ) -> list[tuple[str, list[tuple[float, float]]]]:
    """Join a multipolygon's member ways into closed rings.

    A relation's outline is rarely one way: a lake shore, a harbour or a
    forest edge is split into dozens of ways that only close into a ring end
    to end. Painted one way at a time, each way became a polygon of its own -
    a sliver from one end to the other - so Sydney Harbour came out as 6%
    water in scattered wedges. Ways are merged per role (outer, inner) and
    closed into rings; whatever does not close is kept as it was.
    """
    from shapely.geometry import LineString
    from shapely.ops import linemerge, polygonize, unary_union

    out: list[tuple[str, list[tuple[float, float]]]] = []
    for role in ("outer", "inner"):
        lines = [LineString([(lon, lat) for lat, lon in ring])
                 for r, ring in role_geoms
                 if (r or "outer") == role and len(ring) >= 2]
        if not lines:
            continue
        try:
            faces = list(polygonize(linemerge(unary_union(lines))))
        except Exception:  # noqa: BLE001 - malformed geometry: keep the ways
            faces = []
        if faces:
            out += [(role, [(lat, lon) for lon, lat in face.exterior.coords])
                    for face in faces]
        else:
            out += [(r, ring) for r, ring in role_geoms if (r or "outer") == role]
    return out or role_geoms


def _parse(payload: dict) -> list[OSMFeature]:
    elements = payload.get("elements", [])
    out: list[OSMFeature] = []
    for el in elements:
        kind = el.get("type")
        tags = el.get("tags", {}) or {}
        if kind == "way":
            coords = [(p["lat"], p["lon"]) for p in el.get("geometry", [])]
            if coords:
                out.append(OSMFeature(el["id"], "way", tags, coords))
        elif kind == "relation":
            rings: list[list[tuple[float, float]]] = []
            role_geoms: list[tuple[str, list[tuple[float, float]]]] = []
            for m in el.get("members", []):
                geom = m.get("geometry")
                if not geom:
                    continue
                ring = [(p["lat"], p["lon"]) for p in geom]
                role_geoms.append((m.get("role", ""), ring))
                rings.append(ring)
            if rings:
                role_geoms = assemble_rings(role_geoms)
                feat = OSMFeature(el["id"], "relation", tags,
                                  [ring for _role, ring in role_geoms] or rings)
                feat.role_geoms = role_geoms
                out.append(feat)
        elif kind == "node":
            lat, lon = el.get("lat"), el.get("lon")
            if lat is not None and lon is not None:
                out.append(OSMFeature(el["id"], "node", tags, [(lat, lon)]))
    return out


# Barrier kinds that become fences in game, as opposed to hedges.
FENCE_BARRIERS = {"fence", "wall", "retaining_wall", "city_wall"}

SCHOOL_AMENITIES = {"school", "university", "college", "kindergarten"}


PAVED_SURFACES = {"paved", "asphalt", "concrete", "concrete:plates",
                  "concrete:lanes", "paving_stones", "sett", "cobblestone",
                  "unhewn_cobblestone", "bricks", "metal", "wood", "tiles"}
UNPAVED_SURFACES = {"unpaved", "dirt", "earth", "ground", "grass", "gravel",
                    "fine_gravel", "compacted", "sand", "mud", "pebblestone",
                    "woodchips", "grass_paver"}


# Underground things. A road in a tunnel painted on the surface cut a street
# through whole blocks; a car park under a square covered the square in tarmac.
TUNNEL_VALUES = {"yes", "building_passage", "culvert", "avalanche_protector", "flooded"}

# Track centre lines. Yards, stations and platforms are areas, classified
# with the other land use; these are the rails themselves.
RAIL_TRACKS = frozenset({
    "rail", "light_rail", "narrow_gauge", "disused", "preserved",
})
# Grounds mapped with railway=* rather than landuse=railway.
RAIL_GROUNDS = frozenset({"station", "halt", "platform", "yard"})
# The airfield. Aprons, runways and taxiways are the paved surface on it.
AIRPORT_GROUNDS = frozenset({"aerodrome", "heliport"})
AIRPORT_PAVED = frozenset({
    "apron", "runway", "taxiway", "helipad", "terminal", "hangar",
})
AIRPORT_VALUES = AIRPORT_GROUNDS | AIRPORT_PAVED
# What becomes an area a building can stand in. A runway is a surface, and
# classifying the strip as the site would re-kind whatever it crosses.
AIRPORT_AREAS = AIRPORT_GROUNDS | frozenset({
    "apron", "helipad", "terminal", "hangar",
})


def is_rail_area(tags: dict) -> bool:
    """True when these tags mark railway land, not a track centre line."""
    if tags.get("landuse") == "railway":
        return True
    return tags.get("railway") in RAIL_GROUNDS


def is_airport_ground(tags: dict) -> bool:
    """True when these tags mark the airfield, not a runway or an apron."""
    return tags.get("aeroway") in AIRPORT_GROUNDS


def is_airport_area(tags: dict) -> bool:
    """True when these tags mark airport land, not a runway or a taxiway."""
    return tags.get("aeroway") in AIRPORT_AREAS


def classify(tags: dict, area: bool = False) -> str | None:
    """Map OSM tags to a PZ feature category string. None = ignore.

    `area` says the feature is a closed way or a relation - something with an
    inside - which changes two answers. An arcade is not a tunnel, and a
    pedestrian way that closes on itself is a square rather than a street.
    """
    if "building" in tags:
        return "building"
    # A building passage is a way through a building at ground level, not
    # under it - a colonnade or an archway. Read as a tunnel it took Madrid's
    # Plaza Mayor, 11,437 m2 of it, off the map altogether and left grass.
    underground = tags.get("tunnel") in TUNNEL_VALUES
    if area and tags.get("tunnel") == "building_passage":
        underground = False
    if (underground or tags.get("location") == "underground"
            or tags.get("parking") == "underground"):
        return None

    amenity = tags.get("amenity")
    landuse = tags.get("landuse")
    leisure = tags.get("leisure")
    military = landuse == "military" or (tags.get("military") or "no") != "no"

    # Paved areas before the linear road classes: a pedestrian square and a
    # car park are tagged highway/amenity too, but they are polygons and want
    # a surface, not a stripe down their middle.
    if amenity == "parking" or landuse == "garages":
        return "parking"
    if amenity == "bus_station":
        return "parking"
    # A pedestrian street is drawn as a line and a pedestrian square as a fill.
    # Which it is, is whether the way closes on itself: area=yes says so when
    # the mapper remembered it, and the shape says so either way. The same for
    # the colonnade round a square, which is a footway that comes back to
    # where it started.
    if tags.get("place") == "square" or "area:highway" in tags or (
            tags.get("highway") == "pedestrian"
            and (area or tags.get("area") == "yes")) or (
            area and tags.get("highway") == "footway"
            and tags.get("covered") in ("colonnade", "arcade", "yes")):
        return "plaza"

    h = tags.get("highway")
    if h:
        if h in {"motorway", "trunk", "primary", "motorway_link", "trunk_link",
                 "primary_link"}:
            return "road_major"
        if h in {"secondary", "tertiary", "secondary_link", "tertiary_link"}:
            return "road_medium"
        if h in {"residential", "unclassified", "living_street"}:
            return "road_minor"
        # A pedestrian zone is a square people stand in, not a lane. Lumped
        # in with service alleys it was painted three and a half metres wide,
        # so Madrid's Puerta del Sol - mapped as a mesh of pedestrian ways and
        # no polygon at all - came out as a few paved stripes on grass.
        if h == "pedestrian":
            return "pedestrian"
        # Alleys, driveways and back lanes. Lumping these in with residential
        # streets paved every yard and car park aisle at full street width.
        if h == "service":
            return "road_service"
        # A farm track is a road, not a footpath. A paved one is a narrow
        # street; anything else is laid with the track tiles, unless the
        # mapper said the surface is dirt.
        if h == "track":
            surface = (tags.get("surface") or "").lower()
            if surface in PAVED_SURFACES:
                return "road_service"
            return "road_track"
        if h in {"path", "footway", "cycleway", "bridleway", "steps"}:
            # A city's pavements are mapped as footways alongside each street,
            # and painting them as dirt put a brown strip down every kerb in
            # Paris. Footways, cycleways and steps are paved unless the mapper
            # says otherwise; paths and bridleways are dirt unless they say paved.
            surface = (tags.get("surface") or "").lower()
            if surface in PAVED_SURFACES:
                return "paved_path"
            if surface in UNPAVED_SURFACES:
                return "dirt_path"
            if h in {"footway", "cycleway", "steps"}:
                return "paved_path"
            return "dirt_path"
        return "road_minor"

    if leisure == "swimming_pool" and tags.get("indoor") not in ("yes", "covered"):
        return "pool"
    if tags.get("natural") == "coastline":
        return "coastline"
    if tags.get("man_made") in {"pier", "breakwater", "groyne"}:
        return "pier"
    if tags.get("railway") in RAIL_TRACKS:
        return "railway"
    # Before the land-cover tags. An aerodrome is often also grass, and a
    # platform often sits inside a park; the aeroway or railway tag is the
    # ground that was mapped. A military airfield stays a base.
    if tags.get("aeroway") in AIRPORT_VALUES and not military:
        return "airport"
    if is_rail_area(tags):
        return "railway"
    if tags.get("natural") == "water" or tags.get("waterway") in {
            "river", "riverbank", "canal", "stream"}:
        return "water"
    if landuse in {"reservoir", "basin"}:
        return "water"
    if landuse in {"forest"} or tags.get("natural") == "wood":
        return "forest"
    if tags.get("natural") in {"scrub", "heath"}:
        return "scrub"
    if tags.get("natural") == "wetland":
        return "wetland"
    if tags.get("natural") == "tree":
        return "tree_single"

    if leisure == "playground":
        return "playground"
    if leisure == "track":
        return "track"
    if leisure in {"sports_centre", "stadium"}:
        return "sports"
    if leisure == "park":
        return "park"
    if leisure in {"garden", "pitch"}:
        return "grass"
    if landuse in {"grass", "meadow", "recreation_ground"} \
            or tags.get("natural") == "grassland":
        return "grass"
    if landuse in {"farmland", "farmyard", "allotments"}:
        return "farmland"
    if landuse in {"orchard", "vineyard"}:
        return "orchard"
    if landuse == "cemetery" or amenity == "grave_yard":
        return "cemetery"
    if tags.get("natural") in {"beach", "sand"}:
        return "sand"
    if landuse in {"brownfield", "construction", "quarry"}:
        return "dirt"

    if amenity in SCHOOL_AMENITIES:
        return "schoolyard"
    if amenity in {"hospital", "clinic"}:
        return "hospital_grounds"
    if amenity == "marketplace" or landuse in {"commercial", "retail"}:
        return "commercial"
    if landuse == "industrial":
        return "industrial"
    # A base is as often drawn with military=* alone - airfield, barracks,
    # range, training_area, naval_base - as with landuse=military.
    if military:
        return "military"
    if landuse == "residential":
        return "residential"
    if amenity == "place_of_worship":
        return "worship_grounds"
    # A barrier only counts as one when the outline is nothing else. Here a
    # school's grounds, a barracks or a government compound is commonly one
    # closed way tagged amenity=school and barrier=fence together; checking
    # the barrier first lost every one of those areas to "just a fence". The
    # renderer takes the fence off any outline carrying one, whatever it is.
    barrier = tags.get("barrier")
    if barrier in FENCE_BARRIERS:
        return "fence"
    if barrier == "hedge":
        return "hedge"
    return None
