//! The zombie spawn map: a tenth scale picture of where they start.
//!
//! The game reads the red channel as a raw density, on a much smaller scale than
//! a 0-255 byte suggests. Sampling the vanilla Knox County map, values run one to
//! ten and nearly all of it is zero, so the piece carries its own ceiling and
//! every band is a fraction of it.
//!
//! One deviation from the Python pass, deliberately. Python shrank the ground
//! with a bilinear filter and then asked what colour the result was, which mixes
//! a road and its verge into a colour that is neither and then tests that
//! average against a range of greys. Here each spawn pixel looks at the hundred
//! tiles it stands for and takes whichever band most of them belong to. The
//! ground colours are matched exactly against the sets Python sends, so the
//! answer is "this pixel is mostly street" rather than "the average of this
//! pixel is greyish", which is what the pass was reaching for.

use crate::blob::{Blob, Piece, SpawnBand, ANY_SET};
use crate::canvas::Canvas;
use crate::rng;

/// The spawn picture of one piece, written band by band like the big ones.
pub struct Spawn {
    pub width: usize,
    pub height: usize,
    /// As the canvas: the box owns the picture, and `base` is how the bands
    /// reach it without anybody holding a reference to the whole of it.
    #[allow(dead_code)]
    store: Box<[u8]>,
    base: *mut u8,
    len: usize,
}

// Safety: as the canvas. Each thread writes rows no other thread touches.
unsafe impl Sync for Spawn {}
unsafe impl Send for Spawn {}

impl Spawn {
    pub fn new(piece: &Piece) -> Spawn {
        let scale = piece.spawn_scale.max(1) as usize;
        let width = ((piece.width as usize) / scale).max(1);
        let height = ((piece.height as usize) / scale).max(1);
        let mut store = vec![0u8; width * height * 3].into_boxed_slice();
        let base = store.as_mut_ptr();
        let len = store.len();
        Spawn {
            width,
            height,
            store,
            base,
            len,
        }
    }

    pub fn rows(&self) -> &[u8] {
        // Safety: called when no thread is writing, and inside the one
        // allocation, which lives as long as `self`.
        unsafe { std::slice::from_raw_parts(self.base, self.len) }
    }

    /// # Safety
    /// These rows must be held by exactly one thread.
    #[allow(clippy::mut_from_ref)]
    unsafe fn rows_mut(&self, y0: usize, y1: usize) -> &mut [u8] {
        let stride = self.width * 3;
        let y1 = y1.min(self.height);
        let y0 = y0.min(y1);
        let from = y0 * stride;
        let to = (y1 * stride).min(self.len);
        std::slice::from_raw_parts_mut(self.base.add(from), to.saturating_sub(from))
    }

    /// How many rows of the spawn picture there are to share out.
    pub fn rows_total(&self) -> usize {
        self.height
    }
}

/// Fill rows `y0` up to `y1` of the spawn picture from the finished ground.
///
/// # Safety
/// Those rows must be held by exactly one thread, and the ground must be
/// finished: this reads the whole of it and writes none of it.
pub unsafe fn fill(blob: &Blob, piece: &Piece, canvas: &Canvas, out: &Spawn, y0: usize, y1: usize) {
    let n = piece.spawn_n as usize;
    if n == 0 {
        return;
    }
    canvas.prepare(blob);
    let palette = canvas.colours();
    let scale = piece.spawn_scale.max(1) as usize;
    let ceiling = piece.spawn_density.max(1) as f32;
    let width = out.width;
    let rows = out.rows_mut(y0, y1);
    let mut counts = [0u32; 32];
    let bands = n.min(counts.len());
    let origin = canvas.origin_y;
    let limit = origin + canvas.height;
    let stride = canvas.ground.stride();
    for y in y0..y1 {
        let top = y * scale;
        let bottom = ((y + 1) * scale).min(limit);
        if top >= bottom || bottom <= origin {
            continue;
        }
        let top = top.max(origin);
        let ground = canvas.rows(&canvas.ground, top, bottom);
        let bpp = canvas.ground.bpp().max(1);
        let out_row = (y - y0) * width * 3;
        for x in 0..width {
            let left = x * scale;
            let right = ((x + 1) * scale).min(canvas.width);
            counts[..bands].fill(0);
            for row in 0..bottom - top {
                let at = row * stride;
                for column in left..right {
                    let tile = at + column * bpp;
                    if tile >= ground.len() {
                        continue;
                    }
                    // One byte is the palette index. Three bytes are red, green,
                    // blue, which the palette turns back into that index.
                    let index = if bpp >= 3 {
                        let end = tile + 3;
                        if end > ground.len() {
                            continue;
                        }
                        palette.index(crate::canvas::packed(&ground[tile..end]))
                    } else {
                        ground[tile]
                    };
                    if let Some(which) = band_of(&palette, blob, piece, n, index) {
                        if which < counts.len() {
                            counts[which] += 1;
                        }
                    }
                }
            }
            let mut best = 0usize;
            for which in 1..bands {
                if counts[which] > counts[best] {
                    best = which;
                }
            }
            if counts[best] == 0 {
                continue;
            }
            let band = blob.spawn_band(piece.spawn_at as usize + best);
            let value = density(&band, piece.seed, ceiling, x, y);
            let at = out_row + x * 3;
            rows[at] = value;
            rows[at + 1] = value;
            rows[at + 2] = value;
        }
    }
}

/// The first band whose ground this is. The order is the order of the Python
/// pass's if-else chain, so a street is a street before it is anything else.
#[inline]
fn band_of(
    palette: &crate::canvas::Palette,
    blob: &Blob,
    piece: &Piece,
    n: usize,
    index: u8,
) -> Option<usize> {
    for which in 0..n {
        let band = blob.spawn_band(piece.spawn_at as usize + which);
        if band.set == ANY_SET || palette.in_set(band.set, index) {
            return Some(which);
        }
    }
    None
}

/// How many stand on one spawn pixel: nobody at all most of the time, and
/// somewhere between the band's two fractions of the ceiling when they do.
#[inline]
fn density(band: &SpawnBand, seed: u32, ceiling: f32, x: usize, y: usize) -> u8 {
    let hash = rng::tile(seed, band.salt, x as i64, y as i64);
    if !rng::hit(hash, band.chance_of()) {
        return 0;
    }
    let lo = band.lo_of() * ceiling;
    let hi = band.hi_of() * ceiling;
    let span = (hi - lo).max(0.0);
    let value = lo + span * rng::unit(rng::again(hash));
    value.round().clamp(0.0, 255.0) as u8
}
