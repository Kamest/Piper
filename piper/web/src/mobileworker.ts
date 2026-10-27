// The unchanged iroh WASM and all its sockets/timers live inside this disposable context.
import init, { MobileConnection } from '../vendor/iroh/piper_mobile.js';
import type { QuicStream } from './mobiletransport.js';
import type { WorkerReply, WorkerRequest } from './mobileworkerclient.js';

const scope = globalThis as unknown as {
  onmessage: ((event: MessageEvent<WorkerRequest>) => void) | null;
  postMessage(data: WorkerReply, transfer?: Transferable[]): void;
};
let connection: MobileConnection | null = null;
let connecting = false;
let serial = 0;
let opening = 0;
const streams = new Map<number, QuicStream>();

scope.onmessage = async ({ data }) => {
  try {
    let value: number | Uint8Array | undefined;
    if (data.op === 'connect') {
      if (connecting || connection) throw new Error('Already connecting');
      connecting = true;
      try {
        await init();
        connection = await MobileConnection.connect(data.endpoint, data.relay, data.key);
      } finally { data.key.fill(0); }
    } else {
      if (!connection) throw new Error('Not connected');
      if (data.op === 'open') {
        if (streams.size + opening >= 64) throw new Error('Stream capacity reached');
        opening++;
        try {
          const stream = await connection.open();
          value = ++serial;
          streams.set(value, stream);
        } finally { opening--; }
      } else {
        const stream = streams.get(data.stream);
        if (!stream) throw new Error('Stream ended');
        if (data.op === 'write') await stream.write(data.data);
        else if (data.op === 'read') value = await stream.read(data.count);
        else if (data.op === 'read_to_end') {
          try { value = await stream.read_to_end(); }
          finally { streams.delete(data.stream); }
        } else if (data.op === 'finish') await stream.finish();
        else {
          streams.delete(data.stream);
          await stream.cancel();
        }
      }
    }
    scope.postMessage({ id: data.id, ok: true, ...(value === undefined ? {} : { value }) },
      value instanceof Uint8Array && value.buffer instanceof ArrayBuffer ? [value.buffer] : []);
  } catch {
    // Rust/relay error strings can contain a private URL; only protocol success/failure crosses back.
    scope.postMessage({ id: data.id, ok: false });
  }
};
