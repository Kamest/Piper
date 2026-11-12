import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../../desktop/src/main.cjs', import.meta.url), 'utf8');

function native() {
  const handlers = new Map(), calls = [];
  const context = { platform: 'darwin', remoteSender: event => event.trusted === true,
    path: createRequire(import.meta.url)('node:path'), process: { arch: 'x64' },
    controller: { distro: () => 'Selected-Test-Distro' }, run: async () => ({ stdout: '\\\\wsl.localhost\\Selected-Test-Distro\\home\\fixture\\notes.txt\n' }),
    randomUUID: () => 'opaque-drag-token',
    fsSync: { lstatSync: () => ({ dev: 1, ino: 2, mtimeMs: 3, size: 5, isSymbolicLink: () => false, isFile: () => true }), realpathSync: file => file },
    ipcMain: { handle: (name, fn) => handlers.set(name, fn), on: (name, fn) => handlers.set(name, fn) },
    api: { fileTarget: async path => { calls.push(['validate', path]); if (path === '/refused') throw Error('Refused'); return { path, kind: path === '/folder' ? 'dir' : 'file', openable: path !== '/run.command' }; } },
    shell: { showItemInFolder: path => calls.push(['reveal', path]), openPath: async path => { calls.push(['open', path]); return ''; } },
    app: { getFileIcon: async () => 'icon' },
    dialog: { showOpenDialog: async (_win, options) => { calls.push(['pick', options]); return { canceled: false, filePaths: ['/chosen'] }; } },
    BrowserWindow: { fromWebContents: () => ({ window: true }) }
  };
  vm.runInNewContext(source.slice(source.indexOf("  ipcMain.handle('file:pick-directory'"), source.indexOf("  ipcMain.handle('workspace:action'")), context);
  return { handlers, calls, context };
}

test('file bridge validates trusted top-level sender and target before any native side effect', async () => {
  const f = native(), run = f.handlers.get('file:action');
  await assert.rejects(run({}, 'reveal', '/doc'), /Forbidden/);
  await assert.rejects(run({ trusted: true }, 'execute', '/doc'), /Forbidden/);
  assert.deepEqual(f.calls, []);
  await assert.rejects(run({ trusted: true }, 'reveal', '/refused'), /Refused/);
  await assert.rejects(run({ trusted: true }, 'open', '/run.command'), /Forbidden/);
  assert.equal(f.calls.some(c => c[0] === 'open'), false);
  await run({ trusted: true }, 'reveal', '/run.command');
  await run({ trusted: true }, 'reveal', '/folder');
  assert.deepEqual(f.calls.slice(-3), [['reveal', '/run.command'], ['validate', '/folder'], ['reveal', '/folder']]);
});

test('native drag prepares asynchronously and starts synchronously with a single-use sender-bound token', async () => {
  const f = native(), prepare = f.handlers.get('file:prepare-drag'), drag = f.handlers.get('file:drag'), calls = [];
  const event = { trusted: true, senderFrame: { url: 'http://127.0.0.1/' }, sender: { isDestroyed: () => false, startDrag: value => calls.push(value.file) } };
  await assert.rejects(prepare({ ...event, trusted: false }, '/doc'), /Forbidden/);
  await assert.rejects(prepare(event, '/refused'), /Refused/);
  const token = await prepare(event, '/doc');
  assert.notEqual(token, '/doc');
  drag({ ...event, sender: { ...event.sender } }, token); assert.deepEqual(calls, []);
  drag(event, 'forged-token'); assert.deepEqual(calls, []);
  // Neither delayed validation nor icon lookup can run after the drag gesture begins.
  f.context.api.fileTarget = () => assert.fail('async lookup during drag');
  f.context.app.getFileIcon = () => assert.fail('async icon during drag');
  assert.equal(drag(event, token), undefined);
  assert.deepEqual(calls, ['/doc']);
  drag(event, token); assert.equal(calls.length, 1);
  drag({ ...event, sender: { ...event.sender } }, token); assert.equal(calls.length, 1);
});

test('drag preparation rejects a parent replaced with a symlink after backend validation', async () => {
  const f = native(), event = { trusted: true, senderFrame: { url: 'http://127.0.0.1/' }, sender: { isDestroyed: () => false } };
  f.context.app.getFileIcon = async () => { f.context.fsSync.realpathSync = () => '/private/runtime/state'; return 'icon'; };
  await assert.rejects(f.handlers.get('file:prepare-drag')(event, '/doc'), /File changed/);
});

test('prepared drags reject replaced files, symlinks, expired tokens, navigations and closed renderers', async () => {
  for (const change of ['replaced', 'symlink', 'expired', 'navigation', 'closed']) {
    const f = native(), calls = [], event = { trusted: true, senderFrame: { url: 'http://127.0.0.1/' }, sender: { isDestroyed: () => false, startDrag: value => calls.push(value) } };
    const token = await f.handlers.get('file:prepare-drag')(event, '/doc');
    if (change === 'replaced') f.context.fsSync.realpathSync = () => '/private/state';
    if (change === 'symlink') f.context.fsSync.lstatSync = () => ({ isSymbolicLink: () => true });
    if (change === 'expired') f.context.Date = { now: () => Date.now() + 31000 };
    if (change === 'navigation') event.senderFrame.url += 'other';
    if (change === 'closed') event.sender.isDestroyed = () => true;
    f.handlers.get('file:drag')(event, token);
    assert.deepEqual(calls, [], change);
  }
});

test('Windows file actions map only through the selected WSL distribution with literal argv', async () => {
  const f = native(), commands = [];
  f.context.platform = 'win32';
  f.context.run = async (command, args) => { commands.push([command, [...args]]); return { stdout: 'C:\\fixtures\\notes.txt\r\n' }; };
  const file = '/mnt/c/fixtures/a $(literal) \' quote.txt';
  await f.handlers.get('file:action')({ trusted: true }, 'reveal', file);
  assert.deepEqual(commands, [['wsl.exe', ['-d', 'Selected-Test-Distro', '--exec', 'wslpath', '-w', file]]]);
  assert.deepEqual(f.calls.at(-1), ['reveal', 'C:\\fixtures\\notes.txt']);
  for (const stdout of ['relative\n', '\\\\other-server\\share\\file.txt\n', '\\\\wsl.localhost\\Other-Distro\\home\\file.txt\n']) {
    f.context.run = async () => ({ stdout });
    await assert.rejects(f.handlers.get('file:action')({ trusted: true }, 'reveal', file), /conversion/);
  }
  f.context.run = async () => ({ stdout: '\\\\wsl.localhost\\Selected-Test-Distro\\home\\space name .txt \n' });
  await f.handlers.get('file:action')({ trusted: true }, 'reveal', file);
  assert.equal(f.calls.at(-1)[1], '\\\\wsl.localhost\\Selected-Test-Distro\\home\\space name .txt ');
  f.context.controller.distro = () => '';
  await assert.rejects(f.handlers.get('file:action')({ trusted: true }, 'reveal', file), /selected WSL/);
  f.context.controller.distro = () => 'Selected-Test-Distro'; f.context.process.arch = 'arm64';
  await assert.rejects(f.handlers.get('file:action')({ trusted: true }, 'reveal', file), /selected WSL/);
});

test('project directory picker is an explicit trusted action and cancel preserves the field', async () => {
  const f = native(), pick = f.handlers.get('file:pick-directory');
  await assert.rejects(pick({}, '/projects'), /Forbidden/);
  assert.equal(f.calls.length, 0);
  assert.equal(await pick({ trusted: true }, '/projects'), '/chosen');
  assert.deepEqual([...f.calls[0][1].properties], ['openDirectory', 'createDirectory']);
  assert.equal(f.calls[0][1].defaultPath, '/projects');
  f.context.dialog.showOpenDialog = async () => ({ canceled: true, filePaths: [] });
  assert.equal(await pick({ trusted: true }, '/projects'), null);
});

test('workspace ownership updates every renderer; bringing back closes only that native window', async () => {
  const handlers = new Map(), calls = [], updates = [];
  const main = { isDestroyed: () => false, webContents: { send: (event, slots) => updates.push(['main', event, [...slots]]) } };
  const extra = new Map();
  const second = { isDestroyed: () => false, webContents: { send: (event, slots) => updates.push(['second', event, [...slots]]) },
    close: () => { calls.push('close:2'); extra.delete(2); } };
  extra.set(2, second);
  const context = vm.createContext({ main, mainSlot: 1, extra, BrowserWindow: { fromWebContents: sender => sender === main.webContents ? main : sender === second.webContents ? second : null }, saveBounds() {}, saveWindows() {}, remoteSender: e => e.trusted === true,
    ipcMain: { handle: (name, fn) => handlers.set(name, fn) }, focus: win => calls.push(win === main ? 'focus:main' : 'focus:second'),
    openWindow: ({ slot }) => calls.push('open:'+slot) });
  vm.runInContext(source.slice(source.indexOf('  function workspaceOwner('), source.indexOf("  ipcMain.on('restart:visible'")), context);
  const run = handlers.get('workspace:action'), event = { trusted: true, sender: main.webContents };
  assert.deepEqual([...await run(event, 'state', 1)], [2]);
  assert.deepEqual([...await run({ trusted: true, sender: second.webContents }, 'state', 2)], [1]);
  await assert.rejects(async () => run({}, 'bring', 2), /Forbidden/);
  context.announceWorkspaces();
  assert.deepEqual(updates, [['main', 'workspace:changed', [2]], ['second', 'workspace:changed', [1]]]);
  await run(event, 'open', 2); assert.deepEqual(calls, ['focus:second']);
  assert.deepEqual([...await run(event, 'bring', 2)], []);
  assert.deepEqual(calls, ['focus:second', 'close:2']);
  assert.equal(extra.size, 0);
});


test('extra window 2 claims 3 atomically, survives renderer reload, focuses and brings back by current ownership', async () => {
  const handlers = new Map(), calls = [];
  const main = { isDestroyed: () => false, webContents: { send() {} } };
  const extra = new Map();
  const second = { isDestroyed: () => false, webContents: { send() {} }, close() {
    calls.push('second:close'); for (const [key, win] of extra) if (win === this) extra.delete(key);
  } };
  extra.set(2, second);
  const context = vm.createContext({ main, mainSlot: 1, extra, remoteSender: event => event.trusted === true,
    BrowserWindow: { fromWebContents: sender => sender === main.webContents ? main : sender === second.webContents ? second : null },
    ipcMain: { handle: (name, fn) => handlers.set(name, fn) },
    saveBounds: () => calls.push('bounds'), saveWindows: () => calls.push('save'),
    focus: win => calls.push(win === main ? 'focus:main' : 'focus:second'), openWindow: () => assert.fail('must reuse existing owner') });
  vm.runInContext(source.slice(source.indexOf('  function workspaceOwner('), source.indexOf("  ipcMain.on('restart:visible'")), context);
  const run = handlers.get('workspace:action'), from = sender => ({ trusted: true, sender });
  await run(from(second.webContents), 'claim', 3);
  assert.equal(extra.has(2), false); assert.equal(extra.get(3), second);
  assert.deepEqual([...await run(from(main.webContents), 'state', 1)], [3]);
  // Reload gets ownership from the host, never a stale preload slot captured at startup.
  assert.deepEqual([...await run(from(second.webContents), 'state', 3)], [1]);
  await run(from(main.webContents), 'open', 3);
  assert.ok(calls.includes('focus:second'));
  await assert.rejects(async () => run(from(main.webContents), 'claim', 3), /already open/);
  assert.equal(extra.get(3), second);
  await run(from(main.webContents), 'bring', 3);
  assert.equal(extra.size, 0); assert.equal(calls.filter(c => c === 'second:close').length, 1);
  await run(from(main.webContents), 'claim', 4);
  assert.equal(context.mainSlot, 4);
  assert.deepEqual([...await run(from(second.webContents), 'state', 3)], [4]);
  await assert.rejects(async () => run(from(second.webContents), 'close', 4), /main window/);
});


test('restoring native windows excludes the current main workspace and retains workspace one as an extra', () => {
  const { restorable } = createRequire(import.meta.url)('../../desktop/src/lib/windows.cjs');
  const rows = [{ slot: 1, bounds: { width: 900 } }, { slot: 3 }, { slot: 4 }, { slot: 4 }, { slot: 100 }];
  assert.deepEqual(restorable(rows, 3).map(row => row.slot), [1, 4]);
  assert.deepEqual(restorable(rows).map(row => row.slot), [3, 4]);
});

test('a native file-menu lookup cannot open actions against a project selected while it was loading', async () => {
  const files = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  const start = files.indexOf('async function treeMenu(');
  const code = files.slice(start, files.indexOf('// ---------------------------------------------------------------- right: tabs', start));
  let resolve, menus = 0;
  const context = vm.createContext({ project: 'old', absolutePath: file => '/old/' + file, parentOf: () => '.',
    nativeItems: () => new Promise(done => { resolve = done; }), menu: () => { menus++; } });
  vm.runInContext(code, context);
  const pending = context.treeMenu({ preventDefault() {}, clientX: 1, clientY: 1 }, 'notes.md', false);
  context.project = 'new'; resolve([]); await pending;
  assert.equal(menus, 0);
});

test('a non-openable application bundle is never launched merely because it is a directory', async () => {
  const f = native();
  f.context.api.fileTarget = async () => ({ path: '/Demo.app', kind: 'dir', openable: false });
  await assert.rejects(f.handlers.get('file:action')({ trusted: true }, 'open', '/Demo.app'), /Forbidden/);
  assert.equal(f.calls.some(call => call[0] === 'open'), false);
});

test('native launch stays forbidden even with forged openable metadata for documents and active types', async () => {
  const f = native(), run = f.handlers.get('file:action');
  for (const file of ['/notes.txt', '/guide.pdf', '/run.terminal', '/run.workflow', '/run.pkg', '/alias.txt', '/folder']) {
    f.context.api.fileTarget = async () => ({ path: file, kind: file === '/folder' ? 'dir' : 'file', openable: true, link: file === '/alias.txt' });
    await assert.rejects(run({ trusted: true }, 'open', file), /Forbidden/);
    await run({ trusted: true }, 'reveal', file);
  }
  assert.equal(f.calls.some(call => call[0] === 'open'), false);
  assert.equal(f.calls.filter(call => call[0] === 'reveal').length, 7);
});

test('native menus expose reveal without a default-app launch even for old openable metadata', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/nativefiles.js', import.meta.url), 'utf8');
  const code = compiled.slice(compiled.indexOf('export function nativeFileMenu('), compiled.indexOf('export function dragFile(')).replace('export ', '');
  const calls = [], context = vm.createContext({ nativeFiles: () => true,
    window: { __piperNative: { platform: 'darwin', file: async (action, file) => calls.push([action, file]) } } });
  vm.runInContext(code, context);
  for (const directory of [false, true]) for (const openable of [undefined, false, true]) {
    const entries = context.nativeFileMenu('/notes.txt', directory, () => {}, '', openable).filter(item => item?.label);
    assert.deepEqual([...entries].map(item => item.label), ['Reveal in Finder']);
    entries[0].run();
  }
  assert.equal(calls.length, 6);
  assert.ok(calls.every(([action, file]) => action === 'reveal' && file === '/notes.txt'));
});


test('file drag prepares on hover, preserves a ready token through the press and consumes it synchronously', async () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/nativefiles.js', import.meta.url), 'utf8');
  const code = compiled.slice(compiled.indexOf('export function dragFile(')).replace('export ', '');
  const handlers = new Map(), calls = [], el = { dataset: {}, title: '', addEventListener: (name, fn) => handlers.set(name, fn) };
  let finish;
  const context = vm.createContext({ refHost: () => '', nativeFiles: () => true,
    window: { __piperNative: { prepareDrag: path => { calls.push(['prepare', path]); return new Promise(resolve => { finish = resolve; }); }, drag: token => calls.push(['drag', token]) } } });
  vm.runInContext(code, context);
  context.dragFile(el, '/worktree/notes.md');
  assert.deepEqual(calls, [], 'rendering a file must not make native API calls');
  handlers.get('pointerenter')();
  assert.deepEqual(calls, [['prepare', '/worktree/notes.md']]);
  finish('opaque-token'); await new Promise(resolve => setImmediate(resolve));
  assert.equal(el.dataset.nativeDrag, 'ready');
  handlers.get('pointerdown')();
  assert.equal(calls.length, 1, 'a press must retain the ready token');
  let prevented = false;
  handlers.get('dragstart')({ preventDefault: () => { prevented = true; } });
  assert.equal(prevented, true); assert.deepEqual(calls.at(-1), ['drag', 'opaque-token']);
  assert.equal(el.dataset.nativeDrag, undefined);
  const text = [];
  handlers.get('dragstart')({ dataTransfer: { setData: (...args) => text.push(args) } });
  assert.deepEqual(text, [['text/plain', '/worktree/notes.md']]);
});


test('the isolated preload sends native drag only once during a trusted dragstart dispatch', async () => {
  const preload = readFileSync(new URL('../../desktop/src/preload.cjs', import.meta.url), 'utf8');
  const exposed = {}, events = new Map(), timers = [], calls = [];
  const electron = { contextBridge: { exposeInMainWorld: (name, bridge) => { exposed[name] = bridge; }, executeInMainWorld() {} },
    ipcRenderer: { send: (...args) => calls.push(args) } };
  vm.runInNewContext(preload, { require: () => electron, location: { protocol: 'http:', hostname: '127.0.0.1' },
    process: { platform: 'darwin', arch: 'arm64' }, setTimeout: callback => timers.push(callback), window: { addEventListener: (name, fn) => events.set(name, fn) } });
  const drag = exposed.__piperNative.drag;
  drag('token'); assert.deepEqual(calls, []);
  events.get('dragstart')({ isTrusted: false, eventPhase: 3 }); drag('token'); assert.deepEqual(calls, []);
  const event = { isTrusted: true, eventPhase: 1 };
  events.get('dragstart')(event);
  await Promise.resolve(); // capture→microtask checkpoint→renderer bubble listener
  event.eventPhase = 3; drag('token'); drag('token');
  assert.deepEqual(calls, [['file:drag', 'token']]);
  const ended = { isTrusted: true, eventPhase: 1 }; events.get('dragstart')(ended);
  ended.eventPhase = 0; drag('token'); assert.equal(calls.length, 1, 'dispatch ended before task timer');
  for (const timer of timers) timer(); drag('token'); assert.equal(calls.length, 1);
});


test('Windows native files require an explicit current-desktop capability; legacy hosts stay unsupported', () => {
  const compiled = readFileSync(new URL('../../piper/web/dist/nativefiles.js', import.meta.url), 'utf8');
  const code = compiled.slice(compiled.indexOf('export const nativeFiles'), compiled.indexOf('export function nativeFileMenu')).replace('export const nativeFiles', 'globalThis.nativeFiles');
  const window = {}, context = vm.createContext({ window, refHost: project => project.startsWith('remote:') });
  vm.runInContext(code, context);
  for (const platform of ['darwin', 'linux', 'win32']) {
    window.__piperNative = { platform };
    assert.equal(context.nativeFiles(), platform !== 'win32');
    window.__piperNative.fileSupport = true;
    assert.equal(context.nativeFiles(), true);
    assert.equal(context.nativeFiles('remote:acme/shop'), false);
    window.__piperNative.fileSupport = false;
    assert.equal(context.nativeFiles(), false);
  }
});

test('file context menus open without waiting for piperd (no /api/fs/target round trip)', () => {
  // A restarting or slow daemon left right-click with no menu at all: the menu awaited an unused lookup.
  const files = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  const start = files.indexOf('async function nativeItems(');
  assert.ok(start > 0, 'nativeItems is present');
  const body = files.slice(start, files.indexOf('\n    }', start));
  assert.doesNotMatch(body, /await|fs\/target|call\(/);
});
