"""The human's half-written prompt: ptyhost's InputTracker and
piperd typing automatic text (a handoff line, send_to_session) only into an empty prompt."""
import base64
import json
import os
import time
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import ptyhost


def touched(*chunks: bytes) -> bool:
    t = ptyhost.InputTracker()
    for c in chunks:
        t.feed(c)
    return t.key_at > 0


class TrackerTest(unittest.TestCase):
    """InputTracker.key_at: every byte that can put text into Claude's prompt is the human's touch."""

    def test_keys_that_can_change_the_prompt_touch_it(self):
        for keys in (b"a", "é".encode(), b"\r", b"\t", b"\x7f", b"\x03", b"\x15", b"\x17",
                     b"\x1b\r",                       # Shift/Option+Enter (a newline)
                     b"\x1bb",                          # Alt+b
                     b"\x1b[A", b"\x1b[B", b"\x1bOA",  # Up/Down: history recall puts text in
                     b"\x1b[3~",                        # Delete
                     b"\x1b[200~text\x1b[201~"):       # a paste
            self.assertTrue(touched(keys), keys)

    def test_the_terminals_own_answers_do_not(self):
        for answer in (b"\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\",   # OSC colour answer (ST)
                       b"\x1b]10;rgb:ffff/ffff/ffff\x07",      # (BEL)
                       b"\x1bP1$r0m\x1b\\",                   # DCS (DECRQSS)
                       b"\x1b[I", b"\x1b[O",                  # focus reports
                       b"\x1b[<0;10;5M", b"\x1b[<0;10;5m",    # SGR mouse
                       b"\x1b[12;40R",                        # cursor position report
                       b"\x1b[?1;2c",                         # device attributes
                       b"\x1b[C", b"\x1b[D",                  # cursor left/right: no text
                       b"\x1b"):                              # a lone Esc
            self.assertFalse(touched(answer), answer)

    def test_a_string_ends_with_its_write(self):
        """Alt+] from a non-Mac browser is ESC ] - it must not swallow the keys after it."""
        self.assertTrue(touched(b"\x1b]", b"fix it"))
        t = ptyhost.InputTracker()
        t.feed(b"\x1b[200~a\x1b]b")        # an ESC ] inside a paste is pasted text ...
        t.feed(b"c\x1b[201~")               # ... and the paste still ends
        self.assertFalse(t._paste)

    def test_the_last_enter_marks_where_a_submitted_prompt_ended(self):
        t = ptyhost.InputTracker()
        t.feed(b"fix it")
        t.feed(b"\r")
        self.assertEqual(t.enter_at, t.key_at)          # the Enter is the last touch: nothing after it
        t.feed(b"abc\rdef")                             # keys after an Enter in the same write: a new draft
        self.assertLess(t.enter_at, t.key_at)
        k = t.key_at
        t.auto(b"\x1b[200~text\x1b[201~")              # piperd's paste: no touch, no Enter
        self.assertEqual((t.key_at, t.enter_at < k), (k, True))
        t.auto(b"\r")                                   # piperd's Enter submits: the mark moves, the touch does not
        self.assertGreater(t.enter_at, k)
        self.assertEqual(t.key_at, k)
        u = ptyhost.InputTracker()
        u.feed(b"\x1b[200~line1\rline2\x1b[201~")      # a newline inside a paste is no Enter
        self.assertEqual(u.enter_at, 0.0)

    def test_pending_op_reports_the_last_touch(self):
        h = ptyhost.Host()
        s = ptyhost.Session("s1", 0, -1, 80, 24, None, False)
        h.sessions["s1"] = s
        self.assertEqual(h._handle(None, {"op": "pending", "id": "s1"}), {"ok": True, "key_at": 0.0, "enter_at": 0.0})
        before = time.time()
        s.typed.feed(b"x")
        self.assertGreaterEqual(h._handle(None, {"op": "pending", "id": "s1"})["key_at"], before)
        self.assertEqual(s.info()["key_at"], s.typed.key_at)


class FakePty:
    """ptyhost's `pending` over a real InputTracker per session (what the Manager relies on)."""

    def __init__(self):
        self.trackers = {}

    def tracker(self, sid):
        return self.trackers.setdefault(sid, ptyhost.InputTracker())

    def keys(self, sid, data: bytes):
        self.tracker(sid).feed(data)

    def call(self, op, **kw):
        if op == "pending":
            t = self.tracker(kw["id"])
            return {"ok": True, "key_at": t.key_at, "enter_at": t.enter_at}
        if op == "input":
            import base64 as b64m
            data = b64m.b64decode(kw["data"])
            (self.tracker(kw["id"]).auto if kw.get("auto") else self.tracker(kw["id"]).feed)(data)
        return {"ok": True}


class ScenarioTest(unittest.TestCase):
    """The sequences earlier reviews broke the typed-character tracker with, on the real rule:
    piperd types only while the human touched nothing since the last submitted prompt."""

    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, background=True)

    def setUp(self):
        import sessions
        from unittest import mock
        self.pty = FakePty()
        p = mock.patch.object(self.m, "pty", self.pty)
        p.start()
        self.addCleanup(p.stop)
        self.s = sessions.Session(kind="project", project="scn", path="/x", state="waiting", gitlab=[], pid=4242)
        self.s.idle = True
        os.makedirs(os.path.join(self.s.cfg, "piper-mailbox", "out"), exist_ok=True)
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(setattr, self.s, "state", "exited")

    def ev(self, name, ts=None, **kw):
        with open(os.path.join(self.s.cfg, "piper-events.jsonl"), "a") as f:
            f.write(json.dumps(dict({"ev": name, "ts": time.time() if ts is None else ts}, **kw)) + "\n")
        self.m._events(self.s)

    def keys(self, data: bytes):
        time.sleep(0.002)   # distinct timestamps
        self.pty.keys(self.s.id, data)

    def ok(self) -> bool:
        return self.m.auto_type_ok(self.s)

    def test_a_submitted_prompt_frees_the_terminal(self):
        self.keys(b"fix the test\r")
        self.assertFalse(self.ok())            # typed, not yet confirmed by Claude Code
        self.ev("UserPromptSubmit")
        self.assertTrue(self.ok())
        self.ev("Stop")
        self.assertTrue(self.ok())             # nothing typed since: the handoff line may go

    def test_backslash_newline_and_at_pick_while_claude_works_hold(self):
        import sessions
        self.ev("UserPromptSubmit")
        for keys in (b"first line \\\r", b"@src/fo\r"):   # a newline after a backslash, an @ pick
            self.keys(keys)
            self.ev("Stop")                    # the turn ends: no confirmation of anything
            self.assertFalse(self.ok(), keys)
            self.assertEqual(self.s.hold, sessions.Manager.HOLD_TEXT)
            self.ev("UserPromptSubmit")        # the human sends it

    def test_a_permission_dialog_answer_holds_until_the_next_prompt(self):
        self.ev("UserPromptSubmit")
        self.ev("Notification", notification_type="permission_prompt")
        self.keys(b"\x1b[B")
        self.keys(b"\r")                      # arrows + Enter, or a digit - whatever it was
        self.ev("Stop")
        self.assertFalse(self.ok())            # one click (Send now) instead of guessing
        self.m.input_clear(self.s.id)
        self.assertTrue(self.ok())

    def test_text_typed_before_a_dialog_and_a_queued_prompt(self):
        self.ev("UserPromptSubmit")
        self.keys(b"xy")                       # half-written while Claude works
        self.ev("Notification", notification_type="permission_prompt")
        self.keys(b"\r")                      # answers the dialog
        self.ev("Stop")
        self.assertFalse(self.ok())            # xy may still be there
        self.keys(b"queued\r")                # a queued prompt
        self.ev("UserPromptSubmit")
        self.assertTrue(self.ok())

    def test_a_compaction_is_no_submit_but_clear_is(self):
        self.ev("UserPromptSubmit")
        self.keys(b"draft")
        self.ev("SessionStart", source="compact")
        self.assertFalse(self.ok())
        self.keys(b"\x15/clear\r")           # Ctrl+U, /clear, Enter
        self.ev("SessionStart", source="clear")
        self.assertTrue(self.ok())

    def test_a_replay_after_a_restart_frees_nothing(self):
        self.keys(b"draft")
        self.ev("UserPromptSubmit", ts=time.time() - 3600)   # an old line read again
        self.assertFalse(self.ok())

    def test_a_draft_survives_an_piperd_restart_that_replays_the_hook(self):
        self.keys(b"sent prompt\r")
        self.ev("UserPromptSubmit")
        self.keys(b"next draft")          # a new draft, not sent
        self.s.submit_at, self.s.events_off = 0.0, 0   # piperd restarts: the events file is read from the start
        self.m._events(self.s)                 # the UserPromptSubmit (younger than 120 s) is read again
        self.assertFalse(self.ok())            # its mark is the terminal's Enter, before the draft

    def test_a_paste_right_after_enter_is_a_new_draft_even_before_the_hook(self):
        self.keys(b"prompt\r")
        self.keys(b"\x1b[200~stack trace...\x1b[201~")   # pasted before piperd read the hook
        self.ev("UserPromptSubmit")
        self.assertFalse(self.ok())

    def test_a_queued_prompt_and_a_draft_after_it(self):
        self.ev("UserPromptSubmit")
        self.keys(b"A\r")                     # queued while Claude works
        self.keys(b"draft B")
        self.ev("UserPromptSubmit")            # Claude takes A only now
        self.assertFalse(self.ok())            # B is still there

    def test_send_now_delivers_the_start_task_that_waited(self):
        from unittest import mock
        self.s.task, self.s.task_sent, self.s.state = "fix #7", False, "ready"
        with mock.patch.object(self.m, "_deliver_task") as deliver:
            self.m.input_clear(self.s.id)
        deliver.assert_called_once_with(self.s)

    def test_keys_during_piperds_typing_leave_its_text_unsent(self):
        import threading
        self.ev("UserPromptSubmit")
        th = threading.Thread(target=self.m.type_text, args=(self.s, "[piper] line"))
        th.start()
        time.sleep(0.1)
        self.keys(b"x")                        # the human types within the 0.3 s before piperd's Enter
        th.join(3)
        self.assertLess(self.pty.tracker(self.s.id).enter_at, self.pty.tracker(self.s.id).key_at)   # no Enter sent

    def test_a_restart_of_piperd_holds_until_the_next_prompt(self):
        self.keys(b"done\r")
        self.ev("UserPromptSubmit")
        self.s.submit_at = 0.0                 # what a fresh piperd knows
        self.assertFalse(self.ok())            # safe: a click, never a spoiled prompt
        self.ev("UserPromptSubmit")
        self.assertTrue(self.ok())

    def test_terminal_answers_and_piperds_typing_never_hold(self):
        self.ev("UserPromptSubmit")
        self.keys(b"\x1b]11;rgb:0/0/0\x07")  # xterm.js answers a colour query
        self.m.type_text(self.s, "[piper] line")   # piperd's own paste + Enter
        self.assertTrue(self.ok())

    def test_the_enter_that_answered_the_startup_trust_dialog_never_holds(self):
        self.s.state, self.s.text = "ready", "Yes, I trust this folder  Enter to confirm"
        self.assertEqual(self.m._screen(self.s), "dialog")
        self.keys(b"\r")                      # the human answers Claude's folder trust dialog
        self.s.text += " Claude Code v2  ? for shortcuts"
        self.assertEqual(self.m._screen(self.s), "ready")
        self.assertTrue(self.ok())             # the start task / handoff may go
        self.keys(b"draft")                    # typed at the prompt afterwards: still theirs
        self.m._screen(self.s)
        self.assertFalse(self.ok())

    def test_the_keys_that_answered_holes_trust_question_never_hold(self):
        self.s.state, self.s.trust_seen = "trust", True
        self.keys(b"y\r")
        self.s.text = "Trust this project? y\nLaunching sandbox for: /x\nStarting claude agent"
        self.m._on_pty_event({"ev": "data", "id": self.s.id, "data": ""})
        self.assertEqual(self.s.state, "starting")
        self.assertTrue(self.ok())

    def test_the_timesheet_verdict(self):
        seen = []
        from unittest import mock
        with mock.patch.object(self.m, "_turn_event", lambda sess, ev, e: seen.append(e.get("_human"))):
            self.keys(b"fix it\r")
            self.ev("UserPromptSubmit", _human=False)
            self.ev("UserPromptSubmit", _human=True)   # a forged second line, nothing typed in between
            self.s.gui_sent_at = time.time()           # "Send" from the Composer
            self.ev("UserPromptSubmit")
        self.assertEqual(seen, [True, False, True])    # the file's own _human is ignored


class ManagerHoldTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, background=True)

    def setUp(self):
        self.typed = []
        self.m.type_text = lambda s, text: self.typed.append((s.id, text))
        self.addCleanup(lambda: delattr(self.m, "type_text"))
        self.pending = {"n": 0}
        self.m.input_pending = lambda s: self.pending["n"]
        self.addCleanup(lambda: delattr(self.m, "input_pending"))

    def live(self):
        import sessions
        s = sessions.Session(kind="project", project="live", path="/x", state="waiting", gitlab=["grp/live"])
        s.idle = True
        os.makedirs(os.path.join(s.cfg, "piper-mailbox", "out"), exist_ok=True)
        self.m.sessions[s.id] = s
        self.addCleanup(lambda: setattr(s, "state", "exited"))
        return s

    def wait(self, pred, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end and not pred():
            time.sleep(0.05)
        return pred()

    def test_handoff_line_waits_for_a_half_written_prompt(self):
        import sessions
        parent, child = self.live(), self.live()
        child.parent, child.handoff = parent.id, {"reported": True}
        parent.handoff_notify = [child.id]
        self.pending["n"] = 4
        self.m._flush_handoff_notes(parent)
        time.sleep(0.3)
        self.assertEqual(self.typed, [])
        self.assertEqual(parent.hold, sessions.Manager.HOLD_TEXT)
        self.assertIn("hold", self.m.list()[0])
        self.assertEqual(parent.handoff_notify, [child.id])
        self.pending["n"] = 0   # the human sent (or deleted) it: the poll loop retries after 2 s
        self.assertTrue(self.wait(lambda: self.typed, 8.0))
        self.assertIn(child.id, self.typed[0][1])
        self.assertEqual(parent.hold, "")

    def test_the_badge_clears_once_the_prompt_is_empty(self):
        import sessions
        s = self.live()
        self.pending["n"] = 2
        self.assertFalse(self.m.auto_type_ok(s))
        self.assertEqual(s.hold, sessions.Manager.HOLD_TEXT)
        s.hold_try = 0.0
        self.pending["n"] = 0   # sent (or "Send now"): the poll loop re-checks the badge on its own
        self.assertTrue(self.wait(lambda: s.hold == "", 6.0))

    def test_the_badge_clears_while_claude_works_and_never_shows_on_an_ended_session(self):
        import sessions
        parent, child = self.live(), self.live()
        child.parent, child.handoff = parent.id, {"reported": True}
        parent.handoff_notify = [child.id]
        self.pending["n"] = 3
        self.m._flush_handoff_notes(parent)
        self.assertEqual(parent.hold, sessions.Manager.HOLD_TEXT)
        parent.state, parent.idle = "working", False   # the human sent the prompt: Claude works, the line still waits
        self.pending["n"] = 0
        parent.hold_try = 0.0
        self.assertTrue(self.wait(lambda: parent.hold == "", 6.0))
        self.assertEqual(self.typed, [])               # not typed into a working session
        parent.hold = sessions.Manager.HOLD_TEXT
        parent.state = "exited"
        row = next(r for r in self.m.list() if r["id"] == parent.id)
        self.assertEqual(row["hold"], "")

    def test_input_clear_sends_the_line_that_waited(self):
        import sessions
        parent, child = self.live(), self.live()
        child.parent, child.handoff = parent.id, {"reported": True}
        parent.handoff_notify = [child.id]
        parent.hold = sessions.Manager.HOLD_TEXT
        parent.hold_try = time.time() + 3600   # the poll loop must not be the one that sends it
        self.m.input_clear(parent.id)   # "Send now"
        self.assertTrue(self.wait(lambda: self.typed, 5.0))
        self.assertIn(child.id, self.typed[0][1])

    def test_racing_flushes_type_the_handoff_line_once(self):
        import threading
        parent, child = self.live(), self.live()
        child.parent, child.handoff = parent.id, {"reported": True}
        parent.handoff_notify = [child.id]
        slow = lambda s: time.sleep(0.1) or 0   # noqa: E731 - the ptyhost round trip widens the window
        self.m.input_pending = slow
        ths = [threading.Thread(target=self.m._flush_handoff_notes, args=(parent,)) for _ in range(4)]
        for th in ths:
            th.start()
        for th in ths:
            th.join(5)
        self.assertTrue(self.wait(lambda: not parent.handoff_notify, 5.0))
        time.sleep(0.5)
        self.assertEqual(len(self.typed), 1)

    def test_a_resumed_session_is_idle_on_its_idle_prompt(self):
        s = self.live()
        s.state, s.idle = "ready", False            # resumed: attested, no turn (no Stop) in this process yet
        with open(os.path.join(s.cfg, "piper-events.jsonl"), "w") as f:
            f.write('{"ev": "Notification", "notification_type": "idle_prompt", "ts": %f}\n' % time.time())
        self.m._events(s)
        self.assertEqual((s.state, s.idle), ("attention", True))
        t = self.live()
        t.state, t.idle = "waiting", False          # a permission prompt came first: its idle_prompt is not idle
        with open(os.path.join(t.cfg, "piper-events.jsonl"), "w") as f:
            f.write('{"ev": "Notification", "notification_type": "idle_prompt", "ts": %f}\n' % time.time())
        self.m._events(t)
        self.assertFalse(t.idle)

    def test_send_to_session_refuses_a_half_written_prompt(self):
        import sessions
        adv = sessions.Session(kind="advisor", project="", path="", state="ready")
        self.m.sessions[adv.id] = adv
        self.addCleanup(lambda: setattr(adv, "state", "exited"))
        target = self.live()
        self.m.ask = lambda *a, **k: True
        self.addCleanup(lambda: delattr(self.m, "ask"))
        self.pending["n"] = 2
        with self.assertRaises(ValueError) as e:
            self.m.tool(adv, "send_to_session", {"id": target.id, "text": "hello"})
        self.assertIn("typed", str(e.exception))
        self.pending["n"] = 0
        self.assertEqual(self.m.tool(adv, "send_to_session", {"id": target.id, "text": "hello"}), {"sent": True})

    def test_unknown_to_ptyhost_means_no_hold(self):
        import sessions
        delattr(self.m, "input_pending")
        self.m.input_pending = sessions.Manager.input_pending.__get__(self.m)
        s = self.live()   # not a PTY in ptyhost: it cannot tell - the old behaviour
        self.assertIsNone(self.m.input_pending(s))
        self.assertTrue(self.m.auto_type_ok(s))


@unittest.skipUnless(_env.pty_available(), "no PTY devices here (e.g. a sandbox denying /dev/ptmx)")
class PtyInputTest(unittest.TestCase):
    def test_human_input_counts_automatic_input_does_not(self):
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        c = ph.client()
        env = {"PATH": os.environ["PATH"], "HOME": _env.HOME, "TERM": "xterm-256color"}
        r = c.call("spawn", id="t1", argv=["/bin/cat"], env=env, cols=80, rows=24, locked=False)
        self.assertTrue(r["ok"], r)
        b = lambda x: base64.b64encode(x).decode()   # noqa: E731
        c.call("input", id="t1", data=b(b"\x1b[200~xyz\x1b[201~\r"), auto=True)   # piperd's paste + Enter
        r = c.call("pending", id="t1")
        self.assertEqual(r["key_at"], 0.0)            # piperd's input is no touch ...
        self.assertGreater(r["enter_at"], 0.0)        # ... but its Enter submitted the prompt
        before = time.time()
        c.call("input", id="t1", data=b(b"abc"))
        self.assertGreaterEqual(c.call("pending", id="t1")["key_at"], before)
        c.call("signal", id="t1", sig="KILL")


if __name__ == "__main__":
    unittest.main()
