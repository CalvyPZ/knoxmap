//! KnoxMap terrain: the ground of every map of every mod, drawn in one library
//! with one lot of memory and every core on the machine.
//!
//! Python still decides what is on the ground. It reads the OSM extract, works
//! out that a way is a residential street and that a building belongs to a plot,
//! cuts junctions and bridges and house plots with GEOS, and projects every
//! vertex to tiles. What it no longer does is put the pixels down. It hands over
//! one buffer holding an ordered painting program for every piece of ground in
//! the job, and this library draws all of them, writes their bitmaps, and hands
//! back the one plane Python still writes itself.
//!
//! Why it is worth leaving Python for. Drawing the ground is millions of polygon
//! fills into three pictures, and in Python that was PIL holding the interpreter
//! lock, so more cores only helped by running more maps at once - and each of
//! those held another full set of pictures, which is four bytes a pixel times
//! three. A county map is a quarter of a billion tiles. Here the pictures are one
//! allocation of about seven bytes a tile, every core draws into it at once
//! because each has its own band of rows, and the memory does not move when the
//! thread count does.
//!
//! The boundary. Thirteen calls, no callbacks, nothing clever: open a session on
//! the buffer, start it, poll it, collect the finished pieces, close it. No
//! thread in here ever touches a Python object, so there is nothing for the
//! interpreter lock to serialise and nothing that can deadlock against it. A
//! panic is caught at the edge and comes back as an error code.

mod band;
mod blob;
mod bmp;
mod canvas;
mod classfile;
mod classify;
mod err;
mod paint;
mod passes;
mod piece;
mod pool;
mod raster;
mod rng;
mod spawn;

use std::os::raw::c_char;

use err::Error;

pub use pool::Session;

/// The format this build speaks. Python checks it before sending anything.
pub extern "C" fn knox_t_abi_version() -> u32 {
    blob::ABI_VERSION
}

/// How many cores the machine has, so Python can size its own work the same way.
pub extern "C" fn knox_t_cores() -> u32 {
    pool::cores() as u32
}

/// Open a session on a buffer.
///
/// `threads` is how many to use, or zero for every core but one. `budget` is how many
/// bytes of pictures the library may hold at once, or zero for no limit. The
/// buffer must stay where it is, and stay unchanged, until `knox_t_close`.
///
/// Returns null and writes a code to `fault` if the buffer will not do.
///
/// # Safety
/// `bytes` must point to `len` readable bytes that outlive the session.
pub unsafe extern "C" fn knox_t_open(
    bytes: *const u8,
    len: usize,
    threads: u32,
    budget: u64,
    fault: *mut u32,
) -> *mut Session {
    let opened = std::panic::catch_unwind(|| Session::open(bytes, len, threads as usize, budget));
    let answer = match opened {
        Ok(Ok(session)) => return Box::into_raw(Box::new(session)),
        Ok(Err(wrong)) => wrong.code(),
        Err(_) => Error::Panic.code(),
    };
    if !fault.is_null() {
        *fault = answer;
    }
    std::ptr::null_mut()
}

/// Start drawing. Returns at once; zero means the threads are away.
///
/// # Safety
/// `handle` must come from `knox_t_open` and not have been closed.
pub unsafe extern "C" fn knox_t_start(handle: *mut Session) -> u32 {
    let Some(session) = handle.as_mut() else {
        return Error::BadState.code();
    };
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.start())) {
        Ok(Ok(())) => 0,
        Ok(Err(wrong)) => wrong.code(),
        Err(_) => Error::Panic.code(),
    }
}

/// Wait up to `ms` for the job to finish. One if it has, zero if it is still
/// going.
///
/// # Safety
/// As `knox_t_start`.
pub unsafe extern "C" fn knox_t_wait(handle: *const Session, ms: u64) -> u32 {
    match handle.as_ref() {
        Some(session) => u32::from(session.wait(ms)),
        None => 1,
    }
}

/// Stop as soon as every thread notices. The pieces already drawn keep their
/// files; the rest come back as stopped.
///
/// # Safety
/// As `knox_t_start`.
pub unsafe extern "C" fn knox_t_cancel(handle: *const Session) {
    if let Some(session) = handle.as_ref() {
        session.cancel();
    }
}

/// How much of the work is done, in bands.
///
/// # Safety
/// As `knox_t_start`. `done` and `total` may be null.
pub unsafe extern "C" fn knox_t_progress(handle: *const Session, done: *mut u64, total: *mut u64) {
    let (did, all) = match handle.as_ref() {
        Some(session) => session.progress(),
        None => (0, 0),
    };
    if !done.is_null() {
        *done = did;
    }
    if !total.is_null() {
        *total = all;
    }
}

/// How many pieces of ground the buffer held.
///
/// # Safety
/// As `knox_t_start`.
pub unsafe extern "C" fn knox_t_piece_count(handle: *const Session) -> u32 {
    match handle.as_ref() {
        Some(session) => session.piece_count() as u32,
        None => 0,
    }
}

/// What one piece is doing: waiting, drawing, done, failed or stopped.
///
/// # Safety
/// As `knox_t_start`.
pub unsafe extern "C" fn knox_t_status(handle: *const Session, at: u32) -> u32 {
    match handle.as_ref() {
        Some(session) => session.status(at as usize) as u32,
        None => pool::WAITING as u32,
    }
}

/// Why one piece failed, or zero.
///
/// # Safety
/// As `knox_t_start`.
pub unsafe extern "C" fn knox_t_fault(handle: *const Session, at: u32) -> u32 {
    match handle.as_ref() {
        Some(session) => session.fault(at as usize),
        None => 0,
    }
}

/// Collect the next finished piece, if one is waiting. One if a piece came back.
///
/// The biome plane is the one output Python still writes itself - the cell PNGs
/// it cuts from it need a deflate, and the one in Python is good - so it is
/// handed over rather than written here. The bytes stay valid until
/// `knox_t_release` is called for that piece, and until then they count against
/// the memory budget, which is what keeps the pool from running ahead of Python.
///
/// # Safety
/// As `knox_t_start`. The out pointers may be null.
pub unsafe extern "C" fn knox_t_next(
    handle: *const Session,
    index: *mut u32,
    width: *mut u32,
    height: *mut u32,
    biome: *mut *const u8,
    len: *mut usize,
) -> u32 {
    let Some(session) = handle.as_ref() else {
        return 0;
    };
    let Some((at, wide, high, bytes, count)) = session.lend() else {
        return 0;
    };
    if !index.is_null() {
        *index = at as u32;
    }
    if !width.is_null() {
        *width = wide;
    }
    if !height.is_null() {
        *height = high;
    }
    if !biome.is_null() {
        *biome = bytes;
    }
    if !len.is_null() {
        *len = count;
    }
    1
}

/// Give a collected piece's memory back.
///
/// # Safety
/// As `knox_t_start`. The pointer handed out for this piece must not be read
/// again.
pub unsafe extern "C" fn knox_t_release(handle: *const Session, index: u32) -> u32 {
    match handle.as_ref() {
        Some(session) => match session.release(index as usize) {
            Ok(()) => 0,
            Err(wrong) => wrong.code(),
        },
        None => Error::BadState.code(),
    }
}

/// The most memory the pool has held at once, and what it is holding now. This
/// is the number the whole design is for, so it is worth being able to read it.
///
/// # Safety
/// As `knox_t_start`. Either pointer may be null.
pub unsafe extern "C" fn knox_t_memory(handle: *const Session, peak: *mut u64, now: *mut u64) {
    let (most, held) = match handle.as_ref() {
        Some(session) => session.memory(),
        None => (0, 0),
    };
    if !peak.is_null() {
        *peak = most;
    }
    if !now.is_null() {
        *now = held;
    }
}

/// How the pool decided to spend itself: threads in all, and how many pieces it
/// is willing to hold at once.
///
/// # Safety
/// As `knox_t_start`. Either pointer may be null.
pub unsafe extern "C" fn knox_t_plan(handle: *const Session, threads: *mut u32, at_once: *mut u32) {
    let (all, pieces) = match handle.as_ref() {
        Some(session) => (session.threads(), session.drivers()),
        None => (0, 0),
    };
    if !threads.is_null() {
        *threads = all as u32;
    }
    if !at_once.is_null() {
        *at_once = pieces as u32;
    }
}

/// Plain English for a code, nul-terminated and static.
pub extern "C" fn knox_t_error_text(code: u32) -> *const c_char {
    err::text_for(code).as_ptr() as *const c_char
}

/// Stop everything, join the threads, free the pictures. The buffer Python sent
/// is only released when this returns.
///
/// # Safety
/// `handle` must come from `knox_t_open` and must not be used again.
pub unsafe extern "C" fn knox_t_close(handle: *mut Session) {
    if handle.is_null() {
        return;
    }
    let session = Box::from_raw(handle);
    let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || drop(session)));
}
