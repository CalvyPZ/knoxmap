//! One worker thread's slice of the pool.
//!
//! The whole point of this DLL is that memory does not grow with the number of
//! workers beyond what a worker is actually holding. So a worker allocates its
//! buffers once, on its first building, and reuses them for every building
//! after that: `clear()` keeps the capacity and drops the contents. A thread
//! settles at the size of the largest building it has seen, and peak memory is
//! the read-only buffer plus workers times that.
//!
//! Nothing here is shared. A `Scratch` is owned by one thread and is never
//! handed to another, which is why none of it needs a lock.

use crate::blob::PartyEdge;

pub struct Scratch {
    /// The building's footprint, one bool per tile, row-major.
    pub mask: Vec<bool>,
    /// Shared walls with the buildings next door.
    pub party: Vec<PartyEdge>,
    /// (front room, back room) for each trade the map recorded in this building.
    pub uses: Vec<(u32, u32)>,
    /// Room ids per tile for the storey being laid out, 1-based, 0 for outside.
    pub grid: Vec<i32>,
    /// Connected-component labels, for mending a room cut in two.
    pub labels: Vec<i32>,
    /// Summed-area table, for finding a clear rectangle in one read.
    pub sums: Vec<i32>,
    /// General integer workspace: queues and stacks for the flood fills.
    pub ints: Vec<i32>,
    /// General flag workspace: visited marks.
    pub flags: Vec<u8>,
    /// The .tbx text being built, written out in one call.
    pub text: String,
}

impl Scratch {
    pub fn new() -> Scratch {
        Scratch {
            mask: Vec::new(),
            party: Vec::new(),
            uses: Vec::new(),
            grid: Vec::new(),
            labels: Vec::new(),
            sums: Vec::new(),
            ints: Vec::new(),
            flags: Vec::new(),
            text: String::new(),
        }
    }

    /// Ready for the next building. Capacity is kept on purpose.
    pub fn reset(&mut self) {
        self.mask.clear();
        self.party.clear();
        self.uses.clear();
        self.grid.clear();
        self.labels.clear();
        self.sums.clear();
        self.ints.clear();
        self.flags.clear();
        self.text.clear();
    }

    /// Roughly what this thread is holding, for the memory report Python logs.
    pub fn bytes(&self) -> usize {
        self.mask.capacity()
            + self.party.capacity() * std::mem::size_of::<PartyEdge>()
            + self.uses.capacity() * 8
            + self.grid.capacity() * 4
            + self.labels.capacity() * 4
            + self.sums.capacity() * 4
            + self.ints.capacity() * 4
            + self.flags.capacity()
            + self.text.capacity()
    }
}

impl Default for Scratch {
    fn default() -> Scratch {
        Scratch::new()
    }
}
