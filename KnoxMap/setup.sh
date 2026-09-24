#!/usr/bin/env bash
# KnoxMap setup on Linux or macOS. Safe to run again.
#
# The same as Setup.bat: a private Python environment beside this file, the PZ
# Mapping Tools, the patched map compiler, and the tile artwork from your own
# copy of the game. The tools are Windows programs; here they run under Wine,
# which a PC that plays Project Zomboid through Proton already has. See
# docs/LINUX.md.
set -euo pipefail
cd "$(dirname "$0")"

# UTF-8 for every file Python reads and writes, whatever the locale is.
export PYTHONUTF8=1

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10) or sys.maxsize <= 2**32)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "KnoxMap needs 64-bit Python 3.10 or newer."
  echo "Install it with your package manager, for example:"
  echo "    sudo apt install python3 python3-venv python3-pip     # Debian, Ubuntu"
  echo "    sudo pacman -S python                                 # Arch"
  echo "    sudo dnf install python3                              # Fedora"
  exit 1
fi

# An environment built by a 32-bit Python stays 32-bit, and a town-sized map
# needs more memory than one can address.
if [ -x ".venv/bin/python" ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.maxsize <= 2**32)' 2>/dev/null; then
  echo "The Python environment is 32-bit, which runs out of memory on a big map."
  echo "Making it again with 64-bit Python..."
  rm -rf .venv
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating the Python environment..."
  "$PY" -m venv .venv
fi
echo "Installing Python packages..."
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

# Both of these are worth saying before the long download, not after it.
if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ] &&
   ! command -v wine >/dev/null 2>&1 && ! command -v wine64 >/dev/null 2>&1; then
  echo
  echo "Note: wine was not found. Everything works except Compile, which on"
  echo "this system runs the map tools' Windows build. Install wine, or build"
  echo "the tools for this system and put them in vendor/PZMappingTools/bin -"
  echo "see docs/LINUX.md. (On 64-bit Linux the compiler is fetched for you.)"
  echo
fi

# The window is Electron. A normal desktop already has the libraries it needs.
# A very small install may not, and then KnoxMap opens in the browser instead.
if [ "$(uname -s)" = "Linux" ] && command -v ldconfig >/dev/null 2>&1; then
  if ! ldconfig -p 2>/dev/null | grep -q 'libnss3\.so'; then
    echo
    echo "Note: the window needs the usual desktop libraries (libnss3, libgtk-3,"
    echo "libasound2). Install them with your package manager if the window does"
    echo "not open. KnoxMap still opens in your browser without them. See docs/LINUX.md."
    echo
  fi
fi

.venv/bin/python knoxmap_setup.py
echo
echo "Start KnoxMap with ./knoxmap.sh"
