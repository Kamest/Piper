import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { parseServiceURL, redact, navigation, updateDecision, trayModel } = require('../src/lib/policy.cjs');
const token = 'fake-only-token-1234567890';

test('accepts only one tokened root URL on the configured loopback port', () => {
  assert.equal(parseServiceURL(`http://127.0.0.1:18995/#token=${token}`, 18995).token, token);
  for (const value of ['https://example.com/#token='+token, 'http://127.0.0.1:17895/#token='+token, 'http://127.0.0.1:18995/path#token='+token, 'http://user@127.0.0.1:18995/#token='+token, 'http://127.0.0.1:18995/', 'bad\nhttp://127.0.0.1:18995/#token='+token]) {
    assert.throws(() => parseServiceURL(value, 18995), error => !error.message.includes(token));
  }
});
test('redacts tokens, headers, keys and URL credentials before logging', () => {
  for (const value of [`http://127.0.0.1/#token=${token}`, `Bearer ${token}`, `TOKEN=${token}`, `password=${token}`, `https://user:${token}@example.com/`]) assert.ok(!redact(value).includes(token));
});
test('navigation permits own popouts, sends web links external and rejects executable schemes', () => {
  const origin = 'http://127.0.0.1:18995';
  assert.equal(navigation(origin+'/?popout=1',origin),'internal');
  assert.equal(navigation('https://example.com',origin),'external');
  for (const url of ['javascript:alert(1)','file:///etc/passwd','data:text/html,test','https://user:pass@example.com']) assert.equal(navigation(url,origin),'deny');
});
test('tray counts queue actions, caps labels, backs off offline and supports old API', () => {
  const state = {sessions:[{state:'attention'},{state:'ready'}],queue:{actions:[{title:'needs approval'},{kind:'reply'}]}};
  assert.deepEqual(trayModel(state), {online:true,sessions:2,count:2,items:['needs approval','reply'],poll:10000});
  assert.equal(trayModel(null,false).poll,30000);
  assert.equal(trayModel({sessions:state.sessions}).count,1);
});
test('only desktop releases update; unsigned macOS downloads, other systems install on quit', () => {
  const url = 'https://github.com/Kamest/Piper/releases/tag/desktop-v0.2.0';
  assert.equal(updateDecision('darwin',false,'desktop-v0.2.0',url),'download');
  assert.equal(updateDecision('darwin',true,'desktop-v0.2.0',url),'install-on-quit');
  assert.equal(updateDecision('win32',false,'desktop-v0.2.0',url),'install-on-quit');
  assert.equal(updateDecision('linux',false,'v0.2.0',url),'ignore');
  assert.equal(updateDecision('linux',false,'desktop-v0.2.0','https://example.com'),'ignore');
});
