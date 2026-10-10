//! What can go wrong at the boundary, as a code Python can print.
//!
//! Every error here is about the buffer or the session, not about one piece of
//! ground: a piece that fails comes back as that piece failing, with its own
//! reason, and the rest of the map still gets drawn.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    /// The buffer is shorter than the header says it is.
    Truncated = 1,
    /// Not a terrain buffer at all.
    BadMagic = 2,
    /// A buffer written by another build of the program.
    BadAbi = 3,
    /// A section length that is not a whole number of records, or a count that
    /// does not agree with the section it indexes.
    BadCounts = 4,
    /// A command, ring or piece record that points outside its table.
    BadProgram = 5,
    /// The session is not in a state where this call means anything: started
    /// twice, or asked for results while the threads are still running.
    BadState = 6,
    /// A panic was caught. The work is abandoned; nothing unwinds into Python.
    Panic = 7,
    /// A piece asks for more pixels than this build can address.
    TooBig = 8,
    /// A file would not open, or the disc would not take it.
    Write = 9,
    /// Cancelled from Python while the threads were working.
    Cancelled = 10,
}

impl Error {
    pub fn code(self) -> u32 {
        self as u32
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// Plain English for a code. Nul-terminated, static, so the pointer Python
/// holds stays valid for the life of the program.
pub fn text_for(code: u32) -> &'static str {
    match code {
        0 => "no error\0",
        1 => "the terrain buffer is shorter than its header says\0",
        2 => "that is not a terrain buffer\0",
        3 => "the terrain buffer was written for another version of the library\0",
        4 => "the terrain buffer's tables do not agree with its header\0",
        5 => "the terrain buffer has a command that points outside its tables\0",
        6 => "the terrain library was asked for that at the wrong time\0",
        7 => "the terrain library hit an internal error\0",
        8 => "that piece of ground is larger than the library can address\0",
        9 => "a map file could not be written\0",
        10 => "the terrain work was cancelled\0",
        _ => "the terrain library gave an error code this program does not know\0",
    }
}
