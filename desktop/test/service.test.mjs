import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { locateCLI, service } = require('../src/lib/service.cjs');
const token = 'fake-only-token-1234567890';
const url = `http://127.0.0.1:18995/#token=${token}`;

test('CLI priority: explicit override, user CLI, checkout; missing explicit override never falls back', async () => {
  const seen=[];
  const opts={env:{},home:'/test/home',installDir:'/test/piper',platform:'linux',exists:async p=>{seen.push(p);return p.endsWith('scripts/piper.sh');}};
  assert.equal(await locateCLI(opts),'/test/piper/scripts/piper.sh');
  assert.deepEqual(seen,['/test/home/.local/bin/piper','/test/piper/scripts/piper.sh']);
  seen.length=0; assert.equal(await locateCLI({...opts,env:{PIPER_DESKTOP_CLI:'/test/override'}}),null); assert.deepEqual(seen,['/test/override']);
});
function fake({online=false,cli='/test/piper',fail=false}={}) {
  const calls=[]; let up=online;
  const s=service({port:18995,platform:'linux',locate:async()=>cli,run:async(cmd,args)=>{calls.push(args);if(args[1]==='ensure')up=true; return {stdout:url};},fetch:async(_url,opts)=>{
    if(!up||fail)throw Error('offline');
    return opts.headers?.Authorization ? {ok:true,json:async()=>({sessions:[],queue:{actions:[]}})} : {status:401,json:async()=>({error:'missing or unknown bearer token'})};
  },readFile:async()=>token,tokenFile:'/fake/gui-token',delay:async()=>{}});
  return {s,calls};
}
test('attaches before considering installation and never ensures an already-running service', async()=>{
  const {s,calls}=fake({online:true});assert.equal((await s.start()).state,'attach');assert.deepEqual(calls,[['/test/piper','open','--print']]);
});
test('uses ensure then print URL when offline and a CLI exists',async()=>{
  const {s,calls}=fake();assert.equal((await s.start()).state,'attach');assert.equal(calls[0][1],'ensure');assert.equal(calls[1][1],'open');
});
test('first-run wizard is entered only without a service and CLI',async()=>{
  const {s,calls}=fake({cli:null});assert.deepEqual(await s.start(),{state:'wizard'});assert.deepEqual(calls,[]);
});
test('a surviving service without CLI attaches read-only using its existing token',async()=>{
  const {s,calls}=fake({online:true,cli:null});assert.equal((await s.start()).url,url);assert.deepEqual(calls,[]);
});
test('ensure timeout does not stop any service or session',async()=>{
  const {s,calls}=fake({fail:true});await assert.rejects(s.start(),/30 s/);assert.equal(calls.length,1);
});
test('state polling authenticates and never puts token in a query string',async()=>{
  const {s}=fake({online:true});assert.deepEqual(await s.state(),{sessions:[],queue:{actions:[]}});
});
