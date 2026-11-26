// Files → IDE level, phase 1 core (filedoc.ts): the document state, the save queue (B4), typed save failures (B3),
// the disk poll's comparison (1b) and their wiring into files.ts (B10, B16, B18, S1, K1).
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { docState, movedPaths, POLL_MS, saveFailure, SaveQueue, stateText } from '../../piper/web/dist/filedoc.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const facts = (o = {}) => ({ dirty: false, saving: false, gone: false, conflict: false, writable: true, ...o });

test('docState: the most pressing state wins - gone, conflict, read-only, saving, dirty, clean', () => {
  assert.equal(docState(facts()), 'clean');
  assert.equal(docState(facts({ dirty: true })), 'dirty');
  assert.equal(docState(facts({ dirty: true, saving: true })), 'saving');
  assert.equal(docState(facts({ dirty: true, writable: false })), 'readonly');
  assert.equal(docState(facts({ dirty: true, conflict: true, saving: true })), 'conflict');
  assert.equal(docState(facts({ dirty: true, conflict: true, gone: true })), 'missing');
});

test('stateText (S1): "saves in" only while the autosave timer runs; every state says what happens next', () => {
  assert.equal(stateText('dirty', 2), '● unsaved · saves in 2 s · ⌘S now');
  assert.equal(stateText('dirty', 0), '● unsaved · ⌘S save');
  assert.equal(stateText('clean', 0), '✎ editable · saved automatically');
  assert.equal(stateText('saving', 0), 'saving…');
  assert.equal(stateText('conflict', 2), '⚠ conflict with disk', 'design a.html #s3: no autosave promise under a conflict');
  assert.equal(stateText('missing', 2), '⚠ file no longer on disk', 'design a.html #s4');
  assert.equal(stateText('readonly', 2), '🔒 read-only');
});

test('saveFailure (B3): piperd\'s code decides; a vanished file is never a conflict', () => {
  assert.equal(saveFailure(409, 'conflict'), 'conflict');
  assert.equal(saveFailure(410, 'missing'), 'missing');
  assert.equal(saveFailure(413, 'too_large'), 'too_large');
  assert.equal(saveFailure(422, 'unsupported'), 'unsupported');
  assert.equal(saveFailure(410, ''), 'missing');
  assert.equal(saveFailure(409, ''), 'conflict', 'an older piperd: every 409 was the sha lock');
  assert.equal(saveFailure(404, ''), 'error');
  assert.equal(saveFailure(0, ''), 'error', 'network');
});

test('SaveQueue (B4): one write at a time; saves asked for meanwhile collapse into one run of the newest', async () => {
  const q = new SaveQueue();
  const log = [];
  let release;
  const first = q.run(() => new Promise((r) => { log.push('one'); release = r; }));
  assert.equal(q.busy, true);
  const second = q.run(async () => { log.push('two'); });
  const third = q.run(async () => { log.push('three'); });
  assert.deepEqual(log, ['one'], 'nothing else starts while a write is in flight');
  assert.equal(second, first);
  assert.equal(third, first);
  release();
  await first;
  assert.deepEqual(log, ['one', 'three'], 'the queued saves ran once, after it - the newest job');
  assert.equal(q.busy, false);
  await q.run(async () => { throw new Error('refused'); });   // a failed job does not wedge the queue
  await q.run(async () => { log.push('after'); });
  assert.equal(log.at(-1), 'after');
});

test('movedPaths (1b): changed, gone and back - an unknown token reads once, a known gone path stays quiet', () => {
  const known = { a: '1:2:3', b: '1:2:3', c: '', d: undefined, e: '' };
  const r = movedPaths((p) => known[p], { a: '1:2:3', b: '9:2:3', c: '5:5:5', d: '7:7:7', e: null, f: null });
  assert.deepEqual(r, { changed: ['b', 'd'], gone: ['f'], back: ['c'] });
  assert.deepEqual(movedPaths(() => '1:1:1', { x: null }).gone, ['x']);
  assert.ok(POLL_MS >= 2000 && POLL_MS <= 3000, 'report §5 1b: every 2–3 s');
});

test('wiring: files.ts routes save, reread, banners and status through the document model', () => {
  const files = src('files.ts');
  // B4: every save goes through the tab's queue; the write's answer moves the epoch (B10) and the version (1b)
  assert.match(files, /const run = q\.run\(\(\) => saveNow\(force, sure, t, auto\)\);/);
  assert.match(files, /t\.epoch\+\+;   \/\/ a read begun before this write is older than it \(B10\)\n\s*t\.version = wrote\?\.version \|\| undefined;/);
  // B10: a read answered after a save / a newer read is dropped
  assert.match(files, /if \(t\.epoch !== epoch \|\| saveQueue\(t\)\.busy\) return;/);
  // B3 / K4: a vanished file has Save as… / Create again / Close and no Overwrite anywhere in the project banners
  const banners = files.slice(files.indexOf('function drawBanners('), files.indexOf('function extBanners('));
  for (const label of ["'Save as…'", "'Create again'", "'Close'", "'Compare'", "'Keep mine'", "'Load from disk'"]) assert.ok(banners.includes(label), label);
  assert.doesNotMatch(banners, /Overwrite/);
  // B18: every "load from disk" entry is the reversible one
  assert.doesNotMatch(files, /reread\(t, true\)\] : undefined/);
  assert.match(files, /\['Reload from disk', '', \(\) => void loadFromDisk\(t\)\]/);
  // S1: back from the diff, a pending edit is scheduled again
  assert.match(files, /if \(m === 'code'\) schedule\(t\);/);
  // B16: a slow read never activates its tab over a newer navigation
  assert.match(files, /if \(quiet \|\| nav !== navSeq\) return;/);
  // B11: git status asks the disk poll, not a full read of every tab
  const marks = files.slice(files.indexOf('function refreshMarks('), files.indexOf('function poll('));
  assert.doesNotMatch(marks, /reread\(/);
  // 1b: one POST /api/fs/stat per tick, only while Files is in the page and visible
  assert.match(files, /if \(polling \|\| xo \|\| !project \|\| !el\.isConnected \|\| document\.visibilityState !== 'visible'\) return;/);
  assert.match(files, /window\.setInterval\(\(\) => void poll\(\), POLL_MS\);/);
  // K1 / B19: an in-place refresh maps the carets through the change instead of resetting them
  const cm = src('cmeditor.ts');
  const replace = cm.slice(cm.indexOf('    replace(text: string) {'), cm.indexOf('    setReadOnly('));
  assert.doesNotMatch(replace, /selection: \{ anchor/);
});
