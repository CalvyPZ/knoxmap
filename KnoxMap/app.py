"""Knoxify — Flask entry point.

Real-world areas → Project Zomboid maps.

Run:
    source .venv/bin/activate
    python app.py

Then open http://127.0.0.1:5000/ in a browser.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

from flask import (Flask, jsonify, render_template, request, send_file,
                   send_from_directory)

import knoxlog
import knoxstop
from generator import osm, places, renderer
from knoxbuild import mapstate
from knoxbuild.settings import PRESETS, Settings

# Builds print place names in any script; a console on a legacy code page
# would raise mid-request on the first one it cannot show.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__, template_folder="templates", static_folder="static")

# Only this PC may talk to the app. The server listens on 127.0.0.1, but a web
# page in any browser on the PC can still point a hostname of its own at that
# address ("DNS rebinding") and call the API as if it were the app's own page.
# Such a request carries the attacker's hostname, so anything not addressed to
# localhost is refused before it reaches a route.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


log = knoxlog.setup("app")


def _request_context() -> dict:
    """What the page asked for, for the log: the map, the area and the
    settings, never more than a few hundred characters."""
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        return {}
    keep = ("mapName", "south", "west", "north", "east", "metersPerTile", "title", "modId")
    ctx = {k: body[k] for k in keep if k in body}
    if isinstance(body.get("settings"), dict):
        ctx["settings"] = body["settings"]
    if body.get("shape"):
        ctx["shape"] = "drawn"
    return ctx


def failed(message: str, status: int = 500, exc: BaseException | None = None):
    """An error reply the page shows, logged with an id the user can quote."""
    eid = knoxlog.record(exc, f"{request.method} {request.path} -> {status}: {message}",
                         **_request_context())
    return jsonify({"error": message, "errorId": eid}), status


@app.errorhandler(Exception)
def _api_error(exc):
    """Every failure as JSON the page can show, with the details in the log.

    Flask's default answer to an exception is an HTML page. The page reads
    every reply as JSON, so a crash anywhere in a request showed only
    "Unexpected token '<', "<!doctype"... is not valid JSON", which says
    nothing about what went wrong or where.
    """
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        if not request.path.startswith("/api/"):
            return exc
        return jsonify({"error": f"{exc.code} {exc.name}"}), exc.code
    return failed(f"{type(exc).__name__}: {exc}", 500, exc)


@app.after_request
def _log_refusals(response):
    """Every error the API answers with goes in the log, not just crashes: a
    "Missing or invalid bbox" is as much a clue as a traceback."""
    if (request.path.startswith("/api/") and response.status_code >= 400
            and response.is_json):
        data = response.get_json(silent=True) or {}
        if data.get("stopped"):
            return response          # asked for; already in the log as a stop
        if not data.get("errorId"):
            eid = knoxlog.record(None, f"{request.method} {request.path} -> "
                                       f"{response.status_code}: {data.get('error')}",
                                 **_request_context())
            data["errorId"] = eid
            response.set_data(json.dumps(data))
    return response


@app.route("/api/client-error", methods=["POST"])
def api_client_error():
    """Errors in the page itself, sent by the script in static/js/app.js."""
    data = request.get_json(silent=True) or {}
    eid = knoxlog.error_id()
    log.error("%s page error: %s\n  at %s\n%s", eid,
              str(data.get("message", ""))[:500], str(data.get("where", ""))[:300],
              str(data.get("stack", ""))[:4000])
    return jsonify({"errorId": eid})


@app.route("/api/report")
def api_report():
    """A zip of the logs and a description of the PC, for #bug-reports."""
    log.info("problem report downloaded")
    stamp = time.strftime("%Y%m%d-%H%M")
    return send_file(io.BytesIO(knoxlog.report_zip(OUTPUT_DIR)), mimetype="application/zip",
                     as_attachment=True, download_name=f"KnoxMap-report-{stamp}.zip")


@app.route("/api/report-save", methods=["POST"])
def api_report_save():
    """The same zip, saved into logs/ and shown in Explorer: the app window is
    not a browser, and has nowhere to put a download."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = knoxlog.LOG_DIR / f"KnoxMap-report-{stamp}.zip"
    knoxlog.LOG_DIR.mkdir(exist_ok=True)
    path.write_bytes(knoxlog.report_zip(OUTPUT_DIR))
    log.info("problem report saved: %s", path.name)
    knoxlog.open_folder(path)
    return jsonify({"name": path.name, "path": str(path)})


@app.route("/api/update")
def api_update():
    """Whether a newer KnoxMap is downloading or ready (updater.py)."""
    import updater
    return jsonify(updater.status())


@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    import updater
    threading.Thread(target=updater.check, name="update-check", daemon=True).start()
    return jsonify(updater.status())


@app.route("/api/versions")
def api_versions():
    """The KnoxMap releases on GitHub, for the version menu."""
    import updater
    try:
        return jsonify(updater.releases())
    except Exception as exc:  # noqa: BLE001 - offline or rate-limited
        return failed(f"Could not reach GitHub for the list of versions: {exc}", 502, exc)


@app.route("/api/versions/install", methods=["POST"])
def api_versions_install():
    """Download the version chosen in the menu, to go in on restart."""
    import updater
    version = str((request.get_json(silent=True) or {}).get("version", "")).strip()
    if not re.fullmatch(r"\d+(\.\d+){0,3}", version):
        return failed("Choose a version from the list.", 400)
    if not updater.managed():
        return failed("This copy of KnoxMap is a git checkout: switch versions with git.", 400)
    if updater.status().get("state") in ("checking", "downloading"):
        return failed("A download is already running; try again in a moment.", 409)
    log.info("version %s chosen in the window (this is %s)", version, updater.current_version())
    threading.Thread(target=updater.choose, args=(version,), name="choose-version",
                     daemon=True).start()
    return jsonify({"started": True, "version": version})


@app.route("/api/update/restart", methods=["POST"])
def api_update_restart():
    import updater
    if os.environ.get("KNOXMAP_WINDOW") != "1":
        return failed("Close KnoxMap and open it again to update.", 400)
    if updater.status().get("state") != "ready":
        return failed("No update is ready yet.", 409)
    updater.restart()
    return jsonify({"restarting": True})


# ---- the window in another language ---------------------------------------------
#
# lang/english.txt lists every line the window says. Copy it to lang/<language>
# .txt, translate the right of each "=", and the language appears in the menu at
# the top - no code, no rebuild. tools/make_lang_template.py writes the English
# one from the page itself.

LANG_DIR = BASE_DIR / "lang"


def _language_strings(path: Path) -> dict:
    """The translated lines of one language file, English -> theirs."""
    out = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        english, _, theirs = line.partition("=")
        english, theirs = english.strip(), theirs.strip()
        # A line left as the English is simply not translated.
        if english and theirs and english != theirs:
            out[english] = theirs
    return out


@app.route("/api/languages")
def api_languages():
    files = sorted(LANG_DIR.glob("*.txt")) if LANG_DIR.is_dir() else []
    import knoxpaths
    return jsonify({
        "languages": [{"file": f.stem, "name": f.stem[:1].upper() + f.stem[1:],
                       "lines": len(_language_strings(f))} for f in files],
        "current": knoxpaths.load_config().get("language", "english"),
        "folder": str(LANG_DIR),
    })


@app.route("/api/language/<name>", methods=["GET", "POST"])
def api_language(name: str):
    """One language's lines, and - on POST - the one to open with next time."""
    import knoxpaths

    safe = SAFE_NAME.sub("_", name).strip("_").lower()
    path = (LANG_DIR / f"{safe}.txt")
    if safe != "english" and not path.is_file():
        return failed(f"No lang/{safe}.txt. Copy lang/english.txt and translate it.", 404)
    if request.method == "POST":
        config = knoxpaths.load_config()
        config["language"] = safe
        with open(knoxpaths.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        log.info("language set to %s", safe)
    return jsonify({"file": safe, "strings": _language_strings(path) if path.is_file() else {}})


@app.route("/api/open-logs", methods=["POST"])
def api_open_logs():
    return jsonify({"opened": knoxlog.open_folder(), "folder": str(knoxlog.LOG_DIR)})


@app.before_request
def _only_local():
    host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[")         else (request.host or "").split("]")[0] + "]"
    if host not in LOCAL_HOSTS:
        return ("KnoxMap only answers requests from this computer.", 403)


# Where a map stops being an easy one. None of these refuses anything: they
# are what the window warns about, and what the log records, so somebody who
# wants a whole city can have one and knows what they are in for.
#
# They used to be hard limits, and a limit that says no is worth having only
# when the thing behind it cannot be done. These can: the area cap was
# originally 20 km2 because that is about all one Overpass query will answer,
# and osm.fetch_features_tiled lifted that by splitting a big request into a
# grid of small ones. What is left is memory and patience, and both are the
# mapper's to spend.
#
# BIG_TILES_PER_SIDE is the memory one: the renderer holds a landscape and a
# vegetation image at full size, 3 bytes a tile each, so 9000 tiles a side is
# about 490 MB of pixels before anything else.
BIG_AREA_KM2 = 400.0
OVERPASS_TILE_KM2 = 30.0       # size of each sub-query; overshoot re-splits
BIG_TILES_PER_SIDE = 9000      # 30 cells at 300 tiles each
# A scale still has to be a scale: zero or a negative divides the world by
# nothing. The range the window offers is 0.5 to 8; outside it is allowed and
# said to be unusual.
MIN_METERS_PER_TILE = 0.01
MAX_METERS_PER_TILE = 100.0
USUAL_METERS_PER_TILE = (0.5, 8.0)

# Landmark lookup asks for far more tag keys than the terrain query, so it is
# the first thing to get slow.
BIG_LANDMARK_KM2 = 40.0

SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")

# Source cells per side handed to one WorldEd process. 4x4 keeps its peak
# memory around a gigabyte; larger batches are faster per cell but climb.
COMPILE_BATCH = 4

# Big maps take minutes, so generation reports where it has got to and the page
# polls /api/progress. Keyed by map name; the generate call owns its entry.
_PROGRESS: dict[str, dict] = {}
_COMPILE: dict[str, dict] = {}
_PROGRESS_LOCK = threading.Lock()


def _set_progress(map_name: str, **fields) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.setdefault(map_name, {}).update(fields)


# Maps the window has asked to stop. A long job looks at this between the
# pieces of work it can be interrupted between (knoxstop.py); the name comes
# off the list when the job it stopped notices, so the next run is not
# stopped before it starts.
_STOPPING: set[str] = set()


def _stopper(map_name: str):
    """A callable the long jobs poll: True once the window has asked."""
    return lambda: map_name in _STOPPING


def _done_stopping(map_name: str) -> None:
    _STOPPING.discard(map_name)


def _stopped(map_name: str, step: str):
    """The reply for a job the window stopped: not an error, and the map's
    area and settings are untouched."""
    _done_stopping(map_name)
    log.info("stop %s: %s stopped", map_name, step)
    with _PROGRESS_LOCK:
        _PROGRESS.setdefault(map_name, {})["stage"] = "stopped"
    return jsonify({"stopped": True, "step": step, "mapName": map_name}), 409


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Stop making this map, and leave everything else as it is.

    The drawn area, the settings and whatever has already been written stay
    where they are: pressing the button again starts the same map over. Used
    to mean closing the window, which threw the drawn rectangle away with it.
    """
    name = str((_json_body() or {}).get("mapName", "")).strip()
    if not name:
        return failed("Which map?", 400)
    _STOPPING.add(name)
    log.info("stop %s: asked by the window", name)
    with _PROGRESS_LOCK:
        _PROGRESS.setdefault(name, {})["stage"] = "stopping"
        if _COMPILE.get(name, {}).get("state") == "running":
            _COMPILE[name] = {**_COMPILE[name], "state": "stopping"}
    return jsonify({"stopping": True, "mapName": name})


@app.route("/api/progress")
def api_progress():
    with _PROGRESS_LOCK:
        return jsonify(_PROGRESS.get(request.args.get("map", ""), {}))


@app.route("/")
def index():
    # The app window cannot download a file the way a browser does - clicking
    # a download link there did nothing at all - so the page asks the server
    # to save it and show it in Explorer instead. See /api/save.
    return render_template("index.html", version=knoxlog.version(),
                           in_window=os.environ.get("KNOXMAP_WINDOW") == "1")


# ---- map tiles, fetched the way the OSM tile policy asks -------------------------
#
# https://operations.osmfoundation.org/policies/tiles/ - an installed app must
# identify itself with its own User-Agent (never a browser's), honour the
# server's caching headers or keep tiles at least 7 days, and never pre-fetch
# or bulk-download. The page's map used to load tiles straight from
# tile.openstreetmap.org inside the app window, which sent WebView2's browser
# identity and cached nothing. Tiles now come through here: fetched only when
# the map shows them, with KnoxMap's User-Agent, kept on disk and revalidated.
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_CACHE = BASE_DIR / "cache" / "tiles"
TILE_MIN_AGE = 7 * 24 * 3600
# How many tiles are fetched at once. A map view is twenty or thirty tiles,
# and at two at a time they arrived in a slow ripple; a browser asking
# tile.openstreetmap.org directly opens six connections, so four through one
# proxy is no heavier than the page would be on its own. This is interactive
# browsing, not the bulk downloading the tile policy asks people not to do.
TILE_FETCHES = threading.BoundedSemaphore(4)
# One connection pool for all of them. Without it every tile paid for a new
# TCP connection and a TLS handshake - most of the wait, on a map of tiles
# that are only a few kilobytes each.
TILE_TIMEOUT = (5, 15)          # connect, read
_TILE_SESSION = None
_TILE_SESSION_LOCK = threading.Lock()
# Tiles being revalidated in the background, so a stale one is not fetched
# once per request while the first fetch is still running.
_TILE_REVALIDATING: set = set()


def _tile_session():
    global _TILE_SESSION
    with _TILE_SESSION_LOCK:
        if _TILE_SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter

            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
            session.mount("https://", adapter)
            session.headers["User-Agent"] = places.HEADERS["User-Agent"]
            _TILE_SESSION = session
    return _TILE_SESSION


def _fetch_tile(z: int, x: int, y: int, path, meta, info: dict) -> bool:
    """Fetch or revalidate one tile. True when it is on disk afterwards."""
    import requests

    headers = {}
    if path.exists() and info.get("etag"):
        headers["If-None-Match"] = info["etag"]
    try:
        with TILE_FETCHES:
            r = _tile_session().get(TILE_URL.format(z=z, x=x, y=y),
                                    headers=headers, timeout=TILE_TIMEOUT)
    except requests.RequestException:
        return path.exists()
    if r.status_code not in (200, 304):
        return path.exists()
    max_age = TILE_MIN_AGE
    for part in (r.headers.get("Cache-Control") or "").split(","):
        if part.strip().startswith("max-age="):
            try:
                max_age = max(TILE_MIN_AGE, int(part.split("=", 1)[1]))
            except ValueError:
                pass
    if r.status_code == 200:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        info["etag"] = r.headers.get("ETag")
    info["expires"] = time.time() + max_age
    try:
        meta.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass
    return path.exists()


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile(z: int, x: int, y: int):
    if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return ("", 404)
    path = TILE_CACHE / str(z) / str(x) / f"{y}.png"
    meta = path.with_suffix(".json")
    info = {}
    if path.exists() and meta.exists():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
    fresh = path.exists() and time.time() < info.get("expires", 0)
    if path.exists() and not fresh:
        # A tile we already have is served straight away and checked against
        # the server behind the page's back. Waiting on that round trip made
        # panning back over somewhere already visited as slow as the first
        # time, for a picture of a street that had not changed in a week.
        key = (z, x, y)
        if key not in _TILE_REVALIDATING:
            _TILE_REVALIDATING.add(key)

            def revalidate():
                try:
                    _fetch_tile(z, x, y, path, meta, info)
                finally:
                    _TILE_REVALIDATING.discard(key)

            threading.Thread(target=revalidate, name=f"tile-{z}-{x}-{y}",
                             daemon=True).start()
    elif not fresh and not _fetch_tile(z, x, y, path, meta, info):
        return ("", 502)
    resp = send_file(path, mimetype="image/png")
    resp.headers["Cache-Control"] = f"max-age={TILE_MIN_AGE}"
    return resp


# Nominatim asks that results are cached on the application's side and that
# the same query is not sent again and again
# (https://operations.osmfoundation.org/policies/nominatim/).
_SEARCH_CACHE: dict[tuple, tuple[float, list]] = {}
SEARCH_CACHE_SECONDS = 24 * 3600


@app.route("/api/search")
def api_search():
    """Find a place by name, so the map can jump to it."""
    q = request.args.get("q", "")
    if not q.strip():
        return jsonify({"results": []})

    viewbox = None
    try:
        viewbox = (float(request.args["south"]), float(request.args["west"]),
                   float(request.args["north"]), float(request.args["east"]))
    except (KeyError, ValueError):
        pass
    bounded = request.args.get("bounded") == "1"

    key = (q.strip().lower(), viewbox and tuple(round(v, 3) for v in viewbox), bounded)
    cached = _SEARCH_CACHE.get(key)
    if cached and time.time() - cached[0] < SEARCH_CACHE_SECONDS:
        return jsonify({"results": cached[1]})
    try:
        results = places.search(q, viewbox=viewbox, bounded=bounded)
    except Exception as exc:  # network, rate limit, bad JSON
        return jsonify({"error": f"Search failed: {exc}"}), 502
    if len(_SEARCH_CACHE) > 500:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[key] = (time.time(), results)
    return jsonify({"results": results})


@app.route("/api/landmarks", methods=["POST"])
def api_landmarks():
    """List the named landmarks inside a bbox."""
    data = _json_body()
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Need south, west, north and east."}), 400

    landmark_area = _bbox_area_km2(south, west, north, east)
    if landmark_area > BIG_LANDMARK_KM2:
        # Slow, not impossible. It used to be refused outright.
        log.info("landmarks: %.1f km2, over the %g km2 this gets slow at",
                 landmark_area, BIG_LANDMARK_KM2)

    try:
        found = places.landmarks(south, west, north, east)
    except Exception as exc:
        return jsonify({"error": f"Lookup failed: {exc}"}), 502
    return jsonify({"landmarks": found})


# Enough for a town boundary traced in detail; more is a mistake or an abuse.
MAX_SHAPE_POINTS = 20000


def _shape_rings(shape: dict) -> list:
    polys = shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]]
    return [ring for rings in polys for ring in rings]


def _clean_shape(raw) -> tuple[dict | None, str | None]:
    """A drawn selection as GeoJSON Polygon/MultiPolygon, checked and folded.

    The page sends one for a polygon, a circle, a freehand lasso or a place's
    real outline; a rectangle sends none. Longitudes are folded back into
    range the same way the bbox is, and the points are counted so a broken
    page cannot hand the renderer a million-vertex outline.
    """
    if not raw:
        return None, None
    try:
        kind = raw["type"]
        if kind not in ("Polygon", "MultiPolygon"):
            return None, "The selection must be a polygon."
        polys = raw["coordinates"] if kind == "MultiPolygon" else [raw["coordinates"]]
        cleaned, points = [], 0
        for rings in polys:
            out_rings = []
            for ring in rings:
                pts = [[_wrap_lon(float(lon)), float(lat)] for lon, lat in ring]
                points += len(pts)
                if len(pts) >= 3:
                    if pts[0] != pts[-1]:
                        pts.append(pts[0])
                    out_rings.append(pts)
            if out_rings:
                cleaned.append(out_rings)
    except (KeyError, TypeError, ValueError):
        return None, "The selection shape could not be read."
    if not cleaned:
        return None, "The selection shape has no area."
    if points > MAX_SHAPE_POINTS:
        return None, f"The selection outline has {points:,} points; the most is {MAX_SHAPE_POINTS:,}."
    if kind == "Polygon":
        return {"type": "Polygon", "coordinates": cleaned[0]}, None
    return {"type": "MultiPolygon", "coordinates": cleaned}, None


def _wrap_lon(lon: float) -> float:
    """Fold a longitude back into -180..180."""
    return (lon + 180.0) % 360.0 - 180.0


def _json_body() -> dict:
    """The request's JSON object, or {} for anything else - a bare number, a
    list, broken JSON. Endpoints then report what is missing instead of
    failing with a server error."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _normalise_bbox(south: float, west: float, north: float,
                    east: float) -> tuple[tuple[float, float, float, float],
                                          str | None]:
    """Clean a bbox from the map widget, or say why it cannot be used.

    Leaflet hands back *unwrapped* coordinates once the map has been panned
    across a world copy: drag east past the date line twice and a rectangle
    over Bergama arrives as longitude 747.17 rather than 27.17. It passes
    west < east and it passes the area check, because the span is still small -
    and then every Overpass server rejects it with "the only allowed values are
    floats between -180.0 and 180.0", which reaches the page as three identical
    walls of XHTML and no clue that panning caused it.
    """
    south = max(-85.05, min(85.05, south))
    north = max(-85.05, min(85.05, north))
    west, east = _wrap_lon(west), _wrap_lon(east)
    if not south < north:
        return (south, west, north, east), "BBox is degenerate."
    if west == east:
        return (south, west, north, east), "BBox is degenerate."
    if west > east:
        # Wrapping put the two edges either side of the date line. Splitting
        # the query would work but every map built here would then straddle
        # the seam, so say so rather than quietly building half of it.
        return ((south, west, north, east),
                "That selection crosses the 180th meridian. Draw it on one "
                "side or the other.")
    return (south, west, north, east), None


# Drawing a map holds several pictures of it at once - the ground, the
# vegetation, the masks the gardens and the paving are worked out from. Measured
# on real towns (a 2400 x 2400 Paris peaked at 0.54 GB, a 1200 x 900 Manhattan
# at 0.12 GB), that is about this much, plus what the program itself takes.
BYTES_PER_TILE = 80
BASE_BYTES = 150e6


def _too_big_for_memory(tiles_w: float, tiles_h: float) -> str | None:
    """Why this map will not fit in memory, or None when it should.

    A 32-bit Python can only use about 2 GB however much the PC has, and a map
    of a few square kilometres needs more: it used to get halfway through and
    fail with "MemoryError".
    """
    import knoxpaths
    needed = tiles_w * tiles_h * BYTES_PER_TILE + BASE_BYTES
    status = knoxlog.memory_status()
    if not status:
        return None
    _total, free, room = status
    smaller = "Pick a smaller area, or a larger scale (metres per tile)."
    if sys.maxsize <= 2 ** 32 and needed > room * 0.8:
        return (f"This map needs about {needed / 1e9:.1f} GB of memory, and KnoxMap is "
                f"running 32-bit Python, which can only use about 2 GB however much this "
                f"PC has. Close KnoxMap and run {knoxpaths.setup_command()} again: it "
                f"fetches a 64-bit "
                f"Python and makes its environment again. " + smaller)
    if needed > min(free, room) * 0.8:
        return (f"This map needs about {needed / 1e9:.1f} GB of memory and only "
                f"{min(free, room) / 1e9:.1f} GB is free. Close a few things and try "
                f"again. " + smaller)
    return None


@app.route("/api/generate", methods=["POST"])
def generate():
    data = _json_body()
    shape, problem = _clean_shape(data.get("shape"))
    if problem:
        return jsonify({"error": problem}), 400
    if shape:
        # A drawn shape decides the box: its own bounds, whatever the page sent.
        lons = [p[0] for ring in _shape_rings(shape) for p in ring]
        lats = [p[1] for ring in _shape_rings(shape) for p in ring]
        data = {**data, "south": min(lats), "north": max(lats),
                "west": min(lons), "east": max(lons)}
    try:
        south = float(data["south"])
        west = float(data["west"])
        north = float(data["north"])
        east = float(data["east"])
        meters_per_tile = float(data.get("metersPerTile", 1.0))
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Missing or invalid bbox / scale."}), 400

    import math
    if not all(math.isfinite(v) for v in (south, west, north, east, meters_per_tile)):
        return jsonify({"error": "Missing or invalid bbox / scale."}), 400
    (south, west, north, east), problem = _normalise_bbox(
        south, west, north, east)
    if problem:
        return jsonify({"error": problem}), 400
    if not (MIN_METERS_PER_TILE <= meters_per_tile <= MAX_METERS_PER_TILE):
        return jsonify({"error": f"Metres per tile has to be between "
                                 f"{MIN_METERS_PER_TILE:g} and "
                                 f"{MAX_METERS_PER_TILE:g}."}), 400

    # Nothing below refuses the map. These are what the window has already
    # warned about; they are logged here so a report from somebody whose map
    # died halfway says how big it was.
    heavy: list[str] = []
    area_km2 = _bbox_area_km2(south, west, north, east)
    if area_km2 > BIG_AREA_KM2:
        heavy.append(f"{area_km2:.0f} km2, over the {BIG_AREA_KM2:g} km2 a map "
                     f"usually is")
    if not (USUAL_METERS_PER_TILE[0] <= meters_per_tile <= USUAL_METERS_PER_TILE[1]):
        heavy.append(f"{meters_per_tile:g} m a tile, outside the "
                     f"{USUAL_METERS_PER_TILE[0]:g}-{USUAL_METERS_PER_TILE[1]:g} "
                     f"the window offers")

    raw_name = data.get("mapName") or f"knoxify_{int(time.time())}"
    map_name = SAFE_NAME.sub("_", raw_name).strip("_") or f"knoxify_{int(time.time())}"

    # Upper bound on the final bitmap size before we even hit Overpass.
    approx_w = ((east - west) * 111320 * _cos_lat((south + north) / 2)) / meters_per_tile
    approx_h = ((north - south) * 111320) / meters_per_tile
    if max(approx_w, approx_h) > BIG_TILES_PER_SIDE:
        heavy.append(f"{int(approx_w)}x{int(approx_h)} tiles, over the "
                     f"{BIG_TILES_PER_SIDE} a side that is comfortable")
    tight = _too_big_for_memory(approx_w, approx_h)
    if tight:
        heavy.append(tight)
    if heavy:
        log.warning("generate %s: going ahead with a heavy map - %s",
                    map_name, "; ".join(heavy))

    t0 = time.time()
    log.info("generate %s: %.5f,%.5f,%.5f,%.5f at %s m/tile, %.2f km2", map_name,
             south, west, north, east, meters_per_tile, area_km2)
    map_dir = OUTPUT_DIR / map_name
    map_dir.mkdir(parents=True, exist_ok=True)
    bbox = (south, west, north, east)
    # Remembered next to the map, so Generate buildings and any later rebuild
    # use the same ones without the page having to send them again - and so a
    # map from last week can be reproduced exactly.
    settings = Settings.from_dict(data.get("settings"))
    _save_settings(map_dir, settings)

    # What to download. A map turned to its street grid (see
    # renderer.dominant_road_angle) reaches past the drawn box at its corners,
    # and the angle is only known once the streets are in. This used to fetch
    # the box, measure the angle, then fetch the turned map's bounds as well -
    # the same town twice over, about 2.4 times the data. Now one download
    # covers the map at any angle: the circle round it, as a box.
    turned = bool(settings.align_streets) or bool(settings.rotate_degrees)
    if turned:
        fetch_box = renderer.cover_bbox(south, west, north, east, meters_per_tile)
        cache = osm.cache_path(str(map_dir), f"{map_name}_turned")
    else:
        fetch_box = bbox
        cache = osm.cache_path(str(map_dir), map_name)

    # Regenerating the same area is the common case - it is how a map gets
    # re-rendered after the ground or road rules change - and a town's worth of
    # Overpass tiles takes minutes to download every time. The reply is kept on
    # disk and reused whenever the bbox matches to the metre.
    features = osm.load_cache(cache, fetch_box)
    if features is None:
        _set_progress(map_name, stage="osm", done=0, total=1)
        def _progress(i, total):
            _set_progress(map_name, stage="osm", done=i - 1, total=total)

        try:
            # Always through the tiled path, even for a small area: with one
            # tile it is the same single request, and it brings the retry that
            # quarters a bbox the servers call too heavy.
            features = osm.fetch_features_tiled(
                *fetch_box, max_tile_km2=OVERPASS_TILE_KM2, progress=_progress,
                should_stop=_stopper(map_name))
        except knoxstop.Stopped:
            return _stopped(map_name, "generate")
        except Exception as exc:  # Overpass can be flaky — surface that clearly
            _set_progress(map_name, stage="error", message=str(exc))
            return failed(f"OSM query failed: {exc}", 502, exc)
        try:
            osm.save_cache(cache, fetch_box, features)
        except OSError:
            pass  # a map that cannot be cached still renders

    # Buildings Overture has and OpenStreetMap has not, before anything is
    # measured from them: they are ordinary building features from here on,
    # so the street angle, the ground, the gardens and the .tbx all see them
    # exactly as they see a mapped one (generator/overture.py).
    gaps = {"added": 0}
    if settings.fill_gaps:
        from generator import overture
        _set_progress(map_name, stage="overture", done=0, total=1)
        try:
            features, gaps = overture.add_missing(
                features, fetch_box, str(map_dir), map_name,
                should_stop=_stopper(map_name))
        except knoxstop.Stopped:
            return _stopped(map_name, "generate")
        except Exception as exc:  # noqa: BLE001 - OSM alone still makes a map
            log.warning("overture %s: %s", map_name, exc)
            gaps = {"added": 0, "error": str(exc)}
        if gaps.get("added"):
            log.info("overture %s: %d buildings OSM had not got, from %d fetched",
                     map_name, gaps["added"], gaps.get("fetched", 0))

    rotation = 0.0
    if settings.align_streets:
        angle, strength = renderer.dominant_road_angle(features, *bbox)
        if strength >= renderer.ALIGN_MIN_STRENGTH and abs(angle) >= 0.5:
            rotation = -angle
    # And whatever turn the mapper asked for on top of that.
    rotation += float(settings.rotate_degrees)
    osm_cache_name = Path(cache).name
    osm_bbox = fetch_box

    osm_time = time.time() - t0
    _set_progress(map_name, stage="render", features=len(features))

    try:
        result = renderer.render(
            features, south, west, north, east,
            meters_per_tile=meters_per_tile,
            output_dir=str(map_dir),
            map_name=map_name,
            spawn_density=settings.spawn_density,
            tree_density=settings.tree_density,
            rotation=rotation,
            osm_cache=osm_cache_name,
            osm_bbox=osm_bbox,
            shape=shape,
            straight_roads=bool(settings.straight_roads),
            should_stop=_stopper(map_name),
        )
    except knoxstop.Stopped:
        return _stopped(map_name, "generate")
    except MemoryError:
        # Nothing refuses a big map any more, so this is where one that really
        # was too big lands. Say what it would have taken rather than a
        # traceback: the numbers are the ones the window already showed.
        need = approx_w * approx_h * BYTES_PER_TILE + BASE_BYTES
        _set_progress(map_name, stage="error", message="ran out of memory")
        return failed(
            f"The map ran out of memory while it was being drawn. It needs about "
            f"{need / 1e9:.1f} GB for the ground and the greenery alone, at "
            f"{int(approx_w)}x{int(approx_h)} tiles. Close a few things and try "
            f"again, or draw it at a larger scale - 2 m a tile is a quarter of "
            f"the memory of 1 m. Everything downloaded is kept, so a second run "
            f"starts from the OpenStreetMap data already on disk.", 507)

    _write_readme(map_dir, map_name, result)
    _set_progress(map_name, stage="done")
    mapstate.stamp(str(map_dir), "generate")
    from_addresses = 0
    try:
        with open(map_dir / f"{map_name}_info.json", encoding="utf-8") as f:
            from_addresses = json.load(f).get("houses_from_addresses", 0)
    except (OSError, ValueError):
        pass
    log.info("generate %s: done, %d features, %dx%d tiles, rotation %.1f, "
             "%d houses from addresses, %d from Overture, %.1fs (download %.1fs)",
             map_name, len(features), result.width, result.height, rotation,
             from_addresses, gaps.get("added", 0), time.time() - t0, osm_time)

    return jsonify({
        "mapName": map_name,
        "width": result.width,
        "height": result.height,
        "cellsX": result.cells_x,
        "cellsY": result.cells_y,
        "featureCount": len(features),
        "rotation": round(rotation, 1),
        "osmSeconds": round(osm_time, 2),
        # What Overture put in that OpenStreetMap had not got, so the window
        # can say whether turning it on was worth it here.
        # What the window warned about and the mapper went ahead with anyway,
        # so it can say so beside the finished map too.
        "heavy": heavy,
        "fromOverture": gaps.get("added", 0),
        "overtureError": gaps.get("error") or gaps.get("why") or "",
        "files": {
            "landscape": f"/output/{map_name}/{Path(result.landscape_path).name}",
            "vegetation": f"/output/{map_name}/{Path(result.vegetation_path).name}",
            "spawn": f"/output/{map_name}/{Path(result.spawn_map_path).name}",
            "preview": f"/output/{map_name}/{Path(result.preview_path).name}",
            "buildings": f"/output/{map_name}/{Path(result.buildings_geojson_path).name}",
            "meta": f"/output/{map_name}/{Path(result.meta_path).name}",
            "zip": f"/download/{map_name}.zip",
            "readme": f"/output/{map_name}/README.txt",
        },
    })


def _map_summary(map_dir: Path) -> dict:
    """What the window shows for a map it did not just make."""
    info = {}
    try:
        with open(map_dir / f"{map_dir.name}_info.json", encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    needs = mapstate.needs(str(map_dir))
    stages = mapstate.done(str(map_dir))
    return {
        "mapName": map_dir.name,
        "width": info.get("width_tiles", 0),
        "height": info.get("height_tiles", 0),
        "cellsX": info.get("cells_x", 0),
        "cellsY": info.get("cells_y", 0),
        "featureCount": info.get("feature_count") or info.get("building_count", 0),
        "rotation": round(info.get("rotation", 0.0), 1),
        "updated": int(map_dir.stat().st_mtime),
        # What it was made from, so the window can run the steps again
        # over the same area without asking for it to be drawn afresh.
        "bbox": info.get("bbox"),
        "metersPerTile": info.get("meters_per_tile", 1.0),
        "shape": info.get("shape"),
        # The settings this map was made with. Upgrading it sent none at all,
        # so a map drawn with Knox County roads or a tree density of its own
        # came back with the defaults and looked like a different town.
        "settings": _load_settings(map_dir).to_dict(),
        "madeWith": mapstate.made_with(str(map_dir)),
        "current": knoxlog.version(),
        "stages": {k: v.get("version") for k, v in stages.items()},
        "needs": needs,
        "needsLabels": [mapstate.LABELS[s] for s in needs],
        "files": {
            "landscape": f"/output/{map_dir.name}/{map_dir.name}.bmp",
            "vegetation": f"/output/{map_dir.name}/{map_dir.name}_veg.bmp",
            "spawn": f"/output/{map_dir.name}/{map_dir.name}_ZombieSpawnMap.bmp",
            "preview": f"/output/{map_dir.name}/{map_dir.name}_preview.png",
            "buildings": f"/output/{map_dir.name}/{map_dir.name}_buildings.geojson",
            "meta": f"/output/{map_dir.name}/{map_dir.name}_info.json",
            "zip": f"/download/{map_dir.name}.zip",
            "readme": f"/output/{map_dir.name}/README.txt",
        },
    }


@app.route("/api/maps")
def api_maps():
    """Every map in output/, newest first, and whether an older KnoxMap made
    it - so one can be opened again instead of drawn from scratch."""
    maps = []
    for entry in OUTPUT_DIR.iterdir():
        if not entry.is_dir() or not (entry / f"{entry.name}_info.json").exists():
            continue
        maps.append(_map_summary(entry))
    maps.sort(key=lambda m: -m["updated"])
    return jsonify({"maps": maps, "current": knoxlog.version()})


@app.route("/api/maps/<map_name>")
def api_map(map_name: str):
    """One map, in the shape the page shows a freshly generated one in."""
    map_dir = _map_dir(map_name)
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    summary = _map_summary(map_dir)
    summary["settings"] = _load_settings(map_dir).to_dict()
    summary["population"] = _population(map_dir)
    return jsonify(summary)


@app.route("/output/<path:relpath>")
def serve_output(relpath: str):
    return send_from_directory(OUTPUT_DIR, relpath)


# Pictures of a finished map: drawn from the compiled cells the game itself
# loads, so they are the map rather than an impression of it. A town takes the
# best part of a minute, so it runs on its own thread and the page asks.
_PICTURES: dict[str, dict] = {}


@app.route("/api/pictures", methods=["POST"])
def api_pictures():
    """Draw the map, the way the game would."""
    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    from knoxbuild import picture as pictures

    if not pictures.compiled(map_dir):
        return jsonify({"error": "Compile the map first - there is nothing to "
                                 "draw until then."}), 400
    name = map_dir.name
    with _PROGRESS_LOCK:
        if _PICTURES.get(name, {}).get("state") == "running":
            return jsonify({"started": False, "state": "running"})
        _PICTURES[name] = {"state": "running", "error": None, "files": []}

    def worker() -> None:
        log.info("pictures %s: started", name)
        t0 = time.time()
        try:
            made = pictures.pictures_of(map_dir)
            files = [f"/output/{map_dir.name}/pictures/{p.name}" for p in made]
            log.info("pictures %s: %d in %.0fs", name, len(files), time.time() - t0)
            with _PROGRESS_LOCK:
                _PICTURES[name] = {"state": "done" if files else "error",
                                   "files": files,
                                   "error": None if files else "Nothing was drawn."}
        except Exception as exc:  # noqa: BLE001 - reported to the window
            eid = knoxlog.record(exc, f"pictures {name}: failed")
            with _PROGRESS_LOCK:
                _PICTURES[name] = {"state": "error", "errorId": eid,
                                   "error": str(exc), "files": []}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/pictures-status")
def api_pictures_status():
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    with _PROGRESS_LOCK:
        return jsonify(dict(_PICTURES.get(map_dir.name, {"state": "idle", "files": []})))


# ---- the rest of the pipeline, so the whole thing lives in one window ----

def _worlded_exe(cli: bool = False) -> Path | None:
    """Find PZWorldEd. `cli` prefers the patched build with --generate-map."""
    import knoxpaths

    return knoxpaths.worlded_cli() if cli else knoxpaths.worlded_gui()


def _map_dir(map_name: str) -> Path | None:
    # A page sends null here before any map exists; that is "no such map",
    # not a crash.
    if not isinstance(map_name, str) or not map_name.strip():
        return None
    safe = SAFE_NAME.sub("_", map_name)
    d = (OUTPUT_DIR / safe).resolve()
    if not str(d).startswith(str(OUTPUT_DIR.resolve())) or not d.is_dir():
        return None
    return d


@app.route("/api/buildings", methods=["POST"])
def api_buildings():
    """Run knoxbuild over a generated map."""
    from knoxbuild.build import build as build_buildings

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    settings = Settings.from_dict(data.get("settings"))         if data.get("settings") else _load_settings(map_dir)
    _save_settings(map_dir, settings)
    log.info("buildings %s: started", map_dir.name)
    t0 = time.time()
    out = io.StringIO()
    try:
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            build_buildings(str(map_dir), settings=settings,
                            should_stop=_stopper(map_dir.name))
    except knoxstop.Stopped:
        return _stopped(map_dir.name, "buildings")
    except Exception as exc:
        log.info("buildings %s output before the error:\n%s", map_dir.name,
                 out.getvalue()[-4000:])
        return failed(f"Building generation failed: {exc}", 500, exc)
    tbx = sorted((map_dir / "buildings").glob("*.tbx"))
    mapstate.stamp(str(map_dir), "build")
    log.info("buildings %s: %d files in %.1fs\n%s", map_dir.name, len(tbx),
             time.time() - t0, out.getvalue()[-3000:])
    return jsonify({"count": len(tbx),
                    "pzw": f"{map_dir.name}.pzw",
                    "settings": settings.to_dict(),
                    "population": _population(map_dir)})


def _population(map_dir: Path) -> dict | None:
    try:
        with open(map_dir / f"{map_dir.name}_population.json", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@app.route("/api/zombies", methods=["POST"])
def api_zombies():
    """Recount a built map's zombies with new settings, without rebuilding.

    The spawn map only depends on the buildings' footprints, heights and
    kinds, which the build saved. Redrawing it takes a second or two against
    the minutes a full building pass takes, so the zombie dials can be tried
    freely. The map still needs compiling again for the game to see it.
    """
    from knoxbuild.population import recount

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    # Same rule as Generate buildings: settings sent with the request stand on
    # their own. Layering them over the saved ones let a saved value outrank
    # the preset being asked for, so "town" after a recount at 1.5 stayed 1.5.
    settings = Settings.from_dict(data.get("settings"))         if data.get("settings") else _load_settings(map_dir)
    try:
        summary = recount(str(map_dir), settings)
    except FileNotFoundError as exc:
        return failed(f"Cannot recount zombies: {exc}.", 400, exc)
    _save_settings(map_dir, settings)
    return jsonify({"population": summary})


@app.route("/api/worlded", methods=["POST"])
def api_worlded():
    """Open the generated project in PZWorldEd."""
    import subprocess

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    import knoxpaths

    exe = _worlded_exe()
    if exe is None:
        return jsonify({"error": "PZWorldEd not found. Set the PZWORLDED "
                                 "environment variable to its full path."}), 400
    pzw = map_dir / f"{map_dir.name}.pzw"
    if not pzw.exists():
        return jsonify({"error": "No .pzw yet — generate the buildings first."}), 400
    subprocess.Popen(knoxpaths.command_for(exe) + [str(pzw)])
    return jsonify({"launched": str(pzw)})


SETTINGS_FILE = "settings.json"


def _settings_path(map_dir: Path) -> Path:
    return map_dir / SETTINGS_FILE


def _load_settings(map_dir: Path) -> Settings:
    """This map's saved settings, or the defaults."""
    try:
        with open(_settings_path(map_dir), encoding="utf-8") as f:
            return Settings.from_dict(json.load(f))
    except (OSError, ValueError):
        return Settings()


def _save_settings(map_dir: Path, settings: Settings) -> None:
    try:
        with open(_settings_path(map_dir), "w", encoding="utf-8") as f:
            json.dump(settings.to_dict(), f, indent=2)
    except OSError:
        pass          # a map whose settings cannot be saved still builds


@app.route("/api/setup-status")
def api_setup_status():
    """What is installed, so the page can say exactly what is missing.

    Drawing terrain needs nothing but this app. Buildings need nothing more.
    Compiling needs the map tools and the patched compiler, and the tools need
    tile artwork extracted from the player's own game. Each gap says how to
    close it, instead of a compile failing later with a message about a
    missing executable.
    """
    import knoxpaths

    tools = knoxpaths.mapping_tools_dir()
    cli = knoxpaths.worlded_cli()
    game = knoxpaths.pz_install_dir()
    setup = knoxpaths.setup_command()
    tiles = 0
    if tools and (tools / "Tiles" / "2x").is_dir():
        tiles = sum(1 for _ in (tools / "Tiles" / "2x").glob("*.png"))
    checks = [
        {"id": "tools", "ok": bool(tools), "label": "PZ Mapping Tools",
         "fix": f"Run {setup} to download them."},
        {"id": "compiler", "ok": bool(cli), "label": "Patched map compiler",
         "fix": f"Run {setup}, or compile by hand with Open in WorldEd."},
        {"id": "game", "ok": bool(game), "label": "Project Zomboid install",
         "fix": ("Put the folder your Steam games are in under Steam libraries "
                 f"above - the game's own folder works too - or run {setup} and "
                 "paste the path when it asks."
                 + (" On a Mac the game lives inside ProjectZomboid.app; that "
                    "path is fine, and so is anything above it."
                    if sys.platform == "darwin" else "")
                 + f" If it is not installed yet, install it and run {setup} again.")},
        {"id": "build42", "ok": knoxpaths.is_build42(game),
         "label": "Project Zomboid Build 42",
         "fix": "Your game looks like Build 41. In Steam choose the Build 42 "
                "(unstable) branch under Properties > Betas."},
        # A 32-bit Python can only use about 2 GB, and a town-sized map needs
        # more: it fails part-way through with "MemoryError".
        {"id": "python64", "ok": sys.maxsize > 2 ** 32, "label": "64-bit Python",
         "fix": "KnoxMap is running 32-bit Python, which can only use about 2 GB of "
                "memory, so anything past a few square kilometres fails. Close KnoxMap "
                f"and run {setup} again: it fetches a 64-bit Python."},
        {"id": "tiles", "ok": tiles >= 400, "label": "Tile artwork from your game",
         "fix": f"Run {setup} to extract it from your install."},
        # Added to the tools after KnoxMap 1.0's first setups: without them a
        # compile still works but lays no kerbs or road markings, silently.
        {"id": "road_rules", "ok": _has_road_rules(tools), "label": "Kerbs and road markings",
         "fix": f"Run {setup} again to add them to the map tools."},
    ]
    # Off Windows the compiler is either one built for this system - what
    # Setup fetches on Linux, and nothing else is needed for it - or the
    # Windows build through Wine, which a PC playing Project Zomboid through
    # Proton already has, though not always on the path. Everything but
    # Compile works either way.
    if os.name != "nt":
        checks.append(
            {"id": "wine", "ok": knoxpaths.tools_runnable(),
             "label": "A map compiler that runs here",
             "fix": f"Run {setup} again to fetch the compiler built for this system. "
                    "If there is none for it, the map tools' Windows build needs Wine: "
                    "install it with your package manager (apt install wine, pacman -S "
                    "wine, dnf install wine), or set KNOXMAP_WINE to the build you want "
                    "used. Everything except Compile works without either."})
    optional = [
        {"id": "elevators", "ok": knoxpaths.elevators_mod_installed(),
         "label": "Elevators mod (optional)",
         "fix": "Subscribe to it on the Steam Workshop for working lifts in tall buildings."},
        {"id": "spawn_selector", "ok": knoxpaths.spawn_selector_installed(),
         "label": "Spawn Selector mod (optional)",
         "fix": "Subscribe to it on the Steam Workshop to start at any landmark of your map."},
        {"id": "overture", "ok": _overture_ready(),
         "label": "Overture Maps data (optional)",
         "fix": "Needed only for Fill gaps from Overture, which adds the buildings "
                "OpenStreetMap has not got. Install DuckDB into the Python inside "
                "KnoxMap's own .venv folder: python -m pip install duckdb. Worth it "
                "where your town is half missing from OSM; nothing else needs it."},
        {"id": "erikas_tiles", "ok": knoxpaths.erikas_tiles_ready(),
         "label": "Erika's Tiles (optional)",
         "fix": f"Subscribe to it on the Steam Workshop and run {setup} again for glass shop "
                "fronts and signs, street signs, and far more varied pictures, posters and "
                "plants. Maps made with it require it."},
    ]
    return jsonify({"ready": all(c["ok"] for c in checks), "checks": checks,
                    "optional": optional, "setupCommand": setup,
                    "mods_dir": str(knoxpaths.zomboid_user_dir() / "mods")})


@app.route("/api/steam-libraries", methods=["GET", "POST"])
def api_steam_libraries():
    """The Steam libraries KnoxMap looks in for the game and Workshop mods:
    found on their own, plus the drives or folders the player chose."""
    import knoxpaths

    if request.method == "POST":
        folders = [str(f).strip().strip('"') for f in (request.get_json(silent=True) or {}).get("folders", [])
                   if str(f).strip()]
        bad = [f for f in folders if not knoxpaths.library_of(f)]
        if bad:
            return jsonify({"error": f"No Steam library found in {', '.join(bad)}. Name the drive "
                                     "(E:) or the folder that holds steamapps."}), 400
        knoxpaths.save_steam_folders(folders)
        log.info("steam folders chosen: %s", folders)
    return jsonify({"libraries": knoxpaths.steam_libraries_found(),
                    "chosen": knoxpaths.load_config().get("steam_folders", []),
                    "game": str(knoxpaths.pz_install_dir() or "")})


def _has_road_rules(tools) -> bool:
    if not tools:
        return False
    rules = tools / "config" / "Rules.txt"
    try:
        text = rules.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # The newest rule, so tools patched before street furniture existed are
    # sent back through Setup - and no blank litter tile, so are the ones
    # patched while the litter rule still named trash_01_13 and 14, which the
    # game draws as a question mark.
    return "KnoxMap road Yard bed_soil" in text and "trash_01_13" not in text


def _overture_ready() -> bool:
    """Whether gap-filling from Overture can run here. Kept off the import
    path: the check is one import and the window asks for it on every poll."""
    try:
        from generator import overture
        return overture.available()
    except Exception:  # noqa: BLE001 - a broken optional extra is just absent
        return False


@app.route("/api/settings")
def api_settings():
    """The knobs, their limits, and the presets - so the page is not a
    hard-coded copy of them that drifts out of step."""
    from knoxbuild.settings import LIMITS

    map_dir = _map_dir(request.args.get("map", ""))
    current = _load_settings(map_dir) if map_dir else Settings()
    from dataclasses import fields

    return jsonify({
        "current": current.to_dict(),
        "defaults": Settings().to_dict(),
        # JSON writes 1.0 as 1, so the page cannot tell a float whose default
        # happens to be whole from an int - and a woodland slider built as an
        # integer can only reach 0, 1, 2 or 3. Say which is which.
        "types": {f.name: ("int" if f.type in (int, "int") else "float")
                  for f in fields(Settings)},
        "limits": {k: list(v) for k, v in LIMITS.items()},
        "presets": {name: s.to_dict() for name, s in PRESETS.items()},
    })


def _expected_cells(map_dir: Path) -> int:
    """How many 256-tile cells the compile should produce, for a progress bar."""
    try:
        with open(map_dir / f"{map_dir.name}_info.json", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        return 0
    from knoxbuild.world import CELL_SIZE, WORLD_ORIGIN_CELLS, _project_box

    # Where this map was actually built, which is not 70,0 once a PC holds
    # more than one of them (knoxbuild/world.py choose_origin).
    box = _project_box(str(map_dir / f"{map_dir.name}.pzw"))
    ox = (box[0] if box else WORLD_ORIGIN_CELLS[0]) * CELL_SIZE
    oy = (box[1] if box else WORLD_ORIGIN_CELLS[1]) * CELL_SIZE
    w = info.get("cells_x", 0) * CELL_SIZE
    h = info.get("cells_y", 0) * CELL_SIZE
    x0, x1 = ox // 256, (ox + w + 255) // 256
    y0, y1 = oy // 256, (oy + h + 255) // 256
    return max(0, (x1 - x0) * (y1 - y0))


@app.route("/api/compile-status")
def api_compile_status():
    """Progress of a running compile, so the page never has to block on one."""
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    with _PROGRESS_LOCK:
        state = dict(_COMPILE.get(map_dir.name, {"state": "idle"}))
    lots = map_dir / "lots"
    state["cells"] = len(list(lots.glob("*.lotheader"))) if lots.is_dir() else 0
    state["expected"] = _expected_cells(map_dir)
    state["tmx"] = len(list((map_dir / "tmx").glob("*.tmx"))) \
        if (map_dir / "tmx").is_dir() else 0
    # Cells the last compile tried three times and could not do. It steps over
    # them rather than throwing away the hours already spent, so this is how
    # the window knows to offer them again instead of saying "done".
    from tools.compile_map import failed_cells
    state["failed"] = failed_cells(map_dir)
    return jsonify(state)


@app.route("/api/compile", methods=["POST"])
def api_compile():
    """Convert and compile the whole map without touching WorldEd's menus.

    Uses the patched PZWorldEd_cli.exe, which adds a --generate-map switch that
    runs BMP to TMX and Generate Lots in order. Stock WorldEd has no such
    switch, so without the patched build this falls back to /api/worlded.
    """
    import subprocess

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    exe = _worlded_exe(cli=True)
    if exe is None:
        return jsonify({"error": "Patched PZWorldEd_cli.exe not found — use "
                                 "Open in WorldEd and run the two menu "
                                 "commands instead."}), 400
    pzw = map_dir / f"{map_dir.name}.pzw"
    if not pzw.exists():
        return jsonify({"error": "No .pzw yet — generate the buildings first."}), 400

    # "Compile the cells that failed" rather than the whole town again. A full
    # re-run would skip the batches already on disk anyway, but it walks every
    # one of them to find that out, and on a big map that is minutes before it
    # reaches the handful that matter.
    from tools.compile_map import failed_cells
    only = [f["cells"] for f in failed_cells(map_dir)] if data.get("onlyFailed") else None
    if data.get("onlyFailed") and not only:
        return jsonify({"error": "Nothing is recorded as failed."}), 400

    (map_dir / "tmx").mkdir(exist_ok=True)
    (map_dir / "lots").mkdir(exist_ok=True)

    name = map_dir.name
    with _PROGRESS_LOCK:
        if _COMPILE.get(name, {}).get("state") == "running":
            return jsonify({"started": False, "state": "running"})
        _COMPILE[name] = {"state": "running", "error": None}

    def worker() -> None:
        """Compiling a town takes many minutes.

        Running it inside the request blocked the whole UI - the window simply
        froze until it finished. It runs on its own thread now and the page
        polls /api/compile-status, so the app stays usable and shows progress.

        The work goes out in batches of cells, one short-lived WorldEd
        process each: a single process compiling a whole town never gives
        its memory back and took the machine down at 13.7 GB. See
        tools/compile_map.py.
        """
        from tools import compile_map as compiler

        def note(done: int, total: int, cells: int) -> None:
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "running", "error": None,
                                  "batch": done, "batches": total}

        log.info("compile %s: started", name)
        t0 = time.time()
        try:
            produced = compiler.compile_map(str(map_dir), batch=COMPILE_BATCH,
                                            exe=str(exe), on_progress=note,
                                            should_stop=_stopper(name),
                                            only_cells=only)
            if not produced:
                eid = knoxlog.record(None, f"compile {name}: produced no cells")
                with _PROGRESS_LOCK:
                    _COMPILE[name] = {"state": "error", "errorId": eid,
                                      "error": "Compile produced no cells."}
                return
            log.info("compile %s: %d cells in %.0fs", name, produced, time.time() - t0)
            # A compile that stepped over a batch is finished but not whole:
            # installing it gives a map with a hole in it, so the window is
            # told what is missing rather than a plain "done".
            left = compiler.failed_cells(str(map_dir))
            with _PROGRESS_LOCK:
                mapstate.stamp(str(map_dir), "compile")
                _COMPILE[name] = {"state": "done", "error": None, "failed": left}
        except knoxstop.Stopped:
            log.info("compile %s: stopped after %.0fs", name, time.time() - t0)
            _done_stopping(name)
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "stopped", "error": None}
            return
        except subprocess.TimeoutExpired as exc:
            eid = knoxlog.record(exc, f"compile {name}: timed out")
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "error", "errorId": eid,
                                  "error": "Compile timed out."}
        except Exception as exc:
            eid = knoxlog.record(exc, f"compile {name}: failed")
            with _PROGRESS_LOCK:
                _COMPILE[name] = {"state": "error", "errorId": eid, "error": str(exc)}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"started": True, "expected": _expected_cells(map_dir)})


@app.route("/api/lots")
def api_lots():
    """Has WorldEd's Generate Lots produced anything yet?"""
    map_dir = _map_dir(request.args.get("map", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    lots = map_dir / "lots"
    cells = sorted(lots.glob("*.lotheader")) if lots.is_dir() else []
    return jsonify({"compiled": bool(cells), "cells": len(cells)})


@app.route("/api/install", methods=["POST"])
def api_install():
    """Package the compiled map into ~/Zomboid/mods."""
    from tools import make_map_mod

    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return jsonify({"error": "Unknown map."}), 404
    # Both end up in folder names and mod.info, so they are text of a sane
    # length whatever the page sent.
    raw_title, raw_id = data.get("title"), data.get("modId")
    title = (raw_title if isinstance(raw_title, str) and raw_title.strip()
             else map_dir.name).strip()[:80]
    mod_id = SAFE_NAME.sub("_", raw_id if isinstance(raw_id, str) and raw_id.strip()
                           else map_dir.name).strip("_")[:60] or map_dir.name[:60]
    try:
        mod_root, n_cells, extras = make_map_mod.package(
            str(map_dir), title, mod_id)
    except FileNotFoundError as exc:
        return failed(str(exc), 400, exc)
    except Exception as exc:
        return failed(f"Install failed: {exc}", 500, exc)
    mapstate.stamp(str(map_dir), "install")
    log.info("install %s: %d cells as %s, extras %s", map_dir.name, n_cells, mod_id, extras)
    return jsonify({"modRoot": str(mod_root), "cells": n_cells,
                    "extras": extras, "modId": mod_id, "title": title})


def _bbox_area_km2(south: float, west: float, north: float, east: float) -> float:
    lat_mid = (south + north) / 2
    h_km = (north - south) * 111.32
    w_km = (east - west) * 111.32 * _cos_lat(lat_mid)
    return h_km * w_km


def _cos_lat(lat_deg: float) -> float:
    import math
    return math.cos(math.radians(lat_deg))


def _write_readme(map_dir: Path, map_name: str, result: renderer.RenderResult) -> None:
    text = f"""Project Zomboid map: {map_name}
Generated by KnoxMap.

Bitmap dimensions: {result.width}x{result.height} tiles
Cell grid:         {result.cells_x} x {result.cells_y} (cells are always 300 tiles)

Files
-----
{map_name}.bmp                  Landscape (base terrain — WorldEd's main input)
{map_name}_veg.bmp              Vegetation (trees, bushes, long grass)
{map_name}_ZombieSpawnMap.bmp   Zombie population (grayscale, 1/10 scale)
{map_name}_preview.png          Human-viewable preview of what you'll get
{map_name}_buildings.geojson    Building footprints from OSM (for reference)
{map_name}_info.json            Meta: bbox, scale, cell count
{map_name}.zip                  Everything in this folder, from the web UI

How to import (per Thuztor's Mapping Guide v0.2, chapter 2)
-----------------------------------------------------------
1. Open WorldEd (part of the Zomboid Mapping Tools).
2. File -> New, and choose a {result.cells_x} x {result.cells_y} cell grid.
3. From your file browser, drag {map_name}.bmp onto the empty grid.
   (WorldEd reads the matching {map_name}_veg.bmp automatically if it sits
   next to the landscape bitmap with the same base filename.)
4. File -> BMP to TMX -> All cells. Set an export folder for the .tmx output.
5. Open the resulting project in WorldEd / TileZed to place buildings
   (.tbx files) on top of the landscape. This tool does NOT place PZ
   buildings — OSM building footprints are exported as GeoJSON for
   reference only.
6. File -> Generate Lots. This produces .lotheader + .lotpack files.
7. Copy those into your game's media/maps folder (see chapter 9 of the
   guide for offset / world-origin details).

Notes
-----
* Roads render as asphalt (residential = light, secondary = medium,
  primary/motorway = dark). Paths and tracks render as dirt lines.
* Forests render as dense trees in the interior and a grass+tree blend
  at the edges so the transition isn't a hard rectangle.
* The spawn map is generated procedurally: higher density on asphalt,
  zero on water, slight randomness throughout.
* OSM building footprints CANNOT be converted directly to PZ buildings —
  PZ buildings are a separate thing you assemble in BuildingEd (.tbx).
  The GeoJSON file lets you see where buildings would sit in the real
  world and drop matching .tbx lots in the right tiles.
"""
    # UTF-8 whatever the PC's code page: the text has dashes that a Korean or
    # Japanese Windows cannot write in its own, and the whole generate request
    # failed on them after the map was already drawn.
    (map_dir / "README.txt").write_text(text, encoding="utf-8")


@app.route("/api/save", methods=["POST"])
def api_save():
    """Save a map's file where the player can find it and show it in Explorer.

    A download link does nothing in the app window: it is a WebView, not a
    browser, with nowhere to put a file. Everything is already on disk in the
    map's own folder, so "downloading" here means making the zip when that is
    what was asked for, and opening Explorer with the file selected.
    """
    data = _json_body()
    map_dir = _map_dir(data.get("mapName", ""))
    if map_dir is None:
        return failed("Unknown map.", 404)
    name = str(data.get("name", "")).strip()
    if name in ("", "zip"):
        target = map_dir / f"{map_dir.name}.zip"
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(map_dir.rglob("*")):
                    if not path.is_file() or path.suffix.lower() == ".zip":
                        continue
                    zf.write(path, arcname=str(Path(map_dir.name) / path.relative_to(map_dir)))
        except OSError as exc:
            return failed(f"Could not write the zip: {exc}", 500, exc)
    else:
        target = (map_dir / name).resolve()
        if not str(target).startswith(str(map_dir.resolve())) or not target.is_file():
            return failed("That file is not in this map's folder.", 400)
    knoxlog.open_folder(target)
    log.info("saved %s for %s", target.name, map_dir.name)
    return jsonify({"path": str(target), "name": target.name,
                    "folder": str(target.parent)})


@app.route("/download/<map_name>.zip")
def download_all(map_name: str):
    """Everything for one map, zipped on demand.

    Built when asked for rather than at generation time, so it also picks up
    anything produced later - the .tbx buildings, the .pzw project and the
    placement CSV that `python -m knoxbuild` writes into the same folder.
    """
    safe = SAFE_NAME.sub("_", map_name)
    map_dir = (OUTPUT_DIR / safe).resolve()
    if not str(map_dir).startswith(str(OUTPUT_DIR.resolve())):
        return jsonify({"error": "Bad map name."}), 400
    if not map_dir.is_dir():
        return jsonify({"error": f"No output for {safe!r}."}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(map_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() == ".zip":
                continue
            zf.write(path, arcname=str(Path(safe) / path.relative_to(map_dir)))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe}.zip")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
