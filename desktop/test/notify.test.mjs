import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { notifyPlan, ownedByMain, osascriptArgs } = require('../src/lib/notify.cjs');

const session = (id, state, extra = {}) => ({ id, title: 'S' + id, state, project: 'acme/shop', ...extra });
const run = (polls, focused = false) => {
  let memo = null; const out = [];
  for (const state of polls) { const plan = notifyPlan(memo, state, focused); memo = plan.memo; out.push(plan.notes); }
  return out;
};

test('the first poll only records; a change into waiting, attention or trust is announced once', () => {
  const [first, second, third] = run([
    { sessions: [session('1', 'waiting'), session('2', 'working'), session('3', 'working'), session('4', 'working')] },
    { sessions: [session('1', 'waiting'), session('2', 'waiting', { detail: 'turn done' }), session('3', 'attention'), session('4', 'trust')] },
    { sessions: [session('1', 'waiting'), session('2', 'waiting'), session('3', 'attention'), session('4', 'trust')] }]);
  assert.deepEqual(first, []);
  assert.deepEqual(second, [
    { title: 'S2: waiting for you', body: 'turn done', tag: '2', session: '2' },
    { title: 'S3: needs attention', body: 'acme/shop', tag: '3', session: '3' },
    { title: 'S4: waiting for trust', body: 'acme/shop', tag: '4', session: '4' }]);
  assert.deepEqual(third, []);
});

test('other states, a new session and an offline poll do not notify', () => {
  const notes = run([{ sessions: [session('1', 'waiting')] }, { sessions: [session('1', 'working'), session('2', 'waiting')] },
    null, { sessions: [session('1', 'waiting')] }]);
  assert.deepEqual(notes.flat(), []);
});

test('a session set aside for later waits quietly unless it asks for trust or a blocking action points at it', () => {
  const later = { later: 1700000000 };
  const [, quiet] = run([{ sessions: [session('1', 'working', later)] }, { sessions: [session('1', 'waiting', later)] }]);
  assert.deepEqual(quiet, []);
  const [, trust] = run([{ sessions: [session('1', 'working', later)] }, { sessions: [session('1', 'trust', later)] }]);
  assert.equal(trust.length, 1);
  const permission = { level: 'permission', subject: { type: 'session', key: '1' } };
  const [, asks] = run([{ sessions: [session('1', 'working', later)] },
    { sessions: [session('1', 'attention', later)], queue: { actions: [permission] } }]);
  assert.equal(asks.length, 1);
  const waitingOnly = { level: 'waiting', subject: { type: 'session', key: '1' } };
  const [, still] = run([{ sessions: [session('1', 'working', later)] },
    { sessions: [session('1', 'waiting', later)], queue: { actions: [waitingOnly] } }]);
  assert.deepEqual(still, []);
});

test('a new confirmation card is announced with piperd wording, a seen one is not', () => {
  const card = { id: 7, title: 'Build', kind: 'git_push', session: '1' };
  const action = { key: 'p:7', level: 'confirm', title: 'Build: wants to push' };
  const [first, second, third] = run([
    { sessions: [], pending: [] },
    { sessions: [], pending: [card], queue: { actions: [action] } },
    { sessions: [], pending: [card, { id: 8, title: 'Docs', kind: 'send_to_session' }] }]);
  assert.deepEqual(first, []);
  assert.deepEqual(second, [{ title: 'Build: wants to push', body: 'Waiting for your confirmation', tag: 'p:7', session: '1' }]);
  assert.deepEqual(third, [{ title: 'Docs: send to session', body: 'Waiting for your confirmation', tag: 'p:8', session: '' }]);
  // cards present at the first poll are not announced
  assert.deepEqual(run([{ sessions: [], pending: [card] }, { sessions: [], pending: [card] }]).flat(), []);
});

test('while a Piper window has focus the GUI notifies, the main process only records', () => {
  const polls = [{ sessions: [session('1', 'working')], pending: [] },
    { sessions: [session('1', 'waiting')], pending: [{ id: 1, title: 'x', kind: 'git_commit' }] }];
  assert.deepEqual(run(polls, true).flat(), []);
  let memo = notifyPlan(null, polls[0], false).memo;
  memo = notifyPlan(memo, polls[1], true).memo;
  assert.deepEqual(notifyPlan(memo, polls[1], false).notes, []);
});

test('the GUI copy of a state or card notification is recognised; inbox, CI and production ones are not', () => {
  assert.ok(ownedByMain({ title: 'Build: waiting for you', tag: 's1' }));
  assert.ok(ownedByMain({ title: 'Build: needs attention', tag: 's1' }));
  assert.ok(ownedByMain({ title: 'Build: waiting for trust', tag: 's1' }));
  assert.ok(ownedByMain({ title: 'Build: wants to push', tag: 'p:3' }));
  assert.ok(!ownedByMain({ title: 'Pipeline failed: acme/shop', tag: 's1' }));
  assert.ok(!ownedByMain({ title: 'Production', tag: 'piper-prod' }));
  assert.ok(!ownedByMain({ title: 'Assigned', tag: 'inbox:k:new' }));
});

test('the osascript fallback passes text as arguments after --, never as script source', () => {
  const args = osascriptArgs('-e "x" & do shell script "id"', 'b'.repeat(3000));
  const end = args.indexOf('--');
  assert.ok(end > 0);
  assert.equal(args[end + 1], '-e "x" & do shell script "id"');
  assert.equal(args[end + 2].length, 2000);
  assert.ok(args.slice(0, end).every(a => !a.includes('shell')));
});
