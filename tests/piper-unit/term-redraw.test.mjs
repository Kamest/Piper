// 9.155: ⋯ Redraw terminal takes the PTY size back on demand and reconnects a dead socket;
// __piperTermDiag() reports each view's input path (what reached it, what left, what was dropped and why).
// Loads the real compiled term.js; only xterm's addons, the socket and the browser globals are stubbed.
import './_sandbox.mjs';   // only inside the test sandbox (scripts/piper-sandbox.py)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const renderer = `
      export class Terminal {
        cols = 80; rows = 24; options = {}; modes = { mouseTrackingMode: "any" }; buffer = { active: { type: "normal" } };
        unicode = {}; parser = { registerOscHandler() {} }; textarea = null;
        loadAddon(a) { a.term = this; } registerLinkProvider() {} onScroll() {} onRender() {} attachCustomKeyEventHandler() {}
        onData(cb) { this.data = cb; } onBinary(cb) { this.binary = cb; } dispose() {} focus() { this.focused = true; } reset() {}
        refresh(a, b) { this.refreshed = [a, b]; }
        resize(c, r) { this.cols = c; this.rows = r; (globalThis.GRIDS ||= []).push(c + 'x' + r); }
        write(d, cb) { (globalThis.WRITES ||= []).push(d); cb?.(); }
      }
      export class FitAddon { fit() { this.term.cols = globalThis.PANE.cols; this.term.rows = globalThis.PANE.rows; } }
      export class WebglAddon {} export class Unicode11Addon {} export class WebLinksAddon {} export class SearchAddon {}
    `;
class Element extends EventTarget {
  isConnected = true; offsetParent = {}; className = 'x'; tagName = 'DIV';
  classList = { toggle() {}, add() {}, remove() {}, contains: () => false };
  setAttribute(k,v) { this[k]=v; }
  append() {} remove() {} contains(node) { return node === this; } querySelector() { return null; }
  getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 100 }; }
}
const win = new EventTarget();
const doc = new EventTarget();
let over = null;   // what elementFromPoint returns
Object.assign(doc, { createElement: () => new Element(), documentElement: {}, hidden: false, focused: true, activeElement: null });
doc.hasFocus = () => doc.focused;
doc.elementFromPoint = () => over;
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
  static count = 0;
  readyState = 1; sent = [];
  constructor() { FakeSocket.last = this; FakeSocket.count++; }
  send(m) { this.sent.push(typeof m === 'string' ? JSON.parse(m) : { bin: m.length }); }
  close() { this.readyState = 3; }
  server(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
  resizes() { return this.sent.filter((m) => m.t === 'resize').map((m) => `${m.c}x${m.r}`); }
}
globalThis.WebSocket = FakeSocket;

const moduleUrl = new URL('../../piper/web/dist/term.js', import.meta.url);
const source = readFileSync(moduleUrl, 'utf8').replace(/from '([^']+)'/g, (_match, specifier) =>
  `from '${specifier.startsWith('@xterm/') ? 'data:text/javascript;base64,' + Buffer.from(renderer).toString('base64') : new URL(specifier, moduleUrl).href}'`);
const { TermView, termDiagText } = await import('data:text/javascript;base64,' + Buffer.from(source + '\n//# sourceURL=' + moduleUrl.href).toString('base64'));

function connected(id) {
  const view = new TermView(id, '/project', { onRefused() {}, onExit() {}, onConnection() {}, onImage: async () => '' });
  view.connect();
  const ws = FakeSocket.last;
  ws.onopen();
  ws.server({ t: 'hello', info: { cols: 124, rows: 47, tty: 0 } });
  flush();
  ws.server({ t: 'size', c: 124, r: 47 });
  ws.sent.length = 0;
  return { view, ws };
}

test('a phone shrank the PTY and this window yielded: Redraw takes the size back', () => {
  doc.focused = false;   // the PC window in the background while the phone is used
  const { view, ws } = connected('phone');
  ws.server({ t: 'size', c: 38, r: 32 });   // the phone fits the PTY to its narrow pane
  flush();
  assert.deepEqual(ws.resizes(), [], 'a window without focus does not fight');
  doc.focused = true;   // back at the PC, focus already in the window: no focus event
  assert.equal(view.term.cols, 38, 'the view follows the small PTY');
  ws.sent.length = 0;
  view.redraw();
  flush();
  assert.deepEqual(ws.resizes(), ['38x31', '124x47'], 'a real change for the PTY first, then this pane\'s size');
  assert.equal(view.term.cols, 124);
  assert.ok(view.term.focused, 'the keyboard goes to the terminal');
  view.dispose();
});

test('Redraw in sync still sends the size (SIGWINCH: Claude Code redraws) and bounces xterm\'s grid', () => {
  const { view, ws } = connected('sync');
  globalThis.GRIDS = [];
  view.redraw();
  flush();
  assert.deepEqual(globalThis.GRIDS, ['124x46'], 'one row less (never a column: full-width rules would wrap), then the fit');
  assert.equal(view.term.cols, 124, 'the fit brought the pane\'s width back');
  // Claude Code redraws (and re-sends its mouse / paste modes) only on a real size change: bounce the PTY too
  assert.deepEqual(ws.resizes(), ['124x46', '124x47'], 'one row shorter, then the pane\'s size again');
  assert.deepEqual(view.term.refreshed, [0, 45], 'every row of the bounced grid repainted');
  view.dispose();
});

test('Redraw claims its size; another focused window follows a claimed size instead of answering it', () => {
  doc.focused = true;
  const { view, ws } = connected('claim');
  view.redraw();
  flush();
  const sent = ws.sent.filter((m) => m.t === 'resize');
  assert.deepEqual(sent, [{ t: 'resize', c: 124, r: 46, claim: true }, { t: 'resize', c: 124, r: 47, claim: true }],
    'both Redraw resizes carry the claim');
  ws.sent.length = 0;
  globalThis.PANE = { cols: 124, rows: 47 };
  view.lastSize = '';   // a later ordinary fit claims nothing
  view.scheduleFit();
  flush();
  assert.deepEqual(ws.sent.filter((m) => m.t === 'resize'), [{ t: 'resize', c: 124, r: 47 }], 'the claim is one-shot');
  view.dispose();

  // the phone, focused too: a claimed foreign size is followed, an unclaimed one answered once (9.55)
  const phone = connected('phone2');
  phone.view.reassertAt = 0;
  phone.ws.server({ t: 'size', c: 200, r: 50, claim: true });
  flush();
  assert.deepEqual(phone.ws.resizes(), [], 'no answer to a claim');
  assert.equal(phone.view.term.cols, 200, 'it follows the claimed PTY size');
  phone.view.reassertAt = 0;
  phone.ws.server({ t: 'size', c: 90, r: 30 });
  flush();
  assert.deepEqual(phone.ws.resizes(), ['124x47'], 'an unclaimed foreign size is still answered once');
  phone.view.dispose();
});

test('Redraw of a hidden view does not leave xterm one column short', () => {
  const { view, ws } = connected('hidden');
  view.el.offsetParent = null;
  globalThis.GRIDS = [];
  view.redraw();
  flush();
  assert.deepEqual(globalThis.GRIDS, [], 'no bounce while hidden: the fit cannot run');
  assert.deepEqual(ws.resizes(), []);
  view.dispose();
});

test('Redraw reconnects a closed socket at once instead of waiting for the backoff', () => {
  const { view, ws } = connected('dead');
  ws.readyState = 3;
  const before = FakeSocket.count;
  view.redraw();
  assert.equal(FakeSocket.count, before + 1, 'a new socket');
  assert.notEqual(FakeSocket.last, ws);
  view.dispose();
});

test('__piperTermDiag reports what reached the view, what left and what was dropped and why', () => {
  const { view, ws } = connected('diag');
  const diag = () => window.__piperTermDiag().find((d) => d.id === 'diag');
  let d = diag();
  assert.equal(d.socket, 'open');
  assert.equal(d.grid, '124x47');
  assert.equal(d.mouse, 'any');
  assert.equal(d.dropped, 0);
  assert.equal(d.sentAt, 0);

  view.term.data('x');
  d = diag();
  assert.ok(d.dataAt > 0 && d.sentAt > 0, 'typed and sent');
  assert.deepEqual(ws.sent.at(-1), { t: 'in', d: 'x' });

  ws.readyState = 3;   // closed without this view noticing yet
  view.term.data('y');
  view.term.binary('\x1b[<64;1;1M');
  d = diag();
  assert.equal(d.socket, 'closed');
  assert.equal(d.dropped, 2);
  assert.equal(d.dropWhy, 'socket closed');

  view.host.dispatchEvent(new Event('wheel'));
  view.host.dispatchEvent(new Event('keydown'));
  d = diag();
  assert.ok(d.wheelAt > 0 && d.keyAt > 0, 'the wheel and a key reached the view');

  over = new Element();   // something else lies over the terminal's centre
  over.tagName = 'DIV'; over.className = 'modal';
  assert.match(diag().top, /^div\.modal \(OVER the terminal\)$/);
  over = null;
  view.dispose();
});

test('Copy terminal diagnostics: this session first, every open view, no terminal content', () => {
  globalThis.matchMedia = () => ({ matches: true });
  const a = connected('other');
  const b = connected('mine');
  b.view.term.data('secret typed text');
  const out = JSON.parse(termDiagText('mine', [a.view, b.view], new Date('2026-11-01T10:00:00Z')));
  assert.equal(out.at, '2026-11-01T10:00:00.000Z');
  assert.equal(out.session, 'mine');
  assert.equal(out.standalone, true, 'the Dock web app');
  assert.deepEqual(out.terminals.map((t) => t.id), ['mine', 'other']);
  assert.equal(out.terminals[0].socket, 'open');
  assert.ok(out.terminals[0].sentAt > 0);
  assert.doesNotMatch(JSON.stringify(out), /secret typed text/);
  a.view.dispose(); b.view.dispose();
});

test('Redraw ends a dead-key composition that never finished; the diag shows it and the last key', () => {
  const { view } = connected('ime');
  const helper = { _isComposing: true, _isSendingComposition: false };
  view.term._core = { _compositionHelper: helper };
  view.term.textarea = { value: '\u02c7', blur() { this.blurred = true; } };
  const diag = () => window.__piperTermDiag().find((d) => d.id === 'ime');
  assert.equal(diag().composing, true);
  assert.equal(diag().textareaLength, 1);
  const key = new Event('keydown');
  Object.assign(key, { key: 'a', keyCode: 65, isComposing: true });
  view.host.dispatchEvent(key);
  flush();
  assert.match(diag().lastKey, /^letter keyCode=65 composing=true target=div\.thost active=none prevented=false$/, 'the kind of key, never the key');
  view.redraw();
  assert.equal(helper._isComposing, false, 'xterm takes keys again');
  assert.ok(view.term.textarea.blurred);
  assert.equal(view.term.textarea.value, '');
  assert.equal(diag().composing, false);
  view.dispose();
});

test('the modes piperd sends before a replay are written into the terminal; anything else is not', () => {
  const { view, ws } = connected('modes');
  globalThis.WRITES = [];
  ws.server({ t: 'modes', d: '\x1b[?1049h\x1b[?1000h\x1b[?2004h' });
  ws.server({ t: 'modes', d: '\x1b]52;c;ZXZpbA==\x07' });   // not a mode sequence
  ws.server({ t: 'modes', d: 'text' });
  assert.deepEqual(globalThis.WRITES, ['\x1b[?1049h\x1b[?1000h\x1b[?2004h']);
  view.dispose();
});

 test('SSH channel closure is persistent, blocks input and reconnects explicitly without replay', () => {
  const {view,ws}=connected('piper-ref:ssh-fixture:session:remote-one');
  const count=FakeSocket.count;
  ws.server({t:'channel-closed',error:'SSH terminal connection dropped'});
  assert.equal(view.channelClosed,true);
  assert.match(view.channelText.textContent,/Terminal channel closed/);
  assert.equal(view.channelButton.textContent,'Reconnect terminal');
  view.term.data('NEVER-REPLAY'); view.term.binary('NEVER-BINARY');
  assert.equal(ws.sent.some(m=>m.t==='in'||m.bin),false);
  view.connect(); assert.equal(FakeSocket.count,count,'returning to the pane cannot silently reconnect');
  view.channelButton.dispatchEvent(new Event('click'));
  assert.equal(FakeSocket.count,count+1);
  const next=FakeSocket.last; next.onopen();
  next.server({t:'hello',info:{cols:124,rows:47,tty:0}});flush();
  assert.equal(view.channelClosed,false);
  assert.equal(next.sent.some(m=>m.t==='in'||m.bin),false);
  view.term.data('NEW-INPUT');
  assert.equal(next.sent.at(-1).d,'NEW-INPUT');
  next.onclose({code:1011});
  assert.equal(view.channelClosed,true);
  view.channelButton.dispatchEvent(new Event('click'));
  FakeSocket.last.onclose({code:1011});
  assert.match(view.channelText.textContent,/Reconnect failed/);
  assert.equal(view.channelButton.disabled,false);
  view.dispose();
});

 test('remote authentication refusal restores an actionable reconnect banner', () => {
  const {view,ws}=connected('piper-ref:ssh-fixture:session:auth-one');
  ws.server({t:'channel-closed'});
  view.channelButton.dispatchEvent(new Event('click'));
  FakeSocket.last.onclose({code:4401});
  assert.equal(view.channelClosed,true);
  assert.equal(view.channelButton.disabled,false);
  assert.match(view.channelText.textContent,/Access refused.*Reload Piper/);
  view.dispose();
});
 test('service-mode exited input refusal is a process refusal, not SSH channel loss', () => {
  const {view,ws}=connected('piper-ref:service-fixture:session:service-one');
  ws.server({t:'refused',error:'exited'});
  assert.equal(view.channelClosed,false);
  view.dispose();
});

test('SSH-only exited input refusal uses the persistent channel recovery', async () => {
  const {hosts}=await import('../../piper/web/dist/environment.js');
  hosts.set('ssh-fixture',{host:{mode:'ssh'},online:true});
  const {view,ws}=connected('piper-ref:ssh-fixture:session:legacy-channel');
  try {
    ws.server({t:'refused',error:'exited'});
    assert.equal(view.channelClosed,true);
    assert.match(view.channelDetail.textContent,/Remote session may still be running/);
  } finally { view.dispose(); hosts.delete('ssh-fixture'); }
});
