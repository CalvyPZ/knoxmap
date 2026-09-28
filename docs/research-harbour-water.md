# Why Sydney Harbour is missing from a coastline-and-lakes water fill

Date: 2026-09-27

Notes from primary sources only: the OpenStreetMap wiki pages for coastline, `natural=water`, `natural=coastline`, `natural=bay`, `water=harbour`, multipolygon relations, and `waterway=riverbank`, plus the live objects read from the OSM API and the Overpass API on this date. Download and query failures are named in the last section. Project Zomboid biome indexes are mentioned only where they explain the forest colour of unpainted ground.

The question is why a generator that paints water from `natural=water`, `landuse=reservoir` or `basin`, and `waterway=river`, `riverbank`, `canal`, or `stream`, and that fills the sea from `natural=coastline` ways (land on the left, water on the right), would leave Sydney Harbour as land, and in this project as forest.

## 1. What the wiki assigns to the sea

`natural=water` is an area tag for inland water: a lake, a pond, a moat, a canal. It is drawn as a closed way, or as a multipolygon when the area has islands or is too large for one way. The same page lists what is not inland water. The sea, including tidal channels, bays, lagoons, and estuaries, is `natural=coastline`. ([Tag:natural=water](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dwater))

`natural=coastline` marks mean high water springs. The way is drawn so that the land is on the left and the water is on the right. The tag is for ways. Nodes and relations are not tagged `natural=coastline`. It is not used for lakes and rivers. The only inland water body the page allows to use it is the Caspian Sea. ([Tag:natural=coastline](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dcoastline))

The same page states the rule for harbours that open onto the ocean: bays and other large tidal salt-water areas are part of the sea when they are connected to the sea, and they should be tagged as the sea. A wide river meets that sea on a `natural=coastline` way across the river mouth, and the river’s own area is `natural=water` plus `water=river`. ([Tag:natural=coastline](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dcoastline))

The coastline is a chain of ways joined end to end, not a set of closed water polygons. For a large landmass the chain is not one closed way inside a city extract. A break, a reversed way, or a self-crossing can make the whole landmass fail to render, because one bad section can flood a continent. The Coastline page says renderers usually need closed polygons, and that an imperfect chain breaks the polygon. Standard tiles therefore use processed land polygons from osmdata.openstreetmap.de, not the raw ways on each tile. The page also says not to build a large multipolygon out of coastline segments for a large bay, a peninsula, or a continent: every split of a member way creates a new version of that relation. ([Coastline](https://wiki.openstreetmap.org/wiki/Coastline))

`waterway=riverbank` is not a harbour tag. It is deprecated. The page says to use `natural=water` plus `water=river`, and that the old tag had fallen below 1,400 uses by late 2022. ([Tag:waterway=riverbank](https://wiki.openstreetmap.org/wiki/Tag:waterway%3Driverbank))

## 2. Names are not the water surface

`natural=bay` names an inlet that is mostly surrounded by land and still level with the ocean or a lake. The page says the tag does not mark the presence of water. Presence of water is `natural=water` or `natural=coastline`. A bay area may include land, including islands mapped as inner ways. OSM Carto draws a label, and the page says bays should not be drawn as a solid water colour, because they are already part of a lake or of the ocean. Bays are usually nodes. A node sits near the middle of the bay. ([Tag:natural=bay](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dbay))

The edge of a bay toward the land should coincide with the coastline. The page says the coastline should not be closed across the outer side of a bay, because that water is part of the ocean or lake it joins. Large bay multipolygons are discouraged for the same reason as sea multipolygons: they are hard to keep closed. Fjords may be linear ways. ([Tag:natural=bay](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dbay))

`place=sea` is the tag for a sea, a gulf, or a very large bay such as the Bay of Biscay or the Bay of Bengal. The usual geometry is a node. Area multipolygons for seas are described as controversial, because the boundary with the open ocean is often not verifiable and the relation breaks whenever the coastline is split. The page points `natural=bay` at ordinary bays and says the line between a large bay and a sea is not always clear. ([Tag:place=sea](https://wiki.openstreetmap.org/wiki/Tag:place%3Dsea))

`water=*` is a refinement of inland `natural=water`. The key page says `water=bay` and `water=cove` are not the established tags; bays, coves, and inlets use `natural=bay`. `water=harbour` is a listed value: a sheltered body where ships can dock, drawn as an area with `natural=water`. ([Key:water](https://wiki.openstreetmap.org/wiki/Key:water), [Tag:water=harbour](https://wiki.openstreetmap.org/wiki/Tag:water%3Dharbour))

The Harbour page uses a different split. A harbour can be a node or an area with `harbour=yes`. Where a way encloses both water and land (moles, quays, wharves), `landuse=harbour` is also used. The water inside that area, when it is not already part of `natural=coastline` or `natural=water`, is `waterway=dock`. ([Harbour](https://wiki.openstreetmap.org/wiki/Harbour))

`waterway=flowline` is a centreline through a lake, reservoir, or other large water body, so the river network stays connected. It is a way, not an area. It does not replace the water polygon. ([Tag:waterway=flowline](https://wiki.openstreetmap.org/wiki/Tag:waterway%3Dflowline))

A multipolygon relation has way members in the role `outer` (the outline) and `inner` (the holes). Those ways have to form valid rings. On tag pages, a multipolygon is treated as an area: the same tags apply to a closed way and to the relation. The JOSM note on that page says that once the ways are members, the area tags belong on the relation and should be removed from the outer ways. ([Relation:multipolygon](https://wiki.openstreetmap.org/wiki/Relation:multipolygon))

Read together, the pages describe two geometries that both occur on the live map:

- A bay that is open to the sea is the sea. The shore is `natural=coastline`, water on the right. `natural=bay` only names it. The coastline is not closed across the mouth.
- A harbour basin drawn as water is an area: `natural=water` and `water=harbour`. That is the inland-water tagging, also used when mappers have chosen to polygon a sea inlet anyway.

## 3. Port Jackson on the live map

Nominatim’s first hit for “Port Jackson” is relation 15522136. Its first hits for “Sydney Harbour” are ways, not that relation. ([Nominatim, Port Jackson](https://nominatim.openstreetmap.org/search?q=Port%20Jackson&format=jsonv2), [Nominatim, Sydney Harbour](https://nominatim.openstreetmap.org/search?q=Sydney%20Harbour&format=jsonv2))

Relation 15522136, version 30, last edited 2026-08-21T23:31:08Z in changeset 187820565, is tagged:

- `type=multipolygon`
- `natural=water`
- `water=harbour`
- `name=Port Jackson`
- `intermittent=no`
- `wikidata=Q54504`
- `wikipedia=en:Port Jackson`

It has 484 members with role `outer` and 35 with role `inner`. Every member is a way. There is no untagged role. ([relation/15522136](https://www.openstreetmap.org/relation/15522136), [OSM API](https://api.openstreetmap.org/api/0.6/relation/15522136.json))

The outer ring closes as a chain, and only as a chain. None of the 484 outer ways is a closed way by itself (first node equal to last node). The graph of their endpoints has one component, and every endpoint node has degree 2, so the ways form one cycle when reversals are allowed. That is the OSM closed ring. This check used node ids from the OSM API. It does not prove the polygon is free of self-intersections.

The water tags sit on the relation. Of the 484 outer ways, 291 have no tags. Most of the rest carry only a source tag (`source=Bing`, `source=ABS2011-data.gov.au`, `source:location=nearmap`, and similar). Two outer ways are `natural=coastline`: way 576802264 and way 899661018. Three ways are tagged `source=PSMA Admin boundary - not a water , building nor road!` (ways 4615456, 1082437532, and 1326239978). Their nodes sit on the inner harbour shore, around −33.869, 151.221, a few tens of metres apart, so they are short boundary pieces in the same ring, not a separate outline of the city. An untagged outer, way 4614011, starts at node 13886971, −33.8726054, 151.2346802, on the south shore west of the Heads. ([way/576802264](https://www.openstreetmap.org/way/576802264), [way/4615456](https://www.openstreetmap.org/way/4615456), [node/13886971](https://www.openstreetmap.org/node/13886971))

The 35 inner ways, read with Overpass `out tags` against database timestamp 2026-09-26T23:53:06Z, are holes: untagged rings, `natural=wetland`, `natural=bare_rock`, `place=island` (Snapper Island, way 172947863), `place=islet` (Goat Island, way 397373570), and `man_made=pier` with `area=yes`. None of those inner ways is tagged `natural=coastline` or `natural=water`.

There is no closed way named Port Jackson or Sydney Harbour with `natural=water` in the box (−33.90, 151.05, −33.70, 151.35). The Overpass query for that way returned no ways. The area is the relation.

The name “Sydney Harbour” on the objects Nominatim returns is `waterway=flowline`. Way 1238283940, version 4, is tagged `name=Sydney Harbour`, `name:en=Sydney Harbour`, and `waterway=flowline`, with no `natural` tag. That is a centreline through the water body, which is what the flowline page describes. ([way/1238283940](https://www.openstreetmap.org/way/1238283940))

Smaller inlets inside the harbour are `natural=bay` nodes. In the same box, Overpass returned nodes such as Darling Harbour (node 25843272), Farm Cove (node 911561237), Blackwattle Bay, and many others, each with `natural=bay`. No relation in the Nominatim results for these two names is `natural=bay`. ([node/25843272](https://www.openstreetmap.org/node/25843272))

## 4. Where the coastline actually is

An Overpass query for `way["natural"="coastline"]` in (−33.870, 151.160, −33.820, 151.300), database timestamp 2026-09-26T23:56:06Z, returned 11 ways. Every one of them has `minlon` of at least 151.280897. The inner harbour, including the shore node at longitude 151.235 and the Circular Quay shore near 151.221, is outside those ways.

The two coastline ways that are also outer members of relation 15522136 are the seaward end of that set:

- Way 576802264 has two nodes, from −33.8330562, 151.2808972 to −33.8329710, 151.2810720. Node 13818465 on that way is tagged `source=PGS`.
- Way 899661018 continues from that second node through −33.8275770, 151.2925281 to −33.8239307, 151.3002717.

The other nine coastline ways in the box lie on the same eastern edge, between about −33.883 and −33.833, longitudes 151.281 to 151.288. They meet the two member ways at the Heads. Nothing in this set turns west into Port Jackson.

A fill that paints water on the right of those ways paints the ocean east of the Heads. The harbour west of longitude 151.281 is on the land side of the coastline that is in the database. The wiki’s own rule says a sea-connected bay should be inside the coastline, and that the coastline should not be closed across the outer side of a bay. The published coastline for this harbour stops at the Heads. Coastline alone, using the ways that exist, does not fill Sydney Harbour.

## 5. San Francisco Bay, Tokyo Bay, Halifax Harbour

These three are named as bays, and the samples that were fetched reuse `natural=coastline` as the outline. Member roles come from the OSM API. A follow-up Overpass query that would have counted every coastline-tagged outer returned HTTP 504, so the Tokyo and Halifax tag checks are samples.

San Francisco Bay is relation 9451753, version 17, 2026-02-10T17:15:25Z. Tags include `natural=bay`, `type=multipolygon`, `name=San Francisco Bay`, `wikidata=Q232264`. It has 11 outer ways and 3 inner ways. All 11 outers were fetched. Seven are `natural=coastline` (ways 157393811, 355965060, 355965063, 1296528412, 1296528420, 1296528422, 1296528424). Four have no tags (ways 219537894, 668082533, 761377350, 1296528410), including a two-node way. None is `natural=water`. ([relation/9451753](https://www.openstreetmap.org/relation/9451753))

Tokyo Bay is relation 13904308. Tags include `name=東京湾`, `name:en=Tokyo Bay`, `natural=bay`, `type=multipolygon`, `wikidata=Q141017`. It has 570 outer ways and 31 inner ways. The first eight outer ways in the member list (130930637, 130930664, 130930668, 130930828, 130930106, 130930639, 130930086, 130931275) are all `natural=coastline`. The other 562 outers were not tagged in this pass. ([relation/13904308](https://www.openstreetmap.org/relation/13904308))

Halifax Harbour is relation 13440670, version 42, 2026-08-10T23:08:41Z. Tags are `name=Halifax Harbour`, `natural=bay`, `ocean=yes`, `type=multipolygon`, `wikidata=Q1567789`. It has 184 outer ways and 6 inner ways. Twelve member ways were fetched (ten outers and two inners). All twelve are `natural=coastline`. The two inners in that sample are closed ways and also `place=islet` on way 62641948. The other 178 outers were not tagged in this pass. ([relation/13440670](https://www.openstreetmap.org/relation/13440670))

On these three, the wiki’s split is what is mapped: `natural=bay` names the inlet, and the shore lines that were sampled are `natural=coastline`. Filling the bay relation as a solid water colour is what the bay page says not to do. Filling water on the right of the coastline is what covers the bay, provided the coastline follows the shore instead of stopping at the mouth.

Port Jackson is the other pattern. The name of the water surface is `natural=water` and `water=harbour` on relation 15522136. The shore west of the Heads is not `natural=coastline`. A renderer that only understands the San Francisco pattern will leave this harbour dry. A renderer that only understands the Port Jackson pattern will leave a coastline bay dry, and will also paint a `natural=bay` multipolygon as if it were a lake, including any land the bay page allows inside that area.

## 6. What a renderer can trust

The water surface of Sydney Harbour, as mapped on this date, is relation 15522136. The signal is `natural=water` on that multipolygon, with `water=harbour` saying what kind of inland-style water the mappers used. The outer ways have to be chained. They are open, and they are mostly untagged, which is what the multipolygon page asks for after the tags move to the relation. Expecting `natural=water` on each shore way misses the harbour. Expecting each member to already be a closed ring misses it too.

`natural=coastline` is the signal for the open sea, and for bays whose shore is still coastline (the San Francisco, Tokyo, and Halifax samples). It is not, on the current Sydney data, a signal for the water west of the Heads. `natural=bay` nodes and relations name inlets and are not a fill. `waterway=flowline` is the named centreline of Sydney Harbour and is not a fill. `waterway=riverbank` is the deprecated river-area tag and does not appear on this harbour.

A query for `landuse=forest` and `natural=wood` in a small box of open water (−33.855, 151.220, −33.845, 151.245) returned HTTP 504, so this note does not say whether a forest polygon is drawn across the harbour. With no water polygon assembled, the ground is simply unmapped.

## Implications for KnoxMap

`KnoxMap/generator/osm.py` classifies `natural=water` and `waterway=river|riverbank|canal|stream` as water, and `natural=coastline` as coastline. `landuse=forest` and `natural=wood` are forest. `assemble_rings` chains outer ways. Painting each harbour way on its own had turned this harbour into scattered slivers, about 6% water.

`KnoxMap/generator/localosm.py` used to build a multipolygon only from member ways that were already closed rings, and the way gate dropped anything that was neither a paint tag nor a closed ring. The Port Jackson outers are open, and 291 of 484 have no tags, so that path stored nothing but the two short `natural=coastline` pieces at the Heads. The relation then had no outer ring, and the harbour stayed the temperate open biome, farm-mix forest (`KnoxMap/generator/biomes.py`, absolute latitude at least 32).

The reader now collects way ids of `natural=water` multipolygons before the main pass, keeps every member needed to form the relation, and stitches them with `assemble_rings`. It paints water only when every member is present and the outer closes. It does not infer the water side from an individual member way: multipolygon members may point either direction, so that inference can flood land. Completed relations that do not meet the selected map are discarded. `natural=bay` and `waterway=flowline` are still not a fill.

`KnoxMap/generator/renderer.py` still builds the sea from `natural=coastline` only, water on the right of each way. For this harbour that paints the ocean east of longitude 151.281. The water west of the Heads comes from relation 15522136, not from that sea fill. Coastline bays such as the San Francisco, Tokyo, and Halifax samples stay on the sea path, and their `natural=bay` relations are not painted as lakes.

## Queries that failed

The Overpass query `rel(15522136); way(r:"outer"); out tags;` returned HTTP 504, dispatcher timeout, on 2026-09-27. Outer way tags and node ids were read from the OSM API `GET /api/0.6/ways?ways=` in batches of 80 instead. A burst that included the three comparison relations then returned HTTP 429. A later `out tags` query for relations 9451753, 13904308, and 13440670 succeeded. A query that counted coastline-tagged outers of those three relations returned HTTP 504; member lists were taken from the OSM API, and way tags were sampled as written above. The forest query in open water returned HTTP 504 and was not retried. One earlier bbox used longitude before latitude; Overpass rejected it because south and north must be latitudes between −90 and 90. The corrected bbox is the one cited above.
