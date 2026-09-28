// Edit step. Leaflet map of the generated pieces, plus the sidebar inspector.
// app.js calls window.knoxEdit.show / hide and invalidates window.knoxEditMap.

(function () {
  const LAYER_API = {
    buildings: 'building',
    areas: 'area',
    roads: 'road',
    fences: 'fence',
    places: 'place',
  };
  const LAYER_ORDER = ['area', 'building', 'road', 'fence', 'place'];
  const EDITABLE = new Set(['building', 'area', 'road', 'place']);
  const STYLES = {
    building: { color: '#a5e266', weight: 1, fillColor: '#a5e266', fillOpacity: 0.35 },
    area: { color: '#e0b060', weight: 1, fillColor: '#e0b060', fillOpacity: 0.12 },
    road: { color: '#d7d3c8', weight: 2, opacity: 0.9 },
    fence: { color: '#8a8478', weight: 1, opacity: 0.8, dashArray: '3 3' },
    place: { color: '#7eb6ff', weight: 1, fillColor: '#7eb6ff', fillOpacity: 0.9 },
  };
  const STAGE_RANK = { rebuild: 4, repaint: 3, reroll: 2, labels: 1 };
  const STAGE_BUTTON = {
    rebuild: 'Apply rebuild',
    repaint: 'Apply repaint',
    reroll: 'Apply re-roll',
    labels: 'Apply labels',
  };
  const STAGE_LINE = {
    reroll: 'Rooms and furniture only. The rest of the town stays.',
    rebuild: 'Kinds, placement and interiors are decided again.',
    repaint: 'Ground colours are redrawn.',
    labels: 'Names on the paper map.',
  };
  const DRAW_BUTTONS = {
    select: 'editSelectRect',
    reroll: 'editReroll',
    rebuild: 'editRebuild',
    zone: 'editZone',
    area: 'editDrawArea',
  };
  const BOX_STYLE = {
    color: '#a5e266', weight: 1, fillColor: '#a5e266', fillOpacity: 0.1,
    dashArray: '4 4', interactive: false,
  };

  let editMap = null;
  let tileLayer = null;
  let renderer = null;
  let gridGroup = null;
  let overlayLayer = null;
  let drawer = null;
  let drawIntent = null;
  const groups = {};
  const paintLayers = new Map();
  let pieces = [];
  let pieceGrids = [];
  let overlays = new Map();
  let vocab = { kinds: [], categories: [], styles: [] };
  let pendingStages = [];
  let selection = [];
  let selected = new Set();
  let inspectorKey = null;
  let inspectorState = null;
  let inspectorDirty = false;
  let loadedFor = null;
  let needFit = false;
  let applying = false;
  let errorSource = '';
  let loadToken = 0;
  let featureToken = 0;
  let detailToken = 0;
  let fetchTimer = null;
  let applyTimer = null;
  let applyEpoch = 0;
  let suppressClick = false;

  function mapName() {
    if (typeof window.knoxCurrentMap === 'function') {
      const name = window.knoxCurrentMap();
      if (name) return name;
    }
    return null;
  }

  function el(id) { return document.getElementById(id); }

  function showError(msg, source) {
    const node = el('editError');
    if (!node) return;
    if (!msg) {
      errorSource = '';
      node.hidden = true;
      node.textContent = '';
      return;
    }
    errorSource = source || 'edit';
    node.hidden = false;
    node.textContent = msg;
  }

  function clearErrorFrom(source) {
    if (errorSource === source) showError('');
  }

  async function readJson(res) {
    try { return await res.json(); }
    catch (_) { return {}; }
  }

  async function getJson(url) {
    let res;
    try {
      res = await fetch(url);
    } catch (_) {
      throw new Error('The edit request failed.');
    }
    const data = await readJson(res);
    if (!res.ok || (data && data.error && !data.started)) {
      throw new Error((data && data.error) || 'The edit request failed.');
    }
    return data || {};
  }

  async function postJson(url, body) {
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } catch (_) {
      throw new Error('The edit request failed.');
    }
    const data = await readJson(res);
    if (!res.ok || (data && data.error && !data.started)) {
      throw new Error((data && data.error) || 'The edit request failed.');
    }
    return data || {};
  }

  function guard(fn) {
    return async () => {
      try { await fn(); }
      catch (err) { showError((err && err.message) || 'The edit request failed.', 'edit'); }
    };
  }

  function intOrNull(value) {
    if (value == null || String(value).trim() === '') return null;
    const n = Number(value);
    if (!Number.isFinite(n)) return null;
    return Math.round(n);
  }

  function randSeed() {
    return 1 + Math.floor(Math.random() * 9999);
  }

  function featureId(f) {
    if (!f) return '';
    if (f.id != null && f.id !== '') return String(f.id);
    const props = f.properties || {};
    if (props.fid != null && props.fid !== '') return String(props.fid);
    return '';
  }

  function itemFromLayer(layer) {
    const f = layer && layer.feature;
    if (!f) return null;
    const props = f.properties || {};
    const id = featureId(f);
    const piece = props._piece || '';
    return {
      id,
      piece,
      layer: props.layer || '',
      props,
      key: piece + ':' + id,
      fid: props.fid || id,
    };
  }

  function darkBase() {
    try { return (localStorage.getItem('knoxmap.base') || 'dark') !== 'streets'; }
    catch (_) { return true; }
  }

  function syncTiles() {
    if (!editMap) return;
    const dark = darkBase();
    const nextClass = dark ? 'tiles-dark' : '';
    if (tileLayer && (tileLayer.options.className || '') === nextClass && editMap.hasLayer(tileLayer)) {
      return;
    }
    if (tileLayer) editMap.removeLayer(tileLayer);
    const opts = { maxZoom: 19, attribution: '© OpenStreetMap contributors' };
    if (dark) opts.className = 'tiles-dark';
    tileLayer = L.tileLayer('/tiles/{z}/{x}/{y}.png', opts);
    tileLayer.addTo(editMap);
    tileLayer.bringToBack();
  }

  function styleFor(f) {
    const layer = (f.properties && f.properties.layer) || 'building';
    const st = Object.assign({}, STYLES[layer] || STYLES.building);
    const key = ((f.properties && f.properties._piece) || '') + ':' + featureId(f);
    if (selected.has(key)) {
      st.color = '#ffffff';
      st.weight = (st.weight || 1) + 2;
      if (st.fillOpacity != null) st.fillOpacity = Math.min(0.75, st.fillOpacity + 0.3);
    }
    if (f.properties && f.properties.deleted) {
      st.opacity = 0.35;
      st.fillOpacity = 0.08;
      st.dashArray = '3 3';
    }
    return st;
  }

  function pointToLayer(f, latlng) {
    const st = styleFor(f);
    const opts = {
      radius: (f.properties && f.properties.layer === 'place') ? 6 : 4,
      color: st.color,
      weight: st.weight || 1,
      fillColor: st.fillColor || st.color,
      fillOpacity: st.fillOpacity != null ? st.fillOpacity : 0.9,
      opacity: st.opacity != null ? st.opacity : 1,
    };
    if (renderer) opts.renderer = renderer;
    return L.circleMarker(latlng, opts);
  }

  function onFeatureClick(e) {
    if (suppressClick) {
      suppressClick = false;
      if (e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
      return;
    }
    if (drawIntent) return;
    const item = itemFromLayer(e.target);
    if (!item || !item.id) return;
    if (e.originalEvent) L.DomEvent.stopPropagation(e.originalEvent);
    if (e.originalEvent && e.originalEvent.shiftKey) {
      const idx = selection.findIndex(s => s.key === item.key);
      if (idx >= 0) selection.splice(idx, 1);
      else selection.push(item);
    } else {
      selection = [item];
    }
    onSelectionChanged();
  }

  function onEachFeature(feature, layer) {
    layer.on('click', onFeatureClick);
  }

  function restyle() {
    for (const group of Object.values(groups)) {
      group.eachLayer((layer) => {
        if (!layer.feature || !layer.setStyle) return;
        layer.setStyle(styleFor(layer.feature));
      });
    }
  }

  function ensurePane(name, zIndex) {
    if (!editMap.getPane(name)) {
      const pane = editMap.createPane(name);
      pane.style.zIndex = String(zIndex);
      pane.style.pointerEvents = 'none';
    }
    return name;
  }

  function ensureMap() {
    if (editMap) return;
    editMap = L.map('edit-map', {
      maxZoom: 19,
      boxZoom: false,
      zoomControl: true,
      attributionControl: true,
    });
    window.knoxEditMap = editMap;
    editMap.setView([38.0406, -84.5037], 13);
    renderer = (typeof L.canvas === 'function') ? L.canvas({ padding: 0.5 }) : null;
    ensurePane('edit-shapes', 450);
    ensurePane('edit-grid', 460);
    const geoOpts = { style: styleFor, pointToLayer, onEachFeature };
    if (renderer) geoOpts.renderer = renderer;
    for (const key of LAYER_ORDER) {
      groups[key] = L.geoJSON(null, geoOpts).addTo(editMap);
    }
    gridGroup = L.layerGroup();
    syncTiles();
    applyTerrainOpacity();
    editMap.on('moveend', scheduleFetch);
    editMap.on('mousemove', onMouseMove);
    editMap.on('click', onMapClick);
    editMap.on(L.Draw.Event.CREATED, onDrawCreated);
    editMap.on(L.Draw.Event.DRAWSTOP, () => {
      drawer = null;
      drawIntent = null;
      const hint = el('editDrawHint');
      if (hint) hint.hidden = true;
      markArmed(null);
    });
    bindShiftDrag();
  }

  function onMapClick() {
    if (suppressClick) { suppressClick = false; return; }
    if (drawIntent || !selection.length) return;
    selection = [];
    onSelectionChanged();
  }

  function onMouseMove(e) {
    const hit = gridHit(e.latlng);
    if (!hit) { paintCoords(null, null); return; }
    const x = Math.floor(hit.tile.x);
    const y = Math.floor(hit.tile.y);
    paintCoords({ x, y }, hit.grid.cellOf(x, y));
  }

  function paintCoords(tile, cell) {
    el('editTileX').textContent = tile ? String(tile.x) : '—';
    el('editTileY').textContent = tile ? String(tile.y) : '—';
    el('editCellX').textContent = cell ? String(cell.cx) : '—';
    el('editCellY').textContent = cell ? String(cell.cy) : '—';
  }

  function gridHit(latlng) {
    let best = null;
    let bestDist = Infinity;
    for (const g of pieceGrids) {
      const t = g.grid.latLngToTile(latlng.lat, latlng.lng);
      if (!t) continue;
      const inside = t.x >= 0 && t.y >= 0 && t.x <= g.width && t.y <= g.height;
      if (inside) return { grid: g.grid, tile: t };
      const dx = t.x < 0 ? -t.x : (t.x > g.width ? t.x - g.width : 0);
      const dy = t.y < 0 ? -t.y : (t.y > g.height ? t.y - g.height : 0);
      const dist = dx * dx + dy * dy;
      if (dist < bestDist) {
        bestDist = dist;
        best = { grid: g.grid, tile: t };
      }
    }
    return best;
  }

  function bindShiftDrag() {
    const node = editMap.getContainer();
    node.addEventListener('mousedown', (ev) => {
      if (!ev.shiftKey || ev.button !== 0 || drawIntent) return;
      if (ev.target.closest && ev.target.closest('.leaflet-control')) return;
      editMap.dragging.disable();
      const startPt = editMap.mouseEventToContainerPoint(ev);
      const startLl = editMap.mouseEventToLatLng(ev);
      let rect = null;
      let moved = false;
      const move = (mv) => {
        const pt = editMap.mouseEventToContainerPoint(mv);
        if (pt.distanceTo(startPt) < 5) return;
        moved = true;
        const ll = editMap.mouseEventToLatLng(mv);
        if (!rect) rect = L.rectangle([startLl, ll], BOX_STYLE).addTo(editMap);
        else rect.setBounds(L.latLngBounds(startLl, ll));
      };
      const up = () => {
        window.removeEventListener('mousemove', move);
        window.removeEventListener('mouseup', up);
        editMap.dragging.enable();
        if (!rect || !moved) return;
        const bounds = rect.getBounds();
        editMap.removeLayer(rect);
        suppressClick = true;
        selectInBounds(bounds, true);
      };
      window.addEventListener('mousemove', move);
      window.addEventListener('mouseup', up);
    }, true);
  }

  function layerIntersects(layer, bounds) {
    if (layer.getLatLng) return bounds.contains(layer.getLatLng());
    if (layer.getBounds) {
      const b = layer.getBounds();
      return !!(b && b.isValid() && bounds.intersects(b));
    }
    return false;
  }

  function selectInBounds(bounds, add) {
    const found = [];
    const seen = new Set(add ? selection.map(s => s.key) : []);
    for (const key of LAYER_ORDER) {
      groups[key].eachLayer((layer) => {
        if (!layerIntersects(layer, bounds)) return;
        const item = itemFromLayer(layer);
        if (!item || !item.id || seen.has(item.key)) return;
        seen.add(item.key);
        found.push(item);
      });
    }
    selection = add ? selection.concat(found) : found;
    onSelectionChanged();
  }

  function pieceBounds(p) {
    const b = p && p.bbox;
    if (b && [b.south, b.west, b.north, b.east].every(Number.isFinite)) {
      return L.latLngBounds([b.south, b.west], [b.north, b.east]);
    }
    if (p && p.corners && p.corners.length) return L.latLngBounds(p.corners);
    return null;
  }

  function intersectsView(p, view) {
    const b = pieceBounds(p);
    if (!b) return true;
    return view.intersects(b);
  }

  function pieceForBounds(bounds) {
    const center = bounds.getCenter();
    let fallback = null;
    for (const p of pieces) {
      const b = pieceBounds(p);
      if (!b) continue;
      if (!fallback) fallback = p.name;
      if (b.contains(center)) return p.name;
    }
    for (const p of pieces) {
      const b = pieceBounds(p);
      if (b && bounds.intersects(b)) return p.name;
    }
    return fallback;
  }

  function ticks(limit) {
    const n = Number(limit);
    const out = [];
    if (!(n > 0)) return [0];
    for (let v = 0; v < n; v += 300) out.push(v);
    out.push(n);
    return out;
  }

  function syncGrids() {
    pieceGrids = [];
    if (!gridGroup) return;
    gridGroup.clearLayers();
    const show = !!(el('editLayerGrid') && el('editLayerGrid').checked);
    const pane = ensurePane('edit-grid', 460);
    for (const p of pieces) {
      if (!p.grid || !p.corners || typeof KnoxGrid === 'undefined' || !KnoxGrid.attach) continue;
      const g = KnoxGrid.attach(p.grid, p.corners);
      if (!g) continue;
      const w = +p.grid.width_tiles;
      const h = +p.grid.height_tiles;
      pieceGrids.push({ grid: g, width: w, height: h });
      if (!show) continue;
      const style = {
        pane, color: '#c6d36a', weight: 1, opacity: 0.45, interactive: false,
      };
      for (const x of ticks(w)) {
        const a = g.tileToLatLng(x, 0);
        const b = g.tileToLatLng(x, h);
        if (a && b) gridGroup.addLayer(L.polyline([[a.lat, a.lon], [b.lat, b.lon]], style));
      }
      for (const y of ticks(h)) {
        const a = g.tileToLatLng(0, y);
        const b = g.tileToLatLng(w, y);
        if (a && b) gridGroup.addLayer(L.polyline([[a.lat, a.lon], [b.lat, b.lon]], style));
      }
    }
    if (show && !editMap.hasLayer(gridGroup)) gridGroup.addTo(editMap);
    if (!show && editMap.hasLayer(gridGroup)) editMap.removeLayer(gridGroup);
  }

  function applyTerrainOpacity() {
    const box = el('editLayerTerrain');
    const slider = el('editOpacity');
    const on = !box || box.checked;
    const value = on && slider ? slider.value : '0';
    const mapEl = el('edit-map');
    if (mapEl) mapEl.style.setProperty('--edit-terrain', String(value));
  }

  function syncPaint() {
    const keep = new Set();
    for (const p of pieces) {
      if (!p.preview || !p.corners || p.corners.length < 4 || typeof PaintImage === 'undefined') continue;
      keep.add(p.name);
      let img = paintLayers.get(p.name);
      if (!img) {
        img = new PaintImage(p.preview, p.corners);
        img.addTo(editMap);
        paintLayers.set(p.name, img);
      } else if (img.setUrl) {
        img.setUrl(p.preview);
      }
    }
    for (const [name, img] of paintLayers) {
      if (keep.has(name)) continue;
      if (editMap.hasLayer(img)) editMap.removeLayer(img);
      paintLayers.delete(name);
    }
  }

  function applyLayerFlags() {
    const flags = {};
    let explicit = false;
    for (const p of pieces) {
      if (!p.layers) continue;
      explicit = true;
      for (const [api, on] of Object.entries(p.layers)) {
        if (on) flags[api] = true;
      }
    }
    document.querySelectorAll('#editLayers input[data-layer]').forEach((box) => {
      if (!explicit) {
        box.disabled = false;
        return;
      }
      const on = !!flags[box.dataset.layer];
      box.disabled = !on;
      if (!on) box.checked = false;
    });
  }

  function syncLayerVisibility() {
    for (const key of LAYER_ORDER) {
      const api = Object.keys(LAYER_API).find(name => LAYER_API[name] === key);
      const box = api && document.querySelector('#editLayers input[data-layer="' + api + '"]');
      const group = groups[key];
      if (!group || !editMap) continue;
      const on = !box || (box.checked && !box.disabled);
      if (on && !editMap.hasLayer(group)) group.addTo(editMap);
      if (!on && editMap.hasLayer(group)) editMap.removeLayer(group);
    }
    for (const key of LAYER_ORDER) {
      if (groups[key] && editMap.hasLayer(groups[key])) groups[key].bringToFront();
    }
  }

  function enabledLayerNames() {
    const names = [];
    document.querySelectorAll('#editLayers input[data-layer]').forEach((box) => {
      if (box.checked && !box.disabled) names.push(box.dataset.layer);
    });
    return names;
  }

  function scheduleFetch() {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(fetchFeatures, 200);
  }

  async function fetchFeatures() {
    const name = mapName();
    if (!name || !editMap || !pieces.length) return;
    const layers = enabledLayerNames();
    const token = ++featureToken;
    if (!layers.length) {
      for (const group of Object.values(groups)) group.clearLayers();
      return;
    }
    const bounds = editMap.getBounds();
    const zoom = editMap.getZoom();
    const hits = pieces.filter(p => intersectsView(p, bounds));
    const targets = hits.length ? hits : pieces;
    try {
      const collections = await Promise.all(targets.map((p) => {
        const q = new URLSearchParams({
          map: name,
          piece: p.name,
          layers: layers.join(','),
          south: String(bounds.getSouth()),
          west: String(bounds.getWest()),
          north: String(bounds.getNorth()),
          east: String(bounds.getEast()),
          zoom: String(zoom),
        });
        return getJson('/api/edit/features?' + q.toString());
      }));
      if (token !== featureToken) return;
      const buckets = { building: [], area: [], road: [], fence: [], place: [] };
      targets.forEach((p, i) => {
        const fc = collections[i] || {};
        for (const f of fc.features || []) {
          if (!f || !f.geometry) continue;
          const props = f.properties || (f.properties = {});
          props._piece = p.name;
          if (buckets[props.layer]) buckets[props.layer].push(f);
        }
      });
      for (const key of LAYER_ORDER) {
        groups[key].clearLayers();
        if (buckets[key].length) {
          groups[key].addData({ type: 'FeatureCollection', features: buckets[key] });
        }
      }
      restyle();
      syncLayerVisibility();
      clearErrorFrom('features');
    } catch (err) {
      if (token !== featureToken) return;
      showError(err.message || 'The edit request failed.', 'features');
    }
  }

  function fitPieces(done) {
    const pts = [];
    for (const p of pieces) {
      for (const c of p.corners || []) {
        if (c && c.length >= 2 && Number.isFinite(+c[0]) && Number.isFinite(+c[1])) {
          pts.push([+c[0], +c[1]]);
        }
      }
    }
    if (!pts.length) { if (done) done(); return; }
    const go = () => {
      editMap.invalidateSize();
      if (!editMap.getSize().x || !editMap.getSize().y) return false;
      editMap.fitBounds(L.latLngBounds(pts), { padding: [16, 16], animate: false });
      if (done) done();
      return true;
    };
    if (!go()) requestAnimationFrame(() => { if (!go() && done) done(); });
  }

  function fillSelect(sel, values, preferred) {
    if (!sel) return;
    const current = preferred !== undefined
      ? (preferred == null ? '' : String(preferred))
      : sel.value;
    const opts = [];
    const seen = new Set();
    for (const v of values || []) {
      const s = String(v);
      if (!s || seen.has(s)) continue;
      seen.add(s);
      opts.push(s);
    }
    if (current && !seen.has(current)) opts.push(current);
    sel.replaceChildren();
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = '—';
    sel.appendChild(blank);
    for (const s of opts) {
      const o = document.createElement('option');
      o.value = s;
      o.textContent = s;
      sel.appendChild(o);
    }
    sel.value = [...sel.options].some(o => o.value === current) ? current : '';
  }

  function applyVocabSelects() {
    fillSelect(el('editKind'), vocab.kinds);
    fillSelect(el('editStyle'), vocab.styles);
    fillSelect(el('editCategory'), vocab.categories);
    fillSelect(el('editBulkKind'), vocab.kinds);
    fillSelect(el('editBulkCategory'), vocab.categories);
    fillSelect(el('editZoneCategory'), vocab.categories);
  }

  function setInput(id, value) {
    const node = el(id);
    if (node) node.value = value == null ? '' : String(value);
  }

  function applyFor(layer) {
    const root = el('editInspector');
    root.querySelectorAll('[data-for]').forEach((node) => {
      const ok = node.dataset.for.split(/\s+/).includes(layer);
      if (node.id === 'editMeta') {
        if (!ok) node.hidden = true;
        return;
      }
      node.hidden = !ok;
    });
    if (layer === 'road' && inspectorState && !inspectorState.roadName) {
      el('editRenameWrap').hidden = true;
      el('editHide').hidden = true;
    }
  }

  function paintOffset() {
    const off = (inspectorState && inspectorState.offset) || [0, 0];
    el('editOffset').textContent = off[0] + ', ' + off[1];
  }

  function showMeta(place) {
    const meta = el('editMeta');
    if (!place) { meta.hidden = true; return; }
    meta.hidden = false;
    el('editRooms').textContent = place.rooms != null ? String(place.rooms) : '—';
    el('editFurniture').textContent = place.furniture != null ? String(place.furniture) : '—';
    el('editFile').textContent = place.file || '—';
    el('editPlaceTile').textContent = (place.tile_x != null && place.tile_y != null)
      ? (place.tile_x + ', ' + place.tile_y) : '—';
    el('editPlaceCell').textContent = (place.cell_x != null && place.cell_y != null)
      ? (place.cell_x + ', ' + place.cell_y) : '—';
  }

  function makeState(item) {
    const props = item.props || {};
    return {
      key: item.key,
      id: item.id,
      piece: item.piece,
      layer: item.layer,
      fid: props.fid || item.id,
      placeKey: item.id || props.name || '',
      roadName: String(props.name || '').trim(),
      offset: [0, 0],
    };
  }

  function fillFromItem(item) {
    const props = item.props || {};
    setInput('editName', props.name || '');
    if (item.layer === 'building') {
      fillSelect(el('editKind'), vocab.kinds, props.building || '');
      setInput('editLevels', '');
      fillSelect(el('editStyle'), vocab.styles, '');
      setInput('editSeed', '');
      inspectorState.offset = [0, 0];
      paintOffset();
      el('editMeta').hidden = true;
    } else if (item.layer === 'area') {
      fillSelect(el('editCategory'), vocab.categories, props.category || '');
    } else if (item.layer === 'place') {
      setInput('editPopulation', props.population != null ? props.population : '');
    } else if (item.layer === 'road') {
      el('editRenameAll').checked = false;
    }
  }

  function applyDetail(data) {
    if (!inspectorState) return;
    const props = data.properties || {};
    const place = data.placement || null;
    const edit = data.edit || {};
    const layer = data.layer || inspectorState.layer;
    setInput('editName', edit.name || props.name || (place && place.name) || '');
    if (layer === 'building') {
      fillSelect(el('editKind'), vocab.kinds, edit.kind || (place && place.kind) || props.building || '');
      const levels = edit.levels != null ? edit.levels : (place && place.levels != null ? place.levels : '');
      setInput('editLevels', levels);
      fillSelect(el('editStyle'), vocab.styles, edit.style || (place && place.style) || '');
      setInput('editSeed', edit.seed != null ? edit.seed : '');
      const off = Array.isArray(edit.offset) ? edit.offset : [0, 0];
      inspectorState.offset = [Number(off[0]) || 0, Number(off[1]) || 0];
      paintOffset();
      showMeta(place);
    } else if (layer === 'area') {
      fillSelect(el('editCategory'), vocab.categories, edit.category || props.category || '');
    } else if (layer === 'road') {
      inspectorState.roadName = String(props.name || inspectorState.roadName || '').trim();
      inspectorState.fid = props.fid || data.id || inspectorState.fid;
      setInput('editName', edit.name || props.name || '');
      applyFor('road');
    } else if (layer === 'place') {
      inspectorState.placeKey = data.id || inspectorState.placeKey;
      const pop = edit.population != null ? edit.population : props.population;
      setInput('editPopulation', pop != null ? pop : '');
    }
  }

  async function loadDetail(item) {
    const token = ++detailToken;
    const name = mapName();
    if (!name) return;
    try {
      const q = new URLSearchParams({ map: name, piece: item.piece, id: item.id });
      const data = await getJson('/api/edit/feature?' + q.toString());
      if (token !== detailToken) return;
      if (inspectorDirty || !inspectorState || inspectorState.key !== item.key) return;
      applyDetail(data);
    } catch (err) {
      if (token !== detailToken) return;
      showError(err.message || 'The edit request failed.', 'feature');
    }
  }

  function showOne(item) {
    const same = item.key === inspectorKey && inspectorState;
    if (!same) {
      inspectorKey = item.key;
      inspectorDirty = false;
      inspectorState = makeState(item);
      fillFromItem(item);
      loadDetail(item);
    }
    el('editInspector').hidden = false;
    applyFor(item.layer);
  }

  function onSelectionChanged() {
    selected = new Set(selection.map(s => s.key));
    restyle();
    const buildings = selection.filter(s => s.layer === 'building');
    const areas = selection.filter(s => s.layer === 'area');
    if (buildings.length + areas.length >= 2) {
      el('editInspector').hidden = true;
      el('editBulk').hidden = false;
      el('editBulkKindWrap').hidden = buildings.length === 0;
      el('editBulkCatWrap').hidden = areas.length === 0;
      el('editBulkReroll').hidden = buildings.length === 0;
      return;
    }
    el('editBulk').hidden = true;
    if (selection.length === 1 && EDITABLE.has(selection[0].layer)) {
      showOne(selection[0]);
      return;
    }
    el('editInspector').hidden = true;
  }

  function buildingPatch() {
    const rec = {};
    const name = el('editName').value.trim();
    if (name) rec.name = name;
    if (el('editKind').value) rec.kind = el('editKind').value;
    const levels = intOrNull(el('editLevels').value);
    if (levels != null) rec.levels = levels;
    if (el('editStyle').value) rec.style = el('editStyle').value;
    const seed = intOrNull(el('editSeed').value);
    if (seed != null) rec.seed = seed;
    const off = inspectorState.offset || [0, 0];
    if (off[0] || off[1]) rec.offset = [off[0] | 0, off[1] | 0];
    if (!Object.keys(rec).length) return null;
    return { buildings: { [inspectorState.id]: rec } };
  }

  function areaPatch() {
    const rec = {};
    const name = el('editName').value.trim();
    if (name) rec.name = name;
    if (el('editCategory').value) rec.category = el('editCategory').value;
    if (!Object.keys(rec).length) return null;
    return { areas: { [inspectorState.id]: rec } };
  }

  function roadPatch() {
    const newName = el('editName').value.trim();
    if (!newName) return null;
    if (el('editRenameAll').checked && inspectorState.roadName) {
      return { streets: { rename: { [inspectorState.roadName]: newName } } };
    }
    const fid = inspectorState.fid || inspectorState.id;
    return { streets: { ways: { [fid]: { name: newName } } } };
  }

  function placePatch() {
    const rec = {};
    const name = el('editName').value.trim();
    if (name) rec.name = name;
    const pop = intOrNull(el('editPopulation').value);
    if (pop != null) rec.population = pop;
    if (!Object.keys(rec).length) return null;
    const key = inspectorState.placeKey || inspectorState.id;
    return { places: { [key]: rec } };
  }

  function savePatch() {
    if (!inspectorState) return null;
    if (inspectorState.layer === 'building') return buildingPatch();
    if (inspectorState.layer === 'area') return areaPatch();
    if (inspectorState.layer === 'road') return roadPatch();
    if (inspectorState.layer === 'place') return placePatch();
    return null;
  }

  async function postEdits(piece, patch) {
    const data = await postJson('/api/edit/edits', {
      mapName: mapName(),
      piece,
      patch,
    });
    overlays.set(piece, data);
    if (Array.isArray(data.pendingStages)) setStages(data.pendingStages);
    renderPending();
    drawOverlayShapes();
    showError('');
    return data;
  }

  async function saveInspector() {
    const patch = savePatch();
    if (!patch || !inspectorState) return;
    const btn = el('editSave');
    btn.disabled = true;
    try {
      await postEdits(inspectorState.piece, patch);
      inspectorDirty = false;
      scheduleFetch();
    } finally {
      btn.disabled = false;
    }
  }

  async function deleteOrHide() {
    if (!inspectorState) return;
    const st = inspectorState;
    let patch = null;
    if (st.layer === 'building') patch = { buildings: { [st.id]: { deleted: true } } };
    else if (st.layer === 'area') patch = { areas: { [st.id]: { deleted: true } } };
    else if (st.layer === 'place') {
      patch = { places: { [st.placeKey || st.id]: { deleted: true } } };
    } else if (st.layer === 'road' && st.roadName) {
      const ov = overlays.get(st.piece) || {};
      const hide = ((ov.streets || {}).hide || []).slice();
      if (!hide.includes(st.roadName)) hide.push(st.roadName);
      patch = { streets: { hide } };
    }
    if (!patch) return;
    await postEdits(st.piece, patch);
    scheduleFetch();
  }

  async function forEachPiece(fn) {
    const by = new Map();
    for (const item of selection) {
      if (item.layer !== 'building' && item.layer !== 'area') continue;
      if (!by.has(item.piece)) by.set(item.piece, []);
      by.get(item.piece).push(item);
    }
    for (const [piece, items] of by) {
      const patch = fn(items);
      if (patch) await postEdits(piece, patch);
    }
    scheduleFetch();
  }

  async function bulkApply() {
    const kind = el('editBulkKindWrap').hidden ? '' : el('editBulkKind').value;
    const cat = el('editBulkCatWrap').hidden ? '' : el('editBulkCategory').value;
    if (!kind && !cat) {
      showError('Pick a kind or a category.', 'bulk');
      return;
    }
    await forEachPiece((items) => {
      const patch = {};
      if (kind) {
        for (const item of items) {
          if (item.layer !== 'building') continue;
          (patch.buildings || (patch.buildings = {}))[item.id] = { kind };
        }
      }
      if (cat) {
        for (const item of items) {
          if (item.layer !== 'area') continue;
          (patch.areas || (patch.areas = {}))[item.id] = { category: cat };
        }
      }
      return Object.keys(patch).length ? patch : null;
    });
  }

  async function bulkDelete() {
    await forEachPiece((items) => {
      const patch = {};
      for (const item of items) {
        const bucket = item.layer === 'building' ? 'buildings' : 'areas';
        (patch[bucket] || (patch[bucket] = {}))[item.id] = { deleted: true };
      }
      return Object.keys(patch).length ? patch : null;
    });
  }

  async function bulkReroll() {
    await forEachPiece((items) => {
      const buildings = {};
      for (const item of items) {
        if (item.layer === 'building') buildings[item.id] = { seed: randSeed() };
      }
      return Object.keys(buildings).length ? { buildings } : null;
    });
  }

  function describeBuilding(id, rec) {
    const bits = [];
    if (rec.deleted) bits.push('deleted');
    if (rec.name) bits.push('renamed to ' + rec.name);
    if (rec.kind) bits.push('kind ' + rec.kind);
    if (rec.levels != null) bits.push(rec.levels + ' levels');
    if (rec.style) bits.push(rec.style);
    if (rec.seed != null) bits.push('seed ' + rec.seed);
    if (Array.isArray(rec.offset) && (rec.offset[0] || rec.offset[1])) {
      bits.push('offset ' + rec.offset[0] + ', ' + rec.offset[1]);
    }
    if (!bits.length) bits.push('edited');
    return 'Building ' + id + ' ' + bits.join(', ');
  }

  function describeArea(id, rec) {
    if (rec.category && !rec.deleted && !rec.name) return 'Area ' + id + ' category ' + rec.category;
    const bits = [];
    if (rec.deleted) bits.push('deleted');
    if (rec.category) bits.push('category ' + rec.category);
    if (rec.name) bits.push('renamed to ' + rec.name);
    if (!bits.length) bits.push('edited');
    return 'Area ' + id + ' ' + bits.join(', ');
  }

  function describeRegion(r) {
    if (r.action === 'reroll') return 'Region ' + r.id + ' re-roll';
    if (r.action === 'rebuild') return 'Region ' + r.id + ' rebuild';
    if (r.action === 'zone') return 'Region ' + r.id + ' zoning ' + (r.category || '');
    return 'Region ' + r.id + ' ' + (r.action || 'edited');
  }

  function pushRow(rows, piece, kind, id, text) {
    rows.push({
      piece,
      kind,
      id,
      text: pieces.length > 1 ? (text + ' (' + piece + ')') : text,
    });
  }

  function collectRows() {
    const rows = [];
    for (const [piece, ov] of overlays) {
      if (!ov) continue;
      for (const [id, rec] of Object.entries(ov.buildings || {})) {
        if (rec) pushRow(rows, piece, 'building', id, describeBuilding(id, rec));
      }
      for (const [id, rec] of Object.entries(ov.areas || {})) {
        if (rec) pushRow(rows, piece, 'area', id, describeArea(id, rec));
      }
      const streets = ov.streets || {};
      for (const [oldName, newName] of Object.entries(streets.rename || {})) {
        if (newName == null) continue;
        pushRow(rows, piece, 'rename', oldName, oldName + ' renamed to ' + newName);
      }
      for (const name of streets.hide || []) {
        pushRow(rows, piece, 'hide', name, name + ' hidden');
      }
      for (const [fid, rec] of Object.entries(streets.ways || {})) {
        if (!rec) continue;
        const text = rec.name ? (fid + ' renamed to ' + rec.name) : (fid + ' renamed');
        pushRow(rows, piece, 'way', fid, text);
      }
      for (const [id, rec] of Object.entries(ov.places || {})) {
        if (!rec) continue;
        const bits = [];
        if (rec.deleted) bits.push('hidden');
        if (rec.name) bits.push('renamed to ' + rec.name);
        if (rec.population != null) bits.push('population ' + rec.population);
        if (!bits.length) bits.push('edited');
        pushRow(rows, piece, 'place', id, id + ' ' + bits.join(', '));
      }
      for (const region of ov.regions || []) {
        pushRow(rows, piece, 'region', region.id, describeRegion(region));
      }
      for (const area of ov.added_areas || []) {
        pushRow(rows, piece, 'added', area.id, 'Added area ' + (area.name || area.id));
      }
    }
    return rows;
  }

  function renderPending() {
    const rows = collectRows();
    const list = el('editPending');
    list.replaceChildren();
    for (const row of rows) {
      const li = document.createElement('li');
      const span = document.createElement('span');
      span.textContent = row.text;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-small';
      btn.textContent = 'Undo';
      btn.addEventListener('click', guard(() => undoRow(row)));
      li.append(span, btn);
      list.appendChild(li);
    }
    el('editPendingEmpty').hidden = rows.length > 0;
    el('editRevertAll').disabled = rows.length === 0;
  }

  async function undoRow(row) {
    const ov = overlays.get(row.piece) || {};
    let patch = null;
    if (row.kind === 'building') patch = { buildings: { [row.id]: null } };
    else if (row.kind === 'area') patch = { areas: { [row.id]: null } };
    else if (row.kind === 'place') patch = { places: { [row.id]: null } };
    else if (row.kind === 'rename') patch = { streets: { rename: { [row.id]: null } } };
    else if (row.kind === 'way') patch = { streets: { ways: { [row.id]: null } } };
    else if (row.kind === 'hide') {
      const hide = ((ov.streets || {}).hide || []).filter(name => name !== row.id);
      patch = { streets: { hide } };
    } else if (row.kind === 'region') {
      patch = { regions: (ov.regions || []).filter(r => r.id !== row.id) };
    } else if (row.kind === 'added') {
      patch = { added_areas: (ov.added_areas || []).filter(a => a.id !== row.id) };
    }
    if (!patch) return;
    await postEdits(row.piece, patch);
    scheduleFetch();
  }

  async function revertAll() {
    const name = mapName();
    if (!name) return;
    let last = null;
    for (const p of pieces) {
      last = await postJson('/api/edit/revert', { mapName: name, piece: p.name, clear: 'all' });
      overlays.set(p.name, last);
      if (Array.isArray(last.pendingStages)) setStages(last.pendingStages);
    }
    showError('');
    renderPending();
    drawOverlayShapes();
    scheduleFetch();
  }

  function asGeometry(shape) {
    if (!shape || !shape.type) return null;
    if (shape.type === 'Feature') return shape.geometry || null;
    return shape;
  }

  function drawOverlayShapes() {
    if (!editMap) return;
    if (overlayLayer) {
      editMap.removeLayer(overlayLayer);
      overlayLayer = null;
    }
    const features = [];
    const pane = ensurePane('edit-shapes', 450);
    for (const [piece, ov] of overlays) {
      if (!ov) continue;
      for (const area of ov.added_areas || []) {
        const geometry = asGeometry(area.geometry);
        if (!geometry) continue;
        features.push({
          type: 'Feature',
          id: area.id,
          properties: { kind: 'added', _piece: piece },
          geometry,
        });
      }
      for (const region of ov.regions || []) {
        const geometry = asGeometry(region.shape);
        if (!geometry) continue;
        features.push({
          type: 'Feature',
          id: region.id,
          properties: { kind: 'region', _piece: piece },
          geometry,
        });
      }
    }
    if (!features.length) return;
    overlayLayer = L.geoJSON({ type: 'FeatureCollection', features }, {
      pane,
      interactive: false,
      style: (f) => (f.properties && f.properties.kind === 'region'
        ? { color: '#f07a6e', weight: 2, dashArray: '6 4', fillColor: '#f07a6e', fillOpacity: 0.08 }
        : { color: '#7eb6ff', weight: 2, dashArray: '2 4', fillColor: '#7eb6ff', fillOpacity: 0.1 }),
    }).addTo(editMap);
  }

  function setStages(stages) {
    pendingStages = Array.isArray(stages) ? stages.slice() : [];
    renderApply();
  }

  function renderApply() {
    const seen = new Set();
    const stages = [];
    for (const s of pendingStages) {
      if (!s || seen.has(s)) continue;
      seen.add(s);
      stages.push(s);
    }
    stages.sort((a, b) => (STAGE_RANK[b] || 0) - (STAGE_RANK[a] || 0));
    const heavy = stages[0];
    const btn = el('editApply');
    btn.disabled = !heavy || applying;
    btn.textContent = (heavy && STAGE_BUTTON[heavy]) || 'Apply';
    const notes = el('editStageNotes');
    notes.replaceChildren();
    for (const s of stages) {
      const line = STAGE_LINE[s];
      if (!line) continue;
      const p = document.createElement('p');
      p.className = 'hint';
      p.textContent = line;
      notes.appendChild(p);
    }
  }

  function paintStatus(p) {
    const parts = [];
    if (p.message) parts.push(p.message);
    else if (p.stage) parts.push(p.stage);
    if (p.total) parts.push((p.done || 0) + ' / ' + p.total);
    el('editApplyStatus').textContent = parts.join(' · ') || 'Applying changes…';
  }

  function stopApply(message) {
    clearInterval(applyTimer);
    applyTimer = null;
    applyEpoch += 1;
    applying = false;
    if (message) el('editApplyStatus').textContent = message;
    renderApply();
  }

  async function completeApply() {
    if (!applying) return;
    clearInterval(applyTimer);
    applyTimer = null;
    applyEpoch += 1;
    applying = false;
    try {
      await refreshMapState();
    } catch (err) {
      showError(err.message || 'The edit request failed.', 'apply');
    }
    el('editApplyStatus').textContent = '';
    renderApply();
  }

  function startApplyPoll(name) {
    const epoch = ++applyEpoch;
    clearInterval(applyTimer);
    let saw = false;
    let idleDone = 0;
    const tick = async () => {
      if (epoch !== applyEpoch) return;
      let p = null;
      try {
        const res = await fetch('/api/progress?map=' + encodeURIComponent(name));
        p = await readJson(res);
      } catch (_) { return; }
      if (epoch !== applyEpoch || !p) return;
      if (p.error) { stopApply(p.error); return; }
      const stage = p.stage;
      if (stage && stage !== 'done' && stage !== 'stopped') {
        saw = true;
        idleDone = 0;
        paintStatus(p);
        return;
      }
      if (stage === 'stopped') {
        stopApply(p.message || '');
        return;
      }
      if (stage === 'done' && saw) {
        await completeApply();
        return;
      }
      if (stage === 'done') {
        idleDone += 1;
        if (idleDone < 2) return;
        try {
          const info = await getJson('/api/edit/map?map=' + encodeURIComponent(name));
          if (epoch !== applyEpoch) return;
          if (!info.pendingStages || !info.pendingStages.length) await completeApply();
          else idleDone = 0;
        } catch (_) { idleDone = 0; }
      }
    };
    tick();
    applyTimer = setInterval(tick, 1500);
  }

  async function applyEdits() {
    const name = mapName();
    if (!name || applying || !pendingStages.length) return;
    applying = true;
    renderApply();
    el('editApplyStatus').textContent = 'Applying changes…';
    try {
      const data = await postJson('/api/edit/apply', { mapName: name });
      showError('');
      if (data && data.started === false) {
        applying = false;
        setStages(Array.isArray(data.pendingStages) ? data.pendingStages : []);
        return;
      }
      if (Array.isArray(data.pendingStages)) setStages(data.pendingStages);
      startApplyPoll(name);
    } catch (err) {
      applying = false;
      renderApply();
      throw err;
    }
  }

  function markArmed(intent) {
    for (const [key, id] of Object.entries(DRAW_BUTTONS)) {
      const btn = el(id);
      if (btn) btn.setAttribute('aria-pressed', key === intent ? 'true' : 'false');
    }
  }

  function disarm() {
    const current = drawer;
    drawer = null;
    drawIntent = null;
    if (current) current.disable();
    const hint = el('editDrawHint');
    if (hint) hint.hidden = true;
    markArmed(null);
  }

  function zoneCategory() {
    return el('editZoneCategory').value;
  }

  function arm(intent) {
    if (!editMap) return;
    if (drawIntent === intent) { disarm(); return; }
    if ((intent === 'zone' || intent === 'area') && !zoneCategory()) {
      showError('Pick a category first.', 'draw');
      return;
    }
    disarm();
    const polygon = intent === 'area';
    const Ctor = polygon ? (L.Draw && L.Draw.Polygon) : (L.Draw && L.Draw.Rectangle);
    if (!Ctor) return;
    drawer = new Ctor(editMap, {
      shapeOptions: {
        color: '#a5e266', weight: 2, opacity: 0.95,
        fillColor: '#a5e266', fillOpacity: 0.12,
      },
    });
    drawIntent = intent;
    drawer.enable();
    const hint = el('editDrawHint');
    hint.hidden = false;
    hint.textContent = polygon
      ? 'Draw a polygon on the map.'
      : 'Drag a rectangle on the map.';
    markArmed(intent);
  }

  function onDrawCreated(e) {
    const intent = drawIntent;
    let bounds = null;
    let geometry = null;
    try {
      bounds = e.layer.getBounds();
      geometry = e.layer.toGeoJSON().geometry;
    } catch (_) { return; }
    if (!intent || !bounds || !geometry) return;
    if (intent === 'select') {
      selectInBounds(bounds, false);
      return;
    }
    guard(() => commitDraw(intent, bounds, geometry))();
  }

  async function commitDraw(intent, bounds, geometry) {
    const piece = pieceForBounds(bounds);
    if (!piece) {
      showError('This map has no pieces to edit.', 'draw');
      return;
    }
    if ((intent === 'zone' || intent === 'area') && !zoneCategory()) {
      showError('Pick a category first.', 'draw');
      return;
    }
    const ov = overlays.get(piece) || {};
    if (intent === 'area') {
      const added = (ov.added_areas || []).slice();
      added.push({
        id: 'u' + Date.now(),
        category: zoneCategory(),
        name: el('editAreaName').value.trim(),
        geometry,
      });
      await postEdits(piece, { added_areas: added });
    } else {
      const action = intent === 'rebuild' ? 'rebuild' : (intent === 'zone' ? 'zone' : 'reroll');
      const region = { id: 'r' + Date.now(), action, shape: geometry };
      if (action === 'reroll') region.seed = randSeed();
      if (action === 'zone') region.category = zoneCategory();
      const regions = (ov.regions || []).slice();
      regions.push(region);
      await postEdits(piece, { regions });
    }
    scheduleFetch();
  }

  async function loadVocab(name) {
    const data = await getJson('/api/edit/vocab?map=' + encodeURIComponent(name));
    vocab = {
      kinds: data.kinds || [],
      categories: data.categories || [],
      styles: data.styles || [],
    };
    applyVocabSelects();
  }

  async function loadEdits(name) {
    const next = new Map();
    let failed = null;
    await Promise.all(pieces.map(async (p) => {
      try {
        const q = new URLSearchParams({ map: name, piece: p.name });
        next.set(p.name, await getJson('/api/edit/edits?' + q.toString()));
      } catch (err) {
        failed = err;
        next.set(p.name, overlays.get(p.name) || {});
      }
    }));
    if (mapName() !== name) return;
    overlays = next;
    renderPending();
    drawOverlayShapes();
    if (failed) throw failed;
  }

  async function refreshMapState() {
    const name = mapName();
    if (!name) return;
    const info = await getJson('/api/edit/map?map=' + encodeURIComponent(name));
    if (mapName() !== name) return;
    if (Array.isArray(info.pendingStages)) setStages(info.pendingStages);
    el('editStable').hidden = info.stableIds !== false;
    if (Array.isArray(info.pieces) && info.pieces.length) {
      pieces = info.pieces;
      el('editNoPieces').hidden = true;
      syncGrids();
      syncPaint();
    }
    await loadEdits(name);
    scheduleFetch();
  }

  async function load(name) {
    const token = ++loadToken;
    featureToken += 1;
    try {
      const info = await getJson('/api/edit/map?map=' + encodeURIComponent(name));
      if (token !== loadToken || mapName() !== name) return;
      pieces = info.pieces || [];
      setStages(info.pendingStages || []);
      el('editEmpty').hidden = true;
      el('editTools').hidden = false;
      el('editStable').hidden = info.stableIds !== false;
      el('editNoPieces').hidden = pieces.length > 0;
      applyLayerFlags();
      syncLayerVisibility();
      syncGrids();
      syncPaint();
      loadedFor = name;
      try { await loadVocab(name); }
      catch (err) {
        if (token === loadToken) showError(err.message || 'The edit request failed.', 'map');
      }
      if (token !== loadToken || mapName() !== name) return;
      try { await loadEdits(name); }
      catch (err) {
        if (token === loadToken) showError(err.message || 'The edit request failed.', 'map');
      }
      if (token !== loadToken) return;
      if (needFit) {
        needFit = false;
        fitPieces(scheduleFetch);
      } else {
        scheduleFetch();
      }
    } catch (err) {
      if (token !== loadToken) return;
      loadedFor = null;
      el('editTools').hidden = true;
      el('editEmpty').hidden = true;
      showError(err.message || 'The edit request failed.', 'map');
    }
  }

  async function refreshQuiet(name) {
    try {
      const info = await getJson('/api/edit/map?map=' + encodeURIComponent(name));
      if (mapName() !== name) return;
      if (Array.isArray(info.pendingStages)) setStages(info.pendingStages);
      el('editStable').hidden = info.stableIds !== false;
      await loadEdits(name);
      scheduleFetch();
    } catch (err) {
      showError(err.message || 'The edit request failed.', 'map');
    }
  }

  function show() {
    ensureMap();
    syncTiles();
    const name = mapName();
    if (!name) {
      el('editEmpty').hidden = false;
      el('editTools').hidden = true;
      showError('');
      requestAnimationFrame(() => { if (editMap) editMap.invalidateSize(); });
      return;
    }
    el('editEmpty').hidden = true;
    if (loadedFor !== name) {
      selection = [];
      selected = new Set();
      inspectorKey = null;
      inspectorState = null;
      inspectorDirty = false;
      el('editInspector').hidden = true;
      el('editBulk').hidden = true;
      needFit = true;
      if (applying) stopApply('');
      el('editApplyStatus').textContent = '';
      load(name);
      return;
    }
    el('editTools').hidden = false;
    editMap.invalidateSize();
    refreshQuiet(name);
  }

  function hide() {
    disarm();
  }

  function onLayerChange(e) {
    const t = e.target;
    if (!t || t.type !== 'checkbox' && t.type !== 'range') return;
    if (t.id === 'editLayerGrid') syncGrids();
    else if (t.id === 'editLayerTerrain' || t.id === 'editOpacity') applyTerrainOpacity();
    else syncLayerVisibility();
    if (t.dataset && t.dataset.layer) scheduleFetch();
  }

  function nudge(dx, dy) {
    if (!inspectorState) return;
    inspectorState.offset = [
      (inspectorState.offset[0] || 0) + dx,
      (inspectorState.offset[1] || 0) + dy,
    ];
    inspectorDirty = true;
    paintOffset();
  }

  function init() {
    const tools = el('editTools');
    if (!tools) return;
    el('editLayers').addEventListener('change', onLayerChange);
    el('editOpacity').addEventListener('input', applyTerrainOpacity);
    const inspector = el('editInspector');
    inspector.addEventListener('input', () => { inspectorDirty = true; });
    inspector.addEventListener('change', () => { inspectorDirty = true; });
    el('editNudgeXm').addEventListener('click', () => nudge(-1, 0));
    el('editNudgeXp').addEventListener('click', () => nudge(1, 0));
    el('editNudgeYm').addEventListener('click', () => nudge(0, -1));
    el('editNudgeYp').addEventListener('click', () => nudge(0, 1));
    el('editSave').addEventListener('click', guard(saveInspector));
    el('editDelete').addEventListener('click', guard(deleteOrHide));
    el('editHide').addEventListener('click', guard(deleteOrHide));
    el('editBulkApply').addEventListener('click', guard(bulkApply));
    el('editBulkDelete').addEventListener('click', guard(bulkDelete));
    el('editBulkReroll').addEventListener('click', guard(bulkReroll));
    el('editRevertAll').addEventListener('click', guard(revertAll));
    el('editReroll').addEventListener('click', () => arm('reroll'));
    el('editRebuild').addEventListener('click', () => arm('rebuild'));
    el('editZone').addEventListener('click', () => arm('zone'));
    el('editDrawArea').addEventListener('click', () => arm('area'));
    el('editSelectRect').addEventListener('click', () => arm('select'));
    el('editApply').addEventListener('click', guard(applyEdits));
    const bases = el('basemaps');
    if (bases) bases.addEventListener('click', () => setTimeout(syncTiles, 0));
    window.addEventListener('storage', (e) => {
      if (e.key === 'knoxmap.base') syncTiles();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && drawIntent) disarm();
    });
    applyVocabSelects();
    renderApply();
  }

  init();
  window.knoxEdit = { show, hide };
  window.knoxEditMap = null;
})();
