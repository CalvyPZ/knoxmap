"""Reading and writing the ground and vegetation bitmaps of a map together.

The paths, pumps and props passes each opened both pictures one after the
other on the main thread: tens of seconds a piece on a big map, with every
other core idle. Pillow lets go of the interpreter while it decodes and
encodes, so the two files go on two threads.

Memory is kept to what one picture took before. A picture that is already RGB
is not converted (convert makes a second whole copy), so two pictures loading
together peak at about what one used to.
"""
from __future__ import annotations

import os
import struct
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

# Bytes of `{map}_ground_class.bin`, written beside the ground bitmap by
# Helpers/terrain/src/classify.rs. One byte a tile, top down.
GROUND_OTHER = 0
GROUND_CONCRETE = 1       # pale concrete pavement
GROUND_CARRIAGEWAY = 2    # smooth and worn asphalt, and both pothole shades
GROUND_GRASS = 3          # medium and light grass
GROUND_WATER = 4
GROUND_DARK_GRASS = 5     # dark grass alone; mapped cells, and crossable
GROUND_PARKING = 6        # dark asphalt of a car park
GROUND_PLAZA = 7          # sandstone slabs
GROUND_GRAVEL = 8         # light asphalt, yards and track beds
GROUND_DIRT = 9

_CLASS_MAGIC = b"KNOXCLS1"


def load_rgb(path: str) -> np.ndarray:
    """One bitmap as a writable (H, W, 3) uint8 array."""
    with Image.open(path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        return np.array(img)


def load_pair(ground_path: str, veg_path: str | None) -> tuple[np.ndarray, np.ndarray | None]:
    """(ground, vegetation). Vegetation is None when its file is not there."""
    if not veg_path or not os.path.exists(veg_path):
        return load_rgb(ground_path), None
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="knox-bmp") as pool:
        veg = pool.submit(load_rgb, veg_path)
        ground = load_rgb(ground_path)
        return ground, veg.result()


def _save(array: np.ndarray, path: str) -> None:
    Image.fromarray(array).save(path, format="BMP")


def save_pair(ground: np.ndarray | None, ground_path: str,
              veg: np.ndarray | None, veg_path: str) -> None:
    """Write whichever of the two pictures is given."""
    jobs = [(a, p) for a, p in ((ground, ground_path), (veg, veg_path))
            if a is not None]
    if len(jobs) <= 1:
        for array, path in jobs:
            _save(array, path)
        return
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="knox-bmp") as pool:
        futures = [pool.submit(_save, array, path) for array, path in jobs]
        for future in futures:
            future.result()


def ground_class_path(bmp_path: str) -> str:
    """`{map}_ground_class.bin` beside `{map}.bmp` or `{map}_ground_base.bmp`."""
    folder, name = os.path.split(bmp_path)
    if name.endswith("_ground_base.bmp"):
        stem = name[: -len("_ground_base.bmp")]
    elif name.endswith(".bmp"):
        stem = name[:-4]
    else:
        stem, _ext = os.path.splitext(name)
    return os.path.join(folder, stem + "_ground_class.bin")


def load_ground_class(bmp_path: str, width: int | None = None,
                      height: int | None = None) -> np.ndarray | None:
    """The class plane for a ground bitmap, or None when it cannot be used.

    A missing file, a bad magic, or a payload that is not width times height
    bytes all come back None, and the caller scans colours the way it used to.
    When `width` and `height` are given the plane has to cover that rectangle;
    a larger file is cropped to it, the same as the bitmap readers.
    """
    path = ground_class_path(bmp_path)
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size < 16:
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
            if len(head) != 16 or head[:8] != _CLASS_MAGIC:
                return None
            wide, high = struct.unpack_from("<II", head, 8)
            if wide == 0 or high == 0:
                return None
            need = 16 + wide * high
            if size != need:
                return None
            if width is not None and wide < width:
                return None
            if height is not None and high < height:
                return None
            plane = np.fromfile(handle, dtype=np.uint8, count=wide * high)
    except OSError:
        return None
    if plane.size != wide * high:
        return None
    plane = plane.reshape(high, wide)
    y1 = high if height is None else height
    x1 = wide if width is None else width
    if y1 != high or x1 != wide:
        plane = plane[:y1, :x1].copy()
    return plane


def class_mask(plane: np.ndarray, classes) -> np.ndarray:
    """True where a class plane holds one of `classes`."""
    wanted = np.asarray(tuple(classes), dtype=np.uint8)
    if wanted.size == 0:
        return np.zeros(plane.shape, dtype=bool)
    return np.isin(plane, wanted)
