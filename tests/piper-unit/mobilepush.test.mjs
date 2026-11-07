import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

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
    URL, location: { origin: 'https://mobile.example' },
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


function computersFixture(options = {}) {
  const f = fixture(options), registrations = new Map(), requests = [], enabled = new Map();
  function registration(scope) {
    if (registrations.has(scope)) return registrations.get(scope);
    let subscription = null;
    const value = { scope: 'https://mobile.example' + scope, active: {}, pushManager: {
      getSubscription: async () => subscription,
      async subscribe(config) {
        requests.push([scope, 'browser-subscribe']);
        subscription = { options: config, async unsubscribe() { requests.push([scope, 'browser-unsubscribe']); subscription = null; return true; },
          toJSON: () => ({ endpoint: 'https://push.example' + scope, keys: { auth: 'fixture', p256dh: 'fixture' } }) };
        return subscription;
      },
    } };
    registrations.set(scope, value); return value;
  }
  const rpc = scope => async value => {
    requests.push([scope, value.action]);
    if (value.action === 'subscribe') enabled.set(scope, value.subscription);
    if (value.action === 'unsubscribe') enabled.delete(scope);
    return { available: true, enabled: enabled.has(scope), subscriptionId: enabled.has(scope) ? scope + 'fixture' : '',
      publicKey: Buffer.from([4, ...Array(64).fill(scope === '/' ? 1 : 2)]).toString('base64url'), showTitles: false };
  };
  f.context.navigator.serviceWorker = {
    register: async (_script, { scope }) => registration(scope),
    getRegistration: async scope => registrations.get(scope) ?? registrations.get('/'),
    ready: Promise.resolve(registration('/')),
  };
  f.context.configurePush(rpc('/'), 'Primary', { rpc: rpc('/backup-push/'), scope: '/backup-push/' });
  return { ...f, requests, enabled, registrations, rpc };
}
const flushPush = () => new Promise(resolve => setImmediate(resolve));

test('one permission gesture subscribes both computers with independent VAPID keys and scopes', async () => {
  const f = computersFixture();
  await f.context.enablePush(); await flushPush();
  assert.equal(f.calls.filter(call => call[0] === 'permission').length, 1);
  assert.equal(f.enabled.size, 2);
  assert.equal(f.enabled.get('/').endpoint, 'https://push.example/');
  assert.equal(f.enabled.get('/backup-push/').endpoint, 'https://push.example/backup-push/');
  const primary = await f.registrations.get('/').pushManager.getSubscription();
  const backup = await f.registrations.get('/backup-push/').pushManager.getSubscription();
  assert.notDeepEqual([...primary.options.applicationServerKey], [...backup.options.applicationServerKey]);
  await f.context.syncMobilePush(); await f.context.syncMobilePush();
  assert.equal(f.requests.filter(request => request[1] === 'browser-subscribe').length, 2, 'reconciliation reuses each existing subscription');
  assert.equal(f.enabled.size, 2, 'one subscription per computer; never one per remote session');
  f.context.configurePush(f.rpc('/backup-push/'), 'Backup', { rpc: f.rpc('/'), scope: '/' }, '/backup-push/');
  assert.equal(f.context.pushComputer(), 'Backup');
  assert.equal((await f.context.pushStatus()).state, 'on');
});

test('load never grants push consent; an offline companion is retried without disturbing primary', async () => {
  const f = computersFixture();
  await f.context.syncMobilePush();
  assert.equal(f.enabled.size, 0);
  assert.equal(f.calls.length, 0);
  f.context.configurePush(f.rpc('/'), 'Primary', { rpc: async () => { throw new Error('offline'); }, scope: '/backup-push/' });
  await f.context.enablePush(); await flushPush();
  assert.equal(f.enabled.size, 1);
  const primary = await f.registrations.get('/').pushManager.getSubscription();
  f.context.configurePush(f.rpc('/'), 'Primary', { rpc: f.rpc('/backup-push/'), scope: '/backup-push/' });
  await f.context.syncMobilePush();
  assert.equal(f.enabled.size, 2);
  assert.equal(await f.registrations.get('/').pushManager.getSubscription(), primary);
  assert.equal(f.calls.filter(call => call[0] === 'permission').length, 1);
});

test('Forget backup only unsubscribes its scope; turning off cancels both and prevents automatic re-registration', async () => {
  const f = computersFixture();
  await f.context.enablePush(); await flushPush();
  const primary = await f.registrations.get('/').pushManager.getSubscription();
  await f.context.forgetBackupPush();
  assert.equal(await f.registrations.get('/').pushManager.getSubscription(), primary);
  assert.equal(await f.registrations.get('/backup-push/').pushManager.getSubscription(), null);
  await f.context.syncMobilePush();
  assert.equal(await f.registrations.get('/backup-push/').pushManager.getSubscription(), null);
  f.context.configurePush(f.rpc('/'), 'Primary', { rpc: f.rpc('/backup-push/'), scope: '/backup-push/' });
  await f.context.syncMobilePush();
  await f.context.disablePush(); await flushPush();
  assert.equal(await f.registrations.get('/').pushManager.getSubscription(), null);
  assert.equal(await f.registrations.get('/backup-push/').pushManager.getSubscription(), null);
  assert.equal(f.enabled.size, 0);
  await f.context.syncMobilePush();
  assert.equal(f.enabled.size, 0);
});

test('actual snapshot(manager) reads only local sessions even when GUI lists the other computer too', () => {
  const output = execFileSync('python3', ['-c', `
import json, threading, sys
from types import SimpleNamespace
sys.path.insert(0, 'piper')
from mobilepush import snapshot
class Manager:
    def __init__(self, name):
        self.lock = threading.RLock()
        self.sessions = {name: SimpleNamespace(id=name, hidden=False, state='attention', since=1, kind='project')}
        self.pending = {'card-' + name: {'id': 'card-' + name, 'session': name}}
    def list(self):
        raise AssertionError('must not fetch remote GUI sessions')
a, b = Manager('primary-session'), Manager('backup-session')
a.remote_sessions = b.sessions
b.remote_sessions = a.sessions
print(json.dumps([snapshot(a), snapshot(b)]))
`], { cwd: fileURLToPath(new URL('../../', import.meta.url)), encoding: 'utf8' });
  const [primary, backup] = JSON.parse(output);
  assert.deepEqual(primary.sessions.map(session => session.id), ['primary-session']);
  assert.deepEqual(backup.sessions.map(session => session.id), ['backup-session']);
  assert.deepEqual(primary.pending, ['card-primary-session']);
  assert.deepEqual(backup.pending, ['card-backup-session']);
});
