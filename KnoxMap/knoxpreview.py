"""One thread paints every live preview.

The picture is a convenience. Map drawing and building never wait for it,
and they do not composite their own. This thread does not take a lock while
it paints, so a slow picture cannot hold up generation.

Coordinate mods are visited in row, then column order. A mod is painted only
when a newer picture is waiting, and the same mod is not painted again until
five seconds have passed.
"""
from __future__ import annotations

import os
import re
import threading
import time

# From the moment a mod starts painting until it may start again.
_SAME_MOD_GAP = 5.0
# parent__r2_c5, or parent__r2_c5:layout. A file path does not match.
_MOD_AT = re.compile(r"^(.*)__r(\d+)_c(\d+)(?::([^/\\]*))?$")

_lock = threading.Lock()
_jobs: dict[str, tuple] = {}
# Last time a coordinate mod, or any other picture, started painting.
_started: dict[object, float] = {}
# When a saved picture and a live mod are both due, take turns so the file
# is written and the mods still go in coordinate order.
_last_was_mod = False
_wake = threading.Event()
_thread: threading.Thread | None = None


def submit(key: str, build, publish=None) -> None:
    """Queue a picture. Returns at once. `build` runs on the preview thread.

    A newer request for the same picture replaces one that has not started.
    """
    with _lock:
        _jobs[key] = (build, publish)
        _wake.set()
        _ensure()


def discard(key: str) -> None:
    """Drop a picture that has not started. One already painting finishes."""
    with _lock:
        _jobs.pop(key, None)


def _ensure() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="knox-preview", daemon=True)
    _thread.start()


def _mod_place(key: str) -> tuple | None:
    """(row, col, prefix, suffix) for a coordinate mod, else None."""
    match = _MOD_AT.match(key)
    if match is None:
        return None
    prefix, row, col, suffix = match.groups()
    return (int(row), int(col), prefix, suffix or "")


def _take_job(key: str, ident) -> tuple:
    """Pop `key` and remember that it has started. The caller holds `_lock`."""
    global _last_was_mod
    _started[ident] = time.monotonic()
    _last_was_mod = not isinstance(ident, str)
    job = _jobs.pop(key)
    if not _jobs:
        _wake.clear()
    return ("run", key, job)


def _pick(now: float):
    """The next picture, or how long to wait before one is due.

    The caller holds nothing. The queue lock is not held across the paint.
    """
    with _lock:
        if not _jobs:
            _wake.clear()
            return None
        due_mod = None
        soonest = None
        others = []
        for key in _jobs:
            place = _mod_place(key)
            if place is None:
                others.append(key)
                continue
            row, col, prefix, suffix = place
            last = _started.get((prefix, row, col), 0.0)
            remain = _SAME_MOD_GAP - (now - last)
            if remain > 0:
                if soonest is None or remain < soonest:
                    soonest = remain
                continue
            rank = (row, col, prefix, suffix, key)
            if due_mod is None or rank < due_mod[0]:
                due_mod = (rank, key, (prefix, row, col))
        due_other = None
        for key in others:
            last = _started.get(key, 0.0)
            remain = _SAME_MOD_GAP - (now - last)
            if remain > 0:
                if soonest is None or remain < soonest:
                    soonest = remain
                continue
            if due_other is None or key < due_other:
                due_other = key
        # A live mod and a saved picture both ready: alternate. Mods stay in
        # row, column order against each other.
        if due_mod is not None and (due_other is None or not _last_was_mod):
            _rank, key, ident = due_mod
            return _take_job(key, ident)
        if due_other is not None:
            return _take_job(due_other, due_other)
        _wake.clear()
        return ("wait", soonest if soonest is not None else _SAME_MOD_GAP)


def _loop() -> None:
    _lower_priority()
    while True:
        _wake.wait()
        while True:
            picked = _pick(time.monotonic())
            if picked is None:
                break
            if picked[0] == "wait":
                # A new picture sets the event and cuts this wait short.
                _wake.wait(picked[1])
                continue
            _key, job = picked[1], picked[2]
            build, publish = job
            # Map threads are inside the interpreter between calls. Give them
            # that gap before and after the picture, which is the lower priority.
            time.sleep(0)
            try:
                frame = build()
            except Exception:
                _warn("preview failed")
                continue
            if frame is None or publish is None:
                continue
            try:
                publish(frame)
            except Exception:
                _warn("preview could not be shown")


def _warn(message: str) -> None:
    try:
        import knoxlog
        knoxlog.log.warning(message, exc_info=True)
    except Exception:
        return


def _lower_priority() -> None:
    """Prefer map work when both want the machine."""
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.GetCurrentThread()
            # THREAD_PRIORITY_BELOW_NORMAL
            ctypes.windll.kernel32.SetThreadPriority(handle, -1)
        else:
            os.nice(10)
    except (AttributeError, OSError):
        return
