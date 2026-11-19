"""Dev stacks a session started are stopped after it ends; a worktree sub-stack's data is released later
(stackreap.py). Never a stack it did not start, one that ran before it, one another session uses, a main
checkout's data or anything when the provider cannot say."""
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import _env  # noqa: F401
import common
import settings
import stackreap
import stackview
import sessions

LABEL = "sandbox:piper-%s"


class FakeProvider:
    def __init__(self):
        self.job_rows, self.running, self.calls = [], set(), []
        self.release_error = None

    def session_labels(self, sid):
        return [LABEL % sid]

    def jobs(self):
        return list(self.job_rows)

    def running_stacks(self):
        return [{"root": r, "running": 1, "total": 1} for r in sorted(self.running)]

    def verb(self, root, verb, service=""):
        self.calls.append((verb, root))
        self.running.discard(root)
        return {"id": "a1b2c3"}

    def release_data(self, root):
        if self.release_error:
            raise stackview.StackError(self.release_error)
        self.calls.append(("release", root))
        return {"id": "d4e5f6"}


class Host(stackreap.StackReap):
    """Just what StackReap uses of sessions.Manager."""

    def __init__(self, prov, links, uses):
        self.sessions, self.policy = {}, {"stack_stop_on_end": True, "stack_data_cleanup": "3d"}
        self._stop_event = threading.Event()
        self.prov, self.links, self.uses = prov, links, uses
        self._reap_init()

    def _stack(self):
        return self.prov

    def stack_worktree(self, path):
        root = self.uses.get(os.path.realpath(path))
        if root == "error":
            raise stackview.StackError("down")
        return {"root": root} if root else None

    def worktree_link(self, path):
        return self.links.get(os.path.realpath(path))


class StackReapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(dir=_env.ROOT)
        self.base = os.path.realpath(tmp)
        self.main, self.wt, self.other = (os.path.join(self.base, n) for n in ("main", "wt", "other"))
        for d in (self.main, self.wt, self.other):
            os.makedirs(d)
        self.file = os.path.join(self.base, "stack-reap.json")
        mock.patch.object(stackreap, "REAP_FILE", self.file).start()
        mock.patch("features.on", return_value=True).start()
        self.audit = mock.patch.object(stackreap, "audit").start()
        self.addCleanup(mock.patch.stopall)
        self.prov = FakeProvider()
        links = {self.wt: {"top": self.wt, "main_top": self.main, "main": self.main}}
        self.uses = {}
        self.m = Host(self.prov, links, self.uses)
        self.t0 = 1_000_000.0

    def session(self, sid, path, state="exited", kind="project"):
        s = SimpleNamespace(id=sid, path=path, state=state, kind=kind)
        self.m.sessions[sid] = s
        return s

    def start(self, s, running=()):
        """the spawn's record, as _reap_started writes it once its snapshot is back"""
        with mock.patch.object(stackreap.time, "time", return_value=self.t0), \
                mock.patch.object(stackreap.threading, "Thread") as thread:
            self.prov.running = set(running)
            self.m._reap_started(s)
            thread.call_args.kwargs["target"]()   # the snapshot, inline
        self.assertEqual(common.read_json(self.file, {})["sessions"][s.id]["running"], sorted(running))

    def job(self, sid, root, verb="up", at=None, origin=None):
        self.prov.job_rows.append({"id": "%06x" % len(self.prov.job_rows), "verb": verb, "root": root, "state": "done",
                                   "origin": origin if origin is not None else LABEL % sid, "started": at or self.t0 + 10})
        self.prov.running.add(root)

    def end(self, s, at=None):
        s.state = "exited"
        with mock.patch.object(stackreap.time, "time", return_value=at or self.t0 + 100):
            self.m._reap_ended(s)

    def tick(self, at):
        with mock.patch.object(stackreap.time, "time", return_value=at):
            self.m._reap_tick(at)

    def events(self, name):
        return [c.args[0] for c in self.audit.call_args_list if c.args[0]["event"] == name]

    def test_stops_only_what_the_session_started_after_the_grace(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s, running=[self.main])          # the main stack ran before: never this session's
        self.job("s1", self.main, "restart")
        self.job("s1", self.wt, "up")
        self.job("other", self.other, "up", origin=LABEL % "other")   # another session's stack
        self.end(s)
        self.tick(self.t0 + 120)                    # within the grace: a resume keeps its stack
        self.assertEqual(self.prov.calls, [])
        self.tick(self.t0 + 100 + stackreap.GRACE_S + 1)
        self.assertEqual(self.prov.calls, [("stop", self.wt)])
        self.assertEqual([e["root"] for e in self.events("stack-reap-kept")], [self.main])
        self.assertNotIn("s1", common.read_json(self.file, {})["sessions"])

    def test_a_session_running_again_keeps_its_stack(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.end(s)
        s.state = "working"                          # resumed within the grace
        self.tick(self.t0 + 1000)
        self.assertEqual(self.prov.calls, [])
        self.assertIn("s1", common.read_json(self.file, {})["sessions"])

    def test_never_stops_a_stack_in_use_taken_over_or_unknown(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.job("s1", self.other)
        self.job("s1", self.main)
        self.job("x", self.other, "start", at=self.t0 + 50, origin="dashboard")   # the user started it again
        self.session("s2", self.main + "/sub", state="working")
        self.uses[os.path.realpath(self.main + "/sub")] = self.main                # s2 works on the main stack
        self.end(s)
        self.tick(self.t0 + 1000)
        self.assertEqual(self.prov.calls, [("stop", self.wt)])
        kept = {e["root"]: e["why"] for e in self.events("stack-reap-kept")}
        self.assertIn("someone else", kept[self.other])
        self.assertIn("s2", kept[self.main])

    def test_unknown_use_by_another_session_and_unknown_start_state_keep_the_stack(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.session("s2", self.other, state="working")
        self.uses[self.other] = "error"
        self.end(s)
        self.tick(self.t0 + 1000)
        self.assertEqual(self.prov.calls, [])
        s3 = self.session("s3", self.wt, state="working")
        with mock.patch.object(stackreap.threading, "Thread"), \
                mock.patch.object(stackreap.time, "time", return_value=self.t0 + 1500):
            self.m._reap_started(s3)                 # its snapshot never came back
        self.job("s3", self.wt, at=self.t0 + 2000)
        self.m.sessions.pop("s2")
        self.end(s3, at=self.t0 + 2100)
        self.tick(self.t0 + 3000)
        self.assertEqual(self.prov.calls, [])
        self.assertIn("unknown", self.events("stack-reap-kept")[-1]["why"])

    def test_a_worktree_stack_set_up_through_piper_worktree_stack_is_the_sessions_own(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.m._reap_adopted(s, self.wt)            # adopt ran under the provider's own label
        self.prov.running.add(self.wt)
        self.end(s)
        self.tick(self.t0 + 1000)
        self.assertEqual(self.prov.calls, [("stop", self.wt)])

    def test_stop_off_keeps_stacks_running(self):
        self.m.policy["stack_stop_on_end"] = False
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.end(s)
        self.tick(self.t0 + 1000)
        self.assertEqual(self.prov.calls, [])

    def test_sub_stack_data_is_released_when_due_never_a_main_stack(self):
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.job("s1", self.main)
        self.end(s)
        self.tick(self.t0 + 1000)
        data = common.read_json(self.file, {})["data"]
        self.assertEqual(list(data), [self.wt])      # the main checkout's stack: never armed
        self.assertEqual(data[self.wt]["due"], self.t0 + 1000 + 3 * 86400)
        self.tick(self.t0 + 2 * 86400)
        self.assertNotIn(("release", self.wt), self.prov.calls)
        self.tick(self.t0 + 4 * 86400)
        self.assertIn(("release", self.wt), self.prov.calls)
        self.assertEqual(common.read_json(self.file, {})["data"], {})

    def test_data_release_waits_for_a_session_working_there_and_a_new_one_cancels_it(self):
        self.m.policy["stack_data_cleanup"] = "end"
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.end(s)
        self.tick(self.t0 + 1000)
        s2 = self.session("s2", os.path.join(self.wt, "sub"), state="working")
        self.tick(self.t0 + 1100)                    # a session works there: not now
        self.assertNotIn(("release", self.wt), self.prov.calls)
        self.start(s2)                               # a new runtime in that worktree cancels the release
        self.assertEqual(common.read_json(self.file, {})["data"], {})

    def test_data_kept_when_off_unsupported_running_or_refused(self):
        self.m.policy["stack_data_cleanup"] = "end"
        s = self.session("s1", self.wt, state="working")
        self.start(s)
        self.job("s1", self.wt)
        self.end(s)
        self.tick(self.t0 + 1000)
        self.prov.running.add(self.wt)               # running again within the hour: look later
        self.tick(self.t0 + 1100)
        self.assertIn(self.wt, common.read_json(self.file, {})["data"])
        self.prov.running.clear()
        self.prov.release_error = "not this worktree's own data"
        self.tick(self.t0 + 1200)
        self.assertEqual(self.events("stack-reap-data-failed")[0]["root"], self.wt)
        self.assertEqual(common.read_json(self.file, {})["data"], {})
        self.m.policy["stack_data_cleanup"] = "off"
        self.m._reap_arm_data("s1", self.wt, self.t0, stackreap.CLEANUP["end"])
        self.tick(self.t0 + 1300)
        self.assertEqual(self.events("stack-reap-data-kept")[-1]["why"], "data cleanup is off")
        self.m.policy["stack_data_cleanup"] = "end"
        self.prov.release_data = None
        self.m._reap_arm_data("s1", self.wt, self.t0, 0)
        self.tick(self.t0 + 1400)
        self.assertIn("cannot release", self.events("stack-reap-data-kept")[-1]["why"])
        self.assertEqual([c for c in self.prov.calls if c[0] == "release"], [])

    def test_provider_without_the_optional_methods_is_a_no_op(self):
        self.prov = self.m.prov = SimpleNamespace(jobs=lambda: [], verb=mock.Mock())
        s = self.session("s1", self.wt, state="working")
        with mock.patch.object(stackreap.threading, "Thread"):
            self.m._reap_started(s)
        self.end(s)
        self.tick(self.t0 + 1000)
        self.prov.verb.assert_not_called()
        self.assertEqual(common.read_json(self.file, {})["sessions"], {})


class WiringTest(unittest.TestCase):
    def test_manager_has_the_reaper_and_settings_have_safe_defaults(self):
        self.assertTrue(issubclass(sessions.Manager, stackreap.StackReap))
        fields = {f.key: f for f in settings.section("sandbox").fields}
        self.assertIs(fields["stack_stop_on_end"].default, True)
        self.assertEqual(fields["stack_data_cleanup"].default, "3d")
        self.assertEqual({c[0] for c in fields["stack_data_cleanup"].choices}, set(stackreap.CLEANUP))

    def test_exit_records_the_end_and_the_spawn_its_start(self):
        with open(sessions.__file__) as f:
            src = f.read()
        exit_body = src[src.index("    def _on_exit("):src.index("    def ", src.index("    def _on_exit(") + 10)]
        self.assertIn("self._reap_ended(s)", exit_body)
        self.assertIn("self._reap_started(s)", src)
        self.assertIn("self._reap_adopted(s, top)", src)


if __name__ == "__main__":
    unittest.main()
