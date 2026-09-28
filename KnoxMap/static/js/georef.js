// Shared georeferencing for the paint preview and the Edit map.

const PaintImage = L.Layer.extend({
  initialize(url, corners, onload, pane) {
    this._url = url;
    this._onload = onload || null;
    this._paneName = pane || 'paint';
    this._topLeft = L.latLng(corners[0][0], corners[0][1]);
    this._topRight = L.latLng(corners[1][0], corners[1][1]);
    this._bottomLeft = L.latLng(corners[3][0], corners[3][1]);
  },
  onAdd(map) {
    this._map = map;
    if (!map.getPane(this._paneName)) {
      const pane = map.createPane(this._paneName);
      pane.style.zIndex = this._paneName === 'paint' ? '350' : '460';
      pane.style.pointerEvents = 'none';
    }
    this._image = L.DomUtil.create('img', 'paint-piece');
    this._image.alt = '';
    const reveal = () => { if (this._onload) this._onload(); };
    this._image.onload = reveal;
    this._image.src = this._url;
    if (this._image.complete && this._image.naturalWidth) reveal();
    this._image.style.position = 'absolute';
    this._image.style.width = '1px';
    this._image.style.height = '1px';
    this._image.style.transformOrigin = '0 0';
    map.getPane(this._paneName).appendChild(this._image);
    map.on('viewreset zoom move', this._reset, this);
    this._reset();
  },
  onRemove(map) {
    map.off('viewreset zoom move', this._reset, this);
    if (this._image) L.DomUtil.remove(this._image);
  },
  setUrl(url) {
    this._url = url;
    if (this._image) this._image.src = url;
  },
  _reset() {
    if (!this._map || !this._image) return;
    const topLeft = this._map.latLngToLayerPoint(this._topLeft);
    const topRight = this._map.latLngToLayerPoint(this._topRight);
    const bottomLeft = this._map.latLngToLayerPoint(this._bottomLeft);
    const vx = topRight.subtract(topLeft);
    const vy = bottomLeft.subtract(topLeft);
    this._image.style.transform =
      `matrix(${vx.x}, ${vx.y}, ${vy.x}, ${vy.y}, ${topLeft.x}, ${topLeft.y})`;
  },
});

window.PaintImage = PaintImage;

// A game cell is 300 tiles on a side.
const CELL_SIZE = 300;

function cross(a, b) {
  return a.x * b.y - a.y * b.x;
}

function sub(a, b) {
  return { x: a.x - b.x, y: a.y - b.y };
}

function cornerXY(corner) {
  if (!corner || corner.length < 2) return null;
  const lat = +corner[0];
  const lon = +corner[1];
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  return { x: lon, y: lat };
}

function quadArea(p00, p10, p11, p01) {
  return 0.5 * (
    cross(p00, p10) + cross(p10, p11) + cross(p11, p01) + cross(p01, p00)
  );
}

function bilinear(p00, p10, p11, p01, u, v) {
  const su = 1 - u;
  const sv = 1 - v;
  return {
    x: su * sv * p00.x + u * sv * p10.x + u * v * p11.x + su * v * p01.x,
    y: su * sv * p00.y + u * sv * p10.y + u * v * p11.y + su * v * p01.y,
  };
}

function uvError(p00, p10, p11, p01, q, uv) {
  const p = bilinear(p00, p10, p11, p01, uv.u, uv.v);
  return Math.hypot(p.x - q.x, p.y - q.y);
}

function newtonUv(A, B, C, D, u, v, span) {
  if (!Number.isFinite(u) || !Number.isFinite(v)) return null;
  for (let i = 0; i < 12; i++) {
    const fx = u * A.x + v * B.x + u * v * C.x - D.x;
    const fy = u * A.y + v * B.y + u * v * C.y - D.y;
    if (fx * fx + fy * fy <= 1e-28) return { u, v };
    const dux = A.x + v * C.x;
    const duy = A.y + v * C.y;
    const dvx = B.x + u * C.x;
    const dvy = B.y + u * C.y;
    const det = dux * dvy - duy * dvx;
    if (!(Math.abs(det) > span * span * 1e-18)) return { u, v };
    u -= (fx * dvy - fy * dvx) / det;
    v -= (dux * fy - duy * fx) / det;
    if (!Number.isFinite(u) || !Number.isFinite(v)) return null;
  }
  return { u, v };
}

function bilinearRoots(A, B, C, D, span) {
  const a = cross(B, C);
  const b = cross(B, A) - cross(D, C);
  const c = -cross(D, A);
  const roots = [];
  const pushV = (v) => {
    if (!Number.isFinite(v)) return;
    const E = { x: A.x + v * C.x, y: A.y + v * C.y };
    const F = { x: D.x - v * B.x, y: D.y - v * B.y };
    const useX = Math.abs(E.x) >= Math.abs(E.y);
    const div = useX ? E.x : E.y;
    if (!(Math.abs(div) > span * 1e-15)) return;
    const u = (useX ? F.x : F.y) / div;
    if (Number.isFinite(u)) roots.push({ u, v });
  };
  if (Math.abs(a) <= span * span * 1e-14) {
    if (Math.abs(b) > span * span * 1e-16) pushV(-c / b);
    return roots;
  }
  let disc = b * b - 4 * a * c;
  if (disc < 0) {
    if (disc > -(span ** 4) * 1e-10) disc = 0;
    else return roots;
  }
  const root = Math.sqrt(disc);
  pushV((-b + root) / (2 * a));
  if (root !== 0) pushV((-b - root) / (2 * a));
  return roots;
}

// Inverse of the bilinear corner map. A parallelogram (C ~ 0) is affine:
// exact for rotation 0 when the corners are the grid bbox, and for a rotated
// rectangle treated as flat in lon/lat.
function solveUv(p00, p10, p11, p01, q) {
  const A = sub(p10, p00);
  const B = sub(p01, p00);
  const C = {
    x: p11.x - p10.x - p01.x + p00.x,
    y: p11.y - p10.y - p01.y + p00.y,
  };
  const D = sub(q, p00);
  const span = Math.hypot(A.x, A.y) + Math.hypot(B.x, B.y);
  if (!(span > 0)) return null;
  const den = cross(A, B);
  if (!(Math.abs(den) > span * span * 1e-16)) return null;
  const affine = {
    u: cross(D, B) / den,
    v: cross(A, D) / den,
  };
  if (!Number.isFinite(affine.u) || !Number.isFinite(affine.v)) return null;
  if (Math.hypot(C.x, C.y) <= span * 1e-12) return affine;

  const pool = bilinearRoots(A, B, C, D, span);
  pool.push(affine);
  let best = null;
  let bestErr = Infinity;
  for (const cand of pool) {
    const polished = newtonUv(A, B, C, D, cand.u, cand.v, span);
    if (!polished) continue;
    const err = uvError(p00, p10, p11, p01, q, polished);
    if (err < bestErr) {
      bestErr = err;
      best = polished;
    }
  }
  if (!best || bestErr > Math.max(1e-9, span * 1e-6)) return null;
  return best;
}

// grid: Projector.grid_dict() —
//   { min_x_m, min_y_m, width_tiles, height_tiles, meters_per_tile, epsg, rotation }
// corners: [[lat, lon] × 4] in order NW, NE, SE, SW (app.py _paint_corners).
//
// Returns { latLngToTile, tileToLatLng, cellOf }, or null if the corner quad
// is degenerate. Tile pixels run 0..width, 0..height with y downward.
// lat/lon maps through the corner quad (inverse bilinear). When that quad is
// a parallelogram the uv term drops out and the map is affine, which is exact
// for rotation 0 when the corners are the grid bbox.
function attachGrid(grid, corners) {
  if (!grid || !corners || corners.length < 4) return null;
  const width = +grid.width_tiles;
  const height = +grid.height_tiles;
  if (!(width > 0) || !(height > 0)) return null;
  const p00 = cornerXY(corners[0]);
  const p10 = cornerXY(corners[1]);
  const p11 = cornerXY(corners[2]);
  const p01 = cornerXY(corners[3]);
  if (!p00 || !p10 || !p11 || !p01) return null;
  if (!(Math.abs(quadArea(p00, p10, p11, p01)) > 1e-18)) return null;

  return {
    latLngToTile(lat, lon) {
      const qlat = +lat;
      const qlon = +lon;
      if (!Number.isFinite(qlat) || !Number.isFinite(qlon)) return null;
      const uv = solveUv(p00, p10, p11, p01, { x: qlon, y: qlat });
      if (!uv) return null;
      return { x: uv.u * width, y: uv.v * height };
    },
    tileToLatLng(x, y) {
      const px = +x;
      const py = +y;
      if (!Number.isFinite(px) || !Number.isFinite(py)) return null;
      const p = bilinear(p00, p10, p11, p01, px / width, py / height);
      return { lat: p.y, lon: p.x };
    },
    cellOf(x, y) {
      const px = +x;
      const py = +y;
      if (!Number.isFinite(px) || !Number.isFinite(py)) return null;
      return { cx: Math.floor(px / CELL_SIZE), cy: Math.floor(py / CELL_SIZE) };
    },
  };
}

window.KnoxGrid = { attach: attachGrid };
