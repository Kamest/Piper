import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../piper/web/dist/mobilepush.js', import.meta.url), 'utf8').replace(/^export /gm, '');
function fixture({ ios = false, standalone = true, permission = 'default', supported = true, storage = new Map() } = {}) {
  const calls = []; let sub = null, enabled = false, showTitles = false, subscriptionId = '';
  const publicKey = Buffer.from([4, ...Array(64).fill(1)]).toString('base64url');
  const manager = { getSubscription: async () => sub, async subscribe(options) {
    calls.push(['subscribe', options]);
    sub = { options, async unsubscribe() { calls.push(['unsubscribe']); sub = null; return true; }, toJSON: () => ({ endpoint: 'https://web.push.apple.com/fixture' }) };
    return sub;
  } };
  const registration = supported ? { pushManager: manager } : {};
  const context = {
    navigator: { userAgent: ios ? 'iPhone' : 'Chrome', platform: ios ? 'iPhone' : 'Linux', maxTouchPoints: 1,
      serviceWorker: { register: async (...args) => { calls.push(['register', ...args]); return registration; }, getRegistration: async () => registration, ready: Promise.resolve(registration) } },
    matchMedia: () => ({ matches: standalone }),
    localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v) },
    document: { dispatchEvent() {} }, CustomEvent: class { constructor(type) { this.type = type; } },
    Notification: { permission, async requestPermission() { calls.push(['permission']); this.permission = 'granted'; return 'granted'; } },
    window: { Notification: {}, ...(supported ? { PushManager: {} } : {}), setTimeout, clearTimeout }, atob, Uint8Array,
  };
  vm.runInNewContext(source, context);
  context.configurePush(async value => {
    calls.push(['rpc', value.action]);
    if (value.action === 'subscribe') { enabled = true; subscriptionId = 'fixture-subscription'; }
    if (value.action === 'unsubscribe') enabled = false;
    if (value.action === 'preferences') showTitles = value.showTitles;
    return { available: true, enabled, publicKey, sent: true, showTitles, subscriptionId: enabled ? subscriptionId : '' };
  }, 'Studio Mac');
  return { context, calls, registration, get sub() { return sub; } };
}

test('iPhone Safari asks for Home Screen before unsupported and never prompts on status/load', async () => {
  const f = fixture({ ios: true, standalone: false, supported: false });
  assert.equal(f.context.pushSupport(), 'install');
  assert.equal((await f.context.pushStatus()).state, 'install');
  assert.deepEqual(f.calls, []);
  await assert.rejects(f.context.enablePush(), /unavailable/);
  assert.deepEqual(f.calls, []);
});

test('explicit enable requests permission before its first network await and registers a push-only worker', async () => {
  const f = fixture();
  assert.equal((await f.context.pushStatus()).state, 'off'); f.calls.length = 0;
  const enabling = f.context.enablePush();
  assert.deepEqual(f.calls, [['permission']]);
  await enabling;
  assert.equal((await f.context.pushStatus()).state, 'on');
  assert.equal(f.calls.find(c => c[0] === 'register')[1], '/service-worker.js');
  assert.equal(f.calls.find(c => c[0] === 'subscribe')[1].userVisibleOnly, true);
  assert.equal(f.calls.find(c => c[0] === 'subscribe')[1].applicationServerKey.byteLength, 65);
  assert.equal(f.context.pushComputer(), 'Studio Mac');
});

test('denied and unsupported do not create a subscription or request permission', async () => {
  for (const options of [{ permission: 'denied' }, { supported: false }]) {
    const f = fixture(options);
    assert.ok(['blocked', 'unsupported'].includes((await f.context.pushStatus()).state));
    await assert.rejects(f.context.enablePush());
    assert.deepEqual(f.calls, []);
  }
});

test('turn off also removes the browser subscription when the computer is unavailable', async () => {
  const f = fixture(); await f.context.enablePush();
  f.context.configurePush(async () => { throw Error('offline'); });
  await assert.rejects(f.context.disablePush(), /offline/);
  assert.equal(f.sub, null);
});

test('delayed turn off targets its observed identity and never unsubscribes a newer local object', async () => {
  const f = fixture(); await f.context.enablePush();
  const old = f.sub;
  let release, request;
  f.context.configurePush(value => { request = value; return new Promise(resolve => { release = resolve; }); });
  const disabling = f.context.disablePush();
  while (!release) await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(request.action, 'unsubscribe');
  assert.equal(request.subscriptionId, 'fixture-subscription');
  let oldStopped = 0, newStopped = 0;
  old.unsubscribe = async () => { oldStopped++; return true; };
  const newer = { unsubscribe: async () => { newStopped++; return true; } };
  f.registration.pushManager.getSubscription = async () => newer;
  release({ available: true, enabled: false, publicKey: '', subscriptionId: '' });
  await disabling;
  assert.equal(oldStopped, 1);
  assert.equal(newStopped, 0);
});

test('server detects stale turn off and preserves the newly enabled browser subscription', async () => {
  const f = fixture(); await f.context.enablePush();
  let stopped = 0;
  f.sub.unsubscribe = async () => { stopped++; return true; };
  f.context.configurePush(async () => ({ available: true, enabled: true, publicKey: '', subscriptionId: 'newer' }));
  await assert.rejects(f.context.disablePush(), /changed/);
  assert.equal(stopped, 0);
});

test('revoked/forgotten client can unsubscribe locally without a live transport', async () => {
  const f = fixture(); await f.context.enablePush();
  await f.context.stopLocalPush(); assert.equal(f.sub, null);
  assert.equal(f.calls.filter(c => c[0] === 'unsubscribe').length, 1);
});

test('test push is explicit and accepted does not claim confirmed delivery', async () => {
  const f = fixture(); await f.context.enablePush(); f.calls.length = 0;
  await f.context.testPush(); assert.deepEqual(f.calls, [['rpc', 'test']]);
  f.context.configurePush(async () => ({ sent: false }));
  await assert.rejects(f.context.testPush(), /couldn’t be sent/);
});

test('first-open offer is optional once per installation, survives reload and never prompts permission itself', async () => {
  const storage = new Map(), f = fixture({ storage });
  assert.equal(await f.context.pushOffer(), 'off');
  assert.ok(!f.calls.some(c => c[0] === 'permission'));
  f.context.completePushOffer();
  assert.equal(await f.context.pushOffer(), null);
  assert.equal(await fixture({ storage }).context.pushOffer(), null);
  assert.equal(await fixture({ ios: true, standalone: false, supported: false }).context.pushOffer(), 'install');
  for (const options of [{ permission: 'denied' }, { supported: false }]) {
    assert.equal(await fixture(options).context.pushOffer(), null);
  }
});

test('settings enable/disable handle the offer and session names are an explicit per-phone choice', async () => {
  const f = fixture();
  await f.context.enablePush();
  assert.equal(await f.context.pushOffer(), null);
  assert.equal((await f.context.pushStatus()).showTitles, false);
  await f.context.setPushTitles(true);
  assert.equal((await f.context.pushStatus()).showTitles, true);
  await f.context.setPushTitles(false);
  assert.equal((await f.context.pushStatus()).showTitles, false);
  await f.context.disablePush();
  assert.equal(await f.context.pushOffer(), null);
});

const sw = readFileSync(new URL('../../piper/web/mobile-sw.js', import.meta.url), 'utf8');
function worker() {
  const listeners = {}, notices = [], operations = [];
  const client = { url: 'https://piper.example/session', navigate: async url => operations.push(['navigate', url]), focus: async () => operations.push(['focus']) };
  const context = { URL, self: { location: { origin: 'https://piper.example' },
    addEventListener: (name, listener) => { listeners[name] = listener; }, skipWaiting: async () => {},
    registration: { showNotification: async (...args) => notices.push(args) },
    clients: { claim: async () => {}, matchAll: async () => [client], openWindow: async url => operations.push(['open', url]) } } };
  vm.runInNewContext(sw, context);
  return { context, listeners, notices, operations };
}

test('worker shows only fixed generic text even for malicious/private payloads and has no fetch cache', async () => {
  const f = worker(); assert.equal(f.listeners.fetch, undefined);
  let done;
  f.listeners.push({ data: { json: () => ({ title: 'PRIVATE', body: 'PRIVATE', url: 'https://evil.test', kind: 'attention' }) }, waitUntil: value => { done = value; } });
  await done;
  assert.equal(f.notices[0][0], 'Piper');
  assert.match(f.notices[0][1].body, /needs your attention/);
  assert.doesNotMatch(JSON.stringify(f.notices), /PRIVATE|evil/);
});

test('notification click ignores payload URLs and focuses only the same-origin app root', async () => {
  const f = worker(); let done;
  f.listeners.notificationclick({ notification: { data: { url: 'https://evil.test' }, close() {} }, waitUntil: value => { done = value; } });
  await done;
  assert.deepEqual(f.operations, [['navigate', 'https://piper.example/'], ['focus']]);
});

test('worker accepts only a bounded plain-text opted-in session field, never messages or test names', async () => {
  const f = worker(); let done;
  const push = async value => {
    f.listeners.push({ data: { json: () => value }, waitUntil: value => { done = value; } });
    await done; return f.notices.at(-1)[1].body;
  };
  assert.equal(await push({ v: 1, kind: 'attention', session: ' HTTP\n\u202e\0\u200b ', message: 'PRIVATE' }), '“HTTP” needs your attention. Open Piper to see what’s waiting.');
  assert.equal(await push({ v: 1, kind: 'test', session: 'PRIVATE' }), 'Test notification from Piper.');
  assert.match(await push({ v: 2, kind: 'attention', session: 'PRIVATE' }), /^Piper needs/);
  assert.match(await push({ v: 1, kind: 'attention', session: ' \n' }), /^Piper needs/);
  assert.doesNotMatch(JSON.stringify(f.notices), /PRIVATE/);
});
