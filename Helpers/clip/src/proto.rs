//! The protobuf wire types an OSM PBF uses. Fields this reader does not know
//! are skipped, so a newer optional field does not make the daily unreadable.

use crate::err::Error;

pub struct Cursor<'a> {
    data: &'a [u8],
    i: usize,
}

impl<'a> Cursor<'a> {
    pub fn new(data: &'a [u8]) -> Self {
        Self { data, i: 0 }
    }

    pub fn done(&self) -> bool {
        self.i >= self.data.len()
    }

    pub fn varint(&mut self) -> Result<u64, Error> {
        let mut out = 0u64;
        let mut shift = 0;
        for _ in 0..10 {
            if self.i >= self.data.len() {
                return Err(Error::BadPbf);
            }
            let byte = self.data[self.i];
            self.i += 1;
            out |= ((byte & 0x7f) as u64) << shift;
            if byte & 0x80 == 0 {
                return Ok(out);
            }
            shift += 7;
        }
        Err(Error::BadPbf)
    }

    /// The next field number and wire type, or None at the end of the message.
    pub fn next(&mut self) -> Result<Option<(u32, u32)>, Error> {
        if self.i >= self.data.len() {
            return Ok(None);
        }
        let key = self.varint()?;
        Ok(Some(((key >> 3) as u32, (key & 7) as u32)))
    }

    pub fn skip(&mut self, wire: u32) -> Result<(), Error> {
        match wire {
            0 => {
                self.varint()?;
            }
            1 => self.advance(8)?,
            2 => {
                let n = self.varint()? as usize;
                self.advance(n)?;
            }
            5 => self.advance(4)?,
            _ => return Err(Error::BadPbf),
        }
        Ok(())
    }

    pub fn bytes(&mut self) -> Result<&'a [u8], Error> {
        let n = self.varint()? as usize;
        if self.i + n > self.data.len() {
            return Err(Error::BadPbf);
        }
        let out = &self.data[self.i..self.i + n];
        self.i += n;
        Ok(out)
    }

    fn advance(&mut self, n: usize) -> Result<(), Error> {
        if self.i + n > self.data.len() {
            return Err(Error::BadPbf);
        }
        self.i += n;
        Ok(())
    }
}

pub fn zigzag(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

/// Packed varints, or one varint when the field was not packed.
pub fn take_varints(wire: u32, cur: &mut Cursor, out: &mut Vec<u64>) -> Result<(), Error> {
    out.clear();
    if wire == 2 {
        let raw = cur.bytes()?;
        let mut inner = Cursor::new(raw);
        while !inner.done() {
            out.push(inner.varint()?);
        }
        Ok(())
    } else if wire == 0 {
        out.push(cur.varint()?);
        Ok(())
    } else {
        Err(Error::BadPbf)
    }
}
