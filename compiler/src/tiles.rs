// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// TileMetaInfoMgr names and grid size, plus the .tiles property file
// IsoGridSquare256 reads for solid / wall / water bits.

//! Tileset names, sheet geometry, and tile-definition properties.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use crate::rules;

#[derive(Clone, Debug)]
pub struct TilesetInfo {
    pub name: String,
    pub columns: i32,
    pub rows: i32,
    pub file: String,
    /// "column,row" -> meta-enum name.
    pub enums: HashMap<String, String>,
}

#[derive(Clone, Debug, Default)]
pub struct TileCatalog {
    pub tilesets: Vec<TilesetInfo>,
    pub enum_values: HashMap<String, i32>,
    /// tileset name -> tile id -> property names that are present.
    pub properties: HashMap<String, HashMap<i32, Vec<String>>>,
}

impl TileCatalog {
    pub fn meta_enum(&self, tileset: &str, index: i32) -> i32 {
        let Some(info) = self.tilesets.iter().find(|t| t.name == tileset) else {
            return -1;
        };
        if info.columns <= 0 {
            return -1;
        }
        let key = format!("{},{}", index % info.columns, index / info.columns);
        info.enums
            .get(&key)
            .and_then(|name| self.enum_values.get(name))
            .copied()
            .unwrap_or(-1)
    }

    pub fn enum_name(&self, value: i32) -> Option<&str> {
        self.enum_values
            .iter()
            .find(|(_, v)| **v == value)
            .map(|(name, _)| name.as_str())
    }

    pub fn has(&self, tile: &str, property: &str) -> bool {
        let Some((name, index)) = split_tile(tile) else {
            return false;
        };
        self.properties
            .get(&name)
            .and_then(|tiles| tiles.get(&index))
            .map(|props| props.iter().any(|p| p == property))
            .unwrap_or(false)
    }
}

pub fn split_tile(tile: &str) -> Option<(String, i32)> {
    let (name, index) = tile.rsplit_once('_')?;
    let index = index.trim_start_matches('0');
    let index = if index.is_empty() { 0 } else { index.parse().ok()? };
    Some((name.to_string(), index))
}

pub fn load_tilesets_txt(path: &Path) -> Result<TileCatalog, String> {
    let text = fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let blocks = parse_blocks(&text)?;
    let mut catalog = TileCatalog::default();
    for block in &blocks {
        if block.name == "meta-enums" {
            for (name, values) in &block.values {
                let value = values.first().and_then(|s| s.parse().ok()).unwrap_or(-1);
                catalog.enum_values.insert(name.clone(), value);
            }
        }
    }
    for block in &blocks {
        if block.name != "tileset" {
            continue;
        }
        let file = block
            .values
            .iter()
            .find(|(k, _)| k == "file")
            .and_then(|(_, v)| v.first())
            .cloned()
            .unwrap_or_default();
        let name = Path::new(&file)
            .file_stem()
            .map(|s| s.to_string_lossy().into_owned())
            .unwrap_or(file.clone());
        let size = block
            .values
            .iter()
            .find(|(k, _)| k == "size")
            .and_then(|(_, v)| v.first())
            .cloned()
            .unwrap_or_else(|| "1,1".into());
        let mut parts = size.split(',');
        let columns = parts.next().and_then(|s| s.trim().parse().ok()).unwrap_or(1);
        let rows = parts.next().and_then(|s| s.trim().parse().ok()).unwrap_or(1);
        let mut enums = HashMap::new();
        let mut coord = String::new();
        for nested in &block.nested {
            if nested.name != "tile" {
                continue;
            }
            for (key, values) in &nested.values {
                if key == "xy" {
                    coord = values.first().cloned().unwrap_or_default();
                } else if key == "meta-enum" {
                    if let Some(name) = values.first() {
                        enums.insert(coord.clone(), name.clone());
                    }
                }
            }
        }
        catalog.tilesets.push(TilesetInfo {
            name,
            columns,
            rows,
            file,
            enums,
        });
    }
    Ok(catalog)
}

struct TxtBlock {
    name: String,
    values: Vec<(String, Vec<String>)>,
    nested: Vec<TxtBlock>,
}

fn parse_blocks(text: &str) -> Result<Vec<TxtBlock>, String> {
    // Reuse the Rules parser's shape, including one level of nested tile blocks.
    let parsed = rules_blocks(text)?;
    Ok(parsed)
}

fn rules_blocks(text: &str) -> Result<Vec<TxtBlock>, String> {
    // Local copy of the simple-file reader that keeps nested blocks.
    let mut roots = Vec::new();
    let mut stack: Vec<TxtBlock> = Vec::new();
    let mut pending = String::new();
    for raw in text.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with("//") {
            continue;
        }
        if line == "{" {
            stack.push(TxtBlock {
                name: std::mem::take(&mut pending),
                values: Vec::new(),
                nested: Vec::new(),
            });
            continue;
        }
        if line == "}" {
            let block = stack.pop().ok_or_else(|| "extra } in Tilesets.txt".to_string())?;
            if let Some(parent) = stack.last_mut() {
                parent.nested.push(block);
            } else {
                roots.push(block);
            }
            continue;
        }
        if let Some(eq) = line.find('=') {
            let name = line[..eq].trim().to_string();
            let value = line[eq + 1..]
                .trim()
                .trim_start_matches('[')
                .trim_end_matches(']')
                .trim()
                .to_string();
            let values = if value.is_empty() {
                Vec::new()
            } else {
                value.split_whitespace().map(str::to_string).collect()
            };
            if let Some(block) = stack.last_mut() {
                block.values.push((name, values));
            }
            continue;
        }
        if stack.is_empty() || pending.is_empty() {
            if !pending.is_empty() {
                pending.push(' ');
            }
            pending.push_str(line);
        } else if stack.last().map(|b| b.name.is_empty()).unwrap_or(false) {
            // A name line inside a block starts a nested block; the "{" follows.
            pending = line.to_string();
        } else {
            pending = line.to_string();
        }
    }
    let _ = rules::rgb_string((0, 0, 0));
    Ok(roots)
}

const RUNTIME_ORDER: &[&str] = &[
    "newtiledefinitions.tiles",
    "tiledefinitions_erosion.tiles",
    "tiledefinitions_overlays.tiles",
    "tiledefinitions_b42chunkcaching.tiles",
    "tiledefinitions_noiseworks.patch.tiles",
    "jumbo_trees_big.tiles",
    "jumbo_trees.tiles",
];

pub fn load_tile_defs(dir: &Path, catalog: &mut TileCatalog) -> Result<(), String> {
    if !dir.is_dir() {
        return Ok(());
    }
    let mut files: Vec<PathBuf> = fs::read_dir(dir)
        .map_err(|err| err.to_string())?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.extension().and_then(|s| s.to_str()) == Some("tiles")
                && !p
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("")
                    .ends_with("_4.tiles")
        })
        .collect();
    files.sort();
    let mut ordered = Vec::new();
    for name in RUNTIME_ORDER {
        if let Some(pos) = files.iter().position(|p| {
            p.file_name()
                .and_then(|s| s.to_str())
                .map(|s| s.eq_ignore_ascii_case(name))
                .unwrap_or(false)
        }) {
            ordered.push(files.remove(pos));
        }
    }
    ordered.append(&mut files);
    let mut bases: Vec<HashMap<String, HashMap<i32, Vec<String>>>> = Vec::new();
    for path in ordered {
        let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
        let loaded = read_tiles_file(&path)?;
        if name.to_ascii_lowercase().ends_with(".patch.tiles") {
            for (ts, tiles) in loaded {
                for base in &mut bases {
                    if let Some(existing) = base.get_mut(&ts) {
                        for (id, props) in tiles.iter() {
                            if let Some(slot) = existing.get_mut(id) {
                                for prop in props {
                                    if !slot.contains(prop) {
                                        slot.push(prop.clone());
                                    }
                                }
                            }
                        }
                    }
                }
            }
            continue;
        }
        bases.push(loaded);
    }
    for base in bases {
        for (name, tiles) in base {
            catalog.properties.entry(name).or_default().extend(tiles);
        }
    }
    Ok(())
}

fn read_tiles_file(path: &Path) -> Result<HashMap<String, HashMap<i32, Vec<String>>>, String> {
    let data = fs::read(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let mut pos = 0usize;
    let mut version = 0i32;
    if data.len() >= 4 && &data[..4] == b"tdef" {
        pos = 4;
        version = read_i32(&data, &mut pos)?;
    }
    let nsets = read_i32(&data, &mut pos)?;
    let mut out = HashMap::new();
    for _ in 0..nsets {
        let name = read_nl(&data, &mut pos)?;
        let _image = read_nl(&data, &mut pos)?;
        let _columns = read_i32(&data, &mut pos)?;
        let _rows = read_i32(&data, &mut pos)?;
        if version > 0 {
            let _id = read_i32(&data, &mut pos)?;
        }
        let tile_count = read_i32(&data, &mut pos)?;
        let mut tiles = HashMap::new();
        for j in 0..tile_count {
            let nprops = read_i32(&data, &mut pos)?;
            let mut props = Vec::new();
            for _ in 0..nprops {
                props.push(read_nl(&data, &mut pos)?);
                let _value = read_nl(&data, &mut pos)?;
            }
            tiles.insert(j, props);
        }
        out.insert(name, tiles);
    }
    Ok(out)
}

fn read_i32(data: &[u8], pos: &mut usize) -> Result<i32, String> {
    if *pos + 4 > data.len() {
        return Err("truncated .tiles file".into());
    }
    let v = i32::from_le_bytes(data[*pos..*pos + 4].try_into().unwrap());
    *pos += 4;
    Ok(v)
}

fn read_nl(data: &[u8], pos: &mut usize) -> Result<String, String> {
    let start = *pos;
    let Some(end) = data[*pos..].iter().position(|b| *b == b'\n') else {
        return Err("truncated string in .tiles file".into());
    };
    *pos = start + end + 1;
    Ok(String::from_utf8_lossy(&data[start..start + end]).into_owned())
}
