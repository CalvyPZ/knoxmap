//! Bounding box and packed x/y from a way's own node array.
//!
//! libosmium stores each vertex as an int64 id and then the same int32 x and
//! y the location object reports, 16 bytes apart. x is longitude and y is
//! latitude, in 1e-7 degrees. The invalid location is the lowest int32.

/// libosmium's missing location. A real longitude is not this.
pub const INVALID_X: i32 = i32::MIN;

pub enum Measured {
    /// A node has no location. The way is a miss, and the grid stays valid.
    Invalid,
    /// The pointer could not be read as node records.
    Unreadable,
    Ok {
        min_x: i32,
        min_y: i32,
        max_x: i32,
        max_y: i32,
        /// Little-endian int32 x, int32 y, in the order the way stored them.
        xy: Vec<u8>,
    },
}

/// One copy of the coordinates, then the min and max.
///
/// `nodes` points at the first record. Each record is 16 bytes. The result
/// is the compact pairs the span file stores, not the 16-byte records.
///
/// # Safety
/// `nodes` must point at `count` readable 16-byte records, or be null.
pub unsafe fn measure(nodes: *const u8, count: usize) -> Measured {
    if nodes.is_null() || count < 2 || count > (isize::MAX as usize) / 16 {
        return Measured::Unreadable;
    }
    let raw = std::slice::from_raw_parts(nodes, count * 16);
    let mut min_x = i32::MAX;
    let mut min_y = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_y = i32::MIN;
    let mut xy = Vec::with_capacity(count * 8);
    for index in 0..count {
        let at = index * 16;
        let mut xb = [0u8; 4];
        let mut yb = [0u8; 4];
        xb.copy_from_slice(&raw[at + 8..at + 12]);
        yb.copy_from_slice(&raw[at + 12..at + 16]);
        let x = i32::from_le_bytes(xb);
        let y = i32::from_le_bytes(yb);
        if x == INVALID_X || y == INVALID_X {
            return Measured::Invalid;
        }
        if x < min_x {
            min_x = x;
        }
        if x > max_x {
            max_x = x;
        }
        if y < min_y {
            min_y = y;
        }
        if y > max_y {
            max_y = y;
        }
        xy.extend_from_slice(&xb);
        xy.extend_from_slice(&yb);
    }
    Measured::Ok {
        min_x,
        min_y,
        max_x,
        max_y,
        xy,
    }
}
