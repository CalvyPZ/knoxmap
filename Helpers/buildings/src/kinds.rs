//! What each room is for.
//!
//! The rooms come out of the splitter as rectangles of various sizes, and this
//! decides which is the kitchen and which the bathroom. Two rules carry most of
//! the weight, both learnt from Knox County's own buildings in
//! knoxbuild/layout.py:
//!
//! * The rooms are handed out biggest first, from a list in the order a
//!   building of that sort wants them, so the living room is the big room and
//!   the bathroom lands on the small one that the splitter cut for it.
//! * There is exactly one of the rooms a building only has one of - one
//!   kitchen, one bathroom, one hall - and the rest fall back to the filler
//!   list, which is bedrooms in a house and offices in an office block.
//!
//! Rooms the storey decided before the cut - the corridor, the stair core, the
//! lift shaft - are marked `fixed` and keep the kind they were given.

use crate::blob::Kind;
use crate::plan::{Plan, RoomKind};
use crate::rng::Rng;

/// The order a building of this sort wants its rooms, biggest first, and what
/// to fill the rest with.
struct Mix {
    /// One of each, in this order, from the biggest room down.
    once: &'static [RoomKind],
    /// Repeated for every room past `once`.
    fill: &'static [RoomKind],
    /// Given to the smallest room, whatever the lists say. None leaves it.
    smallest: Option<RoomKind>,
    /// Given to anything too small to furnish.
    tiny: RoomKind,
}

const HOUSE_GROUND: Mix = Mix {
    once: &[
        RoomKind::LivingRoom,
        RoomKind::Kitchen,
        RoomKind::Dining,
        RoomKind::Hall,
    ],
    fill: &[RoomKind::Bedroom, RoomKind::Storage],
    smallest: Some(RoomKind::Bathroom),
    tiny: RoomKind::Closet,
};

const HOUSE_UPPER: Mix = Mix {
    once: &[
        RoomKind::Bedroom,
        RoomKind::Bedroom,
        RoomKind::Landing,
        RoomKind::Laundry,
    ],
    fill: &[RoomKind::Bedroom, RoomKind::KidsBedroom],
    smallest: Some(RoomKind::Bathroom),
    tiny: RoomKind::Closet,
};

const FLAT: Mix = Mix {
    once: &[RoomKind::LivingRoom, RoomKind::Kitchen, RoomKind::Hall],
    fill: &[RoomKind::Bedroom],
    smallest: Some(RoomKind::Bathroom),
    tiny: RoomKind::Closet,
};

const SHOP: Mix = Mix {
    once: &[RoomKind::Shop, RoomKind::StoreRoom],
    fill: &[RoomKind::Shop, RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Storage,
};

const OFFICE: Mix = Mix {
    once: &[RoomKind::Office, RoomKind::Lobby, RoomKind::Office],
    fill: &[RoomKind::Office, RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Closet,
};

const WAREHOUSE: Mix = Mix {
    once: &[RoomKind::Warehouse, RoomKind::Office],
    fill: &[RoomKind::Warehouse, RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Storage,
};

const SCHOOL: Mix = Mix {
    once: &[RoomKind::Canteen, RoomKind::Kitchen, RoomKind::Office],
    fill: &[RoomKind::ClassRoom, RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Storage,
};

const CLINIC: Mix = Mix {
    once: &[RoomKind::Lobby, RoomKind::Office],
    fill: &[RoomKind::Ward, RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Closet,
};

const CHURCH: Mix = Mix {
    once: &[RoomKind::Hall, RoomKind::Office],
    fill: &[RoomKind::Storage],
    smallest: None,
    tiny: RoomKind::Closet,
};

const BAR: Mix = Mix {
    once: &[RoomKind::BarRoom, RoomKind::Kitchen, RoomKind::StoreRoom],
    fill: &[RoomKind::Storage],
    smallest: Some(RoomKind::Restroom),
    tiny: RoomKind::Storage,
};

const GARAGE: Mix = Mix {
    once: &[RoomKind::Garage],
    fill: &[RoomKind::Storage],
    smallest: None,
    tiny: RoomKind::Storage,
};

const PLAIN_STORE: Mix = Mix {
    once: &[RoomKind::Storage],
    fill: &[RoomKind::Storage],
    smallest: None,
    tiny: RoomKind::Storage,
};

/// A building the map calls commercial but gives no trade for: the back of a
/// shop, the rooms above one. knoxbuild/layout.py's COMMERCIAL list.
const COMMERCIAL: Mix = Mix {
    once: &[
        RoomKind::Storage,
        RoomKind::Office,
        RoomKind::Storage,
        RoomKind::Kitchen,
        RoomKind::Hall,
        RoomKind::Laundry,
    ],
    fill: &[RoomKind::Storage, RoomKind::Office],
    smallest: Some(RoomKind::Bathroom),
    tiny: RoomKind::Storage,
};

/// A room smaller than this cannot hold the furniture of what it is called, so
/// it becomes a closet or a storeroom instead.
const TINY_AREA: i32 = 9;

/// `commercial` is what the map said about a building it gave no trade for: a
/// shop with nothing recorded about it, or the back rooms of one. It is not a
/// house, so it is not furnished as one.
fn mix_for(building: Kind, ground: bool, commercial: bool) -> &'static Mix {
    if commercial && matches!(building, Kind::House | Kind::Other) {
        return &COMMERCIAL;
    }
    match building {
        Kind::House => {
            if ground {
                &HOUSE_GROUND
            } else {
                &HOUSE_UPPER
            }
        }
        Kind::Apartment | Kind::Hotel => &FLAT,
        Kind::Retail | Kind::Shop | Kind::Bank => &SHOP,
        Kind::Office | Kind::Civic | Kind::Police | Kind::Fire | Kind::Library => &OFFICE,
        Kind::Warehouse | Kind::Factory | Kind::Army => &WAREHOUSE,
        Kind::School => &SCHOOL,
        Kind::Clinic => &CLINIC,
        Kind::Church => &CHURCH,
        Kind::Restaurant | Kind::Bar => &BAR,
        Kind::Mall => &SHOP,
        Kind::Garage => &GARAGE,
        Kind::Shed => &PLAIN_STORE,
        Kind::Other => {
            if ground {
                &HOUSE_GROUND
            } else {
                &HOUSE_UPPER
            }
        }
    }
}

/// Assign a kind to every room that has not already been given one.
///
/// `unit` groups the rooms of one dwelling: a block of flats assigns each flat
/// on its own, so every flat has its own kitchen and bathroom rather than the
/// whole floor sharing one of each.
pub fn assign(plan: &mut Plan, building: Kind, ground: bool, commercial: bool, rng: &mut Rng) {
    let areas = plan.room_areas();
    let mut units: Vec<u16> = Vec::new();
    for room in &plan.rooms {
        if !units.contains(&room.unit) {
            units.push(room.unit);
        }
    }
    units.sort_unstable();
    for unit in units {
        let mut members: Vec<usize> = (0..plan.rooms.len())
            .filter(|i| plan.rooms[*i].unit == unit && !plan.rooms[*i].fixed)
            .collect();
        if members.is_empty() {
            continue;
        }
        // Biggest first, and the room id breaks a tie so two rooms of the same
        // size do not swap places depending on how they were cut.
        members.sort_unstable_by(|a, b| {
            let (aa, ab) = (
                areas.get(*a + 1).copied().unwrap_or(0),
                areas.get(*b + 1).copied().unwrap_or(0),
            );
            ab.cmp(&aa).then(a.cmp(b))
        });
        if unit > 0 {
            // A dwelling inside a bigger building is a flat, and a flat holds
            // what a flat of that many rooms holds: the plan is a list, handed
            // out from the biggest room down, so the bathroom lands on the
            // smallest and every flat has one of each thing it needs.
            let open = plan.rooms[members[0]].kind == RoomKind::OpenPlan;
            let wanted = flat_plan(members.len(), open);
            assign_from_list(plan, &members, &areas, &wanted);
        } else {
            assign_members(plan, &members, &areas, mix_for(building, ground, commercial), rng);
        }
    }
}

/// Hand out a fixed list of kinds, biggest room first. Rooms past the list
/// become bedrooms, which is what an unusually large flat has more of.
fn assign_from_list(plan: &mut Plan, members: &[usize], areas: &[i32], wanted: &[RoomKind]) {
    for (place, index) in members.iter().enumerate() {
        let area = areas.get(index + 1).copied().unwrap_or(0);
        let kind = match wanted.get(place) {
            Some(kind) => *kind,
            None => RoomKind::Bedroom,
        };
        plan.rooms[*index].kind = if area < TINY_AREA {
            RoomKind::Closet
        } else {
            kind
        };
    }
}

fn assign_members(
    plan: &mut Plan,
    members: &[usize],
    areas: &[i32],
    mix: &Mix,
    rng: &mut Rng,
) {
    let n = members.len();
    // The smallest room is the bathroom, where the mix asks for one and there
    // is more than one room: a one-room flat is a bedsit, not a bathroom.
    let bath_at = if mix.smallest.is_some() && n > 1 {
        Some(n - 1)
    } else {
        None
    };
    let mut once_at = 0usize;
    for (place, index) in members.iter().enumerate() {
        let area = areas.get(index + 1).copied().unwrap_or(0);
        let kind = if Some(place) == bath_at {
            mix.smallest.unwrap_or(mix.tiny)
        } else if area < TINY_AREA {
            mix.tiny
        } else if once_at < mix.once.len() {
            let picked = mix.once[once_at];
            once_at += 1;
            picked
        } else if mix.fill.is_empty() {
            mix.tiny
        } else if mix.fill.len() == 1 {
            mix.fill[0]
        } else {
            // More than one filler: which one is a draw, so a floor of rooms
            // is not all the same thing in a row.
            *rng.choice(mix.fill).unwrap_or(&mix.tiny)
        };
        plan.rooms[*index].kind = kind;
    }
}

/// What a flat of this many rooms holds, in the order the rooms are handed out.
/// A small flat wants a bathroom more than it wants a separate bedroom.
pub fn flat_plan(rooms: usize, open: bool) -> Vec<RoomKind> {
    if open {
        return match rooms {
            0 | 1 => vec![RoomKind::OpenPlan],
            2 => vec![RoomKind::OpenPlan, RoomKind::Bathroom],
            3 => vec![RoomKind::OpenPlan, RoomKind::Bedroom, RoomKind::Bathroom],
            4 => vec![
                RoomKind::OpenPlan,
                RoomKind::Bedroom,
                RoomKind::Bedroom,
                RoomKind::Bathroom,
            ],
            _ => vec![
                RoomKind::Hall,
                RoomKind::OpenPlan,
                RoomKind::Bedroom,
                RoomKind::Bedroom,
                RoomKind::Bathroom,
            ],
        };
    }
    match rooms {
        0 | 1 => vec![RoomKind::Bedroom],
        2 => vec![RoomKind::Kitchen, RoomKind::Bathroom],
        3 => vec![RoomKind::Kitchen, RoomKind::Bedroom, RoomKind::Bathroom],
        4 => vec![
            RoomKind::LivingRoom,
            RoomKind::Kitchen,
            RoomKind::Bedroom,
            RoomKind::Bathroom,
        ],
        _ => vec![
            RoomKind::Hall,
            RoomKind::LivingRoom,
            RoomKind::Kitchen,
            RoomKind::Bedroom,
            RoomKind::Bathroom,
        ],
    }
}

/// How much bigger or smaller than a house's rooms this sort of building cuts
/// them. A warehouse is one big space; a school's classrooms are large rooms.
pub fn room_scale(building: Kind) -> f64 {
    match building {
        Kind::Warehouse | Kind::Factory => 6.0,
        Kind::Mall => 4.0,
        Kind::Church => 3.5,
        Kind::School => 2.0,
        Kind::Army => 2.5,
        Kind::Retail | Kind::Shop | Kind::Bank | Kind::Restaurant | Kind::Bar => 1.8,
        Kind::Office | Kind::Civic | Kind::Library | Kind::Police | Kind::Fire => 1.4,
        Kind::Clinic => 1.3,
        Kind::Garage => 2.0,
        Kind::Shed => 1.0,
        _ => 1.0,
    }
}

/// Does this sort of building have rooms off a corridor, rather than rooms
/// opening onto each other?
pub fn wants_corridor(building: Kind) -> bool {
    matches!(
        building,
        Kind::Apartment
            | Kind::Hotel
            | Kind::School
            | Kind::Clinic
            | Kind::Office
            | Kind::Civic
            | Kind::Library
            | Kind::Police
            | Kind::Fire
    )
}
