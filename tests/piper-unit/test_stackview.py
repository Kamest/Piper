"""stackview.py: the views piperd builds from what a dev stack provider returns."""
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error

import _env  # noqa: F401
import stackview as stack


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class StackTest(unittest.TestCase):
    def opener(self, answer):
        self.reqs = []

        def op(req, timeout):
            self.reqs.append(req)
            if isinstance(answer, Exception):
                raise answer
            return Resp(json.dumps(answer).encode())
        return op

    def test_views_keep_only_offered_verbs_and_safe_links(self):
        v = stack.dash_view({"services": [{"service": "web", "verbs": ["restart", "exec"], "status": "Up 2h (healthy)",
                                           "ports": [{"host": 8080}, {"host": 8080}, {"host": None}],
                                           "links": ["https://a", "javascript:alert(1)"]}],
                             "primary": {"verb": "exec"}, "verbs": ["down", "nuke"], "psErr": "boom"})
        s = v["services"][0]
        self.assertEqual((s["verbs"], s["health"], s["ports"], s["links"]), (["restart"], "healthy", ["8080"], ["https://a"]))
        self.assertIsNone(v["primary"])
        self.assertEqual((v["verbs"], v["error"]), (["down"], "boom"))
        rows = stack.service_rows({"services": [{"service": "db", "state": "running", "status": "Up 3 minutes (health: starting)"}]})
        self.assertEqual(rows, [{"name": "db", "state": "running", "health": "starting", "age": "3 minutes"}])
        self.assertNotIn("argv", stack.job_view({"id": "abcdef", "argv": ["x"], "rc": "1"}))
        # F-STACK 2026-10-07: a reloaded GUI re-adopts a running job from this view - without the service
        # "restart db" read "Restarting the stack"
        self.assertEqual(stack.job_view({"id": "abcdef", "verb": "restart", "services": ["db"]})["services"], ["db"])
        self.assertEqual(stack.job_view({"id": "abcdef", "services": ["ok", "../x", 3, "a b"]})["services"], ["ok", "3"])
        self.assertEqual(stack.job_view({"id": "abcdef", "services": "db"})["services"], [])
        self.assertEqual(stack.job_view({"id": "abcdef"})["services"], [])
        # G-ST5: a killed service (Exited 137) looked like one stopped by hand
        for view in (stack.dash_view, stack.service_rows):
            got = view({"services": [{"service": "a", "state": "exited", "status": "Exited (137) 2 minutes ago"},
                                     {"service": "b", "state": "exited", "status": "Exited (0) 1 minute ago"}]})
            got = got["services"] if isinstance(got, dict) else got
            self.assertEqual([r.get("exitCode") for r in got], [137, None], view.__name__)

    def test_jobs_shown_running_and_recent(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        jobs = [{"id": "aaaaaa", "root": root, "verb": "restart", "state": "running", "started": 100},
                {"id": "bbbbbb", "root": root, "verb": "up", "state": "done", "started": 10},
                {"id": "cccccc", "root": "/other", "verb": "up", "state": "running", "started": 1},
                {"id": "dddddd", "root": root, "verb": "tunnel", "state": "running", "started": 1}]
        seen = {}
        self.assertEqual([j["id"] for j in stack.stack_jobs(jobs, root, 200, seen)], ["aaaaaa"])
        jobs[0]["state"] = "done"
        self.assertEqual([j["id"] for j in stack.stack_jobs(jobs, root, 300, seen)], ["aaaaaa"])
        self.assertEqual(stack.stack_jobs(jobs, root, 300 + stack.JOB_RECENT, seen), [])


if __name__ == "__main__":
    unittest.main()
