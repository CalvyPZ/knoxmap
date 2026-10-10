//! Doorways: between the rooms, and out to the street.
//!
//! Every room has to be reachable from the front door, or the game has rooms
//! nobody can walk into. BuildingEd draws a wall between any two tiles with
//! different room ids, so a doorway is a hole punched in one of those edges,
//! and the problem is which edges to punch.
//!
//! It is solved as a spanning tree over the rooms: start at the room the front
//! door opens into and keep adding the cheapest edge that reaches a room not
//! yet connected. Cheap means "onto a hall or a corridor", so a house's rooms
//! open off its hallway rather than through one another, which is how the
//! game's own houses are laid out. Where there is no circulation - a shop, a
//! warehouse - the widest shared wall wins instead.

use crate::blob::Street;
use crate::grid::Side;
use crate::plan::{EdgeAt, Plan, RoomKind};
use crate::results::Door;
use crate::rng::Rng;

/// A stretch of wall two rooms share, and where a door would go in it.
#[derive(Clone, Copy)]
struct Shared {
    a: usize,
    b: usize,
    /// How many edges long the stretch is.
    length: i32,
    door: EdgeAt,
}

/// Every stretch of wall shared by two rooms, with the spot a door would take.
fn shared_walls(plan: &Plan) -> Vec<Shared> {
    let mut runs: Vec<(usize, usize, Side, i32, i32, i32)> = Vec::new();
    let n_rooms = plan.rooms.len();
    // (room a, room b, side, line, start, end) with start..=end along the wall.
    let push = |a: i32, b: i32, side: Side, line: i32, pos: i32, runs: &mut Vec<(usize, usize, Side, i32, i32, i32)>| {
        // 0 is outside, and an id past the room list is a grid the renumbering
        // has not caught up with: neither is a room to put a door between.
        if a <= 0 || b <= 0 || a == b || a as usize > n_rooms || b as usize > n_rooms {
            return;
        }
        let (lo, hi) = if a < b { (a, b) } else { (b, a) };
        let key = (lo as usize - 1, hi as usize - 1, side, line);
        // Extend the stretch this edge continues, or start a new one.
        if let Some(found) = runs.iter_mut().rev().find(|held| {
            held.0 == key.0 && held.1 == key.1 && held.2 == key.2 && held.3 == key.3 && held.5 + 1 == pos
        }) {
            found.5 = pos;
            return;
        }
        runs.push((key.0, key.1, key.2, key.3, pos, pos));
    };

    // West edges, down each column, so a stretch runs along y.
    for x in 1..plan.width {
        for y in 0..plan.height {
            push(plan.at(x - 1, y), plan.at(x, y), Side::West, x, y, &mut runs);
        }
    }
    // North edges, along each row, so a stretch runs along x.
    for y in 1..plan.height {
        for x in 0..plan.width {
            push(plan.at(x, y - 1), plan.at(x, y), Side::North, y, x, &mut runs);
        }
    }

    runs.into_iter()
        .map(|(a, b, side, line, start, end)| {
            let length = end - start + 1;
            // The middle of the stretch. A door in the first or last tile of a
            // run sits against the corner of the room, where a wall meeting it
            // leaves no room to walk through.
            let middle = start + (end - start) / 2;
            let door = match side {
                Side::West => EdgeAt::new(line, middle, Side::West),
                Side::North => EdgeAt::new(middle, line, Side::North),
            };
            Shared {
                a,
                b,
                length,
                door,
            }
        })
        .collect()
}

/// What a doorway between these two rooms costs. Lower is preferred, so the
/// tree is built through the circulation first and a bedroom is never the way
/// into the bathroom if a hall will do.
fn cost(plan: &Plan, wall: &Shared) -> i32 {
    let a = plan.rooms[wall.a].kind;
    let b = plan.rooms[wall.b].kind;
    if a.is_sealed() || b.is_sealed() {
        return i32::MAX;        // the lift shaft has no doorway
    }
    let through_hall = a.is_circulation() || b.is_circulation();
    let mut score = if through_hall { 0 } else { 40 };
    // A wide shared wall is a better doorway than a two-tile overlap.
    score -= wall.length.min(8);
    // Two dwellings never open into each other: a flat's rooms open onto the
    // flat and onto the corridor once, never into the flat next door.
    let (ua, ub) = (plan.rooms[wall.a].unit, plan.rooms[wall.b].unit);
    if ua != ub && ua != 0 && ub != 0 {
        return i32::MAX;
    }
    score
}

/// Punch doorways until every room is reachable from `start`.
pub fn interior_doors(plan: &mut Plan, start: usize) {
    let walls = shared_walls(plan);
    if walls.is_empty() {
        return;
    }
    let n = plan.rooms.len();
    let mut reached = vec![false; n];
    if start < n {
        reached[start] = true;
    } else {
        reached[0] = true;
    }
    // A sealed room counts as done: nothing is ever going to open into it.
    for (i, room) in plan.rooms.iter().enumerate() {
        if room.kind.is_sealed() {
            reached[i] = true;
        }
    }
    let costs: Vec<i32> = walls.iter().map(|wall| cost(plan, wall)).collect();
    loop {
        let mut best: Option<(i32, usize)> = None;
        for (i, wall) in walls.iter().enumerate() {
            if costs[i] == i32::MAX {
                continue;
            }
            let (ra, rb) = (reached[wall.a], reached[wall.b]);
            if ra == rb {
                continue;           // both in, or both out
            }
            if best.is_none() || costs[i] < best.unwrap().0 {
                best = Some((costs[i], i));
            }
        }
        match best {
            Some((_, at)) => {
                let wall = walls[at];
                reached[wall.a] = true;
                reached[wall.b] = true;
                if !plan.has_door(&wall.door) {
                    plan.doors.push(wall.door);
                }
            }
            None => break,
        }
    }

    // A flat opens onto the corridor once. The tree already gives it one way
    // in; a second door from a corridor into a room of the same flat is what
    // makes a corridor-fed floor walkable when the flat's own rooms are in a
    // row, so add one where a room of a flat touches circulation and has no
    // door at all.
    let mut extra: Vec<EdgeAt> = Vec::new();
    for wall in &walls {
        if cost(plan, wall) == i32::MAX {
            continue;
        }
        let a = plan.rooms[wall.a];
        let b = plan.rooms[wall.b];
        let hall_side = a.kind.is_circulation() != b.kind.is_circulation();
        if !hall_side || wall.length < 3 {
            continue;
        }
        let room = if a.kind.is_circulation() { wall.b } else { wall.a };
        let has_any = plan.doors.iter().any(|door| {
            let (left, right) = sides_of(plan, door);
            left == Some(room) || right == Some(room)
        });
        if !has_any && !plan.has_door(&wall.door) {
            extra.push(wall.door);
        }
    }
    for door in extra {
        if !plan.has_door(&door) {
            plan.doors.push(door);
        }
    }
}

/// The rooms either side of an edge, as room indexes.
pub fn sides_of(plan: &Plan, edge: &EdgeAt) -> (Option<usize>, Option<usize>) {
    match edge.side {
        Side::West => (
            plan.room_of(edge.x - 1, edge.y),
            plan.room_of(edge.x, edge.y),
        ),
        Side::North => (
            plan.room_of(edge.x, edge.y - 1),
            plan.room_of(edge.x, edge.y),
        ),
    }
}

/// Which way an exterior edge faces: the side of the building it is on.
fn faces(plan: &Plan, edge: &EdgeAt) -> Option<Street> {
    let (outer, inner) = match edge.side {
        Side::West => (plan.at(edge.x - 1, edge.y), plan.at(edge.x, edge.y)),
        Side::North => (plan.at(edge.x, edge.y - 1), plan.at(edge.x, edge.y)),
    };
    if outer == 0 && inner > 0 {
        // The tile outside is to the west or north of the building.
        return Some(match edge.side {
            Side::West => Street::West,
            Side::North => Street::North,
        });
    }
    if inner == 0 && outer > 0 {
        return Some(match edge.side {
            Side::West => Street::East,
            Side::North => Street::South,
        });
    }
    None
}

/// Every exterior edge of the storey, with the room behind it and the side of
/// the building it is on. Party edges are left out: a wall shared with the
/// building next door has nothing cut into it.
pub fn outside_edges(plan: &Plan) -> Vec<(EdgeAt, usize, Street)> {
    let mut out = Vec::new();
    for y in 0..=plan.height {
        for x in 0..=plan.width {
            for side in [Side::West, Side::North] {
                let edge = EdgeAt::new(x, y, side);
                let facing = match faces(plan, &edge) {
                    Some(facing) => facing,
                    None => continue,
                };
                if plan.is_party(&edge) {
                    continue;
                }
                let room = match side {
                    Side::West => plan
                        .room_of(edge.x, edge.y)
                        .or_else(|| plan.room_of(edge.x - 1, edge.y)),
                    Side::North => plan
                        .room_of(edge.x, edge.y)
                        .or_else(|| plan.room_of(edge.x, edge.y - 1)),
                };
                if let Some(room) = room {
                    out.push((edge, room, facing));
                }
            }
        }
    }
    out
}

/// The way in. Returns the door and the room it opens into.
///
/// The street side is preferred, and a hall over a bedroom: a front door into
/// somebody's bathroom is the sort of thing that reads as generated. Where the
/// map does not say which way the street is, the longest outside wall wins.
pub fn front_door(plan: &mut Plan, street: Street, rng: &mut Rng) -> Option<(EdgeAt, usize)> {
    let edges = outside_edges(plan);
    if edges.is_empty() {
        return None;
    }
    let mut best: Option<(i32, EdgeAt, usize)> = None;
    for (edge, room, facing) in edges {
        if plan.rooms[room].kind.is_sealed() {
            continue;
        }
        if touches_corner(plan, &edge) {
            continue;
        }
        let mut score = 0;
        if street != Street::None && facing == street {
            score += 100;
        } else if street != Street::None {
            score -= 20;
        }
        if plan.rooms[room].kind.is_circulation() {
            score += 60;
        }
        if matches!(
            plan.rooms[room].kind,
            RoomKind::Bathroom | RoomKind::Bedroom | RoomKind::KidsBedroom
        ) {
            score -= 50;
        }
        // A tile of jitter, so two identical houses do not put the door in
        // exactly the same place.
        score += rng.below_usize(8) as i32;
        if best.is_none() || score > best.unwrap().0 {
            best = Some((score, edge, room));
        }
    }
    let (_score, edge, room) = best?;
    if !plan.has_door(&edge) {
        plan.doors.push(edge);
    }
    Some((edge, room))
}

/// A way out the back, for a house. Knox County's houses nearly all have one,
/// and a yard with no door onto it is a yard nobody uses.
pub fn back_door(plan: &mut Plan, front: Street, rng: &mut Rng) -> Option<EdgeAt> {
    let opposite = match front {
        Street::North => Street::South,
        Street::South => Street::North,
        Street::East => Street::West,
        Street::West => Street::East,
        Street::None => Street::None,
    };
    let edges = outside_edges(plan);
    let mut best: Option<(i32, EdgeAt)> = None;
    for (edge, room, facing) in edges {
        let kind = plan.rooms[room].kind;
        if kind.is_sealed() || touches_corner(plan, &edge) || plan.has_door(&edge) {
            continue;
        }
        let mut score = 0;
        if opposite != Street::None && facing == opposite {
            score += 80;
        }
        // Out through the kitchen or the hall, as a back door usually is.
        if matches!(kind, RoomKind::Kitchen | RoomKind::OpenPlan) || kind.is_circulation() {
            score += 40;
        }
        if matches!(kind, RoomKind::Bathroom | RoomKind::Bedroom) {
            continue;
        }
        score += rng.below_usize(8) as i32;
        if best.is_none() || score > best.unwrap().0 {
            best = Some((score, edge));
        }
    }
    let (_score, edge) = best?;
    plan.doors.push(edge);
    Some(edge)
}

/// Is this edge at the corner of its room? A door there has a wall running
/// into it and cannot be walked through.
fn touches_corner(plan: &Plan, edge: &EdgeAt) -> bool {
    let inside = match edge.side {
        Side::West => plan.room_of(edge.x, edge.y).or(plan.room_of(edge.x - 1, edge.y)),
        Side::North => plan.room_of(edge.x, edge.y).or(plan.room_of(edge.x, edge.y - 1)),
    };
    let inside = match inside {
        Some(room) => room,
        None => return true,
    };
    // One step either way along the wall must be the same room, or the doorway
    // is in the last tile before a corner.
    let (ax, ay, bx, by) = match edge.side {
        Side::West => (edge.x, edge.y - 1, edge.x, edge.y + 1),
        Side::North => (edge.x - 1, edge.y, edge.x + 1, edge.y),
    };
    let (back, forward) = match edge.side {
        Side::West => (
            plan.room_of(ax, ay).or(plan.room_of(ax - 1, ay)),
            plan.room_of(bx, by).or(plan.room_of(bx - 1, by)),
        ),
        Side::North => (
            plan.room_of(ax, ay).or(plan.room_of(ax, ay - 1)),
            plan.room_of(bx, by).or(plan.room_of(bx, by - 1)),
        ),
    };
    back != Some(inside) && forward != Some(inside)
}

/// The ground-floor outside doors, as the paths and yards pass wants them:
/// the tile outside the door and the tile inside it.
pub fn outside_doors(plan: &Plan) -> Vec<Door> {
    let mut out = Vec::new();
    for door in &plan.doors {
        let (outside, inside) = match door.side {
            Side::West => ((door.x - 1, door.y), (door.x, door.y)),
            Side::North => ((door.x, door.y - 1), (door.x, door.y)),
        };
        let a = plan.at(outside.0, outside.1);
        let b = plan.at(inside.0, inside.1);
        if (a == 0) == (b == 0) {
            continue;               // not a way out of the building
        }
        let (out_tile, in_tile) = if b == 0 {
            (inside, outside)
        } else {
            (outside, inside)
        };
        out.push(Door {
            out_x: out_tile.0 as i16,
            out_y: out_tile.1 as i16,
            in_x: in_tile.0 as i16,
            in_y: in_tile.1 as i16,
        });
    }
    out
}
