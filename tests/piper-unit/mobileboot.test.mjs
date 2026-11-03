import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { parsePairingLink, parseTicket, parseSetupCookie } from '../../piper/web/dist/mobiletransport.js';

const boot = readFileSync(new URL('../../piper/web/dist/mobileboot.js', import.meta.url), 'utf8')
  .replace(/^import .*;\n/gm, '').replace('void boot();', 'globalThis.bootDone = boot();');
const endpoint = '11'.repeat(32);
const relay = 'https://relay.example';
const original = { v: 1, endpoint, relay: relay + '?token=' + 'x'.repeat(43),
  key: Buffer.alloc(32, 7).toString('base64'), computer: 'Trusted computer', paired: true };

async function run(ticket, saved = original, reachable = false, paired = false, iosBrowser = false, options = {}) {
  const preferences = new Map();
  if (options.setupEncoded) preferences.set('piper.mobile.setup', options.setupEncoded);
  const stored = saved ? JSON.stringify(saved) : null;
  const writes = [], states = [], connections = [];
  const events = {}, offline = [], actions = [];
  const timers = new Map(), intervals = new Map(); let timerId = 0;
  let resets = 0, closed = 0, stateChanged;
  const context = {
    configurePush() {}, prepareMobilePush: async () => {}, stopLocalPush: async () => {},
    URL, URLSearchParams, AbortSignal, AbortController, Uint8Array, Promise, parsePairingLink, parseTicket, parseSetupCookie, btoa, atob, crypto,
    document: { cookie: options.cookie ?? '', hidden: false, getElementById: () => null, querySelector: selector => selector.startsWith('meta[') ? { content: relay } : null, addEventListener: (name, fn) => { events['document:' + name] = fn; } },
    location: { hash: ticket ? '#iroh=' + Buffer.from(JSON.stringify(ticket)).toString('base64url') : '', pathname: '/', origin: 'https://mobile.example' },
    history: { replaceState() {} },
    localStorage: { getItem: k => k === 'piper.mobile.profile' ? stored : preferences.get(k), setItem: (k, value) => {
      if (options.storageFails) throw new Error('storage unavailable');
      return k === 'piper.mobile.profile' ? writes.push(value) : preferences.set(k, value);
    }, removeItem: k => preferences.delete(k) },
    window: { __PIPER__: {}, setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); }, setInterval(fn,ms) {const id=++timerId;intervals.set(id,{fn,ms});return id;}, clearInterval(id){intervals.delete(id);}, addEventListener: (name, fn) => { events['window:' + name] = fn; } },
    applyChoice() {}, installMobileViewport() {}, mobileBrowserInstall: () => iosBrowser, showMobileState: (state, _computer, action) => { states.push(state); actions.push(action); }, mobileOffline: value => offline.push(value),
    clearMobileState() {}, setToken() {}, installTransport(value) { stateChanged = value.stateChanged; }, notifyTransportReady() { stateChanged?.(true); }, notifyTransportUnavailable() {},
    mobileTransport: () => ({ reset() { resets++; } }),
    init: async () => {},
    MobileConnection: { async connect(...args) {
      connections.push(args.map(value => value instanceof Uint8Array ? new Uint8Array(value) : value));
      if (!reachable) throw new Error('not reachable');
      return { close: async () => { closed++; } };
    } },
    exchange: async (_conn, value) => value.op === 'request_pairing'
      ? ({ ok: options.setupOk !== false, status: options.setupOk === false ? 409 : 200, json: async () => options.setupReply ?? { number: '482193', expires: Date.now() / 1000 + 500 } })
      : ({ ok: true, json: async () => ({ computer: 'New computer', paired }) }),
  };
  context.answerState = () => stateChanged?.(true);
  context.createMobileConnection = (endpoint, relay, key, signal) => new Promise((resolve, reject) => {
    let connection, disposed = false;
    const close = () => { if (disposed) return; disposed = true; signal.removeEventListener('abort', abort); void connection?.close(); };
    const abort = () => { close(); reject(signal.reason); };
    signal.addEventListener('abort', abort, {once:true});
    void context.MobileConnection.connect(endpoint,relay,key).then(raw => {
      connection=raw;
      if(disposed || signal.aborted){void raw.close();return;}
      resolve({...raw,close:async()=>close()});
    },error=>{close();reject(error)});
  });
  vm.runInNewContext(boot.replace("await import('./main.js');", 'globalThis.guiImports = (globalThis.guiImports ?? 0) + 1; globalThis.answerState();'), context, { filename: 'mobileboot.js' });
  await context.bootDone;
  return { writes, states, actions, connections, openLink: context.openLink, context, events, offline, timers, intervals, resets: () => resets, closed: () => closed };
}

test('screen lock and return reconnect the same paired identity without reloading the GUI', async () => {
  const result = await run(null, original, true, true);
  result.context.document.hidden = true;
  result.events['document:visibilitychange']();
  assert.equal(result.connections.length, 1);
  result.context.document.hidden = false;
  result.events['document:visibilitychange']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.connections.length, 2);
  assert.equal(result.resets(), 2);
  assert.equal(result.closed(), 1);
  assert.equal(result.context.guiImports, 1);
  assert.equal(result.offline.at(-1), false);
  assert.equal(result.connections[0][0], result.connections[1][0]);
  assert.deepEqual([...result.connections[0][2]], [...result.connections[1][2]]);
  assert.deepEqual([...result.connections[1][2]], [...Buffer.from(original.key, 'base64')]);
});

test('online and persisted page return renew the transport; ordinary pageshow does not', async () => {
  const result = await run(null, original, true, true);
  result.events['window:pageshow']({ persisted: false });
  assert.equal(result.connections.length, 1);
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  result.events['window:pageshow']({ persisted: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.connections.length, 3);
  assert.equal(result.context.guiImports, 1);
});

test('late hello from a superseded connection cannot replace the profile or revoke the current GUI', async () => {
  const result = await run(null, original, true, true), replies = [];
  result.context.exchange = async () => new Promise(resolve => replies.push(value => resolve({ ok: true, json: async () => value })));
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  result.context.document.hidden=true;result.events['document:visibilitychange']();
  result.context.document.hidden=false;result.events['document:visibilitychange']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(replies.length, 2);
  replies[1]({ computer: 'Current computer', paired: true });
  await new Promise(resolve => setImmediate(resolve));
  replies[0]({ computer: 'Stale reply', paired: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(result.writes.at(-1)).computer, 'Current computer');
  assert.ok(!result.states.includes('revoked'));
  assert.equal(result.context.guiImports, 1);
  assert.equal(result.offline.at(-1), false, 'a stale rejected attempt must not block a healthy GUI');
});

test('a terminal stream ending does not tear down the shared mobile connection', async () => {
  const result = await run(null, original, true, true);
  let closes = 0;
  const raw = { async open() { return { async read() { throw new Error('stream reset'); }, async cancel() {} }; }, async close() { closes++; } };
  const connection = result.context.guarded(raw);
  const stream = await connection.open();
  await assert.rejects(stream.read(5), /stream reset/);
  assert.equal(closes, 0);
  assert.equal(result.resets(), 0);
  assert.equal(result.offline.at(-1), false);
});

test('HTTP I/O failure retires its connection once and stale failure cannot reset its replacement', async () => {
  const result = await run(null, original, true, true);
  let closes = 0;
  const raw = { async open() {}, async close() { closes++; } };
  const failed = result.context.guarded(raw);
  const stale = result.context.guarded(raw);
  failed.invalidate(); failed.invalidate();
  assert.equal(closes, 1);
  assert.equal(result.resets(), 1);
  assert.equal(result.offline.at(-1), true);
  const [id, timer] = [...result.timers][0];
  result.timers.delete(id); timer.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.connections.length, 2);
  assert.equal(result.offline.at(-1), false);
  stale.invalidate();
  assert.equal(result.resets(), 1);
  assert.equal(result.offline.at(-1), false);
  assert.equal(result.timers.size, 0);
  assert.equal(result.context.guiImports, 1);
});

test('a failed recovery retries without depending on another GUI request', async () => {
  const result = await run(null, original, true, true);
  const connect = result.context.MobileConnection.connect;
  let attempts = 0;
  result.context.MobileConnection.connect = (...args) => ++attempts === 1 ? Promise.reject(new Error('temporarily offline')) : connect(...args);
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.offline.at(-1), true);
  assert.equal(result.timers.size, 1, 'exactly one reconnect timer');
  const [id, timer] = [...result.timers][0];
  assert.equal(timer.ms, 1000);
  result.timers.delete(id); timer.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(attempts, 2);
  assert.equal(result.offline.at(-1), false);
  assert.equal(result.context.guiImports, 1);
  assert.equal(result.timers.size, 0);
});

test('request deadlines coalesce a healthy probe and preserve terminals; a failed probe reconnects', async () => {
  const result = await run(null, original, true, true);
  let closes = 0, calls = 0, reply;
  const raw = { async open() {}, async close() { closes++; } };
  const connection = result.context.guarded(raw);
  result.context.exchange = async (peer, request) => {
    assert.equal(peer, raw); assert.equal(request.op, 'hello'); calls++;
    return new Promise(resolve => { reply = resolve; });
  };
  connection.check(); connection.check(); connection.check();
  assert.equal(calls, 1);
  reply({ ok: true, json: async () => ({ paired: true }) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 0); assert.equal(result.resets(), 0);
  assert.equal(result.offline.at(-1), false);
  connection.check();
  assert.equal(calls, 2);
  reply({ ok: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 1); assert.equal(result.resets(), 1);
  assert.equal(result.timers.size, 1);
});

test('a late probe failure cannot reset a newer connection', async () => {
  const result = await run(null, original, true, true);
  let reject;
  result.context.exchange = () => new Promise((_, fail) => { reject = fail; });
  const connection = result.context.guarded({ async open() {}, async close() {} });
  connection.check();
  result.context.exchange = async () => ({ ok: true, json: async () => ({ paired: true, computer: 'Current' }) });
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  const resets = result.resets();
  reject(new Error('stale probe'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.resets(), resets);
  assert.equal(result.offline.at(-1), false);
  assert.equal(result.timers.size, 0);
});

test('recovery has bounded backoff and stops immediately when access is revoked', async () => {
  const result = await run(null, original, true, true);
  let attempts = 0;
  result.context.MobileConnection.connect = async () => { attempts++; throw new Error('offline'); };
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  for (const delay of [1000, 2000, 4000, 8000, 15000, 15000]) {
    assert.equal(result.timers.size, 1);
    const [id, timer] = [...result.timers][0]; assert.equal(timer.ms, delay);
    result.timers.delete(id); timer.fn();
    await new Promise(resolve => setImmediate(resolve));
  }
  const before = attempts;
  assert.throws(() => result.context.removed(), /no longer has Piper access/);
  assert.equal(result.timers.size, 0);
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(attempts, before);
});

test('backgrounding stops retries; returning starts a fresh connection attempt', async () => {
  const result = await run(null, original, true, true), connect = result.context.MobileConnection.connect;
  result.context.MobileConnection.connect = async () => { throw new Error('offline'); };
  result.events['window:online'](); await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.timers.size, 1);
  result.context.document.hidden = true; result.events['document:visibilitychange']();
  assert.equal(result.timers.size, 0);
  result.context.MobileConnection.connect = connect;
  result.context.document.hidden = false; result.events['document:visibilitychange']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.offline.at(-1), false);
  assert.equal(result.timers.size, 0);
});

test('a foreign-relay link cannot overwrite the paired phone profile', async () => {
  const result = await run({ v: 1, endpoint: '22'.repeat(32), relay: 'https://attacker.example', code: 'c'.repeat(43) });
  assert.deepEqual(result.writes, []);
  assert.deepEqual(result.connections, []);
  assert.deepEqual(result.states, ['expired']);
});

test('another computer on the trusted relay cannot silently replace a paired identity', async () => {
  const result = await run({ v: 1, endpoint: '22'.repeat(32), relay: original.relay, code: 'c'.repeat(43) });
  assert.deepEqual(result.writes, []);
  assert.deepEqual(result.connections, []);
  assert.deepEqual(result.states, ['identity']);
});

test('a failed same-computer link cannot persist a replacement relay capability', async () => {
  const result = await run({ v: 1, endpoint, relay: relay + '?token=' + 'z'.repeat(43), code: 'c'.repeat(43) });
  assert.equal(result.connections.length, 1);
  assert.deepEqual(result.writes, []);
  assert.equal(result.states.at(-1), 'offline');
});

test('an unconfirmed first pairing keeps its candidate key only in memory', async () => {
  const result = await run({ v: 1, endpoint, relay: original.relay, code: 'c'.repeat(43) }, null, true);
  assert.equal(result.connections.length, 1);
  assert.deepEqual(result.writes, []);
  assert.equal(result.states.at(-1), 'pair');
});

test('pasted invitations reject foreign origins and relay changes before connecting or storage writes', async () => {
  const result = await run(null, null);
  const link = ticket => 'https://mobile.example/#iroh=' + Buffer.from(JSON.stringify(ticket)).toString('base64url');
  const valid = link({ v: 1, endpoint, relay, code: 'c'.repeat(43) });
  for (const bad of [valid.replace('mobile.example', 'evil.example'), valid.replace('https:', 'http:'),
    valid.replace('/#', '/other#'), valid.replace('/#', '/?leak=1#'),
    link({ v: 1, endpoint, relay: 'https://evil.example', code: 'c'.repeat(43) }), link({ v: 1, endpoint, relay }), valid + '&extra=1']) {
    assert.equal(typeof await result.openLink(bad), 'string');
  }
  assert.deepEqual(result.connections, []);
  assert.deepEqual(result.writes, []);
  assert.equal(await result.openLink(valid), null);
  assert.equal(result.connections.length, 1);
  assert.deepEqual(result.writes, [], 'the invitation and tentative key must not enter persistent storage');
});

test('pasting a valid link does not replace another paired computer', async () => {
  const result = await run({ v: 1, endpoint: '22'.repeat(32), relay, code: 'c'.repeat(43) });
  const link = 'https://mobile.example/#iroh=' + Buffer.from(JSON.stringify({ v: 1, endpoint: '33'.repeat(32), relay, code: 'c'.repeat(43) })).toString('base64url');
  assert.equal(await result.openLink(link), null);
  assert.equal(result.states.at(-1), 'identity');
  assert.deepEqual(result.connections, []);
  assert.deepEqual(result.writes, []);
});


test('unpaired iOS browser installs first without generating an identity or connecting', async () => {
  const result = await run(null, null, true, false, true);
  assert.equal(result.states.at(-1), 'install');
  assert.equal(result.connections.length, 0);
  assert.equal(result.writes.length, 0);
  result.actions.at(-1).browser();
  assert.equal(result.states.at(-1), 'unpaired');
  assert.equal(result.connections.length, 0);
  assert.equal(result.writes.length, 0);
  result.actions.at(-1).install();
  assert.equal(result.states.at(-1), 'install');
});

test('existing paired iOS browser keeps its identity and opens without installation gate', async () => {
  const result = await run(null, original, true, true, true);
  assert.ok(!result.states.includes('install'));
  assert.equal(result.context.guiImports, 1);
  assert.deepEqual([...result.connections[0][2]], [...Buffer.from(original.key, 'base64')]);
});

test('opening a pairing QR in an unpaired browser installs first and cannot accidentally grant browser access', async () => {
  const result = await run({ v: 1, endpoint, relay: original.relay, code: 'c'.repeat(43) }, null, true, false, true);
  assert.equal(result.states.at(-1), 'install');
  assert.equal(result.connections.length, 0, 'no browser identity or connection before choosing browser-only');
  assert.equal(result.writes.length, 0);
  result.actions.at(-1).browser();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.states.at(-1), 'pair', 'explicit browser-only choice can use the same invitation');
  assert.equal(result.connections.length, 1);
  assert.equal(result.writes.length, 0, 'consent is still required');
});

test('a QR opened by an already paired browser does not change its device key or force installation', async () => {
  const result = await run({ v: 1, endpoint, relay: original.relay, code: 'c'.repeat(43) }, original, true, true, true);
  assert.ok(!result.states.includes('install'));
  assert.equal(result.context.guiImports, 1);
  assert.deepEqual([...result.connections[0][2]], [...Buffer.from(original.key, 'base64')]);
});

test('foreground event cluster has one pending connection attempt, not competing same-key endpoints', async () => {
  const result = await run(null, original, true, true);
  let attempts = 0;
  result.context.MobileConnection.connect = () => { attempts++; return new Promise(() => {}); };
  result.context.document.hidden = true; result.events['document:visibilitychange']();
  result.context.document.hidden = false; result.events['document:visibilitychange']();
  await new Promise(resolve => setImmediate(resolve));
  result.events['window:pageshow']({persisted:true});
  result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(attempts, 1, 'one foreground cycle must not create competing endpoints');
});


test('foreground heartbeat checks quiet connections and stops during sleep and revocation',async()=>{
 const result=await run(null,original,true,true);let probes=0;
 result.context.exchange=async()=>{probes++;return {ok:true,json:async()=>({paired:true,computer:'Trusted'})}};
 assert.equal(result.intervals.size,1);const interval=[...result.intervals.values()][0];assert.equal(interval.ms,5000);
 interval.fn();await new Promise(resolve=>setImmediate(resolve));assert.equal(probes,1);
 result.context.document.hidden=true;result.events['document:visibilitychange']();assert.equal(result.intervals.size,0);
 result.context.document.hidden=false;result.events['document:visibilitychange']();await new Promise(resolve=>setImmediate(resolve));assert.equal(result.intervals.size,1);
 assert.throws(()=>result.context.removed(),/no longer/);assert.equal(result.intervals.size,0);
});

test('a capacity refusal of a health probe preserves the authenticated connection and terminals',async()=>{
 const result=await run(null,original,true,true);let closes=0;
 const connection=result.context.guarded({async open(){},async close(){closes++}});
 result.context.exchange=async()=>({ok:false,status:503});
 connection.check();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(closes,0);assert.equal(result.resets(),0);assert.equal(result.offline.at(-1),false);
});

test('a late GUI request while hidden cannot resurrect the retired WASM runtime',async()=>{
 const result=await run(null,original,true,true);
 result.context.document.hidden=true;result.events['document:visibilitychange']();
 await assert.rejects(result.context.connect(),/suspended/);
 await assert.rejects(result.context.connect(),/suspended/);
 assert.equal(result.connections.length,1,'background polls must not open another endpoint');
 assert.equal(result.closed(),1);
 assert.equal(result.intervals.size,0);
 result.context.document.hidden=false;result.events['document:visibilitychange']();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(result.connections.length,2,'foreground creates exactly one new runtime');
});


test('foreground GUI work before visibility dispatch waits for the recovery coordinator',async()=>{
 const result=await run(null,original,true,true);
 result.context.document.hidden=true;result.events['document:visibilitychange']();
 result.context.document.hidden=false; // Safari focus and polling precede visibilitychange.
 await assert.rejects(result.context.connect(),/suspended/);
 assert.equal(result.connections.length,1);
 result.events['window:pageshow']({persisted:true});
 result.events['document:visibilitychange']();
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(result.connections.length,2,'pageshow and later visibility adopt one foreground attempt');
});

test('a delayed health probe must not disconnect a terminal still receiving authenticated output', async () => {
  const result = await run(null, original, true, true);
  let closes = 0, rejectProbe;
  const connection = result.context.guarded({
    async open() { return { async read() { return new Uint8Array([1]); }, async read_to_end() { return new Uint8Array([2]); } }; },
    async close() { closes++; },
  });
  const stream = await connection.open();
  result.context.exchange = () => new Promise((_, reject) => { rejectProbe = reject; });
  connection.check();
  await stream.read(1);
  rejectProbe(new DOMException('probe deadline', 'TimeoutError'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 0, 'live terminal data disproves a dead shared connection');
  assert.equal(result.resets(), 0);
  assert.equal(result.offline.at(-1), false);
  connection.check();
  rejectProbe(new DOMException('probe deadline', 'TimeoutError'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 0, 'one quiet deadline alone is not proof of a dead connection');
  connection.check();
  rejectProbe(new DOMException('second probe deadline', 'TimeoutError'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 1, 'two unanswered probes without fresh data recover a dead connection');
});

test('HTTP progress during a delayed health probe also preserves the connection', async () => {
  const result = await run(null, original, true, true);
  let rejectProbe;
  const connection = result.context.guarded({
    async open() { return { async read_to_end() { return new Uint8Array([1, 2]); } }; }, async close() {},
  });
  result.context.exchange = () => new Promise((_, reject) => { rejectProbe = reject; });
  connection.check();
  await (await connection.open()).read_to_end();
  rejectProbe(new DOMException('probe deadline', 'TimeoutError'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.resets(), 0);
  assert.equal(result.offline.at(-1), false);
});

const setupTicket = { v: 1, endpoint, relay, setup: '22'.repeat(32) };
const setupEncoded = Buffer.from(JSON.stringify(setupTicket)).toString('base64url');

test('one QR in the browser sets only a public installation cookie and never creates a device key', async () => {
  const result = await run({ ...setupTicket, key: 'never-transfer-this' }, null, true, false, true);
  assert.equal(result.states.at(-1), 'install');
  assert.equal(result.writes.length, 0);
  assert.equal(result.connections.length, 0);
  const cookie = result.context.document.cookie;
  assert.ok(cookie.includes('Secure; SameSite=Strict'));
  assert.ok(cookie.includes('Max-Age=600; Path=/'));
  assert.deepEqual(parseTicket(parseSetupCookie(cookie, relay)), setupTicket);
  assert.ok(!cookie.includes('never-transfer-this'));
});

test('copied cookie in an empty Home Screen storage requests approval without a browser grant', async () => {
  const result = await run(null, null, true, false, false, { cookie: '__Host-piper-setup=' + setupEncoded });
  assert.equal(result.states.at(-1), 'approve-wait');
  assert.equal(result.actions.at(-1).number, '482193');
  assert.ok(result.writes.length > 0);
  assert.ok(result.writes.every(value => JSON.parse(value).paired === false));
  assert.equal(result.context.guiImports, undefined);
  assert.ok(result.context.document.cookie.includes('Max-Age=0'));
  assert.equal(result.connections.length, 1);
});

test('reopening pending setup retains its device key; desktop approval needs a fresh authenticated hello', async () => {
  const first = await run(setupTicket, null, true, false);
  const saved = JSON.parse(first.writes.at(-1));
  const result = await run(null, saved, true, false, false, { setupEncoded });
  assert.deepEqual([...result.connections[0][2]], [...first.connections[0][2]]);
  result.context.exchange = async (_conn, value) => ({ ok: true, json: async () => ({ computer: 'Approved computer', paired: value.op === 'hello' }) });
  const [id, timer] = [...result.timers].find(([, value]) => value.ms === 2000);
  result.timers.delete(id); timer.fn();
  await new Promise(resolve => setImmediate(resolve));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(result.writes.at(-1)).paired, true);
  assert.equal(result.context.guiImports, 1);
  assert.equal(result.context.localStorage.getItem('piper.mobile.setup'), undefined);
});

test('denied, stopped and expired setup never launch the GUI or retain a setup locator', async () => {
  for (const reason of ['denied', 'stopped', 'expired']) {
    const result = await run(setupTicket, null, true, false, false, { setupOk: false, setupReply: { reason } });
    assert.equal(result.states.at(-1), 'setup-ended');
    assert.equal(result.actions.at(-1).reason, reason);
    assert.equal(result.context.guiImports, undefined);
    assert.equal(result.context.localStorage.getItem('piper.mobile.setup'), undefined);
    assert.ok(result.writes.every(value => !JSON.parse(value).paired));
  }
});

test('a device that cannot retain its key sends no approval request', async () => {
  const result = await run(setupTicket, null, true, false, false, { storageFails: true });
  assert.equal(result.connections.length, 0);
  assert.equal(result.context.guiImports, undefined);
});

test('cancelling pending approval withdraws only that request and stops retries', async () => {
  const result = await run(setupTicket, null, true, false);
  const operations = [];
  result.context.exchange = async (_conn, value) => { operations.push(value); return { ok: true }; };
  result.actions.at(-1).cancel();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.states.at(-1), 'unpaired');
  assert.equal(result.context.localStorage.getItem('piper.mobile.setup'), undefined);
  assert.deepEqual(JSON.parse(JSON.stringify(operations)), [{ op: 'withdraw_pairing', setup: setupTicket.setup }]);
  assert.equal(result.timers.size, 0);
  assert.equal(result.context.guiImports, undefined);
});

test('returning to a pending approval retries with the same saved key once', async () => {
  const result = await run(setupTicket, null, true, false);
  result.context.document.hidden = true; result.events['document:visibilitychange']();
  assert.equal(result.timers.size, 0);
  result.context.document.hidden = false; result.events['document:visibilitychange']();
  result.events['window:pageshow']({ persisted: true }); result.events['window:online']();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.connections.length, 2);
  assert.deepEqual([...result.connections[0][2]], [...result.connections[1][2]]);
  assert.equal(result.states.at(-1), 'approve-wait');
  assert.equal(result.context.guiImports, undefined);
});

test('a failed approval poll keeps the confirmed code visible and a fresh authenticated hello still launches after approval', async () => {
  for (const failure of ['connection', 'hello', 'request']) {
    const result = await run(setupTicket, null, true, false);
    const connect = result.context.MobileConnection.connect;
    if (failure === 'connection') result.context.MobileConnection.connect = async () => { throw new Error('relay reconnect failed'); };
    result.context.exchange = async (_conn, value) => {
      if (failure === 'hello') throw new Error('hello response lost');
      if (value.op === 'request_pairing') return { ok: false, status: 503, json: async () => ({ error: 'temporary capacity' }) };
      return { ok: true, json: async () => ({ computer: 'New computer', paired: false }) };
    };
    let [id, timer] = [...result.timers].find(([, t]) => t.ms === 2000);
    result.timers.delete(id); timer.fn();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.states.at(-1), 'approve-wait', failure + ' must not replace the code with an offline/expired screen');
    assert.equal(result.actions.at(-1).number, '482193');
    assert.equal(result.context.guiImports, undefined);
    assert.ok(result.writes.every(value => !JSON.parse(value).paired));
    result.context.MobileConnection.connect = connect;
    result.context.exchange = async () => ({ ok: true, json: async () => ({ computer: 'New computer', paired: true }) });
    [id, timer] = [...result.timers].find(([, t]) => t.ms === 8000);
    result.timers.delete(id); timer.fn();
    await new Promise(resolve => setImmediate(resolve));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(result.context.guiImports, 1);
    assert.equal(JSON.parse(result.writes.at(-1)).paired, true);
    assert.equal(result.context.localStorage.getItem('piper.mobile.setup'), undefined);
  }
});

test('an unreachable computer without a confirmed code still shows offline; an old code cannot mask it past its deadline', async () => {
  const first = await run(setupTicket, null, false, false);
  assert.equal(first.states.at(-1), 'offline');
  const result = await run(setupTicket, null, true, false);
  const now = Date.now();
  result.context.Date = class extends Date { static now() { return now + 601000; } };
  result.context.exchange = async () => { throw new Error('still offline after setup deadline'); };
  const [id, timer] = [...result.timers].find(([, t]) => t.ms === 2000);
  result.timers.delete(id); timer.fn();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.states.at(-1), 'offline');
  assert.equal(result.context.guiImports, undefined);
});
