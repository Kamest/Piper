// Files keys (phase 1c): one table drives the bindings and the Keys dialog; IntelliJ's keymap by default, Piper classic
// as the setting; keys bind to the physical key and an ⌥ key a layout types a character with stays the layout's.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { altOwned, FILE_KEYS, filesKeymapFor, keyName, shortcutsFor, shownShortcuts, SHORTCUTS } from '../../piper/web/dist/shortcuts.js';
import { caretClick, definitionClick, occurrences } from '../../piper/web/dist/cmeditor.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');

test('every Files key row has a binding that runs it, no key twice in a keymap', () => {
  const files = src('files.ts'), cmed = src('cmeditor.ts');
  const run = files.slice(files.indexOf('function runFileKey('), files.indexOf('// the Files keys before CodeMirror'));
  for (const [keymap, rows] of Object.entries(FILE_KEYS)) {
    const seen = new Set();
    for (const r of rows) {
      assert.ok(run.includes(`id === '${r.id}'`) || cmed.includes(`case '${r.id}':`), `${keymap} ${r.id}: no handler`);
      if (r.id !== 'addCaret' && r.id !== 'definition') assert.ok(r.keys.length, `${keymap} ${r.id}: no key`);
      for (const k of r.keys) { assert.ok(!seen.has(k), `${keymap}: ${k} twice`); seen.add(k); }
      assert.ok(r.d.length <= 60, r.d);
    }
  }
  // report §4.5: IntelliJ's keys
  const ij = Object.fromEntries(FILE_KEYS.intellij.map((r) => [r.id, r.keys]));
  assert.deepEqual(ij.recent, ['Mod-KeyE']);
  assert.deepEqual(ij.definition, ['Mod-KeyB']);
  assert.deepEqual(ij.gotoFile, ['Mod-Shift-KeyO']);
  assert.deepEqual(ij.outline, ['Mod-F12']);
  assert.deepEqual([ij.duplicateLine, ij.deleteLine], [['Mod-KeyD'], ['Mod-Backspace']]);
  assert.deepEqual([ij.moveLineUp, ij.extendSelection], [['Alt-Shift-ArrowUp'], ['Alt-ArrowUp']]);
  assert.deepEqual([ij.nextOccurrence, ij.allOccurrences, ij.replace], [['Ctrl-KeyG'], ['Ctrl-Mod-KeyG'], ['Mod-KeyR']]);
  assert.ok(ij.closeTab.includes('Alt-KeyW') && FILE_KEYS.classic.find((r) => r.id === 'closeTab').keys.includes('Alt-KeyW'));
  // classic keeps ⌘E / ⌘B for Piper (Composer, the queue strip)
  assert.ok(!FILE_KEYS.classic.some((r) => r.keys.includes('Mod-KeyE') || r.keys.includes('Mod-KeyB')));
});

test('the Keys dialog lists exactly the keymap in use (UX#10)', () => {
  for (const keymap of ['intellij', 'classic']) {
    const files = shownShortcuts(() => true, keymap).find((g) => g.title === 'Files').rows;
    for (const r of FILE_KEYS[keymap]) assert.ok(files.some((x) => x.k === r.k && x.d === r.d), `${keymap}: ${r.k}`);
    const other = FILE_KEYS[keymap === 'intellij' ? 'classic' : 'intellij'];
    for (const r of other) if (!FILE_KEYS[keymap].some((x) => x.k === r.k)) assert.ok(!files.some((x) => x.k === r.k), `${keymap} lists ${r.k}`);
  }
  assert.equal(SHORTCUTS, shortcutsFor('intellij'));
  const session = (keymap) => shortcutsFor(keymap).find((g) => g.title === 'Session').rows.find((r) => r.k === '⌘E').d;
  assert.match(session('intellij'), /outside Files/);
  assert.doesNotMatch(session('classic'), /outside/);
});

test('keys are physical; an ⌥ key the layout types an ASCII character with stays the layout\'s (report §4.5 rule 4)', () => {
  const ev = (code, m = {}) => ({ code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...m });
  assert.equal(keyName(ev('KeyE', { metaKey: true }), true), 'Mod-KeyE');
  assert.equal(keyName(ev('KeyG', { metaKey: true, ctrlKey: true }), true), 'Ctrl-Mod-KeyG');
  assert.equal(keyName(ev('ArrowUp', { altKey: true, shiftKey: true }), true), 'Alt-Shift-ArrowUp');
  assert.equal(keyName(ev('KeyE', { ctrlKey: true }), false), 'Mod-KeyE');   // Ctrl off a Mac is ⌘
  assert.equal(keyName(ev('KeyE', { metaKey: true }), false), '');           // the Windows key: none of ours
  const alt = (key, code) => altOwned({ key, code, altKey: true, metaKey: false, ctrlKey: false });
  assert.ok(alt('∑', 'KeyW'));          // US ⌥W
  assert.ok(!alt('|', 'KeyW'));         // a layout typing | with ⌥W keeps it
  assert.ok(!alt('@', 'KeyV'));
  assert.ok(alt('ArrowUp', 'ArrowUp'));
  assert.ok(altOwned({ key: 'e', code: 'KeyE', altKey: false, metaKey: true, ctrlKey: false }));
});

test('select all occurrences: the selection everywhere, from a caret its word as a whole word', () => {
  const text = 'int id = 1; idx = id + id;';
  const o = occurrences(text, 4, 6, true);
  assert.deepEqual(o.ranges, [[4, 6], [18, 20], [23, 25]]);
  assert.equal(o.main, 0);
  assert.deepEqual(occurrences(text, 12, 14, false).ranges.length, 4);   // a selection matches inside words too
  assert.deepEqual(occurrences(text, 3, 3, false).ranges, []);
});

test('off a Mac Files keeps Piper classic and the mouse follows ⌘ = Ctrl: Ctrl+click declaration, Alt+click caret', () => {
  assert.equal(filesKeymapFor('intellij', true), 'intellij');
  assert.equal(filesKeymapFor('intellij', false), 'classic');   // ⌃G, ⌃Tab, Ctrl+⌫ mean other things there
  assert.equal(filesKeymapFor('classic', true), 'classic');
  const m = (o) => ({ metaKey: false, ctrlKey: false, altKey: false, button: 0, ...o });
  assert.ok(definitionClick(m({ metaKey: true }), true) && !definitionClick(m({ ctrlKey: true }), true));
  assert.ok(definitionClick(m({ ctrlKey: true }), false) && !definitionClick(m({ metaKey: true }), false));
  assert.ok(!definitionClick(m({ metaKey: true, button: 1 }), true));
  assert.ok(caretClick(m({ altKey: true }), true) && caretClick(m({ altKey: true }), false));
  assert.ok(!caretClick(m({ altKey: true, metaKey: true }), true) && !caretClick(m({ altKey: true, ctrlKey: true }), false));
  assert.ok(!definitionClick(m({ altKey: true }), true) && !definitionClick(m({ altKey: true }), false));
});
