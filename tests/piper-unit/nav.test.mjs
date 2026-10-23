// Back / forward through the GUI (policy nav_history): the history of places.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { NavHistory, samePlace } from '../../piper/web/dist/navhistory.js';

const term = { session: 's1', view: 'term' };
const file = (line) => ({ session: 's1', view: 'files', file: { project: 'p', path: 'A.java', line } });
const db = { session: 's1', view: 'db' };

test('back walks terminal <- file <- db, forward returns, a new stop clears forward', () => {
  const n = new NavHistory();
  n.visit(term);
  n.visit(file(36));
  n.visit(db);
  assert.deepEqual(n.goBack(), file(36));
  assert.deepEqual(n.goBack(), term);
  assert.equal(n.goBack(), null);
  assert.deepEqual(n.goForward(), file(36));
  n.visit({ session: 's2', view: 'term' });
  assert.equal(n.goForward(), null);
  assert.deepEqual(n.goBack(), file(36));
});

test('the same stop is recorded once; a far line in the same file is a new stop', () => {
  const n = new NavHistory();
  n.visit(term);
  n.visit(term);
  n.visit(file(10));
  n.visit(file(11));
  assert.ok(samePlace(file(10), file(12)));
  n.visit(file(80));
  assert.deepEqual(n.goBack(), file(11));
  assert.deepEqual(n.goBack(), term);
  assert.equal(n.goBack(), null);
});

test('capped, and a forgotten session drops its stops', () => {
  const n = new NavHistory(3);
  for (let i = 0; i < 6; i++) n.visit({ session: `s${i}`, view: 'term' });
  assert.deepEqual([n.goBack(), n.goBack(), n.goBack(), n.goBack()].map((p) => p?.session ?? null), ['s4', 's3', 's2', null]);
  const m = new NavHistory();
  m.visit(term);
  m.visit({ session: 's2', view: 'term' });
  m.visit(db);
  m.prune((id) => id !== 's2');
  assert.deepEqual(m.goBack(), term);
});

test('N8 / D2: ⌘\\ outside the terminal view shows the 2-pane grid, never flips one nobody sees', async () => {
  // W12: the rule lives in layout.ts (layout.test.mjs); toggleSplit only dispatches, the terminal view first (N10-D-12)
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  const fn = src.slice(src.indexOf('function toggleSplit(): void {'), src.indexOf('function splitWith('));
  assert.match(fn, /dispatch\(\{ t: 'split' \}\);/);
  const toolbar = src.slice(src.indexOf('function besideMenu('), src.indexOf('function loadGridPick('));
  assert.doesNotMatch(toolbar, /onclick: toggleSplit|}, 'Split'/, 'the standalone Split button is removed');
  assert.match(toolbar, /'Another session…'/, 'adding panes remains available (F03: in Beside ▾)');
  assert.match(src, /if \(first\.fx\.toTerm && view !== 'term'\) \{\s*if \(toTerm\(run\)\) run\(\);\s*return;/);
  const { layoutReduce, defaultLayout } = await import('../../piper/web/dist/layout.js');
  const c = { view: 'files', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: ['a', 'b'], hasPty: () => true, splitBlock: () => '',
    paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true };
  const sp = { ...defaultLayout(true), center: { kind: 'grid' } };
  const r = layoutReduce(sp, { t: 'split' }, c);
  assert.equal(r.state.center.kind, 'grid', 'in Files: the 2-pane grid stays');
  assert.equal(r.fx.toTerm, true);
  const g = layoutReduce(sp, { t: 'split' }, { ...c, view: 'term', ids: ['a', 'b', 'c', 'd'], gridIds: ['a', 'b', 'c'], shown: ['a', 'b', 'c'] });
  assert.deepEqual([g.state.center.kind, g.fx.pick], ['grid', ['a', 'd', 'b', 'c']], '9.45: in a bigger grid ⌘\\ adds one off screen, never collapses it');
});

test('N8: a new order / pin of the sessions redraws the strip with the queue (tile n = ⌘n at once, not after the next poll)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  for (const name of ['function saveRailOrder(', 'function togglePin(']) {
    const body = src.slice(src.indexOf(name), src.indexOf('\n}\n', src.indexOf(name)));
    assert.match(body, /queue\.render\(\);\s*renderQueueMini\(\);/, name);
  }
});

test('N9: a strip tile opens the session menu on right click / the ContextMenu key, as Keyboard shortcuts promise', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /tile\.addEventListener\('contextmenu', \(e\) => \{ e\.preventDefault\(\); qpeek\.hidden = true; openRailMenu\(s,/);
  assert.match(src, /tile\.addEventListener\('keydown', \(e\) => \{\s*if \(e\.key !== 'ContextMenu'\) return;/);
});

test('N9: without a session the palette does not offer the session views that would show an empty page', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  const esc = (x) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  for (const label of ["'Project files'", "'Search the project…'", "'Conversation', hint: 'active session",
    "'Session history'", "'Context panel: issue, Git, diagnostics, isolation'"]) {
    assert.match(src, new RegExp('if \\(shown0\\) go\\(\\{ label: ' + esc(label)), label);   // F18: in the Go to group
  }
});

test('N9: the Access to Piper dialog closes when another tab stores a token (storage event), then polls', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /addEventListener\('storage', \(e\) => \{\s*if \(!tokenAsked \|\| e\.key !== 'piper\.token' \|\| !e\.newValue\) return;\s*tokenAsked = false;\s*closeModal\(\);\s*startBackground\(\);\s*void poll\(\);/);
});

test('N9: a closed "needs attention" card stays closed after a reload of the tab (sessionStorage)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /const askClosed = new Set<string>\(\(\(\) => \{ try \{ return JSON\.parse\(sessionStorage\.getItem\(ASK_CLOSED_KEY\)/);
  assert.match(src, /onclick: \(\) => \{ closeAsk\(`\$\{s\.id\}:\$\{s\.detail\}`\); pendingPick = ''; renderPending\(\); \}/);
});
