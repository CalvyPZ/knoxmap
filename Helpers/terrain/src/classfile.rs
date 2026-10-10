//! The ground-class file written beside the ground BMP.
//!
//! One byte a tile, so the building stage can tell pavement from carriageway
//! without walking the bitmap. Little-endian header, then the bytes top down,
//! row by row, the same order as the picture.

use std::fs::File;
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

use crate::canvas;
use crate::err::{Error, Result};

const MAGIC: &[u8; 4] = b"KCLS";
const VERSION: u32 = 1;
const HEAD: usize = 16;

/// 0 other, 1 pavement, 2 carriageway, 3 water, 4 dirt or yard.
const CONCRETE: u8 = 1;
const ASPHALT: u8 = 2;
const WATER: u8 = 3;
const YARD: u8 = 4;

const PALE_CONCRETE: [u8; 3] = [120, 120, 120];
const ASPHALT_RGB: [[u8; 3]; 4] = [
    [150, 150, 150],
    [80, 80, 80],
    [110, 100, 100],
    [130, 120, 120],
];
const WATER_RGB: [u8; 3] = [0, 138, 255];
const YARD_RGB: [[u8; 3]; 5] = [
    [120, 70, 20],
    [110, 80, 60],
    [170, 160, 130],
    [190, 180, 180],
    [165, 160, 140],
];

/// Open `{dir}/{name}.kcls` and write the head. The byte array is reserved at
/// full size so a band can be written at its own row.
pub fn begin(path: &Path, width: usize, height: usize) -> Result<ClassFile> {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = File::create(path).map_err(|_| Error::Write)?;
    let mut file = BufWriter::with_capacity(1 << 20, file);
    let mut head = [0u8; HEAD];
    head[0..4].copy_from_slice(MAGIC);
    head[4..8].copy_from_slice(&VERSION.to_le_bytes());
    head[8..12].copy_from_slice(&(width as u32).to_le_bytes());
    head[12..16].copy_from_slice(&(height as u32).to_le_bytes());
    file.write_all(&head).map_err(|_| Error::Write)?;
    let pixels = width.checked_mul(height).ok_or(Error::TooBig)?;
    file.flush().map_err(|_| Error::Write)?;
    file.get_mut()
        .set_len((HEAD + pixels) as u64)
        .map_err(|_| Error::Write)?;
    Ok(ClassFile {
        file,
        width,
        height,
    })
}

pub struct ClassFile {
    file: BufWriter<File>,
    width: usize,
    height: usize,
}

impl ClassFile {
    /// Write the class bytes of image rows starting at `y0`. `bytes` is
    /// row-major, top down, one byte a tile.
    pub fn append(&mut self, y0: usize, bytes: &[u8]) -> Result<()> {
        if self.width == 0 {
            return if bytes.is_empty() {
                Ok(())
            } else {
                Err(Error::BadState)
            };
        }
        if bytes.len() % self.width != 0 {
            return Err(Error::BadState);
        }
        let n = bytes.len() / self.width;
        match y0.checked_add(n) {
            Some(end) if end <= self.height => {}
            _ => return Err(Error::BadState),
        }
        let at = (HEAD + y0 * self.width) as u64;
        self.file
            .seek(SeekFrom::Start(at))
            .map_err(|_| Error::Write)?;
        self.file.write_all(bytes).map_err(|_| Error::Write)?;
        Ok(())
    }

    pub fn finish(&mut self) -> Result<()> {
        self.file.flush().map_err(|_| Error::Write)
    }
}

/// Class of one ground colour: 0 other, 1 pale concrete, 2 carriageway
/// (medium asphalt, darkest asphalt, both potholes), 3 water, 4 dirt, clay,
/// paving, or a gravel yard.
pub fn class_of(rgb: [u8; 3]) -> u8 {
    if rgb == PALE_CONCRETE {
        CONCRETE
    } else if ASPHALT_RGB.contains(&rgb) {
        ASPHALT
    } else if rgb == WATER_RGB {
        WATER
    } else if YARD_RGB.contains(&rgb) {
        YARD
    } else {
        0
    }
}

/// Three-byte tiles are red, green, blue. One byte is a palette index of the
/// canvas that owns the tile; without that palette it stays other.
pub fn class_byte(pixel: &[u8]) -> u8 {
    if pixel.len() >= 3 {
        class_of([pixel[0], pixel[1], pixel[2]])
    } else {
        0
    }
}

/// Fill `out` with one class byte a tile for `ground`. A water or lake bit
/// forces class 3.
pub fn encode(
    ground: &[u8],
    width: usize,
    bpp: usize,
    water: Option<&[u8]>,
    lake: Option<&[u8]>,
    bit_stride: usize,
    out: &mut Vec<u8>,
) {
    out.clear();
    if width == 0 || bpp == 0 {
        return;
    }
    let stride = width * bpp;
    if ground.len() < stride {
        return;
    }
    let rows = ground.len() / stride;
    out.resize(rows * width, 0);
    let palette = if bpp < 3 {
        canvas::colours_of(ground.as_ptr())
    } else {
        None
    };
    for y in 0..rows {
        let grow = y * stride;
        for x in 0..width {
            let at = grow + x * bpp;
            let mut class = if bpp >= 3 {
                class_byte(&ground[at..at + bpp])
            } else if let Some(palette) = palette.as_ref() {
                let index = ground[at];
                let rgb = palette
                    .colours()
                    .get(index as usize)
                    .copied()
                    .unwrap_or([0, 0, 0]);
                class_of(rgb)
            } else {
                0
            };
            if bit_on(water, bit_stride, y, x) || bit_on(lake, bit_stride, y, x) {
                class = WATER;
            }
            out[y * width + x] = class;
        }
    }
}

fn bit_on(bits: Option<&[u8]>, bit_stride: usize, y: usize, x: usize) -> bool {
    let Some(bits) = bits else {
        return false;
    };
    if bit_stride == 0 {
        return false;
    }
    let at = y * bit_stride;
    if at + bit_stride > bits.len() {
        return false;
    }
    canvas::bit(&bits[at..at + bit_stride], x)
}
