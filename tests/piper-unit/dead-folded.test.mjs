// node --test: dead things wait folded - the palette's Ended group, the queue's ended sessions
// (no longer remembered open), the agent's ended "Started by the agent" rows.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { foldGroups, pick } from '../../piper/web/dist/palette.js';
import { ENDED_STATES } from '../../piper/web/dist/feed.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const run = () => undefined;
const cmds = [
  { label: 'grp/demo', group: 'Sessions', run },
  { label: 'New session…', run },
  { label: 'old one', group: 'Ended', folded: true, run, search: 'demo/plain' },
  { label: 'older one', group: 'Ended', folded: true, run },
];

test('palette: the Ended group is one "2 ended" row until opened; typing finds an ended session', () => {
  const f = foldGroups(pick(cmds, ''), false, new Set());
  assert.deepEqual(f.map((c) => c.label), ['grp/demo', 'New session…', '2 ended']);
  assert.equal(f[2].unfold, 'Ended');
  assert.equal(f[2].group, 'Ended', 'drawn under its Ended heading');
  assert.deepEqual(foldGroups(pick(cmds, ''), false, new Set(['Ended'])).map((c) => c.label), ['grp/demo', 'New session…', 'old one', 'older one']);
  assert.deepEqual(foldGroups(pick(cmds, 'plain'), true, new Set()).map((c) => c.label), ['old one'], 'a search is never folded');
  assert.deepEqual(foldGroups(pick(cmds.slice(0, 2), ''), false, new Set()).map((c) => c.label), ['grp/demo', 'New session…'], 'nothing ended, no row');
});

test('palette wiring: ↵ / click on the row opens the group in place, the ended sessions are marked folded', () => {
  const p = src('palette.ts');
  assert.match(p, /if \(c\.unfold\) \{ unfolded\.add\(c\.unfold\); draw\(\); return; \}/);
  assert.match(p, /items = foldGroups\(pick\(parent\?\.sub \?\? commands, input\.value\), !!input\.value\.trim\(\), unfolded\);/);
  assert.match(src('main.ts'), /ended\.push\(\{ \.\.\.row, group: 'Ended', folded: true \}\)/);
});

test('queue: the ended sessions start folded on every load - the open fold is no longer stored', () => {
  const q = src('queue.ts');
  assert.match(q, /let endedOpen = false;/);
  assert.doesNotMatch(q, /piper\.queue\.ended/);
});

test('agent: ended sessions it started sit in a closed "N ended" fold, the open state survives a redraw', () => {
  assert.deepEqual(ENDED_STATES, ['exited', 'error']);
  const f = src('feed.ts');
  assert.match(f, /h\('details', \{ class: 'feed-st-done' \}, h\('summary', \{ class: 'mut small' \}, `\$\{ended\.length\} ended`\)/);
  assert.match(f, /done\.open = startedDoneOpen; done\.addEventListener\('toggle', \(\) => \{ startedDoneOpen = done\.open; \}\);/);
  assert.match(f, /let startedDoneOpen = false;/);
  assert.match(f, /list\.filter\(\(x\) => !ENDED_STATES\.includes\(x\.state\)\)\.map\(row\)/);
});
