// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.
//
// BmpRulesFile / BmpBlendsFile and the SimpleFile blocks they read.
// The files stay in the mapping-tools directory; nothing here copies them.

//! Rules.txt and Blends.txt.

use std::collections::HashMap;
use std::fs;
use std::path::Path;

#[derive(Clone, Debug)]
pub struct Alias {
    pub name: String,
    pub tiles: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Rule {
    pub label: String,
    pub bitmap: u32,
    pub color: (u8, u8, u8),
    pub tiles: Vec<String>,
    pub layer: String,
    pub condition: (u8, u8, u8),
    pub obsolete: bool,
}

#[derive(Clone, Debug)]
pub struct Blend {
    pub layer: String,
    pub main_tile: String,
    pub blend_tile: String,
    pub dir: String,
    pub exclude: Vec<String>,
    pub exclude2: Vec<String>,
}

#[derive(Clone, Debug, Default)]
pub struct RuleBook {
    pub aliases: Vec<Alias>,
    pub rules: Vec<Rule>,
    pub blends: Vec<Blend>,
    pub by_color0: HashMap<(u8, u8, u8), Vec<usize>>,
    pub by_color1: HashMap<(u8, u8, u8), Vec<usize>>,
}

struct Block {
    name: String,
    values: Vec<(String, Vec<String>)>,
}

pub fn load_rules(path: &Path) -> Result<Vec<(Alias, ())>, String> {
    let _ = path;
    Ok(Vec::new())
}

pub fn load_book(rules_path: &Path, blends_path: &Path) -> Result<RuleBook, String> {
    let mut book = RuleBook::default();
    let text = fs::read_to_string(rules_path)
        .map_err(|err| format!("{}: {err}", rules_path.display()))?;
    if is_old_format(&text) {
        read_old_rules(&text, &mut book)?;
    } else {
        for block in parse_simple(&text)? {
            match block.name.as_str() {
                "alias" => {
                    let name = value(&block, "name")?;
                    if book.aliases.iter().any(|a| a.name == name) {
                        return Err(format!("duplicate alias '{name}'"));
                    }
                    let tiles = values(&block, "tiles")?;
                    book.aliases.push(Alias { name, tiles });
                }
                "rule" => add_rule(&mut book, rule_from_block(&block)?)?,
                other => return Err(format!("unknown Rules.txt block '{other}'")),
            }
        }
    }
    if blends_path.is_file() {
        let text = fs::read_to_string(blends_path)
            .map_err(|err| format!("{}: {err}", blends_path.display()))?;
        for block in parse_simple(&text)? {
            if block.name != "blend" {
                return Err(format!("unknown Blends.txt block '{}'", block.name));
            }
            book.blends.push(Blend {
                layer: value(&block, "layer").unwrap_or_default(),
                main_tile: value(&block, "mainTile").unwrap_or_default(),
                blend_tile: value(&block, "blendTile").unwrap_or_default(),
                dir: value(&block, "dir").unwrap_or_default(),
                exclude: optional_values(&block, "exclude"),
                exclude2: optional_values(&block, "exclude2"),
            });
        }
    }
    index_rules(&mut book);
    Ok(book)
}

fn add_rule(book: &mut RuleBook, rule: Rule) -> Result<(), String> {
    if rule.bitmap > 1 {
        return Err(format!("bitmap index {} is not 0 or 1", rule.bitmap));
    }
    book.rules.push(rule);
    Ok(())
}

fn rule_from_block(block: &Block) -> Result<Rule, String> {
    let color = rgb(&value(block, "color")?)?;
    let mut tiles = values(block, "tiles")?;
    for tile in &mut tiles {
        if tile == "null" {
            tile.clear();
        }
    }
    if tiles.is_empty() {
        return Err("rule needs a tile".into());
    }
    Ok(Rule {
        label: optional(block, "label"),
        bitmap: value(block, "bitmap")?.parse().map_err(|_| "bad bitmap index".to_string())?,
        color,
        tiles,
        layer: value(block, "layer")?,
        condition: match optional(block, "condition") {
            ref s if s.is_empty() => (0, 0, 0),
            ref s => rgb(s)?,
        },
        obsolete: optional(block, "obsolete") == "true",
    })
}

fn index_rules(book: &mut RuleBook) {
    for (i, rule) in book.rules.iter().enumerate() {
        let map = if rule.bitmap == 0 {
            &mut book.by_color0
        } else {
            &mut book.by_color1
        };
        map.entry(rule.color).or_default().push(i);
    }
}

fn is_old_format(text: &str) -> bool {
    text.lines().any(|line| {
        let line = line.trim();
        if line.is_empty() || line.starts_with("//") {
            return false;
        }
        let n = line.split(',').count();
        n == 6 || n == 9
    }) && !text.contains("rule")
}

fn read_old_rules(text: &str, book: &mut RuleBook) -> Result<(), String> {
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with("//") {
            continue;
        }
        let fields: Vec<&str> = line.split(',').map(str::trim).collect();
        if fields.len() != 6 && fields.len() != 9 {
            continue;
        }
        let bitmap: u32 = fields[0].parse().map_err(|_| format!("old rule '{line}'"))?;
        let color = (
            fields[1].parse().map_err(|_| "red".to_string())?,
            fields[2].parse().map_err(|_| "green".to_string())?,
            fields[3].parse().map_err(|_| "blue".to_string())?,
        );
        let tiles = fields[4]
            .split_whitespace()
            .map(|t| if t == "null" { String::new() } else { t.to_string() })
            .collect();
        let condition = if fields.len() == 9 {
            (
                fields[6].parse().unwrap_or(0),
                fields[7].parse().unwrap_or(0),
                fields[8].parse().unwrap_or(0),
            )
        } else {
            (0, 0, 0)
        };
        add_rule(
            book,
            Rule {
                label: String::new(),
                bitmap,
                color,
                tiles,
                layer: fields[5].to_string(),
                condition,
                obsolete: false,
            },
        )?;
    }
    Ok(())
}

fn parse_simple(text: &str) -> Result<Vec<Block>, String> {
    let mut blocks = Vec::new();
    let mut current: Option<Block> = None;
    let mut pending_name = String::new();
    let mut array: Option<(String, Vec<String>)> = None;
    for (number, raw) in text.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with("//") {
            continue;
        }
        if let Some((name, values)) = array.as_mut() {
            let end = line.ends_with(']');
            let piece = line.trim_end_matches(']').trim();
            if !piece.is_empty() {
                values.extend(piece.split_whitespace().map(str::to_string));
            }
            if end {
                if let Some(block) = current.as_mut() {
                    block.values.push((name.clone(), values.clone()));
                }
                array = None;
            }
            continue;
        }
        if line == "{" {
            current = Some(Block {
                name: std::mem::take(&mut pending_name),
                values: Vec::new(),
            });
            continue;
        }
        if line == "}" {
            let block = current.take().ok_or_else(|| format!("line {}: extra }}", number + 1))?;
            blocks.push(block);
            continue;
        }
        if let Some(eq) = line.find('=') {
            let name = line[..eq].trim().to_string();
            let mut value = line[eq + 1..].trim().to_string();
            if value.starts_with('[') && !value.ends_with(']') {
                value = value.trim_start_matches('[').trim().to_string();
                let mut values = Vec::new();
                if !value.is_empty() {
                    values.extend(value.split_whitespace().map(str::to_string));
                }
                array = Some((name, values));
                continue;
            }
            value = value.trim_start_matches('[').trim_end_matches(']').trim().to_string();
            let values: Vec<String> = if value.is_empty() {
                Vec::new()
            } else {
                value.split_whitespace().map(str::to_string).collect()
            };
            if let Some(block) = current.as_mut() {
                block.values.push((name, values));
            }
            continue;
        }
        if current.is_none() {
            if !pending_name.is_empty() {
                pending_name.push(' ');
            }
            pending_name.push_str(line);
        } else {
            return Err(format!("line {}: expected name = value", number + 1));
        }
    }
    Ok(blocks)
}

fn value(block: &Block, key: &str) -> Result<String, String> {
    block
        .values
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, values)| values.join(" "))
        .ok_or_else(|| format!("missing {key} in {}", block.name))
}

fn values(block: &Block, key: &str) -> Result<Vec<String>, String> {
    block
        .values
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, values)| values.clone())
        .ok_or_else(|| format!("missing {key} in {}", block.name))
}

fn optional(block: &Block, key: &str) -> String {
    block
        .values
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, values)| values.join(" "))
        .unwrap_or_default()
}

fn optional_values(block: &Block, key: &str) -> Vec<String> {
    block
        .values
        .iter()
        .find(|(name, _)| name == key)
        .map(|(_, values)| values.clone())
        .unwrap_or_default()
}

fn rgb(text: &str) -> Result<(u8, u8, u8), String> {
    let mut parts = text.split_whitespace();
    let r = parts.next().and_then(|s| s.parse().ok());
    let g = parts.next().and_then(|s| s.parse().ok());
    let b = parts.next().and_then(|s| s.parse().ok());
    match (r, g, b) {
        (Some(r), Some(g), Some(b)) => Ok((r, g, b)),
        _ => Err(format!("colour '{text}' is not three numbers")),
    }
}

pub fn rgb_string(color: (u8, u8, u8)) -> String {
    format!("{} {} {}", color.0, color.1, color.2)
}

/// Expand an alias to its tiles. A tile name that is not an alias is itself.
pub fn resolve_tile<'a>(book: &'a RuleBook, name: &'a str) -> Vec<&'a str> {
    if let Some(alias) = book.aliases.iter().find(|a| a.name == name) {
        alias.tiles.iter().map(String::as_str).collect()
    } else if name.is_empty() {
        Vec::new()
    } else {
        vec![name]
    }
}
