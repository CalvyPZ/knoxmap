//! One pass over an `.osm.pbf`. Each blob is inflated and handed to the caller
//! as the OSMHeader or OSMData message. The file is read, not loaded.

use std::fs::File;
use std::io::{ErrorKind, Read, Seek};
use std::path::Path;

use crate::err::Error;
use crate::inflate;
use crate::proto::Cursor;

const MAX_HEADER: usize = 64 * 1024;
const MAX_BLOB: usize = 64 * 1024 * 1024;
const MAX_PLAIN: usize = 128 * 1024 * 1024;

/// How far a daily walk has got. Python reads this from another thread.
/// `phase` 1 is the harbour pass, 2 is the roads, 3 is the cut.
#[repr(C)]
pub struct WalkNote {
    pub phase: u32,
    pub done: u64,
    pub total: u64,
}

pub fn publish(note: *mut WalkNote, phase: u32, done: u64, total: u64) {
    if note.is_null() {
        return;
    }
    unsafe {
        let note = &mut *note;
        std::ptr::write_volatile(&mut note.phase, phase);
        std::ptr::write_volatile(&mut note.done, done);
        std::ptr::write_volatile(&mut note.total, total);
    }
}

pub fn for_each_blob(
    path: &Path,
    stop: *const u32,
    note: *mut WalkNote,
    phase: u32,
    mut on_blob: impl FnMut(&str, &[u8]) -> Result<(), Error>,
) -> Result<(), Error> {
    let mut file = File::open(path).map_err(|_| Error::Io)?;
    let total = file.metadata().map(|meta| meta.len()).unwrap_or(0);
    publish(note, phase, 0, total);
    let mut compressed = Vec::new();
    let mut plain = Vec::new();
    loop {
        check_stop(stop)?;
        let done = file.stream_position().unwrap_or(0);
        publish(note, phase, done, total);
        let mut len_buf = [0u8; 4];
        if !read_full(&mut file, &mut len_buf)? {
            return Ok(());
        }
        let header_len = u32::from_be_bytes(len_buf) as usize;
        if header_len == 0 || header_len > MAX_HEADER {
            return Err(Error::BadPbf);
        }
        let mut header = vec![0u8; header_len];
        if !read_full(&mut file, &mut header)? {
            return Err(Error::BadPbf);
        }
        let (kind, datasize) = blob_header(&header)?;
        if datasize == 0 || datasize > MAX_BLOB {
            return Err(Error::BadPbf);
        }
        compressed.resize(datasize, 0);
        if !read_full(&mut file, &mut compressed)? {
            return Err(Error::BadPbf);
        }
        decode_blob(&compressed, &mut plain)?;
        on_blob(&kind, &plain)?;
    }
}

pub fn check_stop(stop: *const u32) -> Result<(), Error> {
    if stop.is_null() {
        return Ok(());
    }
    // The flag is a plain integer Python stores from another thread. A 32-bit
    // aligned read is the signal; it is not a lock.
    let flag = unsafe { std::ptr::read_volatile(stop) };
    if flag != 0 {
        return Err(Error::Stopped);
    }
    Ok(())
}

fn read_full(file: &mut File, buf: &mut [u8]) -> Result<bool, Error> {
    let mut got = 0;
    while got < buf.len() {
        match file.read(&mut buf[got..]) {
            Ok(0) if got == 0 => return Ok(false),
            Ok(0) => return Err(Error::BadPbf),
            Ok(n) => got += n,
            Err(err) if err.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return Err(Error::Io),
        }
    }
    Ok(true)
}

fn blob_header(data: &[u8]) -> Result<(String, usize), Error> {
    let mut cur = Cursor::new(data);
    let mut kind = String::new();
    let mut datasize = 0usize;
    while let Some((field, wire)) = cur.next()? {
        match (field, wire) {
            (1, 2) => {
                let raw = cur.bytes()?;
                kind = std::str::from_utf8(raw).map_err(|_| Error::BadPbf)?.to_string();
            }
            (3, 0) => datasize = cur.varint()? as usize,
            _ => cur.skip(wire)?,
        }
    }
    if kind.is_empty() || datasize == 0 {
        return Err(Error::BadPbf);
    }
    Ok((kind, datasize))
}

fn decode_blob(data: &[u8], plain: &mut Vec<u8>) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    let mut raw: &[u8] = &[];
    let mut zlib: &[u8] = &[];
    let mut raw_size = 0usize;
    let mut other = false;
    while let Some((field, wire)) = cur.next()? {
        match (field, wire) {
            (1, 2) => raw = cur.bytes()?,
            (2, 0) => raw_size = cur.varint()? as usize,
            (3, 2) => zlib = cur.bytes()?,
            (4, 2) | (5, 2) | (6, 2) | (7, 2) => {
                other = true;
                cur.skip(wire)?;
            }
            _ => cur.skip(wire)?,
        }
    }
    if raw_size > MAX_PLAIN {
        return Err(Error::BadPbf);
    }
    if !zlib.is_empty() {
        inflate::zlib(zlib, raw_size, plain)?;
        return Ok(());
    }
    if other {
        return Err(Error::BadPbf);
    }
    if raw.is_empty() {
        return Err(Error::BadPbf);
    }
    if raw_size > 0 && raw.len() != raw_size {
        return Err(Error::BadPbf);
    }
    plain.clear();
    plain.extend_from_slice(raw);
    Ok(())
}
