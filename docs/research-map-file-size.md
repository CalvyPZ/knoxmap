# Shrinking compiled Project Zomboid map files

Date: 2026-10-03

Notes from the decompiled Build 42 client in `Downloads/ZomboidDecompiler` (Vineflower) and from KnoxMap’s own writers. The game will not load a lot file whose version is outside 0..1, and it opens those files by path and seek, so a different encoding is a different loader.

## What a compiled map is

`MapFiles.load` registers three kinds of file from the map directory and ignores the rest: `*.lotheader`, `*.lotpack`, and names starting with `chunkdata_`. (`zombie/iso/MapFiles.java`, `load`.)

At startup, `IsoMetaGrid.MetaGridLoaderThread.loadCell` reads every lot header into rooms, buildings, the per-cell tile-name list, and 1024 bytes of zombie intensity. (`zombie/iso/IsoMetaGrid.java`, `loadCell`.) Tile squares are not read then. `IsoLot.load` opens `world_<x>_<y>.lotpack` with `BufferedRandomAccessFile` on the absolute path, seeks to that chunk’s entry, and decodes one 8×8 chunk. (`zombie/iso/IsoLot.java`, `load`.) `chunkdata_<x>_<y>.bin` is handed to native code as a path string. (`zombie/MapCollisionData.java`, `init` → `n_initMetaCell`.)

Biome images are separate. `BiomeMap.getRaster` loads `media/maps/<name>/maps/biomemap_<x>_<y>.png`. (`zombie/iso/worldgen/maps/BiomeMap.java`.) `BiomeRaster` keeps the red and green channels, one sample per tile. (`zombie/iso/worldgen/maps/BiomeRaster.java`.)

A new chunk is built in `IsoChunk.LoadBrandNew`: lot placement first (`CellLoader.LoadCellBinaryChunk`), then `WorldGenChunk.generateChunks` when `IsoChunk.doWorldgen` is true (it is, by default). (`zombie/iso/IsoChunk.java`, `LoadBrandNew`, `doWorldgen`.)

## The lot format the loader will accept

Header magic is `LOTH`, pack magic is `LOTP`. Both classes reject a version below 0 or above 1. (`zombie/iso/LotHeader.java`; `zombie/iso/IsoLot.java` lines 128–133; `zombie/iso/IsoMetaGrid.java` lines 2204–2205 and 2352–2353.)

A version-1 header is: tile count, each tile name as UTF-8 ending in a newline, chunk width, chunk height, min level, max level, rooms (name, level, rectangles, meta objects), buildings (room index lists), then exactly 1024 zombie-intensity bytes. Width and height must both be 8 or the cell is rejected as an invalid chunk size. A short zombie block is an `EOFException`. (`IsoMetaGrid.java`, `loadCell`; the matching writer is `zombie/pot/POTLotHeader.java`, `save`.)

A version-1 pack is: magic, version, chunk size as an int, then 1024 little-endian 8-byte offsets. The reader only uses the low 4 bytes of each offset. (`zombie/pot/POTLotPack.java`, `save`; `IsoLot.java` seeks to `(version >= 1 ? 8 : 0) + 4 + index * 8`.) Each chunk is then, for every level from min to max and every square in the 8×8:

- `count == -1`, then a second int: that many squares are empty. The reader also consumes the current square, so a run of N empties is one pair of ints.
- `count > 1`: one int room id, then `count - 1` tile-name indexes, each an int.
- `count` of 0 or 1: the square is empty and nothing else is read.

The room int is read and dropped. `PlaceLot` assigns the room from the meta grid by coordinate, not from this field. (`IsoLot.java` lines 175–179; `zombie/iso/IsoCell.java`, `PlaceLot`.) The writer still stores −1 for every occupied square. (`POTLotPack.java`, `saveChunk`.)

`chunkdata` is already a bitmask. Version short 1, then 32×32 chunks: one type byte. Types 0, 1, 3, and 4 (empty, solid, water, room) are that single byte. Type 2 is followed by 64 bytes, one per square, with bits for solid, wall north, wall west, water, and room. (`zombie/pot/POTChunkData.java`.) A uniform cell is about 1 KB. A fully mixed cell is about 65 KB.

There is no compression stage. The pack is a real file because the loader seeks. A gzip of the whole pack cannot be substituted for the file the game opens.

## Where the bytes go

An occupied square costs 12 bytes for a single tile: count, discarded room id, one index. A compiled cell is 256×256 = 65,536 squares. One floor on every square is 65,536 × 12 = 786,432 bytes, before extra tiles. A tree, a bush, or furniture adds another 4 bytes on that square. Empty ground is cheap: one 8-byte run covers a whole empty chunk, and an empty chunk still needs its 8-byte offset slot.

Tile-name strings are repeated per cell, not shared. A few hundred names are tens of kilobytes. They are not the bulk. Rooms and buildings are larger in a town than in open ground, and they have to stay: loot, building bounds, and room ids come from the header, not from the pack.

KnoxMap already drops cells that are only dark grass plus forest vegetation, and says the game fills those. (`KnoxMap/knoxbuild/mapped_cells.py`; `KnoxMap/knoxbuild/world.py`.) Any other ground in the cell — a road, a yard, lighter grass, water, sand — keeps the cell, and WorldEd then stamps the bitmap into tiles. Those stamped squares are the large files.

Biome PNGs are 256×256 RGB with the index copied into R, G, and B. (`KnoxMap/generator/biomes.py`, `write_biome_maps`.) The game only reads R and G. They are small next to a full lot pack.

`worldmap.xml.bin` is the paper map (`IGMB`, versions 1 and 2, cell size must be 256). Version 1 is rejected. (`zombie/worldMap/WorldMapBinary.java`.) It is not the lot data.

## What still shrinks inside this loader

The empty-run encoding is the only repetition the pack understands. It does not run-length identical tiles. A field of one grass tile is stored as 12 bytes per square. A new opcode for “repeat this tile” would be a new version, and version 2 is thrown away.

Leaving a square with no objects is the supported compact form. After lots are placed, `generateChunks` checks the chunk:

- If any level-0 square is empty (and has nothing below it), `genRandomChunk` runs. A square that already has objects is kept. An empty square is filled from the biome for that pixel, or from a worldgen road, or from a static prefab. (`WorldGenChunk.java`, `generateChunks`, `genRandomSquare`, `hasEmptySquaresOnLevelZero`.)
- If every level-0 square has objects, `genMapChunk` runs instead. That path can swap tiles listed in the biome’s replacement table and can add or remove plants. It does not throw away the authored floor. (`WorldGenChunk.java`, `genMapSquare`, `replaceSquare`.)

So a natural floor that is written into the pack is stored in full and is what the player stands on. The same pixel in `biomemap_*.png` does not replace it. Omitting that floor, and the vegetation on it, lets `genRandomChunk` paint the biome KnoxMap already wrote. Roads, building floors, walls, and furniture have to remain: worldgen roads are the story generators, not the map’s streets, and `genRandomSquare` will not overwrite a square that already has objects.

A chunk can mix the two. One authored building in an otherwise empty chunk keeps the building and generates the rest. Stripping only the floor from under a tree leaves the tree in place and skips generation, so the ground under it never appears. Floor and vegetation on a procedural square have to go together.

Cells with no header and no pack still generate, because `LoadCellBinaryChunk` returns false when the pack name is absent and `LoadBrandNew` generates anyway. (`CellLoader.java`, `LoadCellBinaryChunk`; `IsoChunk.java`, `LoadBrandNew`.) They do not get rooms, buildings, or the header’s zombie grid. `chunkdata` for a natural cell is optional in the sense that a missing name is a null path into `n_initMetaCell`; collision for a visited chunk is rebuilt from the live squares in `doLoadGridsquare`. Unvisited zombie pathing is what the bitmask file is for, and that file is already the small one.

The practical cut is therefore inside compiled cells: stop emitting pack squares whose only tiles are the natural ground and plants the biome will regenerate. Keep the header and the pack for squares the biome cannot reconstruct. That is the same split `mapped_cells.py` already makes between cells, applied per square. It does not need a new format. How close the generated ground looks to today’s bitmap is the biome definition, not the pack.

## What a different format would require

A denser pack would be per-chunk compression, or a repeat code for identical tiles, plus 16-bit indexes. The same grass cell that is ~768 KB of ints compresses very well; a furnished cell compresses less, because the walls and furniture indexes actually vary. Either change fails the version check in both `IsoLot` and the meta-grid loader. The failure is logged and the chunk’s square data is cleared. (`IsoLot.java`, the `catch` that fills `offsetInData` with −1.)

Nothing in `zombie.ZomboidFileSystem` loads replacement Java classes from a mod. The lot reader is not a Lua hook. `LoadGridsquare` runs only for squares that already have objects, and `LoadChunk` runs at the end of `doLoadGridsquare`, after placement and after worldgen. (`IsoChunk.java`, `doLoadGridsquare`.) A Lua script can add tiles then, on the client and the server, but it cannot stand in for the header: rooms, buildings, and zombie intensity are already loaded. It also runs again when the chunk is loaded from the save, so it has to be idempotent. Using it as the building store would leave `chunkdata` wrong for zombies until the chunk is visited, unless the bitmask is still shipped. That bitmask is not the size problem.

Replacing `IsoLot` and `MetaGridLoaderThread` means patching the game install. Every player and the dedicated server would need the same patch, and a game update would wipe it. Workshop mods are the media files `MapFiles` already scans, not a new loader.

Zipping the mod shrinks the download. The installed files stay large, because the game seeks in the real `world_x_y.lotpack`.

## No binary reader runs a mod parser

There is no file the unmodified client already opens whose parse calls Lua, `RunLua`, a Kahlua table, a script path, or a mod-registered handler. A workshop mod cannot register a new binary extension, a file device, or a compressed stand-in. The searches below are the readers and the call that would have to invoke a parser we ship. None of them make that call.

`IsoLot.load` decodes one chunk and returns. Between the last `this.data.add(d)` and the method’s `catch`, there is no `LuaManager` and no `LuaEventManager`. (`zombie/iso/IsoLot.java` lines 170–192.) The caller is `CellLoader.LoadCellBinaryChunk`, which goes from `IsoLot.get` to `IsoCell.PlaceLot` with no Lua in between. (`zombie/iso/CellLoader.java` lines 375–381.) `IsoMetaGrid.MetaGridLoaderThread.loadCell` reads the header, rooms, buildings, and the 1024 zombie bytes, and the class has no `LuaManager` or `triggerEvent` reference. (`zombie/iso/IsoMetaGrid.java` lines 2179–2358.)

| File | Reader | Call that would have to run our parser, and does not |
|---|---|---|
| `world_x_y.lotpack` (`LOTP`) | `IsoLot.load` | No call after the int decode (lines 170–186) and none in `PlaceLot`’s caller before `DoTileObjectCreation`. |
| `x_y.lotheader` (`LOTH`) | `MetaGridLoaderThread.loadCell` | No call after rooms, buildings, or the zombie block (lines 2229–2347). Version outside 0..1 throws at line 2353. |
| `chunkdata_x_y.bin` | `MapCollisionData.init` → `n_initMetaCell(cx, cy, path)` | The Java side passes a path string into native code. (`zombie/MapCollisionData.java` lines 113–119.) No Lua on that call. |
| `media/binmap/<name>.pzby` and `media/basement_access/<name>.pzby` (`PZBY`) | `NewMapBinaryFile.loadHeaderInternal`, `loadChunkInner`, `addToIsoChunk` | Header returns at line 280. Chunk body returns at line 366. Placement calls `CellLoader.DoTileObjectCreation` (line 651). No `RunLua` and no `triggerEvent` in the class. |
| `map_basements.bin` (`BSMT`) | `Basements.loadSavefile` | Reads placements and building rects from the save. (`zombie/basements/Basements.java` lines 177–226.) No Lua. `onNewChunkLoaded` then calls `SpawnBasementInChunk` (lines 434–443), which is the PZBY reader above. |
| `player_buildings.bin` (`PRBG`) | `PlayerRoomsFile.load` | Save-folder buildings the player defined. Version must be 1. (`zombie/buildingRooms/PlayerRoomsFile.java` lines 119–126.) No Lua. |
| `map_worldgen.bin` (`WGEN`) | `WorldGenParams.load` | Seed and cell bounds from the save. (`zombie/iso/worldgen/WorldGenParams.java` lines 119–149.) No Lua. |
| `worldmap.xml.bin` (`IGMB`) | `WorldMapBinary.read` | Paper-map cells, features, and coordinates. (`zombie/worldMap/WorldMapBinary.java` lines 25–64.) No Lua. Version 1 is rejected. |
| `map_meta.bin`, `map_zone.bin` (`ZONE`), `map_animals.bin` | `IsoMetaGrid.load`, `loadZone`, `loadAnimalZones` | Save-folder meta and zones. `IsoMetaGrid.java` has no `LuaManager` or `triggerEvent`. |
| `map_symbols.bin`, `servermap_symbols.bin` (`WMSY`) | `MapItem`, `WorldMapServer` | Annotations in the save. No Lua in the read. |
| `map_t.bin`, `map_ver.bin` | `GameTime`, `IsoWorld`, `GameLoadingState` | Clock and world version in the save. Not tiles. |
| `{save}/map/{wx}/{wy}.bin` | `IsoChunk.LoadFromDiskOrBufferInternal` | Save-chunk objects. World version above 249 throws. (`zombie/iso/IsoChunk.java` lines 3465–3494.) The byte loop does not call Lua. `LoadGridsquare` and `LoadChunk` run later, in `doLoadGridsquare`. |
| `media/texturepacks/<name>.pack` (`PZPK`, or no magic for version 0) | `TexturePackDevice.initMetaData` | Page names, sub-rects, then a PNG blob. (`zombie/fileSystem/TexturePackDevice.java` lines 81–150.) No Lua. Version other than 1 with `PZPK` throws at line 96. |
| `*.tiles` (`tdef`) | `IsoWorld.LoadTileDefinitions` | Sprite sheets and properties. Version must be 1. (`zombie/iso/IsoWorld.java` lines 623–641.) No Lua in the reader. `mod.info` `tiledef=` only points this loader at another `.tiles` file. |

`OnMapLoadCreateIsoObject` is registered with `AddEvent` and has no `triggerEvent` anywhere in the client. (`zombie/Lua/LuaEventManager.java` line 681.)

### Dispatch a mod can fill in

`MapFiles.load` keeps three names from a map directory: `*.lotheader`, `*.lotpack`, and `chunkdata_*`. Every other file in that directory is ignored. (`zombie/iso/MapFiles.java` lines 83–97.)

`map.info` keys handled by `ChooseGameInfo.getMapDetails` are `title`, `lots`, `description`, `fixed2x`, `zoomX`, `zoomY`, `zoomS`, `demoVideo`, and `only_for_game_mode`. There is no `file=` key. (`zombie/gameStates/ChooseGameInfo.java` lines 73–97.) `lots=` is not ignored: `MapFiles.getLotDirectories` walks it and loads each name as another `media/maps/<dir>/`. (`zombie/iso/MapFiles.java` lines 204–214 and 183–196.) That directory then goes through the same three-name filter. A `lots=` folder of custom binaries is not handed to Lua.

`mod.info` `pack=` calls `mod.addPack`. The file system later mounts that name as a `TexturePackDevice` on `media/texturepacks/<name>.pack`. (`ChooseGameInfo.java` lines 256–292; `zombie/fileSystem/FileSystemImpl.java` `mountTexturePack`, lines 254–267.) The pack is textures. Lot loading never opens it.

### File devices and compression

`IFileDevice` has three implementations: `DiskFileDevice`, `MemoryFileDevice`, and `TexturePackDevice`. `FileSystemImpl.mount` returns true and does not store the device. (`FileSystemImpl.java` lines 42–44.) Nothing in `zombie.fileSystem` calls Lua. A mod cannot register a device that decompresses or rewrites bytes before `IsoLot` sees them, and `IsoLot` does not go through this layer: it opens `BufferedRandomAccessFile` on the absolute path. (`IsoLot.java` lines 124–125.)

`TexturePackDevice.createStream` seeks to a PNG inside the pack. The PNG decoder’s `Inflater` is the IDAT zlib stream. (`zombie/core/textures/PNGDecoder.java`.) That is not on a map path.

The other `Inflater` / `Deflater` uses are network chunk packets (`WorldStreamer.decompress`, lines 312–332), the visited-paper-map mask (`WorldMapVisited`), and the server’s chunk upload (`PlayerDownloadServer`). None of them wrap `lotpack`, `lotheader`, `pzby`, or `chunkdata`. There is no `GZIPInputStream`, zstd, or LZ4 on those paths. `ZomboidFileSystem.searchFolders` records every file under a mod; it does not unzip a map on read. (`ZomboidFileSystem.java` lines 277–296.)

### What the game will execute

`LuaManager.searchFolders` adds a file only when the path ends with `.lua`. (`LuaManager.java` lines 1250–1260.) `RunLuaInternal` opens it with `IndieFileLoader.getStreamReader` and `LuaCompiler.loadis` on a `BufferedReader`. (`lines 1356–1367`.) That is source text. A binary chunk is not loaded. `require` appends `.lua` when the argument does not already end with it. (`lines 4942–4953`.)

`ScriptManager.searchFolders` adds only `.txt`. (`zombie/scripting/ScriptManager.java` lines 1333–1347.) `ModRegistries.init` runs exactly `media/registries.lua`. (`zombie/scripting/objects/ModRegistries.java` lines 14–20.) `SpawnRegions` and `SandboxOptions` call `RunLua` on Lua files (`defines.lua` and the regions file), not on map binaries.

The one map-adjacent `RunLua` is `WorldGenChunk.runLuaOverride`, which runs `media/maps/<name>/WorldGenOverride.lua` if that file exists. (`zombie/iso/worldgen/WorldGenChunk.java` lines 116–124.) The constructor then reads the `worldgen` table in Java. `loadPrefab` parses Lua tables of comma-separated integers. (`WorldGenReader.java` lines 515–548.) `applyPrefab` repeats that schematic with modulo on squares `genRandomSquare` still considers empty, at z=0. (`WorldGenChunk.java` lines 202–208 and 651–664.) That file is Lua the game executes by path. It is not a binary open, and the expander is `applyPrefab`, not code we supplied as the parser. A unique city stored as those strings is text integers, larger than the lot pack’s 4-byte indexes.

### Basement binary

`NewMapBinaryFile` is constructed with `pot == true`, so `chunkDim` is 8. (`NewMapBinaryFile.java` lines 33–37 and 105.) `loadHeaderInternal` requires the bytes `P Z B Y` (80, 90, 66, 89). It stores `version` and never reads it again; `loadChunkInner` does not branch on it. There is no compressed section and no `Inflater`.

After the magic the header is: tile-name count, each name from `IsoLot.readString` (one line), width, height, level count, rooms (name, level, rectangles, meta objects), buildings (room-index lists). The chunk table starts at the file pointer after the buildings. (`lines 214–280`.) Each chunk is `index * 8` bytes into that table, then `IsoLot.readInt` of the low 4 bytes, then seek. (`lines 299–302`.) `readInt` is little-endian. (`IsoLot.java` lines 66–75.)

The body is two full rasters of `levels * 8 * 8` squares:

- Attributes: `count == -1` plus a second int skips that many squares (the reader also consumes the current square). `count > 1` reads one int. Otherwise the attribute is 0.
- Tiles: the same skip rule. `count > 1` reads `count - 1` ints, each an index into the header’s tile-name list. (`loadChunkInner`, lines 303–364.)

That is the lot pack’s 4-byte index layout, plus a second raster the lot pack does not have. A furnished square is larger in a `.pzby` than in a `.lotpack`, not smaller. `loadBasementDefinitionHeaders` drops a definition unless `header.buildingDefList.size()` is exactly 1. (`Basements.java` lines 466–469.) Placement Z is `basementSpawnLocation.z - header.levels`. (`line 364`.) The same file name can be chosen at many spawn points, and each choice stamps that one footprint. The reader does not expand one compact encoding into a city.

### Load-from-buffer

`IsoChunk.LoadFromBuffer` calls `LoadFromDiskOrBuffer`, the same path as `{save}/map/{wx}/{wy}.bin`. (`IsoChunk.java` lines 2355–2360 and 3465–3494.) `WorldStreamer.DoChunkAlways` passes the buffer in as `fromServer` after `decompress` has inflated the network packet. (`WorldStreamer.java` lines 262, 312–332, 476–485.) `ChunkMapFilenames.getFilename` roots the on-disk file in the save’s `map/{wx}/{wy}.bin`. (`zombie/ChunkMapFilenames.java` lines 39–53.) A file shipped in the mod is not that buffer and is not that path. The inflated bytes are a saved chunk (version, CRC, objects), and the reader does not call Lua while it walks them.

### `LoadChunk` is not that binary call

`LuaEventManager.triggerEvent("LoadChunk", this)` is the last statement of `IsoChunk.doLoadGridsquare`. (`IsoChunk.java` line 3974.) `triggerEvent("LoadGridsquare", square)` is earlier, and only for a square that already has objects. (`lines 3809 and 3840`.) `MapObjects.newGridSquare` / `loadGridSquare` run in that same loop, and they call a Lua closure only when an object’s sprite name was registered with `OnNewWithSprite` / `OnLoadWithSprite`. (`zombie/Lua/MapObjects.java` lines 89–107 and 185–198.) None of these are reached from `IsoLot.load`, `loadCell`, or `loadChunkInner`.

Order on a chunk that has no save file: `LoadBrandNew` places the lot, then `generateChunks`, then `Basements.onNewChunkLoaded`. (`IsoChunk.java` lines 2253–2264.) `doLoadGridsquare` runs after that. Inside it, `MapCollisionData.addChunkToWorld` and `PathfindNative.addChunkToWorld` run at lines 3866–3870, and `LoadChunk` runs at line 3974. The global `createTile` does call `DoTileObjectCreation` (`LuaManager.java` lines 6589–6608), and `DoTileObjectCreation` appends through `CellLoader.AddObject`, which does not call `addToWorld`, `MapCollisionData.squareChanged`, or `PathfindNative.squareChanged`. (`CellLoader.java` lines 532–539.) Tiles created from `LoadChunk` are therefore after the chunk collision and native pathfind registration, and `createTile` does not update those itself. `IsoGridSquare.AddTileObject` does call `squareChanged` on `MapCollisionData` and `PolygonalMap2` (`IsoGridSquare.java` lines 6309–6310), and it does not call `PathfindNative.squareChanged`. `PathfindNative.squareChanged` exists (`zombie/pathfind/nativeCode/PathfindNative.java` lines 168–182) and is not what `createTile` uses.

`genRandomSquare` sets `square.setRoomID(-1)` on every square it creates. (`WorldGenChunk.java` line 215.) Room and building records still come only from the lot header (or from the basement header merge). A `LoadChunk` script cannot invent a `RoomDef` the meta grid did not load. Zombie intensity for an unvisited cell still comes from the header’s 1024 bytes and from `chunkdata`, via `n_initMetaChunk` / `n_initMetaCell`. (`MapCollisionData.java` lines 117–127.)

The dedicated server runs `LoadDirBase("shared")`, `LoadDirBase("client", true)`, and `LoadDirBase("server")`. (`zombie/network/GameServer.java` lines 1457–1459.) The `true` is `onlyChecksum`, which does not call `RunLua`. (`LuaManager.java` lines 1205–1207.) A parser in `media/lua/client` does not run on the dedicated server. `doLoadGridsquare` itself does fire `LoadChunk` on the server: that trigger is not inside a `!GameServer.server` block.

`getModFileReader` is a UTF-8 `BufferedReader` rooted at the mod directory. (`LuaManager.java` lines 5193–5228.) `getGameFilesInput` is a `DataInputStream`, and only when `ZomboidFileSystem.isKnownFile` is true. (`lines 5975–5988`.) `loadMod` puts every file under the mod into `activeFileMap` (`ZomboidFileSystem.java` lines 649–659), so a compact file shipped in the mod can be read from Lua. That is Lua pulling bytes. No map reader calls it.

Installed size, if the lot pack is omitted and that Lua file is what places tiles: one occupied square in the pack is 12 bytes for a single tile (count, discarded room id, one index). A 256×256 floor is 65,536 × 12 = 786,432 bytes, plus the 1,024 × 8 byte offset table. A run-length side file of identical tiles is a few integers per run, so a uniform grass cell shrinks by about two orders of magnitude. A cell of unique tiles shrinks from 12 bytes a square to about 4 (the index alone), about threefold, and the tile-name list and the lot header remain if rooms are still required. The header is not the bulk; the pack is. This side file is not opened by `IsoLot`. The first visit still writes a full `{save}/map/{wx}/{wy}.bin`. The workshop copy is the part that gets smaller. Rooms, the zombie grid, and unvisited `chunkdata` do not come from that Lua read. `isNewChunk` is `addZombies`, set in `LoadBrandNew` only when the chunk is not loaded from the save (`IsoChunk.java` lines 2265–2267 and 5155–5156), so a second load of the save bin fires `LoadChunk` again on squares that already exist.

## Other files the client already opens

A pass over every other loader in the same decompile found one whole-map tile path. `CellLoader.LoadCellBinaryChunk` calls `IsoLot.get` and then `IsoCell.PlaceLot`. Those two calls read `x_y.lotheader` and `world_x_y.lotpack` only. (`zombie/iso/CellLoader.java`, `LoadCellBinaryChunk`.)

| File the game opens | Reader | What it places |
|---|---|---|
| `x_y.lotheader` | `IsoMetaGrid.MetaGridLoaderThread.loadCell` | Tile names, rooms, buildings, zombie intensity |
| `world_x_y.lotpack` | `IsoLot.load` | Per-square tile indexes, then `PlaceLot` |
| `chunkdata_x_y.bin` | `MapCollisionData.n_initMetaCell` | Collision bits for zombies, not tiles |
| `media/binmap/<name>.pzby` (`PZBY`) | `NewMapBinaryFile.loadHeader`, then `Basements.onNewChunkLoaded` → `SpawnBasementInChunk` | Tiles via `DoTileObjectCreation`, at `spawnZ - header.levels` |
| `media/maps/<name>/WorldGenOverride.lua` | `WorldGenChunk.runLuaOverride` → `WorldGenReader.loadPrefab` | A repeating z=0 schematic on squares the lot pack left empty |
| `maps/biomemap_x_y.png` | `BiomeMap.getRaster` | Biome index for that fill |
| `{save}/map/{wx}/{wy}.bin` | `IsoChunk.SafeRead` | A full saved chunk, if the file is already in the save |
| `spawnpoints.lua` | `SpawnRegions.loadPointsFile` | Player spawn coordinates |
| `objects.lua` | No Java reader. `OnLoadMapZones` is the Lua hook | Zones, when stock Lua loads the file |
| `worldmap.xml.bin` | `WorldMapBinary.read` | Paper-map geometry |
| `media/texturepacks/*.pack` | `GameWindow.LoadTexturePack` | Textures. Lot code never mounts it |

`objects.lua` does not appear anywhere in the decompiled Java. `spawnpoints.lua` is the spawn list. Neither is a tile grid.

`.pzby` is the only other binary that reaches `DoTileObjectCreation`. `ZomboidFileSystem.getString` will resolve a mod copy under `media/binmap/`. Placement sets Z to `spawnZ - header.levels`, so a one-level file spawned at 0 lands at −1. `Basements.loadBasementDefinitionHeaders` drops a file whose header does not contain exactly one building. The tile body is the same count, skip, and 4-byte index layout as a lot pack. (`NewMapBinaryFile.loadChunkInner`; `Basements.calculateBasementPlacements`.)

The saved chunk `{save}/map/{wx}/{wy}.bin` is a full placed chunk, and `LoadOrCreate` uses it instead of the lot pack when the file exists. `ChunkMapFilenames.getFilename` roots that path in the save cache. Lua cannot write it there: `getFileWriter` is UTF-8 and allows only `ini`, `cfg`, `txt`, `log`, and `json`; `getFileOutput` is binary and writes under the Lua cache. (`LuaManager.java`.)

`OnGameBoot` and `OnPreMapLoad` run before `MapFiles.load` lists the map directory. There is still no binary writer aimed at `media/maps`, and `IsoLot.load` never calls Lua between filling `lot.data` and `PlaceLot`.

## Bottom line

The stock loader cannot be pointed at a more efficient lot encoding. Version 1, 8×8 chunks, int tile indexes, and a seekable uncompressed pack are fixed. The file unmodified Java places as a whole map is that pair: `x_y.lotheader` for rooms and names, `world_x_y.lotpack` for the squares. The bytes that dominate are authored floors and plants on squares the biome map was already going to fill. Dropping those squares, and leaving pure natural cells uncompiled, is the shrink the current loader will honour. A compressed or run-length pack only works if the game’s `IsoLot` is replaced, and a map mod cannot replace it.

No other binary the client already opens runs a parser we ship. The readers are listed under “No binary reader runs a mod parser.” Each one would have to call `RunLua`, `triggerEvent`, or a mod-registered handler during the parse, and none of them do. `LoadChunk` is later, after the squares exist, and it is not that call.
