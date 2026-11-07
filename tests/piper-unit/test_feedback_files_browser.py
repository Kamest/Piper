"""Real headless browser coverage for the workspace bar, stable names and external Files."""
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
        root = _env.make_project('feedback-files', {'README.md': 'Project notes\n', 'Demo.app/Info.plist': 'Synthetic bundle fixture\n'})
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
        external = Path(os.environ['TEST_SANDBOX_WORK'], 'external-notes.md')
        external.write_text('# Notes\nOutside project text\n')
        external_folder = Path(os.environ['TEST_SANDBOX_WORK'], 'external-documents')
        (external_folder / 'empty').mkdir(parents=True)
        (external_folder / 'nested').mkdir()
        (external_folder / 'temporary').mkdir()
        (external_folder / 'nested' / 'notes.md').write_text('# Nested document\nRead-only folder content\n')
        image_bytes = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6S8AAAAASUVORK5CYII=')
        (external_folder / 'sample.png').write_bytes(image_bytes)
        (external_folder / 'guide.pdf').write_bytes(b'%PDF-1.4\nSynthetic document fixture\n')
        (external_folder / 'archive.zip').write_bytes(b'PK\x03\x04\0Synthetic archive fixture')
        (external_folder / 'large.txt').write_text('x' * ((2 << 20) + 1))
        (external_folder / 'protected').symlink_to(common.PIPER_HOME, target_is_directory=True)
        features.save(onboarded=True)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        for endpoint in ('external', 'external-download'):
            request = urlrequest.Request('http://127.0.0.1:%s/api/fs/%s?%s' % (server.server_port, endpoint, urlparse.urlencode({'path': str(external)})),
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
            cli('press', 'Meta+p')
            ready('!!document.querySelector("#overlay input")')
            cli('fill', '#overlay input', str(external))
            ready('document.querySelector("#overlay")?.textContent.includes("Open path")')
            cli('press', 'Enter')
            ready('!!document.querySelector(".files.external-open .fexternal .cm-editor")')
            self.assertTrue(cli('eval', 'document.querySelector(".fexternal").textContent.includes("not shared with the session")'))
            self.assertEqual(cli('eval', 'document.querySelector(".fexternal .cm-content").getAttribute("aria-readonly")'), 'true')
            before = cli('eval', 'document.querySelector(".fexternal .cm-content").textContent')
            cli('click', '.fexternal .cm-content'); cli('keyboard', 'inserttext', 'This must not edit the file')
            self.assertEqual(cli('eval', 'document.querySelector(".fexternal .cm-content").textContent'), before)
            self.assertEqual(external.read_text(), '# Notes\nOutside project text\n')
            external.write_text('# Notes\nChanged outside Piper\n')
            cli('eval', 'window.dispatchEvent(new Event("focus"));true')
            ready('document.querySelector(".fexternal .cm-content")?.textContent.includes("Changed outside Piper")')
            shot('f09-external-readonly')
            cli('eval', '''document.querySelector('.fexternal .ftab').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));true''')
            ready('document.querySelector(".fexternal .ftab").dataset.nativeDrag==="ready"')
            cli('eval', '''document.querySelector('.fexternal .ftab').dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:new DataTransfer()}));true''')
            self.assertEqual(cli('eval', 'nativeCalls.at(-1)'), {'action': 'drag', 'path': str(external)})
            # Hold a real older HTTP response while a newer focus refresh completes.
            # Releasing it must not roll the read-only preview back to stale content.
            cli('eval', '''(async()=>{const {installTransport}=await import('/piper/dist/transport.js');
                installTransport({fetch:async(path,init)=>{const response=await fetch(path,init);
                  if(path.startsWith('/api/fs/external?')&&window.holdNextExternal){window.holdNextExternal=false;
                    const body=await response.text();return new Promise(resolve=>{window.releaseOldExternal=()=>resolve(new Response(body,{status:response.status,headers:response.headers}))})}
                  return response},socket:url=>new WebSocket(url)});return true})()''')
            external.write_text('# Notes\nOlder response\n')
            cli('eval', 'window.holdNextExternal=true;window.dispatchEvent(new Event("focus"));true')
            ready('typeof window.releaseOldExternal==="function"')
            external.write_text('# Notes\nNewest response\n')
            cli('eval', 'window.dispatchEvent(new Event("focus"));true')
            ready('document.querySelector(".fexternal .cm-content")?.textContent.includes("Newest response")')
            cli('eval', 'window.releaseOldExternal();true')
            self.assertTrue(cli('eval', '''(async()=>{await new Promise(resolve=>setTimeout(resolve,100));return document.querySelector('.fexternal .cm-content').textContent.includes('Newest response')})()'''))
            cli('eval', '''(async()=>{const {installTransport}=await import('/piper/dist/transport.js');installTransport(null);return true})()''')
            self.assertFalse(cli('eval', '!!document.querySelector(".fexternal button[title*=Save]")'))
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
            cli('click', '.fexternal .fclose')
            ready('!document.querySelector(".files.external-open")')
            # Human-only external folder browsing uses the real Files renderer and
            # routes. Browser download remains usable with no native bridge installed.
            cli('eval', 'delete window.__piperNative;true')
            cli('press', 'Meta+p')
            ready('!!document.querySelector("#overlay input")')
            cli('fill', '#overlay input', str(external_folder))
            ready('document.querySelector("#overlay")?.textContent.includes("Open path")')
            cli('press', 'Enter')
            ready('document.querySelector(".fexternal .fexternal-list")?.textContent.includes("sample.png")')
            self.assertTrue(cli('eval', 'document.querySelector(".fexternal [aria-label=\\"Up to parent folder\\"]").disabled'))
            self.assertFalse(cli('eval', 'document.querySelector(".fexternal-list").textContent.includes("protected")'))
            self.assertTrue(cli('eval', '[...document.querySelectorAll(".fexternal .fi")].every(icon=>getComputedStyle(icon).maskImage!=="none")'))
            shot('f09-external-folder')
            def external_click(relative):
                cli('click', '.fexternal [data-external-path=%s]' % json.dumps(str(external_folder / relative)))
            external_click('nested')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("notes.md")')
            self.assertEqual(cli('eval', 'document.activeElement.dataset.externalPath'), str(external_folder / 'nested' / 'notes.md'))
            cli('press', 'Enter')
            ready('document.querySelector(".fexternal .cm-content")?.textContent.includes("Nested document")')
            self.assertEqual(cli('eval', 'document.querySelector(".fexternal .cm-content").getAttribute("aria-readonly")'), 'true')
            shot('f09-external-folder-text')
            cli('press', 'Escape')
            ready('!document.querySelector(".fexternal.has-preview")')
            self.assertEqual(cli('eval', 'document.activeElement.dataset.externalPath'), str(external_folder / 'nested' / 'notes.md'))
            cli('press', 'Alt+ArrowUp')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("sample.png")')
            self.assertEqual(cli('eval', 'document.activeElement.dataset.externalPath'), str(external_folder / 'nested'))
            external_click('empty')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("This folder is empty")')
            shot('f09-external-folder-empty')
            cli('press', 'Alt+ArrowUp')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("sample.png")')
            external_click('temporary')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("This folder is empty")')
            (external_folder / 'temporary').rmdir()
            cli('click', '.fexternal [aria-label="Refresh folder"]')
            ready('document.querySelector(".fexternal-list [role=alert]")?.textContent.includes("no longer exists")')
            shot('f09-external-folder-error')
            (external_folder / 'temporary').mkdir()
            cli('click', '.fexternal-list [role=alert] button:first-of-type')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("This folder is empty")')
            cli('press', 'Alt+ArrowUp')
            ready('document.querySelector(".fexternal-list")?.textContent.includes("sample.png")')
            external_click('sample.png')
            ready('document.querySelector(".fexternal .fimg img")?.naturalWidth===1')
            shot('f09-external-image')
            cli('click', '.fexternal-preview-back.desktop')
            external_click('guide.pdf')
            ready('document.querySelector(".fexternal-fallback")?.textContent.includes("No inline preview")')
            self.assertFalse(cli('eval', 'document.querySelector(".fexternal-fallback").textContent.includes("Open with default app")'))
            cli('eval', '''window.externalDownloads=[];const originalAnchorClick=HTMLAnchorElement.prototype.click;
                HTMLAnchorElement.prototype.click=function(){if(this.download)externalDownloads.push(this.download);return originalAnchorClick.call(this)};true''')
            cli('click', '.fexternal-fallback button.primary')
            ready('window.externalDownloads.includes("guide.pdf")')
            shot('f09-external-pdf-download')
            cli('click', '.fexternal-preview-back.desktop')
            external_click('archive.zip')
            ready('document.querySelector(".fexternal-fallback")?.textContent.includes("archive.zip")')
            reply = cli('eval', '''(async()=>{const {apiFetch,token}=await import('/piper/dist/api.js');
                const response=await apiFetch(%s,{headers:{Authorization:'Bearer '+token()}});
                return {status:response.status,disposition:response.headers.get('Content-Disposition'),bytes:[...new Uint8Array(await response.arrayBuffer())]}})()''' % json.dumps('/api/fs/external-download?path='+str(external_folder / 'archive.zip')+'&root='+str(external_folder)))
            self.assertEqual(reply, {'status': 200, 'disposition': 'attachment', 'bytes': list((external_folder / 'archive.zip').read_bytes())})
            cli('click', '.fexternal-preview-back.desktop')
            external_click('large.txt')
            ready('document.querySelector(".fexternal-fallback")?.textContent.includes("2 MiB text preview limit")')
            self.assertTrue(cli('eval', '!!document.querySelector(".fexternal-fallback button.primary")'))
            cli('click', '.fexternal-preview-back.desktop')
            cli('set', 'viewport', '1040', '800')
            shot('f09-external-folder-1040')
            cli('set', 'viewport', '390', '800')
            geometry = cli('eval', '''(()=>{const head=document.querySelector('.fexternal .fhead'),r=head.getBoundingClientRect(),crumbs=head.querySelector('.crumbs').getBoundingClientRect(),buttons=[...head.querySelectorAll(':scope > button')];
                return {ownRow:crumbs.top>=Math.max(...buttons.map(b=>b.getBoundingClientRect().bottom))-1,
                  buttons:buttons.every(b=>{const x=b.getBoundingClientRect();return x.width>=20&&x.height<=36&&x.left>=r.left&&x.right<=r.right+1&&(b.disabled||b.contains(document.elementFromPoint(x.x+x.width/2,x.y+x.height/2)))}),
                  currentVisible:head.querySelector('[aria-current=location]').getBoundingClientRect().right<=r.right+1}})()''')
            self.assertTrue(all(geometry.values()), geometry)
            shot('f09-external-folder-narrow')
            external_click('guide.pdf')
            ready('document.querySelector(".fexternal-fallback")?.checkVisibility()===true')
            self.assertFalse(cli('eval', 'document.querySelector(".fexternal-list").checkVisibility()'))
            shot('f09-external-pdf-narrow')
            cli('click', '.fexternal-preview-back.narrow')
            ready('document.querySelector(".fexternal-list").checkVisibility()')
            cli('set', 'viewport', '1280', '800')
            # No project or session grant is created by the folder context.
            self.assertEqual(set(manager.sessions), {one.id, two.id})
            cli('click', '.fexternal .fclose')
            ready('!document.querySelector(".files.external-open")')
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
