# The Steam Workshop item

KnoxMap is a program you run on your PC, not a mod, so there is nothing to
subscribe to — but the Workshop is where Project Zomboid players look, and an
item there is how they find it.

This folder is that item, ready to upload:

    workshop/
        workshop.txt                 what the uploader reads
        preview.png                  the thumbnail on the item's card
        description.txt              the page text, in Steam's BBCode
        screenshots/                 the pictures for the item's gallery
        Contents/mods/KnoxMapTools/  what subscribers actually get

## The pictures

None of them is a mock-up. `tools/make_workshop_art.py <a built and compiled
map>` renders them from the compiled cells the game itself would load, the
way `KnoxMap/tools/render_lots.py` stacks them:

    01-town.png    the town from above
    02-town.png    the same blocks with the roofs off - every room, furnished
    03-town.png    a few streets close up
    04-plan.png    the plan the generator draws
    05-window.png  KnoxMap itself, with a real town's boundary picked out

The last one is `branding/window.png`, which is a screenshot of the app and
the only one that cannot be rendered. Take it at 1600x1000 with the window
showing a place chosen and the **Generate map** button lit - a headless
browser will do it:

    chrome --headless=new --window-size=1600,1000       --screenshot=branding/window.png       "http://localhost:<port>/?q=Rye%2C%20East%20Sussex&outline=1"

Pick somewhere small enough to stay under the tile limit, or the panel shows
a red warning instead of a green button.

## What subscribers get

**Reset loot** — right-click the ground and pick *Reset loot* to refill the
containers in a building, or everything within 30 tiles, without starting a
new save. It works in any building in any map, vanilla ones included; KnoxMap
writes the same file into every map it installs, and the first copy to load
claims it.

That is a real, working mod, which is the point: an item that only pointed at
a link would be an advertisement, and the Workshop is not for those. This one
does something on its own and says where the map generator lives.

## Uploading it

The game's own uploader reads from `~/Zomboid/Workshop/<folder>`:

1. `python tools/make_workshop.py` — copies this folder there, with the
   current Reset loot code and the version from docs/CHANGELOG.md.
2. Start Project Zomboid → **Workshop** → **Create and Upload**.
3. Pick **KnoxMapTools**, check the description and the pictures, upload.
4. Steam gives the item an id. Put it in `workshop.txt` as `id=<number>` so
   the next upload updates this item instead of making another.

The screenshots go up through the item's page on Steam after the first
upload (**Add Images/Videos**), in the order they are numbered here.

## Keeping it honest

The description says plainly that KnoxMap itself is a download from GitHub
and not a subscription, that maps it makes are the player's own, and that the
map data is OpenStreetMap's under ODbL. Nothing on the page claims the
Workshop item generates anything.
