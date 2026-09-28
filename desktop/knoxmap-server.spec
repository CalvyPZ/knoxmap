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
datas.append((os.path.join(REPO, "docs", "CHANGELOG.md"), "."))
lua = os.path.join(ROOT, "knoxbuild", "lua")
if os.path.isdir(lua):
    datas.append((lua, os.path.join("knoxbuild", "lua")))
worlded = os.path.join(ROOT, "worlded")
for name in os.listdir(worlded):
    if name.endswith(".py"):
        datas.append((os.path.join(worlded, name), "worlded"))


def bundle_osmium():
    # The map reader is pyosmium. Its extension and the libraries next to it
    # have to be inside this program; generating a map does not launch osmium.
    from PyInstaller.utils.hooks import (
        collect_data_files, collect_dynamic_libs, collect_submodules,
    )
    bins = collect_dynamic_libs("osmium")
    data = collect_data_files("osmium")
    mods = collect_submodules("osmium")
    try:
        from PyInstaller.utils.hooks import collect_delvewheel_libs_directory
        more_data, more_bins = collect_delvewheel_libs_directory("osmium")
        data += more_data
        bins += more_bins
    except Exception:
        pass
    return bins, data, mods


def bundle_duckdb():
    # Gap filling reads Overture's GeoParquet through DuckDB. The import sits
    # inside a function, so a frozen build leaves it out unless it is named.
    from PyInstaller.utils.hooks import (
        collect_data_files, collect_dynamic_libs, collect_submodules,
    )
    return (collect_dynamic_libs("duckdb"),
            collect_data_files("duckdb"),
            collect_submodules("duckdb"))


osmium_bins, osmium_data, osmium_mods = bundle_osmium()
hidden += osmium_mods
datas += osmium_data
duckdb_bins, duckdb_data, duckdb_mods = bundle_duckdb()
hidden += duckdb_mods
hidden.append("duckdb")
datas += duckdb_data
bins = osmium_bins + duckdb_bins
# SciPy's hooks collect the rest of the package. These are reached through
# a lazy or private name, so a frozen build misses them unless named here.
hidden += [
    "scipy.ndimage",
    "scipy.spatial",
    "scipy.spatial._ckdtree",
    "scipy._lib.messagestream",
]

a = Analysis(
    [os.path.join(ROOT, "knoxmap.py")],
    pathex=[ROOT],
    binaries=bins,
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
