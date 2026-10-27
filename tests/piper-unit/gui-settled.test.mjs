import './_sandbox.mjs';   // only inside the test sandbox
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../piper-e2e/scans/settled.js', import.meta.url), 'utf8');

function harness({inflight = 0, readyState = 'complete', animations = [], collector = true} = {}) {
  let callback, disconnected = false, frame, timeout, result, error;
  const state = {inflight};
  const document = {documentElement: {}, readyState, getAnimations: () => animations};
  const promise = vm.runInNewContext(source, {
    window: collector ? {__piperScan: state} : {}, document,
    MutationObserver: class {
      constructor(fn) { callback = fn; }
      observe() {}
      disconnect() { disconnected = true; }
    },
    requestAnimationFrame: fn => { frame = fn; return 1; },
    cancelAnimationFrame: () => { frame = undefined; },
    setTimeout: fn => { timeout = fn; return 2; },
    clearTimeout: () => { timeout = undefined; },
  });
  promise.then(value => { result = value; }, e => { error = e; });
  return {
    state, document,
    mutate: () => callback(),
    async step() { const fn = frame; frame = undefined; fn?.(); await Promise.resolve(); },
    async expire() { timeout(); await Promise.resolve(); },
    get result() { return result; }, get error() { return error; },
    get cleaned() { return disconnected && !frame && !timeout; },
  };
}

test('settles only after two quiet rendering frames and releases observers', async () => {
  const h = harness();
  await h.step(); await h.step();
  assert.equal(h.result, undefined);
  await h.step();
  assert.equal(h.result, true);
  assert.ok(h.cleaned);
});

test('pending fetch and late DOM mutation postpone completion', async () => {
  const h = harness({inflight: 1});
  for (let i = 0; i < 5; i++) await h.step();
  assert.equal(h.result, undefined);
  h.state.inflight = 0;
  await h.step();
  h.mutate();
  await h.step(); await h.step();
  assert.equal(h.result, undefined);
  await h.step();
  assert.equal(h.result, true);
});

test('waits for page load and finite animations but ignores infinite spinners', async () => {
  const finite = {playState: 'running', effect: {getComputedTiming: () => ({endTime: 200})}};
  const infinite = {playState: 'running', effect: {getComputedTiming: () => ({endTime: Infinity})}};
  const h = harness({readyState: 'loading', animations: [finite, infinite]});
  for (let i = 0; i < 3; i++) await h.step();
  h.document.readyState = 'complete';
  for (let i = 0; i < 3; i++) await h.step();
  assert.equal(h.result, undefined);
  finite.playState = 'finished';
  await h.step(); await h.step();
  assert.equal(h.result, true);
});

test('stuck work times out and cleans up instead of silently continuing', async () => {
  const h = harness({inflight: 1});
  await h.step(); await h.expire();
  assert.match(h.error.message, /did not settle/);
  assert.equal(h.result, undefined);
  assert.ok(h.cleaned);
});

test('timeout identifies pending work, document state and rendering progress without page content', async () => {
  const finite = {playState: 'running', effect: {getComputedTiming: () => ({endTime: 200})}};
  const h = harness({inflight: 2, readyState: 'loading', animations: [finite]});
  await h.step(); h.mutate(); await h.expire();
  for (const field of ['"inflight":2', '"ready":"loading"', '"animations":1', '"mutations":1', '"frames":1']) {
    assert.ok(h.error.message.includes(field), h.error.message);
  }
  assert.ok(h.cleaned);
});

test('missing error collector fails rather than pretending the page is idle', async () => {
  const h = harness({collector: false});
  await h.step();
  assert.match(h.error.message, /collector is not installed/);
});
