// Fake-only CLI/installer used by the sandboxed Electron tests.
import { readFile, writeFile, appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
const root = process.env.PIPER_E2E_ROOT;
if (!root || !process.env.TEST_SANDBOX || process.env.PIPER_PORT === '17895') process.exit(3);
const [command, ...args] = process.argv.slice(2);
const file = name => path.join(root, name);
const exists = async name => { try { await readFile(file(name)); return true; } catch { return false; } };
await appendFile(file('calls.jsonl'), JSON.stringify([command,...args])+'\n');
// A `core2` marker plays a current core: its list carries origin_changed and declined, and it has `decline`.
const extensions = async () => {
  const trusted = await exists('trusted');
  const current = await exists('core2') ? {origin_changed:false,declined:await exists('declined')} : {};
  return [{name:'team',label:'Team dev tools',path:file('projects/tools/ext'),api:1,compatible:true,trusted,source:'registered',remote:'https://git.example.com/acme/tools.git',commit:'123abcd',...current}];
};
switch (command) {
  case 'install':
    console.log('install: hole installed');
    console.log('install: cloning Piper');
    await new Promise(r=>setTimeout(r,300));
    if(await exists('fail')) { await writeFile(file('failed'),'1'); console.error('service failed'); process.exit(2); }
    await mkdir(path.dirname(process.env.PIPER_DESKTOP_CLI),{recursive:true});
    await writeFile(process.env.PIPER_DESKTOP_CLI, `#!/bin/sh\nexec '${process.execPath.replace(/'/g,"'\\''")}' '${new URL(import.meta.url).pathname.replace(/'/g,"'\\''")}' "$@"\n`,{mode:0o755});
    if(args.includes('--extension'))await writeFile(file('trusted'),'1');
    console.log('install: Piper service starting');
    console.log('install: Doctor OK');
    await new Promise(r=>setTimeout(r,800));
    await writeFile(file('online'),'1');break;
  case 'open': console.log(`http://127.0.0.1:${process.env.PIPER_PORT}/#token=fake-only-token-1234567890`);break;
  case 'ensure': await writeFile(file('online'),'1');break;
  case 'restart': await writeFile(file('online'),'1');await writeFile(file('restarted'),'-'+Date.now());break;
  // 9.173 the tray's Machine menu: `update` pulls (an `update-refused` marker plays local changes), status, doctor
  case 'update':
    if(await exists('update-refused')) { console.error('piper: git pull --ff-only failed in /x (local changes or another branch?) - nothing was changed; update it yourself'); process.exit(1); }
    console.log(await exists('updated') ? 'Piper is up to date (def5678)' : 'Piper updated abc1234 -> def5678');
    await writeFile(file('updated'),'1');break;
  case 'status': console.log(`piperd answers on http://127.0.0.1:${process.env.PIPER_PORT} - no login service`);break;
  case 'doctor': console.log('ok   git\nok   docker running\nwarn token=fake-only-token-1234567890 should never leak');process.exit(1);
  case 'extensions':
    if(args[0]==='trust')await writeFile(file('trusted'),'1');
    else if(args[0]==='untrust') { const {rm}=await import('node:fs/promises');await rm(file('trusted'),{force:true}); }
    else if(args[0]==='decline') {
      // an older core's argparse: an unknown command is a usage error
      if(!await exists('core2')) { console.error("extfind.py: error: argument command: invalid choice: 'decline'"); process.exit(2); }
      await writeFile(file('declined'),args[1]);
    }
    else console.log(JSON.stringify(await extensions()));break;
  default: process.exit(2);
}
