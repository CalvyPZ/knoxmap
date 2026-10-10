//! The .tbx writer: a finished building as the file BuildingEd reads.
//!
//! A port of knoxbuild/tbx.py. The document is one `<building>` with its own
//! tile-entry table, a `<furniture>` block per piece used, a `<room>` per room
//! of every storey, then a `<floor>` per storey holding the doors, windows,
//! furniture, stairs and roofs, and the grid of room ids.
//!
//! Every .tbx carries its own tile table, so a style costs nothing globally:
//! the style's rows are appended after the shared ones and referenced by their
//! new indices. Indices are 1-based with 0 meaning "none", as BuildingEd's are;
//! furniture indices are 0-based, which is a quirk of the format, not a slip.

use std::collections::HashMap;

use crate::blob::{Blob, Kind};
use crate::catalog::{Catalog, Entry, Style};
use crate::grid::{erode, rooms_text, Grid, Side};
use crate::plan::{Orient, Plan};
use crate::rng::Rng;
use crate::storeys::Built;

pub const VERSION: u32 = 4;

/// A pitched roof needs room for two slopes; a narrower strip of a house keeps
/// a flat roof.
const PEAK_MIN_TILES: i32 = 4;
/// The 30-degree roofs the game's own houses wear, which BuildingEd sizes to an
/// odd number of tiles across.
const PEAK30_MAX_ACROSS: i32 = 11;
const HIP_MAX_ACROSS: i32 = 7;

/// Flat roofs carry plant: bare tar from edge to edge reads as unfinished.
const ROOF_MIN_TILES: usize = 60;
const ROOF_AC_EVERY: usize = 90;
const ROOF_VENT_EVERY: usize = 60;

/// How many different interior wall sets one building uses. Knox County's
/// figure, over 77 buildings in Muldraugh, is 4.48.
const INTERIOR_WALLS_PER_BUILDING: usize = 4;
/// A mall is the exception: every unit in it is let to somebody else and
/// painted to suit, so it gets one per room kind out of the whole list.
const MALL_WALLS_PER_KIND: usize = 99;
/// How many room kinds carry wall trim. With every room trimmed ours ran to ten
/// times Knox County's detailing, so about one kind in ten keeps it.
const TRIM_SHARE: f64 = 0.12;

/// One row of this building's tile table.
enum Row {
    /// A row of the shared catalog, by its catalog index.
    Cat(u32),
    /// The building's own roof cap, whose gap tiles are its exterior wall, so
    /// that a flat roof walls in the top storey with the building's own
    /// material instead of the stucco BuildingTemplates.txt defaults to.
    Cap(Vec<(u32, u32)>),
}

struct Table<'a> {
    cat: &'a Catalog,
    rows: Vec<Row>,
}

impl<'a> Table<'a> {
    fn new(cat: &'a Catalog) -> Table<'a> {
        let mut rows = Vec::with_capacity(cat.n_base + 24);
        for at in 0..cat.n_base {
            rows.push(Row::Cat(at as u32 + 1));
        }
        Table { cat, rows }
    }

    fn len(&self) -> usize {
        self.rows.len()
    }

    /// The 1-based index of a catalog row in this table, appending it if it is
    /// not there; 0 for no row.
    fn add(&mut self, which: u32) -> u32 {
        if which == 0 {
            return 0;
        }
        if let Some(base) = self.cat.base_dup.get(which as usize - 1) {
            if *base != 0 && *base as usize <= self.cat.n_base {
                return *base;
            }
        }
        for (at, row) in self.rows.iter().enumerate() {
            if let Row::Cat(held) = row {
                if *held == which {
                    return at as u32 + 1;
                }
            }
        }
        self.append(which)
    }

    /// Append even when the same tiles are in the table already. A style's
    /// walls and the per-building roof cap are always their own rows.
    fn append(&mut self, which: u32) -> u32 {
        if which == 0 {
            return 0;
        }
        self.rows.push(Row::Cat(which));
        self.rows.len() as u32
    }

    fn entry(&self, index: u32) -> Option<&Entry> {
        if index == 0 {
            return None;
        }
        match self.rows.get(index as usize - 1)? {
            Row::Cat(which) => self.cat.entry(*which),
            Row::Cap(_) => None,
        }
    }
}

/// Write the whole document. `name` is the building's own name, used to seed the
/// choices that should be the building's own: its floors, its wall colours.
pub fn render(
    out: &mut String,
    blob: &Blob,
    cat: &Catalog,
    built: &Built,
    name: &str,
    style: Option<&Style>,
    kind: Kind,
    seed: u64,
) {
    let storeys = &built.storeys;
    if storeys.is_empty() {
        return;
    }
    let width = storeys[0].width;
    let height = storeys[0].height;
    let fixed = cat.fixed;
    let mut table = Table::new(cat);

    let mut exterior = fixed.exterior_wall;
    let mut interior = fixed.interior_wall;
    let mut floor_override = 0;
    let mut window = fixed.window;
    let mut curtains = fixed.curtains;
    let mut roof_cap = fixed.roof_cap;
    let mut slope = fixed.roof_slope;
    let mut top = fixed.roof_top;
    let mut trim = 0;
    let mut shutters = 0;
    let mut grime = 0;
    let mut peaked = false;
    let mut roof30 = false;
    let mut front = 0;
    let mut front_curtains = 0;
    let mut cap_base = fixed.roof_cap;

    if let Some(style) = style {
        exterior = table.append(style.exterior);
        interior = table.append(style.interior);
        if style.floor != 0 {
            floor_override = table.append(style.floor);
        }
        if style.window != 0 {
            window = table.append(style.window);
        }
        if style.has_curtains() {
            curtains = table.add(style.curtains);
        }
        // Taller buildings of a kind take bigger windows: the last row whose
        // storey count this building reaches.
        for (levels, win, curt) in &style.by_levels {
            if storeys.len() as u32 >= *levels && *levels > 1 {
                window = table.add(*win);
                curtains = table.add(*curt);
            }
        }
        trim = table.add(style.trim);
        shutters = table.add(style.shutters);
        grime = table.add(style.grime);
        if style.slopes != 0 {
            slope = table.add(style.slopes);
        }
        if style.tops != 0 {
            top = table.add(style.tops);
        }
        if style.caps != 0 {
            cap_base = style.caps;
        }
        peaked = style.peaked();
        roof30 = style.roof30();
        if style.shop_front != 0 {
            front = table.add(style.shop_front);
            front_curtains = table.add(style.shop_curtains);
        }
    }

    let mut rng = Rng::seeded(seed ^ 0x7462_7800);

    // A glass shop front, where the style has one and the mod tiles are there.
    let mut runs: Vec<(Side, i32, i32, i32)> = Vec::new();
    let mut glazed: Vec<(i32, i32, Side)> = Vec::new();
    let mut store_ext = 0;
    let mut store_int = 0;
    let mut store_door = 0;
    if front != 0 && !cat.storefronts.is_empty() && !storeys[0].shop_front.is_empty() {
        let (ext, inside, door) = cat.storefronts[rng.below_usize(cat.storefronts.len())];
        store_ext = table.add(ext);
        store_int = table.add(inside);
        store_door = table.add(door);
        // A one-tile run is a step in a slanted wall, not a shop window.
        runs = storefront_runs(&storeys[0]);
        runs.retain(|(_side, _fixed, _start, length)| *length >= 2);
        for (side, line, start, length) in &runs {
            for step in 0..*length {
                glazed.push(match side {
                    Side::West => (*line, start + step, Side::West),
                    Side::North => (start + step, *line, Side::North),
                });
            }
        }
    }

    // The roof cap whose gaps are this building's own wall.
    if let (Some(ext), Some(base)) = (table.entry(exterior), cat.entry(cap_base)) {
        let west = ext.tile(cat.roles.west);
        let north = ext.tile(cat.roles.north);
        if let (Some(west), Some(north)) = (west, north) {
            let mut pairs: Vec<(u32, u32)> = base
                .pairs
                .iter()
                .copied()
                .filter(|(role, _tile)| {
                    *role != cat.roles.cap_gap_e3 && *role != cat.roles.cap_gap_s3
                })
                .collect();
            pairs.push((cat.roles.cap_gap_e3, west));
            pairs.push((cat.roles.cap_gap_s3, north));
            table.rows.push(Row::Cap(pairs));
            roof_cap = table.len() as u32;
        }
    }

    // Plant on a flat roof, and the furniture index of every piece used.
    let rooftop = match storeys.last() {
        Some(top) if !peaked => rooftop(cat, top, width, height),
        _ => Vec::new(),
    };
    let mut role_at: Vec<u16> = Vec::new();
    for storey in storeys {
        for piece in &storey.furniture {
            if !role_at.contains(&piece.role) {
                role_at.push(piece.role);
            }
        }
    }
    for (role, _x, _y, _o) in &rooftop {
        if !role_at.contains(role) {
            role_at.push(*role);
        }
    }

    // Floors and wall colours, per room kind: one house keeps one floor per
    // kind so it reads as one house, and the next house draws again.
    // Keyed by what each room is in the catalog rather than by the layout's own
    // kind, so the pizza place's dining room is painted and floored as one and
    // not as whatever sort of room the layout happened to cut it as.
    let mut keys: Vec<&str> = Vec::new();
    for storey in storeys {
        for room in &storey.rooms {
            let key = room_key(cat, room);
            if !keys.contains(&key) {
                keys.push(key);
            }
        }
    }
    keys.sort_unstable();
    let building_kind = kind_name(kind);
    let mut pick = Rng::seeded(seed ^ 0x666C_6F6F_7273);
    let mut trim_rng = Rng::seeded(seed ^ 0x7472_696D);
    let mut trimmed: Vec<&str> = Vec::new();
    for key in &keys {
        if trim_rng.chance(TRIM_SHARE) {
            trimmed.push(key);
        }
    }
    let mut room_floor: HashMap<&str, u32> = HashMap::new();
    for key in &keys {
        let choices = cat.floors_for(building_kind, key).unwrap_or(&[]);
        if choices.is_empty() {
            continue;
        }
        let which = choices[pick.below_usize(choices.len())];
        room_floor.insert(key, table.append(which));
    }
    // Interior walls, room kind by room kind. Knox County paints 4.48 different
    // sets into one building: a kitchen is not the colour of the bedroom.
    let mut palette = vec![interior];
    let mut spare: Vec<u32> = cat
        .int_walls
        .iter()
        .copied()
        .filter(|which| style.map(|held| held.interior != *which).unwrap_or(true))
        .collect();
    pick.shuffle(&mut spare);
    let want = if kind == Kind::Mall {
        MALL_WALLS_PER_KIND
    } else {
        INTERIOR_WALLS_PER_BUILDING
    }
    .min(keys.len().max(1));
    for which in spare.iter().take(want.saturating_sub(1)) {
        palette.push(table.append(*which));
    }
    let mut room_wall: HashMap<&str, u32> = HashMap::new();
    for (at, key) in keys.iter().enumerate() {
        let which = if want >= keys.len() {
            palette[at % palette.len()]
        } else {
            palette[pick.below_usize(palette.len())]
        };
        room_wall.insert(key, which);
    }

    // ---- the document ----
    out.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    out.push_str("<building");
    attr_int(out, "version", VERSION as i64);
    attr_int(out, "width", width as i64);
    attr_int(out, "height", height as i64);
    attr_int(out, "ExteriorWall", exterior as i64);
    attr_int(out, "ExteriorWallTrim", trim as i64);
    attr_int(out, "Door", fixed.door as i64);
    attr_int(out, "DoorFrame", fixed.door_frame as i64);
    attr_int(out, "Window", window as i64);
    attr_int(out, "Curtains", curtains as i64);
    attr_int(out, "Shutters", shutters as i64);
    attr_int(out, "Stairs", fixed.stairs as i64);
    attr_int(out, "RoofCap", roof_cap as i64);
    attr_int(out, "RoofSlope", slope as i64);
    attr_int(out, "RoofTop", top as i64);
    attr_int(out, "GrimeWall", grime as i64);
    out.push_str(">\n");

    for row in &table.rows {
        match row {
            Row::Cat(which) => {
                if let Some(entry) = cat.entry(*which) {
                    entry_xml(out, blob, entry.category, &entry.pairs);
                }
            }
            Row::Cap(pairs) => {
                let category = cat
                    .entry(cap_base)
                    .map(|entry| entry.category)
                    .unwrap_or(0);
                entry_xml(out, blob, category, pairs);
            }
        }
    }

    for role in &role_at {
        furniture_xml(out, blob, cat, *role);
    }

    out.push_str(" <used_tiles>");
    for at in 1..=table.len() {
        if at > 1 {
            out.push(' ');
        }
        push_int(out, at as i64);
    }
    out.push_str("</used_tiles>\n");
    out.push_str(" <used_furniture>");
    for at in 0..role_at.len() {
        if at > 0 {
            out.push(' ');
        }
        push_int(out, at as i64);
    }
    out.push_str("</used_furniture>\n");

    for storey in storeys {
        for room in &storey.rooms {
            let key = room_key(cat, room);
            let def = cat.room(key);
            let game_name = def.map(|def| blob.string(def.game_name)).unwrap_or(key);
            let color = def.map(|def| blob.string(def.color)).unwrap_or("200 200 200");
            let fallback = def.map(|def| def.floor).unwrap_or(0);
            let own = room_floor.get(key).copied().unwrap_or(0);
            out.push_str(" <room");
            attr_str(out, "Name", game_name);
            attr_str(out, "InternalName", game_name);
            attr_str(out, "Color", color);
            attr_int(
                out,
                "InteriorWall",
                room_wall.get(key).copied().unwrap_or(interior) as i64,
            );
            attr_int(
                out,
                "InteriorWallTrim",
                if trimmed.contains(&key) {
                    fixed.interior_wall_trim as i64
                } else {
                    0
                },
            );
            // A room with a floor of its own first, then the style's, then the
            // kind's own default.
            let floor = if own != 0 {
                own
            } else if floor_override != 0 {
                floor_override
            } else {
                fallback
            };
            attr_int(out, "Floor", floor as i64);
            attr_int(out, "GrimeFloor", 0);
            attr_int(out, "GrimeWall", 0);
            // A concourse has no ceiling: it is open to the storey above, and
            // BuildingEd draws a room's ceiling on the floor above it, which
            // would lay a lid across the hole the concourse exists to show.
            let ceiling = def.map(|def| def.ceiling()).unwrap_or(true);
            attr_int(out, "Ceiling", if ceiling { fixed.ceiling as i64 } else { 0 });
            out.push_str("/>\n");
        }
    }

    // Each storey's rooms are numbered on from the last, which is what
    // `grid_for` does on the Python side.
    let mut offset = 0i32;
    let mut attic: Vec<String> = Vec::new();
    for (level, storey) in storeys.iter().enumerate() {
        out.push_str(" <floor>\n");
        for door in &storey.doors {
            let shop = level == 0 && glazed.contains(&(door.x, door.y, door.side));
            out.push_str("  <object");
            attr_str(out, "type", "door");
            attr_int(
                out,
                "FrameTile",
                if shop { 0 } else { fixed.door_frame as i64 },
            );
            attr_int(out, "x", door.x as i64);
            attr_int(out, "y", door.y as i64);
            attr_str(out, "dir", side_name(door.side));
            attr_int(
                out,
                "Tile",
                if shop { store_door as i64 } else { fixed.door as i64 },
            );
            out.push_str("/>\n");
        }
        if level == 0 {
            for (side, line, start, length) in &runs {
                let (x, y) = match side {
                    Side::West => (*line, *start),
                    Side::North => (*start, *line),
                };
                out.push_str("  <object");
                attr_str(out, "type", "wall");
                attr_int(out, "length", *length as i64);
                attr_int(out, "InteriorTile", store_int as i64);
                attr_int(out, "ExteriorTrim", 0);
                attr_int(out, "InteriorTrim", 0);
                attr_int(out, "x", x as i64);
                attr_int(out, "y", y as i64);
                // A wall object runs along y when its dir is N, placing west
                // walls, and along x when it is W.
                attr_str(
                    out,
                    "dir",
                    match side {
                        Side::West => "N",
                        Side::North => "W",
                    },
                );
                attr_int(out, "Tile", store_ext as i64);
                out.push_str("/>\n");
            }
        }
        for hole in &storey.windows {
            if level == 0 && glazed.contains(&(hole.x, hole.y, hole.side)) {
                continue;       // the glass wall is the window
            }
            let shop = front != 0 && storey.shop_front.contains(hole);
            let tile = if shop { front } else { window };
            out.push_str("  <object");
            attr_str(out, "type", "window");
            attr_int(
                out,
                "CurtainsTile",
                if shop { front_curtains as i64 } else { curtains as i64 },
            );
            attr_int(
                out,
                "ShuttersTile",
                if shop { 0 } else { shutters as i64 },
            );
            attr_int(out, "x", hole.x as i64);
            attr_int(out, "y", hole.y as i64);
            attr_str(out, "dir", side_name(hole.side));
            attr_int(out, "Tile", tile as i64);
            out.push_str("/>\n");
        }
        for piece in &storey.furniture {
            let at = role_at.iter().position(|held| *held == piece.role).unwrap_or(0);
            out.push_str("  <object");
            attr_str(out, "type", "furniture");
            attr_int(out, "FurnitureTiles", at as i64);
            attr_str(out, "orient", piece.orient.letter());
            attr_int(out, "x", piece.x as i64);
            attr_int(out, "y", piece.y as i64);
            out.push_str("/>\n");
        }
        // A staircase belongs to the storey it rises from; the reader puts the
        // opening in the floor above by itself. Only N and W are valid
        // directions: BuildingEd treats anything else as an invalid object.
        if let Some((sx, sy, side)) = built.stairs.get(level) {
            out.push_str("  <object");
            attr_str(out, "type", "stairs");
            attr_int(out, "x", *sx as i64);
            attr_int(out, "y", *sy as i64);
            attr_str(out, "dir", side_name(*side));
            attr_int(out, "Tile", fixed.stairs as i64);
            out.push_str("/>\n");
        }

        // A roof over whatever of this storey has no storey above it: the whole
        // top floor, and the ledge where a tower steps back.
        let mut exposed: Vec<i32> = storey.grid.clone();
        let last = level + 1 == storeys.len();
        if !last {
            let above = &storeys[level + 1];
            for (at, cell) in exposed.iter_mut().enumerate() {
                if above.grid[at] != 0 {
                    *cell = 0;
                }
            }
            // A square left open in the storey above - the hole down the middle
            // of a mall - is still built over: roofing it would put a ceiling
            // across the concourse and hide the floor the hole exists to show.
            for (x, y) in &above.void {
                if *x >= 0 && *y >= 0 && *x < width && *y < height {
                    exposed[(y * width + x) as usize] = 0;
                }
            }
        }
        if exposed.iter().any(|cell| *cell != 0) {
            let rects = roof_rects(&exposed, width, height);
            for (kind, depth, caps, (rx, ry, rw, rh)) in
                roof_pieces(&rects, peaked && last, roof30)
            {
                let mut line = String::with_capacity(220);
                line.push_str("  <object");
                attr_str(&mut line, "type", "roof");
                attr_int(&mut line, "width", rw as i64);
                attr_int(&mut line, "height", rh as i64);
                attr_str(&mut line, "RoofType", kind);
                attr_str(&mut line, "Depth", depth);
                attr_str(&mut line, "cappedW", bool_name(caps[0]));
                attr_str(&mut line, "cappedN", bool_name(caps[1]));
                attr_str(&mut line, "cappedE", bool_name(caps[2]));
                attr_str(&mut line, "cappedS", bool_name(caps[3]));
                attr_int(&mut line, "CapTiles", roof_cap as i64);
                attr_int(&mut line, "SlopeTiles", slope as i64);
                attr_int(&mut line, "TopTiles", top as i64);
                attr_int(&mut line, "x", rx as i64);
                attr_int(&mut line, "y", ry as i64);
                line.push_str("/>\n");
                // BuildingEd measures a roof's height up from the floor it is
                // on. A flat roof at depth three ends level with the ceiling,
                // which is right; a pitched roof there rose through the top
                // storey, its gable ends covering the upstairs walls. Pitched
                // roofs go on the roof floor above instead.
                if kind == "FlatTop" {
                    out.push_str(&line);
                } else {
                    attic.push(line);
                }
            }
        }

        out.push_str("  <rooms>");
        grid_xml(out, &storey.grid, width, height, offset);
        out.push_str("</rooms>\n");
        out.push_str(" </floor>\n");
        offset += storey.rooms.len() as i32;
    }

    // The roof floor: no rooms. It holds the pitched roofs, and the flat tops
    // BuildingEd places here from the depth-three roofs below.
    let empty = vec![0i32; (width.max(0) * height.max(0)) as usize];
    out.push_str(" <floor>\n");
    for line in &attic {
        out.push_str(line);
    }
    for (role, x, y, orient) in &rooftop {
        let at = role_at.iter().position(|held| held == role).unwrap_or(0);
        out.push_str("  <object");
        attr_str(out, "type", "furniture");
        attr_int(out, "FurnitureTiles", at as i64);
        attr_str(out, "orient", orient.letter());
        attr_int(out, "x", *x as i64);
        attr_int(out, "y", *y as i64);
        out.push_str("/>\n");
    }
    out.push_str("  <rooms>");
    grid_xml(out, &empty, width, height, 0);
    out.push_str("</rooms>\n");
    out.push_str(" </floor>\n");
    if !attic.is_empty() {
        // A pitched roof's top rises a full storey above the roof floor, and
        // BuildingEd lays what is up there on the floor above that.
        out.push_str(" <floor>\n  <rooms>");
        grid_xml(out, &empty, width, height, 0);
        out.push_str("</rooms>\n </floor>\n");
    }
    out.push_str("</building>\n");
    let _ = name;
}

/// What a room is, in the catalog's own terms: what the map said is there if it
/// said anything, else what the layout made it.
fn room_key<'a>(cat: &'a Catalog, room: &crate::plan::Room) -> &'a str {
    cat.role_name(room.role).unwrap_or(room.kind.name())
}

fn kind_name(kind: Kind) -> &'static str {
    match kind {
        Kind::School => "school",
        Kind::Police => "police",
        Kind::Fire => "fire",
        Kind::Clinic => "clinic",
        Kind::Mall => "mall",
        Kind::Warehouse => "warehouse",
        Kind::Factory => "factory",
        Kind::Church => "church",
        Kind::Hotel => "hotel",
        Kind::Bar => "bar",
        Kind::Restaurant => "restaurant",
        Kind::Office => "office",
        Kind::Retail | Kind::Shop => "shop",
        Kind::Apartment => "apartment",
        Kind::Civic => "civic",
        Kind::Garage => "garage",
        _ => "house",
    }
}

fn side_name(side: Side) -> &'static str {
    match side {
        Side::West => "W",
        Side::North => "N",
    }
}


fn bool_name(on: bool) -> &'static str {
    if on {
        "true"
    } else {
        "false"
    }
}

fn entry_xml(out: &mut String, blob: &Blob, category: u32, pairs: &[(u32, u32)]) {
    out.push_str(" <tile_entry category=");
    quote(out, blob.string(category));
    out.push_str(">\n");
    for (role, tile) in pairs {
        out.push_str("  <tile enum=");
        quote(out, blob.string(*role));
        out.push_str(" tile=");
        quote(out, blob.string(*tile));
        out.push_str("/>\n");
    }
    out.push_str(" </tile_entry>\n");
}

/// One `<furniture>` block, all four facings in the order BuildingWriter emits
/// them. Writing only W and N leaves the E and S slots empty, so any object
/// facing that way renders as nothing at all.
fn furniture_xml(out: &mut String, blob: &Blob, cat: &Catalog, role: u16) {
    let Some(piece) = cat.furniture.get(role as usize) else {
        return;
    };
    let layer = blob.string(piece.layer);
    if layer.is_empty() || layer == "Furniture" {
        out.push_str(" <furniture>\n");
    } else {
        out.push_str(" <furniture layer=");
        quote(out, layer);
        out.push_str(">\n");
    }
    for (at, name) in ["W", "N", "E", "S"].iter().enumerate() {
        let tiles = &piece.orients[at];
        if tiles.is_empty() {
            continue;
        }
        out.push_str("  <entry orient=\"");
        out.push_str(name);
        out.push_str("\">\n");
        for (dx, dy, tile) in tiles {
            out.push_str("   <tile x=\"");
            push_int(out, *dx as i64);
            out.push_str("\" y=\"");
            push_int(out, *dy as i64);
            out.push_str("\" name=");
            quote(out, blob.string(*tile));
            out.push_str("/>\n");
        }
        out.push_str("  </entry>\n");
    }
    out.push_str(" </furniture>\n");
}

/// The BuildingWriter grid: a comma after every cell but the last, a newline
/// after every row, escaped as the attribute text it sits in.
fn grid_xml(out: &mut String, grid: &[i32], width: i32, height: i32, offset: i32) {
    let mut text = String::new();
    let g = Grid::new(grid, width.max(0) as usize, height.max(0) as usize);
    rooms_text(&g, offset, &mut text);
    escape_into(out, &text);
}

/// Plant on a flat roof: a hatch, an air-conditioning unit per 90 tiles and a
/// vent per 60, kept off the edges and off each other.
fn rooftop(cat: &Catalog, storey: &Plan, width: i32, height: i32) -> Vec<(u16, i32, i32, Orient)> {
    let cells: Vec<bool> = storey.grid.iter().map(|cell| *cell != 0).collect();
    let area = cells.iter().filter(|held| **held).count();
    if area < ROOF_MIN_TILES {
        return Vec::new();
    }
    // A tile is interior when its whole 3x3 neighbourhood is roof.
    let mut inside = Vec::new();
    erode(&cells, width.max(0) as usize, height.max(0) as usize, 1, 1, &mut inside);
    let mut tiles: Vec<(i32, i32)> = Vec::new();
    for y in 0..height {
        for x in 0..width {
            if inside
                .get((y * width + x) as usize)
                .copied()
                .unwrap_or(false)
            {
                tiles.push((x, y));
            }
        }
    }
    if tiles.is_empty() {
        return Vec::new();
    }
    let mut rng = Rng::seeded(
        (width as u64)
            .wrapping_mul(7919)
            .wrapping_add((height as u64).wrapping_mul(104729))
            .wrapping_add(area as u64),
    );
    let mut wanted: Vec<&str> = vec!["roof_hatch"];
    for _ in 0..(area / ROOF_AC_EVERY).max(1) {
        wanted.push("roof_ac");
    }
    for _ in 0..area / ROOF_VENT_EVERY {
        wanted.push("roof_vent");
    }
    rng.shuffle(&mut tiles);
    let mut taken: Vec<(i32, i32)> = Vec::new();
    let mut out = Vec::new();
    for role in wanted {
        let Some(furn) = cat.furn(role) else {
            continue;
        };
        for (x, y) in &tiles {
            let near = taken
                .iter()
                .any(|(tx, ty)| (tx - x).abs() <= 1 && (ty - y).abs() <= 1);
            if near {
                continue;
            }
            taken.push((*x, *y));
            let orient = if rng.chance(0.5) {
                Orient::West
            } else {
                Orient::North
            };
            out.push((furn as u16, *x, *y, orient));
            break;
        }
    }
    out
}

/// The ground floor's shop glass, as straight runs of wall: (side, the fixed
/// coordinate, the first tile along, the length). A run spans its first glazed
/// tile to its last, taking in the doors and the wall beside them, and stops
/// wherever the line is no longer an outside wall.
fn storefront_runs(storey: &Plan) -> Vec<(Side, i32, i32, i32)> {
    let outside = |side: Side, line: i32, along: i32| -> bool {
        let (x, y) = match side {
            Side::West => (line, along),
            Side::North => (along, line),
        };
        let (a, b) = match side {
            Side::West => (storey.at(x - 1, y), storey.at(x, y)),
            Side::North => (storey.at(x, y - 1), storey.at(x, y)),
        };
        (a != 0) != (b != 0)
    };
    // Group the glazed edges into lines.
    let mut lines: Vec<(Side, i32, Vec<i32>)> = Vec::new();
    for edge in &storey.shop_front {
        let (line, along) = match edge.side {
            Side::West => (edge.x, edge.y),
            Side::North => (edge.y, edge.x),
        };
        match lines
            .iter_mut()
            .find(|(side, held, _)| *side == edge.side && *held == line)
        {
            Some((_side, _line, along_list)) => along_list.push(along),
            None => lines.push((edge.side, line, vec![along])),
        }
    }
    lines.sort_by_key(|(side, line, _)| (side_name(*side), *line));
    let mut out = Vec::new();
    for (side, line, along) in &lines {
        let mut lo = *along.iter().min().unwrap();
        let mut hi = *along.iter().max().unwrap();
        // A shop door just past the last pane belongs to the shop front too.
        for step in [-1i32, 1] {
            let end = if step < 0 { lo } else { hi };
            for k in 1..=2 {
                let at = end + k * step;
                let has_door = storey.doors.iter().any(|door| {
                    door.side == *side
                        && match side {
                            Side::West => door.x == *line && door.y == at,
                            Side::North => door.y == *line && door.x == at,
                        }
                });
                if has_door && (1..=k).all(|i| outside(*side, *line, end + i * step)) {
                    if step < 0 {
                        lo = at;
                    } else {
                        hi = at;
                    }
                    break;
                }
            }
        }
        let mut start: Option<i32> = None;
        for at in lo..=hi + 1 {
            if at <= hi && outside(*side, *line, at) {
                start = start.or(Some(at));
            } else if let Some(from) = start.take() {
                out.push((*side, *line, from, at - from));
            }
        }
    }
    out
}

/// Flat roofs covering exactly a storey's footprint, biggest first.
///
/// A BuildingEd roof is a rectangle, and a roof the size of the bounding box
/// reaches out over the yard of an L-shaped building. The footprint is covered
/// by the largest rectangles that fit instead: an L takes two, a notched block
/// three. Each side is capped only where it is the edge of the building; where
/// one roof meets another the rim would draw a ridge across a flat roof.
type Rects = Vec<(i32, i32, i32, i32, [bool; 4])>;

fn roof_rects(grid: &[i32], width: i32, height: i32) -> Rects {
    let w = width.max(0) as usize;
    let h = height.max(0) as usize;
    if w == 0 || h == 0 {
        return Vec::new();
    }
    let inside: Vec<bool> = grid.iter().map(|cell| *cell != 0).collect();
    let mut todo = inside.clone();
    let mut out: Rects = Vec::new();
    loop {
        let Some((x0, y0, rw, rh)) = largest_rect(&todo, w, h) else {
            break;
        };
        if rw <= 0 || rh <= 0 {
            break;
        }
        for y in y0..y0 + rh {
            for x in x0..x0 + rw {
                todo[(y * width + x) as usize] = false;
            }
        }
        let solid = |xs: (i32, i32), ys: (i32, i32)| -> bool {
            for y in ys.0..=ys.1 {
                for x in xs.0..=xs.1 {
                    if x < 0 || y < 0 || x >= width || y >= height {
                        return false;
                    }
                    if !inside[(y * width + x) as usize] {
                        return false;
                    }
                }
            }
            true
        };
        let caps = [
            !solid((x0 - 1, x0 - 1), (y0, y0 + rh - 1)),
            !solid((x0, x0 + rw - 1), (y0 - 1, y0 - 1)),
            !solid((x0 + rw, x0 + rw), (y0, y0 + rh - 1)),
            !solid((x0, x0 + rw - 1), (y0 + rh, y0 + rh)),
        ];
        out.push((x0, y0, rw, rh, caps));
    }
    out
}

/// Biggest all-true rectangle, by the histogram scan.
fn largest_rect(todo: &[bool], w: usize, h: usize) -> Option<(i32, i32, i32, i32)> {
    let mut heights = vec![0i32; w + 1];
    let mut best: Option<(i32, i32, i32, i32)> = None;
    let mut best_area = 0i32;
    let mut stack: Vec<usize> = Vec::with_capacity(w + 1);
    for y in 0..h {
        for x in 0..w {
            heights[x] = if todo[y * w + x] { heights[x] + 1 } else { 0 };
        }
        stack.clear();
        for x in 0..=w {
            let cur = heights[x];
            while let Some(peak) = stack.last().copied() {
                if heights[peak] < cur {
                    break;
                }
                stack.pop();
                let left = stack.last().map(|at| at + 1).unwrap_or(0);
                let hh = heights[peak];
                let area = hh * (x - left) as i32;
                if area > best_area {
                    best_area = area;
                    best = Some((left as i32, y as i32 - hh + 1, (x - left) as i32, hh));
                }
            }
            stack.push(x);
        }
    }
    best
}

/// BuildingEd's depth for a 45-degree peaked roof this many tiles across.
fn peak_depth(across: i32) -> &'static str {
    match across {
        1 => "Point5",
        2 => "One",
        3 => "OnePoint5",
        4 => "Two",
        5 => "TwoPoint5",
        _ => "Three",
    }
}

/// (RoofType, Depth, caps, box) for each roof over a footprint.
fn roof_pieces(
    rects: &Rects,
    peaked: bool,
    roof30: bool,
) -> Vec<(&'static str, &'static str, [bool; 4], (i32, i32, i32, i32))> {
    let mut out = Vec::new();
    let single = rects.len() == 1;
    for (rx, ry, rw, rh, caps) in rects {
        let across = (*rw).min(*rh);
        if !peaked || across < PEAK_MIN_TILES {
            // Flat roofs are never capped: at depth three a cap is a
            // storey-high wall of roof tiles over the top storey's own walls.
            out.push(("FlatTop", "Three", [false; 4], (*rx, *ry, *rw, *rh)));
            continue;
        }
        let along_x = rw >= rh;
        if !roof30 || across > PEAK30_MAX_ACROSS + 2 {
            let mut cap = [false; 4];
            if along_x {
                cap[0] = caps[0];
                cap[2] = caps[2];
                out.push(("PeakWE", peak_depth(*rh), cap, (*rx, *ry, *rw, *rh)));
            } else {
                cap[1] = caps[1];
                cap[3] = caps[3];
                out.push(("PeakNS", peak_depth(*rw), cap, (*rx, *ry, *rw, *rh)));
            }
            continue;
        }
        // A 30-degree roof needs an odd width across its slopes. On an even one
        // the roof covers one tile less and a flat strip takes the last row, on
        // the side away from the camera where it shows least.
        let odd = if across % 2 == 1 { across } else { across - 1 };
        // Hips only on small houses: across a wide one the four slopes meet in
        // a tall point that looks like a hat.
        let hip = single
            && odd <= HIP_MAX_ACROSS
            && (rx * 31 + ry * 17 + rw * 7 + rh) % 2 == 0;
        let mut cap = [false; 4];
        if along_x {
            let box_at = (*rx, ry + (rh - odd), *rw, odd);
            if *rh != odd {
                out.push(("FlatTop", "Three", [false; 4], (*rx, *ry, *rw, rh - odd)));
            }
            if !hip {
                cap[0] = caps[0];
                cap[2] = caps[2];
            }
            out.push((
                if hip { "Peak30Quad" } else { "Peak30WE" },
                "Zero",
                cap,
                box_at,
            ));
        } else {
            let box_at = (rx + (rw - odd), *ry, odd, *rh);
            if *rw != odd {
                out.push(("FlatTop", "Three", [false; 4], (*rx, *ry, rw - odd, *rh)));
            }
            if !hip {
                cap[1] = caps[1];
                cap[3] = caps[3];
            }
            out.push((
                if hip { "Peak30Quad" } else { "Peak30NS" },
                "Zero",
                cap,
                box_at,
            ));
        }
    }
    out
}

// ---- the XML itself ----

fn attr_int(out: &mut String, key: &str, value: i64) {
    out.push(' ');
    out.push_str(key);
    out.push_str("=\"");
    push_int(out, value);
    out.push('"');
}

fn attr_str(out: &mut String, key: &str, value: &str) {
    out.push(' ');
    out.push_str(key);
    out.push('=');
    quote(out, value);
}

/// An attribute value in double quotes. Nearly every name here is a tile or an
/// enum with nothing to escape, so the scan is for the rare one that has.
fn quote(out: &mut String, value: &str) {
    out.push('"');
    escape_into(out, value);
    out.push('"');
}

fn escape_into(out: &mut String, value: &str) {
    if !value
        .bytes()
        .any(|byte| matches!(byte, b'&' | b'<' | b'>' | b'"'))
    {
        out.push_str(value);
        return;
    }
    for ch in value.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            _ => out.push(ch),
        }
    }
}

fn push_int(out: &mut String, value: i64) {
    let mut buf = [0u8; 20];
    let mut at = buf.len();
    let negative = value < 0;
    let mut left = value.unsigned_abs();
    loop {
        at -= 1;
        buf[at] = b'0' + (left % 10) as u8;
        left /= 10;
        if left == 0 {
            break;
        }
    }
    if negative {
        out.push('-');
    }
    out.push_str(std::str::from_utf8(&buf[at..]).unwrap_or("0"));
}
