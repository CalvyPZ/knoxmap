"""KnoxMap — the whole pipeline in one native window.

A real place becomes a playable Project Zomboid map:

    draw an area  ->  terrain  ->  buildings  ->  compile  ->  install

This is a thin shell. It starts the Flask app on a loopback port and shows it
in a native window, so the window gets the real Leaflet map with the rectangle
tool, place search and landmark lookup, rather than a second UI that would
drift from the web one.

The window is Electron, packed inside the program file players download.
A source checkout starts the Electron binary from desktop/. Set
KNOXMAP_BROWSER=1 to use the browser instead.

Compiling uses the patched PZWorldEd_cli.exe. The program file asks before
downloading it when WorldEd is not beside the executable (see worlded/README.md);
without it the app opens WorldEd on the project instead.

Run it with:  .venv\Scripts\pythonw.exe knoxmap.py   (from the KnoxMap folder, or double-click KnoxMap.exe)
On Linux or macOS:  ./knoxmap.sh
"""
from __future__ import annotations

import multiprocessing
import os
import sys

if __name__ == "__main__":
    # A packed build starts this file again for every layout worker. This has
    # to run before the imports below, or those workers load the app and then
    # wait instead of laying out rooms.
    multiprocessing.freeze_support()

# OpenBLAS starts a pool of workers, and that pool can wait forever when the
# map is drawn on a request thread. One thread does the same arithmetic.
# This has to be set before numpy loads.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
import socket
import subprocess
import threading
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import knoxpaths

BASE_DIR = knoxpaths.BASE_DIR


TITLE = "KnoxMap — real places into Project Zomboid"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int) -> None:
    from app import app

    # threaded so the long Overpass calls don't block the UI's polling requests.
    app.run(host="127.0.0.1", port=port, debug=False,
            use_reloader=False, threaded=True)


def wait_for(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# The Electron process, so a restart can ask it to close before the new
# window starts. See updater.set_before_restart.
_electron: subprocess.Popen | None = None

# Chromium's own words when its sandbox cannot be set up: the helper is not
# SUID, or (Ubuntu 24.04 and newer) AppArmor will not allow user namespaces.
# The page is only this computer's, so opening once more without the sandbox
# is safe. Anything else is a real failure and falls back to the older window.
_SANDBOX_FAILED = (
    "chrome-sandbox",
    "No usable sandbox",
    "namespace sandbox",
    "Failed to move to new namespace",
    "SUID sandbox",
)


def close_electron() -> None:
    """Ask the window to close, and wait until it has.

    Both ways: a line on its stdin, and a file beside it. A GUI process on
    Windows does not always get the pipe, and the file is the one that then
    lands.
    """
    flag = BASE_DIR / "cache" / "electron" / "quit"
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("quit", encoding="ascii")
    except OSError:
        pass
    proc = _electron
    if proc is None or proc.poll() is not None:
        return
    try:
        if proc.stdin:
            proc.stdin.write(b"quit\n")
            proc.stdin.flush()
    except OSError:
        pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def _drain(stream, bucket: bytearray) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if len(bucket) < 16000:
                bucket.extend(chunk[:16000 - len(bucket)])
    except Exception:  # noqa: BLE001 - the stream is only there for the log
        pass


def _start_electron(cmd: list[str], env: dict) -> tuple[subprocess.Popen, bytearray, threading.Thread | None]:
    global _electron
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(BASE_DIR),
    )
    _electron = proc
    err = bytearray()
    reader = None
    if proc.stderr is not None:
        reader = threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True)
        reader.start()
    return proc, err, reader


def _wait_electron(proc: subprocess.Popen, reader: threading.Thread | None) -> tuple[int, bool]:
    """The exit code, and whether it came back within a couple of seconds.

    A window that stays up is one that opened. One that exits immediately did
    not, except exit code 2, which means another KnoxMap window is already
    open and this one stepped aside.
    """
    try:
        code = proc.wait(timeout=2.0)
        quick = True
    except subprocess.TimeoutExpired:
        code = proc.wait()
        quick = False
    if reader is not None:
        reader.join(timeout=1)
    return code, quick


def show_in_electron(cmd: list[str], url: str, token: str) -> int | None:
    """Show the page in Electron.

    None means it never opened, so the caller tries the older window. An
    exit code of 0 includes "already open": the first window is the one in
    use, and this process should just stop.
    """
    import knoxlog
    import updater

    data = BASE_DIR / "cache" / "electron"
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        knoxlog.log.warning("no folder for the window (%s)", exc)
        return None
    flag = data / "quit"
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass

    env = os.environ.copy()
    env["KNOXMAP_URL"] = url
    env["KNOXMAP_TOKEN"] = token
    env["KNOXMAP_TITLE"] = f"{TITLE} ({knoxlog.version()})"
    env["KNOXMAP_DATA"] = str(data)
    env["KNOXMAP_PID"] = str(os.getpid())
    if os.environ.get("KNOXMAP_DEBUG") == "1":
        env["KNOXMAP_DEBUG"] = "1"
    updater.set_before_restart(close_electron)

    proc, err, reader = _start_electron(cmd, env)
    code, quick = _wait_electron(proc, reader)
    text = err.decode("utf-8", "replace")
    if not quick:
        return 0 if code == 2 else code
    if code == 2:
        knoxlog.log.info("KnoxMap is already open")
        return 0
    if (sys.platform.startswith("linux") and "--no-sandbox" not in cmd
            and any(mark in text for mark in _SANDBOX_FAILED)):
        knoxlog.log.warning("the window's sandbox would not start; opening without it")
        # Ahead of the app folder, so Chromium sees the switch as its own.
        extra = ["--no-sandbox"]
        if len(cmd) >= 2 and not str(cmd[-1]).startswith("-"):
            retry = [*cmd[:-1], *extra, cmd[-1]]
        else:
            retry = [*cmd, *extra]
        proc, err, reader = _start_electron(retry, env)
        code, quick = _wait_electron(proc, reader)
        text = err.decode("utf-8", "replace")
        if not quick:
            return 0 if code == 2 else code
        if code == 2:
            return 0
    knoxlog.log.warning("the window did not open (exit %s): %s", code, text[:500])
    return None


def serve_for_shell() -> int:
    """The program file started us. Serve the page and wait until it closes.

    Electron is the process the player launched. It waits for KNOXMAP_READY
    and the port, then shows the window. Quit arrives on stdin and as a file,
    because a Windows process does not always see the pipe.
    """
    import knoxlog
    knoxlog.setup("window")
    import updater
    if updater.apply_staged():
        updater.relaunch()
        return 0
    if not os.environ.get("KNOXMAP_TOKEN"):
        import secrets
        os.environ["KNOXMAP_TOKEN"] = secrets.token_urlsafe(24)
    os.environ["KNOXMAP_SHELL"] = "electron"
    os.environ.setdefault("KNOXMAP_WINDOW", "1")

    import app  # noqa: F401
    app.warm_buildings()

    port = free_port()
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_for(port):
        print("KNOXMAP_ERROR the local server did not start", flush=True)
        return 1
    print(f"KNOXMAP_READY {port}", flush=True)
    import knoxmap_setup
    knoxmap_setup.start_in_background()
    updater.check_in_background()
    _wait_for_shell_quit()
    if updater.restarting():
        updater.wait_restart()
    app.shutdown()
    return 0


def _wait_for_shell_quit() -> None:
    import updater

    flag = Path(os.environ.get("KNOXMAP_DATA") or BASE_DIR / "cache" / "electron") / "quit"
    if flag.exists():
        try:
            flag.unlink()
        except OSError:
            pass
    done = threading.Event()

    def from_stdin() -> None:
        try:
            for line in sys.stdin:
                if line.strip() == "quit":
                    break
        except Exception:  # noqa: BLE001 - a closed pipe is the window going away
            pass
        done.set()

    threading.Thread(target=from_stdin, name="shell-quit", daemon=True).start()
    while not done.is_set():
        if flag.exists() or updater.restarting():
            return
        done.wait(0.4)


def main() -> int:
    if os.environ.get("KNOXMAP_SERVE") == "1":
        return serve_for_shell()

    import knoxlog
    knoxlog.setup("window")
    import updater
    # A downloaded update goes in before any of KnoxMap's code is loaded, and
    # the new version starts in a fresh process.
    if updater.apply_staged():
        updater.relaunch()
        return 0

    # debug_run.bat sets this. The profiler window and logs/debug-log.log
    # exist only for that launch.
    if os.environ.get("KNOXMAP_DEBUG") == "1":
        import knoxprofile
        knoxprofile.start()

    import secrets
    if not os.environ.get("KNOXMAP_TOKEN"):
        os.environ["KNOXMAP_TOKEN"] = secrets.token_urlsafe(24)

    import knoxpaths
    shell_cmd = None if in_a_browser() else knoxpaths.electron_shell()
    if shell_cmd:
        os.environ["KNOXMAP_SHELL"] = "electron"
        os.environ["KNOXMAP_WINDOW"] = "1"
    else:
        os.environ["KNOXMAP_SHELL"] = "browser"
        os.environ["KNOXMAP_WINDOW"] = "0"

    import app  # noqa: F401 - fail here, where it can be reported, not in the thread
    app.warm_buildings()
    if os.environ.get("KNOXMAP_DEBUG") == "1":
        import knoxprofile
        knoxprofile.attach(app.app)

    port = free_port()
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_for(port):
        raise RuntimeError("the local server did not start within 20 seconds")
    import knoxmap_setup
    knoxmap_setup.start_in_background()
    updater.check_in_background()

    url = f"http://127.0.0.1:{port}/"
    code = 0
    if shell_cmd:
        code = show_in_electron(shell_cmd, url, os.environ["KNOXMAP_TOKEN"])
        if code is None:
            shell_cmd = None
            knoxlog.log.warning("no Electron window; opening a browser instead")
            os.environ["KNOXMAP_SHELL"] = "browser"
            os.environ["KNOXMAP_WINDOW"] = "0"
    # A restart closed the window on purpose. Wait until the new process has
    # been started; returning here would end this one first.
    if updater.restarting():
        updater.wait_restart()
        return 0
    if shell_cmd:
        app.shutdown()
        return code or 0
    return show_in_browser(url)


def in_a_browser() -> bool:
    """Whether to skip the window and use the player's browser.

    KNOXMAP_BROWSER=1 forces this. The program file players download always
    has its own window.
    """
    return os.environ.get("KNOXMAP_BROWSER") == "1"


def show_in_browser(url: str) -> int:
    """Serve KnoxMap and open it in the default browser, in the foreground.

    The window is what usually keeps the process alive; without one this
    waits instead, so closing the terminal is what ends KnoxMap.
    """
    import webbrowser

    os.environ["KNOXMAP_WINDOW"] = "0"
    print(f"KnoxMap is running at {url}", flush=True)
    print("Leave this window open while you use it; press Ctrl+C to stop.", flush=True)
    try:
        webbrowser.open(url)
        if os.environ.get("KNOXMAP_DEBUG") == "1":
            webbrowser.open(url + "debug", new=1)
    except Exception:        # noqa: BLE001 - no browser configured; the URL is printed
        pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        try:
            import app
            app.shutdown()
        except Exception:  # noqa: BLE001 - the process is ending anyway
            pass
        return 0


def report_crash() -> None:
    """pythonw has no console, so a failed start would just vanish.

    Write the traceback next to the app and say where it is in a message box.
    """
    import traceback

    where = BASE_DIR / "logs" / "knoxmap.log"
    try:
        import knoxlog
        knoxlog.setup("window")
        eid = " as " + knoxlog.record(sys.exc_info()[1], "KnoxMap could not start")
    except Exception:  # noqa: BLE001 - the logger itself may be what broke
        eid = ""
        where = BASE_DIR / "knoxmap_error.log"
        where.write_text(traceback.format_exc(), encoding="utf-8")
    text = (f"KnoxMap could not start:\n\n{sys.exc_info()[1]}\n\n"
            f"Details were saved to {where}{eid}.\n"
            "If this keeps happening, post that file in #bug-reports on the KnoxMap Discord.")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "KnoxMap", 0x10)
    except Exception:  # noqa: BLE001 - not on Windows; the log is enough
        print(text, file=sys.stderr)


if __name__ == "__main__":
    # A frozen Windows build re-enters this file in each worker. This has to
    # run before main(), and only under the guard, or those workers start the app.
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        report_crash()
        raise SystemExit(1)
