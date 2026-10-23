// node --test: the Overview queue's pure helpers, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ACTIVE, allowDanger, askLine, buildQueue, canDismiss, canForget, canStop, continueLabel, duration, emptyLine, firstLine, freeze, KILL_AFTER_S, matchesText, menuSession, netRestorable, restoreToast, sessionDate, sessionOrder } from '../../piper/web/dist/queue.js';

const NOW = 1_800_000_000;
const sess = (id, extra = {}) => ({ id, kind: 'project', project: 'shop', path: '/p/shop', title: id, task: '', state: 'working', phase: '', detail: '',
  created: NOW - 3600, pid: 1, instance: null, blocks: [], attest: null, rc: null, origin: 'gui', claude_session: null, gitlab: [], profile: null,
  hidden: false, notice: '', claude_mode: 'session', task_sent: true, network: {}, watch: [], since: NOW - 60, ...extra });
const act = (key, level, source, subject, extra = {}) => ({ key, kind: 'action', level, source, title: key, body: '', quote: null, subject,
  since: NOW - 100, actions: [], ...extra });
const input = (over = {}) => ({ sessions: [], actions: [], events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [],
  label: (s) => s, now: NOW, ...over });

test('actions split into Needs you (security…waiting) and New (ci, GitLab, production), in piperd\'s order', () => {
  const s1 = sess('s1', { state: 'waiting' });
  const q = buildQueue(input({
    sessions: [s1],
    actions: [
      act('p:1', 'confirm', 'confirm', { type: 'session', key: 's1', label: 's1', session: 's1' }, { body: 'git push origin test\n⚠ push --force' }),
      act('wait:s1:1', 'waiting', 'session', { type: 'session', key: 's1', label: 's1', session: 's1' }),
      act('gl:x', 'gitlab', 'gitlab', { type: 'issue', key: 'g#issue#4', label: 'shop #4' }, { quote: { author: 'Ann', text: 'Can you?' } }),
      act('prod:d', 'prod', 'prod', { type: 'prod', key: 'prod', label: 'Production' }),
    ],
  }));
  assert.deepEqual(q.needs.map((x) => x.key), ['a:p:1']);
  assert.deepEqual(q.new.map((x) => [x.key, x.glyph]), [['a:prod:d', 'new']], 'D11: forge items stay in the Inbox view');
  assert.equal(q.needs[0].glyph, 'stuck', 'F01: Blocked is the red mark');
  assert.equal(q.needs[0].sub, 'git push origin test');
  assert.match(q.needs[0].title, / — git push origin test$/, 'F01: project — the command');
  assert.match(q.needs[0].status, /^asks 1 min · Allow \/ Deny /);
  assert.ok(!q.needs[0].chips.some((c) => c.cls === 'bad'), 'F01: its ⚠ lines are the card\'s (⌘J), the row stays two lines');
  const lone = buildQueue(input({ actions: [act('p:2', 'confirm', 'confirm', { type: 'project', key: 'x', label: 'x' }, { body: 'git push\n⚠ push --force' })] }));
  assert.deepEqual(lone.needs[0].chips[0], { label: '⚠ push --force', cls: 'bad' }, 'a session-less card keeps its warning as a chip');
  assert.equal(q.running.length, 0, 'a session with an open need is not listed twice');
});

test('blocking requests suppress only that sessions duplicate waiting row', () => {
  const subject = id => ({ type: 'session', key: id, session: id, label: id });
  const q = buildQueue(input({ sessions: [sess('s1'), sess('s2')], actions: [
    act('wait:s1', 'waiting', 'session', subject('s1')),
    act('security:s1', 'security', 'session', subject('s1')),
    act('confirm:s1', 'confirm', 'confirm', subject('s1')),
    act('wait:s2', 'waiting', 'session', subject('s2')),
  ] }));
  assert.deepEqual(q.needs.map(x => x.key), ['a:security:s1', 'a:confirm:s1', 'a:wait:s2']);
  assert.equal(q.running.length, 0);
});

test('a waiting session says what Claude asks (its last question), from the board card', () => {
  const s1 = sess('s1', { state: 'waiting', title: '#4 feeds' });
  const board = new Map([['s1', { id: 's1', last: 'Done:\n- tests pass\nShall I run curl on test?\n```\nx\n```', asked: '', changed: 3, tokens: 1 }]]);
  const q = buildQueue(input({ sessions: [s1], board, actions: [act('wait:s1:1', 'waiting', 'session', { type: 'session', key: 's1', label: '', session: 's1' })] }));
  assert.match(q.needs[0].title, / — Shall I run curl on test\?$/, 'F01: project — the sentence it ended with');
  assert.equal(q.needs[0].sub, 'Shall I run curl on test?');
  assert.match(q.needs[0].status, /^question /, 'F01: a question, not "turn done"');
  assert.equal(askLine('**Bold** end'), 'Bold end');
  assert.equal(firstLine('[tool Bash]\n\n# Title'), 'Title');
});

test('a real permission request stays visible while its subagents run', () => {
  const q = buildQueue(input({ sessions: [sess('s1', { state: 'attention', subagents: { a: 'reviewer', b: 'tester' } })],
    actions: [act('perm:s1', 'permission', 'session', { type: 'session', key: 's1', session: 's1', label: 's1' })] }));
  assert.equal(q.needs.length, 1);
  assert.equal(q.needs[0].glyph, 'stuck', 'F01: a permission blocks (Blocked, red)');
  assert.ok(q.needs[0].chips.some((c) => c.label === '2 subagents'), 'W17: the subagents chip, in its place after the project');
  assert.equal(q.running.length, 0);
});

test('background shells and monitors say Running; a real permission remains Needs you', () => {
  const s = sess('boss', { background_tasks: { shell1: 'shell', monitor1: 'monitor' } });
  const q = buildQueue(input({ sessions: [s] }));
  assert.equal(q.needs.length, 0);
  assert.equal(q.running[0].glyph, 'run');
  assert.deepEqual(q.running[0].chips.filter((c) => c.cls === 'ok').map((c) => c.label), ['1 shell', '1 monitor'], 'W17: chips, not prose');
  const permission = buildQueue(input({ sessions: [{ ...s, state: 'attention' }],
    actions: [act('perm:boss', 'permission', 'session', { type: 'session', key: 'boss', session: 'boss' })] }));
  assert.equal(permission.running.length, 0);
  assert.deepEqual(permission.needs[0].chips.filter((c) => c.cls === 'ok').map((c) => c.label), ['1 shell', '1 monitor']);
});

test('a working session with an L5 warning stays Running; another sessions need stays separate', () => {
  const subject = id => ({ type: 'session', key: id, session: id, label: id });
  const worker = sess('worker', { watch: ['/library/config/new.properties'] });
  const actions = [act('l5:worker', 'security', 'session', subject('worker')),
    act('l5:other', 'security', 'session', subject('other')),
    act('perm:other', 'permission', 'session', subject('other'))];
  const other = sess('other', { state: 'attention', watch: ['/library/config/new.properties'] });
  const q = buildQueue(input({ sessions: [worker, other], actions }));
  assert.deepEqual(q.needs.map(x => x.session), ['other']);
  assert.deepEqual(q.running.map(x => [x.session, x.glyph]), [['worker', 'run']]);
  assert.equal(q.running[0].chips.find(x => x.label === 'L5 · 1')?.cls, 'warn');
  const permission = act('perm:worker', 'permission', 'session', subject('worker'));
  const pending = buildQueue(input({ sessions: [worker, other], actions: [...actions, permission] }));
  assert.deepEqual(pending.needs.map(x => x.session), ['other', 'worker']);
  assert.equal(pending.running.length, 0);
  const idle = buildQueue(input({ sessions: [{ ...worker, state: 'waiting', idle: true }, other],
    actions: [...actions, act('wait:worker', 'waiting', 'session', subject('worker'))] }));
  assert.deepEqual(idle.needs.map(x => x.session), ['other', 'worker']);
  const flagged = buildQueue(input({ sessions: [worker, other], actions: actions.slice(0, 2) }));
  assert.equal(flagged.needs.length, 0);
  assert.equal(flagged.running.length, 2);
});

test('Running: pins, then the Global agent, then the user\'s order, then the oldest; hidden / prodcheck / ended stay out', () => {
  const a = sess('a', { created: NOW - 10 }), b = sess('b', { created: NOW - 20 }), c = sess('c', { created: NOW - 30 });
  const adv = sess('adv', { kind: 'advisor', title: 'x' });
  const q = buildQueue(input({
    sessions: [a, b, c, adv, sess('h', { hidden: true }), sess('p', { kind: 'prodcheck' }), sess('e', { state: 'exited' })],
    pins: new Set(['a']), order: ['b'],
  }));
  assert.deepEqual(q.running.map((x) => x.session), ['a', 'adv', 'b', 'c']);
  assert.equal(q.running[1].title, 'Global agent');
  assert.deepEqual(q.ended.map((x) => x.session), ['e']);
});

test('the marks: working spins, idle is a dot; a stop that hangs goes to Needs you with the KILL countdown', () => {
  const q = buildQueue(input({ sessions: [
    sess('w', { state: 'working' }), sess('r', { state: 'ready' }),
    sess('st', { state: 'stopping', since: NOW - 12 }), sess('st2', { state: 'stopping', since: NOW - 3 }), sess('old', { state: 'stopping', since: NOW - 7200 }),
  ] }));
  assert.deepEqual(q.running.map((x) => [x.session, x.glyph]), [['w', 'run'], ['r', 'idle'], ['st2', 'off']]);
  const adv = buildQueue(input({ sessions: [sess('a', { kind: 'advisor', state: 'attention' })] }));
  assert.equal(adv.running[0].glyph, 'wait', 'the Global agent at a prompt that needs you is not a green dot');
  const stuck = q.needs.find((x) => x.session === 'st');
  assert.equal(stuck.glyph, 'stuck');
  assert.match(stuck.title, /stopping 12 s$/);
  assert.match(stuck.sub, new RegExp(`KILL in ${KILL_AFTER_S - 12} s`));
  const old = q.needs.find((x) => x.session === 'old');
  assert.match(old.title, /stopping 2 h$/);
  assert.match(old.sub, /KILL sent/);
  assert.deepEqual([duration(59), duration(600), duration(9000)], ['59 s', '10 min', '2 h']);
});

test('Recently: the Feed\'s events, newest first, unread after `seen`', () => {
  const ev = (id, ts) => ({ id, key: id, kind: 'event', source: 'session', title: id, body: '', subject: { type: 'session', key: 's', label: 's' }, ts, n: 1 });
  const q = buildQueue(input({ events: [ev('e1', NOW - 50), ev('e2', NOW - 10)], seen: NOW - 30 }));
  assert.deepEqual(q.recent.map((x) => [x.key, x.unread]), [['e:e2', true], ['e:e1', false]]);
});

test('a CI item links a session of its project, but its menu (Stop, Remove from list) never acts on that session', () => {
  const q = buildQueue(input({
    sessions: [sess('s1', { state: 'waiting' }), sess('s2', { state: 'exited' })],
    actions: [act('wait:s1:1', 'waiting', 'session', { type: 'session', key: 's1', label: '', session: 's1' }),
      act('ci:1', 'ci', 'ci', { type: 'project', key: 'shop', label: 'shop', project: 'shop', session: 's2' })],
    events: [{ id: 'e1', ts: NOW - 5, source: 'ci', title: 'CI shop passes again', body: '', n: 1, subject: { type: 'project', key: 'shop', label: 'shop', session: 's2' } }],
  }));
  const ci = q.new.find((x) => x.source === 'ci');
  assert.equal(ci.session, 's2', 'Open still goes to the project session');
  assert.equal(menuSession(ci), '');
  assert.equal(menuSession(q.recent[0]), '');
  assert.equal(menuSession(q.needs[0]), 's1');
  assert.equal(menuSession(q.ended[0]), 's2');
});

test('all queue sources stay searchable without filter pills', () => {
  const q = buildQueue(input({ sessions: [sess('s1')], actions: [
    act('ci:1', 'ci', 'ci', { type: 'project', key: 'shop', label: 'shop' }),
    act('prod:1', 'prod', 'prod', { type: 'project', key: 'shop', label: 'shop' }),
  ] }));
  assert.equal(q.running.length, 1);
  assert.equal(q.new.length, 2);
  assert.equal(matchesText(q.new[0], 'CI:1'), true);
  assert.equal(matchesText(q.new[0], 'nothing'), false);
});

test('freeze: while you move, known items keep their place and new ones come after them', () => {
  const it = (key) => ({ key });
  assert.deepEqual(freeze(['b', 'a'], [it('a'), it('c'), it('b')]).map((x) => x.key), ['b', 'a', 'c']);
  assert.deepEqual(freeze([], [it('a'), it('b')]).map((x) => x.key), ['a', 'b']);
  assert.deepEqual(freeze(['x', 'a'], [it('a')]).map((x) => x.key), ['a'], 'a gone item leaves no hole');
});

test('Ended includes pinned sessions and failed starts even while their attention row remains open', () => {
  const q = buildQueue(input({ sessions: [sess('err', { state: 'error' }), sess('blk', { state: 'blocked' }), sess('pin', { state: 'exited' }), sess('ex', { state: 'exited' })],
    pins: new Set(['pin']) }));
  assert.deepEqual(q.ended.map((x) => [x.session, x.glyph]).sort(), [['blk', 'stuck'], ['err', 'stuck'], ['ex', 'off'], ['pin', 'off']]);
  assert.deepEqual(q.running.map((x) => [x.session, x.glyph]), [['pin', 'off']]);
  const keys = [...q.running, ...q.ended].map(x => x.key);
  assert.equal(new Set(keys).size, keys.length, 'each row needs its own key for keyboard actions and focus restoration');
  const withAction = buildQueue(input({ sessions: [sess('err', { state: 'error' })],
    actions: [act('err:err:1', 'blocked', 'session', { type: 'session', key: 'err', label: '', session: 'err' })] }));
  assert.equal(withAction.ended[0].session, 'err', 'Ended is a complete list even before dismissing the error');
  assert.equal(withAction.needs[0].session, 'err', 'the error still needs attention');
});

test('an unexpected exit stays in Ended alongside its crash action; active and hidden sessions stay out', () => {
  const q = buildQueue(input({ sessions: [
    sess('crash', { state: 'exited', detail: 'Claude ended unexpectedly (code 1)', claude_session: 'saved' }),
    sess('hidden', { state: 'exited', hidden: true }), sess('live'), sess('stopping', { state: 'stopping' }),
    sess('prod', { state: 'exited', kind: 'prodcheck' }),
  ], actions: [act('crash:crash:1', 'blocked', 'session', { type: 'session', key: 'crash', session: 'crash' })] }));
  assert.deepEqual(q.ended.map(x => [x.session, x.resume]), [['crash', 'Continue']]);
  assert.ok(q.needs.some(x => x.session === 'crash'));
});

test('the search also finds the branch', () => {
  const q = buildQueue(input({ sessions: [sess('s', { branch: '412-feed-fix' })] }));
  assert.equal(matchesText(q.running[0], '412-feed'), true);
});

test('G1: Stop for active sessions except an ongoing stop; Remove only where forget() accepts', () => {
  assert.deepEqual([...ACTIVE].sort(), ['attention', 'attesting', 'preflight', 'preparing', 'ready', 'spawning', 'starting', 'stopping', 'trust', 'waiting', 'working']);
  for (const st of ['preflight', 'preparing', 'spawning', 'working']) {
    assert.equal(canStop({ state: st }), true, st);
    assert.equal(canForget({ state: st }), false, st);
  }
  assert.equal(canStop({ state: 'stopping' }), false, 'a pending stop must not offer another Stop');
  assert.equal(canForget({ state: 'stopping' }), false, 'stopping still counts as active until teardown ends');
  for (const st of ['exited', 'error', 'blocked']) {
    assert.equal(canStop({ state: st }), false, st);
    assert.equal(canForget({ state: st }), true, st);
  }
});

test('G7: one rule for Continue / Retry - only an exited session with a conversation continues; Ended rows carry it', () => {
  assert.equal(continueLabel({ state: 'exited', claude_session: 'c1' }), 'Continue');
  assert.equal(continueLabel({ state: 'exited', claude_session: null }), 'Retry');
  assert.equal(continueLabel({ state: 'error', claude_session: 'c1' }), 'Retry');
  assert.equal(continueLabel({ state: 'blocked', claude_session: null }), 'Retry');
  const q = buildQueue(input({ sessions: [sess('a', { state: 'exited', claude_session: 'c' }), sess('b', { state: 'exited' }), sess('c', { state: 'error', claude_session: 'c' })] }));
  assert.deepEqual(Object.fromEntries(q.ended.map((x) => [x.session, x.resume])), { a: 'Continue', b: 'Retry', c: 'Retry' });
});

test('G4: sessionOrder - pinned first, the Global agent, your order, then the oldest; waiting never moves a session', () => {
  const list = [sess('old', { created: NOW - 900 }), sess('new', { created: NOW - 100 }), sess('wait', { state: 'waiting', created: NOW - 500 }),
    sess('pin', { created: NOW - 50 }), sess('adv', { kind: 'advisor', created: NOW - 10 }), sess('start', { state: 'preflight', pid: null, created: NOW - 5 }),
    sess('gone', { state: 'exited' }), sess('hid', { hidden: true }), sess('pc', { kind: 'prodcheck' })];
  assert.deepEqual(sessionOrder(list, new Set(['pin']), []).map((s) => s.id), ['pin', 'adv', 'old', 'wait', 'new', 'start']);
  assert.deepEqual(sessionOrder(list, new Set(['pin']), ['new', 'wait']).map((s) => s.id), ['pin', 'adv', 'new', 'wait', 'old', 'start']);
  // a waiting session keeps its number while Needs you lists it (its row in Running goes, its place does not)
  const q = buildQueue(input({ sessions: list, pins: new Set(['pin']),
    actions: [act('wait:wait:1', 'waiting', 'session', { type: 'session', key: 'wait', label: 'wait', session: 'wait' })] }));
  assert.deepEqual(q.running.map((x) => x.session), ['pin', 'adv', 'old', 'new', 'start']);
  assert.equal(sessionOrder(list, new Set(['pin']), []).findIndex((s) => s.id === 'wait'), 3);
});

test('G9: Dismiss (button and e) only where it can act - the Feed, else GitLab (mark read); never a security item', () => {
  const it = (level, source) => ({ key: 'a:x', action: act('x', level, source, { type: 'session', key: 's', label: '' }) });
  assert.equal(canDismiss(it('gitlab', 'gitlab'), false), true);
  assert.equal(canDismiss(it('prod', 'prod'), false), false);
  assert.equal(canDismiss(it('ci', 'ci'), false), false);
  assert.equal(canDismiss(it('blocked', 'session'), false), false);
  for (const [l, src] of [['prod', 'prod'], ['ci', 'ci'], ['blocked', 'session'], ['gitlab', 'gitlab']]) assert.equal(canDismiss(it(l, src), true), true, l);
  assert.equal(canDismiss(it('security', 'session'), true), false);
  assert.equal(canDismiss(it('waiting', 'session'), true), false, 'a waiting row has Reply, not Dismiss');
  assert.equal(canDismiss({ key: 's:x' }, true), false);
});

test('N9 (B-N8-1): a start that failed after Allow is dismissable with the Feed off too (piperd\'s own row, not the Feed\'s)', async () => {
  const { readFileSync } = await import('node:fs');
  const af = { key: 'a:x', action: act('allowed-failed:propose_session:s1:1791581058', 'blocked', 'session', { type: 'session', key: 's1', label: 'demo' }) };
  assert.equal(canDismiss(af, false), true);
  assert.equal(canDismiss(af, true), true);
  assert.equal(canDismiss({ key: 'a:y', action: act('stuck:s1', 'blocked', 'session', { type: 'session', key: 's1', label: '' }) }, false), false,
    'any other blocked session row still needs the Feed');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /if \(state\.feed\?\.on \|\| allowedFailed\(a\)\) \{\s*void feedApi\.dismiss\(a\.key\)/, 'the queue\'s dismiss posts it without the Feed');
});

test('Ended sessions identify work and support project, task, branch, date and multiword search', () => {
  const now = new Date(2026, 9, 8, 12).getTime() / 1000;
  const yesterday = new Date(2026, 9, 7, 18, 23).getTime() / 1000;
  const q = buildQueue(input({now, sessions:[sess('pdf',{state:'exited',project:'acme/site',title:'acme/site',
    since:yesterday, branch:'1182-pdf',first_prompt:'NAME issue 1213 PDF',last_prompt:'Verify PDF generation'})]}));
  const item = q.ended[0];
  assert.equal(item.title, 'NAME issue 1213 PDF');
  assert.equal(item.sub, 'Verify PDF generation');
  assert.match(sessionDate(yesterday, now), /^Yesterday /);
  for (const query of ['site PDF','1213','1182-pdf','yesterday','2026-10-07']) assert.equal(matchesText(item, query), true, query);
  assert.equal(matchesText(item,'PDF wrong-project'), false);
});

test('N3: a confirmation row is red (no y) when feed.py marks it OR the floating card would be red', () => {
  const row = (args) => ({ level: 'confirm', actions: [{ op: 'decide', label: 'Allow', args: { id: 'p1', ok: true, ...args } },
    { op: 'decide', label: 'Deny', args: { id: 'p1', ok: false } }] });
  assert.equal(allowDanger(row({ danger: 'http_prod' })), 'Allow on PROD');
  assert.equal(allowDanger(row({ danger: 'gitlab_job_protected' })), 'Allow on protected branch');
  // feed.py did not mark it (a weakening settings change, a protected-branch job before the backend fix): the card's rule
  assert.equal(allowDanger(row({}), (id) => (id === 'p1' ? 'Allow weakening' : '')), 'Allow weakening');
  assert.equal(allowDanger(row({}), () => ''), '');
  assert.equal(allowDanger(row({})), '');
  assert.equal(allowDanger({ level: 'waiting', actions: [] }, () => 'x'), '');
});

test('N4: feed.py marks a weakening settings change (danger: settings_change) - the row says Allow weakening, not on PROD', () => {
  const row = (args) => ({ level: 'confirm', actions: [{ op: 'decide', label: 'Allow', args: { id: 'p1', ok: true, ...args } },
    { op: 'decide', label: 'Deny', args: { id: 'p1', ok: false } }] });
  assert.equal(allowDanger(row({ danger: 'settings_change' }), (id) => (id === 'p1' ? 'Allow weakening' : '')), 'Allow weakening');
  assert.equal(allowDanger(row({ danger: 'settings_change' })), 'Allow weakening');
  assert.equal(allowDanger(row({ danger: 'settings_change' }), () => ''), 'Allow weakening');
  assert.equal(allowDanger(row({ danger: 'something_new' })), 'Allow on PROD');   // still red, never green
});

test('N7: an empty Needs you / Running / Recently under a search says the search found nothing, not that nothing waits', () => {
  assert.equal(emptyLine('needs', true), 'Nothing matches.');
  assert.equal(emptyLine('running', true), 'Nothing matches.');
  assert.equal(emptyLine('needs', false), 'Nothing is waiting for you.');
  assert.equal(emptyLine('running', false), 'No running session.');
  assert.equal(emptyLine('recent', true), 'Nothing matches.');
  assert.equal(emptyLine('recent', false), 'Nothing has happened yet.');
});

test('N8: CSS - a tiny pill draws its ellipsis (no flex box), the header text chips give way before the isolation chip', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = (sel) => [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(sel)).map((m) => m[2]).join(';');
  // text-overflow: ellipsis is ignored on a flex container (.pill is inline-flex): the ⚠ chip of a card was cut mid-letter
  assert.match(rules('.pill.tiny'), /text-overflow:\s*ellipsis/);
  assert.match(rules('.pill.tiny'), /display:\s*inline-block/);
  // 1024 px + the queue: project + branch chips shrink (ellipsis, title), the isolation chip (a button) stays whole
  assert.match(rules('.dh-chips span.chip'), /flex:\s*0 1 auto/);
  assert.match(rules('.dh-chips .chip'), /flex:\s*none/);
  assert.match(rules('.chip'), /text-overflow:\s*ellipsis/);
  assert.doesNotMatch(rules('.chip'), /display:\s*(inline-)?flex/);
  // S2: the Commit header shows branch + sync open or folded; the branch ellipsizes (no cut glyph), the sync stays whole
  assert.equal(rules('.sec.git.open .sec-s'), '');
  assert.match(rules('.sec.git .sec-s .gbr'), /text-overflow:\s*ellipsis/);
  assert.match(rules('.sec.git .sec-s .gsync'), /flex:\s*none/);  // a full stack of floating cards scrolls; no card (the DB card has overflow: hidden) shrinks and cuts its buttons
  assert.match(rules('#pending'), /overflow-y:\s*auto/);
  assert.match(rules('#pending > .pending'), /flex:\s*none/);
  // N9: New session with the issue picker in a window under ~1000 px high - the chosen project's row was cut to a half row
  assert.match(rules('.modal:has(.startopts) > .plist'), /flex-shrink:\s*0/);
  // N9: the "piperd is not responding" banner pushes the app down instead of lying over the session header
  assert.doesNotMatch(rules('#banner'), /position:\s*fixed/);
  assert.match(rules('body:has(> #banner) > #app'), /flex:\s*1/);
  // N9: the session header keeps its height without the text buttons (an ended session): no 5 px jump
  assert.match(rules('.dh-top'), /min-height:\s*29px/);
  // N9: ≤1100 px the context panel shows while on (it was display: none - ⌘I did nothing); N10: as a column, not an overlay (test N10)
  assert.doesNotMatch(css, /max-width: 1100px\)[^@]*#inspector \{ display: none/);
  // the issue picker: labels beside a 200-character title keep their width (up to a third), never a 4 px sliver
  assert.match(rules('.isplit .pitem > .mut'), /flex-shrink:\s*0/);
  assert.match(rules('.isplit .pitem > .mut'), /max-width:\s*33%/);
  // a queue section label never wraps beside its hint ("Recently ·" / "21"); N9: the hint wraps (its unmute / restore links
  // were behind the …), the filter pills wrap (a hidden scroll cut them; W17: Resume… and ⌕ left the row, it fits one line at 360 px)
  assert.match(rules('.q-sechead .q-fold'), /white-space:\s*nowrap/);
  assert.match(rules('.q-sechead .q-fold'), /flex:\s*none/);
  assert.doesNotMatch(rules('.q-sechead .q-hint'), /text-overflow:\s*ellipsis|nowrap/);
  assert.match(rules('.q-sechead'), /flex-wrap:\s*wrap/);
});

test('N10: CSS - narrow windows: the isolation chip stays whole, the context panel and the agent drawer take a column, nothing lies over the detail', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = (sel) => [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(sel)).map((m) => m[2]).join(';');
  // CI-2: up to 1100 px the open context panel lay over the detail (fixed) and covered Send, Open in Files, the grid tiles' buttons:
  // it is a column at every width now (⌘I closes it)
  assert.doesNotMatch(css, /#inspector\s*\{[^}]*position:\s*fixed/);
  assert.doesNotMatch(css, /max-width: 1100px\)\s*\{\s*:root\s*\{\s*--ctx-w:\s*0/);
  // CI-3: the open agent drawer takes the context panel's column (it covered the tabs - Composer - and the whole panel);
  // also in a global view (Settings: no-inspector), and the header / Save bar no longer pad for a drawer lying over them
  assert.match(rules('body.drawer-open.no-inspector'), /--ctx-w:\s*var\(--drawer-w\)/);
  assert.match(rules('.drawer'), /width:\s*var\(--drawer-w\)/);
  assert.match(rules('body.drawer-open #pending'), /right:\s*calc\(var\(--drawer-w\) \+ 16px\)/);
  assert.match(rules('body.drawer-open #inspector'), /display:\s*none/);
  assert.equal(rules('body.drawer-open .head.detail'), '');
  assert.equal(rules('body.drawer-open:not(.pending-open) .settings .st-bar'), '');
  // CI-3: a tab row wider than the detail scrolls inside the header instead of running under whatever is to its right
  // (9.69 F03: the tabs moved into the one header bar; the old second row .dh-bot is gone)
  assert.match(rules('.head.detail.one .tabs'), /overflow-x:\s*auto/);
  assert.match(rules('.head.detail.one .tabs'), /min-width:\s*0/);
  assert.equal(rules('.dh-state'), '', 'no duplicate status summary');
  assert.doesNotMatch(css, /\.dh-bot\b/, 'no rules for the removed second header row');
  // N10 (with CI-3): Settings beside the drawer (590 px at 1024) kept the 900 / 700 px viewport layout - a 53 px value column (Environment
  // wrapped a path per 6 letters): the same layout switches follow the view's own width
  assert.match(rules('.settings'), /container:\s*st\s*\/\s*inline-size/);
  assert.match(css, /@container st \(max-width: 820px\)\s*\{\s*\.settings \.st-layout\s*\{\s*grid-template-columns:\s*1fr/);
  assert.match(css, /@container st \(max-width: 620px\)\s*\{\s*\.settings \.st-field\s*\{\s*grid-template-columns:\s*1fr/);
  // CI-1: a narrow header (1024 px + the drawer) drops the project / branch chips; the isolation chip was cut to "isc"
  assert.match(rules('.head.detail'), /container:\s*dh\s*\/\s*inline-size/);
  // N10 (with CI-2): Files beside the 1024 px column - the bottom panel's hint "⌘⌥↓ open" was cut (340/364): a narrow panel packs its tabs, drops the hint
  assert.match(rules('.fpanel'), /container:\s*fpanel\s*\/\s*inline-size/);
  assert.match(css, /@container fpanel \(max-width: 440px\)\s*\{\s*\.fpanel \.pstrip > \.mut\s*\{\s*display:\s*none/);
  assert.match(css, /@container fpanel \(max-width: 440px\)[^@]*\.fpanel \.pstrip\s*\{\s*flex-wrap:\s*wrap/);   // beside the drawer (243 px) Turns was cut
  // ...and a header narrower still (1024 px + the whole queue + the panel or the drawer: 336 / 239 px) wraps its actions under the title
  assert.match(css, /@container dh \(max-width: 440px\)\s*\{\s*\.dh-top, \.dh-top \.actions\s*\{\s*flex-wrap:\s*wrap/);
  assert.match(css, /@container dh \(max-width: 520px\)\s*\{\s*\.dh-chips span\.chip:not\(\.warn\)\s*\{\s*display:\s*none/);
});

test('N10-A-1: Running empty while every live session waits in Needs you says so, not "No running session."', () => {
  assert.equal(emptyLine('running', false), 'No running session.');
  assert.equal(emptyLine('running', false, 0), 'No running session.');
  assert.equal(emptyLine('running', false, 1), 'The running session waits for you above.');
  assert.equal(emptyLine('running', false, 3), 'All 3 running sessions wait for you above.');
  assert.equal(emptyLine('running', true, 3), 'Nothing matches.');
  assert.equal(emptyLine('needs', false, 3), 'Nothing is waiting for you.');
});

test('N10-A-2: "Beside the active session" / ⌘\\ only when both sessions have a terminal (a blocked active one made it a silent no-op)', async () => {
  const { splitBlock, hasPty } = await import('../../piper/web/dist/gridlayout.js');
  const s = (id, extra = {}) => ({ id, pid: 1, state: 'ready', ...extra });
  assert.equal(splitBlock(s('a'), s('b')), '');
  assert.equal(splitBlock(s('a', { state: 'blocked' }), s('b')), 'the active session has no terminal (blocked, starting or ended)');
  assert.equal(splitBlock(s('a', { pid: null, state: 'exited' }), s('b')), 'the active session has no terminal (blocked, starting or ended)');
  assert.equal(splitBlock(undefined, s('b')), 'the active session has no terminal (blocked, starting or ended)');
  assert.equal(splitBlock(s('a'), s('a')), 'this is the active session');
  assert.equal(splitBlock(s('a'), s('b', { state: 'preflight' })), 'this session has no terminal (blocked, starting or ended)');
  assert.equal(splitBlock(s('a'), undefined), 'this session has no terminal (blocked, starting or ended)');
  assert.equal(hasPty(s('a', { state: 'blocked' })), false);
  assert.equal(hasPty(s('a')), true);
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /label: 'Side by side', icon: 'fi-columns', run: \(\) => splitWith\(s\.id\), disabled: besideWhy \|\| false/, 'the menu entry is disabled with the reason (W11: its tooltip)');
  // W12: the reducer refuses with the reason (layout.test.mjs); the context asks gridlayout's splitBlock
  assert.match(src, /function splitWith\(id: string\): void \{\s*unseen\.delete\(id\);\s*dispatch\(\{ t: 'splitWith', id \}\);/, 'splitWith never sets a split nobody sees');
  assert.match(src, /: splitBlock\(cur, x\);/, '⌘\\ picks a partner that can show (an ended one too: layout.test.mjs)');
  const { layoutReduce, defaultLayout } = await import('../../piper/web/dist/layout.js');
  const c = { view: 'term', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: [], hasPty: () => true,
    splitBlock: (id) => splitBlock(s('a'), id === 'b' ? s('b', { state: 'blocked' }) : undefined), paneBlock: () => '', gridOn: true, feedOn: true, sessionView: true };
  assert.equal(layoutReduce(defaultLayout(true), { t: 'splitWith', id: 'b' }, c).fx.refuse, 'Not beside: this session has no terminal (blocked, starting or ended).');
});

test('N10-A-3: New session - a project filter that matches nothing says so (Start only toasted "Pick a project")', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /if \(projects\.length && !visible\(\)\.length\) list\.append\(h\('p', \{ class: 'mut' \}, markBidi\(`No project matches "\$\{filter\.value\}"\.`\)\)\)/);
});

test('N10-A-4 / A-6: CSS - the start dialog keeps its task box; a floating card beside the drawer stays in the drawer column when the detail is too narrow', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = (sel) => [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(sel)).map((m) => m[2]).join(';');
  // A-4: 1024 x 768 with the issue picker open: the task textarea shrank to 14 px (the dialog scrolls instead)
  assert.match(rules('.modal:has(.startopts) > textarea'), /flex-shrink:\s*0/);
  assert.match(rules('.modal:has(.startopts) > .issues'), /flex-shrink:\s*0/);
  // A-6: 1024 px + the whole queue + the drawer: the 460 px card lay over the queue (detail 271 px); below 1300 px it sits in the drawer's column
  assert.match(css, /@media \(max-width: 1299px\)\s*\{\s*body\.drawer-open:not\(\.queue-mini\):not\(\.no-queue\) #pending\s*\{\s*right:\s*16px;\s*bottom:\s*96px;\s*width:\s*calc\(var\(--drawer-w\) - 32px\);\s*max-height:\s*calc\(100vh - 112px\);/);
  assert.match(css, /@media \(max-width: 1000px\)\s*\{\s*body\.drawer-open #pending\s*\{\s*right:\s*16px;\s*bottom:\s*96px;\s*width:\s*calc\(var\(--drawer-w\) - 32px\);\s*max-height:\s*calc\(100vh - 112px\);/);
});

test('N10-A-7: "Choose for grid…" puts the keyboard on its first checkbox (it stayed on body)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /function gridChooser\(\): void \{[\s\S]*?modal\(`Panes in the grid[\s\S]*?list\.querySelector<HTMLInputElement>\('input:not\(\[disabled\]\)'\)\?\.focus\(\);/);
});

test('N10-A-8: Move up / down only where the order can change (a pinned neighbour, the top or the bottom made it a silent no-op)', async () => {
  const { moveTarget } = await import('../../piper/web/dist/queue.js');
  const list = [sess('p1'), sess('a'), sess('b'), sess('c')];
  const pins = new Set(['p1']);
  assert.equal(moveTarget(list, pins, 'a', -1), -1, 'not above a pinned one - sessionOrder puts pins first anyway');
  assert.equal(moveTarget(list, pins, 'p1', 1), -1, 'a pinned one does not sink under the unpinned');
  assert.equal(moveTarget(list, pins, 'p1', -1), -1, 'the top');
  assert.equal(moveTarget(list, pins, 'c', 1), -1, 'the bottom');
  assert.equal(moveTarget(list, pins, 'b', -1), 1);
  assert.equal(moveTarget(list, pins, 'a', 1), 2);
  assert.equal(moveTarget(list, pins, 'nope', 1), -1);
  assert.equal(moveTarget([sess('g', { kind: 'advisor' }), sess('a')], new Set(), 'a', -1), -1, 'the Global agent keeps its place too');
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  // W11: disabled with the reason as its tooltip, the shortcut on the right
  assert.match(src, /label: 'Move up', icon: 'fi-up', run: \(\) => moveInRail\(s\.id, -1\), keys: kbd\('⌘⇧↑'\),\s*disabled: moveTarget\(order, railPins, s\.id, -1\) < 0 && 'already at the top/);
  assert.match(src, /label: 'Move down', icon: 'fi-down', run: \(\) => moveInRail\(s\.id, 1\), keys: kbd\('⌘⇧↓'\),\s*disabled: moveTarget\(order, railPins, s\.id, 1\) < 0 && 'already at the bottom/);
});

test('N10-A-9: grid - an empty slot\'s "Add to grid" button with a long session name stays in the slot (1920: it ran 112 px off the window)', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = (sel) => [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(sel)).map((m) => m[2]).join(';');
  for (const re of [/max-width:\s*100%/, /overflow:\s*hidden/, /text-overflow:\s*ellipsis/, /white-space:\s*nowrap/]) assert.match(rules('.term.slot button'), re);
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  // F14: the names live in the "+ Add pane" popover - one line, cut with an ellipsis, the menu at most 420 px
  assert.match(src, /search: x\.kind === 'advisor' \? 'Global agent' : x\.title, run: \(\) => dispatch\(\{ t: 'add', id: x\.id \}\)/);
  for (const re of [/max-width:\s*min\(420px, 90vw\)/]) assert.match(rules('.cmenu.addpane'), re);
  for (const re of [/overflow:\s*hidden/, /text-overflow:\s*ellipsis/, /white-space:\s*nowrap/]) assert.match(rules('.cmenu.addpane button .grow'), re);
});

test('G-N11-1: a phone-width window (390 px) - the open context panel no longer leaves the detail 0 px with its toggle under the panel; the drawer ends at the queue strip', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const at = css.lastIndexOf('@media (max-width: 640px)', css.indexOf('#inspector { grid-column: 3'));   // N10 has its own later
  assert.ok(at > css.indexOf('body.queue-mini { --q-w: 0px; }'), 'the phone rules come after the folded queue\'s --q-w');
  const q = css.slice(at, css.indexOf('\n}', at));
  // no second column: the panel and the drawer give up theirs (every rule that sets --ctx-w on body)
  assert.match(q, /body, body\.no-inspector, body\.drawer-open, body\.drawer-open\.no-inspector \{ --ctx-w: 0px; \}/);
  // the panel lies in the detail's cell as a bottom sheet - the header (its toggle) stays above it; no fixed overlay (N10 CI-2)
  assert.match(q, /#inspector \{ grid-column: 3; align-self: end; height: 55vh; z-index: 14;/);
  assert.doesNotMatch(q, /position:\s*fixed/);
  // the drawer takes the detail's width, never the queue strip
  assert.match(q, /body \{ --drawer-w: max\(240px, calc\(100vw - var\(--ab-w\) - var\(--q-w\)\)\); \}/);
});

test('G-N11-4 / G-N11-5: the grid\'s header and tiles beside the agent drawer - the isolation chip stays, no action under the drawer, a tile keeps its name', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  // G-N11-4: 1024 px + drawer (header 591 px): the grid's six actions squeezed the chips to 0 px - the isolation chip was gone;
  // a header with five or more actions wraps them under the title below 700 px
  assert.match(css, /@container dh \(max-width: 700px\) \{ \.dh-top:has\(> \.actions > :nth-child\(5\)\), \.dh-top:has\(> \.actions > :nth-child\(5\)\) > \.actions \{ flex-wrap: wrap; \} \.dh-top:has\(> \.actions > :nth-child\(5\)\) > \.actions \{ flex: 0 1 auto; min-width: 0; \} \}/);
  // 768 px + drawer (header 384 px): the wrapped actions kept their one-line width (flex: none) - Stop, ⋯ and ◧ lay under the drawer
  assert.match(css, /@container dh \(max-width: 440px\) \{ \.dh-top \.actions \{ flex: 0 1 auto; min-width: 0; \} \}/);
  // G-N11-5: a 151 px tile: the dot and the name were 0 px (state + tools took the row) - the state words go (the dot says it)
  assert.match(css, /\.body\.grid \.term \{ container: gt \/ inline-size; \}/);
  assert.match(css, /\.tlabel \.dot \{ flex: none; \}/);
  assert.match(css, /@container gt \(max-width: 360px\) \{ \.tlabel > \.mut \{ display: none; \} \}/);
});

test('G-N11-6 / G-N11-7: leaving the grid for a global view leaves no splitter / Add cell over it; Grid from a global view shows the grid', async () => {
  const src = await mainSrc();
  // G-N11-6: renderMain returned early for a non-terminal view - the 14 px splitter (z-index 3) and the grid classes stayed
  // over Database ("Add"), Settings (the GitLab section) and Time
  assert.match(src, /bodyEl\.classList\.remove\('grid'\);\n    clearGridLayout\(\);\n    slotEl\.remove\(\);/);
  // G-N11-7: the rail's Grid is pressed only in the terminal view; from Database it turned the (unseen) grid off - a second click showed it
  // W12: the rule is the reducer's (layout.ts) - ⌘G off only where the grid shows
  const lay = await import('../../piper/web/dist/layout.js');
  const c = { view: 'db', active: 'a', lastActive: '', ids: ['a', 'b'], gridIds: ['a', 'b'], shown: [], hasPty: () => true, splitBlock: () => '',
    paneBlock: () => '', gridOn: true, feedOn: true, sessionView: false };
  const r = lay.layoutReduce({ ...lay.defaultLayout(true), center: { kind: 'grid' } }, { t: 'grid' }, c);
  assert.deepEqual([r.state.center.kind, r.fx.toTerm], ['grid', true], 'from Database: shown, not turned off');
  assert.equal(lay.layoutCommands({ ...lay.defaultLayout(true), center: { kind: 'grid' } }, 'db', 6, 0)[0].label, 'Grid: up to 6 panes at once');
});

test('N10-A-10: "Choose for grid…" keeps a long-named session\'s state ("ready" shrank to 0 px - the path rule of the project list)', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rules = (sel) => [...css.matchAll(/([^{}]+)\{([^}]*)\}/g)].filter((m) => m[1].split(',').map((s) => s.trim()).includes(sel)).map((m) => m[2]).join(';');
  assert.match(rules('.pitem:not(.col) > .name + .mut.gstate'), /flex:\s*none/);
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /h\('span', \{ class: path \? 'mut' : 'mut gstate', title: path \? note : undefined \}, note\)/);   // W26: the chooser's item(); a session's note = LABEL[x.state]
  assert.match(src, /item\(x\.id, x\.kind === 'advisor' \? 'Global agent' : x\.title, LABEL\[x\.state\] \?\? x\.state, false\)/);
});

// ---- N10 pass D (GUI): main.ts paths asserted on the source
const mainSrc = async () => (await import('node:fs')).readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');

test('N10-D-7: the morning-check session has no Git / Diagnostics / Stack in the context panel (git crashed piperd\'s handler, CI 404)', async () => {
  const src = await mainSrc();
  assert.match(src, /if \(s\.kind === 'project'\) \{\s*insp\.append\(git\.el\);\s*git\.show\(s\.id\);/);
  assert.doesNotMatch(src, /if \(s\.kind !== 'advisor'\) \{\s*insp\.append\(git\.el\)/);
});

test('N10-D-9: ⌘E closing the Composer gives the keyboard back to the terminal (it fell to <body>)', async () => {
  const src = await mainSrc();
  const fn = src.slice(src.indexOf('function toggleComposer('), src.indexOf('type UnlockKey'));
  assert.match(fn, /composer\.toggle\(on, \{ compact: false \}\);\s*if \(!composer\.open\(\)\) t\?\.focus\(\);/);
});

test('N10-D-11: Continue in the token dialog with an empty field asks again in place (it polled with no token: 3× 401, "Invalid token")', async () => {
  const src = await mainSrc();
  const fn = src.slice(src.indexOf('function askToken('), src.indexOf("window.addEventListener('storage'", src.indexOf('function askToken(')));
  assert.match(fn, /onclick: \(\) => \{\s*if \(!input\.value\.trim\(\)\) return input\.focus\(\);\s*setToken\(input\.value\);/);
});

test('N10-D-12: unsaved Settings + Composer / Split / Grid / Beside: "Stay" leaves nothing switched on (the Composer opened over Settings)', async () => {
  const src = await mainSrc();
  assert.match(src, /function toTerm\(then\?: \(\) => void\): boolean \{\s*if \(view !== 'term'\) setView\('term', then\);[^\n]*\n\s*return view === 'term';\s*\}/);
  const body = (name, end) => src.slice(src.indexOf(`function ${name}(`), src.indexOf(end, src.indexOf(`function ${name}(`)));
  assert.match(body('toggleComposer', 'type UnlockKey'), /if \(!toTerm\(\(\) => toggleComposer\(on\)\)\) return;/);
  // W12: Grid / Split / Beside go through dispatch - the terminal view first, the layout only when it got there
  assert.match(body('dispatch', 'function commitLayout'), /if \(first\.fx\.toTerm && view !== 'term'\) \{\s*if \(toTerm\(run\)\) run\(\);\s*return;\s*\}/);
  const lay = await import('../../piper/web/dist/layout.js');
  const c = { view: 'settings', active: 'a', lastActive: 'b', ids: ['a', 'b'], gridIds: [], hasPty: () => true, splitBlock: () => '',
    paneBlock: () => '', gridOn: true, feedOn: true, sessionView: false };
  for (const a of [{ t: 'grid' }, { t: 'split' }, { t: 'splitWith', id: 'b' }, { t: 'pane', pane: 'files' }, { t: 'single' }]) {
    assert.equal(lay.layoutReduce(lay.defaultLayout(true), a, c).fx.toTerm, true, a.t);
  }
  // G-N11-8: "Discard" went to the bare terminal - ⌘G / ⌘O / ⌘\\ / ⌘E had to be pressed again; it goes on with the asked layout
  assert.match(body('setView', 'function renderMain'), /settings\.discard\(\);\s*setView\(v\);\s*if \(view === v\) then\?\.\(\);/);
});

test('2026-10-10: a net-failed row restores the originals from Piper\'s trash (Restore / Restore all), dismissable without the Feed', async () => {
  const { readFileSync } = await import('node:fs');
  const subj = { type: 'session', key: 's1', label: 'demo' };
  const trash = [{ id: '20261010-162559-2bf54ed7', path: 'config/x.properties' }, { id: '20261010-162559-aaaaaaaa', path: 'config/‮evil.properties' }];
  const row = act('net-failed:s1:1791581058000', 'blocked', 'session', subj, { project: 'team-x/shop', trash });
  assert.deepEqual(netRestorable(row), trash);
  assert.deepEqual(netRestorable({ ...row, project: undefined }), [], 'no project: nothing to call');
  assert.deepEqual(netRestorable(act('allowed-failed:x:s1:1', 'blocked', 'session', subj, { project: 'p', trash })), [], 'only the net row');
  assert.deepEqual(netRestorable(act('net-failed:s1:2', 'blocked', 'session', subj)), [], 'nothing left to restore: no button');
  assert.equal(canDismiss({ key: 'a:n', action: row }, false), true, 'piperd\'s own row: Dismiss with the Feed off too');
  // the toast: what is back, what was refused with piperd's reason; a path's bidi control is shown, never applied
  assert.equal(restoreToast(['config/x.properties'], []), "Restored config/x.properties from Piper's trash");
  const t = restoreToast([], [{ path: trash[1].path, error: 'the isolation check of session "demo" has not finished' }]);
  assert.match(t, /^config\/\[U\+202E\]evil\.properties not restored: the isolation check/);
  assert.ok(!t.includes('‮'));
  assert.equal(restoreToast([], []), 'Nothing to restore');
  const q = readFileSync(new URL('../../piper/web/src/queue.ts', import.meta.url), 'utf8');
  assert.match(q, /netRestorable\(a\)/, 'the row renders the Restore buttons');
  assert.match(q, /'Restore all'/);
  assert.match(q, /'aria-label': showBidi\(`Restore \$\{t\.path\}`\)/, 'each button names its file (bidi shown)');
  assert.match(q, /x\.action\?\.trash\?\.map\(\(t\) => t\.id\)/, 'a restored entry re-renders the row');
  const m = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(m, /restoreTrash: async \(project, entries\) => \{[\s\S]{0,400}api\.fsRestore\(project, t\.id, true\)[\s\S]{0,300}toast\(restoreToast\(done, failed\)\)/,
    'main posts fs_restore with replace, one toast');
  const api = readFileSync(new URL('../../piper/web/src/api.ts', import.meta.url), 'utf8');
  assert.match(api, /replace \? \{ project, trash, replace \} : \{ project, trash \}/);
});

test('Linux Chromium layout (2026-10-10): wider fonts - the header actions wrap at 390 px, a section title ends with an ellipsis, the start progress lies over the terminal', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  // innermost blocks: the rules inside an @container / @media too
  const blocks = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({ sel: m[1].split(',').map((s) => s.trim()), body: m[2], at: m.index }));
  const rules = (sel) => blocks.filter((b) => b.sel.includes(sel)).map((b) => b.body).join(';');
  // the base `.dh-top .actions { flex: none }` came after the @container overrides (same specificity): it won, the four
  // actions never shrank / wrapped and ◧ ran off a 390 px window - every flex of that selector after the base one shrinks
  const acts = blocks.filter((b) => b.sel.includes('.dh-top .actions') && /\bflex:/.test(b.body));
  assert.match(acts[0].body, /flex:\s*none/, 'the base rule is the first one');
  assert.ok(acts.length > 1 && acts.slice(1).every((b) => /flex:\s*0 1 auto/.test(b.body)), 'the narrow-header overrides follow it');
  // "Changes" beside ↻ Review History Graph in the 262 px bottom sheet: the title was flex: none inside the clipping toggle
  for (const re of [/flex:\s*0 1 auto/, /min-width:\s*0/, /overflow:\s*hidden/, /text-overflow:\s*ellipsis/]) assert.match(rules('.sec-t'), re);
  assert.doesNotMatch(rules('.sec-t'), /flex:\s*none/);
  assert.match(rules('.sec-s'), /flex:\s*0 10000 auto/, 'the status gives way before the title');
  // xterm's canvases (z-index 1-11) covered the start progress' Cancel: the terminal is its own stacking context
  assert.match(rules('.term'), /isolation:\s*isolate/);
  assert.match(rules('.progress'), /z-index:\s*12\b/); // also above xterm inside a grid pane
  assert.match(rules('.panel'), /z-index:\s*1\b/);
});

test('session rows omit redundant Reply buttons and retain row opening and the r shortcut', () => {
  const source = readFileSync(new URL('../../piper/web/src/queue.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /'Reply r'/);
  assert.match(source, /onclick: \(\) => \{ freezeNow\(\); deps\.open\(it\); \}/);
  assert.match(source, /e\.key === 'r' && it\?\.session && deps\.canReply\(it\.session\)\) deps\.reply\(it\.session\)/);
});

test('decision buttons wrap whole in the narrow queue, never their words (9.50 fourth button)', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(css, /\.qi-ops \{[^}]*flex-wrap: wrap/);
  assert.match(css, /\.qi-ops button\.mini \{[^}]*white-space: nowrap/);
});

test('≤ 640 px the whole queue takes the detail\'s place and a pick folds it to the strip (detail was 46 px)', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const phone = css.slice(css.indexOf('@media (max-width: 640px)'));
  assert.match(phone, /body:not\(\.queue-mini\):not\(\.no-queue\) #queue \{ grid-column: 2 \/ 5; \}/);
  assert.match(phone, /body:not\(\.queue-mini\):not\(\.no-queue\) #main, body:not\(\.queue-mini\):not\(\.no-queue\) #inspector/);
  assert.match(css, /\.q-searchbox \{[^}]*min-width: 0/);   // search shrinks beside the new-session button
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /function openItem\(it: QItem\): void \{\n  foldNarrowQueue\(\);/);
  assert.match(main, /matchMedia\('\(max-width: 640px\)'\)\.matches && queueShown\(\) && !stripOn\(\)\) dispatch\(\{ t: 'narrow' \}\)/);
});

test('F17: a card that went unanswered is a line in Needs you for GONE_S - never counted, gone after it', async () => {
  const { GONE_S } = await import('../../piper/web/dist/queue.js');
  const now = 1000;
  const q = buildQueue(input({ now, gone: [{ id: 'p1', kind: 'git_push', session: 's1', title: 'grp/demo', at: now - 2 },
    { id: 'p0', kind: 'git_push', session: 's1', title: 'old', at: now - GONE_S - 1 }] }));
  assert.deepEqual(q.gone, [{ key: 'g:p1', title: 'grp/demo: wants to push', sub: 'cancelled - nothing was sent' }]);
  assert.equal(q.needs.length, 0, 'the badge and ⌘J never count it');
  assert.deepEqual(buildQueue(input({ now })).gone, [], 'an older piperd sends none');
  const src = readFileSync(new URL('../../piper/web/src/queue.ts', import.meta.url), 'utf8');
  assert.match(src, /goneTimer = window\.setTimeout\(render, 1000\)/, 'the line goes without a poll');
});
