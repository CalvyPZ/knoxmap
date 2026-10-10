//! Drawing outlines, lines and discs into a band of the pictures.
//!
//! This is the one place pixels are set from a shape, and every pass goes
//! through it, so the rules are in one place:
//!
//! * A tile is painted when its centre is inside the shape. Two land-use
//!   polygons that share an edge therefore meet exactly: neither leaves a line
//!   of grass between them nor paints a tile twice. PIL's own rule is inclusive
//!   at both ends, which painted shared edges twice and grew every polygon a
//!   tile to the south and east.
//! * A shape thinner than one tile still paints one tile. A kerb, a path or a
//!   hedge mapped as a hairline is a real thing on the ground, and a rule that
//!   only paints tile centres would lose it.
//! * Nothing is drawn outside the band. A band is a range of rows one thread
//!   owns; the same command is handed to every band it reaches into, and each
//!   band draws its own rows of it. That is what lets one picture be drawn by
//!   every core at once with no lock.

use crate::canvas::{self, rgb, GROUND_BPP, VEG_BPP};

/// What a command paints, resolved to bytes.
#[derive(Clone, Copy)]
pub struct Paint {
    pub ground: Option<[u8; 3]>,
    pub veg: Option<[u8; 3]>,
    pub biome: Option<u8>,
    /// A value for the scratch mask, which is how a wood's outline is recorded
    /// for the pass that gives it an edge.
    pub mask: Option<u8>,
    pub water: bool,
    pub lake: bool,
}

impl Paint {
    pub fn of(ground: Option<u32>, veg: Option<u32>, biome: Option<u8>) -> Paint {
        Paint {
            ground: ground.map(rgb),
            veg: veg.map(rgb),
            biome,
            mask: None,
            water: false,
            lake: false,
        }
    }

    pub fn is_nothing(&self) -> bool {
        self.ground.is_none()
            && self.veg.is_none()
            && self.biome.is_none()
            && self.mask.is_none()
            && !self.water
            && !self.lake
    }
}

/// The rows of one piece that one thread owns, and the planes it may write.
pub struct Band<'a> {
    pub y0: usize,
    pub y1: usize,
    pub width: usize,
    pub ground: Option<&'a mut [u8]>,
    pub veg: Option<&'a mut [u8]>,
    pub biome: Option<&'a mut [u8]>,
    pub mask: Option<&'a mut [u8]>,
    pub water: Option<&'a mut [u8]>,
    pub lake: Option<&'a mut [u8]>,
    /// Bytes a row of the bit masks, which are padded so that no two bands share
    /// a byte of them.
    pub bit_stride: usize,
}

impl<'a> Band<'a> {
    /// Tiles `x0` to `x1` inclusive of row `y`, clipped to the band and to the
    /// picture.
    #[inline]
    pub fn hline(&mut self, y: i32, x0: i32, x1: i32, paint: &Paint) {
        if y < self.y0 as i32 || y >= self.y1 as i32 {
            return;
        }
        let (mut from, mut to) = (x0.min(x1), x0.max(x1));
        if to < 0 || from >= self.width as i32 {
            return;
        }
        from = from.max(0);
        to = to.min(self.width as i32 - 1);
        let span = (to - from + 1) as usize;
        let at = from as usize;
        if let Some(colour) = paint.ground {
            if let Some(bytes) = self.ground.as_deref_mut() {
                let (origin, index) = canvas::map_colour(bytes.as_ptr(), colour);
                let row = canvas::band_row(origin, y as usize, self.y0);
                let start = (row * self.width + at) * GROUND_BPP;
                let end = start + span * GROUND_BPP;
                if end <= bytes.len() {
                    bytes[start..end].fill(index);
                }
            }
        }
        if let Some(colour) = paint.veg {
            if let Some(bytes) = self.veg.as_deref_mut() {
                let (origin, index) = canvas::map_colour(bytes.as_ptr(), colour);
                let row = canvas::band_row(origin, y as usize, self.y0);
                let start = (row * self.width + at) * VEG_BPP;
                let end = start + span * VEG_BPP;
                if end <= bytes.len() {
                    bytes[start..end].fill(index);
                }
            }
        }
        let row = canvas::band_row(origin_of_band(self), y as usize, self.y0);
        if let (Some(bytes), Some(value)) = (self.biome.as_deref_mut(), paint.biome) {
            let start = row * self.width + at;
            if start + span <= bytes.len() {
                bytes[start..start + span].fill(value);
            }
        }
        if let (Some(bytes), Some(value)) = (self.mask.as_deref_mut(), paint.mask) {
            let start = row * self.width + at;
            if start + span <= bytes.len() {
                bytes[start..start + span].fill(value);
            }
        }
        let stride = self.bit_stride;
        if paint.water {
            if let Some(bytes) = self.water.as_deref_mut() {
                set_bits(&mut bytes[row * stride..(row + 1) * stride], at, span);
            }
        }
        if paint.lake {
            if let Some(bytes) = self.lake.as_deref_mut() {
                set_bits(&mut bytes[row * stride..(row + 1) * stride], at, span);
            }
        }
    }

    /// The palette index on the ground at one tile of this band. A band owns
    /// its rows, so it may read back what it has already painted - which is how
    /// a pass knows not to put a tree on tarmac.
    #[inline]
    pub fn ground_at(&self, x: usize, y: usize) -> Option<u8> {
        let bytes = self.ground.as_deref()?;
        if x >= self.width || y < self.y0 || y >= self.y1 {
            return None;
        }
        let origin = canvas::origin_of(bytes.as_ptr());
        let row = canvas::band_row(origin, y, self.y0);
        bytes.get((row * self.width + x) * GROUND_BPP).copied()
    }

    /// One tile, where a pass has worked out for itself what goes there.
    #[inline]
    pub fn put(&mut self, x: i32, y: i32, paint: &Paint) {
        self.hline(y, x, x, paint);
    }
}

/// Tiles `at` to `at + n` of one row of a bit mask.
#[inline]
fn set_bits(row: &mut [u8], at: usize, n: usize) {
    for x in at..at + n {
        crate::canvas::set_bit(row, x);
    }
}

/// Piece row of canvas row 0, from whichever plane this band is holding.
fn origin_of_band(band: &Band) -> usize {
    let sample = band
        .ground
        .as_deref()
        .or(band.veg.as_deref())
        .or(band.biome.as_deref())
        .or(band.mask.as_deref());
    match sample {
        Some(bytes) => canvas::origin_of(bytes.as_ptr()),
        None => 0,
    }
}

/// One edge of an outline, ready for the scanline walk: where it starts, how far
/// it leans, and the rows it covers. Horizontal edges are dropped - they cross
/// no scanline - which is also what makes the fill watertight.
#[derive(Clone, Copy)]
struct Edge {
    /// The rows this edge is crossed on, as tile centres: y + 0.5 in [lo, hi).
    lo: f32,
    hi: f32,
    /// x where the edge meets y = lo, and how much x moves per row.
    x: f32,
    slope: f32,
}

fn edges_of(ring: &[(f32, f32)], out: &mut Vec<Edge>) {
    if ring.len() < 3 {
        return;
    }
    let mut last = ring[ring.len() - 1];
    for point in ring {
        let (x0, y0) = last;
        let (x1, y1) = *point;
        last = *point;
        if y0 == y1 {
            continue;
        }
        let (top, bottom, xt) = if y0 < y1 {
            (y0, y1, x0)
        } else {
            (y1, y0, x1)
        };
        out.push(Edge {
            lo: top,
            hi: bottom,
            x: xt,
            slope: (x1 - x0) / (y1 - y0),
        });
    }
}

/// Where the outline crosses the centre of row `y`, in order.
fn crossings(edges: &[Edge], y: i32, out: &mut Vec<f32>) {
    out.clear();
    let at = y as f32 + 0.5;
    for edge in edges {
        if at >= edge.lo && at < edge.hi {
            out.push(edge.x + (at - edge.lo) * edge.slope);
        }
    }
    out.sort_unstable_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
}

/// The tiles between two crossings: the ones whose centre is inside.
#[inline]
fn tiles_between(from: f32, to: f32) -> (i32, i32) {
    let first = (from - 0.5).ceil() as i32;
    let last = (to - 0.5).ceil() as i32 - 1;
    (first, last)
}

/// The rows of a shape that fall in this band.
fn rows_of(band: &Band, lo: f32, hi: f32) -> (i32, i32) {
    let first = ((lo - 0.5).ceil() as i32).max(band.y0 as i32);
    let last = (((hi - 0.5).ceil() as i32) - 1).min(band.y1 as i32 - 1);
    (first, last)
}

/// Fill one outline. Rings are filled one after another, each on its own, which
/// is what PIL's `polygon` does and what the ground pass relies on: a lake drawn
/// as two rings is two lakes, not one with a hole in it.
pub fn fill(band: &mut Band, ring: &[(f32, f32)], paint: &Paint, work: &mut Work) {
    if ring.len() < 3 || paint.is_nothing() {
        return;
    }
    work.edges.clear();
    edges_of(ring, &mut work.edges);
    if work.edges.is_empty() {
        return;
    }
    let (mut lo, mut hi) = (f32::MAX, f32::MIN);
    let (mut left, mut right) = (f32::MAX, f32::MIN);
    for (x, y) in ring {
        lo = lo.min(*y);
        hi = hi.max(*y);
        left = left.min(*x);
        right = right.max(*x);
    }
    // Thinner than a tile either way: paint the one line of tiles it covers, so
    // a hairline path or a hedge mapped as a sliver is still there.
    if hi - lo < 1.0 || right - left < 1.0 {
        thin(band, left, lo, right, hi, paint);
        return;
    }
    let (first, last) = rows_of(band, lo, hi);
    for y in first..=last {
        crossings(&work.edges, y, &mut work.xs);
        let mut at = 0;
        while at + 1 < work.xs.len() {
            let (x0, x1) = tiles_between(work.xs[at], work.xs[at + 1]);
            if x1 >= x0 {
                band.hline(y, x0, x1, paint);
            } else {
                // A span narrower than a tile: the sliver rule again, one tile
                // wide, so a lane between two buildings does not vanish.
                band.hline(y, x0, x0, paint);
            }
            at += 2;
        }
    }
}

/// A shape too thin to have an inside: the row or column of tiles it covers.
fn thin(band: &mut Band, left: f32, top: f32, right: f32, bottom: f32, paint: &Paint) {
    let x0 = (left - 0.5).ceil() as i32;
    let x1 = ((right - 0.5).ceil() as i32 - 1).max(x0);
    let y0 = (top - 0.5).ceil() as i32;
    let y1 = ((bottom - 0.5).ceil() as i32 - 1).max(y0);
    for y in y0..=y1 {
        band.hline(y, x0, x1, paint);
    }
}

/// One ring inside a flat buffer of points, and what it is to its relation.
#[derive(Clone, Copy)]
pub struct Span {
    pub at: usize,
    pub n: usize,
    pub inner: bool,
}

/// Fill a multipolygon: inside an outer ring and outside every inner one. A
/// courtyard, a lake with an island, a wood with a field in it.
pub fn relation(
    band: &mut Band,
    points: &[(f32, f32)],
    rings: &[Span],
    paint: &Paint,
    work: &mut Work,
) {
    if rings.is_empty() || paint.is_nothing() {
        return;
    }
    work.edges.clear();
    work.holes.clear();
    let (mut lo, mut hi) = (f32::MAX, f32::MIN);
    for span in rings {
        if span.at + span.n > points.len() {
            continue;
        }
        let ring = &points[span.at..span.at + span.n];
        if span.inner {
            edges_of(ring, &mut work.holes);
            continue;
        }
        edges_of(ring, &mut work.edges);
        for (_x, y) in ring.iter() {
            lo = lo.min(*y);
            hi = hi.max(*y);
        }
    }
    if work.edges.is_empty() {
        return;
    }
    let (first, last) = rows_of(band, lo, hi);
    for y in first..=last {
        crossings(&work.edges, y, &mut work.xs);
        crossings(&work.holes, y, &mut work.hole_xs);
        let mut at = 0;
        while at + 1 < work.xs.len() {
            let (x0, x1) = tiles_between(work.xs[at], work.xs[at + 1]);
            at += 2;
            if x1 < x0 {
                band.hline(y, x0, x0, paint);
                continue;
            }
            // Walk the holes on this row and paint what is left of the span.
            let mut from = x0;
            let mut hole = 0;
            while hole + 1 < work.hole_xs.len() {
                let (h0, h1) = tiles_between(work.hole_xs[hole], work.hole_xs[hole + 1]);
                hole += 2;
                if h1 < from || h0 > x1 {
                    continue;
                }
                if h0 > from {
                    band.hline(y, from, (h0 - 1).min(x1), paint);
                }
                from = from.max(h1 + 1);
                if from > x1 {
                    break;
                }
            }
            if from <= x1 {
                band.hline(y, from, x1, paint);
            }
        }
    }
}

/// A line of the given width, with a round cap at every vertex so that a bend
/// is filled and a junction of two roads has no notch in it. This is what
/// `draw.line(..., width=w, joint="curve")` and the circle at each vertex came
/// to in Python.
pub fn stroke(band: &mut Band, ring: &[(f32, f32)], width: u16, paint: &Paint, work: &mut Work) {
    if ring.len() < 2 || paint.is_nothing() {
        return;
    }
    let w = (width.max(1)) as f32;
    let half = w / 2.0;
    for pair in ring.windows(2) {
        let (x0, y0) = pair[0];
        let (x1, y1) = pair[1];
        let (dx, dy) = (x1 - x0, y1 - y0);
        let len = (dx * dx + dy * dy).sqrt();
        if len <= f32::EPSILON {
            continue;
        }
        // The quad of this segment: half a width either side of the centre line.
        let (nx, ny) = (-dy / len * half, dx / len * half);
        work.quad[0] = (x0 + nx, y0 + ny);
        work.quad[1] = (x1 + nx, y1 + ny);
        work.quad[2] = (x1 - nx, y1 - ny);
        work.quad[3] = (x0 - nx, y0 - ny);
        let quad = work.quad;
        fill(band, &quad, paint, work);
    }
    if w >= 2.0 {
        let r = (width / 2) as f32;
        for (x, y) in ring {
            disc(band, *x, *y, r, paint);
        }
    }
}

/// A filled circle. The radius is in tiles; a radius under half a tile is the
/// one tile it stands on.
pub fn disc(band: &mut Band, cx: f32, cy: f32, r: f32, paint: &Paint) {
    if paint.is_nothing() {
        return;
    }
    if r < 0.5 {
        band.put(cx.floor() as i32, cy.floor() as i32, paint);
        return;
    }
    let (first, last) = rows_of(band, cy - r, cy + r);
    for y in first..=last {
        let dy = y as f32 + 0.5 - cy;
        let inside = r * r - dy * dy;
        if inside < 0.0 {
            continue;
        }
        let half = inside.sqrt();
        let (x0, x1) = tiles_between(cx - half, cx + half);
        if x1 >= x0 {
            band.hline(y, x0, x1, paint);
        } else {
            band.hline(y, x0, x0, paint);
        }
    }
}

/// A rectangle, both corners included, as PIL's `rectangle` draws it.
pub fn rect(band: &mut Band, x0: f32, y0: f32, x1: f32, y1: f32, paint: &Paint) {
    if paint.is_nothing() {
        return;
    }
    let left = x0.min(x1).round() as i32;
    let right = x0.max(x1).round() as i32;
    let top = y0.min(y1).round() as i32;
    let bottom = y0.max(y1).round() as i32;
    for y in top.max(band.y0 as i32)..=bottom.min(band.y1 as i32 - 1) {
        band.hline(y, left, right, paint);
    }
}

/// The lists the rasterisers reuse. One per thread, kept for the life of the
/// pool, so drawing ten million outlines allocates nothing.
pub struct Work {
    edges: Vec<Edge>,
    holes: Vec<Edge>,
    xs: Vec<f32>,
    hole_xs: Vec<f32>,
    quad: [(f32, f32); 4],
}

impl Work {
    pub fn new() -> Work {
        Work {
            edges: Vec::new(),
            holes: Vec::new(),
            xs: Vec::new(),
            hole_xs: Vec::new(),
            quad: [(0.0, 0.0); 4],
        }
    }

}

impl Default for Work {
    fn default() -> Work {
        Work::new()
    }
}
