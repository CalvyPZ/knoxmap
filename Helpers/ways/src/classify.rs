//! Hit, cover, or miss for one way against a selection.
//!
//! The tests are the ones `localosm._classify_coords` runs, including the
//! segment clip and the ray cast. A line that crosses the box is a hit. A
//! closed ring that only contains the box is a cover, which is how a lake
//! larger than the selection is still drawn.

/// OSM stores a coordinate as an int, 1e-7 degrees.
pub const SCALE: f64 = 10_000_000.0;

pub const MISS: i32 = 0;
pub const HIT: i32 = 1;
pub const COVER: i32 = 2;

const LEFT: i32 = 1;
const RIGHT: i32 = 2;
const BOTTOM: i32 = 4;
const TOP: i32 = 8;

fn code(lat: f64, lon: f64, south: f64, west: f64, north: f64, east: f64) -> i32 {
    let mut bits = 0;
    if lon < west {
        bits |= LEFT;
    } else if lon > east {
        bits |= RIGHT;
    }
    if lat < south {
        bits |= BOTTOM;
    } else if lat > north {
        bits |= TOP;
    }
    bits
}

/// True when the segment meets the box, including along its edge.
///
/// Same eight-step clip as `_seg_hits`. The outside end that is moved is the
/// first of the two, which is what that function does.
fn seg_hits(
    mut lat1: f64,
    mut lon1: f64,
    mut lat2: f64,
    mut lon2: f64,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> bool {
    let mut c1 = code(lat1, lon1, south, west, north, east);
    let mut c2 = code(lat2, lon2, south, west, north, east);
    for _ in 0..8 {
        if c1 == 0 || c2 == 0 {
            return true;
        }
        if c1 & c2 != 0 {
            return false;
        }
        let out = if c1 != 0 { c1 } else { c2 };
        let (lat, lon) = if out & TOP != 0 {
            if lat2 == lat1 {
                return false;
            }
            (north, lon1 + (lon2 - lon1) * (north - lat1) / (lat2 - lat1))
        } else if out & BOTTOM != 0 {
            if lat2 == lat1 {
                return false;
            }
            (south, lon1 + (lon2 - lon1) * (south - lat1) / (lat2 - lat1))
        } else if out & RIGHT != 0 {
            if lon2 == lon1 {
                return false;
            }
            (lat1 + (lat2 - lat1) * (east - lon1) / (lon2 - lon1), east)
        } else {
            if lon2 == lon1 {
                return false;
            }
            (lat1 + (lat2 - lat1) * (west - lon1) / (lon2 - lon1), west)
        };
        if out == c1 {
            lat1 = lat;
            lon1 = lon;
            c1 = code(lat, lon, south, west, north, east);
        } else {
            lat2 = lat;
            lon2 = lon;
            c2 = code(lat, lon, south, west, north, east);
        }
    }
    false
}

fn separated(
    lat: f64,
    lon: f64,
    prev_lat: f64,
    prev_lon: f64,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> bool {
    if lon < west && prev_lon < west {
        return true;
    }
    if lon > east && prev_lon > east {
        return true;
    }
    if lat < south && prev_lat < south {
        return true;
    }
    if lat > north && prev_lat > north {
        return true;
    }
    false
}

/// Ray cast. Points are (lat, lon), closed or not.
fn pip(points: &[(f64, f64)], lat: f64, lon: f64) -> bool {
    let count = points.len();
    if count < 3 {
        return false;
    }
    let mut inside = false;
    let mut j = count - 1;
    for i in 0..count {
        let (lati, loni) = points[i];
        let (latj, lonj) = points[j];
        if (lati > lat) != (latj > lat) {
            let span = latj - lati;
            if span != 0.0 {
                let cross = (lonj - loni) * (lat - lati) / span + loni;
                if lon < cross {
                    inside = !inside;
                }
            }
        }
        j = i;
    }
    inside
}

/// True when (lat, lon) falls inside this ring. The same ray cast as `_pip`.
pub fn contains(xy: &[u8], lat: f64, lon: f64) -> bool {
    if xy.len() < 24 || xy.len() % 8 != 0 {
        return false;
    }
    let count = xy.len() / 8;
    let mut points = Vec::with_capacity(count);
    for index in 0..count {
        points.push(point_at(xy, index));
    }
    pip(&points, lat, lon)
}

fn point_at(xy: &[u8], index: usize) -> (f64, f64) {
    let at = index * 8;
    let mut x = [0u8; 4];
    let mut y = [0u8; 4];
    x.copy_from_slice(&xy[at..at + 4]);
    y.copy_from_slice(&xy[at + 4..at + 8]);
    (
        i32::from_le_bytes(y) as f64 / SCALE,
        i32::from_le_bytes(x) as f64 / SCALE,
    )
}

/// `'hit'`, `'cover'`, or `'miss'` from packed x/y pairs.
///
/// `xy` is little-endian int32 longitude, int32 latitude, the same bytes the
/// span files store. Fewer than two points is a miss.
pub fn classify(xy: &[u8], south: f64, west: f64, north: f64, east: f64) -> i32 {
    if xy.len() < 16 || xy.len() % 8 != 0 {
        return MISS;
    }
    let count = xy.len() / 8;
    let mut hit = false;
    let mut prev: Option<(f64, f64)> = None;
    let mut min_lat = 90.0;
    let mut max_lat = -90.0;
    let mut min_lon = 180.0;
    let mut max_lon = -180.0;
    let mut points = Vec::with_capacity(count);
    for index in 0..count {
        let (lat, lon) = point_at(xy, index);
        points.push((lat, lon));
        if lat < min_lat {
            min_lat = lat;
        }
        if lat > max_lat {
            max_lat = lat;
        }
        if lon < min_lon {
            min_lon = lon;
        }
        if lon > max_lon {
            max_lon = lon;
        }
        if !hit {
            if south <= lat && lat <= north && west <= lon && lon <= east {
                hit = true;
            } else if let Some((plat, plon)) = prev {
                if !separated(lat, lon, plat, plon, south, west, north, east)
                    && seg_hits(plat, plon, lat, lon, south, west, north, east)
                {
                    hit = true;
                }
            }
        }
        prev = Some((lat, lon));
        // A crossing is a hit no matter what the rest of the way does.
        if hit {
            return HIT;
        }
    }
    let closed = count >= 4 && xy[..8] == xy[xy.len() - 8..];
    if !closed || min_lat > south || max_lat < north || min_lon > west || max_lon < east {
        return MISS;
    }
    let mid_lat = (south + north) / 2.0;
    let mid_lon = (west + east) / 2.0;
    if !pip(&points, mid_lat, mid_lon) {
        return MISS;
    }
    COVER
}
