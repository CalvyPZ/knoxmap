// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.

//! BMP to TMX and Generate Lots, translated from PZ Mapping Tools.

pub mod bmptotmx;
pub mod compile;
pub mod composite;
pub mod diffcmp;
pub mod generate;
pub mod lotbytes;
pub mod lua;
pub mod model;
pub mod project;
pub mod rules;
pub mod tbx;
pub mod tiles;
pub mod tmx;
pub mod xmltree;

pub const NOTICE: &str = include_str!("../NOTICE");
