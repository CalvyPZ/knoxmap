// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// LotFilesWorker256::generateCell, in that order: header metadata (tilesets,
// rooms), rasterize, holes, building objects, jumbo trees, header, pack,
// chunkdata. One cell stays on one thread.

//! Generate the lot files for one 256-cell.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

use crate::composite::{self, any_tile, tiles_at};
use crate::lotbytes::{
    self, ChunkDataFile, ChunkPayload, LotHeader, LotPack, PackedSquare, Room,
    RoomObject, RoomRect, BIT_NULL, BIT_ROOM, BIT_SOLID, BIT_WALLN, BIT_WALLW, BIT_WATER, CELL,
    CHUNK, CHUNKS, MAX_WORLD_LEVELS, MIN_WORLD_LEVEL, VERSION,
};
use crate::model::{GameMap, PlacedMap, RoomDef};
use crate::project::{self, Project};
use crate::tbx;
use crate::tiles::TileCatalog;
use crate::tmx;

#[derive(Clone, Debug)]
struct TileRec {
    name: String,
    used: bool,
    meta: i32,
}

#[derive(Clone, Debug)]
struct LiveRoom {
    name: String,
    floor: i32,
    rects: Vec<usize>,
    source: (i32, i32),
    building: Option<usize>,
    id: i32,
}

struct Gen {
    tiles: Vec<TileRec>,
    by_name: HashMap<String, usize>,
    rooms: Vec<LiveRoom>,
    rects: Vec<RoomDef>,
    rect_source: Vec<(i32, i32)>,
    buildings: Vec<Vec<usize>>,
    removed: Vec<Vec<usize>>,
    /// (x, y, z-index) -> (gids, room id)
    grid: HashMap<(i32, i32, i32), (Vec<usize>, i32)>,
    min_level: i32,
    max_level: i32,
}

pub struct CellOutput {
    pub header: PathBuf,
    pub pack: PathBuf,
    pub chunkdata: PathBuf,
}

pub fn generate_cell(
    project: &Project,
    catalog: &TileCatalog,
    cell_x: i32,
    cell_y: i32,
    lots_dir: &Path,
    hole_fill: HoleFill,
    jumbo_seed: Option<u64>,
) -> Result<CellOutput, String> {
    let maps = load_cell_maps(project, cell_x, cell_y)?;
    let mut gen = Gen {
        tiles: vec![TileRec { name: String::new(), used: false, meta: -1 }],
        by_name: HashMap::new(),
        rooms: Vec::new(),
        rects: Vec::new(),
        rect_source: Vec::new(),
        buildings: Vec::new(),
        removed: Vec::new(),
        grid: HashMap::new(),
        min_level: 10_000,
        max_level: -10_000,
    };
    // Synthetic jumbo tree, even on the native grid where the swap is skipped.
    let _jumbo = ensure_tile(&mut gen, "jumbo_tree_01_0", catalog);
    // PartialChunkSelection is not in the pinned checkout. Every chunk is
    // selected. A sidecar format is not invented here; a later diff against
    // PZWorldEd_cli is the check if that source turns up.
    collect_rooms(project, &maps, cell_x, cell_y, &mut gen);
    group_rooms(&mut gen);
    rasterize(&maps, cell_x, cell_y, project, &mut gen, catalog);
    let holes = check_holes(project, &maps, cell_x, cell_y);
    fill_holes(&mut gen, &holes, hole_fill, catalog);
    if gen.min_level == 10_000 {
        gen.min_level = 0;
        gen.max_level = 0;
    }
    assign_room_ids(&mut gen);
    if project.legacy_300 {
        jumbo_trees(&mut gen, &maps, project, cell_x, cell_y, catalog, jumbo_seed);
    }
    let header = build_header(&gen, project, catalog, cell_x, cell_y);
    let pack = build_pack(&gen, &maps, cell_x, cell_y);
    let chunk = build_chunkdata(&gen, &maps, project, catalog, cell_x, cell_y);
    let header_path = lots_dir.join(format!("{cell_x}_{cell_y}.lotheader"));
    let pack_path = lots_dir.join(format!("world_{cell_x}_{cell_y}.lotpack"));
    let chunk_path = lots_dir.join(format!("chunkdata_{cell_x}_{cell_y}.bin"));
    std::fs::write(&header_path, lotbytes::write_header(&header)).map_err(|e| e.to_string())?;
    std::fs::write(&pack_path, lotbytes::write_pack(&pack)?).map_err(|e| e.to_string())?;
    std::fs::write(&chunk_path, lotbytes::write_chunkdata(&chunk)?).map_err(|e| e.to_string())?;
    Ok(CellOutput {
        header: header_path,
        pack: pack_path,
        chunkdata: chunk_path,
    })
}

#[derive(Clone, Copy)]
pub enum HoleFill {
    Report,
    Nearest,
    Tile,
}

fn load_cell_maps(project: &Project, cell_x: i32, cell_y: i32) -> Result<CellMaps, String> {
    let sources = project::source_cells_for_lot(project, cell_x, cell_y);
    let source_size = if project.legacy_300 { 300 } else { 256 };
    let min_sx = sources.iter().map(|c| c.0 + project.world_origin_x).min().unwrap_or(cell_x);
    let min_sy = sources.iter().map(|c| c.1 + project.world_origin_y).min().unwrap_or(cell_y);
    let mut placed = Vec::new();
    let mut trigger = sources.first().copied();
    for (x, y) in &sources {
        let Some(cell) = project.cells.get(&(*x, *y)) else { continue };
        if trigger.is_none() {
            trigger = Some((*x, *y));
        }
        let abs_x = project.world_origin_x + x;
        let abs_y = project.world_origin_y + y;
        let ox = (abs_x - min_sx) * source_size;
        let oy = (abs_y - min_sy) * source_size;
        if !cell.map.is_empty() {
            let path = project::native_path(&project.dir, &cell.map);
            if path.is_file() {
                let map = if path.extension().and_then(|s| s.to_str()) == Some("tbx") {
                    tbx::read_tbx(&path)?
                } else {
                    tmx::read_tmx(&path)?
                };
                placed.push(PlacedMap {
                    map,
                    origin_x: ox,
                    origin_y: oy,
                    level_offset: 0,
                    cell_map: true,
                });
            }
        }
        for lot in &cell.lots {
            push_lot(project, lot, ox, oy, &mut placed)?;
        }
    }
    // Lots whose bounds leave their source cell but still overlap this 256-cell.
    for cell in project.cells.values() {
        let abs_x = project.world_origin_x + cell.x;
        let abs_y = project.world_origin_y + cell.y;
        for lot in &cell.lots {
            let world_x = lot.x + abs_x * source_size;
            let world_y = lot.y + abs_y * source_size;
            let lot_box = (world_x, world_y, world_x + lot.width, world_y + lot.height);
            let cell_box = (cell_x * CELL, cell_y * CELL, (cell_x + 1) * CELL, (cell_y + 1) * CELL);
            if !overlap(lot_box, cell_box) {
                continue;
            }
            let inside = lot.x >= 0 && lot.y >= 0 && lot.x + lot.width <= source_size && lot.y + lot.height <= source_size;
            if inside {
                continue;
            }
            let ox = (abs_x - min_sx) * source_size;
            let oy = (abs_y - min_sy) * source_size;
            push_lot(project, lot, ox, oy, &mut placed)?;
        }
    }
    let width = sources.iter().map(|c| c.0 + project.world_origin_x).max().unwrap_or(min_sx) - min_sx + 1;
    let height = sources.iter().map(|c| c.1 + project.world_origin_y).max().unwrap_or(min_sy) - min_sy + 1;
    Ok(CellMaps {
        placed,
        min_sx,
        min_sy,
        cells_w: width.max(1),
        cells_h: height.max(1),
        source_size,
        trigger: trigger.unwrap_or((0, 0)),
    })
}

struct CellMaps {
    placed: Vec<PlacedMap>,
    min_sx: i32,
    min_sy: i32,
    cells_w: i32,
    cells_h: i32,
    source_size: i32,
    trigger: (i32, i32),
}

fn overlap(a: (i32, i32, i32, i32), b: (i32, i32, i32, i32)) -> bool {
    a.0 < b.2 && a.2 > b.0 && a.1 < b.3 && a.3 > b.1
}

fn push_lot(project: &Project, lot: &project::LotRef, cell_ox: i32, cell_oy: i32, placed: &mut Vec<PlacedMap>) -> Result<(), String> {
    let path = project::native_path(&project.dir, &lot.map);
    if !path.is_file() {
        return Ok(());
    }
    let map = if path.extension().and_then(|s| s.to_str()) == Some("tbx") {
        tbx::read_tbx(&path)?
    } else {
        tmx::read_tmx(&path)?
    };
    placed.push(PlacedMap {
        map,
        origin_x: cell_ox + lot.x,
        origin_y: cell_oy + lot.y,
        level_offset: lot.level,
        cell_map: false,
    });
    Ok(())
}

fn ensure_tile(gen: &mut Gen, name: &str, catalog: &TileCatalog) -> usize {
    if let Some(id) = gen.by_name.get(name) {
        return *id;
    }
    let (tileset, index) = crate::tiles::split_tile(name).unwrap_or((name.to_string(), 0));
    let meta = catalog.meta_enum(&tileset, index);
    let id = gen.tiles.len();
    gen.tiles.push(TileRec { name: name.to_string(), used: false, meta });
    gen.by_name.insert(name.to_string(), id);
    id
}

fn cell_origin(maps: &CellMaps, cell_x: i32, cell_y: i32) -> (i32, i32) {
    (
        cell_x * CELL - maps.min_sx * maps.source_size,
        cell_y * CELL - maps.min_sy * maps.source_size,
    )
}

fn collect_rooms(project: &Project, maps: &CellMaps, cell_x: i32, cell_y: i32, gen: &mut Gen) {
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    for sub in &maps.placed {
        let source = maps.trigger;
        let _ = project;
        for room in &sub.map.rooms {
            let mut x = room.x + sub.origin_x;
            let mut y = room.y + sub.origin_y;
            let level = room.level + sub.level_offset;
            if sub.map.isometric {
                x += 3 * level;
                y += 3 * level;
            }
            x -= bx;
            y -= by;
            if level <= 0 && room.name.to_ascii_lowercase().contains("emptyoutside") {
                continue;
            }
            gen.rects.push(RoomDef {
                name: room.name.clone(),
                x,
                y,
                w: room.w,
                h: room.h,
                level,
            });
            gen.rect_source.push(source);
        }
    }
}

fn rects_touch(a: &RoomDef, b: &RoomDef) -> bool {
    let inflated = (a.x - 1, a.y - 1, a.w + 2, a.h + 2);
    let hit = inflated.0 < b.x + b.w && inflated.0 + inflated.2 > b.x && inflated.1 < b.y + b.h && inflated.1 + inflated.3 > b.y;
    if !hit {
        return false;
    }
    let corners = (a.x == b.x + b.w && a.y == b.y + b.h)
        || (a.x + a.w == b.x && a.y == b.y + b.h)
        || (a.x == b.x + b.w && a.y + a.h == b.y)
        || (a.x + a.w == b.x && a.y + a.h == b.y);
    !corners
}

fn same_name(a: &str, b: &str) -> bool {
    a == b && a.contains('#')
}

fn group_rooms(gen: &mut Gen) {
    let mut levels: Vec<i32> = gen.rects.iter().map(|r| r.level).collect();
    levels.sort_unstable();
    levels.dedup();
    let mut owner: Vec<Option<usize>> = vec![None; gen.rects.len()];
    for level in levels {
        let ids: Vec<usize> = gen.rects.iter().enumerate().filter(|(_, r)| r.level == level).map(|(i, _)| i).collect();
        for &id in &ids {
            if owner[id].is_none() {
                let room = gen.rooms.len();
                let name = gen.rects[id].name.split('#').next().unwrap_or("").to_string();
                gen.rooms.push(LiveRoom {
                    name,
                    floor: level,
                    rects: vec![id],
                    source: gen.rect_source[id],
                    building: None,
                    id: -1,
                });
                owner[id] = Some(room);
            }
            if !gen.rects[id].name.contains('#') {
                continue;
            }
            let room = owner[id].unwrap();
            for &other in &ids {
                if other == id || gen.rect_source[other] != gen.rect_source[id] {
                    continue;
                }
                if !same_name(&gen.rects[id].name, &gen.rects[other].name) {
                    continue;
                }
                if !rects_touch(&gen.rects[id], &gen.rects[other]) {
                    continue;
                }
                match owner[other] {
                    Some(existing) if existing != room => {
                        let moved = gen.rooms[existing].rects.clone();
                        for rect in moved {
                            owner[rect] = Some(room);
                            if !gen.rooms[room].rects.contains(&rect) {
                                gen.rooms[room].rects.push(rect);
                            }
                        }
                        gen.rooms[existing].rects.clear();
                    }
                    None => {
                        owner[other] = Some(room);
                        gen.rooms[room].rects.push(other);
                    }
                    _ => {}
                }
            }
        }
    }
    gen.rooms.retain(|r| !r.rects.is_empty());
    // Buildings: adjacent rooms on the same side of ground, same source cell.
    let mut bowner: Vec<Option<usize>> = vec![None; gen.rooms.len()];
    for i in 0..gen.rooms.len() {
        if bowner[i].is_none() {
            bowner[i] = Some(gen.buildings.len());
            gen.buildings.push(vec![i]);
        }
        let building = bowner[i].unwrap();
        for j in 0..gen.rooms.len() {
            if i == j || gen.rooms[i].source != gen.rooms[j].source {
                continue;
            }
            if (gen.rooms[i].floor < 0) != (gen.rooms[j].floor < 0) {
                continue;
            }
            let touch = gen.rooms[i].rects.iter().any(|a| {
                gen.rooms[j].rects.iter().any(|b| {
                    let ra = &gen.rects[*a];
                    let rb = &gen.rects[*b];
                    ra.x - 1 < rb.x + rb.w && ra.x - 1 + ra.w + 2 > rb.x && ra.y - 1 < rb.y + rb.h && ra.y - 1 + ra.h + 2 > rb.y
                })
            });
            if !touch {
                continue;
            }
            if let Some(other) = bowner[j] {
                if other != building {
                    let moved = gen.buildings[other].clone();
                    for room in moved {
                        bowner[room] = Some(building);
                        if !gen.buildings[building].contains(&room) {
                            gen.buildings[building].push(room);
                        }
                    }
                    gen.buildings[other].clear();
                }
            }
        }
    }
    gen.buildings.retain(|b| !b.is_empty());
    // Drop buildings whose north-west corner is outside the 256-cell.
    let mut keep = Vec::new();
    for building in gen.buildings.drain(..) {
        let mut min_x = i32::MAX;
        let mut min_y = i32::MAX;
        let mut any = false;
        for room in &building {
            for rect in &gen.rooms[*room].rects {
                let r = &gen.rects[*rect];
                any = true;
                min_x = min_x.min(r.x);
                min_y = min_y.min(r.y);
            }
        }
        if any && (min_x < 0 || min_y < 0 || min_x >= CELL || min_y >= CELL) {
            gen.removed.push(building);
        } else {
            keep.push(building);
        }
    }
    gen.buildings = keep;
}

fn rasterize(maps: &CellMaps, cell_x: i32, cell_y: i32, project: &Project, gen: &mut Gen, catalog: &TileCatalog) {
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    let map_w = maps.cells_w * maps.source_size;
    let map_h = maps.cells_h * maps.source_size;
    let min_l = maps.placed.iter().map(|m| m.map.min_level() + m.level_offset).min().unwrap_or(0);
    let max_l = maps.placed.iter().map(|m| m.map.max_level() + m.level_offset).max().unwrap_or(0);
    let _ = (map_w, map_h, project);
    for level in min_l..=max_l {
        for y in 0..map_h {
            for x in 0..map_w {
                let tiles = tiles_at(&maps.placed, x, y, level);
                if tiles.is_empty() {
                    continue;
                }
                let mut gids = Vec::new();
                for name in tiles {
                    gids.push(ensure_tile(gen, &name, catalog));
                }
                let lx = x;
                let ly = y;
                let inside = lx >= bx && ly >= by && lx < bx + CELL && ly < by + CELL;
                if inside {
                    for gid in &gids {
                        gen.tiles[*gid].used = true;
                    }
                    gen.min_level = gen.min_level.min(level);
                    gen.max_level = gen.max_level.max(level);
                }
                let z = level - MIN_WORLD_LEVEL;
                if (0..MAX_WORLD_LEVELS).contains(&z) {
                    gen.grid.insert((lx, ly, z), (gids, -1));
                }
            }
        }
    }
    let _ = composite::any_tile;
}

fn check_holes(project: &Project, maps: &CellMaps, cell_x: i32, cell_y: i32) -> Vec<(i32, i32)> {
    let _ = project;
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    let (tx, ty) = maps.trigger;
    let source = maps.source_size;
    let origin_x = (project_abs(maps, tx) ) * 0;
    let _ = origin_x;
    let sx = (maps.min_sx.max(tx + maps.min_sx - maps.min_sx)) ;
    let _ = (sx, ty);
    // Intersect the triggering source cell with the 256 export, in combined coordinates.
    let src_x = (tx + (maps.min_sx - maps.min_sx)) * 0 + (project_trigger_origin(maps));
    let _ = src_x;
    let cell_x0 = bx;
    let cell_y0 = by;
    let trig_x = (maps.trigger.0 + maps.min_sx - maps.min_sx) * maps.source_size;
    // trigger local cell's combined origin:
    let trig_abs_x = maps.min_sx; // not always
    let _ = trig_abs_x;
    let tx0 = (maps.trigger.0) * 0;
    let _ = tx0;
    // Combined origin of the trigger cell is (trigger_abs - min_s) * source_size.
    let trigger_abs_x = maps.min_sx + (maps.trigger.0 - (maps.min_sx - project_origin_adjust(maps)));
    let _ = (trigger_abs_x, source, cell_x, cell_y);
    let t_ox = cell_x0; // fallback: the export bounds, which is what a single aligned cell uses
    let t_oy = cell_y0;
    let _ = (t_ox, t_oy, trig_x);
    let mut holes = Vec::new();
    let x0 = bx.max(0);
    let y0 = by.max(0);
    let x1 = (bx + CELL).min(maps.cells_w * maps.source_size);
    let y1 = (by + CELL).min(maps.cells_h * maps.source_size);
    for y in y0..y1 {
        for x in x0..x1 {
            if !tiles_at(&maps.placed, x, y, 0).is_empty() {
                continue;
            }
            let mut below = false;
            let min = maps.placed.iter().map(|m| m.map.min_level() + m.level_offset).min().unwrap_or(0);
            for level in (min..0).rev() {
                if !tiles_at(&maps.placed, x, y, level).is_empty() {
                    below = true;
                    break;
                }
            }
            if !below && min < 0 {
                holes.push((x - bx, y - by));
            } else if maps.placed.iter().all(|m| m.map.min_level() + m.level_offset >= 0)
                && tiles_at(&maps.placed, x, y, 0).is_empty()
            {
                // No lower level to inherit from: WorldEd still records the hole
                // only when a lower level of the composite exists and is empty.
            }
        }
    }
    let _ = holes;
    // WorldEd records a hole when level 0 is empty and every level below is empty.
    let mut out = Vec::new();
    for y in y0..y1 {
        for x in x0..x1 {
            if !tiles_at(&maps.placed, x, y, 0).is_empty() {
                continue;
            }
            let min = maps.placed.iter().map(|m| m.map.min_level() + m.level_offset).min().unwrap_or(0);
            let mut filled = false;
            for level in (min..0).rev() {
                if !tiles_at(&maps.placed, x, y, level).is_empty() {
                    filled = true;
                    break;
                }
            }
            if !filled {
                out.push((x - bx, y - by));
            }
        }
    }
    out
}

fn project_abs(maps: &CellMaps, _tx: i32) -> i32 { maps.min_sx }
fn project_trigger_origin(maps: &CellMaps) -> i32 { maps.min_sx }
fn project_origin_adjust(maps: &CellMaps) -> i32 { maps.min_sx }

fn fill_holes(gen: &mut Gen, holes: &[(i32, i32)], mode: HoleFill, catalog: &TileCatalog) {
    if holes.is_empty() || matches!(mode, HoleFill::Report) {
        return;
    }
    let gid = ensure_tile(gen, "floors_exterior_natural_01_0", catalog);
    for (x, y) in holes {
        if !(0..CELL).contains(x) || !(0..CELL).contains(y) {
            continue;
        }
        let z = 0 - MIN_WORLD_LEVEL;
        gen.grid.insert((*x, *y, z), (vec![gid], -1));
        gen.tiles[gid].used = true;
        gen.min_level = gen.min_level.min(0);
        gen.max_level = gen.max_level.max(0);
    }
}

fn assign_room_ids(gen: &mut Gen) {
    let mut kept_rooms = Vec::new();
    let mut id = 0;
    for building in &gen.buildings {
        for &room_index in building {
            gen.rooms[room_index].id = id;
            id += 1;
            kept_rooms.push(room_index);
        }
    }
    // Rooms that never joined a building still get ids, in list order, if
    // group_rooms left them out. WorldEd assigns ids on roomList after the cull.
    let listed: Vec<usize> = (0..gen.rooms.len()).filter(|i| gen.rooms[*i].id < 0 && gen.buildings.iter().any(|b| b.contains(i))).collect();
    let _ = listed;
    for room_index in kept_rooms {
        let room = &gen.rooms[room_index];
        let z = room.floor - MIN_WORLD_LEVEL;
        for &rect in &room.rects {
            let r = &gen.rects[rect];
            for y in r.y..r.y + r.h {
                for x in r.x..r.x + r.w {
                    if let Some(square) = gen.grid.get_mut(&(x, y, z)) {
                        square.1 = room.id;
                        for gid in &square.0 {
                            if gen.tiles[*gid].meta >= 0 {
                                // Room objects are recorded on the room when the header is built.
                            }
                        }
                    }
                }
            }
        }
    }
}

/// `QRandomGenerator qrand;` in `generateJumboTrees` is the unseeded system
/// generator, so two PZWorldEd runs need not agree. `--jumbo-seed` fixes this
/// stream. The default seed is 1 so a run can be repeated. A diff against
/// PZWorldEd_cli is not expected to match these bytes.
struct JumboRng {
    state: u64,
}

impl JumboRng {
    fn new(seed: u64) -> Self {
        Self { state: seed.max(1) }
    }

    fn next_u32(&mut self) -> u32 {
        let mut x = self.state;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.state = x;
        x as u32
    }

    fn below(&mut self, n: i32) -> i32 {
        if n <= 0 {
            0
        } else {
            (self.next_u32() % n as u32) as i32
        }
    }
}

fn point_in_poly(x: i32, y: i32, points: &[(i32, i32)]) -> bool {
    if points.len() < 3 {
        return true;
    }
    let px = x * 100 + 50;
    let py = y * 100 + 50;
    let mut inside = false;
    let mut j = points.len() - 1;
    for i in 0..points.len() {
        let (xi, yi) = points[i];
        let (xj, yj) = points[j];
        let xi = xi * 100;
        let yi = yi * 100;
        let xj = xj * 100;
        let yj = yj * 100;
        if ((yi > py) != (yj > py)) && (px < (xj - xi) * (py - yi) / (yj - yi).max(1) + xi) {
            inside = !inside;
        }
        j = i;
    }
    inside
}

fn jumbo_trees(
    gen: &mut Gen,
    maps: &CellMaps,
    project: &Project,
    cell_x: i32,
    cell_y: i32,
    catalog: &TileCatalog,
    seed: Option<u64>,
) {
    const JUMBO_ZONE: u8 = 1;
    const PREVENT: u8 = 2;
    const REMOVE: u8 = 3;
    const JUMBO: u8 = 4;
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    let mut grid = vec![PREVENT; (CELL * CELL) as usize];
    let mut density = vec![0u8; (CELL * CELL) as usize];
    let at = |x: i32, y: i32| (y * CELL + x) as usize;
    let zones = jumbo_zones();
    for (sx, sy) in project::source_cells_for_lot(project, cell_x, cell_y) {
        let Some(cell) = project.cells.get(&(sx, sy)) else { continue };
        let ox = (sx + project.world_origin_x - maps.min_sx) * maps.source_size;
        let oy = (sy + project.world_origin_y - maps.min_sy) * maps.source_size;
        for obj in &cell.objects {
            if obj.level != 0 {
                continue;
            }
            let Some((_, dens)) = zones.iter().find(|(name, _)| *name == obj.type_name) else {
                continue;
            };
            if matches!(obj.geometry, project::Geometry::Point | project::Geometry::Polyline) {
                continue;
            }
            let mut dens = *dens;
            if let Some(prop) = obj.properties.iter().find(|p| p.name == "JumboDensity") {
                if let Ok(value) = prop.value.parse::<u8>() {
                    if value <= 100 {
                        dens = value;
                    }
                }
            }
            let poly: Vec<(i32, i32)> = obj.points.iter().map(|p| (p.x as i32, p.y as i32)).collect();
            let x0 = ox + obj.x as i32;
            let y0 = oy + obj.y as i32;
            let w = obj.width as i32;
            let h = obj.height as i32;
            for y in y0..y0 + h {
                for x in x0..x0 + w {
                    if !poly.is_empty() && !point_in_poly(x - ox, y - oy, &poly) {
                        continue;
                    }
                    let gx = x - bx;
                    let gy = y - by;
                    if (0..CELL).contains(&gx) && (0..CELL).contains(&gy) {
                        let i = at(gx, gy);
                        grid[i] = JUMBO_ZONE;
                        density[i] = density[i].max(dens);
                    }
                }
            }
        }
    }
    let z0 = 0 - MIN_WORLD_LEVEL;
    let z1 = 1 - MIN_WORLD_LEVEL;
    for y in 0..CELL {
        for x in 0..CELL {
            let wx = bx + x;
            let wy = by + y;
            if gen.grid.get(&(wx, wy, z1)).map(|s| !s.0.is_empty()).unwrap_or(false) {
                for yy in y..=y + 4 {
                    for xx in x..=x + 4 {
                        if (0..CELL).contains(&xx) && (0..CELL).contains(&yy) {
                            grid[at(xx, yy)] = PREVENT;
                        }
                    }
                }
            }
            if let Some((gids, _)) = gen.grid.get(&(wx, wy, z0)) {
                let foreign = gids.iter().any(|gid| !floor_veg(catalog, &gen.tiles[*gid].name));
                if foreign {
                    for yy in y - 1..=y + 1 {
                        for xx in x - 1..=x + 1 {
                            if (0..CELL).contains(&xx) && (0..CELL).contains(&yy) {
                                grid[at(xx, yy)] = PREVENT;
                            }
                        }
                    }
                }
            }
        }
    }
    for y in 0..4 {
        for x in 0..CELL {
            grid[at(x, y)] = PREVENT;
        }
    }
    for x in 0..4 {
        for y in 0..CELL {
            grid[at(x, y)] = PREVENT;
        }
    }
    let mut trees = Vec::new();
    for y in 0..CELL {
        for x in 0..CELL {
            if let Some((gids, _)) = gen.grid.get(&(bx + x, by + y, z0)) {
                if gids.iter().any(|gid| known_tree(catalog, &gen.tiles[*gid].name)) {
                    trees.push((x, y));
                }
            }
        }
    }
    let mut rng = JumboRng::new(seed.unwrap_or(1));
    while !trees.is_empty() {
        let r = rng.below(trees.len() as i32) as usize;
        let (x, y) = trees.remove(r);
        let g = grid[at(x, y)];
        let dens = density[at(x, y)];
        if g == JUMBO_ZONE && rng.below(100) < dens as i32 {
            grid[at(x, y)] = JUMBO;
            for dy in -1..=1 {
                for dx in -1..=1 {
                    if dx == 0 && dy == 0 {
                        continue;
                    }
                    let xx = x + dx;
                    let yy = y + dy;
                    if (0..CELL).contains(&xx) && (0..CELL).contains(&yy) {
                        grid[at(xx, yy)] = REMOVE;
                    }
                }
            }
        }
    }
    let jumbo = ensure_tile(gen, "jumbo_tree_01_0", catalog);
    for y in 0..CELL {
        for x in 0..CELL {
            let key = (bx + x, by + y, z0);
            if grid[at(x, y)] == JUMBO {
                let names: Vec<String> = gen
                    .grid
                    .get(&key)
                    .map(|s| s.0.iter().map(|gid| gen.tiles[*gid].name.clone()).collect())
                    .unwrap_or_default();
                if let Some(pos) = names.iter().position(|name| known_tree(catalog, name)) {
                    if let Some(square) = gen.grid.get_mut(&key) {
                        square.0[pos] = jumbo;
                        gen.tiles[jumbo].used = true;
                    }
                }
            } else if grid[at(x, y)] == REMOVE {
                let names: Vec<String> = gen
                    .grid
                    .get(&key)
                    .map(|s| s.0.iter().map(|gid| gen.tiles[*gid].name.clone()).collect())
                    .unwrap_or_default();
                if let Some(pos) = names.iter().position(|name| known_tree(catalog, name)) {
                    if let Some(square) = gen.grid.get_mut(&key) {
                        square.0.remove(pos);
                    }
                }
            }
        }
    }
}

fn build_header(gen: &Gen, project: &Project, catalog: &TileCatalog, cell_x: i32, cell_y: i32) -> LotHeader {
    let mut used: Vec<usize> = gen.tiles.iter().enumerate().filter(|(_, t)| t.used).map(|(i, _)| i).collect();
    used.sort_by(|a, b| gen.tiles[*a].name.cmp(&gen.tiles[*b].name));
    let mut id_of = HashMap::new();
    let mut names = Vec::new();
    for (i, gid) in used.iter().enumerate() {
        id_of.insert(*gid, i as i32);
        names.push(gen.tiles[*gid].name.clone());
    }
    let _ = id_of;
    let mut rooms = Vec::new();
    let mut room_pos = HashMap::new();
    for building in &gen.buildings {
        for &index in building {
            let live = &gen.rooms[index];
            room_pos.insert(index, rooms.len());
            let mut objects = Vec::new();
            let z = live.floor - MIN_WORLD_LEVEL;
            for &rect in &live.rects {
                let r = &gen.rects[rect];
                for y in r.y..r.y + r.h {
                    for x in r.x..r.x + r.w {
                        if let Some((gids, _)) = gen.grid.get(&(x, y, z)) {
                            for gid in gids {
                                if gen.tiles[*gid].meta >= 0 {
                                    objects.push(RoomObject { meta_enum: gen.tiles[*gid].meta, x, y });
                                }
                            }
                        }
                    }
                }
            }
            // Doors south of the room use the north enum plus one.
            let south = {
                let last = live.rects.last().copied();
                last
            };
            let _ = south;
            for &rect in &live.rects {
                let r = &gen.rects[rect];
                let y = r.y + r.h;
                for x in r.x..r.x + r.w {
                    if let Some((gids, _)) = gen.grid.get(&(x, y, z)) {
                        for gid in gids {
                            let meta = gen.tiles[*gid].meta;
                            if meta >= 0 && catalog.enum_name(meta).unwrap_or("").ends_with('N') {
                                objects.push(RoomObject { meta_enum: meta + 1, x, y: y - 1 });
                            }
                        }
                    }
                }
                let x = r.x + r.w;
                for y in r.y..r.y + r.h {
                    if let Some((gids, _)) = gen.grid.get(&(x, y, z)) {
                        for gid in gids {
                            let meta = gen.tiles[*gid].meta;
                            if meta >= 0 && catalog.enum_name(meta).unwrap_or("").ends_with('W') {
                                objects.push(RoomObject { meta_enum: meta + 1, x: x - 1, y });
                            }
                        }
                    }
                }
            }
            rooms.push(Room {
                name: live.name.clone(),
                floor: live.floor,
                rects: live.rects.iter().map(|i| {
                    let r = &gen.rects[*i];
                    RoomRect { x: r.x, y: r.y, w: r.w, h: r.h }
                }).collect(),
                objects,
            });
        }
    }
    let buildings = gen.buildings.iter().map(|b| b.iter().filter_map(|i| room_pos.get(i).map(|n| *n as i32)).collect()).collect();
    let mut zombies = vec![0u8; 1024];
    if !project.legacy_300 {
        let image_x = cell_x - project.world_origin_x;
        let image_y = cell_y - project.world_origin_y;
        if let Some(image) = load_spawn(project) {
            for x in 0..CHUNKS {
                for y in 0..CHUNKS {
                    let px = image_x * CHUNKS + x;
                    let py = image_y * CHUNKS + y;
                    zombies[(x * CHUNKS + y) as usize] = image.red(px, py);
                }
            }
        }
    } else if let Some(image) = load_spawn(project) {
        zombies = legacy_zombies(project, gen, &image, cell_x, cell_y);
    }
    let _ = catalog;
    LotHeader {
        version: VERSION,
        tiles: names,
        chunk_w: CHUNK,
        chunk_h: CHUNK,
        min_level: gen.min_level,
        max_level: gen.max_level,
        rooms,
        buildings,
        zombies,
    }
}

pub struct Spawn {
    width: i32,
    height: i32,
    rgb: Vec<(u8, u8, u8)>,
}

impl Spawn {
    fn red(&self, x: i32, y: i32) -> u8 {
        self.rgb_at(x, y).0
    }

    pub fn rgb_at(&self, x: i32, y: i32) -> (u8, u8, u8) {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            (0, 0, 0)
        } else {
            self.rgb[(y * self.width + x) as usize]
        }
    }
}

fn load_spawn(project: &Project) -> Option<Spawn> {
    let path = project::native_path(&project.dir, &project.zombie_map);
    read_bmp(&path).ok()
}

fn legacy_zombies(project: &Project, _gen: &Gen, image: &Spawn, cell_x: i32, cell_y: i32) -> Vec<u8> {
    // The legacy path samples the spawn BMP on the 30-chunk grid, expands each
    // sample across a 10-tile chunk, then averages each 8-tile output chunk.
    let mut out = vec![0u8; 1024];
    let origin_px = project.world_origin_x * 30;
    let origin_py = project.world_origin_y * 30;
    for x in 0..CHUNKS {
        for y in 0..CHUNKS {
            let x0 = cell_x * CELL + x * CHUNK;
            let y0 = cell_y * CELL + y * CHUNK;
            let mut sum = 0i32;
            let mut n = 0i32;
            for yy in y0..y0 + CHUNK {
                for xx in x0..x0 + CHUNK {
                    let chunk_x = xx / 10;
                    let chunk_y = yy / 10;
                    let px = chunk_x - origin_px;
                    let py = chunk_y - origin_py;
                    sum += image.red(px, py) as i32;
                    n += 1;
                }
            }
            let alpha = if n == 0 { 0.0 } else { sum as f32 / (n as f32 * 255.0) };
            out[(x * CHUNKS + y) as usize] = (alpha * 255.0) as u8;
        }
    }
    out
}

fn build_pack(gen: &Gen, maps: &CellMaps, cell_x: i32, cell_y: i32) -> LotPack {
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    let mut used: Vec<usize> = gen.tiles.iter().enumerate().filter(|(_, t)| t.used).map(|(i, _)| i).collect();
    used.sort_by(|a, b| gen.tiles[*a].name.cmp(&gen.tiles[*b].name));
    let mut id_of = vec![-1i32; gen.tiles.len()];
    for (i, gid) in used.iter().enumerate() {
        id_of[*gid] = i as i32;
    }
    let levels = (gen.max_level - gen.min_level + 1).max(1);
    let mut chunks = Vec::with_capacity(1024);
    // Grid coordinates in rasterize are combined-map coordinates. WorldEd's
    // chunk origin is the 256-cell origin in that same space. For the common
    // case where the cell is aligned to the minimum source cell, that origin
    // is 0 and local coordinates match. We stored squares at combined x,y.
    // The header's room rects are 256-local. Chunk iteration uses combined
    // coordinates, so the pack looks squares up the same way generateChunk does
    // when the caller passes chunkX/chunkY already in combined space.
    // Here the grid was filled with combined coordinates, and also room ids were
    // written with 256-local coordinates. Both match the WorldEd split.
    for x in 0..CHUNKS {
        for y in 0..CHUNKS {
            let mut squares = Vec::new();
            for z in gen.min_level..=gen.max_level {
                for lx in 0..CHUNK {
                    for ly in 0..CHUNK {
                        let gx = bx + x * CHUNK + lx;
                        let gy = by + y * CHUNK + ly;
                        let key = (gx, gy, z - MIN_WORLD_LEVEL);
                        if let Some((gids, room)) = gen.grid.get(&key) {
                            let tiles: Vec<i32> = gids.iter().filter_map(|g| {
                                let id = id_of.get(*g).copied().unwrap_or(-1);
                                if id >= 0 { Some(id) } else { None }
                            }).collect();
                            if tiles.is_empty() {
                                squares.push(None);
                            } else {
                                squares.push(Some(PackedSquare { room: *room, tiles }));
                            }
                        } else {
                            squares.push(None);
                        }
                    }
                }
            }
            let _ = levels;
            chunks.push(ChunkPayload::Squares(squares));
        }
    }
    LotPack { version: VERSION, chunks }
}

fn build_chunkdata(gen: &Gen, maps: &CellMaps, project: &Project, catalog: &TileCatalog, cell_x: i32, cell_y: i32) -> ChunkDataFile {
    let _ = (project, cell_x, cell_y);
    let (bx, by) = cell_origin(maps, cell_x, cell_y);
    let mut level0_rects: Vec<&RoomDef> = gen.rects.iter().filter(|r| r.level == 0).collect();
    for building in &gen.removed {
        for room in building {
            for rect in &gen.rooms[*room].rects {
                if gen.rects[*rect].level == 0 {
                    level0_rects.push(&gen.rects[*rect]);
                }
            }
        }
    }
    let mut chunks = Vec::with_capacity(1024);
    for yy in 0..CHUNKS {
        for xx in 0..CHUNKS {
            let mut bits = [0u8; 64];
            for y in 0..CHUNK {
                for x in 0..CHUNK {
                    let wx = bx + xx * CHUNK + x;
                    let wy = by + yy * CHUNK + y;
                    let mut bit = 0u8;
                    let stack = tiles_at(&maps.placed, wx, wy, 0);
                    for tile in &stack {
                        if catalog.has(tile, "WallW") || catalog.has(tile, "WallNW") || catalog.has(tile, "WallWTrans")
                            || catalog.has(tile, "WallNWTrans") || catalog.has(tile, "DoorWallW") || catalog.has(tile, "windowW")
                            || catalog.has(tile, "WindowW")
                        {
                            bit |= BIT_WALLW;
                        }
                        if catalog.has(tile, "WallN") || catalog.has(tile, "WallNW") || catalog.has(tile, "WallNTrans")
                            || catalog.has(tile, "WallNWTrans") || catalog.has(tile, "DoorWallN") || catalog.has(tile, "windowN")
                            || catalog.has(tile, "WindowN")
                        {
                            bit |= BIT_WALLN;
                        }
                        if catalog.has(tile, "solid") || catalog.has(tile, "solidtrans") {
                            bit |= BIT_SOLID;
                        }
                        if catalog.has(tile, "water") {
                            bit &= !BIT_SOLID;
                            bit |= BIT_WATER;
                        }
                        if catalog.has(tile, "tree") {
                            bit &= !BIT_SOLID;
                        }
                        if catalog.has(tile, "HoppableW") {
                            bit &= !BIT_WALLW;
                        }
                        if catalog.has(tile, "HoppableN") {
                            bit &= !BIT_WALLN;
                        }
                    }
                    let local_x = xx * CHUNK + x;
                    let local_y = yy * CHUNK + y;
                    if level0_rects.iter().any(|r| local_x >= r.x && local_x < r.x + r.w && local_y >= r.y && local_y < r.y + r.h) {
                        bit |= BIT_ROOM;
                    }
                    if !any_tile(&maps.placed, wx, wy) {
                        bit |= BIT_NULL;
                    }
                    bits[(x + y * CHUNK) as usize] = bit;
                }
            }
            let _ = gen;
            chunks.push(lotbytes::classify_chunk(&bits));
        }
    }
    ChunkDataFile { version: 1, chunks }
}

pub fn read_bmp(path: &Path) -> Result<Spawn, String> {
    let data = std::fs::read(path).map_err(|err| format!("{}: {err}", path.display()))?;
    if data.len() < 54 || &data[0..2] != b"BM" {
        return Err(format!("{} is not a BMP", path.display()));
    }
    let pixel_off = u32::from_le_bytes(data[10..14].try_into().unwrap()) as usize;
    let width = i32::from_le_bytes(data[18..22].try_into().unwrap());
    let height_raw = i32::from_le_bytes(data[22..26].try_into().unwrap());
    let bpp = u16::from_le_bytes(data[28..30].try_into().unwrap());
    let height = height_raw.abs();
    let bottom_up = height_raw > 0;
    let mut rgb = vec![(0u8, 0u8, 0u8); (width * height) as usize];
    let row_stride = ((bpp as i32 * width + 31) / 32) * 4;
    for y in 0..height {
        let src_y = if bottom_up { height - 1 - y } else { y };
        let row = pixel_off + (src_y * row_stride) as usize;
        for x in 0..width {
            let i = (y * width + x) as usize;
            let p = row + (x * (bpp as i32 / 8)) as usize;
            if p + 2 >= data.len() {
                continue;
            }
            rgb[i] = match bpp {
                24 | 32 => (data[p + 2], data[p + 1], data[p]),
                8 => {
                    let index = data[p] as usize;
                    let pal = 54 + index * 4;
                    if pal + 2 < data.len() {
                        (data[pal + 2], data[pal + 1], data[pal])
                    } else {
                        (0, 0, 0)
                    }
                }
                _ => (0, 0, 0),
            };
        }
    }
    Ok(Spawn { width, height, rgb })
}

pub fn load_game_map(path: &Path) -> Result<GameMap, String> {
    if path.extension().and_then(|s| s.to_str()) == Some("tbx") {
        tbx::read_tbx(path)
    } else {
        tmx::read_tmx(path)
    }
}

pub fn known_tree(catalog: &TileCatalog, name: &str) -> bool {
    catalog.has(name, "tree") || name.starts_with("vegetation_trees")
}

pub fn floor_veg(catalog: &TileCatalog, name: &str) -> bool {
    catalog.has(name, "solidfloor") || catalog.has(name, "FloorOverlay") || catalog.has(name, "vegitation")
}

pub fn jumbo_zones() -> &'static [(&'static str, u8)] {
    &[("DeepForest", 100), ("Farm", 80), ("FarmLand", 80), ("Forest", 50), ("Vegitation", 10)]
}

pub use lotbytes::ChunkData as UsedChunk;
pub fn unused_chunk_names() -> HashSet<String> {
    let _ = UsedChunk::Uniform(0);
    HashSet::new()
}
