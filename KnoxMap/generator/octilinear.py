"""Roads in long straight runs at 45-degree steps, the way Knox County's are.

The game's own roads run along the tiles or on the diagonal, nothing between.
A real street at 20 degrees drawn on tiles is a ragged staircase, its kerbs
and markings chopped at every step, and a curving suburban crescent is a
smear. With the "Knox County roads" setting the town is gently redrawn before
anything is painted:

1. The road network becomes a graph: its junctions and the corners left once
   small wiggles are smoothed away.
2. Each stretch between two of those points picks the nearest of the four
   directions the game draws well - along either axis or either diagonal -
   and all the points are moved together, as little as they can be, until
   every stretch runs that way (a least-squares fit; junctions are shared, so
   every road still meets the ones it met). A crescent becomes a few straight
   sides with 45-degree corners; a long road at 10 degrees becomes straight.
3. Everything else - buildings, parks, car parks, water, paths - moves with
   the roads around it, by a smooth blend of how far the nearby roads moved,
   so houses still front their street instead of standing in it.

What is left is a town that reads like the real one, laid out like the game's.
"""
from __future__ import annotations

import math

import numpy as np

ROAD_CATEGORIES = {"road_major", "road_medium", "road_minor", "road_service"}
# Wiggles smaller than this, in tiles, are smoothed away first.
SIMPLIFY_TILES = 3.0
# How strongly a point holds to where it really is, against how strongly each
# stretch turns to its direction. Low: roads straighten, the town bends to fit.
HOLD = 0.02
# How far, in tiles, the rest of the town feels a road's move.
BLEND_TILES = 30
FIELD_CELL = 4                  # tiles per cell of the displacement field
DIRECTIONS = [(1.0, 0.0), (math.sqrt(.5), math.sqrt(.5)), (0.0, 1.0),
              (-math.sqrt(.5), math.sqrt(.5))]


def _key(lat: float, lon: float) -> tuple[int, int]:
    return round(lat * 1e7), round(lon * 1e7)


def _simplify_idx(pts: list[tuple[float, float]], tol: float) -> list[int]:
    """Douglas-Peucker; the indices kept, both ends included."""
    if len(pts) < 3:
        return list(range(len(pts)))
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        best, best_d = -1, tol
        for i in range(a + 1, b):
            d = _dist_to_line(pts[i], pts[a], pts[b])
            if d > best_d:
                best, best_d = i, d
        if best >= 0:
            keep[best] = True
            stack += [(a, best), (best, b)]
    return [i for i, k in enumerate(keep) if k]


def _dist_to_line(p, a, b) -> float:
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0:
        return math.hypot(px - ax, py - ay)
    return abs(dx * (ay - py) - (ax - px) * dy) / length


def _legs(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """From a to b along the grid and the diagonal: half the straight part,
    the diagonal, the other half."""
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    sx, sy = (dx > 0) - (dx < 0), (dy > 0) - (dy < 0)
    diag = min(abs(dx), abs(dy))
    first = (max(abs(dx), abs(dy)) - diag) // 2
    p1 = (ax + sx * first, ay) if abs(dx) >= abs(dy) else (ax, ay + sy * first)
    return [p1, (p1[0] + sx * diag, p1[1] + sy * diag), b]


def _tidy(path: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Without repeated points or points in the middle of a straight run."""
    out: list[tuple[int, int]] = []
    for p in path:
        if out and p == out[-1]:
            continue
        if len(out) >= 2:
            (x0, y0), (x1, y1) = out[-2], out[-1]
            if (x1 - x0) * (p[1] - y1) == (y1 - y0) * (p[0] - x1) and \
                    (x1 - x0) * (p[0] - x1) + (y1 - y0) * (p[1] - y1) >= 0:
                out[-1] = p
                continue
        out.append(p)
    return out


def _solve(p0: np.ndarray, segs: np.ndarray, normals: np.ndarray,
           weights: np.ndarray, iterations: int = 400, hold: float | None = None) -> np.ndarray:
    """Points as near p0 as they can be with every segment turned along its
    direction: minimise HOLD*|P - p0|^2 + sum w*(n . (P_j - P_i))^2, by
    conjugate gradients on the normal equations."""
    hold = HOLD if hold is None else hold
    i, j = segs[:, 0], segs[:, 1]

    def apply(x):
        out = hold * x
        v = weights * np.einsum("ij,ij->i", normals, x[j] - x[i])
        np.add.at(out, j, v[:, None] * normals)
        np.add.at(out, i, -v[:, None] * normals)
        return out

    b = hold * p0
    x = p0.copy()
    r = b - apply(x)
    p = r.copy()
    rs = float((r * r).sum())
    for _ in range(iterations):
        if rs < 1e-6:
            break
        ap = apply(p)
        alpha = rs / float((p * ap).sum())
        x += alpha * p
        r -= alpha * ap
        rs_new = float((r * r).sum())
        p = r + (rs_new / rs) * p
        rs = rs_new
    return x


def _blur(a: np.ndarray, radius: int) -> np.ndarray:
    """Three box blurs, near enough a Gaussian, along both axes."""
    for axis in (0, 1):
        for _ in range(3):
            pad = [(0, 0), (0, 0)]
            pad[axis] = (radius + 1, radius)
            c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
            hi = np.take(c, range(2 * radius + 1, c.shape[axis]), axis=axis)
            lo = np.take(c, range(0, c.shape[axis] - 2 * radius - 1), axis=axis)
            a = (hi - lo) / (2 * radius + 1)
    return a


def _round_together(moved: np.ndarray, segs: np.ndarray, normals: np.ndarray) -> list:
    """Whole tiles for the points, the same row for every point along one
    east-west run, the same column along a north-south one and the same
    diagonal along a diagonal one - rounded one at a time, the ends of a long
    straight road can land either side of a tile edge, and it jogs."""
    n = len(moved)
    parent = {kind: list(range(n)) for kind in ("row", "col", "down", "up")}

    def find(kind, a):
        p = parent[kind]
        while p[a] != a:
            p[a] = p[p[a]]
            a = p[a]
        return a

    for (a, b), (nx, ny) in zip(segs, normals):
        if abs(nx) < 1e-9:
            kind = "row"                  # runs along x: normal is (0, ±1)
        elif abs(ny) < 1e-9:
            kind = "col"
        elif nx * ny < 0:
            kind = "down"                 # along (1, 1): x - y is constant
        else:
            kind = "up"                   # along (-1, 1): x + y is constant
        ra, rb = find(kind, int(a)), find(kind, int(b))
        if ra != rb:
            parent[kind][ra] = rb

    def shared(kind, values):
        groups: dict[int, list[float]] = {}
        for k in range(n):
            groups.setdefault(find(kind, k), []).append(values[k])
        mean = {g: round(sum(v) / len(v)) for g, v in groups.items()}
        sizes = {g: len(v) for g, v in groups.items()}
        return [mean[find(kind, k)] for k in range(n)], [sizes[find(kind, k)] > 1 for k in range(n)]

    xs, ys = moved[:, 0], moved[:, 1]
    row, in_row = shared("row", ys)
    col, in_col = shared("col", xs)
    down, in_down = shared("down", xs - ys)
    up, in_up = shared("up", xs + ys)
    out = []
    for k in range(n):
        y = row[k] if in_row[k] else round(ys[k])
        if in_col[k]:
            x = col[k]
        elif in_down[k]:
            x = down[k] + y
        elif in_up[k]:
            x = up[k] - y
        else:
            x = round(xs[k])
        if not in_row[k] and not in_col[k] and in_down[k] and in_up[k]:
            # Where two diagonals cross, both hold.
            y = (up[k] - down[k]) // 2
            x = down[k] + y
        out.append((int(x), int(y)))
    return out


def straighten_roads(features: list, proj, classify, is_polygon) -> int:
    """Redraw the roads in `features` and move the rest of the town with them,
    in place. Returns how many roads were redrawn."""
    roads = [f for f in features
             if f.kind == "way" and len(f.geometry) >= 2 and not is_polygon(f)
             and classify(f.tags) in ROAD_CATEGORIES]
    if not roads:
        return 0

    # --- the graph: junctions shared, corners per road ---------------------
    uses: dict[tuple[int, int], int] = {}
    for f in roads:
        for n, (lat, lon) in enumerate(f.geometry):
            k = _key(lat, lon)
            uses[k] = uses.get(k, 0) + (2 if n in (0, len(f.geometry) - 1) else 1)
    points: list[tuple[float, float]] = []
    junction: dict[tuple[int, int], int] = {}
    road_vertices: list[list[int]] = []      # per road, its graph points in order
    road_px: list[list[tuple[float, float]]] = []

    def vertex(xy, key=None):
        if key is not None and key in junction:
            return junction[key]
        points.append(xy)
        if key is not None:
            junction[key] = len(points) - 1
        return len(points) - 1

    for f in roads:
        px = [proj.to_px(lat, lon) for lat, lon in f.geometry]
        road_px.append(px)
        keys = [_key(lat, lon) for lat, lon in f.geometry]
        cuts = [n for n, k in enumerate(keys) if n in (0, len(keys) - 1) or uses[k] >= 2]
        verts = [vertex(px[0], keys[0])]
        for a, b in zip(cuts, cuts[1:]):
            kept = _simplify_idx(px[a:b + 1], SIMPLIFY_TILES)
            for n in kept[1:-1]:
                verts.append(vertex(px[a + n]))
            verts.append(vertex(px[b], keys[b]))
        road_vertices.append(verts)

    p0 = np.array(points, dtype=float)
    segs, normals, weights = [], [], []
    for verts in road_vertices:
        for a, b in zip(verts, verts[1:]):
            if a == b:
                continue
            d = p0[b] - p0[a]
            length = float(np.hypot(*d))
            if length < 1e-6:
                continue
            u = d / length
            best = max(DIRECTIONS, key=lambda t: abs(u[0] * t[0] + u[1] * t[1]))
            segs.append((a, b))
            normals.append((-best[1], best[0]))
            # Long stretches turn firmly; a short one between two junctions
            # gives way to the roads around it.
            weights.append(min(length, 60.0) / 10.0)
    if not segs:
        return 0
    segs, normals, weights = np.array(segs), np.array(normals), np.array(weights)
    moved = _solve(p0, segs, normals, weights)
    # Then held only to where that put them, far more loosely: what is left
    # of each stretch's slant goes, and a straight road is straight to the
    # tile rather than jogging one sideways every so often.
    moved = _solve(moved, segs, normals, weights, iterations=1500, hold=HOLD / 50)

    # --- the roads, from the moved points ---------------------------------
    snapped = _round_together(moved, segs, normals)
    for f, verts in zip(roads, road_vertices):
        path = [snapped[verts[0]]]
        for v in verts[1:]:
            path += _legs(path[-1], snapped[v])
        path = _tidy(path)
        if len(path) >= 2:
            f.geometry = [proj.to_latlon(x, y) for x, y in path]

    # --- everything else moves with the roads near it ---------------------
    gw, gh = proj.width // FIELD_CELL + 2, proj.height // FIELD_CELL + 2
    weight = np.zeros((gh, gw))
    shift = np.zeros((gh, gw, 2))
    disp = moved - p0
    for verts, px in zip(road_vertices, road_px):
        # Sample along each stretch, the move blended between its ends.
        for a, b in zip(verts, verts[1:]):
            pa, pb = p0[a], p0[b]
            steps = max(1, int(np.hypot(*(pb - pa)) / FIELD_CELL))
            for t in np.linspace(0.0, 1.0, steps + 1):
                q = pa + (pb - pa) * t
                cx, cy = int(q[0] // FIELD_CELL), int(q[1] // FIELD_CELL)
                if 0 <= cx < gw and 0 <= cy < gh:
                    weight[cy, cx] += 1.0
                    shift[cy, cx] += disp[a] * (1 - t) + disp[b] * t
    radius = max(1, BLEND_TILES // FIELD_CELL)
    w_blur = _blur(weight, radius)
    field_x = _blur(shift[..., 0], radius) / (w_blur + 1e-3)
    field_y = _blur(shift[..., 1], radius) / (w_blur + 1e-3)
    # Far from any road the move fades out rather than jumping to nothing.
    fade = np.clip(w_blur / (w_blur.max() * 0.02 + 1e-9), 0.0, 1.0)
    field_x *= fade
    field_y *= fade

    def move(lat, lon):
        x, y = proj.to_px(lat, lon)
        cx = min(gw - 1, max(0, int(x // FIELD_CELL)))
        cy = min(gh - 1, max(0, int(y // FIELD_CELL)))
        return proj.to_latlon(x + field_x[cy, cx], y + field_y[cy, cx])

    road_ids = {id(f) for f in roads}
    for f in features:
        if id(f) in road_ids:
            continue
        if f.geometry and f.kind in ("way", "node"):
            f.geometry = [move(lat, lon) for lat, lon in f.geometry]
        if f.kind == "relation" and getattr(f, "role_geoms", None):
            f.role_geoms = [(role, [move(lat, lon) for lat, lon in ring])
                            for role, ring in f.role_geoms]
            if f.geometry and isinstance(f.geometry[0][0], (list, tuple)):
                f.geometry = [ring for _role, ring in f.role_geoms]
    return len(roads)
