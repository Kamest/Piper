"""Real headless UI/API regressions for image references, zoom and source-view preservation."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import gitops
import piperd
import sessions


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless image viewer checks')
class ImageViewBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        sample = '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="1200"><rect width="1600" height="1200" fill="white"/><text x="80" y="100" font-size="36">Synthetic image</text></svg>'
        if os.environ.get('PIPER_E2E_IMAGE_SAMPLE'):
            sample = Path(os.environ['PIPER_E2E_IMAGE_SAMPLE']).read_text()
        self.root = os.path.realpath(_env.make_project('image-source', {'sample.svg': sample, 'README.md': '![Draft](sample.svg)\n'}))
        other = os.path.realpath(_env.make_project('image-other', {'sample.svg': sample.replace('width="1600"', 'width="800"', 1)}))
        for key, path in (('acme/shop', self.root), ('acme/other', other)):
            subprocess.run(['git', '-C', path, 'init', '-qb', 'main'], check=True, capture_output=True)
            _env.register(key, path)
        external_folder = Path(tempfile.mkdtemp(prefix='external-images-', dir=os.environ['TEST_SANDBOX_WORK']))
        self.external = external_folder / 'outside.svg'
        self.external.write_text(sample)
        Path(self.root, 'hostile.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><script>top.document.body.dataset.imageScript="ran"</script><rect width="20" height="20" fill="red"/></svg>')
        Path(self.root, 'pixel.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='))
        self.source = sessions.Session(project='acme/shop', path=self.root, title='Image source', state='waiting')
        self.other = sessions.Session(project='acme/other', path=other, title='Other session', state='ready')
        self.other.save(); self.manager.sessions[self.other.id] = self.other
        text = '![Draft](sample.svg)\n\n![External](%s)\n\n[Local link](sample.svg)' % self.external
        self.enterContext(mock.patch.object(self.manager, 'conversation', return_value={'agent': 'codex', 'turns': [{'role': 'assistant', 'text': text}], 'usage': None}))
        # The exported mobile client includes this stylesheet; the desktop HTTP allowlist does not.
        self.enterContext(mock.patch.object(piperd, 'PUBLIC', (*piperd.PUBLIC, 'mobile.css')))
        self.server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', self.manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', self.server.server_port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.browser = 'image-viewer-' + str(os.getpid())
        self.downloads = Path(os.environ['TEST_SANDBOX_WORK'], 'downloads-' + self.source.id)
        self.downloads.mkdir()
        self.addCleanup(self.cli, 'close')
        self.cli('set', 'viewport', '390', '844')
        self.cli('open', piperd.gui_url())
        self.ready('!!document.querySelector("#actbar")')
        self.cli('eval', '''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
          await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
          const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
        sid = self.source.id
        child = self.manager.pty.call('spawn', id=sid, argv=[sys.executable, '-c', 'import time;time.sleep(180)'],
                                      cwd=self.root, env=dict(os.environ), cols=80, rows=24)
        self.source.pid = child['pid']; self.source.save()
        self.cli('eval', '''(async()=>{const {TermView}=await import('/piper/dist/term.js');const connect=TermView.prototype.connect;
          TermView.prototype.connect=function(){if(this.id===%s){window.imageTerm=this;return}return connect.call(this)};return true})()''' % json.dumps(sid))
        self.manager.sessions[sid] = self.source; self.manager.bump()
        self.ready('!!document.querySelector(%s)' % json.dumps('#queue [data-sid="%s"]' % sid))
        self.select(self.source)
        self.ready('!!window.imageTerm?.term.element')

    def cli(self, *args):
        r = subprocess.run(['agent-browser', '--session', self.browser, '--download-path', str(self.downloads), '--json', *args], capture_output=True, text=True, timeout=40)
        self.assertEqual(r.returncode, 0, (r.stdout+r.stderr).replace(common.own_token('gui'), '[token]'))
        data = json.loads(r.stdout)
        self.assertTrue(data['success'], 'agent-browser command failed')
        body = data.get('data', {})
        return body.get('result', body)

    def ready(self, expression):
        self.cli('wait', '--fn', expression)

    def select(self, session):
        self.ready('!!document.querySelector(%s)' % json.dumps('#queue [data-sid="%s"]' % session.id))
        self.cli('eval', 'document.querySelector(%s).click();true' % json.dumps('#queue [data-sid="%s"]' % session.id))

    def tab(self, name):
        # the phone header has Terminal · Conversation · Review; the other session views are in its ⋯ sheet
        self.cli('eval', '''(()=>{const t=[...document.querySelectorAll("header .tab")].find(b=>b.textContent===%s);if(t){t.click();return true}
          const back=document.querySelector("header.ph-pushed .ph-back");if(back&&%s==="Terminal"){back.click();return true}
          document.querySelector("header .ph-more").click();[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.querySelector(".sheet-label").textContent===%s).click();return true})()''' % (json.dumps(name), json.dumps(name), json.dumps(name)))

    def open_path(self, path, image=True, pointer=False):
        self.cli('eval', '''(async()=>{const t=imageTerm.term;imageTerm.freeze(true);t.resize(100,12);t.reset();
          await new Promise(done=>t.write(%s,done));window.imagePath=%s;window.imageLink=imageTerm.fileLinks(1).find(x=>x.text===imagePath);
          if(!imageLink)throw Error('No recognized image reference');%s;return true})()''' % (json.dumps(path+'\r\nUnsent terminal draft'), json.dumps(path), '' if pointer else 'imageLink.activate({})'))
        if pointer:
            x, y = self.cli('eval', '(()=>{const r=imageTerm.host.querySelector(".xterm-screen").getBoundingClientRect();return [r.x+r.width/imageTerm.term.cols*4.5,r.y+r.height/imageTerm.term.rows*.5]})()')
            self.cli('mouse', 'move', '0', '0'); self.cli('mouse', 'move', str(x), str(y))
            self.ready('!!imageTerm.host.querySelector(".xterm-cursor-pointer") || imageTerm.term.element.classList.contains("xterm-cursor-pointer")')
            self.cli('mouse', 'down'); self.cli('mouse', 'up')
        if image:
            self.image_ready()

    def image_ready(self):
        self.ready('!!document.querySelector(".imageview-surface img")?.naturalWidth && !document.querySelector(".imageview-tools").hidden')
        self.cli('eval', '(async()=>{await Promise.all(document.querySelector(".imageview").closest(".modal").getAnimations().map(a=>a.finished));await new Promise(requestAnimationFrame);return true})()')

    def close_image(self):
        self.cli('click', '.imageview-close')
        self.ready('!document.querySelector(".imageview") && !history.state?.piperImage')

    def screenshot(self, name):
        target = Path(os.environ['TEST_SANDBOX_WORK'], name+'.png')
        self.cli('screenshot', str(target))
        print('IMAGEVIEW_PNG '+name+' '+base64.b64encode(target.read_bytes()).decode(), flush=True)

    def test_phone_tab_bar_replaces_the_rail_and_more_lists_every_tool(self):
        # Claude boards 1 / 1b: the mobile export at phone width gets a bottom tab bar, not the 52 px rail
        self.manager.bump()
        self.cli('eval', 'document.activeElement?.blur();true')  # no keyboard up
        # setUp opened a session: its detail hides the bar behind ‹ Sessions (phase 2)
        self.ready('!!document.querySelector("header .ph-back")')
        self.cli('eval', 'document.querySelector("header .ph-back").click();true')
        self.ready('document.body.classList.contains("phone-shell") && !!document.querySelector("#actbar .ab-tab")')
        bar = self.cli('eval', '''(()=>{const a=document.querySelector('#actbar').getBoundingClientRect(),m=document.querySelector('#main').getBoundingClientRect();
          return {labels:[...document.querySelectorAll('#actbar .ab-tab .ab-label')].map(x=>x.textContent),top:a.top,bottom:a.bottom,left:a.left,width:a.width,
            mainLeft:m.left,mainBottom:m.bottom,rail:!!document.querySelector('#actbar .ab-brand, #actbar #qmini, #actbar .ab-main'),
            small:[...document.querySelectorAll('#actbar .ab-tab')].filter(b=>b.getBoundingClientRect().height<44).length}})()''')
        self.assertEqual(bar['labels'][0], 'Sessions')
        self.assertEqual(bar['labels'][-2:], ['Alerts', 'More'])
        self.assertEqual((bar['left'], bar['width'], bar['bottom']), (0, 390, 844))
        self.assertEqual(bar['top'], 794)
        self.assertEqual(bar['mainLeft'], 0)
        self.assertLessEqual(bar['mainBottom'], 794)
        self.assertFalse(bar['rail'])
        self.assertEqual(bar['small'], 0)
        self.screenshot('phone-tabbar')
        # More: the same tools as the rail's All tools, then Settings, then Cancel
        self.cli('eval', 'document.querySelector("#actbar [aria-label=More]").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        sheet = self.cli('eval', '''(()=>{const s=document.querySelector('.sheet').getBoundingClientRect();return {rows:[...document.querySelectorAll('.sheet .sheet-label')].map(x=>x.textContent),
          bottom:s.bottom,small:[...document.querySelectorAll('.sheet button')].filter(b=>b.getBoundingClientRect().height<44).length,
          more:document.querySelector('#actbar [aria-label=More]').classList.contains('on')}})()''')
        self.assertIn('Audit', sheet['rows'])
        self.assertEqual(sheet['rows'][-1], 'Settings')
        self.assertNotIn('Keys', sheet['rows'])
        self.assertEqual(sheet['bottom'], 844)
        self.assertEqual(sheet['small'], 0)
        self.assertTrue(sheet['more'])
        self.screenshot('phone-more')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Settings")).click();true')
        self.ready('!document.querySelector(".sheet") && document.querySelector("#actbar [aria-label=More]").classList.contains("on")')
        # Sessions brings the list back
        self.cli('eval', 'document.querySelector("#actbar [aria-label=Sessions]").click();true')
        self.ready('document.querySelector("#actbar [aria-label=Sessions]").classList.contains("on") && !!document.querySelector(%s)?.getClientRects().length' % json.dumps('#queue [data-sid="%s"]' % self.source.id))
        # compact rows (user: the overview's sessions were too big): 44–56 px, one-line title and meta
        rows = self.cli('eval', '''[...document.querySelectorAll('#queue .q-scroll .qi')].filter(q=>q.checkVisibility()).map(q=>{const t=q.querySelector('.qi-title'),c=getComputedStyle(t);
          return [Math.round(q.getBoundingClientRect().height),c.whiteSpace,c.textOverflow]})''')
        self.assertTrue(rows)
        for height, space, overflow in rows:
            self.assertTrue(44 <= height <= 48, rows)
            self.assertEqual((space, overflow), ('nowrap', 'ellipsis'))
        # a real row also carries chips (branch, MR, CI…): they stay on one clipped line, not extra rows
        chips = self.cli('eval', '''(()=>{const q=document.querySelector('#queue .q-scroll .qi').cloneNode(true);q.querySelector('.qi-chips')?.remove();
          const c=document.createElement('span');c.className='qi-chips';for(const w of ['feature/very-long-branch-name','MR !42','CI passed','3 files','+120 −8','Claude'])
          {const m=document.createElement('span');m.className='qi-meta';m.textContent=w;c.append(m);}q.append(c);document.querySelector('#queue .q-scroll .qi').after(q);
          const r=[Math.round(q.getBoundingClientRect().height),Math.round(c.getBoundingClientRect().height)];q.remove();return r})()''')
        self.assertLessEqual(chips[0], 64, chips)
        self.assertLessEqual(chips[1], 17, chips)
        head = self.cli('eval', '''(()=>{const h=document.querySelector('#queue .q-head').getBoundingClientRect(),n=document.querySelector('.q-new').getBoundingClientRect();
          return [Math.round(h.height),document.elementFromPoint(n.left-5,n.top+n.height/2)?.closest('.q-new')===document.querySelector('.q-new')]})()''')
        self.assertLessEqual(head[0], 84, head)
        self.assertTrue(head[1], 'the + keeps a 44 px touch target')
        self.screenshot('phone-sessions')
        # Recent alerts (phase 6): the whole screen above the bar
        self.cli('eval', 'document.querySelector("#actbar [aria-label=\\"Recent alerts\\"]").click();true')
        self.ready('!!document.querySelector("#notices")')
        alerts = self.cli('eval', '(()=>{const r=document.querySelector("#notices").getBoundingClientRect();return [Math.round(r.left),Math.round(r.right),Math.round(r.bottom)<=Math.round(document.querySelector("#actbar").getBoundingClientRect().top),r.top>=0]})()')
        self.assertEqual(alerts, [0, 390, True, True])
        self.screenshot('phone-alerts')
        self.cli('eval', 'document.querySelector("#actbar [aria-label=\\"Recent alerts\\"]").click();true')
        self.ready('!document.querySelector("#notices")')
        # typing hides the bar: the keyboard takes the bottom
        self.cli('eval', 'const f=document.createElement("textarea");f.id="phone-typing";document.querySelector("#queue").prepend(f);f.focus();true')
        self.ready('getComputedStyle(document.querySelector("#actbar")).display==="none"')
        self.cli('eval', 'document.querySelector("#phone-typing").remove();true')
        self.ready('getComputedStyle(document.querySelector("#actbar")).display!=="none"')

    def test_phone_terminal_latest_pill_send_now_strip_path_sheet_and_ended_bar(self):
        # session C, board P5: a scrolled-back terminal had no way back but a long swipe, the waiting input lived only in
        # Session info, a path:line tap opened a desktop popup and an ended session's bar sat at the top
        self.cli('eval', '''(()=>{document.activeElement?.blur();imageTerm.ws={readyState:WebSocket.OPEN,send(){},close(){},addEventListener(){},removeEventListener(){}};
          imageTerm.setInputState(true,true,'Image source');return true})()''')
        self.ready('!!document.querySelector(".termkeys.dock:not([hidden])")')
        # P5a ↓ Latest: an overlay pill, the terminal keeps its rows and columns
        self.cli('eval', '''(async()=>{const t=imageTerm.term;imageTerm.freeze(true);
          await new Promise(done=>t.write(Array.from({length:120},(_,i)=>"output line "+i).join("\\r\\n"),done));window.p5Size=[t.cols,t.rows];return true})()''')
        self.assertFalse(self.cli('eval', '!!document.querySelector(".ph-latest")'))
        self.cli('eval', 'imageTerm.term.scrollLines(-30);true')
        self.ready('!!document.querySelector(".ph-latest")')
        pill = self.cli('eval', '''(()=>{const r=document.querySelector(".ph-latest").getBoundingClientRect(),d=document.querySelector(".termkeys.dock").getBoundingClientRect();
          return {right:Math.round(r.right),above:r.bottom<=d.top,h:Math.round(r.height),same:[imageTerm.term.cols,imageTerm.term.rows].join()===p5Size.join(),text:document.querySelector(".ph-latest").textContent}})()''')
        self.assertEqual(pill['text'], '↓ Latest')
        self.assertTrue(pill['above'] and pill['same'], pill)
        self.assertLessEqual(pill['right'], 390 - 12)
        self.assertGreaterEqual(pill['h'], 36)
        self.screenshot('phone-terminal-latest')
        self.cli('eval', 'document.querySelector(".ph-latest").click();true')
        self.ready('!document.querySelector(".ph-latest") && imageTerm.term.buffer.active.viewportY===imageTerm.term.buffer.active.baseY')
        # P5a Send now: the waiting input as a strip right above the dock
        sent = []
        self.enterContext(mock.patch.object(self.manager, 'input_clear', side_effect=lambda sid: sent.append(sid)))
        self.cli('eval', 'imageTerm.freeze(false);true')
        pending = [True]
        self.enterContext(mock.patch.object(self.manager, 'input_pending', side_effect=lambda s: pending[0]))
        self.manager.auto_type_ok(self.source)
        self.ready('!!document.querySelector(".ph-hold")')
        strip = self.cli('eval', '''(()=>{const r=document.querySelector(".ph-hold").getBoundingClientRect(),d=document.querySelector(".termkeys.dock").getBoundingClientRect(),
          b=document.querySelector(".ph-hold button").getBoundingClientRect(),x=imageTerm.host.querySelector(".xterm-screen").getBoundingClientRect();
          return {over:Math.round(x.bottom-r.top),gap:Math.round(d.top-r.bottom),w:[Math.round(r.left),Math.round(r.right)],
          btn:Math.round(b.height),text:document.querySelector(".ph-hold-text").textContent}})()''')
        self.assertEqual(strip['gap'], 0, 'the strip sits right above the dock')
        self.assertLessEqual(strip['over'], 0, 'the terminal fits above the strip, its last line is not covered')
        self.assertEqual(strip['w'], [0, 390])
        self.assertGreaterEqual(strip['btn'], 44)
        self.assertIn('automatic text waits', strip['text'])
        self.screenshot('phone-terminal-send-now')
        self.cli('eval', 'document.querySelector(".ph-hold button").click();true')
        self.ready('true')
        for _ in range(50):
            if sent: break
            time.sleep(.1)
        self.assertEqual(sent, [self.source.id])
        pending[0] = False; self.manager.auto_type_ok(self.source)
        self.ready('!document.querySelector(".ph-hold")')
        # P5b path:line: a bottom sheet under the path instead of the desktop popup at the tap
        self.cli('eval', '''(async()=>{const t=imageTerm.term;t.reset();await new Promise(done=>t.write("see README.md:1\\r\\n",done));
          imageTerm.fileLinks(1).find(l=>l.text.startsWith("README.md")).activate(new MouseEvent("click",{clientX:60,clientY:200}));return true})()''')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        sheet = self.cli('eval', '''(()=>{const s=document.querySelector(".sheet"),r=s.getBoundingClientRect();return {head:s.querySelector(".sheet-head strong")?.textContent,
          rows:[...s.querySelectorAll(".sheet-row .sheet-label")].map(x=>x.textContent),box:[Math.round(r.left),Math.round(r.right),Math.round(r.bottom)],
          popup:!!document.querySelector(".pathmenu"),small:[...s.querySelectorAll("button")].filter(b=>b.getBoundingClientRect().height<44).length}})()''')
        self.assertEqual(sheet['head'], 'README.md:1')
        self.assertIn('Open in Files', sheet['rows']); self.assertIn('Copy path', sheet['rows'])
        self.assertEqual(sheet['box'], [0, 390, 844])
        self.assertFalse(sheet['popup'])
        self.assertEqual(sheet['small'], 0)
        self.screenshot('phone-terminal-path-sheet')
        self.cli('eval', 'document.querySelector(".sheet-cancel").click();true')
        self.ready('!document.querySelector(".sheet")')
        # P5c ended: the end bar where the dock was, its actions as 44 pt buttons
        self.source.state = 'exited'; self.source.rc = 0; self.manager.bump()
        self.ready('!!document.querySelector(".endbar:has(> .st-exited)")')
        ended = self.cli('eval', '''(()=>{const e=document.querySelector(".endbar:has(> .st-exited)"),r=e.getBoundingClientRect(),t=document.querySelector(".term:not(.hidden) .thost")?.getBoundingClientRect();
          return {bottom:Math.round(r.bottom),below:!t||r.top>=t.bottom-1,buttons:[...e.querySelectorAll("button")].map(b=>[b.textContent,Math.round(b.getBoundingClientRect().height)]),w:[Math.round(r.left),Math.round(r.right)]}})()''')
        self.assertTrue(ended['below'], 'below the terminal, not above it')
        self.assertFalse(self.cli('eval', 'document.querySelector(".termkeys.dock").checkVisibility()'), 'no key dock for an ended session')
        self.assertGreaterEqual(ended['bottom'], 844 - 1)
        self.assertEqual(ended['w'], [0, 390])
        self.assertTrue(all(h >= 44 for _, h in ended['buttons']), ended['buttons'])
        self.assertIn('Remove', [b for b, _ in ended['buttons']])
        self.screenshot('phone-terminal-ended')

    def test_phone_session_detail_header_dock_sheet_stop_and_info(self):
        # Claude boards 2, 3, 3c, 5c, 5b, 2c: S4 header, segment, key dock, ⋯ sheet, Stop through S8, Session info
        # the fixture session has no pty: an open socket stub stands in so the dock shows its connected state
        self.cli('eval', '''(()=>{document.activeElement?.blur();imageTerm.ws={readyState:WebSocket.OPEN,send(){},close(){},addEventListener(){},removeEventListener(){}};
          imageTerm.setInputState(true,true,'Image source');return true})()''')
        self.ready('document.body.classList.contains("phone-detail") && !!document.querySelector("header.phone-head")')
        self.ready('!!document.querySelector(".termkeys.dock:not([hidden])") && document.querySelector(".termkeys.dock [data-key=Escape]").getAttribute("aria-disabled")==="false"')
        detail = self.cli('eval', '''(()=>{const r=s=>document.querySelector(s).getBoundingClientRect(),dock=r('.termkeys.dock'),bar=r('.phone-head .ph-bar');
          const keys=[...document.querySelectorAll('.termkeys.dock button')].filter(b=>b.checkVisibility()).map(b=>[b.textContent,b.getBoundingClientRect()]).sort((a,b)=>a[1].left-b[1].left);
          return {bar:bar.height,top:bar.top,tabs:[...document.querySelectorAll('.phone-head .tab')].map(b=>b.textContent),actbar:getComputedStyle(document.querySelector('#actbar')).display,
            dock:[dock.top,dock.height,dock.bottom],term:r('.term .thost').bottom,first:keys.filter(k=>k[1].right<=390).map(k=>k[0]),all:keys.map(k=>k[0]),
            small:[...document.querySelectorAll('.phone-head .ph-bar button, .termkeys.dock button')].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).length,
            stop:!!document.querySelector('.phone-head .dh-stop'),keysToggle:!!document.querySelector('.phone-head .termkeys-toggle')}})()''')
        self.assertEqual((detail['top'], detail['bar']), (0, 48))
        self.assertEqual(detail['tabs'], ['Terminal', 'Conversation', 'Review'])
        self.assertEqual(detail['actbar'], 'none')
        self.assertEqual(detail['dock'][1:], [56, 844])
        self.assertLessEqual(detail['term'], detail['dock'][0])
        self.assertEqual(detail['first'], ['esc', '^C', 'tab', '↑', '↓', '⏎', 'Type'])
        for key in ('⇧tab', '/', 'space', '⇧', 'Copy…', 'Paste', 'Image…'):
            self.assertIn(key, detail['all'])
        self.assertEqual(detail['small'], 0)
        self.assertFalse(detail['stop'] or detail['keysToggle'])
        self.screenshot('phone-session-terminal')
        # typing (board 3): a 44 pt header with Done; Done puts the keyboard away
        self.cli('eval', 'imageTerm.term.focus();true')
        self.ready('getComputedStyle(document.querySelector(".phone-head .ph-done")).display!=="none"')
        typing = self.cli('eval', '''(()=>({bar:document.querySelector('.phone-head .ph-bar').getBoundingClientRect().height,
          hidden:['.ph-back','.ph-more','.ph-seg'].every(s=>getComputedStyle(document.querySelector('.phone-head '+s)).display==='none'),
          hide:document.querySelector('.termkeys.dock [data-key=Type]').textContent}))()''')
        self.assertEqual(typing, {'bar': 44, 'hidden': True, 'hide': 'Hide'})
        self.screenshot('phone-session-typing')
        self.cli('eval', 'document.querySelector(".phone-head .ph-done").click();true')
        self.ready('document.activeElement!==imageTerm.term.textarea && getComputedStyle(document.querySelector(".phone-head .ph-done")).display==="none"')
        # ⋯ (board 5c): Session info and Commit & Push, then the session menu without the desktop-only items, Stop last
        self.cli('eval', 'document.querySelector(".phone-head .ph-more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        sheet = self.cli('eval', '''(()=>{const s=document.querySelector('.sheet');return {rows:[...s.querySelectorAll('.sheet-label')].map(x=>x.textContent),
          danger:[...s.querySelectorAll('.sheet-row.danger .sheet-label')].map(x=>x.textContent),bottom:s.getBoundingClientRect().bottom,
          small:[...s.querySelectorAll('button')].filter(b=>b.getBoundingClientRect().height<44).length,head:s.querySelector('.sheet-head strong').textContent,
          clipped:[...s.querySelectorAll('.sheet-row')].filter(b=>b.getBoundingClientRect().bottom>b.parentElement.getBoundingClientRect().bottom+.5).map(b=>b.textContent)}})()''')
        self.assertEqual(sheet['rows'][:2], ['Session info', 'Commit & Push'])
        for label in ('Files', 'History', 'Rename…', 'Redraw terminal', 'Set aside for later'):
            self.assertIn(label, sheet['rows'])
        for label in ('Side by side', 'Add to grid', 'Open'):
            self.assertNotIn(label, sheet['rows'])
        self.assertEqual(sheet['danger'], ['Stop…'])
        self.assertEqual(sheet['rows'][-1], 'Stop…')
        self.assertEqual(sheet['clipped'], [], 'a long menu scrolls the sheet; a group never clips its rows')
        self.assertEqual((sheet['bottom'], sheet['small'], sheet['head']), (844, 0, 'Image source'))
        self.screenshot('phone-session-more')
        # Stop (board 5b): a confirmation sheet with the app's texts, not the desktop dialog; Cancel stops nothing
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Stop")).click();true')
        self.ready('!!document.querySelector(".sheet .sheet-danger")')
        stop = self.cli('eval', '''(()=>({head:document.querySelector('.sheet .sheet-head strong').textContent,text:document.querySelector('.sheet .sheet-head span').textContent,
          modal:!!document.querySelector('#overlay.open'),button:document.querySelector('.sheet-danger').textContent}))()''')
        self.assertEqual((stop['head'], stop['button'], stop['modal']), ('Stop Image source?', 'Stop', False))
        self.assertIn('The sandbox is destroyed', stop['text'])
        self.screenshot('phone-session-stop')
        self.cli('eval', 'document.querySelector(".sheet .sheet-cancel").click();true')
        self.ready('!document.querySelector(".sheet")')
        self.assertNotEqual(self.manager.sessions[self.source.id].state, 'stopping')
        # Session info (board 2c): the context panel full screen, Done closes it
        self.cli('eval', 'document.querySelector(".phone-head .ph-more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Session info")).click();true')
        self.ready('document.body.classList.contains("phone-info") && !!document.querySelector("#inspector .ph-info-head")')
        info = self.cli('eval', '''(()=>{const r=document.querySelector('#inspector').getBoundingClientRect();return {box:[r.top,r.left,r.width,r.height],
          sections:[...document.querySelectorAll('#inspector .sec')].map(x=>x.dataset.k)}})()''')
        self.assertEqual(info['box'], [0, 0, 390, 844])
        self.assertIn('info', info['sections'])
        self.screenshot('phone-session-info')
        self.cli('eval', 'document.querySelector("#inspector .ph-info-done").click();true')
        self.ready('!document.body.classList.contains("phone-info") && getComputedStyle(document.querySelector("#inspector")).display==="none"')
        # ‹ goes back to Sessions; the tab bar returns
        self.cli('eval', 'document.querySelector(".phone-head .ph-back").click();true')
        self.ready('!document.body.classList.contains("phone-detail") && getComputedStyle(document.querySelector("#actbar")).display!=="none" && document.querySelector("#actbar [aria-label=Sessions]").classList.contains("on")')

    def test_phone_conversation_shows_the_open_question_above_the_composer(self):
        # Claude board 4: the agent's open question waits above the composer with its options (the dialog's number on the
        # right) and Other…; questions never authorize input, so a tap opens Terminal, where the dialog is answered.
        self.source.questions = [
            {'id': 'call-1', 'ts': time.time(), 'text': 'Load relative images from the working copy?\n- Yes, from the working copy: preview reads local files\n- No, only absolute URLs'},
            {'id': 'call-2', 'ts': time.time(), 'text': 'Open the preview automatically?\n- Yes\n- No'}]
        self.source.save(); self.manager.bump()
        self.tab('Conversation')
        self.ready('!!document.querySelector("#phone-question .phq-opt")')
        card = self.cli('eval', '''(()=>{const q=document.querySelector('#phone-question'),r=q.getBoundingClientRect(); 
          return {title:q.querySelector('.phq-t').textContent,options:[...q.querySelectorAll('.phq-opt')].map(b=>b.textContent),page:q.querySelector('.phq-pager span').textContent,
            small:[...q.querySelectorAll('button')].filter(b=>b.getBoundingClientRect().height<44).length,inside:r.left>=0&&r.right<=390&&r.bottom<=844}})()''')
        self.assertEqual(card['title'], 'Load relative images from the working copy?')
        self.assertEqual(card['options'], ['Yes, from the working copy1', 'No, only absolute URLs2', 'Other…'])
        self.assertEqual((card['page'], card['small'], card['inside']), ('1 of 2', 0, True))
        self.screenshot('phone-conversation-question')
        self.cli('eval', 'document.querySelector("#phone-question button[aria-label=\'next question\']").click();true')
        self.ready('document.querySelector("#phone-question .phq-t")?.textContent==="Open the preview automatically?"')
        self.cli('eval', 'document.querySelector("#phone-question .phq-opt").click();true')
        self.ready('document.querySelector("header .tab.on")?.textContent==="Terminal" && !document.querySelector("#phone-question")')
        self.assertEqual(self.source.questions[0]['id'], 'call-1', 'a tap sends nothing: the dialog is answered in the terminal')
        # the card is a phone layout: the desktop window never shows it
        self.source.questions = []; self.source.save(); self.manager.bump()

    def test_paths_gestures_files_markdown_and_original_view(self):
        self.open_path('sample.svg', pointer=True)
        initial = self.cli('eval', '''(()=>{const r=document.querySelector('.imageview-surface img').getBoundingClientRect(),head=document.querySelector('.imageview-head').getBoundingClientRect(),bar=document.querySelector('.imageview-tools').getBoundingClientRect();return {width:r.width,height:r.height,head:head.height,bar:[bar.width,bar.height],zoom:document.querySelector('.imageview-percent').textContent}})()''')
        self.assertAlmostEqual(initial['width'], 366)
        self.assertAlmostEqual(initial['height'], 274.5)
        self.assertEqual(initial['head'], 52)
        self.assertEqual(initial['bar'], [259, 44])
        self.assertEqual(initial['zoom'], '23%')
        self.screenshot('image-mobile-fit')
        self.cli('eval', 'window.imageViewportHeight=document.documentElement.style.getPropertyValue("--mobile-viewport-height");document.documentElement.style.setProperty("--mobile-viewport-height","450px");true')
        self.ready('document.querySelector(".imageview").getBoundingClientRect().height===450')
        self.assertTrue(self.cli('eval', 'document.querySelector(".imageview-tools").getBoundingClientRect().bottom<=450'))
        self.cli('eval', 'document.documentElement.style.setProperty("--mobile-viewport-height",imageViewportHeight);true')
        self.ready('document.querySelector(".imageview").getBoundingClientRect().height===844')
        saved = self.cli('eval', '''(()=>{const original=HTMLAnchorElement.prototype.click;let saved;
          HTMLAnchorElement.prototype.click=function(){saved={same:this.href===document.querySelector('.imageview-surface img').src,name:this.download}};
          try{document.querySelector('.imageview-save').click();return saved}finally{HTMLAnchorElement.prototype.click=original}})()''')
        self.assertEqual(saved, {'same': True, 'name': 'sample.svg'})
        self.cli('click', '.imageview-actual')
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-surface img").getBoundingClientRect().width'), 1600)
        self.cli('press', 'ArrowRight')
        self.assertIn('-40px', self.cli('eval', 'document.querySelector(".imageview-surface img").style.transform'))
        self.cli('press', '0')
        # Pointer events exercise the actual shared pinch/pan handlers. Physical Safari gestures remain a device acceptance step.
        self.cli('eval', '''(()=>{const s=document.querySelector('.imageview-surface'),r=s.getBoundingClientRect();window.imagePointer=(type,id,x,y)=>s.dispatchEvent(new PointerEvent(type,{pointerId:id,pointerType:'touch',button:0,clientX:r.left+x,clientY:r.top+y,bubbles:true,cancelable:true}));
          imagePointer('pointerdown',1,120,220);imagePointer('pointerdown',2,220,220);imagePointer('pointermove',2,320,220);imagePointer('pointerup',2,320,220);imagePointer('pointerup',1,120,220);return true})()''')
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-percent").textContent'), '46%')
        self.cli('click', '.imageview-fit')
        self.cli('eval', "imagePointer('pointerdown',1,195,300);imagePointer('pointerup',1,195,300);imagePointer('pointerdown',1,195,300);imagePointer('pointerup',1,195,300);true")
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-percent").textContent'), '100%')
        self.assertEqual(self.cli('eval', '[".imageview-fit",".imageview-actual"].map(s=>document.querySelector(s).getAttribute("aria-pressed"))'), ['false', 'true'])
        self.cli('eval', '''(()=>{const r=document.querySelector('.imageview-surface img').getBoundingClientRect(),s=document.querySelector('.imageview-stage').getBoundingClientRect();
          const dx=-560-r.left,dy=s.top-356-r.top;imagePointer('pointerdown',1,195,400);imagePointer('pointermove',1,195+dx,400+dy);imagePointer('pointerup',1,195+dx,400+dy);return true})()''')
        self.assertTrue(self.cli('eval', 'document.querySelector(".imageview-surface").getBoundingClientRect().bottom===document.querySelector(".imageview-stage").getBoundingClientRect().bottom'))
        self.screenshot('image-mobile-zoom')
        self.cli('back')
        self.ready('!document.querySelector(".imageview")')
        self.assertTrue(self.cli('eval', 'imageTerm.term.element.isConnected && imageTerm.term.buffer.active.getLine(1).translateToString(true).includes("Unsent terminal draft")'))
        # A recognized source reference must not silently follow another active session.
        self.select(self.other)
        self.cli('eval', 'imageLink.activate({});true')
        self.image_ready()
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-surface img").naturalWidth'), 1600)
        self.assertEqual(self.cli('eval', 'document.querySelector(".session-label").textContent'), 'Other session')
        self.close_image()
        self.select(self.source)
        self.tab('Conversation')
        self.ready('!!document.querySelector(".turns .md-image")')
        self.assertFalse(self.cli('eval', '!!document.querySelector(".turns img")'))
        self.cli('click', '.turns .md-image[title="sample.svg"]')
        self.image_ready(); self.close_image()
        self.assertTrue(self.cli('eval', 'document.querySelector(".turns").checkVisibility()'))
        self.cli('eval', '[...document.querySelectorAll(".turns .md-image")].find(b=>b.textContent.startsWith("External")).click();true')
        self.image_ready(); self.close_image()
        self.assertTrue(self.cli('eval', 'document.querySelector(".turns").checkVisibility()'))
        self.tab('Files')
        self.ready('!!document.querySelector(\'.tnode.file[data-nav="sample.svg"]\')')
        self.cli('click', '.tnode.file[data-nav="sample.svg"]')
        self.ready('!!document.querySelector(".fimg img")?.naturalWidth')
        self.cli('click', '.fimg .image-open'); self.image_ready(); self.close_image()
        self.assertTrue(self.cli('eval', 'document.querySelector(".fimg img").naturalWidth===1600'))
        self.cli('click', '.fimg img'); self.image_ready()
        self.cli('set', 'viewport', '1024', '800')
        self.ready('document.querySelector(".imageview-head").getBoundingClientRect().height===48')
        self.assertAlmostEqual(self.cli('eval', 'document.querySelector(".imageview-surface img").getBoundingClientRect().height'), 668)
        self.screenshot('image-desktop-fit')
        self.cli('set', 'viewport', '320', '568')
        self.cli('eval', 'window.imageUiScale=document.documentElement.style.getPropertyValue("--ui-scale");document.documentElement.style.setProperty("--ui-scale","1.5");true')
        self.ready('document.querySelector(".imageview-title").getBoundingClientRect().bottom<=document.querySelector(".imageview-head").getBoundingClientRect().bottom')
        self.assertTrue(self.cli('eval', '[...document.querySelectorAll(".imageview button")].filter(b=>b.checkVisibility()).every(b=>{const r=b.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})'))
        self.screenshot('image-mobile-large-text')
        self.cli('eval', 'if(imageUiScale)document.documentElement.style.setProperty("--ui-scale",imageUiScale);else document.documentElement.style.removeProperty("--ui-scale");true')
        self.cli('set', 'viewport', '844', '390')
        self.ready('document.querySelector(".imageview-tools").getBoundingClientRect().bottom<=innerHeight')
        self.cli('press', 'Escape')
        self.ready('!document.querySelector(".imageview") && !history.state?.piperImage')
        # The preview URL is borrowed; closing the viewer must leave Files fully usable.
        self.cli('click', '.fimg .image-open'); self.image_ready(); self.close_image()
        self.tab('Terminal'); self.open_path(str(self.external)); self.close_image()
        self.open_path(str(self.external.parent), image=False)
        # a folder outside the projects opens in Files' External group; its image is an ordinary tab
        self.ready('[...document.querySelectorAll(".fext [data-nav]")].some(b=>b.dataset.nav.endsWith("//outside.svg"))')
        self.cli('eval', '[...document.querySelectorAll(".fext [data-nav]")].find(b=>b.dataset.nav.endsWith("//outside.svg")).click();true')
        self.ready('!!document.querySelector(".ftab.ext.on") && !!document.querySelector(".fbody .fimg img")?.naturalWidth')
        self.cli('click', '.fbody .image-open'); self.image_ready(); self.close_image()
        self.assertTrue(self.cli('eval', 'document.querySelector(".fbody .fimg img").naturalWidth===1600'))
        self.tab('Terminal')
        self.open_path('hostile.svg')
        self.assertFalse(self.cli('eval', 'document.body.dataset.imageScript==="ran"'))
        self.close_image()
        self.open_path('pixel.png')
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-surface img").naturalWidth'), 1)
        self.assertEqual(self.cli('eval', 'document.querySelector(".imageview-surface img").getBoundingClientRect().width'), 1)
        self.close_image()
        # Render the unmodified Claude SVG exports at the same viewport for side-by-side inspection.
        if os.environ.get('PIPER_E2E_IMAGE_SAMPLE'):
            designs = Path(os.environ['PIPER_E2E_IMAGE_SAMPLE']).parent
            for name, width, height in [('mobile-fit', 390, 844), ('mobile-zoomed', 390, 844), ('desktop-fit', 1024, 800)]:
                self.cli('set', 'viewport', str(width), str(height))
                self.cli('open', (designs/(name+'.svg')).as_uri())
                self.screenshot('design-'+name)

    def test_new_session_dialog_scrolls_above_the_phone_keyboard(self):
        # Shares this mobile shell (390 x 844, mobile.css, the viewport tracker). The keyboard is simulated as Safari
        # reports it: a shorter visual viewport, which the tracker re-measures on focus and its delayed checks.
        self.cli('eval', 'document.querySelector(".q-new").click();true')
        self.ready('!!document.querySelector(".workdlg #new-session-task")')
        self.cli('eval', '''(()=>{Object.defineProperty(visualViewport,'height',{configurable:true,get:()=>420});
          document.querySelector('#new-session-task').focus();visualViewport.dispatchEvent(new Event('resize'));return true})()''')
        self.ready('document.querySelector(".workdlg").closest(".modal").getBoundingClientRect().bottom<=420')
        reached = self.cli('eval', '''(()=>{const form=document.querySelector('.wk-form'),task=document.querySelector('#new-session-task');
          task.scrollIntoView({block:'end'});const box=task.getBoundingClientRect();
          return {scrolls:form.scrollHeight>form.clientHeight,top:form.scrollTop>0,visible:box.bottom<=420&&box.top>=0}})()''')
        self.assertEqual(reached, {'scrolls': True, 'top': True, 'visible': True}, 'the task field must scroll into the area above the keyboard')
        self.screenshot('new-session-keyboard')
        self.cli('eval', "delete visualViewport.height;document.activeElement.blur();visualViewport.dispatchEvent(new Event('resize'));true")
        self.ready('document.querySelector(".workdlg").closest(".modal").getBoundingClientRect().bottom>420')

    def test_phone_new_session_and_resume_are_whole_screens(self):
        # phase 8: Start work (New session, Resume conversation) is a whole screen with 44 pt controls, not a card
        self.cli('eval', 'document.querySelector(".q-new").click();true')
        self.ready('!!document.querySelector(".workdlg #new-session-task")')
        small = '''(sel=>[...document.querySelectorAll(sel)].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).map(b=>b.getAttribute('aria-label')||b.textContent.trim()||b.className))'''
        measure = '''(()=>{const m=document.querySelector(".workdlg").closest(".modal").getBoundingClientRect();
          return {box:[Math.round(m.left),Math.round(m.right),Math.round(m.top),Math.round(m.bottom)],wide:document.querySelector(".workdlg").scrollWidth,
            small:%s(".workdlg button:not(.link), .workdlg input:not([type=checkbox]):not([type=radio]), .workdlg select"),
            zoom:[...document.querySelectorAll(".workdlg input:not([type=checkbox]):not([type=radio]), .workdlg textarea")].filter(e=>e.checkVisibility()&&parseFloat(getComputedStyle(e).fontSize)<16).map(e=>e.id||e.className)}})()''' % small
        new = self.cli('eval', measure)
        self.assertEqual(new['box'], [0, 390, 0, 844], 'New session fills the screen')
        self.assertLessEqual(new['wide'], 390)
        self.assertEqual(new['small'], [])
        self.assertEqual(new['zoom'], [], 'a field under 16 px zooms the page on focus')
        self.screenshot('phone-new-session')
        self.cli('eval', '[...document.querySelectorAll(".workdlg .wk-kind")].find(b=>b.textContent.includes("Resume")).click();true')
        self.ready('!!document.querySelector(".workdlg .wk-resume input[type=search]")')
        resume = self.cli('eval', measure)
        self.assertEqual(resume['box'], [0, 390, 0, 844], 'Resume fills the screen')
        self.assertLessEqual(resume['wide'], 390)
        self.assertEqual(resume['small'], [])
        self.assertEqual(resume['zoom'], [])
        self.screenshot('phone-resume')
        self.cli('eval', 'document.querySelector(".workdlg .wk-close").click();true')
        self.ready('!document.querySelector(".workdlg")')

    def test_phone_resume_lists_over_the_whole_height_with_filters_in_a_sheet_and_the_detail_after_a_tap(self):
        # session C (board R): the list showed ~2 rows under half a screen of filters and a detail, its footer wrapped
        self.cli('eval', 'document.querySelector(".q-new").click();true')
        self.ready('!!document.querySelector(".workdlg #new-session-task")')
        self.cli('eval', '[...document.querySelectorAll(".workdlg .wk-kind")].find(b=>b.textContent.includes("Resume")).click();true')
        self.ready('document.querySelectorAll(".workdlg .resume-result").length>=2')
        layout = '''(()=>{const v=e=>!!e&&e.checkVisibility(),r=e=>e.getBoundingClientRect(),q=s=>document.querySelector(".workdlg "+s);
          const foot=[...document.querySelectorAll(".workdlg .resume-footer button")].filter(v);
          return {list:v(q(".resume-results"))?Math.round(r(q(".resume-results")).height):0,
            rows:[...document.querySelectorAll(".workdlg .resume-result")].filter(v).map(b=>Math.round(r(b).height)),
            detail:v(q(".resume-detail")),back:v(q(".rp-ph-back")),filters:v(q(".resume-filters")),sheet:v(q(".rp-ph-sheet")),
            bar:v(q(".rp-ph-bar"))?Math.round(r(q(".rp-ph-bar")).height):0,
            foot:foot.map(b=>(b.getAttribute("aria-label")||b.textContent).trim()),footTops:[...new Set(foot.map(b=>Math.round(r(b).top)))],
            footSmall:foot.filter(b=>r(b).height<44).length,footBottom:foot.length?Math.round(Math.max(...foot.map(b=>r(b).bottom))):0}})()'''
        listed = self.cli('eval', layout)
        self.assertGreaterEqual(listed['list'], 500, 'the list takes the height, not 2 rows')
        self.assertTrue(listed['rows'] and all(44 <= h <= 56 for h in listed['rows']), listed['rows'])
        self.assertEqual([listed['detail'], listed['back'], listed['filters'], listed['sheet']], [False, False, False, False])
        self.assertLessEqual(listed['bar'], 60)
        self.assertEqual(listed['foot'], ['New conversation…'])
        self.screenshot('phone-resume')
        # R3: the filters are a bottom sheet with 44 pt controls; a set filter is counted on the button
        self.cli('eval', 'document.querySelector(".workdlg .rp-ph-filters-btn").click();true')
        self.ready('document.querySelector(".workdlg .rp-ph-sheet")?.checkVisibility()')
        sheet = self.cli('eval', '''(()=>{const s=document.querySelector(".workdlg .rp-ph-sheet"),b=s.getBoundingClientRect();
          return {bottom:Math.round(b.bottom),small:[...s.querySelectorAll("button:not(.link), select")].filter(e=>e.checkVisibility()&&e.getBoundingClientRect().height<44).map(e=>e.className||e.textContent)}})()''')
        self.assertEqual(sheet, {'bottom': 844, 'small': []})
        self.cli('eval', '(s=>{s.value="claude";s.dispatchEvent(new Event("change"))})(document.querySelector(".workdlg select[aria-label=\'Filter conversations by agent\']"));true')
        self.ready('document.querySelector(".workdlg .rp-ph-count").textContent==="1"')
        self.screenshot('phone-resume-filters')
        self.cli('eval', 'document.querySelector(".workdlg .rp-ph-done").click();true')
        self.ready('!document.querySelector(".workdlg .rp-ph-sheet").checkVisibility()')
        self.cli('eval', '(s=>{s.value="";s.dispatchEvent(new Event("change"))})(document.querySelector(".workdlg select[aria-label=\'Filter conversations by agent\']"));true')
        self.ready('document.querySelectorAll(".workdlg .resume-result").length>=2 && document.querySelector(".workdlg .rp-ph-count").textContent===""')
        # R2: a tap opens the detail under ‹ Conversations; its actions are one bar of 44 pt buttons
        self.cli('eval', 'document.querySelectorAll(".workdlg .resume-result")[1].click();true')
        self.ready('document.querySelector(".workdlg .resume-detail")?.checkVisibility() && !!document.querySelector(".workdlg .resume-detail h3")')
        shown = self.cli('eval', layout)
        self.assertEqual([shown['detail'], shown['back'], shown['list']], [True, True, 0])
        self.assertIn('Resume selected conversation', shown['foot'])
        self.assertIn('Fork selected conversation', shown['foot'])
        self.assertNotIn('New conversation…', shown['foot'])
        self.assertEqual(len(shown['footTops']), 1, 'the actions do not wrap')
        self.assertEqual(shown['footSmall'], 0)
        self.assertLessEqual(shown['footBottom'], 844)
        self.screenshot('phone-resume-detail')
        self.cli('eval', 'document.querySelector(".workdlg .rp-ph-back").click();true')
        self.ready('document.querySelector(".workdlg .resume-results").checkVisibility() && !document.querySelector(".workdlg .resume-detail").checkVisibility()')
        self.cli('eval', 'document.querySelector(".workdlg .wk-close").click();true')
        self.ready('!document.querySelector(".workdlg")')

    def test_folder_link_on_a_phone_opens_the_files_tab_not_a_side_pane(self):
        # A folder named in the terminal opened Files beside the terminal: two 154 px panes, neither usable.
        designs = Path(self.root, 'designs'); designs.mkdir()
        (designs / 'one.png').write_bytes(Path(self.root, 'pixel.png').read_bytes())
        self.open_path(str(designs), image=False)
        # board 7: Files is a screen pushed over the session (‹ back to it), not a segment tab
        self.ready('document.querySelector("header.ph-pushed .ph-view")?.textContent==="Files" && !document.querySelector("header .ph-seg")')
        shown = self.cli('eval', '''(()=>({panes:document.querySelectorAll(".filespane").length,
          row:[...document.querySelectorAll(".tnode.file")].find(e=>e.dataset.nav==="designs/one.png")?.checkVisibility()}))()''')
        self.assertEqual(shown, {'panes': 0, 'row': True})
        self.screenshot('folder-link-phone')
        self.cli('click', '.tnode.file[data-nav="designs/one.png"]')
        self.ready('!!document.querySelector(".fimg img")?.naturalWidth')

    def test_finger_tap_on_a_wrapped_image_path_opens_the_image(self):
        # On an iPhone a tap on a path only raised the keyboard (xterm links follow the mouse hover), and a path
        # wrapped by the agent's renderer at the 36-column phone width must open the image, not its folder.
        folder = Path(self.root, 'development', 'mobile-ui-redesign'); folder.mkdir(parents=True)
        path = str(folder / '1-sessions.png')
        Path(path).write_bytes(Path(self.root, 'pixel.png').read_bytes())
        width = self.cli('eval', 'imageTerm.term.cols') - 2
        rows = ['  ' + path[i:i + width] for i in range(0, len(path), width)]
        self.assertGreater(len(rows), 2)
        self.cli('eval', '''(async()=>{const t=imageTerm.term;imageTerm.freeze(true);t.reset();
          await new Promise(done=>t.write(%s,done));return true})()''' % json.dumps('⏺ Designs:\r\n\r\n' + '\r\n'.join(rows) + '\r\n'))
        tap = '''(row=>{const s=imageTerm.host.querySelector('.xterm-screen').getBoundingClientRect(),t=imageTerm.term;
          const p=new Touch({identifier:1,target:imageTerm.host,clientX:s.left+s.width/t.cols*6,clientY:s.top+s.height/t.rows*(row+.5)});
          imageTerm.host.dispatchEvent(new TouchEvent('touchstart',{touches:[p],changedTouches:[p],bubbles:true,cancelable:true}));
          const end=new TouchEvent('touchend',{touches:[],changedTouches:[p],bubbles:true,cancelable:true});
          imageTerm.host.dispatchEvent(end);return end.defaultPrevented})(%d)'''
        self.assertFalse(self.cli('eval', tap % 0), 'a tap on plain text still opens the keyboard')
        self.assertTrue(self.cli('eval', tap % (1 + len(rows))), 'the tap on the path is handled, no keyboard')
        self.image_ready()
        self.assertTrue(self.cli('eval', 'document.querySelector(".imageview-title").textContent.startsWith("1-sessions.png")'))
        self.close_image()

    def test_sandbox_only_screenshot_opens_in_the_image_viewer(self):
        # A Hole session's screenshot exists only in its container's /tmp: it reported "a binary file".
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        calls = []
        def image(sid, path):
            calls.append((sid, path))
            return 'image/png', png
        self.enterContext(mock.patch.object(self.manager, 'sandbox_image', side_effect=image))
        path = '/tmp/piper-sandbox-only-%d/shot.png' % os.getpid()
        self.open_path(path)
        self.assertEqual(calls, [(self.source.id, path)])
        self.assertIn('sandbox only', self.cli('eval', 'document.querySelector(".imageview-info").textContent'))
        self.screenshot('sandbox-image')
        self.close_image()

    def phone_changes(self):
        """The source checkout with one long uncommitted change, its repository recorded as trusted."""
        self.enterContext(mock.patch.object(gitops, 'FAIL_CLOSED', False))
        git = lambda *a: subprocess.run(['git', '-C', self.root, '-c', 'user.name=t', '-c', 'user.email=t@example.com', *a], check=True, capture_output=True)
        git('add', '-A'); git('commit', '-qm', 'base', '--allow-empty')
        Path(self.root, 'README.md').write_text('![Draft](sample.svg)\n' + ''.join('line %d of a longer readme text that wraps on a phone screen\n' % i for i in range(60)))
        running = dict(self.manager.sessions); self.manager.sessions.clear()
        self.assertTrue(self.manager.repo_top(self.root))
        self.manager.sessions.update(running)
        self.select(self.source)

    def test_phone_commit_failure_stays_as_a_strip_with_the_hook_output(self):
        # board 6b: a rejected commit (a hook) was only a toast that went away; the strip keeps it above the action bar
        self.phone_changes()
        output = 'pre-commit hook failed (exit 1)\nlint: README.md:3 line too long\nlint: README.md:9 trailing space'
        self.enterContext(mock.patch.object(self.manager, 'git_commit', side_effect=ValueError(output)))
        self.cli('eval', 'document.querySelector("header .ph-more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Commit & Push")).click();true')
        self.ready('document.body.classList.contains("phone-commit") && !!document.querySelector("#inspector [data-git-path]")')
        self.cli('eval', '''(()=>{const c=document.querySelector("#inspector li:has([data-git-path]) > input[type=checkbox]");if(!c.checked)c.click();
          const m=document.querySelector("#inspector .gmsg textarea");m.value="Add the readme lines";m.dispatchEvent(new Event("input",{bubbles:true}));return true})()''')
        self.ready('document.querySelector("#inspector .gfoot .split button")?.disabled===false')
        self.cli('eval', '[...document.querySelectorAll("#inspector .gfoot .split button")][0].click();true')
        self.ready('!!document.querySelector("#inspector .gcommit-err")')
        strip = self.cli('eval', '''(()=>{const e=document.querySelector("#inspector .gcommit-err"),r=e.getBoundingClientRect(),f=document.querySelector("#inspector .gfoot").getBoundingClientRect();
          return {line:e.querySelector(".gce-line").textContent,above:r.bottom<=f.top+1,box:[Math.round(r.left),Math.round(r.right)],
            small:[...e.querySelectorAll("button")].filter(b=>b.getBoundingClientRect().height<44).map(b=>b.textContent)}})()''')
        self.assertEqual(strip['line'], 'pre-commit hook failed (exit 1)')
        self.assertTrue(strip['above'], 'the strip is above the action bar')
        self.assertGreaterEqual(strip['box'][0], 0); self.assertLessEqual(strip['box'][1], 390)
        self.assertEqual(strip['small'], [])
        self.cli('eval', 'document.querySelector("#inspector .gce-show").click();true')
        self.ready('!!document.querySelector("#inspector .gce-out")')
        self.assertEqual(self.cli('eval', 'document.querySelector("#inspector .gce-out").textContent'), output)
        self.screenshot('phone-commit-error')
        self.cli('eval', 'document.querySelector("#inspector .gce-x").click();true')
        self.ready('!document.querySelector("#inspector .gcommit-err")')

    def test_review_tab_on_a_phone_shows_a_full_width_wrapped_diff(self):
        # 390 px: the turn list beside the diff left it ~165 px with clipped lines
        self.phone_changes()
        self.tab('Review')
        self.ready('!!document.querySelector(".tr-line.add")')
        geo = self.cli('eval', '''(()=>{const d=document.querySelector(".tr-detail"),t=document.querySelector(".tr-turns-toggle").getBoundingClientRect();
          return {detail:d.getBoundingClientRect().width,overflow:d.scrollWidth-d.clientWidth,toggle:t.width,list:document.querySelector(".tr-all").getClientRects().length}})()''')
        self.assertGreater(geo['detail'], 320)
        self.assertEqual(geo['overflow'], 0, 'long lines wrap instead of running off the screen')
        self.assertEqual(geo['list'], 0, 'the turn list stays closed under the chosen turn')
        self.screenshot('review-tab-phone')
        # board B2: the turns open as a sheet (not inline over the diff)
        self.cli('click', '.tr-turns-toggle')
        self.ready('!!document.querySelector(".sheet[aria-label=Turns] .sheet-row")')
        self.screenshot('review-tab-phone-turns')
        self.cli('eval', 'document.querySelector(".sheet[aria-label=Turns] .sheet-row").click();true')
        self.ready('!document.querySelector(".sheet") && !document.querySelector(".turn-review").classList.contains("turns-open")')

    def test_git_review_on_a_phone_shows_one_version_full_width(self):
        # 390 px: Original and Working side by side were 44 and 80 px wide
        self.phone_changes()
        # the phone opens the git panel from ⋯ › Commit & Push (Session info); the file's review opens over it
        self.cli('eval', 'document.querySelector("header .ph-more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Commit & Push")).click();true')
        self.ready('document.body.classList.contains("phone-info") && !!document.querySelector("#inspector [data-git-path]")')
        self.cli('eval', 'document.querySelector("[data-git-path]").click();true')
        self.ready('document.querySelectorAll(".review-merge .cm-editor").length===2')
        self.assertTrue(self.cli('eval', '(()=>{const r=document.querySelector(".reviewpane").getBoundingClientRect();return document.elementFromPoint(r.left+r.width/2,r.top+r.height/2)?.closest(".reviewpane")!==null})()'),
                        'the review is above Session info, not under it')
        widths = 'Object.fromEntries([...document.querySelectorAll(".review-merge .cm-mergeViewEditor")].map(e=>[e.querySelector(".cm-merge-a")?"a":"b",Math.round(e.getBoundingClientRect().width)]))'
        self.assertEqual(self.cli('eval', widths)['a'], 1)
        self.assertGreater(self.cli('eval', widths)['b'], 300)
        moved = self.cli('eval', '''(async()=>{const s=document.querySelector(".cm-merge-b").closest(".cm-editor").querySelector(".cm-scroller");s.scrollTop=200;s.dispatchEvent(new Event("scroll"));
          await new Promise(r=>setTimeout(r,200));return Math.round(document.querySelector(".cm-merge-a").closest(".cm-editor").querySelector(".cm-scroller").scrollTop)})()''')
        self.assertGreater(moved, 150, 'the hidden version keeps scrolling with the shown one, so block operations stay beside their change')
        self.screenshot('git-review-phone')
        self.cli('eval', '[...document.querySelectorAll(".review-side-switch button")].find(b=>b.textContent==="Original").click();true')
        self.ready('document.querySelector(".reviewpane").classList.contains("review-phone-original")')
        self.assertEqual(self.cli('eval', widths)['b'], 1)
        self.assertGreater(self.cli('eval', widths)['a'], 300)
        self.screenshot('git-review-phone-original')

    def test_phone_turn_review_sheet_revert_and_commit_screen(self):
        # Claude board B2 / 6: the turns are a sheet, Revert to here confirms in a sheet, Commit is its own screen
        self.phone_changes()
        now = time.time()
        turn = {'n': 1, 'prompt': 'Add the readme lines', 'started': now - 600, 'ended': now - 500, 'from': 'a', 'to': 'b', 'summary': {'files': 1, 'add': 60, 'del': 0}}
        self.enterContext(mock.patch.object(self.manager, 'turns', return_value={'turns': [turn]}))
        self.enterContext(mock.patch.object(self.manager, 'turn_revert_preview', return_value={'n': 1, 'undo': '', 'current': 'c', 'files': [{'path': 'README.md', 'removed': False}]}))
        self.tab('Review')
        self.ready('!!document.querySelector(".tr-line.add") && !!document.querySelector(".tr-turns-toggle")')
        self.cli('eval', 'document.querySelector(".tr-turns-toggle").click();true')
        self.ready('!!document.querySelector(".sheet[aria-label=Turns] .sheet-row")')
        small = '''(sel=>[...document.querySelectorAll(sel)].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).map(b=>b.textContent))'''
        sheet = self.cli('eval', '''(()=>{const s=document.querySelector(".sheet[aria-label=Turns]"),r=s.getBoundingClientRect();
          return {rows:[...s.querySelectorAll(".sheet-row .sheet-label")].map(e=>e.textContent),box:[r.left,r.right,Math.round(r.bottom)],small:%s(".sheet button")}})()''' % small)
        self.assertEqual(sheet['rows'][:2], ['All turns since the session started', 'turn 1 · Add the readme lines'])
        self.assertEqual(sheet['box'], [0, 390, 844])
        self.assertEqual(sheet['small'], [])
        self.screenshot('phone-review-turns-sheet')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("turn 1")).click();true')
        self.ready('!document.querySelector(".sheet") && document.querySelector(".tr-toggle-label").textContent.startsWith("turn 1")')
        self.cli('eval', 'document.querySelector(".tr-turns-toggle").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row.danger")')
        self.assertIn('Revert to here', self.cli('eval', 'document.querySelector(".sheet .sheet-row.danger").textContent'))
        self.cli('eval', 'document.querySelector(".sheet .sheet-row.danger").click();true')
        self.ready('!!document.querySelector(".modal .tr-confirm")')
        confirm = self.cli('eval', '''(async()=>{const m=document.querySelector(".modal:has(.tr-confirm)");await Promise.all(m.getAnimations().map(a=>a.finished));const r=m.getBoundingClientRect();
          return {box:[r.left,r.right,Math.round(r.bottom)],small:%s(".modal:has(.tr-confirm) button"),apply:getComputedStyle([...m.querySelectorAll("button")].at(-1)).color===getComputedStyle(document.body).getPropertyValue("--bad").trim()||[...m.querySelectorAll("button")].at(-1).classList.contains("danger")}})()''' % small)
        self.assertEqual(confirm['box'], [0, 390, 844])
        self.assertEqual(confirm['small'], [])
        self.assertTrue(confirm['apply'], 'Revert files is the red S8 button')
        self.screenshot('phone-review-revert')
        self.cli('eval', '[...document.querySelectorAll(".modal button")].find(b=>b.textContent==="Cancel").click();true')
        self.ready('!document.querySelector(".modal .tr-confirm")')
        foot = self.cli('eval', '[...document.querySelectorAll(".tr-foot button")].map(b=>Math.round(b.getBoundingClientRect().height))')
        self.assertTrue(all(h >= 48 for h in foot), foot)
        self.screenshot('phone-review')
        # board 6: ⋯ › Commit & Push opens a Commit screen with the panel alone, not Session info scrolled to it
        self.cli('eval', 'document.querySelector("header .ph-more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Commit & Push")).click();true')
        self.ready('document.body.classList.contains("phone-commit") && !!document.querySelector("#inspector [data-git-path]")')
        commit = self.cli('eval', '''(()=>{const head=document.querySelector("#inspector .ph-info-head strong").textContent,
          others=[...document.querySelectorAll("#inspector .sec")].filter(s=>s.checkVisibility()&&!s.contains(document.querySelector("#inspector textarea"))).length;
          return {head,others,small:%s("#inspector button"),wide:document.querySelector("#inspector").scrollWidth}})()''' % small)
        self.assertEqual(commit['head'], 'Commit')
        self.assertEqual(commit['others'], 0, 'only the git panel shows')
        self.assertEqual(commit['small'], [])
        self.assertLessEqual(commit['wide'], 390)
        self.screenshot('phone-commit')
        self.cli('eval', 'document.querySelector("#inspector .ph-info-done").click();true')
        self.ready('!document.body.classList.contains("phone-commit") && !document.body.classList.contains("phone-info")')

    def test_phone_editor_wraps_lines_and_shows_a_key_row_while_editing(self):
        # boards 8 / 8b: a long line scrolled sideways off a 390 pt screen and the keyboard has no tab { } ( ) ;
        Path(self.root, 'wrap.ts').write_text('export function total(items: Item[]) {\n  return items.map((item) => item.price * item.quantity).reduce((sum, value) => sum + value, 0);\n}\n')
        self.tab('Files')
        self.ready('!!document.querySelector(".files .tnode.file[data-nav=\'wrap.ts\']")')
        self.cli('eval', 'document.querySelector(".files .tnode.file[data-nav=\'wrap.ts\']").click();true')
        self.ready('!!document.querySelector(".fcode .cm-editor .cm-line")')
        read = self.cli('eval', '''(()=>{const e=document.querySelector(".fcode .cm-editor"),num=[...e.querySelectorAll(".cm-lineNumbers .cm-gutterElement")].find(g=>g.textContent==="2");
          return {wrap:e.querySelector(".cm-content").classList.contains("cm-lineWrapping"),side:e.querySelector(".cm-scroller").scrollWidth-e.querySelector(".cm-scroller").clientWidth,
            rows:Math.round(num.getBoundingClientRect().height/20),hook:getComputedStyle(num,"::after").backgroundImage.includes("svg"),keys:document.querySelector(".fe-keys")?.checkVisibility()??false}})()''')
        self.assertTrue(read['wrap'], 'the phone editor soft-wraps')
        self.assertLessEqual(read['side'], 0, 'no sideways scroll')
        self.assertGreaterEqual(read['rows'], 2, 'the long line wraps onto more rows')
        self.assertTrue(read['hook'], 'wrapped rows show ↪ in the gutter')
        self.assertFalse(read['keys'], 'no key row while reading')
        self.cli('click', '.fcode .cm-content .cm-line')
        self.cli('eval', '''(()=>{const l=document.querySelector(".fcode .cm-line"),r=document.createRange();r.setStart(l.firstChild,0);r.collapse(true);
          getSelection().removeAllRanges();getSelection().addRange(r);return true})()''')
        self.ready('document.querySelector(".fe-keys")?.checkVisibility()')
        keys = self.cli('eval', '''(()=>{const k=[...document.querySelectorAll(".fe-keys button")];return {labels:k.map(b=>b.textContent),
          small:k.filter(b=>b.getBoundingClientRect().height<44||b.getBoundingClientRect().width<44).length,
          below:Math.round(document.querySelector(".fe-keys").getBoundingClientRect().bottom)}})()''')
        self.assertEqual(keys['labels'], ['tab', '{', '}', '(', ')', ';', '←', '→'])
        self.assertEqual(keys['small'], 0)
        self.assertLessEqual(keys['below'], 844)
        self.cli('eval', '[...document.querySelectorAll(".fe-keys button")].find(b=>b.textContent==="{").dispatchEvent(new PointerEvent("pointerdown",{bubbles:true,cancelable:true}));[...document.querySelectorAll(".fe-keys button")].find(b=>b.textContent==="{").click();true')
        self.ready('document.querySelector(".fcode .cm-line").textContent.startsWith("{export")')
        self.assertTrue(self.cli('eval', 'document.querySelector(".fcode .cm-editor").classList.contains("cm-focused")'), 'the editor keeps the focus')
        self.screenshot('phone-editor-edit')
        self.cli('eval', '[...document.querySelectorAll(".fe-keys button")].find(b=>b.getAttribute("aria-label")==="Caret left").click();[...document.querySelectorAll(".fe-keys button")].find(b=>b.textContent==="tab").click();true')
        self.ready('/^\\s+\\{export/.test(document.querySelector(".fcode .cm-line").textContent)')

    def test_phone_open_file_and_move_are_bottom_sheets_with_a_file_search(self):
        # session C: Move… was a small desktop picker on the phone and Files had no way to search for a file to open
        Path(self.root, 'docs').mkdir(exist_ok=True)
        Path(self.root, 'docs', 'guide.md').write_text('# Guide\n')
        self.tab('Files')
        self.ready('!!document.querySelector(".files .tree-open")')
        geometry = '''(()=>{const m=document.querySelector(".modal:has(> .plist.tall)"),r=m.getBoundingClientRect(),i=m.querySelector("input[type=search]");
          return {box:[Math.round(r.left),Math.round(r.right),Math.round(r.bottom)],title:m.querySelector("h2")?.textContent,
            field:[Math.round(i.getBoundingClientRect().height),parseFloat(getComputedStyle(i).fontSize)],
            small:[...m.querySelectorAll(".pitem")].filter(b=>b.getBoundingClientRect().height<44).length,rows:m.querySelectorAll(".pitem").length}})()'''
        self.assertGreaterEqual(self.cli('eval', 'Math.round(document.querySelector(".files .tree-open").getBoundingClientRect().height)'), 44)
        self.cli('eval', 'document.querySelector(".files .tree-open").click();true')
        self.ready('!!document.querySelector(".modal:has(> .plist.tall) input[type=search]")')
        self.cli('eval', '(i=>{i.value="guide";i.dispatchEvent(new Event("input"))})(document.querySelector(".modal:has(> .plist.tall) input[type=search]"));true')
        self.ready('[...document.querySelectorAll(".modal .pitem")].some(b=>b.textContent.includes("guide.md"))')
        found = self.cli('eval', geometry)
        self.assertEqual(found['box'], [0, 390, 844], 'Open file is a sheet over the whole width')
        self.assertEqual(found['field'], [44, 16])
        self.assertEqual(found['small'], 0)
        self.screenshot('phone-open-file')
        self.cli('eval', '[...document.querySelectorAll(".modal .pitem")].find(b=>b.textContent.includes("guide.md")).click();true')
        self.ready('!document.querySelector(".modal:has(> .plist.tall)") && !!document.querySelector(".fcode .cm-editor, .file-md-mode")')
        self.cli('eval', 'document.querySelector(".files .tnode.file[data-nav=\'README.md\'] .tmore").click();true')
        self.ready('[...document.querySelectorAll(".sheet .sheet-row")].some(b=>b.textContent.includes("Move"))')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Move")).click();true')
        self.ready('[...document.querySelectorAll(".modal .pitem")].some(b=>b.textContent.includes("docs"))')
        moved = self.cli('eval', geometry)
        self.assertEqual(moved['box'], [0, 390, 844], 'Move… is a sheet over the whole width')
        self.assertTrue(moved['title'].startswith('Move README.md to'))
        self.assertEqual(moved['small'], 0)
        self.screenshot('phone-move-sheet')
        self.cli('eval', 'document.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}));true')

    def test_phone_files_row_actions_sheet_delete_confirm_and_markdown_preview(self):
        # Claude boards 7 / 7b / 8: a tree row has ⋯ (44 pt) whose actions are a sheet; Delete confirms in a sheet;
        # a Markdown file opens with Preview / Edit; nothing is wider than the phone
        self.tab('Files')
        self.ready('!!document.querySelector(".files .tnode.file[data-nav=\'README.md\']")')
        small = '''(sel=>[...document.querySelectorAll(sel)].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).map(b=>b.getAttribute('aria-label')||b.textContent))'''
        tree = self.cli('eval', '''(()=>({rows:%s(".files .tnode"),more:document.querySelectorAll(".files .tnode .tmore").length,wide:document.querySelector(".files").scrollWidth}))()''' % small)
        self.assertEqual(tree['rows'], [], 'tree rows are 44 pt')
        self.assertGreater(tree['more'], 0)
        self.assertLessEqual(tree['wide'], 390)
        self.screenshot('phone-files')
        self.cli('eval', 'document.querySelector(".files .tnode.file[data-nav=\'sample.svg\'] .tmore").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        rows = self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row .sheet-label")].map(e=>e.textContent)')
        for label in ('Rename', 'Move…', 'Delete', 'Copy path'):
            self.assertIn(label, rows)
        self.assertEqual(self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-group")].at(-1).textContent'), 'Delete', 'Delete alone in the last group')
        self.assertEqual(self.cli('eval', '%s(".sheet button")' % small), [])
        self.screenshot('phone-files-sheet')
        self.cli('eval', '[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent==="Delete").click();true')
        self.ready('!!document.querySelector(".modal .fask")')
        box = self.cli('eval', '''(async()=>{const m=document.querySelector(".modal:has(.fask)");await Promise.all(m.getAnimations().map(a=>a.finished));const r=m.getBoundingClientRect();
          return {box:[r.left,r.right,Math.round(r.bottom)],small:%s(".modal:has(.fask) button")}})()''' % small)
        self.assertEqual(box, {'box': [0, 390, 844], 'small': []})
        self.screenshot('phone-files-delete')
        self.cli('eval', '[...document.querySelectorAll(".modal button")].find(b=>b.textContent==="Cancel").click();true')
        self.ready('!document.querySelector(".modal .fask")')
        self.cli('eval', 'document.querySelector(".files .tnode.file[data-nav=\'README.md\']").click();true')
        self.ready('document.querySelector(".files .file-md-mode")?.checkVisibility() && !!document.querySelector(".files .file-md-mode .db-seg.on")')
        md = self.cli('eval', '''(()=>({on:document.querySelector(".files .file-md-mode .db-seg.on").textContent,small:%s(".files .fhead button").filter(x=>x!=="Preview"&&x!=="Edit"),seg:Math.round(document.querySelector(".files .file-md-mode").getBoundingClientRect().height),wide:document.querySelector(".files").scrollWidth}))()''' % small)
        self.assertEqual((md['on'], md['small']), ('Preview', []))
        self.assertGreaterEqual(md['seg'], 44, 'the Preview / Edit segment is a 44 pt target')
        self.assertLessEqual(md['wide'], 390)
        self.screenshot('phone-editor-preview')

    def test_phone_inbox_and_alerts_are_full_screens_above_the_tab_bar(self):
        # phase 6: Inbox and Alerts take the screen above the tab bar (the tab bar stays), with 44 pt controls
        _env.set_modules(self, gitlab=True)
        self.cli('eval', 'location.reload();true')
        self.ready('document.body.classList.contains("phone-shell") || !!document.querySelector("#actbar")')
        self.cli('eval', '''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
          await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
          const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
        self.ready('!!document.querySelector("#actbar .ab-tab[aria-label=Sessions]")')
        if not self.cli('eval', '!!document.querySelector("#actbar .ab-tab[aria-pressed=true][aria-label=Sessions]")'):
            self.cli('eval', 'document.querySelector("#actbar [aria-label=Sessions]").click();true')
        small = '''(sel=>[...document.querySelectorAll(sel)].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).map(b=>b.getAttribute('aria-label')||b.textContent))'''
        self.cli('eval', 'document.querySelector("#actbar [aria-label=\'Recent alerts\']").click();true')
        self.ready('!!document.querySelector("#notices")')
        alerts = self.cli('eval', '''(()=>{const r=document.querySelector("#notices").getBoundingClientRect(),bar=document.querySelector("#actbar").getBoundingClientRect();
          return {box:[r.left,r.right,Math.round(r.top),Math.round(r.bottom)],bar:Math.round(bar.top),small:%s("#notices button")}})()''' % small)
        self.assertEqual(alerts['box'], [0, 390, 0, alerts['bar']], 'Alerts fill the screen above the tab bar')
        self.assertEqual(alerts['small'], [])
        self.screenshot('phone-alerts')
        self.cli('eval', 'document.querySelector("#actbar [aria-label=\'Recent alerts\']").click();true')
        self.ready('!document.querySelector("#notices")')
        inbox = [b for b in self.cli('eval', '[...document.querySelectorAll("#actbar .ab-tab")].map(b=>b.getAttribute("aria-label"))') if 'nbox' in b or 'GitLab' in b]
        self.assertTrue(inbox, 'the Inbox tab is there with the gitlab module on')
        self.cli('eval', 'document.querySelector(%s).click();true' % json.dumps('#actbar [aria-label="%s"]' % inbox[0]))
        self.ready('!!document.querySelector(".inbox:not(.hidden)")')
        box = self.cli('eval', '''(()=>{const r=document.querySelector(".inbox").getBoundingClientRect();
          return {box:[r.left,r.right],wide:document.querySelector(".inbox").scrollWidth,small:%s(".inbox button, .inbox input, .inbox select"),tabs:getComputedStyle(document.querySelector("#actbar")).display}})()''' % small)
        self.assertEqual(box['box'], [0, 390])
        self.assertLessEqual(box['wide'], 390)
        self.assertEqual(box['small'], [])
        self.assertNotEqual(box['tabs'], 'none')
        self.screenshot('phone-inbox')

    def test_phone_agent_chat_is_a_tab_screen_above_the_tab_bar(self):
        # session C: with the Feed on the Agent tab opened the desktop drawer (a column) on the phone
        _env.set_modules(self, agent=True)
        self.cli('eval', 'location.reload();true')
        self.ready('!!document.querySelector("#actbar")')
        self.cli('eval', '''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
          await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
          const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
        self.ready('!!document.querySelector("#actbar .ab-tab[aria-label=Agent]")')
        self.cli('eval', 'document.querySelector("#actbar .ab-tab[aria-label=Agent]").click();true')
        self.ready('!!document.querySelector("#drawer:not(.hidden) .feed-input")')
        # measured once the drawer's slide-in has finished
        shown = self.cli('eval', '''(async()=>{const d=document.querySelector("#drawer");await Promise.all(d.getAnimations({subtree:true}).map(a=>a.finished));
          const r=d.getBoundingClientRect(),bar=document.querySelector("#actbar"),b=bar.getBoundingClientRect();
          const head=d.querySelector(".drawer-head").getBoundingClientRect(),v=e=>e.checkVisibility();
          return {box:[Math.round(r.left),Math.round(r.right),Math.round(r.top),Math.round(r.bottom)],bar:Math.round(b.top),tabs:getComputedStyle(bar).display,
            on:document.querySelector("#actbar .ab-tab[aria-label=Agent]").getAttribute("aria-pressed"),head:Math.round(head.height),
            small:[...d.querySelectorAll("button:not(.link)")].filter(e=>v(e)&&e.getBoundingClientRect().height<44).map(e=>e.getAttribute("aria-label")||e.textContent.trim()),
            zoom:[...d.querySelectorAll("textarea, input")].filter(e=>v(e)&&parseFloat(getComputedStyle(e).fontSize)<16).map(e=>e.className),
            wide:d.scrollWidth}})()''')
        self.assertNotEqual(shown['tabs'], 'none', 'the tab bar stays: Agent is one of its screens')
        self.assertEqual(shown['box'], [0, 390, 0, shown['bar']], 'the chat fills the screen above the tab bar')
        self.assertEqual(shown['on'], 'true')
        self.assertLessEqual(shown['head'], 52)
        self.assertLessEqual(shown['wide'], 390)
        self.assertEqual(shown['small'], [])
        self.assertEqual(shown['zoom'], [], 'a field under 16 px zooms the page on focus')
        self.screenshot('phone-agent')
        self.cli('eval', 'document.querySelector("#actbar .ab-tab[aria-label=Sessions]").click();true')
        self.ready('document.querySelector("#drawer").classList.contains("hidden")')

    def test_phone_settings_are_a_section_list_then_one_section_under_back(self):
        # phase 7: More › Settings is the section list alone (master); a section opens alone under ‹ Settings (detail)
        self.cli('eval', '''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
          await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
          const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
        self.ready('!!document.querySelector("#actbar [aria-label=More]")')
        self.cli('eval', 'document.querySelector("#actbar [aria-label=More]").click();true')
        self.ready('[...document.querySelectorAll(".sheet-row")].some(b=>b.textContent.trim()==="Settings")')
        self.cli('eval', '[...document.querySelectorAll(".sheet-row")].find(b=>b.textContent.trim()==="Settings").click();true')
        self.ready('!!document.querySelector(".settings .st-navi[data-nav]")?.checkVisibility()')
        small = '''(sel=>[...document.querySelectorAll(sel)].filter(b=>b.checkVisibility()&&b.getBoundingClientRect().height<44).map(b=>b.getAttribute('aria-label')||b.textContent.trim()))'''
        master = self.cli('eval', '''(()=>{const side=document.querySelector(".settings .st-side").getBoundingClientRect();
          return {side:[Math.round(side.left),Math.round(side.right)],sec:!!document.querySelector(".settings .st-sec")?.checkVisibility(),
            small:%s(".settings .st-navi, .settings .st-search"),wide:document.querySelector(".settings").scrollWidth}})()''' % small)
        self.assertEqual(master['side'], [0, 390], 'the section list takes the whole width')
        self.assertFalse(master['sec'], 'no section beside the list')
        self.assertEqual(master['small'], [])
        self.assertLessEqual(master['wide'], 390)
        self.screenshot('phone-settings')
        for sid, shot in (('security', 'phone-settings-security'), ('interface', 'phone-settings-interface'), ('mobile', 'phone-settings-mobile')):
            self.cli('eval', 'document.querySelector(%s).click();true' % json.dumps('.settings .st-navi[data-nav="%s"]' % sid))
            self.ready('!!document.querySelector(".settings .st-ph-back")?.checkVisibility()')
            detail = self.cli('eval', '''(()=>{const sec=document.querySelector(".settings .st-sec").getBoundingClientRect();
              return {sec:[Math.round(sec.left),Math.round(sec.right)],side:!!document.querySelector(".settings .st-side")?.checkVisibility(),
                small:%s(".settings .st-sec button:not(.link), .settings .st-sec input:not([type=checkbox]):not([type=radio]), .settings .st-sec select"),
                wide:document.querySelector(".settings .st-sec").scrollWidth}})()''' % small)
            self.assertEqual(detail['sec'], [0, 390], sid)
            self.assertFalse(detail['side'], 'the list goes while a section is open')
            self.assertEqual(detail['small'], [], sid)
            self.assertLessEqual(detail['wide'], 390, sid)
            self.screenshot(shot)
            self.cli('eval', 'document.querySelector(".settings .st-ph-back").click();true')
            self.ready('!!document.querySelector(".settings .st-side")?.checkVisibility() && !document.querySelector(".settings .st-sec")?.checkVisibility()')

    def test_error_retry_and_late_image_release(self):
        large = Path(self.root, 'large.svg')
        with large.open('wb') as stream:
            stream.truncate((10 << 20) + 1)
        self.open_path('large.svg', image=False)
        self.ready('document.querySelector(".imageview-status p")?.textContent.includes("10 MiB")')
        self.assertTrue(self.cli('eval', 'document.querySelector(".imageview-save").disabled && document.querySelector(".imageview-tools").hidden'))
        self.screenshot('image-error')
        large.write_bytes(Path(self.root, 'sample.svg').read_bytes())
        self.cli('click', '.imageview-status button')
        self.image_ready(); self.close_image()
        self.cli('eval', '''(async()=>{const {installTransport}=await import('/piper/dist/transport.js');const native=window.fetch.bind(window);
          window.pendingImageReads=[];window.imageObjects={created:[],revoked:[]};
          const create=URL.createObjectURL.bind(URL),revoke=URL.revokeObjectURL.bind(URL);
          URL.createObjectURL=blob=>{const url=create(blob);imageObjects.created.push(url);return url};
          URL.revokeObjectURL=url=>{imageObjects.revoked.push(url);revoke(url)};
          window.restoreImageTransport=()=>{URL.createObjectURL=create;URL.revokeObjectURL=revoke;installTransport({fetch:native,socket:url=>new WebSocket(url)})};
          installTransport({fetch:(path,init)=>path.startsWith('/api/fs/raw?')?new Promise(resolve=>pendingImageReads.push(()=>resolve(native(path,init)))):native(path,init),socket:url=>new WebSocket(url)});return true})()''')
        self.open_path('sample.svg', image=False)
        self.ready('pendingImageReads.length===1 && document.querySelector(".imageview-status")?.textContent.includes("Loading")')
        self.screenshot('image-loading')
        self.close_image()
        self.cli('eval', 'pendingImageReads[0]();true')
        self.ready('imageObjects.created.length===1 && imageObjects.revoked.includes(imageObjects.created[0])')
        self.cli('eval', 'restoreImageTransport();true')

    def test_original_download_when_native_browser_downloads_are_available(self):
        self.open_path('sample.svg')
        self.cli('eval', '(()=>{const a=document.createElement("a");a.id="control-download";a.download="control.txt";a.href=URL.createObjectURL(new Blob(["control"],{type:"text/plain"}));a.textContent="Control download";document.querySelector(".imageview-head").append(a);return true})()')
        # Some macOS Chrome builds cancel every download inside Seatbelt, including a plain text control.
        result = subprocess.run(['agent-browser', '--session', self.browser, '--json', 'download', '#control-download', str(self.downloads/'control.txt')],
                                capture_output=True, text=True, timeout=40)
        if result.returncode:
            self.assertIn('Download was canceled', result.stdout + result.stderr)
            self.skipTest('Sandboxed Chromium cancels even a plain Blob download; verify native saving on the target device')
        self.assertEqual((self.downloads/'control.txt').read_text(), 'control')
        downloaded = self.downloads/'saved-original.svg'
        self.cli('download', '.imageview-save', str(downloaded))
        self.assertEqual(downloaded.read_bytes(), Path(self.root, 'sample.svg').read_bytes())


if __name__ == '__main__':
    unittest.main()
