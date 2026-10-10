//! The one buffer Python hands over, and the views the workers read it through.
//!
//! Everything needed to generate every building of every mod of a map arrives
//! in a single buffer: the tile catalog, the wall styles, the settings, the
//! footprint masks, the strings, and one fixed-size
//! record per building. The buffer is copied once into the session's pool and
//! is then read-only, so every worker thread reads the same bytes with no
//! locking and no second copy.
//!
//! Layout (little-endian throughout):
//!
//! ```text
//!   0   magic       "KNOXBLD1"            8 bytes
//!   8   abi         u32                   must equal ABI_VERSION
//!  12   n_jobs      u32
//!  16   sections    10 x (u64 off, u64 len)
//! ```
//!
//! A section offset is from the start of the buffer. The sections are, in
//! order: strings, string index, catalog, styles, settings, uses, masks,
//! pieces, jobs, job order.
//!
//! Shared walls are not in here. Which walls a building shares with the one
//! next door is worked out from the footprints themselves (party.rs): every
//! footprint of every piece is already in this buffer, and the Python side used
//! to build a map-sized array of tile owners to answer the same question.
//!
//! * strings      - UTF-8 bytes, all the labels and paths run together
//! * string index - u32 pairs (offset, length) into `strings`
//! * catalog      - the tile and furniture tables (see catalog.rs)
//! * styles       - wall/floor style records, de-duplicated by Python
//! * settings     - Settings records, de-duplicated by Python
//! * uses         - ground-floor use pairs, de-duplicated by Python
//! * masks        - footprint bitmaps, one bit per tile, rows padded to bytes
//! * pieces       - (width, height) of each piece of the map, as u32 pairs
//! * jobs         - `JOB_BYTES` per building (see `Job`)
//! * job order    - u32 job indexes, costliest first, so the pool hands out
//!                  the slowest buildings while every thread is still free
//!
//! Nothing in here is parsed into owned structures. `Blob::parse` checks that
//! every section lies inside the buffer and that the counts agree, and after
//! that a bad index is a bounds-checked slice, not a crash.

use crate::err::{Error, Result};

pub const MAGIC: &[u8; 8] = b"KNOXBLD1";
pub const ABI_VERSION: u32 = 1;

/// Bytes per job record. Fixed so a job is read without walking the table.
pub const JOB_BYTES: usize = 48;

const HEADER_FIXED: usize = 16;
const N_SECTIONS: usize = 10;
pub const HEADER_BYTES: usize = HEADER_FIXED + N_SECTIONS * 16;

/// Which building this is, which decides the room mix and the furniture.
/// Kept as a number because the workers compare it thousands of times; the
/// names are in `Kind::from_id`, matching knoxbuild/build.py's SPECIAL_BY_VALUE.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u16)]
pub enum Kind {
    House = 0,
    Apartment = 1,
    Retail = 2,
    Office = 3,
    Warehouse = 4,
    School = 5,
    Clinic = 6,
    Church = 7,
    Civic = 8,
    Mall = 9,
    Hotel = 10,
    Restaurant = 11,
    Bar = 12,
    Shop = 13,
    Bank = 14,
    Police = 15,
    Fire = 16,
    Army = 17,
    Library = 18,
    Factory = 19,
    Shed = 20,
    Garage = 21,
    Other = 0xFFFF,
}

impl Kind {
    pub fn from_id(id: u16) -> Kind {
        match id {
            0 => Kind::House,
            1 => Kind::Apartment,
            2 => Kind::Retail,
            3 => Kind::Office,
            4 => Kind::Warehouse,
            5 => Kind::School,
            6 => Kind::Clinic,
            7 => Kind::Church,
            8 => Kind::Civic,
            9 => Kind::Mall,
            10 => Kind::Hotel,
            11 => Kind::Restaurant,
            12 => Kind::Bar,
            13 => Kind::Shop,
            14 => Kind::Bank,
            15 => Kind::Police,
            16 => Kind::Fire,
            17 => Kind::Army,
            18 => Kind::Library,
            19 => Kind::Factory,
            20 => Kind::Shed,
            21 => Kind::Garage,
            _ => Kind::Other,
        }
    }
}

/// Which side of the building faces the street. `None` when the map does not
/// say, which is what the layout treats as "no front".
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Street {
    None,
    North,
    South,
    East,
    West,
}

impl Street {
    fn from_id(id: u8) -> Street {
        match id {
            1 => Street::North,
            2 => Street::South,
            3 => Street::East,
            4 => Street::West,
            _ => Street::None,
        }
    }
}

pub const JOB_COMMERCIAL: u8 = 1 << 0;
pub const JOB_RETAIL: u8 = 1 << 1;
pub const JOB_HOTEL: u8 = 1 << 2;
pub const JOB_HAS_MASK: u8 = 1 << 3;

/// One building to generate, as the worker sees it.
#[derive(Clone, Copy, Debug)]
pub struct Job {
    pub width: u16,
    pub height: u16,
    pub levels: u16,
    pub seed: u32,
    pub kind: Kind,
    pub style: u16,
    pub settings: u16,
    pub uses: u16,
    /// Into the string index: the building's name inside the .tbx.
    pub label: u32,
    /// Into the string index: where the .tbx is written.
    pub path: u32,
    /// Into `masks`, in bits. Only meaningful with JOB_HAS_MASK.
    pub mask_bit: u32,
    /// Which piece of the map this building is in. Only buildings of the same
    /// piece can share a wall.
    pub piece: u16,
    /// Where the building sits in its piece, in tiles. Used to find the
    /// buildings next door.
    pub x0: u16,
    pub y0: u16,
    pub street: Street,
    pub flags: u8,
}

impl Job {
    pub fn commercial(&self) -> bool {
        self.flags & JOB_COMMERCIAL != 0
    }
    pub fn retail(&self) -> bool {
        self.flags & JOB_RETAIL != 0
    }
    pub fn hotel(&self) -> bool {
        self.flags & JOB_HOTEL != 0
    }
    pub fn has_mask(&self) -> bool {
        self.flags & JOB_HAS_MASK != 0
    }
}

/// One stretch of wall shared with the building next door: no window, shop
/// front or door goes in it, up to the neighbour's height.
#[derive(Clone, Copy, Debug)]
pub struct PartyEdge {
    pub x: u16,
    pub y: u16,
    /// b'N' or b'W', the edge of the tile, as BuildingEd stores walls.
    pub side: u8,
    /// Storeys of the neighbour. The edge is shared below this level.
    pub up_to: u16,
}

#[derive(Clone, Copy)]
struct Section {
    off: usize,
    len: usize,
}

impl Section {
    fn slice<'a>(&self, all: &'a [u8]) -> &'a [u8] {
        &all[self.off..self.off + self.len]
    }
}

/// A checked view over the buffer. Cheap to copy; holds no owned memory.
pub struct Blob {
    bytes: Box<[u8]>,
    strings: Section,
    string_index: Section,
    catalog: Section,
    /// Reserved: a style travels as an index into the catalog's own table, so
    /// this slot carries nothing. It keeps its place so the header layout never
    /// shifts.
    #[allow(dead_code)]
    styles: Section,
    settings: Section,
    uses: Section,
    masks: Section,
    pieces: Section,
    jobs: Section,
    order: Section,
    n_jobs: usize,
}

fn u32_at(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes([bytes[at], bytes[at + 1], bytes[at + 2], bytes[at + 3]])
}

fn u16_at(bytes: &[u8], at: usize) -> u16 {
    u16::from_le_bytes([bytes[at], bytes[at + 1]])
}

fn u64_at(bytes: &[u8], at: usize) -> u64 {
    let mut raw = [0u8; 8];
    raw.copy_from_slice(&bytes[at..at + 8]);
    u64::from_le_bytes(raw)
}

impl Blob {
    /// Take the buffer over and check it. Every later read is in bounds
    /// because of what is checked here, so the workers never validate again.
    pub fn parse(bytes: Box<[u8]>) -> Result<Blob> {
        if bytes.len() < HEADER_BYTES {
            return Err(Error::Truncated);
        }
        if &bytes[0..8] != MAGIC {
            return Err(Error::BadMagic);
        }
        let abi = u32_at(&bytes, 8);
        if abi != ABI_VERSION {
            return Err(Error::BadAbi);
        }
        let n_jobs = u32_at(&bytes, 12) as usize;

        let mut sections = [Section { off: 0, len: 0 }; N_SECTIONS];
        for (i, section) in sections.iter_mut().enumerate() {
            let at = HEADER_FIXED + i * 16;
            let off = u64_at(&bytes, at);
            let len = u64_at(&bytes, at + 8);
            // Reject anything that does not lie wholly inside the buffer, so
            // that `Section::slice` can never be out of range.
            let end = off.checked_add(len).ok_or(Error::BadSection)?;
            if end > bytes.len() as u64 {
                return Err(Error::BadSection);
            }
            *section = Section {
                off: off as usize,
                len: len as usize,
            };
        }

        let blob = Blob {
            strings: sections[0],
            string_index: sections[1],
            catalog: sections[2],
            styles: sections[3],
            settings: sections[4],
            uses: sections[5],
            masks: sections[6],
            pieces: sections[7],
            jobs: sections[8],
            order: sections[9],
            n_jobs,
            bytes,
        };
        if blob.jobs.len < n_jobs * JOB_BYTES {
            return Err(Error::BadCounts);
        }
        if blob.string_index.len % 8 != 0 {
            return Err(Error::BadCounts);
        }
        if blob.pieces.len % 8 != 0 {
            return Err(Error::BadCounts);
        }
        // The order list is what the pool hands out. A short or long one would
        // silently skip or repeat buildings, so it has to be exactly right.
        if blob.order.len != n_jobs * 4 {
            return Err(Error::BadCounts);
        }
        for i in 0..n_jobs {
            if blob.order_at(i) as usize >= n_jobs {
                return Err(Error::BadCounts);
            }
        }
        Ok(blob)
    }

    pub fn n_jobs(&self) -> usize {
        self.n_jobs
    }

    pub fn catalog_bytes(&self) -> &[u8] {
        self.catalog.slice(&self.bytes)
    }

    /// The room-size setting this building was built with, in tiles of floor per
    /// room. None when the settings table does not reach that far, which is the
    /// defaults.
    pub fn room_size(&self, job: &Job) -> Option<f64> {
        let raw = self.settings.slice(&self.bytes);
        let at = job.settings as usize * 4;
        if at + 4 > raw.len() {
            return None;
        }
        let want = f32::from_le_bytes([raw[at], raw[at + 1], raw[at + 2], raw[at + 3]]);
        if want.is_finite() && want >= 1.0 {
            Some(want as f64)
        } else {
            None
        }
    }

    /// What the map says the ground floor of this building is: (front room,
    /// back room) as 1-based catalog rooms, in the order they were found along
    /// the street. Empty for a building nobody recorded a trade for.
    ///
    /// The section is one (offset, count) pair per building, then the pairs
    /// themselves, so a building's uses are read without walking the table.
    pub fn uses_into(&self, index: usize, out: &mut Vec<(u32, u32)>) {
        out.clear();
        let raw = self.uses.slice(&self.bytes);
        let head = self.n_jobs * 8;
        if index * 8 + 8 > head || head > raw.len() {
            return;
        }
        let off = u32_at(raw, index * 8) as usize;
        let count = u32_at(raw, index * 8 + 4) as usize;
        for i in 0..count {
            let at = head + (off + i) * 8;
            if at + 8 > raw.len() {
                return;
            }
            out.push((u32_at(raw, at), u32_at(raw, at + 4)));
        }
    }

    /// How much of the buffer is held, for the memory report.
    pub fn byte_len(&self) -> usize {
        self.bytes.len()
    }

    /// The `i`th job of the order list: the costliest building first.
    pub fn order_at(&self, i: usize) -> u32 {
        let raw = self.order.slice(&self.bytes);
        u32_at(raw, i * 4)
    }

    pub fn job(&self, index: usize) -> Job {
        let raw = self.jobs.slice(&self.bytes);
        let at = index * JOB_BYTES;
        Job {
            width: u16_at(raw, at),
            height: u16_at(raw, at + 2),
            levels: u16_at(raw, at + 4),
            kind: Kind::from_id(u16_at(raw, at + 6)),
            seed: u32_at(raw, at + 8),
            style: u16_at(raw, at + 12),
            settings: u16_at(raw, at + 14),
            uses: u16_at(raw, at + 16),
            piece: u16_at(raw, at + 18),
            label: u32_at(raw, at + 20),
            path: u32_at(raw, at + 24),
            mask_bit: u32_at(raw, at + 28),
            x0: u16_at(raw, at + 32),
            y0: u16_at(raw, at + 34),
            street: Street::from_id(raw[at + 36]),
            flags: raw[at + 37],
        }
    }

    /// One string from the table, or "" when the index is out of range (which
    /// `parse` cannot rule out without walking every record).
    pub fn string(&self, id: u32) -> &str {
        let index = self.string_index.slice(&self.bytes);
        let at = id as usize * 8;
        if at + 8 > index.len() {
            return "";
        }
        let off = u32_at(index, at) as usize;
        let len = u32_at(index, at + 4) as usize;
        let strings = self.strings.slice(&self.bytes);
        if off + len > strings.len() {
            return "";
        }
        // Python writes UTF-8. A label that somehow is not stays readable
        // rather than failing a building that is otherwise fine.
        std::str::from_utf8(&strings[off..off + len]).unwrap_or("")
    }

    /// The footprint of a building, one bool per tile, row-major. Empty when
    /// the building fills its whole rectangle.
    pub fn mask_into(&self, job: &Job, out: &mut Vec<bool>) {
        out.clear();
        if !job.has_mask() {
            return;
        }
        let w = job.width as usize;
        let h = job.height as usize;
        out.reserve(w * h);
        let masks = self.masks.slice(&self.bytes);
        // Rows are padded to a whole byte so a row starts on a byte boundary,
        // which is what makes a single building's mask copyable by Python.
        let row_bytes = (w + 7) / 8;
        let start = job.mask_bit as usize / 8;
        for y in 0..h {
            for x in 0..w {
                let at = start + y * row_bytes + x / 8;
                let bit = if at < masks.len() {
                    masks[at] >> (x % 8) & 1
                } else {
                    0
                };
                out.push(bit != 0);
            }
        }
    }

    /// Is this tile of that building inside its footprint? Used to find the
    /// walls two buildings share, so it is asked once per boundary tile.
    pub fn mask_at(&self, job: &Job, lx: i32, ly: i32) -> bool {
        if lx < 0 || ly < 0 || lx >= job.width as i32 || ly >= job.height as i32 {
            return false;
        }
        if !job.has_mask() {
            return true;
        }
        let masks = self.masks.slice(&self.bytes);
        let row_bytes = (job.width as usize + 7) / 8;
        let at = job.mask_bit as usize / 8 + ly as usize * row_bytes + lx as usize / 8;
        match masks.get(at) {
            Some(byte) => byte >> (lx as usize % 8) & 1 != 0,
            None => false,
        }
    }
}
