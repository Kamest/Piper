import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { installTransport, notifyTransportReady, notifyTransportUnavailable, onTransportReady, reportTransportState, transportRevision } from '../../piper/web/dist/transport.js';

const main = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
const polling = main.slice(main.indexOf('let polledOnce = false;'), main.indexOf('/** a file only the session'));
const tick = () => new Promise(resolve => setImmediate(resolve));

function pollHarness(mobile = true) {
  const timers = new Map(), reports = [], banners = [];
  let revision = 1, ready, settle, calls = 0, serial = 0;
  const context = {
    Promise, Error, ApiError: class extends Error {}, POLL_MS: 1500, etag: 'old', remoteChanged() {},
    document: { hidden: false, getElementById() { return { remove() {} }; } },
    window: { setTimeout(fn, ms) { const id = ++serial; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } },
    api: { state: () => { calls++; return new Promise((resolve, reject) => { settle = { resolve, reject }; }); } },
    transportRevision: () => revision,
    reportTransportState(ok, owner) { if (mobile && revision === owner) reports.push(ok); return mobile; },
    onTransportReady(fn) { ready = fn; }, refreshRemoteHosts() {}, banner: text => banners.push(text),
  };
  vm.runInNewContext(polling, context);
  return { context, timers, reports, banners, settle: () => settle, calls: () => calls,
    ready() { revision++; ready(); }, retire() { revision++; } };
}

test('verified reconnect discards the cancelled old poll and immediately polls once, clearing mobile status', async () => {
  const h = pollHarness();
  const old = h.context.poll();
  h.retire(); h.ready(); h.ready();
  h.settle().reject(new Error('old connection reset'));
  await old; await tick();
  assert.equal(h.calls(), 2, 'ready event cluster must not overlap state requests');
  assert.deepEqual(h.banners, []);
  assert.deepEqual(h.reports, [], 'a stale rejection must not flag the new connection offline');
  h.settle().resolve({ data: null });
  await tick();
  assert.deepEqual(h.reports, [true]);
  assert.equal(h.timers.size, 1, 'exactly one polling loop survives reconnect');
});

test('a stale successful poll cannot dismiss a newer outage', async () => {
  const h = pollHarness(); const pending = h.context.poll();
  h.retire(); h.settle().resolve({ data: null }); await pending;
  assert.deepEqual(h.reports, []);
});

test('current mobile state failures use the shared status; desktop retains its original banner', async () => {
  for (const mobile of [true, false]) {
    const h = pollHarness(mobile), pending = h.context.poll();
    h.settle().reject(new Error('fixture timeout')); await pending;
    assert.deepEqual(h.reports, mobile ? [false] : []);
    assert.equal(h.banners.length, mobile ? 0 : 1);
    if (!mobile) assert.match(h.banners[0], /Local Piper is not responding: fixture timeout/);
  }
});

test('transport revision rejects results from both a retired and superseded connection', () => {
  const reports = [];
  installTransport({ stateChanged: value => reports.push(value) });
  const old = transportRevision();
  notifyTransportUnavailable();
  assert.equal(reportTransportState(true, old), true);
  const waiting = transportRevision();
  const unsubscribe = onTransportReady(() => {});
  notifyTransportReady();
  reportTransportState(false, waiting);
  assert.deepEqual(reports, []);
  reportTransportState(true, transportRevision());
  assert.deepEqual(reports, [true]);
  unsubscribe();
});
