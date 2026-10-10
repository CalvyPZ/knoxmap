"""Which city blocks are paved, from two bit planes.

Street centre lines go into one plane at the carriageway width the ground
painter uses. Building footprints go into the other. Tiles that share a side
and are not street are one block, clipped to the map frame. A block is paved
when footprints cover at least ``DENSE_COVERAGE`` of its tiles and it is
larger than fifty tiles. The planes are released before the paved runs are
returned.

At 7500 by 7500 a plane is 7,035,000 bytes: stride ``(7500 + 7) // 8`` is
938, times 7500 rows. Both planes are live while the blocks are measured, so
the peak is 14,070,000 bytes.
"""
from __future__ import annotations

import array
import math

from . import renderer as R

# The same ways the line-graph pass treated as the walls of a block.
_STREETS = ("road_major", "road_medium", "road_minor", "road_service", "pedestrian")

# Popcount of one byte. The inner loops count footprint tiles by the byte.
_ONES = bytes(bin(i).count("1") for i in range(256))

# A tile centre sits on an integer-and-a-half. Keeping the boundary inclusive
# matches the library's fill, which paints a tile when its centre is inside.
_EDGE = 1e-6


class _Plane:
    """One bit per tile, packed little-endian in each byte. Row stride is a
    whole number of bytes so a span fill never straddles two rows."""

    def __init__(self, width: int, height: int, *, seal: bool) -> None:
        self.width = width
        self.height = height
        self.stride = (width + 7) // 8
        self.data = bytearray(self.stride * height)
        self.hit = False
        spare = width & 7
        if seal and spare:
            # Padding bits read as street, so a scan cannot run off the row.
            mask = (0xFF << spare) & 0xFF
            last = self.stride - 1
            data = self.data
            step = self.stride
            for y in range(height):
                data[y * step + last] = mask

    def drop(self) -> None:
        self.data = bytearray()

    def set(self, x: int, y: int) -> None:
        if x < 0 or y < 0 or x >= self.width or y >= self.height:
            return
        self.data[y * self.stride + (x >> 3)] |= 1 << (x & 7)

    def fill(self, y: int, x0: int, x1: int) -> None:
        """Set tiles x0..x1 inclusive on row y."""
        if y < 0 or y >= self.height:
            return
        if x0 < 0:
            x0 = 0
        if x1 >= self.width:
            x1 = self.width - 1
        if x0 > x1:
            return
        self.hit = True
        data = self.data
        row = y * self.stride
        while x0 <= x1 and (x0 & 7):
            data[row + (x0 >> 3)] |= 1 << (x0 & 7)
            x0 += 1
        while x0 + 7 <= x1:
            data[row + (x0 >> 3)] = 0xFF
            x0 += 8
        while x0 <= x1:
            data[row + (x0 >> 3)] |= 1 << (x0 & 7)
            x0 += 1

    def count(self, y: int, x0: int, x1: int) -> int:
        """How many bits are set on x0..x1 inclusive."""
        if y < 0 or y >= self.height or x1 < 0 or x0 >= self.width:
            return 0
        if x0 < 0:
            x0 = 0
        if x1 >= self.width:
            x1 = self.width - 1
        if x0 > x1:
            return 0
        data = self.data
        row = y * self.stride
        total = 0
        ones = _ONES
        while x0 <= x1 and (x0 & 7):
            total += (data[row + (x0 >> 3)] >> (x0 & 7)) & 1
            x0 += 1
        while x0 + 7 <= x1:
            total += ones[data[row + (x0 >> 3)]]
            x0 += 8
        while x0 <= x1:
            total += (data[row + (x0 >> 3)] >> (x0 & 7)) & 1
            x0 += 1
        return total

    def open_runs(self, y: int) -> list[tuple[int, int]]:
        """Inclusive x spans of tiles that are not street on this row."""
        data = self.data
        row = y * self.stride
        width = self.width
        runs: list[tuple[int, int]] = []
        x = 0
        start = 0
        in_run = False
        stride = self.stride
        for b in range(stride):
            byte = data[row + b]
            if x + 8 <= width:
                if byte == 0:
                    if not in_run:
                        start = x
                        in_run = True
                    x += 8
                    continue
                if byte == 0xFF:
                    if in_run:
                        runs.append((start, x - 1))
                        in_run = False
                    x += 8
                    continue
            for bit in range(8):
                if x >= width:
                    break
                if (byte >> bit) & 1:
                    if in_run:
                        runs.append((start, x - 1))
                        in_run = False
                elif not in_run:
                    start = x
                    in_run = True
                x += 1
        if in_run:
            end = width - 1 if x >= width else x - 1
            if end >= start:
                runs.append((start, end))
        return runs


def _clip_segment(x0, y0, x1, y1, left, top, right, bottom):
    """Liang–Barsky clip. None when the segment misses the box."""
    dx = x1 - x0
    dy = y1 - y0
    u0 = 0.0
    u1 = 1.0
    for pi, qi in ((-dx, x0 - left), (dx, right - x0), (-dy, y0 - top), (dy, bottom - y0)):
        if pi == 0:
            if qi < 0:
                return None
            continue
        t = qi / pi
        if pi < 0:
            if t > u1:
                return None
            if t > u0:
                u0 = t
        else:
            if t < u0:
                return None
            if t < u1:
                u1 = t
    if u0 > u1:
        return None
    return (x0 + u0 * dx, y0 + u0 * dy, x0 + u1 * dx, y0 + u1 * dy)


def _disc_x(cx, cy, radius, yc):
    dy = yc - cy
    rest = radius * radius - dy * dy
    if rest < 0:
        return None
    dx = math.sqrt(rest)
    return (cx - dx, cx + dx)


def _body_x(x0, y0, x1, y1, radius, yc):
    """The open interval of the stroke's rectangular body on the line y = yc.

    The body is every point whose closest point on the centre segment lies
    between the endpoints, within `radius` of the centre line. The round ends
    are the discs.
    """
    dx = x1 - x0
    dy = y1 - y0
    len2 = dx * dx + dy * dy
    if len2 <= 1e-12:
        return None
    length = math.sqrt(len2)
    limit = radius * length
    if abs(dy) <= 1e-8:
        if abs(yc - y0) > radius:
            return None
        return (x0 if x0 < x1 else x1, x1 if x1 > x0 else x0)
    c0 = dx * (yc - y0) + dy * x0
    if dy > 0:
        x_lo = (c0 - limit) / dy
        x_hi = (c0 + limit) / dy
    else:
        x_lo = (c0 + limit) / dy
        x_hi = (c0 - limit) / dy
    if abs(dx) <= 1e-8:
        t = (yc - y0) / dy
        if t < 0.0 or t > 1.0:
            return None
        return (x_lo, x_hi)
    k = (yc - y0) * dy
    if dx > 0:
        t_lo = x0 + (-k) / dx
        t_hi = x0 + (len2 - k) / dx
    else:
        t_lo = x0 + (len2 - k) / dx
        t_hi = x0 + (-k) / dx
    lo = x_lo if x_lo > t_lo else t_lo
    hi = x_hi if x_hi < t_hi else t_hi
    if lo > hi:
        return None
    return (lo, hi)


def _capsule_x(x0, y0, x1, y1, radius, yc):
    """Real-x interval of the round stroke on the horizontal through yc."""
    lo = None
    hi = None
    for span in (_disc_x(x0, y0, radius, yc),
                 _disc_x(x1, y1, radius, yc),
                 _body_x(x0, y0, x1, y1, radius, yc)):
        if span is None:
            continue
        lo = span[0] if lo is None else min(lo, span[0])
        hi = span[1] if hi is None else max(hi, span[1])
    if lo is None:
        return None
    return (lo, hi)


def _paint_interval(plane: _Plane, y: int, lo: float, hi: float) -> None:
    first = math.ceil(lo - 0.5 - _EDGE)
    last = math.floor(hi - 0.5 + _EDGE)
    if last >= first:
        plane.fill(y, first, last)


def _capsule(plane: _Plane, x0, y0, x1, y1, radius) -> None:
    if radius <= 0:
        return
    ymin = (y0 if y0 < y1 else y1) - radius
    ymax = (y0 if y0 > y1 else y1) + radius
    j0 = math.ceil(ymin - 0.5 - _EDGE)
    j1 = math.floor(ymax - 0.5 + _EDGE)
    if j0 < 0:
        j0 = 0
    if j1 >= plane.height:
        j1 = plane.height - 1
    for y in range(j0, j1 + 1):
        span = _capsule_x(x0, y0, x1, y1, radius, y + 0.5)
        if span is not None:
            _paint_interval(plane, y, span[0], span[1])


def _supercover(plane: _Plane, x0, y0, x1, y1) -> None:
    """Every tile square the centre line touches, so a one-tile street still
    separates the blocks on either side of it."""
    ix = math.floor(x0)
    iy = math.floor(y0)
    ex = math.floor(x1)
    ey = math.floor(y1)
    plane.set(ix, iy)
    if ix == ex and iy == ey:
        return
    dx = x1 - x0
    dy = y1 - y0
    sx = 1 if dx > 0 else -1 if dx < 0 else 0
    sy = 1 if dy > 0 else -1 if dy < 0 else 0
    if sx > 0:
        t_max_x = (ix + 1 - x0) / dx
    elif sx < 0:
        t_max_x = (ix - x0) / dx
    else:
        t_max_x = math.inf
    if sy > 0:
        t_max_y = (iy + 1 - y0) / dy
    elif sy < 0:
        t_max_y = (iy - y0) / dy
    else:
        t_max_y = math.inf
    t_dx = abs(1.0 / dx) if sx else math.inf
    t_dy = abs(1.0 / dy) if sy else math.inf
    steps = abs(ex - ix) + abs(ey - iy) + 2
    for _ in range(steps):
        if ix == ex and iy == ey:
            break
        if t_max_x < t_max_y:
            ix += sx
            t_max_x += t_dx
        elif t_max_y < t_max_x:
            iy += sy
            t_max_y += t_dy
        else:
            # Through a grid corner the line enters the diagonal tile. The
            # side tiles meet only at that point, so the blocks stay apart.
            ix += sx
            iy += sy
            t_max_x += t_dx
            t_max_y += t_dy
        plane.set(ix, iy)
    plane.set(ex, ey)


def _stroke(plane: _Plane, points, width: int) -> None:
    """A round stroke of `width` tiles, the shape the library draws for a way."""
    half = width * 0.5
    pad = half + 1.0
    left = -pad
    top = -pad
    right = plane.width + pad
    bottom = plane.height + pad
    for i in range(len(points) - 1):
        x0, y0 = points[i]
        x1, y1 = points[i + 1]
        clipped = _clip_segment(x0, y0, x1, y1, left, top, right, bottom)
        if clipped is None:
            continue
        ax, ay, bx, by = clipped
        _capsule(plane, ax, ay, bx, by, half)
        if width <= 1:
            _supercover(plane, ax, ay, bx, by)


def _fill_thin(plane: _Plane, left, top, right, bottom) -> None:
    x0 = math.ceil(left - 0.5)
    x1 = max(x0, math.ceil(right - 0.5) - 1)
    y0 = math.ceil(top - 0.5)
    y1 = max(y0, math.ceil(bottom - 0.5) - 1)
    if y0 < 0:
        y0 = 0
    if y1 >= plane.height:
        y1 = plane.height - 1
    for y in range(y0, y1 + 1):
        plane.fill(y, x0, x1)


def _fill_ring(plane: _Plane, ring) -> None:
    """Even-odd fill of one ring. A ring thinner than a tile still paints the
    tiles it crosses, so a shed mapped as a sliver still counts."""
    if len(ring) < 3:
        return
    x0, y0 = ring[-1]
    left = right = x0
    top = bot = y0
    edges = []
    for x1, y1 in ring:
        if x1 < left:
            left = x1
        elif x1 > right:
            right = x1
        if y1 < top:
            top = y1
        elif y1 > bot:
            bot = y1
        if y0 != y1:
            if y0 < y1:
                edges.append((y0, y1, x0, (x1 - x0) / (y1 - y0)))
            else:
                edges.append((y1, y0, x1, (x1 - x0) / (y1 - y0)))
        x0, y0 = x1, y1
    if bot - top < 1.0 or right - left < 1.0:
        _fill_thin(plane, left, top, right, bot)
        return
    if not edges:
        return
    y_first = max(0, math.ceil(top - 0.5))
    y_last = min(plane.height - 1, math.ceil(bot - 0.5) - 1)
    for y in range(y_first, y_last + 1):
        at = y + 0.5
        xs = [x + (at - lo) * slope for lo, hi, x, slope in edges if lo <= at < hi]
        xs.sort()
        i = 0
        n = len(xs)
        while i + 1 < n:
            a = math.ceil(xs[i] - 0.5)
            b = math.ceil(xs[i + 1] - 0.5) - 1
            if b < a:
                b = a
            plane.fill(y, a, b)
            i += 2


class _Block:
    """One open region. Each entry is a horizontal run grown down the rows it
    keeps the same x span, held until the region closes."""

    __slots__ = ("parent", "area", "covered", "x0", "x1", "y0", "y1")

    def __init__(self, area: int, covered: int, y: int, x0: int, x1: int) -> None:
        self.parent = self
        self.area = area
        self.covered = covered
        self.x0 = array.array("I", (x0,))
        self.x1 = array.array("I", (x1,))
        self.y0 = array.array("I", (y,))
        self.y1 = array.array("I", (y,))

    def add(self, y: int, x0: int, x1: int, span: int, covered: int) -> None:
        self.area += span
        self.covered += covered
        n = len(self.y1) - 1
        if n >= 0 and self.y1[n] + 1 == y and self.x0[n] == x0 and self.x1[n] == x1:
            self.y1[n] = y
            return
        self.x0.append(x0)
        self.x1.append(x1)
        self.y0.append(y)
        self.y1.append(y)


def _find(block: _Block) -> _Block:
    root = block
    while root.parent is not root:
        root = root.parent
    while block is not root:
        nxt = block.parent
        block.parent = root
        block = nxt
    return root


def _union(a: _Block, b: _Block) -> _Block:
    a = _find(a)
    b = _find(b)
    if a is b:
        return a
    if a.area < b.area:
        a, b = b, a
    b.parent = a
    a.area += b.area
    a.covered += b.covered
    if b.x0 is not None:
        a.x0.extend(b.x0)
        a.x1.extend(b.x1)
        a.y0.extend(b.y0)
        a.y1.extend(b.y1)
        b.x0 = b.x1 = b.y0 = b.y1 = None
    return a


def _emit(program, block: _Block, mark: int) -> None:
    """One rectangle per run that kept a constant span down the block."""
    rect = program.rect
    x0s = block.x0
    x1s = block.x1
    y0s = block.y0
    y1s = block.y1
    for i in range(len(x0s)):
        rect((int(x0s[i]), int(y0s[i]), int(x1s[i]), int(y1s[i])), mask=mark)


def _close(program, block: _Block, mark: int) -> bool:
    if block.x0 is None:
        return False
    dense = (block.area > 50 and block.covered > 0
             and block.covered / block.area >= R.DENSE_COVERAGE)
    if dense:
        _emit(program, block, mark)
    block.x0 = None
    block.x1 = None
    block.y0 = None
    block.y1 = None
    return dense


def _retire(prev, alive: set[int], program, mark: int) -> bool:
    sent = False
    seen: set[int] = set()
    for _x0, _x1, block in prev:
        root = _find(block)
        ident = id(root)
        if ident in seen or ident in alive:
            seen.add(ident)
            continue
        seen.add(ident)
        if _close(program, root, mark):
            sent = True
    return sent


def _measure(program, streets: _Plane, buildings: _Plane, mark: int) -> bool:
    """Label the open tiles row by row. A block is finished on the first row
    that no longer touches it, and its runs are released then."""
    prev = []
    paved = False
    height = streets.height
    for y in range(height):
        runs = streets.open_runs(y)
        active = []
        j = 0
        nprev = len(prev)
        for x0, x1 in runs:
            while j < nprev and prev[j][1] < x0:
                j += 1
            hit = None
            k = j
            while k < nprev and prev[k][0] <= x1:
                root = _find(prev[k][2])
                if hit is None:
                    hit = root
                elif root is not hit:
                    hit = _union(hit, root)
                k += 1
            span = x1 - x0 + 1
            covered = buildings.count(y, x0, x1)
            if hit is None:
                hit = _Block(span, covered, y, x0, x1)
            else:
                hit.add(y, x0, x1, span, covered)
            active.append((x0, x1, hit))
        resolved = []
        alive: set[int] = set()
        for x0, x1, block in active:
            root = _find(block)
            resolved.append((x0, x1, root))
            alive.add(id(root))
        if _retire(prev, alive, program, mark):
            paved = True
        prev = resolved
    if _retire(prev, set(), program, mark):
        paved = True
    return paved


def _street_width(feat, cat: str, meters_per_tile: float) -> int:
    """Carriageway width in tiles. A way narrower than one tile is still drawn
    one tile wide, which is the stroke the painter records."""
    width = int(R._way_width_m(feat, cat) / meters_per_tile)
    if width < 1:
        return 1
    return width


def mark_blocks(program, building_feats, buckets, proj, meters_per_tile, mark: int) -> bool:
    """Mark dense blocks in the scratch mask. Returns whether any were marked.

    The bit planes are dropped before this returns.
    """
    ways = []
    for cat in _STREETS:
        for feat in buckets.get(cat, []):
            if feat.kind != "way" or R._is_polygon(feat):
                continue
            rings = R._feature_coords_px(feat, proj)
            pts = rings[0] if rings else ()
            if len(pts) < 2:
                continue
            ways.append((pts, _street_width(feat, cat, meters_per_tile)))
    if not ways:
        return False
    footprints = []
    for feat in building_feats:
        for ring in R._feature_coords_px(feat, proj):
            if len(ring) >= 3:
                footprints.append(ring)
    if not footprints:
        return False
    width = proj.width
    height = proj.height
    if width <= 0 or height <= 0:
        return False

    streets = buildings = None
    try:
        streets = _Plane(width, height, seal=True)
        buildings = _Plane(width, height, seal=False)
        for pts, wide in ways:
            _stroke(streets, pts, wide)
        ways.clear()
        for ring in footprints:
            _fill_ring(buildings, ring)
        footprints.clear()
        if not buildings.hit:
            return False
        return _measure(program, streets, buildings, mark)
    finally:
        if streets is not None:
            streets.drop()
        if buildings is not None:
            buildings.drop()
