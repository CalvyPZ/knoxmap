//! What comes back for each building.
//!
//! A worker never writes into a shared array of records. It finishes a
//! building, takes the one lock there is for a moment, and pushes a record
//! onto a list; the records are laid out in job order once the threads have
//! stopped. One lock per building against milliseconds of work is free, and it
//! means there is no unsafe code here at all: no two threads ever hold a
//! pointer into the same memory.
//!
//! Live progress does not go through that lock. It is the per-building status
//! byte (`Pool::status`) and a few counters, all atomics, which is what the
//! window reads while the map is being built.

/// How many outside doors of a house travel back as numbers. A house with more
/// is read from its .tbx by the paths pass, as every house used to be.
pub const DOOR_SLOTS: usize = 6;
pub const DOOR_INTS: usize = DOOR_SLOTS * 4;

pub const STATUS_PENDING: u8 = 0;
pub const STATUS_ACTIVE: u8 = 1;
pub const STATUS_DONE: u8 = 2;
pub const STATUS_FAILED: u8 = 3;
/// Laid out by nothing here: a building this DLL cannot do yet, which Python
/// lays out itself. Not a failure, and not a reason to stop.
pub const STATUS_UNSUPPORTED: u8 = 4;
/// Claimed by a worker that was still holding it when Stop was asked.
pub const STATUS_CANCELLED: u8 = 5;

/// One building's answer, as Python reads it. `#[repr(C)]` because
/// knoxbuild/native.py mirrors this with a ctypes.Structure; the field order
/// and the sizes are the interface.
#[derive(Clone, Copy)]
#[repr(C)]
pub struct ResultRec {
    pub status: u8,
    /// How many of `doors` are filled in.
    pub door_n: u8,
    pub storeys: u16,
    pub rooms: u32,
    pub furniture: u32,
    /// Into the session's error text, in bytes. Zero length when there is none.
    pub error_off: u32,
    pub error_len: u32,
    /// (outside x, y, inside x, y) for each ground-floor outside door.
    pub doors: [i16; DOOR_INTS],
}

impl ResultRec {
    pub fn blank() -> ResultRec {
        ResultRec {
            status: STATUS_PENDING,
            door_n: 0,
            storeys: 0,
            rooms: 0,
            furniture: 0,
            error_off: 0,
            error_len: 0,
            doors: [0; DOOR_INTS],
        }
    }
}

/// A door as the layout found it, before it is flattened into `ResultRec`.
#[derive(Clone, Copy, Debug)]
pub struct Door {
    pub out_x: i16,
    pub out_y: i16,
    pub in_x: i16,
    pub in_y: i16,
}

/// What one building produced, pushed by the worker that did it.
pub struct Finished {
    pub index: u32,
    pub status: u8,
    pub storeys: u16,
    pub rooms: u32,
    pub furniture: u32,
    pub doors: Vec<Door>,
    /// Why it failed, for the line the build prints. None when it did not.
    pub error: Option<String>,
}

impl Finished {
    pub fn ok(index: u32, storeys: u16, rooms: u32, furniture: u32, doors: Vec<Door>) -> Finished {
        Finished {
            index,
            status: STATUS_DONE,
            storeys,
            rooms,
            furniture,
            doors,
            error: None,
        }
    }

    pub fn failed(index: u32, error: String) -> Finished {
        Finished {
            index,
            status: STATUS_FAILED,
            storeys: 0,
            rooms: 0,
            furniture: 0,
            doors: Vec::new(),
            error: Some(error),
        }
    }

}

/// The records in job order, and the text their reasons point into.
pub struct Table {
    pub recs: Vec<ResultRec>,
    pub text: Vec<u8>,
}

impl Table {
    /// Lay the pushed records out by job index. Called once, after the workers
    /// have stopped, so nothing is racing it.
    pub fn build(n_jobs: usize, finished: Vec<Finished>) -> Table {
        let mut recs = vec![ResultRec::blank(); n_jobs];
        let mut text: Vec<u8> = Vec::new();
        for item in finished {
            let at = item.index as usize;
            if at >= n_jobs {
                continue;
            }
            let rec = &mut recs[at];
            rec.status = item.status;
            rec.storeys = item.storeys;
            rec.rooms = item.rooms;
            rec.furniture = item.furniture;
            let n = item.doors.len().min(DOOR_SLOTS);
            rec.door_n = n as u8;
            for (i, door) in item.doors.iter().take(n).enumerate() {
                rec.doors[i * 4] = door.out_x;
                rec.doors[i * 4 + 1] = door.out_y;
                rec.doors[i * 4 + 2] = door.in_x;
                rec.doors[i * 4 + 3] = door.in_y;
            }
            if let Some(reason) = item.error {
                rec.error_off = text.len() as u32;
                rec.error_len = reason.len() as u32;
                text.extend_from_slice(reason.as_bytes());
            }
        }
        Table { recs, text }
    }
}
