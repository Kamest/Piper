"""Compact approvals keep their facts and real decision controls reachable in the client."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest
import uuid
from unittest import mock

import _env
import common
import features
import piperd
import sessions
import settings


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless approval card checks')
class ApprovalCardBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        settings.update('interface', {'ui_font_size': '13'})
        self.manager.reload_policy()
        root = _env.make_project('approval-layout', {'README.md': 'Synthetic approval fixture\n'})
        _env.register('acme/shop', root)
        self.session = sessions.Session(project='acme/shop', path=root, title='Release checks', state='ready')
        self.manager.sessions[self.session.id] = self.session
        self.session.save()
        # Inline-card checks require these modules regardless of a fresh install's preset.
        features.save({'composer': True, 'http': True, 'db': True}, onboarded=True)
        self.server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', self.manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', self.server.server_port))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # Give each test fresh resources so rapid starts never reuse a closing session/profile.
        self.browser = 'piper-card-' + uuid.uuid4().hex[:12]
        self.browser_env = dict(os.environ, AGENT_BROWSER_PROFILE=str(
            Path(os.environ['TEST_SANDBOX_WORK']) / self.browser))
        self.addCleanup(self.cli, 'close')
        self.answers = []
        self.threads = []
        self.addCleanup(self.finish_requests)
        self.cli('open', piperd.gui_url())
        self.ready('!!document.querySelector("#actbar")')

    def cli(self, *args):
        result = subprocess.run(['agent-browser', '--session', self.browser, '--json', *args],
                                capture_output=True, text=True, timeout=40, env=self.browser_env)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
        value = json.loads(result.stdout)
        self.assertTrue(value['success'], 'agent-browser action failed')
        return value.get('data', {}).get('result')

    def ready(self, expr):
        self.cli('wait', '--fn', expr)

    def shot(self, name):
        path = Path(os.environ['TEST_SANDBOX_WORK'], name + '.png')
        self.cli('screenshot', str(path))
        print('APPROVAL_PNG ' + name + ' ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    def finish_requests(self):
        for item in self.manager.pending_list():
            if item['session'] == self.session.id:
                self.manager.decide(item['id'], False)
        for thread in self.threads:
            thread.join(3)

    def ask(self, *, rows=None, state='ready', suffix='', kind='http_host', extra=None, summary=None, show=True, always=''):
        previous = {item['id'] for item in self.manager.pending_list()}
        requests = [{'method': 'GET', 'url': 'https://example.com/' + path} for path in
                    ('health', 'assets/nativefiles.js', 'assets/references.js', 'api/ping')]
        if rows is None:
            rows = [row for _ in requests for row in (
                {'text': 'example.com', 'chips': ['UNKNOWN HOST']},
                {'text': 'GET · 0 B body · 0 declared headers'})]
        payload = {'project': 'acme/shop', 'file': 'http-client/smoke.http', 'env': '', 'hosts': ['example.com'],
                   'request_line': '\n'.join(r['method'] + ' ' + r['url'] for r in requests),
                   'requests': requests, '_force_approval': True,
                   'evidence': {'title': 'What it sends', 'state': state, 'checked_at': time.time(), 'rows': rows}}
        payload.update(extra or {})
        answer = {}
        thread = threading.Thread(target=lambda: self.answers.append((suffix, self.manager.ask(
            kind, self.session, summary or payload['request_line'], payload, timeout=180, result=answer, always=always))), daemon=True)
        thread.start()
        self.threads.append(thread)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = [p for p in self.manager.pending_list() if p['session'] == self.session.id and p['id'] not in previous]
            if pending:
                item = pending[-1]
                if show:
                    self.ready('document.querySelector("#pending")?.dataset.ids.split(" ")[0].replace(/!$/, "")===%s' % json.dumps(item['id']))
                return item
            time.sleep(.02)
        self.fail('approval was not created')

    def geometry(self):
        return self.cli('eval', '''(()=>{
            const card=document.querySelector('#pending .pending'),pending=document.querySelector('#pending');
            const box=e=>{const r=e.getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom,right:r.right}};
            const controls=[...card.querySelectorAll('.pbtns button')].map(e=>{const r=e.getBoundingClientRect();
                return{text:e.textContent,visible:e.checkVisibility({checkOpacity:true})&&r.top>=0&&r.bottom<=innerHeight&&
                    e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};});
            return{card:box(card),pending:box(pending),main:box(document.querySelector('#main')),controls,
                screenWidth:innerWidth,screenHeight:innerHeight,bodyOverflow:document.documentElement.scrollWidth>innerWidth};
        })()''')

    def visible_toast(self, animation_duration=''):
        # A synthetic toast bypasses toast(); resize schedules the same layout callback.
        # Keep its real entrance animation and wait for visibility/geometry rather than just an inline style.
        self.cli('eval', '''(()=>{
            let box=document.getElementById('toasts');if(!box){box=document.createElement('div');box.id='toasts';document.body.append(box)}
            const note=document.createElement('div');note.className='toast';note.dataset.approvalCardFixture='1';
            note.style.animationDuration=%s;
            note.textContent='Synthetic notification remains above the approval';box.append(note);window.dispatchEvent(new Event('resize'));return true;
        })()''' % json.dumps(animation_duration))
        self.manager.bump()
        self.ready('!!document.querySelector("#toasts")?.style.getPropertyValue("--toast-inline-bottom")')
        self.ready('''(()=>{
            const t=document.querySelector('[data-approval-card-fixture]'),p=document.querySelector('#pending');
            return !t.getAnimations().length&&t.checkVisibility({checkOpacity:true})&&
                t.getBoundingClientRect().bottom<=p.getBoundingClientRect().top-7.5;
        })()''')
        self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))

    def clear_toast(self):
        self.cli('eval', 'document.querySelectorAll("[data-approval-card-fixture]").forEach(e=>e.remove());true')

    def test_mobile_overview_keeps_long_approval_actions_below_facts(self):
        self.cli('set', 'viewport', '390', '844')
        self.ask(extra={'cls': 'prod', 'danger': True, '_force_approval': False}, always='Allow these hosts for this project', show=False)
        self.cli('click', '#actbar [aria-label="Overview"]')
        self.ready('document.querySelector("#queue .qi.confirm")?.checkVisibility()')
        self.shot('mobile-overview-390')
        for width in (320, 390, 430, 1024):
            self.cli('set', 'viewport', str(width), '844')
            layout = self.cli('eval', "(()=>{\n              const row=document.querySelector('#queue .qi.confirm'), title=row.querySelector('.qi-title'),\n                sub=row.querySelector('.qi-sub'), ops=row.querySelector('.qi-ops');\n              const r=row.getBoundingClientRect(),t=title.getBoundingClientRect(),o=ops.getBoundingClientRect();\n              return {text:title.textContent,visible:row.checkVisibility(),height:r.height,titleBottom:t.bottom,opsTop:o.top,\n                overflow:row.scrollWidth>row.clientWidth,\n                controls:[...ops.querySelectorAll('button')].map(b=>{const q=b.getBoundingClientRect();\n                  return q.left>=r.left&&q.right<=r.right&&q.bottom<=r.bottom&&q.top>=t.bottom})};\n            })()")
            self.assertTrue(layout['text'])
            self.assertTrue(layout['visible'])
            self.assertGreater(layout['height'], 0)
            self.assertGreaterEqual(layout['opsTop'], layout['titleBottom'], layout)
            self.assertFalse(layout['overflow'], layout)
            self.assertTrue(all(layout['controls']), layout)
        self.cli('set', 'viewport', '390', '844')
        self.shot('mobile-overview-long-approval')

    def test_git_guard_explains_approvals_off_and_links_to_the_actual_rule(self):
        import guard
        from test_gitrun_exposed import build, HIDDEN

        root = build('approval-reason-' + self.browser)
        self.manager.repo_top(root, record='start')
        self.session.path, self.session.excludes = root, [HIDDEN]
        self.session.coverage = [guard.coverage(root, [HIDDEN], {'candidates': []})]
        self.session.save()
        _env.register('acme/shop', root)
        features.save({'composer': True})
        self.manager.bump()
        self.cli('reload')
        self.cli('set', 'viewport', '1440', '1000')
        self.cli('click', '.qm[data-sid="%s"]' % self.session.id)
        self.cli('press', 'Meta+e' if self.cli('eval', '/Mac/i.test(navigator.platform)') else 'Alt+e')
        self.ready('!document.querySelector(".composer").classList.contains("hidden")')
        self.cli('fill', '.composer textarea', 'Keep this draft while reviewing requests')
        self.cli('mouse', 'move', '380', '10')
        self.enterContext(mock.patch.dict(self.manager.policy, ask=[], danger_cards=False,
                                          security_scoped=False, git_mode='hole'))
        os.rename(os.path.join(root, 'config'), os.path.join(root, 'cfgx'))
        results = []

        def request():
            try:
                self.manager.tool(self.session, 'git_run', {'args': ['show', 'HEAD', '--', 'src/a.txt']})
            except ValueError as error:
                results.append(str(error))
        thread = threading.Thread(target=request, daemon=True)
        self.threads.append(thread)
        thread.start()
        self.ready('document.querySelector("#pending .pwhy")?.textContent.includes("always on")')
        pending = [p for p in self.manager.pending_list() if p['session'] == self.session.id][0]
        self.assertIsNone(pending['why']['rule'])
        for width, height in ((1440, 1000), (390, 800)):
            self.cli('set', 'viewport', str(width), str(height))
            self.assertEqual(self.cli('eval', 'document.querySelectorAll("#pending .pwhy a").length'), 0)
            text = self.cli('eval', 'document.querySelector("#pending").textContent')
            self.assertIn('Security or Git approvals are off', text)
            self.assertIn('Sandbox-hidden path moved', text)
            self.assertNotIn('Full git on the host', text)
            self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))
            self.shot('approval-reason-mandatory-%s-%s' % (width, height))
        self.cli('click', '#pending .pbtns > button:first-child')
        self.ready('!document.querySelector("#pending")')
        thread.join(3)
        self.assertTrue(results and 'declined' in results[0], results)
        self.assertEqual(self.cli('eval', 'document.querySelector(".composer textarea").value'),
                         'Keep this draft while reviewing requests')

        self.manager.policy['danger_cards'] = True
        hidden_item = self.ask(kind='git_run', summary='git add config/secrets', extra={
            'danger': True, 'args': ['add', 'config/secrets'],
            'evidence': {'title': 'What it changes', 'state': 'ready', 'rows': []}})
        self.assertEqual(hidden_item['why']['rule'], 'hidden_files', hidden_item['why'])
        self.ready('document.querySelector("#pending .pwhy a")?.textContent.includes("Hidden files protection")')
        self.assertIn('separate from Git approvals', self.cli('eval', 'document.querySelector("#pending").textContent'))
        self.shot('approval-reason-hidden-390-800')
        self.cli('set', 'viewport', '1440', '1000')
        self.cli('click', '#pending .pwhy a')
        self.ready('!!document.querySelector("[data-sec=hidden_files]")')
        self.assertTrue(self.cli('eval', 'document.querySelector("[data-sec=hidden_files]").checkVisibility({checkOpacity:true})'))
        self.shot('approval-reason-settings-target')

    def test_floating_card_does_not_reserve_a_cut_off_workspace_strip(self):
        self.cli('set', 'viewport', '1440', '1000')
        self.cli('click', '.qm[data-sid="%s"]' % self.session.id)
        self.cli('press', 'Meta+e' if self.cli('eval', '/Mac/i.test(navigator.platform)') else 'Alt+e')
        self.ready('!document.querySelector(".composer").classList.contains("hidden")')
        self.cli('fill', '.composer textarea', 'Keep this draft while reviewing requests')
        self.cli('mouse', 'move', '600', '100')
        before = self.cli('eval', 'document.querySelector("#main > .body").getBoundingClientRect().height')
        requests = [{'method': 'GET', 'url': 'https://example.com/' + p} for p in ('health', 'assets/client.js')]
        summary = '\n'.join(r['method'] + ' ' + r['url'] for r in requests)
        rows = [row for _ in requests for row in ({'text': 'example.com', 'chips': ['UNKNOWN HOST']},
                                                  {'text': 'GET · 0 B body · 0 declared headers'})]
        self.ask(rows=rows, summary=summary, extra={'requests': requests, 'request_line': summary},
                 always='Allow fixture requests')
        self.cli('wait', '--fn', 'document.querySelector("#pending").getBoundingClientRect().bottom<=document.querySelector(".composer").getBoundingClientRect().top-7.5')
        self.assertAlmostEqual(before, self.cli('eval', 'document.querySelector("#main > .body").getBoundingClientRect().height'), delta=1,
                               msg='approval must float without carving a strip out of the terminal workspace')
        self.shot('approval-design-desktop')
        self.cli('set', 'viewport', '390', '800')
        self.shot('approval-design-narrow')
        self.cli('click', '#pending .pbtns > button:first-child')
        self.ready('!document.querySelector("#pending")')
        self.cli('set', 'viewport', '1440', '1000')
        self.ask(rows=[{'text': 'Request fact %s' % i} for i in range(60)])
        self.ready('!!document.querySelector("#pending .pbtns button")')
        self.shot('approval-floating-regression')
        after = self.cli('eval', 'document.querySelector("#main > .body").getBoundingClientRect().height')
        self.assertAlmostEqual(before, after, delta=1,
                               msg='approval must float without carving a strip out of the terminal workspace')
        for width, height in ((1440, 1000), (390, 800), (390, 568), (320, 568)):
            with self.subTest(width=width, height=height):
                if width < 500:
                    settings.update('interface', {'ui_font_size': '16'})
                    self.manager.reload_policy()
                    self.manager.bump()
                    self.ready('getComputedStyle(document.body).fontSize==="16px"')
                self.cli('set', 'viewport', str(width), str(height))
                self.ready('document.querySelector("#pending").getBoundingClientRect().bottom<=document.querySelector(".composer").getBoundingClientRect().top-7.5')
                metrics = self.cli('eval', """(()=>{
                    const card=document.querySelector('#pending .pending'),scroll=card.querySelector('.pscroll');
                    const head=card.querySelector('.phead'),footer=card.querySelector('.pbtns');
                    const controls=[head,...footer.querySelectorAll('button'),document.querySelector('.composer button.primary')];
                    const hit=e=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&
                        e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))};
                    const first=head.getBoundingClientRect().top,last=footer.getBoundingClientRect().bottom;
                    scroll.scrollTop=scroll.scrollHeight;
                    return{controls:controls.map(hit),scrolls:scroll.scrollHeight>scroll.clientHeight,
                        belowHeader:head.getBoundingClientRect().top>=document.querySelector('#main').firstElementChild.getBoundingClientRect().bottom,
                        overflow:document.documentElement.scrollWidth>innerWidth,
                        fixed:head.getBoundingClientRect().top===first&&footer.getBoundingClientRect().bottom===last,
                        draft:document.querySelector('.composer textarea').value};
                })()""")
                self.assertTrue(all(metrics['controls']), metrics)
                self.assertTrue(metrics['scrolls'], metrics)
                self.assertTrue(metrics['fixed'], metrics)
                self.assertTrue(metrics['belowHeader'], metrics)
                self.assertFalse(metrics['overflow'], metrics)
                self.assertEqual(metrics['draft'], 'Keep this draft while reviewing requests')
                self.shot('approval-floating-composer-%s-%s' % (width, height))
        self.cli('click', '#pending .pbtns > button:first-child')
        self.ready('!document.querySelector("#pending")')
        self.assertEqual(self.cli('eval', 'document.querySelector(".composer textarea").value'),
                         'Keep this draft while reviewing requests')

    def test_toast_enter_animation_keeps_approval_controls_reachable(self):
        self.cli('set', 'viewport', '1920', '900')
        self.ask()
        # A longer entrance deterministically exposes checks made before the animation settles.
        self.visible_toast(animation_duration='3s')
        self.shot('approval-toast-settled')

    def test_http_facts_and_decision_controls_remain_compact_at_default_and_large_font(self):
        item = self.ask()
        self.cli('set', 'viewport', '1920', '900')
        self.ready('!!document.querySelector("#pending .pbtns button")')
        self.visible_toast()
        wide = self.geometry()
        self.shot('approval-http-wide')
        print('APPROVAL_GEOMETRY wide ' + json.dumps(wide), flush=True)
        self.assertLess(wide['card']['width'], wide['main']['width'] * .9,
                        'an approval must not stretch across the whole wide workspace')
        self.assertTrue(all(c['visible'] for c in wide['controls']), wide)
        self.clear_toast()
        self.cli('set', 'viewport', '1024', '800')
        self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))
        self.shot('approval-http-desktop-1024')
        text = self.cli('eval', 'document.querySelector("#pending .pending").textContent')
        for value in ('UNKNOWN HOST', 'example.com', 'GET', '0 B body', '0 declared headers', 'Asks because'):
            self.assertIn(value, text)
        settings.update('interface', {'ui_font_size': '16'})
        self.manager.reload_policy()
        self.cli('reload')
        self.cli('set', 'viewport', '390', '800')
        self.ready('!!document.querySelector("#pending .pbtns button")')
        self.ready('getComputedStyle(document.body).fontSize==="16px"')
        self.visible_toast()
        narrow = self.geometry()
        self.shot('approval-http-narrow-large-font')
        print('APPROVAL_GEOMETRY narrow ' + json.dumps(narrow), flush=True)
        self.assertFalse(narrow['bodyOverflow'], narrow)
        self.assertGreaterEqual(narrow['card']['x'], 0, narrow)
        self.assertLessEqual(narrow['card']['right'], 390, narrow)
        self.assertTrue(all(c['visible'] for c in narrow['controls']), narrow)
        self.clear_toast()
        self.cli('focus', '#actbar [aria-label="Overview"]')
        self.assertTrue(self.cli('eval', '!document.activeElement.closest("#pending")'))
        self.cli('press', 'y')
        self.assertTrue(any(p['id'] == item['id'] for p in self.manager.pending_list()), 'terminal/body y cannot approve')
        self.cli('focus', '#pending .pending')
        self.cli('press', 'y')
        self.assertTrue(any(p['id'] == item['id'] for p in self.manager.pending_list()), 'dangerous cards require a deliberate click')
        self.cli('press', 'n')
        self.ready('!document.querySelector("#pending .pending")')
        self.threads[-1].join(3)
        self.assertEqual(self.answers, [('', False)])

    def test_shared_container_keeps_git_diff_db_preview_and_nonready_facts_reachable(self):
        settings.update('interface', {'ui_font_size': '16'})
        self.manager.reload_policy()
        self.manager.bump()
        self.ready('getComputedStyle(document.body).fontSize==="16px"')
        self.cli('set', 'viewport', '390', '800')
        cases = (
            ('git_run', {'args': ['diff', '--cached'], 'evidence': {
                'title': 'What it changes', 'state': 'ready', 'rows': [{'text': 'Branch main · staged changes'}],
                'diff': '+' + 'long-unbroken-fixture-' * 100 + '\n' + '\n'.join('+changed line %s' % i for i in range(40)),
                'inline_diff': True}}, 'Staged changes', ('Branch main', 'changed line 39')),
            ('db_write_test', {'card': {'type': 'db_write', 'cls': 'test', 'project': 'acme/shop',
                'connection': 'fixture', 'lead': 'Write test rows', 'sql': "UPDATE ledger SET note = '" + 'x' * 500 + "'",
                'preview': '3 rows', 'preview_ok': True, 'warnings': ['NO WHERE · all rows'],
                'reason': 'Synthetic approval layout check'}, 'evidence': {
                'title': 'What it changes', 'state': 'ready', 'rows': [{'text': 'fixture', 'chips': ['TEST']}],
                'preview': {'columns': ['wide_column_' + str(i) for i in range(12)],
                            'rows': [[{'v': 'before', 'to': 'after'} for _ in range(12)]]}}},
                'Update test database', ('NO WHERE', 'Synthetic approval layout check', 'before', 'after')),
            ('http_host', {'evidence': {'title': 'What it sends', 'state': 'checking', 'rows': []}},
                'Checking request', ('checking',)),
            ('http_host', {'evidence': {'title': 'What it sends', 'state': 'unavailable',
                'rows': [{'text': 'Could not check: earlier host history unavailable'},
                         {'text': 'Secret variables: apiKey'},
                         {'text': 'Could not check: scripts can change the request, body and headers'}]}},
                'Uncertain request', ('earlier host history unavailable', 'apiKey', 'scripts can change')),
            ('http_host', {'evidence': {'title': 'What it sends', 'state': 'error',
                'rows': [{'text': 'Could not check: fixture collector failed'}]}},
                'Unavailable request facts', ('fixture collector failed',)),
        )
        for kind, extra, summary, expected in cases:
            with self.subTest(kind=kind, summary=summary):
                self.ask(kind=kind, extra=extra, summary=summary, suffix=summary)
                bounds = self.geometry()
                self.assertFalse(bounds['bodyOverflow'], bounds)
                self.assertTrue(all(c['visible'] for c in bounds['controls']), bounds)
                self.assertLessEqual(bounds['pending']['right'], 390, bounds)
                text = self.cli('eval', 'document.querySelector("#pending .pending").textContent')
                for value in expected:
                    self.assertIn(value, text)
                self.cli('eval', '(()=>{const p=document.querySelector("#pending .pscroll");p.scrollTop=p.scrollHeight;return true;})()')
                self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))
                self.shot('approval-common-' + kind + '-' + extra['evidence']['state'])
                self.cli('click', '#pending .pbtns > button:first-child')
                self.ready('!document.querySelector("#pending .pending")')
        self.assertTrue(all(answer is False for _, answer in self.answers))

    def test_more_cards_evidence_refresh_and_actual_allow_payload_keep_the_same_buttons(self):
        self.cli('set', 'viewport', '1024', '800')
        first = self.ask(suffix='denied')
        second = self.ask(kind='git_commit', summary='Commit on branch main (1 file)\nCommit fixture change',
                          rows=[{'text': 'One staged fixture file'}], suffix='allowed', show=False,
                          always='Allow fixture commits')
        self.ready('!!document.querySelector("#pending .pmore")')
        self.cli('click', '#pending .pmore')
        self.ready('document.querySelector("#pending")?.dataset.ids.split(" ")[0]===%s' % json.dumps(second['id']))
        self.cli('click', '#pending .pmore')
        self.ready('document.querySelector("#pending")?.dataset.ids.split(" ")[0]===%s' % json.dumps(first['id'] + '!'))
        self.cli('eval', '''(()=>{
            window.approvalAllow=document.querySelector('#pending .pbtns button.danger');
            window.approvalDecisions=[];const fetch=window.fetch.bind(window);
            window.fetch=(url,opts)=>{if(String(url).includes('/api/pending/'))approvalDecisions.push(JSON.parse(opts.body));return fetch(url,opts)};
            return true;
        })()''')
        with self.manager.lock:
            self.manager.pending[first['id']]['payload']['evidence']['rows'].append({'text': 'Late collector fact'})
        self.manager.bump()
        self.ready('document.querySelector("#pending .pevidence").textContent.includes("Late collector fact")')
        self.assertTrue(self.cli('eval', 'approvalAllow===document.querySelector("#pending .pbtns button.danger")'))
        self.visible_toast()
        self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))
        self.shot('approval-more-cards-and-toast')
        self.clear_toast()
        self.cli('click', '#pending .pbtns > button:first-child')
        self.ready('document.querySelector("#pending")?.dataset.ids.split(" ")[0]===%s' % json.dumps(second['id']))
        settings.update('interface', {'ui_font_size': '16'})
        self.manager.reload_policy()
        self.manager.bump()
        self.ready('getComputedStyle(document.body).fontSize==="16px"')
        self.cli('set', 'viewport', '390', '800')
        self.cli('click', '#pending .pcaret')
        self.ready('!!document.querySelector(".pe-allow-menu")')
        self.assertTrue(self.cli('eval', '(()=>{const r=document.querySelector(".pe-allow-menu").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()'))
        self.shot('approval-allow-menu-narrow')
        self.cli('press', 'Escape')
        self.assertTrue(self.cli('eval', '!!document.activeElement.closest("#pending .pending")'))
        self.assertTrue(all(c['visible'] for c in self.geometry()['controls']))
        self.cli('click', '#pending .psplit > button:first-child')
        self.ready('!document.querySelector("#pending .pending")')
        for thread in self.threads:
            thread.join(3)
        self.assertEqual(dict(self.answers), {'denied': False, 'allowed': True})
        self.assertEqual(self.cli('eval', 'approvalDecisions'), [{'approve': False}, {'approve': True}])
