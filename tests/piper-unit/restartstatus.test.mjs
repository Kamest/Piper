import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { RestartCheck, restartDiagnostics } from '../../piper/web/dist/restartstatus.js';
import { ApiError } from '../../piper/web/dist/api.js';

function fixture({ after = [], rejection = 0, uncertain = false, slow = 0, baseline = 'old', route = '/api/restart' } = {}) {
  let clock = 0, posts = 0;
  const changes = [];
  const request = async (method, path, _body, _etag, timeout) => {
    assert.ok(timeout <= 1500, 'every request is bounded');
    if (method === 'POST') {
      assert.equal(path, route); posts++;
      if (rejection) throw new ApiError(rejection, 'secret private server body');
      if (uncertain) throw Error('connection closed');
      return { data: { ok: true } };
    }
    assert.equal(path, '/api/ping');
    const id = posts ? (after.length ? after.shift() : 'old') : baseline;
    if (posts) clock += Math.min(slow, timeout);
    if (id === null) throw Error('offline');
    return { data: { ok: true, version: 1, service_instance: id } };
  };
  const c = new RestartCheck(s => changes.push(s), request, () => clock, async ms => { clock += ms; });
  return { c, changes, after, posts: () => posts, clock: () => clock };
}
test('web restart appears before any response and detects a new instance without sampling downtime', async () => {
  const f = fixture({ after: ['new'] });
  const p = f.c.start();
  assert.equal(f.changes[0].phase, 'requesting');
  await p;
  assert.equal(f.c.status.phase, 'recovered');
  assert.equal(f.posts(), 1);
});
test('unavailable then recovered; checks and timestamps are retained', async () => {
  const f = fixture({ after: [null, 'new'] }); await f.c.start();
  assert.ok(f.changes.some(s => !s.reachable && s.checks));
  assert.equal(f.c.status.checks, 3);
  assert.ok(f.c.status.checkedAt);
  assert.equal(f.c.status.phase, 'recovered');
});
test('slow unchanged probes share one thirty-second budget; retry never restarts', async () => {
  const f = fixture({ slow: 1500 }); await f.c.start();
  assert.equal(f.clock(), 30000);
  assert.equal(f.c.status.phase, 'timeout');
  assert.equal(f.c.status.checking, false);
  f.after.push('new'); await f.c.check();
  assert.equal(f.c.status.phase, 'recovered');
  assert.equal(f.posts(), 1);
});
test('refused update retains the healthy service and only safe diagnostic observations', async () => {
  const f = fixture({ rejection: 409 }); await f.c.start();
  assert.equal(f.c.status.phase, 'rejected');
  assert.equal(f.c.status.reachable, true);
  assert.match(f.c.status.finding, /rejected before restart/);
  assert.doesNotMatch(restartDiagnostics(f.c.status), /secret|private|Bearer|token/);
  await f.c.check(); assert.equal(f.c.status.phase, 'rejected');
  assert.equal(f.posts(), 1);
});
test('lost acknowledgement is explicit; check detects a later recovery without another POST', async () => {
  const f = fixture({ uncertain: true }); await f.c.start();
  assert.equal(f.c.status.phase, 'uncertain');
  f.after.push('new'); await f.c.check();
  assert.equal(f.c.status.phase, 'recovered');
  assert.equal(f.posts(), 1);
});
test('a second click while the request is pending cannot send a second restart', async () => {
  const f = fixture({ after: ['new'] });
  await Promise.all([f.c.start(), f.c.start()]); assert.equal(f.posts(), 1);
});
test('a server error is an uncertain outcome; bounded checks can still confirm recovery', async () => {
  const f = fixture({ rejection: 503, after: ['new'] }); await f.c.start();
  assert.equal(f.c.status.phase, 'recovered'); assert.equal(f.posts(), 1);
});
test('an initially unavailable service can return on the first probe without claiming a verified restart', async () => {
  const f = fixture({ baseline: null, after: ['new'] }); await f.c.start();
  assert.equal(f.c.status.phase, 'returned'); assert.equal(f.posts(), 1);
});
test('service reinstall uses the same diagnostics and read-only retry', async () => {
  const f = fixture({ route: '/api/service/reinstall', after: ['new', 'new'] });
  await f.c.start('/api/service/reinstall'); await f.c.check();
  assert.equal(f.c.status.phase, 'recovered'); assert.equal(f.posts(), 1);
});
