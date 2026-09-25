#!/bin/bash
# Linux and macOS programs, built inside Docker so a Windows PC can produce them.
# The Mac file is a zip of KnoxMap.app. A dmg needs Apple's tools, which this
# machine does not have. Python for Mac is a relocatable build plus wheels;
# it is not run here.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y python3 python3-pip python3-venv python3-dev build-essential curl ca-certificates zip

rm -rf /tmp/build
mkdir -p /tmp/build /knoxmap/release
tar -C /knoxmap -cf - \
  --exclude=.git \
  --exclude=.venv \
  --exclude=node_modules \
  --exclude=pybuild \
  --exclude=release \
  --exclude=released \
  --exclude=output \
  --exclude=cache \
  --exclude=logs \
  --exclude=vendor \
  --exclude=KnoxMap/worlded/src \
  --exclude=KnoxMap/worlded/build \
  --exclude=__pycache__ \
  . | tar -C /tmp/build -xf -

cd /tmp/build
python3 -m venv /tmp/venv
/tmp/venv/bin/pip install --upgrade pip
/tmp/venv/bin/pip install -r KnoxMap/requirements.txt pyinstaller

echo "Packing the Linux Python server..."
/tmp/venv/bin/pyinstaller --noconfirm \
  --distpath desktop/pybuild --workpath /tmp/pyi-work \
  desktop/knoxmap-server.spec

cd /tmp/build/desktop
npm ci

echo "Packing the Linux program..."
node build.mjs --platform linux --arch x64
cp -f /tmp/build/release/KnoxMap-v*-linux.AppImage /knoxmap/release/

echo "Packing the Linux command line..."
/tmp/venv/bin/pyinstaller --noconfirm \
  --distpath /tmp/build/desktop/pybuild --workpath /tmp/pyi-work-cli \
  /tmp/build/desktop/knoxmap-cli.spec
ver=$(awk '/^## [0-9]/{print $2; exit}' /tmp/build/CHANGELOG.md)
cp -f /tmp/build/desktop/pybuild/knoxmap-cli "/knoxmap/release/KnoxMap-v${ver}-linux-cli"
chmod 755 "/knoxmap/release/KnoxMap-v${ver}-linux-cli"

echo "Packing the Mac Python tree..."
rm -rf /tmp/build/desktop/pybuild /tmp/build/desktop/out
dest=/tmp/build/desktop/pybuild/knoxmap-server
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
cp /tmp/build/CHANGELOG.md "$dest/"
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

echo "Packing the Mac program..."
node build.mjs --platform darwin --arch arm64
cp -f /tmp/build/release/KnoxMap-v*-macos.zip /knoxmap/release/
wrap=$(mktemp -d)
cat > "$wrap/knoxmap-cli" << 'EOF'
#!/bin/sh
DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
exec "$DIR/KnoxMap.app/Contents/Resources/python/knoxmap-cli" "$@"
EOF
chmod 755 "$wrap/knoxmap-cli"
for z in /knoxmap/release/KnoxMap-v*-macos.zip; do
  (cd "$wrap" && zip -u "$z" knoxmap-cli)
done
echo "Linux and Mac files are in release/"
