//! Join a relation's member ways into closed rings.
//!
//! Open shores are chained where the endpoints are the same node. A chain
//! that does not close is dropped, which is what `_closed_role_rings` does
//! after the pieces are joined. A member that is already a ring stays one.

#[derive(Clone)]
struct Line {
    inner: bool,
    pts: Vec<(i32, i32)>,
}

fn points_of(xy: &[u8]) -> Vec<(i32, i32)> {
    let mut out = Vec::with_capacity(xy.len() / 8);
    let mut i = 0;
    while i + 8 <= xy.len() {
        let x = i32::from_le_bytes(xy[i..i + 4].try_into().unwrap());
        let y = i32::from_le_bytes(xy[i + 4..i + 8].try_into().unwrap());
        out.push((x, y));
        i += 8;
    }
    out
}

fn closed(pts: &[(i32, i32)]) -> bool {
    pts.len() >= 4 && pts[0] == pts[pts.len() - 1]
}

fn to_xy(pts: &[(i32, i32)]) -> Vec<u8> {
    let mut out = Vec::with_capacity(pts.len() * 8);
    for (x, y) in pts {
        out.extend_from_slice(&x.to_le_bytes());
        out.extend_from_slice(&y.to_le_bytes());
    }
    out
}

fn append(chain: &mut Vec<(i32, i32)>, extra: &[(i32, i32)], reverse: bool) {
    if extra.is_empty() {
        return;
    }
    if reverse {
        for pt in extra.iter().rev().skip(1) {
            chain.push(*pt);
        }
    } else {
        chain.extend_from_slice(&extra[1..]);
    }
}

fn chain_role(lines: &[Line], inner: bool) -> Vec<Vec<u8>> {
    let mut pool: Vec<Option<Vec<(i32, i32)>>> = lines
        .iter()
        .filter(|line| line.inner == inner)
        .map(|line| Some(line.pts.clone()))
        .collect();
    let mut rings = Vec::new();
    for index in 0..pool.len() {
        let Some(mut chain) = pool[index].take() else {
            continue;
        };
        if closed(&chain) {
            rings.push(to_xy(&chain));
            continue;
        }
        // Grow forward, then backward, until the ends meet or nothing fits.
        for _ in 0..pool.len() {
            if closed(&chain) {
                break;
            }
            let end = *chain.last().unwrap();
            let mut found = None;
            for (slot, item) in pool.iter_mut().enumerate() {
                let Some(pts) = item else { continue };
                if pts.is_empty() {
                    continue;
                }
                if pts[0] == end {
                    found = Some((slot, false));
                    break;
                }
                if *pts.last().unwrap() == end {
                    found = Some((slot, true));
                    break;
                }
            }
            let Some((slot, reverse)) = found else { break };
            let pts = pool[slot].take().unwrap();
            append(&mut chain, &pts, reverse);
        }
        if !closed(&chain) {
            for _ in 0..pool.len() {
                if closed(&chain) {
                    break;
                }
                let start = chain[0];
                let mut found = None;
                for (slot, item) in pool.iter_mut().enumerate() {
                    let Some(pts) = item else { continue };
                    if pts.is_empty() {
                        continue;
                    }
                    if *pts.last().unwrap() == start {
                        found = Some((slot, false));
                        break;
                    }
                    if pts[0] == start {
                        found = Some((slot, true));
                        break;
                    }
                }
                let Some((slot, reverse)) = found else { break };
                let pts = pool[slot].take().unwrap();
                let mut grown = Vec::with_capacity(chain.len() + pts.len());
                if reverse {
                    grown.extend(pts.iter().rev().skip(1).copied());
                    // pts[0] is the joint, already skipped from the reversed
                    // tail. The chain's first point is that joint, so the
                    // reversed line without its new end is prepended.
                    grown.extend_from_slice(&chain);
                } else {
                    grown.extend_from_slice(&pts[..pts.len() - 1]);
                    grown.extend_from_slice(&chain);
                }
                chain = grown;
            }
        }
        if closed(&chain) {
            rings.push(to_xy(&chain));
        }
    }
    rings
}

/// Closed outers and closed inners. Empty outers means the relation is not
/// a polygon.
pub fn closed_rings(members: &[(bool, Vec<u8>)]) -> Option<(Vec<Vec<u8>>, Vec<Vec<u8>>)> {
    let lines: Vec<Line> = members
        .iter()
        .filter_map(|(inner, xy)| {
            let pts = points_of(xy);
            if pts.len() < 2 {
                None
            } else {
                Some(Line {
                    inner: *inner,
                    pts,
                })
            }
        })
        .collect();
    let outers = chain_role(&lines, false);
    if outers.is_empty() {
        return None;
    }
    let inners = chain_role(&lines, true);
    Some((outers, inners))
}
