# Scaling OpenStreetMap and map-compiler pipelines to large areas

Date: 2026-09-25

Notes from primary sources only: the OpenStreetMap wiki, the Overpass API manual, the OSMF Operations site, Geofabrik’s download server, planet.openstreetmap.org, the Osmium and pyosmium manuals, the PBF format specification, and the Overture Maps documentation. Download sizes are the figures those servers published when the pages were read on this date.

Project Zomboid and WorldEd internals are out of scope.

## 1. Overpass API limits

The Overpass API is a read-only database over the web: the client sends a query and gets back the matching elements. It is aimed at data consumers who need a few elements quickly, or up to roughly 10 million elements in some minutes, selected by location, type, tags, proximity, or combinations of those. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

Public servers are described as built for small projects and as often becoming overloaded. The same notice says to consider your own server, a commercial provider, or a regional dump filtered with osmium. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

### Published usage on the main instance

For `https://overpass-api.de/api/interpreter` the wiki’s usage policy says you can assume you do not disturb other users when you do fewer than 10,000 queries per day and download less than 1 GB of data per day, and that this limit is for one-off use. If something uses the API regularly, those numbers are to be divided by 100, so fewer than 100 queries and less than 10 MB per day. Usage of an app or website counts as the sum of requests made by all of its users. Requests should send a `User-Agent` or `Referer` that uniquely identifies the app. Parallel running of multiple scripts is not allowed. Commercial use should use a self-hosted or paid server. Calls should be cached and rate-limited, and extracts should be used when a lot of data is needed. An HTTP 429 or 406 should be followed by a 30-second pause before the next request. The same row says the server is overloaded, that callers should not overconsume or expect high reliability, and that alternatives should be used when possible. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

The operator’s manual states the same broad ceiling as a safety margin: a maximum of about 10,000 requests per day and download volume below about 1 GB per day. Above that, it points to the installation instructions for a private instance. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

### Capacity, slots, and cooldown

The public instances exist to stay available to as many users as possible. Computational power is shared among about 30,000 daily users. A typical request runs in less than one second. Each Overpass API server can fulfill about 1 million requests per day, and two servers listen on `overpass-api.de`. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

Those two servers are currently named `gall.openstreetmap.de` and `lambert.openstreetmap.de`. They rate-limit independently of each other. Callers are told not to pick one explicitly, except as a workaround when the other is broken in a way DNS round-robin cannot hide. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

The user is the full IPv4 address, or the upper 64 bits of an IPv6 address, unless the request carries a user key, which overrides the address. The server’s user number is the first line of the status response, after `Connected as:`. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

Each request occupies one slot for the full execution time plus a cooldown. Cooldown exists so other users can run, grows with server load, and scales with execution time. At low load it is a fraction of the execution time. At high load it can be a multiple of the execution time. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

Two goodwill mechanisms cover a slippy map’s burst of short requests. Users receive multiple slots; the count is on line 3 of the status response after `Rate limit:`. If no slot is free, a request may stay queued for up to 15 seconds. The manual’s example is 20 requests of 1 second each, 2 slots, and a 1-to-1 run-to-cooldown ratio: the first two run immediately, the next two start after 2 seconds, requests 15 and 16 start after 14 seconds, and requests 17 to 20 are discarded after 15 seconds because they never got a slot. The client is expected to resubmit those if it still needs them. Denied rate-limit requests return HTTP 429. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

The first requests from a user are given priority over frequent requests from heavy users, so load shedding starts with heavy users. Most users send only a few requests. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

### Timeout, maxsize, and why a large bbox is the wrong tool

If a query does not declare a maximum run time, the default is 180 seconds. If it does not declare a maximum memory use, the default is 512 MiB. Either can be set with `[timeout:…]` and `[maxsize:…]`. A request that exceeds its own declaration is aborted. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

Separately, the server admits a request only when both declared run time and declared memory are at most half of the resources still free. The maximum accepted memory is currently 12 GiB. The manual’s example: eight running requests at 512 MiB each use 4 GiB, so the next request is admitted only if it promises at most 4 GiB, and the one after that only up to 2 GiB. The shared run-time pool is 262,144 seconds, so one request of up to about a day is almost always accepted, and a second such request is then declined. The server waits up to 15 seconds for other work to finish before denying the request. A denial for this resource mismatch is HTTP 504. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

The wiki’s resource-management example raises the timeout from 3 minutes to 900 seconds and sets the memory soft quota to 1 GiB (`1073741824` bytes). It warns that the example retrieves more than 100 MiB, that a browser tab may crash if the result is drawn on the map, and that such results should be downloaded for other tools. It also says these limits cannot be set arbitrarily high: an instance may refuse to extend them, or the query may fail. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

The operator lists stitching bounding boxes to scrape the whole world as problematic behaviour, in the same class as asking for elements one by one millions of times. For both, the manual says to use a planet dump instead of the Overpass API. Tens of thousands of identical requests per day from one address are a script bug. An app for more than OSM mappers that uses the public instances as its backend is told to run its own instance. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

The wiki’s limitations section says the result size is known only when the download finishes, so there is no ETA, and that dynamically generated Overpass files typically take longer to generate and download than an existing static extract of the same region. For a country-sized region with all or nearly all of the data, it says to use planet.osm mirrors. Overpass is most useful when the need is a selection of the data in the region, not the region itself. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

The documented output mode `out geom` returns way geometry inline. The wiki’s developer quick start uses `[out:json]`, `[timeout:90]`, and `out geom`, and the sample element includes a `geometry` array of latitude/longitude pairs plus a `bounds` object. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

### Other public instances named on the wiki

VK Maps (`https://maps.mail.ru/osm/tools/overpass/api/interpreter`) states that there are currently no request limitations. Private.coffee (`https://overpass.private.coffee/api/interpreter`), previously known as `overpass.kumi.systems`, states that there is no rate limit, and asks to be notified in advance of large-scale use. Paid or keyed instances listed alongside them include Geofabrik, FairwayMapper, Tracestrack, Overspan, and NextGIS. The wiki table of global instances, as read on this date, does not list `overpass.openstreetmap.fr`. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))

The editing API’s own policy is a different document. It says large or frequent data users must use planet.osm or the other alternatives it lists, and it points at Overpass as a separate read-only API with limitations and policies of its own. ([API Usage policy](https://operations.osmfoundation.org/policies/api/))

## 2. Bulk acquisition

### Geofabrik extracts

Geofabrik’s free download server publishes OpenStreetMap extracts that are normally updated every day. The public files omit user names, user IDs, and changeset IDs; extracts with full metadata are for OpenStreetMap contributors only. ([Geofabrik download server](https://download.geofabrik.de/))

The commonly used file is `.osm.pbf`, described as suitable for Osmium, Osmosis, imposm, osm2pgsql, mkgmap, and others. Region pages also offer a `.poly` extent, `.osc.gz` change files for updates, and, for many smaller regions, `.gpkg.zip`. A history file with personal data is on the internal server only. ([North America](https://download.geofabrik.de/north-america.html), [United States](https://download.geofabrik.de/north-america/us.html))

Continent PBF sizes on the download index: Africa 7.4 GB, Antarctica 31.6 MB, Asia 15.2 GB, Australia and Oceania 1.5 GB, Central America 753 MB, Europe 32.6 GB, North America 18.1 GB, South America 3.8 GB. ([Geofabrik download server](https://download.geofabrik.de/))

The North America page’s current file is `north-america-latest.osm.pbf`, 18.1 GB, last modified about a day before this note, containing data up to 2026-09-23T20:22:04Z. ([North America](https://download.geofabrik.de/north-america.html))

The United States file is `us-latest.osm.pbf`, 11.3 GB, with the same data timestamp. State extracts on that page include California 1.2 GB, Texas 688 MB, Tennessee 181 MB, Kentucky 146 MB, and the District of Columbia 20.0 MB. ([United States](https://download.geofabrik.de/north-america/us.html))

Germany is `germany-latest.osm.pbf`, 4.5 GB, data up to the same 2026-09-23T20:22:04Z timestamp. Berlin on that page is 94 MB. ([Germany](https://download.geofabrik.de/europe/germany.html))

The planet wiki’s extract table describes Geofabrik as daily PBF extracts for continents, most countries, and sub-country regions for Brazil, Canada, France, Germany, Italy, Japan, Poland, Russia, the UK, and the US, with version and timestamp only. The same table lists other extract services, including daily city and metropolitan extracts from Interline and weekly city extracts from BBBike. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

### planet.osm and OSMF download practice

Planet.osm is the whole OpenStreetMap dataset in one file: all nodes, ways, and relations. A new version is released every week. On 2026-09-01 the wiki recorded the plain OSM XML variant as over 2273.7 GB uncompressed, from a 165.0 GB bzip2 file or an 88.0 GB PBF file. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

The planet server’s own page, read the day before this note, listed the latest weekly planet XML file at 166 GB and the latest weekly planet PBF file at 88 GB, each created a few hours earlier. It says each week a complete copy is published as compressed XML and as PBF, and that a history file also exists with older versions and deleted items. Files after 12 September 2012 are Open Database License 1.0; earlier files are CC BY-SA 2.0. ([Planet OSM](https://planet.openstreetmap.org/))

PBF is described as more compact and faster to process, and as the format to use whenever possible. The planet is also published as history, notes, changesets, and changesets with discussions. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

The weekly dump normally starts around 01:10 UK time on Monday and is guaranteed to contain all updates before that time. The result is usually ready on Fridays. The wiki says the dump is built so that it has referential integrity, and that this does not always apply to extracts. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

Minute diffs are produced with osmdbt. Hourly and daily diffs are Osmosis combinations of minute diffs. A daily diff is generally about 40–80 MB compressed, and can grow to 300–400 MB compressed in exceptional cases. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

The wiki says not to download the planet in a web browser, and shows `curl -OL https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf`. The same page documents anonymous AWS CLI downloads from `s3://osm-planet-eu-central-1/` and `s3://osm-planet-us-west-2/`, plus BitTorrent. It says BitTorrent does not save OSM server bandwidth, because the S3 buckets have unlimited bandwidth. It also says mirrors used to be required to conserve OSM bandwidth and that this is no longer necessary; if a mirror is used, HTTPS is preferred. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

OSMF Operations reported in September 2023 that planet serving had moved to S3 under the AWS Open Data program, that existing URLs redirect to S3, and that this was done to reduce the planet site’s hardware. ([September 2023](https://operations.osmfoundation.org/2023/09/30/september.html))

The OSMF editing-API policy says large or frequent data users must use planet.osm, and its technical requirements for that API include a valid User-Agent identifying the application and version, a valid Referer when known, and a maximum of two download threads. Heavy users are asked to run their own data server, usually from planet.osm, or to use another provider. The planet homepage adds that the data is free to use and is not free to make or host, and it publishes suggested annual donations by revenue band. ([API Usage policy](https://operations.osmfoundation.org/policies/api/), [Planet OSM](https://planet.openstreetmap.org/))

The planet file is ODbL, the same licence as the master database. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

### osmium and pyosmium

Osmium tool reads and writes XML, PBF, O5M/O5C, OPL, and a debug format. Text formats that use gzip or bzip2 are decompressed automatically. `osmium cat` converts between them. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

`osmium extract` cuts a geographic extract so later work uses much less data than the planet. A bounding box example from the manual is `osmium extract -b 2.25,48.81,2.42,48.91 france.pbf -o paris.pbf`. The same command accepts a GeoJSON polygon, an Osmosis `.poly` file, or an OSM file that contains the boundary polygon. A config file can cut many regions in one pass. Bounding-box cuts are the fastest; polygon cost depends on how detailed the polygon is. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

Three strategies change which objects are kept, how much memory is used, and how many times the input is read. `simple` keeps nodes inside the region and any way with at least one node inside, reads the file once, and does not produce reference-complete ways or relations. `complete_ways` also keeps nodes of ways that cross the boundary, so output ways are reference-complete, and reads the file twice. `smart` does that and also completes multipolygon relations that touch the region (by default only `type=multipolygon`), and reads the file three times. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

Cutting all 101 French départements at once is described as likely to run out of memory, because Osmium tracks the node, way, and relation IDs needed for each extract. The manual’s figure is 1 to 2 GB of RAM per extract, depending on strategy. The documented remedy is to extract in rounds: large regions first, then smaller areas from those. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

`osmium tags-filter` keeps objects that match tag expressions such as `w/highway` or `w/highway=primary`, and by default also writes the objects they reference. Without `--omit-referenced` / `-R` the input may be read up to three times. With `-R` it is read once. The man page says the command does its work on the fly and only keeps tables of object IDs in memory, and that `-R` keeps no IDs in memory. ([osmium-tags-filter(1)](https://docs.osmcode.org/osmium/latest/osmium-tags-filter.html))

A full planet dump is reference-complete: every member a relation or way points at is in the file. A geographic extract is not always reference-complete. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

pyosmium is a Python library on libosmium for pipelines that can handle planet-sized data. It processes a stream: one object at a time from a file or other source. It cannot jump to an arbitrary object; the documented techniques are caching and reading the file again. A `FileProcessor` loop yields a read-only view that is invalid after the next object, and retaining the view raises `RuntimeError: Illegal access to removed OSM object`. Data needed later has to be copied out of the loop. OSM files are usually ordered nodes, then ways, then relations, each by ID, so backward references have already been seen when an object appears. Nested relations are the documented exception and can force another scan or a large in-memory set. ([pyosmium introduction](https://docs.osmcode.org/pyosmium/latest/), [First steps](https://docs.osmcode.org/pyosmium/latest/user_manual/01-First-Steps/))

Way coordinates are not stored on the way. `with_locations()` records node coordinates into a location store and attaches them when the way is read. `with_areas()` scans the file twice. Location stores include a file-backed array; the reference example is `dense_file_array,foo.store`. That index maps a node ID to a location. Libosmium describes this class of index as storage of small values keyed by a positive integer, in memory or on disk, scaling to billions of objects, and says a planet-sized node-location index of this kind needs a 64-bit address space. ([Working with geometries](https://docs.osmcode.org/pyosmium/latest/user_manual/03-Working-with-Geometries/), [Indexes](https://docs.osmcode.org/pyosmium/latest/reference/Indexes/), [libosmium Map](https://docs.osmcode.org/libosmium/latest/classosmium_1_1index_1_1map_1_1Map.html))

### PBF, XML, and Overpass JSON

The PBF specification says the format is about half the size of a gzipped planet, about 30% smaller than a bzipped planet, about 5 times faster to write than a gzipped planet, and about 6 times faster to read than a gzipped planet. ([PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format))

Using the 2026-09-01 planet wiki figures, the 88.0 GB PBF is the compressed form of the same dump whose uncompressed XML is over 2273.7 GB and whose bzip2 form is 165.0 GB. ([Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm))

A PBF file is a sequence of fileblocks. Each data block is a `PrimitiveBlock` that can be decompressed on its own: it carries its own string table and its own coordinate granularity. After serialization, each block is optionally zlib-compressed. Readers and writers must support uncompressed and zlib-compressed blobs. The uncompressed blob should be under 16 MiB and must be under 32 MiB. The `BlobHeader.indexdata` field is documented as an arbitrary blob that might hold a bounding box, and as a stub for a future indexed `*.osm.pbf`. Dense nodes delta-encode ids and coordinates. Ways delta-encode node-id references. ([PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format))

OSM XML is the other planet encoding. The planet page publishes it as the large compressed XML file (166 GB on the day it was read) beside the 88 GB PBF. ([Planet OSM](https://planet.openstreetmap.org/))

Overpass JSON is a query response, not a planet dump. The wiki quick start requests `[out:json]` and `out geom`, and the returned way includes both a node-id list and a `geometry` array. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API)) No figure for Overpass JSON versus PBF was found in these sources, so none is stated here.

`osmium sort` is the documented counterexample to streaming: it reads the input into main memory, roughly 10 times the on-disk size of a `.osm.bz2` or `.osm.pbf`, and the manual limits it to smaller files. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

### When a private Overpass instance fits

The operator’s stated goal is to make a private instance simple, and the manual sends anyone above the public safety margin to the installation guide. A private instance is how the manual says you execute an arbitrary number of requests. The guide assumes a server with 500 GB to 1 TB of disk. Worldwide current geodata alone were 350 GB to 400 GB as of 2023; with attic data, 800 GB to 1 TB. A clone from the published snapshot transfers about 50% to 70% of that, because temporary transaction data and the area cache are not copied. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html), [Installation of an own instance](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html))

A local extract can be imported from OSM XML with `update_database` instead of cloning the planet. Runtime is described as minutes to 24 hours depending on file size. Minute diffs then come from `https://planet.openstreetmap.org/replication/minute/`. ([Installation of an own instance](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html))

The cases the manual assigns to a private instance are sustained demand above the public quota, and an application that is not only for OSM mappers. The cases it assigns to a planet dump are element-by-element scraping and bbox stitching. ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html))

### Overture buildings GeoParquet and DuckDB

Overture publishes releases on Amazon S3 and Microsoft Azure. The documented ways to read them are the Overture Python CLI, DuckDB SQL against GeoParquet, and several warehouse mirrors. ([Overture documentation](https://docs.overturemaps.org/))

The buildings theme is human-made structures with a roof or interior space. Feature types are `building` (outer footprint, or roofprint if traced from imagery) and `building_part`. OpenStreetMap is the primary source and has the highest conflation priority. Other inputs include Esri Community Maps, selected national and city datasets, and ML roofprints from Microsoft, Google Open Buildings, and a dataset covering East Asian countries. `building_part` comes only from OpenStreetMap. Because OSM is included, the theme is published under the ODbL. ([Buildings guide](https://docs.overturemaps.org/guides/buildings/))

Matching uses intersection-over-union, and buildings match above an IoU of 0.5. ML-derived footprints are required to have an area greater than 10 m before they enter conflation. Features with a height of 900 m or more are excluded. The theme is global, released monthly, and many footprints in the Global South are ML-derived and of lower precision. ([Buildings guide](https://docs.overturemaps.org/guides/buildings/))

The buildings release path in the guide is `s3://overturemaps-us-west-2/release/2026-09-23.0/theme=buildings/type=building/*`, with a matching Azure URL. A STAC catalog at `https://stac.overturemaps.org/catalog.json` exposes the latest release so paths do not have to be hardcoded. ([Buildings guide](https://docs.overturemaps.org/guides/buildings/), [Quickstart](https://docs.overturemaps.org/getting-data/))

The CLI example downloads one bbox of buildings to GeoJSON. The quickstart says the tool reads cloud GeoParquet and transfers only the data inside the bounding box. The DuckDB example loads `spatial` and `httpfs`, sets `s3_region` to `us-west-2`, reads a `read_parquet` hive path, and filters with `bbox.xmin` and `bbox.ymin`. ([Quickstart](https://docs.overturemaps.org/getting-data/))

KnoxMap already follows that pattern in `KnoxMap/generator/overture.py`. It pins release `2026-08-19.0` (override `KNOXMAP_OVERTURE_RELEASE`), opens DuckDB, loads `httpfs` and `spatial`, sets `s3_region` to `us-west-2`, and queries `s3://overturemaps-us-west-2/release/<release>/theme=buildings/type=building/*` with `bbox.xmin`, `bbox.xmax`, `bbox.ymin`, and `bbox.ymax`. Rows are cached as gzip JSON beside the map. The module comment says there is no HTTP API for a bounding box, so DuckDB is required, and that one town query spends most of its time on DuckDB reading theme metadata. ([KnoxMap/generator/overture.py](../KnoxMap/generator/overture.py))

## 3. Memory and storage patterns

pyosmium’s model is a stream of one OSM object at a time. The object view is not a value you can store. Anything kept past the callback has to be an explicit copy, and copying only the id is the documented way to stay small. ([First steps](https://docs.osmcode.org/pyosmium/latest/user_manual/01-First-Steps/))

`osmium tags-filter` is the same idea at the command line: work on the fly, retain ID tables only, or retain nothing if referenced objects are omitted. It may reread the file to complete references. ([osmium-tags-filter(1)](https://docs.osmcode.org/osmium/latest/osmium-tags-filter.html))

PBF supports that streaming layout. Each primitive block decompresses independently and is capped at 32 MiB uncompressed, so a reader does not need the whole file inflated at once. The header slot that could have carried a per-blob bounding box is specified as a stub for a future index, not as a working spatial index. ([PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format))

The indexes Osmium and pyosmium document are maps from node ID to coordinate, including file-backed maps such as `dense_file_array`. They are how a second pass resolves way geometry without holding every node object. They are not a spatial index that seeks to a cell’s bbox. A planet-scale in-memory form of this ID map is documented as a 64-bit concern. ([Indexes](https://docs.osmcode.org/pyosmium/latest/reference/Indexes/), [libosmium Map](https://docs.osmcode.org/libosmium/latest/classosmium_1_1index_1_1map_1_1Map.html))

The documented way to make a later step read only a bbox is to materialize that bbox with `osmium extract`, then process the smaller file. `simple` does it in one read and allows a pipe. `complete_ways` and `smart` reread the parent two or three times so the output is reference-complete. Many extracts in one invocation cost about 1–2 GB of RAM each, because every extract keeps an ID set. The manual’s large-area pattern is cascaded extracts: cut regions, then cut smaller pieces from those, so no single process tracks every cell. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

Commands that load the whole file are called out separately. `osmium sort` holds roughly 10 times the PBF or bzip2 size in RAM. `apply-changes` reads every change file into memory before applying it, and the manual says to apply large change sets in bunches. ([Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))

Overture’s equivalent of “read this bbox” is a column filter on GeoParquet `bbox` fields through DuckDB or the official CLI, which the quickstart says transfers only the data inside the box. ([Quickstart](https://docs.overturemaps.org/getting-data/))

Compression figures that these sources actually publish are in the next section. They compare PBF with gzipped and bzipped OSM XML. They do not publish a ratio of PBF to Overpass JSON or to gzip JSON.

## 4. Concrete numbers

| Figure | Value | Source |
| --- | --- | --- |
| One-off public Overpass ceiling | under 10,000 queries/day and under 1 GB/day | [wiki](https://wiki.openstreetmap.org/wiki/Overpass_API), [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Regular automated use of the main instance | under 100 queries/day and under 10 MB/day | [wiki](https://wiki.openstreetmap.org/wiki/Overpass_API) |
| Requests a server can fulfill | about 1 million per day, per server; two servers on overpass-api.de | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Daily users sharing that capacity | about 30,000 | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Default query timeout / maxsize | 180 seconds / 512 MiB | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Server memory admission cap | 12 GiB; a request may use at most half of what is still free | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Shared run-time pool | 262,144 seconds | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Slot queue before discard | 15 seconds; denial is HTTP 429 | [commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html) |
| Pause after HTTP 429 or 406 | 30 seconds | [wiki](https://wiki.openstreetmap.org/wiki/Overpass_API) |
| Overpass design target | up to roughly 10 million elements in some minutes | [wiki](https://wiki.openstreetmap.org/wiki/Overpass_API) |
| Planet PBF, live server | 88 GB | [planet.openstreetmap.org](https://planet.openstreetmap.org/) |
| Planet bzip2 XML, live server | 166 GB | [planet.openstreetmap.org](https://planet.openstreetmap.org/) |
| Planet on 2026-09-01 | 88.0 GB PBF, 165.0 GB bzip2, over 2273.7 GB uncompressed XML | [Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm) |
| PBF versus other planet encodings | about half a gzipped planet, about 30% smaller than bzip2, about 5× faster to write and 6× faster to read than gzip | [PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format) |
| PBF block size | uncompressed blob should be under 16 MiB and must be under 32 MiB | [PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format) |
| Daily planet diff | generally 40–80 MB compressed, up to 300–400 MB compressed | [Planet.osm](https://wiki.openstreetmap.org/wiki/Planet.osm) |
| Geofabrik continent PBFs | Europe 32.6 GB, North America 18.1 GB, Asia 15.2 GB, Africa 7.4 GB, South America 3.8 GB, Australia and Oceania 1.5 GB, Central America 753 MB, Antarctica 31.6 MB | [download.geofabrik.de](https://download.geofabrik.de/) |
| Geofabrik country and city examples | United States 11.3 GB, Germany 4.5 GB, California 1.2 GB, Texas 688 MB, Tennessee 181 MB, Kentucky 146 MB, Berlin 94 MB, District of Columbia 20.0 MB | [US](https://download.geofabrik.de/north-america/us.html), [Germany](https://download.geofabrik.de/europe/germany.html) |
| Private Overpass disk | 500 GB–1 TB recommended; global current data 350–400 GB as of 2023; with attic, 800 GB–1 TB | [setup](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html) |
| `osmium extract` memory | 1–2 GB RAM per extract in a multi-extract run | [Osmium tool manual](https://osmcode.org/osmium-tool/manual.html) |
| `osmium sort` memory | roughly 10× the on-disk `.osm.pbf` or `.osm.bz2` size | [Osmium tool manual](https://osmcode.org/osmium-tool/manual.html) |
| Overture building match and ML size | IoU above 0.5; ML footprints kept only above 10 m area | [Buildings guide](https://docs.overturemaps.org/guides/buildings/) |

## Implications for KnoxMap

- Generate follows that advice. `KnoxMap/generator/geofabrik.py` picks the smallest daily Geofabrik regions that cover the box, and `KnoxMap/generator/localosm.py` keeps the PBF and cuts the clip in-process with pyosmium. Overpass remains the named-places list (`KnoxMap/generator/places.py`), not the terrain download. A city-sized selection is then split into mods of 25 cells that meet on a cell edge (`KnoxMap/knoxbuild/modgrid.py`). ([Commons](https://dev.overpass-api.de/overpass-doc/en/preface/commons.html), [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))
- The main instance’s published one-off ceiling is 10,000 queries and 1 GB per day, and regular use is a hundredth of that. The wiki also forbids parallel scripts on that instance, asks for a 30-second pause after HTTP 429 or 406, and says the server is overloaded. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))
- `overpass.kumi.systems` is the instance the wiki now lists as Private.coffee, with no published rate limit and a request to be told before large-scale use. `overpass.openstreetmap.fr` is not in the current global-instance table. ([Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))
- For an area the size of a US state or a European country, Geofabrik already publishes a daily PBF in the hundreds of megabytes to a few gigabytes (Tennessee 181 MB, Germany 4.5 GB, the United States 11.3 GB). That file is the input `osmium extract` and `osmium tags-filter` are documented to cut from. ([United States](https://download.geofabrik.de/north-america/us.html), [Germany](https://download.geofabrik.de/europe/germany.html), [Osmium tool manual](https://osmcode.org/osmium-tool/manual.html))
- A cell pipeline that must not hold the map keeps either a stream (`tags-filter`, pyosmium `FileProcessor`) or a cascade of extracts. One multi-extract process is documented at 1–2 GB RAM per cell, so a metro grid belongs in rounds, not in one ID table. ([osmium-tags-filter(1)](https://docs.osmcode.org/osmium/latest/osmium-tags-filter.html), [Osmium tool manual](https://osmcode.org/osmium-tool/manual.html), [First steps](https://docs.osmcode.org/pyosmium/latest/user_manual/01-First-Steps/))
- There is no working PBF spatial index in the specification to seek a cell bbox. The ID-to-coordinate store (`dense_file_array` and the libosmium map) is for assembling way geometry, on disk when the node table is large. ([PBF Format](https://wiki.openstreetmap.org/wiki/PBF_Format), [Indexes](https://docs.osmcode.org/pyosmium/latest/reference/Indexes/))
- A private Overpass instance is the documented answer when the product itself must serve arbitrary queries. The disk figure for a global database is hundreds of gigabytes. A one-shot map compile is the case the wiki sends to a regional dump plus osmium. ([Installation of an own instance](https://dev.overpass-api.de/overpass-doc/en/more_info/setup.html), [Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API))
- Overture remains a monthly ODbL buildings layer, OSM first and ML roofprints second, already queried in this repo through DuckDB and a bbox on GeoParquet when **Add missing buildings** is on. Roads, landuse, and water stay OSM, from the Geofabrik extract. The official CLI’s bbox download is the same access path, aimed at one box rather than a live Overpass tile grid. ([Buildings guide](https://docs.overturemaps.org/guides/buildings/), [Quickstart](https://docs.overturemaps.org/getting-data/), [KnoxMap/generator/overture.py](../KnoxMap/generator/overture.py))
