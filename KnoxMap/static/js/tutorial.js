// First launch only. Asks once, then walks the map, the tools on it,
// the sidebar, and where to get help. Remembered in localStorage so a
// later launch goes straight to the window.

const tutorial = (() => {
  const KEY = 'knoxmap.tutorial';
  const ASK = 'It looks like this is your first time using KnoxMap. Would you like to go through the quick tutorial?';
  const DISCORD = 'https://discord.gg/ePM8dSxPm7';
  const STEPS = [
    {
      title: 'Map viewer',
      body: 'This is the map. Drag to move it and scroll to zoom. It opens on Lexington, Kentucky. Find the place you want to turn into a Project Zomboid map.',
      targets: ['#map'],
      place: 'bottom',
    },
    {
      title: 'Map interface',
      body: 'Search for a place at the top left. Dark and Light at the top right switch the basemap. The tools on the left draw the area: rectangle, polygon, circle or freehand. The eraser cuts a shape out of it.',
      targets: ['#search-box', '#map-tools', '.leaflet-top.leaflet-left'],
      place: 'bottom',
    },
    {
      title: 'Sidebar',
      body: 'Previous and Next step through Setup, Map and Build. Setup finds the game and names the map. Map is the area you drew. Build generates, compiles and installs the mod.',
      targets: ['#controls'],
      place: 'left-of',
    },
    {
      title: 'More help',
      body: 'The Discord icon in the top bar is where to ask for help.',
      link: DISCORD,
      targets: ['a.discord'],
      place: 'below',
    },
  ];

  const root = document.getElementById('tutorial');
  const dim = document.getElementById('tutorialDim');
  const rings = document.getElementById('tutorialRings');
  const card = root.querySelector('.tutorial-card');
  const titleEl = document.getElementById('tutorialTitle');
  const bodyEl = document.getElementById('tutorialBody');
  const linkEl = document.getElementById('tutorialLink');
  const skipBtn = document.getElementById('tutorialSkip');
  const askEl = document.getElementById('tutorialAsk');
  const navEl = document.getElementById('tutorialNav');
  const backBtn = document.getElementById('tutorialBack');
  const nextBtn = document.getElementById('tutorialNext');
  const countEl = document.getElementById('tutorialCount');
  const yesBtn = document.getElementById('tutorialYes');

  let open = false;
  let mode = 'ask';
  let index = 0;

  function alreadySeen() {
    try { return localStorage.getItem(KEY) === '1'; }
    catch (_) { return true; }
  }

  function markSeen() {
    try { localStorage.setItem(KEY, '1'); }
    catch (_) { /* a window that cannot remember still closes the tutorial */ }
  }

  function rectsFor(selectors) {
    const found = [];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      found.push(r);
    }
    return found;
  }

  function paint(rects) {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const pad = 8;
    dim.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    dim.setAttribute('width', String(w));
    dim.setAttribute('height', String(h));
    const holes = rects.map(r => {
      const x = Math.round(r.left - pad);
      const y = Math.round(r.top - pad);
      const rw = Math.round(r.width + pad * 2);
      const rh = Math.round(r.height + pad * 2);
      return '<rect x="' + x + '" y="' + y + '" width="' + rw + '" height="' + rh + '" rx="8" fill="black"/>';
    }).join('');
    dim.innerHTML = '<defs><mask id="tutorialHole" maskUnits="userSpaceOnUse" x="0" y="0" width="'
      + w + '" height="' + h + '"><rect width="' + w + '" height="' + h + '" fill="white"/>'
      + holes + '</mask></defs><rect width="' + w + '" height="' + h
      + '" fill="rgba(7,9,11,0.75)" mask="url(#tutorialHole)"/>';
    rings.replaceChildren();
    for (const r of rects) {
      const ring = document.createElement('div');
      ring.className = 'tutorial-ring';
      ring.style.left = Math.round(r.left - pad) + 'px';
      ring.style.top = Math.round(r.top - pad) + 'px';
      ring.style.width = Math.round(r.width + pad * 2) + 'px';
      ring.style.height = Math.round(r.height + pad * 2) + 'px';
      rings.append(ring);
    }
  }

  function placeCard(where) {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const m = 16;
    const cw = card.offsetWidth;
    const ch = card.offsetHeight;
    let left = (vw - cw) / 2;
    let top = (vh - ch) / 2;
    if (where === 'bottom') {
      const map = document.getElementById('map-pane').getBoundingClientRect();
      left = map.left + (map.width - cw) / 2;
      top = map.bottom - ch - 18;
    } else if (where === 'left-of') {
      const side = document.getElementById('controls').getBoundingClientRect();
      left = side.left - cw - 16;
      top = side.top + 72;
      if (left < m) {
        const map = document.getElementById('map-pane').getBoundingClientRect();
        left = map.left + (map.width - cw) / 2;
        top = Math.max(m, map.bottom - ch - 18);
      }
    } else if (where === 'below') {
      const icon = document.querySelector('a.discord');
      if (icon) {
        const r = icon.getBoundingClientRect();
        left = r.left;
        top = r.bottom + 14;
        const search = document.getElementById('search-box');
        if (search) {
          const s = search.getBoundingClientRect();
          const overlaps = top < s.bottom && top + ch > s.top && left < s.right && left + cw > s.left;
          if (overlaps) top = s.bottom + 12;
        }
      }
    }
    left = Math.min(Math.max(left, m), Math.max(m, vw - cw - m));
    top = Math.min(Math.max(top, m), Math.max(m, vh - ch - m));
    card.style.left = Math.round(left) + 'px';
    card.style.top = Math.round(top) + 'px';
  }

  function setLink(href) {
    linkEl.replaceChildren();
    if (!href) { linkEl.hidden = true; return; }
    const a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = href;
    linkEl.hidden = false;
    linkEl.append(a);
  }

  function showAsk() {
    mode = 'ask';
    open = true;
    titleEl.hidden = true;
    titleEl.textContent = '';
    bodyEl.textContent = ASK;
    setLink('');
    skipBtn.hidden = true;
    askEl.hidden = false;
    navEl.hidden = true;
    card.setAttribute('aria-labelledby', 'tutorialBody');
    root.hidden = false;
    paint([]);
    placeCard('center');
    yesBtn.focus();
  }

  function showStep() {
    const step = STEPS[index];
    mode = 'steps';
    open = true;
    titleEl.hidden = false;
    titleEl.textContent = step.title;
    bodyEl.textContent = step.body;
    setLink(step.link || '');
    skipBtn.hidden = false;
    askEl.hidden = true;
    navEl.hidden = false;
    countEl.textContent = (index + 1) + ' / ' + STEPS.length;
    backBtn.disabled = index === 0;
    card.setAttribute('aria-labelledby', 'tutorialTitle');
    root.hidden = false;
    paint(rectsFor(step.targets));
    placeCard(step.place);
  }

  function finish() {
    open = false;
    root.hidden = true;
    markSeen();
  }

  function forward() {
    if (index >= STEPS.length - 1) { finish(); return; }
    index += 1;
    showStep();
  }

  function back() {
    if (index <= 0) return;
    index -= 1;
    showStep();
  }

  function relayout() {
    if (!open) return;
    if (mode === 'ask') { paint([]); placeCard('center'); return; }
    const step = STEPS[index];
    paint(rectsFor(step.targets));
    placeCard(step.place);
  }

  yesBtn.addEventListener('click', () => { index = 0; showStep(); });
  document.getElementById('tutorialNo').addEventListener('click', finish);
  skipBtn.addEventListener('click', finish);
  backBtn.addEventListener('click', back);
  nextBtn.addEventListener('click', forward);
  window.addEventListener('resize', relayout);
  document.addEventListener('keydown', (e) => {
    if (!open) return;
    if (e.key === 'Escape') { e.preventDefault(); finish(); return; }
    if (mode !== 'steps') return;
    if (e.key === 'ArrowRight') { e.preventDefault(); forward(); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); back(); }
  });

  document.addEventListener('DOMContentLoaded', () => {
    if (alreadySeen()) return;
    requestAnimationFrame(() => requestAnimationFrame(showAsk));
  });

  return { relayout };
})();
