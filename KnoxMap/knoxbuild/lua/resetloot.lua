-- Written into every map KnoxMap installs. Fresh loot, without a new save.
--
-- The game fills a container the first time its part of the map is loaded and
-- then remembers it as looted, so a map installed again - or a shop whose
-- rooms changed in a newer KnoxMap - keeps whatever it rolled the first time,
-- and the usual advice is to start a new save. This puts loot back instead:
-- right-click the ground and pick "Reset loot", for the building under the
-- cursor or for everything around it.
--
-- It does what the game's own "Refill container" does (ISInventoryPage.lua):
-- forget the room's procedural containers, empty the container, fill it again
-- from the room's loot tables. Everything inside is thrown away first, so it
-- asks before it does anything, and it never touches a vehicle, a corpse or a
-- player's own inventory.
--
-- One map's copy of this file serves them all: the first to load claims it.

if KnoxMapResetLoot then return end
KnoxMapResetLoot = {}

KnoxMapResetLoot.RADIUS = 30        -- tiles, for "everything around here"
KnoxMapResetLoot.LEVELS = 8         -- storeys to sweep

local SKIP = { inventorymale = true, inventoryfemale = true, floor = true }

local function eachContainer(square, found)
    if not square then return end
    local objects = square:getObjects()
    for i = 0, objects:size() - 1 do
        local object = objects:get(i)
        if not instanceof(object, "BaseVehicle") and not instanceof(object, "IsoDeadBody") then
            for c = 0, object:getContainerCount() - 1 do
                local container = object:getContainerByIndex(c)
                if container and not SKIP[container:getType()] and container:getSourceGrid() then
                    table.insert(found, container)
                end
            end
        end
    end
end

local function refill(container, player)
    local room = container:getSourceGrid():getRoom()
    local def = room and room:getRoomDef()
    if def and def:getProceduralSpawnedContainer() then
        def:getProceduralSpawnedContainer():clear()
    end
    container:removeItemsFromProcessItems()
    container:clear()
    ItemPicker.fillContainer(container, player)
    if container:getParent() then
        ItemPicker.updateOverlaySprite(container:getParent())
    end
    container:setExplored(true)
end

--- Every container in the building under `square`, or nil when there is none.
function KnoxMapResetLoot.inBuilding(square)
    local building = square and square:getBuilding()
    local def = building and building:getDef()
    if not def then return nil end
    local found = {}
    for x = def:getX(), def:getX2() do
        for y = def:getY(), def:getY2() do
            for z = def:getMinLevel(), def:getMaxLevel() do
                local at = getCell():getGridSquare(x, y, z)
                -- Its own tiles only: a building's box can take in the one
                -- next door, and a terrace is a row of them.
                if at and at:getBuilding() == building then
                    eachContainer(at, found)
                end
            end
        end
    end
    return found
end

--- Every container within `radius` tiles of a square, on every storey.
function KnoxMapResetLoot.around(square, radius)
    local found = {}
    if not square then return found end
    local cx, cy = square:getX(), square:getY()
    for x = cx - radius, cx + radius do
        for y = cy - radius, cy + radius do
            for z = 0, KnoxMapResetLoot.LEVELS - 1 do
                eachContainer(getCell():getGridSquare(x, y, z), found)
            end
        end
    end
    return found
end

--- Refill them all. Returns how many were filled.
function KnoxMapResetLoot.refill(containers, player)
    local done = 0
    for _, container in ipairs(containers) do
        refill(container, player)
        done = done + 1
    end
    return done
end

local function confirm(player, what, containers)
    -- The game fills containers on the server, and a client cannot ask it to
    -- do it again; this is for a game you host yourself.
    if isClient() then
        player:Say("Resetting loot only works in single player.")
        return
    end
    if #containers == 0 then
        player:Say("Nothing to refill here.")
        return
    end
    local text = string.format(
        "Reset the loot in %d containers %s?\nAnything stored in them is lost.",
        #containers, what)
    local modal = ISModalDialog:new(0, 0, 320, 140, text, true, nil, function(_, button)
        if button.internal ~= "YES" then return end
        local filled = KnoxMapResetLoot.refill(containers, player)
        player:Say(string.format("Refilled %d containers.", filled))
    end)
    modal:initialise()
    modal:addToUIManager()
    modal.moveWithMouse = true
    modal:setAlwaysOnTop(true)
end

local function onFillWorldObjectContextMenu(playerNum, context, worldObjects)
    local player = getSpecificPlayer(playerNum)
    local square = worldObjects and worldObjects[1] and worldObjects[1]:getSquare()
    if not player or not square then return end
    local menu = context:addOption("Reset loot", nil, nil)
    local sub = ISContextMenu:getNew(context)
    context:addSubMenu(menu, sub)
    local inside = KnoxMapResetLoot.inBuilding(square)
    if inside then
        sub:addOption("In this building", nil, function()
            confirm(player, "in this building", inside)
        end)
    end
    sub:addOption(string.format("Within %d tiles", KnoxMapResetLoot.RADIUS), nil, function()
        confirm(player, string.format("within %d tiles", KnoxMapResetLoot.RADIUS),
                KnoxMapResetLoot.around(square, KnoxMapResetLoot.RADIUS))
    end)
end

Events.OnFillWorldObjectContextMenu.Add(onFillWorldObjectContextMenu)
