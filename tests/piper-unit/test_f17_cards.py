"""F17: an approval card names the Security switch that asks (why), and a card that went
unanswered stays listed for GONE_S seconds (the queue's "cancelled - nothing was sent")."""
import os
import threading
import time
import unittest
from unittest import mock

import _env
import security
import sessions


def wait_for(pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class CardWhyTest(unittest.TestCase):
    def test_every_ask_kind_has_its_group_and_label(self):
        for g in security.ASK_GROUPS:
            for kind, label in g["kinds"]:
                self.assertEqual(sessions.ask_why(kind), {"group": g["title"], "label": label})
        self.assertEqual(sessions.ask_why("git_push"), {"group": "Git", "label": "Push (git_push)"})
        self.assertEqual(sessions.ask_why("no_such_kind"), {})


class PendingGoneTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ph = _env.InThreadPtyHost()
        cls.m = sessions.Manager(connect_fn=lambda on_event, on_close: cls.ph.client(on_event, on_close))

    @classmethod
    def tearDownClass(cls):
        cls.ph.stop()

    def _live(self):
        s = sessions.Session(kind="project", project="live", path="/x", state="ready")
        os.makedirs(os.path.join(s.cfg, "piper-mailbox", "out"), exist_ok=True)
        self.m.sessions[s.id] = s
        self.addCleanup(lambda: setattr(s, "state", "exited"))
        return s

    def test_card_carries_why_and_an_unanswered_one_is_gone_for_a_while(self):
        s = self._live()
        with mock.patch.object(self.m, "policy", {**self.m.policy, "ask": ["git_push"]}):
            t = threading.Thread(target=lambda: self.m.ask("git_push", s, "Push", {}, timeout=0.5))
            t.start()
            try:
                self.assertTrue(wait_for(lambda: any(p["session"] == s.id for p in self.m.pending_list())))
                card = next(p for p in self.m.pending_list() if p["session"] == s.id)
                self.assertEqual(card["why"], {"group": "Git", "label": "Push (git_push)"})
            finally:
                t.join(3)
            gone = [g for g in self.m.pending_gone_list() if g["session"] == s.id]
            self.assertEqual([(g["id"], g["kind"], g["title"]) for g in gone], [(card["id"], "git_push", s.title)])
            # an answered card is no "cancelled" line
            t = threading.Thread(target=lambda: self.m.ask("git_push", s, "Push 2", {}, timeout=5))
            t.start()
            try:
                self.assertTrue(wait_for(lambda: any(p["session"] == s.id for p in self.m.pending_list())))
                self.m.decide(next(p for p in self.m.pending_list() if p["session"] == s.id)["id"], False)
            finally:
                t.join(6)
            self.assertEqual(len([g for g in self.m.pending_gone_list() if g["session"] == s.id]), 1)
            with mock.patch.object(sessions.time, "time", return_value=time.time() + sessions.GONE_S + 1):
                self.assertEqual([g for g in self.m.pending_gone_list() if g["session"] == s.id], [], "after GONE_S it goes")


if __name__ == "__main__":
    unittest.main()
