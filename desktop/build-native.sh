#!/bin/bash
# Build the native library and the map compiler for this machine.
# Windows writes Helpers/target/release/helper.dll and
# compiler/target/release/knox-compile.exe. Linux and macOS write the
# same pair without the .exe. The packager bundles both.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
if ! command -v cargo >/dev/null 2>&1; then
  echo "Rust is not installed. Install it from https://rustup.rs and pack again."
  exit 1
fi
echo "Building helper..."
cargo build --release --manifest-path "${ROOT}/Helpers/Cargo.toml" -p helper
echo "Building the map compiler..."
cargo build --release --manifest-path "${ROOT}/compiler/Cargo.toml"
