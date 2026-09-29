"""The end-to-end path on its own, with the numbers printed.

The selftest is two hundred checks over every corner of KnoxMap and takes
several minutes. This is the one line a release has to walk - terrain,
buildings, paper map, install - on a small synthetic town, in about a minute.
It prints what each step produced rather than only pass or fail, so a release
can be eyeballed as well as gated.

    python KnoxMap/tools/smoke_test.py [--keep]

It builds into a temporary folder and installs into a temporary Zomboid
folder, so it never touches a real map or a real mods directory.

Exit code 0 if every step produced something sane, 1 otherwise.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class Report:
    """Lines of (step, detail, ok), printed as they happen."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, bool]] = []
        self.failed = 0
        self.started = time.time()

    def say(self, step: str, detail: str, ok: bool = True) -> None:
        self.rows.append((step, detail, ok))
        if not ok:
            self.failed += 1
        print(f"  {'ok  ' if ok else 'FAIL'}  {step}: {detail}", flush=True)

    def text(self) -> str:
        out = [f"KnoxMap smoke test - {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for step, detail, ok in self.rows:
            out.append(f"{'ok  ' if ok else 'FAIL'}  {step}: {detail}")
        out += ["", f"{len(self.rows) - self.failed} of {len(self.rows)} steps in "
                    f"{time.time() - self.started:.0f}s",
                "smoke test passed" if not self.failed else
                f"smoke test failed ({self.failed})"]
        return "\n".join(out) + "\n"


def run(work: str, report: Report, compile_too: bool = False) -> None:
    from generator import renderer
    from selftest import EAST, NORTH, SOUTH, WEST, town

    feats = town()
    out = os.path.join(work, "smoke")

    # 1. Terrain. The ground BMP is what everything after it is placed on.
    angle, _strength = renderer.dominant_road_angle(feats, SOUTH, WEST, NORTH, EAST)
    renderer.render(feats, SOUTH, WEST, NORTH, EAST, meters_per_tile=1.0,
                    output_dir=out, map_name="smoke", rotation=-angle)
    from PIL import Image
    ground = Image.open(os.path.join(out, "smoke.bmp"))
    report.say("terrain", f"{ground.width} x {ground.height} tiles, road grid at "
                          f"{angle:.0f} degrees", ground.width > 100)

    # The download the terrain was drawn from, kept where the rest of the
    # build looks for it. Without it the paper map gets the buildings and
    # nothing else: no roads, no water, no street names.
    from generator import osm
    osm.save_cache(osm.cache_path(out, "smoke"), (SOUTH, WEST, NORTH, EAST), feats)

    # 2. Buildings. Every footprint laid out, furnished and written as a .tbx.
    import contextlib
    import io as _io

    from knoxbuild.build import build
    from knoxbuild.settings import Settings
    with contextlib.redirect_stdout(_io.StringIO()):
        build(out, settings=Settings(seed=1))
    bdir = os.path.join(out, "buildings")
    tbx = sorted(f for f in os.listdir(bdir) if f.endswith(".tbx"))
    # The CSV is the buildings; the rest of what lands in buildings/ is
    # fences, raised structures, petrol pumps and props. What has to hold is
    # that the project places every one of them - a .tbx nothing refers to is
    # a building generated and then lost.
    rows = open(os.path.join(out, "smoke_placements.csv"),
                encoding="utf-8").read().splitlines()[1:]
    import re as _re
    project = open(os.path.join(out, "smoke.pzw"), encoding="utf-8").read()
    placed = set(_re.findall(r"buildings/([A-Za-z0-9_]+\.tbx)", project))
    lost = [f for f in tbx if f not in placed]
    report.say("buildings", f"{len(rows)} buildings, {len(tbx)} .tbx with the "
                            f"fences and props, {len(lost)} of them unplaced",
               len(rows) >= 20 and not lost)

    # 3. Every .tbx the way the game reads it: rooms closed, nothing outside
    #    its own walls, no furniture hanging through one.
    from validate_tbx import check as validate
    bad = []
    for name in tbx:
        found = validate(os.path.join(bdir, name))
        if found:
            bad.append(f"{name}: {found[0]}")
    report.say("building files", "every one valid" if not bad
               else f"{len(bad)} bad, first {bad[0]}", not bad)

    # 4. Furniture, so an empty town cannot pass as a built one.
    pieces = doors = 0
    for name in tbx:
        text = open(os.path.join(bdir, name), encoding="utf-8").read()
        pieces += text.count('type="furniture"')
        doors += text.count('type="door"')
    report.say("interiors", f"{pieces} pieces of furniture, {doors} doors "
                            f"({pieces / max(len(tbx), 1):.0f} per building)",
               pieces > len(tbx) * 10)

    # 4b. And the outside of them. Knox County hangs a light by 92% of its
    #     front doors; without one a street of houses reads as unfinished.
    import csv as _csv
    houses = sum(1 for r in _csv.DictReader(open(
        os.path.join(out, "smoke_placements.csv"), encoding="utf-8"))
        if r["kind"] == "house")
    lit = 0
    for name in tbx:
        if "_lights_" not in name:
            continue
        text = open(os.path.join(bdir, name), encoding="utf-8").read()
        for grid in _re.findall(r'<tiles layer="[^"]*">(.*?)</tiles>', text, _re.S):
            lit += sum(1 for v in grid.replace("\n", "").split(",")
                       if v.strip() not in ("", "0"))
    report.say("front doors", f"{lit} porch lights on {houses} houses "
                              f"({100 * lit / max(houses, 1):.0f}%, the game's is 92%)",
               houses and lit >= houses * 0.8)

    # 5. Where the map landed. A map on a PC with nothing installed belongs on
    #    the standard origin: the further east it sits, the smaller the town
    #    draws on a paper map that is one grid counted from cell 0.
    from knoxbuild.world import WORLD_ORIGIN_CELLS, choose_origin
    pzw = open(os.path.join(out, "smoke.pzw"), encoding="utf-8").read()
    import re
    got = re.search(r'<worldOrigin origin="(\d+),(\d+)"', pzw)
    here = (int(got.group(1)), int(got.group(2))) if got else (-1, -1)
    report.say("world origin", f"cell {here[0]},{here[1]} "
                               f"(standard is {WORLD_ORIGIN_CELLS[0]},"
                               f"{WORLD_ORIGIN_CELLS[1]})",
               here == WORLD_ORIGIN_CELLS)

    # And the next map generated beside it, which nobody has installed, goes
    # to the same place rather than further out again.
    beside = os.path.join(work, "smoke_again")
    os.makedirs(beside, exist_ok=True)
    nxt = choose_origin(beside, 3, 3)
    report.say("the next map", f"cell {nxt[0]},{nxt[1]}, not pushed east",
               nxt == WORLD_ORIGIN_CELLS)

    # 6. The paper map. What matters is how much of the grid the town fills:
    #    the game draws one grid from cell 0, so a map parked far out is a
    #    speck in the corner of an empty world.
    from knoxbuild.worldmap_bin import write_bin
    xml = os.path.join(out, "worldmap.xml")
    n = write_bin(xml, xml + ".bin")
    from knoxbuild.worldmap_bin import read_bin
    cells = read_bin(xml + ".bin")
    xs = [x for x, _ in cells]
    ys = [y for _, y in cells]
    # The grid runs from cell 0 whatever the map, so what says the town is
    # where it should be is that its first cell is the standard origin's and
    # not some way east of it. 300-tile source cells, 256-tile map cells.
    from knoxbuild.world import CELL_SIZE
    want = WORLD_ORIGIN_CELLS[0] * CELL_SIZE // 256
    blank = min(xs) - want
    report.say("paper map", f"{n} outlines in {len(cells)} cells, "
                            f"x {min(xs)}-{max(xs)} y {min(ys)}-{max(ys)} of a "
                            f"{max(xs) + 1} x {max(ys) + 1} grid, "
                            f"{blank} empty columns past the standard origin",
               n > 100 and blank == 0)

    # 7. Install, into a Zomboid folder of its own. Compiling the cells is
    #    WorldEd's job and takes longer than the rest of this put together,
    #    so unless it is asked for the compiled cells are stood in for and
    #    what is checked is the mod built around them.
    from make_map_mod import folder_name, package
    lots = os.path.join(out, "lots")
    os.makedirs(lots, exist_ok=True)
    if compile_too:
        from compile_map import compile_map as run_worlded
        with contextlib.redirect_stdout(_io.StringIO()):
            run_worlded(out)
        made = [f for f in os.listdir(lots) if f.endswith(".lotheader")]
        report.say("compile", f"{len(made)} cells compiled by WorldEd", bool(made))
    else:
        open(os.path.join(lots, "0_0.lotheader"), "wb").write(b"stand-in")
        report.say("compile", "skipped, stood in for (--compile runs WorldEd)")

    mods = os.path.join(work, "zomboid", "mods")
    with contextlib.redirect_stdout(_io.StringIO()):
        mod_root, n_cells, _extras = package(out, "Smoke Town", "smoketown",
                                             mods_dir=mods)
    files = sum(len(f) for _r, _d, f in os.walk(mod_root))
    maps = os.path.join(mod_root, "common", "media", "maps",
                        folder_name("Smoke Town", "smoketown"))
    paper = os.path.exists(os.path.join(maps, "worldmap.xml.bin"))
    report.say("install", f"{files} files in the mod, {n_cells} cell files, "
                          f"paper map {'in it' if paper else 'MISSING'}",
               files >= 15 and paper)

    # 8. And the one thing a server needs that a single player does not.
    setup = os.path.join(mod_root, "SERVER SETUP.txt")
    report.say("server files", "SERVER SETUP.txt and the spawn regions"
               if os.path.exists(setup) else "missing", os.path.exists(setup))

    # 9. Street names, which is how the in-game map is read.
    named = {s.get("name") for s in ET.parse(os.path.join(out, "streets.xml"))
             .getroot().iter("street") if s.get("name")}
    report.say("street names", f"{len(named)} named streets on the map",
               len(named) >= 5)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    keep = "--keep" in argv
    work = tempfile.mkdtemp(prefix="knoxmap-smoke-")
    os.environ["KNOXMAP_LOG_DIR"] = os.path.join(work, "logs")
    # Never the real one: a smoke test installs a map, and a stray run has
    # landed in a player's mods folder before now.
    os.environ["ZOMBOID_DIR"] = os.path.join(work, "zomboid")
    report = Report()
    print(f"KnoxMap smoke test in {work}", flush=True)
    try:
        run(work, report, compile_too="--compile" in argv)
    except Exception as exc:                      # noqa: BLE001 - it is a test
        import traceback
        traceback.print_exc()
        report.say("crashed", f"{type(exc).__name__}: {exc}", False)
    finally:
        if keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    print()
    print(report.text())
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "logs", "smoke_test.txt")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(report.text())
        print(f"written to {out}")
    except OSError:
        pass
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
