// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.

//! One compile: BMP to TMX, then lot cells, then the lua and xml files.
//! Cells may run on a pool. One cell stays on one thread, in WorldEd's order.
//! Lua and xml are written after the cells, walking cells y then x.

use std::io::Write;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

use crate::bmptotmx;
use crate::generate::{self, HoleFill};
use crate::lua;
use crate::project::{self, Project};
use crate::rules;
use crate::tiles::{self, TileCatalog};

#[derive(Clone, Debug)]
pub struct Options {
    pub tmx: bool,
    pub lots: bool,
    pub cells: Vec<(i32, i32, i32, i32)>,
    /// 0 means use the project's numberOfThreads.
    pub jobs: usize,
    pub resume: bool,
    pub copy_pixels: bool,
    pub jumbo_seed: Option<u64>,
    pub rules: Option<String>,
    pub blends: Option<String>,
    pub map_base: Option<String>,
    pub tile_defs: Option<String>,
    pub tilesets: Option<String>,
    /// Load the project and tile catalog, then wait for a line on stdin
    /// before converting or compiling cells.
    pub hold: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            tmx: true,
            lots: true,
            cells: Vec::new(),
            jobs: 0,
            resume: true,
            copy_pixels: true,
            jumbo_seed: None,
            rules: None,
            blends: None,
            map_base: None,
            tile_defs: None,
            tilesets: None,
            hold: false,
        }
    }
}

static PROGRESS_LOCK: Mutex<()> = Mutex::new(());

pub fn progress(phase: &str, done: usize, total: usize, skipped: bool, detail: &str) {
    let _hold = PROGRESS_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    println!(
        "{{\"phase\":\"{phase}\",\"done\":{done},\"total\":{total},\"skipped\":{skipped},\"detail\":\"{}\"}}",
        detail.replace('\\', "\\\\").replace('"', "\\\"")
    );
    let _ = std::io::stdout().flush();
}

/// Cell progress from the pool. The printed count is read while the line is
/// being written, so a slower thread cannot report an earlier count later.
fn report_cell(done: &AtomicUsize, total: usize, skipped: bool, detail: &str) {
    let _hold = PROGRESS_LOCK.lock().unwrap_or_else(|err| err.into_inner());
    let n = done.load(Ordering::Relaxed);
    println!(
        "{{\"phase\":\"lots\",\"done\":{n},\"total\":{total},\"skipped\":{skipped},\"detail\":\"{}\"}}",
        detail.replace('\\', "\\\\").replace('"', "\\\"")
    );
    let _ = std::io::stdout().flush();
}

pub fn compile(project_path: &Path, options: &Options) -> Result<usize, String> {
    let project = project::load_project(project_path)?;
    let book = load_book(&project, options)?;
    let catalog = if options.lots {
        Some(load_catalog(&project, options)?)
    } else {
        None
    };
    if options.hold {
        progress("ready", 1, 1, false, "loaded");
        let mut line = String::new();
        let _ = std::io::stdin().read_line(&mut line);
    }
    if options.tmx {
        progress("convert", 0, project.width as usize * project.height as usize, false, "bmp");
        let written = bmptotmx::convert_project(&project, &book, options.map_base.as_deref().map(Path::new), options.copy_pixels)?;
        progress("convert", written.len(), written.len(), false, "tmx");
    }
    let mut headers = 0usize;
    if options.lots {
        let catalog = catalog.unwrap_or_default();
        headers = generate_lots(&project, options, &catalog)?;
    }
    progress("lua", 0, 1, false, "lua");
    lua::write_all(&project)?;
    progress("lua", 1, 1, false, "lua");
    Ok(headers)
}

fn load_book(project: &Project, options: &Options) -> Result<rules::RuleBook, String> {
    let rules_path = options.rules.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| project.bmp.rules.clone());
    let blends_path = options.blends.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| project.bmp.blends.clone());
    let rules_path = project::native_path(&project.dir, &rules_path);
    let blends_path = project::native_path(&project.dir, &blends_path);
    if !rules_path.is_file() {
        return Ok(rules::RuleBook::default());
    }
    rules::load_book(&rules_path, &blends_path)
}

fn load_catalog(project: &Project, options: &Options) -> Result<TileCatalog, String> {
    let mut catalog = TileCatalog::default();
    if let Some(path) = &options.tilesets {
        let path = project::native_path(&project.dir, path);
        if path.is_file() {
            catalog = tiles::load_tilesets_txt(&path)?;
        }
    }
    let defs = options.tile_defs.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| project.tile_def_folder.clone());
    if !defs.is_empty() {
        let dir = project::native_path(&project.dir, &defs);
        tiles::load_tile_defs(&dir, &mut catalog)?;
    }
    Ok(catalog)
}

fn generate_lots(project: &Project, options: &Options, catalog: &TileCatalog) -> Result<usize, String> {
    let cells = project::lot_cells_in_world_order(project, &options.cells);
    let total = cells.len();
    progress("lots", 0, total, false, "cells");
    let lots_dir = if project.lots_dir.is_empty() {
        project.dir.join("lots")
    } else {
        project::native_path(&project.dir, &project.lots_dir)
    };
    std::fs::create_dir_all(&lots_dir).map_err(|err| err.to_string())?;
    let catalog = Arc::new(catalog.clone());
    let project = Arc::new(project.clone());
    let done = Arc::new(AtomicUsize::new(0));
    let failed = Arc::new(AtomicBool::new(false));
    let mut error = String::new();
    // The pool replaces WorldEd's batch of processes. Each job owns one cell
    // from start to finish. numberOfThreads in the project is the pool size
    // when --jobs is omitted.
    let jobs = if options.jobs == 0 {
        project.threads.max(1) as usize
    } else {
        options.jobs.max(1)
    };
    let cells = Arc::new(cells);
    thread::scope(|scope| {
        let next = Arc::new(AtomicUsize::new(0));
        for _ in 0..jobs {
            let catalog = Arc::clone(&catalog);
            let project = Arc::clone(&project);
            let done = Arc::clone(&done);
            let failed = Arc::clone(&failed);
            let cells = Arc::clone(&cells);
            let lots_dir = lots_dir.clone();
            let resume = options.resume;
            let seed = options.jumbo_seed;
            let next = Arc::clone(&next);
            scope.spawn(move || {
                loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= cells.len() {
                        break;
                    }
                    let (x, y) = cells[i];
                    if failed.load(Ordering::Relaxed) {
                        break;
                    }
                    let header = lots_dir.join(format!("{x}_{y}.lotheader"));
                    if resume && header.is_file() {
                        done.fetch_add(1, Ordering::Relaxed);
                        report_cell(&done, total, true, &format!("{x},{y}"));
                        continue;
                    }
                    match generate::generate_cell(&project, &catalog, x, y, &lots_dir, HoleFill::Report, seed) {
                        Ok(_) => {
                            done.fetch_add(1, Ordering::Relaxed);
                            report_cell(&done, total, false, &format!("{x},{y}"));
                        }
                        Err(err) => {
                            failed.store(true, Ordering::Relaxed);
                            eprintln!("cell {x},{y}: {err}");
                        }
                    }
                }
            });
        }
    });
    if failed.load(Ordering::Relaxed) {
        error = "one or more cells failed".into();
    }
    if !error.is_empty() {
        return Err(error);
    }
    Ok(done.load(Ordering::Relaxed))
}
