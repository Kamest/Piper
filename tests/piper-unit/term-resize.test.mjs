// 9.55: the PTY has one size, the last GUI window to send one wins; this window takes it back when the user returns.
// Loads the real compiled term.js; only xterm's addons, the socket and the browser globals are stubbed.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const renderer = `
      export class Terminal {
        cols = 80; rows = 24; options = {}; modes = { mouseTrackingMode: "vt200" };
        unicode = {}; parser = { registerOscHandler() {} };
        loadAddon(a) { a.term = this; if (a.activate) this.webLinks = a; } registerLinkProvider() {} attachCustomKeyEventHandler() {}
        onData() {} onBinary() {} dispose() {} focus() { this.focused = true; } reset() {}
        resize(c, r) { this.cols = c; this.rows = r; }
        write(_d, cb) { cb?.(); }
      }
      export class FitAddon { fit() { this.term.cols = globalThis.PANE.cols; this.term.rows = globalThis.PANE.rows; } }
      export class WebglAddon {} export class Unicode11Addon {} export class WebLinksAddon { constructor(activate) { this.activate = activate; } } export class SearchAddon {}
    `;
class Element extends EventTarget {
  isConnected = true; offsetParent = {}; classList = { toggle() {}, add() {}, remove() {}, contains: () => false };
  append() {} remove() {} contains(node) { return node === this; } querySelector() { return null; }
}
const win = new EventTarget();
const doc = new EventTarget();
Object.assign(doc, { createElement: () => new Element(), documentElement: {}, hidden: false, focused: true });
doc.hasFocus = () => doc.focused;
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { platform: 'MacIntel' } });
globalThis.document = doc;
globalThis.location = { protocol: 'http:', host: 'piper.test' };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.PANE = { cols: 124, rows: 47 };
let timers = [];
win.setTimeout = (fn) => { timers.push(fn); return timers.length; };
win.clearTimeout = () => {};
globalThis.window = win;
const flush = () => { while (timers.length) timers.shift()(); };
class FakeSocket {
  static OPEN = 1;
  static last;
  readyState = 1; sent = [];
  constructor() { FakeSocket.last = this; }
  send(m) { this.sent.push(JSON.parse(m)); }
  close() {}
  server(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  resizes() { return this.sent.filter((m) => m.t === 'resize').map((m) => `${m.c}x${m.r}`); }
}
globalThis.WebSocket = FakeSocket;

const moduleUrl = new URL('../../piper/web/dist/term.js', import.meta.url);
const source = readFileSync(moduleUrl, 'utf8').replace(/from '([^']+)'/g, (_match, specifier) =>
  `from '${specifier.startsWith('@xterm/') ? 'data:text/javascript;base64,' + Buffer.from(renderer).toString('base64') : new URL(specifier, moduleUrl).href}'`);
const { TermView, TERM_FLAGS } = await import('data:text/javascript;base64,' + Buffer.from(source + '\n//# sourceURL=' + moduleUrl.href).toString('base64'));

/** a connected view that has fitted the PTY to its pane (124x47) */
function connected() {
  const view = new TermView('s1', '/project', { onRefused() {}, onExit() {}, onConnection() {}, onImage: async () => '' });
  view.connect();
  const ws = FakeSocket.last;
  ws.onopen();
  ws.server({ t: 'hello', info: { cols: 124, rows: 47, tty: 0 } });
  flush();
  assert.deepEqual(ws.resizes(), ['124x47'], 'the settled replay fits to the pane');
  ws.server({ t: 'size', c: 124, r: 47 });   // ptyhost echoes our own resize
  ws.sent.length = 0;
  return { view, ws };
}

test('another window shrinks the PTY while the user is elsewhere: focus takes the size back', () => {
  doc.focused = false;
  const { view, ws } = connected();
  ws.server({ t: 'size', c: 80, r: 38 });   // a second Piper window / tab fitted the PTY to its own pane
  flush();
  assert.deepEqual(ws.resizes(), [], 'a window without focus does not fight');
  assert.equal(view.term.cols, 80, 'render incoming output at the shared PTY width');
  assert.equal(view.term.rows, 38);
  doc.focused = true;
  win.dispatchEvent(new Event('focus'));
  flush();
  assert.deepEqual(ws.resizes(), ['124x47']);
  view.dispose();
});

test('back to a visible tab or from the bfcache reclaims too; in sync it sends nothing (no SIGWINCH)', () => {
  doc.focused = false;
  const { view, ws } = connected();
  win.dispatchEvent(new Event('focus'));
  doc.dispatchEvent(new Event('visibilitychange'));
  flush();
  assert.deepEqual(ws.resizes(), [], 'the PTY already has our size');
  ws.server({ t: 'size', c: 80, r: 38 });
  doc.dispatchEvent(new Event('visibilitychange'));
  flush();
  assert.deepEqual(ws.resizes(), ['124x47']);
  ws.server({ t: 'size', c: 124, r: 47 });
  ws.server({ t: 'size', c: 90, r: 30 });
  win.dispatchEvent(new Event('pageshow'));
  flush();
  assert.deepEqual(ws.resizes(), ['124x47', '124x47']);
  doc.hidden = true;
  ws.server({ t: 'size', c: 90, r: 30 });
  doc.dispatchEvent(new Event('visibilitychange'));
  flush();
  assert.equal(ws.resizes().length, 2, 'hidden again: no reclaim');
  doc.hidden = false;
  view.dispose();
});

test('the focused window answers a foreign resize once, then yields (two focused devices cannot ping-pong)', () => {
  doc.focused = true;
  const { view, ws } = connected();
  ws.server({ t: 'size', c: 80, r: 38 });
  flush();
  assert.deepEqual(ws.resizes(), ['124x47']);
  ws.server({ t: 'size', c: 124, r: 47 });
  ws.server({ t: 'size', c: 80, r: 38 });   // the other device answers back at once
  flush();
  assert.deepEqual(ws.resizes(), ['124x47'], 'within REASSERT_MS this view yields');
  assert.equal(view.term.cols, 80, 'yielding must also follow the new renderer geometry');
  assert.equal(view.term.rows, 38);
  win.dispatchEvent(new Event('focus'));   // the user comes back here: it is this window's again
  flush();
  assert.deepEqual(ws.resizes(), ['124x47', '124x47']);
  view.dispose();
});

test('a disposed view is out of the reclaim set', () => {
  doc.focused = false;
  const { view, ws } = connected();
  ws.server({ t: 'size', c: 80, r: 38 });
  view.dispose();
  win.dispatchEvent(new Event('focus'));
  flush();
  assert.deepEqual(ws.resizes(), []);
});

// Phone navigation never focuses an input; the desktop retains keyboard navigation.
test('navigation focus respects a coarse primary pointer', () => {
  const view = new TermView('phone', '/project', {});
  win.matchMedia = () => ({ matches: true });
  view.focus();
  assert.equal(view.term.focused, undefined);
  win.matchMedia = () => ({ matches: false });
  view.focus();
  assert.equal(view.term.focused, true);
  delete win.matchMedia;
  view.dispose();
});

test('finger drag becomes wheel input; taps and cancelled gestures do not type or scroll', () => {
  const view = new TermView('phone', '/project', {});
  const wheels = [];
  view.host.classList = { contains: () => true }; // attached terminal overflow keeps pixel wheel input
  globalThis.WheelEvent = class extends Event { constructor(type, opts) { super(type, opts); Object.assign(this, { deltaX: opts.deltaX, deltaY: opts.deltaY }); } };
  view.host.querySelector = () => ({ dispatchEvent: e => wheels.push(e) });
  const touch = (type, x, y, count = 1) => {
    const e = new Event(type, { cancelable: true });
    e.touches = Array.from({ length: count }, (_, identifier) => ({ identifier, clientX: x, clientY: y }));
    view.host.dispatchEvent(e);
    return e;
  };
  touch('touchstart', 40, 100);
  touch('touchmove', 40, 97);
  assert.equal(wheels.length, 0);
  assert.equal(touch('touchend', 40, 97, 0).defaultPrevented, false, 'tap still opens keyboard');
  touch('touchstart', 40, 100);
  assert.equal(touch('touchmove', 40, 70).defaultPrevented, true);
  touch('touchmove', 40, 90);
  assert.deepEqual(wheels.map(e => e.deltaY), [30, -20]);
  assert.equal(touch('touchend', 40, 90, 0).defaultPrevented, true);
  touch('touchstart', 40, 100);
  touch('touchcancel', 40, 100, 0);
  touch('touchmove', 40, 50);
  assert.equal(wheels.length, 2);
  touch('touchstart', 40, 100, 2);
  touch('touchmove', 40, 50, 2);
  assert.equal(wheels.length, 2, 'pinch is not terminal input');
  view.dispose();
});

test('normal shell touch scroll uses whole rows and retains small pixel deltas', () => {
  const view = new TermView('shell', '/project', {});
  view.term.modes.mouseTrackingMode = 'none';
  view.term.buffer = { active: { type: 'normal' } };
  const lines = [];
  view.term.scrollLines = n => lines.push(n);
  view.host.querySelector = () => ({ clientHeight: 480 }); // 20 px per row
  const touch = (type, y) => {
    const e = new Event(type, { cancelable: true });
    e.touches = [{ identifier: 0, clientX: 40, clientY: y }];
    view.host.dispatchEvent(e);
  };
  touch('touchstart', 100);
  touch('touchmove', 111);
  touch('touchmove', 125);
  touch('touchmove', 150);
  assert.deepEqual(lines, [-1, -2, -4]);
  touch('touchmove', 100);
  assert.deepEqual(lines, [-1, -2, -4, 7]);
  view.dispose();
});

test('foreign size waits for already queued output to finish at the old geometry', () => {
  doc.focused = false;
  const { view, ws } = connected();
  let drain;
  view.term.write = (_data, cb) => { drain = cb; };
  ws.server({ t: 'size', c: 80, r: 38 });
  assert.equal(view.term.cols, 124, 'older output still uses the old grid');
  drain();
  assert.equal(view.term.cols, 80);
  assert.equal(view.term.rows, 38);
  view.dispose();
});

test('TUI touch distance produces every wheel step independently of event grouping', () => {
  const run = positions => {
    const view = new TermView('tui', '/project', {});
    const deltas = [];
    view.host.querySelector = () => ({ clientHeight: 480, dispatchEvent: e => deltas.push(e.deltaY) });
    for (const [index, y] of positions.entries()) {
      const e = new Event(index ? 'touchmove' : 'touchstart', { cancelable: true });
      e.touches = [{ identifier: 0, clientX: 40, clientY: y }];
      view.host.dispatchEvent(e);
    }
    view.dispose();
    return deltas;
  };
  assert.deepEqual(run([200, 100]), Array(15).fill(1));
  assert.deepEqual(run([200, 190, 180, 160, 140, 120, 100]), Array(15).fill(1));
  assert.deepEqual(run([100, 200]), Array(15).fill(-1));
});


test('terminal URLs follow live host policy for OSC 8 and report failure without a Safari fallback', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [], opened = [], errors = [];
  window.open = (...args) => opened.push(args);
  globalThis.fetch = async (path, init) => { calls.push([path, JSON.parse(init.body)]); return new Response('{"ok":true}'); };
  TERM_FLAGS.hostLinks = false;
  const view = new TermView('links', '/project', { onRefused: msg => errors.push(msg), onImage: async () => '' });
  try {
    const click = uri => view.term.options.linkHandler.activate({}, uri);
    click('https://example.test/first');
    assert.equal(opened.length, 1);
    TERM_FLAGS.hostLinks = true;   // an already running terminal must pick up the new setting
    click('https://example.test/chrome');
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, [['/api/open-url', { url: 'https://example.test/chrome' }]]);
    assert.equal(opened.length, 1);
    view.term.webLinks.activate({}, 'https://example.test/plain');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(calls.at(-1)[1].url, 'https://example.test/plain');
    globalThis.fetch = async () => new Response('{"error":"missing browser"}', {status:502});
    click('https://example.test/missing');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(errors.length, 1);
    assert.equal(opened.length, 1, 'no silent fallback into Safari');
    click('file:///tmp/no');
    assert.equal(opened.length, 1);
  } finally { view.dispose(); TERM_FLAGS.hostLinks = false; globalThis.fetch = originalFetch; delete window.open; }
});
