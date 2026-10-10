"""Compile a Knoxify project to .lot files with knox-compile.

    python tools/compile_map.py output/mytown
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
import threading
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


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class _HiddenDesktopProcess:
        """Runs PZWorldEd on an isolated Windows desktop.

        Qt GUI windows and secondary dialogs (e.g. 'Generate Lots' popups)
        belong to this hidden desktop and are physically impossible for DWM
        to composite onto the user's display, guaranteeing 0 ms visibility
        and zero flashes without any polling overhead.
        
        A background thread proxies error popups (or long-hanging dialogs)
        to the user's default desktop so they can be dismissed.
        """
        def __init__(self, cmd, stdout_f, stderr_f, env=None):
            self.cmd = cmd
            # One desktop per process. CreateDesktop fails if the name already
            # exists, and several mods compile at once, each in its own WorldEd.
            self._desk_name = f"KnoxCompile_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            self._hdesk = ctypes.windll.user32.CreateDesktopW(self._desk_name, None, None, 0, 0x01FF, None)
            if not self._hdesk:
                err = ctypes.GetLastError()
                raise OSError(f"CreateDesktopW failed: {err}")

            h_out = msvcrt.get_osfhandle(stdout_f.fileno())
            h_err = msvcrt.get_osfhandle(stderr_f.fileno())
            ctypes.windll.kernel32.SetHandleInformation(h_out, 1, 1)
            ctypes.windll.kernel32.SetHandleInformation(h_err, 1, 1)

            si = _STARTUPINFOW()
            si.cb = ctypes.sizeof(_STARTUPINFOW)
            si.lpDesktop = self._desk_name
            si.dwFlags = 0x00000100 | 0x00000001  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            si.hStdInput = ctypes.windll.kernel32.GetStdHandle(-10)
            si.hStdOutput = h_out
            si.hStdError = h_err

            pi = _PROCESS_INFORMATION()
            flags = 0
            env_buf = None
            if env:
                flags |= 0x00000400  # CREATE_UNICODE_ENVIRONMENT
                env_str = "".join(f"{k}={v}\0" for k, v in sorted(env.items())) + "\0"
                env_buf = ctypes.create_unicode_buffer(env_str)

            cmd_line = subprocess.list2cmdline([str(c) for c in cmd]) if isinstance(cmd, (list, tuple)) else str(cmd)
            ok = ctypes.windll.kernel32.CreateProcessW(
                None, cmd_line, None, None, True, flags, env_buf, None,
                ctypes.byref(si), ctypes.byref(pi)
            )
            if not ok:
                err = ctypes.GetLastError()
                raise OSError(f"CreateProcessW failed: {err}")

            self._hProcess = pi.hProcess
            self._hThread = pi.hThread
            self.pid = pi.dwProcessId
            self.returncode = None
            
            # Start error popup proxy thread
            self._monitor_thread = __import__("threading").Thread(target=self._monitor_popups, daemon=True)
            self._monitor_thread.start()

        def _monitor_popups(self):
            seen_times = {}
            proxied = set()
            
            EnumDesktopWindows = ctypes.windll.user32.EnumDesktopWindows
            EnumDesktopWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            
            def enum_proc(hwnd, lParam):
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                if hwnd in proxied:
                    return True
                    
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                
                now = time.time()
                if hwnd not in seen_times:
                    seen_times[hwnd] = now
                    
                elapsed = now - seen_times[hwnd]
                title_lower = title.lower()
                
                is_error = "error" in title_lower or "warning" in title_lower or "exception" in title_lower
                is_timeout = elapsed > 60.0
                
                if is_error or is_timeout:
                    proxied.add(hwnd)
                    msg = (f"PZWorldEd popup detected.\n\n"
                           f"Title: {title}\n"
                           f"Reason: {'Error keyword in title' if is_error else 'Open for > 1 minute'}\n\n"
                           f"Click OK to dismiss it and continue.")
                    # Show on default desktop
                    ctypes.windll.user32.MessageBoxW(0, msg, "PZWorldEd error plausible", 0)
                    # Close the hidden window
                    ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0) # WM_CLOSE
                    
                return True
                
            c_enum_proc = EnumDesktopWindowsProc(enum_proc)
            
            while self._hdesk is not None and self.returncode is None:
                # EnumDesktopWindows blocks until all windows are enumerated
                try:
                    EnumDesktopWindows(self._hdesk, c_enum_proc, 0)
                except Exception:
                    pass
                time.sleep(1.0)

        def poll(self):
            if self.returncode is not None:
                return self.returncode
            code = wintypes.DWORD()
            if ctypes.windll.kernel32.GetExitCodeProcess(self._hProcess, ctypes.byref(code)):
                if code.value == 259:  # STILL_ACTIVE
                    return None
                self.returncode = code.value
                self._cleanup()
                return self.returncode
            return None

        def wait(self, timeout=None):
            if self.returncode is not None:
                return self.returncode
            ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
            res = ctypes.windll.kernel32.WaitForSingleObject(self._hProcess, ms)
            if res == 0:  # WAIT_OBJECT_0
                return self.poll()
            elif res == 0x00000102:  # WAIT_TIMEOUT
                raise subprocess.TimeoutExpired(self.cmd, timeout)
            return self.poll()

        def terminate(self):
            if self._hProcess:
                ctypes.windll.kernel32.TerminateProcess(self._hProcess, 1)

        def kill(self):
            self.terminate()

        def _cleanup(self):
            if self._hThread:
                try:
                    ctypes.windll.kernel32.CloseHandle(self._hThread)
                except Exception:
                    pass
                self._hThread = None
            if self._hProcess:
                try:
                    ctypes.windll.kernel32.CloseHandle(self._hProcess)
                except Exception:
                    pass
                self._hProcess = None
            if self._hdesk:
                try:
                    ctypes.windll.user32.CloseDesktop(self._hdesk)
                except Exception:
                    pass
                self._hdesk = None

        def __del__(self):
            self._cleanup()


def _run_batch(cmd, should_stop, started: float, on_tick=None):
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
        if sys.platform == "win32":
            proc = _HiddenDesktopProcess(cmd, out_f, err_f, env=knoxpaths.tool_env())
        else:
            kwargs = {}
            if _OWN_GROUP:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, stdout=out_f, stderr=err_f,
                                    env=knoxpaths.tool_env(),
                                    **kwargs)

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
            if on_tick is not None:
                try:
                    on_tick()
                except Exception:  # noqa: BLE001 - a progress note must not stop the batch
                    knoxlog.log.exception("compile: progress note failed")
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


def project_cells(pzw: Path) -> set[tuple[int, int]]:
    from knoxbuild.world import project_cells as _cells

    return _cells(pzw)


def _tool_path(path: Path) -> str:
    """A file as the map tools name it: forward slashes on Windows, and
    otherwise what the compiler in use can open - its own name for a native
    build, a Wine name for the Windows one."""
    if os.name == "nt":
        return path.as_posix()
    return knoxpaths.tool_path(path)


# Cells worth compiling, written beside the project when the buildings are
# generated. Later batches add empty cells that only point at a converted
# map; those must not widen the next compile to the whole rectangle.
COMPILE_CELLS = "compile_cells.json"


def compile_cells(project: Path, pzw: Path) -> set[tuple[int, int]]:
    """The cells this compile should cover, not the empty map pointers."""
    path = project / COMPILE_CELLS
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found = set()
            for pair in data.get("cells") or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    found.add((int(pair[0]), int(pair[1])))
            if found:
                return found
        except (OSError, ValueError, TypeError):
            pass
    found = project_cells(pzw)
    if found:
        try:
            path.write_text(json.dumps({"cells": [list(cell) for cell in sorted(found)]}),
                            encoding="utf-8")
        except OSError:
            pass
    return found


def batch_side(requested: int = 4) -> int:
    """How many source cells on a side one WorldEd process should take.

    Starting the editor is most of a small batch: it reloads Qt and the
    tilesets before it compiles anything. A wider batch is fewer startups.
    The map cache grows with the cells that process holds, and one process
    given a whole town climbed past 13 GB, so eight on a side is the top.
    """
    requested = max(1, min(8, int(requested)))
    status = knoxlog.memory_status()
    if not status:
        return requested
    usable = min(int(status[1]), int(status[2]))
    per = 150 * 1024 * 1024
    reserve = 6 * 1024 * 1024 * 1024
    side = requested
    for wider in (6, 8):
        if usable - reserve >= wider * wider * per:
            side = max(side, wider)
    return side


def assign_converted_maps(pzw: Path) -> int:
    """Point every cell that has a converted .tmx at it. Returns how many.

    Each batch is a fresh WorldEd process reading the project from disk. The
    first one converts the whole bitmap to .tmx maps, but it never saves which
    map belongs to which cell, so every later batch found map="" everywhere
    and converted the whole town again before compiling its own few cells.
    Writing the assignments back after a batch lets the rest skip straight to
    compiling.

    Cells the game can fill in are left out of the project. WorldEd still has
    them, with an empty map, and it skips conversion only when every cell has
    one. One omitted cell made every later batch convert the whole town again.
    Once a converted map is on disk, that cell is recorded too.
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
    size = re.search(r'<world version="[^"]*" width="(\d+)" height="(\d+)"', new)
    stubs = []
    if size and folder.is_dir():
        width, height = int(size.group(1)), int(size.group(2))
        present = {(int(x), int(y))
                   for x, y in re.findall(r'<cell x="(-?\d+)" y="(-?\d+)"', new)}
        for cy in range(height):
            for cx in range(width):
                if (cx, cy) in present:
                    continue
                path = folder / f"{base}_{ox + cx}_{oy + cy}.tmx"
                if not path.exists():
                    continue
                stubs.append(
                    f' <cell x="{cx}" y="{cy}" map="{_tool_path(path)}"/>')
        if stubs:
            new = new.replace("</world>", "\n".join(stubs) + "\n</world>", 1)
            count += len(stubs)
            knoxlog.log.info(
                "compile %s: recorded %d converted cells so later batches "
                "skip BMP to TMX", pzw.parent.name, len(stubs))
    if count:
        pzw.write_text(new, encoding="utf-8")
    return count


def _batch_lot_files(project: Path, pzw: Path, bx: int, by: int,
                     x1: int, y1: int) -> list[Path] | None:
    """The .lotheader files this batch owns. None when the origin cannot be read.

    Shared edges belong to two batches, so the rectangle is this batch's own.
    """
    try:
        text = pzw.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    origin = re.search(r'<worldOrigin origin="(-?\d+),(-?\d+)"', text)
    if not origin:
        return None
    lots = project / "lots"
    ox, oy = int(origin.group(1)), int(origin.group(2))

    def span(origin_cell: int, c0: int, c1: int) -> range:
        tile0 = (origin_cell + c0) * 300
        tile1 = (origin_cell + c1 + 1) * 300
        return range(tile0 // 256, (tile1 - 1) // 256 + 1)

    return [lots / f"{ix}_{iy}.lotheader"
            for ix in span(ox, bx, x1)
            for iy in span(oy, by, y1)]


def _batch_written(project: Path, pzw: Path, bx: int, by: int,
                   x1: int, y1: int) -> bool:
    """Whether this batch's 256-tile lot cells are already on disk.

    A stopped compile used to start WorldEd again for every finished batch
    just to be told to skip it. Shared edges belong to two batches, so a
    batch is done only when every cell of its own rectangle is there.
    """
    files = _batch_lot_files(project, pzw, bx, by, x1, y1)
    if not files or not (project / "lots").is_dir():
        return False
    return all(path.is_file() for path in files)


def _conversion_done(pzw: Path) -> bool:
    """Whether BMP to TMX has already pointed every cell at a map.

    The compiler skips that step when it has, and goes straight to lots.
    """
    try:
        text = pzw.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    maps = re.findall(r'<cell x="\d+" y="\d+" map="([^"]*)"', text)
    return bool(maps) and all(item.endswith(".tmx") for item in maps)


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


def knox_compile_binary() -> Path | None:
    """knox-compile, beside a packed program or under compiler/target."""
    env = os.environ.get("KNOX_COMPILE", "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
    names = ("knox-compile.exe", "knox-compile") if sys.platform == "win32" else (
        "knox-compile", "knox-compile.exe")
    places: list[Path] = []
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        places.append(Path(frozen))
    places.append(Path(sys.executable).resolve().parent)
    here = Path(__file__).resolve()
    # Packed copies of this tree (the Mac program) keep the binary beside the
    # copy. A source checkout is one folder further up, under compiler/target.
    if len(here.parents) > 1:
        places.append(here.parents[1])
    if len(here.parents) > 2:
        built = here.parents[2] / "compiler" / "target"
        places.append(built / "release")
        places.append(built / "debug")
    seen: set[str] = set()
    for folder in places:
        key = str(folder)
        if key in seen:
            continue
        seen.add(key)
        for name in names:
            path = folder / name
            if path.is_file():
                return path
    return None


def native_jobs() -> int:
    """How many cells knox-compile may hold at once.

    The same memory cap the WorldEd batches used: 150 MB a cell, 6 GB left
    for the rest of the machine, and never more threads than the CPU has.
    """
    cpus = os.cpu_count() or 1
    status = knoxlog.memory_status()
    if not status:
        return max(1, cpus)
    usable = min(int(status[1]), int(status[2]))
    per = 150 * 1024 * 1024
    reserve = 6 * 1024 * 1024 * 1024
    room = max(1, (usable - reserve) // per)
    return max(1, min(cpus, int(room)))


class RunningCompile:
    """One knox-compile process.

    A held compile has already loaded the project and the tile catalog. It
    waits on stdin until release(), then compiles. stop() ends it either way.
    """

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.proc = None
        self.error: BaseException | None = None
        self.count = 0
        self.held = False

    def release(self) -> None:
        """Let a held compile start. A compile that was not held ignores this."""
        proc = self.proc
        if not self.held or proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write("\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def wait(self) -> int:
        self.finished.wait()
        if self.error is not None:
            raise self.error
        return self.count

    def stop(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            self.ready.set()
            return
        _end_batch(proc)


def start_compile(project_dir: str, on_progress=None, should_stop=None,
                  only_cells: list | None = None, hold: bool = False) -> RunningCompile:
    """Start knox-compile for one mod.

    hold=True loads the project and tile catalog, then waits. This returns
    once that load has finished, so the caller can keep it in memory until
    the mod already compiling is done. hold=False returns as soon as the
    process is running.
    """
    import queue
    import threading

    project = Path(project_dir).resolve()
    pzw = project / f"{project.name}.pzw"
    if not pzw.exists():
        raise FileNotFoundError(f"No {pzw.name} — generate the buildings first.")
    native = knox_compile_binary()
    if native is None:
        raise FileNotFoundError(
            "knox-compile was not found. Build it with cargo build --release "
            "--manifest-path compiler/Cargo.toml.")

    job = RunningCompile()
    job.held = hold
    run = uuid.uuid4().hex[:8]

    def _phase(done: int, total: int, phase: str, detail: str = "") -> None:
        if on_progress:
            on_progress(done, total, 0, False, phase=phase, detail=detail,
                        finished=False)

    def _drive() -> None:
        proc = None
        try:
            with _only_one(project, run):
                _phase(0, 0, "prepare", "Repairing the project")
                clear_stale(project)
                from knoxbuild.repair import repair_project
                try:
                    fixed = repair_project(pzw)
                except Exception:  # noqa: BLE001
                    knoxlog.log.exception(
                        "compile %s: checking the project failed", project.name)
                else:
                    if fixed["changed"]:
                        knoxlog.log.warning(
                            "compile %s: repaired the project before compiling - "
                            "moved %d, dropped %d%s", project.name, fixed["moved"],
                            len(fixed["dropped"]),
                            "".join(f"\n  dropped {what}: {why}"
                                    for what, why in fixed["dropped"][:50]))
                lots = project / "lots"
                lots.mkdir(exist_ok=True)
                (project / "tmx").mkdir(exist_ok=True)
                w, h = world_size(pzw)
                if not w or not h:
                    raise ValueError(f"Could not read the world size from {pzw.name}")
                jobs = native_jobs()
                cmd = [str(native), str(pzw), "--tmx", "--lots", f"--jobs={jobs}"]
                if hold:
                    cmd.append("--hold")
                tools = knoxpaths.mapping_tools_dir()
                if tools:
                    for flag, name in (("--rules", "Rules.txt"), ("--blends", "Blends.txt"),
                                       ("--map-base", "MapBaseXML.txt"),
                                       ("--tilesets", "Tilesets.txt")):
                        path = tools / "config" / name
                        if path.is_file():
                            cmd.extend([flag, str(path)])
                if only_cells:
                    for cells in only_cells:
                        x0, y0, x1, y1 = (int(v) for v in list(cells)[:4])
                        cmd.append(f"--cells={x0},{y0},{x1},{y1}")
                _phase(0, 1, "prepare", "Loading" if hold else "knox-compile")
                knoxlog.log.info("compile %s [%s]: knox-compile %s%s",
                                 project.name, run, native,
                                 " (held)" if hold else "")
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE if hold else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace")
                job.proc = proc
                lines: queue.Queue = queue.Queue()

                def _pump() -> None:
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            lines.put(line)
                    lines.put(None)

                err_box: list[str] = []

                def _pump_err() -> None:
                    if proc.stderr is not None:
                        err_box.append(proc.stderr.read())

                threading.Thread(target=_pump, daemon=True).start()
                err_thread = threading.Thread(target=_pump_err, daemon=True)
                err_thread.start()
                try:
                    while True:
                        if should_stop is not None and should_stop():
                            _end_batch(proc)
                            knoxstop.check(should_stop, "the compile")
                        try:
                            line = lines.get(timeout=STOP_POLL_SECONDS)
                        except queue.Empty:
                            continue
                        if line is None:
                            break
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        done = int(msg.get("done") or 0)
                        total = int(msg.get("total") or 0)
                        skipped = bool(msg.get("skipped"))
                        phase = str(msg.get("phase") or "")
                        detail = str(msg.get("detail") or "")
                        if phase == "ready":
                            job.ready.set()
                        if on_progress and (total or phase):
                            on_progress(done, total, done if phase == "lots" else 0,
                                        skipped, phase=phase, detail=detail,
                                        finished=phase not in ("prepare", "ready"))
                finally:
                    if proc.poll() is None:
                        _end_batch(proc)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        _end_batch(proc)
                err_thread.join(timeout=5)
                code = proc.returncode if proc.returncode is not None else 1
                err = "".join(err_box)
                if code != 0:
                    why = (err.strip().splitlines() or [f"exit {code}"])[-1]
                    if only_cells:
                        rects = [tuple(int(v) for v in list(cells)[:4])
                                 for cells in only_cells]
                    else:
                        rects = [(0, 0, max(0, w - 1), max(0, h - 1))]
                    failures = [{"cells": list(rect), "exit": code, "why": why,
                                 "log": "", "attempts": 1} for rect in rects]
                    _record_failures(project, run, jobs, failures)
                    raise RuntimeError(f"knox-compile failed (exit {code}): {why}")
                _record_failures(project, run, jobs, [])
                assign_converted_maps(pzw)
                job.count = len(list(lots.glob("*.lotheader"))) if lots.is_dir() else 0
        except BaseException as exc:
            job.error = exc
        finally:
            job.ready.set()
            job.finished.set()

    threading.Thread(target=_drive, name=f"compile-{project.name}", daemon=True).start()
    if hold:
        job.ready.wait()
    return job


def compile_map(project_dir: str, batch: int = 4, exe: str | None = None,
                on_progress=None, should_stop=None,
                only_cells: list | None = None) -> int:
    """Compile the project with knox-compile. Returns the number of cells.

    `only_cells` is a list of [x0, y0, x1, y1]. on_progress reports each step
    knox-compile prints. A call with finished=False only names the step.
    """
    job = start_compile(project_dir, on_progress=on_progress,
                        should_stop=should_stop, only_cells=only_cells, hold=False)
    return job.wait()


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
