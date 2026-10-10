//! The pictures of one piece of ground, in one allocation.
//!
//! A piece has the ground colours the game reads as floor tiles, the vegetation
//! colours over the top, the biome index Build 42 reads for foraging, two bit
//! masks saying where the water and the still water are, and - only if the piece
//! has woods in it - a scratch mask the woodland pass reads to tell a tile deep
//! inside a forest from one at its edge.
//!
//! They are all the same size in tiles, so they are one buffer cut into planes,
//! and that buffer is the memory pool. Ground and vegetation are one byte a
//! tile, a palette index. With the biome that is three bytes, plus a quarter
//! for the two water masks, and one byte more of woodland mask when the piece
//! has woods. The twenty-four-bit BMP the game reads is expanded from that
//! index while a row is being written, and dropped before the next row. PIL
//! held an RGB image at four bytes a pixel, three images a piece, and a piece
//! a thread.
//!
//! How threads share it. A pass hands each thread a band of rows that no other
//! thread touches, so threads write into one picture with no lock and nothing
//! copied back. That is why the planes sit behind an `UnsafeCell` and why
//! `rows_mut` is unsafe: its contract is that a band of rows goes to exactly one
//! thread, and that no thread reads a plane another thread is writing. The bit
//! masks are padded to a whole number of bytes a row for the same reason - two
//! threads setting bits in one byte would lose each other's writes.
//!
//! Passes that do need to read their neighbours - the bank of a river is a
//! question about the tile next door, which may be in another thread's band -
//! read a plane nobody is writing: the water masks are finished before the
//! shore pass starts, and the pass writes the biome, not the masks.
//!
//! `origin_y` is the piece row that sits at canvas row 0. A canvas of the whole
//! piece leaves it at 0, so piece row and canvas row are the same number. A
//! canvas that holds only a band of the piece sets it to where that band starts,
//! and every read of the buffer uses canvas row = piece row - `origin_y`.

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, RwLock};

use crate::blob::{Blob, Piece, ANY_SET, NO_COLOUR};

pub const GROUND_BPP: usize = 1;
pub const VEG_BPP: usize = 1;
pub const BIOME_BPP: usize = 1;
pub const MASK_BPP: usize = 1;

/// One picture: where it is in the buffer, and how to walk it.
#[derive(Clone, Copy)]
pub struct Plane {
    width: usize,
    height: usize,
    bpp: usize,
    at: usize,
    /// Zero for an absent plane, which is how a piece with no woods pays nothing
    /// for the woodland mask.
    live: bool,
}

impl Plane {
    #[inline]
    pub fn stride(&self) -> usize {
        self.width * self.bpp
    }

    /// Bytes stored for one tile. Ground and vegetation are palette indexes
    /// when this is 1; callers use it instead of assuming three colour bytes.
    #[inline]
    pub fn bpp(&self) -> usize {
        self.bpp
    }

    #[inline]
    pub fn live(&self) -> bool {
        self.live
    }
}

/// The distinct colours one piece paints, and which palette indexes belong to
/// each colour set the passes named.
///
/// Grass is always index 0, because that is what the ground plane is cleared
/// to before any command runs. Nothing-growing is the next index, or 0 when it
/// is the same colour as the grass. Every other colour the program or the
/// colour sets name follows, so a pass asks "is this asphalt" by testing one
/// byte against the asphalt set's indexes.
pub struct Palette {
    rgb: Vec<[u8; 3]>,
    /// Packed colour, then its index, sorted by the colour so a paint can find
    /// its byte without walking the whole list.
    order: Vec<(u32, u8)>,
    /// Bitset of indexes per colour-set id. Empty sets stay empty.
    sets: Vec<[u64; 4]>,
    nothing: u8,
    grass_packed: u32,
    nothing_packed: u32,
    /// Set once the piece's program has been folded in. Before that, only grass
    /// and nothing-growing have indexes, which is all the cleared planes need.
    ready: bool,
}

impl Palette {
    pub fn colours(&self) -> &[[u8; 3]] {
        &self.rgb
    }

    #[inline]
    pub fn nothing(&self) -> u8 {
        self.nothing
    }

    #[inline]
    pub fn index_rgb(&self, colour: [u8; 3]) -> u8 {
        self.index(pack(colour))
    }

    #[inline]
    pub fn index(&self, colour: u32) -> u8 {
        let colour = colour & 0x00FF_FFFF;
        if colour == self.grass_packed {
            return 0;
        }
        if colour == self.nothing_packed {
            return self.nothing;
        }
        match self.order.binary_search_by_key(&colour, |pair| pair.0) {
            Ok(at) => self.order[at].1,
            // A full palette keeps the last index. The real set is a few dozen,
            // so this is the overflow, not a colour the piece meant to paint.
            Err(_) if self.rgb.len() >= 256 => 255,
            Err(_) => 0,
        }
    }

    /// Whether palette index `index` is in colour set `set`. `ANY_SET` matches
    /// everything, which is what an unnamed set means to a pass.
    #[inline]
    pub fn in_set(&self, set: u8, index: u8) -> bool {
        if set == ANY_SET {
            return true;
        }
        match self.sets.get(set as usize) {
            Some(bits) => bits[(index >> 6) as usize] & (1u64 << (index & 63)) != 0,
            None => false,
        }
    }

    fn seed(grass: u32, nothing: u32) -> Palette {
        let grass = grass & 0x00FF_FFFF;
        let nothing = nothing & 0x00FF_FFFF;
        let nothing_index = if nothing == grass { 0 } else { 1 };
        let mut list = vec![rgb(grass)];
        if nothing_index == 1 {
            list.push(rgb(nothing));
        }
        Palette {
            order: order_of(&list),
            rgb: list,
            sets: Vec::new(),
            nothing: nothing_index,
            grass_packed: grass,
            nothing_packed: nothing,
            ready: false,
        }
    }

    fn collect(blob: &Blob, base: &Palette) -> Palette {
        let grass = base.grass_packed;
        let nothing = base.nothing_packed;
        let nothing_index = base.nothing;
        let mut list = vec![rgb(grass)];
        if nothing_index == 1 {
            list.push(rgb(nothing));
        }
        let mut extras: Vec<[u8; 3]> = Vec::new();
        let mut push = |colour: u32| {
            if colour == NO_COLOUR || list.len() + extras.len() >= 256 {
                return;
            }
            let colour = colour & 0x00FF_FFFF;
            if colour == grass || colour == nothing {
                return;
            }
            let bytes = crate::canvas::rgb(colour);
            if !extras.contains(&bytes) {
                extras.push(bytes);
            }
        };
        for at in 0..blob.n_cmds() {
            let Ok(cmd) = blob.cmd(at) else {
                continue;
            };
            push(cmd.ground);
            push(cmd.veg);
        }
        for at in 0..blob.n_picks() {
            let pick = blob.pick(at);
            push(pick.ground);
            push(pick.veg);
        }
        extras.sort_unstable();
        list.extend(extras);
        let mut sets: Vec<[u64; 4]> = Vec::new();
        for set in 0..255u16 {
            let mut bits = [0u64; 4];
            let mut any = false;
            for (index, colour) in list.iter().enumerate() {
                if blob.in_set(set as u8, pack(*colour)) {
                    bits[(index >> 6) as usize] |= 1u64 << (index & 63);
                    any = true;
                }
            }
            if any {
                if sets.len() <= set as usize {
                    sets.resize(set as usize + 1, [0u64; 4]);
                }
                sets[set as usize] = bits;
            }
        }
        Palette {
            order: order_of(&list),
            rgb: list,
            sets,
            nothing: nothing_index,
            grass_packed: grass,
            nothing_packed: nothing,
            ready: true,
        }
    }
}

fn order_of(rgb: &[[u8; 3]]) -> Vec<(u32, u8)> {
    let mut order: Vec<(u32, u8)> = rgb
        .iter()
        .enumerate()
        .map(|(index, colour)| (pack(*colour), index as u8))
        .collect();
    order.sort_unstable_by_key(|pair| pair.0);
    order
}

/// Shared with the threads that only hold a slice of the buffer: the palette
/// and the piece row of canvas row 0. The box keeps this address still when
/// the canvas itself moves.
struct Share {
    start: usize,
    end: usize,
    origin: AtomicUsize,
    pal: RwLock<Arc<Palette>>,
}

impl Share {
    fn prepare(&self, blob: &Blob) {
        {
            let pal = self.pal.read().unwrap_or_else(|poisoned| poisoned.into_inner());
            if pal.ready {
                return;
            }
        }
        let mut slot = self.pal.write().unwrap_or_else(|poisoned| poisoned.into_inner());
        if slot.ready {
            return;
        }
        *slot = Arc::new(Palette::collect(blob, &slot));
    }
}

struct Live {
    start: usize,
    end: usize,
    share: usize,
}

static LIVE: Mutex<Vec<Live>> = Mutex::new(Vec::new());

struct Active {
    start: usize,
    end: usize,
    origin: usize,
    pal: Arc<Palette>,
}

thread_local! {
    static ACTIVE: std::cell::RefCell<Option<Active>> = std::cell::RefCell::new(None);
}

fn live_lock() -> std::sync::MutexGuard<'static, Vec<Live>> {
    LIVE.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn share_ptr(share: &Share) -> usize {
    share as *const Share as usize
}

/// Every plane of one piece, and the one buffer they live in.
pub struct Canvas {
    /// The one allocation. Nothing reads it through this field after the canvas
    /// is built; it is here because it owns the memory, and `base` is how the
    /// planes reach it. Going through the box itself would mean handing out a
    /// reference to the whole of it, which is the one thing the band rule
    /// forbids.
    #[allow(dead_code)]
    store: Box<[u8]>,
    base: *mut u8,
    len: usize,
    pub ground: Plane,
    pub veg: Plane,
    pub biome: Plane,
    /// One byte a tile, only when the piece has woods or scrub in it.
    pub mask: Plane,
    /// One bit a tile, padded to a byte a row: where there is water at all, and
    /// where that water is still. A bank is clay either way, but lake clay and
    /// river clay are different tiles in the game.
    water: Plane,
    lake: Plane,
    pub width: usize,
    pub height: usize,
    /// Bytes a row of a bit mask.
    pub bit_stride: usize,
    /// Piece row of canvas row 0. Zero when the canvas is the whole piece.
    pub origin_y: usize,
    nothing_index: u8,
    share: Box<Share>,
}

// Safety: see the note at the top of this file. Nothing here is read and
// written at the same time. The palette is swapped once, under its lock,
// before the threads paint, and after that it is only read.
unsafe impl Sync for Canvas {}
unsafe impl Send for Canvas {}

impl Drop for Canvas {
    fn drop(&mut self) {
        let share = share_ptr(&self.share);
        live_lock().retain(|row| row.share != share);
    }
}

impl Canvas {
    /// The pictures of one piece, cleared to what unmapped ground is: grass,
    /// nothing growing, and the open country of this climate.
    ///
    /// Ground and vegetation are palette indexes. Index 0 is this piece's
    /// grass. Nothing-growing is the next free index, unless it is the same
    /// colour. `prepare` folds in the rest of the colours the piece paints.
    pub fn new(piece: &Piece, want_mask: bool, want_water: bool) -> Canvas {
        Self::window(piece, 0, piece.height as usize, want_mask, want_water)
    }

    /// A band of the piece, `height` rows starting at piece row `origin_y`.
    ///
    /// Callers pass piece rows. The buffer only holds this band, and every
    /// row accessor turns a piece row into a canvas row by subtracting
    /// `origin_y`.
    pub fn window(
        piece: &Piece,
        origin_y: usize,
        height: usize,
        want_mask: bool,
        want_water: bool,
    ) -> Canvas {
        let width = piece.width as usize;
        let height = height.min(piece.height as usize - origin_y.min(piece.height as usize));
        let tiles = width * height;
        let bit_stride = (width + 7) / 8;

        let mut at = 0usize;
        let mut plane = |bpp: usize, live: bool, stride_bytes: usize| -> Plane {
            let here = at;
            if live {
                at += stride_bytes * height;
            }
            Plane {
                width,
                height,
                bpp,
                at: here,
                live,
            }
        };
        let ground = plane(GROUND_BPP, true, width * GROUND_BPP);
        let veg = plane(VEG_BPP, true, width * VEG_BPP);
        let biome = plane(BIOME_BPP, true, width * BIOME_BPP);
        let mask = plane(MASK_BPP, want_mask, width * MASK_BPP);
        let water = plane(1, want_water, bit_stride);
        let lake = plane(1, want_water, bit_stride);
        let total = at;

        let mut store = vec![0u8; total].into_boxed_slice();
        let palette = Palette::seed(piece.grass, piece.nothing);
        let nothing_index = palette.nothing;
        store[veg.at..veg.at + tiles * VEG_BPP].fill(nothing_index);
        store[biome.at..biome.at + tiles].fill(piece.open_biome);
        let base = store.as_mut_ptr();
        let len = store.len();
        let share = Box::new(Share {
            start: base as usize,
            end: base as usize + len,
            origin: AtomicUsize::new(origin_y),
            pal: RwLock::new(Arc::new(palette)),
        });
        live_lock().push(Live {
            start: base as usize,
            end: base as usize + len,
            share: share_ptr(&share),
        });

        Canvas {
            ground,
            veg,
            biome,
            mask,
            water,
            lake,
            width,
            height,
            bit_stride,
            origin_y,
            nothing_index,
            store,
            base,
            len,
            share,
        }
    }

    /// Fold every colour this job paints into the palette, and remember which
    /// indexes sit in each colour set. Safe to call from every thread: one
    /// builds it, the rest wait and then share it.
    pub fn prepare(&self, blob: &Blob) {
        self.sync_origin();
        self.share.prepare(blob);
        self.activate();
    }

    /// The palette, including grass at index 0. After `prepare`, every colour
    /// the piece paints is in it.
    pub fn colours(&self) -> Arc<Palette> {
        Arc::clone(
            &self
                .share
                .pal
                .read()
                .unwrap_or_else(|poisoned| poisoned.into_inner()),
        )
    }

    #[inline]
    pub fn nothing_index(&self) -> u8 {
        self.nothing_index
    }

    /// Canvas row of a piece row.
    #[inline]
    pub fn row(&self, piece_y: usize) -> usize {
        piece_y.saturating_sub(self.origin_y)
    }

    /// A run of the buffer, to read.
    #[inline]
    fn part(&self, from: usize, to: usize) -> &[u8] {
        let (from, to) = (from.min(self.len), to.clamp(from.min(self.len), self.len));
        // Safety: inside the one allocation, which lives as long as `self`, and
        // the caller guarantees no thread is writing it.
        unsafe { std::slice::from_raw_parts(self.base.add(from), to - from) }
    }

    /// A run of the buffer, to write.
    ///
    /// # Safety
    /// This run must be held by exactly one thread, and no thread may be reading
    /// the plane it belongs to.
    #[allow(clippy::mut_from_ref)]
    #[inline]
    unsafe fn part_mut(&self, from: usize, to: usize) -> &mut [u8] {
        let (from, to) = (from.min(self.len), to.clamp(from.min(self.len), self.len));
        std::slice::from_raw_parts_mut(self.base.add(from), to - from)
    }

    /// Rows `y0` up to but not including `y1` of one plane, to read.
    ///
    /// `y0` and `y1` are piece rows. Canvas row is piece row minus `origin_y`.
    /// Only call this when no thread is writing that plane.
    pub fn rows(&self, plane: &Plane, y0: usize, y1: usize) -> &[u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        let (from, to) = self.span(plane, y0, y1);
        self.part(from, to)
    }

    /// Rows `y0` up to but not including `y1` of one plane, to write.
    ///
    /// # Safety
    /// This band of this plane must be held by exactly one thread, and no thread
    /// may be reading the same plane while it is held.
    #[allow(clippy::mut_from_ref)]
    pub unsafe fn rows_mut(&self, plane: &Plane, y0: usize, y1: usize) -> &mut [u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        let (from, to) = self.span(plane, y0, y1);
        self.part_mut(from, to)
    }

    /// The whole of one plane, to read.
    pub fn whole(&self, plane: &Plane) -> &[u8] {
        self.rows(plane, self.origin_y, self.origin_y + self.height)
    }

    /// Piece rows to canvas rows.
    fn canvas_span(&self, y0: usize, y1: usize) -> (usize, usize) {
        (
            y0.saturating_sub(self.origin_y),
            y1.saturating_sub(self.origin_y),
        )
    }

    fn span(&self, plane: &Plane, y0: usize, y1: usize) -> (usize, usize) {
        if !plane.live {
            return (0, 0);
        }
        let y0 = y0.min(plane.height);
        let y1 = y1.clamp(y0, plane.height);
        let stride = plane.stride();
        (plane.at + y0 * stride, plane.at + y1 * stride)
    }

    /// The water masks. They are bit planes with their own stride, so they get
    /// their own accessors rather than going through `Plane`.
    pub fn water_rows(&self, y0: usize, y1: usize) -> &[u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        self.bits(&self.water, y0, y1)
    }

    pub fn lake_rows(&self, y0: usize, y1: usize) -> &[u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        self.bits(&self.lake, y0, y1)
    }

    /// # Safety
    /// As `rows_mut`. Bit masks are padded to a byte a row, so two bands never
    /// share a byte.
    #[allow(clippy::mut_from_ref)]
    pub unsafe fn water_rows_mut(&self, y0: usize, y1: usize) -> &mut [u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        self.bits_mut(&self.water, y0, y1)
    }

    /// # Safety
    /// As `rows_mut`.
    #[allow(clippy::mut_from_ref)]
    pub unsafe fn lake_rows_mut(&self, y0: usize, y1: usize) -> &mut [u8] {
        self.sync_origin();
        let (y0, y1) = self.canvas_span(y0, y1);
        self.bits_mut(&self.lake, y0, y1)
    }

    pub fn has_water(&self) -> bool {
        self.water.live
    }

    fn bits(&self, plane: &Plane, y0: usize, y1: usize) -> &[u8] {
        if !plane.live {
            return &[];
        }
        let (from, to) = self.bit_span(plane, y0, y1);
        self.part(from, to)
    }

    #[allow(clippy::mut_from_ref)]
    unsafe fn bits_mut(&self, plane: &Plane, y0: usize, y1: usize) -> &mut [u8] {
        if !plane.live {
            return self.part_mut(0, 0);
        }
        let (from, to) = self.bit_span(plane, y0, y1);
        self.part_mut(from, to)
    }

    fn bit_span(&self, plane: &Plane, y0: usize, y1: usize) -> (usize, usize) {
        let y0 = y0.min(self.height);
        let y1 = y1.clamp(y0, self.height);
        (
            plane.at + y0 * self.bit_stride,
            plane.at + y1 * self.bit_stride,
        )
    }

    fn sync_origin(&self) {
        self.share.origin.store(self.origin_y, Ordering::Relaxed);
        let origin = self.origin_y;
        let start = self.base as usize;
        ACTIVE.with(|slot| {
            if let Some(active) = slot.borrow_mut().as_mut() {
                if active.start == start {
                    active.origin = origin;
                }
            }
        });
    }

    fn activate(&self) {
        let pal = self.colours();
        let origin = self.share.origin.load(Ordering::Relaxed);
        ACTIVE.with(|slot| {
            *slot.borrow_mut() = Some(Active {
                start: self.base as usize,
                end: self.base as usize + self.len,
                origin,
                pal,
            });
        });
    }
}

/// Build the palette from a slice of this canvas, then pin it to this thread
/// so the rasteriser can turn a colour into an index without holding the canvas.
pub fn prepare_slice(sample: &[u8], blob: &Blob) {
    let Some(share) = find_share(sample.as_ptr()) else {
        return;
    };
    // Safety: the canvas that owns `share` is alive for as long as `sample` is,
    // which is the band the caller is drawing.
    unsafe { &*share }.prepare(blob);
    remember(sample.as_ptr());
}

/// Canvas row of `piece_y` inside a band whose first piece row is `band_y0`.
/// The band's buffer starts at that canvas row, so the offset into the buffer
/// is the difference of the two canvas rows.
#[inline]
pub fn band_row(origin_y: usize, piece_y: usize, band_y0: usize) -> usize {
    piece_y
        .saturating_sub(origin_y)
        .saturating_sub(band_y0.saturating_sub(origin_y))
}

/// Piece row of canvas row 0 for the canvas that owns `sample`, or 0 when the
/// slice is not one of ours.
pub fn origin_of(sample: *const u8) -> usize {
    view(sample, |active| active.origin).unwrap_or(0)
}

/// Palette index of `colour` in the canvas that owns `sample`.
pub fn colour_index(sample: *const u8, colour: [u8; 3]) -> u8 {
    view(sample, |active| active.pal.index_rgb(colour)).unwrap_or(0)
}

/// The palette of the canvas that owns `sample`. A bitmap writer uses it to
/// turn indexes back into the colours the game reads. `None` when `sample` is
/// not a plane of a live canvas. This reads the canvas, not the thread's
/// cached copy, so a band written after `prepare` sees every colour.
pub fn colours_of(sample: *const u8) -> Option<Arc<Palette>> {
    load_active(sample).map(|active| active.pal)
}

pub fn map_colour(sample: *const u8, colour: [u8; 3]) -> (usize, u8) {
    view(sample, |active| (active.origin, active.pal.index_rgb(colour))).unwrap_or((0, 0))
}

/// Whether `index` is in colour set `set` for the canvas that owns `sample`.
pub fn index_in_set(sample: *const u8, set: u8, index: u8) -> bool {
    if set == ANY_SET {
        return true;
    }
    view(sample, |active| active.pal.in_set(set, index)).unwrap_or(false)
}

fn view<R>(sample: *const u8, f: impl FnOnce(&Active) -> R) -> Option<R> {
    ACTIVE.with(|slot| {
        let mut slot = slot.borrow_mut();
        let fresh = match slot.as_ref() {
            Some(active) if sample as usize >= active.start && (sample as usize) < active.end => {
                false
            }
            _ => true,
        };
        if fresh {
            *slot = load_active(sample);
        }
        slot.as_ref().map(f)
    })
}

fn remember(sample: *const u8) {
    ACTIVE.with(|slot| {
        *slot.borrow_mut() = load_active(sample);
    });
}

fn load_active(sample: *const u8) -> Option<Active> {
    let share = find_share(sample)?;
    // Safety: registered for the life of the canvas, and the canvas outlives
    // every slice handed to a band.
    let share = unsafe { &*share };
    let pal = Arc::clone(
        &share
            .pal
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner()),
    );
    Some(Active {
        start: share.start,
        end: share.end,
        origin: share.origin.load(Ordering::Relaxed),
        pal,
    })
}

fn find_share(sample: *const u8) -> Option<*const Share> {
    let at = sample as usize;
    let live = live_lock();
    live.iter()
        .find(|row| at >= row.start && at < row.end)
        .map(|row| row.share as *const Share)
}

/// Whether tile `x` of a bit mask row is set.
#[inline]
pub fn bit(row: &[u8], x: usize) -> bool {
    match row.get(x >> 3) {
        Some(byte) => byte & (1 << (x & 7)) != 0,
        None => false,
    }
}

/// Set tile `x` of a bit mask row.
#[inline]
pub fn set_bit(row: &mut [u8], x: usize) {
    if let Some(byte) = row.get_mut(x >> 3) {
        *byte |= 1 << (x & 7);
    }
}

/// 0x00RRGGBB as the three bytes a picture holds.
#[inline]
pub fn rgb(colour: u32) -> [u8; 3] {
    [
        ((colour >> 16) & 0xFF) as u8,
        ((colour >> 8) & 0xFF) as u8,
        (colour & 0xFF) as u8,
    ]
}

/// The three bytes a picture holds, as 0x00RRGGBB.
#[inline]
pub fn packed(tile: &[u8]) -> u32 {
    ((tile[0] as u32) << 16) | ((tile[1] as u32) << 8) | tile[2] as u32
}

#[inline]
fn pack(colour: [u8; 3]) -> u32 {
    ((colour[0] as u32) << 16) | ((colour[1] as u32) << 8) | colour[2] as u32
}
