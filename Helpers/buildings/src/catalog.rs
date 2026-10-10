//! The tile, furniture, room and style tables, as the workers read them.
//!
//! knoxbuild/catalog.py is 1.4 MB of generated tables: every tile name
//! BuildingEd knows, the furniture pieces and their offsets, the room colours,
//! the wall and roof styles. Loading that into a worker process is what made
//! workers expensive, so none of it is loaded here. Python serialises the
//! resolved tables into the blob once, and this module reads them.
//!
//! The split is: the tables are data, the decisions are code. Which floor a
//! bathroom gets is decided here (tbx.rs), from the list of floors Knox County
//! lays in a bathroom, which comes from there.
//!
//! Nothing in here is a tile *name*. Everything is a string id into the blob's
//! string table, so a name the catalog repeats ten thousand times is stored
//! once and compared as a u32.

use std::collections::HashMap;

use crate::blob::Blob;
use crate::err::{Error, Result};

/// One row of the .tbx tile-entry table: a category, and the tiles that fill
/// the roles of that category ("West", "NorthDoor", "Slope30S1", ...).
///
/// The pairs keep Python's order, because that is the order BuildingEd's own
/// files list them in and the order the .tbx is written in. Matching, which has
/// to ignore order, sorts a copy (`fingerprint`).
pub struct Entry {
    pub category: u32,
    pub pairs: Vec<(u32, u32)>,
}

impl Entry {
    /// The tile filling one role, or None.
    pub fn tile(&self, role: u32) -> Option<u32> {
        self.pairs
            .iter()
            .find(|(name, _tile)| *name == role)
            .map(|(_name, tile)| *tile)
    }

    /// Category and roles in sorted order: what makes two entries the same row.
    pub fn fingerprint(&self) -> (u32, Vec<(u32, u32)>) {
        let mut pairs = self.pairs.clone();
        pairs.sort_unstable();
        (self.category, pairs)
    }
}

/// One furniture piece: which tiles it is made of, facing each way, and where
/// they sit relative to the piece's own square.
pub struct Furn {
    pub name: u32,
    /// "Furniture", "WallOverlay", "FloorFurniture"... Leaving this off a wall
    /// piece drops it onto the floor layer, which is how a painting ends up
    /// standing in the middle of a room.
    pub layer: u32,
    pub flags: u32,
    /// W, N, E, S. Empty where the piece has no tiles facing that way.
    pub orients: [Vec<(i16, i16, u32)>; 4],
}

/// A room has one of these however big it is: one sofa, one stove, one bath.
pub const FURN_ONCE: u32 = 1 << 0;
/// Hung on the wall rather than standing on the floor. A wall piece shares its
/// tile with whatever stands there; a rug lies on its own layer but is a floor
/// piece, because its 2x2 does have to be inside the room.
pub const FURN_WALL: u32 = 1 << 1;
/// Lies flat and blocks nothing: a rug, a mat.
pub const FURN_FLAT: u32 = 1 << 2;
/// A table: chairs are tucked against it after a centre group goes down.
pub const FURN_TABLE: u32 = 1 << 3;

impl Furn {
    pub fn once(&self) -> bool {
        self.flags & FURN_ONCE != 0
    }
    pub fn on_wall(&self) -> bool {
        self.flags & FURN_WALL != 0
    }
    pub fn flat(&self) -> bool {
        self.flags & FURN_FLAT != 0
    }
    pub fn table(&self) -> bool {
        self.flags & FURN_TABLE != 0
    }
    /// The tiles this piece covers facing `orient`, as offsets from its square.
    /// Falls back to a facing it does have: a piece with only W and N tiles is
    /// still placed against an east wall, drawn the other way round.
    pub fn tiles(&self, orient: usize) -> &[(i16, i16, u32)] {
        if !self.orients[orient].is_empty() {
            return &self.orients[orient];
        }
        // W<->E and N<->S, then anything at all, as _facing does.
        let alt = [2usize, 3, 0, 1][orient];
        if !self.orients[alt].is_empty() {
            return &self.orients[alt];
        }
        for held in &self.orients {
            if !held.is_empty() {
                return held;
            }
        }
        &[]
    }

    /// Which facing to actually emit for a piece put against `orient`.
    pub fn facing(&self, orient: usize) -> usize {
        if !self.orients[orient].is_empty() {
            return orient;
        }
        let alt = [2usize, 3, 0, 1][orient];
        if !self.orients[alt].is_empty() {
            return alt;
        }
        for (at, held) in self.orients.iter().enumerate() {
            if !held.is_empty() {
                return at;
            }
        }
        orient
    }
}

/// One piece of a centre group: what stands in the middle of a room, where it
/// sits in the group, and which way its back faces.
pub struct GroupPiece {
    pub furn: u32,
    pub dx: i16,
    pub dy: i16,
    /// 0 = W, 1 = N, 2 = E, 3 = S.
    pub orient: u16,
}

/// The sofa, the coffee table and the television, as one thing to place. The
/// first group of a room is what it wants; the rest are smaller sets tried when
/// that one does not fit.
pub struct Group {
    /// How many copies a big room may take: one living room suite, but ten
    /// tables down a canteen.
    pub repeat: u32,
    pub pieces: Vec<GroupPiece>,
}

/// Flags on a room definition.
const ROOM_CEILING: u32 = 1 << 0;

/// What one kind of room is, in the game's terms and in ours.
pub struct RoomDef {
    /// Our key: "bathroom", "openplan", "concourse".
    pub name: u32,
    /// What the .tbx calls it, which is what the loot tables key off.
    pub game_name: u32,
    /// "170 170 170", as BuildingEd stores a room colour.
    pub color: u32,
    /// The tile entry this kind falls back to when nothing else applies.
    pub floor: u32,
    pub flags: u32,
    /// Floor entries this kind is laid with, in the shares Knox County lays
    /// them: a repeated entry is simply likelier.
    pub floors: Vec<u32>,
    /// Furniture that wants a wall, in the order it is placed.
    pub wish: Vec<u32>,
    /// What stands in the middle: the group this kind of room wants, then the
    /// smaller ones to fall back on.
    pub groups: Vec<Group>,
}

impl RoomDef {
    pub fn ceiling(&self) -> bool {
        self.flags & ROOM_CEILING != 0
    }
}

/// Floors for the rooms of one sort of building: a station's corridor is not
/// somebody's hallway.
pub struct KindFloors {
    pub kind: u32,
    pub rooms: Vec<(u32, Vec<u32>)>,
}

const STYLE_PEAKED: u32 = 1 << 0;
const STYLE_ROOF30: u32 = 1 << 1;
/// The style has a `curtains` key, even if it is empty (meaning no curtains).
/// Without the key the shared catalog's curtains are used.
const STYLE_HAS_CURTAINS: u32 = 1 << 2;

/// A building's materials. Every index is 1-based into the catalog's entry
/// table, with 0 meaning "nothing", as BuildingEd's indices do.
pub struct Style {
    /// The style's own name, for diagnostics. Parsed because it is in the
    /// record; nothing in the layout decides anything from it.
    #[allow(dead_code)]
    pub name: u32,
    pub exterior: u32,
    pub interior: u32,
    pub floor: u32,
    pub window: u32,
    pub curtains: u32,
    pub trim: u32,
    pub shutters: u32,
    pub grime: u32,
    pub slopes: u32,
    pub tops: u32,
    pub caps: u32,
    pub shop_front: u32,
    pub shop_curtains: u32,
    pub flags: u32,
    /// Taller buildings of a kind take bigger windows: (storeys, window,
    /// curtains), and the last row the building reaches wins.
    pub by_levels: Vec<(u32, u32, u32)>,
}

impl Style {
    pub fn peaked(&self) -> bool {
        self.flags & STYLE_PEAKED != 0
    }
    pub fn roof30(&self) -> bool {
        self.flags & STYLE_ROOF30 != 0
    }
    pub fn has_curtains(&self) -> bool {
        self.flags & STYLE_HAS_CURTAINS != 0
    }
}

/// The 1-based entry indexes the writer needs by name. These are constants in
/// catalog.py; they are sent rather than hardcoded so a regenerated catalog
/// cannot silently shift them.
#[derive(Clone, Copy, Default)]
pub struct Fixed {
    pub exterior_wall: u32,
    pub door: u32,
    pub door_frame: u32,
    pub window: u32,
    pub curtains: u32,
    pub stairs: u32,
    pub interior_wall: u32,
    pub roof_cap: u32,
    pub roof_slope: u32,
    pub roof_top: u32,
    pub ceiling: u32,
    pub interior_wall_trim: u32,
}

/// The roles the writer has to name: it reads the exterior entry's own West and
/// North tiles to build this building's roof cap, so that a flat roof walls in
/// its top storey with the building's own material instead of the stucco
/// BuildingTemplates.txt defaults to.
#[derive(Clone, Copy, Default)]
pub struct Roles {
    pub west: u32,
    pub north: u32,
    pub cap_gap_e3: u32,
    pub cap_gap_s3: u32,
    /// The role a floor entry fills. Python builds the floor rows, so nothing
    /// here names it yet.
    #[allow(dead_code)]
    pub floor: u32,
}

pub struct Catalog {
    /// The shared rows, 1-based: `entries[0]` is index 1. Base rows first, in
    /// catalog.py's order, then every row any style or floor list needs.
    pub entries: Vec<Entry>,
    /// How many rows are the shared base table, which every building writes
    /// unchanged.
    pub n_base: usize,
    pub furniture: Vec<Furn>,
    pub rooms: Vec<RoomDef>,
    pub kinds: Vec<KindFloors>,
    /// Entry indexes of the interior wall sets a building paints its rooms from.
    pub int_walls: Vec<u32>,
    pub styles: Vec<Style>,
    /// (exterior, interior, door) triples for a glass shop front.
    pub storefronts: Vec<(u32, u32, u32)>,
    pub fixed: Fixed,
    pub roles: Roles,
    /// For each row, the first base row with the same tiles, or 0. A style whose
    /// interior wall is already one of the shared rows is written once, which is
    /// what keeps a building's tile table the size the game's own are.
    pub base_dup: Vec<u32>,
    /// The shops a commercial front becomes where the map does not say what is
    /// sold there: (role, weight), the role 1-based as a room records it.
    /// knoxbuild/interiors.py's STORE_WEIGHTS.
    pub stores: Vec<(u32, u32)>,
    /// Each room's key, by index, so a room the map named can be looked up the
    /// same way one of ours is.
    names: Vec<String>,
    /// Our room key -> index into `rooms`.
    by_name: HashMap<String, usize>,
    /// Building kind name -> index into `kinds`.
    kind_by_name: HashMap<String, usize>,
    /// Furniture role name -> index into `furniture`.
    furn_by_name: HashMap<String, usize>,
}

impl Catalog {
    pub fn room(&self, name: &str) -> Option<&RoomDef> {
        self.by_name.get(name).map(|at| &self.rooms[*at])
    }

    /// The key of a room by index. `role` is 1-based, as a room records it.
    pub fn role_name(&self, role: u32) -> Option<&str> {
        if role == 0 {
            return None;
        }
        self.names.get(role as usize - 1).map(|name| name.as_str())
    }

    /// 1-based index of a room by key, for handing one out as a role.
    pub fn role_of(&self, name: &str) -> u32 {
        match self.by_name.get(name) {
            Some(at) => *at as u32 + 1,
            None => 0,
        }
    }

    /// A shop with nothing recorded about it: one of the trades a high street
    /// is made of, by how common each is.
    pub fn store(&self, roll: u32) -> u32 {
        let total: u32 = self.stores.iter().map(|(_room, weight)| *weight).sum();
        if total == 0 {
            return 0;
        }
        let mut want = roll % total;
        for (room, weight) in &self.stores {
            if want < *weight {
                return *room;
            }
            want -= *weight;
        }
        self.stores[0].0
    }

    pub fn floors_for(&self, building: &str, room: &str) -> Option<&[u32]> {
        // The building's own floors first: a school's classrooms are not laid
        // with the generic list, and the generic list has no classroom in it.
        if let Some(at) = self.kind_by_name.get(building) {
            let name = self.room(room).map(|def| def.name);
            if let Some(id) = name {
                for (room_id, floors) in &self.kinds[*at].rooms {
                    if *room_id == id {
                        return Some(floors);
                    }
                }
            }
        }
        self.room(room).map(|def| def.floors.as_slice())
    }

    pub fn furn(&self, name: &str) -> Option<usize> {
        self.furn_by_name.get(name).copied()
    }

    pub fn entry(&self, index: u32) -> Option<&Entry> {
        if index == 0 {
            return None;
        }
        self.entries.get(index as usize - 1)
    }

    pub fn style(&self, index: u16) -> Option<&Style> {
        self.styles.get(index as usize)
    }

    /// Roughly what the tables hold, for the memory report.
    pub fn bytes(&self) -> usize {
        let mut total = 0;
        for entry in &self.entries {
            total += 24 + entry.pairs.capacity() * 8;
        }
        for piece in &self.furniture {
            total += 32;
            for orient in &piece.orients {
                total += orient.capacity() * 8;
            }
        }
        for room in &self.rooms {
            total += 64 + (room.floors.capacity() + room.wish.capacity()) * 4;
            for group in &room.groups {
                total += 32 + group.pieces.capacity() * 12;
            }
        }
        total += self.styles.len() * 96 + self.int_walls.capacity() * 4;
        total += self.stores.capacity() * 8;
        for name in &self.names {
            total += 24 + name.capacity();
        }
        total + self.by_name.len() * 48 + self.furn_by_name.len() * 48
    }

    /// Read the tables. Called once, when the session opens, so a bad table is
    /// one error back to Python rather than a thousand failed buildings.
    pub fn parse(blob: &Blob) -> Result<Catalog> {
        let bytes = blob.catalog_bytes();
        let head = Head::read(bytes)?;
        let mut entries = Vec::with_capacity(head.n_entries);
        let mut at = Cursor::at(bytes, head.entries_off)?;
        for _ in 0..head.n_entries {
            let category = at.u32()?;
            let n = at.count()?;
            let mut pairs = Vec::with_capacity(n);
            for _ in 0..n {
                let role = at.u32()?;
                pairs.push((role, at.u32()?));
            }
            entries.push(Entry { category, pairs });
        }

        let mut furniture = Vec::with_capacity(head.n_furniture);
        let mut at = Cursor::at(bytes, head.furniture_off)?;
        for _ in 0..head.n_furniture {
            let name = at.u32()?;
            let layer = at.u32()?;
            let flags = at.u32()?;
            let mut orients: [Vec<(i16, i16, u32)>; 4] =
                [Vec::new(), Vec::new(), Vec::new(), Vec::new()];
            for orient in orients.iter_mut() {
                let n = at.count()?;
                orient.reserve(n);
                for _ in 0..n {
                    let dx = at.i16()?;
                    let dy = at.i16()?;
                    orient.push((dx, dy, at.u32()?));
                }
            }
            furniture.push(Furn {
                name,
                layer,
                flags,
                orients,
            });
        }

        let mut rooms = Vec::with_capacity(head.n_rooms);
        let mut at = Cursor::at(bytes, head.rooms_off)?;
        for _ in 0..head.n_rooms {
            let name = at.u32()?;
            let game_name = at.u32()?;
            let color = at.u32()?;
            let floor = at.u32()?;
            let flags = at.u32()?;
            let floors = at.list()?;
            let wish = at.list()?;
            let n_groups = at.count()?;
            let mut groups = Vec::with_capacity(n_groups);
            for _ in 0..n_groups {
                let repeat = at.u32()?;
                let n = at.count()?;
                let mut pieces = Vec::with_capacity(n);
                for _ in 0..n {
                    let furn = at.u32()?;
                    let dx = at.i16()?;
                    let dy = at.i16()?;
                    pieces.push(GroupPiece {
                        furn,
                        dx,
                        dy,
                        orient: at.i16()? as u16 & 3,
                    });
                }
                groups.push(Group { repeat, pieces });
            }
            rooms.push(RoomDef {
                name,
                game_name,
                color,
                floor,
                flags,
                floors,
                wish,
                groups,
            });
        }

        let mut kinds = Vec::with_capacity(head.n_kinds);
        let mut at = Cursor::at(bytes, head.kinds_off)?;
        for _ in 0..head.n_kinds {
            let kind = at.u32()?;
            let n = at.count()?;
            let mut per_room = Vec::with_capacity(n);
            for _ in 0..n {
                let room = at.u32()?;
                per_room.push((room, at.list()?));
            }
            kinds.push(KindFloors {
                kind,
                rooms: per_room,
            });
        }

        let mut at = Cursor::at(bytes, head.int_walls_off)?;
        let mut int_walls = Vec::with_capacity(head.n_int_walls);
        for _ in 0..head.n_int_walls {
            int_walls.push(at.u32()?);
        }

        let mut styles = Vec::with_capacity(head.n_styles);
        let mut at = Cursor::at(bytes, head.styles_off)?;
        for _ in 0..head.n_styles {
            let name = at.u32()?;
            let exterior = at.u32()?;
            let interior = at.u32()?;
            let floor = at.u32()?;
            let window = at.u32()?;
            let curtains = at.u32()?;
            let trim = at.u32()?;
            let shutters = at.u32()?;
            let grime = at.u32()?;
            let slopes = at.u32()?;
            let tops = at.u32()?;
            let caps = at.u32()?;
            let shop_front = at.u32()?;
            let shop_curtains = at.u32()?;
            let flags = at.u32()?;
            let n = at.count()?;
            let mut by_levels = Vec::with_capacity(n);
            for _ in 0..n {
                let levels = at.u32()?;
                let window = at.u32()?;
                by_levels.push((levels, window, at.u32()?));
            }
            styles.push(Style {
                name,
                exterior,
                interior,
                floor,
                window,
                curtains,
                trim,
                shutters,
                grime,
                slopes,
                tops,
                caps,
                shop_front,
                shop_curtains,
                flags,
                by_levels,
            });
        }

        let mut at = Cursor::at(bytes, head.storefronts_off)?;
        let mut storefronts = Vec::with_capacity(head.n_storefronts);
        for _ in 0..head.n_storefronts {
            let ext = at.u32()?;
            let inside = at.u32()?;
            storefronts.push((ext, inside, at.u32()?));
        }

        let mut at = Cursor::at(bytes, head.stores_off)?;
        let mut stores = Vec::with_capacity(head.n_stores);
        for _ in 0..head.n_stores {
            let role = at.u32()?;
            let weight = at.u32()?;
            if role >= 1 && role as usize <= rooms.len() {
                stores.push((role, weight));
            }
        }

        let mut names = Vec::with_capacity(rooms.len());
        let mut by_name = HashMap::with_capacity(rooms.len());
        for (at, room) in rooms.iter().enumerate() {
            let name = blob.string(room.name).to_string();
            by_name.insert(name.clone(), at);
            names.push(name);
        }
        let mut kind_by_name = HashMap::with_capacity(kinds.len());
        for (at, kind) in kinds.iter().enumerate() {
            kind_by_name.insert(blob.string(kind.kind).to_string(), at);
        }
        let mut furn_by_name = HashMap::with_capacity(furniture.len());
        for (at, piece) in furniture.iter().enumerate() {
            furn_by_name.insert(blob.string(piece.name).to_string(), at);
        }

        // Which base row each row repeats. Base rows map to themselves, which
        // is how `_add` resolves to the first copy of a fingerprint.
        let mut first: HashMap<(u32, Vec<(u32, u32)>), u32> = HashMap::new();
        let mut base_dup = vec![0u32; entries.len()];
        for (at, entry) in entries.iter().enumerate() {
            let key = entry.fingerprint();
            if at < head.n_base {
                first.entry(key).or_insert(at as u32 + 1);
            } else if let Some(found) = first.get(&key) {
                base_dup[at] = *found;
            }
        }
        for at in 0..head.n_base.min(base_dup.len()) {
            base_dup[at] = first
                .get(&entries[at].fingerprint())
                .copied()
                .unwrap_or(at as u32 + 1);
        }

        let catalog = Catalog {
            entries,
            base_dup,
            n_base: head.n_base,
            furniture,
            rooms,
            kinds,
            int_walls,
            styles,
            storefronts,
            fixed: head.fixed,
            roles: head.roles,
            stores,
            names,
            by_name,
            kind_by_name,
            furn_by_name,
        };
        // Everything the writer reaches for without checking first.
        if catalog.n_base == 0
            || catalog.n_base > catalog.entries.len()
            || catalog.entry(catalog.fixed.exterior_wall).is_none()
            || catalog.entry(catalog.fixed.door).is_none()
            || catalog.entry(catalog.fixed.window).is_none()
            || catalog.entry(catalog.fixed.stairs).is_none()
            || catalog.entry(catalog.fixed.roof_cap).is_none()
            || catalog.entry(catalog.fixed.ceiling).is_none()
            || catalog.rooms.is_empty()
        {
            return Err(Error::BadCatalog);
        }
        Ok(catalog)
    }
}

const HEAD_VERSION: u32 = 1;

struct Head {
    n_entries: usize,
    entries_off: usize,
    n_base: usize,
    n_furniture: usize,
    furniture_off: usize,
    n_rooms: usize,
    rooms_off: usize,
    n_kinds: usize,
    kinds_off: usize,
    n_int_walls: usize,
    int_walls_off: usize,
    n_styles: usize,
    styles_off: usize,
    n_storefronts: usize,
    storefronts_off: usize,
    n_stores: usize,
    stores_off: usize,
    fixed: Fixed,
    roles: Roles,
}

impl Head {
    fn read(bytes: &[u8]) -> Result<Head> {
        let mut at = Cursor::at(bytes, 0)?;
        if at.u32()? != HEAD_VERSION {
            return Err(Error::BadCatalog);
        }
        let head = Head {
            n_entries: at.count()?,
            entries_off: at.count()?,
            n_base: at.count()?,
            n_furniture: at.count()?,
            furniture_off: at.count()?,
            n_rooms: at.count()?,
            rooms_off: at.count()?,
            n_kinds: at.count()?,
            kinds_off: at.count()?,
            n_int_walls: at.count()?,
            int_walls_off: at.count()?,
            n_styles: at.count()?,
            styles_off: at.count()?,
            n_storefronts: at.count()?,
            storefronts_off: at.count()?,
            n_stores: at.count()?,
            stores_off: at.count()?,
            fixed: Fixed {
                exterior_wall: at.u32()?,
                door: at.u32()?,
                door_frame: at.u32()?,
                window: at.u32()?,
                curtains: at.u32()?,
                stairs: at.u32()?,
                interior_wall: at.u32()?,
                roof_cap: at.u32()?,
                roof_slope: at.u32()?,
                roof_top: at.u32()?,
                ceiling: at.u32()?,
                interior_wall_trim: at.u32()?,
            },
            roles: Roles {
                west: at.u32()?,
                north: at.u32()?,
                cap_gap_e3: at.u32()?,
                cap_gap_s3: at.u32()?,
                floor: at.u32()?,
            },
        };
        Ok(head)
    }
}

/// A bounds-checked walk through the catalog bytes. Every read either returns a
/// value that was really there or `BadCatalog`; nothing is assumed about
/// lengths, because the blob arrives from outside this library.
struct Cursor<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Cursor<'a> {
    fn at(bytes: &'a [u8], at: usize) -> Result<Cursor<'a>> {
        if at > bytes.len() {
            return Err(Error::BadCatalog);
        }
        Ok(Cursor { bytes, at })
    }

    fn u32(&mut self) -> Result<u32> {
        if self.at + 4 > self.bytes.len() {
            return Err(Error::BadCatalog);
        }
        let raw = &self.bytes[self.at..self.at + 4];
        self.at += 4;
        Ok(u32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]))
    }

    fn i16(&mut self) -> Result<i16> {
        if self.at + 2 > self.bytes.len() {
            return Err(Error::BadCatalog);
        }
        let raw = &self.bytes[self.at..self.at + 2];
        self.at += 2;
        Ok(i16::from_le_bytes([raw[0], raw[1]]))
    }

    /// A count or an offset. Anything past the end of the section is a bad
    /// table, not an allocation of that size: a corrupt count must never
    /// become a `Vec::with_capacity` of four billion.
    fn count(&mut self) -> Result<usize> {
        let value = self.u32()? as usize;
        if value > self.bytes.len() {
            return Err(Error::BadCatalog);
        }
        Ok(value)
    }

    fn list(&mut self) -> Result<Vec<u32>> {
        let n = self.count()?;
        let mut out = Vec::with_capacity(n);
        for _ in 0..n {
            out.push(self.u32()?);
        }
        Ok(out)
    }
}
