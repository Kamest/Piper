// node --test: Files' External group - the ids of files outside every project, ⌘P's path:line and the persisted roots
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { decodeView, externalAbsolute, externalId, externalKey, externalRootPath, externalSplit, isExternalKey, middleCut, shownPath, sizeText, splitPathLine, transferUndoText } from '../../piper/web/dist/files.js';
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

test('a person sees an External id as its absolute path, never the id separator `//`', () => {
  const root = externalKey('/data/outside');
  assert.equal(shownPath(externalId(root, 'notes.md')), '/data/outside/notes.md');
  assert.equal(shownPath(externalId(root, 'sub/a.txt')), '/data/outside/sub/a.txt');
  assert.equal(shownPath(externalId(externalKey('/'), 'notes.md')), '/notes.md');
  assert.equal(shownPath('docs/notes.md'), 'docs/notes.md');   // a project path stays as it is
  // the id itself keeps `//`: nested roots (/data/outside and /data/outside/sub) stay apart; persisted tabs unchanged
  assert.deepEqual(externalSplit(externalId(externalKey('/data/outside/sub'), 'a.txt')), { root: 'external:/data/outside/sub', rel: 'a.txt' });
});

test('Undo of a Copy / Move says what is on disk now - a Replace restored the original', () => {
  assert.equal(transferUndoText('copy', 'README.md', 'README.md', false), 'Removed the copy README.md');
  assert.equal(transferUndoText('copy', 'README.md', 'README.md', true), 'Restored the original README.md');
  assert.equal(transferUndoText('move', 'docs/report.md', 'report.md', false), 'Moved report.md back');
  assert.equal(transferUndoText('move', 'docs/report.md', 'report.md', true), 'Moved report.md back and restored the original docs/report.md');
});

test('Copy / Move toasts stack, stay out of Recent alerts; External re-reads without polling', () => {
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  // routine transfer outcomes: undoToast(…, false) - no deps.notice; Delete keeps the default (logged)
  assert.match(src, /undoToast\(transferUndoText\(mode, dest, name, !!r!\.replaced\), null, false\)/);
  assert.match(src, /original in Piper's trash`, undo, false\)/);
  assert.match(src, /if \(log\) deps\.notice\?\.\(text\);/);
  assert.match(src, /undoToast\(`Deleted \$\{path\} → Piper's trash`, trash \?/);
  assert.doesNotMatch(src, /deps\.toast\(mode === 'copy' \? `Removed the copy/);
  // one column, at most 4, like #toasts
  assert.match(src, /id: 'ftoasts'/);
  assert.match(src, /while \(stack\.children\.length > 4\)/);
  // External refresh: window focus, the browser tab becoming visible, and Files shown again - no timer
  assert.match(src, /addEventListener\('visibilitychange', \(\) => \{ if \(document\.visibilityState === 'visible'\) refreshExt\(\); \}\)/);
  assert.match(src, /\} else refreshExt\(true\);/);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /#ftoasts \{ position: fixed;[^}]*flex-direction: column/);
  assert.doesNotMatch(css, /\.ftoast \{ position: fixed/);
});
