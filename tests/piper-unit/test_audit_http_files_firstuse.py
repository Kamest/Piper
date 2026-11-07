"""First-use HTTP/Files regression through the production UI and isolated HTTP handler."""
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

import _env
import common
import features
import piperd
import sessions
import settings


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless first-use regression')
class AuditHttpFilesFirstUseTest(unittest.TestCase):
    def test_empty_files_discoverable_http_required_host_and_env_schema_draft(self):
        manager = _env.manager(self, background=True)
        root = Path(_env.make_project('empty-http', {}))
        root.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
        other_root = Path(_env.make_project('no-env', {'requests.http': 'GET {{host}}/\n'}))
        key, other_key = 'acme/empty-http', 'acme/no-env'
        _env.register(key, str(root))
        _env.register(other_key, str(other_root))
        first = sessions.Session(project=key, path=str(root), title='Empty project', state='waiting', runtime='direct')
        second = sessions.Session(project=other_key, path=str(other_root), title='No environment', state='waiting', runtime='direct')
        manager.sessions = {s.id: s for s in (first, second)}
        features.save(onboarded=True)
        settings.update('interface', {'restore_view': True})
        os.environ['PIPER_IJHTTP'] = str(Path(__file__).with_name('fake-ijhttp.py'))
        self.addCleanup(os.environ.pop, 'PIPER_IJHTTP', None)
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        browser = 'piper-http-firstuse-' + str(os.getpid())
        checks = []

        def cli(*args):
            r = subprocess.run(['agent-browser', '--session', browser, '--json', *args], capture_output=True, text=True, timeout=35)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token('gui'), '[token]'))
            data = json.loads(r.stdout)
            self.assertTrue(data['success'])
            return data.get('data', {}).get('result')

        def ready(code):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if cli('eval', code) is True:
                    return
                time.sleep(.1)
            self.fail(code + '\n' + str(cli('eval', 'document.body.innerText')))

        def click(label):
            cli('find', 'role', 'button', 'click', '--name', label, '--exact')

        def tab(label):
            cli('eval', "Array.from(document.querySelectorAll('nav.tabs button')).find(b=>b.textContent===%s).click();true" % json.dumps(label))

        def shot(name):
            path = Path(os.environ['TEST_SANDBOX_WORK'], 'http-firstuse-' + name + '.png')
            cli('eval', "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
            cli('screenshot', str(path))
            if os.environ.get('PIPER_E2E_EXPORT_PNG') == '1':
                print('SCREENSHOT ' + json.dumps({'name': path.name, 'data': base64.b64encode(path.read_bytes()).decode()}), flush=True)

        def activate(s, view):
            cli('eval', "localStorage.setItem('piper.last',JSON.stringify({active:%s,view:%s}));true" % (json.dumps(s.id), json.dumps(view)))
            cli('reload')
            ready("!!document.querySelector('nav.tabs')")

        with mock.patch.object(piperd, 'MANAGER', manager), mock.patch.object(piperd, 'PORT', server.server_port), mock.patch.object(manager, 'http_gui_run', wraps=manager.http_gui_run) as run:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli('open', piperd.gui_url())
                cli('set', 'viewport', '1280', '633')
                ready("!!document.querySelector('.beside')")
                activate(first, 'files')
                ready("document.querySelector('.fe-hub')?.textContent.includes('No files yet')===true")
                self.assertEqual(cli('eval', "Array.from(document.querySelectorAll('.fe-hub button')).filter(b=>b.textContent==='New file').length"), 1)
                shot('empty-files')
                cli('eval', "Array.from(document.querySelectorAll('.fe-hub button')).find(b=>b.textContent==='New file').click();true")
                ready("!!document.querySelector('.tedit')")
                cli('fill', '.tedit', 'cancelled.txt')
                cli('press', 'Escape')
                self.assertFalse((root / 'cancelled.txt').exists())
                checks.append('A04-F02 visible New file uses ordinary root create; Cancel writes nothing')
                cli('eval', "Array.from(document.querySelectorAll('.fe-hub button')).find(b=>b.textContent==='New file').click();true")
                cli('fill', '.tedit', 'first.txt')
                cli('press', 'Enter')
                ready("!!document.querySelector('.ftab')")
                self.assertTrue((root / 'first.txt').is_file())
                checks.append('A04-F02 first ordinary file persists in checkout, no scratch conversion')
                cli('set', 'viewport', '1440', '1000')
                ready("Array.from(document.querySelectorAll('nav.tabs button')).some(b=>b.textContent==='HTTP')")
                tab('HTTP')
                ready("!!document.querySelector('.hp-empty')")
                click('All tools')
                ready("document.querySelector('.nav-tools')?.textContent.includes('No requests')===true")
                shot('http-discovery')
                cli('press', 'Escape')
                tab('HTTP')
                ready("!!document.querySelector('.hp-empty')")
                checks.append('T01 actual session HTTP tab and All tools expose empty project before creating anything')
                click('New request file…')
                ready("!!document.querySelector('.hed-host-required')")
                self.assertEqual(json.loads((root / 'development/http-client.env.json').read_text()), {'dev': {'host': ''}})
                self.assertEqual(run.call_count, 0)
                self.assertTrue(cli('eval', "document.querySelector('.httprun').disabled && Array.from(document.querySelectorAll('.hrun')).every(b=>b.disabled)"))
                shot('required-host')
                cli('eval', "document.querySelector('.hed-host-required button').click();true")
                ready("!!document.querySelector('[data-http-env-editor]')")
                cli('fill', '[data-http-env-editor]', '{"dev":{"host":"https://api.example.com"}}')
                click('Save')
                ready("document.querySelector('.httprun')?.disabled===false")
                self.assertEqual(run.call_count, 0)
                checks.append('T02 new file immediately guides host; explicit host Save enables Run without implicit request')
                activate(second, 'http')
                ready("!!document.querySelector('.hplist .hpfile')")
                # The full HTTP screen picks one request file on a normal row click.
                cli('eval', "document.querySelector('.hplist .hpfile').click();true")
                ready("!!document.querySelector('.httpcfg')")
                click('Environment')
                ready("!!document.querySelector('[data-http-env-editor]')")
                template = json.loads(cli('eval', "document.querySelector('[data-http-env-editor]').value"))
                self.assertEqual(template, {'dev': {'host': 'http://localhost:8080'}})
                self.assertFalse((other_root / 'http-client.env.json').exists())
                cli('set', 'viewport', '1024', '768')
                shot('environment-schema-draft')
                click('Cancel')
                self.assertFalse((other_root / 'http-client.env.json').exists())
                checks.append('T04 zero-env editor provides concrete schema and public/private hint; Cancel creates nothing')
                click('Environment')
                ready("!!document.querySelector('[data-http-env-editor]')")
                cli('fill', '[data-http-env-editor]', '{"host":"invalid shape"}')
                click('Save')
                ready("document.querySelector('.httpenvdlg [role=alert]')?.textContent.includes('Each environment')===true")
                self.assertFalse((other_root / 'http-client.env.json').exists())
                cli('fill', '[data-http-env-editor]', '{"dev":{"host":"https://api.example.com"}}')
                click('Save')
                ready("document.querySelector('.httpenv')?.value==='dev'")
                self.assertEqual(json.loads((other_root / 'http-client.env.json').read_text()), {'dev': {'host': 'https://api.example.com'}})
                self.assertEqual(run.call_count, 0)
                checks.append('T04 validation retains invalid draft; explicit valid Save creates selectable dev, no request')
                print('HTTP_FIRSTUSE_RESULT ' + json.dumps({'checks': checks}), flush=True)
            finally:
                subprocess.run(['agent-browser', '--session', browser, 'close'], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()
