# Junction types from OpenStreetMap

Date: 2026-09-27

Notes from the OpenStreetMap wiki pages for `highway=traffic_signals`, `highway=stop`, `highway=give_way`, `highway=crossing`, `highway=mini_roundabout`, `junction=roundabout`, and the direction keys those pages point at, plus the vanilla tile sheets under `Tiles/2x`. The generator does not invent a control for a junction the map does not tag.

The question is how a rasterizer that receives ways as coordinates, with no node ids, can still tell a signalised crossroads from a stop, a give-way, a roundabout or a plain corner, and which Project Zomboid tiles can show that.

## 1. Where the tags sit

A junction is not an object. It is a coordinate several `highway=*` ways share. What kind of junction it is lives on a node.

`highway=traffic_signals` is a node. The page allows it on the junction itself, or on each incoming way at the stop line. A signal on an incoming way carries `traffic_signals:direction=forward|backward|both`, naming which direction of that way the signal faces. A simple crossing is often one node on the junction, with `crossing=traffic_signals` when the pedestrian crossing is not mapped on its own. ([Tag:highway=traffic_signals](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dtraffic_signals))

`highway=stop` is also a node, normally on the approach rather than in the middle of the junction. `direction` or `stop:direction` says which way it faces. `stop=all` is an all-way stop. `stop=minor` marks the lesser road. ([Tag:highway=stop](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dstop))

`highway=give_way` is the same shape as a stop: a node on the approach, with `direction` or `give_way:direction`. ([Tag:highway=give_way](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dgive_way))

`highway=crossing` is the pedestrian crossing. `crossing=marked|zebra|uncontrolled|traffic_signals|unmarked`, or `crossing:markings=*`, says whether there are stripes. `unmarked` and `crossing:markings=no` are a dropped kerb with no paint. ([Tag:highway=crossing](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dcrossing))

`junction=roundabout` and `junction=circular` are on the way, which is closed and one-way. A closed way is not an area. `highway=mini_roundabout` is a node at the centre of a small island that is only painted. `highway=turning_circle` and `highway=turning_loop` are the node at the end of a dead-end street, with an optional `diameter` or `radius`. ([Tag:junction=roundabout](https://wiki.openstreetmap.org/wiki/Tag:junction%3Droundabout), [Tag:highway=mini_roundabout](https://wiki.openstreetmap.org/wiki/Tag:highway%3Dmini_roundabout))

## 2. Why the nodes are a separate download

Overpass `out geom` returns each way as a list of coordinates. The node ids are not in that geometry, so a junction cannot be found by "the same node is in two ways". It is found the way the octilinear pass already finds one: a coordinate key, `round(lat * 1e7), round(lon * 1e7)`, shared by more than one road end.

The control nodes are therefore requested on their own:

`node["highway"~"^(traffic_signals|stop|give_way|crossing|mini_roundabout|turning_circle|turning_loop)$"]`

`FILTERS_VERSION` is 12, so a cache built before that filter is downloaded again once. The local PBF reader builds its filters from the same list, so an extract picks the nodes up too.

A node that lands on a junction vertex belongs to that junction. A node on an approach is walked along its way, in the tagged direction, up to 30 m, and attached to the first junction it meets. A node that matches no vertex is snapped to the nearest road segment within 5 m, or dropped. A `highway=crossing` that is more than 15 m from any junction stays a crossing in the middle of the block.

Two junction vertices closer than 12 m, or closer than the sum of their road widths, are one junction, so the two carriageways of a dual road count once. Vertices are not merged just because a chain of short blocks can hop from one to the next: every pair in the group has to fall inside that span.

Each cluster gets one type, and each arm gets its own control (`signal`, `stop`, `give_way`, or `none`). A signal outranks a stop, and a stop outranks a give-way. Nothing is filled in for an arm the tags do not name. Three or more arms and no control tags is `uncontrolled`: the corners are squared so the kerb can turn, and no sign or line is painted. A junction that is only a service road joining a street is left alone. A one-arm node gets a bulb only when it is tagged `turning_circle` or `turning_loop`.

Signs and lines assume traffic keeps right, the same rule the speed signs already use. Knox County drives on the right.

## 3. Tiles

Vanilla sheets, 8 columns, checked in `Tiles/2x`. Facing is recorded the same way as the lamp arms (`lighting_outdoor_01_8` north, `_9` east, `_10` south, `_11` west).

| Marker | Colour | Tile | Layer |
| --- | --- | --- | --- |
| Stop sign, face north (back) | `(12, 39, 200)` | `street_decoration_01_2` | `0_Furniture` |
| Stop sign, face east (front) | `(12, 39, 201)` | `street_decoration_01_1` | `0_Furniture` |
| Stop sign, face south (front) | `(12, 39, 202)` | `street_decoration_01_0` | `0_Furniture` |
| Stop sign, face west (back) | `(12, 39, 203)` | `street_decoration_01_3` | `0_Furniture` |
| Signal pole, arm north | `(12, 39, 204)` | `lighting_outdoor_01_12` | `0_Furniture` |
| Signal pole, arm east | `(12, 39, 205)` | `lighting_outdoor_01_13` | `0_Furniture` |
| Signal pole, arm south | `(12, 39, 206)` | `lighting_outdoor_01_14` | `0_Furniture` |
| Signal pole, arm west | `(12, 39, 207)` | `lighting_outdoor_01_15` | `0_Furniture` |
| Stop line, runs east-west | `(12, 39, 208)` | `street_trafficlines_01_32` | `0_FloorOverlay5` |
| Stop line, runs north-south | `(12, 39, 209)` | `street_trafficlines_01_0` | `0_FloorOverlay5` |
| Crosswalk, runs east-west | `(12, 39, 212)` | `street_trafficlines_01_2` | `0_FloorOverlay5` |
| Crosswalk, runs north-south | `(12, 39, 213)` | `street_trafficlines_01_0` | `0_FloorOverlay5` |
| Street-name post | `(12, 39, 214)` | `street_decoration_01_22` | `0_Furniture` |

`street_decoration_01_0` and `_1` are the two fronts of the stop sign. `_2` and `_3` are the backs. The fronts are the south and east faces, which is what the isometric camera shows as a face; the backs are north and west.

`lighting_outdoor_01_12` to `_15` sit on the same row as the lamp arms and follow that order: north, east, south, west. `_20` to `_23` are the other set and are not used.

The only thick white bar on `street_trafficlines_01` is horizontal (`_32`). There is no thick vertical bar, so a north-south stop line uses the faded edge line `_0`, the same stroke the centre lines already use for a north-south white line. A give-way line is that white line drawn dashed. It has no sign: the vanilla sheets have no yield sign. Erika's Tiles duplicates some of these and adds a pedestrian-crossing warning, and none of that is required.

Overlay layers 1 to 4 are wiped where the ground blends. The lines are on `0_FloorOverlay5`, which survives. Signs and poles are `0_Furniture`.

A diagonal arm gets the sign or the pole only. A line that steps from tile to tile reads as a zigzag, and the kerb set only has the eight axis pieces, so a 45-degree corner is not rebuilt.

## 4. Distances

Maps are 1 m or 2 m a tile. Every distance is in metres, and then at least one tile, so a stop line does not jump a lane when the scale changes.

| Use | Distance |
| --- | --- |
| Two vertices count as one junction | 12 m, or the sum of the two road widths, whichever is more |
| A control node on an approach belongs to the junction | 30 m along the way, in the tagged direction |
| A node not on a vertex snaps to a segment | 5 m, otherwise dropped |
| A crossing belongs to the junction rather than the middle of the block | 15 m |
| Crosswalk stripes | two lines, 3 m apart |
| Stop or give-way line | one tile beyond the stripes, or one tile beyond the mouth when there are none |
| Sign or pole | right-hand pavement, two tiles out from the gutter, stepped out to four if that tile is road |
| No parking | the junction box, plus 9 m along each arm |
| Mini-roundabout disc | 2 tiles radius, and only when the box is at least 5 tiles each way |
| Roundabout island paving | inner radius under 6 m is paving; otherwise grass |
| Turning-circle bulb | `diameter` or `radius`, otherwise 1.2 times the road width, clamped to 8–15 m |

The mouth of an arm is the half-width of the widest arm that crosses it, plus one tile. The stop line sits there, across the right-hand half of the road. A one-way arm that only enters the junction takes the line across the full width.

Parked cars read `{map}_junctions.json`. Each junction has a `no_parking` list of rectangles `(x, y, w, h)`. A map drawn before this file existed keeps the petrol-station forecourts and nothing else.

## 5. What this does not do

Untagged junctions get no sign and no signal. Level crossings are not read. Left-hand traffic is not placed. A roundabout on a bridge deck is left as the deck: the ground under a bridge is the road it crosses.
