// Packs KnoxMap into the one file a player downloads.
//
//   node build.mjs
//   node build.mjs --platform linux --arch arm64
//
// Windows comes out as KnoxMap.exe (portable). Linux comes out as
// KnoxMap.AppImage. macOS comes out as KnoxMap.dmg on a Mac, or a zip of
// KnoxMap.app when the packager is not a Mac (a dmg needs Apple's tools).
// The finished file is also copied to releases/ at the top of the repository.
// The Python server must already be at releases/temp/knoxmap-server,
// built on this same system:
//   pyinstaller --noconfirm --distpath releases/temp --workpath releases/temp/work desktop/knoxmap-server.spec
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '..');

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0 && process.argv[i + 1]) return process.argv[i + 1];
  return fallback;
}

function changelogVersion() {
  try {
    const text = fs.readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8');
    const found = text.match(/^##\s+(\d[\w.]*)/m);
    if (found) return found[1];
  } catch (_) { /* package.json's version is enough */ }
  return '0';
}

function appVersion() {
  const parts = changelogVersion().split('.');
  while (parts.length < 3) parts.push('0');
  return parts.slice(0, 3).join('.');
}

const hostPlatform = { win32: 'win32', linux: 'linux', darwin: 'darwin' }[process.platform];
const hostArch = process.arch === 'arm64' ? 'arm64' : 'x64';
const platform = arg('platform', hostPlatform);
const arch = arg('arch', hostArch);
const serverName = platform === 'win32' ? 'knoxmap-server.exe' : 'knoxmap-server';
const server = path.join(root, 'releases', 'temp', 'knoxmap-server', serverName);

if (!fs.existsSync(server)) {
  console.error(`Missing ${server}`);
  console.error('Build the Python server on this system first:');
  console.error('  pyinstaller --noconfirm --distpath releases/temp --workpath releases/temp/work desktop/knoxmap-server.spec');
  process.exit(1);
}

const target = { win32: '--win', linux: '--linux', darwin: '--mac' }[platform];
const archFlag = arch === 'arm64' ? '--arm64' : '--x64';
const crossMac = platform === 'darwin' && process.platform !== 'darwin';
if (!target) {
  console.error(`Unknown platform ${platform}`);
  process.exit(1);
}

const builderArgs = ['exec', '--', 'electron-builder', target, archFlag, '--publish', 'never',
  `--config.extraMetadata.version=${appVersion()}`];
if (crossMac) builderArgs.push('--config.mac.target=zip');

const result = spawnSync(
  'npm',
  builderArgs,
  {
    cwd: here,
    stdio: 'inherit',
    shell: true,
    env: { ...process.env, CSC_IDENTITY_AUTO_DISCOVERY: 'false' },
  },
);
if (result.status !== 0) process.exit(result.status || 1);

const ext = { win32: '.exe', linux: '.AppImage', darwin: crossMac ? '.zip' : '.dmg' }[platform];
const system = { win32: 'windows', linux: 'linux', darwin: 'macos' }[platform];
const outDir = path.join(root, 'releases', 'dist');
const made = fs.readdirSync(outDir)
  .filter((name) => name.endsWith(ext))
  .map((name) => ({ name, mtime: fs.statSync(path.join(outDir, name)).mtimeMs }))
  .sort((a, b) => b.mtime - a.mtime);
if (!made.length) {
  console.error(`Expected a ${ext} in releases/dist, found: ${fs.readdirSync(outDir).join(', ') || 'nothing'}`);
  process.exit(1);
}
const released = path.join(root, 'releases');
fs.mkdirSync(released, { recursive: true });
const dest = path.join(released, `KnoxMap-v${changelogVersion()}-${system}${ext}`);
fs.copyFileSync(path.join(outDir, made[0].name), dest);
console.log(`Built ${dest}`);
