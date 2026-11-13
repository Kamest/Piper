"""Headless feedback flows against real compiled UI, HTTP routes and an isolated Manager."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _env
import common
import features
import piperd
import sessions
import stackview


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for the headless feedback flows")
class FeedbackBrowserTest(unittest.TestCase):
    def test_dev_stacks_pin_footer_reload_and_feature_toggle(self):
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        root = _env.make_project('stacks-pin', {'README.md':'Synthetic stack navigation fixture\n'})
        _env.register('acme/shop', root)
        inventory = []
        for parent in range(5):
            path = os.path.join(root, 'parent-%s' % parent)
            os.makedirs(path)
            inventory.append({'root':path,'name':'acme/parent-%s'%parent,'parent':'','running':2,'total':2,'can_stop':True})
            for child in range(4):
                nested = os.path.join(path,'child-%s'%child);os.makedirs(nested)
                inventory.append({'root':nested,'name':'child-%s'%child,'parent':path,'running':1,'total':1,'can_stop':True})
        class Provider:
            def up(self): return True
            def dashboard_url(self): return ''
            def worktree(self, *args, **kwargs): return None
            def running_stacks(self): return inventory
        self.enterContext(mock.patch.object(manager, '_stack', return_value=Provider()))
        self.enterContext(mock.patch.object(features, 'MODULES', [*features.MODULES, ('stack','Dev stacks','Fixture provider')]))
        self.enterContext(mock.patch.object(features, 'IDS', [*features.IDS, 'stack']))
        self.enterContext(mock.patch.dict(features.PRESETS, {'all':[*features.PRESETS['all'],'stack']}))
        features._CACHE.clear()
        _env.set_modules(self, stack=True, stack_overview=True)
        features.save(onboarded=True)
        # The exported mobile client includes this stylesheet; the desktop HTTP allowlist does not.
        self.enterContext(mock.patch.object(piperd, 'PUBLIC', (*piperd.PUBLIC, 'mobile.css')))
        server = piperd.ThreadingHTTPServer(('127.0.0.1',0),piperd.Handler)
        self.enterContext(mock.patch.object(piperd,'MANAGER',manager))
        self.enterContext(mock.patch.object(piperd,'PORT',server.server_port))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser='piper-stacks-pins-%s'%os.getpid()
        def cli(*args):
            result=subprocess.run(['agent-browser','--session',browser,'--json',*args],capture_output=True,text=True,timeout=40)
            self.assertEqual(result.returncode,0,(result.stdout+result.stderr).replace(common.own_token('gui'),'[token]'))
            data=json.loads(result.stdout)
            self.assertTrue(data['success'],str(data).replace(common.own_token('gui'),'[token]'))
            return data.get('data',{}).get('result')
        def ready(expr):
            try: cli('wait','--fn',expr)
            except AssertionError:
                print('STACKS_PIN_DIAGNOSTIC',cli('eval',"(async()=>{const s=(await (await import('/piper/dist/api.js')).api.state('')).data;return JSON.stringify({body:document.body.innerText.slice(0,600),modules:s?.features?.modules,labels:[...document.querySelectorAll('#actbar [aria-label]')].map(e=>e.getAttribute('aria-label'))})})()"),flush=True)
                raise
        def shot(name):
            path=Path(os.environ['TEST_SANDBOX_WORK'],name+'.png');cli('screenshot',str(path))
            print('FEEDBACK_PNG '+name+' '+base64.b64encode(path.read_bytes()).decode(),flush=True)
        try:
            cli('open',piperd.gui_url());cli('set','viewport','1024','800')
            ready(r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            initial=cli('eval',"(()=>{const e=document.querySelector('#actbar [aria-label=\"Dev stacks\"]');return{main:!!e.closest('.ab-main'),foot:!!e.closest('.ab-foot'),next:e.nextElementSibling?.getAttribute('aria-label')}})()")
            print('STACKS_PIN_INITIAL',json.dumps(initial),flush=True)
            cli('click','#actbar [aria-label="Dev stacks"]')
            ready('document.querySelectorAll(".stack-overview .so-stack").length===25')
            geometry=cli('eval',"(()=>{const b=document.querySelector('.stack-overview').getBoundingClientRect();return{top:b.top,bottom:b.bottom,height:innerHeight}})()")
            print('STACKS_POPOVER_GEOMETRY',json.dumps(geometry),flush=True)
            shot('stacks-inventory-25-initial')
            self.assertGreaterEqual(geometry['top'],8,geometry)
            self.assertLessEqual(geometry['bottom'],geometry['height']-8,geometry)
            cli('press','Escape')
            cli('click','#actbar [aria-label="All tools"]')
            ready(r'!!document.querySelector(".nav-tools")')
            shot('stacks-pin-tools-before')
            self.assertTrue(cli('eval',"!!document.querySelector('.nav-tools [data-menu-id=\"stacks\"]')"),'global overview must use the existing pinnable All tools registry')
            self.assertEqual(initial,{'main':False,'foot':True,'next':'Settings'})
            cli('click','.nav-tools [aria-label="Unpin Dev stacks"]')
            self.assertFalse(cli('eval',r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")'))
            self.assertTrue(cli('eval',r'!!document.querySelector(".nav-tools [aria-label=\"Pin Dev stacks\"]")'))
            cli('press','Escape');cli('reload')
            ready(r'!!document.querySelector("#actbar [aria-label=\"All tools\"]")')
            self.assertFalse(cli('eval',r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")'))
            cli('click','#actbar [aria-label="All tools"]')
            cli('click','.nav-tools .mrow > [data-menu-id="stacks"]:not(.msecondary)')
            ready(r'!!document.querySelector(".stack-overview")')
            self.assertIn('25 running',cli('eval',r'document.querySelector(".stack-overview").textContent'))
            shot('stacks-unpinned-overview')
            cli('press','Escape')
            self.assertEqual(cli('eval',r'document.activeElement.getAttribute("aria-label")'),'All tools')
            cli('click','#actbar [aria-label="All tools"]')
            cli('click','.nav-tools [aria-label="Pin Dev stacks"]')
            cli('press','Escape');cli('reload')
            ready(r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            for width in (1024,1971,390):
                cli('set','viewport',str(width),'800')
                geometry=cli('eval',"(()=>{const e=document.querySelector('#actbar [aria-label=\"Dev stacks\"]'),s=document.querySelector('#actbar [aria-label=\"Settings\"]'),r=e.getBoundingClientRect();return{foot:!!e.closest('.ab-foot'),main:!!e.closest('.ab-main'),above:s?e.nextElementSibling===s:true,hit:e.checkVisibility({checkOpacity:true})&&e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}})()")
                self.assertEqual(geometry,{'foot':True,'main':False,'above':True,'hit':True})
                shot('stacks-pinned-footer-%s'%width)
                cli('click','#actbar [aria-label="Dev stacks"]')
                ready('document.querySelectorAll(".stack-overview .so-stack").length===25')
                bounds=cli('eval',"""(()=>{const box=document.querySelector('.stack-overview'),r=box.getBoundingClientRect(),list=box.querySelector('.so-list');
                  const all=[...box.querySelectorAll('.so-stack button')],bad=[];
                  for(const b of all){b.scrollIntoView({block:'nearest'});const p=b.getBoundingClientRect();if(p.top<r.top||p.bottom>r.bottom||!b.contains(document.elementFromPoint(p.x+p.width/2,p.y+p.height/2)))bad.push(b.dataset.stackStop);}
                  return{top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:innerHeight,width:innerWidth,count:all.length,scrolls:list.scrollHeight>list.clientHeight,bad};})()""")
                print('STACKS_POPOVER_BOUNDS',width,json.dumps(bounds),flush=True)
                self.assertGreaterEqual(bounds['top'],8,bounds)
                self.assertLessEqual(bounds['bottom'],bounds['height']-8,bounds)
                self.assertGreaterEqual(bounds['left'],8,bounds)
                self.assertLessEqual(bounds['right'],bounds['width']-8,bounds)
                self.assertEqual(bounds['count'],25,bounds)
                self.assertTrue(bounds['scrolls'],bounds)
                self.assertEqual(bounds['bad'],[],bounds)
                shot('stacks-inventory-25-scrolled-%s'%width)
                cli('press','Escape')
            features.save({'stack_overview':False});manager.bump()
            ready(r'!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            cli('click','#actbar [aria-label="All tools"]')
            self.assertFalse(cli('eval',r'!!document.querySelector(".nav-tools [data-menu-id=\"stacks\"]")'))
            cli('press','Escape')
            features.save({'stack_overview':True});manager.bump()
            ready(r'!!document.querySelector("#actbar .ab-foot [aria-label=\"Dev stacks\"]")')
            cli('click','#actbar [aria-label="Dev stacks"]')
            ready(r'!!document.querySelector(".stack-overview")')
            self.assertTrue(cli('eval',r'document.querySelector(".stack-overview").textContent.includes("Stop all")'))
            cli('press','Escape')
            self.assertEqual(cli('eval',r'document.activeElement.getAttribute("aria-label")'),'Dev stacks')
            # Phone: the bottom tab bar has no Dev stacks button - a tap on More › Dev stacks opened nothing
            cli('set','viewport','390','844')
            cli('eval','''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
              await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
              const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
            manager.bump()
            ready('document.body.classList.contains("phone-shell") && !!document.querySelector("#actbar .ab-tab[aria-label=More]")')
            cli('click','#actbar .ab-tab[aria-label="More"]')
            ready('[...document.querySelectorAll(".sheet .sheet-row")].some(b=>b.textContent.includes("Dev stacks"))')
            cli('eval','[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Dev stacks")).click();true')
            ready('document.querySelectorAll(".stack-overview .so-stack").length===25 && !document.querySelector(".sheet")')
            phone=cli('eval',"(()=>{const r=document.querySelector('.stack-overview').getBoundingClientRect(),bar=document.querySelector('#actbar').getBoundingClientRect(),b=document.querySelector('.stack-overview .so-head button');const p=b.getBoundingClientRect();return{top:r.top,bottom:r.bottom,left:r.left,right:r.right,width:innerWidth,bar:bar.top,hit:b.contains(document.elementFromPoint(p.x+p.width/2,p.y+p.height/2))}})()")
            print('STACKS_PHONE_BOUNDS',json.dumps(phone),flush=True)
            shot('stacks-phone-overview')
            self.assertGreaterEqual(phone['top'],8,phone)
            self.assertLessEqual(phone['bottom'],phone['bar'],phone)
            self.assertGreaterEqual(phone['left'],8,phone)
            self.assertLessEqual(phone['right'],phone['width']-8,phone)
            self.assertTrue(phone['hit'],phone)
        finally:cli('close')

    def test_native_titlebar_stays_above_update_notice(self):
        """Native controls are fixed OS overlays; classes/bounds here are explicit mocks."""
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        features.save(onboarded=True)
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        state = {"code_build": "fixture-build", "code_disk": "fixture-update", "code_outdated": True}
        self.enterContext(mock.patch.object(piperd.codebuild, "state_fields", side_effect=lambda: dict(state)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = "piper-native-banner-%s" % os.getpid()

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], str(data).replace(common.own_token("gui"), "[token]"))
            return data.get("data", {}).get("errors", []) if args[0] == "errors" else data.get("data", {}).get("result")

        def shot(name):
            path = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
            cli("screenshot", str(path))
            print("FEEDBACK_PNG " + name + " " + base64.b64encode(path.read_bytes()).decode(), flush=True)

        try:
            cli("open", piperd.gui_url())
            cli("wait", "--fn", '!!document.querySelector("#update-banner") && !!document.querySelector("#winbar button")')
            # Do not expose a fake native bridge to the application. Only the actual
            # desktop titlebar classes and OS control rectangles are simulated.
            cli("eval", """(()=>{const w=document.getElementById('winbar');w.className='native darwin';
                // Explicit synthetic content pressure reproduces native flex shrink observed in Electron CI.
                document.getElementById('app').style.flexBasis='100vh';
                const os=document.createElement('div');os.id='native-controls-test';
                Object.assign(os.style,{position:'fixed',left:'10px',top:'10px',width:'66px',height:'16px',zIndex:'1000',pointerEvents:'none'});
                for(const [i,color] of ['#ff5f57','#febc2e','#28c840'].entries()){const b=document.createElement('i');
                  Object.assign(b.style,{position:'absolute',left:(i*25)+'px',top:'0',width:'14px',height:'14px',borderRadius:'50%',background:color});os.append(b);}
                document.body.append(os);return true})()""")
            for width, height in ((1024, 800), (1971, 1280)):
                cli("set", "viewport", str(width), str(height))
                geometry = cli("eval", """(()=>{const w=document.getElementById('winbar'),u=document.getElementById('update-banner'),a=document.getElementById('app'),
                    wr=w.getBoundingClientRect(),ur=u.getBoundingClientRect(),ar=a.getBoundingClientRect(),os=document.getElementById('native-controls-test').getBoundingClientRect(),
                    text=document.createRange();text.selectNodeContents(u.querySelector('span'));
                    const overlap=(x,y)=>x.left<y.right&&x.right>y.left&&x.top<y.bottom&&x.bottom>y.top,
                    buttons=[...w.querySelectorAll('.workspace-tab,button:not([hidden])')],restart=u.querySelector('button'),r=restart.getBoundingClientRect(),
                    hit=b=>{const x=b.getBoundingClientRect();return b.contains(document.elementFromPoint(x.x+x.width/2,x.y+x.height/2))};
                    return {barTop:wr.top,barHeight:wr.height,barBottom:wr.bottom,bannerTop:ur.top,bannerBottom:ur.bottom,appTop:ar.top,appBottom:ar.bottom,
                      textSafe:[...text.getClientRects()].every(x=>x.top>=wr.bottom&&!overlap(x,os)),restartSafe:r.top>=wr.bottom&&hit(restart),
                      controls:buttons.length>0&&buttons.every(b=>b.getBoundingClientRect().left>=78&&hit(b)&&getComputedStyle(b).webkitAppRegion==='no-drag'),
                      buttonInfo:buttons.map(b=>({label:b.getAttribute('aria-label')||b.textContent,x:b.getBoundingClientRect().x,width:b.getBoundingClientRect().width,hit:hit(b),drag:getComputedStyle(b).webkitAppRegion,visible:b.checkVisibility({checkOpacity:true})})),
                      alternativeHidden:w.querySelector('.workspace-list').hidden&&!w.querySelector('.workspace-list').checkVisibility(),bannerDrag:getComputedStyle(u).webkitAppRegion,
                      drag:getComputedStyle(w).webkitAppRegion,root:scrollY===0&&document.documentElement.scrollWidth<=innerWidth};})()""")
                print("NATIVE_BANNER_GEOMETRY", width, json.dumps(geometry, sort_keys=True), flush=True)
                shot("native-update-banner-%s" % width)
                self.assertEqual(geometry["barTop"], 0, geometry)
                self.assertEqual(geometry["barHeight"], 38, geometry)
                self.assertEqual(geometry["bannerTop"], geometry["barBottom"], geometry)
                self.assertEqual(geometry["appTop"], geometry["bannerBottom"], geometry)
                self.assertEqual(geometry["appBottom"], height, geometry)
                self.assertEqual(geometry["drag"], "drag", geometry)
                self.assertNotEqual(geometry["bannerDrag"], "drag", geometry)
                cli("click", "#winbar .workspace-tab.on")
                self.assertTrue(all(geometry[k] for k in ("textSafe", "restartSafe", "controls", "root", "alternativeHidden")), geometry)
                cli('focus', '#update-banner button')
                self.assertTrue(cli('eval', "document.activeElement===document.querySelector('#update-banner button')"))
            # Removing and recreating the real notice must preserve native geometry.
            state.update(code_disk="fixture-build", code_outdated=False)
            manager.bump()
            cli('wait', '--fn', '!document.getElementById("update-banner")')
            self.assertTrue(cli('eval', "(()=>{const w=document.getElementById('winbar').getBoundingClientRect(),a=document.getElementById('app').getBoundingClientRect();return w.top===0&&w.height===38&&a.top===w.bottom&&a.bottom===innerHeight})()"))
            state.update(code_disk="fixture-update", code_outdated=True)
            manager.bump()
            cli('wait', '--fn', '!!document.getElementById("update-banner")')
            self.assertEqual(cli('eval', "document.getElementById('update-banner').getBoundingClientRect().top"), 38)
            # Other native overlays keep their platform-specific reserved side too.
            cli('eval', "document.getElementById('winbar').className='native win32';true")
            self.assertTrue(cli('eval', "(()=>{const w=document.getElementById('winbar');return w.getBoundingClientRect().top===0&&getComputedStyle(w).paddingRight==='140px'&&document.getElementById('update-banner').getBoundingClientRect().top===38})()"))
            # Browser chrome keeps its original ordering and no desktop inset.
            cli('eval', "document.getElementById('app').style.removeProperty('flex-basis');document.getElementById('winbar').className='';document.getElementById('native-controls-test').remove();true")
            baseline = cli('eval', "(()=>{const w=document.getElementById('winbar').getBoundingClientRect(),u=document.getElementById('update-banner').getBoundingClientRect();return{banner:u.top,bar:w.top,height:w.height,bottom:u.bottom,padding:getComputedStyle(document.getElementById('winbar')).paddingLeft}})()")
            self.assertEqual(baseline['banner'], 0, baseline)
            self.assertEqual(baseline['bar'], baseline['bottom'], baseline)
            self.assertEqual(baseline['height'], 32, baseline)
            self.assertEqual(baseline['padding'], '0px', baseline)
            shot('browser-update-banner-1971')
            self.assertFalse(cli('errors'))
        finally:
            cli('close')

    def test_stack_inventory_stop_retry_feature_off_and_ended_later(self):
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        long_file = 'src/main/java/very/long/fixture/package/ChangedFixture.java'
        root = _env.make_project("feedback-browser", {"README.md": "Synthetic feedback fixture\n", long_file: 'class ChangedFixture {}\n'})
        subprocess.run(['git', '-C', root, 'init', '-qb', 'main'], check=True, capture_output=True)
        _env.register("acme/feedback", root)
        ended = sessions.Session(project="acme/feedback", path=root, title="Finished task", state="exited", rc=0)
        manager.sessions[ended.id] = ended
        ended.save()
        previous = sessions.Session(project="acme/feedback", path=root, title="Other finished task", state="exited", rc=0)
        manager.sessions[previous.id] = previous
        previous.save()
        conversation = manager.conversation
        def saved_conversation(sid, *args, **kwargs):
            if sid in (ended.id, previous.id):
                return {"usage": None, "turns": [{"role": "assistant", "text":
                    "Finished task transcript from Later" if sid == ended.id else "Other finished transcript"}]}
            return conversation(sid, *args, **kwargs)
        self.enterContext(mock.patch.object(manager, "conversation", side_effect=saved_conversation))
        retry = self.enterContext(mock.patch.object(manager, "retry", wraps=manager.retry))
        parent, child = root, os.path.join(root, "child")

        class Provider:
            def __init__(self):
                self.calls, self.jobs = [], {}
                self.rows = [{"root": parent, "name": "acme/shop", "parent": "", "running": 3, "total": 3, "can_stop": True},
                             {"root": child, "name": "search", "parent": parent, "running": 1, "total": 1, "can_stop": True}]
                self.fail = True
                self.finish_parent = threading.Event()

            def up(self): return True
            def dashboard_url(self): return ""
            def worktree(self, *args, **kwargs): return None
            def running_stacks(self): return [dict(row) for row in self.rows if row["running"]]
            def verb(self, root, verb, service):
                self.calls.append((root, verb, service))
                if root == child and self.fail:
                    self.fail = False
                    raise stackview.StackError("Temporary provider failure")
                for row in self.rows:
                    if row["root"] == root and root != parent: row["running"] = 0
                jid = "%06x" % len(self.calls)
                self.jobs[jid] = {"id": jid, "root": root, "verb": "stop", "state": "running" if root == parent else "done", "rc": 0}
                return self.jobs[jid]
            def job(self, jid):
                job = self.jobs[jid]
                if job["root"] == parent and self.finish_parent.is_set():
                    job["state"] = "done"
                    self.rows[0]["running"] = 0
                return dict(job)

        provider = Provider()
        self.enterContext(mock.patch.object(manager, "_stack", return_value=provider))
        self.enterContext(mock.patch.object(features, "MODULES", [*features.MODULES, ("stack", "Dev stacks", "Fixture provider")]))
        self.enterContext(mock.patch.object(features, "IDS", [*features.IDS, "stack"]))
        self.enterContext(mock.patch.dict(features.PRESETS, {"all": [*features.PRESETS["all"], "stack"]}))
        features._CACHE.clear()
        _env.set_modules(self, stack=True, stack_overview=True)
        features.save(onboarded=True)
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        build_state = {"code_build": "fixture-build", "code_disk": "fixture-build", "code_outdated": False}
        self.enterContext(mock.patch.object(piperd.codebuild, "state_fields", side_effect=lambda: dict(build_state)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = "piper-feedback-%s" % os.getpid()

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], str(data).replace(common.own_token("gui"), "[token]"))
            return data.get("data", {}).get("result")

        def ready(expression):
            try:
                until = time.monotonic() + 25
                while time.monotonic() < until:
                    if cli("eval", expression) is True:
                        return
                    time.sleep(0.1)
                self.fail("headless UI did not settle: " + expression)
            except AssertionError:
                print("FEEDBACK_DIAGNOSTIC", cli("eval", '(async()=>JSON.stringify({text:document.body.innerText.slice(0,1200),modules:(await (await import("/piper/dist/api.js")).api.state("")).data.features.modules,labels:[...document.querySelectorAll("#actbar [aria-label]")].map(x=>x.getAttribute("aria-label"))}))()'), flush=True)
                print("FEEDBACK_SERVER_MODULES", features.summary()["modules"], flush=True)
                print("FEEDBACK_ERRORS", cli("errors"), flush=True)
                raise

        def shot(name):
            target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
            cli("screenshot", str(target))
            # Same artifact transport as the desktop harness: no writes outside the OS sandbox.
            print("FEEDBACK_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)

        def fonts(ui, editor):
            cli('eval', '''(async()=>{const {call}=await import('/piper/dist/api.js');
                await call('POST','/api/settings',{section:'interface',values:%s});return true})()''' % json.dumps({'ui_font_size': str(ui), 'editor_font_size': str(editor)}))
            ready('getComputedStyle(document.body).fontSize==="%spx"' % ui)

        try:
            cli("open", piperd.gui_url())
            cli("set", "viewport", "1280", "800")
            ready(r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            if cli("eval", 'document.querySelector("#queue").getBoundingClientRect().width<100'):
                cli("click", '#actbar [aria-label="Overview"]')
            cli("click", '#actbar [aria-label="Dev stacks"]')
            ready('document.querySelectorAll(".stack-overview .so-stack").length===2')
            self.assertTrue(cli("eval", '!!document.querySelector(".stack-overview .substack")'))
            self.assertIn("2 running · 4 services", cli("eval", 'document.querySelector(".stack-overview .so-head").textContent'))
            shot("f04-stack-overview")
            cli("click", '#actbar [aria-label="Dev stacks"]')
            ready('!document.querySelector(".stack-overview")')
            self.assertEqual(cli("eval", 'document.activeElement.getAttribute("aria-label")'), "Dev stacks")
            cli("click", '#actbar [aria-label="Dev stacks"]')
            cli("eval", "[...document.querySelectorAll('.stack-overview button')].find(b=>b.textContent==='Stop all…').click(); true")
            ready('!![...document.querySelectorAll("button")].find(b=>b.textContent==="Stop 2 stacks")')
            self.assertEqual(cli("eval", 'document.activeElement.textContent'), "Cancel")
            shot("f04-stop-confirm")
            cli("press", "Escape")
            ready('!document.querySelector("#overlay.open")')
            self.assertEqual(provider.calls, [])
            cli("click", '#actbar [aria-label="Dev stacks"]')
            cli("eval", "[...document.querySelectorAll('.stack-overview button')].find(b=>b.textContent==='Stop all…').click(); true")
            features.save({"stack_overview": False})
            manager.bump()
            ready('!document.querySelector("#overlay.open")')
            self.assertEqual(provider.calls, [])
            features.save({"stack_overview": True})
            manager.bump()
            ready(r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            cli("click", '#actbar [aria-label="Dev stacks"]')
            ready('document.querySelectorAll(".stack-overview .so-stack").length===2')
            cli("eval", "[...document.querySelectorAll('.stack-overview button')].find(b=>b.textContent==='Stop all…').click(); true")
            cli("click", '#overlay .modal button.danger')
            ready(r'!!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            cli("click", '#actbar [aria-label="Dev stacks"]')
            ready('document.querySelector(".stack-overview")?.textContent.includes("Temporary provider failure")')
            ready('!![...document.querySelectorAll(".stack-overview button")].find(b=>b.textContent==="Stopping…"&&b.disabled)')
            self.assertEqual(provider.calls, [(child, "stop", ""), (parent, "stop", "")])
            shot("f04-pending-stop")
            provider.finish_parent.set()
            ready('![...document.querySelectorAll(".stack-overview button")].some(b=>b.textContent==="Stopping…")')
            shot("f04-partial-stop")
            cli("eval", '[...document.querySelectorAll(".stack-overview button")].find(b=>b.textContent==="Retry stop").click(); true')
            ready('document.querySelector(".stack-overview")?.textContent.includes("No running stacks")')
            self.assertEqual(provider.calls, [(child, "stop", ""), (parent, "stop", ""), (child, "stop", "")])
            cli("press", "Escape")
            # Begin in another session's Conversation, then open Later without keeping its stale transcript.
            cli("eval", 'document.querySelector("#queue .q-sec.ended .q-fold[aria-expanded=false]")?.click(); true')
            cli("click", '#queue [data-sid="%s"]' % previous.id)
            cli("click", '.tabs button[title="readable conversation and token usage"]')
            ready('document.querySelector(".convo")?.textContent.includes("Other finished transcript")')
            # Real menu → backend mark → Later queue → Conversation. No start/retry request occurs.
            cli("eval", 'document.querySelector(%s).dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:170,clientY:190})); true' % json.dumps('#queue [data-sid="%s"]' % ended.id))
            ready('!![...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("Set aside for later"))')
            cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("Set aside for later")).click(); true')
            ready('!!document.querySelector("#queue .q-sec.later")')
            cli("eval", 'document.querySelector("#queue .q-sec.later .q-sec-h button, #queue .q-sec.later button")?.click(); true')
            ready(r'!!document.querySelector("#queue [data-key=\"l:%s\"]")' % ended.id)
            shot("f10-ended-later")
            cli("click", '#queue [data-key="l:%s"]' % ended.id)
            ready('JSON.parse(localStorage.getItem("piper.last")||"{}").view==="convo" && document.querySelector(".convo")?.textContent.includes("Finished task transcript from Later")')
            self.assertFalse(cli("eval", 'document.querySelector(".convo")?.textContent.includes("Other finished transcript")'))
            retry.assert_not_called()
            self.assertEqual((ended.state, ended.pid, ended.later), ("exited", None, 0.0))
            shot("f10-conversation-without-resume")
            features.save({"stack_overview": False})
            manager.bump()
            ready(r'!document.querySelector("#actbar [aria-label=\"Dev stacks\"]")')
            self.assertFalse(cli("eval", '!!document.querySelector(".stack-overview")'))
            # Real pane chrome, host view/menu/grid and layout, with a synthetic host
            # inventory entry: do not start a login shell just to measure its header.
            _env.set_modules(self, host=True, agent=True, grid=True)
            owner = sessions.Session(project="acme/feedback", path=root,
                                     title="Remembered owner with a deliberately long descriptive name " * 4, state="waiting")
            process = manager.pty.call("spawn", id=owner.id, argv=[sys.executable, "-c", "import time;time.sleep(120)"],
                                       cwd=root, env=dict(os.environ), cols=80, rows=24)
            owner.pid = process["pid"]
            manager.sessions[owner.id] = owner
            owner.save()
            host_id = "host-a1b2c3d4"
            host_path = os.path.join(root, "long-project-component-" * 6, "nested-working-directory-" * 5)
            os.makedirs(host_path)
            manager.hosts.items.append({"id": host_id, "project": owner.project, "session": owner.id,
                                        "path": host_path, "title": "Long host shell title " * 10,
                                        "created": time.time(), "alive": True, "rc": None})
            cli("eval", '''(async()=>{const {TermView}=await import('/piper/dist/term.js');
                const connect=TermView.prototype.connect;TermView.prototype.connect=function(){
                  if(this.id===%s)return;return connect.call(this)};return true})()''' % json.dumps(host_id))
            manager.bump()
            ready('!!document.querySelector("#queue [data-sid=\\"%s\\"]")' % owner.id)
            cli("click", '#queue [data-sid="%s"]' % owner.id)
            ready('!!document.querySelector("#actbar [aria-label=\\"Host shells\\"]")')
            cli("click", '#actbar [aria-label="Host shells"]')
            ready('!!document.querySelector(".ht-tab")')
            cli("eval", 'document.querySelector(".ht-tab").dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:200,clientY:180}));true')
            cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent==="Add to grid").click();true')
            cli("press", "Meta+g")
            ready('!!document.querySelector(".term.hostpane .pane-owner")')
            zoomed = False
            for width, want_zoom in ((1024, True), (1920, True), (1920, False), (1024, False)):
                cli("set", "viewport", str(width), "800")
                if zoomed != want_zoom:
                    cli("click", '.term.hostpane [data-act="%s"]' % ("unzoom" if zoomed else "zoom"))
                    zoomed = want_zoom
                ready('!!document.querySelector(".term.hostpane [data-act=\\"%s\\"]")' % ("unzoom" if zoomed else "zoom"))
                cli("hover", ".term.hostpane .tlabel")
                ready('Number(getComputedStyle(document.querySelector(".term.hostpane .pane-tools")).opacity)>.99')
                geometry = cli("eval", '''(()=>{const pane=document.querySelector('.term.hostpane'),label=pane.querySelector('.tlabel'),
                  name=label.querySelector('.name'),cwd=label.querySelector('.mono'),owner=label.querySelector('.pane-owner'),pill=label.querySelector('.ht-pill'),tools=label.querySelector('.pane-tools'),
                  r=label.getBoundingClientRect(),c=cwd.getBoundingClientRect();
                  return {width:pane.getBoundingClientRect().width,overflow:label.scrollWidth>label.clientWidth+1,
                    name:name.getBoundingClientRect().width,minName:parseFloat(getComputedStyle(name).fontSize)*5,
                    owner:owner.getBoundingClientRect().width,cwd:c.width,cwdHidden:!cwd.checkVisibility(),
                    path:cwd.title,badge:pill.checkVisibility()&&pill.scrollWidth<=pill.clientWidth,
                    ownRow:tools.getBoundingClientRect().top>=Math.max(name.getBoundingClientRect().bottom,owner.getBoundingClientRect().bottom,pill.getBoundingClientRect().bottom)-1,
                    tools:[...tools.querySelectorAll('button[data-act],button[aria-label="pane menu"]')].every(b=>{const x=b.getBoundingClientRect();return b.checkVisibility({checkOpacity:true})&&x.width>0&&x.left>=r.left-1&&x.right<=r.right+1&&b.contains(document.elementFromPoint(x.x+x.width/2,x.y+x.height/2))})}})()''')
                self.assertFalse(geometry["overflow"], geometry)
                self.assertGreaterEqual(geometry["name"], geometry["minName"] - 1, geometry)
                self.assertGreaterEqual(geometry["owner"], 71, geometry)
                self.assertTrue(geometry["badge"] and geometry["tools"], geometry)
                self.assertEqual(geometry["path"], host_path)
                if geometry["width"] <= 520:
                    self.assertTrue(geometry["cwdHidden"] and geometry["ownRow"], geometry)
                else:
                    self.assertFalse(geometry["cwdHidden"], geometry)
                    self.assertGreaterEqual(geometry["cwd"], 96, geometry)
                shot("host-toolbar-%s-%s" % (width, "zoom" if zoomed else "grid"))
            # Reproduce the physical Host allocation reached by nine UI-added panes
            # plus an inline approval. Only the real pane's outer size is constrained;
            # its header, wrapping, tool styles and container queries remain untouched.
            try:
                for ui, height, screen in ((13, 83.38, 'host-toolbar-218x83'), (16, 80.83, 'host-toolbar-ui16-218x80')):
                    fonts(ui, 18 if ui == 16 else 12.5)
                    cli("eval", '''(()=>{const pane=document.querySelector('.term.hostpane');
                      pane.dataset.feedbackAllocation='compact-host';const style=document.createElement('style');
                      style.id='feedback-host-allocation';style.textContent='[data-feedback-allocation="compact-host"]{width:218px!important;height:%spx!important}';
                      document.head.append(style);return true})()''' % height)
                    try:
                        ready('Math.abs(document.querySelector(".term.hostpane").getBoundingClientRect().height-%s)<1' % height)
                        cli("hover", ".term.hostpane .tlabel")
                        ready('Number(getComputedStyle(document.querySelector(".term.hostpane .pane-tools")).opacity)>.99')
                        geometry = cli("eval", '''(()=>{const pane=document.querySelector('.term.hostpane'),label=pane.querySelector('.tlabel'),
                          name=label.querySelector('.name'),owner=label.querySelector('.pane-owner'),pill=label.querySelector('.ht-pill'),tools=label.querySelector('.pane-tools'),
                          p=pane.getBoundingClientRect(),s=getComputedStyle(pane),inner={left:p.left+parseFloat(s.borderLeftWidth),right:p.right-parseFloat(s.borderRightWidth),top:p.top+parseFloat(s.borderTopWidth),bottom:p.bottom-parseFloat(s.borderBottomWidth)},
                          inside=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=inner.left-1&&r.right<=inner.right+1&&r.top>=inner.top-1&&r.bottom<=inner.bottom+1},
                          buttons=[...tools.querySelectorAll('button[data-act],button[aria-label="pane menu"]')];
                          return {uiFont:parseFloat(getComputedStyle(document.body).fontSize),panePadding:s.padding,labelPadding:getComputedStyle(label).padding,width:p.width,height:p.height,headerHeight:label.getBoundingClientRect().height,headerInside:inside(label),toolsInside:inside(tools),
                            name:name.getBoundingClientRect().width,minName:parseFloat(getComputedStyle(name).fontSize)*5,owner:owner.getBoundingClientRect().width,
                            badge:pill.checkVisibility({checkOpacity:true})&&pill.scrollWidth<=pill.clientWidth&&inside(pill),
                            toolCount:buttons.length,tools:buttons.every(b=>{const r=b.getBoundingClientRect();return b.checkVisibility({checkOpacity:true})&&inside(b)&&b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})}})()''')
                        print("HOST_COMPACT_GEOMETRY", json.dumps(geometry, sort_keys=True), flush=True)
                        shot(screen)
                        self.assertAlmostEqual(geometry["width"], 218, delta=1, msg=geometry)
                        self.assertEqual(geometry["uiFont"], ui)
                        self.assertAlmostEqual(geometry["height"], height, delta=1, msg=geometry)
                        self.assertGreaterEqual(geometry["name"], geometry["minName"] - 1, geometry)
                        self.assertGreaterEqual(geometry["owner"], 71, geometry)
                        self.assertGreaterEqual(geometry["toolCount"], 3, geometry)
                        self.assertTrue(all(geometry[key] for key in ("headerInside", "toolsInside", "badge", "tools")), geometry)
                    finally:
                        cli("eval", '''(()=>{document.querySelector('#feedback-host-allocation')?.remove();
                          document.querySelector('[data-feedback-allocation="compact-host"]')?.removeAttribute('data-feedback-allocation');return true})()''')
            finally:
                fonts(13, 12.5)
            # The update notice must move the app/drawer together. Test geometry and
            # hit testing, then use the actual close button rather than removing DOM.
            build_state.update(code_disk="fixture-update", code_outdated=True)
            manager.bump()
            ready('!!document.querySelector("#update-banner")')
            cli("click", '#actbar [aria-label="Agent"]')
            ready('document.querySelector("#drawer")?.checkVisibility()===true')
            for width in (1024, 1920):
                cli("set", "viewport", str(width), "800")
                geometry = cli("eval", '''(()=>{const d=document.querySelector('#drawer'),r=d.getBoundingClientRect(),a=document.querySelector('#app').getBoundingClientRect(),w=document.querySelector('#winbar').getBoundingClientRect(),u=document.querySelector('#update-banner').getBoundingClientRect(),b=d.querySelector('.drawer-head [aria-label="close"]'),c=b.getBoundingClientRect();return {aligned:Math.abs(r.top-a.top)<2,below:r.top>=w.bottom-1&&r.top>=u.bottom-1,bottom:Math.abs(r.bottom-innerHeight)<2,close:b.checkVisibility()&&b.contains(document.elementFromPoint(c.x+c.width/2,c.y+c.height/2))}})()''')
                self.assertTrue(all(geometry.values()), geometry)
                shot("update-banner-agent-drawer-%s" % width)
            # Keep the underlying detail rendered at phone widths. The strip remains
            # reachable while the mobile drawer owns the detail's focusable controls.
            if cli("eval", 'document.querySelector("#queue").getBoundingClientRect().width>100'):
                cli("focus", '#actbar [aria-label="Agent"]')
                cli("press", "Meta+b")
            widths = (641, 640, 390, 320, 641)
            for index, width in enumerate(widths):
                cli("set", "viewport", str(width), "800")
                blocked = width <= 640
                ready('document.querySelector("#main").inert===%s' % str(blocked).lower())
                focus = cli("eval", '''(()=>{const m=document.querySelector('#main'),b=[...m.querySelectorAll('button:not(:disabled)')].find(b=>b.checkVisibility({checkOpacity:true}));
                  if(b)b.focus();return {hasBackground:!!b,backgroundFocused:!!b&&document.activeElement===b,
                    railActive:!document.querySelector('#actbar').inert,queueActive:!document.querySelector('#queue').inert}})()''')
                self.assertTrue(focus["hasBackground"], focus)
                self.assertEqual(focus["backgroundFocused"], not blocked, focus)
                self.assertTrue(focus["railActive"] and focus["queueActive"], focus)
                shot("update-banner-agent-drawer-%s" % width)
                cli("click", '#drawer .drawer-head [aria-label="close"]')
                ready('document.querySelector("#drawer")?.checkVisibility()===false && document.querySelector("#main").inert===false')
                self.assertTrue(cli("eval", '''(()=>{const b=[...document.querySelectorAll('#main button:not(:disabled)')].find(b=>b.checkVisibility({checkOpacity:true}));b?.focus();return !!b&&document.activeElement===b})()'''))
                if index + 1 < len(widths):
                    cli("click", '#actbar [aria-label="Agent"]')
                    ready('document.querySelector("#drawer")?.checkVisibility()===true')
            # Maximum saved preferences must also fit actual Files outside Settings.
            # Reuse this Manager and the product's real navigation/editor, not a second
            # app or a CSS approximation of the component.
            try:
                fonts(16, 18)
                ready('document.documentElement.style.getPropertyValue("--editor-fs")==="18px"')
                cli('set', 'viewport', '1024', '800')
                cli('press', 'Meta+o')
                ready('!!document.querySelector("#main .term.filespane .fe-hub")')
                cli('click', '#main .term.filespane .fe-acts button:first-child')
                ready('!!document.querySelector("#overlay input")')
                cli('fill', '#overlay input', long_file)
                ready('document.querySelector("#overlay")?.textContent.includes("ChangedFixture.java")')
                cli('press', 'Enter')
                ready('document.querySelector("#main .files .ftab.on")?.textContent.includes("ChangedFixture.java")')
                cli('hover', '#main .files .ftab.on')
                cli('click', '#main .files .ftab.on .fclose')
                ready('[...document.querySelectorAll("#main .fe-row")].some(row=>row.title===%s)' % json.dumps(long_file))
                cli('eval', '''(()=>{const row=[...document.querySelectorAll('#main .fe-row')].find(row=>row.title===%s),pane=row.closest('.term.filespane');
                    pane.dataset.feedbackFontAllocation='hub';row.dataset.feedbackFontRow='true';const style=document.createElement('style');
                    style.id='feedback-font-allocation';style.textContent='[data-feedback-font-allocation="hub"]{width:218px!important}';
                    document.head.append(style);return true})()''' % json.dumps(long_file))
                try:
                    ready('Math.abs(document.querySelector("[data-feedback-font-allocation=hub]").getBoundingClientRect().width-218)<1')
                    geometry = cli('eval', '''(()=>{const row=document.querySelector('[data-feedback-font-row]'),dir=row.querySelector('.dir'),name=row.querySelector('.nm'),pane=row.closest('.term.filespane');
                        return {title:row.title,dirVisible:dir.checkVisibility(),dirWidth:dir.getBoundingClientRect().width,name:name.textContent,nameWidth:name.getBoundingClientRect().width,
                          rowInside:row.getBoundingClientRect().right<=pane.getBoundingClientRect().right+1}})()''')
                    print('FONT_FILES_HUB_GEOMETRY', json.dumps(geometry, sort_keys=True), flush=True)
                    shot('files-hub-ui16-218')
                    self.assertEqual(geometry['title'], long_file)
                    self.assertEqual(geometry['name'], 'ChangedFixture.java')
                    self.assertGreater(geometry['nameWidth'], 0)
                    self.assertTrue(geometry['rowInside'], geometry)
                    self.assertFalse(geometry['dirVisible'], geometry)
                    self.assertEqual(geometry['dirWidth'], 0, geometry)
                finally:
                    cli('eval', '''(()=>{document.querySelector('#feedback-font-allocation')?.remove();
                        document.querySelector('[data-feedback-font-allocation]')?.removeAttribute('data-feedback-font-allocation');
                        document.querySelector('[data-feedback-font-row]')?.removeAttribute('data-feedback-font-row');return true})()''')
                cli('click', 'nav.tabs button[title^="the whole project"]')
                ready('!!document.querySelector("#main .files .tnode.file[data-nav=\\"README.md\\"]")')
                if not cli('eval', 'document.querySelector("#main .files .tnode.file[data-nav=\\"README.md\\"]").checkVisibility()'):
                    cli('click', '#main .files button.ftree')
                cli('click', '#main .files .tnode.file[data-nav="README.md"]')
                ready('!!document.querySelector("#main .file-md-mode")')
                cli('click', '#main .file-md-mode button:nth-child(2)')
                ready('!!document.querySelector("#main .cm-editor") && getComputedStyle(document.querySelector("#main .cm-editor")).fontSize==="18px"')
                cli('set', 'viewport', '320', '800')
                if not cli('eval', 'document.body.classList.contains("no-inspector")'):
                    cli('click', '[aria-label="context panel"]')
                ready('!!document.querySelector("#main .files.narrow .fstatus .wstate.rw")')
                geometry = cli('eval', '''(()=>{const status=document.querySelector('#main .files.narrow .fstatus'),r=status.getBoundingClientRect(),state=status.querySelector('.wstate.rw');
                    return {width:status.clientWidth,scroll:status.scrollWidth,text:state.textContent,stateFits:state.scrollWidth<=state.clientWidth+1,
                      piecesInside:[...status.querySelectorAll(':scope > span')].filter(e=>e.textContent.trim()&&e.checkVisibility()).every(e=>{const x=e.getBoundingClientRect();return x.width>0&&x.left>=r.left-1&&x.right<=r.right+1&&x.top>=r.top-1&&x.bottom<=r.bottom+1})}})()''')
                print('FONT_FILES_STATUS_GEOMETRY', json.dumps(geometry, sort_keys=True), flush=True)
                shot('files-status-ui16-editor18-320')
                self.assertIn('editable', geometry['text'])
                self.assertIn('saved automatically', geometry['text'])
                self.assertLessEqual(geometry['scroll'], geometry['width'] + 1, geometry)
                self.assertTrue(geometry['stateFits'] and geometry['piecesInside'], geometry)
            finally:
                fonts(13, 12.5)
        finally:
            cli("close")


if __name__ == "__main__":
    unittest.main()
