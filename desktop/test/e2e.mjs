import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { _electron } from 'playwright-core';
import { mkdtemp, mkdir, writeFile, readFile, rm, access } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import http from 'node:http';
import net from 'node:net';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { shotLine } from './shots.mjs';
const require=createRequire(import.meta.url);
const desktop=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const electron=require('electron');
const quote=s=>"'"+s.replace(/'/g,"'\\''")+"'";
const exists=async p=>{try{await access(p);return true;}catch{return false;}};
// The sandbox checkout is read-only. Keep screenshots in WORK and copy them out after the run.
const shotDir=process.env.PIPER_E2E_SHOTS || path.join(process.env.TEST_SANDBOX_WORK,'desktop-shots');
await mkdir(shotDir,{recursive:true});

async function fixture(t,{runtime=true,fail=false,installed=false,platform=process.platform}={}) {
  const root=await mkdtemp(path.join(os.tmpdir(),'piper-desktop-'));
  const dirs=['home','state','app','bin','projects/tools/ext','home/.config/piper/extensions.d','cli'];
  for(const d of dirs)await mkdir(path.join(root,d),{recursive:true});
  const f=name=>path.join(root,name);
  const extension=f('projects/tools/ext');
  await writeFile(path.join(extension,'piper-extension.json'),JSON.stringify({name:'team',label:'Team dev tools',api:1,wrapper:'piper.sh'}));
  await writeFile(path.join(extension,'piper.sh'),'#!/bin/sh\nexit 0\n',{mode:0o755});
  await writeFile(f('home/.config/piper/extensions.d/team.json'),JSON.stringify({path:extension}));
  const cliScript=path.join(desktop,'test/fixtures/cli.mjs');
  await writeFile(f('install.sh'),`#!/bin/sh\nexec ${quote(process.execPath)} ${quote(cliScript)} install "$@"\n`,{mode:0o755});
  for(const name of ['docker','podman'])await writeFile(f('bin/'+name),`#!/bin/sh\n${runtime?'exit 0':'exit 1'}\n`,{mode:0o755});
  if(fail)await writeFile(f('fail'),'1');
  const stub=`<!doctype html><html><head><title>Piper</title><meta charset="utf-8"></head><body><h1>Piper fake service</h1><button id="notify">Notify</button><button id="popout">Pop out</button><div id="selected"></div><script>
    document.querySelector('#notify').onclick=()=>{const n=new Notification('Session waits',{body:'Waiting for you',tag:'session-1'});n.onclick=()=>{document.querySelector('#selected').textContent='session-1';window.focus();};};
    document.querySelector('#popout').onclick=()=>window.open('/?popout=session-1','piper-session');
  </script></body></html>`;
  const server=http.createServer(async(req,res)=>{
    if(!await exists(f('online'))){res.writeHead(503);res.end('offline');return;}
    if(req.url==='/api/state'){
      res.setHeader('Content-Type','application/json');
      if(req.headers.authorization!=='Bearer fake-only-token-1234567890'){res.writeHead(401);res.end(JSON.stringify({error:'missing or unknown bearer token'}));}
      else res.end(JSON.stringify({sessions:[{id:'session-1',state:'attention'}],queue:{actions:[{title:'Session waits'}]}}));return;
    }
    res.setHeader('Content-Type','text/html');res.end(stub);
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const port=server.address().port;assert.notEqual(port,17895);
  const env={...process.env,HOME:f('home'),XDG_CONFIG_HOME:f('home/.config'),PROJECT_PATH:f('projects'),PIPER_HOME:f('state'),PIPER_PORT:String(port),
    PIPER_DESKTOP_HOME:f('app'),PIPER_DESKTOP_CLI:f('cli/piper'),PIPER_DESKTOP_INSTALL_SH:f('install.sh'),PIPER_DESKTOP_NO_CHROMIUM_SANDBOX:'1',
    PIPER_E2E_ROOT:root,PATH:f('bin')+path.delimiter+process.env.PATH};
  env.PIPER_DESKTOP_PLATFORM=platform;
  // macOS Chromium ignores TMPDIR (its singleton socket lands in /var/folders, outside the test sandbox)
  env.MAC_CHROMIUM_TMPDIR=os.tmpdir();
  delete env.ELECTRON_RUN_AS_NODE;
  if(installed){await writeFile(env.PIPER_DESKTOP_CLI,`#!/bin/sh\nexec ${quote(process.execPath)} ${quote(cliScript)} "$@"\n`,{mode:0o755});await writeFile(f('online'),'1');}
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const cdp=probe.address().port;await new Promise(r=>probe.close(r));
  const app=await _electron.launch({executablePath:electron,args:[desktop,`--remote-debugging-port=${cdp}`],env,timeout:30000});
  // a hung quit must fail the test, not the run: bounded close, then SIGKILL of the app's own process
  t.after(async()=>{await Promise.race([app.close().catch(()=>{}),new Promise(r=>setTimeout(r,5000))]);try{app.process().kill('SIGKILL');}catch{}server.closeAllConnections();await new Promise(r=>server.close(r));await rm(root,{recursive:true,force:true});});
  return {app,env,f,server,root,cdp};
}
async function localPage(app,heading){
  let page;
  for(let i=0;i<200;i++){
    page=app.windows().find(w=>w.url().startsWith('file:'));
    if(page && await page.getByRole('heading',{name:heading,exact:true}).count())return page;
    await new Promise(r=>setTimeout(r,100));
  }
  throw Error('Local state did not appear: '+heading);
}
async function shot(page,name){const bytes=await page.screenshot({path:path.join(shotDir,name+'.png')});console.log(shotLine(name,bytes));}

test('D1 → D2 consent → D3 install → D6; tray, popouts, native click, D8, single instance, D7', {timeout:120000}, async t=>{
  const {app,env,f,server,cdp}=await fixture(t);
  let page=await localPage(app,'Checking what Piper needs');
  await page.getByRole('button',{name:'Continue',exact:true}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);
  await shot(page,'D1');
  if(process.env.PIPER_E2E_AGENT_BROWSER === '1') {
    const browser=process.env.AGENT_BROWSER || 'agent-browser', session='piper-desktop-'+process.pid;
    const args=['--session',session,'--cdp',String(cdp)];
    const snapshot=execFileSync(browser,[...args,'snapshot','-i'],{encoding:'utf8',timeout:30000});assert.match(snapshot,/Continue/);
    const file=path.join(shotDir,'D1-agent-browser.png');execFileSync(browser,[...args,'screenshot',file],{timeout:30000});
    console.log(shotLine('D1-agent-browser',await readFile(file)));
    // Disconnecting is deferred until app teardown; this session never attaches to another application.
  }
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'An extension was found'}).waitFor();assert.ok(await page.getByRole('radio',{name:/Load it/}).isChecked());await shot(page,'D2');
  await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByRole('heading',{name:'Installing',exact:true}).waitFor();await shot(page,'D3');
  await app.waitForEvent('window',{predicate:w=>!w.url().startsWith('file:')});
  const main=app.windows().find(w=>w!==page && !w.url().startsWith('file:'));
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();await shot(main,'D6');
  await app.evaluate(async({app})=>{for(let i=0;i<100;i++){if(app.piperTest.tray().count===1)return;await new Promise(r=>setTimeout(r,50));}throw Error('Tray count was not updated');});
  assert.equal((await app.evaluate(({app})=>app.piperTest.tray())).count,1);
  const popup=app.waitForEvent('window');await main.getByRole('button',{name:'Pop out'}).click();
  const popout=await popup;await popout.waitForLoadState();assert.ok(popout.url().includes('popout=session-1'));await popout.close();
  await main.getByRole('button',{name:'Notify'}).click();
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).hide());
  await app.evaluate(({app})=>app.piperTest.clickNotification());await main.getByText('session-1',{exact:true}).waitFor();
  assert.ok(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).isVisible()));
  await app.evaluate(({app})=>app.piperTest.showExtensions());page=await localPage(app,'Extensions');await shot(page,'D8');
  await page.getByRole('button',{name:'Turn off'}).click();await page.getByRole('button',{name:'Load…'}).waitFor();
  await page.getByRole('button',{name:'Load…'}).click();await page.getByRole('heading',{name:'An extension was found'}).waitFor();await page.getByRole('radio',{name:/Load it/}).check();await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByRole('button',{name:'Turn off'}).waitFor();
  const second=spawn(electron,[desktop],{env,stdio:'ignore'});const exit=await new Promise(r=>second.on('exit',r));assert.equal(exit,0);
  // OS focus needs an activatable app (LaunchServices is closed in the test sandbox): shown and not minimized
  assert.ok(await app.evaluate(({BrowserWindow})=>{const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:'));return w.isVisible()&&!w.isMinimized();}));
  server.closeAllConnections();await new Promise(r=>server.close(r));
  page=await localPage(app,'Piper is not running');await shot(page,'D7');
  await new Promise(r=>server.listen(Number(env.PIPER_PORT),'127.0.0.1',r));
  await main.waitForFunction(()=>document.visibilityState==='visible');
  const calls=(await readFile(f('calls.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.ok(calls.some(c=>c[0]==='install'&&c.includes('--extension')));assert.ok(calls.some(c=>c[0]==='extensions'&&c[1]==='untrust'));assert.ok(calls.some(c=>c[0]==='restart'));
  assert.ok(!calls.some(c=>['stop','serve','uninstall-service'].includes(c[0])));
});
test('D4 runtime missing only offers optional installation', {timeout:45000}, async t=>{
  const {app,f}=await fixture(t,{runtime:false});const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Piper needs Docker'}).waitFor();await shot(page,'D4');
  assert.ok(!await exists(f('online')));assert.ok(!await exists(f('calls.jsonl')));
});
test('wizard uses the shared light and dark product tokens', {timeout:45000}, async t=>{
  const {app}=await fixture(t);const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);
  const themes=JSON.parse(await readFile(path.join(desktop,'src/ui/themes.json'),'utf8'));
  for(const scheme of ['light','dark']) {
    // the CSS answer to the system scheme; nativeTheme switching does not reach a renderer under Seatbelt
    await page.emulateMedia({colorScheme:scheme});
    await page.waitForFunction(expected=>getComputedStyle(document.documentElement).getPropertyValue('--page').trim()===expected,themes[scheme].tokens.page);
    await shot(page,'D1-'+scheme);
  }
});
test('D4b missing Windows WSL 2 offers UAC only after explicit action', {timeout:45000}, async t=>{
  const {app,f}=await fixture(t,{platform:'win32'});const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Turn on WSL, then restart'}).waitFor();await shot(page,'D4b');
  assert.ok(!await exists(f('app/resume-wsl')));assert.ok(!await exists(f('calls.jsonl')));
});
test('D5 failed installer retains log and retry succeeds', {timeout:60000}, async t=>{
  const {app,f}=await fixture(t,{fail:true});let page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByRole('heading',{name:'An extension was found'}).waitFor();await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Setup stopped'}).waitFor();await shot(page,'D5');assert.match(await readFile(f('app/setup.log'),'utf8'),/service failed/);
  await rm(f('fail'));await page.getByRole('button',{name:'Try again'}).click();
  await app.waitForEvent('window');const main=app.windows().find(w=>w!==page&&!w.url().startsWith('file:'));await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
});
test('existing service attaches without invoking installer; quit leaves it answering', {timeout:45000}, async t=>{
  const {app,f,env}=await fixture(t,{installed:true});
  const page=await app.firstWindow();await page.getByRole('heading',{name:'Piper fake service'}).waitFor();
  assert.ok(!(await readFile(f('calls.jsonl'),'utf8')).includes('install'));
  await app.close();const response=await fetch(`http://127.0.0.1:${env.PIPER_PORT}/api/state`);assert.equal(response.status,401);
});
test('offline Start Piper invokes only ensure and reconnects', {timeout:45000}, async t=>{
  const {app,f}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();await rm(f('online'));
  const page=await localPage(app,'Piper is not running');await page.getByRole('button',{name:'Start Piper'}).click();
  // the main window may still count as visible behind the overlay: wait for the call, then the reconnect
  let calls=[];
  for(let i=0;i<100&&!calls.some(c=>c[0]==='ensure');i++){await new Promise(r=>setTimeout(r,100));calls=(await readFile(f('calls.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);}
  await app.evaluate(async({BrowserWindow})=>{for(let i=0;i<100;i++){const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:'));if(w&&w.isVisible())return;await new Promise(r=>setTimeout(r,100));}throw Error('main window did not come back');});
  assert.ok(calls.some(c=>c[0]==='ensure'));assert.ok(!calls.some(c=>['install','stop','serve'].includes(c[0])));
});
test('a real piperd (tests/piper-e2e/serve.py): attach through the real CLI, real GUI, tray counts, quit leaves it running', {timeout:120000}, async t=>{
  const repo=path.dirname(desktop);
  const work=await mkdtemp(path.join(os.tmpdir(),'piper-desktop-real-'));
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
  assert.notEqual(port,17895);
  const serve=spawn('python3',[path.join(repo,'tests/piper-e2e/serve.py'),'--port',String(port),'--work',work],{stdio:['ignore','pipe','inherit']});
  t.after(async()=>{serve.kill('SIGTERM');await new Promise(r=>serve.once('exit',r));await rm(work,{recursive:true,force:true});});
  await new Promise((resolve,reject)=>{let out='';serve.stdout.on('data',d=>{out+=d;if(out.includes('SERVE '))resolve();});serve.once('exit',c=>reject(Error('serve.py exited '+c)));});
  const served=JSON.parse(await readFile(path.join(work,'serve.json'),'utf8'));
  const app_=path.join(work,'desktop-app');
  const env={...process.env,HOME:path.join(work,'fakehome'),PIPER_HOME:path.join(work,'ph'),PIPER_PORT:String(port),PIPER_NO_EXTENSIONS:'1',
    PIPER_DESKTOP_HOME:app_,PIPER_DESKTOP_CLI:path.join(repo,'scripts/piper.sh'),PIPER_DESKTOP_INSTALL_SH:path.join(work,'no-installer.sh'),PIPER_DESKTOP_NO_CHROMIUM_SANDBOX:'1',MAC_CHROMIUM_TMPDIR:os.tmpdir()};
  delete env.ELECTRON_RUN_AS_NODE;
  const app=await _electron.launch({executablePath:electron,args:[desktop],env,timeout:30000});
  t.after(async()=>{await Promise.race([app.close().catch(()=>{}),new Promise(r=>setTimeout(r,5000))]);try{app.process().kill('SIGKILL');}catch{}});
  const main=await app.firstWindow();
  await main.waitForURL(u=>u.toString().startsWith(`http://127.0.0.1:${port}/`),{timeout:30000});
  // the real GUI: its own script answered with the session list of the demo data
  await main.waitForFunction(()=>document.querySelectorAll('[data-sid], .session, [data-session]').length>0 || /Overview/.test(document.body.innerText),null,{timeout:30000});
  await shot(main,'D6-real');
  const state=await (await fetch(`http://127.0.0.1:${port}/api/state`,{headers:{Authorization:'Bearer '+served.gui_token}})).json();
  await app.evaluate(async({app},n)=>{for(let i=0;i<150;i++){if(app.piperTest.tray().sessions===n)return;await new Promise(r=>setTimeout(r,100));}throw Error('tray did not count '+n+' sessions: '+JSON.stringify(app.piperTest.tray()));},state.sessions.length);
  assert.ok(!JSON.stringify(await readFile(path.join(app_,'setup.log'),'utf8').catch(()=>'')).includes(served.gui_token),'token in the app log');
  await app.close();
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/state`)).status,401,'piperd still answers after the app quit');
});
