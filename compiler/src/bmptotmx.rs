// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// BMPToTMX::convertCell and MapWriter. WorldEd forces copyPixels and compress
// on, so the written tile layers are empty and the terrain stays in bmp-image.
// `--no-copy-pixels` runs imagesToTileGrids and addEdgeTiles. Deflate bytes
// come from flate2; a later diff compares the uncompressed payloads when the
// wrappers differ. Rules.txt and Blends.txt are read from the paths given,
// not copied.

//! Write one cell's TMX and record its path on the project.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::generate;
use crate::lotbytes;
use crate::project::{self, Project};
use crate::rules::{self, RuleBook};

const FALLBACK_LAYERS: &[(&str, bool)] = &[
    ("0_Floor", true),
    ("0_FloorOverlay", true),
    ("0_FloorOverlay2", true),
    ("0_FloorOverlay3", true),
    ("0_FloorOverlay4", true),
    ("0_FloorOverlay5", true),
    ("0_FloorOverlay6", true),
    ("0_Curbs", true),
    ("0_Walls", true),
    ("0_Walls2", true),
    ("0_Furniture", true),
    ("0_Furniture2", true),
    ("0_Furniture3", true),
    ("0_Furniture4", true),
    ("0_Vegetation", true),
    ("0_Lots", false),
];

pub struct Converted {
    pub cell: (i32, i32),
    pub path: PathBuf,
}

pub fn convert_project(
    project: &Project,
    book: &RuleBook,
    map_base: Option<&Path>,
    copy_pixels: bool,
) -> Result<Vec<Converted>, String> {
    let export = if project.bmp.export_dir.is_empty() {
        project.dir.join("tmx")
    } else {
        project::native_path(&project.dir, &project.bmp.export_dir)
    };
    std::fs::create_dir_all(&export).map_err(|err| err.to_string())?;
    let bmp_path = project::native_path(&project.dir, &project.bmp_path);
    let image = if bmp_path.is_file() {
        generate::read_bmp(&bmp_path).ok()
    } else {
        None
    };
    let stem = bmp_path
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "map".into());
    let layers = load_layers(map_base);
    let cell_size = if project.legacy_300 { 300 } else { 256 };
    let mut written = Vec::new();
    let mut assigned = HashMap::new();
    for y in 0..project.height {
        for x in 0..project.width {
            let abs_x = project.world_origin_x + x;
            let abs_y = project.world_origin_y + y;
            let name = format!("{stem}_{abs_x}_{abs_y}.tmx");
            let path = export.join(&name);
            let xml = write_cell(
                project,
                book,
                &layers,
                image.as_ref(),
                x,
                y,
                cell_size,
                &path,
                copy_pixels,
            )?;
            std::fs::write(&path, xml).map_err(|err| format!("{}: {err}", path.display()))?;
            assigned.insert((x, y), project::forward_slash(&path));
            written.push(Converted { cell: (x, y), path });
        }
    }
    project::assign_maps(project, &assigned)?;
    Ok(written)
}

fn load_layers(map_base: Option<&Path>) -> Vec<(String, bool)> {
    let Some(path) = map_base else {
        return FALLBACK_LAYERS.iter().map(|(n, t)| (n.to_string(), *t)).collect();
    };
    let Ok(text) = std::fs::read_to_string(path) else {
        return FALLBACK_LAYERS.iter().map(|(n, t)| (n.to_string(), *t)).collect();
    };
    let mut layers = Vec::new();
    for raw in text.lines() {
        let line = raw.trim();
        if let Some(name) = line.strip_prefix("tile") {
            let name = name.trim().trim_start_matches('=').trim();
            if !name.is_empty() {
                layers.push((name.to_string(), true));
            }
        } else if let Some(name) = line.strip_prefix("object") {
            let name = name.trim().trim_start_matches('=').trim();
            if !name.is_empty() {
                layers.push((name.to_string(), false));
            }
        }
    }
    if layers.is_empty() {
        FALLBACK_LAYERS.iter().map(|(n, t)| (n.to_string(), *t)).collect()
    } else {
        layers
    }
}

fn write_cell(
    project: &Project,
    book: &RuleBook,
    layers: &[(String, bool)],
    image: Option<&generate::Spawn>,
    cell_x: i32,
    cell_y: i32,
    cell_size: i32,
    tmx_path: &Path,
    copy_pixels: bool,
) -> Result<String, String> {
    let mut ground = crop(image, project, cell_x, cell_y, cell_size);
    let veg = vec![(0u8, 0u8, 0u8); (cell_size * cell_size) as usize];
    if project.bmp.repair_unknown {
        repair(&mut ground, book, &project.bmp.unknown_ground);
    }
    let painted = if copy_pixels {
        HashMap::new()
    } else {
        blend(&ground, &veg, book, cell_size)
    };
    let rules_file = relative_attr(tmx_path, &project.bmp.rules);
    let blends_file = relative_attr(tmx_path, &project.bmp.blends);
    let mut xml = String::new();
    xml.push_str("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n");
    xml.push_str(&format!(
        "<map version=\"2.0\" orientation=\"levelisometric\" width=\"{cell_size}\" height=\"{cell_size}\" tilewidth=\"64\" tileheight=\"32\">\n"
    ));
    for (full, tile) in layers {
        let (level, name) = split_layer(full);
        if *tile {
            let gids = painted.get(full).cloned().unwrap_or_else(|| vec![0u32; (cell_size * cell_size) as usize]);
            let body = layer_payload(&gids)?;
            xml.push_str(&format!(
                " <layer name=\"{name}\" level=\"{level}\" width=\"{cell_size}\" height=\"{cell_size}\">\n"
            ));
            xml.push_str("  <data encoding=\"base64\" compression=\"zlib\">\n");
            xml.push_str("   ");
            xml.push_str(&body);
            xml.push_str("\n  </data>\n");
            xml.push_str(" </layer>\n");
        } else {
            xml.push_str(&format!(
                " <objectgroup name=\"{name}\" level=\"{level}\" width=\"{cell_size}\" height=\"{cell_size}\"/>\n"
            ));
        }
    }
    xml.push_str(" <bmp-settings version=\"1\">\n");
    xml.push_str(&format!("  <rules-file file=\"{rules_file}\"/>\n"));
    xml.push_str(&format!("  <blends-file file=\"{blends_file}\"/>\n"));
    xml.push_str("  <edges-everywhere value=\"false\"/>\n");
    xml.push_str("  <aliases>\n");
    for alias in &book.aliases {
        xml.push_str(&format!(
            "   <alias name=\"{}\" tiles=\"{}\"/>\n",
            escape(&alias.name),
            escape(&alias.tiles.join(" "))
        ));
    }
    xml.push_str("  </aliases>\n");
    xml.push_str("  <rules>\n");
    for rule in &book.rules {
        let choices: Vec<String> = rule.tiles.iter().map(|t| if t.is_empty() { "null".into() } else { t.clone() }).collect();
        let mut line = format!(
            "   <rule label=\"{}\" bitmapIndex=\"{}\" color=\"{}\" tileChoices=\"{}\" targetLayer=\"{}\" condition=\"{}\"",
            escape(&rule.label),
            rule.bitmap,
            rules::rgb_string(rule.color),
            escape(&choices.join(" ")),
            escape(&rule.layer),
            rules::rgb_string(rule.condition)
        );
        if rule.obsolete {
            line.push_str(" obsolete=\"true\"");
        }
        line.push_str("/>\n");
        xml.push_str(&line);
    }
    xml.push_str("  </rules>\n");
    xml.push_str("  <blends>\n");
    for blend in &book.blends {
        xml.push_str(&format!(
            "   <blend targetLayer=\"{}\" mainTile=\"{}\" blendTile=\"{}\" dir=\"{}\" ExclusionList=\"{}\" exclude2=\"{}\"/>\n",
            escape(&blend.layer),
            escape(&blend.main_tile),
            escape(&blend.blend_tile),
            escape(&blend.dir),
            escape(&blend.exclude.join(" ")),
            escape(&blend.exclude2.join(" "))
        ));
    }
    xml.push_str("  </blends>\n");
    xml.push_str(" </bmp-settings>\n");
    let stored_ground = if copy_pixels { &ground } else { &veg };
    let stored_veg = &veg;
    xml.push_str(&bmp_image(0, stored_ground, cell_size)?);
    xml.push_str(&bmp_image(1, stored_veg, cell_size)?);
    xml.push_str("</map>\n");
    Ok(xml)
}

fn split_layer(full: &str) -> (i32, String) {
    match full.split_once('_') {
        Some((level, name)) => (level.parse().unwrap_or(0), name.to_string()),
        None => (0, full.to_string()),
    }
}

fn crop(
    image: Option<&generate::Spawn>,
    project: &Project,
    cell_x: i32,
    cell_y: i32,
    cell_size: i32,
) -> Vec<(u8, u8, u8)> {
    let mut out = vec![(0u8, 0u8, 0u8); (cell_size * cell_size) as usize];
    let Some(image) = image else { return out };
    let ox = (cell_x - project.bmp_x) * cell_size;
    let oy = (cell_y - project.bmp_y) * cell_size;
    for y in 0..cell_size {
        for x in 0..cell_size {
            out[(y * cell_size + x) as usize] = image.rgb_at(ox + x, oy + y);
        }
    }
    out
}

fn repair(pixels: &mut [(u8, u8, u8)], book: &RuleBook, ground: &str) {
    let mut known = std::collections::HashSet::new();
    known.insert((0u8, 0u8, 0u8));
    for rule in &book.rules {
        known.insert(rule.color);
    }
    let fallback = book.rules.iter().find(|r| r.label == ground || r.tiles.iter().any(|t| t == ground)).map(|r| r.color).unwrap_or((0, 0, 0));
    for pixel in pixels {
        if !known.contains(pixel) {
            *pixel = fallback;
        }
    }
}

/// `BmpBlender::imagesToTileGrids` then `addEdgeTiles`. Tile choice uses the
/// same seed WorldEd stores (1) and an xorshift stream. Qt's `QRandomGenerator`
/// is the stream a byte diff has to check.
fn blend(ground: &[(u8, u8, u8)], veg: &[(u8, u8, u8)], book: &RuleBook, size: i32) -> HashMap<String, Vec<u32>> {
    let mut layers: HashMap<String, Vec<Option<String>>> = HashMap::new();
    let mut rng = Rng::new(1);
    let _ = rng.next();
    for y in 0..size {
        for x in 0..size {
            let i = (y * size + x) as usize;
            let col = ground[i];
            if let Some(rules) = book.by_color0.get(&col) {
                for index in rules {
                    let rule = &book.rules[*index];
                    if rule.bitmap != 0 || rule.tiles.is_empty() {
                        continue;
                    }
                    let pick = rule.tiles[(rand_at(1, x, y) as usize) % rule.tiles.len()].clone();
                    layers.entry(rule.layer.clone()).or_insert_with(|| vec![None; (size * size) as usize])[i] = Some(pick);
                }
            }
            let col2 = veg[i];
            if col2 != (0, 0, 0) {
                if let Some(rules) = book.by_color1.get(&col2) {
                    for index in rules {
                        let rule = &book.rules[*index];
                        if rule.bitmap != 1 || rule.tiles.is_empty() {
                            continue;
                        }
                        if rule.condition != col && rule.condition != (0, 0, 0) {
                            continue;
                        }
                        let pick = rule.tiles[(rand_at(1, x, y) as usize) % rule.tiles.len()].clone();
                        layers.entry(rule.layer.clone()).or_insert_with(|| vec![None; (size * size) as usize])[i] = Some(pick);
                    }
                }
            }
        }
    }
    // Edge blends look at the floor grid and the eight neighbours.
    let floor_key = layers.keys().find(|k| k.ends_with("Floor") && !k.contains("Overlay")).cloned();
    if let Some(floor_key) = floor_key {
        let floor = layers.get(&floor_key).cloned().unwrap_or_default();
        for blend in &book.blends {
            let (dx, dy) = dir_delta(&blend.dir);
            for y in 0..size {
                for x in 0..size {
                    let i = (y * size + x) as usize;
                    let here = floor.get(i).and_then(|t| t.as_deref()).unwrap_or("");
                    if here != blend.main_tile && !rules::resolve_tile(book, &blend.main_tile).contains(&here) {
                        continue;
                    }
                    let nx = x + dx;
                    let ny = y + dy;
                    if nx < 0 || ny < 0 || nx >= size || ny >= size {
                        continue;
                    }
                    let ni = (ny * size + nx) as usize;
                    let neighbor = floor.get(ni).and_then(|t| t.as_deref()).unwrap_or("");
                    let want = rules::resolve_tile(book, &blend.blend_tile);
                    if neighbor != blend.blend_tile && !want.contains(&neighbor) {
                        continue;
                    }
                    if blend.exclude.iter().any(|name| name == here || name == neighbor) {
                        continue;
                    }
                    let choices = rules::resolve_tile(book, &blend.blend_tile);
                    if choices.is_empty() {
                        continue;
                    }
                    let pick = choices[(rand_at(1, x, y) as usize) % choices.len()].to_string();
                    layers.entry(blend.layer.clone()).or_insert_with(|| vec![None; (size * size) as usize])[i] = Some(pick);
                }
            }
        }
    }
    let mut out = HashMap::new();
    for (name, cells) in layers {
        let mut gids = vec![0u32; cells.len()];
        let mut seen: HashMap<String, u32> = HashMap::new();
        let mut next = 1u32;
        for (i, cell) in cells.into_iter().enumerate() {
            if let Some(tile) = cell {
                let gid = *seen.entry(tile).or_insert_with(|| {
                    let gid = next;
                    next += 1;
                    gid
                });
                gids[i] = gid;
            }
        }
        out.insert(name, gids);
    }
    let _ = book;
    out
}

fn dir_delta(dir: &str) -> (i32, i32) {
    match dir {
        "n" => (0, -1),
        "s" => (0, 1),
        "e" => (1, 0),
        "w" => (-1, 0),
        "nw" => (-1, -1),
        "ne" => (1, -1),
        "sw" => (-1, 1),
        "se" => (1, 1),
        _ => (0, 0),
    }
}

struct Rng {
    state: u64,
}

impl Rng {
    fn new(seed: u64) -> Self {
        Self { state: seed.max(1) }
    }
    fn next(&mut self) -> u32 {
        self.state ^= self.state << 13;
        self.state ^= self.state >> 7;
        self.state ^= self.state << 17;
        self.state as u32
    }
}

fn rand_at(seed: u32, x: i32, y: i32) -> u32 {
    let mut rng = Rng::new((seed as u64) << 32 | (x as u32 as u64) << 16 | y as u32 as u64);
    rng.next()
}

fn layer_payload(gids: &[u32]) -> Result<String, String> {
    let mut raw = Vec::with_capacity(gids.len() * 4);
    for gid in gids {
        raw.extend_from_slice(&gid.to_le_bytes());
    }
    let zipped = lotbytes::deflate_zlib(&raw)?;
    Ok(lotbytes::encode_base64(&zipped))
}

fn bmp_image(index: i32, pixels: &[(u8, u8, u8)], size: i32) -> Result<String, String> {
    let mut colors: Vec<(u8, u8, u8)> = pixels.iter().copied().collect();
    colors.sort_unstable();
    colors.dedup();
    colors.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let mut raw = Vec::with_capacity(pixels.len() * 4);
    for pixel in pixels {
        let n: u32 = if *pixel == (0, 0, 0) {
            0
        } else {
            colors.iter().position(|c| c == pixel).unwrap_or(0) as u32 + 1
        };
        raw.extend_from_slice(&n.to_le_bytes());
    }
    let zipped = lotbytes::deflate_gzip(&raw)?;
    let mut xml = format!(" <bmp-image index=\"{index}\" seed=\"1\">\n");
    for color in &colors {
        xml.push_str(&format!(
            "  <color rgb=\"{} {} {}\"/>\n",
            color.0, color.1, color.2
        ));
    }
    xml.push_str("  <pixels>\n   ");
    xml.push_str(&lotbytes::encode_base64(&zipped));
    xml.push_str("\n  </pixels>\n");
    xml.push_str(" </bmp-image>\n");
    let _ = size;
    Ok(xml)
}

fn relative_attr(tmx_path: &Path, stored: &str) -> String {
    if stored.is_empty() {
        return String::new();
    }
    let target = PathBuf::from(stored.replace('\\', "/"));
    let from = tmx_path.parent().unwrap_or(Path::new("."));
    match pathdiff(from, &target) {
        Some(rel) => {
            let text = rel.to_string_lossy().replace('/', "\\");
            if cfg!(windows) { text } else { rel.to_string_lossy().replace('\\', "/") }
        }
        None => stored.replace('/', "\\"),
    }
}

fn pathdiff(from_dir: &Path, to: &Path) -> Option<PathBuf> {
    let from_comps: Vec<_> = from_dir.components().collect();
    let to_comps: Vec<_> = to.components().collect();
    if from_comps.first() != to_comps.first() {
        return None;
    }
    let mut i = 0;
    while i < from_comps.len() && i < to_comps.len() && from_comps[i] == to_comps[i] {
        i += 1;
    }
    let mut out = PathBuf::new();
    for _ in i..from_comps.len() {
        out.push("..");
    }
    for part in &to_comps[i..] {
        out.push(part);
    }
    Some(out)
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('"', "&quot;").replace('<', "&lt;")
}
