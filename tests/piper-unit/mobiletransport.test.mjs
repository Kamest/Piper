import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { gzipSync } from 'node:zlib';
import { exchange, header, mobileTransport, parseTicket } from '../../piper/web/dist/mobiletransport.js';
import { installTransport, terminalSocket, transportFetch } from '../../piper/web/dist/transport.js';

const encode = new TextEncoder();
const concat = (...parts) => { const b = new Uint8Array(parts.reduce((n, p) => n + p.length, 0)); let at = 0; for (const p of parts) { b.set(p, at); at += p.length; } return b; };
const ticket = (value) => Buffer.from(JSON.stringify(value)).toString('base64url');

test('the link pins a PC key and permits only a credential-free HTTPS relay origin', () => {
  const value = { v: 1, endpoint: '11'.repeat(32), relay: 'https://relay.example', code: 'x'.repeat(43) };
  assert.deepEqual(parseTicket(ticket(value)), value);
  for (const change of [{ v: 2 }, { endpoint: 'other' }, { code: 'short' }, { relay: 'http://relay.example' },
    { relay: 'https://user:pass@relay.example' }, { relay: 'https://relay.example/#secret' }, { relay: 'https://relay.example/path' }]) {
    assert.throws(() => parseTicket(ticket({ ...value, ...change })));
  }
});

test('relay capability stays in the private ticket and accepts exactly one bounded token', () => {
  const base = { v: 1, endpoint: '11'.repeat(32), relay: 'https://relay.example' };
  const token = 'x'.repeat(43);
  assert.equal(parseTicket(ticket({ ...base, relay: base.relay + '/?token=' + token })).relay,
    base.relay + '?token=' + token);
  for (const query of ['token=short', 'other=' + token, 'token=' + token + '&other=1',
    'token=' + token + '&token=' + token, 'token=%78' + 'x'.repeat(42)]) {
    assert.throws(() => parseTicket(ticket({ ...base, relay: base.relay + '?' + query })));
  }
});

function stream(response = concat(header({ status: 200 }), encode.encode('response'))) {
  return { writes: [], cancelled: false, async write(b) { this.writes.push(b); }, async finish() {},
    async read_to_end() { return response; }, async cancel() { this.cancelled = true; } };
}

test('database response deadline covers bridge and remote work without extending ordinary polling', async () => {
  const original = AbortSignal.timeout;
  const deadlines = [];
  AbortSignal.timeout = ms => { deadlines.push(ms); return new AbortController().signal; };
  try {
    const transport = mobileTransport(async () => ({ open: async () => stream() }));
    await transport.fetch('/api/db/query', { method: 'POST', body: '{}' });
    await transport.fetch('/api/state', {});
    assert.ok(deadlines[0] > (2 * 3600 + 210) * 1000);
    assert.equal(deadlines[1], 130000);
  } finally { AbortSignal.timeout = original; }
});

async function aborted(run) {
  const controller = new AbortController();
  let watchdog;
  const guard = new Promise((_, reject) => { watchdog = setTimeout(() => reject(new Error('operation did not abort')), 150); });
  const pending = run(controller.signal);
  await new Promise(resolve => setImmediate(resolve));
  controller.abort();
  try { await assert.rejects(Promise.race([pending, guard]), { name: 'AbortError' }); }
  finally { clearTimeout(watchdog); }
}

test('abort settles a suspended connection attempt without retrying a mutation', async () => {
  let calls = 0;
  const transport = mobileTransport(() => { calls++; return new Promise(() => {}); });
  await aborted(signal => transport.fetch('/api/git/push', { method: 'POST', signal }));
  assert.equal(calls, 1);
});

test('abort settles suspended open and disposes a stream arriving after cancellation', async () => {
  let resolve;
  const pending = new Promise(r => { resolve = r; });
  await aborted(signal => exchange({ open: () => pending }, { op: 'http' }, undefined, signal));
  const late = stream(); resolve(late);
  await new Promise(r => setImmediate(r));
  assert.equal(late.cancelled, true);
});

test('abort settles even if a suspended stream never acknowledges read or cancel', async () => {
  const s = stream();
  let reading = false, cancelling = false;
  s.read_to_end = () => { reading = true; return new Promise(() => {}); };
  s.cancel = () => { cancelling = true; return new Promise(() => {}); };
  await aborted(signal => exchange({ open: async () => s }, { op: 'http' }, undefined, signal));
  assert.equal(reading, true);
  assert.equal(cancelling, true);
});

test('network loss emits a reconnectable terminal close rather than an authentication refusal', async () => {
  globalThis.CloseEvent ??= class extends Event { constructor(type, init) { super(type); Object.assign(this, init); } };
  const ws = mobileTransport(async () => { throw new Error('network unavailable'); }).socket('wss://client.example/ws/s1');
  const code = await new Promise(resolve => { ws.onclose = e => resolve(e.code); });
  assert.equal(code, 1006);
});

test('transport reset cancels suspended mutations once and lets subsequent reads use a new stream', async () => {
  const first = stream(); first.read_to_end = () => new Promise(() => {});
  let calls = 0;
  const transport = mobileTransport(async () => ({ open: async () => ++calls === 1 ? first : stream() }));
  const pending = transport.fetch('/api/git/push', { method: 'POST', body: '{}' });
  await new Promise(resolve => setImmediate(resolve));
  transport.reset();
  await assert.rejects(pending, /check the outcome/);
  assert.equal(calls, 1, 'the mutation was not replayed');
  assert.equal(await (await transport.fetch('/api/ping', {})).text(), 'response');
});

test('an explicit terminal authentication refusal still stops automatic reconnect', async () => {
  globalThis.CloseEvent ??= class extends Event { constructor(type, init) { super(type); Object.assign(this, init); } };
  const bytes = header({ status: 403 }); let offset = 0;
  const s = { async write() {}, async read(n) { const b = bytes.slice(offset, offset + n); offset += n; return b; }, async cancel() {} };
  const ws = mobileTransport(async () => ({ open: async () => s })).socket('wss://client.example/ws/s1');
  const code = await new Promise(resolve => { ws.onclose = e => resolve(e.code); });
  assert.equal(code, 4401);
});

test('HTTP bodies and response metadata cross the stream; bearer/cookies never do', async () => {
  const s = stream(concat(header({ status: 200, headers: { 'Content-Type': 'application/octet-stream', ETag: 'version' } }), new Uint8Array([0, 255, 42])));
  const transport = mobileTransport(async () => ({ open: async () => s }));
  const response = await transport.fetch('/api/sessions', { method: 'POST', headers: {
    Authorization: 'Bearer must-stay-local', Cookie: 'private', 'Content-Type': 'application/json', 'X-Piper-Environment': 'server',
  }, body: '{"task":"hello"}' });
  const bytes = concat(...s.writes);
  const size = new DataView(bytes.buffer).getUint32(0);
  const meta = JSON.parse(new TextDecoder().decode(bytes.subarray(4, size + 4)));
  assert.deepEqual(meta, { v: 1, op: 'http', method: 'POST', path: '/api/sessions', accept_encoding: 'gzip', headers: {
    'content-type': 'application/json', 'x-piper-environment': 'server',
  } });
  assert.equal(new TextDecoder().decode(bytes.subarray(size + 4)), '{"task":"hello"}');
  assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [0, 255, 42]);
  assert.equal(response.headers.get('etag'), 'version');
});

test('a lost response never retries a write', async () => {
  let calls = 0;
  const transport = mobileTransport(async () => { calls++; throw new Error('connection lost'); });
  await assert.rejects(transport.fetch('/api/git/push', { method: 'POST', body: '{}' }), /connection lost/);
  assert.equal(calls, 1);
});

test('I/O failure invalidates, but a request deadline probes without replaying a mutation', async () => {
  for (const timedOut of [false, true]) {
    let discarded = 0, checked = 0, opened = 0;
    const s = stream();
    s.read_to_end = () => timedOut ? new Promise(() => {}) : Promise.reject(new Error('connection lost'));
    const signal = timedOut ? AbortSignal.timeout(10) : undefined;
    const guard = setTimeout(() => {}, 100);
    try {
      await assert.rejects(exchange({ open: async () => { opened++; return s; }, invalidate() { discarded++; }, check() { checked++; } },
        { op: 'http', method: 'POST', path: '/api/git/push' }, undefined, signal));
    } finally { clearTimeout(guard); }
    assert.equal(discarded, timedOut ? 0 : 1);
    assert.equal(checked, timedOut ? 1 : 0);
    assert.equal(opened, 1, 'the failed mutation must never be replayed');
    assert.equal(s.cancelled, true);
  }
});

test('ordinary request cancellation leaves the shared connection available', async () => {
  const s = stream(); s.read_to_end = () => new Promise(() => {});
  let discarded = 0;
  await aborted(signal => exchange({ open: async () => s, invalidate() { discarded++; } }, { op: 'http' }, undefined, signal));
  assert.equal(discarded, 0);
});

test('abort cancels the stream and bounded response parsing rejects truncated metadata', async () => {
  const controller = new AbortController();
  const s = stream();
  s.read_to_end = () => new Promise((_, reject) => { s.cancel = async () => { s.cancelled = true; reject(new Error('cancelled')); }; controller.abort(); });
  await assert.rejects(exchange({ open: async () => s }, { op: 'http', path: '/api/state' }, undefined, controller.signal), { name: 'AbortError' });
  assert.equal(s.cancelled, true);
  for (const bytes of [new Uint8Array(), new Uint8Array([0, 0, 0, 20, 123]), new Uint8Array([0, 1, 0, 0])]) {
    const bad = stream(bytes);
    await assert.rejects(exchange({ open: async () => bad }, { op: 'http' }), /Invalid mobile response/);
  }
});

test('304 has no body and unsupported targets are refused before connecting', async () => {
  const s = stream(header({ status: 304, headers: { ETag: 'same' } }));
  const response = await exchange({ open: async () => s }, { op: 'http' });
  assert.equal(response.status, 304);
  assert.equal(await response.text(), '');
  await assert.rejects(mobileTransport(() => assert.fail('must not connect')).fetch('https://evil.example/api/state', {}));
});

test('negotiated gzip preserves Unicode JSON and rejects malformed or oversized inflation', async () => {
  const raw = JSON.stringify({ text: 'Konverzace 🌲 '.repeat(10000) });
  const packed = concat(header({ status: 200, encoding: 'gzip', headers: { 'Content-Type': 'application/json' } }), gzipSync(raw));
  const s = stream(packed);
  const transport = mobileTransport(async () => ({ open: async () => s }));
  const response = await transport.fetch('/api/state', {});
  assert.equal(await response.text(), raw);
  const request = JSON.parse(new TextDecoder().decode(s.writes[0].slice(4)));
  assert.equal(request.accept_encoding, 'gzip');
  for (const [encoding, bytes] of [['gzip', new Uint8Array([1, 2, 3])], ['br', gzipSync(raw)],
    ['gzip', gzipSync(Buffer.alloc((32 << 20) + 1, 65))]]) {
    const bad = stream(concat(header({ status: 200, encoding }), bytes));
    await assert.rejects(exchange({ open: async () => bad }, { op: 'http' }));
    assert.equal(bad.cancelled, true);
  }
});

test('a browser without decompression requests the legacy identity response', async () => {
  const previous = globalThis.DecompressionStream;
  globalThis.DecompressionStream = undefined;
  try {
    const s = stream();
    assert.equal(await (await mobileTransport(async () => ({ open: async () => s })).fetch('/api/state', {})).text(), 'response');
    const request = JSON.parse(new TextDecoder().decode(s.writes[0].slice(4)));
    assert.equal(request.accept_encoding, undefined);
  } finally { globalThis.DecompressionStream = previous; }
});

test('terminal frames preserve order, binary output and close; desktop auth stays off the stream', async () => {
  globalThis.CloseEvent ??= class extends Event { constructor(type, init) { super(type); Object.assign(this, init); } };
  const frame = (kind, payload) => { const b = new Uint8Array(payload.length + 5); new DataView(b.buffer).setUint32(0, payload.length); b[4] = kind; b.set(payload, 5); return b; };
  const incoming = concat(header({ status: 101 }), frame(1, encode.encode('{"state":"ready"}')), frame(2, new Uint8Array([0, 255, 27])));
  let offset = 0, stopped = false, rejectRead;
  const writes = [];
  const s = {
    async read(n) {
      if (offset + n > incoming.length) return new Promise((_, reject) => { rejectRead = reject; });
      const b = incoming.slice(offset, offset + n); offset += n; return b;
    },
    async write(bytes) { writes.push(bytes); },
    async cancel() { stopped = true; rejectRead?.(new Error('cancelled')); },
  };
  const ws = mobileTransport(async () => ({ open: async () => s })).socket('wss://client.example/ws/s1?cols=80');
  const messages = [];
  await new Promise((resolve, reject) => {
    ws.onopen = () => {
      ws.send('{"auth":"desktop-token"}');
      ws.send('first'); ws.send(new Uint8Array([1, 2])); ws.send('last');
    };
    ws.onerror = reject;
    ws.onmessage = e => { messages.push(e.data); if (messages.length === 2) resolve(); };
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(writes.length, 4); // upgrade + three ordered input frames
  assert.equal(JSON.parse(new TextDecoder().decode(writes[0].slice(4))).path, '/ws/s1?cols=80');
  assert.deepEqual(writes.slice(1).map(p => p[4]), [1, 2, 1]);
  assert.equal(new TextDecoder().decode(writes[1].slice(5)), 'first');
  assert.equal(new TextDecoder().decode(writes[3].slice(5)), 'last');
  assert.deepEqual([...new Uint8Array(messages[1])], [0, 255, 27]);
  ws.close();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(stopped, true);
  assert.equal(ws.readyState, 3);
  assert.throws(() => ws.send('after close'), /not connected/);
});

test('the optional transport leaves ordinary desktop HTTP and WebSocket untouched', async () => {
  const seen = [];
  globalThis.fetch = async (path) => { seen.push(path); return new Response('local'); };
  globalThis.WebSocket = class { constructor(url) { this.url = url; } };
  assert.equal(await (await transportFetch('/api/state', {})).text(), 'local');
  assert.equal(terminalSocket('ws://local/ws/s1').url, 'ws://local/ws/s1');
  assert.deepEqual(seen, ['/api/state']);
  installTransport({ fetch: async () => new Response('encrypted'), socket: () => ({ readyState: 1 }) });
  assert.equal(await (await transportFetch('/api/state', {})).text(), 'encrypted');
  assert.equal(terminalSocket('ws://local/ws/s1').readyState, 1);
});
