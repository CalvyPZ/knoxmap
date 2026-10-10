//! Writing the bitmaps the game reads.
//!
//! A 24-bit BMP is a 54 byte head and then the rows of the picture, bottom one
//! first, blue byte first, each row padded to a multiple of four bytes. There is
//! no compression and nothing to decide, which is why these files are written
//! here rather than handed back. Ground and vegetation are one byte a tile in
//! memory, a palette index; the file the game reads is still three. The index
//! is turned back into red, green and blue while the row is written, and that
//! row is dropped before the next one. The spawn map is already three bytes a
//! tile, and is written as it stands.
//!
//! The base copies the building stage reads are written in the same pass. Python
//! wrote the file and then copied it; writing it twice while the rows are already
//! in hand is one less read of a file that may be half a gigabyte.

use std::fs::{File, OpenOptions};
use std::io::{BufWriter, Seek, SeekFrom, Write};
use std::path::Path;

use crate::canvas::{self, Palette};
use crate::err::{Error, Result};

const HEAD_BYTES: usize = 54;

/// Write `rows` as a 24-bit BMP, and the same bytes again to `also` if there is
/// one. The rows are the picture top down. Three bytes a tile are red, green,
/// blue, which is the spawn map. One byte a tile is a palette index, which is
/// a ground or vegetation plane; the palette is the one that owns those bytes,
/// and each index is expanded to red, green, blue as the row is written.
pub fn write(
    path: &Path,
    also: Option<&Path>,
    width: usize,
    height: usize,
    rows: &[u8],
) -> Result<()> {
    let indexed = is_indexed(width, height, rows.len());
    let palette = if indexed {
        Some(canvas::colours_of(rows.as_ptr()).ok_or(Error::BadState)?)
    } else {
        None
    };
    let stride = width * 3;
    let padded = (stride + 3) & !3;
    let head = head_bytes(width, height, padded);

    let mut first = open(path)?;
    let mut second = match also {
        Some(path) => Some(open(path)?),
        None => None,
    };
    put(&mut first, &head)?;
    if let Some(handle) = second.as_mut() {
        put(handle, &head)?;
    }
    let pad = [0u8; 4];
    let mut line = vec![0u8; stride];
    let src_stride = if indexed { width } else { stride };
    for y in (0..height).rev() {
        let at = y * src_stride;
        if at + src_stride > rows.len() {
            return Err(Error::BadState);
        }
        paint_row(&rows[at..at + src_stride], palette.as_deref(), &mut line);
        put(&mut first, &line)?;
        if padded > stride {
            put(&mut first, &pad[..padded - stride])?;
        }
        if let Some(handle) = second.as_mut() {
            put(handle, &line)?;
            if padded > stride {
                put(handle, &pad[..padded - stride])?;
            }
        }
    }
    first.flush().map_err(|_| Error::Write)?;
    if let Some(handle) = second.as_mut() {
        handle.flush().map_err(|_| Error::Write)?;
    }
    Ok(())
}

/// One file row, blue byte first. `palette` set means `src` is one index a tile.
fn paint_row(src: &[u8], palette: Option<&Palette>, line: &mut [u8]) {
    if let Some(palette) = palette {
        for (index, out) in src.iter().zip(line.chunks_exact_mut(3)) {
            let rgb = palette
                .colours()
                .get(*index as usize)
                .copied()
                .unwrap_or([0, 0, 0]);
            out[0] = rgb[2];
            out[1] = rgb[1];
            out[2] = rgb[0];
        }
        return;
    }
    for (tile, out) in src.chunks_exact(3).zip(line.chunks_exact_mut(3)) {
        out[0] = tile[2];
        out[1] = tile[1];
        out[2] = tile[0];
    }
}

/// One byte a tile, and not also three. An empty picture is written as RGB,
/// which is no rows either way.
fn is_indexed(width: usize, height: usize, len: usize) -> bool {
    let Some(tiles) = width.checked_mul(height) else {
        return false;
    };
    let Some(rgb_len) = tiles.checked_mul(3) else {
        return false;
    };
    len == tiles && tiles != rgb_len
}

/// Write one band of palette indexes as 24-bit BMP rows, and the matching
/// ground class bytes.
///
/// `y0` is the first row of this band in the finished picture, top down.
/// `y0 == 0` creates the files; a later band appends. The BMP the game reads
/// is bottom up, so each band is placed at its own offset and only one row of
/// RGB is expanded at a time. The class file is top down: 16 bytes (`KCLS`,
/// width, height, then a zero), then one byte a tile in row order.
///
/// Class bytes: 0 other, 1 grass, 2 pale concrete, 3 asphalt (medium, dark,
/// darkest, and both potholes), 4 water, 5 dirt, clay or sand, 6 paving.
pub fn write_band(
    ground_bmp: &Path,
    veg_bmp: &Path,
    class_path: &Path,
    width: usize,
    total_height: usize,
    y0: usize,
    ground_index_rows: &[u8],
    veg_index_rows: &[u8],
    palette: &[[u8; 3]],
) -> Result<()> {
    if width == 0
        || total_height == 0
        || ground_index_rows.len() != veg_index_rows.len()
        || ground_index_rows.len() % width != 0
    {
        return Err(Error::BadState);
    }
    let rows = ground_index_rows.len() / width;
    if y0.saturating_add(rows) > total_height {
        return Err(Error::BadState);
    }
    let stride = width * 3;
    let padded = (stride + 3) & !3;
    let fresh = y0 == 0;

    let mut ground = open_band(ground_bmp, fresh)?;
    let mut veg = open_band(veg_bmp, fresh)?;
    let mut class = open_band(class_path, fresh)?;
    if fresh {
        let head = head_bytes(width, total_height, padded);
        put(&mut ground, &head)?;
        put(&mut veg, &head)?;
        put(&mut class, &class_head(width, total_height))?;
    } else {
        class
            .seek(SeekFrom::End(0))
            .map_err(|_| Error::Write)?;
    }

    let mut class_row = vec![0u8; width];
    for row in 0..rows {
        let indexes = &ground_index_rows[row * width..(row + 1) * width];
        classify(indexes, palette, &mut class_row);
        put(&mut class, &class_row)?;
    }
    class.flush().map_err(|_| Error::Write)?;

    if rows == 0 {
        ground.flush().map_err(|_| Error::Write)?;
        veg.flush().map_err(|_| Error::Write)?;
        return Ok(());
    }
    // The bottom row of the band is the first of this run in the file.
    let bottom = y0 + rows - 1;
    let at = (HEAD_BYTES + (total_height - 1 - bottom) * padded) as u64;
    ground.seek(SeekFrom::Start(at)).map_err(|_| Error::Write)?;
    veg.seek(SeekFrom::Start(at)).map_err(|_| Error::Write)?;
    let mut line = vec![0u8; stride];
    let pad = [0u8; 4];
    for row in (0..rows).rev() {
        expand(
            &ground_index_rows[row * width..(row + 1) * width],
            palette,
            &mut line,
        );
        put(&mut ground, &line)?;
        if padded > stride {
            put(&mut ground, &pad[..padded - stride])?;
        }
        expand(
            &veg_index_rows[row * width..(row + 1) * width],
            palette,
            &mut line,
        );
        put(&mut veg, &line)?;
        if padded > stride {
            put(&mut veg, &pad[..padded - stride])?;
        }
    }
    ground.flush().map_err(|_| Error::Write)?;
    veg.flush().map_err(|_| Error::Write)?;
    Ok(())
}

fn expand(indexes: &[u8], palette: &[[u8; 3]], line: &mut [u8]) {
    for (index, out) in indexes.iter().zip(line.chunks_exact_mut(3)) {
        let colour = palette.get(*index as usize).copied().unwrap_or([0, 0, 0]);
        // BMP keeps a tile as blue, green, red.
        out[0] = colour[2];
        out[1] = colour[1];
        out[2] = colour[0];
    }
}

fn classify(indexes: &[u8], palette: &[[u8; 3]], out: &mut [u8]) {
    for (index, byte) in indexes.iter().zip(out.iter_mut()) {
        let colour = palette.get(*index as usize).copied().unwrap_or([0, 0, 0]);
        *byte = class_of(colour);
    }
}

fn class_of(colour: [u8; 3]) -> u8 {
    const GRASS: [[u8; 3]; 3] = [[90, 100, 35], [117, 117, 47], [145, 135, 60]];
    const PALE: [u8; 3] = [120, 120, 120];
    const ASPHALT: [[u8; 3]; 5] = [
        [150, 150, 150],
        [100, 100, 100],
        [80, 80, 80],
        [110, 100, 100],
        [130, 120, 120],
    ];
    const WATER: [u8; 3] = [0, 138, 255];
    const EARTH: [[u8; 3]; 3] = [[120, 70, 20], [110, 80, 60], [210, 200, 160]];
    const PAVING: [[u8; 3]; 2] = [[170, 160, 130], [190, 180, 180]];
    if GRASS.contains(&colour) {
        1
    } else if colour == PALE {
        2
    } else if ASPHALT.contains(&colour) {
        3
    } else if colour == WATER {
        4
    } else if EARTH.contains(&colour) {
        5
    } else if PAVING.contains(&colour) {
        6
    } else {
        0
    }
}

fn class_head(width: usize, height: usize) -> [u8; 16] {
    let mut head = [0u8; 16];
    head[0..4].copy_from_slice(b"KCLS");
    head[4..8].copy_from_slice(&(width as u32).to_le_bytes());
    head[8..12].copy_from_slice(&(height as u32).to_le_bytes());
    head
}

fn open_band(path: &Path, fresh: bool) -> Result<BufWriter<File>> {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(fresh)
        .open(path)
        .map_err(|_| Error::Write)?;
    Ok(BufWriter::with_capacity(1 << 20, file))
}

fn open(path: &Path) -> Result<BufWriter<File>> {
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let file = File::create(path).map_err(|_| Error::Write)?;
    // A megabyte at a time: a map's ground is a row of forty five thousand bytes
    // and a syscall a row is most of the cost of writing it.
    Ok(BufWriter::with_capacity(1 << 20, file))
}

fn put(handle: &mut BufWriter<File>, bytes: &[u8]) -> Result<()> {
    handle.write_all(bytes).map_err(|_| Error::Write)
}

fn head_bytes(width: usize, height: usize, padded: usize) -> [u8; HEAD_BYTES] {
    let pixels = (padded * height) as u32;
    let mut head = [0u8; HEAD_BYTES];
    head[0] = b'B';
    head[1] = b'M';
    put_u32(&mut head, 2, HEAD_BYTES as u32 + pixels);
    put_u32(&mut head, 10, HEAD_BYTES as u32);
    // The forty byte info head, which is the one every reader expects.
    put_u32(&mut head, 14, 40);
    put_u32(&mut head, 18, width as u32);
    put_u32(&mut head, 22, height as u32);
    put_u16(&mut head, 26, 1);
    put_u16(&mut head, 28, 24);
    put_u32(&mut head, 34, pixels);
    // Ninety six dots an inch, in dots a metre, which is what PIL writes.
    put_u32(&mut head, 38, 3780);
    put_u32(&mut head, 42, 3780);
    head
}

fn put_u16(bytes: &mut [u8], at: usize, value: u16) {
    bytes[at..at + 2].copy_from_slice(&value.to_le_bytes());
}

fn put_u32(bytes: &mut [u8], at: usize, value: u32) {
    bytes[at..at + 4].copy_from_slice(&value.to_le_bytes());
}

/// A 24-bit BMP that rows can be dropped into as each band of the picture finishes.
///
/// The bytes on disk match `write`: a 54 byte head, then the rows bottom first,
/// blue byte first, each row padded to four bytes. `append_band` seeks to where
/// that band sits, so bands may arrive top-down.
pub struct Stream {
    first: BufWriter<File>,
    second: Option<BufWriter<File>>,
    width: usize,
    height: usize,
    padded: usize,
    /// One file row, blue byte first, without the pad.
    line: Vec<u8>,
}

/// Open the BMP (and the base copy, if `also` is set) and write the head.
/// The pixel array is reserved at full size so a later band can land anywhere in it.
pub fn begin(path: &Path, also: Option<&Path>, width: usize, height: usize) -> Result<Stream> {
    let stride = width * 3;
    let padded = (stride + 3) & !3;
    let pixels = padded.checked_mul(height).ok_or(Error::TooBig)?;
    let head = head_bytes(width, height, padded);
    let mut first = open(path)?;
    let mut second = match also {
        Some(path) => Some(open(path)?),
        None => None,
    };
    put(&mut first, &head)?;
    if let Some(handle) = second.as_mut() {
        put(handle, &head)?;
    }
    let bytes = (HEAD_BYTES + pixels) as u64;
    reserve(&mut first, bytes)?;
    if let Some(handle) = second.as_mut() {
        reserve(handle, bytes)?;
    }
    Ok(Stream {
        first,
        second,
        width,
        height,
        padded,
        line: vec![0u8; stride],
    })
}

impl Stream {
    /// Write image rows `y0..y1` (top down in `rows`) into the file `begin` opened.
    ///
    /// The header was written once, for the whole picture, and is not written
    /// again. This seeks to where those rows sit. A BMP stores the bottom row
    /// first, so the bottom of the band is written first and the bytes follow
    /// it; bands may arrive in any order. `rows` is `bpp` bytes a tile.
    /// Three-byte tiles are red, green, blue (the spawn map). One byte is a
    /// palette index from the canvas that owns `rows`, expanded to the 24-bit
    /// row the game reads. The file byte order is blue first either way.
    pub fn append_band(&mut self, y0: usize, y1: usize, rows: &[u8], bpp: usize) -> Result<()> {
        if y0 > y1 || y1 > self.height || bpp == 0 {
            return Err(Error::BadState);
        }
        let n = y1 - y0;
        if n == 0 {
            return Ok(());
        }
        let src_stride = self.width.checked_mul(bpp).ok_or(Error::TooBig)?;
        let need = n.checked_mul(src_stride).ok_or(Error::TooBig)?;
        if rows.len() < need {
            return Err(Error::BadState);
        }
        // One index expands to RGB only for the row being written. The palette
        // lives with the canvas that owns `rows`, so the caller can pass the
        // plane and nothing else.
        let palette = if bpp < 3 {
            Some(canvas::colours_of(rows.as_ptr()).ok_or(Error::BadState)?)
        } else {
            None
        };
        // Image row y (0 at the top) is file row (height - 1 - y). The bottom
        // of this band is the first of those, and the rest follow it.
        let at = HEAD_BYTES + (self.height - y1) * self.padded;
        self.seek_both(at as u64)?;
        let padded = self.padded;
        let pad = [0u8; 4];
        for y in (y0..y1).rev() {
            let src = (y - y0) * src_stride;
            let src_row = &rows[src..src + src_stride];
            for (tile, out) in src_row.chunks_exact(bpp).zip(self.line.chunks_exact_mut(3)) {
                let bgr = bgr_of(tile, palette.as_deref());
                out[0] = bgr[0];
                out[1] = bgr[1];
                out[2] = bgr[2];
            }
            put(&mut self.first, &self.line)?;
            if padded > self.line.len() {
                put(&mut self.first, &pad[..padded - self.line.len()])?;
            }
            if let Some(handle) = self.second.as_mut() {
                put(handle, &self.line)?;
                if padded > self.line.len() {
                    put(handle, &pad[..padded - self.line.len()])?;
                }
            }
        }
        Ok(())
    }

    pub fn finish(&mut self) -> Result<()> {
        self.first.flush().map_err(|_| Error::Write)?;
        if let Some(handle) = self.second.as_mut() {
            handle.flush().map_err(|_| Error::Write)?;
        }
        Ok(())
    }

    fn seek_both(&mut self, at: u64) -> Result<()> {
        self.first
            .seek(SeekFrom::Start(at))
            .map_err(|_| Error::Write)?;
        if let Some(handle) = self.second.as_mut() {
            handle.seek(SeekFrom::Start(at)).map_err(|_| Error::Write)?;
        }
        Ok(())
    }
}

fn reserve(handle: &mut BufWriter<File>, bytes: u64) -> Result<()> {
    handle.flush().map_err(|_| Error::Write)?;
    handle.get_mut().set_len(bytes).map_err(|_| Error::Write)
}

/// Three stored bytes are red, green, blue. One byte is a palette index, expanded
/// here so the file stays 24-bit without a second copy of the picture.
fn bgr_of(tile: &[u8], palette: Option<&crate::canvas::Palette>) -> [u8; 3] {
    if tile.len() >= 3 {
        [tile[2], tile[1], tile[0]]
    } else if let (Some(index), Some(palette)) = (tile.first().copied(), palette) {
        let rgb = palette
            .colours()
            .get(index as usize)
            .copied()
            .unwrap_or([0, 0, 0]);
        [rgb[2], rgb[1], rgb[0]]
    } else {
        [0, 0, 0]
    }
}
