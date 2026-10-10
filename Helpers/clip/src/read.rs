//! Span records, as `localosm` writes them after the `KNXS` marker.

use std::collections::HashMap;

#[derive(Clone)]
pub struct WayRec {
    pub xy: Vec<u8>,
    pub tags: Vec<u8>,
}

pub struct NodeRec {
    pub id: i64,
    pub x: i32,
    pub y: i32,
    pub tags: Vec<u8>,
}

pub struct Member {
    pub role_inner: bool,
    pub id: i64,
    pub xy: Vec<u8>,
}

pub struct RelRec {
    pub id: i64,
    pub tags: Vec<u8>,
    pub water: bool,
    pub members: Vec<Member>,
}

pub fn load_ways(buf: &[u8], ways: &mut HashMap<i64, WayRec>, limits: (f64, f64, f64, f64)) {
    let mut i = 0;
    while i < buf.len() {
        let kind = buf[i];
        i += 1;
        if kind == 1 {
            if i + 28 > buf.len() {
                return;
            }
            let id = i64::from_le_bytes(buf[i..i + 8].try_into().unwrap());
            let min_x = i32::from_le_bytes(buf[i + 8..i + 12].try_into().unwrap());
            let min_y = i32::from_le_bytes(buf[i + 12..i + 16].try_into().unwrap());
            let max_x = i32::from_le_bytes(buf[i + 16..i + 20].try_into().unwrap());
            let max_y = i32::from_le_bytes(buf[i + 20..i + 24].try_into().unwrap());
            let xy_len = u32::from_le_bytes(buf[i + 24..i + 28].try_into().unwrap()) as usize;
            i += 28;
            if i + xy_len + 4 > buf.len() {
                return;
            }
            let xy = buf[i..i + xy_len].to_vec();
            i += xy_len;
            let tag_len = u32::from_le_bytes(buf[i..i + 4].try_into().unwrap()) as usize;
            i += 4;
            if i + tag_len > buf.len() {
                return;
            }
            let tags = buf[i..i + tag_len].to_vec();
            i += tag_len;
            if xy_len > 0 && misses(min_x, min_y, max_x, max_y, limits) {
                continue;
            }
            ways.entry(id).or_insert(WayRec { xy, tags });
        } else if kind == 2 {
            if i + 20 > buf.len() {
                return;
            }
            let tag_len = u32::from_le_bytes(buf[i + 16..i + 20].try_into().unwrap()) as usize;
            i += 20 + tag_len;
        } else {
            return;
        }
    }
}

pub fn load_nodes(buf: &[u8], nodes: &mut Vec<NodeRec>) {
    let mut i = 0;
    while i < buf.len() {
        let kind = buf[i];
        i += 1;
        if kind == 2 {
            if i + 20 > buf.len() {
                return;
            }
            let id = i64::from_le_bytes(buf[i..i + 8].try_into().unwrap());
            let x = i32::from_le_bytes(buf[i + 8..i + 12].try_into().unwrap());
            let y = i32::from_le_bytes(buf[i + 12..i + 16].try_into().unwrap());
            let tag_len = u32::from_le_bytes(buf[i + 16..i + 20].try_into().unwrap()) as usize;
            i += 20;
            if i + tag_len > buf.len() {
                return;
            }
            let tags = buf[i..i + tag_len].to_vec();
            i += tag_len;
            nodes.push(NodeRec { id, x, y, tags });
        } else if kind == 1 {
            if i + 28 > buf.len() {
                return;
            }
            let xy_len = u32::from_le_bytes(buf[i + 24..i + 28].try_into().unwrap()) as usize;
            i += 28;
            if i + xy_len + 4 > buf.len() {
                return;
            }
            let tag_len = u32::from_le_bytes(buf[i + xy_len..i + xy_len + 4].try_into().unwrap()) as usize;
            i += xy_len + 4 + tag_len;
        } else {
            return;
        }
    }
}

pub fn load_relations(buf: &[u8], limits: (f64, f64, f64, f64)) -> Vec<RelRec> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < buf.len() {
        if buf[i] != 3 {
            break;
        }
        i += 1;
        // i64 + 4*i32 + u8 + u32 = 8+16+1+4 = 29
        if i + 29 > buf.len() {
            break;
        }
        let id = i64::from_le_bytes(buf[i..i + 8].try_into().unwrap());
        let min_x = i32::from_le_bytes(buf[i + 8..i + 12].try_into().unwrap());
        let min_y = i32::from_le_bytes(buf[i + 12..i + 16].try_into().unwrap());
        let max_x = i32::from_le_bytes(buf[i + 16..i + 20].try_into().unwrap());
        let max_y = i32::from_le_bytes(buf[i + 20..i + 24].try_into().unwrap());
        let flags = buf[i + 24];
        let tag_len = u32::from_le_bytes(buf[i + 25..i + 29].try_into().unwrap()) as usize;
        i += 29;
        if i + tag_len + 4 > buf.len() {
            break;
        }
        let tags = buf[i..i + tag_len].to_vec();
        i += tag_len;
        let count = u32::from_le_bytes(buf[i..i + 4].try_into().unwrap()) as usize;
        i += 4;
        let water = flags & 1 != 0;
        let known = flags & 2 != 0;
        if water && known && misses(min_x, min_y, max_x, max_y, limits) {
            for _ in 0..count {
                if i + 13 > buf.len() {
                    return out;
                }
                let xy_len = u32::from_le_bytes(buf[i + 9..i + 13].try_into().unwrap()) as usize;
                i += 13 + xy_len;
            }
            continue;
        }
        let mut members = Vec::with_capacity(count);
        let mut broken = false;
        for _ in 0..count {
            if i + 13 > buf.len() {
                broken = true;
                break;
            }
            let role_inner = buf[i] == 1;
            let mid = i64::from_le_bytes(buf[i + 1..i + 9].try_into().unwrap());
            let xy_len = u32::from_le_bytes(buf[i + 9..i + 13].try_into().unwrap()) as usize;
            i += 13;
            if i + xy_len > buf.len() {
                broken = true;
                break;
            }
            let xy = buf[i..i + xy_len].to_vec();
            i += xy_len;
            members.push(Member {
                role_inner,
                id: mid,
                xy,
            });
        }
        if broken {
            break;
        }
        out.push(RelRec {
            id,
            tags,
            water,
            members,
        });
    }
    out
}

fn misses(min_x: i32, min_y: i32, max_x: i32, max_y: i32, limits: (f64, f64, f64, f64)) -> bool {
    let (south_lo, north_hi, west_lo, east_hi) = limits;
    !((max_y as f64) >= south_lo
        && (min_y as f64) <= north_hi
        && (max_x as f64) >= west_lo
        && (min_x as f64) <= east_hi)
}
