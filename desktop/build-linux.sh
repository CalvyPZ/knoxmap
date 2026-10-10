#!/bin/bash
# Linux program, built inside Docker so a Windows PC can produce it.
# Writes KnoxMap-v…-linux.AppImage and KnoxMap-v…-linux-cli into releases/.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-pip python3-venv python3-dev build-essential curl ca-certificates

# shellcheck disable=SC1091
. /knoxmap/desktop/stage-tree.sh
stage_tree

cd /tmp/build
if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
  # shellcheck disable=SC1091
  . "$HOME/.cargo/env"
fi
echo "Building the Linux helper library..."
bash desktop/build-native.sh

python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --upgrade pip
/tmp/venv/bin/pip install -r KnoxMap/requirements.txt pyinstaller

echo "Packing the Linux Python server..."
/tmp/venv/bin/pyinstaller --noconfirm \
  --distpath releases/temp --workpath releases/temp/work \
  desktop/knoxmap-server.spec

cd /tmp/build/desktop
npm ci

echo "Packing the Linux program..."
node build.mjs --platform linux --arch x64
cp -f /tmp/build/releases/KnoxMap-v*-linux.AppImage /knoxmap/releases/

echo "Packing the Linux command line..."
/tmp/venv/bin/pyinstaller --noconfirm \
  --distpath /tmp/build/releases/temp --workpath /tmp/build/releases/temp/work-cli \
  /tmp/build/desktop/knoxmap-cli.spec
ver=$(awk '/^## [0-9]/{gsub(/\r/, ""); print $2; exit}' /tmp/build/docs/CHANGELOG.md)
cp -f /tmp/build/releases/temp/knoxmap-cli "/knoxmap/releases/KnoxMap-v${ver}-linux-cli"
chmod 755 "/knoxmap/releases/KnoxMap-v${ver}-linux-cli"
echo "Linux files are in releases/"
