"""Buildings from Overture Maps, where OpenStreetMap has none.

OpenStreetMap is drawn by people, so how much of a town is on it depends on
who lives there and whether anyone has traced it. In a German town it is all
of it; in plenty of the world it is the high street and not much else, and a
map generated from it comes out as a few streets of buildings with empty land
where the rest of the town is.

Overture Maps publishes a buildings theme that is OpenStreetMap first and
machine-detected roofprints (Microsoft's and Google's) second, under the same
ODbL licence. Where OSM has the building, the two agree and this adds
nothing; where OSM is blank and somebody's model found a roof, it fills in.
Measured on two towns, the same size of box:

    Gifhorn, Germany   OSM 1,962 buildings   Overture adds    60  (3%)
    Urgup, Turkey      OSM   473 buildings   Overture adds   761 (62%)

The German ones are mostly sheds and garages, 38 m2 at the median. The
Turkish ones are houses, 87 m2 at the median, and they nearly treble the
town. So this is off by default and worth turning on exactly where a mapper
can see their town is half missing - which they can, from the preview.

`_houses_from_addresses` in the renderer already does a smaller version of
this from OSM's own address points, and keeps doing it: it catches the houses
somebody numbered but never drew. That was 22 houses in Gifhorn and 20 in
Urgup, so it is not an alternative to this, and the two do not collide -
addresses are filled in after, and only where nothing stands yet.

The data is GeoParquet on S3 and there is no HTTP API for a bounding box, so
this needs DuckDB, which is not a small install and is therefore optional.
Without it everything else works and the setting says why. The buildings
theme is hundreds of files. A release's catalogue already records which file
covers which part of the world. OpenStreetMap is read first, and the query
asks only for the patches it left empty — not the whole map. That answer is
cached beside the map, and every piece is cut from the copy on disk.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
import threading
import time
import urllib.request

from .osm import OSMFeature

# The release this was written against. Overture publishes monthly and keeps
# the old ones, so pinning means a map built today is the same map next year;
# KNOXMAP_OVERTURE_RELEASE moves it without a new KnoxMap.
RELEASE = "2026-08-19.0"
REGION = "us-west-2"
BUCKET = "overturemaps-us-west-2"

# Overture's ids are strings and OSM's are numbers, so a building from here
# gets a number of its own that no OSM way can have.
FIRST_ID = -1_000_000_000

# Smaller than this is a sliver, a bin store or an artefact of whatever traced
# it, not something worth standing on a map. The generator turns anything up
# to 30 m2 into a shed already; this is only about what is not a building at
# all. In Urgup 71 of the 761 new ones were under 20 m2.
MIN_AREA_M2 = 10.0

# How finely the map is divided when looking for ground OpenStreetMap did not
# put a building on. A cell this wide with a roof in it is left alone.
GAP_CELL_M = 100.0

# How long to let one query run before giving up on it.
TIMEOUT_S = 15 * 60

# The release catalogue lists each building file's box. Reading that is one
# small file, and it is what keeps the query off the other five hundred.
STAC = "https://stac.overturemaps.org"
_HEADERS = {"User-Agent": "KnoxMap/1.0 (+https://github.com/CalvyPZ/knoxmap) local map generator"}


def available() -> bool:
    """Whether this PC can fetch from Overture at all."""
    try:
        import duckdb  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return False
    return True


def why_unavailable() -> str:
    """What to tell somebody who asked for it and has not got it."""
    if getattr(sys, "frozen", False):
        return ("Filling gaps from Overture Maps needs DuckDB, and this copy "
                "of KnoxMap was built without it. Everything else works "
                "without it.")
    return ("Filling gaps from Overture Maps needs DuckDB, which reads the "
            "map data where Overture publishes it. Install it with "
            "'.venv/bin/python -m pip install duckdb' (or "
            '".venv\\Scripts\\python -m pip install duckdb" on Windows) and '
            "start KnoxMap again. Everything else works without it.")


def _extension_dir() -> str:
    """Where DuckDB may download httpfs and spatial.

    A packaged program cannot write beside its own file. The map folder's
    cache can, and a later generate reuses the extensions it already fetched.
    """
    home = os.environ.get("KNOXMAP_HOME")
    if not home:
        home = os.path.dirname(os.path.abspath(__file__))
    path = os.path.abspath(os.path.join(home, "cache", "duckdb"))
    os.makedirs(path, exist_ok=True)
    return path.replace("\\", "/").replace("'", "''")


def release() -> str:
    return os.environ.get("KNOXMAP_OVERTURE_RELEASE") or RELEASE


def cache_path(output_dir: str, map_name: str) -> str:
    return os.path.join(output_dir, f"{map_name}_overture.json.gz")


def save_cache(path: str, bbox: tuple[float, float, float, float],
               rows: list[dict]) -> None:
    payload = {"release": release(), "bbox": list(bbox), "buildings": rows}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)


def load_cache(path: str,
               bbox: tuple[float, float, float, float]) -> list[dict] | None:
    """What was fetched for exactly this box and release, or None."""
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if payload.get("release") != release():
        return None
    kept = payload.get("bbox")
    if not (isinstance(kept, list) and len(kept) == 4
            and all(abs(a - b) < 1e-9 for a, b in zip(kept, bbox))):
        return None
    found = payload.get("buildings")
    return found if isinstance(found, list) else None


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.load(res)


def _intersects(box: list, south: float, west: float, north: float, east: float) -> bool:
    xmin, ymin, xmax, ymax = (float(v) for v in box)
    return xmin <= east and xmax >= west and ymin <= north and ymax >= south


def parquet_urls(south: float, west: float, north: float, east: float) -> list[tuple[str, int]]:
    """The building files whose own box meets this one, and each file's size.

    None of the theme's other files are opened. The first box in the
    catalogue is the whole theme; the rest follow the part number.
    """
    rel = release()
    collection = _get_json(f"{STAC}/{rel}/buildings/building/collection.json")
    boxes = (collection.get("extent") or {}).get("spatial", {}).get("bbox") or []
    hits = [index for index, box in enumerate(boxes[1:])
            if isinstance(box, list) and len(box) == 4
            and _intersects(box, south, west, north, east)]
    urls = []
    for index in hits:
        ident = f"{index:05d}"
        item = _get_json(f"{STAC}/{rel}/buildings/building/{ident}/{ident}.json")
        aws = (item.get("assets") or {}).get("aws") or {}
        href = aws.get("href")
        if not isinstance(href, str) or not href:
            continue
        try:
            size = int(aws.get("file:size") or 0)
        except (TypeError, ValueError):
            size = 0
        urls.append((href, size))
    if hits and not urls:
        raise RuntimeError("Overture's catalogue listed files but not where they are.")
    return urls


def _latlon_bounds(ring) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of a ring of (lat, lon), or None."""
    if not isinstance(ring, list) or len(ring) < 3:
        return None
    first = ring[0]
    if not isinstance(first, (list, tuple)) or len(first) < 2 or isinstance(first[0], (list, tuple)):
        return None
    south = north = float(first[0])
    west = east = float(first[1])
    for point in ring:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        lat, lon = float(point[0]), float(point[1])
        south = min(south, lat)
        north = max(north, lat)
        west = min(west, lon)
        east = max(east, lon)
    return south, west, north, east


def osm_building_boxes(features) -> list[tuple[float, float, float, float]]:
    """The outline of every OpenStreetMap building, as south, west, north, east."""
    boxes = []
    for feature in features:
        if not (feature.tags.get("building") or "").strip():
            continue
        rings = ([feature.geometry] if feature.kind == "way"
                 else [ring for ring in (feature.geometry or []) if isinstance(ring, list)])
        for ring in rings:
            bounds = _latlon_bounds(ring)
            if bounds is not None:
                boxes.append(bounds)
    return boxes


def gap_rects(buildings: list[tuple[float, float, float, float]],
              bbox: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """Rectangles inside the map where no OpenStreetMap building stands.

    A cell that a mapped roof touches is left out. The empty cells are joined
    into as few boxes as they will go, and those boxes are the query.
    """
    south, west, north, east = bbox
    if north <= south or east <= west:
        return []
    lat0 = (south + north) / 2.0
    metres_lat = 111320.0
    metres_lon = max(1.0, 111320.0 * math.cos(math.radians(lat0)))
    cell = GAP_CELL_M
    rows = max(1, math.ceil((north - south) * metres_lat / cell))
    cols = max(1, math.ceil((east - west) * metres_lon / cell))
    while rows * cols > 2_000_000:
        cell *= 2
        rows = max(1, math.ceil((north - south) * metres_lat / cell))
        cols = max(1, math.ceil((east - west) * metres_lon / cell))
    covered = bytearray(rows * cols)

    def mark(box) -> None:
        bs, bw, bn, be = box
        if bn < south or bs > north or be < west or bw > east:
            return
        c0 = max(0, min(cols - 1, int((min(bw, be) - west) / (east - west) * cols)))
        c1 = max(0, min(cols - 1, int((max(bw, be) - west) / (east - west) * cols)))
        r0 = max(0, min(rows - 1, int((min(bs, bn) - south) / (north - south) * rows)))
        r1 = max(0, min(rows - 1, int((max(bs, bn) - south) / (north - south) * rows)))
        for row in range(r0, r1 + 1):
            base = row * cols
            for col in range(c0, c1 + 1):
                covered[base + col] = 1

    for box in buildings:
        mark(box)
    if not any(covered):
        return [bbox]
    if all(covered):
        return []

    used = bytearray(rows * cols)
    rects = []

    def cell_box(c0: int, r0: int, c1: int, r1: int):
        return (
            south + (north - south) * r0 / rows,
            west + (east - west) * c0 / cols,
            south + (north - south) * r1 / rows,
            west + (east - west) * c1 / cols,
        )

    for row in range(rows):
        col = 0
        while col < cols:
            if covered[row * cols + col] or used[row * cols + col]:
                col += 1
                continue
            end = col + 1
            while end < cols and not covered[row * cols + end] and not used[row * cols + end]:
                end += 1
            bottom = row + 1
            while bottom < rows and all(
                    not covered[bottom * cols + at] and not used[bottom * cols + at]
                    for at in range(col, end)):
                bottom += 1
            for rr in range(row, bottom):
                base = rr * cols
                for cc in range(col, end):
                    used[base + cc] = 1
            rects.append(cell_box(col, row, end, bottom))
            col = end
    return rects


def _query(boxes: list[tuple[float, float, float, float]],
           urls: list[str] | None = None) -> str:
    # Overlapping, not contained: a building on the edge of a gap is half
    # inside it and has to be fetched, or the gap comes back with a bite out.
    if urls:
        listed = ", ".join("'" + url.replace("'", "''") + "'" for url in urls)
        source = f"[{listed}]"
    else:
        source = (f"'s3://{BUCKET}/release/{release()}"
                  f"/theme=buildings/type=building/*'")
    where = " OR ".join(
        f"(bbox.xmin <= {east} AND bbox.xmax >= {west}"
        f" AND bbox.ymin <= {north} AND bbox.ymax >= {south})"
        for south, west, north, east in boxes
    )
    return f"""
        SELECT id, height, num_floors, class, ST_AsGeoJSON(geometry) AS gj
        FROM read_parquet({source}, hive_partitioning=false)
        WHERE {where}
    """


# Several pieces install the same DuckDB extensions at once. The queries
# themselves run together; only the install is one at a time.
_SETUP = threading.Lock()


def _union(boxes: list[tuple[float, float, float, float]]):
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _row_group_span(con, url: str) -> list[tuple[float, float, float, float, int]]:
    """Each row group's box and compressed size, from the file footer alone."""
    table = con.execute(
        """
        SELECT row_group_id, row_group_bytes, path_in_schema, stats_min, stats_max
        FROM parquet_metadata(?)
        WHERE path_in_schema LIKE '%xmin' OR path_in_schema LIKE '%xmax'
           OR path_in_schema LIKE '%ymin' OR path_in_schema LIKE '%ymax'
        """,
        [url],
    ).fetchall()
    groups: dict = {}
    for row_group, nbytes, path, raw_min, raw_max in table:
        found = groups.setdefault(row_group, {"bytes": int(nbytes or 0)})
        name = str(path).rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        try:
            low, high = float(raw_min), float(raw_max)
        except (TypeError, ValueError):
            continue
        if name == "xmin":
            found["west"] = low
        elif name == "xmax":
            found["east"] = high
        elif name == "ymin":
            found["south"] = low
        elif name == "ymax":
            found["north"] = high
    spans = []
    for found in groups.values():
        if not all(key in found for key in ("south", "west", "north", "east")):
            continue
        spans.append((found["south"], found["west"], found["north"], found["east"],
                      found["bytes"]))
    return spans


def _download_bytes(con, files: list[tuple[str, int]] | None,
                    gaps: list[tuple[float, float, float, float]]) -> int:
    """Bytes the query will read: the row groups that meet a gap.

    The footer is enough for that. When it cannot be read, the whole file
    is the figure, which is larger than the query but still a size.
    """
    if not files:
        return 0
    total = 0
    for url, file_size in files:
        try:
            spans = _row_group_span(con, url)
        except Exception:  # noqa: BLE001 - the file size still answers
            spans = []
        if not spans:
            total += max(0, file_size)
            continue
        for south, west, north, east, nbytes in spans:
            if any(_intersects([west, south, east, north], *gap) for gap in gaps):
                total += nbytes
    return total


def fetch(south: float, west: float, north: float, east: float,
          should_stop=None, gaps=None, progress=None) -> list[dict]:
    """Overture buildings in the gaps, or in this box when no gaps are given.

    Runs on a thread of its own so the window's Stop button still works.
    `progress(done, total, speed)` is bytes of the query, so the window can
    show the size and how long is left.
    """
    import duckdb

    boxes = list(gaps) if gaps else [(south, west, north, east)]
    if not boxes:
        return []
    cover = _union(boxes)

    # The catalogue says which files meet the gaps. If it cannot be read, the
    # query still runs, over the whole theme, which is the slow way.
    files = None
    try:
        files = parquet_urls(*cover)
    except Exception as exc:  # noqa: BLE001 - the glob still answers
        try:
            import knoxlog
            knoxlog.log.warning("overture: file list unavailable (%s)", exc)
        except Exception:  # noqa: BLE001
            pass
    if files is not None and not files:
        return []
    urls = [url for url, _size in files] if files else None
    if urls:
        try:
            import knoxlog
            knoxlog.log.info("overture: %d building file%s, %d gap%s",
                             len(urls), "" if len(urls) == 1 else "s",
                             len(boxes), "" if len(boxes) == 1 else "s")
        except Exception:  # noqa: BLE001
            pass

    con = duckdb.connect()
    with _SETUP:
        con.execute(f"SET extension_directory='{_extension_dir()}';")
        con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
        con.execute(f"SET s3_region='{REGION}';")

    total = _download_bytes(con, files, boxes)
    if progress and total > 0:
        progress(0, total, 0.0)

    # A few hundred gaps are one query. More than that is split so the
    # statement stays a size DuckDB will plan, and the byte total still
    # covers every piece of it.
    chunk = 400
    parts = [boxes[i:i + chunk] for i in range(0, len(boxes), chunk)] or [boxes]
    out: dict = {"rows": []}

    def run(part, index: int) -> None:
        try:
            found = con.execute(_query(part, urls)).fetchall()
            out["rows"].extend(found)
            out["index"] = index
        except Exception as exc:  # noqa: BLE001 - reported on the main thread
            out["error"] = exc

    started = time.time()
    for index, part in enumerate(parts):
        out.pop("error", None)
        worker = threading.Thread(target=run, args=(part, index), daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.4)
            if progress and total > 0:
                try:
                    pct = float(con.query_progress() or 0.0)
                except Exception:  # noqa: BLE001 - the query still runs
                    pct = 0.0
                done = total * (index + max(0.0, min(pct, 100.0)) / 100.0) / len(parts)
                elapsed = time.time() - started
                speed = done / elapsed if elapsed >= 0.5 and done > 0 else 0.0
                progress(done, total, speed)
            if time.time() - started > TIMEOUT_S:
                con.interrupt()
                worker.join(30)
                raise TimeoutError(
                    f"Overture did not answer within {TIMEOUT_S // 60} minutes.")
            if should_stop is not None and should_stop():
                con.interrupt()
                worker.join(30)
                import knoxstop
                raise knoxstop.Stopped("the Overture download")
        if "error" in out:
            raise RuntimeError(f"Overture query failed: {out['error']}")
    if progress and total > 0:
        elapsed = max(0.5, time.time() - started)
        progress(total, total, total / elapsed)

    rows = []
    seen = set()
    for ident, height, floors, cls, gj in out.get("rows", []):
        if ident in seen:
            continue
        seen.add(ident)
        try:
            shape = json.loads(gj)
        except (TypeError, ValueError):
            continue
        rows.append({"id": ident, "height": height, "levels": floors,
                     "class": cls, "geometry": shape})
    return rows


def _rings(shape: dict) -> list[list[tuple[float, float]]]:
    """The outer ring of a polygon, or of each part of a multipolygon, as
    (lat, lon) the way an OSM way carries it."""
    kind = shape.get("type")
    if kind == "Polygon":
        parts = [shape.get("coordinates") or []]
    elif kind == "MultiPolygon":
        parts = shape.get("coordinates") or []
    else:
        return []
    out = []
    for part in parts:
        if not part:
            continue
        ring = [(float(lat), float(lon)) for lon, lat in part[0]
                if lon is not None and lat is not None]
        if len(ring) >= 3:
            out.append(ring)
    return out


def _area_m2(ring: list[tuple[float, float]]) -> float:
    """The shoelace area of a small ring of (lat, lon), in square metres."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    mx = 111320.0 * math.cos(math.radians(lat0))
    xy = [(lon * mx, lat * 111320.0) for lat, lon in ring]
    total = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def to_features(rows: list[dict]) -> list[OSMFeature]:
    """Overture buildings as the features the rest of KnoxMap already reads.

    `class` carries OpenStreetMap's own building values - house, apartments,
    barn, church, detached - so it goes straight into the building tag and
    everything downstream classifies it exactly as it would a mapped one.
    The machine-detected ones have no class at all, and arrive as a plain
    building=yes: a footprint, which is what they are.
    """
    feats = []
    ident = FIRST_ID
    for row in rows:
        for ring in _rings(row.get("geometry") or {}):
            if _area_m2(ring) < MIN_AREA_M2:
                continue
            tags = {"building": (row.get("class") or "yes")}
            levels = row.get("levels")
            if isinstance(levels, (int, float)) and 0 < levels < 100:
                tags["building:levels"] = str(int(levels))
            height = row.get("height")
            if isinstance(height, (int, float)) and 0 < height < 500:
                tags["height"] = f"{float(height):g}"
            feats.append(OSMFeature(osm_id=ident, kind="way", tags=tags,
                                    geometry=ring))
            ident -= 1
    return feats


def _lonlat_ring(ring) -> list[tuple[float, float]] | None:
    """A ring of (lat, lon) as (lon, lat), or None when it cannot be a polygon."""
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        return None
    xy = []
    for pair in ring:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            return None
        lat, lon = pair[0], pair[1]
        if isinstance(lat, bool) or isinstance(lon, bool):
            return None
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            return None
        xy.append((float(lon), float(lat)))
    return xy if len(xy) >= 3 else None


def _stacked_polygons(rings: list[list[tuple[float, float]]]):
    """Every ring as a polygon in one GEOS call."""
    import numpy as np
    import shapely

    arrays = [np.asarray(ring, dtype=np.float64) for ring in rings]
    counts = np.fromiter((len(ring) for ring in arrays), dtype=np.int64, count=len(arrays))
    xy = np.concatenate(arrays, axis=0)
    indices = np.repeat(np.arange(len(arrays), dtype=np.int64), counts)
    return shapely.polygons(shapely.linearrings(xy, indices=indices))


def only_missing(candidates: list[OSMFeature],
                 features: list[OSMFeature]) -> list[OSMFeature]:
    """The candidates that do not stand where a building already stands.

    A centre inside an existing outline is the test, not an overlap: Overture
    and OSM trace the same building slightly differently, and anything that
    asks how much two outlines share has to be told how much counts. A roof
    whose middle is inside a mapped building is that building.
    """
    try:
        return _only_missing_batch(candidates, features)
    except Exception as exc:  # noqa: BLE001 - one bad ring falls back to the single test
        import knoxstop
        if isinstance(exc, knoxstop.Stopped):
            raise
        import knoxlog
        knoxlog.log.warning("overture overlap fell back to one roof at a time: %s", exc)
        return _only_missing_each(candidates, features)


def _only_missing_batch(candidates: list[OSMFeature],
                        features: list[OSMFeature]) -> list[OSMFeature]:
    import numpy as np
    import shapely
    from shapely.strtree import STRtree

    rings = []
    for feat in features:
        if not (feat.tags.get("building") or "").strip():
            continue
        raw = ([feat.geometry] if feat.kind == "way"
               else [ring for ring in (feat.geometry or []) if isinstance(ring, list)])
        for ring in raw:
            xy = _lonlat_ring(ring)
            if xy is not None:
                rings.append(xy)
    if not rings:
        return list(candidates)
    existing = _stacked_polygons(rings)
    bad = ~np.asarray(shapely.is_valid(existing), dtype=bool)
    if bad.any():
        existing = np.array(existing, dtype=object, copy=True)
        existing[bad] = shapely.buffer(existing[bad], 0)
    keep = (~np.asarray(shapely.is_empty(existing), dtype=bool)
            & (np.asarray(shapely.area(existing), dtype=np.float64) > 0))
    existing = existing[keep]
    if len(existing) == 0:
        return list(candidates)
    tree = STRtree(existing)

    cand_rings = []
    cand_index = []
    for index, cand in enumerate(candidates):
        xy = _lonlat_ring(cand.geometry)
        if xy is None:
            continue
        cand_rings.append(xy)
        cand_index.append(index)
    if not cand_rings:
        return []
    here = _stacked_polygons(cand_rings)
    bad = ~np.asarray(shapely.is_valid(here), dtype=bool)
    if bad.any():
        here = np.array(here, dtype=object, copy=True)
        here[bad] = shapely.buffer(here[bad], 0)
    middle = shapely.centroid(here)
    empty = np.asarray(shapely.is_empty(middle), dtype=bool)
    blocked = empty.copy()
    alive = np.flatnonzero(~empty)
    if len(alive):
        hits = np.asarray(tree.query(np.asarray(middle[alive], dtype=object),
                                      predicate="within"))
        if hits.size:
            if hits.ndim == 1:
                if len(alive) == 1:
                    blocked[alive[0]] = True
            else:
                blocked[alive[hits[0]]] = True
    drop = {cand_index[int(i)] for i in np.flatnonzero(blocked)}
    usable = set(cand_index)
    return [cand for index, cand in enumerate(candidates)
            if index in usable and index not in drop]


def _only_missing_each(candidates: list[OSMFeature],
                       features: list[OSMFeature]) -> list[OSMFeature]:
    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    existing = []
    for feat in features:
        if not (feat.tags.get("building") or "").strip():
            continue
        rings = ([feat.geometry] if feat.kind == "way"
                 else [ring for ring in (feat.geometry or []) if isinstance(ring, list)])
        for ring in rings:
            if not ring or len(ring) < 3:
                continue
            try:
                poly = Polygon([(lon, lat) for lat, lon in ring])
                if not poly.is_valid:
                    poly = poly.buffer(0)
            except Exception:  # noqa: BLE001 - a ring shapely rejects is no test
                continue
            if not poly.is_empty and poly.area > 0:
                existing.append(poly)
    if not existing:
        return list(candidates)

    tree = STRtree(existing)
    kept = []
    for cand in candidates:
        try:
            here = Polygon([(lon, lat) for lat, lon in cand.geometry])
            if not here.is_valid:
                here = here.buffer(0)
            middle = here.centroid
        except Exception:  # noqa: BLE001
            continue
        if middle.is_empty:
            continue
        if any(existing[int(i)].contains(middle) for i in tree.query(middle)):
            continue
        kept.append(cand)
    return kept


def _shape_bounds(shape: dict) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) of a GeoJSON polygon, or None."""
    kind = shape.get("type")
    raw = shape.get("coordinates") or []
    if kind == "Polygon":
        rings = raw
    elif kind == "MultiPolygon":
        rings = [ring for poly in raw if isinstance(poly, list) for ring in poly]
    else:
        return None
    south = west = math.inf
    north = east = -math.inf
    for ring in rings:
        if not isinstance(ring, list):
            continue
        for pair in ring:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            lon, lat = float(pair[0]), float(pair[1])
            south = min(south, lat)
            north = max(north, lat)
            west = min(west, lon)
            east = max(east, lon)
    if south is math.inf:
        return None
    return south, west, north, east


def rows_touching(rows: list[dict],
                  bbox: tuple[float, float, float, float]) -> list[dict]:
    """The fetched buildings whose outline meets this box.

    The same overlap the query uses: a roof on the line between two pieces
    belongs to both, or the map has a gap along that line.
    """
    south, west, north, east = bbox
    kept = []
    for row in rows:
        bounds = _shape_bounds(row.get("geometry") or {})
        if bounds is None:
            continue
        rs, rw, rn, re = bounds
        if rn >= south and rs <= north and re >= west and rw <= east:
            kept.append(row)
    return kept


def prepare(bbox: tuple[float, float, float, float],
            output_dir: str, map_name: str,
            should_stop=None, gaps=None, progress=None) -> tuple[list[dict], dict]:
    """Overture buildings for the gaps in this box, from the copy beside the map.

    One query for the places OpenStreetMap left empty. A later piece, and a
    later generate of the same box, reads the file and does not ask again.
    """
    stats = {"available": available(), "fetched": 0, "added": 0, "cached": False}
    if gaps is not None and not gaps:
        return [], stats
    path = cache_path(output_dir, map_name)
    rows = load_cache(path, bbox)
    if rows is not None:
        stats["cached"] = True
        stats["fetched"] = len(rows)
        return rows, stats
    if not stats["available"]:
        stats["why"] = why_unavailable()
        return [], stats
    south, west, north, east = bbox
    rows = fetch(south, west, north, east, should_stop=should_stop,
                 gaps=gaps, progress=progress)
    try:
        save_cache(path, bbox, rows)
    except OSError:
        pass
    stats["fetched"] = len(rows)
    return rows, stats


def load_covering(path: str,
                  bbox: tuple[float, float, float, float]) -> list[dict] | None:
    """A cached fetch whose box contains this one, or None."""
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if payload.get("release") != release():
        return None
    kept = payload.get("bbox")
    if not (isinstance(kept, list) and len(kept) == 4):
        return None
    try:
        cover = tuple(float(v) for v in kept)
    except (TypeError, ValueError):
        return None
    south, west, north, east = bbox
    if not (cover[0] <= south and cover[1] <= west
            and cover[2] >= north and cover[3] >= east):
        return None
    found = payload.get("buildings")
    return found if isinstance(found, list) else None


def fill_from(features: list[OSMFeature],
              rows: list[dict]) -> tuple[list[OSMFeature], dict]:
    """`features`, plus the buildings in `rows` that OSM does not already have."""
    extra = only_missing(to_features(rows), features)
    stats = {"available": True, "fetched": len(rows), "added": len(extra), "cached": True}
    return (features + extra) if extra else features, stats


def add_missing(features: list[OSMFeature],
                bbox: tuple[float, float, float, float],
                output_dir: str, map_name: str,
                should_stop=None, gaps=None, progress=None) -> tuple[list[OSMFeature], dict]:
    """`features`, plus a building wherever Overture has one and OSM does not.

    Returns the new list and what happened, for the window to report. Never
    raises for want of Overture: a map that cannot reach it is the map OSM
    alone makes, which is the map every earlier KnoxMap made.
    """
    south, west, north, east = bbox
    stats = {"available": available(), "fetched": 0, "added": 0, "cached": False}

    # The cache first, and only then DuckDB. Fetching is the one thing that
    # needs it: a map already fetched re-renders on any PC, and a map folder
    # handed to somebody else carries its buildings with it.
    path = cache_path(output_dir, map_name)
    rows = load_cache(path, bbox)
    if rows is not None:
        stats["cached"] = True
    else:
        if not stats["available"]:
            stats["why"] = why_unavailable()
            return features, stats
        if gaps is None:
            gaps = gap_rects(osm_building_boxes(features), bbox)
        if not gaps:
            return features, stats
        rows = fetch(south, west, north, east, should_stop=should_stop,
                     gaps=gaps, progress=progress)
        try:
            save_cache(path, bbox, rows)
        except OSError:
            pass          # a fetch that cannot be cached is still a fetch
    stats["fetched"] = len(rows)

    extra = only_missing(to_features(rows), features)
    stats["added"] = len(extra)
    return (features + extra) if extra else features, stats
