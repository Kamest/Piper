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
    path: { isAbsolute: value => value.startsWith('/') },
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
  await assert.rejects(run({ trusted: true }, 'open', '/refused'), /Refused/);
  await assert.rejects(run({ trusted: true }, 'open', '/run.command'), /Executable/);
  assert.equal(f.calls.some(c => c[0] === 'open'), false);
  await run({ trusted: true }, 'reveal', '/run.command');
  await run({ trusted: true }, 'open', '/folder');
  assert.deepEqual(f.calls.slice(-3), [['reveal', '/run.command'], ['validate', '/folder'], ['open', '/folder']]);
});

test('native drag is validated and does nothing after its renderer closes', async () => {
  const f = native(), drag = f.handlers.get('file:drag');
  const event = { trusted: true, sender: { isDestroyed: () => true, startDrag: () => assert.fail('closed renderer') } };
  await drag(event, '/doc');
  const calls = [];
  event.sender = { isDestroyed: () => false, startDrag: value => calls.push(value.file) };
  await drag(event, '/refused'); assert.deepEqual(calls, []);
  await drag(event, '/doc'); assert.deepEqual(calls, ['/doc']);
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
  const context = vm.createContext({ project: 'old', info: () => ({ path: '/old' }), parentOf: () => '.',
    nativeItems: () => new Promise(done => { resolve = done; }), menu: () => { menus++; } });
  vm.runInContext(code, context);
  const pending = context.treeMenu({ preventDefault() {}, clientX: 1, clientY: 1 }, 'notes.md', false);
  context.project = 'new'; resolve([]); await pending;
  assert.equal(menus, 0);
});

test('a non-openable application bundle is never launched merely because it is a directory', async () => {
  const f = native();
  f.context.api.fileTarget = async () => ({ path: '/Demo.app', kind: 'dir', openable: false });
  await assert.rejects(f.handlers.get('file:action')({ trusted: true }, 'open', '/Demo.app'), /Executable/);
  assert.equal(f.calls.some(call => call[0] === 'open'), false);
});
