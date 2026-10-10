//! The passes that are not shapes: the things the Python renderer did tile by
//! tile with a random number generator and a numpy mask.
//!
//! There are six, and between them they cover every procedural pass of the
//! renderer: worn tarmac, tufts over a lawn, flowers between graves, dense bush
//! over wetland, trees in the rows of an orchard, the woods with their lighter
//! edge, the clay banks of rivers and lakes, and dark grass under anything
//! growing. What each one puts down, how often, and over what ground all come
//! from Python, so the look of a map is still decided where the rest of the map
//! is decided; what is here is the walking.
//!
//! None of them asks a question whose answer depends on what another thread is
//! doing. A tile's randomness comes from its own coordinates (see `rng`), a
//! patch that straddles two bands is worked out identically by both and each
//! writes only its own rows, and the two passes that do look at their
//! neighbours - the woods and the shores - read planes that were finished before
//! the pass began and that no thread is writing while they run.

use crate::blob::{
    Blob, Pass, PassKind, Pick, Piece, ANY_SET, NO_BIOME, NO_COLOUR, PASS_BARE, PASS_IN_MASK,
};
use crate::canvas::{self, Canvas};
use crate::err::Result;
use crate::raster::{Band, Paint};
use crate::rng;

/// Run one pass over one band.
pub fn run(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) -> Result<()> {
    canvas.prepare(blob);
    match pass.kind {
        PassKind::Scatter => {
            if pass.radius == 0 {
                scatter(blob, piece, canvas, pass, band);
            } else {
                blots(blob, piece, canvas, pass, band);
            }
        }
        PassKind::Grid => grid(blob, piece, canvas, pass, band),
        PassKind::Woodland => woodland(blob, canvas, pass, band),
        PassKind::Shores => shores(blob, canvas, pass, band),
        PassKind::Soften => soften(blob, piece, pass, band),
        PassKind::PaintMask => paint_mask(blob, canvas, pass, band),
        PassKind::Kerbs => kerbs(blob, piece, canvas, pass, band),
        PassKind::Near => near(blob, piece, canvas, pass, band),
        // The spawn map writes a picture of its own rather than the piece's, so
        // the driver runs it, not this.
        PassKind::Spawn => {}
    }
    Ok(())
}

/// A chance a tile: tufts over a lawn, flowers between graves, bushes over a
/// marsh.
fn scatter(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    let width = band.width;
    let mask = mask_rows(canvas, pass, band.y0, band.y1);
    if pass.flags & PASS_IN_MASK != 0 && !mask_has(mask.as_deref(), (pass.extra & 0xFF) as u8) {
        return;
    }
    for y in band.y0..band.y1 {
        for x in 0..width {
            let hash = rng::tile(piece.seed, pass.salt, x as i64, y as i64);
            if !rng::hit(hash, pass.chance) {
                continue;
            }
            if !lands(blob, piece, pass, band, mask.as_deref(), x, y) {
                continue;
            }
            if let Some(pick) = choose(blob, pass, rng::again(hash)) {
                band.put(x as i32, y as i32, &of(&pick));
            }
        }
    }
}

/// A chance a tile of a small round patch: worn tarmac, a clump of bushes.
///
/// The patch is a function of its centre alone, so a patch centred in the band
/// above is worked out again here and each band writes only its own rows. Every
/// tile of the patch is tested against the ground it lands on, rather than the
/// centre being tested once: the centre may belong to another thread's rows, and
/// testing every tile is what keeps a worn patch on the carriageway instead of
/// letting it spill onto the verge.
fn blots(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    let width = band.width;
    let reach = pass.radius as usize;
    let from = band.y0.saturating_sub(reach);
    let to = (band.y1 + reach).min(piece.height as usize);
    let mask = mask_rows(canvas, pass, band.y0, band.y1);
    for cy in from..to {
        for cx in 0..width {
            let hash = rng::tile(piece.seed, pass.salt, cx as i64, cy as i64);
            if !rng::hit(hash, pass.chance) {
                continue;
            }
            let second = rng::again(hash);
            let radius = 1 + rng::below(second, pass.radius.max(1) as u64) as i64;
            let pick = match choose(blob, pass, second) {
                Some(pick) => pick,
                None => continue,
            };
            let paint = of(&pick);
            let top = (cy as i64 - radius).max(band.y0 as i64);
            let bottom = (cy as i64 + radius).min(band.y1 as i64 - 1);
            let left = (cx as i64 - radius).max(0);
            let right = (cx as i64 + radius).min(width as i64 - 1);
            for y in top..=bottom {
                let dy = y - cy as i64;
                for x in left..=right {
                    let dx = x - cx as i64;
                    if dx * dx + dy * dy > radius * radius {
                        continue;
                    }
                    if lands(blob, piece, pass, band, mask.as_deref(), x as usize, y as usize) {
                        band.put(x as i32, y as i32, &paint);
                    }
                }
            }
        }
    }
}

/// Every `spacing` tiles both ways. What makes an orchard read as an orchard
/// from the air is that its trees stand in rows.
fn grid(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    let step = pass.spacing.max(1) as usize;
    let width = band.width;
    let chance = if pass.chance <= 0.0 { 1.0 } else { pass.chance };
    let mask = mask_rows(canvas, pass, band.y0, band.y1);
    if pass.flags & PASS_IN_MASK != 0 && !mask_has(mask.as_deref(), (pass.extra & 0xFF) as u8) {
        return;
    }
    // Rows are counted from the corner of the piece, not from the band, so that
    // the rows of an orchard do not step sideways at a band boundary.
    let mut y = band.y0 + (step - band.y0 % step) % step;
    while y < band.y1 {
        let mut x = 0;
        while x < width {
            if lands(blob, piece, pass, band, mask.as_deref(), x, y) {
                let hash = rng::tile(piece.seed, pass.salt, x as i64, y as i64);
                if rng::hit(hash, chance) {
                    if let Some(pick) = choose(blob, pass, rng::again(hash)) {
                        band.put(x as i32, y as i32, &of(&pick));
                    }
                }
            }
            x += step;
        }
        y += step;
    }
}

/// The woods. A tile deep inside a forest gets the full shade; one near its edge
/// gets the lighter mix, so a wood stops looking like a rectangle of trees.
/// Scrub is its own shade. Nothing is painted over water or over anything hard.
///
/// The outlines went into the scratch mask while the program was being drawn,
/// which is what makes the edge possible: whether a tile is deep inside a wood
/// is a question about its neighbours, and the mask is finished and read only by
/// the time this runs.
fn woodland(blob: &Blob, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    if !canvas.mask.live() || pass.pick_n == 0 {
        return;
    }
    let width = band.width;
    let edge = pass.spacing.max(1) as usize;
    // Which marks in the mask are this pass's. A map can have several kinds of
    // wood - pine on the hills, birch along the river - and each kind is its own
    // pass with its own marks, so one wood's tiles are never shaded as another's.
    let forest = (pass.extra & 0xFF) as u8;
    let scrub_value = ((pass.extra >> 8) & 0xFF) as u8;
    let origin = canvas.origin_y;
    let lo = band.y0.saturating_sub(edge).max(origin);
    let hi = (band.y1 + edge).min(origin + canvas.height);
    let mask = canvas.rows(
        &canvas.mask,
        lo,
        hi,
    );
    let rows = hi - lo;
    let full = blob.pick(pass.pick_at as usize);
    let fringe = if pass.pick_n > 1 {
        blob.pick(pass.pick_at as usize + 1)
    } else {
        full
    };
    let scrub = if pass.pick_n > 2 {
        Some(blob.pick(pass.pick_at as usize + 2))
    } else {
        None
    };
    for y in band.y0..band.y1 {
        let row = (y - lo) * width;
        for x in 0..width {
            let here = mask[row + x];
            if (here != forest && here != scrub_value) || !allowed(blob, pass, band, x, y) {
                continue;
            }
            if here == scrub_value {
                if let Some(pick) = scrub {
                    band.put(x as i32, y as i32, &of(&pick));
                }
                continue;
            }
            let core = deep(mask, width, rows, x, y - lo, edge, forest);
            let pick = if core { full } else { fringe };
            band.put(x as i32, y as i32, &of(&pick));
        }
    }
}

/// Whether every tile within `reach` is the same wood. This is the erosion the
/// Python pass got from a minimum filter. A tile at the edge of the piece has no
/// neighbours out there to ask, and what is off the picture counted as empty, so
/// it is never core.
fn deep(mask: &[u8], width: usize, rows: usize, x: usize, y: usize, reach: usize, forest: u8) -> bool {
    if x < reach || y < reach || x + reach >= width || y + reach >= rows {
        return false;
    }
    for row in y - reach..=y + reach {
        let at = row * width;
        for column in x - reach..=x + reach {
            if mask[at + column] != forest {
                return false;
            }
        }
    }
    true
}

/// The banks. Clay comes out a tile or two from every shore, and the clay of a
/// lake is not the clay of a river, so which one a bank gets depends on the
/// water it stands next to.
fn shores(blob: &Blob, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    if !canvas.has_water() || pass.pick_n == 0 {
        return;
    }
    let width = band.width;
    let reach = pass.spacing.max(1) as usize;
    let origin = canvas.origin_y;
    let lo = band.y0.saturating_sub(reach).max(origin);
    let hi = (band.y1 + reach).min(origin + canvas.height);
    let water = canvas.water_rows(lo, hi);
    let lake = canvas.lake_rows(lo, hi);
    let stride = canvas.bit_stride;
    let river_pick = blob.pick(pass.pick_at as usize);
    let lake_pick = if pass.pick_n > 1 {
        blob.pick(pass.pick_at as usize + 1)
    } else {
        river_pick
    };
    for y in band.y0..band.y1 {
        let row = (y - lo) * stride;
        for x in 0..width {
            if canvas::bit(&water[row..row + stride], x) {
                continue;
            }
            if !allowed(blob, pass, band, x, y) {
                continue;
            }
            let top = y.saturating_sub(reach).max(lo);
            let bottom = (y + reach).min(hi - 1);
            let left = x.saturating_sub(reach);
            let right = (x + reach).min(width - 1);
            let mut found = false;
            let mut still = false;
            'out: for near in top..=bottom {
                let at = (near - lo) * stride;
                let row_bits = &water[at..at + stride];
                let lake_bits = &lake[at..at + stride];
                for column in left..=right {
                    if canvas::bit(row_bits, column) {
                        found = true;
                        if canvas::bit(lake_bits, column) {
                            still = true;
                            break 'out;
                        }
                    }
                }
            }
            if !found {
                continue;
            }
            let pick = if still { lake_pick } else { river_pick };
            band.put(x as i32, y as i32, &of(&pick));
        }
    }
}

/// Dark grass under anything growing. Trees and bushes sit best on the darker
/// shade, and the Python renderer switched the ground under every tree it
/// painted for exactly that reason.
fn soften(blob: &Blob, piece: &Piece, pass: &Pass, band: &mut Band) {
    if pass.pick_n == 0 {
        return;
    }
    let width = band.width;
    let pick = blob.pick(pass.pick_at as usize);
    if pick.ground == NO_COLOUR {
        return;
    }
    let paint = Paint {
        ground: Some(canvas::rgb(pick.ground)),
        veg: None,
        biome: None,
        mask: None,
        water: false,
        lake: false,
    };
    for y in band.y0..band.y1 {
        for x in 0..width {
            let growing = match band.veg.as_deref() {
                Some(bytes) => {
                    let sample = bytes.as_ptr();
                    let row = canvas::band_row(canvas::origin_of(sample), y, band.y0);
                    let at = (row * width + x) * canvas::VEG_BPP;
                    match bytes.get(at).copied() {
                        Some(index) => {
                            index != canvas::colour_index(sample, canvas::rgb(piece.nothing))
                        }
                        None => false,
                    }
                }
                None => false,
            };
            if growing && allowed(blob, pass, band, x, y) {
                band.put(x as i32, y as i32, &paint);
            }
        }
    }
}

/// Whether this band's mask holds the mark at all. One look, so a pass over a
/// map that has the mark in one corner does not search the rest of it.
fn mask_has(mask: Option<&[u8]>, mark: u8) -> bool {
    match mask {
        Some(rows) => rows.contains(&mark),
        None => false,
    }
}

/// The scratch mask for this band, when the pass only lands on one mark in it.
fn mask_rows<'a>(canvas: &'a Canvas, pass: &Pass, y0: usize, y1: usize) -> Option<&'a [u8]> {
    if pass.flags & PASS_IN_MASK == 0 || !canvas.mask.live() {
        return None;
    }
    Some(canvas.rows(&canvas.mask, y0, y1))
}

/// Whether this tile is one the pass may change: the right ground, the right
/// mark, and nothing already growing when the pass asked for bare ground.
fn lands(
    blob: &Blob,
    piece: &Piece,
    pass: &Pass,
    band: &Band,
    mask: Option<&[u8]>,
    x: usize,
    y: usize,
) -> bool {
    if !allowed(blob, pass, band, x, y) {
        return false;
    }
    if pass.flags & PASS_IN_MASK != 0 {
        let rows = match mask {
            Some(rows) => rows,
            None => return false,
        };
        let at = (y - band.y0) * band.width + x;
        if rows.get(at).copied().unwrap_or(0) != (pass.extra & 0xFF) as u8 {
            return false;
        }
    }
    if pass.flags & PASS_BARE != 0 && !bare(piece, band, x, y) {
        return false;
    }
    true
}

/// Nothing growing on this tile yet.
fn bare(piece: &Piece, band: &Band, x: usize, y: usize) -> bool {
    let bytes = match band.veg.as_deref() {
        Some(bytes) => bytes,
        None => return true,
    };
    if x >= band.width || y < band.y0 || y >= band.y1 {
        return false;
    }
    let sample = bytes.as_ptr();
    let row = canvas::band_row(canvas::origin_of(sample), y, band.y0);
    let at = (row * band.width + x) * canvas::VEG_BPP;
    bytes.get(at).copied() == Some(canvas::colour_index(sample, canvas::rgb(piece.nothing)))
}

/// Where the scratch mask holds this pass's mark, paint the pick. A mark of
/// zero is "not marked", so it never paints.
fn paint_mask(blob: &Blob, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    if !canvas.mask.live() || pass.pick_n == 0 {
        return;
    }
    let mark = (pass.extra & 0xFF) as u8;
    if mark == 0 {
        return;
    }
    let width = band.width;
    let mask = canvas.rows(
        &canvas.mask,
        band.y0,
        band.y1,
    );
    let paint = of(&blob.pick(pass.pick_at as usize));
    for y in band.y0..band.y1 {
        let row = (y - band.y0) * width;
        for x in 0..width {
            if mask[row + x] != mark || !allowed(blob, pass, band, x, y) {
                continue;
            }
            band.put(x as i32, y as i32, &paint);
        }
    }
}

/// The eight kerb tiles, in the order the program sends their colours: road to
/// the west, the north, the south, the east, then the four corners.
const KERB_BITS: [u8; 8] = [1, 2, 4, 8, 3, 5, 10, 12];

/// A pavement tile beside a road. The road colours are the pass's avoid set,
/// and the pavement is its over set. This reads the ground and writes only the
/// vegetation, so the ground of the rows next door can be read while this band
/// is being written.
fn kerbs(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    if pass.pick_n < KERB_BITS.len() as u16 {
        return;
    }
    let width = band.width;
    let origin = canvas.origin_y;
    let limit = origin + canvas.height;
    let top = band.y0.saturating_sub(1).max(origin);
    let bottom = (band.y1 + 1).min(limit);
    let ground = canvas.rows(
        &canvas.ground,
        top,
        bottom,
    );
    let stride = width * canvas::GROUND_BPP;
    let sample = ground.as_ptr();
    let colours: Vec<Paint> = (0..KERB_BITS.len())
        .map(|at| of(&blob.pick(pass.pick_at as usize + at)))
        .collect();
    let asphalt = |x: usize, y: usize| -> bool {
        if y < top || y >= bottom || x >= width {
            return false;
        }
        let at = (y - top) * stride + x * canvas::GROUND_BPP;
        match ground.get(at).copied() {
            Some(index) => canvas::index_in_set(sample, pass.avoid, index),
            None => false,
        }
    };
    for y in band.y0..band.y1 {
        let at = (y - top) * stride;
        for x in 0..width {
            let Some(here) = ground.get(at + x * canvas::GROUND_BPP).copied() else {
                continue;
            };
            if pass.over != ANY_SET && !canvas::index_in_set(sample, pass.over, here)
                || !bare(piece, band, x, y)
            {
                continue;
            }
            let mut bits = 0u8;
            if x > 0 && asphalt(x - 1, y) {
                bits |= 1;
            }
            if y > 0 && asphalt(x, y - 1) {
                bits |= 2;
            }
            if y + 1 < limit && asphalt(x, y + 1) {
                bits |= 4;
            }
            if x + 1 < width && asphalt(x + 1, y) {
                bits |= 8;
            }
            if let Some(which) = KERB_BITS.iter().position(|kind| *kind == bits) {
                band.put(x as i32, y as i32, &colours[which]);
            }
        }
    }
}

/// Tiles on a grid that sit near a mark and not on top of it. `extra` is the
/// mark, how far the yard reaches, how far off the wall a trunk stays, and how
/// far off the paving, each in one byte.
fn near(blob: &Blob, piece: &Piece, canvas: &Canvas, pass: &Pass, band: &mut Band) {
    if !canvas.mask.live() || pass.pick_n == 0 {
        return;
    }
    let mark = (pass.extra & 0xFF) as u8;
    let reach = ((pass.extra >> 8) & 0xFF) as usize;
    let clear = ((pass.extra >> 16) & 0xFF) as usize;
    let paving = ((pass.extra >> 24) & 0xFF) as usize;
    if reach == 0 || mark == 0 {
        return;
    }
    let width = band.width;
    let step = pass.spacing.max(1) as usize;
    let origin = canvas.origin_y;
    let lo = band.y0.saturating_sub(reach).max(origin);
    let hi = (band.y1 + reach).min(origin + canvas.height);
    let mask = canvas.rows(
        &canvas.mask,
        lo,
        hi,
    );
    if !mask_has(Some(mask), mark) {
        return;
    }
    let rows = hi - lo;
    let chance = if pass.chance <= 0.0 { 1.0 } else { pass.chance };
    let pick = of(&blob.pick(pass.pick_at as usize));
    let mut y = band.y0 + (step - band.y0 % step) % step;
    while y < band.y1 {
        let mut x = 0;
        while x < width {
            if allowed(blob, pass, band, x, y)
                && (pass.flags & PASS_BARE == 0 || bare(piece, band, x, y))
                && near_mark(mask, width, rows, x, y - lo, mark, clear, reach)
                && !near_paving(blob, pass, band, x, y, paving)
            {
                let hash = rng::tile(piece.seed, pass.salt, x as i64, y as i64);
                if rng::hit(hash, chance) {
                    band.put(x as i32, y as i32, &pick);
                }
            }
            x += step;
        }
        y += step;
    }
}

/// Chebyshev distance to the nearest mark, and whether it falls in range.
fn near_mark(
    mask: &[u8],
    width: usize,
    rows: usize,
    x: usize,
    y: usize,
    mark: u8,
    clear: usize,
    reach: usize,
) -> bool {
    let top = y.saturating_sub(reach);
    let bottom = (y + reach).min(rows.saturating_sub(1));
    let left = x.saturating_sub(reach);
    let right = (x + reach).min(width.saturating_sub(1));
    let mut best = usize::MAX;
    for row in top..=bottom {
        let at = row * width;
        for column in left..=right {
            if mask[at + column] != mark {
                continue;
            }
            let dist = (row as isize - y as isize)
                .unsigned_abs()
                .max((column as isize - x as isize).unsigned_abs());
            if dist < best {
                best = dist;
            }
        }
    }
    best >= clear && best <= reach
}

/// Whether paving, a road or water sits within `reach` of this tile. The yard
/// stops short of the kerb. Tiles outside this band are treated as open, which
/// costs a tree on the seam and nothing else.
fn near_paving(blob: &Blob, pass: &Pass, band: &Band, x: usize, y: usize, reach: usize) -> bool {
    let _ = blob;
    if reach == 0 || pass.avoid == ANY_SET {
        return false;
    }
    let top = y.saturating_sub(reach).max(band.y0);
    let bottom = (y + reach).min(band.y1.saturating_sub(1));
    let left = x.saturating_sub(reach);
    let right = (x + reach).min(band.width.saturating_sub(1));
    for row in top..=bottom {
        for column in left..=right {
            if let Some(ground) = band.ground_at(column, row) {
                let sample = band.ground.as_deref().map(|bytes| bytes.as_ptr()).unwrap_or(std::ptr::null());
                if canvas::index_in_set(sample, pass.avoid, ground) {
                    return true;
                }
            }
        }
    }
    false
}

/// Whether a pass may touch this tile: the ground has to be something it is
/// allowed over, and not something it must keep off.
#[inline]
fn allowed(blob: &Blob, pass: &Pass, band: &Band, x: usize, y: usize) -> bool {
    let _ = blob;
    if pass.over == ANY_SET && pass.avoid == ANY_SET {
        return true;
    }
    let Some(index) = band.ground_at(x, y) else {
        return false;
    };
    let sample = band
        .ground
        .as_deref()
        .map(|bytes| bytes.as_ptr())
        .unwrap_or(std::ptr::null());
    (pass.over == ANY_SET || canvas::index_in_set(sample, pass.over, index))
        && (pass.avoid == ANY_SET || !canvas::index_in_set(sample, pass.avoid, index))
}

/// Which of a pass's picks this tile gets, by weight.
#[inline]
fn choose(blob: &Blob, pass: &Pass, hash: u64) -> Option<Pick> {
    let n = pass.pick_n as usize;
    if n == 0 {
        return None;
    }
    if n == 1 {
        return Some(blob.pick(pass.pick_at as usize));
    }
    let mut total = 0u64;
    for which in 0..n {
        total += blob.pick(pass.pick_at as usize + which).weight as u64;
    }
    if total == 0 {
        return Some(blob.pick(pass.pick_at as usize + rng::below(hash, n as u64) as usize));
    }
    let mut roll = rng::below(hash, total);
    for which in 0..n {
        let pick = blob.pick(pass.pick_at as usize + which);
        let weight = pick.weight as u64;
        if roll < weight {
            return Some(pick);
        }
        roll -= weight;
    }
    Some(blob.pick(pass.pick_at as usize + n - 1))
}

/// A pick as something the rasteriser can put down.
#[inline]
fn of(pick: &Pick) -> Paint {
    Paint {
        ground: if pick.ground == NO_COLOUR {
            None
        } else {
            Some(canvas::rgb(pick.ground))
        },
        veg: if pick.veg == NO_COLOUR {
            None
        } else {
            Some(canvas::rgb(pick.veg))
        },
        biome: if pick.biome == NO_BIOME {
            None
        } else {
            Some(pick.biome as u8)
        },
        mask: None,
        water: false,
        lake: false,
    }
}
