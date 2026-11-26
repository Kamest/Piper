"""Files keys in the whole GUI (phase 1c): with the focus in the Files editor no key leaves Files - ⌘E is IntelliJ's
recent files there, not the Composer; ⌥W closes the tab and types nothing; ⌘B is no queue strip. Piper classic
(policy files_keymap) gives ⌘E back to Piper and lists its own keys in the Keys dialog. Run with PIPER_BROWSER_TESTS=1."""
import json
import os
import shutil
import subprocess
import threading
import time
import unittest
from unittest import mock

import _env
import common
import features
import piperd
import sessions


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'), 'headless browser opt-in')
class FilesKeysBrowserTest(unittest.TestCase):
    def test_editor_keys_stay_in_files_and_classic_gives_them_back(self):
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        text = 'class Main {\n  int alpha;\n}\n'
        root = _env.make_project('files-keys', {'src/Main.java': text, 'src/Other.java': 'class Other {}\n'})
        subprocess.run(['git', '-C', root, 'init', '-qb', 'main'], check=True, capture_output=True)
        _env.register('acme/shop', root)
        one = sessions.Session(project='acme/shop', path=root, title='Keys check', state='ready')
        manager.sessions[one.id] = one
        one.save()
        features.save({'composer': True}, onboarded=True)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = 'files-keys-%s' % os.getpid()

        def cli(*args):
            result = subprocess.run(['agent-browser', '--session', browser, '--json', *args], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
            obj = json.loads(result.stdout)
            self.assertTrue(obj['success'], str(obj).replace(common.own_token('gui'), '[token]'))
            data = obj.get('data', {})
            return data.get('result', data)

        def ready(expr, timeout=20):
            until = time.monotonic() + timeout
            while time.monotonic() < until:
                if cli('eval', expr) is True:
                    return
                time.sleep(.1)
            self.fail('UI did not settle: ' + expr + '\n' + str(cli('eval', 'document.body.innerText.slice(0,2000)')))

        files_tab = '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files")?.getAttribute("aria-pressed")==="true"'
        editor_text = 'document.querySelector(".files .cm-content")?.textContent'

        def open_main():
            cli('click', '.tnode.file[data-nav="src/Main.java"]')
            ready('document.querySelector(".files .crumbs strong")?.textContent==="Main.java"')
            cli('click', '.files .cm-content .cm-line')

        def shot(name):
            path = os.path.join(os.environ['TEST_SANDBOX_WORK'], name + '.png')
            cli('screenshot', path)
            print('FILES_KEYS_PNG ' + path, flush=True)

        try:
            cli('open', piperd.gui_url())
            cli('set', 'viewport', '1280', '800')
            ready('!!document.querySelector("#queue [data-sid=\\"%s\\"]")' % one.id)
            cli('eval', 'document.querySelector(%s).click(); true' % json.dumps('#queue [data-sid="%s"]' % one.id))
            ready('document.querySelector(".session-label")?.textContent==="Keys check"')
            cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            ready('!!document.querySelector(\'.tnode.dir[data-nav="src"], .tnode.file[data-nav="src/Main.java"]\')')
            if not cli('eval', '!!document.querySelector(\'.tnode.file[data-nav="src/Main.java"]\')'):
                cli('click', '.tnode.dir[data-nav="src"]')
                ready('!!document.querySelector(\'.tnode.file[data-nav="src/Main.java"]\')')
            cli('click', '.tnode.file[data-nav="src/Other.java"]')
            ready('document.querySelector(".files .crumbs strong")?.textContent==="Other.java"')
            open_main()
            strip = cli('eval', 'document.querySelector("#queue").getBoundingClientRect().width')

            # IntelliJ (the default): ⌘E = recent files in Files, the Composer stays shut, the view stays Files
            cli('press', 'Meta+e')
            ready('[...document.querySelectorAll(".modal h2, .modal .mtitle, .modal header")].some(x=>x.textContent.includes("Recent files"))'
                  ' || document.querySelector(".overlay.open")?.textContent.includes("Recent files")===true')
            shot('files-keys-recent')
            self.assertTrue(cli('eval', files_tab))
            self.assertFalse(cli('eval', '!!document.querySelector(".composer.open, .composer:not([hidden]) textarea:focus")'))
            cli('press', 'Escape')
            ready('!document.querySelector(".overlay.open")')
            # ⌘B in the editor: the declaration, never the queue strip
            cli('click', '.files .cm-content .cm-line')
            cli('press', 'Meta+b')
            time.sleep(.4)
            self.assertEqual(cli('eval', 'document.querySelector("#queue").getBoundingClientRect().width'), strip)
            self.assertTrue(cli('eval', files_tab))
            # ⌥W: the tab closes, nothing is typed, the file on disk is untouched
            before = cli('eval', editor_text)
            self.assertNotIn('∑', before)
            cli('click', '.files .cm-content .cm-line')
            cli('press', 'Alt+w')
            ready('!document.querySelector(\'.files .ftab[title^="src/Main.java"]\')')
            self.assertTrue(cli('eval', files_tab))
            with open(os.path.join(root, 'src', 'Main.java')) as f:
                self.assertEqual(f.read(), text)
            self.assertNotIn('∑', cli('eval', editor_text) or '')
            shot('files-keys-closed')

            # Piper classic: ⌘E is Piper's again (the Composer), the Keys dialog shows the classic Files keys
            manager.policy['files_keymap'] = 'classic'
            cli('reload')
            ready('!!document.querySelector("#actbar")')
            if not cli('eval', files_tab):
                cli('eval', '[...document.querySelectorAll("header .tab")].find(x=>x.textContent==="Files").click();true')
            ready('!!document.querySelector(\'.tnode.file[data-nav="src/Main.java"]\')')
            open_main()
            cli('press', 'Meta+e')
            time.sleep(.5)
            self.assertFalse(cli('eval', 'document.querySelector(".overlay.open")?.textContent.includes("Recent files")===true'))
            cli('press', 'Escape')
            ready('!document.querySelector(".overlay.open")')
            # the Keys dialog: ? with the keyboard in the queue (⌘⇧L)
            cli('press', 'Meta+Shift+l')
            cli('press', '?')
            ready('document.querySelector(".ks-cols")?.textContent.includes("outline")===true')
            keys = cli('eval', 'document.querySelector(".ks-cols").textContent')
            self.assertIn('move the line up', keys)
            self.assertNotIn('recent files', keys)
            shot('files-keys-classic-sheet')
        finally:
            subprocess.run(['agent-browser', '--session', browser, 'close'], capture_output=True, timeout=20)


if __name__ == '__main__':
    unittest.main()
