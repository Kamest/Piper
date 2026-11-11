"""Real headless browser coverage for the workspace bar, stable names and Files' External group."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock
from urllib import error as urlerror, parse as urlparse, request as urlrequest

import _env
import common
import features
import piperd
import sessions


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'), 'headless browser opt-in')
class FilesBrowserTest(unittest.TestCase):
    def test_workspaces_identity_native_menu_and_external_file(self):
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        root = _env.make_project('feedback-files', {'README.md': 'Project notes\n', 'Demo.app/Info.plist': 'Synthetic bundle fixture\n', 'docs/index.md': '# Docs\n'})
        _env.register('acme/shop', root)
        foreign_root = _env.make_project('feedback-foreign', {'guide.md': '# Other project\n'})
        # These flows represent Git projects; avoid an unrelated no-repository toast
        # obscuring the context/back controls in the acceptance screenshots.
        for checkout in (root, foreign_root):
            subprocess.run(['git', '-C', checkout, 'init', '-qb', 'main'], check=True, capture_output=True)
        _env.register('demo/docs', foreign_root)
        # Derived worktrees deliberately do not occur in /api/state.projects.
        # Native actions must use fs-list/session authority rather than inventing /README.md.
        subprocess.run(['git', '-C', root, 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', root, 'commit', '-qm', 'Fixture'], check=True, capture_output=True)
        base_root = root
        root = str(Path(base_root, '.claude', 'worktrees', 'native-files'))
        subprocess.run(['git', '-C', base_root, 'worktree', 'add', '--detach', root], check=True, capture_output=True)
        derived = 'acme/shop@native-files'
        one = sessions.Session(project=derived, path=root, title='Checkout fix', state='ready')
        two = sessions.Session(project=derived, path=root, title='Docs review', state='ready')
        for item in (one, two): manager.sessions[item.id] = item; item.save()
        # a folder outside every project (an exchange folder of another tool)
        exchange = Path(os.environ['TEST_SANDBOX_WORK'], 'exchange')
        (exchange / 'results' / 'deep').mkdir(parents=True)
        (exchange / 'empty').mkdir()
        (exchange / 'many').mkdir()
        for i in range(1001): (exchange / 'many' / ('f%04d.txt' % i)).write_text('')
        report = exchange / 'report.md'
        report.write_text('# Report\nOutside project text\n')
        (exchange / 'notes.txt').write_text('Dragged into the project\n')
        (exchange / 'results' / 'a.csv').write_text('a,1\n')
        (exchange / 'results' / 'b.csv').write_text('b,2\n')
        (exchange / 'results' / 'deep' / 'c.txt').write_text('c\n')
        image_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6S8AAAAASUVORK5CYII=')
        (exchange / 'sample.png').write_bytes(image_bytes)
        (exchange / 'guide.pdf').write_bytes(b'%PDF-1.4\nSynthetic document fixture\n')
        (exchange / 'large.txt').write_text('x' * ((2 << 20) + 1))
        (exchange / 'protected').symlink_to(common.PIPER_HOME, target_is_directory=True)
        gone = Path(os.environ['TEST_SANDBOX_WORK'], 'gone-folder')
        gone.mkdir()
        (gone / 'x.txt').write_text('x\n')
        features.save(onboarded=True)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        for endpoint in ('external', 'external-download'):
            request = urlrequest.Request('http://127.0.0.1:%s/api/fs/%s?%s' % (server.server_port, endpoint, urlparse.urlencode({'path': str(report)})),
                                         headers={'Authorization': 'Bearer ' + common.own_token('tty')})
            with self.assertRaises(urlerror.HTTPError) as denied:
                urlrequest.urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            denied.exception.close()
        browser = 'feedback-files-%s' % os.getpid()
        def cli(*args):
            result = subprocess.run(['agent-browser', '--session', browser, '--json', *args], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
            obj = json.loads(result.stdout); self.assertTrue(obj['success'], str(obj).replace(common.own_token('gui'), '[token]'))
            data = obj.get('data', {})
            return data.get('result', data)
        def ready(expr):
            until = time.monotonic() + 20
            while time.monotonic() < until:
                if cli('eval', expr) is True: return
                time.sleep(.1)
            self.fail('UI did not settle: '+expr+'\n'+str(cli('snapshot', '-i'))+'\n'+str(cli('errors')))
        def shot(name):
            path = Path(os.environ['TEST_SANDBOX_WORK'], name+'.png'); cli('screenshot', str(path))
            print('FEEDBACK_PNG '+name+' '+base64.b64encode(path.read_bytes()).decode(), flush=True)
        def session_click(item):
            cli('eval', 'document.querySelector(%s).click(); true' % json.dumps('#queue [data-sid="%s"]' % item.id))
        try:
            cli('open', piperd.gui_url()); cli('set', 'viewport', '1280', '800')
            ready('!!document.querySelector("#winbar .workspace-tab")')
            cli('snapshot', '-i')
            if cli('eval', 'document.querySelector("#queue").getBoundingClientRect().width<100'): cli('click', '#actbar [aria-label="Overview"]')
            session_click(one)
            ready('document.querySelector(".session-label")?.textContent==="Checkout fix"')
            self.assertEqual(cli('eval', 'document.querySelector(".session-project").textContent'), derived)
            self.assertEqual(cli('eval', 'document.querySelector(%s).querySelector(".qi-title").textContent' % json.dumps('#queue [data-sid="%s"]' % one.id)), one.title)
            shot('f07-session-identity')
            cli('click', '#winbar [aria-label="New workspace"]')
            ready('new URL(location.href).searchParams.get("window")==="2"')
            session_click(two)
            ready('document.querySelector(".session-label")?.textContent==="Docs review"')
            cli('eval', 'document.querySelectorAll("#winbar [role=tab]")[0].click();true')
            ready('document.querySelector(".session-label")?.textContent==="Checkout fix"')
            self.assertEqual(cli('eval', 'performance.getEntriesByType("navigation").length'), 1)
            shot('f02-window-workspaces')
            cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            ready('!!document.querySelector(".tnode.file")')
            cli('click', '.tnode.file[data-nav="README.md"]')
            ready('document.querySelector(".files .crumbs strong")?.textContent==="README.md"')
            cli('eval', 'document.querySelectorAll("#winbar [role=tab]")[1].click();true')
            ready('document.querySelector(".session-label")?.textContent==="Docs review"')
            cli('eval', 'document.querySelectorAll("#winbar [role=tab]")[0].click();true')
            ready('document.querySelector(".files .crumbs strong")?.textContent==="README.md" && [...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").getAttribute("aria-pressed")==="true"')
            self.assertEqual(cli('eval', '''(()=>{const row=document.querySelector('.tnode.file[data-nav="README.md"]');row.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));const d=new DataTransfer();row.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:d}));return d.getData('text/plain')})()'''), str(Path(root, 'README.md')))
            cli('eval', 'document.querySelector(".tnode.file").dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:220,clientY:190})); true')
            ready('document.querySelector(".cmenu")?.textContent.includes("Copy absolute path")')
            self.assertFalse(cli('eval', 'document.querySelector(".cmenu").textContent.includes("Reveal in Finder")'))
            cli('press', 'Escape')
            cli('eval', 'window.nativeCalls=[];window.__piperNative={platform:"darwin",file:async(action,path)=>{nativeCalls.push({action,path})},prepareDrag:async path=>{window.preparedNativePath=path;return "opaque-token"},drag:token=>{if(token!=="opaque-token")throw Error("invalid token");nativeCalls.push({action:"drag",path:preparedNativePath})}};document.querySelector(".tnode.file").dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:220,clientY:190})); true')
            ready('document.querySelector(".cmenu")?.textContent.includes("Reveal in Finder")')
            shot('f08-files-native-menu')
            cli('eval', '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("Reveal in Finder")).click();true')
            self.assertEqual(cli('eval', 'nativeCalls[0]'), {'action': 'reveal', 'path': str(Path(root, 'README.md'))})
            cli('eval', '''document.querySelector('.tnode.file[data-nav="README.md"]').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));true''')
            ready("document.querySelector('.tnode.file[data-nav=\"README.md\"]').dataset.nativeDrag===\"ready\"")
            cli('eval', '''document.querySelector('.tnode.file[data-nav="README.md"]').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:new DataTransfer()}));true''')
            self.assertEqual(cli('eval', 'nativeCalls[1]'), {'action': 'drag', 'path': str(Path(root, 'README.md'))})
            cli('eval', '''document.querySelector('.tnode.file[data-nav="README.md"]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,clientX:220,clientY:190}));true''')
            self.assertFalse(cli('eval', 'document.querySelector(".cmenu").textContent.includes("Open with default app")'))
            self.assertEqual(cli('eval', 'nativeCalls.length'), 2)
            cli('eval', 'document.querySelector(%s).dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:220,clientY:170}));true' % json.dumps('.tnode.dir[data-nav="Demo.app"]'))
            ready('document.querySelector(".cmenu")?.textContent.includes("Reveal in Finder")')
            self.assertFalse(cli('eval', 'document.querySelector(".cmenu").textContent.includes("Open folder in Finder")'))
            cli('press', 'Escape')
            # ---- External: a file outside every project opens in the ordinary Files module (design 01-10)
            def until(check, what):
                end = time.monotonic() + 20
                while time.monotonic() < end:
                    if check(): return
                    time.sleep(.1)
                self.fail(what + '\n' + str(cli('errors')))
            def open_path(path):
                cli('press', 'Meta+p')
                ready('!!document.querySelector("#overlay input")')
                cli('fill', '#overlay input', path)
                ready('document.querySelector("#overlay")?.textContent.includes("Open path")')
                cli('press', 'Enter')
            def ext_row(rel):
                return '.fext [data-nav=%s]' % json.dumps(ext_root + '//' + rel)
            def overlay_has(text):
                ready('document.querySelector("#overlay")?.textContent.includes(%s)' % json.dumps(text))
            def overlay_click(label):
                ready('[...document.querySelectorAll("#overlay button")].some(b=>b.textContent===%s)' % json.dumps(label))
                cli('eval', '[...document.querySelectorAll("#overlay button")].find(b=>b.textContent===%s).click();true' % json.dumps(label))
            def pick(name):
                ready('[...document.querySelectorAll("#overlay .pitem .name")].some(b=>b.textContent===%s)' % json.dumps(name))
                cli('eval', '[...document.querySelectorAll("#overlay .pitem")].find(b=>b.querySelector(".name").textContent===%s).click();true' % json.dumps(name))
            def toast_undo(text):
                ready('[...document.querySelectorAll(".ftoast")].some(t=>t.textContent.includes(%s))' % json.dumps(text))
                cli('eval', '[...document.querySelectorAll(".ftoast")].find(t=>t.textContent.includes(%s)).querySelector("button").click();true' % json.dumps(text))
            def menu_at(selector, label=None):
                cli('eval', 'document.querySelector(%s).dispatchEvent(new MouseEvent("contextmenu",{bubbles:true,clientX:200,clientY:420}));true' % json.dumps(selector))
                ready('!!document.querySelector(".cmenu")')
                if label:
                    ready('[...document.querySelectorAll(".cmenu button")].some(b=>b.textContent.includes(%s))' % json.dumps(label))
                    cli('eval', '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes(%s)).click();true' % json.dumps(label))
            def banner_click(label):
                cli('eval', '[...document.querySelectorAll(".fext-banner button")].find(b=>b.textContent===%s).click();true' % json.dumps(label))
            open_path(str(report))
            ready('!!document.querySelector(".fext .fext-root") && !!document.querySelector(".ftab.ext.on") && !!document.querySelector(".fbody .fmd")')
            ext_root = cli('eval', 'document.querySelector(".fext .fext-root").dataset.nav').removesuffix('//')
            self.assertTrue(ext_root.startswith('external:/'), ext_root)
            self.assertEqual(os.path.realpath(ext_root.removeprefix('external:')), os.path.realpath(exchange))
            self.assertEqual(cli('eval', 'document.querySelector(".fext .fext-head").textContent'), 'EXTERNAL · not shared')
            self.assertIn('Outside every project - not shared with the session. Edits save in place.', cli('eval', 'document.querySelector(".fext-banner").textContent'))
            self.assertEqual(cli('eval', 'document.querySelector(".fhead .ext-chip").textContent'), 'EXTERNAL · NOT SHARED')
            self.assertIn('Copy to project…', cli('eval', 'document.querySelector(".fhead").textContent'))
            self.assertEqual(cli('eval', 'document.querySelector(".fbody .fmd h1")?.textContent'), 'Report')
            # nothing of the project's git, search or session for a file outside it
            self.assertFalse(cli('eval', '!!document.querySelector(".fhead .ib.at") || [...document.querySelectorAll(".fhead button")].some(b=>/\\+ Git|\\+ Actions/.test(b.textContent))'))
            self.assertEqual(cli('eval', 'document.querySelector(".ftab.ext.on .tl").textContent'), 'report.md')
            self.assertEqual(cli('eval', 'document.querySelector(%s).classList.contains("active")' % json.dumps(ext_row('report.md'))), True)
            shot('ext-01-file-preview')
            # Edit + save writes the file in place
            cli('eval', '[...document.querySelectorAll(".fhead .file-md-mode button")].find(b=>b.textContent.includes("Edit")).click();true')
            ready('!!document.querySelector(".fbody .cm-editor")')
            self.assertEqual(cli('eval', 'document.querySelector(".fbody .cm-content").getAttribute("aria-readonly")'), 'false')
            cli('click', '.fbody .cm-content'); cli('press', 'Meta+End'); cli('keyboard', 'inserttext', 'Edited in Piper')
            cli('press', 'Meta+s')
            until(lambda: 'Edited in Piper' in report.read_text(), 'the External edit was not saved')
            shot('ext-02-edit-saved')
            # a change on disk under an unsaved edit: the conflict banner, Overwrite asks first
            cli('click', '.fbody .cm-content'); cli('press', 'Meta+End'); cli('keyboard', 'inserttext', ' mine')
            report.write_text('# Report\nChanged outside Piper\n')
            cli('press', 'Meta+s')
            ready('document.querySelector(".fbanners .b-conflict")?.textContent.includes("Reload (drop mine)")')
            shot('ext-03-conflict')
            cli('eval', '[...document.querySelectorAll(".fbanners .b-conflict button")].find(b=>b.textContent==="Overwrite").click();true')
            overlay_has('on disk?')
            shot('ext-02b-overwrite-confirm')
            self.assertIn('Your text replaces the other change, which is lost.', cli('eval', 'document.querySelector("#overlay").textContent'))
            overlay_click('Overwrite')
            until(lambda: report.read_text().rstrip('\n').endswith('Edited in Piper mine'), 'Overwrite did not write')
            ready('!document.querySelector(".fbanners .b-conflict") && !document.querySelector(".ftab.ext.dirty")')
            # Focus refresh: an older held read never wins over a newer one (the newer read is queued behind it)
            cli('eval', '''(async()=>{const {installTransport}=await import('/piper/dist/transport.js');
                installTransport({fetch:async(path,init)=>{const response=await fetch(path,init);
                  if(path.startsWith('/api/fs/read?')&&path.includes('external%3A')&&window.holdNextExternal){window.holdNextExternal=false;
                    const body=await response.text();return new Promise(resolve=>{window.releaseOldExternal=()=>resolve(new Response(body,{status:response.status,headers:response.headers}))})}
                  return response},socket:url=>new WebSocket(url)});return true})()''')
            report.write_text('# Report\nOlder response\n')
            cli('eval', 'window.holdNextExternal=true;window.dispatchEvent(new Event("focus"));true')
            ready('typeof window.releaseOldExternal==="function"')
            report.write_text('# Report\nNewest response\n')
            cli('eval', 'window.dispatchEvent(new Event("focus"));true')
            cli('eval', 'window.releaseOldExternal();true')
            ready('document.querySelector(".fbody .cm-content")?.textContent.includes("Newest response")')
            self.assertTrue(cli('eval', '''(async()=>{await new Promise(resolve=>setTimeout(resolve,300));return document.querySelector('.fbody .cm-content').textContent.includes('Newest response')})()'''))
            cli('eval', '''(async()=>{const {installTransport}=await import('/piper/dist/transport.js');installTransport(null);return true})()''')
            # Copy into the project: no confirmation, a toast with Undo (the copy goes to Piper's trash)
            banner_click('Copy…')
            overlay_has('Copy report.md to…')
            self.assertIn('the copy becomes visible to the session', cli('eval', 'document.querySelector("#overlay").textContent'))
            shot('ext-04-copy-picker')
            pick('(project root)')
            ready('[...document.querySelectorAll(".ftoast")].some(t=>t.textContent.includes("Copied to report.md - now visible to the session"))')
            self.assertEqual(Path(root, 'report.md').read_text(), '# Report\nNewest response\n')
            shot('ext-05-copied')
            toast_undo('Copied to report.md')
            until(lambda: not Path(root, 'report.md').exists(), 'Undo did not remove the copy')
            self.assertTrue(report.exists())
            # the name is taken: Keep both saves "report copy.md"
            for _ in range(2):
                banner_click('Copy…'); pick('(project root)')
                if _ == 0: until(lambda: Path(root, 'report.md').exists(), 'the second copy did not land')
            overlay_has('report.md already exists')
            self.assertIn('Keep both saves the copy as report copy.md', cli('eval', 'document.querySelector("#overlay").textContent'))
            shot('ext-06-destination-conflict')
            overlay_click('Keep both')
            until(lambda: Path(root, 'report copy.md').exists(), 'Keep both did not save a copy')
            # Move asks first; the tab follows the file into the project (no chip), Undo puts it back
            banner_click('Move…'); pick('docs')
            overlay_has('Move report.md into acme/shop@native-files/docs?')
            self.assertIn("The original goes to Piper's trash and the copy becomes visible to the session.", cli('eval', 'document.querySelector("#overlay").textContent'))
            shot('ext-07-move-confirm')
            overlay_click('Move')
            ready('[...document.querySelectorAll(".ftoast")].some(t=>t.textContent.includes("Moved to docs/report.md - original in Piper\'s trash"))')
            until(lambda: not report.exists() and Path(root, 'docs', 'report.md').exists(), 'Move did not move')
            ready('!document.querySelector(".ftab.ext") && !document.querySelector(".fhead .ext-chip") && document.querySelector(".fhead .crumbs strong")?.textContent==="report.md"')
            toast_undo('Moved to docs/report.md')
            until(lambda: report.exists() and not Path(root, 'docs', 'report.md').exists(), 'Undo did not move back')
            ready('!!document.querySelector(".ftab.ext.on")')
            # the context menu of an External row (design 10), with the desktop bridge
            cli('eval', 'window.nativeCalls=[];window.__piperNative={platform:"darwin",file:async(action,path)=>{nativeCalls.push({action,path})},prepareDrag:async()=>"opaque-token",drag:()=>{nativeCalls.push({action:"drag"})}};true')
            menu_at(ext_row('notes.txt'))
            ready('document.querySelector(".cmenu")?.textContent.includes("Reveal in Finder")')
            menu_text = cli('eval', 'document.querySelector(".cmenu").textContent')
            for label in ('Open', 'Copy to project…', 'Move to project…', 'Copy absolute path', 'Remove from External'):
                self.assertIn(label, menu_text)
            self.assertNotIn('Insert @', menu_text); self.assertNotIn('Delete', menu_text)
            cli('eval', 'document.querySelectorAll(".ftoast,.toast").forEach(t=>t.remove());true')
            shot('ext-10-context-menu')
            cli('eval', '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("Reveal in Finder")).click();true')
            self.assertEqual(os.path.realpath(cli('eval', 'nativeCalls[0].path')), os.path.realpath(exchange / 'notes.txt'))
            cli('eval', 'delete window.__piperNative;true')
            # drag External -> a project folder copies (⌥ moves); never text for a prompt
            dragged = cli('eval', '''(()=>{const d=new DataTransfer();const src=document.querySelector(%s);
                src.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:d}));
                const dst=document.querySelector('.tnode.dir[data-nav="docs"]');
                dst.dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:d}));
                dst.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:d}));return d.types})()''' % json.dumps(ext_row('notes.txt')))
            self.assertEqual(dragged, ['application/x-piper-external'])
            until(lambda: Path(root, 'docs', 'notes.txt').exists(), 'the drop did not copy')
            self.assertTrue((exchange / 'notes.txt').exists())
            # a folder: copy without a question, move with the counts
            menu_at(ext_row('results'), 'Copy to project…'); pick('(project root)')
            until(lambda: Path(root, 'results', 'deep', 'c.txt').exists(), 'the folder copy did not land')
            menu_at(ext_row('results'), 'Move to project…'); pick('docs')
            overlay_has('Move results into acme/shop@native-files/docs?')
            overlay_text = cli('eval', 'document.querySelector("#overlay").textContent')
            self.assertIn('3 files · ', overlay_text)
            self.assertIn("The originals go to Piper's trash and the copies become visible to the session.", overlay_text)
            shot('ext-07b-move-folder-confirm')
            overlay_click('Cancel')
            self.assertTrue((exchange / 'results').is_dir())
            menu_at(ext_row('results'), 'Move to project…'); pick('docs'); overlay_click('Move')
            until(lambda: Path(root, 'docs', 'results', 'deep', 'c.txt').exists() and not (exchange / 'results').exists(), 'the folder move did not land')
            toast_undo('Moved to docs/results')
            until(lambda: (exchange / 'results' / 'deep' / 'c.txt').exists() and not Path(root, 'docs', 'results').exists(), 'Undo did not move the folder back')
            # Replace: the existing project file goes to Piper's trash; Undo brings it back
            # (the drop's own "Copied to docs/notes.txt" toast is still up - drop it so Undo hits the Replace's toast)
            cli('eval', 'document.querySelectorAll(".ftoast").forEach(t=>t.remove());true')
            Path(root, 'docs', 'notes.txt').write_text('Project version\n')
            menu_at(ext_row('notes.txt'), 'Copy to project…'); pick('docs')
            overlay_has('docs/notes.txt already exists')
            overlay_click('Replace')
            until(lambda: Path(root, 'docs', 'notes.txt').read_text() == 'Dragged into the project\n', 'Replace did not replace')
            toast_undo('Copied to docs/notes.txt')
            until(lambda: Path(root, 'docs', 'notes.txt').exists() and Path(root, 'docs', 'notes.txt').read_text() == 'Project version\n', 'Undo did not restore the replaced file')
            # ⌘P Open path with a line: the file opens in the editor at that line
            open_path(str(report) + ':2')
            ready('document.querySelector(".ftab.ext.on .tl")?.textContent==="report.md" && !!document.querySelector(".fbody .cm-editor") && document.querySelector(".fstatus")?.textContent.includes("Ln 2,")')
            # binary / too large (design 08), an image, an empty and a truncated folder
            cli('click', ext_row('guide.pdf'))
            ready('document.querySelector(".fext-card")?.textContent.includes("No inline preview for this file type.")')
            self.assertIn('PDF, Office, archive and binary files open outside Piper.', cli('eval', 'document.querySelector(".fext-card").textContent'))
            self.assertTrue(cli('eval', '!!document.querySelector(".fext-banner")'))
            self.assertNotIn('Edits save in place', cli('eval', 'document.querySelector(".fext-banner").textContent'))
            # without the desktop app the card's Reveal in Finder is there, disabled with the hint
            self.assertTrue(cli('eval', '[...document.querySelectorAll(".fext-card button")].find(b=>b.textContent==="Reveal in Finder")?.disabled===true'))
            cli('eval', '''window.externalDownloads=[];const originalAnchorClick=HTMLAnchorElement.prototype.click;
                HTMLAnchorElement.prototype.click=function(){if(this.download)externalDownloads.push(this.download);return originalAnchorClick.call(this)};true''')
            cli('eval', '[...document.querySelectorAll(".fext-card button")].find(b=>b.textContent==="Download").click();true')
            ready('window.externalDownloads.includes("guide.pdf")')
            cli('eval', 'document.querySelectorAll(".ftoast,.toast").forEach(t=>t.remove());true')
            shot('ext-08-binary-card')
            # moving the file of an open card: the card closes (a project file has no External card), Undo puts it back
            banner_click('Move…'); pick('docs'); overlay_click('Move')
            until(lambda: Path(root, 'docs', 'guide.pdf').exists() and not (exchange / 'guide.pdf').exists(), 'the card file move did not land')
            ready('!document.querySelector(".fext-card") && ![...document.querySelectorAll(".ftab")].some(t=>t.textContent.includes("guide.pdf"))')
            self.assertEqual(cli('errors').get('errors', []), [])
            toast_undo('Moved to docs/guide.pdf')
            until(lambda: (exchange / 'guide.pdf').exists() and not Path(root, 'docs', 'guide.pdf').exists(), 'Undo did not move the card file back')
            cli('click', ext_row('large.txt'))
            ready('document.querySelector(".fext-card")?.textContent.includes("Larger than the 2 MiB text preview limit.")')
            cli('click', ext_row('sample.png'))
            ready('document.querySelector(".fbody .fimg img")?.naturalWidth===1')
            cli('click', ext_row('empty'))
            ready('document.querySelector(".fext")?.textContent.includes("empty folder")')
            cli('click', ext_row('many'))
            ready('document.querySelector(".fext")?.textContent.includes("Only the first 1,000 entries are shown. Open a subfolder to continue.")')
            cli('click', ext_row('many'))
            self.assertFalse(cli('eval', 'document.querySelector(".fext").textContent.includes("protected")'))
            # the filter applies to the group too; the scope pills do not
            cli('fill', '.fleft .tfilter', 'notes')
            ready('!!document.querySelector(%s)' % json.dumps(ext_row('notes.txt')) + ' && !document.querySelector(%s)' % json.dumps(ext_row('guide.pdf')))
            cli('fill', '.fleft .tfilter', '')
            ready('!!document.querySelector(%s)' % json.dumps(ext_row('guide.pdf')))
            shot('ext-01b-group')
            # Use only this suite's original sandbox-owned runtime. A case alias on
            # case-insensitive filesystems must not turn protected state into a GUI file.
            runtime = Path(common.PIPER_HOME)
            self.assertTrue(runtime.resolve().is_relative_to(Path(_env.ROOT).resolve()))
            protected_file = runtime / 'case-alias-browser-fixture.txt'
            protected_file.write_text('Synthetic protected runtime fixture\n')
            self.addCleanup(protected_file.unlink, missing_ok=True)
            alias = runtime.with_name(runtime.name.swapcase()) / protected_file.name
            paths = [protected_file]
            if alias.exists() and os.path.samefile(alias, protected_file):
                paths.append(alias)
                print('F09_CASE_ALIAS exercised: real fixture aliases share inode/device', flush=True)
            else:
                print('F09_CASE_ALIAS skipped: fixture filesystem is case-sensitive', flush=True)
            for protected_path in paths:
                for endpoint in ('target', 'external', 'external-download'):
                    reply = cli('eval', '''(async()=>{const {call}=await import('/piper/dist/api.js');
                        try {await call('GET',%s+encodeURIComponent(%s));return {status:200}}
                        catch(error){return {status:error.status,message:error.message}}})()''' %
                        (json.dumps('/api/fs/'+endpoint+'?path='), json.dumps(str(protected_path))))
                    self.assertEqual(reply['status'], 404, (endpoint, reply))
                    self.assertIn("Piper's own state", reply['message'])
                    self.assertNotIn('Synthetic protected runtime fixture', json.dumps(reply))
                reply = cli('eval', '''(async()=>{const {api}=await import('/piper/dist/api.js');
                    try {await api.read(%s,%s,0);return {status:200}}
                    catch(error){return {status:error.status,message:error.message}}})()''' %
                    (json.dumps('external:' + str(protected_path.parent)), json.dumps(protected_path.name)))
                self.assertEqual(reply['status'], 404, reply)
                self.assertNotIn('Synthetic protected runtime fixture', json.dumps(reply))
            # Open path to Piper's own state: refused in the External group, nothing opens
            open_path(str(protected_file))
            ready('document.querySelector(".fext .b-error")?.textContent.includes("Refused: Piper\'s own state")')
            self.assertFalse(cli('eval', '[...document.querySelectorAll(".ftab")].some(t=>t.textContent.includes("case-alias"))'))
            shot('ext-02b-protected-refused')
            cli('click', '.fext .b-error button')
            ready('!document.querySelector(".fext .b-error")')
            # Phone (design 09): 44 px rows, a collapsible group, the banner's actions behind ⋯
            cli('click', ext_row('report.md'))
            ready('document.querySelector(".ftab.ext.on .tl")?.textContent==="report.md"')
            cli('eval', '[...document.querySelectorAll(".fhead .file-md-mode button")].find(b=>b.textContent.includes("Preview")).click();true')
            ready('!!document.querySelector(".fbody .fmd")')
            cli('eval', 'document.querySelectorAll(".ftoast").forEach(t=>t.remove());true')   # the Undo toasts of the steps above
            cli('set', 'viewport', '390', '844')
            ready('!!document.querySelector(".fext-banner .fext-more") || !document.querySelector(".fext-banner")')
            ready('!!document.querySelector(".fext-banner .fext-more")')
            shot('ext-09-phone-file')
            cli('click', '.fext-banner .fext-more')
            ready('document.querySelector(".cmenu")?.textContent.includes("Move to project…")')
            shot('ext-09b-phone-banner-menu')
            cli('press', 'Escape')
            if not cli('eval', '!!document.querySelector(".fext .fext-head[aria-expanded]")?.checkVisibility()'):
                cli('click', '.ftabs .ftree')
            ready('!!document.querySelector(".fext .fext-head[aria-expanded]")?.checkVisibility()')
            self.assertGreaterEqual(cli('eval', 'document.querySelector(%s).getBoundingClientRect().height' % json.dumps(ext_row('notes.txt'))), 44)
            shot('ext-09a-phone-tree')
            cli('eval', 'document.querySelector(".fext .fext-head").click();true')
            ready('document.querySelector(".fext .fext-head").getAttribute("aria-expanded")==="false" && !document.querySelector(%s)' % json.dumps(ext_row('notes.txt')))
            cli('eval', 'document.querySelector(".fext .fext-head").click();true')
            ready('!!document.querySelector(%s)' % json.dumps(ext_row('notes.txt')))
            cli('set', 'viewport', '1280', '800')
            # A second root; a reload restores the roots and the External tab; a folder gone meanwhile is struck, with Remove
            open_path(str(gone))
            ready('document.querySelectorAll(".fext .fext-root").length===2')
            cli('reload')
            ready('!!document.querySelector("#winbar .workspace-tab")')
            cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            ready('document.querySelectorAll(".fext .fext-root").length===2 && !!document.querySelector(".ftab.ext")')
            shutil.rmtree(gone)
            cli('reload')
            ready('!!document.querySelector("#winbar .workspace-tab")')
            cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            ready('document.querySelector(".fext .fext-rootrow.missing")?.nextElementSibling?.textContent==="no longer exists"')
            shot('ext-07c-missing-root')
            cli('eval', '[...document.querySelectorAll(".fext .fext-rootrow.missing button")].find(b=>b.textContent==="Remove").click();true')
            ready('!document.querySelector(".fext .fext-rootrow.missing") && document.querySelectorAll(".fext .fext-root").length===1')
            # × of the root: its tabs close, the empty group disappears, nothing on disk changes
            cli('click', '.fext .fext-x')
            ready('!document.querySelector(".fext") && !document.querySelector(".ftab.ext")')
            self.assertTrue(report.exists() and (exchange / 'results').is_dir())
            # No project or session grant is created by the External group.
            self.assertEqual(set(manager.sessions), {one.id, two.id})
            cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            cli('press', 'Meta+p')
            ready('!!document.querySelector("#overlay input")')
            cli('fill', '#overlay input', str(Path(foreign_root, 'guide.md')))
            ready('document.querySelector("#overlay")?.textContent.includes("Open path")')
            cli('press', 'Enter')
            ready('document.querySelector(".file-project")?.textContent==="demo/docs"')
            self.assertIn('Checkout fix', cli('eval', 'document.querySelector(".file-return").textContent'))
            self.assertFalse(cli('eval', '!!document.querySelector(".fhead .ib.at")'))
            shot('f09-other-project-context')
            cli('click', '.file-return')
            ready('document.querySelector(".session-label")?.textContent==="Checkout fix" && [...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Terminal").getAttribute("aria-pressed")==="true"')
            self.assertEqual(manager.sessions[one.id].state, 'ready')
            # Exercise the real workspace CSS with deliberately long captions. The short view label
            # must retain its width; a long project gets an ellipsis while the title remains readable.
            clip = Path(__file__).parents[1].joinpath('piper-e2e/scans/clip.js').read_text()
            for width in (1024, 390):
                cli('set', 'viewport', str(width), '800')
                for subtitle in ('files', 'very-long-project-name-' * 8):
                    got = cli('eval', """(()=>{const tab=document.querySelector('#winbar .workspace-tab.on'),
                      title=tab.querySelector('.workspace-title'),sub=tab.querySelector('.workspace-sub'),
                      oldTitle=title.textContent,oldSub=sub.textContent;
                      try {title.textContent='Long session title '.repeat(15);sub.textContent=%s;
                        const scan=%s;return {clip:scan.split('\\n').filter(x=>/workspace-(title|sub)/.test(x)),
                          title:title.getBoundingClientRect().width,sub:sub.getBoundingClientRect().width,
                          hidden:getComputedStyle(sub).display==='none'};
                      } finally {title.textContent=oldTitle;sub.textContent=oldSub}})()""" % (json.dumps(subtitle), clip))
                    self.assertEqual(got['clip'], [], (width, subtitle, got))
                    self.assertGreaterEqual(got['title'], 8, (width, subtitle, got))
                    if width == 1024: self.assertGreaterEqual(got['sub'], 8, got)
                    else: self.assertTrue(got['hidden'], got)
            cli('set', 'viewport', '1280', '800')
            renamed = 'Checkout fix with a deliberately long descriptive session name and saved manual identity'
            cli('dblclick', '.session-identity')
            ready('!!document.querySelector("#overlay.open input")')
            cli('fill', '#overlay.open input', renamed)
            cli('press', 'Enter')
            ready('document.querySelector(".session-label")?.textContent===%s' % json.dumps(renamed))
            self.assertEqual(manager.sessions[one.id].title_source, 'manual')
            cli('reload')
            ready('document.querySelector(".session-label")?.textContent===%s' % json.dumps(renamed))
            self.assertEqual(cli('eval', 'document.querySelector(%s).querySelector(".qi-title").textContent' % json.dumps('#queue [data-sid="%s"]' % one.id)), renamed)
            self.assertEqual(cli('eval', 'document.querySelector(".session-project").textContent'), derived)
            shot('f07-renamed-reloaded')
        finally: cli('close')
