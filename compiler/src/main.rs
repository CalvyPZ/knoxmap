// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.

//! knox-compile: the GPL compile path, as its own process.

use std::path::PathBuf;
use std::process::ExitCode;

use knox_compile::compile::Options;
use knox_compile::lotbytes;

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--version" || a == "-V") {
        print!("{}", knox_compile::NOTICE);
        if !knox_compile::NOTICE.ends_with('\n') {
            println!();
        }
        return ExitCode::SUCCESS;
    }
    if args.first().map(String::as_str) == Some("diff") {
        if args.len() != 3 {
            eprintln!("usage: knox-compile diff <left> <right>");
            return ExitCode::from(2);
        }
        return match knox_compile::diffcmp::diff_paths(PathBuf::from(&args[1]).as_path(), PathBuf::from(&args[2]).as_path()) {
            Ok(()) => ExitCode::SUCCESS,
            Err(err) => {
                eprintln!("{err}");
                ExitCode::from(1)
            }
        };
    }
    if args.first().map(String::as_str) == Some("roundtrip") {
        return roundtrip(args.get(1).map(String::as_str).unwrap_or(""));
    }
    if args.is_empty() || args[0].starts_with('-') {
        eprintln!(
            "usage: knox-compile <project.pzw> [--tmx] [--lots] [--cells x0,y0,x1,y1] [--jobs N] [--hold] [--no-resume] [--no-copy-pixels] [--jumbo-seed N] [--rules PATH] [--blends PATH] [--map-base PATH] [--tilesets PATH] [--tile-defs PATH]"
        );
        return ExitCode::from(2);
    }
    let project = PathBuf::from(args.remove(0));
    let mut options = Options::default();
    let mut saw_stage = false;
    let mut i = 0;
    while i < args.len() {
        let arg = args[i].as_str();
        match arg {
            "--tmx" => {
                if !saw_stage {
                    options.tmx = false;
                    options.lots = false;
                    saw_stage = true;
                }
                options.tmx = true;
            }
            "--lots" => {
                if !saw_stage {
                    options.tmx = false;
                    options.lots = false;
                    saw_stage = true;
                }
                options.lots = true;
            }
            "--no-resume" => options.resume = false,
            "--resume" => options.resume = true,
            "--no-copy-pixels" => options.copy_pixels = false,
            "--hold" => options.hold = true,
            "--jobs" => {
                i += 1;
                options.jobs = args.get(i).and_then(|s| s.parse().ok()).unwrap_or(1);
            }
            "--jumbo-seed" => {
                i += 1;
                options.jumbo_seed = args.get(i).and_then(|s| s.parse().ok());
            }
            "--rules" => {
                i += 1;
                options.rules = args.get(i).cloned();
            }
            "--blends" => {
                i += 1;
                options.blends = args.get(i).cloned();
            }
            "--map-base" => {
                i += 1;
                options.map_base = args.get(i).cloned();
            }
            "--tilesets" => {
                i += 1;
                options.tilesets = args.get(i).cloned();
            }
            "--tile-defs" => {
                i += 1;
                options.tile_defs = args.get(i).cloned();
            }
            "--cells" => {
                i += 1;
                if let Some(text) = args.get(i) {
                    if let Some(rect) = parse_cells(text) {
                        options.cells.push(rect);
                    } else {
                        eprintln!("--cells wants x0,y0,x1,y1");
                        return ExitCode::from(2);
                    }
                }
            }
            other if other.starts_with("--cells=") => {
                if let Some(rect) = parse_cells(&other["--cells=".len()..]) {
                    options.cells.push(rect);
                } else {
                    eprintln!("--cells wants x0,y0,x1,y1");
                    return ExitCode::from(2);
                }
            }
            other if other.starts_with("--jobs=") => {
                options.jobs = other["--jobs=".len()..].parse().unwrap_or(1);
            }
            other => {
                eprintln!("unknown argument {other}");
                return ExitCode::from(2);
            }
        }
        i += 1;
    }
    match knox_compile::compile::compile(&project, &options) {
        Ok(n) => {
            println!("{{\"phase\":\"done\",\"done\":{n},\"total\":{n},\"skipped\":false,\"detail\":\"\"}}");
            ExitCode::SUCCESS
        }
        Err(err) => {
            eprintln!("{err}");
            ExitCode::from(1)
        }
    }
}

fn parse_cells(text: &str) -> Option<(i32, i32, i32, i32)> {
    let mut parts = text.split(',');
    let x0 = parts.next()?.trim().parse().ok()?;
    let y0 = parts.next()?.trim().parse().ok()?;
    let x1 = parts.next()?.trim().parse().ok()?;
    let y1 = parts.next()?.trim().parse().ok()?;
    Some((x0, y0, x1, y1))
}

fn roundtrip(path: &str) -> ExitCode {
    let data = match std::fs::read(path) {
        Ok(data) => data,
        Err(err) => {
            eprintln!("{err}");
            return ExitCode::from(1);
        }
    };
    let again = if data.starts_with(b"LOTH") {
        match lotbytes::read_header(&data).and_then(|header| Ok(lotbytes::write_header(&header))) {
            Ok(bytes) => bytes,
            Err(err) => {
                eprintln!("{err}");
                return ExitCode::from(1);
            }
        }
    } else if data.starts_with(b"LOTP") {
        match lotbytes::read_header(&[]) {
            _ => {}
        }
        // Levels are not in the pack. A header beside it supplies them.
        let header_path = path.replace(".lotpack", ".lotheader").replace("world_", "");
        let levels = std::fs::read(&header_path)
            .ok()
            .and_then(|bytes| lotbytes::read_header(&bytes).ok())
            .map(|header| header.max_level - header.min_level + 1)
            .unwrap_or(1);
        match lotbytes::read_pack(&data, levels).and_then(|pack| lotbytes::write_pack(&pack)) {
            Ok(bytes) => bytes,
            Err(err) => {
                eprintln!("{err}");
                return ExitCode::from(1);
            }
        }
    } else if path.contains("chunkdata") {
        match lotbytes::read_chunkdata(&data).and_then(|file| lotbytes::write_chunkdata(&file)) {
            Ok(bytes) => bytes,
            Err(err) => {
                eprintln!("{err}");
                return ExitCode::from(1);
            }
        }
    } else {
        eprintln!("roundtrip wants a lotheader, lotpack, or chunkdata file");
        return ExitCode::from(2);
    };
    if again == data {
        println!("round-trip match");
        ExitCode::SUCCESS
    } else {
        if let Some(msg) = lotbytes::diff_bytes(&data, &again) {
            eprintln!("{msg}");
        }
        ExitCode::from(1)
    }
}
