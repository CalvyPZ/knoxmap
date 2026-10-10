// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// A later `knox-compile diff` against files PZWorldEd_cli wrote is the parity
// check. This module does not invent a match. TMX and lot wrappers that differ
// only in the deflate container are compared after inflate.

//! Compare two lot or TMX files, or walk two directories.

use std::fs;
use std::path::Path;

use crate::lotbytes;

pub fn diff_paths(left: &Path, right: &Path) -> Result<(), String> {
    if left.is_dir() && right.is_dir() {
        return diff_dirs(left, right);
    }
    diff_files(left, right)
}

fn diff_dirs(left: &Path, right: &Path) -> Result<(), String> {
    let mut names = Vec::new();
    for dir in [left, right] {
        for entry in fs::read_dir(dir).map_err(|err| err.to_string())? {
            let entry = entry.map_err(|err| err.to_string())?;
            if entry.path().is_file() {
                if let Some(name) = entry.file_name().to_str() {
                    if !names.iter().any(|n| n == name) {
                        names.push(name.to_string());
                    }
                }
            }
        }
    }
    names.sort();
    let mut failed = 0usize;
    for name in &names {
        let a = left.join(name);
        let b = right.join(name);
        if !a.is_file() || !b.is_file() {
            println!("only one side has {name}");
            failed += 1;
            continue;
        }
        if let Err(err) = diff_files(&a, &b) {
            println!("{name}: {err}");
            failed += 1;
        }
    }
    if failed == 0 {
        println!("match {} files", names.len());
        Ok(())
    } else {
        Err(format!("{failed} files differ"))
    }
}

fn diff_files(left: &Path, right: &Path) -> Result<(), String> {
    let a = fs::read(left).map_err(|err| err.to_string())?;
    let b = fs::read(right).map_err(|err| err.to_string())?;
    if a == b {
        return Ok(());
    }
    let name = left.file_name().and_then(|s| s.to_str()).unwrap_or("");
    if name.ends_with(".lotheader") {
        let ra = lotbytes::roundtrip_header(&a)?;
        let rb = lotbytes::roundtrip_header(&b)?;
        if !ra || !rb {
            return Err("lotheader does not round-trip".into());
        }
    }
    if name.ends_with(".tmx") || name.ends_with(".xml") || name.ends_with(".lua") {
        if let Some(msg) = payload_delta(&a, &b) {
            return Err(msg);
        }
    }
    lotbytes::diff_bytes(&a, &b).map(Err).unwrap_or(Ok(()))
}

fn payload_delta(left: &[u8], right: &[u8]) -> Option<String> {
    let left_text = String::from_utf8_lossy(left);
    let right_text = String::from_utf8_lossy(right);
    if left_text == right_text {
        return None;
    }
    let left_plain = unwrap_embedded(&left_text);
    let right_plain = unwrap_embedded(&right_text);
    if left_plain == right_plain {
        return Some("deflate wrappers differ; uncompressed payloads match".into());
    }
    lotbytes::diff_bytes(left_plain.as_bytes(), right_plain.as_bytes())
}

fn unwrap_embedded(text: &str) -> String {
    let mut out = String::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.chars().all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/' || c == '=') && trimmed.len() > 32 {
            if let Ok(bytes) = lotbytes::decode_base64(trimmed) {
                if let Ok(plain) = lotbytes::inflate_zlib(&bytes).or_else(|_| lotbytes::inflate_gzip(&bytes)) {
                    out.push_str(&format!("<payload {} bytes>", plain.len()));
                    continue;
                }
            }
        }
        out.push_str(line);
        out.push('\n');
    }
    out
}
