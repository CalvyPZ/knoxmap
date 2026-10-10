//! Windows, for the whole building at once.
//!
//! They are placed for every storey together rather than floor by floor,
//! because windows that stack in columns are what makes a building read as a
//! building: each floor scattering its own left elevations that looked like
//! noise. So the ground floor decides the columns, and an upper floor repeats
//! them wherever it has wall in the same place.
//!
//! Nothing is cut into a wall shared with the building next door (the party
//! edges), into a doorway, or into the tile beside a corner.

use crate::blob::{Kind, Street};
use crate::doors::outside_edges;
use crate::grid::Side;
use crate::plan::{EdgeAt, Plan, RoomKind};
use crate::rng::Rng;

/// One window, then this many blank tiles, along a wall. Knox County's houses
/// are not glazed end to end.
const WINDOW_EVERY: i32 = 2;
/// A wall this short gets one window in the middle rather than a run of them.
const SHORT_WALL: i32 = 4;

/// A straight stretch of outside wall belonging to one room.
struct Wall {
    room: usize,
    side: Side,
    line: i32,
    start: i32,
    end: i32,
    facing: Street,
}

/// Group a storey's outside edges into straight stretches, one room each.
fn walls(plan: &Plan) -> Vec<Wall> {
    let mut edges = outside_edges(plan);
    // Along each wall line, so a stretch is a run of consecutive positions.
    edges.sort_unstable_by_key(|(edge, room, _facing)| {
        let (line, pos) = match edge.side {
            Side::West => (edge.x, edge.y),
            Side::North => (edge.y, edge.x),
        };
        (*room, edge.side == Side::North, line, pos)
    });
    let mut out: Vec<Wall> = Vec::new();
    for (edge, room, facing) in edges {
        let (line, pos) = match edge.side {
            Side::West => (edge.x, edge.y),
            Side::North => (edge.y, edge.x),
        };
        match out.last_mut() {
            Some(last)
                if last.room == room
                    && last.side == edge.side
                    && last.line == line
                    && last.end + 1 == pos
                    && last.facing == facing =>
            {
                last.end = pos;
            }
            _ => out.push(Wall {
                room,
                side: edge.side,
                line,
                start: pos,
                end: pos,
                facing,
            }),
        }
    }
    out
}

fn edge_at(side: Side, line: i32, pos: i32) -> EdgeAt {
    match side {
        Side::West => EdgeAt::new(line, pos, Side::West),
        Side::North => EdgeAt::new(pos, line, Side::North),
    }
}

/// Which rooms get windows at all. A lift shaft is a sealed box, and a closet
/// with a window is a closet that used to be a room.
fn wants_windows(kind: RoomKind) -> bool {
    !matches!(kind, RoomKind::Closet | RoomKind::ShaftBox)
}

/// Place the windows of every storey. `shop_ground` glazes the ground floor of a
/// shop as a shop front instead.
pub fn place(storeys: &mut [Plan], building: Kind, shop_ground: bool, rng: &mut Rng) {
    if storeys.is_empty() {
        return;
    }
    // The columns the ground floor chose, which the floors above repeat.
    let mut columns: Vec<EdgeAt> = Vec::new();
    for (level, storey) in storeys.iter_mut().enumerate() {
        let mut here: Vec<EdgeAt> = Vec::new();
        for wall in walls(storey) {
            let kind = storey.rooms[wall.room].kind;
            if !wants_windows(kind) {
                continue;
            }
            let length = wall.end - wall.start + 1;
            if length < 2 {
                continue;           // a step in a slanted wall, not a wall
            }
            let spots: Vec<i32> = if length <= SHORT_WALL {
                vec![wall.start + length / 2]
            } else {
                // In from both corners, then every other tile.
                let mut found = Vec::new();
                let mut at = wall.start + 1;
                while at <= wall.end - 1 {
                    found.push(at);
                    at += WINDOW_EVERY;
                }
                found
            };
            for pos in spots {
                let edge = edge_at(wall.side, wall.line, pos);
                if storey.is_party(&edge) || storey.has_door(&edge) {
                    continue;
                }
                // On an upper floor, only where the ground floor has a window
                // in the same column, so they stack.
                if level > 0 && !columns.is_empty() && !columns.contains(&edge) {
                    // A storey that is set back, or over a different part of an
                    // L, has walls the ground floor never had. Those are glazed
                    // on their own account rather than left blank.
                    if columns.iter().any(|held| {
                        held.side == edge.side
                            && match edge.side {
                                Side::West => held.x == edge.x,
                                Side::North => held.y == edge.y,
                            }
                    }) {
                        continue;
                    }
                }
                here.push(edge);
            }
        }
        // A shop's ground floor is glass: the whole street side, not windows
        // punched in brick.
        let front_side = shop_front_side(storey, building);
        for edge in &here {
            storey.windows.push(*edge);
            if shop_ground && level == 0 {
                if let Some(side) = front_side {
                    if faces(storey, edge) == Some(side) {
                        storey.shop_front.push(*edge);
                    }
                }
            }
        }
        if level == 0 {
            columns = here;
        }
        // One last pass: a room with no window at all and outside wall to spare
        // gets one, so no room is a windowless box unless it really has no
        // outside wall.
        light_every_room(storey, rng);
    }
}

/// Which way an outside edge faces, by what is on the far side of it.
fn faces(plan: &Plan, edge: &EdgeAt) -> Option<Street> {
    let (outer, inner) = match edge.side {
        Side::West => (plan.at(edge.x - 1, edge.y), plan.at(edge.x, edge.y)),
        Side::North => (plan.at(edge.x, edge.y - 1), plan.at(edge.x, edge.y)),
    };
    if outer == 0 && inner > 0 {
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

/// The side a shop's glass goes in: the side its front door is on.
fn shop_front_side(plan: &Plan, building: Kind) -> Option<Street> {
    if !matches!(
        building,
        Kind::Retail | Kind::Shop | Kind::Bank | Kind::Restaurant | Kind::Bar | Kind::Mall
    ) {
        return None;
    }
    for door in &plan.doors {
        if let Some(side) = faces(plan, door) {
            return Some(side);
        }
    }
    None
}

fn light_every_room(plan: &mut Plan, rng: &mut Rng) {
    let mut lit = vec![false; plan.rooms.len()];
    for window in &plan.windows {
        let room = match window.side {
            Side::West => plan
                .room_of(window.x, window.y)
                .or_else(|| plan.room_of(window.x - 1, window.y)),
            Side::North => plan
                .room_of(window.x, window.y)
                .or_else(|| plan.room_of(window.x, window.y - 1)),
        };
        if let Some(room) = room {
            if room < lit.len() {
                lit[room] = true;
            }
        }
    }
    let mut add: Vec<EdgeAt> = Vec::new();
    for wall in walls(plan) {
        if wall.room >= lit.len() || lit[wall.room] {
            continue;
        }
        let kind = plan.rooms[wall.room].kind;
        if !wants_windows(kind) {
            continue;
        }
        let length = wall.end - wall.start + 1;
        if length < 2 {
            continue;
        }
        // Anywhere along it but the corners, so two identical rooms are not
        // glazed in exactly the same square.
        let span = (length - 2).max(1);
        let pos = wall.start + 1 + rng.below_usize(span as usize) as i32;
        let edge = edge_at(wall.side, wall.line, pos.min(wall.end));
        if plan.is_party(&edge) || plan.has_door(&edge) || plan.has_window(&edge) {
            continue;
        }
        lit[wall.room] = true;
        add.push(edge);
    }
    for edge in add {
        plan.windows.push(edge);
    }
}
