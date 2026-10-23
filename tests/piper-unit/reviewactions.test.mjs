import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reviewDigit, toggleReviewAction, reviewPrompt, renderReviewInstruction, revertText } from '../../piper/web/dist/reviewactions.js';
const target = { path: 'src/A.ts', from: 3, to: 4, old: false, code: 'a();\nb();', sha: 'aaa' };
const tests = { id: 'tests', key: 1, label: 'Tests', kind: 'edit', prompt: 'Test {{target}}: {{note}}' };
const check = { id: 'check', key: 2, label: 'Check', kind: 'read', prompt: 'Check {{file}} {{range}} {{side}} {{code}}' };
test('physical shortcuts work on Czech keyboards without intercepting modified keys', () => {
  const event = { code: 'Digit1', key: '+', metaKey: false, ctrlKey: false, altKey: false };
  assert.equal(reviewDigit(event), 1);
  assert.equal(reviewDigit({ ...event, code: 'Numpad6', key: '6' }), 6);
  for (const flag of ['metaKey', 'ctrlKey', 'altKey', 'isComposing']) assert.equal(reviewDigit({ ...event, [flag]: true }), null);
  assert.equal(reviewDigit({ ...event, code: 'KeyA', key: 'a' }), null);
});
test('multiple actions preserve instruction; zero clears actions and custom text; versions never share requests', () => {
  let items = toggleReviewAction([], target, tests);
  items[0].text = 'Use the existing fake clock';
  items = toggleReviewAction(items, target, check);
  assert.equal(items[0].actions.length, 2);
  tests.prompt = 'Changed configuration';
  assert.equal(items[0].actions[0].prompt, 'Test {{target}}: {{note}}');
  const previous = structuredClone(items);
  items = toggleReviewAction(items, target, null);
  assert.deepEqual(items, []);
  assert.equal(previous[0].text, 'Use the existing fake clock');
  items = toggleReviewAction(previous, { ...target, sha: 'bbb' }, check);
  assert.equal(items.length, 2);
});
test('prompt expands templates, carries concrete solutions and keeps overlapping requests separate', () => {
  const item = { ...target, id: 'one', state: 'open', text: 'Return an empty list on timeout', actions: [tests, check] };
  assert.equal(renderReviewInstruction(check, item), 'Check src/A.ts 3–4 working a();\nb();');
  const prompt = reviewPrompt([item, { ...item, id: 'two', old: true, actions: [] }, { ...item, id: 'three', from: 4 }]);
  assert.match(prompt, /Return an empty list on timeout/);
  assert.match(prompt, /ORIGINAL SIDE/);
  assert.match(prompt, /Overlaps: three/);
  assert.ok(prompt.indexOf('Check (read)') < prompt.indexOf('Tests (edit)'));
  assert.match(prompt, /Working SHA256: aaa/);
  assert.match(prompt, /Do not commit or push/);
  assert.equal(reviewPrompt([{ ...item, state: 'sent' }]).includes('[one]'), false);
});
test('restoring selected rows preserves the comparison newline and leaves other rows intact', () => {
  const chunk = { fromA: 0, toA: 7, fromB: 0, toB: 7 };
  assert.equal(revertText('old\nend', 'new\nfin', chunk, { from: 4, to: 7 }), 'new\nend');
  assert.equal(revertText('old\nend', 'new\nfin', chunk, { from: 0, to: 4 }), 'old\nfin');
  assert.equal(revertText('old\nend', 'new\nfin', chunk), 'old\nend');
  assert.equal(revertText('', 'one\ntwo\n', { fromA: 0, toA: 0, fromB: 0, toB: 8 }, { from: 4, to: 8 }), 'one\n');
  assert.equal(revertText('', 'one\ntwo\n', { fromA: 0, toA: 1, fromB: 0, toB: 9 }, { from: 4, to: 8 }), 'one\n');
  assert.throws(() => revertText('a\nb\n', 'c\n', { fromA: 0, toA: 4, fromB: 0, toB: 2 }, { from: 0, to: 2 }), /different row counts/);
  assert.throws(() => revertText('a', 'b', { fromA: 0, toA: 1, fromB: 0, toB: 1 }, { from: 0, to: 2 }), /within one change block/);
});

test('compact prompts reference disk files; original code and explicit code placeholders stay available', () => {
  const item = { ...target, id: 'compact', state: 'open', text: '', actions: [{ ...tests, prompt: 'Test {{target}}' }] };
  const compact = reviewPrompt([item]);
  assert.match(compact, /src\/A.ts/); assert.match(compact, /Lines 3–4/);
  assert.equal(compact.includes(target.code), false);
  assert.ok(reviewPrompt([item], true).includes(target.code));
  assert.ok(reviewPrompt([{ ...item, old: true }]).includes(target.code));
  assert.ok(reviewPrompt([{ ...item, actions: [{ ...tests, prompt: 'Inspect {{code}}' }] }]).includes(target.code));
  const repeated = reviewPrompt([item, { ...item, id: 'other', from: 10, to: 11 }]);
  assert.equal(repeated.split('Working SHA256: aaa').length, 2);
});
