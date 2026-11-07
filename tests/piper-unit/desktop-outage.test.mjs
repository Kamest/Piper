import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../desktop/src/main.cjs', import.meta.url), 'utf8');
// Exercise the production window transition callbacks without starting native windows on the desktop.
const callbacks = source.slice(source.indexOf('  async function offline()'), source.indexOf('  const localState ='));
function fixture() {
  const events = [], timers = new Map(); let changed, serial = 0, polls = 0;
  const window = label => ({ isDestroyed: () => false, isVisible: () => label === 'local' && context.localVisible,
    hide: () => events.push(label + ':hide'), showInactive: () => events.push(label + ':show'),
    webContents: { reload: () => events.push(label + ':reload') } });
  const context = vm.createContext({ quitting: false, connected: true, model: {}, restartState: null, restartHidden: false,
    localVisible: false, localLayout: 'restart', offlineTimer: null,
    trayModel: () => ({}), refreshTray() {}, sendState() {}, poll: () => { polls++; }, focus: win => events.push(win === context.main ? 'main:focus' : 'local:focus'),
    localWindow: screen => { context.localVisible = true; events.push('local:' + screen); },
    setTimeout: (fn, ms) => { timers.set(++serial, { fn, ms }); return serial; }, clearTimeout: id => timers.delete(id),
    api: { observeOutage: options => { changed = options.changed; }, stopOutage: () => events.push('stop') },
    extra: new Map(), failedRenderers: new Set(), restartViews: new Set() });
  context.main = window('main'); context.local = window('local'); context.extra.set(2, window('extra'));
  vm.runInContext(callbacks, context);
  const update = state => changed({ kind: 'outage', checking: true, reachable: false, ...state });
  return { context, events, timers, update, polls: () => polls };
}

test('brief service interruption never hides windows, opens diagnostics, navigates or takes focus', async () => {
  const f = fixture(); await f.context.offline(); f.update({});
  assert.equal(f.context.connected, false);
  assert.deepEqual(f.events, []);
  assert.equal([...f.timers.values()][0].ms, 5000);
  f.update({ reachable: true, phase: 'returned' });
  assert.equal(f.context.connected, true);
  assert.equal(f.context.restartState, null);
  assert.equal(f.timers.size, 0);
  assert.deepEqual(f.events, ['stop']);
  assert.equal(f.polls(), 1, 'recovery must refresh the tray and service state without a thirty-second stale offline status');
});

test('sustained outage preserves workspace windows and shows verified recovery before returning to them', async () => {
  const f = fixture(); await f.context.offline(); f.update({});
  [...f.timers.values()][0].fn();
  assert.deepEqual(f.events, ['local:restart']);
  f.update({ reachable: true, phase: 'returned' });
  assert.equal(f.context.restartState.reachable, true);
  assert.deepEqual(f.events, ['local:restart', 'stop']);
  await f.context.finishOutage();
  assert.deepEqual(f.events.slice(2), ['stop', 'local:hide', 'main:focus', 'extra:show']);
});

test('a live renderer-owned diagnostic overlay suppresses the native fallback window', async () => {
  const f = fixture(); await f.context.offline(); f.update({});
  f.context.restartViews.add(1);
  [...f.timers.values()][0].fn();
  assert.deepEqual(f.events, []);
});

test('repeated failures keep the original grace timer; only crashed renderers reload after recovery', async () => {
  const f = fixture(); await f.context.offline(); f.update({});
  await f.context.offline();
  assert.equal(f.timers.size, 1);
  f.context.failedRenderers.add(f.context.main);
  f.update({ reachable: true, phase: 'returned' });
  assert.deepEqual(f.events, ['stop', 'main:reload']);
  assert.equal(f.context.failedRenderers.size, 0);
});

test('renderer actions accept only an explicit start or log request from the trusted GUI', async () => {
  let handler, starts = 0; const actions = [];
  const code = source.slice(source.indexOf("  ipcMain.handle('restart:action'"), source.indexOf("  ipcMain.handle('setup:state'"));
  vm.runInNewContext(code, { ipcMain: { handle: (_name, fn) => { handler = fn; } },
    remoteSender: event => event.trusted === true,
    startFromOutage: async () => { starts++; }, controller: { action: async name => actions.push(name) } });
  await assert.rejects(handler({ trusted: false }, 'ensure'), /Forbidden/);
  await assert.rejects(handler({ trusted: true }, 'restart'), /Forbidden/);
  await assert.rejects(handler({ trusted: true }, 'quit'), /Forbidden/);
  assert.equal(starts, 0);
  await handler({ trusted: true }, 'log'); assert.deepEqual(actions, ['log']);
  await handler({ trusted: true }, 'ensure'); assert.equal(starts, 1);
});

test('a second outage is detected while the previous recovered native card is still open', async () => {
  const code = source.slice(source.indexOf('  async function poll()'), source.indexOf('  /** Show the main window'));
  let outages = 0;
  const context = vm.createContext({ pollTimer: null, quitting: false, model: {}, notifyMemo: null,
    restartState: { kind: 'outage', reachable: true }, holding: false, controller: { busy: false }, restartViews: new Set(),
    api: { state: async () => { throw Error('offline again'); } }, trayModel: () => ({ poll: 30000 }),
    offline: async () => { outages++; }, clearTimeout() {}, setTimeout() {}, refreshTray() {}, sendAbout() {} });
  vm.runInContext(code, context); await context.poll();
  assert.equal(context.restartState, null); assert.equal(outages, 1);
});

test('simultaneous Start Piper clicks run one start and retain a safe retryable failure', async () => {
  let rejectStart, starts = 0;
  const code = source.slice(source.indexOf('  async function startFromOutage()'), source.indexOf('  const localState ='));
  const context = vm.createContext({ startingOutage: false, restartState: { kind: 'outage', phase: 'timeout' },
    api: { stopOutage() {} }, sendState() {},
    start: () => { starts++; return new Promise((_resolve, reject) => { rejectStart = reject; }); } });
  vm.runInContext(code, context);
  const first = context.startFromOutage();
  await context.startFromOutage();
  assert.equal(starts, 1); assert.equal(context.restartState.starting, true);
  rejectStart(Error('private command output'));
  await assert.rejects(first, /Show the log/);
  assert.equal(context.startingOutage, false); assert.equal(context.restartState.starting, false);
  assert.equal(context.restartState.checking, false);
  assert.doesNotMatch(context.restartState.finding, /private command output/);
});


test('one crashed secondary renderer recovers locally without disconnecting or reloading other windows', () => {
  const code = source.slice(source.indexOf('  function rendererFailed('), source.indexOf('  function localWindow('));
  const timers = [], events = [];
  const context = vm.createContext({ quitting: false, connected: true, failedRenderers: new Set(),
    setTimeout: fn => timers.push(fn), offline: () => events.push('offline') });
  const win = name => ({ isDestroyed: () => false, webContents: { reload: () => events.push(name + ':reload') } });
  const main = win('main'), extra = win('extra');
  vm.runInContext(code, context); context.rendererFailed(extra);
  assert.equal(context.connected, true); assert.deepEqual(events, []);
  timers.shift()(); assert.deepEqual(events, ['extra:reload']);
  context.connected = false; context.rendererFailed(main);
  assert.equal(timers.length, 0); assert.equal(context.failedRenderers.has(main), true);
  assert.deepEqual(events, ['extra:reload']);
});

test('a failed window navigation probes Piper before declaring a service outage and ignores subframes', async () => {
  const code = source.slice(source.indexOf('  async function windowLoadFailed('), source.indexOf('  function localWindow('));
  const events = [], context = vm.createContext({ quitting: false, connected: true, failedRenderers: new Set(),
    api: { probe: async () => true }, rendererFailed: () => events.push('recover'), offline: async () => events.push('offline') });
  const win = { isDestroyed: () => false };
  assert.ok(code.includes('async function windowLoadFailed'), 'production load-failure callback is required');
  vm.runInContext(code, context);
  await context.windowLoadFailed(win, -3, true);
  await context.windowLoadFailed(win, -105, false);
  assert.deepEqual(events, []);
  await context.windowLoadFailed(win, -105, true);
  assert.deepEqual(events, ['recover']);
  context.api.probe = async () => false;
  await context.windowLoadFailed(win, -102, true);
  assert.deepEqual(events, ['recover', 'offline']);
  context.quitting = true;
  await context.windowLoadFailed(win, -102, true);
  assert.deepEqual(events, ['recover', 'offline']);
});
