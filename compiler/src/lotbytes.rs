// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// On-disk contract: LotFilesWorker256::SaveString, generateHeaderAux,
// generateChunk, and Navigate::ChunkDataFile256 (little-endian lots,
// big-endian chunkdata). A later diff against PZWorldEd_cli is the check;
// this module does not invent a second layout.

//! Read and write `.lotheader`, `.lotpack`, and `chunkdata_*.bin`.

use std::io::{Read, Write};

pub const VERSION: i32 = 1;
pub const CELL: i32 = 256;
pub const CHUNKS: i32 = 32;
pub const CHUNK: i32 = 8;
pub const CHUNK_COUNT: usize = 1024;

pub const MIN_WORLD_LEVEL: i32 = -32;
pub const MAX_WORLD_LEVELS: i32 = 64;

pub const BIT_SOLID: u8 = 1;
pub const BIT_WALLN: u8 = 2;
pub const BIT_WALLW: u8 = 4;
pub const BIT_WATER: u8 = 8;
pub const BIT_ROOM: u8 = 16;
pub const BIT_NULL: u8 = 32;

pub const EMPTY_CHUNK: u8 = 0;
pub const SOLID_CHUNK: u8 = 1;
pub const REGULAR_CHUNK: u8 = 2;
pub const WATER_CHUNK: u8 = 3;
pub const ROOM_CHUNK: u8 = 4;
pub const NULL_CHUNK: u8 = 5;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoomObject {
    pub meta_enum: i32,
    pub x: i32,
    pub y: i32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RoomRect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Room {
    pub name: String,
    pub floor: i32,
    pub rects: Vec<RoomRect>,
    pub objects: Vec<RoomObject>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LotHeader {
    pub version: i32,
    pub tiles: Vec<String>,
    pub chunk_w: i32,
    pub chunk_h: i32,
    pub min_level: i32,
    pub max_level: i32,
    pub rooms: Vec<Room>,
    /// Each building is the list of room ids, in WorldEd's room-list order.
    pub buildings: Vec<Vec<i32>>,
    /// 1024 intensity bytes, x outer, y inner, matching generateHeaderAux.
    pub zombies: Vec<u8>,
}

/// One non-empty square in a chunk body. Tile ids index `LotHeader::tiles`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PackedSquare {
    pub room: i32,
    pub tiles: Vec<i32>,
}

/// A chunk body, or the shared "absent" blob used by partial exports.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChunkPayload {
    /// Squares in order z, then x, then y. `None` is an empty square.
    Squares(Vec<Option<PackedSquare>>),
    /// The shared absent record: (-1, skip) covering every square.
    Absent { squares: i32 },
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LotPack {
    pub version: i32,
    pub chunks: Vec<ChunkPayload>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ChunkData {
    Uniform(u8),
    Regular([u8; 64]),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChunkDataFile {
    pub version: i16,
    pub chunks: Vec<ChunkData>,
}

pub fn save_string(out: &mut Vec<u8>, text: &str) {
    for ch in text.chars() {
        let latin = if (ch as u32) <= 0xFF { ch as u8 } else { 0 };
        if latin == b'\n' {
            continue;
        }
        out.push(latin);
    }
    out.push(b'\n');
}

fn read_string(data: &[u8], pos: &mut usize) -> Result<String, String> {
    let start = *pos;
    let end = data[*pos..]
        .iter()
        .position(|b| *b == b'\n')
        .ok_or_else(|| "lot string is missing its newline".to_string())?;
    *pos = start + end + 1;
    Ok(String::from_utf8_lossy(&data[start..start + end]).into_owned())
}

fn need(data: &[u8], pos: usize, n: usize) -> Result<(), String> {
    if pos + n > data.len() {
        Err(format!(
            "lot file ended at {pos} (need {n} more bytes, have {})",
            data.len()
        ))
    } else {
        Ok(())
    }
}

fn i32_le(data: &[u8], pos: &mut usize) -> Result<i32, String> {
    need(data, *pos, 4)?;
    let v = i32::from_le_bytes(data[*pos..*pos + 4].try_into().unwrap());
    *pos += 4;
    Ok(v)
}

fn i64_le(data: &[u8], pos: &mut usize) -> Result<i64, String> {
    need(data, *pos, 8)?;
    let v = i64::from_le_bytes(data[*pos..*pos + 8].try_into().unwrap());
    *pos += 8;
    Ok(v)
}

fn push_i32(out: &mut Vec<u8>, v: i32) {
    out.extend_from_slice(&v.to_le_bytes());
}

fn push_i64(out: &mut Vec<u8>, v: i64) {
    out.extend_from_slice(&v.to_le_bytes());
}

pub fn write_header(header: &LotHeader) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(b"LOTH");
    push_i32(&mut out, header.version);
    push_i32(&mut out, header.tiles.len() as i32);
    for name in &header.tiles {
        save_string(&mut out, name);
    }
    push_i32(&mut out, header.chunk_w);
    push_i32(&mut out, header.chunk_h);
    push_i32(&mut out, header.min_level);
    push_i32(&mut out, header.max_level);
    push_i32(&mut out, header.rooms.len() as i32);
    for room in &header.rooms {
        save_string(&mut out, &room.name);
        push_i32(&mut out, room.floor);
        push_i32(&mut out, room.rects.len() as i32);
        for rect in &room.rects {
            push_i32(&mut out, rect.x);
            push_i32(&mut out, rect.y);
            push_i32(&mut out, rect.w);
            push_i32(&mut out, rect.h);
        }
        push_i32(&mut out, room.objects.len() as i32);
        for object in &room.objects {
            push_i32(&mut out, object.meta_enum);
            push_i32(&mut out, object.x);
            push_i32(&mut out, object.y);
        }
    }
    push_i32(&mut out, header.buildings.len() as i32);
    for building in &header.buildings {
        push_i32(&mut out, building.len() as i32);
        for id in building {
            push_i32(&mut out, *id);
        }
    }
    out.extend_from_slice(&header.zombies);
    out
}

pub fn read_header(data: &[u8]) -> Result<LotHeader, String> {
    let mut pos = if data.starts_with(b"LOTH") { 4 } else { 0 };
    let version = i32_le(data, &mut pos)?;
    let count = i32_le(data, &mut pos)?;
    if count < 0 {
        return Err(format!("negative tile count {count}"));
    }
    let mut tiles = Vec::with_capacity(count as usize);
    for _ in 0..count {
        tiles.push(read_string(data, &mut pos)?);
    }
    let chunk_w = i32_le(data, &mut pos)?;
    let chunk_h = i32_le(data, &mut pos)?;
    let min_level = i32_le(data, &mut pos)?;
    let max_level = i32_le(data, &mut pos)?;
    let room_count = i32_le(data, &mut pos)?;
    if room_count < 0 {
        return Err(format!("negative room count {room_count}"));
    }
    let mut rooms = Vec::with_capacity(room_count as usize);
    for _ in 0..room_count {
        let name = read_string(data, &mut pos)?;
        let floor = i32_le(data, &mut pos)?;
        let nrect = i32_le(data, &mut pos)?;
        if nrect < 0 {
            return Err(format!("negative room rect count {nrect}"));
        }
        let mut rects = Vec::with_capacity(nrect as usize);
        for _ in 0..nrect {
            rects.push(RoomRect {
                x: i32_le(data, &mut pos)?,
                y: i32_le(data, &mut pos)?,
                w: i32_le(data, &mut pos)?,
                h: i32_le(data, &mut pos)?,
            });
        }
        let nobj = i32_le(data, &mut pos)?;
        if nobj < 0 {
            return Err(format!("negative room object count {nobj}"));
        }
        let mut objects = Vec::with_capacity(nobj as usize);
        for _ in 0..nobj {
            objects.push(RoomObject {
                meta_enum: i32_le(data, &mut pos)?,
                x: i32_le(data, &mut pos)?,
                y: i32_le(data, &mut pos)?,
            });
        }
        rooms.push(Room {
            name,
            floor,
            rects,
            objects,
        });
    }
    let nbuild = i32_le(data, &mut pos)?;
    if nbuild < 0 {
        return Err(format!("negative building count {nbuild}"));
    }
    let mut buildings = Vec::with_capacity(nbuild as usize);
    for _ in 0..nbuild {
        let n = i32_le(data, &mut pos)?;
        if n < 0 {
            return Err(format!("negative building room count {n}"));
        }
        let mut ids = Vec::with_capacity(n as usize);
        for _ in 0..n {
            ids.push(i32_le(data, &mut pos)?);
        }
        buildings.push(ids);
    }
    need(data, pos, CHUNK_COUNT)?;
    let zombies = data[pos..pos + CHUNK_COUNT].to_vec();
    Ok(LotHeader {
        version,
        tiles,
        chunk_w,
        chunk_h,
        min_level,
        max_level,
        rooms,
        buildings,
        zombies,
    })
}

/// Encode one chunk the way `generateChunk` does: z, then x, then y.
pub fn encode_squares(squares: &[Option<PackedSquare>]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut empty = 0i32;
    for square in squares {
        match square {
            None => empty += 1,
            Some(square) => {
                if empty > 0 {
                    push_i32(&mut out, -1);
                    push_i32(&mut out, empty);
                    empty = 0;
                }
                push_i32(&mut out, square.tiles.len() as i32 + 1);
                push_i32(&mut out, square.room);
                for id in &square.tiles {
                    push_i32(&mut out, *id);
                }
            }
        }
    }
    if empty > 0 {
        push_i32(&mut out, -1);
        push_i32(&mut out, empty);
    }
    out
}

pub fn decode_squares(data: &[u8], pos: &mut usize, count: usize) -> Result<Vec<Option<PackedSquare>>, String> {
    let mut out = Vec::with_capacity(count);
    let mut skip = 0i32;
    while out.len() < count {
        if skip > 0 {
            skip -= 1;
            out.push(None);
            continue;
        }
        let n = i32_le(data, pos)?;
        if n == -1 {
            skip = i32_le(data, pos)?;
            if skip <= 0 {
                return Err(format!("empty run length {skip}"));
            }
            skip -= 1;
            out.push(None);
            continue;
        }
        if n < 1 {
            return Err(format!("tile count field {n}"));
        }
        let room = i32_le(data, pos)?;
        let ntiles = (n - 1) as usize;
        let mut tiles = Vec::with_capacity(ntiles);
        for _ in 0..ntiles {
            tiles.push(i32_le(data, pos)?);
        }
        out.push(Some(PackedSquare { room, tiles }));
    }
    Ok(out)
}

pub fn write_pack(pack: &LotPack) -> Result<Vec<u8>, String> {
    if pack.chunks.len() != CHUNK_COUNT {
        return Err(format!(
            "lotpack needs {CHUNK_COUNT} chunks, got {}",
            pack.chunks.len()
        ));
    }
    let header_len = 4 + 4 + 4 + CHUNK_COUNT * 8;
    let mut body = Vec::new();
    let mut offsets = vec![0i64; CHUNK_COUNT];
    let absent_squares = pack.chunks.iter().find_map(|chunk| match chunk {
        ChunkPayload::Absent { squares } => Some(*squares),
        ChunkPayload::Squares(_) => None,
    });
    let absent_at = if let Some(squares) = absent_squares {
        let at = header_len as i64;
        push_i32(&mut body, -1);
        push_i32(&mut body, squares);
        Some(at)
    } else {
        None
    };
    for (i, chunk) in pack.chunks.iter().enumerate() {
        match chunk {
            ChunkPayload::Absent { .. } => offsets[i] = absent_at.expect("absent offset"),
            ChunkPayload::Squares(squares) => {
                offsets[i] = (header_len + body.len()) as i64;
                body.extend(encode_squares(squares));
            }
        }
    }
    let mut out = Vec::with_capacity(header_len + body.len());
    out.extend_from_slice(b"LOTP");
    push_i32(&mut out, pack.version);
    push_i32(&mut out, CHUNK_COUNT as i32);
    for offset in offsets {
        push_i64(&mut out, offset);
    }
    out.extend(body);
    Ok(out)
}

pub fn read_pack(data: &[u8], levels: i32) -> Result<LotPack, String> {
    let mut pos = if data.starts_with(b"LOTP") { 4 } else { 0 };
    let version = i32_le(data, &mut pos)?;
    let n = i32_le(data, &mut pos)?;
    if n != CHUNK_COUNT as i32 {
        return Err(format!("lotpack chunk count {n}, expected {CHUNK_COUNT}"));
    }
    let mut offsets = Vec::with_capacity(CHUNK_COUNT);
    for _ in 0..CHUNK_COUNT {
        offsets.push(i64_le(data, &mut pos)?);
    }
    let per_chunk = (CHUNK * CHUNK * levels.max(0)) as usize;
    let mut chunks = Vec::with_capacity(CHUNK_COUNT);
    let mut seen: Vec<(i64, ChunkPayload)> = Vec::new();
    for offset in offsets {
        if offset < 0 {
            return Err(format!("negative chunk offset {offset}"));
        }
        if let Some((_, payload)) = seen.iter().find(|(at, _)| *at == offset) {
            chunks.push(payload.clone());
            continue;
        }
        let mut p = offset as usize;
        let probe = i32_le(data, &mut p)?;
        if probe == -1 {
            let squares = i32_le(data, &mut p)?;
            let payload = ChunkPayload::Absent { squares };
            seen.push((offset, payload.clone()));
            chunks.push(payload);
        } else {
            let mut p = offset as usize;
            let squares = decode_squares(data, &mut p, per_chunk)?;
            let payload = ChunkPayload::Squares(squares);
            seen.push((offset, payload.clone()));
            chunks.push(payload);
        }
    }
    Ok(LotPack { version, chunks })
}

pub fn write_chunkdata(file: &ChunkDataFile) -> Result<Vec<u8>, String> {
    if file.chunks.len() != CHUNK_COUNT {
        return Err(format!(
            "chunkdata needs {CHUNK_COUNT} chunks, got {}",
            file.chunks.len()
        ));
    }
    let mut out = Vec::new();
    out.extend_from_slice(&file.version.to_be_bytes());
    for chunk in &file.chunks {
        match chunk {
            ChunkData::Uniform(kind) => out.push(*kind),
            ChunkData::Regular(bits) => {
                out.push(REGULAR_CHUNK);
                out.extend_from_slice(bits);
            }
        }
    }
    Ok(out)
}

pub fn read_chunkdata(data: &[u8]) -> Result<ChunkDataFile, String> {
    if data.len() < 2 {
        return Err("chunkdata is shorter than its version".into());
    }
    let version = i16::from_be_bytes([data[0], data[1]]);
    let mut pos = 2;
    let mut chunks = Vec::with_capacity(CHUNK_COUNT);
    for _ in 0..CHUNK_COUNT {
        need(data, pos, 1)?;
        let kind = data[pos];
        pos += 1;
        if kind == REGULAR_CHUNK {
            need(data, pos, 64)?;
            let mut bits = [0u8; 64];
            bits.copy_from_slice(&data[pos..pos + 64]);
            pos += 64;
            chunks.push(ChunkData::Regular(bits));
        } else {
            chunks.push(ChunkData::Uniform(kind));
        }
    }
    Ok(ChunkDataFile { version, chunks })
}

pub fn classify_chunk(bits: &[u8; 64]) -> ChunkData {
    let mut empty = 0;
    let mut solid = 0;
    let mut water = 0;
    let mut room = 0;
    let mut nulls = 0;
    for bit in bits {
        if *bit == 0 {
            empty += 1;
        } else if *bit == BIT_SOLID {
            solid += 1;
        } else if *bit == BIT_WATER {
            water += 1;
        } else if *bit == BIT_ROOM {
            room += 1;
        } else if *bit == BIT_NULL {
            nulls += 1;
        }
    }
    if empty == 64 {
        ChunkData::Uniform(EMPTY_CHUNK)
    } else if solid == 64 {
        ChunkData::Uniform(SOLID_CHUNK)
    } else if water == 64 {
        ChunkData::Uniform(WATER_CHUNK)
    } else if room == 64 {
        ChunkData::Uniform(ROOM_CHUNK)
    } else if nulls == 64 {
        ChunkData::Uniform(NULL_CHUNK)
    } else {
        ChunkData::Regular(*bits)
    }
}

/// Byte diff. Lot files compare raw. TMX/XML also reports whether the
/// uncompressed base64 payloads match when the deflate wrapper does not.
pub fn diff_bytes(left: &[u8], right: &[u8]) -> Option<String> {
    if left == right {
        return None;
    }
    let n = left.len().min(right.len());
    let mut at = 0;
    while at < n && left[at] == right[at] {
        at += 1;
    }
    Some(format!(
        "differ at byte {at} (lengths {} and {})",
        left.len(),
        right.len()
    ))
}

pub fn roundtrip_header(data: &[u8]) -> Result<bool, String> {
    let header = read_header(data)?;
    Ok(write_header(&header) == data)
}

pub fn inflate_zlib(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut dec = flate2::read::ZlibDecoder::new(data);
    let mut out = Vec::new();
    dec.read_to_end(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

pub fn deflate_zlib(data: &[u8]) -> Result<Vec<u8>, String> {
    use flate2::write::ZlibEncoder;
    use flate2::Compression;
    let mut enc = ZlibEncoder::new(Vec::new(), Compression::default());
    enc.write_all(data).map_err(|e| e.to_string())?;
    enc.finish().map_err(|e| e.to_string())
}

pub fn inflate_gzip(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut dec = flate2::read::GzDecoder::new(data);
    let mut out = Vec::new();
    dec.read_to_end(&mut out).map_err(|e| e.to_string())?;
    Ok(out)
}

pub fn deflate_gzip(data: &[u8]) -> Result<Vec<u8>, String> {
    use flate2::write::GzEncoder;
    use flate2::Compression;
    let mut enc = GzEncoder::new(Vec::new(), Compression::default());
    enc.write_all(data).map_err(|e| e.to_string())?;
    enc.finish().map_err(|e| e.to_string())
}

pub fn encode_base64(data: &[u8]) -> String {
    const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    let mut i = 0;
    while i + 3 <= data.len() {
        let n = ((data[i] as u32) << 16) | ((data[i + 1] as u32) << 8) | data[i + 2] as u32;
        out.push(T[((n >> 18) & 63) as usize] as char);
        out.push(T[((n >> 12) & 63) as usize] as char);
        out.push(T[((n >> 6) & 63) as usize] as char);
        out.push(T[(n & 63) as usize] as char);
        i += 3;
    }
    if i < data.len() {
        let b0 = data[i] as u32;
        let b1 = if i + 1 < data.len() { data[i + 1] as u32 } else { 0 };
        let n = (b0 << 16) | (b1 << 8);
        out.push(T[((n >> 18) & 63) as usize] as char);
        out.push(T[((n >> 12) & 63) as usize] as char);
        if i + 1 < data.len() {
            out.push(T[((n >> 6) & 63) as usize] as char);
            out.push('=');
        } else {
            out.push('=');
            out.push('=');
        }
    }
    out
}

pub fn decode_base64(text: &str) -> Result<Vec<u8>, String> {
    fn val(c: u8) -> Result<u8, String> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            _ => Err(format!("bad base64 byte {c}")),
        }
    }
    let bytes: Vec<u8> = text.bytes().filter(|b| !b.is_ascii_whitespace()).collect();
    if bytes.len() % 4 != 0 {
        return Err("base64 length is not a multiple of 4".into());
    }
    let mut out = Vec::new();
    for chunk in bytes.chunks(4) {
        let pad = chunk.iter().filter(|b| **b == b'=').count();
        let a = val(chunk[0])?;
        let b = val(chunk[1])?;
        let c = if chunk[2] == b'=' { 0 } else { val(chunk[2])? };
        let d = if chunk[3] == b'=' { 0 } else { val(chunk[3])? };
        let n = ((a as u32) << 18) | ((b as u32) << 12) | ((c as u32) << 6) | d as u32;
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Ok(out)
}
