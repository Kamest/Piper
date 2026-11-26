// Files tree selection (treesel.ts): ⇧-ranges over the shown rows, ⌘-toggles, the top-level items an action works on
// - IntelliJ's project view selection, without the Files closure.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rangeSel, selLabel, toggleSel, topLevel } from '../../piper/web/dist/treesel.js';

const rows = ['src', 'src/A.java', 'src/B.java', 'src/C.java', 'pom.xml'];

test('rangeSel: every shown row between the anchor and the click, either direction; an unknown anchor selects one', () => {
  assert.deepEqual([...rangeSel(rows, 'src/A.java', 'src/C.java')], ['src/A.java', 'src/B.java', 'src/C.java']);
  assert.deepEqual([...rangeSel(rows, 'pom.xml', 'src/B.java')], ['src/B.java', 'src/C.java', 'pom.xml']);
  assert.deepEqual([...rangeSel(rows, 'gone.txt', 'src/B.java')], ['src/B.java']);
});

test('toggleSel: ⌘-click adds or removes one row; the first ⌘-click keeps the row that was already selected', () => {
  assert.deepEqual([...toggleSel(new Set(['a']), 'b')], ['a', 'b']);
  assert.deepEqual([...toggleSel(new Set(['a', 'b']), 'a')], ['b']);
  assert.deepEqual([...toggleSel(new Set(), 'b', 'a')], ['a', 'b'], 'the open file joins the selection');
  assert.deepEqual([...toggleSel(new Set(), 'a', 'a')], [], 'toggling the open file itself unselects it');
});

test('topLevel: a folder and files inside it act once, as the folder; order kept, duplicates dropped', () => {
  assert.deepEqual(topLevel(['src/A.java', 'src', 'pom.xml', 'srcx/B.java', 'pom.xml']), ['src', 'pom.xml', 'srcx/B.java']);
  assert.deepEqual(topLevel([]), []);
});

test('selLabel: one name, or how many items', () => {
  assert.equal(selLabel(['src/A.java']), 'A.java');
  assert.equal(selLabel(['src', 'pom.xml']), '2 items');
});
