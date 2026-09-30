"""Discord Rich Presence: what KnoxMap is doing, on your Discord profile.

Discord's local client listens on a named pipe and speaks a small framed-JSON
protocol, so this needs no library: a frame is a little-endian uint32 opcode,
a little-endian uint32 length, and that many bytes of JSON. Opcode 0 is the
handshake, 1 a command, 2 a close.

Everything here fails quietly. Discord not running, a pipe that disappears
when it restarts, a permission error on the socket - none of it is the
player's problem and none of it may interrupt a map that is halfway built, so
every call is wrapped and the worst case is no presence.

It runs on its own thread: the IPC write is a blocking pipe write, and the
generator must never wait on Discord to finish a tile.
"""
from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import uuid

import knoxlog
import knoxpaths

log = knoxlog.log

# The Discord application the presence is published as. It carries the name
# on the profile - "Playing KnoxMap" - and the artwork. An application id is
# not a secret and is readable out of any build; it is not a token and grants
# nothing, so it lives here rather than in a config a player has to fill in.
# A build can override it with KNOXMAP_DISCORD_APP_ID or discord_app_id in
# the config file.
#
# Its art asset is named "knoxmap" and is branding/logo.png, uploaded under
# Rich Presence -> Art Assets in the Discord developer portal.
DEFAULT_APP_ID = "1554593562010845275"

# The art asset uploaded under Rich Presence -> Art Assets. Discord matches
# large_image against the name it was uploaded under, so one uploaded as
# anything else shows no logo at all and says nothing about why. An
# application's art assets are public - this needs no token - so the id is
# looked up once and sent instead of the name, which works whatever it was
# called and survives it being renamed. The name is only the preference when
# there are several; the fallback is the first asset there is, and then the
# name itself, which is what it always sent.
ASSET_PREFERRED = "knoxmap"
ASSETS_URL = "https://discord.com/api/v9/oauth2/applications/{app}/assets"
ASSET_TTL_S = 3600.0

# Discord refuses updates faster than about one every 15 seconds and drops
# the rest, so a build that reports every tile would mostly be talking to
# itself. The last state is kept and sent when the window opens again.
MIN_GAP_S = 15.0
# How long to wait before trying the pipe again after it refuses or vanishes.
RETRY_S = 60.0

_HANDSHAKE, _FRAME, _CLOSE = 0, 1, 2

_asset_cache: dict = {"at": 0.0, "image": None}
_asset_lock = threading.Lock()


def large_image() -> str:
    """The art asset id to show, or the plain name if it cannot be looked up.

    Called from the presence thread only: it makes one HTTP request an hour
    at most and must never sit in front of anything the window is doing.
    """
    with _asset_lock:
        fresh = time.time() - _asset_cache["at"] < ASSET_TTL_S
        if fresh and _asset_cache["image"]:
            return _asset_cache["image"]
    # Set outright when the lookup cannot be reached - some networks block
    # discord.com - with the id from
    # discord.com/api/v9/oauth2/applications/<app>/assets, which any browser
    # can open: "discord_asset" in knoxmap_config.json, or
    # KNOXMAP_DISCORD_ASSET.
    fixed = os.environ.get("KNOXMAP_DISCORD_ASSET", "").strip()
    if not fixed:
        try:
            fixed = str(knoxpaths.load_config().get("discord_asset", "") or "").strip()
        except Exception:  # noqa: BLE001 - a broken config is not worth a crash
            fixed = ""
    if fixed:
        with _asset_lock:
            _asset_cache.update(at=time.time(), image=fixed)
        return fixed
    found = ASSET_PREFERRED
    try:
        import requests

        r = requests.get(ASSETS_URL.format(app=app_id()),
                         headers={"User-Agent": "KnoxMap"}, timeout=10)
        r.raise_for_status()
        assets = [a for a in r.json() if a.get("id")]
        if not assets:
            log.debug("discord: the application has no art assets uploaded")
        else:
            pick = next((a for a in assets
                         if (a.get("name") or "").lower() == ASSET_PREFERRED), assets[0])
            found = str(pick["id"])
            log.debug("discord: art asset %r is %s", pick.get("name"), found)
    except Exception as exc:  # noqa: BLE001 - offline, blocked, anything
        log.debug("discord: could not read the art assets (%s)", exc)
    with _asset_lock:
        _asset_cache.update(at=time.time(), image=found)
    return found


def app_id() -> str:
    """The Discord application id, from the config file or the environment."""
    env = os.environ.get("KNOXMAP_DISCORD_APP_ID", "").strip()
    if env:
        return env
    try:
        return str(knoxpaths.load_config().get("discord_app_id", "")
                   or DEFAULT_APP_ID).strip()
    except Exception:  # noqa: BLE001 - a broken config is not worth a crash
        return DEFAULT_APP_ID


def enabled() -> bool:
    """Whether to publish presence at all. On, unless it is turned off.

    It needs an application id to publish as. Presence does tell everyone on
    your friends list what you are doing, so there are two ways out of it
    without a switch in the window: "discord_presence": false in
    knoxmap_config.json, and KNOXMAP_NO_DISCORD=1.
    """
    if os.environ.get("KNOXMAP_NO_DISCORD"):
        return False
    if not app_id():
        return False
    try:
        return knoxpaths.load_config().get("discord_presence", True) is not False
    except Exception:  # noqa: BLE001 - no config yet is the default
        return True


def _pipe_paths():
    """Where Discord listens, in the order Discord itself looks."""
    if sys.platform == "win32":
        for i in range(10):
            yield rf"\\.\pipe\discord-ipc-{i}"
        return
    base = (os.environ.get("XDG_RUNTIME_DIR")
            or os.environ.get("TMPDIR")
            or os.environ.get("TMP")
            or os.environ.get("TEMP")
            or tempfile.gettempdir())
    # Flatpak and Snap Discord put their socket a level or two down.
    roots = [base,
             os.path.join(base, "app", "com.discordapp.Discord"),
             os.path.join(base, "snap.discord")]
    for root in roots:
        for i in range(10):
            yield os.path.join(root, f"discord-ipc-{i}")


class _Pipe:
    """One connection to the Discord client, or nothing."""

    def __init__(self) -> None:
        self._f = None
        self._sock = None

    def open(self, client_id: str) -> bool:
        for path in _pipe_paths():
            try:
                if sys.platform == "win32":
                    self._f = open(path, "r+b", buffering=0)
                else:
                    import socket as _socket
                    if not os.path.exists(path):
                        continue
                    self._sock = _socket.socket(_socket.AF_UNIX,
                                                _socket.SOCK_STREAM)
                    self._sock.settimeout(2.0)
                    self._sock.connect(path)
            except (OSError, ValueError):
                self.close()
                continue
            try:
                self._send(_HANDSHAKE, {"v": 1, "client_id": client_id})
                op, data = self._read()
            except OSError:
                self.close()
                continue
            # Discord answers the handshake with READY, or closes the socket
            # with the reason - an application id that does not exist, say.
            # Nothing used to read this, so a connection that had already been
            # refused looked live and every update went into the dark.
            if op == _FRAME and (data or {}).get("evt") == "READY":
                return True
            log.debug("discord refused the handshake: %s", data)
            self.close()
        return False

    def _read(self) -> tuple[int | None, dict | None]:
        """The next frame, or (None, None). Discord answers every command, so
        this does not wait for something that is not coming."""
        head = self._recv(8)
        if len(head) < 8:
            return None, None
        op, length = struct.unpack("<II", head)
        body = self._recv(length) if length else b""
        try:
            return op, json.loads(body.decode("utf-8"))
        except ValueError:
            return op, None

    def _recv(self, want: int) -> bytes:
        out = b""
        while len(out) < want:
            if self._f is not None:
                chunk = self._f.read(want - len(out))
            elif self._sock is not None:
                chunk = self._sock.recv(want - len(out))
            else:
                raise OSError("not connected")
            if not chunk:
                break
            out += chunk
        return out

    def _send(self, op: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        frame = struct.pack("<II", op, len(body)) + body
        if self._f is not None:
            self._f.write(frame)
            self._f.flush()
        elif self._sock is not None:
            self._sock.sendall(frame)
        else:
            raise OSError("not connected")

    def activity(self, activity: dict | None) -> None:
        self._send(_FRAME, {
            "cmd": "SET_ACTIVITY",
            "nonce": str(uuid.uuid4()),
            "args": {"pid": os.getpid(), "activity": activity},
        })
        # Read what it made of it. An activity Discord will not show - a
        # rate limit, a field it does not like - comes back as an error here
        # and is worth a line in the log; without this the only symptom is a
        # profile that never changes.
        op, data = self._read()
        if op is None:
            raise OSError("discord closed the connection")
        if (data or {}).get("evt") == "ERROR":
            log.debug("discord refused the activity: %s", (data or {}).get("data"))

    def close(self) -> None:
        for handle in (self._f, self._sock):
            try:
                if handle is not None:
                    handle.close()
            except OSError:
                pass
        self._f = self._sock = None

    @property
    def live(self) -> bool:
        return self._f is not None or self._sock is not None


class Presence:
    """The presence KnoxMap publishes, updated from whatever it is doing."""

    def __init__(self) -> None:
        self._pipe = _Pipe()
        self._lock = threading.Lock()
        self._want: dict | None = None
        self._sent: dict | None = None
        self._sent_at = 0.0
        self._next_try = 0.0
        self._started = time.time()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- what to show ----------------------------------------------------
    def set(self, details: str, state: str | None = None,
            *, keep_start: bool = True) -> None:
        """Say what the player is doing. Cheap, and safe from any thread."""
        if not enabled():
            return
        # The artwork is added on the presence thread, where looking it up is
        # allowed to take a moment; what is compared against what was last
        # sent stays this dict, so adding it never looks like a change.
        activity = {"details": details[:128]}
        if state:
            activity["state"] = state[:128]
        if keep_start:
            activity["timestamps"] = {"start": int(self._started)}
        with self._lock:
            self._want = activity
        self._ensure_thread()

    def clear(self) -> None:
        with self._lock:
            self._want = None
        self._ensure_thread()

    # -- the thread ------------------------------------------------------
    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="discord",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 - never leaves this thread
                log.debug("discord presence: %s", exc)
                self._pipe.close()
                self._next_try = time.time() + RETRY_S

    def _tick(self) -> None:
        with self._lock:
            want = self._want
        if want == self._sent and self._pipe.live:
            return
        now = time.time()
        if now - self._sent_at < MIN_GAP_S:
            return
        if not self._pipe.live:
            if now < self._next_try:
                return
            if not self._pipe.open(app_id()):
                self._next_try = now + RETRY_S
                return
            # A fresh connection has nothing on it, so resend whatever the
            # state is rather than trusting what was sent down the old one.
            self._sent = object()          # never equal to an activity dict
        self._pipe.activity(self._with_artwork(want))
        self._sent = want
        self._sent_at = now

    @staticmethod
    def _with_artwork(activity: dict | None) -> dict | None:
        """`activity` with the logo on it, looked up here and not by the
        caller: this runs on the presence thread, which is allowed to wait."""
        if not activity:
            return activity
        out = dict(activity)
        out["assets"] = {"large_image": large_image(), "large_text": "KnoxMap"}
        return out

    def close(self) -> None:
        self._stop.set()
        try:
            if self._pipe.live:
                self._pipe.activity(None)
        except Exception:  # noqa: BLE001
            pass
        self._pipe.close()


# One per process. Importing this module costs a lock and nothing else; no
# pipe is opened until something actually sets a presence.
presence = Presence()


# What each stage of the pipeline says on the profile, in the author's own
# words. Three of them cover the whole pipeline: the two download stages read
# the same, and so do the two that write the map out.
#
# There is deliberately no entry for "error": a failed build is not something
# to announce to somebody's friends list. A stage with no entry here leaves
# the presence as it was rather than replacing it - except the ones in
# STAGE_CLEARS, which take it down.
STAGE_TEXT = {
    "osm": "Scooping data from osm",
    "overture": "Scooping data from osm",
    "render": "Mapping",
    "buildings": "Mapping",
    "compile": "Compiling",
    "install": "Compiling",
}
# What it says with the window open and no map building. Without this there
# was no presence at all except during a run, so somebody who switched it on
# between maps saw nothing happen and reported it as broken - which is most of
# the time the window is open.
IDLE_TEXT = "Planning a map"

# The run is over: back to idle rather than leaving "Compiling" up all evening.
STAGE_CLEARS = {"done", "stopped", "stopping", "error"}


def idle() -> None:
    """The window is open and no map is building."""
    presence.set(IDLE_TEXT)


def stage(name: str) -> None:
    """Publish one pipeline stage. Safe to call from a worker thread.

    The stage and nothing else. The map is named after the place somebody is
    building, which is often where they live, and that is not something to put
    on a friends list.
    """
    if name in STAGE_CLEARS:
        idle()
        return
    text = STAGE_TEXT.get(name)
    if not text:
        return
    presence.set(text)
