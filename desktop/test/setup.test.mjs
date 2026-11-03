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

// Extension API 2: several extensions may be loaded; piperd's API is used while it runs.
function multi({api=true,startFails=0}={}) {
  const calls=[];let starts=0;
  const list=[{path:'/projects/acme-a',label:'Acme A',api:2,source:'registered',compatible:true,trusted:true,origin_changed:false},
    {path:'/projects/acme-b',label:'Acme B',api:2,source:'registered',compatible:true,trusted:false,origin_changed:false},
    {path:'/projects/acme-c',label:'Acme C',api:2,source:'checkout',compatible:true,trusted:false,origin_changed:false}];
  const state=e=>({...e,state:e.trusted?'loaded':'found'});
  const service={locate:async()=>'/c',ready:async()=>{},
    command:async args=>{calls.push(['cli',...args]);if(args[1]==='--json')return {stdout:JSON.stringify(list)};if(args[1]==='trust')list.find(e=>e.path===args[2]).trusted=true;if(args[1]==='untrust')list.find(e=>e.path===args[2]).trusted=false;return {stdout:''};},
    extensionsAPI:async()=>{calls.push(['api','list']);return api?{extensions:list.map(state),offer:[]}:null;},
    changeExtension:async(action,p)=>{calls.push(['api',action,p]);list.find(e=>e.path===p).trusted=action==='trust';return {ok:true};},
    restart:async()=>{calls.push(['api','restart']);},
    start:async decline=>{calls.push(['start',decline]);if(starts++<startFails)throw Error('service did not answer');}};
  const c=setupController({env:{},platform:'linux',arch:'x64',home:{native:'/home/test'},userData:'/app',resources:'/resources',fs:{},run:async()=>({stdout:'',code:0}),changed:()=>{},service,openExternal:async()=>{},showLog:async()=>{}});
  return {c,calls,list};
}
test('Extensions window over the API: loading a second one keeps the first, restarts piperd once',async()=>{
  const {c,calls,list}=multi();await c.extensions();
  assert.deepEqual(c.wizard.state.extensions.map(e=>e.state),['loaded','found','found']);
  await c.action('load','/projects/acme-b');await c.action('choice','load');await c.action('next');
  assert.ok(list[0].trusted&&list[1].trusted);
  assert.deepEqual(calls.filter(a=>a[0]==='api'&&a[1]!=='list'),[['api','trust','/projects/acme-b'],['api','restart']]);
  assert.ok(!calls.some(a=>a[0]==='cli'));
  await c.action('untrust','/projects/acme-a');assert.deepEqual(calls.filter(a=>a[1]==='untrust'),[['api','untrust','/projects/acme-a']]);
  assert.equal(c.wizard.state.screen,'extensions');assert.equal(c.wizard.state.extensions[0].state,'found');
});
test('Extensions window without the API (service down or older) uses the CLI and keeps other trusted ones',async()=>{
  const {c,calls,list}=multi({api:false});await c.extensions();
  await c.action('load','/projects/acme-b');await c.action('choice','load');await c.action('next');
  assert.ok(list[0].trusted&&list[1].trusted);
  assert.ok(!calls.some(a=>a[1]==='untrust'));assert.ok(calls.some(a=>a[0]==='cli'&&a[1]==='restart'));
  assert.ok(!calls.some(a=>a[0]==='api'&&a[1]!=='list'));
});
test('start offer: each extension asked, several loaded, the rest declined after the service starts',async()=>{
  const {c,calls,list}=multi();list[0].trusted=false;
  c.offer(list.slice(1));assert.equal(c.wizard.state.screen,'extension');assert.equal(c.wizard.state.offer,true);
  assert.equal(c.wizard.state.choices['/projects/acme-b'],'load');assert.equal(c.wizard.state.choices['/projects/acme-c'],'skip');
  await c.action('next');assert.equal(c.wizard.state.index,1);assert.ok(!calls.length);
  await c.action('back');assert.equal(c.wizard.state.index,0);await c.action('next');
  await c.action('next');
  assert.deepEqual(calls,[['cli','extensions','trust','/projects/acme-b'],['start',['/projects/acme-c']]]);
  assert.equal(c.wizard.state.offer,false);
});
test('start offer: Load on both trusts both; a failed start retries without asking again',async()=>{
  const {c,calls}=multi({startFails:1});
  c.offer([{path:'/projects/acme-b',label:'B',api:2,source:'registered',compatible:true,trusted:false},{path:'/projects/acme-c',label:'C',api:2,source:'checkout',compatible:true,trusted:false}]);
  await c.action('next');await c.action('choice','load');await c.action('next');
  assert.equal(c.wizard.state.screen,'error');
  await c.action('retry');
  assert.deepEqual(calls.filter(a=>a[0]==='start'),[['start',[]],['start',[]]]);
  assert.deepEqual(calls.filter(a=>a[2]==='trust').map(a=>a[3]),['/projects/acme-b','/projects/acme-c','/projects/acme-b','/projects/acme-c']);
});
