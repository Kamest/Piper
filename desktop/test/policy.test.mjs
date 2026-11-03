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
// 9.173: the GUI's "Open in IntelliJ" (Files, terminal links) was swallowed by the guard in the app
test('navigation hands only idea://open?file= to the OS, no other command of the scheme', () => {
  const origin = 'http://127.0.0.1:18995';
  assert.equal(navigation('idea://open?file=%2FUsers%2Fme%2Fshop%2FA.java&line=12',origin),'external');
  assert.equal(navigation('idea://open?file=/x',origin),'external');
  for (const url of ['idea://open','idea://open?line=1','idea://settings?file=/x','idea://user:pw@open?file=/x','idea://open?file=/x#frag','jetbrains://idea/open?file=/x','vscode://file/x'])
    assert.equal(navigation(url,origin),'deny',url);
});
test('tray counts queue actions, caps labels, backs off offline and supports old API', () => {
  const state = {sessions:[{state:'attention'},{state:'ready'}],queue:{actions:[{title:'needs approval'},{kind:'reply'}]}};
  const m = trayModel(state);
  assert.deepEqual({...m, entries:undefined, list:undefined}, {online:true,sessions:2,count:2,waiting:0,inbox:0,items:['needs approval','reply'],poll:5000,entries:undefined,list:undefined});
  assert.deepEqual(m.entries.map(e => [e.label, e.card]), [['needs approval',null],['reply',null]]);
  assert.equal(trayModel(null,false).poll,30000);
  assert.equal(trayModel({sessions:state.sessions}).count,1);
});
// 27 inbox items and 4 waiting sessions showed as "31 waiting for you": the badge counts only what blocks work
test('tray badge counts blocking actions; waiting sessions and the inbox are separate counts', () => {
  const act = (level, title) => ({level, title});
  const actions = [act('confirm','Build: wants to push'), act('permission','Docs asks for permission'), act('security','A sensitive file'),
    act('blocked','Start blocked'), ...Array(4).fill(act('waiting','x is waiting for you')),
    ...Array(25).fill(act('gitlab','Assigned: issue')), act('ci','CI failed'), act('prod','Production: 2 new errors')];
  const m = trayModel({sessions:[{state:'waiting'}],queue:{actions}});
  assert.deepEqual([m.count, m.waiting, m.inbox], [4, 4, 27]);
  assert.deepEqual(m.items, ['Build: wants to push','Docs asks for permission','A sensitive file','Start blocked']);
  // an old piperd without the queue: cards and attention/trust/blocked sessions block, waiting ones wait
  const old = trayModel({sessions:[{state:'waiting'},{state:'waiting'},{state:'trust'},{state:'blocked'},{state:'attention'},{state:'ready'}],pending:[{title:'Build',kind:'git_push'}]});
  assert.deepEqual([old.count, old.waiting, old.inbox], [4, 2, 0]);
});
test('only desktop releases update; unsigned macOS downloads, other systems install on quit', () => {
  const url = 'https://github.com/Kamest/Piper/releases/tag/desktop-v0.2.0';
  assert.equal(updateDecision('darwin',false,'desktop-v0.2.0',url),'download');
  assert.equal(updateDecision('darwin',true,'desktop-v0.2.0',url),'install-on-quit');
  assert.equal(updateDecision('win32',false,'desktop-v0.2.0',url),'install-on-quit');
  assert.equal(updateDecision('linux',false,'v0.2.0',url),'ignore');
  assert.equal(updateDecision('linux',false,'desktop-v0.2.0','https://example.com'),'ignore');
});
