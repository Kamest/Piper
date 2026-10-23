// node --test: a full grid (9 panes) left for another session - ⌘G brings it back with that session in it
// (it used to stay "the grid has 9 panes at most" until a pane was closed by hand).
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GRID_MAX } from '../../piper/web/dist/gridlayout.js';
import { defaultLayout, layoutReduce, restoreMembers } from '../../piper/web/dist/layout.js';

const nine = Array.from({ length: GRID_MAX }, (_, i) => `s${i}`);
const ctx = (o = {}) => ({ view: 'term', active: 'new', lastActive: 's8', ids: [...nine, 'new'], gridIds: nine,
  hasPty: () => true, splitBlock: () => '', paneBlock: () => '', gridOn: true, feedOn: false, sessionView: false, shown: ['new'], ...o });

test('⌘G from a session outside a full grid: it takes the last session pane, the grid opens, a toast says so', () => {
  const single = { ...defaultLayout(true), center: { kind: 'single' } };
  const r = layoutReduce(single, { t: 'grid' }, ctx());
  assert.equal(r.fx.refuse, undefined);
  assert.equal(r.state.center.kind, 'grid');
  assert.deepEqual(r.fx.pick, [...nine.slice(0, 8), 'new']);
  assert.match(r.fx.toast, /The grid was full \(9 panes\): this session took the place of the last one/);
});

test('the last session pane goes, not a tool pane; an ended session frees its place without a swap', () => {
  const mixed = [...nine.slice(0, 7), 's7', 'host-ab'];
  assert.deepEqual(restoreMembers(ctx({ gridIds: mixed })), [...nine.slice(0, 7), 'new', 'host-ab']);
  const r = layoutReduce(defaultLayout(true), { t: 'grid' }, ctx({ isEnded: (id) => id === 's3' }));
  assert.equal(r.fx.toast, undefined, 'room after the ended pane left: nothing replaced');
  assert.ok(r.fx.pick.includes('new') && !r.fx.pick.includes('s3') && r.fx.pick.length === GRID_MAX);
});

test('a session already in the grid or a grid with room: unchanged behaviour', () => {
  assert.deepEqual(restoreMembers(ctx({ active: 's4' })), nine);
  assert.deepEqual(restoreMembers(ctx({ gridIds: nine.slice(0, 3) })), [...nine.slice(0, 3), 'new']);
});

test('an ended session outside a full grid does not push out a live pane: the grid refuses and says how to continue', () => {
  const ended = ctx({ isEnded: (id) => id === 'new' });
  assert.deepEqual(restoreMembers(ended), nine);
  const single = { ...defaultLayout(true), center: { kind: 'single' } };
  const r = layoutReduce(single, { t: 'grid' }, ended);
  assert.equal(r.state, single);
  assert.equal(r.fx.pick, undefined);
  assert.match(r.fx.refuse, /this session has ended\. Remove a pane before adding this session\./);
});
