// Knoxify — frontend.
//
// - Leaflet map with leaflet-draw for selecting the bbox.
// - The eraser uses those same shape tools to cut a piece out.
// - Area stats update live as the rectangle is drawn/edited.
// - POSTs to /api/generate and renders results.

// Mirrors the server. Big areas are fetched as a grid of Overpass queries, so
// the cap is render memory and patience rather than one API call's limit.
// Where a map stops being an easy one. None of these stops anything: the
// window says what you are in for and the button stays lit. A limit that says
// no is worth having only when the thing behind it cannot be done, and a big
// map can be done - it costs memory and patience, which are the mapper's to
// spend.
const BIG_AREA_KM2 = 400.0;
// Vanilla Knox County occupies compiled cells 0..77 by 0..62. The eastern
// edge is 77 * 256 tiles (knoxbuild/world.py); the north-south edge is
// 62 * 256 tiles by the same count. A game tile is 1 metre.
const VANILLA_AREA_KM2 = (77 * 256) * (62 * 256) / 1e6;
const BIG_TILES_PER_SIDE = 9000;
const BIG_LANDMARK_KM2 = 40.0;
const OVERPASS_TILE_KM2 = 30.0;
const SLOW_ABOVE_KM2 = 60.0;
// Each mod is 25 cells on a side. A cell is 300 tiles, so neighbouring mods
// meet on a cell edge.
const MOD_CELLS = 25;
const MOD_TILES = MOD_CELLS * 300;

// ---- errors -------------------------------------------------------------
// Every failure the server answers with carries an id (E-7F3A2C) that is also
// in logs/knoxmap.log beside the full error; the page shows it, so a bug
// report can point straight at the line. See knoxlog.py.
let lastErrorId = null;

function apiError(data, res) {
  const err = new Error((data && data.error) || `HTTP ${res.status}`);
  err.errorId = data && data.errorId;
  lastErrorId = err.errorId || null;
  return err;
}

// Errors in this page's own code, which otherwise vanish inside the app
// window where there is no console to see them.
function sendPageError(message, where, stack) {
  try {
    fetch('/api/client-error', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: String(message).slice(0, 500),
                             where: String(where || '').slice(0, 300),
                             stack: String(stack || '').slice(0, 4000) }),
    }).catch(() => {});
  } catch (_) { /* nothing more to do */ }
}
window.addEventListener('error', e =>
  sendPageError(e.message, `${e.filename}:${e.lineno}:${e.colno}`, e.error && e.error.stack));
window.addEventListener('unhandledrejection', e =>
  sendPageError((e.reason && e.reason.message) || e.reason, 'unhandled promise',
                e.reason && e.reason.stack));

const map = L.map('map', { zoomControl: false }).setView([38.0406, -84.5037], 14);
// Tiles through KnoxMap's own server, which follows the OSM tile policy -
// see the /tiles route in app.py.
L.tileLayer('/tiles/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '© OpenStreetMap contributors',
}).addTo(map);

const drawnItems = new L.FeatureGroup().addTo(map);
const SEL_STYLE = { color: '#a5e266', weight: 2, fillOpacity: 0.1, className: 'sel-rect' };
const ERASE_STYLE = { color: '#e06a62', weight: 2, fillColor: '#e06a62', fillOpacity: 0.16, dashArray: '4 4' };
function drawToolOptions(style) {
  return {
    rectangle: { shapeOptions: style },
    // Any outline, clicked point by point: a neighbourhood, a stretch of
    // coast, the blocks either side of a high street.
    polygon: { allowIntersection: false, showArea: true, shapeOptions: style },
    circle: { shapeOptions: style, showRadius: true, metric: true },
  };
}
const drawControl = new L.Control.Draw({
  position: 'bottomleft',
  draw: {
    polyline: false, marker: false, circlemarker: false,
    ...drawToolOptions(SEL_STYLE),
  },
  edit: { featureGroup: drawnItems, remove: true },
});

const VanillaOverlayControl = L.Control.extend({
  options: { position: 'bottomleft' },
  onAdd() {
    const box = L.DomUtil.create('div', 'vanilla-overlay-control');
    const bar = L.DomUtil.create('div', 'leaflet-bar', box);
    const btn = L.DomUtil.create('button', '', bar);
    btn.type = 'button';
    btn.id = 'vanillaOverlayBtn';
    btn.setAttribute('aria-pressed', 'false');
    btn.title = 'Overlay Vanilla Map';
    btn.setAttribute('aria-label', 'Overlay Vanilla Map');
    btn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" d="M1.6 4.2 6 2.4l4 1.6 4.4-1.8v9.6L10 13.6l-4-1.6-4.4 1.8Z"/><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M6 2.4v9.6M10 4v9.6"/></svg>';
    const note = L.DomUtil.create('div', 'hint', box);
    note.id = 'vanillaNote';
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    return box;
  },
});
const vanillaOverlayControl = new VanillaOverlayControl();

let currentRect = null;
let eraseMode = false;
let markerMode = false;
let lassoButton = null;
let stopLasso = null;
const LASSO_ADD = 'Draw freehand: drag round the area you want';
const LASSO_CUT = 'Eraser: drag round the area to cut out';
const MARKER_TIP = 'Marker: draw a shape to add to the selection';

function syncShapeTools() {
  const open = markerMode || eraseMode;
  const section = document.querySelector('#map .knox-shape-tools');
  const drawEl = document.querySelector('#map .knox-map-tools');
  if (section) section.hidden = !open;
  if (drawEl) drawEl.classList.toggle('knox-shapes-open', open);
  const marker = document.querySelector('#map .marker-btn');
  if (marker) {
    marker.classList.toggle('is-on', markerMode);
    marker.setAttribute('aria-pressed', markerMode ? 'true' : 'false');
  }
}

function cancelShapeDraw() {
  const bars = drawControl._toolbars;
  if (bars && bars.draw) bars.draw.disable();
  if (stopLasso) stopLasso();
}

function setMarkerMode(on) {
  if (markerMode === on) return;
  markerMode = on;
  if (on && eraseMode) setEraseMode(false);
  else {
    syncShapeTools();
    cancelShapeDraw();
  }
}

function setEraseMode(on) {
  if (eraseMode === on) return;
  eraseMode = on;
  if (on) markerMode = false;
  const btn = document.querySelector('#map .eraser-btn');
  if (btn) {
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  if (lassoButton) lassoButton.title = on ? LASSO_CUT : LASSO_ADD;
  drawControl.setDrawingOptions(drawToolOptions(on ? ERASE_STYLE : SEL_STYLE));
  const tips = [
    ['rectangle', 'Click and drag to draw rectangle.', 'Click and drag to erase a rectangle.'],
    ['circle', 'Click and drag to draw circle.', 'Click and drag to erase a circle.'],
    ['polygon', 'Click to start drawing shape.', 'Click to start the shape to erase.'],
  ];
  for (const [kind, add, cut] of tips) {
    L.drawLocal.draw.handlers[kind].tooltip.start = on ? cut : add;
  }
  map.getContainer().classList.toggle('erase-mode', on);
  syncShapeTools();
  cancelShapeDraw();
}

// A rectangle is one layer. A cut that leaves several pieces is a group of
// polygons; the edit tool needs each piece directly in drawnItems.
function setSelection(layer) {
  drawnItems.clearLayers();
  currentRect = layer;
  if (layer instanceof L.FeatureGroup && !(layer instanceof L.Path)) {
    layer.eachLayer(part => drawnItems.addLayer(part));
  } else {
    drawnItems.addLayer(layer);
  }
  updateBboxFields();
}

function clearSelection() {
  drawnItems.clearLayers();
  currentRect = null;
  clearBboxFields();
}

// A new shape joins the area already drawn. With the eraser on, the same
// shape is cut out of it.
function applyDrawn(layer) {
  if (!eraseMode) {
    if (!currentRect) setSelection(layer);
    else setSelection(L.featureGroup(selectionPieces().concat([layer])));
    return;
  }
  if (!currentRect) {
    fx.toast('', 'Nothing to erase', 'Draw an area first, then cut a shape out of it.');
    return;
  }
  cutSelection(layer);
}

map.on(L.Draw.Event.CREATED, (e) => {
  if (e.lasso) return;
  applyDrawn(e.layer);
});

// ---- freehand lasso ---------------------------------------------------------------
// Drag round what you want. The traced line is thinned to a polygon, so the
// server receives a few dozen points rather than every mouse move.
const LassoControl = L.Control.extend({
  options: { position: 'bottomleft' },
  onAdd() {
    const bar = L.DomUtil.create('div', 'leaflet-bar leaflet-control lasso-control');
    const a = L.DomUtil.create('a', 'lasso-btn', bar);
    a.href = '#';
    a.title = LASSO_ADD;
    a.innerHTML = '&#9998;';
    lassoButton = a;
    L.DomEvent.on(a, 'click', (ev) => { L.DomEvent.stop(ev); startLasso(a); });
    const eraser = L.DomUtil.create('a', 'eraser-btn', bar);
    eraser.href = '#';
    eraser.title = 'Eraser: draw a shape to cut it out of the selection';
    eraser.setAttribute('role', 'button');
    eraser.setAttribute('aria-pressed', 'false');
    eraser.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M9.1 1.7a1.4 1.4 0 0 1 2 0l3.2 3.2a1.4 1.4 0 0 1 0 2L8.2 13H4.7L1.8 10.1a1.4 1.4 0 0 1 0-2L9.1 1.7z"/><path stroke="currentColor" stroke-width="1.4" d="M2 14.2h12"/></svg>';
    L.DomEvent.on(eraser, 'click', (ev) => {
      L.DomEvent.stop(ev);
      setEraseMode(!eraseMode);
    });
    const marker = L.DomUtil.create('a', 'marker-btn', bar);
    marker.href = '#';
    marker.title = MARKER_TIP;
    marker.setAttribute('role', 'button');
    marker.setAttribute('aria-pressed', 'false');
    marker.innerHTML = '<svg viewBox="315 14 32 32" aria-hidden="true"><path fill="currentColor" d="m 337,30.156 0,0.407 0,5.604 c 0,1.658 -1.344,3 -3,3 l -10,0 c -1.655,0 -3,-1.342 -3,-3 l 0,-10 c 0,-1.657 1.345,-3 3,-3 l 6.345,0 3.19,-3.17 -9.535,0 c -3.313,0 -6,2.687 -6,6 l 0,10 c 0,3.313 2.687,6 6,6 l 10,0 c 3.314,0 6,-2.687 6,-6 l 0,-8.809 -3,2.968"/><path fill="currentColor" d="m 338.72,24.637 -8.892,8.892 -2.828,0 0,-2.829 8.89,-8.89 z"/><path fill="currentColor" d="m 338.697,17.826 4,0 0,4 -4,0 z" transform="matrix(-0.70698336,-0.70723018,0.70723018,-0.70698336,567.55917,274.78273)"/></svg>';
    L.DomEvent.on(marker, 'click', (ev) => {
      L.DomEvent.stop(ev);
      setMarkerMode(!markerMode);
    });
    return bar;
  },
});
// Bottom corners insert newest-first. arrangeMapTools then stacks the draw
// tools and leaves the vanilla overlay as its own control above them.
map.addControl(new LassoControl());
map.addControl(drawControl);
L.control.zoom({ position: 'bottomleft' }).addTo(map);
map.addControl(vanillaOverlayControl);
arrangeMapTools();

// One stack, bottom to top: Zoom out, Zoom in, then Clear all and Edit layer
// in their own group, then Eraser and Marker. Circle, Rectangle, Polygon, and
// Freehand sit above Marker only while Marker or Eraser is on. The vanilla
// overlay stays out of the stack.
function arrangeMapTools() {
  const corner = document.querySelector('#map .leaflet-bottom.leaflet-left');
  const shapeBtn = document.querySelector('#map .leaflet-draw-draw-polygon');
  const editBtn = document.querySelector('#map .leaflet-draw-edit-edit');
  const lasso = document.querySelector('#map .lasso-btn');
  const eraser = document.querySelector('#map .eraser-btn');
  const marker = document.querySelector('#map .marker-btn');
  const zoom = document.querySelector('#map .leaflet-control-zoom');
  const drawEl = document.querySelector('#map .leaflet-draw');
  const overlay = document.querySelector('#map .vanilla-overlay-control');
  const lassoBar = document.querySelector('#map .lasso-control');
  if (!corner || !shapeBtn || !editBtn || !lasso || !eraser || !marker || !zoom || !drawEl) return;
  editBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2.3 10.6 4.5 12.8 10.4 6.9 13.2 2.9 8.3 4.6z"/><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M2 14.3h6.2"/></svg>';

  const shapeBar = shapeBtn.closest('.leaflet-draw-toolbar');
  const shapeSection = shapeBtn.closest('.leaflet-draw-section');
  const editSection = editBtn.closest('.leaflet-draw-section');
  shapeBar.insertBefore(lasso, shapeBar.firstChild);

  const modeSection = L.DomUtil.create('div', 'leaflet-draw-section knox-mode-tools');
  const modeBar = L.DomUtil.create('div', 'leaflet-draw-toolbar leaflet-bar');
  modeBar.appendChild(marker);
  modeBar.appendChild(eraser);
  modeSection.appendChild(modeBar);
  editSection.parentNode.insertBefore(modeSection, editSection);

  const drawBar = drawControl._toolbars && drawControl._toolbars.draw;
  if (drawBar && drawBar._modes) {
    for (const type of ['polygon', 'rectangle', 'circle']) {
      if (drawBar._modes[type]) drawBar._modes[type].buttonIndex += 1;
    }
    if (typeof drawBar._lastButtonIndex === 'number') drawBar._lastButtonIndex += 1;
  }

  shapeSection.classList.add('knox-shape-tools');
  editSection.classList.add('knox-edit-tools');
  drawEl.classList.add('knox-map-tools');
  if (lassoBar) lassoBar.hidden = true;
  if (overlay && overlay.parentElement === corner) corner.appendChild(overlay);
  corner.appendChild(drawEl);
  corner.appendChild(zoom);
  syncShapeTools();
}

// The base game's Knox County is one header file per 256-tile cell
// (media/maps/Muldraugh, KY/<x>_<y>.lotheader). The overlay is those cells,
// not the rectangle around them. /api/vanilla-cells reads the names.
const VANILLA_CELL_TILES = 256;
const METRES_PER_DEGREE = 111320;
let vanillaCells = [];
let vanillaGroups = null;
let vanillaParts = [];
let vanillaMarker = null;

function vanillaMetresPerTile() {
  return parseFloat(document.getElementById('metersPerTile').value) || 1;
}

function vanillaKm(metres) {
  const km = metres / 1000;
  return km >= 100 ? String(Math.round(km)) : (Math.round(km * 10) / 10).toFixed(1);
}

function signedRingArea(ring) {
  let sum = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    sum += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  }
  return sum / 2;
}

function pointInCellRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

// Outer boundary and holes, in cell-corner coordinates. Shared edges cancel,
// so a missing cell stays a bite out of the county rather than being filled in.
function vanillaOutlineGroups(cells) {
  const edges = new Map();
  const add = (x1, y1, x2, y2) => {
    const fwd = `${x1},${y1},${x2},${y2}`;
    const rev = `${x2},${y2},${x1},${y1}`;
    if (edges.has(rev)) edges.delete(rev);
    else edges.set(fwd, [x1, y1, x2, y2]);
  };
  for (const [x, y] of cells) {
    add(x, y, x + 1, y);
    add(x + 1, y, x + 1, y + 1);
    add(x + 1, y + 1, x, y + 1);
    add(x, y + 1, x, y);
  }
  const from = new Map();
  for (const edge of edges.values()) {
    const key = `${edge[0]},${edge[1]}`;
    if (!from.has(key)) from.set(key, []);
    from.get(key).push(edge);
  }
  const unused = new Set(edges.keys());
  const rings = [];
  for (const edge of edges.values()) {
    const key = `${edge[0]},${edge[1]},${edge[2]},${edge[3]}`;
    if (!unused.has(key)) continue;
    const ring = [[edge[0], edge[1]]];
    let cur = edge;
    unused.delete(key);
    let guard = 0;
    while (guard++ < edges.size + 2) {
      ring.push([cur[2], cur[3]]);
      if (cur[2] === ring[0][0] && cur[3] === ring[0][1]) break;
      const nexts = from.get(`${cur[2]},${cur[3]}`) || [];
      const nxt = nexts.find(n => unused.has(`${n[0]},${n[1]},${n[2]},${n[3]}`));
      if (!nxt) break;
      unused.delete(`${nxt[0]},${nxt[1]},${nxt[2]},${nxt[3]}`);
      cur = nxt;
    }
    if (ring.length >= 4) rings.push(ring);
  }
  const scored = rings.map(ring => ({ ring, area: signedRingArea(ring) }));
  scored.sort((a, b) => Math.abs(b.area) - Math.abs(a.area));
  if (!scored.length) return [];
  const outerSign = Math.sign(scored[0].area) || 1;
  const groups = [];
  const holes = [];
  for (const item of scored) {
    if (Math.sign(item.area) === outerSign) groups.push({ outer: item.ring, holes: [] });
    else holes.push(item.ring);
  }
  for (const hole of holes) {
    const [x, y] = hole[0];
    const host = groups.find(g => pointInCellRing(x, y, g.outer));
    if (host) host.holes.push(hole);
  }
  return groups;
}

function vanillaExtentMetres() {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const group of vanillaGroups || []) {
    for (const [x, y] of group.outer) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
  }
  const mpt = vanillaMetresPerTile();
  return {
    width: (maxX - minX) * VANILLA_CELL_TILES * mpt,
    height: (maxY - minY) * VANILLA_CELL_TILES * mpt,
    minX, maxX, minY, maxY,
  };
}

function vanillaLatLngRings(center) {
  const { minX, maxX, minY, maxY } = vanillaExtentMetres();
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const mpt = vanillaMetresPerTile();
  const cos = Math.cos(center.lat * Math.PI / 180);
  const toLL = (x, y) => {
    const east = (x - cx) * VANILLA_CELL_TILES * mpt;
    const north = (cy - y) * VANILLA_CELL_TILES * mpt;
    return [
      center.lat + north / METRES_PER_DEGREE,
      center.lng + east / (METRES_PER_DEGREE * cos),
    ];
  };
  return (vanillaGroups || []).map(group =>
    [group.outer, ...group.holes].map(ring => ring.map(([x, y]) => toLL(x, y))));
}

function vanillaIcon() {
  const { width, height } = vanillaExtentMetres();
  return L.divIcon({
    className: 'vanilla-handle',
    html: `<span class="vanilla-name">Vanilla map</span><span class="vanilla-size">${vanillaKm(width)} × ${vanillaKm(height)} km</span>`,
    iconSize: [230, 24],
    iconAnchor: [115, 12],
  });
}

function setVanillaButton(on) {
  const btn = document.getElementById('vanillaOverlayBtn');
  btn.classList.toggle('is-on', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
}

function placeVanilla(center) {
  const rings = vanillaLatLngRings(center);
  vanillaParts.forEach((part, i) => { if (rings[i]) part.setLatLngs(rings[i]); });
  if (vanillaMarker) {
    vanillaMarker.setLatLng(center);
    vanillaMarker.setIcon(vanillaIcon());
  }
}

function bindVanillaDrag(layer) {
  const el = layer.getElement();
  if (!el) return;
    el.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0 || !vanillaMarker) return;
    ev.preventDefault();
    ev.stopPropagation();
    if (el.setPointerCapture) el.setPointerCapture(ev.pointerId);
    const start = map.mouseEventToLatLng(ev);
    const origin = vanillaMarker.getLatLng();
    el.style.cursor = 'grabbing';
    const move = (e) => {
      const now = map.mouseEventToLatLng(e);
      placeVanilla(L.latLng(
        origin.lat + (now.lat - start.lat),
        origin.lng + (now.lng - start.lng),
      ));
    };
    const up = () => {
      el.style.cursor = 'grab';
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
}

function hideVanillaOverlay() {
  vanillaParts.forEach(part => map.removeLayer(part));
  vanillaParts = [];
  if (vanillaMarker) map.removeLayer(vanillaMarker);
  vanillaMarker = null;
  setVanillaButton(false);
}

function syncVanillaOverlay() {
  if (!vanillaMarker) return;
  placeVanilla(vanillaMarker.getLatLng());
}

function showVanillaOverlay() {
  const note = document.getElementById('vanillaNote');
  if (!vanillaCells.length || !vanillaGroups || !vanillaGroups.length) {
    if (note) note.textContent = 'The base game\'s Knox County map was not found.';
    return;
  }
  if (note) note.textContent = '';
  hideVanillaOverlay();
  const center = map.getCenter();
  const style = {
    color: '#ffc857', weight: 2, dashArray: '7 6',
    fillColor: '#ffc857', fillOpacity: 0.08,
    interactive: true, smoothFactor: 0, className: 'vanilla-shape',
  };
  vanillaParts = vanillaLatLngRings(center).map(rings => L.polygon(rings, style).addTo(map));
  vanillaParts.forEach(bindVanillaDrag);
  vanillaMarker = L.marker(center, {
    draggable: true, keyboard: false, zIndexOffset: 600,
    bubblingMouseEvents: false, icon: vanillaIcon(),
  }).addTo(map);
  vanillaMarker.on('drag', () => placeVanilla(vanillaMarker.getLatLng()));
  setVanillaButton(true);
  const pts = [];
  const walk = (node) => {
    if (!node) return;
    if (typeof node.lat === 'number') pts.push(node);
    else if (Array.isArray(node)) node.forEach(walk);
  };
  vanillaParts.forEach(part => walk(part.getLatLngs()));
  if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [28, 28] });
}

async function loadVanillaCells() {
  try {
    const data = await (await fetch('/api/vanilla-cells')).json();
    vanillaCells = data.cells || [];
    vanillaGroups = vanillaOutlineGroups(vanillaCells);
  } catch (_) {
    vanillaCells = [];
    vanillaGroups = [];
  }
}
loadVanillaCells();

document.getElementById('vanillaOverlayBtn').addEventListener('click', () => {
  if (vanillaParts.length) hideVanillaOverlay();
  else showVanillaOverlay();
});

function startLasso(button) {
  if (stopLasso) stopLasso();
  const box = map.getContainer();
  button.classList.add('is-active');
  box.classList.add('lasso-armed');
  map.dragging.disable();
  let points = [];
  let trail = null;
  let done = false;
  const down = (e) => {
    points = [e.latlng];
    const color = eraseMode ? ERASE_STYLE.color : SEL_STYLE.color;
    trail = L.polyline(points, { color, weight: 2, dashArray: '4 4' }).addTo(map);
    map.on('mousemove', move);
  };
  const move = (e) => { points.push(e.latlng); trail.setLatLngs(points); };
  const finish = (commit) => {
    if (done) return;
    done = true;
    stopLasso = null;
    map.off('mousedown', down);
    map.off('mousemove', move);
    map.off('mouseup', up);
    map.dragging.enable();
    button.classList.remove('is-active');
    box.classList.remove('lasso-armed');
    if (trail) map.removeLayer(trail);
    if (!commit) return;
    const thin = simplifyLatLngs(points, 8);
    if (thin.length >= 3) {
      const layer = L.polygon(thin, eraseMode ? ERASE_STYLE : SEL_STYLE);
      if (!eraseMode) layer._knoxKind = 'freehand';
      applyDrawn(layer);
      map.fire(L.Draw.Event.CREATED, { layer, layerType: 'polygon', lasso: true });
    }
  };
  const up = () => finish(true);
  stopLasso = () => finish(false);
  map.on('mousedown', down);
  map.on('mouseup', up);
}

// Douglas-Peucker on screen pixels, so "a few metres" means the same at every zoom.
function simplifyLatLngs(latlngs, tolerancePx) {
  if (latlngs.length < 3) return latlngs;
  const pts = latlngs.map(ll => map.latLngToLayerPoint(ll));
  const keep = new Array(pts.length).fill(false);
  keep[0] = keep[pts.length - 1] = true;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    let best = -1, bestD = tolerancePx;
    for (let i = a + 1; i < b; i++) {
      const d = L.LineUtil.pointToSegmentDistance(pts[i], pts[a], pts[b]);
      if (d > bestD) { best = i; bestD = d; }
    }
    if (best >= 0) { keep[best] = true; stack.push([a, best], [best, b]); }
  }
  return latlngs.filter((_, i) => keep[i]);
}

function circleRing(circle) {
  const c = circle.getLatLng();
  const r = circle.getRadius();
  const ring = [];
  for (let i = 0; i < 64; i++) {
    const a = (i / 64) * 2 * Math.PI;
    const dLat = (r * Math.cos(a)) / 111320;
    const dLon = (r * Math.sin(a)) / (111320 * Math.cos(c.lat * Math.PI / 180));
    ring.push([wrapLon(c.lng + dLon), c.lat + dLat]);
  }
  ring.push(ring[0]);
  return ring;
}

function rectRing(rect) {
  const b = rect.getBounds();
  const w = wrapLon(b.getWest());
  const e = wrapLon(b.getEast());
  return [[w, b.getSouth()], [e, b.getSouth()], [e, b.getNorth()], [w, b.getNorth()], [w, b.getSouth()]];
}

// GeoJSON Polygon or MultiPolygon for a layer. A rectangle is included here
// so the eraser can cut it; selectionShape drops it again, because a plain
// rectangle means "build the whole box".
function outlineGeometry(layer) {
  if (!layer) return null;
  if (layer instanceof L.FeatureGroup && !(layer instanceof L.Path)) {
    const polys = [];
    layer.eachLayer(part => {
      const geo = outlineGeometry(part);
      if (!geo) return;
      if (geo.type === 'Polygon') polys.push(geo.coordinates);
      else polys.push(...geo.coordinates);
    });
    if (!polys.length) return null;
    if (polys.length === 1) return { type: 'Polygon', coordinates: polys[0] };
    return { type: 'MultiPolygon', coordinates: polys };
  }
  if (layer instanceof L.Circle) return { type: 'Polygon', coordinates: [circleRing(layer)] };
  if (layer instanceof L.Rectangle) return { type: 'Polygon', coordinates: [rectRing(layer)] };
  const raw = layer.toGeoJSON && layer.toGeoJSON();
  const geo = raw && (raw.geometry || raw);
  const fold = coords => coords.map(ring => ring.map(([lon, lat]) => [wrapLon(lon), lat]));
  if (geo && geo.type === 'Polygon') return { type: 'Polygon', coordinates: fold(geo.coordinates) };
  if (geo && geo.type === 'MultiPolygon') return { type: 'MultiPolygon', coordinates: geo.coordinates.map(fold) };
  return null;
}

function layerPolygons(layer) {
  const geo = outlineGeometry(layer);
  if (!geo) return [];
  return geo.type === 'Polygon' ? [geo.coordinates] : geo.coordinates;
}

// The selection as GeoJSON for the server, or null for a plain rectangle.
function selectionShape() {
  if (!currentRect || currentRect instanceof L.Rectangle) return null;
  return outlineGeometry(currentRect);
}

// Cut `layer` out of the current selection. A miss leaves the selection as it
// was, so a circle stays a circle. Cutting the whole thing clears it.
function cutSelection(layer) {
  const subject = layerPolygons(currentRect);
  const clip = layerPolygons(layer);
  if (!subject.length || !clip.length) return;
  let minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
  const take = polys => {
    for (const rings of polys) {
      for (const ring of rings) {
        for (const [lon, lat] of ring) {
          if (lon < minLon) minLon = lon;
          if (lon > maxLon) maxLon = lon;
          if (lat < minLat) minLat = lat;
          if (lat > maxLat) maxLat = lat;
        }
      }
    }
  };
  take(subject);
  take(clip);
  const lon0 = (minLon + maxLon) / 2;
  const lat0 = (minLat + maxLat) / 2;
  const cos = Math.cos(lat0 * Math.PI / 180);
  const toXY = polys => polys.map(rings => rings.map(ring => ring.map(([lon, lat]) => [
    (lon - lon0) * 111320 * cos,
    (lat - lat0) * 111320,
  ])));
  const diff = knoxClip.difference(toXY(subject), toXY(clip));
  if (!diff.changed) return;
  if (!diff.polygons.length) {
    clearSelection();
    return;
  }
  const back = diff.polygons.map(rings => rings.map(ring => ring.map(([x, y]) => [
    wrapLon(lon0 + x / (111320 * cos)),
    lat0 + y / 111320,
  ])));
  const parts = [];
  for (const rings of back) {
    const latlngs = rings.map(ring => {
      const pts = ring.slice();
      const a = pts[0], b = pts[pts.length - 1];
      if (pts.length > 1 && a[0] === b[0] && a[1] === b[1]) pts.pop();
      return pts.map(([lon, lat]) => [lat, lon]);
    }).filter(ring => ring.length >= 3);
    if (latlngs.length) parts.push(L.polygon(latlngs, SEL_STYLE));
  }
  if (!parts.length) clearSelection();
  else setSelection(parts.length === 1 ? parts[0] : L.featureGroup(parts));
}

// Area inside the selection in km², on a local flat projection: plenty for
// town-sized shapes.
function selectionAreaKm2() {
  const shape = selectionShape();
  if (!shape) return null;
  const polys = shape.type === 'Polygon' ? [shape.coordinates] : shape.coordinates;
  let total = 0;
  for (const rings of polys) {
    rings.forEach((ring, idx) => {
      const lat0 = ring[0][1] * Math.PI / 180;
      let sum = 0;
      for (let i = 0; i < ring.length - 1; i++) {
        const [x1, y1] = [ring[i][0] * 111.32 * Math.cos(lat0), ring[i][1] * 111.32];
        const [x2, y2] = [ring[i + 1][0] * 111.32 * Math.cos(lat0), ring[i + 1][1] * 111.32];
        sum += x1 * y2 - x2 * y1;
      }
      total += (idx === 0 ? 1 : -1) * Math.abs(sum) / 2;
    });
  }
  return total;
}
map.on(L.Draw.Event.EDITED, () => updateBboxFields());
map.on(L.Draw.Event.DELETED, (e) => {
  const removed = [];
  e.layers.eachLayer(l => removed.push(l));
  if (currentRect instanceof L.FeatureGroup && !(currentRect instanceof L.Path)) {
    removed.forEach(l => { if (currentRect.hasLayer(l)) currentRect.removeLayer(l); });
    const left = currentRect.getLayers();
    if (left.length === 1) currentRect = left[0];
    else if (!left.length) currentRect = null;
  } else if (!drawnItems.getLayers().length || removed.includes(currentRect)) {
    currentRect = null;
  }
  if (!currentRect) clearBboxFields();
  else updateBboxFields();
});

// Leaflet reports coordinates *unwrapped* once the map has been panned across
// a world copy, so a rectangle drawn after dragging east twice comes back at
// longitude 747 rather than 27. The server folds these back too, but doing it
// here as well keeps the boxes on screen showing where the user actually is.
function wrapLon(lon) {
  return ((lon + 180) % 360 + 360) % 360 - 180;
}

function rectBounds(rect) {
  const b = rect.getBounds();
  return {
    s: b.getSouth(), n: b.getNorth(),
    w: wrapLon(b.getWest()), e: wrapLon(b.getEast()),
  };
}

function formatSpan(metres) {
  const a = Math.abs(metres);
  if (a >= 1000) {
    const km = a / 1000;
    const text = km >= 100 ? String(Math.round(km)) : (Math.round(km * 10) / 10).toFixed(1);
    return `${text} km`;
  }
  return `${Math.round(a)} m`;
}

function formatKm2(km2) {
  if (km2 < 10) return `${km2.toFixed(2)} km²`;
  return `${Math.round(km2)} km²`;
}

function mapSizeWarning(area, mods) {
  const size = formatKm2(area);
  const baseline = formatKm2(BIG_AREA_KM2);
  const vsBase = Math.round(Math.abs(area - BIG_AREA_KM2) / BIG_AREA_KM2 * 100);
  const relation = area < BIG_AREA_KM2 ? 'smaller' : 'bigger';
  const ofVanilla = Math.round(area / VANILLA_AREA_KM2 * 100);
  const splitWord = mods === 1 ? 'mod' : 'mods';
  return `${size} is ${vsBase}% ${relation} than most modded maps (${baseline}). `
    + `It is ${ofVanilla}% the size of the Vanilla Map. `
    + `It will be split into ${mods} ${splitWord}.`;
}

function coordText(lat, lon) {
  return `${Number(lat).toFixed(5)}, ${wrapLon(lon).toFixed(5)}`;
}

function cornerItems(bounds) {
  const s = bounds.getSouth(), n = bounds.getNorth();
  const w = wrapLon(bounds.getWest()), e = wrapLon(bounds.getEast());
  return [['SW', s, w], ['SE', s, e], ['NE', n, e], ['NW', n, w]];
}

function pointItems(latlngs) {
  const pts = latlngs.slice();
  if (pts.length > 1) {
    const a = pts[0], b = pts[pts.length - 1];
    if (a.lat === b.lat && a.lng === b.lng) pts.pop();
  }
  return pts.map(ll => ['', ll.lat, ll.lng]);
}

function asPolygons(latlngs) {
  if (!latlngs || !latlngs.length) return [];
  const first = latlngs[0];
  if (first && typeof first.lat === 'number') return [[latlngs]];
  if (first && first[0] && typeof first[0].lat === 'number') return [latlngs];
  return latlngs;
}

function layerBlocks(layer) {
  if (layer instanceof L.Circle) {
    const c = layer.getLatLng();
    const radius = formatSpan(layer.getRadius());
    return [{
      name: 'Circle',
      points: [['Centre', c.lat, c.lng], ['Radius', null, null, radius],
        ...cornerItems(layer.getBounds())],
    }];
  }
  if (layer instanceof L.Rectangle) {
    return [{ name: 'Rectangle', points: cornerItems(layer.getBounds()) }];
  }
  const kind = layer._knoxKind === 'freehand' ? 'Freehand' : 'Polygon';
  const blocks = [];
  for (const rings of asPolygons(layer.getLatLngs ? layer.getLatLngs() : [])) {
    rings.forEach((ring, i) => {
      blocks.push({ name: i === 0 ? kind : 'Hole', points: pointItems(ring) });
    });
  }
  return blocks;
}

function selectionPieces() {
  if (!currentRect) return [];
  if (currentRect instanceof L.FeatureGroup && !(currentRect instanceof L.Path)) {
    return currentRect.getLayers();
  }
  return [currentRect];
}

function renderAreaLayers() {
  const host = document.getElementById('area-layers');
  if (!host) return;
  host.innerHTML = '';
}

function updateBboxFields() {
  if (!currentRect) return clearBboxFields();
  const { s, w, n, e } = rectBounds(currentRect);
  document.getElementById('south').value = s.toFixed(6);
  document.getElementById('west').value  = w.toFixed(6);
  document.getElementById('north').value = n.toFixed(6);
  document.getElementById('east').value  = e.toFixed(6);
  renderAreaLayers();

  const area = bboxAreaKm2(s, w, n, e);
  const mpt = parseFloat(document.getElementById('metersPerTile').value);
  const widthM  = haversineKm(s, w, s, e) * 1000;
  const heightM = haversineKm(s, w, n, w) * 1000;
  const tilesX = Math.ceil(widthM / mpt / 300) * 300;
  const tilesY = Math.ceil(heightM / mpt / 300) * 300;
  const cellsX = tilesX / 300;
  const cellsY = tilesY / 300;

  const modsX = Math.max(1, Math.ceil(tilesX / MOD_TILES));
  const modsY = Math.max(1, Math.ceil(tilesY / MOD_TILES));
  const mods = modsX * modsY;
  const stats = document.getElementById('area-stats');
  const btn = document.getElementById('generateBtn');
  const side = Math.max(tilesX, tilesY);
  const heavy = [];
  if (area > BIG_AREA_KM2) {
    heavy.push(mapSizeWarning(area, mods));
  }
  if (side > BIG_TILES_PER_SIDE) {
    heavy.push(`${side} tiles a side is a large selection `
             + `(${BIG_TILES_PER_SIDE} is a comfortable one).`);
  }
  const slow = !heavy.length && area > SLOW_ABOVE_KM2;
  const shape = selectionAreaKm2();
  const shown = shape !== null ? shape : area;

  stats.hidden = false;
  stats.className = heavy.length || slow ? 'warn' : 'ok';
  stats.innerHTML = `
    <div class="size-block">
      <div class="size-k">Real world</div>
      <div class="size-line">${formatSpan(widthM)} × ${formatSpan(heightM)}</div>
      <div class="size-line">${formatKm2(shown)}</div>
    </div>
    <div class="size-block">
      <div class="size-k">In game</div>
      <div class="size-line">${tilesX.toLocaleString()} × ${tilesY.toLocaleString()} <span>tiles</span></div>
      <div class="size-line">${cellsX} × ${cellsY} <span>cells</span></div>
    </div>
    ${shape !== null ? `<div class="stat-note shape">Only the selected area is built. Padding and woodland-only cells are left out so the game fills them in.</div>` : ''}
    ${heavy.length ? `<div class="stat-note warn">${heavy.join(' ')}
      You can still build it — this is a heads-up, not a wall.</div>` : ''}
    ${slow ? `<div class="stat-note warn">A large selection — ${mods} mods, drawn one at a time.</div>` : ''}
  `;
  btn.disabled = false;
  fx.step('area', 'done');
}

function clearBboxFields() {
  ['south', 'west', 'north', 'east'].forEach(id => {
    document.getElementById(id).value = '';
  });
  renderAreaLayers();
  const stats = document.getElementById('area-stats');
  stats.className = 'empty';
  stats.hidden = true;
  stats.innerHTML = '';
  document.getElementById('generateBtn').disabled = true;
  const landmarks = document.getElementById('landmark-results');
  if (landmarks) landmarks.innerHTML = '';
  fx.resetFrom('area');
}

function syncScaleCards() {
  const scale = document.getElementById('metersPerTile');
  if (!scale) return;
  document.querySelectorAll('#scaleCards .preset-card').forEach(card => {
    const on = Number(card.dataset.scale) === Number(scale.value);
    card.classList.toggle('is-on', on);
    card.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

document.querySelectorAll('#scaleCards .preset-card').forEach(card => {
  card.addEventListener('click', () => {
    const scale = document.getElementById('metersPerTile');
    if (!scale) return;
    scale.value = card.dataset.scale;
    scale.dispatchEvent(new Event('change'));
  });
});

document.getElementById('metersPerTile').addEventListener('change', () => {
  syncScaleCards();
  updateBboxFields();
  syncVanillaOverlay();
});
syncScaleCards();

// Landscape and vegetation, 3 bytes a tile each, held at full size while the
// map is drawn. It is the number that decides whether a big map finishes.
function bitmapFor(tilesX, tilesY) {
  const mb = (tilesX * tilesY * 3 * 2) / 1e6;
  return mb < 1000 ? `${Math.round(mb)} MB` : `${(mb / 1000).toFixed(1)} GB`;
}

function bboxAreaKm2(s, w, n, e) {
  const hKm = (n - s) * 111.32;
  const wKm = (e - w) * 111.32 * Math.cos((s + n) / 2 * Math.PI / 180);
  return Math.abs(hKm * wKm);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat/2)**2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon/2)**2;
  return 2 * R * Math.asin(Math.sqrt(a));
}

// ---- settings ----
//
// The controls are built from /api/settings rather than written out in the
// markup, so the limits and presets live in exactly one place: a knob added to
// Settings appears here on its own, and one whose range changes cannot end up
// with the page enforcing last week's bounds.

const AREA_SETTING_KEYS = new Set(['seed', 'fill_gaps', 'true_map', 'guaranteed_rifle']);
let stableSeed = 1;

const SETTING_LABELS = {
  zombies_per_resident:['Zombies per person', 'How many zombies appear for each person who lived or worked here. Higher means a busier outbreak.'],
  m2_per_person:       ['Living space (m²)', 'How much room each person had. Lower packs more people into each building, so you get more zombies.'],
  spawn_density:       ['Horde cap', 'The biggest crowd of zombies that can stand in one small patch of ground. Higher lets busy streets fill up more.'],
  tree_density:        ['Woodland', 'How thick the trees are. Higher means more woods to hide in.'],
  min_size:            ['Smallest building', 'Sheds and other tiny buildings smaller than this are left off the map. Raise it to skip more of them.'],
  align_streets:       ['Straighten streets', 'Turns the town so the main streets run straight, instead of jagged diagonal roads. Off leaves north pointing up.'],
  rotate_degrees:      ['Turn the map', 'Spins the whole town by this many degrees. Use it if Straighten streets picks the wrong direction.'],
  straight_roads:      ['Knox County roads', 'Redraws roads as straight lines and corners, like the original game map, and lines buildings up beside them. Off keeps the real road shapes.'],
  max_size:            ['Largest building', 'Buildings bigger than this are left off the map. Raise it to keep factories, malls, and other huge places.'],
  apartment_footprint: ['Flats above', 'A large building with no name is treated as apartments once it reaches this size. Smaller ones stay houses.'],
  apartment_chance:    ['Flats chance', 'How often those large unnamed buildings actually become apartment blocks. Lower means more big houses.'],
  max_levels:          ['Tallest building', 'The most floors any building can have. Taller towns take longer to finish.'],
  room_size:           ['Room size', 'How big the rooms inside buildings are. Higher means fewer, larger rooms.'],
  square_buildings:    ['Square up buildings', 'Crooked buildings are straightened so their walls run with the street. Higher straightens more of them. All the way up, every building faces the street.'],
  neighbourhood_tiles: ['Neighbourhood', 'How far a row of matching houses stretches. Higher means whole streets look alike. Lower means the look changes more often.'],
  style_oddity:        ['Odd one out', 'How often one house looks different from the houses next to it.'],
  parking_density:     ['Parking', 'How many cars are parked on the streets and in lots. Higher means more vehicles.'],
};

let settingsMeta = null;
let savedMapSettings = null;

function isSwitch(key) {
  const lim = settingsMeta.limits[key];
  if (!lim || Number(lim[0]) !== 0 || Number(lim[1]) !== 1) return false;
  // A 0–1 float is still a quantity (how often, how odd). Only an integer
  // that can be nothing but off or on becomes a switch.
  const type = settingsMeta.types
    ? settingsMeta.types[key]
    : (Number.isInteger(settingsMeta.defaults[key]) ? 'int' : 'float');
  return type === 'int';
}

async function loadSettings() {
  const res = await fetch('/api/settings');
  settingsMeta = await res.json();
  buildSettingsForm(settingsMeta.current);
  applyAreaSettings(settingsMeta.current);
  if (savedMapSettings) applySavedMapSettings(savedMapSettings);
}

function buildSettingsForm(values) {
  const body = document.getElementById('advanced-body');
  body.innerHTML = '';
  for (const [key, [label, hint]] of Object.entries(SETTING_LABELS)) {
    if (AREA_SETTING_KEYS.has(key)) continue;
    if (!(key in settingsMeta.defaults)) continue;   // knob has been removed
    const [lo, hi] = settingsMeta.limits[key];
    const isInt = settingsMeta.types
      ? settingsMeta.types[key] === 'int'
      : Number.isInteger(settingsMeta.defaults[key]);
    const wrap = document.createElement('div');
    if (isSwitch(key)) {
      wrap.className = 'setting setting-toggle';
      wrap.innerHTML = `<label class="toggle">
        <input type="checkbox" data-key="${key}"${Number(values[key]) ? ' checked' : ''}>
        <span class="toggle-track" aria-hidden="true"></span>
        <span class="toggle-copy">
          <span class="toggle-name">${label}</span>
          <span class="toggle-hint">${hint}</span>
        </span>
      </label>`;
    } else {
      wrap.className = 'setting';
      wrap.innerHTML = `
        <div class="setting-head"><span class="setting-name">${label}</span>
          <output class="setting-val">${values[key]}</output></div>
        <input type="range" data-key="${key}" min="${lo}" max="${hi}"
           step="${isInt ? 1 : 0.05}" value="${values[key]}">
        <span class="setting-hint">${hint}</span>`;
    }
    body.appendChild(wrap);
  }
  fx.wireSettings(body);
}

function controlValue(el) {
  if (el.type === 'checkbox') return el.checked ? 1 : 0;
  if (el.value.trim() === '') return null;
  const n = parseFloat(el.value);
  return Number.isNaN(n) ? null : n;
}

function readSettings() {
  const out = { preset: document.getElementById('preset').value };
  for (const el of document.querySelectorAll('#advanced-body input[data-key], #tabPanelArea input[data-key]')) {
    // Blank means "whatever the preset says" rather than zero.
    const value = controlValue(el);
    if (value !== null) out[el.dataset.key] = value;
  }
  const setSeed = document.getElementById('seedRandom');
  if (setSeed && !setSeed.checked) out.seed = 1;
  return out;
}

function applyAreaSettings(values) {
  if (!values) return;
  const seedEl = document.getElementById('areaSeed');
  if (settingsMeta && settingsMeta.limits && settingsMeta.limits.seed && seedEl) {
    seedEl.min = settingsMeta.limits.seed[0];
    seedEl.max = settingsMeta.limits.seed[1];
  }
  const setSeed = document.getElementById('seedRandom');
  if (seedEl && (!setSeed || !setSeed.checked)) {
    stableSeed = 1;
    seedEl.value = '1';
  } else if ('seed' in values) {
    stableSeed = Number(values.seed);
  }
  for (const key of ['fill_gaps', 'true_map', 'guaranteed_rifle']) {
    const el = document.querySelector(`#tabPanelArea input[data-key="${key}"]`);
    if (el && key in values) el.checked = Number(values[key]) === 1;
  }
}

// Opening a saved map keeps the other knobs it was built with. Set seed stays
// off, so the build uses seed 1 until that switch is turned on.
function applySavedMapSettings(settings) {
  if (!settings) return;
  savedMapSettings = settings;
  if (!settingsMeta) return;
  const random = document.getElementById('seedRandom');
  if (random) random.checked = false;
  const field = document.getElementById('seedField');
  if (field) field.hidden = true;
  buildSettingsForm(settings);
  applyAreaSettings(settings);
}

document.getElementById('seedRandom').addEventListener('change', () => {
  const on = document.getElementById('seedRandom').checked;
  const field = document.getElementById('seedField');
  const input = document.getElementById('areaSeed');
  if (on) {
    input.value = String(Math.floor(Math.random() * 999999) + 1);
    field.hidden = false;
  } else {
    stableSeed = 1;
    input.value = '1';
    field.hidden = true;
  }
});

document.getElementById('preset').addEventListener('change', () => {
  if (!settingsMeta) return;
  const preset = settingsMeta.presets[document.getElementById('preset').value];
  if (!preset) return;
  buildSettingsForm(preset);
  applyAreaSettings(preset);
});

document.getElementById('resetSettings').addEventListener('mousedown', (e) => {
  e.preventDefault();
  e.stopPropagation();
});
document.getElementById('resetSettings').addEventListener('click', (e) => {
  e.preventDefault();
  e.stopPropagation();
  if (!settingsMeta) return;
  const preset = settingsMeta.presets[document.getElementById('preset').value];
  buildSettingsForm(preset || settingsMeta.defaults);
});

// ---- setup check -----------------------------------------------------------------

let setupTimer = null;
let setupCheckGen = 0;

function setupProgressLine(done, total) {
  const template = 'X of Y steps complete';
  const translated = (typeof i18n !== 'undefined' && i18n.say && i18n.say(template)) || template;
  return translated.split('X').join(String(done)).split('Y').join(String(total));
}

function showFirstTimeSetup(on, step, total) {
  const actions = document.getElementById('sideHomeActions');
  const panel = document.getElementById('sideHomeSetup');
  if (actions) actions.hidden = !!on;
  if (panel) panel.hidden = !on;
  document.body.dataset.firstSetup = on ? '1' : '0';
  if (!on) return;
  const count = document.getElementById('setupStepCount');
  if (!count) return;
  const line = setupProgressLine(step || 0, total || 0);
  if (count.textContent !== line) count.textContent = line;
}

async function checkSetup() {
  const gen = ++setupCheckGen;
  clearTimeout(setupTimer);
  try {
    const res = await fetch('/api/setup-status');
    const data = await res.json();
    if (gen !== setupCheckGen) return;
    showFirstTimeSetup(!!data.first_time, data.step, data.total);
    const card = document.getElementById('setupCard');
    const lead = document.getElementById('setupLead');
    if (data.ready) {
      card.hidden = true;
    } else {
      if (lead) lead.hidden = !data.busy;
      document.getElementById('setupList').innerHTML = data.checks.map(c =>
        `<li class="${c.ok ? 'ok' : 'missing'}"><span>${c.ok ? '✓' : '✗'}</span>
          <b>${escapeHtml(c.label)}</b>${c.ok ? '' : ` — ${escapeHtml(c.fix)}`}</li>`).join('');
      card.hidden = false;
    }
    applyExportLocations(data);
    if (data.first_time || (!data.ready && data.busy)) {
      setupTimer = setTimeout(checkSetup, data.first_time ? 500 : 2000);
    }
  } catch (_) {
    if (gen !== setupCheckGen) return;
    showFirstTimeSetup(false, 0, 0);
  }
}
checkSetup();

// The program file does not fetch WorldEd until the player says so. Download
// installs the release next to KnoxMap. Find opens a file window for the exe.

let worldedChoiceTimer = null;
let worldedChoiceBusy = false;

function applyWorldEdChoice(data) {
  const box = document.getElementById('worldedAsk');
  if (!box) return;
  const show = !!(data && (data.ask || data.state === 'running' || data.state === 'error'));
  box.hidden = !show;
  document.body.dataset.worldedAsk = show ? '1' : '0';
  const noteEl = document.getElementById('worldedAskNote');
  const download = document.getElementById('worldedDownload');
  const find = document.getElementById('worldedFind');
  const busy = !!(data && data.state === 'running');
  if (!worldedChoiceBusy) {
    if (download) download.disabled = busy;
    if (find) find.disabled = busy;
  }
  if (!noteEl || worldedChoiceBusy) return;
  if (data && data.state === 'error' && data.error) noteEl.textContent = data.error;
  else if (data && data.message) noteEl.textContent = data.message;
}

async function pollWorldEdChoice() {
  clearTimeout(worldedChoiceTimer);
  try {
    const data = await (await fetch('/api/worlded-choice')).json();
    applyWorldEdChoice(data);
    if (data.state === 'running') {
      worldedChoiceTimer = setTimeout(pollWorldEdChoice, 700);
      if (document.body.dataset.firstSetup !== '1') checkSetup();
    } else if (data.ask) worldedChoiceTimer = setTimeout(pollWorldEdChoice, 2000);
    else checkSetup();
  } catch (_) {
    document.body.dataset.worldedAsk = document.body.dataset.worldedAsk || '0';
  }
}

pollWorldEdChoice();

async function chooseWorldEd(action) {
  const download = document.getElementById('worldedDownload');
  const find = document.getElementById('worldedFind');
  const noteEl = document.getElementById('worldedAskNote');
  worldedChoiceBusy = true;
  download.disabled = true;
  find.disabled = true;
  if (action === 'download') noteEl.textContent = 'Downloading WorldEd…';
  else noteEl.textContent = '';
  try {
    const res = await fetch('/api/worlded-choice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Could not set up WorldEd.');
    if (data.cancelled) {
      download.disabled = false;
      find.disabled = false;
      noteEl.textContent = '';
      return;
    }
    applyWorldEdChoice(data.ask === undefined ? { ...data, ask: true, state: 'running' } : data);
    pollWorldEdChoice();
  } catch (err) {
    noteEl.textContent = err.message;
    download.disabled = false;
    find.disabled = false;
  } finally {
    worldedChoiceBusy = false;
  }
}

document.getElementById('worldedDownload').addEventListener('click', () => chooseWorldEd('download'));
document.getElementById('worldedFind').addEventListener('click', () => chooseWorldEd('find'));

// ---- game location ---------------------------------------------------------------
//
// Either found on its own, or a folder the player picked. The folder has to
// be the install: the one that contains the Project Zomboid jar.

function showGameLocation(data) {
  const field = document.getElementById('gamePath');
  if (field) field.value = data.game || '';
  const note = document.getElementById('steamNote');
  note.className = 'hint';
  if (data.game && data.jar) {
    note.textContent = '';
  } else {
    note.className = 'hint bad';
    note.textContent = data.game
      ? 'That folder does not contain the Project Zomboid jar. Choose the game folder, the one with ProjectZomboid64.jar in it.'
      : 'Project Zomboid was not found. Choose the folder that contains the jar.';
  }
  syncSideSteps();
}

async function saveGameLocation(body) {
  const res = await fetch('/api/game-location', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw apiError(data, res);
  if (data.cancelled) return;
  showGameLocation(data);
  checkSetup();
}

async function loadGameLocation() {
  try {
    showGameLocation(await (await fetch('/api/game-location')).json());
  } catch (_) { /* the choices are already on the page */ }
}

loadGameLocation();

document.getElementById('gameBrowse').addEventListener('click', async () => {
  try {
    await saveGameLocation({
      mode: 'browse',
      path: document.getElementById('gamePath').value,
    });
  } catch (err) {
    const place = document.getElementById('steamNote');
    place.className = 'hint bad';
    place.textContent = err.message;
  }
});

let sideMaster = 'setup';
let generateRunning = false;
let generateComplete = false;
let generateToken = 0;

const SIDE_LABELS = {
  setup: 'Config',
  map: 'Map',
  build: 'Generate',
  edit: 'Edit',
  export: 'Export',
};

// Edit sits between Generate and Export. Off until that step is ready again.
const EDIT_STEP_ENABLED = false;

function showMaster(master) {
  sideMaster = master;
  for (const panel of document.querySelectorAll('.side-panel')) {
    panel.hidden = panel.dataset.master !== master;
  }
  syncSideSteps();
  syncMapDisplay();
}

function sideSteps() {
  const steps = [];
  const seen = new Set();
  for (const panel of document.querySelectorAll('.side-panel')) {
    const master = panel.dataset.master;
    if (!master || seen.has(master)) continue;
    if (!EDIT_STEP_ENABLED && master === 'edit') continue;
    seen.add(master);
    steps.push({ master });
  }
  return steps;
}

function currentSideStep() {
  return sideSteps().findIndex(s => s.master === sideMaster);
}

function sideStepLabel(current, total) {
  const template = 'Step X / Y';
  const translated = (typeof i18n !== 'undefined' && i18n.say && i18n.say(template)) || template;
  return translated.split('X').join(String(current)).split('Y').join(String(total));
}

function sideText(english) {
  return (typeof i18n !== 'undefined' && i18n.say && i18n.say(english)) || english;
}

function configStepReady() {
  const game = document.getElementById('gamePath');
  return !!(game && game.value.trim());
}

function sideStepReady() {
  if (sideMaster === 'setup') return configStepReady();
  if (sideMaster === 'map' || sideMaster === 'edit') return true;
  if (sideMaster === 'build') return generateComplete && !generateRunning;
  return false;
}

// Next on Generate watches these two flags, not the status text. The buildings
// panel writes its own "done" line, so the flags have to be cleared in that
// same place or the result can be on screen while Next stays locked.
function releaseGenerate(token) {
  if (token != null && token !== generateToken) return;
  generateRunning = false;
  syncSideSteps();
}

function finishGenerate(token) {
  if (token != null && token !== generateToken) return;
  generateComplete = true;
  releaseGenerate(token);
}

function showSideHome() {
  const controls = document.getElementById('controls');
  if (controls) controls.classList.add('is-home');
  showMaster('setup');
}

function fillConfigIdentity() {
  const project = document.getElementById('mapName');
  const title = document.getElementById('mapTitle');
  const modId = document.getElementById('modId');
  if (!project || !title || !modId) return;
  if (!project.value.trim()) {
    project.value = (`knoxify_${Date.now()}`)
      .replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '');
  }
  const projectName = project.value.trim();
  if (!title.value.trim() && projectName) title.value = projectName;
  if (!modId.value.trim()) {
    const source = title.value.trim() || projectName;
    modId.value = sanitizedModId(source);
    modIdEdited = modId.value !== modIdFromName(title.value);
  }
}

function syncSideSteps() {
  const steps = sideSteps();
  const i = currentSideStep();
  const last = steps.length - 1;
  const label = i < 0 ? '' : sideStepLabel(i + 1, steps.length);
  const onConfig = sideMaster === 'setup';
  const hideNext = sideMaster === 'export' || (i >= 0 && i >= last);
  const backText = sideText(onConfig ? 'Cancel' : 'Back');
  for (const btn of document.querySelectorAll('.side-prev')) {
    btn.disabled = onConfig ? false : i <= 0;
    if (btn.textContent !== backText) btn.textContent = backText;
  }
  for (const btn of document.querySelectorAll('.side-next')) {
    btn.classList.toggle('is-hidden', hideNext);
    btn.disabled = hideNext || i < 0 || !sideStepReady();
  }
  for (const el of document.querySelectorAll('.side-count')) el.textContent = label;
  syncSectionHeader();
}

function syncSectionHeader() {
  const el = document.getElementById('sideSection');
  if (!el) return;
  const name = SIDE_LABELS[sideMaster];
  if (!name) return;
  const shown = (typeof i18n !== 'undefined' && i18n.say && i18n.say(name)) || name;
  if (el.textContent !== shown) el.textContent = shown;
}

function stepSide(dir) {
  if (dir < 0 && sideMaster === 'setup') {
    showSideHome();
    return;
  }
  if (dir > 0) {
    if (!sideStepReady()) return;
    if (sideMaster === 'setup') fillConfigIdentity();
  }
  const step = sideSteps()[currentSideStep() + dir];
  if (!step) return;
  showMaster(step.master);
}

document.getElementById('controls').addEventListener('click', e => {
  if (e.target.closest('.side-prev')) stepSide(-1);
  else if (e.target.closest('.side-next')) stepSide(1);
});
syncSideSteps();
document.addEventListener('knoxmap-lang', syncSideSteps);

function enterWorkspace() {
  if (document.body.dataset.firstSetup === '1') return;
  document.getElementById('controls').classList.remove('is-home');
}

document.getElementById('mapName').addEventListener('input', syncSideSteps);

document.getElementById('newMapBtn').addEventListener('click', () => {
  generateToken += 1;
  generateRunning = false;
  generateComplete = false;
  showMaster('setup');
  enterWorkspace();
});

document.getElementById('loadMapBtn').addEventListener('click', async () => {
  const btn = document.getElementById('loadMapBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/maps/load', { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    if (data.cancelled) return;
    generateToken += 1;
    generateRunning = false;
    enterWorkspace();
    presentMap(data);
    showMaster('build');
    loadMaps();
  } catch (err) {
    fx.toast('bad', 'Could not open that map', err.message, 8000);
  } finally {
    btn.disabled = false;
  }
});

loadSettings().catch(() => {
  document.getElementById('advanced-body').textContent =
    'Could not load the settings list.';
});

// ---- maps already made -----------------------------------------------------------
//
// A map does not have to be drawn again when KnoxMap is updated: each step
// records the version that ran it (knoxbuild/mapstate.py), so opening one an
// older release made says which steps are out of date - usually Install alone -
// and offers to run just those.

async function loadMaps() {
  const list = document.getElementById('mapList');
  const card = document.getElementById('mapsCard');
  if (!list || !card) return;
  let data;
  try {
    data = await (await fetch('/api/maps')).json();
  } catch (_) { return; }
  if (!data.maps || !data.maps.length) { card.hidden = true; return; }
  card.hidden = false;
  document.getElementById('mapsCount').textContent = `(${data.maps.length})`;
  list.innerHTML = '';
  for (const m of data.maps) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = m.mapName;
    const size = document.createElement('span');
    size.className = 'size';
    size.textContent = `${m.cellsX}×${m.cellsY} cells`;
    li.append(name, size);
    if (m.needs.length) {
      const tag = document.createElement('span');
      tag.className = 'old';
      tag.textContent = m.madeWith === 'an early release' ? 'older release'
        : `made with ${m.madeWith}`;
      li.append(tag);
    }
    const open = document.createElement('button');
    open.type = 'button';
    open.textContent = 'Open';
    open.addEventListener('click', () => openMap(m.mapName));
    li.append(open);
    list.append(li);
  }
}

function latLngRings(rings) {
  return rings.map(ring => ring.map(([lon, lat]) => [lat, lon]));
}

function layerForSavedMap(data) {
  const shape = data && data.shape;
  if (shape && shape.type === 'Polygon' && Array.isArray(shape.coordinates)) {
    return L.polygon(latLngRings(shape.coordinates), SEL_STYLE);
  }
  if (shape && shape.type === 'MultiPolygon' && Array.isArray(shape.coordinates)) {
    const parts = shape.coordinates
      .filter(poly => Array.isArray(poly) && poly.length)
      .map(poly => L.polygon(latLngRings(poly), SEL_STYLE));
    if (parts.length === 1) return parts[0];
    if (parts.length) return L.featureGroup(parts);
  }
  const box = data && data.bbox;
  if (!box) return null;
  const south = Number(box.south);
  const west = Number(box.west);
  const north = Number(box.north);
  const east = Number(box.east);
  if (![south, west, north, east].every(Number.isFinite)) return null;
  return L.rectangle([[south, west], [north, east]], SEL_STYLE);
}

function showLoadedArea(data) {
  const layer = layerForSavedMap(data);
  if (!layer) return;
  setSelection(layer);
  const bounds = layer.getBounds();
  if (bounds && bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] });
}

function presentMap(data) {
  generateToken += 1;
  generateRunning = false;
  generateComplete = true;
  const nameInput = document.getElementById('mapName');
  if (nameInput && data.mapName) nameInput.value = data.mapName;
  const scale = document.getElementById('metersPerTile');
  if (scale && data.metersPerTile != null) {
    const match = [...scale.options].find(o => Number(o.value) === Number(data.metersPerTile));
    if (match) scale.value = match.value;
  }
  syncScaleCards();
  renderResults(data);
  showUpgrade(data);
  showLoadedArea(data);
  syncSideSteps();
}

async function openMap(mapName) {
  let data;
  try {
    const res = await fetch(`/api/maps/${encodeURIComponent(mapName)}`);
    data = await res.json();
    if (!res.ok) throw apiError(data, res);
  } catch (err) {
    fx.toast('bad', 'Could not open that map', err.message, 8000);
    return;
  }
  presentMap(data);
}

function showUpgrade(data) {
  const box = document.getElementById('upgradeNote');
  if (!box) return;
  if (!data.needs || !data.needs.length) { box.hidden = true; box.innerHTML = ''; return; }
  const steps = data.needsLabels.join(' → ');
  box.hidden = false;
  box.innerHTML = '<span></span>';
  // A map this KnoxMap made itself is not "from an earlier release": it is
  // simply a step short, usually the install it has never had.
  box.querySelector('span').textContent = data.madeWith === data.current
    ? `This map still needs: ${steps}.`
    : `This map was made with ${data.madeWith === 'an early release'
        ? 'an earlier release' : 'KnoxMap ' + data.madeWith}. `
      + `Do you want to upgrade it to ${data.current}? It needs: ${steps}.`;
  const go = document.createElement('button');
  go.type = 'button';
  go.textContent = 'Upgrade';
  go.addEventListener('click', () => upgradeMap(data, go));
  const later = document.createElement('button');
  later.type = 'button';
  later.className = 'later';
  later.textContent = 'Not now';
  later.addEventListener('click', () => { box.hidden = true; });
  box.append(go, later);
}

// ---- a picture of the finished map ------------------------------------------------
// Drawn from the compiled cells the game itself loads, so it is the map rather
// than an impression of it - which is why it needs the map compiled, and why
// it takes the better part of a minute and runs on its own thread.
function wirePictures(mapName) {
  const button = document.getElementById('makePictures');
  const note = document.getElementById('pictureNote');
  const shots = document.getElementById('pictureShots');
  if (!button) return;
  const draw = document.getElementById('pictureDraw');
  if (draw) draw.hidden = false;
  bindExportGroups();
  syncPictureButton();
  note.textContent = '';
  shots.hidden = true;
  shots.innerHTML = '';

  const show = files => {
    shots.innerHTML = '';
    const names = { 'town.png': 'The whole map', 'close.png': 'Close up',
                    'inside.png': 'With the roofs off' };
    for (const url of files) {
      const file = url.split('/').pop();
      const link = document.createElement('a');
      link.href = url;
      link.target = '_blank';
      link.title = names[file] || file;
      const img = document.createElement('img');
      img.src = `${url}?t=${Date.now()}`;   // a redraw replaces the same file
      img.alt = link.title;
      link.append(img);
      shots.append(link);
    }
    shots.hidden = files.length === 0;
  };

  button.onclick = async () => {
    button.disabled = true;
    note.className = 'hint';
    note.textContent = 'Drawing the map — this takes a minute…';
    try {
      const shots = pictureShots();
      if (!shots.length) return;
      const res = await fetch('/api/pictures', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mapName, shots }),
      });
      const started = await res.json();
      if (!res.ok) throw apiError(started, res);
      for (;;) {
        await new Promise(r => setTimeout(r, 1500));
        const st = await (await fetch(
          `/api/pictures-status?map=${encodeURIComponent(mapName)}`)).json();
        if (st.state === 'running') continue;
        if (st.error) throw new Error(st.error);
        show(st.files || []);
        note.textContent = 'Click one to see it full size.';
        break;
      }
    } catch (err) {
      note.className = 'hint error';
      note.textContent = err.message || String(err);
    } finally {
      syncPictureButton();
      button.textContent = 'Draw it again';
    }
  };
}


async function upgradeMap(data, button) {
  const box = document.getElementById('upgradeNote');
  const say = text => { box.querySelector('span').textContent = text; };
  for (const b of box.querySelectorAll('button')) b.disabled = true;
  const post = async (url, body) => {
    const res = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const out = await res.json();
    if (!res.ok) throw apiError(out, res);
    return out;
  };
  try {
    for (const step of data.needs) {
      if (step === 'generate') {
        say(`Drawing ${data.mapName} again…`);
        const b = data.bbox || {};
        await post('/api/generate', {
          south: b.south, west: b.west, north: b.north, east: b.east,
          metersPerTile: data.metersPerTile, mapName: data.mapName,
          settings: data.settings, shape: data.shape,
        });
      } else if (step === 'build') {
        say('Building the buildings again…');
        await post('/api/buildings', { mapName: data.mapName, settings: data.settings });
      } else if (step === 'compile') {
        say('Compiling…');
        await post('/api/compile', { mapName: data.mapName });
        for (;;) {
          await new Promise(r => setTimeout(r, 5000));
          const st = await (await fetch(
            `/api/compile-status?map=${encodeURIComponent(data.mapName)}`)).json();
          if (st.state === 'running') {
            say(`Compiling… ${st.cells || 0} of ${st.expected || '?'} cells`);
            continue;
          }
          if (st.error) throw new Error(st.error);
          break;
        }
      } else if (step === 'install') {
        say('Installing…');
        await post('/api/install', { mapName: data.mapName, title: data.mapName,
                                     modId: data.mapName });
      }
    }
    box.innerHTML = '<span></span>';
    box.querySelector('span').textContent =
      `${data.mapName} is up to date with KnoxMap ${data.current}.`;
    fx.toast('ok', 'Map upgraded', `${data.needsLabels.join(', ')} ran again.`);
    loadMaps();
  } catch (err) {
    say(`Could not upgrade it: ${err.message}`);
    for (const b of box.querySelectorAll('button')) b.disabled = false;
    if (button) button.textContent = 'Try again';
  }
}

loadMaps();

// ---- painted map, while generating and building ----
//
// The street map stays up until a bitmap exists. Then each pass replaces
// that picture: terrain, streets, vegetation, and later the plots, buildings
// and yards. It stays up on Generate and on Export. Edit has its own map.
// Back on Map, the street map returns so the area can be changed.

let paintMap = null;
let paintSession = null;
let paintToken = 0;

// Projector.rotation: degrees the generated map is turned counter-clockwise
// so its street grid runs with the tiles (generator/renderer.py). CSS rotate
// is clockwise, so the view uses the opposite sign of that same value.
// The preview image is that bitmap, edges parallel to the frame, nothing
// cropped. paintCover is the scale that puts the same upright bitmap inside
// the pane. Fitting the geographic box and then covering the pane zooms past
// the bitmap and the two pictures no longer show the same ground.
let paintBearing = 0;
let paintCover = 1;
// Leaflet zoom when the bitmap was fitted. Later zooms stay in Leaflet;
// the cover scale stays the one that matched the preview at this zoom.
let paintFitZoom = null;

function unrotateClient(map, clientX, clientY) {
  const rect = map.getContainer().getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const dx = clientX - cx;
  const dy = clientY - cy;
  const rad = (-paintBearing) * Math.PI / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  const inv = paintCover || 1;
  return {
    x: cx + (dx * cos + dy * sin) / inv,
    y: cy + (-dx * sin + dy * cos) / inv,
  };
}

function bearingPoint(obj, x, y) {
  return new Proxy(obj, {
    get(target, prop) {
      if (prop === 'clientX') return x;
      if (prop === 'clientY') return y;
      const value = target[prop];
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

function bearingEvent(e) {
  if ((!paintBearing && paintCover === 1) || !paintMap || !e) return e;
  const touch = e.touches && e.touches.length === 1 ? e.touches[0] : null;
  const src = touch || e;
  if (src.clientX == null) return e;
  const u = unrotateClient(paintMap, src.clientX, src.clientY);
  return new Proxy(e, {
    get(target, prop) {
      if (prop === 'clientX') return u.x;
      if (prop === 'clientY') return u.y;
      // Leaflet reads the finger from touches[0], not the event itself.
      if (prop === 'touches' && touch) return [bearingPoint(touch, u.x, u.y)];
      const value = target[prop];
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

function hookPaintInput(map) {
  if (map._bearingInput) return;
  map._bearingInput = true;
  const drag = map.dragging && map.dragging._draggable;
  if (drag) {
    const down = drag._onDown;
    const move = drag._onMove;
    // Leaflet binds mousedown to the function that exists when dragging is
    // enabled. Replacing _onDown later leaves that listener on the old
    // function, so the press stays in screen pixels while the move handler
    // (looked up on the press) is already in rotated pixels. The map then
    // jumps on the first move after a click.
    const enabled = !!drag._enabled;
    if (enabled) drag.disable();
    drag._onDown = function (e) {
      const result = down.call(this, bearingEvent(e));
      // bearingEvent already turned the drag into container pixels.
      this._parentScale = { x: 1, y: 1 };
      return result;
    };
    drag._onMove = function (e) { return move.call(this, bearingEvent(e)); };
    if (enabled) drag.enable();
  }
  map.mouseEventToContainerPoint = function (e) {
    const u = unrotateClient(this, e.clientX, e.clientY);
    const rect = this.getContainer().getBoundingClientRect();
    const size = this.getSize();
    return L.point(
      size.x / 2 + (u.x - (rect.left + rect.width / 2)),
      size.y / 2 + (u.y - (rect.top + rect.height / 2)),
    );
  };
}

function uprightBitmapBox() {
  if (!paintMap || !paintSession) return null;
  const pts = [];
  for (const piece of paintSession.pieces || []) {
    for (const c of piece.corners || []) {
      if (!c || c.length < 2) continue;
      pts.push(paintMap.latLngToContainerPoint(L.latLng(c[0], c[1])));
    }
  }
  if (pts.length < 2) return null;
  const el = document.getElementById('paint-map');
  const cx = (el ? el.clientWidth : 0) / 2;
  const cy = (el ? el.clientHeight : 0) / 2;
  const rad = (-paintBearing) * Math.PI / 180;
  const cos = Math.cos(rad);
  const sin = Math.sin(rad);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const p of pts) {
    const dx = p.x - cx;
    const dy = p.y - cy;
    const x = dx * cos - dy * sin;
    const y = dx * sin + dy * cos;
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  const width = maxX - minX;
  const height = maxY - minY;
  if (!(width > 8 && height > 8)) return null;
  return { width, height, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 };
}

function updatePaintCover() {
  const el = document.getElementById('paint-map');
  const w = el ? el.clientWidth : 0;
  const h = el ? el.clientHeight : 0;
  if (!w || !h || !paintMap || !paintSession || !paintSession.fitted) {
    paintCover = 1;
    return;
  }
  const box = uprightBitmapBox();
  if (!box) {
    paintCover = 1;
    return;
  }
  const zoom = paintMap.getZoom();
  if (paintFitZoom == null || !Number.isFinite(paintFitZoom)) paintFitZoom = zoom;
  // Container pixels grow with Leaflet zoom. Divide that back out so the
  // cover stays the preview fit and a scroll wheel still zooms.
  const back = Math.pow(2, zoom - paintFitZoom);
  const contain = Math.min(w / (box.width / back), h / (box.height / back));
  paintCover = Number.isFinite(contain) && contain > 0 ? contain : 1;
}

function applyPaintTransform() {
  const el = document.getElementById('paint-map');
  if (!el) return;
  updatePaintCover();
  if (!paintBearing && paintCover <= 1.001) {
    paintCover = 1;
    el.style.transform = '';
    el.style.removeProperty('--paint-unspin');
    el.style.removeProperty('--paint-unscale');
    return;
  }
  const parts = [];
  if (paintBearing) parts.push(`rotate(${-paintBearing}deg)`);
  if (Math.abs(paintCover - 1) > 0.001) parts.push(`scale(${paintCover})`);
  el.style.transformOrigin = 'center center';
  el.style.transform = parts.join(' ');
  if (paintBearing) el.style.setProperty('--paint-unspin', `${paintBearing}deg`);
  else el.style.removeProperty('--paint-unspin');
  if (Math.abs(paintCover - 1) > 0.001) el.style.setProperty('--paint-unscale', String(1 / paintCover));
  else el.style.removeProperty('--paint-unscale');
}

function setPaintBearing(degrees) {
  const next = Number(degrees);
  const value = Number.isFinite(next) ? next : 0;
  if (value !== paintBearing) paintFitZoom = null;
  paintBearing = value;
  applyPaintTransform();
  if (paintMap) hookPaintInput(paintMap);
}

function ensurePaintMap() {
  if (paintMap) return paintMap;
  paintMap = L.map('paint-map', {
    zoomControl: true,
    attributionControl: false,
    zoomAnimation: false,
    zoomSnap: 0,
  });
  paintMap.setView([38.0406, -84.5037], 13);
  paintMap.on('resize', applyPaintTransform);
  hookPaintInput(paintMap);
  return paintMap;
}

function onGenerateStep() {
  return sideMaster === 'build' || sideMaster === 'export';
}

function onEditStep() {
  return sideMaster === 'edit';
}

function hideEditMap() {
  document.getElementById('map-pane').classList.remove('is-editing');
  if (typeof window.knoxEdit?.hide === 'function') window.knoxEdit.hide();
}

function showEditMap() {
  const pane = document.getElementById('map-pane');
  pane.classList.add('is-editing');
  pane.classList.remove('is-painting', 'is-painted');
  if (typeof window.knoxEdit?.show === 'function') window.knoxEdit.show();
  if (window.knoxEditMap) {
    requestAnimationFrame(() => {
      if (window.knoxEditMap) window.knoxEditMap.invalidateSize();
    });
  }
}

function showPaintMap() {
  hideEditMap();
  const pane = document.getElementById('map-pane');
  pane.classList.add('is-painting');
  if (paintSession && paintSession.frame) pane.classList.add('is-painted');
  const m = ensurePaintMap();
  const place = () => {
    applyPaintTransform();
    m.invalidateSize({ pan: false });
    if (!paintSession) return;
    drawPaintPieces();
    if (paintSession.fitted || !paintSession.fit) return;
    if (!m.getSize().x || !m.getSize().y) return;
    m.fitBounds(paintSession.fit, { padding: [0, 0], animate: false });
    paintSession.fitted = true;
    applyPaintTransform();
  };
  place();
  requestAnimationFrame(place);
}

function showMainMap() {
  const pane = document.getElementById('map-pane');
  pane.classList.remove('is-painting', 'is-painted');
  hideEditMap();
  requestAnimationFrame(() => map.invalidateSize());
}

function syncMapDisplay() {
  if (onEditStep()) {
    showEditMap();
    return;
  }
  const ready = (paintSession?.pieces || []).some(p => (p.rev || 0) > 0);
  if (paintSession && onGenerateStep() && ready) showPaintMap();
  else showMainMap();
}

function beginPaintView() {
  paintToken += 1;
  paintFitZoom = null;
  genOverlayTerrainReady = false;
  resetGenOverlays();
  paintSession = {
    layers: new Map(), fitted: false, fit: null, pieces: [],
    frame: false, token: paintToken,
  };
  setPaintBearing(0);
  if (paintMap) paintMap.eachLayer(layer => paintMap.removeLayer(layer));
  syncMapDisplay();
}

function revealPaintFrame() {
  if (!paintSession) return;
  paintSession.frame = true;
  const pane = document.getElementById('map-pane');
  if (pane.classList.contains('is-painting')) pane.classList.add('is-painted');
}

function drawPaintPieces() {
  if (!paintMap || !paintSession) return;
  for (const piece of paintSession.pieces || []) {
    if (!piece.corners || piece.corners.length < 4) continue;
    let entry = paintSession.layers.get(piece.name);
    if (!entry) {
      const outline = L.polygon(piece.corners, {
        color: '#c6d36a', weight: 1, fillColor: '#5a6423', fillOpacity: 0.95,
        interactive: false,
      }).addTo(paintMap);
      entry = { outline, image: null, rev: 0 };
      paintSession.layers.set(piece.name, entry);
    }
    const rev = piece.rev || 0;
    if (rev > 0 && rev !== entry.rev) {
      const url = `/paint/${encodeURIComponent(piece.name)}.png?v=${rev}`;
      const token = paintSession.token;
      const reveal = () => {
        if (!paintSession || paintSession.token !== token) return;
        revealPaintFrame();
      };
      entry.outline.setStyle({ fillOpacity: 0 });
      if (!entry.image) {
        entry.image = new PaintImage(url, piece.corners, reveal).addTo(paintMap);
        entry.outline.bringToFront();
      } else {
        entry.image.setUrl(url);
        if (entry.image._image && entry.image._image.complete && entry.image._image.naturalWidth) {
          reveal();
        } else if (entry.image._image) {
          entry.image._image.onload = reveal;
        }
      }
      entry.rev = rev;
    }
  }
}

function updatePaintPieces(pieces) {
  if (!Array.isArray(pieces) || !pieces.length) return;
  if (!paintSession) {
    paintToken += 1;
    paintSession = {
      layers: new Map(), fitted: false, fit: null, pieces: [],
      frame: false, token: paintToken,
    };
  }
  paintSession.pieces = pieces;
  if (onGenerateStep()) syncGenOverlays(pieces);
  const pts = [];
  for (const piece of pieces) {
    for (const c of piece.corners || []) pts.push([c[0], c[1]]);
  }
  if (pts.length && !paintSession.fit) paintSession.fit = L.latLngBounds(pts);
  const ready = pieces.some(p => (p.rev || 0) > 0);
  if (!ready || onEditStep() || !onGenerateStep()) return;
  const pane = document.getElementById('map-pane');
  if (!pane.classList.contains('is-painting')) {
    showPaintMap();
    return;
  }
  drawPaintPieces();
  if (paintMap && paintSession.fit && !paintSession.fitted && paintMap.getSize().x) {
    paintMap.fitBounds(paintSession.fit, { padding: [0, 0], animate: false });
    paintSession.fitted = true;
    applyPaintTransform();
  }
}

// ---- finished-area overlays ---------------------------------------------
//
// Switches under Buildings, shown once a generate has started. Each one
// draws what that piece has already finished: red building footprints as
// rooms are laid out, biome and foraging colours, land-use zones, streets.

const ZONE_STYLE = {
  residential: ['#3d7ec9', 'Residential'],
  commercial: ['#e07a2f', 'Commercial'],
  industrial: ['#8d6bb5', 'Industrial'],
  military: ['#6b7c3a', 'Military'],
  schoolyard: ['#d4b23a', 'School'],
  hospital_grounds: ['#d45b7a', 'Medical'],
  worship_grounds: ['#c9a227', 'Worship'],
  cemetery: ['#8a8f98', 'Cemetery'],
  parking: ['#6e7784', 'Parking'],
  sports: ['#3aaa6a', 'Sports'],
  airport: ['#4aa8b5', 'Airport'],
  railway: ['#8b5a3c', 'Railway'],
  park: ['#7dbe4a', 'Park'],
  grass: ['#9ccc6a', 'Grass'],
  farmland: ['#c4a15a', 'Farmland'],
  forest: ['#2f6b3a', 'Forest'],
  scrub: ['#6a8f4e', 'Scrub'],
  orchard: ['#88a84a', 'Orchard'],
  wetland: ['#4f8f8a', 'Wetland'],
  playground: ['#e0a040', 'Playground'],
  plaza: ['#b7b1a6', 'Plaza'],
};

const STREET_STYLE = {
  road_major: ['#e24b4b', 'Major road'],
  road_medium: ['#e0a030', 'Medium road'],
  road_minor: ['#f2f0e6', 'Minor road'],
  road_service: ['#9aa3ad', 'Service road'],
  dirt_path: ['#a67c52', 'Dirt path'],
  paved_path: ['#d7d3c8', 'Paved path'],
  road_track: ['#c4b08a', 'Track'],
};

// Same colours as generator/biomes.py OVERLAY_ROWS.
const BIOME_LEGEND = [
  ['#2e78ba', 'Water', 'Water'],
  ['#c4a870', 'Clay shore', 'Forest'],
  ['#789c8a', 'Clay lake', 'Forest'],
  ['#d69c8c', 'Trailer park', 'TrailerPark'],
  ['#b06054', 'Town', 'TownZone'],
  ['#d6b048', 'Farm', 'Farm'],
  ['#c4c460', 'Farmland', 'FarmLand'],
  ['#2e6e48', 'Pine forest', 'PHForest'],
  ['#7a9c40', 'Hardwood forest', 'PRForest'],
  ['#9ab054', 'Farm mix', 'FarmMixForest'],
  ['#488c48', 'Farm forest', 'FarmForest'],
  ['#a8c45c', 'Birch forest', 'BirchForest'],
  ['#70a860', 'Birch mix', 'BirchMixForest'],
  ['#387840', 'Organic forest', 'OrganicForest'],
  ['#8c7c68', 'Bare ground', 'ForagingNav'],
  ['#1c4828', 'Deep forest', 'DeepForest'],
];

let genOverlayTerrainReady = false;
const genOverlayLayers = {
  buildings: new Map(),
  biomes: new Map(),
  zones: new Map(),
  streets: new Map(),
};
const genOverlayMiss = new Set();
const genOverlayLoading = new Set();
const genOverlaySamples = { biomes: new Map(), buildings: new Map() };
let genStreetIndex = [];
let genZoneIndex = [];
let genBuildingIndex = [];
let genStreetLabels = null;
let genOverlayZoomHooked = false;
let genHoverTip = null;
let genHoverAt = null;
const genSampleCanvas = document.createElement('canvas');
genSampleCanvas.width = 1;
genSampleCanvas.height = 1;
const genSampleCtx = genSampleCanvas.getContext('2d', { willReadFrequently: true });
const BIOME_RGB = BIOME_LEGEND.map(([hex, biome, zone]) => ({
  r: parseInt(hex.slice(1, 3), 16),
  g: parseInt(hex.slice(3, 5), 16),
  b: parseInt(hex.slice(5, 7), 16),
  text: `${biome} · ${zone}`,
}));

function overlayWanted(id) {
  const box = document.getElementById(id);
  return !!(box && box.checked && !box.disabled);
}

function showGenOverlays() {
  const root = document.getElementById('genOverlays');
  if (root) root.hidden = false;
  const buildings = document.getElementById('genBuildings');
  const box = document.getElementById('ovBuildings');
  if (box) {
    const on = !buildings || buildings.checked;
    box.disabled = !on;
    if (!on) box.checked = false;
  }
  paintGenLegend();
}

function resetGenOverlays() {
  if (paintMap) {
    for (const group of Object.values(genOverlayLayers)) {
      for (const layer of group.values()) {
        if (paintMap.hasLayer(layer)) paintMap.removeLayer(layer);
      }
    }
    if (genStreetLabels && paintMap.hasLayer(genStreetLabels)) {
      paintMap.removeLayer(genStreetLabels);
    }
  }
  genOverlayLayers.buildings.clear();
  genOverlayLayers.biomes.clear();
  genOverlayLayers.zones.clear();
  genOverlayLayers.streets.clear();
  genOverlayMiss.clear();
  genOverlayLoading.clear();
  genOverlaySamples.biomes.clear();
  genOverlaySamples.buildings.clear();
  genStreetIndex = [];
  genZoneIndex = [];
  genBuildingIndex = [];
  genStreetLabels = null;
  hideOverlayHover();
  paintGenLegend();
}

function ensureOverlayPanes(map) {
  const specs = [
    ['gen-biomes', '420'],
    ['gen-zones', '440'],
    ['gen-buildings', '480'],
    ['gen-streets', '520'],
  ];
  for (const [name, z] of specs) {
    if (map.getPane(name)) continue;
    const pane = map.createPane(name);
    pane.style.zIndex = z;
    pane.style.pointerEvents = 'none';
  }
}

function pieceReady(piece, layer) {
  if (genOverlayTerrainReady && layer !== 'buildings') return true;
  return !!(piece.layers && piece.layers[layer]);
}

function legendHead(text) {
  const head = document.createElement('div');
  head.className = 'leg-head';
  head.textContent = text;
  return head;
}

function legendRow(color, text) {
  const row = document.createElement('div');
  row.className = 'leg-row';
  const swatch = document.createElement('i');
  swatch.style.background = color;
  const label = document.createElement('span');
  label.textContent = text;
  row.append(swatch, label);
  return row;
}

function paintGenLegend() {
  const box = document.getElementById('genOverlayLegend');
  if (!box) return;
  box.replaceChildren();
  const buildings = overlayWanted('ovBuildings');
  const biomes = overlayWanted('ovBiomes');
  const zones = overlayWanted('ovZones');
  const streets = overlayWanted('ovStreets');
  if (!buildings && !biomes && !zones && !streets) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  if (buildings) {
    box.append(legendHead('Buildings'));
    box.append(legendRow('rgb(205, 48, 43)', 'Finished buildings'));
  }
  if (biomes) {
    box.append(legendHead('Biomes'));
    for (const [color, biome, zone] of BIOME_LEGEND) {
      box.append(legendRow(color, `${biome} · ${zone}`));
    }
  }
  if (zones) {
    box.append(legendHead('Zoning'));
    for (const [color, label] of Object.values(ZONE_STYLE)) {
      box.append(legendRow(color, label));
    }
  }
  if (streets) {
    box.append(legendHead('Streets'));
    for (const [color, label] of Object.values(STREET_STYLE)) {
      box.append(legendRow(color, label));
    }
    const note = document.createElement('p');
    note.className = 'leg-note';
    note.textContent = 'Colour is the street type. Thicker lines are wider. Names show when the map is close.';
    box.append(note);
  }
}

function streetWeight(metres) {
  const width = Number(metres);
  const m = Number.isFinite(width) && width > 0 ? width : 4;
  const zoom = paintMap ? paintMap.getZoom() : 14;
  return Math.max(1.25, Math.min(12, m * Math.pow(2, zoom - 16) * 0.35));
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function streetTip(props) {
  const style = STREET_STYLE[props.category];
  const kind = style ? style[1] : (props.category || 'Street');
  const name = props.name || 'Unnamed';
  const width = Number(props.width);
  const metres = Number.isFinite(width) && width > 0 ? `${width} m` : '';
  return [name, kind, metres].filter(Boolean).join(' · ');
}

function restyleStreets() {
  for (const group of genOverlayLayers.streets.values()) {
    group.eachLayer((layer) => {
      const props = (layer.feature && layer.feature.properties) || {};
      layer.setStyle({ weight: streetWeight(props.width) });
    });
  }
  refreshStreetLabels();
}

function refreshStreetLabels() {
  if (!paintMap || !genStreetLabels) return;
  genStreetLabels.clearLayers();
  if (!overlayWanted('ovStreets') || paintMap.getZoom() < 15) return;
  const bounds = paintMap.getBounds();
  const shown = genStreetIndex
    .filter((road) => road.name && bounds.contains(road.mid))
    .sort((a, b) => (b.width || 0) - (a.width || 0))
    .slice(0, 40);
  for (const road of shown) {
    const width = Number(road.width);
    const text = Number.isFinite(width) && width > 0
      ? `${road.name} · ${width} m` : road.name;
    const icon = L.divIcon({
      className: 'gen-street-label',
      html: escapeHtml(text),
      iconSize: null,
    });
    L.marker(road.mid, { icon, interactive: false, pane: 'gen-streets' }).addTo(genStreetLabels);
  }
}

function hookOverlayZoom() {
  if (!paintMap || genOverlayZoomHooked) return;
  genOverlayZoomHooked = true;
  paintMap.on('zoomend moveend', restyleStreets);
}

function clearOverlayKind(kind) {
  for (const name of [...genOverlayLayers[kind].keys()]) dropOverlay(kind, name);
  const prefix = `${kind}:`;
  for (const key of [...genOverlayMiss]) {
    if (key.startsWith(prefix)) genOverlayMiss.delete(key);
  }
  for (const key of [...genOverlayLoading]) {
    if (key.startsWith(prefix)) genOverlayLoading.delete(key);
  }
}

function dropOverlay(kind, name) {
  const layer = genOverlayLayers[kind].get(name);
  if (layer && paintMap && paintMap.hasLayer && paintMap.hasLayer(layer)) {
    paintMap.removeLayer(layer);
  }
  genOverlayLayers[kind].delete(name);
  if (kind === 'streets') {
    genStreetIndex = genStreetIndex.filter((road) => road.piece !== name);
    refreshStreetLabels();
  } else if (kind === 'zones') {
    genZoneIndex = genZoneIndex.filter((area) => area.piece !== name);
  } else if (kind === 'buildings') {
    genBuildingIndex = genBuildingIndex.filter((area) => area.piece !== name);
    genOverlaySamples.buildings.delete(name);
  } else if (kind === 'biomes') {
    genOverlaySamples.biomes.delete(name);
  }
}

function syncGenOverlays(pieces) {
  paintGenLegend();
  const list = Array.isArray(pieces) ? pieces : [];
  const wantBuildings = overlayWanted('ovBuildings');
  const wantBiomes = overlayWanted('ovBiomes');
  const wantZones = overlayWanted('ovZones');
  const wantStreets = overlayWanted('ovStreets');
  if (!wantBuildings && !wantBiomes && !wantZones && !wantStreets) {
    for (const kind of Object.keys(genOverlayLayers)) {
      for (const name of [...genOverlayLayers[kind].keys()]) dropOverlay(kind, name);
    }
    genStreetIndex = [];
    genZoneIndex = [];
    genBuildingIndex = [];
    if (genStreetLabels && paintMap && paintMap.hasLayer(genStreetLabels)) {
      paintMap.removeLayer(genStreetLabels);
    }
    hideOverlayHover();
    return;
  }
  if (!onGenerateStep()) return;
  const map = ensurePaintMap();
  ensureOverlayPanes(map);
  hookOverlayZoom();
  hookOverlayHover();
  const keep = new Set(list.map((piece) => piece && piece.name).filter(Boolean));

  if (!wantBuildings) clearOverlayKind('buildings');
  if (!wantBiomes) clearOverlayKind('biomes');
  if (!wantZones) clearOverlayKind('zones');
  if (!wantStreets) {
    clearOverlayKind('streets');
    genStreetIndex = [];
    if (genStreetLabels && map.hasLayer(genStreetLabels)) map.removeLayer(genStreetLabels);
  }

  for (const piece of list) {
    if (!piece || !piece.name || !piece.corners || piece.corners.length < 4) continue;
    const name = piece.name;
    if (wantBuildings && (pieceReady(piece, 'buildings') || pieceReady(piece, 'zones'))) {
      loadBuildingIndex(name);
    }
    if (wantBuildings && pieceReady(piece, 'buildings')) {
      const rev = piece.buildingsRev || 0;
      if (rev > 0) {
        let image = genOverlayLayers.buildings.get(name);
        const url = `/paint/${encodeURIComponent(name)}_buildings.png?v=${rev}`;
        if (!image) {
          image = new PaintImage(url, piece.corners, null, 'gen-buildings').addTo(map);
          image._rev = rev;
          genOverlayLayers.buildings.set(name, image);
          rememberOverlayImage(name, piece.corners, url, 'buildings');
          loadBuildingIndex(name);
        } else if (image._rev !== rev) {
          image.setUrl(url);
          image._rev = rev;
          if (!map.hasLayer(image)) image.addTo(map);
          rememberOverlayImage(name, piece.corners, url, 'buildings');
        }
      }
    }
    if (wantBiomes && pieceReady(piece, 'biomes') && !genOverlayLayers.biomes.has(name)
        && !genOverlayMiss.has(`biomes:${name}`) && !genOverlayLoading.has(`biomes:${name}`)) {
      loadRasterOverlay(name, piece.corners, 'biomes',
        `/output/${encodeURIComponent(name)}/${encodeURIComponent(name)}_biome_overlay.png`,
        'gen-biomes');
    }
    if (wantZones && pieceReady(piece, 'zones') && !genOverlayLayers.zones.has(name)
        && !genOverlayMiss.has(`zones:${name}`) && !genOverlayLoading.has(`zones:${name}`)) {
      loadZoneOverlay(name);
    }
    if (wantStreets && pieceReady(piece, 'streets') && !genOverlayLayers.streets.has(name)
        && !genOverlayMiss.has(`streets:${name}`) && !genOverlayLoading.has(`streets:${name}`)) {
      loadStreetOverlay(name);
    }
  }

  for (const kind of ['buildings', 'biomes', 'zones', 'streets']) {
    if ((kind === 'buildings' && !wantBuildings) || (kind === 'biomes' && !wantBiomes)
        || (kind === 'zones' && !wantZones) || (kind === 'streets' && !wantStreets)) continue;
    for (const name of [...genOverlayLayers[kind].keys()]) {
      if (!keep.has(name)) dropOverlay(kind, name);
    }
  }
  if (genHoverAt) showOverlayHover(genHoverAt);
}

function loadRasterOverlay(name, corners, kind, url, pane) {
  const key = `${kind}:${name}`;
  if (genOverlayLoading.has(key)) return;
  genOverlayLoading.add(key);
  const image = new Image();
  image.onload = () => {
    genOverlayLoading.delete(key);
    const wanted = kind === 'biomes' ? 'ovBiomes' : 'ovBuildings';
    if (!overlayWanted(wanted) || !paintMap || !paintSession) return;
    if (!(paintSession.pieces || []).some((piece) => piece.name === name)) return;
    if (genOverlayLayers[kind].has(name)) return;
    const layer = new PaintImage(url, corners, null, pane).addTo(paintMap);
    genOverlayLayers[kind].set(name, layer);
    if (kind === 'biomes') genOverlaySamples.biomes.set(name, { img: image, corners });
  };
  image.onerror = () => {
    genOverlayLoading.delete(key);
    genOverlayMiss.add(key);
  };
  image.src = url;
}

function loadZoneOverlay(name) {
  const key = `zones:${name}`;
  if (genOverlayLoading.has(key)) return;
  genOverlayLoading.add(key);
  fetch(`/output/${encodeURIComponent(name)}/${encodeURIComponent(name)}_zones.geojson`)
    .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
    .then((data) => {
      genOverlayLoading.delete(key);
      if (!overlayWanted('ovZones') || !paintMap) return;
      if (genOverlayLayers.zones.has(name)) return;
      genZoneIndex.push(...indexPolygons(data.features, name));
      const layer = L.geoJSON(data, {
        pane: 'gen-zones',
        interactive: false,
        style(feature) {
          const props = (feature && feature.properties) || {};
          const style = ZONE_STYLE[props.category] || ['#888888', props.category || 'Area'];
          return {
            color: style[0], weight: 1, fillColor: style[0], fillOpacity: 0.38,
          };
        },
      }).addTo(paintMap);
      genOverlayLayers.zones.set(name, layer);
    })
    .catch(() => {
      genOverlayLoading.delete(key);
      genOverlayMiss.add(key);
    });
}

function loadStreetOverlay(name) {
  const key = `streets:${name}`;
  if (genOverlayLoading.has(key)) return;
  genOverlayLoading.add(key);
  fetch(`/output/${encodeURIComponent(name)}/${encodeURIComponent(name)}_roads.geojson`)
    .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
    .then((data) => {
      genOverlayLoading.delete(key);
      if (!overlayWanted('ovStreets') || !paintMap) return;
      if (genOverlayLayers.streets.has(name)) return;
      if (!genStreetLabels) {
        genStreetLabels = L.layerGroup().addTo(paintMap);
      } else if (!paintMap.hasLayer(genStreetLabels)) {
        genStreetLabels.addTo(paintMap);
      }
      const layer = L.geoJSON(data, {
        pane: 'gen-streets',
        style(feature) {
          const props = (feature && feature.properties) || {};
          const style = STREET_STYLE[props.category] || ['#dddddd', 'Street'];
          return {
            color: style[0], weight: streetWeight(props.width), opacity: 0.9,
          };
        },
        onEachFeature(feature, line) {
          const props = (feature && feature.properties) || {};
          const coords = (feature.geometry && feature.geometry.coordinates) || [];
          const mid = coords[Math.floor(coords.length / 2)];
          if (mid && mid.length >= 2) {
            let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
            for (const pair of coords) {
              if (!pair || pair.length < 2) continue;
              if (pair[0] < minLon) minLon = pair[0];
              if (pair[1] < minLat) minLat = pair[1];
              if (pair[0] > maxLon) maxLon = pair[0];
              if (pair[1] > maxLat) maxLat = pair[1];
            }
            const width = Number(props.width);
            genStreetIndex.push({
              name: props.name || '',
              category: props.category || '',
              width: Number.isFinite(width) ? width : 0,
              mid: L.latLng(mid[1], mid[0]),
              coords,
              minLon, minLat, maxLon, maxLat,
              piece: name,
            });
          }
        },
      }).addTo(paintMap);
      genOverlayLayers.streets.set(name, layer);
      refreshStreetLabels();
    })
    .catch(() => {
      genOverlayLoading.delete(key);
      genOverlayMiss.add(key);
    });
}

function rememberOverlayImage(name, corners, url, kind) {
  const img = new Image();
  img.onload = () => {
    const wanted = kind === 'biomes' ? 'ovBiomes' : 'ovBuildings';
    if (!overlayWanted(wanted)) return;
    genOverlaySamples[kind].set(name, { img, corners });
  };
  img.src = url;
}

function loadBuildingIndex(name) {
  const key = `buildings:${name}`;
  if (genOverlayMiss.has(key) || genOverlayLoading.has(key)) return;
  if (genBuildingIndex.some((area) => area.piece === name)) return;
  genOverlayLoading.add(key);
  fetch(`/output/${encodeURIComponent(name)}/${encodeURIComponent(name)}_buildings.geojson`)
    .then((res) => {
      if (!res.ok) throw new Error('missing');
      return res.json();
    })
    .then((data) => {
      genOverlayLoading.delete(key);
      if (!overlayWanted('ovBuildings')) return;
      if (genBuildingIndex.some((area) => area.piece === name)) return;
      genBuildingIndex.push(...indexPolygons((data && data.features) || [], name));
    })
    .catch(() => {
      genOverlayLoading.delete(key);
      genOverlayMiss.add(key);
    });
}

function indexPolygons(features, piece) {
  const out = [];
  for (const feature of features || []) {
    const geom = feature && feature.geometry;
    if (!geom) continue;
    const props = (feature.properties) || {};
    const polygons = geom.type === 'Polygon' ? [geom.coordinates]
      : geom.type === 'MultiPolygon' ? geom.coordinates : [];
    for (const polygon of polygons) {
      const ring = polygon && polygon[0];
      if (!ring || ring.length < 3) continue;
      let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
      for (const pair of ring) {
        if (!pair || pair.length < 2) continue;
        if (pair[0] < minLon) minLon = pair[0];
        if (pair[1] < minLat) minLat = pair[1];
        if (pair[0] > maxLon) maxLon = pair[0];
        if (pair[1] > maxLat) maxLat = pair[1];
      }
      out.push({
        props, piece, ring, holes: polygon.slice(1),
        minLon, minLat, maxLon, maxLat, area: Math.abs(ringArea(ring)),
      });
    }
  }
  return out;
}

function ringArea(ring) {
  let sum = 0;
  for (let i = 0, n = ring.length; i < n; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % n];
    if (!a || !b) continue;
    sum += a[0] * b[1] - b[0] * a[1];
  }
  return sum / 2;
}

function ringContains(ring, lon, lat) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i];
    const b = ring[j];
    if (!a || !b || a.length < 2 || b.length < 2) continue;
    const crosses = (a[1] > lat) !== (b[1] > lat)
      && lon < (b[0] - a[0]) * (lat - a[1]) / ((b[1] - a[1]) || 1e-12) + a[0];
    if (crosses) inside = !inside;
  }
  return inside;
}

function polygonContains(area, lon, lat) {
  if (lon < area.minLon || lon > area.maxLon || lat < area.minLat || lat > area.maxLat) return false;
  if (!ringContains(area.ring, lon, lat)) return false;
  for (const hole of area.holes || []) {
    if (hole && ringContains(hole, lon, lat)) return false;
  }
  return true;
}

function smallestHit(index, lon, lat) {
  let best = null;
  for (const area of index) {
    if (!polygonContains(area, lon, lat)) continue;
    if (!best || area.area < best.area) best = area;
  }
  return best;
}

function zoneLabel(props) {
  const style = ZONE_STYLE[props.category] || ['#888', props.category || 'Zone'];
  const name = (props.name || '').trim();
  return name ? `${style[1]} · ${name}` : style[1];
}

function buildingLabel(props) {
  const name = (props.name || '').trim();
  const kind = ['amenity', 'shop', 'leisure', 'tourism', 'healthcare', 'office']
    .map((key) => props[key])
    .find((value) => value && value !== 'yes');
  const kindText = kind ? String(kind).replace(/_/g, ' ') : '';
  if (name && kindText) return `${name} · ${kindText}`;
  if (name) return name;
  if (kindText) return kindText;
  return 'Finished building';
}

function imagePixel(entry, latlng) {
  if (!paintMap || !entry || !entry.img || !entry.img.naturalWidth || !genSampleCtx) return null;
  const p = paintMap.latLngToLayerPoint(latlng);
  const tl = paintMap.latLngToLayerPoint(L.latLng(entry.corners[0][0], entry.corners[0][1]));
  const tr = paintMap.latLngToLayerPoint(L.latLng(entry.corners[1][0], entry.corners[1][1]));
  const bl = paintMap.latLngToLayerPoint(L.latLng(entry.corners[3][0], entry.corners[3][1]));
  const vx = { x: tr.x - tl.x, y: tr.y - tl.y };
  const vy = { x: bl.x - tl.x, y: bl.y - tl.y };
  const dx = p.x - tl.x;
  const dy = p.y - tl.y;
  const det = vx.x * vy.y - vx.y * vy.x;
  if (!det) return null;
  const u = (dx * vy.y - dy * vy.x) / det;
  const v = (vx.x * dy - vx.y * dx) / det;
  if (u < 0 || v < 0 || u > 1 || v > 1) return null;
  const x = Math.min(entry.img.naturalWidth - 1, Math.max(0, Math.floor(u * entry.img.naturalWidth)));
  const y = Math.min(entry.img.naturalHeight - 1, Math.max(0, Math.floor(v * entry.img.naturalHeight)));
  genSampleCtx.clearRect(0, 0, 1, 1);
  genSampleCtx.drawImage(entry.img, x, y, 1, 1, 0, 0, 1, 1);
  return genSampleCtx.getImageData(0, 0, 1, 1).data;
}

function biomeAt(latlng) {
  for (const entry of genOverlaySamples.biomes.values()) {
    const px = imagePixel(entry, latlng);
    if (!px || px[3] < 20) continue;
    let best = null;
    let bestD = 48 * 48;
    for (const row of BIOME_RGB) {
      const d = (px[0] - row.r) ** 2 + (px[1] - row.g) ** 2 + (px[2] - row.b) ** 2;
      if (d < bestD) {
        bestD = d;
        best = row.text;
      }
    }
    if (best) return best;
  }
  return null;
}

function buildingPixel(latlng) {
  for (const entry of genOverlaySamples.buildings.values()) {
    const px = imagePixel(entry, latlng);
    if (px && px[3] >= 30 && px[0] > 120 && px[0] > px[1] + 40 && px[0] > px[2] + 40) return true;
  }
  return false;
}

function distToSeg(p, a, b) {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const t = len ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len)) : 0;
  const x = a.x + t * dx - p.x;
  const y = a.y + t * dy - p.y;
  return Math.hypot(x, y);
}

function nearestStreet(latlng) {
  if (!paintMap) return null;
  const here = paintMap.latLngToLayerPoint(latlng);
  let best = null;
  let bestD = 12;
  for (const road of genStreetIndex) {
    if (!road.coords || road.coords.length < 2) continue;
    const sw = paintMap.latLngToLayerPoint(L.latLng(road.minLat, road.minLon));
    const ne = paintMap.latLngToLayerPoint(L.latLng(road.maxLat, road.maxLon));
    const pad = 14;
    if (here.x < Math.min(sw.x, ne.x) - pad || here.x > Math.max(sw.x, ne.x) + pad) continue;
    if (here.y < Math.min(sw.y, ne.y) - pad || here.y > Math.max(sw.y, ne.y) + pad) continue;
    let prev = null;
    for (const pair of road.coords) {
      if (!pair || pair.length < 2) continue;
      const pt = paintMap.latLngToLayerPoint(L.latLng(pair[1], pair[0]));
      if (prev) {
        const d = distToSeg(here, prev, pt);
        if (d < bestD) {
          bestD = d;
          best = road;
        }
      }
      prev = pt;
    }
  }
  return best;
}

function overlayHoverLines(latlng) {
  const lines = [];
  if (!latlng) return lines;
  if (overlayWanted('ovBiomes')) {
    const text = biomeAt(latlng);
    if (text) lines.push({ label: 'Biomes', value: text });
  }
  if (overlayWanted('ovZones')) {
    const area = smallestHit(genZoneIndex, latlng.lng, latlng.lat);
    if (area) lines.push({ label: 'Zoning', value: zoneLabel(area.props) });
  }
  if (overlayWanted('ovBuildings')) {
    const hit = smallestHit(genBuildingIndex, latlng.lng, latlng.lat);
    if (hit) lines.push({ label: 'Buildings', value: buildingLabel(hit.props) });
    else if (buildingPixel(latlng)) lines.push({ label: 'Buildings', value: 'Finished building' });
  }
  if (overlayWanted('ovStreets')) {
    const road = nearestStreet(latlng);
    if (road) {
      lines.push({
        label: 'Streets',
        value: streetTip({ name: road.name, category: road.category, width: road.width }),
      });
    }
  }
  return lines;
}

function ensureHoverTip() {
  if (genHoverTip) return genHoverTip;
  const tip = document.createElement('div');
  tip.className = 'gen-hover-tip';
  tip.hidden = true;
  document.body.appendChild(tip);
  genHoverTip = tip;
  return tip;
}

function hideOverlayHover() {
  if (genHoverTip) genHoverTip.hidden = true;
}

function placeOverlayHover(tip, ev) {
  const x = ev.originalEvent.clientX + 14;
  const y = ev.originalEvent.clientY + 16;
  tip.hidden = false;
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  const left = x + w > window.innerWidth - 8 ? Math.max(8, ev.originalEvent.clientX - w - 12) : x;
  const top = y + h > window.innerHeight - 8 ? Math.max(8, ev.originalEvent.clientY - h - 12) : y;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}

function showOverlayHover(ev) {
  if (!ev || !ev.latlng || !ev.originalEvent) return;
  genHoverAt = ev;
  const lines = overlayHoverLines(ev.latlng);
  const tip = ensureHoverTip();
  if (!lines.length) {
    tip.hidden = true;
    return;
  }
  tip.replaceChildren();
  for (const line of lines) {
    const row = document.createElement('div');
    const key = document.createElement('span');
    key.className = 'gen-hover-k';
    key.textContent = line.label;
    const value = document.createElement('span');
    value.textContent = line.value;
    row.append(key, value);
    tip.append(row);
  }
  placeOverlayHover(tip, ev);
}

function hookOverlayHover() {
  if (!paintMap || paintMap._genHoverHooked) return;
  paintMap._genHoverHooked = true;
  let frame = 0;
  let pending = null;
  paintMap.on('mousemove', (ev) => {
    if (!ev.latlng || !ev.originalEvent) return;
    pending = {
      latlng: L.latLng(ev.latlng.lat, ev.latlng.lng),
      originalEvent: {
        clientX: ev.originalEvent.clientX,
        clientY: ev.originalEvent.clientY,
      },
    };
    if (frame) return;
    frame = requestAnimationFrame(() => {
      frame = 0;
      const snap = pending;
      if (!snap) return;
      if (!overlayWanted('ovBiomes') && !overlayWanted('ovZones')
          && !overlayWanted('ovBuildings') && !overlayWanted('ovStreets')) {
        hideOverlayHover();
        return;
      }
      showOverlayHover(snap);
    });
  });
  paintMap.on('mouseout', () => {
    genHoverAt = null;
    hideOverlayHover();
  });
}

document.getElementById('genOverlays').addEventListener('change', () => {
  syncGenOverlays(paintSession && paintSession.pieces);
});

// ---- generation ----
//
// Furnishing buildings follows a finished terrain pass when this is on.

const generateOptions = { buildings: true, paperMap: true };

function readGenerateOptions() {
  const buildings = document.getElementById('genBuildings');
  const paperMap = document.getElementById('genPaperMap');
  if (buildings) generateOptions.buildings = buildings.checked;
  if (paperMap) generateOptions.paperMap = paperMap.checked;
  const box = document.getElementById('ovBuildings');
  const root = document.getElementById('genOverlays');
  if (box && root && !root.hidden) {
    box.disabled = !generateOptions.buildings;
    if (!generateOptions.buildings && box.checked) {
      box.checked = false;
      syncGenOverlays(paintSession && paintSession.pieces);
    }
  }
  return generateOptions;
}

document.getElementById('generateOptions').addEventListener('change', readGenerateOptions);

document.getElementById('generateBtn').addEventListener('click', async () => {
  if (!currentRect) return;
  const b = currentRect.getBounds();

  // Name it here rather than letting the server invent one, so progress can be
  // polled under a key the page already knows.
  const nameField = document.getElementById('mapName');
  const chosen = (nameField.value.trim() || `knoxify_${Date.now()}`)
    .replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '');
  nameField.value = chosen;

  const bb = rectBounds(currentRect);
  const body = {
    south: bb.s,
    west: bb.w,
    north: bb.n,
    east: bb.e,
    metersPerTile: parseFloat(document.getElementById('metersPerTile').value),
    mapName: chosen,
    settings: readSettings(),
    shape: selectionShape(),
  };

  const btn = document.getElementById('generateBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.className = '';
  status.textContent = 'Starting…';
  fx.step('style', 'done');
  fx.step('terrain', 'running');
  fx.overlay.show();
  beginPaintView();
  showGenOverlays();
  showStop(chosen);
  startProgress(chosen);
  const token = ++generateToken;
  generateRunning = true;
  generateComplete = false;
  syncSideSteps();
  let furnishBuildings = false;

  try {
    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (wasStopped(data, res)) {
      status.className = '';
      status.textContent = 'Stopped. The area is still drawn - press Generate map to start again.';
      fx.overlay.hide();
      fx.step('terrain', '');
      fx.toast('ok', 'Stopped', 'The area and the settings are as you left them.');
      return;
    }
    if (!res.ok) throw apiError(data, res);

    status.className = 'success';
    const added = Number(data.fromOverture || 0);
    const streets = Number(data.fromStreets || 0);
    const gapNote = data.overtureError
      ? data.overtureError
      : (added
        ? `${added.toLocaleString()} buildings added where OpenStreetMap had none.`
        : '');
    const streetNote = streets
      ? `${streets.toLocaleString()} houses added along streets that had none.`
      : '';
    const extra = [gapNote, streetNote].filter(Boolean).join(' ');
    status.textContent = `Done in ${data.osmSeconds}s (OSM query). ${data.featureCount} features rendered.`
      + (extra ? ` ${extra}` : '');
    fx.overlay.done(`${data.featureCount.toLocaleString()} features on the map`);
    fx.step('terrain', 'done');
    fx.toast('ok', 'Terrain generated',
             `${data.cellsX} × ${data.cellsY} cells from ${data.featureCount.toLocaleString()} features.`
             + (extra ? ` ${extra}` : ''));
    if (token === generateToken) generateComplete = true;
    genOverlayTerrainReady = true;
    syncGenOverlays(paintSession && paintSession.pieces);
    renderResults(data);
    loadMaps();
    furnishBuildings = readGenerateOptions().buildings;
  } catch (err) {
    if (token === generateToken) generateComplete = false;
    status.className = 'error';
    status.textContent = `Error: ${err.message}`;
    fx.overlay.fail(err.message);
    fx.step('terrain', 'error');
    fx.problem('Generation failed', err.message, err.errorId);
  } finally {
    stopProgress();
    showStop(null);
    btn.disabled = false;
    if (token === generateToken && !furnishBuildings) releaseGenerate(token);
  }
  if (furnishBuildings) {
    try {
      await generateBuildings(token);
    } finally {
      releaseGenerate(token);
    }
  }
});

// ---- stopping a long step ----
//
// Generating, building and compiling take minutes, and the only way out of
// one used to be closing the window - which threw the drawn rectangle away
// with it. The Stop button asks the server to drop the job where it can be
// dropped safely; nothing here touches the selection, the settings or the
// map name, so pressing the step again starts the same map over.

let stoppingMap = null;

function showStop(mapName) {
  stoppingMap = null;
  document.querySelectorAll('[data-stop]').forEach(b => {
    b.hidden = !mapName;
    b.disabled = false;
    b.textContent = 'Stop';
  });
  runningMap = mapName;
}

let runningMap = null;

async function requestStop() {
  if (!runningMap || stoppingMap === runningMap) return;
  stoppingMap = runningMap;
  document.querySelectorAll('[data-stop]').forEach(b => {
    b.disabled = true;
    b.textContent = 'Stopping…';
  });
  try {
    await fetch('/api/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapName: runningMap }),
    });
  } catch (_) { /* the step's own reply is the source of truth */ }
}

document.addEventListener('click', ev => {
  if (ev.target.closest('[data-stop]')) requestStop();
});

// Electron asks this before the window closes. A generate, a build or a
// compile keeps WorldEd and the request alive; closing without asking used
// to leave them running. The line is in lang/english.txt, like the rest.
window.knoxmapBeforeClose = async () => {
  if (!runningMap) return true;
  const line = 'A map is still being made. Stop it and close KnoxMap?';
  const shown = (typeof i18n !== 'undefined' && i18n.say && i18n.say(line)) || line;
  if (!window.confirm(shown)) return false;
  await requestStop();
  return true;
};

// A reply the server sends when a step was stopped on purpose. Not an error:
// no red, no problem report, and the area stays drawn.
function wasStopped(data, res) {
  return res.status === 409 && data && data.stopped;
}

// ---- progress for long generations ----
//
// A 400 km² map is dozens of Overpass queries and minutes of rendering. The
// request itself stays open the whole time, so the page polls a side channel
// to show what stage it is at rather than sitting on a dead spinner.

let progressTimer = null;
let progressEpoch = 0;

function startProgress(mapName) {
  const epoch = ++progressEpoch;
  clearInterval(progressTimer);
  progressTimer = setInterval(async () => {
    if (epoch !== progressEpoch) return;
    try {
      const res = await fetch(`/api/progress?map=${encodeURIComponent(mapName)}`);
      const p = await res.json();
      if (epoch !== progressEpoch) return;
      const overlay = document.getElementById('gen-overlay');
      if (!overlay || overlay.dataset.mode !== 'buildings') fx.overlay.update(p);
      updatePaintPieces(p.pieces);
      if (p.rotation != null) setPaintBearing(p.rotation);
      const status = document.getElementById('status');
      if (p.view && (p.stage === 'mod' || p.stage === 'render')) {
        status.textContent = p.view;
      } else if (p.stage === 'osm') {
        const total = p.total || 1;
        status.textContent = total > 1
          ? `Querying OpenStreetMap — area ${(p.done || 0) + 1} of ${total}…`
          : 'Querying OpenStreetMap…';
      } else if (p.stage === 'overture') {
        // A few minutes, nearly all of it Overture's own files being sifted
        // for the handful that cover this box. Saying so beats a dead bar.
        status.textContent = 'Looking up the buildings OpenStreetMap has not got '
                           + '— this takes a few minutes the first time…';
      } else if (p.stage === 'render') {
        status.textContent = `Rendering ${p.features.toLocaleString()} features `
                           + 'into bitmaps…';
      }
    } catch (_) { /* the generate call is the source of truth */ }
  }, 1500);
}

function stopProgress() {
  progressEpoch += 1;
  clearInterval(progressTimer);
  progressTimer = null;
}

function exportBoxes(id) {
  const root = document.getElementById(id);
  const by = {};
  if (!root) return by;
  root.querySelectorAll('input[type="checkbox"]').forEach(el => { by[el.value] = el; });
  return by;
}

function exportParts(id) {
  return Object.values(exportBoxes(id)).filter(el => el.checked).map(el => el.value);
}

function pictureShots() {
  return exportParts('pictureOptions');
}

function syncPictureButton() {
  const button = document.getElementById('makePictures');
  if (!button || button.hidden) return;
  button.disabled = pictureShots().length === 0;
}

let installReady = false;
let exportBusy = false;
let compileRunning = false;

function exportSelection() {
  return {
    compile: !!document.getElementById('optCompile')?.checked,
    install: !!document.getElementById('optInstall')?.checked,
    editable: !!document.getElementById('optEditable')?.checked,
  };
}

function applyExportLocations(data) {
  if (!data) return;
  const output = document.getElementById('exportOutputPath');
  const install = document.getElementById('exportInstallPath');
  if (output && !output.value && data.output_dir) output.value = data.output_dir;
  if (install && !install.value && data.mods_dir) install.value = data.mods_dir;
}

function syncExportButton() {
  const btn = document.getElementById('exportBtn');
  if (!btn) return;
  const sel = exportSelection();
  btn.disabled = exportBusy || compileRunning || !(sel.compile || sel.install || sel.editable);
}

function syncExportOptions() {
  const sel = exportSelection();
  const row = document.getElementById('exportOutputRow');
  const installRow = document.getElementById('exportInstallRow');
  if (installRow) installRow.hidden = !sel.install;
  if (row) {
    const slot = sel.compile
      ? document.getElementById('compileOutputSlot')
      : document.getElementById('editableOutputSlot');
    if (sel.compile || sel.editable) {
      if (slot && row.parentElement !== slot) slot.appendChild(row);
      row.hidden = false;
    } else {
      row.hidden = true;
    }
  }
  syncExportButton();
}

function syncInstallButton() {
  syncExportButton();
}

function bindExportGroups() {
  const pictures = document.getElementById('pictureOptions');
  if (pictures && !pictures.dataset.bound) {
    pictures.dataset.bound = '1';
    pictures.addEventListener('change', syncPictureButton);
  }
  const choices = document.getElementById('exportChoices');
  if (choices && !choices.dataset.bound) {
    choices.dataset.bound = '1';
    choices.addEventListener('change', ev => {
      if (!ev.target || ev.target.type !== 'checkbox') return;
      syncExportOptions();
    });
  }
  syncExportOptions();
}

bindExportGroups();

function renderResults(data) {
  const downloadReady = document.getElementById('downloadReady');
  if (downloadReady) downloadReady.hidden = false;
  const preview = document.getElementById('previewImg');
  const previewLink = document.getElementById('previewLink');
  if (preview && data.files && data.files.preview) {
    preview.src = data.files.preview + '?t=' + Date.now();
    if (previewLink) previewLink.href = data.files.preview;
  }

  const info = document.getElementById('results-info');
  if (info) {
    info.innerHTML = `<div class="tiles">
      ${fx.tile(data.width, '', 'tiles wide')}
      ${fx.tile(data.height, '', 'tiles tall')}
      <div class="tile"><div class="v"><span data-count="${data.cellsX}">0</span><small>×</small><span
        data-count="${data.cellsY}">0</span></div><div class="k">cells</div></div>
      ${fx.tile(data.featureCount, '', 'osm features')}
      ${data.modCount ? fx.tile(data.modCount, '', data.modCount === 1 ? 'mod' : 'mods') : ''}
    </div>`;
    fx.countUp(info);
  }

  bindExportGroups();
  const note = document.getElementById('saveNote');
  if (note) { note.className = 'hint'; note.textContent = ''; }
  wirePictures(data.mapName);

  document.querySelectorAll('.map-badge').forEach(el => {
    el.textContent = data.mapName;
  });

  setupPipeline(data);
  if (data.settings) applySavedMapSettings(data.settings);
  if (data.rotation != null) setPaintBearing(data.rotation);
}

// ---- place search ---------------------------------------------------------
//
// Nominatim finds a place by name; picking a result both flies the map there
// and drops a selection rectangle, so "school -> map" is two clicks. A result's
// own bounding box is used when it is a sensible size, otherwise we centre a
// default-sized box on it - searching a whole city should not hand you a
// 200 km² selection the generator will refuse.

const DEFAULT_BOX_KM = 1.2;
const MIN_BOX_KM = 0.4;

const searchInput = document.getElementById('searchInput');
const searchResults = document.getElementById('search-results');
const searchModeButtons = [...document.querySelectorAll('[data-search-mode]')];
let searchMode = 'place';
let searchTimer = null;
let searchMarker = null;

function setRectFromBounds(bounds) {
  setSelection(L.rectangle(bounds, SEL_STYLE));
}

// A place's own boundary from the search result, as the selection.
function setOutline(geojson) {
  const flip = rings => rings.map(ring => ring.map(([lon, lat]) => [lat, lon]));
  const latlngs = geojson.type === 'Polygon' ? flip(geojson.coordinates)
                                             : geojson.coordinates.map(flip);
  setSelection(L.polygon(latlngs, SEL_STYLE));
  return currentRect.getBounds();
}

function boundsAround(lat, lon, km) {
  const dLat = km / 111.32 / 2;
  const dLon = km / (111.32 * Math.cos(lat * Math.PI / 180)) / 2;
  return L.latLngBounds([lat - dLat, lon - dLon], [lat + dLat, lon + dLon]);
}

function boundsForResult(r) {
  const [s, w, n, e] = r.bbox;
  const km2 = bboxAreaKm2(s, w, n, e);
  const widthKm = haversineKm(s, w, s, e);
  const heightKm = haversineKm(s, w, n, w);
  if (km2 <= BIG_AREA_KM2 && widthKm >= MIN_BOX_KM && heightKm >= MIN_BOX_KM) {
    return L.latLngBounds([s, w], [n, e]);
  }
  return boundsAround(r.lat, r.lon, DEFAULT_BOX_KM);
}

function hideSearchResults() {
  searchResults.hidden = true;
  searchResults.innerHTML = '';
}

function renderSearchResults(results) {
  if (!results.length) {
    searchResults.innerHTML = `<li class="empty">${
      searchMode === 'region' ? 'No administrative regions with an OSM border found.' : 'No matches.'
    }</li>`;
    searchResults.hidden = false;
    return;
  }
  searchResults.innerHTML = results.map((r, i) => {
    const kind = r.region_type || [r.type, r.category].filter(Boolean)[0] || '';
    const rest = r.display_name.split(',').slice(1).join(',').trim();
    return `<li data-i="${i}">
      <span class="r-name">${escapeHtml(r.name)}</span>
      ${kind ? `<span class="r-kind">${escapeHtml(kind.replace(/_/g, ' '))}</span>` : ''}
      ${r.outline ? `<button type="button" class="r-outline" data-outline="${i}"
        title="Select its OpenStreetMap border">select border</button>` : ''}
      <span class="r-where">${escapeHtml(rest)}</span>
    </li>`;
  }).join('');
  searchResults.hidden = false;

  const selectBorder = (r) => {
    const bounds = setOutline(r.outline);
    map.fitBounds(bounds, { padding: [30, 30] });
    if (searchMarker) {
      map.removeLayer(searchMarker);
      searchMarker = null;
    }
    hideSearchResults();
    searchInput.value = r.name;
  };

  searchResults.querySelectorAll('button[data-outline]').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const r = results[parseInt(btn.dataset.outline, 10)];
      selectBorder(r);
    });
  });

  searchResults.querySelectorAll('li[data-i]').forEach(li => {
    li.addEventListener('click', () => {
      const r = results[parseInt(li.dataset.i, 10)];
      if (searchMode === 'region' && r.outline) {
        selectBorder(r);
        return;
      }
      const bounds = boundsForResult(r);
      map.fitBounds(bounds, { padding: [30, 30] });
      setRectFromBounds(bounds);
      if (searchMarker) map.removeLayer(searchMarker);
      searchMarker = L.marker([r.lat, r.lon]).addTo(map)
        .bindPopup(escapeHtml(r.name)).openPopup();
      hideSearchResults();
      searchInput.value = r.name;
    });
  });
}

const SEARCH_DEBOUNCE_MS = 400;
let searchSeq = 0;

async function runSearch(q) {
  const seq = ++searchSeq;
  if (!q.trim()) return hideSearchResults();
  searchResults.innerHTML = '<li class="empty">Searching…</li>';
  searchResults.hidden = false;
  const params = new URLSearchParams({ q });
  if (searchMode === 'region') params.set('regions', '1');
  try {
    const res = await fetch('/api/search?' + params.toString());
    const data = await res.json();
    if (seq !== searchSeq) return;
    if (!res.ok) throw apiError(data, res);
    renderSearchResults(data.results);
  } catch (err) {
    if (seq !== searchSeq) return;
    searchResults.innerHTML = `<li class="empty">${escapeHtml(err.message)}</li>`;
  }
}

// Search after a short pause while typing. Enter still searches immediately.
// The server keeps Nominatim to one request a second; a newer query replaces
// one that has not come back yet, so keystrokes do not each become a request.
function scheduleSearch() {
  clearTimeout(searchTimer);
  if (!searchInput.value.trim()) {
    searchSeq++;
    hideSearchResults();
    return;
  }
  searchTimer = setTimeout(() => runSearch(searchInput.value), SEARCH_DEBOUNCE_MS);
}

searchModeButtons.forEach(btn => {
  btn.addEventListener('click', () => {
    searchMode = btn.dataset.searchMode === 'region' ? 'region' : 'place';
    searchModeButtons.forEach(other => {
      const on = other === btn;
      other.classList.toggle('is-on', on);
      other.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    searchInput.placeholder = searchMode === 'region'
      ? 'Search city, council, state, country…'
      : 'Search for a place';
    clearTimeout(searchTimer);
    if (searchInput.value.trim()) runSearch(searchInput.value);
    else hideSearchResults();
    searchInput.focus();
  });
});

searchInput.addEventListener('input', scheduleSearch);
searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    clearTimeout(searchTimer);
    runSearch(searchInput.value);
  }
  if (e.key === 'Escape') {
    clearTimeout(searchTimer);
    searchSeq++;
    hideSearchResults();
  }
});
document.addEventListener('click', (e) => {
  if (!document.getElementById('search-box').contains(e.target)) {
    clearTimeout(searchTimer);
    searchSeq++;
    hideSearchResults();
  }
});

// ---- landmarks inside the selection ---------------------------------------

let landmarkMarkers = L.layerGroup().addTo(map);

const landmarksBtn = document.getElementById('landmarksBtn');
if (landmarksBtn) landmarksBtn.addEventListener('click', async () => {
  if (!currentRect) return;
  const bb = rectBounds(currentRect);
  const btn = document.getElementById('landmarksBtn');
  const out = document.getElementById('landmark-results');
  btn.disabled = true;
  out.innerHTML = '<div class="hint">Looking…</div>';

  try {
    const res = await fetch('/api/landmarks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        south: bb.s, west: bb.w, north: bb.n, east: bb.e,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    renderLandmarks(data.landmarks);
  } catch (err) {
    out.innerHTML = `<div class="error">${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

function renderLandmarks(items) {
  const out = document.getElementById('landmark-results');
  landmarkMarkers.clearLayers();
  if (!items.length) {
    out.innerHTML = '<div class="hint">Nothing named in this area.</div>';
    return;
  }
  const groups = {};
  items.forEach(it => { (groups[it.value] ||= []).push(it); });
  const order = Object.keys(groups).sort((a, b) =>
    groups[b].length - groups[a].length || a.localeCompare(b));

  out.innerHTML = `<div class="hint">${items.length} named landmarks</div>` +
    order.map(k => `
      <details>
        <summary>${escapeHtml(k.replace(/_/g, ' '))}
          <span class="count">${groups[k].length}</span></summary>
        <ul class="landmark-list">
          ${groups[k].map(it =>
            `<li data-lat="${it.lat}" data-lon="${it.lon}">${escapeHtml(it.name)}</li>`
          ).join('')}
        </ul>
      </details>`).join('');

  out.querySelectorAll('li[data-lat]').forEach(li => {
    li.addEventListener('click', () => {
      const lat = parseFloat(li.dataset.lat), lon = parseFloat(li.dataset.lon);
      map.setView([lat, lon], Math.max(map.getZoom(), 17));
      landmarkMarkers.clearLayers();
      L.circleMarker([lat, lon], {
        radius: 8, color: '#ffd23c', weight: 2, fillOpacity: 0.4,
      }).addTo(landmarkMarkers).bindPopup(li.textContent).openPopup();
    });
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---- steps 3-5: buildings, compile, install -------------------------------
//
// Everything after terrain generation, driven from this one window. The only
// part that is not automated is WorldEd's BMP to TMX and Generate Lots, which
// exist solely as menu commands - so we launch WorldEd on the project and then
// poll for the compiled cells instead of asking the user to report back.

let currentMap = null;
window.knoxCurrentMap = () => currentMap;
let lotsPoll = null;

function note(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'step-note' + (cls ? ' ' + cls : '');
  fx.noted(id, text, cls);
  if (cls === 'bad') {
    const what = { buildingsNote: 'Building failed', compileNote: 'Compile failed',
                   worldedNote: 'WorldEd failed', installNote: 'Install failed',
                   censusNote: 'Recount failed' }[id] || 'Something went wrong';
    fx.problem(what, text, lastErrorId);
    lastErrorId = null;
  }
}

// Mod id follows Mod name, with spaces written as underscores, until the
// user types a different id of their own.
let modIdEdited = false;

function modIdFromName(name) {
  return String(name == null ? '' : name).replace(/ /g, '_');
}

function sanitizedModId(name) {
  return modIdFromName(name).replace(/[^A-Za-z0-9_-]+/g, '_').replace(/^_+|_+$/g, '');
}

function applyDerivedModId() {
  const title = document.getElementById('mapTitle');
  const modId = document.getElementById('modId');
  if (!title || !modId || modIdEdited) return;
  modId.value = modIdFromName(title.value);
}

(function wireModNameFields() {
  const title = document.getElementById('mapTitle');
  const modId = document.getElementById('modId');
  if (!title || !modId) return;
  title.addEventListener('input', applyDerivedModId);
  modId.addEventListener('input', () => {
    modIdEdited = modId.value !== modIdFromName(title.value);
  });
  if (modId.value === '' || modId.value === modIdFromName(title.value)) {
    modIdEdited = false;
    applyDerivedModId();
  } else {
    modIdEdited = true;
  }
})();

function setupPipeline(data) {
  currentMap = data.mapName;
  const upgrade = document.getElementById('upgradeNote');
  if (upgrade) { upgrade.hidden = true; upgrade.innerHTML = ''; }
  document.getElementById('mapTitle').value = data.mapName;
  modIdEdited = false;
  applyDerivedModId();
  installReady = false;
  syncExportButton();
  ['buildings', 'compile', 'install'].forEach(k => fx.card(k, null));
  fx.card('buildings', 'ready');
  fx.resetFrom('buildings');
  const compileBar = document.getElementById('compileBar');
  if (compileBar) compileBar.style.width = '0%';
  note('buildingsNote', 'Turns every OSM footprint into a furnished building.');
  note('compileNote', '');
  note('worldedNote', '');
  note('installNote', '');
  renderCensus(null);
  const retry = document.getElementById('retryCellsBtn');
  if (retry) retry.hidden = true;
  checkLots();
  // Whatever the last compile of this map left behind, said again now: the
  // window has been closed and reopened since, and "done" would be a lie.
  fetch(`/api/compile-status?map=${encodeURIComponent(data.mapName)}`)
    .then(r => r.json())
    .then(p => { if (p.state !== 'running') showFailedCells(p.failed, p.cells); })
    .catch(() => { /* nothing to add if it cannot be asked */ });
}

let buildingsJob = false;

async function generateBuildings(token) {
  if (buildingsJob || !currentMap) return;
  buildingsJob = true;
  const card = document.querySelector('#tabPanelGenerate .pipe-card[data-step="buildings"]');
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  note('buildingsNote', 'Generating buildings…');
  fx.progress('buildings', 1);
  showStop(currentMap);
  fx.overlay.buildings({
    stage: 'Starting buildings',
    detail: 'waiting for the first status',
    pct: 1,
  });
  startBuildProgress(currentMap);
  try {
    const res = await fetch('/api/buildings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // Sent again so the buildings can be regenerated with different
      // settings without re-downloading the town from OSM.
      body: JSON.stringify({
        mapName: currentMap,
        settings: readSettings(),
        paperMap: readGenerateOptions().paperMap,
      }),
    });
    stopBuildProgress();
    const data = await res.json();
    if (wasStopped(data, res)) {
      note('buildingsNote', 'Stopped. The terrain and the area are still here.');
      fx.card('buildings', 'ready');
      fx.progress('buildings', 0);
      fx.overlay.hide();
      fx.toast('ok', 'Stopped', 'Nothing was thrown away.');
      return;
    }
    if (!res.ok) throw apiError(data, res);
    try {
      const progress = await (await fetch(
        `/api/progress?map=${encodeURIComponent(currentMap)}`)).json();
      updatePaintPieces(progress.pieces);
    } catch (_) { /* the last poll already drew what it could */ }
    fx.progress('buildings', 100);
    fx.overlay.done(`${Number(data.count || 0).toLocaleString()} buildings`);
    note('buildingsNote', `${data.count} buildings → ${data.pzw}`, 'ok');
    renderCensus(data.population);
    note('compileNote', '');
    finishGenerate(token);
  } catch (err) {
    note('buildingsNote', err.message, 'bad');
    fx.progress('buildings', 0);
    fx.overlay.fail(err.message);
  } finally {
    stopBuildProgress();
    showStop(null);
    buildingsJob = false;
  }
}

let buildProgressTimer = null;
let buildPaintTimer = null;
let buildProgressToken = 0;
let buildSnap = null;
let buildSnapAt = 0;

function formatLeft(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return null;
  const s = Math.max(0, Math.round(Number(seconds)));
  if (s < 5) return 'a few seconds left';
  if (s < 60) return `${s}s left`;
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m < 60) return r ? `${m}m ${r}s left` : `${m}m left`;
  return `${Math.floor(m / 60)}h ${m % 60}m left`;
}

function formatElapsed(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return `${s}s elapsed`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s elapsed` : `${m}m elapsed`;
  return `${Math.floor(m / 60)}h ${m % 60}m elapsed`;
}

function buildingFraction(p) {
  if (p && typeof p.fraction === 'number' && !Number.isNaN(p.fraction)) {
    return Math.max(0, Math.min(1, p.fraction));
  }
  const mods = (p && p.mods) || 1;
  const frac = p && p.total ? (p.done || 0) / p.total : 0;
  return (Math.max(((p && p.mod) || 1) - 1, 0) + frac) / mods;
}

function progressElapsed(p) {
  if (!p) return null;
  if (typeof p.elapsed === 'number' && !Number.isNaN(p.elapsed)) return Math.max(0, p.elapsed);
  const started = Number(p.started);
  const now = Number(p.now);
  if (!started || !now) return null;
  return Math.max(0, now - started);
}

function formatPct(fraction) {
  const pct = Math.max(0, Math.min(100, fraction * 100));
  if (pct > 0 && pct < 10) return `${pct.toFixed(1)}%`;
  return `${Math.round(pct)}%`;
}

function liveLeft(p, sampledAt) {
  if (!p || p.stage === 'stopping' || p.stage === 'stopped') return null;
  const frac = buildingFraction(p);
  const base = progressElapsed(p);
  if (base == null || frac <= 0) return p.eta;
  if (frac >= 1) return 0;
  const elapsed = base + (sampledAt ? (Date.now() - sampledAt) / 1000 : 0);
  if (elapsed < 1) return null;
  return elapsed * (1 - frac) / frac;
}

function paintBuildProgress(p, sampledAt) {
  if (!p || !p.stage || p.stage === 'done') return;
  const stopping = p.stage === 'stopping' || p.stage === 'stopped';
  const stage = stopping ? 'Stopping' : (p.message || 'Generating buildings');
  const frac = buildingFraction(p);
  const elapsed = progressElapsed(p);
  const extra = sampledAt ? (Date.now() - sampledAt) / 1000 : 0;
  const shownElapsed = elapsed == null ? null : elapsed + extra;
  const parts = [];
  if ((p.mods || 1) > 1) parts.push(`mod ${p.mod || 1} of ${p.mods}`);
  if (p.detail) parts.push(p.detail);
  if (p.total > 0) {
    parts.push(`${Number(p.done || 0).toLocaleString()} of ${Number(p.total).toLocaleString()}`);
  }
  if ((p.processes || 1) > 1) parts.push(`${p.process || 0} of ${p.processes} workers`);
  parts.push(formatPct(frac));
  if (!stopping) {
    const left = formatLeft(liveLeft(p, sampledAt));
    parts.push(left || (shownElapsed == null ? 'starting' : formatElapsed(shownElapsed)));
  }
  const detail = parts.join(' · ');
  updatePaintPieces(p.pieces);
  note('buildingsNote', `${stage} · ${detail}`);
  fx.card('buildings', 'running');
  const bar = Math.max(frac * 100, 1);
  fx.progress('buildings', bar);
  fx.overlay.buildings({
    stage,
    detail,
    pct: bar,
    elapsed: shownElapsed,
  });
}

function startBuildProgress(mapName) {
  const token = ++buildProgressToken;
  clearInterval(buildProgressTimer);
  clearInterval(buildPaintTimer);
  buildSnap = null;
  const localStart = Date.now();
  const paintWaiting = () => {
    const waited = (Date.now() - localStart) / 1000;
    const detail = `waiting for the first status · ${formatElapsed(waited)}`;
    note('buildingsNote', `Starting buildings · ${detail}`);
    fx.card('buildings', 'running');
    fx.overlay.buildings({
      stage: 'Starting buildings',
      detail,
      pct: 1,
      elapsed: waited,
    });
  };
  const tick = async () => {
    if (token !== buildProgressToken) return;
    try {
      const res = await fetch(`/api/progress?map=${encodeURIComponent(mapName)}`);
      const p = await res.json();
      if (token !== buildProgressToken) return;
      if (!p || !p.stage || p.stage === 'done') return;
      buildSnap = p;
      buildSnapAt = Date.now();
      paintBuildProgress(p, buildSnapAt);
    } catch (_) { /* the build call is the source of truth */ }
  };
  tick();
  buildProgressTimer = setInterval(tick, 400);
  buildPaintTimer = setInterval(() => {
    if (token !== buildProgressToken) return;
    if (!buildSnap) {
      paintWaiting();
      return;
    }
    paintBuildProgress(buildSnap, buildSnapAt);
  }, 1000);
}

function stopBuildProgress() {
  buildProgressToken += 1;
  clearInterval(buildProgressTimer);
  clearInterval(buildPaintTimer);
  buildProgressTimer = null;
  buildPaintTimer = null;
  buildSnap = null;
}

async function openWorldEd() {
  const status = await (await fetch('/api/worlded-tools')).json();
  if (!status.installed) {
    const yes = window.confirm(
      'Install the community world editing tools? They are downloaded once, and this button then opens your map in them.');
    if (yes) {
      note('worldedNote', 'Downloading the community world editing tools…');
      const start = await fetch('/api/worlded-tools', { method: 'POST' });
      const started = await start.json();
      if (!start.ok) throw new Error(started.error || 'Could not install the community world editing tools.');
      for (;;) {
        await new Promise(r => setTimeout(r, 700));
        const job = await (await fetch('/api/worlded-tools')).json();
        if (job.message) note('worldedNote', job.message);
        if (job.state === 'running') continue;
        if (job.state === 'error' || job.error) {
          throw new Error(job.error || 'Could not install the community world editing tools.');
        }
        break;
      }
    }
  }
  const res = await fetch('/api/worlded', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mapName: currentMap }),
  });
  const data = await res.json();
  if (!res.ok) throw apiError(data, res);
  note('worldedNote', 'WorldEd opened. File > BMP To TMX > All Cells…, then '
                      + 'File > Generate Lots 8x8 > All Cells… — waiting…');
  startLotsPoll();
}

document.getElementById('worldedBtn')?.addEventListener('click', async () => {
  try {
    await openWorldEd();
  } catch (err) {
    note('worldedNote', err.message, 'bad');
  }
});

function startLotsPoll() {
  clearInterval(lotsPoll);
  lotsPoll = setInterval(checkLots, 4000);
}

async function checkLots() {
  if (!currentMap) return;
  try {
    const res = await fetch(`/api/lots?map=${encodeURIComponent(currentMap)}`);
    const data = await res.json();
    if (data.compiled) {
      clearInterval(lotsPoll);
      installReady = true;
      syncInstallButton();
    }
  } catch (_) { /* keep polling quietly */ }
}

async function runInstall(modsDir) {
  note('installNote', 'Installing…');
  try {
    const res = await fetch('/api/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mapName: currentMap,
        title: document.getElementById('mapTitle').value.trim(),
        modId: document.getElementById('modId').value.trim(),
        modsDir: modsDir || '',
      }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    let lifts = '';
    try {
      const status = await (await fetch('/api/setup-status')).json();
      const elevators = (status.optional || []).find(c => c.id === 'elevators');
      lifts = elevators && elevators.ok
        ? ' Enable "Elevators" too, for working lifts in tall buildings.'
        : ' Tall buildings have lifts: subscribe to the Elevators mod on the Steam Workshop to make them work.';
      const selector = (status.optional || []).find(c => c.id === 'spawn_selector');
      if (selector && selector.ok) {
        lifts += ' With "Spawn Selector" enabled you can start at any landmark of the map.';
      }
    } catch (_) { /* the install itself worked; the tip is optional */ }
    const many = data.mods > 1 ? ` as ${data.mods} mods` : '';
    const full = !data.export || data.export.includes('full');
    const loot = full
      ? ' In a save you are already playing, right-click the ground and pick "Reset loot" for fresh loot in a building.'
      : '';
    note('installNote',
         `Installed ${data.cells} cells${many} to ${data.modRoot}. Enable `
         + `"${data.title}"${data.mods > 1 ? ' and the numbered mods beside it' : ''} `
         + 'in the game\'s Mods menu, then start a NEW save.' + loot + lifts, 'ok');
    return true;
  } catch (err) {
    note('installNote', err.message, 'bad');
    return false;
  }
}

async function publishWorldEd(output) {
  note('worldedNote', 'Writing the WorldEd project…');
  try {
    const res = await fetch('/api/worlded', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapName: currentMap, output: output || '' }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    note('worldedNote', `WorldEd project is in ${data.project}.`, 'ok');
    return true;
  } catch (err) {
    note('worldedNote', err.message, 'bad');
    return false;
  }
}

async function runExport() {
  if (exportBusy) return;
  const sel = exportSelection();
  if (!sel.compile && !sel.install && !sel.editable) return;
  if (!currentMap) {
    note('compileNote', 'Generate a map first.', 'bad');
    return;
  }
  exportBusy = true;
  syncExportButton();
  const output = document.getElementById('exportOutputPath')?.value.trim() || '';
  const modsDir = document.getElementById('exportInstallPath')?.value.trim() || '';
  try {
    if (sel.compile) {
      const p = await startCompile(false, output);
      if (!p || p.state === 'error' || p.state === 'stopped') return;
      if (sel.editable && p.output) {
        note('worldedNote', `WorldEd project is in ${p.output}.`, 'ok');
      }
    }
    if (sel.editable && !sel.compile) {
      const ok = await publishWorldEd(output);
      if (!ok) return;
    }
    if (sel.install) {
      // Compile writes the cells the game loads. Saving the project does not,
      // so Install with Compile turned off only runs when those cells already
      // exist from an earlier compile.
      let compiled = sel.compile;
      if (!compiled) {
        try {
          const ready = await (await fetch(
            `/api/lots?map=${encodeURIComponent(currentMap)}`)).json();
          compiled = !!ready.compiled;
        } catch (_) { compiled = false; }
      }
      if (compiled) await runInstall(modsDir);
      else note('installNote',
                (sel.editable ? 'Project saved. ' : '')
                + 'Install needs Compile map — the game reads the compiled cells.',
                'warn');
    }
  } finally {
    exportBusy = false;
    syncExportButton();
  }
}

async function browseExportFolder(inputId, title) {
  const input = document.getElementById(inputId);
  if (!input) return;
  const shown = (typeof i18n !== 'undefined' && i18n.say && i18n.say(title)) || title;
  const res = await fetch('/api/browse-folder', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: shown, path: input.value }),
  });
  const data = await res.json();
  if (!res.ok) throw apiError(data, res);
  if (data.cancelled || !data.path) return;
  input.value = data.path;
}

document.getElementById('exportBtn')?.addEventListener('click', () => runExport());
document.getElementById('exportOutputBrowse')?.addEventListener('click', async () => {
  try {
    await browseExportFolder('exportOutputPath', 'Select the output folder');
  } catch (err) {
    note('compileNote', err.message, 'bad');
  }
});
document.getElementById('exportInstallBrowse')?.addEventListener('click', async () => {
  try {
    await browseExportFolder('exportInstallPath', 'Select the mods folder');
  } catch (err) {
    note('installNote', err.message, 'bad');
  }
});

// ---- zombie census ------------------------------------------------------------
//
// Where the zombies go is worked out from the people in the buildings, so the
// build reports a head count. Recounting redraws only the spawn map from the
// saved footprints - a fraction of a second - so the zombie settings can be
// tried without regenerating a single building.

function renderCensus(pop) {
  const box = document.getElementById('census');
  if (!pop) { box.hidden = true; return; }
  box.hidden = false;
  const tiles = document.getElementById('censusTiles');
  tiles.innerHTML = `
    ${fx.tile(pop.residents, '', 'residents')}
    ${fx.tile(pop.daytime_occupants, '', 'at work or school')}
    ${fx.tile(pop.on_the_street || 0, '', 'out on the street')}
    ${fx.tile(pop.zombie_estimate, '', 'zombies, roughly')}
    ${fx.tile(pop.share_with_zombies * 100, '%', 'of the map has zombies', 0)}`;
  fx.countUp(tiles);
  const official = document.getElementById('censusOfficial');
  official.innerHTML = (pop.official || []).length
    ? 'OpenStreetMap lists ' + pop.official.map(p =>
        `<b>${escapeHtml(p.name || p.place)}</b> at ${p.population.toLocaleString()} people`
      ).join(', ') + ' — official figures can cover a whole district, not just what is on the map.'
    : '';
}

document.getElementById('recountBtn').addEventListener('click', async () => {
  const btn = document.getElementById('recountBtn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/zombies', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapName: currentMap, settings: readSettings() }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    renderCensus(data.population);
    const censusNote = document.getElementById('censusNote');
    censusNote.textContent = 'Recounted. Compile the map again so the game sees the new zombies.';
    censusNote.className = 'step-note ok';
    // The compiled lots hold the old spawn map until they are rebuilt.
    installReady = false;
    note('compileNote', 'Ready — compile again to bake in the new zombie counts.');
  } catch (err) {
    const censusNote = document.getElementById('censusNote');
    censusNote.textContent = err.message;
    censusNote.className = 'step-note bad';
  } finally {
    btn.disabled = false;
  }
});

// ---- automatic compile (patched WorldEd) ---------------------------------
//
// Stock WorldEd exposes BMP to TMX and Generate Lots only as menu items. The
// rebuilt PZWorldEd_cli.exe adds a --generate-map switch that runs both, so
// this step needs no clicking; the manual route stays available underneath.

// A batch WorldEd could not do is tried three times and then stepped over, so
// a compile can finish with a hole in it rather than throwing away the hours
// already spent. `onlyFailed` asks for just those cells back.
let compileWait = null;

function finishCompile(p) {
  compileRunning = false;
  if (!exportBusy) syncExportButton();
  const wait = compileWait;
  compileWait = null;
  if (wait) wait(p || { state: 'error' });
}

async function startCompile(onlyFailed, output) {
  const retry = document.getElementById('retryCellsBtn');
  if (retry) retry.hidden = true;
  compileRunning = true;
  syncExportButton();
  note('compileNote', 'Starting…');
  showStop(currentMap);
  try {
    const res = await fetch('/api/compile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mapName: currentMap,
        onlyFailed: !!onlyFailed,
        output: output || '',
      }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    return await new Promise(resolve => {
      compileWait = resolve;
      pollCompile();
    });
  } catch (err) {
    note('compileNote', err.message, 'bad');
    showStop(null);
    compileRunning = false;
    if (!exportBusy) syncExportButton();
    return { state: 'error', error: err.message };
  }
}

document.getElementById('retryCellsBtn')?.addEventListener('click', () => {
  const output = document.getElementById('exportOutputPath')?.value || '';
  startCompile(true, output);
});

// A compile that stepped over a batch is finished but not whole: installing it
// gives a map with a hole where those cells should be. Say so wherever that is
// noticed - at the end of a compile, and again when the map is opened later,
// because by then nobody remembers which one it was.
function showFailedCells(failed, cells) {
  const retry = document.getElementById('retryCellsBtn');
  failed = failed || [];
  if (retry) retry.hidden = !failed.length;
  if (!failed.length) return false;
  const where = failed.map(f => `${f.cells[0]},${f.cells[1]}`).join('  ');
  const many = failed.length === 1 ? 'batch' : 'batches';
  note('compileNote',
       `${cells} cells compiled, but ${failed.length} ${many} would not compile `
       + `after 3 tries (at ${where}). The map has a hole in it until those are done.`,
       'warn');
  note('installNote', 'You can install it, but those cells will be missing.');
  return true;
}

// The compile runs on the server's own thread; this just watches it. Blocking
// the request instead froze the whole window for the length of a town.
let compileTimer = null;

function pollCompile() {
  clearInterval(compileTimer);
  compileTimer = setInterval(async () => {
    try {
      const res = await fetch(
        `/api/compile-status?map=${encodeURIComponent(currentMap)}`);
      const p = await res.json();
      if (p.state === 'stopping') {
        note('compileNote', 'Stopping — waiting for WorldEd to close…');
        return;
      }
      if (p.state === 'running') {
        const pct = p.expected ? Math.floor(100 * p.cells / p.expected) : 0;
        // The batch counter is the honest one on a big map: cell files land in
        // bursts and stay flat for minutes inside a batch, which reads as a
        // hang. Batches tick over steadily.
        const batch = p.batches ? ` — batch ${p.batch}/${p.batches}` : '';
        fx.progress('compile', p.batches ? Math.max(pct, 100 * (p.batch - 1) / p.batches) : pct);
        note('compileNote',
             `Compiling${batch} — ${p.tmx} cells converted, `
             + `${p.cells}/${p.expected || '?'} compiled (${pct}%)…`);
        return;
      }
      clearInterval(compileTimer);
      showStop(null);
      if (p.state === 'stopped') {
        fx.progress('compile', 0);
        note('compileNote', 'Stopped. The cells already compiled are kept — '
                            + 'press Export to carry on from there.');
        fx.toast('ok', 'Stopped', 'Compiling picks up where it left off.');
      } else if (p.state === 'error') {
        lastErrorId = p.errorId || null;
        note('compileNote', p.error || 'Compile failed.', 'bad');
      } else if (p.state === 'done') {
        fx.progress('compile', 100);
        installReady = true;
        const where = p.output ? ` Written to ${p.output}.` : '';
        if (!showFailedCells(p.failed, p.cells)) {
          note('compileNote', `${p.cells} cells compiled.${where}`, 'ok');
        } else if (where && document.getElementById('compileNote')) {
          document.getElementById('compileNote').textContent += where;
        }
      }
      finishCompile(p);
    } catch (_) { /* keep watching */ }
  }, 2000);
}

// ---- links straight to a place ----------------------------------------------------
// ?q=<place> searches on load and selects the first match; &outline=1 takes its
// real boundary instead of a box. Handy for sharing "make this" with someone.
(function openFromLink() {
  const params = new URLSearchParams(location.search);
  const q = params.get('q');
  if (!q) return;
  searchInput.value = q;
  const wantOutline = params.get('outline') === '1';
  const watch = new MutationObserver(() => {
    const outline = wantOutline && searchResults.querySelector('button[data-outline]');
    const first = searchResults.querySelector('li[data-i]');
    if (!outline && !first) return;
    watch.disconnect();
    (outline || first).click();
  });
  watch.observe(searchResults, { childList: true });
  runSearch(q);
})();
