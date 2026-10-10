//! One band of a piece, and the class bytes taken from it.
//!
//! The canvas for a band is opened in `piece`. This is the hook that runs once
//! those rows are finished and about to be written: the ground is still the
//! band, not the whole mod.

use crate::classify;
use crate::err::{Error, Result};

/// Record the ground of piece rows starting at `y0`.
///
/// `rows` is the stored ground, `bpp` bytes a tile. Three bytes are red, green,
/// blue. One byte is a palette index, and `palette[index]` is that colour.
/// The class file is filled from here, one band at a time, because the ground
/// picture is not kept.
pub fn note_ground_rows(
    out: &mut classify::Out,
    y0: usize,
    width: usize,
    rows: &[u8],
    bpp: usize,
    palette: &[[u8; 3]],
) -> Result<()> {
    if width == 0 || bpp == 0 {
        return Ok(());
    }
    let stride = width.saturating_mul(bpp);
    if stride == 0 || rows.len() % stride != 0 {
        return Err(Error::BadState);
    }
    let height = rows.len() / stride;
    let classes = if bpp >= 3 {
        classify::from_rgb(rows, width, height)
    } else {
        classify::from_indexes(rows, palette, width, height)
    };
    let classes = classes.ok_or(Error::BadState)?;
    out.rows(y0, &classes)
}
