//! Drawing one piece of ground with every core on the machine.
//!
//! The program is ordered and the order matters, so it is not shared out shape
//! by shape. Instead the piece is cut into bands of rows, every thread walks the
//! whole program, and each thread draws only the rows it owns. Order is
//! preserved exactly - a thread sees the same commands in the same sequence as
//! Python did - and no two threads ever write the same tile, so there is no lock
//! anywhere in the drawing and no copying of pictures between threads.
//!
//! A procedural pass is a wall across the program: everything before it has to
//! be finished before it runs, because it asks questions about ground another
//! thread may still be painting. So the program is cut into stages at those
//! passes, the threads meet at the end of each stage, and the next one starts.
//! Within a stage there is nothing to wait for.
//!
//! Bands are handed out from a counter rather than dealt out in advance, so a
//! thread that draws a band of empty countryside in no time moves straight on to
//! the next one instead of idling while another thread works through a city.
//!
//! The picture is not held whole. A mod is thousands of rows, and the ground,
//! vegetation, mask and water of all of them is hundreds of megabytes. The piece
//! is walked in bands of four cells. Each band, plus the rows a neighbour pass
//! can see, is drawn and then written out before the next band's canvas exists.
//! The biome plane stays for the whole piece: Python still cuts the cell PNGs
//! from it.

use std::sync::atomic::{AtomicU32, AtomicUsize, Ordering};
use std::sync::Barrier;

use crate::blob::{Blob, Pass, PassKind, Piece, Shape};
use crate::canvas::{self, Canvas};
use crate::err::{Error, Result};
use crate::paint::{self, Scratch};
use crate::passes;
use crate::pool::Beat;
use crate::raster::{Band, Paint};
use crate::spawn::{self, Spawn};

/// The game's cell: three hundred tiles square, which is what the mapping guide
/// says and what the game reads.
pub const CELL_SIZE: usize = 300;

/// How many cells of rows are drawn and written before the canvas moves on.
/// The halo around them is extra, and is not a substitute for this.
const FLUSH_CELLS: usize = 4;

/// Rows at a time. Small enough that sixty four threads all have work on a
/// modest map, large enough that the per-band setup is nothing beside the
/// drawing.
const MIN_BAND: usize = 24;
/// Bands a thread, so that an uneven map still shares out evenly.
const BANDS_PER_THREAD: usize = 4;

/// What one stage of the program is.
enum Stage {
    /// Commands `from` up to `to`, drawn band by band.
    Draw { from: usize, to: usize },
    /// A procedural pass over the piece.
    Walk { pass: Pass },
    /// The spawn map, which reads the finished ground and writes a picture of
    /// its own, so its bands are rows of that picture rather than of the piece.
    Spawn,
}

/// One finished piece of ground: the files are written, and the biome plane is
/// kept because Python's cell PNGs are the one output still written there.
pub struct Finished {
    pub index: usize,
    pub width: u32,
    pub height: u32,
    pub biome: Box<[u8]>,
}

impl Finished {
    pub fn bytes(&self) -> usize {
        self.biome.len()
    }
}

/// Draw one piece and write its files.
///
/// The canvas is one band of the piece plus the rows a neighbour pass can read.
/// When the band is finished its ground, vegetation, class bytes and the spawn
/// rows that sit on it are written, and the canvas is dropped. The biome is
/// copied into a full-size plane that stays alive until Python releases it.
pub fn render(blob: &Blob, index: usize, threads: usize, beat: &Beat) -> Result<Finished> {
    let piece = blob.piece(index)?;
    let stages = split(blob, &piece)?;
    let halo = neighbour_halo(blob, &piece)?;
    let width = piece.width as usize;
    let height = piece.height as usize;
    let want_mask = piece.wants_mask(blob);
    let want_water = piece.wants_water(blob);
    let tiles = width.checked_mul(height).ok_or(Error::TooBig)?;
    let mut biome = vec![piece.open_biome; tiles].into_boxed_slice();
    let out = Spawn::new(&piece);
    let threads = threads.max(1);

    let dir = std::path::PathBuf::from(blob.string(piece.dir as usize));
    let name = blob.string(piece.name as usize);
    let mut ground_bmp = crate::bmp::begin(
        &dir.join(format!("{name}.bmp")),
        Some(&dir.join(format!("{name}_ground_base.bmp"))),
        width,
        height,
    )?;
    let mut veg_bmp = crate::bmp::begin(
        &dir.join(format!("{name}_veg.bmp")),
        Some(&dir.join(format!("{name}_veg_base.bmp"))),
        width,
        height,
    )?;
    // The ground is one band at a time, so the class file is filled from that
    // band. This is the file the building stage reads.
    let mut ground_class = crate::classify::begin(
        &dir.join(format!("{name}_ground_class.bin")),
        width,
        height,
    )?;
    let mut class_file = crate::classfile::begin(&dir.join(format!("{name}.kcls")), width, height)?;
    let mut class_bytes: Vec<u8> = Vec::new();

    for slice in slices(&piece, halo) {
        if beat.stopped() {
            return Err(Error::Cancelled);
        }
        let canvas = Canvas::window(
            &piece,
            slice.win0,
            slice.win1 - slice.win0,
            want_mask,
            want_water,
        );
        paint_slice(blob, &piece, &canvas, &out, &stages, &slice, threads, beat)?;
        canvas.prepare(blob);
        copy_biome(&canvas, &mut biome, slice.core0, slice.core1, width)?;
        let ground = canvas.rows(&canvas.ground, slice.core0, slice.core1);
        let palette = canvas.colours();
        crate::band::note_ground_rows(
            &mut ground_class,
            slice.core0,
            width,
            ground,
            canvas.ground.bpp(),
            palette.colours(),
        )?;
        let (water, lake) = if canvas.has_water() {
            (
                Some(canvas.water_rows(slice.core0, slice.core1)),
                Some(canvas.lake_rows(slice.core0, slice.core1)),
            )
        } else {
            (None, None)
        };
        crate::classfile::encode(
            ground,
            width,
            canvas.ground.bpp(),
            water,
            lake,
            canvas.bit_stride,
            &mut class_bytes,
        );
        class_file.append(slice.core0, &class_bytes)?;
        ground_bmp.append_band(slice.core0, slice.core1, ground, canvas.ground.bpp())?;
        let veg = canvas.rows(&canvas.veg, slice.core0, slice.core1);
        veg_bmp.append_band(slice.core0, slice.core1, veg, canvas.veg.bpp())?;
    }

    class_file.finish()?;
    write_files(
        &mut ground_bmp,
        &mut veg_bmp,
        &mut ground_class,
        &dir.join(format!("{name}_ZombieSpawnMap.bmp")),
        &out,
    )?;

    Ok(Finished {
        index,
        width: piece.width,
        height: piece.height,
        biome,
    })
}

/// How many bands of work this piece is, so that the progress Python shows moves
/// with the work rather than jumping when a piece finishes. Counted before any
/// thread starts, so the total never moves afterwards.
pub fn work_units(blob: &Blob, piece: &Piece, threads: usize) -> u64 {
    let stages = match split(blob, piece) {
        Ok(stages) => stages,
        Err(_) => return 1,
    };
    let halo = match neighbour_halo(blob, piece) {
        Ok(halo) => halo,
        Err(_) => return 1,
    };
    let threads = threads.max(1);
    let mut total = 0u64;
    for slice in slices(piece, halo) {
        for stage in &stages {
            total += stage_bands(stage, &slice, threads);
        }
    }
    total
}

/// Bytes this piece holds while it is being drawn: one window of the pictures,
/// the full biome plane, and the spawn picture. The pool reserves this instead
/// of a full-piece canvas.
pub(crate) fn working_bytes(blob: &Blob, piece: &Piece) -> Result<u64> {
    let width = piece.width as usize;
    let height = piece.height as usize;
    let halo = neighbour_halo(blob, piece)?;
    let scale = piece.spawn_scale.max(1) as usize;
    let band = flush_len(height, scale);
    // One passed row past the flushed band, then the read halo beyond that.
    let extra = halo.saturating_add(1);
    let window = band.saturating_add(extra.saturating_mul(2)).min(height);
    // Ground, vegetation, biome, the woodland mask, and one byte for the two
    // water bit planes. The mask and the water are not on every piece; counting
    // them anyway keeps the reserve above the allocation, and the extra byte is
    // more than the two bit planes actually take.
    let per_tile = (canvas::GROUND_BPP
        + canvas::VEG_BPP
        + canvas::BIOME_BPP
        + canvas::MASK_BPP
        + 1) as u64;
    let pictures = (window as u64)
        .saturating_mul(width as u64)
        .saturating_mul(per_tile);
    // Class bytes of the flushed band, held while that band's ground is still
    // in hand.
    let class_scratch = (band as u64).saturating_mul(width as u64);
    let biome = (height as u64)
        .saturating_mul(width as u64)
        .saturating_mul(canvas::BIOME_BPP as u64);
    let spawn_w = (width / scale).max(1) as u64;
    let spawn_h = (height / scale).max(1) as u64;
    // spawn.rs stores three bytes a pixel of its own picture.
    let spawn = spawn_w.saturating_mul(spawn_h).saturating_mul(3);
    Ok(pictures
        .saturating_add(biome)
        .saturating_add(spawn)
        .saturating_add(class_scratch))
}

/// The files of one piece. Ground and vegetation were written band by band,
/// because those pictures are not kept; this finishes them, finishes the class
/// files the same way, and writes the spawn map, which is small enough to hold.
fn write_files(
    ground: &mut crate::bmp::Stream,
    veg: &mut crate::bmp::Stream,
    ground_class: &mut crate::classify::Out,
    spawn_path: &std::path::Path,
    out: &Spawn,
) -> Result<()> {
    ground.finish()?;
    veg.finish()?;
    ground_class.finish()?;
    crate::bmp::write(spawn_path, None, out.width, out.height, out.rows())
}

/// Rows committed at a time. Four cells, snapped down to a multiple of the
/// spawn scale so one spawn pixel's block of ground never straddles two bands.
fn flush_len(height: usize, scale: usize) -> usize {
    if height == 0 {
        return 0;
    }
    let scale = scale.max(1);
    let want = CELL_SIZE * FLUSH_CELLS;
    let want = if scale <= 1 {
        want
    } else {
        (want / scale).max(1) * scale
    };
    want.min(height)
}

/// How far past the rows it writes a neighbour pass reads.
///
/// Woodland's edge, the shore reach, the near-pass reach, and the one row kerbs
/// look up and down. A blot is not included: its centre may sit outside the
/// band, but the pass recomputes the blot and only writes tiles inside the band,
/// so it never reads an outside row.
fn neighbour_halo(blob: &Blob, piece: &Piece) -> Result<usize> {
    let mut halo = 1usize;
    let from = piece.cmd_at as usize;
    let to = from + piece.cmd_n as usize;
    for at in from..to {
        let cmd = blob.cmd(at)?;
        if cmd.shape != Shape::Pass {
            continue;
        }
        let pass = blob.pass(cmd.ring_at as usize)?;
        let reach = match pass.kind {
            PassKind::Woodland | PassKind::Shores => pass.spacing.max(1) as usize,
            PassKind::Kerbs => 1,
            PassKind::Near => ((pass.extra >> 8) & 0xFF) as usize,
            _ => 0,
        };
        if reach > halo {
            halo = reach;
        }
    }
    Ok(halo)
}

/// One flush of the piece. `core` is written out. Passes also run on the row
/// just outside it, so a kerb reads ground those passes have already changed.
/// The window is that, plus the neighbour reach, which is what the passes read.
struct Slice {
    win0: usize,
    win1: usize,
    pass0: usize,
    pass1: usize,
    core0: usize,
    core1: usize,
    spawn0: usize,
    spawn1: usize,
}

fn slices(piece: &Piece, halo: usize) -> Vec<Slice> {
    let height = piece.height as usize;
    let scale = piece.spawn_scale.max(1) as usize;
    let band = flush_len(height, scale);
    let mut out = Vec::new();
    if band == 0 {
        return out;
    }
    let mut y = 0;
    while y < height {
        let y1 = (y + band).min(height);
        let pass0 = y.saturating_sub(1);
        let pass1 = (y1 + 1).min(height);
        let (spawn0, spawn1) = spawn_span(y, y1, scale, height);
        out.push(Slice {
            win0: pass0.saturating_sub(halo),
            win1: (pass1 + halo).min(height),
            pass0,
            pass1,
            core0: y,
            core1: y1,
            spawn0,
            spawn1,
        });
        y = y1;
    }
    out
}

/// Spawn-picture rows whose ground block lies entirely inside `y0..y1`.
fn spawn_span(y0: usize, y1: usize, scale: usize, height: usize) -> (usize, usize) {
    let scale = scale.max(1);
    let spawn_h = (height / scale).max(1);
    if y0 >= y1 || height == 0 {
        return (0, 0);
    }
    // Shorter than one block: the single spawn row is filled with the band
    // that reaches the bottom of the piece.
    if height < scale {
        return if y1 >= height { (0, spawn_h) } else { (0, 0) };
    }
    let sy0 = ((y0 + scale - 1) / scale).min(spawn_h);
    let sy1 = if y1 >= height { spawn_h } else { y1 / scale }.min(spawn_h);
    if sy1 <= sy0 {
        (0, 0)
    } else {
        (sy0, sy1)
    }
}

fn stage_range(stage: &Stage, slice: &Slice) -> (usize, usize) {
    match stage {
        Stage::Draw { .. } => (slice.win0, slice.win1),
        Stage::Walk { .. } => (slice.pass0, slice.pass1),
        Stage::Spawn => (slice.spawn0, slice.spawn1),
    }
}

fn stage_bands(stage: &Stage, slice: &Slice, threads: usize) -> u64 {
    let (from, to) = stage_range(stage, slice);
    let rows = to.saturating_sub(from);
    if rows == 0 {
        return 0;
    }
    let each = band_rows(rows, threads.max(1));
    ((rows + each - 1) / each) as u64
}

fn paint_slice(
    blob: &Blob,
    piece: &Piece,
    canvas: &Canvas,
    out: &Spawn,
    stages: &[Stage],
    slice: &Slice,
    threads: usize,
    beat: &Beat,
) -> Result<()> {
    let threads = threads.max(1);
    let fault = AtomicU32::new(0);
    let plan: Vec<(usize, usize, usize)> = stages
        .iter()
        .map(|stage| {
            let (from, to) = stage_range(stage, slice);
            let rows = to.saturating_sub(from);
            if rows == 0 {
                (from, 1usize, 0usize)
            } else {
                let each = band_rows(rows, threads);
                let n = (rows + each - 1) / each;
                (from, each, n)
            }
        })
        .collect();
    let next: Vec<AtomicUsize> = plan.iter().map(|_| AtomicUsize::new(0)).collect();
    let barrier = Barrier::new(threads);
    let work = |scratch: &mut Scratch| {
        for (which, stage) in stages.iter().enumerate() {
            let (from, each, total) = plan[which];
            let end = stage_range(stage, slice).1;
            loop {
                let band = next[which].fetch_add(1, Ordering::Relaxed);
                if band >= total {
                    break;
                }
                if beat.stopped() || fault.load(Ordering::Relaxed) != 0 {
                    continue;
                }
                let y0 = from + band * each;
                let y1 = (y0 + each).min(end);
                let caught = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    one_band(blob, piece, canvas, out, stage, y0, y1, scratch)
                }));
                match caught {
                    Ok(Ok(())) => {}
                    Ok(Err(wrong)) => {
                        let _ = fault.compare_exchange(
                            0,
                            wrong.code(),
                            Ordering::Relaxed,
                            Ordering::Relaxed,
                        );
                    }
                    Err(_) => {
                        let _ = fault.compare_exchange(
                            0,
                            Error::Panic.code(),
                            Ordering::Relaxed,
                            Ordering::Relaxed,
                        );
                    }
                }
                beat.step();
            }
            // Every thread meets here, whatever happened to it, which is why the
            // panic above is caught around one band rather than around the lot:
            // a thread that stopped walking would leave the others at the wall
            // for ever.
            barrier.wait();
        }
    };

    if threads == 1 {
        let mut scratch = Scratch::new();
        work(&mut scratch);
    } else {
        std::thread::scope(|scope| {
            for _ in 0..threads {
                scope.spawn(|| {
                    let mut scratch = Scratch::new();
                    work(&mut scratch);
                });
            }
        });
    }

    if beat.stopped() {
        return Err(Error::Cancelled);
    }
    let fault = fault.load(Ordering::Relaxed);
    if fault != 0 {
        return Err(code_of(fault));
    }
    Ok(())
}

fn copy_biome(canvas: &Canvas, biome: &mut [u8], y0: usize, y1: usize, width: usize) -> Result<()> {
    let src = canvas.rows(&canvas.biome, y0, y1);
    let from = y0.checked_mul(width).ok_or(Error::TooBig)?;
    let to = y1.checked_mul(width).ok_or(Error::TooBig)?;
    if to > biome.len() || src.len() != to - from {
        return Err(Error::BadState);
    }
    biome[from..to].copy_from_slice(src);
    Ok(())
}

fn code_of(code: u32) -> Error {
    match code {
        1 => Error::Truncated,
        2 => Error::BadMagic,
        3 => Error::BadAbi,
        4 => Error::BadCounts,
        5 => Error::BadProgram,
        6 => Error::BadState,
        8 => Error::TooBig,
        9 => Error::Write,
        10 => Error::Cancelled,
        _ => Error::Panic,
    }
}

fn band_rows(rows: usize, threads: usize) -> usize {
    let want = threads * BANDS_PER_THREAD;
    let rows = rows.max(1);
    ((rows + want - 1) / want).max(MIN_BAND).min(rows)
}

/// Cut the program into stages at every procedural pass.
fn split(blob: &Blob, piece: &Piece) -> Result<Vec<Stage>> {
    let from = piece.cmd_at as usize;
    let to = from + piece.cmd_n as usize;
    let mut stages = Vec::new();
    let mut run = from;
    for at in from..to {
        let cmd = blob.cmd(at)?;
        if cmd.shape != Shape::Pass {
            continue;
        }
        if at > run {
            stages.push(Stage::Draw { from: run, to: at });
        }
        let pass = blob.pass(cmd.ring_at as usize)?;
        stages.push(match pass.kind {
            PassKind::Spawn => Stage::Spawn,
            _ => Stage::Walk { pass },
        });
        run = at + 1;
    }
    if to > run {
        stages.push(Stage::Draw { from: run, to });
    }
    // The cells a diagonal cut left out are painted back to empty countryside
    // last of all, so that nothing drawn into them survives.
    if piece.keep_n > 0 {
        stages.push(Stage::Walk {
            pass: Pass {
                kind: PassKind::Soften,
                over: crate::blob::ANY_SET,
                avoid: crate::blob::ANY_SET,
                radius: 0,
                chance: 0.0,
                salt: 0,
                spacing: 0,
                pick_at: 0,
                pick_n: 0,
                flags: KEEP_STAGE,
                extra: 0,
            },
        });
    }
    Ok(stages)
}

/// The flag that marks the stage that blanks the cells outside the selection.
/// It rides on a pass record because a stage is a pass or a run of commands, and
/// this is neither: it is the one thing in the program that is about the shape of
/// the map rather than about what is on it.
const KEEP_STAGE: u16 = 1 << 15;

/// One band of one stage. `y0..y1` are absolute rows of the piece for a draw or
/// a pass, and rows of the spawn picture for the spawn stage.
fn one_band(
    blob: &Blob,
    piece: &Piece,
    canvas: &Canvas,
    out: &Spawn,
    stage: &Stage,
    y0: usize,
    y1: usize,
    scratch: &mut Scratch,
) -> Result<()> {
    if y0 >= y1 {
        return Ok(());
    }
    match stage {
        Stage::Draw { from, to } => {
            // Safety: this band of these planes belongs to this thread alone for
            // the length of the stage, and no thread reads a plane in a drawing
            // stage.
            let mut rows = unsafe { draw_band(canvas, y0, y1) };
            paint::draw(blob, *from, *to, &mut rows, scratch)
        }
        Stage::Walk { pass } => {
            // Kerbs read the ground of the rows next door, which belong to
            // another band, so they must not hold a write borrow of the ground.
            // They write the vegetation only.
            if pass.kind == PassKind::Kerbs {
                let mut rows = unsafe { veg_band(canvas, y0, y1) };
                return passes::run(blob, piece, canvas, pass, &mut rows);
            }
            // Safety: as above, and the planes a pass reads - the woodland mask,
            // the water masks - are not in the band, so nothing is read and
            // written at once.
            let mut rows = unsafe { pass_band(canvas, y0, y1) };
            if pass.flags & KEEP_STAGE != 0 {
                blank(blob, piece, &mut rows);
                return Ok(());
            }
            passes::run(blob, piece, canvas, pass, &mut rows)
        }
        Stage::Spawn => {
            // Safety: these rows of the spawn picture belong to this thread, and
            // the ground it reads is finished.
            unsafe { spawn::fill(blob, piece, canvas, out, y0, y1) };
            Ok(())
        }
    }
}

/// A band with everything a drawing stage may write.
///
/// # Safety
/// One thread a band.
unsafe fn draw_band<'a>(canvas: &'a Canvas, y0: usize, y1: usize) -> Band<'a> {
    Band {
        y0,
        y1,
        width: canvas.width,
        ground: Some(canvas.rows_mut(&canvas.ground, y0, y1)),
        veg: Some(canvas.rows_mut(&canvas.veg, y0, y1)),
        biome: Some(canvas.rows_mut(&canvas.biome, y0, y1)),
        mask: if canvas.mask.live() {
            Some(canvas.rows_mut(&canvas.mask, y0, y1))
        } else {
            None
        },
        water: if canvas.has_water() {
            Some(canvas.water_rows_mut(y0, y1))
        } else {
            None
        },
        lake: if canvas.has_water() {
            Some(canvas.lake_rows_mut(y0, y1))
        } else {
            None
        },
        bit_stride: canvas.bit_stride,
    }
}

/// A band that may write the vegetation and nothing else. Kerbs use this so the
/// ground stays free to be read, including the row that belongs to the band
/// above.
///
/// # Safety
/// One thread a band, and no other thread is writing the vegetation.
unsafe fn veg_band<'a>(canvas: &'a Canvas, y0: usize, y1: usize) -> Band<'a> {
    Band {
        y0,
        y1,
        width: canvas.width,
        ground: None,
        veg: Some(canvas.rows_mut(&canvas.veg, y0, y1)),
        biome: None,
        mask: None,
        water: None,
        lake: None,
        bit_stride: canvas.bit_stride,
    }
}

/// A band with the pictures only. The masks stay out of it because a pass reads
/// them, including rows that belong to other threads.
///
/// # Safety
/// One thread a band.
unsafe fn pass_band<'a>(canvas: &'a Canvas, y0: usize, y1: usize) -> Band<'a> {
    Band {
        y0,
        y1,
        width: canvas.width,
        ground: Some(canvas.rows_mut(&canvas.ground, y0, y1)),
        veg: Some(canvas.rows_mut(&canvas.veg, y0, y1)),
        biome: Some(canvas.rows_mut(&canvas.biome, y0, y1)),
        mask: None,
        water: None,
        lake: None,
        bit_stride: canvas.bit_stride,
    }
}

/// Paint the cells the selection did not keep back to empty countryside. A
/// diagonal cut leaves those cells out of the map: the game fills them in, and
/// building generation never sees them.
fn blank(blob: &Blob, piece: &Piece, band: &mut Band) {
    let keep_at = piece.keep_at as usize;
    let keep_n = piece.keep_n as usize;
    let paint = Paint {
        ground: Some(canvas::rgb(piece.grass)),
        veg: Some(canvas::rgb(piece.nothing)),
        biome: Some(piece.open_biome),
        mask: None,
        water: false,
        lake: false,
    };
    let cells_x = (band.width + CELL_SIZE - 1) / CELL_SIZE;
    let cells_y = (piece.height as usize + CELL_SIZE - 1) / CELL_SIZE;
    if keep_n >= cells_x * cells_y {
        return;
    }
    // The kept cells as a grid rather than a list: a map is a few thousand cells
    // at most, and asking the list on every tile would cost more than the
    // drawing did.
    let mut kept = vec![false; cells_x * cells_y];
    for at in keep_at..keep_at + keep_n {
        let (cell_x, cell_y) = blob.cell(at);
        if cell_x < 0 || cell_y < 0 {
            continue;
        }
        let (cell_x, cell_y) = (cell_x as usize, cell_y as usize);
        if cell_x < cells_x && cell_y < cells_y {
            kept[cell_y * cells_x + cell_x] = true;
        }
    }
    for y in band.y0..band.y1 {
        let cell_y = y / CELL_SIZE;
        let mut x = 0;
        while x < band.width {
            let cell_x = x / CELL_SIZE;
            let right = ((cell_x + 1) * CELL_SIZE).min(band.width);
            if !kept[cell_y * cells_x + cell_x] {
                band.hline(y as i32, x as i32, right as i32 - 1, &paint);
            }
            x = right;
        }
    }
}

