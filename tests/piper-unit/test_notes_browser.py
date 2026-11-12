"""Notes (module notes) in a real headless browser against the Claude boards: the quick note's title row (02), the
first run (06), the phone's More › Notes (07) and a session's Notes access card with its grant row (08).

Opt-in like the other *_browser tests (PIPER_BROWSER_TESTS=1 and agent-browser); the card is raised through
notes.tool() - the real phase-4 path of a notes_* call, only the MCP hop of piper-mcp is skipped."""
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
import notes
import piperd
import sessions
import settings


@unittest.skipUnless(os.environ.get('PIPER_BROWSER_TESTS') == '1' and shutil.which('agent-browser'),
                     'set PIPER_BROWSER_TESTS=1 for headless Notes checks')
class NotesBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        settings.update('interface', {'ui_font_size': '13'})
        self.manager.reload_policy()
        _env.set_modules(self, notes=True)
        features.save({'notes': True}, onboarded=True)
        self.dir = os.path.join(common.REAL_HOME, 'Documents', 'Piper Notes')   # the default: ~/Documents/Piper Notes
        shutil.rmtree(self.dir, True)
        self.addCleanup(shutil.rmtree, self.dir, True)
        if os.path.exists(notes.CONFIG_FILE):
            os.unlink(notes.CONFIG_FILE)
        self.server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, 'MANAGER', self.manager))
        self.enterContext(mock.patch.object(piperd, 'PORT', self.server.server_port))
        self.enterContext(mock.patch.object(piperd, 'PUBLIC', (*piperd.PUBLIC, 'mobile.css')))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.browser = 'piper-notes-' + uuid.uuid4().hex[:12]
        self.browser_env = dict(os.environ, AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK']) / self.browser))
        self.addCleanup(self.cli, 'close')
        self.addCleanup(self.cancel_cards)

    def cli(self, *args):
        result = subprocess.run(['agent-browser', '--session', self.browser, '--json', *args],
                                capture_output=True, text=True, timeout=40, env=self.browser_env)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token('gui'), '[token]'))
        value = json.loads(result.stdout)
        self.assertTrue(value['success'], 'agent-browser action failed')
        return value.get('data', {}).get('result')

    def ready(self, expr):
        self.cli('wait', '--fn', expr)

    def js(self, expr):
        return self.cli('eval', expr)

    def shot(self, name):
        path = Path(os.environ['TEST_SANDBOX_WORK'], name + '.png')
        self.cli('screenshot', str(path))
        print('NOTES_PNG ' + name + ' ' + base64.b64encode(path.read_bytes()).decode(), flush=True)

    def cancel_cards(self):
        for p in list(self.manager.pending.values()):
            p['_cancelled'] = 'test cleanup'
            p['_ev'].set()

    def open(self, width, height, phone=False):
        self.cli('set', 'media', 'dark')
        self.cli('set', 'viewport', str(width), str(height))
        self.cli('open', piperd.gui_url())
        self.ready('!!document.querySelector("#actbar")')
        if phone:
            self.js('''(async()=>{const link=document.createElement('link');link.rel='stylesheet';link.href='/piper/mobile.css';
              await new Promise((resolve,reject)=>{link.onload=resolve;link.onerror=reject;document.head.append(link)});
              const {installMobileViewport}=await import('/piper/dist/mobileui.js');installMobileViewport();return true})()''')
            self.ready('document.body.classList.contains("phone-shell")')

    def seed(self):
        os.makedirs(self.dir)
        now = time.time()
        files = {'notes.md': ('# Notes\n\n## Thu 12 Nov 2026\n\n- 09:12 Ask about the export format\n'
                              '- 14:32 Capture keeps the stamp\n', now - 60),
                 'order-export-4812.json': ('{"order": 4812, "items": [1, 2, 3]}\n' * 60, now - 3600),
                 'standup.md': ('# Standup\n\n- done: export\n', now - 86400),
                 'orders.http': ('GET https://api.example.com/orders/4812\n', now - 3 * 86400)}
        for name, (text, mtime) in files.items():
            p = os.path.join(self.dir, name)
            with open(p, 'w') as f:
                f.write(text)
            os.utime(p, (mtime, mtime))

    def test_quick_note_title_row_and_first_run(self):
        # board 06: no folder yet - the first run says how a session gets them
        self.open(1280, 800)
        self.js('document.querySelector("#actbar [aria-label=\'Notes\'], #actbar [title^=\'your own notes\']")?.click();true')
        self.ready('!!document.querySelector(".notes .nt-first-card")')
        first = self.js('''(()=>{const c=document.querySelector('.notes .nt-first-card');return {text:c.innerText,
            tiles:[...c.querySelectorAll('.nt-tile')].map(t=>t.querySelector('.fi')?.className||'')}})()''')
        self.assertIn('A session sees them only after you allow it on a card', first['text'])
        self.assertNotIn('never shared', first['text'].lower())
        self.assertIn('Paste becomes a file', first['text'])
        self.assertEqual(len(first['tiles']), 3)
        self.assertTrue(all('fi-' in t for t in first['tiles']), first)
        self.shot('06-first-run')
        # board 02: the notebook icon, "Quick note" and the Line / New file switch on one title row
        self.js('document.querySelector(".head .nt-quick").click();true')
        self.ready('!!document.querySelector(".modal.qn-modal h2.qn-title .qn-mode")')
        row = self.js('''(()=>{const t=document.querySelector('.modal.qn-modal h2.qn-title'),m=t.querySelector('.qn-mode'),
            i=t.querySelector('.fi-notes'),r=t.getBoundingClientRect(),q=m.getBoundingClientRect(),f=i.getBoundingClientRect();
            return {text:t.innerText,segs:[...m.querySelectorAll('button')].map(b=>b.textContent),right:Math.round(r.right-q.right),
              sameRow:Math.abs((q.top+q.bottom)/2-(r.top+r.bottom)/2)<4,iconFirst:f.left<q.left&&f.right<=t.querySelector('.grow').getBoundingClientRect().left,
              inBody:!!document.querySelector('.modal .qn .qn-mode')}})()''')
        self.assertTrue(row['text'].startswith('Quick note'), row)
        self.assertEqual(row['segs'], ['Line in notes.md', 'New file'])
        self.assertTrue(row['sameRow'] and row['iconFirst'], row)
        self.assertLessEqual(row['right'], 2, 'the switch is right-aligned on the title row')
        self.assertFalse(row['inBody'])
        self.js('document.querySelector(".modal .qn-text").value="call the client back about the export";'
                'document.querySelector(".modal .qn-text").dispatchEvent(new Event("input",{bubbles:true}));true')
        self.shot('02-quick-note')

    def test_phone_notes_list_capture_and_file(self):
        # board 07: More › Notes - ‹ Notes · Paste as file, where the folder is, the files, the capture bar
        self.seed()
        self.open(390, 844, phone=True)
        self.js('document.querySelector("#actbar [aria-label=\'More\']").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.js('[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Notes")).click();true')
        self.ready('document.querySelectorAll(".notes.nt-phone .nt-ph-row").length===4')
        page = self.js('''(()=>{const n=document.querySelector('.notes.nt-phone'),b=n.querySelector('.nt-ph-bar'),
            send=n.querySelector('.nt-ph-send').getBoundingClientRect(),cap=n.querySelector('.nt-ph-cap').getBoundingClientRect(),
            tab=document.querySelector('#actbar').getBoundingClientRect(),field=getComputedStyle(n.querySelector('.nt-cap-in'));
            return {bar:b.innerText.trim(),paste:!!b.querySelector('[aria-label="Paste as file"]'),where:n.querySelector('.nt-ph-where').innerText,
              rows:[...n.querySelectorAll('.nt-ph-row')].map(r=>[r.querySelector('.nt-name').textContent,r.querySelector('.nt-ph-sub').textContent]),
              small:[...n.querySelectorAll('.nt-ph-row, .nt-ph-bar button, .nt-ph-send')].filter(x=>x.getBoundingClientRect().height<32).length,
              send:[send.width,send.height],capAboveTabs:cap.bottom<=tab.top+1,font:field.fontSize,
              header:!!document.querySelector('#main .head:not(.hidden) .nt-quick'),reveal:/Reveal|Finder/.test(n.innerText),
              moreOn:document.querySelector('#actbar [aria-label="More"]').classList.contains('on'),
              overflow:document.documentElement.scrollWidth>innerWidth,hint:!!n.querySelector('.nt-cap-hint')?.checkVisibility()}})()''')
        self.assertEqual(page['bar'], 'Notes')
        self.assertTrue(page['paste'])
        self.assertIn('~/Documents/Piper Notes', page['where'])
        self.assertIn('on your Mac', page['where'])
        self.assertEqual(page['rows'][0], ['notes.md', '14:32 Capture keeps the stamp'], 'the main note first, its newest line')
        self.assertIn(['order-export-4812.json', 'JSON · 2.1 KiB'], page['rows'])
        self.assertEqual(page['send'], [44, 44])
        self.assertEqual(page['small'], 0)
        self.assertTrue(page['capAboveTabs'], page)
        self.assertEqual(page['font'], '16px', 'iOS does not zoom into the field')
        self.assertFalse(page['header'] or page['reveal'] or page['overflow'] or page['hint'], page)
        self.assertTrue(page['moreOn'], 'More stays highlighted')
        self.js('const c=document.querySelector(".notes .nt-cap-in");c.value="Phone line from the train";true')
        self.shot('07-phone-notes')
        # Append (the main action) writes the line
        self.js('document.querySelector(".notes .nt-ph-send").click();true')
        self.ready('document.querySelector(".notes .nt-ph-row.main .nt-ph-sub")?.textContent.endsWith("Phone line from the train")')
        with open(os.path.join(self.dir, 'notes.md')) as f:
            self.assertIn('Phone line from the train', f.read())
        self.assertEqual(self.js('document.querySelector(".notes .nt-cap-in").value'), '')
        # a file opens as its own screen; its ⋯ has Save as… and no Reveal; ‹ goes back to the list
        self.js('[...document.querySelectorAll(".notes .nt-ph-row")].find(r=>r.textContent.includes("standup.md")).click();true')
        self.ready('!document.querySelector(".notes .nt-ph-row") && !!document.querySelector(".notes > .files .mdv h1")')
        head = self.js('''(()=>{const h=document.querySelector('.notes .files .fhead'),r=h.getBoundingClientRect();
            return {tabs:!!document.querySelector('.notes .files .ftabs')?.checkVisibility(),copy:[...h.querySelectorAll('button')].some(b=>b.textContent.includes('Copy path')),
              clipped:[...h.querySelectorAll('button, .crumbs')].filter(b=>b.getBoundingClientRect().right>r.right+1).length}})()''')
        self.assertEqual(head, {'tabs': False, 'copy': False, 'clipped': 0}, 'the phone editor: no tab strip, actions in ⋯')
        self.shot('07b-phone-note')
        self.assertEqual(self.js('document.querySelector(".notes .nt-ph-back").getAttribute("aria-label")'), 'Back to Notes')
        # Edit: typing saves in place (autosave)
        self.js('[...document.querySelectorAll(".notes .files .file-md-mode .db-seg")].find(b=>b.textContent.includes("Edit")).click();true')
        self.ready('!!document.querySelector(".notes .files .cm-content")')
        self.js('document.querySelector(".notes .files .cm-content").focus();true')
        self.cli('press', 'Control+End')
        self.cli('keyboard', 'type', '- todo: phone edit')
        deadline = time.monotonic() + 10
        text = ''
        while time.monotonic() < deadline:
            with open(os.path.join(self.dir, 'standup.md')) as f:
                text = f.read()
            if 'todo: phone edit' in text:
                break
            time.sleep(.2)
        self.assertIn('todo: phone edit', text, 'the edit saved itself')
        # ⋯: Save as… (a copy through the browser), no Reveal on the phone
        self.js('''window.__dl=null;const oc=URL.createObjectURL.bind(URL);URL.createObjectURL=(b)=>{b.text().then(t=>{window.__dlText=t});return oc(b)};
            const ck=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){if(this.download){window.__dl=this.download;return}return ck.call(this)};true''')
        self.js('document.querySelector(".notes .files .ib.more").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        labels = self.js('[...document.querySelectorAll(".sheet .sheet-label")].map(x=>x.textContent)')
        self.assertIn('Save as…', labels)
        self.assertFalse([x for x in labels if 'Reveal' in x or 'Finder' in x], labels)
        self.js('[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Save as…")).click();true')
        self.ready('window.__dl==="standup.md" && (window.__dlText||"").includes("todo: phone edit")')
        self.js('document.querySelector(".notes .nt-ph-back").click();true')
        self.ready('document.querySelectorAll(".notes .nt-ph-row").length===4')
        # Paste as file: the clipboard goes to the quick note as a new file; it fits the phone and saves
        self.js('''Object.defineProperty(navigator,'clipboard',{configurable:true,value:{readText:()=>Promise.resolve('{"order": 4813, "items": []}'),writeText:()=>Promise.resolve()}});true''')
        self.js('document.querySelector(".notes .nt-ph-paste").click();true')
        self.ready('!!document.querySelector(".modal.qn-modal h2.qn-title .qn-mode") && document.querySelector(".modal .qn-text")?.value.includes("4813")')
        modal = self.js('''(()=>{const m=document.querySelector('.modal.qn-modal'),r=m.getBoundingClientRect(),t=m.querySelector('h2.qn-title');
            return {hints:!!m.querySelector('.qn-hints')?.checkVisibility(),inside:r.left>=0&&r.right<=innerWidth,title:t.scrollWidth<=t.clientWidth+1,on:m.querySelector('.qn-mode .db-seg.on')?.textContent,
              overflow:document.documentElement.scrollWidth>innerWidth}})()''')
        self.assertEqual(modal, {'hints': False, 'inside': True, 'title': True, 'on': 'New file', 'overflow': False})
        self.shot('07c-phone-paste')
        self.js('[...document.querySelectorAll(".modal .qn-foot button.primary")].find(b=>b.textContent.includes("Save file")).click();true')
        self.ready('!document.querySelector(".modal.qn-modal") && document.querySelectorAll(".notes .nt-ph-row").length===5')
        self.assertTrue([n for n in os.listdir(self.dir) if n.endswith('.json') and '4813' in Path(self.dir, n).read_text()])
        # ‹ on the list goes back to More
        self.js('document.querySelector(".notes .nt-ph-back").click();true')
        self.ready('!!document.querySelector(".sheet[aria-label=\'More\']")')

    def test_phone_first_run(self):
        self.open(390, 844, phone=True)
        self.js('document.querySelector("#actbar [aria-label=\'More\']").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.js('[...document.querySelectorAll(".sheet .sheet-row")].find(b=>b.textContent.includes("Notes")).click();true')
        self.ready('!!document.querySelector(".notes.nt-phone .nt-first-card")')
        card = self.js('''(()=>{const c=document.querySelector('.notes .nt-first-card');return {text:c.innerText,
            overflow:document.documentElement.scrollWidth>innerWidth||c.scrollWidth>c.clientWidth+1,kbd:/⌘/.test(c.innerText),
            small:[...c.querySelectorAll('.nt-first-acts button')].filter(b=>b.getBoundingClientRect().height<44).length}})()''')
        self.assertIn('A session sees them only after you allow it on a card', card['text'])
        self.assertIn('Add a line', card['text'])
        self.assertFalse(card['overflow'] or card['kbd'], card)
        self.assertEqual(card['small'], 0)
        self.shot('06b-phone-first-run')

    def test_phone_without_the_module_has_no_notes(self):
        _env.set_modules(self, notes=False)
        features.save({'notes': False}, onboarded=True)
        self.open(390, 844, phone=True)
        self.js('document.querySelector("#actbar [aria-label=\'More\']").click();true')
        self.ready('!!document.querySelector(".sheet .sheet-row")')
        self.assertFalse(self.js('[...document.querySelectorAll(".sheet .sheet-label")].some(x=>x.textContent==="Notes")'))

    def raise_card(self):
        root = _env.make_project('notes-card', {'README.md': 'Synthetic notes fixture\n'})
        _env.register('acme/shop', root)
        s = sessions.Session(kind='project', project='acme/shop', path=root, title='Order export', state='ready')
        self.manager.sessions[s.id] = s
        s.save()
        os.makedirs(self.dir, exist_ok=True)
        out = []
        def call():
            try:
                out.append(notes.tool(self.manager, s, 'notes_write', {'name': 'order-export-4812.json', 'text': '{"order": 4812}\n'}))
            except ValueError as e:   # the card cancelled at cleanup
                out.append(str(e))
        th = threading.Thread(target=call, daemon=True)
        th.start()
        deadline = time.monotonic() + 5
        while not self.manager.pending_list() and time.monotonic() < deadline:
            time.sleep(.02)
        cards = self.manager.pending_list()
        self.assertEqual([c['kind'] for c in cards], ['notes_access'])
        return s, cards[0], th, out

    def test_notes_access_card_and_grant_row(self):
        # board 08: the card - Notes access, the folder with its rights, why it asks, Deny / Allow for this session
        self.open(1280, 800)
        s, card, th, out = self.raise_card()
        self.ready('document.querySelector("#pending")?.dataset.ids?.split(" ")[0].replace(/!$/, "")===%s' % json.dumps(card['id']))
        text = self.js('document.querySelector("#pending .pending").innerText')
        for want in ('Notes access', '~/Documents/Piper Notes', 'read and write · no delete', 'Wants to write order-export-4812.json.',
                     'until it ends', 'Other sessions still ask', 'Allow for this session'):
            self.assertIn(want, text)
        self.shot('08-notes-card')
        self.js('[...document.querySelectorAll("#pending .pbtns button")].find(b=>b.textContent.includes("Allow for this session")).click();true')
        th.join(5)
        self.assertTrue(out and isinstance(out[0], dict) and out[0]['name'] == 'order-export-4812.json', out)
        self.assertIn(notes.access_scope(self.manager), s.approvals)
        # the session inspector › Approval grants: Notes · read and write · this session · Revoke
        self.ready('!!document.querySelector(%s)' % json.dumps('#queue [data-sid="%s"]' % s.id))
        self.js('document.querySelector(%s).click();true' % json.dumps('#queue [data-sid="%s"]' % s.id))
        self.ready('!!document.querySelector("[aria-label=\'context panel\']")')
        if self.js('document.querySelector("[aria-label=\'context panel\']").getAttribute("aria-pressed")') != 'true':
            self.js('document.querySelector("[aria-label=\'context panel\']").click();true')
        self.ready('[...document.querySelectorAll("#inspector .prop")].some(p=>p.textContent.includes("Notes · read and write"))')
        if self.js('document.querySelector("#inspector .sec[data-k=\\"iso\\"] .sec-tg").getAttribute("aria-expanded")') != 'true':
            self.js('document.querySelector("#inspector .sec[data-k=\\"iso\\"] .sec-tg").click();true')
        row = self.js('''(()=>{const p=[...document.querySelectorAll('#inspector .prop')].find(p=>p.textContent.includes('Notes · read and write'));
            p.scrollIntoView({block:'center'});const h=[...document.querySelectorAll('#inspector .prop-h')].map(x=>x.textContent);
            return {text:[...p.children].map(c=>c.textContent.trim()).join('|'),icon:!!p.querySelector('.prop-l .fi-notes'),heads:h,visible:p.checkVisibility(),clipped:p.querySelector('.prop-l').scrollWidth>p.querySelector('.prop-l').clientWidth+1}})()''')
        self.assertIn('Approval grants', row['heads'])
        self.assertEqual(row['text'], 'Notes · read and write|this session|Revoke')
        self.assertTrue(row['icon'] and row['visible'] and not row['clipped'], row)
        self.shot('08b-grant-row')
        self.js('[...document.querySelectorAll("#inspector .prop")].find(p=>p.textContent.includes("Notes · read and write")).querySelector("button").click();true')
        deadline = time.monotonic() + 5
        while notes.access_scope(self.manager) in s.approvals and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertNotIn(notes.access_scope(self.manager), s.approvals, 'Revoke ends the grant')

    def test_phone_notes_access_card(self):
        self.open(390, 844, phone=True)
        _, card, _, _ = self.raise_card()
        self.ready('document.querySelector("#pending")?.dataset.ids?.split(" ")[0].replace(/!$/, "")===%s' % json.dumps(card['id']))
        sheet = self.js('''(()=>{const p=document.querySelector('#pending'),r=p.getBoundingClientRect();
            return {box:[r.left,r.width,r.bottom],text:p.innerText,small:[...p.querySelectorAll('.pbtns button')].filter(b=>b.getBoundingClientRect().height<44).length}})()''')
        self.assertEqual(sheet['box'], [0, 390, 844])
        self.assertIn('Allow for this session', sheet['text'])
        self.assertEqual(sheet['small'], 0)
        self.shot('08c-phone-notes-card')


if __name__ == '__main__':
    unittest.main()
