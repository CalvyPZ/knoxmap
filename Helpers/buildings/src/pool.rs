//! The pool of buildings, and the threads that empty it.
//!
//! Every building of every mod of the map is in one pool. A worker takes the
//! next one with a single atomic add, lays it out, writes its .tbx, and takes
//! another. There is no per-mod pool, no queue of messages, and no handing
//! work back and forth: the only shared mutable state is a counter, a status
//! byte per building, and the list of finished records.
//!
//! Because the work is native, the threads run at the same time for real.
//! Python makes one call and waits; the interpreter lock it holds is irrelevant
//! here, and nothing in this file ever calls back into Python.

use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};

use crate::blob::Blob;
use crate::generate;
use crate::catalog::Catalog;
use crate::party;
use crate::results::{
    Finished, STATUS_ACTIVE, STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED, STATUS_PENDING,
    STATUS_UNSUPPORTED,
};
use crate::scratch::Scratch;

/// What the window shows while the map is being built. `#[repr(C)]`: Python
/// reads it through a ctypes.Structure.
#[derive(Clone, Copy, Default)]
#[repr(C)]
pub struct Progress {
    pub total: u64,
    pub done: u64,
    pub failed: u64,
    pub unsupported: u64,
    pub active: u64,
    pub claimed: u64,
    pub workers: u32,
    /// 1 once every worker has stopped, whether finished or cancelled.
    pub stopped: u32,
    /// What this session holds in native memory, in bytes: the one buffer, the
    /// tables every worker shares, and the scratch the threads reuse.
    pub scratch_bytes: u64,
}

pub struct Pool {
    pub blob: Blob,
    /// Where every footprint of every piece is, so a building can find the
    /// buildings it shares a wall with. Built once; read by every worker.
    party: party::Index,
    /// The tile, furniture and style tables, read once and shared by every
    /// worker: this is the memory a Python worker process used to need its own
    /// copy of.
    catalog: Catalog,
    /// One byte per building, so the window can paint which are being done
    /// without taking any lock.
    status: Box<[AtomicU8]>,
    /// The next place in the blob's order list. One add per building claimed.
    next: AtomicUsize,
    done: AtomicU64,
    failed: AtomicU64,
    unsupported: AtomicU64,
    active: AtomicU64,
    cancel: AtomicBool,
    live: AtomicU32,
    scratch_bytes: AtomicU64,
    finished: Mutex<Vec<Finished>>,
}

impl Pool {
    /// Read the tables and bucket the footprints. Fails only on a bad catalog,
    /// which is one error back to Python rather than a thousand failed
    /// buildings.
    pub fn new(blob: Blob) -> crate::err::Result<Pool> {
        let catalog = Catalog::parse(&blob)?;
        let n = blob.n_jobs();
        let mut status = Vec::with_capacity(n);
        for _ in 0..n {
            status.push(AtomicU8::new(STATUS_PENDING));
        }
        let party = party::Index::build(&blob);
        Ok(Pool {
            blob,
            party,
            catalog,
            status: status.into_boxed_slice(),
            next: AtomicUsize::new(0),
            done: AtomicU64::new(0),
            failed: AtomicU64::new(0),
            unsupported: AtomicU64::new(0),
            active: AtomicU64::new(0),
            cancel: AtomicBool::new(false),
            live: AtomicU32::new(0),
            scratch_bytes: AtomicU64::new(0),
            finished: Mutex::new(Vec::new()),
        })
    }

    pub fn n_jobs(&self) -> usize {
        self.blob.n_jobs()
    }

    pub fn cancel(&self) {
        self.cancel.store(true, Ordering::Relaxed);
    }

    pub fn cancelled(&self) -> bool {
        self.cancel.load(Ordering::Relaxed)
    }

    pub fn progress(&self) -> Progress {
        let live = self.live.load(Ordering::Relaxed);
        Progress {
            total: self.n_jobs() as u64,
            done: self.done.load(Ordering::Relaxed),
            failed: self.failed.load(Ordering::Relaxed),
            unsupported: self.unsupported.load(Ordering::Relaxed),
            active: self.active.load(Ordering::Relaxed),
            claimed: self.next.load(Ordering::Relaxed) as u64,
            workers: live,
            stopped: if live == 0 { 1 } else { 0 },
            scratch_bytes: self.scratch_bytes.load(Ordering::Relaxed)
                + self.blob.byte_len() as u64
                + self.party.bytes() as u64
                + self.catalog.bytes() as u64,
        }
    }

    /// The status byte of one building, for the live preview.
    pub fn status_at(&self, index: usize) -> u8 {
        match self.status.get(index) {
            Some(cell) => cell.load(Ordering::Relaxed),
            None => STATUS_PENDING,
        }
    }

    /// Copy every status byte out in one call, which is what the preview wants.
    pub fn statuses_into(&self, out: &mut [u8]) {
        for (i, slot) in out.iter_mut().enumerate() {
            *slot = self.status_at(i);
        }
    }

    pub fn take_finished(&self) -> Vec<Finished> {
        match self.finished.lock() {
            Ok(mut held) => std::mem::take(&mut *held),
            // A worker panicked while holding the list. The records already in
            // it are still sound, and the ones missing come back as pending,
            // which Python lays out itself.
            Err(poisoned) => std::mem::take(&mut *poisoned.into_inner()),
        }
    }

    fn push(&self, item: Finished) {
        match item.status {
            STATUS_DONE => {
                self.done.fetch_add(1, Ordering::Relaxed);
            }
            STATUS_FAILED => {
                self.failed.fetch_add(1, Ordering::Relaxed);
            }
            STATUS_UNSUPPORTED => {
                self.unsupported.fetch_add(1, Ordering::Relaxed);
            }
            _ => {}
        }
        if let Some(cell) = self.status.get(item.index as usize) {
            cell.store(item.status, Ordering::Relaxed);
        }
        if let Ok(mut held) = self.finished.lock() {
            held.push(item);
        }
    }

    /// One worker's whole life: claim, build, repeat until the pool is empty.
    fn work(self: &Arc<Pool>) {
        let mut scratch = Scratch::new();
        let mut counted = 0usize;
        let n = self.n_jobs();
        loop {
            if self.cancelled() {
                break;
            }
            let at = self.next.fetch_add(1, Ordering::Relaxed);
            if at >= n {
                break;
            }
            let index = self.blob.order_at(at) as usize;
            if let Some(cell) = self.status.get(index) {
                cell.store(STATUS_ACTIVE, Ordering::Relaxed);
            }
            self.active.fetch_add(1, Ordering::Relaxed);
            scratch.reset();
            let outcome = generate::one(
                &self.blob,
                &self.catalog,
                &self.party,
                index,
                &mut scratch,
                || {
                    self.cancelled()
                },
            );
            self.active.fetch_sub(1, Ordering::Relaxed);
            match outcome {
                generate::Outcome::Built(mut item) => {
                    item.index = index as u32;
                    self.push(item);
                }
                generate::Outcome::Cancelled => {
                    if let Some(cell) = self.status.get(index) {
                        cell.store(STATUS_CANCELLED, Ordering::Relaxed);
                    }
                    break;
                }
            }
            // The report is what this thread holds, kept roughly up to date
            // rather than on every building: it only grows a few times.
            let held = scratch.bytes();
            if held > counted {
                self.scratch_bytes
                    .fetch_add((held - counted) as u64, Ordering::Relaxed);
                counted = held;
            }
        }
        if counted > 0 {
            self.scratch_bytes
                .fetch_sub(counted as u64, Ordering::Relaxed);
        }
        self.live.fetch_sub(1, Ordering::Relaxed);
    }

    /// Start `workers` threads. Returns how many actually started.
    pub fn start(self: &Arc<Pool>, workers: usize, low_priority: bool) -> Vec<JoinHandle<()>> {
        let mut handles = Vec::with_capacity(workers);
        for i in 0..workers {
            let me = Arc::clone(self);
            self.live.fetch_add(1, Ordering::Relaxed);
            let spawned = thread::Builder::new()
                .name(format!("knox-build-{i}"))
                // A building needs a few hundred kilobytes; the recursion in
                // the room splitting is bounded by MAX_DEPTH. 1 MB is room to
                // spare and keeps a hundred threads cheap.
                .stack_size(1 << 20)
                .spawn(move || {
                    if low_priority {
                        lower_priority();
                    }
                    me.work();
                });
            match spawned {
                Ok(handle) => handles.push(handle),
                Err(_) => {
                    // The machine will not give us another thread. Whatever
                    // started is enough to empty the pool, only slower.
                    self.live.fetch_sub(1, Ordering::Relaxed);
                    break;
                }
            }
        }
        handles
    }
}

/// How many workers to run when Python does not say.
///
/// One core stays free for the rest of the program. A machine with one core
/// still lays buildings out on that core.
pub fn default_workers() -> usize {
    let cores = thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    cores.saturating_sub(1).max(1)
}

#[cfg(windows)]
fn lower_priority() {
    // Below normal, so the window's own work (serving the map, drawing the
    // preview) is never waiting behind a building. Declared here rather than
    // taken from a crate: it is two calls, and this crate has no dependencies.
    const BELOW_NORMAL: i32 = -1;
    extern "system" {
        fn GetCurrentThread() -> isize;
        fn SetThreadPriority(thread: isize, priority: i32) -> i32;
    }
    unsafe {
        SetThreadPriority(GetCurrentThread(), BELOW_NORMAL);
    }
}

#[cfg(not(windows))]
fn lower_priority() {
    // Thread priority on Linux and macOS needs a scheduling policy change that
    // an unprivileged process is not granted. The threads run as they are.
}
