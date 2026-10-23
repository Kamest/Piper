// node --test: terminal copy/paste keys on every OS (web/src/clipkeys.ts)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { clipAction, isMac } from '../../piper/web/dist/clipkeys.js';

const k = (key, mods = {}) => ({ type: 'keydown', key, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...mods });

test('Windows/Linux: Ctrl+C copies only with a selection, otherwise stays the interrupt', () => {
  assert.equal(clipAction(k('c', { ctrlKey: true }), true, false), 'copy');
  assert.equal(clipAction(k('c', { ctrlKey: true }), false, false), null);
  assert.equal(clipAction(k('C', { ctrlKey: true, shiftKey: true }), false, false), 'copy');
  assert.equal(clipAction(k('Insert', { ctrlKey: true }), true, false), 'copy');
});

test('Windows/Linux: Ctrl+V, Ctrl+Shift+V and Shift+Insert paste', () => {
  assert.equal(clipAction(k('v', { ctrlKey: true }), false, false), 'paste');
  assert.equal(clipAction(k('V', { ctrlKey: true, shiftKey: true }), false, false), 'paste');
  assert.equal(clipAction(k('Insert', { shiftKey: true }), false, false), 'paste');
});

test('other keys, keyup, Alt/AltGr and macOS are left alone', () => {
  assert.equal(clipAction(k('k', { ctrlKey: true }), true, false), null);
  assert.equal(clipAction({ ...k('v', { ctrlKey: true }), type: 'keyup' }, false, false), null);
  assert.equal(clipAction(k('v', { ctrlKey: true, altKey: true }), false, false), null);   // AltGr on a Czech layout
  assert.equal(clipAction(k('c', { metaKey: true }), true, true), 'copy');                 // ⌘C: xterm or cached TUI selection
  assert.equal(clipAction(k('c', { metaKey: true }), false, true), null);
  assert.equal(clipAction(k('c', { metaKey: true, shiftKey: true }), true, true), null);
  assert.equal(clipAction(k('c', { ctrlKey: true }), true, true), null);                   // Ctrl+C on macOS: the interrupt
});

test('platform detection', () => {
  assert.ok(isMac('MacIntel'));
  assert.ok(isMac('iPad'));
  assert.ok(!isMac('Win32'));
  assert.ok(!isMac('Linux x86_64'));
});

test('macOS: Cmd+Left / Right move to the beginning / end of the input line', async () => {
  const { lineKey } = await import('../../piper/web/dist/clipkeys.js');
  assert.equal(lineKey(k('ArrowLeft', { metaKey: true }), true), '\x01');
  assert.equal(lineKey(k('ArrowRight', { metaKey: true }), true), '\x05');
});

test('line navigation leaves other platforms, modifiers, composition and keyup alone', async () => {
  const { lineKey } = await import('../../piper/web/dist/clipkeys.js');
  for (const key of ['ArrowLeft', 'ArrowRight']) {
    assert.equal(lineKey(k(key, { metaKey: true }), false), null);
    assert.equal(lineKey(k(key), true), null);
    for (const mod of ['shiftKey', 'altKey', 'ctrlKey', 'isComposing']) {
      assert.equal(lineKey(k(key, { metaKey: true, [mod]: true }), true), null, mod);
    }
    for (const type of ['keyup', 'keypress']) {
      assert.equal(lineKey({ ...k(key, { metaKey: true }), type }, true), null, type);
    }
  }
  assert.equal(lineKey(k('ArrowUp', { metaKey: true }), true), null);
  assert.equal(lineKey(k('c', { metaKey: true }), true), null);
});

test('OSC 52: base64 text decoded as UTF-8, queries and garbage ignored', async () => {
  const { osc52Text } = await import('../../piper/web/dist/clipkeys.js');
  assert.equal(osc52Text('c;' + Buffer.from('https://claude.com/x?code=1').toString('base64')), 'https://claude.com/x?code=1');
  assert.equal(osc52Text(';' + Buffer.from('Grüße aus Köln').toString('base64')), 'Grüße aus Köln');
  assert.equal(osc52Text('c;?'), null);
  assert.equal(osc52Text('c;%%%'), null);
  assert.equal(osc52Text('no-separator'), null);
});

test('app shortcuts: ⌘ on a Mac, Alt elsewhere (not AltGr = Ctrl+Alt, not the Win key)', async () => {
  const { appMod, appKey } = await import('../../piper/web/dist/clipkeys.js');
  assert.equal(appMod(k('k', { metaKey: true }), true), true);
  assert.equal(appMod(k('k', { altKey: true }), true), false);
  assert.equal(appMod(k('k', { altKey: true }), false), true);
  assert.equal(appMod(k('k', { metaKey: true }), false), false);
  assert.equal(appMod(k('k', { ctrlKey: true, altKey: true }), false), false);
  // the number row by position: a Czech layout types ě for 2
  assert.equal(appKey({ key: 'ě', code: 'Digit2' }), '2');
  assert.equal(appKey({ key: 'K', code: 'KeyK' }), 'k');
  assert.equal(appKey({ key: ',' }), ',');
});

test('off a Mac Alt+B, Alt+F and Alt+P stay with the terminal (bash words, Claude Code model picker)', async () => {
  const { TERM_KEEPS } = await import('../../piper/web/dist/clipkeys.js');
  assert.deepEqual([...TERM_KEEPS].sort(), ['b', 'f', 'p']);
});

test('appKey: [ ] \\ by position (Czech ú ) ¨), with ⇧ the typed key as before (⌘⇧2 is not ⌘2)', async () => {
  const { appKey } = await import('../../piper/web/dist/clipkeys.js');
  assert.equal(appKey({ key: 'ú', code: 'BracketLeft' }), '[');
  assert.equal(appKey({ key: ')', code: 'BracketRight' }), ']');
  assert.equal(appKey({ key: '¨', code: 'Backslash' }), '\\');
  assert.equal(appKey({ key: '@', code: 'Digit2', shiftKey: true }), '@');
  assert.equal(appKey({ key: '[', code: 'Digit5' }), '[');   // a Czech Mac: ⌘⌥5
  assert.equal(appKey({ key: 'F', code: 'KeyF', shiftKey: true }), 'f');
});

test('keyLabel: off a Mac ⌘ is Alt for Piper\'s keys, Ctrl for the editor\'s / dialogs\'; a Mac keeps the glyphs', async () => {
  const { keyLabel } = await import('../../piper/web/dist/clipkeys.js');
  assert.equal(keyLabel('⌘K does everything', true), '⌘K does everything');
  assert.equal(keyLabel('⌘K does everything', false), 'Alt+K does everything');
  assert.equal(keyLabel('⌘⇧L', false), 'Alt+⇧L');
  assert.equal(keyLabel('⌘⌥↓', false, 'Ctrl'), 'Ctrl+Alt+↓');
  assert.equal(keyLabel('⌘↩ run · ⌘E explain · ⌃Space complete', false, 'Ctrl'), 'Ctrl+↩ run · Ctrl+E explain · Ctrl+Space complete');
  assert.equal(keyLabel('⌘D · ⌘/ · ⌥⇧↑↓', false, 'Ctrl'), 'Ctrl+D · Ctrl+/ · Alt+⇧↑↓');
  assert.equal(keyLabel('⌘/Ctrl+click opens Files', false), 'Ctrl+click opens Files');
  assert.equal(keyLabel('(⌘/⇧-click: IntelliJ)', false, 'Ctrl'), '(Ctrl/⇧-click: IntelliJ)');
  assert.equal(keyLabel('⌘+click / ⌥+click', false), 'Alt+click');   // one key off a Mac
  assert.equal(keyLabel('⌘[ / ⌘]', false), 'Alt+[ / Alt+]');
  assert.equal(keyLabel(keyLabel('⌘⇧F', false), false, 'Ctrl'), 'Alt+⇧F');   // idempotent
});
