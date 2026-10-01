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
from collections import deque
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
    # Its kind is already decided and the mix must not overwrite it: the
    # mall concourse, carved before the units are cut out around it.
    fixed: bool = False
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
    # Squares deliberately left open to the storey below: the hole in an
    # upper floor of a mall, so the concourse is one space several storeys
    # tall and you can see the ground floor from the gallery. They belong to
    # no room, which is how BuildingEd leaves a square with no floor, and
    # tbx.py counts them as built-over so the storey below is not roofed.
    void: set = field(default_factory=set)
    # The stair shaft, as (x0, y0, x1, y1) inclusive, identical on every
    # storey of a building. Painted last so it is always exactly one room.
    core: tuple[int, int, int, int] | None = None
    # True when the core is a corridor flats open onto, not just a stair shaft.
    corridor: bool = False
    # The elevator shaft (x0, y0, x1, y1) inclusive, and where its doors hang:
    # (x, y, "W" or "N") for the two-square wall edge facing the stair hall.
    shaft: tuple[int, int, int, int] | None = None
    shaft_door: tuple[int, int, str] | None = None
    # Flats laid out open plan: one room for the kitchen and the living room
    # together. Decided when the flat is cut, not after - a flat already
    # divided into four equal rooms cannot be made open by renaming one of
    # them, and at 16 m2 a room has no space for a sofa and a television.
    open_units: set = field(default_factory=set)
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
    # Nothing here that wants a wall: the counters fill whatever wall the
    # wishlist leaves, and Knox County's kitchens carry as much counter as
    # ours do. What ours were short of - a table, chairs, a mat - stands in
    # the middle, so it is in the centre group rather than on this list.
    # The washing machine is on its own list: Knox County keeps laundry out of
    # four kitchens in five, and having it in the wishlist proper put one in
    # 80% of ours. See LAUNDRY_IN_KITCHEN.
    "kitchen": (C.FLOOR_TILE_CHECK, "Kitchen",
                ["fridge", "stove", "kitchen_sink", "counter", "counter",
                 "shelf", "plant"]),
    # One room that is kitchen and living room both, for a flat with no wall
    # between them. The cooking end comes first so it takes the wall it needs
    # before the sofa and the television are placed.
    # The cooking end first, so it takes the wall it needs before the rest.
    # The sofa is on both this list and the centre group: these rooms come out
    # as strips about four tiles deep, and a sofa with a walkway round it does
    # not fit in four tiles - against the wall is what a flat that size really
    # has, and the centre group takes over in the ones with room for it.
    "openplan": (C.FLOOR_WOOD, "Living Room",
                 ["fridge", "stove", "kitchen_sink", "counter", "counter",
                  "sofa", "tv", "shelf", "plant", "painting"]),
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
    # A school canteen: the hall the food is eaten in, with the kitchen that
    # serves it next door. The tables are the centre group below.
    "diningroom": (C.FLOOR_TILE_CHECK, "Canteen",
                   ["counter", "vending", "shelf", "plant", "painting",
                    "water_cooler"]),
    "schoollab": (C.FLOOR_TILE_PALE, "Laboratory",
                  ["counter", "counter", "sink", "table", "shelf",
                   "chair", "stove", "chair"]),
    "schoolstorage": (C.FLOOR_LINO, "School Storage",
                      ["shelf", "crate", "metal_rack", "crate", "bookshelf"]),
    "sportstorage": (C.FLOOR_LINO, "Sports Store",
                     ["metal_rack", "crate", "shelf", "crate"]),
    "janitor": (C.FLOOR_LINO, "Janitor",
                ["shelf", "crate", "sink", "metal_rack"]),
    "security": (C.FLOOR_LINO, "Security",
                 ["desk", "office_chair", "corkboard", "filing_cabinet",
                  "shelf"]),
    "officestorage": (C.FLOOR_LINO, "Office Storage",
                      ["shelf", "filing_cabinet", "crate", "metal_rack"]),
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
    # A concourse is walked down, not furnished. Knox County's has 16 pieces
    # of furniture in 5,855 tiles - 0.03 per 10 - and what fills it instead is
    # the shopfronts along its edges (location_shop_mall_01). A planter and a
    # bin here and there is the whole of it; anything more and the hundreds of
    # people it is built for cannot get past.
    "concourse": (C.FLOOR_TILE_PALE, "Concourse",
                  ["plant", "shop_bin", "vending"]),
    # The units Knox County's mall is let out to, and the storerooms behind
    # them. Fitted out by interiors.furnish_store like the shops above; these
    # wishlists are for a unit too small for rows of shelving.
    "clothesstore": (C.FLOOR_WOOD, "Clothes Store",
                     ["clothes_rack", "shop_shelf_wood", "mirror", "shop_counter", "mannequin"]),
    "shoestore": (C.FLOOR_WOOD, "Shoe Store",
                  ["shop_shelf_wood", "shop_display", "mirror", "shop_counter"]),
    "sewingstore": (C.FLOOR_WOOD, "Sewing Store",
                    ["shop_shelf_wood", "shop_display", "shop_counter"]),
    "electronicsstore": (C.FLOOR_TILE_PALE, "Electronics Store",
                         ["shop_shelf", "shop_case", "shop_counter"]),
    "housewarestore": (C.FLOOR_TILE_CHECK, "Houseware Store",
                       ["shop_shelf_wood", "dresser", "shop_counter"]),
    "cornerstore": (C.FLOOR_TILE_PALE, "Corner Store",
                    ["shop_shelf_red", "shop_fridge", "shop_counter_red"]),
    "optometrist": (C.FLOOR_TILE_PALE, "Optometrist",
                    ["shop_case", "shop_counter", "mirror", "chair"]),
    "dressingrooms": (C.FLOOR_WOOD, "Dressing Rooms", ["mirror", "chair"]),
    "foodcourt": (C.FLOOR_TILE_CHECK, "Food Court",
                  ["table", "chair", "chair", "chair", "bin", "plant"]),
    "clothesstorage": (C.FLOOR_LINO, "Clothes Storage",
                       ["metal_rack", "crate", "shelf", "clothes_rack"]),
    "departmentstorage": (C.FLOOR_LINO, "Department Storage",
                          ["metal_rack", "crate", "crate", "shelf"]),
    "giftstorage": (C.FLOOR_LINO, "Gift Storage", ["shelf", "crate", "metal_rack"]),
    "toystorage": (C.FLOOR_LINO, "Toy Storage", ["shelf", "crate", "metal_rack"]),
    "bookstorage": (C.FLOOR_LINO, "Book Storage", ["shelf", "crate", "bookshelf"]),
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
                         ["gun_locker", "gun_locker", "metal_rack", "crate",
                          "shelf"]),
    # The armoury proper: racks and lockers, no desks. A separate room from
    # the gun store, which is where the ammunition and the spares live. Both
    # names are the game's own - armory is in 2 of its buildings and
    # policegunstorage in 15 - so both are looted.
    "armory": (C.FLOOR_LINO, "Armory",
               ["gun_locker", "gun_locker", "metal_rack", "gun_locker",
                "crate", "corkboard"]),
    # Knox County calls this lockerroom in 99 buildings and policelocker in
    # 27; both are real, and a station has both a changing room and its own.
    "lockerroom": (C.FLOOR_LINO, "Locker Room",
                   ["wardrobe", "wardrobe", "wardrobe_pale", "chair", "shelf",
                    "mirror"]),
    "evidenceroom": (C.FLOOR_LINO, "Evidence Room",
                     ["metal_rack", "shelf", "crate", "filing_cabinet",
                      "corkboard"]),
    "policearchive": (C.FLOOR_LINO, "Police Archive",
                      ["filing_cabinet", "filing_cabinet", "shelf", "desk", "office_chair"]),
    "interrogationroom": (C.FLOOR_LINO, "Interrogation Room",
                          ["table", "chair", "chair", "mirror"]),
    # Knox County has 540 prisoncells and no "cells" at all - the name is
    # what the loot tables key off, so ours filled with nothing.
    "prisoncells": (C.FLOOR_LINO, "Cells", ["bed", "toilet", "sink"]),
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
    # Desks, not a table and two chairs. Knox County's classrooms are 2.97
    # location_community_school_01_32-35 is the commonest thing in a vanilla
    # classroom and stands against a wall in 93-95% of sightings, which is
    # why it was taken for a desk and called school_desk. It is a wall clock.
    # Adding it put 765 of them in one school and is what "too many clocks"
    # was. Whatever the classroom desk is in that tileset, it is not those
    # four; until it is identified a classroom is tables and chairs.
    # No corkboard either: Knox County's school rooms carry none at all -
    # location_business_office_generic_01_7/15 appears zero times in 13,243
    # tiles of them - where ours had 0.24 per 10 m2.
    "classroom": (C.FLOOR_TILE_PALE, "Classroom",
                  ["table", "chair", "chair", "desk", "table", "chair",
                   "bookshelf", "chair", "shelf"]),
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
# Houses do not read these: _assign_house_kinds places their rooms by what
# each one sits next to. They are kept because the selftest checks every name
# we can emit is one the game knows.
RESIDENTIAL_FILL = ["closet", "bedroom", "storage", "bedroom",
                    "livingroom", "laundry", "bathroom", "office"]
COMMERCIAL_FILL = ["storage", "office", "storage", "bathroom"]

# Room plans for buildings OSM identifies as something specific. A school full
# of bedrooms reads as wrong immediately; these keep the interior in character,
# and the room names drive what loot spawns there.
SPECIAL_MIXES = {
    # The canteen is a kitchen serving a dining hall, which is what a school
    # is missing without it. schoollab, schoolstorage, sportstorage and
    # janitor are the game's own school rooms.
    # Mostly classrooms. Enlarging the rooms instead dropped them from 6 a
    # floor to 3 and filled the space with offices, because the mix covers
    # more of a floor when there are fewer rooms on it.
    "school":     (["lobby", "classroom", "classroom", "diningroom",
                    "classroom", "gym", "kitchen",
                    "library", "office", "classroom", "schoollab",
                    "bathroom", "schoolstorage", "sportstorage"],
                   ["classroom", "classroom", "classroom", "schoolstorage",
                    "classroom", "office", "janitor", "bathroom"]),
    "church":     (["church", "lobby", "office", "storage", "bathroom"],
                   ["church", "church", "church", "storage"]),
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
    "military":   (["armystorage", "office", "bedroom", "armystorage",
                    "bathroom", "kitchen", "bedroom"],
                   ["bedroom", "bedroom", "armystorage", "storage", "office"]),
    "shed":       (["shed"], ["shed"]),
    # A shopping centre, measured off Muldraugh 54_22: a third of the floor
    # is concourse, and the rest is units of 150-400 tiles - clothes,
    # department, furniture, gift, toy, book and sports shops - with a food
    # court, lavatories, a janitor's and the centre office behind. Every one
    # of these is a room interiors.py can fit out as a shop.
    # No "hall" in either list: the concourse is cut before the units
    # (_mall_rooms) and dealing more halls on top of it took the mall to 53%
    # circulation against the game's 33.6%.
    "mall":       (["foodcourt", "clothesstore", "departmentstore",
                    "sportstore", "bookstore", "pizzakitchen",
                    "giftstore", "toystore", "shoestore", "electronicsstore",
                    "furniturestore", "bathroom", "jewelrystore",
                    "housewarestore", "chinesekitchen", "gunstore",
                    "clothesstorage", "office", "toolstore", "musicstore",
                    "pharmacy", "candystore", "bakery", "icecreamkitchen",
                    "sewingstore", "optometrist", "janitor", "cornerstore",
                    "departmentstorage", "security", "storage", "breakroom"],
                   ["clothesstore", "giftstore", "clothesstorage",
                    "toystore", "bookstore", "shoestore", "sportstore",
                    "housewarestore", "storage", "electronicsstore",
                    "jewelrystore", "musicstore", "candystore"]),
    # A castle is a great hall with a chapel, a kitchen that fed everybody and
    # the rooms people slept in, and today a ticket desk and a gift counter by
    # the door. Every name here is one the game furnishes; "throne room" would
    # look right and spawn nothing, the loot tables keying off the name.
    "castle":     (["hall", "church", "kitchen", "storage", "bedroom",
                    "library", "office", "bathroom", "generalstore"],
                   ["hall", "bedroom", "storage", "hall"]),
    # A ground: the concourse under the stand, the changing rooms and showers
    # off it, the kit store, a first aid room and the club offices. The food
    # is a counter on the concourse, which is what cafe is.
    # lockerroom is not in the fill: it is one of the kinds _split_to_size
    # cuts down to size, so every one the fill handed out became a row of
    # eight and a big ground came out 57% changing rooms. Twice in the mix is
    # the home side and the away side, and that is where they stay.
    "stadium":    (["hall", "lockerroom", "bathroom", "sportstorage", "cafe",
                    "office", "clinic", "gym", "storage", "hall"],
                   ["hall", "cafe", "storage", "office", "sportstorage"]),
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
    # Knox County's police rooms by how many there are: prisoncells 540,
    # policeoffice 220, policelocker 27, policegunstorage 15. policestorage
    # has two in the whole county, so leaning on it filled a station with a
    # room the game has no loot for.
    # A police station, not a jail. Knox County's buildings that hold a
    # policeoffice are 31% cells because most of them are the county jail with
    # a station attached; a town station is a front desk, offices, an
    # interrogation room, somewhere to change, the armoury and a few cells at
    # the back. prisoncells is capped hard in ROOM_CAP_PER so "a few" stays a
    # few. Every name here is one the game furnishes: reception and
    # holdingcell are not, so the lobby and the cells carry their real names.
    "police":     (["lobby", "policeoffice", "hall", "interrogationroom",
                    "lockerroom", "policegunstorage", "armory", "prisoncells",
                    "evidenceroom", "bathroom", "security", "breakroom",
                    "policelocker", "policearchive"],
                   ["policeoffice", "hall", "policeoffice", "officestorage",
                    "prisoncells", "policeoffice", "janitor", "bathroom",
                    "policeoutfitstorage"]),
    # A library is its reading rooms, not an office block with one in it.
    "library":    (["library", "library", "lobby", "office", "bathroom",
                    "storage", "library"],
                   ["library", "library", "library", "storage", "office"]),
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


# A bathroom in Knox County is 6.5 m2. Ours were 13.7 - a bathroom the size of
# a bedroom - because a floor is cut to one target area and the bathroom then
# takes the smallest of the ordinary rooms, there being no small one. One
# region per house floor gives up a corner to a bathroom-sized room first.
# MIN_ROOM is 3, so 9 to 12 tiles is as near the game's as squares allow.
SMALL_ROOM_RUN = (MIN_ROOM, MIN_ROOM + 1)


def _inside(x0: int, y0: int, x1: int, y1: int,
            mask: list[list[bool]] | None) -> int:
    """Tiles of a rectangle that are really inside the building.

    On a footprint turned 38 degrees half of every bounding rectangle is empty
    corner, and sizing rooms by the rectangle kept splitting until a house had
    fifteen rooms a floor, most of them offices and storerooms.
    """
    if mask is None:
        return (x1 - x0 + 1) * (y1 - y0 + 1)
    return sum(1 for y in range(y0, y1 + 1) for x in range(x0, x1 + 1) if mask[y][x])


def _split(x0: int, y0: int, x1: int, y1: int, rng: random.Random,
           depth: int, out: list[Room],
           target_area: int = TARGET_ROOM_AREA,
           mask: np.ndarray | None = None,
           small: list[bool] | None = None,
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

    # The one small room. A corner taken out of a rectangle leaves an L and
    # the plan is rectangles, so it takes two cuts: a strip the minimum depth
    # off one end, and the room off one end of the strip. What is left of the
    # strip is a closet or a box room, which is what sits beside a bathroom in
    # the game's houses too.
    if small and small[0] and w >= MIN_ROOM * 2 and h >= MIN_ROOM * 2 \
            and area >= target_area * 2:
        wide = w >= h
        length = w if wide else h
        run = rng.randint(SMALL_ROOM_RUN[0],
                          min(SMALL_ROOM_RUN[1], length - MIN_ROOM))
        far_end = rng.random() < 0.5        # which end of the region the strip is
        far_side = rng.random() < 0.5       # which end of the strip the room is
        if wide:
            sy0, sy1 = ((y1 - MIN_ROOM + 1, y1) if far_end else (y0, y0 + MIN_ROOM - 1))
            bx0, bx1 = ((x1 - run + 1, x1) if far_side else (x0, x0 + run - 1))
            room = (bx0, sy0, bx1, sy1)
            strip = ((x0, sy0, bx0 - 1, sy1) if far_side else (bx1 + 1, sy0, x1, sy1))
            rest = (x0, y0, x1, y1 - MIN_ROOM) if far_end else (x0, y0 + MIN_ROOM, x1, y1)
        else:
            sx0, sx1 = ((x1 - MIN_ROOM + 1, x1) if far_end else (x0, x0 + MIN_ROOM - 1))
            by0, by1 = ((y1 - run + 1, y1) if far_side else (y0, y0 + run - 1))
            room = (sx0, by0, sx1, by1)
            strip = ((sx0, y0, sx1, by0 - 1) if far_side else (sx0, by1 + 1, sx1, y1))
            rest = (x0, y0, x1 - MIN_ROOM, y1) if far_end else (x0 + MIN_ROOM, y0, x1, y1)
        # Not where the footprint has cut most of it away: a room of three
        # tiles in the corner of a turned building is not a bathroom.
        if _inside(*room, mask) >= MIN_ROOM * MIN_ROOM - 2:
            small[0] = False
            out.append(Room(*room))
            _split(*strip, rng, depth - 1, out, target_area, mask, small, _sat)
            _split(*rest, rng, depth - 1, out, target_area, mask, small, _sat)
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
        _split(x0, y0, cut - 1, y1, rng, depth - 1, out, target_area, mask, small, _sat)
        _split(cut, y0, x1, y1, rng, depth - 1, out, target_area, mask, small, _sat)
    else:
        cut = rng.randint(lo, hi)
        _split(x0, y0, x1, cut - 1, rng, depth - 1, out, target_area, mask, small, _sat)
        _split(x0, cut, x1, y1, rng, depth - 1, out, target_area, mask, small, _sat)


# What a flat contains, by how many rooms it got. The first entry goes to the
# room the front door opens into; the rest are handed out biggest-first, so the
# bathroom lands on the smallest room.
#
# Size matters: a two-room flat is a bedsit and wants a bathroom more than it
# wants a separate bedroom.
FLAT_PLANS = {
    # A one-room flat is a bedsit: somewhere to sleep, not a sofa and nothing.
    1: ["bedroom"],
    2: ["kitchen", "bathroom"],
    3: ["kitchen", "bedroom", "bathroom"],
    4: ["livingroom", "kitchen", "bedroom", "bathroom"],
    # From five rooms up the flat gets a little hall to open them off. Knox
    # County has 0.12 halls to a flat and two thirds of its floors of flats
    # have none at all - because its flats are small. Ours run to five, six
    # and eight rooms once they trade with the neighbour, and at that size a
    # third of the rooms were two doors deep and some were three or four: a
    # bedroom through a bathroom through a kitchen. Only the big ones get it,
    # which comes to about 0.09 halls a flat.
    5: ["hall", "livingroom", "kitchen", "bedroom", "bathroom"],
}
# From this many rooms up, a flat is entered through a hall.
FLAT_HALL_FROM = 5
# Not every flat is a set of small rectangles. Knox County gives only 0.56
# kitchens to a flat: the rest have no kitchen room at all, just a living room
# with the cooker and the fridge along one wall. The game draws a wall between
# any two rooms, so open plan is not a wall we leave out - it is one room that
# is both, which is what "openplan" is. It reaches the game as a livingroom,
# because the name is what the loot tables key off.
FLAT_PLANS_OPEN = {
    1: ["openplan"],
    2: ["openplan", "bathroom"],
    3: ["openplan", "bedroom", "bathroom"],
    4: ["openplan", "bedroom", "bedroom", "bathroom"],
    5: ["hall", "openplan", "bedroom", "bedroom", "bathroom"],
}
OPEN_PLAN_SHARE = 0.44
# Our room kinds are written out as the game's room names. Where the two
# differ, this maps ours to one the game furnishes and loots.
ROOM_NAME = {
    "openplan": "livingroom",
    # The mall concourse. The game knows it as a hall and the loot tables key
    # off that, so that is the name it is written under; it is a kind of its
    # own here only so it can be furnished as a concourse - planters, bins,
    # vending, the odd kiosk - instead of as the bare corridor a hall is.
    "concourse": "hall",
    # Knox County has 694 rooms called diningroom and 11 called dining. Ours
    # called every house's one "dining", which is a name the loot tables do
    # not know - the same mistake as "cells" for "prisoncells". The kind stays
    # separate from the canteen kind, which is furnished quite differently.
    "dining": "diningroom",
}
FLAT_EXTRA = ["bedroom", "storage"]
# Floor area, in tiles, from which a flat is cut into at least three rooms.
FLAT_MIN_SPLIT_AREA = 30
# Tiles of corridor wall each flat gets. Narrower slices gave two- and
# three-room flats, and with the bathroom going to the smallest room, a flat
# of two big rooms had a bathroom the size of its living room.
# This is the fallback: _flat_frontage works it out from how deep the
# floorplate is, because one fixed frontage cannot suit every building. At 8
# to 11 tiles a deep plate gave 117 m2 flats of six rooms; at 5 to 7 a shallow
# one gave 40 m2 flats that could not hold three.
FLAT_FRONTAGE = (5, 7)
# What a flat should come out at. Knox County's are 57 m2; a little over gives
# room for the separate kitchen ours each need.
FLAT_TARGET_AREA = 62


def _flat_frontage(depth: int) -> tuple[int, int]:
    """How wide to slice flats off a corridor a given depth of floor deep.

    A flat wants about FLAT_TARGET_AREA of floor however deep the building is,
    so the frontage falls as the plate gets deeper. The lower bound is what
    three rooms can physically be cut from: below MIN_SPLIT * MIN_ROOM a room
    cannot be halved again, so a narrow flat comes out as two rooms whatever
    the plan asks for.
    """
    want = max(MIN_ROOM + 1, round(FLAT_TARGET_AREA / max(1, depth)))
    least = max(MIN_ROOM + 1, (MIN_SPLIT * MIN_ROOM * 2) // max(1, depth))
    low = max(want, least)
    return (low, low + 2)
# What a flat is. Knox County's blocks hold, per block, 13.8 bathrooms, 13.7
# living rooms, 10.6 bedrooms, 7.7 kitchens and 4.8 closets. Counting one
# bathroom to a flat - the surest marker, being the commonest room - that is
# about 3.8 rooms to a flat. Ours were 6 rooms in 117 m2: two flats' worth of
# floor cut into two flats' worth of rooms and called one home, which is why a
# floor read as a grid of cells rather than as somewhere people live.
#
# The game gives only 0.56 kitchens to a flat, the rest being a kitchenette in
# the living room. Ours gives every flat one, because a flat with no kitchen
# room spawns no food: the room name is what the loot tables key off.
FLAT_ROOMS = 4
# An open-plan flat is cut into fewer: the living end is one room that is
# also the kitchen, so it has to stay big enough to hold a sofa facing a
# television. At FLAT_ROOMS the open room came out at 16 m2.
FLAT_ROOMS_OPEN = 2
# A floorplate deeper than this gets two ranks of flats back to back, the way
# a real block does, rather than one flat running the whole depth of it.
FLAT_MAX_DEPTH = 11
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
        # A floorplate deeper than one flat gets two ranks back to back, the
        # way a real block does, rather than one flat running the whole depth.
        depth = a1 - a0 + 1
        if frontage == FLAT_FRONTAGE and depth > FLAT_MAX_DEPTH + MIN_ROOM:
            ranks = [(a0, a0 + depth // 2 - 1), (a0 + depth // 2, a1)]
        else:
            ranks = [(a0, a1)]
        for b0, b1 in ranks:
            cut = (_flat_frontage(b1 - b0 + 1)
                   if frontage == FLAT_FRONTAGE else frontage)
            for s0, s1 in _slices(length, rng, *cut):
                unit += 1
                box = (b0, s0, b1, s1) if axis == "y" else (s0, b0, s1, b1)
                area = (box[2] - box[0] + 1) * (box[3] - box[1] + 1)
                flat: list[Room] = []
                # A flat is cut towards FLAT_ROOMS rooms, not towards the
                # building's own room size: at that size a 117 m2 flat came
                # out as six rooms, where the game's 57 m2 flat has three.
                # An open-plan flat is cut towards fewer, so the living end
                # stays one big room instead of two small ones.
                open_plan = (frontage == FLAT_FRONTAGE
                             and rng.random() < OPEN_PLAN_SHARE)
                if open_plan:
                    plan.open_units.add(unit)
                rooms_wanted = FLAT_ROOMS_OPEN if open_plan else FLAT_ROOMS
                want = (max(target, area // rooms_wanted)
                        if frontage == FLAT_FRONTAGE else target)
                _split(*box, rng, MAX_DEPTH, flat, target_area=want,
                       mask=plan.mask)
                # A flat that came out as one or two rooms was all living
                # room: in a small block every flat on every floor had nothing
                # else, not a bed or a bathroom in the building. Cut it into a
                # home's rooms when it is big enough for them.
                if (frontage == FLAT_FRONTAGE and not open_plan
                        and len(flat) < 3
                        and area >= FLAT_MIN_SPLIT_AREA):
                    flat = []
                    _split(*box, rng, MAX_DEPTH, flat,
                           target_area=max(MIN_SPLIT * MIN_ROOM, area // 3),
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


# How many flats trade a room with the one next door, so the two of them come
# out L-shaped around each other instead of both being boxes. Every flat used
# to be one rectangle sliced off the corridor, which is what made a floor read
# as a spreadsheet however well the rooms inside it were arranged.
FLAT_STAGGER_SHARE = 0.35


def _contiguous(members: list[int], adj: dict[int, dict[int, int]]) -> bool:
    """Whether these rooms all touch each other, directly or through others."""
    if len(members) <= 1:
        return True
    want = set(members)
    seen = {members[0]}
    edge = [members[0]]
    while edge:
        nxt = []
        for i in edge:
            for n in adj.get(i, ()):
                if n in want and n not in seen:
                    seen.add(n)
                    nxt.append(n)
        edge = nxt
    return seen == want


def _stagger_flats(plan: Plan, rng: random.Random) -> None:
    """Hand a room from one flat to the flat next door, where both survive it.

    A flat is cut as a rectangle, so a floor of them is a grid. Giving a
    boundary room to the neighbour leaves both of them L-shaped around each
    other, which is how a real block comes out once the flats are not all the
    same size. A room only moves when what it leaves behind still holds
    together and neither flat is emptied.
    """
    adj = _neighbours(plan)
    units: dict[int, list[int]] = {}
    for i, room in enumerate(plan.rooms, 1):
        if room.unit:
            units.setdefault(room.unit, []).append(i)
    order = sorted(units)
    rng.shuffle(order)
    for unit in order:
        members = units.get(unit, [])
        if len(members) < 3 or rng.random() >= FLAT_STAGGER_SHARE:
            continue
        # The room to give away: one on the flat's edge, touching another flat.
        options = [(i, plan.rooms[n - 1].unit)
                   for i in members
                   for n in adj.get(i, ())
                   if plan.rooms[n - 1].unit
                   and plan.rooms[n - 1].unit != unit]
        rng.shuffle(options)
        for give, to in options:
            left = [i for i in members if i != give]
            if not _contiguous(left, adj):
                continue
            gained = units.get(to, []) + [give]
            if not _contiguous(gained, adj):
                continue
            # Touching is not enough: the flat has to be able to put a door
            # between them. A room joined across two tiles of wall could not
            # be reached from its new flat's front door, so the door tree
            # fell through to its last resort and joined two flats together
            # or gave one of them a second front door - 175 and 89 of them.
            if max((adj[give].get(n, 0) for n in units.get(to, ())),
                   default=0) < MIN_SPLIT:
                continue
            plan.rooms[give - 1].unit = to
            units[unit] = left
            units[to] = gained
            plan.open_units.discard(unit)
            plan.open_units.discard(to)
            break


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


def _assign_flat_kinds(plan: Plan, hotel: bool = False,
                       rng: random.Random | None = None) -> None:
    """Give every flat its own set of rooms, arranged around its front door.

    The room touching the corridor is where you walk in, so it becomes the
    living room; the rest follow by size, with the bathroom on the smallest.
    In a hotel each "flat" is guest rooms with a bathroom.
    """
    rng = rng or random.Random(len(plan.rooms))
    adj = _neighbours(plan)
    corridor = {i for i, r in enumerate(plan.rooms, 1) if r.unit == 0}
    units: dict[int, list[int]] = {}
    for i, room in enumerate(plan.rooms, 1):
        if room.unit == 0:
            room.kind = "hall"
        else:
            units.setdefault(room.unit, []).append(i)
    for u, members in units.items():
        entry = [i for i in members if any(n in corridor for n in adj[i])]
        by_size = sorted(members, key=lambda i: -plan.rooms[i - 1].area)
        first = max(entry, key=lambda i: plan.rooms[i - 1].area) \
            if entry else by_size[0]
        # In a flat big enough for a hall, the hall is the first of the plan,
        # so it has to fall on the room that can actually serve as one: the
        # one touching the most of the others. Giving the name to the room by
        # the door instead left it bordering two rooms out of six, and the
        # rest still opened through each other.
        if len(members) >= FLAT_HALL_FROM and not hotel:
            inside = {i: sum(1 for n in adj[i] if n in members)
                      for i in members}
            first = max(members, key=lambda i: (inside[i],
                                                i in entry,
                                                plan.rooms[i - 1].area))
        ordered = [first] + [i for i in by_size if i != first]
        # Some flats are open plan: one room for the kitchen and the living
        # room together rather than a wall between them.
        open_plan = u in plan.open_units
        kinds = ((FLAT_PLANS_OPEN if open_plan else FLAT_PLANS)
                 .get(len(ordered)))
        if hotel:
            # A guest room, its bathroom, and a wardrobe closet in a big one.
            kinds = (["motelroom", "closet"][:max(1, len(ordered) - 1)]
                     + ["motelroom"] * max(0, len(ordered) - 3) + ["bathroom"])[:len(ordered)]                 if len(ordered) > 1 else ["motelroom"]
        elif kinds is None:
            base = FLAT_PLANS_OPEN[5] if open_plan else FLAT_PLANS[5]
            extra = [FLAT_EXTRA[k % len(FLAT_EXTRA)]
                     for k in range(len(ordered) - 5)]
            kinds = base[:-1] + extra + base[-1:]
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
# A room this size or under is a closet rather than a bedroom. It was 8,
# which no room can ever be: MIN_ROOM and MIN_SPLIT put the smallest room
# the splitter can make at 9 tiles, so the rule never once fired and Knox
# County's commonest small room - 5,397 closets - was one we never built.
SMALL_ROOM_TILES = 10
# Upstairs. The game's houses have 1.39 bedrooms and 0.42 children's rooms
# each; ours had 2.15 and 1.21, an upstairs of nothing but beds. A closet on
# the landing is what it really has - 0.50 a house, and none of ours had one.
# A separate dining room, in Knox County, is in 13% of houses.
DINING_ROOM_SHARE = 0.13
HOUSE_SLEEPING = ["bedroom", "kidsbedroom", "bedroom", "storage"]
UPSTAIRS = ["bedroom", "kidsbedroom", "bedroom", "laundry",
            "bedroom", "office", "storage"]


# Where you walk when you are not in a room. Knox County's houses have a hall
# in 42% of them and a living room in nearly all, and everything opens off one
# or the other; ours had no circulation upstairs at all, so a floor of bedrooms
# was a chain of them - the commonest door in the whole town was one bedroom
# into the next.
CIRCULATION = {"hall", "lobby", "livingroom", "concourse"}


def _landing(plan: Plan, adj: dict, free: list,
             stairs: tuple[int, int, str] | None) -> int | None:
    """The room a floor is walked through: where the stairs arrive, or failing
    that whichever room touches the most others."""
    if stairs is not None:
        x, y, d = stairs
        dx, dy = (0, 1) if d == "N" else (1, 0)
        for i in range(STAIR_RUN):
            room = _room_at(plan, x + dx * i, y + dy * i)
            if room in free:
                return room
    if not free:
        return None
    return max(free, key=lambda i: (len([n for n in adj.get(i, ()) if n in free]),
                                    plan.rooms[i - 1].area))


# One more hall per this many rooms on a floor, on top of the corridor and the
# landing. It is a balance between two ways of being wrong, and the number was
# picked by measuring both against Knox County. Too few and rooms chain: at
# one per twelve, 230 doors in 300 buildings went bedroom into bedroom. Too
# many and the plan is corridor: at one per six, a house was a third
# circulation where the game's are a quarter by room. One per eight puts a
# house at 23.3% of its rooms against the game's 23.7%, and bedroom-to-bedroom
# doors at 120 where the old plan had 771.
ROOMS_PER_HALL = 8


def _more_halls(plan: Plan, adj: dict, free: list, kinds: dict,
                seeds: list[int], cap: int) -> int:
    """Rooms turned into halls until the rest all open onto one."""
    made = 0
    while made < cap and free:
        covered = set(seeds)
        for i in seeds:
            covered |= set(adj.get(i, ()))
        left = [i for i in free if i not in covered]
        if not left:
            break
        best = max(free, key=lambda i: len([n for n in adj.get(i, ()) if n in left]))
        if not any(n in left for n in adj.get(best, ())):
            break
        kinds[best] = "hall"
        free.remove(best)
        seeds.append(best)
        made += 1
    return made


def _assign_house_kinds(plan: Plan, level: int, levels: int,
                        stairs: tuple[int, int, str] | None = None,
                        rng: random.Random | None = None) -> None:
    """Rooms of a house, placed by what they sit next to.

    Handing kinds out in size order put the kitchen wherever the second-biggest
    rectangle fell, bathrooms opening off living rooms, and a kitchen on every
    storey of a three-storey house. Here the ground floor holds the rooms a
    household shares - the living room, the kitchen beside it, dining beside
    that - and upper floors hold bedrooms. Bedrooms go as far from the living
    room as the plan allows; the bathroom takes the smallest room.
    """
    rng = rng or random.Random(plan.width * 31 + plan.height)
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
            # A separate dining room is the exception: Knox County has one in
            # 13% of its houses, the rest eating in the kitchen or the living
            # room. Taking one whenever there was a room to spare gave ours
            # one in 93%.
            if near and len(free) >= 3 and rng.random() < DINING_ROOM_SHARE:
                take(max(near, key=lambda i: area[i]), "dining")
        if free and (levels == 1 or len(free) >= 2):
            take(min(free, key=lambda i: area[i]), "bathroom")
        # A small room beside the kitchen is its laundry.
        near = [n for n in adj.get(kitchens[0], ()) if n in free
                and area[n] <= SMALL_ROOM_TILES] if (kitchens := [i for i, k in kinds.items()
                                                                  if k == "kitchen"]) else []
        if near:
            take(min(near, key=lambda i: area[i]), "laundry")
        _more_halls(plan, adj, free, kinds, [living] + [i for i, k in kinds.items()
                                                        if k == "hall"],
                    len(free) // ROOMS_PER_HALL)
        dist = _graph_distance(adj, living)
        rest = sorted(free, key=lambda i: -dist.get(i, 99))
        sleeping = HOUSE_SLEEPING if levels == 1 else ["office", "bedroom", "kidsbedroom"]
        for n, i in enumerate(rest):
            kinds[i] = "closet" if area[i] <= SMALL_ROOM_TILES else sleeping[n % len(sleeping)]
    else:
        # The landing. Without one an upstairs was a row of bedrooms opening
        # into each other, which is what a plan should never ask you to walk
        # through. It is taken before the bathroom, which would otherwise take
        # the smallest room and leave nothing to walk in.
        landing = _landing(plan, adj, free, stairs)
        if landing is not None and len(free) >= 3:
            take(landing, "hall")
        _more_halls(plan, adj, free, kinds,
                    [i for i, k in kinds.items() if k == "hall"],
                    len(free) // ROOMS_PER_HALL)
        if free:
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


# At most one room of this kind per this many rooms on a floor. The fill list
# was cycled round-robin once the mix was spent, so every kind in it ended up
# with an equal share however silly that was: a school came out with 61
# lavatories and 58 offices to its 63 classrooms, a police station with seven
# locker rooms, and a church with as many storerooms as nave. A kind that has
# had its share is skipped and the next one in the list takes the room.
# One room in this many may be of that kind. A kind missing from here has no
# cap at all, which is how a police station came out 36% policeoffice: the
# fill fell through to it every time. Measured over Knox County's 13 buildings
# with a policeoffice in them, a station is 31% cells, 17% offices, 13% hall
# and 8% bathroom - so cells get the run of the place and offices do not.
ROOM_CAP_PER = {
    # A school has one library, one gym, one canteen and one janitor,
    # not one per six rooms: at a cap of 6 a 25-room floor came out with
    # four libraries. These are the rooms a building has exactly one of.
    "library": 40, "gym": 40, "kitchen": 40, "diningroom": 40,
    "janitor": 40, "lobby": 40, "schoollab": 24, "schoolstorage": 24,
    "sportstorage": 40,
    "bathroom": 10, "hall": 12,
    "breakroom": 25, "office": 12, "storage": 8, "garage": 15,
    "policeoffice": 6, "evidenceroom": 25,
    # A few cells, not a cell block: one room in eight at most, so a
    # 30-room station gets three or four rather than the eleven that a
    # cap of 3 gave it.
    "prisoncells": 8, "interrogationroom": 20, "policearchive": 30,
    "lobby_police": 30, "armory": 30, "lockerroom": 20,
    "policegunstorage": 30, "policeoutfitstorage": 30, "policelocker": 15,
    "policehall": 25, "firegarage": 12, "armystorage": 4, "medical": 4,
    "clinic": 4, "officestorage": 12, "janitor": 18,
    "security": 25,
    # A castle's chambers. Everything past the mix falls to the fill, and
    # bedroom was the only kind in the castle's with no cap, so it took every
    # room a bigger keep had: 5% of a small one, 51% at the size the landmark
    # growth actually gives them, 63% at the largest. A barracks is unmoved -
    # its fill falls back to bedroom, which is what a dormitory block is.
    "bedroom": 4,
    # A ground has a few counters on the concourse, not a food hall: cafe was
    # the one kind in the stadium's fill with no cap and took 36% of a big
    # one once the rest were spent.
    "cafe": 10,
    # No mall is all one chain. Each unit kind takes a share of the units,
    # the way a fill kind without a cap took the whole of a big castle.
    "clothingstore": 8, "giftstore": 12, "toystore": 14, "bookstore": 14,
    "furniturestore": 14, "sportstore": 14, "departmentstore": 16,
    # No mall is all one chain, and Knox County's lets to about thirty
    # different trades: clothes 11 units, department 21, gift 3, toy 3, shoe
    # 1, jeweller 3, houseware 3. These keep any one of them to a share.
    "clothesstore": 9, "shoestore": 16, "electronicsstore": 18,
    "housewarestore": 16, "sewingstore": 30, "cornerstore": 30,
    "optometrist": 30, "jewelrystore": 16, "musicstore": 20,
    "candystore": 20, "toolstore": 20, "gunstore": 30, "pharmacy": 25,
    "bakery": 25, "foodcourt": 40, "clothesstorage": 12,
    "departmentstorage": 25, }


# How big each kind of room is in Knox County, in tiles. The mix used to be
# handed to rooms in size order, so whatever stood first in the list got the
# biggest room: a station's cells landed on its fifth-largest room and came
# out at 24 m2 against the game's 15, while its corridor got 36 against 51.
# Ordering the mix by these instead puts a cell on a small room and a hall on
# a big one. A kind that is not here keeps its place in the list.
ROOM_MEDIAN_AREA = {
    # Measured over every room of that name in Knox County, not estimated.
    # The first version of this table was guessed and several were far out: a
    # classroom is 24 m2 and had 48, a dining room 22 and had 55, a hall 27
    # and had 51, a closet 2 and had 6. _split_to_size cuts a room down to
    # these, so a wrong number here decides how big rooms actually come out.
    "library": 100, "gym": 88, "church": 110, "policehall": 51,
    "restaurant": 50, "corridor": 45,
    "policeoffice": 30, "office": 30, "livingroom": 29,
    # Knox County's classrooms are 24 m2 and its schools are a warren of
    # them. These are set higher on purpose: a school of a dozen big
    # rooms reads better than one of forty small ones, and _split_to_size
    # cuts a room down to these, so they are what decides it.
    "hall": 27, "lobby": 40, "classroom": 140, "secondaryclassroom": 140,
    "schoollab": 56, "diningroom": 60, "kitchen": 21,
    "policelocker": 24, "lockerroom": 24, "breakroom": 24, "storage": 20,
    "policegunstorage": 18, "policeoutfitstorage": 18, "policearchive": 18,
    "evidenceroom": 18, "armory": 18,
    "interrogationroom": 16, "bedroom": 15, "prisoncells": 15,
    "security": 15, "officestorage": 14, "janitor": 12,
    "bathroom": 6, "closet": 2,
}


def _by_expected_size(kinds: list[str]) -> list[str]:
    """The same kinds, biggest-roomed first, so they meet rooms of their size.

    Stable on anything ROOM_MEDIAN_AREA does not know, which keeps a list
    whose order was chosen for other reasons in the order it was written.
    """
    if not any(k in ROOM_MEDIAN_AREA for k in kinds):
        return kinds
    return sorted(kinds, key=lambda k: -ROOM_MEDIAN_AREA.get(k, 25))


# A room is cut down to its kind's size when it is this many times too big.
# Below that the spread is just how buildings are.
OVERSIZE = 1.6
# Rooms a building has one of. Splitting an oversized one in two gave a
# school four libraries and two gyms however hard the mix was capped,
# because _split_to_size copies the kind into both halves.
# Only these are cut down to their kind's size. The pass was written to
# stop a police cell coming out at 24 m2 when the game's are 15, and
# applying it to every kind quietly became what decided room size
# everywhere: it was shredding a school floor into halls of 43 m2 and
# offices of 48 however big the splitter had made them.
SHRINK_TO_KIND = {"prisoncells", "closet", "policelocker", "lockerroom",
                  "janitor", "officestorage"}
# How many rooms one oversized room may be cut into. In a big building every
# room is big, so a kind that is small everywhere is always oversized and was
# halved until it fit: two changing rooms in a 70x56 ground became sixteen,
# 39% of the building. A cell block of eight is still a cell block.
MAX_SPLIT_PIECES = 4
ONE_OF_A_KIND = {"library", "gym", "kitchen", "diningroom", "lobby",
                 "janitor", "schoollab", "interrogationroom", "armory",
                 "evidenceroom", "security", "breakroom"}


def _split_to_size(plan: Plan, rng: random.Random) -> None:
    """Cut a room that is far bigger than its kind ever is into rooms of it.

    The splitter makes rooms of roughly one size, so a police station came out
    with every room near 34 m2 - which meant its cells were 24 m2 where Knox
    County's are 15, because there was no small room for a cell to be. Sizing
    the kinds afterwards cannot fix that; there has to be a cell block. Once
    the kind is known the room is halved until it is about the size that kind
    is, which turns one oversized cell into a row of them.

    Runs after the kinds are handed out and before the doors, so the doors are
    placed on the rooms that actually exist.
    """
    for idx in range(len(plan.rooms)):
        room = plan.rooms[idx]
        want = ROOM_MEDIAN_AREA.get(room.kind)
        if (want is None or room.is_core or room.is_shaft
                or room.kind in ONE_OF_A_KIND
                or room.kind not in SHRINK_TO_KIND):
            continue
        queue = [idx]
        pieces = 1
        while queue:
            i = queue.pop()
            r = plan.rooms[i]
            if r.area < want * OVERSIZE or pieces >= MAX_SPLIT_PIECES:
                continue
            # Halve the long way, so a cell block comes out as a row.
            if r.w >= r.h:
                if r.w < MIN_SPLIT:
                    continue
                cut = r.x0 + r.w // 2
                new = Room(cut, r.y0, r.x1, r.y1, kind=r.kind, unit=r.unit)
                r.x1 = cut - 1
            else:
                if r.h < MIN_SPLIT:
                    continue
                cut = r.y0 + r.h // 2
                new = Room(r.x0, cut, r.x1, r.y1, kind=r.kind, unit=r.unit)
                r.y1 = cut - 1
            if min(r.w, r.h, new.w, new.h) < MIN_ROOM:
                # Put it back: the halves would be slivers.
                if new.x0 > r.x0:
                    r.x1 = new.x1
                else:
                    r.y1 = new.y1
                continue
            plan.rooms.append(new)
            pieces += 1
            at = len(plan.rooms)
            for y in range(new.y0, new.y1 + 1):
                for x in range(new.x0, new.x1 + 1):
                    if plan.grid[y][x] == i + 1:
                        plan.grid[y][x] = at
            queue.extend([i, at - 1])


def _assign_kinds(rooms: list[Room], mix: list[str], fill: list[str]) -> None:
    import collections as _c

    mix = _by_expected_size(mix)
    order = sorted(rooms, key=lambda r: -r.area)
    total = len(order)
    used: _c.Counter = _c.Counter()

    def spare(kind: str) -> bool:
        per = ROOM_CAP_PER.get(kind)
        return per is None or used[kind] < max(1, total // per)

    for i, room in enumerate(order):
        if i < len(mix):
            kind = mix[i]
        else:
            # Of the fill kinds that still have a share going, the one whose
            # rooms are this size in Knox County. Cycling the list instead
            # handed cells and cupboards whatever room came next, so a
            # station's cells averaged 24 m2 against the game's 15 while its
            # storerooms took the big ones.
            start = (i - len(mix)) % len(fill)
            ready = [fill[(start + k) % len(fill)] for k in range(len(fill))
                     if spare(fill[(start + k) % len(fill)])]
            if ready and any(k in ROOM_MEDIAN_AREA for k in ready):
                kind = min(ready, key=lambda k: abs(
                    ROOM_MEDIAN_AREA.get(k, 25) - room.area))
            else:
                # Every fill kind is spent. Carry on round the list from
                # where this room falls rather than dropping the whole
                # remainder on the first entry: a big building has far more
                # rooms than the mix and the caps together cover, and that
                # gave a stadium sixteen identical corridors and a castle a
                # floor of beds.
                kind = ready[0] if ready else fill[start]
        room.kind = kind
        used[kind] += 1
    # The smallest room makes a far more convincing bathroom than a hall. It
    # trades kinds with whatever the mix made the bathroom rather than adding
    # one, or a nine-room church came out with two lavatories in it.
    if len(order) >= 3 and "bathroom" in mix and order[-1].kind != "bathroom":
        swap = next((r for r in order if r.kind == "bathroom"), None)
        if swap is not None:
            swap.kind = order[-1].kind
        order[-1].kind = "bathroom"


# Buildings you walk through to get somewhere else. A school of classrooms
# wants a corridor and a police station wants one; a church does not, its nave
# being the way through, and nor does a warehouse or a shop floor. Only houses
# got circulation before this, so a school was classrooms opening into one
# another the way the bedrooms upstairs used to.
# A floor wants a corridor once it holds about this many rooms; below that
# the corridor is more of the building than the rooms it serves.
CORRIDOR_WORTH_IT = 8


NEEDS_CORRIDOR = {"school", "police", "civic", "medical", "fire", "military",
                  "library", "offices"}


def _circulation(plan: Plan, rooms: list[Room]) -> int:
    """Rooms turned into halls until the rest all open onto one."""
    adj = _neighbours(plan)
    where = {id(r): i for i, r in enumerate(plan.rooms, 1)}
    free = [where[id(r)] for r in rooms if (r.kind or "") not in CIRCULATION]
    seeds = [where[id(r)] for r in rooms if (r.kind or "") in CIRCULATION]
    kinds: dict[int, str] = {}
    made = _more_halls(plan, adj, free, kinds, seeds,
                       len(free) // ROOMS_PER_HALL)
    for i, kind in kinds.items():
        plan.rooms[i - 1].kind = kind
    return made


# Kinds whose rooms are run together into one space rather than left as a row
# of separate corridors. Knox County's mall is one concourse of 5,855 tiles on
# the ground floor - a single room made of 62 rectangles - where ours was
# seven halls of about 300 tiles each, because the splitter's target caps
# every room in a building and a hall is no exception. A mall holds hundreds
# of people and the concourse is the reason it can.
MERGE_CIRCULATION: set[str] = set()


# How much of a mall's depth the concourse takes, and the width it is held
# between. Knox County's mall is 33.6% hall by floor and that hall is one
# room of 5,855 tiles, not a row of corridors: the units open onto it and it
# is where the hundreds of people are. Cut before the units, so it is one
# rectangle rather than whatever the splitter leaves over.
MALL_CONCOURSE_SHARE = 0.16
MALL_CONCOURSE_MIN = 9
MALL_CONCOURSE_MAX = 26
# The gallery either side of the hole on an upper floor. Knox County's mall
# leaves 89% of the ground concourse open to the floor above - 5,185 of its
# 5,855 squares have nothing on them at level 1 - so the two floors read as
# one space. This is how much of the band stays walkable up there.
MALL_GALLERY = 3
# The arms that run off the spine to the far walls: how wide against the
# spine, the bounds on that, how far apart, and how many at most. Knox
# County's concourse is 27% of its bounding box because of these; without
# them a mall is one aisle with rooms down each side.
MALL_ARM_SHARE = 0.60
MALL_ARM_MIN = 5
MALL_ARM_MAX = 8
MALL_ARM_EVERY = 26
MALL_ARMS_MAX = 3


def _mall_rooms(plan: Plan, rng: random.Random, target: int,
                level: int = 0) -> None:
    """A branching concourse, with the units filling the blocks between it.

    Knox County's mall concourse is not a corridor down the middle: it is a
    wide spine across the building with arms running off it to the far walls,
    and it covers 27% of its own bounding box. A single band covered 100% of
    one and read as a warehouse aisle. The spine and each arm is its own
    rectangle - a Room here is its rectangle, and the doors and the furnishing
    both read it - and they are all concourse, so the game sees one run of
    hall the shopper walks the length of.

    Above the ground floor the middle of the spine is left open (no room, so
    no floor) with a gallery either side, which is how the mall is one space
    several storeys tall.
    """
    w, h = plan.width, plan.height
    along_x = w >= h
    across, length = (h, w) if along_x else (w, h)
    band = max(MALL_CONCOURSE_MIN,
               min(MALL_CONCOURSE_MAX, round(across * MALL_CONCOURSE_SHARE)))
    if across - band < 2 * MIN_ROOM:
        _split(0, 0, w - 1, h - 1, rng, MAX_DEPTH, plan.rooms,
               target_area=target, mask=plan.mask)
        return
    start = (across - band) // 2

    def put(a0, b0, a1, b1, kind=None, fixed=False):
        """A room in the building's own axes (a = along, b = across)."""
        if a1 < a0 or b1 < b0:
            return
        if along_x:
            plan.rooms.append(Room(a0, b0, a1, b1, kind=kind or "hall",
                                   fixed=fixed) if kind
                              else Room(a0, b0, a1, b1))
        else:
            plan.rooms.append(Room(b0, a0, b1, a1, kind=kind or "hall",
                                   fixed=fixed) if kind
                              else Room(b0, a0, b1, a1))

    def cut(a0, b0, a1, b1):
        if a1 < a0 or b1 < b0:
            return
        box = (a0, b0, a1, b1) if along_x else (b0, a0, b1, a1)
        _split(box[0], box[1], box[2], box[3], rng, MAX_DEPTH, plan.rooms,
               target_area=target, mask=plan.mask)

    # Where the arms meet the spine, worked out first: the hole in an upper
    # floor stops at them, so each arm carries on across as a bridge. Without
    # that the hole cut the storey in two and half the units had no way to
    # the stairs.
    arm = max(MALL_ARM_MIN, min(MALL_ARM_MAX, round(band * MALL_ARM_SHARE)))
    count = max(1, min(MALL_ARMS_MAX, (length - arm) // MALL_ARM_EVERY))
    centres = [round(length * (k + 1) / (count + 1)) for k in range(count)]
    spans = []
    for c in centres:
        a0 = max(0, min(length - arm, c - arm // 2))
        if not spans or a0 > spans[-1][1] + MIN_ROOM:
            spans.append((a0, a0 + arm - 1))
    bridged = {a for a0, a1 in spans for a in range(a0, a1 + 1)}

    # The spine, all the way across.
    hole = band - 2 * MALL_GALLERY
    open_middle = level > 0 and hole >= 3
    if open_middle:
        put(0, start, length - 1, start + MALL_GALLERY - 1, "concourse", True)
        put(0, start + band - MALL_GALLERY, length - 1, start + band - 1,
            "concourse", True)
        for a0, a1 in spans:
            put(a0, start + MALL_GALLERY, a1, start + band - MALL_GALLERY - 1,
                "concourse", True)
        for b in range(start + MALL_GALLERY, start + band - MALL_GALLERY):
            for a in range(length):
                if a not in bridged:
                    plan.void.add((a, b) if along_x else (b, a))
    else:
        put(0, start, length - 1, start + band - 1, "concourse", True)

    # The units fill the blocks between the arms.
    for lo, hi in (0, start - 1), (start + band, across - 1):
        if hi < lo:
            continue
        edge = 0
        for a0, a1 in spans:
            cut(edge, lo, a0 - 1, hi)
            put(a0, lo, a1, hi, "concourse", True)
            edge = a1 + 1
        cut(edge, lo, length - 1, hi)
    if plan.void:
        _bridge_core(plan, along_x)


# What may stand on the concourse: what its own wishlist puts there, plus the
# light switches and lamps every room gets. With Erika's Tiles the plants come
# through as erika_plant_*, so these are matched as prefixes.
CONCOURSE_KEEP = {"plant", "erika_plant", "shop_bin", "vending",
                  "erika_vending", "switch", "lamp", "light"}


def _clear_concourse(plan: Plan) -> int:
    """Take off the concourse whatever the units put there.

    A shop is fitted from its own walls out, and a unit whose front is the
    concourse edge lays its wall pieces on the far side of that wall - a
    furniture shop left fourteen dressers standing in the middle of the mall.
    Anything on a concourse tile that is not concourse fitting comes off.
    """
    concourse = {i for i, r in enumerate(plan.rooms, start=1)
                 if r.kind == "concourse"}
    if not concourse:
        return 0
    kept, dropped = [], 0
    ids = plan._ids if plan._ids is not None else _bind_ids(plan)
    w, h = plan.width, plan.height
    keep = tuple(CONCOURSE_KEEP)
    for piece in plan.furniture:
        role, x, y = piece[0], piece[1], piece[2]
        inside = (0 <= x < w and 0 <= y < h and ids[y * w + x] in concourse)
        if inside and not role.startswith(keep):
            dropped += 1
            continue
        kept.append(piece)
    plan.furniture = kept
    return dropped


def _bridge_core(plan: Plan, along_x: bool) -> None:
    """Keep the stairs out of the hole, and a way to them across it.

    The stair core stands in the middle of the building, which is the middle
    of the concourse, which is exactly where the hole goes: left alone it was
    a landing marooned in mid-air with the galleries out of reach either side.
    The core keeps its floor and a walkway runs from it to the nearer gallery.
    """
    if not plan.core:
        return
    cx0, cy0, cx1, cy1 = plan.core
    rows = [y for x, y in plan.void]
    cols = [x for x, y in plan.void]
    lo, hi = (min(rows), max(rows)) if along_x else (min(cols), max(cols))
    for x in range(cx0, cx1 + 1):
        for y in range(cy0, cy1 + 1):
            plan.void.discard((x, y))
    if along_x:
        # Out of the core to whichever edge of the hole is closer.
        up = (cy0 - lo) <= (hi - cy1)
        span = range(lo, cy0) if up else range(cy1 + 1, hi + 1)
        for y in span:
            for x in range(cx0, cx1 + 1):
                plan.void.discard((x, y))
    else:
        left = (cx0 - lo) <= (hi - cx1)
        span = range(lo, cx0) if left else range(cx1 + 1, hi + 1)
        for x in span:
            for y in range(cy0, cy1 + 1):
                plan.void.discard((x, y))


def _merge_halls(plan: Plan) -> int:
    """Run neighbouring halls together into one concourse.

    Only where the two make a rectangle between them: a Room here is its
    rectangle, and the doors and the furnishing both read it, so an L-shaped
    room would put doors and shelving outside the room. Repeated until nothing
    else will join, which turns a row of corridor cells into one long hall.
    """
    merged = 0
    changed = True
    while changed:
        changed = False
        halls = [(i, r) for i, r in enumerate(plan.rooms, start=1)
                 if r.kind == "hall" and not r.is_core and not r.is_shaft]
        for ai, a in halls:
            for bi, b in halls:
                if ai >= bi or a.kind != "hall" or b.kind != "hall":
                    continue
                side_by_side = (a.y0 == b.y0 and a.y1 == b.y1
                                and (a.x1 + 1 == b.x0 or b.x1 + 1 == a.x0))
                stacked = (a.x0 == b.x0 and a.x1 == b.x1
                           and (a.y1 + 1 == b.y0 or b.y1 + 1 == a.y0))
                if not (side_by_side or stacked):
                    continue
                for y in range(b.y0, b.y1 + 1):
                    for x in range(b.x0, b.x1 + 1):
                        if plan.grid[y][x] == bi:
                            plan.grid[y][x] = ai
                a.x0, a.y0 = min(a.x0, b.x0), min(a.y0, b.y0)
                a.x1, a.y1 = max(a.x1, b.x1), max(a.y1, b.y1)
                b.kind = None          # owns no tiles now; _renumber drops it
                merged += 1
                changed = True
                break
            if changed:
                break
    if merged:
        _renumber(plan)
    return merged


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


# How far along a wall a door may sit, as a share of the run, and how much
# wall it keeps beside it. Every door used to go at the middle of the longest
# wall, which reads as a corridor of identical openings. Real doors sit off
# to one side, but not hard against the corner: a frame needs a tile beside it.
DOOR_ALONG = (0.18, 0.82)
DOOR_FROM_CORNER = 1


def _door_spot(edges: list[tuple[int, int, str]],
               min_run: int,
               rng: random.Random | None = None
               ) -> tuple[tuple[int, int, str], int] | None:
    """A place on the longest straight run of wall, and that run's length.

    The runs are grouped once. Somewhere along the longest one rather than
    its middle - see DOOR_ALONG. Without an rng it still takes the middle.
    """
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
                    mid = moving[start + _along(length, rng)]
                    if direction == "W":
                        spot = (int(fixed), int(mid), "W")
                    else:
                        spot = (int(mid), int(fixed), "N")
                    best = (spot, int(length))
                start = i
    return best


def _along(length: int, rng: random.Random | None) -> int:
    """Which tile of a wall run of this length the door goes on."""
    if rng is None or length < 3:
        return length // 2
    low = max(DOOR_FROM_CORNER, int(length * DOOR_ALONG[0]))
    high = min(length - 1 - DOOR_FROM_CORNER, int(length * DOOR_ALONG[1]))
    if high < low:
        return length // 2
    return rng.randint(low, high)


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


# Rooms you sleep, wash or work in. A door between two of them is a room you
# have to walk through to reach another, which is what made a plan read as a
# warren rather than a house - the commonest door in a generated town was one
# bedroom into the next. The one pair a real house does have is a bathroom off
# a bedroom, and the kitchen, dining room and living room share their walls
# openly; everything else goes through the hall.
PRIVATE_ROOMS = {"bedroom", "kidsbedroom", "bathroom", "office", "kitchen",
                 "dining", "storage", "closet", "laundry"}
ENSUITE = ({"bedroom", "bathroom"}, {"kidsbedroom", "bathroom"})
# Dear enough that the tree takes any other way round, cheap enough that a
# room with no other wall to open on is still reached rather than sealed.
PRIVATE_PAIR_COST = 24.0
# What each room already walked through costs. The tree took the cheapest
# door anywhere on the frontier, which strings rooms into a chain: a third of
# the rooms in a big flat sat two doors from the front door and some sat four,
# a bedroom through a bathroom through a kitchen. Hanging a room off one that
# is already deep now costs more than hanging it off one near the door, so
# the plan fans out instead of running away from the entrance.
# At this weight three rooms deep costs more than PRIVATE_PAIR_COST, so in
# theory the tree would rather open a bedroom into a bedroom than go on -
# measured, it does not: that pair is 0.6% of doors at this setting and 0.5%
# at a third of it, because the frontier rarely offers the choice.
DEPTH_COST = 9.0


def _door_cost(a: str, b: str) -> float:
    kinds = {a, b}
    if "hall" in kinds or "lobby" in kinds:
        cost = 1.0
    elif kinds in TOGETHER:
        cost = 1.5
    elif "livingroom" in kinds:
        cost = 3.0
    elif kinds in ENSUITE:
        cost = 4.0
    elif kinds <= PRIVATE_ROOMS:
        cost = PRIVATE_PAIR_COST
    else:
        cost = 6.0
    # Where Knox County's bathrooms open, counted off the compiled map: 614 of
    # its 807 bathroom doors are onto a living room, 56 onto a kitchen, 36 off
    # a bedroom. Ours had the living room on the dear side of this and did
    # 30% of its bathrooms as bedroom en-suites against the game's 4%, so the
    # commonest arrangement in the game was the one we almost never built.
    if ("bathroom" in kinds
            and not kinds & {"hall", "lobby", "livingroom", "bedroom",
                             "kidsbedroom"}):
        cost += 6.0
    # ...and a bathroom off a bedroom is the exception there, not the rule.
    if kinds in ENSUITE:
        cost += 5.0
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
    door_spots = {pair: _door_spot(wall, 1, rng) for pair, wall in edges.items()}

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
    # How many doors from the way in each connected room is, for DEPTH_COST.
    depth: dict[int, int] = {i: 0 for i in connected}

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
                    elif flat in front and relax < 3:
                        # A flat has one front door. This was relaxed a
                        # pass earlier, and every room that could not be
                        # reached any other way opened its own way onto the
                        # corridor - 101 flats with two or three of them.
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
                # A bathroom is a room you choose to go into, never a way
                # through to somewhere else: once it has its door it takes no
                # other, so nothing is ever reached by walking through it.
                # This was a cost of 12, which the tree simply paid whenever
                # the way round was dearer - 4.8% of rooms were still reached
                # through one. Only the last relax pass may break it, and that
                # runs when the alternative is a room with no door at all.
                if any(rooms[i - 1].kind == "bathroom" and doors_of.get(i)
                       for i in (a, b)):
                    if relax < 2:
                        continue
                    # Last pass: allowed, but dearer than anything else on
                    # the board, so it happens only where a room would
                    # otherwise have no door at all.
                    cost += 100.0
                cost -= min(spot[1], 6) * 0.05
                # Hanging a room off one that is already deep costs more, so
                # the plan fans out from the way in rather than chaining.
                grown = a if a in connected else b
                cost += DEPTH_COST * depth.get(grown, 0)
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
            grown, fresh = (a, b) if a in connected else (b, a)
            depth[fresh] = depth.get(grown, 0) + 1
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
PRIVATE_ROOMS = {"bathroom", "bedroom", "kidsbedroom", "closet", "prisoncells"}


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
# What "a shelf" is, by the room it stands in.
#
# One generic wooden shelf was on nearly every room's list and was always the
# same sprite, which made it the third commonest object in a town. Varying it
# per home was worse: it put warehouse wire racking in people's bathrooms. A
# shelf is a different piece of furniture in a kitchen, a study and a garage,
# so the room picks, and only a room that would really have one gets the
# racking.
# wall_cabinet is not in here on purpose. It is an upper cabinet that hangs
# above a counter, on the roof layer so it draws over one, and the kitchen
# already puts them there. Offering it as a shelf stood one on any wall with
# nothing underneath, taking floor space it does not stand on.
# "shelf" here means the plain household shelf in any of its styles. It used
# to mean one sprite, furniture_shelving_01_001-004, which is on nearly every
# room's list: it came to 4% of all the furniture in a town, one shelf you saw
# in every house. Knox County's most-used single shelf is 12% of its shelving
# and the rest is spread over a dozen styles, so ours spreads over three.
SHELVES = ("shelf", "shelf_1", "shelf_2")
SHELVING = {
    # Nothing stands along a concourse wall: that edge is the shopfronts.
    "concourse": (),
    "bathroom": SHELVES + ("dresser",),
    "kitchen": SHELVES,
    "laundry": SHELVES + ("dresser",),
    "livingroom": ("bookshelf",) + SHELVES,
    "dining": ("bookshelf",) + SHELVES,
    "bedroom": ("bookshelf",) + SHELVES,
    "kidsbedroom": ("bookshelf",) + SHELVES,
    "hall": SHELVES + ("bookshelf",),
    "office": ("bookshelf", "filing_cabinet"),
    "library": ("bookshelf",),
    "classroom": ("bookshelf",) + SHELVES,
    "storage": ("metal_rack", "crate") + SHELVES,
    "garage": ("metal_rack", "crate"),
    "shed": ("metal_rack",) + SHELVES,
    "warehouse": ("metal_rack", "crate"),
    "factory": ("metal_rack", "crate"),
    "workshop": ("metal_rack",) + SHELVES,
}
DEFAULT_SHELVING = SHELVES + ("bookshelf",)
# What goes on a corridor's walls, and how much of its facade may be used
# before the windows lose their columns. Rugs go on the floor because they
# are the one thing you can walk over.
CORE_WALL_ART = ("painting", "mirror", "painting", "corkboard")
# ...and nothing at all down a workplace corridor.
CORE_WALL_ART_CIVIC: tuple = ()
CORE_FACADE_SHARE = 4
# One piece of art per this many wall slots. Hanging one on every slot filled
# a corridor edge to edge and made pictures the commonest thing in a house.
CORE_ART_EVERY = 3
# Pictures and mirrors a room may hold, however long its wishlist, and how
# often a room gets one at all. They repeat where other things do not and
# they are on nearly every room's list, so almost every room had one or two
# and wall art came to 11.8% of everything in a house. Not every room in a
# house has a picture in it.
WALL_ART_CAP = 1
WALL_ART_CHANCE = 0.55
# Workplaces hang nothing. Knox County's school rooms carry 0.19 pieces of
# wall decoration per 10 m2 and ours sat at 0.21, but with Erika's Tiles
# installed the substitution below turns every picture and mirror into one
# of her 59 wall-art sprites - among them round faces that read as clocks,
# and a school came out with a wall of them. A classroom, a corridor and a
# police office get none.
ART_FREE_KINDS = {"police", "civic", "school", "medical", "fire",
                  "military", "library"}
# Buildings that are places of work, not homes, and the rooms in them whose
# usual wishlist is a domestic one. A station, a school or a clinic gets a
# noticeboard and a water cooler where a house gets a side table and a
# picture.
# Tiles of floor per piece of furniture, where a room is not the usual 4.
ROOM_DENSITY = {"classroom": 2, "secondaryclassroom": 2, "schoollab": 2,
                "library": 3, "prisoncells": 3}
# Warehouse fittings. A house keeps none of them: its box room had a steel
# rack and a packing crate in it, which is a stockroom, not a cupboard under
# the stairs. Barns and sheds are outbuildings and may keep theirs.
WAREHOUSE_ROLES = {"metal_rack", "crate"}
HOME_KINDS = {None, "house", "apartment"}
# What a home puts in a box room instead.
HOME_STORE = {"metal_rack": "shelf", "crate": "dresser"}
CIVIC_KINDS = {"police", "civic", "school", "medical", "fire", "military",
               "library"}
CIVIC_ROOMS = {
    "hall": ["plant", "water_cooler", "chair", "painting", "chair"],
    "lobby": ["shop_counter", "chair", "chair", "plant", "painting",
              "chair", "water_cooler"],
    "office": ["desk", "office_chair", "filing_cabinet", "corkboard",
               "filing_cabinet", "plant"],
    "breakroom": ["counter", "fridge", "chair", "chair", "corkboard",
                  "water_cooler"],
}
CORE_RUGS = ("rug_wide", "rug", "rug_small")
# One rug per this many tiles of corridor. It was 3, chosen to reach '7
# pieces per 10 m2' - a figure read off a per-house median over a filtered
# set of houses, not off the halls themselves. Measured over the whole map,
# a Knox County hall carries 1.2 pieces per 10 m2 of everything and 0.08
# rugs: a corridor there is bare floor, not a runner of carpet.
CORE_RUG_EVERY = 125


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
    carrying both a west and a north wall as one corner piece, and a door on
    one comes out as neither door nor wall. On a shipped town map 989 doorways
    of 57,357 sat on one, over 741 buildings, which is what a player reads as
    a door that will not open.

    It moves along its own wall to the nearest tile that is not a corner, and
    where that whole boundary is corners - a stepped diagonal wall is one or
    two tiles long - to a clean wall the room shares with some other
    neighbour. Never at the cost of shutting a room off: every swap is checked
    against the rooms that could be reached before it.
    """
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

    def homes(pair) -> tuple:
        """Which dwellings a wall stands between; -1 is the outside."""
        return tuple(sorted(storey.rooms[r - 1].unit if r else -1 for r in pair))

    def joinable(pair) -> bool:
        """Whether a door may hang between these two rooms at all."""
        a, b = pair
        if a == b or (a and storey.rooms[a - 1].is_shaft) \
                or (b and storey.rooms[b - 1].is_shaft):
            return False
        # Never a door from one person's flat into the next.
        ua, ub = homes(pair)
        return not (ua > 0 and ub > 0 and ua != ub)

    # Not from outside: a floor above the ground has no outside door, so a
    # walk that started there reached nothing and the check below waved
    # everything through. From a room, so it holds on every storey, and with
    # the outside among the rooms it reaches, so a front door cannot be moved
    # indoors either.
    start = next((i for i, r in enumerate(storey.rooms, 1) if not r.is_shaft), 0)

    def reaches(doors) -> set:
        """What can be walked to from inside, through doors alone. 0 is the
        outside, so it is in the set when a way in survives."""
        adj: dict[int, set] = {}
        for (x, y, d) in doors:
            a, b = _sides_of(storey.grid, x, y, d)
            if a == b:
                continue
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        seen, stack = {start}, [start]
        while stack:
            here = stack.pop()
            # The outside counts as reached, so a way in cannot be moved
            # away, but it is not a corridor: going out of the front door and
            # round to the back is not how a room is got into.
            if here == 0:
                continue
            for nxt in adj.get(here, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    moved = 0
    for i, (x, y, d) in enumerate(storey.doors):
        if (x, y) not in corners:
            continue
        want = _sides_of(storey.grid, x, y, d)
        taken = set(storey.doors)
        before = reaches(storey.doors)
        spots = [s for s in boundary.get(want, ()) if s not in taken]
        if not spots:
            # That boundary is corners end to end. The room still has to open
            # onto something, so any clean wall it shares with a neighbour
            # will do - as long as the door keeps its job. A wall between the
            # same two dwellings only: move a flat's front door onto an inside
            # wall and the flat has no front door, move it onto the corridor
            # somewhere else and the flat it lands on has two.
            mine = {r for r in want if r}
            role = homes(want)
            spots = [s for pair, places in boundary.items()
                     if joinable(pair) and mine & set(pair) and homes(pair) == role
                     for s in places if s not in taken]
        # The nearest first, so a front door stays on the front of the house.
        spots.sort(key=lambda s: (abs(s[0] - x) + abs(s[1] - y), s))
        for spot in spots[:DOOR_SPOTS_TRIED]:
            trial = list(storey.doors)
            trial[i] = spot
            if reaches(trial) >= before:
                storey.doors[i] = spot
                moved += 1
                break
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


def _occ_mark_room(plan: Plan, idx: int, cells, bit: int) -> None:
    """Mark `bit` on cells that belong to room `idx`.

    A shop's aisle ring and a dining room's door halo reach one tile into the
    next room. The occupancy grid is shared by the whole storey, and the set
    it replaced started empty for every room, so that spill is not furniture
    here. Leaving it off the neighbour means the next room does not have to
    wipe the grid before it places anything.
    """
    occ = plan.occ
    if occ is None:
        return
    ids = plan._ids
    if ids is None:
        ids = _bind_ids(plan)
    w, h = plan.width, plan.height
    flag = np.uint8(bit)
    for x, y in cells:
        if 0 <= x < w and 0 <= y < h and ids[y * w + x] == idx:
            occ[y, x] |= flag


def _note(plan: Plan, occupied: set | None, cells) -> None:
    """The Python placement set, used only when the plan has no occupancy grid.

    With a grid, fit checks read the grid. Writing the same cells into the
    set as well does not change a single placement.
    """
    if plan.occ is None and occupied is not None:
        occupied.update(cells)


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


# Neighbour just past a facing. Shared by the facade probe and the shop
# wall lines, which used to rebuild this from the room's tiles.
_STEP = {"N": (0, -1), "S": (0, 1), "W": (-1, 0), "E": (1, 0)}


def _room_box(plan: Plan, idx: int, room: Room):
    """(count, x0, x1, y0, y1) of the tiles this room owns, or None.

    The count and the bounds are what a shop used to get by building a Python
    set of every tile. x1 and y1 are inclusive, matching Room.
    """
    sub = plan.grid[room.y0:room.y1 + 1, room.x0:room.x1 + 1]
    ys, xs = np.nonzero(sub == idx)
    if ys.size == 0:
        return None
    return (int(ys.size),
            room.x0 + int(xs.min()), room.x0 + int(xs.max()),
            room.y0 + int(ys.min()), room.y0 + int(ys.max()))


def _wall_lines(plan: Plan, idx: int, slots, stair_tiles) -> dict[str, list]:
    """Tiles of each wall, sorted by (x, y), stair tiles included.

    `_wall_slots` already walked the room. A shop used to walk it again,
    and that walk left stair tiles in. They are put back here so a wall
    piece still steps over the flight in the same order.
    """
    lines = {side: [] for side in _STEP}
    for x, y, facing in slots:
        lines[facing].append((x, y))
    for x, y in stair_tiles:
        if _room_at(plan, x, y) != idx:
            continue
        for facing, (dx, dy) in _STEP.items():
            if _room_at(plan, x + dx, y + dy) != idx:
                lines[facing].append((x, y))
    for side in lines:
        lines[side].sort()
    return lines


def _outside_counts(plan: Plan, idx: int, slots, facade, stair_tiles) -> dict[str, int]:
    """Outside-wall tiles per facing, party walls left out.

    The same count a shop front used to make by probing every tile of the
    room. Stair tiles are not in `slots`; they are counted here.
    """
    counts = {side: 0 for side in _STEP}
    for slot in slots:
        if not facade[slot]:
            continue
        x, y, facing = slot
        if _wall_edge(x, y, facing) not in plan.party:
            counts[facing] += 1
    for x, y in stair_tiles:
        if _room_at(plan, x, y) != idx:
            continue
        for facing, (dx, dy) in _STEP.items():
            if _room_at(plan, x + dx, y + dy):
                continue
            if _wall_edge(x, y, facing) not in plan.party:
                counts[facing] += 1
    return counts


SWITCH = "switch"
# One light switch per this much floor, in tiles, and never more than this
# many in a room. A room of a hundred tiles keeps its single switch; a
# supermarket's sales floor gets one about every eight metres.
LIGHT_EVERY_TILES = 110
MAX_SWITCHES = 8
# How often a room too small to need one by area gets a switch anyway. In a
# house every room has one and there are eight rooms; a school has forty and
# a switch in each came to 0.20 per 10 m2 against Knox County's 0.03 - a
# wall of light switches down every corridor. Workplaces get them sparsely.
# These are the round wall fittings that read as clocks in a render, and
# there were dozens of them: 0.27 per 10 m2 against Knox County's 0.12 over
# the same cells. Not every room in any building has one.
SWITCH_SHARE_CIVIC = 0.14
SWITCH_SHARE_HOME = 0.22
SWITCHES_APART = 7
# Things fixed to a wall rather than standing against it. A painting has only
# north and west sprites, so on a south or east wall the fallback drew it on
# the far edge of the tile, hanging in mid-air a tile into the room; shelves
# and mirrors on those walls rendered as planks floating over the floor. They
# go on north and west walls only. (The switch has true east and south
# sprites, and every room needs one, so it may go anywhere.)
# Pieces that read the same whichever wall they are against: a table has no
# front, so the north sprite on a south wall is not wrong, it is just a table.
_EITHER_WAY = {"table", "coffee_table", "dining_table", "diner_table",
               "picnic_table", "pizza_table", "bath_mat", "rug", "rug_small",
               "rug_wide", "shag_rug", "roof_ac", "roof_vent", "roof_hatch",
               "elevator_door"}
# Anything with no south or east sprite, worked out from the catalogue rather
# than listed by hand. The fallback draws such a piece with its north or west
# sprite, which puts it on the far edge of the tile: a corkboard on a south
# wall hangs a tile into the room, a rack faces its own back. Listing them by
# hand meant a role added later was quietly wrong, which is how 1076 pieces
# in 200 houses ended up against a wall they have no sprite for.
#
# Painting, mirror and shelf are here despite having all four: on a south or
# east wall they render as planks floating over the floor.
NORTH_WEST_ONLY = (
    {role for role, facings in C.FURNITURE.items() if set(facings) <= {"N", "W"}}
    - _EITHER_WAY
    | {"painting", "mirror", "shelf"}
    | set(getattr(C, "ERIKA_WALL_ART", ()))
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
    # An open-plan flat: the same seating, set out in the middle of the floor,
    # because the cooking end has the walls.
    "openplan": ([("rug_wide", 0, 1, "W"), ("sofa", 0, 0, "N"),
                  ("coffee_table", 0, 2, "N")], 1),
    "lobby": ([("rug_wide", 0, 0, "W"), ("coffee_table", 1, 0, "W")], 2),
    "dining": ([("rug_wide", 0, 0, "W"), ("dining_table", 1, 1, "W"),
                ("chair", 0, 1, "W"), ("chair", 3, 1, "E"),
                ("chair", 1, 0, "N"), ("chair", 2, 2, "S")], 1),
    # A mat under the table. Rugs lie on their own layer and block nothing, so
    # it costs the group a row of clearance and nothing else.
    "kitchen": ([("rug_small", 0, 0, "W"), ("round_table", 1, 0, "W"),
                 ("chair", 0, 0, "W"), ("chair", 2, 0, "E")], 1),
    # A canteen is rows of long tables, not one table in the middle.
    "diningroom": ([("dining_table", 1, 1, "W"), ("chair", 0, 1, "W"),
                    ("chair", 3, 1, "E"), ("chair", 1, 0, "N"),
                    ("chair", 2, 2, "S")], 10),
    # A bedroom has no centre group. Its whole group used to be a rug, which
    # put one in every bedroom in the town: 63% of our rugs sat in bedrooms
    # against Knox County's 6%, where two thirds of them are in living
    # rooms. The bed and its tables come from bed_against_wall instead.
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
KITCHENS = {"kitchen", "openplan", "breakroom", "restaurantkitchen", "pizzakitchen", "burgerkitchen",
            "dinerkitchen", "chinesekitchen", "sushikitchen", "mexicankitchen", "seafoodkitchen",
            "cafekitchen", "bakerykitchen", "icecreamkitchen"}
FALLBACK_GROUPS: dict[str, list[list[tuple[str, int, int, str]]]] = {
    "livingroom": [[("sofa", 0, 0, "N"), ("coffee_table", 0, 2, "N"), ("tv", 0, 4, "S")],
                   [("sofa", 0, 0, "N"), ("coffee_table", 0, 2, "N")],
                   [("rug_small", 0, 0, "W"), ("coffee_table", 0, 0, "N")]],
    "dining": [[("dining_table", 1, 0, "W"), ("chair", 0, 0, "W"), ("chair", 3, 0, "E")],
               [("round_table", 1, 0, "W"), ("chair", 0, 0, "W"), ("chair", 2, 0, "E")]],
    # Half the game's kitchens have a table in them and one in eight of ours
    # did: the group and its aisle want a five-by-three clear block, and a
    # kitchen four tiles across with two doors in it has nowhere to put one.
    # A table on its own is the last thing tried, as it is what a kitchen too
    # small to eat in still has.
    "kitchen": [[("round_table", 0, 0, "W"), ("chair", 1, 0, "E")],
                [("round_table", 0, 0, "W")]],
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
    before = len(plan.furniture)
    placed = _place_group(plan, idx, room, own(group), repeat, occupied, keep_clear)
    for smaller in FALLBACK_GROUPS.get(room.kind, ()):
        if placed:
            break
        placed = _place_group(plan, idx, room, own(smaller), 1, occupied, keep_clear)
    if placed:
        _seat_the_table(plan, idx, keep_clear, plan.furniture[before:])
    return placed


# Every piece in a centre group needs its own clear block and a tile of aisle
# all round the group, so in a kitchen four tiles across only the bare table
# ever fitted: 0.17 chairs a kitchen against the game's 1.10. A chair is
# tucked against the table, not given an aisle of its own, so they go on after
# the group is down.
TABLE_SEATS = 2
_BESIDE = (((-1, 0), "W"), ((1, 0), "E"), ((0, -1), "N"), ((0, 1), "S"))


def _seat_the_table(plan: Plan, idx: int, keep_clear: set[tuple[int, int]],
                    added: list) -> None:
    """Chairs on the free tiles beside a table the centre group just put down."""
    tables = [(role, x, y, o) for role, x, y, o in added
              if role in ("round_table", "dining_table")]
    if not tables:
        return
    # The centre group is the only floor furniture in this room so far, and a
    # piece placed earlier cannot cover this room's tiles. Scanning the whole
    # storey here was the same set.
    taken = {c for role, x, y, o in added
             if C.FURNITURE_LAYERS.get(role, "Furniture") == "Furniture"
             for c in _cells_for(role, x, y, o)}
    # Only up to what the group would have seated anyway: a dining table that
    # already came with four chairs is not given two more.
    already = sum(1 for role, *_ in added if role == "chair") // len(tables)
    for role, tx, ty, orient in tables:
        seats = already
        for cell in _cells_for(role, tx, ty, orient):
            for (dx, dy), side in _BESIDE:
                if seats >= TABLE_SEATS:
                    break
                x, y = cell[0] + dx, cell[1] + dy
                if (x, y) in taken or (x, y) in keep_clear or _room_at(plan, x, y) != idx:
                    continue
                plan.furniture.append(("chair", x, y, _facing("chair", side)))
                taken.add((x, y))
                seats += 1


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
    gx0 = gy0 = gx1 = gy1 = 0
    any_cell = False
    for role, dx, dy, o in pieces:
        for cx, cy in _cells_for(role, 0, 0, o):
            x, y = dx + cx, dy + cy
            if not any_cell:
                gx0 = gx1 = x
                gy0 = gy1 = y
                any_cell = True
            else:
                if x < gx0:
                    gx0 = x
                elif x > gx1:
                    gx1 = x
                if y < gy0:
                    gy0 = y
                elif y > gy1:
                    gy1 = y
    if not any_cell:
        return 0
    # The solid footprint sits inside this box, and the halo is the box plus
    # one tile. Marking the halo covers every solid cell, so the footprint
    # is not recorded a second time.
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
        if mv is not None:
            for y in range(ry0, ry1 + 1):
                base = y * w
                for x in range(rx0, rx1 + 1):
                    if ids[base + x] != idx or mv[base + x] & flag:
                        blocked = True
                        break
                if blocked:
                    break
        else:
            for y in range(ry0, ry1 + 1):
                base = y * w
                for x in range(rx0, rx1 + 1):
                    if ids[base + x] != idx or (x, y) in occupied or (x, y) in keep_clear:
                        blocked = True
                        break
                if blocked:
                    break
        if blocked:
            continue
        for role, dx, dy, o in pieces:
            plan.furniture.append((role, ox + dx, oy + dy, o))
        # The whole footprint and its ring stay clear of the next copy, so
        # tables in a classroom keep an aisle between them.
        if plan.occ is not None:
            plan.occ[ry0:ry1 + 1, rx0:rx1 + 1] |= np.uint8(B_ITEM)
        else:
            occupied.update((x, y) for y in range(ry0, ry1 + 1)
                            for x in range(rx0, rx1 + 1))
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
    if plan._ids is None:
        _bind_ids(plan)
    _occ_mark(plan, door_tiles, B_DOOR)
    _occ_mark(plan, stair_tiles, B_STAIR)
    # Door clearance and the flight do not change from room to room.
    keep_clear = door_tiles | stair_tiles

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
        # Walls this room has, as (x, y, facing) for a piece standing on the
        # tile with its back to that wall.
        slots = _wall_slots(plan, idx, r, stair_tiles)
        used_walls: set[tuple[int, int, str]] = set()
        # Outside walls are where the windows go, and the windows are laid out
        # last, in columns up the facade: a painting or switch hung there took
        # the bay and left a hole in the column. Hang things inside first.
        # One probe per slot. Each later sort used to ask the grid again.
        facade: dict[tuple[int, int, str], bool] = {}
        for slot in slots:
            sx, sy, facing = slot
            ox, oy = _STEP[facing]
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
        # Every room keeps one. The game lights a room from the switch in
        # it, so a room without one is dark whatever the sprite count says -
        # tools/audit_layouts.py fails a plan for it. Thinning these out to
        # match Knox County's 0.12 per 10 m2 left 1,945 rooms unlit, and it
        # was chasing the wrong thing anyway: the round fittings that read as
        # clocks were school_desk, not these.
        floor_switch = 1
        want_switches = max(floor_switch,
                            min(MAX_SWITCHES,
                                r.area // LIGHT_EVERY_TILES))
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
        if not lit and want_switches:
            for x, y, facing in by_reach:
                if hang(SWITCH, x, y, facing):
                    break

        # Nothing standing goes in the stair hall or corridor: a flat's front
        # door and the only way past the flight both run through it, and one
        # bookcase beside the stairs closes the corridor.
        if r.is_core:
            # The walls are fair game, and a rug is walked over. Knox County's
            # halls carry 7 pieces per 10 m2; hanging one every third slot and
            # nothing else left ours at 1.6, which is what a corridor of blank
            # walls reads as.
            inside = [s for s in slots if not facade[s]]
            outside = [s for s in slots if facade[s]]
            # Inside walls first and most of the facade left alone, because
            # the windows are laid out last and in columns: a picture on a
            # bay takes it and leaves a hole up the front of the building.
            hung = 0
            for n, slot in enumerate(
                    inside + outside[:max(1, len(outside) // CORE_FACADE_SHARE)]):
                if n % CORE_ART_EVERY:
                    continue
                art_set = (CORE_WALL_ART_CIVIC
                           if plan.kind in ART_FREE_KINDS else CORE_WALL_ART)
                if not art_set:
                    break
                if hang(art_set[hung % len(art_set)], *slot):
                    hung += 1
            laid = 0
            ids = plan._ids
            span = plan.width
            for n, (x, y) in enumerate(
                    (x, y) for y in range(r.y0, r.y1 + 1)
                    for x in range(r.x0, r.x1 + 1)
                    if ids[y * span + x] == idx and (x, y) not in keep_clear):
                if n % CORE_RUG_EVERY:
                    continue
                # A rug is two or three tiles wide, so every tile of it has to
                # be this room's floor and clear; one hanging over the edge is
                # a rug through the wall.
                order = CORE_RUGS[laid % len(CORE_RUGS):] + CORE_RUGS[:laid % len(CORE_RUGS)]
                for role in order:
                    cells = _cells_for(role, x, y, "N")
                    if any(_room_at(plan, cx, cy) != idx or (cx, cy) in keep_clear
                           for cx, cy in cells):
                        continue
                    plan.furniture.append((role, x, y, "N"))
                    laid += 1
                    break
            continue
        # A shop's sales floor is fitted out as a whole - rows of shelving,
        # the till by the door - not from a list pushed against its walls.
        if r.kind in interiors.STORES and interiors.furnish_store(
                plan, idx, r, rng, door_tiles, stair_tiles, street,
                _wall_lines(plan, idx, slots, stair_tiles),
                _outside_counts(plan, idx, slots, facade, stair_tiles)):
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
            base = interiors.furnish_dining(
                plan, idx, r, rng, occupied, stair_tiles, door_tiles, street,
                _outside_counts(plan, idx, slots, facade, stair_tiles))
        elif commercial_kitchen:
            base = interiors.KITCHEN_KIT.get(r.kind, interiors.DEFAULT_KITCHEN_KIT)
        elif r.kind == "bathroom" and plan.kind not in HOUSE_LIKE_KINDS | {"apartment"}:
            # A shop's or an office's lavatory, not a family bathroom with a
            # bath. The game gives both the same room name, so the basin is
            # what tells them apart: a household bathroom has
            # fixtures_sinks_01_0-3 at 1.03 a room and a public one at 0.08,
            # and the public one has sink_public instead. Two cubicles and a
            # row of basins, no bath.
            base = ["toilet", "sink_public", "toilet", "sink_public",
                    "mirror"]
        elif plan.kind in CIVIC_KINDS and r.kind in CIVIC_ROOMS:
            # A police station's corridor is not somebody's hallway. The
            # wishlist for hall and lobby is a domestic one - side table,
            # picture, bookcase, sofa - and a station was getting 8.2 side
            # tables, 6.9 chests and 5.3 pictures a building, which is what
            # reads as a living room with a cell block attached. Knox County
            # furnishes a police office out of location_business_office at
            # 3.15 pieces per 10 m2 and almost nothing else.
            base = list(CIVIC_ROOMS[r.kind])
        # A dining room's door halo stays on its own tiles. The pieces hung
        # after it test door and furniture bits, not that halo, and the next
        # room never reads these tiles.
        shelving = [s for s in SHELVING.get(r.kind, DEFAULT_SHELVING)
                    if s in C.FURNITURE] or ["shelf"]
        if plan.kind in HOME_KINDS:
            # No warehouse racking in somebody's house.
            keep = [s for s in shelving if s not in WAREHOUSE_ROLES]
            shelving = keep or ["shelf"]
        base = [rng.choice(shelving) if role == "shelf" else pal.get(role, role)
                for role in base]
        if plan.kind in HOME_KINDS:
            base = [HOME_STORE.get(role, role) for role in base]
        if _erika_ready():
            # With Erika's Tiles installed, pictures and plants come from its
            # far larger range, so no two living rooms hang the same print.
            shop = r.kind in SHOP_DECOR_ROOMS
            base = [rng.choice(C.ERIKA_SHOP_ADS) if role == "painting" and shop and C.ERIKA_SHOP_ADS
                    else rng.choice(C.ERIKA_WALL_ART)
                    if role in ("painting", "mirror") and C.ERIKA_WALL_ART
                    and plan.kind not in ART_FREE_KINDS
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
        # A house's laundry is mostly not in the kitchen. Knox County has a
        # washer or dryer in 16% of its kitchens; ours had one in 80%, because
        # anything on the wishlist proper is placed every time. Added before
        # the target is counted, so it is one of the pieces the room is worth
        # rather than an extra on top.
        if r.kind == "kitchen" and rng.random() < LAUNDRY_IN_KITCHEN:
            base = base + ["washer"]
        # One piece per four tiles, which is 2.5 per 10 m2 - about what a
        # living room or a bedroom holds. A room packed with furniture by
        # its nature gets more: Knox County's classrooms run to 8.2 pieces
        # per 10 m2, nearly all of them desks, and at the standard rate ours
        # could only reach 1.36 of 2.97.
        per = ROOM_DENSITY.get(r.kind, 4)
        target = max(len(base), min(40, r.area // per))
        if eatery or commercial_kitchen or r.kind == "theatre":
            target = len(base)       # fitted out; nothing more to scatter
        wishlist = []
        art = (WALL_ART_CAP if plan.kind in ART_FREE_KINDS
               else 0 if rng.random() < WALL_ART_CHANCE else WALL_ART_CAP)
        for i in range(target):
            role = base[i % len(base)]
            if i >= len(base) and _once(role):
                continue
            # Pictures and mirrors are the one thing that repeats freely, so
            # scaling the list with floor area turned a big room into a
            # gallery. Everything else keeps its share.
            if role in ("painting", "mirror") or role.startswith(("erika_art", "erika_ad")):
                if art >= WALL_ART_CAP:
                    continue
                art += 1
            # Counters have a budget of their own (_counter_runs), and the
            # wishlist cycling round put a big kitchen over it before the
            # budget was ever consulted.
            if role.startswith("counter") and wishlist.count(role) >= WISHLIST_COUNTERS:
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
        # Inside walls first. The facade flags do not change as pieces land,
        # so the order is sorted once rather than for every hung piece.
        wall_order = sorted(floor_slots, key=facade.__getitem__)
        for role in wishlist:
            if _is_wall_piece(role):
                for x, y, facing in wall_order:
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
                _note(plan, occupied, cells)
                _note(plan, occupied, front)
                _occ_mark(plan, cells, B_ITEM)
                _occ_mark(plan, front, B_ITEM)
                plan.furniture.append((role, x, y, orient))
                break

        if commercial_kitchen:
            # Steel counters, and no wall cupboards or microwave. A working
            # kitchen really is worktop wall to wall, so it keeps no budget.
            _counter_runs(plan, idx, slots, occupied, door_tiles, stair_tiles,
                          "counter_2", cabinets=False, start=room_furniture_start)
        elif r.kind in KITCHENS:
            _counter_runs(plan, idx, slots, occupied, door_tiles, stair_tiles,
                          pal.get("counter", "counter"), area=r.area, rng=rng,
                          start=room_furniture_start)
        _stand_on_something(plan, idx, r, pal, room_furniture_start)


# Pieces drawn at worktop height: a sink, a television, a table lamp, a pot
# plant. Put down on their own they float over bare floor - a sink plumbed into
# the ground - so each gets something to stand on first, the way the game's
# own houses have them. Found by the height of their sprites (a floating
# piece's lowest pixel sits well above the floor diamond); the television is
# drawn standing, but no home keeps one on the carpet.
# Pieces drawn with their base part-way up the tile, so they need something
# under them. Sinks are matched by name as well: three more were added with
# the sink variety and every one of them hung in the air, because the set was
# a list of the two that existed at the time.
SURFACE_ROLES = {"kitchen_sink", "sink", "lamp", "tv", "register"}


def _is_sink(role: str) -> bool:
    return role.startswith(("sink", "kitchen_sink"))
WORKTOP_ROOMS = ({"kitchen", "bathroom", "laundry", "breakroom", "openplan"}
                 | KITCHENS)
# Opened from the front: the tile before them is kept clear.
FRONT_CLEAR_ROLES = {"fridge", "stove", "stove_alt", "washer", "dryer", "wardrobe",
                     "bookshelf", "dresser", "dresser_alt", "filing_cabinet"}
DOOR_CLEAR_DEPTH = 2


def _needs_front(role: str) -> bool:
    return role in FRONT_CLEAR_ROLES or role.startswith(("wardrobe", "dresser", "fridge"))


def _needs_surface(role: str) -> bool:
    return role in SURFACE_ROLES or _is_sink(role) or role.startswith("erika_plant")


# Sprite geometry, read off the tiles themselves. A small thing is drawn with
# its base part-way up its 256-pixel tile, and whatever it stands on has to
# reach that high or it hangs in the air: a lamp's base is 153 pixels down and
# a pot plant's 151, a bedside chest's top edge is at 97 and a counter's at
# 125, but a coffee table's is at 172. A lamp on a coffee table floated a
# quarter of a tile above it. Knox County stands 180 of its 270 table lamps on
# furniture_storage_01, a bedside chest, and not one of them on a low table.
LOW_TABLES = {"table", "sidetable", "coffee_table"}
# What goes under one instead. Both of these are tiles the game itself puts
# lamps on (furniture_storage_01 8 and 12).
NIGHTSTANDS = ("dresser", "dresser_alt")


def _stand_on_something(plan: Plan, idx: int, room: Room, palette: dict,
                        start: int = 0) -> None:
    """A counter, a chest or a table under every piece of this room that needs
    one and does not have one - and a taller one under anything left standing
    on a piece too low to reach it."""
    standing: set[tuple[int, int]] = set()
    low: dict[tuple[int, int], int] = {}
    mine = []
    for n in range(start, len(plan.furniture)):
        role, x, y, o = plan.furniture[n]
        if _room_at(plan, x, y) != idx or _is_wall_piece(role):
            continue
        mine.append(n)
        if _needs_surface(role):
            continue
        cells = _cells_for(role, x, y, o)
        if role in LOW_TABLES:
            for cell in cells:
                low[cell] = n
        else:
            standing.update(cells)
    added, swapped = [], []
    for n in mine:
        role, x, y, o = plan.furniture[n]
        if not _needs_surface(role) or (x, y) in standing:
            continue
        if room.kind in WORKTOP_ROOMS or _is_sink(role) or role == "register":
            support = palette.get("counter", "counter")
        elif role == "tv":
            support = "dresser"
        else:
            support = ("filing_cabinet"
                       if plan.kind in CIVIC_KINDS
                       and "filing_cabinet" in C.FURNITURE
                       else NIGHTSTANDS[(x + y) % len(NIGHTSTANDS)])
        if support not in C.FURNITURE:
            continue
        # A lamp already sitting on a coffee table: the table goes, the chest
        # takes its place, rather than two pieces on the one tile.
        under = low.pop((x, y), None)
        if under is not None:
            swapped.append((under, (support, x, y, _facing(support, o))))
        else:
            added.append((n, (support, x, y, _facing(support, o))))
        standing.add((x, y))
    for n, piece in swapped:
        plan.furniture[n] = piece
    # Each support goes in just before its piece, so it is drawn underneath.
    # One rebuild of this room's tail, rather than sliding the list once per lamp.
    if added:
        inserts = {n: piece for n, piece in added}
        rebuilt = []
        for n in range(start, len(plan.furniture)):
            extra = inserts.get(n)
            if extra is not None:
                rebuilt.append(extra)
            rebuilt.append(plan.furniture[n])
        plan.furniture[start:] = rebuilt


# Counter and cupboard are the same tileset (fixtures_counters_01: the wall
# cupboards are 24-27), so Knox County's kitchens are counted together: 3.9
# pieces over a median 21 m2, which is the rate below. Filling both long walls
# end to end instead put 7.1 in ours - a galley of worktop with a gangway down
# the middle. The figure this was first tuned against, 12 per 10 m2, was every
# piece of furniture in the room, not the counters.
COUNTER_PER_M2 = 0.186
# Counters come first out of that budget and cupboards take what is left, so a
# small kitchen is worktop rather than cupboard doors.
CABINET_SHARE = 0.4
# Knox County cooks on 1.2 appliances and the stove is one of them, so a
# microwave is the exception rather than the rule.
MICROWAVE_SHARE = 0.2
# How often a house keeps its washing machine in the kitchen rather than a
# laundry, a bathroom or the garage. Knox County: 16% of kitchens.
LAUNDRY_IN_KITCHEN = 0.16

# What a room kind's floor may be, in the shares Knox County lays them. Each
# building draws once per kind, so a town has variety between houses while one
# house stays coherent. Measured over 86,045 interior tiles: a bedroom is
# tilesandwood_01_42 a third of the time and carpet the rest, a bathroom is
# almost always hard tile, a hall has its own runner.
FLOOR_CHOICES = {
    "bedroom":     ["wood_pale", "carpet_brown", "carpet_grey",
                    "carpet_green", "carpet_beige"],
    "kidsbedroom": ["carpet_grey", "carpet_brown", "carpet_green",
                    "wood_pale", "carpet_red"],
    "livingroom":  ["wood_pale", "wood_pale", "carpet_brown", "wood_mid",
                    "carpet_grey"],
    "openplan":    ["wood_pale", "wood_mid", "carpet_brown", "wood_dark"],
    "dining":      ["wood_pale", "wood_pale", "wood_mid", "carpet_grey"],
    "diningroom":  ["wood_pale", "wood_pale", "wood_mid", "carpet_grey"],
    "kitchen":     ["wood_pale", "wood_mid", "tile_small", "tile_grey"],
    "bathroom":    ["tile_white", "tile_white", "tile_grey", "tile_cream",
                    "wood_mid"],
    # No hall_runner (tilesandwood_01_28). It was picked because it is 44% of
    # the hall *tiles* in Knox County - but that is a handful of big halls,
    # and by room it is 2 of 33. The halls the game actually builds are
    # tilesandwood 42, 45, 44 and 17, which are plain; _28 has a line through
    # it that tiles into a grid across a floor.
    "hall":        ["wood_pale", "wood_mid", "office_grey", "civic_pale"],
    "closet":      ["wood_pale", "wood_pale", "wood_pale", "wood_dark"],
    "office":      ["wood_mid", "carpet_red", "carpet_brown", "carpet_grey"],
}

# Floors for the rooms of a particular kind of building, where they differ
# from the house ones above. A station's hall is not somebody's hallway, and
# the runner measured over houses looked like carpet down the middle of a
# police station. Knox County floors its stations hard throughout - its cells
# are one tile and nothing else - and the offices it does carpet are left
# uncarpeted here on purpose.
KIND_FLOOR_CHOICES = {
    "school": {
        "classroom":    ["wood_pale", "school_pale", "school_warm"],
        "secondaryclassroom": ["wood_pale", "school_pale"],
        "schoollab":    ["wood_pale", "school_pale"],
        "gym":          ["wood_pale"],
        "library":      ["school_wood", "wood_pale"],
        "lobby":        ["school_pale", "school_warm", "wood_mid",
                         "school_wood"],
        "hall":         ["school_pale", "wood_pale", "school_warm"],
        "diningroom":   ["wood_pale", "school_pale"],
        "schoolstorage": ["school_warm", "wood_pale"],
        "sportstorage": ["school_warm", "wood_pale"],
        "office":       ["school_wood", "wood_mid"],
        "janitor":      ["civic_scuff", "school_warm"],
        "bathroom":     ["tile_white", "tile_cream", "civic_pale"],
    },
    "police": {
        "hall":              ["civic_hall", "civic_pale", "civic_grey"],
        "prisoncells":       ["civic_hall"],
        "policeoffice":      ["civic_pale", "civic_grey", "civic_hall"],
        "policehall":        ["civic_hall", "civic_pale"],
        "policelocker":      ["civic_pale", "civic_scuff"],
        "policegunstorage":  ["civic_pale", "civic_grey", "civic_scuff"],
        "policeoutfitstorage": ["civic_pale", "civic_scuff"],
        "policearchive":     ["civic_pale", "civic_grey"],
        "officestorage":     ["civic_pale", "civic_scuff"],
        "interrogationroom": ["civic_pale", "civic_grey"],
        "security":          ["civic_pale", "civic_grey"],
        "breakroom":         ["civic_pale", "civic_worn"],
        "janitor":           ["civic_scuff", "civic_worn"],
        "bathroom":          ["civic_pale", "civic_worn"],
        "office":            ["civic_pale", "civic_grey"],
        "storage":           ["civic_scuff", "civic_pale"],
    },
}
# The most counters a wishlist may ask for however big the room; past this the
# budget above decides.
WISHLIST_COUNTERS = 2


def _counter_runs(plan: Plan, idx: int, slots, occupied: set,
                  door_tiles: set, stair_tiles: set, counter: str = "counter",
                  cabinets: bool = True, area: int = 0,
                  rng: random.Random | None = None, start: int = 0) -> None:
    """Fitted counters along a kitchen's two longest walls, up to what the
    floor is worth.

    Knox County sets the sink and stove into a run of worktop rather than
    standing them against bare wall, so the gaps along the two longest walls
    fill with counters - but only as far as COUNTER_PER_M2, and never in front
    of a door. Whatever the wishlist placed stays and counts against the same
    budget.
    """
    rng = rng or random.Random(idx)
    # One budget for the whole tileset, counters and cupboards together, since
    # that is how the 3.9 was counted. Spending it on counters first and then
    # hanging cupboards on top of it is what left ours at 6.6.
    budget = max(3, round(area * COUNTER_PER_M2)) if area else 99
    room_cabinets = max(1, round(budget * CABINET_SHARE)) if area else 99
    already = sum(1 for role, x, y, _o in plan.furniture[start:]
                  if role.startswith("counter") and _room_at(plan, x, y) == idx)
    left = max(0, budget - room_cabinets - already)
    placed = []
    by_wall: dict[str, list[tuple[int, int, str]]] = {}
    for x, y, facing in slots:
        by_wall.setdefault(facing, []).append((x, y, facing))
    for facing in sorted(by_wall, key=lambda f: -len(by_wall[f]))[:2]:
        for x, y, _f in by_wall[facing]:
            if len(placed) >= left:
                break
            orient = _facing(counter, facing)
            cells = _cells_for(counter, x, y, orient)
            if _busy_any(plan, cells, B_DOOR | B_STAIR | B_ITEM,
                         occupied, door_tiles, stair_tiles):
                continue
            if any(_room_at(plan, cx, cy) != idx for cx, cy in cells):
                continue
            # A corner tile is on two walls; one counter is enough.
            _note(plan, occupied, cells)
            _occ_mark(plan, cells, B_ITEM)
            plan.furniture.append((counter, x, y, orient))
            placed.append((x, y, orient))

    # Cupboards on the wall above the counters, and sometimes a microwave on
    # one. North and west walls only, as for everything fixed to a wall; the
    # windows then keep off those tiles.
    if not cabinets:
        return
    hung = 0
    microwave = rng.random() >= MICROWAVE_SHARE      # True means: already done
    for role, x, y, orient in plan.furniture[start:]:
        if hung >= room_cabinets:
            break
        if role != counter or orient not in ("N", "W") or _room_at(plan, x, y) != idx:
            continue
        edge = _wall_edge(x, y, orient)
        if edge in plan.wall_pieces or _room_at(plan, *((x, y - 1) if orient == "N" else (x - 1, y))) == idx:
            continue
        plan.furniture.append(("wall_cabinet", x, y, _facing("wall_cabinet", orient)))
        plan.wall_pieces.add(edge)
        hung += 1
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
MANY = 1 << 30     # further than any search goes
# How many places a door stuck on a corner may be offered before it stays put.
DOOR_SPOTS_TRIED = 40
CORE_WIDE = 3      # the shaft is the flight plus a landing beside it


CORRIDOR_WIDE = 3   # a landing wide enough to be circulation, not a cupboard
# How wide a corridor is, in the shares Knox County builds them: of its 259
# school corridor rectangles the narrow side is 3 in 101, 4 in 45, 2 in 43
# and 6 in 23. Ours was always exactly 3, which is the commonest but the
# only one we ever built.
CORRIDOR_WIDTHS = (2, 3, 3, 3, 4, 4, 6)


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
    # Width is the one draw. The strip itself is the best score, not a random
    # one: moving it would shift every later storey.
    along_y = height >= width
    span = height if along_y else width
    across = width if along_y else height
    # This building's corridor width, narrowing if the floor is too thin
    # for the one drawn.
    wide = _rng.choice(CORRIDOR_WIDTHS)
    while wide > CORRIDOR_WIDE and across < wide + 2 * MIN_ROOM:
        wide -= 1
    if span < STAIR_RUN + 2 or across < wide + 2 * MIN_ROOM:
        return None
    if mask is None:
        mask_arr = np.ones((height, width), dtype=bool)
    else:
        mask_arr = _as_mask(mask)
    table = grids.sat(mask_arr)
    if along_y:
        extent = np.flatnonzero(mask_arr.any(axis=1))
        ys, xs = grids.window_full(table, 1, wide)
    else:
        extent = np.flatnonzero(mask_arr.any(axis=0))
        ys, xs = grids.window_full(table, wide, 1)
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
            centre = abs((start + wide / 2) - across / 2)
            score = (-length, centre)
            if best is not None and score >= best[0]:
                continue
            if along_y:
                rect = (start, run_start, start + wide - 1, a - 1)
            else:
                rect = (run_start, start, a - 1, start + wide - 1)
            best = (score, rect)
        return best

    best = None
    last = across - wide - MIN_ROOM
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


def _blocks(role: str) -> bool:
    """Whether a piece stands on the floor and stops you walking through."""
    return C.FURNITURE_LAYERS.get(role, "Furniture") == "Furniture"


def _tiles_under(role: str, x: int, y: int, orient: str):
    """Every tile a piece covers, from the offsets in its catalog entry: a
    double bed is four of them and a sofa two, not one each."""
    have = C.FURNITURE.get(role)
    if not have or orient not in have:
        yield x, y
        return
    for dx, dy in _offsets(role, orient):
        yield x + dx, y + dy


def _ways_in(building: "Building", level: int, storey: "Plan") -> set:
    """The tiles a player arrives on: the outside doors on the ground floor,
    the stairs on every floor above."""
    w, h = storey.width, storey.height
    ids = storey._ids if storey._ids is not None else _bind_ids(storey)

    def room(x, y):
        return ids[y * w + x] if 0 <= x < w and 0 <= y < h else 0

    out = set()
    if level == 0:
        for (x, y, d) in storey.doors:
            a = (x - 1, y) if d == "W" else (x, y - 1)
            if room(*a) and not room(x, y):
                out.add(a)
            elif room(x, y) and not room(*a):
                out.add((x, y))
    for lvl, (sx, sy, sd) in enumerate(building.stairs):
        if lvl not in (level - 1, level):
            continue
        dx, dy = (0, 1) if sd == "N" else (1, 0)
        out |= {(sx + dx * i, sy + dy * i) for i in range(STAIR_RUN)
                if room(sx + dx * i, sy + dy * i)}
    return out


def _open_up(storey: "Plan", starts: set) -> set:
    """Which pieces have to go for every room to be walked into."""
    w, h = storey.width, storey.height
    ids = storey._ids if storey._ids is not None else _bind_ids(storey)
    doors = set(storey.doors)
    at: dict = {}
    for i, (role, fx, fy, orient) in enumerate(storey.furniture):
        if not _blocks(role):
            continue
        for tile in _tiles_under(role, fx, fy, orient):
            at.setdefault(tile, set()).add(i)

    def step(ax, ay, bx, by) -> bool:
        """Whether you can walk from one tile to the next: same room, or a
        doorway in the wall between them."""
        if not (0 <= bx < w and 0 <= by < h) or not ids[by * w + bx]:
            return False
        if ids[ay * w + ax] == ids[by * w + bx]:
            return True
        if bx == ax + 1:
            return (bx, by, "W") in doors
        if ax == bx + 1:
            return (ax, ay, "W") in doors
        if by == ay + 1:
            return (bx, by, "N") in doors
        return (ax, ay, "N") in doors

    # A 0-1 search out from the way in: stepping onto an empty tile is free,
    # stepping onto an occupied one costs the piece standing there. What comes
    # back is the fewest pieces that have to move for each room to open.
    best: dict = {}
    back: dict = {}
    queue = deque()
    for tile in starts:
        cost = 1 if tile in at else 0
        if cost < best.get(tile, MANY):
            best[tile], back[tile] = cost, None
            queue.appendleft(tile) if cost == 0 else queue.append(tile)
    while queue:
        x, y = queue.popleft()
        here = best[(x, y)]
        for nxt in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not step(x, y, *nxt):
                continue
            cost = here + (1 if nxt in at else 0)
            if cost < best.get(nxt, MANY):
                best[nxt], back[nxt] = cost, (x, y)
                queue.appendleft(nxt) if cost == here else queue.append(nxt)

    walked = {ids[y * w + x] for (x, y), cost in best.items() if cost == 0}
    want: dict = {}
    for (x, y), cost in best.items():
        idx = ids[y * w + x]
        if not idx or idx in walked or storey.rooms[idx - 1].is_shaft:
            continue
        if cost < want.get(idx, (MANY, None))[0]:
            want[idx] = (cost, (x, y))
    gone: set = set()
    for _cost, tile in want.values():
        while tile is not None:
            gone |= at.get(tile, set())
            tile = back.get(tile)
    return gone


def _clear_the_way(building: "Building") -> int:
    """Take out whatever stands between a doorway and the rooms behind it.

    Rooms are furnished one at a time, so nothing stopped a shelf landing in
    the only doorway or a counter spanning the only way through. The plan was
    connected - every audit of it passed - but the furnished building was not.
    """
    removed = 0
    for level, storey in enumerate(building.storeys):
        starts = _ways_in(building, level, storey)
        if not starts:
            continue
        for _round in range(6):
            gone = _open_up(storey, starts)
            if not gone:
                break
            storey.furniture = [f for i, f in enumerate(storey.furniture)
                                if i not in gone]
            removed += len(gone)
    return removed


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
    elif kind in NEEDS_CORRIDOR and (
            width * height >= CORRIDOR_WORTH_IT
            * (settings.room_size if settings else Settings().room_size)
            * KIND_ROOM_SCALE.get(kind, 1.0)):
        # A school or a clinic is rooms off a corridor. In Knox County a
        # school's halls are 11% of its rooms and 21% of its floor, and they
        # average 122 tiles: one long one serving a storey, not a dozen square
        # ones relabelled, which is what read as corridors all the way down.
        # Only where there are rooms enough to be worth serving - a two-room
        # library with a corridor through it is a third corridor.
        core = _pick_corridor(width, height, mask, rng)
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
    # Last of all, because it is the furnished building that has to be
    # walkable, not the plan.
    _clear_the_way(building)
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
# A great hall and a concourse are big rooms; neither building is cut into
# bedsits.
KIND_ROOM_SCALE = {"industrial": 6.0, "barn": 5.0, "shed": 8.0, "church": 12.0,
                   "castle": 14.0, "stadium": 11.0, "mall": 16.0,
                   "shop": 2.0, "school": 9.0, "civic": 2.5,
                   "restaurant": 1.5, "medical": 1.3, "offices": 3.0,
                   "police": 2.0, "library": 8.0, "fire": 4.0,
                   "military": 4.0}
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
# ...except where the game's own buildings are plainly bigger. Every room in
# Knox County, by what it is called: a church is 39 tiles at the median but
# 340 at the ninth decile and 1132 at the largest, which is one nave with
# small rooms off it; a library room is 100 at the median and 282 at the
# decile; a gym 88 and 314; a warehouse 134 and 461. Held to 120 whatever the
# building was, a church and a library came out as a grid of cubicles where
# the game has a hall. The loot allowance above is the reason for the cap, and
# these are the buildings the game itself spends it on.
KIND_MAX_ROOM = {"church": 340, "library": 280, "school": 170, "gym": 320,
                 "industrial": 460, "barn": 460, "shed": 460, "military": 150,
                 "civic": 170, "fire": 220, "medical": 140,
                 # A great hall and a concourse. Without an entry here the cap
                 # is MAX_ROOM_AREA whatever KIND_ROOM_SCALE says, which is
                 # why no room-size setting moved either of these.
                 "castle": 340, "stadium": 460,
                 # Knox County's mall units: a clothes shop is 306 tiles at
                 # the median, a department store 441, a furniture shop 379.
                 "mall": 440}


def _room_cap(kind: str | None) -> int:
    return KIND_MAX_ROOM.get(kind or "", MAX_ROOM_AREA)


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
    target = min(max(target, floor_tiles / MAX_ROOMS_PER_FLOOR),
                 _room_cap(mix_kind))

    if kind == "apartment":
        _apartment_rooms(plan, rng, target, HOTEL_FRONTAGE if hotel else FLAT_FRONTAGE)
    elif shop_floor and kind in ("shop", "restaurant"):
        _shop_rooms(plan, rng, street)
    elif mix_kind == "mall":
        _mall_rooms(plan, rng, target, level)
    else:
        # A house floor gets one bathroom-sized room; anything else cut this
        # way - a factory floor, a civic building - has no bathroom to put in
        # it and is left alone.
        house_floor = not (shop_floor or commercial
                           or (mix_kind and mix_kind in SPECIAL_MIXES))
        _split(0, 0, width - 1, height - 1, rng, MAX_DEPTH, plan.rooms,
               target_area=target, mask=mask, small=[house_floor])
    _paint(plan)

    # Kinds are chosen after painting, from the plan as it really is: what a
    # room is depends on what it borders, and that is only known once the
    # footprint and the shaft have had their say.
    if kind == "apartment":
        _unit_touches_corridor(plan)
        # Trade a room between neighbours before the kinds are handed out, so
        # a flat that has gained or lost one is furnished for the size it
        # ended up, not the size it was cut.
        _stagger_flats(plan, rng)
        _unit_touches_corridor(plan)
        _assign_flat_kinds(plan, hotel=hotel, rng=rng)
    elif shop_floor:
        if kind == "restaurant" and not uses:
            uses = [("restaurantdining", "restaurantkitchen")]
        _assign_shop_floor(plan, rng, street, uses, several=(kind == "retail"))
    elif mix_kind and mix_kind in SPECIAL_MIXES:
        mix, fill = SPECIAL_MIXES[mix_kind]
        # A room whose kind is already decided - the mall concourse - keeps
        # it; the mix is dealt to the rest.
        rooms = [r for r in plan.rooms if not r.is_core and not r.fixed]
        _assign_kinds(rooms, mix, fill)
        if mix_kind in NEEDS_CORRIDOR:
            _circulation(plan, rooms)
        if mix_kind in MERGE_CIRCULATION:
            _merge_halls(plan)
    elif commercial:
        rooms = [r for r in plan.rooms if not r.is_core]
        _assign_kinds(rooms, COMMERCIAL, COMMERCIAL_FILL)
        _circulation(plan, rooms)
    else:
        _assign_house_kinds(plan, level, levels, stairs, rng)
    for room in plan.rooms:
        if room.is_core:
            room.kind = "hall"
    if shaft is not None:
        _carve_shaft(plan, shaft, shaft_door)

    # A cell is a cell: rooms far bigger than their kind ever is are cut down
    # now the kind is known, before any door is placed on them.
    _split_to_size(plan, rng)

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
    if mix_kind == "mall":
        _clear_concourse(plan)
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
