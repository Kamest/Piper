"""Operator exceptions from a quiet window: shared (not quiet), runtime-bound, fenced return."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import _env
import common
import features
import quietwindow as qw
import sessions


class ExemptCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "quiet.json")
        self.time = 100.0
        self.rows = [SimpleNamespace(id=str(i), title="Session " + str(i), created=1, pid=100+i, instance="a",
                                     state="ready", hidden=False, subagents={}, background_tasks={}, box=self.tmp.name) for i in range(4)]
        self.q = self.coordinator()
        self.rid = self.q.request("0", "Benchmark")["active"]["id"]

    def coordinator(self):
        return qw.Coordinator(lambda: self.rows, lambda: True, path=self.path,
                              clock=lambda: self.time, wall=lambda: self.time + 1000)

    def part(self, sid, q=None):
        return next(p for p in (q or self.q).snapshot()["active"]["participants"] if p["id"] == sid)

    def settle(self):
        self.q.tick(); self.time += qw.SETTLE_S; self.q.tick()
        return self.q.snapshot()["active"]

    def test_exempt_session_does_not_hold_draining_and_window_is_shared_not_quiet(self):
        self.q.ack("1", self.rid, done=True); self.q.ack("2", self.rid, done=True)
        self.assertNotEqual(self.settle()["phase"], "ready")   # 3 still finishing
        self.q.exempt("3", self.rid, True)
        with self.q.operation("3", "tool"):      # the exception admits real managed work
            a = self.settle()
        self.assertEqual(a["phase"], "ready")
        self.assertTrue(a["shared"])
        self.assertEqual(self.part("3")["status"], "exempt")
        self.assertEqual(self.q.blocked("3"), "")
        self.assertTrue(self.q.blocked("2"))
        kinds = [e["kind"] for e in self.q.snapshot()["events"]]
        self.assertIn("exempt_granted", kinds)
        self.assertEqual(self.q.snapshot()["events"][kinds.index("exempt_granted")]["source"], "operator")

    def test_exempt_participant_cannot_confirm_and_owner_or_unknown_ids_are_refused(self):
        self.q.exempt("1", self.rid, True)
        with self.assertRaisesRegex(ValueError, "exception"):
            self.q.ack("1", self.rid, done=True)
        with self.assertRaisesRegex(ValueError, "running session"):
            self.q.exempt("0", self.rid, True)      # the owner is not a participant
        with self.assertRaisesRegex(ValueError, "running session"):
            self.q.exempt("nope", self.rid, True)
        with self.assertRaises(ValueError):
            self.q.exempt("2", self.rid, "yes")
        with self.assertRaisesRegex(ValueError, "no longer current"):
            self.q.exempt("2", "other", True)

    def test_revoke_holds_again_and_only_an_ack_with_the_new_revision_restores_the_hold(self):
        for sid in ("1", "2"):
            self.q.ack(sid, self.rid, done=True)
        self.q.exempt("3", self.rid, True)
        self.assertEqual(self.settle()["phase"], "ready")
        self.q.exempt("3", self.rid, False)
        p = self.part("3")
        self.assertEqual((p["exempt"], p["status"], p["rev"]), ("returning", "finishing", 2))
        self.assertTrue(self.q.snapshot()["active"]["shared"])          # still shared until it confirms
        self.assertTrue(self.q.blocked("3"))
        with self.assertRaises(qw.QuietBlocked):
            with self.q.operation("3", "tool"):
                pass
        for stale in (None, 1, True, "2"):
            with self.assertRaisesRegex(ValueError, "revision=2"):
                self.q.ack("3", self.rid, done=True, revision=stale)
        self.q.ack("3", self.rid, done=False, detail="stopping benchmark helper")   # progress needs no revision
        # It was working by the operator's choice; its progress must not cost the owner the window.
        self.assertEqual(self.q.snapshot()["active"]["phase"], "ready")
        self.q.ack("3", self.rid, done=True, revision=2)
        p = self.part("3")
        self.assertEqual(p["status"], "waiting"); self.assertNotIn("exempt", p)
        self.assertFalse(self.q.snapshot()["active"]["shared"])
        self.assertIn("exempt_revoked", [e["kind"] for e in self.q.snapshot()["events"]])

    def test_regrant_bumps_revision_so_an_ack_for_the_earlier_return_is_refused(self):
        self.q.exempt("1", self.rid, True); self.q.exempt("1", self.rid, False)
        self.q.exempt("1", self.rid, True); self.q.exempt("1", self.rid, False)
        with self.assertRaisesRegex(ValueError, "revision=4"):
            self.q.ack("1", self.rid, done=True, revision=2)
        self.q.ack("1", self.rid, done=True, revision=4)

    def test_operator_confirmation_needs_no_revision(self):
        self.q.exempt("1", self.rid, True); self.q.exempt("1", self.rid, False)
        self.q.ack("1", self.rid, done=True, operator=True)
        self.assertEqual(self.part("1")["status"], "waiting")

    def test_restart_of_the_exempt_runtime_drops_the_exception(self):
        self.q.exempt("1", self.rid, True)
        self.rows[1].pid = 999
        self.q.tick()
        p = self.part("1")
        self.assertEqual(p["status"], "unknown"); self.assertNotIn("exempt", p)
        self.assertTrue(self.q.blocked("1"))
        with self.assertRaisesRegex(ValueError, "running session"):
            self.q.exempt("1", self.rid, True)

    def test_daemon_restart_keeps_the_exception_of_the_same_runtime(self):
        self.q.exempt("1", self.rid, True)
        q = self.coordinator()
        self.assertEqual(self.part("1", q)["exempt"], "allowed")
        self.assertEqual(q.blocked("1"), "")

    def test_next_window_does_not_inherit_the_exception(self):
        self.q.request("2", "Next benchmark")
        for sid in ("2", "3"):
            self.q.ack(sid, self.rid, done=True)
        self.q.exempt("1", self.rid, True)
        self.settle()
        self.q.release("0", self.rid, finished=True)
        a = self.q.snapshot()["active"]
        self.assertEqual(a["owner"], "2")
        self.assertFalse(a["shared"])
        self.assertTrue(all("exempt" not in p and "rev" not in p for p in a["participants"]))
        self.assertTrue(self.q.blocked("1"))

    def test_owner_release_ignores_exempt_jobs_but_not_unattributed_or_owner_work(self):
        for sid in ("1", "2"):
            self.q.ack(sid, self.rid, done=True)
        self.q.exempt("3", self.rid, True)
        self.settle()
        self.q.external = [{"session": "", "kind": "stack job a"}]
        with self.assertRaisesRegex(ValueError, "managed work"):
            self.q.release("0", self.rid, finished=True)
        self.q.external = [{"session": "3", "kind": "stack job b"}]
        with self.q.operation("3", "tool"):
            self.q.release("0", self.rid, finished=True)
        self.assertIsNone(self.q.snapshot()["active"])

    def test_unattributed_job_still_holds_draining_with_an_exception(self):
        for sid in ("1", "2"):
            self.q.ack(sid, self.rid, done=True)
        self.q.exempt("3", self.rid, True)
        self.q.external = [{"session": "", "kind": "stack job a"}]
        self.assertEqual(self.settle()["phase"], "draining")
        self.q.external = [{"session": "3", "kind": "stack job a"}]
        self.assertEqual(self.settle()["phase"], "ready")

    def test_failed_save_rolls_back_the_exception(self):
        with mock.patch.object(common, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.q.exempt("1", self.rid, True)
        self.assertNotIn("exempt", self.part("1"))
        self.assertTrue(self.q.blocked("1"))


class ExemptDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='quiet-exempt-')
        self.addCleanup(self.tmp.cleanup)
        self.manager = sessions.Manager(background=False)
        self.addCleanup(self.manager.close)
        self.rows = [sessions.Session(id='e000000' + str(i), project='demo', title='Demo',
                                      path=_env.PP, pid=100+i, state='waiting', idle=True,
                                      runtime='direct', agent='codex', claude_mode='none') for i in range(3)]
        self.manager.sessions = {s.id: s for s in self.rows}
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True, path=os.path.join(self.tmp.name, 'quiet.json'))
        for s in self.rows:
            Path(s.box, 'piper').mkdir(parents=True, exist_ok=True)
        stack = mock.patch.object(self.manager, '_stack_up', return_value=False)
        stack.start(); self.addCleanup(stack.stop)
        self.manager._last_key = mock.Mock(return_value={'key_at': 0, 'enter_at': 0})
        self.manager.type_text = mock.Mock(return_value=True)
        self.rid = self.manager.quiet.request(self.rows[0].id, 'Tests')['active']['id']

    def notices(self):
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        return {c.args[0].id: c.args[1] for c in self.manager.type_text.call_args_list}

    def test_grant_and_revoke_notify_the_session_and_owner_with_revision_controls(self):
        self.notices()
        self.manager.quiet.exempt(self.rows[1].id, self.rid, True)
        sent = self.notices()
        self.assertIn('gave this session an exception', sent[self.rows[1].id])
        self.assertNotIn('"action": "ack"', sent[self.rows[1].id])
        self.assertIn('shared, not quiet', sent[self.rows[0].id])
        self.assertNotIn(self.rows[2].id, sent)            # an unrelated participant is not re-notified
        self.assertEqual(self.notices(), {})                # delivered once per change
        self.manager.quiet.exempt(self.rows[1].id, self.rid, False)
        msg = self.notices()[self.rows[1].id]
        self.assertIn('revision=2', msg)
        controls = [json.loads(l)['quiet'] for l in msg.splitlines() if l.startswith('{"quiet":')]
        self.assertIn({'action': 'ack', 'id': self.rid, 'finished': True, 'revision': 2}, controls)
        # the exact legacy control from the notice is accepted through piper_session
        self.manager.quiet_tool(self.rows[1], 'quiet_ack', {'id': self.rid, 'finished': True, 'revision': 2})
        self.assertEqual(self.manager.quiet.snapshot()['active']['participants'][0]['status'], 'waiting')

    def test_native_hint_opens_for_the_exempt_runtime_only(self):
        self.manager.quiet.ack(self.rows[2].id, self.rid, done=True)
        self.manager.quiet.exempt(self.rows[1].id, self.rid, True)
        self.manager._quiet_tick()
        hint = lambda s: json.loads(Path(s.box, 'piper/quiet.json').read_text())
        self.assertEqual(hint(self.rows[1])['blocked'], '')
        self.assertTrue(hint(self.rows[2])['blocked'])

    def test_agents_have_no_path_to_grant_an_exception(self):
        for args in ({'action': 'exempt', 'id': self.rid, 'session': self.rows[1].id, 'allow': True},):
            with self.assertRaises(ValueError):
                self.manager.settings_tool(self.rows[1], 'piper_session', {'quiet': args})
        with self.assertRaises(ValueError):
            self.manager.quiet_tool(self.rows[1], 'quiet_exempt', {'id': self.rid, 'allow': True})

    def test_stack_job_is_attributed_only_to_the_runtime_that_was_granted_access(self):
        s = self.rows[1]
        s.stack_since = 1000.4

        class Prov:
            def session_labels(self, sid):
                return ['agent:' + sid, 'agent-old:' + sid]
        prov = Prov()
        job = lambda **k: {'id': 'j', 'state': 'running', **k}
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent:' + s.id, started=1000), prov), s.id)
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent-old:' + s.id, started=1200), prov), s.id)
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent:' + s.id, started=999), prov), '')   # older runtime
        self.assertEqual(self.manager._quiet_job_session(job(origin='dashboard', started=1200), prov), '')
        self.assertEqual(self.manager._quiet_job_session(job(started=1200), prov), '')                           # no origin
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent:' + s.id, started=True), prov), '')
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent:' + s.id, started=1200)), '',
                         'a provider without session_labels attributes nothing')
        s.stack_since = 0.0
        self.assertEqual(self.manager._quiet_job_session(job(origin='agent:' + s.id, started=1200), prov), '')

    def test_stack_since_survives_session_persistence(self):
        s = sessions.Session(id='e1', stack_since=12.5)
        self.assertEqual(sessions.Session(**{k: getattr(s, k) for k in sessions.Session.FIELDS if hasattr(s, k)}).stack_since, 12.5)
        self.assertEqual(sessions.Session(id='e2', stack_since='x').stack_since, 0.0)

if __name__ == "__main__":
    unittest.main()
