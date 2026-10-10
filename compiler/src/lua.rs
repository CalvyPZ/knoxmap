// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// LuaWriter and LuaTableWriter, plus the worldmap and streets exports.
// Cells are visited y then x, after every lot cell has been written.

//! Spawn points, objects, room tones, world map, streets, and annotations.

use std::path::{Path, PathBuf};

use crate::project::{self, Geometry, Object, Project};

pub struct LuaFiles {
    pub spawn_points: PathBuf,
    pub objects: PathBuf,
    pub room_tones: PathBuf,
    pub worldmap: PathBuf,
    pub streets: PathBuf,
    pub annotations: PathBuf,
}

pub fn write_all(project: &Project) -> Result<LuaFiles, String> {
    let files = LuaFiles {
        spawn_points: out_path(project, &project.spawn_points_file, "spawnpoints.lua"),
        objects: out_path(project, &project.world_objects_file, "objects.lua"),
        room_tones: out_path(project, &project.room_tones_file, "roomtones.lua"),
        worldmap: project.dir.join("worldmap.xml"),
        streets: project.dir.join("streets.xml"),
        annotations: project.dir.join("worldmap-annotations.lua"),
    };
    std::fs::write(&files.spawn_points, spawn_points(project)).map_err(|e| e.to_string())?;
    std::fs::write(&files.objects, objects_lua(project)).map_err(|e| e.to_string())?;
    std::fs::write(&files.room_tones, room_tones(project)).map_err(|e| e.to_string())?;
    std::fs::write(&files.worldmap, worldmap_xml(project)).map_err(|e| e.to_string())?;
    std::fs::write(&files.streets, streets_xml(project)).map_err(|e| e.to_string())?;
    std::fs::write(&files.annotations, annotations_lua(project)).map_err(|e| e.to_string())?;
    Ok(files)
}

fn out_path(project: &Project, stored: &str, fallback: &str) -> PathBuf {
    if stored.is_empty() {
        project.dir.join(fallback)
    } else {
        let path = project::native_path(&project.dir, stored);
        if path.is_absolute() || path.exists() || stored.contains('/') || stored.contains('\\') {
            if path.is_absolute() { path } else { project.dir.join(stored) }
        } else {
            project.dir.join(stored)
        }
    }
}

fn cell_size(project: &Project) -> i32 {
    if project.legacy_300 { 300 } else { 256 }
}

fn cells_yx(project: &Project) -> Vec<(i32, i32)> {
    let mut cells: Vec<(i32, i32)> = project.cells.keys().copied().collect();
    cells.sort_by(|a, b| a.1.cmp(&b.1).then(a.0.cmp(&b.0)));
    cells
}

fn spawn_points(project: &Project) -> String {
    let mut by_job: Vec<(String, Vec<(i32, i32, i32)>)> = Vec::new();
    let size = cell_size(project);
    for (x, y) in cells_yx(project) {
        let Some(cell) = project.cells.get(&(x, y)) else { continue };
        for obj in &cell.objects {
            if !is_spawn(obj) {
                continue;
            }
            let ax = (x + project.world_origin_x) * size + obj.x.round() as i32;
            let ay = (y + project.world_origin_y) * size + obj.y.round() as i32;
            for job in professions(obj) {
                if let Some(slot) = by_job.iter_mut().find(|(name, _)| name == &job) {
                    slot.1.push((ax, ay, obj.level));
                } else {
                    by_job.push((job, vec![(ax, ay, obj.level)]));
                }
            }
        }
    }
    by_job.sort_by(|a, b| a.0.cmp(&b.0));
    let mut w = Lua::new();
    w.raw.push_str("function SpawnPoints()\n");
    w.start_return();
    for (job, points) in &by_job {
        w.start_named(job);
        for (x, y, z) in points {
            w.start_anon();
            w.suppress = true;
            w.key_int("posX", *x);
            w.key_int("posY", *y);
            w.key_int("posZ", *z);
            for prop in &project_props_of(project, points_object(project, *x, *y)) {
                let _ = prop;
            }
            w.end_table();
            w.suppress = false;
        }
        w.end_table();
    }
    w.end_table();
    w.raw.push_str("\nend\n");
    let _ = project;
    w.raw
}

fn points_object<'a>(project: &'a Project, _x: i32, _y: i32) -> Option<&'a Object> {
    let _ = project;
    None
}

fn project_props_of<'a>(project: &'a Project, obj: Option<&'a Object>) -> Vec<(&'a str, &'a str)> {
    let _ = project;
    match obj {
        Some(obj) => obj.properties.iter().map(|p| (p.name.as_str(), p.value.as_str())).collect(),
        None => Vec::new(),
    }
}

fn is_spawn(obj: &Object) -> bool {
    obj.type_name.eq_ignore_ascii_case("SpawnPoint")
        || obj.properties.iter().any(|p| p.name == "Professions")
}

fn professions(obj: &Object) -> Vec<String> {
    let raw = obj.properties.iter().find(|p| p.name == "Professions").map(|p| p.value.as_str()).unwrap_or("unemployed");
    let mut jobs = Vec::new();
    for part in raw.split(|c| c == ',' || c == ';') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        let name = if part.eq_ignore_ascii_case("all") { "unemployed" } else { part };
        if !jobs.iter().any(|j| j == name) {
            jobs.push(name.to_string());
        }
    }
    if jobs.is_empty() {
        jobs.push("unemployed".into());
    }
    jobs
}

fn objects_lua(project: &Project) -> String {
    let mut w = Lua::new();
    w.start_named("objects");
    let size = cell_size(project);
    for (x, y) in cells_yx(project) {
        let Some(cell) = project.cells.get(&(x, y)) else { continue };
        for obj in &cell.objects {
            write_object(&mut w, project, x, y, size, obj);
        }
    }
    w.end_table();
    w.finish();
    w.raw
}

fn write_object(w: &mut Lua, project: &Project, cx: i32, cy: i32, size: i32, obj: &Object) {
    let _ = project;
    w.start_anon();
    w.suppress = true;
    w.key_str("name", &obj.name);
    w.key_str("type", &obj.type_name);
    match obj.geometry {
        Geometry::Rect => {
            let ax = (cx + project.world_origin_x) as f64 * size as f64 + obj.x;
            let ay = (cy + project.world_origin_y) as f64 * size as f64 + obj.y;
            w.key_num("x", ax);
            w.key_num("y", ay);
            w.key_int("z", obj.level);
            w.key_num("width", obj.width);
            w.key_num("height", obj.height);
        }
        Geometry::Point | Geometry::Polygon | Geometry::Polyline => {
            w.key_int("z", obj.level);
            let kind = match obj.geometry {
                Geometry::Point => "point",
                Geometry::Polygon => "polygon",
                Geometry::Polyline => "polyline",
                Geometry::Rect => "rect",
            };
            w.key_str("geometry", kind);
            if matches!(obj.geometry, Geometry::Polyline) && obj.line_width > 0.0 {
                w.key_num("lineWidth", obj.line_width);
            }
            let mut inner = Lua::new();
            inner.suppress = true;
            inner.start_anon();
            for point in &obj.points {
                let px = (cx + project.world_origin_x) as f64 * size as f64 + point.x;
                let py = (cy + project.world_origin_y) as f64 * size as f64 + point.y;
                inner.value_num(px);
                inner.value_num(py);
            }
            inner.end_table();
            w.key_raw("points", inner.raw.trim());
        }
    }
    let props: Vec<_> = obj.properties.iter().filter(|p| p.name != "Professions" || !is_spawn(obj)).collect();
    if !props.is_empty() {
        let mut probe = Lua::new();
        probe.suppress = true;
        probe.start_named("properties");
        for prop in &props {
            probe.key_str(&prop.name, &prop.value);
        }
        probe.end_table();
        let inline = probe.raw.len() <= 64;
        let was = w.suppress;
        w.suppress = inline;
        w.start_named("properties");
        for prop in &props {
            w.key_str(&prop.name, &prop.value);
        }
        w.end_table();
        w.suppress = was;
    }
    w.end_table();
    w.suppress = false;
}

fn room_tones(project: &Project) -> String {
    let mut w = Lua::new();
    w.start_named("objects");
    let size = cell_size(project);
    for (x, y) in cells_yx(project) {
        let Some(cell) = project.cells.get(&(x, y)) else { continue };
        for lot in &cell.lots {
            let path = project::native_path(&project.dir, &lot.map);
            let Some(tone) = map_property(&path, "RoomTone") else { continue };
            w.start_anon();
            w.suppress = true;
            w.key_str("name", &tone);
            w.key_int("x", (x + project.world_origin_x) * size + lot.x);
            w.key_int("y", (y + project.world_origin_y) * size + lot.y);
            w.key_int("z", lot.level);
            w.end_table();
            w.suppress = false;
        }
    }
    w.end_table();
    w.finish();
    w.raw
}

fn map_property(path: &Path, name: &str) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let needle = format!("name=\"{name}\"");
    let at = text.find(&needle)?;
    let value = text[at..].find("value=\"")?;
    let start = at + value + "value=\"".len();
    let end = text[start..].find('"')? + start;
    Some(text[start..end].to_string())
}

fn worldmap_xml(project: &Project) -> String {
    // InGameMap features are not stored on the KnoxMap cells this compiler
    // loads. The document is the WorldEd root, and cells are omitted while
    // they have no features, which is what writeCell does for an empty list.
    let size = cell_size(project);
    format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<world version=\"1.0\" cellSize=\"{size}\"/>\n")
}

fn streets_xml(project: &Project) -> String {
    // Street names live in the streets dock, not in the .pzw object list.
    // An empty <streets version="1"> is the file that dock writes when it
    // has nothing to save. Default Qt indent is 4 spaces.
    let _ = project;
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<streets version=\"1\"/>\n".to_string()
}

fn annotations_lua(project: &Project) -> String {
    let mut out = String::from("return function(mapUI)\n\tlocal mapAPI = mapUI.javaObject:getAPIv3()\n\tlocal symbolsAPI = mapAPI:getSymbolsAPIv2()\n\tlocal symbol\n");
    for (x, y) in cells_yx(project) {
        let Some(cell) = project.cells.get(&(x, y)) else { continue };
        for obj in &cell.objects {
            if obj.type_name != "Annotation" {
                continue;
            }
            let text = prop(obj, "text");
            let style = prop(obj, "style");
            let translated = prop(obj, "translated") == "true";
            let kind = if translated { "TranslatedText" } else { "UntranslatedText" };
            out.push_str(&format!(
                "\tsymbol = symbolsAPI:add{kind}(\"{}\", \"{}\", {}, {})\n",
                lua_escape(&text),
                lua_escape(&style),
                qt_number(obj.x),
                qt_number(obj.y)
            ));
            out.push_str("\tsymbol:setRGBA(0.000, 0.000, 0.000, 1.000)\n");
            out.push_str("\tsymbol:setScale(1.000)\n");
            out.push_str("\tsymbol:setAnchor(0.00, 0.00)\n");
            out.push_str("\tsymbol:setRotation(0.0)\n");
            out.push_str("\tsymbol:setMatchPerspective(true)\n");
            out.push_str("\tsymbol:setApplyZoom(true)\n");
            out.push_str("\tsymbol:setMinZoom(0.00)\n");
            out.push_str("\tsymbol:setMaxZoom(24.00)\n");
            out.push_str("\tsymbol:setUserDefined(false)\n\n");
            let _ = (x, y);
        }
    }
    out.push_str("end\n");
    out
}

fn prop(obj: &Object, name: &str) -> String {
    obj.properties.iter().find(|p| p.name == name).map(|p| p.value.clone()).unwrap_or_default()
}

fn lua_escape(text: &str) -> String {
    text.replace('\\', "\\\\").replace('"', "\\\"")
}

/// `QByteArray::number(double)` is format `g` with 6 significant digits.
/// Whole values are written with no decimal point.
fn qt_number(value: f64) -> String {
    if value.is_finite() && value.fract() == 0.0 && value.abs() < 1e15 {
        return format!("{}", value as i64);
    }
    let mut text = format!("{value:.6}");
    if text.contains('.') {
        while text.ends_with('0') {
            text.pop();
        }
        if text.ends_with('.') {
            text.pop();
        }
    }
    text
}

struct Lua {
    raw: String,
    indent: i32,
    value_written: bool,
    new_line: bool,
    suppress: bool,
}

impl Lua {
    fn new() -> Self {
        Self { raw: String::new(), indent: 0, value_written: false, new_line: true, suppress: false }
    }

    fn finish(&mut self) {
        self.raw.push('\n');
    }

    fn start_return(&mut self) {
        self.prepare_line();
        self.raw.push_str("return {");
        self.indent += 1;
        self.new_line = false;
        self.value_written = false;
    }

    fn start_named(&mut self, name: &str) {
        self.prepare_line();
        self.raw.push_str(name);
        self.raw.push_str(" = {");
        self.indent += 1;
        self.new_line = false;
        self.value_written = false;
    }

    fn start_anon(&mut self) {
        self.prepare_line();
        self.raw.push('{');
        self.indent += 1;
        self.new_line = false;
        self.value_written = false;
    }

    fn end_table(&mut self) {
        self.indent -= 1;
        if self.value_written {
            self.newline();
        }
        self.raw.push('}');
        self.new_line = false;
        self.value_written = true;
    }

    fn key_int(&mut self, key: &str, value: i32) {
        self.key_raw(key, &value.to_string());
    }

    fn key_num(&mut self, key: &str, value: f64) {
        self.key_raw(key, &qt_number(value));
    }

    fn key_str(&mut self, key: &str, value: &str) {
        self.prepare_line();
        self.raw.push_str(key);
        self.raw.push_str(" = \"");
        self.raw.push_str(&lua_escape(value));
        self.raw.push('"');
        self.new_line = false;
        self.value_written = true;
    }

    fn key_raw(&mut self, key: &str, value: &str) {
        self.prepare_line();
        self.raw.push_str(key);
        self.raw.push_str(" = ");
        self.raw.push_str(value);
        self.new_line = false;
        self.value_written = true;
    }

    fn value_num(&mut self, value: f64) {
        self.prepare_value();
        self.raw.push_str(&qt_number(value));
        self.new_line = false;
        self.value_written = true;
    }

    fn prepare_line(&mut self) {
        if self.value_written {
            self.raw.push(',');
            self.value_written = false;
        }
        self.newline();
    }

    fn prepare_value(&mut self) {
        if !self.value_written {
            self.newline();
        } else {
            self.raw.push_str(", ");
        }
    }

    fn newline(&mut self) {
        if self.new_line {
            return;
        }
        if self.suppress {
            self.raw.push(' ');
        } else {
            self.raw.push('\n');
            for _ in 0..self.indent {
                self.raw.push_str("  ");
            }
        }
        self.new_line = true;
    }
}
