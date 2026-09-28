"""Which 300-tile cells are worth compiling.

The Knoxify bitmap is always a rectangle of whole cells, but a drawn shape or
the padding at the edge of a selection is often nothing but dark grass and
mapped woodland. Project Zomboid fills that ground procedurally, so those cells
are left out of the WorldEd project and never compiled into lots.
"""
from __future__ import annotations

import os

import numpy as np

from generator import pz_colors as C
from .world import CELL_SIZE, Placement, Zone

# Open country and clip-outside shape both use dark grass on the landscape
# bitmap. Parks, farmland and yards use lighter grass and stay mapped.
_GRASS = np.array(C.DARK_GRASS, dtype=np.uint8)

_FOREST_VEG = frozenset({
    C.TREES, C.TREES_DARK_GRASS, C.SPARSE_TREES,
    C.LOT_OF_GRASS_AND_TREES, C.BUSHES_TREES_DARK_GRASS,
})


def _cell_grass_only(land: np.ndarray, x0: int, y0: int) -> bool:
    tile = land[y0:y0 + CELL_SIZE, x0:x0 + CELL_SIZE]
    if tile.size == 0:
        return True
    return bool(np.all(tile == _GRASS))


def _cell_forest_only(veg: np.ndarray, x0: int, y0: int) -> bool:
    tile = veg[y0:y0 + CELL_SIZE, x0:x0 + CELL_SIZE]
    if tile.size == 0:
        return True
    for row in np.unique(tile.reshape(-1, 3), axis=0):
        rgb = (int(row[0]), int(row[1]), int(row[2]))
        if rgb == C.VEG_NOTHING or rgb in _FOREST_VEG:
            continue
        return False
    return True


def _cell_procedural(land: np.ndarray, veg: np.ndarray,
                       x0: int, y0: int) -> bool:
    """True when the game should generate this cell itself."""
    return _cell_grass_only(land, x0, y0) and _cell_forest_only(veg, x0, y0)


def cells_to_build(out_dir: str, map_name: str, info: dict,
                   placements: list[Placement],
                   zones: list[Zone] | None,
                   proj) -> set[tuple[int, int]]:
    """Local (cx, cy) cells that should appear in the .pzw."""
    from PIL import Image

    cells_x = int(info["cells_x"])
    cells_y = int(info["cells_y"])
    all_cells = {(cx, cy) for cy in range(cells_y) for cx in range(cells_x)}

    occupied: set[tuple[int, int]] = set()
    for p in placements:
        occupied.add((p.cell_x, p.cell_y))
    for z in zones or ():
        occupied.add((z.cell_x, z.cell_y))

    land_path = os.path.join(out_dir, f"{map_name}.bmp")
    veg_path = os.path.join(out_dir, f"{map_name}_veg.bmp")
    if not os.path.isfile(land_path) or not os.path.isfile(veg_path):
        return all_cells

    land = np.asarray(Image.open(land_path).convert("RGB"))
    veg = np.asarray(Image.open(veg_path).convert("RGB"))
    height, width = land.shape[:2]

    keep: set[tuple[int, int]] = set()
    for cy in range(cells_y):
        for cx in range(cells_x):
            if (cx, cy) in occupied:
                keep.add((cx, cy))
                continue
            x0, y0 = cx * CELL_SIZE, cy * CELL_SIZE
            if x0 >= width or y0 >= height:
                continue
            if not _cell_procedural(land, veg, x0, y0):
                keep.add((cx, cy))

    return keep
