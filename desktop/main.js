// The KnoxMap window.
//
// Two ways to start:
//   * Developing: python KnoxMap/knoxmap.py serves the page and starts this process,
//     passing KNOXMAP_URL. This process only owns the window.
//   * The program file: this process is what the player launched. It starts
//     the Python server packed in resources/python and loads the page that
//     server prints as KNOXMAP_READY.
//
// Nothing here talks to Node from the page. The window loads
// http://127.0.0.1 and the page keeps using fetch.
//
// Exit code 2 means another KnoxMap window is already open.

const { app, BrowserWindow, Menu, dialog, shell, session, screen } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

let pageUrl = process.env.KNOXMAP_URL || '';
let pageToken = process.env.KNOXMAP_TOKEN || '';
const TITLE = process.env.KNOXMAP_TITLE || 'KnoxMap';

const ALREADY_OPEN = 2;

let home = '';
let server = null;
let serverReady = false;
let swapping = false;

function packagedHome() {
  if (process.env.PORTABLE_EXECUTABLE_DIR) return process.env.PORTABLE_EXECUTABLE_DIR;
  if (process.env.APPIMAGE) return path.dirname(process.env.APPIMAGE);
  if (process.platform === 'darwin') {
    const bundle = path.resolve(process.execPath, '..', '..', '..');
    const parent = path.dirname(bundle);
    if (path.basename(parent) === 'Applications') {
      const user = process.env.HOME || process.env.USERPROFILE || '';
      return path.join(user, 'KnoxMap');
    }
    return parent;
  }
  return path.dirname(process.env.PORTABLE_EXECUTABLE_FILE || process.execPath);
}

function programFile() {
  if (process.env.PORTABLE_EXECUTABLE_FILE) return process.env.PORTABLE_EXECUTABLE_FILE;
  if (process.env.APPIMAGE) return process.env.APPIMAGE;
  if (process.platform === 'darwin' && app.isPackaged) {
    return path.resolve(process.execPath, '..', '..', '..');
  }
  return process.execPath;
}

function bundledPython() {
  const name = process.platform === 'win32' ? 'knoxmap-server.exe' : 'knoxmap-server';
  return path.join(process.resourcesPath, 'python', name);
}

let DATA_DIR = process.env.KNOXMAP_DATA || '';
if (app.isPackaged) {
  home = packagedHome();
  DATA_DIR = path.join(home, 'cache', 'electron');
  try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch (_) { /* shown when the server starts */ }
  app.setPath('userData', DATA_DIR);
} else if (DATA_DIR) {
  try { fs.mkdirSync(DATA_DIR, { recursive: true }); } catch (_) { /* app.setPath still points here */ }
  app.setPath('userData', DATA_DIR);
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) app.exit(ALREADY_OPEN);

let mainWindow = null;
let profilerWindow = null;
let quitting = false;
let userClosed = false;

function logFile() {
  const data = DATA_DIR || app.getPath('userData');
  return path.join(data, '..', '..', 'logs', 'knoxmap.log');
}

function windowIcon() {
  const packed = path.join(__dirname, 'icon.png');
  if (fs.existsSync(packed)) return packed;
  const branding = path.join(__dirname, '..', 'docs', 'branding', 'knoxmap.png');
  if (fs.existsSync(branding)) return branding;
  return undefined;
}

function sameOrigin(target) {
  try {
    return new URL(target).origin === new URL(pageUrl).origin;
  } catch (_) {
    return false;
  }
}

function webPreferences() {
  return {
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    nodeIntegrationInSubFrames: false,
    backgroundThrottling: false,
  };
}

function loadBounds() {
  try {
    const raw = JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'window.json'), 'utf8'));
    const area = screen.getDisplayMatching(raw).workArea;
    const width = Math.min(Math.max(raw.width || 1440, 1000), area.width);
    const height = Math.min(Math.max(raw.height || 920, 680), area.height);
    const x = Math.min(Math.max(raw.x, area.x), area.x + area.width - 200);
    const y = Math.min(Math.max(raw.y, area.y), area.y + area.height - 200);
    return { x, y, width, height, maximized: !!raw.maximized };
  } catch (_) {
    return null;
  }
}

function saveBounds(win) {
  if (!win || win.isDestroyed()) return;
  try {
    const bounds = win.getNormalBounds();
    const payload = { ...bounds, maximized: win.isMaximized() };
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    fs.writeFileSync(path.join(app.getPath('userData'), 'window.json'), JSON.stringify(payload));
  } catch (_) { /* a window that cannot remember its size still closes */ }
}

function attachGuards(contents) {
  contents.setWindowOpenHandler(({ url }) => {
    if (sameOrigin(url)) {
      return {
        action: 'allow',
        overrideBrowserWindowOptions: {
          parent: mainWindow || undefined,
          backgroundColor: '#07090B',
          autoHideMenuBar: true,
          icon: windowIcon(),
          webPreferences: webPreferences(),
        },
      };
    }
    shell.openExternal(url).catch(() => {});
    return { action: 'deny' };
  });
  contents.on('will-navigate', (event, url) => {
    if (url.startsWith('data:')) return;
    if (!sameOrigin(url)) event.preventDefault();
  });
}

const SPLASH_BG = '#151714';
let splashWindow = null;
let suppressQuit = false;

function splashMarkup() {
  return `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>KnoxMap</title>
<style>
  html, body { margin: 0; height: 100%; background: ${SPLASH_BG}; color: #e6e5de;
    font: 600 15px/1.4 system-ui, "Segoe UI", sans-serif; }
  body { display: flex; align-items: center; justify-content: center; }
  .card { display: flex; flex-direction: column; align-items: center; gap: 18px; }
  svg { width: 72px; height: 72px; display: block; }
  .spin {
    width: 28px; height: 28px; box-sizing: border-box; border-radius: 50%;
    border: 3px solid #30352c; border-top-color: #a5e266;
    animation: turn .75s linear infinite;
  }
  p { margin: 0; }
  @keyframes turn { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
</head>
<body>
  <div class="card">
    <svg viewBox="0 0 512 512" aria-hidden="true">
      <rect width="512" height="512" rx="96" fill="#14180f"/>
      <path d="M256 300 L420 382 L256 464 L92 382 Z" fill="none" stroke="#a5e266" stroke-width="22" stroke-linejoin="round"/>
      <path d="M256 382 C256 382 150 262 150 188 A106 106 0 0 1 362 188 C362 262 256 382 256 382 Z" fill="#a5e266"/>
      <circle cx="256" cy="190" r="40" fill="#14180f"/>
    </svg>
    <div class="spin" aria-hidden="true"></div>
    <p>Launching KnoxMap</p>
  </div>
</body>
</html>`;
}

function showSplash() {
  if (splashWindow && !splashWindow.isDestroyed()) return splashWindow;
  const splash = new BrowserWindow({
    width: 360,
    height: 300,
    frame: false,
    resizable: false,
    maximizable: false,
    minimizable: false,
    fullscreenable: false,
    center: true,
    show: true,
    alwaysOnTop: true,
    backgroundColor: SPLASH_BG,
    title: 'KnoxMap',
    autoHideMenuBar: true,
    icon: windowIcon(),
    webPreferences: webPreferences(),
  });
  splashWindow = splash;
  splash.show();
  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(splashMarkup()));
  splash.on('closed', () => {
    const closedByUser = splashWindow === splash;
    if (closedByUser) splashWindow = null;
    if (closedByUser && !suppressQuit && !quitting) app.quit();
  });
  return splash;
}

function closeSplash() {
  const splash = splashWindow;
  splashWindow = null;
  if (splash && !splash.isDestroyed()) splash.destroy();
}

function createWindow() {
  const saved = loadBounds();
  const win = new BrowserWindow({
    width: saved ? saved.width : 1440,
    height: saved ? saved.height : 920,
    x: saved ? saved.x : undefined,
    y: saved ? saved.y : undefined,
    minWidth: 1000,
    minHeight: 680,
    show: false,
    backgroundColor: '#07090B',
    title: TITLE,
    autoHideMenuBar: true,
    icon: windowIcon(),
    webPreferences: webPreferences(),
  });
  mainWindow = win;
  if (saved && saved.maximized) win.maximize();

  win.once('ready-to-show', () => {
    win.show();
    win.focus();
    closeSplash();
  });
  win.webContents.on('did-fail-load', (_event, code, desc, _url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    closeSplash();
    dialog.showErrorBox(
      'KnoxMap',
      `The window could not open the page (${desc}).\n\nDetails are in:\n${logFile()}`,
    );
  });

  let closing = false;
  win.on('close', (event) => {
    if (closing || quitting) return;
    event.preventDefault();
    const timeout = new Promise((resolve) => setTimeout(() => resolve(true), 8000));
    const ask = win.webContents.executeJavaScript(
      'window.knoxmapBeforeClose ? window.knoxmapBeforeClose() : true',
      true,
    ).catch(() => true);
    Promise.race([ask, timeout]).then((allow) => {
      if (!allow || win.isDestroyed()) return;
      closing = true;
      userClosed = true;
      saveBounds(win);
      win.close();
    }).catch(() => {
      closing = true;
      userClosed = true;
      win.close();
    });
  });

  win.on('closed', () => {
    userClosed = true;
    if (mainWindow === win) mainWindow = null;
    closeProfiler();
  });

  win.loadURL(pageUrl);
}

function installMenus() {
  if (process.platform === 'darwin') {
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { role: 'appMenu' },
      { role: 'editMenu' },
    ]));
    return;
  }
  Menu.setApplicationMenu(null);
}

function installDownloads() {
  session.defaultSession.on('will-download', (_event, item, webContents) => {
    item.pause();
    const parent = BrowserWindow.fromWebContents(webContents) || mainWindow;
    const owner = parent && !parent.isDestroyed() ? parent : undefined;
    dialog.showSaveDialog(owner, {
      defaultPath: path.join(app.getPath('downloads'), item.getFilename()),
    }).then((result) => {
      if (result.canceled || !result.filePath) {
        item.cancel();
        return;
      }
      item.setSavePath(result.filePath);
      item.resume();
      item.once('done', (_done, state) => {
        if (state === 'completed') shell.showItemInFolder(result.filePath);
      });
    }).catch(() => item.cancel());
  });
}

function installToken() {
  if (!pageToken || !pageUrl) return;
  const origin = new URL(pageUrl).origin;
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: [`${origin}/*`] },
    (details, callback) => {
      details.requestHeaders['X-KnoxMap-Token'] = pageToken;
      callback({ requestHeaders: details.requestHeaders });
    },
  );
}

function parentPid() {
  const pid = Number(process.env.KNOXMAP_PID || 0);
  return Number.isInteger(pid) && pid > 0 ? pid : 0;
}

function parentAlive() {
  const pid = parentPid();
  if (!pid) return true;
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return !!(err && err.code === 'EPERM');
  }
}

let parentReported = false;

function parentDied() {
  if (quitting || userClosed || parentReported) {
    if (!parentReported) app.quit();
    return;
  }
  if (parentAlive()) return;
  parentReported = true;
  suppressQuit = true;
  closeSplash();
  dialog.showErrorBox(
    'KnoxMap',
    `KnoxMap stopped unexpectedly.\n\nDetails are in:\n${logFile()}`,
  );
  app.exit(1);
}

function closeProfiler() {
  const win = profilerWindow;
  if (win && !win.isDestroyed()) win.close();
}

function createProfilerWindow() {
  // Only a launch from debug_run.bat sets this. The page is the same server.
  if (process.env.KNOXMAP_DEBUG !== '1' || !pageUrl) return;
  if (profilerWindow && !profilerWindow.isDestroyed()) return;
  let url;
  try {
    url = new URL('/debug', pageUrl).href;
  } catch (_) {
    return;
  }
  const area = screen.getPrimaryDisplay().workArea;
  const width = Math.min(1080, Math.max(760, area.width - 80));
  const height = Math.min(780, Math.max(480, area.height - 80));
  const win = new BrowserWindow({
    width,
    height,
    x: area.x + Math.max(0, area.width - width - 24),
    y: area.y + 24,
    minWidth: 760,
    minHeight: 480,
    show: false,
    backgroundColor: '#151714',
    title: 'KnoxMap Profiler',
    autoHideMenuBar: true,
    icon: windowIcon(),
    webPreferences: webPreferences(),
  });
  profilerWindow = win;
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    if (profilerWindow === win) profilerWindow = null;
  });
  win.webContents.on('did-fail-load', (_event, code, desc, _url, isMainFrame) => {
    if (!isMainFrame || code === -3) return;
    dialog.showErrorBox('KnoxMap Profiler', `The profiler could not open (${desc}).`);
  });
  win.loadURL(url);
}

function quitNow() {
  quitting = true;
  userClosed = true;
  if (mainWindow && !mainWindow.isDestroyed()) {
    saveBounds(mainWindow);
    mainWindow.close();
  } else {
    closeProfiler();
    app.quit();
  }
}

function shQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function launchSwapper(src, dest) {
  const staged = path.join(home, 'update', 'staged.json');
  fs.mkdirSync(path.join(home, 'update'), { recursive: true });
  if (!src || !fs.existsSync(src)) {
    relaunchSelf();
    return;
  }
  if (process.platform === 'win32') {
    const bat = path.join(home, 'update', 'swap.bat');
    const lines = [
      '@echo off',
      'set /a n=0',
      ':again',
      'set /a n+=1',
      'if %n% gtr 30 exit /b 1',
      'timeout /t 1 /nobreak >nul',
      `move /y "${src}" "${dest}"`,
      'if errorlevel 1 goto again',
      `del /f /q "${staged}"`,
      `start "" "${dest}"`,
      'del /f /q "%~f0"',
      '',
    ];
    fs.writeFileSync(bat, lines.join('\r\n'));
    spawn('cmd.exe', ['/d', '/c', bat], { detached: true, stdio: 'ignore', windowsHide: true }).unref();
    return;
  }
  const script = path.join(home, 'update', 'swap.sh');
  const qsrc = shQuote(src);
  const qdest = shQuote(dest);
  const qstaged = shQuote(staged);
  const body = process.platform === 'darwin'
    ? `#!/bin/sh
n=0
while [ "$n" -lt 30 ]; do
  n=$((n + 1))
  sleep 1
  mnt=$(mktemp -d /tmp/knoxmap-dmg.XXXXXX) || continue
  if hdiutil attach ${qsrc} -nobrowse -readonly -mountpoint "$mnt"; then
    if ditto "$mnt/KnoxMap.app" ${qdest}; then
      hdiutil detach "$mnt" || true
      rm -rf "$mnt"
      rm -f ${qstaged}
      open ${qdest}
      rm -f "$0"
      exit 0
    fi
    hdiutil detach "$mnt" || true
  fi
  rm -rf "$mnt"
done
exit 1
`
    : `#!/bin/sh
n=0
while [ "$n" -lt 30 ]; do
  n=$((n + 1))
  sleep 1
  if mv -f ${qsrc} ${qdest}; then
    chmod +x ${qdest}
    rm -f ${qstaged}
    exec ${qdest}
  fi
done
exit 1
`;
  fs.writeFileSync(script, body, { mode: 0o755 });
  spawn('/bin/sh', [script], { detached: true, stdio: 'ignore' }).unref();
}

function relaunchSelf() {
  if (process.platform === 'darwin' && app.isPackaged) {
    spawn('open', [programFile()], { detached: true, stdio: 'ignore' }).unref();
    return;
  }
  const file = process.env.PORTABLE_EXECUTABLE_FILE || process.env.APPIMAGE || process.execPath;
  spawn(file, [], { detached: true, stdio: 'ignore', windowsHide: true }).unref();
}

function pendingSwap() {
  if (!home) return '';
  try {
    const staged = JSON.parse(fs.readFileSync(path.join(home, 'update', 'staged.json'), 'utf8'));
    if (staged && staged.exe && staged.zip && fs.existsSync(staged.zip)) return staged.zip;
  } catch (_) { /* no update waiting */ }
  return '';
}

function stopServer() {
  if (!server || server.exitCode !== null) return;
  try { server.stdin.write('quit\n'); } catch (_) { /* already gone */ }
  if (DATA_DIR) {
    try { fs.writeFileSync(path.join(DATA_DIR, 'quit'), 'quit'); } catch (_) { /* the pipe is the other way */ }
  }
}

function onServerLine(line) {
  const ready = line.match(/^KNOXMAP_READY (\d+)$/);
  if (ready && !serverReady) {
    serverReady = true;
    pageUrl = `http://127.0.0.1:${ready[1]}/`;
    return;
  }
  if (line.startsWith('KNOXMAP_RESTART ')) {
    swapping = true;
    const src = line.slice('KNOXMAP_RESTART '.length).trim();
    if (src) launchSwapper(src, programFile());
    else relaunchSelf();
    quitNow();
  }
}

function startServer() {
  const exe = bundledPython();
  if (!fs.existsSync(exe)) {
    return Promise.reject(new Error(`KnoxMap's program is missing:\n${exe}`));
  }
  pageToken = crypto.randomBytes(24).toString('base64url');
  const env = {
    ...process.env,
    KNOXMAP_SERVE: '1',
    KNOXMAP_HOME: home,
    KNOXMAP_LOG_DIR: path.join(home, 'logs'),
    KNOXMAP_DATA: DATA_DIR,
    KNOXMAP_TOKEN: pageToken,
    KNOXMAP_SHELL: 'electron',
    KNOXMAP_WINDOW: '1',
    KNOXMAP_EXE: programFile(),
    PYTHONUNBUFFERED: '1',
  };
  delete env.ELECTRON_RUN_AS_NODE;
  const child = spawn(exe, [], {
    env,
    cwd: home,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
  server = child;
  let out = '';
  let err = '';
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`KnoxMap did not start.\n\n${err.slice(-2000)}`));
    }, 30000);
    const finish = () => {
      if (!serverReady || settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    child.stdout.on('data', (buf) => {
      out += buf.toString('utf8');
      let nl = out.indexOf('\n');
      while (nl >= 0) {
        onServerLine(out.slice(0, nl).trim());
        out = out.slice(nl + 1);
        nl = out.indexOf('\n');
        finish();
      }
    });
    child.stderr.on('data', (buf) => {
      err += buf.toString('utf8');
      if (err.length > 8000) err = err.slice(-8000);
    });
    child.on('exit', (code) => {
      if (swapping || quitting || userClosed) return;
      clearTimeout(timer);
      const message = `KnoxMap stopped (exit ${code}).\n\n${err.slice(-2000)}\n\nDetails are in:\n${logFile()}`;
      if (!serverReady) {
        if (settled) return;
        settled = true;
        reject(new Error(message));
        return;
      }
      dialog.showErrorBox('KnoxMap', message);
      app.quit();
    });
    child.on('error', (failure) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(failure);
    });
  });
}

function watchParent() {
  if (app.isPackaged) return;
  if (!process.stdin.isTTY) {
    let buf = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => {
      buf += chunk;
      let nl = buf.indexOf('\n');
      while (nl >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line === 'quit') quitNow();
        nl = buf.indexOf('\n');
      }
    });
    process.stdin.on('end', parentDied);
    process.stdin.on('error', parentDied);
  }
  if (DATA_DIR) {
    const flag = path.join(DATA_DIR, 'quit');
    const timer = setInterval(() => {
      if (parentPid() && !parentAlive()) {
        clearInterval(timer);
        parentDied();
        return;
      }
      if (!fs.existsSync(flag)) return;
      clearInterval(timer);
      try { fs.unlinkSync(flag); } catch (_) { /* gone already */ }
      quitNow();
    }, 500);
  }
}

if (gotLock) {
  app.on('second-instance', () => {
    const win = mainWindow || splashWindow;
    if (!win || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });

  app.on('web-contents-created', (_event, contents) => attachGuards(contents));

  app.on('before-quit', () => {
    quitting = true;
    stopServer();
  });

  app.whenReady().then(async () => {
    if (app.isPackaged && !process.env.KNOXMAP_URL) {
      const waiting = pendingSwap();
      if (waiting) {
        launchSwapper(waiting, programFile());
        app.exit(0);
        return;
      }
      showSplash();
      try {
        await startServer();
      } catch (err) {
        suppressQuit = true;
        closeSplash();
        dialog.showErrorBox('KnoxMap', String(err && err.message || err));
        app.exit(1);
        return;
      }
    } else if (!pageUrl) {
      dialog.showErrorBox(
        'KnoxMap',
        'This window is started by KnoxMap itself.\n\nRun KnoxMap.exe. From a checkout, run .venv\\Scripts\\pythonw.exe knoxmap.py in the KnoxMap folder, or ./knoxmap.sh.',
      );
      app.exit(1);
      return;
    } else {
      showSplash();
    }
    installMenus();
    installToken();
    installDownloads();
    session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) => {
      // Copy details uses the clipboard. Everything else stays refused.
      callback(permission === 'clipboard-sanitized-write' || permission === 'clipboard-write');
    });
    watchParent();
    createWindow();
    try {
      createProfilerWindow();
    } catch (err) {
      dialog.showErrorBox('KnoxMap Profiler', String(err && err.message || err));
    }
    if (process.env.KNOXMAP_DEVTOOLS === '1' && mainWindow) {
      mainWindow.webContents.openDevTools({ mode: 'detach' });
    }
  }).catch((err) => {
    suppressQuit = true;
    closeSplash();
    dialog.showErrorBox('KnoxMap', String(err && err.message || err));
    app.exit(1);
  });

  app.on('window-all-closed', () => {
    if (!suppressQuit) app.quit();
  });
}
