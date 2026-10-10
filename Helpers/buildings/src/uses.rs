//! What a commercial ground floor really is.
//!
//! OpenStreetMap records a trade at a point - a pizza place, a bank, a vet -
//! and knoxbuild/uses.py turns each into the pair of rooms it needs: the room on
//! the street and the room that serves it. This pass hands those out: the rooms
//! with an outside wall on the street become the fronts, in street order, so a
//! parade of shops reads along the pavement in the order the map found them, and
//! the biggest room behind each front becomes its stockroom or its kitchen.
//!
//! A building the map says nothing about still gets a trade, drawn from the ones
//! a high street is made of, so a block of flats is not eight general stores.
//!
//! This is knoxbuild/layout.py's `_assign_shop_floor`.

use crate::blob::Street;
use crate::catalog::Catalog;
use crate::plan::{Plan, RoomKind};
use crate::rng::Rng;

/// A room needs this much floor before it can be what the building sells.
const MIN_SHOP_ROOM: i32 = 16;
/// ...and this much outside wall on the street.
const MIN_STREET_WALL: i32 = 3;

/// Name the rooms of a commercial ground floor.
///
/// `uses` is (front room, back room) as 1-based catalog rooms, in the order the
/// map found them. `several` is whether this building may hold more than one
/// business: a row of shops may, a house with a shop in the front cannot.
pub fn shop_floor(
    plan: &mut Plan,
    cat: &Catalog,
    street: Street,
    uses: &[(u32, u32)],
    several: bool,
    rng: &mut Rng,
) {
    let areas = plan.room_areas();
    let open: Vec<usize> = (0..plan.rooms.len())
        .filter(|at| !plan.rooms[*at].is_core && !plan.rooms[*at].is_shaft)
        .collect();
    if open.is_empty() {
        return;
    }
    let area_of = |at: usize| areas.get(at + 1).copied().unwrap_or(0);

    let mut fronts: Vec<usize> = open
        .iter()
        .copied()
        .filter(|at| area_of(*at) >= MIN_SHOP_ROOM && street_wall(plan, *at, street) >= MIN_STREET_WALL)
        .collect();
    if fronts.is_empty() {
        fronts.push(biggest(&open, &area_of));
    }
    if !several {
        let one = biggest(&fronts, &area_of);
        fronts.clear();
        fronts.push(one);
    }
    // Along the street, so the uses keep the order they were found in.
    let along_x = matches!(street, Street::North | Street::South | Street::None);
    fronts.sort_by_key(|at| {
        let rect = plan.rooms[*at].rect;
        if along_x {
            (rect.x0, rect.y0)
        } else {
            (rect.y0, rect.x0)
        }
    });

    let mut taken = vec![false; plan.rooms.len()];
    for at in &fronts {
        taken[*at] = true;
    }
    let mut backs: Vec<(usize, u32)> = Vec::with_capacity(fronts.len());
    for (n, at) in fronts.iter().enumerate() {
        let (front, back) = match uses.get(n) {
            Some(pair) => *pair,
            // More shop fronts than the map has businesses: the last one along
            // the street takes the room next door as well.
            None => match uses.last() {
                Some(last) => (last.0, cat.role_of("storage")),
                None => {
                    let shop = cat.store(rng.below(u32::MAX as u64) as u32);
                    let back = if cat.role_name(shop) == Some("grocery") {
                        cat.role_of("grocerystorage")
                    } else {
                        cat.role_of("storage")
                    };
                    (shop, back)
                }
            },
        };
        if front != 0 {
            plan.rooms[*at].kind = RoomKind::Shop;
            plan.rooms[*at].role = front;
            plan.rooms[*at].fixed = true;
        }
        backs.push((*at, back));
    }

    for (at, back) in backs {
        // The biggest room behind this front that nobody has yet.
        let mut best: Option<usize> = None;
        for other in neighbours(plan, at) {
            if taken[other] {
                continue;
            }
            if best.is_none() || area_of(other) > area_of(best.unwrap()) {
                best = Some(other);
            }
        }
        if let Some(other) = best {
            taken[other] = true;
            if back != 0 {
                plan.rooms[other].kind = RoomKind::StoreRoom;
                plan.rooms[other].role = back;
                plan.rooms[other].fixed = true;
            }
        }
    }

    // Whatever is left over: storerooms, an office, and a lavatory in the
    // smallest of them.
    let mut rest: Vec<usize> = open.iter().copied().filter(|at| !taken[*at]).collect();
    rest.sort_by_key(|at| (-area_of(*at), *at));
    const BACK_ROOMS: [RoomKind; 3] = [RoomKind::Storage, RoomKind::Office, RoomKind::Storage];
    for (n, at) in rest.iter().enumerate() {
        plan.rooms[*at].kind = BACK_ROOMS[n % BACK_ROOMS.len()];
        plan.rooms[*at].role = 0;
        plan.rooms[*at].fixed = true;
    }
    if rest.len() >= 2 {
        let last = rest[rest.len() - 1];
        plan.rooms[last].kind = RoomKind::Restroom;
        plan.rooms[last].role = 0;
    }
}

fn biggest(of: &[usize], area_of: &impl Fn(usize) -> i32) -> usize {
    let mut best = of[0];
    for at in of {
        if area_of(*at) > area_of(best) {
            best = *at;
        }
    }
    best
}

/// How many tiles of this room's wall look out on the street. With no street
/// known, every outside wall counts: a corner shop faces whichever way it can.
fn street_wall(plan: &Plan, at: usize, street: Street) -> i32 {
    let id = at as i32 + 1;
    let rect = plan.rooms[at].rect;
    let mut n = 0;
    let wants = |side: Street| street == Street::None || street == side;
    if wants(Street::North) {
        for x in rect.x0..=rect.x1 {
            if plan.at(x, rect.y0) == id && plan.at(x, rect.y0 - 1) == 0 {
                n += 1;
            }
        }
    }
    if wants(Street::South) {
        for x in rect.x0..=rect.x1 {
            if plan.at(x, rect.y1) == id && plan.at(x, rect.y1 + 1) == 0 {
                n += 1;
            }
        }
    }
    if wants(Street::West) {
        for y in rect.y0..=rect.y1 {
            if plan.at(rect.x0, y) == id && plan.at(rect.x0 - 1, y) == 0 {
                n += 1;
            }
        }
    }
    if wants(Street::East) {
        for y in rect.y0..=rect.y1 {
            if plan.at(rect.x1, y) == id && plan.at(rect.x1 + 1, y) == 0 {
                n += 1;
            }
        }
    }
    n
}

/// The rooms this one shares a wall with, by index.
fn neighbours(plan: &Plan, at: usize) -> Vec<usize> {
    let id = at as i32 + 1;
    let rect = plan.rooms[at].rect;
    let mut out: Vec<usize> = Vec::new();
    let note = |other: i32, out: &mut Vec<usize>| {
        if other > 0 && other != id {
            let index = other as usize - 1;
            if index < plan.rooms.len() && !out.contains(&index) {
                out.push(index);
            }
        }
    };
    for x in rect.x0..=rect.x1 {
        if plan.at(x, rect.y0) == id {
            note(plan.at(x, rect.y0 - 1), &mut out);
        }
        if plan.at(x, rect.y1) == id {
            note(plan.at(x, rect.y1 + 1), &mut out);
        }
    }
    for y in rect.y0..=rect.y1 {
        if plan.at(rect.x0, y) == id {
            note(plan.at(rect.x0 - 1, y), &mut out);
        }
        if plan.at(rect.x1, y) == id {
            note(plan.at(rect.x1 + 1, y), &mut out);
        }
    }
    out
}
