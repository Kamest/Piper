// node --test: the token handling and the state timeout of the GUI's API client (web/src/api.ts)
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';

const store = new Map();
globalThis.window = { localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) } };
globalThis.location = { hash: '', pathname: '/piper/', search: '' };
globalThis.history = { replaceState: () => { location.hash = ''; } };
let answer = () => new Response('{}', { status: 200 });
const seen = [];
globalThis.fetch = async (path, init) => { seen.push(init.headers.Authorization); return answer(path, init); };
const { absorbFragmentToken, api, ApiError, forgetToken, token } = await import('../../piper/web/dist/api.js');

test('a link token is only a candidate: a refused one never touches the token of the other tabs', async () => {
  store.set('piper.token', 'good');
  location.hash = '#token=bad';
  assert.equal(absorbFragmentToken(), true);
  assert.equal(location.hash, '');
  assert.equal(token(), 'bad');
  assert.equal(store.get('piper.token'), 'good');   // not overwritten before piperd accepts it
  answer = () => new Response('{"error":"unauthorized"}', { status: 401 });
  const e = await api.ping().catch((x) => x);
  assert.ok(e instanceof ApiError && e.status === 401 && e.token === 'bad');
  assert.equal(forgetToken(e.token), true);   // the stored one is left to try
  assert.equal(token(), 'good');
  assert.equal(store.get('piper.token'), 'good');
});

test('an accepted link token replaces the stored one; a refused stored token is dropped', async () => {
  store.set('piper.token', 'old');
  location.hash = '#x=1&token=new';
  absorbFragmentToken();
  answer = () => new Response('{"ok":true}', { status: 200 });
  await api.ping();
  assert.equal(store.get('piper.token'), 'new');
  assert.equal(forgetToken('new'), false);   // nothing else left: the GUI asks
  assert.equal(store.has('piper.token'), false);
  assert.equal(absorbFragmentToken(), false);   // no fragment, nothing changes
});

test('forgetToken keeps a token another tab stored meanwhile', () => {
  store.set('piper.token', 'fresh');
  assert.equal(forgetToken('stale'), true);
  assert.equal(store.get('piper.token'), 'fresh');
});

test('a hung piperd: the state poll gives up with a readable error instead of waiting forever', async () => {
  store.set('piper.token', 't');
  answer = (_p, init) => new Promise((_res, rej) => init.signal.addEventListener('abort', () => rej(init.signal.reason)));
  const t0 = Date.now();
  const alive = setTimeout(() => {}, 10000);   // AbortSignal.timeout's timer does not keep node running
  const e = await api.state('').catch((x) => x);
  clearTimeout(alive);
  assert.match(String(e.message), /no answer in 8 s/);
  assert.ok(Date.now() - t0 >= 7900);
});

test('N8: an invisible character in a copied link / pasted token never reaches the header (fetch threw - "piperd is not responding")', async () => {
  const { setToken } = await import('../../piper/web/dist/api.js');
  store.set('piper.token', 'good');
  // a zero-width space, a bidi override, an NBSP: piperd's tokens are URL-safe base64, none of them belongs there
  location.hash = '#token=' + encodeURIComponent('​Abc-_9‮ ');
  assert.equal(absorbFragmentToken(), true);
  assert.equal(token(), 'Abc-_9');
  answer = () => new Response('{"ok":true}', { status: 200 });
  seen.length = 0;
  await api.ping();
  assert.equal(seen[0], 'Bearer Abc-_9');
  // a cut %-escape in the link does not throw at boot
  location.hash = '#token=abc%E0%A4';
  assert.doesNotThrow(() => absorbFragmentToken());
  assert.match(token(), /^[\x21-\x7e]*$/);
  // the Access dialog: a pasted token with a trailing zero-width joiner / newline
  setToken('Pasted_tok‍\n');
  assert.equal(store.get('piper.token'), 'Pasted_tok');
});

test('project allow sends a GUI-authenticated POST and reports a refusal', async () => {
  store.set('piper.token', 'gui-test-token');
  answer = (path, init) => {
    assert.equal(path, '/api/sessions/s1/allow-uncovered');
    assert.equal(init.method, 'POST');
    assert.equal(init.headers.Authorization, 'Bearer gui-test-token');
    assert.deepEqual(JSON.parse(init.body), {});
    return new Response('{"allowed":["/project (+1)"]}', { status: 200 });
  };
  assert.deepEqual((await api.allowUncovered('s1')).data, { allowed: ['/project (+1)'] });
  answer = () => new Response('{"error":"a protected mount cannot be allowed"}', { status: 400 });
  await assert.rejects(api.allowUncovered('s1'), /protected mount/);
});
