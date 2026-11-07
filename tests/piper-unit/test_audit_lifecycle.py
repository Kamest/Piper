"""Headless regressions for factual lifecycle labels and existing grid controls."""
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


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for headless lifecycle regressions")
class AuditLifecycleTest(unittest.TestCase):
    def test_grid_live_states_first_remove_direct_status_key_limit_and_consent(self):
        manager = _env.manager(self, background=True)
        root = _env.make_project("lifecycle", {"README.md": "Synthetic lifecycle fixture\n"})
        key = "acme/lifecycle"
        _env.register(key, root)
        first, second = [sessions.Session(project=key, path=root, title=title, state="waiting", runtime="direct")
                         for title in ("First session", "Second session")]
        manager.sessions = {s.id: s for s in (first, second)}
        features.save(onboarded=True)
        settings.update("interface", {"restore_view": True})
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "piper-lifecycle-%s" % os.getpid()
        checks = []

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if cli("eval", code) is True:
                    return
                time.sleep(0.1)
            self.fail(code)

        def shot(name):
            path = Path(os.environ["TEST_SANDBOX_WORK"], "lifecycle-" + name + ".png")
            cli("eval", "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
            cli("screenshot", str(path))
            if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1440", "1000")
                ready('!!document.querySelector(".beside")')
                cli("eval", "localStorage.setItem('piper.grid',%s);localStorage.setItem('piper.layout',JSON.stringify({v:1,left:'strip',leftBack:'strip',right:'hidden',center:{kind:'grid'}}));localStorage.setItem('piper.last',JSON.stringify({active:%s,view:'term'}));true" % (json.dumps(json.dumps([first.id, second.id])), json.dumps(first.id)))
                cli("reload")
                ready('document.querySelectorAll(".gcell.sessionpane").length===2')
                cli("click", ".beside")
                cli("eval", "Array.from(document.querySelectorAll('.besidemenu button')).find(b=>b.textContent==='Choose panes…').click();true")
                ready('!!document.querySelector(".gridchooser")')
                row = '.gridchooser input[data-id="' + second.id + '"]'
                self.assertTrue(cli("eval", "document.querySelector(%s).checked" % json.dumps(row)))
                second.state, second.rc = "exited", 0
                ready("document.querySelector(%s)?.closest('label').textContent.includes('ended')===true" % json.dumps(row))
                self.assertNotIn("ready", cli("eval", "document.querySelector(%s).closest('label').textContent" % json.dumps(row)))
                checks.append("A03-005 open chooser updates selected ended session before Show")
                shot("chooser-ended")
                cli("eval", "Array.from(document.querySelectorAll('.olayer button')).find(b=>b.textContent==='Show').click();true")
                target = '.gcell[data-sid="' + second.id + '"] [data-act="remove"]'
                ready("!!document.querySelector(%s)" % json.dumps(target))
                cli("click", target)
                ready("JSON.parse(localStorage.getItem('piper.grid')).length===1")
                self.assertEqual(cli("eval", "JSON.parse(localStorage.getItem('piper.grid'))"), [first.id])
                checks.append("A03-004 first pointer Remove removes only the inactive ended pane")
                shot("first-remove")
                first.state, first.rc = "exited", 0
                ready("Array.from(document.querySelectorAll('.endbar')).some(el=>el.offsetParent!==null&&el.textContent.includes('process ended'))")
                self.assertNotIn("sandbox", cli("eval", "Array.from(document.querySelectorAll('.endbar')).find(el=>el.offsetParent!==null).textContent"))
                checks.append("A01-006 Direct endbar describes the native process")
                shot("direct-ended")
                cli("click", '#actbar [aria-label="Settings"]')
                ready('!!document.querySelector(\'.settings [data-nav="projects"]\')')
                cli("click", '.settings [data-nav="projects"]')
                ready('!!document.querySelector(".st-projects")')
                cli("eval", "Array.from(document.querySelectorAll('.st-projects button')).find(b=>b.textContent.includes('Add a project by hand')).click();true")
                cli("fill", '.st-ptable tbody tr:first-child input[placeholder="client/repo"]', "a" * 65)
                cli("fill", '.st-ptable tbody tr:first-child input[placeholder="/path/to/checkout"]', root)
                cli("click", 'section[data-sid="projects"] .st-save')
                ready("document.querySelector('[data-err=projects]')?.textContent.includes('1–64 ASCII')===true")
                checks.append("A01-B04 key65 explains the per-level limit and retains draft")
                shot("key-limit")
                cli("fill", '.st-ptable tbody tr:first-child input[placeholder="client/repo"]', "a" * 64)
                cli("click", 'section[data-sid="projects"] .st-save')
                ready("document.querySelector('section[data-sid=projects] .st-note')?.textContent.startsWith('Saved:')===true")
                self.assertIn("a" * 64, common.read_json(common.PROJECTS_FILE, {})["projects"])
                checks.append("A01-B04 key64 persists without changing its checkout")
                # Exercise the same exported consent renderer with its actual Codex argument;
                # provider import/replay is covered separately, not simulated as a successful import here.
                cli("eval", "(async()=>{const m=await import('/piper/dist/convo.js');const root=document.querySelector('#overlay');root.classList.add('open');const layer=document.createElement('div');layer.className='olayer';const modal=document.createElement('div');modal.className='modal';modal.append(m.hiddenImportNotice(['private/settings.txt'],()=>{},()=>{},'codex'));layer.append(modal);root.append(layer);return true;})()")
                ready("document.querySelector('.resume-hidden')?.textContent.includes('Codex on the host read')===true")
                self.assertNotIn("Claude", cli("eval", "document.querySelector('.resume-hidden').textContent"))
                checks.append("A02-001 actual consent renderer names Codex and the receiving agent")
                shot("codex-consent")
                print("LIFECYCLE_RESULT " + json.dumps({"checks": checks}), flush=True)
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()
