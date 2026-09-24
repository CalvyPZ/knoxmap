"""Compile a Knoxify project to .lot files, a batch of cells at a time.

    python tools/compile_map.py output/mytown [--batch 4]

WorldEd's lot export never releases what it loads: every cell adds to the map
and tileset caches, so one process compiling a whole town climbs without limit.
A 572-cell map reached 13.7 GB and had done under a sixth of the work before the
machine started thrashing.

So the work is handed over in batches, each to a fresh PZWorldEd_cli process
which exits and gives the memory back. Output accumulates in the same lots
folder, because each batch only generates the cells it was given.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import knoxlog  # noqa: E402
import knoxpaths  # noqa: E402
import knoxstop  # noqa: E402


BATCH_TIMEOUT = 2 * 3600
# How often a running batch is asked whether the window wants it stopped.
STOP_POLL_SECONDS = 1.0


# Off Windows the tools are started by `wine`, which is a launcher: it hands
# the program to wineserver and the program is not its child. Killing the
# launcher leaves WorldEd running, so a batch is given a process group of its
# own and the whole group is ended together.
_OWN_GROUP = os.name != "nt"


def _end_batch(proc) -> None:
    """End a batch, and anything it started with it. Never waits for ever."""
    def send(hard: bool) -> None:
        try:
            if _OWN_GROUP:
                os.killpg(os.getpgid(proc.pid),
                          signal.SIGKILL if hard else signal.SIGTERM)
            elif hard:
                proc.kill()
            else:
                proc.terminate()
        except OSError:            # already gone, or not ours to signal
            pass

    for hard, grace in ((False, 20), (True, 10)):
        send(hard)
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_batch(cmd, should_stop, started: float):
    """Run one WorldEd batch, watching for a stop while it works.

    subprocess.run waits for the process and nothing else, so a compile could
    only be stopped between batches - and one batch of a big map is minutes.
    This waits in short steps instead, and when the window asks it to stop it
    closes WorldEd down and raises.

    WorldEd's output goes to files rather than pipes, and what is waited for
    is the process, not the output. Under Wine the pipes are inherited by
    wineserver, which outlives everything it runs: after WorldEd died the
    pipes stayed open, so reading them to the end never ended. The window sat
    on "compiling" with nothing running at all, and Stop could not get out of
    it either, because closing the batch down read those same pipes. Waiting
    on the process alone cannot get stuck that way, on any system.
    """
    def scratch():
        return tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace")

    with scratch() as out_f, scratch() as err_f:
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f,
                                env=knoxpaths.tool_env(),
                                **({"start_new_session": True} if _OWN_GROUP else {}))

        def said() -> tuple[str, str]:
            """Whatever WorldEd wrote, however it ended."""
            texts = []
            for handle in (out_f, err_f):
                try:
                    handle.seek(0)
                    texts.append(handle.read())
                except OSError:
                    texts.append("")
            return texts[0], texts[1]

        while True:
            try:
                proc.wait(timeout=STOP_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
            else:
                out, err = said()
                return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
            if should_stop is not None and should_stop():
                _end_batch(proc)
                raise knoxstop.Stopped("the compile")
            if time.time() - started > BATCH_TIMEOUT:
                _end_batch(proc)
                raise subprocess.TimeoutExpired(cmd, BATCH_TIMEOUT,
                                                output=said()[0], stderr=said()[1])


DEFAULT_EXE = knoxpaths.worlded_cli() or Path("PZWorldEd_cli.exe")


def world_size(pzw: Path) -> tuple[int, int]:
    text = pzw.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<world version="[^"]*" width="(\d+)" height="(\d+)"', text)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _tool_path(path: Path) -> str:
    """A file as the map tools name it: forward slashes on Windows, and
    otherwise what the compiler in use can open - its own name for a native
    build, a Wine name for the Windows one."""
    if os.name == "nt":
        return path.as_posix()
    return knoxpaths.tool_path(path)


def assign_converted_maps(pzw: Path) -> int:
    """Point every cell that has a converted .tmx at it. Returns how many.

    Each batch is a fresh WorldEd process reading the project from disk. The
    first one converts the whole bitmap to .tmx maps, but it never saves which
    map belongs to which cell, so every later batch found map="" everywhere
    and converted the whole town again before compiling its own few cells.
    Writing the assignments back after a batch lets the rest skip straight to
    compiling.
    """
    text = pzw.read_text(encoding="utf-8", errors="replace")
    origin = re.search(r'<worldOrigin origin="(-?\d+),(-?\d+)"', text)
    bmp = re.search(r'<bmp path="([^"]+)"', text)
    if not (origin and bmp):
        return 0
    ox, oy = int(origin.group(1)), int(origin.group(2))
    base = Path(bmp.group(1)).stem
    # The converted maps sit in the project's own tmx folder. The path the
    # .pzw carries is the one the tools read, which under Wine is a Z: name
    # that means nothing here - so the file is looked for by its real name
    # and written back by the tools' one (knoxbuild/world.py _tool_path).
    folder = pzw.parent / "tmx"
    count = 0

    def fill(m):
        nonlocal count
        x, y = int(m.group(1)), int(m.group(2))
        path = folder / f"{base}_{ox + x}_{oy + y}.tmx"
        if not path.exists():
            return m.group(0)
        count += 1
        return f'<cell x="{x}" y="{y}" map="{_tool_path(path)}"'

    new = re.sub(r'<cell x="(\d+)" y="(\d+)" map=""', fill, text)
    if count:
        pzw.write_text(new, encoding="utf-8")
    return count


def clear_stale(project: Path) -> None:
    """Delete compiled output that is older than what it was compiled from.

    The patched CLI skips a batch whose cells all have a .lotheader, and skips
    converting a cell that already has a .tmx, so that a stopped compile picks
    up where it left off. That also meant a map rebuilt with new buildings or
    terrain "compiled" in seconds and kept every old lot. Output newer than
    its inputs is kept, so resuming still works.

    The .pzw is not an input here: assign_converted_maps rewrites it after the
    first batch, which would make every finished compile look stale.
    """
    def newest(paths) -> float:
        return max((p.stat().st_mtime for p in paths), default=0.0)

    lots, tmx = project / "lots", project / "tmx"
    terrain = newest(project.glob("*.bmp"))
    # The rules are baked in when a bitmap is converted, so new rules (Setup
    # adding street furniture, say) need the conversion done again too.
    settings_time = 0.0
    tools = knoxpaths.mapping_tools_dir()
    if tools:
        terrain = max(terrain, newest(p for p in (tools / "config" / "Rules.txt",
                                                  tools / "config" / "Blends.txt")
                                      if p.exists()))
        # Editor settings (the game folder, whose tile definitions shape every
        # window opening) change what the lots come out as.
        ini = tools / "settings" / "PZTools.ini"
        if ini.exists():
            settings_time = ini.stat().st_mtime
    inputs = max(terrain, newest((project / "buildings").glob("*.tbx")),
                 settings_time)
    written = [p for p in lots.glob("*") if p.is_file()] if lots.is_dir() else []
    if written and min(p.stat().st_mtime for p in written) < inputs:
        for p in written:
            p.unlink()
    converted = list(tmx.glob("*.tmx")) if tmx.is_dir() else []
    if converted and min(p.stat().st_mtime for p in converted) < terrain:
        for p in converted:
            p.unlink()
        # And forget them, or WorldEd stops on "missing assigned TMX".
        pzw = project / f"{project.name}.pzw"
        if pzw.exists():
            text = pzw.read_text(encoding="utf-8", errors="replace")
            unassigned = re.sub(r'(<cell x="\d+" y="\d+" map=")[^"]*\.tmx"', r'\1"', text)
            pzw.write_text(unassigned, encoding="utf-8")


# A batch that fails is tried again before the compile gives up on it. Most
# of what goes wrong in WorldEd's lot export goes wrong once - a texture read
# that returned short, a file still held open - and the second run of the same
# cells comes out clean. Three attempts, because a fourth has never helped.
#
# Nothing is deleted between attempts. The patched CLI skips a batch only when
# every one of its cells already has a .lotheader, so a batch that died
# part-way still has cells pending and is redone; and the lot files are named
# in 256-tile cells against the world origin, not the 300-tile cells a batch
# is given, so working out which files belong to a batch is a good way to
# delete somebody else's finished work.
BATCH_ATTEMPTS = 3

# What a compile leaves behind about the cells it could not do, for the
# window to offer and for a later run to pick up.
FAILURES_FILE = "compile_failures.json"

# One compile of a project at a time. The window already refuses a second
# while one is running, but the command line does not know about the window,
# and two compiles of one project write the same lots folder and the same
# .pzw: assign_converted_maps rewrites it after every batch.
LOCK_FILE = ".compiling"


def _pid_alive(pid: int) -> bool:
    """Whether a process is still running.

    Only used to tell a compile that is still going from a lock left behind
    by one that crashed, so "cannot tell" counts as alive: refusing to start
    is recoverable and two compiles in one folder are not.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False          # gone, or never ours to look at
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True               # somebody else's, but running
    except OSError:
        return True
    return True


@contextlib.contextmanager
def _only_one(project: Path, run: str):
    """Hold this project's compile lock for the length of a run."""
    path = project / LOCK_FILE
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        held = None
    if isinstance(held, dict):
        pid = int(held.get("pid") or 0)
        if pid != os.getpid() and _pid_alive(pid):
            raise RuntimeError(
                f"{project.name} is already being compiled (run {held.get('run')}, "
                f"process {pid}). Wait for that to finish, or stop it, rather than "
                f"running two - they write the same files.")
        knoxlog.log.warning("compile %s [%s]: cleared a lock left by run %s (process "
                            "%s is gone)", project.name, run, held.get("run"), pid)
    try:
        path.write_text(json.dumps({"pid": os.getpid(), "run": run,
                                    "started": time.time()}), encoding="utf-8")
    except OSError:
        pass                      # a lock that cannot be written is not a reason to stop
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def failed_cells(project_dir: str | Path) -> list[dict]:
    """The batches the last compile of this project could not do.

    Each is {"cells": [x0, y0, x1, y1], "exit": int, "why": str, "log": str}.
    Empty when the last compile did the lot, which is the usual answer.
    """
    try:
        with open(Path(project_dir) / FAILURES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    found = data.get("failed") if isinstance(data, dict) else None
    return [f for f in found if isinstance(f, dict)] if isinstance(found, list) else []


def _record_failures(project: Path, run: str, batch: int, failures: list[dict]) -> None:
    path = project / FAILURES_FILE
    if not failures:
        try:
            path.unlink()         # a clean run leaves nothing behind
        except OSError:
            pass
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"run": run, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "batch": batch, "failed": failures}, f, indent=2)
    except OSError:
        knoxlog.log.warning("compile %s [%s]: could not write %s",
                            project.name, run, FAILURES_FILE)


def _why(proc) -> str:
    """The lines of a batch's output that say what went wrong, rather than
    the last three, which after a crash are thread shutdown chatter."""
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    said = [ln for ln in lines
            if re.search(r"CRITICAL|ERROR|FATAL|Could not|failed", ln)]
    return " | ".join((said or lines)[-3:])


def compile_map(project_dir: str, batch: int = 4, exe: str | None = None,
                on_progress=None, should_stop=None,
                only_cells: list | None = None) -> int:
    """Run every batch. Returns the number of compiled cells.

    A batch that fails is tried again (BATCH_ATTEMPTS) and, if it still will
    not go, written down and stepped over: half a day's compiling should not
    be thrown away because batch 23 of 48 hit a bad texture read. What could
    not be done ends up in compile_failures.json and is offered again, and
    `only_cells` - a list of [x0, y0, x1, y1] - compiles just those.

    Some failures are not worth a second attempt because they are about this
    machine rather than this batch: a Qt that cannot start fails every batch
    in exactly the same way, and forty-eight batches of it is hours of
    nothing. Those stop the run at once, with what to do about it.
    """
    project = Path(project_dir).resolve()
    pzw = project / f"{project.name}.pzw"
    if not pzw.exists():
        raise FileNotFoundError(f"No {pzw.name} — generate the buildings first.")
    exe_path = Path(exe) if exe else DEFAULT_EXE
    if not exe_path.exists():
        raise FileNotFoundError(f"PZWorldEd_cli not found at {exe_path}")

    # Every line of this run's log carries it, so two runs in one log file can
    # be told apart - which is the first question to ask of any compile that
    # looks like it did its batches out of order.
    run = uuid.uuid4().hex[:8]

    with _only_one(project, run):
        clear_stale(project)
        # Whatever wrote the project, one entry past the edge of the map must not
        # stop the whole compile: WorldEd refuses a project over a single one.
        from knoxbuild.repair import repair_project
        try:
            fixed = repair_project(pzw)
        except Exception:  # noqa: BLE001 - a repair that fails leaves the project as it was
            knoxlog.log.exception("compile %s: checking the project failed", project.name)
        else:
            if fixed["changed"]:
                knoxlog.log.warning("compile %s: repaired the project before compiling - moved %d, "
                                    "dropped %d%s", project.name, fixed["moved"],
                                    len(fixed["dropped"]),
                                    "".join(f"\n  dropped {what}: {why}"
                                            for what, why in fixed["dropped"][:50]))
        lots = project / "lots"
        lots.mkdir(exist_ok=True)
        (project / "tmx").mkdir(exist_ok=True)

        w, h = world_size(pzw)
        if not w or not h:
            raise ValueError(f"Could not read the world size from {pzw.name}")

        if only_cells:
            batches = [tuple(int(v) for v in cells[:4]) for cells in only_cells]
        else:
            batches = [(x, y, min(x + batch - 1, w - 1), min(y + batch - 1, h - 1))
                       for y in range(0, h, batch) for x in range(0, w, batch)]
        started = time.time()
        failures: list[dict] = []
        # Batches are done one at a time, in this order, and the counter says
        # so: this has always been a plain loop, but a compile that looked
        # like it jumped from 25 to 18 is worth being able to rule out.
        previous = 0
        for i, (bx, by, x1, y1) in enumerate(batches, start=1):
            if i != previous + 1:
                raise RuntimeError(f"compile {project.name}: batch {i} followed {previous} "
                                   f"- the batches are not being done in order")
            previous = i
            cmd = knoxpaths.command_for(exe_path) + [
                f"--generate-map={knoxpaths.tool_path(pzw)}",
                f"--cells={bx},{by},{x1},{y1}"]
            for attempt in range(1, BATCH_ATTEMPTS + 1):
                knoxstop.check(should_stop, "the compile")
                attempt_started = time.time()
                proc = _run_batch(cmd, should_stop, attempt_started)
                # WorldEd's own account of the batch, kept whatever happened: when it
                # crashes this is the only record of how far it got.
                saved = knoxlog.save_tool_output("PZWorldEd_cli", project.name,
                                                 f"cells_{bx}_{by}-{x1}_{y1}", proc.returncode,
                                                 proc.stdout, proc.stderr)
                again = f" (attempt {attempt}/{BATCH_ATTEMPTS})" if attempt > 1 else ""
                knoxlog.log.info("compile %s [%s]: batch %d/%d cells %d,%d..%d,%d exit %d "
                                 "(%s) in %.0fs%s", project.name, run, i, len(batches),
                                 bx, by, x1, y1, proc.returncode,
                                 knoxlog.explain_exit(proc.returncode),
                                 time.time() - attempt_started, again)
                if proc.returncode == 0:
                    break
                # Not this batch's fault, and every other batch would go the
                # same way: stop now and say what to do about it.
                trouble = knoxpaths.qt_trouble((proc.stderr or "") + (proc.stdout or ""))
                if trouble:
                    raise RuntimeError(f"Compile cannot run on this system: {trouble}.")
                if attempt < BATCH_ATTEMPTS:
                    knoxlog.log.warning("compile %s [%s]: batch %d/%d failed, trying again",
                                        project.name, run, i, len(batches))
            assign_converted_maps(pzw)
            cells = len(list(lots.glob("*.lotheader")))
            # 65 used to be tolerated because the wait for WorldEd was a guess.
            # It now waits for the lot manager's own completion, so 65 means a
            # genuine stall or timeout and the batch's cells cannot be trusted.
            if proc.returncode != 0:
                where = f"logs/worlded/{saved.name}" if saved else ""
                failures.append({"cells": [bx, by, x1, y1], "exit": proc.returncode,
                                 "why": _why(proc), "log": where,
                                 "attempts": BATCH_ATTEMPTS})
                knoxlog.log.error("compile %s [%s]: giving up on batch %d/%d cells "
                                  "%d,%d..%d,%d after %d attempts - carrying on with the "
                                  "rest", project.name, run, i, len(batches),
                                  bx, by, x1, y1, BATCH_ATTEMPTS)
            if on_progress:
                on_progress(i, len(batches), cells)
            else:
                elapsed = time.time() - started
                state = "FAILED" if proc.returncode != 0 else f"-> {cells} compiled"
                print(f"  batch {i}/{len(batches)} cells {bx},{by}..{x1},{y1} "
                      f"{state}  ({elapsed:.0f}s)", flush=True)

        # Asked for particular cells, the batches that were not in this run
        # keep whatever the last full compile said about them - retrying two
        # of five failures must not lose the other three.
        this_run = list(failures)
        if only_cells:
            done = {tuple(b) for b in batches}
            failures = [f for f in failed_cells(project)
                        if tuple(f.get("cells") or ()) not in done] + failures
        _record_failures(project, run, batch, failures)
        if this_run and len(this_run) >= len(batches):
            first = this_run[0]
            where = f" - WorldEd's output is in {first['log']}" if first.get("log") else ""
            at = (f"{first['cells'][0]},{first['cells'][1]}.."
                  f"{first['cells'][2]},{first['cells'][3]}")
            if only_cells:
                raise RuntimeError(f"Those cells still will not compile. {at} "
                                   f"(exit {first['exit']}): {first['why']}{where}")
            raise RuntimeError(f"WorldEd failed on every batch. The first was cells "
                               f"{at} (exit {first['exit']}): {first['why']}{where}")
        if failures:
            knoxlog.log.warning("compile %s [%s]: finished with %d of %d batches failed",
                                project.name, run, len(failures), len(batches))
        return len(list(lots.glob("*.lotheader")))


def main(argv: list[str] | None = None) -> int:
    # Place names can be in any script, and a Windows console using a legacy
    # code page cannot print most of them - "OSM says Kadıköy" crashed a build
    # on cp1252. Print what it can and mark the rest, rather than dying.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project_dir")
    ap.add_argument("--batch", type=int, default=4,
                    help="source cells per side per batch (default 4)")
    ap.add_argument("--exe", default=None)
    args = ap.parse_args(argv)
    try:
        total = compile_map(args.project_dir, args.batch, args.exe)
    except Exception as exc:
        print(f"Compile failed: {exc}", file=sys.stderr)
        return 1
    print(f"compiled {total} cells into {args.project_dir}/lots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
