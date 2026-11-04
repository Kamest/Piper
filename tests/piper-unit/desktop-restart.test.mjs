import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { service } = createRequire(import.meta.url)('../../desktop/src/lib/service.cjs');

function fixture({ replies = [], status = 202, slow = 0, uncertain = false, baseline = 'old' } = {}) {
  let elapsed = 0, posted = false, posts = 0, probes = 0;
  const changes = [];
  const s = service({ port: 18995, platform: 'linux', now: () => elapsed,
    locate: async () => '/example/piper', run: async () => ({ stdout: 'http://127.0.0.1:18995/#token=fake-only-token-1234567890' }),
    delay: async ms => { elapsed += ms; }, fetch: async (_url, init) => {
      if (init.method === 'POST') {
        posted = true; posts++;
        if (uncertain) throw Error('connection closed');
        return { ok: status === 202, status, json: async () => ({ error: 'private local detail' }) };
      }
      probes++;
      if (posted) elapsed += Math.min(slow, Math.max(0, 30000 - elapsed));
      const instance = posted ? (replies.length ? replies.shift() : 'old') : baseline;
      if (instance === null) throw Error('unavailable');
      return { ok: true, json: async () => ({ ok: true, version: 1, service_instance: instance }) };
    } });
  return { s, changes, changed: s => changes.push(s), elapsed: () => elapsed, posts: () => posts, probes: () => probes, replies };
}

test('restart reports immediately, observes outage and confirms a changed instance without reloading sessions', async () => {
  const f = fixture({ replies: [null, 'new'] });
  const promise = f.s.restart({ changed: f.changed });
  assert.equal(f.changes[0].phase, 'requesting');
  assert.equal((await promise).phase, 'recovered');
  assert.ok(f.changes.some(s => s.sawDown));
  assert.equal(f.posts(), 1);
  assert.equal(f.changes.at(-1).checking, false);
});
test('a fast exec restart is confirmed by identity even when no outage was sampled', async () => {
  const f = fixture({ replies: ['new'] });
  assert.equal((await f.s.restart()).phase, 'recovered');
});
test('an unchanged answering service times out; Check again never sends another restart', async () => {
  const f = fixture({ slow: 1500 });
  await assert.rejects(f.s.restart({ changed: f.changed }), /not confirmed/);
  assert.equal(f.elapsed(), 30000);
  assert.equal(f.s.restartStatus().phase, 'timeout');
  f.replies.push('new');
  assert.equal((await f.s.checkRestart()).phase, 'recovered');
  assert.equal(f.posts(), 1);
  assert.ok(f.probes() < 25);
});
test('unavailable service stops checks at the deadline and retains the last observation', async () => {
  const f = fixture({ replies: Array(60).fill(null), slow: 1500 });
  await assert.rejects(f.s.restart(), /did not return/);
  assert.equal(f.elapsed(), 30000);
  assert.equal(f.s.restartStatus().reachable, false);
  assert.equal(f.s.restartStatus().checking, false);
});
test('preflight rejection is explicit and diagnostic status excludes raw server details', async () => {
  const f = fixture({ status: 409 });
  await assert.rejects(f.s.restart({ changed: f.changed }));
  assert.equal(f.s.restartStatus().phase, 'rejected');
  assert.equal(f.s.restartStatus().reachable, true);
  assert.doesNotMatch(JSON.stringify(f.changes), /private local detail/);
  assert.equal(f.posts(), 1);
});
test('an unacknowledged request stays uncertain; retry checks availability only', async () => {
  const f = fixture({ uncertain: true });
  await assert.rejects(f.s.restart(), /acknowledgement/);
  assert.equal(f.s.restartStatus().phase, 'uncertain');
  f.replies.push('new');
  assert.equal((await f.s.checkRestart()).phase, 'recovered');
  assert.equal(f.posts(), 1);
});
test('an already-current update is reported without a restart POST', async () => {
  const f = fixture(); let calls = 0;
  const result = await f.s.restart({ action: async () => { calls++; return 'Piper is up to date'; } });
  assert.equal(result.phase, 'returned'); assert.equal(calls, 1); assert.equal(f.posts(), 0);
});
test('a refused CLI update retains the observed original service and excludes CLI error details', async () => {
  const f = fixture();
  const result = await f.s.restart({ action: async () => { throw Error('Piper was not updated: private local detail'); } });
  assert.equal(result.phase, 'rejected'); assert.equal(result.reachable, true);
  assert.doesNotMatch(JSON.stringify(result), /private local detail/); assert.equal(f.posts(), 0);
});
test('a server error is observed without assuming it rejected the restart or repeating it', async () => {
  const f = fixture({ status: 503, replies: ['new'] });
  assert.equal((await f.s.restart()).phase, 'recovered'); assert.equal(f.posts(), 1);
});
test('an initially unavailable service returns without claiming verified restart identity', async () => {
  const f = fixture({ baseline: null, replies: ['new'] });
  assert.equal((await f.s.restart()).phase, 'returned'); assert.equal(f.posts(), 1);
});

test('unexpected outage is observed without CLI commands or restart requests, including legacy service recovery', async () => {
  let elapsed = 0, up = false;
  const changes = [], requests = [];
  const s = service({ port: 18995, platform: 'linux', now: () => elapsed,
    locate: () => assert.fail('outage must not look up or start the CLI'),
    run: () => assert.fail('outage must never run a command'),
    delay: async ms => { elapsed += ms; up = true; },
    fetch: async (url, init) => {
      requests.push([url, init.method]);
      if (!up) throw Error('offline');
      return { ok: true, json: async () => ({ ok: true, version: 1 }) };
    } });
  const pending = s.observeOutage({ changed: state => changes.push(state) });
  assert.equal(changes[0].kind, 'outage');
  assert.equal(changes[0].checking, true);
  const result = await pending;
  assert.equal(result.phase, 'returned');
  assert.equal(result.checking, false);
  assert.equal(result.finding, 'Piper is answering again.');
  assert.equal(requests.length, 2);
  assert.ok(requests.every(([url, method]) => url.endsWith('/api/ping') && method === undefined));
});

test('cancelled outage cannot report a late failed check over a recovered workspace', async () => {
  let finish;
  const changes = [];
  const s = service({ port: 18995, fetch: () => new Promise(resolve => { finish = resolve; }) });
  const pending = s.observeOutage({ changed: state => changes.push(state) });
  const before = changes.length;
  s.stopOutage();
  finish({ ok: false, status: 503 });
  await pending;
  assert.equal(changes.length, before);
});

test('outage checks time out with a shared thirty-second budget and Check again stays read-only', async () => {
  let elapsed = 0, up = false, requests = 0;
  const s = service({ port: 18995, now: () => elapsed, delay: async ms => { elapsed += ms; },
    fetch: async (_url, init) => {
      assert.equal(init.method, undefined); requests++;
      if (!up) { elapsed += Math.min(1500, 30000 - elapsed); throw Error('offline'); }
      return { ok: true, json: async () => ({ ok: true, version: 1, service_instance: 'current' }) };
    } });
  assert.equal((await s.observeOutage()).phase, 'timeout');
  assert.equal(elapsed, 30000);
  assert.ok(requests <= 16);
  up = true;
  assert.equal((await s.checkRestart()).phase, 'returned');
});
