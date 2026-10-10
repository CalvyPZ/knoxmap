// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.

//! A loaded map after TMX or TBX has been turned into the layers WorldEd stacks.

#[derive(Clone, Debug)]
pub struct Layer {
    pub name: String,
    pub level: i32,
    pub width: i32,
    pub height: i32,
    /// Row-major. Empty cells are None. Names are `tileset_index`, unpadded.
    pub cells: Vec<Option<String>>,
    pub above_lot: bool,
}

impl Layer {
    pub fn new(name: &str, level: i32, width: i32, height: i32) -> Self {
        Self {
            above_lot: name.contains("AboveLot"),
            name: name.to_string(),
            level,
            width,
            height,
            cells: vec![None; (width * height) as usize],
        }
    }

    pub fn get(&self, x: i32, y: i32) -> Option<&str> {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return None;
        }
        self.cells[(y * self.width + x) as usize].as_deref()
    }

    pub fn set(&mut self, x: i32, y: i32, tile: Option<String>) {
        if x < 0 || y < 0 || x >= self.width || y >= self.height {
            return;
        }
        self.cells[(y * self.width + x) as usize] = tile;
    }
}

#[derive(Clone, Debug)]
pub struct RoomDef {
    pub name: String,
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
    pub level: i32,
}

#[derive(Clone, Debug)]
pub struct GameMap {
    /// `Map::Isometric`, which nudges rooms and the lot grid by 3 tiles a level.
    /// Level-isometric maps, the ones BMP to TMX writes, leave this false.
    pub isometric: bool,
    pub width: i32,
    pub height: i32,
    pub layers: Vec<Layer>,
    pub rooms: Vec<RoomDef>,
}

impl GameMap {
    pub fn min_level(&self) -> i32 {
        self.layers.iter().map(|l| l.level).min().unwrap_or(0)
    }

    pub fn max_level(&self) -> i32 {
        self.layers.iter().map(|l| l.level).max().unwrap_or(0)
    }
}

/// One map inside the combined cell, in the order `CombinedCellMaps::checkLoading` adds them.
#[derive(Clone, Debug)]
pub struct PlacedMap {
    pub map: GameMap,
    pub origin_x: i32,
    pub origin_y: i32,
    pub level_offset: i32,
    pub cell_map: bool,
}
