// 9.55: the PTY has one size, the last GUI window to send one wins; this window takes it back when the user returns.
// Loads the real compiled term.js; only xterm's addons, the socket and the browser globals are stubbed.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { installTransport, notifyTransportReady } from '../../piper/web/dist/transport.js';
import { piperUrl } from '../../piper/web/dist/references.js';

const renderer = `
      export class Terminal {
        cols = 80; rows = 24; options = {}; modes = { mouseTrackingMode: "vt200" };
        unicode = {}; parser = { registerOscHandler() {} };
        loadAddon(a) { a.term = this; if (a.activate) this.webLinks = a; } registerLinkProvider() {} onScroll() {} onRender() {} attachCustomKeyEventHandler() {}
        onData() {} onBinary() {} dispose() {} focus() { this.focused = true; } reset() { this.resets = (this.resets || 0) + 1; this.selection = ''; }
        hasSelection() { return !!this.selection; } getSelection() { return this.selection || ''; }
        getSelectionPosition() { return this.selection ? this.selectionPosition : undefined; }
        select(x,y,n) { this.selectedArgs = [x,y,n]; this.selection = this.replayText || ''; }
        clearSelection() { this.selection = ''; }
        resize(c, r) { this.cols = c; this.rows = r; }
        write(_d, cb) { if (globalThis.WRITES) globalThis.WRITES.push(() => cb?.()); else cb?.(); }
      }
      export class FitAddon { fit() { this.term.cols = globalThis.PANE.cols; this.term.rows = globalThis.PANE.rows; } }
      export class WebglAddon {} export class Unicode11Addon {} export class WebLinksAddon { constructor(activate) { this.activate = activate; } } export class SearchAddon {}
    `;
class Element extends EventTarget {
  setAttribute(k,v) { this[k]=v; }
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
const { TermView, TERM_FLAGS, terminalFontSize } = await import('data:text/javascript;base64,' + Buffer.from(source + '\n//# sourceURL=' + moduleUrl.href).toString('base64'));

/** a connected view that has fitted the PTY to its pane (124x47) */
function connected(info = {}) {
  const view = new TermView('s1', '/project', { onRefused() {}, onExit() {}, onConnection() {}, onImage: async () => '' });
  view.connect();
  const ws = FakeSocket.last;
  ws.onopen();
  ws.server({ t: 'hello', info: { cols: 124, rows: 47, tty: 0, ...info } });
  flush();
  assert.deepEqual(ws.resizes(), [], 'the settled replay already matches the pane; no SIGWINCH');
  ws.server({ t: 'size', c: 124, r: 47 });   // ptyhost echoes our own resize
  ws.sent.length = 0;
  return { view, ws };
}

test('saved terminal size refits live views, validates enum and survives temporary attached shrink', () => {
  for (const value of ['12', '13.5', '15', '16', '18', '20']) assert.equal(terminalFontSize(value), Number(value));
  for (const value of [undefined, '14', -1, 'NaN', '20px']) assert.equal(terminalFontSize(value), 13.5);
  const { view, ws } = connected();
  const originalPane = { ...PANE };
  try {
    PANE.cols = 96; PANE.rows = 35;
    TermView.setFontSizeAll('18');
    assert.equal(view.term.options.fontSize, 18);
    flush();
    assert.deepEqual(ws.resizes(), ['96x35'], 'changing preference schedules the normal fit and PTY resize');
    ws.sent.length = 0;
    TermView.setFontSizeAll('18'); flush();
    assert.deepEqual(ws.resizes(), [], 'unchanged polls never resize terminals again');
    view.tty = 1; view.pty = { cols: 124, rows: 47 };
    view.host.clientWidth = 900; view.host.clientHeight = 1000;
    view.host.querySelector = () => ({
      get offsetWidth() { return Number(view.term.options.fontSize) * 62; },
      get offsetHeight() { return Number(view.term.options.fontSize) * 47; },
    });
    view.holdTtySize(); flush();
    assert.ok(view.term.options.fontSize < 18, 'attached geometry may temporarily shrink the view');
    view.host.clientWidth = 1800; view.host.clientHeight = 1800;
    view.holdTtySize(); flush();
    assert.equal(view.term.options.fontSize, 18, 'larger pane restores the saved ceiling rather than 13.5');
    view.term.options.fontSize = 10;
    view.setTty(0); flush();
    assert.equal(view.term.options.fontSize, 18, 'detach restores the saved size');
    TermView.setFontSizeAll('invalid'); flush();
    assert.equal(view.term.options.fontSize, 13.5, 'invalid preference falls back safely');
  } finally {
    Object.assign(PANE, originalPane); view.dispose(); TermView.setFontSizeAll('13.5'); flush();
  }
});

test('local file and web links retain their complete soft-wrapped target on every terminal row', () => {
  const opened = [], refs = [];
  window.open = uri => opened.push(uri);
  for (const text of ['file:///fixtures/my%20report.md#L8', 'https://example.com/acme/base/-/issues/12', 'src/' + 'folder/'.repeat(40) + 'Report.java:12']) {
    const lines = text.match(/.{1,10}/g);
    const view = new TermView('wrapped', '/project', {onRefused() {}, onReference: ref => refs.push(ref)});
    view.term.buffer = {active:{getLine: row => lines[row] === undefined ? undefined : {
      isWrapped: row > 0, length: 10, translateToString: () => lines[row],
      getCell: x => ({getChars: () => lines[row][x] || '',getWidth: () => 1}),
    }}};
    for (let row = 1; row <= lines.length; row++) {
      const links = view.fileLinks(row);
      assert.equal(links.length, 1, `row ${row} must have one whole link`);
      assert.equal(links[0].text, text);
      assert.deepEqual(links[0].range, {start:{x:1,y:1},end:{x:lines.at(-1).length,y:lines.length}});
      links[0].activate({});
    }
    if (text.startsWith('file:')) assert.deepEqual(refs.at(-1),{kind:'file',path:'/fixtures/my report.md',line:8});
    else if (text.startsWith('https:')) assert.equal(opened.at(-1),text);
    else assert.equal(refs.at(-1).path,text.slice(0,-3));
    view.dispose();
  }
  delete window.open;
});

test('CLI hard wraps preserve complete targets and original terminal cell ranges', () => {
  for (const [lines, expected, range] of [
    [['Open helper-', '  kit/.gitlab-ci.yml'], 'helper-kit/.gitlab-ci.yml', {start:{x:6,y:1},end:{x:20,y:2}}],
    [['See https://example.com/long-', '  reference?q=one&', '  page=2'], 'https://example.com/long-reference?q=one&page=2', {start:{x:5,y:1},end:{x:8,y:3}}],
    [['src/VeryLongCompo', '  nent.java:12'], 'src/VeryLongComponent.java:12', {start:{x:1,y:1},end:{x:14,y:2}}],
  ]) {
    const view = new TermView('hard-wrapped', '/project', {onRefused() {}});
    view.term.cols = expected.startsWith('src/') ? 16 : 80;
    view.term.buffer = {active:{getLine: row => lines[row] === undefined ? undefined : {
      isWrapped: false, length: view.term.cols, translateToString: () => lines[row],
      getCell: x => ({getChars: () => lines[row][x] || '',getWidth: () => 1}),
    }}};
    for (let row = 1; row <= lines.length; row++) {
      const links = view.fileLinks(row);
      assert.equal(links.length, 1);
      assert.equal(links[0].text, expected);
      assert.deepEqual(links[0].range, range);
    }
    view.dispose();
  }
});

test('a path wrapped inside a CLI column joins its column cells, not the neighbouring columns', () => {
  // Claude Code's attachment row: "[image]" | path column | "(152.9 KB)" column; the path continues below its own start.
  const head = '  ›      ', size = 'development/startup-panel/transparen'.length + head.length + 1;
  const lines = [head + 'development/startup-panel/transparen (152.9', ('  [image]t-idle-1280.png').padEnd(size) + 'KB)'];
  lines.push('', 'plain prose with a short path src/a.md here');
  const refs = [];
  const view = new TermView('column-wrap', '/project', {onRefused() {}, onReference: ref => refs.push(ref)});
  view.term.buffer = {active:{getLine: row => lines[row] === undefined ? undefined : {
    isWrapped: false, length: 80, translateToString: () => lines[row],
    getCell: x => ({getChars: () => lines[row][x] || '', getWidth: () => 1}),
  }}};
  const expected = 'development/startup-panel/transparent-idle-1280.png';
  const top = view.fileLinks(1), bottom = view.fileLinks(2);
  assert.deepEqual(top.map(l => l.text), [expected]);
  assert.deepEqual(bottom.map(l => l.text), [expected]);
  assert.deepEqual(top[0].range, {start:{x:10,y:1}, end:{x:45,y:1}}, 'only the path cells of the first row');
  assert.deepEqual(bottom[0].range, {start:{x:10,y:2}, end:{x:24,y:2}}, 'only the path cells of the second row');
  bottom[0].activate({});
  assert.equal(refs.at(-1).path, expected);
  assert.deepEqual(view.fileLinks(4).map(l => l.text), ['src/a.md'], 'ordinary single-row output is unchanged');
  view.dispose();
});

test('hard-wrap cell ranges preserve wide Unicode prefixes and stop at bounded output', () => {
  const view = new TermView('wide-hard-wrap', '/project', {onRefused() {}});
  const lines = ['前🙂 Open helper-', '  kit/.gitlab-ci.yml'];
  const cells = lines.map(line => [...line].flatMap(char => ['前','🙂'].includes(char)
    ? [{getChars:()=>char,getWidth:()=>2},{getChars:()=>'',getWidth:()=>0}]
    : [{getChars:()=>char,getWidth:()=>1}]));
  view.term.buffer = {active:{getLine: row => lines[row] === undefined ? undefined : {
    isWrapped:false,length:80,translateToString:()=>lines[row],getCell:x=>cells[row][x],
  }}};
  assert.deepEqual(view.fileLinks(2)[0].range,{start:{x:11,y:1},end:{x:20,y:2}});
  assert.equal(view.fileLinks(2)[0].text,'helper-kit/.gitlab-ci.yml');
  view.term.buffer = {active:{getLine:row=>row>=0&&row<300 ? {
    isWrapped:false,length:80,translateToString:()=>row===299?'  final.md':'  folder/',
    getCell:x=>({getChars:()=>x<9?'x':'',getWidth:()=>1}),
  }:undefined}};
  assert.deepEqual(view.fileLinks(300),[],'a bounded suffix must never become a different guessed path');
  view.dispose();
});

test('controller reconnect preserves native terminal selection only for the same PTY and exact replayed text', () => {
  for (const [sameProcess, sameText, expected] of [[true,true,'kept text'],[false,true,''],[true,false,'']]) {
    const { view, ws } = connected({pid:41,started:100});
    view.term.selection = 'kept text'; view.term.replayText = sameText ? 'kept text' : 'changed text';
    view.term.selectionPosition = {start:{x:2,y:3},end:{x:11,y:3}};
    ws.onclose({code:1006}); flush();
    const next = FakeSocket.last; next.onopen();
    next.server({t:'hello',info:{cols:124,rows:47,tty:0,pid:sameProcess?41:42,started:100,replay_geometry:true}});
    next.server({t:'replay-end'}); flush();
    assert.equal(view.term.getSelection(),expected);
    assert.equal(next.sent.some(m=>['in','input','send'].includes(m.t)),false,'selection restoration must never type into the PTY');
    view.dispose();
  }
});

test('geometry-aware replay waits for its boundary and never reclaims historical sizes', () => {
  const view = new TermView('geometry', '/project', { onRefused() {}, onExit() {}, onConnection() {} });
  view.connect();
  const ws = FakeSocket.last;
  ws.onopen();
  ws.server({t:'hello', info:{cols:96,rows:47,tty:0,replay_geometry:true}});
  flush();
  assert.deepEqual(ws.resizes(), [], 'silence while replaying is not completion');
  ws.server({t:'size', c:48,r:47,replay:true});
  flush();
  assert.equal(view.term.cols,48);
  assert.deepEqual(ws.resizes(), [], 'historical geometry must not trigger focus reclaim');
  ws.server({t:'size', c:96,r:47,replay:true});
  ws.server({t:'replay-end'});
  flush();
  assert.deepEqual(ws.resizes(), ['124x47']);
  view.dispose();
});

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
  // the tap lands on no link (empty screen): it keeps opening the keyboard
  view.host.querySelector = () => ({ dispatchEvent: e => wheels.push(e), getBoundingClientRect: () => ({ left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0 }) });
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

test('mobile terminal links open on the phone even when the desktop host browser policy is enabled', () => {
  const opened = [], refused = [];
  window.open = (...args) => opened.push(args);
  installTransport({ visibleTerminalsOnly: true, fetch() { throw Error('must not call host browser'); }, socket() {} });
  TERM_FLAGS.hostLinks = true;
  const view = new TermView('mobile-links', '/fixture', { onRefused: value => refused.push(value), onUrl: uri => uri.includes('session=fixture') });
  try {
    view.term.options.linkHandler.activate({}, 'https://example.test/osc');
    view.term.webLinks.activate({}, 'https://example.test/plain');
    view.term.webLinks.activate({}, 'http://piper.test/');
    view.term.options.linkHandler.activate({}, 'http://piper.test/');
    view.term.options.linkHandler.activate({}, 'http://piper.test/?session=fixture');
    view.term.options.linkHandler.activate({}, 'javascript:alert(1)');
    assert.deepEqual(opened, [
      ['https://example.test/osc', '_blank', 'noopener'],
      ['https://example.test/plain', '_blank', 'noopener'],
      ['http://piper.test/', '_blank', 'noopener'],
      ['http://piper.test/', '_blank', 'noopener'],
    ]);
    assert.deepEqual(refused, []);
  } finally { view.dispose(); installTransport(null); TERM_FLAGS.hostLinks = false; delete window.open; }
});

test('mobile markdown anchors keep native navigation, desktop host links and internal Piper routing retain their policies', () => {
  const main = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const start = main.indexOf('function anchorLink(e) {');
  const handler = main.slice(start, main.indexOf("document.addEventListener('click', anchorLink", start));
  const calls = []; let mobile = true;
  const route = new Function('piperUrl', 'location', 'window', 'sessionUrl', 'TERM_FLAGS', 'mobilePushClient', 'openUrl', 'toast', handler + ';return anchorLink;')(
    piperUrl, { href: 'https://piper.test/' }, {}, uri => calls.push(['internal', uri]),
    { hostLinks: true }, () => mobile, uri => calls.push(['host', uri]), () => {});
  const click = (href, rawHref = href) => {
    const event = { target: { closest: () => ({ href, getAttribute: name => name === 'href' ? rawHref : null, hasAttribute: () => false }) }, prevented: false, stopped: false,
      preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    route(event); return event;
  };
  assert.equal(click('https://example.test/transcript').prevented, false, 'the phone follows the real anchor itself');
  assert.equal(click('https://piper.test/').prevented, false, 'the public homepage is a web link on the phone');
  assert.deepEqual(calls, []);
  assert.equal(click('https://piper.test/?session=fixture').prevented, true);
  mobile = false;
  const placeholder = click('https://piper.test/#', '#');
  assert.equal(placeholder.prevented, false, 'approval rule links keep their local click handler');
  assert.equal(placeholder.stopped, false, 'the global router must not stop the card handler');
  assert.equal(click('https://piper.test/').prevented, true, 'desktop homepage routing is unchanged');
  assert.equal(click('https://example.test/desktop').prevented, true);
  assert.deepEqual(calls, [['internal', 'https://piper.test/?session=fixture'], ['internal', 'https://piper.test/'], ['host', 'https://example.test/desktop']]);
});


test('returning to the same pane never sends an unchanged PTY resize', () => {
  const { view, ws } = connected();
  view.refit();
  flush();
  assert.deepEqual(ws.resizes(), [], 'unchanged resize triggers SIGWINCH/redraw despite no geometry change');
  view.dispose();
});

test('reconnect at the announced pane geometry does not redraw the live agent', () => {
  const view = new TermView('same-size', '/project', { onRefused() {}, onExit() {}, onConnection() {} });
  view.connect();
  const ws = FakeSocket.last;
  ws.onopen();
  ws.server({ t: 'hello', info: { cols: PANE.cols, rows: PANE.rows, tty: 0, replay_geometry: true } });
  ws.server({ t: 'replay-end' });
  flush();
  assert.deepEqual(ws.resizes(), [], 'replay completion must measure without forcing SIGWINCH');
  view.dispose();
});

test('a hidden browser cannot seize PTY geometry during a layout notification', () => {
  const { view, ws } = connected();
  doc.hidden = true;
  const prior = PANE.rows;
  try {
    PANE.rows = prior - 5;
    view.refit();
    flush();
    assert.deepEqual(ws.resizes(), [], 'a background phone resizes the shared agent');
  } finally {
    doc.hidden = false; PANE.rows = prior; view.dispose();
  }
});


test('mobile recovery immediately reconnects the visible terminal, never a retained hidden one',()=>{
  let opened=0;
  installTransport({visibleTerminalsOnly:true,socket(){opened++;return new FakeSocket()},fetch(){throw Error('unused')}});
  const visible=new TermView('visible','/fixture',{onConnection(){},onExit(){},onRefused(){}});
  const hidden=new TermView('hidden','/fixture',{onConnection(){},onExit(){},onRefused(){}});
  visible.term.element={};hidden.term.element={};
  try{
    visible.setInputState(true,true,'Visible');hidden.setInputState(true,false,'Hidden');
    assert.equal(opened,1);
    const ws=FakeSocket.last;ws.onopen();
    assert.equal(visible.term.options.disableStdin,true,'handshake is not completed replay');
    visible.send({t:'in',d:'never replay'});visible.sendBinary('x');assert.equal(ws.sent.length,1,'recovery cannot accept uncertain input');
    ws.server({t:'hello',info:{cols:124,rows:47,tty:0,replay_geometry:true}});
    assert.equal(visible.term.options.disableStdin,true);
    ws.server({t:'replay-end'});flush();assert.equal(visible.term.options.disableStdin,false);
    ws.readyState=3;ws.onclose({code:1006});assert.ok(visible.retryTimer);
    notifyTransportReady();assert.equal(opened,2,'ready transport bypasses old retry');
    flush();assert.equal(opened,2,'stale retry must not open a duplicate');
    visible.setInputState(true,false,'Visible');assert.equal(visible.ws,null);
    notifyTransportReady();assert.equal(opened,2,'no hidden stream on shared ready');
    doc.hidden=true;visible.setInputState(true,true,'Visible');assert.equal(opened,2);
    doc.hidden=false;visible.reclaim();assert.equal(opened,3);
  }finally{visible.dispose();hidden.dispose();installTransport(null);doc.hidden=false;flush()}
});


test('mobile resume waits for queued xterm parsing and requests only committed bytes',()=>{
  const urls=[];
  installTransport({visibleTerminalsOnly:true,socket(url){urls.push(url);return new FakeSocket()},fetch(){throw Error('unused')}});
  const view=new TermView('checkpoint','/fixture',{onConnection(){},onExit(){},onRefused(){}});
  view.term.element={};
  try{
    view.setInputState(true,true,'Fixture');
    const ws=FakeSocket.last;ws.onopen();
    ws.server({t:'hello',checkpoint:true,resumed:false,info:{pid:11,started:12.5,bytes:3,cols:124,rows:47,replay_geometry:true}});
    globalThis.WRITES=[];
    ws.server({t:'chunk',off:0,bytes:3});ws.onmessage({data:new Uint8Array([97,98,99]).buffer});
    view.setInputState(true,false,'Fixture');view.setInputState(true,true,'Fixture');
    assert.equal(urls.length,1,'a new dial waits for the emulator parse barrier');
    while(WRITES.length)WRITES.shift()();
    delete globalThis.WRITES;
    assert.equal(urls.length,2);
    const query=new URL(urls[1]).searchParams;
    assert.equal(query.get('checkpoint'),'1');assert.equal(query.get('from'),'3');assert.equal(query.get('pid'),'11');
    const next=FakeSocket.last;next.onopen();
    next.server({t:'hello',checkpoint:true,resumed:true,info:{pid:11,started:12.5,bytes:5,cols:124,rows:47,replay_geometry:true}});
    next.server({t:'chunk',off:3,bytes:2});next.onmessage({data:new Uint8Array([100,101]).buffer});
    next.server({t:'replay-end'});flush();
    assert.equal(view.term.resets,1,'a valid resume preserves the existing emulator');
    assert.equal(view.checkpoint.offset,5);
    assert.equal(view.term.options.disableStdin,false);
  }finally{delete globalThis.WRITES;view.dispose();installTransport(null);flush()}
});

test('checkpoint gaps discard the checkpoint and never enable input',()=>{
  installTransport({visibleTerminalsOnly:true,socket(){return new FakeSocket()},fetch(){throw Error('unused')}});
  const view=new TermView('gap','/fixture',{onConnection(){},onExit(){},onRefused(){}});view.term.element={};
  try{
    view.setInputState(true,true,'Fixture');const ws=FakeSocket.last;ws.onopen();
    ws.server({t:'hello',checkpoint:true,resumed:false,info:{pid:11,started:12.5,bytes:5,cols:124,rows:47,replay_geometry:true}});
    ws.server({t:'chunk',off:0,bytes:2});ws.onmessage({data:new Uint8Array([97,98]).buffer});
    ws.server({t:'chunk',off:4,bytes:1});
    assert.equal(view.checkpoint,null);assert.equal(view.term.options.disableStdin,true);
  }finally{view.dispose();installTransport(null);flush()}
});

test('a queued foreign geometry event cannot resize a respawned emulator',()=>{
  const {view,ws}=connected();
  globalThis.WRITES=[];
  try{
    ws.server({t:'size',c:80,r:24});
    ws.server({t:'respawn'});
    while(WRITES.length)WRITES.shift()();
    assert.equal(view.term.cols,124,'old epoch queued geometry must be discarded');
    assert.equal(view.lastSize,'');
  }finally{delete globalThis.WRITES;view.dispose();flush()}
});
