// Host-side harness: every test process still runs through Piper's OS sandbox.
import { spawn } from 'node:child_process';
import { mkdir, readdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import readline from 'node:readline';
import { shotCollector } from './shots.mjs';
const desktop=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const repo=path.dirname(desktop);
const mode=process.argv[2]||'unit';
if(!['unit','e2e'].includes(mode))throw Error('Use unit or e2e');
const files=mode==='e2e'?['e2e.mjs']:(await readdir(path.join(desktop,'test'))).filter(n=>n.endsWith('.test.mjs'));
const child=spawn('python3',[path.join(repo,'scripts/piper-sandbox.py'),'--',process.execPath,'--test','--test-reporter=tap','--test-concurrency=1',...files.map(n=>path.join(desktop,'test',n))],{stdio:['inherit','pipe','inherit'],
  // e2e under xvfb-run: Linux bwrap shows the display only on request; Electron needs it
  env:{...process.env,...(mode==='e2e'?{PIPER_SANDBOX_X11:'1'}:{})}});
const writes=[];
const feed=shotCollector();
for await(const line of readline.createInterface({input:child.stdout})) {
  const result=feed(line);
  if(!result) console.log(line);
  else if(result.error) console.log('# '+result.error);
  else if(result.shot) {
    const dir=path.join(desktop,'test/shots'), {name,bytes}=result.shot;
    writes.push(mkdir(dir,{recursive:true}).then(()=>writeFile(path.join(dir,name+'.png'),bytes)));
  }
}
await Promise.all(writes);
const code=child.exitCode ?? await new Promise(resolve=>child.once('exit',resolve));
process.exitCode=code ?? 1;
