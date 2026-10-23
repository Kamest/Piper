// node --test: W11 components - the one popup menu (menu.ts), the dialog layers (layers.ts),
// the one button set. Pure helpers from web/dist; structure checks over web/src and app.css.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { normalizeEntries, filterEntries, itemCount, menuMove, placeMenu, labelText, SEARCH_MIN } from '../../piper/web/dist/menu.js';
import { LayerStack, firstOver } from '../../piper/web/dist/layers.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const item = (label, extra = {}) => ({ label, run() {}, ...extra });
const kinds = (rows) => rows.map((r) => (r.kind === 'item' ? r.item.label : r.kind === 'group' ? `[${r.text}]` : '-'));

test('W11 normalizeEntries: no separator at an edge or twice, an empty group goes, undefined / false skipped', () => {
  assert.deepEqual(kinds(normalizeEntries([null, item('Open'), null, null, false, undefined, item('Stop'), null])), ['Open', '-', 'Stop']);
  assert.deepEqual(kinds(normalizeEntries([{ group: 'Session' }, null, { group: 'Order' }, item('Move up')])), ['[Order]', 'Move up'],
    'a group followed by a separator or another group has nothing under it');
  assert.deepEqual(kinds(normalizeEntries([item('a'), { group: 'Tail' }])), ['a']);
  assert.deepEqual(normalizeEntries([]), []);
});

test('W11 filterEntries: speed search keeps the matching items (any case) under their group, drops separators', () => {
  const rows = normalizeEntries([{ group: 'Session' }, item('Files'), item('Conversation'), null, { group: 'Order' }, item('Move up'), item('Move down'), item('Rename…')]);
  assert.deepEqual(kinds(filterEntries(rows, 'MOVE')), ['[Order]', 'Move up', 'Move down']);
  assert.deepEqual(kinds(filterEntries(rows, 'o')), ['[Session]', 'Conversation', '[Order]', 'Move up', 'Move down']);
  assert.deepEqual(filterEntries(rows, 'zzz'), [], 'no match: nothing (the menu says "No match")');
  assert.equal(filterEntries(rows, '  '), rows, 'blank: all');
  assert.equal(itemCount(rows), 5);
  // a Node label (DB Switch rows) filters by its text
  assert.equal(labelText({ textContent: 'prod / main' }), 'prod / main');
  assert.equal(SEARCH_MIN, 8);
  // an item's own search text wins over its label (DB Switch: the "dev" chip is not the connection's name)
  const sw = normalizeEntries([{ group: 'team-x/dbx' }, item({ textContent: 'devlocal' }, { search: 'local' }), item({ textContent: 'devreports' }, { search: 'reports' })]);
  assert.deepEqual(filterEntries(sw, 'dev'), [], 'browser: "dev" matched every connection');
  assert.equal(filterEntries(sw, 'rep').length, 2);
});

test('W11 a menu opens on its ticked item (IntelliJ: the current choice), Esc after a filter goes back to it', () => {
  const m = read('menu.ts');
  assert.match(m, /let focusIdx = -1;/);
  assert.match(m, /if \(focusIdx < 0\) focusIdx = Math\.max\(0, bs\.findIndex\(\(b\) => b\.getAttribute\('aria-checked'\) === 'true'\)\);/);
  assert.match(m, /if \(q\) \{ q = ''; focusIdx = -1; draw\(\); \}/);
  assert.match(read('dbview.ts'), /c\.name === conn \? 'on' : '', short\(c\)\]\)/, 'Switch searches the name only');
  assert.match(m, /texts\.find\(\(t\) => search !== undefined && t\.data === search\)/, 'a Node label marks the match in its searched text (board: acme_<order>)');
  assert.match(m, /marked\(it\.label, q, it\.search\)/);
});

test('W11 a pressed toggle is tile + a lighter frame, not green', () => {
  assert.match(css, /button\.on \{ background: var\(--tile\); border-color: var\(--mut\); color: var\(--fg\); \}/);
  assert.ok(!/^button\.on \{ border-color: var\(--ok\)/m.test(css), 'green is the primary action, not a state');
});

test('W11 menuMove: ↑↓ wrap, Home / End, other keys are not the menu\'s', () => {
  assert.equal(menuMove(3, 0, 'ArrowDown'), 1);
  assert.equal(menuMove(3, 2, 'ArrowDown'), 0);
  assert.equal(menuMove(3, 0, 'ArrowUp'), 2);
  assert.equal(menuMove(3, -1, 'ArrowUp'), 2);
  assert.equal(menuMove(3, 1, 'Home'), 0);
  assert.equal(menuMove(3, 1, 'End'), 2);
  assert.equal(menuMove(3, 1, 'a'), -1);
  assert.equal(menuMove(0, 0, 'ArrowDown'), -1);
});

test('W11 placeMenu: under the anchor right-aligned, flips up when it does not fit, stays 4 px inside the window', () => {
  const a = { left: 300, top: 100, right: 340, bottom: 120 };
  assert.deepEqual(placeMenu(a, 200, 150, 1000, 800), { left: 140, top: 124 });
  assert.deepEqual(placeMenu({ ...a, top: 700, bottom: 720 }, 200, 150, 1000, 800), { left: 140, top: 546 }, 'above');
  assert.deepEqual(placeMenu({ left: 0, top: 10, right: 20, bottom: 30 }, 200, 50, 1000, 800), { left: 4, top: 34 }, 'not off the left edge');
  assert.deepEqual(placeMenu({ x: 990, y: 790 }, 200, 150, 1000, 800), { left: 796, top: 646 }, 'a point near the corner');
  assert.deepEqual(placeMenu({ x: 10, y: 10 }, 200, 150, 1000, 800), { left: 10, top: 10 });
});

test('9.110 placeMenu: an anchor\'s DOMRect (it has x / y as well) is a rect - the menu goes under it, not over it', () => {
  const rect = { x: 300, y: 100, width: 40, height: 20, left: 300, top: 100, right: 340, bottom: 120 };
  assert.deepEqual(placeMenu(rect, 200, 150, 1000, 800), { left: 140, top: 124 });
  assert.deepEqual(placeMenu({ ...rect, y: 700, top: 700, bottom: 720 }, 200, 150, 1000, 800), { left: 140, top: 546 }, 'above');
});

test('W11 LayerStack: replace keeps the opener, over pushes, pop gives the opener back, clear goes top first', () => {
  const s = new LayerStack();
  assert.equal(s.open('git', 'queue', false), undefined, 'the first dialog');
  assert.equal(s.open('git2', 'ignored', false), 'git', 'modal() replaces the top: the old layer comes back to remove');
  assert.deepEqual(s.all(), ['git2']);
  assert.equal(s.open('push', 'pushBtn', true), undefined);
  assert.equal(s.depth, 2);
  assert.equal(s.top(), 'push');
  assert.equal(s.open('push2', 'x', false), 'push', 'a re-render of the top layer replaces only it');
  assert.deepEqual(s.pop(), { layer: 'push2', back: 'pushBtn' }, 'Esc: the push dialog goes, the focus to its button');
  assert.equal(s.top(), 'git2');
  s.open('hist', 'histBtn', true);
  assert.deepEqual(s.clear().map((x) => x.back), ['histBtn', 'queue'], 'leaving: every layer; the last one is the first opener');
  assert.equal(s.depth, 0);
  assert.equal(s.pop(), undefined);
});

test('W11 firstOver: a dialog that redraws itself adds one layer, not one per draw', () => {
  const calls = [];
  const m = firstOver((t) => calls.push(['over', t]), (t) => calls.push(['replace', t]));
  m('History'); m('History · blame'); m('History · commit');
  assert.deepEqual(calls, [['over', 'History'], ['replace', 'History · blame'], ['replace', 'History · commit']]);
});

test('W11 structure: one menu, dialogs as layers, one button set', () => {
  const files = readdirSync(SRC).filter((f) => f.endsWith('.ts'));
  for (const f of files) {
    const src = read(f);
    if (f !== 'menu.ts') {
      const roles = [...src.matchAll(/role: 'menu'/g)].length;
      const allowed = f === 'main.ts' ? 1 : 0;   // the grid's layout popover (a picker, not a menu)
      assert.ok(roles <= allowed, `${f}: a popup menu of its own (role: 'menu') - use menu.ts`);
    }
    for (const old of ['db-btn', 'ibx-small', 'ibx-icon', 'ibx-primary', "'ibx-chip'", 'stackmenu', 'db-menu', 'db-mi\'', 'cmenu-sep', 'db-msep']) {
      assert.ok(!src.includes(old), `${f}: ${old} is gone (W11)`);
    }
  }
  for (const old of ['.db-btn', '.ibx-small', '.ibx-icon', '.ibx-primary', 'button.ibx-chip', '.stackmenu', '.db-menu', '.db-mi ', '.db-mi.', '.cmenu-sep', '.db-msep', '.db-shade']) {
    assert.ok(!css.includes(old), `app.css: ${old} is gone (W11)`);
  }
  assert.ok(!/one overlay/.test(read('history.ts')) && !/one overlay/.test(read('gitwindow.ts')), 'dialogs can stack now');
  assert.match(css, /\.olayer \{ position: absolute; inset: 0; display: flex; \}/);
  assert.match(css, /\.olayer \+ \.olayer \{ background: rgba\(11, 14, 18, \.55\); \}/, 'a layer over another dims it');
});

test('W11 wiring: Git window → Push / History over it; a review\'s task over the review; navigation closes every layer', () => {
  const main = read('main.ts');
  assert.match(main, /function closeModal\(\): void \{\s*const gone = layers\.pop\(\);/, 'closeModal takes only the top layer');
  assert.match(main, /for \(const l of layers\.all\(\)\) l\.inert = l !== layer;/, 'the lower layers are inert');
  assert.match(main, /const top = topLayer\(\);\s*if \(top\?\.dataset\.dirty && !top\.dataset\.escArmed\)/, 'the unsaved-text Esc guard is per layer');
  assert.match(main, /openSession: \(id\) => \{ closeAllModals\(\); select\(id\)/);
  assert.match(main, /openRailMenu\(s, r\.left - 180, r\.bottom \+ 4, \(\) => document\.querySelector<HTMLElement>\('button\[aria-label="more actions"\]'\)\?\.focus\(\)\);/,
    'the session bar redraws on every poll: Esc in its menu focuses the ⋯ that is there then (browser: BODY before)');
  assert.match(main, /openRailMenu\(s, e\.clientX[^]*?\(\) => document\.querySelector<HTMLElement>\(`\.qm\[data-sid=/,
    'the compact rail\'s menu gives the focus back to its tile (the queue row is hidden there)');
  assert.match(main, /label: 'Open', icon: 'fi-open', keys: '↵', run: open\('term'\), disabled: s\.id === active && !grid && view === 'term' && 'this session is open'/);
  const gw = read('gitwindow.ts');
  assert.match(gw, /onclick: \(\) => deps\.push!\(\(\) => \{ if \(win\.isConnected\) \{ void loadBranches\(\); void loadRows\(\); \} \}\) \}, 'Push'\)/,
    'Push keeps the window and reloads it after the push');
  assert.match(gw, /onclick: \(\) => deps\.history!\(project, f\.path\) \}, 'History'\)/, 'History keeps the window');
  assert.equal([...gw.matchAll(/win\.closest\('#overlay'\)/g)].length, 2, 'a window frees its editors when its layer goes');
  const gp = read('gitpanel.ts');
  assert.match(gp, /modal: done \? over\(\) : deps\.modal/, 'the push dialog from the Git window is a layer');
  assert.match(gp, /\(back \? over\(\) : deps\.modal\)\('Review task · editable prompt', root\)/);
  assert.match(gp, /root\.closest\('#overlay'\)/, 'the review frees its viewer when its layer goes');
  assert.match(gp, /openFile: \(pj: string, f: string, line: number\) => \{ deps\.closeAllModals\?\.\(\); deps\.openFile!/, 'a file from History over the window leaves both');
});
