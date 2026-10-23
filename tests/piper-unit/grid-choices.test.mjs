// node --test: Beside ▾'s Layout cards - one per distinct shape, only in the open grid with a choice,
// a card that does not fit looks disabled.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { layoutChoices, PRESETS, shapeOf } from '../../piper/web/dist/gridlayout.js';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const all = () => true;
const ids = (n, eff = 'auto', fits = all) => layoutChoices(n, eff, fits).map((c) => c.id + (c.on ? '*' : '') + (c.disabled ? '-' : ''));

test('two panes: only Auto (two columns) and Rows - Columns, 1 + rest, 2 × 2, … looked the same and a click did nothing', () => {
  assert.deepEqual(ids(2), ['auto*', 'rows']);
  assert.deepEqual(ids(2, '2x2'), ['auto', 'rows*'], 'a pick with the shape of Rows ticks Rows');
  assert.deepEqual(ids(2, 'columns'), ['auto*', 'rows']);
});

test('more panes: one card per arrangement, presets for fewer panes left out', () => {
  assert.deepEqual(ids(3), ['auto*', 'columns', 'rows', 'big-top', '2x2']);
  assert.deepEqual(ids(4), ['auto*', 'columns', 'rows', 'one-rest', 'big-top', '2x3']);
  assert.deepEqual(ids(7), ['auto*', 'columns', 'rows', 'one-rest', 'big-top', '3x3']);
  for (const n of [2, 3, 4, 5, 6, 7, 8, 9]) {
    const cs = layoutChoices(n, 'auto', all);
    assert.ok(cs.every((c) => n <= PRESETS.find((p) => p.id === c.id).cap), `n=${n}: a preset for fewer panes`);
    // one group of k is k groups of one the other way: compare the cells, not the keys
    const cells = cs.map((c) => { const s = shapeOf(c.id, n); return s.groups.length === 1 ? `${s.dir === 'col' ? 'row' : 'col'}:${Array(s.groups[0]).fill(1)}` : `${s.dir}:${s.groups}`; });
    assert.equal(new Set(cells).size, cells.length, `n=${n}: two cards with one arrangement`);
  }
});

test('nothing to choose = no Layout section: a single pane, or only one card fits (project tools to the right)', () => {
  assert.deepEqual(ids(1), []);
  assert.deepEqual(ids(0), []);
  assert.deepEqual(ids(2, 'one-rest', (id) => id === 'one-rest'), []);
  assert.deepEqual(ids(3, 'auto', (id) => id === 'auto' || id === 'rows'), ['auto*', 'columns-', 'rows', 'big-top-', '2x2-']);
});

test('main.ts: Layout only in the open grid, from layoutChoices; disabled cards say why', () => {
  assert.match(main, /const choices = on\('grid'\) && open \? layoutChoices\(n, effectiveGridLayout\(n\), \(id\) => effectiveGridLayout\(n, id\) === id\) : \[\];/);
  assert.match(main, /choices\.length \? h\('div', \{ class: 'sec-h lsec' \}, 'Layout'/);
  assert.match(main, /disabled: c\.disabled,\n      title: c\.disabled \? 'needs more space - a wider window or fewer panes'/);
  assert.doesNotMatch(main, /\.\.\.PRESETS\.map\(\(p\) => h\('button'/, 'every preset no longer listed');
});

test('a card that does not fit is visibly disabled', () => {
  const b = css.slice(css.indexOf('/* ==== 9.135'), css.indexOf('/* ==== end 9.135'));
  assert.match(b, /\.besidemenu \.lcard:disabled \{ opacity: \.4; cursor: default; \}/);
});
