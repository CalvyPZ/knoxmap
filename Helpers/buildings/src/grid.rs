//! The room-grid kernels, ported from KnoxMap/knoxbuild/grids.py.
//!
//! Wall coordinates follow BuildingEd, as they do there: a west edge sits on
//! the west side of a tile and a north edge on its north side. The orders are
//! part of the interface, not an accident - the layout walks exterior runs in
//! the order they come out, and a door lands on the middle tile of a run - so
//! every function here produces its results in the same order the NumPy and
//! SciPy versions did.
//!
//! These are flat loops over `&[i32]` rather than array expressions, which is
//! why none of SciPy comes with us. A storey is at most a few hundred tiles
//! across; the work is in how often it is done, not in how big it is.

/// A room grid: 1-based room ids, row-major, 0 for a tile that is not in the
/// building. Borrowed, never owned, so it can sit on a worker's scratch.
#[derive(Clone, Copy)]
pub struct Grid<'a> {
    pub cells: &'a [i32],
    pub width: usize,
    pub height: usize,
}

impl<'a> Grid<'a> {
    pub fn new(cells: &'a [i32], width: usize, height: usize) -> Grid<'a> {
        debug_assert!(cells.len() >= width * height);
        Grid {
            cells,
            width,
            height,
        }
    }

    #[inline]
    pub fn at(&self, x: usize, y: usize) -> i32 {
        self.cells[y * self.width + x]
    }

}

/// Which side of a tile a wall, door or window sits on. BuildingEd stores a
/// wall on the West or North edge of a tile and nowhere else: the wall on the
/// east side of a tile is the West edge of the tile beyond it.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Side {
    West,
    North,
}

/// A summed-area table of the footprint, `(width + 1)` by `(height + 1)`, so the
/// number of set tiles in any box is four reads. The room splitter asks this of
/// every region it cuts, and the setback erosion of every tile.
pub fn sat(mask: &[bool], width: usize, height: usize, out: &mut Vec<i32>) {
    let stride = width + 1;
    out.clear();
    out.resize(stride * (height + 1), 0);
    for y in 0..height {
        let mut row = 0i32;
        for x in 0..width {
            row += mask[y * width + x] as i32;
            out[(y + 1) * stride + x + 1] = out[y * stride + x + 1] + row;
        }
    }
}

/// The sum of a box from a table built by `sat`.
#[inline]
pub fn sat_box(table: &[i32], stride: usize, x0: usize, y0: usize, x1: usize, y1: usize) -> i32 {
    table[y1 * stride + x1] - table[y0 * stride + x1] - table[y1 * stride + x0]
        + table[y0 * stride + x0]
}

/// Binary erosion with a solid rectangle of `2 * rx + 1` by `2 * ry + 1`, the
/// tower setback footprint. Tiles past the edge count as unset, so a tile
/// within the radius of the outside is cleared, as grids.erode does.
pub fn erode(
    mask: &[bool],
    width: usize,
    height: usize,
    rx: usize,
    ry: usize,
    out: &mut Vec<bool>,
) {
    out.clear();
    out.resize(width * height, false);
    if width == 0 || height == 0 {
        return;
    }
    // One summed-area table makes the whole pass a read per tile, whatever the
    // radius: a 30-storey tower's setback is not worth a nested loop.
    let mut table: Vec<i32> = Vec::new();
    sat(mask, width, height, &mut table);
    let stride = width + 1;
    let bw = 2 * rx + 1;
    let bh = 2 * ry + 1;
    let need = (bw * bh) as i32;
    for y in 0..height {
        for x in 0..width {
            if x < rx || y < ry || x + rx >= width || y + ry >= height {
                continue;
            }
            let full = sat_box(
                &table,
                stride,
                x - rx,
                y - ry,
                x + rx + 1,
                y + ry + 1,
            ) == need;
            out[y * width + x] = full;
        }
    }
}

/// The room-grid text for a `<rooms>` body, before XML escaping: a leading
/// newline, every row, a comma after every cell but the last of the grid.
///
/// offset is added to every room id, and never to 0: a storey's rooms are
/// numbered on from the storey below, and 0 is outside on every floor.
pub fn rooms_text(g: &Grid, offset: i32, out: &mut String) {
    out.clear();
    if g.width == 0 {
        for _ in 0..g.height + 1 {
            out.push('\n');
        }
        return;
    }
    if g.height == 0 {
        out.push('\n');
        return;
    }
    out.push('\n');
    let mut buf = itoa_buf();
    for y in 0..g.height {
        for x in 0..g.width {
            let id = g.at(x, y);
            push_int(out, if id == 0 { 0 } else { id + offset }, &mut buf);
            if x + 1 < g.width {
                out.push(',');
            }
        }
        if y + 1 < g.height {
            out.push(',');
        }
        out.push('\n');
    }
}

fn itoa_buf() -> [u8; 12] {
    [0; 12]
}

/// Write a small integer without building a String for it. The room grid of a
/// mall is a quarter of a million cells; `format!` for each was most of the
/// time the .tbx writer spent.
fn push_int(out: &mut String, value: i32, buf: &mut [u8; 12]) {
    if value == 0 {
        out.push('0');
        return;
    }
    let negative = value < 0;
    let mut left = (value as i64).unsigned_abs();
    let mut at = buf.len();
    while left > 0 {
        at -= 1;
        buf[at] = b'0' + (left % 10) as u8;
        left /= 10;
    }
    if negative {
        out.push('-');
    }
    // Only ASCII digits were written, so this is valid UTF-8 by construction.
    out.push_str(std::str::from_utf8(&buf[at..]).unwrap_or("0"));
}
