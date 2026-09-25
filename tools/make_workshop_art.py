"""Make the Workshop item's thumbnail and gallery pictures.

    python tools/make_workshop_art.py output/<a built and compiled map>

The pictures are the map itself, not mock-ups: the plan the generator draws,
and the compiled cells rendered the way the game stacks them (see
tools/render_lots.py). The thumbnail is the project's own cover art, which
already carries the wordmark, over a strip of that render.

Everything lands in workshop/preview.png and workshop/screenshots/.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "KnoxMap"
sys.path.insert(0, str(APP))

OUT = REPO / "workshop"
SHOTS = OUT / "screenshots"
COVER = REPO / "docs" / "branding" / "cover.png"
# Steam shows the thumbnail small and the gallery big.
PREVIEW = (512, 512)
SHOT = (1920, 1080)
BACKGROUND = (20, 24, 15)



# The thumbnail is the roofs-off render with the wordmark on a band across
# the foot. Steam lists an item at about a hundred pixels beside its title,
# and at that size the old thumbnail - half of it a black bar of unreadable
# text over a thin strip of map - was a dark smudge in a dark list. The band
# is the only part of it that carries at that size, and the render fills the
# rest instead of being squeezed into a strip.
WORDMARK = "KnoxMap"
STRAPLINE = "REAL PLACES, PLAYABLE IN PROJECT ZOMBOID"
GREEN = (165, 226, 102)          # the pin in docs/branding/logo.svg
INK = (18, 22, 14)
BAND = 120
# Of the 1920x1080 render, the quarter with the most furnished rooms in it
# and a corner of the park for colour. The rooms are what is worth showing:
# a park at this size is a green blob.
THUMB_CROP = (480, 280, 1280, 1080)
# Bold and wide, whatever the machine has. The picture ships, not the font.
FONTS = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
         "LiberationSans-Bold.ttf", "Helvetica.ttc")


def _font(px: int):
    from PIL import ImageFont

    here = Path("C:/Windows/Fonts")
    for name in FONTS:
        for folder in (here, Path("/usr/share/fonts/truetype/dejavu"),
                       Path("/usr/share/fonts/truetype/liberation"),
                       Path("/System/Library/Fonts")):
            path = folder / name
            if path.exists():
                try:
                    return ImageFont.truetype(str(path), px)
                except OSError:
                    pass
    return ImageFont.load_default()


def thumbnail(render: Path) -> Image.Image:
    """The 512x512 card Steam shows beside the item's name."""
    from PIL import ImageDraw

    im = Image.open(render).convert("RGB").crop(THUMB_CROP)
    im = im.resize(PREVIEW, Image.Resampling.LANCZOS)
    im.paste(Image.new("RGB", (PREVIEW[0], BAND), GREEN), (0, PREVIEW[1] - BAND))
    draw = ImageDraw.Draw(im)
    draw.text((26, PREVIEW[1] - BAND + 10), WORDMARK, font=_font(58), fill=INK)
    draw.text((29, PREVIEW[1] - BAND + 82), STRAPLINE, font=_font(18),
              fill=(44, 60, 28))
    return im


def fit(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """`image` centred on the project's background, cropped to fill."""
    want_w, want_h = size
    scale = max(want_w / image.width, want_h / image.height)
    big = image.resize((max(1, round(image.width * scale)),
                        max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    left = (big.width - want_w) // 2
    top = (big.height - want_h) // 2
    return big.crop((left, top, left + want_w, top + want_h))


def letterbox(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The whole of `image`, on the background, nothing cropped away.

    Never made bigger than it is: Steam shows these at whatever size they
    come in, and an upscaled picture only looks soft.
    """
    want_w, want_h = size
    scale = min(want_w / image.width, want_h / image.height, 1.0)
    if scale >= 1.0 and abs(image.width / image.height - want_w / want_h) < 0.01:
        return image.convert("RGB")
    small = image.resize((max(1, round(image.width * scale)),
                          max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
    out = Image.new("RGB", size, BACKGROUND)
    out.paste(small, ((want_w - small.width) // 2, (want_h - small.height) // 2))
    return out


def render_lots(project: Path, x: int, y: int, w: int, h: int, scale: float,
                where: Path, max_level: int | None = None) -> Path | None:
    """One isometric view of the compiled cells, through render_lots.py."""
    cmd = [sys.executable, str(APP / "tools" / "render_lots.py"), str(project),
           str(where), str(x), str(y), str(w), str(h), "--scale", str(scale)]
    if max_level is not None:
        cmd += ["--max-level", str(max_level)]
    done = subprocess.run(cmd, capture_output=True, text=True, check=False,
                          cwd=str(APP))
    if done.returncode != 0 or not where.exists():
        print(f"   could not render {x},{y} {w}x{h}: "
              f"{(done.stderr or done.stdout).strip()[-200:]}")
        return None
    return where


def trim(image: Image.Image) -> Image.Image:
    """Cut the empty border off a render, which is mostly diagonal nothing."""
    if image.mode != "RGBA":
        return image
    box = image.getbbox()
    return image.crop(box) if box else image


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", help="a map folder that has been built and compiled")
    parser.add_argument("--spots", default="",
                        help="x,y,w,h[,top storey];... - which parts of the map to "
                             "render, and how far up; three views of the middle by "
                             "default, one of them with the roofs off")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    name = project.name
    info_path = project / f"{name}_info.json"
    if not info_path.exists():
        print(f"{info_path} is missing - is that a map folder?", file=sys.stderr)
        return 2
    import json
    info = json.loads(info_path.read_text(encoding="utf-8"))
    width, height = info["width_tiles"], info["height_tiles"]

    SHOTS.mkdir(parents=True, exist_ok=True)
    work = SHOTS / "_raw"
    work.mkdir(exist_ok=True)

    if args.spots:
        spots = []
        for piece in args.spots.split(";"):
            bits = [int(v) for v in piece.split(",")]
            spots.append(tuple(bits[:4]) + ((bits[4],) if len(bits) > 4 else ()))
    else:
        # The town from above; the same blocks with the roofs off, which is
        # the one that shows what is actually inside every building; and a
        # close view of a few streets.
        spots = [
            (width // 2 - 170, height // 2 - 170, 340, 340, None),
            (width // 2 - 80, height // 2 - 80, 160, 160, 0),
            (width // 2 - 60, height // 2 - 60, 120, 120, None),
        ]

    made: list[Path] = []
    print("rendering the compiled cells...")
    for n, spot in enumerate(spots, 1):
        x, y, w, h = spot[:4]
        max_level = spot[4] if len(spot) > 4 else None
        scale = 1.0 if w <= 180 else 0.55
        got = render_lots(project, x, y, w, h, scale, work / f"lots{n}.png",
                          max_level=max_level)
        if got:
            made.append(got)
            print(f"   {got.name}: {x},{y} {w}x{h}"
                  + (" (ground floor only)" if max_level == 0 else ""))

    shots: list[tuple[str, Image.Image]] = []
    for n, path in enumerate(made, 1):
        picture = trim(Image.open(path).convert("RGBA"))
        flat = Image.new("RGB", picture.size, BACKGROUND)
        flat.paste(picture, mask=picture.split()[3])
        shots.append((f"{n:02d}-town.png", letterbox(flat, SHOT)))

    plan = project / f"{name}_preview.png"
    if plan.exists():
        shots.append((f"{len(shots) + 1:02d}-plan.png",
                      letterbox(Image.open(plan).convert("RGB"), SHOT)))

    window = REPO / "docs" / "branding" / "window.png"
    if window.exists():
        shots.append((f"{len(shots) + 1:02d}-window.png",
                      letterbox(Image.open(window).convert("RGB"), SHOT)))

    for filename, picture in shots:
        picture.save(SHOTS / filename)
        print(f"wrote {SHOTS / filename}")

    preview = thumbnail(SHOTS / "02-town.png")
    preview.save(OUT / "preview.png")
    print(f"wrote {OUT / 'preview.png'}")

    for path in work.glob("*"):
        path.unlink()
    work.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
