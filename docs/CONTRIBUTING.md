# Contributing

Thanks for helping. A few things keep KnoxMap working and publishable.

## Before you open a pull request

Run these from the `KnoxMap` directory, where setup creates `.venv`
(they are what GitHub Actions runs):

```bat
.venv\Scripts\python tools\selftest.py
.venv\Scripts\python tools\audit_layouts.py 200
```

`selftest.py` needs no internet, game or map tools. If you changed how
buildings or roads look, also generate a small real area and look at it with
`tools\render_ground.py`, or better, in the game, and include a screenshot.

## Never commit

- **Project Zomboid files**, including tile sheets extracted by Setup
  (`Tiles/`, `*.pack`). They belong to The Indie Stone. The `.gitignore` already
  excludes them; please don't force them in.
- **Generated maps** (`output/`), downloaded tools (`vendor/`) or the tile cache
  (`cache/`).
- **Personal paths or keys.** Paths go through `knoxpaths.py`.

## Be a good citizen of OpenStreetMap's servers

KnoxMap uses free, volunteer-run services. Changes must keep to their usage
policies (see [LEGAL.md](LEGAL.md)): the search box may offer results as you
type, but Nominatim still gets at most one request a second and a newer
query replaces one that has not come back. Identify the app, cache what you
fetch, and never bulk-download tiles. Map geometry comes from Geofabrik
extracts read in the program, not from a grid of Overpass queries.

## Style

- Match the code around you. Comments say *why*, and usually what went wrong
  before: a measured failure beats a guess.
- New behaviour gets a check in `tools/selftest.py` or `tools/audit_layouts.py`
  when it can break.
- Keep terms and credits intact: OpenStreetMap attribution, The Indie Stone's
  fan-production credit, and the licences in `docs/LICENSES.md`.

## Licences

By contributing you agree your work is released under the licence of the part
of the project it goes into: MIT for most files, GPL for `KnoxMap/worlded/` (see
[LICENSES.md](LICENSES.md)).

## Releasing

Add a `## <version>` section to `docs/CHANGELOG.md`, then push a tag:

    git tag v1.1
    git push origin v1.1

GitHub Actions runs the checks and publishes one program file per system:
`KnoxMap-v1.1-windows.exe`, `KnoxMap-v1.1-linux.AppImage`, and
`KnoxMap-v1.1-macos.dmg`, with that CHANGELOG section as the notes. A tag
whose suffix names one system (`v1.1-mc1`, `v1.1-win1`, `v1.1-lin1`) publishes
only that system's file. The version the window shows is the first `##`
heading in `docs/CHANGELOG.md`, so that heading has to be the version.
