//! The first read of a daily.
//!
//! Places, ways and relations are filed into the regional grid, and the
//! selection is cut from that grid before the function returns. A later map
//! does not open the daily. A file this crate cannot read is an error, and
//! Python walks it instead.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use knox_ways::classify::SCALE;

use crate::err::Error;
use crate::pbf::{self, check_stop};
use crate::proto::{self, Cursor};

struct Rules {
    any: HashSet<String>,
    values: HashMap<String, HashSet<String>>,
}

struct KindRules {
    node: Rules,
    way: Rules,
    relation: Rules,
}

struct Block<'a> {
    strings: Vec<String>,
    granularity: i64,
    lat_offset: i64,
    lon_offset: i64,
    groups: Vec<&'a [u8]>,
}

struct Locations {
    rows: Vec<(i64, u64)>,
    extra: HashMap<i64, u64>,
    frozen: bool,
}

struct Shore {
    min_x: i32,
    min_y: i32,
    max_x: i32,
    max_y: i32,
    xy: Vec<u8>,
}

struct GridOut {
    south: f64,
    west: f64,
    deg: f64,
    rows: i32,
    cols: i32,
    wide_limit: i32,
    cells: HashMap<(i32, i32), Vec<u8>>,
    wide: Vec<u8>,
    relations: Vec<u8>,
}

struct Fill<'a> {
    rules: &'a KindRules,
    water: &'a HashSet<i64>,
    locs: &'a mut Locations,
    shores: &'a mut HashMap<i64, Shore>,
    grid: &'a mut GridOut,
    stop: *const u32,
    seen: u32,
}

pub fn run(
    pbf_path: &Path,
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
    span_wide: u32,
    out_rows: u32,
    out_cols: u32,
    filters_version: u32,
    clip_version: u32,
    span_version: u32,
    mtime_ms: i64,
    rules_raw: &[u8],
    stop: *const u32,
    note: *mut pbf::WalkNote,
) -> Result<(), Error> {
    let values = [
        south,
        west,
        north,
        east,
        region_south,
        region_west,
        region_north,
        region_east,
        deg,
    ];
    if values.iter().any(|value| !value.is_finite()) || north < south || east < west {
        return Err(Error::BadBox);
    }
    if region_north < region_south || region_east < region_west || deg <= 0.0 {
        return Err(Error::BadBox);
    }
    let rules = parse_rules(rules_raw);
    let mut water = HashSet::new();
    pbf::for_each_blob(pbf_path, stop, note, 1, |kind, plain| {
        if kind == "OSMHeader" {
            return header_ok(plain);
        }
        if kind == "OSMData" {
            let block = read_block(plain)?;
            for group in &block.groups {
                scan_water(group, &block, &rules, &mut water)?;
            }
        }
        Ok(())
    })?;
    let (rows, cols) = grid_shape(region_south, region_west, region_north, region_east, deg);
    let mut grid = GridOut {
        south: region_south,
        west: region_west,
        deg,
        rows,
        cols,
        wide_limit: span_wide.max(1) as i32,
        cells: HashMap::new(),
        wide: Vec::new(),
        relations: Vec::new(),
    };
    let mut locs = Locations {
        rows: Vec::new(),
        extra: HashMap::new(),
        frozen: false,
    };
    let mut shores = HashMap::new();
    pbf::for_each_blob(pbf_path, stop, note, 2, |kind, plain| {
        if kind != "OSMData" {
            return Ok(());
        }
        let block = read_block(plain)?;
        let mut fill = Fill {
            rules: &rules,
            water: &water,
            locs: &mut locs,
            shores: &mut shores,
            grid: &mut grid,
            stop,
            seen: 0,
        };
        for group in &block.groups {
            fill_group(group, &block, &mut fill)?;
        }
        Ok(())
    })?;
    pbf::publish(note, 3, 0, 0);
    let temp = temp_dir(span_dir)?;
    let mut guard = DirGuard {
        path: temp.clone(),
        armed: true,
    };
    write_span(
        &temp,
        &grid,
        span_version,
        filters_version,
        mtime_ms,
        region_south,
        region_west,
        region_north,
        region_east,
    )?;
    let node_rules = node_rule_text(&rules.node);
    crate::cut(
        &temp,
        out_dir,
        south,
        west,
        north,
        east,
        region_south,
        region_west,
        region_north,
        region_east,
        deg,
        rows as u32,
        cols as u32,
        span_wide,
        out_rows,
        out_cols,
        filters_version,
        clip_version,
        crate::PART_WAYS | crate::PART_NODES | crate::PART_RELATIONS,
        &node_rules,
        stop,
        note,
    )?;
    if span_dir.exists() {
        fs::remove_dir_all(span_dir).map_err(|_| Error::Io)?;
    }
    fs::rename(&temp, span_dir).map_err(|_| Error::Io)?;
    guard.armed = false;
    Ok(())
}

struct DirGuard {
    path: PathBuf,
    armed: bool,
}

impl Drop for DirGuard {
    fn drop(&mut self) {
        if self.armed {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

fn temp_dir(span_dir: &Path) -> Result<PathBuf, Error> {
    let name = span_dir.file_name().ok_or(Error::Io)?;
    let mut temp_name = name.to_os_string();
    temp_name.push(".part-rust");
    let temp = span_dir.with_file_name(temp_name);
    if temp.exists() {
        fs::remove_dir_all(&temp).map_err(|_| Error::Io)?;
    }
    fs::create_dir_all(&temp).map_err(|_| Error::Io)?;
    Ok(temp)
}

fn header_ok(data: &[u8]) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    while let Some((field, wire)) = cur.next()? {
        if field == 4 && wire == 2 {
            let raw = cur.bytes()?;
            let text = std::str::from_utf8(raw).unwrap_or("");
            if text != "OsmSchema-V0.6" && text != "DenseNodes" {
                return Err(Error::BadPbf);
            }
        } else {
            cur.skip(wire)?;
        }
    }
    Ok(())
}

fn read_block(data: &[u8]) -> Result<Block<'_>, Error> {
    let mut cur = Cursor::new(data);
    let mut strings = Vec::new();
    let mut granularity = 100i64;
    let mut lat_offset = 0i64;
    let mut lon_offset = 0i64;
    let mut groups = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match (field, wire) {
            (1, 2) => strings = string_table(cur.bytes()?)?,
            (2, 2) => groups.push(cur.bytes()?),
            (17, 0) => granularity = cur.varint()? as i64,
            (19, 0) => lat_offset = cur.varint()? as i64,
            (20, 0) => lon_offset = cur.varint()? as i64,
            _ => cur.skip(wire)?,
        }
    }
    if granularity <= 0 {
        return Err(Error::BadPbf);
    }
    Ok(Block {
        strings,
        granularity,
        lat_offset,
        lon_offset,
        groups,
    })
}

fn string_table(data: &[u8]) -> Result<Vec<String>, Error> {
    let mut cur = Cursor::new(data);
    let mut out = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        if field == 1 && wire == 2 {
            let raw = cur.bytes()?;
            out.push(String::from_utf8_lossy(raw).into_owned());
        } else {
            cur.skip(wire)?;
        }
    }
    Ok(out)
}

fn scan_water(group: &[u8], block: &Block, rules: &KindRules, water: &mut HashSet<i64>) -> Result<(), Error> {
    let mut cur = Cursor::new(group);
    while let Some((field, wire)) = cur.next()? {
        if field == 4 && wire == 2 {
            note_water(cur.bytes()?, block, rules, water)?;
        } else {
            cur.skip(wire)?;
        }
    }
    Ok(())
}

fn note_water(data: &[u8], block: &Block, rules: &KindRules, water: &mut HashSet<i64>) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    let mut keys = Vec::new();
    let mut vals = Vec::new();
    let mut roles = Vec::new();
    let mut mems = Vec::new();
    let mut types = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match field {
            2 => proto::take_varints(wire, &mut cur, &mut keys)?,
            3 => proto::take_varints(wire, &mut cur, &mut vals)?,
            8 => proto::take_varints(wire, &mut cur, &mut roles)?,
            9 => proto::take_varints(wire, &mut cur, &mut mems)?,
            10 => proto::take_varints(wire, &mut cur, &mut types)?,
            _ => cur.skip(wire)?,
        }
    }
    let pairs = pairs(&block.strings, &keys, &vals);
    if !matches_pairs(&rules.relation, &pairs) {
        return Ok(());
    }
    let kind = tag_get(&pairs, "type");
    if kind != Some("multipolygon") && kind != Some("boundary") {
        return Ok(());
    }
    if tag_get(&pairs, "natural") != Some("water") {
        return Ok(());
    }
    let mems = delta_ids(&mems);
    let n = mems.len().min(types.len());
    for i in 0..n {
        if types[i] == 1 {
            water.insert(mems[i]);
        }
    }
    let _ = roles;
    Ok(())
}

fn fill_group(group: &[u8], block: &Block, fill: &mut Fill) -> Result<(), Error> {
    let mut cur = Cursor::new(group);
    while let Some((field, wire)) = cur.next()? {
        if wire != 2 {
            cur.skip(wire)?;
            continue;
        }
        let raw = cur.bytes()?;
        match field {
            1 => one_node(raw, block, fill)?,
            2 => dense_nodes(raw, block, fill)?,
            3 => one_way(raw, block, fill)?,
            4 => one_relation(raw, block, fill)?,
            _ => {}
        }
    }
    Ok(())
}

fn one_node(data: &[u8], block: &Block, fill: &mut Fill) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    let mut id = 0i64;
    let mut lat = 0i64;
    let mut lon = 0i64;
    let mut keys = Vec::new();
    let mut vals = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match field {
            1 if wire == 0 => id = proto::zigzag(cur.varint()?),
            8 if wire == 0 => lat = proto::zigzag(cur.varint()?),
            9 if wire == 0 => lon = proto::zigzag(cur.varint()?),
            2 => append_varints(wire, &mut cur, &mut keys)?,
            3 => append_varints(wire, &mut cur, &mut vals)?,
            _ => cur.skip(wire)?,
        }
    }
    keep_node(id, lat, lon, &keys, &vals, block, fill)
}

fn dense_nodes(data: &[u8], block: &Block, fill: &mut Fill) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    let mut ids = Vec::new();
    let mut lats = Vec::new();
    let mut lons = Vec::new();
    let mut kv = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match field {
            1 => append_varints(wire, &mut cur, &mut ids)?,
            8 => append_varints(wire, &mut cur, &mut lats)?,
            9 => append_varints(wire, &mut cur, &mut lons)?,
            10 => append_varints(wire, &mut cur, &mut kv)?,
            _ => cur.skip(wire)?,
        }
    }
    let n = ids.len().min(lats.len()).min(lons.len());
    let mut id = 0i64;
    let mut lat = 0i64;
    let mut lon = 0i64;
    let mut t = 0usize;
    let mut keys = Vec::new();
    let mut vals = Vec::new();
    for i in 0..n {
        fill.seen += 1;
        if fill.seen % 8192 == 0 {
            check_stop(fill.stop)?;
        }
        id = id.wrapping_add(proto::zigzag(ids[i]));
        lat = lat.wrapping_add(proto::zigzag(lats[i]));
        lon = lon.wrapping_add(proto::zigzag(lons[i]));
        keys.clear();
        vals.clear();
        while t < kv.len() {
            let key = kv[t];
            t += 1;
            if key == 0 {
                break;
            }
            if t >= kv.len() {
                break;
            }
            let val = kv[t];
            t += 1;
            if key <= u32::MAX as u64 && val <= u32::MAX as u64 {
                keys.push(key);
                vals.push(val);
            }
        }
        keep_node(id, lat, lon, &keys, &vals, block, fill)?;
    }
    Ok(())
}

fn keep_node(
    id: i64,
    lat: i64,
    lon: i64,
    keys: &[u64],
    vals: &[u64],
    block: &Block,
    fill: &mut Fill,
) -> Result<(), Error> {
    let Some(x) = coord(block.lon_offset, block.granularity, lon) else {
        return Ok(());
    };
    let Some(y) = coord(block.lat_offset, block.granularity, lat) else {
        return Ok(());
    };
    fill.locs.add(id, x, y);
    if !matches_idx(&fill.rules.node, &block.strings, keys, vals) {
        return Ok(());
    }
    let tags = json_tags(&block.strings, keys, vals);
    fill.grid.add_node(id, x, y, &tags);
    Ok(())
}

fn one_way(data: &[u8], block: &Block, fill: &mut Fill) -> Result<(), Error> {
    fill.seen += 1;
    if fill.seen % 8192 == 0 {
        check_stop(fill.stop)?;
    }
    let mut cur = Cursor::new(data);
    let mut id = 0i64;
    let mut keys = Vec::new();
    let mut vals = Vec::new();
    let mut refs = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match field {
            1 if wire == 0 => id = cur.varint()? as i64,
            2 => append_varints(wire, &mut cur, &mut keys)?,
            3 => append_varints(wire, &mut cur, &mut vals)?,
            8 => append_varints(wire, &mut cur, &mut refs)?,
            _ => cur.skip(wire)?,
        }
    }
    let paint = matches_idx(&fill.rules.way, &block.strings, &keys, &vals);
    let needed = fill.water.contains(&id);
    if !paint && !needed {
        return Ok(());
    }
    let refs = delta_ids(&refs);
    let mut xy = Vec::with_capacity(refs.len() * 8);
    let mut min_x = i32::MAX;
    let mut min_y = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_y = i32::MIN;
    for ref_id in refs {
        let Some((x, y)) = fill.locs.get(ref_id) else {
            return Ok(());
        };
        if x < min_x {
            min_x = x;
        }
        if y < min_y {
            min_y = y;
        }
        if x > max_x {
            max_x = x;
        }
        if y > max_y {
            max_y = y;
        }
        xy.extend_from_slice(&x.to_le_bytes());
        xy.extend_from_slice(&y.to_le_bytes());
    }
    if xy.len() < 16 {
        return Ok(());
    }
    if needed {
        fill.shores.insert(
            id,
            Shore {
                min_x,
                min_y,
                max_x,
                max_y,
                xy: xy.clone(),
            },
        );
    }
    if paint {
        let tags = json_tags(&block.strings, &keys, &vals);
        fill.grid.add_way(id, min_x, min_y, max_x, max_y, &xy, &tags);
    }
    Ok(())
}

fn one_relation(data: &[u8], block: &Block, fill: &mut Fill) -> Result<(), Error> {
    let mut cur = Cursor::new(data);
    let mut id = 0i64;
    let mut keys = Vec::new();
    let mut vals = Vec::new();
    let mut roles = Vec::new();
    let mut mems = Vec::new();
    let mut types = Vec::new();
    while let Some((field, wire)) = cur.next()? {
        match field {
            1 if wire == 0 => id = cur.varint()? as i64,
            2 => append_varints(wire, &mut cur, &mut keys)?,
            3 => append_varints(wire, &mut cur, &mut vals)?,
            8 => append_varints(wire, &mut cur, &mut roles)?,
            9 => append_varints(wire, &mut cur, &mut mems)?,
            10 => append_varints(wire, &mut cur, &mut types)?,
            _ => cur.skip(wire)?,
        }
    }
    let pairs = pairs(&block.strings, &keys, &vals);
    if !matches_pairs(&fill.rules.relation, &pairs) {
        return Ok(());
    }
    let kind = tag_get(&pairs, "type");
    if kind != Some("multipolygon") && kind != Some("boundary") {
        return Ok(());
    }
    let water = tag_get(&pairs, "natural") == Some("water");
    let mems = delta_ids(&mems);
    let n = mems.len().min(types.len()).min(roles.len().max(mems.len()));
    let mut way_ids = Vec::new();
    let mut inners = Vec::new();
    let count = mems.len().min(types.len());
    for i in 0..count {
        if types[i] != 1 {
            continue;
        }
        let role = roles.get(i).copied().unwrap_or(0);
        let text = block.strings.get(role as usize).map(String::as_str).unwrap_or("");
        way_ids.push(mems[i]);
        inners.push(is_inner(text));
    }
    let _ = n;
    let bounds = if water {
        union_shores(&way_ids, fill.shores)
    } else {
        None
    };
    let (min_x, min_y, max_x, max_y, known) = match bounds {
        Some((a, b, c, d)) => (a, b, c, d, true),
        None => (0, 0, 0, 0, false),
    };
    let tags = json_pairs(&pairs);
    let mut rec = Vec::new();
    rec.push(3);
    rec.extend_from_slice(&id.to_le_bytes());
    rec.extend_from_slice(&min_x.to_le_bytes());
    rec.extend_from_slice(&min_y.to_le_bytes());
    rec.extend_from_slice(&max_x.to_le_bytes());
    rec.extend_from_slice(&max_y.to_le_bytes());
    let flags = (if water { 1 } else { 0 }) | (if known { 2 } else { 0 });
    rec.push(flags);
    rec.extend_from_slice(&(tags.len() as u32).to_le_bytes());
    rec.extend_from_slice(&tags);
    rec.extend_from_slice(&(way_ids.len() as u32).to_le_bytes());
    for (index, wid) in way_ids.iter().enumerate() {
        rec.push(if inners[index] { 1 } else { 0 });
        rec.extend_from_slice(&wid.to_le_bytes());
        let xy = if water {
            fill.shores.get(wid).map(|shore| shore.xy.as_slice()).unwrap_or(&[])
        } else {
            &[]
        };
        rec.extend_from_slice(&(xy.len() as u32).to_le_bytes());
        rec.extend_from_slice(xy);
    }
    fill.grid.relations.extend_from_slice(&rec);
    Ok(())
}

fn union_shores(ids: &[i64], shores: &HashMap<i64, Shore>) -> Option<(i32, i32, i32, i32)> {
    if ids.is_empty() {
        return None;
    }
    let mut min_x = i32::MAX;
    let mut min_y = i32::MAX;
    let mut max_x = i32::MIN;
    let mut max_y = i32::MIN;
    for id in ids {
        let shore = shores.get(id)?;
        min_x = min_x.min(shore.min_x);
        min_y = min_y.min(shore.min_y);
        max_x = max_x.max(shore.max_x);
        max_y = max_y.max(shore.max_y);
    }
    Some((min_x, min_y, max_x, max_y))
}

fn is_inner(role: &str) -> bool {
    let trimmed = role.trim();
    trimmed.len() == 5 && trimmed.eq_ignore_ascii_case("inner")
}

fn append_varints(wire: u32, cur: &mut Cursor, out: &mut Vec<u64>) -> Result<(), Error> {
    let mut part = Vec::new();
    proto::take_varints(wire, cur, &mut part)?;
    out.extend(part);
    Ok(())
}

fn delta_ids(raw: &[u64]) -> Vec<i64> {
    let mut acc = 0i64;
    let mut out = Vec::with_capacity(raw.len());
    for &item in raw {
        acc = acc.wrapping_add(proto::zigzag(item));
        out.push(acc);
    }
    out
}

fn coord(offset: i64, granularity: i64, value: i64) -> Option<i32> {
    let nano = offset.checked_add(granularity.checked_mul(value)?)?;
    let scaled = nano / 100;
    if scaled <= i32::MIN as i64 || scaled > i32::MAX as i64 {
        return None;
    }
    Some(scaled as i32)
}

impl Locations {
    fn add(&mut self, id: i64, x: i32, y: i32) {
        let packed = ((x as u32 as u64) << 32) | (y as u32 as u64);
        if self.frozen {
            self.extra.insert(id, packed);
        } else {
            self.rows.push((id, packed));
        }
    }

    fn get(&mut self, id: i64) -> Option<(i32, i32)> {
        if let Some(packed) = self.extra.get(&id) {
            return Some(unpack(*packed));
        }
        if !self.frozen {
            self.rows.sort_unstable_by_key(|row| row.0);
            self.frozen = true;
        }
        let index = self.rows.binary_search_by_key(&id, |row| row.0).ok()?;
        Some(unpack(self.rows[index].1))
    }
}

fn unpack(packed: u64) -> (i32, i32) {
    ((packed >> 32) as u32 as i32, packed as u32 as i32)
}

impl GridOut {
    fn add_node(&mut self, id: i64, x: i32, y: i32, tags: &[u8]) {
        let mut rec = Vec::with_capacity(1 + 20 + tags.len());
        rec.push(2);
        rec.extend_from_slice(&id.to_le_bytes());
        rec.extend_from_slice(&x.to_le_bytes());
        rec.extend_from_slice(&y.to_le_bytes());
        rec.extend_from_slice(&(tags.len() as u32).to_le_bytes());
        rec.extend_from_slice(tags);
        let key = self.cell_of(y, x);
        self.cells.entry(key).or_default().extend_from_slice(&rec);
    }

    fn add_way(&mut self, id: i64, min_x: i32, min_y: i32, max_x: i32, max_y: i32, xy: &[u8], tags: &[u8]) {
        let mut rec = Vec::with_capacity(1 + 28 + xy.len() + tags.len());
        rec.push(1);
        rec.extend_from_slice(&id.to_le_bytes());
        rec.extend_from_slice(&min_x.to_le_bytes());
        rec.extend_from_slice(&min_y.to_le_bytes());
        rec.extend_from_slice(&max_x.to_le_bytes());
        rec.extend_from_slice(&max_y.to_le_bytes());
        rec.extend_from_slice(&(xy.len() as u32).to_le_bytes());
        rec.extend_from_slice(xy);
        rec.extend_from_slice(&(tags.len() as u32).to_le_bytes());
        rec.extend_from_slice(tags);
        match self.cells_touched(min_x, min_y, max_x, max_y) {
            None => self.wide.extend_from_slice(&rec),
            Some(cells) => {
                for key in cells {
                    self.cells.entry(key).or_default().extend_from_slice(&rec);
                }
            }
        }
    }

    fn cell_of(&self, y: i32, x: i32) -> (i32, i32) {
        let lat = y as f64 / SCALE;
        let lon = x as f64 / SCALE;
        let mut row = ((lat - self.south) / self.deg) as i32;
        let mut col = ((lon - self.west) / self.deg) as i32;
        if row < 0 {
            row = 0;
        } else if row >= self.rows {
            row = self.rows - 1;
        }
        if col < 0 {
            col = 0;
        } else if col >= self.cols {
            col = self.cols - 1;
        }
        (row, col)
    }

    fn cells_touched(&self, min_x: i32, min_y: i32, max_x: i32, max_y: i32) -> Option<Vec<(i32, i32)>> {
        let (mut r0, mut c0) = self.cell_of(min_y, min_x);
        let (mut r1, mut c1) = self.cell_of(max_y, max_x);
        if r0 > r1 {
            std::mem::swap(&mut r0, &mut r1);
        }
        if c0 > c1 {
            std::mem::swap(&mut c0, &mut c1);
        }
        let across = (r1 as i64 - r0 as i64 + 1) * (c1 as i64 - c0 as i64 + 1);
        if across > self.wide_limit as i64 {
            return None;
        }
        let mut out = Vec::with_capacity(across as usize);
        for row in r0..=r1 {
            for col in c0..=c1 {
                out.push((row, col));
            }
        }
        Some(out)
    }
}

fn write_span(
    dir: &Path,
    grid: &GridOut,
    span_version: u32,
    filters_version: u32,
    mtime_ms: i64,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> Result<(), Error> {
    for ((row, col), blob) in &grid.cells {
        write_bin(&dir.join(format!("r{row}c{col}.bin")), blob)?;
    }
    if !grid.wide.is_empty() {
        write_bin(&dir.join("wide.bin"), &grid.wide)?;
    }
    if !grid.relations.is_empty() {
        write_bin(&dir.join("relations.bin"), &grid.relations)?;
    }
    let meta = format!(
        "{{\"span\": {span_version}, \"filters\": {filters_version}, \"mtime_ms\": {mtime_ms}, \"box\": [{}, {}, {}, {}], \"deg\": {}, \"rows\": {}, \"cols\": {}}}",
        json_num(south),
        json_num(west),
        json_num(north),
        json_num(east),
        json_num(grid.deg),
        grid.rows,
        grid.cols,
    );
    fs::write(dir.join("meta.json"), meta).map_err(|_| Error::Io)?;
    let mut complete = fs::File::create(dir.join("complete")).map_err(|_| Error::Io)?;
    complete.write_all(b"ok\n").map_err(|_| Error::Io)?;
    Ok(())
}

fn write_bin(path: &Path, blob: &[u8]) -> Result<(), Error> {
    let mut file = fs::File::create(path).map_err(|_| Error::Io)?;
    file.write_all(b"KNXS").map_err(|_| Error::Io)?;
    file.write_all(blob).map_err(|_| Error::Io)?;
    Ok(())
}

fn json_num(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e15 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

fn grid_shape(south: f64, west: f64, north: f64, east: f64, deg: f64) -> (i32, i32) {
    let rows = ((north - south).max(1e-6) / deg).ceil() as i32;
    let cols = ((east - west).max(1e-6) / deg).ceil() as i32;
    (rows.max(1), cols.max(1))
}

fn parse_rules(raw: &[u8]) -> KindRules {
    let mut rules = KindRules {
        node: Rules { any: HashSet::new(), values: HashMap::new() },
        way: Rules { any: HashSet::new(), values: HashMap::new() },
        relation: Rules { any: HashSet::new(), values: HashMap::new() },
    };
    let Ok(text) = std::str::from_utf8(raw) else {
        return rules;
    };
    for line in text.split('\n') {
        let line = line.trim().trim_end_matches('\r');
        if line.len() < 3 || line.as_bytes()[1] != b'/' {
            continue;
        }
        let slot = match line.as_bytes()[0] {
            b'n' => &mut rules.node,
            b'w' => &mut rules.way,
            b'r' => &mut rules.relation,
            _ => continue,
        };
        let rest = &line[2..];
        if let Some((key, value)) = rest.split_once('=') {
            if slot.any.contains(key) {
                continue;
            }
            slot.values.entry(key.to_string()).or_default().insert(value.to_string());
        } else {
            slot.values.remove(rest);
            slot.any.insert(rest.to_string());
        }
    }
    rules
}

fn node_rule_text(rules: &Rules) -> Vec<u8> {
    let mut out = Vec::new();
    for key in &rules.any {
        out.extend_from_slice(key.as_bytes());
        out.push(b'\n');
    }
    for (key, values) in &rules.values {
        for value in values {
            out.extend_from_slice(key.as_bytes());
            out.push(b'=');
            out.extend_from_slice(value.as_bytes());
            out.push(b'\n');
        }
    }
    out
}

fn matches_idx(rules: &Rules, table: &[String], keys: &[u64], vals: &[u64]) -> bool {
    if rules.any.is_empty() && rules.values.is_empty() {
        return false;
    }
    for (&key_i, &val_i) in keys.iter().zip(vals.iter()) {
        let Some(key) = table.get(key_i as usize) else {
            continue;
        };
        if rules.any.contains(key) {
            return true;
        }
        if let Some(wanted) = rules.values.get(key) {
            if let Some(value) = table.get(val_i as usize) {
                if wanted.contains(value) {
                    return true;
                }
            }
        }
    }
    false
}

fn matches_pairs(rules: &Rules, pairs: &[(String, String)]) -> bool {
    if rules.any.is_empty() && rules.values.is_empty() {
        return false;
    }
    for (key, value) in pairs {
        if rules.any.contains(key) {
            return true;
        }
        if let Some(wanted) = rules.values.get(key) {
            if wanted.contains(value) {
                return true;
            }
        }
    }
    false
}

fn pairs(table: &[String], keys: &[u64], vals: &[u64]) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for (&key_i, &val_i) in keys.iter().zip(vals.iter()) {
        let Some(key) = table.get(key_i as usize) else {
            continue;
        };
        let Some(value) = table.get(val_i as usize) else {
            continue;
        };
        if key.is_empty() {
            continue;
        }
        if let Some(slot) = out.iter_mut().find(|(have, _)| have == key) {
            slot.1 = value.clone();
        } else {
            out.push((key.clone(), value.clone()));
        }
    }
    out
}

fn tag_get<'a>(pairs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    pairs.iter().rev().find(|(have, _)| have == key).map(|(_, value)| value.as_str())
}

fn json_tags(table: &[String], keys: &[u64], vals: &[u64]) -> Vec<u8> {
    json_pairs(&pairs(table, keys, vals))
}

fn json_pairs(pairs: &[(String, String)]) -> Vec<u8> {
    let mut out = Vec::new();
    out.push(b'{');
    for (index, (key, value)) in pairs.iter().enumerate() {
        if index > 0 {
            out.push(b',');
        }
        push_json(&mut out, key);
        out.push(b':');
        push_json(&mut out, value);
    }
    out.push(b'}');
    out
}

fn push_json(out: &mut Vec<u8>, text: &str) {
    out.push(b'"');
    for ch in text.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\u{0008}' => out.extend_from_slice(b"\\b"),
            '\u{000c}' => out.extend_from_slice(b"\\f"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            ch if (ch as u32) < 0x20 => {
                let escaped = format!("\\u{code:04x}", code = ch as u32);
                out.extend_from_slice(escaped.as_bytes());
            }
            ch => {
                let mut buf = [0u8; 4];
                out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
            }
        }
    }
    out.push(b'"');
}
