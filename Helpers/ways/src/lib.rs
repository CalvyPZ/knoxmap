//! Picking the roads and buildings inside a selection.
//!
//! The daily is read in `knox_clip` when that library is present. When Python
//! is the reader, this crate still measures each way that might be painted:
//! the packed coordinates, hit or cover or miss, a footprint or a line, and
//! the regional grid record. The grid records
//! are the same bytes the Python loader already reads, so a later selection
//! in that daily does not open the state file.
//!
//! How Python uses it (generator/ways_native.py), through helper.dll:
//!
//! ```text
//!   knox_w_open(...)     one selection, and the region the grid covers
//!   knox_w_offer(...)    one way: its node records and its tags
//!   knox_w_cell(...)     the grid, after the last way
//!   knox_w_close()
//! ```
//!
//! `knox_w_classify` and `knox_w_is_area` are the same tests for a way that
//! was already stored, when a later selection is cut from the grid.
//!
//! Nothing here calls back into Python. A panic is caught at the edge and
//! comes back as an error code. The session is used from one thread.

pub mod area;
pub mod classify;
mod err;
mod measure;
mod span;

use std::os::raw::c_char;

use classify::{COVER, HIT, SCALE};
use err::Error;
use measure::Measured;
use span::Grid;

/// The format this build speaks. Python checks it before sending anything.
pub const ABI_VERSION: u32 = 1;

/// The node array could not be read. Python falls back for that way and does
/// not publish the grid.
pub const UNREADABLE: i32 = -2;
/// A node has no location. The way is dropped. The grid stays valid.
pub const INVALID: i32 = -1;

/// What `knox_w_offer` writes. `xy` is the compact coordinate pairs, valid
/// until the next offer or until close, and only when Python still needs
/// them: a water shore, or a way the selection kept.
#[repr(C)]
pub struct WayAnswer {
    pub code: i32,
    pub min_x: i32,
    pub min_y: i32,
    pub max_x: i32,
    pub max_y: i32,
    pub area: u32,
    pub xy: *const u8,
    pub xy_len: usize,
}

/// One selection. The grid covers the whole region; the box is the part
/// being cut out of it now.
pub struct Session {
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    limits: (f64, f64, f64, f64),
    grid: Grid,
    trusted: bool,
    /// Reused so a kept way's coordinates stay put until the next offer.
    scratch: Vec<u8>,
    /// Ways the selection keeps, packed for the rare handoff back to Python.
    selection: Vec<u8>,
}

impl Session {
    fn open(
        south: f64,
        west: f64,
        north: f64,
        east: f64,
        region_south: f64,
        region_west: f64,
        region_north: f64,
        region_east: f64,
        deg: f64,
        wide: u32,
    ) -> Result<Self, Error> {
        let values = [
            south,
            west,
            north,
            east,
            region_south,
            region_west,
            region_north,
            region_east,
            deg,
        ];
        if values.iter().any(|value| !value.is_finite()) || north < south || east < west {
            return Err(Error::BadBox);
        }
        Ok(Self {
            south,
            west,
            north,
            east,
            limits: (
                south * SCALE - 2.0,
                north * SCALE + 2.0,
                west * SCALE - 2.0,
                east * SCALE + 2.0,
            ),
            grid: Grid::new(
                region_south,
                region_west,
                region_north,
                region_east,
                deg,
                wide,
            ),
            trusted: true,
            scratch: Vec::new(),
            selection: Vec::new(),
        })
    }

    fn offer(
        &mut self,
        id: i64,
        nodes: *const u8,
        count: u32,
        tags: &[u8],
        paint: bool,
        needed: bool,
    ) -> WayAnswer {
        self.scratch.clear();
        let measured = unsafe { measure::measure(nodes, count as usize) };
        let (min_x, min_y, max_x, max_y, xy) = match measured {
            Measured::Unreadable => return self.answer(UNREADABLE, 0, 0, 0, 0, false),
            Measured::Invalid => return self.answer(INVALID, 0, 0, 0, 0, false),
            Measured::Ok {
                min_x,
                min_y,
                max_x,
                max_y,
                xy,
            } => (min_x, min_y, max_x, max_y, xy),
        };
        if paint && self.trusted {
            self.grid.add_way(id, min_x, min_y, max_x, max_y, &xy, tags);
        }
        let outside = span::misses(min_x, min_y, max_x, max_y, self.limits);
        if outside {
            if needed {
                self.scratch = xy;
            }
            return self.answer(classify::MISS, min_x, min_y, max_x, max_y, false);
        }
        if !paint {
            if needed {
                self.scratch = xy;
            }
            return self.answer(classify::MISS, min_x, min_y, max_x, max_y, false);
        }
        let status = classify::classify(&xy, self.south, self.west, self.north, self.east);
        if status != HIT && status != COVER {
            if needed {
                self.scratch = xy;
            }
            return self.answer(classify::MISS, min_x, min_y, max_x, max_y, false);
        }
        let closed = xy.len() >= 32 && xy.len() % 8 == 0 && xy[..8] == xy[xy.len() - 8..];
        let area = area::is_area(tags, closed);
        self.scratch = xy;
        self.answer(status, min_x, min_y, max_x, max_y, area)
    }

    fn answer(
        &self,
        code: i32,
        min_x: i32,
        min_y: i32,
        max_x: i32,
        max_y: i32,
        area: bool,
    ) -> WayAnswer {
        let (xy, xy_len) = if self.scratch.is_empty() {
            (std::ptr::null(), 0)
        } else {
            (self.scratch.as_ptr(), self.scratch.len())
        };
        WayAnswer {
            code,
            min_x,
            min_y,
            max_x,
            max_y,
            area: u32::from(area),
            xy,
            xy_len,
        }
    }

    fn abandon(&mut self) {
        self.trusted = false;
        self.grid.clear();
        self.scratch.clear();
    }

    /// Ways in the grid that meet the selection, so a handoff can write them
    /// without walking the daily again. The bytes stay in `selection` until
    /// the next take, abandon, or close.
    fn take_selection(&mut self) {
        self.selection.clear();
        let south = self.south;
        let west = self.west;
        let north = self.north;
        let east = self.east;
        let mut packed: Vec<(i64, i32, Vec<u8>, Vec<u8>)> = Vec::new();
        self.grid.each_way(|id, xy, tags| {
            let status = classify::classify(xy, south, west, north, east);
            if status != HIT && status != COVER {
                return;
            }
            packed.push((id, status, xy.to_vec(), tags.to_vec()));
        });
        let n = packed.len() as u32;
        self.selection.extend_from_slice(&n.to_le_bytes());
        for (id, status, xy, tags) in packed {
            self.selection.extend_from_slice(&id.to_le_bytes());
            self.selection.extend_from_slice(&status.to_le_bytes());
            self.selection.extend_from_slice(&(xy.len() as u32).to_le_bytes());
            self.selection.extend_from_slice(&xy);
            self.selection.extend_from_slice(&(tags.len() as u32).to_le_bytes());
            self.selection.extend_from_slice(&tags);
        }
    }
}

fn write_fault(fault: *mut u32, err: Error) {
    if !fault.is_null() {
        unsafe { *fault = err.code() };
    }
}

pub extern "C" fn knox_w_abi_version() -> u32 {
    ABI_VERSION
}

pub extern "C" fn knox_w_error_text(code: u32) -> *const c_char {
    err::text_for(code).as_ptr() as *const c_char
}

/// Open a session for one selection.
///
/// The first box is the selection. The second is the region the grid covers.
/// `deg` is the cell size in degrees and `wide` is how many cells a way may
/// span before it is stored once instead of in each cell.
///
/// # Safety
/// `fault` may be null. The returned session is owned by the caller until
/// `knox_w_close`.
pub unsafe extern "C" fn knox_w_open(
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    wide: u32,
    fault: *mut u32,
) -> *mut Session {
    let opened = std::panic::catch_unwind(|| {
        Session::open(
            south,
            west,
            north,
            east,
            region_south,
            region_west,
            region_north,
            region_east,
            deg,
            wide,
        )
    });
    match opened {
        Ok(Ok(session)) => Box::into_raw(Box::new(session)),
        Ok(Err(err)) => {
            write_fault(fault, err);
            std::ptr::null_mut()
        }
        Err(_) => {
            write_fault(fault, Error::Panic);
            std::ptr::null_mut()
        }
    }
}

/// Measure one way, file it if it is painted, and say whether the selection
/// keeps it.
///
/// `nodes` is the first of `count` libosmium node records, 16 bytes each.
/// `tags` is the JSON object of the way's tags, or empty when it is only a
/// water shore. Returns zero when `out` was written.
///
/// # Safety
/// `session` must come from `knox_w_open`. `nodes` must point at `count`
/// records that stay put for this call. `tags` must point at `tags_len`
/// bytes. `out` must not be null.
pub unsafe extern "C" fn knox_w_offer(
    session: *mut Session,
    id: i64,
    nodes: *const u8,
    count: u32,
    tags: *const u8,
    tags_len: usize,
    paint: u32,
    needed: u32,
    out: *mut WayAnswer,
) -> u32 {
    if out.is_null() {
        return Error::BadState.code();
    }
    let Some(session) = session.as_mut() else {
        return Error::BadState.code();
    };
    let tags = if tags.is_null() || tags_len == 0 {
        &[][..]
    } else {
        std::slice::from_raw_parts(tags, tags_len)
    };
    let offered = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        session.offer(id, nodes, count, tags, paint != 0, needed != 0)
    }));
    match offered {
        Ok(answer) => {
            *out = answer;
            0
        }
        Err(_) => Error::Panic.code(),
    }
}

/// The selection no longer trusts its grid. Later cells come back empty.
///
/// # Safety
/// `session` must come from `knox_w_open` and not have been closed.
pub unsafe extern "C" fn knox_w_abandon(session: *mut Session) {
    if let Some(session) = session.as_mut() {
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.abandon()));
    }
}

/// How many grid cells hold a way. Zero when the grid was abandoned.
///
/// # Safety
/// As `knox_w_abandon`.
pub unsafe extern "C" fn knox_w_cell_count(session: *mut Session) -> u32 {
    let Some(session) = session.as_mut() else {
        return 0;
    };
    if !session.trusted {
        return 0;
    }
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.grid.cell_count())) {
        Ok(count) => count,
        Err(_) => 0,
    }
}

/// One cell of way records. The pointer stays valid until the next offer,
/// abandon, or close.
///
/// # Safety
/// As `knox_w_abandon`. The out pointers must not be null.
pub unsafe extern "C" fn knox_w_cell(
    session: *mut Session,
    index: u32,
    row: *mut i32,
    col: *mut i32,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    if row.is_null() || col.is_null() || ptr.is_null() || len.is_null() {
        return Error::BadState.code();
    }
    let Some(session) = session.as_mut() else {
        return Error::BadState.code();
    };
    if !session.trusted {
        return Error::BadState.code();
    }
    let found = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.grid.cell(index)));
    match found {
        Ok(Some((cell_row, cell_col, bytes, nbytes))) => {
            *row = cell_row;
            *col = cell_col;
            *ptr = bytes;
            *len = nbytes;
            0
        }
        Ok(None) => Error::BadState.code(),
        Err(_) => Error::Panic.code(),
    }
}

/// Ways too long for a cell. The pointer stays valid until the next offer,
/// abandon, or close. Empty when the grid was abandoned.
///
/// # Safety
/// As `knox_w_cell`.
pub unsafe extern "C" fn knox_w_wide(
    session: *const Session,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    if ptr.is_null() || len.is_null() {
        return Error::BadState.code();
    }
    let Some(session) = session.as_ref() else {
        return Error::BadState.code();
    };
    if !session.trusted {
        *ptr = std::ptr::null();
        *len = 0;
        return 0;
    }
    let (bytes, nbytes) = session.grid.wide();
    *ptr = bytes;
    *len = nbytes;
    0
}

/// Hit, cover, or miss for coordinates already packed the way a span file
/// stores them.
///
/// # Safety
/// `xy` must point at `xy_len` bytes, or be null when `xy_len` is zero.
pub unsafe extern "C" fn knox_w_classify(
    xy: *const u8,
    xy_len: usize,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> i32 {
    if xy.is_null() || xy_len == 0 {
        return classify::MISS;
    }
    let bytes = std::slice::from_raw_parts(xy, xy_len);
    match std::panic::catch_unwind(|| classify::classify(bytes, south, west, north, east)) {
        Ok(status) => status,
        Err(_) => classify::MISS,
    }
}

/// One when a closed way with these tags is a footprint.
///
/// # Safety
/// `tags` must point at `tags_len` bytes of JSON, or be null when the length
/// is zero.
pub unsafe extern "C" fn knox_w_is_area(tags: *const u8, tags_len: usize, closed: u32) -> u32 {
    if closed == 0 || tags.is_null() || tags_len == 0 {
        return 0;
    }
    let bytes = std::slice::from_raw_parts(tags, tags_len);
    match std::panic::catch_unwind(|| u32::from(area::is_area(bytes, true))) {
        Ok(area) => area,
        Err(_) => 0,
    }
}

/// Ways already filed that meet this session's selection.
///
/// The buffer is a little-endian count, then for each way its id, status
/// (1 hit, 2 cover), packed coordinates, and tag JSON. Valid until the next
/// offer, take, abandon, or close.
///
/// # Safety
/// As `knox_w_abandon`. The out pointers must not be null.
pub unsafe extern "C" fn knox_w_take_selection(
    session: *mut Session,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    if ptr.is_null() || len.is_null() {
        return Error::BadState.code();
    }
    let Some(session) = session.as_mut() else {
        return Error::BadState.code();
    };
    let taken = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| session.take_selection()));
    match taken {
        Ok(()) => {
            *ptr = session.selection.as_ptr();
            *len = session.selection.len();
            0
        }
        Err(_) => Error::Panic.code(),
    }
}

/// # Safety
/// `session` must come from `knox_w_open` and must not be used again.
pub unsafe extern "C" fn knox_w_close(session: *mut Session) {
    if !session.is_null() {
        drop(Box::from_raw(session));
    }
}
