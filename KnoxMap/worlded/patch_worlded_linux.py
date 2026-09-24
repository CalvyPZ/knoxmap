"""Make PZWorldEd build with GCC and Qt 5.15, so Linux needs no Wine.

Upstream builds and tests on Windows with MSVC and Qt 5.14.2; its own
BUILDING.md calls the Linux flow "intended" rather than tested. It very nearly
builds as it stands - one construct in one file stops it, and this fixes that
one:

    QPolygonF({QPointF(10, 20), QPointF(140, 20)})

Two points in braces are as good a match for `QPolygonF(const QRectF &)` -
QRectF takes a top-left and a bottom-right - as they are for the list of
points that was meant, so GCC calls it ambiguous and stops. MSVC let it
through. Saying which container it is resolves it, and means the same thing
on every compiler:

    QPolygonF(QVector<QPointF>{QPointF(10, 20), QPointF(140, 20)})

Run it after patch_worlded_cli.py, on the same tree:

    python patch_worlded_linux.py <path to PZ_Mapping_Tools source>
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

# The construct, and what it becomes. Written over several lines in places, so
# the whitespace between the bracket and the brace is part of the match.
AMBIGUOUS = re.compile(r"QPolygonF\((\s*)\{")
RESOLVED = r"QPolygonF(\1QVector<QPointF>{"

MARK = "// Modified by KnoxMap (https://github.com/spytheeuclidean-a11y/knoxmap): "


def _mark_modified(path: Path, what: str) -> None:
    """Say at the top of the file that it is not upstream's any more, which
    the GPL asks of a changed file."""
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return
    path.write_text(f"{MARK}{what}\n{text}", encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 1
    root = Path(argv[1])
    if not root.is_dir():
        print(f"{root} is not a folder", file=sys.stderr)
        return 2

    # Wherever it appears, rather than one named file: the same line is easy
    # to write again, and a build that stops on the twenty-first one has
    # taught nobody anything.
    sources = sorted(p for p in (root / "WorldEd" / "src").rglob("*.cpp")
                     if AMBIGUOUS.search(p.read_text(encoding="utf-8", errors="replace")))
    if not sources:
        print("nothing to fix - already patched, or upstream has changed it")
        return 0

    for path in sources:
        text = path.read_text(encoding="utf-8")
        fixed, count = AMBIGUOUS.subn(RESOLVED, text)
        backup = path.with_suffix(".cpp.linux-orig")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text(fixed, encoding="utf-8")
        _mark_modified(path, "named the container in QPolygonF's braced "
                             "initialiser, which GCC finds ambiguous.")
        print(f"patched {count} in {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
