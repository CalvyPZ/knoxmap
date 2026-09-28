"""KnoxMap from the command line.

No window is opened. Maps and logs are written beside this program, the same
folder the window uses when both files sit together.

    knoxmap build output/mytown --preset city
    knoxmap compile output/mytown
    knoxmap install output/mytown --name "My Town, KY" --id mytown
    knoxmap render-ground output/mytown street.png 300 300 40 40
    knoxmap render-lots output/mytown view.png 0 0 100 100
    knoxmap audit 400
    knoxmap validate output/mytown
"""
from __future__ import annotations

import multiprocessing
import sys

# (help, module, how that module reads argv)
# "args"  — argparse, the list is the arguments with no program name
# "argv"  — the list includes a program name in front
COMMANDS = {
    "build": ("furnish the buildings in a generated map", "knoxbuild.build", "args"),
    "compile": ("compile a map with the map compiler", "tools.compile_map", "args"),
    "install": ("package a compiled map as a Project Zomboid mod", "tools.make_map_mod", "args"),
    "render-ground": ("draw a map's ground from the game's tiles", "tools.render_ground", "argv"),
    "render-lots": ("draw a compiled map the way the game draws it", "tools.render_lots", "argv"),
    "audit": ("stress-test floor plans", "tools.audit_layouts", "argv"),
    "validate": ("check building files against the editor's rules", "tools.validate_tbx", "argv"),
}


def _help() -> None:
    print("KnoxMap command line. This does not open a window.\n")
    print("usage: knoxmap <command> [arguments]\n")
    print("commands:")
    for name, (help_line, _module, _kind) in COMMANDS.items():
        print(f"  {name:<16} {help_line}")
    print("\nknoxmap <command> --help   that command's own arguments")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _help()
        return 0
    command, *rest = argv
    found = COMMANDS.get(command)
    if found is None:
        print(f"unknown command: {command}\n", file=sys.stderr)
        _help()
        return 2
    _help_line, module_name, kind = found
    import importlib
    module = importlib.import_module(module_name)
    passed = rest if kind == "args" else [command, *rest]
    return int(module.main(passed) or 0)


if __name__ == "__main__":
    # A frozen Windows build re-enters this file in each worker. This has to
    # run before main(), and only under the guard, or those workers start the app.
    multiprocessing.freeze_support()
    raise SystemExit(main())
