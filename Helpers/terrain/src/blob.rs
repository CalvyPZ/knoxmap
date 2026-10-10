//! What Python hands over: every piece of ground of every map of every mod, in
//! one buffer, read in place.
//!
//! Python still does the work only it can do - reading the OSM extract, deciding
//! what a way is, cutting junctions and bridges and house plots with GEOS - but
//! it stops at the point where that work becomes pixels. What it sends is an
//! ordered program: fill this outline with this colour, draw this line this
//! wide, then scatter tufts over the grass with this chance. The order is the
//! order Python painted in, so the ground comes out the same way round: a
//! driveway over a lawn, a kerb over a carriageway, a tree over both.
//!
//! Nothing here is copied. The buffer stays where Python put it and every
//! accessor reads out of it, so a map with ten million points costs ten million
//! points once rather than once a thread.
//!
//! Layout. A sixteen byte head, then eleven sections, each an offset and a
//! length into the same buffer:
//!
//! ```text
//! magic   "KNOXTER1"
//! abi     u32
//! pieces  u32
//! section 0  strings      the bytes of every name and directory
//!         1  string index (u32 at, u32 n) a string
//!         2  colour sets  which ground colours a pass is allowed to touch
//!         3  pieces       one record a piece of ground
//!         4  commands     the program, in painting order
//!         5  rings        (u32 at, u32 n, u32 role) a ring of points
//!         6  points       i32 pairs, sixteenths of a tile
//!         7  passes       the procedural passes the program refers to
//!         8  picks        what a pass may put down, with weights
//!         9  cells        i32 pairs, the 300-tile cells a piece keeps
//!        10  order        the pieces biggest first, so no core idles at the end
//!        11  spawn bands  how thick the zombies are over each kind of ground
//! ```

use crate::err::{Error, Result};

pub const MAGIC: &[u8; 8] = b"KNOXTER1";
pub const ABI_VERSION: u32 = 1;

pub const PIECE_BYTES: usize = 64;
pub const CMD_BYTES: usize = 32;
pub const RING_BYTES: usize = 12;
pub const POINT_BYTES: usize = 8;
pub const PASS_BYTES: usize = 24;
pub const PICK_BYTES: usize = 16;
pub const CELL_BYTES: usize = 8;
pub const BAND_BYTES: usize = 12;

const HEADER_FIXED: usize = 16;
const N_SECTIONS: usize = 12;
pub const HEADER_BYTES: usize = HEADER_FIXED + N_SECTIONS * 16;

const S_STRINGS: usize = 0;
const S_STRING_INDEX: usize = 1;
const S_SETS: usize = 2;
const S_PIECES: usize = 3;
const S_CMDS: usize = 4;
const S_RINGS: usize = 5;
const S_POINTS: usize = 6;
const S_PASSES: usize = 7;
const S_PICKS: usize = 8;
const S_CELLS: usize = 9;
const S_ORDER: usize = 10;
const S_BANDS: usize = 11;

/// A command that paints nothing on that plane.
pub const NO_COLOUR: u32 = 0xFFFF_FFFF;
/// A command that leaves the biome alone.
pub const NO_BIOME: u16 = 0xFFFF;
/// A pass that does not care what is already there.
pub const ANY_SET: u8 = 0xFF;

/// Points come over as sixteenths of a tile. A tile is two metres, so a
/// sixteenth is twelve centimetres: finer than the ground can show, and small
/// enough that a kerb line lands on the right side of a tile every time.
const POINT_SCALE: f32 = 1.0 / 16.0;

/// What one entry of the program draws.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Shape {
    /// Every ring filled on its own, the way a lake drawn as two rings is two
    /// lakes rather than one with a hole in it.
    Fill = 1,
    /// A line of the given width through every ring, with round bends.
    Stroke = 2,
    /// Two corners, both included. Ring one holds the two points.
    Rect = 3,
    /// Outer rings minus inner ones: a courtyard, an island, a field in a wood.
    Relation = 4,
    /// A filled circle at the first point of the first ring, the width being the
    /// diameter. A single mapped tree, a bollard, a manhole.
    Disc = 5,
    /// Not a shape: run pass `ring_at` of the pass table here, in this order.
    Pass = 6,
}

impl Shape {
    fn from(byte: u8) -> Result<Shape> {
        match byte {
            1 => Ok(Shape::Fill),
            2 => Ok(Shape::Stroke),
            3 => Ok(Shape::Rect),
            4 => Ok(Shape::Relation),
            5 => Ok(Shape::Disc),
            6 => Ok(Shape::Pass),
            _ => Err(Error::BadProgram),
        }
    }
}

/// Which pictures a command writes.
pub const TO_GROUND: u8 = 1 << 0;
pub const TO_VEG: u8 = 1 << 1;
pub const TO_BIOME: u8 = 1 << 2;
/// Mark these tiles as water, so a later pass can find the banks.
pub const TO_WATER: u8 = 1 << 3;
/// Mark them as still water as well, so the bank is lake clay and not river.
pub const TO_LAKE: u8 = 1 << 4;
/// Write `mask` into the scratch plane instead of a picture. This is how a wood
/// gets an edge: the outline goes into the mask, and the woodland pass reads the
/// mask to tell a tile deep inside a forest from one at its edge.
pub const TO_MASK: u8 = 1 << 5;

/// What a ring is to the relation it belongs to. Nought is an outer ring, which
/// is also what a ring that is not part of a relation gets.
pub const ROLE_INNER: u32 = 1;

/// One entry of the program.
#[derive(Clone, Copy)]
pub struct Cmd {
    pub shape: Shape,
    pub to: u8,
    /// A line's width in tiles, or a disc's diameter.
    pub width: u16,
    pub ring_at: u32,
    pub ring_n: u32,
    pub ground: u32,
    pub veg: u32,
    pub biome: u16,
    pub mask: u16,
    /// The first and last row this command can reach, already clipped to the
    /// piece. A thread drawing rows 4000 to 4500 of a city skips every command
    /// outside them without looking at a single point, which is what makes
    /// handing the same program to every thread cheap.
    pub top: u16,
    pub bottom: u16,
}

impl Cmd {
    /// Whether this command reaches into the rows `y0` up to `y1`.
    #[inline]
    pub fn touches(&self, y0: usize, y1: usize) -> bool {
        self.bottom as usize >= y0 && (self.top as usize) < y1
    }

    #[inline]
    pub fn ground_colour(&self) -> Option<u32> {
        if self.to & TO_GROUND != 0 && self.ground != NO_COLOUR {
            Some(self.ground)
        } else {
            None
        }
    }

    #[inline]
    pub fn veg_colour(&self) -> Option<u32> {
        if self.to & TO_VEG != 0 && self.veg != NO_COLOUR {
            Some(self.veg)
        } else {
            None
        }
    }

    #[inline]
    pub fn biome_index(&self) -> Option<u8> {
        if self.to & TO_BIOME != 0 && self.biome != NO_BIOME {
            Some(self.biome as u8)
        } else {
            None
        }
    }

    #[inline]
    pub fn mask_value(&self) -> Option<u8> {
        if self.to & TO_MASK != 0 {
            Some(self.mask as u8)
        } else {
            None
        }
    }

}

/// One ring of points, and what it is to its relation.
#[derive(Clone, Copy)]
pub struct Ring {
    pub at: u32,
    pub n: u32,
    pub role: u32,
}

/// What a pass does. The kinds are the procedural passes of the Python
/// renderer, each with its numbers pulled out so Python keeps deciding how
/// thick a wood is and Rust only does the walking.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum PassKind {
    /// A chance a tile. With a radius, a blot of that size instead of a tile:
    /// worn tarmac, a clump of bushes, a patch of flowers.
    Scatter = 1,
    /// Every `spacing` tiles both ways. What makes an orchard read as an
    /// orchard from the air is that its trees stand in rows.
    Grid = 2,
    /// Woods, from the mask: full shade deep inside, a lighter mix at the edge,
    /// scrub where the mask says scrub, nothing over water or tarmac.
    Woodland = 3,
    /// The banks of water: clay a tile or two out from every shore.
    Shores = 4,
    /// The zombie spawn map, a tenth scale picture of where they start.
    Spawn = 5,
    /// Trees sit best on dark grass: wherever something is growing, step the
    /// ground under it down to the darker shade.
    Soften = 6,
    /// Where the scratch mask holds `extra`, and the ground is one this pass is
    /// allowed over, put the pick down. City blocks are paved this way, and
    /// buildings are cleared of trees this way.
    PaintMask = 7,
    /// A pavement tile with a road beside it becomes the kerb that faces the
    /// road. The eight picks are the eight directions, in the order west,
    /// north, south, east, then the four corners.
    Kerbs = 8,
    /// On a grid, near a mark in the scratch mask and not on it: garden trees
    /// within reach of a house and off its wall.
    Near = 9,
}

impl PassKind {
    fn from(byte: u8) -> Result<PassKind> {
        match byte {
            1 => Ok(PassKind::Scatter),
            2 => Ok(PassKind::Grid),
            3 => Ok(PassKind::Woodland),
            4 => Ok(PassKind::Shores),
            5 => Ok(PassKind::Spawn),
            6 => Ok(PassKind::Soften),
            7 => Ok(PassKind::PaintMask),
            8 => Ok(PassKind::Kerbs),
            9 => Ok(PassKind::Near),
            _ => Err(Error::BadProgram),
        }
    }
}

/// A scatter or a grid that only lands where the scratch mask holds `extra`.
pub const PASS_IN_MASK: u16 = 1 << 0;
/// A pass that leaves a tile alone when something is already growing there.
pub const PASS_BARE: u16 = 1 << 1;

#[derive(Clone, Copy)]
pub struct Pass {
    pub kind: PassKind,
    /// The colour set a tile's ground must be in for the pass to touch it.
    pub over: u8,
    /// The colour set that stops it: water under trees, tarmac under tufts.
    pub avoid: u8,
    /// How far a blot reaches. Zero is a single tile.
    pub radius: u8,
    /// The chance a tile, for `Scatter`.
    pub chance: f32,
    /// Mixed into the tile hash, so two passes over the same ground with the
    /// same chance do not land on the same tiles.
    pub salt: u16,
    /// For `Grid`, the gap between rows. For `Woodland`, how far inside the wood
    /// the edge band reaches. For `Shores`, how far the clay comes out.
    pub spacing: u16,
    pub pick_at: u32,
    pub pick_n: u16,
    pub flags: u16,
    /// Kind specific: the mask value `Woodland` treats as scrub, the scale
    /// `Spawn` shrinks by.
    pub extra: u32,
}

/// What a pass may put down on one tile.
#[derive(Clone, Copy)]
pub struct Pick {
    pub ground: u32,
    pub veg: u32,
    pub biome: u16,
    /// Relative weight. A pick of weight zero is never chosen.
    pub weight: u16,
}

/// How thick the zombies stand on one kind of ground.
///
/// The bands are tried in order and the first whose colour set matches wins,
/// which is the if-else chain of the Python spawn pass written down: water first
/// and empty, then the paved ground where crowds belong, then yards and tracks,
/// then open grass, and last a band over any ground at all. A band with no
/// chance leaves the ground empty, which is how water gets no zombies.
#[derive(Clone, Copy)]
pub struct SpawnBand {
    pub set: u8,
    /// The share of this ground that has anybody on it at all, as a fraction of
    /// 65535. Vanilla leaves nearly all of the map empty and the sparsity
    /// matters as much as the ceiling.
    pub chance: u16,
    /// How thick they stand when they are there, as fractions of the piece's
    /// ceiling.
    pub lo: u16,
    pub hi: u16,
    pub salt: u16,
}

impl SpawnBand {
    #[inline]
    pub fn chance_of(&self) -> f32 {
        self.chance as f32 / 65535.0
    }

    #[inline]
    pub fn lo_of(&self) -> f32 {
        self.lo as f32 / 65535.0
    }

    #[inline]
    pub fn hi_of(&self) -> f32 {
        self.hi as f32 / 65535.0
    }
}

/// One piece of ground: one map's one cell grid, or one mod's.
#[derive(Clone, Copy)]
pub struct Piece {
    pub width: u32,
    pub height: u32,
    pub seed: u32,
    /// The piece's name, for the files it writes.
    pub name: u32,
    /// Where to write them.
    pub dir: u32,
    pub cmd_at: u32,
    pub cmd_n: u32,
    pub keep_at: u32,
    pub keep_n: u32,
    /// What the biome is where nothing is mapped: the open country of this
    /// climate.
    pub open_biome: u8,
    pub spawn_density: u8,
    /// The colour unmapped ground starts as, and what nothing growing looks
    /// like. Both come from the palette rather than being built in, because the
    /// palette is Rules.txt's and Rules.txt can change.
    pub grass: u32,
    pub nothing: u32,
    /// The spawn bands this piece uses, and how many tiles a spawn map pixel
    /// stands for. The guide says a tenth, and the guide is what the game reads.
    pub spawn_at: u32,
    pub spawn_n: u16,
    pub spawn_scale: u16,
}

impl Piece {
    #[inline]
    pub fn tiles(&self) -> usize {
        self.width as usize * self.height as usize
    }

    /// Whether any entry of this piece's program needs the scratch mask plane.
    /// Worth asking, because the plane is another byte a tile and a big piece is
    /// two hundred million tiles.
    pub fn wants_mask(&self, blob: &Blob) -> bool {
        for at in self.cmd_at as usize..(self.cmd_at + self.cmd_n) as usize {
            if let Ok(cmd) = blob.cmd(at) {
                if cmd.to & TO_MASK != 0 {
                    return true;
                }
                if cmd.shape == Shape::Pass {
                    if let Ok(pass) = blob.pass(cmd.ring_at as usize) {
                        if matches!(
                            pass.kind,
                            PassKind::Woodland | PassKind::PaintMask | PassKind::Near
                        ) || pass.flags & PASS_IN_MASK != 0
                        {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }

    /// Whether the piece needs the water and still-water masks.
    pub fn wants_water(&self, blob: &Blob) -> bool {
        for at in self.cmd_at as usize..(self.cmd_at + self.cmd_n) as usize {
            if let Ok(cmd) = blob.cmd(at) {
                if cmd.to & (TO_WATER | TO_LAKE) != 0 {
                    return true;
                }
            }
        }
        false
    }
}

/// Where one section of the buffer starts and how long it is.
#[derive(Clone, Copy, Default)]
struct Section {
    at: usize,
    len: usize,
}

/// The buffer Python handed over, checked once and then read in place.
pub struct Blob {
    bytes: &'static [u8],
    section: [Section; N_SECTIONS],
    n_pieces: usize,
    /// The colour sets, unpacked at the start because every pass asks for them
    /// on every tile.
    sets: Vec<(usize, usize)>,
    set_colours_at: usize,
}

impl Blob {
    /// Check the buffer over, once, before any thread touches it. Everything a
    /// worker reads is proved to be in range here, so a worker's inner loop has
    /// no bounds to re-check and a short or crooked buffer is a clean error
    /// rather than a crash in a thread.
    ///
    /// # Safety
    /// `bytes` must stay put, and stay unchanged, until `Blob` is dropped.
    pub unsafe fn parse(bytes: *const u8, len: usize) -> Result<Blob> {
        if bytes.is_null() || len < HEADER_BYTES {
            return Err(Error::Truncated);
        }
        let bytes: &'static [u8] = std::slice::from_raw_parts(bytes, len);
        if &bytes[0..8] != MAGIC {
            return Err(Error::BadMagic);
        }
        if read_u32(bytes, 8) != ABI_VERSION {
            return Err(Error::BadAbi);
        }
        let n_pieces = read_u32(bytes, 12) as usize;

        let mut section = [Section::default(); N_SECTIONS];
        for (which, slot) in section.iter_mut().enumerate() {
            let head = HEADER_FIXED + which * 16;
            let at = read_u64(bytes, head) as usize;
            let bytes_len = read_u64(bytes, head + 8) as usize;
            if at > len || bytes_len > len - at {
                return Err(Error::Truncated);
            }
            *slot = Section {
                at,
                len: bytes_len,
            };
        }

        let whole = |which: usize, record: usize| -> bool {
            record == 0 || section[which].len % record == 0
        };
        if !whole(S_STRING_INDEX, 8)
            || !whole(S_PIECES, PIECE_BYTES)
            || !whole(S_CMDS, CMD_BYTES)
            || !whole(S_RINGS, RING_BYTES)
            || !whole(S_POINTS, POINT_BYTES)
            || !whole(S_PASSES, PASS_BYTES)
            || !whole(S_PICKS, PICK_BYTES)
            || !whole(S_CELLS, CELL_BYTES)
            || !whole(S_ORDER, 4)
            || !whole(S_BANDS, BAND_BYTES)
        {
            return Err(Error::BadCounts);
        }
        if section[S_PIECES].len != n_pieces * PIECE_BYTES
            || section[S_ORDER].len != n_pieces * 4
        {
            return Err(Error::BadCounts);
        }

        let mut blob = Blob {
            bytes,
            section,
            n_pieces,
            sets: Vec::new(),
            set_colours_at: 0,
        };
        blob.read_sets()?;
        blob.check_program()?;
        Ok(blob)
    }

    /// The colour sets: how many, then where each one's colours are.
    fn read_sets(&mut self) -> Result<()> {
        let whole = self.section[S_SETS];
        if whole.len == 0 {
            return Ok(());
        }
        if whole.len < 4 {
            return Err(Error::BadCounts);
        }
        let n = read_u32(self.bytes, whole.at) as usize;
        let table = 4 + n * 8;
        if whole.len < table {
            return Err(Error::BadCounts);
        }
        let colours = (whole.len - table) / 4;
        self.set_colours_at = whole.at + table;
        self.sets.reserve(n);
        for which in 0..n {
            let head = whole.at + 4 + which * 8;
            let at = read_u32(self.bytes, head) as usize;
            let count = read_u32(self.bytes, head + 4) as usize;
            if at + count > colours {
                return Err(Error::BadCounts);
            }
            self.sets.push((at, count));
        }
        Ok(())
    }

    /// Every index that a worker would otherwise have to trust.
    fn check_program(&self) -> Result<()> {
        let n_cmds = self.n_cmds();
        let n_rings = self.n_rings();
        let n_points = self.n_points();
        let n_passes = self.n_passes();
        let n_picks = self.n_picks();
        let n_cells = self.n_cells();
        let n_strings = self.n_strings();

        for at in 0..self.n_pieces {
            let which = self.order_at(at)?;
            if which >= self.n_pieces {
                return Err(Error::BadCounts);
            }
        }
        for at in 0..self.n_pieces {
            let piece = self.piece(at)?;
            if piece.width == 0 || piece.height == 0 {
                return Err(Error::BadCounts);
            }
            // The planes of a piece are addressed as one allocation, so a piece
            // whose tiles times eight bytes would not fit in an offset is
            // refused here rather than overflowing in a worker.
            let tiles = (piece.width as u64) * (piece.height as u64);
            if tiles.saturating_mul(9) > (isize::MAX as u64) / 2 {
                return Err(Error::TooBig);
            }
            if piece.cmd_at as usize + piece.cmd_n as usize > n_cmds
                || piece.keep_at as usize + piece.keep_n as usize > n_cells
                || piece.spawn_at as usize + piece.spawn_n as usize > self.n_spawn_bands()
                || piece.name as usize >= n_strings.max(1)
                || piece.dir as usize >= n_strings.max(1)
            {
                return Err(Error::BadProgram);
            }
        }
        for at in 0..n_cmds {
            let cmd = self.cmd(at)?;
            if cmd.shape == Shape::Pass {
                if cmd.ring_at as usize >= n_passes {
                    return Err(Error::BadProgram);
                }
                continue;
            }
            if cmd.ring_at as usize + cmd.ring_n as usize > n_rings {
                return Err(Error::BadProgram);
            }
        }
        for at in 0..n_rings {
            let ring = self.ring(at)?;
            if ring.at as usize + ring.n as usize > n_points {
                return Err(Error::BadProgram);
            }
        }
        for at in 0..n_passes {
            let pass = self.pass(at)?;
            if pass.pick_at as usize + pass.pick_n as usize > n_picks {
                return Err(Error::BadProgram);
            }
            for set in [pass.over, pass.avoid] {
                if set != ANY_SET && set as usize >= self.sets.len() {
                    return Err(Error::BadProgram);
                }
            }
        }
        for at in 0..self.n_spawn_bands() {
            let band = self.spawn_band(at);
            if band.set != ANY_SET && band.set as usize >= self.sets.len() {
                return Err(Error::BadProgram);
            }
        }
        for at in 0..n_strings {
            let head = self.section[S_STRING_INDEX].at + at * 8;
            let from = read_u32(self.bytes, head) as usize;
            let n = read_u32(self.bytes, head + 4) as usize;
            if from + n > self.section[S_STRINGS].len {
                return Err(Error::Truncated);
            }
        }
        Ok(())
    }

    pub fn n_pieces(&self) -> usize {
        self.n_pieces
    }

    pub fn n_cmds(&self) -> usize {
        self.section[S_CMDS].len / CMD_BYTES
    }

    pub fn n_rings(&self) -> usize {
        self.section[S_RINGS].len / RING_BYTES
    }

    pub fn n_points(&self) -> usize {
        self.section[S_POINTS].len / POINT_BYTES
    }

    pub fn n_passes(&self) -> usize {
        self.section[S_PASSES].len / PASS_BYTES
    }

    pub fn n_picks(&self) -> usize {
        self.section[S_PICKS].len / PICK_BYTES
    }

    pub fn n_cells(&self) -> usize {
        self.section[S_CELLS].len / CELL_BYTES
    }

    pub fn n_strings(&self) -> usize {
        self.section[S_STRING_INDEX].len / 8
    }

    pub fn n_spawn_bands(&self) -> usize {
        self.section[S_BANDS].len / BAND_BYTES
    }

    pub fn spawn_band(&self, at: usize) -> SpawnBand {
        let head = self.section[S_BANDS].at + at * BAND_BYTES;
        let b = self.bytes;
        SpawnBand {
            set: b[head],
            // Byte one is spare.
            chance: read_u16(b, head + 2),
            lo: read_u16(b, head + 4),
            hi: read_u16(b, head + 6),
            salt: read_u16(b, head + 8),
        }
    }

    /// Which piece to do next. Python sends them biggest first so that the last
    /// thread to finish is not the one that picked up the biggest map last.
    pub fn order_at(&self, at: usize) -> Result<usize> {
        if at >= self.n_pieces {
            return Err(Error::BadCounts);
        }
        Ok(read_u32(self.bytes, self.section[S_ORDER].at + at * 4) as usize)
    }

    pub fn piece(&self, at: usize) -> Result<Piece> {
        if at >= self.n_pieces {
            return Err(Error::BadCounts);
        }
        let head = self.section[S_PIECES].at + at * PIECE_BYTES;
        let b = self.bytes;
        Ok(Piece {
            width: read_u32(b, head),
            height: read_u32(b, head + 4),
            seed: read_u32(b, head + 8),
            name: read_u32(b, head + 12),
            dir: read_u32(b, head + 16),
            cmd_at: read_u32(b, head + 20),
            cmd_n: read_u32(b, head + 24),
            keep_at: read_u32(b, head + 28),
            keep_n: read_u32(b, head + 32),
            // Bytes 36 to 43 are spare: how thick the woods are and how many
            // metres a tile is are decided in Python, where the shades and the
            // widths they change are decided.
            open_biome: b[head + 44],
            spawn_density: b[head + 45],
            // Bytes 46 and 47 are spare.
            grass: read_u32(b, head + 48),
            nothing: read_u32(b, head + 52),
            spawn_at: read_u32(b, head + 56),
            spawn_n: read_u16(b, head + 60),
            spawn_scale: read_u16(b, head + 62),
        })
    }

    pub fn cmd(&self, at: usize) -> Result<Cmd> {
        if at >= self.n_cmds() {
            return Err(Error::BadProgram);
        }
        let head = self.section[S_CMDS].at + at * CMD_BYTES;
        let b = self.bytes;
        Ok(Cmd {
            shape: Shape::from(b[head])?,
            to: b[head + 1],
            width: read_u16(b, head + 2),
            ring_at: read_u32(b, head + 4),
            ring_n: read_u32(b, head + 8),
            ground: read_u32(b, head + 12),
            veg: read_u32(b, head + 16),
            biome: read_u16(b, head + 20),
            mask: read_u16(b, head + 22),
            top: read_u16(b, head + 24),
            bottom: read_u16(b, head + 26),
        })
    }

    pub fn ring(&self, at: usize) -> Result<Ring> {
        if at >= self.n_rings() {
            return Err(Error::BadProgram);
        }
        let head = self.section[S_RINGS].at + at * RING_BYTES;
        Ok(Ring {
            at: read_u32(self.bytes, head),
            n: read_u32(self.bytes, head + 4),
            role: read_u32(self.bytes, head + 8),
        })
    }

    /// One point, in tiles. Checked ranges mean this is two loads and two
    /// multiplies, which matters: it is the most called thing in the library.
    #[inline]
    pub fn point(&self, at: usize) -> (f32, f32) {
        let head = self.section[S_POINTS].at + at * POINT_BYTES;
        let x = read_i32(self.bytes, head) as f32 * POINT_SCALE;
        let y = read_i32(self.bytes, head + 4) as f32 * POINT_SCALE;
        (x, y)
    }

    pub fn pass(&self, at: usize) -> Result<Pass> {
        if at >= self.n_passes() {
            return Err(Error::BadProgram);
        }
        let head = self.section[S_PASSES].at + at * PASS_BYTES;
        let b = self.bytes;
        Ok(Pass {
            kind: PassKind::from(b[head])?,
            over: b[head + 1],
            avoid: b[head + 2],
            radius: b[head + 3],
            chance: read_f32(b, head + 4),
            salt: read_u16(b, head + 8),
            spacing: read_u16(b, head + 10),
            pick_at: read_u32(b, head + 12),
            pick_n: read_u16(b, head + 16),
            flags: read_u16(b, head + 18),
            extra: read_u32(b, head + 20),
        })
    }

    pub fn pick(&self, at: usize) -> Pick {
        let head = self.section[S_PICKS].at + at * PICK_BYTES;
        let b = self.bytes;
        Pick {
            ground: read_u32(b, head),
            veg: read_u32(b, head + 4),
            biome: read_u16(b, head + 8),
            weight: read_u16(b, head + 10),
        }
    }

    /// A 300-tile cell this piece keeps, as cell coordinates.
    pub fn cell(&self, at: usize) -> (i32, i32) {
        let head = self.section[S_CELLS].at + at * CELL_BYTES;
        (
            read_i32(self.bytes, head),
            read_i32(self.bytes, head + 4),
        )
    }

    /// Whether a ground colour is in one of the sets a pass named. `ANY_SET`
    /// means the pass does not care.
    #[inline]
    pub fn in_set(&self, set: u8, colour: u32) -> bool {
        if set == ANY_SET {
            return true;
        }
        let (at, n) = match self.sets.get(set as usize) {
            Some(range) => *range,
            None => return false,
        };
        let head = self.set_colours_at + at * 4;
        for which in 0..n {
            if read_u32(self.bytes, head + which * 4) == colour {
                return true;
            }
        }
        false
    }

    pub fn string(&self, at: usize) -> &str {
        if at >= self.n_strings() {
            return "";
        }
        let head = self.section[S_STRING_INDEX].at + at * 8;
        let from = read_u32(self.bytes, head) as usize;
        let n = read_u32(self.bytes, head + 4) as usize;
        let at = self.section[S_STRINGS].at + from;
        std::str::from_utf8(&self.bytes[at..at + n]).unwrap_or("")
    }
}

// Safety: the buffer is read only for the whole life of the pool, and Python
// promises not to move or touch it until `knox_t_close`.
unsafe impl Send for Blob {}
unsafe impl Sync for Blob {}

#[inline]
fn read_u16(bytes: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([bytes[at], bytes[at + 1]])
}

#[inline]
fn read_u32(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]])
}

#[inline]
fn read_i32(bytes: &[u8], at: usize) -> i32 {
    read_u32(bytes, at) as i32
}

#[inline]
fn read_f32(bytes: &[u8], at: usize) -> f32 {
    f32::from_bits(read_u32(bytes, at))
}

#[inline]
fn read_u64(bytes: &[u8], at: usize) -> u64 {
    let mut eight = [0u8; 8];
    eight.copy_from_slice(&bytes[at..at + 8]);
    u64::from_le_bytes(eight)
}
