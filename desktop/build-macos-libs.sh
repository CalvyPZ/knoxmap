#!/bin/bash
# The macOS helper library and map compiler, cross-built from Linux. The
# image is ghcr.io/rust-cross/cargo-zigbuild, which carries the macOS SDK.
# KnoxMap's Mac download is arm64, so this is aarch64-apple-darwin.
# Both binaries are copied out of target/ so the Mac pack can take a clean
# tree and still drop them into the app.
set -euo pipefail
rustup target add aarch64-apple-darwin
cargo zigbuild --release --target aarch64-apple-darwin --manifest-path Helpers/Cargo.toml -p helper
cargo zigbuild --release --target aarch64-apple-darwin --manifest-path compiler/Cargo.toml
mkdir -p desktop/native-macos
cp -f Helpers/target/aarch64-apple-darwin/release/libhelper.dylib desktop/native-macos/
cp -f compiler/target/aarch64-apple-darwin/release/knox-compile desktop/native-macos/
ls -l desktop/native-macos
