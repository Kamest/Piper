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

async function fixture(t,{runtime=true,fail=false,installed=false,online=installed,core2=false,platform=process.platform}={}) {
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
  if(core2)await writeFile(f('core2'),'1');
  const imports=(await readFile(path.resolve(desktop,'../piper/web/index.html'),'utf8')).match(/<script type="importmap"[^\n]*\n([\s\S]*?)<\/script>/)[1];
  const stub=`<!doctype html><html><head><title>Piper</title><meta charset="utf-8"><link rel="stylesheet" href="/piper/app.css"><link rel="stylesheet" href="/piper/restart.css"><link rel="stylesheet" href="/piper/vendor/xterm/xterm.css"><script type="importmap">${imports}</script></head><body><h1>Piper fake service</h1><textarea id="draft" aria-label="Unsent input"></textarea><button id="notify">Notify</button><button id="notify-owned">State alert</button><button id="popout">Pop out</button><div id="selected"></div><script>
    window.addEventListener('hashchange',()=>{const m=/session=([^&]+)/.exec(location.hash);if(m)document.querySelector('#selected').textContent='hash:'+decodeURIComponent(m[1]);});
    document.querySelector('#notify-owned').onclick=()=>new Notification('Build: waiting for you',{body:'from the GUI',tag:'session-2'});
    document.querySelector('#notify').onclick=()=>{const n=new Notification('Session waits',{body:'Waiting for you',tag:'session-1'});n.onclick=()=>{document.querySelector('#selected').textContent='session-1';window.focus();};};
    document.querySelector('#popout').onclick=()=>window.open('/?popout=session-1','piper-session');
  </script></body></html>`;
  let restarting=0, instance=1;
  const server=http.createServer(async(req,res)=>{
    if(req.url.includes('fail-window-once')&&await exists(f('fail-window-once'))){
      await rm(f('fail-window-once'));req.socket.destroy();return;
    }
    if(req.url.startsWith('/piper/')) {
      const web=path.resolve(desktop,'../piper/web'), target=path.resolve(web,req.url.slice(7));
      if(!target.startsWith(web+path.sep)){res.writeHead(403);res.end();return;}
      try{res.setHeader('Content-Type',target.endsWith('.css')?'text/css':'text/javascript');res.end(await readFile(target));}catch{res.writeHead(404);res.end();}return;
    }
    if(!await exists(f('online'))||Date.now()<restarting){res.writeHead(503);res.end('offline');return;}
    // 9.173: a card answer, a typed reply, a restart - logged with whether the token came along
    if(req.method==='POST'){
      let body='';for await(const chunk of req)body+=chunk;
      await writeFile(f('posts.jsonl'),JSON.stringify([req.url,body?JSON.parse(body):null,req.headers.authorization==='Bearer fake-only-token-1234567890'])+'\n',{flag:'a'});
      if(req.url==='/api/restart') {
        if(await exists(f('restart-refused'))){res.writeHead(409,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Piper update is not ready: finish the merge before restarting.'}));return;}
        if(await exists(f('restart-ack-delay')))await new Promise(r=>setTimeout(r,900));
        restarting=Date.now()+700;instance++;
      }
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true}));return;
    }
    if(req.url==='/api/ping'){
      if(instance>1&&await exists(f('restart-hang'))){res.writeHead(503);res.end();return;}
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify({ok:true,version:1,service_instance:String(instance)+(await exists(f('updated'))?'-updated':'')+(await readFile(f('restarted'),'utf8').catch(()=>''))}));return;
    }
    if(req.url==='/api/state'){
      res.setHeader('Content-Type','application/json');
      if(req.headers.authorization!=='Bearer fake-only-token-1234567890'){res.writeHead(401);res.end(JSON.stringify({error:'missing or unknown bearer token'}));}
      // a test may replace the state (state.json) to drive transitions
      else res.end(await readFile(f('state.json'),'utf8').catch(()=>JSON.stringify({sessions:[{id:'session-1',state:'attention'}],queue:{actions:[{title:'Session waits'}]}})));return;
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
  if(installed){await writeFile(env.PIPER_DESKTOP_CLI,`#!/bin/sh\nexec ${quote(process.execPath)} ${quote(cliScript)} "$@"\n`,{mode:0o755});if(online)await writeFile(f('online'),'1');}
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const cdp=probe.address().port;await new Promise(r=>probe.close(r));
  const app=await _electron.launch({executablePath:electron,args:[desktop,`--remote-debugging-port=${cdp}`],env,timeout:30000});
  // a hung quit must fail the test, not the run: bounded close, then SIGKILL of the app's own process
  // `apps`: a relaunch of the same app (same root) is closed here too, before its files are removed
  const apps=[app];
  t.after(async()=>{for(const a of apps){await Promise.race([a.close().catch(()=>{}),new Promise(r=>setTimeout(r,5000))]);try{a.process().kill('SIGKILL');}catch{}}server.closeAllConnections();await new Promise(r=>server.close(r));await rm(root,{recursive:true,force:true,maxRetries:20,retryDelay:250});});
  // A visible document can precede attach's connected flag, focus and initial tray poll on macOS.
  if(installed&&online)await until(()=>app.evaluate(({app})=>app.piperTest.tray().online),'initial service attachment',300);
  return {app,apps,env,f,server,root,cdp};
}
async function localPage(app,heading){
  let page;
  // 40 s: a cold first launch on a slow CI runner took over 20 s
  for(let i=0;i<400;i++){
    for (page of app.windows().filter(w=>w.url().startsWith('file:'))) {
      if(await page.getByRole('heading',{name:heading,exact:true}).count())return page;
    }
    await new Promise(r=>setTimeout(r,100));
  }
  const shown=await Promise.all(app.windows().map(async w=>w.url().replace(/#.*/,'')+' '+JSON.stringify(await w.locator('h1').allTextContents().catch(()=>[]))));
  throw Error('Local state did not appear: '+heading+'; windows: '+shown.join(' | '));
}
async function shot(page,name){const bytes=await page.screenshot({path:path.join(shotDir,name+'.png')});console.log(shotLine(name,bytes));}
async function workspacePointer(app,page,selector) {
  const target=page.locator(selector);
  if(process.env.PIPER_E2E_NATIVE_POINTER!=='1')return target.click();
  // The Linux CI/Xvfb display is isolated. XTest traverses native drag-region hit testing;
  // CDP mouse dispatch alone can reach a renderer even where the OS would drag its window.
  assert.equal(process.platform,'linux');
  assert.ok(process.env.DISPLAY,'native pointer requires the isolated X display');
  assert.equal(await page.evaluate(()=>devicePixelRatio),1,'XTest coordinates require a scale-one display');
  await target.waitFor({state:'visible'});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const rect=await target.boundingBox();assert.ok(rect&&rect.width&&rect.height,selector);
  const bounds=await app.evaluate(({BrowserWindow},url)=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url).getContentBounds(),page.url());
  const point=[Math.round(bounds.x+rect.x+rect.width/2),Math.round(bounds.y+rect.y+rect.height/2)];
  execFileSync('python3',['-c',`import ctypes,os,sys
x=ctypes.CDLL('libX11.so.6');t=ctypes.CDLL('libXtst.so.6')
x.XOpenDisplay.argtypes=[ctypes.c_char_p];x.XOpenDisplay.restype=ctypes.c_void_p
x.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int];x.XCloseDisplay.argtypes=[ctypes.c_void_p]
t.XTestFakeMotionEvent.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
t.XTestFakeButtonEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
d=x.XOpenDisplay(os.environ['DISPLAY'].encode());assert d,'isolated X display unavailable'
try:
 assert t.XTestFakeMotionEvent(d,-1,int(sys.argv[1]),int(sys.argv[2]),0)
 assert t.XTestFakeButtonEvent(d,1,1,0)
 assert t.XTestFakeButtonEvent(d,1,0,0)
 x.XSync(d,0)
finally:x.XCloseDisplay(d)
`,...point.map(String)],{timeout:10000});
}
async function restartShot(page,cdp,name) {
  await page.emulateMedia({colorScheme:'dark'});
  await shot(page,name);
  if(process.env.PIPER_E2E_AGENT_BROWSER!=='1')return;
  const args=['--session','piper-restart-'+process.pid,'--cdp',String(cdp),'--color-scheme','dark'];
  const browser=process.env.AGENT_BROWSER||'agent-browser';
  const listed=JSON.parse(execFileSync(browser,[...args,'tab','list','--json'],{encoding:'utf8',timeout:30000}));
  const tabs=listed.data?.tabs||listed.tabs||listed.data;
  assert.ok(Array.isArray(tabs),JSON.stringify(listed));
  const target=tabs.find(t=>t.url===page.url());assert.ok(target,'the tested Electron window must be listed');
  execFileSync(browser,[...args,'tab',String(target.id||target.targetId||target.index)],{timeout:30000});
  execFileSync(browser,[...args,'set','media','dark'],{timeout:30000});
  const snapshot=execFileSync(browser,[...args,'snapshot','-i'],{encoding:'utf8',timeout:30000});
  assert.match(snapshot,/Copy diagnostics/);
  const file=path.join(shotDir,name+'-ab.png');
  execFileSync(browser,[...args,'screenshot',file],{timeout:30000});
  console.log(shotLine(name+'-ab',await readFile(file)));
}

test('Restart diagnostics: local and web flows, bounded timeout, retry, refusal and retained input', {timeout:180000}, async t=>{
  const {app,f,cdp}=await fixture(t,{installed:true});
  await app.evaluate(({nativeTheme})=>{nativeTheme.themeSource='dark';});
  const main=await app.firstWindow(); await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  // Load the local renderer before asserting the 900 ms acknowledgement state. A cold macOS
  // renderer can take longer than that state lasts; startup latency is not the restart outcome.
  await app.evaluate(({app})=>app.piperTest.showExtensions());
  await localPage(app,'Extensions');
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('file:')).hide());
  await main.evaluate(async()=>{localStorage.setItem('piper.token','fake-only-token-1234567890');window.restartUI=await import('/piper/dist/restartui.js');});
  await main.getByLabel('Unsent input').fill('unsent draft kept');
  await main.evaluate(()=>{const e=document.querySelector('#draft');e.readOnly=true;e.focus();e.setSelectionRange(2,7);window.restartIdentity=e;});
  const kept=async()=>assert.deepEqual(await main.evaluate(()=>({same:window.restartIdentity===document.querySelector('#draft'),value:document.querySelector('#draft').value,
    selection:[document.querySelector('#draft').selectionStart,document.querySelector('#draft').selectionEnd]})),{same:true,value:'unsent draft kept',selection:[2,7]});
  await writeFile(f('restart-hang'),'1'); await writeFile(f('restart-ack-delay'),'1');
  await click(app,'tray',['Machine','Restart Piper']);
  const local=await localPage(app,'Asking Piper to restart');
  await restartShot(local,cdp,'restart-desktop-requesting');
  await local.getByRole('heading',{name:'Waiting for the new service',exact:true}).waitFor();
  const before=await local.locator('.restart-facts strong').first().textContent();
  await local.waitForTimeout(1100);
  assert.notEqual(await local.locator('.restart-facts strong').first().textContent(),before,'elapsed clock must tick without piperd');
  await restartShot(local,cdp,'restart-desktop-down');
  // The native window close behaves like Hide: keep checking and retain the workspace renderer.
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('file:')).close());
  await kept();
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).isVisible()),true);
  await local.getByRole('heading',{name:'Piper did not come back'}).waitFor({timeout:35000});
  await restartShot(local,cdp,'restart-desktop-timeout');
  await local.getByRole('button',{name:'Show details'}).click();
  await restartShot(local,cdp,'restart-desktop-details');
  await local.getByRole('button',{name:'Copy diagnostics'}).click();
  const copied=await app.evaluate(({clipboard})=>clipboard.readText());
  assert.match(copied,/Outcome: timeout/);assert.doesNotMatch(copied,/fake-only-token|Bearer|\/Users\/|unsent draft/);
  await app.evaluate(({BrowserWindow})=>{const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('file:'));w.setContentSize(620,560);});
  await restartShot(local,cdp,'restart-desktop-620-details');
  await rm(f('restart-hang'));await local.getByRole('button',{name:'Check again',exact:true}).click();
  await local.getByRole('heading',{name:'Piper is back'}).waitFor();
  await restartShot(local,cdp,'restart-desktop-620-recovered');
  assert.equal((await posts(f)).filter(p=>p[0]==='/api/restart').length,1,'retry must be read-only');
  await local.getByRole('button',{name:'Return to workspace'}).click();await kept();
  await writeFile(f('restart-refused'),'1');await click(app,'tray',['Machine','Restart Piper']);
  await local.getByRole('heading',{name:'Restart was not started'}).waitFor();
  await restartShot(local,cdp,'restart-desktop-rejected');
  await local.getByRole('button',{name:'Return to workspace'}).click();await kept();
  await rm(f('restart-refused'));await rm(f('restart-ack-delay'));
  // Hiding a successful desktop restart reports its outcome in the retained workspace.
  await writeFile(f('restart-ack-delay'),'1');await click(app,'tray',['Machine','Restart Piper']);
  await local.getByRole('heading',{name:'Asking Piper to restart'}).waitFor();
  await local.getByRole('button',{name:'Hide',exact:true}).click();
  await main.locator('#toasts').getByText('A new Piper service instance is answering.',{exact:true}).waitFor({timeout:7000});
  await kept();await rm(f('restart-ack-delay'));
  // The actual web modules run in the real Electron renderer, with IPC protection against the generic offline window.
  await writeFile(f('restart-hang'),'1');
  await main.evaluate(()=>{document.querySelector('#draft').focus();void window.restartUI.showRestart();});
  await main.getByRole('heading',{name:'Waiting for the new service',exact:true}).waitFor();
  await main.setViewportSize({width:1040,height:560});
  await restartShot(main,cdp,'restart-web-down');
  assert.equal(await main.locator('dialog').evaluate(e=>e.open),true);
  await main.getByRole('button',{name:'Hide',exact:true}).click();
  await app.evaluate(({app})=>app.piperTest.poll());
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).isVisible()),true,'a hidden active check must keep its workspace available');
  await main.getByRole('button',{name:'Show',exact:true}).click();
  await rm(f('restart-hang'));
  await main.getByRole('heading',{name:'Piper is back'}).waitFor();
  await restartShot(main,cdp,'restart-web-recovered');
  await main.getByRole('button',{name:'Return to workspace'}).click();await kept();
  assert.equal(await main.evaluate(()=>document.activeElement===document.querySelector('#draft')),true,'restore terminal/input focus');
  await writeFile(f('restart-refused'),'1');
  await main.evaluate(()=>void window.restartUI.showRestart());
  await main.getByRole('heading',{name:'Restart was not started'}).waitFor();
  await main.setViewportSize({width:390,height:700});
  await restartShot(main,cdp,'restart-web-390-rejected');
  const bounds=await main.locator('.restart-footer').boundingBox();assert.ok(bounds.y+bounds.height<=700);
  await main.getByRole('button',{name:'Show details'}).click();await restartShot(main,cdp,'restart-web-390-details');
  await main.getByRole('button',{name:'Return to workspace'}).click();await kept();
  assert.equal((await posts(f)).filter(p=>p[0]==='/api/restart').length,5);
  assert.ok(!(await callsOf(f)).some(c=>['stop','stop-all','send','restart'].includes(c[0])),'no terminal/PTY/CLI restart commands');
});

test('Restart preserves a real xterm selection across controller reconnect, without terminal input', {timeout:60000}, async t=>{
  const {app}=await fixture(t,{installed:true});const page=await app.firstWindow();
  await page.getByRole('heading',{name:'Piper fake service'}).waitFor();
  await page.evaluate(async()=>{
    const {TermView}=await import('/piper/dist/term.js'), {installTransport}=await import('/piper/dist/transport.js');
    window.replaySelection=async(sameProcess,sameText)=>{
      const sockets=[],messages=[];
      installTransport({fetch:(...args)=>fetch(...args),socket:()=>{const s={readyState:1,send:m=>messages.push(JSON.parse(m)),close(){}};sockets.push(s);return s;}});
      const parent=document.createElement('div');parent.style.cssText='display:flex;width:700px;height:230px;';document.body.append(parent);
      const view=new TermView('selection-fixture','/example',{onRefused(){},onExit(){},onConnection(){}});
      view.mount(parent);const old=sockets.at(-1);old.onopen();
      const send=(s,msg)=>s.onmessage({data:JSON.stringify(msg)});
      send(old,{t:'hello',info:{cols:80,rows:24,tty:0,pid:41,started:100,replay_geometry:true}});
      old.onmessage({data:new TextEncoder().encode('selected terminal text\r\n').buffer});send(old,{t:'replay-end'});
      await new Promise(r=>view.term.write('',r));await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      view.term.select(0,0,8);const before=view.term.getSelection(),cols=view.term.cols,rows=view.term.rows;
      old.readyState=3;view.ws=null;view.connect();const next=sockets.at(-1);next.onopen();
      send(next,{t:'hello',info:{cols,rows,tty:0,pid:sameProcess?41:42,started:100,replay_geometry:true}});
      next.onmessage({data:new TextEncoder().encode(sameText?'selected terminal text\r\n':'different terminal text\r\n').buffer});send(next,{t:'replay-end'});
      await new Promise(r=>view.term.write('',r));
      const selected=view.term.getSelection();
      window.disposeSelection=()=>{view.dispose();parent.remove();};
      return {before,selected,typed:messages.some(m=>['in','input','send'].includes(m.t))};
    };
  });
  for(const [sameProcess,sameText,expected] of [[true,true,'selected'],[false,true,''],[true,false,'']]){
    const got=await page.evaluate(([p,s])=>window.replaySelection(p,s),[sameProcess,sameText]);
    assert.deepEqual(got,{before:'selected',selected:expected,typed:false});
    if(expected)await shot(page,'restart-xterm-selection');
    await page.evaluate(()=>window.disposeSelection());
  }
});

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
  await page.getByRole('heading',{name:'An extension was found'}).waitFor();assert.ok(await page.getByRole('radio',{name:/^Load it/}).isChecked());await shot(page,'D2');
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
  // The main process announces a session's turn and a new card itself, with the window hidden; the tray badge counts
  // only what blocks work, waiting sessions and the inbox are their own rows.
  const state=(s1,pending)=>writeFile(f('state.json'),JSON.stringify({sessions:[{id:'session-1',title:'Build',state:s1,detail:'turn done'},{id:'session-2',title:'Docs',state:'ready'}],pending,
    queue:{actions:[...pending.map(p=>({key:'p:'+p.id,level:'confirm',title:'Build: wants to push'})),...(s1==='waiting'?[{level:'waiting',title:'Build is waiting for you'}]:[]),{level:'gitlab',title:'Assigned: issue'},{level:'ci',title:'CI failed'}]}}));
  await state('working',[]);await app.evaluate(({app})=>app.piperTest.poll());
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).hide());
  await state('waiting',[{id:7,title:'Build',kind:'git_push',session:'session-1'}]);await app.evaluate(({app})=>app.piperTest.poll());
  assert.deepEqual(await app.evaluate(({app})=>app.piperTest.notes().slice(-2)),['Build: waiting for you','Build: wants to push']);
  assert.deepEqual(await app.evaluate(({app})=>{const m=app.piperTest.tray();return [m.count,m.waiting,m.inbox,m.items];}),[1,1,2,['Build: wants to push']]);
  // the GUI's own copy of a state notification is not posted again while no Piper window has focus
  const before=await app.evaluate(({app})=>app.piperTest.notes().length);
  await main.evaluate(()=>document.querySelector('#notify-owned').click());await new Promise(r=>setTimeout(r,300));
  assert.equal(await app.evaluate(({app})=>app.piperTest.notes().length),before);
  // a click shows the hidden window and selects the session through the GUI's #session= link
  await app.evaluate(({app})=>app.piperTest.clickNote(app.piperTest.notes().length-2));await main.getByText('hash:session-1',{exact:true}).waitFor();
  assert.ok(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')).isVisible()));
  // once the OS refused a native notification (ad-hoc signed macOS), GUI notifications go through the fallback
  const fell=await app.evaluate(({app})=>{app.piperTest.failNative();return app.piperTest.fallbacks().length;});
  await main.getByRole('button',{name:'Notify',exact:true}).click();
  await app.evaluate(async({app},n)=>{for(let i=0;i<50;i++){if(app.piperTest.fallbacks().length>n||process.platform!=='darwin')return;await new Promise(r=>setTimeout(r,50));}throw Error('no fallback');},fell);
  assert.deepEqual((await app.evaluate(({app})=>app.piperTest.fallbacks())).slice(fell).map(x=>x.title),process.platform==='darwin'?['Session waits']:[]);
  await app.evaluate(({app})=>app.piperTest.showExtensions());page=await localPage(app,'Extensions');await shot(page,'D8');
  await page.getByRole('button',{name:'Turn off'}).click();await page.getByRole('heading',{name:'Piper is back'}).waitFor();
  await page.getByRole('button',{name:'Return to workspace'}).click();await app.evaluate(({app})=>app.piperTest.showExtensions());
  await page.getByRole('button',{name:'Load…'}).waitFor();
  await page.getByRole('button',{name:'Load…'}).click();await page.getByRole('heading',{name:'An extension was found'}).waitFor();await page.getByRole('radio',{name:/^Load it/}).check();await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Piper is back'}).waitFor();await page.getByRole('button',{name:'Return to workspace'}).click();
  await app.evaluate(({app})=>app.piperTest.showExtensions());await page.getByRole('button',{name:'Turn off'}).waitFor();
  const second=spawn(electron,[desktop],{env,stdio:'ignore'});const exit=await new Promise(r=>second.on('exit',r));assert.equal(exit,0);
  // OS focus needs an activatable app (LaunchServices is closed in the test sandbox): shown and not minimized
  assert.ok(await app.evaluate(({BrowserWindow})=>{const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:'));return w.isVisible()&&!w.isMinimized();}));
  server.closeAllConnections();await new Promise(r=>server.close(r));
  page=await localPage(app,'Piper is not answering');await shot(page,'D7');
  await new Promise(r=>server.listen(Number(env.PIPER_PORT),'127.0.0.1',r));
  await until(async()=>sub(await menu(app,'tray'),'Machine').some(i=>i.label==='Restart Piper'&&i.enabled),'service returned after interruption');
  await main.waitForFunction(()=>document.visibilityState==='visible');
  const calls=(await readFile(f('calls.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
  assert.ok(calls.some(c=>c[0]==='install'&&c.includes('--extension')));assert.ok(calls.some(c=>c[0]==='extensions'&&c[1]==='untrust'));assert.ok(calls.some(c=>c[0]==='restart'));
  assert.ok(!calls.some(c=>['stop','serve','uninstall-service'].includes(c[0])));
});
test('D4 runtime missing only offers optional installation', {timeout:75000}, async t=>{
  const {app,f}=await fixture(t,{runtime:false});const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Piper needs Docker'}).waitFor();await shot(page,'D4');
  assert.ok(!await exists(f('online')));assert.ok(!await exists(f('calls.jsonl')));
});
test('wizard uses the shared light and dark product tokens', {timeout:75000}, async t=>{
  const {app}=await fixture(t);const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);
  const themes=JSON.parse(await readFile(path.join(desktop,'src/ui/themes.json'),'utf8'));
  for(const scheme of ['light','dark']) {
    // Trigger the real main-process callback too: setTitleBarOverlay is absent on macOS.
    const background=await app.evaluate(({nativeTheme,BrowserWindow},scheme)=>{
      nativeTheme.themeSource=scheme;
      nativeTheme.emit('updated');
      return BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('file:')).getBackgroundColor();
    },scheme);
    assert.equal(background.toLowerCase(),themes[scheme].tokens.page.toLowerCase());
    // Native theme switching does not reach the renderer under Seatbelt; emulate its CSS answer.
    await page.emulateMedia({colorScheme:scheme});
    await page.waitForFunction(expected=>getComputedStyle(document.documentElement).getPropertyValue('--page').trim()===expected,themes[scheme].tokens.page);
    await shot(page,'D1-'+scheme);
  }
});
test('D4b missing Windows WSL 2 offers UAC only after explicit action', {timeout:75000}, async t=>{
  const {app,f}=await fixture(t,{platform:'win32'});const page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Turn on WSL, then restart'}).waitFor();await shot(page,'D4b');
  assert.ok(!await exists(f('app/resume-wsl')));assert.ok(!await exists(f('calls.jsonl')));
});
test('D5 failed installer retains log and retry succeeds', {timeout:90000}, async t=>{
  const {app,f}=await fixture(t,{fail:true});let page=await localPage(app,'Checking what Piper needs');
  await page.waitForFunction(()=>!document.querySelector('[data-action="continue"]').disabled);await page.getByRole('button',{name:'Continue',exact:true}).click();await page.getByRole('heading',{name:'An extension was found'}).waitFor();await page.getByRole('button',{name:'Continue',exact:true}).click();
  await page.getByRole('heading',{name:'Setup stopped'}).waitFor();await shot(page,'D5');assert.match(await readFile(f('app/setup.log'),'utf8'),/service failed/);
  await rm(f('fail'));await page.getByRole('button',{name:'Try again'}).click();
  await app.waitForEvent('window');const main=app.windows().find(w=>w!==page&&!w.url().startsWith('file:'));await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
});
test('existing service attaches without invoking installer; quit leaves it answering', {timeout:75000}, async t=>{
  const {app,f,env}=await fixture(t,{installed:true});
  const page=await app.firstWindow();await page.getByRole('heading',{name:'Piper fake service'}).waitFor();
  assert.ok(!(await readFile(f('calls.jsonl'),'utf8')).includes('install'));
  await app.close();const response=await fetch(`http://127.0.0.1:${env.PIPER_PORT}/api/state`);assert.equal(response.status,401);
});
test('offline Start Piper invokes only ensure and reconnects', {timeout:75000}, async t=>{
  const {app,f}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();await rm(f('online'));
  const page=await localPage(app,'Piper is not answering');await page.getByRole('button',{name:'Start Piper'}).click({timeout:40000});
  // the main window may still count as visible behind the overlay: wait for the call, then the reconnect
  let calls=[];
  for(let i=0;i<100&&!calls.some(c=>c[0]==='ensure');i++){await new Promise(r=>setTimeout(r,100));calls=(await readFile(f('calls.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);}
  await until(async()=>sub(await menu(app,'tray'),'Machine').some(i=>i.label==='Restart Piper'&&i.enabled),'service reconnected');
  assert.ok(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(w=>w.webContents.getURL().startsWith('http:')&&w.isVisible())));
  assert.ok(calls.some(c=>c[0]==='ensure'));assert.ok(!calls.some(c=>['install','stop','serve'].includes(c[0])));
});
const callsOf=async f=>(await readFile(f('calls.jsonl'),'utf8')).trim().split('\n').map(JSON.parse);
const mainWindow=async app=>{
  for(let i=0;i<300;i++){const w=app.windows().find(w=>!w.url().startsWith('file:'));if(w)return w;await new Promise(r=>setTimeout(r,100));}
  throw Error('the Piper window did not open');
};
test('stopped service, a found extension: offered before ensure; Not now is recorded by the CLI first', {timeout:75000}, async t=>{
  const {app,f}=await fixture(t,{installed:true,online:false,core2:true});
  const page=await localPage(app,'An extension was found');
  assert.ok(await page.getByRole('radio',{name:/^Load it/}).isChecked());
  await page.getByRole('radio',{name:/^Not now/}).check();await shot(page,'D2-offer');
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await (await mainWindow(app)).getByRole('heading',{name:'Piper fake service'}).waitFor();
  const calls=(await callsOf(f)).map(c=>c.join(' '));
  const decline=calls.indexOf('extensions decline '+f('projects/tools/ext')), ensure=calls.indexOf('ensure');
  assert.ok(decline>=0&&ensure>decline,calls.join(' | '));
  assert.equal(await readFile(f('declined'),'utf8'),f('projects/tools/ext'));
  assert.ok(!calls.some(c=>c.startsWith('extensions trust')||c.startsWith('install')));
});
test('offline Start Piper asks the same: Load it trusts the extension before ensure', {timeout:75000}, async t=>{
  const {app,f}=await fixture(t,{installed:true,core2:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();await rm(f('online'));
  let page=await localPage(app,'Piper is not answering');await page.getByRole('button',{name:'Start Piper'}).click({timeout:40000});
  page=await localPage(app,'An extension was found');
  assert.ok(await page.getByRole('radio',{name:/^Load it/}).isChecked());await shot(page,'D7-offer');
  await page.getByRole('button',{name:'Continue',exact:true}).click();
  await until(async()=>(await callsOf(f)).some(c=>c[0]==='ensure'),'extension consent completed and Piper ensured');
  await until(async()=>sub(await menu(app,'tray'),'Machine').some(i=>i.label==='Restart Piper'&&i.enabled),'service reconnected after consent');
  assert.ok(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(w=>w.webContents.getURL().startsWith('http:')&&w.isVisible())));
  const calls=(await callsOf(f)).map(c=>c.join(' '));
  const trust=calls.indexOf('extensions trust '+f('projects/tools/ext')), ensure=calls.indexOf('ensure');
  assert.ok(trust>=0&&ensure>trust,calls.join(' | '));assert.ok(await exists(f('trusted')));assert.ok(!await exists(f('declined')));
});
test('a real piperd (tests/piper-e2e/serve.py): attach through the real CLI, real GUI, tray counts, quit leaves it running', {timeout:120000}, async t=>{
  const repo=path.dirname(desktop);
  const work=await mkdtemp(path.join(os.tmpdir(),'piper-desktop-real-'));
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
  assert.notEqual(port,17895);
  const serve=spawn('python3',[path.join(repo,'tests/piper-e2e/serve.py'),'--port',String(port),'--work',work],{stdio:['ignore','pipe','inherit']});
  // piperd's children may still write into the fake home after serve.py exits (ENOTEMPTY on Linux arm64 CI)
  t.after(async()=>{serve.kill('SIGTERM');await new Promise(r=>serve.once('exit',r));await rm(work,{recursive:true,force:true,maxRetries:20,retryDelay:250});});
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
  // a notification click's route into the real GUI: #session= selects that session (its rail tile is current)
  const tiles=await main.$$eval('button.qm[data-sid]',els=>els.map(e=>({sid:e.dataset.sid,current:e.getAttribute('aria-current')==='true'})));
  const target=tiles.find(x=>!x.current);assert.ok(target,'no unselected session tile: '+JSON.stringify(tiles));
  await app.evaluate(({app},id)=>app.piperTest.openSession(id),target.sid);
  await main.waitForSelector(`button.qm[data-sid="${target.sid}"][aria-current="true"]`,{timeout:10000});
  assert.ok(!JSON.stringify(await readFile(path.join(app_,'setup.log'),'utf8').catch(()=>'')).includes(served.gui_token),'token in the app log');
  await app.close();
  assert.equal((await fetch(`http://127.0.0.1:${port}/api/state`)).status,401,'piperd still answers after the app quit');
});

// ---- 9.173 the desktop app's hands ----
const until=async(fn,what,n=100)=>{for(let i=0;i<n;i++){const v=await fn();if(v)return v;await new Promise(r=>setTimeout(r,100));}throw Error('timed out: '+what);};
const posts=async f=>(await readFile(f('posts.jsonl'),'utf8').catch(()=>'')).trim().split('\n').filter(Boolean).map(JSON.parse);
const told=app=>app.evaluate(({app})=>app.piperTest.told());
const menu=(app,which)=>app.evaluate(({app},w)=>app.piperTest.menu(w),which);
const click=(app,which,labels)=>app.evaluate(({app},[w,l])=>app.piperTest.click(w,l),[which,labels]);
const sub=(items,label)=>items.find(i=>i.label===label)?.submenu;
const decide=(id,allow={})=>[{op:'decide',label:'Allow',args:{id,ok:true,...allow}},{op:'decide',label:'Deny',args:{id,ok:false}}];

test('idea:// from the GUI reaches the OS; another command of the scheme does not; the page stays', {timeout:75000}, async t=>{
  const {app}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  await main.evaluate(()=>{location.href='idea://open?file=%2Fwork%2Fshop%2FA.java&line=12';});
  await main.evaluate(()=>{location.href='idea://settings?file=/x';});
  await main.evaluate(()=>{window.open('idea://open?file=%2Fwork%2FB.java');});
  await until(async()=>(await app.evaluate(({app})=>app.piperTest.externals())).length>=2,'externals');
  await new Promise(r=>setTimeout(r,300));
  assert.deepEqual(await app.evaluate(({app})=>app.piperTest.externals()),['idea://open?file=%2Fwork%2Fshop%2FA.java&line=12','idea://open?file=%2Fwork%2FB.java']);
  // the GUI did not navigate away (a locator would wait for the cancelled navigation)
  assert.equal(await main.evaluate(()=>document.querySelector('h1').textContent+' '+location.pathname),'Piper fake service /');
});

test('cards answered from the tray and from notification buttons; a red card never offers Allow; Reply types into a waiting session', {timeout:90000}, async t=>{
  const {app,f}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  const state=(s1,pending,actions)=>writeFile(f('state.json'),JSON.stringify({sessions:[{id:'session-1',title:'Build',state:s1},{id:'session-2',title:'Docs',state:'ready'}],pending,queue:{actions}}));
  await state('working',[],[]);await app.evaluate(({app})=>app.piperTest.poll());
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().forEach(w=>w.hide()));
  await until(()=>app.evaluate(({BrowserWindow})=>!BrowserWindow.getFocusedWindow()),'all windows unfocused before notifications');
  const cards=[{key:'p:7',level:'confirm',title:'Build: wants to push',subject:{type:'session',key:'session-1'},actions:decide(7)},
    {key:'p:8',level:'confirm',title:'Build: write to production',subject:{type:'session',key:'session-1'},actions:decide(8,{danger:'db_write_prod'})}];
  await state('waiting',[{id:7,session:'session-1'},{id:8,session:'session-1'}],[...cards,{level:'waiting',title:'Build is waiting for you',actions:[{op:'reply',args:{session:'session-1'}}]}]);
  await app.evaluate(({app})=>app.piperTest.poll());
  // the tray: each card its own submenu
  const tray=await menu(app,'tray');
  assert.deepEqual(sub(tray,'Build: wants to push').map(i=>i.label),['Allow','Deny','Open in Piper']);
  assert.deepEqual(sub(tray,'Build: write to production').map(i=>i.label),['Deny','Open in Piper to allow']);
  await click(app,'tray',['Build: wants to push','Allow']);
  await until(async()=>(await posts(f)).length>=1,'allow post');
  assert.deepEqual((await posts(f))[0],['/api/pending/7',{approve:true},true]);
  // the notifications of that poll: the plain card with Allow + Deny, the red one only Deny, the waiting session Reply
  const notes=await app.evaluate(({app})=>app.piperTest.notes());
  const at=title=>notes.lastIndexOf(title);
  assert.ok(at('Build: wants to push')>=0&&at('Build: write to production')>=0&&at('Build: waiting for you')>=0,JSON.stringify(notes));
  assert.deepEqual(await app.evaluate(({app},i)=>app.piperTest.noteButtons(i),at('Build: wants to push')),['Allow','Deny']);
  assert.deepEqual(await app.evaluate(({app},i)=>app.piperTest.noteButtons(i),at('Build: write to production')),['Deny']);
  assert.ok(await app.evaluate(({app},i)=>app.piperTest.noteHasReply(i),at('Build: waiting for you')));
  assert.ok(!await app.evaluate(({app},i)=>app.piperTest.noteHasReply(i),at('Build: wants to push')));
  await app.evaluate(({app},i)=>app.piperTest.noteAction(i,'Deny'),at('Build: write to production'));
  await app.evaluate(({app},i)=>app.piperTest.noteReply(i,'yes, push it'),at('Build: waiting for you'));
  await app.evaluate(({app},i)=>app.piperTest.noteReply(i,'   '),at('Build: waiting for you'));   // nothing typed: nothing sent
  await until(async()=>(await posts(f)).length>=3,'deny + reply posts');await new Promise(r=>setTimeout(r,300));
  assert.deepEqual((await posts(f)).slice(1),[['/api/pending/8',{approve:false},true],['/api/sessions/session-1/send',{text:'yes, push it'},true]]);
  // Open in Piper shows the hidden window on that session
  await click(app,'tray',['Build: write to production','Open in Piper to allow']);
  await main.getByText('hash:session-1',{exact:true}).waitFor();
  assert.deepEqual(await told(app),[]);   // nothing failed
});

test('Machine: Start Docker, Update / Restart / Start Piper and Copy Diagnostics through the CLI and docker', {timeout:120000}, async t=>{
  const {app,f,cdp}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  // a stopped Docker Desktop that `docker desktop start` brings up; `open` must never be reached
  await writeFile(f('bin/docker'),`#!/bin/sh\necho "docker $*" >> ${quote(f('runtime.log'))}\ncase "$1 $2" in\n  "info "*) [ -f ${quote(f('docker-up'))} ] ;;\n  "desktop start") : > ${quote(f('docker-up'))} ;;\n  *) exit 1 ;;\nesac\n`,{mode:0o755});
  await writeFile(f('bin/podman'),'#!/bin/sh\nexit 1\n',{mode:0o755});
  await writeFile(f('bin/open'),`#!/bin/sh\necho "open $*" >> ${quote(f('runtime.log'))}\nexit 1\n`,{mode:0o755});
  const machine=sub(await menu(app,'tray'),'Machine');
  assert.deepEqual(machine.map(i=>i.label),['Start Docker','Restart Piper','Update Piper','Copy Diagnostics','Sessions keep running through a restart or an update.']);
  await click(app,'tray',['Machine','Start Docker']);
  await until(async()=>(await told(app)).length>=1,'docker started');
  assert.deepEqual(await told(app),['Docker is running.']);
  assert.match(await readFile(f('runtime.log'),'utf8'),/docker info\n(docker info\n)?docker desktop start\ndocker info\n/);
  assert.doesNotMatch(await readFile(f('runtime.log'),'utf8'),/^open /m);
  await click(app,'tray',['Machine','Start Docker']);await until(async()=>(await told(app)).length>=2,'already');
  assert.equal((await told(app))[1],'Docker is already running.');
  // Update outcomes use the same local screen; the retained renderer is never reloaded.
  await main.evaluate(()=>{window.keptAcrossUpdate=true;});
  await click(app,'tray',['Machine','Update Piper']);
  let outcome=await localPage(app,'Piper is back');
  await outcome.getByRole('button',{name:'Return to workspace'}).click();
  assert.equal(await main.evaluate(()=>window.keptAcrossUpdate),true);
  await click(app,'tray',['Machine','Update Piper']);outcome=await localPage(app,'Piper is back');
  assert.match(await outcome.locator('.restart-message').textContent(),/up to date/);
  await outcome.getByRole('button',{name:'Return to workspace'}).click();
  await writeFile(f('update-refused'),'1');
  await click(app,'tray',['Machine','Update Piper']);outcome=await localPage(app,'Restart was not started');
  assert.match(await outcome.locator('.restart-message').textContent(),/Piper was not updated/);
  await outcome.getByRole('button',{name:'Return to workspace'}).click();
  await rm(f('update-refused'));await writeFile(f('update-missing'),'1');
  await click(app,'tray',['Machine','Update Piper']);outcome=await localPage(app,'Restart was not started');
  assert.match(await outcome.locator('.restart-rejection').textContent(),/This Piper has no `piper update` yet .*git pull/);
  await restartShot(outcome,cdp,'M1-old-core-update');
  await outcome.getByRole('button',{name:'Return to workspace'}).click();
  await rm(f('update-missing'));
  // Restart: a local diagnostic screen appears, retaining the workspace renderer.
  const busy=click(app,'tray',['Machine','Restart Piper']);await busy;
  assert.equal((await sub(await menu(app,'tray'),'Machine'))[0].label,'Restarting Piper…');
  const restart=await localPage(app,'Piper is back');
  await restart.getByRole('button',{name:'Return to workspace',exact:true}).click();
  assert.ok((await posts(f)).some(p=>p[0]==='/api/restart'&&p[2]));
  assert.ok(await app.evaluate(({BrowserWindow})=>!BrowserWindow.getAllWindows().some(w=>w.webContents.getURL().startsWith('file:')&&w.isVisible())),'the offline screen appeared during the restart');
  // diagnostics: versions, runtime, status, doctor - the token redacted
  await click(app,'tray',['Machine','Copy Diagnostics']);await until(async()=>(await told(app)).length>=3,'diagnostics');
  const text=await app.evaluate(({clipboard})=>clipboard.readText());
  assert.match(text,/^Piper desktop \S+ on \w+ \w+\nService: answering\nContainer runtime: docker\n/);
  assert.match(text,/\$ piper status \(exit 0\)\npiperd answers on/);assert.match(text,/\$ piper doctor \(exit 1\)\nok {3}git/);
  assert.ok(!text.includes('fake-only-token-1234567890'),'token in the diagnostics');
  // offline: the Machine menu offers Start Piper (ensure), and the window comes back
  await rm(f('online'));await localPage(app,'Piper is not answering');
  assert.deepEqual(sub(await menu(app,'tray'),'Machine').map(i=>i.label).slice(0,2),['Start Docker','Start Piper']);
  await click(app,'tray',['Machine','Start Piper']);
  await until(async()=>sub(await menu(app,'tray'),'Machine').some(i=>i.label==='Restart Piper'&&i.enabled),'service reconnected after Machine start');
  const calls=(await callsOf(f)).map(c=>c[0]);
  assert.ok(calls.includes('ensure')&&calls.includes('update')&&calls.includes('doctor')&&calls.includes('status'));
  assert.ok(!calls.some(c=>['stop','stop-all','serve','uninstall-service','install'].includes(c)),calls.join(' '));
});

test('more windows: ⌘N and a session in its own window, retained offline, back on reconnect, restored after a restart of the app', {timeout:120000}, async t=>{
  const {app,apps,f,env}=await fixture(t,{installed:true});const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  await writeFile(f('state.json'),JSON.stringify({sessions:[{id:'session-1',title:'Build',state:'ready'},{id:'session-2',title:'Docs',state:'ready'}],queue:{actions:[]}}));
  await app.evaluate(({app})=>app.piperTest.poll());
  const file=sub(await menu(app,'app'),'File');
  assert.deepEqual(file.map(i=>[i.label,i.enabled]),[['New Window',true],['Open Session in New Window',true]]);
  assert.deepEqual(file[1].submenu.map(i=>i.label),['Build','Docs']);
  assert.ok((await menu(app,'app')).some(i=>i.label==='Window'||i.label==='&Window'||/Window/.test(i.label)),'no Window menu');
  const opened=app.waitForEvent('window');await click(app,'app',['File','New Window']);const w2=await opened;
  await w2.getByRole('heading',{name:'Piper fake service'}).waitFor();
  assert.match(w2.url(),/\/\?window=2#token=/);
  const opened3=app.waitForEvent('window');await click(app,'tray',['Open Session in New Window','Docs']);const w3=await opened3;
  await w3.getByText('hash:session-2',{exact:true}).waitFor();
  assert.match(w3.url(),/\/\?window=3#session=session-2$/);
  await shot(w3,'W1-session-window');
  let wins=await app.evaluate(({app})=>app.piperTest.windows());
  assert.deepEqual(wins.map(w=>[w.slot,w.visible]),[[2,true],[3,true]]);
  // closing one closes it (the main window only hides); the list on disk follows
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().includes('window=2')).close());
  // The asynchronous write can be observed between truncation and completion; retry that intermediate state.
  await until(async()=>{try{return JSON.parse(await readFile(f('app/windows.json'),'utf8')).length===1;}catch{return false;}},'windows.json after close');
  assert.deepEqual(JSON.parse(await readFile(f('app/windows.json'),'utf8')).map(w=>w.slot),[3]);
  // the slot is free again for the next window
  const opened2=app.waitForEvent('window');await click(app,'app',['File','New Window']);const again=await opened2;
  await again.getByRole('heading',{name:'Piper fake service'}).waitFor();assert.match(again.url(),/window=2/);
  // offline: every workspace window stays intact behind diagnostics; Start Piper reconnects it
  await rm(f('online'));const page=await localPage(app,'Piper is not answering');
  wins=await app.evaluate(({app})=>app.piperTest.windows());assert.ok(wins.every(w=>w.visible),JSON.stringify(wins));
  await page.getByRole('button',{name:'Start Piper'}).click({timeout:40000});
  await until(async()=>sub(await menu(app,'tray'),'Machine').some(i=>i.label==='Restart Piper'&&i.enabled),'service reconnected with extra windows');
  await until(async()=>(await app.evaluate(({app})=>app.piperTest.windows())).every(w=>w.visible),'extra windows back');
  // New Window is off while the service is down
  await app.evaluate(({BrowserWindow})=>{const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().includes('window=3'));w.setBounds({x:60,y:80,width:900,height:640});});
  await new Promise(r=>setTimeout(r,300));
  await app.close();
  const saved=JSON.parse(await readFile(f('app/windows.json'),'utf8'));
  assert.deepEqual(saved.map(w=>w.slot).sort(),[2,3]);
  assert.deepEqual(saved.find(w=>w.slot===3).bounds,{x:60,y:80,width:900,height:640});
  // the next start opens them again, where they were
  const app2=await _electron.launch({executablePath:electron,args:[desktop],env,timeout:30000});
  apps.push(app2);
  await until(async()=>{const w=await app2.evaluate(({app})=>app.piperTest.windows()).catch(()=>[]);return w.length===2&&w.every(x=>x.url&&x.visible);},'restored windows',300).catch(async e=>{
    throw Error(e.message+' '+JSON.stringify(await app2.evaluate(({app,BrowserWindow})=>({w:app.piperTest.windows(),all:BrowserWindow.getAllWindows().map(w=>w.webContents.getURL().replace(/#.*/,'')),t:app.piperTest.told()})).catch(x=>String(x)))+' exit='+app2.process().exitCode);});
  wins=await app2.evaluate(({app})=>app.piperTest.windows());
  assert.deepEqual(wins.map(w=>w.slot).sort(),[2,3]);
  assert.ok(wins.every(w=>/\?window=\d$/.test(w.url)),JSON.stringify(wins));
  const w3b=wins.find(w=>w.slot===3);assert.deepEqual([w3b.bounds.width,w3b.bounds.height],[900,640]);
  // offline at start: nothing is opened over the wizard
  await rm(f('online'));
  const menuOff=await until(async()=>{const m=sub(await menu(app2,'app'),'File');return m&&!m[0].enabled?m:null;},'New Window disabled offline');
  assert.equal(menuOff[0].enabled,false);
});

test('workspace ownership and a crashed second renderer leave the main window and daemon online', {timeout:90000}, async t=>{
  const {app,f}=await fixture(t,{installed:true});
  const main=await app.firstWindow();
  await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  await main.getByLabel('Unsent input').fill('main draft survives another renderer');
  await main.evaluate(()=>{window.retainedWorkspace=document.querySelector('#draft');});
  const opened=app.waitForEvent('window');
  await click(app,'app',['File','New Window']);
  const second=await opened;
  await second.getByRole('heading',{name:'Piper fake service'}).waitFor();
  assert.deepEqual(await main.evaluate(()=>window.__piperNative.workspace('state',1)),[2]);
  // Move the main window first, then claim its old slot from the secondary window.
  await main.evaluate(async()=>{
    await window.__piperNative.workspace('claim',3);
    const url=new URL(location.href);url.searchParams.set('window','3');history.replaceState(null,'',url);
  });
  await second.evaluate(async()=>{
    await window.__piperNative.workspace('claim',1);
    const url=new URL(location.href);url.searchParams.delete('window');history.replaceState(null,'',url);
  });
  assert.deepEqual(await main.evaluate(()=>window.__piperNative.workspace('state',3)),[1]);
  assert.deepEqual(await second.evaluate(()=>window.__piperNative.workspace('state',1)),[3]);
  const rejected=await main.evaluate(()=>window.__piperNative.workspace('claim',1).then(()=>false,()=>true));
  assert.equal(rejected,true,'a live secondary owner cannot be overwritten');
  assert.deepEqual((await app.evaluate(({app})=>app.piperTest.windows())).map(w=>w.slot),[1]);
  const crashed=second.waitForEvent('crash');
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')&&!w.webContents.getURL().includes('window=3')).webContents.forcefullyCrashRenderer());
  await crashed;
  await until(()=>app.evaluate(async({BrowserWindow})=>{
    const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')&&!w.webContents.getURL().includes('window=3'));
    return w&&!w.webContents.isCrashed()&&await w.webContents.executeJavaScript('document.querySelector("h1")?.textContent === "Piper fake service"').catch(()=>false);
  }).catch(()=>false),'second renderer recovered',300).catch(async error=>{
    throw Error(error.message+' '+JSON.stringify(await app.evaluate(({app,BrowserWindow})=>({online:app.piperTest.tray().online,
      windows:BrowserWindow.getAllWindows().map(w=>({url:w.webContents.getURL().replace(/#.*/,''),crashed:w.webContents.isCrashed(),loading:w.webContents.isLoading()}))}))));
  });
  assert.equal(await main.evaluate(()=>window.retainedWorkspace===document.querySelector('#draft')),true);
  assert.equal(await main.getByLabel('Unsent input').inputValue(),'main draft survives another renderer');
  assert.equal(await app.evaluate(({app})=>app.piperTest.tray().online),true);
  // A real failed main-frame request while /api/state remains healthy recovers only that window.
  await writeFile(f('fail-window-once'),'1');
  await app.evaluate(async({BrowserWindow})=>{
    const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')&&!w.webContents.getURL().includes('window=3'));
    const url=new URL(w.webContents.getURL());url.searchParams.set('fail-window-once','1');
    await w.loadURL(url.href).catch(()=>{});
  });
  await until(()=>app.evaluate(async({BrowserWindow})=>{
    const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().startsWith('http:')&&!w.webContents.getURL().includes('window=3'));
    return w&&await w.webContents.executeJavaScript('document.querySelector("h1")?.textContent === "Piper fake service"').catch(()=>false);
  }).catch(()=>false),'failed navigation recovered',300);
  assert.equal(await main.evaluate(()=>window.retainedWorkspace===document.querySelector('#draft')),true);
  assert.equal(await main.getByLabel('Unsent input').inputValue(),'main draft survives another renderer');
  assert.equal(await app.evaluate(({app})=>app.piperTest.tray().online),true);
  assert.equal(await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(w=>w.webContents.getURL().startsWith('file:')&&w.isVisible())),false);
  // A subframe failure must not start global outage diagnostics either.
  await app.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL().includes('window=3')).webContents.emit('did-fail-load',{},-105,'synthetic subframe failure','https://example.com/frame',false));
  assert.equal(await app.evaluate(({app})=>app.piperTest.tray().online),true);
  // Record the surviving window and its retained draft through the same screenshot path as the suite.
  // Recovery of the other renderer is asserted above; its GPU surface can still reject capturePage().
  await shot(main,'W3-main-preserved-renderer-recovery');
});

// This is deliberately a second native app, rather than a webContents.startDrag mock or
// CDP drag dispatch. Only the isolated Linux CI display may receive OS pointer input.
async function linkedWorktreeDrop({app,main,env,work,port,served},setReceiver) {
  const request=async(route,body)=>{
    const response=await fetch(`http://127.0.0.1:${port}/api/${route}`,{method:body?'POST':'GET',headers:{Authorization:'Bearer '+served.gui_token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
    const data=await response.json();assert.ok(response.ok,JSON.stringify(data));return data;
  };
  const wt=await request('git/worktree',{project:'grp/demo',action:'add',name:'native-file',branch:'native-file',new:true});
  const filename="native file's $value; fixture.txt", source=path.join(wt.path,filename), text='Native linked-worktree drag fixture\n';
  await writeFile(source,text);
  const session=await request('sessions',{project:wt.key,title:'Native file fixture'});
  const state=await request('state');
  assert.equal(state.projects.some(p=>p.key===wt.key),false,'derived worktree must be absent from the base project registry');
  assert.equal(state.sessions.find(s=>s.id===session.id)?.path,wt.path,'the session owns the actual linked-worktree root');
  await main.locator(`button.qm[data-sid="${session.id}"]`).click({timeout:30000});
  await main.getByRole('navigation',{name:'session view'}).getByRole('button',{name:'Files',exact:true}).click();
  const row=main.locator('.files .tree [data-nav]').filter({hasText:filename});
  await row.waitFor({state:'visible'});
  await row.click({button:'right'});
  await main.getByRole('menuitem',{name:'Copy absolute path',exact:true}).click();
  await until(async()=>(await app.evaluate(({clipboard})=>clipboard.readText()))===source,'worktree absolute path copied');
  assert.ok(await exists(path.join(wt.path,'.git')),'source really is a linked worktree');
  // A missing root used to turn this into /<filename> and Finder's "file not found".
  // prepareDrag reaches the real API and validates this actual file before native handoff.
  await app.evaluate(({BrowserWindow},url)=>{
    const w=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url);w.setBounds({x:10,y:30,width:740,height:850});
  },main.url());
  const receiverDir=path.join(work,'drop-receiver');await mkdir(path.join(receiverDir,'app-data'),{recursive:true});
  await writeFile(path.join(receiverDir,'preload.cjs'),`const {ipcRenderer,webUtils}=require('electron');
window.addEventListener('dragover',e=>e.preventDefault());
window.addEventListener('drop',e=>{e.preventDefault();const files=[...e.dataTransfer.files].map(f=>webUtils.getPathForFile(f));
ipcRenderer.invoke('fixture:received',files).then(result=>{document.getElementById('received').textContent=JSON.stringify(result);});});\n`);
  await writeFile(path.join(receiverDir,'index.html'),'<!doctype html><meta charset="utf-8"><title>Native drop receiver</title><body style="background:#202124;color:white;font:18px sans-serif;padding:24px"><h1>Native file drop receiver</h1><p>Drop the linked-worktree fixture here.</p><pre id="received" style="white-space:pre-wrap;overflow-wrap:anywhere">Waiting for a real OS file drop</pre></body>');
  await writeFile(path.join(receiverDir,'main.cjs'),`const {app,BrowserWindow,ipcMain}=require('electron');const fs=require('node:fs');const path=require('node:path');
app.setPath('userData',path.join(__dirname,'app-data'));app.whenReady().then(()=>{
ipcMain.handle('fixture:received',(_event,files)=>{if(files.length!==1||files[0]!==${JSON.stringify(source)})return {error:'unexpected file',files};return {files,text:fs.readFileSync(files[0],'utf8')};});
const win=new BrowserWindow({x:780,y:30,width:460,height:620,webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,sandbox:true}});win.loadFile(path.join(__dirname,'index.html'));});\n`);
  const receiver=await _electron.launch({executablePath:electron,args:[path.join(receiverDir,'main.cjs'),'--no-sandbox'],env,timeout:30000});setReceiver(receiver);
  const target=await receiver.firstWindow();await target.getByRole('heading',{name:'Native file drop receiver'}).waitFor();
  const errors=[];main.on('pageerror',error=>errors.push({window:'source',message:error.message}));target.on('pageerror',error=>errors.push({window:'receiver',message:error.message}));
  await main.evaluate(()=>{
    window.__nativeSourceTrace=[];
    for(const capture of [true,false])for(const type of ['pointerdown','dragstart','dragend','pointerup'])window.addEventListener(type,event=>{
      window.__nativeSourceTrace.push({type,capture,trusted:event.isTrusted,phase:event.eventPhase,prevented:event.defaultPrevented,nav:event.target.closest?.('[data-nav]')?.dataset.nav||'',ready:event.target.closest?.('[data-nav]')?.dataset.nativeDrag||''});
    },capture);
  });
  await target.evaluate(()=>{
    window.__nativeReceiverTrace=[];
    for(const type of ['dragenter','dragover','dragleave','drop'])window.addEventListener(type,event=>{
      // Bound the passive trace: dragover may repeat until the OS operation finishes.
      if(window.__nativeReceiverTrace.length<20||type==='drop')window.__nativeReceiverTrace.push({type,trusted:event.isTrusted,types:[...event.dataTransfer.types],files:[...event.dataTransfer.files].map(f=>({name:f.name,size:f.size}))});
    },true);
  });
  await app.evaluate(({BrowserWindow,ipcMain},url)=>{
    globalThis.__nativeDragRequests=0;globalThis.__nativeStartCalls=[];
    ipcMain.on('file:drag',()=>{globalThis.__nativeDragRequests++;});
    const contents=BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url).webContents, original=contents.startDrag;
    // Observe and delegate to the real native API. Never replace its result or swallow errors.
    contents.startDrag=function(options){
      const call={called:true,iconEmpty:options.icon.isEmpty(),iconSize:options.icon.getSize()};globalThis.__nativeStartCalls.push(call);
      try{return original.call(this,options);}catch(error){call.error=error.message;throw error;}
    };
  },main.url());
  await app.evaluate(({BrowserWindow},url)=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url).focus(),main.url());
  await row.scrollIntoViewIfNeeded();
  const point=async(owner,page,locator)=>{
    const rect=await locator.boundingBox();assert.ok(rect&&rect.width&&rect.height);
    assert.equal(await page.evaluate(()=>devicePixelRatio),1);
    const bounds=await owner.evaluate(({BrowserWindow},url)=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url).getContentBounds(),page.url());
    return [Math.round(bounds.x+rect.x+rect.width/2),Math.round(bounds.y+rect.y+rect.height/2)];
  };
  const start=await point(app,main,row), end=await point(receiver,target,target.locator('body'));
  const pointer=(gesture)=>execFileSync('python3',['-c',`import ctypes,os,sys,time
x=ctypes.CDLL('libX11.so.6');t=ctypes.CDLL('libXtst.so.6')
x.XOpenDisplay.argtypes=[ctypes.c_char_p];x.XOpenDisplay.restype=ctypes.c_void_p
x.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int];x.XCloseDisplay.argtypes=[ctypes.c_void_p]
t.XTestFakeMotionEvent.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
t.XTestFakeButtonEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
d=x.XOpenDisplay(os.environ['DISPLAY'].encode());assert d,'isolated X display unavailable'
sx,sy,ex,ey=map(int,sys.argv[2:]);pressed=False
def move(px,py):
 assert t.XTestFakeMotionEvent(d,-1,px,py,0);x.XSync(d,0)
try:
 move(sx,sy)
 if sys.argv[1]=='drag':
  assert t.XTestFakeButtonEvent(d,1,1,0);pressed=True;x.XSync(d,0)
  for i in range(1,41):
   move(round(sx+(ex-sx)*i/40),round(sy+(ey-sy)*i/40));time.sleep(.02)
  time.sleep(.15)
finally:
 if pressed:t.XTestFakeButtonEvent(d,1,0,0);x.XSync(d,0)
 x.XCloseDisplay(d)
`,gesture,...start.map(String),...end.map(String)],{timeout:10000});
  pointer('hover');
  await until(async()=>await row.getAttribute('data-native-drag')==='ready','native drag prepared from worktree root');
  await shot(main,'Files-linked-worktree-native-source');
  // No delay between press and movement: waiting after press would conceal the original
  // asynchronous prepare/startDrag race during a normal user gesture.
  try {
    pointer('drag');
    await target.waitForFunction(()=>document.getElementById('received').textContent.startsWith('{'));
    assert.deepEqual(JSON.parse(await target.locator('#received').textContent()),{files:[source],text},'another process received the exact native file and bytes');
    assert.equal(await row.getAttribute('data-native-drag'),null,'native drag consumed its prepared token');
  } finally {
    console.log('# Native OS drag diagnostics '+JSON.stringify({start,end,
      source:await main.evaluate(()=>window.__nativeSourceTrace),
      requests:await app.evaluate(()=>globalThis.__nativeDragRequests),
      native:await app.evaluate(()=>globalThis.__nativeStartCalls),
      receiver:await target.evaluate(()=>window.__nativeReceiverTrace),
      ready:await row.getAttribute('data-native-drag'),errors}));
    await shot(target,'Files-native-drop-received');
  }
  console.log('# Real XTest drag transferred the linked-worktree file into a separate receiver process');
}

test('a real piperd: independent windows, tray approvals and linked-worktree native drag', {timeout:180000}, async t=>{
  const repo=path.dirname(desktop);
  const work=await mkdtemp(path.join(os.tmpdir(),'piper-desktop-hands-'));
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
  const serve=spawn('python3',[path.join(repo,'tests/piper-e2e/serve.py'),'--port',String(port),'--work',work],{stdio:['ignore','pipe','inherit']});
  let app, receiver;
  t.after(async()=>{
    // Finish intercepted requests while their upstream is alive, including after an assertion fails.
    // Node runs these hooks in registration order: close the client before stopping its service.
    try {
      if(app)await Promise.all(app.windows().filter(p=>!p.isClosed()).map(p=>p.unrouteAll({behavior:'wait'})));
    } finally {
      if(receiver){
        await Promise.race([receiver.close().catch(()=>{}),new Promise(r=>setTimeout(r,5000))]);
        try{receiver.process().kill('SIGKILL');}catch{}
      }
      if(app){
        await Promise.race([app.close().catch(()=>{}),new Promise(r=>setTimeout(r,5000))]);
        try{app.process().kill('SIGKILL');}catch{}
      }
      if(serve.exitCode===null){serve.kill('SIGTERM');await new Promise(r=>serve.once('exit',r));}
      await rm(work,{recursive:true,force:true,maxRetries:20,retryDelay:250});
    }
  });
  await new Promise((resolve,reject)=>{let out='';serve.stdout.on('data',d=>{out+=d;if(out.includes('SERVE '))resolve();});serve.once('exit',c=>reject(Error('serve.py exited '+c)));});
  const served=JSON.parse(await readFile(path.join(work,'serve.json'),'utf8'));
  const env={...process.env,HOME:path.join(work,'fakehome'),PIPER_HOME:path.join(work,'ph'),PIPER_PORT:String(port),PIPER_NO_EXTENSIONS:'1',
    PIPER_DESKTOP_HOME:path.join(work,'desktop-app'),PIPER_DESKTOP_CLI:path.join(repo,'scripts/piper.sh'),PIPER_DESKTOP_INSTALL_SH:path.join(work,'no-installer.sh'),PIPER_DESKTOP_NO_CHROMIUM_SANDBOX:'1',MAC_CHROMIUM_TMPDIR:os.tmpdir()};
  delete env.ELECTRON_RUN_AS_NODE;
  app=await _electron.launch({executablePath:electron,args:[desktop],env,timeout:30000});
  const main=await app.firstWindow();
  await main.waitForURL(u=>u.toString().startsWith(`http://127.0.0.1:${port}/`),{timeout:30000});
  await main.waitForSelector('button.qm[data-sid]',{timeout:30000});
  const tiles=await main.$$eval('button.qm[data-sid]',els=>els.map(e=>({sid:e.dataset.sid,current:e.getAttribute('aria-current')==='true'})));
  const mine=tiles.find(x=>x.current)?.sid, other=tiles.find(x=>!x.current)?.sid;assert.ok(mine&&other,JSON.stringify(tiles));
  // Actual workspace labels must click through native titlebar hit testing, not drag.
  // Only the outdated flag is a fixture; the renderer, IPC ownership and window are real.
  await main.route('**/api/state',async route=>{
    const headers=route.request().headers();delete headers['if-none-match'];
    const response=await route.fetch({headers}), modifiedHeaders=response.headers();
    // The injected JSON has a different representation: never cache it under the origin's ETag.
    delete modifiedHeaders.etag;delete modifiedHeaders['content-length'];
    await route.fulfill({response,headers:modifiedHeaders,json:{...await response.json(),code_outdated:true}});
  });
  await main.waitForSelector('#update-banner');
  await main.waitForSelector('.xterm');
  await main.evaluate(()=>{window.__workspaceTerminal=document.querySelector('.xterm');});
  await workspacePointer(app,main,'#winbar .workspace-add');
  await main.waitForURL(u=>u.searchParams.get('window')==='2',{timeout:10000});
  if(process.env.PIPER_E2E_NATIVE_POINTER==='1') {
    // Prove the original failure through the OS: a drag region consumes the label click.
    const key=await app.evaluate(({BrowserWindow},url)=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url)
      .webContents.insertCSS('#winbar .workspace-tab{-webkit-app-region:drag!important}'),main.url());
    await main.waitForTimeout(200); // Chromium must publish the changed native drag regions.
    await workspacePointer(app,main,'#winbar .workspace-tab:not(.on) .workspace-title');
    await main.waitForTimeout(200);
    assert.equal(new URL(main.url()).searchParams.get('window'),'2','native drag region must consume the baseline click');
    await app.evaluate(({BrowserWindow},{url,key})=>BrowserWindow.getAllWindows().find(w=>w.webContents.getURL()===url).webContents.removeInsertedCSS(key),{url:main.url(),key});
    await main.waitForTimeout(200);
    console.log('# Native XTest reproduced the drag-region failure; now exercising fixed workspace labels');
  }
  for(const slot of [1,2,1]) {
    await workspacePointer(app,main,'#winbar .workspace-tab:not(.on) .workspace-title');
    await main.waitForURL(u=>Number(u.searchParams.get('window')||1)===slot,{timeout:10000});
    assert.equal(await main.locator('#winbar .workspace-tab.on').evaluate(el=>getComputedStyle(el).webkitAppRegion),'no-drag');
  }
  await main.waitForSelector(`button.qm[data-sid="${mine}"][aria-current="true"]`);
  assert.equal(await main.evaluate(()=>window.__workspaceTerminal.isConnected),true,'workspace switch retained the original terminal');
  const chrome=await main.evaluate(()=>({bar:document.getElementById('winbar').getBoundingClientRect().top,
    banner:document.getElementById('update-banner').getBoundingClientRect().top,
    height:document.getElementById('winbar').getBoundingClientRect().height}));
  assert.deepEqual(chrome,{bar:0,banner:38,height:38});
  await shot(main,'W4-native-workspace-click-and-banner');
  await workspacePointer(app,main,'#winbar .workspace-tab:not(.on) .workspace-close');
  await main.waitForFunction(()=>document.querySelectorAll('#winbar .workspace-tab').length===1);
  await main.unrouteAll({behavior:'wait'});
  await main.waitForSelector('#update-banner',{state:'detached'});
  // the other session in a window of its own; the main window keeps its session, before and after a reload
  const opened=app.waitForEvent('window');
  await app.evaluate(async({app})=>{for(let i=0;i<100&&!app.piperTest.menu('app').find(m=>m.label==='File')?.submenu[1].enabled;i++)await new Promise(r=>setTimeout(r,100));});
  const title=(await menu(app,'app')).find(m=>m.label==='File').submenu[1].submenu.find(s=>s.label)?.label;assert.ok(title);
  const state0=await (await fetch(`http://127.0.0.1:${port}/api/state`,{headers:{Authorization:'Bearer '+served.gui_token}})).json();
  const otherTitle=state0.sessions.find(s=>s.id===other).title;
  await click(app,'app',['File','Open Session in New Window',otherTitle]);
  const w2=await opened;
  await w2.waitForSelector(`button.qm[data-sid="${other}"][aria-current="true"]`,{timeout:30000});
  await shot(w2,'W2-real-second-window');
  await main.waitForSelector(`button.qm[data-sid="${mine}"][aria-current="true"]`,{timeout:10000});
  await main.reload();await main.waitForSelector(`button.qm[data-sid="${mine}"][aria-current="true"]`,{timeout:30000});
  await w2.reload();await w2.waitForSelector(`button.qm[data-sid="${other}"][aria-current="true"]`,{timeout:30000});
  const keys=await w2.evaluate(()=>Object.keys(localStorage).filter(k=>/^piper\.(last|layout)/.test(k)).sort());
  assert.ok(keys.includes('piper.last@2')&&keys.includes('piper.last'),keys.join(','));
  // two real cards: a plain domain and one naming this machine (red) - the tray answers them through the API
  execFileSync('python3',[path.join(repo,'tests/piper-e2e/cards.py'),path.join(work,'serve.json'),'grp/demo:piper_allow_domain:{"domain":"api.example.org"}','grp/demo:piper_allow_domain:{"domain":"host.docker.internal"}'],{stdio:'inherit'});
  // the two allow_domain cards (the demo data may hold other cards): piperd's pending list names their kind
  const ours=async()=>{const s=await (await fetch(`http://127.0.0.1:${port}/api/state`,{headers:{Authorization:'Bearer '+served.gui_token}})).json();
    return s.pending.filter(p=>p.kind==='allow_domain').map(p=>String(p.id));};
  const entries=await until(async()=>{await app.evaluate(({app})=>app.piperTest.poll());const ids=await ours();const m=await app.evaluate(({app})=>app.piperTest.tray());
    const cards=(m.entries||[]).filter(e=>e.card&&ids.includes(e.card.id));return cards.length>=2?cards:null;},'two allow_domain cards in the tray',300);
  const plain=entries.find(e=>e.card.allow), red=entries.find(e=>!e.card.allow);
  assert.ok(plain&&red,JSON.stringify(entries));
  // rows by position (both cards may read the same): running, summary, WAITING FOR YOU, then the entries
  const all=(await app.evaluate(({app})=>app.piperTest.tray())).entries;
  const row=e=>3+all.findIndex(x=>x.card?.id===e.card.id);
  const tray=(await menu(app,'tray'));
  assert.deepEqual(tray[row(red)].submenu.map(i=>i.label),['Deny','Open in Piper to allow']);
  assert.deepEqual(tray[row(plain)].submenu.map(i=>i.label),['Allow','Deny','Open in Piper']);
  await click(app,'tray',[row(plain),'Allow']);await click(app,'tray',[row(red),'Deny']);
  await until(async()=>{const s=await (await fetch(`http://127.0.0.1:${port}/api/state`,{headers:{Authorization:'Bearer '+served.gui_token}})).json();
    return !s.pending.some(p=>[plain.card.id,red.card.id].includes(String(p.id)));},'cards answered',200);
  // piperd's audit: the two answers, by a human (not auto), one allowed and one denied
  const answered=async()=>(await readFile(path.join(work,'ph','audit.log'),'utf8')).trim().split('\n').map(JSON.parse)
    .filter(e=>e.event==='confirm'&&e.kind==='allow_domain'&&e.answered&&!e.auto);
  await until(async()=>(await answered()).length>=2,'both answers audited',200);
  assert.deepEqual((await answered()).map(e=>e.approved).sort(),[false,true]);
  assert.deepEqual(await told(app),[]);
  if(process.env.PIPER_E2E_NATIVE_POINTER==='1') {
    assert.equal(process.platform,'linux');assert.ok(process.env.DISPLAY,'native drag requires isolated Xvfb');
    await w2.close();
    await linkedWorktreeDrop({app,main,env,work,port,served},value=>{receiver=value;});
  } else console.log('# Native OS drag-out: unexercised on this platform (isolated Linux/XTest only)');
});

test('About shows desktop and loaded service versions; checks, manual download and offline details work', {timeout:90000}, async t=>{
  const {app,f,cdp}=await fixture(t,{installed:true});
  const main=await app.firstWindow();await main.getByRole('heading',{name:'Piper fake service'}).waitFor();
  await writeFile(f('state.json'),JSON.stringify({version:1,service_version:'0.1.4+4.g821c071',sessions:[]}));
  await app.evaluate(async({app})=>{await app.piperTest.poll();app.piperTest.setUpdates({version:'0.1.8',packaged:true,signed:false,platform:'darwin',fetch:async()=>{await new Promise(r=>setTimeout(r,300));return {ok:true,json:async()=>[]};}});});
  await click(app,'tray',['About Piper Desktop']);
  let page=await localPage(app,'Piper Desktop');
  const location=process.platform==='darwin'?'Running on this Mac':process.platform==='win32'?'Running in WSL (Ubuntu)':'Running on this computer';
  await page.getByText(location+' · version 0.1.4+4.g821c071',{exact:true}).waitFor();
  assert.match(await page.locator('.about-identity').innerText(),/Version 0.1.8/);
  assert.equal(await main.evaluate(()=>typeof window.piperAbout),'undefined');
  await click(app,'tray',['About Piper Desktop']);
  assert.equal(app.windows().filter(w=>w.url().endsWith('/about.html')).length,1);
  await page.getByRole('button',{name:'Copy Details',exact:true}).click();
  const details=await app.evaluate(({clipboard})=>clipboard.readText());
  assert.match(details,/Piper Desktop 0.1.8/);assert.match(details,/Piper service: 0.1.4\+4.g821c071/);
  assert.ok(!details.includes('fake-only-token')&&!details.includes('127.0.0.1')&&!details.includes('service: 1'));
  await page.getByRole('button',{name:'Check for Updates',exact:true}).click();
  await page.getByText('Checking for updates…',{exact:true}).waitFor();
  await page.getByText('Piper Desktop is up to date',{exact:true}).waitFor();
  await app.evaluate(({nativeTheme})=>{nativeTheme.themeSource='dark';nativeTheme.emit('updated');});
  await page.emulateMedia({colorScheme:'dark'});
  await page.waitForFunction(()=>matchMedia('(prefers-color-scheme: dark)').matches);
  await page.waitForFunction(()=>Math.abs(innerHeight - (38 + document.getElementById('about').scrollHeight)) < 2);
  assert.ok((await page.evaluate(()=>innerHeight)) < 560,'About fits its content rather than leaving a blank footer gap');
  await shot(page,'About-current-dark');
  if(process.env.PIPER_E2E_AGENT_BROWSER==='1'){
    const browser=process.env.AGENT_BROWSER||'agent-browser', args=['--session','piper-about-'+process.pid,'--cdp',String(cdp)];
    const tabs=JSON.parse(execFileSync(browser,[...args,'tab','list','--json'],{encoding:'utf8',timeout:30000}));
    const target=tabs.data.tabs.find(t=>t.url.endsWith('/about.html'));assert.ok(target,JSON.stringify(tabs));
    execFileSync(browser,[...args,'tab',target.targetId||target.id||String(target.index)],{encoding:'utf8',timeout:30000});
    const snapshot=execFileSync(browser,[...args,'snapshot','-i'],{encoding:'utf8',timeout:30000});assert.match(snapshot,/Check Again/);assert.match(snapshot,/Copy Details/);
    const checkRef=/button "Check Again" \[ref=([^\]]+)\]/.exec(snapshot);assert.ok(checkRef,snapshot);
    execFileSync(browser,[...args,'click','@'+checkRef[1]],{encoding:'utf8',timeout:30000});
    const file=path.join(shotDir,'About-agent-browser.png');execFileSync(browser,[...args,'screenshot',file],{timeout:30000});
    console.log(shotLine('About-agent-browser',await readFile(file)));
  }
  await app.evaluate(({nativeTheme})=>{nativeTheme.themeSource='light';nativeTheme.emit('updated');});
  await page.emulateMedia({colorScheme:'light'});
  await page.waitForFunction(()=>!matchMedia('(prefers-color-scheme: dark)').matches);
  await page.getByText('Piper Desktop is up to date',{exact:true}).waitFor();
  await shot(page,'About-current-light');
  await app.evaluate(({app})=>app.piperTest.setUpdates({version:'0.1.8',packaged:true,platform:'darwin',signed:false,fetch:async()=>{throw Error('private error');}}));
  await click(app,'tray',['Check for Updates…']);
  await page.getByText('Couldn’t check for updates',{exact:true}).waitFor();await shot(page,'About-error');
  assert.ok(!await page.getByText('private error').count());
  await app.evaluate(({app})=>app.piperTest.setUpdates({version:'0.1.8',packaged:true,platform:'darwin',signed:false,fetch:async()=>({ok:true,json:async()=>[{tag_name:'desktop-v0.1.9',html_url:'https://github.com/Kamest/Piper/releases/tag/desktop-v0.1.9'}]})}));
  await page.getByRole('button',{name:'Check for Updates',exact:true}).click();
  await page.getByText('Piper Desktop 0.1.9 is available',{exact:true}).waitFor();await shot(page,'About-available');
  const before=(await app.evaluate(({app})=>app.piperTest.externals())).length;
  await page.getByRole('button',{name:'Download 0.1.9',exact:true}).click();
  await until(async()=>(await app.evaluate(({app})=>app.piperTest.externals())).length>before,'download link');
  assert.equal((await app.evaluate(({app})=>app.piperTest.externals())).at(-1),'https://github.com/Kamest/Piper/releases/tag/desktop-v0.1.9');
  await writeFile(f('state.json'),JSON.stringify({version:1,sessions:[]}));await app.evaluate(({app})=>app.piperTest.poll());
  await page.getByText(location+' · version not reported',{exact:true}).waitFor();
  await page.getByText('Update the Piper service to see its version. It updates separately from the desktop app.',{exact:true}).waitFor();
  await rm(f('online'));await app.evaluate(({app})=>app.piperTest.poll());
  await page.getByText('Not reachable. Desktop details and updates still work.',{exact:true}).waitFor();await shot(page,'About-offline');
  assert.equal((await posts(f)).length,0,'About must not restart or modify the service');
  await page.keyboard.press('Escape').catch(error=>{if(!page.isClosed())throw error;});await until(()=>Promise.resolve(page.isClosed()),'About closes');
  await localPage(app,'Piper is not answering');
});

test('About automatic download shows progress, retry and ready; Store uses its own action', {timeout:75000}, async t=>{
  const {app}=await fixture(t,{installed:true});await (await app.firstWindow()).getByRole('heading',{name:'Piper fake service'}).waitFor();
  await app.evaluate(async({app})=>{
    const client={listeners:{},on(name,fn){this.listeners[name]=fn;},emit(name,value){this.listeners[name]?.(value);}};app.aboutUpdater=client;
    client.setFeedURL=()=>{};client.checkForUpdates=async()=>({updateInfo:{version:'0.1.9'}});
    client.downloadUpdate=async()=>{client.emit('download-progress',{percent:42});};
    client.quitAndInstall=(...args)=>{app.aboutInstalled=args;};
    app.piperTest.setUpdates({version:'0.1.8',packaged:true,platform:'linux',updater:()=>client,fetch:async()=>({ok:true,json:async()=>[{tag_name:'desktop-v0.1.9',html_url:'https://github.com/Kamest/Piper/releases/tag/desktop-v0.1.9'}]})});
  });
  await click(app,'tray',['Check for Updates…']);const page=await localPage(app,'Piper Desktop');
  await page.getByRole('button',{name:'Download 0.1.9',exact:true}).click();
  await page.locator('progress[value="42"]').waitFor();await shot(page,'About-downloading');
  assert.equal(await app.evaluate(({app})=>app.aboutInstalled),undefined);
  await app.evaluate(({app})=>app.aboutUpdater.emit('error',Error('private error')));
  await page.getByText('Couldn’t download Piper Desktop 0.1.9',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Try Again',exact:true}).click();await page.locator('progress[value="42"]').waitFor();
  await app.evaluate(({app})=>app.aboutUpdater.emit('update-downloaded'));
  await page.getByText('Piper Desktop 0.1.9 is ready',{exact:true}).waitFor();await shot(page,'About-ready');
  await page.getByRole('button',{name:'Restart to Update',exact:true}).click();
  await until(async()=>!!await app.evaluate(({app})=>app.aboutInstalled),'install request');
  assert.deepEqual(await app.evaluate(({app})=>app.aboutInstalled),[false,true]);
  await app.evaluate(({app})=>app.piperTest.setUpdates({version:'0.1.8',packaged:true,windowsStore:true,fetch:async()=>{throw Error('Store must not fetch');}}));
  await page.getByText('Updates come from the Microsoft Store',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Open Microsoft Store',exact:true}).click();
  await until(async()=>(await app.evaluate(({app})=>app.piperTest.externals())).includes('ms-windows-store://downloadsandupdates'),'Store');
});
