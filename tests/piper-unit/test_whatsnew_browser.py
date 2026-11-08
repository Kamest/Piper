"""The migration news scrolls independently and stays dismissed in the real client/API."""
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
import security
import settings


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless migration news checks')
class WhatsNewBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        self.enterContext(mock.patch.object(features, 'home_since', return_value='2026-09-24'))
        self.server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', self.manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', self.server.server_port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.browser = 'piper-whats-new-%s' % os.getpid()
        self.config = Path(os.environ['TEST_SANDBOX_WORK'], 'whats-new-browser.json')
        self.config.write_text(json.dumps({'headed': False, 'autoConnect': False}))
        self.addCleanup(self.cli, 'close')

    def cli(self, *args):
        result = subprocess.run(['agent-browser', '--config', str(self.config), '--session', self.browser,
                                 '--json', *args], capture_output=True, text=True, timeout=40,
                                env=dict(os.environ, AGENT_BROWSER_HEADED='0'))
        self.assertEqual(result.returncode, 0,
                         (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
        value = json.loads(result.stdout)
        self.assertTrue(value['success'], 'agent-browser action failed')
        return value.get('data', {}).get('result')

    def ready(self, expr):
        self.cli('wait', '--fn', expr)

    def open_notice(self, theme='dark', size='13'):
        # gui_url uses a token fragment: opening that same URL again need not reload the document.
        self.cli('open', 'about:blank')
        settings.update('interface', {'theme': theme, 'ui_font_size': size})
        self.manager.reload_policy()
        common.write_json(features.FILE, {'base': 'basic', 'onboarded': True, 'modules': {},
                                         'v': features.MIGRATION, 'seen': [security.NOTICE]})
        features._CACHE.clear()
        self.cli('open', piperd.gui_url())
        self.ready('!!document.querySelector(".wn")')
        self.ready('document.querySelector(".wn").closest(".modal").getAnimations().every(a=>a.playState==="finished")')
        self.ready('getComputedStyle(document.body).fontSize===%s' % json.dumps(size + 'px'))
        self.ready('document.querySelectorAll(".wn-card").length===8')

    def shot(self, name):
        if os.environ.get('PIPER_BROWSER_SCREENSHOTS') == '1':
            path = Path(os.environ['TEST_SANDBOX_WORK'], name + '.png')
            self.cli('screenshot', str(path))
            print('WHATSNEW_PNG ' + name + ' ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    def geometry(self):
        return self.cli('eval', '''(()=>{
            const wn=document.querySelector('.wn'),body=wn.querySelector('.wn-body'),modal=wn.closest('.modal');
            const box=e=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right}};
            const button=wn.querySelector('[data-wn=close]'),r=button.getBoundingClientRect();
            return {hero:box(wn.querySelector('.wn-hero')),foot:box(wn.querySelector('.wn-foot')),body:box(body),
                modal:box(modal),scroll:body.scrollTop,scrollable:body.scrollHeight>body.clientHeight,
                overflow:document.documentElement.scrollWidth>innerWidth||body.scrollWidth>body.clientWidth,
                outerScroll:modal.scrollTop,
                closeReachable:r.top>=0&&r.bottom<=innerHeight&&button.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)),
                tags:[...wn.querySelectorAll('.wn-tag')].map(e=>e.scrollWidth<=e.clientWidth),
                columns:getComputedStyle(wn.querySelector('.wn-grid')).gridTemplateColumns.split(' ').length};
        })()''')

    def test_layout_themes_scrolling_and_dismissal(self):
        for theme in ('dark', 'light', 'indigo'):
            for width, height, size in ((1024, 900, '13'), (1024, 900, '16'), (390, 844, '13'), (390, 844, '16')):
                with self.subTest(theme=theme, width=width, size=size):
                    self.cli('set', 'viewport', str(width), str(height))
                    self.open_notice(theme, size)
                    before = self.geometry()
                    self.assertFalse(before['overflow'], before)
                    self.assertTrue(before['closeReachable'], before)
                    self.assertTrue(before['scrollable'], before)
                    self.assertTrue(all(before['tags']), before)
                    self.assertEqual(before['columns'], 2 if width == 1024 else 1)
                    self.assertEqual(self.cli('eval', 'document.querySelectorAll(".wn-more-row").length'), 8)
                    self.assertIn('Workspaces', self.cli('eval', 'document.querySelector(".wn-grid").textContent'))
                    self.assertEqual(self.cli('eval', '[...document.querySelectorAll(".wn-tag")].map(e=>e.textContent)'),
                                     ['BETA', 'EXPERIMENTAL'])
                    name = 'whats-new-%s-%s-%s' % (theme, width, size)
                    self.shot(name)
                    self.cli('focus', '.wn-body')
                    self.cli('press', 'End')
                    self.ready('document.querySelector(".wn-body").scrollTop>0')
                    self.cli('eval', '(()=>{const b=document.querySelector(".wn-body");b.scrollTop=b.scrollHeight;return true})()')
                    after = self.geometry()
                    self.assertEqual(before['hero'], after['hero'])
                    self.assertEqual(before['foot'], after['foot'])
                    self.assertEqual(after['outerScroll'], 0)
                    self.assertTrue(after['closeReachable'], after)
                    self.assertTrue(self.cli('eval', '''(()=>{
                        const r=document.querySelector('.wn-more-row:last-child').getBoundingClientRect();
                        return r.bottom<=document.querySelector('.wn-body').getBoundingClientRect().bottom;
                    })()'''))
                    self.shot(name + '-scrolled')
                    if (theme, width, size) == ('dark', 1024, '13'):
                        self.cli('tab', 'new', '--label', 'seen-check', piperd.gui_url())
                        self.ready('!!document.querySelector("#actbar")')
                        self.assertFalse(self.cli('eval', '!!document.querySelector(".wn")'))
                        self.cli('tab', 'close', 'seen-check')
                        self.ready('!!document.querySelector(".wn")')
                    self.cli('press', 'Escape') if size == '16' else self.cli('click', '[data-wn=close]')
                    self.ready('!document.querySelector(".wn")')
                    self.assertIn(features.WHATS_NEW, features.load()['seen'])
                    self.cli('reload')
                    self.ready('!!document.querySelector("#actbar")')
                    self.assertIsNone(piperd._whats_new_notice())
                    self.assertFalse(self.cli('eval', '!!document.querySelector(".wn")'))


if __name__ == '__main__':
    unittest.main()
