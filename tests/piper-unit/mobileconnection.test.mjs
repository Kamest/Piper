import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { configureMobileComputers, mobileComputers, mobileLimited, installTransport, notifyTransportReady, notifyTransportUnavailable, onTransportReady, reportTransportState, transportRevision } from '../../piper/web/dist/transport.js';

const main = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const polling = main.slice(main.indexOf('let polledOnce = false;'), main.indexOf('/** a file only the session'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function pollHarness(mobile = true) {
  const timers = new Map(), reports = [], banners = [], connections = [];
  let revision = 1, ready, settle, calls = 0, serial = 0;
  const context = {
    Promise, Error, ApiError: class extends Error {}, POLL_MS: 1500, etag: 'old', remoteChanged() {},
    document: { hidden: false, getElementById() { return { remove() {} }; } },
    window: { setTimeout(fn, ms) { const id = ++serial; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } },
    api: { state: () => { calls++; return new Promise((resolve, reject) => { settle = { resolve, reject }; }); } },
    transportRevision: () => revision,
    reportTransportState(ok, owner) { if (mobile && revision === owner) reports.push(ok); return mobile; },
    onTransportReady(fn) { ready = fn; }, refreshRemoteHosts() {}, banner: text => banners.push(text),
    connectionLost: () => connections.push('lost'), connectionRestored: () => connections.push('restored'),
    restarting: () => false, restartAnswered: () => false, restartFailed() {}, extensionRequests() {}, toast() {},   // 9.168
    updateNotice() {}, localState: {},   // 9.171
  };
  vm.runInNewContext(polling, context);
  return { context, timers, reports, banners, connections, settle: () => settle, calls: () => calls,
    ready() { revision++; ready(); }, retire() { revision++; } };
}

test('verified reconnect discards the cancelled old poll and immediately polls once, clearing mobile status', async () => {
  const h = pollHarness();
  const old = h.context.poll();
  h.retire(); h.ready(); h.ready();
  h.settle().reject(new Error('old connection reset'));
  await old; await tick();
  assert.equal(h.calls(), 2, 'ready event cluster must not overlap state requests');
  assert.deepEqual(h.banners, []);
  assert.deepEqual(h.reports, [], 'a stale rejection must not flag the new connection offline');
  h.settle().resolve({ data: null });
  await tick();
  assert.deepEqual(h.reports, [true]);
  assert.equal(h.timers.size, 1, 'exactly one polling loop survives reconnect');
});

test('a stale successful poll cannot dismiss a newer outage', async () => {
  const h = pollHarness(); const pending = h.context.poll();
  h.retire(); h.settle().resolve({ data: null }); await pending;
  assert.deepEqual(h.reports, []);
});

test('current mobile failures use mobile status; desktop failures enter the shared delayed connection diagnostics', async () => {
  for (const mobile of [true, false]) {
    const h = pollHarness(mobile), pending = h.context.poll();
    h.settle().reject(new Error('fixture timeout')); await pending;
    assert.deepEqual(h.reports, mobile ? [false] : []);
    assert.deepEqual(h.banners, [], 'no immediate red banner or raw transport error');
    assert.deepEqual(h.connections, mobile ? [] : ['lost']);
  }
});

test('transport revision rejects results from both a retired and superseded connection', () => {
  const reports = [];
  installTransport({ stateChanged: value => reports.push(value) });
  const old = transportRevision();
  notifyTransportUnavailable();
  assert.equal(reportTransportState(true, old), true);
  const waiting = transportRevision();
  const unsubscribe = onTransportReady(() => {});
  notifyTransportReady();
  reportTransportState(false, waiting);
  assert.deepEqual(reports, []);
  reportTransportState(true, transportRevision());
  assert.deepEqual(reports, [true]);
  unsubscribe();
});


// The real compiled mobile UI and Settings run with a small DOM stand-in, not source assertions.
class MobileNode {
  constructor(tag = '', text = '') { this.tag = tag; this.textContent = text; this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.className = ''; this.classList = { add: (...names) => { this.className += ' ' + names.join(' '); } }; }
  setAttribute(key, value) { this.attrs[key] = value; if (key === 'data-state') this.dataset.state = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(key, value) { this.listeners[key] = value; }
  append(...nodes) { for (const node of nodes.flat()) { if (node == null || node === false) continue; const value = typeof node === 'string' ? new MobileNode('', node) : node; value.parent = this; this.children.push(value); } }
  prepend(node) { node.parent = this; this.children.unshift(node); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  focus() {}
  text() { return this.textContent + this.children.map(node => node.text()).join(' '); }
  all() { return [this, ...this.children.flatMap(node => node.all())]; }
  querySelector(selector) { return this.all().find(node => selector.split(', ').includes(node.tag)); }
}
function mobileDOM() {
  const timers = new Map(); let serial = 0;
  const body = new MobileNode('body'), app = new MobileNode('main'); app.attrs.id = 'app'; body.append(app);
  const h = (tag, attrs = {}, ...children) => {
    const node = new MobileNode(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (typeof value === 'function') node.addEventListener(key.replace(/^on/, ''), value);
      else if (key === 'class') node.className = value;
      else if (value !== undefined && value !== false) node.setAttribute(key, value);
    }
    node.append(...children); return node;
  };
  const context = { h, fi: () => h('i'), markBidi: value => value,
    navigator: { userAgent: '', platform: '', maxTouchPoints: 0 },
    document: { hidden: false, body, getElementById: id => body.all().find(node => node.attrs.id === id), createTextNode: value => new MobileNode('', value), addEventListener() {} },
    window: { clearInterval() {}, setInterval() { return 1; }, setTimeout(fn, ms) { const id = ++serial; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } },
  };
  return { context, body, app, timers, fire(ms) { const [id, timer] = [...timers].find(([, value]) => value.ms === ms); timers.delete(id); timer.fn(); } };
}
const mobileUI = readFileSync(new URL('../../piper/web/dist/mobileui.js', import.meta.url), 'utf8').replace(/^import .*;\n/gm, '').replace(/^export /gm, '');

test('backup dialog and falling-back stage use design facts/actions without exposing the invitation secret', () => {
  const f = mobileDOM(); vm.runInNewContext(mobileUI, f.context);
  let paired = 0, cancelled = 0;
  f.context.showMobileState('pair-backup', 'Backup', { primary: 'Primary', replacement: 'Old backup', number: 'Verified', pair: () => paired++, cancel: () => cancelled++ });
  assert.match(f.body.text(), /Pair Backup as backup/);
  assert.match(f.body.text(), /Primary · paired/);
  assert.match(f.body.text(), /Pairing code Verified/);
  assert.match(f.body.text(), /Full access to Backup/);
  assert.match(f.body.text(), /replaces Old backup/);
  f.body.all().find(node => node.tag === 'button' && node.text() === 'Pair as backup').listeners.click();
  assert.equal(paired, 1);
  f.context.showMobileState('falling-back', 'Backup', { primary: 'Primary', cancel: () => cancelled++ });
  assert.match(f.body.text(), /Primary unreachable/);
  assert.match(f.body.text(), /Connecting to Backup \(backup\)…/);
  f.body.all().find(node => node.tag === 'button').listeners.click();
  assert.equal(cancelled, 1);
  assert.equal(f.body.all().some(node => node.attrs.id === 'mobile-offline'), false);
});

test('backup scanner says what to scan and Cancel or Back return to the running app', () => {
  const f = mobileDOM(); vm.runInNewContext(mobileUI, f.context);
  f.context.startQRScanner = () => () => {};
  let cancelled = 0;
  const actions = { backup: true, cancel: () => cancelled++, validLink: () => false, openLink: async () => null };
  f.context.showMobileState('scan', 'Piper', actions);
  assert.match(f.body.text(), /Scan backup code/);
  assert.match(f.body.text(), /Your current computer stays primary/);
  f.body.all().find(node => node.tag === 'button' && node.text() === 'Cancel').listeners.click();
  assert.equal(cancelled, 1);
  f.context.showMobileState('invalid-qr', 'Piper', actions);
  f.body.all().find(node => node.tag === 'button' && node.text() === 'Back').listeners.click();
  assert.equal(cancelled, 2);
});

test('offline action exists only with a backup, switches explicitly, and clears on recovery', () => {
  for (const hasBackup of [false, true]) {
    const f = mobileDOM(); vm.runInNewContext(mobileUI, f.context);
    let switched = 0;
    f.context.mobileOffline(true, 'Primary', async () => {}, hasBackup ? { computer: 'Backup', use: () => switched++ } : undefined);
    f.fire(2000); f.fire(13000);
    const buttons = f.body.all().filter(node => node.tag === 'button');
    const backup = buttons.find(node => node.text() === 'Use Backup (backup)');
    assert.equal(!!backup, hasBackup);
    assert.equal(switched, 0);
    backup?.listeners.click(); assert.equal(switched, hasBackup ? 1 : 0);
    f.context.mobileOffline(false);
    assert.equal(f.body.all().some(node => node.attrs.id === 'mobile-offline'), false);
  }
});

test('backup strip becomes a recovery offer while keeping the app interactive and never switching itself', () => {
  const f = mobileDOM(); vm.runInNewContext(mobileUI, f.context);
  let switched = 0;
  f.context.showMobileBackup('Primary', 'Backup', false, () => switched++);
  assert.match(f.body.text(), /Primary is offline – on Backup \(backup, limited\)/);
  assert.equal(f.body.all().some(node => node.tag === 'button'), false);
  f.context.showMobileBackup('Primary', 'Backup', true, () => switched++);
  assert.match(f.body.text(), /Primary is back/);
  assert.equal(switched, 0);
  assert.notEqual(f.app.inert, true);
  f.body.all().find(node => node.tag === 'button').listeners.click(); assert.equal(switched, 1);
});

test('limited backup mode blocks Settings/Setup routes and removes their rail and palette entries', () => {
  configureMobileComputers({ primary: 'Primary', fallback: 'Backup', limited: true, connected: true, forgetBackup() {} });
  assert.equal(mobileComputers().primary, 'Primary');
  assert.equal(mobileLimited(), true);
  const start = main.indexOf('function viewOn('), end = main.indexOf('/** a feature module', start);
  const context = { mobileLimited, active: '', localState: {}, sshOnly: () => false, GLOBAL_VIEWS: new Set(), VIEW_MODULE: {}, on: () => true };
  vm.runInNewContext(main.slice(start, end), context);
  assert.equal(context.viewOn('settings'), false);
  assert.equal(context.viewOn('setup'), false);
  assert.equal(context.viewOn('term'), true);
  assert.equal(context.viewOn('convo'), true);
  assert.match(main, /mobileLimited\(\) \? null : item\('settings'/);
  assert.match(main, /if \(!mobileLimited\(\)\)\s*go\(\{ label: 'Settings'/);
  configureMobileComputers({ ...mobileComputers(), limited: false });
  assert.equal(context.viewOn('settings'), true, 'the primary and desktop retain Settings');
});

const mobileSettings = readFileSync(new URL('../../piper/web/dist/mobilesettings.js', import.meta.url), 'utf8');
test('phone Computers shows both names, Forget backup redraws the empty instructions, limited mode exposes no settings', async () => {
  const f = mobileDOM(); let forgotten = 0, requests = 0;
  let scans = 0;
  let computers = { primary: 'Primary', fallback: 'Backup', connected: true, limited: false,
    forgetBackup() { forgotten++; computers = { ...computers, fallback: null }; }, scanBackup() { scans++; } };
  Object.assign(f.context, { mobileComputers: () => computers, mobileLimited: () => computers.limited, clear: node => node.replaceChildren(),
    createNotificationPanel: () => ({ el: new MobileNode('div'), show() {} }), mobilePushClient: () => true,
    request: async () => { requests++; throw new Error('phone must not request local-only admin API'); } });
  vm.runInNewContext(mobileSettings.slice(mobileSettings.indexOf('export function createMobileSettings')).replace('export ', ''), f.context);
  const settings = f.context.createMobileSettings({}); await settings.refresh();
  assert.match(settings.el.text(), /Computers/);
  assert.match(settings.el.text(), /Primary · connected/);
  assert.match(settings.el.text(), /Backup · used when Primary is offline/);
  assert.match(settings.el.text(), /Each computer notifies about its own sessions while it is online: Primary and Backup/);
  settings.el.all().find(node => node.tag === 'button' && node.text() === 'Forget backup').listeners.click();
  assert.equal(forgotten, 1);
  assert.match(settings.el.text(), /No backup/);
  assert.match(settings.el.text(), /open Pair phone\. Then scan its code here/);
  settings.el.all().find(node => node.tag === 'button' && node.text() === 'Scan backup code').listeners.click();
  assert.equal(scans, 1, 'a paired phone scans backup codes inside the app, not with the Camera app');
  assert.equal(requests, 0);
  computers = { ...computers, limited: true };
  const limited = f.context.createMobileSettings({}); await limited.refresh();
  assert.equal(limited.el.text(), '');
  assert.equal(requests, 0);
});
