// node --test: pure helpers of the 9.7 OPS features (turns, history/blame/graph, CI/MR,
// stack, diagnostics, usage), from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { canOfferMr, canPush, ciLabel, ciTone, amendOffMessage, changedSince, commitHint, commitLabel, commitPlan, generatedNote, mrCreatedText, numberDiff, pickState, primaryAction, reviewKey, reviewPrimary } from '../../piper/web/dist/gitpanel.js';
import { diffPaths, filesLabel, lastTurn, pickTurn, turnLabel, turnRange } from '../../piper/web/dist/turns.js';
import { ageBucket, blameRuns, graphRemotes, laneRuns, parseGraphLine, parseRefs, relAge } from '../../piper/web/dist/history.js';
import { elapsed, filterLog, fixVerb, jobTitle, needsConfirm, shouldTrack, serviceState, serviceTone, sortServices, stackSummary, tailLines, worktreeSource } from '../../piper/web/dist/stack.js';
import { diagMessage, groupDiagnostics } from '../../piper/web/dist/diag.js';
import { barParts, dayRuns, fillDays, gapText, localDay, noUsage, partsTitle, usageSummary, usageTotal } from '../../piper/web/dist/usage.js';

const turn = (n, prompt, to = 't' + n) => ({ n, prompt, started: 100 * n, ended: to ? 100 * n + 50 : null, from: 'f' + n, to });

test('turn labels: first non-empty prompt line, cut, open turn marked', () => {
  assert.equal(turnLabel(turn(3, '\n  fix the test\nnext line')), 'turn 3 · fix the test');
  assert.equal(turnLabel(turn(1, '')), 'turn 1');
  assert.equal(turnLabel(turn(2, 'x'.repeat(100)), 10), 'turn 2 · ' + 'x'.repeat(9) + '…');
  assert.equal(turnLabel(turn(4, 'running', null)), 'turn 4 · running (running)');
});

test('newest turn, picked turn with fallback, open turn diffs against now', () => {
  const list = [turn(1, 'a'), turn(3, 'c'), turn(2, 'b')];
  assert.equal(lastTurn(list).n, 3);
  assert.equal(lastTurn([]), null);
  assert.equal(pickTurn(list, 2).n, 2);
  assert.equal(pickTurn(list, null).n, 3);
  assert.equal(pickTurn(list, 9).n, 3, 'a vanished pick falls back to the newest');
  assert.deepEqual(turnRange(turn(5, '', null)), { from: 'f5', to: '' });
  assert.deepEqual(turnRange(turn(5, '')), { from: 'f5', to: 't5' });
});

test('plural of files', () => {
  assert.deepEqual([0, 1, 2, 4, 5, 22].map(filesLabel), ['0 files', '1 file', '2 files', '4 files', '5 files', '22 files']);
});

test('multi-file diff: every line knows its file; an added "++" line is not a header; deletions use the old name', () => {
  const text = [
    'diff --git a/src/A.java b/src/A.java', 'index 1..2 100644', '--- a/src/A.java', '+++ b/src/A.java', '@@ -1,2 +1,2 @@',
    ' keep', '+++ looks like a header', 'diff --git a/old.txt b/old.txt', 'deleted file mode 100644', '--- a/old.txt', '+++ /dev/null',
    '@@ -1 +0,0 @@', '-gone',
    'diff --git "a/sp ace.md" "b/sp ace.md"', '--- "a/sp ace.md"', '+++ "b/sp ace.md"', '@@ -0,0 +1 @@', '+x',
  ].join('\n');
  const lines = numberDiff(text);
  const paths = diffPaths(lines);
  assert.equal(paths.length, lines.length);
  assert.equal(paths[6], 'src/A.java');
  assert.equal(lines[6].cls, 'add');
  assert.equal(paths[12], 'old.txt');
  assert.equal(paths[17], 'sp ace.md');
});

test('stat lines before the first diff belong to no file', () => {
  const lines = numberDiff(['commit abc', ' a.txt | 2 +-', '', 'diff --git a/a.txt b/a.txt', '--- a/a.txt', '+++ b/a.txt', '@@ -1 +1 @@', '-x', '+y'].join('\n'));
  const paths = diffPaths(lines);
  assert.equal(paths[1], '');
  assert.equal(paths[8], 'a.txt');
});

test('CI tone and label', () => {
  assert.equal(ciTone('success'), 'ok');
  assert.equal(ciTone('failed'), 'bad');
  assert.equal(ciTone('running'), 'run');
  assert.equal(ciTone('pending'), 'run');
  assert.equal(ciTone('canceled'), 'mut');
  assert.equal(ciLabel('failed'), 'failed');
  assert.equal(ciLabel('weird'), 'weird');
});

test('MR is offered only for a pushed feature branch without an MR', () => {
  const ci = { project: 'p', branch: 'feat/x', pipeline: null, mr: null };
  const st = { branch: 'feat/x', upstream: 'origin/feat/x', ahead: 0 };
  assert.equal(canOfferMr(st, ci), true);
  assert.equal(canOfferMr({ ...st, ahead: 1 }, ci), false, 'unpushed commits');
  assert.equal(canOfferMr({ ...st, upstream: '' }, ci), false, 'no upstream');
  assert.equal(canOfferMr({ ...st, branch: 'master' }, { ...ci, branch: 'master' }), false);
  assert.equal(canOfferMr(st, { ...ci, mr: { iid: 1 } }), false, 'MR exists');
  assert.equal(canOfferMr(st, null), false, 'CI unknown');
  assert.equal(canOfferMr(st, { ...ci, branch: 'other' }), false, 'stale CI of another branch');
});

test('relative age and blame colour buckets', () => {
  const now = 1_000_000_000;
  assert.equal(relAge(now - 10, now), 'now');
  assert.equal(relAge(now - 600, now), '10 min');
  assert.equal(relAge(now - 7200, now), '2 h');
  assert.equal(relAge(now - 86400 * 3, now), '3 d');
  assert.equal(relAge(now - 86400 * 100, now), '3 mo');
  assert.equal(relAge(now - 86400 * 365 * 4, now), '4 y');
  const days = [0, 6, 7, 29, 30, 89, 90, 364, 365, 1094, 1095, 5000];
  assert.deepEqual(days.map((d) => ageBucket(now - d * 86400, now)), [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]);
  assert.equal(ageBucket(now + 50, now), 0, 'clock skew');
});

test('blame runs start where the commit changes', () => {
  assert.deepEqual(blameRuns([{ commit: 'a' }, { commit: 'a' }, { commit: '' }, { commit: 'b' }, { commit: 'b' }, { commit: 'a' }]),
    [true, false, true, true, false, true]);
  assert.deepEqual(blameRuns([]), []);
});

test('graph lines: lanes, hash, refs, subject; lane-only lines', () => {
  assert.deepEqual(parseGraphLine('* 1a2b3c4 (HEAD -> main, origin/main, tag: v1.0) feat: x (y)'),
    { graph: '* ', hash: '1a2b3c4', refs: 'HEAD -> main, origin/main, tag: v1.0', subject: 'feat: x (y)' });
  assert.deepEqual(parseGraphLine('| * deadbeef fix it'), { graph: '| * ', hash: 'deadbeef', refs: '', subject: 'fix it' });
  assert.deepEqual(parseGraphLine('|/  '), { graph: '|/  ', hash: '', refs: '', subject: '' });
  assert.deepEqual(parseGraphLine('* abcdef1'), { graph: '* ', hash: 'abcdef1', refs: '', subject: '' });
});

test('refs are classified: head, tag, remote (origin and any <r>/HEAD), local with a slash', () => {
  const rows = ['* 1111111 (upstream/HEAD, upstream/main) a', '* 2222222 (HEAD -> feat/x, origin/feat/x, tag: v2) b'].map(parseGraphLine);
  const remotes = graphRemotes(rows);
  assert.deepEqual([...remotes].sort(), ['origin', 'upstream']);
  assert.deepEqual(parseRefs(rows[1].refs, remotes), [
    { name: 'feat/x', kind: 'head' }, { name: 'origin/feat/x', kind: 'remote' }, { name: 'v2', kind: 'tag' }]);
  assert.deepEqual(parseRefs('HEAD, feature/y, upstream/main', remotes), [
    { name: 'HEAD', kind: 'head' }, { name: 'feature/y', kind: 'local' }, { name: 'upstream/main', kind: 'remote' }]);
});

test('lane runs colour by column and keep every character', () => {
  const runs = laneRuns('| | * ');
  assert.equal(runs.map((r) => r.t).join(''), '| | * ');
  assert.deepEqual(runs.filter((r) => r.lane >= 0).map((r) => [r.t, r.lane]), [['|', 0], ['|', 1], ['*', 2]]);
  assert.equal(laneRuns('|'.repeat(14)).length, 7, 'two characters per lane');
  assert.ok(laneRuns('| '.repeat(8)).every((r) => r.lane < 6));
});

test('service tone: health wins, then state', () => {
  assert.equal(serviceTone({ state: 'running', health: 'healthy' }), 'ok');
  assert.equal(serviceTone({ state: 'running' }), 'ok');
  assert.equal(serviceTone({ state: 'running', health: 'unhealthy' }), 'bad');
  assert.equal(serviceTone({ state: 'running', health: 'starting' }), 'warn');
  assert.equal(serviceTone({ state: 'restarting' }), 'warn');
  assert.equal(serviceTone({ state: 'dead' }), 'bad');
  assert.equal(serviceTone({ state: 'exited' }), 'off');
  // G-ST5: killed / crashed (a non-zero exit) is not a plain Stop
  assert.equal(serviceTone({ state: 'exited', exitCode: 137 }), 'warn');
  assert.equal(serviceState({ state: 'exited', exitCode: 137, age: '2 minutes' }), 'exited (137) · 2 minutes');
  assert.equal(serviceState({ state: 'exited', age: '2 minutes' }), 'stopped · 2 minutes');
  assert.equal(fixVerb({ state: 'exited', exitCode: 137, verbs: ['start', 'restart'] }), 'start', 'killed after the stop grace: Start like a stopped one');
});

test('log filter: case-insensitive, trailing newline dropped', () => {
  assert.deepEqual(filterLog('a ERROR x\nok\nerror y\n', 'error'), ['a ERROR x', 'error y']);
  assert.deepEqual(filterLog('a\nb\n', ''), ['a', 'b']);
});

const diag = (path, line, message, severity = 'error', col = null) => ({ tool: 'tsc', severity, path, line, col, message });

test('diagnostics grouped by file: files with errors first, items by line', () => {
  const g = groupDiagnostics([diag('b.ts', 9, 'x'), diag('a.ts', 3, 'w', 'warning'), diag('b.ts', 2, 'y', 'error', 5), diag('c.ts', 1, 'z')]);
  assert.deepEqual(g.map((x) => [x.path, x.errors]), [['b.ts', 2], ['c.ts', 1], ['a.ts', 0]]);
  assert.deepEqual(g[0].items.map((x) => x.line), [2, 9]);
});

test('diagnostics message: references and one-line messages, errors first, bounded', () => {
  const m = diagMessage([diag('a.ts', 3, 'unused', 'warning'), diag('b.ts', 2, 'Type\n  string is not number', 'error', 5)], 'npx tsc -p .');
  const lines = m.split('\n');
  assert.match(lines[0], /\(npx tsc -p \.\)/);
  assert.equal(lines[1], '- b.ts:2:5: Type string is not number');
  assert.equal(lines[2], '- a.ts:3: warning: unused');
  const many = diagMessage(Array.from({ length: 70 }, (_, i) => diag('x.ts', i + 1, 'e')));
  assert.equal(many.split('\n').length, 1 + 60 + 1);
  assert.match(many, /and 10 more$/);
  assert.ok(diagMessage([diag('x.ts', 1, 'y'.repeat(1000))]).length < 400);
});

test('usage totals, bar scaling (max 0 safe), cache read optional', () => {
  const r = { input: 10, output: 20, cache_write: 30, cache_read: 940 };
  assert.equal(usageTotal(r), 1000);
  assert.equal(usageTotal(r, false), 60);
  assert.deepEqual(barParts(r, 1000).map((x) => [x.part, x.pct]), [['input', 1], ['output', 2], ['cache_write', 3], ['cache_read', 94]]);
  assert.deepEqual(barParts(r, 60, false).map((x) => x.part), ['input', 'output', 'cache_write']);
  assert.ok(barParts(r, 0).every((x) => x.pct === 0));
});

test('days are filled back from today, newest first, across a month and DST', () => {
  const days = fillDays([{ day: '2026-03-30', input: 1, output: 0, cache_read: 0, cache_write: 0 }], 4, '2026-04-01');
  assert.deepEqual(days.map((d) => d.day), ['2026-04-01', '2026-03-31', '2026-03-30', '2026-03-29']);
  assert.equal(days[2].input, 1);
  assert.equal(days[0].input, 0);
  assert.match(localDay(new Date(2026, 0, 5)), /^2026-01-05$/);
});

test('F20 usage: days without use fold into one line, the heading sums the used days, exact numbers in the tooltip', () => {
  const d = (day, output = 0) => ({ day, input: 0, output, cache_read: 0, cache_write: 0 });
  const days = [d('2026-10-20'), d('2026-10-19', 5), d('2026-10-18'), d('2026-10-17'), d('2026-10-16'), d('2026-10-15', 7), d('2026-10-14')];
  const runs = dayRuns(days);
  assert.deepEqual(runs.map((r) => r.kind === 'day' ? r.d.day : `${r.from}..${r.to}:${r.n}`),
    ['2026-10-20..2026-10-20:1', '2026-10-19', '2026-10-16..2026-10-18:3', '2026-10-15', '2026-10-14..2026-10-14:1']);
  assert.match(gapText(runs[2]), /^Oct 16 – Oct 18 · 3 days without use$/);
  assert.match(gapText(runs[0]), /^Oct 20 · no use$/);
  assert.equal(usageSummary(days), '≈ 12 over 2 days of use');
  assert.equal(usageSummary([d('2026-10-20')]), 'no use in 30 days');
  assert.equal(partsTitle({ input: 1, output: 2, cache_write: 3, cache_read: 4000 }),
    `input 1 · output 2 · cache write 3 · cache read ${(4000).toLocaleString()} · total ${(4006).toLocaleString()}`);
});

test('F20 usage structure: a day = day · stacked bar · total, legend and the cache switch stay, no 5-column day table', () => {
  const src = readFileSync(new URL('../../piper/web/src/usage.ts', import.meta.url), 'utf8');
  assert.match(src, /class: 'uday', role: 'listitem', title: partsTitle\(d\)/);
  assert.match(src, /bar\(d, maxDay\), h\('span', \{ class: 'uday-n' \}, fmtTokens\(usageTotal\(d\)\)\)/);
  assert.match(src, /class: 'ulegrow' \}, legend, cacheLabel/);
  assert.match(src, /'cache reads in the chart'/);
  assert.match(src, /h\('h3', \{\}, 'By project \(all time\)'\), pt/);   // Z: the project table stays
  assert.doesNotMatch(src, /headRow\('day', 'chart'\)/);
});

test('N7: usage with sessions that have not answered yet is the empty state, not 30 rows of zeros', () => {
  const zero = (project) => ({ project, sessions: 1, input: 0, output: 0, cache_read: 0, cache_write: 0 });
  assert.equal(noUsage({ days: [], projects: [] }), true);
  assert.equal(noUsage({ days: [], projects: [zero('grp/demo'), zero('e2e/leak')] }), true, 'listed sessions without a token');
  assert.equal(noUsage({ days: [], projects: [{ ...zero('old'), output: 5 }] }), false, 'usage older than 30 days still has a report');
  assert.equal(noUsage({ days: [{ day: '2026-10-09', input: 1, output: 0, cache_read: 0, cache_write: 0 }], projects: [] }), false);
});

test('stack header: counts from the provider\'s card (one-shots out), else from the rows', () => {
  const svc = (name, state, extra = {}) => ({ name, state, ...extra });
  assert.deepEqual(stackSummary({ stack: 'x', services: [svc('a', 'running'), svc('b', 'exited')], running: 1, total: 2 }), { text: '1/2 up', tone: 'warn' });
  assert.deepEqual(stackSummary({ stack: 'x', services: [svc('a', 'running'), svc('builder', 'exited', { oneShot: true })] }), { text: '1/1 up', tone: 'ok' });
  assert.deepEqual(stackSummary({ stack: 'x', services: [svc('a', 'exited')], running: 0, total: 1 }), { text: 'stopped', tone: 'faint' });
  assert.deepEqual(stackSummary({ stack: 'x', services: [svc('a', 'dead')], running: 0, total: 1 }), { text: '0/1 up', tone: 'bad' });
  assert.deepEqual(stackSummary({ stack: 'x', services: [] }), { text: 'not started', tone: 'faint' });
  assert.deepEqual(stackSummary({ stack: null, services: [] }), { text: 'none', tone: 'faint' });
  assert.deepEqual(stackSummary({ stack: null, services: [], error: 'the dev stack is not reachable' }), { text: 'unavailable', tone: 'faint' });
  assert.deepEqual(stackSummary({ stack: null, services: [], error: 'no dev stack for /x' }), { text: 'none', tone: 'faint' });
  assert.deepEqual(stackSummary(null, 'HTTP 500'), { text: 'unavailable', tone: 'faint' });
});

test('stack jobs: titles, clock, log tail', () => {
  assert.equal(jobTitle({ verb: 'rebuild', state: 'running' }, 'app'), 'Rebuilding app');
  assert.equal(jobTitle({ verb: 'restart', state: 'done' }), 'Restarted the stack');
  assert.equal(jobTitle({ verb: 'up', state: 'failed' }), 'Recreate the stack failed');
  assert.equal(jobTitle({ verb: 'down', state: 'canceled' }), 'Down the stack canceled');
  // the provider's worktree_adopt job of "Set up and start"
  assert.equal(jobTitle({ verb: 'worktree_adopt', state: 'running' }), 'Setting up the stack');
  assert.equal(jobTitle({ verb: 'worktree_adopt', state: 'done' }), 'Set up the stack');
  assert.equal(jobTitle({ verb: 'worktree_adopt', state: 'failed' }), 'Set up the stack failed');
  assert.equal(elapsed(42.9), '0:42');
  assert.equal(elapsed(725), '12:05');
  assert.equal(elapsed(-3), '0:00');
  assert.deepEqual(tailLines('a\n\nb  \nc\nd\n', 3), ['b', 'c', 'd']);
});

test('stack actions: confirm only Down and stopping / restarting / rebuilding everything', () => {
  assert.equal(needsConfirm('down', ''), true);
  for (const v of ['stop', 'restart', 'rebuild']) assert.equal(needsConfirm(v, ''), true, v);
  for (const v of ['start', 'up', 'build']) assert.equal(needsConfirm(v, ''), false, v);
  for (const v of ['start', 'stop', 'restart', 'rebuild', 'build', 'up']) assert.equal(needsConfirm(v, 'app'), false, v);
});

test('stack rows: problems first, one-shots apart, the inline fix only where the provider offers it', () => {
  const list = [{ name: 'db', state: 'running' }, { name: 'app', state: 'running', health: 'unhealthy', verbs: ['stop', 'restart'] },
    { name: 'web', state: 'exited', verbs: ['start', 'restart'] }, { name: 'builder', state: 'exited', oneShot: true, verbs: ['start'] },
    { name: 'dind', state: 'exited', location: 'sandbox', verbs: [] }];
  const { main, oneShots } = sortServices(list);
  assert.deepEqual(main.map((s) => s.name), ['app', 'dind', 'web', 'db']);
  assert.deepEqual(oneShots.map((s) => s.name), ['builder']);
  assert.equal(fixVerb(list[1]), 'restart');
  assert.equal(fixVerb(list[2]), 'start');
  assert.equal(fixVerb(list[2], false), null);                 // the whole stack is down: Start is in the header
  assert.equal(fixVerb(list[3]), null);                     // a finished one-shot is not broken
  assert.equal(fixVerb(list[4]), null);                     // the provider offers nothing for a DinD service
  assert.equal(fixVerb({ name: 'x', state: 'running', health: 'unhealthy' }), null);   // an older provider: no verbs known
  assert.equal(serviceState({ name: 'a', state: 'running', health: 'unhealthy', age: '2m' }), 'unhealthy · 2m');
  assert.equal(serviceState({ name: 'a', state: 'running', age: '4h' }), '4h');
  assert.equal(serviceState({ name: 'a', state: 'exited', age: '3m', location: 'sandbox' }), 'stopped · 3m · in sandbox');
});

test('stack jobs: which server job the panel follows', () => {
  const j = (id, state, started = 100) => ({ id, verb: 'restart', state, stage: '', rc: null, error: '', started });
  assert.equal(shouldTrack(null, null), false);
  assert.equal(shouldTrack(null, j('a', 'running')), true);
  assert.equal(shouldTrack(j('a', 'running'), j('a', 'running')), false);       // already polling it
  assert.equal(shouldTrack(j('a', 'failed'), j('a', 'running')), true);        // lost contact, the job lives: resync
  assert.equal(shouldTrack(null, j('a', 'done')), false);                      // a success before we looked is no news
  assert.equal(shouldTrack(null, j('a', 'failed')), true);                     // a failure is
  assert.equal(shouldTrack(j('a', 'failed'), j('a', 'failed')), false);        // shown / dismissed already
  assert.equal(shouldTrack({ ...j('a', 'failed'), lost: true }, j('a', 'done')), true);   // "lost contact" -> the real outcome
});

test('S2: a group / directory box ticks only the files not hidden from Claude, shows a partial pick', () => {
  const files = [{ path: 'a', sensitive: false }, { path: 'b', sensitive: false }, { path: '.env', sensitive: true }];
  let p = pickState(files, new Set());
  assert.deepEqual([p.checked, p.indeterminate, p.disabled, p.bulk], [false, false, false, ['a', 'b']]);
  p = pickState(files, new Set(['a']));
  assert.deepEqual([p.checked, p.indeterminate], [false, true]);
  // all the normal files ticked: the hidden one keeps the box partial - it is ticked by hand only
  p = pickState(files, new Set(['a', 'b']));
  assert.deepEqual([p.checked, p.indeterminate], [false, true]);
  p = pickState(files, new Set(['a', 'b', '.env']));
  assert.deepEqual([p.checked, p.indeterminate], [true, false]);
  // only hidden files: nothing to tick in bulk
  assert.equal(pickState([{ path: '.env', sensitive: true }], new Set()).disabled, true);
});

test('S2: the panel offers Push for commits ahead or a branch not on a remote, never detached', () => {
  assert.equal(canPush({ branch: 'f', upstream: 'origin/f', ahead: 1 }), true);
  assert.equal(canPush({ branch: 'f', upstream: 'origin/f', ahead: 0 }), false);
  assert.equal(canPush({ branch: 'f', upstream: '', ahead: 0 }), true);
  assert.equal(canPush({ branch: '', upstream: '', ahead: 2 }), false);
});

test('commit A: Commit pins the checked files - both rename paths, refusals named, unviewed counted', () => {
  const v = (oid) => ({ oid, mode: '100644' });
  const row = (path, extra = {}) => ({ path, status: 'M', sensitive: false, withheld: false, committable: true, before: v('b' + path), after: v('a' + path), work_sha: 's', work_mode: '100644', ...extra });
  const review = { head: 'h1', files: [row('a.ts'), row('new.ts', { status: 'R', orig: 'old.ts', orig_after: { oid: null, mode: null } }),
    row('gone.ts', { committable: false }), row('far.ts', { withheld: true })] };
  const viewed = new Map([['a.ts', reviewKey(review.files[0], 'h1')], ['new.ts', 'an older version']]);
  const plan = commitPlan(['a.ts', 'new.ts', 'gone.ts', 'far.ts', 'nowhere.ts'], review, viewed);
  assert.deepEqual(plan.versions, { 'a.ts': v('aa.ts'), 'new.ts': v('anew.ts'), 'old.ts': { oid: null, mode: null } });
  assert.deepEqual(plan.blocked, ['gone.ts', 'far.ts']);
  assert.deepEqual(plan.missing, ['nowhere.ts']);
  assert.deepEqual(plan.unviewed, ['new.ts'], 'a Viewed mark of another version does not count');
  assert.deepEqual(commitPlan([], review, viewed).versions, {});
});

test('commit A: one primary button by state - Commit, then Push…, then Create MR…', () => {
  const pushed = { branch: 'f', upstream: 'origin/f', ahead: 0 };
  assert.equal(primaryAction({ ...pushed, ahead: 2 }, true, true), 'commit');
  assert.equal(primaryAction({ ...pushed, ahead: 2 }, false, false), 'push');
  assert.equal(primaryAction({ branch: 'f', upstream: '', ahead: 0 }, false, false), 'push');
  assert.equal(primaryAction(pushed, false, true), 'mr');
  assert.equal(primaryAction(pushed, false, false), '');
});

test('commit A: Commit says what it lacks - a message, checked files - else its key; never "N of M"', () => {
  assert.deepEqual(commitHint(2, '  ', false), { text: 'Write a message', warn: true });
  assert.deepEqual(commitHint(0, '', true), { text: 'Write a message', warn: true }, 'an amend needs one too');
  assert.deepEqual(commitHint(2, 'fix: x', false), { text: '⌘⏎', warn: false });
  assert.deepEqual(commitHint(0, '', false), { text: 'Check files to commit', warn: false });
  assert.deepEqual(commitHint(0, 'fix: x', false), { text: 'Check files to commit', warn: false });
});

test('F07: the Commit button carries the count of the checked files ("1 of 10" counted Unversioned too)', () => {
  assert.equal(commitLabel(0, false), 'Commit');
  assert.equal(commitLabel(1, false), 'Commit 1 file');
  assert.equal(commitLabel(3, false), 'Commit 3 files');
  assert.equal(commitLabel(0, true), 'Amend');
  assert.equal(commitLabel(2, true), 'Amend 2 files');
});

test('F07: Review has one green button - Prepare task only while review items are open, else Commit', () => {
  assert.equal(reviewPrimary(0), 'commit');
  assert.equal(reviewPrimary(2), 'send');
});

test('commit A: the MR draft note says it came from the branch and names withheld files', () => {
  assert.equal(generatedNote('the branch', ['config/db.properties'], true),
    'Drafted by Haiku from the branch. db.properties is hidden and was not sent. The diff was cut to fit.');
  assert.equal(generatedNote(1, [], false), 'Drafted by Haiku from 1 file.');
});

test('commit A: a refused commit names the files that changed after the click (both rename paths)', () => {
  const v = (oid) => ({ oid, mode: '100644' });
  const pinned = { 'a.ts': v('1'), 'b.ts': v('2'), 'new.ts': v('3'), 'old.ts': { oid: null, mode: null } };
  const now = { files: [
    { path: 'a.ts', after: v('1') },
    { path: 'b.ts', after: v('2b') },
    { path: 'new.ts', orig: 'old.ts', after: v('3'), orig_after: { oid: null, mode: null } },
  ] };
  assert.deepEqual(changedSince(pinned, now), ['b.ts']);
  assert.deepEqual(changedSince({ 'gone.ts': v('9') }, now), ['gone.ts'], 'a path out of the list changed too');
  assert.deepEqual(changedSince({ 'a.ts': { oid: '1', mode: '100755' } }, now), ['a.ts'], 'a mode change counts');
});

test('commit A: cancelling Amend takes back only the untouched HEAD message it filled in', () => {
  assert.equal(amendOffMessage('track the notes', 'track the notes'), '');
  assert.equal(amendOffMessage('track the notes, edited', 'track the notes'), 'track the notes, edited');
  assert.equal(amendOffMessage('typed before', ''), 'typed before');
});

test('commit A: the MR toast never shows undefined when the forge answers without a number', () => {
  assert.equal(mrCreatedText({ iid: 4, web_url: 'https://g/x/-/merge_requests/4' }, false), 'MR !4 created: https://g/x/-/merge_requests/4');
  assert.equal(mrCreatedText({ iid: 5 }, true), 'PR #5 created');
  assert.equal(mrCreatedText([], false), 'MR created');
  assert.equal(mrCreatedText(null, true), 'PR created');
});

test('a worktree\'s Stack source row - the main checkout\'s stack or its own', () => {
  assert.equal(worktreeSource({ stack: 'demo' }, false), null, 'a main checkout has no choice');
  const main = { stack: 'demo', worktree: { mode: 'main', main: '/Users/x/work/grp/demo' } };
  assert.deepEqual(worktreeSource(main, false), { chip: 'MAIN CHECKOUT', tone: 'main', text: 'grp/demo', switchTo: 'own', switchLabel: 'Own stack…' });
  const own = { stack: null, worktree: { mode: 'own', main: '/Users/x/work/grp/demo' } };
  assert.equal(worktreeSource(own, false).text, 'not set up');
  assert.equal(worktreeSource(own, true).text, 'setting up');
  assert.deepEqual(worktreeSource({ ...own, stack: 'demo-x' }, false),
    { chip: "WORKTREE'S OWN", tone: 'own', text: 'demo-x', switchTo: 'main', switchLabel: 'Use main' });
});
