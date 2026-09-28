// KnoxMap in another language, from a plain text file.
//
// lang/english.txt lists every line the window says, as "English = English".
// Copy it to lang/<language>.txt, translate the right-hand side, and the
// language is in the menu at the top - no code, no rebuild. Anything left in
// English stays English, so a half-translated file is fine.
//
// The page itself is written in English, so translating means swapping the
// text as it is shown: every text node and every placeholder, title and label
// now, and anything the app writes later - a note under a button, a toast, the
// settings it builds from the server - through a MutationObserver. Matching is
// on the whole string, which is what the translator sees in the file.

const i18n = (() => {
  let strings = {};            // English -> translated
  let watching = null;

  const clean = s => s.replace(/\s+/g, ' ').trim();

  function say(text) {
    if (!text) return null;
    const hit = strings[clean(text)];
    if (!hit) return null;
    // Keep the spacing around it: the page lays text out with it.
    const [, before, , after] = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
    return before + hit + after;
  }

  function translateNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const next = say(node.nodeValue);
      if (next !== null && next !== node.nodeValue) node.nodeValue = next;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.closest && node.closest('[data-no-translate]')) return;
    for (const attr of ['placeholder', 'title', 'aria-label']) {
      const value = node.getAttribute && node.getAttribute(attr);
      const next = value && say(value);
      if (next && next !== value) node.setAttribute(attr, next);
    }
    for (const child of node.childNodes) translateNode(child);
  }

  function translateAll() {
    translateNode(document.body);
  }

  function watch() {
    if (watching) return;
    watching = new MutationObserver(records => {
      watching.disconnect();          // our own changes must not re-trigger it
      for (const record of records) {
        if (record.type === 'characterData') translateNode(record.target);
        for (const node of record.addedNodes) translateNode(node);
        if (record.type === 'attributes' && record.target) translateNode(record.target);
      }
      observe();
    });
    observe();
  }

  function observe() {
    watching.observe(document.body, {
      subtree: true, childList: true, characterData: true,
      attributeFilter: ['placeholder', 'title', 'aria-label'],
    });
  }

  async function use(file, remember) {
    try {
      const res = await fetch(`/api/language/${encodeURIComponent(file)}`,
                              { method: remember ? 'POST' : 'GET' });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      strings = data.strings || {};
    } catch (_) {
      strings = {};                   // no file, no harm: English it is
    }
    if (Object.keys(strings).length) {
      translateAll();
      watch();
      document.dispatchEvent(new Event('knoxmap-lang'));
    } else if (remember) {
      window.location.reload();       // back to English: the simplest way back
    }
  }

  // File name in lang/ -> a small flag. Anything else gets its initial.
  const FLAG_CODE = {
    english: 'gb', espanol: 'es', spanish: 'es', castellano: 'es',
    turkce: 'tr', turkish: 'tr',
    deutsch: 'de', german: 'de',
    francais: 'fr', french: 'fr',
    portugues: 'pt', portuguese: 'pt',
    italiano: 'it', italian: 'it',
    russian: 'ru', polski: 'pl', polish: 'pl',
    nederlands: 'nl', dutch: 'nl',
    svenska: 'se', swedish: 'se',
    norsk: 'no', norwegian: 'no',
    dansk: 'dk', danish: 'dk',
    suomi: 'fi', finnish: 'fi',
    czech: 'cz', cestina: 'cz',
    magyar: 'hu', hungarian: 'hu',
    romana: 'ro', romanian: 'ro',
    ukrainian: 'ua',
    japanese: 'jp', chinese: 'cn', korean: 'kr',
    portuguesbr: 'br',
  };

  const FLAG_SVG = {
    gb: '<svg viewBox="0 0 60 30"><path fill="#012169" d="M0 0h60v30H0z"/><path stroke="#fff" stroke-width="6" d="M0 0l60 30m0-30L0 30"/><path stroke="#C8102E" stroke-width="4" d="M0 0l60 30m0-30L0 30"/><path stroke="#fff" stroke-width="10" d="M30 0v30M0 15h60"/><path stroke="#C8102E" stroke-width="6" d="M30 0v30M0 15h60"/></svg>',
    es: '<svg viewBox="0 0 18 12"><path fill="#c60b1e" d="M0 0h18v12H0z"/><path fill="#ffc400" d="M0 3h18v6H0z"/></svg>',
    tr: '<svg viewBox="0 0 18 12"><path fill="#e30a17" d="M0 0h18v12H0z"/><circle cx="7" cy="6" r="3" fill="#fff"/><circle cx="8.05" cy="6" r="2.4" fill="#e30a17"/><path fill="#fff" d="M10.6 4.55l.38 1.15 1.2.02-1 .73.37 1.15-.95-.72-.96.72.37-1.15-1-.73 1.2-.02z"/></svg>',
    de: '<svg viewBox="0 0 18 12"><path fill="#000" d="M0 0h18v4H0z"/><path fill="#dd0000" d="M0 4h18v4H0z"/><path fill="#ffce00" d="M0 8h18v4H0z"/></svg>',
    fr: '<svg viewBox="0 0 18 12"><path fill="#002395" d="M0 0h6v12H0z"/><path fill="#fff" d="M6 0h6v12H6z"/><path fill="#ed2939" d="M12 0h6v12H12z"/></svg>',
    it: '<svg viewBox="0 0 18 12"><path fill="#009246" d="M0 0h6v12H0z"/><path fill="#fff" d="M6 0h6v12H6z"/><path fill="#ce2b37" d="M12 0h6v12H12z"/></svg>',
    pt: '<svg viewBox="0 0 18 12"><path fill="#006600" d="M0 0h7v12H0z"/><path fill="#ff0000" d="M7 0h11v12H7z"/><circle cx="7" cy="6" r="2.2" fill="#ffcc00"/></svg>',
    ru: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v4H0z"/><path fill="#0039a6" d="M0 4h18v4H0z"/><path fill="#d52b1e" d="M0 8h18v4H0z"/></svg>',
    pl: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v6H0z"/><path fill="#dc143c" d="M0 6h18v6H0z"/></svg>',
    nl: '<svg viewBox="0 0 18 12"><path fill="#ae1c28" d="M0 0h18v4H0z"/><path fill="#fff" d="M0 4h18v4H0z"/><path fill="#21468b" d="M0 8h18v4H0z"/></svg>',
    se: '<svg viewBox="0 0 18 12"><path fill="#006aa7" d="M0 0h18v12H0z"/><path fill="#fecc00" d="M5 0h3v12H5zM0 4.5h18v3H0z"/></svg>',
    no: '<svg viewBox="0 0 18 12"><path fill="#ba0c2f" d="M0 0h18v12H0z"/><path fill="#fff" d="M5 0h4v12H5zM0 4h18v4H0z"/><path fill="#00205b" d="M6 0h2v12H6zM0 5h18v2H0z"/></svg>',
    dk: '<svg viewBox="0 0 18 12"><path fill="#c60c30" d="M0 0h18v12H0z"/><path fill="#fff" d="M5 0h3v12H5zM0 4.5h18v3H0z"/></svg>',
    fi: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v12H0z"/><path fill="#003580" d="M5 0h3v12H5zM0 4.5h18v3H0z"/></svg>',
    cz: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v6H0z"/><path fill="#d7141a" d="M0 6h18v6H0z"/><path fill="#11457e" d="M0 0l8 6L0 12z"/></svg>',
    hu: '<svg viewBox="0 0 18 12"><path fill="#ce2939" d="M0 0h18v4H0z"/><path fill="#fff" d="M0 4h18v4H0z"/><path fill="#477050" d="M0 8h18v4H0z"/></svg>',
    ro: '<svg viewBox="0 0 18 12"><path fill="#002b7f" d="M0 0h6v12H0z"/><path fill="#fcd116" d="M6 0h6v12H6z"/><path fill="#ce1126" d="M12 0h6v12H12z"/></svg>',
    ua: '<svg viewBox="0 0 18 12"><path fill="#005bbb" d="M0 0h18v6H0z"/><path fill="#ffd500" d="M0 6h18v6H0z"/></svg>',
    jp: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v12H0z"/><circle cx="9" cy="6" r="3.2" fill="#bc002d"/></svg>',
    cn: '<svg viewBox="0 0 18 12"><path fill="#de2910" d="M0 0h18v12H0z"/><path fill="#ffde00" d="M3.2 2.2l.45 1.35 1.4.02-1.12.84.42 1.32-1.15-.82-1.15.82.42-1.32-1.12-.84 1.4-.02z"/></svg>',
    kr: '<svg viewBox="0 0 18 12"><path fill="#fff" d="M0 0h18v12H0z"/><circle cx="9" cy="6" r="2.6" fill="#cd2e3a"/><path fill="#0047a0" d="M9 3.4a2.6 2.6 0 0 0 0 5.2 1.3 1.3 0 0 1 0-2.6 1.3 1.3 0 0 0 0-2.6z"/></svg>',
    br: '<svg viewBox="0 0 18 12"><path fill="#009c3b" d="M0 0h18v12H0z"/><path fill="#ffdf00" d="M9 1.4l7 4.6-7 4.6L2 6z"/><circle cx="9" cy="6" r="2.1" fill="#002776"/></svg>',
  };

  function flagSvg(file) {
    const drawn = FLAG_SVG[FLAG_CODE[(file || '').toLowerCase()]];
    if (drawn) return drawn;
    const letter = (file || '?').replace(/[^0-9A-Za-z]/g, '').slice(0, 1).toUpperCase() || '?';
    return '<svg viewBox="0 0 18 12"><path fill="#252922" d="M0 0h18v12H0z"/><text x="9" y="9" text-anchor="middle" font-size="8" font-family="system-ui,sans-serif" fill="#a5e266">' + letter + '</text></svg>';
  }

  function flagEl(file) {
    const el = document.createElement('span');
    el.className = 'language-flag';
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = flagSvg(file);
    return el;
  }

  async function start() {
    const menu = document.getElementById('language');
    if (!menu) return;
    let data;
    try {
      data = await (await fetch('/api/languages')).json();
    } catch (_) { return; }
    const languages = data.languages || [];
    // With nothing to choose from, the menu only gets in the way; the file
    // that explains how to add one is named in its tooltip.
    if (languages.length < 2) {
      menu.hidden = true;
      return;
    }
    const current = data.current && languages.some(l => l.file === data.current)
      ? data.current : languages[0].file;
    const byFile = Object.fromEntries(languages.map(l => [l.file, l.name]));

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'language-btn';
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    const currentName = document.createElement('span');
    currentName.className = 'language-name';

    const list = document.createElement('ul');
    list.className = 'language-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;

    function showCurrent(file) {
      button.replaceChildren(flagEl(file), currentName);
      currentName.textContent = byFile[file] || file;
      button.dataset.file = file;
      for (const item of list.querySelectorAll('button')) {
        item.setAttribute('aria-selected', item.dataset.file === file ? 'true' : 'false');
      }
    }

    for (const language of languages) {
      const li = document.createElement('li');
      const option = document.createElement('button');
      option.type = 'button';
      option.setAttribute('role', 'option');
      option.dataset.file = language.file;
      const name = document.createElement('span');
      name.className = 'language-name';
      name.textContent = language.name;
      option.append(flagEl(language.file), name);
      option.addEventListener('click', () => {
        list.hidden = true;
        button.setAttribute('aria-expanded', 'false');
        if (option.dataset.file === button.dataset.file) return;
        showCurrent(option.dataset.file);
        use(option.dataset.file, true);
      });
      li.append(option);
      list.append(li);
    }

    const close = () => {
      list.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    };
    button.addEventListener('click', () => {
      const open = list.hidden;
      list.hidden = !open;
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    document.addEventListener('click', e => {
      if (!list.hidden && !menu.contains(e.target)) close();
    });

    menu.replaceChildren(button, list);
    showCurrent(current);
    menu.hidden = false;
    if (current && current !== 'english') use(current, false);
  }

  document.addEventListener('DOMContentLoaded', start);
  return { use, translateAll, say };
})();
