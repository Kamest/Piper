import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

globalThis.window = { localStorage: { getItem: () => 'fixture', setItem() {}, removeItem() {} } };
const { hosts } = await import('../../piper/web/dist/environment.js');
const { refreshHost, refreshRemoteHosts } = await import('../../piper/web/dist/api.js');

test('slow remote polls share a request and notify once; unchanged polls do not redraw', async () => {
  hosts.clear();
  const host = { id: 'remote', mode: 'service', label: 'Remote', host: 'example.test' };
  hosts.set(host.id, { host, online: false, error: '', checkedAt: 0, etag: '', capabilitiesCheckedAt: Date.now() });
  let finish, calls = 0, changes = 0;
  globalThis.fetch = async () => { calls++; return await new Promise(resolve => { finish = resolve; }); };
  const changed = () => changes++;
  const requests = Array.from({length: 20}, () => refreshHost(host.id, changed));
  assert.equal(calls, 1);
  finish(new Response('{"sessions":[],"pending":[],"projects":[]}', {headers:{ETag:'"one"'}}));
  await Promise.all(requests);
  assert.equal(changes, 1, 'one completed request must not fan out into twenty renders');
  globalThis.fetch = async () => new Response(null, {status:304, headers:{ETag:'"one"'}});
  await refreshHost(host.id, changed);
  assert.equal(changes, 1, '304 keeps snapshot and online state unchanged');
  hosts.get(host.id).online = false;
  await refreshHost(host.id, changed);
  assert.equal(changes, 2, 'recovery must redraw even with the same snapshot');
});

test('stable environment discovery does not redraw, but changed host metadata does', async () => {
  const row = hosts.get('remote');
  row.attemptedAt = Date.now();
  let changes = 0;
  globalThis.fetch = async path => new Response(path === '/api/environments'
    ? JSON.stringify({environments:[row.host]}) : null, {status:path === '/api/environments' ? 200 : 304});
  refreshRemoteHosts(() => changes++, true);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(changes, 0);
  const next = {...row.host, label:'Renamed remote'};
  globalThis.fetch = async path => new Response(path === '/api/environments'
    ? JSON.stringify({environments:[next]}) : null, {status:path === '/api/environments' ? 200 : 304});
  refreshRemoteHosts(() => changes++, true);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(changes, 1);
});
