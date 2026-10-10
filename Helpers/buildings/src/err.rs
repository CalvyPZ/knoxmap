//! What can go wrong at the boundary, as numbers Python can act on.
//!
//! A building that cannot be laid out is not an error here: it comes back as
//! that building's record, with its reason, and the rest of the map carries
//! on. These are the failures that stop the whole call.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u32)]
pub enum Error {
    /// The buffer is shorter than its own header.
    Truncated = 1,
    /// Not a KnoxMap building buffer at all.
    BadMagic = 2,
    /// Written by a different version of the Python side than this DLL.
    BadAbi = 3,
    /// A section claims bytes that are not in the buffer.
    BadSection = 4,
    /// The counts in the header do not agree with the sections.
    BadCounts = 5,
    /// The catalog section is not in the shape catalog.rs expects.
    BadCatalog = 6,
    /// The operating system would not start a worker thread.

    /// A session pointer that is null, or a call made out of order.
    BadState = 8,
    /// Something panicked where it should not have. The call is given up on
    /// rather than left half done.
    Panic = 9,
}

pub type Result<T> = core::result::Result<T, Error>;

impl Error {
    pub fn code(self) -> u32 {
        self as u32
    }
}

/// Plain English for a code, for the message Python prints. Static bytes with
/// a terminator, so the pointer handed out stays valid for ever.
pub fn text_for(code: u32) -> &'static [u8] {
    match code {
        0 => b"no error\0",
        1 => b"the building buffer is truncated\0",
        2 => b"not a KnoxMap building buffer\0",
        3 => b"the building buffer was written for a different version of the DLL\0",
        4 => b"a section of the building buffer lies outside it\0",
        5 => b"the building buffer's counts do not agree\0",
        6 => b"the tile catalog in the buffer is not readable\0",
        7 => b"no worker threads could be started\0",
        8 => b"the building session is not in a state for that call\0",
        9 => b"the building DLL hit an internal error\0",
        _ => b"unknown error\0",
    }
}
