//! Cutting a floor into rooms.
//!
//! The same recursive cut knoxbuild/layout.py makes: halve the region across
//! its longer side until what is left is about the size of one room. Two things
//! stop that producing a grid of cells:
//!
//! * The region is measured by the floor that is really inside the footprint,
//!   not by its bounding box. Half of every box over a footprint turned 38
//!   degrees is empty corner, and sizing rooms by the box kept splitting until
//!   a house had fifteen rooms to a floor.
//! * One small room is taken out of a corner first, so a house has somewhere
//!   bathroom-sized. Cutting to a single target area and then calling the
//!   smallest room the bathroom gave bathrooms the size of bedrooms.
//!
//! A cut near the middle is forced while a region is far bigger than a room:
//! a free cut on a floor the size of a factory kept shaving slivers off one
//! end until the recursion ran out and left a room of two thousand tiles.

use crate::grid::{sat, sat_box};
use crate::plan::{Rect, MIN_ROOM};
use crate::rng::Rng;

/// A region this wide can be cut and leave a room either side.
pub const MIN_SPLIT: i32 = MIN_ROOM * 2 + 1;
/// How deep the one small room's strip is, in tiles.
pub const SMALL_ROOM_RUN: (i32, i32) = (MIN_ROOM, MIN_ROOM + 1);
/// Past this much of a room's worth of floor, the cut is forced near the middle.
const FORCE_MIDDLE_OVER: f64 = 2.5;

/// The footprint, as a summed-area table, so a region's real floor is one read.
pub struct Floor {
    table: Vec<i32>,
    width: usize,
    height: usize,
    solid: bool,
}

impl Floor {
    /// `mask` empty means the building fills its rectangle.
    pub fn new(mask: &[bool], width: usize, height: usize) -> Floor {
        if mask.is_empty() {
            return Floor {
                table: Vec::new(),
                width,
                height,
                solid: true,
            };
        }
        let mut table = Vec::new();
        sat(mask, width, height, &mut table);
        Floor {
            table,
            width,
            height,
            solid: false,
        }
    }

    /// Tiles of a rectangle that are really inside the building.
    pub fn inside(&self, rect: &Rect) -> i32 {
        let x0 = rect.x0.max(0) as usize;
        let y0 = rect.y0.max(0) as usize;
        let x1 = (rect.x1 + 1).max(0).min(self.width as i32) as usize;
        let y1 = (rect.y1 + 1).max(0).min(self.height as i32) as usize;
        if x1 <= x0 || y1 <= y0 {
            return 0;
        }
        if self.solid {
            return ((x1 - x0) * (y1 - y0)) as i32;
        }
        sat_box(&self.table, self.width + 1, x0, y0, x1, y1)
    }
}

/// How big a room should come out, given the player's room-size setting and
/// what sort of building this is.
/// `room_size` is the player's setting in tiles, not a multiplier: it is the
/// floor area one room should come to, which is what knoxbuild/settings.py holds
/// and what layout.py multiplies the building's own scale into.
pub fn target_area(room_size: f64, kind_scale: f64) -> i32 {
    let want = room_size.max(1.0) * kind_scale.max(0.1);
    want.round().max((MIN_ROOM * MIN_ROOM) as f64) as i32
}

/// Cut a region into room rectangles. `small` asks for one bathroom-sized room
/// to be taken out of a corner, and is cleared once that has happened.
pub fn split(
    rect: Rect,
    rng: &mut Rng,
    depth: u32,
    target: i32,
    floor: &Floor,
    small: &mut bool,
    out: &mut Vec<Rect>,
) {
    let w = rect.w();
    let h = rect.h();
    let can_v = w >= MIN_SPLIT;
    let can_h = h >= MIN_SPLIT;
    let area = floor.inside(&rect);
    if depth == 0 || (!can_v && !can_h) || area <= target {
        out.push(rect);
        return;
    }

    if *small && w >= MIN_ROOM * 2 && h >= MIN_ROOM * 2 && area >= target * 2 {
        if let Some((room, strip, rest)) = small_corner(&rect, rng) {
            // Not where the footprint has cut most of it away: three tiles in
            // the corner of a turned building is not a bathroom.
            if floor.inside(&room) >= MIN_ROOM * MIN_ROOM - 2 {
                *small = false;
                out.push(room);
                split(strip, rng, depth - 1, target, floor, small, out);
                split(rest, rng, depth - 1, target, floor, small, out);
                return;
            }
        }
    }

    let vertical = if !can_h {
        true
    } else if !can_v {
        false
    } else if w != h {
        w > h
    } else {
        rng.chance(0.5)
    };

    let (low, high) = if vertical {
        (rect.x0 + MIN_ROOM, rect.x1 - MIN_ROOM)
    } else {
        (rect.y0 + MIN_ROOM, rect.y1 - MIN_ROOM)
    };
    let (mut low, mut high) = (low, high);
    if (area as f64) > target as f64 * FORCE_MIDDLE_OVER {
        let (a, b) = if vertical {
            (rect.x0, rect.x1)
        } else {
            (rect.y0, rect.y1)
        };
        let mid = (a + b) / 2;
        let reach = (((b - a) / 6) as i32).max(1);
        low = low.max(mid - reach);
        high = high.min(mid + reach);
        if low > high {
            low = mid.max(a + MIN_ROOM).min(b - MIN_ROOM);
            high = low;
        }
    }
    if low > high {
        out.push(rect);
        return;
    }
    let cut = rng.randint(low as i64, high as i64) as i32;
    if vertical {
        split(
            Rect::new(rect.x0, rect.y0, cut - 1, rect.y1),
            rng,
            depth - 1,
            target,
            floor,
            small,
            out,
        );
        split(
            Rect::new(cut, rect.y0, rect.x1, rect.y1),
            rng,
            depth - 1,
            target,
            floor,
            small,
            out,
        );
    } else {
        split(
            Rect::new(rect.x0, rect.y0, rect.x1, cut - 1),
            rng,
            depth - 1,
            target,
            floor,
            small,
            out,
        );
        split(
            Rect::new(rect.x0, cut, rect.x1, rect.y1),
            rng,
            depth - 1,
            target,
            floor,
            small,
            out,
        );
    }
}

/// The one small room, as (room, the rest of its strip, the rest of the floor).
///
/// A corner taken out of a rectangle leaves an L, and the plan is rectangles,
/// so it takes two cuts: a strip the minimum depth off one end, and the room
/// off one end of that strip. What is left of the strip is a closet or a box
/// room, which is what sits beside a bathroom in the game's own houses.
fn small_corner(rect: &Rect, rng: &mut Rng) -> Option<(Rect, Rect, Rect)> {
    let w = rect.w();
    let h = rect.h();
    let wide = w >= h;
    let length = if wide { w } else { h };
    let high = SMALL_ROOM_RUN.1.min(length - MIN_ROOM);
    if high < SMALL_ROOM_RUN.0 {
        return None;
    }
    let run = rng.randint(SMALL_ROOM_RUN.0 as i64, high as i64) as i32;
    let far_end = rng.chance(0.5);
    let far_side = rng.chance(0.5);
    if wide {
        let (sy0, sy1) = if far_end {
            (rect.y1 - MIN_ROOM + 1, rect.y1)
        } else {
            (rect.y0, rect.y0 + MIN_ROOM - 1)
        };
        let (bx0, bx1) = if far_side {
            (rect.x1 - run + 1, rect.x1)
        } else {
            (rect.x0, rect.x0 + run - 1)
        };
        let room = Rect::new(bx0, sy0, bx1, sy1);
        let strip = if far_side {
            Rect::new(rect.x0, sy0, bx0 - 1, sy1)
        } else {
            Rect::new(bx1 + 1, sy0, rect.x1, sy1)
        };
        let rest = if far_end {
            Rect::new(rect.x0, rect.y0, rect.x1, rect.y1 - MIN_ROOM)
        } else {
            Rect::new(rect.x0, rect.y0 + MIN_ROOM, rect.x1, rect.y1)
        };
        Some((room, strip, rest))
    } else {
        let (sx0, sx1) = if far_end {
            (rect.x1 - MIN_ROOM + 1, rect.x1)
        } else {
            (rect.x0, rect.x0 + MIN_ROOM - 1)
        };
        let (by0, by1) = if far_side {
            (rect.y1 - run + 1, rect.y1)
        } else {
            (rect.y0, rect.y0 + run - 1)
        };
        let room = Rect::new(sx0, by0, sx1, by1);
        let strip = if far_side {
            Rect::new(sx0, rect.y0, sx1, by0 - 1)
        } else {
            Rect::new(sx0, by1 + 1, sx1, rect.y1)
        };
        let rest = if far_end {
            Rect::new(rect.x0, rect.y0, rect.x1 - MIN_ROOM, rect.y1)
        } else {
            Rect::new(rect.x0 + MIN_ROOM, rect.y0, rect.x1, rect.y1)
        };
        Some((room, strip, rest))
    }
}

/// Cut `0..length` into runs of about `lo..hi`, shared out evenly.
///
/// Taking random lengths off one end and giving the last slice the remainder
/// made the end flat of each row up to twice the size of the others: a 26-tile
/// corridor came out as flats of 7 and 19. Deciding how many fit and then
/// sharing the length keeps them comparable, with a tile of jitter so a row of
/// flats is not perfectly regular.
pub fn slices(length: i32, rng: &mut Rng, lo: i32, hi: i32) -> Vec<(i32, i32)> {
    if length <= 0 {
        return Vec::new();
    }
    let want = ((lo + hi) as f64 / 2.0).max(1.0);
    let count = ((length as f64 / want).round() as i32).max(1);
    let mut widths = vec![length / count; count as usize];
    let short = length - widths.iter().sum::<i32>();
    for i in 0..short.max(0) as usize {
        widths[i] += 1;
    }
    for i in 0..widths.len().saturating_sub(1) {
        let shift = *rng.choice(&[-1i32, 0, 1]).unwrap_or(&0);
        if widths[i] + shift >= lo && widths[i + 1] - shift >= lo {
            widths[i] += shift;
            widths[i + 1] -= shift;
        }
    }
    let mut out = Vec::with_capacity(widths.len());
    let mut at = 0;
    for width in widths {
        if width <= 0 {
            continue;
        }
        out.push((at, at + width - 1));
        at += width;
    }
    out
}

