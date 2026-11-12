import './_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { desktopUpdates, MANIFEST, RELEASES } = require('../../desktop/src/lib/updates.cjs');
const { machine } = require('../../desktop/src/lib/machine.cjs');

test('core update identifies an old CLI by its usage banner and exit code, preserving other errors', async () => {
  const usage = 'piper.sh - run Piper, the AI agent orchestrator. Opt-in:';
  for (const [code, lines, expected] of [
    [2, [usage], /has no `piper update` yet .*git pull/],
    [2, ['Another command error'], /^Piper was not updated: run `piper update` in a terminal/],
    [1, [usage], /^Piper was not updated: run `piper update` in a terminal/],
    [2, [usage, 'piper: update refused'], /^Piper was not updated: update refused$/],
  ]) {
    const ops = machine({ command: async (_args, { onLine }) => {
      lines.forEach(onLine);
      return { code, stdout: lines.join('\n') };
    } });
    await assert.rejects(ops.update(), error => expected.test(error.message), JSON.stringify({ code, lines }));
  }
});

const release = (v, extra = {}) => ({ tag_name: 'desktop-v' + v,
  html_url: 'https://github.com/Kamest/Piper/releases/tag/desktop-v' + v, ...extra });
function fixture(options = {}) {
  const changes = [], opened = [], calls = [], client = new EventEmitter();
  client.setFeedURL = value => calls.push(['feed', value]);
  client.checkForUpdates = async () => ({ updateInfo: { version: '0.2.0' } });
  client.downloadUpdate = async () => { client.emit('download-progress', { percent: 40 }); client.emit('update-downloaded'); };
  // the Pages manifest is unavailable unless a test publishes one: the API list answers
  let response = { ok: true, json: async () => [release('0.1.8')] }, manifest = { ok: false };
  const updates = desktopUpdates({ version: '0.1.8', platform: 'darwin', arch: 'arm64', packaged: true,
    windowsStore: false, signed: false, fetch: async (...args) => { calls.push(['fetch', ...args]); return args[0] === MANIFEST ? manifest : response; },
    updater: () => client, openExternal: async url => opened.push(url), changed: s => changes.push(s),
    now: () => '2026-11-04T12:00:00Z', ...options });
  return { updates, changes, opened, calls, client, response: r => { response = r; }, manifest: m => { manifest = m; } };
}
test('a manual check always reports the installed version is current', async () => {
  const f = fixture(); await f.updates.check();
  assert.deepEqual(f.changes.map(s => s.status), ['checking', 'current']);
  assert.equal(f.updates.state().current, '0.1.8');
  assert.equal(f.updates.state().checkedAt, '2026-11-04T12:00:00Z');
  assert.ok(f.calls[0][2].signal instanceof AbortSignal);
  assert.deepEqual(f.calls.map(c => c[1]), [MANIFEST, RELEASES]);
});
test('the Pages manifest answers without the rate-limited GitHub API', async () => {
  const f = fixture({ platform: 'linux', appImage: true });
  f.manifest({ ok: true, json: async () => ({ tag_name: 'desktop-v0.2.0', html_url: release('0.2.0').html_url }) });
  await f.updates.check();
  assert.equal(f.updates.state().status, 'available'); assert.equal(f.updates.state().latest, '0.2.0');
  assert.deepEqual(f.calls.filter(c => c[0] === 'fetch').map(c => c[1]), [MANIFEST]);
  assert.equal(f.calls.find(c => c[0] === 'feed')[1].url, 'https://github.com/Kamest/Piper/releases/download/desktop-v0.2.0/');
});
test('a manifest naming a core tag, a foreign page or an older version announces nothing', async () => {
  for (const latest of [{ tag_name: 'v0.2.0', html_url: release('0.2.0').html_url },
    { tag_name: 'desktop-v0.2.0', html_url: 'https://example.com/Kamest/Piper/releases/tag/desktop-v0.2.0' },
    { tag_name: 'desktop-v0.2.0/../../x', html_url: release('0.2.0').html_url }, release('0.1.7')]) {
    const f = fixture({ platform: 'linux', appImage: true }); f.manifest({ ok: true, json: async () => latest });
    await f.updates.check();
    assert.equal(f.updates.state().status, 'current', JSON.stringify(latest));
    assert.ok(!f.calls.some(c => c[0] === 'feed'));
  }
});
test('a broken or unreachable manifest falls back to the API list', async () => {
  for (const manifest of [{ ok: true, json: async () => [release('0.2.0')] }, { ok: true, json: async () => { throw Error('html'); } }]) {
    const f = fixture(); f.manifest(manifest); f.response({ ok: true, json: async () => [release('0.2.0')] });
    await f.updates.check(); assert.equal(f.updates.state().latest, '0.2.0');
  }
  const f = fixture({ fetch: async url => { if (url === MANIFEST) throw Error('offline'); return { ok: true, json: async () => [release('0.2.0')] }; } });
  await f.updates.check(); assert.equal(f.updates.state().status, 'available');
});
test('a Linux deb package opens the release page; only an AppImage installs by itself', async () => {
  const deb = fixture({ platform: 'linux' }); deb.response({ ok: true, json: async () => [release('0.2.0')] });
  assert.equal(deb.updates.state().method, 'download');
  await deb.updates.check(); await deb.updates.download();
  assert.deepEqual(deb.opened, [release('0.2.0').html_url]); assert.ok(!deb.calls.some(c => c[0] === 'feed'));
  assert.equal(fixture({ platform: 'linux', appImage: true }).updates.state().method, 'install-on-quit');
});
test('ignore core, drafts, prereleases and foreign links; choose the newest desktop version', async () => {
  const f = fixture();
  f.response({ ok: true, json: async () => [release('0.1.9'), release('0.2.0'), release('0.1.10'),
    release('8.0.0', { draft: true }), release('9.0.0', { prerelease: true }),
    release('7.0.0', { html_url: 'https://example.com' }), release('4.0.0', { tag_name: 'v4.0.0' })] });
  await f.updates.check(); assert.equal(f.updates.state().status, 'available');
  assert.equal(f.updates.state().latest, '0.2.0'); assert.deepEqual(f.opened, []);
  await f.updates.download(); assert.deepEqual(f.opened, [release('0.2.0').html_url]);
  assert.ok(!f.calls.some(c => c[0] === 'feed'));
});
test('failed and malformed checks report an error and can be retried', async () => {
  const f = fixture(); f.response({ ok: false }); await f.updates.check();
  assert.equal(f.updates.state().status, 'error');
  f.response({ ok: true, json: async () => ({ message: 'not a list' }) }); await f.updates.check();
  assert.equal(f.updates.state().status, 'error');
  f.response({ ok: true, json: async () => [] }); await f.updates.check();
  assert.equal(f.updates.state().status, 'current');
});
test('rapid checks share one request', async () => {
  let finish, count = 0;
  const f = fixture({ fetch: url => { count++; return url === MANIFEST ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ ok: true, json: async () => [] }); } });
  const first = f.updates.check(), second = f.updates.check();
  assert.equal(first, second); assert.equal(count, 1);
  finish({ ok: false }); await first; assert.equal(count, 2);
  assert.equal(f.updates.state().status, 'current');
});
test('Store and development builds never contact GitHub', async () => {
  for (const [options, status] of [[{ windowsStore: true }, 'store'], [{ packaged: false }, 'development']]) {
    const f = fixture(options); await f.updates.check(); await f.updates.download();
    assert.equal(f.updates.state().status, status); assert.deepEqual(f.calls, []);
  }
});
test('supported updater waits for Download then reports progress and ready on quit', async () => {
  const f = fixture({ platform: 'linux', appImage: true }); f.response({ ok: true, json: async () => [release('0.2.0')] });
  await f.updates.check(); assert.equal(f.updates.state().status, 'available');
  assert.equal(f.client.autoDownload, false); assert.equal(f.client.autoInstallOnAppQuit, true);
  assert.equal(f.client.channel, 'desktop-arm64');
  assert.equal(f.calls.find(c => c[0] === 'feed')[1].url, 'https://github.com/Kamest/Piper/releases/download/desktop-v0.2.0/');
  await f.updates.download();
  assert.deepEqual(f.changes.slice(-3).map(s => [s.status, s.progress]), [['downloading', 0], ['downloading', 40], ['ready', 100]]);
  await f.updates.check(); assert.equal(f.updates.state().status, 'ready');
});
test('updater errors stay visible and retry reuses listeners', async () => {
  const f = fixture({ platform: 'win32' }); f.response({ ok: true, json: async () => [release('0.2.0')] });
  await f.updates.check(); f.client.emit('error', Error('private URL'));
  assert.equal(f.updates.state().status, 'error'); assert.ok(!JSON.stringify(f.updates.state()).includes('private'));
  await f.updates.check(); assert.equal(f.updates.state().status, 'available');
  assert.equal(f.client.listenerCount('error'), 1); assert.equal(f.client.listenerCount('update-downloaded'), 1);
  f.client.downloadUpdate = async () => { throw Error('failed'); };
  await f.updates.download(); assert.equal(f.updates.state().status, 'error');
  assert.equal(f.updates.state().errorStage, 'download');
  f.client.downloadUpdate = async () => f.client.emit('update-downloaded');
  await f.updates.download(); assert.equal(f.updates.state().status, 'ready');
});
test('a mismatched feed is an error instead of announcing an update', async () => {
  const f = fixture({ platform: 'linux', appImage: true }); f.response({ ok: true, json: async () => [release('0.2.0')] });
  f.client.checkForUpdates = async () => ({ updateInfo: { version: '0.1.8' } });
  await f.updates.check(); assert.equal(f.updates.state().status, 'error');
});

test('only a completed explicit download can request installation; late events cannot hide an error', async () => {
  const f = fixture({ platform: 'linux', appImage: true }); let installed = 0;
  f.client.quitAndInstall = (...args) => { assert.deepEqual(args, [false, true]); installed++; };
  f.updates.install(); assert.equal(installed, 0);
  f.response({ ok: true, json: async () => [release('0.2.0')] });
  f.client.checkForUpdates = async () => { f.client.emit('error', Error('check failed')); return { updateInfo: { version: '0.2.0' } }; };
  await f.updates.check(); assert.equal(f.updates.state().status, 'error');
  f.client.emit('download-progress', { percent: 99 }); f.client.emit('update-downloaded');
  assert.equal(f.updates.state().status, 'error'); f.updates.install(); assert.equal(installed, 0);
  f.client.checkForUpdates = async () => ({ updateInfo: { version: '0.2.0' } });
  await f.updates.check(); f.updates.install(); assert.equal(installed, 0);
  await f.updates.download(); f.updates.install(); assert.equal(installed, 1);
});

const prompt = require('../../desktop/src/lib/updateprompt.cjs');
test('the update prompt: tray items and one notification per desktop version', () => {
  assert.deepEqual(prompt.desktopItems({ status: 'current', latest: '' }), []);
  assert.deepEqual(prompt.desktopItems({ status: 'error', latest: '0.2.0' }), []);
  assert.deepEqual(prompt.desktopItems({ status: 'available', latest: '0.2.0' }), [{ action: 'download', label: 'Update Piper Desktop to 0.2.0…' }]);
  assert.deepEqual(prompt.desktopItems({ status: 'ready', latest: '0.2.0' }), [{ action: 'install', label: 'Restart to Update Piper Desktop 0.2.0' }]);
  const seen = new Set();
  assert.equal(prompt.desktopNotice({ status: 'checking', latest: '0.2.0' }, seen), null);
  const note = prompt.desktopNotice({ status: 'available', latest: '0.2.0', method: 'install-on-quit' }, seen);
  assert.equal(note.title, 'Piper Desktop 0.2.0 is available');
  assert.match(note.body, /installs when Piper Desktop restarts/);
  assert.equal(prompt.desktopNotice({ status: 'available', latest: '0.2.0' }, seen), null, 'a later check repeats nothing');
  assert.match(prompt.desktopNotice({ status: 'available', latest: '0.3.0', method: 'download' }, seen).body, /release page/);
  assert.ok(prompt.CHECK_EVERY >= 60 * 60 * 1000, 'background checks stay rare');
});
test('Machine › Update Piper names the version, and a pinned checkout keeps it disabled with the reason', () => {
  assert.deepEqual(prompt.coreItem(null), { label: 'Update Piper', enabled: true }, 'an older service reports nothing');
  assert.deepEqual(prompt.coreItem({ available: '0.2.0', updatable: true, reason: '' }), { label: 'Update Piper to 0.2.0', enabled: true });
  assert.deepEqual(prompt.coreItem({ available: '', updatable: false, reason: 'pinned' }), { label: 'Update Piper (version set by its installer)', enabled: false });
  assert.deepEqual(prompt.coreItem({ available: '', updatable: false, reason: 'embedded' }), { label: 'Update Piper (version set by its installer)', enabled: false },
    'Piper inside another repository');
  assert.deepEqual(prompt.coreItem({ available: '', updatable: false, reason: 'modified' }), { label: 'Update Piper', enabled: true },
    'own changes: piper update explains why it cannot pull');
});
