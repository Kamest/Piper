// Application framing only. All cryptography and peer authentication are provided by iroh.
import type { TerminalSocket, Transport } from './transport.js';

export interface QuicStream {
  write(data: Uint8Array): Promise<void>;
  read(count: number): Promise<Uint8Array>;
  read_to_end(): Promise<Uint8Array>;
  finish(): Promise<void>;
  cancel(): Promise<void>;
}
export interface QuicConnection { open(): Promise<QuicStream>; close(): Promise<void>; invalidate?(): void; check?(): void }
export interface Ticket { v: 1; endpoint: string; relay: string; code?: string }
const encode = new TextEncoder();
const decode = new TextDecoder('utf-8', { fatal: true });
const HEADER_LIMIT = 8192;
const BODY_LIMIT = 16 << 20;
const RESPONSE_LIMIT = 32 << 20;

/** A pasted invitation stays on the trusted client origin and the pinned relay. */
export function parsePairingLink(value: string, origin: string, relay: string): string {
  if (value.length > 4096) throw new Error('Invalid pairing link');
  const url = new URL(value);
  const fragment = new URLSearchParams(url.hash.slice(1));
  const encoded = fragment.get('iroh');
  if (url.origin !== origin || url.protocol !== 'https:' || url.username || url.password
      || url.pathname !== '/' || url.search || fragment.size !== 1 || !encoded) throw new Error('Invalid pairing link');
  const parsed = parseTicket(encoded);
  if (!parsed.code || new URL(parsed.relay).origin !== relay) throw new Error('Invalid pairing link');
  return encoded;
}

export function parseTicket(encoded: string): Ticket {
  if (encoded.length > 4096 || !/^[A-Za-z0-9_-]+$/.test(encoded)) throw new Error('Invalid mobile link');
  const value: unknown = JSON.parse(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')));
  if (!value || typeof value !== 'object') throw new Error('Invalid mobile link');
  const t = value as Record<string, unknown>;
  if (t.v !== 1 || typeof t.endpoint !== 'string' || !/^[0-9a-f]{64}$/.test(t.endpoint)
    || typeof t.relay !== 'string' || (t.code !== undefined && (typeof t.code !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(t.code)))) throw new Error('Invalid mobile link');
  const url = new URL(t.relay);
  if (url.protocol !== 'https:' || url.username || url.password || url.hash || url.pathname !== '/'
    || /[^\x21-\x7e]/.test(t.relay) || t.relay.includes('\\')
    || (url.search && !/^\?token=[A-Za-z0-9_-]{43,128}$/.test(url.search))) throw new Error('Invalid relay origin');
  return { v: 1, endpoint: t.endpoint, relay: url.origin + url.search, ...(typeof t.code === 'string' ? { code: t.code } : {}) };
}

export function header(value: object): Uint8Array {
  const body = encode.encode(JSON.stringify({ v: 1, ...value }));
  if (body.length > HEADER_LIMIT) throw new Error('Mobile request header too large');
  const data = new Uint8Array(4 + body.length);
  new DataView(data.buffer).setUint32(0, body.length);
  data.set(body, 4);
  return data;
}

function metadata(bytes: Uint8Array): { status: number; headers?: Record<string, string>; error?: string; encoding?: string } {
  const meta: unknown = JSON.parse(decode.decode(bytes));
  if (!meta || typeof meta !== 'object' || !('v' in meta) || meta.v !== 1 || !('status' in meta)
    || typeof meta.status !== 'number' || meta.status < 100 || meta.status > 599) throw new Error('Invalid mobile response');
  return meta as { status: number; headers?: Record<string, string>; error?: string; encoding?: string };
}

async function responseBody(payload: Uint8Array, encoding?: string, signal?: AbortSignal | null): Promise<Uint8Array<ArrayBuffer>> {
  if (!encoding) return new Uint8Array(payload);
  if (encoding !== 'gzip' || typeof DecompressionStream !== 'function') throw new Error('Unsupported mobile compression');
  const reader = new Blob([new Uint8Array(payload)]).stream().pipeThrough(new DecompressionStream('gzip')).getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      signal?.throwIfAborted();
      const { done, value } = await interruptible(reader.read(), signal);
      if (done) break;
      size += value.byteLength;
      if (size > RESPONSE_LIMIT) throw new Error('Mobile response too large');
      chunks.push(value);
    }
  } catch (error) { void reader.cancel().catch(() => {}); throw error; }
  const result = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.byteLength; }
  return result;
}

/** Cancellation must settle JS even when a suspended QUIC promise never settles. */
async function interruptible<T>(pending: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return pending;
  let abort = () => {};
  const cancelled = new Promise<never>((_, reject) => {
    abort = () => reject(signal.reason);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
  try { return await Promise.race([pending, cancelled]); }
  finally { signal.removeEventListener('abort', abort); }
}

export async function exchange(connection: QuicConnection, value: object, body = new Uint8Array(), signal?: AbortSignal | null): Promise<Response> {
  signal?.throwIfAborted();
  if (body.length > BODY_LIMIT) throw new Error('Mobile request body too large');
  try { return await exchangeStream(connection, value, body, signal); }
  catch (error) {
    // A stream can open on a stale QUIC connection and fail only during I/O.
    // Retire that connection for later calls, without replaying this request.
    // A request deadline can be a slow backend, not a dead shared connection.
    // Probe independently before disturbing other HTTP requests or terminals.
    if (!signal?.aborted) connection.invalidate?.();
    else if (signal.reason?.name === 'TimeoutError') connection.check?.();
    throw error;
  }
}

async function exchangeStream(connection: QuicConnection, value: object, body: Uint8Array, signal?: AbortSignal | null): Promise<Response> {
  const opening = connection.open();
  void opening.then(s => { if (signal?.aborted) void s.cancel().catch(() => {}); }, () => {});
  const stream = await interruptible(opening, signal);
  const abort = () => { void stream.cancel().catch(() => {}); };
  const step = <T>(run: () => Promise<T>) => { signal?.throwIfAborted(); return interruptible(run(), signal); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    signal?.throwIfAborted();
    await step(() => stream.write(header(value)));
    if (body.length) await step(() => stream.write(body));
    await step(() => stream.finish());
    const data = await step(() => stream.read_to_end());
    if (data.length < 4 || data.length > RESPONSE_LIMIT + HEADER_LIMIT + 4) throw new Error('Invalid mobile response size');
    const size = new DataView(data.buffer, data.byteOffset, data.byteLength).getUint32(0);
    if (!size || size > HEADER_LIMIT || size + 4 > data.length) throw new Error('Invalid mobile response header');
    const meta = metadata(data.subarray(4, size + 4));
    const payload = data.subarray(size + 4);
    const decoded = meta.status === 204 || meta.status === 205 || meta.status === 304 ? null : await responseBody(payload, meta.encoding, signal);
    return new Response(decoded, { status: meta.status, headers: meta.headers ?? {} });
  } catch (error) {
    void stream.cancel().catch(() => {});
    signal?.throwIfAborted();
    throw error;
  } finally { signal?.removeEventListener('abort', abort); }
}

class MobileSocket implements TerminalSocket {
  binaryType: BinaryType = 'arraybuffer';
  readyState = 0;
  onopen: WebSocket['onopen'] = null;
  onclose: WebSocket['onclose'] = null;
  onerror: WebSocket['onerror'] = null;
  onmessage: WebSocket['onmessage'] = null;
  private stream: QuicStream | null = null;
  private writes = Promise.resolve();
  private queued = 0;
  constructor(connection: QuicConnection, path: string, private readonly disposed: () => void) { void this.connect(connection, path); }
  private emit(name: 'open' | 'error' | 'close' | 'message', event: Event): void {
    const handler = this[`on${name}`];
    if (handler) (handler as (this: WebSocket, e: Event) => unknown).call(this as unknown as WebSocket, event);
  }
  private async connect(connection: QuicConnection, path: string): Promise<void> {
    let failureCode = 1006;
    try {
      const stream = await connection.open();
      this.stream = stream;
      if (this.readyState === 3) { await stream.cancel(); return; }
      await stream.write(header({ op: 'ws', path }));
      const prefix = await stream.read(4);
      const size = new DataView(prefix.buffer, prefix.byteOffset, prefix.byteLength).getUint32(0);
      if (!size || size > HEADER_LIMIT) throw new Error('Invalid terminal response');
      const status = metadata(await stream.read(size)).status;
      if (status !== 101) {
        if (status === 401 || status === 403) failureCode = 4401;
        throw new Error('Terminal refused');
      }
      if (this.readyState === 3) return;
      this.readyState = 1;
      this.emit('open', new Event('open'));
      while (this.readyState === 1) {
        const prefix = await stream.read(5);
        const size = new DataView(prefix.buffer, prefix.byteOffset, prefix.byteLength).getUint32(0);
        const kind = prefix[4];
        if (size > 4 << 20 || (kind !== 1 && kind !== 2 && kind !== 8)) throw new Error('Invalid terminal frame');
        const data = await stream.read(size);
        if (kind === 8) break;
        if (this.readyState !== 1) return;
        this.emit('message', new MessageEvent('message', { data: kind === 1 ? decode.decode(data) : data.slice().buffer }));
      }
      this.close();
    } catch {
      if (this.readyState === 3) return;
      this.emit('error', new Event('error'));
      this.close(failureCode);
    }
  }
  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.readyState !== 1) throw new Error('Terminal not connected');
    // Piper's existing terminal sends this first. The bridge authenticates with its private local token.
    if (typeof data === 'string') {
      try { if ('auth' in JSON.parse(data)) return; } catch { /* ordinary terminal input */ }
    }
    const size = typeof data === 'string' ? encode.encode(data).byteLength : data instanceof Blob ? data.size : data.byteLength;
    if (size > 4 << 20 || this.queued + size > 8 << 20) { this.close(1009); return; }
    this.queued += size;
    this.writes = this.writes.then(async () => {
      if (this.readyState !== 1 || !this.stream) return;
      const bytes = typeof data === 'string' ? encode.encode(data) : data instanceof Blob ? new Uint8Array(await data.arrayBuffer())
        : ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
      const packet = new Uint8Array(5 + bytes.byteLength);
      new DataView(packet.buffer).setUint32(0, bytes.byteLength);
      packet[4] = typeof data === 'string' ? 1 : 2;
      packet.set(bytes, 5);
      await this.stream.write(packet);
    }).catch(() => this.close(1006)).finally(() => { this.queued -= size; });
  }
  close(code = 1000): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.disposed();
    void this.stream?.cancel().catch(() => {});
    this.emit('close', new CloseEvent('close', { code, wasClean: code === 1000 }));
  }
}

export function mobileTransport(connect: () => Promise<QuicConnection>): Transport & { reset(): void } {
  let lifetime = new AbortController();
  const sockets = new Set<MobileSocket>();
  return {
    visibleTerminalsOnly: true,
    reset() {
      lifetime.abort(new Error('Mobile connection reset; check the outcome before repeating a change'));
      lifetime = new AbortController();
      for (const socket of [...sockets]) socket.close(1006);
    },
    async fetch(path, init) {
      if (!path.startsWith('/api/')) throw new Error('Invalid mobile API path');
      const controller = new AbortController();
      const endpoint = path.split('?')[0], method = init.method ?? 'GET';
      // GUI DB limit: two 3600-second phases, setup, SSH and bridge reserves; polling keeps its short deadline.
      const database = method === 'POST' && ['/api/db/query', '/api/db/execute', '/api/db/tx'].includes(endpoint!) || method === 'GET' && endpoint === '/api/db/meta';
      const signals = [lifetime.signal, init.signal, AbortSignal.timeout(database ? (2 * 3600 + 240) * 1000 : 130000)].filter((s): s is AbortSignal => !!s);
      const cancel = (event: Event) => controller.abort((event.target as AbortSignal).reason);
      for (const s of signals) { s.addEventListener('abort', cancel, { once: true }); if (s.aborted) controller.abort(s.reason); }
      const signal = controller.signal;
      try {
        signal.throwIfAborted();
        const headers: Record<string, string> = {};
        new Headers(init.headers).forEach((value, name) => {
          if (['content-type', 'if-none-match', 'x-piper-environment'].includes(name)) headers[name] = value;
        });
        const body = init.body == null ? new Uint8Array() : new Uint8Array(await interruptible(new Response(init.body).arrayBuffer(), signal));
        signal.throwIfAborted();
        return await exchange(await interruptible(connect(), signal), { op: 'http', method: init.method ?? 'GET', path, headers,
          ...(typeof DecompressionStream === 'function' ? { accept_encoding: 'gzip' } : {}) }, body, signal);
      } finally { for (const s of signals) s.removeEventListener('abort', cancel); }
    },
    socket(url) {
      const u = new URL(url);
      // The existing TermView installs its callbacks synchronously before this promise opens the stream.
      const connection = { async open() { return (await connect()).open(); }, async close() {} };
      const socket = new MobileSocket(connection, u.pathname + u.search, () => sockets.delete(socket));
      sockets.add(socket);
      return socket;
    },
  };
}
