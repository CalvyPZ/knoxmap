# KnoxMap on Linux and macOS

Everything KnoxMap does itself is Python, and runs anywhere Python does:
reading the area from a Geofabrik extract of OpenStreetMap, drawing the
terrain, laying out and furnishing the buildings, writing the paper map and
installing the mod. The map reader is one of those libraries, already inside
the program file. **Compile** drives the PZ Mapping Tools. On 64-bit Linux,
setup fetches a build of the map compiler made for this system, so that needs
no help either; anywhere else it is a Windows program and runs through Wine.

Download `KnoxMap-v…-linux.AppImage` (or `…-macos.dmg`) from
[Releases](https://github.com/spytheeuclidean-a11y/knoxmap/releases/latest).
That file is the whole program. Python and the map reader are already inside.

    chmod +x KnoxMap-v1.5-linux.AppImage
    ./KnoxMap-v1.5-linux.AppImage

On macOS, open the dmg and drag KnoxMap to Applications. The first launch
still downloads the map tools and reads tiles from your own copy of the game.
Maps and logs appear beside the AppImage. A copy that lives in Applications
keeps them in a `KnoxMap` folder in your home directory.

A git checkout is the other way in. It needs 64-bit Python 3.10+. From the
`KnoxMap` folder:

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    chmod +x knoxmap.sh
    ./knoxmap.sh

`./knoxmap.sh` expects `.venv` to exist already. The first time the window
opens, it downloads the map tools and reads tiles from your game.

## What setup does

The first open, or `python knoxmap_setup.py` from the `KnoxMap` folder, puts
the PZ Mapping Tools into `vendor/`, fetches the patched map compiler, and
extracts the tile artwork from **your own copy of the game** — never
downloaded. The Python environment above is the checkout's own step; the
program file already has Python inside it.

It needs 64-bit Python 3.10 or newer. A 32-bit one can only address about
2 GB, which a town-sized map runs out of part-way through.

A checkout needs Python:

    sudo apt install python3 python3-venv python3-pip     # Debian, Ubuntu
    sudo pacman -S python                                 # Arch
    sudo dnf install python3                              # Fedora

## The window

The window is Electron, the same one on Windows, Linux and macOS, and it is
inside the AppImage or the app. It does not need the GTK or Qt libraries
Python used to need for a window of its own. A checkout uses the Electron
binary from `desktop/` after `npm install` when `desktop/main.js` is present,
and opens in the browser when it is not.

It does need the ordinary desktop libraries, which a normal desktop already
has. If the window will not open and `logs/knoxmap.log` names one of them,
install the set your distribution ships:

    sudo apt install libnss3 libgtk-3-0 libasound2       # Debian, Ubuntu
    sudo pacman -S nss gtk3 alsa-lib                      # Arch
    sudo dnf install nss gtk3 alsa-lib                    # Fedora

If the sandbox cannot start — `chrome-sandbox` is not setuid, which it cannot
be without root, or AppArmor on Ubuntu 24.04 and newer will not allow a user
namespace — KnoxMap opens the window once more without the sandbox and says
so in the log. The page is only this computer's; nothing is loaded from
anywhere else. From the `KnoxMap` folder, `KNOXMAP_BROWSER=1 ./knoxmap.sh` forces
the browser on any system, and a missing window does that on its own.

## Compile

On 64-bit Linux there is nothing to install. Setup downloads the map
compiler built for this system — the same program from the same source as
the Windows one, with the Qt it needs beside it — checks its fingerprint and
puts it in `vendor/PZMappingTools/bin/`. No Wine, no Qt to install, and
WorldEd's own log ends up in `logs/worlded/` where you can read it.

On macOS, on a 32-bit or ARM machine, or if that download fails, the map
compiler is the Windows one and KnoxMap runs it through Wine, which a PC
that plays Project Zomboid through Proton already has in some form:

    sudo apt install wine       # or wine64, or your distribution's package

From the `KnoxMap` folder, `KNOXMAP_WINE=/path/to/wine ./knoxmap.sh` points KnoxMap at a particular
build — a Proton runtime's, for instance.

### If the compiler will not start

The build for Linux carries its own Qt, and it has to be the one that loads.
The binary records that folder as DT_RUNPATH, which the loader searches after
`LD_LIBRARY_PATH` - so KnoxMap puts it on the front of `LD_LIBRARY_PATH`
itself before running the compiler. Without that, a machine with its own Qt 5
on it compiled against 5.15.3 and loaded 5.15.13, and Qt aborted the run:

    Cannot mix incompatible Qt library (5.15.13) with this library (5.15.3)

Setup runs the compiler once after installing it and says so if it will not
start. If it still happens, something is putting a Qt ahead of the bundled
one: start KnoxMap from a plain terminal rather than from Steam, or clear
`LD_LIBRARY_PATH` for it. `KNOXMAP_QT_PLATFORM` overrides the platform
plugin, which is `offscreen` because a compile draws nothing.

This system also has to be Ubuntu 22.04's vintage or newer - the build
leaves the C library and libstdc++ to the machine. On anything older,
install wine and KnoxMap will use the Windows build instead.

With neither, every step except Compile works, and the window says so under
**Setup isn't finished**. You can still finish a map by hand: use **Open in
WorldEd** and run *BMP To TMX → All Cells* and *Generate Lots → All Cells*
yourself, then **Install**.

If you build the tools natively (see `KnoxMap/worlded/README.md`), drop the
binaries in `KnoxMap/vendor/PZMappingTools/bin/` **without** the `.exe` — KnoxMap
finds `PZWorldEd_cli` as readily as `PZWorldEd_cli.exe`, runs it directly,
and setup leaves it alone.

## Where things are found

- **The game**, and the mods folder it installs into, come from Steam:
  `~/.steam/steam`, `~/.local/share/Steam`, the Flatpak
  (`~/.var/app/com.valvesoftware.Steam/…`) and Snap paths, plus every
  library `libraryfolders.vdf` names. Libraries on a second disk are looked
  for under `/mnt`, `/media`, `/run/media` and your home folder.
- **Saves and mods** are in `~/Zomboid`, as on Windows. `ZOMBOID_DIR` moves
  that if yours is somewhere else.
- If a library is somewhere none of this looks, on **Setup → Game** choose
  the folder that contains the game's jar, or set
  `KNOXMAP_STEAM_FOLDERS=/path/one;/path/two`.

## Proton and the game's memory

A big map needs more memory than Project Zomboid gives itself. Under Proton
the setting is in the same place as on Windows — `ProjectZomboid64.json` in
the game folder — and a map's mod folder carries a `HOW TO PLAY.txt` saying
what to change when it is big enough to matter.
