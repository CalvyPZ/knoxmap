//! The regional grid a later selection reads instead of the daily.
//!
//! A way is filed under every cell its box meets. A way that spans more than
//! a handful of cells is stored once, in the wide bucket, rather than copied
//! into each of them. The record bytes are the ones `localosm._way_record`
//! writes, so a grid this crate files is read by the same loader.

use std::collections::{HashMap, HashSet};

use crate::classify::SCALE;

pub struct Grid {
    south: f64,
    west: f64,
    deg: f64,
    rows: i32,
    cols: i32,
    wide_limit: i32,
    cells: HashMap<(i32, i32), Vec<u8>>,
    wide: Vec<u8>,
    listed: Vec<(i32, i32)>,
    list_ready: bool,
}

impl Grid {
    pub fn new(south: f64, west: f64, north: f64, east: f64, deg: f64, wide_limit: u32) -> Self {
        let (rows, cols) = grid_shape(south, west, north, east, deg);
        Self {
            south,
            west,
            deg: if deg > 0.0 { deg } else { 0.25 },
            rows,
            cols,
            wide_limit: wide_limit.max(1) as i32,
            cells: HashMap::new(),
            wide: Vec::new(),
            listed: Vec::new(),
            list_ready: false,
        }
    }

    pub fn clear(&mut self) {
        self.cells.clear();
        self.wide.clear();
        self.listed.clear();
        self.list_ready = false;
    }

    pub fn add_way(
        &mut self,
        id: i64,
        min_x: i32,
        min_y: i32,
        max_x: i32,
        max_y: i32,
        xy: &[u8],
        tags: &[u8],
    ) {
        self.list_ready = false;
        let record = way_record(id, min_x, min_y, max_x, max_y, xy, tags);
        match self.cells_touched(min_x, min_y, max_x, max_y) {
            None => self.wide.extend_from_slice(&record),
            Some(cells) => {
                for key in cells {
                    self.cells
                        .entry(key)
                        .or_default()
                        .extend_from_slice(&record);
                }
            }
        }
    }

    pub fn cell_count(&mut self) -> u32 {
        self.ensure_list();
        self.listed.len() as u32
    }

    pub fn cell(&mut self, index: u32) -> Option<(i32, i32, *const u8, usize)> {
        self.ensure_list();
        let key = *self.listed.get(index as usize)?;
        let buf = self.cells.get(&key)?;
        Some((key.0, key.1, buf.as_ptr(), buf.len()))
    }

    pub fn wide(&self) -> (*const u8, usize) {
        (self.wide.as_ptr(), self.wide.len())
    }

    /// Every stored way once. A way filed in several cells is visited on the
    /// first cell only. `xy` and `tags` borrow the cell bytes.
    pub fn each_way(&self, mut visit: impl FnMut(i64, &[u8], &[u8])) {
        let mut seen = HashSet::new();
        for buf in self.cells.values() {
            walk_ways(buf, &mut seen, &mut visit);
        }
        walk_ways(&self.wide, &mut seen, &mut visit);
    }

    fn ensure_list(&mut self) {
        if self.list_ready {
            return;
        }
        self.listed = self.cells.keys().copied().collect();
        self.list_ready = true;
    }

    fn cell_of(&self, lat: f64, lon: f64) -> (i32, i32) {
        let mut row = if self.deg == 0.0 {
            0
        } else {
            ((lat - self.south) / self.deg) as i32
        };
        let mut col = if self.deg == 0.0 {
            0
        } else {
            ((lon - self.west) / self.deg) as i32
        };
        if row < 0 {
            row = 0;
        } else if row >= self.rows {
            row = self.rows - 1;
        }
        if col < 0 {
            col = 0;
        } else if col >= self.cols {
            col = self.cols - 1;
        }
        (row, col)
    }

    /// Cells the box meets, or None when the way is too long to copy into
    /// each of them.
    fn cells_touched(
        &self,
        min_x: i32,
        min_y: i32,
        max_x: i32,
        max_y: i32,
    ) -> Option<Vec<(i32, i32)>> {
        let (mut r0, mut c0) = self.cell_of(min_y as f64 / SCALE, min_x as f64 / SCALE);
        let (mut r1, mut c1) = self.cell_of(max_y as f64 / SCALE, max_x as f64 / SCALE);
        if r0 > r1 {
            std::mem::swap(&mut r0, &mut r1);
        }
        if c0 > c1 {
            std::mem::swap(&mut c0, &mut c1);
        }
        let across = (r1 as i64 - r0 as i64 + 1) * (c1 as i64 - c0 as i64 + 1);
        if across > self.wide_limit as i64 {
            return None;
        }
        let mut out = Vec::with_capacity(across as usize);
        for row in r0..=r1 {
            for col in c0..=c1 {
                out.push((row, col));
            }
        }
        Some(out)
    }
}

fn walk_ways(buf: &[u8], seen: &mut HashSet<i64>, visit: &mut impl FnMut(i64, &[u8], &[u8])) {
    let mut i = 0;
    while i < buf.len() {
        if buf[i] != 1 {
            break;
        }
        i += 1;
        if i + 28 > buf.len() {
            break;
        }
        let id = i64::from_le_bytes(buf[i..i + 8].try_into().unwrap());
        i += 24;
        let xy_len = u32::from_le_bytes(buf[i..i + 4].try_into().unwrap()) as usize;
        i += 4;
        if i + xy_len + 4 > buf.len() {
            break;
        }
        let xy = &buf[i..i + xy_len];
        i += xy_len;
        let tag_len = u32::from_le_bytes(buf[i..i + 4].try_into().unwrap()) as usize;
        i += 4;
        if i + tag_len > buf.len() {
            break;
        }
        let tags = &buf[i..i + tag_len];
        i += tag_len;
        if seen.insert(id) {
            visit(id, xy, tags);
        }
    }
}

fn grid_shape(south: f64, west: f64, north: f64, east: f64, deg: f64) -> (i32, i32) {
    if deg <= 0.0 {
        return (1, 1);
    }
    let rows = ((north - south).max(1e-6) / deg).ceil() as i32;
    let cols = ((east - west).max(1e-6) / deg).ceil() as i32;
    (rows.max(1), cols.max(1))
}

/// Kind byte, then id, box, coordinate length, coordinates, tag length, tags.
fn way_record(
    id: i64,
    min_x: i32,
    min_y: i32,
    max_x: i32,
    max_y: i32,
    xy: &[u8],
    tags: &[u8],
) -> Vec<u8> {
    let mut out = Vec::with_capacity(1 + 8 + 16 + 4 + xy.len() + 4 + tags.len());
    out.push(1);
    out.extend_from_slice(&id.to_le_bytes());
    out.extend_from_slice(&min_x.to_le_bytes());
    out.extend_from_slice(&min_y.to_le_bytes());
    out.extend_from_slice(&max_x.to_le_bytes());
    out.extend_from_slice(&max_y.to_le_bytes());
    out.extend_from_slice(&(xy.len() as u32).to_le_bytes());
    out.extend_from_slice(xy);
    out.extend_from_slice(&(tags.len() as u32).to_le_bytes());
    out.extend_from_slice(tags);
    out
}

/// The integer envelope misses the selection. Compared the way
/// `_span_overlaps` compares: the limits are floats, the coordinates are ints.
pub fn misses(
    min_x: i32,
    min_y: i32,
    max_x: i32,
    max_y: i32,
    limits: (f64, f64, f64, f64),
) -> bool {
    let (south_lo, north_hi, west_lo, east_hi) = limits;
    !((max_y as f64) >= south_lo
        && (min_y as f64) <= north_hi
        && (max_x as f64) >= west_lo
        && (min_x as f64) <= east_hi)
}
