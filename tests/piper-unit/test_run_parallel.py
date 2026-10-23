"""run_parallel.py: unittest's per-module summaries add up, and a crashed or failing module fails the run."""
import os
import sys
import tempfile
import unittest
import unittest.mock

import _env  # noqa: F401  (the sandbox check; run_parallel imports no Piper module)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_parallel  # noqa: E402


def mod(name, text, rc=0):
    return dict(run_parallel.parse_result(text), name=name, rc=rc, output=text, secs=0.1)


class RunParallelTest(unittest.TestCase):
    def test_parse_ok_failed_and_skips(self):
        self.assertEqual(run_parallel.parse_result("...\n----\nRan 3 tests in 0.1s\n\nOK\n"),
                         {"ran": 3, "ok": True, "failures": 0, "errors": 0, "skipped": 0, "expected failures": 0, "unexpected successes": 0})
        r = run_parallel.parse_result("Ran 12 tests in 2.0s\n\nFAILED (failures=1, errors=2, skipped=4)\n")
        self.assertEqual((r["ran"], r["ok"], r["failures"], r["errors"], r["skipped"]), (12, False, 1, 2, 4))
        self.assertEqual(run_parallel.parse_result("Ran 1 test in 0.0s\n\nOK (skipped=1)\n")["skipped"], 1)
        # an import error before any test ran: no summary -> not ok
        self.assertFalse(run_parallel.parse_result("Traceback (most recent call last):\nImportError: x\n")["ok"])

    def test_summary_adds_up_and_fails_on_any_bad_module(self):
        good = mod("test_a.py", "Ran 5 tests in 1s\n\nOK (skipped=2)\n")
        also = mod("test_b.py", "Ran 7 tests in 1s\n\nOK\n")
        tail, ok = run_parallel.summarize([good, also], 3.0)
        self.assertTrue(ok)
        self.assertEqual(tail, "Ran 12 tests in 3.000s\n\nOK (skipped=2)")
        bad = mod("test_c.py", "Ran 2 tests in 1s\n\nFAILED (errors=1)\n", rc=1)
        tail, ok = run_parallel.summarize([good, bad], 3.0)
        self.assertFalse(ok)
        self.assertIn("FAILED (errors=1, skipped=2)", tail)
        # killed / crashed: exit != 0 without unittest's tally still fails, and says so
        dead = mod("test_d.py", "Segmentation fault\n", rc=-11)
        tail, ok = run_parallel.summarize([good, dead], 1.0)
        self.assertFalse(ok)
        self.assertIn("crashed modules=1", tail)
        # "OK" printed but a non-zero exit (e.g. a hang killed at teardown) is not a pass
        self.assertFalse(run_parallel.summarize([mod("test_e.py", "Ran 1 test in 0s\n\nOK\n", rc=1)], 1.0)[1])

    def test_split_module_runs_per_class_and_others_whole(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test_big.py"), "w") as f:
                f.write("import unittest\nclass Base(unittest.TestCase): pass\nclass ATest(Base):\n    def test_a(self): pass\n"
                        "class BTest(unittest.TestCase):\n    def test_b(self): pass\n")
            with open(os.path.join(d, "test_small.py"), "w") as f:
                f.write("import unittest\nclass CTest(unittest.TestCase):\n    def test_c(self): pass\n")
            with unittest.mock.patch.object(run_parallel, "SPLIT_BY_CLASS", {"test_big.py"}):
                self.assertEqual(run_parallel.units(d, ["test_big.py", "test_small.py"]),
                                 [("test_big.py:ATest", "test_big.ATest"), ("test_big.py:BTest", "test_big.BTest"), ("test_small.py", "")])
                # a class by name, importable from the test directory, whatever the cwd
                r = run_parallel.run_module(d, "test_big.py:BTest", "test_big.BTest")
        self.assertEqual((r["ok"], r["ran"], r["rc"]), (True, 1, 0), r["output"])
        # the real split module still lists its classes (a rename must not silently drop one)
        self.assertIn("K1WorkTreeWriteTest", run_parallel.test_classes(os.path.join(run_parallel.HERE, "test_gitrun_exposed.py")))

    def test_runs_each_module_in_its_own_process(self):
        with tempfile.TemporaryDirectory() as d:
            for n, body in (("test_one.py", "def test_x(self): pass"), ("test_two.py", "def test_x(self): self.fail('no')")):
                with open(os.path.join(d, n), "w") as f:
                    f.write("import os, unittest\nclass T(unittest.TestCase):\n    %s\n" % body)
            rc = run_parallel.main(["-s", d, "-j", "2"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
