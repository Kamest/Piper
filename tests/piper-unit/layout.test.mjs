// node --test: W12 one layout model - the reducer, the predicates, the persistence
// and the migration from the five keys before it; plus the wiring in main.ts (every input goes through dispatch).
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  editMembers, restoreMembers, defaultLayout, layoutReduce, queueShown, stripShown, gridShown,
  persisted, migrateLayout, layoutCommands, LAYOUT_TOASTS, LAYOUT_KEY, LEGACY_KEYS,
} from '../../piper/web/dist/layout.js';
import { GRID_MAX } from '../../piper/web/dist/gridlayout.js';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

/** a context: sessions a, b, c live (a active), d without a terminal */
const ctx = (o = {}) => {
  const live = new Set(o.live ?? ['a', 'b', 'c']);
  return {
    view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b', 'c', 'd'], gridIds: ['a', 'b', 'c'],
    hasPty: (id) => live.has(id), splitBlock: (id) => (live.has(id) ? '' : 'it has no terminal'),
    paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true, ...o,
  };
};
const L = (o = {}) => ({ ...defaultLayout(true), ...o });
const run = (s, a, c = ctx()) => layoutReduce(s, a, c);

test('Grid and project tools retain the active session after its terminal has ended or has not started', () => {
  const c = ctx({ live: [], gridIds: [], shown: [], hasSession: (id) => id === 'a' });
  const g = run(L(), { t: 'grid', on: true }, c);
  assert.deepEqual(g.fx.pick, ['a']);
  assert.equal(g.fx.add, true, 'first Grid opens Add beside the session');
  assert.equal(g.state.center.kind, 'grid');
  assert.equal(g.fx.refuse, undefined);
  assert.deepEqual(run(L(), { t: 'addCell' }, c).fx.pick, ['a'], 'Add also retains a session without a PTY');
  assert.ok(run(L(), { t: 'split' }, c).fx.refuse, 'ended / starting sessions are still not live split partners');
  for (const pane of ['files', 'db:here', 'http']) {
    const r = run(L(), { t: 'pane', pane }, c);
    assert.deepEqual(r.fx.pick, ['a', pane], 'the session remains on the left');
    assert.equal(r.state.center.kind, 'grid');
    const off = run(r.state, { t: 'pane', pane }, { ...c, gridIds: r.fx.pick, shown: r.fx.pick });
    assert.deepEqual(off.fx.pick, ['a']);
    assert.equal(off.state.center.kind, 'single');
  }
  const zoomed = L({ center: { kind: 'single', pane: 'http' } });
  assert.deepEqual(run(zoomed, { t: 'pane', pane: 'files' }, { ...c, gridIds: ['a', 'http'], shown: ['http'] }).fx.pick,
    ['a', 'files'], 'switching a zoomed project tool restores its agent to the left');
});

test('D1 ⌘O / ⌘D: tools beside Single, after focus in a multi-agent grid; on screen again = out', () => {
  // Single with no grid: a 2-pane grid [active, pane]
  const r = run(L(), { t: 'pane', pane: 'files' }, ctx({ gridIds: [] }));
  assert.deepEqual(r.state.center, { kind: 'grid' });
  assert.deepEqual(r.fx.pick, ['a', 'files']);
  // Single edits the visible workspace, never resurrects the hidden grid.
  const four = ['a', 'b', 'c', 'host-1'];
  const add = run(L(), { t: 'pane', pane: 'files' }, ctx({ gridIds: four }));
  assert.deepEqual(add.fx.pick, ['a', 'files']);
  assert.deepEqual(add.state.center, { kind: 'grid' });
  // In a multi-agent grid, a project tool is an ordinary cell inserted after focus.
  const g = L({ center: { kind: 'grid' } });
  assert.deepEqual(run(g, { t: 'pane', pane: 'db:here' }, ctx({ gridIds: ['b', 'a', 'c'], shown: ['b', 'a', 'c'] })).fx.pick, ['b', 'a', 'db:here', 'c']);
  assert.deepEqual(run(g, { t: 'pane', pane: 'db:here' }, ctx({ gridIds: ['b', 'a', 'c'], shown: ['b', 'a', 'c'], focus: 'c' })).fx.pick, ['b', 'a', 'c', 'db:here']);
  for (const pane of ['files', 'db:here', 'http']) {
    assert.deepEqual(run(g, { t: 'pane', pane }, ctx({ gridIds: ['a', 'b'], shown: ['a', 'b'], focus: 'a' })).fx.pick, ['a', pane, 'b']);
    assert.deepEqual(run(g, { t: 'pane', pane }, ctx({ gridIds: ['a', 'b'], shown: ['a', 'b'], focus: 'b' })).fx.pick, ['a', 'b', pane]);
    assert.deepEqual(run(g, { t: 'pane', pane }, ctx({ gridIds: ['a', 'b'], shown: ['a', 'b'], focus: 'missing' })).fx.pick, ['a', pane, 'b']);
  }
  // on screen: out (the grid stays its state; fewer than 2 panes show as Single in main.ts)
  const off = run(g, { t: 'pane', pane: 'files' }, ctx({ gridIds: ['a', 'b', 'files'], shown: ['a', 'b', 'files'] }));
  assert.deepEqual(off.fx.pick, ['a', 'b']);
  assert.equal(off.state.center.kind, 'grid');
  // A pane remembered in a hidden grid still opens beside just the visible terminal.
  const back = run(L(), { t: 'pane', pane: 'files' }, ctx({ gridIds: ['a', 'files'] }));
  assert.deepEqual(back.fx.pick, ['a', 'files']);
  assert.deepEqual(back.state.center, { kind: 'grid' });
  // a full grid refuses; from another tab the terminal shows
  const full = ['a', 'b', 'c', ...Array.from({ length: GRID_MAX - 3 }, (_, i) => `host-${i}`)];
  assert.equal(run(g, { t: 'pane', pane: 'files' }, ctx({ gridIds: full })).fx.refuse, `The grid has ${GRID_MAX} panes at most`);
  const fromFiles = run(g, { t: 'pane', pane: 'files' }, ctx({ view: 'files', gridIds: ['a', 'files'] }));
  assert.equal(fromFiles.state, g);
  assert.equal(fromFiles.fx.toTerm, true);
  assert.equal(run(L(), { t: 'pane', pane: 'files' }, ctx({ view: 'http' })).fx.toTerm, true);
  // 9.37: the grid module off - the beside pair still works (Basic preset), a bigger grid is refused
  const basic = run(L(), { t: 'pane', pane: 'files' }, ctx({ gridOn: false, gridIds: [] }));
  assert.deepEqual([basic.state.center, basic.fx.pick], [{ kind: 'grid' }, ['a', 'files']]);
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'pane', pane: 'db:here' }, ctx({ gridOn: false, gridIds: ['a', 'files'] })).fx.pick, ['a', 'db:here']);
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'pane', pane: 'files' }, ctx({ gridOn: false, gridIds: ['a', 'b'] })).fx.pick, ['a', 'files'], 'only the beside pair');
  assert.equal(run(L({ center: { kind: 'grid' } }), { t: 'add', id: 'b' }, ctx({ gridOn: false, gridIds: ['a', 'files'] })).fx.refuse, 'The grid is off (Settings > Features)');
  // the reasons: not a project session (main.ts paneBlock)
  assert.equal(run(L(), { t: 'pane', pane: 'db:here' }, ctx({ paneBlock: () => 'Files and Database panes belong to a project session' })).fx.refuse,
    'Files and Database panes belong to a project session');
});

test('Choose panes replaces the entire pick atomically and keeps only a selected active session', () => {
  const c = ctx({ gridIds: ['a', 'b', 'c'], shown: ['a', 'b', 'c'], adding: true, focus: 'c' });
  for (const s of [L(), L({ center: { kind: 'grid' } }), L({ center: { kind: 'single', pane: 'files' } })]) {
    for (const ids of [['a'], ['b'], ['b', 'c'], ['b', 'a'], ['d'], ['d', 'b'], ['files'], ['files', 'db:here']]) {
      const r = run(s, { t: 'choose', ids }, c);
      assert.deepEqual(r.fx.pick, ids, 'no previously shown or active pane can join the selection');
      assert.notEqual(r.fx.pick, ids, 'the applied pick is independent of the checkbox draft');
      assert.equal(r.fx.add, false, 'Show closes a pending Add cell');
      assert.equal(r.fx.focus, ids.some((id) => !['files', 'db:here'].includes(id)) ? 'detail' : undefined, 'a selected session receives keyboard focus after Show');
      assert.equal(r.fx.next, ids.includes('a') ? 'a' : ids[0], 'keep active when selected, otherwise focus a selected pane');
      assert.deepEqual(r.state.center, ids.length === 1 ? ids[0] === 'files' ? { kind: 'single', pane: 'files' } : { kind: 'single' } : { kind: 'grid' });
    }
    assert.deepEqual(run(s, { t: 'choose', ids: ['files', 'b', 'c'] }, c).fx.next, 'b', 'prefer a chosen session over a tool for the active project');
    assert.deepEqual(run(s, { t: 'choose', ids: ['d'] }, c).fx.pick, ['d'], 'a session without a PTY remains a valid choice');
    const empty = run(s, { t: 'choose', ids: [] }, c);
    assert.equal(empty.state, s);
    assert.ok(empty.fx.refuse, 'empty draft cannot show an unrelated session');
    assert.equal(empty.fx.pick, undefined);
  }
  assert.ok(run(L(), { t: 'choose', ids: ['a'] }, ctx({ gridOn: false })).fx.refuse);
  assert.ok(run(L(), { t: 'choose', ids: Array.from({ length: GRID_MAX + 1 }, (_, i) => `host-${i}`) }, c).fx.refuse);
});

test('Choose panes stores a session beside its project tools first, as the grid shows it', () => {
  // a session ticked after Files: the pick stayed ['files', 'b'] and the next added pane moved the agent right
  const c = ctx({ gridIds: ['a', 'files'], shown: ['a', 'files'] });
  assert.deepEqual(run(L(), { t: 'choose', ids: ['files', 'b'] }, c).fx.pick, ['b', 'files']);
  assert.deepEqual(run(L(), { t: 'choose', ids: ['db:here', 'files', 'b'] }, c).fx.pick, ['b', 'db:here', 'files']);
  // a multi-session grid keeps the order the user ticked (drag-arranged grids stay as they are)
  assert.deepEqual(run(L(), { t: 'choose', ids: ['host-1', 'b', 'db:1'] }, c).fx.pick, ['host-1', 'b', 'db:1']);
  assert.deepEqual(run(L(), { t: 'choose', ids: ['c', 'files', 'b'] }, c).fx.pick, ['c', 'files', 'b']);
});

test('D2 / 9.45 ⌘\\ adds the previous session (else the next live one off screen) after the focused pane; never closes', () => {
  const r = run(L(), { t: 'split' }, ctx({ gridIds: [] }));
  assert.deepEqual(r.state.center, { kind: 'grid' });
  assert.deepEqual(r.fx.pick, ['a', 'b'], '⌘\\ pairs with the previous session');
  // on screen already: the next one off screen joins, the grid is not collapsed
  const again = run(r.state, { t: 'split' }, ctx({ gridIds: ['a', 'b'], shown: ['a', 'b'] }));
  assert.deepEqual(again.fx.pick, ['a', 'c', 'b']);
  assert.equal(again.state.center.kind, 'grid');
  assert.equal(run(r.state, { t: 'split' }, ctx({ gridIds: ['a', 'b', 'c'], shown: ['a', 'b', 'c'] })).fx.refuse, 'No other session with a terminal to put beside this one');
  // from Files: the terminal shows
  assert.equal(run(r.state, { t: 'split' }, ctx({ view: 'files', gridIds: ['a', 'b'] })).fx.toTerm, true);
  // the previous one ended: the next live one; none: refused with the reason
  assert.deepEqual(run(L(), { t: 'split' }, ctx({ live: ['a', 'c'], gridIds: [] })).fx.pick, ['a', 'c']);
  assert.equal(run(L(), { t: 'split' }, ctx({ live: ['a'], lastActive: '' })).fx.refuse, 'No other session with a terminal to put beside this one');
  assert.equal(run(L(), { t: 'split' }, ctx({ gridOn: false })).fx.refuse, 'The grid is off (Settings > Features)');
  assert.deepEqual(run(L(), { t: 'splitWith', id: 'c' }, ctx({ gridIds: [] })).fx.pick, ['a', 'c']);
  assert.equal(run(L(), { t: 'splitWith', id: 'd' }).fx.refuse, 'Not beside: it has no terminal.');
});

test('9.45 ✕ / add / zoom / Add cell: the reducer edits the pick, the focus goes to the neighbour', () => {
  const g = L({ center: { kind: 'grid' } });
  const x = run(g, { t: 'remove', id: 'a' }, ctx({ gridIds: ['b', 'a', 'c'] }));
  assert.deepEqual([x.fx.pick, x.fx.next], [['b', 'c'], 'c'], '✕ on the active pane: the one after takes the focus');
  assert.equal(run(g, { t: 'remove', id: 'c' }, ctx({ gridIds: ['b', 'a', 'c'] })).fx.next, undefined, 'another pane: the focus stays');
  assert.deepEqual(run(g, { t: 'remove', id: 'files' }, ctx({ gridIds: ['a', 'files'], focus: 'files' })).fx.next, 'a');
  for (const pane of ['files', 'http', 'db:here']) {
    assert.equal(run(g, { t: 'remove', id: pane }, ctx({ gridIds: ['a', pane, 'b'], shown: ['a', pane, 'b'], focus: pane })).fx.next,
      'a', 'closing a project tool returns to its session, not the next terminal');
  }
  assert.deepEqual(run(g, { t: 'remove', id: 'a' }, ctx({ gridIds: ['a', 'files'], shown: ['a', 'files'] })).state.center,
    { kind: 'single', pane: 'files' }, 'removing the last terminal keeps the remaining tool on screen');
  const z = L({ center: { kind: 'single', pane: 'files' } });
  assert.deepEqual(run(z, { t: 'remove', id: 'files' }, ctx({ gridIds: ['a', 'files'] })).state.center, { kind: 'single' }, 'a zoomed pane removed');
  assert.deepEqual(run(L(), { t: 'add', id: 'host-1' }, ctx({ gridIds: ['a', 'b'], focus: 'a' })).fx.pick, ['a', 'host-1']);
  assert.equal(run(L(), { t: 'add', id: 'host-1' }, ctx({ gridOn: false })).fx.refuse, 'The grid is off (Settings > Features)');
  assert.deepEqual(run(g, { t: 'zoom', pane: 'files' }).state.center, { kind: 'single', pane: 'files' });
  assert.deepEqual(run(g, { t: 'zoom', pane: 'b' }).state.center, { kind: 'single' }, 'a session: Single of the active one');
  assert.deepEqual(run(z, { t: 'zoom', pane: '' }).state.center, { kind: 'grid' });
  const cell = run(L(), { t: 'addCell' }, ctx({ gridIds: [] }));
  assert.deepEqual([cell.state.center, cell.fx.pick, cell.fx.add], [{ kind: 'grid' }, ['a'], true]);
});

test('Single additions use only the visible pane, including remembered tools, full grids and stale focus', () => {
  const hidden = ['b', 'a', 'files', 'c', 'db:here', 'http', 'host-1', 'host-2', 'host-3'];
  for (const pane of ['files', 'db:here', 'http']) {
    const r = run(L(), { t: 'pane', pane }, ctx({ gridIds: hidden, shown: ['a'], focus: 'c' }));
    assert.deepEqual(r.fx.pick, ['a', pane]);
    assert.equal(r.fx.refuse, undefined);
    const off = run(r.state, { t: 'pane', pane }, ctx({ gridIds: r.fx.pick, shown: r.fx.pick }));
    assert.deepEqual(off.fx.pick, ['a'], 'closing the tool returns to one terminal');
    assert.equal(gridShown(off.state, 'term', off.fx.pick.length), false);
  }
  assert.deepEqual(run(L(), { t: 'split' }, ctx({ gridIds: hidden, shown: ['a'], focus: 'c' })).fx.pick, ['a', 'b']);
  assert.deepEqual(run(L(), { t: 'splitWith', id: 'c' }, ctx({ gridIds: hidden, shown: ['a'] })).fx.pick, ['a', 'c']);
  assert.deepEqual(run(L(), { t: 'addCell' }, ctx({ gridIds: hidden, shown: ['a'] })).fx.pick, ['a']);
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'pane', pane: 'files' },
    ctx({ gridIds: ['b'], shown: ['a'] })).fx.pick, ['a', 'files'], 'a derived Single follows the newly selected session');
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'add', id: 'host-4' },
    ctx({ gridIds: ['http'], shown: ['http'] })).fx.pick, ['http', 'host-4'], 'Add beside a lone tool keeps the tool');
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'activate', id: 'b' },
    ctx({ gridIds: ['a'], shown: ['a'], adding: true })).fx.pick, ['b'], 'queue selection replaces the lone pane beside Add');
  const z = L({ center: { kind: 'single', pane: 'http' } });
  assert.deepEqual(run(z, { t: 'add', id: 'host-4' }, ctx({ gridIds: hidden, shown: ['http'] })).fx.pick,
    ['http', 'host-4'], 'adding beside a zoomed tool never inserts an unseen terminal');
  const off = run(L({ center: { kind: 'single', pane: 'files' } }), { t: 'pane', pane: 'files' },
    ctx({ gridIds: hidden, shown: ['files'] }));
  assert.equal(off.state.center.kind, 'single', 'the zoomed tool toggle closes it');
  assert.ok(!off.fx.pick.includes('files'));
  assert.equal(off.fx.next, undefined, 'closing a zoomed tool keeps the active session');
  assert.equal(run(L({ center: { kind: 'grid' } }), { t: 'addCell' }, ctx({ gridIds: hidden, shown: hidden })).fx.refuse,
    `The grid has ${GRID_MAX} panes at most`);
  const alone = run(L(), { t: 'pane', pane: 'files' }, ctx({ live: [], gridIds: hidden }));
  assert.deepEqual(alone.fx.pick, ['a', 'files']);
  assert.deepEqual(alone.state.center, { kind: 'grid' }, 'no running terminal: the tool stays beside the session');
});

test('W12 #4 the agent drawer has the right column: ⌘I shows the context there, ⌘0 off gives back what was there', () => {
  let s = run(L({ right: 'context' }), { t: 'agent' }).state;
  assert.equal(s.right, 'agent');
  assert.equal(s.rightBack, 'context');
  s = run(s, { t: 'context' }).state;
  assert.equal(s.right, 'context', 'before: ⌘I under the drawer flipped a panel nobody saw');
  s = run(run(s, { t: 'agent' }).state, { t: 'agent', on: false }).state;
  assert.equal(s.right, 'context');
  const hid = run(run(L({ right: 'hidden', rightBack: 'hidden' }), { t: 'agent' }).state, { t: 'agent' }).state;
  assert.equal(hid.right, 'hidden');
  assert.equal(run(L(), { t: 'agent' }, ctx({ feedOn: false })).fx.refuse, 'The Feed is off - the Global agent has its terminal');
});

test('W12 #6 ⌘I in a global view says why, nothing changes', () => {
  const s = L();
  const r = run(s, { t: 'context' }, ctx({ view: 'audit', sessionView: false }));
  assert.equal(r.fx.refuse, LAYOUT_TOASTS.perSession);
  assert.equal(r.state, s);
});

test('≤ 640 px a pick from the whole queue folds it to the strip - not stored, ⌘B / Overview bring it back (merge review)', () => {
  let s = L({ left: 'queue', leftBack: 'queue' });
  s = run(s, { t: 'narrow' }).state;
  assert.equal(stripShown(s), true);
  assert.deepEqual([s.left, s.leftBack], ['queue', 'queue'], 'the user\'s mode stays');
  assert.deepEqual([persisted(s, false).left, persisted(s, false).leftBack], ['queue', 'queue']);
  assert.equal(stripShown(run(s, { t: 'view', to: 'gitlab' }).state), true, 'the picked item\'s view keeps it folded');
  const b = run(s, { t: 'strip' });
  assert.equal(stripShown(b.state), false);
  assert.equal(b.fx.focus, 'queue');
  assert.equal(stripShown(run(s, { t: 'overview' }).state), false);
  const strip = L({ left: 'strip', leftBack: 'strip' });
  assert.equal(run(strip, { t: 'narrow' }).state, strip, 'the strip: nothing to fold');
});

test('W12 #5 Files / HTTP fold the whole queue while there - Terminal brings it back, nothing stored', () => {
  let s = L({ left: 'queue', leftBack: 'queue' });
  s = run(s, { t: 'view', to: 'files' }).state;
  assert.equal(s.leftFold, true);
  assert.equal(stripShown(s), true);
  assert.equal(s.left, 'queue', 'the user\'s mode stays');
  assert.equal(persisted(s, false).left, 'queue', 'before: piper.queue.mode = strip for good');
  s = run(s, { t: 'view', to: 'http' }, ctx({ view: 'files' })).state;
  assert.equal(s.leftFold, true, 'Files → HTTP stays folded');
  s = run(s, { t: 'view', to: 'term' }, ctx({ view: 'http' })).state;
  assert.equal(s.leftFold, false);
  assert.equal(stripShown(s), false);
  // ⌘B in Files opens the whole queue there and stays so
  const f = run(run(L({ left: 'queue', leftBack: 'queue' }), { t: 'view', to: 'files' }).state, { t: 'strip' }, ctx({ view: 'files' }));
  assert.equal(f.state.leftFold, false);
  assert.equal(f.fx.focus, 'queue');
  assert.equal(run(f.state, { t: 'view', to: 'http' }, ctx({ view: 'files' })).state.leftFold, false, 'files → http keeps the choice');
  // the strip user: nothing folds
  assert.equal(run(L(), { t: 'view', to: 'files' }).state.leftFold, false);
});

test('W12 #6b ⌘B / Overview in the grid show the queue beside it; the grid stays', () => {
  const s = L({ left: 'hidden', leftBack: 'strip', center: { kind: 'grid', back: { kind: 'single' } } });
  const r = run(s, { t: 'queue' });
  assert.equal(r.state.left, 'strip');
  assert.equal(r.state.center.kind, 'grid', 'before: the queue turned the grid off');
  assert.equal(queueShown(r.state, 'term'), true);
  assert.equal(r.fx.hub, undefined);
  const strip = run(L({ center: { kind: 'grid', back: { kind: 'single' } } }), { t: 'strip' });
  assert.equal(strip.state.left, 'queue');
  assert.equal(strip.state.center.kind, 'grid');
  // from Settings: the hub
  assert.equal(run(L({ left: 'hidden' }), { t: 'queue' }, ctx({ view: 'settings' })).fx.hub, true);
  assert.equal(run(L(), { t: 'queue' }).state.left, 'hidden');
});

test('W12 #7 / 9.45 a queue click: in the grid = focus; a live one outside takes the focused pane; no terminal = alone', () => {
  const g = L({ center: { kind: 'grid' } });
  const c = (o = {}) => ctx({ gridIds: ['a', 'b', 'files'], shown: ['a', 'b', 'files'], ...o });
  assert.equal(run(g, { t: 'activate', id: 'b' }, c()).state, g, '⌘2 on a pane focuses it');
  assert.equal(run(g, { t: 'activate', id: 'b' }, c()).fx.pick, undefined);
  const r = run(g, { t: 'activate', id: 'c' }, c());
  assert.equal(r.state, g);
  assert.deepEqual(r.fx.pick, ['c', 'b', 'files'], 'the active (focused) session pane is replaced, the grid keeps its size');
  assert.deepEqual(run(g, { t: 'activate', id: 'c' }, c({ focus: 'b' })).fx.pick, ['a', 'c', 'files']);
  assert.deepEqual(run(g, { t: 'activate', id: 'd' }, c()).state.center, { kind: 'single' });
  assert.equal(run(g, { t: 'activate', id: 'd' }, c()).fx.pick, undefined, 'the grid keeps its panes');
  assert.equal(run(L(), { t: 'activate', id: 'c' }, c({ shown: [] })).fx.pick, undefined, 'Single stays Single');
});

test('W12 ⇧⌘F12 hides both sides, again restores them; ⌘I in it leaves it', () => {
  const s = L({ left: 'queue', leftBack: 'queue', right: 'context' });
  const z = run(s, { t: 'zen' });
  assert.equal(z.state.left, 'hidden');
  assert.equal(z.state.right, 'hidden');
  assert.equal(z.fx.toast, LAYOUT_TOASTS.zen);
  assert.deepEqual(persisted(z.state, false), persisted(s, false), 'a reload never comes back without panels');
  const back = run(z.state, { t: 'zen' }).state;
  assert.equal(back.left, 'queue');
  assert.equal(back.right, 'context');
  assert.equal(back.zen, null);
  const i = run(z.state, { t: 'context' }).state;
  assert.equal(i.zen, null);
  assert.equal(i.right, 'context');
  assert.equal(i.left, 'hidden');
});

test('W12 / D1 migrateLayout: piper.layout first, else the old keys, else the default; Beside and Split are not restored', () => {
  const get = (m) => (k) => (k in m ? m[k] : null);
  assert.equal(migrateLayout(get({}), 1399).right, 'hidden');
  assert.equal(migrateLayout(get({}), 1400).right, 'context');
  assert.equal(migrateLayout(get({ 'piper.ctx': '0' }), 1920).right, 'hidden');
  assert.equal(migrateLayout(get({ 'piper.ctx': '1' }), 1024).right, 'context');
  assert.equal('aside' in migrateLayout(get({ 'piper.side': '1', 'piper.sidedb': '1' }), 1400), false, 'the old Beside is gone (D1)');
  const h = migrateLayout(get({ 'piper.queue.hidden': '1', 'piper.queue.mode': 'full' }), 1400);
  assert.equal(h.left, 'hidden');
  assert.equal(h.leftBack, 'queue');
  assert.equal(migrateLayout(get({ 'piper.queue.mode': 'full' }), 1400).left, 'queue');
  // the new key wins; a stored grid stays a grid (its old `back` dropped), a split / Beside come back Single
  const stored = JSON.stringify({ v: 1, left: 'queue', leftBack: 'queue', right: 'hidden', aside: 'files', center: { kind: 'grid', back: { kind: 'split', partner: 'x' } } });
  const m = migrateLayout(get({ [LAYOUT_KEY]: stored, 'piper.ctx': '1' }), 1920);
  assert.equal(m.right, 'hidden');
  assert.deepEqual(m.center, { kind: 'grid' });
  assert.equal('aside' in m, false);
  assert.equal(migrateLayout(get({ [LAYOUT_KEY]: '{bad', 'piper.ctx': '0' }), 1920).right, 'hidden');
  assert.deepEqual(migrateLayout(get({ [LAYOUT_KEY]: JSON.stringify({ v: 1, center: { kind: 'split', partner: 'b' } }) }), 1920).center, { kind: 'single' });
  assert.deepEqual(LEGACY_KEYS, ['piper.ctx', 'piper.side', 'piper.sidedb', 'piper.queue.hidden', 'piper.queue.mode']);
});

test('W12 restore_view: the center is stored only with it; the agent drawer never', () => {
  const s = L({ center: { kind: 'grid' }, right: 'agent', rightBack: 'context' });
  assert.equal(persisted(s, false).center, undefined);
  assert.deepEqual(persisted(s, true).center, { kind: 'grid' });
  assert.equal(persisted(s, true).right, 'context');
  assert.equal(persisted(L(), true).center, undefined, 'single is the default');
});

test('W12 modules off: grid → Single, the drawer → what was there', () => {
  const c = ctx({ gridOn: false, feedOn: false });
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'reconcile' }, c).state.center, { kind: 'single' });
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'reconcile' }, { ...c, gridIds: ['a', 'b'] }).state.center, { kind: 'single' });
  assert.deepEqual(run(L({ center: { kind: 'grid' } }), { t: 'reconcile' }, { ...c, gridIds: ['a', 'files'] }).state.center, { kind: 'grid' }, '9.37: the beside pair stays');
  assert.equal(run(L({ right: 'agent', rightBack: 'context' }), { t: 'reconcile' }, c).state.right, 'context');
  assert.equal(run(L(), { t: 'grid' }, c).fx.refuse, 'The grid is off (Settings > Features)');
  const same = L();
  assert.equal(run(same, { t: 'reconcile' }).state, same, 'nothing to fix: the same object (no render)');
});

test('W12 #8 / 9.45 the palette\'s layout commands: labels by what is on screen, the pane count, no Split', () => {
  const cmds = layoutCommands(L(), 'term', 6, 0);
  assert.equal(cmds[0].label, 'Grid: up to 6 panes at once', 'before: "up to 4" with 6 panes');
  assert.equal(layoutCommands(L(), 'term', 6, 3)[0].label, 'Grid: its 3 panes');
  assert.equal(layoutCommands(L({ center: { kind: 'grid' } }), 'term', 6, 3)[0].label, 'Grid: off (Single, the grid keeps its panes)');
  assert.equal(layoutCommands(L({ center: { kind: 'grid' } }), 'files', 6, 0)[0].label, 'Grid: up to 6 panes at once');
  // one pane + the Add cell is a grid on screen (syncLayout): the palette offered "up to 6" and no way off (CI 320159)
  assert.equal(layoutCommands(L({ center: { kind: 'grid' } }), 'term', 6, 1, true)[0].label, 'Grid: off (Single, the grid keeps its panes)');
  assert.equal(layoutCommands(L({ center: { kind: 'grid' } }), 'term', 6, 1)[0].label, 'Grid: up to 6 panes at once');
  assert.equal(layoutCommands(L({ center: { kind: 'grid' } }), 'term', 6)[2].label, 'Single terminal (leave the grid)');
  assert.equal(layoutCommands(L({ zen: { left: 'strip', right: 'context' } }), 'term', 6)[3].label, 'Restore the panels');
  assert.deepEqual(cmds.map((x) => x.hint), ['⌘G', '', '', '⇧⌘F12']);
  assert.deepEqual(cmds[1].action, { t: 'addCell' });
  assert.ok(!/up to 4/.test(main), 'rail, palette, shortcuts: GRID_MAX');
});

test('W12 wiring: one layout state, every input dispatches, the session bar\'s layout control', () => {
  for (const fn of ['toggleQueue', 'toggleStrip', 'toggleInspector', 'toggleGrid', 'toggleSplit']) {
    const body = main.slice(main.indexOf(`function ${fn}(`)).split('\n}\n')[0];
    assert.match(body, /dispatch\(/, `${fn} goes through the reducer`);
  }
  assert.ok(!/function toggleSide\(/.test(main) && !/function setSideDb\(/.test(main) && !/t: 'aside'/.test(main), 'D1: no Beside, ⌘O / ⌘D = dispatch pane');
  assert.ok(!/^\s+grid = false;/m.test(main), 'activate decides (I4), no input turns the grid off on its own');
  assert.ok(!/piper\.(ctx|side|sidedb|queue\.hidden|queue\.mode)'/.test(main), 'one key: piper.layout');
  assert.equal([...main.matchAll(/^\s*(grid|queueStrip|inspector|drawer) = /gm)].length, 4, 'written in syncLayout only');
  assert.ok(!/^let (split|side|sideDb|sideFr) /m.test(main), 'D1/D2: no split / side state');
  assert.match(main, /applyLayout\(\{ t: 'activate', id \}\);/);
  assert.match(main, /applyLayout\(\{ t: 'view', to: v \}\);/);
  assert.match(main, /reconcileLayout\(\);/);
  assert.match(main, /const livePty = \(x: Session\): boolean => hasPty\(x\) && x\.state !== 'exited' && x\.state !== 'error';/,
    'browser: an ended partner keeps its pid, the split stayed on a dead terminal');
  assert.match(main, /hasPty: \(id\) => \{ const x = session\(id\); return !!x && livePty\(x\); \},/);
  assert.match(main, /return x && hasPty\(x\) && !livePty\(x\) \? 'this session has no terminal \(blocked, starting or ended\)' : splitBlock\(cur, x\);/,
    'browser: the next ⌘\\ paired with the ended partner again (lastActive)');
  assert.match(main, /e\.key === 'F12' && e\.shiftKey\) \{\s+\/\/ ⇧⌘F12/);
  assert.match(main, /'⇧f12'/, 'the terminal passes ⇧⌘F12 to the GUI');
  assert.match(readFileSync(new URL('../../piper/web/src/shortcuts.ts', import.meta.url), 'utf8'), /k: '⇧⌘F12', d: 'hide all panels \(again: back\)'/);   // D15: the Keys registry
  assert.match(main, /'Beside', open && n > 1 \? h\('span', \{ class: 'bcount' \}, String\(n\)\) : null/, 'F03 (9.69): the grid\'s pane count on Beside ▾');
  assert.ok(!/seg\('split'/.test(main), 'D2: Split is the grid\'s 2-pane preset');
  assert.match(main, /function layoutControl\(s: Session, live: boolean\): HTMLElement \{\n\s+const box = h\('div', \{ class: 'layoutctl' \}\);/, 'F03: Beside ▾ on every session tab');
  assert.match(main, /layoutChoices\(n, effectiveGridLayout\(n\), \(id\) => effectiveGridLayout\(n, id\) === id\)/, 'the preset follows the available space (9.135: grid-choices.test.mjs)');
  assert.ok(!/class: 'addpane'|seg\(open, 'grid'/.test(main), 'F03: no + Add pane, no Single | Grid in the session bar');
  assert.match(main, /if \(open\) dispatch\(\{ t: 'addCell' \}\); else toggleGrid\(true\);/, 'F03: Another session… adds a cell');
  assert.match(main, /'Choose panes…'/, 'the grid chooser moved into the layout popover (W26: panes)');
  assert.ok(!/'Choose for grid…'/.test(main) && !/'Grid off ⌘G'\), \(\) => toggleGrid\(false\)\)\);/.test(main), 'the grid\'s header buttons went');
  assert.match(css, /\.zenpill \{ position: fixed;/);
  assert.match(css, /\.layoutctl button\.beside\.on \{ color: var\(--fg\); background: var\(--tile\); \}/, 'F03: open grid = W11 tile');
});

test('9.38 / 9.45: the Grid click shows the grid\'s panes as they are; fewer than 2 = the Add cell beside them', () => {
  const r = run(L(), { t: 'grid' }, ctx({ gridIds: [] }));
  assert.deepEqual(r.state.center, { kind: 'grid' });
  assert.deepEqual(r.fx.pick, ['a']);
  assert.equal(r.fx.add, true);
  const saved = run(L(), { t: 'grid' }, ctx({ gridIds: ['a', 'b', 'c'] }));
  assert.equal(saved.fx.pick, undefined, 'the stored pick stays, Single never overwrote it');
  assert.equal(saved.fx.add, undefined);
  assert.deepEqual(run(L(), { t: 'grid' }, ctx({ gridIds: ['b', 'c'] })).fx.pick, ['b', 'c', 'a'], 'the active session joins');
  assert.equal(run(L({ center: { kind: 'grid' } }), { t: 'grid' }, ctx({ shown: ['a', 'b'] })).state.center.kind, 'single', 'on screen the click = Single');
  assert.equal(run(L({ center: { kind: 'single', pane: 'files' } }), { t: 'grid' }, ctx()).state.center.kind, 'grid', 'a zoom: back to the grid');
});


test('Grid count and restore exclude the ended session and include the newly starting session', () => {
  const c = ctx({ active: 'c', gridIds: ['a', 'b'], live: ['a'],
    hasSession: (id) => ['a', 'b', 'c'].includes(id), isEnded: (id) => id === 'b' });
  assert.deepEqual(restoreMembers(c), ['a', 'c']);
  const restored = run(L(), { t: 'grid', on: true }, c);
  assert.deepEqual(restored.fx.pick, ['a', 'c']);
  assert.equal(restored.fx.pick.length, restoreMembers(c).length);
  assert.deepEqual(restoreMembers({ ...c, gridIds: ['a', 'b', 'files', 'db:here'] }), ['a', 'files', 'db:here', 'c']);
  assert.deepEqual(restoreMembers({ ...c, active: 'b' }), ['a', 'b'], 'explicitly viewed ended session remains readable');
  assert.match(main, /const n = on\('grid'\) \? \(open \? gridMembers\(\) : restoreMembers\(layoutCtx\(\)\)\)\.length : 0;/);
});


test('all Grid restore entry points discard inactive finished sessions and keep starting sessions', () => {
  const c = ctx({ active: 'c', gridIds: ['a', 'b', 'd', 'files'], live: ['a'],
    hasSession: (id) => ['a', 'b', 'c', 'd'].includes(id), isEnded: (id) => ['b', 'd'].includes(id) });
  for (const center of [{ kind: 'single' }, { kind: 'single', pane: 'files' }]) {
    for (const action of [{ t: 'grid', on: true }, { t: 'grid' }, { t: 'zoom', pane: '' }]) {
      const result = run(L({ center }), action, c);
      assert.deepEqual(result.fx.pick, ['a', 'files', 'c'], JSON.stringify({ center, action }));
      assert.equal(result.state.center.kind, 'grid');
    }
  }
  const count = restoreMembers(c).length;
  assert.equal(layoutCommands(L(), 'term', GRID_MAX, c.gridIds.length, false, count)[0].label, `Grid: its ${count} panes`);
  assert.match(layoutCommands(L({ center: { kind: 'grid' } }), 'term', GRID_MAX, 1, false, 2)[0].label,
    /its 2 panes/, 'a collapsed one-pane grid must not be labelled as open using the restored count');
});


test('a full remembered grid comes back with the current session in the last session pane (9.101), never without it', () => {
  const c = ctx({ active: 'new', ids: ['new'], hasSession: () => true,
    gridIds: Array.from({ length: GRID_MAX }, (_, i) => `session-${i}`) });
  for (const action of [{ t: 'grid', on: true }, { t: 'zoom', pane: '' }]) {
    const r = run(L(), action, c);
    assert.equal(r.fx.refuse, undefined, 'the user report 2026-10-20: the refusal stuck until a pane was closed by hand');
    assert.equal(r.state.center.kind, 'grid');
    assert.deepEqual(r.fx.pick, [...c.gridIds.slice(0, GRID_MAX - 1), 'new']);
    assert.match(r.fx.toast, /took the place of the last one/);
  }
  const r = run(L(), { t: 'grid', on: true }, { ...c, isEnded: (id) => id === 'session-0' });
  assert.equal(r.fx.refuse, undefined);
  assert.equal(r.fx.pick.length, GRID_MAX);
  assert.ok(r.fx.pick.includes('new'));
  assert.ok(!r.fx.pick.includes('session-0'));
});


test('Add capacity counts the panes being edited, independently of a smaller restore preview', () => {
  const c = ctx({ view: 'files', active: 'a', gridIds: ['a', 'ended', ...Array.from({length: 7}, (_, i) => `s${i}`)],
    isEnded: (id) => id === 'ended' });
  const state = L({ center: { kind: 'grid' } });
  assert.equal(restoreMembers(c).length, 8);
  assert.equal(editMembers(state, c).length, GRID_MAX);
  assert.match(run(state, { t: 'addCell' }, c).fx.refuse, /9 panes/);
  assert.match(main, /const full = editMembers\(lay, layoutCtx\(\)\)\.length >= GRID_MAX/);
  assert.deepEqual(editMembers(L(), c), ['a'], 'Single can start a new pair even with a remembered full grid');
});


test('Single status panels restore their keyboard focus after a poll redraw', () => {
  const render = main.slice(main.indexOf('function renderMain()'), main.indexOf('// D7: under Conversation'));
  assert.match(render, /const panelFocused = !!panelEl\?\.contains\(document.activeElement\)/);
  assert.match(render, /if \(panelFocused\) focusDetail\(\)/);
});

test('Beside switches project tools; explicit Add and Choose retain multiple tools', () => {
  const g = L({ center: { kind: 'grid' } });
  const tools = ['files', 'db:here', 'http'];
  for (const gridOn of [false, true]) for (const before of tools) for (const pane of tools) {
    const ids = ['a', before];
    const r = run(g, { t: 'pane', pane }, ctx({ gridOn, gridIds: ids, shown: ids }));
    assert.deepEqual(r.fx.pick, before === pane ? ['a'] : ['a', pane]);
    assert.equal(r.fx.refuse, undefined);
  }
  const ids = ['a', 'files'];
  assert.deepEqual(run(g, { t: 'add', id: 'db:here' }, ctx({ gridIds: ids, shown: ids, focus: 'files' })).fx.pick,
    ['a', 'files', 'db:here']);
  assert.deepEqual(run(g, { t: 'choose', ids: ['a', ...tools] }).fx.pick, ['a', ...tools]);
  assert.deepEqual(run(g, { t: 'pane', pane: 'http' }, ctx({ gridIds: ['a', 'files', 'db:here'] })).fx.pick,
    ['a', 'http'], 'Beside also replaces an old stacked tool layout');
});
