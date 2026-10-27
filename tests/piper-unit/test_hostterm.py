"""W2 host terminal: opt-in, the GUI's only, audited, a clean environment."""
import base64
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import hostterm
import piperd
import settings
import wsproto
from test_piperd import HOST, FakeManager, get, post, serve, ws_frame, ws_read
from test_ptyhost import NEEDS_PTY


class FakePty:
    def __init__(self):
        self.calls, self.alive = [], {}

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "spawn":
            self.alive[kw["id"]] = True
            return {"ok": True, "pid": 4242}
        if op == "list":
            return {"sessions": [{"id": k, "alive": v} for k, v in self.alive.items()]}
        return {"ok": True}


def make(policy=None, pty=None):
    pty = pty or FakePty()
    bumps = []
    pol = {"host": True} if policy is None else policy
    path = os.path.join(common.PIPER_HOME, "host-terminals-%s.json" % os.urandom(3).hex())
    return hostterm.HostTerminals(lambda: pty, lambda: bumps.append(1), lambda: pol.get("host"), path), pty, bumps


def project(tmp=None):
    path = tmp or common.PIPER_HOME
    return {"key": "team-x/app", "title": "app", "path": path}


def audits(event):
    """Newest first."""
    return [a for a in common.audit_tail(400) if a.get("event") == event]


class HostTermUnitTest(unittest.TestCase):
    def test_off_by_default_and_refused_while_off(self):
        import features
        self.assertIn("host", features.IDS)
        for name, mods in features.PRESETS.items():
            self.assertNotIn("host", mods, name)   # D10: no preset (not even Everything) opens a host shell
        ht, pty, _ = make(policy={})
        with self.assertRaises(PermissionError):
            ht.open(project())
        self.assertEqual(pty.calls, [])
        self.assertEqual(ht.summary(), {"on": False, "user": hostterm._user(), "items": []})
        self.assertTrue(hostterm._user())   # the GUI's HOST strip says who the shell runs as

    def test_open_spawns_a_login_shell_with_a_clean_environment(self):
        ht, pty, bumps = make()
        secret_env = {"GITLAB_TOKEN": "glpat-x", "GH_TOKEN": "ghp-x", "PIPER_TOKEN": "t", "SHELL": "/bin/sh",
                      "AWS_SECRET_ACCESS_KEY": "a", "USER": "me", "SSH_AUTH_SOCK": "/tmp/agent"}
        with mock.patch.dict(os.environ, secret_env):
            item = ht.open(project(), cols=90, rows=20)
        op, kw = pty.calls[0]
        self.assertEqual(op, "spawn")
        self.assertTrue(kw["id"].startswith("host-") and kw["id"] == item["id"])
        self.assertEqual(kw["argv"], ["/bin/sh", "-l"])
        self.assertEqual(kw["cwd"], os.path.realpath(common.PIPER_HOME))
        self.assertIs(kw["locked"], False)
        self.assertNotIn("log", kw)   # the output never lands on disk
        self.assertEqual((kw["cols"], kw["rows"]), (90, 20))
        env = kw["env"]
        for k in ("GITLAB_TOKEN", "GH_TOKEN", "PIPER_TOKEN", "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(k, env)
        self.assertEqual((env["HOME"], env["PIPER_HOST_TERMINAL"], env["USER"], env["SSH_AUTH_SOCK"]),
                         (common.REAL_HOME, "1", "me", "/tmp/agent"))
        self.assertEqual(pty.calls[1], ("subscribe", {"id": item["id"], "data": False}))
        self.assertEqual(bumps, [1])
        a = audits("host-terminal-open")[0]
        self.assertEqual((a["terminal"], a["project"], a["cwd"]), (item["id"], "team-x/app", kw["cwd"]))
        self.assertEqual([r["id"] for r in ht.summary()["items"]], [item["id"]])
        # persisted: a restarted piperd lists it again
        again = hostterm.HostTerminals(lambda: pty, lambda: None, lambda: True, ht.file)
        self.assertEqual(again.get(item["id"])["project"], "team-x/app")

    def test_second_shell_of_a_project_is_numbered_and_the_limit_holds(self):
        ht, _, _ = make()
        a, b = ht.open(project()), ht.open(project())
        self.assertEqual((a["title"], b["title"]), ("app", "app 2"))
        for _ in range(hostterm.MAX_OPEN - 2):
            ht.open(project())
        with self.assertRaises(ValueError):
            ht.open(project())

    def test_missing_folder_is_refused(self):
        ht, pty, _ = make()
        with self.assertRaises(ValueError):
            ht.open(project(os.path.join(common.PIPER_HOME, "no-such-dir")))
        self.assertEqual(pty.calls, [])

    def test_exit_event_and_close(self):
        ht, pty, _ = make()
        item = ht.open(project())
        ht.on_event({"ev": "exit", "id": item["id"], "rc": 3})
        row = ht.summary()["items"][0]
        self.assertEqual((row["alive"], row["rc"]), (False, 3))
        self.assertEqual(audits("host-terminal-exit")[0]["rc"], 3)
        ht.on_event({"ev": "exit", "id": "s-other", "rc": 1})   # not a host id: ignored
        self.assertTrue(ht.close(item["id"]))
        self.assertEqual(pty.calls[-1], ("forget", {"id": item["id"]}))   # exited: only its buffer goes
        self.assertEqual(ht.summary()["items"], [])
        self.assertFalse(ht.close(item["id"]))
        self.assertEqual(audits("host-terminal-close")[0]["terminal"], item["id"])

    def test_an_ended_shell_carries_when_it_ended(self):
        """D5: the GUI closes an ended shell outside the grid 10 minutes after `ended`."""
        ht, _, _ = make()
        a, b = ht.open(project()), ht.open(project())
        self.assertIsNone(ht.summary()["items"][0]["ended"])
        with mock.patch.object(time, "time", return_value=1000.0):
            ht.on_event({"ev": "exit", "id": a["id"], "rc": 0})
        with mock.patch.object(time, "time", return_value=2000.0):
            ht.reconcile({a["id"]: {"id": a["id"], "alive": False, "rc": 0}})   # b gone with ptyhost, a stays
        rows = {r["id"]: r for r in ht.summary()["items"]}
        self.assertEqual(rows[a["id"]]["ended"], 1000.0)   # an already-ended one keeps its time
        self.assertEqual(rows[b["id"]]["ended"], 2000.0)

    def test_close_of_a_running_shell_hangs_it_up(self):
        ht, pty, _ = make()
        item = ht.open(project())
        with mock.patch.object(threading.Thread, "start"):   # the later KILL check
            ht.close(item["id"])
        self.assertIn(("signal", {"id": item["id"], "sig": "HUP"}), pty.calls)

    def test_reconcile_after_a_ptyhost_restart(self):
        ht, pty, _ = make()
        a, b = ht.open(project()), ht.open(project())
        pty.calls.clear()
        ht.reconcile({a["id"]: {"id": a["id"], "alive": True}})   # b is gone with the old ptyhost
        rows = {r["id"]: r for r in ht.summary()["items"]}
        self.assertTrue(rows[a["id"]]["alive"])
        self.assertFalse(rows[b["id"]]["alive"])
        self.assertEqual(pty.calls, [("subscribe", {"id": a["id"], "data": False})])

    def test_an_agent_can_neither_see_nor_propose_it(self):
        self.assertIn(("features", "host"), settings.AGENT_DENY)   # 9.40: Integrations folded into Features
        keys = {(s["section"], f["key"]) for s in settings.agent_describe() for f in s["fields"]}
        self.assertNotIn(("features", "host"), keys)
        self.assertIn(("features", "grid"), keys)   # the other modules stay proposable (each a card)
        self.assertIn(("features", "gitlab"), keys)
        with self.assertRaises(settings.SettingsError):
            settings.agent_preview("features", {"host": True})
        f = next(f for f in settings.section("features").fields if f.key == "host")
        self.assertTrue(f.weakens(False, True))   # turning it on asks for a confirmation
        self.assertIsNone(f.weakens(True, False))
        mcp = open(os.path.join(os.path.dirname(hostterm.__file__), "sandbox", "piper-mcp.py")).read()
        self.assertNotIn("host-terminal", mcp)
        self.assertNotIn("hostterm", mcp)


@NEEDS_PTY
class HostTermPtyTest(unittest.TestCase):
    """A real shell through the in-thread ptyhost: typed input runs on the host, the exit is seen."""

    def test_shell_runs_and_its_exit_reaches_the_list(self):
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        events, got = [], threading.Event()
        ht = None

        def on_event(ev):
            events.append(ev)
            if ht is not None:
                ht.on_event(ev)
            if ev.get("ev") == "exit":
                got.set()
        pc = ph.client(on_event=on_event)
        self.addCleanup(pc.close)
        ht = hostterm.HostTerminals(lambda: pc, lambda: None, lambda: True,
                                    os.path.join(common.PIPER_HOME, "host-terminals-pty.json"))
        with mock.patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            item = ht.open(project())
        viewer = ph.client()
        self.addCleanup(viewer.close)
        self.assertTrue(viewer.call("input", id=item["id"], data=base64.b64encode(
            b"echo hi-$PIPER_HOST_TERMINAL-$GITLAB_TOKEN; exit 3\n").decode())["ok"])   # unlocked
        self.assertTrue(got.wait(10))
        self.assertFalse(ht.get(item["id"])["alive"])
        self.assertEqual(ht.get(item["id"])["rc"], 3)


class HostTermFakeManager(FakeManager):
    def __init__(self, on):
        super().__init__()
        self.pol = {"host": on}
        self.pty = FakePty()
        self.hosts = hostterm.HostTerminals(lambda: self.pty, lambda: None, lambda: self.pol.get("host"),
                                            os.path.join(common.PIPER_HOME, "host-terminals-api-%s.json" % os.urandom(3).hex()))


class HostTermApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gui = common.own_token("gui")
        cls.tty = common.own_token("tty")
        with open(common.TOKENS_FILE, "a") as f:
            f.write("scoped-host\tsession:s1\thook\n")
        cls.proj = _env.make_project("hostterm-app", {"README": "x"})

    def setUp(self):
        self.old = piperd.MANAGER
        piperd.MANAGER = self.m = HostTermFakeManager(on=False)
        self.addCleanup(lambda: setattr(piperd, "MANAGER", self.old))
        known = {"team-x/hostterm-app": {"key": "team-x/hostterm-app", "title": "app", "path": self.proj}}
        patcher = mock.patch.object(piperd.projects, "find", lambda key, pp=None: known.get(key))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_is_403_while_off_and_for_any_other_token(self):
        self.assertEqual(post("/api/host-terminals", {"project": "team-x/hostterm-app"}, self.gui)[0], 403)
        self.m.pol["host"] = True
        for tok in (self.tty, "scoped-host"):
            self.assertEqual(post("/api/host-terminals", {"project": "team-x/hostterm-app"}, tok)[0], 403, tok)
        self.assertFalse(piperd.terminal_post(["api", "host-terminals"]))
        self.assertEqual(self.m.pty.calls, [])
        self.assertEqual(post("/api/host-terminals", {"project": "nope/nope"}, self.gui)[0], 404)
        code, _, body = post("/api/host-terminals", {"project": "team-x/hostterm-app"}, self.gui)
        self.assertEqual(code, 200, body)
        tid = json.loads(body)["id"]
        self.assertEqual(self.m.pty.calls[0][1]["cwd"], os.path.realpath(self.proj))
        self.assertEqual(post("/api/host-terminals/%s/close" % tid, {}, self.tty)[0], 403)
        self.assertEqual(post("/api/host-terminals/%s/close" % tid, {}, self.gui)[0], 200)
        self.assertEqual(post("/api/host-terminals/%s/close" % tid, {}, self.gui)[0], 404)

    def test_session_terminal_uses_registered_checkout_and_preserves_owner(self):
        from types import SimpleNamespace
        self.m.pol["host"] = True
        session = SimpleNamespace(project="team-x/hostterm-app@topic", path=self.proj, title="Topic", kind="project")
        with mock.patch.object(self.m, "get", return_value=session), mock.patch.object(self.m, "gui_project", create=True,
                return_value={"key": session.project, "path": self.proj, "title": "Worktree"}):
            code, _, body = post("/api/host-terminals", {"session": "s1", "project": "ignored/base"}, self.gui)
            self.assertEqual(code, 200, body)
            item = json.loads(body)
            self.assertEqual((item["session"], item["project"], item["path"]), ("s1", session.project, os.path.realpath(self.proj)))
            self.assertEqual(self.m.pty.calls[0][1]["cwd"], os.path.realpath(self.proj))
            self.assertEqual(self.m.hosts.summary()["items"][0]["session"], "s1")
            session.path = os.path.join(self.proj, "different")
            self.assertEqual(post("/api/host-terminals", {"session": "s1"}, self.gui)[0], 409)

    def test_state_lists_them_for_the_gui_only(self):
        self.m.pol["host"] = True
        item = self.m.hosts.open({"key": "team-x/hostterm-app", "title": "app", "path": self.proj})
        st = json.loads(get("/api/state", token=self.gui)[2])
        self.assertEqual((st["host_terms"]["on"], [r["id"] for r in st["host_terms"]["items"]]), (True, [item["id"]]))
        self.assertEqual(json.loads(get("/api/state", token=self.tty)[2])["host_terms"], {})

    def ws(self, sid, tok):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        b.settimeout(10)
        threading.Thread(target=serve, args=(a,), daemon=True).start()
        key = base64.b64encode(os.urandom(16)).decode()
        b.sendall(("GET /ws/%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nOrigin: http://%s\r\n\r\n"
                   % (sid, HOST, key, HOST)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += b.recv(1)
        status = int(head.split()[1])
        if status != 101 or tok is None:
            return status, None
        b.sendall(ws_frame(json.dumps({"auth": tok}).encode()))
        return status, ws_read(b)

    def test_websocket_needs_the_policy_and_the_gui_token(self):
        self.m.pol["host"] = True
        item = self.m.hosts.open({"key": "team-x/hostterm-app", "title": "app", "path": self.proj})
        self.assertEqual(self.ws("host-unknown", None)[0], 404)
        for tok in (self.tty, "scoped-host"):
            status, (op, payload) = self.ws(item["id"], tok)
            self.assertEqual((status, op), (101, wsproto.OP_CLOSE))
            self.assertEqual(int.from_bytes(payload[:2], "big"), 4403, tok)
        self.assertEqual(audits("host-terminal-refused")[0]["terminal"], item["id"])
        class Relay:   # what the GUI token reaches: ptyhost's subscribe (the relay itself: test_piperd)
            def call(self, op, **kw):
                return {"ok": True, "info": {"id": kw.get("id")}} if op == "subscribe" else {"ok": True}

            def close(self):
                pass
        with mock.patch.object(piperd, "pty_client", lambda on_event: Relay()):
            status, (op, payload) = self.ws(item["id"], self.gui)
        self.assertEqual((status, op, json.loads(payload)), (101, wsproto.OP_TEXT, {"t": "hello", "info": {"id": item["id"]}}))
        self.m.pol["host"] = False
        self.assertEqual(self.ws(item["id"], None)[0], 403)


if __name__ == "__main__":
    unittest.main()
