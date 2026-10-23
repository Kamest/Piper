// node --test: F18 palette - Sessions · Actions · Go to, the key as a chip, ⇧↵ beside.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pick, splitHint } from '../../piper/web/dist/palette.js';

const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
const pal = readFileSync(new URL('../../piper/web/src/palette.ts', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const cmds = main.slice(main.indexOf('function commands('), main.indexOf('function palette('));

test('F18: a hint that starts with a shortcut gives the chip; the rest is the grey description', () => {
  assert.deepEqual(splitHint({ hint: '⌘, · features, GitLab, projects, isolation' }), { keys: '⌘,', desc: 'features, GitLab, projects, isolation' });
  assert.deepEqual(splitHint({ hint: '⌘B' }), { keys: '⌘B', desc: '' });
  assert.deepEqual(splitHint({ hint: '⌥⇧T in the grid · no sandbox' }), { keys: '', desc: '⌥⇧T in the grid · no sandbox' }, 'a key inside a sentence stays text');
  assert.deepEqual(splitHint({ hint: '18 new · assigned' }), { keys: '', desc: '18 new · assigned' });
  assert.deepEqual(splitHint({ hint: 'fold / focus', keys: '⌘B' }), { keys: '⌘B', desc: 'fold / focus' });
  assert.deepEqual(splitHint({}), { keys: '', desc: '' });
});

test('F18: a session row says state · branch, never the prompt - which still matches; ⇧↵ opens it beside', () => {
  assert.match(cmds, /hint: \[LABEL\[s\.state\] \?\? s\.state, s\.branch\]\.filter\(Boolean\)\.join\(' · '\), search: history,/);
  assert.match(cmds, /hasTerminal\(s\) && s\.id !== active && on\('grid'\) \? \{ beside: \(\) => splitWith\(s\.id\) \} : \{\}/);
  const rows = [{ label: 'acme-app', hint: 'waiting', search: 'java/acme-app · fix the PDF export', run() {} }, { label: 'Settings', run() {} }];
  assert.deepEqual(pick(rows, 'pdf export').map((r) => r.label), ['acme-app']);
  assert.match(pal, /if \(c\.unfold\) \{ unfolded\.add\(c\.unfold\); draw\(\); return; \}\n    close\(\); if \(beside && c\.beside\) c\.beside\(\); else c\.run\(\);/, '9.136: the "N ended" row opens in place');
  assert.match(pal, /if \(c\) runIt\(c, e\.shiftKey\);/);
});

test('F18: groups - the four Queue rows are one with ›, views under Go to (Keyboard shortcuts too), the rest Actions, Ended last', () => {
  assert.doesNotMatch(cmds, /label: 'Queue: /);
  assert.match(cmds, /go\(\{ label: 'Queue', hint: 'fold \/ focus',[^\n]*sub: qsub \}\);/);
  for (const l of ["'Keyboard shortcuts'", "'Settings'", "'Piper audit'", "'Database'", "'Recent alerts'", "'Git window'"]) assert.match(cmds, new RegExp(`go\\(\\{ label: ${l}`), l);
  assert.match(cmds, /\(c\.act === 'focus' \? go : /, 'the inbox under Go to, Refresh / automation are actions');
  assert.match(cmds, /const actions = cmds\.filter\(\(c\) => c\.group !== 'Sessions'\)\.map\(\(c\) => \(\{ \.\.\.c, group: 'Actions' \}\)\);/);
  assert.match(cmds, /ended\.push\(\{ \.\.\.row, group: 'Ended', folded: true \}\)/, 'Z: the Ended group stays (9.136: folded)');
});

test('F18: the row - name, description, the group while typing, the key chip; a foot ↵ open · ⇧↵ beside · esc close', () => {
  assert.match(pal, /typed && c\.group \? h\('span', \{ class: 'pgrp' \}, c\.group\) : null,/);
  assert.match(pal, /keys \? h\('kbd', \{ class: 'pkey' \}, kbd\(keys\)\) : null,/);
  assert.match(pal, /'⇧↵'\), ' beside'/);
  assert.match(pal, /placeholder: 'Command, project or session'/);
  assert.match(pal, /c\.sub\?\.length \? h\('span', \{ class: 'psub'/, 'Z: › sub-actions stay');
  const block = css.slice(css.indexOf('/* ==== 9.79 F18'), css.indexOf('/* ==== end F18 ==== */'));
  assert.match(block, /\.palette \.plist \.psec \{[^}]*text-transform: uppercase/);
  assert.match(block, /\.palette \.pitem\.active \{ border-color: transparent; background: var\(--tile\); \}/);
});
