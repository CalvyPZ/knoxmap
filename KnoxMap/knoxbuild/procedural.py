"""Laying a town out for the game rather than copying the survey.

OpenStreetMap draws a town at its real density, and that is the problem. A
European or Turkish street is houses shoulder to shoulder on plots six metres
wide; at 2 m a tile that reaches the game as a row of five-by-four boxes, each
one room and a door. It is an accurate model of the street and it is nothing
anyone wants to walk into. A Knox County house is four or five proper rooms
with a yard round it, and it is the yard as much as the rooms that makes the
game's towns readable: somewhere to be seen crossing, somewhere to run.

So with `true_map` off, this module answers two questions for every footprint
before it is placed - is it built at all, and how big - and the rest of the
build carries on unchanged. Nothing here touches roads, water, woodland or the
terrain: those are drawn from the survey either way, and they are what makes a
generated map recognisable as the place it is.

Two rules do the work.

**Thinning.** Ordinary houses are kept in descending size order, and one is
dropped when another already kept stands closer than `GAP` times the distance
at which the two of them would touch. That is a spacing rule, not a coin flip,
and it behaves the way a mapper would want at both ends: a terrace packed at
one house-width loses every other one, a farmhouse with a field around it
loses nothing, and what survives is the larger house of each pair rather than
a random one. It needs no random stream, so a map built twice is the same
map.

**Growing.** What is kept is scaled about its own centre until it is big
enough to lay rooms out in - a house to `HOUSE_TILES`, a police station or a
school to whatever that kind of building is in the game. The footprint placer
cuts anything back that still will not fit (footprint.place), so growing is a
request, not a promise, and a building that has no room to grow simply stays
the size it was.

Landmarks are never thinned and are placed first, before the housing, so the
ground they need is theirs to take. OSM says there is a police station here;
what a police station looks like is the game's business.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import knoxstop
import numpy as np
from scipy.spatial import cKDTree

from shapely.affinity import scale as _scale
from shapely.geometry import Polygon

# A house is dropped when one already kept stands closer than this many times
# the distance at which the two of them would touch. 1.0 would only drop
# houses that actually overlap; past about 2.0 an ordinary suburban street
# starts losing houses that were never crowded to begin with.
GAP = 1.55

# What a kept house should be, in tiles of floor. Knox County's houses run
# from about 80 tiles (a small bungalow) to 150 (a family house with a
# garage); 90 is four rooms at the default room size, plus its walls and a
# hall. Tiles, not square metres, because it is the room count that decides
# whether a house is worth entering, and a room is measured in tiles.
HOUSE_TILES = 90

# And what the buildings a town is known by should be. OSM gives these as
# whatever the surveyor traced - a police station mapped as its front office
# is 40 tiles, which in the game is a hut with a desk in it - so the tags
# decide that there is one and this decides what it is. Roughly the floor
# area of the game's own, at one storey; taller kinds get theirs from
# DEFAULT_LEVELS on top.
LANDMARK_TILES = {
    "police": 300,
    "fire": 300,
    "medical": 450,
    "school": 600,
    "library": 260,
    "church": 220,
    "military": 400,
    "industrial": 500,
    "civic": 280,
    "shop": 320,
    "restaurant": 150,
    "apartment": 260,
}

# Ceilings on the scaling itself. A landmark traced as one room has to grow a
# long way to become a building; a house should never grow so far that the
# street it is on stops being the street that was mapped.
GROW_MAX_LANDMARK = 2.6
GROW_MAX_HOUSE = 1.9

# Kinds that are ordinary housing, and are thinned. Everything else is a
# landmark: it is on the map because somebody goes there.
HOUSING = {None, "house", "apartment", "shed"}


@dataclass
class Candidate:
    """One OSM footprint, before anything has been placed."""
    index: int                    # its place in the geojson
    px: list                      # the projected ring, in tiles
    area: float                   # of that ring, in tiles
    kind: str | None              # classify_building(tags)
    notable: bool                 # is_notable(tags, kind)
    has_use: bool                 # a shop, surgery or office mapped inside it

    @property
    def landmark(self) -> bool:
        """Somewhere a player would go on purpose, and never thinned."""
        return (self.notable or self.has_use
                or self.kind not in HOUSING)

    @property
    def width(self) -> float:
        """The side of a square of the same area: how wide it stands, near
        enough, without caring which way round it is."""
        return math.sqrt(max(1.0, self.area))


def _centre(px: list) -> tuple[float, float]:
    xs = [p[0] for p in px]
    ys = [p[1] for p in px]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _grown(px: list, factor: float) -> list:
    """The ring scaled about its own centre. Below 1.01 it is returned as it
    stands, so a building that does not need to grow keeps its exact outline
    and the floating point never moves it a tile sideways."""
    if factor <= 1.01:
        return px
    poly = Polygon(px)
    if poly.is_empty or poly.area <= 0:
        return px
    big = _scale(poly, xfact=factor, yfact=factor, origin="centroid")
    ring = list(big.exterior.coords)
    return ring[:-1] if len(ring) > 3 and ring[0] == ring[-1] else ring


def _long_side(px: list) -> float:
    """The longer side of the footprint's own rectangle, which is what
    footprint.place measures against max_size."""
    poly = Polygon(px)
    if poly.is_empty or poly.area <= 0:
        return 0.0
    rect = poly.minimum_rotated_rectangle
    c = list(rect.exterior.coords) if hasattr(rect, "exterior") else []
    if len(c) >= 4:
        return max(math.dist(c[0], c[1]), math.dist(c[1], c[2]))
    minx, miny, maxx, maxy = poly.bounds
    return max(maxx - minx, maxy - miny)


def _factor(cand: Candidate, max_size: float) -> float:
    """How much to scale this footprint up by, clamped so the result is still
    something the placer will accept."""
    if cand.landmark:
        target = LANDMARK_TILES.get(cand.kind or "", 0)
        ceiling = GROW_MAX_LANDMARK
    else:
        target = HOUSE_TILES
        ceiling = GROW_MAX_HOUSE
    if target <= 0 or cand.area >= target:
        return 1.0
    factor = min(ceiling, math.sqrt(target / max(1.0, cand.area)))
    # Past max_size the placer throws the building away as "large", which
    # would lose the very landmark this is trying to make room for.
    long_side = _long_side(cand.px)
    if long_side > 0:
        factor = min(factor, max(1.0, max_size / long_side))
    return factor


def _clamped(factor: float, cx: float, cy: float, width: float,
             nearby: list[tuple[float, float, float]]) -> float:
    """`factor`, cut back so this building does not grow into one already
    grown. Two barracks a few tiles apart both asked for 400 tiles of floor
    and the second one lost the ground to the first, so the map came out with
    one barracks where OSM had two - growing has to stop at the neighbour."""
    for ox, oy, ow in nearby:
        gap = math.dist((cx, cy), (ox, oy)) - ow / 2.0
        if gap <= 0.0:
            return 1.0
        factor = min(factor, 2.0 * gap / max(1.0, width))
    return max(1.0, factor)


def _reach(width: float, widest: float) -> float:
    """How far `near` used to look.

    A neighbour past this cannot thin the candidate or cut its growth: the
    gap test and the clamp both give up inside it. `widest` is the widest
    thing already kept, the same term the bucket reach used.
    """
    return max(GAP, GROW_MAX_LANDMARK) * (width + widest) / 2.0


def _max_gap(cands: list[Candidate]) -> float:
    """The greatest separation `_reach` can ask for on this list.

    A kept building is never wider than its own width times the growth
    ceiling, and a query uses the footprint's width before it grows, so
    every pair the old reach could touch lies inside this.
    """
    max_width = 0.0
    max_kept = 0.0
    for c in cands:
        max_width = max(max_width, c.width)
        ceiling = GROW_MAX_LANDMARK if c.landmark else GROW_MAX_HOUSE
        max_kept = max(max_kept, c.width * ceiling)
    return _reach(max_width, max_kept)


def _neighbour_lists(n: int, pairs: np.ndarray) -> list[np.ndarray]:
    """Each candidate's partners, sorted by index.

    `pairs` is `(i, j)` with `i < j` from `query_pairs`. Sorting makes the
    later walk deterministic; the keep test itself does not care about order.
    """
    lists = [np.empty(0, dtype=np.int64) for _ in range(n)]
    if len(pairs) == 0:
        return lists
    left = np.concatenate((pairs[:, 0], pairs[:, 1]))
    right = np.concatenate((pairs[:, 1], pairs[:, 0]))
    order = np.lexsort((right, left))
    left = left[order]
    right = right[order]
    cuts = np.flatnonzero(np.diff(left)) + 1
    starts = np.concatenate((np.array([0], dtype=np.int64), cuts))
    ends = np.concatenate((cuts, np.array([left.size], dtype=np.int64)))
    for start, end in zip(starts, ends):
        lists[int(left[start])] = right[start:end]
    return lists


def plan(cands: list[Candidate], max_size: float,
         should_stop=None) -> tuple[list[Candidate], dict]:
    """Which of these to build, in the order they should claim their ground.

    Returns the survivors - with `px` already grown - and a count of what
    happened, for the build's printout. Landmarks come first so the ground a
    police station needs is not already under the bungalow next door;
    everything else follows in descending size, as it did before.
    """
    counts = {"thinned": 0, "grown": 0, "landmarks": 0}
    by_size = (lambda c: (-c.area, c.index))
    landmarks = sorted((c for c in cands if c.landmark), key=by_size)
    housing = sorted((c for c in cands if not c.landmark), key=by_size)
    counts["landmarks"] = len(landmarks)

    # Same greedy order as before: every landmark, largest first, then the
    # housing, largest first. The tree is built on that sequence so a
    # neighbour's index is its place in the walk.
    ordered = landmarks + housing
    out: list[Candidate] = []
    if not ordered:
        return out, counts

    centres = [_centre(c.px) for c in ordered]
    xy = np.asarray(centres, dtype=np.float64)
    max_gap = _max_gap(ordered)
    pairs = (cKDTree(xy).query_pairs(max_gap, output_type="ndarray")
             if len(ordered) > 1 else np.empty((0, 2), dtype=np.int64))
    neighbours = _neighbour_lists(len(ordered), pairs)
    kept = np.zeros(len(ordered), dtype=bool)
    widths = np.zeros(len(ordered), dtype=np.float64)

    def nearby_of(i: int) -> list:
        nbrs = neighbours[i]
        alive = nbrs[kept[nbrs]] if nbrs.size else nbrs
        return [(centres[int(j)][0], centres[int(j)][1], float(widths[int(j)]))
                for j in alive]

    def take(i: int, c: Candidate, cx: float, cy: float, nearby: list) -> None:
        factor = _clamped(_factor(c, max_size), cx, cy, c.width, nearby)
        widths[i] = c.width * factor
        kept[i] = True
        if factor > 1.01:
            counts["grown"] += 1
            c = Candidate(c.index, _grown(c.px, factor), c.area * factor * factor,
                          c.kind, c.notable, c.has_use)
        out.append(c)

    # Landmarks first and all of them: OSM says there is a police station
    # here, and what the game needs a police station to be is not the
    # surveyor's business.
    for n, c in enumerate(landmarks):
        if n % 64 == 0:
            knoxstop.check(should_stop, "the buildings")
        cx, cy = centres[n]
        take(n, c, cx, cy, nearby_of(n))

    for n, c in enumerate(housing):
        if n % 64 == 0:
            knoxstop.check(should_stop, "the buildings")
        i = len(landmarks) + n
        cx, cy = centres[i]
        nearby = nearby_of(i)
        # Two buildings of width w1 and w2 touch when their centres are
        # (w1 + w2) / 2 apart, so that is the distance to measure the gap
        # against. Judging a house by its own width alone would let a
        # bungalow sit inside the school's new footprint, and judging it by
        # the larger width alone would clear the whole block around one.
        if any((cx - ox) ** 2 + (cy - oy) ** 2 < (GAP * (c.width + ow) / 2) ** 2
               for ox, oy, ow in nearby):
            counts["thinned"] += 1
            continue
        take(i, c, cx, cy, nearby)
    return out, counts
