"""worldmap.xml.bin: the paper map in the form Build 42 actually reads.

The in-game map (M) showed street names and nothing else - no roads, no
buildings, no water. worldmap.xml was being read, but Build 42 no longer
loads that format properly: its XML reader stores each outline's length in
coordinates rather than points, reads twice as far as it wrote, and every
outline fails with an IndexOutOfBoundsException (console.txt: "Error while
parsing xml element: geometry"). The game's own maps, and every working
Build 42 map mod, ship the binary file beside the XML, and when it is there
the game reads that instead. It also uses the new 256-tile cells, where the
XML is in the old 300-tile ones.

The layout, from the game's WorldMapBinary reader and checked against
Muldraugh's own file (every byte of it parses):

    "IGMB"  int version = 2  int cell size = 256
    int width, int height                  cells, counted from cell 0, 0
    int n, n x (short length, UTF-8 bytes) the string table
    width x height cells, row by row:
        int -1                             no data in this cell, or
        int x, int y, int features, then per feature:
            short type (a string index: "Polygon")
            byte rings, per ring: short points, points x (short x, short y)
            byte properties, per property: short key, short value (indices)

Points are in tiles from the cell's corner. Everything is little-endian.

The binary is converted from worldmap.xml, so a map built before this gets
its paper map back when it is installed again.
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import Polygon, box
from shapely.validation import make_valid

XML_CELL = 300
BIN_CELL = 256
# Cells of one outline clipped in one shapely.intersection call.
_CLIP_BATCH = 128


def _features(xml_path: str):
    """(rings in world tiles, [(key, value)]) for every feature in the XML."""
    root = ET.parse(xml_path).getroot()
    for cell in root.iter("cell"):
        ox = int(cell.get("x")) * XML_CELL
        oy = int(cell.get("y")) * XML_CELL
        for feature in cell.iter("feature"):
            geometry = feature.find("geometry")
            if geometry is None or geometry.get("type", "Polygon") != "Polygon":
                continue
            rings = []
            for coords in geometry.iter("coordinates"):
                ring = [(ox + int(float(p.get("x"))), oy + int(float(p.get("y"))))
                        for p in coords.iter("point")]
                if len(ring) >= 3:
                    rings.append(ring)
            if not rings:
                continue
            props = [(p.get("name"), p.get("value"))
                     for p in feature.iter("property") if p.get("name")]
            yield rings, props


# A polygon smaller than this draws as nothing and is not worth the risk.
MIN_AREA = 1.0


def _polygons(shape):
    """Every polygon in whatever a clip or a repair handed back. make_valid
    can answer with a collection holding loose lines beside the shapes."""
    if shape.geom_type == "Polygon":
        if not shape.is_empty:
            yield shape
    elif hasattr(shape, "geoms"):
        for part in shape.geoms:
            yield from _polygons(part)


def _ring(points) -> list | None:
    """One ring at whole tiles, or None if rounding has collapsed it."""
    out = []
    for x, y in points:
        point = (round(x), round(y))
        if not out or point != out[-1]:
            out.append(point)
    while len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out if len(set(out)) >= 3 else None


def _clean(rings) -> list[list]:
    """Rings rounded to whole tiles, checked again after the rounding.

    Rounding is what breaks them: corners land on the same tile and the shape
    collapses, or edges cross. The game cannot triangulate either, and throws
    in WorldMapRenderer.fillPolygon when the map is zoomed out.
    """
    tidy = [r for r in (_ring(ring) for ring in rings) if r]
    if not tidy:
        return []
    try:
        shape = Polygon(tidy[0], tidy[1:])
        if not shape.is_valid:
            # make_valid over buffer(0): buffer keeps only the lobes that wind
            # the right way, so half of a crossed outline would disappear.
            shape = make_valid(shape)
    except Exception:  # noqa: BLE001 - a broken outline is left off, not fatal
        return []
    out = []
    for part in _polygons(shape):
        if part.area < MIN_AREA:
            continue
        piece = [r for r in (_ring(ring.coords)
                             for ring in [part.exterior, *part.interiors]) if r]
        if not piece:
            continue
        # A repair can put a corner on a half tile, so the rounding above may
        # have broken it again. Whatever is left is what gets written.
        try:
            final = Polygon(piece[0], piece[1:])
        except Exception:  # noqa: BLE001
            continue
        if final.is_valid and not final.is_empty and final.area >= MIN_AREA:
            out.append(piece)
    return out


def _polygons_to_pieces(clipped, x0, y0):
    """Ring lists for the polygons in one clipped geometry, cell-local."""
    out = []
    for part in _polygons(clipped):
        out.extend(_clean([[(x - x0, y - y0) for x, y in ring.coords]
                           for ring in [part.exterior, *part.interiors]]))
    return out


def _pieces(rings, cell_x, cell_y):
    """The part of a polygon inside one 256-tile cell, as ring lists."""
    x0, y0 = cell_x * BIN_CELL, cell_y * BIN_CELL
    try:
        shape = Polygon(rings[0], rings[1:])
        if not shape.is_valid:
            shape = make_valid(shape)
        clipped = shape.intersection(box(x0, y0, x0 + BIN_CELL, y0 + BIN_CELL))
        return _polygons_to_pieces(clipped, x0, y0)
    except Exception:  # noqa: BLE001 - a broken outline is left off, not fatal
        return []


def _sorted_unique(cells: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(cx, cy) with cy changing slowest, matching the old cell walk."""
    cells.sort(key=lambda c: (c[1], c[0]))
    out = []
    prev = None
    for cell in cells:
        if cell != prev:
            out.append(cell)
            prev = cell
    return out


def _assign_feature_cells(multi) -> dict[int, list[tuple[int, int]]]:
    """Cells each multi-cell outline crosses, from an STRtree of its bounds.

    `multi` rows are (loaded index, minx, miny, maxx, maxy, cx0, cx1, cy0, cy1).
    cx1 and cy1 are inclusive, as `max // BIN_CELL`.
    """
    assigned: dict[int, list[tuple[int, int]]] = {}
    if not multi:
        return assigned
    keys: list[tuple[int, int]] = []
    index_of: dict[tuple[int, int], int] = {}
    spans = {}
    bounds = np.empty((len(multi), 4), dtype=np.float64)
    for i, (idx, minx, miny, maxx, maxy, cx0, cx1, cy0, cy1) in enumerate(multi):
        bounds[i] = (minx, miny, maxx, maxy)
        rx0, rx1 = max(0, cx0), cx1 + 1
        ry0, ry1 = max(0, cy0), cy1 + 1
        spans[idx] = (rx0, rx1, ry0, ry1)
        if rx0 >= rx1 or ry0 >= ry1:
            continue
        for cy in range(ry0, ry1):
            for cx in range(rx0, rx1):
                key = (cx, cy)
                if key not in index_of:
                    index_of[key] = len(keys)
                    keys.append(key)
    hits: dict[int, list[tuple[int, int]]] = {}
    if keys:
        xs = np.array([cx * BIN_CELL for cx, _cy in keys], dtype=np.float64)
        ys = np.array([cy * BIN_CELL for _cx, cy in keys], dtype=np.float64)
        cell_boxes = shapely.box(xs, ys, xs + BIN_CELL, ys + BIN_CELL)
        try:
            tree = STRtree(shapely.box(bounds[:, 0], bounds[:, 1], bounds[:, 2], bounds[:, 3]))
            pairs = np.asarray(tree.query(cell_boxes, predicate="intersects"))
        except Exception:  # noqa: BLE001 - the span below is the same cell walk
            pairs = np.empty((2, 0), dtype=np.int64)
        if pairs.ndim == 1:
            pairs = np.vstack((np.zeros(pairs.size, dtype=np.int64), pairs))
        if pairs.size:
            for ci, mi in zip(pairs[0].tolist(), pairs[1].tolist()):
                idx = multi[mi][0]
                cx, cy = keys[ci]
                rx0, rx1, ry0, ry1 = spans[idx]
                if rx0 <= cx < rx1 and ry0 <= cy < ry1:
                    hits.setdefault(idx, []).append((cx, cy))
    for idx, (rx0, rx1, ry0, ry1) in spans.items():
        span_n = max(0, rx1 - rx0) * max(0, ry1 - ry0)
        unique = _sorted_unique(hits[idx]) if idx in hits else []
        if len(unique) == span_n:
            if unique:
                assigned[idx] = unique
        elif span_n:
            assigned[idx] = [(cx, cy) for cy in range(ry0, ry1) for cx in range(rx0, rx1)]
    return assigned


def _clip_shape(shape, rings, chunk):
    """Pieces of `shape` inside each (cx, cy), in that order."""
    x0 = np.array([cx * BIN_CELL for cx, _cy in chunk], dtype=np.float64)
    y0 = np.array([cy * BIN_CELL for _cx, cy in chunk], dtype=np.float64)
    boxes = shapely.box(x0, y0, x0 + BIN_CELL, y0 + BIN_CELL)
    try:
        geoms = np.asarray(shapely.intersection(shape, boxes), dtype=object).ravel()
        if geoms.size != len(chunk):
            raise RuntimeError("clip")
    except Exception:  # noqa: BLE001 - one cell at a time still drops a broken outline
        return [((cx, cy), _pieces(rings, cx, cy)) for cx, cy in chunk]
    out = []
    for geom, (cx, cy) in zip(geoms, chunk):
        ox, oy = cx * BIN_CELL, cy * BIN_CELL
        try:
            pieces = _polygons_to_pieces(geom, ox, oy)
        except Exception:  # noqa: BLE001
            pieces = _pieces(rings, cx, cy)
        out.append(((cx, cy), pieces))
    return out


def _append_clipped(cells, rings, props, cell_list) -> None:
    try:
        shape = Polygon(rings[0], rings[1:])
        if not shape.is_valid:
            shape = make_valid(shape)
        if shape.is_empty:
            return
    except Exception:  # noqa: BLE001
        shape = None
    for start in range(0, len(cell_list), _CLIP_BATCH):
        chunk = cell_list[start:start + _CLIP_BATCH]
        if shape is None:
            groups = [((cx, cy), _pieces(rings, cx, cy)) for cx, cy in chunk]
        else:
            groups = _clip_shape(shape, rings, chunk)
        for (cx, cy), pieces in groups:
            for piece in pieces:
                cells.setdefault((cx, cy), []).append((piece, props))


# The game keeps all of a cell's points in one buffer and remembers where each
# outline starts in it as a 16-bit number: past 32767 values - 16383 points -
# the start wraps negative and every outline after it fails to load. Knox
# County's densest cell has about 1600. A packed city centre can have more.
CELL_POINT_BUDGET = 15000
# What goes first when a cell is over it: the small things.
KEEP_ORDER = ("water", "highway", "railway", "building", "natural")


def _points(piece) -> int:
    return sum(len(ring) for ring in piece)


def _within_budget(features: list) -> list:
    """A cell's features, simplified and then thinned until they fit."""
    if sum(_points(p) for p, _ in features) <= CELL_POINT_BUDGET:
        return features
    for tolerance in (0.75, 1.5, 3.0):
        simpler = []
        for piece, props in features:
            try:
                shape = Polygon(piece[0], piece[1:]).simplify(tolerance, preserve_topology=True)
            except Exception:  # noqa: BLE001
                simpler.append((piece, props))
                continue
            if shape.is_empty or shape.geom_type != "Polygon" or len(shape.exterior.coords) < 4:
                continue
            for cleaned in _clean([r.coords for r in [shape.exterior, *shape.interiors]]):
                simpler.append((cleaned, props))
        features = simpler
        if sum(_points(p) for p, _ in features) <= CELL_POINT_BUDGET:
            return features

    def rank(item):
        piece, props = item
        keys = [k for k, _ in props]
        kind = min((KEEP_ORDER.index(k) for k in keys if k in KEEP_ORDER), default=len(KEEP_ORDER))
        xs = [x for x, _ in piece[0]]
        ys = [y for _, y in piece[0]]
        return (kind, -(max(xs) - min(xs)) * (max(ys) - min(ys)))

    kept, total = [], 0
    for item in sorted(features, key=rank):
        n = _points(item[0])
        if total + n <= CELL_POINT_BUDGET:
            kept.append(item)
            total += n
    return kept


def write_bin(xml_path: str, bin_path: str) -> int:
    """Convert worldmap.xml to worldmap.xml.bin. Returns features written."""
    loaded = []
    multi = []
    for rings, props in _features(xml_path):
        xs = [x for x, _ in rings[0]]
        ys = [y for _, y in rings[0]]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        cx0, cx1 = minx // BIN_CELL, maxx // BIN_CELL
        cy0, cy1 = miny // BIN_CELL, maxy // BIN_CELL
        loaded.append((rings, props, cx0, cy0, cx1, cy1))
        if not (cx0 == cx1 and cy0 == cy1):
            multi.append((len(loaded) - 1, minx, miny, maxx, maxy, cx0, cx1, cy0, cy1))
    assigned = _assign_feature_cells(multi)

    cells: dict[tuple[int, int], list] = {}
    for i, (rings, props, cx0, cy0, cx1, cy1) in enumerate(loaded):
        if cx0 == cx1 and cy0 == cy1:
            if cx0 < 0 or cy0 < 0:
                continue
            local = [[(x - cx0 * BIN_CELL, y - cy0 * BIN_CELL) for x, y in r] for r in rings]
            for piece in _clean(local):
                cells.setdefault((cx0, cy0), []).append((piece, props))
            continue
        cell_list = assigned.get(i)
        if not cell_list:
            continue
        _append_clipped(cells, rings, props, cell_list)

    for key in list(cells):
        cells[key] = _within_budget(cells[key])

    strings: dict[str, int] = {}

    def index(text: str) -> int:
        if text not in strings:
            strings[text] = len(strings)
        return strings[text]

    index("Polygon")
    body = bytearray()
    width = max((x for x, _ in cells), default=-1) + 1
    height = max((y for _, y in cells), default=-1) + 1
    count = 0
    for y in range(height):
        for x in range(width):
            features = cells.get((x, y))
            if not features:
                body += struct.pack("<i", -1)
                continue
            features = features[:0x7FFFFFFF]
            body += struct.pack("<iii", x, y, len(features))
            for piece, props in features:
                body += struct.pack("<hB", index("Polygon"), min(len(piece), 255))
                for ring in piece[:255]:
                    ring = ring[:32767]
                    body += struct.pack("<h", len(ring))
                    for px, py in ring:
                        body += struct.pack("<hh", max(-32768, min(32767, px)),
                                            max(-32768, min(32767, py)))
                props = props[:255]
                body += struct.pack("<B", len(props))
                for key, value in props:
                    body += struct.pack("<hh", index(key), index(value or ""))
                count += 1

    head = bytearray(b"IGMB")
    head += struct.pack("<iiii", 2, BIN_CELL, width, height)
    head += struct.pack("<i", len(strings))
    for text in sorted(strings, key=strings.get):
        raw = text.encode("utf-8")
        head += struct.pack("<h", len(raw)) + raw
    with open(bin_path, "wb") as f:
        f.write(head + body)
    return count


def read_bin(bin_path: str) -> dict:
    """The file read back the way the game reads it, for the self-test:
    {(cell x, cell y): [(rings, {key: value})]}."""
    data = open(bin_path, "rb").read()
    pos = 0

    def take(fmt):
        nonlocal pos
        values = struct.unpack_from(fmt, data, pos)
        pos += struct.calcsize(fmt)
        return values if len(values) > 1 else values[0]

    if data[:4] != b"IGMB":
        raise ValueError("invalid format (magic doesn't match)")
    pos = 4
    version, cell, width, height = take("<iiii")
    if version != 2 or cell != 256:
        raise ValueError(f"version {version}, cell size {cell}")
    strings = []
    for _ in range(take("<i")):
        n = take("<h")
        strings.append(data[pos:pos + n].decode("utf-8"))
        pos += n
    out = {}
    for _ in range(width * height):
        x = take("<i")
        if x == -1:
            continue
        y, n = take("<ii")
        feats = []
        for _ in range(n):
            kind = strings[take("<h")]
            rings = []
            for _ in range(take("<B")):
                rings.append([take("<hh") for _ in range(take("<h"))])
            props = {strings[take("<h")]: strings[take("<h")] for _ in range(take("<B"))}
            feats.append((kind, rings, props))
        out[(x, y)] = feats
    if pos != len(data):
        raise ValueError(f"{len(data) - pos} bytes left over")
    return out
