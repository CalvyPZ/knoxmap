// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// BuildingReader plus BuildingFloor::LayoutToSquares and BuildingMap::mergedMap.
// Roof slope, cap, and flat-top lists follow RoofObject in
// buildingobjects.cpp (slopeTiles, west/east/north/southCapTiles, flatTop)
// and LayoutToSquares in buildingfloor.cpp. Peak30 gables and hips are the
// roofs KnoxMap writes. Corner and dormer lists stay out of this path.

//! Load a .tbx building and bake it into the same layers a merged map has.

use std::collections::HashMap;
use std::path::Path;

use crate::model::{GameMap, Layer, RoomDef};
use crate::xmltree::{self, Elem};

#[derive(Clone, Debug, Default)]
struct Entry {
    tiles: HashMap<String, String>,
}

#[derive(Clone, Debug)]
struct Room {
    name: String,
    internal: String,
    floor: Option<usize>,
    ceiling: Option<usize>,
    interior_wall: Option<usize>,
    interior_trim: Option<usize>,
    grime_floor: Option<usize>,
    grime_wall: Option<usize>,
}

#[derive(Clone, Debug)]
struct Obj {
    kind: String,
    x: i32,
    y: i32,
    dir: String,
    length: i32,
    width: i32,
    height: i32,
    tile: Option<usize>,
    interior: Option<usize>,
    exterior_trim: Option<usize>,
    interior_trim: Option<usize>,
    frame: Option<usize>,
    curtains: Option<usize>,
    shutters: Option<usize>,
    roof_type: String,
    depth: String,
    capped_w: bool,
    capped_n: bool,
    capped_e: bool,
    capped_s: bool,
    cap_tiles: Option<usize>,
    slope_tiles: Option<usize>,
    top_tiles: Option<usize>,
    furniture: Option<usize>,
    orient: String,
}

#[derive(Clone, Debug)]
struct Floor {
    rooms: Vec<Vec<i32>>,
    objects: Vec<Obj>,
    user: HashMap<String, Vec<Option<String>>>,
    user_w: i32,
    user_h: i32,
}

struct Building {
    width: i32,
    height: i32,
    entries: Vec<Entry>,
    rooms: Vec<Room>,
    floors: Vec<Floor>,
    furniture: Vec<Furniture>,
}

struct Furniture {
    layer: String,
    orients: HashMap<String, Vec<(i32, i32, String)>>,
}

pub fn read_tbx(path: &Path) -> Result<GameMap, String> {
    let text = std::fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let root = xmltree::parse(&text)?;
    if root.name != "building" {
        return Err(format!("{} is not a building", path.display()));
    }
    let width = root.attr("width").parse::<i32>().unwrap_or(1);
    let height = root.attr("height").parse::<i32>().unwrap_or(1);
    let mut entries = vec![Entry::default()];
    for node in root.children_named("tile_entry") {
        let mut entry = Entry::default();
        for tile in node.children_named("tile") {
            let name = tile.attr("tile");
            if !name.is_empty() && name != "none" {
                entry.tiles.insert(tile.attr("enum").to_string(), name.to_string());
            }
        }
        entries.push(entry);
    }
    let mut furniture = Vec::new();
    for node in root.children_named("furniture") {
        let mut piece = Furniture {
            layer: node.attr("layer").to_string(),
            orients: HashMap::new(),
        };
        for entry in node.children_named("entry") {
            let orient = entry.attr("orient").to_string();
            let mut tiles = Vec::new();
            for tile in entry.children_named("tile") {
                tiles.push((
                    tile.attr("x").parse().unwrap_or(0),
                    tile.attr("y").parse().unwrap_or(0),
                    tile.attr("name").to_string(),
                ));
            }
            piece.orients.insert(orient, tiles);
        }
        furniture.push(piece);
    }
    let mut user_names = Vec::new();
    if let Some(user) = root.child("user_tiles") {
        for tile in user.children_named("tile") {
            user_names.push(tile.attr("tile").to_string());
        }
    }
    let mut rooms = Vec::new();
    for node in root.children_named("room") {
        rooms.push(Room {
            name: node.attr("Name").to_string(),
            internal: {
                let internal = node.attr("InternalName");
                if internal.is_empty() { node.attr("Name").to_string() } else { internal.to_string() }
            },
            floor: idx(node.attr("Floor")),
            ceiling: idx(node.attr("Ceiling")),
            interior_wall: idx(node.attr("InteriorWall")),
            interior_trim: idx(node.attr("InteriorWallTrim")),
            grime_floor: idx(node.attr("GrimeFloor")),
            grime_wall: idx(node.attr("GrimeWall")),
        });
    }
    let mut floors = Vec::new();
    for node in root.children_named("floor") {
        floors.push(read_floor(node, width, height, &user_names)?);
    }
    let building = Building {
        width,
        height,
        entries,
        rooms,
        floors,
        furniture,
    };
    Ok(bake(&building))
}

fn idx(text: &str) -> Option<usize> {
    let n: usize = text.parse().ok()?;
    if n == 0 { None } else { Some(n) }
}

fn read_floor(node: &Elem, width: i32, height: i32, user_names: &[String]) -> Result<Floor, String> {
    let mut rooms = vec![vec![0; width as usize]; height as usize];
    if let Some(grid) = node.child("rooms") {
        let mut x = 0i32;
        let mut y = 0i32;
        for part in grid.text.split(',') {
            let part = part.trim();
            if part.is_empty() {
                continue;
            }
            if y < height && x < width {
                rooms[y as usize][x as usize] = part.parse().unwrap_or(0);
            }
            x += 1;
            if x == width {
                x = 0;
                y += 1;
            }
        }
    }
    let mut objects = Vec::new();
    for obj in node.children_named("object") {
        objects.push(Obj {
            kind: obj.attr("type").to_string(),
            x: obj.attr("x").parse().unwrap_or(0),
            y: obj.attr("y").parse().unwrap_or(0),
            dir: obj.attr("dir").to_string(),
            length: obj.attr("length").parse().unwrap_or(1),
            width: obj.attr("width").parse().unwrap_or(0),
            height: obj.attr("height").parse().unwrap_or(0),
            tile: idx(obj.attr("Tile")),
            interior: idx(obj.attr("InteriorTile")),
            exterior_trim: idx(obj.attr("ExteriorTrim")),
            interior_trim: idx(obj.attr("InteriorTrim")),
            frame: idx(obj.attr("FrameTile")),
            curtains: idx(obj.attr("CurtainsTile")),
            shutters: idx(obj.attr("ShuttersTile")),
            roof_type: obj.attr("RoofType").to_string(),
            depth: obj.attr("Depth").to_string(),
            capped_w: obj.attr("cappedW") == "true",
            capped_n: obj.attr("cappedN") == "true",
            capped_e: obj.attr("cappedE") == "true",
            capped_s: obj.attr("cappedS") == "true",
            cap_tiles: idx(obj.attr("CapTiles")),
            slope_tiles: idx(obj.attr("SlopeTiles")),
            top_tiles: idx(obj.attr("TopTiles")),
            furniture: obj.attr("FurnitureTiles").parse().ok(),
            orient: obj.attr("orient").to_string(),
        });
    }
    let mut user = HashMap::new();
    let mut user_w = width + 1;
    let mut user_h = height + 1;
    for tiles in node.children_named("tiles") {
        let values: Vec<i32> = tiles
            .text
            .split(',')
            .filter_map(|s| {
                let s = s.trim();
                if s.is_empty() { None } else { s.parse().ok() }
            })
            .collect();
        if values.len() as i32 == (width + 1) * (height + 1) {
            user_w = width + 1;
            user_h = height + 1;
        }
        let names: Vec<Option<String>> = values
            .into_iter()
            .map(|i| {
                if i > 0 && (i as usize) <= user_names.len() {
                    let name = &user_names[(i as usize) - 1];
                    if name.is_empty() { None } else { Some(name.clone()) }
                } else {
                    None
                }
            })
            .collect();
        user.insert(tiles.attr("layer").to_string(), names);
    }
    Ok(Floor {
        rooms,
        objects,
        user,
        user_w,
        user_h,
    })
}

const LAYERS: &[&str] = &[
    "Floor", "FloorGrime", "FloorGrime2", "FloorFurniture", "Walls", "WallTrim", "Walls2",
    "WallTrim2", "RoofCap", "RoofCap2", "WallOverlay", "WallOverlay2", "WallGrime", "WallGrime2",
    "WallFurniture", "WallFurniture2", "Frames", "Doors", "Windows", "Curtains", "Furniture",
    "Furniture2", "Furniture3", "Furniture4", "Curtains2", "WallFurniture3", "WallFurniture4",
    "WallOverlay3", "WallOverlay4", "Roof", "Roof2", "RoofTop",
];

fn bake(building: &Building) -> GameMap {
    let levels = building.floors.len().max(1) as i32;
    let extra = 1;
    let width = building.width + extra;
    let height = building.height + extra;
    let mut map = GameMap {
        isometric: false,
        width,
        height,
        layers: Vec::new(),
        rooms: Vec::new(),
    };
    for level in 0..levels {
        for name in LAYERS {
            map.layers.push(Layer::new(name, level, width, height));
        }
    }
    let mut room_at: Vec<Vec<Vec<i32>>> = Vec::new();
    for floor in &building.floors {
        room_at.push(floor.rooms.clone());
    }
    for (level, floor) in building.floors.iter().enumerate() {
        paint_floor(building, floor, level as i32, &room_at, &mut map);
        paint_user(building, floor, level as i32, &mut map);
        map.rooms.extend(room_rects(building, floor, level as i32));
    }
    map
}

fn layer<'a>(map: &'a mut GameMap, level: i32, name: &str) -> Option<&'a mut Layer> {
    map.layers.iter_mut().find(|l| l.level == level && l.name == name)
}

fn put(map: &mut GameMap, level: i32, name: &str, x: i32, y: i32, tile: Option<&str>) {
    if let Some(tile) = tile {
        if !tile.is_empty() {
            if let Some(layer) = layer(map, level, name) {
                if layer.get(x, y).is_none() {
                    layer.set(x, y, Some(tile.to_string()));
                }
            }
        }
    }
}

fn put_over(map: &mut GameMap, level: i32, name: &str, x: i32, y: i32, tile: Option<&str>) {
    if let Some(tile) = tile {
        if !tile.is_empty() {
            if let Some(layer) = layer(map, level, name) {
                layer.set(x, y, Some(tile.to_string()));
            }
        }
    }
}

/// BuildingFloor::Square::ReplaceRoof / ReplaceRoofCap: the first tile lands
/// on the primary layer, the next on the secondary.
fn put_stack(map: &mut GameMap, level: i32, primary: &str, secondary: &str, x: i32, y: i32, tile: Option<&str>) {
    let Some(tile) = tile.filter(|tile| !tile.is_empty()) else { return };
    let occupied = layer(map, level, primary).and_then(|layer| layer.get(x, y)).is_some();
    let name = if occupied { secondary } else { primary };
    if let Some(layer) = layer(map, level, name) {
        layer.set(x, y, Some(tile.to_string()));
    }
}

fn entry<'a>(building: &'a Building, index: Option<usize>) -> Option<&'a Entry> {
    index.and_then(|i| building.entries.get(i))
}

fn tile_named<'a>(entry: Option<&'a Entry>, enum_name: &str) -> Option<&'a str> {
    entry.and_then(|e| e.tiles.get(enum_name).map(String::as_str))
}

fn paint_floor(building: &Building, floor: &Floor, level: i32, room_at: &[Vec<Vec<i32>>], map: &mut GameMap) {
    let w = building.width;
    let h = building.height;
    let room_index = |x: i32, y: i32| -> i32 {
        if x < 0 || y < 0 || x >= w || y >= h {
            return -1;
        }
        let id = floor.rooms[y as usize][x as usize];
        if id <= 0 { -1 } else { id }
    };
    // Walls from room edges, then wall objects, matching LayoutToSquares.
    for x in 0..=w {
        for y in 0..=h {
            if x < w {
                let here = if y < h { room_index(x, y) } else { -1 };
                let above = if y > 0 { room_index(x, y - 1) } else { -1 };
                if y == h && above >= 0 {
                    paint_wall(building, map, level, x, y, "N", None, true);
                } else if y < h && here < 0 && y > 0 && above != here {
                    paint_wall(building, map, level, x, y, "N", None, true);
                } else if y < h && here >= 0 && (y == 0 || above != here) {
                    let room = building.rooms.get((here as usize).saturating_sub(1));
                    paint_wall(building, map, level, x, y, "N", room, false);
                }
            }
            if y < h {
                let here = if x < w { room_index(x, y) } else { -1 };
                let left = if x > 0 { room_index(x - 1, y) } else { -1 };
                if x == w && left >= 0 {
                    paint_wall(building, map, level, x, y, "W", None, true);
                } else if x < w && here < 0 && x > 0 && left != here {
                    paint_wall(building, map, level, x, y, "W", None, true);
                } else if x < w && here >= 0 && (x == 0 || left != here) {
                    let room = building.rooms.get((here as usize).saturating_sub(1));
                    paint_wall(building, map, level, x, y, "W", room, false);
                }
            }
        }
    }
    for obj in &floor.objects {
        if obj.kind != "wall" {
            continue;
        }
        let exterior = obj.tile;
        let interior = obj.interior.or(obj.tile);
        if obj.dir == "N" {
            for y in obj.y..obj.y + obj.length {
                let outside = room_index(obj.x, y) < 0;
                let which = if outside { exterior } else { interior };
                let trim = if outside { obj.exterior_trim } else { obj.interior_trim };
                put(map, level, "Walls", obj.x, y, tile_named(entry(building, which), "West"));
                put(map, level, "WallTrim", obj.x, y, tile_named(entry(building, trim), "West"));
            }
        } else {
            for x in obj.x..obj.x + obj.length {
                let outside = room_index(x, obj.y) < 0;
                let which = if outside { exterior } else { interior };
                let trim = if outside { obj.exterior_trim } else { obj.interior_trim };
                put(map, level, "Walls", x, obj.y, tile_named(entry(building, which), "North"));
                put(map, level, "WallTrim", x, obj.y, tile_named(entry(building, trim), "North"));
            }
        }
    }
    if level > 0 {
        let below = &room_at[(level - 1) as usize];
        for y in 0..h {
            for x in 0..w {
                let id = below.get(y as usize).and_then(|row| row.get(x as usize)).copied().unwrap_or(0);
                if id > 0 {
                    if let Some(room) = building.rooms.get((id as usize) - 1) {
                        put(map, level, "Floor", x, y, tile_named(entry(building, room.ceiling), "Ceiling"));
                    }
                }
            }
        }
    }
    for obj in &floor.objects {
        match obj.kind.as_str() {
            "door" => {
                let enum_name = if obj.dir == "N" || obj.dir == "S" { "North" } else { "West" };
                let wall_enum = if enum_name == "North" { "NorthDoor" } else { "WestDoor" };
                put_over(map, level, "Doors", obj.x, obj.y, tile_named(entry(building, obj.tile), enum_name));
                put_over(map, level, "Frames", obj.x, obj.y, tile_named(entry(building, obj.frame), enum_name));
                put_over(map, level, "Walls", obj.x, obj.y, tile_named(wall_entry_at(building, floor, obj.x, obj.y), wall_enum));
            }
            "window" => {
                let enum_name = if obj.dir == "N" || obj.dir == "S" { "North" } else { "West" };
                let wall_enum = if enum_name == "North" { "NorthWindow" } else { "WestWindow" };
                put_over(map, level, "Windows", obj.x, obj.y, tile_named(entry(building, obj.tile), enum_name));
                put_over(map, level, "Curtains", obj.x, obj.y, tile_named(entry(building, obj.curtains), enum_name));
                put_over(map, level, "Walls", obj.x, obj.y, tile_named(wall_entry_at(building, floor, obj.x, obj.y), wall_enum));
                let _ = obj.shutters;
            }
            "stairs" => {
                let north = obj.dir == "N" || obj.dir == "S";
                for i in 1..=3 {
                    let (x, y) = if north { (obj.x, obj.y + i) } else { (obj.x + i, obj.y) };
                    let enum_name = if north { format!("North{i}") } else { format!("West{i}") };
                    put(map, level, "Furniture", x, y, tile_named(entry(building, obj.tile), &enum_name));
                }
            }
            "furniture" => paint_furniture(building, map, level, obj),
            "roof" => paint_roof(building, map, level, obj),
            _ => {}
        }
    }
    for y in 0..h {
        for x in 0..w {
            let id = room_index(x, y);
            if id > 0 {
                if let Some(room) = building.rooms.get((id as usize) - 1) {
                    put(map, level, "Floor", x, y, tile_named(entry(building, room.floor), "Floor")
                        .or_else(|| tile_named(entry(building, room.floor), "West")));
                    put(map, level, "FloorGrime", x, y, tile_named(entry(building, room.grime_floor), "West"));
                    put(map, level, "WallGrime", x, y, tile_named(entry(building, room.grime_wall), "West"));
                }
            }
        }
    }
    if level > 0 {
        // Depth-three flat tops are the floor of the storey above. LayoutToSquares
        // writes them after that storey's own floors, so they replace it.
        for obj in &building.floors[(level - 1) as usize].objects {
            if obj.kind == "roof" && obj.depth == "Three" {
                paint_flat_top(building, map, level, obj);
            }
        }
        // Stairs on the floor below remove the floor they rise through.
        for obj in &building.floors[(level - 1) as usize].objects {
            if obj.kind != "stairs" {
                continue;
            }
            let north = obj.dir == "N" || obj.dir == "S";
            for i in 1..=3 {
                let (x, y) = if north { (obj.x, obj.y + i) } else { (obj.x + i, obj.y) };
                if let Some(layer) = layer(map, level, "Floor") {
                    layer.set(x, y, None);
                }
            }
        }
    }
}

fn wall_entry_at<'a>(building: &'a Building, floor: &Floor, x: i32, y: i32) -> Option<&'a Entry> {
    let id = floor.rooms.get(y as usize).and_then(|row| row.get(x as usize)).copied().unwrap_or(0);
    if id > 0 {
        entry(building, building.rooms.get((id as usize) - 1).and_then(|r| r.interior_wall))
    } else {
        None
    }
}

fn paint_wall(building: &Building, map: &mut GameMap, level: i32, x: i32, y: i32, dir: &str, room: Option<&Room>, exterior: bool) {
    let (wall, trim) = if exterior {
        (None, None)
    } else if let Some(room) = room {
        (room.interior_wall, room.interior_trim)
    } else {
        (None, None)
    };
    // Exterior walls come from the building's wall objects, which overwrite this.
    let enum_name = if dir == "N" { "North" } else { "West" };
    put(map, level, "Walls", x, y, tile_named(entry(building, wall), enum_name));
    put(map, level, "WallTrim", x, y, tile_named(entry(building, trim), enum_name));
}

fn paint_furniture(building: &Building, map: &mut GameMap, level: i32, obj: &Obj) {
    let Some(index) = obj.furniture else { return };
    let Some(piece) = building.furniture.get(index) else { return };
    let tiles = piece.orients.get(&obj.orient).or_else(|| piece.orients.values().next());
    let Some(tiles) = tiles else { return };
    let layer_name = if piece.layer.is_empty() { "Furniture" } else { piece.layer.as_str() };
    for (x, y, name) in tiles {
        if !name.is_empty() {
            put(map, level, layer_name, obj.x + x, obj.y + y, Some(name));
        }
    }
}

fn depth_enums(depth: &str, axis: &str) -> Vec<String> {
    match depth {
        "Point5" | "0.5" => vec![format!("SlopePt5{axis}")],
        "One" | "1" => vec![format!("Slope{axis}1")],
        "OnePoint5" | "1.5" => vec![format!("SlopeOnePt5{axis}"), format!("Slope{axis}1")],
        "Two" | "2" => vec![format!("Slope{axis}2"), format!("Slope{axis}1")],
        "TwoPoint5" | "2.5" => vec![format!("SlopeTwoPt5{axis}"), format!("Slope{axis}2"), format!("Slope{axis}1")],
        _ => vec![format!("Slope{axis}3"), format!("Slope{axis}2"), format!("Slope{axis}1")],
    }
}

fn slope_thickness(depth: &str) -> i32 {
    match depth {
        "Point5" | "0.5" | "One" | "1" => 1,
        "OnePoint5" | "1.5" | "Two" | "2" => 2,
        _ => 3,
    }
}

/// `RoofObject::resize` as the constructor calls it. Peak30 hips become a
/// square snapped to 1, 3, 5, 7, 9, or 11. Gables snap only the slope span.
fn roof_size(kind: &str, w: i32, h: i32) -> (i32, i32) {
    let snap = |n: i32| {
        if n > 9 { 11 } else if n > 7 { 9 } else if n > 5 { 7 } else if n > 3 { 5 } else if n > 1 { 3 } else { 1 }
    };
    match kind {
        "Peak30Quad" => {
            let side = snap(w.max(h));
            (side, side)
        }
        "Peak30WE" => (w, snap(h)),
        "Peak30NS" => (snap(w), h),
        _ => (w, h),
    }
}

/// `RoofObject::slopeTiles`, the four cap lists, then `flatTop`.
fn paint_roof(building: &Building, map: &mut GameMap, level: i32, obj: &Obj) {
    let slopes = entry(building, obj.slope_tiles.or(obj.tile));
    let caps = entry(building, obj.cap_tiles);
    let kind = obj.roof_type.as_str();
    let (w, h) = roof_size(kind, if obj.width > 0 { obj.width } else { 1 }, if obj.height > 0 { obj.height } else { 1 });
    let thick = slope_thickness(&obj.depth);
    let kind = obj.roof_type.as_str();
    let paint_slope = |map: &mut GameMap, x, y, name: &str| {
        put_stack(map, level, "Roof", "Roof2", x, y, tile_named(slopes, name));
    };
    match kind {
        "SlopeE" | "PeakNS" | "DormerN" | "DormerS" => {
            let enums = depth_enums(&obj.depth, "E");
            let (x0, span) = if kind == "PeakNS" {
                (obj.x + w - thick, thick)
            } else if kind == "DormerN" || kind == "DormerS" {
                (obj.x + w - thick, thick)
            } else {
                (obj.x, thick.min(w))
            };
            let y0 = if kind == "DormerN" { obj.y } else if kind == "DormerS" { obj.y + thick } else { obj.y };
            let rows = if kind == "DormerN" || kind == "DormerS" { (h - thick).max(0) } else { h };
            for (i, enum_name) in enums.iter().enumerate() {
                if (i as i32) >= span {
                    break;
                }
                for y in 0..rows {
                    paint_slope(map, x0 + i as i32, y0 + y, enum_name);
                }
            }
        }
        "SlopeS" | "PeakWE" | "DormerW" | "DormerE" => {
            let enums = depth_enums(&obj.depth, "S");
            let y0 = obj.y + h - thick;
            let x0 = if kind == "DormerE" { obj.x + thick } else { obj.x };
            let cols = if kind == "DormerW" || kind == "DormerE" { (w - thick).max(0) } else { w };
            for (i, enum_name) in enums.iter().enumerate() {
                for x in 0..cols {
                    paint_slope(map, x0 + x, y0 + i as i32, enum_name);
                }
            }
        }
        "ShallowSlopeW" | "ShallowPeakNS" => {
            for y in 0..h {
                paint_slope(map, obj.x, obj.y + y, "ShallowSlopeW1");
                if w > 1 {
                    paint_slope(map, obj.x + 1, obj.y + y, "ShallowSlopeW2");
                }
            }
        }
        "Peak30WE" => {
            for (y, name) in peak30_line(h, "Peak30NS", "Slope30N", "Slope30S").iter().enumerate() {
                for x in 0..w {
                    paint_slope(map, obj.x + x, obj.y + y as i32, name);
                }
            }
        }
        "Peak30NS" => {
            let line = peak30_line(w, "Peak30WE", "Slope30W", "Slope30E");
            for y in 0..h {
                for (x, name) in line.iter().enumerate() {
                    paint_slope(map, obj.x + x as i32, obj.y + y, name);
                }
            }
        }
        "Peak30Quad" => {
            for (i, name) in peak30_quad(w, h).into_iter().enumerate() {
                let Some(name) = name else { continue };
                paint_slope(map, obj.x + (i as i32 % w), obj.y + (i as i32 / w), &name);
            }
        }
        _ => {}
    }
    let paint_cap = |map: &mut GameMap, x, y, name: &str| {
        put_stack(map, level, "RoofCap", "RoofCap2", x, y, tile_named(caps, name));
    };
    if obj.capped_w {
        for (i, name) in cap_we(&obj.depth, h, kind).iter().enumerate() {
            paint_cap(map, obj.x, obj.y + i as i32, name);
        }
    }
    if obj.capped_e {
        for (i, name) in cap_we(&obj.depth, h, kind).iter().enumerate() {
            paint_cap(map, obj.x + w, obj.y + i as i32, name);
        }
    }
    if obj.capped_n {
        for (i, name) in cap_ns(&obj.depth, w, kind).iter().enumerate() {
            paint_cap(map, obj.x + i as i32, obj.y, name);
        }
    }
    if obj.capped_s {
        for (i, name) in cap_ns(&obj.depth, w, kind).iter().enumerate() {
            paint_cap(map, obj.x + i as i32, obj.y + h, name);
        }
    }
    // Depth Three is deferred until the storey above has laid its floors.
    if obj.depth != "Three" {
        paint_flat_top(building, map, level, obj);
    }
}

/// `RoofObject::slopeTiles` for Peak30WE / Peak30NS. `mid` is the ridge
/// prefix, `fall` the low index, `rise` the high index. Odd spans 3..11 fill
/// every cell; that is the range BuildingEd allows.
fn peak30_line(span: i32, mid: &str, fall: &str, rise: &str) -> Vec<String> {
    let n = span.max(0) as usize;
    let mut pat = vec![String::new(); n];
    if n == 0 {
        return pat;
    }
    let mid_i = n / 2;
    pat[mid_i] = format!("{mid}1");
    for (i, need) in [3, 5, 7, 9, 11].into_iter().enumerate() {
        if span >= need {
            pat[i] = format!("{fall}{}", i + 1);
            pat[n - 1 - i] = format!("{rise}{}", i + 1);
            pat[mid_i] = format!("{mid}{}", i + 2);
        }
    }
    pat
}

/// `RoofObject::slopeTiles` case Peak30Quad, via QuadRoofTiler::ring.
fn peak30_quad(w: i32, h: i32) -> Vec<Option<String>> {
    let mut tiles = vec![None; (w.max(0) * h.max(0)) as usize];
    let mut set = |x: i32, y: i32, name: String| {
        if x >= 0 && y >= 0 && x < w && y < h {
            tiles[(x + y * w) as usize] = Some(name);
        }
    };
    if w == 1 {
        set(0, 0, "Peak30Quad1".to_string());
    }
    for k in 1..=5 {
        if w < k * 2 + 1 {
            break;
        }
        let ring = k - 1;
        let wh = w - ring * 2;
        let x0 = ring;
        let y0 = ring;
        set(w / 2, w / 2, format!("Peak30Quad{}", k + 1));
        set(x0, y0, format!("OuterSlope30NW{k}"));
        set(x0 + wh - 1, y0, format!("OuterSlope30NE{k}"));
        set(x0 + wh - 1, y0 + wh - 1, format!("OuterSlope30SE{k}"));
        set(x0, y0 + wh - 1, format!("OuterSlope30SW{k}"));
        for dx in 1..wh - 1 {
            set(x0 + dx, y0, format!("Slope30N{k}"));
            set(x0 + dx, y0 + wh - 1, format!("Slope30S{k}"));
        }
        for dy in 1..wh - 1 {
            set(x0, y0 + dy, format!("Slope30W{k}"));
            set(x0 + wh - 1, y0 + dy, format!("Slope30E{k}"));
        }
    }
    tiles
}

fn cap_we(depth: &str, height: i32, kind: &str) -> Vec<String> {
    if kind == "Peak30WE" {
        return peak30_line(height, "CapPeak30E", "CapSlope30FallE", "CapSlope30RiseE");
    }
    if kind != "PeakWE" && kind != "DormerW" && kind != "DormerE" {
        return Vec::new();
    }
    match depth {
        "Three" => {
            let mut names = vec!["CapFallE1".into(), "CapFallE2".into(), "CapFallE3".into()];
            for _ in 0..(height - 6).max(0) {
                names.push("CapGapE3".into());
            }
            names.extend(["CapRiseE3", "CapRiseE2", "CapRiseE1"].map(str::to_string));
            names
        }
        "TwoPoint5" => names(&["CapFallE1", "CapFallE2", "PeakTwoPt5E", "CapRiseE2", "CapRiseE1"]),
        "Two" => names(&["CapFallE1", "CapFallE2", "CapRiseE2", "CapRiseE1"]),
        "OnePoint5" => names(&["CapFallE1", "PeakOnePt5E", "CapRiseE1"]),
        "One" => names(&["CapFallE1", "CapRiseE1"]),
        "Point5" => names(&["PeakPt5E"]),
        _ => Vec::new(),
    }
}

fn cap_ns(depth: &str, width: i32, kind: &str) -> Vec<String> {
    if kind == "Peak30NS" {
        return peak30_line(width, "CapPeak30S", "CapSlope30RiseS", "CapSlope30FallS");
    }
    if kind != "PeakNS" && kind != "DormerN" && kind != "DormerS" {
        return Vec::new();
    }
    match depth {
        "Three" => {
            let mut names = vec!["CapRiseS1".into(), "CapRiseS2".into(), "CapRiseS3".into()];
            for _ in 0..(width - 6).max(0) {
                names.push("CapGapS3".into());
            }
            names.extend(["CapFallS3", "CapFallS2", "CapFallS1"].map(str::to_string));
            names
        }
        "TwoPoint5" => names(&["CapRiseS1", "CapRiseS2", "PeakTwoPt5S", "CapFallS2", "CapFallS1"]),
        "Two" => names(&["CapRiseS1", "CapRiseS2", "CapFallS2", "CapFallS1"]),
        "OnePoint5" => names(&["CapRiseS1", "PeakOnePt5S", "CapFallS1"]),
        "One" => names(&["CapRiseS1", "CapFallS1"]),
        "Point5" => names(&["PeakPt5S"]),
        _ => Vec::new(),
    }
}

fn names(items: &[&str]) -> Vec<String> {
    items.iter().map(|item| (*item).to_string()).collect()
}

/// `ReplaceRoofTop`. A loaded roof has no direction, so `isN()` is false and
/// the top tile is the West enum for that depth.
fn paint_flat_top(building: &Building, map: &mut GameMap, level: i32, obj: &Obj) {
    let w = if obj.width > 0 { obj.width } else { 1 };
    let h = if obj.height > 0 { obj.height } else { 1 };
    let kind = obj.roof_type.as_str();
    let flat = match kind {
        "FlatTop" => Some((obj.x, obj.y, w, h)),
        "PeakWE" | "DormerW" | "DormerE" if h > 6 => Some((obj.x, obj.y + 3, w, h - 6)),
        "PeakNS" | "DormerN" | "DormerS" if w > 6 => Some((obj.x + 3, obj.y, w - 6, h)),
        _ => None,
    };
    let Some((x, y, fw, fh)) = flat else { return };
    let enum_name = match obj.depth.as_str() {
        "One" | "1" => "West1",
        "Two" | "2" => "West2",
        _ => "West3",
    };
    let tile = tile_named(entry(building, obj.top_tiles.or(obj.tile)), enum_name);
    let on_floor = obj.depth == "Zero" || obj.depth == "Three" || obj.depth.is_empty();
    let layer_name = if on_floor { "Floor" } else { "RoofTop" };
    for yy in y..y + fh {
        for xx in x..x + fw {
            put_over(map, level, layer_name, xx, yy, tile);
        }
    }
}

fn paint_user(_building: &Building, floor: &Floor, level: i32, map: &mut GameMap) {
    // BuildingMap::mergedMap: a non-empty user cell replaces the auto-tile.
    for (name, cells) in &floor.user {
        for (i, tile) in cells.iter().enumerate() {
            let Some(tile) = tile else { continue };
            let x = (i as i32) % floor.user_w;
            let y = (i as i32) / floor.user_w;
            put_over(map, level, name, x, y, Some(tile));
        }
    }
}

fn room_rects(building: &Building, floor: &Floor, level: i32) -> Vec<RoomDef> {
    // BuildingRoomDefecator: horizontal runs, then a scanline flood that
    // stops on west/north walls, then vertical merge of equal-width rects.
    let mut rects = Vec::new();
    let mut room_serial = 1i32;
    for (index, room) in building.rooms.iter().enumerate() {
        let id = (index + 1) as i32;
        let mut region = Vec::new();
        for y in 0..building.height {
            let mut x = 0;
            while x < building.width {
                if floor.rooms[y as usize][x as usize] != id {
                    x += 1;
                    continue;
                }
                let mut wid = 0;
                while x + wid < building.width && floor.rooms[y as usize][(x + wid) as usize] == id {
                    wid += 1;
                }
                region.push((x, y, wid));
                x += wid;
            }
        }
        let walls = wall_sets(floor);
        let mut seen = vec![false; (building.width * building.height) as usize];
        let in_room = |x: i32, y: i32| {
            y >= 0 && x >= 0 && x < building.width && y < building.height
                && floor.rooms[y as usize][x as usize] == id
        };
        let west_wall = |x: i32, y: i32| in_room(x, y) && !in_room(x - 1, y) || walls.west.contains(&(x, y));
        let north_wall = |x: i32, y: i32| in_room(x, y) && !in_room(x, y - 1) || walls.north.contains(&(x, y));
        let should = |x1: i32, y1: i32, x2: i32, y2: i32, seen: &Vec<bool>| {
            if !in_room(x1, y1) || !in_room(x2, y2) {
                return false;
            }
            let key = (y2 * building.width + x2) as usize;
            if seen.get(key).copied().unwrap_or(true) {
                return false;
            }
            if x2 < x1 && west_wall(x1, y1) {
                return false;
            }
            if x2 > x1 && west_wall(x2, y2) {
                return false;
            }
            if y2 < y1 && north_wall(x1, y1) {
                return false;
            }
            if y2 > y1 && north_wall(x2, y2) {
                return false;
            }
            true
        };
        for (sx, sy, sw) in region {
            for dx in 0..sw {
                let x = sx + dx;
                let key = (sy * building.width + x) as usize;
                if seen[key] {
                    continue;
                }
                let mut cells = Vec::new();
                flood(building.width, building.height, x, sy, &mut seen, &mut cells, &should);
                let merged = merge_vertical(&cells);
                for (rx, ry, rw, rh) in merged {
                    rects.push(RoomDef {
                        name: format!("{}#{}", room.internal, room_serial),
                        x: rx,
                        y: ry,
                        w: rw,
                        h: rh,
                        level,
                    });
                }
                room_serial += 1;
            }
        }
        let _ = room.name;
    }
    rects
}

struct WallSets {
    north: std::collections::HashSet<(i32, i32)>,
    west: std::collections::HashSet<(i32, i32)>,
}

fn wall_sets(floor: &Floor) -> WallSets {
    let mut north = std::collections::HashSet::new();
    let mut west = std::collections::HashSet::new();
    for obj in &floor.objects {
        if obj.kind != "wall" || obj.interior.is_none() {
            continue;
        }
        if obj.dir == "N" {
            for y in obj.y..obj.y + obj.length {
                west.insert((obj.x, y));
            }
        } else {
            for x in obj.x..obj.x + obj.length {
                north.insert((x, obj.y));
            }
        }
    }
    WallSets { north, west }
}

fn flood(
    width: i32,
    height: i32,
    x0: i32,
    y0: i32,
    seen: &mut Vec<bool>,
    cells: &mut Vec<(i32, i32)>,
    should: &dyn Fn(i32, i32, i32, i32, &Vec<bool>) -> bool,
) {
    let mut stack = vec![(x0, y0)];
    seen[(y0 * width + x0) as usize] = true;
    while let Some((x, mut y)) = stack.pop() {
        let mut y1 = y;
        while should(x, y1, x, y1 - 1, seen) {
            y1 -= 1;
        }
        let mut span_left = false;
        let mut span_right = false;
        while should(x, y, x, y1, seen) {
            seen[(y1 * width + x) as usize] = true;
            cells.push((x, y1));
            if !span_left && x > 0 && should(x, y1, x - 1, y1, seen) {
                stack.push((x - 1, y1));
                span_left = true;
            } else if span_left && x > 0 && !should(x, y1, x - 1, y1, seen) {
                span_left = false;
            }
            if !span_right && x < width - 1 && should(x, y1, x + 1, y1, seen) {
                stack.push((x + 1, y1));
                span_right = true;
            } else if span_right && x < width - 1 && !should(x, y1, x + 1, y1, seen) {
                span_right = false;
            }
            y = y1;
            y1 += 1;
        }
    }
    let _ = height;
}

fn merge_vertical(cells: &[(i32, i32)]) -> Vec<(i32, i32, i32, i32)> {
    let mut rows: HashMap<i32, Vec<i32>> = HashMap::new();
    for (x, y) in cells {
        rows.entry(*y).or_default().push(*x);
    }
    let mut rects = Vec::new();
    for (y, xs) in rows {
        let mut xs = xs;
        xs.sort_unstable();
        xs.dedup();
        let mut start = xs[0];
        let mut prev = xs[0];
        for x in xs.into_iter().skip(1) {
            if x == prev + 1 {
                prev = x;
            } else {
                rects.push((start, y, prev - start + 1, 1));
                start = x;
                prev = x;
            }
        }
        rects.push((start, y, prev - start + 1, 1));
    }
    let mut changed = true;
    while changed {
        changed = false;
        for i in 0..rects.len() {
            for j in 0..rects.len() {
                if i == j {
                    continue;
                }
                let (x, y, w, h) = rects[i];
                let (x2, y2, w2, h2) = rects[j];
                if w == 0 || w2 == 0 {
                    continue;
                }
                if x == x2 && w == w2 && y + h == y2 {
                    rects[i] = (x, y, w, h + h2);
                    rects[j] = (0, 0, 0, 0);
                    changed = true;
                }
            }
        }
    }
    rects.into_iter().filter(|r| r.2 > 0 && r.3 > 0).collect()
}
