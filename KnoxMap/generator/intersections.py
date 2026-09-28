"""Junctions, after the streets exist and before the kerbs are trusted.

OpenStreetMap does not hand over an intersection object. A junction is a
coordinate several roads share, and what the junction *is* — signals, a stop,
a roundabout, a plain crossing — is tagged on a node at that point or on the
approach just short of it. Ways arrive as coordinates with no node ids, so
those nodes are fetched on their own and matched back by position.

Nothing here is guessed. A junction with no signal, stop, give-way, crossing,
roundabout or turning-circle tag is still squared off, so the kerb can turn a
corner, but it gets no sign and no line. Placement assumes traffic keeps
right, the same rule the speed signs already use.

Distances are in metres. The map is 1 m or 2 m a tile, and a stop line that
was "2 tiles" would jump a lane when the scale changed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import biomes
from . import pz_colors as C


ROAD_CATS = ("road_major", "road_medium", "road_minor", "road_service")
# Highest class wins the tarmac where two carriageways overlap.
_RANK = {"road_major": 4, "road_medium": 3, "road_minor": 2, "road_service": 1}
_COLOUR = {
    4: C.DARKEST_ASPHALT,
    3: C.MEDIUM_ASPHALT,
    2: C.MEDIUM_ASPHALT,
    1: C.DARKEST_ASPHALT,
}
CONTROL_VALUES = frozenset({
    "traffic_signals", "stop", "give_way", "crossing",
    "mini_roundabout", "turning_circle", "turning_loop",
})
# One control per arm. A signal replaces a stop; a stop replaces a give-way.
_CONTROL_RANK = {"none": 0, "give_way": 1, "stop": 2, "signal": 3}
_ONEWAY_FORWARD = frozenset({"yes", "1"})
_ONEWAY_BACKWARD = frozenset({"-1", "reverse"})
_EMIT = frozenset({
    "signals", "all_way_stop", "stop", "give_way", "roundabout",
    "mini_roundabout", "uncontrolled", "turning_circle", "crossing",
})
# Corners are rebuilt only for these. A roundabout gets an island instead,
# a turning circle a bulb, a driveway nothing.
_CORNERS = frozenset({
    "signals", "all_way_stop", "stop", "give_way",
    "uncontrolled", "mini_roundabout",
})

# How near two junction nodes must be before they are the same junction.
# Wide roads use their combined width, so the two carriageways of a dual
# road still meet; nothing closer than this merges on a narrow street.
CLUSTER_MIN_M = 12.0
APPROACH_M = 30.0
SNAP_M = 5.0
CROSSING_NEAR_M = 15.0
NO_PARKING_M = 9.0
CROSSWALK_GAP_M = 3.0
ISLAND_PAVING_M = 6.0
TURNING_MIN_M = 8.0
TURNING_MAX_M = 15.0
# The same test the centre lines use: a run this close to an axis is on it.
AXIS_SLOPE = 0.2
# A disc smaller than the junction reads as a dot in the lane.
MINI_MIN_TILES = 5
MINI_RADIUS_TILES = 2

_ASPHALT = frozenset({
    C.MEDIUM_ASPHALT, C.DARK_ASPHALT, C.DARKEST_ASPHALT,
    C.DARK_POTHOLE, C.LIGHT_POTHOLE,
})
_GRASS = frozenset({C.DARK_GRASS, C.MEDIUM_GRASS, C.LIGHT_GRASS})
_KERBS = frozenset({
    C.KERB_W, C.KERB_N, C.KERB_S, C.KERB_E,
    C.KERB_NW, C.KERB_SW, C.KERB_NE, C.KERB_SE,
})
_LINES = frozenset({
    C.LINE_YELLOW_N, C.LINE_YELLOW_W, C.LINE_WHITE_N, C.LINE_WHITE_W,
    C.EDGE_LINE_W, C.EDGE_LINE_N, C.EDGE_LINE_E, C.EDGE_LINE_S,
})
_STOP_LINE = {
    "ns": (C.STOP_LINE_N, C.STOP_LINE_S),
    "ew": (C.STOP_LINE_W, C.STOP_LINE_E),
}
_CROSSWALK = {"ns": C.CROSSWALK_N, "ew": C.CROSSWALK_W}
_GIVE_LINE = {"ns": C.LINE_WHITE_N, "ew": C.LINE_WHITE_W}
_STOP_SIGN = {"N": C.STOP_SIGN_N, "E": C.STOP_SIGN_E,
              "S": C.STOP_SIGN_S, "W": C.STOP_SIGN_W}
_SIGNAL = {"N": C.SIGNAL_N, "E": C.SIGNAL_E, "S": C.SIGNAL_S, "W": C.SIGNAL_W}
_LAMP = {"N": C.LAMP_N, "E": C.LAMP_E, "S": C.LAMP_S, "W": C.LAMP_W}


def is_control(tags: dict) -> bool:
    """A node the intersection pass asked the download for."""
    return tags.get("highway") in CONTROL_VALUES


def _key(lat: float, lon: float) -> tuple[int, int]:
    return round(lat * 1e7), round(lon * 1e7)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = a
    lat2, lon2 = b
    mid = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * 111320.0
    dx = (lon2 - lon1) * 111320.0 * math.cos(mid)
    return math.hypot(dx, dy)


def _prefer(old: str, new: str) -> str:
    return new if _CONTROL_RANK[new] > _CONTROL_RANK[old] else old


def _direction(tags: dict, kind: str) -> str:
    """Which way along the road this node faces. Untagged faces both."""
    specific = {
        "traffic_signals": "traffic_signals:direction",
        "stop": "stop:direction",
        "give_way": "give_way:direction",
    }.get(kind)
    for key in (specific, "direction"):
        if not key:
            continue
        value = (tags.get(key) or "").lower()
        if value in ("forward", "backward", "both"):
            return value
    return "both"


def _number(tags: dict, *keys: str) -> float | None:
    for key in keys:
        raw = tags.get(key)
        if not raw:
            continue
        try:
            return float(str(raw).split()[0].replace(",", "."))
        except ValueError:
            continue
    return None


def _crossing_style(tags: dict) -> str:
    if tags.get("crossing") == "unmarked" or tags.get("crossing:markings") == "no":
        return "unmarked"
    if tags.get("crossing") == "traffic_signals" or tags.get("highway") == "traffic_signals":
        return "signals"
    return "marked"


def _allows(oneway: str | None, step: int) -> bool:
    """Whether traffic may travel `step` (+1 with the way, -1 against it)."""
    if oneway in _ONEWAY_FORWARD:
        return step == 1
    if oneway in _ONEWAY_BACKWARD:
        return step == -1
    return True


@dataclass
class _Road:
    feat: object
    cat: str
    keys: list
    lls: list
    closed: bool
    roundabout: bool
    width_m: float
    sidewalk_m: float
    oneway: str | None
    name: str
    at: dict = field(default_factory=dict)
    skipped: bool = False


@dataclass
class Arm:
    road_id: int
    road_index: int
    here: tuple
    far: tuple
    step: int
    cat: str
    name: str
    roundabout: bool
    width_m: float
    sidewalk_m: float
    oneway: str | None
    inbound: bool
    control: str = "none"
    crosswalk: str | None = None
    # Filled once the roads have their final pixel positions.
    ox: float = 0.0
    oy: float = 0.0
    axis: str | None = None
    half_w: float = 1.0
    sidewalk: float = 0.0
    mouth: float = 2.0


@dataclass
class Cluster:
    keys: list
    kind: str = ""
    arms: list = field(default_factory=list)
    mini: bool = False
    turning: bool = False
    all_way: bool = False
    crosswalk_all: bool = False
    diameter_m: float | None = None
    centre: tuple | None = None
    box: tuple | None = None
    skip: tuple | None = None
    no_parking: list = field(default_factory=list)
    skipped: bool = False


@dataclass
class Crossing:
    road_id: int
    road_index: int
    before: tuple
    after: tuple
    fraction: float
    style: str
    width_m: float
    sidewalk_m: float
    cat: str
    ll: tuple
    px: tuple | None = None
    axis: str | None = None
    half_w: float = 1.0
    sidewalk: float = 0.0
    box: tuple | None = None
    no_parking: list = field(default_factory=list)
    skipped: bool = False


@dataclass
class Plan:
    clusters: list = field(default_factory=list)
    crossings: list = field(default_factory=list)
    roundabouts: list = field(default_factory=list)
    key_ll: dict = field(default_factory=dict)
    key_px: dict = field(default_factory=dict)
    mpt: float = 2.0

    def relocate(self, moved: dict, proj) -> None:
        """Pixel positions after the roads have been straightened.

        `moved` is what octilinear reported for each shared vertex. Anything
        it did not move — straightening off, or a point it never shared — is
        projected from where it was mapped.
        """
        self.key_px = {}
        for key, ll in self.key_ll.items():
            if key in moved:
                xy = moved[key]
                self.key_px[key] = (float(xy[0]), float(xy[1]))
            else:
                self.key_px[key] = proj.to_px(ll[0], ll[1])
        for cluster in self.clusters:
            pts = [self.key_px[k] for k in cluster.keys if k in self.key_px]
            if not pts:
                continue
            cluster.centre = (sum(p[0] for p in pts) / len(pts),
                              sum(p[1] for p in pts) / len(pts))
        for crossing in self.crossings:
            a = self.key_px.get(crossing.before)
            b = self.key_px.get(crossing.after)
            if a and b:
                t = crossing.fraction
                crossing.px = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            else:
                crossing.px = proj.to_px(crossing.ll[0], crossing.ll[1])

    def measure(self, mpt: float) -> None:
        """Bearings, mouths and the boxes the centre lines have to stop for.

        Called after relocate, so every key already has a pixel.
        """
        self.mpt = mpt if mpt > 0 else 1.0
        gap = max(1.0, CROSSWALK_GAP_M / self.mpt)
        park = max(2.0, NO_PARKING_M / self.mpt)
        for cluster in self.clusters:
            self._measure_cluster(cluster, gap, park)
        for crossing in self.crossings:
            self._measure_crossing(crossing, park)

    def _measure_cluster(self, cluster: Cluster, gap: float, park: float) -> None:
        alive = []
        for arm in cluster.arms:
            here = self.key_px.get(arm.here)
            far = self.key_px.get(arm.far)
            if here is None or far is None:
                continue
            dx, dy = far[0] - here[0], far[1] - here[1]
            length = math.hypot(dx, dy)
            if length < 1e-3:
                continue
            arm.ox, arm.oy = dx / length, dy / length
            if abs(dy) <= AXIS_SLOPE * abs(dx):
                arm.axis = "ew"
            elif abs(dx) <= AXIS_SLOPE * abs(dy):
                arm.axis = "ns"
            else:
                arm.axis = None
            arm.half_w = max(1.0, (arm.width_m / self.mpt) / 2.0)
            arm.sidewalk = arm.sidewalk_m / self.mpt
            alive.append(arm)
        cluster.arms = alive
        if not alive or cluster.centre is None:
            return
        others = [a.half_w for a in alive]
        for arm in alive:
            rest = [a.half_w for a in alive if a is not arm and a.axis != arm.axis]
            if not rest:
                rest = [a.half_w for a in alive if a is not arm]
            # Two arms of one road are not a cross street. A stop line a
            # whole carriageway back from a mid-block signal would sit in
            # the lane behind it.
            if len(alive) <= 2 and len({a.axis for a in alive}) <= 1:
                arm.mouth = 1.0
            else:
                arm.mouth = (max(rest) if rest else max(others)) + 1.0
        xs, ys = [], []
        reach_pts = []
        for arm in alive:
            here = self.key_px[arm.here]
            style = arm.crosswalk
            if style is None and cluster.crosswalk_all:
                style = "marked"
            reach = arm.mouth
            if style == "marked" or style == "signals":
                reach = arm.mouth + gap + 1.0
            elif arm.control != "none":
                reach = arm.mouth + 1.0
            hx, hy = here
            ox, oy = arm.ox, arm.oy
            px, py = -oy, ox
            for along in (0.0, reach):
                for side in (-arm.half_w - arm.sidewalk, arm.half_w + arm.sidewalk):
                    xs.append(hx + ox * along + px * side)
                    ys.append(hy + oy * along + py * side)
            reach_pts.append((hx + ox * reach, hy + oy * reach))
        if not xs:
            return
        x0, y0 = math.floor(min(xs)), math.floor(min(ys))
        x1, y1 = math.ceil(max(xs)), math.ceil(max(ys))
        cluster.box = (x0, y0, x1, y1)
        cluster.skip = cluster.box
        if cluster.kind not in _EMIT:
            cluster.no_parking = []
            return
        rects = [(x0, y0, x1 - x0, y1 - y0)]
        for arm in alive:
            here = self.key_px[arm.here]
            rects.append(_aabb(
                here[0], here[1], arm.ox, arm.oy,
                arm.mouth, arm.mouth + park, arm.half_w))
        cluster.no_parking = [r for r in rects if r[2] > 0 and r[3] > 0]

    def _measure_crossing(self, crossing: Crossing, park: float) -> None:
        a = self.key_px.get(crossing.before)
        b = self.key_px.get(crossing.after)
        if crossing.px is None:
            return
        if a and b:
            dx, dy = b[0] - a[0], b[1] - a[1]
        else:
            dx, dy = 1.0, 0.0
        if abs(dy) <= AXIS_SLOPE * abs(dx):
            crossing.axis = "ew"
        elif abs(dx) <= AXIS_SLOPE * abs(dy):
            crossing.axis = "ns"
        else:
            crossing.axis = None
        crossing.half_w = max(1.0, (crossing.width_m / self.mpt) / 2.0)
        crossing.sidewalk = crossing.sidewalk_m / self.mpt
        span = crossing.half_w + crossing.sidewalk + 1.0
        px, py = crossing.px
        if crossing.axis == "ew":
            box = (math.floor(px - 2), math.floor(py - span),
                   math.ceil(px + 2), math.ceil(py + span))
        elif crossing.axis == "ns":
            box = (math.floor(px - span), math.floor(py - 2),
                   math.ceil(px + span), math.ceil(py + 2))
        else:
            box = (math.floor(px - span), math.floor(py - span),
                   math.ceil(px + span), math.ceil(py + span))
        crossing.box = box
        x0, y0, x1, y1 = box
        crossing.no_parking = [(x0, y0, x1 - x0, y1 - y0)]
        # `park` keeps a car off the approach as well as the stripes.
        extra = max(span, park)
        if crossing.axis == "ew":
            crossing.no_parking.append(
                (math.floor(px - extra), math.floor(py - crossing.half_w),
                 math.ceil(2 * extra), math.ceil(2 * crossing.half_w)))
        elif crossing.axis == "ns":
            crossing.no_parking.append(
                (math.floor(px - crossing.half_w), math.floor(py - extra),
                 math.ceil(2 * crossing.half_w), math.ceil(2 * extra)))

    def dismiss(self, skip_ids: set, decks) -> None:
        """Drop a junction that sits on a bridge deck or on lifted geometry.

        The ground under a bridge is the road it crosses. Marking that as
        this junction would put a stop line on the wrong street.
        """
        for cluster in self.clusters:
            if any(arm.road_id in skip_ids for arm in cluster.arms):
                cluster.skipped = True
            elif cluster.centre is not None and _inside(cluster.centre, decks):
                cluster.skipped = True
        for road in self.roundabouts:
            if id(road.feat) in skip_ids:
                road.skipped = True
        for crossing in self.crossings:
            if crossing.road_id in skip_ids:
                crossing.skipped = True
            elif crossing.px is not None and _inside(crossing.px, decks):
                crossing.skipped = True

    def skip_rects(self) -> list:
        """Axis-aligned boxes the centre line must not enter."""
        rects = []
        for cluster in self.clusters:
            if cluster.skipped or cluster.kind not in _EMIT or cluster.skip is None:
                continue
            if cluster.kind in ("turning_circle", "service", "dead_end"):
                continue
            rects.append(cluster.skip)
        for crossing in self.crossings:
            if not crossing.skipped and crossing.box is not None and crossing.style != "unmarked":
                rects.append(crossing.box)
        return rects


def _aabb(hx, hy, ox, oy, near, far, half_w) -> tuple:
    px, py = -oy, ox
    xs, ys = [], []
    for along in (near, far):
        for side in (-half_w, half_w):
            xs.append(hx + ox * along + px * side)
            ys.append(hy + oy * along + py * side)
    x0, y0 = math.floor(min(xs)), math.floor(min(ys))
    x1, y1 = math.ceil(max(xs)), math.ceil(max(ys))
    return (x0, y0, x1 - x0, y1 - y0)


def _inside(px, decks) -> bool:
    x, y = px
    for x0, y0, x1, y1 in decks:
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


class Reserved:
    """Tiles street furniture must leave alone, and lamps the corners asked for."""

    # Junction boxes are small and scattered. A tile only needs the boxes that
    # share its cell; the test on each of those is the old half-open one.
    _CELL = 64

    def __init__(self):
        self.rects: list = []
        self.tiles: set = set()
        self.lamps: list = []
        self._cells: dict = {}
        self._bounds: tuple | None = None

    def add_rect(self, x0, y0, x1, y1) -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        if x1 > x0 and y1 > y0:
            rect = (x0, y0, x1, y1)
            self.rects.append(rect)
            cell = self._CELL
            for cy in range(y0 // cell, (y1 - 1) // cell + 1):
                for cx in range(x0 // cell, (x1 - 1) // cell + 1):
                    self._cells.setdefault((cx, cy), []).append(rect)
            if self._bounds is None:
                self._bounds = rect
            else:
                bx0, by0, bx1, by1 = self._bounds
                self._bounds = (min(bx0, x0), min(by0, y0),
                                max(bx1, x1), max(by1, y1))

    def add_tile(self, x, y) -> None:
        self.tiles.add((int(x), int(y)))

    def covers(self, x: int, y: int) -> bool:
        if (x, y) in self.tiles:
            return True
        bounds = self._bounds
        if bounds is None:
            return False
        bx0, by0, bx1, by1 = bounds
        if x < bx0 or y < by0 or x >= bx1 or y >= by1:
            return False
        cell = self._CELL
        for x0, y0, x1, y1 in self._cells.get((x // cell, y // cell), ()):
            if x0 <= x < x1 and y0 <= y < y1:
                return True
        return False


def collect(features, classify, is_polygon, way_width_m, sidewalk_m) -> Plan:
    """The junction graph, from the roads as mapped and the control nodes.

    Run this before the roads are straightened. `relocate` carries the result
    onto the moved streets.
    """
    plan = Plan()
    roads: list[_Road] = []
    links: dict[tuple, list] = {}
    key_roads: dict[tuple, set] = {}
    width_at: dict[tuple, float] = {}

    def add_link(a, b, ri):
        if a == b:
            return
        slot = links.setdefault(a, [])
        if (ri, b) not in slot:
            slot.append((ri, b))

    for feat in features:
        if feat.kind != "way" or len(feat.geometry) < 2 or is_polygon(feat):
            continue
        cat = classify(feat.tags)
        if cat not in ROAD_CATS:
            continue
        lls = list(feat.geometry)
        closed = len(lls) >= 4 and lls[0] == lls[-1]
        if closed:
            lls = lls[:-1]
        if len(lls) < 2:
            continue
        keys = [_key(lat, lon) for lat, lon in lls]
        road = _Road(
            feat, cat, keys, lls, closed,
            feat.tags.get("junction") in {"roundabout", "circular"},
            way_width_m(feat, cat),
            float(sidewalk_m.get(cat, 0.0) or 0.0),
            (feat.tags.get("oneway") or "").lower() or None,
            (feat.tags.get("name") or "").strip(),
        )
        ri = len(roads)
        for i, key in enumerate(keys):
            road.at.setdefault(key, i)
            plan.key_ll.setdefault(key, lls[i])
            key_roads.setdefault(key, set()).add(ri)
            width_at[key] = max(width_at.get(key, 0.0), road.width_m)
        for i in range(len(keys) - 1):
            add_link(keys[i], keys[i + 1], ri)
            add_link(keys[i + 1], keys[i], ri)
        if closed and len(keys) > 2:
            add_link(keys[-1], keys[0], ri)
            add_link(keys[0], keys[-1], ri)
        roads.append(road)
        if road.roundabout:
            plan.roundabouts.append(road)
    if not roads:
        return plan

    cuts = set()
    for key, lst in links.items():
        if len(lst) != 2 or lst[0][0] != lst[1][0]:
            cuts.add(key)

    def step_to(road: _Road, index: int, neighbor) -> int | None:
        n = len(road.keys)
        for step in (1, -1):
            nxt = (index + step) % n if road.closed else index + step
            if road.closed or 0 <= nxt < n:
                if road.keys[nxt] == neighbor:
                    return step
        return None

    # Edge lengths are `_metres` once. Later walks add those same floats in
    # the same order, and a cut is the key `next_cut` would have stopped on.
    import bisect
    road_ri = {}
    edge_of = []
    prefix_of = []
    cut_of = []
    key_indices = []
    from_prev = []
    for ri, road in enumerate(roads):
        road_ri[id(road)] = ri
        n = len(road.lls)
        count = n if road.closed else n - 1
        edges = [0.0] * count
        for i in range(count):
            j = (i + 1) % n
            edges[i] = _metres(road.lls[i], road.lls[j])
        prefix = [0.0] * (count + 1)
        for i in range(count):
            prefix[i + 1] = prefix[i] + edges[i]
        edge_of.append(edges)
        prefix_of.append(prefix)
        groups = {}
        for i, key in enumerate(road.keys):
            groups.setdefault(key, []).append(i)
        key_indices.append(groups)
        fwd = [None] * n
        back = [None] * n
        cut_idx = [i for i in range(n) if road.keys[i] in cuts]
        if not road.closed:
            nxt = None
            for i in range(n - 1, -1, -1):
                fwd[i] = nxt
                if road.keys[i] in cuts:
                    nxt = road.keys[i]
            prev = None
            for i in range(n):
                back[i] = prev
                if road.keys[i] in cuts:
                    prev = road.keys[i]
        elif cut_idx:
            last = len(cut_idx) - 1
            for i in range(n):
                at = bisect.bisect_right(cut_idx, i)
                c = cut_idx[at] if at <= last else cut_idx[0]
                if c != i:
                    fwd[i] = road.keys[c]
                at = bisect.bisect_left(cut_idx, i)
                c = cut_idx[at - 1] if at else cut_idx[-1]
                if c != i:
                    back[i] = road.keys[c]
        cut_of.append((fwd, back))
        # Forward metres from the previous cut's first vertex, left-folded
        # the way a +1 walk adds edges. Open roads only; rings stay on the walk.
        dist = [None] * n
        if not road.closed:
            for i in range(n):
                bk = back[i]
                if bk is None:
                    continue
                anchor = road.at.get(bk)
                if anchor is None or anchor >= i:
                    continue
                if i == anchor + 1:
                    dist[i] = edges[anchor]
                elif back[i - 1] == bk and dist[i - 1] is not None:
                    dist[i] = dist[i - 1] + edges[i - 1]
                else:
                    total = 0.0
                    for t in range(anchor, i):
                        total += edges[t]
                    dist[i] = total
        from_prev.append(dist)

    def next_cut(road: _Road, index: int, step: int):
        fwd, back = cut_of[road_ri[id(road)]]
        return fwd[index] if step == 1 else back[index]

    def _along(ri, i, k, step, limit):
        """Metres walking from vertex i to k, or None once past `limit`.

        Prefix subtraction only rejects a target that is already a metre
        past the limit. The returned sum is still the edge-by-edge add, so
        which junction is nearer does not move.
        """
        edges = edge_of[ri]
        prefix = prefix_of[ri]
        road = roads[ri]
        n = len(road.keys)
        if not road.closed:
            if step == 1:
                if k <= i or prefix[k] - prefix[i] > limit + 1.0:
                    return None
                dist = 0.0
                for t in range(i, k):
                    dist += edges[t]
                    if dist > limit:
                        return None
                return dist
            if k >= i or prefix[i] - prefix[k] > limit + 1.0:
                return None
            dist = 0.0
            for t in range(i - 1, k - 1, -1):
                dist += edges[t]
                if dist > limit:
                    return None
            return dist
        if step == 1:
            if k > i:
                if prefix[k] - prefix[i] > limit + 1.0:
                    return None
                dist = 0.0
                for t in range(i, k):
                    dist += edges[t]
                    if dist > limit:
                        return None
                return dist
            if (prefix[n] - prefix[i]) + prefix[k] > limit + 1.0:
                return None
            dist = 0.0
            for t in range(i, n):
                dist += edges[t]
                if dist > limit:
                    return None
            for t in range(0, k):
                dist += edges[t]
                if dist > limit:
                    return None
            return dist
        if k < i:
            if prefix[i] - prefix[k] > limit + 1.0:
                return None
            dist = 0.0
            for t in range(i - 1, k - 1, -1):
                dist += edges[t]
                if dist > limit:
                    return None
            return dist
        if prefix[i] + (prefix[n] - prefix[k]) > limit + 1.0:
            return None
        dist = 0.0
        for t in range(i - 1, -1, -1):
            dist += edges[t]
            if dist > limit:
                return None
        for t in range(n - 1, k - 1, -1):
            dist += edges[t]
            if dist > limit:
                return None
        return dist

    def walk_dist(road: _Road, i: int, j: int, step: int, limit: float):
        ri = road_ri[id(road)]
        n = len(road.lls)
        if (not road.closed and step == 1 and 0 <= j < n
                and cut_of[ri][1][j] is not None
                and road.at.get(cut_of[ri][1][j]) == i):
            dist = from_prev[ri][j]
            if dist is None or dist > limit:
                return None
            return dist
        if not road.closed:
            return _along(ri, i, j, step, limit)
        edges = edge_of[ri]
        dist = 0.0
        k = i
        for _ in range(n):
            nxt = (k + step) % n
            dist += edges[k if step == 1 else nxt]
            if dist > limit:
                return None
            k = nxt
            if k == j:
                return dist
            if k == i:
                return None
        return None

    def arms_from(keys) -> list[Arm]:
        made = []
        seen = set()
        keyset = set(keys)
        for key in keys:
            for ri, neighbor in links.get(key, ()):
                road = roads[ri]
                index = road.at.get(key)
                if index is None:
                    continue
                step = step_to(road, index, neighbor)
                if step is None:
                    continue
                far = next_cut(road, index, step)
                if far is None or far in keyset:
                    continue
                mark = (ri, key, far)
                if mark in seen:
                    continue
                seen.add(mark)
                made.append(Arm(
                    id(road.feat), ri, key, far, step, road.cat, road.name,
                    road.roundabout, road.width_m, road.sidewalk_m, road.oneway,
                    _allows(road.oneway, -step),
                ))
        return made

    # --- which vertices are junctions, and which of those are one junction --
    nodes = [f for f in features if f.kind == "node" and is_control(f.tags) and f.geometry]
    seeds = {key for key, lst in links.items() if len(lst) >= 3}
    for node in nodes:
        kind = node.tags.get("highway")
        if kind not in ("turning_circle", "turning_loop", "mini_roundabout"):
            continue
        key = _key(node.geometry[0][0], node.geometry[0][1])
        if key in links:
            seeds.add(key)

    lat0 = roads[0].lls[0][0]
    scale = max(0.2, math.cos(math.radians(lat0)))
    cell = 20.0 / 111320.0
    cell_lon = cell / scale

    def bucket(ll):
        return (int(math.floor(ll[0] / cell)), int(math.floor(ll[1] / cell_lon)))

    grid: dict[tuple, list] = {}
    for key in seeds:
        grid.setdefault(bucket(plan.key_ll[key]), []).append(key)

    parent = {key: key for key in seeds}
    members = {key: [key] for key in seeds}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def threshold(a, b):
        return max(CLUSTER_MIN_M, width_at.get(a, 6.0) + width_at.get(b, 6.0))

    pairs = []
    seen_pair = set()
    for key in seeds:
        cx, cy = bucket(plan.key_ll[key])
        for dx in range(-4, 5):
            for dy in range(-4, 5):
                for other in grid.get((cx + dx, cy + dy), ()):
                    if other <= key:
                        continue
                    mark = (key, other)
                    if mark in seen_pair:
                        continue
                    seen_pair.add(mark)
                    limit = threshold(key, other)
                    dist = _metres(plan.key_ll[key], plan.key_ll[other])
                    # Distance only, not "on the same way". The two carriageways
                    # of a dual road do not share a way, and they are still one
                    # junction. Complete linkage below stops a chain of ordinary
                    # blocks from collapsing into that same junction.
                    if dist <= limit:
                        pairs.append((dist, key, other))
    pairs.sort()
    for _dist, a, b in pairs:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        limit = threshold(a, b)
        # Complete linkage: a chain of short blocks must not become one
        # junction. Every member of both groups has to sit inside the span.
        if any(_metres(plan.key_ll[x], plan.key_ll[y]) > max(threshold(x, y), limit)
               for x in members[ra] for y in members[rb]):
            continue
        parent[rb] = ra
        members[ra].extend(members[rb])

    groups: dict[tuple, list] = {}
    for key in seeds:
        groups.setdefault(find(key), []).append(key)
    # A cul-de-sac bulb is not part of the junction down the street, even
    # when the street is shorter than the cluster span. Keep one-arm nodes
    # on their own unless every member is a dead end (it never is, here).
    clusters: list[Cluster] = []
    owners: dict[tuple, Cluster] = {}
    for keys in groups.values():
        big = [k for k in keys if len(links.get(k, ())) >= 3]
        small = [k for k in keys if k not in big]
        if big:
            cluster = Cluster(big)
            cluster.arms = arms_from(big)
            clusters.append(cluster)
            for key in big:
                owners[key] = cluster
        for key in small:
            cluster = Cluster([key])
            cluster.arms = arms_from([key])
            clusters.append(cluster)
            owners[key] = cluster

    owner_pos = []
    for ri, road in enumerate(roads):
        owner_pos.append([i for i, key in enumerate(road.keys) if key in owners])

    def _note_owner(key):
        for ri in key_roads.get(key, ()):
            pos = owner_pos[ri]
            for i in key_indices[ri].get(key, ()):
                at = bisect.bisect_left(pos, i)
                if at == len(pos) or pos[at] != i:
                    pos.insert(at, i)

    def ensure(key) -> Cluster | None:
        if key in owners:
            return owners[key]
        if key not in links:
            return None
        cluster = Cluster([key])
        cluster.arms = arms_from([key])
        clusters.append(cluster)
        owners[key] = cluster
        _note_owner(key)
        return cluster

    def arm_back(cluster: Cluster, road_index: int, here, step: int):
        """The arm at `here` pointing back along `step` (the side we arrived from)."""
        want = -step
        for arm in cluster.arms:
            if arm.road_index == road_index and arm.here == here and arm.step == want:
                return arm
        return None

    def find_junction(road: _Road, index: int, step: int, limit: float, skip):
        ri = road_ri[id(road)]
        pos = owner_pos[ri]
        npos = len(pos)
        if npos == 0:
            return None
        keys = road.keys

        def take(k):
            dist = _along(ri, index, k, step, limit)
            if dist is None:
                return None, True
            if keys[k] == skip:
                return None, False
            return (keys[k], dist), False

        if step == 1:
            p = bisect.bisect_right(pos, index)
            end = npos
            wrapped = False
            while True:
                if p >= end:
                    if not road.closed or wrapped:
                        return None
                    wrapped = True
                    p = 0
                    end = bisect.bisect_left(pos, index)
                    continue
                found, stop = take(pos[p])
                if found is not None:
                    return found
                if stop:
                    return None
                p += 1
        p = bisect.bisect_left(pos, index) - 1
        end = -1
        wrapped = False
        while True:
            if p <= end:
                if not road.closed or wrapped:
                    return None
                wrapped = True
                p = npos - 1
                end = bisect.bisect_right(pos, index) - 1
                continue
            found, stop = take(pos[p])
            if found is not None:
                return found
            if stop:
                return None
            p -= 1

    # seg: road, i, j, lat0, lon0, lat1, lon1, minlat, maxlat, minlon, maxlon, cos_floor
    snap_lat = SNAP_M / 111320.0
    seg_grid: dict[tuple, list] = {}
    placed = set()
    for road in roads:
        n = len(road.lls)
        count = n if road.closed else n - 1
        for i in range(count):
            j = (i + 1) % n
            a0, a1 = road.lls[i]
            b0, b1 = road.lls[j]
            minlat = a0 if a0 < b0 else b0
            maxlat = b0 if a0 < b0 else a0
            minlon = a1 if a1 < b1 else b1
            maxlon = b1 if a1 < b1 else a1
            lat_lo = minlat - snap_lat
            lat_hi = maxlat + snap_lat
            if lat_lo < -90.0:
                lat_lo = -90.0
            if lat_hi > 90.0:
                lat_hi = 90.0
            far = lat_lo if abs(lat_lo) >= abs(lat_hi) else lat_hi
            seg = (road, i, j, a0, a1, b0, b1, minlat, maxlat, minlon, maxlon,
                   math.cos(math.radians(far)))
            ca, cb = bucket(road.lls[i]), bucket(road.lls[j])
            steps = max(abs(ca[0] - cb[0]), abs(ca[1] - cb[1]), 1)
            for s in range(steps + 1):
                t = s / steps
                gcell = (int(round(ca[0] + (cb[0] - ca[0]) * t)),
                         int(round(ca[1] + (cb[1] - ca[1]) * t)))
                mark = (gcell, id(road), i)
                if mark in placed:
                    continue
                placed.add(mark)
                seg_grid.setdefault(gcell, []).append(seg)

    def segment_hit(lat, lon):
        """Nearest road segment within SNAP_M: (road, i0, i1, t)."""
        best = None
        cx, cy = bucket((lat, lon))
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for seg in seg_grid.get((cx + dx, cy + dy), ()):
                    mark = (id(seg[0]), seg[1])
                    if mark in seen:
                        continue
                    seen.add(mark)
                    lim = SNAP_M if best is None else best[0]
                    if lat < seg[7]:
                        dlat = seg[7] - lat
                    elif lat > seg[8]:
                        dlat = lat - seg[8]
                    else:
                        dlat = 0.0
                    if dlat * 111320.0 > lim + 1e-4:
                        continue
                    if lon < seg[9]:
                        dlon = seg[9] - lon
                    elif lon > seg[10]:
                        dlon = lon - seg[10]
                    else:
                        dlon = 0.0
                    if dlat != 0.0 or dlon != 0.0:
                        # cos_floor is at or below the projection's cosine, so
                        # this is short of the true metres. A segment that can
                        # still win is not dropped; equal distances keep the
                        # earlier one in this same cell order.
                        bound = dlon * 111320.0 * seg[11]
                        if dlat * dlat * (111320.0 * 111320.0) + bound * bound > (lim + 1.0) * (lim + 1.0):
                            continue
                    a0, a1, b0, b1 = seg[3], seg[4], seg[5], seg[6]
                    mid = math.radians((a0 + b0 + lat) / 3.0)
                    cos_mid = math.cos(mid)
                    bx = (b1 - a1) * 111320.0 * cos_mid
                    by = (b0 - a0) * 111320.0
                    px = (lon - a1) * 111320.0 * cos_mid
                    py = (lat - a0) * 111320.0
                    length2 = bx * bx + by * by
                    if length2 <= 1e-6:
                        dist = math.hypot(px, py)
                        t = 0.0
                    else:
                        t = (px * bx + py * by) / length2
                        if t < 0.0:
                            t = 0.0
                        elif t > 1.0:
                            t = 1.0
                        dist = math.hypot(px - bx * t, py - by * t)
                    if dist <= SNAP_M and (best is None or dist < best[0]):
                        best = (dist, seg[0], seg[1], seg[2], t)
                        if dist == 0.0:
                            return best[1:]
        return None if best is None else best[1:]

    def locate_on_road(road: _Road, index: int, kind: str, limit: float, skip):
        facing = _direction_of(kind)
        steps = []
        if facing in ("forward", "both"):
            steps.append(1)
        if facing in ("backward", "both"):
            steps.append(-1)
        best = None
        for step in steps:
            found = find_junction(road, index, step, limit, skip)
            if found is not None and (best is None or found[1] < best[1]):
                best = (found[0], found[1], step, road)
        return best

    # direction depends on the node; bind it per call via this holder
    facing_kind = {"kind": "stop"}

    def _direction_of(kind: str) -> str:
        return _direction(facing_kind["tags"], kind)

    def apply_at(cluster: Cluster, tags: dict, arm: Arm | None, at_junction: bool) -> None:
        kind = tags.get("highway")
        if kind == "mini_roundabout":
            cluster.mini = True
            return
        if kind in ("turning_circle", "turning_loop"):
            cluster.turning = True
            diameter = _number(tags, "diameter")
            radius = _number(tags, "radius")
            cluster.diameter_m = diameter if diameter else (radius * 2.0 if radius else None)
            return
        if kind == "crossing":
            style = _crossing_style(tags)
            if arm is not None:
                arm.crosswalk = _merge_cross(arm.crosswalk, style)
            else:
                _note_cross(cluster, style)
            return
        if kind == "traffic_signals":
            if tags.get("crossing") == "traffic_signals":
                cluster.crosswalk_all = True
            if at_junction:
                for one in cluster.arms:
                    if one.inbound:
                        one.control = _prefer(one.control, "signal")
            elif arm is not None and arm.inbound:
                arm.control = _prefer(arm.control, "signal")
            return
        if kind in ("stop", "give_way"):
            control = "stop" if kind == "stop" else "give_way"
            all_way = kind == "stop" and (
                tags.get("stop") == "all"
                or (at_junction and _one_class(cluster.arms)))
            if all_way:
                cluster.all_way = True
                for one in cluster.arms:
                    if one.inbound:
                        one.control = _prefer(one.control, "stop")
                return
            if at_junction:
                top = max((_RANK.get(one.cat, 0) for one in cluster.arms), default=0)
                for one in cluster.arms:
                    if one.inbound and _RANK.get(one.cat, 0) < top:
                        one.control = _prefer(one.control, control)
                # Every arm the same class, and the node is not stop=all:
                # the mapper put one sign on the junction. Treat it as all-way
                # only for stop; a give-way on the node marks the lesser roads,
                # and with one class there is no lesser road, so every inbound
                # arm gives way.
                if top and all(_RANK.get(one.cat, 0) == top for one in cluster.arms):
                    for one in cluster.arms:
                        if one.inbound:
                            one.control = _prefer(one.control, control)
                    if control == "stop":
                        cluster.all_way = True
            elif arm is not None and arm.inbound:
                arm.control = _prefer(arm.control, control)
            return

    def _one_class(arms) -> bool:
        cats = {arm.cat for arm in arms}
        return len(cats) <= 1

    def _note_cross(cluster: Cluster, style: str) -> None:
        if style == "unmarked":
            for arm in cluster.arms:
                if arm.crosswalk is None:
                    arm.crosswalk = "unmarked"
            return
        cluster.crosswalk_all = True

    def _merge_cross(old, style):
        if old == "unmarked" or style == "unmarked":
            return "unmarked"
        if old == "signals" or style == "signals":
            return "signals"
        return style or old

    def consider(node) -> None:
        tags = node.tags or {}
        kind = tags.get("highway")
        lat, lon = node.geometry[0]
        key = _key(lat, lon)
        facing_kind["tags"] = tags
        on_vertex = key in links
        if not on_vertex:
            hit = segment_hit(lat, lon)
            if hit is None:
                return
            road_for, i0, i1, t = hit
            seg = edge_of[road_ri[id(road_for)]][i0]
            d0, d1 = t * seg, (1.0 - t) * seg
            if d0 <= d1 and d0 <= SNAP_M:
                key = road_for.keys[i0]
                on_vertex = True
            elif d1 <= SNAP_M:
                key = road_for.keys[i1]
                on_vertex = True
            else:
                _span_or_approach(node, road_for, i0, i1, d0, d1, kind, tags, (lat, lon))
                return
        if key in owners or (kind in ("turning_circle", "turning_loop", "mini_roundabout")):
            cluster = ensure(key) if key not in owners else owners.get(key)
            if cluster is None:
                cluster = ensure(key)
            if cluster is not None:
                apply_at(cluster, tags, None, True)
            return
        limit = CROSSING_NEAR_M if kind == "crossing" else APPROACH_M
        best = None
        for ri in key_roads.get(key, ()):
            road = roads[ri]
            found = locate_on_road(road, road.at[key], kind, limit, key)
            if found is not None and (best is None or found[1] < best[1]):
                best = found
        if best is not None:
            here, _dist, step, road = best
            cluster = owners.get(here)
            if cluster is None:
                return
            arm = arm_back(cluster, road_ri[id(road)], here, step)
            apply_at(cluster, tags, arm, False)
            if kind == "traffic_signals" and tags.get("crossing") == "traffic_signals" and arm is not None:
                arm.crosswalk = _merge_cross(arm.crosswalk, "signals")
            return
        if kind == "traffic_signals" and tags.get("crossing") not in (None, "no"):
            _add_crossing(key, tags, (lat, lon))
            return
        if kind == "traffic_signals":
            cluster = ensure(key)
            if cluster is not None:
                apply_at(cluster, tags, None, True)
            return
        if kind == "crossing":
            _add_crossing(key, tags, (lat, lon))

    def _span_or_approach(node, road, i0, i1, d0, d1, kind, tags, ll):
        """A control node that fell between two vertices, not on one.

        Forward travel runs toward i1. The end vertex counts when it is
        itself the junction; otherwise the walk carries on in that direction.
        `step` in the result is the direction of travel on arrival, which is
        what `arm_back` expects.
        """
        limit = CROSSING_NEAR_M if kind == "crossing" else APPROACH_M
        facing = _direction(tags, kind)
        options = []
        if facing in ("forward", "both"):
            options.append((i1, 1, d1))
        if facing in ("backward", "both"):
            options.append((i0, -1, d0))
        best = None
        for index, step, already in options:
            end = road.keys[index]
            if end in owners and already <= limit and (best is None or already < best[0]):
                best = (already, end, step, road)
            found = find_junction(road, index, step, max(0.0, limit - already), end)
            if found is None:
                continue
            dist = found[1] + already
            if dist <= limit and (best is None or dist < best[0]):
                best = (dist, found[0], step, road)
        near = best is not None and (kind != "crossing" or best[0] <= CROSSING_NEAR_M)
        if near:
            _dist, here, step, road = best
            cluster = owners.get(here)
            if cluster is not None:
                arm = arm_back(cluster, road_ri[id(road)], here, step)
                apply_at(cluster, tags, arm, False)
                if kind == "traffic_signals" and tags.get("crossing") == "traffic_signals" \
                        and arm is not None:
                    arm.crosswalk = _merge_cross(arm.crosswalk, "signals")
                return
        if kind in ("crossing", "traffic_signals"):
            _crossing_between(road, i0, i1, (d0, d1), tags, ll)

    def _crossing_between(road, i0, i1, distances, tags, ll):
        before = next_cut(road, i0, -1) or road.keys[i0]
        after = next_cut(road, i1, 1) or road.keys[i1]
        if before == after:
            return
        db = walk_dist(road, road.at[before], i0, 1, 1e9) or 0.0
        db += distances[0]
        da = walk_dist(road, i1, road.at[after], 1, 1e9) or 0.0
        da += distances[1]
        total = db + edge_of[road_ri[id(road)]][i0] + da
        fraction = 0.0 if total <= 0 else db / total
        style = _crossing_style(tags)
        if tags.get("highway") == "traffic_signals" and tags.get("crossing") in (None, "no"):
            return
        plan.crossings.append(Crossing(
            id(road.feat), road_ri[id(road)], before, after, fraction, style,
            road.width_m, road.sidewalk_m, road.cat, ll,
        ))

    def _add_crossing(key, tags, ll):
        roads_here = list(key_roads.get(key, ()))
        if not roads_here:
            return
        road = roads[roads_here[0]]
        index = road.at[key]
        before = next_cut(road, index, -1)
        after = next_cut(road, index, 1)
        if before is None or after is None or before == after:
            return
        db = walk_dist(road, road.at[before], index, 1, 1e9)
        da = walk_dist(road, index, road.at[after], 1, 1e9)
        if db is None or da is None:
            db = walk_dist(road, index, road.at[before], -1, 1e9) or 0.0
            da = walk_dist(road, index, road.at[after], 1, 1e9) or 0.0
        total = (db or 0.0) + (da or 0.0)
        fraction = 0.0 if total <= 0 else (db or 0.0) / total
        plan.crossings.append(Crossing(
            id(road.feat), road_ri[id(road)], before, after, fraction,
            _crossing_style(tags), road.width_m, road.sidewalk_m, road.cat, ll,
        ))

    for node in nodes:
        consider(node)

    for cluster in clusters:
        cluster.kind = _kind_of(cluster)
    plan.clusters = [c for c in clusters if c.kind]
    return plan


def _kind_of(cluster: Cluster) -> str:
    arms = cluster.arms
    if len(arms) <= 1 and cluster.turning:
        return "turning_circle"
    if any(arm.roundabout for arm in arms):
        return "roundabout"
    if cluster.mini:
        return "mini_roundabout"
    if any(arm.control == "signal" for arm in arms):
        return "signals"
    if cluster.all_way:
        return "all_way_stop"
    if any(arm.control == "stop" for arm in arms):
        return "stop"
    if any(arm.control == "give_way" for arm in arms):
        return "give_way"
    nonservice = {arm.road_index for arm in arms if arm.cat != "road_service"}
    if len(arms) >= 3 and len(nonservice) <= 1:
        return "service"
    if len(arms) < 3:
        return ""
    return "uncontrolled"


def _project(a, b, p) -> tuple[float, float]:
    """Distance in metres from p to segment ab, and the fraction along ab."""
    lat, lon = p
    mid = math.radians((a[0] + b[0] + lat) / 3.0)
    def xy(ll):
        return ((ll[1] - a[1]) * 111320.0 * math.cos(mid),
                (ll[0] - a[0]) * 111320.0)
    ax, ay = 0.0, 0.0
    bx, by = xy(b)
    px, py = xy(p)
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-6:
        return math.hypot(px, py), 0.0
    t = max(0.0, min(1.0, (px * dx + py * dy) / length2))
    return math.hypot(px - dx * t, py - dy * t), t


def _face(ox: float, oy: float) -> str:
    """The compass point an outward arm points at. Tile y grows south."""
    if abs(ox) >= abs(oy):
        return "E" if ox > 0 else "W"
    return "S" if oy > 0 else "N"


def shape_ground(landscape, plan: Plan, proj, cover, building_rings,
                 skip_ids, decks) -> None:
    """Square the corners, cut roundabout islands, lay bulbs.

    Kerbs are painted from whatever this leaves, so a corner that is still a
    blob of tarmac becomes a row of teeth. Only axis-aligned junctions are
    rebuilt: a diagonal has no corner tile that reads as a corner.
    """
    plan.dismiss(skip_ids or set(), decks or ())
    if landscape is None or plan is None:
        return
    import numpy as np
    from PIL import Image, ImageDraw
    from shapely.geometry import Polygon

    prepared = []
    for ring in building_rings or ():
        if len(ring) < 3:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        prepared.append((min(xs), min(ys), max(xs), max(ys), ring))

    def building_mask(x0, y0, x1, y1):
        mask = Image.new("L", (max(1, x1 - x0), max(1, y1 - y0)), 0)
        draw = ImageDraw.Draw(mask)
        hit = False
        for bx0, by0, bx1, by1, ring in prepared:
            if bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1:
                continue
            draw.polygon([(px - x0, py - y0) for px, py in ring], fill=255)
            hit = True
        if not hit:
            return None
        return np.asarray(mask) > 0

    def run(x0, y0, x1, y1, decide) -> None:
        x0 = max(0, int(math.floor(x0)))
        y0 = max(0, int(math.floor(y0)))
        x1 = min(landscape.width, int(math.ceil(x1)))
        y1 = min(landscape.height, int(math.ceil(y1)))
        if x1 <= x0 or y1 <= y0:
            return
        buildings = building_mask(x0, y0, x1, y1)
        ground = np.asarray(landscape.crop((x0, y0, x1, y1))).copy()
        dirt = np.zeros(ground.shape[:2], dtype=bool)
        town = np.zeros(ground.shape[:2], dtype=bool)
        opened = np.zeros(ground.shape[:2], dtype=bool)
        h, w = ground.shape[:2]
        if buildings is not None and buildings.shape != (h, w):
            buildings = buildings[:h, :w]
        for y in range(h):
            row = ground[y]
            for x in range(w):
                if buildings is not None and buildings[y, x]:
                    continue
                pixel = (int(row[x, 0]), int(row[x, 1]), int(row[x, 2]))
                if pixel == C.WATER:
                    continue
                colour, biome = decide(x, y, pixel, x0, y0)
                if colour is None:
                    continue
                row[x] = colour
                if biome == biomes.DIRT:
                    dirt[y, x] = True
                elif biome == biomes.TOWN:
                    town[y, x] = True
                elif biome == "open":
                    opened[y, x] = True
        landscape.paste(Image.fromarray(ground), (x0, y0))
        if cover is None:
            return
        # Masks share the batch opened below. Empty ones are skipped. The
        # biome image is rebuilt once, when shaping finishes.
        for mask, value in ((dirt, biomes.DIRT), (town, biomes.TOWN),
                            (opened, cover.open)):
            if not mask.any():
                continue
            cover.paint_batch(mask, value, x0, y0)

    try:
        if cover is not None:
            cover.begin_mask_batch()
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
            inset = max(1.0, (road.width_m / plan.mpt) / 2.0
                        + road.sidewalk_m / plan.mpt + 0.5)
            inner = ring.buffer(-inset)
            parts = []
            if getattr(inner, "geom_type", "") == "Polygon":
                parts = [inner]
            else:
                parts = [g for g in getattr(inner, "geoms", ()) if g.geom_type == "Polygon"]
            parts = [g for g in parts if not g.is_empty]
            if not parts:
                continue
            area = sum(part.area for part in parts)
            radius_m = math.sqrt(max(area, 0.0) / math.pi) * plan.mpt
            fill = C.PAVING if radius_m < ISLAND_PAVING_M else C.MEDIUM_GRASS
            biome = biomes.TOWN if fill == C.PAVING else "open"
            minx = min(part.bounds[0] for part in parts)
            miny = min(part.bounds[1] for part in parts)
            maxx = max(part.bounds[2] for part in parts)
            maxy = max(part.bounds[3] for part in parts)
            x0 = max(0, int(math.floor(minx)))
            y0 = max(0, int(math.floor(miny)))
            x1 = min(landscape.width, int(math.ceil(maxx)) + 1)
            y1 = min(landscape.height, int(math.ceil(maxy)) + 1)
            if x1 <= x0 or y1 <= y0:
                continue
            mask_img = Image.new("L", (x1 - x0, y1 - y0), 0)
            draw = ImageDraw.Draw(mask_img)
            for part in parts:
                draw.polygon([(px - x0, py - y0) for px, py in part.exterior.coords], fill=255)
                for hole in part.interiors:
                    draw.polygon([(px - x0, py - y0) for px, py in hole.coords], fill=0)
            island = np.asarray(mask_img) > 0

            def decide_island(x, y, pixel, _ox, _oy, _island=island, _fill=fill, _biome=biome):
                # Water and building footprints are already left alone. The hole
                # in the ring is the island whether or not the old disc of tarmac
                # is still there.
                if y >= _island.shape[0] or x >= _island.shape[1] or not _island[y, x]:
                    return None, None
                if pixel == _fill:
                    return None, None
                return _fill, _biome

            run(x0, y0, x1, y1, decide_island)

        for cluster in plan.clusters:
            if cluster.skipped or cluster.centre is None or cluster.box is None:
                continue
            if cluster.kind == "turning_circle":
                _bulb(cluster, plan, run)
            elif cluster.kind in _CORNERS and len(cluster.keys) == 1 \
                    and len(cluster.arms) >= 3 and all(arm.axis for arm in cluster.arms):
                _square(cluster, plan, run)
                if cluster.kind == "mini_roundabout":
                    _mini(cluster, plan, run)
            elif cluster.kind == "mini_roundabout":
                _mini(cluster, plan, run)
    finally:
        if cover is not None:
            cover.end_mask_batch()


def _square(cluster: Cluster, plan: Plan, run) -> None:
    arms = cluster.arms
    x0, y0, x1, y1 = cluster.box
    lengths = {id(arm): arm.mouth + 2.0 for arm in arms}

    def decide(x, y, pixel, ox, oy):
        wx, wy = x + ox + 0.5, y + oy + 0.5
        rank = 0
        band = False
        for arm in arms:
            here = plan.key_px[arm.here]
            dx, dy = wx - here[0], wy - here[1]
            along = dx * arm.ox + dy * arm.oy
            across = abs(dx * (-arm.oy) + dy * arm.ox)
            if -0.5 <= along <= lengths[id(arm)]:
                if across <= arm.half_w:
                    rank = max(rank, _RANK.get(arm.cat, 1))
                elif across <= arm.half_w + max(arm.sidewalk, 1.0):
                    band = True
        if rank and pixel in _ASPHALT | _GRASS | {C.PALE_CONCRETE}:
            return _COLOUR[rank], biomes.DIRT
        # Verge is the dark grass strip. A park is medium grass and stays.
        if band and (pixel in _ASPHALT or pixel == C.DARK_GRASS):
            return C.PALE_CONCRETE, biomes.TOWN
        return None, None

    run(x0, y0, x1, y1, decide)


def _mini(cluster: Cluster, plan: Plan, run) -> None:
    x0, y0, x1, y1 = cluster.box
    if (x1 - x0) < MINI_MIN_TILES or (y1 - y0) < MINI_MIN_TILES:
        return
    cx, cy = cluster.centre
    radius = MINI_RADIUS_TILES

    def decide(x, y, pixel, ox, oy):
        wx, wy = x + ox + 0.5, y + oy + 0.5
        if (wx - cx) ** 2 + (wy - cy) ** 2 > radius * radius:
            return None, None
        if pixel == C.WATER or pixel in _GRASS and pixel != C.DARK_GRASS:
            return None, None
        return C.PAVING, biomes.TOWN

    run(cx - radius, cy - radius, cx + radius + 1, cy + radius + 1, decide)


def _bulb(cluster: Cluster, plan: Plan, run) -> None:
    arm = cluster.arms[0] if cluster.arms else None
    if arm is None or cluster.centre is None:
        return
    diameter = cluster.diameter_m
    if diameter is None:
        diameter = min(TURNING_MAX_M, max(TURNING_MIN_M, arm.width_m * 1.2))
    else:
        diameter = min(TURNING_MAX_M, max(TURNING_MIN_M, diameter))
    radius = diameter / 2.0 / plan.mpt
    sidewalk = arm.sidewalk
    outer = radius + max(sidewalk, 1.0)
    cx, cy = cluster.centre
    colour = _COLOUR.get(_RANK.get(arm.cat, 2), C.MEDIUM_ASPHALT)

    def decide(x, y, pixel, ox, oy):
        # Only ground that is not already a road, so the street still joins
        # the bulb instead of being repainted in a circle.
        if pixel in _ASPHALT or pixel == C.WATER:
            return None, None
        wx = x + ox + 0.5
        wy = y + oy + 0.5
        d = math.hypot(wx - cx, wy - cy)
        if d <= radius:
            return colour, biomes.DIRT
        if d <= outer:
            return C.PALE_CONCRETE, biomes.TOWN
        return None, None

    run(cx - outer, cy - outer, cx + outer + 1, cy + outer + 1, decide)


def paint_controls(veg, landscape, plan: Plan, proj) -> Reserved:
    """Stop lines, crosswalks, dropped kerbs, signs and signal poles.

    Diagonal arms get the sign or the pole only. A line that steps from tile
    to tile reads as a zigzag, which is why the centre lines skip them too.
    """
    reserved = Reserved()
    if veg is None or landscape is None or plan is None:
        return reserved
    gap = max(1.0, CROSSWALK_GAP_M / plan.mpt)
    ground = landscape.load()
    layer = veg.load()
    width, height = landscape.size
    for cluster in plan.clusters:
        if cluster.skipped or cluster.kind not in _EMIT or cluster.centre is None:
            continue
        if cluster.box is not None and cluster.kind != "turning_circle":
            reserved.add_rect(*cluster.box)
        _name_post(cluster, plan, ground, layer, width, height, reserved)
        if cluster.kind == "signals":
            _corner_lamps(cluster, plan, reserved)
        for arm in cluster.arms:
            if not arm.inbound and arm.crosswalk is None and not cluster.crosswalk_all:
                continue
            style = arm.crosswalk
            if style is None and cluster.crosswalk_all:
                style = "marked"
            _arm_markings(arm, style, cluster, plan, gap, ground, layer,
                          width, height, reserved)
    for crossing in plan.crossings:
        if crossing.skipped or crossing.px is None or crossing.axis is None:
            continue
        if crossing.box is not None:
            reserved.add_rect(*crossing.box)
        _midblock(crossing, plan, gap, ground, layer, width, height, reserved)
    return reserved


def _arm_markings(arm: Arm, style, cluster, plan, gap, ground, layer,
                  width, height, reserved: Reserved) -> None:
    here = plan.key_px.get(arm.here)
    if here is None or (arm.ox == 0 and arm.oy == 0):
        return
    hx, hy = here
    lined = arm.axis is not None
    cross = style in ("marked", "signals") and lined
    stop_at = arm.mouth + (gap + 1.0 if cross else (1.0 if arm.control != "none" else 0.0))
    if cross:
        _across(arm, hx, hy, arm.mouth, -arm.half_w, arm.half_w,
                _CROSSWALK[arm.axis], ground, layer, width, height, False, reserved)
        _across(arm, hx, hy, arm.mouth + gap, -arm.half_w, arm.half_w,
                _CROSSWALK[arm.axis], ground, layer, width, height, False, reserved)
        _kerb_gap(arm, hx, hy, arm.mouth, arm.mouth + gap, layer, width, height)
    if arm.control != "none" and arm.inbound and lined:
        full = arm.oneway in _ONEWAY_FORWARD or arm.oneway in _ONEWAY_BACKWARD
        side0, side1 = _inbound_half(arm, full)
        if arm.control == "give_way":
            colour = _GIVE_LINE[arm.axis]
            dashed = True
        else:
            colour = _STOP_LINE[arm.axis][0]
            dashed = False
        _across(arm, hx, hy, stop_at, side0, side1, colour, ground, layer,
                width, height, dashed, reserved)
    if arm.control == "stop" and arm.inbound:
        _prop(arm, hx, hy, stop_at, _STOP_SIGN[_face(arm.ox, arm.oy)],
              ground, layer, width, height, reserved)
    elif arm.control == "signal" and arm.inbound:
        _prop(arm, hx, hy, stop_at, _SIGNAL[_face(arm.ox, arm.oy)],
              ground, layer, width, height, reserved)


def _inbound_half(arm: Arm, full: bool) -> tuple[float, float]:
    if full:
        return -arm.half_w, arm.half_w
    # Right of the driver, who travels against the arm (toward the junction).
    tx, ty = -arm.ox, -arm.oy
    rx, ry = -ty, tx
    px, py = -arm.oy, arm.ox
    if px * rx + py * ry >= 0:
        return 0.0, arm.half_w
    return -arm.half_w, 0.0


def _across(arm, hx, hy, along, side0, side1, colour, ground, layer,
            width, height, dashed, reserved: Reserved) -> None:
    px, py = -arm.oy, arm.ox
    cx = hx + arm.ox * along
    cy = hy + arm.oy * along
    span = abs(side1 - side0)
    steps = max(1, int(round(span)))
    for i in range(steps + 1):
        if dashed and i % 2:
            continue
        t = side0 + (side1 - side0) * (i / steps)
        x = int(round(cx + px * t))
        y = int(round(cy + py * t))
        if not (0 <= x < width and 0 <= y < height):
            continue
        if ground[x, y] not in _ASPHALT:
            continue
        if layer[x, y] not in (C.VEG_NOTHING,) and layer[x, y] not in _LINES:
            continue
        layer[x, y] = colour
        reserved.add_tile(x, y)


def _kerb_gap(arm, hx, hy, near, far, layer, width, height) -> None:
    """Drop the kerb where the crosswalk meets the pavement."""
    px, py = -arm.oy, arm.ox
    reach = max(arm.sidewalk, 1.0) + 1.0
    steps_along = max(1, int(round(abs(far - near))) + 1)
    for i in range(steps_along + 1):
        along = near + (far - near) * (i / steps_along)
        for sign in (-1, 1):
            for step in range(0, int(math.ceil(reach)) + 1):
                side = sign * (arm.half_w + step)
                x = int(round(hx + arm.ox * along + px * side))
                y = int(round(hy + arm.oy * along + py * side))
                if 0 <= x < width and 0 <= y < height and layer[x, y] in _KERBS:
                    layer[x, y] = C.VEG_NOTHING


def _prop(arm, hx, hy, along, colour, ground, layer, width, height,
          reserved: Reserved) -> None:
    tx, ty = -arm.ox, -arm.oy
    rx, ry = -ty, tx
    for extra in (2, 3, 4):
        x = int(round(hx + arm.ox * along + rx * (arm.half_w + extra)))
        y = int(round(hy + arm.oy * along + ry * (arm.half_w + extra)))
        if not (0 <= x < width and 0 <= y < height):
            return
        if ground[x, y] in _ASPHALT or ground[x, y] == C.WATER:
            continue
        if layer[x, y] != C.VEG_NOTHING:
            continue
        layer[x, y] = colour
        reserved.add_tile(x, y)
        return


def _name_post(cluster, plan, ground, layer, width, height, reserved: Reserved) -> None:
    names = {arm.name for arm in cluster.arms if arm.name}
    if len(names) < 2 or cluster.box is None:
        return
    x0, y0, x1, y1 = cluster.box
    for x, y in ((x0 - 1, y0 - 1), (x1, y0 - 1), (x0 - 1, y1), (x1, y1)):
        if not (0 <= x < width and 0 <= y < height):
            continue
        if ground[x, y] in _ASPHALT or ground[x, y] == C.WATER:
            continue
        if layer[x, y] != C.VEG_NOTHING:
            continue
        layer[x, y] = C.STREET_NAME_SIGN
        reserved.add_tile(x, y)
        return


def _corner_lamps(cluster, plan, reserved: Reserved) -> None:
    if cluster.box is None or not all(arm.axis for arm in cluster.arms):
        return
    x0, y0, x1, y1 = cluster.box
    cx, cy = cluster.centre
    for x, y in ((x0 - 1, y0 - 1), (x1, y0 - 1), (x0 - 1, y1), (x1, y1)):
        dx, dy = cx - x, cy - y
        if abs(dx) >= abs(dy):
            facing = "E" if dx > 0 else "W"
        else:
            facing = "S" if dy > 0 else "N"
        reserved.lamps.append((x, y, _LAMP[facing]))


def _midblock(crossing: Crossing, plan, gap, ground, layer, width, height,
              reserved: Reserved) -> None:
    px, py = crossing.px
    axis = crossing.axis
    half = crossing.half_w
    if crossing.style != "unmarked":
        colour = _CROSSWALK[axis]
        for shift in (-gap / 2.0, gap / 2.0):
            if axis == "ew":
                _run(px + shift, py, 0.0, 1.0, -half, half, colour,
                     ground, layer, width, height, False, reserved)
            else:
                _run(px, py + shift, 1.0, 0.0, -half, half, colour,
                     ground, layer, width, height, False, reserved)
        _mid_kerb(crossing, gap, layer, width, height)
    if crossing.style != "signals":
        return
    # Both approaches: each driver stops on their right, short of the stripes.
    for sign in (-1, 1):
        along = sign * (gap / 2.0 + 1.0)
        # sign +1 is the east (ew) or south (ns) side. Traffic approaching
        # from that side travels the other way, and keeps right.
        if axis == "ew":
            travel = -sign  # +x travel when approaching from the west (sign -1)
            side0, side1 = (0.0, half) if travel > 0 else (-half, 0.0)
            # Eastbound (travel +x) keeps south, which is +y, side > 0.
            if travel > 0:
                side0, side1 = 0.0, half
            else:
                side0, side1 = -half, 0.0
            _run(px + along, py, 0.0, 1.0, side0, side1, _STOP_LINE["ew"][0],
                 ground, layer, width, height, False, reserved)
            rx = 0.0
            ry = 1.0 if travel > 0 else -1.0
            _stand(px + along, py, rx, ry, half, _SIGNAL["W" if travel > 0 else "E"],
                   ground, layer, width, height, reserved)
        else:
            travel = -sign
            if travel > 0:
                side0, side1 = 0.0, half  # southbound keeps west? y-down south is +y
            # Travel +y (south): right is west, -x, side < 0.
            if travel > 0:
                side0, side1 = -half, 0.0
            else:
                side0, side1 = 0.0, half
            _run(px, py + along, 1.0, 0.0, side0, side1, _STOP_LINE["ns"][0],
                 ground, layer, width, height, False, reserved)
            rx = -1.0 if travel > 0 else 1.0
            _stand(px, py + along, rx, 0.0, half,
                   _SIGNAL["N" if travel > 0 else "S"],
                   ground, layer, width, height, reserved)


def _run(cx, cy, px, py, side0, side1, colour, ground, layer,
         width, height, dashed, reserved: Reserved) -> None:
    span = abs(side1 - side0)
    steps = max(1, int(round(span)))
    for i in range(steps + 1):
        if dashed and i % 2:
            continue
        t = side0 + (side1 - side0) * (i / steps)
        x = int(round(cx + px * t))
        y = int(round(cy + py * t))
        if not (0 <= x < width and 0 <= y < height):
            continue
        if ground[x, y] not in _ASPHALT:
            continue
        if layer[x, y] not in (C.VEG_NOTHING,) and layer[x, y] not in _LINES:
            continue
        layer[x, y] = colour
        reserved.add_tile(x, y)


def _stand(cx, cy, rx, ry, half, colour, ground, layer, width, height,
           reserved: Reserved) -> None:
    for extra in (2, 3, 4):
        x = int(round(cx + rx * (half + extra)))
        y = int(round(cy + ry * (half + extra)))
        if not (0 <= x < width and 0 <= y < height):
            return
        if ground[x, y] in _ASPHALT or ground[x, y] == C.WATER:
            continue
        if layer[x, y] != C.VEG_NOTHING:
            continue
        layer[x, y] = colour
        reserved.add_tile(x, y)
        return


def _mid_kerb(crossing: Crossing, gap, layer, width, height) -> None:
    px, py = crossing.px
    reach = int(math.ceil(max(crossing.sidewalk, 1.0) + 1.0))
    half = crossing.half_w
    along = int(math.ceil(gap)) + 1
    for da in range(-along, along + 1):
        for sign in (-1, 1):
            for step in range(0, reach + 1):
                if crossing.axis == "ew":
                    x = int(round(px + da))
                    y = int(round(py + sign * (half + step)))
                else:
                    x = int(round(px + sign * (half + step)))
                    y = int(round(py + da))
                if 0 <= x < width and 0 <= y < height and layer[x, y] in _KERBS:
                    layer[x, y] = C.VEG_NOTHING


def to_json(plan: Plan) -> dict:
    """Junctions and the rectangles cars must not be parked in."""
    rows = []
    number = 0
    for cluster in plan.clusters:
        if cluster.skipped or cluster.kind not in _EMIT or cluster.box is None:
            continue
        x0, y0, x1, y1 = cluster.box
        rows.append({
            "id": number,
            "type": cluster.kind,
            "centre": [round(cluster.centre[0], 1), round(cluster.centre[1], 1)]
            if cluster.centre else None,
            "box": [x0, y0, x1, y1],
            "arms": [
                {"cat": arm.cat, "control": arm.control, "axis": arm.axis,
                 "name": arm.name, "inbound": arm.inbound}
                for arm in cluster.arms
            ],
            "no_parking": [[int(v) for v in rect] for rect in cluster.no_parking],
        })
        number += 1
    for crossing in plan.crossings:
        if crossing.skipped or crossing.box is None:
            continue
        x0, y0, x1, y1 = crossing.box
        rows.append({
            "id": number,
            "type": "crossing",
            "centre": [round(crossing.px[0], 1), round(crossing.px[1], 1)]
            if crossing.px else None,
            "box": [int(x0), int(y0), int(x1), int(y1)],
            "arms": [],
            "markings": crossing.style,
            "no_parking": [[int(v) for v in rect] for rect in crossing.no_parking],
        })
        number += 1
    return {"junctions": rows}
