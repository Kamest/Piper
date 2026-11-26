// Files tree drag & drop (filedrop.ts): where a drop lands, what a move refuses, the names a Finder drop clashes
// with, and the folders and files a Finder drop brings (folders through the FileSystemEntry API).
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { clashes, collectDropped, DROP_MAX_ENTRIES, dropDir, moveRefused, movedTo } from '../../piper/web/dist/filedrop.js';

test('dropDir: a folder row itself, a file row its folder, the empty tree the project root', () => {
  assert.equal(dropDir('src/main', true), 'src/main');
  assert.equal(dropDir('src/A.java', false), 'src');
  assert.equal(dropDir('README.md', false), '.');
  assert.equal(dropDir(undefined, false), '.');
});

test('moveRefused / movedTo: no move into its own folder or subtree; the name is kept', () => {
  assert.equal(moveRefused('src/A.java', 'src'), 'already there');
  assert.equal(moveRefused('README.md', '.'), 'already there');
  assert.equal(moveRefused('src', 'src/main'), 'a folder cannot move into itself');
  assert.equal(moveRefused('src', 'src'), 'a folder cannot move into itself');
  assert.equal(moveRefused('src/A.java', 'pkg'), '');
  assert.equal(moveRefused('srcx/A.java', 'src'), '');
  assert.equal(movedTo('src/A.java', 'pkg'), 'pkg/A.java');
  assert.equal(movedTo('src/A.java', '.'), 'A.java');
});

test('clashes: only the top-level names of a drop that the folder already has', () => {
  const items = [{ rel: 'img', dir: true }, { rel: 'img/a.png', dir: false }, { rel: 'a.txt', dir: false }];
  assert.deepEqual(clashes(items, new Set(['img', 'a.png'])), ['img']);
  assert.deepEqual(clashes(items, new Set()), []);
});

const file = (name, size = 1) => ({ name, size });
const fileEntry = (name, size) => ({ isFile: true, isDirectory: false, name, file: (ok) => ok(file(name, size)) });
const dirEntry = (name, children, batch = 2) => ({
  isFile: false, isDirectory: true, name,
  createReader() { let i = 0; return { readEntries(ok) { const b = children.slice(i, i + batch); i += batch; ok(b); } }; },
});
const transfer = (entries, files) => ({
  items: entries.map((e) => ({ kind: 'file', webkitGetAsEntry: () => e })),
  files: files ?? entries.map((e) => file(e.name)),
});

test('collectDropped: a folder from Finder - parents before children, every batch of readEntries read', async () => {
  const tree = dirEntry('assets', [fileEntry('a.png', 3), dirEntry('icons', [fileEntry('x.svg', 2), fileEntry('y.svg', 2), fileEntry('z.svg', 2)]), fileEntry('b.png', 4)]);
  const got = await collectDropped(transfer([tree, fileEntry('README.md', 5)]));
  assert.deepEqual(got.map((d) => [d.rel, d.dir]), [
    ['assets', true], ['assets/a.png', false], ['assets/icons', true], ['assets/icons/x.svg', false],
    ['assets/icons/y.svg', false], ['assets/icons/z.svg', false], ['assets/b.png', false], ['README.md', false]]);
  assert.equal(got.at(-1).file.size, 5);
});

test('collectDropped: Piper Desktop gives the top-level paths (a checkout file dragged back is moved)', async () => {
  const got = await collectDropped(transfer([fileEntry('A.java', 1)]), (f) => '/work/acme/src/' + f.name);
  assert.equal(got[0].abs, '/work/acme/src/A.java');
  const plain = await collectDropped({ items: [], files: [file('n.txt')] }, () => '');
  assert.deepEqual(plain.map((d) => [d.rel, d.dir, d.abs]), [['n.txt', false, undefined]]);
});

test('collectDropped: refuses a drop past the entry limit instead of copying part of it', async () => {
  const many = Array.from({ length: DROP_MAX_ENTRIES + 1 }, (_, i) => fileEntry(`f${i}.txt`, 1));
  await assert.rejects(collectDropped(transfer([dirEntry('big', many, 500)])), /more than 1000/);
});
