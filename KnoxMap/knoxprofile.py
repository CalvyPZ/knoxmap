"""Debug-only profiler for a launch from debug_run.bat.

KNOXMAP_DEBUG=1 times every Python function in this process and in the build
workers it starts. The timings are what the profiler window shows, and what
logs/debug-log.log keeps. A normal launch never imports this.
"""
from __future__ import annotations

import atexit
import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

SLOW_SECONDS = 0.05
MAX_LOG = 8 * 1024 * 1024
_SKIP = object()

_tls = threading.local()
_reg_lock = threading.Lock()
_slow_lock = threading.Lock()
_file_lock = threading.Lock()
_buckets: list[dict] = []
_keys: dict = {}
_slow_ui: deque = deque(maxlen=80)
_slow_log: deque = deque(maxlen=2000)
_slow_seq = 0
_child_slow_seq: dict[int, int] = {}
_failures = 0
_epoch = 0
_recording = True
_started = False
_finished = False
_leader = False
_t0 = 0.0
_header_done = False
_session_mark = 0
_seen_reset = 0
_log: Path | None = None
_profile_dir: Path | None = None
_reset_path: Path | None = None
_flusher: threading.Thread | None = None
_stop = threading.Event()
_orig_thread_start = None
_worker_cache: tuple[list, float] = ([], 0.0)

_ROOT = Path(__file__).resolve().parent
_ROOT_TEXT = str(_ROOT).replace("/", "\\")
_ROOT_FOLD = _ROOT_TEXT.casefold()


def start() -> None:
    """Begin timing. Safe to call once; a second call does nothing."""
    try:
        _start()
    except Exception:
        try:
            import knoxlog
            knoxlog.log.warning("debug profiler did not start", exc_info=True)
        except Exception:
            pass


def attach(flask_app) -> None:
    """Serve the profiler window from the app that is already running."""
    if getattr(flask_app, "_knox_profiler", False):
        return
    flask_app._knox_profiler = True
    from flask import jsonify, render_template, request

    @flask_app.route("/debug")
    @flask_app.route("/debug/")
    def debug_profiler_page():
        return render_template("profiler.html")

    @flask_app.route("/api/debug/profile", methods=["GET", "POST"])
    def debug_profiler_api():
        opened = None
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            if "paused" in body:
                set_paused(bool(body.get("paused")))
            if body.get("clear"):
                clear()
            if body.get("open"):
                import knoxlog
                opened = knoxlog.open_folder(_log) if _log else False
        payload = snapshot(
            scope=request.args.get("scope", "knox"),
            sort=request.args.get("sort", "cum"),
            limit=_clamp(request.args.get("limit"), 300),
            query=(request.args.get("q") or "").strip().casefold(),
        )
        if opened is not None:
            payload["opened"] = bool(opened)
        response = jsonify(payload)
        response.headers["Cache-Control"] = "no-store"
        return response


def set_paused(paused: bool) -> None:
    """Stop adding calls, and keep the ones already recorded."""
    global _recording, _epoch
    paused = bool(paused)
    if paused == (not _recording):
        return
    if paused:
        _recording = False
        _epoch += 1
    else:
        _recording = True


def clear() -> None:
    """Zero the counts. The log keeps what was written before this."""
    global _session_mark, _worker_cache
    _session_mark = int(time.time() * 1000)
    _worker_cache = ([], 0.0)
    try:
        if _reset_path is not None:
            _reset_path.write_text(str(_session_mark), encoding="ascii")
    except OSError:
        pass
    _reset_counts(True)
    if not _leader:
        return
    _child_slow_seq.clear()
    _delete_worker_dumps()


def snapshot(scope: str = "knox", sort: str = "cum", limit: int = 300,
             query: str = "") -> dict:
    try:
        return _snapshot(scope, sort, limit, query)
    except Exception as exc:  # noqa: BLE001 - the window shows this, the app stays up
        return {
            "error": str(exc), "rows": [], "slow": [], "paused": not _recording,
            "log": str(_log or ""), "calls": 0, "knoxCalls": 0, "functions": 0,
            "workers": 0, "uptime": 0, "selfSeconds": 0, "knoxSelfSeconds": 0,
            "slowMs": int(SLOW_SECONDS * 1000), "matched": 0, "shown": 0,
        }


def finish() -> None:
    """Write the last snapshot. Runs when the process exits."""
    global _finished, _recording
    if not _started or _finished:
        return
    _finished = True
    _recording = False
    sys.setprofile(None)
    _stop.set()
    thread = _flusher
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1.5)
    try:
        if _leader:
            _append_slow_lines()
            _append_summary(True)
            _append_text("--- session ended ---\n")
        else:
            _dump_worker()
    except Exception:
        pass


def enable_worker() -> None:
    """A build process: time its own calls into the same profiler folder."""
    os.environ["KNOXMAP_PROFILE_WORKER"] = "1"
    start()


def _worker_boot(user_initializer, user_initargs) -> None:
    try:
        enable_worker()
    except Exception:
        pass
    if user_initializer is not None:
        user_initializer(*user_initargs)


def _start() -> None:
    global _started, _leader, _t0, _log, _profile_dir, _reset_path, _flusher, _seen_reset
    if _started:
        return
    _started = True
    _leader = os.environ.get("KNOXMAP_PROFILE_WORKER") != "1"
    _t0 = time.perf_counter()

    import knoxlog
    folder = Path(os.environ.get("KNOXMAP_PROFILE_DIR") or (knoxlog.LOG_DIR / "debug-profile"))
    _profile_dir = folder
    _reset_path = folder / "reset"
    _log = knoxlog.LOG_DIR / "debug-log.log"
    if _leader:
        os.environ["KNOXMAP_PROFILE_DIR"] = str(folder)
        _prepare_dir(folder, True)
        _log.parent.mkdir(parents=True, exist_ok=True)
        _append_text("Recording function calls.\n")
        knoxlog.log.info("debug profiler writing %s", _log)
    else:
        _prepare_dir(folder, False)
        _seen_reset = _read_reset_mark()

    _patch_threads()
    try:
        _patch_pool()
    except Exception:
        pass
    if hasattr(threading, "setprofile"):
        threading.setprofile(_hook)

    _flusher = threading.Thread(target=_flush_loop, name="knox-profiler", daemon=True)
    _flusher.start()
    sys.setprofile(_hook)
    atexit.register(finish)


def _prepare_dir(folder: Path, leader: bool) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    if not leader:
        return
    for child in folder.iterdir():
        if child.suffix == ".json" or child.name == "reset" or child.suffix == ".tmp":
            try:
                child.unlink()
            except OSError:
                pass


def _patch_threads() -> None:
    global _orig_thread_start
    if getattr(threading.Thread.start, "_knox_profile", False):
        return
    _orig_thread_start = threading.Thread.start

    def start(self, *args, **kwargs):
        run = self.run
        name = getattr(self, "name", "")

        def profiled():
            # The sampler thread would time itself and fill the log with it.
            if name == "knox-profiler":
                sys.setprofile(None)
            else:
                sys.setprofile(_hook)
            try:
                run()
            finally:
                sys.setprofile(None)

        self.run = profiled
        return _orig_thread_start(self, *args, **kwargs)

    start._knox_profile = True
    threading.Thread.start = start


def _patch_pool() -> None:
    import concurrent.futures
    import inspect

    cls = concurrent.futures.ProcessPoolExecutor
    if getattr(cls, "_knox_profile", False):
        return
    orig = cls.__init__
    sig = inspect.signature(orig)

    def wrapped(self, *args, **kwargs):
        bound = sig.bind(self, *args, **kwargs)
        bound.apply_defaults()
        user_initializer = bound.arguments.get("initializer")
        user_initargs = bound.arguments.get("initargs") or ()
        bound.arguments["initializer"] = _worker_boot
        bound.arguments["initargs"] = (user_initializer, tuple(user_initargs))
        params = dict(bound.arguments)
        params.pop("self", None)
        return orig(self, **params)

    wrapped._knox_profile = True
    cls.__init__ = wrapped
    cls._knox_profile = True


def _hook(frame, event, arg) -> None:
    global _failures
    if _failures >= 3:
        return
    try:
        if event == "call":
            _enter(frame)
        elif event == "return":
            _leave(frame)
    except Exception:
        _failures += 1
        if _failures >= 3:
            sys.setprofile(None)


def _enter(frame) -> None:
    local = _tls
    if getattr(local, "busy", False):
        return
    local.busy = True
    try:
        _ensure(local)
        code = frame.f_code
        now = time.perf_counter()
        key = _lookup(code)
        free = local.free
        if free:
            row = free.pop()
            row[0] = key
            row[1] = now
            row[2] = 0.0
            row[3] = _epoch
            row[4] = _recording
            row[5] = code
        else:
            row = [key, now, 0.0, _epoch, _recording, code]
        local.stack.append(row)
    finally:
        local.busy = False


def _leave(frame) -> None:
    local = _tls
    if getattr(local, "busy", False):
        return
    local.busy = True
    try:
        stack = getattr(local, "stack", None)
        if not stack:
            return
        now = time.perf_counter()
        row = stack.pop()
        code = frame.f_code
        if row[5] is not code:
            _recycle(local, row)
            return
        key = row[0]
        start = row[1]
        child = row[2]
        epoch = row[3]
        record = row[4]
        _recycle(local, row)
        elapsed = now - start
        if elapsed < 0:
            elapsed = 0.0
        if stack and stack[-1][3] == epoch:
            stack[-1][2] += elapsed
        if key is None or not record or epoch != _epoch:
            return
        self_t = elapsed - child
        if self_t < 0:
            self_t = 0.0
        _add_stat(local, key, self_t, elapsed)
        if elapsed >= SLOW_SECONDS:
            _note_slow(frame, key, elapsed)
    finally:
        local.busy = False


def _ensure(local) -> None:
    if getattr(local, "stack", None) is None:
        local.stack = []
        local.free = []


def _recycle(local, row) -> None:
    free = getattr(local, "free", None)
    if free is None or len(free) >= 512:
        return
    row[0] = None
    row[5] = None
    free.append(row)


def _add_stat(local, key, self_t: float, elapsed: float) -> None:
    stats = getattr(local, "stats", None)
    if stats is None:
        stats = {}
        local.stats = stats
        with _reg_lock:
            _buckets.append(stats)
    val = stats.get(key)
    if val is None:
        stats[key] = [1, self_t, elapsed]
    else:
        val[0] += 1
        val[1] += self_t
        val[2] += elapsed


def _note_slow(frame, key, elapsed: float) -> None:
    global _slow_seq
    caller = _caller_of(frame)
    with _slow_lock:
        _slow_seq += 1
        entry = (_slow_seq, time.time(), elapsed, key[0], key[1], key[2], caller)
        _slow_ui.append(entry)
        _slow_log.append(entry)


def _caller_of(frame) -> str:
    back = frame.f_back
    if back is None:
        return ""
    code = back.f_code
    info = _lookup(code)
    if info is None:
        name = getattr(code, "co_qualname", None) or code.co_name
        return f"{_classify(code.co_filename)[0]}:{back.f_lineno} {name}"
    return f"{info[0]}:{back.f_lineno} {info[2]}"


def _lookup(code):
    found = _keys.get(code)
    if found is not None:
        return None if found is _SKIP else found
    filename = code.co_filename
    if _profiler_file(filename):
        if len(_keys) < 100000:
            _keys[code] = _SKIP
        return None
    display, knox = _classify(filename)
    qual = getattr(code, "co_qualname", None) or code.co_name
    found = (display, code.co_firstlineno, qual, knox)
    if len(_keys) < 100000:
        _keys[code] = found
    return found


def _profiler_file(filename: str) -> bool:
    return filename.replace("/", "\\").rsplit("\\", 1)[-1].casefold() == "knoxprofile.py"


def _classify(filename: str) -> tuple[str, bool]:
    text = filename.replace("/", "\\")
    root = _ROOT_TEXT
    if (len(text) > len(root) and text[len(root)] == "\\"
            and text[:len(root)].casefold() == _ROOT_FOLD):
        rel = text[len(root):].lstrip("\\").replace("\\", "/")
        folded = rel.casefold()
        if folded.startswith(".venv/") or folded.startswith("cache/"):
            return rel, False
        return rel, True
    slash = filename.replace("\\", "/")
    folded = slash.casefold()
    at = folded.find("site-packages/")
    if at >= 0:
        return slash[at:], False
    return slash, False


def _reset_counts(write_log: bool) -> None:
    global _epoch
    _epoch += 1
    with _reg_lock:
        buckets = list(_buckets)
    for bucket in buckets:
        bucket.clear()
    with _slow_lock:
        _slow_ui.clear()
        _slow_log.clear()
    if write_log and _leader:
        _append_text("--- counts cleared ---\n")


def _flush_loop() -> None:
    sys.setprofile(None)
    next_summary = time.perf_counter()
    interval = 0.5 if _leader else 1.0
    while not _stop.wait(interval):
        try:
            if _leader:
                _append_slow_lines()
                now = time.perf_counter()
                if now >= next_summary:
                    next_summary = now + 5
                    _append_summary(False)
            else:
                _honor_reset()
                _dump_worker()
        except Exception:
            continue


def _honor_reset() -> None:
    global _seen_reset
    mark = _read_reset_mark()
    if mark <= _seen_reset:
        return
    _reset_counts(False)
    _seen_reset = mark


def _read_reset_mark() -> int:
    try:
        if _reset_path is None:
            return 0
        return int(_reset_path.read_text(encoding="ascii").strip() or "0")
    except (OSError, ValueError):
        return 0


def _dump_worker() -> None:
    if _profile_dir is None:
        return
    merged = _local_merged()
    rows = _compact_rows(merged)
    with _slow_lock:
        slow = [
            [seq, t, round(elapsed * 1000, 3), file, line, func, caller]
            for seq, t, elapsed, file, line, func, caller in _slow_ui
        ]
    payload = {
        "pid": os.getpid(),
        "since": _seen_reset,
        "rows": rows,
        "slow": slow,
    }
    path = _profile_dir / f"{os.getpid()}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _compact_rows(merged: dict) -> list:
    knox = []
    libs = []
    for key, val in merged.items():
        if val[0] <= 0:
            continue
        item = [key[0], key[1], key[2], bool(key[3]), val[0], val[1], val[2]]
        if key[3]:
            knox.append(item)
        else:
            libs.append(item)
    libs.sort(key=lambda item: item[6], reverse=True)
    return knox + libs[:100]


def _local_merged() -> dict:
    merged: dict = {}
    with _reg_lock:
        buckets = list(_buckets)
    for bucket in buckets:
        try:
            items = list(bucket.items())
        except RuntimeError:
            continue
        for key, val in items:
            try:
                calls, self_t, cum = val[0], val[1], val[2]
            except (TypeError, ValueError, IndexError):
                continue
            _add_merged(merged, key, calls, self_t, cum)
    return merged


def _add_merged(merged, key, calls, self_t, cum) -> None:
    row = merged.get(key)
    calls = int(calls)
    self_t = float(self_t)
    cum = float(cum)
    if row is None:
        merged[key] = [calls, self_t, cum]
        return
    row[0] += calls
    row[1] += self_t
    row[2] += cum


def _worker_payloads() -> list[dict]:
    global _worker_cache
    now = time.perf_counter()
    cached, at = _worker_cache
    if at and now - at < 0.4:
        return cached
    folder = _profile_dir
    found: list[dict] = []
    if folder is not None and folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                since = int(data.get("since") or 0)
            except (TypeError, ValueError):
                since = 0
            if since < _session_mark:
                continue
            found.append(data)
    _worker_cache = (found, now)
    return found


def _delete_worker_dumps() -> None:
    folder = _profile_dir
    if folder is None or not folder.is_dir():
        return
    for path in folder.glob("*.json"):
        try:
            path.unlink()
        except OSError:
            pass


def _merged_rows() -> tuple[list[dict], int]:
    merged = _local_merged()
    workers = _worker_payloads()
    for data in workers:
        for item in data.get("rows") or []:
            if not isinstance(item, list) or len(item) < 7:
                continue
            try:
                key = (str(item[0]), int(item[1]), str(item[2]), bool(item[3]))
                _add_merged(merged, key, item[4], item[5], item[6])
            except (TypeError, ValueError):
                continue
    rows = []
    for (file, line, func, knox), (calls, self_t, cum) in merged.items():
        if calls <= 0:
            continue
        rows.append({
            "file": file,
            "line": int(line),
            "func": func,
            "knox": bool(knox),
            "calls": int(calls),
            "self": self_t,
            "cum": cum,
        })
    return rows, len(workers)


def _snapshot(scope: str, sort: str, limit: int, query: str) -> dict:
    if scope not in ("knox", "all"):
        scope = "knox"
    if sort not in ("cum", "self", "calls", "each"):
        sort = "cum"
    rows, workers = _merged_rows()
    calls = 0
    knox_calls = 0
    self_s = 0.0
    knox_self = 0.0
    for row in rows:
        calls += row["calls"]
        self_s += row["self"]
        if row["knox"]:
            knox_calls += row["calls"]
            knox_self += row["self"]
    shown = rows
    if scope == "knox":
        shown = [row for row in shown if row["knox"]]
    if query:
        shown = [
            row for row in shown
            if query in row["func"].casefold() or query in row["file"].casefold()
        ]
    shown.sort(key=lambda row: (-_sort_value(row, sort), row["func"].casefold()))
    matched = len(shown)
    clipped = shown[:limit]
    for row in clipped:
        row["self"] = round(row["self"], 6)
        row["cum"] = round(row["cum"], 6)
    return {
        "paused": not _recording,
        "uptime": round(max(0.0, time.perf_counter() - _t0), 3) if _t0 else 0,
        "calls": calls,
        "knoxCalls": knox_calls,
        "functions": len(rows),
        "knoxFunctions": sum(1 for row in rows if row["knox"]),
        "selfSeconds": round(self_s, 6),
        "knoxSelfSeconds": round(knox_self, 6),
        "workers": workers,
        "log": str(_log or ""),
        "slowMs": int(SLOW_SECONDS * 1000),
        "matched": matched,
        "shown": len(clipped),
        "rows": clipped,
        "slow": _slow_view(),
        "scope": scope,
        "sort": sort,
    }


def _sort_value(row: dict, sort: str) -> float:
    if sort == "calls":
        return row["calls"]
    if sort == "self":
        return row["self"]
    if sort == "each":
        calls = row["calls"] or 1
        return row["cum"] / calls
    return row["cum"]


def _slow_view() -> list[dict]:
    found = []
    with _slow_lock:
        items = list(_slow_ui)
    for seq, t, elapsed, file, line, func, caller in items:
        found.append(_slow_dict(t, elapsed * 1000, file, line, func, caller, 0))
    for data in _worker_payloads():
        try:
            worker = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            worker = 0
        for raw in data.get("slow") or []:
            if not isinstance(raw, list) or len(raw) < 7:
                continue
            try:
                found.append(_slow_dict(
                    float(raw[1]), float(raw[2]), str(raw[3]), int(raw[4]),
                    str(raw[5]), str(raw[6] or ""), worker,
                ))
            except (TypeError, ValueError):
                continue
    found.sort(key=lambda row: row["time"], reverse=True)
    out = []
    for row in found[:40]:
        out.append({
            "t": _stamp(row["time"]),
            "ms": round(row["ms"], 1),
            "file": row["file"],
            "line": row["line"],
            "func": row["func"],
            "caller": row["caller"],
            "pid": row["pid"],
        })
    return out


def _slow_dict(t, ms, file, line, func, caller, pid) -> dict:
    return {
        "time": t, "ms": ms, "file": file, "line": line, "func": func,
        "caller": caller, "pid": pid,
    }


def _fresh_child_slow(data: dict) -> list:
    try:
        pid = int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return []
    last = _child_slow_seq.get(pid, 0)
    fresh = []
    highest = last
    for raw in data.get("slow") or []:
        if not isinstance(raw, list) or len(raw) < 7:
            continue
        try:
            seq = int(raw[0])
        except (TypeError, ValueError):
            continue
        if seq > highest:
            highest = seq
        if seq > last:
            fresh.append(raw)
    fresh.sort(key=lambda raw: int(raw[0]))
    if highest > last:
        _child_slow_seq[pid] = highest
    return fresh


def _append_slow_lines() -> None:
    with _slow_lock:
        items = list(_slow_log)
        _slow_log.clear()
    lines = [_format_slow(item, os.getpid()) for item in items]
    for data in _worker_payloads():
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        for raw in _fresh_child_slow(data):
            try:
                entry = (
                    int(raw[0]), float(raw[1]), float(raw[2]) / 1000.0,
                    str(raw[3]), int(raw[4]), str(raw[5]), str(raw[6] or ""),
                )
            except (TypeError, ValueError):
                continue
            lines.append(_format_slow(entry, pid))
    if lines:
        _append_text("\n".join(lines) + "\n")


def _append_summary(final: bool) -> None:
    rows, workers = _merged_rows()
    knox = sorted((row for row in rows if row["knox"]), key=lambda row: row["cum"], reverse=True)
    libs = sorted((row for row in rows if not row["knox"]), key=lambda row: row["cum"], reverse=True)
    calls = sum(row["calls"] for row in rows)
    knox_calls = sum(row["calls"] for row in knox)
    uptime = max(0.0, time.perf_counter() - _t0) if _t0 else 0
    label = "final" if final else "snapshot"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"\n--- {stamp}  {label}  uptime {uptime:.1f}s  "
        f"calls {calls}  knox {knox_calls}  functions {len(rows)}  "
        f"workers {workers} ---\n"
        "KnoxMap\n"
        f"{_format_table(knox, 40)}\n"
        "Libraries\n"
        f"{_format_table(libs, 20)}\n"
    )
    _append_text(text)


def _format_table(rows: list[dict], limit: int) -> str:
    lines = [
        "     calls         self        total        each  function",
        "     -----         ----        -----        ----  --------",
    ]
    if not rows:
        lines.append("     (none)")
        return "\n".join(lines)
    for row in rows[:limit]:
        calls = row["calls"] or 1
        each = row["cum"] / calls * 1000
        where = f"{row['file']}:{row['line']}  {row['func']}"
        lines.append(
            f"{row['calls']:10d} {row['self'] * 1000:12.1f} {row['cum'] * 1000:12.1f} "
            f"{each:12.2f}  {where}"
        )
    return "\n".join(lines)


def _format_slow(entry, pid: int) -> str:
    _seq, t, elapsed, file, line, func, caller = entry
    text = f"{_stamp(t)}  {elapsed * 1000:10.1f} ms  {file}:{line}  {func}"
    if caller:
        text += f"  <- {caller}"
    if pid and pid != os.getpid():
        text += f"  [pid {pid}]"
    return text


def _stamp(t: float) -> str:
    whole = time.localtime(t)
    return time.strftime("%Y-%m-%d %H:%M:%S", whole) + f".{int((t % 1) * 1000):03d}"


def _header() -> str:
    import knoxlog
    return (
        "=" * 78 + "\n"
        f"KnoxMap {knoxlog.version()} debug profiler\n"
        f"started {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"log {_log}\n"
        f"pid {os.getpid()}\n"
        "Times are milliseconds. self is time in that function, including\n"
        "built-ins it calls and not other Python functions. total is from the\n"
        "call until it returns. A call of 50 ms or more is written as it\n"
        "happens. Each snapshot lists the functions with the most total time.\n"
        "Worker processes add every KnoxMap function and their heaviest\n"
        "library functions.\n"
        + "=" * 78 + "\n"
    )


def _append_text(text: str) -> None:
    global _header_done
    if not _leader or not text or _log is None:
        return
    with _file_lock:
        try:
            _rotate_unlocked()
            blob = text
            if not _header_done:
                blob = _header() + text
                _header_done = True
            _log.parent.mkdir(parents=True, exist_ok=True)
            with open(_log, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(blob)
                handle.flush()
        except OSError:
            pass


def _rotate_unlocked() -> None:
    global _header_done
    try:
        if _log is None or not _log.is_file() or _log.stat().st_size <= MAX_LOG:
            return
        backup = _log.with_name("debug-log.1.log")
        if backup.exists():
            backup.unlink()
        _log.replace(backup)
        _header_done = False
    except OSError:
        return


def _clamp(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return 1
    if number > 1000:
        return 1000
    return number
