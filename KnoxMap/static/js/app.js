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
const BIG_TILES_PER_SIDE = 9000;
const BIG_LANDMARK_KM2 = 40.0;
const OVERPASS_TILE_KM2 = 30.0;
const SLOW_ABOVE_KM2 = 60.0;

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

const map = L.map('map', { zoomControl: true }).setView([38.0406, -84.5037], 14);
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
  draw: {
    polyline: false, marker: false, circlemarker: false,
    ...drawToolOptions(SEL_STYLE),
  },
  edit: { featureGroup: drawnItems, remove: true },
});
map.addControl(drawControl);

let currentRect = null;
let eraseMode = false;
let lassoButton = null;
const LASSO_ADD = 'Draw freehand: drag round the area you want';
const LASSO_CUT = 'Eraser: drag round the area to cut out';

function setEraseMode(on) {
  eraseMode = on;
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

// A new shape adds an area. With the eraser on, the same shape is cut out
// of the area already drawn.
function applyDrawn(layer) {
  if (!eraseMode) {
    setSelection(layer);
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
  options: { position: 'topleft' },
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
    return bar;
  },
});
map.addControl(new LassoControl());

function startLasso(button) {
  const box = map.getContainer();
  button.classList.add('is-active');
  box.classList.add('lasso-armed');
  map.dragging.disable();
  let points = [];
  let trail = null;
  const down = (e) => {
    points = [e.latlng];
    const color = eraseMode ? ERASE_STYLE.color : SEL_STYLE.color;
    trail = L.polyline(points, { color, weight: 2, dashArray: '4 4' }).addTo(map);
    map.on('mousemove', move);
  };
  const move = (e) => { points.push(e.latlng); trail.setLatLngs(points); };
  const up = () => {
    map.off('mousedown', down); map.off('mousemove', move); map.off('mouseup', up);
    map.dragging.enable();
    button.classList.remove('is-active');
    box.classList.remove('lasso-armed');
    if (trail) map.removeLayer(trail);
    const thin = simplifyLatLngs(points, 8);
    if (thin.length >= 3) {
      const layer = L.polygon(thin, eraseMode ? ERASE_STYLE : SEL_STYLE);
      if (!eraseMode) layer._knoxKind = 'freehand';
      applyDrawn(layer);
      map.fire(L.Draw.Event.CREATED, { layer, layerType: 'polygon', lasso: true });
    }
  };
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

function updateBboxFields() {
  if (!currentRect) return clearBboxFields();
  const { s, w, n, e } = rectBounds(currentRect);
  document.getElementById('south').value = s.toFixed(6);
  document.getElementById('west').value  = w.toFixed(6);
  document.getElementById('north').value = n.toFixed(6);
  document.getElementById('east').value  = e.toFixed(6);

  const area = bboxAreaKm2(s, w, n, e);
  const mpt = parseFloat(document.getElementById('metersPerTile').value);
  const widthM  = haversineKm(s, w, s, e) * 1000;
  const heightM = haversineKm(s, w, n, w) * 1000;
  const tilesX = Math.ceil(widthM / mpt / 300) * 300;
  const tilesY = Math.ceil(heightM / mpt / 300) * 300;
  const cellsX = tilesX / 300;
  const cellsY = tilesY / 300;

  const queries = area > OVERPASS_TILE_KM2
    ? Math.ceil(Math.sqrt(area / OVERPASS_TILE_KM2)) ** 2 : 1;
  // Landscape and vegetation images, 3 bytes a tile each.
  const pixelsMB = (tilesX * tilesY * 3 * 2) / 1e6;

  const stats = document.getElementById('area-stats');
  const btn = document.getElementById('generateBtn');
  const side = Math.max(tilesX, tilesY);
  const heavy = [];
  if (area > BIG_AREA_KM2) {
    heavy.push(`${Math.round(area)} km² is bigger than maps usually are `
             + `(${BIG_AREA_KM2} km²).`);
  }
  if (side > BIG_TILES_PER_SIDE) {
    heavy.push(`${side} tiles a side is past what is comfortable `
             + `(${BIG_TILES_PER_SIDE}) — about ${bitmapFor(tilesX, tilesY)} of `
             + 'ground and greenery held in memory at once. Raising metres per '
             + 'tile is the cheapest fix: 2 m is a quarter of the memory of 1 m.');
  }
  const slow = !heavy.length && area > SLOW_ABOVE_KM2;
  const bitmap = bitmapFor(tilesX, tilesY);
  const fill = Math.min(100, (area / BIG_AREA_KM2) * 100);

  stats.className = heavy.length ? 'warn' : (slow ? 'warn' : 'ok');
  stats.innerHTML = `
    <div class="tiles">
      ${fx.tile(area, 'km²', 'area', area < 10 ? 2 : 1)}
      <div class="tile"><div class="v"><span data-count="${cellsX}">0</span><small>×</small><span
        data-count="${cellsY}">0</span></div><div class="k">cells</div></div>
      ${fx.tile(tilesX * tilesY / 1e6, 'M', 'tiles', 2)}
      ${fx.tile(queries, '', queries === 1 ? 'osm query' : 'osm queries')}
    </div>
    <div class="meter">
      <div class="meter-top"><span>~${Math.round(widthM)} × ${Math.round(heightM)} m · ${bitmap} of bitmap</span>
        <span>${fill < 1 ? '<1' : Math.round(fill)}% of limit</span></div>
      <div class="bar"><div class="bar-fill" style="width:${fill}%"></div></div>
    </div>
    ${selectionAreaKm2() !== null ? `<div class="stat-note shape">Only the drawn shape is built:
      <b>${selectionAreaKm2().toFixed(2)} km²</b> of this ${area.toFixed(2)} km² box. Outside it the land
      turns back to countryside, with the main roads and rivers running on.</div>` : ''}
    ${heavy.length ? `<div class="stat-note warn">${heavy.join(' ')}
      You can still build it — this is a heads-up, not a wall.</div>` : ''}
    ${slow ? `<div class="stat-note warn">A big map — roughly ${Math.ceil(queries * 12 / 60)}+ min
      of OpenStreetMap queries before rendering starts.</div>` : ''}
  `;
  fx.countUp(stats);
  btn.disabled = false;
  fx.step('area', 'done');

  const lm = document.getElementById('landmarksBtn');
  lm.disabled = false;
  lm.title = area > BIG_LANDMARK_KM2
    ? `${Math.round(area)} km² is a lot to search for landmarks — it will take a while.`
    : '';
}

function clearBboxFields() {
  ['south', 'west', 'north', 'east'].forEach(id => {
    document.getElementById(id).value = '';
  });
  const stats = document.getElementById('area-stats');
  stats.className = 'empty';
  stats.innerHTML = `<div class="empty-state">
    Draw an area on the map - rectangle, polygon, circle or freehand - to see what you'll get.</div>`;
  document.getElementById('generateBtn').disabled = true;
  document.getElementById('landmarksBtn').disabled = true;
  document.getElementById('landmark-results').innerHTML = '';
  fx.resetFrom('area');
}

document.getElementById('metersPerTile').addEventListener('change', updateBboxFields);

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

const SETTING_LABELS = {
  zombies_per_resident:['Zombies per person', 'Each person who lived or worked here becomes this many zombies.'],
  m2_per_person:       ['Living space (m²)', 'Per resident. Lower = more crowded homes = more zombies. ~50 city, 45 town, 60 suburb.'],
  spawn_density:       ['Horde cap', 'Most zombies one 10×10 m spot can hold. Vanilla towns peak at 10.'],
  tree_density:        ['Woodland', 'Scales tree cover. Trees are cover to hide in.'],
  seed:                ['Seed', 'Same seed and area gives the same town again.'],
  fill_gaps:           ['Fill gaps from Overture', '1 adds the buildings OpenStreetMap has not got, from Overture Maps - OSM plus machine-detected roofprints, same licence. Worth it where your town is half missing from OSM; elsewhere it adds sheds. Needs DuckDB.'],
  true_map:            ['True map generation', '1 builds every address as mapped. 0 keeps the real roads, rivers, woods and terrain, but leaves out half the houses and grows the rest into proper homes with yards. Named places are always built, at the game’s size.'],
  guaranteed_rifle:    ['Guaranteed rifle', '1 leaves one military rifle on the map: in an army building if there is one, else the police station, else a gun shop, else a house on the edge of town. A real town has no checkpoints for one to spawn in.'],
  min_size:            ['Smallest building', 'Buildings narrower than this many tiles are left out.'],
  align_streets:       ['Straighten streets', '1 turns the map so the main street grid runs along the tiles - no staircase roads. 0 keeps north up.'],
  rotate_degrees:      ['Turn the map', 'Degrees to turn the whole area before it is built, on top of Straighten streets. Use it when the automatic angle picks the wrong grid.'],
  straight_roads:      ['Knox County roads', '1 lays every road in straight runs along the tiles and on 45-degree diagonals, and stands every building upright beside them, like the game’s own map. 0 draws roads as they are.'],
  max_size:            ['Largest building', 'Footprints above this are skipped.'],
  apartment_footprint: ['Flats above', 'An untagged footprint this big reads as flats.'],
  apartment_chance:    ['Flats chance', 'How often such a footprint really becomes flats.'],
  max_levels:          ['Tallest building', 'Storeys, up to 30 - as tall as the base game gets. OSM heights are capped to this. Tall cities take longer to compile.'],
  room_size:           ['Room size', 'Target room area in tiles before it gets split.'],
  square_buildings:    ['Square up buildings', 'Buildings turned less than this many degrees stand upright on the grid; the rest keep their real angle with stepped walls. 45 = every building upright.'],
  neighbourhood_tiles: ['Neighbourhood', 'How far one set of materials reaches.'],
  style_oddity:        ['Odd one out', 'How often a building breaks from its block.'],
  parking_density:     ['Parking', 'Vehicles only ever spawn in a parking stall.'],
};

let settingsMeta = null;

async function loadSettings() {
  const res = await fetch('/api/settings');
  settingsMeta = await res.json();
  buildSettingsForm(settingsMeta.current);
}

function buildSettingsForm(values) {
  const body = document.getElementById('advanced-body');
  body.innerHTML = '';
  for (const [key, [label, hint]] of Object.entries(SETTING_LABELS)) {
    if (!(key in settingsMeta.defaults)) continue;   // knob has been removed
    const [lo, hi] = settingsMeta.limits[key];
    const isInt = settingsMeta.types
      ? settingsMeta.types[key] === 'int'
      : Number.isInteger(settingsMeta.defaults[key]);
    const wrap = document.createElement('div');
    wrap.className = 'setting';
    // A seed is an identifier, not a quantity: nobody wants to drag a slider
    // across two billion values to find one, so it gets a box and a dice.
    const control = key === 'seed'
      ? `<div class="seed-row"><input type="number" data-key="${key}" min="${lo}"
           max="${hi}" step="1" value="${values[key]}"><button type="button"
           class="dice" title="Random seed">random</button></div>`
      : `<input type="range" data-key="${key}" min="${lo}" max="${hi}"
           step="${isInt ? 1 : 0.05}" value="${values[key]}">`;
    wrap.innerHTML = `
      <div class="setting-head"><span class="setting-name">${label}</span>
        ${key === 'seed' ? '' : `<output class="setting-val">${values[key]}</output>`}</div>
      ${control}
      <span class="setting-hint">${hint}</span>`;
    body.appendChild(wrap);
  }
  fx.wireSettings(body);
}

function readSettings() {
  const out = { preset: document.getElementById('preset').value };
  for (const el of document.querySelectorAll('#advanced-body input[data-key]')) {
    // Blank means "whatever the preset says" rather than zero.
    if (el.value.trim() !== '') out[el.dataset.key] = parseFloat(el.value);
  }
  return out;
}

document.getElementById('preset').addEventListener('change', () => {
  if (!settingsMeta) return;
  const preset = settingsMeta.presets[document.getElementById('preset').value];
  if (preset) buildSettingsForm(preset);
});

document.getElementById('resetSettings').addEventListener('click', () => {
  if (!settingsMeta) return;
  const preset = settingsMeta.presets[document.getElementById('preset').value];
  buildSettingsForm(preset || settingsMeta.defaults);
});

// ---- setup check -----------------------------------------------------------------

async function checkSetup() {
  try {
    const res = await fetch('/api/setup-status');
    const data = await res.json();
    const card = document.getElementById('setupCard');
    const lead = document.getElementById('setupLead');
    if (data.ready) { card.hidden = true; return; }
    if (lead) lead.hidden = !data.busy;
    document.getElementById('setupList').innerHTML = data.checks.map(c =>
      `<li class="${c.ok ? 'ok' : 'missing'}"><span>${c.ok ? '✓' : '✗'}</span>
        <b>${escapeHtml(c.label)}</b>${c.ok ? '' : ` — ${escapeHtml(c.fix)}`}</li>`).join('');
    card.hidden = false;
    if (data.busy) setTimeout(checkSetup, 2000);
  } catch (_) { /* the page still works without the check */ }
}
checkSetup();

// ---- game location ---------------------------------------------------------------
//
// Either found on its own, or a folder the player picked. The folder has to
// be the install: the one that contains the Project Zomboid jar.

function showGameLocation(data) {
  const manual = data.mode === 'manual';
  document.getElementById('gameAuto').checked = !manual;
  document.getElementById('gameManual').checked = manual;
  document.getElementById('gameBrowseRow').hidden = !manual;
  document.getElementById('gamePath').textContent = manual && data.game ? data.game : '';
  document.getElementById('gameJar').textContent = manual && data.jar ? data.jar : '';
  const note = document.getElementById('steamNote');
  note.className = 'hint';
  if (data.game && data.jar) {
    note.textContent = manual ? '' : `${data.game} (${data.jar})`;
  } else {
    note.className = 'hint bad';
    note.textContent = data.game
      ? 'That folder does not contain the Project Zomboid jar. Choose the game folder, the one with ProjectZomboid64.jar in it.'
      : 'Project Zomboid was not found. Choose the folder that contains the jar.';
  }
}

async function saveGameLocation(body) {
  const res = await fetch('/api/game-location', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw apiError(data, res);
  if (data.cancelled) {
    document.getElementById('gameManual').checked = true;
    document.getElementById('gameBrowseRow').hidden = false;
    return;
  }
  showGameLocation(data);
  checkSetup();
}

async function loadGameLocation() {
  try {
    showGameLocation(await (await fetch('/api/game-location')).json());
  } catch (_) { /* the choices are already on the page */ }
}

loadGameLocation();

document.getElementById('gameAuto').addEventListener('change', async () => {
  if (!document.getElementById('gameAuto').checked) return;
  try {
    await saveGameLocation({ mode: 'auto' });
  } catch (err) {
    note('steamNote', err.message, 'bad');
  }
});

document.getElementById('gameManual').addEventListener('change', () => {
  if (!document.getElementById('gameManual').checked) return;
  document.getElementById('gameBrowseRow').hidden = false;
  document.getElementById('gameBrowse').click();
});

document.getElementById('gameBrowse').addEventListener('click', async () => {
  try {
    await saveGameLocation({ mode: 'browse' });
  } catch (err) {
    note('steamNote', err.message, 'bad');
    document.getElementById('gameManual').checked = true;
    document.getElementById('gameBrowseRow').hidden = false;
  }
});

const sideSub = { setup: 'game', map: 'area', build: 'generate' };

document.querySelector('.side-tabs').addEventListener('click', e => {
  const tab = e.target.closest('.side-tab');
  if (tab) showMaster(tab.dataset.master);
});
document.querySelector('.side-tabs').addEventListener('keydown', e => moveTab(e, '.side-tab', tab => {
  showMaster(tab.dataset.master);
}));

for (const row of document.querySelectorAll('.side-subtabs')) {
  row.addEventListener('click', e => {
    const tab = e.target.closest('.side-subtab');
    if (!tab) return;
    sideSub[row.dataset.master] = tab.dataset.tab;
    showMaster(row.dataset.master);
  });
  row.addEventListener('keydown', e => moveTab(e, '.side-subtab', tab => {
    sideSub[row.dataset.master] = tab.dataset.tab;
    showMaster(row.dataset.master);
  }));
}

function moveTab(e, selector, choose) {
  const tabs = [...e.currentTarget.querySelectorAll(selector)];
  const i = tabs.indexOf(document.activeElement);
  if (i < 0) return;
  let next = null;
  if (e.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
  else if (e.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
  else if (e.key === 'Home') next = tabs[0];
  else if (e.key === 'End') next = tabs[tabs.length - 1];
  if (!next) return;
  e.preventDefault();
  next.focus();
  choose(next);
}

function showMaster(master) {
  for (const tab of document.querySelectorAll('.side-tab')) {
    const on = tab.dataset.master === master;
    tab.classList.toggle('is-on', on);
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
  }
  for (const row of document.querySelectorAll('.side-subtabs')) {
    row.hidden = row.dataset.master !== master;
  }
  const sub = sideSub[master];
  const row = document.querySelector(`.side-subtabs[data-master="${master}"]`);
  if (row && sub) {
    for (const tab of row.querySelectorAll('.side-subtab')) {
      const on = tab.dataset.tab === sub;
      tab.classList.toggle('is-on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      tab.tabIndex = on ? 0 : -1;
    }
  }
  for (const panel of document.querySelectorAll('.side-panel')) {
    const show = panel.dataset.master === master && (!panel.dataset.tab || panel.dataset.tab === sub);
    panel.hidden = !show;
  }
  const shell = document.querySelector('.side-shell');
  if (shell) shell.classList.toggle('has-sub', !!document.querySelector(`.side-subtabs[data-master="${master}"]`));
  syncSideSteps();
}

function sideSteps() {
  const steps = [];
  for (const tab of document.querySelectorAll('.side-tab')) {
    const master = tab.dataset.master;
    const row = document.querySelector(`.side-subtabs[data-master="${master}"]`);
    if (!row) { steps.push({ master, sub: null }); continue; }
    for (const sub of row.querySelectorAll('.side-subtab')) {
      steps.push({ master, sub: sub.dataset.tab });
    }
  }
  return steps;
}

function currentSideStep() {
  const master = document.querySelector('.side-tab.is-on')?.dataset.master;
  const sub = sideSub[master] || null;
  return sideSteps().findIndex(s => s.master === master && s.sub === sub);
}

function syncSideSteps() {
  const i = currentSideStep();
  const last = sideSteps().length - 1;
  document.getElementById('sidePrev').disabled = i <= 0;
  document.getElementById('sideNext').disabled = i < 0 || i >= last;
}

function stepSide(dir) {
  const step = sideSteps()[currentSideStep() + dir];
  if (!step) return;
  if (step.sub) sideSub[step.master] = step.sub;
  showMaster(step.master);
}

document.getElementById('sidePrev').addEventListener('click', () => stepSide(-1));
document.getElementById('sideNext').addEventListener('click', () => stepSide(1));
syncSideSteps();

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
  let data;
  try {
    data = await (await fetch('/api/maps')).json();
  } catch (_) { return; }
  const list = document.getElementById('mapList');
  const card = document.getElementById('mapsCard');
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
  renderResults(data);
  showUpgrade(data);
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
  button.hidden = false;
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
      const res = await fetch('/api/pictures', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mapName }),
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
      button.disabled = false;
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

// ---- generation ----

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
  showStop(chosen);
  startProgress(chosen);

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
    status.textContent = `Done in ${data.osmSeconds}s (OSM query). ${data.featureCount} features rendered.`;
    fx.overlay.done(`${data.featureCount.toLocaleString()} features on the map`);
    fx.step('terrain', 'done');
    fx.toast('ok', 'Terrain generated',
             `${data.cellsX} × ${data.cellsY} cells from ${data.featureCount.toLocaleString()} features.`);
    renderResults(data);
    loadMaps();
  } catch (err) {
    status.className = 'error';
    status.textContent = `Error: ${err.message}`;
    fx.overlay.fail(err.message);
    fx.step('terrain', 'error');
    fx.problem('Generation failed', err.message, err.errorId);
  } finally {
    stopProgress();
    showStop(null);
    btn.disabled = false;
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

function startProgress(mapName) {
  clearInterval(progressTimer);
  progressTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/progress?map=${encodeURIComponent(mapName)}`);
      const p = await res.json();
      fx.overlay.update(p);
      const status = document.getElementById('status');
      if (p.stage === 'osm') {
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
  clearInterval(progressTimer);
  progressTimer = null;
}

function renderResults(data) {
  const section = document.getElementById('results');
  section.hidden = false;
  document.getElementById('previewImg').src = data.files.preview + '?t=' + Date.now();
  document.getElementById('previewLink').href = data.files.preview;

  const info = document.getElementById('results-info');
  info.innerHTML = `<div class="tiles">
      ${fx.tile(data.width, '', 'tiles wide')}
      ${fx.tile(data.height, '', 'tiles tall')}
      <div class="tile"><div class="v"><span data-count="${data.cellsX}">0</span><small>×</small><span
        data-count="${data.cellsY}">0</span></div><div class="k">cells</div></div>
      ${fx.tile(data.featureCount, '', 'osm features')}
    </div>`;
  fx.countUp(info);

  // One click gets the lot; the individual files stay available but folded away.
  const all = document.getElementById('downloadAll');
  all.href = data.files.zip;
  all.setAttribute('download', `${data.mapName}.zip`);
  all.textContent = IN_WINDOW ? 'Save everything as a .zip' : 'Download everything (.zip)';
  wireDownload(all, 'zip', 'the zip');
  const note = document.getElementById('saveNote');
  if (note) { note.className = 'hint'; note.textContent = ''; }
  wirePictures(data.mapName);

  document.querySelectorAll('#results .ph').forEach(el => {
    el.textContent = data.mapName;
  });

  const entries = [
    ['Landscape BMP', data.files.landscape],
    ['Vegetation BMP', data.files.vegetation],
    ['Zombie spawn BMP', data.files.spawn],
    ['Preview PNG', data.files.preview],
    ['Building footprints (GeoJSON)', data.files.buildings],
    ['Meta (JSON)', data.files.meta],
    ['README', data.files.readme],
  ];
  const ul = document.getElementById('downloads');
  ul.innerHTML = entries.map(([label, href]) =>
    `<li>→ <a href="${href}" target="_blank" download>${label}</a></li>`
  ).join('');
  ul.querySelectorAll('a').forEach((link, i) => {
    const href = entries[i][1];
    wireDownload(link, href.split('/').pop(), entries[i][0]);
  });
  setupPipeline(data);
  section.scrollIntoView({ behavior: 'smooth' });
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
const searchHere = document.getElementById('searchHere');
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
    searchResults.innerHTML = '<li class="empty">No matches.</li>';
    searchResults.hidden = false;
    return;
  }
  searchResults.innerHTML = results.map((r, i) => {
    const kind = [r.type, r.category].filter(Boolean)[0] || '';
    const rest = r.display_name.split(',').slice(1).join(',').trim();
    return `<li data-i="${i}">
      <span class="r-name">${escapeHtml(r.name)}</span>
      ${kind ? `<span class="r-kind">${escapeHtml(kind.replace(/_/g, ' '))}</span>` : ''}
      ${r.outline ? `<button type="button" class="r-outline" data-outline="${i}"
        title="Select its real boundary instead of a box">outline</button>` : ''}
      <span class="r-where">${escapeHtml(rest)}</span>
    </li>`;
  }).join('');
  searchResults.hidden = false;

  searchResults.querySelectorAll('button[data-outline]').forEach(btn => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const r = results[parseInt(btn.dataset.outline, 10)];
      const bounds = setOutline(r.outline);
      map.fitBounds(bounds, { padding: [30, 30] });
      hideSearchResults();
      searchInput.value = r.name;
    });
  });

  searchResults.querySelectorAll('li[data-i]').forEach(li => {
    li.addEventListener('click', () => {
      const r = results[parseInt(li.dataset.i, 10)];
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

async function runSearch(q) {
  if (!q.trim()) return hideSearchResults();
  searchResults.innerHTML = '<li class="empty">Searching…</li>';
  searchResults.hidden = false;
  const params = new URLSearchParams({ q });
  if (searchHere.checked) {
    const b = map.getBounds();
    // Zoomed far enough out, the viewport spans more than a whole world and
    // wrapping it would describe a sliver instead. Search globally then.
    if (b.getEast() - b.getWest() < 360) {
      params.set('south', b.getSouth());
      params.set('west', wrapLon(b.getWest()));
      params.set('north', b.getNorth());
      params.set('east', wrapLon(b.getEast()));
      params.set('bounded', '1');
    }
  }
  try {
    const res = await fetch('/api/search?' + params.toString());
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    renderSearchResults(data.results);
  } catch (err) {
    searchResults.innerHTML = `<li class="empty">${escapeHtml(err.message)}</li>`;
  }
}

// Search runs when you press Enter, never as you type. Nominatim's usage
// policy forbids autocomplete-style searching on its public server
// (https://operations.osmfoundation.org/policies/nominatim/).
searchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { clearTimeout(searchTimer); runSearch(searchInput.value); }
  if (e.key === 'Escape') hideSearchResults();
});
document.addEventListener('click', (e) => {
  if (!document.getElementById('search-box').contains(e.target)) hideSearchResults();
});

// ---- landmarks inside the selection ---------------------------------------

let landmarkMarkers = L.layerGroup().addTo(map);

document.getElementById('landmarksBtn').addEventListener('click', async () => {
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
let lotsPoll = null;

function note(id, text, cls) {
  const el = document.getElementById(id);
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

function setupPipeline(data) {
  currentMap = data.mapName;
  const upgrade = document.getElementById('upgradeNote');
  if (upgrade) { upgrade.hidden = true; upgrade.innerHTML = ''; }
  document.getElementById('mapTitle').value = data.mapName;
  document.getElementById('modId').value = data.mapName;
  document.getElementById('buildingsBtn').disabled = false;
  document.getElementById('worldedBtn').disabled = true;
  document.getElementById('compileBtn').disabled = true;
  document.getElementById('installBtn').disabled = true;
  ['buildings', 'compile', 'install'].forEach(k => fx.card(k, null));
  fx.card('buildings', 'ready');
  fx.resetFrom('buildings');
  document.getElementById('compileBar').style.width = '0%';
  note('buildingsNote', 'Turns every OSM footprint into a furnished building.');
  // Reset with the rest of them: opening another map used to leave whatever
  // the last compile said sitting under the new one's Compile button.
  note('compileNote', "Turns the map into the game's files with WorldEd. "
                      + 'Takes a few minutes.');
  note('worldedNote', 'Generate the buildings first.');
  note('installNote', 'Copies the compiled map into ~/Zomboid/mods.');
  renderCensus(null);
  document.getElementById('retryCellsBtn').hidden = true;
  checkLots();
  // Whatever the last compile of this map left behind, said again now: the
  // window has been closed and reopened since, and "done" would be a lie.
  fetch(`/api/compile-status?map=${encodeURIComponent(data.mapName)}`)
    .then(r => r.json())
    .then(p => { if (p.state !== 'running') showFailedCells(p.failed, p.cells); })
    .catch(() => { /* nothing to add if it cannot be asked */ });
}

document.getElementById('buildingsBtn').addEventListener('click', async () => {
  const btn = document.getElementById('buildingsBtn');
  btn.disabled = true;
  note('buildingsNote', 'Generating…');
  showStop(currentMap);
  try {
    const res = await fetch('/api/buildings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      // Sent again so the buildings can be regenerated with different
      // settings without re-downloading the town from OSM.
      body: JSON.stringify({ mapName: currentMap, settings: readSettings() }),
    });
    const data = await res.json();
    if (wasStopped(data, res)) {
      note('buildingsNote', 'Stopped. The terrain and the area are still here — press Build again.');
      fx.toast('ok', 'Stopped', 'Nothing was thrown away.');
      return;
    }
    if (!res.ok) throw apiError(data, res);
    note('buildingsNote', `${data.count} buildings → ${data.pzw}`, 'ok');
    renderCensus(data.population);
    document.getElementById('worldedBtn').disabled = false;
    document.getElementById('compileBtn').disabled = false;
    note('compileNote', 'Ready — runs BMP to TMX and Generate Lots for you.');
  } catch (err) {
    note('buildingsNote', err.message, 'bad');
  } finally {
    showStop(null);
    btn.disabled = false;
  }
});

document.getElementById('worldedBtn').addEventListener('click', async () => {
  try {
    const res = await fetch('/api/worlded', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapName: currentMap }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    note('worldedNote', 'WorldEd opened. File > BMP To TMX > All Cells…, then '
                        + 'File > Generate Lots 8x8 > All Cells… — waiting…');
    startLotsPoll();
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
      note('worldedNote', `${data.cells} cells compiled.`, 'ok');
      document.getElementById('installBtn').disabled = false;
      note('installNote', 'Ready to install.');
    }
  } catch (_) { /* keep polling quietly */ }
}

document.getElementById('installBtn').addEventListener('click', async () => {
  const btn = document.getElementById('installBtn');
  btn.disabled = true;
  note('installNote', 'Installing…');
  try {
    const res = await fetch('/api/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mapName: currentMap,
        title: document.getElementById('mapTitle').value.trim(),
        modId: document.getElementById('modId').value.trim(),
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
    note('installNote',
         `Installed ${data.cells} cells to ${data.modRoot}. Enable "${data.title}" `
         + 'in the game\'s Mods menu, then start a NEW save. In a save you are '
         + 'already playing, right-click the ground and pick "Reset loot" for '
         + 'fresh loot in a building.' + lifts, 'ok');
  } catch (err) {
    note('installNote', err.message, 'bad');
    btn.disabled = false;
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
    document.getElementById('installBtn').disabled = true;
    document.getElementById('compileBtn').disabled = false;
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
async function startCompile(onlyFailed) {
  const btn = document.getElementById('compileBtn');
  const retry = document.getElementById('retryCellsBtn');
  btn.disabled = true;
  retry.hidden = true;
  note('compileNote', 'Starting…');
  showStop(currentMap);
  try {
    const res = await fetch('/api/compile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mapName: currentMap, onlyFailed: !!onlyFailed }),
    });
    const data = await res.json();
    if (!res.ok) throw apiError(data, res);
    pollCompile();
  } catch (err) {
    note('compileNote', err.message, 'bad');
    showStop(null);
    btn.disabled = false;
  }
}

document.getElementById('compileBtn')
  .addEventListener('click', () => startCompile(false));
document.getElementById('retryCellsBtn')
  .addEventListener('click', () => startCompile(true));

// A compile that stepped over a batch is finished but not whole: installing it
// gives a map with a hole where those cells should be. Say so wherever that is
// noticed - at the end of a compile, and again when the map is opened later,
// because by then nobody remembers which one it was.
function showFailedCells(failed, cells) {
  const retry = document.getElementById('retryCellsBtn');
  failed = failed || [];
  retry.hidden = !failed.length;
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
      document.getElementById('compileBtn').disabled = false;
      if (p.state === 'stopped') {
        fx.progress('compile', 0);
        note('compileNote', 'Stopped. The cells already compiled are kept — '
                            + 'press Compile to carry on from there.');
        fx.toast('ok', 'Stopped', 'Compiling picks up where it left off.');
        return;
      }
      if (p.state === 'error') {
        lastErrorId = p.errorId || null;
        note('compileNote', p.error || 'Compile failed.', 'bad');
        return;
      }
      if (p.state === 'done') {
        fx.progress('compile', 100);
        document.getElementById('installBtn').disabled = false;
        if (showFailedCells(p.failed, p.cells)) return;
        note('compileNote', `${p.cells} cells compiled.`, 'ok');
        note('installNote', 'Ready to install.');
      }
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
