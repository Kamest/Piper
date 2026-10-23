// node --test: sessions a restart interrupted - the restore card's and the queue section's pure parts
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { causeTitle, howWord, interruptedStatus, preselect, restoreLabel, restoreWanted, sectionTitle, slotsLine, wasWord } from '../../piper/web/dist/restorenotice.js';
import { buildQueue } from '../../piper/web/dist/queue.js';

const NOW = 1_800_000_000;
const sess = (id, extra = {}) => ({ id, kind: 'project', project: 'team-x/shop', path: '/p/shop', title: id, task: '', state: 'exited', phase: '', detail: '',
  created: NOW - 3600, pid: null, instance: null, blocks: [], attest: null, rc: -1, origin: 'gui', claude_session: 'c', gitlab: [], profile: null,
  hidden: false, notice: '', claude_mode: 'session', task_sent: true, network: {}, watch: [], since: NOW - 60, ...extra });
const mark = (was, cause = 'reboot', resumes = true) => ({ at: NOW - 30, was, cause, resumes });
const input = (sessions) => ({ sessions, actions: [], events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [],
  label: (s) => s, now: NOW });
const notice = (over = {}) => ({ id: NOW, cause: 'reboot', at: NOW, limit: 4, seen: false, sessions: [{ id: 'a', was: 'working' }], ...over });

test('what it was doing, in words: working and waiting are unfinished work', () => {
  assert.equal(wasWord('working'), 'was working');
  assert.equal(wasWord('waiting'), 'waited for you');
  assert.equal(wasWord('attention'), 'waited for you');
  assert.equal(wasWord('ready'), 'idle');
  assert.equal(wasWord('preflight'), 'was starting');
  assert.equal(howWord(true), 'resumes');
  assert.equal(howWord(false), 'starts again with its task');
});

test('the card checks the unfinished work, never more than the free slots (no queue over the limit)', () => {
  const rows = [{ id: 'a', was: 'ready' }, { id: 'b', was: 'working' }, { id: 'c', was: 'waiting' }, { id: 'd', was: 'working' }];
  assert.deepEqual([...preselect(rows, 9)], ['b', 'c', 'd']);
  assert.deepEqual([...preselect(rows, 2)], ['b', 'c']);
  assert.deepEqual([...preselect(rows, 0)], []);
  assert.deepEqual([...preselect(rows, -1)], []);
  assert.equal(restoreLabel(0), 'Restore');
  assert.equal(restoreLabel(1), 'Restore 1 session');
  assert.equal(restoreLabel(3), 'Restore 3 sessions');
  assert.match(slotsLine(2, 4), /^2 of 4 session slots free/);
  assert.match(slotsLine(0, 4), /^0 of 4 slots free\. Stop a session or raise the limit/);
});

test('the cause decides the words: a reboot, Piper itself, its terminal host', () => {
  assert.equal(causeTitle('reboot', 3), 'Your computer restarted — 3 sessions stopped');
  assert.equal(causeTitle('reboot', 1), 'Your computer restarted — 1 session stopped');
  assert.equal(causeTitle('piperd', 2), 'Piper restarted — 2 sessions did not start');
  assert.equal(causeTitle('ptyhost', 2), "Piper's terminal host restarted — 2 sessions stopped");
  assert.equal(sectionTitle(['reboot', 'reboot']), 'Interrupted by restart');
  assert.equal(sectionTitle(['reboot', 'ptyhost']), 'Interrupted');
  assert.equal(sectionTitle([]), 'Interrupted');
});

test('the queue row says what it was doing, when and what Continue does', () => {
  const at = new Date(2026, 9, 22, 8, 4).getTime() / 1000;
  assert.equal(interruptedStatus({ was: 'working', resumes: true }, at), 'was working · 08:04 · resumes its conversation');
  assert.equal(interruptedStatus({ was: 'preflight', resumes: false }, at), 'was starting · 08:04 · starts again with its task');
});

test('opened on its own once per notice, not after it was seen, never over the setup or another dialog', () => {
  assert.equal(restoreWanted(notice(), 0, false), true);
  assert.equal(restoreWanted(notice(), NOW, false), false, 'this page showed it');
  assert.equal(restoreWanted(notice({ id: NOW + 5 }), NOW, false), true, 'a later interruption');
  assert.equal(restoreWanted(notice({ seen: true }), 0, false), false, 'another tab showed it');
  assert.equal(restoreWanted(notice(), 0, true), false);
  assert.equal(restoreWanted(notice({ sessions: [] }), 0, false), false);
  assert.equal(restoreWanted(null, 0, false), false);
  assert.equal(restoreWanted(undefined, 0, false), false);
});

test('marked sessions leave Ended for their own section, the unfinished work first', () => {
  const q = buildQueue(input([
    sess('idle', { interrupted: mark('ready'), since: NOW - 10 }),
    sess('work', { interrupted: mark('working'), since: NOW - 500 }),
    sess('wait', { interrupted: mark('waiting', 'reboot', false), since: NOW - 100 }),
    sess('done', { rc: 0 }),
    sess('agent', { kind: 'advisor', interrupted: mark('working') }),
  ]));
  assert.deepEqual(q.interrupted.map((x) => x.key), ['i:wait', 'i:work', 'i:idle']);
  assert.ok(q.interrupted.every((x) => x.glyph === 'intr' && x.section === 'interrupted'));
  assert.match(q.interrupted[0].status, /^waited for you · \d\d:\d\d · starts again with its task$/);
  assert.match(q.interrupted[0].find, /interrupted restart/);
  const ended = q.ended.map((x) => x.session);
  assert.ok(ended.includes('done'));
  assert.ok(!ended.some((id) => ['idle', 'work', 'wait'].includes(id)), 'not repeated under Ended');
});

test('a dismissed mark or a running session is not in the section', () => {
  const q = buildQueue(input([sess('a', { interrupted: null }), sess('b', { state: 'working', pid: 1, interrupted: mark('working') })]));
  assert.deepEqual(q.interrupted, []);
});
