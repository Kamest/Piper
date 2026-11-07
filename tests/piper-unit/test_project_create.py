"""GUI clone/empty project creation: real local Git, asynchronous jobs and fail-closed HTTP scope."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import piperd
import projects
from test_piperd import FakeManager, get, post


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True,
                          stdin=subprocess.DEVNULL).stdout.decode()


class ProjectCreateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="project-create-")
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.previous = common.read_json(common.PROJECTS_FILE, {})
        self.addCleanup(common.write_json, common.PROJECTS_FILE, self.previous)
        self.token = common.own_token("gui")
        self.manager = mock.patch.object(piperd, "MANAGER", FakeManager())
        self.manager.start()
        self.addCleanup(self.manager.stop)

    def destination(self, name="created"):
        return os.path.join(self.root, name)

    def wait_job(self, job):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            result = projects.create_job(job["id"])
            if result["state"] != "running":
                return result
            time.sleep(.02)
        self.fail("project creation did not finish")

    def source(self):
        root = self.destination("source")
        os.mkdir(root)
        git(root, "init", "-q", "-b", "main")
        with open(os.path.join(root, "README.md"), "w") as f:
            f.write("Example project\n")
        git(root, "add", "README.md")
        git(root, "commit", "-qm", "initial fixture")
        return root

    def test_directory_picker_lists_only_real_folders_with_gui_scope(self):
        for name in ("zeta", "Ünicode folder", ".git"):
            os.mkdir(self.destination(name))
        with open(self.destination("private-file.txt"), "w") as f:
            f.write("not a directory picker result")
        os.symlink(self.destination("zeta"), self.destination("link"))
        from urllib.parse import urlencode
        url = "/api/projects/directories?" + urlencode({"path": self.root})
        code, _, raw = get(url, token=self.token)
        self.assertEqual(code, 200)
        data = json.loads(raw)
        self.assertEqual(data["path"], self.root)
        self.assertEqual([row["name"] for row in data["directories"]], ["zeta", "Ünicode folder"])
        self.assertFalse(data["empty"])
        self.assertTrue(projects.creation_directories(self.destination("zeta"))["empty"])
        self.assertEqual(get(url, token=common.own_token("tty"))[0], 403)
        for path, expected in (("relative", 400), (self.destination("missing"), 404), (self.destination(".git"), 400)):
            self.assertEqual(get("/api/projects/directories?" + urlencode({"path": path}), token=self.token)[0], expected)
        with mock.patch.object(projects.os, "scandir", side_effect=PermissionError()):
            self.assertEqual(get(url, token=self.token)[0], 403)

    def test_empty_project_is_a_registered_git_repository_without_a_commit(self):
        job = projects.create_start({"mode": "empty", "path": self.destination()})
        self.assertIn(job["state"], ("running", "done"))
        result = self.wait_job(job)
        self.assertEqual(result["state"], "done", result)
        self.assertFalse(result["can_cancel"])
        self.assertTrue(os.path.isfile(os.path.join(result["path"], ".git", "HEAD")))
        self.assertEqual(projects.find(result["key"])["path"], result["path"])
        self.assertEqual(git(result["path"], "status", "--porcelain"), "")
        self.assertEqual(git(result["path"], "ls-files"), "")
        self.assertEqual(common.read_json(common.PROJECTS_FILE, {})["projects"][result["key"]]["gitlab"], [])

    def test_clone_copies_real_history_and_never_runs_global_hooks(self):
        source = self.source()
        hooks = self.destination("hooks")
        os.mkdir(hooks)
        marker = self.destination("HOOK-RAN")
        hook = os.path.join(hooks, "post-checkout")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\ntouch '%s'\n" % marker)
        os.chmod(hook, 0o755)
        config = self.destination("gitconfig")
        with open(config, "w") as f:
            f.write("[core]\n hooksPath = %s\n" % hooks)
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": config}):
            result = self.wait_job(projects.create_start({"mode": "clone", "path": self.destination(), "url": source}))
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(git(result["path"], "log", "-1", "--format=%s"), "initial fixture\n")
        self.assertEqual(git(result["path"], "remote", "get-url", "origin").strip(), source)
        with open(os.path.join(result["path"], "README.md")) as f:
            self.assertEqual(f.read(), "Example project\n")
        self.assertFalse(os.path.exists(marker))

    def test_existing_nonempty_and_symlink_targets_are_never_reused(self):
        for name in ("nonempty", "link"):
            path = self.destination(name)
            if name == "link":
                os.symlink(self.destination("missing-link-target"), path)
            else:
                os.mkdir(path)
            if name == "nonempty":
                with open(os.path.join(path, "keep.txt"), "w") as f:
                    f.write("keep\n")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "not empty|already exists"):
                projects.create_start({"mode": "empty", "path": path})
        self.assertFalse(os.path.exists(self.destination("missing-link-target")))
        with open(self.destination("nonempty/keep.txt")) as f:
            self.assertEqual(f.read(), "keep\n")

    def test_existing_empty_folder_can_receive_a_project(self):
        path = self.destination()
        os.mkdir(path)
        identity = os.stat(path)
        result = self.wait_job(projects.create_start({"mode": "empty", "path": path}))
        self.assertEqual(result["state"], "done", result)
        self.assertEqual((os.stat(path).st_dev, os.stat(path).st_ino), (identity.st_dev, identity.st_ino))

    def test_invalid_modes_paths_and_credential_urls_are_rejected_before_mkdir(self):
        for body in ({"mode": "other"}, {"mode": "empty", "path": "relative/path"},
                     {"mode": "empty", "path": self.destination("missing-parent/child")},
                     {"mode": "empty", "path": self.destination(".git/new")},
                     *({"mode": "clone", "path": self.destination(), "url": url} for url in (
                         "https://user:example-password@example.com/shop.git", "https://token@example.com/shop.git",
                         "https://example.com/shop.git?token=example-token", "https://example.com/shop.git#secret",
                         "ssh://git:example-password@example.com/shop.git", "ssh://git%3Asecret@example.com/shop.git",
                         "ext::sh -c echo", "-u", "https://example.com/repo.git\noption"))):
            with self.subTest(body=body), self.assertRaises(ValueError):
                projects.create_start(body)
        self.assertFalse(os.path.lexists(self.destination()))

    def test_valid_remote_urls_are_preserved_without_network_access(self):
        for url in ("https://example.com/acme/shop.git", "ssh://git@example.com:2222/acme/shop.git",
                    "git@example.com:acme/shop.git"):
            self.assertEqual(projects._clone_source(url), url)

    def test_registration_avoids_key_collisions_and_preserves_other_settings(self):
        data = common.read_json(common.PROJECTS_FILE, {})
        data.setdefault("projects", {})["created"] = {"path": self.destination("different")}
        data["future_field"] = {"keep": True}
        common.write_json(common.PROJECTS_FILE, data)
        result = self.wait_job(projects.create_start({"mode": "empty", "path": self.destination()}))
        self.assertEqual(result["key"], "created-2")
        after = common.read_json(common.PROJECTS_FILE, {})
        self.assertEqual(after["future_field"], {"keep": True})
        self.assertEqual(after["projects"]["created"]["path"], self.destination("different"))

    def test_job_requests_return_while_git_runs_and_cancel_reaps_only_its_child(self):
        started = threading.Event()
        real_popen, children = subprocess.Popen, []

        def slow_git(*args, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            started.set()
            return child

        with mock.patch.object(projects.subprocess, "Popen", side_effect=slow_git):
            before = time.monotonic()
            code, _, raw = post("/api/projects/create", {"mode": "empty", "path": self.destination()}, self.token)
            self.assertEqual(code, 202)
            self.assertLess(time.monotonic() - before, 1)
            job = json.loads(raw)
            self.assertTrue(started.wait(2))
            with open(self.destination("keep.txt"), "w") as f:
                f.write("concurrent user edit\n")
            self.assertEqual(get("/api/projects/create/" + job["id"], token=self.token)[0], 200)
            self.assertEqual(post("/api/projects/create/" + job["id"] + "/cancel", {}, self.token)[0], 200)
            result = self.wait_job(job)
        self.assertEqual(result["state"], "cancelled")
        self.assertIsNotNone(children[0].poll())
        with open(self.destination("keep.txt")) as f:
            self.assertEqual(f.read(), "concurrent user edit\n")
        self.assertFalse(any(p.get("path") == self.destination() for p in common.read_json(common.PROJECTS_FILE, {}).get("projects", {}).values()))

    def test_git_error_never_registers_or_exposes_output(self):
        source = self.destination("not-a-repo")
        os.mkdir(source)
        result = self.wait_job(projects.create_start({"mode": "clone", "path": self.destination(), "url": source}))
        self.assertEqual(result["state"], "error")
        self.assertIn("Check repository access", result["message"])
        self.assertNotIn("key", result)
        self.assertNotIn("url", result)

    @unittest.skipUnless(os.name == "posix", "directory-descriptor Git cwd is POSIX only")
    def test_swapping_path_after_open_cannot_replace_the_pinned_identity(self):
        destination, moved, outside = self.destination(), self.destination("reserved"), self.destination("outside")
        os.mkdir(outside)
        real_open = os.open

        def swapped(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if path == destination:
                os.rename(destination, moved)
                os.symlink(outside, destination)
            return fd

        with mock.patch.object(projects.os, "open", side_effect=swapped):
            result = self.wait_job(projects.create_start({"mode": "empty", "path": destination}))
        self.assertEqual(result["state"], "error", result)
        self.assertNotIn("key", result)
        self.assertFalse(os.path.lexists(os.path.join(outside, ".git")))
        self.assertTrue(os.path.isdir(os.path.join(moved, ".git")))

    @unittest.skipUnless(os.name == "posix", "directory-descriptor Git cwd is POSIX only")
    def test_swapped_destination_cannot_redirect_git_or_be_registered(self):
        destination, moved, outside = self.destination(), self.destination("reserved"), self.destination("outside")
        os.mkdir(outside)
        real_popen = subprocess.Popen

        def swapped(*args, **kwargs):
            os.rename(destination, moved)
            os.symlink(outside, destination)
            return real_popen(*args, **kwargs)

        with mock.patch.object(projects.subprocess, "Popen", side_effect=swapped):
            result = self.wait_job(projects.create_start({"mode": "empty", "path": destination}))
        self.assertEqual(result["state"], "error")
        self.assertIn("destination changed", result["message"])
        self.assertFalse(os.path.lexists(os.path.join(outside, ".git")))
        self.assertTrue(os.path.isdir(os.path.join(moved, ".git")))
        self.assertNotIn("key", result)

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for bounded project picker")
    def test_browser_project_picker_bounds_search_keyboard_and_actions(self):
        import sessions
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        for i in range(35):
            key = "acme/team-%02d/very-long-project-name-%02d" % (i, i)
            path = os.path.join(self.root, "workspace-parent-with-a-long-name", "team-%02d" % i, "very-long-project-name-%02d" % i)
            os.makedirs(path)
            Path(path, "README.md").write_text("Synthetic picker fixture\n")
            _env.register(key, path)
            if i < 3:
                item = sessions.Session(project=key, path=path, title="Synthetic running task", state="working")
                manager.sessions[item.id] = item
                item.save()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = "piper-picker-%s" % os.getpid()
        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(self.token, "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")
        def shot(name):
            target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
            cli("screenshot", str(target))
            print("FEEDBACK_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)
        try:
            cli("open", piperd.gui_url())
            cli("wait", "--fn", '!!document.querySelector(\'#actbar [aria-label="New session"]\')')
            for font in (13, 16):
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:%d/api/settings" % server.server_port,
                    data=json.dumps({"section":"interface", "values":{"ui_font_size":str(font)}}).encode(),
                    headers={"Authorization":"Bearer "+self.token, "Content-Type":"application/json"})
                with urllib.request.urlopen(req) as response:
                    self.assertEqual(response.status, 200)
                cli('wait', '--fn', 'getComputedStyle(document.body).fontSize===%s' % json.dumps(str(font)+'px'))
                for width, height in ((1380,982),(1024,800),(390,800),(690,491)):
                    with self.subTest(font=font,width=width,height=height):
                        cli('set','viewport','1024','800')
                        cli('click','#actbar [aria-label="New session"]')
                        cli('set','viewport',str(width),str(height))
                        cli('wait','--fn','!!document.querySelector("#new-session-project")')
                        if cli('eval','document.querySelector("#new-session-project").getAttribute("aria-expanded")==="false"'):
                            cli('click','#new-session-project')
                        cli('wait','--fn','!!document.querySelector(".wk-project-pop:not(.hidden)")')
                        cli('wait','--fn','document.querySelector(".modal").getAnimations().every(a=>a.playState!=="running")')
                        geometry=cli('eval',"""(()=>{const p=document.querySelector('.wk-project-pop:not(.hidden)'),f=document.querySelector('.wk-foot'),m=p.closest('.modal'),r=p.getBoundingClientRect(),b=f.getBoundingClientRect(),s=document.querySelector('#new-session-project'),create=p.querySelector('.wk-project-create-entry');
                          return{popup:{x:r.x,y:r.y,width:r.width,bottom:r.bottom},footer:{top:b.top,bottom:b.bottom},modalHeight:m.getBoundingClientRect().height,rows:p.querySelectorAll('[data-project]').length,triggerTitle:s.title,selected:p.querySelector('[aria-selected=true]')?.textContent,running:[...p.querySelectorAll('.mut')].map(e=>e.textContent),createVisible:create.checkVisibility(),scrollHeight:p.querySelector('.plist').scrollHeight}})()""")
                        print('PROJECT_PICKER_GEOMETRY',font,width,height,json.dumps(geometry),flush=True)
                        cli('hover','#new-session-project')
                        shot('project-picker-%s-%sx%s'%(font,width,height))
                        try:
                            self.assertGreaterEqual(geometry['popup']['x'],0,geometry)
                            self.assertLessEqual(geometry['popup']['x']+geometry['popup']['width'],width,geometry)
                            self.assertLessEqual(geometry['popup']['bottom'],geometry['footer']['top'],geometry)
                            self.assertEqual(geometry['rows'], 35)
                            self.assertIn('1 running', geometry['running'])
                            self.assertTrue(cli('eval', "document.querySelector('#new-session-project').title==='' && [...document.querySelectorAll('#new-session-projects [data-project]')].every(e=>!e.hasAttribute('title'))"))
                            self.assertTrue(cli('eval', "(()=>{const p=document.querySelector('#new-session-picker-path'),r=p.getBoundingClientRect();return p.checkVisibility()&&r.left>=0&&r.right<=innerWidth&&p.scrollWidth<=p.clientWidth+1&&scrollY===0})()"))
                            path_geometry = cli('eval', """(()=>{const p=document.querySelector('#new-session-picker-path'),s=getComputedStyle(p),r=p.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(p);
                              const top=r.top+parseFloat(s.borderTopWidth)+parseFloat(s.paddingTop),bottom=r.bottom-parseFloat(s.borderBottomWidth)-parseFloat(s.paddingBottom),
                                glyphs=[...range.getClientRects()].filter(x=>x.bottom>top&&x.top<bottom);
                              return {box:s.boxSizing,maxHeight:parseFloat(s.maxHeight),lineHeight:parseFloat(s.lineHeight),visible:glyphs.length,
                                fullGlyphs:glyphs.every(x=>x.top>=top-.5&&x.bottom<=bottom+.5)};})()""")
                            self.assertEqual(path_geometry['box'], 'content-box', path_geometry)
                            self.assertAlmostEqual(path_geometry['maxHeight'], 3*path_geometry['lineHeight'], delta=.5, msg=path_geometry)
                            self.assertGreater(path_geometry['visible'], 0, path_geometry)
                            self.assertTrue(path_geometry['fullGlyphs'], path_geometry)
                            # Hover a real row: the complete path stays inside the form,
                            # rather than asking the OS to place an unbounded native title.
                            cli('focus','#new-session-project-search')
                            cli('press','Home')
                            cli('hover','#new-session-project-1')
                            self.assertIn('very-long-project-name-01',cli('eval',"document.querySelector('#new-session-picker-path').textContent"))
                            self.assertTrue(cli('eval', "[...document.querySelectorAll('.wk-foot button')].every(b=>{const r=b.getBoundingClientRect();return b.checkVisibility({checkOpacity:true})&&b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})"))
                            cli('focus','#new-session-project-search')
                            cli('press','Home')
                            cli('press','PageDown')
                            self.assertEqual(cli('eval',"document.querySelector('#new-session-project-search').getAttribute('aria-activedescendant')"),'new-session-project-5')
                            cli('press','End')
                            self.assertEqual(cli('eval',"document.querySelector('#new-session-project-search').getAttribute('aria-activedescendant')"),'new-session-project-create')
                            self.assertTrue(cli('eval',"(()=>{const c=document.querySelector('#new-session-project-create'),r=c.getBoundingClientRect(),f=document.querySelector('.wk-foot').getBoundingClientRect();return r.bottom<=f.top&&c.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()"))
                            shot('project-picker-create-%s-%sx%s'%(font,width,height))
                            cli('press','Home')
                            cli('press','Escape')
                            self.assertEqual(cli('eval','document.activeElement.id'),'new-session-project')
                            self.assertTrue(cli('eval',"document.querySelector('#new-session-selected-path').checkVisibility()"))
                            # Search matches the project key/path, selection returns
                            # to the task, and no-match Enter still opens create/clone.
                            cli('click','#new-session-project')
                            cli('fill','#new-session-project-search','team-34')
                            self.assertEqual(cli('eval',"document.querySelectorAll('#new-session-projects [data-project]').length"),1)
                            cli('press','Enter')
                            self.assertEqual(cli('eval','document.activeElement.id'),'new-session-task')
                            self.assertIn('team-34',cli('eval',"document.querySelector('#new-session-project-name').textContent"))
                            cli('fill','#new-session-task','Keep the picker draft')
                            cli('click','#new-session-project')
                            cli('fill','#new-session-project-search','no-such-synthetic-project')
                            self.assertEqual(cli('eval',"document.querySelectorAll('#new-session-projects [data-project]').length"),0)
                            cli('press','Enter')
                            cli('wait','--fn','!!document.querySelector(".wk-project-create")')
                            self.assertTrue(cli('eval',"!!document.querySelector('#project-create-url') && [...document.querySelectorAll('.wk-project-create [role=radio]')].some(e=>e.textContent==='Empty project')"))
                            shot('project-picker-clone-sheet-%s-%sx%s'%(font,width,height))
                            cli('click','.wk-project-create-back')
                            cli('wait','--fn','!document.querySelector(".wk-project-create")')
                            self.assertEqual(cli('eval',"document.querySelector('#new-session-task').value"),'Keep the picker draft')
                            cli('fill','#new-session-task','')
                        finally:
                            cli('press','Escape')
                            cli('press','Escape')
        finally:
            cli('close')

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the headless project creation flow")
    def test_browser_empty_clone_validation_and_preserved_draft(self):
        manager = _env.manager(self, background=True)
        self.addCleanup(_env.clear_sessions, manager)
        source = self.source()
        _env.register("acme/source", source)
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        self.enterContext(mock.patch.object(piperd, "MANAGER", manager))
        self.enterContext(mock.patch.object(piperd, "PORT", server.server_port))
        # Only the transport fixture changes: the browser sends a normal remote URL and
        # every HTTP/job/registration step executes real Git against local fixture history.
        validate = projects._clone_source
        self.enterContext(mock.patch.object(projects, "_clone_source", side_effect=lambda url:
            source if url == "https://example.com/acme/source.git" else validate(url)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        browser = "piper-project-create-%s" % os.getpid()

        def cli(*args):
            result = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(self.token, "[token]"))
            data = json.loads(result.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(expression):
            try:
                cli("wait", "--fn", expression)
            except AssertionError:
                print("PROJECT_CREATE_DIAGNOSTIC", cli("eval", "document.body.innerText.slice(-2500)"), flush=True)
                raise

        def shot(name):
            target = Path(os.environ["TEST_SANDBOX_WORK"], name + ".png")
            cli("screenshot", str(target))
            print("FEEDBACK_PNG " + name + " " + base64.b64encode(target.read_bytes()).decode(), flush=True)

        def create_sheet():
            cli("click", "#new-session-project")
            cli("click", ".wk-project-create-entry")
            ready('!!document.querySelector(".wk-project-create")')

        try:
            cli("open", piperd.gui_url())
            cli("set", "viewport", "1280", "800")
            ready('!!document.querySelector("#actbar")')
            ready('!!document.querySelector(\'#actbar [aria-label="New session"]\')')
            cli("click", '#actbar [aria-label="New session"]')
            ready('!!document.querySelector("#new-session-task")')
            cli("fill", "#new-session-task", "Keep this task draft")
            cli("click", "#new-session-project")
            cli("fill", ".wk-project-pop input", "no such project")
            shot("f01-project-picker")
            # The persistent last row is accessible by keyboard even with no matching project.
            cli("press", "ArrowDown")
            cli("press", "Enter")
            ready('!!document.querySelector(".wk-project-create")')
            self.assertEqual(cli("eval", "document.activeElement.id"), "project-create-url")
            cli("fill", "#project-create-url", "https://user:example-token@example.com/acme/source.git")
            cli("click", "#project-create-location")
            self.assertTrue(cli("eval", 'document.querySelector(".wk-project-create button[type=submit]").disabled'))
            self.assertNotIn("example-token", cli("eval", 'document.querySelector("#project-create-url").value'))
            shot("f01-url-credentials")
            cli("click", ".wk-project-create-back")
            ready('!document.querySelector(".wk-project-create")')
            self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this task draft")
            create_sheet()
            cli("eval", '[...document.querySelectorAll(".wk-project-create [role=radio]")].find(b=>b.textContent==="Empty project").click(); true')
            cli("click", '[aria-label="Choose parent folder"]')
            ready('!!document.querySelector(".wk-project-create-picker")')
            self.assertTrue(cli("eval", '(()=>{const p=document.querySelector(".wk-project-create-picker > p");return p.checkVisibility()&&p.textContent.includes("filesystem of the selected Piper host")&&!!p.querySelector(".chip").textContent.trim()&&!p.textContent.includes("not this computer")})()'))
            cli("fill", '[aria-label="Folder path"]', self.destination("missing-folder"))
            cli("press", "Enter")
            ready('document.querySelector(".wk-project-create-picker .st-err")?.textContent.includes("doesn’t exist") || document.querySelector(".wk-project-create-picker .st-err")?.textContent.includes("doesn\'t exist")')
            self.assertTrue(cli("eval", '[...document.querySelectorAll(".wk-project-create-picker button")].find(b=>b.textContent==="Use folder").disabled'))
            shot("f01-folder-missing")
            bidi_folder = self.destination("reports\u202Etxt")
            os.mkdir(bidi_folder)
            cli("fill", '[aria-label="Folder path"]', bidi_folder)
            cli("press", "Enter")
            ready('document.querySelector(".wk-project-create-picker .wk-project-create-foot")?.textContent.includes("[U+202E]")')
            self.assertEqual(cli("eval", 'document.querySelector(".wk-project-create-picker input").value'), bidi_folder,
                             "editable paths keep their identity while shown text marks bidi controls")
            cli("fill", '[aria-label="Folder path"]', self.root)
            cli("press", "Enter")
            ready('!!document.querySelector(".wk-project-create-folders .pitem")')
            shot("f01-server-folder-picker")
            cli("eval", '[...document.querySelectorAll(".wk-project-create-folders button")].find(b=>b.textContent.trim()==="source").click();true')
            ready('document.querySelector(".wk-project-create-folders")?.textContent.includes("No subfolders here")')
            self.assertFalse(cli("eval", '[...document.querySelectorAll(".wk-project-create-picker button")].find(b=>b.textContent==="Use folder").disabled'))
            cli("click", '[aria-label="Up one folder"]')
            ready('!!document.querySelector(".wk-project-create-folders .pitem")')
            cli("eval", '[...document.querySelectorAll(".wk-project-create-picker button")].find(b=>b.textContent==="Use folder").click();true')
            ready('!document.querySelector(".wk-project-create-picker")')
            self.assertTrue(cli("eval", 'document.querySelector("#project-create-location").value.startsWith(%s)' % json.dumps(self.root + "/")))
            before_pick = cli("eval", 'document.querySelector("#project-create-location").value')
            # Native bridge is mocked: this test never opens an OS dialog on the working desktop.
            cli("eval", "window.__piperNative={platform:'darwin',pickDirectory:initial=>new Promise(resolve=>{window.pickInitial=initial;window.pickResolve=resolve})};true")
            cli("click", '[aria-label="Choose parent folder"]')
            self.assertTrue(cli("eval", 'document.querySelector("#project-create-location + button").disabled'))
            cli("eval", "window.pickResolve(null);true")
            ready('document.querySelector("#project-create-location + button").disabled===false')
            self.assertEqual(cli("eval", 'document.querySelector("#project-create-location").value'), before_pick)
            self.assertEqual(cli("eval", 'document.activeElement.getAttribute("aria-label")'), "Choose parent folder")
            cli("fill", "#project-create-location", self.destination("custom-name"))
            os.mkdir(self.destination("native-parent"))
            cli("eval", 'window.__piperNative.pickDirectory=async()=>%s;true' % json.dumps(self.destination("native-parent")))
            cli("click", '[aria-label="Choose parent folder"]')
            ready('document.querySelector("#project-create-location + button").disabled===false')
            self.assertEqual(cli("eval", 'document.querySelector("#project-create-location").value'), self.destination("native-parent/custom-name"))
            cli("eval", "window.__piperNative.pickDirectory=async()=>{throw Error('private fixture detail')};true")
            cli("click", '[aria-label="Choose parent folder"]')
            ready('document.querySelector("#project-create-location-error")?.textContent.includes("folder picker")')
            self.assertNotIn("private fixture detail", cli("eval", 'document.querySelector(".wk-project-create").textContent'))
            cli("eval", "delete window.__piperNative;true")
            empty = self.destination("browser-empty")
            cli("fill", "#project-create-location", empty)
            shot("f01-empty-project")
            cli("click", ".wk-project-create button[type=submit]")
            ready('!document.querySelector(".wk-project-create") && document.querySelector(".wk-project-create-added")?.textContent.includes("Created")')
            self.assertTrue(os.path.isfile(os.path.join(empty, ".git", "HEAD")))
            self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this task draft")
            self.assertEqual(cli("eval", "document.activeElement.id"), "new-session-task")
            self.assertEqual(len(manager.sessions), 0)
            create_sheet()
            cli("fill", "#project-create-url", "https://example.com/acme/source.git")
            # An existing nonempty destination must fail in the real HTTP flow, keep the
            # draft, and let the user correct the location without recreating the sheet.
            cli("fill", "#project-create-location", source)
            cli("click", ".wk-project-create button[type=submit]")
            ready('document.querySelector(".wk-project-create button[type=submit]")?.textContent==="Try again"')
            self.assertFalse(cli("eval", 'document.querySelector("#project-create-url").disabled'))
            self.assertEqual(cli("eval", 'document.querySelector("#project-create-location").value'), source)
            self.assertEqual(len(manager.sessions), 0)
            shot("f01-nonempty-error")
            # Keep only this project's subprocess factory slow; agent-browser and the
            # private daemon/PTY continue normally. Cancel must reap the owned process.
            children = []
            def slow_git(*args, **kwargs):
                child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], **kwargs)
                children.append(child)
                self.addCleanup(child.wait, timeout=5)
                self.addCleanup(child.terminate)
                return child
            with mock.patch.object(projects, "subprocess", mock.Mock(wraps=subprocess)) as child_process:
                child_process.Popen.side_effect = slow_git
                child_process.DEVNULL = subprocess.DEVNULL
                child_process.TimeoutExpired = subprocess.TimeoutExpired
                cli("fill", "#project-create-location", self.destination("browser-cancel"))
                cli("click", ".wk-project-create button[type=submit]")
                ready('!![...document.querySelectorAll(".wk-project-create button")].find(b=>b.textContent==="Cancel clone"&&!b.disabled)')
                self.assertTrue(cli("eval", 'document.querySelector("#project-create-url").disabled && document.querySelector("#project-create-location").disabled'))
                shot("f01-clone-pending")
                self.assertTrue(cli("eval", 'document.activeElement.blur();document.body.focus();document.activeElement===document.body'))
                cli("press", "Escape")
                self.assertTrue(cli("eval", '!!document.querySelector(".wk-project-create")'))
                # Forcing BODY resets Chromium's sequential focus origin; one Tab may
                # traverse browser chrome before returning to the sole active control.
                for _ in range(3):
                    cli("press", "Tab")
                    if cli("eval", "document.activeElement.textContent") == "Cancel clone":
                        break
                self.assertEqual(cli("eval", "document.activeElement.textContent"), "Cancel clone")
                cli("set", "viewport", "390", "800")
                self.assertTrue(cli("eval", '(()=>{const r=document.querySelector(".wk-project-create-modal").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()'))
                shot("f01-clone-pending-narrow")
                cli("eval", '[...document.querySelectorAll(".wk-project-create button")].find(b=>b.textContent==="Cancel clone").click();true')
                ready('document.querySelector(".wk-project-create button[type=submit]")?.textContent==="Try again" && document.querySelector(".wk-project-create")?.textContent.includes("cancelled")')
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].poll())
                self.assertFalse(any(row.get("path") == self.destination("browser-cancel") for row in common.read_json(common.PROJECTS_FILE, {}).get("projects", {}).values()))
                shot("f01-clone-cancelled")
                cli("set", "viewport", "1280", "800")
            clone = self.destination("browser-clone")
            cli("fill", "#project-create-location", clone)
            shot("f01-clone-project")
            cli("click", ".wk-project-create button[type=submit]")
            ready('!document.querySelector(".wk-project-create") && document.querySelector(".wk-project-create-added")?.textContent.includes("Cloned")')
            self.assertEqual(git(clone, "log", "-1", "--format=%s"), "initial fixture\n")
            self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this task draft")
            self.assertEqual(cli("eval", 'document.querySelector("#new-session-project-name").textContent'), "browser-clone")
            self.assertEqual(len(manager.sessions), 0)
            shot("f01-cloned-draft")
            # Explicit Start uses the registered checkout. The standard fake-hole agent runs
            # through this test Manager's real private PTY; no Docker or host agent is launched.
            manager.policy["attestation"] = False
            cli("eval", '[...document.querySelectorAll(".workdlg button")].find(b=>b.textContent.startsWith("Start session")).click(); true')
            deadline = time.monotonic() + 15
            while not manager.sessions and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertEqual(len(manager.sessions), 1)
            session = next(iter(manager.sessions.values()))
            deadline = time.monotonic() + 15
            while session.state not in ("blocked", "ready", "running", "waiting", "error") and time.monotonic() < deadline:
                time.sleep(.05)
            if session.state == "blocked":
                manager.approve(session.id)
            deadline = time.monotonic() + 15
            while session.state not in ("ready", "running", "waiting", "error") and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn(session.state, ("ready", "running", "waiting"), session.detail)
            self.assertEqual((session.project, session.path, session.task), ("browser-clone", clone, "Keep this task draft"))
            self.assertIsNotNone(session.pid)
            deadline = time.monotonic() + 10
            while not session.task_sent and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(session.task_sent, "the task was not sent to the isolated fake agent")
            deadline = time.monotonic() + 10
            def received():
                return bytes(self.ph.host.sessions[session.id].buf).replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
            while b"got:Keep this task draft" not in received() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertIn(b"got:Keep this task draft", received())
            shot("f01-explicit-session-start")
            # The owner-scoped discard hint must disappear as soon as that launcher closes;
            # editing after the first warning requires confirmation afresh.
            for method in ("cancel", "escape"):
                cli("click", '#actbar [aria-label="New session"]')
                ready('!!document.querySelector("#new-session-task")')
                cli("fill", "#new-session-task", "Temporary discard fixture")
                def dismiss():
                    if method == "escape":
                        cli("press", "Escape")
                    else:
                        cli("eval", '[...document.querySelectorAll(".workdlg button")].find(b=>b.textContent==="Cancel").click();true')
                dismiss()
                ready('[...document.querySelectorAll(".toast")].some(n=>n.textContent.includes("discard"))')
                self.assertTrue(cli("eval", '!!document.querySelector("#new-session-task")'))
                cli("fill", "#new-session-task", "Edited discard fixture")
                self.assertFalse(cli("eval", '[...document.querySelectorAll(".toast")].some(n=>n.textContent.includes("discard"))'))
                dismiss()
                self.assertTrue(cli("eval", '!!document.querySelector("#new-session-task")'))
                dismiss()
                ready('!document.querySelector("#new-session-task")')
                self.assertFalse(cli("eval", '[...document.querySelectorAll(".toast")].some(n=>n.textContent.includes("discard"))'))
            self.assertEqual(len(manager.sessions), 1)
        finally:
            cli("close")

    def test_http_creation_poll_and_cancel_are_gui_only(self):
        with open(common.TOKENS_FILE, "a") as f:
            f.write("project-create-scoped\tsession:s1\thook\n")
        with mock.patch.object(projects, "create_start") as start, mock.patch.object(projects, "create_cancel") as cancel:
            for token in (common.own_token("tty"), "project-create-scoped"):
                self.assertEqual(post("/api/projects/create", {"mode": "empty", "path": self.destination()}, token)[0], 403)
                self.assertEqual(get("/api/projects/create/missing", token=token)[0], 403)
                self.assertEqual(post("/api/projects/create/missing/cancel", {}, token)[0], 403)
            start.assert_not_called()
            cancel.assert_not_called()
        self.assertEqual(get("/api/projects/create/missing", token=self.token)[0], 404)
        self.assertEqual(post("/api/projects/create/missing/cancel", {}, self.token)[0], 404)


if __name__ == "__main__":
    unittest.main()
