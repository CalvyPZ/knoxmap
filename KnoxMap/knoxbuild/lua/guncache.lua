-- Written into every KnoxMap map. One military rifle, guaranteed.
--
-- The game's M16 spawns from army and police loot and almost nowhere else,
-- and a real town has neither an army checkpoint nor, usually, a gun shop.
-- On a map generated from a quiet suburb there may be no container anywhere
-- that could ever roll one, which reads as the map being broken rather than
-- as the town being peaceful. knoxbuild/guns.py picks the best building the
-- map actually has - an army building, else the police station, else a gun
-- shop, else a house out on the edge - and each map ships a small file next
-- to this one registering that building's box.
--
-- Here it waits for the game to fill a container inside one of those boxes
-- and drops the rifle in. It cannot be baked into the .tbx: a .tbx holds
-- walls, floors and furniture and no items at all, and what is in a
-- container is rolled by the game the first time somebody walks in.
--
-- Once per map per save, remembered in ModData, so it survives a reload and
-- does not turn the police station into an armoury.
--
-- One map's copy of this file serves them all: the first to load claims it,
-- and every map's own file adds its box to the same list whichever of the
-- two the game happens to load first.

KnoxMapGunCache = KnoxMapGunCache or { boxes = {} }

if KnoxMapGunCache.wired then return end
KnoxMapGunCache.wired = true

local MODDATA = "KnoxMapGunCache"

-- Not a corpse, not a player, not the ground: somewhere a rifle can sit and
-- be found. The rifle weighs 4 and the magazines and ammunition a little
-- more, so a container that cannot hold that much is passed over and the
-- next one inside the same building is tried instead.
local SKIP = { inventorymale = true, inventoryfemale = true, floor = true }
local NEEDED_CAPACITY = 12

--- The container out of whatever OnFillContainer was called with.
-- The event passes (roomName, containerType, container), but rather than
-- trust an argument order across game versions this takes whichever of them
-- answers getSourceGrid - only a container does.
local function asContainer(value)
    if value == nil then return nil end
    local ok, grid = pcall(function() return value:getSourceGrid() end)
    if ok and grid then return value end
    return nil
end

local function boxFor(x, y)
    for _, box in ipairs(KnoxMapGunCache.boxes) do
        if x >= box.x and x < box.x + box.w
            and y >= box.y and y < box.y + box.h then
            return box
        end
    end
    return nil
end

local function onFillContainer(a, b, c)
    if #KnoxMapGunCache.boxes == 0 then return end
    local container = asContainer(c) or asContainer(b) or asContainer(a)
    if not container then return end
    if SKIP[container:getType()] then return end

    local square = container:getSourceGrid()
    local box = boxFor(square:getX(), square:getY())
    if not box then return end

    local filled = ModData.getOrCreate(MODDATA)
    if filled[box.id] then return end

    -- Big enough to hold it? If not, leave the flag alone: the next container
    -- in the same building gets the chance instead.
    local capacity = container:getMaxWeight()
    if capacity and capacity < NEEDED_CAPACITY then return end

    for _, item in ipairs(box.items or KnoxMapGunCache.ITEMS) do
        container:AddItem(item)
    end
    filled[box.id] = true
    print("KnoxMap: left a rifle in " .. tostring(box.id) ..
          " (" .. tostring(box.kind) .. ") at " ..
          tostring(square:getX()) .. "," .. tostring(square:getY()))
end

-- The fallback list, for a map file written before the items were recorded
-- in it. Checked against the game's own scripts: AssaultRifle takes
-- MagazineType Base.556Clip and AmmoType base:bullets_556.
KnoxMapGunCache.ITEMS = { "Base.AssaultRifle", "Base.556Clip", "Base.556Clip",
                          "Base.556Box" }

Events.OnFillContainer.Add(onFillContainer)
