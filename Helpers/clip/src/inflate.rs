//! zlib inflate for OSM PBF blobs. The format is the DEFLATE stream inside a
//! zlib wrapper (RFC 1950 / 1951). No crates.io decoder: this is the subset
//! a Geofabrik daily uses.

use crate::err::Error;

const MAX_BITS: usize = 15;
const TABLE: usize = 1 << MAX_BITS;

const LEN_BASE: [u16; 29] = [
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115, 131,
    163, 195, 227, 258,
];
const LEN_EXTRA: [u8; 29] = [
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
];
const DIST_BASE: [u16; 30] = [
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
    2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
];
const DIST_EXTRA: [u8; 30] = [
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13,
    13,
];
const CL_ORDER: [usize; 19] = [
    16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
];

struct Huff {
    sym: Vec<u16>,
    len: Vec<u8>,
}

struct Bits<'a> {
    data: &'a [u8],
    pos: usize,
    acc: u32,
    nbits: u32,
}

impl<'a> Bits<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            pos: 0,
            acc: 0,
            nbits: 0,
        }
    }

    fn fill(&mut self, n: u32) {
        while self.nbits < n && self.pos < self.data.len() {
            self.acc |= (self.data[self.pos] as u32) << self.nbits;
            self.pos += 1;
            self.nbits += 8;
        }
    }

    fn pull(&mut self, n: u32) -> Result<u32, Error> {
        if n == 0 {
            return Ok(0);
        }
        self.fill(n);
        if self.nbits < n {
            return Err(Error::BadPbf);
        }
        let mask = (1u32 << n) - 1;
        let out = self.acc & mask;
        self.acc >>= n;
        self.nbits -= n;
        Ok(out)
    }

    fn align(&mut self) {
        let spare = self.nbits % 8;
        if spare != 0 {
            self.acc >>= spare;
            self.nbits -= spare;
        }
    }

    fn u16(&mut self) -> Result<u16, Error> {
        self.align();
        let lo = self.pull(8)?;
        let hi = self.pull(8)?;
        Ok((lo | (hi << 8)) as u16)
    }

    /// First byte not consumed by the bit reader, giving back whole bytes that
    /// were only pulled to fill the window.
    fn end(&self) -> usize {
        self.pos - (self.nbits as usize / 8)
    }
}

fn build(lengths: &[u8]) -> Result<Huff, Error> {
    let mut counts = [0u32; MAX_BITS + 1];
    for &len in lengths {
        if len as usize > MAX_BITS {
            return Err(Error::BadPbf);
        }
        counts[len as usize] += 1;
    }
    counts[0] = 0;
    let mut left = 1i32;
    for bits in 1..=MAX_BITS {
        left <<= 1;
        left -= counts[bits] as i32;
        if left < 0 {
            return Err(Error::BadPbf);
        }
    }
    let mut next = [0u32; MAX_BITS + 1];
    let mut code = 0u32;
    for bits in 1..=MAX_BITS {
        code = (code + counts[bits - 1]) << 1;
        next[bits] = code;
    }
    let mut sym = vec![0u16; TABLE];
    let mut len_tab = vec![0u8; TABLE];
    for (symbol, &len) in lengths.iter().enumerate() {
        if len == 0 {
            continue;
        }
        let canon = next[len as usize];
        next[len as usize] = canon + 1;
        let rev = reverse_low(canon, len);
        let step = 1usize << len;
        let mut fill = rev;
        while fill < TABLE {
            sym[fill] = symbol as u16;
            len_tab[fill] = len;
            fill += step;
        }
    }
    Ok(Huff { sym, len: len_tab })
}

fn reverse_low(code: u32, len: u8) -> usize {
    let mut c = code;
    let mut rev = 0usize;
    for _ in 0..len {
        rev = (rev << 1) | ((c & 1) as usize);
        c >>= 1;
    }
    rev
}

fn decode(huff: &Huff, bits: &mut Bits) -> Result<u16, Error> {
    bits.fill(MAX_BITS as u32);
    if bits.nbits == 0 {
        return Err(Error::BadPbf);
    }
    let peek = (bits.acc & ((1u32 << MAX_BITS) - 1)) as usize;
    let len = huff.len[peek];
    if len == 0 || len as u32 > bits.nbits {
        return Err(Error::BadPbf);
    }
    let symbol = huff.sym[peek];
    bits.acc >>= len;
    bits.nbits -= len as u32;
    Ok(symbol)
}

fn fixed_lits() -> Result<Huff, Error> {
    let mut lengths = vec![8u8; 288];
    for item in lengths.iter_mut().take(256).skip(144) {
        *item = 9;
    }
    for item in lengths.iter_mut().take(280).skip(256) {
        *item = 7;
    }
    build(&lengths)
}

fn fixed_dists() -> Result<Huff, Error> {
    build(&[5u8; 32])
}

fn dynamic(bits: &mut Bits) -> Result<(Huff, Huff), Error> {
    let nlit = bits.pull(5)? as usize + 257;
    let ndist = bits.pull(5)? as usize + 1;
    let nlen = bits.pull(4)? as usize + 4;
    if nlit > 286 || ndist > 30 {
        return Err(Error::BadPbf);
    }
    let mut cl_len = [0u8; 19];
    for i in 0..nlen {
        cl_len[CL_ORDER[i]] = bits.pull(3)? as u8;
    }
    let cl = build(&cl_len)?;
    let mut lengths = vec![0u8; nlit + ndist];
    let mut i = 0;
    while i < lengths.len() {
        let symbol = decode(&cl, bits)?;
        match symbol {
            0..=15 => {
                lengths[i] = symbol as u8;
                i += 1;
            }
            16 => {
                if i == 0 {
                    return Err(Error::BadPbf);
                }
                let prev = lengths[i - 1];
                let rep = bits.pull(2)? as usize + 3;
                if i + rep > lengths.len() {
                    return Err(Error::BadPbf);
                }
                for _ in 0..rep {
                    lengths[i] = prev;
                    i += 1;
                }
            }
            17 => {
                let rep = bits.pull(3)? as usize + 3;
                if i + rep > lengths.len() {
                    return Err(Error::BadPbf);
                }
                i += rep;
            }
            18 => {
                let rep = bits.pull(7)? as usize + 11;
                if i + rep > lengths.len() {
                    return Err(Error::BadPbf);
                }
                i += rep;
            }
            _ => return Err(Error::BadPbf),
        }
    }
    let lit = build(&lengths[..nlit])?;
    let dist = build(&lengths[nlit..])?;
    Ok((lit, dist))
}

fn block(bits: &mut Bits, out: &mut Vec<u8>, lit: &Huff, dist: &Huff) -> Result<(), Error> {
    loop {
        let symbol = decode(lit, bits)?;
        if symbol < 256 {
            out.push(symbol as u8);
            continue;
        }
        if symbol == 256 {
            return Ok(());
        }
        let li = (symbol as usize).wrapping_sub(257);
        if li >= LEN_BASE.len() {
            return Err(Error::BadPbf);
        }
        let length = LEN_BASE[li] as usize + bits.pull(LEN_EXTRA[li] as u32)? as usize;
        let di = decode(dist, bits)? as usize;
        if di >= DIST_BASE.len() {
            return Err(Error::BadPbf);
        }
        let distance = DIST_BASE[di] as usize + bits.pull(DIST_EXTRA[di] as u32)? as usize;
        if distance == 0 || distance > out.len() {
            return Err(Error::BadPbf);
        }
        for _ in 0..length {
            let byte = out[out.len() - distance];
            out.push(byte);
        }
    }
}

fn inflate_blocks(data: &[u8], out: &mut Vec<u8>) -> Result<usize, Error> {
    let mut bits = Bits::new(data);
    loop {
        let last = bits.pull(1)?;
        let kind = bits.pull(2)?;
        match kind {
            0 => {
                let len = bits.u16()? as usize;
                let nlen = bits.u16()? as usize;
                if len ^ 0xffff != nlen {
                    return Err(Error::BadPbf);
                }
                // The length words sit on a byte boundary. Bytes the window
                // already pulled past them are the start of the payload.
                let start = bits.end();
                if start + len > bits.data.len() {
                    return Err(Error::BadPbf);
                }
                out.extend_from_slice(&bits.data[start..start + len]);
                bits.pos = start + len;
                bits.acc = 0;
                bits.nbits = 0;
            }
            1 => {
                let lit = fixed_lits()?;
                let dist = fixed_dists()?;
                block(&mut bits, out, &lit, &dist)?;
            }
            2 => {
                let (lit, dist) = dynamic(&mut bits)?;
                block(&mut bits, out, &lit, &dist)?;
            }
            _ => return Err(Error::BadPbf),
        }
        if last == 1 {
            return Ok(bits.end());
        }
    }
}

fn adler32(data: &[u8]) -> u32 {
    let mut a: u32 = 1;
    let mut b: u32 = 0;
    for chunk in data.chunks(5552) {
        for &byte in chunk {
            a += byte as u32;
            b += a;
        }
        a %= 65521;
        b %= 65521;
    }
    (b << 16) | a
}

/// Inflate one zlib blob into `out`. `raw_size` is the size the PBF promised.
pub fn zlib(data: &[u8], raw_size: usize, out: &mut Vec<u8>) -> Result<(), Error> {
    if data.len() < 6 {
        return Err(Error::BadPbf);
    }
    let cmf = data[0] as u32;
    let flg = data[1] as u32;
    if (cmf * 256 + flg) % 31 != 0 {
        return Err(Error::BadPbf);
    }
    if cmf & 0x0f != 8 || (cmf >> 4) > 7 || flg & 0x20 != 0 {
        return Err(Error::BadPbf);
    }
    out.clear();
    if raw_size > 0 {
        out.reserve(raw_size);
    }
    let end = inflate_blocks(&data[2..], out)?;
    let tail = 2 + end;
    if tail + 4 > data.len() {
        return Err(Error::BadPbf);
    }
    let expect = u32::from_be_bytes(data[tail..tail + 4].try_into().unwrap());
    if adler32(out) != expect {
        return Err(Error::BadPbf);
    }
    if raw_size > 0 && out.len() != raw_size {
        return Err(Error::BadPbf);
    }
    Ok(())
}
