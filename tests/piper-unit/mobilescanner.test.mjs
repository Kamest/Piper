import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import qrcode from '../../piper/web/vendor/qrcode/qrcode.js';
import decode from '../../piper/web/vendor/jsqr/jsqr.js';
import { readQRPhoto, startQRScanner } from '../../piper/web/dist/mobilescanner.js';

function qrPixels(value) {
  const qr = qrcode(0, 'M'); qr.addData(value); qr.make();
  const scale = 5, size = (qr.getModuleCount() + 8) * scale;
  const data = new Uint8ClampedArray(size * size * 4).fill(255);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const row = Math.floor(y / scale) - 4, col = Math.floor(x / scale) - 4;
    if (row >= 0 && col >= 0 && row < qr.getModuleCount() && col < qr.getModuleCount() && qr.isDark(row, col)) {
      const i = (y * size + x) * 4; data[i] = data[i + 1] = data[i + 2] = 0;
    }
  }
  return { data, width: size, height: size };
}

function fixture(getUserMedia, pixels = qrPixels('fixture QR, no private data')) {
  const document = Object.assign(new EventTarget(), { hidden: false, createElement: () => ({ width: 0, height: 0,
    getContext: () => ({ drawImage() {}, getImageData: () => pixels }) }) });
  const timers = new Map(); let serial = 0;
  const window = Object.assign(new EventTarget(), {
    setTimeout(fn, ms) { timers.set(++serial, { fn, ms }); return serial; }, clearTimeout(id) { timers.delete(id); },
  });
  let stopped = 0;
  const stream = { getTracks: () => [{ stop() { stopped++; } }] };
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { mediaDevices: { getUserMedia: getUserMedia ?? (async () => stream) } } });
  Object.assign(globalThis, { document, window });
  const video = { srcObject: null, readyState: 4, videoWidth: pixels.width, videoHeight: pixels.height, play: async () => {}, pause() {} };
  const values = [], errors = [];
  const stop = startQRScanner(video, value => { values.push(value); }, error => errors.push(error));
  return { document, window, timers, stream, video, stop, stopped: () => stopped, values, errors,
    async frame() {
      await new Promise(resolve => setImmediate(resolve));
      const entry = [...timers].find(([, timer]) => timer.ms < 1000);
      if (entry) { timers.delete(entry[0]); await entry[1].fn(); }
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test('Safari fallback decodes the actual desktop QR without BarcodeDetector', () => {
  const value = 'https://phone.example/#iroh=' + Buffer.from(JSON.stringify({ v: 1, endpoint: '11'.repeat(32), relay: 'https://relay.example?token=' + 'x'.repeat(43), code: 'c'.repeat(43) })).toString('base64url');
  const image = qrPixels(value);
  assert.equal(decode(image.data, image.width, image.height)?.data, value);
});

test('scanner reads frames locally and does not navigate or upload a QR', async () => {
  const f = fixture(); await f.frame();
  assert.deepEqual(f.values, ['fixture QR, no private data']);
  assert.deepEqual(f.errors, []);
  f.stop(); assert.equal(f.stopped(), 1); assert.equal(f.video.srcObject, null); assert.equal(f.timers.size, 0);
});

test('cancel before camera permission resolves stops a late camera without decoding', async () => {
  let resolve;
  const f = fixture(() => new Promise(done => { resolve = done; }));
  f.stop(); resolve(f.stream); await f.frame();
  assert.equal(f.stopped(), 1); assert.deepEqual(f.values, []); assert.equal(f.video.srcObject, null);
});

test('camera permission denial is an actionable error and leaves no scan timers', async () => {
  const f = fixture(async () => { throw new DOMException('private system detail', 'NotAllowedError'); });
  await f.frame();
  assert.equal(f.errors.length, 1); assert.equal(f.errors[0], 'denied');
  assert.equal(f.timers.size, 0); assert.deepEqual(f.values, []);
});

test('backgrounding and page exit stop camera tracks and pending work', async () => {
  for (const event of ['visibilitychange', 'pagehide']) {
    const f = fixture(undefined, { data: new Uint8ClampedArray(100 * 100 * 4).fill(255), width: 100, height: 100 }); await f.frame();
    assert.equal(f.stopped(), 0, 'camera is still scanning before leaving');
    if (event === 'visibilitychange') { f.document.hidden = true; f.document.dispatchEvent(new Event(event)); }
    else f.window.dispatchEvent(new Event(event));
    assert.equal(f.stopped(), 1); assert.equal(f.video.srcObject, null); assert.equal(f.timers.size, 0);
    f.stop(); assert.equal(f.stopped(), 1, 'cleanup is idempotent');
  }
});

test('a camera that fails to start releases tracks and reports only a stable error', async () => {
  const f = fixture(); f.video.play = async () => { throw new Error('private camera error'); };
  await f.frame();
  assert.deepEqual(f.errors, ['unavailable']); assert.equal(f.stopped(), 1); assert.equal(f.timers.size, 0);
});

test('a browser refusing the preview stream still closes the camera', async () => {
  const f = fixture();
  Object.defineProperty(f.video, 'srcObject', { get: () => null, set(value) { if (value) throw new Error('fixture stream refusal'); } });
  await f.frame();
  assert.deepEqual(f.errors, ['unavailable']); assert.equal(f.stopped(), 1); assert.equal(f.timers.size, 0);
});

test('permission wait is bounded, and a camera granted after the deadline is closed', async () => {
  let resolve;
  const f = fixture(() => new Promise(done => { resolve = done; }));
  const deadline = [...f.timers.values()].find(timer => timer.ms === 20000);
  deadline.fn();
  assert.deepEqual(f.errors, ['unavailable']); assert.equal(f.timers.size, 0);
  resolve(f.stream); await f.frame();
  assert.equal(f.stopped(), 1); assert.deepEqual(f.values, []);
});

test('missing camera API still offers a stable unavailable result', () => {
  const f = fixture(); f.stop(); delete navigator.mediaDevices;
  const errors = [];
  startQRScanner(f.video, () => assert.fail('unexpected QR'), kind => errors.push(kind));
  assert.deepEqual(errors, ['unavailable']); assert.equal(f.timers.size, 0);
});

test('photo fallback rejects non-images and oversized files before allocating a blob URL', async () => {
  for (const file of [{ type: 'text/plain', size: 10 }, { type: 'image/png', size: 21 * 1024 * 1024 }]) {
    await assert.rejects(readQRPhoto(file), /Photo unavailable/);
  }
});

test('photo decoding failure revokes its local URL without exposing file details', async () => {
  const originalURL = globalThis.URL, originalImage = globalThis.Image, revoked = [];
  globalThis.URL = { createObjectURL: () => 'blob:fixture-photo', revokeObjectURL: value => revoked.push(value) };
  globalThis.Image = class { async decode() { throw new Error('fixture decode'); } };
  try { await assert.rejects(readQRPhoto({ type: 'image/png', size: 10 })); }
  finally { globalThis.URL = originalURL; globalThis.Image = originalImage; }
  assert.deepEqual(revoked, ['blob:fixture-photo']);
});
