//! One byte a tile beside the ground bitmap, so a later pass can ask what the
//! ground is without opening the picture.
//!
//! The file is `{name}_ground_class.bin`, in the same directory as `{name}.bmp`
//! and `{name}_ground_base.bmp`. Eight bytes of magic, then the width and the
//! height as little-endian `u32`, then `width * height` class bytes, row by
//! row from the top. The colours are the ones the building stage already
//! compares, taken from the landscape palette.

use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

use crate::err::{Error, Result};

const MAGIC: &[u8; 8] = b"KNOXCLS1";
const HEAD: usize = 16;

/// What one ground tile is, as the byte stored in the class file.
///
/// `0` other, `1` pale concrete, `2` carriageway, `3` medium or light grass,
/// `4` water. The rest are splits the scans disagree on, so they cannot share
/// one of those five.
#[repr(u8)]
#[derive(Clone, Copy)]
pub enum Class {
    /// Sand, clay, paving stone, and any colour no scan names.
    Other = 0,
    /// Pale concrete `(120, 120, 120)`: pavements and kerbs. Road weight is 1.
    Concrete = 1,
    /// Carriageway asphalt, and both pothole shades: `(150, 150, 150)`,
    /// `(80, 80, 80)`, `(110, 100, 100)`, `(130, 120, 120)`. Road weight is 2.
    /// Yard paths and the street-side finder treat this whole class as paved,
    /// so a pothole tile counts as road there too.
    Carriageway = 2,
    /// Medium grass `(117, 117, 47)` and light grass `(145, 135, 60)`.
    /// Dark grass is [`Class::DarkGrass`].
    Grass = 3,
    /// Open water `(0, 138, 255)`.
    Water = 4,
    /// Dark grass `(90, 100, 35)` on its own. Mapped cells leave a cell to the
    /// game only when every tile is this shade; parks use a lighter grass and
    /// stay mapped. Yards treat this, [`Class::Grass`] and [`Class::Dirt`] as
    /// ground a path may cross.
    DarkGrass = 5,
    /// Dark asphalt `(100, 100, 100)`, the car-park tarmac. Yards, kerbside
    /// stalls and fences count it. Road weight does not, and nor do the pump
    /// and prop masks that mean "this is the carriageway".
    Parking = 6,
    /// Sandstone slabs `(170, 160, 130)`. Zone hard-ground and the street
    /// population count them. Road weight and yard paths do not: this is not
    /// the pale concrete of a pavement.
    Plaza = 7,
    /// Light asphalt `(165, 160, 140)`, the beige gravel of yards and track
    /// beds. Fences and the street population count it. It is not a carriageway.
    Gravel = 8,
    /// Bare dirt `(120, 70, 20)`. Yards may cross it. It is not grass, and clay
    /// is not this class.
    Dirt = 9,
}

impl Class {
    fn of(r: u8, g: u8, b: u8) -> Class {
        match (r, g, b) {
            (120, 120, 120) => Class::Concrete,
            (150, 150, 150) | (80, 80, 80) | (110, 100, 100) | (130, 120, 120) => {
                Class::Carriageway
            }
            (117, 117, 47) | (145, 135, 60) => Class::Grass,
            (0, 138, 255) => Class::Water,
            (90, 100, 35) => Class::DarkGrass,
            (100, 100, 100) => Class::Parking,
            (170, 160, 130) => Class::Plaza,
            (165, 160, 140) => Class::Gravel,
            (120, 70, 20) => Class::Dirt,
            _ => Class::Other,
        }
    }
}

/// One class byte a tile from packed RGB: red, green, blue, top down, three
/// bytes a tile, no row padding.
///
/// `None` when `bytes` is not exactly `width * height * 3`. A palette-index
/// plane is that case: the palette lives with the canvas, and guessing an
/// index would write a class file Python would trust instead of scanning the
/// bitmap.
pub fn from_rgb(bytes: &[u8], width: usize, height: usize) -> Option<Vec<u8>> {
    let tiles = width.checked_mul(height)?;
    let need = tiles.checked_mul(3)?;
    if bytes.len() != need {
        return None;
    }
    let mut out = vec![Class::Other as u8; tiles];
    for (tile, class) in bytes.chunks_exact(3).zip(out.iter_mut()) {
        *class = Class::of(tile[0], tile[1], tile[2]) as u8;
    }
    Some(out)
}

/// One class byte a tile from palette indexes. `palette[index]` is red, green,
/// blue, the table the canvas already publishes as its colours. An index past
/// that table is other. The ground plane does not hold the colour itself when
/// it is one byte a tile, and the bitmap writer does not hand back the RGB it
/// expanded, so this is the lookup that already exists.
///
/// `None` when `indexes` is not exactly `width * height` bytes.
pub fn from_indexes(
    indexes: &[u8],
    palette: &[[u8; 3]],
    width: usize,
    height: usize,
) -> Option<Vec<u8>> {
    let tiles = width.checked_mul(height)?;
    if indexes.len() != tiles {
        return None;
    }
    let mut out = vec![Class::Other as u8; tiles];
    for (index, class) in indexes.iter().zip(out.iter_mut()) {
        let colour = match palette.get(*index as usize) {
            Some(colour) => *colour,
            None => [0, 0, 0],
        };
        *class = Class::of(colour[0], colour[1], colour[2]) as u8;
    }
    Some(out)
}

/// The class file, opened so a band of rows can be written on its own.
///
/// The picture is drawn a band at a time and the whole ground is not in
/// memory, so each band is classified and placed at its own rows. `write`
/// is the same file when the whole plane is already one slice.
pub struct Out {
    file: BufWriter<File>,
    width: usize,
    height: usize,
}

/// Create `{name}_ground_class.bin` and reserve one byte a tile.
pub fn begin(path: &Path, width: usize, height: usize) -> Result<Out> {
    let tiles = width.checked_mul(height).ok_or(Error::TooBig)?;
    let wide = u32::try_from(width).map_err(|_| Error::TooBig)?;
    let high = u32::try_from(height).map_err(|_| Error::TooBig)?;
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = File::create(path).map_err(|_| Error::Write)?;
    let mut file = BufWriter::with_capacity(1 << 20, file);
    let mut head = [0u8; HEAD];
    head[..8].copy_from_slice(MAGIC);
    head[8..12].copy_from_slice(&wide.to_le_bytes());
    head[12..16].copy_from_slice(&high.to_le_bytes());
    file.write_all(&head).map_err(|_| Error::Write)?;
    file.flush().map_err(|_| Error::Write)?;
    let bytes = (HEAD as u64)
        .checked_add(tiles as u64)
        .ok_or(Error::TooBig)?;
    file.get_mut()
        .set_len(bytes)
        .map_err(|_| Error::Write)?;
    Ok(Out {
        file,
        width,
        height,
    })
}

impl Out {
    /// Write class bytes for image rows starting at `y0`. Top down, one byte
    /// a tile, `width` of them a row.
    pub fn rows(&mut self, y0: usize, classes: &[u8]) -> Result<()> {
        if self.width == 0 {
            return if classes.is_empty() {
                Ok(())
            } else {
                Err(Error::BadState)
            };
        }
        if classes.len() % self.width != 0 {
            return Err(Error::BadState);
        }
        let n = classes.len() / self.width;
        let end = y0.checked_add(n).ok_or(Error::BadState)?;
        if end > self.height {
            return Err(Error::BadState);
        }
        let at = (HEAD + y0 * self.width) as u64;
        self.file
            .seek(SeekFrom::Start(at))
            .map_err(|_| Error::Write)?;
        self.file.write_all(classes).map_err(|_| Error::Write)?;
        Ok(())
    }

    pub fn finish(&mut self) -> Result<()> {
        self.file.flush().map_err(|_| Error::Write)
    }
}

/// Write one band of class bytes into a file from [`begin`], at image row `y0`.
///
/// A whole plane is the same call with `y0 == 0` and one byte for every tile.
/// The ground is drawn a band at a time, so that is the slice available here.
pub fn write(out: &mut Out, y0: usize, classes: &[u8]) -> Result<()> {
    out.rows(y0, classes)
}
