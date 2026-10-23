import './_sandbox.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {readFileSync} from 'node:fs';

test('session references route through the real GUI with project files, HTTP, GitLab, DB and Piper links', {
  skip: !process.env.PIPER_LINK_FIXTURE, timeout: 240000,
}, () => {
  const fixture = JSON.parse(readFileSync(process.env.PIPER_LINK_FIXTURE, 'utf8'));
  const base = `http://127.0.0.1:${fixture.port}`;
  assert.notEqual(fixture.port,17895,'never the live daemon');
  const sid = fixture.sessions['grp/demo'], other = fixture.sessions['demo/hidden'];
  const hash = execFileSync('git',['-C',fixture.projects['grp/demo'],'rev-parse','HEAD'],{encoding:'utf8'}).trim();
  const name = `piper-reference-${process.pid}`;
  const ab = (...args) => {
    try { return execFileSync('agent-browser',['--session',name,...args],{encoding:'utf8',timeout:45000,stdio:'pipe'}).trim(); }
    catch { assert.fail(`agent-browser ${args[0]} failed`); }
  };
  const js = source => {
    let result;
    try { result = execFileSync('agent-browser',['--session',name,'eval','--stdin'],{input:source,encoding:'utf8',timeout:45000,stdio:'pipe'}).trim(); }
    catch { assert.fail('browser evaluation failed'); }
    assert.ok(!result.startsWith('✗'), result.replaceAll(fixture.gui_token,'[redacted]'));
    return JSON.parse(result);
  };
  const wait = expression => ab('wait','--fn',`!!(${expression})`);
  const route = url => js(`(()=>{const a=document.createElement('a');a.href=${JSON.stringify(url)};a.textContent='test navigation';document.body.append(a);a.click();a.remove();return true;})()`);
  const conversation = () => {
    route(`${base}/?session=${sid}&view=convo`);
    wait(`Array.from(document.querySelectorAll('.convo .tmd button.link')).some(b=>b.textContent==='App.java:5')`);
  };
  const click = label => js(`(()=>{const b=Array.from(document.querySelectorAll('.convo .tmd button.link')).find(b=>b.textContent===${JSON.stringify(label)});if(!b)throw Error('missing reference');b.click();return true;})()`);
  try {
    ab('open',base);
    js(`localStorage.setItem('piper.token',${JSON.stringify(fixture.gui_token)});true`);
    ab('reload');
    wait('document.querySelector("nav[aria-label=Piper]")');
    js(`(async()=>{
      const {call,api}=await import('/piper/dist/api.js');
      await call('POST','/api/settings',{section:'sessions',values:{open_links_on_host:true},confirm:['open_links_on_host']});
      await call('POST','/api/settings',{section:'interface',values:{host_link_browser:'chrome'}});
      await api.fsCreate('grp/demo','report.pdf','file','%PDF-1.7 fixture').catch(e=>{if(!e.message.includes('already exists'))throw e;});
      window._linkFlags=(await import('/piper/dist/term.js')).TERM_FLAGS;
      window._linkRequests=[];
      const original=window.fetch.bind(window);
      const sid=${JSON.stringify(sid)};
      const text=${JSON.stringify(`src/main/java/App.java:5\n\nApp.java:5\n\napi/demo.http:5\n\nsrc/\n\nreport.pdf\n\n#123 !45 pipeline 77\n\npublic.users\n\ncommit ${hash}\n\nsession ${other}`)};
      const conn={name:'grp/demo/dev/test',project:'grp/demo',class:'dev',engine:'postgres',database:'test',user:'fixture',reach:'direct',boundary:'',has_password:true};
      const status={ready:true,python:'fixture',drivers:{postgres:'fixture'},installing:false,install_error:''};
      window.fetch=async (input,init={})=>{
        const u=new URL(typeof input==='string'?input:input.url,location.href);
        const path=u.pathname;
        window._linkRequests.push({path,query:u.search,body:init.body?JSON.parse(init.body):null});
        let data;
        if(path==='/api/sessions/'+sid+'/conversation')data={agent:'claude',turns:[{role:'assistant',text}],usage:null};
        if(path==='/api/db/connections')data={project:'grp/demo',connections:[conn],disabled:[],status,tunnel_targets:[],idea:[]};
        if(path==='/api/db/all')data={projects:[{project:'grp/demo',title:'Demo',root:'/fixture',connections:[conn]}],orphans:[],status};
        if(path==='/api/db/meta') {
          const what=u.searchParams.get('what');
          data=what==='schemas'?{schemas:['public'],default:'public'}:what==='objects'?{schema:'public',objects:[{name:'users',kind:'table',engine:'postgres',rows:1,comment:''}],routines:[]}:
            {name:'users',schema:'public',columns:[{name:'id',type:'integer',nullable:false,key:'PRI',default:null,extra:'',comment:''}],primary_key:['id'],foreign_keys:[],referenced_by:[],indexes:[]};
        }
        if(path==='/api/db/query')data={result_id:'fixture',columns:[{name:'id',type:'integer'}],rows:[[7]],offset:0,row_count:1,more:false};
        if(path==='/api/db/history')data={queries:[]};
        if(path==='/api/db/running')data={running:[]};
        if(data!==undefined)return new Response(JSON.stringify(data),{headers:{'Content-Type':'application/json'}});
        return original(input,init);
      };
      return true;
    })()`);
    wait('window._linkFlags.hostLinks');
    conversation();
    click('App.java:5');
    wait('document.querySelector(".files .cm-content")');
    assert.equal(js('document.querySelector(".files .crumbs strong")?.textContent'),'App.java');
    assert.ok(js('document.querySelector(".files .cm-content").textContent.includes("class App")'));
    conversation(); click('api/demo.http:5');
    wait(`document.querySelector('.httppanel [data-req="1"][aria-expanded="true"]')`);
    assert.equal(js('window._linkRequests.filter(r=>r.path==="/api/http/run").length'),0);
    conversation(); click('src/');
    wait(`document.querySelector('.files [data-nav="src/main/java"]')`);
    conversation(); click('report.pdf');
    wait('window._linkRequests.some(r=>r.path==="/api/fs/download")');
    conversation();
    for(const label of ['#123','!45','pipeline 77'])click(label);
    wait('window._linkRequests.filter(r=>r.path==="/api/open-url").length>=3');
    const urls=js('window._linkRequests.filter(r=>r.path==="/api/open-url").map(r=>r.body.url)');
    assert.ok(urls.some(u=>u.endsWith('/grp/demo/-/issues/123')));
    assert.ok(urls.some(u=>u.endsWith('/grp/demo/-/merge_requests/45')));
    assert.ok(urls.some(u=>u.endsWith('/grp/demo/-/pipelines/77')));
    const before=urls.length;
    route(`http://localhost:${fixture.port}/?session=${sid}&view=files&path=README.md`);
    wait('document.querySelector(".files .crumbs strong")?.textContent==="README.md"');
    assert.equal(js('window._linkRequests.filter(r=>r.path==="/api/open-url").length'),before,'Piper never reaches the host opener');
    conversation(); click('public.users');
    wait('window._linkRequests.some(r=>r.path==="/api/db/query")');
    assert.equal(js('window._linkRequests.filter(r=>r.path==="/api/db/query").at(-1).body.project'),'grp/demo');
    assert.equal(js('window._linkRequests.filter(r=>r.path==="/api/db/query").at(-1).body.schema'),'public');
    assert.equal(js('window._linkRequests.some(r=>r.path==="/api/db/execute")'),false);
    ab('screenshot',process.env.PIPER_LINK_SCREENSHOT || '/tmp/piper-reference-db.png');
    conversation(); click(hash);
    wait('document.querySelector(".hist .hrow.active")');
    assert.ok(js(`document.querySelector('.hist .hrow.active')?.title.includes(${JSON.stringify(hash)})`));
    js(`(()=>{const b=Array.from(document.querySelectorAll('#overlay button')).find(b=>b.textContent==='Close');b?.click();return true;})()`);
    conversation(); click(`session ${other}`);
    wait(`document.querySelector('.queue [data-sid="${other}"]')?.classList.contains('active') || document.querySelector('.term[data-sid="${other}"]:not(.hidden)')`);
    const opened=readFileSync(fixture.work+'/open.log','utf8');
    assert.ok(opened.includes('-a Google Chrome '),'the backend selected Chrome');
    assert.ok(!opened.includes(`localhost:${fixture.port}`),'the Piper alias never launched a browser');
  } catch (e) {
    console.log('Failed step:', e.message);
    console.log('GUI state:', js(`JSON.stringify({http:document.querySelector('.httppanel')?.textContent, alerts:document.querySelector('#notices')?.textContent, view:document.querySelector('nav.tabs')?.textContent})`));
    ab('screenshot', process.env.TMPDIR + '/reference-failure.png');
    throw e;
  } finally { ab('close'); }
});
