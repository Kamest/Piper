"""Headless: the New session picker's All projects row (sessions.WORKSPACE) starts a Direct session."""
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
import piperd
import sessions


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for headless picker checks")
class WorkspacePickerBrowserTest(unittest.TestCase):
    def test_all_projects_is_the_first_row_and_starts_direct(self):
        manager = _env.manager(self, background=True)
        root = _env.make_project("picker-a", {"README.md": "a\n"})
        subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
        _env.register("acme/picker-a", root)
        started = []
        real_create = manager.create

        def create(project, *a, **k):
            started.append({"project": project, "runtime": k.get("runtime"), "agent": k.get("agent")})
            return real_create(project, *a, **k)

        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "piper-ws-%s" % os.getpid()

        def cli(*args):
            r = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(r.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if cli("eval", code) is True:
                    return
                time.sleep(.1)
            self.fail(code)

        def shot(name):
            path = Path(os.environ["TEST_SANDBOX_WORK"], "workspace-" + name + ".png")
            cli("screenshot", str(path))
            if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

        def search(text):
            if cli("eval", 'document.querySelector("#new-session-project").getAttribute("aria-expanded")') != "true":
                cli("click", "#new-session-project")
            ready('document.querySelector("#new-session-project").getAttribute("aria-expanded")==="true"')
            cli("fill", "#new-session-project-search", text)

        visible = lambda sel: '(()=>{const e=document.querySelector(%s);return !!e&&e.getClientRects().length>0&&getComputedStyle(e).visibility!=="hidden";})()' % json.dumps(sel)  # noqa: E731
        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port), \
                mock.patch.object(manager, "create", create), mock.patch.object(sessions.Manager, "_start", lambda self, s: None):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1280", "800")
                cli("click", '#actbar [aria-label="New session"]')
                ready('!!document.querySelector("#new-session-task")')
                search("")
                ready('!!document.querySelector("#new-session-projects [data-project=workspace]")')
                rows = cli("eval", "[...document.querySelectorAll('#new-session-projects [data-project]')].map(e=>e.dataset.project)")
                self.assertEqual(rows[0], "workspace")
                self.assertIn("acme/picker-a", rows)
                self.assertIn("All projects", cli("eval", "document.querySelector('#new-session-project-0').textContent"))
                shot("picker")
                # the search finds it by its name, and Enter on the highlighted first row chooses it
                search("all proj")
                ready("document.querySelectorAll('#new-session-projects [data-project]').length===1")
                cli("press", "Enter")
                ready('document.querySelector("#new-session-project-name")?.textContent==="All projects"')
                self.assertFalse(cli("eval", visible(".wk-runtime [role=radio]")))   # no Hole / Direct choice
                self.assertTrue(cli("eval", visible(".wk-runtime .wk-direct-only")))
                self.assertFalse(cli("eval", visible(".where")))
                self.assertFalse(cli("eval", visible(".so-remember")))
                self.assertIn("reads every project folder", cli("eval", "document.querySelector('.so-native').textContent"))
                cli("fill", "#new-session-task", "Find vulnerable Java dependencies")
                shot("chosen")
                # another project brings the earlier Hole choice back
                search("team-x")
                ready("document.querySelectorAll('#new-session-projects [data-project]').length>0")
                self.assertNotEqual(cli("eval", "document.querySelector('#new-session-projects [data-project]').dataset.project"), "workspace")
                search("picker-a")
                cli("press", "Enter")
                ready('document.querySelector("#new-session-project-name")?.textContent==="acme/picker-a"')
                self.assertTrue(cli("eval", visible(".wk-runtime [role=radio]")))
                self.assertTrue(cli("eval", visible(".where")))
                search("")
                cli("eval", "document.querySelector('#new-session-projects [data-project=workspace]').click();true")
                ready('document.querySelector("#new-session-project-name")?.textContent==="All projects"')
                cli("click", ".wk-start")
                ready("!document.querySelector('#new-session-task')")
                self.assertEqual(started, [{"project": "workspace", "runtime": "direct", "agent": "claude"}])
                s = next(x for x in manager.sessions.values() if x.kind == "workspace")
                self.assertEqual((s.runtime, s.task), ("direct", "Find vulnerable Java dependencies"))
                # the session it opens renders as All projects, without client errors
                cli("eval", "window.__piperErrors=[];addEventListener('error',e=>window.__piperErrors.push(String(e.message)));true")
                ready('[...document.querySelectorAll(".session-identity")].some(e=>(e.title||"").includes("all projects"))')
                shot("session")
                self.assertEqual(cli("eval", "window.__piperErrors"), [])
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()


if __name__ == "__main__":
    unittest.main()
