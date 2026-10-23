// node --test: compact session list and its metadata.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildQueue, capChips, rowChips, rowSummary } from '../../piper/web/dist/queue.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');

const NOW = 1_800_000_000;
const sess = (id, extra = {}) => ({ id, kind: 'project', project: 'team-x/demo', path: '/p/demo', title: id, task: '', state: 'working', phase: '', detail: '',
  created: NOW - 3600, pid: 1, instance: null, blocks: [], attest: null, rc: null, origin: 'gui', claude_session: null, gitlab: [], profile: null,
  hidden: false, notice: '', claude_mode: 'session', task_sent: true, network: {}, watch: [], since: NOW - 240, ...extra });
const act = (key, level, source, subject, extra = {}) => ({ key, kind: 'action', level, source, title: key, body: '', quote: null, subject,
  since: NOW - 100, actions: [], ...extra });
const input = (over = {}) => ({ sessions: [], actions: [], events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [],
  label: (s) => s, now: NOW, ...over });

test('compact sessions keep search visible and ignore obsolete stored filters', () => {
  const q = read('queue.ts');
  const head = q.slice(q.indexOf('function head('), q.indexOf('function secHead('));
  assert.doesNotMatch(q, /piper\.queue\.filter|searchOpen|q-filters/);
  assert.match(head, /placeholder: 'Find a session…'/);
  assert.match(head, /'aria-label': 'new session'/);
  assert.match(head, /deps\.palette\(\)/);
  assert.match(head, /h\('h2', \{\}, 'Overview'\)/);
  assert.match(head, /'aria-label': 'collapse to the strip'/);
  assert.match(head, /deps\.fold/);
  assert.match(head, /\+ Session/);
  assert.match(css, /\.q-collapse svg \{[^}]*width: 16px;[^}]*stroke: currentColor/);
  assert.doesNotMatch(read('main.ts'), /class: `dh-state/);
});

test('W17 rowChips: project · issue · L5 · subagents · shells · monitors · terminal · text held', () => {
  const s = sess('s', { issue: { iid: 42, type: 'issue', title: 'VAT' }, watch: ['/a', '/b'], subagents: { a: 'x', b: 'y' },
    background_tasks: { t1: 'shell', t2: 'monitor', t3: 'monitor' }, tty: true, hold: 'draft' });
  assert.deepEqual(rowChips(s, undefined).map((c) => c.label),
    ['team-x/demo', '#42', 'L5 · 2', '2 subagents', '1 shell', '2 monitors', 'terminal', 'text held']);
  assert.equal(rowChips(s, undefined)[0].proj, true, 'the project chip is marked: a group header drops it');
  assert.deepEqual(rowChips(sess('g', { kind: 'advisor' }), undefined).map((c) => c.label), ['all projects']);
  assert.deepEqual(rowChips(sess('o', { subagents: { a: 'x' } }), undefined).map((c) => c.label), ['team-x/demo', '1 subagent']);
  assert.equal(rowChips(s, undefined, [{ label: 'team-x/demo #42', cls: 'link' }]).some((c) => c.label === '#42'), false, 'a GitLab item\'s issue is not repeated');
});

test('W17 capChips: four, then +N with the rest in the tooltip', () => {
  const c = (n) => Array.from({ length: n }, (_, i) => ({ label: 'c' + i }));
  assert.deepEqual(capChips(c(4)).map((x) => x.label), ['c0', 'c1', 'c2', 'c3']);
  const six = capChips(c(6));
  assert.deepEqual(six.map((x) => x.label), ['c0', 'c1', 'c2', 'c3', '+2']);
  assert.equal(six[4].title, 'c4 · c5');
});

test('session rows omit workflow next-step labels, including Review and Push', () => {
  const issue = { iid: 42, type: 'issue', title: 'VAT' };
  for (const flow of [
    { analysis: true, impl: true, verify: 0, draft: null, commits: 0, ahead: 0, pushed: false },
    { analysis: true, impl: true, verify: 1, draft: { sent: true }, commits: 1, ahead: 1, pushed: false },
  ]) {
    for (const state of ['working', 'attention']) {
      const s = sess('s', { issue, state });
      const q = buildQueue(input({ sessions: [s], board: new Map([['s', { issue, flow }]]),
        actions: state === 'attention' ? [act('w:s', 'waiting', 'session', { type: 'session', key: 's', session: 's' })] : [] }));
      const row = [...q.needs, ...q.running].find(x => x.session === 's');
      // F01: "project — #42 VAT" is the title, so neither the project nor the issue is a chip again
      assert.equal(row.title, 'demo — #42 VAT');
      assert.deepEqual(row.chips.map(c => c.label), []);
      assert.ok(row.chips.every(c => !c.title?.startsWith('next step:')));
    }
  }
});

test('W17 one anatomy: Running says the state in words, the facts are the same chips as in Needs you', () => {
  const s = sess('vat', { subagents: { a: 'x', b: 'y' }, background_tasks: { t: 'shell' }, tty: true });
  const q = buildQueue(input({ sessions: [s] }));
  assert.equal(q.running[0].sub, 'working', 'only the state: no counts, no project, no "in a terminal"');
  assert.equal(q.running[0].status, '4 min', 'F01: working = its time alone (the section says the state)');
  assert.deepEqual(q.running[0].chips.map((c) => c.label), ['2 subagents', '1 shell', 'terminal'], 'F01: the project is in the title');
  const idle = buildQueue(input({ sessions: [sess('i', { state: 'ready' })], board: new Map([['i', { last: 'Done, the check passes.' }]]) }));
  assert.equal(idle.running[0].sub, 'ready · Done, the check passes.', 'idle: the state + the last answer');
  assert.equal(idle.running[0].title, 'demo — Done, the check passes.', 'F01: project — the sentence it ended with');
  assert.equal(idle.running[0].status, 'ready 4 min');
  const waiting = buildQueue(input({ sessions: [{ ...s, state: 'attention' }],
    actions: [act('perm:vat', 'permission', 'session', { type: 'session', key: 'vat', session: 'vat' })] }));
  assert.deepEqual(waiting.needs[0].chips.map((c) => c.label), q.running[0].chips.map((c) => c.label), 'the same chips in Needs you');
  const confirm = buildQueue(input({ sessions: [s], actions: [act('c:1', 'confirm', 'confirm', { type: 'session', key: 'vat', session: 'vat' })] }));
  assert.match(confirm.needs[0].title, /^demo — /, 'a confirmation names its project too (F01: in the title)');
});

test('compact rows keep project metadata and important detail, without last-message previews', () => {
  const a = sess('a', { state: 'ready' }), b = sess('b');
  const q = buildQueue(input({ sessions: [a, b], board: new Map([['a', { last: 'Done. Would you like more?' }]]) }));
  // F01: a session row's second line is its state + time, never the last message
  assert.deepEqual(q.running.map(x => rowSummary(x)), ['ready 4 min', '4 min']);
  assert.ok(q.running.every(x => x.title.startsWith('demo') && !x.chips.some(c => c.label === 'team-x/demo')));
  const waiting = { ...q.running[0], status: undefined, action: { level: 'waiting' }, sub: 'Last answer?' };
  assert.equal(rowSummary(waiting), '');
  assert.equal(rowSummary({ ...waiting, action: { level: 'permission' }, sub: 'Allow access' }), 'Allow access');
  assert.equal(rowSummary({ ...waiting, action: { level: 'confirm', body: 'git push' } }), '', 'full confirmation body is rendered separately');
  assert.equal(rowSummary({ ...waiting, action: { level: 'blocked' }, sub: 'Mount refused' }), 'Mount refused');
  const stopped = buildQueue(input({ sessions: [sess('err', { state: 'error', detail: 'Start failed', last_prompt: 'Old task' }), sess('stop', { state: 'stopping', since: NOW - 15 })] }));
  assert.equal(rowSummary(stopped.ended[0]), 'Start failed');
  assert.match(rowSummary(stopped.needs[0]), /ignores TERM/);
  const src = read('queue.ts');
  assert.match(src, /box\.append\(\.\.\.sec\.running\.map\(item\)\)/);
  assert.match(src, /const summary = rowSummary\(it\)/);
  assert.match(src, /summary \? h\('span', \{ class: 'qi-sub'/);
  assert.doesNotMatch(src, /h\('pre', \{ class: 'qi-body' \}/, 'F01: the whole card is the floating one (⌘J)');
  assert.match(src, /'Find & resume'\)/);
});

test('compact rows wrap titles, align counts and retain accessible state', () => {
  assert.match(css, /\.qi-title \{[^}]*-webkit-line-clamp: 2/);
  assert.match(css, /\.qi\.confirm \.qi-title \{[^}]*-webkit-line-clamp: unset/);
  assert.match(css, /\.q-heading, \.q-sechead \.q-fold \{[^}]*justify-content: space-between/);
  assert.match(read('queue.ts'), /'aria-label': glyphWord\(it\), title: glyphWord\(it\)/);
  assert.match(read('main.ts'), /role: 'img', 'aria-label': word/);
});

test('Inbox and Production remain separate activity-bar views with their badges', () => {
  const m = read('main.ts');
  assert.match(m, /!viewOn\('gitlab'\) \? null : item\('inbox', inboxName\(state\.inbox\), '', view === 'gitlab'[^\n]*setView\('gitlab'\)/);
  assert.match(m, /badge\(state\.inbox\?\.new \?\? 0, ''/);
  assert.match(m, /on\('prod'\) && state\.prod\?\.on && \{ id: 'prod', label: 'Production'[^\n]*active: view === 'prod'[^\n]*setView\('prod'\)[^\n]*count: state\.prod\.new \?\? 0/);
  assert.match(m, /inbox: \['M4 4h16/);
  assert.ok(!m.includes('M12 21l-9-7l2.5-10l3 7h7l3-7l2.5 10z'), 'the shared inbox never uses the GitLab logo');
  assert.match(m, /prod: \['M3 12h4/);
});

test('queue issue chips omit the exact reference repeated in the displayed title', () => {
  const issue = { iid: 42, type: 'issue', title: 'VAT' };
  const labels = (title, type = 'issue') => rowChips(sess('s', { title, issue: { ...issue, type } }), undefined).map(c => c.label);
  assert.deepEqual(labels('#42 VAT'), ['team-x/demo']);
  assert.deepEqual(labels('Fix (#42): VAT'), ['team-x/demo']);
  assert.deepEqual(labels('#420 VAT'), ['team-x/demo', '#42']);
  assert.deepEqual(labels('#42x VAT'), ['team-x/demo', '#42']);
  assert.deepEqual(labels('VAT'), ['team-x/demo', '#42']);
  assert.deepEqual(labels('!42 VAT', 'mr'), ['team-x/demo']);
  assert.deepEqual(labels('#42 VAT', 'mr'), ['team-x/demo', '!42']);
  const s = sess('s', { title: '#42 VAT', issue });
  const running = buildQueue(input({ sessions: [s] }));
  assert.deepEqual(running.running[0].chips.map(c => c.label), []);
  const waiting = buildQueue(input({ sessions: [s], actions: [act('w', 'waiting', 'session', { type: 'session', key: 's', session: 's' })] }));
  assert.deepEqual(waiting.needs[0].chips.map(c => c.label), []);
  const confirm = buildQueue(input({ sessions: [s], actions: [act('c', 'confirm', 'confirm', { type: 'session', key: 's', session: 's' }, { title: 'Allow command' })] }));
  assert.deepEqual(confirm.needs[0].chips.map(c => c.label), ['#42'], 'a confirmation with a different title keeps the reference');
});
