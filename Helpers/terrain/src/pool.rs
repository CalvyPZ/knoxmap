//! The pool: every piece of ground of every map, drawn with one lot of memory
//! and every core on the machine.
//!
//! There are two ways to spend cores here and the pool uses both. Inside a piece,
//! threads take bands of rows (see `piece`). Across pieces, several pieces can be
//! in hand at once. A piece draws one band of rows and keeps the biome plane, so
//! the pool sizes how many it can afford from that band plus the biome, and puts
//! every remaining core into the bands. One big map therefore gets all of them,
//! and forty mod maps get one piece a core without holding forty full pictures.
//!
//! Finished pieces are handed back one at a time rather than all at the end. The
//! biome plane is the one output Python still writes itself, so Rust keeps that
//! one plane alive, drops the rest of the piece, and waits if Python has fallen
//! behind - which is what keeps the memory flat however many maps are queued.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;

use crate::blob::Blob;
use crate::err::{Error, Result};
use crate::piece::{self, Finished};

/// What a piece is doing.
pub const WAITING: u8 = 0;
pub const DRAWING: u8 = 1;
pub const DONE: u8 = 2;
pub const FAILED: u8 = 3;
pub const STOPPED: u8 = 4;

/// Progress, and the one flag that stops everything.
pub struct Beat {
    done: AtomicU64,
    total: AtomicU64,
    stop: AtomicBool,
}

impl Beat {
    fn new() -> Beat {
        Beat {
            done: AtomicU64::new(0),
            total: AtomicU64::new(0),
            stop: AtomicBool::new(false),
        }
    }

    /// One band of one stage of one piece.
    #[inline]
    pub fn step(&self) {
        self.done.fetch_add(1, Ordering::Relaxed);
    }

    #[inline]
    pub fn stopped(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }

    fn plan(&self, units: u64) {
        self.total.fetch_add(units, Ordering::Relaxed);
    }

    pub fn progress(&self) -> (u64, u64) {
        (
            self.done.load(Ordering::Relaxed),
            self.total.load(Ordering::Relaxed),
        )
    }
}

/// The pieces that are finished and still in hand, and how much they weigh.
struct Held {
    /// Finished and waiting for Python to come and get it.
    ready: Vec<Finished>,
    /// Handed to Python and not given back yet. Python is reading these bytes,
    /// so they stay exactly where they are until it says it is done.
    lent: Vec<Finished>,
    bytes: u64,
    working: u64,
    peak: u64,
}

struct State {
    next: AtomicUsize,
    status: Vec<AtomicU8>,
    fault: Vec<AtomicU32>,
    held: Mutex<Held>,
    room: Condvar,
    finished: Mutex<bool>,
    all_done: Condvar,
    budget: u64,
}

impl State {
    /// Wait until there is room for another piece. If nothing is being held
    /// there is nothing to wait for, so a single piece larger than the whole
    /// budget still gets drawn rather than waiting for ever.
    fn make_room(&self, want: u64, beat: &Beat) {
        if self.budget == 0 {
            return;
        }
        let mut held = match self.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        while !beat.stopped()
            && !held.ready.is_empty()
            && held.bytes + held.working + want > self.budget
        {
            let waited = self.room.wait(held);
            held = match waited {
                Ok(held) => held,
                Err(poisoned) => poisoned.into_inner(),
            };
        }
        held.working += want;
        let now = held.bytes + held.working;
        if now > held.peak {
            held.peak = now;
        }
    }

    fn drop_working(&self, want: u64) {
        let mut held = match self.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        held.working = held.working.saturating_sub(want);
        self.room.notify_all();
    }

    fn put(&self, done: Finished) {
        let mut held = match self.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        held.bytes += done.bytes() as u64;
        held.ready.push(done);
        let now = held.bytes + held.working;
        if now > held.peak {
            held.peak = now;
        }
    }
}

/// One run of the library: the buffer, the threads, and what has come back.
pub struct Session {
    blob: Arc<Blob>,
    state: Arc<State>,
    beat: Arc<Beat>,
    threads: usize,
    drivers: usize,
    controller: Option<JoinHandle<()>>,
    started: bool,
}

impl Session {
    /// Check the buffer and work out how to spend the cores and the memory. No
    /// thread starts here: `start` does that, so that Python can look the plan
    /// over first.
    ///
    /// # Safety
    /// `bytes` must stay put and unchanged until the session is closed.
    pub unsafe fn open(bytes: *const u8, len: usize, threads: usize, budget: u64) -> Result<Session> {
        let blob = Blob::parse(bytes, len)?;
        let n = blob.n_pieces();
        let threads = if threads == 0 { allowed_cores() } else { threads };
        let drivers = plan_drivers(&blob, threads, budget)?;
        let beat = Beat::new();
        let band_threads = (threads / drivers).max(1);
        for at in 0..n {
            let piece = blob.piece(at)?;
            beat.plan(piece::work_units(&blob, &piece, band_threads));
        }
        let state = State {
            next: AtomicUsize::new(0),
            status: (0..n).map(|_| AtomicU8::new(WAITING)).collect(),
            fault: (0..n).map(|_| AtomicU32::new(0)).collect(),
            held: Mutex::new(Held {
                ready: Vec::new(),
                lent: Vec::new(),
                bytes: 0,
                working: 0,
                peak: 0,
            }),
            room: Condvar::new(),
            finished: Mutex::new(false),
            all_done: Condvar::new(),
            budget,
        };
        Ok(Session {
            blob: Arc::new(blob),
            state: Arc::new(state),
            beat: Arc::new(beat),
            threads,
            drivers,
            controller: None,
            started: false,
        })
    }

    pub fn piece_count(&self) -> usize {
        self.blob.n_pieces()
    }

    pub fn threads(&self) -> usize {
        self.threads
    }

    pub fn drivers(&self) -> usize {
        self.drivers
    }

    /// Start drawing. Returns at once; the work goes on in the pool's own
    /// threads, none of which ever calls back into Python.
    pub fn start(&mut self) -> Result<()> {
        if self.started {
            return Err(Error::BadState);
        }
        self.started = true;
        let blob = Arc::clone(&self.blob);
        let state = Arc::clone(&self.state);
        let beat = Arc::clone(&self.beat);
        let drivers = self.drivers;
        let band_threads = (self.threads / drivers).max(1);
        let controller = std::thread::Builder::new()
            .name("knox-terrain".to_string())
            .spawn(move || {
                run(blob, state.clone(), beat, drivers, band_threads);
                let mut flag = match state.finished.lock() {
                    Ok(flag) => flag,
                    Err(poisoned) => poisoned.into_inner(),
                };
                *flag = true;
                state.all_done.notify_all();
                // Anybody waiting for room is waiting for work that will never
                // come now.
                state.room.notify_all();
            })
            .map_err(|_| Error::BadState)?;
        self.controller = Some(controller);
        Ok(())
    }

    /// Wait up to `ms` for everything to finish. True if it has.
    pub fn wait(&self, ms: u64) -> bool {
        let flag = match self.state.finished.lock() {
            Ok(flag) => flag,
            Err(poisoned) => poisoned.into_inner(),
        };
        if *flag {
            return true;
        }
        let waited = self
            .state
            .all_done
            .wait_timeout(flag, std::time::Duration::from_millis(ms));
        match waited {
            Ok((guard, _)) => *guard,
            Err(poisoned) => *poisoned.into_inner().0,
        }
    }

    pub fn cancel(&self) {
        self.beat.stop();
        self.state.room.notify_all();
    }

    pub fn progress(&self) -> (u64, u64) {
        self.beat.progress()
    }

    pub fn status(&self, at: usize) -> u8 {
        self.state
            .status
            .get(at)
            .map(|status| status.load(Ordering::Relaxed))
            .unwrap_or(WAITING)
    }

    pub fn fault(&self, at: usize) -> u32 {
        self.state
            .fault
            .get(at)
            .map(|fault| fault.load(Ordering::Relaxed))
            .unwrap_or(0)
    }

    /// The next finished piece, if one is waiting: which piece it is, how big it
    /// is, and where its biome plane sits. The bytes stay put until `release`.
    pub fn lend(&self) -> Option<(usize, u32, u32, *const u8, usize)> {
        let mut held = match self.state.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        let done = held.ready.pop()?;
        let answer = (
            done.index,
            done.width,
            done.height,
            done.biome.as_ptr(),
            done.biome.len(),
        );
        held.lent.push(done);
        Some(answer)
    }

    /// Give a piece's memory back. Until this is called the pool counts those
    /// bytes against the budget, which is what stops a hundred queued maps from
    /// piling up in memory while Python writes their cell PNGs.
    pub fn release(&self, index: usize) -> Result<()> {
        let mut held = match self.state.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        let at = match held.lent.iter().position(|done| done.index == index) {
            Some(at) => at,
            None => return Err(Error::BadState),
        };
        let done = held.lent.swap_remove(at);
        held.bytes = held.bytes.saturating_sub(done.bytes() as u64);
        drop(done);
        drop(held);
        self.state.room.notify_all();
        Ok(())
    }

    /// The most memory the pool has held at once, and what it is holding now.
    pub fn memory(&self) -> (u64, u64) {
        let held = match self.state.held.lock() {
            Ok(held) => held,
            Err(poisoned) => poisoned.into_inner(),
        };
        (held.peak, held.bytes + held.working)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.beat.stop();
        self.state.room.notify_all();
        if let Some(controller) = self.controller.take() {
            let _ = controller.join();
        }
    }
}

/// How many pieces to hold at once. One if the budget will not take more, and
/// never more than there are pieces or cores.
fn plan_drivers(blob: &Blob, threads: usize, budget: u64) -> Result<usize> {
    let n = blob.n_pieces();
    if n <= 1 {
        return Ok(1);
    }
    let mut biggest = 0u64;
    for at in 0..n {
        let piece = blob.piece(at)?;
        let cost = piece::working_bytes(blob, &piece)?;
        biggest = biggest.max(cost);
    }
    if budget == 0 || biggest == 0 {
        return Ok(1);
    }
    let room = (budget / biggest).max(1) as usize;
    Ok(room.min(n).min(threads.max(1)))
}

/// Draw every piece, biggest first.
fn run(blob: Arc<Blob>, state: Arc<State>, beat: Arc<Beat>, drivers: usize, band_threads: usize) {
    let n = blob.n_pieces();
    if drivers <= 1 {
        drive(&blob, &state, &beat, band_threads, n);
        return;
    }
    let mut hands = Vec::with_capacity(drivers);
    for _ in 0..drivers {
        let blob = Arc::clone(&blob);
        let state = Arc::clone(&state);
        let beat = Arc::clone(&beat);
        let hand = std::thread::Builder::new()
            .name("knox-terrain-piece".to_string())
            .spawn(move || drive(&blob, &state, &beat, band_threads, n));
        match hand {
            Ok(hand) => hands.push(hand),
            // No thread to be had: this driver's share of the pieces will be
            // picked up by the others, because they all take from one counter.
            Err(_) => continue,
        }
    }
    for hand in hands {
        let _ = hand.join();
    }
    // If every spawn failed, draw the lot here rather than returning with
    // nothing done.
    if state.next.load(Ordering::Relaxed) == 0 {
        drive(&blob, &state, &beat, band_threads, n);
    }
}

/// One thread's worth of pieces.
fn drive(blob: &Blob, state: &State, beat: &Beat, band_threads: usize, n: usize) {
    loop {
        let at = state.next.fetch_add(1, Ordering::Relaxed);
        if at >= n {
            return;
        }
        let which = match blob.order_at(at) {
            Ok(which) => which,
            Err(_) => continue,
        };
        if beat.stopped() {
            set(state, which, STOPPED, Error::Cancelled.code());
            continue;
        }
        let want = match blob.piece(which) {
            Ok(piece) => piece::working_bytes(blob, &piece).unwrap_or_else(|_| {
                // A piece that will not price itself still has to be kept inside
                // the budget. The old full-canvas ceiling is the safe reserve.
                (piece.tiles() as u64).saturating_mul(9)
            }),
            Err(_) => 0,
        };
        state.make_room(want, beat);
        set(state, which, DRAWING, 0);
        let drawn = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            piece::render(blob, which, band_threads, beat)
        }));
        state.drop_working(want);
        match drawn {
            Ok(Ok(done)) => {
                set(state, which, DONE, 0);
                state.put(done);
            }
            Ok(Err(wrong)) => {
                let status = if wrong == Error::Cancelled { STOPPED } else { FAILED };
                set(state, which, status, wrong.code());
            }
            Err(_) => set(state, which, FAILED, Error::Panic.code()),
        }
    }
}

fn set(state: &State, at: usize, status: u8, fault: u32) {
    if let Some(slot) = state.status.get(at) {
        slot.store(status, Ordering::Relaxed);
    }
    if fault != 0 {
        if let Some(slot) = state.fault.get(at) {
            slot.store(fault, Ordering::Relaxed);
        }
    }
}

/// How many cores there are, or one if the machine will not say.
pub fn cores() -> usize {
    std::thread::available_parallelism()
        .map(|cores| cores.get())
        .unwrap_or(1)
}

/// How many cores a session may use when the caller asks for every core.
/// One core stays free for the rest of the program.
fn allowed_cores() -> usize {
    cores().saturating_sub(1).max(1)
}
