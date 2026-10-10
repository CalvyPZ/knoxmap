"""Draw one piece of ground through the terrain library.

The decisions stay here: which way is a street, where a house goes, which block
is dense enough to pave. What leaves is a painting program. The library walks
that program on native threads, out of one allocation, and writes the bitmaps.
The cell PNGs are still written here, from the biome plane the library hands
back, because that writer is already there and already releases the lock.
"""
from __future__ import annotations

import gc
import json
import math
import os
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from PIL import Image

import knoxstop

from . import biomes
from . import intersections
from . import pz_colors as C
from . import renderer as R
from . import terrain_native as native
from . import terrain_record as record
from .osm import AIRPORT_PAVED, is_rail_area

# Marks in the scratch mask. Woods start at 16, above these, so a wood pass
# never shades a block that was only marked to be paved.
MARK_PAVE = 1
MARK_HOUSE = 2
MARK_CLEAR = 3
MARK_ORCHARD = 4
MARK_CEMETERY = 5
MARK_WETLAND = 6


def paint(proj, *, south, west, north, east, meters_per_tile, output_dir,
          map_name, spawn_density, tree_density, rotation, osm_cache, osm_bbox,
          shape, straight_roads, should_stop, on_view, file_workers, keep_cells,
          library_threads, buckets, paint_buckets, paint_veg, building_feats,
          fence_feats, place_feats, lifted, junctions, clip,
          houses_from_addresses, houses_from_streets) -> R.RenderResult:
    job = native.Job()
    palette = record.Palette(job)
    drawn = record.Recorder(
        job, palette, proj.width, proj.height,
        (south + north) / 2.0, (west + east) / 2.0,
        tree_density=tree_density, metres_per_tile=meters_per_tile)
    program = drawn.program
    cover = drawn.cover
    show = R._paint_watch(on_view, key=map_name)
    show(None, force=True, stage="Terrain and biomes", work=0.05)

    def ground_rings(feat):
        rings = R._feature_coords_px(feat, proj)
        cut = lifted.cut.get(id(feat))
        if cut is None:
            return rings
        from shapely.geometry import LineString
        left = LineString(rings[0]).difference(cut)
        return [list(part.coords) for part in getattr(left, "geoms", [left])
                if part.geom_type == "LineString" and not part.is_empty]

    def roles(feat):
        return [(role, ring) for (role, _coords), ring
                in zip(feat.role_geoms, R._feature_coords_px(feat, proj))]

    def mark(cat, feat, rings, width=None):
        cover.stamp(cat, feat.tags, rings, width=width,
                    relation_rings=roles(feat) if feat.kind == "relation" else None)

    for sea in R.sea_polygons(buckets.get("coastline", []), proj):
        for part in getattr(sea, "geoms", None) or [sea]:
            if not hasattr(part, "exterior"):
                continue
            outer = list(part.exterior.coords)
            program.fill([outer], C.WATER)
            cover.polygon([outer], biomes.WATER, water=True)
            for hole in part.interiors:
                ring = list(hole.coords)
                program.fill([ring], C.DARK_GRASS)
                cover.polygon([ring], cover.open)

    for cat in R.LANDSCAPE_ORDER:
        if cat == "railway":
            for road in ("road_minor", "road_medium", "road_major"):
                margin = R.SIDEWALK_M[road]
                for feat in paint_buckets.get(road, []):
                    if R._is_polygon(feat):
                        continue
                    width = int((R._way_width_m(feat, road) + 2 * margin) / meters_per_tile)
                    rings = ground_rings(feat)
                    program.stroke(rings, width, C.PALE_CONCRETE)
                    cover.line(rings, biomes.DIRT, width)
            for road, verge in R.VERGE_M.items():
                for feat in paint_buckets.get(road, []):
                    if R._is_polygon(feat):
                        continue
                    width = int((R._way_width_m(feat, road) + 2 * verge) / meters_per_tile)
                    rings = ground_rings(feat)
                    program.stroke(rings, width, C.DARK_GRASS)
                    cover.line(rings, biomes.TOWN, width)
        fill = R.LANDSCAPE_FILL.get(cat)
        if fill is None:
            continue
        for feat in paint_buckets.get(cat, []):
            if cat == "airport" and feat.tags.get("aeroway") in AIRPORT_PAVED:
                continue
            if cat == "railway" and is_rail_area(feat.tags) and R._is_polygon(feat):
                continue
            rings = R._feature_coords_px(feat, proj)
            colour = R._route_fill(cat, feat.tags)
            water = cat in {"water", "pool"}
            lake = water and biomes.is_lake(feat.tags)
            if cat in R.ROAD_WIDTHS_M and not R._is_polygon(feat):
                width = int(R._way_width_m(feat, cat) / meters_per_tile)
                drawn_rings = ground_rings(feat)
                program.stroke(drawn_rings, max(1, width), colour, water=water, lake=lake)
                mark(cat, feat, drawn_rings, max(1, width))
            elif feat.kind == "relation":
                _relation(program, feat, proj, colour, water=water, lake=lake)
                mark(cat, feat, rings)
            elif R._is_polygon(feat):
                program.fill(rings, colour, water=water, lake=lake)
                mark(cat, feat, rings)
            else:
                metres = R.WATERWAY_WIDTH_M.get(feat.tags.get("waterway"), 3.0)
                width = max(1, int(metres / meters_per_tile))
                program.stroke(rings, width, fill, water=water, lake=lake)
                mark(cat, feat, rings, width)
        if cat == "industrial":
            yard = R.LANDSCAPE_FILL["railway"]
            for feat in paint_buckets.get("railway", []):
                if not (is_rail_area(feat.tags) and R._is_polygon(feat)):
                    continue
                rings = R._feature_coords_px(feat, proj)
                if feat.kind == "relation":
                    _relation(program, feat, proj, yard)
                else:
                    program.fill(rings, yard)
                mark("railway", feat, rings)

    for feat in paint_buckets.get("airport", []):
        if feat.tags.get("aeroway") not in AIRPORT_PAVED:
            continue
        if R._is_polygon(feat):
            rings = R._feature_coords_px(feat, proj)
            if feat.kind == "relation":
                _relation(program, feat, proj, C.DARK_ASPHALT)
            else:
                program.fill(rings, C.DARK_ASPHALT)
            mark("parking", feat, rings)
        else:
            width = max(1, int(R._aeroway_width_m(feat) / meters_per_tile))
            rings = ground_rings(feat)
            program.stroke(rings, width, C.DARK_ASPHALT)
            cover.line(rings, biomes.DIRT, width)

    for deck, cat in lifted.straight:
        colour = R.LANDSCAPE_FILL.get(cat, C.MEDIUM_ASPHALT)
        program.rect(deck.bounds, colour)
        cover.rectangle(deck.bounds, biomes.pixel_for(cat, {}, cover.climate))
    _islands(program, cover, junctions, proj)
    _bulbs(program, cover, junctions)
    for paved in lifted.paving:
        ring = list(paved.exterior.coords)
        program.fill([ring], C.PAVING)
        cover.polygon([ring], biomes.TOWN)

    show(None, stage="Finding city blocks", work=0.30)
    _city_blocks(drawn, building_feats, paint_buckets, proj, meters_per_tile)
    if clip is not None:
        show(None, stage="Clipping the map edge", work=0.55)
        _clip(drawn, clip, paint_buckets, proj, meters_per_tile)
    show(None, stage="Wearing the road surface", work=0.60)
    drawn.weather_pass()

    knoxstop.check(should_stop, "the terrain")
    show(None, stage="Painting trees", work=0.66)
    _vegetation(drawn, paint_veg, paint_buckets, building_feats, proj, meters_per_tile)
    drawn.soften_pass()
    drawn.kerbs_pass()
    _centre_lines(program, paint_buckets, proj, junctions)
    for x, y in lifted.clear_veg:
        program.rect((x, y, x, y), veg=C.VEG_NOTHING)

    bands = drawn.spawn_pass()
    drawn.add(name=map_name, folder=output_dir, seed=_seed(map_name),
              spawn_density=spawn_density, keep_cells=keep_cells,
              spawn_bands=bands)

    # Side files that still name features are written here, while the lists
    # exist. The library draws from the packed program, so once those files
    # are on disk the lists and the pixel-ring cache go, before the canvas.
    show(None, stage="Writing map files", work=0.74)
    kept = _store_feature_files(
        proj, output_dir, map_name, buckets, building_feats, fence_feats,
        place_feats, lifted, junctions, clip, file_workers, show)
    climate, open_biome = cover.climate, cover.open
    del ground_rings, roles, mark, bands
    _release_graph(
        proj, buckets, paint_buckets, paint_veg, building_feats,
        fence_feats, place_feats, junctions, lifted)
    del buckets, paint_buckets, paint_veg, building_feats, fence_feats, place_feats
    del junctions, lifted, clip
    blob = job.pack()
    _unlink_recorder(drawn, cover)
    del job, palette, drawn, program, cover
    gc.collect()

    show(None, stage="Drawing the ground", work=0.80)
    biome_image = None

    def on_piece(piece):
        nonlocal biome_image
        biome_image = Image.frombytes("L", (piece.width, piece.height), bytes(piece.biome))

    def on_progress(done, total):
        if total:
            show(None, stage="Drawing the ground", work=0.80 + 0.17 * done / total)

    statuses, faults = native.run(
        blob, threads=library_threads or 0, on_piece=on_piece,
        on_progress=on_progress, should_stop=should_stop)
    if not statuses or statuses[0] != native.DONE:
        code = faults[0] if faults else 0
        if statuses and statuses[0] == native.STOPPED:
            knoxstop.check(lambda: True, "the terrain")
        raise RuntimeError(native.why(code) if code else "the terrain library did not draw this piece")
    if biome_image is None:
        raise RuntimeError("the terrain library drew the piece and returned no biome")
    del blob

    show(None, force=True, stage="Writing map files", work=0.98)
    return _write(proj, biome_image, output_dir, map_name, south, west, north, east,
                  rotation, osm_cache, osm_bbox, shape, straight_roads, meters_per_tile,
                  spawn_density, kept, climate, open_biome, houses_from_addresses,
                  houses_from_streets, on_view, file_workers, show)


def _relation(program, feat, proj, colour, *, water=False, lake=False) -> None:
    pairs = list(zip(feat.role_geoms, R._feature_coords_px(feat, proj)))
    outers = [ring for (role, _c), ring in pairs if role != "inner" and len(ring) >= 3]
    inners = [ring for (role, _c), ring in pairs if role == "inner" and len(ring) >= 3]
    if outers:
        program.relation(outers, inners, colour, water=water, lake=lake)


def _islands(program, cover, junctions, proj) -> None:
    """The hole in a roundabout, filled as paving or grass by how big it is."""
    from shapely.geometry import Polygon

    plan = junctions
    for road in plan.roundabouts:
        if road.skipped:
            continue
        pts = [proj.to_px(lat, lon) for lat, lon in road.feat.geometry]
        if len(pts) < 4:
            continue
        if pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        ring = Polygon(pts)
        if not ring.is_valid:
            ring = ring.buffer(0)
        if ring.is_empty:
            continue
        inset = max(1.0, (road.width_m / plan.mpt) / 2.0 + road.sidewalk_m / plan.mpt + 0.5)
        inner = ring.buffer(-inset)
        parts = [inner] if getattr(inner, "geom_type", "") == "Polygon" else [
            part for part in getattr(inner, "geoms", ()) if part.geom_type == "Polygon"]
        parts = [part for part in parts if not part.is_empty]
        if not parts:
            continue
        radius_m = math.sqrt(max(sum(part.area for part in parts), 0.0) / math.pi) * plan.mpt
        fill = C.PAVING if radius_m < intersections.ISLAND_PAVING_M else C.MEDIUM_GRASS
        biome = biomes.TOWN if fill == C.PAVING else cover.open
        for part in parts:
            outer = list(part.exterior.coords)
            holes = [list(hole.coords) for hole in part.interiors]
            program.relation([outer], holes, fill, biome=biome)


def _bulbs(program, cover, junctions) -> None:
    """A turning circle: concrete disc, then the carriageway colour inside it."""
    plan = junctions
    for cluster in plan.clusters:
        if cluster.skipped or cluster.kind != "turning_circle" or cluster.centre is None:
            continue
        arm = cluster.arms[0] if cluster.arms else None
        if arm is None:
            continue
        diameter = cluster.diameter_m
        if diameter is None:
            diameter = min(intersections.TURNING_MAX_M,
                           max(intersections.TURNING_MIN_M, arm.width_m * 1.2))
        else:
            diameter = min(intersections.TURNING_MAX_M,
                           max(intersections.TURNING_MIN_M, diameter))
        radius = diameter / 2.0 / plan.mpt
        outer = radius + max(arm.sidewalk, 1.0)
        rank = intersections._RANK.get(arm.cat, 2)
        colour = intersections._COLOUR.get(rank, C.MEDIUM_ASPHALT)
        cx, cy = cluster.centre
        program.disc((cx, cy), outer * 2, C.PALE_CONCRETE, biome=biomes.TOWN)
        program.disc((cx, cy), radius * 2, colour, biome=biomes.DIRT)


def _city_blocks(drawn, building_feats, buckets, proj, meters_per_tile) -> None:
    """Pave a block whole when buildings cover enough of it. Parks stay grass."""
    from .terrain_blocks import mark_blocks

    if not mark_blocks(drawn.program, building_feats, buckets, proj,
                       meters_per_tile, MARK_PAVE):
        return
    for cat in R.GREEN_CATEGORIES:
        for feat in buckets.get(cat, []):
            if R._is_polygon(feat):
                drawn.fill_mask(R._feature_coords_px(feat, proj), 0)
    drawn.paint_mask_pass(MARK_PAVE, over=drawn.palette.wild,
                          ground=C.PALE_CONCRETE, biome=biomes.TOWN)


def _clip(drawn, clip, buckets, proj, meters_per_tile) -> None:
    """Outside the drawn shape, countryside again. Water and the main roads stay."""
    rect = [(0, 0), (proj.width, 0), (proj.width, proj.height), (0, proj.height)]
    inners = []
    holes = []
    for part in getattr(clip, "geoms", None) or [clip]:
        if not hasattr(part, "exterior"):
            continue
        inners.append(list(part.exterior.coords))
        holes.extend(list(hole.coords) for hole in part.interiors)
    grass = C.DARK_GRASS
    program = drawn.program
    cover = drawn.cover
    if inners:
        program.relation([rect], inners, grass, biome=cover.open)
    for hole in holes:
        program.fill([hole], grass, biome=cover.open)
    for cat in ("water", "pool"):
        for feat in buckets.get(cat, []):
            rings = R._feature_coords_px(feat, proj)
            lake = biomes.is_lake(feat.tags)
            if R._is_polygon(feat):
                if feat.kind == "relation":
                    _relation(program, feat, proj, C.WATER, water=True, lake=lake)
                else:
                    program.fill(rings, C.WATER, water=True, lake=lake)
                cover.stamp(cat, feat.tags, rings,
                            relation_rings=[(role, ring) for (role, _), ring
                                            in zip(feat.role_geoms, rings)]
                            if feat.kind == "relation" else None)
    for cat in R.THROUGH_ROADS:
        for feat in buckets.get(cat, []):
            if R._is_polygon(feat):
                continue
            width = int((R._way_width_m(feat, cat) + 2 * R.SIDEWALK_M[cat]) / meters_per_tile)
            rings = R._feature_coords_px(feat, proj)
            program.stroke(rings, max(1, width), C.PALE_CONCRETE)
            cover.line(rings, biomes.DIRT, max(1, width))
            road = int(R._way_width_m(feat, cat) / meters_per_tile)
            colour = R._route_fill(cat, feat.tags)
            program.stroke(rings, max(1, road), colour)
            cover.line(rings, biomes.pixel_for(cat, feat.tags, cover.climate), max(1, road))


def _vegetation(drawn, feats, buckets, buildings, proj, meters_per_tile) -> None:
    """Hedges, orchards, cemeteries, wetland, then the woods, then yards."""
    program = drawn.program
    hedge = max(1, int(round(1.5 / meters_per_tile)))
    for feat in feats:
        if feat.tags.get("barrier") == "hedge":
            program.stroke(R._feature_coords_px(feat, proj), hedge, veg=C.BUSHES)
    _marked(drawn, feats, proj, "orchard", MARK_ORCHARD)
    spacing = max(2, int(round(4 / meters_per_tile)))
    drawn.masked_pass(native.GRID, MARK_ORCHARD, over=drawn.palette.soft,
                      spacing=spacing, salt=0x05E1,
                      picks=[(C.DARK_GRASS, C.TREES, None, 1)], bare=True)
    _marked(drawn, feats, proj, "cemetery", MARK_CEMETERY)
    drawn.masked_pass(native.SCATTER, MARK_CEMETERY, over=drawn.palette.soft,
                      chance=0.04, salt=0x05E2,
                      picks=[(C.DARK_GRASS, C.TREES, None, 3),
                             (None, C.FLOWERS, None, 7)], bare=True)
    _marked(drawn, feats, proj, "wetland", MARK_WETLAND)
    drawn.masked_pass(native.SCATTER, MARK_WETLAND, over=drawn.palette.soft,
                      chance=0.35, salt=0x05E3,
                      picks=[(C.DARK_GRASS, C.DENSE_BUSHES_GRASS, None, 1)], bare=True)
    for feat in feats:
        cat = R.classify(feat.tags, R._is_polygon(feat))
        rings = R._feature_coords_px(feat, proj)
        if cat in {"forest", "scrub"} and R._is_polygon(feat):
            holes = ()
            if feat.kind == "relation":
                outers, holes = [], []
                for (role, _coords), projected in zip(feat.role_geoms, rings):
                    if len(projected) < 3:
                        continue
                    (holes if role == "inner" else outers).append(projected)
                rings = outers
            drawn.cover.queue_wood(rings, feat.tags, scrub=cat == "scrub", holes=holes)
        elif cat == "tree_single" and rings and rings[0]:
            radius = max(1, int(2 / meters_per_tile))
            drawn.cover.queue_wood(rings, feat.tags, radius=radius)
    drawn.cover.flush_woods()
    drawn.cover.apply_shores()
    for feat in buildings:
        drawn.fill_mask(R._feature_coords_px(feat, proj), MARK_HOUSE)
    reach = R.GARDEN_REACH_TILES
    every = R.GARDEN_TREE_EVERY
    chance = min(1.0, R.GARDEN_TREE_CHANCE * drawn.tree_density)
    drawn.near_pass(mark=MARK_HOUSE, reach=reach, clear=R.GARDEN_CLEAR_OF_HOUSE + 1,
                    paving=R.GARDEN_CLEAR_OF_PAVING, spacing=every, chance=chance,
                    salt=0x6A11, over=drawn.palette.grass, avoid=drawn.palette.hard,
                    picks=[(C.DARK_GRASS, C.TREES, None, 1)])
    shrub = min(1.0, R.SHRUB_CHANCE * drawn.tree_density)
    drawn.near_pass(mark=MARK_HOUSE, reach=1, clear=1, paving=R.GARDEN_CLEAR_OF_PAVING,
                    spacing=1, chance=shrub, salt=0x6A12, over=drawn.palette.grass,
                    avoid=drawn.palette.hard, picks=[(None, C.BUSHES, None, 1)])
    drawn.tufts_pass(R.SHORT_GRASS_SHARE)
    wild = min(1.0, 0.04 * drawn.tree_density)
    which = drawn.job.add_pass(
        native.SCATTER, over=drawn.palette.wild, avoid=drawn.palette.hard,
        chance=wild, salt=0x571D, flags=native.PASS_BARE,
        picks=[(C.DARK_GRASS, C.TREES, None, 55), (C.DARK_GRASS, C.BUSHES, None, 45)])
    program.run_pass(which)
    for feat in buildings:
        drawn.fill_mask(R._feature_coords_px(feat, proj), MARK_CLEAR)
    for feat in buckets.get("airport", []):
        if R._is_polygon(feat):
            drawn.fill_mask(R._feature_coords_px(feat, proj), MARK_CLEAR)
    for cat in ("dirt_path", "paved_path", "road_track"):
        for feat in buckets.get(cat, []):
            if R._is_polygon(feat):
                continue
            width = max(1, int(R._way_width_m(feat, cat) / meters_per_tile))
            drawn.stroke_mask(R._feature_coords_px(feat, proj), width, MARK_CLEAR)
    drawn.paint_mask_pass(MARK_CLEAR, veg=C.VEG_NOTHING)


def _marked(drawn, feats, proj, cat, mark) -> None:
    for feat in feats:
        if R.classify(feat.tags, R._is_polygon(feat)) == cat and R._is_polygon(feat):
            drawn.fill_mask(R._feature_coords_px(feat, proj), mark)


def _centre_lines(program, buckets, proj, junctions) -> None:
    """A line down a straight two-lane road. A diagonal is left plain."""
    rects = []
    try:
        rects = list(junctions.skip_rects())
    except Exception:
        rects = []
    mpt = proj.meters_per_tile
    for cat, colour in (("road_major", C.LINE_YELLOW_N), ("road_medium", C.LINE_WHITE_N)):
        across = C.LINE_YELLOW_W if cat == "road_major" else C.LINE_WHITE_W
        for feat in buckets.get(cat, []):
            if R._is_polygon(feat):
                continue
            if feat.tags.get("junction") in {"roundabout", "circular"}:
                continue
            width = R._way_width_m(feat, cat) / mpt
            if width < 6 or feat.tags.get("oneway") in ("yes", "1", "-1"):
                continue
            for ring in R._feature_coords_px(feat, proj):
                for (ax, ay), (bx, by) in zip(ring, ring[1:]):
                    dx, dy = bx - ax, by - ay
                    if abs(dy) <= 0.2 * abs(dx):
                        tone, horizontal = colour, True
                    elif abs(dx) <= 0.2 * abs(dy):
                        tone, horizontal = across, False
                    else:
                        continue
                    _dash(program, ax, ay, bx, by, tone, dashed=cat == "road_medium",
                          horizontal=horizontal, blocked=rects)


def _dash(program, ax, ay, bx, by, colour, *, dashed, horizontal, blocked) -> None:
    steps = int(max(abs(bx - ax), abs(by - ay)))
    if steps <= 0:
        return
    for i in range(steps + 1):
        t = i / steps
        x = int(round(ax + (bx - ax) * t))
        y = int(round(ay + (by - ay) * t))
        if dashed and ((x if horizontal else y) // 3) % 2:
            continue
        if any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in blocked):
            continue
        program.rect((x, y, x, y), veg=colour)


def _seed(name: str) -> int:
    return zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF


def _save_json(path, data, **options) -> None:
    """One JSON file. dumps() is the same encoder the Python painter uses."""
    text = json.dumps(data, **options)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def _store_feature_files(proj, output_dir, map_name, buckets, building_feats,
                         fence_feats, place_feats, lifted, junctions, clip,
                         file_workers, show) -> dict:
    """Write the feature files, then keep only the preview lines and the counts.

    Same documents `_write` used to build. Each one is made in the worker
    that saves it, and dropped when that write returns, so the documents are
    not still in memory when the canvas is allocated. The preview reads fence
    lines off the bitmap later; those lines are copied here, first.
    """
    os.makedirs(output_dir, exist_ok=True)
    roads = [feat for cat in R.ROAD_EXPORT_CATEGORIES for feat in buckets.get(cat, [])]
    fence_export = (R._features_inside(fence_feats, proj, clip)
                    if clip is not None else fence_feats)
    preview_lines = R._preview_line_rings(fence_export, proj)
    building_count = len(building_feats)
    feature_count = sum(len(v) for v in buckets.values())

    def bind(make, options):
        def run(path):
            _save_json(path, make(), **options)
        return run

    jobs = (
        ("buildings", os.path.join(output_dir, f"{map_name}_buildings.geojson"),
         bind(lambda: R._buildings_geojson(building_feats), {})),
        ("areas", os.path.join(output_dir, f"{map_name}_areas.geojson"),
         bind(lambda: R._areas_geojson(buckets), {})),
        ("roads", os.path.join(output_dir, f"{map_name}_roads.geojson"),
         bind(lambda: R._roads_geojson(roads, R.classify), {})),
        ("junctions", os.path.join(output_dir, f"{map_name}_junctions.json"),
         bind(lambda: intersections.to_json(junctions), {})),
        ("fences", os.path.join(output_dir, f"{map_name}_fences.geojson"),
         bind(lambda: R._lines_geojson(fence_export), {})),
        ("structures", os.path.join(output_dir, f"{map_name}_structures.json"),
         bind(lambda: {"bridges": lifted.bridges, "monuments": lifted.monuments,
                       "tiles": lifted.rows()}, {})),
        ("places", os.path.join(output_dir, f"{map_name}_places.json"),
         bind(lambda: R._places(place_feats, proj), {"ensure_ascii": False})),
        ("zones", os.path.join(output_dir, f"{map_name}_zones.geojson"),
         bind(lambda: R._zones_geojson(buckets), {})),
    )
    workers = R._half_cores() if file_workers is None else max(1, int(file_workers))
    total = len(jobs)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="knox-output") as executor:
        pending = {executor.submit(run, path): name for name, path, run in jobs}
        done = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                pending.pop(future)
                future.result()
                done += 1
                show(None, force=True,
                     stage=f"Writing map files, {done} of {total}",
                     work=0.74 + 0.06 * done / total)
    return {
        "preview_lines": preview_lines,
        "building_count": building_count,
        "feature_count": feature_count,
    }


def _blank_feature(feat) -> None:
    """Drop one feature's coordinates and tags. Callers may still hold the shell."""
    geometry = getattr(feat, "geometry", None)
    if isinstance(geometry, list):
        for ring in geometry:
            if isinstance(ring, list):
                ring.clear()
        geometry.clear()
    roles = getattr(feat, "role_geoms", None)
    if isinstance(roles, list):
        for item in roles:
            ring = item[1] if isinstance(item, tuple) and len(item) >= 2 else None
            if isinstance(ring, list):
                ring.clear()
        roles.clear()
    tags = getattr(feat, "tags", None)
    if isinstance(tags, dict):
        tags.clear()


def _blank_features(groups) -> None:
    seen: set[int] = set()
    for group in groups:
        if not group:
            continue
        for feat in group:
            if feat is None:
                continue
            key = id(feat)
            if key in seen:
                continue
            seen.add(key)
            _blank_feature(feat)


def _clear_bucket_table(table) -> None:
    if not isinstance(table, dict):
        return
    for feats in table.values():
        if isinstance(feats, list):
            feats.clear()
    table.clear()


def _release_graph(proj, buckets, paint_buckets, paint_veg, building_feats,
                   fence_feats, place_feats, junctions, lifted) -> None:
    """Empty the feature lists and the junction lines the draw no longer reads."""
    groups = []
    if isinstance(buckets, dict):
        groups.extend(buckets.values())
    if paint_buckets is not buckets and isinstance(paint_buckets, dict):
        groups.extend(paint_buckets.values())
    groups.extend((paint_veg, building_feats, fence_feats, place_feats))
    if junctions is not None:
        groups.append([road.feat for road in junctions.roundabouts])
    _blank_features(groups)
    _clear_bucket_table(buckets)
    if paint_buckets is not buckets:
        _clear_bucket_table(paint_buckets)
    for feats in (paint_veg, building_feats, fence_feats, place_feats):
        if isinstance(feats, list):
            feats.clear()
    if junctions is not None:
        for road in junctions.roundabouts:
            road.feat = None
            if isinstance(road.lls, list):
                road.lls.clear()
            if isinstance(road.keys, list):
                road.keys.clear()
            if isinstance(road.at, dict):
                road.at.clear()
        junctions.roundabouts.clear()
        junctions.key_ll.clear()
        junctions.key_px.clear()
        junctions.clusters.clear()
        junctions.crossings.clear()
    if lifted is not None:
        lifted.tiles.clear()
        lifted.cut.clear()
        lifted.clear_veg.clear()
        lifted.water_rails.clear()
        lifted.paving.clear()
        lifted.not_buildings.clear()
        lifted.posts.clear()
        lifted.straight.clear()
    R.clear_px_rings(proj)


def _unlink_recorder(drawn, cover) -> None:
    """The packed buffer already holds the program. Drop the Python copy of it."""
    if cover is not None:
        woods = getattr(cover, "_woods", None)
        if isinstance(woods, list):
            woods.clear()
        cover.record = None
    if drawn is None:
        return
    drawn.cover = None
    drawn.ground = None
    drawn.veg = None
    drawn.job = None
    drawn.palette = None
    drawn.program = None


def _write(proj, biome_image, output_dir, map_name, south, west, north, east,
           rotation, osm_cache, osm_bbox, shape, straight_roads, meters_per_tile,
           spawn_density, kept, climate, open_biome, houses_from_addresses,
           houses_from_streets, on_view, file_workers, show) -> R.RenderResult:
    os.makedirs(output_dir, exist_ok=True)
    landscape_path = os.path.join(output_dir, f"{map_name}.bmp")
    veg_path = os.path.join(output_dir, f"{map_name}_veg.bmp")
    spawn_path = os.path.join(output_dir, f"{map_name}_ZombieSpawnMap.bmp")
    preview_path = os.path.join(output_dir, f"{map_name}_preview.png")
    buildings_path = os.path.join(output_dir, f"{map_name}_buildings.geojson")
    meta_path = os.path.join(output_dir, f"{map_name}_info.json")
    cells_x, cells_y = proj.cell_grid()
    metadata = {
        "map_name": map_name,
        "bbox": {"south": south, "west": west, "north": north, "east": east},
        "rotation": rotation,
        "osm_cache": osm_cache,
        "straight_roads": bool(straight_roads),
        "osm_bbox": list(osm_bbox) if osm_bbox else None,
        "shape": shape,
        "meters_per_tile": meters_per_tile,
        "width_tiles": proj.width,
        "height_tiles": proj.height,
        "cells_x": cells_x,
        "cells_y": cells_y,
        "spawn_density_max": spawn_density,
        "building_count": kept["building_count"],
        "feature_count": kept["feature_count"],
        "houses_from_addresses": houses_from_addresses,
        "houses_from_streets": houses_from_streets,
        "guide_reference": "Thuztor Mapping Guide v0.2",
        "grid": proj.grid_dict(),
        "biome": {"climate": climate, "open": open_biome,
                  "cell_tiles": biomes.BIOME_CELL},
        "terrain": "native",
    }

    def save_json(path, make_data, **options):
        text = json.dumps(make_data(), **options)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    # Buildings, areas, roads, fences, zones, and the smaller side files
    # were written before the draw. What is left needs the bitmaps.
    output_jobs = {
        "metadata": lambda: save_json(meta_path, lambda: metadata, indent=2),
    }
    landscape = Image.open(landscape_path).convert("RGB")
    vegetation = Image.open(veg_path).convert("RGB")
    R._queue_view_file(landscape, vegetation, preview_path,
                       None if on_view is None else (lambda frame: on_view(frame)),
                       lines=kept["preview_lines"])
    workers = R._half_cores() if file_workers is None else max(1, int(file_workers))
    file_total = len(output_jobs) + 1
    group_left = {"biomes": 3}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="knox-output") as executor:
        pending = {}
        R._queue_png(executor, pending, biome_image,
                     os.path.join(output_dir, f"{map_name}_biome.png"),
                     "L", "biomes", workers)
        R._queue_png(executor, pending, biome_image,
                     os.path.join(output_dir, f"{map_name}_biome_overlay.png"),
                     "overlay", "biomes", workers)
        pending[executor.submit(R._write_biome_legend, output_dir, map_name)] = ("group", "biomes")
        for name, job in output_jobs.items():
            pending[executor.submit(job)] = ("job", name)
        completed = 0
        while pending:
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                kind = pending.pop(future)
                result = future.result()
                if kind[0] == "job":
                    completed += 1
                elif kind[0] == "group":
                    group_left[kind[1]] -= 1
                    if group_left[kind[1]] == 0:
                        completed += 1
                else:
                    state, index = kind[1], kind[2]
                    state.parts[index] = result
                    state.left -= 1
                    if state.left == 0:
                        R._write_banded_png(state)
                        group_left[state.group] -= 1
                        if group_left[state.group] == 0:
                            completed += 1
                show(None, force=True,
                     stage=f"Writing map files, {completed} of {file_total}",
                     work=0.98 + 0.02 * completed / max(1, file_total))
    return R.RenderResult(
        landscape_path=landscape_path, vegetation_path=veg_path,
        spawn_map_path=spawn_path, preview_path=preview_path,
        buildings_geojson_path=buildings_path, meta_path=meta_path,
        width=proj.width, height=proj.height, cells_x=cells_x, cells_y=cells_y)
