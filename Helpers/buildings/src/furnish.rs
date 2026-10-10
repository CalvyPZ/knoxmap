//! Furniture: what stands where in each room.
//!
//! Two passes per room, which is how knoxbuild/layout.py does it. First the
//! pieces that want a wall, taken from the room kind's wishlist and put in the
//! slots where a tile has a wall on one side; then the centre group, the sofa
//! and coffee table and television that make a living room read as one, which
//! needs an open block in the middle with a walkway all round it.
//!
//! The rules the room has to keep whatever it is furnished with:
//!
//! * every tile of a piece is inside the room, so nothing hangs through a wall;
//! * nothing stands in a doorway or on the stairs, or a room is furnished shut;
//! * a piece a room has one of (a stove, a bath, a sofa) is placed once.
//!
//! Which pieces a kind of room wants, and what its centre group is, is data
//! from the catalog. The decisions are here.

use crate::catalog::{Catalog, Group};
use crate::plan::{Plan, Piece, Room, RoomKind};
use crate::rng::Rng;
use crate::grid::Side;

/// Occupancy bits. A tile keeps every reason it is taken, and each check names
/// the bits it minds: a piece allowed to stand on the stair landing is not
/// stopped by the flight.
const B_DOOR: u8 = 1;
const B_STAIR: u8 = 2;
const B_ITEM: u8 = 4;
/// Walkway round a centre group, laid down and lifted again.
const B_AISLE: u8 = 8;
const B_BLOCK: u8 = B_DOOR | B_STAIR | B_ITEM | B_AISLE;

/// Wall pieces share a tile with what stands there, but not with each other.
const B_HUNG: u8 = 16;

/// Facings, in the order the .tbx writes them.
const W: usize = 0;
const N: usize = 1;
const E: usize = 2;
const S: usize = 3;

/// Turned for a room deeper than it is wide.
const TURN: [usize; 4] = [N, W, S, E];

pub struct Fit<'a> {
    pub cat: &'a Catalog,
    /// One byte per tile of the storey: why that tile is taken.
    occ: Vec<u8>,
    width: i32,
    height: i32,
}

impl<'a> Fit<'a> {
    pub fn new(cat: &'a Catalog, plan: &Plan) -> Fit<'a> {
        Fit {
            cat,
            occ: vec![0u8; (plan.width.max(0) * plan.height.max(0)) as usize],
            width: plan.width,
            height: plan.height,
        }
    }

    fn at(&self, x: i32, y: i32) -> u8 {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return 0;
        }
        self.occ[(y * self.width + x) as usize]
    }

    fn mark(&mut self, x: i32, y: i32, bits: u8) {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return;
        }
        self.occ[(y * self.width + x) as usize] |= bits;
    }

    fn unmark(&mut self, x: i32, y: i32, bits: u8) {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return;
        }
        self.occ[(y * self.width + x) as usize] &= !bits;
    }
}

/// Furnish every room of one storey.
pub fn storey(
    plan: &mut Plan,
    cat: &Catalog,
    stairs: Option<(i32, i32, Side)>,
    rng: &mut Rng,
) {
    let mut fit = Fit::new(cat, plan);
    block_doors(plan, &mut fit);
    block_stairs(plan, &mut fit, stairs);
    for index in 0..plan.rooms.len() {
        let room = plan.rooms[index];
        if room.is_shaft || skip(room.kind) {
            continue;
        }
        let id = index as i32 + 1;
        against_walls(plan, &mut fit, id, &room, rng);
        in_the_middle(plan, &mut fit, id, &room, rng);
    }
}

/// What a room is, in the catalog's own terms: the trade the map recorded there
/// if it recorded one, else what the layout made the room.
fn room_key<'a>(cat: &'a Catalog, room: &Room) -> &'a str {
    cat.role_name(room.role).unwrap_or(room.kind.name())
}

/// A tile either side of every doorway stays clear, or the door opens onto a
/// wardrobe. The same for the tile in front of a window-wall shop door.
fn block_doors(plan: &Plan, fit: &mut Fit) {
    for door in &plan.doors {
        match door.side {
            Side::West => {
                fit.mark(door.x - 1, door.y, B_DOOR);
                fit.mark(door.x, door.y, B_DOOR);
            }
            Side::North => {
                fit.mark(door.x, door.y - 1, B_DOOR);
                fit.mark(door.x, door.y, B_DOOR);
            }
        }
    }
}

/// The flight, and the tile it is walked onto. A staircase is five tiles long
/// and nothing may stand on it.
fn block_stairs(plan: &Plan, fit: &mut Fit, stairs: Option<(i32, i32, Side)>) {
    let Some((x, y, side)) = stairs else {
        return;
    };
    for step in -1..crate::cores::STAIR_RUN + 1 {
        match side {
            Side::West => fit.mark(x, y + step, B_STAIR),
            Side::North => fit.mark(x + step, y, B_STAIR),
        }
    }
    let _ = plan;
}

/// Where a piece can stand with its back to a wall: the tile, and which way it
/// faces. Row-major, as layout.py's `_wall_slots` returns them.
fn wall_slots(plan: &Plan, id: i32, room: &Room, out: &mut Vec<(i32, i32, usize)>) {
    out.clear();
    for y in room.rect.y0..=room.rect.y1 {
        for x in room.rect.x0..=room.rect.x1 {
            if plan.at(x, y) != id {
                continue;
            }
            // A wall is wherever the next tile along belongs to something else,
            // including outside: the room's own rectangle is not the wall, the
            // change of room id is.
            if plan.at(x, y - 1) != id {
                out.push((x, y, N));
            }
            if plan.at(x, y + 1) != id {
                out.push((x, y, S));
            }
            if plan.at(x - 1, y) != id {
                out.push((x, y, W));
            }
            if plan.at(x + 1, y) != id {
                out.push((x, y, E));
            }
        }
    }
}

/// Does this piece fit here: every tile inside the room and nothing in the way.
fn fits(plan: &Plan, fit: &Fit, id: i32, furn: usize, x: i32, y: i32, orient: usize, bits: u8) -> bool {
    let Some(piece) = fit.cat.furniture.get(furn) else {
        return false;
    };
    let tiles = piece.tiles(orient);
    if tiles.is_empty() {
        return false;
    }
    for (dx, dy, _tile) in tiles {
        let cx = x + *dx as i32;
        let cy = y + *dy as i32;
        if plan.at(cx, cy) != id {
            return false;
        }
        if fit.at(cx, cy) & bits != 0 {
            return false;
        }
    }
    true
}

fn take(plan: &Plan, fit: &mut Fit, furn: usize, x: i32, y: i32, orient: usize, bits: u8) {
    let tiles: Vec<(i16, i16)> = match fit.cat.furniture.get(furn) {
        Some(piece) => piece.tiles(orient).iter().map(|(dx, dy, _)| (*dx, *dy)).collect(),
        None => return,
    };
    for (dx, dy) in tiles {
        fit.mark(x + dx as i32, y + dy as i32, bits);
    }
    let _ = plan;
}

/// The wishlist, placed against the walls. Each piece takes the first slot it
/// fits in, from a shuffled list, so two rooms of the same kind in the same
/// building are not furnished identically.
fn against_walls(plan: &mut Plan, fit: &mut Fit, id: i32, room: &Room, rng: &mut Rng) {
    let Some(def) = fit.cat.room(room_key(fit.cat, room)) else {
        return;
    };
    let wish: Vec<u32> = def.wish.clone();
    if wish.is_empty() {
        return;
    }
    let mut slots: Vec<(i32, i32, usize)> = Vec::new();
    wall_slots(plan, id, room, &mut slots);
    if slots.is_empty() {
        return;
    }
    rng.shuffle(&mut slots);
    let mut done: Vec<u32> = Vec::new();
    for role in wish {
        let Some(piece) = fit.cat.furniture.get(role as usize) else {
            continue;
        };
        if piece.once() && done.contains(&role) {
            continue;
        }
        let hung = piece.on_wall();
        // A painting shares its tile with the chest of drawers below it; a
        // chest does not share with a wardrobe.
        let bits = if hung { B_HUNG | B_DOOR } else { B_BLOCK };
        let mut put = None;
        for (x, y, facing) in &slots {
            if fits(plan, fit, id, role as usize, *x, *y, *facing, bits) {
                put = Some((*x, *y, *facing));
                break;
            }
        }
        let Some((x, y, facing)) = put else {
            continue;
        };
        let orient = fit
            .cat
            .furniture
            .get(role as usize)
            .map(|piece| piece.facing(facing))
            .unwrap_or(facing);
        take(plan, fit, role as usize, x, y, facing, if hung { B_HUNG } else { B_ITEM });
        plan.furniture.push(Piece {
            role: role as u16,
            x,
            y,
            orient: orient_of(orient),
        });
        done.push(role);
    }
}

fn orient_of(at: usize) -> crate::plan::Orient {
    match at {
        0 => crate::plan::Orient::West,
        1 => crate::plan::Orient::North,
        2 => crate::plan::Orient::East,
        _ => crate::plan::Orient::South,
    }
}

/// The centre group, nearest the middle of the room first, with a tile of
/// walkway all round it. Up to `repeat` copies, which is one living room suite
/// but ten tables down a canteen.
fn in_the_middle(plan: &mut Plan, fit: &mut Fit, id: i32, room: &Room, rng: &mut Rng) {
    if room.is_core || room.is_shaft {
        return;
    }
    let Some(def) = fit.cat.room(room_key(fit.cat, room)) else {
        return;
    };
    if def.groups.is_empty() {
        return;
    }
    // The group the room wants, then the smaller sets, until one goes down.
    for at in 0..def.groups.len() {
        let placed = {
            let group = &fit.cat.room(room_key(fit.cat, room)).unwrap().groups[at];
            let repeat = if at == 0 { group.repeat.max(1) } else { 1 };
            let laid = lay_out(group, room, fit);
            place_group(plan, fit, id, room, &laid, repeat, rng)
        };
        if placed > 0 {
            break;
        }
    }
}

/// One group's pieces, turned for the shape of the room and resolved to the
/// facing each piece can really be drawn in.
struct Laid {
    furn: usize,
    dx: i32,
    dy: i32,
    orient: usize,
    /// Tiles the piece covers, relative to its own square.
    cells: Vec<(i32, i32)>,
}

fn lay_out(group: &Group, room: &Room, fit: &Fit) -> Vec<Laid> {
    let turned = room.rect.w() < room.rect.h();
    let mut out = Vec::with_capacity(group.pieces.len());
    for piece in &group.pieces {
        let furn = piece.furn as usize;
        let (dx, dy, orient) = if turned {
            (
                piece.dy as i32,
                piece.dx as i32,
                TURN[piece.orient as usize & 3],
            )
        } else {
            (piece.dx as i32, piece.dy as i32, piece.orient as usize & 3)
        };
        let Some(known) = fit.cat.furniture.get(furn) else {
            continue;
        };
        let facing = known.facing(orient);
        let cells = known
            .tiles(orient)
            .iter()
            .map(|(cx, cy, _tile)| (*cx as i32, *cy as i32))
            .collect();
        out.push(Laid {
            furn,
            dx,
            dy,
            orient: facing,
            cells,
        });
    }
    out
}

fn place_group(
    plan: &mut Plan,
    fit: &mut Fit,
    id: i32,
    room: &Room,
    group: &[Laid],
    repeat: u32,
    rng: &mut Rng,
) -> u32 {
    if group.is_empty() {
        return 0;
    }
    // The group's own extent, so it can be offered every position inside the
    // room and sorted by how near the middle it lands.
    let mut x0 = i32::MAX;
    let mut y0 = i32::MAX;
    let mut x1 = i32::MIN;
    let mut y1 = i32::MIN;
    for piece in group {
        for (cx, cy) in &piece.cells {
            x0 = x0.min(piece.dx + cx);
            y0 = y0.min(piece.dy + cy);
            x1 = x1.max(piece.dx + cx);
            y1 = y1.max(piece.dy + cy);
        }
    }
    if x0 > x1 || y0 > y1 {
        return 0;
    }
    let gw = x1 - x0 + 1;
    let gh = y1 - y0 + 1;
    let mid_x = (room.rect.x0 + room.rect.x1) as f64 / 2.0;
    let mid_y = (room.rect.y0 + room.rect.y1) as f64 / 2.0;
    let mut spots: Vec<(i64, i32, i32)> = Vec::new();
    for y in room.rect.y0..=room.rect.y1 - gh + 1 {
        for x in room.rect.x0..=room.rect.x1 - gw + 1 {
            let cx = x as f64 + (gw as f64 - 1.0) / 2.0;
            let cy = y as f64 + (gh as f64 - 1.0) / 2.0;
            let away = ((cx - mid_x) * (cx - mid_x) + (cy - mid_y) * (cy - mid_y)) * 16.0;
            spots.push((away as i64, x, y));
        }
    }
    if spots.is_empty() {
        return 0;
    }
    // Nearest the middle first; a tie goes either way so two rooms of a size
    // are not furnished identically.
    rng.shuffle(&mut spots);
    spots.sort_by_key(|(away, _x, _y)| *away);
    let mut placed = 0u32;
    for (_away, sx, sy) in spots {
        if placed >= repeat {
            break;
        }
        let ox = sx - x0;
        let oy = sy - y0;
        // Every piece inside the room and clear, and the walkway round the
        // whole group clear of anything that blocks: a suite you cannot walk
        // round is a suite in the way.
        let mut ok = true;
        for piece in group {
            let bits = if fit
                .cat
                .furniture
                .get(piece.furn)
                .map(|known| known.flat())
                .unwrap_or(false)
            {
                // A rug lies under the rest and blocks nothing, so it only has
                // to be inside the room and off the doorways.
                B_DOOR | B_STAIR
            } else {
                B_BLOCK
            };
            if !fits(plan, fit, id, piece.furn, ox + piece.dx, oy + piece.dy, piece.orient, bits) {
                ok = false;
                break;
            }
        }
        if ok {
            for y in sy - 1..=sy + gh {
                for x in sx - 1..=sx + gw {
                    // The aisle may fall outside the room; what matters is that
                    // it is not another piece of furniture.
                    if plan.at(x, y) == id && fit.at(x, y) & B_ITEM != 0 {
                        ok = false;
                    }
                }
            }
        }
        if !ok {
            continue;
        }
        let mut added: Vec<(usize, i32, i32, usize)> = Vec::new();
        for piece in group {
            let x = ox + piece.dx;
            let y = oy + piece.dy;
            let flat = fit
                .cat
                .furniture
                .get(piece.furn)
                .map(|known| known.flat())
                .unwrap_or(false);
            take(
                plan,
                fit,
                piece.furn,
                x,
                y,
                piece.orient,
                if flat { 0 } else { B_ITEM },
            );
            plan.furniture.push(Piece {
                role: piece.furn as u16,
                x,
                y,
                orient: orient_of(piece.orient),
            });
            added.push((piece.furn, x, y, piece.orient));
        }
        // The walkway, so the next copy of the group keeps its distance.
        for y in sy - 1..=sy + gh {
            for x in sx - 1..=sx + gw {
                fit.mark(x, y, B_AISLE);
            }
        }
        seat_the_table(plan, fit, id, &added);
        placed += 1;
    }
    // The walkway is only there to space the groups out; with the room done it
    // would stop the next room's pieces reaching their own wall.
    for y in room.rect.y0 - 1..=room.rect.y1 + 1 {
        for x in room.rect.x0 - 1..=room.rect.x1 + 1 {
            fit.unmark(x, y, B_AISLE);
        }
    }
    placed
}

/// Chairs a table came without. A chair is tucked against the table, not given
/// an aisle of its own, so they go on after the group is down.
const TABLE_SEATS: usize = 2;

fn seat_the_table(plan: &mut Plan, fit: &mut Fit, id: i32, added: &[(usize, i32, i32, usize)]) {
    let Some(chair) = fit.cat.furn("chair") else {
        return;
    };
    let tables: Vec<(usize, i32, i32, usize)> = added
        .iter()
        .copied()
        .filter(|(furn, _x, _y, _o)| {
            fit.cat
                .furniture
                .get(*furn)
                .map(|known| known.table())
                .unwrap_or(false)
        })
        .collect();
    if tables.is_empty() {
        return;
    }
    // Only up to what the group would have seated anyway: a dining table that
    // came with four chairs is not given two more.
    let chairs = added.iter().filter(|(furn, ..)| *furn == chair).count();
    let already = chairs / tables.len();
    for (furn, tx, ty, orient) in tables {
        let mut seats = already;
        let cells: Vec<(i32, i32)> = match fit.cat.furniture.get(furn) {
            Some(known) => known
                .tiles(orient)
                .iter()
                .map(|(dx, dy, _tile)| (tx + *dx as i32, ty + *dy as i32))
                .collect(),
            None => continue,
        };
        for (cx, cy) in cells {
            for ((dx, dy), side) in [
                ((-1i32, 0i32), W),
                ((1, 0), E),
                ((0, -1), N),
                ((0, 1), S),
            ] {
                if seats >= TABLE_SEATS {
                    break;
                }
                let x = cx + dx;
                let y = cy + dy;
                if plan.at(x, y) != id || fit.at(x, y) & (B_ITEM | B_DOOR | B_STAIR) != 0 {
                    continue;
                }
                let facing = fit
                    .cat
                    .furniture
                    .get(chair)
                    .map(|known| known.facing(side))
                    .unwrap_or(side);
                take(plan, fit, chair, x, y, facing, B_ITEM);
                plan.furniture.push(Piece {
                    role: chair as u16,
                    x,
                    y,
                    orient: orient_of(facing),
                });
                seats += 1;
            }
        }
    }
}

/// Rooms nothing is put in, whatever their kind's list says: the lift shaft,
/// which is a sealed box, and a mall concourse, which is walked down rather than
/// furnished. Knox County's own concourse holds 16 pieces in 1,900 tiles.
fn skip(kind: RoomKind) -> bool {
    kind.is_sealed() || matches!(kind, RoomKind::ShaftBox | RoomKind::Concourse)
}
