import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { wsl, wslRows, wslBootstrap, podmanCommand, installArgs, linuxAutostart } = require('../src/lib/platform.cjs');

test('WSL selects only version 2 and tolerates UTF-16 output', () => {
  const text = '  NAME            STATE           VERSION\r\n* Ubuntu          Running         2\r\n  Debian          Stopped         1';
  assert.deepEqual(wslRows(text.split('').join('\0')), [{name:'Ubuntu',version:2},{name:'Debian',version:1}]);
});
test('WSL preserves spaces, quotes and shell metacharacters as data', () => {
  const [cmd,args] = wsl('sh',["/home/test/a ' b.sh",'--extension','/tmp/$(danger)'],'Ubuntu',{PIPER_HOME:'/home/test/state space',PIPER_PORT:'18995'});
  assert.equal(cmd,'wsl.exe'); assert.deepEqual(args.slice(0,5),['-d','Ubuntu','--exec','sh','-c']);
  assert.ok(args.at(-1).includes("'/tmp/$(danger)'")); assert.ok(args.at(-1).includes("'\\''"));
});
test('WSL bootstrap uses UAC and a per-user one-shot resume, never forces reboot', () => {
  const [cmd,args] = wslBootstrap('C:\\Program Files\\Piper\\Piper.exe');
  assert.equal(cmd,'powershell.exe'); assert.match(args.at(-1),/RunOnce/); assert.match(args.at(-1),/-Verb RunAs/); assert.match(args.at(-1),/--no-distribution/); assert.doesNotMatch(args.at(-1),/Restart-Computer|shutdown/);
});
test('Podman commands are explicitly platform-specific and require known package managers', () => {
  assert.equal(podmanCommand('darwin',{packagePath:'/tmp/podman.pkg'})[0],'osascript');
  assert.throws(()=>podmanCommand('darwin'));
  assert.deepEqual(podmanCommand('linux',{manager:'apt'}),['pkexec',['apt-get','install','-y','podman','uidmap']]);
  assert.throws(()=>podmanCommand('linux',{manager:'unknown'}));
  assert.deepEqual(podmanCommand('win32',{distro:'Ubuntu'})[1].slice(0,6),['-d','Ubuntu','-u','root','--exec','sh']);
});
test('installer receives explicit consent, service flags and a neutral project checkout', () => {
  assert.deepEqual(installArgs({PROJECT_PATH:'/work/projects'},'/work/team/ext'),['--yes','--no-open','--service','--extension','/work/team/ext','--dir','/work/projects/piper']);
  assert.deepEqual(installArgs({},null),['--yes','--no-open','--service','--no-extensions']);
});
test('Linux autostart quotes the executable and rejects line injection', () => {
  assert.match(linuxAutostart('/opt/Piper App/Piper'),/Exec="\/opt\/Piper App\/Piper"/);
  assert.throws(()=>linuxAutostart('/tmp/a\nHidden=true'));
});
