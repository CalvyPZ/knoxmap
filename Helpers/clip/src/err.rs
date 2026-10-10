//! What can go wrong cutting a selection, as numbers Python can act on.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u32)]
pub enum Error {
    /// A pointer was null, or a path was not text.
    BadState = 1,
    /// The selection box was not a finite rectangle.
    BadBox = 2,
    /// The span directory or the output directory could not be used.
    Io = 3,
    /// Something panicked. The output directory is left for Python to delete.
    Panic = 4,
    /// The caller asked the walk to stop. Nothing it wrote is published.
    Stopped = 5,
    /// The daily is not a PBF this crate can read. Python walks it instead.
    BadPbf = 6,
}

impl Error {
    pub fn code(self) -> u32 {
        self as u32
    }
}

/// Plain English for a code, nul-terminated and static.
pub fn text_for(code: u32) -> &'static [u8] {
    match code {
        1 => b"the clip library was called out of order\0",
        2 => b"the selection box is not usable\0",
        3 => b"the clip library could not read or write the area\0",
        4 => b"the clip library failed on this cut\0",
        5 => b"the clip library was stopped\0",
        6 => b"the daily could not be read\0",
        _ => b"the clip library returned an unknown error\0",
    }
}
