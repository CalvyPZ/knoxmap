//! KnoxMap's building generator, as a library the Python program calls once.
//!
//! What it is for. Laying out rooms is the slowest part of building a map, and
//! in Python it can only be spread across cores by starting a separate process
//! per worker, because the interpreter lets one thread run Python at a time.
//! Every one of those processes loads its own copy of the tile catalog, so the
//! number of workers is limited by memory rather than by the processors. Here
//! there is no interpreter lock: the catalog is loaded once, every worker
//! thread reads that one copy, and the only limit left is how many cores the
//! machine has.
//!
//! How Python uses it (knoxbuild/native.py), through helper.dll:
//!
//! ```text
//!   knox_open(buffer)        every building of every mod, and the catalog
//!   knox_start(workers)      native threads, one pool, no messages
//!   knox_progress(...)       polled for the window; knox_statuses for the map
//!   knox_cancel()            Stop; workers put down what they hold
//!   knox_finish()            waits for the threads, lays the records out
//!   knox_results(...)        one record per building, in job order
//!   knox_close()
//! ```
//!
//! Memory. One session holds exactly three allocations that depend on the size
//! of the map: the buffer Python handed over, one status byte per building, and
//! one record per building. Everything a worker needs while it works lives in
//! that worker's `Scratch`, which is allocated once and reused, so twenty
//! workers cost twenty of the largest building rather than twenty catalogs.
//!
//! Safety at the boundary. Every entry point checks its pointers, catches a
//! panic rather than letting one unwind into Python, and does nothing if the
//! session is in the wrong state. Inside, the only shared mutable state is a
//! handful of atomics and one list behind a mutex, so no two threads ever hold
//! a pointer into the same memory.

mod blob;
mod catalog;
mod cores;
mod doors;
mod err;
mod furnish;
mod generate;
mod grid;
mod kinds;
mod plan;
mod party;
mod pool;
mod results;
mod rng;
mod scratch;
mod split;
mod storeys;
mod tbx;
mod uses;
mod windows;

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use blob::Blob;
use err::Error;
use pool::Pool;
use results::Table;

pub use pool::Progress;
pub use results::ResultRec;

pub use blob::ABI_VERSION;

/// What `knox_open` hands back. Opaque to Python: it only ever holds the
/// pointer and gives it back to the calls below.
pub struct Session {
    pool: Arc<Pool>,
    handles: Vec<JoinHandle<()>>,
    table: Option<Table>,
}

/// The version of the buffer format and of these calls. Python checks this
/// against its own number and refuses to use a DLL that does not match, which
/// is what stops a stale DLL beside a new program from writing nonsense.
pub extern "C" fn knox_abi_version() -> u32 {
    ABI_VERSION
}

/// Plain English for an error code. The pointer is to static bytes and stays
/// valid for the life of the program.
pub extern "C" fn knox_error_message(code: u32) -> *const u8 {
    err::text_for(code).as_ptr()
}

/// Take the buffer and make a session. The buffer is copied, so Python is free
/// to drop its own the moment this returns.
///
/// # Safety
/// `data` must point to `len` readable bytes, and `out_err` must be null or
/// point to a writable u32.
pub unsafe extern "C" fn knox_open(data: *const u8, len: usize, out_err: *mut u32) -> *mut Session {
    let set_err = |code: u32| {
        if !out_err.is_null() {
            *out_err = code;
        }
    };
    if data.is_null() || len == 0 {
        set_err(Error::Truncated.code());
        return std::ptr::null_mut();
    }
    let copied = std::slice::from_raw_parts(data, len).to_vec().into_boxed_slice();
    let opened = catch_unwind(AssertUnwindSafe(move || Blob::parse(copied)));
    let blob = match opened {
        Ok(Ok(blob)) => blob,
        Ok(Err(problem)) => {
            set_err(problem.code());
            return std::ptr::null_mut();
        }
        Err(_) => {
            set_err(Error::Panic.code());
            return std::ptr::null_mut();
        }
    };
    // The tables are read here too, so a catalog Python built wrongly is one
    // error on opening rather than every building failing in turn.
    let pool = match catch_unwind(AssertUnwindSafe(move || Pool::new(blob))) {
        Ok(Ok(pool)) => pool,
        Ok(Err(problem)) => {
            set_err(problem.code());
            return std::ptr::null_mut();
        }
        Err(_) => {
            set_err(Error::Panic.code());
            return std::ptr::null_mut();
        }
    };
    set_err(0);
    Box::into_raw(Box::new(Session {
        pool: Arc::new(pool),
        handles: Vec::new(),
        table: None,
    }))
}

/// How many buildings this session holds.
///
/// # Safety
/// `session` must be a pointer from `knox_open` that has not been closed.
pub unsafe extern "C" fn knox_job_count(session: *mut Session) -> u64 {
    match session.as_ref() {
        Some(held) => held.pool.n_jobs() as u64,
        None => 0,
    }
}

/// Start the workers. `workers` of 0 means every core but one.
/// Returns how many threads are running, or 0 when none could start.
///
/// # Safety
/// `session` must be a pointer from `knox_open` that has not been closed.
pub unsafe extern "C" fn knox_start(session: *mut Session, workers: u32, low_priority: u32) -> u32 {
    let held = match session.as_mut() {
        Some(held) => held,
        None => return 0,
    };
    if !held.handles.is_empty() {
        return held.handles.len() as u32;        // already running
    }
    let want = if workers == 0 {
        pool::default_workers()
    } else {
        workers as usize
    };
    // Never more threads than there are buildings: a thread with nothing to
    // claim is a thread that costs a stack for no reason.
    let want = want.max(1).min(held.pool.n_jobs().max(1));
    let started = catch_unwind(AssertUnwindSafe(|| {
        held.pool.start(want, low_priority != 0)
    }));
    match started {
        Ok(handles) => {
            held.handles = handles;
            held.handles.len() as u32
        }
        Err(_) => 0,
    }
}

/// Where the work has got to. Safe to call from any thread, as often as liked.
///
/// # Safety
/// `session` must be a pointer from `knox_open`, and `out` must point to a
/// writable `Progress`.
pub unsafe extern "C" fn knox_progress(session: *mut Session, out: *mut Progress) -> u32 {
    let held = match session.as_ref() {
        Some(held) => held,
        None => return Error::BadState.code(),
    };
    if out.is_null() {
        return Error::BadState.code();
    }
    *out = held.pool.progress();
    0
}

/// The status byte of every building, in job order, for the live preview.
/// Returns how many were written.
///
/// # Safety
/// `out` must point to `cap` writable bytes.
pub unsafe extern "C" fn knox_statuses(session: *mut Session, out: *mut u8, cap: usize) -> usize {
    let held = match session.as_ref() {
        Some(held) => held,
        None => return 0,
    };
    if out.is_null() {
        return 0;
    }
    let n = held.pool.n_jobs().min(cap);
    let into = std::slice::from_raw_parts_mut(out, n);
    held.pool.statuses_into(into);
    n
}

/// Ask every worker to stop. They put down the building they hold without
/// writing it, so nothing half-written is left on disk.
///
/// # Safety
/// `session` must be a pointer from `knox_open`.
pub unsafe extern "C" fn knox_cancel(session: *mut Session) {
    if let Some(held) = session.as_ref() {
        held.pool.cancel();
    }
}

/// Wait up to `timeout_ms` for the workers to stop. Returns 1 when they have.
/// Python polls this so that Stop and the progress line still work while a map
/// is being built.
///
/// # Safety
/// `session` must be a pointer from `knox_open`.
pub unsafe extern "C" fn knox_wait(session: *mut Session, timeout_ms: u32) -> u32 {
    let held = match session.as_ref() {
        Some(held) => held,
        None => return 1,
    };
    let deadline = Instant::now() + Duration::from_millis(timeout_ms as u64);
    loop {
        if held.pool.progress().stopped == 1 {
            return 1;
        }
        if Instant::now() >= deadline {
            return 0;
        }
        thread_sleep();
    }
}

fn thread_sleep() {
    std::thread::sleep(Duration::from_millis(2));
}

/// Join the threads and lay the records out in job order. Call it once the
/// workers have stopped; it blocks until they have.
///
/// # Safety
/// `session` must be a pointer from `knox_open`.
pub unsafe extern "C" fn knox_finish(session: *mut Session) -> u32 {
    let held = match session.as_mut() {
        Some(held) => held,
        None => return Error::BadState.code(),
    };
    for handle in std::mem::take(&mut held.handles) {
        // A worker that panicked has already been reported as that building
        // failing. There is nothing left to do but collect the thread.
        let _ = handle.join();
    }
    if held.table.is_none() {
        let n = held.pool.n_jobs();
        let finished = held.pool.take_finished();
        held.table = Some(Table::build(n, finished));
    }
    0
}

/// One record per building, in job order. Valid until `knox_close`.
///
/// # Safety
/// `out_recs` and `out_n` must be writable, and `knox_finish` must have been
/// called first.
pub unsafe extern "C" fn knox_results(
    session: *mut Session,
    out_recs: *mut *const ResultRec,
    out_n: *mut usize,
) -> u32 {
    let held = match session.as_ref() {
        Some(held) => held,
        None => return Error::BadState.code(),
    };
    let table = match held.table.as_ref() {
        Some(table) => table,
        None => return Error::BadState.code(),
    };
    if out_recs.is_null() || out_n.is_null() {
        return Error::BadState.code();
    }
    *out_recs = table.recs.as_ptr();
    *out_n = table.recs.len();
    0
}

/// The text the records' reasons point into. Valid until `knox_close`.
///
/// # Safety
/// `out_text` and `out_len` must be writable, and `knox_finish` must have been
/// called first.
pub unsafe extern "C" fn knox_error_text(
    session: *mut Session,
    out_text: *mut *const u8,
    out_len: *mut usize,
) -> u32 {
    let held = match session.as_ref() {
        Some(held) => held,
        None => return Error::BadState.code(),
    };
    let table = match held.table.as_ref() {
        Some(table) => table,
        None => return Error::BadState.code(),
    };
    if out_text.is_null() || out_len.is_null() {
        return Error::BadState.code();
    }
    *out_text = table.text.as_ptr();
    *out_len = table.text.len();
    0
}

/// Stop the workers, wait for them, and give every byte back.
///
/// # Safety
/// `session` must be a pointer from `knox_open` that has not been closed, and
/// must not be used afterwards.
pub unsafe extern "C" fn knox_close(session: *mut Session) {
    if session.is_null() {
        return;
    }
    let mut held = Box::from_raw(session);
    held.pool.cancel();
    for handle in std::mem::take(&mut held.handles) {
        let _ = handle.join();
    }
    drop(held);
}
