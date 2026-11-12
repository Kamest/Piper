"""The rail's Inbox is a pinnable tool in a real headless browser: shown by default, unpinned in All tools without
switching its feature off (its new count then moves to the All tools badge), pinned back at its place.

Opt-in like the other *_browser tests (PIPER_BROWSER_TESTS=1 and agent-browser)."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest
import uuid
from unittest import mock

import _env
import common
import features
import piperd
import settings


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless rail checks')
class RailPinsBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        settings.update('interface', {'ui_font_size': '13'})
        self.manager.reload_policy()
        _env.set_modules(self, gitlab=True)
        features.save({'gitlab': True}, onboarded=True)
        self.server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', self.manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', self.server.server_port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.browser = 'piper-rail-' + uuid.uuid4().hex[:12]
        self.browser_env = dict(os.environ, AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK']) / self.browser))
        self.addCleanup(self.cli, 'close')

    def cli(self, *args):
        result = subprocess.run(['agent-browser', '--session', self.browser, '--json', *args],
                                capture_output=True, text=True, timeout=40, env=self.browser_env)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
        value = json.loads(result.stdout)
        self.assertTrue(value['success'], 'agent-browser action failed')
        return value.get('data', {}).get('result')

    def js(self, expr):
        return self.cli('eval', expr)

    def shot(self, name):
        path = Path(os.environ['TEST_SANDBOX_WORK'], name + '.png')
        self.cli('screenshot', str(path))
        print('RAIL_PNG ' + name + ' ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    RAIL = '''(()=>{const r=document.querySelector('#actbar');const labels=[...r.querySelectorAll('.ab-main [aria-label]')]
        .map(b=>b.getAttribute('aria-label'));const more=r.querySelector('[aria-label="All tools"]');
        return {labels, moreBadge: more.querySelector('.ab-badge')?.textContent||''}})()'''

    def test_inbox_unpins_and_pins_back_without_its_feature(self):
        self.cli('set', 'media', 'dark')
        self.cli('set', 'viewport', '1280', '800')
        self.cli('open', piperd.gui_url())
        self.cli('wait', '--fn', '!!document.querySelector("#actbar [aria-label=\'Inbox\']")')
        before = self.js(self.RAIL)
        self.assertEqual(before['labels'][:3], ['Overview', 'Agent', 'Inbox'], before)
        self.shot('inbox-pinned')
        # All tools › Unpin Inbox
        self.js('document.querySelector("#actbar [aria-label=\'All tools\']").click();true')
        self.cli('wait', '--fn', '!!document.querySelector(".nav-tools")')
        self.assertIn('Inbox', self.js('document.querySelector(".nav-tools").innerText'))
        self.js('[...document.querySelectorAll(".nav-tools [aria-label]")].find(b=>b.getAttribute("aria-label")==="Unpin Inbox").click();true')
        self.cli('wait', '--fn', '!document.querySelector("#actbar .ab-main [aria-label=\'Inbox\']")')
        self.shot('inbox-unpinned-menu')
        self.cli('press', 'Escape')
        self.assertNotIn('Inbox', self.js(self.RAIL)['labels'])
        self.assertTrue(features.on('gitlab'), 'unpinning never switches the feature off')
        stored = self.js('Object.entries(localStorage).find(([k])=>k.endsWith("piper.navigation.pins"))?.[1]')
        self.assertNotIn('inbox', json.loads(stored)['pins'])
        # reload: it stays unpinned; pin it back - the same place under Overview
        self.cli('open', piperd.gui_url())
        self.cli('wait', '--fn', '!!document.querySelector("#actbar [aria-label=\'All tools\']")')
        self.assertNotIn('Inbox', self.js(self.RAIL)['labels'])
        self.js('document.querySelector("#actbar [aria-label=\'All tools\']").click();true')
        self.cli('wait', '--fn', '!!document.querySelector(".nav-tools")')
        self.js('[...document.querySelectorAll(".nav-tools [aria-label]")].find(b=>b.getAttribute("aria-label")==="Pin Inbox").click();true')
        self.cli('wait', '--fn', '!!document.querySelector("#actbar .ab-main [aria-label=\'Inbox\']")')
        self.cli('press', 'Escape')
        self.assertEqual(self.js(self.RAIL)['labels'][:3], ['Overview', 'Agent', 'Inbox'])


if __name__ == '__main__':
    unittest.main()
