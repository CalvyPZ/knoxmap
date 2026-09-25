// Draws docs/branding/knoxmap.ico, .icns and .png from docs/branding/logo.svg.
// The mark is small enough to rasterise here: a rounded tile, a diamond
// stroke and a map pin. Run with: node desktop/make-icons.mjs
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const branding = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'docs', 'branding');
const desktop = path.dirname(fileURLToPath(import.meta.url));

const BG = [0x14, 0x18, 0x0f];
const GREEN = [0xa5, 0xe2, 0x66];

const pin = pinPolygon();

function pinPolygon() {
  const pts = [];
  const push = (x, y) => {
    const last = pts[pts.length - 1];
    if (!last || last[0] !== x || last[1] !== y) pts.push([x, y]);
  };
  for (const [x, y] of cubic([256, 382], [256, 382], [150, 262], [150, 188], 24)) push(x, y);
  for (let i = 1; i <= 32; i++) {
    const a = Math.PI * (1 - i / 32);
    push(256 + 106 * Math.cos(a), 188 - 106 * Math.sin(a));
  }
  for (const [x, y] of cubic([362, 188], [362, 262], [256, 382], [256, 382], 24)) push(x, y);
  return pts;
}

function cubic(p0, p1, p2, p3, n) {
  const out = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    const u = 1 - t;
    out.push([
      u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
      u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
    ]);
  }
  return out;
}

function insidePin(x, y) {
  const dx = x - 256;
  const dy = y - 190;
  if (dx * dx + dy * dy <= 40 * 40) return false;
  let inside = false;
  for (let i = 0, j = pin.length - 1; i < pin.length; j = i++) {
    const [xi, yi] = pin[i];
    const [xj, yj] = pin[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  let t = len2 === 0 ? 0 : ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  const qx = ax + t * dx;
  const qy = ay + t * dy;
  return Math.hypot(px - qx, py - qy);
}

const DIAMOND = [[256, 300], [420, 382], [256, 464], [92, 382]];

function onDiamond(x, y) {
  for (let i = 0; i < DIAMOND.length; i++) {
    const [ax, ay] = DIAMOND[i];
    const [bx, by] = DIAMOND[(i + 1) % DIAMOND.length];
    if (distToSegment(x, y, ax, ay, bx, by) <= 11) return true;
  }
  return false;
}

function sdRoundRect(px, py) {
  const x = Math.abs(px - 256) - (256 - 96);
  const y = Math.abs(py - 256) - (256 - 96);
  return Math.hypot(Math.max(x, 0), Math.max(y, 0)) + Math.min(Math.max(x, y), 0) - 96;
}

function sample(x, y) {
  if (sdRoundRect(x, y) > 0) return null;
  if (insidePin(x, y) || onDiamond(x, y)) return GREEN;
  return BG;
}

function render(size) {
  const img = Buffer.alloc(size * size * 4);
  const n = size >= 512 ? 2 : 4;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let j = 0; j < n; j++) {
        for (let i = 0; i < n; i++) {
          const sx = ((x + (i + 0.5) / n) / size) * 512;
          const sy = ((y + (j + 0.5) / n) / size) * 512;
          const color = sample(sx, sy);
          if (!color) continue;
          r += color[0];
          g += color[1];
          b += color[2];
          a += 1;
        }
      }
      const o = (y * size + x) * 4;
      const samples = n * n;
      img[o] = Math.round(r / samples);
      img[o + 1] = Math.round(g / samples);
      img[o + 2] = Math.round(b / samples);
      img[o + 3] = Math.round(255 * a / samples);
    }
  }
  return img;
}

function crc32(buf) {
  let c = ~0;
  for (let i = 0; i < buf.length; i++) {
    c ^= buf[i];
    for (let k = 0; k < 8; k++) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
  }
  return ~c >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, 4, 'ascii');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, crc]);
}

function png(size, rgba) {
  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y++) {
    raw[y * (stride + 1)] = 0;
    rgba.copy(raw, y * (stride + 1) + 1, y * stride, y * stride + stride);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

function ico(images) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);
  const entries = [];
  let offset = 6 + 16 * images.length;
  const parts = [header];
  for (const { size, pngBytes } of images) {
    const entry = Buffer.alloc(16);
    entry[0] = size >= 256 ? 0 : size;
    entry[1] = size >= 256 ? 0 : size;
    entry.writeUInt16LE(1, 4);
    entry.writeUInt16LE(32, 6);
    entry.writeUInt32LE(pngBytes.length, 8);
    entry.writeUInt32LE(offset, 12);
    entries.push(entry);
    offset += pngBytes.length;
  }
  return Buffer.concat([header, ...entries, ...images.map((img) => img.pngBytes)]);
}

function icns(images) {
  const types = {
    16: 'icp4', 32: 'icp5', 64: 'icp6', 128: 'ic07', 256: 'ic08',
    512: 'ic09', 1024: 'ic10',
  };
  const chunks = [];
  for (const { size, pngBytes } of images) {
    const type = types[size];
    if (!type) continue;
    const head = Buffer.alloc(8);
    head.write(type, 0, 4, 'ascii');
    head.writeUInt32BE(8 + pngBytes.length, 4);
    chunks.push(head, pngBytes);
  }
  const body = Buffer.concat(chunks);
  const head = Buffer.alloc(8);
  head.write('icns', 0, 4, 'ascii');
  head.writeUInt32BE(8 + body.length, 4);
  return Buffer.concat([head, body]);
}

const sizes = [16, 32, 48, 64, 128, 256, 512, 1024];
const rendered = sizes.map((size) => {
  const pngBytes = png(size, render(size));
  return { size, pngBytes };
});

fs.mkdirSync(branding, { recursive: true });
const png512 = rendered.find((img) => img.size === 512).pngBytes;
fs.writeFileSync(path.join(branding, 'knoxmap.png'), png512);
fs.writeFileSync(path.join(desktop, 'icon.png'), png512);
fs.writeFileSync(path.join(branding, 'knoxmap.ico'), ico(
  rendered.filter((img) => [16, 32, 48, 64, 128, 256].includes(img.size)),
));
fs.writeFileSync(path.join(branding, 'knoxmap.icns'), icns(
  rendered.filter((img) => [16, 32, 64, 128, 256, 512, 1024].includes(img.size)),
));
console.log('wrote docs/branding/knoxmap.png, .ico, .icns and desktop/icon.png');
