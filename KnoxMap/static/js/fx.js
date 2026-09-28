// KnoxMap - the small bits of UI glue app.js calls into.
//
// It never talks to the server or decides anything: app.js calls `fx` when an
// area is chosen, a step starts or finishes, and this shows it. If this file
// failed to load, the app would still work.

const fx = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);

  // Steps used to light up in a header bar; the panels now say it themselves.
  function step() {}
  function resetFrom() {}

  // ---- messages -----------------------------------------------------------
  function toast(kind, title, msg, ms = 5000) {
    const box = $('#toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = '<b></b> <span></span>';
    el.querySelector('b').textContent = title;
    el.querySelector('span').textContent = msg || '';
    box.appendChild(el);
    const kill = () => el.remove();
    el.addEventListener('click', kill);
    setTimeout(kill, ms);
  }

  // An error worth reporting: the message, its id in the log, and two ways to
  // pass it on - the details copied for a Discord message, or the whole
  // report zip saved into the logs folder and shown in Explorer.
  function problem(title, msg, errorId) {
    const box = $('#toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = 'toast bad problem';
    el.innerHTML = '<b></b> <span class="msg"></span><div class="eid"></div>'
      + '<div class="toast-actions"><button type="button" class="copy">Copy details</button>'
      + '<button type="button" class="report">Save report</button>'
      + '<button type="button" class="dismiss">Dismiss</button></div>';
    el.querySelector('b').textContent = title;
    el.querySelector('.msg').textContent = msg || '';
    el.querySelector('.eid').textContent = errorId ? `Error ${errorId}` : '';
    el.querySelector('.dismiss').addEventListener('click', () => el.remove());
    el.querySelector('.copy').addEventListener('click', async (e) => {
      const version = $('#appVersion')?.textContent || '';
      const text = [`KnoxMap ${version}: ${title}`, msg || '', errorId || ''].filter(Boolean).join('\n');
      try { await navigator.clipboard.writeText(text); e.target.textContent = 'Copied'; }
      catch (_) { e.target.textContent = 'Could not copy'; }
    });
    el.querySelector('.report').addEventListener('click', saveReport);
    box.appendChild(el);
  }

  async function saveReport() {
    try {
      const res = await fetch('/api/report-save', { method: 'POST' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      toast('ok', 'Report saved',
            `${data.name} is in KnoxMap's logs folder. Post it in #bug-reports on the Discord.`,
            15000);
    } catch (_) {
      // In a plain browser rather than the app window: download it instead.
      window.location.href = '/api/report';
    }
  }

  // ---- numbers ------------------------------------------------------------
  function countUp(root = document) {
    root.querySelectorAll('[data-count]').forEach(el => {
      const value = parseFloat(el.dataset.count);
      const decimals = parseInt(el.dataset.decimals || '0', 10);
      el.textContent = isFinite(value) ? value.toLocaleString(undefined, {
        minimumFractionDigits: decimals, maximumFractionDigits: decimals,
      }) : '';
    });
  }

  function tile(value, unit, label, decimals = 0) {
    return `<div class="tile"><div class="v"><span data-count="${value}"
      data-decimals="${decimals}">0</span>${unit ? ` <small>${unit}</small>` : ''}</div>
      <div class="k">${label}</div></div>`;
  }

  // ---- terrain progress ---------------------------------------------------
  function bytes(n) {
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(i >= 2 && n < 100 ? 1 : 0)} ${units[i]}`;
  }

  function duration(s) {
    s = Math.max(1, Math.round(s));
    if (s < 60) return `${s} s`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${m % 60} min`;
  }

  let clockTimer = null;
  let hideTimer = null;
  let started = 0;

  function formatClock(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(r).padStart(2, '0');
    if (h) return `${h}:${mm}:${ss}`;
    return `${m}:${ss}`;
  }

  function paintClock(seconds) {
    $('#gen-clock').textContent = formatClock(seconds);
  }

  function showElapsedWord(on) {
    const word = $('#gen-elapsed-word');
    if (word) word.hidden = !on;
  }

  function armClock(reset) {
    if (reset) started = Date.now();
    clearInterval(clockTimer);
    const tick = () => paintClock(Math.floor((Date.now() - started) / 1000));
    tick();
    clockTimer = setInterval(tick, 500);
  }

  const overlay = {
    show() {
      clearTimeout(hideTimer);
      const el = $('#gen-overlay');
      el.hidden = false;
      el.dataset.mode = 'terrain';
      $('.gen-kicker', el).textContent = 'Generating terrain';
      $('#gen-stage').textContent = 'Checking daily map extracts…';
      $('#gen-detail').textContent = '';
      $('#gen-bar').style.width = '4%';
      showElapsedWord(false);
      armClock(true);
    },
    buildings(view) {
      clearTimeout(hideTimer);
      const el = $('#gen-overlay');
      const starting = el.hidden || el.dataset.mode !== 'buildings';
      el.hidden = false;
      el.dataset.mode = 'buildings';
      $('.gen-kicker', el).textContent = 'Generating buildings';
      $('#gen-stage').textContent = (view && view.stage) || 'Generating buildings';
      $('#gen-detail').textContent = (view && view.detail) || '';
      const pct = Math.max(0, Math.min(100, Number(view && view.pct) || 0));
      $('#gen-bar').style.width = `${Math.max(pct, 2)}%`;
      showElapsedWord(true);
      if (view && typeof view.elapsed === 'number') {
        clearInterval(clockTimer);
        clockTimer = null;
        paintClock(view.elapsed);
      } else if (starting) {
        armClock(true);
      }
    },
    update(p) {
      const el = $('#gen-overlay');
      if (el && el.dataset.mode === 'buildings') return;
      if (p.stage === 'osm' || p.stage === 'regions') {
        const total = p.total || 1;
        const done = p.done || 0;
        const dl = p.download;
        const share = dl && dl.total ? Math.min(1, dl.done / dl.total) : 0;
        let stage = p.message || 'Checking daily map extracts';
        if (dl && dl.total > 0) {
          const pct = Math.min(100, Math.round(100 * dl.done / dl.total));
          const left = dl.speed > 0 && dl.total > dl.done
            ? `, ${duration((dl.total - dl.done) / dl.speed)} left`
            : '';
          stage = `${stage} · ${pct}%${left}`;
        }
        $('#gen-stage').textContent = stage;
        let detail = p.detail || (total > 1 ? `Part ${done + 1} of ${total}` : '');
        if (dl) {
          const parts = [];
          if (dl.total > 0) {
            parts.push(`${Math.min(100, Math.round(100 * dl.done / dl.total))}%`);
          }
          parts.push(dl.total ? `${bytes(dl.done)} of ${bytes(dl.total)}` : bytes(dl.done));
          if (dl.speed > 0) parts.push(`${bytes(dl.speed)}/s`);
          if (dl.speed > 0 && dl.total > dl.done) {
            parts.push(`${duration((dl.total - dl.done) / dl.speed)} left`);
          }
          if (total > 1) parts.push(`region ${done + 1} of ${total}`);
          detail = `${detail ? detail + ': ' : ''}${parts.join(' · ')}`;
        }
        $('#gen-detail').textContent = detail;
        $('#gen-bar').style.width = `${Math.max(6, Math.round(8 + 40 * (done + share) / total))}%`;
      } else if (p.stage === 'mod') {
        const total = p.total || 1;
        const done = p.done || 0;
        $('#gen-stage').textContent = p.view || 'Drawing a map piece';
        $('#gen-detail').textContent = p.detail || `Piece ${done + 1} of ${total}`;
        $('#gen-bar').style.width = `${Math.max(48, Math.round(48 + 34 * done / total))}%`;
      } else if (p.stage === 'render') {
        $('#gen-stage').textContent = p.view || 'Drawing the terrain';
        $('#gen-detail').textContent = `${(p.features || 0).toLocaleString()} features`;
        $('#gen-bar').style.width = '82%';
      } else if (p.view) {
        $('#gen-stage').textContent = p.view;
      }
    },
    done(summary) {
      clearTimeout(hideTimer);
      const el = $('#gen-overlay');
      el.hidden = false;
      delete el.dataset.mode;
      $('.gen-kicker', el).textContent = 'Done';
      $('#gen-stage').textContent = summary || '';
      $('#gen-detail').textContent = '';
      $('#gen-bar').style.width = '100%';
      showElapsedWord(false);
      this.hide(600);
    },
    fail(message) {
      clearTimeout(hideTimer);
      const el = $('#gen-overlay');
      el.hidden = false;
      delete el.dataset.mode;
      $('.gen-kicker', el).textContent = 'Failed';
      $('#gen-stage').textContent = message.length > 160 ? message.slice(0, 157) + '…' : message;
      $('#gen-detail').textContent = '';
      showElapsedWord(false);
      this.hide(2500);
    },
    hide(delay = 0) {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(() => {
        const el = $('#gen-overlay');
        el.hidden = true;
        delete el.dataset.mode;
        clearInterval(clockTimer);
        clockTimer = null;
      }, delay);
    },
  };

  // ---- the build / compile / install steps --------------------------------
  const NOTE_STEP = {
    buildingsNote: 'buildings', compileNote: 'compile',
    worldedNote: 'compile', installNote: 'install',
  };
  const BUSY = /generating|starting|installing|compiling|waiting|opened/i;

  function card(key, state) {
    const el = $(`.pipe-card[data-step="${key}"]`);
    if (!el) return;
    el.classList.remove('is-running', 'is-done', 'is-error', 'is-ready');
    if (state) el.classList.add(`is-${state}`);
  }

  function noted(id, text, cls) {
    const key = NOTE_STEP[id];
    if (!key) return;
    if (cls === 'ok') card(key, 'done');
    // 'warn' is a step that finished and still needs attention - a compile
    // that stepped over a batch of cells. It has stopped, so the card must
    // not go on saying it is running.
    else if (cls === 'bad' || cls === 'warn') card(key, 'error');
    else if (BUSY.test(text)) card(key, 'running');
    else if (/^ready/i.test(text)) card(key, 'ready');
  }

  function progress(key, pct) {
    const bar = key === 'compile' ? $('#compileBar')
      : key === 'buildings' ? $('#buildingsBar') : null;
    if (!bar) return;
    const meter = bar.parentElement;
    const measured = pct > 0;
    meter.classList.toggle('is-indeterminate', !measured);
    meter.classList.toggle('is-measured', measured);
    bar.style.width = measured ? `${Math.min(100, pct)}%` : '';
  }

  // ---- theme ------------------------------------------------------------------
  // Dark and Light restyle the whole window. The same choice picks the map
  // tiles: streets as they are, or the same tiles darkened in CSS.
  let showTiles = () => {};

  function savedBase() {
    let saved = 'dark';
    try { saved = localStorage.getItem('knoxmap.base') || 'dark'; } catch (_) {}
    return saved === 'streets' ? 'streets' : 'dark';
  }

  function applyTheme(name) {
    document.documentElement.dataset.theme = name === 'streets' ? 'light' : 'dark';
    document.querySelectorAll('#basemaps button').forEach(btn => {
      btn.classList.toggle('is-on', btn.dataset.base === name);
    });
  }

  function chooseBase(name) {
    const base = name === 'streets' ? 'streets' : 'dark';
    applyTheme(base);
    try { localStorage.setItem('knoxmap.base', base); } catch (_) {}
    showTiles(base);
  }

  function mapExtras() {
    if (typeof map === 'undefined') return;
    const attribution = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';
    const layers = {
      streets: L.tileLayer('/tiles/{z}/{x}/{y}.png', { maxZoom: 19, attribution }),
      dark: L.tileLayer('/tiles/{z}/{x}/{y}.png', { maxZoom: 19, className: 'tiles-dark', attribution }),
    };
    const existing = [];
    map.eachLayer(l => { if (l instanceof L.TileLayer) existing.push(l); });
    existing.forEach(l => map.removeLayer(l));
    showTiles = (name) => {
      Object.values(layers).forEach(l => map.removeLayer(l));
      layers[name].addTo(map);
      map.getContainer().classList.toggle('base-light', name === 'streets');
    };
    showTiles(savedBase());
  }

  // ---- default population ------------------------------------------------------
  function presetCards() {
    const select = $('#preset');
    if (!select) return;
    document.querySelectorAll('#presetCards .preset-card').forEach(card => {
      card.addEventListener('click', () => {
        document.querySelectorAll('#presetCards .preset-card').forEach(c => {
          const on = c === card;
          c.classList.toggle('is-on', on);
          c.setAttribute('aria-checked', on ? 'true' : 'false');
        });
        select.value = card.dataset.preset;
        // app.js listens on the real select; it stays the single source of truth.
        select.dispatchEvent(new Event('change'));
      });
    });
  }

  // ---- settings form ------------------------------------------------------------
  function paintRange(input) {
    const v = parseFloat(input.value);
    const out = input.closest('.setting')?.querySelector('.setting-val');
    if (out) out.textContent = input.step && parseFloat(input.step) < 1 ? v.toFixed(2) : String(v);
  }

  function wireSettings(root) {
    root.querySelectorAll('input[type="range"]').forEach(r => {
      paintRange(r);
      r.addEventListener('input', () => paintRange(r));
    });
    root.querySelectorAll('.dice').forEach(d => {
      d.addEventListener('click', () => {
        d.parentElement.querySelector('input').value = Math.floor(Math.random() * 999999) + 1;
      });
    });
  }

  // ---- updates ------------------------------------------------------------
  // updater.py downloads a newer release in the background; once it is
  // ready the header offers a restart, which installs it.
  // The version chip wears a dot: green on its own, and amber once a newer
  // release has downloaded and only a restart is left.
  function versionDot(state, version) {
    const dot = $('#versionDot');
    if (!dot) return;
    dot.hidden = false;
    dot.classList.toggle('ready', state === 'ready');
    if (state === 'ready') dot.title = `KnoxMap ${version} is downloaded - restart to use it`;
    else if (state) dot.title = `KnoxMap ${version} is out`;
    else dot.removeAttribute('title');
  }

  async function watchUpdates() {
    const box = $('#updateNote');
    if (!box) return;
    let st;
    try { st = await (await fetch('/api/update')).json(); } catch (_) { return; }
    versionDot(st.state === 'ready' ? 'ready'
               : (st.latest && st.latest !== st.current ? 'new' : ''), st.latest);
    if (st.state === 'ready') {
      box.hidden = false;
      box.innerHTML = '<span></span> <button type="button">Restart to update</button>';
      box.querySelector('span').textContent = `KnoxMap ${st.latest} is ready.`;
      box.querySelector('button').addEventListener('click', async (e) => {
        e.target.disabled = true;
        e.target.textContent = 'Restarting...';
        try {
          const res = await fetch('/api/update/restart', { method: 'POST' });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        } catch (err) {
          e.target.disabled = false;
          e.target.textContent = 'Restart to update';
          toast('bad', 'Could not restart', err.message, 8000);
        }
      });
      return;
    }
    if (st.state === 'downloading') {
      box.hidden = false;
      box.textContent = `Downloading KnoxMap ${st.latest}...`;
    }
    setTimeout(watchUpdates, st.state === 'downloading' ? 5000 : 10 * 60 * 1000);
  }

  // ---- version menu ---------------------------------------------------------------
  // Click the version at the top for every release on GitHub. The one chosen
  // downloads now (updater.py checks it against GitHub's fingerprint) and goes
  // in on restart - older ones too, for when a new release breaks something.

  const VERSION_MENU_SINCE = [1, 3, 5];   // the first release with this menu

  const versionParts = v => (String(v).match(/\d+/g) || []).map(Number);
  function versionBefore(a, b) {
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      if ((a[i] || 0) !== (b[i] || 0)) return (a[i] || 0) < (b[i] || 0);
    }
    return false;
  }

  function versionNote(text, cls) {
    const note = $('#versionNote');
    note.className = 'hint' + (cls ? ' ' + cls : '');
    note.textContent = text;
  }

  async function waitForVersion(version) {
    let st;
    for (;;) {
      await new Promise(r => setTimeout(r, 1500));
      try { st = await (await fetch('/api/update')).json(); } catch (_) { continue; }
      if (st.state === 'downloading') continue;
      break;
    }
    if (st.state !== 'ready' || st.latest !== version) {
      throw new Error(st.error || `KnoxMap ${version} could not be downloaded.`);
    }
    const note = $('#versionNote');
    note.className = 'hint';
    note.innerHTML = '<span></span> <button type="button">Restart now</button>';
    note.querySelector('span').textContent = `KnoxMap ${version} is ready.`;
    note.querySelector('button').addEventListener('click', async (e) => {
      e.target.disabled = true;
      e.target.textContent = 'Restarting...';
      try {
        const res = await fetch('/api/update/restart', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      } catch (err) {
        e.target.disabled = false;
        e.target.textContent = 'Restart now';
        versionNote(err.message, 'bad');
      }
    });
    watchUpdates();
  }

  async function chooseVersion(item, button, current) {
    const older = versionBefore(versionParts(item.version), versionParts(current));
    if (older && versionBefore(versionParts(item.version), VERSION_MENU_SINCE)) {
      const ok = window.confirm(
        `KnoxMap ${item.version} has no version menu. To come back, download the newest ` +
        'KnoxMap from GitHub, or set "auto_update" to true in knoxmap_config.json. ' +
        `Go back to ${item.version}?`);
      if (!ok) return;
    }
    for (const b of document.querySelectorAll('#versionList button')) b.disabled = true;
    button.textContent = 'Downloading...';
    versionNote(`Downloading KnoxMap ${item.version} from GitHub...`);
    try {
      const res = await fetch('/api/versions/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: item.version }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      await waitForVersion(item.version);
      button.textContent = 'Downloaded';
    } catch (err) {
      versionNote(err.message, 'bad');
      openVersions(true);
    }
  }

  async function openVersions(keepNote) {
    const list = $('#versionList');
    list.innerHTML = '<li class="hint">Asking GitHub…</li>';
    if (!keepNote) versionNote('');
    let data;
    try {
      const res = await fetch('/api/versions');
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      list.innerHTML = '';
      versionNote(err.message, 'bad');
      return;
    }
    list.innerHTML = '';
    const newest = data.releases.find(r => r.latest);
    if (data.managed && newest && !newest.current && !keepNote) {
      // The one thing most people opened this menu to do, before the list of
      // every release there has ever been.
      versionNote('');
      const note = $('#versionNote');
      note.className = 'hint update-ready';
      note.innerHTML = '<span></span> ';
      note.querySelector('span').textContent = `KnoxMap ${newest.version} is out.`;
      const go = document.createElement('button');
      go.type = 'button';
      go.textContent = 'Update';
      go.addEventListener('click', () => chooseVersion(newest, go, data.current));
      note.append(go);
    } else if (!data.managed) {
      versionNote('This copy of KnoxMap is a git checkout: switch versions with git.');
    } else if (!data.auto_update && !keepNote) {
      versionNote('Automatic updates are off while you are on an older version. ' +
                  'Choose the newest to turn them back on.');
    }
    for (const item of data.releases) {
      const li = document.createElement('li');
      const row = document.createElement('div');
      row.className = 'version-row';
      const v = document.createElement('span');
      v.className = 'v';
      v.textContent = `v${item.version}`;
      const date = document.createElement('span');
      date.className = 'date';
      date.textContent = item.date;
      row.append(v, date);
      if (item.current) {
        const tag = document.createElement('span');
        tag.className = 'tag now';
        tag.textContent = 'this one';
        row.append(tag);
      } else if (item.latest) {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = 'newest';
        row.append(tag);
      }
      if (!item.current) {
        const older = versionBefore(versionParts(item.version), versionParts(data.current));
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = older ? 'Go back' : 'Update';
        if (older) button.classList.add('older');
        button.disabled = !data.managed;
        button.addEventListener('click', () => chooseVersion(item, button, data.current));
        row.append(button);
      }
      li.append(row);
      if (item.notes) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = "What's in it";
        const pre = document.createElement('pre');
        pre.textContent = item.notes;
        details.append(summary, pre);
        li.append(details);
      }
      list.append(li);
    }
  }

  function versionMenu() {
    const badge = $('#appVersion');
    const menu = $('#versionMenu');
    if (!badge || !menu) return;
    const close = () => { menu.hidden = true; badge.setAttribute('aria-expanded', 'false'); };
    badge.addEventListener('click', () => {
      if (!menu.hidden) { close(); return; }
      menu.hidden = false;
      badge.setAttribute('aria-expanded', 'true');
      openVersions();
    });
    menu.querySelector('.version-menu-close').addEventListener('click', close);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    document.addEventListener('click', e => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== badge) close();
    });
  }

  function creditsMenu() {
    const badge = $('#creditsBtn');
    const menu = $('#creditsMenu');
    if (!badge || !menu) return;
    const close = () => { menu.hidden = true; badge.setAttribute('aria-expanded', 'false'); };
    badge.addEventListener('click', () => {
      if (!menu.hidden) { close(); return; }
      menu.hidden = false;
      badge.setAttribute('aria-expanded', 'true');
    });
    menu.querySelector('.version-menu-close').addEventListener('click', close);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    document.addEventListener('click', e => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== badge) close();
    });
  }

  function closeSettingsPopouts() {
    const credits = $('#creditsMenu');
    const creditsBtn = $('#creditsBtn');
    if (credits) credits.hidden = true;
    if (creditsBtn) creditsBtn.setAttribute('aria-expanded', 'false');
    const langList = document.querySelector('#language .language-list');
    const langBtn = document.querySelector('#language .language-btn');
    if (langList) langList.hidden = true;
    if (langBtn) langBtn.setAttribute('aria-expanded', 'false');
  }

  function settingsMenu() {
    const wrap = $('#settingsWrap');
    if (!wrap) return;
    wrap.addEventListener('mouseleave', closeSettingsPopouts);
    wrap.addEventListener('focusout', e => {
      if (!wrap.contains(e.relatedTarget)) closeSettingsPopouts();
    });
    const clearBtn = $('#clearCacheBtn');
    if (!clearBtn) return;
    clearBtn.addEventListener('click', async () => {
      clearBtn.disabled = true;
      try {
        const res = await fetch('/api/cache/clear', { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || 'Could not clear the cache');
        toast('ok', 'Clear cache', 'Map tiles and downloaded map data were cleared.');
      } catch (err) {
        toast('bad', 'Clear cache', err.message || 'Could not clear the cache');
      } finally {
        clearBtn.disabled = false;
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    applyTheme(savedBase());
    document.querySelectorAll('#basemaps button').forEach(btn => {
      btn.addEventListener('click', () => chooseBase(btn.dataset.base));
    });
    versionMenu();
    creditsMenu();
    settingsMenu();
    setTimeout(watchUpdates, 8000);
    presetCards();
  });
  window.addEventListener('load', mapExtras);

  return { step, resetFrom, toast, problem, saveReport, countUp, tile, overlay, noted, card, progress,
           confetti() {}, wireSettings, paintRange };
})();
