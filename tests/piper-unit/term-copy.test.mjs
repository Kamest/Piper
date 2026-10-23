// Exercise TermView's real event handlers; only its rendering addons and browser APIs are stubbed.
import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const renderer = `
      export class Terminal {
        options = {}; unicode = {}; parser = { registerOscHandler: (_n, fn) => { this.osc = fn; } };
        selection = ''; loadAddon() {} registerLinkProvider() {}
        attachCustomKeyEventHandler(fn) { this.key = fn; }
        onData(fn) { this.input = fn; } onBinary(fn) { this.binary = fn; }
        hasSelection() { return !!this.selection; } getSelection() { return this.selection; }
        dispose() {} focus() {} reset() {}
      }
      export class FitAddon {} export class WebglAddon {} export class Unicode11Addon {}
      export class WebLinksAddon {} export class SearchAddon {}
    `;
class Element extends EventTarget {
  style = {}; select() {} focus() {}
  append() {} remove() {} contains(node) { return node === this; }
}
globalThis.HTMLElement = Element;
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { platform: 'MacIntel' } });
globalThis.document = { createElement: () => new Element(), documentElement: {}, addEventListener() {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });
globalThis.ClipboardItem = class { constructor(data) { this.data = data; } };
let timeout;
globalThis.window = {
  addEventListener() {},
  setTimeout(fn) { timeout = fn; return 1; }, clearTimeout() { timeout = undefined; },
};
// Load the entire compiled module, replacing renderer imports only (no method extraction).
const moduleUrl = new URL('../../piper/web/dist/term.js', import.meta.url);
const source = readFileSync(moduleUrl, 'utf8').replace(/from '([^']+)'/g, (_match, specifier) =>
  `from '${specifier.startsWith('@xterm/') ? 'data:text/javascript;base64,' + Buffer.from(renderer).toString('base64') : new URL(specifier, moduleUrl).href}'`);
const { TermView, TERM_FLAGS } = await import('data:text/javascript;base64,' + Buffer.from(source + '\n//# sourceURL=' + moduleUrl.href).toString('base64'));
const tick = () => new Promise(setImmediate);
const osc = (text) => 'c;' + Buffer.from(text).toString('base64');
const key = (key, mods = {}) => ({ type: 'keydown', key, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, preventDefault() { this.prevented = true; }, ...mods });

function setup(write) {
  const calls = [], notices = [];
  navigator.clipboard = {
    write: write ?? (async (items) => { calls.push(await items[0].data['text/plain']); }),
    writeText: async (text) => { calls.push(text); },
  };
  const view = new TermView('test', '/project', { onRefused: (...args) => notices.push(args) });
  return { view, calls, notices };
}

test('Safari refuses write() at once after a cancelled one: the arriving selection is written without a toast', async () => {
  // Live Safari 27 log: the item of a plain click is rejected after COPY_WAIT_MS, the next write()
  // fails in 1 ms with NotAllowedError, its OSC 52 arrives ~10 ms later and writeText() succeeds.
  const { view, calls, notices } = setup(async () => { throw new DOMException('refused', 'NotAllowedError'); });
  view.host.dispatchEvent(new Event('mouseup'));
  await tick();
  view.term.osc(osc('Grüße aus Köln'));
  await tick();
  assert.deepEqual(notices, [], 'a successful second attempt must not toast');
  assert.deepEqual(calls, ['Grüße aus Köln']);
});

test('early rejection with writeText denied too is reported when the selected UTF-8 text arrives; Copy retries', async () => {
  const { view, calls, notices } = setup(async () => { throw new Error('NotAllowedError'); });
  let denied = 0;
  navigator.clipboard.writeText = async () => { denied++; throw new DOMException('denied', 'NotAllowedError'); };
  view.host.dispatchEvent(new Event('mouseup'));
  await tick();
  assert.equal(notices.length, 0, 'an ordinary click with no OSC52 must stay quiet');
  view.term.osc(osc('Grüße aus Köln'));
  await tick();
  assert.equal(denied, 1);
  assert.equal(notices.length, 1, 'early write failure must not disappear');
  assert.equal(notices[0][1].label, 'Copy');
  navigator.clipboard.writeText = async (text) => { calls.push(text); };
  notices[0][1].run();
  await tick();
  assert.deepEqual(calls, ['Grüße aus Köln']);
});

test('late rejection retries writeText; a failed retry stays visible, without unhandled rejections', async () => {
  let rejectWrite;
  const { view, calls, notices } = setup(() => new Promise((_r, reject) => { rejectWrite = reject; }));
  view.host.dispatchEvent(new Event('mouseup'));
  view.term.osc(osc('selection'));
  rejectWrite(new Error('NotAllowedError'));
  await tick();
  assert.deepEqual(notices, []);
  assert.deepEqual(calls, ['selection']);
  navigator.clipboard.writeText = async () => { throw new Error('still denied'); };
  view.host.dispatchEvent(new Event('mouseup'));
  view.term.osc(osc('again'));
  rejectWrite(new Error('NotAllowedError'));
  await tick();
  assert.equal(notices.length, 1);
  notices[0][1].run();
  await tick();
  assert.equal(notices.length, 2);
});

test('Cmd+C repeats the TUI selection after automatic copy, never sends input; native selection wins', async () => {
  const { view, calls } = setup();
  view.host.dispatchEvent(new Event('mouseup'));
  view.term.osc(osc('Codex selection'));
  await tick();
  calls.length = 0; // another app replaced the clipboard
  const event = key('c', { metaKey: true });
  assert.equal(view.term.key(event), false);
  assert.equal(event.prevented, true);
  await tick();
  assert.deepEqual(calls, ['Codex selection']);
  view.term.selection = 'xterm selection';
  view.term.key(key('c', { metaKey: true }));
  await tick();
  assert.deepEqual(calls, ['Codex selection', 'xterm selection']);
  assert.equal(view.term.key(key('c', { ctrlKey: true })), true, 'Ctrl+C on Mac stays interrupt');
});

test('a fresh mouse selection or terminal input invalidates cached text', async () => {
  for (const invalidate of [
    view => view.host.dispatchEvent(new Event('mousedown')),
    view => view.term.key(key('x')),
    view => view.term.key(key('Escape')),
  ]) {
    const { view, calls } = setup();
    view.term.osc(osc('old selection'));
    await tick();
    calls.length = 0;
    invalidate(view);
    const event = key('c', { metaKey: true });
    assert.equal(view.term.key(event), true);
    await tick();
    assert.deepEqual(calls, [], 'Cmd+C must not restore obsolete text');
  }
});

test('mouse reports keep the gesture armed; modifier keys preserve the TUI selection', async () => {
  const { view, calls } = setup();
  view.host.dispatchEvent(new Event('mouseup'));
  view.term.input('\x1b[<0;10;5m');
  view.term.binary('\x1b[M#!!');
  assert.ok(view.pendingCopy);
  view.term.osc(osc('mouse selection'));
  await tick();
  view.term.key(key('Meta', { metaKey: true }));
  view.term.key(key('c', { metaKey: true }));
  await tick();
  assert.deepEqual(calls, ['mouse selection', 'mouse selection']);
});

test('plain clicks expire quietly; disposal cancels a pending write', async () => {
  const { view, calls, notices } = setup();
  view.host.dispatchEvent(new Event('mouseup'));
  timeout();
  await tick();
  assert.deepEqual(notices, []);
  view.host.dispatchEvent(new Event('mouseup'));
  view.dispose();
  await tick();
  assert.equal(view.pendingCopy, null);
  assert.deepEqual(calls, []);
});

test('early denial followed by an ordinary click timeout stays quiet and handled', async () => {
  const { view, notices } = setup(async () => { throw new Error('denied'); });
  view.host.dispatchEvent(new Event('mouseup'));
  await tick();
  timeout();
  await tick();
  assert.deepEqual(notices, []);
});

test('Enter keeps the optional /copy gesture armed after invalidating the previous selection', async () => {
  const { view, calls } = setup();
  TERM_FLAGS.armEnter = true;
  try {
    view.term.key(key('Enter'));
    assert.ok(view.pendingCopy);
    view.term.osc(osc('command output'));
    await tick();
    assert.deepEqual(calls, ['command output']);
  } finally { TERM_FLAGS.armEnter = false; }
});

test('replayed OSC52 never replaces the clipboard or leaves a cached selection', async () => {
  const { view, calls } = setup();
  view.replaying = true;
  view.term.osc(osc('historical secret'));
  assert.equal(view.term.key(key('c', { metaKey: true })), true);
  await tick();
  assert.deepEqual(calls, []);
});

test('reconnect and respawn discard the previous selection before replay/reset', async () => {
  globalThis.location = { protocol: 'http:', host: 'localhost' };
  globalThis.WebSocket = class { close() {} };
  const { view, calls } = setup();
  view.term.osc(osc('before reconnect'));
  await tick();
  view.connect();
  assert.equal(view.term.key(key('c', { metaKey: true })), true);
  view.replaying = false;
  view.term.osc(osc('before respawn'));
  await tick();
  calls.length = 0;
  view.ws.onmessage({ data: JSON.stringify({ t: 'respawn' }) });
  assert.equal(view.term.key(key('c', { metaKey: true })), true);
  await tick();
  assert.deepEqual(calls, []);
});

test('Copy retry uses a gesture-based fallback when Clipboard API remains denied', async () => {
  const { view, notices } = setup(async () => { throw new DOMException('denied', 'NotAllowedError'); });
  let copied = '', removed = false, restored = false;
  const originalCreate = document.createElement;
  document.activeElement = Object.assign(new Element(), { focus() { restored = true; } });
  document.body = { append(el) { copied = el.value; } };
  document.createElement = () => Object.assign(new Element(), { remove() { removed = true; } });
  document.execCommand = command => command === 'copy';
  navigator.clipboard.writeText = async () => { throw new DOMException('denied', 'NotAllowedError'); };
  try {
    view.host.dispatchEvent(new Event('mouseup'));
    view.term.osc(osc('Grüße aus Köln'));
    await tick();
    notices[0][1].run();
    await tick();
    assert.equal(copied, 'Grüße aus Köln');
    assert.equal(notices.length, 1, 'successful fallback must not report another refusal');
    assert.ok(removed);
    assert.ok(restored);
  } finally {
    document.createElement = originalCreate;
    delete document.execCommand;
    delete document.body;
    delete document.activeElement;
  }
});


test('clipboard denial uses its own notification instead of reporting refused terminal input', async () => {
  const { view, notices } = setup();
  const copyNotices = [];
  view.ev.onCopyFailed = (...args) => copyNotices.push(args);
  navigator.clipboard.writeText = async () => { throw new DOMException('denied', 'NotAllowedError'); };
  view.term.osc(osc('selected text'));
  await tick();
  assert.equal(notices.length, 0);
  assert.equal(copyNotices.length, 1);
  assert.match(copyNotices[0][0], /Click Copy to retry/);
  assert.equal(copyNotices[0][1].label, 'Copy');
});
