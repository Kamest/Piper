// node --test: N4 - the Keys sheet: a fixed header with a filter, groups in columns, keys as chips.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { keyChips, filterShortcuts, SHORTCUTS } from '../../piper/web/dist/shortcuts.js';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const t = (k) => keyChips(k).map((c) => `${c.kind}:${c.t}`);

test('N4: keys become chips - alternatives with their separator, short keys in turn split, a phrase stays words', () => {
  assert.deepEqual(t('⌘K'), ['key:⌘K']);
  assert.deepEqual(t('j k / ↑↓'), ['key:j', 'key:k', 'sep:/', 'key:↑↓']);
  assert.deepEqual(t('Esc, Ctrl+C, Ctrl+K'), ['key:Esc', 'sep:·', 'key:Ctrl+C', 'sep:·', 'key:Ctrl+K']);
  assert.deepEqual(t('Mouse ◀ ▶ · ⌘[ ⌘]'), ['key:Mouse ◀ ▶', 'sep:·', 'key:⌘[', 'key:⌘]']);
  assert.deepEqual(t('e · g · /'), ['key:e', 'sep:·', 'key:g', 'sep:·', 'key:/']);
  assert.deepEqual(t('right click on a session'), ['text:right click on a session']);
  assert.deepEqual(t('Shift/⌥+Enter'), ['key:Shift/⌥+Enter'], 'a slash inside a key is not a separator');
  for (const g of SHORTCUTS) for (const r of g.rows) assert.ok(keyChips(r.k).some((c) => c.kind !== 'sep'), r.k);
});

test('N4: the filter matches a description, a key or a group title; an empty group goes', () => {
  assert.deepEqual(filterShortcuts(SHORTCUTS, '').length, SHORTCUTS.length);
  const diff = filterShortcuts(SHORTCUTS, 'DIFF');
  assert.deepEqual(diff.map((g) => g.title), ['Files']);
  assert.deepEqual(diff[0].rows.map((r) => r.k), ['⌘⇧D']);
  assert.equal(filterShortcuts(SHORTCUTS, 'database').find((g) => g.title === 'Database').rows.length,
    SHORTCUTS.find((g) => g.title === 'Database').rows.length, 'the group title shows all of it');
  assert.ok(filterShortcuts(SHORTCUTS, '⌘K').some((g) => g.rows.some((r) => r.k === '⌘K')));
  assert.deepEqual(filterShortcuts(SHORTCUTS, 'zzzz'), []);
});

test('N4: the sheet - header with the filter and ×, the body scrolls, the columns inside it only flow', () => {
  assert.match(main, /h\('div', \{ class: 'ks-head' \}, h\('h3', \{\}, 'Keyboard shortcuts'\), find,/);
  assert.match(main, /const body = h\('div', \{ class: 'ks-body' \}, cols\);/);
  assert.match(main, /'No key matches “'/);
  assert.doesNotMatch(main, /table', \{ class: 'keys' \}/, 'the old table is gone');
  const block = css.slice(css.indexOf('/* ==== 9.100 N4'), css.indexOf('/* ==== end N4 ==== */'));
  assert.match(block, /\.ks-body \{ flex: 1; min-height: 0; overflow-y: auto;/);
  assert.match(block, /\.ks-cols \{ columns: 2 360px;/);
  assert.doesNotMatch(block, /\.ks-body \{[^}]*columns/, 'columns on the scrolling box overflow sideways (Files was off-screen)');
  assert.match(block, /\.ks-row \{ display: flex; align-items: center; justify-content: space-between;/);
});
