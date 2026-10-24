import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const {setupController}=require('../src/lib/setup.cjs');

function fixture({readyFails=false}={}) {
  const calls=[],writes=[];let readyCount=0,loaded=false;
  const extension={path:'/projects/tools/ext',label:'Team tools',api:1,source:'registered',compatible:true,trusted:false};
  const fs={access:async()=>{},mkdir:async()=>{},cp:async()=>{},writeFile:async(...a)=>writes.push(a),appendFile:async(...a)=>writes.push(a),readFile:async()=>'{}',rm:async()=>{}};
  const run=async(cmd,args,opts)=>{
    calls.push([cmd,...args]);
    if(args.includes('--json'))return {stdout:JSON.stringify([extension]),code:0};
    if(args.includes('--service'))opts?.onLine('Piper service started');
    return {stdout:'',code:0};
  };
  const c=setupController({env:{PIPER_DESKTOP_INSTALL_SH:'/test/install.sh'},platform:'linux',arch:'x64',home:{native:'/home/test'},userData:'/app',resources:'/resources',fs,run,changed:()=>{},
    service:{locate:async()=>null,command:async args=>{calls.push(['piper',...args]);if(args[0]==='extensions'&&args[1]==='--json')return {stdout:JSON.stringify([{...extension,trusted:loaded}])};if(args[1]==='untrust')loaded=false;if(args[1]==='trust')loaded=true;return {stdout:''};},ready:async()=>{if(readyFails&&readyCount++===0)throw Error('service did not answer');}},openExternal:async()=>{},showLog:async()=>{}});
  return {c,calls,writes};
}
test('checking never invokes install, service, trust, or privilege commands',async()=>{
  const {c,calls}=fixture();await c.check();assert.equal(c.wizard.state.screen,'check');
  assert.ok(!calls.some(a=>a.includes('--service')||a.includes('trust')||a[0]==='pkexec'));
});
test('checks use stable bundled Python even when a fake installer is supplied',async()=>{
  const {c,calls}=fixture();const paths=await c.prepare();assert.equal(paths.python,'/app/runtime/python-bundled/bin/python3');
  await c.check();assert.ok(calls.some(a=>a[0]===paths.python&&a[1]==='-c'));
});
test('installer waits for explicit D2 consent; output is saved and streamed',async()=>{
  const {c,calls,writes}=fixture();await c.check();await c.action('continue');assert.equal(c.wizard.state.screen,'extension');assert.ok(!calls.some(a=>a.includes('--service')));
  await c.action('next');assert.ok(calls.some(a=>a.includes('--extension')&&a.includes('/projects/tools/ext')));assert.ok(writes.some(a=>a[0]==='/app/setup.log'));
});
test('retry after a successful installer retries service readiness without reinstalling',async()=>{
  const {c,calls}=fixture({readyFails:true});await c.check();await c.action('continue');await c.action('next');assert.equal(c.wizard.state.screen,'error');await c.action('retry');assert.equal(calls.filter(a=>a.includes('--service')).length,1);
});
test('extension management never trusts a caller-supplied arbitrary path',async()=>{
  const {c,calls}=fixture();await c.extensions();await c.action('load','/foreign/path');assert.equal(c.wizard.state.screen,'error');assert.ok(!calls.some(a=>a.includes('trust')));
});
test('D8 consent loads an extension then restarts only piperd',async()=>{
  const {c,calls}=fixture();await c.extensions();await c.action('load','/projects/tools/ext');await c.action('choice','load');await c.action('next');
  assert.ok(calls.some(a=>a[0]==='piper'&&a[1]==='extensions'&&a[2]==='trust'));assert.ok(calls.some(a=>a[1]==='restart'));assert.ok(!calls.some(a=>a.includes('stop')));
});
test('D8 Not now does not untrust another extension or restart the service',async()=>{
  const {c,calls}=fixture();await c.extensions();await c.action('load','/projects/tools/ext');await c.action('choice','skip');await c.action('next');
  assert.equal(c.wizard.state.screen,'extensions');assert.ok(!calls.some(a=>a.includes('trust')||a.includes('untrust')||a.includes('restart')));
});
