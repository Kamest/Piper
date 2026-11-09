"""Global stack controls act only on a provider's current, explicitly selected inventory."""
import unittest
from unittest import mock

import _env  # noqa: F401
import stackview
import workbench


class StackOverviewTest(unittest.TestCase):
    def setUp(self):
        self.m = workbench.Workbench()
        self.provider = mock.Mock()
        self.provider.running_stacks.return_value = [
            {"root": "/fixtures/main", "name": "Main", "running": 2, "total": 3, "can_stop": True},
            {"root": "/fixtures/child", "name": "Child", "parent": "/fixtures/main", "running": 1, "total": 1, "can_stop": True},
            {"root": "/fixtures/main", "name": "Duplicate", "running": 2, "total": 3, "can_stop": True},
            {"root": "/fixtures/stopped", "name": "Stopped", "running": 0, "total": 1, "can_stop": True},
        ]
        self.provider.verb.return_value = {"id": "abcdef", "state": "queued", "verb": "stop"}
        self.stack = mock.patch.object(self.m, "_stack", return_value=self.provider).start()
        self.addCleanup(mock.patch.stopall)
        self.enabled = mock.patch.object(workbench.features, "on", return_value=True).start()

    def test_inventory_deduplicates_and_keeps_parent_without_exposing_provider_extras(self):
        rows = self.m.stack_overview()["stacks"]
        self.assertEqual([r["root"] for r in rows], ["/fixtures/main", "/fixtures/child"])
        self.assertEqual(rows[1]["parent"], "/fixtures/main")
        self.assertTrue(self.m.stack_overview()["supported"])

    def test_disabled_feature_performs_no_provider_work(self):
        self.enabled.return_value = False
        for run in (self.m.stack_overview, lambda: self.m.stack_stop_all(["/fixtures/main"])):
            with self.assertRaisesRegex(ValueError, "disabled"):
                run()
        self.stack.assert_not_called()

    def test_old_or_absent_provider_is_an_explicit_unsupported_state(self):
        self.stack.return_value = object()
        self.assertEqual(self.m.stack_overview(), {"supported": False, "stacks": []})
        self.stack.side_effect = stackview.StackError("no provider")
        self.assertEqual(self.m.stack_overview(), {"supported": False, "stacks": []})

    def test_stop_targets_only_the_previewed_roots_once_children_first(self):
        result = self.m.stack_stop_all(["/fixtures/main", "/fixtures/child", "/fixtures/main"])
        self.assertEqual(self.provider.verb.call_args_list, [
            mock.call("/fixtures/child", "stop", ""), mock.call("/fixtures/main", "stop", "")])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["results"]), 2)

    def test_unknown_or_unsupported_target_fails_before_dispatch(self):
        for roots in (["/fixtures/main", "/fixtures/unknown"], "*", [], [1]):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                self.m.stack_stop_all(roots)
        self.provider.verb.assert_not_called()

    def test_partial_stop_failure_is_reported_and_other_stacks_continue(self):
        self.provider.verb.side_effect = [stackview.StackError("provider unavailable"), {"id": "abcdef", "state": "queued"}]
        result = self.m.stack_stop_all(["/fixtures/child", "/fixtures/main"])
        self.assertFalse(result["ok"])
        self.assertIn("error", result["results"][0])
        self.assertEqual(result["results"][1]["job"]["id"], "abcdef")
        self.assertEqual(self.provider.verb.call_count, 2)

    def test_job_progress_retains_failure_and_validates_stack_identity(self):
        self.provider.job.return_value = {"id": "abcdef", "root": "/fixtures/child", "state": "failed", "error": "stop failed", "rc": 1}
        result = self.m.stack_overview_job("/fixtures/child", "abcdef")
        self.assertEqual((result["state"], result["error"], result["rc"]), ("failed", "stop failed", 1))
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.m.stack_overview_job("/fixtures/main", "abcdef")


if __name__ == "__main__":
    unittest.main()
