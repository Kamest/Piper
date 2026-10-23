"""9.141: sessions a restart interrupted - the mark _connect leaves on what ptyhost lost (what it was doing,
reboot / ptyhost / piperd), the one-time restore card (/api/state interrupted_notice + seen) and its end
(a start, Remove, Dismiss)."""
import os
import time
import unittest
from unittest import mock

import _env
import common
import sessions


class FakePty:
    """ptyhost after a restart: knows only `alive` sessions."""

    def __init__(self, alive=()):
        self.alive = list(alive)
        self.calls = []

    def call(self, op, **kw):
        self.calls.append((op, kw))
        if op == "list":
            return {"sessions": [{"id": i, "alive": True, "pid": 1} for i in self.alive]}
        return {"ok": True}

    def close(self):
        pass


class InterruptedTest(unittest.TestCase):
    def setUp(self):
        self.m = _env.manager(self)
        self.addCleanup(self.m.sessions.clear)
        self.addCleanup(lambda: os.path.exists(common.INTERRUPTED_FILE) and os.unlink(common.INTERRUPTED_FILE))
        if os.path.exists(common.INTERRUPTED_FILE):
            os.unlink(common.INTERRUPTED_FILE)
        self.pty = FakePty()
        self.m._connect_fn = lambda event, close: self.pty

    def add(self, sid, state, **kw):
        s = sessions.Session(id=sid, project=kw.pop("project", "team-x/app"), state=state, **kw)
        self.m.sessions[sid] = s
        return s

    def connect(self, boot, resumable=True):
        with mock.patch.object(common, "boot_id", return_value=boot), \
                mock.patch.object(self.m, "_has_transcript", return_value=resumable):
            self.m._connect()

    def test_a_reboot_marks_what_each_lost_session_was_doing(self):
        self.connect("boot-A")   # the boot before the restart is stored
        work = self.add("w1", "working", pid=11, claude_session="c1")
        wait = self.add("w2", "waiting", pid=12, claude_session="c2")
        start = self.add("w3", "preflight")
        stop = self.add("w4", "stopping", pid=14)
        agent = self.add("w5", "working", pid=15, kind="advisor")
        live = self.add("w6", "ready", pid=16)
        ended = self.add("w7", "exited", pid=17, rc=0)
        self.pty.alive = ["w6"]
        self.connect("boot-B", resumable=True)
        self.assertEqual((work.state, work.interrupted["was"], work.interrupted["cause"], work.interrupted["resumes"]),
                         ("exited", "working", "reboot", True))
        self.assertEqual(wait.interrupted["was"], "waiting")
        self.assertEqual(work.detail, "lost (computer restarted)")
        self.assertEqual((start.interrupted["was"], start.interrupted["resumes"]), ("preflight", False))   # no conversation yet
        self.assertEqual(stop.state, "exited")
        self.assertIsNone(stop.interrupted, "the user was stopping it - nothing to offer")
        self.assertIsNone(agent.interrupted, "the Global agent has its own start")
        self.assertIsNone(live.interrupted)
        self.assertIsNone(ended.interrupted)
        saved = sessions.Session(**common.read_json(os.path.join(work.dir, "meta.json"), {}))
        self.assertEqual(saved.interrupted["was"], "working", "the mark survives a piperd restart")

    def test_without_a_reboot_the_cause_is_ptyhost_or_piperd(self):
        self.connect("boot-A")
        lost = self.add("p1", "working", pid=11)
        start = self.add("p2", "preparing")
        self.connect("boot-A")
        self.assertEqual((lost.interrupted["cause"], lost.detail), ("ptyhost", "lost (ptyhost restarted)"))
        self.assertEqual(start.interrupted["cause"], "piperd")

    def test_an_unknown_or_first_boot_is_never_called_a_restart(self):
        s = self.add("u1", "working", pid=11)
        self.connect("boot-A")   # nothing stored yet (the first piperd with 9.141)
        self.assertEqual(s.interrupted["cause"], "ptyhost")
        self.assertEqual(common.read_json(common.INTERRUPTED_FILE, {})["boot"], "boot-A")
        s2 = self.add("u2", "working", pid=12)
        self.connect("")
        self.assertEqual(s2.interrupted["cause"], "ptyhost")
        self.assertEqual(common.read_json(common.INTERRUPTED_FILE, {})["boot"], "boot-A", "an unknown boot keeps the stored one")

    def test_the_notice_is_shown_once_per_interruption(self):
        self.assertIsNone(self.m.interrupted_notice())
        self.connect("boot-A")
        a = self.add("n1", "working", pid=11, title="one", since=100)
        b = self.add("n2", "ready", pid=12, title="two", since=200)
        child = self.add("n3", "working", pid=13, parent="n1")
        self.connect("boot-B")
        n = self.m.interrupted_notice()
        self.assertEqual(n["cause"], "reboot")
        self.assertEqual(n["limit"], self.m.max_sessions())
        self.assertEqual([x["id"] for x in n["sessions"]], ["n2", "n1"], "newest activity first; a handoff child follows its parent")
        self.assertEqual(n["sessions"][1]["was"], "working")
        self.assertIsNotNone(child.interrupted, "the child is still marked in the queue")
        self.m.interrupted_seen(n["id"])
        self.assertFalse(n["seen"])
        self.assertTrue(self.m.interrupted_notice()["seen"], "seen - another tab / device does not open it again")
        self.assertIsNotNone(a.interrupted, "seen keeps the marks (Later)")
        time.sleep(0.01)
        c = self.add("n4", "working", pid=14)
        self.connect("boot-B")
        n2 = self.m.interrupted_notice()
        self.assertEqual({x["id"] for x in n2["sessions"]}, {"n1", "n2", "n4"}, "a later interruption offers all marked again")
        self.assertEqual((n2["cause"], n2["seen"]), ("ptyhost", False))
        self.assertTrue(c.interrupted and b.interrupted)
        with self.assertRaises(ValueError):
            self.m.interrupted_seen("x")

    def test_dismiss_drops_only_the_marks(self):
        self.connect("boot-A")
        a = self.add("d1", "working", pid=11)
        b = self.add("d2", "working", pid=12)
        self.connect("boot-B")
        self.assertEqual(self.m.interrupted_dismiss(["d1"]), {"dismissed": 1})
        self.assertIsNone(a.interrupted)
        self.assertEqual(a.state, "exited")
        self.assertEqual(self.m.interrupted_dismiss("all"), {"dismissed": 1})
        self.assertIsNone(b.interrupted)
        self.assertIsNone(self.m.interrupted_notice())
        for bad in (None, "d1", [1]):
            with self.assertRaises(ValueError):
                self.m.interrupted_dismiss(bad)

    def test_a_start_or_remove_ends_the_mark(self):
        self.connect("boot-A")
        a = self.add("r1", "working", pid=11)
        b = self.add("r2", "working", pid=12)
        self.connect("boot-B")
        self.m._reserve(a, ("exited",), fresh=True)   # Continue / Retry / the card's Restore
        self.assertIsNone(a.interrupted)
        self.m.forget("r2")
        self.assertIsNone(b.interrupted)

    def test_a_broken_mark_in_meta_is_ignored(self):
        self.assertIsNone(sessions.Session(id="x1", interrupted="yes").interrupted)
        self.assertEqual(sessions.Session(id="x2", interrupted={"was": "ready"}).interrupted, {"was": "ready"})

    def test_boot_id_reads_the_platform_value(self):
        with mock.patch("builtins.open", side_effect=OSError), \
                mock.patch("subprocess.run") as run:
            run.return_value.stdout = "{ sec = 1729500000, usec = 12 } Mon Oct 21 10:00:00 2026\n"
            self.assertEqual(common.boot_id(), "1729500000")
            run.return_value.stdout = ""
            self.assertEqual(common.boot_id(), "")


if __name__ == "__main__":
    unittest.main()
