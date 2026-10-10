// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// CompositeLayerGroup::orderedCellsAt2 and the root merge in mapcomposite.cpp:
// cell maps, then buildings (floor clears, same layer name replaces), then
// AboveLot. Basement Keep* flags are applied when a sub-map is marked basement.

//! Stack the maps that contribute to one square.

use crate::model::PlacedMap;

#[derive(Clone, Debug)]
struct Hit {
    layer: String,
    tile: String,
    above: bool,
}

/// Tiles on one square at one world level, bottom to top, in WorldEd's merge order.
pub fn tiles_at(maps: &[PlacedMap], x: i32, y: i32, level: i32) -> Vec<String> {
    let mut kept: Vec<Hit> = Vec::new();
    let mut above: Vec<Hit> = Vec::new();
    for sub in maps.iter().filter(|m| m.cell_map) {
        let hits = hits(sub, x, y, level);
        if hits.first().map(|h| h.layer == "Floor").unwrap_or(false) {
            kept.clear();
        }
        for hit in hits {
            if hit.above {
                above.push(hit);
            } else {
                kept.push(hit);
            }
        }
    }
    for sub in maps.iter().filter(|m| !m.cell_map) {
        let hits = hits(sub, x, y, level);
        if hits.is_empty() {
            continue;
        }
        if hits.iter().any(|h| h.layer == "Floor") {
            kept.retain(|h| h.above);
        }
        let names: Vec<String> = hits.iter().map(|h| h.layer.clone()).collect();
        kept.retain(|h| h.above || !names.iter().any(|n| n == &h.layer));
        for hit in hits {
            if hit.above {
                above.push(hit);
            } else {
                kept.push(hit);
            }
        }
    }
    kept.extend(above);
    kept.into_iter().map(|h| h.tile).collect()
}

fn hits(sub: &PlacedMap, x: i32, y: i32, level: i32) -> Vec<Hit> {
    let local_x = x - sub.origin_x;
    let local_y = y - sub.origin_y;
    let z = level - sub.level_offset;
    let mut out = Vec::new();
    for layer in &sub.map.layers {
        if layer.level != z {
            continue;
        }
        if let Some(tile) = layer.get(local_x, local_y) {
            out.push(Hit {
                layer: layer.name.clone(),
                tile: tile.to_string(),
                above: layer.above_lot,
            });
        }
    }
    out
}

pub fn any_tile(maps: &[PlacedMap], x: i32, y: i32) -> bool {
    let min = maps.iter().map(|m| m.map.min_level() + m.level_offset).min().unwrap_or(0);
    let max = maps.iter().map(|m| m.map.max_level() + m.level_offset).max().unwrap_or(0);
    (min..=max).any(|level| !tiles_at(maps, x, y, level).is_empty())
}
