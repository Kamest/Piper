"""piper attach: picking the target, and "Open in terminal" (Ghostty) without opening anything."""
import json
import os
import stat
import tempfile
import time
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import ttyattach


def state(*sessions, projects=()):
    return {"sessions": list(sessions), "projects": list(projects), "pending": []}


P_APP = {"key": "team-x/app", "title": "app", "path": "/w/team-x/app"}
P_SUB = {"key": "team-x/app-sub", "title": "sub", "path": "/w/team-x/app/modules/sub"}
P_WEB = {"key": "team-y/web", "title": "web", "path": "/w/team-y/web"}


def sess(sid, project="team-x/app", alive=True, kind="project", state_="waiting", **kw):
    return dict({"id": sid, "project": project, "alive": alive, "kind": kind, "state": state_,
                 "title": project, "tty": 0}, **kw)


class ResolveTest(unittest.TestCase):
    def test_session_id_prefix_and_project(self):
        st = state(sess("aaaa1111"), sess("bbbb2222", project="team-y/web"), projects=[P_APP, P_WEB])
        self.assertEqual(ttyattach.resolve(st, "aaaa1111")[0]["id"], "aaaa1111")
        self.assertEqual(ttyattach.resolve(st, "bbbb")[0]["id"], "bbbb2222")          # a unique prefix
        self.assertEqual(ttyattach.resolve(st, "team-y/web")[0]["id"], "bbbb2222")       # the project's one session
        self.assertEqual(ttyattach.resolve(st, "web")[0]["id"], "bbbb2222")           # its last path part

    def test_project_without_a_live_session_starts_one(self):
        st = state(sess("aaaa1111", alive=False, state_="exited"), projects=[P_APP])
        self.assertEqual(ttyattach.resolve(st, "app"), (None, P_APP))

    def test_ended_session_by_id_is_offered_to_continue(self):
        st = state(sess("aaaa1111", alive=False, state_="exited"), projects=[P_APP])
        s, p = ttyattach.resolve(st, "aaaa1111")
        self.assertEqual((s["id"], p), ("aaaa1111", None))

    def test_two_live_sessions_need_an_id(self):
        st = state(sess("aaaa1111"), sess("cccc3333"), projects=[P_APP])
        with self.assertRaises(ttyattach.Fail) as e:
            ttyattach.resolve(st, "app")
        self.assertEqual(e.exception.code, 2)

    def test_ambiguous_and_unknown_names(self):
        st = state(projects=[P_APP, {"key": "team-z/app", "title": "app", "path": "/w/team-z/app"}])
        with self.assertRaises(ttyattach.Fail) as e:
            ttyattach.resolve(st, "app")
        self.assertIn("full key", str(e.exception))
        with self.assertRaises(ttyattach.Fail):
            ttyattach.resolve(st, "nope")

    def test_this_directory_is_the_deepest_project(self):
        st = state(projects=[P_APP, P_SUB])
        self.assertEqual(ttyattach.cwd_project(st, "/w/team-x/app/src")["key"], "team-x/app")
        self.assertEqual(ttyattach.cwd_project(st, "/w/team-x/app/modules/sub/x")["key"], "team-x/app-sub")
        self.assertIsNone(ttyattach.cwd_project(st, "/w/team-x/application"))      # a prefix of the name is not inside

    def test_global_agent(self):
        st = state(sess("adv00001", project="", kind="advisor"))
        self.assertEqual(ttyattach.resolve(st, "advisor")[0]["id"], "adv00001")
        self.assertEqual(ttyattach.resolve(state(), "advisor")[1]["key"], "advisor")

    def test_an_older_piperd_is_named(self):
        old = {"sessions": [{"id": "aaaa1111", "state": "ready", "kind": "project"}], "projects": []}
        with self.assertRaises(ttyattach.Fail) as e:
            ttyattach.live(old)
        self.assertIn("piper restart", str(e.exception))

    def test_prodcheck_is_never_a_target(self):
        st = state(sess("prod0001", kind="prodcheck"), projects=[P_APP])
        self.assertEqual(ttyattach.live(st), [])


class UsageTest(unittest.TestCase):
    def test_help_and_bad_flags(self):
        import subprocess
        import sys
        run = lambda *a: subprocess.run([sys.executable, ttyattach.__file__, *a], capture_output=True, text=True)
        r = run("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("piper attach <project> --new", r.stdout)
        self.assertIn("--take", r.stdout)
        for bad in (["--bogus"], ["a", "b"]):
            r = run(*bad)
            self.assertEqual((r.returncode, "Traceback" in r.stderr), (2, False), bad)


class WatchTest(unittest.TestCase):
    """What piperd knows and the PTY bytes do not show reaches the attached terminal once."""

    def test_confirmation_card_and_notice(self):
        import threading
        polls = [
            state(sess("s1", notice="")),
            state(sess("s1", notice=""), ) | {"pending": [{"id": "p1", "session": "s1", "title": "git push to origin"},
                                                            {"id": "p2", "session": "other", "title": "x"}]},
            state(sess("s1", notice="the task was not sent")) | {"pending": [{"id": "p1", "session": "s1", "title": "git push to origin"}]},
        ]
        a = object.__new__(ttyattach.Attach)
        a.sid, a.done = "s1", threading.Event()

        class Api:
            def poll(self, timeout=5):
                if not polls:
                    a.done.set()
                    return {}
                return polls.pop(0)
        a.api = Api()
        said = []
        a.say = said.append
        written = []
        old, ttyattach.out = ttyattach.out, written.append
        old_wait = a.done.wait
        a.done.wait = lambda t: old_wait(0.01)
        try:
            a.watch()
        finally:
            ttyattach.out = old
        self.assertEqual(said, ["waiting for your confirmation in the GUI: git push to origin - open it: piper",
                                "the task was not sent"])
        self.assertEqual(written, [b"\x07"])   # the bell: Ghostty marks the tab


class OpenTerminalTest(unittest.TestCase):
    """Settings > Terminal: Ghostty / Warp / Terminal - with a fake open(1) that only records."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.apps = {}
        for key in ("ghostty", "warp", "terminal"):
            app = os.path.join(self.tmp, key + ".app")
            os.mkdir(app)
            self.apps[key] = app
        self.rec = os.path.join(self.tmp, "argv.json")
        fake = os.path.join(self.tmp, "open")
        with open(fake, "w") as f:
            f.write("#!%s\nimport json, sys\njson.dump(sys.argv[1:], open(%r, 'w'))\n" % (ttyattach.sys.executable, self.rec))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        self.old = (dict(ttyattach.TERMINALS), ttyattach.TERMINAL_CONFIG, ttyattach.WARP_CONFIG, os.environ["PATH"])
        for key, (name, _) in self.old[0].items():
            ttyattach.TERMINALS[key] = (name, (self.apps[key],))
        ttyattach.TERMINAL_CONFIG = os.path.join(self.tmp, "terminal.json")
        ttyattach.WARP_CONFIG = os.path.join(self.tmp, "warp", "launch_configurations", "piper-attach.yaml")
        os.environ["PATH"] = self.tmp + os.pathsep + os.environ["PATH"]

    def tearDown(self):
        ttyattach.TERMINALS.clear()
        ttyattach.TERMINALS.update(self.old[0])
        ttyattach.TERMINAL_CONFIG, ttyattach.WARP_CONFIG, os.environ["PATH"] = self.old[1:]

    def use(self, app):
        with open(ttyattach.TERMINAL_CONFIG, "w") as f:
            json.dump({"app": app}, f)

    def check_attach(self, text):
        self.assertIn("PIPER_HOME=" + ttyattach.common.PIPER_HOME, text)   # not launchd's defaults
        self.assertIn("%s %s abcd1234 --take" % (ttyattach.sys.executable, os.path.abspath(ttyattach.__file__)), text)

    @unittest.skipUnless(ttyattach.sys.platform == "darwin", "open(1) is macOS only")
    def test_ghostty_is_the_default(self):
        r = ttyattach.open_terminal("abcd1234", "/w/team-x/app")
        self.assertEqual(r, {"ok": True, "command": "piper attach abcd1234", "terminal": "Ghostty"})
        argv = json.load(open(self.rec))
        self.assertEqual(argv[:6], ["-na", self.apps["ghostty"], "--args", "-e", "sh", "-c"])
        self.assertEqual(len(argv), 7)                                    # one string: nothing else reaches Ghostty
        self.assertFalse([a for a in argv if os.path.exists(a) and a != self.apps["ghostty"]])   # macOS would open them as tabs
        self.assertTrue(argv[6].startswith("cd /w/team-x/app "))
        self.assertIn('exec "${SHELL:-/bin/zsh}" -l', argv[6])           # after a detach: a shell in the project
        self.check_attach(argv[6])

    @unittest.skipUnless(ttyattach.sys.platform == "darwin", "open(1) is macOS only")
    def test_terminal_app_opens_a_command_file(self):
        self.use("terminal")
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "Terminal")
        argv = json.load(open(self.rec))
        self.assertEqual(argv[:2], ["-a", self.apps["terminal"]])
        self.assertTrue(argv[2].endswith("/run/attach-abcd1234.command"))
        self.assertEqual(os.stat(argv[2]).st_mode & 0o777, 0o700)
        body = open(argv[2]).read()
        self.assertTrue(body.startswith("#!/bin/sh\ncd /w/team-x/app "))
        self.check_attach(body)

    @unittest.skipUnless(ttyattach.sys.platform == "darwin", "open(1) is macOS only")
    def test_warp_opens_a_launch_configuration(self):
        self.use("warp")
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "Warp")
        self.assertEqual(json.load(open(self.rec)), ["warp://launch/piper-attach"])
        yaml = open(ttyattach.WARP_CONFIG).read()
        self.assertIn("\nname: piper-attach\n", yaml)          # Warp matches the URI against `name:`, not the file
        self.assertIn('cwd: "/w/team-x/app"', yaml)
        self.assertIn("- exec: ", yaml)
        self.check_attach(json.loads(yaml.split("- exec: ", 1)[1].strip()))   # a JSON string is a YAML string

    def test_auto_falls_back_to_terminal_and_a_missing_app_gives_the_command(self):
        os.rmdir(self.apps["ghostty"])
        self.assertEqual(ttyattach.terminal_app(), ("terminal", self.apps["terminal"]))
        if ttyattach.sys.platform == "darwin":   # D4: the GUI's "Attach in <app>"
            self.assertEqual(ttyattach.terminal_name(), "Terminal")
        self.use("ghostty")
        self.assertIsNone(ttyattach.terminal_app())
        self.assertEqual(ttyattach.terminal_name(), "")
        r = ttyattach.open_terminal("abcd1234", "/w")
        self.assertEqual((r["ok"], r["command"]), (False, "piper attach abcd1234"))
        self.assertFalse(os.path.exists(self.rec))


class OpenTerminalElsewhereTest(unittest.TestCase):
    """Linux / WSL: a fake emulator in an otherwise empty PATH records its argv."""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.rec = os.path.join(self.tmp, "argv.json")
        self.cfg = os.path.join(self.tmp, "terminal.json")
        self.patches = [mock.patch.object(ttyattach.sys, "platform", "linux"),
                        mock.patch.object(ttyattach.common, "is_wsl", return_value=False),
                        mock.patch.object(ttyattach, "TERMINAL_CONFIG", self.cfg),
                        # the fakes exit at once, so a long grace costs nothing; 0.5 s failed under the parallel
                        # gate
                        mock.patch.object(ttyattach, "OPEN_GRACE_S", 5.0),
                        mock.patch.object(ttyattach.glob, "glob", return_value=[]),
                        mock.patch.dict(os.environ, {"PATH": self.bin}),
                        mock.patch.object(ttyattach.os.path, "exists",
                                          side_effect=lambda q, _e=os.path.exists: q != "/mnt/c/Windows/System32/cmd.exe" and _e(q))]
        for pt in self.patches:
            pt.start()
        os.environ.pop("TERMINAL", None)
        os.environ.pop("WSL_DISTRO_NAME", None)

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()

    def fake(self, name, body=""):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!%s\nimport json, sys, time\njson.dump(sys.argv, open(%r, 'w'))\n%s\n" % (ttyattach.sys.executable, self.rec, body))
        os.chmod(path, 0o755)
        return path

    def argv(self):
        return json.load(open(self.rec))

    def check_script(self, script):
        self.assertTrue(script.startswith("cd /w/team-x/app "))
        self.assertIn("%s %s abcd1234 --take" % (ttyattach.sys.executable, os.path.abspath(ttyattach.__file__)), script)
        self.assertIn('exec "${SHELL:-/bin/sh}" -l', script)        # no zsh assumed off a Mac

    def test_x_terminal_emulator_gets_the_syntax_of_what_it_points_to(self):
        os.symlink(self.fake("gnome-terminal.wrapper"), os.path.join(self.bin, "x-terminal-emulator"))
        real = self.fake("gnome-terminal")
        self.fake("xterm")                                           # x-terminal-emulator comes first
        r = ttyattach.open_terminal("abcd1234", "/w/team-x/app")
        self.assertEqual(r, {"ok": True, "command": "piper attach abcd1234", "terminal": "gnome-terminal"})
        argv = self.argv()
        self.assertEqual(argv[0], real)            # Debian's wrapper drops `--window --` (seen on Ubuntu 24.04)
        self.assertEqual(argv[1:5], ["--window", "--", "sh", "-c"])
        self.assertEqual(len(argv), 6)
        self.check_script(argv[5])

    def test_terminal_env_wins_and_an_unknown_one_gets_dash_e(self):
        self.fake("konsole")
        self.fake("myterm")
        os.environ["TERMINAL"] = "myterm"
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "myterm")
        self.assertEqual(self.argv()[1:4], ["-e", "sh", "-c"])
        os.environ["TERMINAL"] = "konsole"
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "konsole")
        self.assertEqual(self.argv()[1:4], ["-e", "sh", "-c"])

    def test_known_terminals_in_order(self):
        self.fake("xterm")
        self.fake("kitty")
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "kitty")
        self.assertEqual(self.argv()[1:3], ["sh", "-c"])             # kitty takes the command bare

    def test_settings_ghostty_is_honoured_on_linux(self):
        self.fake("xterm")
        self.fake("ghostty")
        with open(self.cfg, "w") as f:
            json.dump({"app": "ghostty"}, f)
        self.assertEqual(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["terminal"], "ghostty")
        self.assertEqual(self.argv()[1:4], ["-e", "sh", "-c"])

    def test_an_emulator_that_keeps_running_counts_as_opened(self):
        self.fake("xterm", "time.sleep(3)")                         # xterm: the process is the window
        from unittest import mock
        t = time.time()
        with mock.patch.object(ttyattach, "OPEN_GRACE_S", 0.5):
            self.assertTrue(ttyattach.open_terminal("abcd1234", "/w/team-x/app")["ok"])
        self.assertLess(time.time() - t, 2.5)

    def test_a_failing_emulator_says_why_and_leaves_the_command(self):
        self.fake("xterm", "sys.stderr.write('xterm: Xt error: Can\'t open display: \\n'); sys.exit(1)")
        r = ttyattach.open_terminal("abcd1234", "/w")
        self.assertEqual((r["ok"], r["command"]), (False, "piper attach abcd1234"))
        self.assertIn("Can't open display", r["error"])

    def test_no_emulator_gives_the_command(self):
        r = ttyattach.open_terminal("abcd1234", "/w")
        self.assertEqual((r["ok"], r["command"]), (False, "piper attach abcd1234"))
        self.assertIn("no terminal emulator", r["error"])

    def test_wsl_opens_windows_terminal_with_a_script_file(self):
        from unittest import mock
        self.fake("wt.exe")
        self.fake("xterm")                                          # WSLg's xterm is not the one
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu-24.04"
        with mock.patch.object(ttyattach.common, "is_wsl", return_value=True), \
                mock.patch.object(ttyattach.common, "PIPER_HOME", self.tmp):
            r = ttyattach.open_terminal("abcd1234", "/w/team-x/app")
        self.assertEqual(r["terminal"], "Windows Terminal")
        argv = self.argv()
        self.assertEqual(argv[1:8], ["-w", "new", "wsl.exe", "-d", "Ubuntu-24.04", "--", "sh"])
        self.assertEqual(argv[8], os.path.join(self.tmp, "run", "attach-abcd1234.sh"))
        self.assertFalse([a for a in argv if ";" in a])            # wt.exe splits its command line at ;
        self.assertEqual(os.stat(argv[8]).st_mode & 0o777, 0o700)
        self.check_script(open(argv[8]).read().split("\n", 1)[1])

    def test_wsl_without_windows_terminal_uses_cmd_start(self):
        from unittest import mock
        self.fake("cmd.exe")
        with mock.patch.object(ttyattach.common, "is_wsl", return_value=True), \
                mock.patch.object(ttyattach.common, "PIPER_HOME", self.tmp):
            r = ttyattach.open_terminal("abcd1234", "/w/team-x/app")
        self.assertEqual(r["terminal"], "a Windows console")
        self.assertEqual(self.argv()[1:7], ["/c", "start", "", "wsl.exe", "--", "sh"])

if __name__ == "__main__":
    unittest.main()
