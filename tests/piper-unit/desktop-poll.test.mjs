import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
const { failureReason } = require('../../desktop/src/lib/updates.cjs');

const source = readFileSync(new URL('../../desktop/src/main.cjs', import.meta.url), 'utf8');
// The production poll() without native windows: About's "Piper service" row and the tray read `model` and serviceReason.
const pollSource = source.slice(source.indexOf('  async function poll()'), source.indexOf('  /** Show the main window on a session'));
function fixture({ state, trayFails = false, notifyFails = false } = {}) {
  const timers = [], events = [];
  const context = vm.createContext({ pollTimer: null, quitting: false, connected: true, restartState: null, holding: false, model: { online: true, poll: 5000 },
    serviceVersion: '', serviceReason: '', coreUpdate: null, notifyMemo: null, restartViews: new Set(), controller: { busy: false },
    failureReason, BrowserWindow: { getFocusedWindow: () => null },
    api: { state: async () => { if (state instanceof Error) throw state; return state; } },
    trayModel: (s, online = true) => ({ online, poll: online ? 5000 : 30000, sessions: s?.sessions?.length || 0 }),
    notifyPlan: () => { if (notifyFails) throw TypeError('private title'); return { memo: {}, notes: [] }; },
    refreshTray: () => { events.push('tray'); if (trayFails) throw TypeError('menu'); },
    sendAbout: () => events.push('about'), offline: async () => events.push('offline'), finishOutage: async () => {},
    post() {}, openSession() {}, answers() {}, clearTimeout() {}, setTimeout: (fn, ms) => { timers.push(ms); return timers.length; } });
  vm.runInContext(pollSource + '\nthis.poll = poll;', context);
  return { context, timers, events };
}

test('a fault in the tray keeps polling and never reports a running service as unreachable', async () => {
  const f = fixture({ state: { sessions: [{ id: 'a' }], service_version: '0.1.39' }, trayFails: true });
  await f.context.poll();
  assert.equal(f.context.model.online, true); assert.equal(f.context.serviceVersion, '0.1.39');
  assert.deepEqual(f.timers, [5000]); assert.deepEqual(f.events, ['tray', 'about']);
  assert.equal(f.context.serviceReason, 'tray: TypeError');
});
test('a fault in handling the state (notifications) keeps the service online and the next poll scheduled', async () => {
  const f = fixture({ state: { sessions: [] }, notifyFails: true });
  await f.context.poll();
  assert.equal(f.context.model.online, true); assert.deepEqual(f.timers, [5000]);
  assert.equal(f.context.serviceReason, 'state handling: TypeError'); assert.ok(!f.events.includes('offline'));
});
test('an unreachable service is reported with a safe reason, and a tray fault offline still schedules the next poll', async () => {
  const f = fixture({ state: Object.assign(Error('private 127.0.0.1 token'), { name: 'TimeoutError' }), trayFails: true });
  await f.context.poll();
  assert.equal(f.context.model.online, false); assert.equal(f.context.serviceReason, 'TimeoutError');
  assert.deepEqual(f.timers, [30000]); assert.deepEqual(f.events, ['offline', 'tray', 'about']);
});
test('a recovered service clears the reason', async () => {
  const f = fixture({ state: { sessions: [] } }); f.context.serviceReason = 'TimeoutError';
  await f.context.poll(); assert.equal(f.context.serviceReason, ''); assert.equal(f.context.model.online, true);
});
