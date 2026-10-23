// node --test: the command palette's fuzzy scoring (piper/web/dist/palette.js)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { score } from '../../piper/web/dist/palette.js';

test('subsequence matches, misses are null', () => {
  assert.equal(score('', 'anything'), 0);
  assert.notEqual(score('sp24', 'New session in shop2024'), null);
  assert.equal(score('xyz', 'New session in shop2024'), null);
});

test('word starts and contiguous runs rank higher', () => {
  const a = score('sho', 'Switch: shop2024');
  const b = score('sho', 'Switch: asphalt-tool');
  assert.ok(a !== null && b !== null && a > b);
});

test('pick: an empty query lists every command in its order; a query ranks the matches', async () => {
  const { pick } = await import('../../piper/web/dist/palette.js');
  const cmds = Array.from({ length: 70 }, (_, i) => ({ label: `Command ${i}`, run: () => {} }));
  cmds.push({ label: 'Context panel', run: () => {} });
  assert.equal(pick(cmds, '').length, 71);
  assert.equal(pick(cmds, '  ')[0].label, 'Command 0');
  assert.equal(pick(cmds, 'contpan')[0].label, 'Context panel');
  assert.equal(pick(cmds, 'zzz').length, 0);
});

test('the Piper audit (security log) stays reachable in every preset', async () => {
  // main.ts needs a DOM - read the built module's text: no module gate on the audit view or its palette command
  const { readFile } = await import('node:fs/promises');
  const src = await readFile(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const vm = src.match(/const VIEW_MODULE = \{[^}]*\}/);
  assert.ok(vm, 'VIEW_MODULE not found');
  assert.ok(!/\baudit\s*:/.test(vm[0]), vm[0]);
  const cmd = src.match(/label: 'Piper audit'[^}]*\}/);
  assert.ok(cmd && !/module:/.test(cmd[0]), cmd && cmd[0]);
});
