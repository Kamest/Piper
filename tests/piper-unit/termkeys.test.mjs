// Real TermView + TerminalKeys handlers, with only rendering/browser/socket boundaries replaced.
import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Element extends EventTarget {
  children = []; attributes = new Map(); className = ''; hidden = false; textContent = ''; isConnected = true;
  style = { values: new Map(), setProperty(k, v) { this.values.set(k, v); }, removeProperty(k) { this.values.delete(k); } };
  classList = {
    contains: name => this.className.split(' ').includes(name),
    toggle: (name, on) => { const names = new Set(this.className.split(' ').filter(Boolean)); (on ?? !names.has(name)) ? names.add(name) : names.delete(name); this.className = [...names].join(' '); },
    add: name => this.classList.toggle(name, true), remove: name => this.classList.toggle(name, false),
  };
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k); }
  append(...children) { this.children.push(...children); }
  remove() { this.isConnected = false; }
  contains(el) { return el === this || this.children.some(child => child.contains?.(el)); }
  querySelector() { return null; }
  focus() { document.activeElement?.blur(); document.activeElement = this; this.dispatchEvent(new Event('focus')); }
  blur() { if (document.activeElement === this) { document.activeElement = null; this.dispatchEvent(new Event('blur')); } }
}
const app = new Element();
const media = [];
const doc = Object.assign(new EventTarget(), {
  hidden: false, activeElement: null, documentElement: {}, hasFocus: () => true,
  createElement: () => new Element(), createTextNode: text => Object.assign(new Element(), { textContent: text }),
  getElementById: id => id === 'app' ? app : null,
});
const win = Object.assign(new EventTarget(), {
  setTimeout: () => 1, clearTimeout() {},
  visualViewport: Object.assign(new EventTarget(), { height: 514 }),
  matchMedia() { const query = Object.assign(new EventTarget(), { matches: true }); media.push(query); return query; },
});
Object.assign(globalThis, { document: doc, window: win, HTMLElement: Element,
  location: { protocol: 'http:', host: 'piper.test' }, getComputedStyle: () => ({ getPropertyValue: () => '' }) });
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { platform: 'MacIntel' } });
class Socket {
  static OPEN = 1; static last;
  readyState = 0; sent = [];
  constructor() { Socket.last = this; }
  send(message) { this.sent.push(JSON.parse(message)); }
  close() { this.readyState = 3; }
  server(message) { this.onmessage({ data: JSON.stringify(message) }); }
  open() { this.readyState = 1; this.onopen(); }
  disconnect() { this.readyState = 3; this.onclose({ code: 1006 }); }
  input() { return this.sent.filter(m => m.t === 'in').map(m => m.d); }
}
globalThis.WebSocket = Socket;
globalThis.makeTextarea = () => new Element();
const renderer = `
  export class Terminal {
    cols = 80; rows = 24; options = {}; unicode = {}; textarea = globalThis.makeTextarea();
    modes = { applicationCursorKeysMode: false }; parser = { registerOscHandler() {} };
    loadAddon() {} registerLinkProvider() {} attachCustomKeyEventHandler() {}
    onData(fn) { this.data = fn; } onBinary() {} input(s) { this.data(s); }
    focus() { this.textarea.focus(); } dispose() {} reset() {}
    resize(c,r) { this.cols=c; this.rows=r; } write(_s, cb) { cb?.(); }
  }
  export class FitAddon {} export class WebglAddon {} export class Unicode11Addon {}
  export class WebLinksAddon {} export class SearchAddon {}
`;
const url = new URL('../../piper/web/dist/term.js', import.meta.url);
const source = readFileSync(url, 'utf8').replace(/from '([^']+)'/g, (_m, path) =>
  `from '${path.startsWith('@xterm/') ? 'data:text/javascript;base64,' + Buffer.from(renderer).toString('base64') : new URL(path, url).href}'`);
const { TermView } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const { terminalKey } = await import('../../piper/web/dist/termkeys.js');

function setup(id = 'session-a', alive = true, active = true) {
  doc.hidden = false; doc.activeElement = null;
  const view = new TermView(id, '/project', { onRefused() {}, onConnection() {}, onExit() {} });
  view.setInputState(alive, active, id);
  view.connect();
  const socket = Socket.last;
  socket.open();
  return { view, socket, keys: view.touchKeys, press: key => view.touchKeys.buttons.get(key).dispatchEvent(new Event('click')) };
}

test('normal, application-cursor and Shift keys match terminal encoding; unsupported Shift combinations are refused', () => {
  for (const [key, suffix] of [['ArrowUp', 'A'], ['ArrowDown', 'B'], ['ArrowRight', 'C'], ['ArrowLeft', 'D']]) {
    assert.equal(terminalKey(key, false, false), '\x1b[' + suffix);
    assert.equal(terminalKey(key, false, true), '\x1bO' + suffix);
    for (const application of [false, true]) assert.equal(terminalKey(key, true, application), '\x1b[1;2' + suffix);
  }
  assert.equal(terminalKey('Tab', false, false), '\t');
  assert.equal(terminalKey('Tab', true, true), '\x1b[Z');
  for (const [key, data] of [['Escape', '\x1b'], ['Space', ' '], ['Enter', '\r']]) {
    assert.equal(terminalKey(key, false, false), data);
    assert.equal(terminalKey(key, true, false), null);
  }
  assert.equal(terminalKey('unknown', false, false), null);
});

test('tap input goes only to its own session, without focusing xterm or automatically submitting an answer', () => {
  const a = setup(), b = setup('session-b', true, false);
  a.press('ArrowDown'); a.press('Tab'); a.press('Space');
  assert.deepEqual(a.socket.input(), ['\x1b[B', '\t', ' ']);
  assert.deepEqual(b.socket.input(), []);
  assert.equal(doc.activeElement, null);
  a.press('Enter');
  assert.deepEqual(a.socket.input(), ['\x1b[B', '\t', ' ', '\r']);
  a.view.dispose(); b.view.dispose();
});

test('Shift is one-shot, shows modified labels, blocks Escape/Enter/Space and does not latch to the next key', () => {
  const t = setup();
  t.press('Shift');
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'true');
  assert.equal(t.keys.buttons.get('ArrowLeft').textContent, '⇧←');
  for (const key of ['Escape', 'Space', 'Enter']) {
    assert.equal(t.keys.buttons.get(key).getAttribute('aria-disabled'), 'true'); t.press(key);
  }
  assert.deepEqual(t.socket.input(), []);
  t.press('ArrowLeft'); t.press('ArrowLeft'); t.press('Shift'); t.press('Tab');
  assert.deepEqual(t.socket.input(), ['\x1b[1;2D', '\x1b[D', '\x1b[Z']);
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  t.view.dispose();
});

test('disconnected running session keeps disabled keys; taps are never queued for reconnect', () => {
  const t = setup(); t.press('Shift'); t.socket.disconnect();
  assert.equal(t.keys.el.hidden, false);
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  for (const key of ['Shift', 'Tab', 'ArrowLeft', 'Enter', 'Type']) t.press(key);
  assert.deepEqual(t.socket.input(), []);
  t.view.connect();
  const replacement = Socket.last; replacement.open();
  assert.deepEqual(replacement.input(), []);
  t.press('ArrowLeft');
  assert.deepEqual(replacement.input(), ['\x1b[D']);
  t.view.dispose();
});

test('stopped session has no keys even with a connected PTY replay; exit hides immediately and respawn can restore', () => {
  const t = setup('stopped', false);
  assert.equal(t.socket.readyState, Socket.OPEN);
  assert.equal(t.keys.el.hidden, true);
  t.press('Enter'); assert.deepEqual(t.socket.input(), []);
  t.view.setInputState(true, true, 'restarted');
  assert.equal(t.keys.el.hidden, false);
  t.socket.server({ t: 'exit', rc: 0 });
  assert.equal(t.keys.el.hidden, true);
  t.view.setInputState(true, true, 'stale poll');
  assert.equal(t.keys.el.hidden, true, 'a stale state poll must not reopen an exited PTY');
  t.socket.server({ t: 'respawn' });
  assert.equal(t.keys.el.hidden, false);
  t.view.setInputState(false, true, 'stopped again');
  assert.equal(t.keys.el.hidden, true, 'controller state also hides keys before a websocket exit arrives');
  t.view.dispose();
});

test('selection, background, touch-media and disposal reset Shift and release viewport listeners', () => {
  const t = setup();
  t.press('Shift'); t.view.setInputState(true, false, 'inactive');
  assert.equal(t.keys.el.hidden, true);
  t.view.setInputState(true, true, 'active'); t.press('ArrowLeft');
  assert.deepEqual(t.socket.input(), ['\x1b[D']);
  t.press('Shift'); doc.hidden = true; doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  doc.hidden = false;
  t.keys.media.matches = false; t.keys.media.dispatchEvent(new Event('change'));
  assert.equal(t.keys.el.hidden, true, 'desktop gets no keys');
  t.keys.media.matches = true; t.keys.media.dispatchEvent(new Event('change'));
  assert.equal(t.keys.el.hidden, false);
  assert.equal(app.style.values.get('--term-viewport-height'), '514px');
  t.view.dispose();
  assert.equal(t.keys.el.hidden, true);
  assert.equal(app.style.values.has('--term-viewport-height'), false);
});

test('Type/Hide alone changes keyboard focus; navigation pointerdown preserves it', () => {
  const t = setup();
  t.press('Type'); assert.equal(doc.activeElement, t.view.term.textarea);
  assert.equal(t.keys.buttons.get('Type').textContent, 'Hide');
  const down = new Event('pointerdown', { cancelable: true });
  t.keys.buttons.get('ArrowDown').dispatchEvent(down);
  assert.equal(down.defaultPrevented, true);
  t.press('ArrowDown'); assert.equal(doc.activeElement, t.view.term.textarea);
  t.press('Type'); assert.equal(doc.activeElement, null);
  assert.equal(t.keys.buttons.get('Type').textContent, 'Type');
  t.view.dispose();
});

test('switching panes in either update order keeps viewport owned by the newly active terminal', () => {
  const a = setup(), b = setup('other', true, false);
  b.view.setInputState(true, true, 'other');
  a.view.setInputState(true, false, 'previous');
  assert.equal(app.style.values.get('--term-viewport-height'), '514px');
  win.visualViewport.height = 430;
  win.visualViewport.dispatchEvent(new Event('resize'));
  assert.equal(app.style.values.get('--term-viewport-height'), '430px');
  a.view.dispose(); b.view.dispose();
});

test('shortening and restoring the viewport keeps touch input on the selected terminal', () => {
  const height = win.visualViewport.height;
  const a = setup('selected'), b = setup('background', true, false);
  try {
    a.press('Shift');
    win.visualViewport.height = 190;
    win.visualViewport.dispatchEvent(new Event('resize'));
    assert.equal(app.style.values.get('--term-viewport-height'), '190px');
    a.press('ArrowDown');
    win.visualViewport.height = 740;
    win.visualViewport.dispatchEvent(new Event('resize'));
    assert.equal(app.style.values.get('--term-viewport-height'), '740px');
    a.press('ArrowDown');
    assert.deepEqual(a.socket.input(), ['\x1b[1;2B', '\x1b[B']);
    assert.deepEqual(b.socket.input(), [], 'layout changes must not retarget or replay keys');
    assert.equal(doc.activeElement, null, 'resizing must not open the native keyboard');
  } finally {
    a.view.dispose(); b.view.dispose();
    win.visualViewport.height = height;
  }
});
