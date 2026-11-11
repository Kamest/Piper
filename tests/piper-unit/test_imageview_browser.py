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
        self.cli('eval', '[...document.querySelectorAll("header .tab")].find(b=>b.textContent===%s).click();true' % json.dumps(name))

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

    def test_folder_link_on_a_phone_opens_the_files_tab_not_a_side_pane(self):
        # A folder named in the terminal opened Files beside the terminal: two 154 px panes, neither usable.
        designs = Path(self.root, 'designs'); designs.mkdir()
        (designs / 'one.png').write_bytes(Path(self.root, 'pixel.png').read_bytes())
        self.open_path(str(designs), image=False)
        self.ready('document.querySelector("header .tab.on")?.textContent==="Files"')
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
        self.cli('click', '.tr-turns-toggle')
        self.ready('document.querySelector(".tr-all").getClientRects().length>0')
        self.screenshot('review-tab-phone-turns')
        self.cli('click', '.tr-all')
        self.ready('!document.querySelector(".turn-review").classList.contains("turns-open")')

    def test_git_review_on_a_phone_shows_one_version_full_width(self):
        # 390 px: Original and Working side by side were 44 and 80 px wide
        self.phone_changes()
        self.cli('eval', '[...document.querySelectorAll("button")].find(b=>b.title==="show the context panel (⌘I)").click();true')
        self.ready('!!document.querySelector("[data-git-path]")')
        self.cli('eval', 'document.querySelector("[data-git-path]").click();true')
        self.ready('document.querySelectorAll(".review-merge .cm-editor").length===2')
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
