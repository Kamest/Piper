import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { locateCLI, service, offerable, extensionState } = require('../src/lib/service.cjs');
const token = 'fake-only-token-1234567890';
const url = `http://127.0.0.1:18995/#token=${token}`;

test('CLI priority: explicit override, user CLI, checkout; missing explicit override never falls back', async () => {
  const seen=[];
  const opts={env:{},home:'/test/home',installDir:'/test/piper',platform:'linux',exists:async p=>{seen.push(p);return p.endsWith('scripts/piper.sh');}};
  assert.equal(await locateCLI(opts),'/test/piper/scripts/piper.sh');
  assert.deepEqual(seen,['/test/home/.local/bin/piper','/test/piper/scripts/piper.sh']);
  seen.length=0; assert.equal(await locateCLI({...opts,env:{PIPER_DESKTOP_CLI:'/test/override'}}),null); assert.deepEqual(seen,['/test/override']);
});
const ext=(over={})=>({path:'/projects/acme-tools',label:'Acme tools',api:2,compatible:true,trusted:false,source:'registered',origin_changed:false,...over});
function fake({online=false,cli='/test/piper',fail=false,found=[],declined='{}',api=true,downProbes=2}={}) {
  const calls=[],requests=[]; let up=online,down=0;
  const s=service({port:18995,platform:'linux',locate:async()=>cli,run:async(cmd,args)=>{
    calls.push(args);if(args[1]==='ensure')up=true;
    if(args[1]==='extensions'&&args[2]==='--json')return {stdout:JSON.stringify(found)};
    return {stdout:url};
  },fetch:async(full,opts)=>{
    if(!up||fail)throw Error('offline');
    const route=new URL(full).pathname;
    if(route==='/api/state'){
      if(down>0){down--;if(!down)up=true;throw Error('restarting');}
      return opts.headers?.Authorization ? {ok:true,json:async()=>({sessions:[],queue:{actions:[]}})} : {status:401,json:async()=>({error:'missing or unknown bearer token'})};
    }
    requests.push([opts.method,route,opts.body?JSON.parse(opts.body):null,opts.headers.Authorization]);
    if(!api)return {ok:false,status:404,json:async()=>({error:'not found'})};
    if(route==='/api/extensions')return {ok:true,status:200,json:async()=>({extensions:found.map(e=>({...e,state:e.trusted?'loaded':'found'})),offer:[],pending:[]})};
    if(route==='/api/restart'){down=downProbes;return {ok:true,status:202,json:async()=>({ok:true,restarting:true})};}
    return {ok:true,status:200,json:async()=>({ok:true,restart:true})};
  },readFile:async file=>file.endsWith('declined-extensions.json')?declined:token,tokenFile:'/fake/gui-token',declinedFile:'/fake/declined-extensions.json',delay:async()=>{}});
  return {s,calls,requests};
}
test('attaches before considering installation and never ensures an already-running service', async()=>{
  const {s,calls}=fake({online:true,found:[ext()]});assert.equal((await s.start()).state,'attach');assert.deepEqual(calls,[['/test/piper','open','--print']]);
});
test('uses ensure then print URL when offline and a CLI exists',async()=>{
  const {s,calls}=fake();assert.equal((await s.start()).state,'attach');assert.deepEqual(calls.map(c=>c[1]),['extensions','ensure','open']);
});
test('offline start offers a registered, untrusted extension before ensure; no terminal question is needed',async()=>{
  const {s,calls}=fake({found:[ext(),ext({path:'/deep/scan',source:'scan'}),ext({path:'/old',compatible:false})]});
  const r=await s.start();assert.equal(r.state,'offer');assert.deepEqual(r.extensions.map(e=>e.path),['/projects/acme-tools']);
  assert.ok(!calls.some(c=>c[1]==='ensure'));
});
test('no offer when declined, when one is already trusted, or when the core cannot record a decline',async()=>{
  const {origin_changed,...older}=ext();
  for(const opts of [{found:[ext()],declined:JSON.stringify({'/projects/acme-tools':'2026-11-01T00:00:00Z'})},
    {found:[ext(),ext({path:'/other',trusted:true})]},
    {found:[older]}]){
    const {s,calls}=fake(opts);assert.equal((await s.start()).state,'attach');assert.ok(calls.some(c=>c[1]==='ensure'));
  }
  // an unreadable declined file means nothing declined
  assert.equal((await fake({found:[ext()],declined:'not json'}).s.start()).state,'offer');
});
test('a failing extension list still starts the service',async()=>{
  const calls=[];let up=false;
  const s=service({port:18995,platform:'linux',locate:async()=>'/test/piper',run:async(cmd,args)=>{calls.push(args);if(args[1]==='extensions')throw Error('no python');if(args[1]==='ensure')up=true;return {stdout:url};},
    fetch:async()=>{if(!up)throw Error('offline');return {ok:true,json:async()=>({sessions:[]})};},readFile:async()=>token,tokenFile:'/t',delay:async()=>{}});
  assert.equal((await s.start()).state,'attach');assert.deepEqual(calls.map(c=>c[1]),['extensions','ensure','open']);
});
test('a declined offer is recorded through the started service, after ensure',async()=>{
  const {s,calls,requests}=fake({found:[ext()]});
  assert.equal(await s.ensure({decline:['/projects/acme-tools']}),url);
  assert.equal(calls[0][1],'ensure');
  assert.deepEqual(requests,[['POST','/api/extensions/decline',{path:'/projects/acme-tools'},'Bearer '+token]]);
  // an older service without the route: still attached, nothing thrown
  assert.equal(await fake({api:false}).s.ensure({decline:['/x']}),url);
});
test('extensions API: listed when piperd has it, null (CLI) when it does not or is down',async()=>{
  const {s}=fake({online:true,found:[ext({trusted:true})]});
  assert.equal((await s.extensionsAPI()).extensions[0].state,'loaded');
  assert.equal(await fake({online:true,api:false}).s.extensionsAPI(),null);
  assert.equal(await fake().s.extensionsAPI(),null);
  const html=service({port:18995,platform:'linux',locate:async()=>'/c',run:async()=>({stdout:url}),fetch:async(full)=>full.endsWith('/api/state')?{ok:true,json:async()=>({sessions:[]})}:{ok:true,json:async()=>{throw SyntaxError('html');}},readFile:async()=>token,tokenFile:'/t',delay:async()=>{}});
  assert.equal(await html.extensionsAPI(),null);
});
test('trust over the API, then restart waits for the old socket to go and the new one to answer',async()=>{
  const {s,requests}=fake({online:true,found:[ext()],downProbes:3});
  assert.deepEqual(await s.changeExtension('trust','/projects/acme-tools'),{ok:true,restart:true});
  await s.restart();
  assert.deepEqual(requests.map(r=>r[1]),['/api/extensions/trust','/api/restart']);
  assert.ok(await s.probe());
  await assert.rejects(s.changeExtension('approve','/x'),/Unknown/);
});
test('extension states for the window: API state wins, CLI lists are derived',()=>{
  assert.equal(extensionState({state:'failed',compatible:true,trusted:true}),'failed');
  assert.equal(extensionState({compatible:false}),'incompatible');
  assert.equal(extensionState({compatible:true,trusted:true,origin_changed:true}),'origin-changed');
  assert.equal(extensionState({compatible:true,trusted:true}),'loaded');
  assert.equal(extensionState({compatible:true,trusted:false}),'found');
  assert.deepEqual(offerable('nope'),[]);
});
test('first-run wizard is entered only without a service and CLI',async()=>{
  const {s,calls}=fake({cli:null});assert.deepEqual(await s.start(),{state:'wizard'});assert.deepEqual(calls,[]);
});
test('a surviving service without CLI attaches read-only using its existing token',async()=>{
  const {s,calls}=fake({online:true,cli:null});assert.equal((await s.start()).url,url);assert.deepEqual(calls,[]);
});
test('ensure timeout does not stop any service or session',async()=>{
  const {s,calls}=fake({fail:true});await assert.rejects(s.start(),/30 s/);assert.deepEqual(calls.map(c=>c[1]),['extensions','ensure']);
});
test('state polling authenticates and never puts token in a query string',async()=>{
  const {s}=fake({online:true});assert.deepEqual(await s.state(),{sessions:[],queue:{actions:[]}});
});
