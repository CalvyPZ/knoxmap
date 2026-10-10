// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// WorldReader's .pzw subset, plus lots on other levels, object properties,
// and the 300-to-256 overlap in CombinedCellMaps::toCellRect256 / toCellRect300.

//! Load a WorldEd project and map 300-tile source cells onto 256-tile lot cells.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::xmltree;

pub const SOURCE_CELL: i32 = 300;
pub const LOT_CELL: i32 = 256;

#[derive(Clone, Debug)]
pub struct Property {
    pub name: String,
    pub value: String,
}

#[derive(Clone, Debug)]
pub struct LotRef {
    pub x: i32,
    pub y: i32,
    pub level: i32,
    pub width: i32,
    pub height: i32,
    pub map: String,
}

#[derive(Clone, Debug)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

#[derive(Clone, Debug)]
pub enum Geometry {
    Rect,
    Point,
    Polygon,
    Polyline,
}

#[derive(Clone, Debug)]
pub struct Object {
    pub name: String,
    pub group: String,
    pub type_name: String,
    pub x: f64,
    pub y: f64,
    pub level: i32,
    pub width: f64,
    pub height: f64,
    pub geometry: Geometry,
    pub line_width: f64,
    pub points: Vec<Point>,
    pub properties: Vec<Property>,
}

#[derive(Clone, Debug)]
pub struct Cell {
    pub x: i32,
    pub y: i32,
    pub map: String,
    pub chunk_override: String,
    pub lots: Vec<LotRef>,
    pub objects: Vec<Object>,
    pub properties: Vec<Property>,
}

#[derive(Clone, Debug)]
pub struct BmpSettings {
    pub export_dir: String,
    pub rules: String,
    pub blends: String,
    pub map_base: String,
    pub assign_maps: bool,
    pub warn_unknown: bool,
    pub repair_unknown: bool,
    pub unknown_ground: String,
    pub unknown_veg: String,
    pub compress: bool,
    pub copy_pixels: bool,
    pub update_existing: bool,
}

#[derive(Clone, Debug)]
pub struct Project {
    pub path: PathBuf,
    pub dir: PathBuf,
    pub width: i32,
    pub height: i32,
    /// Empty cellFormat and `legacy-300` are the 300 grid. `native-256` is direct.
    pub legacy_300: bool,
    pub bmp_path: String,
    pub bmp_x: i32,
    pub bmp_y: i32,
    pub bmp_width: i32,
    pub bmp_height: i32,
    pub bmp: BmpSettings,
    pub lots_dir: String,
    pub zombie_map: String,
    pub tile_def_folder: String,
    pub world_origin_x: i32,
    pub world_origin_y: i32,
    pub threads: i32,
    pub spawn_points_file: String,
    pub world_objects_file: String,
    pub room_tones_file: String,
    pub cells: HashMap<(i32, i32), Cell>,
    pub object_types: Vec<String>,
    pub object_groups: Vec<(String, String)>,
    /// Original file text, so map assignments can be written back without a rewrite.
    pub text: String,
}

pub fn load_project(path: &Path) -> Result<Project, String> {
    let path = if path.is_dir() {
        let name = path
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| "project directory has no name".to_string())?;
        path.join(format!("{name}.pzw"))
    } else {
        path.to_path_buf()
    };
    let text = std::fs::read_to_string(&path).map_err(|err| format!("{}: {err}", path.display()))?;
    let root = xmltree::parse(&text)?;
    if root.name != "world" {
        return Err(format!("expected <world>, found <{}>", root.name));
    }
    let width = parse_i32(root.attr("width"))?;
    let height = parse_i32(root.attr("height"))?;
    let format = root.attr("cellFormat");
    let legacy_300 = format.is_empty() || format == "legacy-300";
    if !legacy_300 && format != "native-256" {
        return Err(format!("unknown cellFormat '{format}'"));
    }
    let dir = path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));

    let mut project = Project {
        path: path.clone(),
        dir,
        width,
        height,
        legacy_300,
        bmp_path: String::new(),
        bmp_x: 0,
        bmp_y: 0,
        bmp_width: width,
        bmp_height: height,
        bmp: BmpSettings {
            export_dir: String::new(),
            rules: String::new(),
            blends: String::new(),
            map_base: String::new(),
            assign_maps: true,
            warn_unknown: true,
            repair_unknown: false,
            unknown_ground: String::new(),
            unknown_veg: String::new(),
            compress: true,
            copy_pixels: true,
            update_existing: false,
        },
        lots_dir: String::new(),
        zombie_map: String::new(),
        tile_def_folder: String::new(),
        world_origin_x: 0,
        world_origin_y: 0,
        threads: 1,
        spawn_points_file: String::new(),
        world_objects_file: String::new(),
        room_tones_file: String::new(),
        cells: HashMap::new(),
        object_types: Vec::new(),
        object_groups: Vec::new(),
        text,
    };

    for child in &root.children {
        match child.name.as_str() {
            "BMPToTMX" => read_bmp_settings(&mut project, child),
            "GenerateLots" => read_lots_settings(&mut project, child),
            "LuaSettings" => read_lua_settings(&mut project, child),
            "bmp" => {
                project.bmp_path = child.attr("path").to_string();
                project.bmp_x = parse_i32(child.attr("x")).unwrap_or(0);
                project.bmp_y = parse_i32(child.attr("y")).unwrap_or(0);
                project.bmp_width = parse_i32(child.attr("width")).unwrap_or(width);
                project.bmp_height = parse_i32(child.attr("height")).unwrap_or(height);
            }
            "objecttype" => project.object_types.push(child.attr("name").to_string()),
            "objectgroup" => project.object_groups.push((
                child.attr("name").to_string(),
                child.attr("color").to_string(),
            )),
            "cell" => {
                let cell = read_cell(child)?;
                project.cells.insert((cell.x, cell.y), cell);
            }
            _ => {}
        }
    }
    Ok(project)
}

fn read_bmp_settings(project: &mut Project, elem: &xmltree::Elem) {
    if let Some(node) = elem.child("tmxexportdir") {
        project.bmp.export_dir = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("rulesfile") {
        project.bmp.rules = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("blendsfile") {
        project.bmp.blends = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("mapbasefile") {
        project.bmp.map_base = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("assign-maps-to-world") {
        project.bmp.assign_maps = checked(node.attr("checked"));
    }
    if let Some(node) = elem.child("warn-unknown-colors") {
        project.bmp.warn_unknown = checked(node.attr("checked"));
    }
    if let Some(node) = elem.child("repair-unknown-colors") {
        project.bmp.repair_unknown = checked(node.attr("checked"));
        project.bmp.unknown_ground = node.attr("ground").to_string();
        project.bmp.unknown_veg = node.attr("vegetation").to_string();
    }
    if let Some(node) = elem.child("compress") {
        project.bmp.compress = checked(node.attr("checked"));
    }
    if let Some(node) = elem.child("copy-pixels") {
        project.bmp.copy_pixels = checked(node.attr("checked"));
    }
    if let Some(node) = elem.child("update-existing") {
        project.bmp.update_existing = checked(node.attr("checked"));
    }
}

fn read_lots_settings(project: &mut Project, elem: &xmltree::Elem) {
    if let Some(node) = elem.child("exportdir") {
        project.lots_dir = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("ZombieSpawnMap") {
        project.zombie_map = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("TileDefFolder") {
        project.tile_def_folder = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("worldOrigin") {
        let origin = node.attr("origin");
        let mut parts = origin.split(',');
        project.world_origin_x = parts.next().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
        project.world_origin_y = parts.next().and_then(|s| s.trim().parse().ok()).unwrap_or(0);
    }
    if let Some(node) = elem.child("numberOfThreads") {
        project.threads = parse_i32(node.attr("count")).unwrap_or(1).max(1);
    }
}

fn read_lua_settings(project: &mut Project, elem: &xmltree::Elem) {
    if let Some(node) = elem.child("spawnPointsFile") {
        project.spawn_points_file = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("worldObjectsFile") {
        project.world_objects_file = node.attr("path").to_string();
    }
    if let Some(node) = elem.child("roomTonesFile") {
        project.room_tones_file = node.attr("path").to_string();
    }
}

fn read_cell(elem: &xmltree::Elem) -> Result<Cell, String> {
    let mut cell = Cell {
        x: parse_i32(elem.attr("x"))?,
        y: parse_i32(elem.attr("y"))?,
        map: elem.attr("map").to_string(),
        chunk_override: elem.attr("chunkDataOverride").to_string(),
        lots: Vec::new(),
        objects: Vec::new(),
        properties: Vec::new(),
    };
    for child in &elem.children {
        match child.name.as_str() {
            "lot" => cell.lots.push(LotRef {
                x: parse_i32(child.attr("x")).unwrap_or(0),
                y: parse_i32(child.attr("y")).unwrap_or(0),
                level: parse_i32(child.attr("level")).unwrap_or(0).clamp(-32, 31),
                width: parse_i32(child.attr("width")).unwrap_or(0),
                height: parse_i32(child.attr("height")).unwrap_or(0),
                map: child.attr("map").to_string(),
            }),
            "object" => cell.objects.push(read_object(child)?),
            "property" => cell.properties.push(Property {
                name: child.attr("name").to_string(),
                value: child.attr("value").to_string(),
            }),
            _ => {}
        }
    }
    Ok(cell)
}

fn read_object(elem: &xmltree::Elem) -> Result<Object, String> {
    let geometry = match elem.attr("geometry") {
        "point" => Geometry::Point,
        "polygon" => Geometry::Polygon,
        "polyline" => Geometry::Polyline,
        _ => Geometry::Rect,
    };
    let mut points = Vec::new();
    if !elem.attr("points").is_empty() {
        for pair in elem.attr("points").split_whitespace() {
            let mut xy = pair.split(',');
            let x = xy.next().and_then(|s| s.parse().ok()).unwrap_or(0.0);
            let y = xy.next().and_then(|s| s.parse().ok()).unwrap_or(0.0);
            points.push(Point { x, y });
        }
    }
    let mut properties = Vec::new();
    for child in elem.children_named("property") {
        properties.push(Property {
            name: child.attr("name").to_string(),
            value: child.attr("value").to_string(),
        });
    }
    Ok(Object {
        name: elem.attr("name").to_string(),
        group: elem.attr("group").to_string(),
        type_name: elem.attr("type").to_string(),
        x: elem.attr("x").parse().unwrap_or(0.0),
        y: elem.attr("y").parse().unwrap_or(0.0),
        level: parse_i32(elem.attr("level")).unwrap_or(0),
        width: elem.attr("width").parse().unwrap_or(0.0),
        height: elem.attr("height").parse().unwrap_or(0.0),
        geometry,
        line_width: elem.attr("lineWidth").parse().unwrap_or(0.0),
        points,
        properties,
    })
}

fn checked(value: &str) -> bool {
    value == "true" || value == "1"
}

fn parse_i32(value: &str) -> Result<i32, String> {
    value
        .trim()
        .parse()
        .map_err(|_| format!("expected an integer, found '{value}'"))
}

/// A path as this compiler opens it. Wine `Z:` names are only a fallback for
/// projects written for the Windows editor; a native compile keeps native paths.
pub fn native_path(project_dir: &Path, stored: &str) -> PathBuf {
    if stored.is_empty() {
        return PathBuf::new();
    }
    let slash = stored.replace('\\', "/");
    let direct = PathBuf::from(&slash);
    if direct.exists() {
        return direct;
    }
    if let Some(rest) = slash
        .strip_prefix("Z:")
        .or_else(|| slash.strip_prefix("z:"))
    {
        let unix = PathBuf::from(rest);
        if unix.exists() {
            return unix;
        }
    }
    let beside = project_dir.join(stored);
    if beside.exists() {
        return beside;
    }
    let beside_slash = project_dir.join(slash.trim_start_matches('/'));
    if beside_slash.exists() {
        return beside_slash;
    }
    direct
}

pub fn forward_slash(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

/// `CombinedCellMaps::toCellRect256`. Returns x, y, width, height.
pub fn to_cell_rect_256(x: i32, y: i32, w: i32, h: i32) -> (i32, i32, i32, i32) {
    let min_x = ((x as f32) * (SOURCE_CELL as f32) / (LOT_CELL as f32)).floor() as i32;
    let min_y = ((y as f32) * (SOURCE_CELL as f32) / (LOT_CELL as f32)).floor() as i32;
    let max_x = ((((x + w) as f32) * (SOURCE_CELL as f32) - 1.0) / (LOT_CELL as f32)).ceil() as i32;
    let max_y = ((((y + h) as f32) * (SOURCE_CELL as f32) - 1.0) / (LOT_CELL as f32)).ceil() as i32;
    (min_x, min_y, max_x - min_x, max_y - min_y)
}

/// `CombinedCellMaps::toCellRect300`.
pub fn to_cell_rect_300(x: i32, y: i32, w: i32, h: i32) -> (i32, i32, i32, i32) {
    let min_x = ((x as f32) * (LOT_CELL as f32) / (SOURCE_CELL as f32)).floor() as i32;
    let min_y = ((y as f32) * (LOT_CELL as f32) / (SOURCE_CELL as f32)).floor() as i32;
    let max_x = ((((x + w) as f32) * (LOT_CELL as f32) - 1.0) / (SOURCE_CELL as f32)).ceil() as i32;
    let max_y = ((((y + h) as f32) * (LOT_CELL as f32) - 1.0) / (SOURCE_CELL as f32)).ceil() as i32;
    (min_x, min_y, max_x - min_x, max_y - min_y)
}

/// Lot-cell coordinates a source rectangle owns, matching `_batch_lot_files`.
pub fn lot_cells_for_source(origin_x: i32, origin_y: i32, x0: i32, y0: i32, x1: i32, y1: i32) -> Vec<(i32, i32)> {
    let span = |origin: i32, c0: i32, c1: i32| {
        let tile0 = (origin + c0) * SOURCE_CELL;
        let tile1 = (origin + c1 + 1) * SOURCE_CELL;
        (tile0.div_euclid(LOT_CELL))..(tile1 - 1).div_euclid(LOT_CELL) + 1
    };
    let mut out = Vec::new();
    for ix in span(origin_x, x0, x1) {
        for iy in span(origin_y, y0, y1) {
            out.push((ix, iy));
        }
    }
    out
}

/// 256-cells in the order WorldEd enqueues them: source cells y then x, first
/// sight of each lot cell wins.
pub fn lot_cells_in_world_order(project: &Project, only: &[(i32, i32, i32, i32)]) -> Vec<(i32, i32)> {
    let mut seen = std::collections::BTreeSet::new();
    let mut order = Vec::new();
    let push = |cell: (i32, i32), seen: &mut std::collections::BTreeSet<_>, order: &mut Vec<_>| {
        if seen.insert(cell) {
            order.push(cell);
        }
    };
    if project.legacy_300 {
        for y in 0..project.height {
            for x in 0..project.width {
                if !only.is_empty() && !only.iter().any(|r| x >= r.0 && x <= r.2 && y >= r.1 && y <= r.3) {
                    continue;
                }
                if !project.cells.contains_key(&(x, y)) && only.is_empty() {
                    continue;
                }
                let abs_x = project.world_origin_x + x;
                let abs_y = project.world_origin_y + y;
                let (lx, ly, lw, lh) = to_cell_rect_256(abs_x, abs_y, 1, 1);
                for ix in lx..lx + lw {
                    for iy in ly..ly + lh {
                        push((ix, iy), &mut seen, &mut order);
                    }
                }
            }
        }
    } else {
        for y in 0..project.height {
            for x in 0..project.width {
                if !only.is_empty() && !only.iter().any(|r| x >= r.0 && x <= r.2 && y >= r.1 && y <= r.3) {
                    continue;
                }
                push(
                    (project.world_origin_x + x, project.world_origin_y + y),
                    &mut seen,
                    &mut order,
                );
            }
        }
    }
    order
}

pub fn source_cells_for_lot(project: &Project, cell_x: i32, cell_y: i32) -> Vec<(i32, i32)> {
    if !project.legacy_300 {
        let x = cell_x - project.world_origin_x;
        let y = cell_y - project.world_origin_y;
        return if project.cells.contains_key(&(x, y)) {
            vec![(x, y)]
        } else {
            Vec::new()
        };
    }
    let (sx, sy, sw, sh) = to_cell_rect_300(cell_x, cell_y, 1, 1);
    let mut out = Vec::new();
    for x in sx..sx + sw {
        for y in sy..sy + sh {
            let local = (x - project.world_origin_x, y - project.world_origin_y);
            if project.cells.contains_key(&local) {
                out.push(local);
            }
        }
    }
    out
}

/// Write `map=` back into the project. Paths stay native, with forward slashes.
pub fn assign_maps(project: &Project, maps: &HashMap<(i32, i32), String>) -> Result<(), String> {
    let mut text = project.text.clone();
    for ((x, y), path) in maps {
        let path = path.replace('\\', "/");
        let pattern = format!("<cell x=\"{x}\" y=\"{y}\" map=\"");
        if let Some(at) = text.find(&pattern) {
            let value_at = at + pattern.len();
            let end = text[value_at..]
                .find('"')
                .map(|n| value_at + n)
                .ok_or_else(|| format!("cell {x},{y} map attribute is not closed"))?;
            text.replace_range(value_at..end, &path);
        }
    }
    if let Some(end) = text.rfind("</world>") {
        let mut stubs = String::new();
        for ((x, y), path) in maps {
            let marker = format!("<cell x=\"{x}\" y=\"{y}\"");
            if text.contains(&marker) {
                continue;
            }
            stubs.push_str(&format!(" <cell x=\"{x}\" y=\"{y}\" map=\"{path}\"/>\n"));
        }
        if !stubs.is_empty() {
            text.insert_str(end, &stubs);
        }
    }
    std::fs::write(&project.path, text).map_err(|err| format!("{}: {err}", project.path.display()))
}
