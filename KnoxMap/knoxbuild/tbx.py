"""Serialise a Plan to BuildingEd's .tbx format.

Schema taken from BuildingWriter in timbaker/buildinged. Two index conventions
differ and are easy to get backwards:

    tile entries   1-based, and 0 means "none"   (entryIndex)
    furniture      0-based, no null form         (furnitureIndex)

Element order inside <building> follows the writer: tile entries, furniture,
user_tiles, used_tiles, used_furniture, the <room> list, then <floor>.
"""
from __future__ import annotations

import functools
import random
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from . import catalog as C
from .grids import erode, rooms_text
from .layout import ROOM_STYLE, Building, Plan, _erika_ready, roof_rects

# Version 4 is the first that carries a per-room Ceiling tile. Writing 3 still
# loads - the reader accepts 1..7 - but then it silently back-fills ceilings
# itself, so we may as well state them.
VERSION = 4

# A tile is interior when its whole 3x3 neighbourhood is roof. erode's default
# footprint steps in by two; this one is the neighbourhood the plant used.
_ROOF_INTERIOR = np.ones((3, 3), dtype=bool)


_QUOTE: dict[str, str] = {}
_ENTRY_XML: dict[tuple, str] = {}
# The shared catalog rows are only read while a town is written, so their
# XML is built on the first building and reused.
_BASE_ENTRY_XML: list[str] | None = None


def _quote(value: str) -> str:
    """quoteattr, without the escape scan for the usual tile and enum names.

    quoteattr wraps in double quotes when the text has no ``&``, ``<``, ``>``
    or ``"``. Anything else, including a quote or an ampersand, goes through
    quoteattr so the escaping stays its.
    """
    hit = _QUOTE.get(value)
    if hit is not None:
        return hit
    if any(ch in "&<>\"" for ch in value):
        hit = quoteattr(value)
    else:
        hit = f'"{value}"'
    _QUOTE[value] = hit
    return hit


def _attrs(pairs: list[tuple[str, object]]) -> str:
    # Nearly every value is a coordinate or an index. Quoting those through
    # quoteattr, tens of millions of calls for a town, was most of the time
    # spent writing buildings; numbers never need escaping.
    parts = []
    append = parts.append
    quote = _quote
    for key, value in pairs:
        if type(value) is int:
            append(f' {key}="{value}"')
        elif type(value) is str:
            append(f" {key}={quote(value)}")
        else:
            append(f" {key}={quote(str(value))}")
    return "".join(parts)


def _entry_xml(entry: dict) -> str:
    tiles = entry["tiles"]
    items = tuple(tiles.items())
    key = (entry["category"], items)
    hit = _ENTRY_XML.get(key)
    if hit is not None:
        return hit
    lines = [f" <tile_entry category={_quote(entry['category'])}>"]
    for enum_name, tile in items:
        lines.append(f"  <tile enum={_quote(enum_name)} tile={_quote(tile)}/>")
    lines.append(" </tile_entry>")
    hit = "\n".join(lines)
    _ENTRY_XML[key] = hit
    return hit


def _base_entry_xml() -> list[str]:
    global _BASE_ENTRY_XML
    if _BASE_ENTRY_XML is None:
        _BASE_ENTRY_XML = [_entry_xml(entry) for entry in C.TILE_ENTRIES]
    return _BASE_ENTRY_XML


def _fingerprint(entry: dict) -> tuple:
    """Category plus tiles in key order. The first copy of a fingerprint wins."""
    return (entry["category"], tuple(sorted(entry["tiles"].items())))


def _remember(index: dict[tuple, int], entry: dict, at: int) -> None:
    index.setdefault(_fingerprint(entry), at)


def _add(entries: list[dict], index: dict[tuple, int], entry: dict | None) -> int:
    """The 1-based index of `entry` in the tile-entry table, appending it if it
    is not there yet; 0, BuildingEd's "none", for no entry."""
    if not entry:
        return 0
    key = _fingerprint(entry)
    found = index.get(key)
    if found is not None:
        return found
    entries.append(entry)
    index[key] = len(entries)
    return len(entries)


def _append_entry(entries: list[dict], index: dict[tuple, int], entry: dict) -> None:
    """Append even when the same tiles are already in the table.

    Style walls and the per-building roof cap are always their own rows.
    Lookup still resolves to the first copy, which is what a scan used to do.
    """
    entries.append(entry)
    _remember(index, entry, len(entries))


@functools.lru_cache(maxsize=None)
def _furniture_xml(role: str) -> str:
    """One <furniture> block for this role, built once.

    BuildingWriter only writes a layer when it is not the default. Leaving
    it off a wall piece drops it onto the floor layer, which is how light
    switches, paintings and mirrors ended up standing mid-room.

    All four facings, in the order BuildingWriter emits them. Writing only
    W and N leaves the E/S slots empty, so any object facing that way
    renders as nothing at all.
    """
    layer = C.FURNITURE_LAYERS.get(role, "Furniture")
    lines = [" <furniture>" if layer == "Furniture"
             else f" <furniture layer={quoteattr(layer)}>"]
    for orient in ("W", "N", "E", "S"):
        tiles = C.FURNITURE[role].get(orient)
        if not tiles:
            continue
        lines.append(f'  <entry orient="{orient}">')
        for key, tile in tiles.items():
            dx, dy = key.split(",")
            lines.append(f'   <tile x="{dx}" y="{dy}" name={quoteattr(tile)}/>')
        lines.append("  </entry>")
    lines.append(" </furniture>")
    return "\n".join(lines)


# A pitched roof needs room for two slopes; a narrower strip of a house (a
# porch, one step of a turned footprint) keeps a flat roof.
PEAK_MIN_TILES = 4
# The 30-degree roofs the game's own houses wear, which BuildingEd sizes to an
# odd number of tiles across, 3 to 11; a house up to two tiles wider takes an
# 11 and a flat strip. Wider than that, the steep 45-degree gable with a flat
# top in the middle is all the editor offers.
PEAK30_MAX_ACROSS = 11
HIP_MAX_ACROSS = 7
_PEAK_DEPTHS = {1: "Point5", 2: "One", 3: "OnePoint5", 4: "Two", 5: "TwoPoint5"}
_NO_CAPS = {"cappedW": False, "cappedN": False, "cappedE": False, "cappedS": False}


def _peak_depth(across: int) -> str:
    """BuildingEd's depth for a 45-degree peaked roof this many tiles across."""
    return _PEAK_DEPTHS.get(across, "Three")


def _roof_pieces(rects, peaked: bool, roof30: bool = True):
    """(RoofType, Depth, caps, (x, y, w, h)) for each roof over a footprint.

    Flat roofs are never capped: at depth three a cap is a storey-high wall of
    roof tiles laid over the top storey's own walls. A pitched roof is capped
    at its gable ends where they are the outside of the house, and left open
    where it runs into the next roof.

    30-degree roofs need an odd width across their slopes. On an even one the
    roof covers one tile less and a flat strip takes the last row, on the side
    away from the camera (north or west) where it shows least. A house that is
    a single rectangle gets a hip roof about half the time, as Knox County's
    do; an L-shape keeps gables, whose ends meet cleanly.
    """
    out = []
    single = len(rects) == 1
    for rx, ry, rw, rh, caps in rects:
        across = min(rw, rh)
        if not peaked or across < PEAK_MIN_TILES:
            out.append(("FlatTop", "Three", dict(_NO_CAPS), (rx, ry, rw, rh)))
            continue
        along_x = rw >= rh
        if not roof30 or across > PEAK30_MAX_ACROSS + 2:
            cap = dict(_NO_CAPS)
            if along_x:
                cap["cappedW"], cap["cappedE"] = caps["cappedW"], caps["cappedE"]
                out.append(("PeakWE", _peak_depth(rh), cap, (rx, ry, rw, rh)))
            else:
                cap["cappedN"], cap["cappedS"] = caps["cappedN"], caps["cappedS"]
                out.append(("PeakNS", _peak_depth(rw), cap, (rx, ry, rw, rh)))
            continue
        odd = across if across % 2 else across - 1
        # Hips only on small houses: across a wide one the four slopes meet
        # in a tall point that looks like a hat.
        hip = single and odd <= HIP_MAX_ACROSS and (rx * 31 + ry * 17 + rw * 7 + rh) % 2 == 0
        cap = dict(_NO_CAPS)
        if along_x:
            box = (rx, ry + (rh - odd), rw, odd)
            if rh != odd:
                out.append(("FlatTop", "Three", dict(_NO_CAPS), (rx, ry, rw, rh - odd)))
            if not hip:
                cap["cappedW"], cap["cappedE"] = caps["cappedW"], caps["cappedE"]
            out.append(("Peak30Quad" if hip else "Peak30WE", "Zero", cap, box))
        else:
            box = (rx + (rw - odd), ry, odd, rh)
            if rw != odd:
                out.append(("FlatTop", "Three", dict(_NO_CAPS), (rx, ry, rw - odd, rh)))
            if not hip:
                cap["cappedN"], cap["cappedS"] = caps["cappedN"], caps["cappedS"]
            out.append(("Peak30Quad" if hip else "Peak30NS", "Zero", cap, box))
    return out


# Flat roofs carry plant: bare tar from edge to edge read as unfinished. One
# air-conditioning unit per ROOF_AC_EVERY tiles of roof, a vent per
# ROOF_VENT_EVERY, and a hatch, kept off the edges and off each other.
ROOF_MIN_TILES = 60
ROOF_AC_EVERY = 90
ROOF_VENT_EVERY = 60


def _rooftop(grid, width: int, height: int) -> list[tuple[str, int, int, str]]:
    g = np.asarray(grid)
    ys, xs = np.nonzero(erode(g, _ROOF_INTERIOR))
    # Row-major (x, y), the order the old y-then-x scan handed to the shuffle.
    tiles = list(zip(xs.tolist(), ys.tolist()))
    area = int(np.count_nonzero(g))
    if area < ROOF_MIN_TILES or not tiles:
        return []
    rng = random.Random(width * 7919 + height * 104729 + area)
    wanted = (["roof_hatch"] + ["roof_ac"] * max(1, area // ROOF_AC_EVERY)
              + ["roof_vent"] * (area // ROOF_VENT_EVERY))
    taken: set[tuple[int, int]] = set()
    out = []
    rng.shuffle(tiles)
    for role in wanted:
        for x, y in tiles:
            if any((x + dx, y + dy) in taken for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                continue
            taken.add((x, y))
            out.append((role, x, y, rng.choice(("W", "N"))))
            break
    return out


def _storefront_runs(storey, doors) -> list[tuple[str, int, int, int]]:
    """The ground floor's shop glass as straight runs of wall: (edge dir,
    fixed coordinate, first tile along, length). A run spans from its first
    glazed tile to its last, taking in the doors and the bits of wall beside
    them, and stops wherever the line is no longer an outside wall."""
    grid = np.asarray(storey.grid)
    height, width = grid.shape

    def cell(x, y):
        if 0 <= x < width and 0 <= y < height:
            return int(grid[y, x])
        return 0

    def outside(d, fixed, t):
        x, y = (fixed, t) if d == "W" else (t, fixed)
        a, b = (cell(x - 1, y), cell(x, y)) if d == "W" else (cell(x, y - 1), cell(x, y))
        return bool(a) != bool(b)

    lines: dict[tuple[str, int], list[int]] = {}
    for x, y, d in storey.shop_front:
        lines.setdefault((d, x if d == "W" else y), []).append(y if d == "W" else x)
    door_at = {(d, x if d == "W" else y, y if d == "W" else x) for x, y, d in doors}
    runs = []
    for (d, fixed), along in sorted(lines.items()):
        lo, hi = min(along), max(along)
        # A shop door just past the last pane belongs to the shop front too.
        for step in (-1, 1):
            end = lo if step < 0 else hi
            for k in (1, 2):
                if (d, fixed, end + k * step) in door_at and all(
                        outside(d, fixed, end + i * step) for i in range(1, k + 1)):
                    end += k * step
                    break
            lo, hi = (end, hi) if step < 0 else (lo, end)
        start = None
        for t in range(lo, hi + 2):
            if t <= hi and outside(d, fixed, t):
                start = t if start is None else start
            elif start is not None:
                runs.append((d, fixed, start, t - start))
                start = None
    return runs


def render_tbx(plan: Plan | Building, name: str,
               style: dict | None = None) -> str:
    """Return the complete .tbx document for a plan or a stack of them.

    `style` supplies this building's own exterior and interior wall materials,
    and optionally a floor that overrides the usual per-room palette. Each .tbx
    carries its own tile-entry table, so styles cost nothing globally: the
    style's entries are simply appended after the shared ones and referenced by
    their new indices.
    """
    building = plan if isinstance(plan, Building) else Building(
        width=plan.width, height=plan.height, storeys=[plan])
    storeys = building.storeys

    entries = list(C.TILE_ENTRIES)
    entry_index: dict[tuple, int] = {}
    for at, entry in enumerate(entries, start=1):
        _remember(entry_index, entry, at)
    exterior_idx = C.EXTERIOR_WALL
    interior_idx = C.INTERIOR_WALL
    floor_override = None
    window_idx = C.WINDOW
    roof_cap_idx = C.ROOF_CAP
    curtains_idx = C.CURTAINS
    front_idx = front_curtains = None
    slope_idx, top_idx, peaked = C.ROOF_SLOPE, C.ROOF_TOP, False
    trim_idx = shutters_idx = grime_idx = 0
    roof30 = False

    if style:
        _append_entry(entries, entry_index, style["exterior"])
        exterior_idx = len(entries)
        _append_entry(entries, entry_index, style["interior"])
        interior_idx = len(entries)
        if style.get("floor"):
            _append_entry(entries, entry_index, style["floor"])
            floor_override = len(entries)
        if style.get("window"):
            _append_entry(entries, entry_index, style["window"])
            window_idx = len(entries)
        curtains_idx = _add(entries, entry_index, style.get("curtains")) if "curtains" in style else C.CURTAINS
        # Taller buildings of a kind take bigger windows: the last row whose
        # storey count this building reaches.
        for levels, entry, curtains in style.get("windows_by_levels") or ():
            if len(storeys) >= levels and levels > 1:
                window_idx = _add(entries, entry_index, entry)
                curtains_idx = _add(entries, entry_index, curtains)
        trim_idx = _add(entries, entry_index, style.get("trim"))
        shutters_idx = _add(entries, entry_index, style.get("shutters"))
        grime_idx = _add(entries, entry_index, style.get("grime"))
        if style.get("roof"):
            roof = style["roof"]
            if roof.get("slopes"):
                slope_idx = _add(entries, entry_index, roof["slopes"])
            top_idx = _add(entries, entry_index, roof["tops"])
            peaked = bool(roof.get("peaked"))
            # 30-degree roofs only where the gable ends have 30-degree tiles.
            roof30 = (
                "CapPeak30S1" in ((roof.get("caps") or {}).get("tiles") or {})
                and "Slope30S1" in ((roof.get("slopes") or {}).get("tiles") or {})
            )
        if style.get("shop_front"):
            entry, curtains = style["shop_front"]
            front_idx = _add(entries, entry_index, entry)
            front_curtains = _add(entries, entry_index, curtains)
    # With Erika's Tiles, a shop front is a wall of glass in a painted frame,
    # not windows set in brick, and a sign hangs over it.
    runs: list[tuple[str, int, int, int]] = []
    glazed: set[tuple[int, int, str]] = set()
    user_tiles: dict[int, dict[tuple[int, int], str]] = {}
    store_ext = store_int = store_door = 0
    if front_idx and C.ERIKA_STOREFRONTS and storeys[0].shop_front and _erika_ready():
        rng = random.Random(name)
        ext, inte, door = rng.choice(C.ERIKA_STOREFRONTS)
        store_ext = _add(entries, entry_index, ext)
        store_int = _add(entries, entry_index, inte)
        store_door = _add(entries, entry_index, door)
        # A one-tile run is a step in a slanted wall, not a shop window.
        runs = [r for r in _storefront_runs(storeys[0], storeys[0].doors) if r[3] >= 2]
        for d, fixed, start, length in runs:
            glazed.update((fixed, t, d) if d == "W" else (t, fixed, d)
                          for t in range(start, start + length))
        # The sign goes on the longest run the street can see, one storey up
        # so it hangs above the glass. Only the south and east faces are
        # seen: a sign on a north or west wall is drawn on its inner side.
        grid = np.asarray(storeys[0].grid)
        gh, gw = int(grid.shape[0]), int(grid.shape[1])
        seen = [r for r in runs
                if (r[0] == "N" and (r[1] >= gh or not grid[r[1], r[2]]))
                or (r[0] == "W" and (r[1] >= gw or not grid[r[2], r[1]]))]
        if seen:
            d, fixed, start, length = max(seen, key=lambda r: r[3])
            fits = [s for s in C.ERIKA_SIGNS.get(d, ()) if len(s) <= length]
            if fits:
                sign = rng.choice(fits)
                first = start + (length - len(sign)) // 2
                user_tiles[1] = {((fixed, first + j) if d == "W" else (first + j, fixed)): tile
                                 for j, tile in enumerate(sign)}
    # A depth-three flat roof walls in its storey with the cap entry's
    # CapGap tiles, which BuildingTemplates.txt sets to stucco - every top
    # floor came out stucco whatever the building was made of. Give each
    # building a cap entry whose gaps are its own exterior wall.
    ext_tiles = entries[exterior_idx - 1]["tiles"]
    if ext_tiles.get("West") and ext_tiles.get("North"):
        base = ((style or {}).get("roof") or {}).get("caps") or entries[C.ROOF_CAP - 1]
        cap = dict(base["tiles"])
        cap["CapGapE3"], cap["CapGapS3"] = ext_tiles["West"], ext_tiles["North"]
        _append_entry(entries, entry_index, {"category": "roof_caps", "tiles": cap})
        roof_cap_idx = len(entries)

    rooftop = [] if peaked else _rooftop(storeys[-1].grid, building.width, building.height)
    # Which furniture roles this building actually uses, in first-use order.
    # The index is 0-based, which is how furniture entries are referenced.
    role_to_idx: dict[str, int] = {}
    for storey in storeys:
        for role, _x, _y, _o in storey.furniture:
            if role not in role_to_idx:
                role_to_idx[role] = len(role_to_idx)
    for role, _x, _y, _o in rooftop:
        if role not in role_to_idx:
            role_to_idx[role] = len(role_to_idx)

    out: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>']

    building_attrs = [
        ("version", VERSION),
        ("width", building.width),
        ("height", building.height),
        ("ExteriorWall", exterior_idx),
        ("ExteriorWallTrim", trim_idx),
        ("Door", C.DOOR),
        ("DoorFrame", C.DOOR_FRAME),
        ("Window", window_idx),
        ("Curtains", curtains_idx),
        ("Shutters", shutters_idx),
        ("Stairs", C.STAIRS),
        ("RoofCap", roof_cap_idx),
        ("RoofSlope", slope_idx),
        ("RoofTop", top_idx),
        ("GrimeWall", grime_idx),
    ]
    out.append(f"<building{_attrs(building_attrs)}>")

    base_n = len(C.TILE_ENTRIES)
    if len(entries) >= base_n and all(entries[i] is C.TILE_ENTRIES[i] for i in range(base_n)):
        out.extend(_base_entry_xml())
        extra = entries[base_n:]
    else:
        extra = entries
    for entry in extra:
        out.append(_entry_xml(entry))

    for role in role_to_idx:
        out.append(_furniture_xml(role))

    names = sorted({t for tiles in user_tiles.values() for t in tiles.values()})
    name_to_idx = {tile: i + 1 for i, tile in enumerate(names)}
    if names:
        out.append(" <user_tiles>")
        out.extend(f"  <tile tile={_quote(t)}/>" for t in names)
        out.append(" </user_tiles>")

    def user_tile_layer(level: int) -> str | None:
        tiles = user_tiles.get(level)
        if not tiles:
            return None
        cols, rows = building.width + 1, building.height + 1
        layer = np.zeros((rows, cols), dtype=np.int32)
        for (x, y), tile in tiles.items():
            if 0 <= x < cols and 0 <= y < rows:
                layer[y, x] = name_to_idx[tile]
        return '  <tiles layer="WallFurniture">' + escape(rooms_text(layer)) + "</tiles>"

    used = " ".join(str(i) for i in range(1, len(entries) + 1))
    out.append(f" <used_tiles>{used}</used_tiles>")
    used_f = " ".join(str(i) for i in range(len(role_to_idx)))
    out.append(f" <used_furniture>{used_f}</used_furniture>")

    for room in building.rooms:
        floor_idx, _label, _ = ROOM_STYLE[room.kind]
        # The shipped templates set Name identical to InternalName; the room
        # name is what reaches the game's room definitions, so don't get
        # creative with it.
        room_attrs = [
            ("Name", room.kind),
            ("InternalName", room.kind),
            ("Color", C.ROOM_COLORS[room.kind]),
            ("InteriorWall", interior_idx),
            ("InteriorWallTrim", C.INTERIOR_WALL_TRIM),
            ("Floor", floor_override or floor_idx),
            ("GrimeFloor", 0),
            ("GrimeWall", 0),
            ("Ceiling", C.CEILING),
        ]
        out.append(f" <room{_attrs(room_attrs)}/>")

    attic: list[str] = []
    for level, storey in enumerate(storeys):
        out.append(" <floor>")

        for x, y, direction in storey.doors:
            # In the shop glass: a glass door, framed by the glass wall itself.
            shop_door = level == 0 and (x, y, direction) in glazed
            attrs = [("type", "door"), ("FrameTile", 0 if shop_door else C.DOOR_FRAME),
                     ("x", x), ("y", y), ("dir", direction),
                     ("Tile", store_door if shop_door else C.DOOR)]
            out.append(f"  <object{_attrs(attrs)}/>")

        if level == 0:
            for d, fixed, start, length in runs:
                x, y = (fixed, start) if d == "W" else (start, fixed)
                # A wall object runs along y when its dir is N, placing west
                # walls, and along x when it is W.
                attrs = [("type", "wall"), ("length", length), ("InteriorTile", store_int),
                         ("ExteriorTrim", 0), ("InteriorTrim", 0), ("x", x), ("y", y),
                         ("dir", "N" if d == "W" else "W"), ("Tile", store_ext)]
                out.append(f"  <object{_attrs(attrs)}/>")

        for x, y, direction in storey.windows:
            if level == 0 and (x, y, direction) in glazed:
                continue                        # the glass wall is the window
            tile, curtains = window_idx, curtains_idx
            if front_idx and (x, y, direction) in getattr(storey, "shop_front", ()):
                tile, curtains = front_idx, front_curtains
            attrs = [("type", "window"), ("CurtainsTile", curtains),
                     ("ShuttersTile", 0 if tile == front_idx else shutters_idx),
                     ("x", x), ("y", y),
                     ("dir", direction), ("Tile", tile)]
            out.append(f"  <object{_attrs(attrs)}/>")

        for role, x, y, orient in storey.furniture:
            attrs = [("type", "furniture"),
                     ("FurnitureTiles", role_to_idx[role]),
                     ("orient", orient), ("x", x), ("y", y)]
            out.append(f"  <object{_attrs(attrs)}/>")

        # A staircase belongs to the storey it rises from; the reader puts the
        # opening in the floor above by itself. Only N and W are valid
        # directions - Stairs::bounds returns an empty rect for anything else,
        # and an empty rect is treated as an invalid object.
        if level < len(building.stairs):
            sx, sy, sdir = building.stairs[level]
            attrs = [("type", "stairs"), ("x", sx), ("y", sy),
                     ("dir", sdir), ("Tile", C.STAIRS)]
            out.append(f"  <object{_attrs(attrs)}/>")

        # Roofs on the top storey only - one on each would bury every floor
        # below under a ceiling of roof tiles - and shaped to the footprint
        # rather than its bounding box, so an L-shaped building does not carry
        # a roof over its own back yard.
        # A roof over whatever of this storey has no storey above it: the
        # whole top floor, and the ledge where a tower steps back.
        storey_grid = np.asarray(storey.grid)
        if level + 1 < len(storeys):
            above = np.asarray(storeys[level + 1].grid)
            exposed = np.where(above != 0, np.int32(0), storey_grid)
        else:
            above = None
            exposed = storey_grid
        if np.any(exposed):
            rects = roof_rects(exposed)
            for roof_type, depth, cap, (rx, ry, rw, rh) in _roof_pieces(
                    rects, peaked and above is None, roof30):
                roof_attrs = [
                    ("type", "roof"),
                    ("width", rw),
                    ("height", rh),
                    ("RoofType", roof_type),
                    ("Depth", depth),
                    ("cappedW", str(cap["cappedW"]).lower()),
                    ("cappedN", str(cap["cappedN"]).lower()),
                    ("cappedE", str(cap["cappedE"]).lower()),
                    ("cappedS", str(cap["cappedS"]).lower()),
                    ("CapTiles", roof_cap_idx),
                    ("SlopeTiles", slope_idx),
                    ("TopTiles", top_idx),
                    ("x", rx), ("y", ry),
                ]
                line = f"  <object{_attrs(roof_attrs)}/>"
                # BuildingEd measures a roof's height up from the floor it is
                # on. A flat roof at depth three on the top storey ends level
                # with its ceiling, which is right; a pitched roof there rose
                # through the top storey, its gable ends covering the upstairs
                # walls. Pitched roofs go on the roof floor above instead.
                (attic if roof_type != "FlatTop" else out).append(line)

        # rooms_text is the BuildingWriter grid: a comma after every cell but
        # the last, and a newline after every row. escape() is applied here.
        out.append("  <rooms>" + escape(rooms_text(np.asarray(building.grid_for(level)))) + "</rooms>")
        if (layer := user_tile_layer(level)):
            out.append(layer)

        out.append(" </floor>")

    # The roof floor: no rooms. It holds the pitched roofs, and the flat roof
    # tops BuildingEd places here from the depth-three roofs below.
    empty_rooms = escape(rooms_text(np.zeros((building.height, building.width), dtype=np.int32)))
    out.append(" <floor>")
    out.extend(attic)
    for role, x, y, orient in rooftop:
        attrs = [("type", "furniture"), ("FurnitureTiles", role_to_idx[role]),
                 ("orient", orient), ("x", x), ("y", y)]
        out.append(f"  <object{_attrs(attrs)}/>")
    out.append("  <rooms>" + empty_rooms + "</rooms>")
    if (layer := user_tile_layer(len(storeys))):
        out.append(layer)
    out.append(" </floor>")
    if attic:
        # A pitched roof's top rises a full storey above the roof floor, and
        # BuildingEd lays what is up there on the floor above that.
        out.append(" <floor>")
        out.append("  <rooms>" + empty_rooms + "</rooms>")
        out.append(" </floor>")
    out.append("</building>")
    return "\n".join(out) + "\n"
