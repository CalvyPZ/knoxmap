//! Cutting a selection out of the saved area grid.
//!
//! The daily is already reduced to a grid of ways, places, and relations.
//! Deciding which of those meet the box, and writing the clip the rest of the
//! program reads, was the slow part of a later map: every coordinate became a
//! Python float, and every kept feature became a JSON object on the
//! interpreter. This crate does that cut in one call.
//!
//! Ring joining is endpoint to endpoint. A shore chain closes when its ends
//! are the same node. A chain that does not close is dropped, as
//! `_closed_role_rings` drops it.

mod err;
mod geom;
mod inflate;
mod pbf;
mod proto;
mod read;
mod rings;
mod walk;

use std::collections::{HashMap, HashSet};
use std::ffi::CStr;
use std::fs;
use std::os::raw::c_char;
use std::path::Path;

use knox_ways::area::{is_area, tag_pairs};
use knox_ways::classify::{classify, contains, COVER, HIT, SCALE};

use err::Error;
use geom::Files;
use read::{RelRec, WayRec};

pub use pbf::WalkNote;

/// Ways, places, and relations. A caller that already wrote one of them
/// passes the others.
pub const PART_WAYS: u32 = 1;
pub const PART_NODES: u32 = 2;
pub const PART_RELATIONS: u32 = 4;

pub extern "C" fn knox_c_abi_version() -> u32 {
    1
}

pub extern "C" fn knox_c_error_text(code: u32) -> *const c_char {
    err::text_for(code).as_ptr() as *const c_char
}

/// Cut `box` out of the span directory into `out_dir`.
///
/// `parts` selects ways, nodes, and relations. Files already in `out_dir`
/// are appended to, so a caller that wrote the places itself can ask for
/// just the ways and relations.
///
/// # Safety
/// Both paths are nul-terminated UTF-8. `node_rules` is `node_rules_len`
/// bytes, or null when the length is zero. Rules are `key` or `key=value`
/// lines; an empty list keeps every place in the grid.
pub unsafe extern "C" fn knox_c_cut(
    span_dir: *const c_char,
    out_dir: *const c_char,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    span_rows: u32,
    span_cols: u32,
    span_wide: u32,
    out_rows: u32,
    out_cols: u32,
    filters_version: u32,
    clip_version: u32,
    parts: u32,
    node_rules: *const u8,
    node_rules_len: usize,
) -> u32 {
    let run = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let span = c_path(span_dir)?;
        let out = c_path(out_dir)?;
        let rules = if node_rules.is_null() || node_rules_len == 0 {
            &[][..]
        } else {
            std::slice::from_raw_parts(node_rules, node_rules_len)
        };
        cut(
            &span,
            &out,
            south,
            west,
            north,
            east,
            region_south,
            region_west,
            region_north,
            region_east,
            deg,
            span_rows,
            span_cols,
            span_wide,
            out_rows,
            out_cols,
            filters_version,
            clip_version,
            parts,
            rules,
            std::ptr::null(),
            std::ptr::null_mut(),
        )
    }));
    match run {
        Ok(Ok(())) => 0,
        Ok(Err(err)) => err.code(),
        Err(_) => Error::Panic.code(),
    }
}

fn c_path(ptr: *const c_char) -> Result<std::path::PathBuf, Error> {
    if ptr.is_null() {
        return Err(Error::BadState);
    }
    let text = unsafe { CStr::from_ptr(ptr) };
    let text = text.to_str().map_err(|_| Error::BadState)?;
    Ok(std::path::PathBuf::from(text))
}

fn cut(
    span_dir: &Path,
    out_dir: &Path,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    span_rows: u32,
    span_cols: u32,
    span_wide: u32,
    out_rows: u32,
    out_cols: u32,
    filters_version: u32,
    clip_version: u32,
    parts: u32,
    node_rules: &[u8],
    stop: *const u32,
    note: *mut pbf::WalkNote,
) -> Result<(), Error> {
    pbf::publish(note, 3, 0, 0);
    let values = [south, west, north, east, region_south, region_west, region_north, region_east, deg];
    if values.iter().any(|v| !v.is_finite()) || north < south || east < west {
        return Err(Error::BadBox);
    }
    fs::create_dir_all(out_dir).map_err(|_| Error::Io)?;
    let limits = (
        south * SCALE - 2.0,
        north * SCALE + 2.0,
        west * SCALE - 2.0,
        east * SCALE + 2.0,
    );
    let paths = span_files(
        span_dir,
        west,
        south,
        east,
        north,
        region_south,
        region_west,
        region_north,
        region_east,
        deg,
        span_rows as i32,
        span_cols as i32,
        span_wide as i32,
    );
    let mut ways: HashMap<i64, WayRec> = HashMap::new();
    let mut nodes = Vec::new();
    for path in &paths {
        pbf::check_stop(stop)?;
        let Ok(bytes) = fs::read(path) else { continue };
        if bytes.len() < 4 || &bytes[..4] != b"KNXS" {
            continue;
        }
        let body = &bytes[4..];
        if parts & PART_WAYS != 0 || parts & PART_RELATIONS != 0 {
            read::load_ways(body, &mut ways, limits);
        }
        if parts & PART_NODES != 0 {
            read::load_nodes(body, &mut nodes);
        }
    }
    let relations = if parts & PART_RELATIONS != 0 {
        let path = span_dir.join("relations.bin");
        match fs::read(&path) {
            Ok(bytes) if bytes.len() >= 4 && &bytes[..4] == b"KNXS" => read::load_relations(&bytes[4..], limits),
            _ => Vec::new(),
        }
    } else {
        Vec::new()
    };
    let rules = parse_rules(node_rules);
    let mut files = Files::new(out_dir, south, west, north, east, out_rows as i32, out_cols as i32);
    let mut work = 0u64;
    if parts & PART_WAYS != 0 {
        work += ways.len() as u64;
    }
    if parts & PART_NODES != 0 {
        work += nodes.len() as u64;
    }
    if parts & PART_RELATIONS != 0 {
        work += relations.len() as u64;
    }
    let mut cursor = 0u64;
    pbf::publish(note, 3, 0, work);
    if parts & PART_WAYS != 0 {
        write_ways(&mut files, &ways, south, west, north, east, stop, note, &mut cursor, work)?;
    }
    if parts & PART_NODES != 0 {
        write_nodes(&mut files, &nodes, &rules, south, west, north, east, stop, note, &mut cursor, work)?;
    }
    if parts & PART_RELATIONS != 0 {
        write_relations(
            &mut files, &relations, &ways, south, west, north, east, stop, note, &mut cursor, work,
        )?;
    }
    pbf::publish(note, 3, work, work);
    files.finish(filters_version, clip_version)
}

fn tick(stop: *const u32, note: *mut pbf::WalkNote, cursor: &mut u64, work: u64) -> Result<(), Error> {
    *cursor += 1;
    if *cursor % 4096 == 0 {
        pbf::check_stop(stop)?;
        pbf::publish(note, 3, *cursor, work);
    }
    Ok(())
}

fn write_ways(
    files: &mut Files,
    ways: &HashMap<i64, WayRec>,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    stop: *const u32,
    note: *mut pbf::WalkNote,
    cursor: &mut u64,
    work: u64,
) -> Result<(), Error> {
    for (id, way) in ways {
        tick(stop, note, cursor, work)?;
        let status = classify(&way.xy, south, west, north, east);
        if status != HIT && status != COVER {
            continue;
        }
        let closed = way.xy.len() >= 32 && way.xy.len() % 8 == 0 && way.xy[..8] == way.xy[way.xy.len() - 8..];
        let geometry = if closed && is_area(&way.tags, true) {
            geom::polygon_geometry(&[way.xy.clone()])
        } else if way.xy.len() >= 16 {
            geom::line_geometry(&way.xy)
        } else {
            continue;
        };
        let line = geom::feature_line("way", *id, &way.tags, &geometry);
        files.add(&line, &way.xy, status == COVER)?;
    }
    Ok(())
}

fn write_nodes(
    files: &mut Files,
    nodes: &[read::NodeRec],
    rules: &Rules,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    stop: *const u32,
    note: *mut pbf::WalkNote,
    cursor: &mut u64,
    work: u64,
) -> Result<(), Error> {
    for node in nodes {
        tick(stop, note, cursor, work)?;
        let lat = node.y as f64 / SCALE;
        let lon = node.x as f64 / SCALE;
        if lat < south || lat > north || lon < west || lon > east {
            continue;
        }
        let tags = match std::str::from_utf8(&node.tags).ok().and_then(tag_pairs) {
            Some(pairs) => pairs,
            None => continue,
        };
        if !rules.allows(&tags) {
            continue;
        }
        let geometry = geom::point_geometry(node.x, node.y);
        let line = geom::feature_line("node", node.id, &node.tags, &geometry);
        let mut xy = Vec::with_capacity(8);
        xy.extend_from_slice(&node.x.to_le_bytes());
        xy.extend_from_slice(&node.y.to_le_bytes());
        files.add(&line, &xy, false)?;
    }
    Ok(())
}

fn write_relations(
    files: &mut Files,
    relations: &[RelRec],
    ways: &HashMap<i64, WayRec>,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    stop: *const u32,
    note: *mut pbf::WalkNote,
    cursor: &mut u64,
    work: u64,
) -> Result<(), Error> {
    for rel in relations {
        tick(stop, note, cursor, work)?;
        if let Some((geometry, flat, cover)) = relation_geometry(rel, ways, south, west, north, east) {
            let line = geom::feature_line("relation", rel.id, &rel.tags, &geometry);
            files.add(&line, &flat, cover)?;
        }
    }
    Ok(())
}

fn relation_geometry(
    rel: &RelRec,
    ways: &HashMap<i64, WayRec>,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> Option<(String, Vec<u8>, bool)> {
    let way_members = rel.members.len();
    let mut lines = Vec::new();
    for member in &rel.members {
        let xy = if rel.water {
            if member.xy.is_empty() {
                None
            } else {
                Some(member.xy.as_slice())
            }
        } else {
            ways.get(&member.id).map(|way| way.xy.as_slice())
        };
        let Some(xy) = xy else {
            if rel.water {
                return None;
            }
            continue;
        };
        if xy.len() < 16 {
            if rel.water {
                return None;
            }
            continue;
        }
        lines.push((member.role_inner, xy));
    }
    if rel.water && lines.len() != way_members {
        return None;
    }
    if !lines.iter().any(|(inner, _)| !*inner) {
        return None;
    }
    if envelope_misses(&lines, south, west, north, east) {
        return None;
    }
    let owned: Vec<(bool, Vec<u8>)> = lines
        .iter()
        .map(|(inner, xy)| (*inner, (*xy).to_vec()))
        .collect();
    let (outers, inners) = rings::closed_rings(&owned)?;
    let keep = outers.iter().any(|ring| {
        let status = classify(ring, south, west, north, east);
        status == HIT || status == COVER
    });
    if !keep {
        return None;
    }
    let mut groups: Vec<(Vec<u8>, Vec<Vec<u8>>)> = Vec::new();
    if outers.len() == 1 {
        groups.push((outers[0].clone(), inners));
    } else {
        let mut assigned = vec![Vec::new(); outers.len()];
        for inner in inners {
            let (lat, lon) = first_latlon(&inner);
            let mut placed = false;
            for (index, outer) in outers.iter().enumerate() {
                if contains(outer, lat, lon) {
                    assigned[index].push(inner.clone());
                    placed = true;
                    break;
                }
            }
            if !placed {
                assigned[0].push(inner);
            }
        }
        for (outer, holes) in outers.into_iter().zip(assigned) {
            groups.push((outer, holes));
        }
    }
    let mut flat = Vec::new();
    let mut cover = false;
    let mut polygons: Vec<Vec<Vec<u8>>> = Vec::new();
    for (outer, holes) in &groups {
        flat.extend_from_slice(outer);
        for hole in holes {
            flat.extend_from_slice(hole);
        }
        if classify(outer, south, west, north, east) == COVER {
            cover = true;
        }
        let mut rings_xy = vec![outer.clone()];
        rings_xy.extend(holes.iter().cloned());
        polygons.push(rings_xy);
    }
    let geometry = if polygons.len() == 1 {
        geom::polygon_geometry(&polygons[0])
    } else {
        geom::multi_geometry(&polygons)
    };
    Some((geometry, flat, cover))
}

fn first_latlon(xy: &[u8]) -> (f64, f64) {
    if xy.len() < 8 {
        return (0.0, 0.0);
    }
    let x = i32::from_le_bytes(xy[0..4].try_into().unwrap());
    let y = i32::from_le_bytes(xy[4..8].try_into().unwrap());
    (y as f64 / SCALE, x as f64 / SCALE)
}

fn envelope_misses(lines: &[(bool, &[u8])], south: f64, west: f64, north: f64, east: f64) -> bool {
    let mut min_lat: f64 = 90.0;
    let mut max_lat: f64 = -90.0;
    let mut min_lon: f64 = 180.0;
    let mut max_lon: f64 = -180.0;
    let mut seen = false;
    for (_, xy) in lines {
        let mut i = 0;
        while i + 8 <= xy.len() {
            let x = i32::from_le_bytes(xy[i..i + 4].try_into().unwrap());
            let y = i32::from_le_bytes(xy[i + 4..i + 8].try_into().unwrap());
            let lat = y as f64 / SCALE;
            let lon = x as f64 / SCALE;
            min_lat = min_lat.min(lat);
            max_lat = max_lat.max(lat);
            min_lon = min_lon.min(lon);
            max_lon = max_lon.max(lon);
            seen = true;
            i += 8;
        }
    }
    if !seen {
        return true;
    }
    max_lat < south || min_lat > north || max_lon < west || min_lon > east
}

struct Rules {
    any: HashSet<String>,
    values: HashMap<String, HashSet<String>>,
    empty: bool,
}

impl Rules {
    fn allows(&self, tags: &[(String, String)]) -> bool {
        if self.empty {
            return true;
        }
        for (key, value) in tags {
            if self.any.contains(key) {
                return true;
            }
            if let Some(wanted) = self.values.get(key) {
                if wanted.contains(value) {
                    return true;
                }
            }
        }
        false
    }
}

fn parse_rules(raw: &[u8]) -> Rules {
    let mut rules = Rules {
        any: HashSet::new(),
        values: HashMap::new(),
        empty: true,
    };
    let Ok(text) = std::str::from_utf8(raw) else {
        return rules;
    };
    for line in text.split('\n') {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        rules.empty = false;
        if let Some((key, value)) = line.split_once('=') {
            if rules.any.contains(key) {
                continue;
            }
            rules.values.entry(key.to_string()).or_default().insert(value.to_string());
        } else {
            rules.values.remove(line);
            rules.any.insert(line.to_string());
        }
    }
    rules
}

fn span_files(
    dir: &Path,
    west: f64,
    south: f64,
    east: f64,
    north: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    rows: i32,
    cols: i32,
    wide: i32,
) -> Vec<std::path::PathBuf> {
    let mut paths = vec![dir.join("wide.bin")];
    let bounds = (
        trunc_scale(west) - 2,
        trunc_scale(south) - 2,
        trunc_scale(east) + 2,
        trunc_scale(north) + 2,
    );
    match cells_touched(bounds, region_south, region_west, region_north, region_east, deg, rows, cols, wide) {
        None => {
            if let Ok(read) = fs::read_dir(dir) {
                for entry in read.flatten() {
                    let name = entry.file_name();
                    let name = name.to_string_lossy();
                    if name.starts_with('r') && name.ends_with(".bin") {
                        paths.push(entry.path());
                    }
                }
            }
        }
        Some(cells) => {
            for (row, col) in cells {
                paths.push(dir.join(format!("r{row}c{col}.bin")));
            }
        }
    }
    paths
}

fn trunc_scale(v: f64) -> i32 {
    (v * SCALE) as i32
}

/// Read one daily, file the regional grid, and cut the selection into `out_dir`.
///
/// `rules` is `kind/key` or `kind/key=value` lines (`n`, `w`, `r`). `stop`
/// may be null; a non-zero value asks the walk to abandon what it has written.
/// `note` may be null. When it is live, each pass writes its phase and how
/// far through that pass the walk has got.
///
/// # Safety
/// Paths are nul-terminated UTF-8. `rules` is `rules_len` bytes, or null when
/// the length is zero. `stop` is either null or a live `u32` for the whole call.
pub unsafe extern "C" fn knox_c_walk(
    pbf: *const c_char,
    span_dir: *const c_char,
    out_dir: *const c_char,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    span_wide: u32,
    out_rows: u32,
    out_cols: u32,
    filters_version: u32,
    clip_version: u32,
    span_version: u32,
    mtime_ms: i64,
    rules: *const u8,
    rules_len: usize,
    stop: *const u32,
    note: *mut pbf::WalkNote,
) -> u32 {
    let run = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let pbf = c_path(pbf)?;
        let span = c_path(span_dir)?;
        let out = c_path(out_dir)?;
        let raw = if rules.is_null() || rules_len == 0 {
            &[][..]
        } else {
            std::slice::from_raw_parts(rules, rules_len)
        };
        walk::run(
            &pbf,
            &span,
            &out,
            south,
            west,
            north,
            east,
            region_south,
            region_west,
            region_north,
            region_east,
            deg,
            span_wide,
            out_rows,
            out_cols,
            filters_version,
            clip_version,
            span_version,
            mtime_ms,
            raw,
            stop,
            note,
        )
    }));
    match run {
        Ok(Ok(())) => 0,
        Ok(Err(err)) => err.code(),
        Err(_) => Error::Panic.code(),
    }
}

fn cells_touched(
    bounds: (i32, i32, i32, i32),
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    rows: i32,
    cols: i32,
    wide: i32,
) -> Option<Vec<(i32, i32)>> {
    let _ = (region_north, region_east);
    let (min_x, min_y, max_x, max_y) = bounds;
    let cell = |lat: f64, lon: f64| -> (i32, i32) {
        let mut row = if deg == 0.0 {
            0
        } else {
            ((lat - region_south) / deg) as i32
        };
        let mut col = if deg == 0.0 {
            0
        } else {
            ((lon - region_west) / deg) as i32
        };
        if row < 0 {
            row = 0;
        } else if rows > 0 && row >= rows {
            row = rows - 1;
        }
        if col < 0 {
            col = 0;
        } else if cols > 0 && col >= cols {
            col = cols - 1;
        }
        (row, col)
    };
    let (mut r0, mut c0) = cell(min_y as f64 / SCALE, min_x as f64 / SCALE);
    let (mut r1, mut c1) = cell(max_y as f64 / SCALE, max_x as f64 / SCALE);
    if r0 > r1 {
        std::mem::swap(&mut r0, &mut r1);
    }
    if c0 > c1 {
        std::mem::swap(&mut c0, &mut c1);
    }
    let across = (r1 as i64 - r0 as i64 + 1) * (c1 as i64 - c0 as i64 + 1);
    if wide > 0 && across > wide as i64 {
        return None;
    }
    let mut out = Vec::new();
    for row in r0..=r1 {
        for col in c0..=c1 {
            out.push((row, col));
        }
    }
    Some(out)
}
