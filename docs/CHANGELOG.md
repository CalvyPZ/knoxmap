# Changelog

## 1.5

- **KnoxMap is one file.** Windows gets `KnoxMap.exe`, Linux an AppImage,
  macOS a dmg. Python and the libraries are already inside. Put the file
  anywhere and open it. Maps, logs and the first-run downloads sit in the
  folder beside it (on a Mac that lives in Applications, they sit in a
  `KnoxMap` folder in your home directory). The map tools and the game's
  tiles are still fetched the first time, from the tools' own release and
  from your own copy of Project Zomboid — they are not inside the file. That
  first-run setup happens inside the program. There is no setup script to run.

  The window is the same Electron window on every system. The page is
  unchanged — the map, the search, the pipeline — and Python still serves it
  and still does the work. A source checkout opens that window from `desktop/`
  after `npm install`, or the browser when `KNOXMAP_BROWSER=1`.
  On Windows, start that checkout from the `KnoxMap` folder with
  `.venv\Scripts\pythonw.exe knoxmap.py`. On Linux and macOS, `./knoxmap.sh`.

  The small Windows launcher and the separately downloaded window are gone.
  Players are not handed a folder of scripts. Downloads ask where to save.
  Closing KnoxMap while a map is being made asks first, and a compile in
  progress is stopped so the map compiler is not left running.

  On Linux the window no longer needs GTK or Qt installed for Python. It does
  need the ordinary desktop libraries, which a desktop already has. If the
  sandbox cannot start — the helper is not setuid, or AppArmor will not allow
  a user namespace — KnoxMap opens it once more without the sandbox. The page
  is only this computer's.

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
  with an error: over 400 km², over 9,000 tiles a side, or more memory than
  the PC had free. Somebody who wanted a whole city could not have one at
  all, whatever their machine.

  Every one of those is a warning now and the button stays lit. The panel says
  what the area will cost — the tiles, the gigabytes of ground and greenery
  held at once, how many OpenStreetMap queries — and adds that raising metres
  per tile is the cheapest fix, because 2 m a tile is a quarter of the memory
  of 1 m. Then it gets out of the way. Landmark lookup is the same: over
  40 km² it is slow, so it says so instead of refusing.

  A limit that says no is worth having only when the thing behind it cannot be
  done. These can be done; what they cost is the mapper's to spend.

  What is still refused is a scale that is not a scale — zero, a negative, or
  something absurd — because dividing the world by nothing is not a map
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
  | Gifhorn, Germany | 1,962 | **60** (3%) | 38 m² — sheds and garages |
  | Ürgüp, Turkey | 473 | **761** (62%) | 87 m² — houses |

  So it is **off by default**. Where OSM is complete it adds sheds and costs a
  few minutes; where OSM is thin it nearly trebles the town. You can see which
  case you are in from the preview before you turn it on.

  Overture's `class` is OpenStreetMap's own building values — house,
  apartments, barn, church — so where it has one the building arrives already
  classified. The machine-found ones have none and arrive as plain
  footprints, which is what they are; the generator reads the land around
  them for the rest, as it does for any untagged building.

  It needs DuckDB, because the data is GeoParquet on S3 and there is no
  bounding-box API for it. That is not a small install, so it is optional and
  listed under the optional extras with what to type. Everything else works
  without it. One fetch takes two to three minutes, nearly all of it spent
  finding which of Overture's files cover your box, so the answer is kept
  beside the map like the Overpass one — and a map that has been fetched once
  re-renders with no DuckDB at all, on any PC, including one you hand the
  folder to.

  A map built this way credits Overture in its `ATTRIBUTION.txt` alongside
  OpenStreetMap, which ODbL asks for; one built without it is unchanged.

  This does not replace the houses KnoxMap already invents from OSM's own
  address points — those are the homes somebody numbered but never drew, 22
  of them in Gifhorn and 20 in Ürgüp, and they are still filled in afterwards
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
  had to be made absolute for exactly this reason — so WorldEd looked for it
  beside its own executable, found nothing, and baked every map with no
  zombies at all. Measured on the same map compiled both ways: sixteen
  chunkdata files and 23,054 bytes of zombie data with the fix, and none
  without it. Maps already made need Compile and Install again.
- **Walls you can see straight through.** A map built with Erika's Tiles
  said `require=\Erikas_Tiles` in its mod.info. No mod has that id — the id
  is `Erikas_Tiles`, with nothing in front of it — so the game neither
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
  density or a scale of its own came back with the defaults — and then saved
  them over the map's own.
- **A house from an address no longer sits on the road.** Mappers put an
  address point anywhere from the doorstep to the middle of the carriageway,
  and 1.3.6 built the house where the point was; a street of them read as a
  road that had gone missing. Each one is now pushed back off the nearest
  road until it is clear, and left out if it cannot be. How many houses came
  from addresses is in the map's info file and the log, so "it made none of
  mine" is a number.
- **Español and Türkçe**, in the language menu. Spanish came from a player -
  thank you. `lang/english.txt` is still the file to copy for any other.
- **A download per system**: `KnoxMap-v…-windows.zip`,
  `…-linux.tar.gz`, `…-macos.tar.gz`. The tarballs keep the executable bit
  a zip cannot, so `./setup.sh` runs straight out of one, and each carries
  only the launchers for its own system. The in-app updater takes the file
  built for the PC it is running on, and older releases still install.

- **Linux and macOS.** `./setup.sh` once, `./knoxmap.sh` after that. Drawing
  the terrain, the buildings, the paper map and installing the mod are all
  Python and need nothing extra; Compile runs the map tools — Windows
  programs — through Wine, which a PC playing Project Zomboid through Proton
  already has. Without Wine every other step still works and the window says
  so, and you can finish a map by hand in WorldEd. `KNOXMAP_WINE` points at a
  particular build; a native build of the tools, dropped in
  `vendor/PZMappingTools/bin` without the `.exe`, is run directly and left
  alone by setup. See [LINUX.md](LINUX.md).
- **A window, or your browser.** pywebview needs a desktop toolkit behind it
  that pip cannot install, so a Linux machine without one had no window at
  all. KnoxMap now checks before it serves and opens your browser instead —
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
- Restaurants, cafés and bars fitted out like the game's own: diner and pizzeria
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
