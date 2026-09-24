"""Write lang/english.txt: every line of the window, ready to be translated.

    python tools/make_lang_template.py

Anyone can put KnoxMap in their language without touching the code: copy
lang/english.txt to lang/<their language>.txt, translate the right-hand side
of each line, and pick the language at the top of the window. The name of the
file is the name in the menu, so russian.txt appears as "Russian".

The English side is what the window actually says, so this is generated rather
than typed: the text of every element in templates/index.html, its
placeholders and titles, and the messages static/js writes into the page. A
line whose translation is left as the English is simply not translated, which
is what an untouched template does.
"""
from __future__ import annotations

import html as html_mod
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "templates", "index.html")
SCRIPTS = (os.path.join(ROOT, "static", "js", "app.js"),
           os.path.join(ROOT, "static", "js", "fx.js"))
OUT = os.path.join(ROOT, "lang", "english.txt")

HEADER = """\
# KnoxMap in your language
#
# Copy this file, name it after the language - russian.txt, deutsch.txt,
# turkce.txt - and translate the right of each "=" only. Leave the left alone:
# that is what KnoxMap says in English, and how it finds your translation.
# Whatever is still in English is shown in English, so a half-finished file
# works. Lines starting with # are notes and are ignored.
#
# Then start KnoxMap and pick the language at the top of the window.
#
# Written by tools/make_lang_template.py from the window itself - run it again
# after a KnoxMap update to pick up anything new.
"""

# Text that is not worth translating: names, symbols and numbers.
SKIP = {"KnoxMap", "discord", "OpenStreetMap", "Leaflet", "×", "→", "?"}
# Anything that is code rather than words: a selector, a path, a template.
CODE = re.compile(r"^[#./]|[\[\]{}<>$`|]|https?:|^\w+-\w+$")


def is_words(text: str) -> bool:
    """Whether this looks like something a person reads, not code."""
    if len(text) < 3 or CODE.search(text):
        return False
    if not re.search(r"[A-Za-z]{2}", text):
        return False
    # A lone lower-case word or two is a key or a class name, not a message.
    words = text.split()
    return len(words) >= 3 or text[0].isupper() or text[0].isdigit()


def page_strings(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", "", text, flags=re.S)
    found = []
    for piece in re.split(r"<[^>]+>", body):
        piece = " ".join(html_mod.unescape(piece).split())
        if "{{" not in piece and is_words(piece):
            found.append(piece)
    for attr in re.findall(r'(?:placeholder|title|aria-label)="([^"]+)"', text):
        attr = html_mod.unescape(attr)
        if "{{" not in attr and is_words(attr):
            found.append(attr)
    return found


def script_strings(paths) -> list[str]:
    """Sentences the scripts put on the page.

    Only plain quoted text of a few words: enough to catch the notes, the
    buttons and the settings, without dragging in every css class and key.
    """
    found = []
    for path in paths:
        text = open(path, encoding="utf-8").read()
        for quote in ("'", '"'):
            for piece in re.findall(rf"{quote}([^{quote}\\\n]{{6,220}}){quote}", text):
                piece = piece.strip()
                if is_words(piece) and re.search(r"[A-Za-z]{2,}\s+[A-Za-z]", piece):
                    found.append(piece)
    return found


def main() -> int:
    strings = page_strings(PAGE) + script_strings(SCRIPTS)
    seen, lines = set(), []
    for text in strings:
        if text in SKIP or text in seen or "=" in text:
            continue                   # "=" is the separator; skip those
        seen.add(text)
        lines.append(f"{text} = {text}")
    lines.sort(key=str.lower)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n" + "\n".join(lines) + "\n")
    print(f"wrote {OUT} with {len(lines)} lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
