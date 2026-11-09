"""Slow inventory and history reads never multiply from redraws or timer ticks."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
from unittest import mock

import _env
import common
import features
import piperd
import sessions


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless polling checks')
class PollingPerformanceBrowserTest(unittest.TestCase):
    def test_folded_historical_sessions_keep_search_text_without_requesting_git_previews(self):
        manager = _env.manager(self)
        for i in range(20):
            session = sessions.Session(kind='advisor', state='exited', title='Historical %02d' % i, task='search-only-%s' % i,
                                       created=1_790_000_000, since=1_790_000_000)
            manager.sessions[session.id] = session
        self.enterContext(mock.patch.object(manager, 'conversation', side_effect=lambda sid, *args, **kw: {
            'turns': [{'role': 'assistant', 'text': 'search-only-%s' % list(manager.sessions).index(sid)}], 'usage': None}))
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-board-%s' % os.getpid()
        def cli(*args):
            run = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                 capture_output=True, text=True, timeout=40)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token('gui'), '[token]'))
            value = json.loads(run.stdout)
            self.assertTrue(value['success'])
            return value.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector("#actbar")')
        cli('eval', "localStorage.setItem('piper.layout',JSON.stringify({v:1,left:'queue',leftBack:'queue',center:{kind:'single'},right:'hidden'}))")
        cli('reload')
        cli('wait', '--fn', '!!document.querySelector("#queue .ended .q-fold")&&!document.body.classList.contains("queue-mini")&&!document.body.classList.contains("no-queue")')
        cli('eval', "window.boardRequests=[]; import('/piper/dist/api.js').then(({api})=>{const original=api.board;api.board=async(...args)=>{window.boardRequests.push(args);return original(...args)}})")
        cli('wait', '--fn', 'window.boardRequests.length>0')
        folded = cli('eval', '({width:document.querySelector("#queue").getBoundingClientRect().width,height:document.querySelector("#queue").getBoundingClientRect().height,requests:window.boardRequests,rows:[...document.querySelectorAll("#queue .ended [data-key]")].map(el=>el.dataset.key)})')
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'board-folded.png')
        cli('screenshot', str(path))
        print('BOARD_FOLDED_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)
        self.assertGreater(folded['width'], 200, 'the tested queue must actually be visible')
        self.assertGreater(folded['height'], 200)
        self.assertIsInstance(folded['requests'][-1][1] if len(folded['requests'][-1]) > 1 else None, list)
        self.assertLessEqual(len(folded['requests'][-1][1]), 1, 'only the selected historical session needs Git')
        cli('eval', 'window.boardRequests=[];document.querySelector("#queue .ended .q-fold").click()')
        cli('wait', '--fn', 'window.boardRequests.some(args=>args[1]?.length===20)')
        cli('eval', 'window.boardRequests=[];const input=document.querySelector("#queue .q-search");input.value="search-only-19";input.dispatchEvent(new Event("input",{bubbles:true}))')
        cli('wait', '--fn', 'document.querySelectorAll("#queue .ended [data-key]").length===1&&window.boardRequests.length>0')
        search = cli('eval', '({requests:window.boardRequests, text:document.querySelector("#queue .ended").textContent})')
        self.assertIn('search-only-19', search['text'])
        self.assertLessEqual(len(search['requests'][-1][1]), 2, 'search result plus the selected session')
        print('BOARD_SELECTION_RESULTS ' + json.dumps({'folded': folded, 'search': search}, sort_keys=True), flush=True)
        slow = cli('eval', r'''(async()=>{
          const {api}=await import('/piper/dist/api.js'),original=api.board;
          let release,active=0,max=0,first=true;
          const calls=[],tick=ms=>new Promise(resolve=>setTimeout(resolve,ms));
          const wait=async check=>{for(let i=0;i<100;i++){if(check())return;await tick(100)}throw Error('Board refresh did not settle')};
          api.board=async(...args)=>{
            calls.push(args);active++;max=Math.max(max,active);
            try{if(first){first=false;await new Promise(resolve=>release=resolve)}return await original(...args)}finally{active--}
          };
          try{
            await wait(()=>!!release);
            const search=text=>{const input=document.querySelector('#queue .q-search');input.value=text;input.dispatchEvent(new Event('input',{bubbles:true}))};
            search('search-only-18');
            await tick(1200);
            search('search-only-17');
            await tick(1200);release();
            await wait(()=>calls.length>=2&&active===0);
            return{max,calls};
          }finally{api.board=original;release?.()}
        })()''')
        self.assertEqual(slow['max'], 1, 'slow requests must never overlap')
        self.assertIn(list(manager.sessions)[17], slow['calls'][-1][1], 'latest selection is queued during an in-flight refresh')
        hidden = cli('eval', r'''(async()=>{
          window.boardRequests=[];
          Object.defineProperty(document,'hidden',{value:true,configurable:true});
          try{
            const input=document.querySelector('#queue .q-search');input.value='search-only-16';
            input.dispatchEvent(new Event('input',{bubbles:true}));
            await new Promise(resolve=>setTimeout(resolve,1500));return window.boardRequests.length;
          }finally{delete document.hidden}
        })()''')
        self.assertEqual(hidden, 0, 'hidden clients do not start delayed preview reads')



    def test_visiting_ended_sessions_bounds_retained_terminals_and_keeps_live_buffers(self):
        manager = _env.manager(self)
        for i in range(22):
            item = sessions.Session(kind='advisor', state='ready' if i < 2 else 'exited',
                                    task='terminal-cache-%s' % i, created=1_790_000_000 + i)
            manager.sessions[item.id] = item
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-term-cache-%s' % os.getpid()
        def cli(*args):
            run = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                 capture_output=True, text=True, timeout=40)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token('gui'), '[token]'))
            result = json.loads(run.stdout)
            self.assertTrue(result['success'])
            return result.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector("#queue .ended .q-fold")')
        cli('eval', "localStorage.setItem('piper.layout',JSON.stringify({v:1,left:'queue',leftBack:'queue',center:{kind:'single'},right:'hidden'}))")
        cli('reload')
        try:
            cli('wait', '--fn', '!!document.querySelector("#queue .ended .q-fold")&&document.querySelector("#queue").getBoundingClientRect().width>200')
        except AssertionError:
            print('TERMINAL_CACHE_STARTUP ' + json.dumps(cli('eval', '({body:document.body.className,text:document.body.innerText.slice(0,2000),queue:!!document.querySelector("#queue")})')), flush=True)
            raise
        ids = list(manager.sessions)
        result = cli('eval', r'''(async()=>{
          const {api}=await import('/piper/dist/api.js'),{installTransport}=await import('/piper/dist/transport.js');
          const {TermView}=await import('/piper/dist/term.js');
          const instances=new Map(),connections=new Map(),originalState=api.state,originalMount=TermView.prototype.mount;
          const tick=ms=>new Promise(resolve=>setTimeout(resolve,ms));
          const wait=async check=>{for(let i=0;i<100;i++){if(check())return;await tick(30)}throw Error('Terminal cache fixture did not settle')};
          class Socket{
            readyState=0;binaryType='arraybuffer';onopen=null;onmessage=null;onclose=null;onerror=null;
            constructor(url){this.id=decodeURIComponent(new URL(url).pathname.split('/').at(-1));connections.set(this.id,(connections.get(this.id)||0)+1);setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
            send(raw){const msg=JSON.parse(raw);if(!msg.auth)return;
              queueMicrotask(()=>{
                this.onmessage?.({data:JSON.stringify({t:'hello',info:{cols:100,rows:24,tty:0,pid:42,started:1,replay_geometry:true}})});
                this.onmessage?.({data:new TextEncoder().encode('saved terminal '+this.id+'\r\n').buffer});
                this.onmessage?.({data:JSON.stringify({t:'replay-end'})});
              })}
            close(){this.readyState=3;this.onclose?.({code:1000})}
          }
          installTransport({fetch:(...args)=>fetch(...args),socket:url=>new Socket(url)});
          api.state=async(...args)=>{const r=await originalState(...args);if(r.data)r.data.sessions.forEach(s=>s.pid=42);return r};
          TermView.prototype.mount=function(...args){instances.set(this.id,this);return originalMount.apply(this,args)};
          const ids=IDS;
          try{
            await wait(()=>instances.has(ids[0]));
            const visit=async(id,section)=>{
              const row=document.querySelector('#queue [data-key="'+section+id+'"]');if(!row)throw Error('missing row '+id);
              row.click();await wait(()=>instances.has(id)&&!instances.get(id).el.classList.contains('hidden'));await tick(30);
            };
            await visit(ids[1],'s:');
            const live=[instances.get(ids[0]),instances.get(ids[1])];
            await wait(()=>live.every(v=>v.term.buffer.active.getLine(0)?.translateToString().includes(v.id)));
            document.querySelector('#queue .ended .q-fold').click();
            for(const id of ids.slice(2))await visit(id,'ended:');
            const retained=window.__piperTermDiag().length;
            const liveKept=live.every(v=>!v.closed&&v.term.buffer.active.getLine(0).translateToString().includes(v.id));
            const current=instances.get(ids.at(-1)),selectedKept=!current.closed;
            const recent=instances.get(ids[14]);await visit(ids[14],'ended:');
            const before=connections.get(ids[2]);await visit(ids[2],'ended:');
            await wait(()=>instances.get(ids[2]).term.buffer.active.getLine(0)?.translateToString().includes(ids[2]));
            await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
            return{retained,liveKept,selectedKept,recentKept:instances.get(ids[14])===recent&&!recent.closed,reopened:(connections.get(ids[2])||0)>before,
              afterReopen:window.__piperTermDiag().length};
          }finally{api.state=originalState;TermView.prototype.mount=originalMount}
        })()'''.replace('IDS', json.dumps(ids)))
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'terminal-cache.png')
        cli('screenshot', str(path))
        print('TERMINAL_CACHE_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)
        print('TERMINAL_CACHE_RESULTS ' + json.dumps(result, sort_keys=True), flush=True)
        self.assertLessEqual(result['retained'], 10, 'eight ended renderers plus both live terminals')
        self.assertTrue(result['liveKept'])
        self.assertTrue(result['selectedKept'])
        self.assertTrue(result['recentKept'], 'recently visited historical terminals take priority over older ones')
        self.assertTrue(result['reopened'], 'an evicted ended terminal must replay when opened again')
        self.assertLessEqual(result['afterReopen'], 10)

    def test_slow_file_refreshes_join_reads_preserve_manual_reload_and_project_pages(self):
        manager = _env.manager(self)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-fr-%s' % os.getpid()
        def cli(*args):
            r = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                               capture_output=True, text=True, timeout=40)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token('gui'), '[token]'))
            value = json.loads(r.stdout)
            self.assertTrue(value['success'])
            return value.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector("#actbar")')
        result = cli('eval', r'''(async()=>{
          const {createFiles}=await import('/piper/dist/files.js');
          const {api}=await import('/piper/dist/api.js');
          const {loadCM}=await import('/piper/dist/cmeditor.js');
          const cm=await loadCM(),original={...api},pending=[],errors=[];
          const tick=(ms=20)=>new Promise(r=>setTimeout(r,ms));
          const wait=async check=>{for(let i=0;i<100;i++){if(check())return;await tick()}throw Error('File fixture did not settle: '+JSON.stringify({errors,calls,body:scope.textContent}))};
          let slow=false,version=1,active=0,max=0,calls=0,working=false,pageRelease;
          const pageProjects=[];
          api.list=async()=>({data:{path:'.',root:'/fixture',entries:[]}});
          api.ideaStatus=async()=>({data:{available:false}});
          api.marks=async()=>({data:{path:'demo.txt',added:[],changed:[],deleted:[],untracked:false}});
          api.blame=async()=>({data:{lines:[]}});
          api.read=async(key,path,offset=0)=>{
            if(path==='multi.txt'){
              if(!offset)await new Promise(resolve=>{pageRelease=resolve});
              else pageProjects.push(key);
              return{data:{path,total_lines:2,offset,lines:[`${offset+1}\tpage`],sha:'multi'}};
            }
            const v=version;calls++;active++;max=Math.max(max,active);
            if(slow)await new Promise(resolve=>pending.push(resolve));
            active--;return{data:{path,total_lines:1,offset:0,lines:[`1\tversion-${v}`],sha:'version-'+v}};
          };
          const scope=document.createElement('div');scope.style.cssText='position:fixed;inset:60px;z-index:100;background:#13151a';document.body.append(scope);
          const files=createFiles({toast(text){errors.push(text)},modal(){},closeModal(){},projects:()=>[],changed:()=>new Set(),
            insert:()=>false,diff(){},addNote:()=>false,working:()=>working,projectPath:()=>'/fixture'});
          scope.append(files.el);
          const editor=()=>cm.EditorView.findFromDOM(scope.querySelector('.cm-editor'));
          try{
            files.show('alpha');files.open('demo.txt');
            await wait(()=>!!scope.querySelector('.cm-editor'));await tick(50);
            calls=0;max=0;slow=true;
            files.refreshMarks();await wait(()=>active===1);
            files.refreshMarks();await tick(300);files.refreshMarks();await tick(300);
            files.sessionEdited('alpha','fixture',1,['demo.txt']);
            version=2;files.sessionEdited('alpha','fixture',2,['demo.txt']);
            slow=false;pending.splice(0).forEach(resolve=>resolve());
            await wait(()=>active===0&&editor().state.doc.toString()==='version-2');
            const background={max,calls,text:editor().state.doc.toString()};
            working=true;files.refreshWorking();
            editor().dispatch({changes:{from:0,to:editor().state.doc.length,insert:'unsaved draft'}});
            max=0;slow=true;version=3;
            files.sessionEdited('alpha','fixture',3,['demo.txt']);await wait(()=>active===1);
            const draft=editor().state.doc.toString();
            scope.querySelector('.fhead .ib.more').click();
            await wait(()=>[...document.querySelectorAll('button')].some(b=>b.textContent==='Reload from disk'));
            [...document.querySelectorAll('button')].find(b=>b.textContent==='Reload from disk').click();
            await tick(30);version=4;slow=false;pending.splice(0).forEach(resolve=>resolve());
            await wait(()=>active===0);await tick(50);
            const manual={max,draft,text:editor().state.doc.toString()};
            files.open('multi.txt');await wait(()=>!!pageRelease);files.show('beta');pageRelease();
            await wait(()=>pageProjects.length===1);await tick(50);
            files.show('alpha');
            return{background,manual,pageProjects};
          }finally{Object.assign(api,original)}
        })()''')
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'file-reads.png')
        cli('screenshot', str(path))
        print('FILE_READS_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)
        print('FILE_READ_RESULTS ' + json.dumps(result, sort_keys=True), flush=True)
        self.assertEqual(result['background'], {'max': 1, 'calls': 2, 'text': 'version-2'})
        self.assertEqual(result['manual'], {'max': 1, 'draft': 'unsaved draft', 'text': 'version-4'})
        self.assertEqual(result['pageProjects'], ['alpha'])

    def test_removed_servers_release_setup_panels_in_the_real_browser(self):
        manager = _env.manager(self)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-sl-%s' % os.getpid()
        def cli(*args):
            result = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
            value = json.loads(result.stdout)
            self.assertTrue(value['success'])
            return value.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector("#actbar")')
        result = cli('eval', r'''(async()=>{
          const {createServerSettings}=await import('/piper/dist/environments.js');
          const original={add:document.addEventListener,remove:document.removeEventListener,fetch:window.fetch};
          const listeners=new Set();
          document.addEventListener=function(type,listener,options){
            if(type==='visibilitychange')listeners.add(listener);
            return original.add.call(this,type,listener,options);
          };
          document.removeEventListener=function(type,listener,options){
            if(type==='visibilitychange')listeners.delete(listener);
            return original.remove.call(this,type,listener,options);
          };
          const scope=document.createElement('div');scope.style.cssText='position:fixed;inset:60px;z-index:100;background:#13151a;padding:24px;overflow:auto';
          document.body.append(scope);
          const modal=document.createElement('div');scope.append(modal);
          const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
          const wait=async check=>{for(let i=0;i<100;i++){if(check())return;await tick()}throw Error('Fixture did not settle')};
          const click=(root,text)=>{const b=[...root.querySelectorAll('button')].find(b=>b.textContent===text);if(!b)throw Error('Missing '+text);b.click()};
          let removed=0;
          try{
            for(let i=0;i<20;i++){
              const server={id:'lifecycle-'+i,label:'Build server',host:'build.example.com',project_path:'/srv/projects',mode:'service'};
              let present=true;
              window.fetch=async(input,init)=>{
                const path=new URL(String(input),location.href).pathname;
                if(path.endsWith('/remove')){present=false;removed++;return new Response('{}')}
                if(path.endsWith('/setup'))return new Response(JSON.stringify({environment:server.id,status:'done',updated:1,projects:[],steps:[]}));
                if(path==='/api/environments')return new Response(JSON.stringify({environments:present?[server]:[],providers:[]}));
                return original.fetch.call(window,input,init);
              };
              const settings=createServerSettings({changed(){},toast(){},modal:(_title,...nodes)=>modal.replaceChildren(...nodes),closeModal:()=>modal.replaceChildren()});
              scope.append(settings.el);settings.show();
              await wait(()=>!!settings.el.querySelector('.server-row'));
              settings.el.querySelector('.server-row').click();
              click(settings.el,'Remove server…');click(modal,'Remove connection');
              await wait(()=>settings.count()===0&&settings.el.textContent.includes('No servers yet'));
              if(i<19)settings.el.remove();
            }
            document.dispatchEvent(new Event('visibilitychange'));await tick();
            return{removed,retainedListeners:listeners.size};
          }finally{
            document.addEventListener=original.add;document.removeEventListener=original.remove;window.fetch=original.fetch;
          }
        })()''')
        self.assertEqual(result, {'removed': 20, 'retainedListeners': 0})
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'server-lifecycle.png')
        cli('screenshot', str(path))
        print('SERVER_LIFECYCLE_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    def test_disposed_terminals_release_global_listeners_and_resize_observers(self):
        manager = _env.manager(self)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-term-lifecycle-%s' % os.getpid()
        def cli(*args):
            result = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
            value = json.loads(result.stdout)
            self.assertTrue(value['success'])
            return value.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector("#actbar")')
        result = cli('eval', r'''(async()=>{
          const {TermView,TERM_FLAGS}=await import('/piper/dist/term.js');
          const original={add:window.addEventListener,remove:window.removeEventListener,resize:window.ResizeObserver,
                          armEnter:TERM_FLAGS.armEnter};
          const mouseups=new Set(),observing=new Set();
          window.addEventListener=function(type,listener,options){
            if(type==='mouseup')mouseups.add(listener);
            return original.add.call(this,type,listener,options);
          };
          window.removeEventListener=function(type,listener,options){
            if(type==='mouseup')mouseups.delete(listener);
            return original.remove.call(this,type,listener,options);
          };
          window.ResizeObserver=class extends original.resize{
            observe(...args){observing.add(this);return super.observe(...args)}
            disconnect(){observing.delete(this);return super.disconnect()}
          };
          const host=document.createElement('div');host.style.cssText='position:fixed;inset:100px;z-index:100;background:#13151a';
          document.body.append(host);
          let lateCopies=0;
          try{
            TERM_FLAGS.armEnter=true;
            for(let i=0;i<20;i++){
              const view=new TermView('lifecycle-'+i,'/fixture',{
                onRefused(){},onExit(){},onConnection(){},onImage:async()=>''});
              view.connect=()=>{}; // lifecycle of the actual renderer; no live terminal socket
              view.armCopy=()=>{lateCopies++};
              view.mount(host);
              view.host.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
              view.dispose();
            }
            document.body.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
            await new Promise(resolve=>setTimeout(resolve,120));
            return {retainedMouseups:mouseups.size,retainedObservers:observing.size,lateCopies,
                    remainingElements:host.childElementCount};
          }finally{
            host.remove();TERM_FLAGS.armEnter=original.armEnter;
            window.addEventListener=original.add;window.removeEventListener=original.remove;window.ResizeObserver=original.resize;
          }
        })()''')
        self.assertEqual(result, {'retainedMouseups': 0, 'retainedObservers': 0, 'lateCopies': 0, 'remainingElements': 0})
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'terminal-lifecycle.png')
        cli('screenshot', str(path))
        print('TERMINAL_LIFECYCLE_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    def test_slow_reads_are_shared_and_header_does_not_scan_http_files(self):
        manager = _env.manager(self)
        self.addCleanup(_env.clear_sessions, manager)
        root = _env.make_project('polling', {'README.md': 'Synthetic polling fixture\n'})
        _env.register('acme/shop', root)
        session = sessions.Session(project='acme/shop', path=root, title='Polling fixture', state='ready')
        manager.sessions[session.id] = session
        session.save()
        features.save({'http': True}, onboarded=True)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        scans = self.enterContext(mock.patch.object(manager, 'http_gui_files', wraps=manager.http_gui_files))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'piper-polling-%s' % os.getpid()
        def cli(*args):
            result = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
            value = json.loads(result.stdout)
            self.assertTrue(value['success'])
            return value.get('data', {}).get('result')
        self.addCleanup(cli, 'close')
        cli('open', piperd.gui_url())
        cli('wait', '--fn', '!!document.querySelector(%s)' % json.dumps('.qm[data-sid="%s"]' % session.id))
        cli('click', '.qm[data-sid="%s"]' % session.id)
        cli('wait', '--fn', '!!document.querySelector(".head nav.tabs")')
        self.assertEqual(scans.call_count, 0, 'rendering a session header must not inventory HTTP files')
        result = cli('eval', r'''(async()=>{
          const {createDbSection}=await import('/piper/dist/dbsection.js');
          const {createDbView,dbApi}=await import('/piper/dist/dbview.js');
          const {createHttpPanel}=await import('/piper/dist/httppanel.js');
          const {httpApi}=await import('/piper/dist/httpview.js');
          const {createStack}=await import('/piper/dist/stack.js');
          const {api}=await import('/piper/dist/api.js');
          const check=(ok,message)=>{if(!ok)throw Error(message)};
          const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
          const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}};
          const host=document.createElement('div');document.body.append(host);
          const modal=document.createElement('div');host.append(modal);
          const deps={pending:()=>[],open(){},toast(){},projects:()=>[],closeModal:()=>modal.replaceChildren(),
            modal:(_title,...nodes)=>modal.replaceChildren(...nodes.filter(Boolean))};
          const original={interval:window.setInterval,now:Date.now,db:{...dbApi},http:{...httpApi},stack:api.stack,fetch:window.fetch};
          const timers=[];window.setInterval=(fn,ms)=>{timers.push({fn,ms});return 0};
          let now=original.now();Date.now=()=>now;
          const fire=ms=>{for(const timer of timers.filter(t=>t.ms===ms))timer.fn()};
          const connections=new Map(),histories=new Map(),runs=new Map(),files=new Map();
          const counts={connections:0,history:0,running:0,files:0,logs:0};
          const pending=(map,id,kind)=>{counts[kind]++;const d=deferred();map.set(id,d);return d.promise};
          try{
            dbApi.connections=(_p,id)=>pending(connections,id,'connections');
            dbApi.history=(_p,id)=>pending(histories,id,'history');
            dbApi.running=(_p,_c,id)=>pending(runs,id,'running');
            const section=createDbSection(deps);host.append(section.el);
            for(let i=0;i<20;i++)section.show('alpha');
            now+=60000;for(let i=0;i<20;i++)fire(1000);
            check(counts.connections===1&&counts.history===1,'DB redraw/timer duplicated pending reads');
            section.show('beta');section.show('alpha');
            check(counts.connections===2,'DB returning to session duplicated its pending read');
            connections.get('beta').reject(Error('stale beta error'));await tick();
            section.show('beta');section.show('alpha');
            check(counts.connections===2,'DB failure released guard before sibling history completed');
            histories.get('beta').resolve({data:{queries:[]}});
            await tick();check(!section.el.textContent.includes('stale beta error'),'stale DB error crossed sessions');
            connections.get('alpha').resolve({data:{connections:[],project:'alpha'}});histories.get('alpha').resolve({data:{queries:[]}});
            await tick();now+=11000;for(let i=0;i<20;i++)fire(1000);
            check(counts.running===1,'running query polls overlapped');
            runs.get('alpha').resolve({data:{running:[]}});await tick();now+=11000;fire(1000);
            check(counts.running===2,'running poll did not resume after completion');
            runs.get('alpha').reject(Error('temporary error'));await tick();section.el.remove();

            httpApi.files=p=>pending(files,p,'files');
            const panel=createHttpPanel({...deps,openFile(){}});host.append(panel.el);
            for(let i=0;i<20;i++)panel.show('alpha');
            panel.el.querySelector('button[aria-label="look for .http files again"]').click();
            panel.show('beta');panel.show('alpha');
            check(counts.files===2,'HTTP redraw/refresh duplicated a pending scan');
            files.get('beta').reject(Error('stale HTTP error'));await tick();
            check(!panel.el.textContent.includes('stale HTTP error'),'stale HTTP error crossed projects');
            files.get('alpha').resolve({data:{files:[]}});await tick();
            panel.el.querySelector('button[aria-label="look for .http files again"]').click();
            check(counts.files===3,'explicit HTTP refresh stopped working');
            files.get('alpha').resolve({data:{files:[]}});await tick();panel.el.remove();

            dbApi.all=async()=>({data:{projects:[],orphans:[],status:{}}});
            dbApi.history=()=>pending(histories,'view','history');
            const db=createDbView(deps);host.append(db.el);db.show();await tick();
            const historyAt=counts.history;for(let i=0;i<20;i++)fire(5000);
            check(counts.history===historyAt,'DB history polls overlapped');
            histories.get('view').resolve({data:{queries:[]}});await tick();fire(5000);
            check(counts.history===historyAt+1,'DB history poll did not resume');
            histories.get('view').resolve({data:{queries:[]}});await tick();db.el.remove();

            api.stack=async()=>({data:{stack:true,up:true,jobs:[],services:[{name:'web',state:'running',ports:[],links:[]}]}});
            let log=deferred();window.fetch=(url,opts)=>String(url).includes('/api/stack/log')?(counts.logs++,log.promise):original.fetch(url,opts);
            const stack=createStack(deps);host.append(stack.el);stack.show('alpha');await tick();
            stack.el.querySelector('[aria-label="logs of web"]').click();
            const auto=modal.querySelector('input[type=checkbox]');auto.checked=true;auto.dispatchEvent(new Event('change'));
            for(let i=0;i<20;i++)fire(3000);
            check(counts.logs===1,'stack log timer duplicated pending reads');
            const tail=modal.querySelector('select');tail.value='2000';tail.dispatchEvent(new Event('change'));
            check(counts.logs===1,'tail changes must wait for the current read');
            const first=log;log=deferred();first.resolve(new Response('fixture log'));await tick();
            check(counts.logs===2,'latest tail selection was lost during the read');
            log.resolve(new Response('next log'));await tick();log=deferred();fire(3000);
            check(counts.logs===3,'stack log refresh did not resume');log.resolve(new Response('last log'));await tick();
            return {ok:true,counts};
          }finally{
            window.setInterval=original.interval;Date.now=original.now;Object.assign(dbApi,original.db);Object.assign(httpApi,original.http);
            api.stack=original.stack;window.fetch=original.fetch;host.remove();
          }
        })()''')
        self.assertTrue(result['ok'])
        cli('click', '.head nav.tabs button[title^="every .http file"]')
        cli('wait', '--fn', '!!document.querySelector(".httppanel .hp-empty")')
        self.assertEqual(scans.call_count, 1, 'HTTP inventory starts only when the panel is opened')
        path = Path(os.environ['TEST_SANDBOX_WORK'], 'polling-performance.png')
        cli('screenshot', str(path))
        print('POLLING_PNG ' + base64.b64encode(path.read_bytes()).decode(), flush=True)
