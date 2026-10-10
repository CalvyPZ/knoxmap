//! The one native library KnoxMap loads.
//!
//! Room layout lives in `knox_buildings`, the ground lives in `knox_terrain`,
//! and the picking of roads and buildings out of a selection lives in
//! `knox_ways`. The daily itself is read in `knox_clip`. They stay separate
//! crates. This crate is the shared library
//! they are linked into: `helper.dll`, `libhelper.so`, or `libhelper.dylib`.
//! A shared library only publishes symbols defined in that library's own
//! crate, so each call Python already uses is exported again here and handed
//! straight through.

use std::os::raw::c_char;

use knox_buildings::{Progress, ResultRec, Session as BuildingSession};
use knox_terrain::Session as TerrainSession;
use knox_ways::{Session as WaySession, WayAnswer};

#[no_mangle]
pub extern "C" fn knox_abi_version() -> u32 {
    knox_buildings::knox_abi_version()
}

#[no_mangle]
pub extern "C" fn knox_error_message(code: u32) -> *const u8 {
    knox_buildings::knox_error_message(code)
}

/// # Safety
/// Same as `knox_buildings::knox_open`.
#[no_mangle]
pub unsafe extern "C" fn knox_open(
    data: *const u8,
    len: usize,
    out_err: *mut u32,
) -> *mut BuildingSession {
    knox_buildings::knox_open(data, len, out_err)
}

/// # Safety
/// Same as `knox_buildings::knox_job_count`.
#[no_mangle]
pub unsafe extern "C" fn knox_job_count(session: *mut BuildingSession) -> u64 {
    knox_buildings::knox_job_count(session)
}

/// # Safety
/// Same as `knox_buildings::knox_start`.
#[no_mangle]
pub unsafe extern "C" fn knox_start(
    session: *mut BuildingSession,
    workers: u32,
    low_priority: u32,
) -> u32 {
    knox_buildings::knox_start(session, workers, low_priority)
}

/// # Safety
/// Same as `knox_buildings::knox_progress`.
#[no_mangle]
pub unsafe extern "C" fn knox_progress(session: *mut BuildingSession, out: *mut Progress) -> u32 {
    knox_buildings::knox_progress(session, out)
}

/// # Safety
/// Same as `knox_buildings::knox_statuses`.
#[no_mangle]
pub unsafe extern "C" fn knox_statuses(
    session: *mut BuildingSession,
    out: *mut u8,
    cap: usize,
) -> usize {
    knox_buildings::knox_statuses(session, out, cap)
}

/// # Safety
/// Same as `knox_buildings::knox_cancel`.
#[no_mangle]
pub unsafe extern "C" fn knox_cancel(session: *mut BuildingSession) {
    knox_buildings::knox_cancel(session)
}

/// # Safety
/// Same as `knox_buildings::knox_wait`.
#[no_mangle]
pub unsafe extern "C" fn knox_wait(session: *mut BuildingSession, timeout_ms: u32) -> u32 {
    knox_buildings::knox_wait(session, timeout_ms)
}

/// # Safety
/// Same as `knox_buildings::knox_finish`.
#[no_mangle]
pub unsafe extern "C" fn knox_finish(session: *mut BuildingSession) -> u32 {
    knox_buildings::knox_finish(session)
}

/// # Safety
/// Same as `knox_buildings::knox_results`.
#[no_mangle]
pub unsafe extern "C" fn knox_results(
    session: *mut BuildingSession,
    out_recs: *mut *const ResultRec,
    out_n: *mut usize,
) -> u32 {
    knox_buildings::knox_results(session, out_recs, out_n)
}

/// # Safety
/// Same as `knox_buildings::knox_error_text`.
#[no_mangle]
pub unsafe extern "C" fn knox_error_text(
    session: *mut BuildingSession,
    out_text: *mut *const u8,
    out_len: *mut usize,
) -> u32 {
    knox_buildings::knox_error_text(session, out_text, out_len)
}

/// # Safety
/// Same as `knox_buildings::knox_close`.
#[no_mangle]
pub unsafe extern "C" fn knox_close(session: *mut BuildingSession) {
    knox_buildings::knox_close(session)
}

#[no_mangle]
pub extern "C" fn knox_t_abi_version() -> u32 {
    knox_terrain::knox_t_abi_version()
}

#[no_mangle]
pub extern "C" fn knox_t_cores() -> u32 {
    knox_terrain::knox_t_cores()
}

/// # Safety
/// Same as `knox_terrain::knox_t_open`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_open(
    bytes: *const u8,
    len: usize,
    threads: u32,
    budget: u64,
    fault: *mut u32,
) -> *mut TerrainSession {
    knox_terrain::knox_t_open(bytes, len, threads, budget, fault)
}

/// # Safety
/// Same as `knox_terrain::knox_t_start`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_start(handle: *mut TerrainSession) -> u32 {
    knox_terrain::knox_t_start(handle)
}

/// # Safety
/// Same as `knox_terrain::knox_t_wait`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_wait(handle: *const TerrainSession, ms: u64) -> u32 {
    knox_terrain::knox_t_wait(handle, ms)
}

/// # Safety
/// Same as `knox_terrain::knox_t_cancel`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_cancel(handle: *const TerrainSession) {
    knox_terrain::knox_t_cancel(handle)
}

/// # Safety
/// Same as `knox_terrain::knox_t_progress`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_progress(
    handle: *const TerrainSession,
    done: *mut u64,
    total: *mut u64,
) {
    knox_terrain::knox_t_progress(handle, done, total)
}

/// # Safety
/// Same as `knox_terrain::knox_t_piece_count`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_piece_count(handle: *const TerrainSession) -> u32 {
    knox_terrain::knox_t_piece_count(handle)
}

/// # Safety
/// Same as `knox_terrain::knox_t_status`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_status(handle: *const TerrainSession, at: u32) -> u32 {
    knox_terrain::knox_t_status(handle, at)
}

/// # Safety
/// Same as `knox_terrain::knox_t_fault`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_fault(handle: *const TerrainSession, at: u32) -> u32 {
    knox_terrain::knox_t_fault(handle, at)
}

/// # Safety
/// Same as `knox_terrain::knox_t_next`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_next(
    handle: *const TerrainSession,
    index: *mut u32,
    width: *mut u32,
    height: *mut u32,
    biome: *mut *const u8,
    len: *mut usize,
) -> u32 {
    knox_terrain::knox_t_next(handle, index, width, height, biome, len)
}

/// # Safety
/// Same as `knox_terrain::knox_t_release`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_release(handle: *const TerrainSession, index: u32) -> u32 {
    knox_terrain::knox_t_release(handle, index)
}

/// # Safety
/// Same as `knox_terrain::knox_t_memory`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_memory(
    handle: *const TerrainSession,
    peak: *mut u64,
    now: *mut u64,
) {
    knox_terrain::knox_t_memory(handle, peak, now)
}

/// # Safety
/// Same as `knox_terrain::knox_t_plan`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_plan(
    handle: *const TerrainSession,
    threads: *mut u32,
    at_once: *mut u32,
) {
    knox_terrain::knox_t_plan(handle, threads, at_once)
}

#[no_mangle]
pub extern "C" fn knox_t_error_text(code: u32) -> *const c_char {
    knox_terrain::knox_t_error_text(code)
}

/// # Safety
/// Same as `knox_terrain::knox_t_close`.
#[no_mangle]
pub unsafe extern "C" fn knox_t_close(handle: *mut TerrainSession) {
    knox_terrain::knox_t_close(handle)
}

#[no_mangle]
pub extern "C" fn knox_w_abi_version() -> u32 {
    knox_ways::knox_w_abi_version()
}

#[no_mangle]
pub extern "C" fn knox_w_error_text(code: u32) -> *const c_char {
    knox_ways::knox_w_error_text(code)
}

/// # Safety
/// Same as `knox_ways::knox_w_open`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_open(
    south: f64,
    west: f64,
    north: f64,
    east: f64,
    region_south: f64,
    region_west: f64,
    region_north: f64,
    region_east: f64,
    deg: f64,
    wide: u32,
    fault: *mut u32,
) -> *mut WaySession {
    knox_ways::knox_w_open(
        south,
        west,
        north,
        east,
        region_south,
        region_west,
        region_north,
        region_east,
        deg,
        wide,
        fault,
    )
}

/// # Safety
/// Same as `knox_ways::knox_w_offer`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_offer(
    session: *mut WaySession,
    id: i64,
    nodes: *const u8,
    count: u32,
    tags: *const u8,
    tags_len: usize,
    paint: u32,
    needed: u32,
    out: *mut WayAnswer,
) -> u32 {
    knox_ways::knox_w_offer(
        session, id, nodes, count, tags, tags_len, paint, needed, out,
    )
}

/// # Safety
/// Same as `knox_ways::knox_w_abandon`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_abandon(session: *mut WaySession) {
    knox_ways::knox_w_abandon(session)
}

/// # Safety
/// Same as `knox_ways::knox_w_cell_count`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_cell_count(session: *mut WaySession) -> u32 {
    knox_ways::knox_w_cell_count(session)
}

/// # Safety
/// Same as `knox_ways::knox_w_cell`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_cell(
    session: *mut WaySession,
    index: u32,
    row: *mut i32,
    col: *mut i32,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    knox_ways::knox_w_cell(session, index, row, col, ptr, len)
}

/// # Safety
/// Same as `knox_ways::knox_w_wide`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_wide(
    session: *const WaySession,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    knox_ways::knox_w_wide(session, ptr, len)
}

/// # Safety
/// Same as `knox_ways::knox_w_classify`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_classify(
    xy: *const u8,
    xy_len: usize,
    south: f64,
    west: f64,
    north: f64,
    east: f64,
) -> i32 {
    knox_ways::knox_w_classify(xy, xy_len, south, west, north, east)
}

/// # Safety
/// Same as `knox_ways::knox_w_is_area`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_is_area(tags: *const u8, tags_len: usize, closed: u32) -> u32 {
    knox_ways::knox_w_is_area(tags, tags_len, closed)
}

/// # Safety
/// Same as `knox_ways::knox_w_take_selection`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_take_selection(
    session: *mut WaySession,
    ptr: *mut *const u8,
    len: *mut usize,
) -> u32 {
    knox_ways::knox_w_take_selection(session, ptr, len)
}

/// # Safety
/// Same as `knox_ways::knox_w_close`.
#[no_mangle]
pub unsafe extern "C" fn knox_w_close(session: *mut WaySession) {
    knox_ways::knox_w_close(session)
}

#[no_mangle]
pub extern "C" fn knox_c_abi_version() -> u32 {
    knox_clip::knox_c_abi_version()
}

#[no_mangle]
pub extern "C" fn knox_c_error_text(code: u32) -> *const c_char {
    knox_clip::knox_c_error_text(code)
}

/// # Safety
/// Same as `knox_clip::knox_c_cut`.
#[no_mangle]
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
    knox_clip::knox_c_cut(
        span_dir,
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
        span_rows,
        span_cols,
        span_wide,
        out_rows,
        out_cols,
        filters_version,
        clip_version,
        parts,
        node_rules,
        node_rules_len,
    )
}

/// # Safety
/// Same as `knox_clip::knox_c_walk`.
#[no_mangle]
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
    note: *mut knox_clip::WalkNote,
) -> u32 {
    knox_clip::knox_c_walk(
        pbf,
        span_dir,
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
        span_wide,
        out_rows,
        out_cols,
        filters_version,
        clip_version,
        span_version,
        mtime_ms,
        rules,
        rules_len,
        stop,
        note,
    )
}
