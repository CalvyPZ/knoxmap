// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// TMX is read here. libtiled is not translated. The GPL part is the stacking
// and lot export that happen after the map is loaded.

//! Read a TMX map into layers and RoomDefs.

use std::path::Path;

use crate::lotbytes;
use crate::model::{GameMap, Layer, RoomDef};
use crate::xmltree::{self, Elem};

struct Sheet {
    first: u32,
    count: u32,
    name: String,
}

pub fn read_tmx(path: &Path) -> Result<GameMap, String> {
    let text = std::fs::read_to_string(path).map_err(|err| format!("{}: {err}", path.display()))?;
    let root = xmltree::parse(&text)?;
    if root.name != "map" {
        return Err(format!("{} is not a TMX map", path.display()));
    }
    let width = root.attr("width").parse::<i32>().unwrap_or(0);
    let height = root.attr("height").parse::<i32>().unwrap_or(0);
    let isometric = root.attr("orientation") == "isometric";
    let mut sheets = Vec::new();
    for child in root.children_named("tileset") {
        let first = child.attr("firstgid").parse::<u32>().unwrap_or(1);
        let name = tileset_name(path, child);
        let count = tileset_count(child);
        sheets.push(Sheet { first, count, name });
    }
    sheets.sort_by_key(|s| s.first);
    let mut layers = Vec::new();
    let mut rooms = Vec::new();
    for child in &root.children {
        if child.name == "layer" {
            let level = child.attr("level").parse().unwrap_or(0);
            let name = child.attr("name").to_string();
            let lw = child.attr("width").parse().unwrap_or(width);
            let lh = child.attr("height").parse().unwrap_or(height);
            let mut layer = Layer::new(&name, level, lw, lh);
            if let Some(data) = child.child("data") {
                let gids = decode_data(data)?;
                for (i, gid) in gids.into_iter().enumerate() {
                    if gid == 0 {
                        continue;
                    }
                    if let Some(tile) = gid_name(&sheets, gid & 0x1FFF_FFFF) {
                        let x = (i as i32) % lw;
                        let y = (i as i32) / lw;
                        layer.set(x, y, Some(tile));
                    }
                }
            }
            layers.push(layer);
        } else if child.name == "objectgroup" && child.attr("name").contains("RoomDefs") {
            let level = child.attr("level").parse().unwrap_or(0) ;
            for object in child.children_named("object") {
                let name = object.attr("name");
                if name.is_empty() {
                    continue;
                }
                let x = object.attr("x").parse::<f64>().unwrap_or(0.0).floor() as i32;
                let y = object.attr("y").parse::<f64>().unwrap_or(0.0).floor() as i32;
                let w = object.attr("width").parse::<f64>().unwrap_or(0.0);
                let h = object.attr("height").parse::<f64>().unwrap_or(0.0);
                let w = (x as f64 + w).ceil() as i32 - x;
                let h = (y as f64 + h).ceil() as i32 - y;
                if w == 0 || h == 0 {
                    continue;
                }
                rooms.push(RoomDef {
                    name: name.to_string(),
                    x,
                    y,
                    w,
                    h,
                    level,
                });
            }
        }
    }
    Ok(GameMap {
        isometric,
        width,
        height,
        layers,
        rooms,
    })
}

fn tileset_name(map_path: &Path, elem: &Elem) -> String {
    if !elem.attr("name").is_empty() {
        return elem.attr("name").to_string();
    }
    if let Some(image) = elem.child("image") {
        if let Some(stem) = Path::new(image.attr("source")).file_stem() {
            return stem.to_string_lossy().into_owned();
        }
    }
    let source = elem.attr("source");
    if !source.is_empty() {
        let tsx = map_path.parent().unwrap_or(Path::new(".")).join(source);
        if let Ok(text) = std::fs::read_to_string(&tsx) {
            if let Ok(root) = xmltree::parse(&text) {
                if !root.attr("name").is_empty() {
                    return root.attr("name").to_string();
                }
                if let Some(image) = root.child("image") {
                    if let Some(stem) = Path::new(image.attr("source")).file_stem() {
                        return stem.to_string_lossy().into_owned();
                    }
                }
            }
        }
        if let Some(stem) = Path::new(source).file_stem() {
            return stem.to_string_lossy().into_owned();
        }
    }
    "tileset".into()
}

fn tileset_count(elem: &Elem) -> u32 {
    if let Ok(n) = elem.attr("tilecount").parse::<u32>() {
        return n;
    }
    let tw = elem.attr("tilewidth").parse::<i32>().unwrap_or(64).max(1);
    let th = elem.attr("tileheight").parse::<i32>().unwrap_or(128).max(1);
    if let Some(image) = elem.child("image") {
        let w = image.attr("width").parse::<i32>().unwrap_or(0);
        let h = image.attr("height").parse::<i32>().unwrap_or(0);
        if w > 0 && h > 0 {
            return ((w / tw) * (h / th)) as u32;
        }
    }
    1
}

fn gid_name(sheets: &[Sheet], gid: u32) -> Option<String> {
    let sheet = sheets.iter().rev().find(|s| gid >= s.first)?;
    let index = gid - sheet.first;
    if sheet.count > 0 && index >= sheet.count {
        return None;
    }
    Some(format!("{}_{index}", sheet.name))
}

fn decode_data(data: &Elem) -> Result<Vec<u32>, String> {
    let encoding = data.attr("encoding");
    let text = data.text.trim();
    if encoding == "csv" {
        return Ok(text
            .split(|c: char| c == ',' || c.is_whitespace())
            .filter(|s| !s.is_empty())
            .map(|s| s.parse().unwrap_or(0))
            .collect());
    }
    if encoding != "base64" {
        let mut gids = Vec::new();
        for tile in data.children_named("tile") {
            gids.push(tile.attr("gid").parse().unwrap_or(0));
        }
        return Ok(gids);
    }
    let raw = lotbytes::decode_base64(text)?;
    let bytes = match data.attr("compression") {
        "zlib" => lotbytes::inflate_zlib(&raw)?,
        "gzip" => lotbytes::inflate_gzip(&raw)?,
        _ => raw,
    };
    let mut gids = Vec::with_capacity(bytes.len() / 4);
    for chunk in bytes.chunks_exact(4) {
        gids.push(u32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Ok(gids)
}
