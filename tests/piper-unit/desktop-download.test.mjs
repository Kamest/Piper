import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { releaseAsset, githubAsset, download } from '../../desktop/build/download.mjs';
import { prefetch, electronCachePath, toolsetAssets } from '../../desktop/build/prefetch.mjs';

const url = 'https://github.com/electron/electron/releases/download/v45.0.0/electron-v45.0.0-linux-x64.zip';
const bytes = Buffer.from('synthetic archive');
const checksum = createHash('sha256').update(bytes).digest('hex');
function apiFixture({ digest = `sha256:${checksum}`, body = bytes, size = bytes.length } = {}) {
  const calls = [];
  const api = async (endpoint, binary) => {
    calls.push([endpoint, !!binary]);
    return binary ? body : Buffer.from(JSON.stringify({ assets: [{ id: 123, name: 'electron-v45.0.0-linux-x64.zip', state: 'uploaded', size, digest }] }));
  };
  return { api, calls };
}

test('release assets accept only fixed official repositories and safe asset paths', () => {
  assert.deepEqual(releaseAsset(url), { repo: 'electron/electron', tag: 'v45.0.0', name: 'electron-v45.0.0-linux-x64.zip' });
  for (const bad of [url.replace('https:', 'http:'), url.replace('github.com', 'example.com'),
    url.replace('electron/electron', 'acme/other'), url.replace('github.com', 'secret@github.com'),
    `${url}?token=secret`, `${url}#fragment`, url.replace('v45.0.0/', 'v45%2fother/'),
    url.replace('electron-v45.0.0-linux-x64.zip', '..%2fother.zip')]) assert.throws(() => releaseAsset(bad));
});

test('504 fallback retrieves the exact official asset through API and verifies the original pin', async () => {
  const f = apiFixture();
  assert.deepEqual(await download(url, checksum, { fetcher: async () => ({ ok: false, status: 504 }), api: f.api }), bytes);
  assert.deepEqual(f.calls, [['repos/electron/electron/releases/tags/v45.0.0', false], ['repos/electron/electron/releases/assets/123', true]]);
});

test('transport timeout can use API, while 403 and 404 remain errors', async () => {
  const f = apiFixture();
  assert.deepEqual(await download(url, checksum, { fetcher: async () => { throw Error('timeout'); }, api: f.api }), bytes);
  for (const status of [403, 404]) {
    const blocked = apiFixture();
    await assert.rejects(download(url, checksum, { fetcher: async () => ({ ok: false, status }), api: blocked.api }), /HTTP/);
    assert.equal(blocked.calls.length, 0);
  }
});

test('a corrupt successful direct download fails without accepting another payload', async () => {
  const f = apiFixture();
  await assert.rejects(download(url, checksum, { fetcher: async () => ({ ok: true, arrayBuffer: async () => Buffer.from('corrupt') }), api: f.api }), /SHA-256/);
  assert.equal(f.calls.length, 0);
});

test('API rejects corruption, truncation, missing digest and disagreement with a pin', async () => {
  for (const [options, pin] of [[{ body: Buffer.from('wrong archive dat') }, checksum], [{ size: bytes.length + 1 }, checksum],
    [{ digest: undefined }, undefined], [{ digest: `sha256:${'0'.repeat(64)}` }, checksum]]) {
    const f = apiFixture(options);
    // Explicitly remove the digest; the fixture's default otherwise supplies one.
    if (options.digest === undefined && pin === undefined) {
      const api = async (endpoint, binary) => binary ? bytes : Buffer.from(JSON.stringify({ assets: [{ id: 123, name: 'electron-v45.0.0-linux-x64.zip', state: 'uploaded', size: bytes.length }] }));
      await assert.rejects(githubAsset(url, pin, api), /checksum/);
    } else await assert.rejects(githubAsset(url, pin, f.api));
  }
});

test('old tool releases without API digests still require and verify a pinned SHA-256', async () => {
  const api = async (endpoint, binary) => binary ? bytes : Buffer.from(JSON.stringify({ assets: [{ id: 123, name: 'electron-v45.0.0-linux-x64.zip', state: 'uploaded', size: bytes.length }] }));
  assert.deepEqual((await githubAsset(url, checksum, api)).bytes, bytes);
  await assert.rejects(githubAsset(url, '0'.repeat(64), api), /SHA-256/);
});

test('Electron prefetch seeds a validated archive and versioned checksums into one explicit cache', async () => {
  const f = apiFixture();
  const writes = new Map();
  const cache = path.resolve('build-cache');
  await prefetch('electron', { platform: 'linux', arch: 'x64', env: { ELECTRON_CACHE: cache, electron_config_cache: cache },
    asset: (target, pin) => githubAsset(target, pin, f.api), saveFile: async (file, content) => writes.set(file, content) });
  assert.deepEqual(writes.get(electronCachePath(cache, '45.0.0', 'electron-v45.0.0-linux-x64.zip')), bytes);
  assert.equal(writes.get(path.join(cache, 'SHASUMS256.txt-45.0.0')), `${checksum} *electron-v45.0.0-linux-x64.zip\n`);
  const corrupt = apiFixture({ body: Buffer.from('corrupt') });
  let wrote = false;
  await assert.rejects(prefetch('electron', { platform: 'linux', arch: 'x64', env: { ELECTRON_CACHE: cache, electron_config_cache: cache },
    asset: (target, pin) => githubAsset(target, pin, corrupt.api), saveFile: async () => { wrote = true; } }));
  assert.equal(wrote, false);
});

test('all six native build targets select only matching pinned installer toolsets', () => {
  for (const platform of ['linux', 'darwin', 'win32']) for (const arch of ['x64', 'arm64']) {
    const items = toolsetAssets(platform, arch);
    assert.ok(items.every(item => /^[a-f0-9]{64}$/.test(item.checksum)));
    assert.equal(items.filter(item => item.tag.startsWith('7zip')).length, 1);
    if (platform === 'linux') assert.ok(items.some(item => item.name.includes(arch === 'x64' ? 'linux-amd64' : 'linux-arm64v8')));
    if (platform === 'darwin') assert.ok(items.some(item => item.name.includes(`bundle-${arch === 'x64' ? 'x86_64' : 'arm64'}-`)));
    if (platform === 'win32') assert.equal(items.filter(item => item.tag.startsWith('nsis')).length, 2);
  }
  assert.throws(() => toolsetAssets('linux', 'unsupported'));
});

test('prefetch refuses implicit user caches and unknown modes', async () => {
  for (const mode of ['electron', 'tools', 'other']) await assert.rejects(prefetch(mode, { env: {} }));
});
