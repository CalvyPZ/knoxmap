"""Split one selection into mods that sit against each other.

Each mod is 25 cells by 25 cells. A cell is 300 tiles, so the seam between
two mods is a cell edge: they never write the same cell, and enabling all of
them continues the streets across the join.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

from generator.pz_colors import CELL_SIZE

# Cells on a side of one mod. 25 cells is 7500 tiles, and the next mod starts
# on the next cell, so the pieces meet and do not share a cell.
MOD_CELLS = 25


def mod_side_tiles() -> int:
    """Tiles on a side of one mod."""
    return MOD_CELLS * CELL_SIZE


@dataclass
class ModTile:
    row: int
    col: int
    x0: int
    y0: int
    tiles_w: int
    tiles_h: int

    @property
    def cells_x(self) -> int:
        return self.tiles_w // CELL_SIZE

    @property
    def cells_y(self) -> int:
        return self.tiles_h // CELL_SIZE


def plan(width: int, height: int) -> list[ModTile]:
    """Tile a cell-aligned map into mods, left to right, top to bottom."""
    side = mod_side_tiles()
    cols = max(1, math.ceil(width / side))
    rows = max(1, math.ceil(height / side))
    mods = []
    for row in range(rows):
        for col in range(cols):
            x0 = col * side
            y0 = row * side
            mods.append(ModTile(
                row=row, col=col, x0=x0, y0=y0,
                tiles_w=min(side, width - x0),
                tiles_h=min(side, height - y0),
            ))
    return mods


def mod_name(parent: str, row: int, col: int) -> str:
    return f"{parent}__r{row}_c{col}"


def write_pack(path: str, parent: str, origin: tuple[int, int],
               cells_x: int, cells_y: int, mods: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "parent": parent,
            "origin": [int(origin[0]), int(origin[1])],
            "cells_x": int(cells_x),
            "cells_y": int(cells_y),
            "mod_cells": MOD_CELLS,
            "mod_side_tiles": mod_side_tiles(),
            "mods": mods,
        }, fh, indent=2)


def read_pack(directory: str) -> dict | None:
    path = os.path.join(directory, "pack.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pack_box(directory: str) -> tuple[int, int, int, int] | None:
    """(origin x, origin y, cells across, cells down) reserved by a pack."""
    data = read_pack(directory)
    if not data:
        return None
    origin = data.get("origin")
    if not (isinstance(origin, list) and len(origin) == 2):
        return None
    try:
        return (int(origin[0]), int(origin[1]),
                int(data["cells_x"]), int(data["cells_y"]))
    except (KeyError, TypeError, ValueError):
        return None
