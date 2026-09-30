# Changelog

## 1.5

- **KnoxMap is one file.** Windows gets `KnoxMap-vΓÇª-windows.exe`, Linux an
  AppImage, macOS a dmg. Python and the libraries are already inside. Put the file
  anywhere and open it. Maps, logs and the first-run downloads sit in the
  folder beside it (on a Mac that lives in Applications, they sit in a
  `KnoxMap` folder in your home directory). The map tools and the game's
  tiles are still fetched the first time, from the tools' own release and
  from your own copy of Project Zomboid ΓÇö they are not inside the file. That
  first-run setup happens inside the program. There is no setup script to run.

  The window is the same Electron window on every system. The page is
  unchanged ΓÇö the map, the search, the pipeline ΓÇö and Python still serves it
  and still does the work. A source checkout opens that window from `desktop/`
  after `npm install` when `desktop/main.js` is present, or the browser when
  it is not, or when `KNOXMAP_BROWSER=1`.
  On Windows, start that checkout from the `KnoxMap` folder with
  `.venv\Scripts\pythonw.exe knoxmap.py`. On Linux and macOS, `./knoxmap.sh`.

  The small Windows launcher and the separately downloaded window are gone.
  Players are not handed a folder of scripts. Downloads ask where to save.
  Closing KnoxMap while a map is being made asks first, and a compile in
  progress is stopped so the map compiler is not left running.

  On Linux the window no longer needs GTK or Qt installed for Python. It does
  need the ordinary desktop libraries, which a desktop already has. If the
  sandbox cannot start ΓÇö the helper is not setuid, or AppArmor will not allow
  a user namespace ΓÇö KnoxMap opens it once more without the sandbox. The page
  is only this computer's.

- **The map comes from a regional extract.** Generate no longer asks Overpass
  for the streets and buildings. It downloads the smallest Geofabrik daily
  that covers the box, keeps it under `cache/geofabrik`, and cuts the piece
  this mod draws inside the program. A selection bigger
  than one mod is cut into pieces of 20 cells by 20 cells that meet on a cell
  edge, each its own mod, so the streets continue when all of them are
  enabled. Named places inside the area still come from Overpass. **Add
  missing buildings** is the Overture switch, on the area tab. While an
  extract downloads, the progress box shows its size, how much has arrived,
  the speed, and the time left. Picking the roads and buildings out of a new
  extract has its own step in that box, and only tagged objects pass through
  Python, so a state extract no longer takes minutes to prepare.

- **The sidebar is one surface.** Setup finds the game, or takes the folder
  that contains its jar. Map holds the area as layers, with its real size, an
  overlay of the vanilla Knox County map, and switches for missing buildings,
  a random seed, true map generation and the guaranteed rifle. The rest of
  the knobs are under Advanced, headed More settings. Build is Generate
  (with the project download), Build, a placeholder Edit tab, and Export
  (Compile and Install). The eraser cuts a shape out of a drawn area. Search
  runs as you type, in a narrower field, with Within view. The first open
  offers a short tour. The top bar is Credits and the Discord mark. Open in
  WorldEd offers to install the community editor before it opens the map.

- **The tools run without the window.** `knoxmap_cli.py` builds, compiles,
  installs, draws and checks a map from a terminal. A checkout creates
  `KnoxMap/.venv` itself and starts `knoxmap.py`; the old `Setup.bat` and
  `setup.sh` are gone, and the first open still runs that setup.

- **Building generation is faster.** Room grids, zone detection and spatial
  queries now use NumPy and SciPy. The layout workers stay alive across the
  mods in one build, so the frozen app starts them once.

- **Junctions come from OpenStreetMap.** After the streets are drawn, each
  place roads meet is typed from the nodes on it: traffic signals, stop,
  give way, a crossing, a roundabout, a mini-roundabout, a turning circle.
  Nothing is guessed. A tagged junction gets its stop line, crosswalk, dropped
  kerb, stop sign or signal pole, and parked cars stay out of the box.
  Untagged corners are only squared off so the kerb can turn. Roundabouts are
  rings with an island, not discs of tarmac. The download now asks for those
  nodes, so the first generate after this version fetches the area again.

- **Upstream through 1.4.8 is in this copy.** Interior plans, porch lights, Overpass tiles that arrive empty, paper-map outlines, dedicated-server notes, the Linux Qt fixes, malls and castles, and Discord presence. The notes for those releases follow this one.
## 1.4.8

- **Every unit in a mall its own colour** (`knoxbuild/catalog.py`,
  `knoxbuild/tbx.py`). Knox County's mall paints 117 different interior wall
  tiles into the one building, where ours managed 27 - each unit is let to
  somebody else and decorated to suit. The wall styles the game itself uses
  are read off its compiled cells rather than guessed at from tile numbers,
  which is how the school ended up full of clocks: a style is its four wall
  tiles at a base on a 16 with the window and door tiles after them, and only
  the eighteen whose whole set the game uses are taken. Twenty-three styles
  to draw from now, and a mall takes one per trade. Ours: 112 tiles. Ordinary
  buildings are untouched at 4.0 wall sets each against the game's 4.48.

- **Malls** (`knoxbuild/layout.py`, `knoxbuild/build.py`,
  `knoxbuild/interiors.py`, `knoxbuild/catalog.py`). A shopping centre was a
  "shop" - the corner-shop recipe, which makes one sales floor the width of
  the front - so a 92x72 mall came out as a single room of 5,670 tiles with
  an office block on top of it. Loot is capped per room, so that one room got
  one room's worth over the whole floor. Malls are their own kind now, built
  from what Knox County's own mall (Muldraugh cell 54_22, 325 rooms over five
  storeys) is made of: a concourse with the units off it, and fourteen new
  room kinds - clothes, shoe, electronics, houseware, sewing, corner shop,
  optometrist, food court and the storerooms behind them. `clothesstore` is
  the game's spelling, 158 rooms across the county against 9 for the
  `clothingstore` we were writing, so it carries the better loot table. A big
  mall lets to 23.8 different trades where the game's lets to about 30.

- **The concourse is a concourse** (`knoxbuild/layout.py`). Knox County's is
  not a corridor down the middle: it is a wide spine across the building with
  arms running off it to the far walls, and it covers 27% of its own bounding
  box. Cut as one band it covered 100% of one and read as a warehouse aisle.
  Spine and arms are each their own rectangle, and at the size of the game's
  own mall ours comes out at 27% of the floor against its 25%.

- **Two floors, one space** (`knoxbuild/layout.py`, `knoxbuild/tbx.py`). The
  game leaves 89% of its mall's ground concourse open to the floor above -
  5,185 of 5,855 squares have nothing on them at level 1 - so the mall is one
  room several storeys tall. Ours now cuts the same hole, with a gallery
  either side and the arms carrying on across it as bridges: without those
  the hole cut the storey in two and half the units had no way to the stairs.
  The roof pass counts a hole as built over, or the storey below was roofed;
  a concourse is written with no ceiling, or BuildingEd laid a lid straight
  across the opening. The stairs keep their floor and a walkway out to the
  nearer gallery, rather than standing in mid-air.

- **Nothing of the shops left standing in the mall**
  (`knoxbuild/layout.py`). A unit is fitted from its own walls outward, and
  one whose front is the concourse edge laid its wall pieces on the far side
  of that wall: a furniture shop left fourteen dressers out in the middle of
  the concourse. The concourse itself is furnished as one - planters, bins,
  vending - and it is nearly bare on purpose. Knox County's holds 16 pieces
  of furniture in 5,855 tiles; what fills it is the shopfronts along its
  edges, not anything standing on it.

- **Castles, forts and grounds the size the game makes them**
  (`knoxbuild/layout.py`). Everything past a building's room mix falls to the
  fill, and whichever fill kind has no cap takes the lot: a castle was 5%
  bedrooms at the size these were tuned on and 63% at the size the landmark
  growth actually gives them - a keep of nothing but beds. Fixing that
  exposed the same thing in a stadium, at 39% changing rooms and then 50%
  cafes. The fill now carries on round its list instead of dropping the whole
  remainder on the first entry, one room may be cut into at most four, and
  bedroom and cafe have shares of their own. Police, school, military, shop,
  house and flats are unmoved.

- **A building is not one colour inside** (`knoxbuild/tbx.py`,
  `knoxbuild/catalog.py`). InteriorWall has always been written per room and
  every room was handed the same one, so a building was a single colour
  throughout however many its style could have used - and the civic styles
  lead with a blue, which is why every public building was blue. Knox County
  paints 4.48 different wall sets into one building over 9 in all; a few are
  dealt out per room kind now, so two bedrooms match and the kitchen does
  not. Measured back at 4.50.

## 1.4.7.3

- **The logo on the Discord presence** (`knoxpresence.py`). `large_image` was
  the plain name "knoxmap", which Discord matches against the name the art
  asset was uploaded under - one uploaded as anything else shows no logo and
  says nothing about why. KnoxMap reads the application's art assets instead,
  which are public and need no token, and sends the asset's id: that works
  whatever it was named and survives a rename. If the lookup cannot be
  reached - some networks block discord.com - it falls back to the name as
  before, and `"discord_asset"` in `knoxmap_config.json` (or
  KNOXMAP_DISCORD_ASSET) sets the id outright. The lookup runs on the
  presence thread, once an hour at most, and never in front of the window.


- **The copies stranded by the old updater update themselves after all**
  (`.github/workflows/release.yml`). 1.4.7.1 fixed the unpacking, but the
  updater doing the unpacking is the one already on the PC, so a copy on
  1.4.7 or earlier still had the broken one and could not be reached by it.
  An updater that old walks a release in the order its files are stored and
  stops at the first one Windows will not let it overwrite - KnoxMap.exe,
  held open by the virus scanner that takes an interest in it (#9) - and
  everything after that point was left at the old version. KnoxMap.exe is
  stored last now, so the whole release is already in place when that
  happens, the fixed updater with it, and the only thing left behind is a
  launcher that still works. Measured on a real 1.4.7 install running its own
  updater with the file held open: updater.py, app.py and knoxpresence.py
  were all left behind before, and all land now. The release stops rather
  than publishing if the file is not last.

- **Rich Presence is on, and on while nothing is building**
  (`knoxpresence.py`, `app.py`, `knoxmap.py`, `templates/index.html`). The
  presence was only ever set from the build progress, so it showed nothing at
  all until a map started and went blank again the moment one finished -
  which is most of the time the window is open, and it read as broken.
  KnoxMap says "Planning a map" from the moment the window opens and the
  three build lines replace it while a map runs. The map's name is not on
  there: it is named after the place somebody is building, which is often
  where they live. It is on by default now and
  the switch in the header is gone; `"discord_presence": false` in
  `knoxmap_config.json` or `KNOXMAP_NO_DISCORD=1` still turns it off.

- **The Discord handshake is read** (`knoxpresence.py`). Nothing read
  Discord's side of the conversation, so a connection Discord had already
  refused - an application id it does not know - looked live and every update
  went into the dark. The handshake now has to come back READY before the
  socket counts as open, and a rejected activity is written to the log
  instead of being a profile that silently never changes.

- **Towers are towers** (`generator/structures.py`, `generator/osm.py`). A
  water tower, a lighthouse, a windmill, a clock tower: `classify_building`
  has no kind for any of them, so every one was ordinary housing - thinned
  like a house, grown like a house and furnished with a sofa and two
  bedrooms. They are built as what they are now, the way an obelisk already
  was, and taken off the building list: a shaft rising from the footprint,
  and for a water tower the tank spread back out over the legs. `man_made`
  towers were never downloaded at all unless the mapper also tagged
  `building=*`, and now they are - except masts and floodlights, because a
  hundred concrete blocks up the hillside is not a landmark.

- **Castles and grounds** (`knoxbuild/build.py`, `knoxbuild/layout.py`,
  `knoxbuild/procedural.py`). The same gap one storey down: a castle, a fort,
  a city gate, a stadium or a sports centre had no kind either, so a keep came
  out as a bungalow. A castle is a great hall, a chapel, the kitchen that fed
  everybody, a library and its chambers; a ground is the concourse, changing
  rooms, a kit store, a first aid room and a counter. `historic=*` is read at
  last, which is what carried castle and city gate. Stone walls, not
  clapboard.

- **A room-size setting that moves something** (`knoxbuild/layout.py`).
  `_room_cap` falls back to MAX_ROOM_AREA for any kind not in KIND_MAX_ROOM,
  and the split target is clamped to it - so the new kinds sat at 120 tiles
  whatever KIND_ROOM_SCALE said, and a sweep from 10 to 24 moved the room
  count by nothing at all. With a cap of their own a castle went from 21 rooms
  a floor to 9 and a stadium from 21 to 11, median room 45 tiles to 105.

## 1.4.7.1

- **An update no longer gives up on one file it cannot write**
  (`updater.py`). Windows will not let a file be overwritten while another
  program has it open, and something usually does for a moment: a virus
  scanner reading the file it was just handed - KnoxMap.exe above all, which
  Defender takes an interest in (#9) - OneDrive, the launcher that has not
  quite finished exiting. One such file ended the whole update, and because
  CHANGELOG.md is what KnoxMap reads its version out of and went in before it,
  the install was left reading as the new version with most of the release
  still the old one, with the download deleted and nothing to try again.
  Reproduced by holding KnoxMap.exe open over a 1.4.6 install: it came out
  saying 1.4.7 with `app.py`, `knoxmap.py` and everything else past that file
  untouched. Each file is now waited for and tried again, and when it still
  will not go the old one is renamed out of the way instead, which Windows
  allows where it does not allow a replacement. The version file goes in last,
  so a run that stops halfway still reads as the old version, keeps its
  download and applies it on the next start - and after three starts it says
  so in the window rather than fetching the same zip every few hours.

- **Restart to update appears when it is ready** (`static/js/fx.js`). The
  window asked the update state once on load and then every ten minutes, and
  the first check only runs a few seconds after the window opens, so the
  banner could be ten minutes late on a window somebody had open for five. It
  asks every five seconds for the first minute now, then eases off.

- **Discord Rich Presence** (`knoxpresence.py`, new). What KnoxMap is doing on
  your Discord profile: Scooping data from osm, Mapping, Compiling, and
  nothing once the run is over. Off until you turn it on with the switch in
  the header - it is on show to everyone on your friends list - and it needs
  no library: Discord's own client listens on a named pipe. It runs on its own
  thread and fails quietly, so Discord being closed, restarting or refusing
  the socket costs a map halfway through nothing at all.

## 1.4.7

- **Rooms reachable, lit, and one flat to a front door**
  (`knoxbuild/layout.py`). Making a bathroom a dead end pushed whatever it
  had blocked into the door tree's last resort, and that pass is allowed to
  join two flats together: 246 doors between neighbouring dwellings and 101
  flats with two or three front doors of their own. The bathroom rule gives
  way a pass earlier now and the one-front-door rule holds to the last, which
  leaves bathrooms dead ends in 96% of cases and neither of the other two at
  all. Light switches are back in every room - the game lights a room from
  the switch inside it, so a room without one is dark whatever the sprite
  count says, and 1,945 rooms had none.

- **No warehouse racking in a house** (`knoxbuild/layout.py`). A house's box
  room, laundry and closet were furnished out of the storage list, which is
  steel shelving and packing crates: a stockroom, not a cupboard under the
  stairs. Homes and flats keep none of it now and get shelving and a chest
  instead; sheds, barns and warehouses still have theirs. There was already a
  check for this - it looked at living rooms and bedrooms and not at the three
  rooms the racking was actually in, and it now looks at every room a house
  has.

- **Kitchens fitted the way the game fits them** (`knoxbuild/layout.py`).
  Counters filled both long walls end to end and cupboards were hung above
  that, for 7.1 pieces of the counter tileset against Knox County's 3.9 over
  672 kitchens; the figure this was first tuned against, 12 per 10 m2, was
  every piece of furniture in the room rather than the counters. Counters and
  cupboards now share one budget scaled to the floor. A microwave went over
  every cupboard, for 1.8 cooking appliances against the game's 1.2, and is
  now the exception it is there. The washing machine came off the wishlist
  proper, which placed one in 80% of kitchens against the game's 16%.

- **A town is not made of one shelf** (`knoxbuild/catalog.py`,
  `knoxbuild/layout.py`). One sprite, `furniture_shelving_01_001-004`, was on
  nearly every room's list in every house and came to 3.9% of all the
  furniture in a town. Knox County spreads its 2,028 household shelving tiles
  over about a dozen styles, its most-used single shelf being 12% of them. Two
  more styles, their facings read off the vanilla map the way the porch lights
  were and confirmed at 84-95% over 27 to 77 sightings each. Down to 1.9%.

- **Rooms the size the building wants them** (`knoxbuild/layout.py`). A pass
  added to stop a police cell coming out at 24 m2, where Knox County's are 15,
  was applied to every kind of room and quietly became what set room size
  everywhere: it cut a school's halls to 43 m2 and its offices to 48 however
  large the floor had been divided, and no room-size setting could move it. It
  now only touches the rooms that want to be small. A school floor went from
  22 rooms to 12 and its median room from 42 m2 to 105.

- **The clocks** (`knoxbuild/layout.py`, `knoxbuild/catalog.py`).
  location_community_school_01_32-35 is the commonest thing in the game's
  classrooms and stands against a wall in 93-95% of 800 sightings, so it was
  added as a school desk. It is a wall clock, and a school came out with 765
  of them. Taken out again; a classroom is tables and chairs until the real
  desk in that tileset is identified.

- **Floors reach the rooms that never had them** (`knoxbuild/tbx.py`). A
  style's floor overrode every room in the building, so a block of flats had
  one carpet over its bathrooms and its kitchens alike and a school a shop
  tile throughout - and none of the per-room floors applied to them at all.
  The style's floor is the fallback now. Knox County gives a bathroom 18
  different floors and a kitchen 35.

- **A police station, not a house with cells** (`knoxbuild/layout.py`). Its
  corridor used the house wishlist and its reception the house lobby: 8.2 side
  tables, 6.9 chests and a sofa to a building. Workplaces have their own list
  now, and a lamp stands on a filing cabinet. The room mix is a lobby,
  offices, interrogation, lockers, an armoury with gun lockers, an evidence
  room and a few cells - offices fell from 36% of the building to 13%.

- **Flats that are flats** (`knoxbuild/layout.py`). A flat was cut towards the
  building's room size rather than a flat's, so 117 m2 of floor became six
  rooms. 40% are open plan now - one room that is kitchen and living room
  both, which is how the game does 44% of its own - 37% are not rectangles,
  and rooms reached only by walking through another fell from 29% to 20%.
  Bathrooms are dead ends: 95% have exactly one door.

- **Wall clutter** (`knoxbuild/layout.py`, `knoxbuild/tbx.py`). Measured
  against Knox County, per 10 m2: interior trim 2.43 against 0.25, corkboards
  in schools 0.24 against none at all, light switches 0.27 against 0.12, rugs
  in corridors 1.49 against 0.08. All brought to the game's own rates, and
  workplaces hang no pictures.

- **Room names the game knows** (`knoxbuild/layout.py`). Houses called their
  dining room "dining", which Knox County has 11 of against 694 "diningroom" -
  the loot tables key off the name, so ours spawned nothing. Closets and
  laundries were never built at all: the rule that makes a small room a closet
  needed one of 8 tiles or under, and the splitter cannot make a room smaller
  than 9, so it had never once fired.
## 1.4.6

- **A map is not empty because a server said nothing** (`generator/osm.py`).
  Two ways the download reported success and brought back no town. Overpass
  answers a query it could not finish with HTTP 200, the part it managed in
  `elements` and the reason in `remark`; only the status was read, so a
  half-downloaded tile went in as a finished one. And `overpass.osm.ch`
  answers every query with 200 and an empty list ΓÇö a one-block query that
  `overpass-api.de` returns 674 elements for. Tiles are dealt to the instances
  in turn, so a third of a city's tiles were handed to it and came back as
  open ground, which is why a town rendered as a meadow with its river still
  in it: the river was in the tiles that did arrive. A `remark` is now a
  failed tile, and an empty answer has to be confirmed by a second instance
  before the tile counts as empty. `overpass.openstreetmap.fr` is dropped; it
  has been 403 "only available to white-listed usages" on every request.

- **An outline with no area is refused** (`app.py`). A lasso drawn as one
  stroke, or a traced outline whose points land on each other, passed as a
  valid polygon that encloses nothing. The map is clipped to the drawn shape,
  so everything outside it ΓÇö all of it ΓÇö went back to grass, and the
  generation ran to the end and handed over a meadow. It is turned down now
  with a reason, before the download.

## 1.4.5

- **Public squares are paved** (`generator/osm.py`, `generator/renderer.py`). A
  pedestrian zone was read as a service alley and painted 3.5 m wide, so Madrid's
  Puerta del Sol ΓÇö a mesh of pedestrian ways with no polygon anywhere ΓÇö came out
  as stripes on a lawn. And an arcade, tagged as a passage through a building,
  was read as a tunnel and dropped, which took all 11,437 m┬▓ of Plaza Mayor with
  it. A pedestrian zone is its own class now, paved 9 m wide; a pedestrian way
  that closes on itself is a square whether or not it says `area=yes`; and a
  building passage is at ground level, not under it. Plaza Mayor, Puerta del Sol
  and Plaza de Santa Ana all paved, checked by downloading the real thing.

- **A school is a school inside** (`knoxbuild/layout.py`). The fill list was
  cycled round-robin once the mix was spent, so every kind in it got an equal
  share however silly: a school came out with 61 lavatories and 58 offices to
  its 63 classrooms, a police station with seven locker rooms, a church with as
  many storerooms as nave. Each kind has a share of a floor now. Schools gained
  a canteen, a laboratory, gym stores and janitors; barracks lost the police
  office that was in them.

- **Cells the game knows what to put in** (`knoxbuild/layout.py`). Every room
  name was checked against the 586 in Knox County and `cells` was the only one
  the game does not have ΓÇö it calls them `prisoncells`, and there are 540. The
  name is what the loot tables key off, so ours were furnished and then spawned
  nothing. The station leaned on `policestorage` as well, which has two rooms in
  the whole county.

- **Rooms the size the game builds them** (`knoxbuild/layout.py`). Every room in
  Knox County, measured by what it is called: a church is 39 tiles at the median
  but 340 at the ninth decile, a library room 100, a gym 88, a warehouse 134.
  Held to one size for every building, ours came out as a grid of cubicles where
  the game has a hall. The cap is by kind now.

- **Buildings you walk round, not through** (`knoxbuild/layout.py`). Only houses
  ever got circulation, so a school was classrooms opening into one another. A
  real corridor is cut through a school, a police station or a clinic ΓÇö the
  game's halls average 122 tiles in a school, not the 24 of a relabelled room ΓÇö
  and only where there are rooms enough to be worth serving. How much is a
  balance measured both ways: at one extra hall per twelve rooms, 230 doors in
  300 buildings went bedroom into bedroom; at one per six, a house was a third
  corridor. One per eight puts a house at 23.3% circulation against the game's
  23.7%, and bedroom-to-bedroom doors at 120 where the old plan had 771.

- **No two buildings of a kind alike** (`knoxbuild/build.py`). Church, barn and
  industrial shipped with one wall style each; police, library, fire and
  barracks had none at all and fell through to the house styles, so a police
  station could come out in clapboard. Every variant of a kind shared one
  interior wall besides. Public buildings borrow the civic style, the others
  take an exterior and an interior from the house styles ΓÇö whole, so window and
  door tiles come with them ΓÇö and a house takes its block's style 55% of the
  time and its own the rest, which builds a street rather than an estate.

- **Nothing hanging in the air** (`knoxbuild/layout.py`, `knoxbuild/catalog.py`).
  A piece is drawn with its base part-way up its tile when it is meant to sit on
  something: a lamp's base is 153 pixels down a 256-pixel tile, a pot plant's
  151, but a coffee table's top edge is at 172, so 94% of them hovered. The box
  was the stacking one drawn a quarter of a tile up, where Knox County puts 336
  of its 338 boxes on the one that sits on the ground. And the sink list was the
  two that existed when it was written, so the three added in 1.4.3.2 all hung.

- **A .tbx that cannot be written no longer loses a building**
  (`knoxbuild/catalog.py`). BuildingEd wants a colour for every room name and
  throws without one. The failure was swallowed as "left out 2 buildings that
  could not be laid out", which is how a school and a police station vanished
  from a town without a word. Every room kind is checked for furniture and a
  colour now.

## 1.4.4

- **Lamps and pot plants stand on something that reaches them**
  (`knoxbuild/layout.py`). A small piece is drawn with its base part-way up its
  tile and whatever it stands on has to reach that high: a lamp's base is 153
  pixels down a 256-pixel tile and a pot plant's 151, but a coffee table's top
  edge is at 172 ΓÇö so both hung a quarter of a tile above the table put under
  them. 94% of them were in the air. A bedside chest tops out at 97 and a
  counter at 125; Knox County stands 180 of its 270 table lamps on
  `furniture_storage_01` and not one on a low table. A chest goes under them
  instead, and a low table already on the tile is swapped for one rather than
  stacked with it.

- **A house you walk round, not through** (`knoxbuild/layout.py`). Every room
  opened onto every room it touched: the commonest door in a generated town was
  one bedroom into the next, and 56% of doors joined two rooms you should not
  have to cross. An upstairs had no circulation at all, because only a lift or
  stair core was ever made a hall. The room the stairs arrive in is the landing
  now, and a floor gets one more hall per five rooms until the rest all open
  onto circulation. A door between two private rooms is priced dearly enough
  that the plan takes any other way round, bar the pairs a real house has ΓÇö a
  bathroom off a bedroom, and the kitchen, dining and living rooms. 56% down to
  2%, with no room sealed off and corner doors unchanged at 0.05%.

- **Bathrooms the size of bathrooms** (`knoxbuild/layout.py`). A floor is cut
  into rooms of one target size and the bathroom then takes the smallest of
  them, so it came out at 13.7 m┬▓ ΓÇö a bathroom the size of a bedroom, and the
  reason bathrooms measured 7.4 pieces per 10 m┬▓ against Knox County's 12.6.
  One region on a house floor now gives up a corner to a small room first, in
  two cuts because a corner taken out of a rectangle leaves an L. Bathrooms
  13.1 m┬▓ down to 10.0 with the same fittings in them; what is left of the
  corner is a closet or a box room, which is what sits beside a bathroom in
  the game's houses too. The smallest room a wall can enclose is 3├ù3, so 9 m┬▓
  is as near the game's 6.5 as whole tiles allow.

- **A table and chairs in the kitchen** (`knoxbuild/layout.py`). Every piece
  of a centre group needs its own clear block with a tile of aisle all round
  it, so in a kitchen four tiles across nothing but the bare table ever fitted:
  one kitchen in eight had a table and there were 0.17 chairs in one, against
  the game's 0.52 and 1.10. A table on its own is the last arrangement tried,
  and chairs go on after the group is down, tucked against the table rather
  than given an aisle each. Tables in 36% of kitchens, 0.70 chairs each, and a
  mat under the table where there is room for one.

- **A light by the front door** (`knoxbuild/yards.py`). Knox County hangs one
  outside 92% of its houses; generated ones had 1%, which is most of why a
  street of them reads as unfinished from outside. One goes on the wall beside
  every front door, on the slab the stoop already lays. Which sprite belongs on
  which wall was read off the vanilla map rather than guessed ΓÇö for every
  outdoor light standing outside a house with house tiles on exactly one side,
  that side ΓÇö and the five sets used came back 96ΓÇô100% one-sided over 60 to 140
  sightings each. They are written as loose tiles, because they stand outside
  the building and a house's own `.tbx` stops at its footprint. The square a
  light stands on usually carries the house wall too, and WorldEd lays a
  cell's lots down in the order the project lists them ΓÇö sorted by position,
  the light went down before the wall and the wall covered it. A lot can now
  ask to go last, which is where all 659 of Knox County's porch lights that
  share a square with a wall are written.

## 1.4.3.3

- **Maps stop drifting east** (`knoxbuild/world.py`). Generating makes a new
  output folder every time, and every folder claimed its cells in the world for
  good, whether it was ever installed or not. So each map started further out
  than the last ΓÇö 70, 98, 104, 110, 132, 159 on the PC this was found on ΓÇö and
  the paper map is one grid counted from cell 0, so the town drew smaller and
  further into the corner of an empty world with every generation. Only
  installed maps hold a place now, and uninstalling one hands its cells back. A
  new map lands on 70,0 again, where the first one did.

- **A smoke test** (`tools/smoke_test.py`). The selftest is 200 checks and
  several minutes; this is the end-to-end path on its own ΓÇö terrain, buildings,
  paper map, install ΓÇö with the numbers printed rather than asserted, for
  checking a release in a minute.

## 1.4.3.2

- **More than one kind of sink** (`knoxbuild/catalog.py`). Every kitchen in a
  town had the same steel double sink and every bathroom the same white
  basin, which is what reads as institutional; the game's own map spreads
  across nine sets. Three more are added, and the facing of every tile was
  read off the vanilla map rather than guessed ΓÇö for each sink standing
  against exactly one wall of a room, which wall that was. The two sets
  already here came back exactly as written, which is what makes the rest of
  it trustworthy. The pedestal basins are left out: they only ever answer
  north and west, so they have two sprites, not four.

- **Not every room has a picture in it** (`knoxbuild/layout.py`). Pictures and
  mirrors are on nearly every room's list and, unlike everything else, they
  repeat as the list grows with the floor, so almost every room had one or
  two and wall art came to 11.8% of everything in a house. One per room at
  most, and only about half of rooms get one: 1.25 per room down to 0.60,
  6.6% of the furniture. Corridors hang one every third wall slot rather
  than on every one.

- **Furniture no longer faces the wrong way** (`knoxbuild/layout.py`). A piece
  with only north and west sprites, stood against a south or east wall, is
  drawn with its north sprite on the far edge of the tile: a corkboard hangs
  a tile into the room, a rack faces its own back. Which pieces that applies
  to was a list kept by hand, so anything added later was quietly wrong ΓÇö
  1,076 of them over 200 houses, corkboards and bedside tables worst. It is
  worked out from the catalogue now, so a new piece cannot get it wrong.

- **A shelf is whatever that room would really have** (`knoxbuild/layout.py`).
  One generic wooden shelf was on nearly every room's list and was always the
  same sprite, which made it the third commonest object in a town at 5.9% of
  all furniture. The room picks now: bookshelves in living rooms, bedrooms,
  studies and libraries, a shelf or a chest of drawers in bathrooms, wire
  racking and crates only in storerooms, garages and works. The wooden shelf
  is 3.5%, nobody has a warehouse rack in their bathroom, and a wall cabinet
  hangs over a kitchen counter rather than standing on a bare wall.

## 1.4.3

- **Every map now ships what a dedicated server needs**
  (`tools/make_map_mod.py`). A map mod is enough for one player; a server
  needs three things the mod folder cannot tell it, so they are written out
  with this map's real names: `SERVER SETUP.txt` with the exact `Mods=` and
  `Map=` lines, and `server/<map>_spawnregions.lua` ready to copy into
  `Zomboid/Server/`. Without the spawn region players start in Muldraugh
  rather than on the map.

- **Both notes say to add the map before the world exists.** A world is
  written cell by cell as players walk into it, from whatever map was loaded
  at the time, so a map added or changed afterwards leaves old cells beside
  new ones that do not match ΓÇö which surfaces later as unexplained failures
  rather than an error. Installing now also reports the saves already on the
  PC, so the choice is in front of you at the moment it matters.

- **The server notes cover what actually goes wrong.** A KnoxMap map is not
  on the Workshop, so nothing fetches it for players ΓÇö every one of them
  needs the same folder, and a player without it falls through the world
  where the map should be. A map built with Erika's Tiles says the server and
  the players need that mod too, with the `Mods=` and `WorkshopItems=` lines
  to match. Map folder names are case sensitive on the Linux servers most
  people rent. There is a short list of symptoms and their causes at the end.

## 1.4.2

- **The map compiler uses its own Qt plugins on Linux, not the machine's**
  (`knoxpaths.py`, `worlded-linux.yml`). Loading the right Qt libraries was
  only half of it: Qt looks for its plugins under the prefix it was compiled
  with, so on Ubuntu 24.04 the bundled Qt 5.15.3 read
  `/usr/lib/x86_64-linux-gnu/qt5/plugins`, whose `libqsvg.so` pulled the
  system's Qt 5.15.13 in behind it and Qt aborted. `QT_PLUGIN_PATH` cannot
  settle that, because a plugin found through the built-in prefix is loaded
  before the environment is consulted. A `qt.conf` beside the compiler
  replaces the prefix itself, which is the first thing Qt reads. Written on
  setup, so an install that already exists is fixed without a reinstall.
  Reported with the diagnosis by a player on Ubuntu 24.04.5.

- **glib and harfbuzz are left to the machine.** Every desktop has them, and
  the bundled copies were older than the system GTK they ended up beside,
  which failed on `g_dir_unref` and `hb_ot_color_has_paint`.

- **Corridors and stair halls are dressed, not blank** (`knoxbuild/layout.py`).
  Nothing may stand in a corridor ΓÇö a flat's front door and the only way past
  the flight both run through it ΓÇö so ours carried one picture every third
  slot and nothing else, 1.6 pieces per 10 m┬▓ against Knox County's 7.1. The
  walls now take pictures, mirrors and a corkboard along their inside faces,
  and rugs go on the floor, which is the one thing you can walk over. 1.6 ΓåÆ
  6.8 per 10 m┬▓. Most of the facade is left clear so the windows keep their
  columns up the front of the building.

- **The map compiler now loads its own Qt on Linux, whatever
  `LD_LIBRARY_PATH` says** (`knoxpaths.py`, `worlded-linux.yml`). The bundled
  binaries recorded their library folder as DT_RUNPATH, which the loader
  searches *after* `LD_LIBRARY_PATH` ΓÇö and Steam, Proton and several desktops
  export one with a system Qt on it. That Qt was found first and Qt aborted
  before WorldEd ran a line: *Cannot mix incompatible Qt library (5.15.13)
  with this library (5.15.3)*. Putting the bundled folder at the front of
  `LD_LIBRARY_PATH` was not enough, because nothing done at run time outranks
  what the loader read before starting.

  The tag is now DT_RPATH, which is searched first. Future builds are made
  that way (`patchelf --force-rpath`); an install that already exists is
  converted in place the first time it is checked, so it heals itself without
  a reinstall. A folder on `LD_LIBRARY_PATH` carrying a Qt of its own is also
  dropped for the compiler, and an inherited `QT_PLUGIN_PATH` is ignored when
  there are no bundled plugins to point at.

- **Doors no longer sit on a wall corner, where the game draws neither door
  nor wall** (`knoxbuild/layout.py`). A tile carrying both a west and a north
  wall is one corner piece, and a door on one comes out as blank wall ΓÇö a
  doorway that will not open. On a built town map 989 doors of 57,357 were on
  one, spread over 741 buildings. A stuck door now moves to a clean wall the
  room shares with some other neighbour, not just to the same boundary, and
  every move is checked so nothing is shut off and a flat keeps exactly one
  front door. 1.69% of doors ΓåÆ 0.057%, and `audit_layouts.py` fails if the
  rate climbs back.

- **A busy Overpass server no longer loses the map** (`generator/osm.py`). One
  tile failing threw the whole download away uncached, so a town that fetched
  forty tiles and missed one started again from nothing. The tiles that
  arrived are kept and only the ones that did not are asked for again. A tile
  nothing answered in time is also quartered and retried, as a tile the server
  refuses outright already was ΓÇö bounded, so a dropped connection costs eight
  requests rather than sixty-four. Added `overpass.osm.ch` as a fourth
  endpoint, and the failure now says how many tiles were missing and what to
  do about it.

- **Furniture is cleared out of the way of doorways** (`knoxbuild/layout.py`).
  Rooms were furnished one at a time, so a shelf could land in the only
  doorway and a counter could span the only way through. The plan was always
  connected; the furnished building was not. Over 400 buildings, 272 had at
  least one room nobody could walk into and 3,030 rooms of 34,331 were sealed
  off. Now none, for 0.42% of the furniture.

- **A big two-storey building is flats, not one enormous house**
  (`knoxbuild/build.py`). A floor count of two used to settle it whatever the
  footprint, so a terraced row came out as a single dwelling: on a 23x19
  building, 43 rooms with 24 bedrooms, two bathrooms and one kitchen. Two
  storeys says nothing on its own ΓÇö a terrace is two and so is a bungalow
  with an attic ΓÇö so the footprint decides, as it already did for a building
  with no floor count at all. The same building now lays out as eight flats
  with a kitchen, a bathroom and a living room each. One storey is still a
  house at any size, three or more is still flats, and `building=house` is
  still believed.

- **Paper map outlines are checked again after they are rounded to whole
  tiles** (`knoxbuild/worldmap_bin.py`). Collapsed and self-crossing polygons
  are repaired where they can be and dropped where they cannot, instead of
  being written out. They were crashing the world map when it was zoomed out
  over a dense area. Measured on two built maps: 21 bad outlines and 15 bad
  outlines, now none.

- **`KnoxMapGunCache.lua` checks an argument's type before calling a method on
  it.** Stops the repeated "Tried to call nil" in the game's log.

## 1.4.1

- **KnoxMap.exe is built with MSVC** (`win/build_launcher.ps1`, on a Windows
  runner) instead of mingw-w64, and is no longer stripped. Drops the two
  things antivirus scores a 30 KB two-`CreateProcessW` binary on: the mingw
  toolchain and a missing symbol table. `win/build_launcher.sh` still builds
  the identical binary with mingw on Linux and CI still checks it, so what
  ships is reproducible without Visual Studio. Not a substitute for signing.
  v1.4's zip shipped the mingw build; this is the first release with the
  MSVC one.

- **PZWorldEd runs on a Windows desktop of its own** (`CreateDesktopW`,
  `STARTUPINFOW.lpDesktop`, in `tools/compile_map.py`). Qt's windows and the
  Generate Lots dialogs cannot be composited onto the screen at all, so
  compiling no longer flashes windows over whatever is in front. A watcher
  thread copies anything titled error, warning or exception - and anything
  still open after a minute - back to the real desktop as a message box, so
  a compiler that is genuinely stuck is still visible.

- **Setup.bat fetches Python with `curl`, `certutil` and `tar`** instead of a
  PowerShell one-liner. Same URL, same SHA-256 check, same private copy under
  `.python`. Kaspersky flagged the PowerShell form; these are stock Windows
  tools and are not flagged.

- **Added `<game>/projectzomboid/media` to the game folder search**
  (`knoxpaths.py`). Linux installs that nest the media folder a level deeper
  were refused as "no game found". Checked in `tools/selftest.py`.

- **Removed `btn-small` from the pipeline Stop buttons**, which sat shorter
  than the Build and Compile buttons beside them.

- **Releases publish from a tag with or without the `v`, and from a release
  drafted by hand on GitHub** (`release: published`). `1.4.1` was tagged
  without the `v`, so `release.yml` never ran, the release carried no files,
  and a release with no files is invisible to the in-app updater - there is
  nothing for it to download. `release.yml` now also refuses a tag the
  changelog does not head, because the app reads its version from that
  heading and would otherwise offer the same update for ever.

## 1.4

- **KnoxMap.exe is built the way Windows software is built.** The launcher is
  a small binary whose whole job is to start Setup once and then the window -
  two `CreateProcessW` calls and nothing else. That is structurally what a
  dropper is, and it was built with mingw-w64, whose output is heavily
  over-represented in the corpora antivirus heuristics are trained on. The
  same source now compiles with MSVC on a Windows runner, which is what the
  rest of Windows software is compiled with, and it is no longer stripped: a
  tiny binary with every symbol taken out of it reads as something with
  something to hide, and 30 KB was not worth that.

  The same source also built with mingw-w64 on Linux, and CI checked that
  it did, so what shipped could be reproduced without Visual Studio.

  None of this is a substitute for a code signing certificate, which is the
  only real answer to "Windows protected your PC" - but it is free, and it
  stops the file looking like the thing it is not.

- **The size limits are advice now, not a wall.** Draw too big an area and
  KnoxMap used to grey the **Generate map** button out and answer the request
  with an error: over 400 km┬▓, over 9,000 tiles a side, or more memory than
  the PC had free. Somebody who wanted a whole city could not have one at
  all, whatever their machine.

  Every one of those is a warning now and the button stays lit. The panel says
  what the area will cost ΓÇö the tiles, the gigabytes of ground and greenery
  held at once, how many OpenStreetMap queries ΓÇö and adds that raising metres
  per tile is the cheapest fix, because 2 m a tile is a quarter of the memory
  of 1 m. Then it gets out of the way. Landmark lookup is the same: over
  40 km┬▓ it is slow, so it says so instead of refusing.

  A limit that says no is worth having only when the thing behind it cannot be
  done. These can be done; what they cost is the mapper's to spend.

  What is still refused is a scale that is not a scale ΓÇö zero, a negative, or
  something absurd ΓÇö because dividing the world by nothing is not a map
  anybody asked for.

  And if a map really is too big, it now fails like a grown-up. Running out of
  memory mid-render used to be a traceback; it says how much the map needed,
  at what size, that raising the scale is the fix, and that everything
  downloaded is kept so a second attempt starts from the data already on disk.

- **The buildings OpenStreetMap has not got.** A new setting under *Fine
  tuning*: **Fill gaps from Overture**. OpenStreetMap is drawn by people, so
  how much of a town is on it depends on who lives there and whether anyone
  has traced it. Generate a German town and every building is there; generate
  one in plenty of the rest of the world and you get the high street, a
  school, and empty land where the other two thirds of the town is.

  [Overture Maps](https://overturemaps.org) publishes a buildings theme that
  is OpenStreetMap first and machine-detected roofprints second, under the
  same ODbL licence. Turned on, every building Overture has that OSM does not
  is added before anything else runs, so the ground, the gardens, the street
  angle and the .tbx all treat it exactly as they treat a mapped one.

  Measured over the same size of box, on two real towns:

  | | OSM has | Overture adds | median size of the new ones |
  |---|---|---|---|
  | Gifhorn, Germany | 1,962 | **60** (3%) | 38 m┬▓ ΓÇö sheds and garages |
  | ├£rg├╝p, Turkey | 473 | **761** (62%) | 87 m┬▓ ΓÇö houses |

  So it is **off by default**. Where OSM is complete it adds sheds and costs a
  few minutes; where OSM is thin it nearly trebles the town. You can see which
  case you are in from the preview before you turn it on.

  Overture's `class` is OpenStreetMap's own building values ΓÇö house,
  apartments, barn, church ΓÇö so where it has one the building arrives already
  classified. The machine-found ones have none and arrive as plain
  footprints, which is what they are; the generator reads the land around
  them for the rest, as it does for any untagged building.

  It needs DuckDB, because the data is GeoParquet on S3 and there is no
  bounding-box API for it. That is not a small install, so it is optional and
  listed under the optional extras with what to type. Everything else works
  without it. One fetch takes two to three minutes, nearly all of it spent
  finding which of Overture's files cover your box, so the answer is kept
  beside the map like the Overpass one ΓÇö and a map that has been fetched once
  re-renders with no DuckDB at all, on any PC, including one you hand the
  folder to.

  A map built this way credits Overture in its `ATTRIBUTION.txt` alongside
  OpenStreetMap, which ODbL asks for; one built without it is unchanged.

  This does not replace the houses KnoxMap already invents from OSM's own
  address points ΓÇö those are the homes somebody numbered but never drew, 22
  of them in Gifhorn and 20 in ├£rg├╝p, and they are still filled in afterwards
  wherever nothing stands.

## 1.3.9.2

- **Floating windows, and walls with holes beside them.** Reported on a
  generated town: windows all along a wall, each one hanging in a gap you
  could see straight through.

  BuildingEd draws a tile that carries both a west and a north wall as *one*
  corner piece. Put a window on that tile and it becomes a window facing one
  way, and the other half of the corner is not drawn at all. KnoxMap has
  always known this and blocked it - but it asked `_facade_runs`, which only
  knows the outside of the building, so the corner where a *room's* wall
  arrives at the facade on the same tile was never blocked. That is almost
  all of them. Counted over the two city maps sitting in `output/`: **7,684
  windows on such corners across 5,311 buildings**, 37% of every building
  affected. Not one of them was the case the old guard covered.

  Room walls now count as walls. On the same buildings rebuilt, 241 bad
  windows became 0, for five per cent fewer windows overall.

  Doors land on those corners too and break them the same way, and a door
  cannot simply be dropped - the room behind it may have no other way in. A
  door on a corner now moves to the nearest tile of the same boundary: the
  same two rooms either side, so nothing is shut in. Sliding along its own
  wall was not enough, because a stepped diagonal side is a wall one or two
  tiles long with nowhere to slide to; it looks at the whole boundary
  instead. Where every tile of that boundary is a corner too, the door stays
  where it is, because a broken corner beats a room nobody can enter.

  This is why it showed up on a town and not on Knox County: the corners come
  from stepped diagonal walls, and a building only steps its walls when it is
  turned more than *Square up buildings* degrees from the grid - which is
  most of them on a real map of a real place.

- **Compile failed on Linux with a Qt no-one asked for.** Reported from Linux
  Mint: every compile died on the first batch of cells with

      WorldEd failed on cells 0,0..3,3 (exit -6): Cannot mix incompatible
      Qt library (5.15.13) with this library (5.15.3)

  The compiler built for Linux carries the exact Qt it was built against in
  `lib/` beside it, and the binary records that folder - but as DT_RUNPATH,
  which the loader searches *after* `LD_LIBRARY_PATH`. Steam and Proton both
  export that, so on a machine with its own Qt 5 installed the system's won,
  the program found a Qt it was not built against, and Qt killed it on the
  spot. Nothing to do with Python, the virtual environment or Wine: none of
  them are in a compile on 64-bit Linux.

  The bundled folder now goes on the *front* of `LD_LIBRARY_PATH`, ahead of
  whatever was already there, and the compile is held to the offscreen
  platform plugin rather than inheriting a desktop's `QT_QPA_PLATFORM` -
  only offscreen and minimal are shipped, so anything else could only fail.

  Setup also runs the compiler once now, straight after installing it, and
  says in words if it will not start. Finding that out in the seconds after
  setup beats finding it out after drawing a map and waiting through a
  build, and a compile that hits it anyway now explains itself instead of
  printing two version numbers.

- **One bad batch no longer throws away the whole compile.** Reported from a
  fourteen-hour run: batch 23 of 48 exited 1 after 849 seconds, and with it
  went the other 47. A batch is now tried three times - most of what goes
  wrong in WorldEd's lot export goes wrong once - and if it still will not go
  it is written down and stepped over so the rest of the town still compiles.

  What could not be done is named in the window, with **Compile the cells
  that failed** beside it to run just those rather than walking the whole map
  again. The step says the map has a hole in it until they are done, and says
  it again next time the map is opened, because by then nobody remembers
  which one it was. Nothing is deleted to retry: the compiler already redoes
  a batch that has cells missing.

  Failures that are about the machine rather than the batch still stop
  everything at once - a Qt that cannot start would fail all forty-eight the
  same way, and three attempts each is hours of the same abort.

- **Two compiles of one map can no longer run at once.** Reported as batches
  appearing out of order in the log. The batch loop has always been a plain
  sequential one, and it now proves it: every run carries an id on every log
  line so two runs in one log file can be told apart, the batch counter is
  checked against the one before it, and a project holds a lock for the
  length of a compile. Starting a second - from the window and the command
  line, say - is refused with what is already running rather than letting
  both write the same lots folder and the same `.pzw`. A lock left behind by
  a compile that crashed is cleared by the next one.

- **Hidden buttons were not hidden.** `.btn` sets `display`, which beats the
  browser's own rule for `[hidden]`, so every button meant to be out of the
  way was on screen anyway - Stop, most visibly, offered on a step that was
  not running. Also: opening a map left whatever the last compile had said
  sitting under the new one's Compile button.

- **True map generation, and what to do when you do not want it.** A new
  setting under *Fine tuning*. Left on - as it is by default - nothing
  changes: every address OpenStreetMap has is built where it is, at the size
  it really is.

  Turned off, the roads, rivers, woodland and terrain are still exactly as
  mapped, and only the housing changes. OSM draws a town at its real density,
  which at 2 m a tile is a street of five-by-four-tile houses standing
  shoulder to shoulder: accurate, and a row of one-room boxes to loot. So
  about half the ordinary houses are left out and the ones that stay grow
  into the gap. On a test town at 2 m a tile that turned 140 houses of one
  room each into 95 of four - the same street, in houses worth going into.

  Which houses go is a spacing rule rather than a coin toss: the larger house
  of a crowded pair survives, and a farmhouse with a field around it is never
  touched. It uses no random stream at all, so the same seed still gives the
  same town.

- **A police station the size of a police station.** With true map generation
  off, the buildings a town is known by - the station, the fire station, the
  school, the hospital, the supermarket - are never left out, are placed
  before the housing so the ground they need is still free, and are built at
  the size the game gives that kind of building rather than the size somebody
  traced. OSM decides that there is a police station here; what a police
  station is, is the game's business.

  A station mapped as its front office came out as 42 tiles - a hut with a
  desk in it - where the game's own is ten times that. It now grows to about
  220, the school to 1100, the hospital to 800, and stops short of whatever
  stands next to it rather than swallowing it.

  Buildings that only the land around them identifies are covered too: the
  huts on an army base and the wings of a hospital used to read as untagged
  houses and be thinned away with them, which on one test map took away the
  barracks the whole map's rifles were going to spawn in.

- **One rifle, guaranteed.** The M16 spawns from army and police loot and
  almost nowhere else, and a real town has no checkpoints in it and usually
  no gun shop, so a generated map could contain no container anywhere that
  could ever roll one. Every map now gets one, in the best place it actually
  has: an army building, else the police station, else a gun shop, else a
  house out on the edge of town, as the survivor who had it.

  It goes in the first container the game fills inside that building, once
  per map per save, and it ships with the map as `KnoxMapGunCache.lua`. It
  cannot be baked into the building: a `.tbx` holds walls, floors and
  furniture and no items at all, and what is in a container is rolled by the
  game the first time somebody walks in. Turn it off with *Guaranteed rifle*
  if you would rather the town stayed as peaceful as it really is.

## 1.3.9 rnd

- **The updater could not see a named release.** It matched a download by
  its filename, and the pattern had no room for a name in it, so
  `KnoxMap-v1.3.9-mc1-macos.tar.gz` looked like nothing at all - the macOS
  fix was on GitHub and no Mac was ever offered it. Fixed here, which means
  it works from this release on; 1.3.9 mc1 has to be downloaded by hand.

- **A picture of your map.** Once a map is compiled there is a *Draw a
  picture of it* button beside the download: the whole town, the middle of it
  close enough to see, and that same middle with the roofs off, which is how
  every room and everything in it becomes visible. They are drawn from the
  compiled cells the game itself loads, not from the terrain bitmap, so what
  you get is the map rather than an impression of it.

  `python tools/make_pictures.py output/<map>` does the same from a terminal,
  and takes `--box x,y,w,h`, `--no-roofs`, `--size` and `--scale` for a
  particular corner. The scale is chosen to suit the area, and a map too big
  to draw in one go is drawn from the middle out rather than asking for a
  canvas of several gigabytes.

## 1.3.9 mc1

*A macOS-only release. Windows and Linux are unchanged and stay on 1.3.9.*

- **A tutorial in the download.** It walks through setup start to
  finish: what to have installed, running Setup, finding the game when it
  asks, choosing an area that will not blow the limit, the four buttons and
  what each one does, and what to do when a step will not run.

- **One system at a time.** A fix that only matters on one system is now
  released for that system alone - this one is macOS only, and Windows and
  Linux stay on 1.3.9 with nothing to download. KnoxMap only offers you an
  update that has a file for the PC you are on.

- **macOS could not find Project Zomboid.** On a Mac the game ships as an
  application bundle and keeps everything inside it, but KnoxMap only ever
  looked for `<ProjectZomboid>/media`. So Setup found the install, looked
  straight into it, and said "That folder has no media/texturepacks inside -
  try again" - including when you pasted the path by hand, because the path
  you pasted was the one being rejected.

  The game's artwork is now looked up in one place that knows about bundles,
  so a Mac install is found on its own, with nothing to paste. If it still
  asks, any folder that names the install is taken: the Steam library, the
  ProjectZomboid folder, the `.app`, anything inside it, or the media folder
  itself. An install folder Steam named something else is found too, and a
  folder you put under **Steam libraries** in the window counts even when it
  is the game rather than a library.

- **The editor was given a path it could not open.** The game folder written
  into `PZTools.ini` was always spelled the machine's way, but off Windows
  the editor runs under Wine and cannot open `/Users/somebody/...`. Without
  the game's tile definitions every window gets a small house-window hole cut
  in the wall, so a shop's floor-to-ceiling glass showed wall behind it. Each
  path is now written the way the editor in use will read it, and on a Mac it
  points inside the bundle where the media actually is.

## 1.3.9

- **Only KnoxMap counts as an update.** The map compiler is published from
  the same repository, and its tags are dates, so a compiler release could
  become GitHub's "latest" and the updater would read
  `worlded-cli-linux-20260909f` as version 20260909 and offer it to you. It
  now checks that a tag is a version before believing it.

- **Linux does not need Wine any more.** Setup now fetches a map compiler
  built for Linux - the same program, from the same source at the same
  commit, with the Qt it needs beside it - checks its fingerprint and puts it
  in `vendor/PZMappingTools/bin/`. Nothing to install, no Wine, and the
  compiler's own log lands in `logs/worlded/` where you can read it.

  Upstream publishes Windows binaries only and calls its own Linux build flow
  "intended" rather than tested, and it turned out not to compile: 20 copies
  of `QPolygonF({a, b})`, which GCC will not accept because two points in
  braces are as good a match for a rectangle's corners as for a list of
  points. `worlded/patch_worlded_linux.py` names the container, and the build
  is made and exercised on every push, with its source and licences published
  beside it.

  macOS, 32-bit and ARM still use the Windows build through Wine, and so does
  Linux if that download ever fails.

- **A compile that never ended on Linux.** WorldEd would die mid-compile and
  the window would carry on saying "compiling" for ever - nothing running,
  no error, and Stop did nothing either. KnoxMap was waiting for WorldEd's
  output to finish rather than for WorldEd, and off Windows the tools are
  started by `wine`, which hands their pipes to wineserver. wineserver
  outlives everything it runs, so the pipes never closed and the wait never
  returned; Stop was stuck on the same pipes.

  A batch now writes to files and is waited for by the process, which cannot
  get stuck that way on any system, and nothing waits without a deadline.
  Because `wine` is only a launcher and killing it leaves the program
  running, a batch is also given a process group of its own and the whole
  group is ended together - so Stop stops it, and a crashed WorldEd is
  reported as the error it is, with the path to its log.

- **Windows with no wall behind them.** A window in Project Zomboid is a
  frame and a pane of glass with nothing behind it: the hole it sits in is a
  tile of the wall's own, and there is one per window style. The wall lists
  KnoxMap was building named only the first, so every window that used any
  other style had no wall at all - you could see straight through the
  building, and walk through it.

  It showed up on the north and west face of every building and nowhere else,
  which is what made it look random: those are the two sides BuildingEd walls
  with the room's *interior* wall, and the interior wall lists were the ones
  missing their cut-outs. Compiled cells of a test town had 62 of 192
  windowed squares with no wall on them; now none of them do.

  Every wall in the catalogue now carries a cut-out for every window style,
  taken from the editor's own list, and falls back to the wall's single
  opening where the editor has no list either. Existing maps are rebuilt the
  next time you press Build.

## 1.3.8

- **`KnoxMap.exe`.** Windows downloads now carry a launcher you double-click
  instead of a batch file: it works in its own folder, runs setup the first
  time in a console you can watch, starts the window and gets out of the way.
  It carries KnoxMap's icon and version, and it exits as soon as the window
  is up so that an update is free to replace it. Windows will warn about an
  unsigned program the first time: **More info** then **Run anyway**.

  It is built with mingw-w64 and then *run* on a Windows runner before any
  release goes out - a folder with no environment must run Setup, and one
  with an environment must start the window, exit within a few seconds and
  leave its own file replaceable. None of that can be checked on the machine
  it is written on.

## 1.3.7

- **A town with no zombies in it.** The project told WorldEd where the zombie
  spawn map was by its bare file name, in the same block whose export folder
  had to be made absolute for exactly this reason ΓÇö so WorldEd looked for it
  beside its own executable, found nothing, and baked every map with no
  zombies at all. Measured on the same map compiled both ways: sixteen
  chunkdata files and 23,054 bytes of zombie data with the fix, and none
  without it. Maps already made need Compile and Install again.
- **Walls you can see straight through.** A map built with Erika's Tiles
  said `require=\Erikas_Tiles` in its mod.info. No mod has that id ΓÇö the id
  is `Erikas_Tiles`, with nothing in front of it ΓÇö so the game neither
  insisted on the mod nor loaded it before the map, and every tile from it
  came out missing. One compiled cell of the test town names Erika's tiles
  276 times, which is how much of a building can simply not be there.
- **The version menu says what to do at the top**, with the Update or
  Restart button beside it, instead of under every release there has ever
  been. The version in the corner wears a dot when a newer one is out, and
  an amber one when it has downloaded and only a restart is left. (Thanks
  Pwnagee.)
- **Upgrading a map kept its settings.** The window sent none at all when it
  redid a map for a new release, so one drawn with Knox County roads, a tree
  density or a scale of its own came back with the defaults ΓÇö and then saved
  them over the map's own.
- **A house from an address no longer sits on the road.** Mappers put an
  address point anywhere from the doorstep to the middle of the carriageway,
  and 1.3.6 built the house where the point was; a street of them read as a
  road that had gone missing. Each one is now pushed back off the nearest
  road until it is clear, and left out if it cannot be. How many houses came
  from addresses is in the map's info file and the log, so "it made none of
  mine" is a number.
- **Espa├▒ol and T├╝rk├ºe**, in the language menu. Spanish came from a player -
  thank you. `lang/english.txt` is still the file to copy for any other.
- **A download per system**: `KnoxMap-vΓÇª-windows.zip`,
  `ΓÇª-linux.tar.gz`, `ΓÇª-macos.tar.gz`. The tarballs keep the executable bit
  a zip cannot, so `./setup.sh` runs straight out of one, and each carries
  only the launchers for its own system. The in-app updater takes the file
  built for the PC it is running on, and older releases still install.

- **Linux and macOS.** `./setup.sh` once, `./knoxmap.sh` after that. Drawing
  the terrain, the buildings, the paper map and installing the mod are all
  Python and need nothing extra; Compile runs the map tools ΓÇö Windows
  programs ΓÇö through Wine, which a PC playing Project Zomboid through Proton
  already has. Without Wine every other step still works and the window says
  so, and you can finish a map by hand in WorldEd. `KNOXMAP_WINE` points at a
  particular build; a native build of the tools, dropped in
  `vendor/PZMappingTools/bin` without the `.exe`, is run directly and left
  alone by setup. See [LINUX.md](LINUX.md).
- **A window, or your browser.** pywebview needs a desktop toolkit behind it
  that pip cannot install, so a Linux machine without one had no window at
  all. KnoxMap now checks before it serves and opens your browser instead ΓÇö
  the same app, nothing missing. `KNOXMAP_BROWSER=1` forces that anywhere.
- Steam is found where each system keeps it: the two paths every
  distribution uses, Flatpak, Snap, macOS's Application Support, and
  libraries on a second disk under `/mnt`, `/media` and `/run/media`.
- The project the map tools read carries paths they can follow. Wine shows
  the filesystem as drive `Z:`, so a project saying its export folder was
  `/home/you/maps/town/tmx` named a folder WorldEd could not open; every
  path handed to the tools goes through `winepath` now, while KnoxMap keeps
  opening the real ones itself.
- The updater could not restart the app off Windows: it looked for the
  private Python in `Scripts/` and detached with flags only Windows has.

## 1.3.6

Everything in here works off tags OpenStreetMap uses the world over, so it
lands the same way on a town in Kentucky, in Australia or in Turkey.

- **A row of shops is a row of shops.** A parade, a strip mall or a terrace
  of houses is usually one outline on the map - the mapper drew the block,
  not the seven front doors in it - and it used to come out as one enormous
  shed with a single door. It is now cut into units of about a shop's
  frontage, each its own building standing wall to wall with the next, and
  the shops mapped inside the row are dealt out along it.
- **Loot in every container, however big the building.** The game caps how
  many containers in one room it will fill - for most household and office
  loot the limit is one, two or four - so a huge room had loot at one end and
  bare shelves at the other, which is why a big building "stopped spawning
  loot at some point". No room is bigger than about eleven tiles square any
  more, whatever the building is.
- **Police stations, libraries and fire stations** are laid out as what they
  are, with the game's own rooms: cells, lockers, an evidence store, an
  interrogation room and a gun store in a station; reading rooms in a
  library; the appliance bay and the gear store in a fire station. All three
  used to be an office block with a cupboard.
- **Graveyards have graves in them.** Headstones in rows with paths between,
  the odd wooden cross and now and then an angel - instead of a lawn with
  flowers on it.
- **Army bases exist.** `military=*` - armoury, barracks, hangar, checkpoint,
  training area - was not read at all, so an armoury came out as somebody's
  house. Bases are now fenced off with wire whether or not anyone drew the
  fence, and there are supply crates and drums on the apron.
- **Houses where the map only has an address.** In whole countries, and in
  most American suburbs, the houses are not drawn: what the survey left is
  one point per home with its number on it. Those streets used to be roads
  through empty grass; each address now gets a house.
- **Open country is not a bowling green.** Ground nobody mapped had not one
  tree on it. Trees and scrub are now scattered over open grass, in thickets
  and clearings rather than evenly - and never over farmland, which stays a
  field.
- **Turn the map** by hand: a new setting, in degrees, on top of Straighten
  streets, for a city where the automatic angle picks the wrong grid.
- **Two KnoxMap maps can be installed at once.** Every map used to be built
  at the same place in the world, so a second one claimed the same cells as
  the first and the game fell over on the way in. Each map now takes the
  first free run of cells beside the ones already there; the first map on a
  PC does not move.
- Installing clears the old map out of the mod folder first. A map rebuilt
  smaller used to ship its old cells alongside the new ones.

- **Stop**, on all three long steps. Generating, building and compiling take
  minutes, and the only way out of one was to close the window - which threw
  the drawn rectangle away with it. The button drops the job at the first
  place it can be dropped cleanly (between Overpass tiles, between buildings,
  inside a compile batch, which closes WorldEd down rather than waiting it
  out). The area, the settings and everything already made stay exactly as
  they were: press the step again and it starts over, and a stopped compile
  carries on from the cells it had finished.

Reported in #report-the-bugs:

- **The question marks on the pavement.** The litter rule named
  `trash_01_13`, `14` and `15`, which are blank squares in the sheet Build 42
  ships: the game logged "missing tile trash_01_14" and drew a question mark
  wherever litter fell. Those are gone, one of the three mailboxes was blank
  the same way, and Setup now checks every tile it writes into a rule against
  the artwork so this cannot come back. Run Setup again to take the fix.
- **Buildings with no door anywhere.** A building got exactly one, wherever
  it landed, so a church or a works a hundred metres round had one door
  somewhere along the back. They now get one about every thirty metres,
  spread along the walls, and rows of shops get one per unit.
- **Shopping malls repeating the same rack forty times.** Each aisle is drawn
  from a mix of fittings now, changes what it holds partway along, and the
  cross aisles are staggered, so a supermarket is not a grid of one shelf.
- **Dark shops.** The game hangs one ceiling light off each light switch, and
  a sales floor lit by the single switch beside its door was dark everywhere
  else. A big room gets a switch about every eight metres.
- **Zombies only inside the buildings.** Every zombie came from a building,
  so a town OpenStreetMap has the roads of but not the houses came out empty,
  and the streets between buildings were bare. Paved ground now carries its
  own few, in proportion to how much of it there is.
- Four of Erika's pictures were blank tiles and hung as nothing.
- A map big enough to need more memory than Project Zomboid gives itself gets
  a **HOW TO PLAY.txt** in its mod folder saying so, and how to raise it. A
  map that tears and then closes is usually this.
- The paper map's XML never ships without the binary the game actually reads.

## 1.3.5

- A version menu: click the version at the top of the window for every
  KnoxMap release on GitHub, with what is in each. The one you pick downloads,
  is checked against GitHub's fingerprint and goes in when KnoxMap restarts -
  including an older one, for when a new release breaks something. On an older
  version automatic updates stay off until you choose the newest again.
- **KnoxMap in your language**, from a text file. `lang/english.txt` lists
  every line the window says; copy it, name it after the language -
  `russian.txt`, `deutsch.txt` - translate the right of each `=`, and it
  appears in the menu at the top of the window. No code, no rebuild, and
  whatever is left in English stays English, so a half-finished file works.
  `python tools/make_lang_template.py` writes the English file again after an
  update.
- The download buttons work in the app window (GitHub issue #2). The window
  is not a browser and had nowhere to put a file, so clicking them did
  nothing at all; there they now save the file - making the zip when that is
  what was asked for - and show it in Explorer. In a browser they download as
  before.
- Big maps no longer die with "MemoryError" halfway through drawing. Setup
  now insists on a 64-bit Python - a 32-bit one can only use about 2 GB
  however much the PC has - and makes an environment built by a 32-bit Python
  again; the window lists 64-bit Python among the things setup checks. A map
  too big for the memory there is says so before it starts, with what it
  needs and what is free, and drawing the gardens takes a fraction of the
  memory it did.
- **Reset loot**, in the game: right-click the ground for "Reset loot" and
  pick this building or everything within 30 tiles, and those containers are
  emptied and filled again from their loot tables. A map installed again keeps
  the loot it rolled the first time otherwise, and the only cure was a new
  save. It asks first - anything stored in them is lost - and leaves vehicles,
  corpses and your own inventory alone. Single player only, and it comes with
  every map KnoxMap installs.
- **Your maps**, listed in the window: open one made earlier and build,
  compile or install it again without drawing it from scratch. A map an older
  release made says so - "This map was made with an earlier release. Do you
  want to upgrade?" - and **Upgrade** runs only the steps that release
  changed, usually just Install. Every step now records the version that ran
  it, so this gets more exact from here on.
- The in-game map holds up in a packed city centre: a map cell with more
  outlines than the game can index is simplified, and thinned if it has to
  be, instead of the map screen throwing them away as it drew them.
- Steam libraries are looked up once a minute rather than on every request,
  so a disconnected network drive cannot make the window slow.

## 1.3.4

- The in-game map (M) shows the roads, buildings and water, not just street
  names. Build 42 cannot read the paper map from worldmap.xml any more - every
  outline failed to load ("Error while parsing xml element: geometry" in
  console.txt) - and reads the binary worldmap.xml.bin the game's own maps
  ship instead. Install now writes it. Maps made before this only need
  **Install** again.

## 1.3.3

- Setup no longer fails when Steam still lists a library on a drive that is
  gone ("A device which does not exist was specified"); that library is
  skipped.
- Steam libraries are found on every drive, and you can name the drive or
  folder yourself: in Setup, or in the app under **Settings > Steam
  libraries**.
- Cars spawn. The parking spaces were in the WorldEd project but never
  reached the game, which reads them from objects.lua, and compiling did not
  write one; install writes it now. Install a map again to get its cars.
- Car parks are laid out in rows of parking spaces, and most house drives
  have a car on them.
- **Knox County roads** (under More settings): every road in straight runs
  along the tiles and on 45-degree diagonals, like the game's own map, with
  every building upright. The road network is straightened as a whole, so
  roads still meet where they met, and the buildings, parks and car parks
  move with the streets around them rather than standing in them.
- Petrol stations have a tarmac forecourt reaching the street with a row of
  Fossoil or Gas 2 Go pumps holding fuel, under the canopy where one is
  mapped; so does a station mapped only as a point.

## 1.3.2

- The version you are running is shown at the top of the window, in its
  title bar, and in the details an error copies for a bug report.

## 1.3.1

- A map is no longer stopped by one bad entry. Before compiling, KnoxMap checks
  the WorldEd project: anything past the edge of the map or its cell is moved
  where it belongs or left out, and a building file that is missing or broken is
  left out, all written to the log - instead of WorldEd refusing the whole map
  ("Could not open project", "invalid cell coordinates"). Maps made with older
  versions are repaired too, without building again.
- One building that cannot be laid out is left out, with the reason in the
  log, instead of stopping Build for the rest.
- A problem report includes WorldEd's own logs.

## 1.3

- KnoxMap updates itself: a new release downloads in the background, is checked
  against GitHub's fingerprint, and installs on restart, keeping your maps,
  logs, settings, Python and map tools. Setup and the Python packages are
  brought up to date only when a release changes them, and a map compiler
  setup installed is replaced when a release ships a new one. From 1.2.1 or
  earlier, download 1.3 by hand once; after that it is automatic.

## 1.2.1

- Fixed WorldEd refusing a map with "error reading world, invalid cell
  coordinates": a railing on a bridge along the map's bottom or right edge
  stood in a cell past the edge. Nothing in the project can name such a cell
  now. Maps made before this need **Build** again, then **Compile**.

## 1.2

- A proper error log: logs/knoxmap.log with a description of the PC, every step
  and every error in full; an id on each error in the app; WorldEd's output
  from every compile; a setup log; and a report zip to post in #bug-reports.
- The README's screenshot shows the current window.

## 1.1

**Fixes from the Discord**
- Errors come back as a message and a knoxmap_error.log, not "<!doctype is
  not valid JSON"; and the app runs in UTF-8, which fixes generating a map on
  Korean and Japanese Windows.
- Overpasses and flyovers on ramps, with railed decks on posts, and the roads
  underneath left whole; bridges over water laid square with railings.
- Arches, columns, statues and fountains from OpenStreetMap's monuments.
- Sinks, televisions, lamps and pot plants stand on a counter, cabinet or table;
  nothing blocks the two tiles in front of a door or the tile in front of a
  fridge, stove or wardrobe; a bed keeps floor at its foot.
- No window on the inside corner of a stepped diagonal wall, where it took half
  the wall with it.
- Small blocks of flats have bedrooms and bathrooms, not a living room for
  every flat.

## 1.0 (first release)

KnoxMap grows [Knoxify](https://github.com/arytek/knoxify)'s terrain generator
into a full pipeline, from a real place to an installed Project Zomboid Build 42
map.

**Choosing an area**
- Rectangle, polygon, circle and freehand lasso tools, and a search result's
  real boundary (a park, a district, a town). Only the shape is built; main
  roads and rivers run on past it.
- `?q=<place>&outline=1` opens the app straight to a place.

**Terrain**
- Streets at real widths, with pavements, kerbs and centre lines; the map turns
  so the main street grid runs along the tiles.
- Seas from OpenStreetMap coastlines, harbours and lakes from multipolygons,
  rivers with their bridges, canals, piers and railways.
- Parks, schoolyards, industrial yards, cemeteries, orchards, playgrounds,
  pools, fences, walls and hedges; dense city blocks paved.

**Buildings**
- Every building on its real footprint, turned to the grid, with rooms laid out
  for what it is: houses, blocks of flats with corridors and separate flats,
  shops, schools, churches, clinics, offices, factories and sheds.
- Real heights up to 30 storeys, borrowed from tagged neighbours where missing.
- Lifts in buildings of five storeys or more, working with the Elevators mod.
- Windows that suit the building: kind, size and spacing follow what it is
  and how tall (glass towers, shop fronts, tall panes on flats), with
  matching curtains or blinds. Window count is not a setting.
- A light switch in every room, clear staircases, roofs that follow the
  footprint, loot tables that match the room.

**In the game**
- The paper map (M) with real buildings, water, streets, street names and
  landmarks.
- Zombies spawned from an estimate of who lived and worked in each building,
  with a census and a one-second recount.
- Buildings are what they really are: the shops, restaurants, banks, offices,
  hotels and theatres OpenStreetMap maps inside them become the game's own
  rooms - a pizza place is a dining room and a pizza kitchen, a hotel's floors
  are guest rooms, a theatre is a foyer and an auditorium - so the loot fits.
- Shops fitted out like Knox County's: rows of shelving, fridges along the
  walls, a till by the door and a stockroom behind; offices with desks and
  filing cabinets; every flat with its own sofa, beds, wardrobes and kitchen.
- Restaurants, caf├⌐s and bars fitted out like the game's own: diner and pizzeria
  booth sets, tables with their chairs (against the wall in a narrow place), a
  counter across the back, and kitchens of steel counters, commercial ovens, a
  griddle and fryers. Narrow shops mix fridges with shelving; theatres have rows
  of seats. Army bases get the game's army storage rooms.
- *Square up buildings* setting: how far off the grid a building may be and
  still stand upright (15 degrees by default, 45 for every building).
- No windows, shop fronts or doors in walls shared with the building next door;
  stairs at the back of a shop, not in the middle of it.
- Uses Erika's Tiles when it is installed: glass shop fronts with glass doors
  and shop signs, drinks machines, posters and bookcases in shops, pictures,
  mirrors and plants in homes, and speed limit signs on the streets. Maps made
  with it require it.
- Spawn points inside homes across the town, and the map's landmarks as
  starting points in the Spawn Selector mod when it is installed.

**The app**
- One window: search, generate, build, compile, install. Setup.bat downloads
  the tools, extracts tiles from your own game and checks everything.
- No Python needed beforehand: setup fetches the official python.org build into
  the KnoxMap folder when the PC has none.
- A dark, plain window with a link to the Discord.
- Presets and fine tuning for zombies, living space, heights,
  woodland, parking and more.
- A patched, headless map compiler so compiling needs no clicks in WorldEd.

**Behind the scenes**
- Follows OpenStreetMap's tile, Nominatim and Overpass usage policies; credits
  OpenStreetMap in every map; ships the compiler with its GPL source. See
  [LEGAL.md](LEGAL.md).
- `tools/selftest.py` runs the whole pipeline offline, `tools/audit_layouts.py`
  stress-tests floor plans, and GitHub Actions runs both on every push.
