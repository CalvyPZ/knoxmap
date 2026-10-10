//! Randomness that belongs to a tile rather than to a thread.
//!
//! Python's scatter passes walked the map in order and pulled from one stream,
//! so the tufts on a lawn depended on how many tiles had been looked at before
//! it. That cannot be split across threads: the answer would change with the
//! number of cores. So every scattered thing here is a function of where it is -
//! the map's seed, which pass is asking, and the tile's own coordinates - and
//! nothing else. The same seed gives the same map on four cores and on sixty
//! four, and a band may be drawn in any order because no tile's answer depends
//! on any other tile's.

/// A hash of one tile, for one pass of one map.
#[inline]
pub fn tile(seed: u32, salt: u16, x: i64, y: i64) -> u64 {
    let mut h = (seed as u64) ^ 0x9E37_79B9_7F4A_7C15;
    h = h.wrapping_add((x as u64).wrapping_mul(0xD6E8_FEB8_6659_FD93));
    h = h.rotate_left(29) ^ (y as u64).wrapping_mul(0xA076_1D64_78BD_642F);
    h ^= (salt as u64).wrapping_mul(0xE703_7ED1_A0B4_28DB);
    mix(h)
}

/// A second answer about the same tile, for a pass that needs two: which shade,
/// and how big.
#[inline]
pub fn again(hash: u64) -> u64 {
    mix(hash ^ 0x2545_F491_4F6C_DD1D)
}

#[inline]
fn mix(mut h: u64) -> u64 {
    h ^= h >> 33;
    h = h.wrapping_mul(0xFF51_AFD7_ED55_8CCD);
    h ^= h >> 33;
    h = h.wrapping_mul(0xC4CE_B9FE_1A85_EC53);
    h ^= h >> 33;
    h
}

/// A number in [0, 1).
#[inline]
pub fn unit(hash: u64) -> f32 {
    // Twenty four bits is the whole of an f32's mantissa; taking them from the
    // top of the hash is better mixed than taking them from the bottom.
    ((hash >> 40) as f32) * (1.0 / 16_777_216.0)
}

/// Whether this tile is one of the `chance` of tiles that get something.
#[inline]
pub fn hit(hash: u64, chance: f32) -> bool {
    chance > 0.0 && (chance >= 1.0 || unit(hash) < chance)
}

/// A number in 0..n.
#[inline]
pub fn below(hash: u64, n: u64) -> u64 {
    if n == 0 {
        0
    } else {
        // Multiply-shift: no division, and no bias worth the name at these sizes.
        (((hash >> 32) as u64) * n) >> 32
    }
}
