#!/bin/bash
# macOS program, built inside Docker so a Windows PC can produce it.
# The file is a zip of KnoxMap.app. A dmg needs Apple's tools, which this
# machine does not have. Python for Mac is a relocatable build plus wheels.
# The arm64 helper library must already be in desktop/native-macos.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-pip python3-venv python3-dev curl ca-certificates zip

# shellcheck disable=SC1091
. /knoxmap/desktop/stage-tree.sh
stage_tree

cd /tmp/build
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --upgrade pip

echo "Packing the Mac Python tree..."
dest=/tmp/build/releases/temp/knoxmap-server
mkdir -p "$dest"
url=$(node << 'JS'
const https = require('https');
const opts = {
  headers: { 'User-Agent': 'knoxmap-build', Accept: 'application/vnd.github+json' },
};
https.get('https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest', opts, (res) => {
  let body = '';
  res.on('data', (chunk) => { body += chunk; });
  res.on('end', () => {
    const data = JSON.parse(body);
    const asset = (data.assets || []).find((item) =>
      /^cpython-3\.12\.\d+(?:\+\d+)?-aarch64-apple-darwin-install_only\.tar\.gz$/.test(item.name));
    if (!asset) {
      console.error('no macOS Python build in the latest release');
      process.exit(1);
    }
    process.stdout.write(asset.browser_download_url);
  });
}).on('error', (err) => {
  console.error(err);
  process.exit(1);
});
JS
)
curl -fsSL "$url" -o /tmp/macpython.tar.gz
tar -xzf /tmp/macpython.tar.gz -C "$dest"
site=$(echo "$dest"/python/lib/python3.*/site-packages)
mkdir -p "$site"
/tmp/venv/bin/pip install --target "$site" \
  --platform macosx_11_0_arm64 \
  --python-version 3.12 \
  --implementation cp \
  --abi cp312 \
  --only-binary=:all: \
  -r /tmp/build/KnoxMap/requirements.txt

for name in knoxmap.py knoxmap_cli.py app.py knoxpaths.py knoxlog.py updater.py knoxmap_setup.py knoxstop.py; do
  cp "/tmp/build/KnoxMap/$name" "$dest/"
done
cp /tmp/build/docs/CHANGELOG.md "$dest/CHANGELOG.md"
cp -a /tmp/build/KnoxMap/generator /tmp/build/KnoxMap/knoxbuild /tmp/build/KnoxMap/tools \
  /tmp/build/KnoxMap/templates /tmp/build/KnoxMap/static /tmp/build/KnoxMap/lang "$dest/"
mkdir -p "$dest/worlded"
cp /tmp/build/KnoxMap/worlded/*.py "$dest/worlded/"
cat > "$dest/knoxmap-server" << 'EOF'
#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
SITE=$(echo "$ROOT"/python/lib/python3.*/site-packages)
export PYTHONNOUSERSITE=1
export PYTHONPATH="$ROOT:$SITE${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/python/bin/python3" "$ROOT/knoxmap.py" "$@"
EOF
chmod 755 "$dest/knoxmap-server"
cat > "$dest/knoxmap-cli" << 'EOF'
#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
SITE=$(echo "$ROOT"/python/lib/python3.*/site-packages)
export PYTHONNOUSERSITE=1
export PYTHONPATH="$ROOT:$SITE${PYTHONPATH:+:$PYTHONPATH}"
exec "$ROOT/python/bin/python3" "$ROOT/knoxmap_cli.py" "$@"
EOF
chmod 755 "$dest/knoxmap-cli"
src=/tmp/build/desktop/native-macos/libhelper.dylib
if [ ! -f "$src" ]; then
  echo "Missing the macOS library ${src}."
  echo "build_macos.bat builds it before this script runs."
  exit 1
fi
cp -f "$src" "$dest/libhelper.dylib"
src_compile=/tmp/build/desktop/native-macos/knox-compile
if [ ! -f "$src_compile" ]; then
  echo "Missing the macOS compiler ${src_compile}."
  echo "build_macos.bat builds it before this script runs."
  exit 1
fi
cp -f "$src_compile" "$dest/knox-compile"
chmod 755 "$dest/knox-compile"
mkdir -p "$dest/compiler"
tar -C /tmp/build/compiler -cf - --exclude=target . | tar -C "$dest/compiler" -xf -

cd /tmp/build/desktop
npm ci

echo "Packing the Mac program..."
node build.mjs --platform darwin --arch arm64
cp -f /tmp/build/releases/KnoxMap-v*-macos.zip /knoxmap/releases/
wrap=$(mktemp -d)
cat > "$wrap/knoxmap-cli" << 'EOF'
#!/bin/sh
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec "$DIR/KnoxMap.app/Contents/Resources/python/knoxmap-cli" "$@"
EOF
chmod 755 "$wrap/knoxmap-cli"
for z in /knoxmap/releases/KnoxMap-v*-macos.zip; do
  (cd "$wrap" && zip -u "$z" knoxmap-cli)
done
echo "Mac files are in releases/"
