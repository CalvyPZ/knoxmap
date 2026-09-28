"""Turn a footprint rectangle into a furnished floor plan.

Wall model, which the rest of the package depends on: BuildingEd stores walls on
the north and west *edges* of tiles, which is why its per-floor tile grids are
(width+1) x (height+1). So for a building w tiles wide and h tall:

    north exterior wall   y = 0,  dir N,  x in 0..w-1
    west  exterior wall   x = 0,  dir W,  y in 0..h-1
    south exterior wall   y = h,  dir N
    east  exterior wall   x = w,  dir W

Interior walls appear automatically wherever two adjacent tiles hold different
room indices, so we only ever paint rooms - we never emit wall objects.
"""
from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field

import knoxstop
import numpy as np

from . import catalog as C
from . import grids
from .uses import FRONT_ROOMS
from .settings import Settings

MIN_ROOM = 3          # smallest room dimension, in tiles
MIN_SPLIT = MIN_ROOM * 2 + 1
# Keep cutting while a region is bigger than roughly one generous room. This,
# rather than a fixed recursion depth, is what stops a 26x27 shop ending up as
# one cavernous 21x23 space.
TARGET_ROOM_AREA = 56
# Only a safety net against runaway recursion; TARGET_ROOM_AREA is what should
# decide when to stop. At 5 the cap bound first and left 18x18 living rooms in
# large buildings, and at 8 a warehouse 200 tiles across still did. There is
# no cost to a generous cap: the target area is what decides.
MAX_DEPTH = 16


@dataclass
class Room:
    x0: int
    y0: int
    x1: int   # inclusive
    y1: int   # inclusive
    kind: str = "hall"
    # Which dwelling this room belongs to. 0 is shared circulation - the
    # landing and corridor everyone uses. A flat's rooms open onto each other
    # and onto the corridor once, never into the flat next door.
    unit: int = 0
    # The stair shaft or corridor. Kept as a flag rather than recognised by its
    # rectangle, because a sliver folded into it changes the rectangle.
    is_core: bool = False
    # An elevator shaft: a sealed box with the lift doors set into one wall.
    # No doorway, no furniture, no windows, never merged into a neighbour.
    is_shaft: bool = False

    @property
    def w(self) -> int:
        return self.x1 - self.x0 + 1

    @property
    def h(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def area(self) -> int:
        return self.w * self.h


@dataclass
class Plan:
    width: int
    height: int
    rooms: list[Room] = field(default_factory=list)
    # 1-based room index, shape (height, width). Index as grid[y, x].
    grid: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.int32))
    doors: list[tuple[int, int, str]] = field(default_factory=list)
    windows: list[tuple[int, int, str]] = field(default_factory=list)
    furniture: list[tuple[str, int, int, str]] = field(default_factory=list)
    # None = the building fills its rectangle; otherwise True where
    # the real footprint lies. Tiles outside it stay room 0, which is
    # how BuildingEd knows they are not part of the building.
    mask: np.ndarray | None = None
    # The stair shaft, as (x0, y0, x1, y1) inclusive, identical on every
    # storey of a building. Painted last so it is always exactly one room.
    core: tuple[int, int, int, int] | None = None
    # True when the core is a corridor flats open onto, not just a stair shaft.
    corridor: bool = False
    # The elevator shaft (x0, y0, x1, y1) inclusive, and where its doors hang:
    # (x, y, "W" or "N") for the two-square wall edge facing the stair hall.
    shaft: tuple[int, int, int, int] | None = None
    shaft_door: tuple[int, int, str] | None = None
    # Wall edges carrying a switch, painting or mirror; windows keep off them.
    wall_pieces: set = field(default_factory=set)
    # Ground-floor windows that are a shop front, glazed with big panels.
    shop_front: set = field(default_factory=set)
    # What the building is (build_plan's kind), for rooms furnished by it: an
    # office in a house is a study, in an office block it is desks.
    kind: str | None = None
    # Outside wall edges this storey shares with the building next door:
    # no window, shop front or door goes in them.
    party: set = field(default_factory=set)
    # Exterior runs and the shared-wall matrix. Both describe `grid`, so any
    # paint drops them rather than letting a later door see the old walls.
    _runs: list | None = field(default=None, repr=False)
    _adj: np.ndarray | None = field(default=None, repr=False)
    # One cell per tile for furniture clearance. Bits are B_DOOR, B_STAIR,
    # B_ITEM and B_EXTRA; 0 is free. Replaces set membership on the hot path.
    occ: np.ndarray | None = field(default=None, repr=False)
    # Room ids as Python ints, row-major. Dropped with the wall caches: a
    # numpy scalar conversion on every probe was most of _room_at.
    _ids: list | None = field(default=None, repr=False)
    # 1-d view of occ. In-place updates of that array stay visible here.
    _occ_flat: np.ndarray | None = field(default=None, repr=False)
    _occ_mv: memoryview | None = field(default=None, repr=False)


def _as_mask(mask):
    """Bool array, or None. A list of lists from an older caller still works."""
    if mask is None:
        return None
    arr = np.asarray(mask)
    if arr.dtype != np.bool_:
        arr = arr.astype(bool)
    return arr


def _invalidate(plan: Plan) -> None:
    """The wall caches are a picture of the grid; painting makes them a lie."""
    plan._runs = None
    plan._adj = None
    plan._ids = None


def _ensure_adj(plan: Plan) -> np.ndarray:
    if plan._adj is None:
        plan._adj = grids.adjacency(plan.grid, len(plan.rooms))
    return plan._adj


def _ensure_runs(plan: Plan):
    if plan._runs is None:
        plan._runs = grids.runs_by_room(plan.grid)
    return plan._runs


# kind -> (floor tile entry index, display name, furniture wishlist)
ROOM_STYLE = {
    "livingroom": (C.FLOOR_CARPET_RED, "Living Room",
                   ["sofa", "armchair", "tv", "sidetable", "bookshelf", "lamp",
                    "painting", "plant", "armchair", "shelf", "lamp", "shag_rug"]),
    "kitchen": (C.FLOOR_TILE_CHECK, "Kitchen",
                ["fridge", "stove", "kitchen_sink", "counter", "counter", "counter",
                 "washer", "shelf", "plant"]),
    "bedroom": (C.FLOOR_CARPET_BLUE, "Bedroom",
                ["double_bed", "wardrobe", "dresser_alt", "sidetable", "lamp",
                 "painting", "mirror", "dresser", "bookshelf", "plant"]),
    "bathroom": (C.FLOOR_TILE_PALE, "Bathroom",
                 ["toilet", "bath", "sink", "mirror", "bath_mat", "shelf", "shower"]),
    "dining": (C.FLOOR_WOOD, "Dining Room",
               ["dresser", "painting", "plant", "shelf", "lamp", "bookshelf"]),
    "hall": (C.FLOOR_WOOD, "Hall",
             ["sidetable", "painting", "plant", "mirror", "lamp", "shelf",
              "bookshelf", "painting"]),
    "storage": (C.FLOOR_LINO, "Storage",
                ["shelf", "crate", "shelf", "crate", "bookshelf"]),
    # Vanilla room names, with the game's own loot for them.
    "kidsbedroom": (C.FLOOR_CARPET_BLUE, "Kids Bedroom",
                    ["bed", "dresser", "bookshelf", "lamp", "shelf", "plant",
                     "painting", "sidetable"]),
    "closet": (C.FLOOR_WOOD, "Closet", ["wardrobe", "shelf", "crate", "wardrobe2"]),
    "laundry": (C.FLOOR_TILE_PALE, "Laundry", ["washer", "shelf", "crate", "sink"]),
    "generalstore": (C.FLOOR_TILE_CHECK, "General Store",
                     ["shop_shelf", "shop_shelf_wood", "shop_counter", "shop_fridge_double"]),
    "conveniencestore": (C.FLOOR_TILE_PALE, "Convenience Store",
                         ["shop_shelf_red", "shop_fridge", "shop_counter_red", "vending"]),
    "clothingstore": (C.FLOOR_WOOD, "Clothing Store",
                      ["clothes_rack", "shop_shelf_wood", "mirror", "shop_counter", "mannequin"]),
    "cafe": (C.FLOOR_WOOD, "Cafe",
             ["counter", "fridge", "stove", "chair", "chair", "plant", "painting"]),
    # Shops fitted out by interiors.furnish_store; the wishlists are for a
    # room too small for rows of shelving.
    "grocery": (C.FLOOR_TILE_CHECK, "Grocery", ["shop_shelf", "shop_fridge_double", "shop_counter"]),
    "liquorstore": (C.FLOOR_TILE_PALE, "Liquor Store", ["shop_shelf", "shop_fridge", "shop_counter"]),
    "pharmacy": (C.FLOOR_TILE_PALE, "Pharmacy", ["shop_shelf_white", "shop_counter", "shop_case"]),
    "bookstore": (C.FLOOR_WOOD, "Bookstore", ["bookshelf", "bookshelf", "shop_counter"]),
    "toolstore": (C.FLOOR_LINO, "Tool Store", ["metal_rack", "shop_shelf", "shop_counter"]),
    "grocerystorage": (C.FLOOR_LINO, "Grocery Storage", ["metal_rack", "crate", "crate", "shelf"]),
    # What OpenStreetMap says a ground floor is (knoxbuild/uses.py), by the
    # game's own room names so the loot fits: eating places and their
    # kitchens, and the other shops and services of a high street.
    "restaurantdining": (C.FLOOR_TILE_CHECK, "Restaurant",
                         ["plant", "painting", "plant"]),
    "italianrestaurant": (C.FLOOR_WOOD, "Italian Restaurant",
                          ["plant", "painting", "bookshelf", "plant"]),
    "chineserestaurant": (C.FLOOR_CARPET_RED, "Chinese Restaurant",
                          ["plant", "painting", "plant"]),
    "icecream": (C.FLOOR_TILE_PALE, "Ice Cream Parlour",
                 ["shop_freezer", "shop_counter", "shop_freezer", "plant"]),
    **{k: (C.FLOOR_TILE_PALE, label,
           ["stove", "stove_alt", "fridge", "kitchen_sink", "shop_fridge_double", "metal_rack",
            "crate", "stove"])
       for k, label in (("restaurantkitchen", "Kitchen"), ("pizzakitchen", "Pizza Kitchen"),
                        ("burgerkitchen", "Burger Kitchen"), ("dinerkitchen", "Diner Kitchen"),
                        ("chinesekitchen", "Chinese Kitchen"), ("sushikitchen", "Sushi Kitchen"),
                        ("mexicankitchen", "Mexican Kitchen"), ("seafoodkitchen", "Seafood Kitchen"),
                        ("cafekitchen", "Cafe Kitchen"), ("bakerykitchen", "Bakery Kitchen"),
                        ("icecreamkitchen", "Ice Cream Kitchen"))},
    "barstorage": (C.FLOOR_LINO, "Bar Storage", ["crate", "crate", "metal_rack", "shop_fridge"]),
    "bank": (C.FLOOR_TILE_CHECK, "Bank",
             ["shop_counter", "shop_counter", "desk", "office_chair", "filing_cabinet", "plant",
              "painting", "water_cooler"]),
    "post": (C.FLOOR_LINO, "Post Office",
             ["shop_counter", "shop_counter", "shop_shelf", "crate", "crate", "corkboard"]),
    "aesthetic": (C.FLOOR_TILE_CHECK, "Salon",
                  ["chair", "mirror", "sink", "chair", "mirror", "sink", "armchair", "shelf",
                   "plant"]),
    "dentist": (C.FLOOR_TILE_PALE, "Dentist",
                ["bed", "sink", "counter", "shelf", "chair", "filing_cabinet"]),
    "medicaloffice": (C.FLOOR_TILE_PALE, "Medical Office",
                      ["bed", "sink", "counter", "desk", "office_chair", "filing_cabinet"]),
    # Seats come in rows (CENTRE_GROUPS); along the walls only a plant.
    "theatre": (C.FLOOR_CARPET_RED, "Theatre", ["plant", "painting"]),
    "policeoffice": (C.FLOOR_LINO, "Police Office",
                     ["desk", "office_chair", "filing_cabinet", "corkboard", "water_cooler"]),
    # The rest of a police station. Every name is one of the game's own, so
    # the uniforms, the guns and the evidence lockers spawn where they should.
    "policehall": (C.FLOOR_LINO, "Police Hall",
                   ["shop_counter", "corkboard", "chair", "plant", "painting"]),
    "policelocker": (C.FLOOR_LINO, "Police Lockers",
                     ["wardrobe", "wardrobe", "shelf", "chair", "wardrobe_pale"]),
    "policeoutfitstorage": (C.FLOOR_LINO, "Police Outfit Storage",
                            ["wardrobe", "metal_rack", "shelf", "crate"]),
    "policestorage": (C.FLOOR_LINO, "Police Storage",
                      ["metal_rack", "shelf", "crate", "filing_cabinet"]),
    "policegunstorage": (C.FLOOR_LINO, "Police Gun Storage",
                         ["metal_rack", "wardrobe", "crate", "shelf"]),
    "policearchive": (C.FLOOR_LINO, "Police Archive",
                      ["filing_cabinet", "filing_cabinet", "shelf", "desk", "office_chair"]),
    "interrogationroom": (C.FLOOR_LINO, "Interrogation Room",
                          ["table", "chair", "chair", "mirror"]),
    "cells": (C.FLOOR_LINO, "Cells", ["bed", "toilet", "sink"]),
    # A fire station: the appliance bay, the gear and the crew's quarters.
    "firegarage": (C.FLOOR_LINO, "Fire Garage",
                   ["metal_rack", "crate", "counter", "shelf"]),
    "firestorage": (C.FLOOR_LINO, "Fire Storage",
                    ["wardrobe", "metal_rack", "shelf", "crate"]),
    "daycare": (C.FLOOR_CARPET_BLUE, "Daycare",
                ["table", "chair", "chair", "bookshelf", "shag_rug", "plant"]),
    "mechanic": (C.FLOOR_LINO, "Mechanic", ["metal_rack", "crate", "counter", "metal_rack"]),
    "motelroom": (C.FLOOR_CARPET_BLUE, "Hotel Room",
                  ["double_bed", "sidetable", "tv", "armchair", "dresser", "lamp", "painting"]),
    "bakery": (C.FLOOR_TILE_PALE, "Bakery", ["shop_display", "shop_counter", "shop_shelf"]),
    **{k: (floor, label, ["shop_shelf", "shop_counter", "shop_shelf_wood"])
       for k, floor, label in (
           ("gasstore", C.FLOOR_TILE_PALE, "Gas Station Store"),
           ("giftstore", C.FLOOR_WOOD, "Gift Store"), ("toystore", C.FLOOR_CARPET_BLUE, "Toy Store"),
           ("candystore", C.FLOOR_TILE_CHECK, "Candy Store"), ("butcher", C.FLOOR_TILE_PALE, "Butcher"),
           ("departmentstore", C.FLOOR_TILE_PALE, "Department Store"),
           ("jewelrystore", C.FLOOR_CARPET_RED, "Jewelry Store"),
           ("camerastore", C.FLOOR_TILE_PALE, "Electronics Store"),
           ("musicstore", C.FLOOR_WOOD, "Music Store"), ("movierental", C.FLOOR_CARPET_BLUE, "Video Store"),
           ("gunstore", C.FLOOR_LINO, "Gun Store"), ("sportstore", C.FLOOR_WOOD, "Sports Store"),
           ("gardenstore", C.FLOOR_LINO, "Garden Store"),
           ("furniturestore", C.FLOOR_CARPET_RED, "Furniture Store"))},
    "armystorage": (C.FLOOR_LINO, "Army Storage",
                    ["metal_rack", "crate", "metal_rack", "crate", "filing_cabinet", "shelf"]),
    "breakroom": (C.FLOOR_LINO, "Break Room",
                  ["fridge", "counter", "counter", "sink", "vending", "water_cooler",
                   "plant"]),
    "office": (C.FLOOR_WOOD, "Office",
               ["table", "chair", "bookshelf", "painting", "plant"]),
    # Rooms only special buildings use. Every name is in RoomNames.txt, so loot
    # tables recognise them.
    "classroom": (C.FLOOR_TILE_PALE, "Classroom",
                  ["table", "chair", "chair", "bookshelf", "painting", "shelf"]),
    "library": (C.FLOOR_WOOD, "Library",
                ["bookshelf", "bookshelf", "table", "chair"]),
    "gym": (C.FLOOR_WOOD, "Gym", ["shelf", "crate", "painting"]),
    "lobby": (C.FLOOR_TILE_CHECK, "Lobby",
              ["sofa", "armchair", "sidetable", "plant", "painting"]),
    "church": (C.FLOOR_WOOD, "Church",
               ["chair", "chair", "table", "painting", "plant"]),
    "restaurant": (C.FLOOR_TILE_CHECK, "Restaurant",
                   ["table", "chair", "chair", "counter", "plant"]),
    "bar": (C.FLOOR_WOOD, "Bar", ["shelf", "painting", "plant"]),
    "clinic": (C.FLOOR_TILE_PALE, "Clinic",
               ["bed", "sink", "shelf", "counter", "chair"]),
    "medical": (C.FLOOR_TILE_PALE, "Medical",
                ["counter", "shelf", "sink", "bed", "chair"]),
    "warehouse": (C.FLOOR_LINO, "Warehouse",
                  ["crate", "crate", "shelf", "shelf", "crate"]),
    "garage": (C.FLOOR_LINO, "Garage", ["crate", "shelf", "counter"]),
    # Nothing goes in a lift car; the door is hung separately (see _furnish).
    "elevator": (C.FLOOR_LINO, "Elevator", []),
    # The game's own shed room: its loot is carpentry, farming and metalwork
    # tools, where "garage" would stock a garden shed with car parts.
    "shed": (C.FLOOR_WOOD, "Shed", ["counter", "shelf", "crate"]),
}

# Room mixes per building flavour. Order matters: the biggest room gets the
# first kind and so on down. Every name here appears in the game's own
# Distributions.lua, so loot actually spawns in them.
RESIDENTIAL = ["livingroom", "kitchen", "bedroom", "bedroom", "dining",
               "bathroom", "hall"]
COMMERCIAL = ["storage", "office", "storage", "kitchen", "bathroom", "hall"]

# Once the mix above is spent, big buildings cycle through these instead of
# repeating the last entry - otherwise a 26x27 shop comes out as nine halls,
# and halls carry almost no loot.
RESIDENTIAL_FILL = ["bedroom", "storage", "bedroom", "livingroom",
                    "bathroom", "office"]
COMMERCIAL_FILL = ["storage", "office", "storage", "bathroom"]

# Room plans for buildings OSM identifies as something specific. A school full
# of bedrooms reads as wrong immediately; these keep the interior in character,
# and the room names drive what loot spawns there.
SPECIAL_MIXES = {
    "school":     (["classroom", "classroom", "library", "office", "gym",
                    "lobby", "bathroom", "storage", "kitchen"],
                   ["classroom", "office", "storage", "bathroom"]),
    "church":     (["church", "lobby", "office", "storage", "bathroom"],
                   ["church", "storage"]),
    "restaurant": (["restaurant", "kitchen", "bar", "storage", "bathroom",
                    "office"],
                   ["restaurant", "storage"]),
    "shop":       (["storage", "office", "storage", "bathroom", "lobby"],
                   ["storage", "office"]),
    # The shops under a block of flats: the game's own shop rooms, with a
    # stockroom behind.
    "retail":     (["generalstore", "conveniencestore", "clothingstore", "cafe",
                    "storage"],
                   ["generalstore", "cafe", "storage"]),
    "industrial": (["warehouse", "warehouse", "office", "storage", "bathroom",
                    "garage"],
                   ["warehouse", "storage"]),
    "barn":       (["warehouse", "storage", "garage"], ["warehouse", "storage"]),
    # A base's buildings: army stores (the game's army loot), offices,
    # dormitory rooms, a mess kitchen.
    "military":   (["armystorage", "office", "armystorage", "bedroom", "bathroom", "kitchen",
                    "policeoffice"],
                   ["armystorage", "bedroom", "office"]),
    "shed":       (["shed"], ["shed"]),
    "medical":    (["clinic", "medical", "lobby", "office", "bathroom",
                    "storage"],
                   ["clinic", "medical", "storage"]),
    "civic":      (["office", "lobby", "office", "storage", "bathroom",
                    "library"],
                   ["office", "storage"]),
    # A police station was "civic" - offices and a storeroom - so the one
    # thing players go to a police station for was not in it. These are the
    # game's own police rooms, which is what puts the uniforms, the evidence
    # and the guns behind the counter.
    "police":     (["policeoffice", "policehall", "policelocker",
                    "interrogationroom", "policestorage", "bathroom",
                    "policearchive", "policeoutfitstorage", "policegunstorage",
                    "cells"],
                   ["policeoffice", "policestorage", "policelocker"]),
    # A library is its reading rooms, not an office block with one in it.
    "library":    (["library", "library", "lobby", "office", "bathroom",
                    "storage", "library"],
                   ["library", "library", "office"]),
    # A fire station: the appliance bay, the gear store and the crew's rooms.
    "fire":       (["firegarage", "firestorage", "office", "bedroom",
                    "kitchen", "bathroom", "firestorage"],
                   ["firestorage", "firegarage", "office"]),
    # The floors above a shop: offices, as in any high street.
    "offices":    (["office", "office", "breakroom", "bathroom", "storage", "office"],
                   ["office", "office", "storage"]),
    # A block of flats is not one big house: it is several small dwellings off
    # a shared landing, so the mix repeats a compact bedroom/living/kitchen set
    # rather than laying out one household across the whole floor.
    "apartment":  (["hall", "livingroom", "kitchen", "bedroom", "bathroom",
                    "livingroom", "bedroom", "kitchen", "bathroom", "storage"],
                   ["bedroom", "livingroom", "kitchen", "bathroom"]),
}


def _split(x0: int, y0: int, x1: int, y1: int, rng: random.Random,
           depth: int, out: list[Room],
           target_area: int = TARGET_ROOM_AREA,
           mask: np.ndarray | None = None,
           _sat: np.ndarray | None = None) -> None:
    w, h = x1 - x0 + 1, y1 - y0 + 1
    can_v = w >= MIN_SPLIT
    can_h = h >= MIN_SPLIT
    # Measure the floor that is actually inside the building. On a footprint
    # turned 38 degrees half of every bounding rectangle is empty corner, and
    # sizing rooms by the rectangle kept splitting until a house had fifteen
    # rooms a floor, most of them offices and storerooms. The summed-area
    # table makes each region O(1); it is built once and handed down so the
    # recursion does not draw any extra random numbers.
    if mask is None:
        area = w * h
    else:
        if _sat is None:
            mask = _as_mask(mask)
            _sat = grids.sat(mask)
        area = int(_sat[y1 + 1, x1 + 1] - _sat[y0, x1 + 1]
                   - _sat[y1 + 1, x0] + _sat[y0, x0])
    if depth <= 0 or not (can_v or can_h) or area <= target_area:
        out.append(Room(x0, y0, x1, y1))
        return
    if not can_h:
        vertical = True
    elif not can_v:
        vertical = False
    else:
        vertical = w > h if w != h else rng.random() < 0.5
    # Where to cut. Anywhere, for a region already near the size of a room -
    # that is what stops every house being a grid. But a region far bigger
    # than a room has to come down towards one on both sides of the cut, and
    # a random cut kept shaving a sliver off a floor the size of a factory
    # until it ran out of depth and left a room of two thousand tiles.
    lo = (x0 if vertical else y0) + MIN_ROOM
    hi = (x1 if vertical else y1) - MIN_ROOM
    if area > target_area * 2.5:
        mid = ((x0 + x1) if vertical else (y0 + y1)) // 2
        reach = max(1, ((x1 - x0) if vertical else (y1 - y0)) // 6)
        lo, hi = max(lo, mid - reach), min(hi, mid + reach)
        if lo > hi:
            lo = hi = min(max(mid, (x0 if vertical else y0) + MIN_ROOM),
                          (x1 if vertical else y1) - MIN_ROOM)
    if vertical:
        cut = rng.randint(lo, hi)
        _split(x0, y0, cut - 1, y1, rng, depth - 1, out, target_area, mask, _sat)
        _split(cut, y0, x1, y1, rng, depth - 1, out, target_area, mask, _sat)
    else:
        cut = rng.randint(lo, hi)
        _split(x0, y0, x1, cut - 1, rng, depth - 1, out, target_area, mask, _sat)
        _split(x0, cut, x1, y1, rng, depth - 1, out, target_area, mask, _sat)


# What a flat contains, by how many rooms it got. The first entry goes to the
# room the front door opens into; the rest are handed out biggest-first, so the
# bathroom lands on the smallest room.
#
# Size matters: a two-room flat is a bedsit and wants a bathroom more than it
# wants a separate bedroom.
FLAT_PLANS = {
    # A one-room flat is a bedsit: somewhere to sleep, not a sofa and nothing.
    1: ["bedroom"],
    2: ["bedroom", "bathroom"],
    3: ["livingroom", "bedroom", "bathroom"],
    4: ["livingroom", "kitchen", "bedroom", "bathroom"],
    5: ["livingroom", "kitchen", "bedroom", "bedroom", "bathroom"],
}
FLAT_EXTRA = ["bedroom", "storage"]
# Floor area, in tiles, from which a flat is cut into at least three rooms.
FLAT_MIN_SPLIT_AREA = 30
# Tiles of corridor wall each flat gets. Narrower slices gave two- and
# three-room flats, and with the bathroom going to the smallest room, a flat
# of two big rooms had a bathroom the size of its living room.
FLAT_FRONTAGE = (8, 11)
# A hotel room is narrower than a flat: a room and its bathroom.
HOTEL_FRONTAGE = (4, 6)


def _slices(length: int, rng: random.Random,
            lo: int, hi: int) -> list[tuple[int, int]]:
    """Cut 0..length-1 into runs close to lo..hi, sized evenly.

    Taking random lengths from one end and giving the last slice whatever was
    left made the final flat on each side up to twice the size of the others -
    a 26-tile corridor came out as flats of 7 and 19. Deciding how many flats
    fit first and then sharing the length out keeps them comparable, with a
    tile of jitter so a row of flats is not perfectly regular.
    """
    count = max(1, round(length / ((lo + hi) / 2)))
    widths = [length // count] * count
    for i in range(length - sum(widths)):
        widths[i] += 1
    for i in range(count - 1):
        shift = rng.choice((-1, 0, 1))
        if widths[i] + shift >= lo and widths[i + 1] - shift >= lo:
            widths[i] += shift
            widths[i + 1] -= shift
    out, a = [], 0
    for wdt in widths:
        out.append((a, a + wdt - 1))
        a += wdt
    return out


def _corridor_axis(plan: Plan) -> str | None:
    """"y" or "x" for the direction a corridor runs, None without one."""
    if plan.core is None or not plan.corridor:
        return None
    x0, y0, x1, y1 = plan.core
    return "y" if (y1 - y0) >= (x1 - x0) else "x"


def _apartment_rooms(plan: Plan, rng: random.Random, target: int,
                     frontage: tuple[int, int] = FLAT_FRONTAGE) -> None:
    """Lay a floor out as flats either side of a corridor.

    The order is what matters. Splitting the whole floor into rooms, grouping
    those into flats and then painting a corridor over the top - the first
    attempt at this - left most flats not touching the corridor at all, and cut
    rooms in half where the corridor ran through them. 191 of 200 blocks came
    out with a room nobody could get into.

    Here the corridor exists first. Each side of it is sliced into flats that
    run from the corridor wall to the outside wall, so every flat has a front
    door onto the corridor and a window onto the street by construction, and
    only then is each flat divided into its own rooms.
    """
    axis = _corridor_axis(plan)
    if axis is None:
        # No corridor fits (an irregular footprint, usually). One flat per
        # floor is a real kind of building, so lay it out as that.
        _split(0, 0, plan.width - 1, plan.height - 1, rng, MAX_DEPTH,
               plan.rooms, target_area=target, mask=plan.mask)
        for room in plan.rooms:
            room.unit = 1
        return

    cx0, cy0, cx1, cy1 = plan.core
    unit = 0
    if axis == "y":
        sides = [(0, cx0 - 1), (cx1 + 1, plan.width - 1)]
        length = plan.height
    else:
        sides = [(0, cy0 - 1), (cy1 + 1, plan.height - 1)]
        length = plan.width
    for a0, a1 in sides:
        if a1 - a0 + 1 < MIN_ROOM:
            continue
        # Each side sliced independently, so the flats do not line up across
        # the corridor like a spreadsheet.
        for s0, s1 in _slices(length, rng, *frontage):
            unit += 1
            box = (a0, s0, a1, s1) if axis == "y" else (s0, a0, s1, a1)
            flat: list[Room] = []
            _split(*box, rng, MAX_DEPTH, flat, target_area=target, mask=plan.mask)
            # A flat that came out as one or two rooms was all living room: in
            # a small block every flat on every floor had nothing else, not a
            # bed or a bathroom in the building. Cut it into a home's rooms
            # when it is big enough for them.
            area = (box[2] - box[0] + 1) * (box[3] - box[1] + 1)
            if frontage == FLAT_FRONTAGE and len(flat) < 3 and area >= FLAT_MIN_SPLIT_AREA:
                flat = []
                _split(*box, rng, MAX_DEPTH, flat, target_area=max(MIN_SPLIT * MIN_ROOM, area // 3),
                       mask=plan.mask)
            for room in flat:
                room.unit = unit
            plan.rooms.extend(flat)

    # Beyond the ends of a corridor that stops short of the building's ends,
    # its own width of floor would otherwise belong to no room at all.
    if axis == "y":
        caps = [(cx0, 0, cx1, cy0 - 1), (cx0, cy1 + 1, cx1, plan.height - 1)]
    else:
        caps = [(0, cy0, cx0 - 1, cy1), (cx1 + 1, cy0, plan.width - 1, cy1)]
    for x0, y0, x1, y1 in caps:
        if x1 < x0 or y1 < y0:
            continue
        unit += 1
        cap: list[Room] = []
        _split(x0, y0, x1, y1, rng, MAX_DEPTH, cap, target_area=target, mask=plan.mask)
        for room in cap:
            room.unit = unit
        plan.rooms.extend(cap)


def _neighbours(plan: Plan) -> dict[int, dict[int, int]]:
    """Room index -> {neighbour index: length of shared wall in tiles}."""
    matrix = _ensure_adj(plan)
    adj: dict[int, dict[int, int]] = {i: {} for i in range(1, len(plan.rooms) + 1)}
    y, x, side, a, b = grids.wall_edges(plan.grid)
    keep = (a > 0) & (b > 0)
    if not np.any(keep):
        return adj
    y, x, side, a, b = y[keep], x[keep], side[keep], a[keep], b[keep]
    # First meeting in the old row-major scan, west before north on a tile.
    # An equal-sized neighbour is chosen by that order.
    pri = (side != "W").astype(np.int8)
    order = np.lexsort((pri, x, y))
    a, b = a[order], b[order]
    for i in range(int(a.size)):
        ia, ib = int(a[i]), int(b[i])
        if ib in adj[ia]:
            continue
        length = int(matrix[ia, ib])
        adj[ia][ib] = length
        adj[ib][ia] = length
    return adj


def _assign_flat_kinds(plan: Plan, hotel: bool = False) -> None:
    """Give every flat its own set of rooms, arranged around its front door.

    The room touching the corridor is where you walk in, so it becomes the
    living room; the rest follow by size, with the bathroom on the smallest.
    In a hotel each "flat" is guest rooms with a bathroom.
    """
    adj = _neighbours(plan)
    corridor = {i for i, r in enumerate(plan.rooms, 1) if r.unit == 0}
    units: dict[int, list[int]] = {}
    for i, room in enumerate(plan.rooms, 1):
        if room.unit == 0:
            room.kind = "hall"
        else:
            units.setdefault(room.unit, []).append(i)
    for members in units.values():
        entry = [i for i in members if any(n in corridor for n in adj[i])]
        by_size = sorted(members, key=lambda i: -plan.rooms[i - 1].area)
        first = max(entry, key=lambda i: plan.rooms[i - 1].area) \
            if entry else by_size[0]
        ordered = [first] + [i for i in by_size if i != first]
        kinds = FLAT_PLANS.get(len(ordered))
        if hotel:
            # A guest room, its bathroom, and a wardrobe closet in a big one.
            kinds = (["motelroom", "closet"][:max(1, len(ordered) - 1)]
                     + ["motelroom"] * max(0, len(ordered) - 3) + ["bathroom"])[:len(ordered)]                 if len(ordered) > 1 else ["motelroom"]
        elif kinds is None:
            extra = [FLAT_EXTRA[k % len(FLAT_EXTRA)]
                     for k in range(len(ordered) - 5)]
            kinds = FLAT_PLANS[5][:-1] + extra + FLAT_PLANS[5][-1:]
        for i, kind in zip(ordered, kinds):
            plan.rooms[i - 1].kind = kind


def _graph_distance(adj: dict[int, dict[int, int]], start: int) -> dict[int, int]:
    dist = {start: 0}
    frontier = [start]
    while frontier:
        nxt = []
        for cur in frontier:
            for n in adj[cur]:
                if n not in dist:
                    dist[n] = dist[cur] + 1
                    nxt.append(n)
        frontier = nxt
    return dist


# Knox County's houses (546 measured) have per house about 1.4 bedrooms, 0.4
# children's bedrooms, 0.5 closets and 0.2 laundries - and ours had three
# bedrooms and an office apiece, every spare room another bedroom. A room this
# small is a closet; the rest take turns down these lists.
SMALL_ROOM_TILES = 8
HOUSE_SLEEPING = ["bedroom", "kidsbedroom", "bedroom", "office", "storage"]
UPSTAIRS = ["bedroom", "kidsbedroom", "bedroom", "office", "kidsbedroom", "storage"]


def _assign_house_kinds(plan: Plan, level: int, levels: int) -> None:
    """Rooms of a house, placed by what they sit next to.

    Handing kinds out in size order put the kitchen wherever the second-biggest
    rectangle fell, bathrooms opening off living rooms, and a kitchen on every
    storey of a three-storey house. Here the ground floor holds the rooms a
    household shares - the living room, the kitchen beside it, dining beside
    that - and upper floors hold bedrooms. Bedrooms go as far from the living
    room as the plan allows; the bathroom takes the smallest room.
    """
    adj = _neighbours(plan)
    free = [i for i, r in enumerate(plan.rooms, 1) if not r.is_core]
    for i, room in enumerate(plan.rooms, 1):
        if i not in free:
            room.kind = "hall"
    if not free:
        return
    area = {i: plan.rooms[i - 1].area for i in free}
    kinds: dict[int, str] = {}

    def take(i: int, kind: str) -> None:
        kinds[i] = kind
        free.remove(i)

    if level == 0:
        living = max(free, key=lambda i: area[i])
        take(living, "livingroom")
        if free:
            near = [n for n in adj[living] if n in free] or free
            kitchen = max(near, key=lambda i: area[i])
            take(kitchen, "kitchen")
            near = [n for n in adj[kitchen] if n in free]
            if near and len(free) >= 3:
                take(max(near, key=lambda i: area[i]), "dining")
        if free and (levels == 1 or len(free) >= 2):
            take(min(free, key=lambda i: area[i]), "bathroom")
        # A small room beside the kitchen is its laundry.
        near = [n for n in adj.get(kitchens[0], ()) if n in free
                and area[n] <= SMALL_ROOM_TILES] if (kitchens := [i for i, k in kinds.items()
                                                                  if k == "kitchen"]) else []
        if near:
            take(min(near, key=lambda i: area[i]), "laundry")
        dist = _graph_distance(adj, living)
        rest = sorted(free, key=lambda i: -dist.get(i, 99))
        sleeping = HOUSE_SLEEPING if levels == 1 else ["office", "bedroom", "kidsbedroom"]
        for n, i in enumerate(rest):
            kinds[i] = "closet" if area[i] <= SMALL_ROOM_TILES else sleeping[n % len(sleeping)]
    else:
        take(min(free, key=lambda i: area[i]), "bathroom")
        for n, i in enumerate(sorted(free, key=lambda i: -area[i])):
            kinds[i] = "closet" if area[i] <= SMALL_ROOM_TILES else UPSTAIRS[n % len(UPSTAIRS)]

    for i, kind in kinds.items():
        plan.rooms[i - 1].kind = kind
SHOP_BACK_ROOMS = ["storage", "office", "storage"]
# A shop's back rooms take this share of its depth, within these many tiles.
SHOP_BACK_SHARE = 0.3
SHOP_BACK_TILES = (3, 7)
MIN_SALES_DEPTH = 8


def _shop_rooms(plan: Plan, rng: random.Random, street: str | None) -> None:
    """A shop's ground floor as one sales floor the width of the shop front,
    and a strip of back rooms behind it: stockroom, office, toilet.

    Cut up like a house, a supermarket was eight rooms of 24 tiles, and the
    one that became the shop was a corridor four tiles wide with nothing but
    a shelf along each wall.
    """
    w, h = plan.width, plan.height
    side = street if street in ("N", "S", "W", "E") else ("S" if w >= h else "E")
    total = h if side in ("N", "S") else w
    back = max(SHOP_BACK_TILES[0], min(SHOP_BACK_TILES[1], round(total * SHOP_BACK_SHARE)))
    if total - back < MIN_SALES_DEPTH:
        plan.rooms.append(Room(0, 0, w - 1, h - 1))
        return
    if side == "S":
        sales, strip = (0, back, w - 1, h - 1), (0, 0, w - 1, back - 1)
    elif side == "N":
        sales, strip = (0, 0, w - 1, h - 1 - back), (0, h - back, w - 1, h - 1)
    elif side == "E":
        sales, strip = (back, 0, w - 1, h - 1), (0, 0, back - 1, h - 1)
    else:
        sales, strip = (0, 0, w - 1 - back, h - 1), (w - back, 0, w - 1, h - 1)
    plan.rooms.append(Room(*sales))
    _split(*strip, rng, MAX_DEPTH, plan.rooms, target_area=max(16, back * 8), mask=plan.mask)
MIN_SHOP_ROOM = 16


def _assign_shop_floor(plan: Plan, rng: random.Random, street: str | None,
                       uses: list[tuple[str, str]] | None, several: bool) -> None:
    """A commercial ground floor: what is on the street in front, what serves
    it behind.

    The rooms with an outside wall on the street (or on any side, when the
    street is not known) and floor enough become the fronts, taken along the
    street. Each takes the next of the building's real uses
    (knoxbuild/uses.py) - the pizza place's dining room, the bank hall - and
    a room behind it becomes that use's back room: the pizza kitchen, the
    stockroom. A block of flats with more shop fronts than known uses fills
    the rest with shops of the usual kinds; the remaining back rooms are
    storerooms, an office and a toilet.
    """
    from . import interiors
    rooms = [(i, r) for i, r in enumerate(plan.rooms, 1) if not r.is_core and not r.is_shaft]
    if not rooms:
        return
    uses = list(uses or [])

    def street_wall(i):
        n = 0
        for side, wall in _outside_runs(plan, i):
            if street is None or side == street:
                n += len(wall)
        return n

    fronts = [ir for ir in rooms if street_wall(ir[0]) >= 3 and ir[1].area >= MIN_SHOP_ROOM]
    if not fronts:
        fronts = [max(rooms, key=lambda ir: ir[1].area)]
    if not several:
        fronts = [max(fronts, key=lambda ir: ir[1].area)]
    # Along the street, so the uses keep the order they were found in.
    fronts.sort(key=lambda ir: (ir[1].x0, ir[1].y0) if street in ("N", "S", None) else (ir[1].y0, ir[1].x0))
    adj = _neighbours(plan)
    taken = {i for i, _ in fronts}
    backs: list[tuple[int, str]] = []
    for n, (i, r) in enumerate(fronts):
        if n < len(uses):
            front, back = uses[n]
        elif uses:
            # More shop fronts than the map has businesses: the last one
            # along the street takes the room next door as well.
            front, back = uses[-1][0], "storage"
        else:
            front = interiors.store_kind(rng)
            back = "grocerystorage" if front == "grocery" else "storage"
        r.kind = front
        backs.append((i, back))
    for i, back in backs:
        # The biggest room behind this front that nobody has yet.
        near = sorted((j for j in adj.get(i, {}) if j not in taken
                       and not plan.rooms[j - 1].is_core and not plan.rooms[j - 1].is_shaft),
                      key=lambda j: -plan.rooms[j - 1].area)
        if near:
            plan.rooms[near[0] - 1].kind = back
            taken.add(near[0])
    rest = sorted((ir for ir in rooms if ir[0] not in taken), key=lambda ir: -ir[1].area)
    for n, (i, r) in enumerate(rest):
        r.kind = SHOP_BACK_ROOMS[n % len(SHOP_BACK_ROOMS)]
    if len(rest) >= 2:
        rest[-1][1].kind = "bathroom"


def _assign_kinds(rooms: list[Room], mix: list[str], fill: list[str]) -> None:
    order = sorted(rooms, key=lambda r: -r.area)
    for i, room in enumerate(order):
        room.kind = mix[i] if i < len(mix) else fill[(i - len(mix)) % len(fill)]
    # The smallest room makes a far more convincing bathroom than a hall.
    if len(order) >= 3 and "bathroom" in mix:
        order[-1].kind = "bathroom"


def _renumber(plan: Plan) -> None:
    """Drop rooms that own no tiles and close the gaps in the numbering."""
    _invalidate(plan)
    g = plan.grid
    if g.size == 0:
        return
    present = np.unique(g)
    present = present[present > 0]
    if present.size == len(plan.rooms):
        return
    used = set(int(v) for v in present.tolist())
    lut = np.zeros(int(g.max()) + 1, np.int32)
    kept = []
    for idx, room in enumerate(plan.rooms, start=1):
        if idx in used:
            kept.append(room)
            lut[idx] = len(kept)
    plan.grid = lut[g]
    plan.rooms = kept


def _refit(plan: Plan) -> None:
    """Shrink or grow each room's rectangle to the tiles it actually owns."""
    boxes = grids.bounds(plan.grid)
    for idx, room in enumerate(plan.rooms, start=1):
        if idx - 1 >= len(boxes):
            continue
        sl = boxes[idx - 1]
        if sl is None:
            continue
        ys, xs = sl
        room.y0, room.x0 = int(ys.start), int(xs.start)
        room.y1, room.x1 = int(ys.stop) - 1, int(xs.stop) - 1


def _paint(plan: Plan) -> None:
    g = np.zeros((plan.height, plan.width), np.int32)
    mask = plan.mask
    for idx, r in enumerate(plan.rooms, start=1):
        view = g[r.y0:r.y1 + 1, r.x0:r.x1 + 1]
        if mask is None:
            view[:] = idx
        else:
            view[mask[r.y0:r.y1 + 1, r.x0:r.x1 + 1]] = idx

    # The stair shaft is painted over whatever is beneath it, identically on
    # every storey. Stairs used to be dropped wherever five tiles happened to be
    # inside the building, which on 80% of them meant a flight crossing a room
    # boundary and running into an interior wall.
    if plan.core is not None:
        cx0, cy0, cx1, cy1 = plan.core
        plan.rooms.append(Room(cx0, cy0, cx1, cy1, kind="hall", unit=0,
                               is_core=True))
        g[cy0:cy1 + 1, cx0:cx1 + 1] = len(plan.rooms)

    plan.grid = g
    _invalidate(plan)
    _renumber(plan)
    _mend_fragments(plan)
    _refit(plan)


# A room piece smaller than this, or only this thick, is not a room. Along a
# diagonal wall the grid cuts the corners of every rectangle into wedges of a
# few tiles; left alone each became a room of its own - a house turned 38
# degrees came out with 18 rooms, most of them triangles you could not stand in.
SLIVER = 10
SLIVER_THICKNESS = 2


def _mend_fragments(plan: Plan) -> None:
    """Split rooms that were cut in two, and fold slivers into a neighbour.

    An irregular footprint or the stair shaft can cut one rectangle into pieces
    that no longer touch. BuildingEd treats them as one room, so the only door
    lands in one piece and the other is sealed; the game then has a room with
    no way in. Each piece becomes a room of its own. A piece too small to be
    a room joins the neighbour it shares the most wall with; the lower room
    number wins a tie so the fold does not depend on which edge was seen first.
    """
    g = plan.grid
    # Newly split pieces are not walked again: the old flood fill stopped at
    # the rooms that existed when the pass started.
    n0 = len(plan.rooms)
    for idx in range(1, n0 + 1):
        ys, xs = np.nonzero(g == idx)
        if ys.size == 0:
            continue
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        labeled, n = grids.label4(g[y0:y1, x0:x1] == idx)
        if n <= 1:
            continue
        sizes = np.bincount(labeled.ravel(), minlength=n + 1)[1:]
        # Largest piece keeps the number. An equal size keeps the earlier
        # label, which is the one label4 met first reading the box row by row.
        order = sorted(range(n), key=lambda i: (-int(sizes[i]), i))
        base = plan.rooms[idx - 1]
        view = g[y0:y1, x0:x1]
        for i in order[1:]:
            plan.rooms.append(Room(0, 0, 0, 0, kind=base.kind, unit=base.unit))
            view[labeled == i + 1] = len(plan.rooms)
    _invalidate(plan)

    def too_small(idx: int) -> bool:
        yy, xx = np.nonzero(g == idx)
        count = int(yy.size)
        if count == 0:
            return False
        if count < 4:
            return True
        nxs = int(np.unique(xx).size)
        nys = int(np.unique(yy).size)
        # A whole rectangle of 3x3 or more is a real room - a small bathroom
        # is exactly that. Only the ragged wedges a diagonal wall leaves go.
        if count == nxs * nys and min(nxs, nys) >= 3:
            return False
        return count < SLIVER or (min(nxs, nys) <= SLIVER_THICKNESS
                                  and count < 3 * SLIVER)

    for idx in range(1, len(plan.rooms) + 1):
        if plan.rooms[idx - 1].is_core or plan.rooms[idx - 1].is_shaft:
            continue
        if not too_small(idx):
            continue
        # A sliver already given away changes the wall the next one shares.
        counts = grids.adjacency(g, len(plan.rooms))[idx]
        cands = [j for j in range(1, int(counts.shape[0]))
                 if j != idx and int(counts[j]) > 0
                 and not plan.rooms[j - 1].is_shaft]
        if not cands:
            continue
        home = min(cands, key=lambda j: (-int(counts[j]), j))
        g[g == idx] = home

    _invalidate(plan)
    _renumber(plan)


def _unit_touches_corridor(plan: Plan) -> None:
    """Make every flat one connected piece with a wall on the corridor.

    A flat can be cut in two by a notch in the footprint, and a flat sliced
    where the building narrows can border only its neighbours. Either way,
    some of its rooms could only be reached by walking through somebody
    else's home. Working piece by piece: a piece that reaches the corridor
    stays a flat of its own (split off if it had become separated from the
    rest), and a piece that does not is joined to the neighbouring flat it
    shares most wall with.
    """
    corridor_units = {r.unit for r in plan.rooms if r.unit == 0}
    if not corridor_units:
        return
    for _ in range(len(plan.rooms) + 1):
        adj = _neighbours(plan)
        by_unit: dict[int, list[int]] = {}
        for i, room in enumerate(plan.rooms, 1):
            if room.unit:
                by_unit.setdefault(room.unit, []).append(i)

        def pieces(members: list[int]) -> list[set[int]]:
            left, out = set(members), []
            while left:
                seed = left.pop()
                piece, stack = {seed}, [seed]
                while stack:
                    cur = stack.pop()
                    for n in adj[cur]:
                        if n in left:
                            left.remove(n)
                            piece.add(n)
                            stack.append(n)
                out.append(piece)
            return out

        def reaches(piece: set[int]) -> bool:
            return any(plan.rooms[n - 1].unit == 0 for i in piece for n in adj[i])

        changed = False
        next_unit = max(by_unit, default=0) + 1
        for unit, members in by_unit.items():
            parts = pieces(members)
            if len(parts) == 1 and reaches(parts[0]):
                continue
            touching = [part for part in parts if reaches(part)]
            # Every separate piece that reaches the corridor becomes its own
            # flat; the first keeps the flat's number.
            for extra in touching[1:]:
                for i in extra:
                    plan.rooms[i - 1].unit = next_unit
                next_unit += 1
                changed = True
            for part in parts:
                if reaches(part):
                    continue
                border: dict[int, int] = {}
                for i in part:
                    for n, length in adj[i].items():
                        u = plan.rooms[n - 1].unit
                        if u and n not in part:
                            border[u] = border.get(u, 0) + length
                if not border:
                    continue
                into = max(border, key=lambda u: border[u])
                for i in part:
                    plan.rooms[i - 1].unit = into
                changed = True
            if changed:
                break
        if not changed:
            return


def _boundary_edges(plan: Plan) -> dict[tuple[int, int], list[tuple[int, int, str]]]:
    """(room a, room b) with a < b -> every wall edge between them."""
    y, x, side, a, b = grids.wall_edges(plan.grid)
    keep = (a > 0) & (b > 0)
    if not np.any(keep):
        return {}
    y, x, side, a, b = y[keep], x[keep], side[keep], a[keep], b[keep]
    # West before north on the same tile, then row-major: that is the order
    # the door pass used to meet each pair, and an equal-cost tie keeps the
    # first one.
    pri = (side != "W").astype(np.int8)
    order = np.lexsort((pri, x, y))
    y, x, side, a, b = y[order], x[order], side[order], a[order], b[order]
    lo = np.minimum(a, b)
    hi = np.maximum(a, b)
    out: dict[tuple[int, int], list[tuple[int, int, str]]] = {}
    for i in range(int(y.size)):
        key = (int(lo[i]), int(hi[i]))
        out.setdefault(key, []).append((int(x[i]), int(y[i]), str(side[i])))
    return out


def _door_spot(edges: list[tuple[int, int, str]],
               min_run: int) -> tuple[tuple[int, int, str], int] | None:
    """The middle of the longest straight run of wall, and that run's length."""
    if not edges:
        return None
    best = None
    # W before N, and within a direction the earlier run wins an equal length.
    # Same order as lexsort by (moving, fixed): lower fixed, then lower moving.
    for direction in ("W", "N"):
        grouped: dict[int, list[int]] = {}
        if direction == "W":
            for x, y, side in edges:
                if side == "W":
                    grouped.setdefault(x, []).append(y)
        else:
            for x, y, side in edges:
                if side == "N":
                    grouped.setdefault(y, []).append(x)
        for fixed in sorted(grouped):
            moving = grouped[fixed]
            moving.sort()
            start = 0
            count = len(moving)
            for i in range(1, count + 1):
                if i != count and moving[i] == moving[i - 1] + 1:
                    continue
                length = i - start
                if length >= min_run and (best is None or length > best[1]):
                    mid = moving[start + (length // 2)]
                    if direction == "W":
                        spot = (int(fixed), int(mid), "W")
                    else:
                        spot = (int(mid), int(fixed), "N")
                    best = (spot, int(length))
                start = i
    return best


# How much a door between two kinds of room is worth avoiding. Circulation is
# cheap to open onto; rooms a household uses together are cheap to join; a
# bathroom off a kitchen or a bedroom through another bedroom is not how
# anybody builds.
TOGETHER = [{"livingroom", "kitchen"}, {"kitchen", "dining"},
            {"livingroom", "dining"}]
# A flat's front door, by the room it opens into.
FRONT_DOOR_COST = {"livingroom": 0.0, "hall": 0.5, "kitchen": 1.5,
                   "dining": 1.5, "storage": 6.0, "bedroom": 7.0,
                   "bathroom": 12.0, "motelroom": 0.0}


def _door_cost(a: str, b: str) -> float:
    kinds = {a, b}
    if "hall" in kinds or "lobby" in kinds:
        cost = 1.0
    elif kinds in TOGETHER:
        cost = 1.5
    elif "livingroom" in kinds:
        cost = 3.0
    else:
        cost = 6.0
    if "bathroom" in kinds and not kinds & {"hall", "lobby", "bedroom"}:
        cost += 6.0
    if a == b == "bedroom":
        cost += 8.0
    return cost


def _doors(plan: Plan, rng: random.Random) -> None:
    """Connect every room, with as few doors as a real plan would have.

    The old rule put a door on every boundary between two rooms. A floor where
    every room opens into every room it touches is a maze of doorways, not a
    home. This grows a tree outward from the circulation instead - hall, then
    living room - choosing the cheapest door each time, so a bedroom gets the
    one door it needs and a bathroom opens off a hall rather than a kitchen.

    Flats are enforced here as well: rooms of the same flat may open onto each
    other, a flat opens onto the corridor once, and two different flats never
    share a door. Only if a room could otherwise not be reached at all are
    those rules relaxed, one at a time, cheapest first.
    """
    n = len(plan.rooms)
    if n <= 1:
        return
    rooms = plan.rooms
    sealed = {i for i, r in enumerate(rooms, 1) if r.is_shaft}
    edges = {k: v for k, v in _boundary_edges(plan).items()
             if k[0] not in sealed and k[1] not in sealed}
    # A boundary never changes while the connectivity tree grows. Finding its
    # longest run used to rebuild and sort three NumPy arrays every time a room
    # was added, even though every pass asked the same question.
    door_spots = {pair: _door_spot(wall, 1) for pair, wall in edges.items()}

    def start_room() -> int:
        for kind in ("hall", "lobby", "livingroom"):
            found = [i for i, r in enumerate(rooms, 1) if r.kind == kind]
            if found:
                return max(found, key=lambda i: rooms[i - 1].area)
        return max(range(1, n + 1), key=lambda i: rooms[i - 1].area)

    connected = {start_room()} | sealed
    front: set[int] = set()
    placed: set[tuple[int, int]] = set()
    doors_of: dict[int, int] = {}

    # Each pass relaxes one rule, and only for rooms still unreached.
    for relax in range(4):
        while True:
            best = None
            for (a, b), wall in edges.items():
                if (a in connected) == (b in connected):
                    continue
                ra, rb = rooms[a - 1], rooms[b - 1]
                if ra.unit != rb.unit:
                    flat = ra.unit or rb.unit
                    if ra.unit and rb.unit:
                        if relax < 3:
                            continue
                    elif flat in front and relax < 2:
                        continue
                spot = door_spots[(a, b)]
                if spot is None:
                    continue
                if ra.unit != rb.unit and not (ra.unit and rb.unit):
                    inner = rb if ra.unit == 0 else ra
                    cost = FRONT_DOOR_COST.get(inner.kind, 3.0)
                else:
                    cost = _door_cost(ra.kind, rb.kind)
                # A wall one tile long takes a door, but only if nothing
                # better exists: skipping them outright sent a bedroom through
                # the bathroom to reach a hall it shared a one-tile wall with.
                if spot[1] < 2:
                    cost += 3.0
                # A bathroom is a dead end. Once it has a door, walking through
                # it to reach another room is the last thing to try.
                for room_idx in (a, b):
                    if rooms[room_idx - 1].kind == "bathroom" and doors_of.get(room_idx):
                        cost += 12.0
                cost -= min(spot[1], 6) * 0.05
                if best is None or cost < best[0]:
                    best = (cost, a, b, spot[0])
            if best is None:
                break
            _, a, b, door = best
            plan.doors.append(door)
            placed.add((a, b))
            doors_of[a] = doors_of.get(a, 0) + 1
            doors_of[b] = doors_of.get(b, 0) + 1
            ra, rb = rooms[a - 1], rooms[b - 1]
            if ra.unit != rb.unit and not (ra.unit and rb.unit):
                front.add(ra.unit or rb.unit)
            connected.add(a)
            connected.add(b)
        if len(connected) == n:
            break

    # A few extra openings between shared rooms of the same household, so a
    # kitchen opens onto both living and dining rooms instead of being reached
    # only through one of them.
    shared = {"livingroom", "kitchen", "dining", "hall", "lobby"}
    for (a, b), wall in edges.items():
        if (a, b) in placed:
            continue
        ra, rb = rooms[a - 1], rooms[b - 1]
        if ra.unit != rb.unit or ra.kind not in shared or rb.kind not in shared:
            continue
        if ra.unit and (ra.kind == "hall" or rb.kind == "hall"):
            continue
        spot = door_spots[(a, b)]
        if spot and spot[1] >= 3 and rng.random() < 0.6:
            plan.doors.append(spot[0])


def _bind_ids(plan: Plan) -> list:
    ids = plan.grid.ravel().tolist()
    plan._ids = ids
    return ids


def _room_at(plan: Plan, x: int, y: int) -> int:
    w = plan.width
    if 0 <= x < w and 0 <= y < plan.height:
        ids = plan._ids
        if ids is None:
            ids = _bind_ids(plan)
        return ids[y * w + x]
    return 0


def _side_edge(side: str, fixed: int, pos: int) -> tuple[int, int, str]:
    """The wall edge at `pos` along a side line, as _outside_runs keys them."""
    if side in ("N", "S"):
        return (pos, fixed, "N")
    return (fixed, pos, "W")


def _runs_from_positions(side: str, line: int, positions: list[int]):
    """Split one face line into the stretches a party wall did not punch out."""
    runs = []
    run: list[int] = []
    for p in positions + [None]:
        if run and (p is None or p != run[-1] + 1):
            if side in ("N", "S"):
                runs.append((side, [(m, line, "N") for m in run]))
            else:
                runs.append((side, [(line, m, "W") for m in run]))
            run = []
        if p is not None:
            run.append(p)
    return runs


def _outside_runs(plan: Plan, idx: int) -> list[tuple[str, list[tuple[int, int, str]]]]:
    """A room's exterior walls, as (facing, edges) straight runs, longest first.

    Built per room and per side so that a run is a real stretch of one wall.
    Walking the whole building's edge list instead - every tile's four sides
    interleaved - broke the side walls into runs one tile long, which is why
    windows landed where they did. The runs themselves are cached on the plan:
    windows, the back door and the shop front all ask for them, and scanning
    the floor once per room per caller is where the layout time went.
    """
    packed_all = _ensure_runs(plan)
    if idx < 0 or idx >= len(packed_all):
        return []
    runs: list[tuple[str, list[tuple[int, int, str]]]] = []
    for rec in packed_all[idx]:
        side = str(rec["side"])
        line = int(rec["line"])
        positions = [m for m in range(int(rec["start"]), int(rec["end"]))
                     if _side_edge(side, line, m) not in plan.party]
        runs.extend(_runs_from_positions(side, line, positions))
    runs.sort(key=lambda r: -len(r[1]))
    return runs


# Where the way in goes, in order of preference. Never straight into a
# bathroom or a bedroom unless there is truly nothing else.
ENTRY_KINDS = ["hall", "lobby", "livingroom", "restaurant", "church",
               "classroom", "clinic", "warehouse", "kitchen", "office",
               "garage", "dining", "storage", "bedroom", "bathroom"]


def _exterior_door(plan: Plan, rng: random.Random,
                   avoid: tuple[int, int] | None = None,
                   street: str | None = None) -> None:
    """One way in, into the room a visitor would expect to arrive in.

    `avoid` is the foot of the staircase, so the front door does not open
    straight onto the bottom step.
    """
    order = {k: i for i, k in enumerate(ENTRY_KINDS)}
    if plan.kind in ("shop", "restaurant"):
        # Customers come in through the shop, not the staff stairs.
        order.update({k: -1 for k in RETAIL_ROOMS | FRONT_ROOMS})
    candidates = sorted((i for i in range(1, len(plan.rooms) + 1)
                         if not plan.rooms[i - 1].is_shaft),
                        key=lambda i: (order.get(plan.rooms[i - 1].kind, 50),
                                       -plan.rooms[i - 1].area))
    for idx in candidates:
        runs = _outside_runs(plan, idx)
        if not runs:
            continue

        def score(run):
            side, wall = run
            mid = wall[len(wall) // 2]
            far = 0.0
            if avoid is not None:
                far = abs(mid[0] - avoid[0]) + abs(mid[1] - avoid[1])
            # Facing the street, as front doors do; without that the door
            # went on the south wall and half the paths wrapped round houses.
            return (len(wall) >= 3, side == (street or "S"), far, len(wall))

        side, wall = max(runs, key=score)
        front = wall[len(wall) // 2]
        plan.doors.append(front)
        _more_ways_in(plan, front)
        return


# One door per this much exterior wall, in tiles.
#
# A building got exactly one, wherever it landed. On a house that is a front
# door; on a church, a works or a parade of shops a hundred metres round, it
# is one door somewhere along the back, and everyone who walked up to the
# front reported a building with no door anywhere. Real buildings that size
# have several ways in, so these do - about one every thirty-odd metres,
# which is near enough that you meet one whichever side you arrive from.
DOOR_EVERY_TILES = 34
MAX_EXTERIOR_DOORS = 6
# Two doors closer together than this are one entrance, not two.
DOORS_APART_TILES = 12
# Nobody's front door opens into these, and a second one need not either.
PRIVATE_ROOMS = {"bathroom", "bedroom", "kidsbedroom", "closet", "cells"}


def _more_ways_in(plan: Plan, front: tuple[int, int, str]) -> None:
    """Extra doors round a big building, spread along its walls.

    `front` is the door already hung, which the rest keep away from.
    """
    if plan.kind in (None, "house"):
        return          # a house has its front door and its back door
    walls: list[tuple[int, list]] = []
    for idx in range(1, len(plan.rooms) + 1):
        room = plan.rooms[idx - 1]
        if room.is_shaft:
            continue
        private = room.kind in PRIVATE_ROOMS
        for _side, wall in _outside_runs(plan, idx):
            if len(wall) >= 3:
                walls.append((len(wall) - (1000 if private else 0), wall))
    perimeter = sum(len(wall) for _rank, wall in walls)
    want = min(MAX_EXTERIOR_DOORS, perimeter // DOOR_EVERY_TILES)
    placed = [(front[0], front[1])]
    # Longest walls first, and never twice on one stretch or beside a door
    # already hung. Rooms nobody enters a building through come last.
    for _rank, wall in sorted(walls, key=lambda r: -r[0]):
        if len(placed) >= want:
            break
        spot = wall[len(wall) // 2]
        if any(abs(spot[0] - px) + abs(spot[1] - py) < DOORS_APART_TILES
               for px, py in placed):
            continue
        plan.doors.append(spot)
        placed.append((spot[0], spot[1]))


OPPOSITE_SIDE = {"N": "S", "S": "N", "W": "E", "E": "W"}


def _back_door(plan: Plan, street: str | None = None) -> None:
    """A second way out, into the back yard: on the wall facing away from the
    street, from the kitchen if it has that wall, else the room nearest it.

    Knox County's houses have two outside doors as a rule (the median of 546);
    ours had one, and the second one picked any far wall, so it could open
    onto the side of the house instead of the yard behind it.
    """
    if not plan.doors:
        return
    fx, fy, _fd = plan.doors[-1]
    back = OPPOSITE_SIDE.get(street or "S", "N")
    for wanted in (back, None):
        for kind in ("kitchen", "dining", "hall", "livingroom", "laundry", "bedroom"):
            for idx, room in enumerate(plan.rooms, 1):
                if room.kind != kind or room.is_shaft:
                    continue
                runs = [wall for side, wall in _outside_runs(plan, idx)
                        if len(wall) >= 3 and (side == wanted if wanted else
                        abs(wall[len(wall) // 2][0] - fx) + abs(wall[len(wall) // 2][1] - fy) > 6)]
                if runs:
                    wall = max(runs, key=len)
                    plan.doors.append(wall[len(wall) // 2])
                    return


# Tiles of wall per window bay, by what the building is, on its front and on
# its other sides. Not a setting: how glazed a facade is follows from what the
# building is. A tile is about a metre, and real buildings put a window in
# roughly every room-width of wall: a house every four metres or so across its
# front and fewer down the side; flats every three metres on every side, since
# each flat looks out wherever it can; offices and hotels close to a band of
# glass; a works or a barn only a few high windows. The old spacings (nine and
# sixteen tiles for flats) left city blocks looking like warehouses.
FACADE_SPACING = {
    "house": (3, 4), "apartment": (3, 3), "barn": (10, 20), "shed": (12, 24),
    "industrial": (6, 9), "shop": (3, 5), "restaurant": (3, 5),
    "civic": (2, 2), "school": (3, 3), "church": (4, 5), "medical": (3, 3),
}
DEFAULT_FACADE_SPACING = (4, 6)
# Towers are glass: from this many storeys an office or hotel is glazed on
# every tile of its outside wall.
GLASS_TOWER_FROM_LEVELS = 8
# The ground floor of a shop or restaurant is its shop front: glass across the
# front, broken only by the door.
SHOP_FRONT_KINDS = {"shop", "restaurant"}
RETAIL_ROOMS = {"generalstore", "conveniencestore", "clothingstore", "cafe", "grocery",
                "liquorstore", "pharmacy", "bookstore", "toolstore"}
# Buildings laid out like a house, where each room takes only the windows it
# needs. Elsewhere a room takes whatever its stretch of facade offers - an
# open office along a glazed wall is not limited to one window.
HOUSE_LIKE_KINDS = {"house", "barn", "shed", None}
# A house window per this many tiles of a room's outside wall, and the tiles
# kept clear either side of a shuttered window (its shutters, and a gap).
HOUSE_WINDOW_EVERY = 3
WINDOW_CLEARANCE_SHUTTERED = 2
# Most windows one room may take, whatever the facade offers it.
ROOM_WINDOW_CAP = {
    "bathroom": 1, "storage": 1, "hall": 1, "garage": 0, "shed": 1, "elevator": 0,
    "kitchen": 2, "bedroom": 3, "dining": 3, "office": 2, "livingroom": 5,
    "kidsbedroom": 2, "closet": 0, "laundry": 1,
    "generalstore": 6, "conveniencestore": 6, "clothingstore": 6, "cafe": 6,
}
DEFAULT_ROOM_WINDOW_CAP = 4
# Outside house-like buildings a facade keeps its rhythm whatever is behind
# it - a stockroom or washroom on an office front still has its window - so
# only these are capped. Limiting bathrooms and storerooms as in a house left
# a third of an office block's bays empty, holes all over the grid.
SERVICE_WINDOW_CAP = {"garage": 0, "elevator": 0, "shed": 1}
# Rooms that must not be left without daylight.
# Kept to the rooms that matter: guaranteeing every office and dining room a
# window as well pushed facades back up to 1.09 windows per ten tiles of wall.
# Those rooms still get windows from the bays; they just are not promised one.
LIVED_IN = {"livingroom", "bedroom", "kidsbedroom", "kitchen", "classroom", "restaurant"}
MIN_WALL_FOR_WINDOW = 3


def _facade_runs(grid) -> list[tuple[str, list[tuple[int, int, str, int, int]]]]:
    """The building's outside walls as straight runs.

    Each edge is (x, y, dir, inside_x, inside_y): where BuildingEd draws the
    wall, and the tile of the building behind it. A straight stretch can cross
    several rooms; the per-room runs cannot, so this is grouped from the
    outside edges of the whole floor.
    """
    grid = np.asarray(grid)
    if grid.ndim != 2 or grid.size == 0:
        return []
    y, x, side, a, b = grids.outside_edges(grid)
    if y.size == 0:
        return []
    on_b = b != 0
    west = side == "W"
    # 0 N, 1 S, 2 W, 3 E: the order a row-major walk used to meet each face.
    face = np.where(west, np.where(on_b, 2, 3), np.where(on_b, 0, 1)).astype(np.int8)
    line = np.where(west, x, y).astype(np.int32)
    pos = np.where(west, y, x).astype(np.int32)
    order = np.lexsort((pos, line, face))
    face, line, pos = face[order], line[order], pos[order]
    fresh = np.empty(pos.shape, dtype=bool)
    fresh[0] = True
    if pos.size > 1:
        fresh[1:] = (
            (face[1:] != face[:-1])
            | (line[1:] != line[:-1])
            | (np.diff(pos) != 1)
        )
    starts = np.flatnonzero(fresh)
    ends = np.empty_like(starts)
    ends[:-1] = starts[1:]
    ends[-1] = pos.size
    names = ("N", "S", "W", "E")
    runs = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        which = names[int(face[s])]
        fixed = int(line[s])
        edges = []
        for m in pos[s:e].tolist():
            if which == "N":
                edges.append((m, fixed, "N", m, fixed))
            elif which == "S":
                edges.append((m, fixed, "N", m, fixed - 1))
            elif which == "W":
                edges.append((fixed, m, "W", fixed, m))
            else:
                edges.append((fixed, m, "W", fixed - 1, m))
        runs.append((which, edges))
    return runs


def _room_edges(grid) -> set[tuple[int, int, str]]:
    """The walls BuildingEd draws between one room and the next.

    _facade_runs only knows about the outside of the building. These are just
    as real to the renderer: where a room wall meets the facade on the same
    tile, that tile carries both a west and a north wall and is drawn as one
    corner piece.
    """
    grid = np.asarray(grid)
    if grid.size == 0:
        return set()
    y, x, side, a, b = grids.wall_edges(grid)
    keep = (a > 0) & (b > 0)
    return {(int(x[i]), int(y[i]), str(side[i])) for i in np.flatnonzero(keep)}


def _sides_of(grid, x: int, y: int, d: str) -> tuple[int, int]:
    """The room ids either side of a wall edge; 0 is outside."""
    grid = np.asarray(grid)
    h, w = grid.shape

    def at(px, py):
        if 0 <= px < w and 0 <= py < h:
            return int(grid[py, px])
        return 0

    return (at(x - 1, y), at(x, y)) if d == "W" else (at(x, y - 1), at(x, y))


def _doors_off_corners(storey, edges: set, corners: set) -> int:
    """Slide a door off an inside corner, where there is somewhere to slide to.

    A door has the same trouble a window does - BuildingEd draws a tile
    carrying both a west and a north wall as one corner piece, and a door
    replaces it with a door facing one way, losing the other half. A door
    cannot simply be dropped, though: the room behind it may have no other
    way in. So it moves along its own wall to the nearest tile that is not a
    corner and has the same two rooms either side of it, and if there is no
    such tile it stays where it is - a door in a broken corner still beats a
    room nobody can enter.
    """
    taken = set(storey.doors)
    keep_off = set(storey.party) | set(getattr(storey, "wall_pieces", ()))
    # Every wall that is not a corner, by the pair of rooms it stands between.
    # Sliding along the door's own wall is not enough: a stepped diagonal side
    # is a wall one or two tiles long, so there is nowhere on it to slide to.
    # A door only has to separate the same two spaces, and the rest of that
    # boundary will do.
    boundary: dict[tuple[int, int], list] = {}
    for (ex, ey, ed) in edges:
        if (ex, ey) in corners or (ex, ey, ed) in keep_off:
            continue
        boundary.setdefault(_sides_of(storey.grid, ex, ey, ed), []).append((ex, ey, ed))
    moved = 0
    for i, (x, y, d) in enumerate(storey.doors):
        if (x, y) not in corners:
            continue
        want = _sides_of(storey.grid, x, y, d)
        spots = [s for s in boundary.get(want, ()) if s not in taken]
        if not spots:
            continue
        # The nearest one, so a front door stays on the front of the house.
        spot = min(spots, key=lambda s: (abs(s[0] - x) + abs(s[1] - y), s))
        taken.discard((x, y, d))
        taken.add(spot)
        storey.doors[i] = spot
        moved += 1
    return moved


def _front_side(building: "Building", kind: str | None) -> set[str]:
    """Which walls count as the front, and get the closer window spacing.

    For a house, the wall with the front door. For a block of flats, both long
    sides: the ends are where the corridor comes out, and the flats look out
    over the street from the sides.
    """
    ground = building.storeys[0]
    if kind == "apartment" and ground.core is not None:
        x0, y0, x1, y1 = ground.core
        return {"W", "E"} if (y1 - y0) >= (x1 - x0) else {"N", "S"}
    for x, y, d in ground.doors:
        # An outside door has building on exactly one side of it.
        if d == "N":
            above, below = _room_at(ground, x, y - 1), _room_at(ground, x, y)
            if bool(above) != bool(below):
                return {"N"} if below else {"S"}
        else:
            left, right = _room_at(ground, x - 1, y), _room_at(ground, x, y)
            if bool(left) != bool(right):
                return {"W"} if right else {"E"}
    return {"S"}


def _bays(by_side: dict, front: set[str], near: int, far: int,
          only: set[str] | None = None) -> list[tuple[int, int, str, int, int]]:
    """Window positions along each side, `near` tiles apart on the front and
    `far` elsewhere. `only` limits them to those sides."""
    bays: list[tuple[int, int, str, int, int]] = []
    for side, edges in by_side.items():
        if only is not None and side not in only:
            continue
        # Position along the side: x for north and south faces, y for west
        # and east.
        along = (lambda e: e[3]) if side in ("N", "S") else (lambda e: e[4])
        positions = sorted({along(e) for e in edges})
        if len(positions) < MIN_WALL_FOR_WINDOW:
            continue
        spacing = max(1, near if side in front else far)
        if side not in front and len(positions) < spacing:
            continue            # a short side stays blank
        inner = positions[1:-1]
        if spacing == 1:
            chosen = set(inner)
        else:
            count = max(1, round(len(inner) / spacing))
            step = len(inner) / count
            chosen = {inner[int((i + 0.5) * step)] for i in range(count)}
        bays.extend(e for e in edges if along(e) in chosen)
    return bays


def _place_windows(building: "Building", kind: str | None,
                   shop_ground: bool = False) -> None:
    """Windows in bays down the facade, the same bays on every storey.

    Two things made the buildings look wrong. Windows were spaced along every
    wall at the same pitch, so a house was as glazed down its side as across
    its front; and each storey placed its own, so no window sat above the one
    below. Real facades are built in bays: the positions are decided once for
    the building and repeated floor by floor, and a storey skips a bay only
    where the room behind has no use for a window.

    How many bays there are is not a setting. It follows from what the
    building is (FACADE_SPACING): a glass tower, a shop front, a block of
    flats and a barn are glazed nothing alike, and one dial for all of them
    could only ever be right for one.

    Bays are counted along each side of the building rather than along each
    straight run of wall. A building on its real footprint at 38 degrees has
    no straight runs at all - its sides are staircases of one- and two-tile
    steps - and requiring three tiles of straight wall left such a house with
    two windows. Counting along the side puts a window every few steps of the
    staircase, the way it would sit on the real diagonal wall.
    """
    if not building.storeys:
        return
    front = _front_side(building, kind)
    near, far = FACADE_SPACING.get(kind or "house", DEFAULT_FACADE_SPACING)
    if kind == "civic" and len(building.storeys) >= GLASS_TOWER_FROM_LEVELS:
        near = far = 1
    by_side: dict[str, list[tuple[int, int, str, int, int]]] = {}
    for side, wall in _facade_runs(building.storeys[0].grid):
        by_side.setdefault(side, []).extend(wall)
    bays = _bays(by_side, front, near, far)
    ground_bays = bays
    glass: list[tuple[int, int, str, int, int]] = []
    glass_set: set = set()
    # A storey stepped back from the ones below has its own outside walls.
    bays_by_grid: dict = {}

    def bays_for(grid):
        # The footprint, not the room numbers: two storeys with the same
        # outside walls share bays even when the rooms inside differ.
        g = np.asarray(grid)
        key = (g.shape, np.ascontiguousarray(g).astype(bool).tobytes())
        if key not in bays_by_grid:
            sides: dict[str, list] = {}
            for side, wall in _facade_runs(g):
                sides.setdefault(side, []).extend(wall)
            bays_by_grid[key] = _bays(sides, front, near, far)
        return bays_by_grid[key]

    if kind in SHOP_FRONT_KINDS or shop_ground:
        glass = _bays(by_side, front, 1, far, only=front)
        # Glass only where there is a shop behind it: a block of flats' long
        # sides are both "front", and its offices and toilets were glazed
        # like the shops.
        g0 = building.storeys[0]
        sales = RETAIL_ROOMS | FRONT_ROOMS | {"restaurant"}
        glass = [b for b in glass
                 if (rid := int(g0.grid[b[4], b[3]]))
                 and g0.rooms[rid - 1].kind in sales]
        glass_set = set(glass)
        ground_bays = glass + [b for b in bays if b not in glass_set]
    house_like = kind in HOUSE_LIKE_KINDS

    for level, storey in enumerate(building.storeys):
        blocked = set(getattr(storey, "wall_pieces", set())) | storey.party
        # No window where a tile carries both a west and a north wall - the
        # inside corner of every step down a diagonal side. BuildingEd draws
        # that corner as one piece; a window there replaced it with a window
        # facing one way, and the other half of the wall was left out.
        #
        # A room wall counts as much as the facade does. This used to ask
        # _facade_runs alone, which only knows the outside of the building, so
        # the corner where a room's wall meets the facade was not blocked -
        # and that is most of them. Counted on a city map: 290 windows over
        # 60 buildings hanging in a gap, every one of them where an inside
        # wall arrives at the outside one on the same tile.
        edges = {(x, y, d) for _side, wall in _facade_runs(storey.grid)
                 for x, y, d, _ix, _iy in wall} | _room_edges(storey.grid)
        on_corner = {(x, y) for x, y, d in edges
                     if (x, y, "N" if d == "W" else "W") in edges}
        blocked |= {(x, y, d) for x, y, d in edges if (x, y) in on_corner}
        # Doors land on those corners too, and break them the same way. They
        # move along their wall rather than being dropped, so nothing is shut
        # in - and it happens before the windows are placed, which keep clear
        # of wherever the doors end up.
        _doors_off_corners(storey, edges, on_corner)
        # Shuttered windows need the tile either side for their shutters: a
        # window two tiles from a door or another window had its shutters
        # jammed against the frame or overlapping the next one's.
        clear = WINDOW_CLEARANCE_SHUTTERED if house_like else 1
        for x, y, d in storey.doors:
            for off in range(-clear, clear + 1):
                blocked.add((x + off, y, d) if d == "N" else (x, y + off, d))
        # Windows already hung, sorted along each wall line, so the clearance
        # test is a neighbour lookup rather than a walk of every window.
        along: dict[str, dict[int, list[int]]] = {"N": {}, "W": {}}

        def spaced(x, y, d):
            line, pos = (y, x) if d == "N" else (x, y)
            spots = along[d].get(line)
            if not spots:
                return True
            i = bisect.bisect_left(spots, pos)
            if i < len(spots) and spots[i] - pos <= clear:
                return False
            if i and pos - spots[i - 1] <= clear:
                return False
            return True

        def add(edge):
            storey.windows.append(edge)
            x, y, d = edge
            line, pos = (y, x) if d == "N" else (x, y)
            bisect.insort(along[d].setdefault(line, []), pos)

        taken: dict[int, int] = {}
        if house_like:
            # A house's windows are placed room by room, centred on each
            # outside wall and evenly spread along it, as Knox County's are.
            # Bays laid out for the whole facade put two or three windows
            # side by side in one room and none along the rest of the wall.
            for idx, room in enumerate(storey.rooms, 1):
                if room.is_shaft:
                    continue
                cap = ROOM_WINDOW_CAP.get(room.kind, DEFAULT_ROOM_WINDOW_CAP)
                if room.is_core:
                    cap = 0
                runs = sorted(_outside_runs(storey, idx), key=lambda r: -len(r[1]))
                for _side, wall in runs:
                    if taken.get(idx, 0) >= cap or len(wall) < MIN_WALL_FOR_WINDOW:
                        continue
                    n = max(1, min(cap - taken.get(idx, 0),
                                   (len(wall) + 1) // HOUSE_WINDOW_EVERY))
                    for k in range(n):
                        i = int((k + 0.5) * len(wall) / n)
                        # The nearest spot to the even spacing that is clear
                        # of doors, corners and the other windows.
                        for j in sorted(range(1, len(wall) - 1), key=lambda j: abs(j - i)):
                            edge = wall[j]
                            if edge not in blocked and spaced(*edge):
                                add(edge)
                                taken[idx] = taken.get(idx, 0) + 1
                                break
        for x, y, d, ix, iy in ([] if house_like else (ground_bays if level == 0 else bays_for(storey.grid))):
            if (x, y, d) in blocked:
                continue
            idx = int(storey.grid[iy, ix])
            if not idx:
                continue
            room = storey.rooms[idx - 1]
            if house_like:
                cap = ROOM_WINDOW_CAP.get(room.kind, DEFAULT_ROOM_WINDOW_CAP)
            else:
                cap = SERVICE_WINDOW_CAP.get(room.kind, 1 << 30)
            if room.is_shaft:
                continue
            # A corridor or stair hall along an outside wall gets that wall's
            # windows like any room: capped at one, a corridor running the
            # length of a tower's side left sixty tiles of blank brick.
            if taken.get(idx, 0) >= cap:
                continue
            add((x, y, d))
            taken[idx] = taken.get(idx, 0) + 1
            if level == 0 and (kind in SHOP_FRONT_KINDS or shop_ground) \
                    and (x, y, d, ix, iy) in glass_set:
                storey.shop_front.add((x, y, d))

        # The bays keep windows in columns, but a room they miss would have no
        # daylight at all. Any room people spend time in that still has none
        # gets one in the middle of its longest outside wall.
        for idx, room in enumerate(storey.rooms, 1):
            if taken.get(idx) or room.kind not in LIVED_IN:
                continue
            for _side, wall in _outside_runs(storey, idx):
                # A straight wall keeps its corners clear; a one- or two-tile
                # step of a diagonal wall has no corners to spare.
                inner = wall[1:-1] if len(wall) >= MIN_WALL_FOR_WINDOW else wall
                candidates = sorted(inner, key=lambda e: abs(
                    wall.index(e) - len(wall) // 2))
                spot = next((e for e in candidates if e not in blocked and spaced(*e)), None)
                if spot is not None:
                    add(spot)
                    taken[idx] = 1
                    break


# A piece against a wall should have its back to that wall. Most pieces define
# all four facings; the few that only define N and W fall back to the nearest.
_ORIENT_FALLBACK = {"S": "N", "E": "W", "N": "N", "W": "W"}


def _facing(role: str, wanted: str) -> str:
    """The orientation to actually emit for `role` against a given wall."""
    have = C.FURNITURE[role]
    if wanted in have:
        return wanted
    alt = _ORIENT_FALLBACK[wanted]
    return alt if alt in have else next(iter(have))


# Door clearance, the flight, furniture, and a temporary halo. A cell keeps
# every reason it is blocked; checks name the bits they care about, so a
# stair tile does not stop a piece that is allowed to stand on the flight.
B_DOOR = 1
B_STAIR = 2
B_ITEM = 4
B_EXTRA = 8
B_CLEAR = B_DOOR | B_STAIR | B_ITEM | B_EXTRA

_OFFSETS: dict[str, dict[str, tuple[tuple[int, int], ...]]] = {}


def _offsets(role: str, orient: str) -> tuple[tuple[int, int], ...]:
    """Tile offsets of one piece, cached. The catalog walks the same key order."""
    by_orient = _OFFSETS.get(role)
    if by_orient is None:
        by_orient = {}
        _OFFSETS[role] = by_orient
    hit = by_orient.get(orient)
    if hit is None:
        keys = C.FURNITURE[role][orient]
        hit = tuple(tuple(map(int, raw.split(","))) for raw in keys)
        by_orient[orient] = hit
    return hit


def _cells_for(role: str, x: int, y: int, orient: str) -> list[tuple[int, int]]:
    """Tiles a furniture piece covers, derived from its tile-offset keys."""
    off = _offsets(role, orient)
    return [(x + dx, y + dy) for dx, dy in off]


def _bind_occ(plan: Plan) -> memoryview | None:
    occ = plan.occ
    if occ is None:
        plan._occ_flat = None
        plan._occ_mv = None
        return None
    # A copy would stop seeing later in-place writes to plan.occ.
    if not occ.flags.c_contiguous or occ.dtype != np.uint8:
        occ = np.ascontiguousarray(occ, dtype=np.uint8)
        plan.occ = occ
    flat = occ.reshape(-1)
    plan._occ_flat = flat
    plan._occ_mv = memoryview(flat)
    return plan._occ_mv


def _occ_at(plan: Plan, x: int, y: int) -> int:
    mv = plan._occ_mv
    if mv is None:
        mv = _bind_occ(plan)
    return mv[y * plan.width + x]


def _occ_mark(plan: Plan, cells, bit: int) -> None:
    occ = plan.occ
    if occ is None:
        return
    h, w = occ.shape
    flag = np.uint8(bit)
    for x, y in cells:
        if 0 <= x < w and 0 <= y < h:
            occ[y, x] |= flag


def _busy(plan: Plan, x: int, y: int, bits: int, *backups) -> bool:
    """In-bounds cells ask the occupancy grid. A tile past the edge has no
    cell, so it still has to be looked up in the set it came from."""
    if plan.occ is not None and 0 <= x < plan.width and 0 <= y < plan.height:
        return bool(_occ_at(plan, x, y) & bits)
    return any((x, y) in group for group in backups)


def _busy_any(plan: Plan, cells, bits: int, *backups) -> bool:
    # This is one of furnishing's innermost loops. Keep the same out-of-bounds
    # fallback semantics without a generator and a Python function call per
    # occupied tile.
    if plan.occ is not None:
        mv = plan._occ_mv
        if mv is None:
            mv = _bind_occ(plan)
        w = plan.width
        h = plan.height
        for x, y in cells:
            if 0 <= x < w and 0 <= y < h:
                if mv[y * w + x] & bits:
                    return True
            elif any((x, y) in group for group in backups):
                return True
        return False
    return any((x, y) in group for x, y in cells for group in backups)


def _covers(plan: Plan, idx: int, x: int, y: int, offsets) -> bool:
    """Every tile of the piece sits in room `idx`. Out of range is room 0."""
    ids = plan._ids
    if ids is None:
        ids = _bind_ids(plan)
    w = plan.width
    h = plan.height
    for dx, dy in offsets:
        cx = x + dx
        cy = y + dy
        if cx < 0 or cy < 0 or cx >= w or cy >= h or ids[cy * w + cx] != idx:
            return False
    return True


def _piece_fits(plan: Plan, idx: int, x: int, y: int, offsets, bits: int,
                *backups) -> bool:
    """The piece is entirely in `idx` and `_busy_any` would not reject it."""
    ids = plan._ids
    if ids is None:
        ids = _bind_ids(plan)
    occ = plan.occ
    mv = plan._occ_mv if occ is not None else None
    if occ is not None and mv is None:
        mv = _bind_occ(plan)
    w = plan.width
    h = plan.height
    for dx, dy in offsets:
        cx = x + dx
        cy = y + dy
        if 0 <= cx < w and 0 <= cy < h:
            if ids[cy * w + cx] != idx:
                return False
            if mv is not None:
                if mv[cy * w + cx] & bits:
                    return False
            elif any((cx, cy) in group for group in backups):
                return False
        else:
            if idx != 0 or any((cx, cy) in group for group in backups):
                return False
    return True


def _clear_room_occ(plan: Plan, idx: int) -> None:
    """Drop a neighbour's aisle spill and any halo from the room before it.

    The spill is one tile into this room and is not furniture here. Leaving
    it set would push this room's pieces off the tiles the old set, which
    started empty, would have allowed.
    """
    if plan.occ is None:
        return
    plan.occ &= np.uint8(~B_EXTRA & 0xFF)
    plan.occ[plan.grid == idx] &= np.uint8(~B_ITEM & 0xFF)


def _wall_slots(plan: Plan, idx: int, room: Room,
                stair_tiles: set[tuple[int, int]]) -> list[tuple[int, int, str]]:
    """Tiles a piece can stand on, back to a wall, in row-major order.

    Each facing is the neighbour just past that side of the cell. The box is
    only the room's rectangle; the row outside it is read so a wall on the
    edge of the rectangle is still a wall.
    """
    g = plan.grid
    y0, x0 = room.y0, room.x0
    y1, x1 = room.y1 + 1, room.x1 + 1
    sub = g[y0:y1, x0:x1]
    here = sub == idx
    north = np.zeros(here.shape, np.int32)
    south = np.zeros(here.shape, np.int32)
    west = np.zeros(here.shape, np.int32)
    east = np.zeros(here.shape, np.int32)
    if y0 > 0:
        north[0, :] = g[y0 - 1, x0:x1]
    if here.shape[0] > 1:
        north[1:, :] = sub[:-1, :]
    if y1 < plan.height:
        south[-1, :] = g[y1, x0:x1]
    if here.shape[0] > 1:
        south[:-1, :] = sub[1:, :]
    if x0 > 0:
        west[:, 0] = g[y0:y1, x0 - 1]
    if here.shape[1] > 1:
        west[:, 1:] = sub[:, :-1]
    if x1 < plan.width:
        east[:, -1] = g[y0:y1, x1]
    if here.shape[1] > 1:
        east[:, :-1] = sub[:, 1:]
    for sx, sy in stair_tiles:
        if y0 <= sy < y1 and x0 <= sx < x1:
            here[sy - y0, sx - x0] = False
    ys, xs = np.nonzero(here)
    if ys.size == 0:
        return []
    n_hit = north[ys, xs] != idx
    s_hit = south[ys, xs] != idx
    w_hit = west[ys, xs] != idx
    e_hit = east[ys, xs] != idx
    slots: list[tuple[int, int, str]] = []
    for i in range(int(ys.size)):
        x = int(xs[i]) + x0
        y = int(ys[i]) + y0
        if n_hit[i]:
            slots.append((x, y, "N"))
        if s_hit[i]:
            slots.append((x, y, "S"))
        if w_hit[i]:
            slots.append((x, y, "W"))
        if e_hit[i]:
            slots.append((x, y, "E"))
    return slots


SWITCH = "switch"
# One light switch per this much floor, in tiles, and never more than this
# many in a room. A room of a hundred tiles keeps its single switch; a
# supermarket's sales floor gets one about every eight metres.
LIGHT_EVERY_TILES = 110
MAX_SWITCHES = 8
SWITCHES_APART = 7
# Things fixed to a wall rather than standing against it. A painting has only
# north and west sprites, so on a south or east wall the fallback drew it on
# the far edge of the tile, hanging in mid-air a tile into the room; shelves
# and mirrors on those walls rendered as planks floating over the floor. They
# go on north and west walls only. (The switch has true east and south
# sprites, and every room needs one, so it may go anywhere.)
NORTH_WEST_ONLY = ({"painting", "mirror", "shelf"} | set(getattr(C, "ERIKA_WALL_ART", ()))
                   | set(getattr(C, "ERIKA_SHOP_ADS", ())))
# With Erika's Tiles: shops hang its drinks and magazine posters instead of
# paintings, and these rooms get a drinks machine.
SHOP_DECOR_ROOMS = {"generalstore", "conveniencestore", "clothingstore", "cafe", "bar",
                    "grocery", "liquorstore", "pharmacy", "bookstore", "toolstore",
                    "restaurant", "gym"}
VENDING_ROOMS = {"cafe", "lobby", "gym", "classroom", "clinic", "breakroom"}
ERIKA_SHELF_SHARE = 0.5

_ERIKA: list[bool] = []


def _erika_ready() -> bool:
    """Whether buildings may use Erika's Tiles; asked once per process."""
    if not _ERIKA:
        import os
        try:
            import knoxpaths
            _ERIKA.append(os.environ.get("KNOXMAP_NO_MOD_TILES") != "1"
                          and knoxpaths.erikas_tiles_ready())
        except Exception:      # noqa: BLE001 - no tools, no mod tiles
            _ERIKA.append(False)
    return _ERIKA[0]


def _is_wall_piece(role: str) -> bool:
    """Hung on a wall rather than standing on the floor. Rugs are on their own
    floor layer but are floor pieces: placed as wall pieces, their 2x2
    footprint was never checked against the room and hung out through the
    outside wall, floating beside the building."""
    return C.FURNITURE_LAYERS.get(role, "Furniture") not in ("Furniture", "FloorFurniture")


def _wall_edge(x: int, y: int, facing: str) -> tuple[int, int, str]:
    """The wall a piece facing `facing` on tile (x, y) hangs on, as a door or
    window would name it."""
    if facing == "N":
        return (x, y, "N")
    if facing == "S":
        return (x, y + 1, "N")
    if facing == "W":
        return (x, y, "W")
    return (x + 1, y, "W")


# What stands in the middle of a room, as (role, dx, dy, orient) laid out for
# a room wider than it is deep; turned for the other way. Orient is the side a
# piece has its back to, as for pieces against a wall, so a chair north of a
# table has its back to the north. Rugs go on the floor layer under the rest.
# The group, and a clear tile all round it, has to fit inside the room away
# from the furniture along its walls and the tiles in front of doors, or the
# room keeps an open floor instead. `repeat` is how many a big room may take.
# FALLBACK_GROUPS are smaller sets tried when the first does not fit.
CENTRE_GROUPS: dict[str, tuple[list[tuple[str, int, int, str]], int]] = {
    # The sofa facing the television across the coffee table, an armchair at
    # the side: how every lived-in living room in Knox County is arranged.
    "livingroom": ([("rug_wide", 0, 1, "W"), ("sofa", 0, 0, "N"), ("armchair", 3, 2, "E"),
                    ("coffee_table", 0, 2, "N"), ("tv", 1, 4, "S")], 1),
    "lobby": ([("rug_wide", 0, 0, "W"), ("coffee_table", 1, 0, "W")], 2),
    "dining": ([("rug_wide", 0, 0, "W"), ("dining_table", 1, 1, "W"),
                ("chair", 0, 1, "W"), ("chair", 3, 1, "E"),
                ("chair", 1, 0, "N"), ("chair", 2, 2, "S")], 1),
    "kitchen": ([("round_table", 1, 0, "W"), ("chair", 0, 0, "W"),
                 ("chair", 2, 0, "E")], 1),
    "bedroom": ([("rug_small", 0, 0, "W")], 1),
    "office": ([("dining_table", 0, 1, "W"), ("chair", 0, 0, "N")], 3),
    "library": ([("dining_table", 1, 1, "W"), ("chair", 0, 1, "W"),
                 ("chair", 3, 1, "E")], 3),
    "classroom": ([("dining_table", 0, 1, "W"), ("chair", 0, 0, "N"),
                   ("chair", 1, 0, "N")], 6),
    "cafe": ([("round_table", 1, 0, "W"), ("chair", 0, 0, "W"),
              ("chair", 2, 0, "E")], 3),
    "restaurant": ([("round_table", 1, 1, "W"), ("chair", 0, 1, "W"),
                    ("chair", 2, 1, "E"), ("chair", 1, 0, "N"),
                    ("chair", 1, 2, "S")], 6),
}
# Every dining room seats people the same way, however it is named; the salon
# and the ice cream parlour have tables too.
# (Dining rooms, cafés and bars are fitted out by interiors.furnish_dining.)
CENTRE_GROUPS["breakroom"] = CENTRE_GROUPS["cafe"]
CENTRE_GROUPS["theatre"] = ([("chair", 0, 0, "S"), ("chair", 1, 0, "S"), ("chair", 2, 0, "S"),
                             ("chair", 3, 0, "S"), ("chair", 4, 0, "S"), ("chair", 5, 0, "S")], 40)
# Commercial kitchens are fitted with counters wall to wall, like a home's.
KITCHENS = {"kitchen", "breakroom", "restaurantkitchen", "pizzakitchen", "burgerkitchen",
            "dinerkitchen", "chinesekitchen", "sushikitchen", "mexicankitchen", "seafoodkitchen",
            "cafekitchen", "bakerykitchen", "icecreamkitchen"}
FALLBACK_GROUPS: dict[str, list[list[tuple[str, int, int, str]]]] = {
    "livingroom": [[("sofa", 0, 0, "N"), ("coffee_table", 0, 2, "N"), ("tv", 0, 4, "S")],
                   [("sofa", 0, 0, "N"), ("coffee_table", 0, 2, "N")],
                   [("rug_small", 0, 0, "W"), ("coffee_table", 0, 0, "N")]],
    "dining": [[("dining_table", 1, 0, "W"), ("chair", 0, 0, "W"), ("chair", 3, 0, "E")],
               [("round_table", 1, 0, "W"), ("chair", 0, 0, "W"), ("chair", 2, 0, "E")]],
    "kitchen": [[("round_table", 0, 0, "W"), ("chair", 1, 0, "E")]],
    "lobby": [[("rug_small", 0, 0, "W"), ("coffee_table", 0, 0, "N")]],
}
_TURN = {"W": "N", "N": "W", "E": "S", "S": "E"}


def _furnish_middle(plan: Plan, idx: int, room: Room,
                    occupied: set[tuple[int, int]],
                    keep_clear: set[tuple[int, int]],
                    palette: dict[str, str] | None = None) -> int:
    """Put the room's centre group(s) in its open middle. Returns how many."""
    spec = CENTRE_GROUPS.get(room.kind)
    if spec is None or room.is_core or room.is_shaft:
        return 0
    palette = palette or {}

    def own(group):
        return [(palette.get(role, role), dx, dy, o) for role, dx, dy, o in group]

    group, repeat = spec
    placed = _place_group(plan, idx, room, own(group), repeat, occupied, keep_clear)
    for smaller in FALLBACK_GROUPS.get(room.kind, ()):
        if placed:
            break
        placed = _place_group(plan, idx, room, own(smaller), 1, occupied, keep_clear)
    return placed


# Pieces a room has one of, however big it is.
ONCE = {"sofa", "tv", "double_bed", "bed", "bath", "toilet", "stove", "fridge", "washer",
        "shower", "kitchen_sink", "sink", "coffee_table", "dining_table", "wardrobe",
        "water_cooler", "whiteboard", "corkboard", "vending"}


def _once(role: str) -> bool:
    return role in ONCE or role.startswith(("sofa_", "double_bed", "bed_", "wardrobe", "erika_vending"))


def _by_dist(item):
    return item[0]


def _place_group(plan: Plan, idx: int, room: Room,
                 group: list[tuple[str, int, int, str]], repeat: int,
                 occupied: set[tuple[int, int]],
                 keep_clear: set[tuple[int, int]],
                 block_bits: int | None = None) -> int:
    """Up to `repeat` copies of one group, nearest the room's middle first."""
    if (room.x1 - room.x0) < (room.y1 - room.y0):
        # Rugs are drawn one way round whatever their orient, so a turned
        # group swaps the wide rug for the long one.
        swap = {"rug_wide": "rug", "rug": "rug_wide"}
        group = [(swap.get(role, role), dy, dx, _TURN[o]) for role, dx, dy, o in group]
    pieces = [(role, dx, dy, _facing(role, o)) for role, dx, dy, o in group]
    shape = [(dx + cx, dy + cy) for role, dx, dy, o in pieces
             for cx, cy in _cells_for(role, 0, 0, o)]
    gx0 = min(x for x, _ in shape)
    gy0 = min(y for _, y in shape)
    gx1 = max(x for x, _ in shape)
    gy1 = max(y for _, y in shape)
    solid = {(dx + cx, dy + cy) for role, dx, dy, o in pieces
             if C.FURNITURE_LAYERS.get(role, "Furniture") == "Furniture"
             for cx, cy in _cells_for(role, 0, 0, o)}
    mid_x, mid_y = (room.x0 + room.x1) / 2, (room.y0 + room.y1) / 2
    # Row-major candidates, stable on an equal distance so a tie lands on the
    # same cell the old sort kept. Rooms are small; a meshgrid per group spent
    # more time building arrays than ranking the cells.
    y_start, y_stop = room.y0 - gy0, room.y1 - gy1 + 1
    x_start, x_stop = room.x0 - gx0, room.x1 - gx1 + 1
    if y_start >= y_stop or x_start >= x_stop:
        return 0
    half_x = (gx0 + gx1) / 2
    half_y = (gy0 + gy1) / 2
    ranked = []
    for oy in range(y_start, y_stop):
        dy = abs(oy + half_y - mid_y)
        for ox in range(x_start, x_stop):
            ranked.append((abs(ox + half_x - mid_x) + dy, ox, oy))
    ranked.sort(key=_by_dist)
    bits = B_CLEAR if block_bits is None else block_bits
    flag = int(np.uint8(bits))
    h, w = plan.height, plan.width
    ids = plan._ids
    if ids is None:
        ids = _bind_ids(plan)
    mv = plan._occ_mv if plan.occ is not None else None
    if plan.occ is not None and mv is None:
        mv = _bind_occ(plan)
    placed = 0
    for _dist, ox, oy in ranked:
        if placed >= repeat:
            break
        ry0 = oy + gy0 - 1
        ry1 = oy + gy1 + 1
        rx0 = ox + gx0 - 1
        rx1 = ox + gx1 + 1
        if rx0 < 0 or ry0 < 0 or rx1 >= w or ry1 >= h:
            continue
        blocked = False
        for y in range(ry0, ry1 + 1):
            base = y * w
            for x in range(rx0, rx1 + 1):
                if ids[base + x] != idx:
                    blocked = True
                    break
            if blocked:
                break
        if blocked:
            continue
        if mv is not None:
            for y in range(ry0, ry1 + 1):
                base = y * w
                for x in range(rx0, rx1 + 1):
                    if mv[base + x] & flag:
                        blocked = True
                        break
                if blocked:
                    break
            if blocked:
                continue
        elif plan.occ is None:
            ring = {(x, y) for y in range(ry0, ry1 + 1) for x in range(rx0, rx1 + 1)}
            if any((x, y) in occupied or (x, y) in keep_clear for x, y in ring):
                continue
        for role, dx, dy, o in pieces:
            plan.furniture.append((role, ox + dx, oy + dy, o))
        # The whole footprint and its ring stay clear of the next copy, so
        # tables in a classroom keep an aisle between them.
        if plan.occ is not None:
            plan.occ[ry0:ry1 + 1, rx0:rx1 + 1] |= np.uint8(B_ITEM)
        occupied.update((x, y) for y in range(ry0, ry1 + 1) for x in range(rx0, rx1 + 1))
        occupied.update((ox + x, oy + y) for x, y in solid)
        placed += 1
    return placed


def _furnish(plan: Plan, rng: random.Random,
             stairs: tuple[int, int, str] | None = None,
             street: str | None = None) -> None:
    """Push furniture against room walls, skipping tiles a door needs clear.

    Every room, the corridor and stair hall included, first gets a light
    switch on the wall beside its door. The game hangs a room's ceiling light
    off its switch, so a room without one has no light at all - which is why
    so many rooms were pitch dark: the switch was only on some rooms'
    wishlists, and even there it was written to the floor rather than a wall.
    """
    door_tiles: set[tuple[int, int]] = set()
    door_edges = set(plan.doors)
    # The lift doors take their stretch of wall: nothing hangs on it.
    if plan.shaft_door is not None:
        lx, ly, ld = plan.shaft_door
        door_edges |= {(lx, ly + i, "W") if ld == "W" else (lx + i, ly, "N")
                       for i in range(SHAFT_SIZE)}
    for x, y, d in plan.doors:
        # Two tiles deep either side: one clear tile in front of a door still
        # had a fridge or a bookcase on the next, and nobody could step past.
        for k in range(DOOR_CLEAR_DEPTH):
            door_tiles.add((x + k, y) if d == "W" else (x, y + k))
            door_tiles.add((x - 1 - k, y) if d == "W" else (x, y - 1 - k))
    # Tiles of the flight. A switch hung there would be deleted along with the
    # furniture cleared off the staircase, leaving the stair hall dark.
    stair_tiles: set[tuple[int, int]] = set()
    if stairs is not None:
        sx, sy, sd = stairs
        dx, dy = (0, 1) if sd == "N" else (1, 0)
        stair_tiles = {(sx + dx * i, sy + dy * i) for i in range(STAIR_RUN)}

    plan.occ = np.zeros((plan.height, plan.width), np.uint8)
    _bind_occ(plan)
    _occ_mark(plan, door_tiles, B_DOOR)
    _occ_mark(plan, stair_tiles, B_STAIR)

    if plan.shaft_door is not None:
        lx, ly, ld = plan.shaft_door
        plan.furniture.append(("elevator_door", lx, ly, ld))

    from . import interiors
    # Each home its own furniture: one palette per flat, or per storey of a
    # house, drawn from a seed of this storey's own.
    home_seed = rng.random()
    palettes: dict[int, dict[str, str]] = {}

    for idx, r in enumerate(plan.rooms, start=1):
        # A lift car has no switch and no furniture: it is a sealed box.
        if r.is_shaft:
            continue
        # Furniture is appended room by room. Remember this room's first item
        # so its finishing pass need not rescan every earlier room's contents.
        room_furniture_start = len(plan.furniture)
        _clear_room_occ(plan, idx)
        # Walls this room has, as (x, y, facing) for a piece standing on the
        # tile with its back to that wall.
        slots = _wall_slots(plan, idx, r, stair_tiles)
        used_walls: set[tuple[int, int, str]] = set()
        # Outside walls are where the windows go, and the windows are laid out
        # last, in columns up the facade: a painting or switch hung there took
        # the bay and left a hole in the column. Hang things inside first.
        step = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}
        # One probe per slot. Each later sort used to ask the grid again.
        facade: dict[tuple[int, int, str], bool] = {}
        for slot in slots:
            sx, sy, facing = slot
            ox, oy = step[facing]
            facade[slot] = not _room_at(plan, sx + ox, sy + oy)

        def hang(role: str, x: int, y: int, facing: str) -> bool:
            if role in NORTH_WEST_ONLY and facing in ("S", "E"):
                return False
            edge = _wall_edge(x, y, facing)
            if edge in door_edges or edge in used_walls:
                return False
            orient = _facing(role, facing)
            # Every tile of the piece in this room: a two-tile mirror hung at
            # the end of a wall reached through the outside wall.
            if not _covers(plan, idx, x, y, _offsets(role, orient)):
                return False
            plan.furniture.append((role, x, y, orient))
            used_walls.add(edge)
            plan.wall_pieces.add(edge)
            return True

        # The switch: on a wall one tile from this room's door, the way people
        # actually fit them, or on any wall if the door is awkward.
        mine = [(x, y) for x, y in door_tiles if _room_at(plan, x, y) == idx]
        ranks = []
        for slot in slots:
            sx, sy, _facing_slot = slot
            if mine:
                best = abs(sx - mine[0][0]) + abs(sy - mine[0][1])
                for dx, dy in mine[1:]:
                    dist = abs(sx - dx) + abs(sy - dy)
                    if dist < best:
                        best = dist
            else:
                best = 0
            if facade[slot]:
                best += 4
            ranks.append(best)
        by_reach = [slots[i] for i in sorted(range(len(slots)), key=ranks.__getitem__)]
        # The game hangs one ceiling light off each switch, and that light
        # only reaches so far, so a sales floor or a warehouse lit by the one
        # switch beside its door was dark everywhere else - "lighting seems
        # somewhat broken". A big room gets a switch every so often, spread
        # out along its walls, the way a real shop is wired.
        want_switches = max(1, min(MAX_SWITCHES, r.area // LIGHT_EVERY_TILES))
        lit: list[tuple[int, int]] = []
        for x, y, facing in by_reach:
            if len(lit) >= want_switches:
                break
            if (x, y) in mine and _wall_edge(x, y, facing) in door_edges:
                continue
            if lit and min(abs(x - lx) + abs(y - ly) for lx, ly in lit) < SWITCHES_APART:
                continue
            if hang(SWITCH, x, y, facing):
                lit.append((x, y))
        if not lit:
            for x, y, facing in by_reach:
                if hang(SWITCH, x, y, facing):
                    break

        # Nothing else goes in the stair hall or corridor: a flat's front door
        # and the only way past the flight both run through it, and one
        # bookcase beside the stairs closes the corridor.
        if r.is_core:
            # Nothing standing, but the walls are fair game: Knox County's
            # halls carry 7 pieces per 10 m2 and ours carried none.
            for n, (x, y, facing) in enumerate(sorted(slots, key=facade.__getitem__)):
                if n % 3 == 0:
                    hang(("painting", "mirror", "painting")[n % 9 // 3], x, y, facing)
            continue
        keep_clear = door_tiles | stair_tiles
        # A shop's sales floor is fitted out as a whole - rows of shelving,
        # the till by the door - not from a list pushed against its walls.
        if r.kind in interiors.STORES and interiors.furnish_store(
                plan, idx, r, rng, door_tiles, stair_tiles, street):
            continue
        pal = palettes.get(r.unit)
        if pal is None:
            pal = palettes[r.unit] = interiors.palette(random.Random(f"{home_seed}:{r.unit}"))
        _, _, base = ROOM_STYLE[r.kind]
        occupied: set[tuple[int, int]] = set()
        office = r.kind == "office" and plan.kind not in HOUSE_LIKE_KINDS
        eatery = r.kind in interiors.DINING_ROOMS
        commercial_kitchen = r.kind in interiors.COMMERCIAL_KITCHENS
        if office:
            base = interiors.furnish_office(plan, idx, r, rng, occupied, keep_clear)
        elif eatery:
            base = interiors.furnish_dining(plan, idx, r, rng, occupied, stair_tiles,
                                            door_tiles, street)
        elif commercial_kitchen:
            base = interiors.KITCHEN_KIT.get(r.kind, interiors.DEFAULT_KITCHEN_KIT)
        elif r.kind == "bathroom" and plan.kind not in HOUSE_LIKE_KINDS | {"apartment"}:
            # A shop's or an office's toilet, not a family bathroom with a bath.
            base = ["toilet", "sink", "mirror", "toilet"]
        # The dining halo blocked the counter and the tables. It is not
        # clearance for the pieces hung afterwards.
        if plan.occ is not None:
            plan.occ &= np.uint8(~B_EXTRA & 0xFF)
        base = [pal.get(role, role) for role in base]
        if _erika_ready():
            # With Erika's Tiles installed, pictures and plants come from its
            # far larger range, so no two living rooms hang the same print.
            shop = r.kind in SHOP_DECOR_ROOMS
            base = [rng.choice(C.ERIKA_SHOP_ADS) if role == "painting" and shop and C.ERIKA_SHOP_ADS
                    else rng.choice(C.ERIKA_WALL_ART) if role in ("painting", "mirror") and C.ERIKA_WALL_ART
                    else rng.choice(C.ERIKA_PLANTS) if role == "plant" and C.ERIKA_PLANTS
                    else rng.choice(C.ERIKA_SHELVES)
                    if role == "bookshelf" and C.ERIKA_SHELVES and rng.random() < ERIKA_SHELF_SHARE
                    else role for role in base]
            if r.kind in VENDING_ROOMS and C.ERIKA_VENDING:
                base = base + [rng.choice(C.ERIKA_VENDING)]
        # Scale the wishlist with floor area, or a 12x9 living room ends up
        # with four items rattling around in it.
        # Knox County's rooms hold 8-13 pieces per 10 m2 of floor (counting
        # each tile a piece covers); one piece per 7 tiles gave 3-4. Only the
        # small things repeat: a big living room got a second sofa and
        # television, a big bathroom two baths.
        target = max(len(base), min(24, r.area // 4))
        if eatery or commercial_kitchen or r.kind == "theatre":
            target = len(base)       # fitted out; nothing more to scatter
        wishlist = []
        for i in range(target):
            role = base[i % len(base)]
            if i >= len(base) and _once(role):
                continue
            wishlist.append(role)
        # The middle first, with an aisle round it the wall pieces must leave
        # free; placed after them, it almost never found room.
        before = len(plan.furniture)
        if not office and not eatery:
            _furnish_middle(plan, idx, r, occupied, keep_clear, pal)
        # What the middle took is off the list: the living room's sofa, table
        # and television are the group there.
        for role, *_ in plan.furniture[before:]:
            if role in wishlist and _once(role):
                wishlist.remove(role)
        # A bed goes head to a wall between its bedside tables.
        if r.kind in ("bedroom", "kidsbedroom"):
            bed = next((role for role in wishlist if role in (pal.get("double_bed"), pal.get("bed"))
                        or role.startswith(("double_bed", "bed"))), None)
            if bed and interiors.bed_against_wall(plan, idx, r, slots, occupied, door_tiles,
                                                  bed, "sidetable"):
                wishlist.remove(bed)
                if "sidetable" in wishlist:
                    wishlist.remove("sidetable")
        floor_slots = [s for s in slots]
        rng.shuffle(floor_slots)
        for role in wishlist:
            if _is_wall_piece(role):
                for x, y, facing in sorted(floor_slots, key=facade.__getitem__):
                    if hang(role, x, y, facing):
                        break
                continue
            for (x, y, wanted) in floor_slots:
                if role in NORTH_WEST_ONLY and wanted in ("S", "E"):
                    continue
                orient = _facing(role, wanted)
                off = _offsets(role, orient)
                if not _piece_fits(plan, idx, x, y, off, B_DOOR | B_ITEM,
                                   occupied, door_tiles):
                    continue
                cells = [(x + dx, y + dy) for dx, dy in off]
                front = set()
                if _needs_front(role):
                    # A fridge, a stove or a wardrobe is opened from the tile
                    # in front of it; that tile stays floor.
                    fx, fy = {"N": (0, 1), "S": (0, -1), "W": (1, 0), "E": (-1, 0)}[wanted]
                    front = {(cx + fx, cy + fy) for cx, cy in cells} - set(cells)
                    if any(_busy(plan, c[0], c[1], B_ITEM, occupied)
                           or _room_at(plan, *c) != idx for c in front):
                        continue
                occupied.update(cells)
                occupied.update(front)
                _occ_mark(plan, cells, B_ITEM)
                _occ_mark(plan, front, B_ITEM)
                plan.furniture.append((role, x, y, orient))
                break

        if commercial_kitchen:
            # Steel counters, and no wall cupboards or microwave.
            _counter_runs(plan, idx, slots, occupied, door_tiles, stair_tiles,
                          "counter_2", cabinets=False)
        elif r.kind in KITCHENS:
            _counter_runs(plan, idx, slots, occupied, door_tiles, stair_tiles,
                          pal.get("counter", "counter"))
        _stand_on_something(plan, idx, r, pal, room_furniture_start)


# Pieces drawn at worktop height: a sink, a television, a table lamp, a pot
# plant. Put down on their own they float over bare floor - a sink plumbed into
# the ground - so each gets something to stand on first, the way the game's
# own houses have them. Found by the height of their sprites (a floating
# piece's lowest pixel sits well above the floor diamond); the television is
# drawn standing, but no home keeps one on the carpet.
SURFACE_ROLES = {"kitchen_sink", "sink", "lamp", "tv"}
WORKTOP_ROOMS = {"kitchen", "bathroom", "laundry", "breakroom"} | KITCHENS
# Opened from the front: the tile before them is kept clear.
FRONT_CLEAR_ROLES = {"fridge", "stove", "stove_alt", "washer", "dryer", "wardrobe",
                     "bookshelf", "dresser", "dresser_alt", "filing_cabinet"}
DOOR_CLEAR_DEPTH = 2


def _needs_front(role: str) -> bool:
    return role in FRONT_CLEAR_ROLES or role.startswith(("wardrobe", "dresser", "fridge"))


def _needs_surface(role: str) -> bool:
    return role in SURFACE_ROLES or role.startswith("erika_plant")


def _stand_on_something(plan: Plan, idx: int, room: Room, palette: dict,
                        start: int = 0) -> None:
    """A counter, a cabinet or a small table under every piece of this room
    that needs one and does not have one."""
    standing: set[tuple[int, int]] = set()
    mine = []
    for n in range(start, len(plan.furniture)):
        role, x, y, o = plan.furniture[n]
        if _room_at(plan, x, y) != idx or _is_wall_piece(role):
            continue
        mine.append(n)
        if not _needs_surface(role):
            standing.update(_cells_for(role, x, y, o))
    added = []
    for n in mine:
        role, x, y, o = plan.furniture[n]
        if not _needs_surface(role) or (x, y) in standing:
            continue
        if room.kind in WORKTOP_ROOMS or role.endswith("sink"):
            support = palette.get("counter", "counter")
        elif role == "tv":
            support = "dresser"
        else:
            support = "table"
        if support not in C.FURNITURE:
            continue
        added.append((n, (support, x, y, _facing(support, o))))
        standing.add((x, y))
    # Each support goes in just before its piece, so it is drawn underneath.
    for n, piece in sorted(added, reverse=True):
        plan.furniture.insert(n, piece)


def _counter_runs(plan: Plan, idx: int, slots, occupied: set,
                  door_tiles: set, stair_tiles: set, counter: str = "counter",
                  cabinets: bool = True) -> None:
    """Fitted counters along a kitchen's two longest walls.

    Knox County's kitchens are counters wall to wall with the sink and stove
    set into them - 12 pieces per 10 m2, against 5 when a kitchen took three
    counters from its wishlist and left the rest of the wall bare. Whatever the
    wishlist placed stays; the gaps along those walls fill with counters,
    except in front of a door.
    """
    by_wall: dict[str, list[tuple[int, int, str]]] = {}
    for x, y, facing in slots:
        by_wall.setdefault(facing, []).append((x, y, facing))
    for facing in sorted(by_wall, key=lambda f: -len(by_wall[f]))[:2]:
        for x, y, _f in by_wall[facing]:
            orient = _facing(counter, facing)
            cells = _cells_for(counter, x, y, orient)
            if _busy_any(plan, cells, B_DOOR | B_STAIR | B_ITEM,
                         occupied, door_tiles, stair_tiles):
                continue
            if any(_room_at(plan, cx, cy) != idx for cx, cy in cells):
                continue
            # A corner tile is on two walls; one counter is enough.
            occupied.update(cells)
            _occ_mark(plan, cells, B_ITEM)
            plan.furniture.append((counter, x, y, orient))

    # Cupboards on the wall above the counters, and a microwave on one - the
    # rest of what makes a vanilla kitchen full. North and west walls only, as
    # for everything fixed to a wall; the windows then keep off those tiles.
    if not cabinets:
        return
    microwave = False
    for role, x, y, orient in list(plan.furniture):
        if role != counter or orient not in ("N", "W") or _room_at(plan, x, y) != idx:
            continue
        edge = _wall_edge(x, y, orient)
        if edge in plan.wall_pieces or _room_at(plan, *((x, y - 1) if orient == "N" else (x - 1, y))) == idx:
            continue
        plan.furniture.append(("wall_cabinet", x, y, _facing("wall_cabinet", orient)))
        plan.wall_pieces.add(edge)
        if not microwave:
            plan.furniture.append(("microwave", x, y, _facing("microwave", orient)))
            microwave = True


@dataclass
class Building:
    """One or more storeys stacked into a single .tbx.

    A .tbx holds a list of <floor> elements and one building-wide room list; a
    floor's grid indexes into that shared list. So each storey is laid out as
    its own plan and the room lists are concatenated, with every storey's grid
    shifted by the rooms that came before it.
    """
    width: int
    height: int
    storeys: list[Plan] = field(default_factory=list)
    # Per storey below the top: where its staircase rises from.
    stairs: list[tuple[int, int, str]] = field(default_factory=list)

    @property
    def rooms(self) -> list[Room]:
        return [r for s in self.storeys for r in s.rooms]

    def grid_for(self, level: int) -> np.ndarray:
        """That storey's grid, renumbered into the shared room list."""
        offset = sum(len(s.rooms) for s in self.storeys[:level])
        g = np.asarray(self.storeys[level].grid)
        return np.where(g > 0, g + np.int32(offset), np.int32(0)).astype(np.int32, copy=False)


STAIR_RUN = 5      # tiles a staircase occupies, from Stairs::bounds
CORE_WIDE = 3      # the shaft is the flight plus a landing beside it


CORRIDOR_WIDE = 3   # a landing wide enough to be circulation, not a cupboard


def _pick_corridor(width: int, height: int, mask: np.ndarray | None,
                   _rng: random.Random) -> tuple[int, int, int, int] | None:
    """A corridor through the floor, with room for the stairs inside it.

    Flats need something to open onto, and it has to reach all of them: a
    compact shaft in the middle leaves the flats in the corners touching
    nothing. A strip along the building's length touches the flats either side
    of it by construction.

    The strip used to have to span the bounding box end to end. Placed on its
    real footprint, a block of flats turned 30 degrees has triangles of empty
    box at every corner, so no such strip ever fitted and the block fell back
    to one flat per floor. Now the longest strip that lies wholly inside the
    footprint is taken, as long as it runs most of the building's length;
    flats beyond its ends are folded into neighbours that do reach it.
    """
    # `rng` is unused. The strip is the best score, not a random one, and a
    # draw here would shift every later storey.
    along_y = height >= width
    span = height if along_y else width
    across = width if along_y else height
    if span < STAIR_RUN + 2 or across < CORRIDOR_WIDE + 2 * MIN_ROOM:
        return None
    if mask is None:
        mask_arr = np.ones((height, width), dtype=bool)
    else:
        mask_arr = _as_mask(mask)
    table = grids.sat(mask_arr)
    if along_y:
        extent = np.flatnonzero(mask_arr.any(axis=1))
        ys, xs = grids.window_full(table, 1, CORRIDOR_WIDE)
    else:
        extent = np.flatnonzero(mask_arr.any(axis=0))
        ys, xs = grids.window_full(table, CORRIDOR_WIDE, 1)
    if extent.size == 0:
        return None
    needed = max(STAIR_RUN + 2, int(0.6 * (int(extent[-1]) - int(extent[0]) + 1)))

    def consider(best, positions: np.ndarray, start: int):
        if positions.size == 0:
            return best
        cuts = np.flatnonzero(np.diff(positions) != 1) + 1
        starts_at = np.concatenate((np.zeros(1, np.int32), cuts))
        ends_at = np.concatenate((cuts, np.array([positions.size], np.int32)))
        for s, e in zip(starts_at.tolist(), ends_at.tolist()):
            length = e - s
            if length < needed:
                continue
            run_start = int(positions[s])
            # `a` is the first tile past the run, as the old scan left it.
            a = run_start + length
            centre = abs((start + CORRIDOR_WIDE / 2) - across / 2)
            score = (-length, centre)
            if best is not None and score >= best[0]:
                continue
            if along_y:
                rect = (start, run_start, start + CORRIDOR_WIDE - 1, a - 1)
            else:
                rect = (run_start, start, a - 1, start + CORRIDOR_WIDE - 1)
            best = (score, rect)
        return best

    best = None
    last = across - CORRIDOR_WIDE - MIN_ROOM
    for start in range(MIN_ROOM, last + 1):
        if along_y:
            positions = ys[xs == start]
        else:
            positions = xs[ys == start]
        best = consider(best, positions, start)
    return best[1] if best else None


def _pick_core(width: int, height: int, mask: np.ndarray | None,
               rng: random.Random, street: str | None = None
               ) -> tuple[int, int, int, int] | None:
    """Reserve a stair shaft: the same rectangle on every storey.

    Returned as (x0, y0, x1, y1) inclusive, oriented either way round, and
    always wholly inside the footprint - a shaft half outside an L-shaped
    building would put the flight in the garden.

    With the street known the stairs go at the back against a side wall,
    where a shop or a restaurant keeps its stairs; in the middle of the floor,
    a terrace of narrow shops had a flight of stairs in the middle of every one.
    """
    shapes = [(CORE_WIDE, STAIR_RUN + 1), (STAIR_RUN + 1, CORE_WIDE)]
    table = None if mask is None else grids.sat(_as_mask(mask))
    scores: list[np.ndarray] = []
    rects: list[np.ndarray] = []
    for cw, ch in shapes:
        if cw > width or ch > height:
            continue
        if table is None:
            yy, xx = np.meshgrid(
                np.arange(height - ch + 1, dtype=np.int32),
                np.arange(width - cw + 1, dtype=np.int32),
                indexing="ij")
            ys, xs = yy.ravel(), xx.ravel()
        else:
            ys, xs = grids.window_full(table, ch, cw)
        if ys.size == 0:
            continue
        ys_f = ys.astype(np.float64)
        xs_f = xs.astype(np.float64)
        if street in STEP_OF:
            # Back and side: distance from the wall opposite the street,
            # then from the nearer side wall.
            if street == "S":
                back = ys_f
            elif street == "N":
                back = height - (ys_f + ch)
            elif street == "E":
                back = xs_f
            else:
                back = width - (xs_f + cw)
            if street in ("N", "S"):
                side = np.minimum(xs_f, width - (xs_f + cw))
            else:
                side = np.minimum(ys_f, height - (ys_f + ch))
            score = back * 2 + side
        else:
            # Central, so the flight is not jammed against the windows.
            score = (np.abs((xs_f + cw / 2) - width / 2)
                     + np.abs((ys_f + ch / 2) - height / 2))
        scores.append(np.asarray(score, dtype=np.float64))
        rects.append(np.stack((xs, ys, xs + cw - 1, ys + ch - 1), axis=1))
    if not scores:
        return None
    all_scores = np.concatenate(scores)
    all_rects = np.concatenate(rects)
    best_score = float(all_scores.min())
    # The same band the old running comparison kept, in row-major order, so
    # the one rng.choice still sees the ties in the order it used to.
    tied = np.flatnonzero(np.abs(all_scores - best_score) < 1e-9)
    choices = [tuple(int(v) for v in all_rects[i]) for i in tied.tolist()]
    return rng.choice(choices)


STEP_OF = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}


# Buildings this tall get a lift. Four flights is a fair climb; a thirty-storey
# tower on stairs alone is not something anyone built.
ELEVATOR_FROM_LEVELS = 5
# ...if it is more than a townhouse: a lift in a building five tiles wide
# took half its ground floor.
ELEVATOR_MIN_SIDE = 10
SHAFT_SIZE = 2


def _pick_shaft(core: tuple[int, int, int, int], width: int, height: int,
                mask: list[list[bool]] | None, stairs: tuple[int, int, str] | None
                ) -> tuple[tuple[int, int, int, int], tuple[int, int, str]] | None:
    """A 2x2 elevator shaft beside the stair hall, and the wall its doors go in.

    The Elevators mod finds a lift by its door tiles - vanilla
    fixtures_escalators_01_48-51 - repeated at the same square on every floor
    it serves, with a small sealed box behind them. So the shaft is fixed once
    for the whole building, like the stairs, and sits against the long side of
    the stair hall so its doors open onto the landing on every storey.
    """
    cx0, cy0, cx1, cy1 = core
    s = SHAFT_SIZE
    stair_cells = set()
    if stairs is not None:
        sx, sy, d = stairs
        stair_cells = {(sx, sy + i) if d == "N" else (sx + i, sy) for i in range(STAIR_RUN)}

    def inside(x0, y0):
        if x0 < 0 or y0 < 0 or x0 + s > width or y0 + s > height:
            return False
        return mask is None or bool(np.asarray(mask)[y0:y0 + s, x0:x0 + s].all())

    options = []
    if (cy1 - cy0) >= (cx1 - cx0):
        # Hall runs north-south: shafts to its west or east, doors in a W wall.
        mid = (cy0 + cy1) / 2
        for y0 in range(cy0, cy1 - s + 2):
            landing = [(cx0, y0 + i) for i in range(s)] + [(cx1, y0 + i) for i in range(s)]
            for x0, door_x, side_col in ((cx0 - s, cx0, cx0), (cx1 + 1, cx1 + 1, cx1)):
                if not inside(x0, y0):
                    continue
                if any((side_col, y0 + i) in stair_cells for i in range(s)):
                    continue
                options.append((abs(y0 + s / 2 - 1 - mid), (x0, y0, x0 + s - 1, y0 + s - 1),
                                (door_x, y0, "W")))
    else:
        mid = (cx0 + cx1) / 2
        for x0 in range(cx0, cx1 - s + 2):
            for y0, door_y, side_row in ((cy0 - s, cy0, cy0), (cy1 + 1, cy1 + 1, cy1)):
                if not inside(x0, y0):
                    continue
                if any((x0 + i, side_row) in stair_cells for i in range(s)):
                    continue
                options.append((abs(x0 + s / 2 - 1 - mid), (x0, y0, x0 + s - 1, y0 + s - 1),
                                (x0, door_y, "N")))
    if not options:
        return None
    options.sort(key=lambda o: (o[0], o[1]))
    return options[0][1], options[0][2]


def _carve_shaft(plan: Plan, shaft: tuple[int, int, int, int],
                 door: tuple[int, int, str]) -> None:
    """Paint the elevator shaft over the storey as a sealed room of its own."""
    x0, y0, x1, y1 = shaft
    plan.rooms.append(Room(x0, y0, x1, y1, kind="elevator", unit=0, is_shaft=True))
    plan.grid[y0:y1 + 1, x0:x1 + 1] = len(plan.rooms)
    plan.shaft = shaft
    plan.shaft_door = door
    _invalidate(plan)
    _renumber(plan)
    _mend_fragments(plan)
    _refit(plan)


def _stairs_in_core(core: tuple[int, int, int, int]
                    ) -> tuple[int, int, str]:
    """The flight inside a shaft. Runs along the shaft's long axis.

    Set one tile in from the end so the corridor is still walkable past it.
    """
    x0, y0, x1, y1 = core
    if (y1 - y0) >= (x1 - x0):
        start = min(y0 + 1, y1 - (STAIR_RUN - 1))
        return x0 + (x1 - x0) // 2, max(y0, start), "N"
    start = min(x0 + 1, x1 - (STAIR_RUN - 1))
    return max(x0, start), y0 + (y1 - y0) // 2, "W"


def _clear_for_stairs(plan: Plan, x: int, y: int, d: str) -> None:
    """Drop furniture standing where the staircase goes."""
    dx, dy = (0, 1) if d == "N" else (1, 0)
    blocked = {(x + dx * i, y + dy * i) for i in range(STAIR_RUN)}
    plan.furniture = [
        f for f in plan.furniture
        if _is_wall_piece(f[0])
        or not (set(_cells_for(f[0], f[1], f[2], f[3])) & blocked)
    ]


def build_building(width: int, height: int, levels: int = 1,
                   commercial: bool = False, seed: int = 0,
                   kind: str | None = None,
                   mask: list[list[bool]] | None = None,
                   settings: Settings | None = None,
                   street: str | None = None,
                   retail: bool = False,
                   uses: list[tuple[str, str]] | None = None,
                   hotel: bool = False,
                   party: dict | None = None,
                   should_stop=None) -> Building:
    """Lay out a building of `levels` storeys.

    `retail` puts shops on the ground floor of a block of flats, as on any
    city street; towers step back above SETBACK_FROM_LEVELS. `uses` are what
    OpenStreetMap says the ground floor is (knoxbuild/uses.py), as (front
    room, back room); `hotel` makes a block of flats' flats hotel rooms.

    Each storey is laid out separately rather than copied, because a block of
    flats whose every floor is identical reads as a rendering error; only the
    staircase has to line up, and it does because the shaft is reserved first.
    """
    levels = max(1, levels)
    mask = _as_mask(mask)
    rng = random.Random(seed ^ 0x5745)

    # A block of flats gets a corridor whether or not it is tall enough to
    # need stairs, because the flats have to open onto something. Everything
    # else only needs a shaft, and only once there is a floor above.
    core = None
    corridor = False
    if kind == "apartment":
        core = _pick_corridor(width, height, mask, rng)
        corridor = core is not None
    if core is None and levels > 1:
        core = _pick_core(width, height, mask, rng, street)
    if levels > 1 and core is None:
        levels = 1          # nowhere to put a staircase, so one floor it is

    stairs = _stairs_in_core(core) if core is not None and levels > 1 else None
    shaft = shaft_door = None
    if core is not None and levels >= ELEVATOR_FROM_LEVELS and min(width, height) >= ELEVATOR_MIN_SIDE:
        found = _pick_shaft(core, width, height, mask, stairs)
        if found:
            shaft, shaft_door = found
    upper_mask, setback_at = _setback(width, height, mask, levels, core, shaft,
                                      corridor)
    # Shops under flats where the town is dense, and wherever the map puts a
    # shop, a restaurant or a bank in a building that is more than one.
    shops = kind in ("apartment", "civic") and core is not None and (
        (retail and kind == "apartment" and levels >= 3) or (bool(uses) and levels >= 2))
    storeys = []
    for lvl in range(levels):
        # Between storeys, so Stop is not stuck inside one tall building.
        if should_stop is not None and should_stop():
            raise knoxstop.Stopped("the buildings")
        storeys.append(
            build_plan(width, height, commercial=commercial or (shops and lvl == 0),
                       seed=seed + 977 * lvl,
                       kind="retail" if shops and lvl == 0 else kind,
                       mask=upper_mask if setback_at and lvl >= setback_at else mask,
                       ground=(lvl == 0), settings=settings,
                       core=core, level=lvl, levels=levels, stairs=stairs,
                       corridor=corridor, shaft=shaft, shaft_door=shaft_door,
                       street=street, uses=uses, hotel=hotel,
                       party={e for e, up in (party or {}).items() if lvl < up}))
    building = Building(width=width, height=height, storeys=storeys)
    if stairs is not None:
        for lvl in range(levels - 1):
            building.stairs.append(stairs)
            _clear_for_stairs(storeys[lvl], *stairs)
            _clear_for_stairs(storeys[lvl + 1], *stairs)
    # Windows last and for the whole building at once, so they stack in
    # columns instead of each floor scattering its own.
    _place_windows(building, kind, shop_ground=shops)
    return building


# Towers step back: from this many storeys the top part is set in from the
# edges, the way tall buildings are built for light and wind. A 30-storey
# block that went straight up from its footprint was a featureless box.
SETBACK_FROM_LEVELS = 12
SETBACK_SHARE = 0.65          # the step comes this far up
SETBACK_TILES = 2
SETBACK_MIN_KEEP = 0.5        # the upper part keeps at least this much floor


def _setback(width: int, height: int, mask, levels: int, core, shaft,
             corridor: bool):
    """(the upper storeys' mask, the storey the step is at) or (None, 0)."""
    if levels < SETBACK_FROM_LEVELS or core is None:
        return None, 0
    base = np.ones((height, width), dtype=bool) if mask is None else _as_mask(mask)
    # A block of flats has a corridor end to end: it steps in along its long
    # sides only, so the corridor still reaches both ends. The erosion
    # footprint is that step: a square, or a line when only one axis moves.
    x0, y0, x1, y1 = core
    along_x = corridor and (x1 - x0) >= (y1 - y0)
    along_y = corridor and not along_x
    if along_x:
        structure = np.ones((2 * SETBACK_TILES + 1, 1), dtype=bool)
    elif along_y:
        structure = np.ones((1, 2 * SETBACK_TILES + 1), dtype=bool)
    else:
        structure = None
    upper = grids.erode(base, structure)
    for rx0, ry0, rx1, ry1 in [core] + ([shaft] if shaft else []):
        if not bool(upper[ry0:ry1 + 1, rx0:rx1 + 1].all()):
            return None, 0
    if int(upper.sum()) < SETBACK_MIN_KEEP * int(base.sum()):
        return None, 0
    return upper, round(levels * SETBACK_SHARE)


def _shop_doors(plan: "Plan", street: str | None) -> None:
    """A street door into each shop on a block of flats' ground floor."""
    for idx, room in enumerate(plan.rooms, 1):
        if room.is_core or room.is_shaft or room.kind not in RETAIL_ROOMS | FRONT_ROOMS:
            continue
        runs = [(side, wall) for side, wall in _outside_runs(plan, idx) if len(wall) >= 3]
        if not runs:
            continue
        side, wall = max(runs, key=lambda r: (r[0] == street, len(r[1])))
        door = wall[len(wall) // 2]
        if door not in plan.doors:
            plan.doors.append(door)


def _stair_foot(stairs: tuple[int, int, str] | None) -> tuple[int, int] | None:
    if stairs is None:
        return None
    x, y, d = stairs
    return (x, y + STAIR_RUN - 1) if d == "N" else (x + STAIR_RUN - 1, y)


# Rooms scale with what the building is: a warehouse is a few great halls, a
# church one nave, a school rooms the size of classrooms.
KIND_ROOM_SCALE = {"industrial": 6.0, "barn": 5.0, "shed": 8.0, "church": 4.0,
                   "shop": 2.0, "school": 1.6, "civic": 1.5,
                   "restaurant": 1.5, "medical": 1.3, "offices": 3.0,
                   "police": 2.0, "library": 2.5, "fire": 3.0,
                   "military": 2.0}
# A shop's ground floor is a sales floor or a few: rooms the size of a house's
# cut a grocery into cupboards no rows of shelving fit in.
SHOP_FLOOR_SCALE = 4.0
MAX_ROOMS_PER_FLOOR = 90
# No room bigger than this, however big the building.
#
# Loot is capped per room, not per container: every entry in the game's
# Distributions.lua carries a max, which is how many containers in one room
# may be filled from it (RoomDef.proceduralSpawnedContainer counts them), and
# most of the household and office ones are 1, 2 or 4. So a room with twenty
# cabinets in it has loot in the first few and nothing in the rest - "if a
# building is too big, it just stops spawning loot in containers at some
# point". More rooms means more allowances, so no room is bigger than this
# whatever the building is, and the depth cap below has to be loose enough to
# reach it: a 200x200 building needs eleven cuts, and at eight it stopped
# with rooms of two thousand tiles.
MAX_ROOM_AREA = 120


def build_plan(width: int, height: int, commercial: bool = False,
               seed: int = 0, kind: str | None = None,
               mask: list[list[bool]] | None = None,
               ground: bool = True,
               settings: Settings | None = None,
               core: tuple[int, int, int, int] | None = None,
               level: int = 0, levels: int = 1,
               stairs: tuple[int, int, str] | None = None,
               corridor: bool = False,
               shaft: tuple[int, int, int, int] | None = None,
               shaft_door: tuple[int, int, str] | None = None,
               street: str | None = None,
               uses: list[tuple[str, str]] | None = None,
               hotel: bool = False,
               party: set | None = None) -> Plan:
    """Lay out and furnish one storey of the given tile size.

    `uses` are what OpenStreetMap says a commercial ground floor holds
    (build_building); `hotel` turns flats into hotel rooms.

    `ground` gates the exterior door: a door in an upper-floor wall opens onto
    a five-metre drop, and the game will happily let a zombie walk through it.
    """
    settings = settings or Settings()
    mask = _as_mask(mask)
    rng = random.Random(seed)
    plan = Plan(width=width, height=height, mask=mask, core=core,
                corridor=corridor, kind=kind, party=set(party or ()))
    shop_floor = kind in ("shop", "retail", "restaurant") and ground
    mix_kind = "offices" if kind in ("shop", "restaurant") and not ground else kind
    # Rooms in a flat are smaller than rooms in a house, and there have to be
    # enough of them per floor to make several dwellings out of.
    target = settings.room_size * KIND_ROOM_SCALE.get(mix_kind or "", 1.0)
    if shop_floor:
        target = settings.room_size * SHOP_FLOOR_SCALE
    if kind == "apartment":
        target = max(16, round(settings.room_size * 0.4))
    # A factory floor 160 tiles across split into house-sized rooms would be
    # hundreds of cupboards. However big the building, keep it to a number of
    # rooms a person could walk through.
    floor_tiles = int(mask.sum()) if mask is not None else width * height
    target = min(max(target, floor_tiles / MAX_ROOMS_PER_FLOOR), MAX_ROOM_AREA)

    if kind == "apartment":
        _apartment_rooms(plan, rng, target, HOTEL_FRONTAGE if hotel else FLAT_FRONTAGE)
    elif shop_floor and kind in ("shop", "restaurant"):
        _shop_rooms(plan, rng, street)
    else:
        _split(0, 0, width - 1, height - 1, rng, MAX_DEPTH, plan.rooms,
               target_area=target, mask=mask)
    _paint(plan)

    # Kinds are chosen after painting, from the plan as it really is: what a
    # room is depends on what it borders, and that is only known once the
    # footprint and the shaft have had their say.
    if kind == "apartment":
        _unit_touches_corridor(plan)
        _assign_flat_kinds(plan, hotel=hotel)
    elif shop_floor:
        if kind == "restaurant" and not uses:
            uses = [("restaurantdining", "restaurantkitchen")]
        _assign_shop_floor(plan, rng, street, uses, several=(kind == "retail"))
    elif mix_kind and mix_kind in SPECIAL_MIXES:
        mix, fill = SPECIAL_MIXES[mix_kind]
        _assign_kinds([r for r in plan.rooms if not r.is_core], mix, fill)
    elif commercial:
        _assign_kinds([r for r in plan.rooms if not r.is_core],
                      COMMERCIAL, COMMERCIAL_FILL)
    else:
        _assign_house_kinds(plan, level, levels)
    for room in plan.rooms:
        if room.is_core:
            room.kind = "hall"
    if shaft is not None:
        _carve_shaft(plan, shaft, shaft_door)

    _doors(plan, rng)
    if ground:
        _exterior_door(plan, rng, avoid=_stair_foot(stairs), street=street)
        if kind in (None, "house"):
            _back_door(plan, street)
        if kind == "retail":
            # Each shop its own street door, before the shop is fitted out
            # round it.
            _shop_doors(plan, street)
    _furnish(plan, rng, stairs, street)
    return plan


def _largest_rectangle(todo) -> tuple[int, int, int, int] | None:
    """Biggest all-True rectangle as (x0, y0, width, height), by histogram."""
    todo = np.asarray(todo, dtype=bool)
    h, w = todo.shape
    if h == 0 or w == 0:
        return None
    # One conversion, then the histogram stays on Python ints. Building a new
    # height array and copying it out on every row cost more than the scan.
    rows = todo.tolist()
    heights = [0] * (w + 1)
    best = None
    best_area = 0
    for y in range(h):
        row = rows[y]
        for x in range(w):
            heights[x] = heights[x] + 1 if row[x] else 0
        stack: list[int] = []
        for x in range(w + 1):
            cur = heights[x]
            while stack and heights[stack[-1]] >= cur:
                top = stack.pop()
                left = stack[-1] + 1 if stack else 0
                hh = heights[top]
                area = hh * (x - left)
                if area > best_area:
                    best_area = area
                    best = (left, y - hh + 1, x - left, hh)
            stack.append(x)
    return best


def roof_rects(grid) -> list[tuple[int, int, int, int, dict]]:
    """Flat roofs covering exactly a storey's footprint.

    A BuildingEd roof is a rectangle, and every building here used to get one
    the size of its bounding box. On an L-shaped or notched footprint that
    roof reaches out over the yard - up to 43% of it over nothing on the
    buildings measured. The footprint is covered instead by the largest
    rectangles that fit, biggest first, which keeps the count low on the
    common shapes: an L takes two roofs, a notched block three.

    Each side is capped - drawn with a rim - only where it is the edge of the
    building. Where one roof meets another the rim would draw a ridge across
    the middle of a flat roof, so that side is left open.
    """
    grid = np.asarray(grid)
    if grid.ndim != 2 or grid.shape[0] == 0 or grid.shape[1] == 0:
        return []
    inside = grid != 0
    todo = inside.copy()
    h, w = inside.shape
    rects = []
    while True:
        occupied_rows = np.flatnonzero(np.any(todo, axis=1))
        occupied_cols = np.flatnonzero(np.any(todo, axis=0))
        if occupied_rows.size == 0 or occupied_cols.size == 0:
            break
        crop_y0, crop_y1 = int(occupied_rows[0]), int(occupied_rows[-1]) + 1
        crop_x0, crop_x1 = int(occupied_cols[0]), int(occupied_cols[-1]) + 1
        remaining = todo[crop_y0:crop_y1, crop_x0:crop_x1]
        if bool(np.all(remaining)):
            # Common after taking the main body of an L or notched footprint.
            found = (0, 0, crop_x1 - crop_x0, crop_y1 - crop_y0)
        else:
            found = _largest_rectangle(remaining)
        if found is None:
            break
        local_x, local_y, rw, rh = found
        x0, y0 = crop_x0 + local_x, crop_y0 + local_y
        if rw <= 0 or rh <= 0:
            break
        todo[y0:y0 + rh, x0:x0 + rw] = False

        def open_side(xs, ys) -> bool:
            xs = np.asarray(xs, dtype=np.int32)
            ys = np.asarray(ys, dtype=np.int32)
            ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
            if not bool(np.all(ok)):
                return False
            return bool(np.all(inside[ys, xs]))

        caps = {
            "cappedW": not open_side(np.full(rh, x0 - 1, np.int32),
                                     np.arange(y0, y0 + rh)),
            "cappedE": not open_side(np.full(rh, x0 + rw, np.int32),
                                     np.arange(y0, y0 + rh)),
            "cappedN": not open_side(np.arange(x0, x0 + rw),
                                     np.full(rw, y0 - 1, np.int32)),
            "cappedS": not open_side(np.arange(x0, x0 + rw),
                                     np.full(rw, y0 + rh, np.int32)),
        }
        rects.append((x0, y0, rw, rh, caps))
    return rects
