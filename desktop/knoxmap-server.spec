# -*- mode: python ; coding: utf-8 -*-
# The Python half of the single program file. Electron starts knoxmap-server;
# this folder is copied in beside the window as resources/python.
# Build with:
#   pyinstaller --noconfirm --distpath releases/temp --workpath releases/temp/work desktop/knoxmap-server.spec
import os

REPO = os.path.abspath(os.path.join(SPECPATH, ".."))
ROOT = os.path.join(REPO, "KnoxMap")


def py_modules(folder, prefix, skip=()):
    found = []
    base = os.path.join(ROOT, folder)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py") or name in skip:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), base)
            stem = rel[:-3].replace(os.sep, ".")
            if stem == "__init__" or stem.endswith(".__init__"):
                stem = stem[: -len(".__init__")] if stem != "__init__" else ""
            found.append(prefix if not stem else prefix + "." + stem)
    return found


hidden = []
hidden += py_modules("generator", "generator")
hidden += py_modules("knoxbuild", "knoxbuild")
hidden += py_modules("tools", "tools", skip=("selftest.py",))

datas = []
for folder in ("templates", "static", "lang"):
    datas.append((os.path.join(ROOT, folder), folder))
datas.append((os.path.join(REPO, "CHANGELOG.md"), "."))
lua = os.path.join(ROOT, "knoxbuild", "lua")
if os.path.isdir(lua):
    datas.append((lua, os.path.join("knoxbuild", "lua")))
worlded = os.path.join(ROOT, "worlded")
for name in os.listdir(worlded):
    if name.endswith(".py"):
        datas.append((os.path.join(worlded, name), "worlded"))

a = Analysis(
    [os.path.join(ROOT, "knoxmap.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["webview", "pywebview", "clr", "pythonnet",
              "PyQt5", "PyQt6", "PySide2", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="knoxmap-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="knoxmap-server",
)
