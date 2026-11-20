"""All projects (sessions.WORKSPACE): one Direct session over every project folder of this computer."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _env
import common
import directruntime
import projects
import remotes
import sessions


def checkout(*parts: str) -> str:
    path = os.path.join(*parts)
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)
    return os.path.realpath(path)


class WorkspaceRootsTest(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(dir=_env.ROOT))
        self.projects_file = os.path.join(self.base, "projects.json")
        patcher = mock.patch.object(projects, "PROJECTS_FILE", self.projects_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, doc):
        with open(self.projects_file, "w") as f:
            json.dump(doc, f)

    def test_project_path_is_the_cwd_and_checkouts_elsewhere_are_added(self):
        pp = os.path.join(self.base, "pp")
        checkout(pp, "acme", "shop")
        other = checkout(self.base, "elsewhere", "development", "app")
        self.write({"globs": [os.path.join(self.base, "elsewhere", "development", "*")]})
        cwd, extra = projects.workspace_roots(pp)
        self.assertEqual(cwd, os.path.realpath(pp))
        self.assertEqual(extra, [other])

    def test_without_project_path_the_common_folder_of_the_checkouts_is_the_cwd(self):
        dev = os.path.join(self.base, "development")
        checkout(dev, "app")
        checkout(dev, "server")
        self.write({"globs": [os.path.join(dev, "*")]})
        cwd, extra = projects.workspace_roots("")
        self.assertEqual((cwd, extra), (os.path.realpath(dev), []))

    def test_never_the_home_folder_or_root_as_cwd(self):
        a = checkout(self.base, "one", "app")
        b = checkout(self.base, "two", "srv")
        self.write({"projects": {"x/app": {"path": a}, "x/srv": {"path": b}}})
        with mock.patch.object(os.path, "expanduser", side_effect=lambda p: self.base if p == "~" else p):
            cwd, extra = projects.workspace_roots("")
        self.assertEqual(cwd, os.path.realpath(os.path.join(self.base, "one", "app")))
        self.assertEqual(extra, [b])

    def test_no_project_folder_is_refused(self):
        self.write({})
        with self.assertRaises(ValueError):
            projects.workspace_roots("")


class WorkspaceCreateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def setUp(self):
        self.addCleanup(_env.clear_sessions, self.m)
        patcher = mock.patch.object(sessions.Manager, "_start", lambda self, s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        _env.make_project("ws-a", {"README": "a"})
        subprocess.run(["git", "init", "-q", os.path.join(_env.PP, "team-x", "ws-a")], check=True)

    def test_starts_direct_over_the_project_path_by_default(self):
        s = self.m.create(sessions.WORKSPACE, "find log4j", agent="codex")
        self.assertEqual((s.kind, s.project, s.runtime, s.agent), ("workspace", "workspace", "direct", "codex"))
        self.assertEqual(s.path, os.path.realpath(_env.PP))
        self.assertEqual(s.title, "All projects")
        self.assertEqual(self.m._mcp_role(s), "workspace")
        self.assertEqual(s.opts, {})

    def test_hole_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Direct only"):
            self.m.create(sessions.WORKSPACE, runtime="hole")

    def test_an_agent_cannot_start_it(self):
        for origin in ("advisor", "handoff", "inbox", "prod"):
            with self.assertRaisesRegex(ValueError, "by the user only"):
                self.m.create(sessions.WORKSPACE, origin=origin)

    def test_no_issue_parent_or_start_options(self):
        with self.assertRaisesRegex(ValueError, "new conversation only"):
            self.m.create(sessions.WORKSPACE, issue={"gl": "acme/shop", "iid": 1, "type": "issue"})
        with self.assertRaises(ValueError):
            self.m.create(sessions.WORKSPACE, opts={"docker": True})
        s = self.m.create(sessions.WORKSPACE)
        s.state = "exited"
        with self.assertRaisesRegex(ValueError, "no Hole start options"):
            self.m.set_start_opt(s.id, "docker", True)


class WorkspaceRestartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def test_a_hole_restart_is_blocked(self):
        s = sessions.Session(kind="workspace", project=sessions.WORKSPACE, path=_env.PP, runtime="hole")
        self.addCleanup(__import__("shutil").rmtree, s.dir, True)
        with mock.patch.object(self.m, "_start_direct") as direct, mock.patch.object(self.m, "_set") as setter:
            self.m._start(s)
        direct.assert_not_called()
        self.assertEqual(setter.call_args[0][1], "blocked")
        self.assertIn("Direct only", setter.call_args[0][3])


class WorkspaceDirectTest(unittest.TestCase):
    def session(self, agent):
        s = sessions.Session(kind="workspace", project=sessions.WORKSPACE, path=_env.PP, agent=agent, runtime="direct")
        s.claude_mode = "none" if agent == "codex" else "host"
        self.addCleanup(__import__("shutil").rmtree, s.dir, True)
        return s

    def prepare(self, s, dirs):
        with mock.patch.object(directruntime.shutil, "which", return_value="/usr/bin/fake-agent"):
            return directruntime.prepare(s, "workspace", "", add_dirs=dirs)[0]

    def test_claude_gets_each_root_joined_so_the_task_is_not_swallowed(self):
        s = self.session("claude")
        s.task = "scan"
        argv = self.prepare(s, ["/srv/a", "/srv/b"])
        self.assertIn("--add-dir=/srv/a", argv)
        self.assertIn("--add-dir=/srv/b", argv)
        self.assertEqual(argv[-2:], ["--", "scan"])

    def test_codex_gets_each_root(self):
        argv = self.prepare(self.session("codex"), ["/srv/a"])
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/srv/a")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")


class WorkspaceToolsTest(unittest.TestCase):
    def tools(self, role):
        script = os.path.join(os.path.dirname(sessions.__file__), "sandbox", "piper-mcp.py")
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        out = subprocess.run([sys.executable, script, "--role", role, "--mailbox", tempfile.mkdtemp(dir=_env.ROOT)],
                             input=req.encode(), capture_output=True, timeout=20, check=True).stdout
        return {t["name"] for t in json.loads(out.splitlines()[0])["result"]["tools"]}

    def test_no_project_git_http_db_or_orchestration_tools(self):
        names = self.tools("workspace")
        self.assertIn("piper_session", names)
        self.assertIn("quiet_status", names)
        for name in ("git_commit", "git_push", "http_run", "db_query", "read_file", "edit_file", "spawn_session",
                     "gitlab_upload_file", "piper_allow_domain", "piper_worktree_stack"):
            self.assertNotIn(name, names)

    def test_the_session_role_is_unchanged(self):
        self.assertTrue({"git_commit", "http_run", "db_query", "read_file"} <= self.tools("session"))


class WorkspaceStartDirectTest(unittest.TestCase):
    """_start_direct itself: the roots are recomputed and reach the agent as --add-dir and as instructions."""
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def test_roots_reach_prepare_and_the_spawn_cwd(self):
        self.addCleanup(_env.clear_sessions, self.m)
        _env.make_project("ws-b", {"README": "b"})
        subprocess.run(["git", "init", "-q", os.path.join(_env.PP, "team-x", "ws-b")], check=True)
        outside = checkout(tempfile.mkdtemp(dir=_env.ROOT), "elsewhere", "app")
        s = sessions.Session(kind="workspace", project=sessions.WORKSPACE, path="/stale", agent="claude", runtime="direct")
        s.task = "scan 100% of the checkouts"
        self.addCleanup(shutil.rmtree, s.dir, True)
        calls = {}

        def prepare(sess, role, extra, add_dirs=None):
            calls.update(role=role, extra=extra, add_dirs=add_dirs)
            return ["/usr/bin/fake-agent", "--", sess.task], {}

        pty = mock.Mock()
        pty.call.return_value = {"ok": True, "pid": 4242}
        with mock.patch.object(projects, "workspace_roots", return_value=(os.path.realpath(_env.PP), [outside])) as roots, \
                mock.patch.object(directruntime, "prepare", side_effect=prepare), mock.patch.object(self.m, "pty", pty):
            self.m._start_direct(s)
        roots.assert_called_once()
        self.assertEqual(calls["role"], "workspace")
        self.assertEqual(calls["add_dirs"], [outside])
        self.assertIn("- " + os.path.realpath(_env.PP), calls["extra"])
        self.assertIn("- " + outside, calls["extra"])
        self.assertIn("All projects", calls["extra"])
        spawn = next(c for c in pty.call.call_args_list if c.args[0] == "spawn")
        self.assertEqual(spawn.kwargs["cwd"], os.path.realpath(_env.PP))
        self.assertEqual(s.path, os.path.realpath(_env.PP))
        self.assertEqual(s.pid, 4242)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class WorkspaceSshOnlyPeerTest(unittest.TestCase):
    """SSH-only server: the peer advertises All projects and starts it in its project folder, Direct only."""
    def test_peer_offers_and_starts_all_projects(self):
        home = Path(tempfile.mkdtemp(prefix="ws-peer-", dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, home, True)
        (home / "projects" / "repo" / ".git").mkdir(parents=True)
        binary = home / "bin"
        binary.mkdir()
        agent = binary / "claude"
        agent.write_text("#!/bin/sh\nsleep 30\n")
        agent.chmod(0o700)
        env = dict(os.environ, HOME=str(home), PATH=str(binary) + ":" + os.environ["PATH"], TMUX_TMPDIR=str(home))
        self.addCleanup(lambda: subprocess.run(["tmux", "-L", "piper-ssh", "kill-server"], env=env,
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        def request(method, path, body=None):
            req = dict(op="http", mode="ssh", project_path=str(home / "projects"), method=method, path=path,
                       body=base64.b64encode(json.dumps(body or {}).encode()).decode(), ctype="application/json")
            p = subprocess.run([sys.executable, "-c", remotes.peer_source()], input=json.dumps(req).encode(),
                               capture_output=True, env=env, timeout=15)
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            r = json.loads(p.stdout)
            return r["status"], json.loads(base64.b64decode(r["body"]))

        self.assertTrue(request("GET", "/api/state")[1]["workspace_session"])
        self.assertEqual(request("POST", "/api/sessions", dict(project="workspace", runtime="hole"))[0], 400)
        code, row = request("POST", "/api/sessions", dict(project="workspace", agent="claude", runtime="direct", task="scan"))
        self.assertEqual(code, 200, row)
        self.assertEqual((row["kind"], row["project"], row["title"]), ("workspace", "workspace", "All projects"))
        self.assertEqual(row["path"], os.path.realpath(home / "projects"))
        self.assertEqual(request("POST", "/api/sessions/%s/stop" % row["id"])[0], 200)


if __name__ == "__main__":
    unittest.main()
