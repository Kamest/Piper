"""Quiet window transitions, admission races, durable recovery and authenticated controls."""
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import _env  # noqa: F401
import common
import features
import quietwindow as qw
import settings


class QuietTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "quiet.json")
        self.time = 100.0
        self.rows = [SimpleNamespace(id=str(i), title="Session " + str(i), created=1, pid=100+i, instance="a",
                                     state="ready", hidden=False, subagents={}, background_tasks={}, box=self.tmp.name) for i in range(5)]
        self.on = True
        self.q = qw.Coordinator(lambda: self.rows, lambda: self.on, path=self.path,
                                clock=lambda: self.time, wall=lambda: self.time + 1000)

    def request(self, owner="0"):
        return self.q.request(owner, "Full build", 15)["active"]["id"]

    def ready(self):
        rid = self.request()
        for s in self.rows[1:]:
            self.q.ack(s.id, rid, done=True)
        self.q.tick()
        self.time += qw.SETTLE_S
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "ready")
        return rid

    def test_activity_is_durable_truthful_and_does_not_log_heartbeats(self):
        rid = self.request()
        self.q.delivery("1", rid, "Sent", sent=True)
        self.q.ack("1", rid, done=False, detail="Stopping compiler")
        self.q.ack("1", rid, done=True, detail="Compiler stopped")
        events = self.q.snapshot()["events"]
        self.assertEqual([e["kind"] for e in events if e.get("session") == "1"],
                         ["delivered", "progress", "waiting"])
        self.assertEqual(events[-1]["detail"], "Compiler stopped")
        for _ in range(30):
            self.q.renew("0", rid)
            self.q.ack("1", rid, done=True, detail="Compiler stopped")
        self.assertEqual(events, self.q.snapshot()["events"])
        self.assertEqual(json.loads(Path(self.path).read_text())["events"], events)
        self.q.ack("1", rid, done=True, detail="Compiler and helper ended")
        updated = self.q.snapshot()["events"]
        self.assertEqual(len(updated), len(events) + 1)
        self.assertEqual(updated[-1]["kind"], "waiting")
        self.assertEqual(updated[-1]["detail"], "Compiler and helper ended")

    def test_activity_rollback_and_restart_do_not_invent_legacy_history(self):
        rid = self.request()
        before = self.q.snapshot()["events"]
        with mock.patch.object(common, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.q.ack("1", rid, done=True, detail="Ended")
        self.assertEqual(before, self.q.snapshot()["events"])
        self.q.ack("1", rid, done=True, detail="Ended")
        q = qw.Coordinator(lambda: self.rows, lambda: True, path=self.path,
                           clock=lambda: self.time, wall=lambda: self.time + 1000)
        self.assertEqual(q.snapshot()["events"][-1]["kind"], "recovering")
        self.assertEqual(q.snapshot()["events"][:-1], self.q.snapshot()["events"])
        doc = json.loads(Path(self.path).read_text()); doc.pop("events"); doc.pop("event_seq", None)
        Path(self.path).write_text(json.dumps(doc))
        legacy = qw.Coordinator(lambda: self.rows, lambda: True, path=self.path)
        self.assertFalse(any(e["kind"] in ("requested", "waiting", "ready") for e in legacy.snapshot()["events"]))

    def test_observed_background_work_is_not_labelled_an_agent_report(self):
        rid = self.request()
        self.q.ack("1", rid, done=True, detail="Ended")
        self.rows[1].background_tasks = {"task": {}}
        self.time += 1
        self.q.tick()
        participant = self.q.snapshot()["active"]["participants"][0]
        self.assertEqual(participant["status"], "finishing")
        self.assertEqual(participant["confirmed_by"], "Piper observed")
        self.assertEqual(participant["at"], self.time + 1000)
        event = next(e for e in reversed(self.q.snapshot()["events"]) if e["kind"] == "progress")
        self.assertEqual(event["source"], "Piper observed")

    def test_activity_operator_confirmation_retention_and_resume(self):
        rid = self.request()
        self.q.ack("1", rid, done=True, detail="Inspected", operator=True)
        p = self.q.snapshot()["active"]["participants"][0]
        self.assertEqual(p["confirmed_by"], "operator")
        self.assertEqual(self.q.snapshot()["events"][-1]["source"], "operator")
        for i in range(250):
            self.q.ack("2", rid, done=False, detail="Progress " + str(i))
        self.assertLessEqual(len([e for e in self.q.snapshot()["events"] if e["window"] == rid]), 200)
        self.assertGreater(self.q.snapshot()["event_dropped"].get(rid, 0), 0)
        self.q.release("0", rid, finished=True)
        self.q.resume_delivery("1", rid, "Continue notice sent", sent=True)
        self.assertEqual(self.q.snapshot()["events"][-1]["kind"], "resume_sent")
        seq = [e["seq"] for e in self.q.snapshot()["events"]]
        self.assertEqual(seq, sorted(set(seq)))

    def test_default_idle_and_feature_disabled(self):
        self.assertIsNone(self.q.snapshot()["active"])
        self.assertFalse(self.q.blocked("1"))
        self.on = False
        with self.assertRaisesRegex(ValueError, "off"):
            self.request()
        self.assertFalse(os.path.exists(self.path))

    def test_start_admission_preserves_resume_and_cancellation_paths(self):
        class Manager:
            quiet = self.q
            _stop_event = threading.Event()
            _canceled = mock.Mock(return_value=False)
            @qw.managed("start")
            def start(inner, session):
                return session.state
        m = Manager()
        for state in ("ready", "exited", "preflight"):
            self.rows[1].state = state
            self.assertEqual(m.start(self.rows[1]), state)
        m._canceled.return_value = True
        self.assertIsNone(m.start(self.rows[1]))
        self.assertEqual(self.q.operations, {})

    def test_start_cancelled_while_waiting_never_dispatches(self):
        self.request()
        session = self.rows[1]
        session.phase = ""
        class Manager:
            quiet = self.q
            _stop_event = threading.Event()
            _canceled = mock.Mock(side_effect=[False, True])
            def _set(inner, s, state, phase, detail):
                s.state, s.phase = state, phase
            @qw.managed("start")
            def start(inner, s):
                self.fail("A held start must not dispatch")
        self.assertIsNone(Manager().start(session))
        self.assertEqual(session.phase, "quiet")

    def test_five_sessions_wait_for_every_ack_and_settling(self):
        rid = self.request()
        self.assertEqual(len(self.q.snapshot()["active"]["participants"]), 4)
        for s in self.rows[1:4]:
            self.q.ack(s.id, rid, done=True)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "draining")
        self.q.ack("4", rid, done=True)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "settling")
        self.time += qw.SETTLE_S
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "ready")

    def test_long_drain_with_explicit_owner_supervision_eventually_grants(self):
        rid = self.request()
        self.rows[4].background_tasks = {"build": "finishing"}
        for row in self.rows[1:4]:
            self.q.ack(row.id, rid, done=True)
        for _ in range(8):
            self.time += 55
            self.q.renew("0", rid)
            self.q.ack("4", rid, done=False, detail="Finishing existing build; no new work")
            self.assertEqual(self.q.snapshot()["active"]["phase"], "draining")
            self.assertTrue(self.q.blocked("0"))
        with self.assertRaises(ValueError): self.q.ack("4", rid, done=True)
        self.rows[4].background_tasks = {}
        self.q.ack("4", rid, done=True)
        self.q.tick()
        self.time += qw.SETTLE_S
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "ready")
        self.assertEqual(self.q.snapshot()["active"]["deadline"], self.time + 1000 + 900)

    def test_unresponsive_participant_stays_blocking_and_cancellable_after_warning(self):
        rid = self.request()
        for _ in range(3):
            self.time += 55
            self.q.renew("0", rid)
        a = self.q.snapshot()["active"]
        self.assertEqual(a["phase"], "draining")
        self.assertTrue(all(p["stale"] for p in a["participants"]))
        self.assertTrue(self.q.blocked("0"))
        self.q.cancel("0", rid)
        self.assertIsNone(self.q.snapshot()["active"])

    def test_progress_refresh_does_not_ack_or_hide_owner_lease_expiry(self):
        rid = self.request()
        self.time += 60
        self.q.renew("0", rid)
        self.time += 60
        self.q.renew("0", rid)
        self.q.ack("1", rid, done=False, detail="Stopping existing child job")
        parts = self.q.snapshot()["active"]["participants"]
        self.assertFalse(parts[0]["stale"])
        self.assertTrue(parts[1]["stale"])
        self.assertEqual(parts[0]["detail"], "Stopping existing child job")
        self.assertEqual(parts[0]["status"], "finishing")
        self.time += qw.LEASE_S
        self.q.ack("1", rid, done=False, detail="Still finishing")
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")
        with self.assertRaises(ValueError): self.q.renew("0", rid)

    def test_participant_ends_during_preparation_requires_check_without_waiting_forever(self):
        rid = self.request()
        self.rows[1].state = "exited"
        self.q.tick()
        a = self.q.snapshot()["active"]
        self.assertEqual(a["phase"], "recovering")
        self.assertIn("ended or restarted", a["problem"])
        self.assertTrue(self.q.blocked("0"))
        self.q.release("0", rid, finished=True)

    def test_owner_can_run_many_jobs_without_auto_release(self):
        rid = self.ready()
        for _ in range(2):
            self.q.work("0", rid, True)
            with self.q.operation("0", "build"):
                with self.assertRaisesRegex(ValueError, "still running"):
                    self.q.release("0", rid, finished=True)
            self.q.work("0", rid, False)
            self.assertEqual(self.q.snapshot()["active"]["id"], rid)
        with self.assertRaisesRegex(ValueError, "Confirm"):
            self.q.release("0", rid)
        self.q.release("0", rid, finished=True)
        self.assertIsNone(self.q.snapshot()["active"])
        self.assertFalse(self.q.blocked("1"))

    def test_implicit_request_supports_three_hours_until_owner_releases(self):
        rid = self.q.request("0", "Long data import")["active"]["id"]
        for s in self.rows[1:]:
            self.q.ack(s.id, rid, done=True)
        self.q.tick()
        self.time += qw.SETTLE_S
        self.q.tick()
        self.q.work("0", rid, True)
        for _ in range(181):
            self.time += 60
            self.q.renew("0", rid)
            a = self.q.snapshot()["active"]
            self.assertEqual(a["phase"], "working")
            self.assertIsNone(a.get("deadline"))
            self.assertTrue(self.q.blocked("1"))
        self.q.work("0", rid, False)
        self.q.release("0", rid, finished=True)
        self.assertFalse(self.q.blocked("1"))

    def test_pregrant_recovery_can_cancel_but_ever_granted_window_needs_checked_release(self):
        rid = self.request()
        self.time += qw.LEASE_S
        self.q.tick()
        self.q.cancel("0", rid)
        self.assertIsNone(self.q.snapshot()["active"])
        rid = self.ready()
        self.q.work("0", rid, True)
        self.q.work("0", rid, False)
        self.time += qw.LEASE_S
        self.q.tick()
        with self.assertRaises(ValueError): self.q.cancel("0", rid)
        self.assertTrue(self.q.blocked("1"))
        self.q.release("0", rid, finished=True)

    def test_drain_delivery_persistence_and_old_request_cannot_change_next_window(self):
        rid = self.request()
        with mock.patch.object(self.q, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.q.delivery("1", rid, "Sent", sent=True)
        self.assertIsNone(self.q.snapshot()["active"]["participants"][0]["delivered_at"])
        self.q.delivery("1", rid, "Sent", sent=True)
        self.assertEqual(self.q.snapshot()["active"]["participants"][0]["delivered_at"], self.time + 1000)
        self.q.cancel("0", rid)
        new = self.request()
        self.q.delivery("1", rid, "Late old delivery", sent=True)
        self.assertEqual(self.q.snapshot()["active"]["id"], new)
        self.assertIsNone(self.q.snapshot()["active"]["participants"][0]["delivered_at"])

    def test_inflight_operation_blocks_ack_and_grant(self):
        with self.q.operation("1", "test"):
            rid = self.request()
            with self.assertRaisesRegex(ValueError, "Finish all"):
                self.q.ack("1", rid, done=True)
        self.q.ack("1", rid, done=True)
        with self.assertRaisesRegex(ValueError, "Quiet window"):
            with self.q.operation("2", "build"):
                self.fail("must not start")

    def test_background_jobs_and_subagents_must_finish(self):
        rid = self.request()
        for field in ("background_tasks", "subagents"):
            setattr(self.rows[1], field, {"job": "shell"})
            with self.assertRaises(ValueError):
                self.q.ack("1", rid, done=True)
            setattr(self.rows[1], field, {})
        self.q.ack("1", rid, done=True)

    def test_lost_lease_never_resumes_others_or_revives(self):
        rid = self.ready()
        self.q.work("0", rid, True)
        self.time += qw.LEASE_S
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")
        self.assertTrue(self.q.blocked("1"))
        for fn in (lambda: self.q.renew("0", rid), lambda: self.q.work("0", rid, True)):
            with self.assertRaises(ValueError): fn()
        self.q.release("gui", rid, admin=True, finished=True)

    def test_owner_renew_keeps_original_maximum_duration(self):
        rid = self.ready()
        for _ in range(16):
            self.time += 59
            if self.time < 100 + qw.SETTLE_S + 900:
                self.q.renew("0", rid)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")

    def test_changed_runtime_cannot_ack_renew_or_release(self):
        rid = self.request()
        self.rows[1].pid = 900
        with self.assertRaises(ValueError): self.q.ack("1", rid, done=True)
        self.rows[0].quiet_generation = "other"
        with self.assertRaises(ValueError): self.q.renew("0", rid)
        with self.assertRaises(ValueError): self.q.release("0", rid, finished=True)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")

    def test_restart_keeps_holds_and_requires_check(self):
        rid = self.ready()
        q = qw.Coordinator(lambda: self.rows, lambda: True, path=self.path)
        self.assertEqual(q.snapshot()["active"]["phase"], "recovering")
        self.assertTrue(q.blocked("0"))
        with self.assertRaises(ValueError): q.renew("0", rid)

    def test_fifo_dedup_and_stale_release(self):
        rid = self.ready()
        self.q.request("1", "Import", 10)
        self.q.request("2", "Local model", 30)
        self.q.request("1", "Duplicate", 15)
        self.assertEqual(len(self.q.snapshot()["queue"]), 2)
        self.q.release("0", rid, finished=True)
        a = self.q.snapshot()["active"]
        self.assertEqual(a["owner"], "1")
        self.assertEqual(a["phase"], "draining")
        self.assertTrue(self.q.blocked("0"))
        self.assertEqual(len(a["participants"]), 4)
        with self.assertRaises(ValueError): self.q.release("0", rid, finished=True)

    def test_unreachable_owner_skipped_in_queue(self):
        rid = self.request()
        self.q.request("1", "Import", 10)
        self.q.request("2", "Build", 15)
        self.rows[1].state = "exited"
        self.q.cancel("0", rid)
        self.assertEqual(self.q.snapshot()["active"]["owner"], "2")

    def test_new_session_and_changed_ack_are_not_silent_consent(self):
        rid = self.ready()
        self.rows.append(SimpleNamespace(id="new", title="New", created=1, pid=900, instance="a", state="working"))
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")
        self.assertEqual(self.q.snapshot()["active"]["participants"][-1]["status"], "finishing")

    def test_persist_failure_does_not_grant_or_release(self):
        with mock.patch.object(common, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.request()
        self.assertIsNone(self.q.snapshot()["active"])
        rid = self.ready()
        with mock.patch.object(common, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.q.release("0", rid, finished=True)
        self.assertIsNotNone(self.q.snapshot()["active"])
        self.assertTrue(self.q.blocked("1"))

    def test_simultaneous_requests_and_work_are_serialized(self):
        barrier = threading.Barrier(3)
        errors = []
        def run(sid):
            try:
                barrier.wait()
                self.q.request(sid, "Build", 15)
            except BaseException as e: errors.append(e)
        ts = [threading.Thread(target=run, args=(str(i),)) for i in (0, 1)]
        for t in ts: t.start()
        barrier.wait()
        for t in ts: t.join()
        self.assertEqual(errors, [])
        self.assertIsNotNone(self.q.snapshot()["active"])
        self.assertEqual(len(self.q.snapshot()["queue"]), 1)

    def test_identity_and_input_validation(self):
        for reason, minutes in [("", 15), ("x", True), ("x", 900), (123, 15)]:
            with self.assertRaises(ValueError): self.q.request("0", reason, minutes)
        rid = self.request()
        for fn in (lambda: self.q.ack("0", rid, done=True), lambda: self.q.renew("1", rid),
                   lambda: self.q.release("1", rid, finished=True), lambda: self.q.cancel("1", rid)):
            with self.assertRaises(ValueError): fn()

    def test_bounded_wait_responds_to_release(self):
        rid = self.request()
        self.assertIsNotNone(self.q.wait("1", rid, 0)["active"])
        with self.assertRaises(ValueError): self.q.wait("1", rid, 26)
        t = threading.Thread(target=lambda: self.q.cancel("0", rid))
        t.start()
        self.assertIsNone(self.q.wait("1", rid, 1)["active"])
        t.join()

    def test_participant_background_work_after_ack_invalidates_grant(self):
        self.ready()
        self.rows[1].background_tasks = {"job": "shell"}
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")
        self.assertTrue(self.q.blocked("0"))

    def test_checked_release_after_clock_change_allows_a_fresh_window(self):
        rid = self.ready()
        self.q.wall = lambda: self.time + 1060
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")
        self.q.release("0", rid, finished=True)
        self.ready()

    def test_async_host_jobs_block_grant_and_release_but_not_cancelling_preparation(self):
        rid = self.request()
        self.q.external = [{"session": "", "kind": "stack job test"}]
        for s in self.rows[1:]: self.q.ack(s.id, rid, done=True)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "draining")
        self.q.cancel("0", rid)
        self.q.external = []
        rid = self.ready()
        self.q.external = [{"session": "", "kind": "stack state unavailable"}]
        with self.assertRaises(ValueError): self.q.release("0", rid, finished=True)

    def test_release_retry_is_idempotent_until_another_window_is_selected(self):
        rid = self.ready()
        self.q.release("0", rid, finished=True)
        self.assertIsNone(self.q.release("0", rid, finished=True)["active"])

    def test_manager_tools_use_the_calling_session_and_gate_other_dispatch(self):
        import sessions
        m = _env.manager(self)
        m.quiet = self.q
        m.sessions = {s.id: s for s in self.rows}
        self.rows[1].runtime = "hole"
        owner, participant = self.rows[0], self.rows[1]
        owner.kind = participant.kind = "project"
        for value in (None, False, "true", 1):
            with self.subTest(user_requested=value):
                with self.assertRaisesRegex(ValueError, "user explicitly asks"):
                    m.tool(owner, "quiet_request", {"reason": "Build", "user_requested": value})
                with self.assertRaisesRegex(ValueError, "user explicitly asks"):
                    m.settings_tool(owner, "piper_session", {"quiet": {"action": "request", "reason": "Build", "user_requested": value}})
                self.assertIsNone(self.q.snapshot()["active"])
        result = m.tool(owner, "quiet_request", {"reason": "Build", "session": "1", "user_requested": True})
        self.assertEqual(result["active"]["owner"], "0")
        rid = result["active"]["id"]
        queued = m.settings_tool(participant, "piper_session", {"quiet": {"action": "request", "reason": "Explicit user request", "user_requested": True}})
        self.assertEqual(queued["queue"][0]["owner"], participant.id)
        with self.assertRaises(ValueError): m.tool(participant, "quiet_release", {"id": rid, "finished": True})
        with self.assertRaisesRegex(ValueError, "Quiet window"): m.tool(participant, "git_status", {})
        self.assertTrue(m.tool(participant, "quiet_ack", {"id": rid, "finished": True})["active"])
        with self.assertRaisesRegex(ValueError, "Quiet window"): m._check_limit()
        participant.turn_action_lock, participant.revert_note = threading.Lock(), ""   # a Session has both
        with self.assertRaisesRegex(ValueError, "Quiet window"): m.send(participant.id, "new work")

    def test_start_already_in_progress_can_obtain_pid_before_ack(self):
        self.rows[1].pid = None
        self.rows[1].state = "preflight"
        rid = self.request()
        with self.assertRaises(ValueError): self.q.ack("1", rid, done=True)
        self.rows[1].pid, self.rows[1].state = 222, "ready"
        self.q.ack("1", rid, done=True)
        self.assertEqual(self.q.snapshot()["active"]["participants"][0]["status"], "waiting")

    def test_reused_pid_is_fenced_by_runtime_nonce(self):
        self.rows[0].quiet_generation = "first"
        rid = self.ready()
        self.rows[0].quiet_generation = "second"
        with self.assertRaises(ValueError): self.q.renew("0", rid)
        self.q.tick()
        self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")

    def test_failed_renew_does_not_extend_the_lease_in_memory(self):
        rid = self.ready()
        original = self.q.lease
        self.time += 30
        with mock.patch.object(common, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.q.renew("0", rid)
        self.assertEqual(self.q.lease, original)

    def test_managed_work_is_supervised_but_cannot_extend_the_window_deadline(self):
        rid = self.ready()
        with self.q.operation("0", "long test"):
            self.time += qw.LEASE_S + 1
            self.q.tick()
            self.assertEqual(self.q.snapshot()["active"]["phase"], "ready")
            self.time += 900
            self.q.tick()
            self.assertEqual(self.q.snapshot()["active"]["phase"], "recovering")

    def test_queued_start_is_durable_and_dispatched_once_after_release(self):
        class Manager(qw.QuietWindows):
            def __init__(inner):
                inner.quiet, inner.lock, inner.created = self.q, threading.RLock(), []
            def _check_limit(inner): pass
            def create(inner, project, **kwargs):
                inner.created.append((project, kwargs))
                return SimpleNamespace(id="started")
        m = Manager()
        rid = self.ready()
        body = {"project": "demo", "task": "Build", "request_key": "unique"}
        m.quiet_queue_start(body)
        m.quiet_queue_start(body)
        self.assertEqual(len(json.loads(Path(self.path).read_text())["starts"]), 1)
        m._quiet_dispatch_starts()
        self.assertEqual(m.created, [])
        self.q.release("0", rid, finished=True)
        m._quiet_dispatch_starts()
        m._quiet_dispatch_starts()
        self.assertEqual(len(m.created), 1)
        self.assertEqual(self.q.snapshot()["starts"], [])

    def test_ambiguous_queued_start_is_not_replayed_after_restart(self):
        self.q.doc["starts"] = [{"id": "once", "phase": "checking"}]
        with self.q.lock: self.q._save()
        restarted = qw.Coordinator(lambda: self.rows, lambda: True, path=self.path)
        m = SimpleNamespace(quiet=restarted, lock=threading.RLock(), _check_limit=mock.Mock(), create=mock.Mock())
        qw.QuietWindows._quiet_dispatch_starts(m)
        m.create.assert_not_called()
        self.assertEqual(restarted.snapshot()["starts"][0]["phase"], "uncertain")
        with self.assertRaisesRegex(ValueError, "explicitly confirm"):
            qw.QuietWindows.quiet_cancel_start(m, "once")
        qw.QuietWindows.quiet_cancel_start(m, "once", checked=True)
        self.assertEqual(restarted.snapshot()["starts"], [])

    def test_cancel_of_dispatching_start_is_refused_without_losing_the_record(self):
        entered, finish = threading.Event(), threading.Event()
        class Manager(qw.QuietWindows):
            quiet = self.q
            def create(inner, *args, **kwargs):
                entered.set()
                self.assertTrue(finish.wait(3))
                return SimpleNamespace(id="started")
        m = Manager()
        m.quiet_queue_start({"project": "demo", "request_key": "once"})
        errors = []
        def dispatch():
            try: m._quiet_dispatch_starts()
            except Exception as e: errors.append(e)
        t = threading.Thread(target=dispatch)
        t.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ValueError, "already being started"):
                m.quiet_cancel_start("once")
            self.assertEqual(self.q.snapshot()["starts"][0]["phase"], "checking")
        finally:
            finish.set()
            t.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(self.q.snapshot()["last_start"]["session"], "started")

    def test_queued_worktree_waits_and_is_not_created_twice_on_admission_retry(self):
        class Manager(qw.QuietWindows):
            quiet = self.q
            git_worktree_op = mock.Mock(return_value={"key": "demo@build"})
            create = mock.Mock(side_effect=[qw.QuietBlocked("Quiet window"), SimpleNamespace(id="started")])
        m = Manager()
        rid = self.ready()
        m.quiet_queue_start({"project": "demo", "request_key": "wt", "worktree": {"action": "add", "name": "build", "branch": "feat/build", "new": True}})
        m._quiet_dispatch_starts()
        m.git_worktree_op.assert_not_called()
        self.q.release("0", rid, finished=True)
        m._quiet_dispatch_starts()
        self.assertEqual(self.q.snapshot()["starts"][0]["phase"], "queued")
        m._quiet_dispatch_starts()
        m.git_worktree_op.assert_called_once()
        self.assertEqual(m.create.call_args.args[0], "demo@build")

    def test_gui_worktree_operation_is_gated_before_any_repository_access(self):
        import workbench
        class Manager(workbench.Workbench):
            quiet = self.q
            gui_project = mock.Mock()
        m = Manager()
        self.request()
        with self.assertRaises(qw.QuietBlocked):
            m.git_worktree_op("demo", {"action": "add", "name": "build"})
        m.gui_project.assert_not_called()


class HookTest(unittest.TestCase):
    def test_native_checkpoint_denies_new_work_but_preserves_control_and_cleanup(self):
        script = Path(common.PIPER_DIR) / "sandbox" / "piper-event.py"
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "piper"
            p.mkdir()
            hint = p / "quiet.json"
            hint.write_text(json.dumps({"blocked": "Quiet window q1: finish, acknowledge and wait"}))
            def run(name, args=None):
                r = subprocess.run([sys.executable, str(script), "QuietCheckpoint", str(Path(folder) / "events.jsonl")],
                                   input=json.dumps({"tool_name": name, "tool_input": args or {}}),
                                   text=True, capture_output=True, check=True)
                return json.loads(r.stdout) if r.stdout else None
            for name in ("Bash", "apply_patch", "mcp__piper__http_run"):
                self.assertEqual(run(name)["hookSpecificOutput"]["permissionDecision"], "deny")
            for name in ("mcp__piper__quiet_ack", "mcp__piper__quiet_wait", "mcp__piper__quiet_status", "TaskStop", "TaskOutput"):
                self.assertIsNone(run(name))
            self.assertIsNone(run("write_stdin", {"chars": ""}))
            self.assertIsNotNone(run("write_stdin", {"chars": "start new work"}))
            hint.write_text('{"blocked":""}')
            self.assertIsNone(run("Bash"))
            hint.write_text('{broken')
            self.assertEqual(run("Bash")["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertFalse((Path(folder) / "events.jsonl").exists(), "never retain tool input or commands")


class QuietApiTest(unittest.TestCase):
    """The same coordinator behind the real HTTP authentication and GUI route."""

    setUp = QuietTest.setUp

    def test_real_manager_initializes_coordinator_before_any_background_service(self):
        import sessions
        manager = sessions.Manager(background=False)
        self.addCleanup(manager.close)
        self.assertIsInstance(manager.quiet, qw.Coordinator)
        self.assertIsNone(manager.quiet_summary()["active"])

    def test_controller_without_coordinator_refuses_action_without_500(self):
        import piperd
        from test_piperd import FakeManager, post
        gui = common.mint_token("gui")
        with mock.patch.object(piperd, "MANAGER", FakeManager()):
            code, _, raw = post("/api/quiet", {"action": "request", "session": "0", "reason": "Build"}, token=gui)
            self.assertEqual(code, 409)
            self.assertIn("restart", json.loads(raw)["error"].lower())

    def test_health_reports_actual_initialized_coordinator_and_process(self):
        import piperd
        from test_piperd import FakeManager, get
        gui = common.mint_token("gui")
        manager = FakeManager()
        with mock.patch.object(piperd, "MANAGER", manager):
            code, _, raw = get("/api/ping", token=gui)
            health = json.loads(raw)
            self.assertEqual(code, 200)
            self.assertEqual(health["pid"], os.getpid())
            self.assertFalse(health["capabilities"]["quiet"])
            manager.quiet = self.q
            code, _, raw = get("/api/ping", token=gui)
            self.assertTrue(json.loads(raw)["capabilities"]["quiet"])

    def test_missing_coordinator_health_reports_loaded_code_origin(self):
        import piperd
        from test_piperd import FakeManager, get
        class NewManager(FakeManager):
            def __init__(self):
                super().__init__()
                self._quiet_init()
            def _quiet_init(self):
                pass
        gui = common.mint_token("gui")
        manager = NewManager()
        with mock.patch.object(piperd, "MANAGER", manager):
            code, _, raw = get("/api/ping", token=gui)
            health = json.loads(raw)
            self.assertEqual(code, 200)
            detail = health["quiet_diagnostics"]
            self.assertTrue(detail["manager"].endswith("NewManager"))
            self.assertEqual(detail["module_file"], __file__)
            self.assertEqual(detail["initializer_file"], __file__)
            self.assertEqual(detail["quiet_initializer_file"], __file__)
            self.assertTrue(detail["initializer_calls_quiet"])
            self.assertTrue(detail["class_has_quiet_initializer"])
            self.assertRegex(detail["initializer_hash"], r"^[a-f0-9]{64}$")
            self.assertFalse(health["capabilities"]["quiet"])
            manager.quiet = self.q
            code, _, raw = get("/api/ping", token=gui)
            self.assertNotIn("quiet_diagnostics", json.loads(raw))

    def test_gui_request_cancel_and_scope(self):
        import piperd
        from test_piperd import FakeManager, post
        manager = FakeManager()
        manager.quiet = self.q
        gui, tty = common.mint_token("gui"), common.mint_token("tty")
        with mock.patch.object(piperd, "MANAGER", manager):
            body = {"action": "request", "session": "0", "reason": "Build"}
            self.assertEqual(post("/api/quiet", body)[0], 401)
            self.assertEqual(post("/api/quiet", body, token=tty)[0], 403)
            self.assertEqual(post("/api/quiet", body, token=gui, origin="https://evil.invalid")[0], 403)
            code, _, raw = post("/api/quiet", body, token=gui)
            self.assertEqual(code, 200, raw)
            rid = json.loads(raw)["active"]["id"]
            code, _, raw = post("/api/quiet", {"action": "cancel", "id": rid}, token=gui)
            self.assertEqual(code, 200, raw)
            self.assertIsNone(json.loads(raw)["active"])


class FeatureTest(unittest.TestCase):
    def setUp(self):
        self.saved = Path(features.FILE).read_bytes()
        self.addCleanup(lambda: Path(features.FILE).write_bytes(self.saved))
        self.addCleanup(features._CACHE.clear)

    def test_on_in_all_presets_and_existing_basic_without_key(self):
        for preset in features.PRESETS:
            self.assertTrue(features.preset(preset)["quiet"])
        self.assertTrue(features._normalise({"base": "basic", "modules": {}})["modules"]["quiet"])
        self.assertFalse(features._normalise({"base": "basic", "modules": {"quiet": False}})["modules"]["quiet"])
        f = next(f for sec in settings.REGISTRY for f in sec.fields if sec.id == "features" and f.key == "quiet")
        self.assertTrue(f.default)

    def test_disable_refused_through_both_settings_paths_during_hold(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(qw, "FILE", os.path.join(folder, "quiet.json")):
            common.write_json(qw.FILE, {"active": {"id": "a"}, "queue": []})
            with self.assertRaisesRegex(ValueError, "Release or cancel"):
                features.save({"quiet": False})
            with self.assertRaises(settings.SettingsError):
                settings.update("features", {"quiet": False})
            common.write_json(qw.FILE, {"active": None, "queue": []})
            features.save({"quiet": False})
            self.assertFalse(features.on("quiet"))
            settings.update("features", {"quiet": True})
            self.assertTrue(features.on("quiet"))
