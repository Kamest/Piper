// Real TermView + TerminalKeys handlers, with only rendering/browser/socket boundaries replaced.
import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Element extends EventTarget {
  children = []; value = ''; selectionStart = 0; selectionEnd = 0; disabled = false; attributes = new Map(); className = ''; hidden = false; textContent = ''; isConnected = true;
  style = { values: new Map(), setProperty(k, v) { this.values.set(k, v); }, removeProperty(k) { this.values.delete(k); } };
  classList = {
    contains: name => this.className.split(' ').includes(name),
    toggle: (name, on) => { const names = new Set(this.className.split(' ').filter(Boolean)); (on ?? !names.has(name)) ? names.add(name) : names.delete(name); this.className = [...names].join(' '); },
    add: name => this.classList.toggle(name, true), remove: name => this.classList.toggle(name, false),
  };
  setAttribute(k, v) { this.attributes.set(k, String(v)); }
  getAttribute(k) { return this.attributes.get(k); }
  append(...children) { this.children.push(...children); for (const child of children) child.parentElement = this; }
  remove() { this.isConnected = false; if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
  prepend(child) { child.remove(); this.children.unshift(child); child.parentElement=this; child.isConnected=true; }
  insertBefore(child, before) { child.remove(); this.children.splice(this.children.indexOf(before),0,child); child.parentElement=this; child.isConnected=true; }
  contains(el) { return el === this || this.children.some(child => child.contains?.(el)); }
  querySelector() { return null; }
  click() { this.dispatchEvent(new Event('click')); }
  setSelectionRange(start, end) { this.selectionStart=start; this.selectionEnd=end; }
  focus() { document.activeElement?.blur(); document.activeElement = this; this.dispatchEvent(new Event('focus')); }
  blur() { if (document.activeElement === this) { document.activeElement = null; this.dispatchEvent(new Event('blur')); } }
}
const observers = [];
globalThis.ResizeObserver = class {
  constructor(callback) { this.callback = callback; observers.push(this); }
  observe(target) { this.target = target; }
  disconnect() { this.disconnected = true; }
  width(value) { this.callback([{contentRect: {width: value}}]); }
};
const app = new Element();
const media = [];
const doc = Object.assign(new EventTarget(), {
  hidden: false, activeElement: null, documentElement: {}, hasFocus: () => true,
  createElement: () => new Element(), createTextNode: text => Object.assign(new Element(), { textContent: text }),
  getElementById: id => id === 'app' ? app : null,
});
const win = Object.assign(new EventTarget(), {
  setTimeout: () => 1, clearTimeout() {},
  requestAnimationFrame(callback) { callback(); return 1; }, cancelAnimationFrame() {},
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
    modes = { applicationCursorKeysMode: false, bracketedPasteMode: false }; parser = { registerOscHandler() {} };
    loadAddon() {} registerLinkProvider() {} attachCustomKeyEventHandler() {}
    onData(fn) { this.data = fn; } onBinary() {} input(s) { this.data(s); }
    focus() { this.textarea.focus(); } paste(s) { this.data(s); } dispose() {} reset() {}
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

function setup(id = 'session-a', alive = true, active = true, opened = true) {
  doc.hidden = false; doc.activeElement = null;
  const view = new TermView(id, '/project', { onRefused() {}, onConnection() {}, onExit() {} });
  view.setInputState(alive, active, id);
  view.connect();
  const socket = Socket.last;
  socket.open();
  const opener = view.touchKeys.createToggle?.();
  if (opened) opener?.dispatchEvent(new Event('click'));
  return { view, socket, keys: view.touchKeys, opener, press: key => view.touchKeys.buttons.get(key).dispatchEvent(new Event('click')) };
}

test('touch keys start closed and opening/closing never focuses, resizes, scrolls or sends input', () => {
  const t = setup('explicit', true, true, false);
  assert.equal(t.keys.el.hidden, true, 'ordinary terminal reading must not expose the keys');
  assert.ok(t.opener);
  assert.equal(t.opener.getAttribute('aria-expanded'), 'false');
  assert.equal(t.opener.getAttribute('aria-controls'), t.keys.el.id);
  let fits = 0; t.view.scheduleFit = () => { fits++; };
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, false);
  assert.equal(t.opener.getAttribute('aria-expanded'), 'true');
  t.press('Shift');
  t.keys.closeButton.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, true);
  assert.equal(t.opener.getAttribute('aria-expanded'), 'false');
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  t.press('Enter');
  assert.deepEqual(t.socket.input(), []);
  assert.equal(doc.activeElement, null);
  assert.equal(fits, 0);
  t.view.dispose();
});

test('switching terminal or backgrounding closes the panel until explicitly reopened', () => {
  const t = setup();
  assert.equal(t.keys.el.hidden, false);
  t.view.setInputState(true, false, 'inactive');
  t.view.setInputState(true, true, 'active again');
  assert.equal(t.keys.el.hidden, true);
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, false);
  doc.hidden = true; doc.dispatchEvent(new Event('visibilitychange'));
  doc.hidden = false; doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(t.keys.el.hidden, true);
  t.view.dispose();
});

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
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, false);
  t.socket.server({ t: 'exit', rc: 0 });
  assert.equal(t.keys.el.hidden, true);
  t.view.setInputState(true, true, 'stale poll');
  assert.equal(t.keys.el.hidden, true, 'a stale state poll must not reopen an exited PTY');
  t.socket.server({ t: 'respawn' });
  assert.equal(t.keys.el.hidden, true, 'respawn does not implicitly reopen keys');
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, false);
  t.view.setInputState(false, true, 'stopped again');
  assert.equal(t.keys.el.hidden, true, 'controller state also hides keys before a websocket exit arrives');
  t.view.dispose();
});

test('selection, background, touch-media and disposal reset Shift without owning viewport layout', () => {
  const t = setup();
  t.press('Shift'); t.view.setInputState(true, false, 'inactive');
  assert.equal(t.keys.el.hidden, true);
  t.view.setInputState(true, true, 'active'); t.opener.dispatchEvent(new Event('click')); t.press('ArrowLeft');
  assert.deepEqual(t.socket.input(), ['\x1b[D']);
  t.press('Shift'); doc.hidden = true; doc.dispatchEvent(new Event('visibilitychange'));
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  doc.hidden = false;
  t.keys.media.matches = false; t.keys.media.dispatchEvent(new Event('change'));
  assert.equal(t.keys.el.hidden, true, 'desktop gets no keys');
  t.keys.media.matches = true; t.keys.media.dispatchEvent(new Event('change'));
  assert.equal(t.keys.el.hidden, true);
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.el.hidden, false);
  assert.equal(app.style.values.has('--term-viewport-height'), false);
  t.view.dispose();
  assert.equal(t.keys.el.hidden, true);
  assert.equal(app.style.values.has('--term-viewport-height'), false);
});

test('Type/Hide alone changes keyboard focus; navigation pointerdown preserves it', () => {
  const t = setup();
  t.press('Type'); assert.equal(doc.activeElement, t.view.term.textarea);
  assert.equal(t.keys.buttons.get('Type').textContent, 'Hide');
  assert.equal(t.keys.keyboardStatus.textContent, 'Keyboard open');
  assert.equal(t.keys.status.getAttribute('aria-hidden'), 'true');
  const down = new Event('pointerdown', { cancelable: true });
  t.keys.buttons.get('ArrowDown').dispatchEvent(down);
  assert.equal(down.defaultPrevented, true);
  t.press('ArrowDown'); assert.equal(doc.activeElement, t.view.term.textarea);
  t.press('Type'); assert.equal(doc.activeElement, null);
  assert.equal(t.keys.buttons.get('Type').textContent, 'Type');
  t.view.dispose();
});

test('switching panes in either update order never leaves a panel open on a new target', () => {
  const a = setup(), b = setup('other', true, false);
  b.view.setInputState(true, true, 'other');
  a.view.setInputState(true, false, 'previous');
  assert.equal(a.keys.el.hidden, true);
  assert.equal(b.keys.el.hidden, true);
  win.visualViewport.height = 430;
  win.visualViewport.dispatchEvent(new Event('resize'));
  assert.equal(app.style.values.has('--term-viewport-height'), false);
  a.view.dispose(); b.view.dispose();
});

test('shortening and restoring the viewport keeps touch input on the selected terminal', () => {
  const height = win.visualViewport.height;
  const a = setup('selected'), b = setup('background', true, false);
  try {
    a.press('Shift');
    win.visualViewport.height = 190;
    win.visualViewport.dispatchEvent(new Event('resize'));
    assert.equal(app.style.values.has('--term-viewport-height'), false);
    a.press('ArrowDown');
    win.visualViewport.height = 740;
    win.visualViewport.dispatchEvent(new Event('resize'));
    assert.equal(app.style.values.has('--term-viewport-height'), false);
    a.press('ArrowDown');
    assert.deepEqual(a.socket.input(), ['\x1b[1;2B', '\x1b[B']);
    assert.deepEqual(b.socket.input(), [], 'layout changes must not retarget or replay keys');
    assert.equal(doc.activeElement, null, 'resizing must not open the native keyboard');
  } finally {
    a.view.dispose(); b.view.dispose();
    win.visualViewport.height = height;
  }
});


test('narrow panes hide the toggle and close the panel without shrinking touch targets or reopening on expansion', () => {
  const t = setup();
  const observer = observers.at(-1);
  observer.width(252);
  assert.equal(t.opener.hidden, false);
  assert.equal(t.keys.el.hidden, false);
  t.press('Shift');
  observer.width(251);
  assert.equal(t.opener.hidden, true);
  assert.equal(t.keys.el.hidden, true);
  observer.width(338);
  assert.equal(t.opener.hidden, false);
  assert.equal(t.keys.el.hidden, true);
  t.opener.dispatchEvent(new Event('click'));
  assert.equal(t.keys.buttons.get('Shift').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(t.socket.input(), []);
  t.view.dispose();
  assert.equal(observer.disconnected, true);
});

test('replacement headers stay synchronized and panel padding preserves keyboard focus', () => {
  const t = setup();
  const retained = t.keys.createToggle();
  assert.equal(retained.getAttribute('aria-expanded'), 'true');
  t.keys.closeButton.dispatchEvent(new Event('click'));
  assert.equal(retained.getAttribute('aria-expanded'), 'false');
  retained.dispatchEvent(new Event('click'));
  assert.equal(t.opener.getAttribute('aria-expanded'), 'true');
  const down = new Event('pointerdown', {cancelable: true});
  t.keys.el.dispatchEvent(down);
  assert.equal(down.defaultPrevented, true);
  t.keys.buttons.get('Tab').focus();
  t.keys.closeButton.dispatchEvent(new Event('click'));
  assert.equal(doc.activeElement, t.opener);
  t.view.dispose();
});

test('pane width observer defers layout writes and ignores height-only notifications', () => {
  const t = setup(), observer = observers.at(-1), frame = win.requestAnimationFrame;
  let pending, scheduled = 0;
  win.requestAnimationFrame = callback => { pending = callback; return ++scheduled; };
  try {
    observer.width(251);
    assert.equal(t.opener.hidden, false, 'no layout mutation during ResizeObserver delivery');
    assert.equal(scheduled, 1);
    pending();
    assert.equal(t.opener.hidden, true);
    observer.width(251);
    assert.equal(scheduled, 1, 'a height-only notification cannot start another layout cycle');
  } finally { win.requestAnimationFrame = frame; t.view.dispose(); }
});

const tick = () => new Promise(setImmediate);
const tool = (t, name) => t.keys.tools.get(name).click();

test('touch Copy offers native selectable buffer text and copies only the chosen range without PTY input', async () => {
  const t = setup();
  t.view.term.buffer = {active: {length: 2, getLine: i => ({isWrapped: false, translateToString: () => ['first', 'second'][i]})}};
  const copied = [];
  navigator.clipboard = {writeText: async text => copied.push(text)};
  tool(t, 'copy');
  assert.equal(t.keys.sheet.hidden, false);
  assert.equal(t.keys.field.readOnly, true);
  assert.equal(t.keys.field.value, 'first\nsecond');
  t.keys.field.setSelectionRange(6, 12); t.keys.field.dispatchEvent(new Event('select'));
  assert.equal(t.keys.selectionButton.disabled, false);
  t.keys.selectionButton.click(); await tick();
  assert.deepEqual(copied, ['second']);
  assert.deepEqual(t.socket.input(), []);
  navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  t.keys.primaryButton.click(); await tick();
  assert.match(t.keys.sheetStatus.textContent, /long-press it and choose Copy/);
  assert.equal(t.keys.field.selectionEnd, 12);
  t.view.dispose();
});

test('touch Paste reads from its click and inserts once without adding Enter', async () => {
  const t = setup(); let reads = 0;
  navigator.clipboard = {readText: () => { reads++; return Promise.resolve('pasted text'); }};
  tool(t, 'paste');
  assert.equal(reads, 1, 'API must be invoked synchronously in the gesture');
  await tick();
  assert.deepEqual(t.socket.input(), ['pasted text']);
  assert.equal(t.keys.sheet.hidden, true);
  assert.match(t.keys.status.textContent, /Enter not pressed/);
  t.view.dispose();
});

test('clipboard denial and multiline paste use a native editable field and explicit Insert', async () => {
  const t = setup();
  navigator.clipboard = {readText: async () => { throw new Error('denied'); }};
  tool(t, 'paste'); await tick();
  assert.equal(t.keys.field.readOnly, false);
  assert.equal(doc.activeElement, t.keys.field);
  assert.match(t.keys.sheetStatus.textContent, /Long-press the box/);
  t.keys.field.value = 'manual'; t.keys.field.dispatchEvent(new Event('input'));
  t.keys.primaryButton.click();
  assert.deepEqual(t.socket.input(), ['manual']);
  navigator.clipboard.readText = async () => 'line one\nline two';
  tool(t, 'paste'); await tick();
  assert.equal(t.keys.sheet.hidden, false);
  assert.equal(t.keys.warning.hidden, false);
  assert.deepEqual(t.socket.input(), ['manual']);
  t.keys.primaryButton.click();
  assert.deepEqual(t.socket.input(), ['manual', 'line one\nline two']);
  t.view.dispose();
});

test('late clipboard reads never send after disconnect/reconnect, background or a pane switch', async () => {
  for (const reason of ['disconnect', 'switch', 'background']) {
    const t = setup(); let resolve;
    navigator.clipboard = {readText: () => new Promise(r => {resolve = r;})};
    tool(t, 'paste');
    if (reason === 'disconnect') { t.socket.readyState=3; t.view.syncKeys(); t.socket.readyState=1; t.view.syncKeys(); }
    if (reason === 'switch') { t.view.setInputState(true,false,'old'); t.view.setInputState(true,true,'new'); }
    if (reason === 'background') { doc.hidden=true; doc.dispatchEvent(new Event('visibilitychange')); doc.hidden=false; }
    resolve('late'); await tick();
    assert.deepEqual(t.socket.input(), [], reason);
    t.view.dispose();
  }
});

test('Image opens a picker in the click, reuses upload, and never inserts on a disconnected terminal', async () => {
  const t = setup(); let picks = 0, finish; const files = [];
  t.keys.picker.addEventListener('click', () => picks++);
  t.view.ev.onImage = file => { files.push(file); return new Promise(r => { finish=r; }); };
  tool(t, 'image'); assert.equal(picks, 1);
  const file = {type:'image/png',name:'screenshot.png'};
  t.keys.picker.files = [file]; t.keys.picker.dispatchEvent(new Event('change'));
  assert.deepEqual(files, [file]); assert.equal(t.keys.picker.value, '');
  assert.equal(t.keys.tools.get('image').getAttribute('aria-disabled'), 'true');
  finish('/project/image.png'); await tick();
  assert.deepEqual(t.socket.input(), ['/project/image.png ']);
  t.keys.picker.dispatchEvent(new Event('change'));
  t.socket.readyState=3; t.view.syncKeys(); finish('/project/late.png'); await tick();
  assert.deepEqual(t.socket.input(), ['/project/image.png ']);
  assert.equal(t.keys.tools.get('copy').getAttribute('aria-disabled'), 'false');
  assert.equal(t.keys.tools.get('paste').getAttribute('aria-disabled'), 'true');
  t.view.dispose();
});

test('bracketed paste accepts multiline clipboard text without an extra submission', async () => {
  const t = setup(); t.view.term.modes.bracketedPasteMode = true;
  navigator.clipboard = {readText: async () => 'first\nsecond'};
  tool(t, 'paste'); await tick();
  assert.deepEqual(t.socket.input(), ['first\nsecond']);
  assert.equal(t.keys.sheet.hidden, true);
  t.press('Shift'); tool(t, 'image');
  assert.equal(t.keys.tools.get('paste').getAttribute('aria-disabled'), 'true');
  t.view.dispose();
});

test('an image finishing after disconnect/reconnect or a session switch is never replayed', async () => {
  for (const reason of ['disconnect', 'switch']) {
    const t = setup(); let finish;
    t.view.ev.onImage = () => new Promise(r => {finish = r;});
    t.keys.picker.files = [{type:'image/png',name:'screenshot.png'}];
    t.keys.picker.dispatchEvent(new Event('change'));
    if (reason === 'disconnect') { t.socket.readyState=3; t.view.syncKeys(); t.socket.readyState=1; t.view.syncKeys(); }
    else { t.view.setInputState(true,false,'old'); t.view.setInputState(true,true,'again'); }
    finish('/project/late.png'); await tick();
    assert.deepEqual(t.socket.input(), [], reason);
    t.view.dispose();
  }
});
