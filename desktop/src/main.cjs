'use strict';
const { app, BrowserWindow, Menu, Tray, nativeImage, nativeTheme, ipcMain, shell, Notification, clipboard, screen, session, dialog } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const { runner } = require('./lib/process.cjs');
const { service, locateCLI } = require('./lib/service.cjs');
const { setupController } = require('./lib/setup.cjs');
const { navigation, trayModel, redact, updatesEnabled, updateDecision } = require('./lib/policy.cjs');
const { linuxAutostart, wsl } = require('./lib/platform.cjs');
const signed = require('../package.json').desktopSigned === true;
const themes = require('./ui/themes.json');
const pythonVersion = require('./runtime.json').python;
const { badge } = require('./lib/badge.cjs');
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
  let main, local, tray, trayImage, controller, api, quitting = false, connected = false, pollTimer, offlineTimer, tokenURL = '';
  let model = trayModel(null, false), login = false, bounds = {}, updateURL = '', updateReady = false, localLayout = '';
  const notifications = new Map();
  const origin = `http://127.0.0.1:${port}`;
  const userData = app.getPath('userData');
  const resources = app.isPackaged ? path.join(process.resourcesPath, 'desktop') : path.resolve(__dirname, '../resources');
  const pythonSource = app.isPackaged ? path.join(process.resourcesPath, 'python') : path.resolve(__dirname, `../vendor/${process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'win' : 'linux'}-${process.arch}/python`);
  const nativeHome = os.homedir();
  const home = { native: nativeHome, linux: '' };
  // The service receives a stable Python path even after an AppImage unmounts.
  const runtimeDir = path.join(userData, 'runtime', 'python-' + pythonVersion);
  env.PATH = [path.join(runtimeDir, 'bin'), path.join(nativeHome, '.local/bin'), env.PATH || '', '/opt/homebrew/bin', '/usr/local/bin', '/opt/podman/bin', '/usr/bin', '/bin'].join(path.delimiter);
  const run = runner(spawn, env);
  const wslEnv = {};
  const exists = async p => { try { await fs.access(p); return true; } catch { return false; } };
  const locate = () => locateCLI({ env, home: nativeHome, installDir: controller.installDir(), exists, platform, run, distro: controller.distro() });
  const focus = win => { if (!win || win.isDestroyed()) return; if (win.isMinimized()) win.restore(); win.show(); win.focus(); app.focus({ steal: true }); };
  const show = () => focus(connected && main && !main.isDestroyed() ? main : local);
  const external = url => navigation(url, origin) !== 'deny' ? shell.openExternal(url) : Promise.resolve();
  const preferences = { contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, preload: path.join(__dirname, 'preload.cjs') };
  const saveBounds = () => {
    if (!main || main.isDestroyed() || main.isMinimized() || main.isMaximized()) return;
    bounds = main.getBounds(); fs.writeFile(path.join(userData, 'bounds.json'), JSON.stringify(bounds), { mode: 0o600 }).catch(() => {});
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
    wc.on('page-title-updated', (event, title) => { event.preventDefault(); win.setTitle(remote && title.startsWith('Piper') ? title.slice(0, 180) : 'Piper'); });
    wc.on('render-process-gone', () => { if (remote) void offline(); });
  }
  function localWindow(screenName) {
    if (local && !local.isDestroyed()) { sizeLocal(screenName); focus(local); return; }
    local = new BrowserWindow({ width: screenName === 'extensions' ? 640 : 960, height: screenName === 'extensions' ? 420 : 620,
      titleBarStyle: 'hidden', titleBarOverlay: { color: theme().card, symbolColor: theme().fg, height: 38 },
      useContentSize: true, minWidth: 620, minHeight: 400, title: 'Piper', backgroundColor: theme().page, webPreferences: preferences });
    secure(local, false);
    localLayout = screenName === 'extensions' ? 'extensions' : 'wizard';
    local.on('close', event => { if (!quitting) { event.preventDefault(); local.hide(); } });
    void local.loadFile(path.join(__dirname, 'ui/index.html'));
  }
  async function attach(url) {
    tokenURL = url;
    if (!main || main.isDestroyed()) {
      const valid = Number.isFinite(bounds.width) && Number.isFinite(bounds.height) && bounds.width >= 620 && bounds.height >= 400;
      const position = valid && screen.getAllDisplays().some(d => bounds.x < d.workArea.x + d.workArea.width && bounds.x + bounds.width > d.workArea.x && bounds.y < d.workArea.y + d.workArea.height && bounds.y + bounds.height > d.workArea.y);
      main = new BrowserWindow({ width: 1040, height: 760, ...(valid ? { width: bounds.width, height: bounds.height } : {}), ...(position ? { x: bounds.x, y: bounds.y } : {}), minWidth: 620, minHeight: 400, title: 'Piper', backgroundColor: '#0c0d0f', webPreferences: preferences });
      secure(main, true);
      main.on('resize', saveBounds); main.on('move', saveBounds);
      main.on('close', event => { if (!quitting) { event.preventDefault(); main.hide(); } });
      main.webContents.on('did-fail-load', (_event, code) => { if (code !== -3) void offline(); });
    }
    await main.loadURL(url); connected = true;
    if (local && !local.isDestroyed()) local.hide();
    focus(main); clearTimeout(offlineTimer); void poll();
  }
  async function connect() {
    await controller.resume(); await controller.windows();
    if (platform === 'win32' && controller.distro()) { const p = await controller.prepare(); Object.assign(wslEnv, p.serviceEnv); }
    else if (platform !== 'win32') await controller.prepare();
    const result = await api.start();
    if (result.state === 'attach') await attach(result.url);
    else { localWindow('check'); await controller.check(); }
  }
  async function offline() {
    if (quitting || !connected) return;
    connected = false; model = trayModel(null, false); refreshTray();
    controller.wizard.state.screen = 'offline'; controller.wizard.state.port = port;
    localWindow('offline'); sendState();
    if (main && !main.isDestroyed()) main.hide();
    const retry = async () => {
      if (quitting || connected) return;
      if (await api.probe()) { try { await attach(await api.url()); return; } catch { /* retry read-only */ } }
      offlineTimer = setTimeout(retry, 5000);
    };
    offlineTimer = setTimeout(retry, 5000);
  }
  function sendState() {
    if (local && !local.isDestroyed()) { sizeLocal(controller.wizard.state.screen); local.webContents.send('setup:state', { ...controller.wizard.state, busy: controller.busy, port }); }
  }
  function sizeLocal(screenName) {
    const layout = screenName === 'extensions' ? 'extensions' : screenName === 'offline' ? 'offline' : 'wizard';
    if (localLayout === layout) return;
    localLayout = layout;
    local.setContentSize(...(layout === 'extensions' ? [640, 420] : layout === 'offline' ? [1040, 560] : [960, 620]));
  }
  async function poll() {
    clearTimeout(pollTimer);
    if (quitting) return;
    try { model = trayModel(await api.state()); }
    catch { model = trayModel(null, false); await offline(); }
    refreshTray(); pollTimer = setTimeout(poll, model.poll);
  }
  async function autostart(enabled) {
    if (env.PIPER_DESKTOP_HOME) { login = enabled; refreshTray(); return; }
    if (platform === 'linux') {
      const dir = path.join(env.XDG_CONFIG_HOME || path.join(nativeHome, '.config'), 'autostart');
      await fs.mkdir(dir, { recursive: true });
      const file = path.join(dir, 'piper-desktop.desktop');
      if (enabled) await fs.writeFile(file, linuxAutostart(env.APPIMAGE || process.execPath), { mode: 0o600 });
      else await fs.rm(file, { force: true });
    } else app.setLoginItemSettings({ openAtLogin: enabled });
    login = enabled; refreshTray();
  }
  function refreshTray() {
    if (!tray) return;
    const summary = `${model.sessions} sessions · ${model.count} waiting for you`;
    tray.setToolTip(model.online ? `Piper — ${summary}` : 'Piper is not running');
    if (process.platform === 'darwin') tray.setTitle(model.count ? String(model.count) : '');
    else if (trayImage) tray.setImage(nativeImage.createFromBitmap(badge(trayImage.toBitmap(), model.count, theme().warn), { width: 24, height: 24 }));
    app.setBadgeCount(model.count);
    if (main && !main.isDestroyed()) main.setTitle(`Piper - ${model.sessions} sessions - ${model.count} waiting for you`);
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: model.online ? 'Piper is running' : 'Piper is not running', enabled: false },
      { label: summary, enabled: false }, { type: 'separator' },
      { label: 'WAITING FOR YOU', enabled: false },
      ...model.items.map(label => ({ label, click: show })), { type: 'separator' },
      { label: 'Open Piper', accelerator: 'CommandOrControl+O', click: show },
      { label: 'Open in browser', enabled: !!tokenURL, click: () => void external(tokenURL) }, { type: 'separator' },
      { label: 'Extensions…', enabled: model.online && !controller?.busy, click: () => { localWindow('extensions'); void controller.extensions().catch(e => { controller.wizard.fail(e); controller.wizard.state.retryAction = 'extensions'; sendState(); }); } },
      ...(!windowsStore ? [{ label: updateReady ? 'Update ready — quit to install' : updateURL ? 'Update available — Download' : 'Check for updates', click: () => void checkUpdates() }] : []),
      { label: 'Start at login', type: 'checkbox', checked: login, click: item => void autostart(item.checked).catch(() => {}) }, { type: 'separator' },
      { label: 'Quit Piper window', accelerator: 'CommandOrControl+Q', click: () => app.quit() },
      { label: 'Sessions keep running; the Piper service stays on.', enabled: false }
    ]));
  }
  async function checkUpdates() {
    if (!updatesEnabled(app.isPackaged, windowsStore)) return;
    try {
      if (updateURL) { await external(updateURL); return; }
      // electron-updater's default v tag cannot address desktop-v releases. Select the desktop release first.
      const response = await fetch('https://api.github.com/repos/Kamest/Piper/releases', { signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error('Release check failed');
      const releases = await response.json();
      const newer = tag => {
        const target = tag.slice('desktop-v'.length).split('.').map(Number), current = app.getVersion().split('.').map(Number);
        for (let i = 0; i < 3; i++) { if (target[i] !== current[i]) return target[i] > current[i]; }
        return false;
      };
      const release = releases.find(r => !r.draft && !r.prerelease && updateDecision(platform, signed, r.tag_name, r.html_url) !== 'ignore' && newer(r.tag_name));
      if (!release || release.tag_name.slice('desktop-v'.length) === app.getVersion()) return;
      const decision = updateDecision(platform, signed, release.tag_name, release.html_url);
      if (decision === 'download') { updateURL = release.html_url; refreshTray(); await external(updateURL); return; }
      const { autoUpdater } = require('electron-updater');
      autoUpdater.logger = null; autoUpdater.autoInstallOnAppQuit = true; autoUpdater.allowDowngrade = false;
      autoUpdater.channel = `desktop-${process.arch}`;
      autoUpdater.setFeedURL({ provider: 'generic', url: `https://github.com/Kamest/Piper/releases/download/${release.tag_name}/`, channel: `desktop-${process.arch}` });
      autoUpdater.once('update-downloaded', () => { updateReady = true; refreshTray(); });
      autoUpdater.on('error', () => {});
      await autoUpdater.checkForUpdates();
    } catch { await dialog.showMessageBox({ type: 'info', title: 'Piper', message: 'Updates could not be checked. Try again later.' }); }
  }
  const localSender = event => local && !local.isDestroyed() && event.sender === local.webContents && event.senderFrame === local.webContents.mainFrame && event.senderFrame.url === pathToFileURL(path.join(__dirname, 'ui/index.html')).href;
  const remoteSender = event => event.senderFrame === event.sender.mainFrame && navigation(event.senderFrame.url, origin) === 'internal';
  ipcMain.handle('setup:state', event => { if (!localSender(event)) throw new Error('Forbidden'); return { ...controller.wizard.state, busy: controller.busy, port }; });
  ipcMain.handle('setup:action', async (event, name, value) => {
    if (!localSender(event) || typeof name !== 'string' || (value !== undefined && typeof value !== 'string')) throw new Error('Forbidden');
    if (name === 'quit') { app.quit(); return; }
    if (name === 'copy') { clipboard.writeText(controller.wizard.state.log.join('\n')); return; }
    if (name === 'browser') { if (tokenURL) await external(tokenURL); return; }
    if (name === 'ensure') { try { await attach(await api.ensure()); } catch (e) { controller.wizard.state.error = redact(e.message); sendState(); } return; }
    if (name === 'retry' && controller.wizard.state.retryAction === 'connect') {
      try { await connect(); }
      catch (e) { controller.wizard.fail(e); sendState(); } return;
    }
    await controller.action(name, value);
  });
  ipcMain.on('notification:show', (event, data) => {
    if (!remoteSender(event) || !data || !/^\d{1,10}$/.test(data.id) || typeof data.title !== 'string' || typeof data.body !== 'string' || notifications.size > 100) return;
    const key = event.sender.id + ':' + data.id;
    const n = new Notification({ title: data.title.slice(0, 200), body: data.body.slice(0, 2000) });
    notifications.set(key, n);
    n.on('click', () => { focus(BrowserWindow.fromWebContents(event.sender)); event.sender.send('notification:event', { id: data.id, type: 'click' }); });
    n.on('close', () => { notifications.delete(key); if (!event.sender.isDestroyed()) event.sender.send('notification:event', { id: data.id, type: 'close' }); });
    n.show();
  });
  ipcMain.on('notification:close', (event, id) => { if (remoteSender(event)) { const key = event.sender.id + ':' + id; notifications.get(key)?.close(); notifications.delete(key); } });
  // Test harness can exercise the same native click callback without OS notification permissions.
  if (env.PIPER_DESKTOP_HOME) app.piperTest = { clickNotification: () => notifications.values().next().value?.emit('click'), showExtensions: () => { localWindow('extensions'); return controller.extensions(); }, tray: () => model };
  app.on('second-instance', show); app.on('activate', show);
  app.on('window-all-closed', () => {});
  app.on('before-quit', event => {
    if (controller?.busy) { event.preventDefault(); show(); return; }
    quitting = true; clearTimeout(pollTimer); clearTimeout(offlineTimer);
  });
  app.whenReady().then(async () => {
    await fs.mkdir(userData, { recursive: true });
    try { bounds = JSON.parse(await fs.readFile(path.join(userData, 'bounds.json'), 'utf8')); } catch { /* first launch */ }
    Menu.setApplicationMenu(Menu.buildFromTemplate([{ label: 'Piper', submenu: [{ label: 'Open Piper', click: show }, { label: 'Quit Piper window', accelerator: 'CommandOrControl+Q', click: () => app.quit() }] }, { role: 'editMenu' }, { role: 'viewMenu' }]));
    session.defaultSession.setPermissionRequestHandler((wc, permission, callback) => callback(navigation(wc.getURL(), origin) === 'internal' && ['notifications', 'clipboard-sanitized-write'].includes(permission)));
    session.defaultSession.setPermissionCheckHandler((wc, permission, requestingOrigin) => navigation(requestingOrigin, origin) === 'internal' && ['notifications', 'clipboard-sanitized-write'].includes(permission));
    const icon = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAGUlEQVQ4T2NkoBAwUqifYdQABtQwGDAAAAjQAAEFiENsAAAAAElFTkSuQmCC');
    // Use the product icon; the fallback only covers unpackaged tests without prepared resources.
    const productIcon = nativeImage.createFromPath(path.join(resources, 'tray.png'));
    trayImage = (productIcon.isEmpty() ? icon : productIcon).resize({ width: 24, height: 24 });
    tray = new Tray(trayImage);
    tray.on('click', show);
    login = platform === 'linux' ? await exists(path.join(env.XDG_CONFIG_HOME || path.join(nativeHome, '.config'), 'autostart/piper-desktop.desktop')) : app.getLoginItemSettings().openAtLogin;
    api = service({ port, run, fetch, platform, distro: () => controller.distro(), serviceEnv: wslEnv, locate,
      tokenFile: path.join(env.PIPER_HOME || path.join(nativeHome, '.piper'), 'gui-token'),
      readFile: async (file, encoding) => platform === 'win32'
        ? (await run(...wsl('cat', [(wslEnv.PIPER_HOME || home.linux + '/.piper') + '/gui-token'], controller.distro(), wslEnv))).stdout
        : fs.readFile(file, encoding) });
    controller = setupController({ env, platform, windowsStore, arch: process.arch, home, userData, resources, pythonSource, pythonVersion, runtimeDir, fs, run, fetch, executable: process.execPath,
      service: { locate, command: api.command, ready: async () => { await api.wait(); await attach(await api.url()); } }, changed: sendState, openExternal: external,
      showLog: async () => { const file = path.join(userData, 'setup.log'); if (!await exists(file)) await fs.writeFile(file, '', { mode: 0o600 }); await shell.openPath(file); } });
    refreshTray();
    nativeTheme.on('updated', () => { if (local && !local.isDestroyed()) { local.setBackgroundColor(theme().page); local.setTitleBarOverlay({ color: theme().card, symbolColor: theme().fg, height: 38 }); } refreshTray(); });
    try {
      await connect();
    } catch (e) { controller.wizard.fail(e); controller.wizard.state.retryAction = 'connect'; localWindow('error'); sendState(); }
  }).catch(() => { app.exit(1); });
}
