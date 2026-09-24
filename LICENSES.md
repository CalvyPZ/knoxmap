# Licences and credits

This repository combines work under different terms. Please check which part
you are using before reusing it.

## Original Knoxify — arytek, no licence published

KnoxMap is a fork of [arytek/knoxify](https://github.com/arytek/knoxify).
That project has not published a licence, so its author keeps all rights to
it. These files come from it (most have since been modified here):

`README.md` · `KnoxMap/app.py` · `KnoxMap/generator/__init__.py` · `KnoxMap/generator/osm.py` ·
`KnoxMap/generator/pz_colors.py` · `KnoxMap/generator/renderer.py` · `KnoxMap/requirements.txt` ·
`KnoxMap/static/css/app.css` · `KnoxMap/static/js/app.js` ·
`KnoxMap/templates/index.html` · `.gitignore`

The KnoxMap logo and cover (`branding/*`, `static/logo.svg`) are new to this
fork and replace Knoxify's own artwork; they are under the MIT licence below.

If you want to reuse
these files beyond that, ask arytek.

## Added in this fork — MIT

See [LICENSE-MIT.txt](LICENSE-MIT.txt). Everything not listed in the other
two sections, including:

`KnoxMap/knoxbuild/` · `KnoxMap/tools/` · `KnoxMap/knoxmap.py` · `KnoxMap/knoxmap_setup.py` · `KnoxMap/knoxpaths.py` ·
`Setup.bat` · `KnoxMap/generator/places.py` · `KnoxMap/static/js/fx.js` ·
`desktop/` · `KNOXBUILD.md` · `LICENSES.md` · `CHANGELOG.md` ·
`CONTRIBUTING.md` · `.github/` · `docs/`

The program file is [Electron](https://www.electronjs.org/), which is MIT,
built on Chromium, with CPython and the Python libraries packed inside.
Electron's own licence and Chromium's `LICENSES.chromium.html` ship in that
file. They are not copied into this repository.

## The map compiler patch — GPL-2.0-or-later

Everything in [`KnoxMap/worlded/`](KnoxMap/worlded/), and the prebuilt `PZWorldEd_cli.exe` in
this repository's releases, modifies
[PZ Mapping Tools](https://github.com/Unjammer/PZ_Mapping_Tools) (Alree /
Unjammer, built on Tim Baker's TileZed and WorldEd) and is distributed under
the GNU General Public License version 2 or later. Each compiler release
carries the binary's complete corresponding source (`PZWorldEd_cli-source.zip`,
made by `KnoxMap/worlded/make_release.py`), the licence text and a written source
offer. See [worlded/README.md](KnoxMap/worlded/README.md) and
[docs/LEGAL.md](docs/LEGAL.md#the-map-compiler-gnu-gpl-version-2).

## Things this repository does not contain

- **Project Zomboid artwork.** Setup extracts tile sheets from each player's
  own installed copy of the game. They belong to The Indie Stone and are never
  included here or downloaded from anywhere.
- **PZ Mapping Tools itself.** Setup downloads the official release from its
  own GitHub page.
- **The Elevators mod.** KnoxMap lays out lifts the way that mod recognises
  them; the mod is a separate work, installed by players from the Steam
  Workshop.

Some pictures in `docs/images/` (`roads_ingame_tiles.jpg`) are drawn from
Project Zomboid's tile artwork by `KnoxMap/tools/render_ground.py`. That artwork is
© The Indie Stone and is shown only to illustrate what KnoxMap produces; it is
not covered by this repository's licences. Pictures made from map data
(`nyc_midtown.png`, `shape_circle.png`, `app.jpg`) contain OpenStreetMap data
and tiles, © OpenStreetMap contributors.

## Data and services

- **Map data** © [OpenStreetMap](https://www.openstreetmap.org/copyright)
  contributors, available under the Open Database License. Maps you generate
  are built from it and should credit "© OpenStreetMap contributors" if you
  share them.
- **Overpass API** and **Nominatim** are free community services with usage
  policies; KnoxMap identifies itself and keeps to one search a second.
- **Map tiles** in the app: OpenStreetMap standard tiles, fetched through
  KnoxMap's local server under the
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
  (There is no satellite view: Esri's imagery terms do not cover this use.)
- **Bundled in `static/vendor/`**, each with its licence text beside it:
  **Leaflet** 1.9.4 (BSD-2-Clause, © Volodymyr Agafonkin), **Leaflet.draw**
  1.0.4 (MIT, © Jon West, Jacob Toye and Leaflet).

## Not affiliated

KnoxMap is an unofficial fan project. It is not made, endorsed or supported by
The Indie Stone. Project Zomboid is a trademark of The Indie Stone.
