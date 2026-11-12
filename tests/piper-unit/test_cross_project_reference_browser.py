"""Real Manager/API → terminal reference → registered sibling Files regressions."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest
from unittest import mock

import _env
import common
import piperd
import sessions


def git_fixture(root):
    for args in (("init", "-qb", "main"), ("add", "."), ("commit", "-qm", "fixture baseline")):
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for headless cross-project reference routing")
class CrossProjectReferenceBrowserTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, self.manager)
        # the project registry is one file per test process: start from an empty one and put it back afterwards, so
        # another module's acme/app (whose checkout has a helper-kit/ folder) cannot make the sibling path ambiguous
        self.addCleanup(common.write_json, common.PROJECTS_FILE, common.read_json(common.PROJECTS_FILE, {"projects": {}}))
        common.write_json(common.PROJECTS_FILE, {"projects": {}})
        self.root = os.path.realpath(_env.make_project("reference-source", {"README.md": "Source checkout\n"}))
        git_fixture(self.root)
        _env.register("acme/app", self.root)
        self.sibling = os.path.realpath(_env.make_project("helper-kit", {".gitlab-ci.yml": "stages: [sibling-test]\n"}))
        git_fixture(self.sibling)
        _env.register("acme/helper-kit", self.sibling)
        self.session = sessions.Session(project="acme/app", path=self.root, title="Cross-project reference", state="waiting")
        self.session.save()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", self.manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.browser = "piper-cross-project-%s" % os.getpid()
        self.addCleanup(self.cli, "close")
        self.cli("open", piperd.gui_url())
        self.cli("set", "viewport", "1280", "800")
        self.ready('!!document.querySelector("#actbar")')
        sid = self.session.id
        child = self.manager.pty.call("spawn", id=sid, argv=[sys.executable, "-c", "import time;time.sleep(120)"],
                                      cwd=self.root, env=dict(os.environ), cols=80, rows=24)
        self.session.pid = child["pid"]
        self.cli("eval", '''(async()=>{
            const {TermView}=await import('/piper/dist/term.js');
            const connect=TermView.prototype.connect;
            TermView.prototype.connect=function(){
                if(this.id===%s){window.referenceView=this;return;}return connect.call(this);
            };return true;
        })()''' % json.dumps(sid))
        self.manager.sessions[sid] = self.session
        self.manager.bump()
        self.ready('!!document.querySelector(%s)' % json.dumps('#queue [data-sid="%s"]' % sid))
        self.select_terminal()

    def cli(self, *args):
        result = subprocess.run(["agent-browser", "--session", self.browser, "--json", *args],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
        data = json.loads(result.stdout)
        self.assertTrue(data["success"], "agent-browser action failed")
        return data.get("data", {}).get("result")

    def ready(self, expression):
        self.cli("wait", "--fn", expression)

    def shot(self, name):
        target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
        self.cli("screenshot", str(target))
        print("CROSS_PROJECT_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)

    def select_terminal(self):
        self.cli("eval", 'document.querySelector(%s).click();true' % json.dumps('#queue [data-sid="%s"]' % self.session.id))
        self.cli("eval", '[...document.querySelectorAll("header .tab")].find(b=>b.textContent==="Terminal")?.click();true')
        self.ready('!!window.referenceView?.term.element')
        self.ready('referenceView.term.element.checkVisibility()')

    def open_path(self, path, *, pointer=True):
        self.cli("eval", '''(async()=>{
            const t=referenceView.term;referenceView.freeze(true);t.resize(100,12);t.reset();
            await new Promise(done=>t.write(%s,done));return true;
        })()''' % json.dumps(path + "\r\n"))
        self.assertTrue(self.cli("eval", 'referenceView.fileLinks(1).some(link=>link.text===%s)' % json.dumps(path)))
        if not pointer:
            self.cli("eval", 'referenceView.fileLinks(1).find(link=>link.text===%s).activate({});true' % json.dumps(path))
            return
        x, y = self.cli("eval", '''(()=>{const r=referenceView.host.querySelector('.xterm-screen').getBoundingClientRect();
            return [r.x+r.width/referenceView.term.cols*4.5,r.y+r.height/referenceView.term.rows*.5]})()''')
        self.cli("mouse", "move", "0", "0")
        self.cli("mouse", "move", str(x), str(y))
        self.ready('!!referenceView.host.querySelector(".xterm-cursor-pointer") || referenceView.term.element.classList.contains("xterm-cursor-pointer")')
        self.cli("mouse", "down")
        self.cli("mouse", "up")

    def test_sibling_file_exact_current_priority_and_ambiguous_choice(self):
        self.open_path("helper-kit/.gitlab-ci.yml")
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [sibling-test]")')
        self.assertEqual(self.cli("eval", 'document.querySelector(".files .crumbs strong").textContent'), ".gitlab-ci.yml")
        self.shot("cross-project-sibling-file")

        # The very same relative token names a real file inside the source checkout now.
        current = Path(self.root, "helper-kit", ".gitlab-ci.yml")
        current.parent.mkdir()
        current.write_text("stages: [current-priority]\n")
        self.select_terminal()
        self.open_path("helper-kit/.gitlab-ci.yml", pointer=False)
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [current-priority]")')
        self.shot("cross-project-current-priority")

        current.unlink()
        current.parent.rmdir()
        self.cli("eval", '''(async()=>{const {api}=await import('/piper/dist/api.js');
            const state=api.state;api.state=async(...args)=>{const result=await state(...args);
                window.referenceProjects=result.data?.projects;return result;};return true;})()''')
        other = os.path.realpath(_env.make_project("other/helper-kit", {".gitlab-ci.yml": "stages: [chosen-other]\n"}))
        git_fixture(other)
        _env.register("other/helper-kit", other)
        self.manager.bump()
        self.select_terminal()
        # Wait for the browser's actual state refresh to include the second registered project.
        self.ready('window.referenceProjects?.some(p=>p.key==="other/helper-kit")')
        self.open_path("helper-kit/.gitlab-ci.yml", pointer=False)
        self.ready('document.querySelector(".cmenu")?.textContent.includes("acme/helper-kit") && document.querySelector(".cmenu")?.textContent.includes("other/helper-kit")')
        self.assertFalse(self.cli("eval", 'document.querySelector(".files .cm-editor")?.textContent.includes("stages: [chosen-other]") ?? false'))
        self.shot("cross-project-ambiguous-choice")
        self.cli("eval", '[...document.querySelectorAll(".cmenu button")].find(b=>b.textContent.includes("other/helper-kit")).click();true')
        self.ready('document.querySelector(".files .cm-editor")?.textContent.includes("stages: [chosen-other]")')
        self.shot("cross-project-selected-file")
