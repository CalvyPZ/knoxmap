"""Put a real building footprint onto the tile grid.

Project Zomboid buildings cannot be rotated. The first approach squared every
footprint up into an upright rectangle with the right side lengths, centred on
the real one. That is tidy, and wrong for most real towns: in the Turkish town
this was measured on, 61% of buildings stand more than 15 degrees off the
grid, and 37% of the squared-up rectangles covered less than 70% of the real
footprint. A street of houses running diagonally became a scatter of upright
boxes jutting into the road - buildings in the right places, but not a town
anyone would recognise.

So a building close to the grid is still squared up, because a wall with a
one-tile step every eight tiles looks like a mistake. Anything turned further
is rasterised as it really is: a tile belongs to the building when the middle
of that tile lies inside the real outline. The result has stepped walls along
its diagonal sides, and it stands exactly where the building stands.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import Polygon

# Within this many degrees of the grid, square the building up.
SNAP_DEGREES = 8.0
# A footprint filling this share of its rotated rectangle is a rectangle.
RECTANGULAR_ENOUGH = 0.86
MIN_TILES = 16


def _polygon(px: list[tuple[float, float]]):
    poly = Polygon(px)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    return poly


def grid_angle(poly) -> float:
    """How far the building's long side is from the nearest grid axis, 0..45."""
    rect = poly.minimum_rotated_rectangle
    if not hasattr(rect, "exterior"):
        return 0.0
    c = list(rect.exterior.coords)
    e1 = (c[1][0] - c[0][0], c[1][1] - c[0][1])
    e2 = (c[2][0] - c[1][0], c[2][1] - c[1][1])
    long_edge = e1 if math.hypot(*e1) >= math.hypot(*e2) else e2
    a = abs(math.degrees(math.atan2(long_edge[1], long_edge[0]))) % 90.0
    return min(a, 90.0 - a)


def _largest_component(mask: np.ndarray) -> np.ndarray:
    """Keep only the biggest 4-connected piece of a mask."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            piece = []
            stack = [(y, x)]
            seen[y, x] = True
            while stack:
                cy, cx = stack.pop()
                piece.append((cy, cx))
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(piece) > len(best):
                best = piece
    out = np.zeros_like(mask, dtype=bool)
    for y, x in best:
        out[y, x] = True
    return out


class Footprint:
    """Where one building goes: its bounding box and which tiles it owns."""

    def __init__(self, x0: int, y0: int, mask: np.ndarray, angle: float,
                 short_side: float, long_side: float):
        self.x0, self.y0 = x0, y0
        self.mask = mask
        self.angle = angle
        self.short_side = short_side
        self.long_side = long_side

    @property
    def width(self) -> int:
        return self.mask.shape[1]

    @property
    def height(self) -> int:
        return self.mask.shape[0]

    @property
    def tiles(self) -> int:
        return int(self.mask.sum())

    def mask_list(self) -> list[list[bool]] | None:
        """The mask as layout.py takes it, or None when it fills its box."""
        if self.mask.all():
            return None
        return self.mask.tolist()


# A unit narrower than this is not a building anyone can walk into: three
# tiles of room plus its walls.
MIN_ROOM_SIDE = 5


def split_row(fp: "Footprint", unit_tiles: int,
              min_tiles: int = MIN_TILES) -> list["Footprint"]:
    """Cut a long footprint into one footprint per unit along its length.

    A row of shops or a terrace of houses is usually one polygon in
    OpenStreetMap - the mapper drew the block, not the seven doors in it - and
    built as one building it came out as a single cavernous shed with one
    front door, the "uber building" players kept reporting. Cut into units of
    about a shop's frontage it becomes what it is: separate buildings standing
    wall to wall, which is a case the rest of the build already handles (the
    shared walls lose their windows, see _party_walls).

    The cuts are made on tile boundaries with no gap between units, so the row
    still covers exactly the tiles the real building covers.
    """
    along_x = fp.width >= fp.height
    length = fp.width if along_x else fp.height
    units = max(1, int(round(length / max(1, unit_tiles))))
    if units < 2:
        return [fp]
    out: list[Footprint] = []
    edges = [round(length * k / units) for k in range(units + 1)]
    for a, b in zip(edges, edges[1:]):
        if b - a < MIN_ROOM_SIDE:
            continue
        part = fp.mask[:, a:b] if along_x else fp.mask[a:b, :]
        if part.sum() < min_tiles:
            continue
        part = _largest_component(part)
        rows = np.where(part.any(axis=1))[0]
        cols = np.where(part.any(axis=0))[0]
        if len(rows) == 0 or len(cols) == 0:
            continue
        part = part[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1]
        if part.sum() < min_tiles:
            continue
        x0 = fp.x0 + (a if along_x else 0) + int(cols[0])
        y0 = fp.y0 + (0 if along_x else a) + int(rows[0])
        out.append(Footprint(x0, y0, part, fp.angle,
                             fp.short_side, fp.long_side / units))
    return out or [fp]


def _hug(mask: np.ndarray, x0: int, y0: int) -> tuple[np.ndarray, int, int]:
    """The mask's biggest piece, in a box trimmed to hold only that."""
    mask = _largest_component(mask)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if len(rows) == 0:
        return mask[:0, :0], x0, y0
    return (mask[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1],
            x0 + int(cols[0]), y0 + int(rows[0]))


# A building reaches the game as a lot, and a lot is a rectangle: the .pzw
# says <lot x y width height> and WorldEd writes that whole rectangle of
# squares into the cell. A footprint off the grid owns a staircase of tiles
# inside its rectangle and nothing else, so most of that rectangle is ground
# the building never claimed - and the next building along claims it, because
# `occupied` only ever held the tiles. On Brugge that came to 5004 pairs of
# lots standing on top of each other among 4202 buildings, overlapping by
# whole rows rather than the single shared column a terrace wants. Where two
# lots cover a square, one is written over the other and a wall goes missing.
#
# So a footprint claims its rectangle as well as its tiles, and a later one is
# cut back to the biggest part of itself whose rectangle is still free. Real
# neighbours still stand wall to wall: their rectangles meet along an edge,
# and the wall between them is the one column both of them draw.
def _clear_box(mask: np.ndarray, blocked: np.ndarray) -> tuple[int, int, int, int]:
    """The box holding most of `mask` and no `blocked` tile, half open.

    Cutting a row off the near side, as a nudge would, is no use when the lot
    in the way sits along the middle of the footprint, so every rectangle of
    free ground is measured - the row-by-row histogram scan that finds them -
    and the one keeping the most of the building wins. A footprint therefore
    gives up whichever end of itself is emptiest, and one that has nowhere
    left to stand gives up altogether.
    """
    h, w = mask.shape
    # How much of the building any box holds, in one subtraction.
    held = np.zeros((h + 1, w + 1), dtype=np.int32)
    held[1:, 1:] = mask.cumsum(0).cumsum(1)
    best = (0, 0, 0, 0, 0)
    heights = np.zeros(w, dtype=np.int32)
    for y in range(h):
        # Free tiles standing one above another, counting up to this row.
        heights = np.where(blocked[y], 0, heights + 1)
        stack: list[tuple[int, int]] = []
        for x, tall in enumerate(heights.tolist() + [0]):
            start = x
            while stack and stack[-1][1] >= tall:
                start, high = stack.pop()
                top = y + 1 - high
                tiles = int(held[y + 1, x] - held[top, x]
                            - held[y + 1, start] + held[top, start])
                if tiles > best[0]:
                    best = (tiles, top, y + 1, start, x)
            stack.append((start, tall))
    return best[1:]


NUDGE_TILES = 5


def _clear_of(mask: np.ndarray, x0: int, y0: int, avoid: np.ndarray,
              occupied: np.ndarray) -> tuple[int, int]:
    """Where to put a footprint so it stands off the roads.

    `avoid` weighs each tile: 2 for carriageway, 1 for pavement, 0 for free
    ground. With roads straightened and buildings squared up, a building can
    come out a tile or three into the street; it is moved the least distance,
    up to NUDGE_TILES, that takes it out, without walking into another."""
    map_h, map_w = avoid.shape
    h, w = mask.shape

    def cost(ox, oy):
        ax0, ay0 = x0 + ox, y0 + oy
        cx0, cy0 = max(0, ax0), max(0, ay0)
        cx1, cy1 = min(map_w, ax0 + w), min(map_h, ay0 + h)
        if cx1 <= cx0 or cy1 <= cy0:
            return None
        m = mask[cy0 - ay0:cy1 - ay0, cx0 - ax0:cx1 - ax0]
        return (int(avoid[cy0:cy1, cx0:cx1][m].sum()),
                int(occupied[cy0:cy1, cx0:cx1][m].sum()))

    here = cost(0, 0)
    if here is None or here[0] == 0:
        return x0, y0
    best, best_score = (0, 0), here[0] * 4 + here[1]
    for ox in range(-NUDGE_TILES, NUDGE_TILES + 1):
        for oy in range(-NUDGE_TILES, NUDGE_TILES + 1):
            c = cost(ox, oy)
            if c is None:
                continue
            score = c[0] * 4 + c[1] + (abs(ox) + abs(oy)) * 0.5
            if score < best_score:
                best, best_score = (ox, oy), score
    return x0 + best[0], y0 + best[1]


def place(px: list[tuple[float, float]], occupied: np.ndarray,
          min_side: float = 0, max_side: float = 1e9,
          snap_degrees: float = SNAP_DEGREES,
          avoid: np.ndarray | None = None,
          lots: np.ndarray | None = None
          ) -> tuple[Footprint | None, str]:
    """Rasterise a projected footprint, claiming its tiles in `occupied`.

    Returns the footprint and why not when there is none: "small", "large",
    "outside" or "taken". Size is judged on the real building's sides, before
    anything is claimed, so a rejected building never blocks its neighbours.

    Tiles another building already owns are left to it. Real neighbours share
    walls; two buildings claiming the same tile would put one wall inside the
    other.

    `lots` is the ground already covered by a building's lot rectangle rather
    than by its tiles (_clear_box). Passed in, the footprint is cut back until
    its own rectangle stands clear of every other, and its rectangle is
    claimed there in turn.
    """
    map_h, map_w = occupied.shape
    poly = _polygon(px)
    if poly.is_empty or poly.area <= 0:
        return None, "small"
    rect = poly.minimum_rotated_rectangle
    c = list(rect.exterior.coords) if hasattr(rect, "exterior") else []
    if len(c) >= 4:
        sides = sorted((math.dist(c[0], c[1]), math.dist(c[1], c[2])))
    else:
        minx, miny, maxx, maxy = poly.bounds
        sides = sorted((maxx - minx, maxy - miny))
    if sides[0] < min_side:
        return None, "small"
    if sides[1] > max_side:
        return None, "large"
    angle = grid_angle(poly)
    rectangular = rect.area > 0 and poly.area / rect.area >= RECTANGULAR_ENOUGH

    # Past 30 degrees a turned outline squared up would stand far out of its
    # real footprint, so from there any shape is squared (the user asked for
    # every building on the grid), not just near-rectangles.
    if angle <= snap_degrees and (rectangular or snap_degrees >= 30):
        # Square it up: an upright rectangle of the true side lengths, centred.
        cx, cy = poly.centroid.x, poly.centroid.y
        horizontal = (angle == 0.0 and (poly.bounds[2] - poly.bounds[0])
                      >= (poly.bounds[3] - poly.bounds[1]))
        if len(c) >= 4:
            dx = abs(c[1][0] - c[0][0]) + abs(c[2][0] - c[1][0])
            dy = abs(c[1][1] - c[0][1]) + abs(c[2][1] - c[1][1])
            horizontal = dx >= dy
        w, h = (sides[1], sides[0]) if horizontal else (sides[0], sides[1])
        w, h = max(1, int(round(w))), max(1, int(round(h)))
        x0 = int(round(cx - w / 2))
        y0 = int(round(cy - h / 2))
        mask = np.ones((h, w), dtype=bool)
    else:
        minx, miny, maxx, maxy = poly.bounds
        x0, y0 = int(math.floor(minx)), int(math.floor(miny))
        x1, y1 = int(math.ceil(maxx)), int(math.ceil(maxy))
        xs, ys = np.meshgrid(np.arange(x0, x1) + 0.5, np.arange(y0, y1) + 0.5)
        mask = shapely.contains_xy(poly, xs, ys)

    if avoid is not None:
        x0, y0 = _clear_of(mask, x0, y0, avoid, occupied)

    # Clip to the map, then give up tiles already owned by a neighbour.
    h, w = mask.shape
    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(map_w, x0 + w), min(map_h, y0 + h)
    if cx1 <= cx0 or cy1 <= cy0:
        return None, "outside"
    mask = mask[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0].copy()
    mask &= ~occupied[cy0:cy1, cx0:cx1]
    if mask.sum() < MIN_TILES:
        return None, "taken"
    # Trim empty rows and columns so the box hugs what is left.
    mask, fx0, fy0 = _hug(mask, cx0, cy0)
    if mask.sum() < MIN_TILES:
        return None, "taken"

    # ...and cut it back again where the box that hugs it would stand on a
    # lot already placed, even though the tiles under it are free.
    if lots is not None:
        blocked = lots[fy0:fy0 + mask.shape[0], fx0:fx0 + mask.shape[1]]
        if blocked.any():
            by0, by1, bx0, bx1 = _clear_box(mask, blocked)
            mask, fx0, fy0 = _hug(mask[by0:by1, bx0:bx1], fx0 + bx0, fy0 + by0)
            if mask.sum() < MIN_TILES:
                return None, "taken"

    occupied[fy0:fy0 + mask.shape[0], fx0:fx0 + mask.shape[1]] |= mask
    if lots is not None:
        lots[fy0:fy0 + mask.shape[0], fx0:fx0 + mask.shape[1]] = True
    return Footprint(fx0, fy0, mask, angle, sides[0], sides[1]), "ok"
