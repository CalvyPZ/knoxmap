"""Draw a finished map the way the game draws it.

    python tools/make_pictures.py output/<map>
    python tools/make_pictures.py output/<map> --box 400,400,320,320 --no-roofs
    python tools/make_pictures.py output/<map> --out shot.png --size 2560x1440

With nothing but a map it writes the set worth having into the map's own
pictures folder: the whole town, the middle of it close enough to see, and
that same middle with the roofs off, which is how every room and everything
in it becomes visible.

The map has to have been compiled - these are drawn from the .lotheader and
.lotpack files the game itself loads, not from the terrain bitmap, so what
comes out is the map rather than an impression of it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from knoxbuild import picture as pictures  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", help="a map folder that has been compiled")
    parser.add_argument("--out", help="one picture, written here, instead of the set")
    parser.add_argument("--box", help="x,y,width,height in map tiles (default: all of it)")
    parser.add_argument("--no-roofs", action="store_true",
                        help="take the top off, so the rooms show")
    parser.add_argument("--size", default="1920x1080", help="the picture's size")
    parser.add_argument("--scale", type=float, default=None,
                        help="tile size, 1 being the game's own (default: to suit the area)")
    args = parser.parse_args(argv)

    size = tuple(int(v) for v in args.size.lower().split("x"))
    box = tuple(int(v) for v in args.box.split(",")) if args.box else None
    try:
        if args.out or box or args.no_roofs or args.scale:
            out = Path(args.out or "picture.png")
            pictures.picture(args.project, out, box=box, roofs=not args.no_roofs,
                             size=size, scale=args.scale)
            print(f"wrote {out}")
        else:
            made = pictures.pictures_of(args.project, size=size)
            for path in made:
                print(f"wrote {path}")
            if not made:
                print("nothing was drawn", file=sys.stderr)
                return 1
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
