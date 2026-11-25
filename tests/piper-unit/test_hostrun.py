"""host_run: a session asks for one command on the host; the human allows it on a red card every time; it runs in
the session's host shell beside it; Piper's own tools come first (hostrun.py)."""
import os
import shutil
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import features
import hostrun
import sessioncfg
import sessions
import settings
from test_ptyhost import NEEDS_PTY


def audits(event):
    return [a for a in common.audit_tail(400) if a.get("event") == event]


class CommandTest(unittest.TestCase):
    def test_one_printable_line_without_comment_or_continuation(self):
        self.assertEqual(hostrun.check_command("  open .  "), "open .")
        self.assertEqual(hostrun.check_command("echo 'a # b' \"#c\" x#y ${#v}"), "echo 'a # b' \"#c\" x#y ${#v}")
        for bad in ("", "   ", None, 3, "ls\nrm -rf x", "ls\rx", "echo \x1b[2J", "echo ‮x", "ls # gone",
                    "ls;#x", "echo 'open", "echo \"open", "ls \\", "x" * (hostrun.MAX_CMD + 1)):
            with self.assertRaises(hostrun.Refused, msg=repr(bad)):
                hostrun.check_command(bad)

    def test_the_router_sees_through_wrappers_shells_and_substitutions(self):
        cases = {
            "git push": {"git"}, "/usr/bin/git log": {"git"}, "sudo -u me git fetch": {"git"},
            "env A=1 B=2 mysql -e 'select 1'": {"db"}, "FOO=1 psql x": {"db"}, "bash -lc 'redis-cli ping'": {"db"},
            "echo $(curl -s example.com)": {"http"}, "x=`wget -qO- example.com`": {"http"},
            "docker compose up -d": {"stack"}, "docker-compose ps": {"stack"}, "glab mr list": {"gitlab"},
            "gh pr view 1": {"github"}, "cat .env | head -3": {"files"}, "timeout 5 nice -n 3 git status": {"git"},
            "xargs -I{} git rm {} < list": {"git"}, "eval 'git status'": {"git"},
            "make test && git commit -m x": {"git"}, "ls > git": set(),
            "open .": set(), "task piper:test": set(), "brew upgrade": set(), "docker ps": set(),
            "tail -f /var/log/system.log": set(), "uname -sm && ls -1 | head -3": set(), "x 2>&1 | head": set(),
            "task piper:test 2>&1 | head -n 40": set(), "head -n 5 notes.txt": {"files"}, "python3 scripts/piper-sandbox.py --selftest": set(),
        }
        for cmd, want in cases.items():
            self.assertEqual(hostrun.categories(cmd), want, cmd)

    def test_the_typed_line_the_marker_and_the_output(self):
        line = hostrun.typed_line("make x", "ab12", "/bin/zsh")
        self.assertEqual(line, " printf '\\033]133;C;piper-ab12\\007'; eval 'make x'; "
                               "printf '\\033]133;D;%s;piper-ab12\\007' \"$?\"\r")
        self.assertIn("\"$status\"", hostrun.typed_line("ls", "ab12", "/opt/homebrew/bin/fish"))
        self.assertIn("; command eval 'echo '\\''a'\\'''; printf", hostrun.typed_line("echo 'a'", "n", "/bin/sh"))
        self.assertIn(r"; eval 'echo \'a\\\''; printf", hostrun.typed_line("echo 'a\\'", "n", "/usr/bin/fish"))
        # dash echoes a typed-ahead line, then prints its prompt: the output starts after the start marker
        dash = (b" printf '\\033]133;C;piper-ab12\\007'; eval 'x'; printf '...piper-ab12...'\r\n$ "
                b"\x1b]133;C;piper-ab12\x07a\r\nb\r\n")
        self.assertEqual(hostrun.output_text(dash, "ab12")[0], "a\nb")
        raw = (b"\x1b[1m$\x1b[0m  make x; printf '\\033]133;D;%s;piper-ab12\\007' \"$?\"\r\n"
               b"building\r\n50%\r100%\r\ntoken=glpat-abcdefghijklmnopqrstu\r\n")
        m = hostrun.marker("ab12").search(raw + b"\x1b]133;D;2;piper-ab12\x07$ ")
        self.assertEqual(m.group(1), b"2")
        self.assertIsNone(hostrun.marker("ab12").search(raw))   # the echoed line names the nonce but is no marker
        text, cut = hostrun.output_text(raw, "ab12")
        self.assertEqual(text.split("\n")[:2], ["building", "100%"])
        self.assertNotIn("glpat-abcdefghijklmnopqrstu", text)
        self.assertFalse(cut)
        text, cut = hostrun.output_text(b"echo piper-n\r\n" + b"line\n" * 9000, "n")
        self.assertTrue(cut)
        self.assertLessEqual(len(text), hostrun.OUT_TAIL)


class ModuleTest(unittest.TestCase):
    def test_opt_in_needs_host_shells_and_no_agent_may_switch_it_on(self):
        self.assertIn("host_run", features.IDS)
        self.assertIn("host_run", features.NO_PRESET)
        for name, mods in features.PRESETS.items():
            self.assertNotIn("host_run", mods, name)
        self.assertIn(("features", "host_run"), settings.AGENT_DENY)
        with self.assertRaises(settings.SettingsError):
            settings.agent_preview("features", {"host_run": True})
        for host, run, want in ((True, True, True), (False, True, False), (True, False, False)):
            _env.set_modules(self, host=host, host_run=run)
            self.assertEqual(hostrun.enabled(), want)
            self.assertEqual(sessioncfg.host_run_flag("session"), ["--host-run"] if want else [])
            self.assertEqual(sessioncfg.host_run_flag("advisor"), [])

    def test_the_mcp_lists_the_tool_only_with_its_flag(self):
        import importlib.util
        import io
        import json
        spec = importlib.util.spec_from_file_location("piper_mcp_t", os.path.join(
            os.path.dirname(hostrun.__file__), "sandbox", "piper-mcp.py"))
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        self.assertIn("host_run", mcp.CONFIRMED)   # a card: the 15 min wait

        def names(*argv):
            out = io.StringIO()
            with mock.patch("sys.argv", ["piper-mcp.py", *argv]), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")), \
                    mock.patch("sys.stdout", out):
                mcp.main()
            return {t["name"] for t in json.loads(out.getvalue().splitlines()[0])["result"]["tools"]}
        self.assertIn("host_run", names("--role", "session", "--host-run"))
        self.assertNotIn("host_run", names("--role", "session"))
        self.assertNotIn("host_run", names("--role", "advisor", "--host-run"))


class FlowTest(unittest.TestCase):
    """A real Manager and its host shells with a scripted ptyhost: the router, the card, the run."""

    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def setUp(self):
        _env.set_modules(self, host=True, host_run=True)
        self.root = _env.make_project("hostrun-%s" % os.urandom(3).hex(), {"README.md": "x\n"})
        self.key = "team-x/" + os.path.basename(self.root)
        _env.register(self.key, self.root)
        self.s = sessions.Session(kind="project", project=self.key, path=self.root, state="ready", title="app")
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(self.m.sessions.pop, self.s.id, None)
        self.typed = []
        self.answer = {"rc": 0, "out": b"hello\r\n"}
        test = self

        class Pty:
            def __init__(self):
                self.alive = {}

            def call(self, op, **kw):
                if op == "spawn":
                    self.alive[kw["id"]] = 77
                    return {"ok": True, "pid": 77}
                if op == "list":
                    return {"sessions": [{"id": k, "alive": True, "pid": v, "bytes": 0} for k, v in self.alive.items()]}
                if op == "pending":
                    return {"ok": True, "key_at": 0.0, "enter_at": 0.0}
                if op == "input":
                    import base64
                    line = base64.b64decode(kw["data"]).decode()
                    test.typed.append((kw["id"], line, kw.get("auto")))
                    nonce = line.split("piper-")[1].split("\\")[0]
                    out = line.encode().replace(b"\r", b"\r\n") + test.answer["out"] + \
                        b"\x1b]133;D;%d;piper-%s\x07" % (test.answer["rc"], nonce.encode())
                    threading.Timer(0.05, lambda: test.m.hosts.on_event(
                        {"ev": "data", "id": kw["id"], "data": base64.b64encode(out).decode()})).start()
                return {"ok": True}
        self.pty = Pty()
        patches = [mock.patch.object(self.m, "pty", self.pty, create=True),
                   mock.patch.object(self.m, "_host_fg_idle", lambda pid, info=None: True),
                   mock.patch.object(self.m, "policy", {**self.m.policy, "ask": [], "auto_approve": ["host_run"]})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.m.hosts._pty = lambda: self.pty

    def run_tool(self, args, decide=None):
        """host_run in a thread; `decide(card)` answers its card. Returns (result, error, card)."""
        box = {}

        def go():
            try:
                box["result"] = self.m.tool(self.s, "host_run", args)
            except ValueError as e:
                box["error"] = str(e)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        card = None
        deadline = time.time() + 5
        while decide and card is None and time.time() < deadline and t.is_alive():
            card = next((p for p in self.m.pending_list() if p["kind"] == "host_run" and p["session"] == self.s.id), None)
            time.sleep(0.02)
        if card is not None:
            decide(card)
        t.join(10)
        self.assertFalse(t.is_alive())
        return box.get("result"), box.get("error"), card

    def test_off_means_no_card(self):
        _env.set_modules(self, host=True, host_run=False)
        _r, err, card = self.run_tool({"command": "open .", "reason": "show it"}, decide=lambda c: None)
        self.assertIn("host_run is off", err)
        self.assertIsNone(card)
        self.assertEqual(self.typed, [])

    def test_a_covered_command_needs_its_piper_tool_first_and_a_reason(self):
        _r, err, card = self.run_tool({"command": "git push --force", "reason": "push"}, decide=lambda c: None)
        self.assertIn("git -> git_run", err)
        self.assertIsNone(card)
        self.assertEqual(audits("host-run-routed")[0]["categories"], ["git"])
        self.m.note_tool_use(self.s, "git_status")
        _r, err, _c = self.run_tool({"command": "git push --force", "reason": "push"})
        self.assertIn("why_no_tool is required", err)
        _r, err, card = self.run_tool({"command": "git push --force", "reason": "push",
                                       "why_no_tool": "git_run refuses --force"},
                                      decide=lambda c: self.m.decide(c["id"], False))
        self.assertIn("declined", err)
        self.assertEqual(card["payload"]["tried"], ["git_status"])
        self.assertIn("git_run refuses --force", card["summary"])
        self.assertEqual(self.typed, [])

    def test_every_command_is_a_red_card_no_grant_skips_it(self):
        """Security off for host_run, a session grant, a late grant: the card still comes, Allow always is no grant."""
        self.s.approvals.append(self.m.approval_scope("host_run", {"danger": True}))
        _r, err, card = self.run_tool({"command": "open .", "reason": "show the folder"},
                                      decide=lambda c: self.m.decide(c["id"], False))
        self.assertIsNotNone(card)
        self.assertNotIn("session_always", card)
        self.assertEqual(card["always"], hostrun.KEEP_HERE)
        self.assertTrue(card["payload"]["danger"])
        self.assertEqual((card["payload"]["command"], card["payload"]["cwd"]), ("open .", self.root))
        self.assertIn("declined", err)

    def test_allowed_it_runs_in_the_sessions_shell_and_reuses_it(self):
        res, err, card = self.run_tool({"command": "open .", "reason": "show the folder"},
                                       decide=lambda c: self.m.decide(c["id"], True))
        self.assertIsNone(err)
        self.assertEqual((res["rc"], res["output"]), (0, "hello"))
        tid, line, auto = self.typed[0]
        self.assertTrue(auto)   # piperd's own input, never the human's
        self.assertIn(" eval 'open .'; printf '\\033]133;D;%s;piper-", line)
        item = self.m.hosts.get(tid)
        self.assertEqual((item["session"], item["path"]), (self.s.id, os.path.realpath(self.root)))
        self.assertTrue(item["run_at"])
        a = audits("host-run")[0]
        self.assertEqual((a["command"], a["rc"], a["output_to_agent"], a["terminal"]), ("open .", 0, True, tid))
        self.assertNotIn("hello", str(a))   # never the output
        # the next command: the same shell, its exit code, the output kept from the agent on request
        self.answer = {"rc": 3, "out": b"secret stuff\r\n"}
        res, err, _c = self.run_tool({"command": "ls ~/.ssh", "reason": "list"},
                                     decide=lambda c: self.m.decide(c["id"], True, always=True))
        self.assertEqual((res["rc"], "output" in res, bool(res.get("output_withheld"))), (3, False, True))
        self.assertEqual(self.typed[1][0], tid)
        self.assertEqual(self.s.approvals.count(self.m.approval_scope("host_run", {"danger": True})), 0)
        self.assertFalse(audits("host-run")[0]["output_to_agent"])

    def test_a_busy_shell_is_waited_for_then_refused(self):
        self.run_tool({"command": "true", "reason": "open the shell"}, decide=lambda c: self.m.decide(c["id"], True))
        with mock.patch.object(hostrun, "IDLE_WAIT", 1), mock.patch.object(self.m, "_host_fg_idle", lambda pid, info=None: False):
            _r, err, _c = self.run_tool({"command": "true", "reason": "x"}, decide=lambda c: self.m.decide(c["id"], True))
        self.assertIn("a command is still running", err)
        self.assertEqual(len(self.typed), 1)
        orig = self.pty.call
        with mock.patch.object(hostrun, "IDLE_WAIT", 1), mock.patch.object(
                self.pty, "call", lambda op, **kw: {"ok": True, "key_at": time.time() - 9, "enter_at": time.time() - 20}
                if op == "pending" else orig(op, **kw)):
            _r, err, _c = self.run_tool({"command": "true", "reason": "x"}, decide=lambda c: self.m.decide(c["id"], True))
        self.assertIn("unsent text", err)
        self.assertEqual(len(self.typed), 1)


@NEEDS_PTY
class RealShellTest(unittest.TestCase):
    """The typed line and its marker in real shells, through the in-thread ptyhost."""

    def test_the_marker_gives_the_exit_code_and_the_output(self):
        m = _env.manager(self, pty=True)
        m.pty = self.ph.client(on_event=m._on_pty_event)   # as piperd's control connection: events reach the Manager
        self.addCleanup(m.pty.close)
        _env.set_modules(self, host=True, host_run=True)
        root = _env.make_project("hostrun-real-%s" % os.urandom(3).hex(), {"README.md": "x\n"})
        key = "team-x/" + os.path.basename(root)
        _env.register(key, root)
        s = sessions.Session(kind="project", project=key, path=root, state="ready", title="app")
        m.sessions[s.id] = s
        self.addCleanup(m.sessions.pop, s.id, None)
        shells = [x for x in ("/bin/sh", "/bin/dash", "/bin/bash", "/bin/zsh") if os.access(x, os.X_OK)]   # dash: CI's /bin/sh

        def run(m, s, cmd):
            out = {}

            def go():
                try:
                    out["r"] = m.tool(s, "host_run", {"command": cmd, "reason": "test"})
                except ValueError as e:
                    out["e"] = str(e)
            t = threading.Thread(target=go, daemon=True)
            t.start()
            deadline = time.time() + 10
            card = None
            while card is None and time.time() < deadline:
                card = next((p for p in m.pending_list() if p["kind"] == "host_run"), None)
                time.sleep(0.05)
            self.assertIsNotNone(card, cmd)
            m.decide(card["id"], True)
            t.join(60)
            self.assertNotIn("e", out, out.get("e"))
            return out["r"]

        def lines(*want):
            return lambda r: self.assertEqual(r["output"].split("\n")[-len(want):], list(want), r)

        def failed(r):   # interactive bash 3.2 reports rc 0 after a syntax error in eval: the message is the proof
            self.assertRegex(r["output"], r"(?i)syntax error|parse error", r)
        # the eval wrapper: a trailing ; or &, an unterminated construct or a here-document never leaves the shell
        # at a continuation prompt (the next command would be appended to it) and never loses the marker
        cases = [("printf 'a\\nb\\n'; ls README.md; (exit 4)", lambda r: (self.assertEqual(r["rc"], 4, r),
                                                                         lines("a", "b", "README.md")(r))),
                 ("echo semi;", lines("semi")),
                 ("sleep 0 &", lambda r: self.assertEqual(r["rc"], 0, r)),
                 ("for x in a; do echo $x", failed),
                 ("( echo open", failed),
                 ("echo tail |", failed),
                 ("cat <<EOF", lambda r: None),
                 ("echo 'it'\\''s' after", lines("it's after"))]
        for sh in shells:
            with self.subTest(shell=sh), mock.patch.dict(os.environ, {"SHELL": sh}), \
                    mock.patch.object(hostrun, "IDLE_WAIT", 20), mock.patch.object(hostrun, "CALL_BUDGET", 45), \
                    mock.patch.object(m, "policy", {**m.policy, "ask": []}):
                for item in list(m.hosts.items):
                    m.hosts.close(item["id"])
                for cmd, check in cases:
                    r = run(m, s, cmd)
                    self.assertNotIn("running", r, (sh, cmd, r))   # the marker came: the shell is back at its prompt
                    self.assertIsInstance(r["rc"], int, (sh, cmd, r))
                    check(r)
                self.assertEqual(len(m.hosts.items), 1)   # every command in the one shell


    def test_ptyhost_says_when_a_job_holds_the_shell(self):
        import base64
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        pc = ph.client()
        self.addCleanup(pc.close)
        import hostterm
        ht = hostterm.HostTerminals(lambda: pc, lambda: None, lambda: True,
                                    os.path.join(common.PIPER_HOME, "host-terminals-fg.json"))
        with mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            item = ht.open({"key": "k", "title": "t", "path": common.PIPER_HOME})
        self.addCleanup(ht.close, item["id"])

        def fg():
            return next(x for x in pc.call("list")["sessions"] if x["id"] == item["id"])["shell_in_front"]
        deadline = time.time() + 10
        while fg() is not True and time.time() < deadline:
            time.sleep(0.1)
        self.assertIs(fg(), True)   # at its prompt
        pc.call("input", id=item["id"], data=base64.b64encode(b"sleep 2\n").decode())
        deadline = time.time() + 5
        while fg() is not False and time.time() < deadline:
            time.sleep(0.05)
        self.assertIs(fg(), False)  # a job in front
        deadline = time.time() + 10
        while fg() is not True and time.time() < deadline:
            time.sleep(0.1)
        self.assertIs(fg(), True)


if __name__ == "__main__":
    unittest.main()
