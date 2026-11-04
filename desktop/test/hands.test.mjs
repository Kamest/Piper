// 9.173 the desktop app's hands: cards answered outside the window, the Machine menu, more windows.
import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { cardChoices, replyTarget } = require('../src/lib/cards.cjs');
const { trayModel } = require('../src/lib/policy.cjs');
const { service } = require('../src/lib/service.cjs');
const { machine, runtimeStarters } = require('../src/lib/machine.cjs');
const { windowURL, freeSlot, placement, restorable } = require('../src/lib/windows.cjs');

// the queue action piperd's feed.py words for a pending card (key p:<id>, decide ops)
const card = (id, allowArgs = {}, more = []) => ({ key: 'p:' + id, level: 'confirm', title: 'Build: wants to push', subject: { type: 'session', key: 's1' },
  actions: [{ op: 'decide', label: 'Allow', args: { id, ok: true, ...allowArgs } }, ...more, { op: 'decide', label: 'Deny', args: { id, ok: false } }] });

test('a plain card offers Allow and Deny; a red, a prompt or an always-only card never offers Allow', () => {
  assert.deepEqual(cardChoices(card(7)), { id: '7', allow: true, title: 'Build: wants to push', session: 's1' });
  assert.equal(cardChoices(card(7, { danger: 'db_execute' })).allow, false);
  assert.equal(cardChoices(card(7, { prompt: true })).allow, false);
  // the "always" buttons are never what the tray presses
  const always = card(7, {}, [{ op: 'decide', label: 'Always', args: { id: 7, ok: true, always: true } }]);
  assert.equal(cardChoices(always).allow, true);
  const onlyAlways = { ...card(7), actions: [{ op: 'decide', label: 'Always', args: { id: 7, ok: true, always: true } }, { op: 'decide', label: 'Deny', args: { id: 7, ok: false } }] };
  assert.equal(cardChoices(onlyAlways).allow, false);
  const sessionAlways = { ...card(7), actions: [{ op: 'decide', label: 'For this session', args: { id: 7, ok: true, session_always: true } }, { op: 'decide', label: 'Deny', args: { id: 7, ok: false } }] };
  assert.equal(cardChoices(sessionAlways).allow, false);
});

test('only a confirm card with a matching key and id can be answered outside the window', () => {
  assert.equal(cardChoices({ ...card(7), level: 'permission' }), null);
  assert.equal(cardChoices({ ...card(7), key: 'p:8' }), null);
  assert.equal(cardChoices({ ...card(7), key: 'perm:7' }), null);
  assert.equal(cardChoices({ key: 'p:7', level: 'confirm', title: 'x' }), null);   // an older piperd: no decide ops
  assert.equal(cardChoices({ ...card('../x'), key: 'p:../x' }), null);
  assert.equal(cardChoices(null), null);
  // an Allow op for another id is not this card's Allow
  const other = { ...card(7), actions: [{ op: 'decide', args: { id: 9, ok: true } }, { op: 'decide', args: { id: 7, ok: false } }] };
  assert.equal(cardChoices(other).allow, false);
  assert.equal(cardChoices({ ...card(7), title: 'x'.repeat(500) }).title.length, 140);
});

test('a waiting session row has a Reply target, other rows none', () => {
  assert.equal(replyTarget({ actions: [{ op: 'reply', args: { session: 'abc123' } }] }), 'abc123');
  assert.equal(replyTarget({ actions: [{ op: 'open', args: { session: 'abc123' } }] }), '');
  assert.equal(replyTarget({ actions: [{ op: 'reply', args: { session: 'a b' } }] }), '');
  assert.equal(replyTarget(null), '');
});

test('the tray model lists its rows with their card and the sessions a new window can show', () => {
  const m = trayModel({ sessions: [{ id: 's1', title: 'Build', state: 'waiting' }, { id: 's2', project: 'acme/docs', state: 'ready' }, { id: 's3', hidden: true }],
    queue: { actions: [card(7), { level: 'permission', title: 'Docs asks', subject: { type: 'session', key: 's2' } }, { level: 'waiting', title: 'Build is waiting' }] } });
  assert.deepEqual(m.entries, [{ label: 'Build: wants to push', card: { id: '7', allow: true, title: 'Build: wants to push', session: 's1' }, session: 's1' },
    { label: 'Docs asks', card: null, session: 's2' }]);
  assert.deepEqual(m.list, [{ id: 's1', title: 'Build', state: 'waiting' }, { id: 's2', title: 'acme/docs', state: 'ready' }]);
  assert.deepEqual(m.items, ['Build: wants to push', 'Docs asks']);   // older consumers keep their labels
  // an old piperd: a session row opens that session
  assert.equal(trayModel({ sessions: [{ id: 's9', state: 'attention' }] }).entries[0].session, 's9');
  assert.deepEqual(trayModel(null, false).list, []);
});

function api(answers = {}) {
  const requests = [];
  const s = service({ port: 18995, platform: 'linux', locate: async () => '/test/piper', run: async () => ({ code: 0, stdout: 'http://127.0.0.1:18995/#token=fake-only-token-1234567890' }),
    fetch: async (full, opts) => {
      const route = new URL(full).pathname;
      requests.push([opts.method, route, opts.body ? JSON.parse(opts.body) : null, opts.headers.Authorization]);
      const status = answers[route] ?? 200;
      return { ok: status < 300, status, json: async () => (status < 300 ? { ok: true } : { error: 'card is gone' }) };
    } });
  return { s, requests };
}

test('decide posts only approve to the card, send types the reply; both refuse what is not theirs', async () => {
  const { s, requests } = api();
  await s.decide('7', true); await s.decide('7', false);
  await s.send('s1', 'yes, push it');
  assert.deepEqual(requests, [['POST', '/api/pending/7', { approve: true }, 'Bearer fake-only-token-1234567890'],
    ['POST', '/api/pending/7', { approve: false }, 'Bearer fake-only-token-1234567890'],
    ['POST', '/api/sessions/s1/send', { text: 'yes, push it' }, 'Bearer fake-only-token-1234567890']]);
  for (const [id, ok] of [['../x', true], ['7', 'yes'], ['', true]]) await assert.rejects(s.decide(id, ok), /Invalid card/);
  for (const [id, text] of [['s/1', 'x'], ['s1', '  '], ['s1', 'x'.repeat(20001)], ['s1', 7]]) await assert.rejects(s.send(id, text), /Invalid reply/);
  assert.equal(requests.length, 3);
  const gone = api({ '/api/pending/7': 404 });
  await assert.rejects(gone.s.decide('7', true), /card is gone/);
});

test('runtime starters: Docker Desktop first, never sudo or a system dockerd', () => {
  for (const p of ['darwin', 'linux', 'win32']) {
    const all = runtimeStarters(p).flat(2).join(' ');
    assert.doesNotMatch(all, /sudo|systemctl start docker\b|dockerd|service docker/);
    assert.deepEqual(runtimeStarters(p)[0][1], ['desktop', 'start']);
  }
  assert.ok(runtimeStarters('darwin').some(([c, a]) => c === 'open' && a.join(' ') === '-a Docker'));
});

function machineWith({ up = [], starts = {}, cli = {} } = {}) {
  const calls = []; let clock = 0; const answers = [...up];
  const ops = machine({ platform: 'darwin', delay: async ms => { clock += ms; }, now: () => clock,
    run: async (cmd, args) => { calls.push([cmd, ...args]);
      if (args[0] === 'info') return { code: answers.length ? (answers.shift() === cmd ? 0 : 1) : 1, stdout: '' };
      if (cmd + ' ' + args.join(' ') in starts) { const c = starts[cmd + ' ' + args.join(' ')]; if (c instanceof Error) throw c; return { code: c, stdout: '' }; }
      return { code: 1, stdout: '' }; },
    command: async (args, opts) => { calls.push(['piper', ...args]); const r = cli[args[0]] || { code: 0, stdout: '' };
      for (const l of r.lines || []) opts?.onLine?.(l); return r; } });
  return { ops, calls, clock: () => clock };
}

test('Start Docker: nothing when one answers, else the first starter that works, then it waits until it answers', async () => {
  let m = machineWith({ up: ['docker'] });
  assert.equal(await m.ops.startRuntime(), 'running');
  assert.deepEqual(m.calls, [['docker', 'info']]);
  // docker and podman down; `docker desktop start` is missing (older Docker), `open -a Docker` works, up after 2 checks
  m = machineWith({ up: ['x', 'x', 'x', 'x', 'x', 'x', 'docker'], starts: { 'docker desktop start': new Error('not found'), 'open -a Docker': 0 } });
  assert.equal(await m.ops.startRuntime(), 'started');
  assert.deepEqual(m.calls.slice(2, 4), [['docker', 'desktop', 'start'], ['open', '-a', 'Docker']]);
  assert.ok(!m.calls.some(c => c.join(' ') === 'open -a OrbStack'));
  m = machineWith();
  await assert.rejects(m.ops.startRuntime(), /Start your container runtime yourself/);
  m = machineWith({ starts: { 'docker desktop start': 0 } });
  await assert.rejects(m.ops.startRuntime({ wait: 9000 }), /does not answer yet/);
  assert.ok(m.clock() >= 9000);
});

test('Update Piper runs `piper update` and reports its line or its refusal', async () => {
  let m = machineWith({ cli: { update: { code: 0, stdout: 'Piper updated abc1234 -> def5678\n' } } });
  assert.equal(await m.ops.update(), 'Piper updated abc1234 -> def5678');
  m = machineWith({ cli: { update: { code: 0, stdout: 'Piper is up to date (abc1234)\n' } } });
  assert.match(await m.ops.update(), /up to date/);
  m = machineWith({ cli: { update: { code: 1, stdout: '', lines: ['piper: git pull --ff-only failed in /p (local changes or another branch?) - nothing was changed; update it yourself'] } } });
  await assert.rejects(m.ops.update(), /Piper was not updated: git pull --ff-only failed .* nothing was changed/);
  m = machineWith({ cli: { update: { code: 2, stdout: 'piper.sh - run Piper, the AI agent orchestrator. Opt-in:\n', lines: ['piper.sh - run Piper, the AI agent orchestrator. Opt-in:'] } } });
  await assert.rejects(m.ops.update(), e => /has no `piper update` yet/.test(e.message) && /git pull/.test(e.message) && !/see why/.test(e.message));
});

test('diagnostics name versions, service, runtime, status and doctor - never a token', async () => {
  const m = machineWith({ up: ['docker'], cli: {
    status: { code: 0, stdout: 'piperd answers on http://127.0.0.1:17895 - service piperd installed (launchd)\n' },
    doctor: { code: 1, stdout: 'ok   hole 2.1\nFAIL docker: password=hunter2 token=abc http://127.0.0.1:17895/#token=secret-token-123456\n' } } });
  const text = await m.ops.diagnostics({ app: '0.1.6', os: 'darwin', arch: 'arm64', online: true });
  assert.match(text, /^Piper desktop 0\.1\.6 on darwin arm64\nService: answering\nContainer runtime: docker\n/);
  assert.match(text, /\$ piper status \(exit 0\)\npiperd answers on/);
  assert.match(text, /\$ piper doctor \(exit 1\)\nok {3}hole 2\.1/);
  for (const secret of ['hunter2', 'secret-token-123456', 'token=abc']) assert.ok(!text.includes(secret), secret);
  const broken = machine({ platform: 'linux', run: async () => { throw Error('no docker'); }, command: async () => { throw Error('Piper command was not found'); } });
  assert.match(await broken.diagnostics({ app: '1', os: 'linux', arch: 'x64', online: false }), /Service: not answering\nContainer runtime: not running[\s\S]*Piper command was not found/);
});

test('window URLs keep the token fragment and add ?window=N; slots are reused from 2', () => {
  const url = 'http://127.0.0.1:18995/#token=fake-only-token-1234567890';
  assert.equal(windowURL(url, 2), 'http://127.0.0.1:18995/?window=2#token=fake-only-token-1234567890');
  assert.equal(windowURL(url, 1), url);
  assert.equal(windowURL('http://127.0.0.1:18995/?window=4#token=x', 1), 'http://127.0.0.1:18995/#token=x');
  assert.equal(freeSlot([]), 2);
  assert.equal(freeSlot([2, 3, 5]), 4);
  assert.equal(freeSlot(Array.from({ length: 98 }, (_, i) => i + 2)), 0);
});

test('a window keeps its place while a display shows it, else opens on the primary one', () => {
  const primary = { workArea: { x: 0, y: 0, width: 1440, height: 900 } }, second = { workArea: { x: 1440, y: 0, width: 1920, height: 1080 } };
  const there = { x: 1600, y: 100, width: 1200, height: 800 };
  assert.deepEqual(placement(there, [primary, second]), there);
  assert.deepEqual(placement(there, [primary]), { width: 1200, height: 800 });   // the second monitor was unplugged
  assert.deepEqual(placement({ width: 300, height: 200 }, [primary]), { width: 1040, height: 760 });
  assert.deepEqual(placement(null, [primary], { x: 10, y: 20, width: 1000, height: 700 }), { x: 42, y: 52, width: 1000, height: 700 });
  assert.deepEqual(placement(undefined, [primary]), { width: 1040, height: 760 });
});

test('windows.json is read defensively: distinct slots 2..99, at most 20', () => {
  assert.deepEqual(restorable([{ slot: 2, bounds: { x: 1 } }, { slot: 2 }, { slot: 1 }, { slot: 100 }, { slot: '3' }, null, { slot: 3, bounds: 'x' }]),
    [{ slot: 2, bounds: { x: 1 } }, { slot: 3, bounds: {} }]);
  assert.equal(restorable(Array.from({ length: 40 }, (_, i) => ({ slot: i + 2 }))).length, 20);
  for (const bad of [null, {}, 'x', 7]) assert.deepEqual(restorable(bad), []);
});
