"""One military rifle, somewhere, whatever the map turned out to be.

Knox County is a county under martial law: there are checkpoints on the roads,
an army surplus store, a gun shop in most towns and a police armoury, and the
game's loot tables lean on all of them. A real town does not have any of that.
Generate Reading, or Antalya, or a Dutch suburb, and the map may contain no
army building at all and no gun shop, so `Base.AssaultRifle` - the M16, which
spawns from army and police loot and almost nowhere else - has no container on
the whole map it could ever appear in. Players read that as the map being
broken rather than as the town being peaceful.

So one is placed by hand. Where it goes follows what the map actually has:

    an army building, if the map has one - a barracks, an armoury, a bunker
    the police station, whose gun locker is where the town's rifles were
    the gun shop, if somebody mapped one
    a house out on the edge of town, as the survivor who had it

The choice is written to <map>_guncache.json and shipped as a Lua file with
the mod (knoxbuild/lua/guncache.lua), which puts the rifle into the first
container the game fills inside that building and then never does it again.
It has to be done at fill time rather than baked into the .tbx, because a
.tbx has walls, floors and furniture in it and no items: what is inside a
container is rolled by the game the first time anybody walks in.
"""
from __future__ import annotations

import json
import math
import os

# The M16 and enough to fire it. Checked against the game's own scripts:
# AssaultRifle takes MagazineType Base.556Clip and AmmoType base:bullets_556
# (media/scripts/generated/items/weapon.txt).
ITEMS = ["Base.AssaultRifle", "Base.556Clip", "Base.556Clip", "Base.556Box"]

# Where to look, best first. A kind from the placements CSV; "gunshop" is not
# a kind but a use, and is matched separately.
PRIORITY = ("military", "police", "gunshop", "house")

# A survivor's house is on the edge of town, not in the middle of the high
# street: of the houses big enough to be worth the walk, the one furthest out.
# This is the share of houses, by size, considered at all.
SURVIVOR_TOP_SHARE = 0.25
SURVIVOR_MIN_HOUSES = 8


def _centre(rows: list[dict]) -> tuple[float, float]:
    if not rows:
        return 0.0, 0.0
    xs = sum(r["tile_x"] + r["width"] / 2 for r in rows) / len(rows)
    ys = sum(r["tile_y"] + r["height"] / 2 for r in rows) / len(rows)
    return xs, ys


def _biggest(candidates: list[dict]) -> dict:
    """The largest, and the same one every time two are the same size."""
    return max(candidates, key=lambda r: (r["width"] * r["height"], r["file"]))


def _survivor(rows: list[dict]) -> dict | None:
    """A house on the edge of town, big enough to be somebody's home."""
    houses = [r for r in rows if r["kind"] in ("house", "apartment")]
    if not houses:
        return None
    houses.sort(key=lambda r: (-(r["width"] * r["height"]), r["file"]))
    top = houses[:max(SURVIVOR_MIN_HOUSES,
                      int(len(houses) * SURVIVOR_TOP_SHARE))]
    cx, cy = _centre(rows)
    return max(top, key=lambda r: (
        math.dist((r["tile_x"] + r["width"] / 2, r["tile_y"] + r["height"] / 2),
                  (cx, cy)), r["file"]))


def choose(rows: list[dict], gunshops: set[str]) -> dict | None:
    """The building to put the rifle in, or None when there is nowhere at all.

    `rows` is the placements the build just made; `gunshops` the file names of
    the buildings OSM said sell weapons.
    """
    if not rows:
        return None
    for want in PRIORITY:
        if want == "gunshop":
            here = [r for r in rows if r["file"] in gunshops]
        elif want == "house":
            pick = _survivor(rows)
            here = [pick] if pick else []
        else:
            here = [r for r in rows if r["kind"] == want]
        if here:
            return dict(_biggest(here) if want != "house" else here[0],
                        _found_as=want)
    # No army base, no station, no gun shop and no house: a map of nothing but
    # warehouses still gets one, in the biggest thing standing.
    return dict(_biggest(rows), _found_as="any")


def write(out_dir: str, map_name: str, pick: dict | None,
          world_origin: tuple[int, int], cell_size: int) -> dict | None:
    """Record the choice in world coordinates, for the mod builder to ship.

    The tile coordinates in `rows` are this map's own; the game knows the map
    by where it was placed in the world (knoxbuild/world.py), so they are
    moved by the origin here, once, rather than everywhere that reads them.
    """
    path = os.path.join(out_dir, f"{map_name}_guncache.json")
    if pick is None:
        try:
            os.remove(path)      # a rebuild that found nowhere leaves nothing
        except OSError:
            pass
        return None
    ox, oy = world_origin[0] * cell_size, world_origin[1] * cell_size
    out = {
        "map": map_name,
        "kind": pick.get("_found_as", pick.get("kind")),
        "building": pick["file"],
        "name": pick.get("name") or "",
        "x": int(ox + pick["tile_x"]),
        "y": int(oy + pick["tile_y"]),
        "w": int(pick["width"]),
        "h": int(pick["height"]),
        "items": ITEMS,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out
