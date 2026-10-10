//! Shared walls: which of a building's walls are also the wall of the building
//! next door.
//!
//! A terrace is one footprint per house, and where two of them touch there is
//! one wall, not two. The game draws a wall between any two rooms, so the two
//! houses agree about that wall by neither of them putting a window, a shop
//! front or a door in it. The wall also only exists as high as both buildings
//! go: a three-storey neighbour shares nothing with our fourth floor.
//!
//! knoxbuild/build.py answered this with an int32 array of tile owners the size
//! of the whole map, which for a Knox County piece is tens of millions of
//! entries, built and thrown away for every piece. Here the footprints are
//! already in the blob, so the question is answered from them directly: the
//! buildings are bucketed into coarse cells once, and a building only ever
//! compares itself with the handful whose rectangles touch its own.
//!
//! The index is built once per session and read by every worker at the same
//! time, so it is immutable after `Index::build`.

use crate::blob::{Blob, Job, PartyEdge};
use std::collections::HashMap;

/// Cell side, in tiles. Big enough that the map of cells stays small, small
/// enough that a cell holds a handful of buildings rather than a street.
const CELL: i32 = 32;

pub struct Index {
    /// (piece, cell x, cell y) -> the buildings whose rectangle touches it.
    cells: HashMap<(u16, i32, i32), Vec<u32>>,
}

impl Index {
    /// Bucket every building of every piece. One pass over the jobs; the cost
    /// is a few bytes per building per cell it covers.
    pub fn build(blob: &Blob) -> Index {
        let mut cells: HashMap<(u16, i32, i32), Vec<u32>> = HashMap::new();
        for index in 0..blob.n_jobs() {
            let job = blob.job(index);
            // Grown by one tile: two buildings that only touch are in the same
            // cell even when their rectangles are in different ones.
            let x0 = (job.x0 as i32 - 1).div_euclid(CELL);
            let y0 = (job.y0 as i32 - 1).div_euclid(CELL);
            let x1 = (job.x0 as i32 + job.width as i32).div_euclid(CELL);
            let y1 = (job.y0 as i32 + job.height as i32).div_euclid(CELL);
            for cy in y0..=y1 {
                for cx in x0..=x1 {
                    cells
                        .entry((job.piece, cx, cy))
                        .or_default()
                        .push(index as u32);
                }
            }
        }
        Index { cells }
    }

    /// Roughly how much the index holds, for the memory report.
    pub fn bytes(&self) -> usize {
        let mut total = self.cells.len() * (std::mem::size_of::<(u16, i32, i32)>() + 48);
        for held in self.cells.values() {
            total += held.capacity() * 4;
        }
        total
    }

    fn near(&self, job: &Job, index: usize, out: &mut Vec<u32>) {
        out.clear();
        let x0 = (job.x0 as i32 - 1).div_euclid(CELL);
        let y0 = (job.y0 as i32 - 1).div_euclid(CELL);
        let x1 = (job.x0 as i32 + job.width as i32).div_euclid(CELL);
        let y1 = (job.y0 as i32 + job.height as i32).div_euclid(CELL);
        for cy in y0..=y1 {
            for cx in x0..=x1 {
                if let Some(held) = self.cells.get(&(job.piece, cx, cy)) {
                    for other in held {
                        if *other as usize != index && !out.contains(other) {
                            out.push(*other);
                        }
                    }
                }
            }
        }
    }
}

/// The walls this building shares, in its own tile coordinates.
///
/// Walls live on the West or North edge of a tile, and the grid of a building
/// `w` by `h` has edges up to `w` and `h`, so the wall on the east side of the
/// last column is the West edge of column `w`. That is how BuildingEd stores
/// them and how the .tbx writer reads them back.
pub fn edges_for(blob: &Blob, index: &Index, at: usize, out: &mut Vec<PartyEdge>) {
    out.clear();
    let job = blob.job(at);
    let mut near: Vec<u32> = Vec::new();
    index.near(&job, at, &mut near);
    if near.is_empty() {
        return;
    }
    let ax = job.x0 as i32;
    let ay = job.y0 as i32;
    for other in &near {
        let them = blob.job(*other as usize);
        let bx = them.x0 as i32;
        let by = them.y0 as i32;
        // Only the band where the two rectangles are within a tile of each
        // other can hold a shared wall, so that is all that is walked.
        let lo_x = (bx - 1).max(ax);
        let hi_x = (bx + them.width as i32).min(ax + job.width as i32 - 1);
        let lo_y = (by - 1).max(ay);
        let hi_y = (by + them.height as i32).min(ay + job.height as i32 - 1);
        if lo_x > hi_x || lo_y > hi_y {
            continue;
        }
        let up_to = them.levels.max(1);
        for gy in lo_y..=hi_y {
            for gx in lo_x..=hi_x {
                if !blob.mask_at(&job, gx - ax, gy - ay) {
                    continue;
                }
                let mine = [
                    (gx, gy - 1, 0i32, 0i32, b'N'),
                    (gx, gy + 1, 0, 1, b'N'),
                    (gx - 1, gy, 0, 0, b'W'),
                    (gx + 1, gy, 1, 0, b'W'),
                ];
                for (nx, ny, dx, dy, side) in mine {
                    if !blob.mask_at(&them, nx - bx, ny - by) {
                        continue;
                    }
                    // Their tile may also be ours, where two footprints
                    // overlap. An overlap is not a shared wall.
                    if blob.mask_at(&job, nx - ax, ny - ay) {
                        continue;
                    }
                    push(
                        out,
                        PartyEdge {
                            x: (gx - ax + dx) as u16,
                            y: (gy - ay + dy) as u16,
                            side,
                            up_to,
                        },
                    );
                }
            }
        }
    }
}

/// Keep one edge per place, at the height of the tallest neighbour that shares
/// it. Three buildings can meet at a corner, and the edge is then shared as
/// high as the taller of the two that actually touch it.
fn push(out: &mut Vec<PartyEdge>, edge: PartyEdge) {
    for held in out.iter_mut() {
        if held.x == edge.x && held.y == edge.y && held.side == edge.side {
            if edge.up_to > held.up_to {
                held.up_to = edge.up_to;
            }
            return;
        }
    }
    out.push(edge);
}
