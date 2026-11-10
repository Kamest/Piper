'use strict';
const { app, BrowserWindow, Menu, Tray, nativeImage, nativeTheme, ipcMain, shell, Notification, clipboard, screen, session, dialog } = require('electron');
const fs = require('node:fs/promises');
const fsSync = require('node:fs');
const { randomUUID } = require('node:crypto');
const path = require('node:path');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { spawn, execFile } = require('node:child_process');
const { runner } = require('./lib/process.cjs');
const { service, locateCLI } = require('./lib/service.cjs');
const { setupController } = require('./lib/setup.cjs');
const { navigation, trayModel, redact } = require('./lib/policy.cjs');
const { desktopUpdates } = require('./lib/updates.cjs');
const { linuxAutostart, integrateAppImage, LINUX_ENTRY, wsl } = require('./lib/platform.cjs');
const signed = require('../package.json').desktopSigned === true;
const themes = require('./ui/themes.json');
const pythonVersion = require('./runtime.json').python;
const { badge } = require('./lib/badge.cjs');
const { notifyPlan, ownedByMain, osascriptArgs } = require('./lib/notify.cjs');
const { machine } = require('./lib/machine.cjs');
const { windowURL, freeSlot, placement, restorable } = require('./lib/windows.cjs');
const theme = () => themes[nativeTheme.shouldUseDarkColors ? 'dark' : 'light'].tokens;

const env = { ...process.env };
const platform = env.PIPER_DESKTOP_PLATFORM || process.platform;
const windowsStore = process.windowsStore === true;
const port = Number(env.PIPER_PORT || 17895);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid Piper port');
if (env.PIPER_DESKTOP_HOME) {
  if (port === 17895 || !env.PIPER_HOME || !env.PIPER_DESKTOP_CLI || !env.PIPER_DESKTOP_INSTALL_SH) throw new Error('Test mode requires an isolated port, Piper home, CLI and installer');
  app.setPath('userData', path.resolve(env.PIPER_DESKTOP_HOME));
  app.setPath('logs', path.resolve(env.PIPER_DESKTOP_HOME, 'logs'));
}
if (env.PIPER_DESKTOP_NO_CHROMIUM_SANDBOX === '1') {
  if (!env.PIPER_DESKTOP_HOME) throw new Error('The Chromium sandbox switch is test-only');
  app.commandLine.appendSwitch('no-sandbox');
}
if (!app.requestSingleInstanceLock()) { app.quit(); }
else {
  let main, local, about, tray, trayImage, controller, api, quitting = false, connected = false, pollTimer, offlineTimer, tokenURL = '';
  let model = trayModel(null, false), login = false, bounds = {}, localLayout = '', mainSlot = 1;
  const notifications = new Map();
  // notifyPlan's memo; posted keeps native notifications referenced until closed; nativeFailed latches the fallback
  let notifyMemo = null, nativeFailed = false;
  const posted = new Set(), fallbacks = [], testNotes = [], told = [], externals = [];
  // 9.173: extra Piper windows by slot (2..99; the main window is 1), restored once after the first attach
  const extra = new Map();
  const restartViews = new Set();
  const failedRenderers = new Set();
  let restartHidden = false;
  let startingOutage = false;
  let ops, serviceVersion = '', machineBusy = '', holding = false, restored = false, savedWindows = [], appMenuKey = '', trayMenu = null, restartState = null;
  const origin = `http://127.0.0.1:${port}`;
  const userData = app.getPath('userData');
  const resources = app.isPackaged ? path.join(process.resourcesPath, 'desktop') : path.resolve(__dirname, '../resources');
  const pythonSource = app.isPackaged ? path.join(process.resourcesPath, 'python') : path.resolve(__dirname, `../vendor/${process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'win' : 'linux'}-${process.arch}/python`);
  const nativeHome = os.homedir();
  const linuxConfigHome = env.XDG_CONFIG_HOME || path.join(nativeHome, '.config');
  const linuxDataHome = env.XDG_DATA_HOME || path.join(nativeHome, '.local/share');
  const home = { native: nativeHome, linux: '' };
  // The service receives a stable Python path even after an AppImage unmounts.
  const runtimeDir = path.join(userData, 'runtime', 'python-' + pythonVersion);
  env.PATH = [path.join(runtimeDir, 'bin'), path.join(nativeHome, '.local/bin'), env.PATH || '', '/opt/homebrew/bin', '/usr/local/bin', '/opt/podman/bin', '/usr/bin', '/bin'].join(path.delimiter);
  const run = runner(spawn, env);
  const wslEnv = {};
  const exists = async p => { try { await fs.access(p); return true; } catch { return false; } };
  const locate = () => locateCLI({ env, home: nativeHome, installDir: controller.installDir(), exists, platform, run, distro: controller.distro() });
  const focus = win => { if (!win || win.isDestroyed()) return; if (win.isMinimized()) win.restore(); win.show(); win.focus(); app.focus({ steal: true }); };
  const show = () => focus(restartState && !restartHidden && (restartState.kind !== 'outage' || (local && !local.isDestroyed() && local.isVisible()))
    ? local : (connected || restartHidden || restartState?.kind === 'outage') && main && !main.isDestroyed() ? main : local);
  // tests record what would open (a browser, IntelliJ's idea://) instead of opening it
  const external = url => navigation(url, origin) === 'deny' ? Promise.resolve()
    : env.PIPER_DESKTOP_HOME ? (externals.push(url), Promise.resolve()) : shell.openExternal(url);
  const createUpdates = (overrides = {}) => desktopUpdates({ version: app.getVersion(), platform, arch: process.arch,
    packaged: app.isPackaged, windowsStore, signed, fetch: (...args) => fetch(...args),
    updater: () => require('electron-updater').autoUpdater, openExternal: external,
    changed: () => { if (tray) refreshTray(); sendAbout(); }, ...overrides });
  let desktopUpdate = createUpdates();
  const aboutState = () => ({ version: desktopUpdate.state().current,
    system: (platform === 'darwin' ? 'macOS' : platform === 'win32' ? 'Windows' : 'Linux') + ' ' + (process.getSystemVersion?.() || os.release()),
    architecture: platform === 'darwin' ? process.arch === 'arm64' ? 'Apple silicon (arm64)' : 'Intel (x64)' : process.arch,
    online: model.online, serviceVersion, serviceLocation: platform === 'win32' ? 'Running in WSL' + (controller?.distro() ? ' (' + controller.distro() + ')' : '')
      : platform === 'darwin' ? 'Running on this Mac' : 'Running on this computer', update: desktopUpdate.state() });
  function sendAbout() { if (about && !about.isDestroyed()) about.webContents.send('about:state', aboutState()); }
  function showAbout(check = false) {
    if (!about || about.isDestroyed()) {
      const parent = BrowserWindow.getFocusedWindow();
      about = new BrowserWindow({ width: 480, height: 522, useContentSize: true, resizable: false, maximizable: false,
        show: false, title: 'About Piper Desktop', titleBarStyle: 'hidden',
        titleBarOverlay: { color: theme().card, symbolColor: theme().fg, height: 38 },
        backgroundColor: theme().page, webPreferences: preferences });
      secure(about, false);
      about.once('ready-to-show', () => focus(about));
      about.on('closed', () => { about = null; if (!quitting && parent && !parent.isDestroyed()) focus(parent); });
      void about.loadFile(path.join(__dirname, 'ui/about.html'));
    } else focus(about);
    if (check) void desktopUpdate.check();
  }
  const preferences = { contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, backgroundThrottling: false, preload: path.join(__dirname, 'preload.cjs') };
  const saveBounds = () => {
    if (!main || main.isDestroyed() || main.isMinimized() || main.isMaximized()) return;
    bounds = { ...main.getBounds(), slot: mainSlot }; fs.writeFile(path.join(userData, 'bounds.json'), JSON.stringify(bounds), { mode: 0o600 }).catch(() => {});
  };
  function secure(win, remote) {
    const wc = win.webContents;
    const guard = (event, url) => {
      const policy = navigation(url, origin);
      if (!remote || policy !== 'internal') { event.preventDefault(); if (policy === 'external') void external(url); }
    };
    wc.on('will-navigate', guard); wc.on('will-redirect', guard);
    wc.on('will-attach-webview', event => event.preventDefault());
    wc.setWindowOpenHandler(({ url }) => {
      if (remote && navigation(url, origin) === 'internal') return { action: 'allow', overrideBrowserWindowOptions: { title: 'Piper', webPreferences: preferences } };
      if (navigation(url, origin) === 'external') void external(url);
      return { action: 'deny' };
    });
    wc.on('did-create-window', child => secure(child, true));
    wc.on('page-title-updated', (event, title) => { event.preventDefault(); win.setTitle(remote && title.startsWith('Piper') ? title.slice(0, 180) : win === about ? 'About Piper Desktop' : 'Piper'); });
    wc.on('render-process-gone', () => { if (remote) rendererFailed(win); });
    if (remote) wc.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
      void windowLoadFailed(win, code, isMainFrame);
    });
  }
  /** A crashed renderer is local to its window; daemon reachability comes only from the service probe. */
  function rendererFailed(win) {
    if (quitting || win.isDestroyed()) return;
    if (!connected) { failedRenderers.add(win); return; }
    // Recover the existing window without showing/focusing it or disturbing the other terminals.
    setTimeout(() => { if (!quitting && !win.isDestroyed()) {
      if (connected) win.webContents.reload(); else failedRenderers.add(win);
    } }, 1000);
  }
  /** A navigation or subframe failure in one window does not establish a daemon outage. */
  async function windowLoadFailed(win, code, isMainFrame) {
    if (code === -3 || isMainFrame === false || quitting || win.isDestroyed()) return;
    const reachable = await api.probe();
    if (quitting || win.isDestroyed()) return;
    if (reachable) rendererFailed(win); else await offline();
  }
  function localWindow(screenName) {
    if (local && !local.isDestroyed()) { sizeLocal(screenName); focus(local); return; }
    local = new BrowserWindow({ width: screenName === 'restart' ? 1040 : screenName === 'extensions' ? 640 : 960, height: screenName === 'restart' ? 560 : screenName === 'extensions' ? 420 : 620,
      titleBarStyle: 'hidden', titleBarOverlay: { color: theme().card, symbolColor: theme().fg, height: 38 },
      useContentSize: true, minWidth: 620, minHeight: 400, title: 'Piper', backgroundColor: theme().page, webPreferences: preferences });
    secure(local, false);
    localLayout = screenName === 'restart' ? 'restart' : screenName === 'extensions' ? 'extensions' : 'wizard';
    local.on('close', event => { if (!quitting) { event.preventDefault(); if (restartState) void closeRestart(); else local.hide(); } });
    void local.loadFile(path.join(__dirname, 'ui/index.html'));
  }
  async function attach(url) {
    tokenURL = url;
    if (!main || main.isDestroyed()) {
      main = new BrowserWindow({ ...placement(bounds, screen.getAllDisplays()), titleBarStyle: 'hidden', titleBarOverlay: { color: theme().card, symbolColor: theme().fg, height: 38 }, minWidth: 620, minHeight: 400, title: 'Piper', backgroundColor: '#0c0d0f', webPreferences: preferences });
      secure(main, true);
      main.on('resize', saveBounds); main.on('move', saveBounds);
      main.on('close', event => { if (!quitting) { event.preventDefault(); main.hide(); } });
    }
    await main.loadURL(windowURL(url, mainSlot)); connected = true;
    if (local && !local.isDestroyed()) local.hide();
    focus(main); clearTimeout(offlineTimer);
    // the extra windows come back with the service: reloaded (a new piperd), shown behind the main window
    for (const [slot, win] of extra) if (!win.isDestroyed()) { void win.loadURL(windowURL(url, slot)).catch(() => {}); win.showInactive(); }
    if (!restored) { restored = true; for (const w of savedWindows) openWindow({ slot: w.slot, bounds: w.bounds, background: true }); }
    void poll();
  }
  /** Reconnect existing renderers in place: reloading loses terminal selection and unsent browser input. */
  async function resumeWorkspace() {
    if (!main || main.isDestroyed() || !main.webContents.getURL().startsWith(origin + '/')) return attach(await api.url());
    connected = true; clearTimeout(offlineTimer);
    if (local && !local.isDestroyed()) local.hide();
    focus(main);
    for (const win of extra.values()) if (!win.isDestroyed()) win.showInactive();
    void poll();
  }
  async function closeRestart() {
    if (restartState?.kind === 'outage') {
      if (restartState.reachable) { await finishOutage(); return; }
      restartHidden = true; clearTimeout(offlineTimer);
      if (local && !local.isDestroyed()) local.hide();
      if (main && !main.isDestroyed()) focus(main);
      for (const win of extra.values()) if (!win.isDestroyed()) win.showInactive();
      return;
    }
    if (restartState?.checking) restartHidden = true; else restartState = null;
    await resumeWorkspace();
  }
  /**
   * Another Piper window (⌘N, Open Session in New Window): the same GUI with `?window=N`, so it keeps its own session,
   * layout and grid (the GUI's per-window keys) - for a second monitor. Closing it closes it; quitting keeps the
   * list (windows.json) and the next start opens them again where they were.
   */
  function openWindow({ slot, bounds: where, session: sid, background = false } = {}) {
    if (!connected || !tokenURL) { show(); return null; }
    slot ||= freeSlot([mainSlot, ...extra.keys()]);
    if (slot === mainSlot && main && !main.isDestroyed()) { focus(main); return main; }
    if (!slot || extra.has(slot)) { if (extra.has(slot)) focus(extra.get(slot)); return extra.get(slot) || null; }
    const win = new BrowserWindow({ ...placement(where, screen.getAllDisplays(), main && !main.isDestroyed() ? main.getBounds() : null),
      minWidth: 620, minHeight: 400, titleBarStyle: 'hidden', titleBarOverlay: { color: theme().card, symbolColor: theme().fg, height: 38 }, title: 'Piper', backgroundColor: '#0c0d0f', show: false, webPreferences: preferences });
    secure(win, true);
    extra.set(slot, win);
    announceWorkspaces();
    win.on('resize', saveWindows); win.on('move', saveWindows);
    win.on('closed', () => { for (const [key, window] of extra) if (window === win) extra.delete(key); if (!quitting) saveWindows(); announceWorkspaces(); });
    win.once('ready-to-show', () => { if (background) win.showInactive(); else focus(win); });
    void win.loadURL(windowURL(tokenURL, slot)).then(() => { if (sid) selectSession(win, sid); }).catch(() => {});
    saveWindows();
    return win;
  }
  /** windows.json: each extra window's slot and normal bounds; written while the app runs, read at start. */
  function saveWindows() {
    if (!restored) return;   // never overwrite the saved list before it was opened
    const list = [...extra].filter(([, w]) => !w.isDestroyed()).map(([slot, w]) => ({ slot, bounds: w.getNormalBounds() }));
    try { require('node:fs').writeFileSync(path.join(userData, 'windows.json'), JSON.stringify(list), { mode: 0o600 }); } catch { /* next change */ }
  }
  async function connect() {
    await controller.resume(); await controller.windows();
    if (platform === 'win32' && controller.distro()) { const p = await controller.prepare(); Object.assign(wslEnv, p.serviceEnv); }
    else if (platform !== 'win32') await controller.prepare();
    await start();
  }
  /** Every start of a stopped service (app start, offline Start Piper) asks about a found extension first. */
  async function start() {
    const result = await api.start();
    if (result.state === 'attach') {
      if (restartState?.kind === 'outage') { api.stopOutage(); restartState = null; restartHidden = false; }
      if (main && !main.isDestroyed()) await resumeWorkspace(); else await attach(result.url);
    }
    // The service is down and an extension waits for a first answer: ask before starting without it.
    else if (result.state === 'offer') { restartState = null; controller.offer(result.extensions); localWindow('extension'); sendState(); }
    else { restartState = null; localWindow('check'); await controller.check(); }
  }
  async function offline() {
    if (quitting || !connected) return;
    connected = false; model = trayModel(null, false); refreshTray();
    // A brief interruption must not hide/reload retained renderers or flash an error window.
    restartHidden = false;
    offlineTimer = setTimeout(() => {
      if (quitting || connected || restartState?.kind !== 'outage' || restartHidden || restartViews.size) return;
      localWindow('restart'); sendState();
    }, 5000);
    void api.observeOutage({ changed: outageChanged });
  }
  function outageChanged(state) {
    if (quitting) return;
    restartState = state;
    if (state.reachable) {
      if (local && !local.isDestroyed() && local.isVisible() && localLayout === 'restart') {
        api.stopOutage(); clearTimeout(offlineTimer); connected = true;
        restartState = { ...state, checking: false }; sendState();
        void poll();
      } else void finishOutage();
      return;
    }
    sendState();
  }
  async function finishOutage(pollNow = true) {
    api.stopOutage(); clearTimeout(offlineTimer);
    const wasShown = local && !local.isDestroyed() && local.isVisible() && localLayout === 'restart';
    restartState = null; restartHidden = false;
    connected = true;
    for (const win of failedRenderers) if (!win.isDestroyed()) win.webContents.reload();
    failedRenderers.clear();
    if (wasShown) {
      local.hide(); focus(main);
      for (const win of extra.values()) if (!win.isDestroyed()) win.showInactive();
    }
    if (pollNow) void poll();
  }
  async function startFromOutage() {
    if (startingOutage) return;
    startingOutage = true;
    const outage = restartState?.kind === 'outage';
    if (outage) { api.stopOutage(); restartState.starting = true; sendState(); }
    try { await start(); }
    catch (e) {
      if (restartState?.kind === 'outage') {
        restartState = { ...restartState, checking: false, starting: false,
          finding: 'Piper could not be started. Show the log for details before trying again.' };
      } else controller.wizard.state.error = redact(e.message);
      sendState();
      throw new Error('Piper could not be started. Show the log for details before trying again.');
    }
    finally { startingOutage = false; }
  }
  const localState = () => restartState && !restartHidden ? { screen: 'restart', restart: restartState, port } : { ...controller.wizard.state, busy: controller.busy, port };
  function sendState() {
    if (local && !local.isDestroyed()) { sizeLocal(localState().screen); local.webContents.send('setup:state', localState()); }
  }
  function sizeLocal(screenName) {
    const layout = screenName === 'restart' ? 'restart' : screenName === 'extensions' ? 'extensions' : 'wizard';
    if (localLayout === layout) return;
    localLayout = layout;
    local.setContentSize(...(layout === 'extensions' ? [640, 420] : layout === 'restart' ? [1040, 560] : [960, 620]));
  }
  async function poll() {
    clearTimeout(pollTimer);
    if (quitting) return;
    try {
      const state = await api.state();
      if (!connected && restartState?.kind === 'outage') await finishOutage(false);
      model = trayModel(state);
      serviceVersion = typeof state.service_version === 'string' ? state.service_version : '';
      const plan = notifyPlan(notifyMemo, state, !!BrowserWindow.getFocusedWindow());
      notifyMemo = plan.memo;
      // a notification that cannot be posted never takes the service offline
      for (const note of plan.notes) { try { post(note.title, note.body, () => openSession(note.session), answers(note)); } catch { /* next poll */ } }
    }
    // An extension change restarts piperd on purpose (so do Restart / Update Piper, `holding`): the window stays and
    // the next poll finds it again.
    catch {
      model = trayModel(null, false); notifyMemo = null;
      if (restartState?.kind === 'outage' && restartState.reachable) restartState = null;
      if (!controller.busy && !holding && !restartState && !restartViews.size) await offline();
    }
    refreshTray(); sendAbout(); pollTimer = setTimeout(poll, model.poll);
  }
  /** Show the main window on a session: the GUI selects it from a `#session=` fragment (hashchange). */
  function openSession(id) {
    if (!connected || !main || main.isDestroyed()) { show(); return; }
    focus(main);
    if (id) selectSession(main, id);
  }
  function selectSession(win, id) {
    void win.webContents.executeJavaScript(`history.replaceState(null, '', location.pathname + location.search); location.hash = ${JSON.stringify('session=' + encodeURIComponent(id))};`).catch(() => {});
  }
  /** A confirmation card answered outside the window (tray, notification button); the GUI shows the result. */
  async function answer(id, approve) {
    try { await api.decide(id, approve); } catch (e) { tell('The card was not answered: ' + redact(e.message || String(e)), true); }
    void poll();
  }
  /** A reply typed into a notification of a session waiting at its prompt. */
  async function reply(session, text) {
    if (!String(text || '').trim()) return;
    try { await api.send(session, String(text).slice(0, 20000)); } catch (e) { tell('The reply was not sent: ' + redact(e.message || String(e)), true); }
    void poll();
  }
  /** The buttons of one main-process notification: Allow (when the GUI's `y` may) / Deny on a card, Reply on a waiting session. */
  function answers(note) {
    if (note.card) return { actions: [...(note.card.allow ? [{ label: 'Allow', run: () => answer(note.card.id, true) }] : []), { label: 'Deny', run: () => answer(note.card.id, false) }] };
    if (note.reply) return { reply: text => reply(note.reply, text) };
    return {};
  }
  /** A result of a tray action: a native message box; tests record it. */
  function tell(message, warning = false) {
    if (env.PIPER_DESKTOP_HOME) { told.push(message); return; }
    void dialog.showMessageBox({ type: warning ? 'warning' : 'info', title: 'Piper', message, buttons: ['OK'] }).catch(() => {});
  }
  /** One Machine action at a time; its name replaces the menu's items until it ends. */
  async function machineOp(busy, fn) {
    if (machineBusy) return;
    machineBusy = busy; refreshTray();
    try { const message = await fn(); if (message) tell(message); }
    catch (e) { tell(redact(e.message || String(e)), true); }
    finally { machineBusy = ''; holding = false; refreshTray(); }
  }
  async function restartService() {
    holding = true;
    try { await api.restart({ changed: restartChanged }); } catch { /* the local screen retains the observed failure */ }
    void poll();
    return '';
  }
  function restartChanged(state) {
    const first = !restartState || (state.phase === 'requesting' && state.startedAt !== restartState.startedAt);
    if (first) restartHidden = false;
    restartState = state;
    if (restartHidden) {
      if (state.checking) return;
      if (['recovered', 'returned'].includes(state.phase)) {
        if (main && !main.isDestroyed()) main.webContents.send('restart:result', state.finding);
        restartState = null; return;
      }
      restartHidden = false; localWindow('restart'); focus(local);
    }
    if (first) { localWindow('restart'); focus(local); }
    sendState();
  }
  async function updateService() {
    holding = connected;
    // The CLI owns exactly one update; the independent local screen remains responsive throughout it.
    try { await api.restart({ changed: restartChanged, action: () => ops.update() }); }
    catch { /* retain the diagnostic outcome; never repeat the update */ }
    void poll(); return '';
  }
  async function copyDiagnostics() {
    const text = await ops.diagnostics({ app: app.getVersion(), os: platform, arch: process.arch, online: model.online });
    clipboard.writeText(text);
    return 'Diagnostics copied. Tokens and passwords are removed; read it before you share it.';
  }
  /** macOS refuses UNUserNotificationCenter to an ad-hoc signed app ("Notifications are not allowed for this
   *  application"); the fallback posts through osascript (attributed to Script Editor, no click-through). */
  function fallback(title, body) {
    if (process.platform !== 'darwin') return;
    if (env.PIPER_DESKTOP_HOME) { fallbacks.push({ title, body }); return; }   // tests never post a real notification
    execFile('/usr/bin/osascript', osascriptArgs(title, body), { timeout: 10000 }, () => {});
  }
  /** Post a native notification; once the OS refused one, the fallback from then on (null: nothing to track). */
  function post(title, body, onClick, { actions = [], reply: onReply = null } = {}) {
    if (env.PIPER_DESKTOP_HOME) testNotes.push({ title, body, onClick, actions, onReply });
    if (nativeFailed) { fallback(title, body); return null; }
    // buttons and the reply field are macOS notification features; elsewhere the tray's card menu answers
    const n = new Notification({ title, body, actions: actions.map(a => ({ type: 'button', text: a.label })),
      ...(onReply ? { hasReply: true, replyPlaceholder: 'Reply to the session' } : {}) });
    posted.add(n);
    n.on('click', onClick);
    n.on('action', (_event, index) => { void actions[index]?.run(); });
    if (onReply) n.on('reply', (_event, text) => { void onReply(text); });
    n.on('close', () => posted.delete(n));
    n.on('failed', () => { posted.delete(n); nativeFailed = true; fallback(title, body); });
    n.show();
    return n;
  }
  async function autostart(enabled) {
    if (env.PIPER_DESKTOP_HOME) { login = enabled; refreshTray(); return; }
    if (platform === 'linux') {
      const dir = path.join(linuxConfigHome, 'autostart');
      await fs.mkdir(dir, { recursive: true });
      const file = path.join(dir, LINUX_ENTRY + '.desktop');
      // the AppImage's icon is the copy integrateAppImage installs; the deb package installs a theme icon
      const icon = env.APPIMAGE ? path.join(linuxDataHome, 'icons/hicolor/512x512/apps', LINUX_ENTRY + '.png') : LINUX_ENTRY;
      if (enabled) await fs.writeFile(file, linuxAutostart(env.APPIMAGE || process.execPath, { icon, wmClass: app.getName() }), { mode: 0o600 });
      else await fs.rm(file, { force: true });
    } else app.setLoginItemSettings({ openAtLogin: enabled });
    login = enabled; refreshTray();
  }
  function refreshTray() {
    if (!tray) return;
    const summary = `${model.sessions} sessions · ${model.count} waiting for you`;
    const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
    tray.setToolTip(model.online ? `Piper — ${summary}` : 'Piper is not running');
    if (process.platform === 'darwin') tray.setTitle(model.count ? String(model.count) : '');
    else if (trayImage) tray.setImage(nativeImage.createFromBitmap(badge(trayImage.toBitmap(), model.count, theme().warn), { width: 24, height: 24 }));
    app.setBadgeCount(model.count);
    if (main && !main.isDestroyed()) main.setTitle(`Piper - ${model.sessions} sessions - ${model.count} waiting for you`);
    const free = !machineBusy && !controller?.busy;
    trayMenu = Menu.buildFromTemplate([
      { label: model.online ? 'Piper is running' : 'Piper is not running', enabled: false },
      { label: summary, enabled: false }, { type: 'separator' },
      { label: 'WAITING FOR YOU', enabled: false },
      // a card is answered right here (Allow only where the GUI's `y` may: never a red or a prompt card)
      ...(model.entries || []).map(e => e.card ? { label: e.label, submenu: [
        ...(e.card.allow ? [{ label: 'Allow', click: () => void answer(e.card.id, true) }] : []),
        { label: 'Deny', click: () => void answer(e.card.id, false) }, { type: 'separator' },
        { label: e.card.allow ? 'Open in Piper' : 'Open in Piper to allow', click: () => openSession(e.card.session || e.session) }] }
        : { label: e.label, click: () => openSession(e.session) }),
      ...(model.waiting ? [{ label: `${plural(model.waiting, 'session')} waiting`, click: show }] : []),
      ...(model.inbox ? [{ label: `${model.inbox} in inbox`, click: show }] : []), { type: 'separator' },
      { label: 'Open Piper', accelerator: 'CommandOrControl+O', click: show },
      { label: 'New Window', enabled: connected, click: () => openWindow() },
      { label: 'Open Session in New Window', enabled: connected && model.list.length > 0, submenu: sessionItems() },
      { label: 'Open in browser', enabled: !!tokenURL, click: () => void external(tokenURL) }, { type: 'separator' },
      { label: 'Machine', submenu: machineBusy ? [{ label: machineBusy, enabled: false }] : [
        { label: 'Start Docker', enabled: free, click: () => void machineOp('Starting Docker…', async () => (await ops.startRuntime()) === 'running' ? 'Docker is already running.' : 'Docker is running.') },
        model.online ? { label: 'Restart Piper', enabled: free, click: () => void machineOp('Restarting Piper…', restartService) }
          : { label: 'Start Piper', enabled: free, click: () => void machineOp('Starting Piper…', async () => { await start(); return ''; }) },
        { label: 'Update Piper', enabled: free, click: () => void machineOp('Updating Piper…', updateService) },
        { type: 'separator' },
        { label: 'Copy Diagnostics', enabled: free, click: () => void machineOp('Collecting diagnostics…', copyDiagnostics) },
        { label: 'Sessions keep running through a restart or an update.', enabled: false }] },
      { label: 'Extensions…', enabled: model.online && !controller?.busy, click: () => { localWindow('extensions'); void controller.extensions().catch(e => { controller.wizard.fail(e); controller.wizard.state.retryAction = 'extensions'; sendState(); }); } },
      { label: 'About Piper Desktop', click: () => showAbout() },
      ...(!windowsStore ? [{ label: 'Check for Updates…', click: () => showAbout(true) }] : []),
      { label: 'Start at login', type: 'checkbox', checked: login, click: item => void autostart(item.checked).catch(() => {}) }, { type: 'separator' },
      { label: 'Quit Piper window', accelerator: 'CommandOrControl+Q', click: () => app.quit() },
      { label: 'Sessions keep running; the Piper service stays on.', enabled: false }
    ]);
    tray.setContextMenu(trayMenu);
    appMenu();
  }
  /** The sessions to open in a window of their own (tray and File menu). */
  function sessionItems() {
    return model.list.length ? model.list.map(s => ({ label: s.title, click: () => openWindow({ session: s.id }) })) : [{ label: 'No sessions', enabled: false }];
  }
  /** The application menu: File > New Window (⌘N) / Open Session in New Window, and the system Window menu. */
  function appMenu() {
    const key = JSON.stringify([connected, model.list.map(s => [s.id, s.title])]);
    if (key === appMenuKey) return;
    appMenuKey = key;
    Menu.setApplicationMenu(Menu.buildFromTemplate([
      { label: 'Piper', submenu: [...(platform === 'darwin' ? aboutItems() : []), { label: 'Open Piper', click: show }, { label: 'Quit Piper window', accelerator: 'CommandOrControl+Q', click: () => app.quit() }] },
      { label: 'File', submenu: [{ label: 'New Window', accelerator: 'CommandOrControl+N', enabled: connected, click: () => openWindow() },
        { label: 'Open Session in New Window', enabled: connected && model.list.length > 0, submenu: sessionItems() }] },
      { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
      ...(platform !== 'darwin' ? [{ label: 'Help', submenu: aboutItems() }] : [])]));
  }
  function aboutItems() {
    return [{ label: 'About Piper Desktop', click: () => showAbout() },
      ...(!windowsStore ? [{ label: 'Check for Updates…', click: () => showAbout(true) }] : []), { type: 'separator' }];
  }
  const aboutSender = event => about && !about.isDestroyed() && event.sender === about.webContents &&
    event.senderFrame === about.webContents.mainFrame && event.senderFrame.url === pathToFileURL(path.join(__dirname, 'ui/about.html')).href;
  ipcMain.on('about:resize', (event, height) => {
    if (!aboutSender(event) || typeof height !== 'number' || !Number.isFinite(height)) return;
    const maximum = Math.max(469, screen.getDisplayMatching(about.getBounds()).workAreaSize.height - 48);
    const wanted = Math.max(469, Math.min(maximum, Math.ceil(height)));
    if (about.getContentSize()[1] !== wanted) about.setContentSize(480, wanted);
  });
  ipcMain.handle('about:state', event => { if (!aboutSender(event)) throw new Error('Forbidden'); return aboutState(); });
  ipcMain.handle('about:action', async (event, action) => {
    if (!aboutSender(event)) throw new Error('Forbidden');
    const links = { project: 'https://github.com/Kamest/Piper', notes: desktopUpdate.state().releaseURL || 'https://github.com/Kamest/Piper/releases',
      releases: 'https://github.com/Kamest/Piper/releases', issue: 'https://github.com/Kamest/Piper/issues/new/choose' };
    if (Object.hasOwn(links, action)) return external(links[action]);
    switch (action) {
      case 'check': return desktopUpdate.check();
      case 'download': return desktopUpdate.download();
      case 'retry': return desktopUpdate.state().errorStage === 'download' ? desktopUpdate.download() : desktopUpdate.check();
      case 'install': return desktopUpdate.install();
      case 'close': about.close(); return;
      case 'setup': about.close(); show(); return;
      case 'copy': {
        const s = aboutState(), channel = s.update.status === 'store' ? 'Microsoft Store' : s.update.status === 'development' ? 'Development' : s.update.method === 'download' ? 'Manual download' : 'Automatic';
        clipboard.writeText(['Piper Desktop ' + s.version, 'desktop-v' + s.version, s.system, s.architecture, channel,
          'Piper service: ' + (s.online ? (s.serviceVersion || 'not reported') + ' · ' + s.serviceLocation : 'Not reachable')].join('\n')); return;
      }
      case 'store':
        if (desktopUpdate.state().status !== 'store') throw new Error('Store updates are unavailable');
        return env.PIPER_DESKTOP_HOME ? externals.push('ms-windows-store://downloadsandupdates') : shell.openExternal('ms-windows-store://downloadsandupdates');
      default: throw new Error('Unknown desktop action');
    }
  });
  const localSender = event => local && !local.isDestroyed() && event.sender === local.webContents && event.senderFrame === local.webContents.mainFrame && event.senderFrame.url === pathToFileURL(path.join(__dirname, 'ui/index.html')).href;
  const remoteSender = event => event.senderFrame === event.sender.mainFrame && navigation(event.senderFrame.url, origin) === 'internal';
  ipcMain.handle('file:pick-directory', async (event, initial) => {
    if (!remoteSender(event) || platform === 'win32' || (initial !== undefined && (typeof initial !== 'string' || initial.length > 8192 || initial.includes('\0')))) throw new Error('Forbidden');
    const win = BrowserWindow.fromWebContents(event.sender);
    const result = await dialog.showOpenDialog(win, { title: 'Choose project folder', buttonLabel: 'Choose folder',
      properties: ['openDirectory', 'createDirectory'], ...(initial && path.isAbsolute(initial) ? { defaultPath: initial } : {}) });
    return result.canceled ? null : result.filePaths[0] || null;
  });
  /** Backend paths belong to the daemon's selected WSL distribution, never the default distro. */
  async function nativeFilePath(file) {
    if (platform !== 'win32') return file;
    const distro = controller.distro();
    if (process.arch !== 'x64' || !distro || !file.startsWith('/')) throw new Error('Native files require the selected WSL distribution');
    const result = await run('wsl.exe', ['-d', distro, '--exec', 'wslpath', '-w', file], { timeout: 10000 });
    const value = result.stdout.replace(/\r?\n$/, '');
    const unc = value.split('\\');
    const selectedUNC = unc[0] === '' && unc[1] === '' && ['wsl.localhost', 'wsl$'].includes(unc[2]?.toLowerCase()) && unc[3]?.toLowerCase() === distro.toLowerCase();
    if (value.length > 8192 || /[\r\n\0]/.test(value) || (!/^[A-Za-z]:\\/.test(value) && !selectedUNC)) throw new Error('File path conversion failed');
    return value;
  }
  ipcMain.handle('file:action', async (event, action, file) => {
    if (!remoteSender(event) || action !== 'reveal') throw new Error('Forbidden');
    const target = await api.fileTarget(file);
    shell.showItemInFolder(await nativeFilePath(target.path));
  });
  const preparedDrags = new WeakMap();
  ipcMain.handle('file:prepare-drag', async (event, file) => {
    if (!remoteSender(event)) throw new Error('Forbidden');
    const target = await api.fileTarget(file), nativePath = await nativeFilePath(target.path);
    const icon = await app.getFileIcon(nativePath);
    if (event.sender.isDestroyed() || !remoteSender(event)) throw new Error('Window closed');
    const identity = fsSync.lstatSync(nativePath), real = fsSync.realpathSync(nativePath);
    const samePath = platform === 'win32' ? path.win32.normalize(real).toLowerCase() === path.win32.normalize(nativePath).toLowerCase() : real === nativePath;
    if (!samePath || identity.isSymbolicLink() || (!identity.isFile() && !identity.isDirectory())) throw new Error('File changed');
    const token = randomUUID();
    // Small sender-owned cache: moving across another row must not invalidate a still-ready item.
    const items = preparedDrags.get(event.sender) || new Map();
    for (const [key, item] of items) if (Date.now() - item.at > 30000) items.delete(key);
    if (items.size >= 16) items.delete(items.keys().next().value);
    items.set(token, { file: nativePath, real, identity, icon, frame: event.senderFrame, url: event.senderFrame.url, at: Date.now() });
    preparedDrags.set(event.sender, items);
    return token;
  });
  ipcMain.on('file:drag', (event, token) => {
    if (!remoteSender(event)) return;
    const items = preparedDrags.get(event.sender), item = items?.get(token);
    items?.delete(token);
    if (!item || item.frame !== event.senderFrame || item.url !== event.senderFrame.url || Date.now() - item.at > 30000 || event.sender.isDestroyed()) return;
    try {
      // Keep the actual OS drag in the original gesture; no async API/icon work here.
      const current = fsSync.lstatSync(item.file);
      if (current.isSymbolicLink() || fsSync.realpathSync(item.file) !== item.real || current.dev !== item.identity.dev || current.ino !== item.identity.ino || current.mtimeMs !== item.identity.mtimeMs || current.size !== item.identity.size) return;
      event.sender.startDrag({ file: item.file, icon: item.icon });
    } catch { /* changed/missing files never reach the OS drag */ }
  });
  /** One current workspace owner per native window, updated atomically before renderer navigation. */
  function workspaceOwner(slot) {
    if (slot === mainSlot && main && !main.isDestroyed()) return main;
    const win = extra.get(slot); return win && !win.isDestroyed() ? win : null;
  }
  function nativeWorkspaceSlots(sender) {
    return [...new Set([...(main && !main.isDestroyed() && main.webContents !== sender ? [mainSlot] : []),
      ...[...extra].filter(([, win]) => !win.isDestroyed() && win.webContents !== sender).map(([slot]) => slot)])];
  }
  function announceWorkspaces() {
    for (const win of [main, ...extra.values()]) if (win && !win.isDestroyed()) win.webContents.send('workspace:changed', nativeWorkspaceSlots(win.webContents));
  }
  ipcMain.handle('workspace:action', (event, action, slot) => {
    if (!remoteSender(event) || !Number.isInteger(slot) || slot < 1 || slot > 99) throw new Error('Forbidden');
    if (action === 'state') return nativeWorkspaceSlots(event.sender);
    const owner = workspaceOwner(slot), sender = BrowserWindow.fromWebContents(event.sender);
    if (action === 'claim') {
      if (!sender || sender.isDestroyed()) throw new Error('Window closed');
      if (owner && owner !== sender) { focus(owner); throw new Error('This workspace is already open in another window'); }
      if (sender === main) { mainSlot = slot; saveBounds(); }
      else {
        for (const [key, win] of extra) if (win === sender) extra.delete(key);
        extra.set(slot, sender); saveWindows();
      }
      announceWorkspaces(); return nativeWorkspaceSlots(event.sender);
    }
    if (action === 'open') { if (owner) focus(owner); else openWindow({ slot }); }
    else if (['bring', 'close'].includes(action)) {
      if (owner === main && owner.webContents !== event.sender) throw new Error('This workspace belongs to the main window; close or switch it there');
      if (owner && owner !== main) owner.close();
    } else throw new Error('Unknown workspace action');
    return nativeWorkspaceSlots(event.sender);
  });
  ipcMain.on('restart:visible', (event, visible) => {
    if (!remoteSender(event) || typeof visible !== 'boolean') return;
    if (visible) {
      if (!restartViews.has(event.sender.id)) event.sender.once('destroyed', () => restartViews.delete(event.sender.id));
      restartViews.add(event.sender.id);
      if (restartState?.kind === 'outage' && local && !local.isDestroyed() && localLayout === 'restart') {
        restartHidden = true; local.hide();
      }
    } else restartViews.delete(event.sender.id);
  });
  ipcMain.handle('restart:action', async (event, name) => {
    if (!remoteSender(event) || !['ensure', 'log'].includes(name)) throw new Error('Forbidden');
    if (name === 'log') return controller.action('log');
    // Only a click in the trusted local GUI can request a start; observers remain read-only.
    await startFromOutage();
  });
  ipcMain.handle('setup:state', event => { if (!localSender(event)) throw new Error('Forbidden'); return localState(); });
  ipcMain.handle('setup:action', async (event, name, value) => {
    if (!localSender(event) || typeof name !== 'string' || (value !== undefined && typeof value !== 'string')) throw new Error('Forbidden');
    if (name === 'restart-close' && restartState) {
      await closeRestart(); return;
    }
    if (name === 'restart-check' && restartState) { void api.checkRestart({ changed: restartChanged }); return; }
    if (name === 'restart-copy' && restartState) {
      // The renderer may supply only its own whitelisted summary, bounded; no terminal or process command.
      if (typeof value === 'string' && value.length <= 8000) clipboard.writeText(value);
      return;
    }
    if (name === 'quit') { app.quit(); return; }
    if (name === 'copy') { clipboard.writeText(controller.wizard.state.log.join('\n')); return; }
    if (name === 'browser') { if (tokenURL) await external(tokenURL); return; }
    if (name === 'ensure') {
      try { await startFromOutage(); } catch { /* the local diagnostic screen retains the safe failure */ }
      return;
    }
    if (name === 'retry' && controller.wizard.state.retryAction === 'connect') {
      try { await connect(); }
      catch (e) { controller.wizard.fail(e); sendState(); } return;
    }
    await controller.action(name, value);
  });
  ipcMain.on('notification:show', (event, data) => {
    if (!remoteSender(event) || !data || !/^\d{1,10}$/.test(data.id) || typeof data.title !== 'string' || typeof data.body !== 'string' || notifications.size > 100) return;
    const closed = () => { if (!event.sender.isDestroyed()) event.sender.send('notification:event', { id: data.id, type: 'close' }); };
    // Unfocused, the main process announces session states and cards itself (poll); the GUI's copy would double it.
    // It still posts the GUI-only ones (inbox, CI, production) and everything while a Piper window has focus.
    if (!BrowserWindow.getFocusedWindow() && ownedByMain(data)) { closed(); return; }
    const key = event.sender.id + ':' + data.id;
    const n = post(data.title.slice(0, 200), data.body.slice(0, 2000), () => { focus(BrowserWindow.fromWebContents(event.sender)); event.sender.send('notification:event', { id: data.id, type: 'click' }); });
    if (!n) { closed(); return; }
    notifications.set(key, n);
    n.on('close', () => { notifications.delete(key); closed(); });
  });
  ipcMain.on('notification:close', (event, id) => { if (remoteSender(event)) { const key = event.sender.id + ':' + id; notifications.get(key)?.close(); notifications.delete(key); } });
  // Test harness can exercise the same native click callback without OS notification permissions.
  if (env.PIPER_DESKTOP_HOME) app.piperTest = { clickNotification: () => notifications.values().next().value?.emit('click'), showExtensions: () => { localWindow('extensions'); return controller.extensions(); }, tray: () => model,
    setUpdates: overrides => { desktopUpdate = createUpdates(overrides); sendAbout(); },
    poll: () => poll(), fallbacks: () => fallbacks, failNative: () => { nativeFailed = true; },
    notes: () => testNotes.map(n => n.title), clickNote: i => testNotes[i]?.onClick(), openSession: id => openSession(id),
    // 9.173: a notification's buttons / reply, the tray and application menus as data, a click by label path
    noteButtons: i => (testNotes[i]?.actions || []).map(a => a.label), noteHasReply: i => !!testNotes[i]?.onReply,
    noteAction: (i, label) => testNotes[i]?.actions.find(a => a.label === label)?.run(), noteReply: (i, text) => testNotes[i]?.onReply?.(text),
    menu: which => shape(which === 'app' ? Menu.getApplicationMenu() : trayMenu),
    click: (which, labels) => { const item = find(which === 'app' ? Menu.getApplicationMenu() : trayMenu, labels); if (!item || !item.enabled) throw new Error('No enabled menu item ' + labels.join(' > ')); item.click(); },
    told: () => told, externals: () => externals, busy: () => machineBusy,
    windows: () => [...extra].map(([slot, w]) => ({ slot, url: w.webContents.getURL().replace(/#.*/, ''), visible: w.isVisible(), bounds: w.getBounds() })) };
  const shape = menu => (menu?.items || []).filter(i => i.type !== 'separator').map(i => ({ label: i.label, enabled: i.enabled, ...(i.submenu && i.type === 'submenu' ? { submenu: shape(i.submenu) } : {}) }));
  // a label, or the index among the visible items (two rows may share a label)
  const find = (menu, [first, ...rest]) => { const items = (menu?.items || []).filter(i => i.type !== 'separator'); const item = typeof first === 'number' ? items[first] : items.find(i => i.label === first); return !item ? null : rest.length ? find(item.submenu, rest) : item; };
  app.on('second-instance', show); app.on('activate', show);
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (controller?.busy) { event.preventDefault(); show(); return; }
    saveWindows();   // the open extra windows come back at the next start
    quitting = true; clearTimeout(pollTimer); clearTimeout(offlineTimer);
  });
  app.whenReady().then(async () => {
    await fs.mkdir(userData, { recursive: true });
    try { bounds = JSON.parse(await fs.readFile(path.join(userData, 'bounds.json'), 'utf8')); } catch { /* first launch */ }
    mainSlot = Number.isInteger(bounds.slot) && bounds.slot >= 1 && bounds.slot <= 99 ? bounds.slot : 1;
    try { savedWindows = restorable(JSON.parse(await fs.readFile(path.join(userData, 'windows.json'), 'utf8')), mainSlot); } catch { /* none yet */ }
    appMenu();
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => callback(navigation(wc.getURL(), origin) === 'internal' && ['notifications', 'clipboard-sanitized-write'].includes(permission)));
    session.defaultSession.setPermissionCheckHandler((wc, permission, requestingOrigin) => navigation(requestingOrigin, origin) === 'internal' && ['notifications', 'clipboard-sanitized-write'].includes(permission));
    const icon = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAGUlEQVQ4T2NkoBAwUqifYdQABtQwGDAAAAjQAAEFiENsAAAAAElFTkSuQmCC');
    // Use the product icon; the fallback only covers unpackaged tests without prepared resources.
    const productIcon = nativeImage.createFromPath(path.join(resources, 'tray.png'));
    trayImage = (productIcon.isEmpty() ? icon : productIcon).resize({ width: 24, height: 24 });
    tray = new Tray(trayImage);
    tray.on('click', show);
    // An AppImage registers its launcher and icon per user (tests never write into the user's menu)
    if (platform === 'linux' && env.APPIMAGE && !env.PIPER_DESKTOP_HOME) {
      await integrateAppImage({ appImage: env.APPIMAGE, iconSource: path.join(resources, 'icon.png'), dataHome: linuxDataHome,
        configHome: linuxConfigHome, wmClass: app.getName(), fs }).catch(e => console.warn('Piper: desktop menu entry not installed:', e.code || e.message));
    }
    login = platform === 'linux' ? await exists(path.join(linuxConfigHome, 'autostart', LINUX_ENTRY + '.desktop')) : app.getLoginItemSettings().openAtLogin;
    api = service({ port, run, fetch, platform, distro: () => controller.distro(), serviceEnv: wslEnv, locate,
      tokenFile: path.join(env.PIPER_HOME || path.join(nativeHome, '.piper'), 'gui-token'),
      declinedFile: path.join(env.PIPER_HOME || path.join(nativeHome, '.piper'), 'declined-extensions.json'),
      // On Windows the same file name inside the Linux Piper home.
      readFile: async (file, encoding) => platform === 'win32'
        ? (await run(...wsl('cat', [(wslEnv.PIPER_HOME || home.linux + '/.piper') + '/' + path.basename(file)], controller.distro(), wslEnv))).stdout
        : fs.readFile(file, encoding) });
    controller = setupController({ env, platform, windowsStore, arch: process.arch, home, userData, resources, pythonSource, pythonVersion, runtimeDir, fs, run, fetch, executable: process.execPath,
      service: { locate, command: async (args, opts) => args[0] === 'restart'
        ? api.restart({ changed: restartChanged, action: () => api.command(args, opts) }) : api.command(args, opts),
        ready: async () => { await api.wait(); await attach(await api.url()); },
        start: async decline => attach(await api.ensure({ decline })),
        extensionsAPI: api.extensionsAPI, changeExtension: api.changeExtension, restart: async () => {
          await api.restart({ changed: restartChanged });
        } }, changed: sendState, openExternal: external,
      showLog: async () => { const file = path.join(userData, 'setup.log'); if (!await exists(file)) await fs.writeFile(file, '', { mode: 0o600 }); await shell.openPath(file); } });
    // Machine: docker/podman inside the WSL distro on Windows, the CLI as everywhere
    ops = machine({ platform, run, command: api.command, inLinux: (c, a) => platform === 'win32' ? wsl(c, a, controller.distro(), wslEnv) : [c, a] });
    refreshTray();
    nativeTheme.on('updated', () => { for (const window of [local, about]) if (window && !window.isDestroyed()) { window.setBackgroundColor(theme().page); if (typeof window.setTitleBarOverlay === 'function') window.setTitleBarOverlay({ color: theme().card, symbolColor: theme().fg, height: 38 }); } refreshTray(); });
    try {
      await connect();
    } catch (e) { controller.wizard.fail(e); controller.wizard.state.retryAction = 'connect'; localWindow('error'); sendState(); }
  }).catch(() => { app.exit(1); });
}
