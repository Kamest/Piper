"""First-session demo: isolated homes, local git only, and a stand-in HTTP daemon."""
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

import _env  # noqa: F401  (must precede Piper imports)
import common
import demo
import guard
import projects
import sessions
import ttyattach

SK = Path(common.SK)


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT)).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.root = self.tmp / "projects"
        self.root.mkdir()
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.project = self.root / demo.NAME
        self.env = {"HOME": str(self.home), "PROJECT_PATH": str(self.root),
                    "PIPER_HOME": str(self.state), "PIPER_REAL_HOME": str(self.home),
                    "PIPER_NO_EXTENSIONS": "1", "PIPER_EXTENSIONS": "",
                    "XDG_CONFIG_HOME": str(self.tmp / "config")}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        for name, value in (("PIPER_HOME", str(self.state)), ("PROJECTS_FILE", str(self.state / "projects.json")),
                            ("TOKENS_FILE", str(self.state / "tokens"))):
            patch = mock.patch.object(common, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(projects, "PROJECTS_FILE", str(self.state / "projects.json"))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.dict(projects._DISCOVERED, {"at": 0.0, "root": None, "items": [], "skipped": []})
        patch.start()
        self.addCleanup(patch.stop)

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = demo.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def stub_opener(self):
        original = subprocess.run
        patch = mock.patch.object(demo.subprocess, "run", side_effect=lambda args, **kw:
                                  types.SimpleNamespace(returncode=0) if args[0] == "sh" else original(args, **kw))
        run = patch.start()
        self.addCleanup(patch.stop)
        return run

    def start_stub(self, cached=False, reject=False, key=demo.NAME):
        self.calls = []
        calls = self.calls
        root, project = self.root, self.project

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body, status=200):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                calls.append(("GET", self.path, self.headers.get("Authorization"), None))
                if self.path == "/piper/icon.svg":
                    self.reply({})
                elif self.path == "/api/state":
                    self.reply({"projects": [] if cached else [{"key": key, "path": str(project)}]})
                elif self.path == "/api/settings":
                    self.reply({"sections": [{"id": "env", "fields": [{"key": "project_path", "value": str(root)}]}]})
                else:
                    self.reply({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append(("POST", self.path, self.headers.get("Authorization"), body))
                self.reply({"error": "stub refusal"} if reject else {"id": "demo-session", "path": str(project)},
                           409 if reject else 202)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        patch = mock.patch.object(ttyattach, "BASE", base)
        patch.start()
        self.addCleanup(patch.stop)
        return base, server.server_address[1]

    def test_project_origin_initial_commit_and_test_before_and_after_fix(self):
        project = demo.prepare()
        marker = json.loads((project / demo.MARKER).read_text())
        origin = Path(marker["origin"])
        self.assertNotIn(self.root, origin.parents)
        self.assertIn(self.state, origin.parents)
        self.assertEqual(demo.git(origin, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(demo.git(project, "remote", "get-url", "origin"), str(origin))
        self.assertEqual(demo.git(project, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(demo.git(project, "status", "--porcelain"), "")
        self.assertTrue((project / "README.md").is_file())
        self.assertEqual(projects.find(demo.NAME)["path"], str(project))
        self.assertEqual(list(projects.load()), [demo.NAME])
        args = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
        broken = subprocess.run(args, cwd=project, capture_output=True, text=True, timeout=20)
        self.assertEqual(broken.returncode, 1, broken.stderr)
        self.assertIn("AssertionError: -1 != 5", broken.stderr)
        source = project / "demo_math" / "__init__.py"
        source.write_text(source.read_text().replace("return a - b", "return a + b"))
        fixed = subprocess.run(args, cwd=project, capture_output=True, text=True, timeout=20)
        self.assertEqual(fixed.returncode, 0, fixed.stderr)
        demo.git(project, "add", "demo_math/__init__.py")
        demo.git(project, "commit", "-m", "Fix addition")
        demo.git(project, "push", "-u", "origin", "HEAD")
        self.assertEqual(demo.git(origin, "rev-parse", "refs/heads/main"), demo.git(project, "rev-parse", "HEAD"))

    def test_rerun_keeps_user_work_commits_and_origin(self):
        project = demo.prepare()
        source = project / "demo_math" / "__init__.py"
        source.write_text(source.read_text().replace("return a - b", "return a + b"))
        demo.git(project, "add", "demo_math/__init__.py")
        demo.git(project, "commit", "-m", "Keep the fix")
        (project / "notes.txt").write_text("Keep my notes.\n")
        before = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        origin = (project / demo.MARKER).read_bytes()
        self.assertEqual(demo.prepare(), project)
        after = {p.relative_to(project): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((project / demo.MARKER).read_bytes(), origin)
        self.assertEqual(len(list((self.state / "demo-remotes").iterdir())), 1)

    def test_refuses_foreign_directory_without_writes(self):
        self.project.mkdir()
        keep = self.project / "keep.txt"
        keep.write_text("Existing project.\n")
        code, _, err = self.invoke("--no-start")
        self.assertEqual(code, 1)
        self.assertIn("not a marked Piper demo", err)
        self.assertEqual(keep.read_text(), "Existing project.\n")
        self.assertEqual(list(self.project.iterdir()), [keep])
        self.assertFalse((self.state / "demo-remotes").exists())

    def test_refuses_symlink_and_changed_remote(self):
        other = self.tmp / "other"
        other.mkdir()
        self.project.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.invoke("--no-start")[0], 1)
        self.assertEqual(list(other.iterdir()), [])
        self.project.unlink()
        demo.prepare()
        demo.git(self.project, "remote", "set-url", "origin", "https://example.invalid/foreign.git")
        code, _, err = self.invoke("--no-start")
        self.assertEqual(code, 1)
        self.assertIn("local origin does not match", err)

    def test_refuses_an_external_push_url_even_with_local_fetch_url(self):
        demo.prepare()
        demo.git(self.project, "config", "remote.origin.pushurl", "https://example.invalid/foreign.git")
        code, _, err = self.invoke("--no-start")
        self.assertEqual(code, 1)
        self.assertIn("local origin does not match", err)

    def test_origin_must_be_outside_discovery_roots(self):
        for data, state in (({}, self.root / "state"),
                            ({"globs": [str(self.tmp / "*")]}, self.state)):
            with self.subTest(data=data):
                (self.state / "projects.json").write_text(json.dumps(data))
                with mock.patch.object(common, "PIPER_HOME", str(state)):
                    self.assertEqual(self.invoke("--no-start")[0], 1)
                self.assertFalse(self.project.exists())

    def test_start_request_and_gui_link_for_each_agent(self):
        base, _ = self.start_stub()
        run = self.stub_opener()
        for flags, agent in (((), "claude"), (("--agent", "codex"), "codex")):
            code, out, err = self.invoke(*flags)
            self.assertEqual(code, 0, err)
            post = [c for c in self.calls if c[0] == "POST"][-1]
            self.assertEqual(post[1], "/api/sessions")
            self.assertEqual(post[3], {"project": demo.NAME, "agent": agent, "task": demo.TASK})
            self.assertEqual(projects.find(post[3]["project"])["path"], str(self.project))
            token = (self.state / "gui-token").read_text().strip()
            self.assertEqual(post[2], "Bearer " + token)
            self.assertNotIn(token, out + err)
            self.assertIn("Session started:", out)
            self.assertEqual(run.call_args.args[0], ["sh", str(SK / "scripts/open-url.sh"),
                                                   base + "/?session=demo-session#token=" + token])

    def test_cached_empty_discovery_resolves_using_daemon_root(self):
        self.start_stub(cached=True)
        self.stub_opener()
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertTrue(any(c[1] == "/api/settings" for c in self.calls))
        self.assertEqual([c[3]["project"] for c in self.calls if c[0] == "POST"], [demo.NAME])

    def test_session_uses_the_daemons_registered_key_for_the_exact_path(self):
        self.start_stub(key="examples/demo")
        self.stub_opener()
        code, _, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertEqual([c[3]["project"] for c in self.calls if c[0] == "POST"], ["examples/demo"])

    def test_no_start_has_no_http_token_or_browser_side_effects(self):
        self.start_stub()
        with mock.patch.object(ttyattach, "piperd_up") as probe, mock.patch.object(common, "own_token") as token:
            code, out, err = self.invoke("--no-start", "--agent", "codex")
        self.assertEqual(code, 0, err)
        self.assertTrue(self.project.is_dir())
        self.assertEqual(self.calls, [])
        probe.assert_not_called()
        token.assert_not_called()
        self.assertIn("piper demo --agent codex", out)

    def test_stopped_daemon_prints_next_steps_without_starting_it(self):
        with mock.patch.object(ttyattach, "piperd_up", return_value=False), \
                mock.patch.object(common, "own_token") as token:
            code, out, err = self.invoke("--agent", "codex")
        self.assertEqual(code, 0, err)
        token.assert_not_called()
        self.assertIn("piper open\n  piper demo --agent codex", out)
        self.assertIn("PROJECT_PATH=", out)

    def test_api_refusal_does_not_open_gui_or_destroy_project(self):
        self.start_stub(reject=True)
        run = self.stub_opener()
        code, _, err = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 409", err)
        self.assertTrue((self.project / demo.MARKER).is_file())
        self.assertFalse(any(call.args[0][0] == "sh" for call in run.call_args_list))

    def test_shell_dispatch_and_usage(self):
        _, port = self.start_stub()
        env = dict(os.environ, PIPER_PORT=str(port), BROWSER=shutil.which("true"))
        result = subprocess.run(["sh", str(SK / "scripts/piper.sh"), "demo", "--no-start", "--agent", "codex"],
                                env=env, cwd=self.tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.project / demo.MARKER).exists())
        self.assertEqual(self.calls, [])
        result = subprocess.run(["sh", str(SK / "scripts/piper.sh"), "--help"], env=env,
                                cwd=self.tmp, capture_output=True, text=True, timeout=30)
        self.assertIn("demo [--agent claude|codex] [--no-start]", result.stdout)

    def test_demo_approval_uses_fresh_install_policy_function(self):
        # The suite defaults to legacy policy. Explicitly exercise a fresh install's tree.
        manager = types.SimpleNamespace(policy=guard.load_policy(str(self.state / "missing-policy.json"), tree=True))
        self.assertTrue(sessions.Manager.auto_ok(manager, "git_commit"))
        self.assertFalse(sessions.Manager.auto_ok(manager, "git_push"))   # Recommended: a push asks
        self.assertFalse(sessions.Manager.auto_ok(manager, "allow_domain", {"domain": demo.DOMAIN}))
        self.assertIn("piper_allow_domain", demo.TASK)
        self.assertIn(demo.DOMAIN, demo.TASK)


if __name__ == "__main__":
    unittest.main()
