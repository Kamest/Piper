import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require=createRequire(import.meta.url);
const { Wizard, checkComputer, install }=require('../src/lib/wizard.cjs');
const ext=(source='registered')=>({path:'/test/ext',label:'Team tools',api:1,compatible:true,trusted:false,source});

test('D1 cannot continue while checking or with blocking prerequisites',()=>{
  const w=new Wizard();w.continue();assert.equal(w.state.screen,'check');
  w.checked([{id:'git',status:'problem'}],[]);w.continue();assert.equal(w.state.screen,'check');
});
test('D1 rows cover running, ok, install and problem; D4 runtime problem is separate',async()=>{
  const snapshots=[];
  const run=async(cmd,args)=>({code:cmd==='docker'||cmd==='podman'||cmd==='hole'?1:0,stdout:args.includes('--json')?'[]':''});
  const result=await checkComputer({run,platform:'darwin',python:'python3',extfind:'/fake/extfind.py',installDir:'/test/piper',onRow:r=>snapshots.push(structuredClone(r))});
  assert.deepEqual(result.rows.map(r=>r.status),['problem','ok','ok','install','install','ok']);
  assert.ok(snapshots.some(r=>r.some(x=>x.status==='running')));
  const w=new Wizard();w.checked(result.rows,[]);w.continue();assert.equal(w.state.screen,'runtime');
});
test('D4b appears for missing WSL 2 without executing Linux checks',async()=>{
  const r=await checkComputer({platform:'win32',distro:'',run:()=>{throw Error('must not run');}});
  const w=new Wizard();w.checked(r.rows,[]);w.continue();assert.equal(w.state.screen,'wsl');
});
test('macOS git probing does not invoke the installer stub before consent',async()=>{
  const calls=[];
  const run=async(cmd,args)=>{calls.push([cmd,...args]);return {code:cmd==='xcode-select'?1:0,stdout:cmd==='sh'?'/usr/bin/git\n':args.includes('--json')?'[]':''};};
  const {rows}=await checkComputer({run,platform:'darwin',python:'python3',extfind:'/fake/extfind.py',installDir:'/test/piper'});
  assert.equal(rows.find(r=>r.id==='git').status,'problem');assert.ok(!calls.some(c=>c[0]==='git'));assert.ok(!calls.some(c=>c.includes('--install')));
});
test('D2 registered preselects load, scan preselects skip; incompatible/trusted skip consent',()=>{
  for(const [source,choice] of [['registered','load'],['scan','skip']]){
    const w=new Wizard();w.checked([], [ext(source)]);w.continue();assert.equal(w.state.screen,'extension');assert.equal(w.state.choices['/test/ext'],choice);w.choose('load');w.next();assert.equal(w.selected(),'/test/ext');assert.equal(w.state.screen,'install');
  }
  const w=new Wizard();w.checked([], [{...ext(),compatible:false},{...ext(),path:'/trusted',trusted:true}]);w.continue();assert.equal(w.state.screen,'install');
});
test('D2 makes exactly one extension selected',()=>{
  const w=new Wizard();w.checked([], [ext(),{...ext(),path:'/second'}]);w.continue();w.choose('load');w.next();w.choose('load');assert.equal(w.selected(),'/second');
});
test('D3 streams redacted output; D5 preserves earlier logs and retries idempotently',async()=>{
  const w=new Wizard();const logs=[];
  await install({run:async(cmd,args,opts)=>{assert.equal(cmd,'sh');assert.ok(args.includes('--no-extensions'));opts.onLine('hole ready');opts.onLine('url #token=fake-only-secret');return {code:0};},platform:'linux',installer:'/test/install.sh',env:{},onLine:l=>{w.line(l);logs.push(l);}});
  w.fail(Error('failed service'));assert.equal(w.state.screen,'error');w.retry();assert.equal(w.state.screen,'install');assert.equal(w.state.log.length,2);assert.ok(!w.state.log.join('').includes('fake-only-secret'));
});
test('WSL installer wraps bundled script in the selected distro with preserved env',async()=>{
  let seen;await install({run:async(...args)=>{seen=args;},platform:'win32',distro:'Ubuntu',installer:'/test/install.sh',env:{},serviceEnv:{PIPER_PORT:'18995'},onLine:()=>{}});
  assert.equal(seen[0],'wsl.exe');assert.ok(seen[1].includes('Ubuntu'));assert.match(seen[1].at(-1),/18995/);
});
