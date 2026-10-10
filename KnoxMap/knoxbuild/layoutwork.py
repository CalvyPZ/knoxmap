"""Room layout on worker processes that outlive any one piece of the map.

What this replaces. Each piece of a big map used to start its own
multiprocessing.Pool for the layout stage and send it one building per task.
Every task and every answer went through helper threads in the one program
that also runs the other stages of every piece, and those stages are Python
that holds the interpreter lock. A thread that wants the lock back after a
pipe write waits for the running thread to give it up, so the pool moved
only as many buildings a second as the lock changed hands. The workers sat
waiting for work with the processor mostly idle. The pools were also sized
from memory on the guess that a worker costs a quarter of a gigabyte, and a
worker was a full copy of the build code (Shapely, PROJ, Pillow, the renderer)
that it never called.

How it works now.

* One service owns the workers. They are started once, are shared by every
  piece that is building at the same time, and exit by themselves when nothing
  has asked them for work for a while. A piece that is not laying out rooms
  (it is reading footprints, painting paths) costs the others nothing, and the
  workers it is not using go to the pieces that are.
* A worker imports only NumPy, the catalog, the layout and the .tbx writer.
* Work does not travel one building at a time. A piece's buildings are packed
  into chunks of similar cost, biggest first, and written once into a block
  of shared memory with the piece's results. A worker takes a chunk off one
  queue (a few hundred messages for a piece, not tens of thousands), reads it
  from the block, lays out each building, and writes the answer into the block
  as a few integers. The piece's thread only reads counters. Nothing a worker
  does needs the lock in the main program.
* The styles and settings are the same dictionaries for thousands of
  buildings. They go into the block once, not once per building.
* Workers run below normal priority, so the main program's own stages (the
  part that cannot be spread out) are never waiting behind them.
* How many workers? Every processor but one, and as many as the free memory
  can hold after an eighth is left alone. The core left free stays with the
  rest of the program. The memory cost of a worker is measured from the first
  ones that start, not guessed.
* Each worker also reads the doors out of the house it has just written, so
  the paths pass does not read every file back from disk.

A worker that dies takes only the buildings it was holding. They are laid out
again here. A service that cannot start workers at all hands the whole piece
back to be laid out in this process, as before.
"""
from __future__ import annotations

import atexit
import os
import pickle
import queue
import struct
import sys
import threading
import time
import multiprocessing
from contextlib import contextmanager
from multiprocessing import shared_memory

import knoxstop

# ---------------------------------------------------------------------------
# What a building's status byte can be, in the shared block.

PENDING, CLAIMED, DONE, FAILED, ACTIVE = 0, 1, 2, 3, 4

# A house's outside doors travel back as up to this many (x, y, x, y) groups.
# A house with more is read from its file, as before.
DOOR_SLOTS = 6
_DOOR_INTS = 1 + 4 * DOOR_SLOTS

# At most this many buildings in one chunk, and about this many chunks for
# each worker so the last ones are small.
MAX_CHUNK_JOBS = 24
CHUNKS_PER_WORKER = 24

# Seconds with no building finished before the workers are given up on and
# the rest are laid out here. A mall can take a couple of minutes by itself.
STALL_S = 300.0

# A worker's own timers.
_POLL_S = 1.0           # how long it waits on the queue before looking around
_DETACH_IDLE_S = 5.0    # idle this long: let go of the last piece's block
_IDLE_EXIT_S = 180.0    # idle this long: exit and give the memory back
_RECYCLE_AFTER = 6000   # buildings before a fresh process takes over

# The memory side. An eighth of installed memory stays free, and at least 1 GB.
_KEEP_FREE_DIVISOR = 8
_KEEP_FREE_MIN = 1024 ** 3
# What a worker is assumed to cost until the first ones have been measured.
_WORKER_BYTES_GUESS = 192 * 1024 ** 2
_WORKER_BYTES_MIN = 96 * 1024 ** 2
# Headroom added to the measured size: a worker grows a little as it works.
_WORKER_GROWTH = 48 * 1024 ** 2
# Workers started per wave (at least this many; more on a big machine). The
# next wave is sized after the last one loaded, from the memory then free.
_WAVE = 8
_BOOT_WAIT_S = 120.0
_MAX_SLOTS = 512
# Workers run below normal priority, so the main program is not waiting behind
# a building. One processor stays free for that work. Memory is the other
# limit: see budget() and Service._grow.

_HEADER = 64


class Unavailable(RuntimeError):
    """Workers cannot be started. The caller lays the rooms out itself."""


# ---------------------------------------------------------------------------
# Memory.

def installed_memory() -> tuple[int, int, int] | None:
    """(total, free, room) in bytes, or None when the machine will not say."""
    try:
        import knoxlog
        status = knoxlog.memory_status()
    except Exception:  # noqa: BLE001 - no reading
        return None
    if not status or status[0] <= 0:
        return None
    return int(status[0]), int(status[1]), int(status[2])


def keep_free(total: int) -> int:
    """Memory the workers leave alone: an eighth, and at least 1 GB."""
    return max(_KEEP_FREE_MIN, int(total) // _KEEP_FREE_DIVISOR)


def process_private_bytes(pid: int) -> int | None:
    """What a process holds for itself, in bytes. None when it cannot be read."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            info = kernel32.K32GetProcessMemoryInfo
            info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            info.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # query limited
            if not handle:
                return None
            try:
                counters = Counters()
                counters.cb = ctypes.sizeof(Counters)
                if not info(handle, ctypes.byref(counters), counters.cb):
                    return None
                return int(counters.PrivateUsage)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return None
    try:
        with open(f"/proc/{int(pid)}/statm", encoding="ascii") as handle:
            parts = handle.read().split()
        pages = max(0, int(parts[1]) - int(parts[2]))     # resident, not shared
        return pages * int(os.sysconf("SC_PAGE_SIZE"))
    except Exception:  # noqa: BLE001
        return None


def cpu_cap() -> int:
    """The most workers the processors may use. One core stays free."""
    cores = os.cpu_count() or 1
    return max(1, cores - 1)


def budget() -> int:
    """How many layout workers this PC should run at once, for every piece.

    KNOXBUILD_WORKERS, when set, is that number. Otherwise it is what the
    processors can use, or fewer if free memory cannot hold that many of the
    workers measured so far (a guess until some have started).
    """
    raw = os.environ.get("KNOXBUILD_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    cap = cpu_cap()
    status = installed_memory()
    if not status:
        return max(1, min(cap, os.cpu_count() or 1))
    total, free, room = status
    svc = _service
    running = svc.alive_count() if svc is not None else 0
    each = svc.worker_bytes() if svc is not None else _WORKER_BYTES_GUESS
    usable = min(free, room) - keep_free(total)
    fit = max(0, int(usable // each))
    return max(1, min(cap, running + fit))


# ---------------------------------------------------------------------------
# The shared block of one piece's layout.
#
#   header       byte 0: asked to stop
#   status       one byte per building
#   who          the process id that holds the building (unsigned 32-bit)
#   stats        storeys, rooms, furniture (three 32-bit ints) per building
#   doors        a count, then up to DOOR_SLOTS groups of four 16-bit ints
#   table        where each part of the blob starts; part 0 is the registry
#   blob         the registry, then each chunk

def _align(n: int) -> int:
    return (n + 7) & ~7


class _Layout:
    """Where each array sits in a block for this many buildings and chunks."""

    __slots__ = ("n", "chunks", "status", "who", "stats", "doors", "table", "blob")

    def __init__(self, n_jobs: int, n_chunks: int):
        self.n = n_jobs
        self.chunks = n_chunks
        self.status = _HEADER
        self.who = _align(self.status + n_jobs)
        self.stats = _align(self.who + 4 * n_jobs)
        self.doors = _align(self.stats + 12 * n_jobs)
        self.table = _align(self.doors + 2 * _DOOR_INTS * n_jobs)
        self.blob = _align(self.table + 8 * (n_chunks + 2))


def _fingerprint(obj):
    """Equal for two objects that are the same thing, for sharing one copy.

    A dictionary matches another when every key holds the same text or number,
    or the very same object. The wall styles are rebuilt for every special
    building from shared parts, so they compare equal here though they are not
    the same dictionary.
    """
    if isinstance(obj, dict):
        return ("d",) + tuple(
            (k, v if isinstance(v, (str, int, float, bool, type(None))) else id(v))
            for k, v in obj.items())
    return ("o", id(obj))


def _attach(name: str):
    """Open a block another process made, without taking it over."""
    try:
        return shared_memory.SharedMemory(name=name, create=False, track=False)
    except TypeError:       # before 3.13 there is no `track`
        block = shared_memory.SharedMemory(name=name, create=False)
        if os.name != "nt":
            try:
                from multiprocessing import resource_tracker
                resource_tracker.unregister(block._name, "shared_memory")
            except Exception:  # noqa: BLE001
                pass
        return block


# ---------------------------------------------------------------------------
# One building. Used by the workers and by the main program when it lays out
# a few rooms itself.

def make_one(job: tuple, should_stop=None) -> tuple:
    """Lay out one building and write its .tbx.

    Returns (storeys, rooms, furniture, error, doors). A building that cannot
    be laid out is left out with the reason, instead of stopping the other
    two thousand. `doors` is the outside doors of a house, read from the text
    just written, or None for anything else. A file that cannot be written
    raises: that is the disk, not the building.
    """
    from .layout import build_building
    from .tbx import render_tbx

    stop = should_stop if should_stop is not None else (lambda: False)
    if stop():
        raise knoxstop.Stopped("the buildings")
    (w, h, levels, commercial, seed, kind, mask, settings, style, label, path, street,
     retail, uses, hotel, party) = job
    try:
        plan = build_building(w, h, levels=levels, commercial=commercial, seed=seed,
                              kind=kind, mask=mask, settings=settings, street=street,
                              retail=retail, uses=uses, hotel=hotel, party=party,
                              should_stop=stop)
        text = render_tbx(plan, label, style)
    except knoxstop.Stopped:
        raise
    except Exception:  # noqa: BLE001
        import traceback
        return (0, 0, 0, f"{os.path.basename(path)} ({kind or 'house'}, {w}x{h}, "
                         f"{levels} storeys): {traceback.format_exc()}", None)
    if stop():
        raise knoxstop.Stopped("the buildings")
    partial = path + ".part"
    with open(partial, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(partial, path)
    doors = None
    if (kind or "house") == "house":
        try:
            from .doors import outside_doors_from_text
            doors = outside_doors_from_text(text)
        except Exception:  # noqa: BLE001 - the paths pass reads the file instead
            doors = None
    return (len(plan.storeys), len(plan.rooms),
            sum(len(s.furniture) for s in plan.storeys), None, doors)


# ---------------------------------------------------------------------------
# The worker process.

def _lower_priority() -> None:
    """Below normal, so the main program's own stages never wait for a worker."""
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)
        else:
            os.nice(5)
    except Exception:  # noqa: BLE001 - priority is a courtesy
        pass


def _prepare_worker() -> None:
    """Import what laying out a room needs, and nothing else."""
    os.environ["KNOXMAP_WORKER"] = "1"
    os.environ.pop("KNOXMAP_SERVE", None)
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "1"
    # A worker inherits the window's output pipe, and nothing reads it here.
    devnull = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = devnull
    sys.stderr = devnull
    _lower_priority()
    from . import catalog, layout, tbx
    catalog.HOUSE_STYLES
    layout.build_building
    tbx.render_tbx
    import gc
    gc.collect()
    # The catalog is millions of objects that never change. Freezing them keeps
    # every later collection from walking them again.
    gc.freeze()


class _Attached:
    """A piece's shared block, as a worker sees it."""

    def __init__(self, name: str, n_jobs: int, n_chunks: int):
        self.name = name
        self.shm = _attach(name)
        self.buf = self.shm.buf
        self.lay = _Layout(n_jobs, n_chunks)
        self._registry = None

    def cancelled(self) -> bool:
        return self.buf[0] != 0

    def registry(self) -> list:
        if self._registry is None:
            a, b = struct.unpack_from("<2q", self.buf, self.lay.table)
            blob = self.lay.blob
            self._registry = pickle.loads(bytes(self.buf[blob + a:blob + b]))
        return self._registry

    def chunk(self, c: int):
        a, b = struct.unpack_from("<2q", self.buf, self.lay.table + 8 * (1 + c))
        blob = self.lay.blob
        return pickle.loads(bytes(self.buf[blob + a:blob + b]))

    def restore(self, row: tuple) -> tuple:
        reg = self.registry()
        return row[:7] + (reg[row[7]], reg[row[8]]) + row[9:]

    def close(self) -> None:
        self._registry = None
        self.buf = None
        try:
            self.shm.close()
        except (BufferError, OSError, ValueError):
            pass


def _serve_chunk(view: _Attached, c: int, errors, quit_event) -> int:
    """Lay out every building of one chunk. Returns how many were finished."""
    lay = view.lay
    buf = view.buf
    pid = os.getpid()
    idxs, rows = view.chunk(c)
    for j in idxs:
        struct.pack_into("<I", buf, lay.who + 4 * j, pid)
        buf[lay.status + j] = CLAIMED

    def stop() -> bool:
        return buf[0] != 0

    made = 0
    for j, row in zip(idxs, rows):
        if stop() or quit_event.is_set():
            return made
        buf[lay.status + j] = ACTIVE
        try:
            storeys, rooms, furniture, error, doors = make_one(view.restore(row), stop)
        except knoxstop.Stopped:
            buf[lay.status + j] = PENDING
            return made
        except OSError as exc:      # the disk: the whole piece should stop
            try:
                errors.put((view.name, j, f"{type(exc).__name__}: {exc}", True))
            except Exception:  # noqa: BLE001
                pass
            buf[lay.status + j] = FAILED
            return made
        if error:
            try:
                errors.put((view.name, j, error, False))
            except Exception:  # noqa: BLE001
                pass
            buf[lay.status + j] = FAILED
        else:
            struct.pack_into("<3i", buf, lay.stats + 12 * j, storeys, rooms, furniture)
            vals = [0] * _DOOR_INTS
            if doors is not None and len(doors) <= DOOR_SLOTS:
                vals[0] = len(doors) + 1
                for i, door in enumerate(doors):
                    vals[1 + 4 * i:5 + 4 * i] = [int(v) for v in door]
            try:
                struct.pack_into(f"<{_DOOR_INTS}h", buf,
                                 lay.doors + 2 * _DOOR_INTS * j, *vals)
            except struct.error:
                struct.pack_into(f"<{_DOOR_INTS}h", buf,
                                 lay.doors + 2 * _DOOR_INTS * j, *([0] * _DOOR_INTS))
            buf[lay.status + j] = DONE
        made += 1
    return made


def worker_main(claims, errors, quit_event, ready, slot: int) -> None:
    """The life of one worker: take a chunk, lay it out, take the next."""
    try:
        _prepare_worker()
    except BaseException:  # noqa: BLE001 - the parent sees the exit code
        os._exit(3)
    ready[slot] = 1
    attached: dict[str, _Attached] = {}
    idle = 0.0
    made = 0

    def let_go(keep: str | None = None) -> None:
        for key in list(attached):
            if key != keep:
                attached.pop(key).close()

    try:
        while not quit_event.is_set():
            try:
                item = claims.get(timeout=_POLL_S)
            except queue.Empty:
                idle += _POLL_S
                if attached and idle >= _DETACH_IDLE_S:
                    let_go()
                if idle >= _IDLE_EXIT_S:
                    break
                continue
            except (EOFError, OSError, ValueError):
                break
            idle = 0.0
            if item is None:
                break
            name, n_jobs, n_chunks, c = item
            view = attached.get(name)
            if view is None:
                let_go()
                try:
                    view = _Attached(name, n_jobs, n_chunks)
                except (FileNotFoundError, OSError, ValueError):
                    continue            # the piece is over; nothing left to do
                attached[name] = view
            if view.cancelled():
                continue
            made += _serve_chunk(view, c, errors, quit_event)
            if made >= _RECYCLE_AFTER:
                break
    finally:
        let_go()


# ---------------------------------------------------------------------------
# The service, in the main program.

_service = None
_service_lock = threading.Lock()
_spawn_lock = threading.Lock()
_context = None


def _mp():
    global _context
    if _context is None:
        _context = multiprocessing.get_context("spawn")
    return _context


def _trace(message: str) -> None:
    try:
        import knoxlog
        knoxlog.setup("knoxbuild")
        knoxlog.log.info("knoxbuild %s", message)
        knoxlog.flush()
    except Exception:  # noqa: BLE001
        pass


def _blas_one_thread() -> None:
    """Keep numeric libraries from starting a thread team inside every worker."""
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        if name not in os.environ:
            os.environ[name] = "1"


class Service:
    """The workers, and the queues that feed them."""

    def __init__(self):
        self._lock = threading.RLock()
        self._leases = 0
        self._procs: list = []
        self._claims = None
        self._errors = None
        self._quit = None
        self._ready = None
        self._next_slot = 0
        self._want = 0
        self._measured = 0
        self._broken = ""
        self._boot_failures = 0
        self._ever_ready = False
        self._grower = None
        self._batches = 0
        self._last_maintain = 0.0
        self._err_lock = threading.Lock()
        self._errs: dict = {}
        self.generation = 0
        self.deaths = 0

    # -- who is using it -------------------------------------------------

    @contextmanager
    def lease(self):
        """Keep the workers alive. Nested and shared: the last one out stops them."""
        with self._lock:
            self._leases += 1
        try:
            yield self
        finally:
            with self._lock:
                self._leases = max(0, self._leases - 1)
                last = self._leases == 0
            if last:
                self.shutdown()

    def batch_started(self) -> None:
        with self._lock:
            self._batches += 1

    def batch_ended(self) -> None:
        with self._lock:
            self._batches = max(0, self._batches - 1)

    # -- workers ---------------------------------------------------------

    def alive_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._procs if p.is_alive())

    def alive_pids(self) -> set:
        with self._lock:
            return {p.pid for p in self._procs if p.is_alive()}

    def worker_bytes(self) -> int:
        """What one more worker should be counted as."""
        if self._measured:
            return max(_WORKER_BYTES_MIN,
                       int(self._measured * 1.1) + _WORKER_GROWTH)
        return _WORKER_BYTES_GUESS

    def quitting(self) -> bool:
        quit_event = self._quit
        return quit_event is not None and quit_event.is_set()

    def _create_channels(self) -> None:
        ctx = _mp()
        self._claims = ctx.Queue()
        self._errors = ctx.Queue()
        self._quit = ctx.Event()
        self._ready = ctx.RawArray("b", _MAX_SLOTS)
        self._next_slot = 0
        self._procs = []
        self._boot_failures = 0
        self._ever_ready = False
        self.generation += 1

    def _prune(self) -> None:
        """Forget workers that have exited. Counts the ones that died."""
        keep = []
        for proc in self._procs:
            if proc.is_alive():
                keep.append(proc)
                continue
            if proc.exitcode not in (0, None):
                self.deaths += 1
            try:
                proc.join(0)
                proc.close()
            except Exception:  # noqa: BLE001
                pass
        self._procs = keep

    def _spawn(self, count: int) -> list:
        """Start `count` workers. The caller holds the lock."""
        ctx = _mp()
        started = []
        _blas_one_thread()
        for _ in range(count):
            slot = self._next_slot % _MAX_SLOTS
            self._next_slot += 1
            self._ready[slot] = 0
            proc = ctx.Process(
                target=worker_main,
                args=(self._claims, self._errors, self._quit, self._ready, slot),
                name="knox-layout", daemon=True)
            # Windows starts a worker by running this program again. The flag
            # is inherited, so that copy lays out rooms and does not open
            # another server. The lock keeps one start from restoring the
            # server flag while another is still reading it.
            with _spawn_lock:
                os.environ["KNOXMAP_WORKER"] = "1"
                serve = os.environ.pop("KNOXMAP_SERVE", None)
                try:
                    proc.start()
                finally:
                    os.environ.pop("KNOXMAP_WORKER", None)
                    if serve is not None:
                        os.environ["KNOXMAP_SERVE"] = serve
            proc._knox_slot = slot
            self._procs.append(proc)
            started.append(proc)
        return started

    def prepare(self, want: int) -> None:
        """Have workers on the way. Raises Unavailable when there can be none."""
        want = max(1, int(want))
        with self._lock:
            if self._broken:
                raise Unavailable(self._broken)
            if self._claims is None or self._quit is None or self._quit.is_set():
                self._create_channels()
            self._want = want
            self._prune()
            alive = len(self._procs)
            first = min(want, max(4, os.cpu_count() or 1)) - alive
            if first > 0:
                try:
                    self._spawn(first)
                except Exception as exc:  # noqa: BLE001
                    self._broken = f"worker processes could not start: {exc}"
                    _trace(self._broken)
                    raise Unavailable(self._broken) from exc
            self._ensure_grower()
        _trace(f"layout service: {self.alive_count()} workers running, "
               f"{want} wanted ({self.worker_bytes() >> 20} MB each counted)")

    def _ensure_grower(self) -> None:
        """Start the thread that brings the workers up to the number wanted."""
        if self._grower is not None or self._broken:
            return
        if len(self._procs) >= self._want:
            return
        self._grower = threading.Thread(target=self._grow, name="knox-layout-grow",
                                        daemon=True)
        self._grower.start()

    def _await_ready(self, procs: list) -> None:
        deadline = time.monotonic() + _BOOT_WAIT_S
        pending = list(procs)
        while pending and time.monotonic() < deadline:
            if self.quitting():
                return
            still = []
            for proc in pending:
                slot = getattr(proc, "_knox_slot", -1)
                if slot >= 0 and self._ready is not None and self._ready[slot]:
                    self._ever_ready = True
                elif proc.exitcode is not None:
                    self._boot_failures += 1
                else:
                    still.append(proc)
            pending = still
            if pending:
                time.sleep(0.2)
        with self._lock:
            if self._boot_failures >= 3 and not self._ever_ready:
                self._broken = "worker processes start and stop at once"
                _trace(self._broken)

    def _measure(self, procs: list) -> None:
        """Take the memory of workers that have finished loading."""
        sizes = []
        for proc in procs:
            slot = getattr(proc, "_knox_slot", -1)
            if slot < 0 or self._ready is None or not self._ready[slot]:
                continue
            if not proc.is_alive():
                continue
            size = process_private_bytes(proc.pid)
            if size:
                sizes.append(size)
        if sizes:
            sizes.sort()
            self._measured = sizes[len(sizes) // 2]
            _trace(f"layout worker holds {self._measured >> 20} MB once loaded")

    def _grow(self) -> None:
        """Add workers in waves while memory allows and more are wanted."""
        try:
            while True:
                with self._lock:
                    if (self._quit is None or self._quit.is_set()
                            or self._leases <= 0 or self._broken):
                        return
                    self._prune()
                    need = self._want - len(self._procs)
                    generation = self.generation
                if need <= 0:
                    return
                status = installed_memory()
                count = min(need, max(_WAVE, (os.cpu_count() or 1) // 2))
                if status:
                    total, free, room = status
                    spare = min(free, room) - keep_free(total)
                    fit = int(spare // self.worker_bytes())
                    if fit <= 0:
                        _trace("layout service: free memory limits the workers "
                               f"to {self.alive_count()}")
                        return
                    count = min(count, fit)
                with self._lock:
                    if generation != self.generation or self._claims is None:
                        return
                    try:
                        started = self._spawn(count)
                    except Exception as exc:  # noqa: BLE001
                        _trace(f"layout service: could not add workers: {exc}")
                        return
                self._await_ready(started)
                self._measure(started)
        finally:
            with self._lock:
                self._grower = None

    def maintain(self) -> None:
        """Once a second: forget the dead, take their error reports, refill."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_maintain < 1.0:
                return
            self._last_maintain = now
            self._prune()
            if self._batches > 0 and not self._broken:
                self._ensure_grower()
        self.drain_errors()

    # -- the work --------------------------------------------------------

    def post(self, name: str, n_jobs: int, n_chunks: int) -> int:
        """Queue every chunk of a piece. Returns this service's generation."""
        with self._lock:
            claims = self._claims
            generation = self.generation
        if claims is None:
            raise Unavailable("the layout service is not running")
        for c in range(n_chunks):
            claims.put((name, n_jobs, n_chunks, c))
        return generation

    def drain_errors(self) -> None:
        errors = self._errors
        if errors is None:
            return
        while True:
            try:
                name, j, text, fatal = errors.get_nowait()
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                break
            with self._err_lock:
                self._errs.setdefault(name, {})[j] = (text, fatal)

    def errors_of(self, name: str) -> dict:
        with self._err_lock:
            return dict(self._errs.get(name, {}))

    def forget(self, name: str) -> None:
        with self._err_lock:
            self._errs.pop(name, None)

    def fatal_of(self, name: str) -> str | None:
        with self._err_lock:
            for text, fatal in self._errs.get(name, {}).values():
                if fatal:
                    return text
        return None

    # -- stopping --------------------------------------------------------

    def shutdown(self, kill: bool = False) -> None:
        """Stop every worker and drop the queues. The next prepare starts over."""
        with self._lock:
            procs = list(self._procs)
            self._procs = []
            quit_event = self._quit
            claims, errors = self._claims, self._errors
            self._claims = self._errors = self._quit = self._ready = None
            self._want = 0
            self.generation += 1
        if quit_event is not None:
            try:
                quit_event.set()
            except Exception:  # noqa: BLE001
                pass
        deadline = time.monotonic() + (0.0 if kill else 5.0)
        for proc in procs:
            try:
                proc.join(max(0.0, deadline - time.monotonic()))
            except Exception:  # noqa: BLE001
                pass
        for proc in procs:
            try:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(3.0)
            except Exception:  # noqa: BLE001
                pass
        for q in (claims, errors):
            if q is None:
                continue
            try:
                q.cancel_join_thread()
                q.close()
            except Exception:  # noqa: BLE001
                pass

    def hard_reset(self) -> None:
        """Kill every worker now. Used when a stop is not being noticed."""
        _trace("layout service: workers did not stop; ending them")
        self.shutdown(kill=True)


def service() -> Service:
    global _service
    with _service_lock:
        if _service is None:
            _service = Service()
            atexit.register(_at_exit)
        return _service


def _at_exit() -> None:
    svc = _service
    if svc is not None:
        try:
            svc.shutdown(kill=True)
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def lease():
    """Hold the workers for the length of the block (see Service.lease)."""
    with service().lease() as svc:
        yield svc


def running() -> bool:
    svc = _service
    return svc is not None and svc.alive_count() > 0


# ---------------------------------------------------------------------------
# One piece's buildings.

class _Batch:
    """The buildings of one piece, in a shared block, being laid out."""

    def __init__(self, svc: Service, jobs: list, workers: int):
        self.svc = svc
        self.jobs = jobs
        self.n = len(jobs)
        self.local: dict[int, tuple] = {}
        self.shm = None
        self.buf = None
        self.generation = svc.generation

        n = self.n
        cost = [max(1, job[0] * job[1]) * max(1, job[2]) for job in jobs]
        order = sorted(range(n), key=cost.__getitem__, reverse=True)
        target = max(1, sum(cost) // max(1, workers * CHUNKS_PER_WORKER))
        chunks: list[list[int]] = []
        current: list[int] = []
        spent = 0
        for j in order:
            current.append(j)
            spent += cost[j]
            if spent >= target or len(current) >= MAX_CHUNK_JOBS:
                chunks.append(current)
                current = []
                spent = 0
        if current:
            chunks.append(current)
        self.n_chunks = len(chunks)

        registry: list = []
        seen: dict = {}

        def intern(obj) -> int:
            key = _fingerprint(obj)
            at = seen.get(key)
            if at is None:
                at = len(registry)
                registry.append(obj)
                seen[key] = at
            return at

        parts: list[bytes] = [b""]
        for idxs in chunks:
            rows = []
            for j in idxs:
                job = jobs[j]
                rows.append(job[:7] + (intern(job[7]), intern(job[8])) + job[9:])
            parts.append(pickle.dumps((idxs, rows), protocol=pickle.HIGHEST_PROTOCOL))
        parts[0] = pickle.dumps(registry, protocol=pickle.HIGHEST_PROTOCOL)

        self.layout = lay = _Layout(n, self.n_chunks)
        table = [0]
        for part in parts:
            table.append(table[-1] + len(part))
        self.shm = shared_memory.SharedMemory(create=True, size=max(1, lay.blob + table[-1]))
        self.name = self.shm.name
        self.buf = buf = self.shm.buf
        buf[:lay.table] = bytes(lay.table)
        struct.pack_into(f"<{len(table)}q", buf, lay.table, *table)
        for k, part in enumerate(parts):
            at = lay.blob + table[k]
            buf[at:at + len(part)] = part

    # -- reading the block ------------------------------------------------

    def _snapshot(self) -> bytes:
        lay = self.layout
        return bytes(self.buf[lay.status:lay.status + self.n])

    def _set(self, j: int, state: int) -> None:
        self.buf[self.layout.status + j] = state

    def _cancel(self) -> None:
        if self.buf is not None:
            self.buf[0] = 1

    def _wait_quiet(self, seconds: float) -> bool:
        """Wait for the workers to put their buildings down. False if they did not."""
        deadline = time.monotonic() + seconds
        while True:
            if self._snapshot().count(ACTIVE) == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _abort(self) -> None:
        self._cancel()
        if not self._wait_quiet(5.0):
            self.svc.hard_reset()

    # -- the loop ---------------------------------------------------------

    def run(self, should_stop, on_done, on_state) -> list:
        svc = self.svc
        n = self.n
        self.generation = svc.post(self.name, n, self.n_chunks)
        svc.batch_started()
        try:
            return self._watch(should_stop, on_done, on_state)
        finally:
            svc.batch_ended()

    def _report(self, on_done, finished: int, busy: int) -> None:
        if on_done is None:
            return
        try:
            on_done(finished, self.n, max(1, busy), max(1, self.svc.alive_count()))
        except knoxstop.Stopped:
            self._abort()
            raise

    def _paint(self, on_state, snap: bytes) -> None:
        if on_state is None:
            return
        active = {j for j in range(self.n) if snap[j] == ACTIVE}
        completed = {j for j in range(self.n) if snap[j] in (DONE, FAILED)}
        on_state(active, completed)

    def _run_local(self, indexes, should_stop, on_done, cancel_first: bool) -> None:
        """Lay these buildings out in this process."""
        if cancel_first:
            self._abort()
        snap = self._snapshot()
        todo = [j for j in indexes if snap[j] not in (DONE, FAILED)]
        if not todo:
            return
        _trace(f"layout service: laying out {len(todo)} buildings in this process")
        for j in todo:
            knoxstop.check(should_stop, "the buildings")
            try:
                result = make_one(self.jobs[j], should_stop)
            except knoxstop.Stopped:
                raise
            self.local[j] = result
            self._set(j, FAILED if result[3] else DONE)
            snap = self._snapshot()
            self._report(on_done, snap.count(DONE) + snap.count(FAILED), 1)

    def _watch(self, should_stop, on_done, on_state) -> list:
        svc = self.svc
        lay = self.layout
        n = self.n
        started = time.monotonic()
        last_move = started
        last_check = started
        last_paint = 0.0
        last_finished = -1
        seen_deaths = svc.deaths
        starved_since = None
        no_workers_since = None
        ever_started = False
        while True:
            now = time.monotonic()
            snap = self._snapshot()
            finished = snap.count(DONE) + snap.count(FAILED)
            active = snap.count(ACTIVE)
            claimed = snap.count(CLAIMED)
            if finished != last_finished:
                last_finished = finished
                last_move = now
                self._report(on_done, finished, active)
            if finished >= n:
                break
            if (should_stop is not None and should_stop()) or svc.quitting():
                self._abort()
                raise knoxstop.Stopped("the buildings")
            svc.maintain()
            fatal = svc.fatal_of(self.name)
            if fatal:
                self._abort()
                raise OSError(fatal)
            if active or claimed or finished:
                ever_started = True

            if svc.generation != self.generation:
                # The workers were ended under this piece, and the chunks that
                # were waiting went with them.
                self._run_local(range(n), should_stop, on_done, True)
                continue

            if svc.alive_count() == 0:
                no_workers_since = no_workers_since or now
                if now - no_workers_since >= 15.0:
                    print("  (layout workers are gone; laying out the rest in this process)")
                    self._run_local(range(n), should_stop, on_done, True)
                    continue
            else:
                no_workers_since = None

            if now - last_move >= STALL_S:
                print("  (layout workers produced no rooms; laying out in this process)")
                _trace("layout workers produced no rooms; laying out in this process")
                self._run_local(range(n), should_stop, on_done, True)
                continue

            if ever_started and not active and not claimed:
                # Nobody holds a building yet some are not done: their chunk
                # was lost on the way to a worker.
                starved_since = starved_since or now
                if now - starved_since >= 20.0:
                    starved_since = None
                    lost = [j for j in range(n) if snap[j] == PENDING]
                    self._run_local(lost, should_stop, on_done, False)
                    continue
            else:
                starved_since = None

            if svc.deaths != seen_deaths or now - last_check >= 10.0:
                seen_deaths = svc.deaths
                last_check = now
                held = [j for j in range(n) if snap[j] in (CLAIMED, ACTIVE)]
                if held:
                    pids = svc.alive_pids()
                    who = bytes(self.buf[lay.who:lay.who + 4 * n])
                    orphans = [j for j in held
                               if struct.unpack_from("<I", who, 4 * j)[0] not in pids]
                    if orphans:
                        _trace(f"layout service: {len(orphans)} buildings were held "
                               "by a worker that has gone")
                        self._run_local(orphans, should_stop, on_done, False)
                        continue

            if finished > 0 and now - last_paint >= 2.0:
                last_paint = now
                self._paint(on_state, snap)
            time.sleep(0.15)

        self._paint(on_state, self._snapshot())
        return self._results()

    def _results(self) -> list:
        svc = self.svc
        lay = self.layout
        buf = self.buf
        snap = self._snapshot()
        # A building's reason travels on its own queue, a little behind its
        # status. Give the last of them a moment to arrive.
        errors: dict = {}
        wanted = snap.count(FAILED) - sum(1 for j, r in self.local.items() if r[3])
        deadline = time.monotonic() + 3.0
        while True:
            svc.drain_errors()
            errors = svc.errors_of(self.name)
            if len(errors) >= wanted or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        out = []
        for j in range(self.n):
            if j in self.local:
                out.append(self.local[j])
            elif snap[j] == DONE:
                storeys, rooms, furniture = struct.unpack_from("<3i", buf, lay.stats + 12 * j)
                vals = struct.unpack_from(f"<{_DOOR_INTS}h", buf,
                                          lay.doors + 2 * _DOOR_INTS * j)
                doors = None
                if vals[0] > 0:
                    doors = [tuple(vals[1 + 4 * i:5 + 4 * i])
                             for i in range(vals[0] - 1)]
                out.append((storeys, rooms, furniture, None, doors))
            else:
                text = errors.get(j, ("the building could not be laid out", False))[0]
                out.append((0, 0, 0, text, None))
        return out

    def close(self) -> None:
        self.svc.forget(self.name)
        shm = self.shm
        self.shm = None
        self.buf = None
        if shm is None:
            return
        try:
            shm.close()
        except (BufferError, OSError, ValueError):
            pass
        try:
            shm.unlink()
        except (FileNotFoundError, OSError):
            pass


def run(jobs: list, should_stop=None, on_done=None, on_state=None) -> list:
    """Lay out every building on the workers. Results are in `jobs` order.

    Each result is (storeys, rooms, furniture, error, doors). Raises
    Unavailable when no worker can be started (the caller does it itself),
    knoxstop.Stopped on Stop, and OSError when a file cannot be written.
    """
    svc = service()
    with svc.lease():
        want = budget()
        svc.prepare(want)
        try:
            batch = _Batch(svc, jobs, want)
        except (OSError, ValueError) as exc:
            raise Unavailable(f"shared memory is not available: {exc}") from exc
        try:
            return batch.run(should_stop, on_done, on_state)
        finally:
            batch.close()
