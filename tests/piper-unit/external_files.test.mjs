// node --test: Files' External group - the ids of files outside every project, ⌘P's path:line and the persisted roots
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decodeView, externalAbsolute, externalId, externalKey, externalRootPath, externalSplit, isExternalKey, middleCut, sizeText, splitPathLine } from '../../piper/web/dist/files.js';
import { reference } from '../../piper/web/dist/environment.js';

test('an External root key names an absolute folder, also on another computer', () => {
  assert.equal(externalKey('/data/exchange/'), 'external:/data/exchange');
  assert.equal(externalKey('/'), 'external:/');
  assert.equal(externalRootPath('external:/data/exchange'), '/data/exchange');
  const remote = externalKey('/data/exchange', 'build-host');
  assert.deepEqual(reference(remote, 'project'), { host: 'build-host', kind: 'project', raw: 'external:/data/exchange' });
  assert.equal(externalRootPath(remote), '/data/exchange');
  assert.ok(isExternalKey(remote));
  // a project key, a relative "external:" and a broken reference are not External roots
  for (const key of ['acme/shop', 'external:relative/path', 'external:', 'piper-ref:bad host:project:x']) assert.equal(isExternalKey(key), false, key);
});

test('an External id splits into its root and relative path; a project path never does', () => {
  const root = externalKey('/data/exchange');
  const id = externalId(root, 'results/report.md');
  assert.equal(id, 'external:/data/exchange//results/report.md');
  assert.deepEqual(externalSplit(id), { root, rel: 'results/report.md' });
  assert.equal(externalAbsolute(id), '/data/exchange/results/report.md');
  assert.deepEqual(externalSplit(externalId(root, '')), { root, rel: '' });
  assert.equal(externalAbsolute(externalId(root, '')), '/data/exchange');
  // the filesystem root: its own `//` after the prefix is not the separator
  const top = externalKey('/');
  assert.deepEqual(externalSplit(externalId(top, 'report.md')), { root: top, rel: 'report.md' });
  assert.equal(externalAbsolute(externalId(top, 'report.md')), '/report.md');
  // a remote root: the encoded key holds no `/`
  const remote = externalKey('/data/exchange', 'build-host');
  assert.deepEqual(externalSplit(externalId(remote, 'a/b.txt')), { root: remote, rel: 'a/b.txt' });
  for (const p of ['README.md', 'docs/external:/x//y', 'external-notes.md', 'piper-ref:build-host:project:acme%2Fshop//x']) assert.equal(externalSplit(p), null, p);
});

test('⌘P Open path takes path:line(:col)', () => {
  assert.deepEqual(splitPathLine('/data/exchange/report.md:12:4'), { path: '/data/exchange/report.md', line: 12, col: 4 });
  assert.deepEqual(splitPathLine(' /data/exchange/report.md:12 '), { path: '/data/exchange/report.md', line: 12, col: 0 });
  assert.deepEqual(splitPathLine('/data/exchange/report.md'), { path: '/data/exchange/report.md', line: 0, col: 0 });
  assert.deepEqual(splitPathLine('/data/a:b/report.md'), { path: '/data/a:b/report.md', line: 0, col: 0 });
});

test('a long root path is cut in the middle, keeping the start and the last folder', () => {
  assert.equal(middleCut('/data/exchange'), '/data/exchange');
  const cut = middleCut('/data/exchange/customers/acme/2026/results', 26);
  assert.equal(cut.length, 26);
  assert.ok(cut.startsWith('/data/exch') && cut.endsWith('…/results'), cut);
  assert.equal(middleCut('/' + 'x'.repeat(60), 26).length, 26);
  assert.equal(sizeText(812), '812 B');
  assert.equal(sizeText(3.4 * 1024 * 1024), '3.4 MiB');
});

test('the persisted External roots survive a reload; junk is dropped', () => {
  const view = decodeView(JSON.stringify({ tabs: ['README.md', 'external:/data/exchange//report.md'], active: 'external:/data/exchange//report.md',
    external: [{ key: 'external:/data/exchange', expanded: ['', 'results', 7] }, { key: 'acme/shop', expanded: [] }, 'junk', { key: externalKey('/srv/x', 'build-host'), expanded: [] }] }));
  assert.deepEqual(view.external, [{ key: 'external:/data/exchange', expanded: ['', 'results'] }, { key: externalKey('/srv/x', 'build-host'), expanded: [] }]);
  assert.deepEqual(view.tabs, ['README.md', 'external:/data/exchange//report.md']);
  assert.deepEqual(decodeView(JSON.stringify({ tabs: [] })).external, []);
});
