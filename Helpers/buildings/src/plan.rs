//! One storey being laid out: its rooms, its grid, and what is hung on it.
//!
//! The model follows BuildingEd, as knoxbuild/layout.py does. A storey is a
//! grid of 1-based room ids with 0 for a tile that is not part of the building,
//! and walls are not objects: BuildingEd draws a wall wherever two neighbouring
//! tiles hold different room ids. So laying out a storey is deciding which
//! room owns which tile, and nothing else.
//!
//! Doors and windows sit on the west or north *edge* of a tile, again as the
//! editor stores them, so a wall between tile (x-1, y) and (x, y) is the W edge
//! of (x, y).

use crate::grid::Side;

/// The smallest a room may be across, in tiles. Below this it is a cupboard
/// that the furniture pass cannot put anything in.
pub const MIN_ROOM: i32 = 3;
/// Only a guard against runaway recursion; the target area is what decides.
pub const MAX_DEPTH: u32 = 16;

/// An inclusive box of tiles.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Rect {
    pub x0: i32,
    pub y0: i32,
    pub x1: i32,
    pub y1: i32,
}

impl Rect {
    pub fn new(x0: i32, y0: i32, x1: i32, y1: i32) -> Rect {
        Rect { x0, y0, x1, y1 }
    }

    pub fn w(&self) -> i32 {
        self.x1 - self.x0 + 1
    }

    pub fn h(&self) -> i32 {
        self.y1 - self.y0 + 1
    }

    pub fn area(&self) -> i32 {
        self.w().max(0) * self.h().max(0)
    }

}

/// What a room is for. The layout reasons about these; the names are what the
/// catalog keys its colours, floors and furniture wishlists on, so they must
/// match the keys knoxbuild/layout.py's ROOM_STYLE uses.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum RoomKind {
    LivingRoom,
    Kitchen,
    OpenPlan,
    Bedroom,
    KidsBedroom,
    Bathroom,
    Dining,
    Hall,
    Storage,
    Closet,
    Corridor,
    Landing,
    Office,
    Shop,
    StoreRoom,
    Lobby,
    ClassRoom,
    Ward,
    Concourse,
    Warehouse,
    Garage,
    Laundry,
    Canteen,
    BarRoom,
    Restroom,
    ShaftBox,
}

impl RoomKind {
    /// The catalog key, which is the room name the game loads.
    pub fn name(self) -> &'static str {
        match self {
            RoomKind::LivingRoom => "livingroom",
            RoomKind::Kitchen => "kitchen",
            RoomKind::OpenPlan => "openplan",
            RoomKind::Bedroom => "bedroom",
            RoomKind::KidsBedroom => "kidsbedroom",
            RoomKind::Bathroom => "bathroom",
            RoomKind::Dining => "dining",
            RoomKind::Hall => "hall",
            RoomKind::Storage => "storage",
            RoomKind::Closet => "closet",
            RoomKind::Corridor => "corridor",
            RoomKind::Landing => "landing",
            RoomKind::Office => "office",
            RoomKind::Shop => "shop",
            RoomKind::StoreRoom => "storeroom",
            RoomKind::Lobby => "lobby",
            RoomKind::ClassRoom => "classroom",
            RoomKind::Ward => "ward",
            RoomKind::Concourse => "concourse",
            RoomKind::Warehouse => "warehouse",
            RoomKind::Garage => "garage",
            RoomKind::Laundry => "laundry",
            RoomKind::Canteen => "canteen",
            RoomKind::BarRoom => "barroom",
            RoomKind::Restroom => "restroom",
            RoomKind::ShaftBox => "closet",
        }
    }

    /// Circulation: everyone passes through it, so doors open onto it freely
    /// and the furniture pass keeps it clear.
    pub fn is_circulation(self) -> bool {
        matches!(
            self,
            RoomKind::Hall | RoomKind::Corridor | RoomKind::Landing | RoomKind::Lobby
                | RoomKind::Concourse
        )
    }

    /// A room nobody walks into: the lift shaft, sealed on every side.
    pub fn is_sealed(self) -> bool {
        matches!(self, RoomKind::ShaftBox)
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Room {
    pub rect: Rect,
    pub kind: RoomKind,
    /// Which dwelling this room belongs to. 0 is shared circulation.
    pub unit: u16,
    pub is_core: bool,
    pub is_shaft: bool,
    /// Its kind is already decided and the room mix must not overwrite it.
    pub fixed: bool,
    /// What the map says this room really is, as a 1-based catalog room: the
    /// pizza place's dining room, the bank's hall, the toy shop's stockroom.
    /// The layout still reasons about `kind` - a shop is a shop whatever it
    /// sells - but the name, the colour, the floor and the furniture all come
    /// from here instead. 0 is a room the map said nothing about.
    pub role: u32,
}

impl Room {
    pub fn new(rect: Rect, kind: RoomKind) -> Room {
        Room {
            rect,
            kind,
            unit: 0,
            is_core: false,
            is_shaft: false,
            fixed: false,
            role: 0,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Orient {
    West,
    North,
    East,
    South,
}

impl Orient {
    pub fn letter(self) -> &'static str {
        match self {
            Orient::West => "W",
            Orient::North => "N",
            Orient::East => "E",
            Orient::South => "S",
        }
    }
}

/// A door, window or wall fitting: the tile and which of its edges.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct EdgeAt {
    pub x: i32,
    pub y: i32,
    pub side: Side,
}

impl EdgeAt {
    pub fn new(x: i32, y: i32, side: Side) -> EdgeAt {
        EdgeAt { x, y, side }
    }
}

/// A piece of furniture: which role, where, and which way it faces.
#[derive(Clone, Copy, Debug)]
pub struct Piece {
    pub role: u16,
    pub x: i32,
    pub y: i32,
    pub orient: Orient,
}

pub struct Plan {
    pub width: i32,
    pub height: i32,
    pub rooms: Vec<Room>,
    /// 1-based room ids, row-major, 0 outside the building.
    pub grid: Vec<i32>,
    pub doors: Vec<EdgeAt>,
    pub windows: Vec<EdgeAt>,
    pub shop_front: Vec<EdgeAt>,
    pub furniture: Vec<Piece>,
    /// Edges shared with the building next door: nothing is cut into them.
    pub party: Vec<EdgeAt>,
    /// Tiles left open to the storey below, for a mall's gallery.
    pub void: Vec<(i32, i32)>,
    pub core: Option<Rect>,
    pub corridor: bool,
    pub shaft: Option<Rect>,
    pub shaft_door: Option<EdgeAt>,
    /// What this storey is, for rooms whose furniture depends on it.
    pub kind: Option<RoomKind>,
}

impl Plan {
    pub fn new(width: i32, height: i32) -> Plan {
        Plan {
            width,
            height,
            rooms: Vec::new(),
            grid: vec![0; (width.max(0) * height.max(0)) as usize],
            doors: Vec::new(),
            windows: Vec::new(),
            shop_front: Vec::new(),
            furniture: Vec::new(),
            party: Vec::new(),
            void: Vec::new(),
            core: None,
            corridor: false,
            shaft: None,
            shaft_door: None,
            kind: None,
        }
    }

    #[inline]
    pub fn at(&self, x: i32, y: i32) -> i32 {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return 0;
        }
        self.grid[(y * self.width + x) as usize]
    }

    #[inline]
    pub fn set(&mut self, x: i32, y: i32, id: i32) {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return;
        }
        self.grid[(y * self.width + x) as usize] = id;
    }


    /// Lay every room's rectangle into the grid, clipped to the footprint. Later
    /// rooms win where they overlap, which is how the core is painted last and
    /// always comes out as exactly one room.
    pub fn paint(&mut self, mask: &[bool]) {
        for cell in self.grid.iter_mut() {
            *cell = 0;
        }
        let width = self.width;
        let height = self.height;
        let rooms = self.rooms.clone();
        for (i, room) in rooms.iter().enumerate() {
            let id = i as i32 + 1;
            for y in room.rect.y0.max(0)..=room.rect.y1.min(height - 1) {
                for x in room.rect.x0.max(0)..=room.rect.x1.min(width - 1) {
                    // An empty mask is a building that fills its rectangle. A
                    // mask that does not reach this tile is treated as outside,
                    // so a short one cannot bring a whole building down.
                    if !mask.is_empty()
                        && !mask
                            .get((y * width + x) as usize)
                            .copied()
                            .unwrap_or(false)
                    {
                        continue;
                    }
                    self.set(x, y, id);
                }
            }
        }
    }

    /// How many tiles each room ended up with, indexed by room id.
    pub fn room_areas(&self) -> Vec<i32> {
        let mut out = vec![0i32; self.rooms.len() + 1];
        for cell in &self.grid {
            let id = *cell;
            if id > 0 && (id as usize) < out.len() {
                out[id as usize] += 1;
            }
        }
        out
    }

    /// Drop rooms that ended up with nothing (a rectangle wholly outside the
    /// footprint) or with too little to be a room, and renumber the grid so the
    /// ids stay 1..rooms.len() with no gaps, which is what the .tbx needs.
    pub fn drop_empty(&mut self, least: i32) {
        let areas = self.room_areas();
        let mut remap = vec![0i32; self.rooms.len() + 1];
        let mut kept: Vec<Room> = Vec::with_capacity(self.rooms.len());
        for (i, room) in self.rooms.iter().enumerate() {
            let id = i + 1;
            let area = areas.get(id).copied().unwrap_or(0);
            // A sealed shaft is kept whatever its size: it is a box, not a room
            // anybody has to stand in.
            if area >= least || (room.is_shaft && area > 0) {
                kept.push(*room);
                remap[id] = kept.len() as i32;
            }
        }
        for cell in self.grid.iter_mut() {
            let id = *cell;
            if id > 0 {
                *cell = remap.get(id as usize).copied().unwrap_or(0);
            }
        }
        self.rooms = kept;
        self.refit();
    }

    /// Shrink every room's rectangle to the tiles it actually holds. A room cut
    /// by the footprint keeps a box that reaches outside the building, and the
    /// furniture pass would try to stand things in the garden.
    pub fn refit(&mut self) {
        let mut boxes: Vec<Option<Rect>> = vec![None; self.rooms.len()];
        for y in 0..self.height {
            for x in 0..self.width {
                let id = self.at(x, y);
                if id <= 0 {
                    continue;
                }
                let at = id as usize - 1;
                if at >= boxes.len() {
                    continue;
                }
                match &mut boxes[at] {
                    Some(found) => {
                        found.x0 = found.x0.min(x);
                        found.y0 = found.y0.min(y);
                        found.x1 = found.x1.max(x);
                        found.y1 = found.y1.max(y);
                    }
                    None => boxes[at] = Some(Rect::new(x, y, x, y)),
                }
            }
        }
        for (room, found) in self.rooms.iter_mut().zip(boxes) {
            if let Some(rect) = found {
                room.rect = rect;
            }
        }
    }

    /// Mend a room the footprint cut in two: every piece but the biggest is
    /// given to a neighbour it touches, so no room is two places at once.
    ///
    /// A turned or L-shaped footprint does this often - a rectangle crossing
    /// the outside of the building comes back as two islands - and BuildingEd
    /// has no idea they are one room, so doors and furniture in one half never
    /// reach the other.
    pub fn mend_fragments(&mut self) {
        let width = self.width;
        let height = self.height;
        if width <= 0 || height <= 0 || self.rooms.is_empty() {
            return;
        }
        let mut label = vec![0i32; (width * height) as usize];
        let mut queue: Vec<i32> = Vec::new();
        // Per room: the pieces it came out as, as (size, first tile).
        let mut pieces: Vec<Vec<(i32, usize)>> = vec![Vec::new(); self.rooms.len() + 1];
        let mut next_label = 0i32;
        for start in 0..(width * height) as usize {
            let id = self.grid[start];
            if id <= 0 || label[start] != 0 {
                continue;
            }
            next_label += 1;
            let tag = next_label;
            label[start] = tag;
            queue.clear();
            queue.push(start as i32);
            let mut size = 0;
            while let Some(at) = queue.pop() {
                size += 1;
                let x = at % width;
                let y = at / width;
                let step = |nx: i32, ny: i32, queue: &mut Vec<i32>, label: &mut Vec<i32>| {
                    if nx < 0 || ny < 0 || nx >= width || ny >= height {
                        return;
                    }
                    let next = (ny * width + nx) as usize;
                    if label[next] == 0 && self.grid[next] == id {
                        label[next] = tag;
                        queue.push(next as i32);
                    }
                };
                step(x - 1, y, &mut queue, &mut label);
                step(x + 1, y, &mut queue, &mut label);
                step(x, y - 1, &mut queue, &mut label);
                step(x, y + 1, &mut queue, &mut label);
            }
            if let Some(list) = pieces.get_mut(id as usize) {
                list.push((size, start));
            }
        }

        // For every room with more than one piece, keep the biggest and hand
        // the rest to whichever neighbour touches them most.
        for id in 1..pieces.len() {
            if pieces[id].len() < 2 {
                continue;
            }
            let mut sorted = pieces[id].clone();
            sorted.sort_unstable_by(|a, b| b.0.cmp(&a.0));
            let keep_tag = label[sorted[0].1];
            for (_size, seed) in sorted.into_iter().skip(1) {
                let tag = label[seed];
                if tag == keep_tag {
                    continue;
                }
                let new_id = self.best_neighbour(&label, tag, id as i32);
                for at in 0..label.len() {
                    if label[at] == tag {
                        self.grid[at] = new_id;
                    }
                }
            }
        }
        self.refit();
    }

    /// Which room a stranded piece should join: the neighbour it shares the
    /// most wall with. 0 (outside) when it touches nothing, which leaves the
    /// tiles out of the building rather than inventing a room for them.
    fn best_neighbour(&self, label: &[i32], tag: i32, own: i32) -> i32 {
        let width = self.width;
        let height = self.height;
        let mut counts: Vec<(i32, i32)> = Vec::new();
        for at in 0..label.len() {
            if label[at] != tag {
                continue;
            }
            let x = at as i32 % width;
            let y = at as i32 / width;
            for (nx, ny) in [(x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)] {
                if nx < 0 || ny < 0 || nx >= width || ny >= height {
                    continue;
                }
                let other = self.grid[(ny * width + nx) as usize];
                if other <= 0 || other == own {
                    continue;
                }
                match counts.iter_mut().find(|(id, _)| *id == other) {
                    Some(found) => found.1 += 1,
                    None => counts.push((other, 1)),
                }
            }
        }
        // The most shared wall wins; the lowest id breaks a tie, so the result
        // does not depend on which tile happened to be walked first.
        counts.sort_unstable_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(&b.0)));
        counts.first().map(|(id, _)| *id).unwrap_or(0)
    }

    /// Which room owns this tile, as an index into `rooms`. None for outside,
    /// and for an id the room list does not reach: the callers index `rooms`
    /// with what they get back.
    pub fn room_of(&self, x: i32, y: i32) -> Option<usize> {
        let id = self.at(x, y);
        if id <= 0 || id as usize > self.rooms.len() {
            None
        } else {
            Some(id as usize - 1)
        }
    }

    pub fn is_party(&self, edge: &EdgeAt) -> bool {
        self.party.iter().any(|held| held == edge)
    }

    pub fn has_door(&self, edge: &EdgeAt) -> bool {
        self.doors.iter().any(|held| held == edge)
    }

    pub fn has_window(&self, edge: &EdgeAt) -> bool {
        self.windows.iter().any(|held| held == edge)
    }
}
