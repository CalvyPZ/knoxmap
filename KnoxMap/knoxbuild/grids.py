"""NumPy and SciPy kernels over room grids and masks.

Wall coordinates follow BuildingEd. A west edge sits on the west side of a
tile and a north edge on its north side, which is how layout already records
them, so later passes can use these arrays without translating.

SciPy is required. There is no second implementation.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
from scipy import ndimage

# Two tiles is how far a tower steps in, and the diagonals count: a cell
# stays only when that whole square is inside the footprint. Cells past the
# edge count as unset, matching the setback test on every side.
_SETBACK_RADIUS = 2
_SETBACK_STRUCTURE = np.ones((2 * _SETBACK_RADIUS + 1,) * 2, dtype=bool)

# Corner-touching tiles are not the same component. Diagonals stay 0 so a
# label stops at an edge.
_NEIGHBOURS4 = np.array([[0, 1, 0],
                         [1, 1, 1],
                         [0, 1, 0]], dtype=np.uint8)

# Faces in the order exterior runs are walked: north, south, west, east.
_FACE = np.array(["N", "S", "W", "E"])

# One straight exterior stretch. ``end`` is exclusive.
RUN_DTYPE = np.dtype([
    ("side", "U1"),
    ("line", np.int32),
    ("start", np.int32),
    ("end", np.int32),
    ("room", np.int32),
])


class Edges(NamedTuple):
    """Differing wall edges as parallel arrays.

    ``y``, ``x``, ``a`` and ``b`` are int32. ``side`` is ``U1``, ``"W"`` or
    ``"N"``. All five have the same length. Unpack as ``y, x, side, a, b``.
    """

    y: np.ndarray
    x: np.ndarray
    side: np.ndarray
    a: np.ndarray
    b: np.ndarray


def _empty_edges() -> Edges:
    return Edges(
        np.empty(0, np.int32),
        np.empty(0, np.int32),
        np.empty(0, dtype="U1"),
        np.empty(0, np.int32),
        np.empty(0, np.int32),
    )


def _west_edges(g: np.ndarray):
    """West edges of ``g[:, 1:]`` where that cell differs from the one west."""
    differ = g[:, 1:] != g[:, :-1]
    y, ahead = np.nonzero(differ)
    x = ahead + 1
    return y, x, g[y, x - 1], g[y, x]


def _north_edges(g: np.ndarray):
    """North edges of ``g[1:, :]`` where that cell differs from the one north."""
    differ = g[1:, :] != g[:-1, :]
    ahead, x = np.nonzero(differ)
    y = ahead + 1
    return y, x, g[y - 1, x], g[y, x]


def _combine(west, north) -> Edges:
    """W edges in row-major order, then N edges in row-major order."""
    wy, wx, wa, wb = west
    ny, nx, na, nb = north
    if wy.size == 0 and ny.size == 0:
        return _empty_edges()
    side = np.empty(wy.size + ny.size, dtype="U1")
    side[:wy.size] = "W"
    side[wy.size:] = "N"
    return Edges(
        np.concatenate((wy, ny)).astype(np.int32),
        np.concatenate((wx, nx)).astype(np.int32),
        side,
        np.concatenate((np.asarray(wa), np.asarray(na))).astype(np.int32),
        np.concatenate((np.asarray(wb), np.asarray(nb))).astype(np.int32),
    )


def wall_edges(g: np.ndarray) -> Edges:
    """In-grid edges whose two cells differ.

    A W edge is the west side of tile ``(x, y)``: ``a`` is ``g[y, x - 1]``
    and ``b`` is ``g[y, x]``. An N edge is the north side of tile ``(x, y)``:
    ``a`` is ``g[y - 1, x]`` and ``b`` is ``g[y, x]``. Both cells lie inside
    ``g``, so the west column and the north row have no border edge here, and
    neither does the face past the east or south side. An empty cell (0)
    still counts when it sits in the array.

    W edges come first, then N edges. Each group is row-major in ``(y, x)``.
    """
    g = np.asarray(g)
    return _combine(_west_edges(g), _north_edges(g))


def outside_edges(g: np.ndarray) -> Edges:
    """Exterior edges: one side is empty, or past the array.

    Same W/N coordinates as ``wall_edges``, taken from a zero-padded grid and
    shifted back. Room-against-room edges are left out. An east face is a W
    edge at ``x == width`` and a south face is an N edge at ``y == height``,
    which is the tile just outside that side. Empty cells inside ``g`` are
    outside too, so a courtyard wall is included.
    """
    g = np.asarray(g)
    padded = np.pad(g, 1, mode="constant", constant_values=0)
    y, x, side, a, b = wall_edges(padded)
    keep = (a == 0) | (b == 0)
    if not np.any(keep):
        return _empty_edges()
    return Edges(y[keep] - 1, x[keep] - 1, side[keep], a[keep], b[keep])


def areas(g: np.ndarray, R: int) -> np.ndarray:
    """How many cells carry each room id, including 0.

    The result is int64 and indexed by room id. Its length is at least
    ``R + 1``; an id above ``R`` extends it.
    """
    return np.bincount(np.asarray(g).ravel(), minlength=int(R) + 1).astype(np.int64)


def bounds(g: np.ndarray) -> list:
    """Bounding slices per room id, from ``ndimage.find_objects``.

    Entry ``i`` is the box of room ``i + 1``, or None when that id is absent.
    Room 0 is the background and is not listed. The list runs through the
    highest id in ``g``.
    """
    return ndimage.find_objects(np.asarray(g))


def adjacency(g: np.ndarray, R: int) -> np.ndarray:
    """Shared-wall counts between room ids, shape ``(R + 1, R + 1)``, int32.

    ``adj[i, j]`` is how many edges rooms ``i`` and ``j`` share, and the
    matrix is symmetric. Each edge is stored from its west or north cell
    only, then added to the transpose, so it contributes one to both rooms
    and not two to one of them. Id 0 is empty cells and the area past the
    array. Ids must lie in ``0 .. R``.
    """
    g = np.asarray(g)
    padded = np.pad(g, 1, mode="constant", constant_values=0)
    _y, _x, _side, a, b = wall_edges(padded)
    adj = np.zeros((int(R) + 1, int(R) + 1), dtype=np.int32)
    if a.size:
        # A plain += would keep one edge when several share the same pair.
        np.add.at(adj, (a, b), 1)
        # Not in place: adj.T is a view, so += would read cells already
        # written when the same two rooms meet from both directions.
        adj = adj + adj.T
    return adj


def label4(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected components. Returns ``(labeled, n)``.

    ``labeled`` is 0 on the background. ``n`` is how many components were
    found. Connectivity is the cross ``[[0, 1, 0], [1, 1, 1], [0, 1, 0]]``.
    """
    labeled, n = ndimage.label(np.asarray(mask), structure=_NEIGHBOURS4)
    return labeled, int(n)


def _empty_runs() -> np.ndarray:
    return np.empty(0, dtype=RUN_DTYPE)


def runs_by_room(g: np.ndarray) -> list[np.ndarray]:
    """Exterior wall runs for every room, indexed by room id.

    ``runs[room]`` is a ``RUN_DTYPE`` array. ``runs[0]`` is empty, and so is
    any id with no outside wall. The list is long enough for ``max(g)``.
    Missing ids in between are empty arrays.

    A run is one straight stretch: the same face, the same room, tiles
    adjacent along the wall. ``end`` is exclusive, so the tiles are
    ``range(start, end)`` and the length is ``end - start``. The middle tile,
    the one a door uses, is ``start + (end - start) // 2``.

    ``side`` is the way the room faces and ``line`` is the fixed coordinate
    of the BuildingEd edge. For ``m`` in ``range(start, end)``:

    - ``"N"``: edge ``(m, line, "N")`` on the north of tile ``(m, line)``
    - ``"S"``: edge ``(m, line, "N")`` on the south of tile ``(m, line - 1)``
    - ``"W"``: edge ``(line, m, "W")`` on the west of tile ``(line, m)``
    - ``"E"``: edge ``(line, m, "W")`` on the east of tile ``(line - 1, m)``

    Within a room the runs are ordered N, S, W, E, then ``line``, then
    ``start``. A wall shared with the next building is still here; the caller
    drops party edges.
    """
    g = np.asarray(g)
    if g.size == 0:
        return [_empty_runs()]
    by_room = [_empty_runs() for _ in range(int(np.max(g)) + 1)]
    y, x, side, a, b = outside_edges(g)
    if y.size == 0:
        return by_room

    # b is the cell the edge sits on. When that cell is the room, the face is
    # west or north; when the room is the other cell, the face is east or south.
    room = np.where(b != 0, b, a).astype(np.int32)
    west = side == "W"
    on_b = b != 0
    face = np.where(west, np.where(on_b, 2, 3), np.where(on_b, 0, 1)).astype(np.int8)
    line = np.where(west, x, y).astype(np.int32)
    pos = np.where(west, y, x).astype(np.int32)

    # Last key is the primary sort, so this groups a room's face and line
    # and leaves positions increasing along the wall.
    order = np.lexsort((pos, line, face, room))
    room, face, line, pos = room[order], face[order], line[order], pos[order]
    fresh = np.empty(pos.shape, dtype=bool)
    fresh[0] = True
    if pos.size > 1:
        fresh[1:] = (
            (room[1:] != room[:-1])
            | (face[1:] != face[:-1])
            | (line[1:] != line[:-1])
            | (np.diff(pos) != 1)
        )
    start_at = np.flatnonzero(fresh)
    end_at = np.empty_like(start_at)
    end_at[:-1] = start_at[1:] - 1
    end_at[-1] = pos.size - 1

    packed = np.empty(start_at.size, dtype=RUN_DTYPE)
    packed["side"] = _FACE[face[start_at]]
    packed["line"] = line[start_at]
    packed["start"] = pos[start_at]
    packed["end"] = pos[end_at] + 1
    packed["room"] = room[start_at]

    rooms = packed["room"]
    cuts = np.flatnonzero(np.diff(rooms)) + 1
    spans = np.concatenate((np.array([0]), cuts, np.array([rooms.size])))
    for i0, i1 in zip(spans[:-1], spans[1:]):
        by_room[int(rooms[i0])] = packed[i0:i1]
    return by_room


def sat(mask: np.ndarray) -> np.ndarray:
    """Summed-area table of a mask, int64, shape ``(H + 1, W + 1)``.

    The first row and column are 0. With ``y1`` and ``x1`` exclusive::

        mask[y0:y1, x0:x1].sum()
            == sat[y1, x1] - sat[y0, x1] - sat[y1, x0] + sat[y0, x0]
    """
    values = np.asarray(mask, dtype=np.int64)
    table = np.zeros((values.shape[0] + 1, values.shape[1] + 1), dtype=np.int64)
    table[1:, 1:] = values.cumsum(axis=0).cumsum(axis=1)
    return table


def window_full(sat: np.ndarray, h: int, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Top-left corners where an ``h`` by ``w`` window is entirely set.

    ``sat`` is a summed-area table from ``sat()``. Returns ``y`` and ``x``
    as int32, row-major. A window that does not fit, or a non-positive size,
    yields two empty arrays. Cells count as set when they sum to ``h * w``,
    so the mask behind the table is 0/1.
    """
    height = sat.shape[0] - 1
    width = sat.shape[1] - 1
    if h <= 0 or w <= 0 or h > height or w > width:
        return np.empty(0, np.int32), np.empty(0, np.int32)
    sums = sat[h:, w:] - sat[:-h, w:] - sat[h:, :-w] + sat[:-h, :-w]
    need = np.int64(h) * np.int64(w)
    y, x = np.nonzero(sums == need)
    return y.astype(np.int32), x.astype(np.int32)


def erode(mask: np.ndarray, structure: np.ndarray | None = None) -> np.ndarray:
    """Binary erosion. Nonzero cells count as set.

    The default footprint is the ``5×5`` setback square (radius 2, diagonals
    included). The border value is unset, so a cell within two tiles of the
    outside of the array is cleared, the same as the setback test that steps
    in on every side. Pass another footprint for a one-axis step.
    """
    if structure is None:
        structure = _SETBACK_STRUCTURE
    return ndimage.binary_erosion(
        np.asarray(mask), structure=structure, border_value=0)


def colour_mask(rgb: np.ndarray, colours) -> np.ndarray:
    """True where an ``(H, W, 3)`` uint8 image matches one of ``colours``.

    Each pixel and each ``(r, g, b)`` tuple is packed into uint32 as
    ``(r << 16) | (g << 8) | b`` before ``np.isin``. The result is a bool
    array of shape ``(H, W)``.
    """
    image = np.asarray(rgb)
    packed = (
        image[..., 0].astype(np.uint32) << 16
        | image[..., 1].astype(np.uint32) << 8
        | image[..., 2].astype(np.uint32)
    )
    palette = np.asarray(colours, dtype=np.uint32)
    if palette.size == 0:
        return np.zeros(packed.shape, dtype=bool)
    if palette.ndim == 1:
        palette = palette.reshape(1, -1)
    keys = palette[..., 0] << 16 | palette[..., 1] << 8 | palette[..., 2]
    return np.isin(packed, keys.ravel())


def rooms_text(g: np.ndarray) -> str:
    """Room-grid text for a ``<rooms>`` body, before XML escaping.

    A leading newline, then each row, with a newline after every row. A comma
    follows every cell except the last cell of the whole grid, so a comma
    also sits at the end of every row but the last. ``escape()`` is the
    writer's job; this is the string the writer used to build by hand.
    """
    g = np.asarray(g)
    height, width = int(g.shape[0]), int(g.shape[1])
    # No cells: the hand-built block is still a leading newline, plus one
    # more for every empty row.
    if width == 0:
        return "\n" * (height + 1)
    if height == 0:
        return "\n"
    cells = np.char.mod("%d", g.astype(np.int64))
    rows = [",".join(cells[y].tolist()) for y in range(height)]
    return "\n" + ",\n".join(rows) + "\n"
