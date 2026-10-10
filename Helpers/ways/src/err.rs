//! What can go wrong at the boundary, as numbers Python can act on.
//!
//! A way that misses the selection is not an error. It comes back as that
//! way's answer code, and the read carries on. These are the failures that
//! stop the call itself.

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
#[repr(u32)]
pub enum Error {
    /// A session pointer that is null, or a call made after close.
    BadState = 1,
    /// The selection or the region box was not a finite rectangle.
    BadBox = 2,
    /// Something panicked where it should not have. The call is given up on
    /// rather than left half done.
    Panic = 3,
}

impl Error {
    pub fn code(self) -> u32 {
        self as u32
    }
}

/// Plain English for a code, nul-terminated and static.
pub fn text_for(code: u32) -> &'static [u8] {
    match code {
        1 => b"the way library was called out of order\0",
        2 => b"the selection box is not usable\0",
        3 => b"the way library failed on this call\0",
        _ => b"the way library returned an unknown error\0",
    }
}
