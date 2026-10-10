//! The stair core, the corridor, and the lift shaft.
//!
//! All three are decided once for the whole building and reserved before the
//! rooms are cut, because they have to be the same rectangle on every storey:
//! a staircase that moved between floors would rise into a bedroom, and the
//! Elevators mod finds a lift by its door tiles being at the same square on
//! every floor it serves.
//!
//! Ported from knoxbuild/layout.py's `_pick_core`, `_pick_corridor`,
//! `_pick_shaft` and `_stairs_in_core`.

use crate::blob::Street;
use crate::grid::{sat, sat_box, Side};
use crate::plan::{EdgeAt, Rect};
use crate::rng::Rng;

/// Tiles a staircase occupies, from BuildingEd's Stairs::bounds.
pub const STAIR_RUN: i32 = 5;
/// The flight plus a landing beside it.
pub const CORE_WIDE: i32 = 3;
/// A landing wide enough to be circulation, not a cupboard.
pub const CORRIDOR_WIDE: i32 = 3;
/// How wide a corridor is, in the shares Knox County builds them: of its 259
/// school corridor rectangles the narrow side is 3 in 101, 4 in 45, 2 in 43 and
/// 6 in 23. Ours was always exactly 3, the commonest but the only one.
pub const CORRIDOR_WIDTHS: [i32; 7] = [2, 3, 3, 3, 4, 4, 6];
/// Buildings this tall get a lift. Four flights is a fair climb.
pub const ELEVATOR_FROM_LEVELS: u16 = 5;
/// ...if it is more than a townhouse: a lift in a building five tiles wide took
/// half its ground floor.
pub const ELEVATOR_MIN_SIDE: i32 = 10;
pub const SHAFT_SIZE: i32 = 2;
/// A corridor has to run most of the building's length to be worth cutting.
const CORRIDOR_SPAN_SHARE: f64 = 0.6;

/// The footprint as a table, so "is this rectangle wholly inside" is one read.
struct Inside<'a> {
    table: Vec<i32>,
    width: usize,
    height: usize,
    solid: bool,
    mask: &'a [bool],
}

impl<'a> Inside<'a> {
    fn new(mask: &'a [bool], width: usize, height: usize) -> Inside<'a> {
        if mask.is_empty() {
            return Inside {
                table: Vec::new(),
                width,
                height,
                solid: true,
                mask,
            };
        }
        let mut table = Vec::new();
        sat(mask, width, height, &mut table);
        Inside {
            table,
            width,
            height,
            solid: false,
            mask,
        }
    }

    fn full(&self, x0: i32, y0: i32, w: i32, h: i32) -> bool {
        if x0 < 0 || y0 < 0 || w <= 0 || h <= 0 {
            return false;
        }
        if x0 + w > self.width as i32 || y0 + h > self.height as i32 {
            return false;
        }
        if self.solid {
            return true;
        }
        let _ = self.mask;
        sat_box(
            &self.table,
            self.width + 1,
            x0 as usize,
            y0 as usize,
            (x0 + w) as usize,
            (y0 + h) as usize,
        ) == w * h
    }
}

/// Reserve a stair shaft: the same rectangle on every storey, always wholly
/// inside the footprint.
///
/// With the street known the stairs go at the back against a side wall, where a
/// shop or a restaurant keeps them. In the middle of the floor, a terrace of
/// narrow shops had a flight of stairs in the middle of every one.
pub fn pick_core(
    width: i32,
    height: i32,
    mask: &[bool],
    rng: &mut Rng,
    street: Street,
) -> Option<Rect> {
    let inside = Inside::new(mask, width.max(0) as usize, height.max(0) as usize);
    let shapes = [
        (CORE_WIDE, STAIR_RUN + 1),
        (STAIR_RUN + 1, CORE_WIDE),
    ];
    let mut best: Option<(f64, Vec<Rect>)> = None;
    for (cw, ch) in shapes {
        if cw > width || ch > height {
            continue;
        }
        for y0 in 0..=height - ch {
            for x0 in 0..=width - cw {
                if !inside.full(x0, y0, cw, ch) {
                    continue;
                }
                let score = core_score(x0, y0, cw, ch, width, height, street);
                match &mut best {
                    Some((found, tied)) => {
                        if score < *found - 1e-9 {
                            *found = score;
                            tied.clear();
                            tied.push(Rect::new(x0, y0, x0 + cw - 1, y0 + ch - 1));
                        } else if (score - *found).abs() < 1e-9 {
                            tied.push(Rect::new(x0, y0, x0 + cw - 1, y0 + ch - 1));
                        }
                    }
                    None => {
                        best = Some((score, vec![Rect::new(x0, y0, x0 + cw - 1, y0 + ch - 1)]))
                    }
                }
            }
        }
    }
    let (_score, tied) = best?;
    rng.choice(&tied).copied()
}

fn core_score(
    x0: i32,
    y0: i32,
    cw: i32,
    ch: i32,
    width: i32,
    height: i32,
    street: Street,
) -> f64 {
    let (xf, yf) = (x0 as f64, y0 as f64);
    let (wf, hf) = (width as f64, height as f64);
    match street {
        Street::None => ((xf + cw as f64 / 2.0) - wf / 2.0).abs()
            + ((yf + ch as f64 / 2.0) - hf / 2.0).abs(),
        _ => {
            // Back and side: how far from the wall opposite the street, then
            // from the nearer side wall.
            let back = match street {
                Street::South => yf,
                Street::North => hf - (yf + ch as f64),
                Street::East => xf,
                _ => wf - (xf + cw as f64),
            };
            let side = match street {
                Street::North | Street::South => xf.min(wf - (xf + cw as f64)),
                _ => yf.min(hf - (yf + ch as f64)),
            };
            back * 2.0 + side
        }
    }
}

/// A corridor through the floor, with room for the stairs inside it.
///
/// Flats need something to open onto and it has to reach all of them: a compact
/// shaft in the middle leaves the flats in the corners touching nothing. A strip
/// along the building's length touches the flats either side of it by
/// construction. The strip no longer has to span the bounding box end to end -
/// on a footprint turned 30 degrees no such strip ever fitted - only most of the
/// building's length.
pub fn pick_corridor(width: i32, height: i32, mask: &[bool], rng: &mut Rng) -> Option<Rect> {
    let inside = Inside::new(mask, width.max(0) as usize, height.max(0) as usize);
    let along_y = height >= width;
    let span = if along_y { height } else { width };
    let least = ((span as f64 * CORRIDOR_SPAN_SHARE).round() as i32).max(STAIR_RUN + 1);
    let wide = *rng.choice(&CORRIDOR_WIDTHS).unwrap_or(&CORRIDOR_WIDE);
    // The width is the one draw. Which strip is the best score, not a random
    // one: moving it would shift every later storey.
    let mut best: Option<(i32, i32, Rect)> = None;
    let across = if along_y { width } else { height };
    if wide > across {
        return None;
    }
    for line in 0..=across - wide {
        // The longest stretch of this strip that lies wholly inside.
        let mut run = 0;
        let mut start = 0;
        for at in 0..span {
            let full = if along_y {
                inside.full(line, at, wide, 1)
            } else {
                inside.full(at, line, 1, wide)
            };
            if full {
                if run == 0 {
                    start = at;
                }
                run += 1;
                if run >= least {
                    let rect = if along_y {
                        Rect::new(line, start, line + wide - 1, start + run - 1)
                    } else {
                        Rect::new(start, line, start + run - 1, line + wide - 1)
                    };
                    // The longest strip wins; nearest the middle breaks a tie,
                    // so the flats either side of it come out even.
                    let middle = ((line + wide / 2) * 2 - across).abs();
                    let better = match best {
                        Some((found_run, found_mid, _)) => {
                            run > found_run || (run == found_run && middle < found_mid)
                        }
                        None => true,
                    };
                    if better {
                        best = Some((run, middle, rect));
                    }
                }
            } else {
                run = 0;
            }
        }
    }
    best.map(|(_run, _mid, rect)| rect)
}

/// The flight inside a core. It runs along the core's long axis, set one tile in
/// from the end so the landing is still walkable past it.
pub fn stairs_in_core(core: &Rect) -> (i32, i32, Side) {
    if (core.y1 - core.y0) >= (core.x1 - core.x0) {
        let start = (core.y0 + 1).min(core.y1 - (STAIR_RUN - 1));
        (
            core.x0 + (core.x1 - core.x0) / 2,
            start.max(core.y0),
            Side::North,
        )
    } else {
        let start = (core.x0 + 1).min(core.x1 - (STAIR_RUN - 1));
        (
            start.max(core.x0),
            core.y0 + (core.y1 - core.y0) / 2,
            Side::West,
        )
    }
}

/// The tiles a flight covers, so furniture keeps off them.
pub fn stair_tiles(stairs: (i32, i32, Side)) -> Vec<(i32, i32)> {
    let (x, y, side) = stairs;
    let (dx, dy) = match side {
        Side::North => (0, 1),
        Side::West => (1, 0),
    };
    (0..STAIR_RUN).map(|i| (x + dx * i, y + dy * i)).collect()
}

/// A 2x2 lift shaft beside the stair hall, and the wall its doors go in.
///
/// The shaft sits against the long side of the core so its doors open onto the
/// landing on every storey.
pub fn pick_shaft(
    core: &Rect,
    width: i32,
    height: i32,
    mask: &[bool],
    stairs: Option<(i32, i32, Side)>,
) -> Option<(Rect, EdgeAt)> {
    let inside = Inside::new(mask, width.max(0) as usize, height.max(0) as usize);
    let s = SHAFT_SIZE;
    let flight: Vec<(i32, i32)> = stairs.map(stair_tiles).unwrap_or_default();
    let mut options: Vec<(f64, Rect, EdgeAt)> = Vec::new();
    if (core.y1 - core.y0) >= (core.x1 - core.x0) {
        // The hall runs north to south: shafts to its west or east, with the
        // doors in a west wall.
        let mid = (core.y0 + core.y1) as f64 / 2.0;
        for y0 in core.y0..=core.y1 - s + 1 {
            for (x0, door_x, side_col) in [
                (core.x0 - s, core.x0, core.x0),
                (core.x1 + 1, core.x1 + 1, core.x1),
            ] {
                if !inside.full(x0, y0, s, s) {
                    continue;
                }
                if (0..s).any(|i| flight.contains(&(side_col, y0 + i))) {
                    continue;
                }
                options.push((
                    (y0 as f64 + s as f64 / 2.0 - 1.0 - mid).abs(),
                    Rect::new(x0, y0, x0 + s - 1, y0 + s - 1),
                    EdgeAt::new(door_x, y0, Side::West),
                ));
            }
        }
    } else {
        let mid = (core.x0 + core.x1) as f64 / 2.0;
        for x0 in core.x0..=core.x1 - s + 1 {
            for (y0, door_y, side_row) in [
                (core.y0 - s, core.y0, core.y0),
                (core.y1 + 1, core.y1 + 1, core.y1),
            ] {
                if !inside.full(x0, y0, s, s) {
                    continue;
                }
                if (0..s).any(|i| flight.contains(&(x0 + i, side_row))) {
                    continue;
                }
                options.push((
                    (x0 as f64 + s as f64 / 2.0 - 1.0 - mid).abs(),
                    Rect::new(x0, y0, x0 + s - 1, y0 + s - 1),
                    EdgeAt::new(x0, door_y, Side::North),
                ));
            }
        }
    }
    options.sort_unstable_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then((a.1.x0, a.1.y0).cmp(&(b.1.x0, b.1.y0)))
    });
    options
        .into_iter()
        .next()
        .map(|(_score, rect, door)| (rect, door))
}
