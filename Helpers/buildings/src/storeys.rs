//! A whole building: the storeys, the stairs between them, and the way in.
//!
//! Each storey is laid out separately rather than copied, because a block of
//! flats whose every floor is identical reads as a rendering error. Only the
//! staircase has to line up, and it does because the core is reserved first,
//! before any storey is cut.
//!
//! This is the shape of knoxbuild/layout.py's `build_building`: pick the core,
//! decide how many storeys can really be built, lay out each one, then do the
//! windows for the building as a whole so they stack in columns.

use crate::blob::{Job, Kind, PartyEdge};
use crate::catalog::Catalog;
use crate::cores::{
    pick_core, pick_corridor, pick_shaft, stairs_in_core, ELEVATOR_FROM_LEVELS, ELEVATOR_MIN_SIDE,
    STAIR_RUN,
};
use crate::doors::{back_door, front_door, interior_doors, outside_doors};
use crate::grid::Side;
use crate::kinds::{assign, room_scale, wants_corridor};
use crate::plan::{Plan, Rect, Room, RoomKind, MAX_DEPTH, MIN_ROOM};
use crate::results::Door;
use crate::rng::Rng;
use crate::split::{slices, split, target_area, Floor, MIN_SPLIT};
use crate::windows;

/// What a flat should come out at. Knox County's are 57 m2; a little over gives
/// room for the separate kitchen ours each need.
const FLAT_TARGET_AREA: i32 = 62;
/// A floorplate deeper than this gets two ranks of flats back to back, the way
/// a real block does, rather than one flat running the whole depth.
const FLAT_MAX_DEPTH: i32 = 11;
/// A hotel room is narrower than a flat: a room and its bathroom.
const HOTEL_FRONTAGE: (i32, i32) = (4, 6);
/// Open plan: one room that is the kitchen and the living room both. Knox County
/// gives only 0.56 kitchens to a flat; the rest have a kitchenette in the
/// living room, and the game draws a wall between any two rooms, so open plan is
/// one room rather than a wall left out.
const OPEN_PLAN_SHARE: f64 = 0.44;
/// Rooms off a corridor are only worth it where there are rooms enough to serve:
/// a two-room library with a corridor through it is a third corridor.
const CORRIDOR_WORTH_IT: f64 = 6.0;
/// From this many storeys a tower steps back, for light and wind. A 30-storey
/// block that went straight up from its footprint was a featureless box.
const SETBACK_FROM_LEVELS: u16 = 12;
const SETBACK_SHARE: f64 = 0.65;
const SETBACK_TILES: usize = 2;
/// The upper part has to keep at least this much of the floor to be worth it.
const SETBACK_MIN_KEEP: f64 = 0.5;

pub struct Built {
    pub storeys: Vec<Plan>,
    /// Per storey below the top: where its staircase rises from.
    pub stairs: Vec<(i32, i32, Side)>,
    /// The ground floor's outside doors, for the paths and yards pass.
    pub doors_out: Vec<Door>,
}

impl Built {
    pub fn rooms(&self) -> usize {
        self.storeys.iter().map(|storey| storey.rooms.len()).sum()
    }

    pub fn furniture(&self) -> usize {
        self.storeys
            .iter()
            .map(|storey| storey.furniture.len())
            .sum()
    }
}

/// The player's settings, as the layout uses them.
#[derive(Clone, Copy)]
pub struct Rules {
    /// The floor area one room should come to, in tiles. Bigger means fewer,
    /// larger rooms. knoxbuild/settings.py's default is 24.
    pub room_size: f64,
}

impl Default for Rules {
    fn default() -> Rules {
        Rules { room_size: 24.0 }
    }
}

impl Rules {
    /// The settings record this building was given, or the defaults.
    pub fn of(blob: &crate::blob::Blob, job: &Job) -> Rules {
        match blob.room_size(job) {
            Some(room_size) => Rules { room_size },
            None => Rules::default(),
        }
    }
}

/// Lay out one building.
pub fn build(
    job: &Job,
    mask: &[bool],
    party: &[PartyEdge],
    rules: &Rules,
    cat: &Catalog,
    uses: &[(u32, u32)],
) -> Built {
    let width = job.width as i32;
    let height = job.height as i32;
    let mut rng = Rng::seeded(job.seed as u64 ^ 0x5745);
    let mut levels = job.levels.max(1);

    // The core first: every storey has to agree about where the stairs are.
    let mut corridor = false;
    let mut core = None;
    let lodging = matches!(job.kind, Kind::Apartment | Kind::Hotel) || job.hotel();
    if lodging {
        core = pick_corridor(width, height, mask, &mut rng);
        corridor = core.is_some();
    } else if wants_corridor(job.kind) {
        let worth_it = (width * height) as f64
            >= CORRIDOR_WORTH_IT * 56.0 * rules.room_size * room_scale(job.kind);
        if worth_it {
            core = pick_corridor(width, height, mask, &mut rng);
            corridor = core.is_some();
        }
    }
    if core.is_none() && levels > 1 {
        core = pick_core(width, height, mask, &mut rng, job.street);
    }
    if levels > 1 && core.is_none() {
        levels = 1;             // nowhere to put a staircase, so one floor it is
    }
    let stairs = match (core, levels > 1) {
        (Some(rect), true) => Some(stairs_in_core(&rect)),
        _ => None,
    };
    let mut shaft = None;
    let mut shaft_door = None;
    if let Some(rect) = core {
        if levels >= ELEVATOR_FROM_LEVELS && width.min(height) >= ELEVATOR_MIN_SIDE {
            if let Some((found, door)) = pick_shaft(&rect, width, height, mask, stairs) {
                shaft = Some(found);
                shaft_door = Some(door);
            }
        }
    }

    // Shops under flats where the map says so, and wherever a shop, a
    // restaurant or a bank is in a building of more than one storey.
    let shops = matches!(job.kind, Kind::Apartment | Kind::Civic)
        && core.is_some()
        && ((job.retail() && job.kind == Kind::Apartment && levels >= 3)
            || (job.uses != u16::MAX && levels >= 2));

    let upper = setback_mask(mask, width, height, levels, core.is_some());
    let step_at = upper.as_ref().map(|_| {
        ((levels as f64 * SETBACK_SHARE).round() as u16).clamp(1, levels.saturating_sub(1))
    });

    let mut storeys: Vec<Plan> = Vec::with_capacity(levels as usize);
    for level in 0..levels {
        let use_mask = match (&upper, step_at) {
            (Some(small), Some(at)) if level >= at => small.as_slice(),
            _ => mask,
        };
        let floor_kind = if shops && level == 0 {
            Kind::Retail
        } else {
            job.kind
        };
        let mut plan = one_storey(
            job,
            cat,
            uses,
            floor_kind,
            use_mask,
            width,
            height,
            level,
            levels,
            core,
            corridor,
            shaft,
            shaft_door,
            rules,
            &mut rng,
        );
        // The walls this storey shares with the building next door. A
        // neighbour of three storeys shares nothing with our fourth.
        plan.party = party
            .iter()
            .filter(|edge| (level as u16) < edge.up_to)
            .map(|edge| crate::plan::EdgeAt {
                x: edge.x as i32,
                y: edge.y as i32,
                side: if edge.side == b'N' {
                    Side::North
                } else {
                    Side::West
                },
            })
            .collect();
        storeys.push(plan);
    }

    // Doors, ground floor first: the front door decides which room the rest of
    // the building is reached from.
    for (level, storey) in storeys.iter_mut().enumerate() {
        let entry = if level == 0 {
            let found = front_door(storey, job.street, &mut rng);
            if matches!(job.kind, Kind::House | Kind::Other) {
                back_door(storey, job.street, &mut rng);
            }
            found.map(|(_edge, room)| room)
        } else {
            // Upstairs, everything is reached from the landing.
            storey
                .rooms
                .iter()
                .position(|room| room.is_core)
                .or_else(|| storey.rooms.iter().position(|room| room.kind.is_circulation()))
        };
        interior_doors(storey, entry.unwrap_or(0));
        if let Some(door) = storey.shaft_door {
            // The lift doors are not a doorway: they are the fixture the
            // Elevators mod looks for, and the box behind them stays sealed.
            storey.doors.retain(|held| *held != door);
        }
    }

    // Glass along the street, not windows punched in brick: a floor of shops
    // under flats, and the whole ground floor of a building that is a shop.
    let glazed = shops
        || matches!(
            job.kind,
            Kind::Shop | Kind::Retail | Kind::Restaurant | Kind::Bar | Kind::Bank | Kind::Mall
        );
    windows::place(&mut storeys, job.kind, glazed, &mut rng);

    let mut flights = Vec::new();
    if let Some(found) = stairs {
        for _ in 0..levels.saturating_sub(1) {
            flights.push(found);
        }
    }
    let doors_out = storeys
        .first()
        .map(outside_doors)
        .unwrap_or_default();
    Built {
        storeys,
        stairs: flights,
        doors_out,
    }
}

/// The mask the storeys above the step use, or None when the building does not
/// step back.
fn setback_mask(mask: &[bool], width: i32, height: i32, levels: u16, core: bool) -> Option<Vec<bool>> {
    if levels < SETBACK_FROM_LEVELS || !core {
        return None;
    }
    let w = width.max(0) as usize;
    let h = height.max(0) as usize;
    if w == 0 || h == 0 {
        return None;
    }
    let base: Vec<bool> = if mask.is_empty() {
        vec![true; w * h]
    } else {
        mask.to_vec()
    };
    let mut stepped = Vec::new();
    crate::grid::erode(&base, w, h, SETBACK_TILES, SETBACK_TILES, &mut stepped);
    let before = base.iter().filter(|held| **held).count();
    let after = stepped.iter().filter(|held| **held).count();
    if before == 0 || (after as f64) < before as f64 * SETBACK_MIN_KEEP {
        return None;
    }
    Some(stepped)
}

#[allow(clippy::too_many_arguments)]
fn one_storey(
    job: &Job,
    cat: &Catalog,
    uses: &[(u32, u32)],
    floor_kind: Kind,
    mask: &[bool],
    width: i32,
    height: i32,
    level: u16,
    levels: u16,
    core: Option<Rect>,
    corridor: bool,
    shaft: Option<Rect>,
    shaft_door: Option<crate::plan::EdgeAt>,
    rules: &Rules,
    rng: &mut Rng,
) -> Plan {
    let mut plan = Plan::new(width, height);
    plan.core = core;
    plan.corridor = corridor;
    plan.shaft = shaft;
    plan.shaft_door = shaft_door;
    let floor = Floor::new(mask, width.max(0) as usize, height.max(0) as usize);
    let target = target_area(rules.room_size, room_scale(floor_kind));

    let mut rects: Vec<Rect> = Vec::new();
    let mut units: Vec<u16> = Vec::new();
    // Flats either side of the corridor, but not on a floor of shops.
    let lodging = matches!(job.kind, Kind::Apartment | Kind::Hotel) || job.hotel();
    let flats = match core {
        Some(rect)
            if corridor
                && lodging
                && !matches!(floor_kind, Kind::Retail) =>
        {
            Some(rect)
        }
        _ => None,
    };
    if let Some(core) = flats {
        cut_flats(
            core,
            width,
            height,
            &floor,
            job.kind == Kind::Hotel || job.hotel(),
            rng,
            &mut rects,
            &mut units,
        );
    } else {
        // The whole plate, less the core, cut down to rooms.
        let mut small = matches!(floor_kind, Kind::House | Kind::Other);
        let region = Rect::new(0, 0, width - 1, height - 1);
        split(
            region,
            rng,
            MAX_DEPTH,
            target,
            &floor,
            &mut small,
            &mut rects,
        );
        units = vec![0; rects.len()];
    }

    for (rect, unit) in rects.into_iter().zip(units.into_iter()) {
        let mut room = Room::new(rect, RoomKind::Storage);
        room.unit = unit;
        // Whether this flat is open plan is decided when it is cut, not after: a
        // flat already divided into four rooms cannot be made open by renaming
        // one of them.
        plan.rooms.push(room);
    }
    if flats.is_some() {
        mark_open_plan(&mut plan, rng);
    }

    // The core is painted last so it always comes out as exactly one room,
    // whatever the room cut did around it.
    if let Some(rect) = core {
        let kind = if corridor {
            RoomKind::Corridor
        } else {
            RoomKind::Landing
        };
        let mut room = Room::new(rect, kind);
        room.is_core = true;
        room.fixed = true;
        plan.rooms.push(room);
    }
    if let Some(rect) = shaft {
        let mut room = Room::new(rect, RoomKind::ShaftBox);
        room.is_shaft = true;
        room.fixed = true;
        plan.rooms.push(room);
    }

    plan.paint(mask);
    // A room of fewer tiles than this is a sliver the footprint left, not a
    // room: it goes back to whichever neighbour it touches.
    plan.drop_empty(MIN_ROOM);
    plan.mend_fragments();
    plan.drop_empty(MIN_ROOM);
    assign(&mut plan, floor_kind, level == 0, job.commercial(), rng);
    // What the map says is really on this street: the pizza place and its
    // kitchen, the bank hall, the vet's surgery. The mix named every room a
    // moment ago; this says what the ones on the pavement are instead.
    if level == 0 && matches!(floor_kind, Kind::Shop | Kind::Retail | Kind::Restaurant) {
        let mut list: Vec<(u32, u32)> = uses.to_vec();
        if list.is_empty() && floor_kind == Kind::Restaurant {
            // A restaurant with nothing recorded about it is still a restaurant.
            let front = cat.role_of("restaurantdining");
            if front != 0 {
                list.push((front, cat.role_of("restaurantkitchen")));
            }
        }
        crate::uses::shop_floor(
            &mut plan,
            cat,
            job.street,
            &list,
            floor_kind == Kind::Retail,
            rng,
        );
    }
    // The core and the shaft keep what they were given; `fixed` survived the
    // renumbering because the rooms were dropped, not reordered.
    plan.kind = Some(match floor_kind {
        Kind::Retail | Kind::Shop => RoomKind::Shop,
        Kind::Warehouse | Kind::Factory => RoomKind::Warehouse,
        Kind::Office | Kind::Civic => RoomKind::Office,
        Kind::School => RoomKind::ClassRoom,
        Kind::Clinic => RoomKind::Ward,
        Kind::Mall => RoomKind::Concourse,
        _ => RoomKind::LivingRoom,
    });
    let _ = levels;
    plan
}

/// Slice the floor either side of the corridor into flats, and cut each flat
/// into its rooms.
#[allow(clippy::too_many_arguments)]
fn cut_flats(
    core: Rect,
    width: i32,
    height: i32,
    floor: &Floor,
    hotel: bool,
    rng: &mut Rng,
    rects: &mut Vec<Rect>,
    units: &mut Vec<u16>,
) {
    let along_y = core.h() >= core.w();
    let frontage = if hotel {
        HOTEL_FRONTAGE
    } else {
        // A flat wants about FLAT_TARGET_AREA of floor however deep the plate
        // is, so the frontage falls as the plate gets deeper.
        let depth = if along_y {
            (core.x0).max(width - 1 - core.x1).max(1)
        } else {
            (core.y0).max(height - 1 - core.y1).max(1)
        };
        let want = (FLAT_TARGET_AREA as f64 / depth as f64).round() as i32;
        let low = want.max(MIN_ROOM + 1).min(FLAT_MAX_DEPTH);
        (low, low + 2)
    };
    let mut unit = 0u16;
    // The two bands either side of the corridor, each cut into frontages along
    // its length.
    let bands: [(i32, i32); 2] = if along_y {
        [(0, core.x0 - 1), (core.x1 + 1, width - 1)]
    } else {
        [(0, core.y0 - 1), (core.y1 + 1, height - 1)]
    };
    for (lo, hi) in bands {
        if hi < lo {
            continue;
        }
        let span = if along_y { core.h() } else { core.w() };
        let base = if along_y { core.y0 } else { core.x0 };
        for (a, b) in slices(span, rng, frontage.0, frontage.1) {
            unit += 1;
            let flat = if along_y {
                Rect::new(lo, base + a, hi, base + b)
            } else {
                Rect::new(base + a, lo, base + b, hi)
            };
            if floor.inside(&flat) < MIN_ROOM * MIN_ROOM {
                continue;       // the footprint has cut this one away
            }
            let mut rooms: Vec<Rect> = Vec::new();
            let mut small = true;
            // A deep plate takes two ranks of flats back to back rather than one
            // flat running the whole depth of the building.
            let deep = if along_y { flat.w() } else { flat.h() };
            if deep > FLAT_MAX_DEPTH && deep >= MIN_SPLIT * 2 {
                let half = deep / 2;
                let (near, far) = if along_y {
                    (
                        Rect::new(flat.x0, flat.y0, flat.x0 + half - 1, flat.y1),
                        Rect::new(flat.x0 + half, flat.y0, flat.x1, flat.y1),
                    )
                } else {
                    (
                        Rect::new(flat.x0, flat.y0, flat.x1, flat.y0 + half - 1),
                        Rect::new(flat.x0, flat.y0 + half, flat.x1, flat.y1),
                    )
                };
                for part in [near, far] {
                    let mut own: Vec<Rect> = Vec::new();
                    let mut own_small = true;
                    split(part, rng, MAX_DEPTH, FLAT_TARGET_AREA / 3, floor, &mut own_small, &mut own);
                    for rect in own {
                        rooms.push(rect);
                        units.push(unit);
                    }
                    unit += 1;
                }
                // The rank beyond the corridor is its own flat, counted above.
                unit -= 1;
                rects.extend(rooms);
                continue;
            }
            split(
                flat,
                rng,
                MAX_DEPTH,
                FLAT_TARGET_AREA / 3,
                floor,
                &mut small,
                &mut rooms,
            );
            for rect in rooms {
                rects.push(rect);
                units.push(unit);
            }
        }
    }
    let _ = STAIR_RUN;
}

/// Some flats are open plan: the living end is one room that is also the
/// kitchen. Decided per flat, so a block has both sorts in it.
fn mark_open_plan(plan: &mut Plan, rng: &mut Rng) {
    let mut units: Vec<u16> = Vec::new();
    for room in &plan.rooms {
        if room.unit > 0 && !units.contains(&room.unit) {
            units.push(room.unit);
        }
    }
    for unit in units {
        if !rng.chance(OPEN_PLAN_SHARE) {
            continue;
        }
        // The biggest room of the flat becomes the open one; `assign` reads it
        // back to know the flat is open.
        let mut best: Option<(i32, usize)> = None;
        for (i, room) in plan.rooms.iter().enumerate() {
            if room.unit != unit {
                continue;
            }
            let area = room.rect.area();
            if best.is_none() || area > best.unwrap().0 {
                best = Some((area, i));
            }
        }
        if let Some((_area, at)) = best {
            plan.rooms[at].kind = RoomKind::OpenPlan;
        }
    }
}
