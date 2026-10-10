//! GeoJSON sequence lines, in the shape `_feature_line` and `_ClipWriter` write.

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;

use crate::err::Error;

const SCALE: f64 = 10_000_000.0;

pub struct Files {
    folder: std::path::PathBuf,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    rows: i32,
    cols: i32,
    open: std::collections::HashMap<String, File>,
}

impl Files {
    pub fn new(folder: &Path, south: f64, west: f64, north: f64, east: f64, rows: i32, cols: i32) -> Self {
        Self {
            folder: folder.to_path_buf(),
            south,
            west,
            north,
            east,
            rows: rows.max(1),
            cols: cols.max(1),
            open: std::collections::HashMap::new(),
        }
    }

    pub fn add(&mut self, line: &str, xy: &[u8], cover: bool) -> Result<(), Error> {
        if !cover {
            let mut row_lo = i32::MAX;
            let mut row_hi = i32::MIN;
            let mut col_lo = i32::MAX;
            let mut col_hi = i32::MIN;
            let mut seen = false;
            let mut i = 0;
            while i + 8 <= xy.len() {
                let x = i32::from_le_bytes(xy[i..i + 4].try_into().unwrap());
                let y = i32::from_le_bytes(xy[i + 4..i + 8].try_into().unwrap());
                let row = self.bucket(y as f64 / SCALE);
                let col = self.slice(x as f64 / SCALE);
                row_lo = row_lo.min(row);
                row_hi = row_hi.max(row);
                col_lo = col_lo.min(col);
                col_hi = col_hi.max(col);
                seen = true;
                i += 8;
            }
            if seen {
                let n = (row_hi - row_lo + 1) as i64 * (col_hi - col_lo + 1) as i64;
                if n <= 32 {
                    for row in row_lo..=row_hi {
                        for col in col_lo..=col_hi {
                            self.write(&format!("r{row}c{col}.jsonl"), line)?;
                        }
                    }
                    return Ok(());
                }
            }
        }
        self.write("cover.jsonl", line)
    }

    pub fn finish(&mut self, filters: u32, clip: u32) -> Result<(), Error> {
        self.open.clear();
        let meta = format!(
            "{{\"box\": [{}, {}, {}, {}], \"rows\": {}, \"cols\": {}, \"filters\": {}, \"clip\": {}}}\n",
            json_num(self.south),
            json_num(self.west),
            json_num(self.north),
            json_num(self.east),
            self.rows,
            self.cols,
            filters,
            clip,
        );
        // Python's json.dump does not add a trailing newline. The reader only
        // parses the object, so the newline is harmless, but match the keys
        // and the spaces.
        let meta = meta.trim_end_matches('\n');
        std::fs::write(self.folder.join("meta.json"), meta).map_err(|_| Error::Io)?;
        std::fs::write(self.folder.join("complete"), b"ok\n").map_err(|_| Error::Io)?;
        Ok(())
    }

    fn write(&mut self, name: &str, line: &str) -> Result<(), Error> {
        if !self.open.contains_key(name) {
            let file = OpenOptions::new()
                .create(true)
                .append(true)
                .open(self.folder.join(name))
                .map_err(|_| Error::Io)?;
            self.open.insert(name.to_string(), file);
        }
        let file = self.open.get_mut(name).unwrap();
        file.write_all(line.as_bytes()).map_err(|_| Error::Io)
    }

    fn bucket(&self, lat: f64) -> i32 {
        if self.rows <= 1 || self.north <= self.south {
            return 0;
        }
        let mut i = ((lat - self.south) / (self.north - self.south) * self.rows as f64) as i32;
        if i < 0 {
            i = 0;
        } else if i >= self.rows {
            i = self.rows - 1;
        }
        i
    }

    fn slice(&self, lon: f64) -> i32 {
        if self.cols <= 1 || self.east <= self.west {
            return 0;
        }
        let mut i = ((lon - self.west) / (self.east - self.west) * self.cols as f64) as i32;
        if i < 0 {
            i = 0;
        } else if i >= self.cols {
            i = self.cols - 1;
        }
        i
    }
}

pub fn feature_line(kind: &str, id: i64, tags: &[u8], geometry: &str) -> String {
    let mut props = String::new();
    props.push('{');
    if tags.len() >= 2 && tags[0] == b'{' && tags[tags.len() - 1] == b'}' {
        let inner = &tags[1..tags.len() - 1];
        if inner.iter().any(|byte| !byte.is_ascii_whitespace()) {
            if let Ok(text) = std::str::from_utf8(inner) {
                props.push_str(text);
                props.push(',');
            }
        }
    }
    props.push_str(&format!("\"@id\":{id},\"@type\":\"{kind}\""));
    props.push('}');
    format!("\u{1e}{{\"type\":\"Feature\",\"geometry\":{geometry},\"properties\":{props}}}\n")
}

pub fn line_geometry(xy: &[u8]) -> String {
    let mut out = String::from("{\"type\":\"LineString\",\"coordinates\":[");
    push_pairs(&mut out, xy);
    out.push_str("]}");
    out
}

pub fn polygon_geometry(rings: &[Vec<u8>]) -> String {
    let mut out = String::from("{\"type\":\"Polygon\",\"coordinates\":[");
    for (index, ring) in rings.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push('[');
        push_pairs(&mut out, ring);
        out.push(']');
    }
    out.push_str("]}");
    out
}

pub fn multi_geometry(polygons: &[Vec<Vec<u8>>]) -> String {
    let mut out = String::from("{\"type\":\"MultiPolygon\",\"coordinates\":[");
    for (index, rings) in polygons.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push('[');
        for (ring_i, ring) in rings.iter().enumerate() {
            if ring_i > 0 {
                out.push(',');
            }
            out.push('[');
            push_pairs(&mut out, ring);
            out.push(']');
        }
        out.push(']');
    }
    out.push_str("]}");
    out
}

pub fn point_geometry(x: i32, y: i32) -> String {
    let mut out = String::from("{\"type\":\"Point\",\"coordinates\":[");
    push_deg(&mut out, x);
    out.push(',');
    push_deg(&mut out, y);
    out.push_str("]}");
    out
}

pub fn push_pairs(out: &mut String, xy: &[u8]) {
    let mut i = 0;
    let mut first = true;
    while i + 8 <= xy.len() {
        if !first {
            out.push(',');
        }
        first = false;
        let x = i32::from_le_bytes(xy[i..i + 4].try_into().unwrap());
        let y = i32::from_le_bytes(xy[i + 4..i + 8].try_into().unwrap());
        out.push('[');
        push_deg(out, x);
        out.push(',');
        push_deg(out, y);
        out.push(']');
        i += 8;
    }
}

fn push_deg(out: &mut String, v: i32) {
    let n = v as i64;
    let neg = n < 0;
    let u = n.unsigned_abs();
    if neg {
        out.push('-');
    }
    let whole = u / 10_000_000;
    let frac = u % 10_000_000;
    out.push_str(&whole.to_string());
    if frac != 0 {
        out.push('.');
        let mut digits = format!("{frac:07}");
        while digits.ends_with('0') {
            digits.pop();
        }
        out.push_str(&digits);
    }
}

fn json_num(v: f64) -> String {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        let text = format!("{v}");
        text
    }
}
