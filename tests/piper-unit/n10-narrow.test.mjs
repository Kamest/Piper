// node --test: N10 - Piper at 300 px: the whole queue folds for a move to another view, the
// rail tile shows its session, the Settings nav scrolls sideways, and every narrow rule sits under a phone-width query.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { defaultLayout, layoutReduce, queueShown } from '../../piper/web/dist/layout.js';
import { revealNavItem } from '../../piper/web/dist/settings.js';

const read = (p) => readFileSync(new URL(p, import.meta.url), 'utf8');
const main = read('../../piper/web/src/main.ts');
const db = read('../../piper/web/src/dbview.ts');
const css = read('../../piper/web/app.css');
const block = css.slice(css.lastIndexOf('/*', css.indexOf('N10: Piper at 300 px')), css.indexOf('/* ==== end N10 ==== */'));

const ctx = (o = {}) => ({
  view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: ['a'],
  hasPty: () => true, splitBlock: () => '', paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true, ...o,
});
const L = (o = {}) => ({ ...defaultLayout(true), left: 'queue', leftBack: 'queue', ...o });

test('N10: ≤ 640 px a move to another view folds the whole queue (Inbox / Production showed nothing under it)', () => {
  const s = layoutReduce(L(), { t: 'view', to: 'gitlab' }, ctx({ narrow: true })).state;
  assert.equal(s.leftNarrow, true);
  assert.equal(s.left, 'queue', 'the stored mode stays');
  assert.equal(queueShown(s, 'gitlab'), true, 'still "shown" - main.ts draws it as the strip');
  // wide: nothing changes (1100 / 1440 keep the queue beside the view)
  const wide = L();
  assert.equal(layoutReduce(wide, { t: 'view', to: 'gitlab' }, ctx()).state, wide);
  // the same view, an already folded queue or the strip: nothing to fold
  assert.equal(layoutReduce(L(), { t: 'view', to: 'term' }, ctx({ narrow: true })).state.leftNarrow, undefined);
  const folded = L({ leftNarrow: true });
  assert.equal(layoutReduce(folded, { t: 'view', to: 'prod' }, ctx({ narrow: true })).state, folded);
  const strip = L({ left: 'strip', leftBack: 'strip' });
  assert.equal(layoutReduce(strip, { t: 'view', to: 'prod' }, ctx({ narrow: true })).state, strip);
  // Files / HTTP keep their own fold beside it
  const f = layoutReduce(L(), { t: 'view', to: 'files' }, ctx({ narrow: true })).state;
  assert.equal(f.leftFold, true);
  assert.equal(f.leftNarrow, true);
  // Overview unfolds again after its own move to the terminal
  assert.equal(layoutReduce(L({ leftNarrow: true }), { t: 'overview', on: true }, ctx({ view: 'gitlab', narrow: true })).state.leftNarrow, false);
});

test('N10: main.ts gives the reducer the width; a rail tile folds the whole queue before it opens its session', () => {
  assert.match(main, /narrow: matchMedia\('\(max-width: 640px\)'\)\.matches,\n {2}\};/);
  assert.match(main, /qpeek\.hidden = true;\n {2}foldNarrowQueue\(\);[^\n]*\n {2}const keep = /);
});

test('N10: the open Settings section is scrolled into the sideways nav; the nav keeps scrollLeft across redraws', () => {
  const rect = (left, right, top = 10, bottom = 30) => ({ left, right, top, bottom });
  const mk = (l, r) => {
    const on = { getBoundingClientRect: () => rect(l, r) };
    return { scrollTop: 0, scrollLeft: 0, getBoundingClientRect: () => rect(60, 290, 0, 40), querySelector: () => on };
  };
  const right = mk(280, 340); revealNavItem(right); assert.equal(right.scrollLeft, 58);
  assert.equal(right.scrollTop, 0);
  const inside = mk(100, 160); revealNavItem(inside); assert.equal(inside.scrollLeft, 0);
  const left = mk(20, 80); left.scrollLeft = 100; revealNavItem(left); assert.equal(left.scrollLeft, 52);
  const src = read('../../piper/web/src/settings.ts');
  assert.match(src, /nav\.scrollTop = navTop;\n\s+nav\.scrollLeft = navLeft;\n\s+revealNavItem\(nav\);/);
});

test('N10: the Database view has ▤ Tables whenever a tab is open (CSS shows it only where the tree is folded)', () => {
  assert.match(db, /if \(connTabs\(\)\.length\) tabsEl\.append\(h\('button', \{ class: 'db-tab-tree'/);
  assert.match(css, /\.db-tab-tree \{ display: none;/, 'hidden by default: 1100 / 1440 unchanged');
  assert.match(block, /\.dbview \.db-tab-tree \{ display: inline-flex; \}/);
});

test('N10: every rule of the block is under a phone-width query (1100 / 1440 stay as they are)', () => {
  assert.ok(block.length > 500, 'the block is there');
  // outside the @media blocks only comments and whitespace
  let depth = 0, outside = '';
  for (const ch of block.replace(/\/\*[\s\S]*?\*\//g, '')) {
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    else if (depth === 0) outside += ch;
  }
  const queries = outside.trim().split(/\s+(?=@media)/).map((x) => x.trim());
  assert.deepEqual(queries, ['@media (max-width: 640px)', '@media (max-width: 480px)', '@media (max-width: 420px)']);
});

test('N10: the narrow rules - the queue column, the rail, the card, the session bar, Settings, Inbox, Database, Usage, ⌘K, Keys, Git', () => {
  for (const rule of [
    'body:not(.queue-mini):not(.no-queue) { --q-w: calc(100vw - var(--ab-w)); }',
    '#pending { right: 8px; bottom: 8px; width: calc(100vw - var(--ab-w) - 16px);',
    '.settings .st-side { display: block; white-space: nowrap; overflow: auto hidden;',
    '.settings .st-side > div.st-gtitle { display: none; }',
    '.settings .st-sec select { max-width: 100%; }',
    '.head.detail.one .layoutctl button.beside { font-size: 0;',
    '.head.detail.one .dh-top .actions { flex: none; flex-wrap: nowrap; gap: 0; }',
    ':root { --ab-w: 52px; }',
    '.board { padding: 8px 0; }',
    '.dbview:not(.wide):not(.compact):has(.db-tab):not(.db-treeon) { grid-template-rows: 0 minmax(0, 1fr); }',
    '.usage .utable { display: block; overflow-x: auto; }',
    '.palette .pitem > .name { flex: 0 0 auto; max-width: 72%; }',
    '.ks-find { order: 3; flex: 1 1 100%; width: auto; }',
    '.gw-body > .gw-side { display: none; }',
  ]) assert.ok(block.includes(rule), rule);
});
