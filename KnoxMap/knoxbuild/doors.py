"""Where a building's outside doors are, read from its .tbx text.

This is its own small module so a layout worker can run it on the text it has
just written, without loading the paint code (and the image library) that
wants the answer. The paths and yards pass used to read every house back from
disk to find its front door, one file after another on the main process.
"""
from __future__ import annotations

import re

_ROOMS = re.compile(r"<rooms>(.*?)</rooms>", re.S)
_DOOR = re.compile(r'type="door"[^>]*? x="(\d+)" y="(\d+)" dir="([NW])"')


def outside_doors_from_text(text: str) -> list[tuple[int, int, int, int]]:
    """(outside x, y, inside x, y) of each ground-floor outside door, front first."""
    first = text.split("<floor>", 2)
    if len(first) < 2:
        return []
    floor = first[1]
    grid_text = _ROOMS.search(floor)
    if not grid_text:
        return []
    grid = [[int(v) for v in row.strip().strip(",").split(",")]
            for row in grid_text.group(1).strip().splitlines() if row.strip()]
    if not grid:
        return []
    h, w = len(grid), len(grid[0])

    def inside(x, y):
        return 0 <= x < w and 0 <= y < h and grid[y][x] != 0

    found = []
    for m in _DOOR.finditer(floor):
        x, y, d = int(m.group(1)), int(m.group(2)), m.group(3)
        a, b = ((x, y - 1), (x, y)) if d == "N" else ((x - 1, y), (x, y))
        if inside(*a) != inside(*b):
            out, into = (a, b) if inside(*b) else (b, a)
            found.append((out[0], out[1], into[0], into[1]))
    return found
