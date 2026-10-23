"""The test suite never touches the real home.

`python3 -m unittest test_holehome test_guard test_sessions` imported common (through guard) before
_env.py, so common.REAL_HOME / PIPER_HOME were the user's own and the cleanups of test_sessions
rmtree'd ~/.claude/{skills,projects,...}, ~/.ssh and ~/.m2. Three locks, each tested here:
_env refuses to run after a Piper module, common refuses the real home under unittest/pytest, and
every test module imports _env before any Piper module."""
import ast
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _env

import common  # noqa: E402

UNIT = os.path.dirname(os.path.abspath(__file__))
PIPER = os.path.join(UNIT, "..", "..", "piper")
PIPER_MODULES = {n[:-3] for n in os.listdir(PIPER) if n.endswith(".py")}
REAL = pwd.getpwuid(os.getuid()).pw_dir


def _py(code, env):
    """A fresh interpreter in UNIT (as `python3 -m unittest` runs), unittest already loaded."""
    return subprocess.run([sys.executable, "-c", "import unittest, sys\nsys.path.insert(0, %r)\n%s" % (PIPER, code)],
                          cwd=UNIT, env=env, capture_output=True, text=True, timeout=60)


class IsolationTest(unittest.TestCase):
    def test_this_process_is_isolated(self):
        root = os.path.realpath(_env.ROOT) + os.sep
        for p in (common.REAL_HOME, common.PIPER_HOME, os.environ["HOME"], os.path.expanduser("~")):
            self.assertTrue(os.path.realpath(p).startswith(root), p)

    def test_every_test_module_imports_env_before_piper(self):
        """top-level imports, in order: `import _env` comes before the first Piper module"""
        bad = []
        for name in sorted(os.listdir(UNIT)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            with open(os.path.join(UNIT, name), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            seen_env = False
            for node in tree.body:
                mods = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
                    [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else []
                if "_env" in mods:
                    seen_env = True
                # a sibling test module is fine only if it is itself checked (it is: same loop)
                if not seen_env and any(m in PIPER_MODULES for m in mods):
                    bad.append("%s: %s before _env" % (name, mods))
                    break
        self.assertEqual(bad, [])

    def test_every_test_file_checks_the_os_sandbox(self):
        """each test_*.py imports _env (testsandbox.require), each *.test.mjs imports
        _sandbox.mjs before anything else"""
        bad = []
        for name in sorted(os.listdir(UNIT)):
            if not name.endswith((".py", ".mjs")):
                continue
            with open(os.path.join(UNIT, name), encoding="utf-8") as fh:
                src = fh.read()
            if name.startswith("test_") and name.endswith(".py"):
                tree = ast.parse(src)
                if not any(isinstance(n, ast.Import) and any(a.name == "_env" for a in n.names) for n in tree.body):
                    bad.append(name)
            elif name.endswith(".test.mjs"):
                first = next((ln for ln in src.splitlines() if ln.startswith("import ")), "")
                if not first.startswith("import './_sandbox.mjs'"):
                    bad.append(name)
        self.assertEqual(bad, [])

    def test_common_refuses_the_real_home_under_unittest(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        env["HOME"] = REAL
        r = _py("import common", env)   # no PIPER_* at all: ~ = the real home
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REAL home", r.stderr)
        # inside a Piper session PIPER_REAL_HOME names the real home explicitly
        r = _py("import common", dict(env, PIPER_REAL_HOME=REAL, PIPER_HOME=os.path.join(_env.ROOT, "x")))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REAL home", r.stderr)
        # ... and a real ~/.piper with a fake home, the other way round
        r = _py("import common", dict(env, PIPER_REAL_HOME=_env.HOME, PIPER_HOME=os.path.join(REAL, ".piper")))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REAL home", r.stderr)

    def test_common_refuses_a_letter_case_alias_of_the_real_home(self):
        alias = os.path.join(os.path.dirname(REAL), os.path.basename(REAL).upper())
        if not os.path.exists(alias):
            self.skipTest("case-sensitive filesystem")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        r = _py("import common", dict(env, PIPER_REAL_HOME=alias, PIPER_HOME=os.path.join(_env.ROOT, "x")))
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REAL home", r.stderr)

    def test_common_still_imports_outside_tests(self):
        """piperd itself (no unittest loaded) keeps the real home"""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        env["HOME"] = REAL
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import common; "
                            "print(common.REAL_HOME)" % PIPER], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), REAL)

    def test_no_write_or_delete_outside_the_temp_dirs(self):
        """the write guard: every probe names a path under a directory that does not exist in the real home,
        so without the guard it fails with FileNotFoundError (not PermissionError) and touches nothing"""
        import shutil
        gone = os.path.join(REAL, ".piper-guard-probe-%d" % os.getpid())
        self.assertFalse(os.path.exists(gone))
        f = os.path.join(gone, "f")
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "src")
            open(src, "w").close()                       # inside the temp dir: allowed
            link = os.path.join(t, "link")
            os.symlink(gone, link)                       # a temp name that resolves into the real home
            probes = [lambda: shutil.rmtree(gone), lambda: os.remove(f), lambda: os.unlink(f),
                      lambda: os.rmdir(gone), lambda: os.mkdir(f), lambda: os.makedirs(f),
                      lambda: os.rename(src, f), lambda: os.replace(src, f), lambda: os.symlink(src, f),
                      lambda: os.chmod(f, 0o600), lambda: os.truncate(f, 0),
                      lambda: os.open(f, os.O_WRONLY | os.O_CREAT), lambda: open(f, "w"), lambda: open(f, "a"),
                      lambda: open(f, "r+"), lambda: open(os.path.join(link, "f"), "w"),
                      lambda: shutil.rmtree(link + "/x"), lambda: shutil.copy(src, f)]
            for i, probe in enumerate(probes):
                with self.assertRaises(PermissionError, msg="probe %d" % i):
                    probe()
            self.assertTrue(os.path.exists(src))
            os.replace(src, os.path.join(t, "moved"))    # still fine inside
            # a link that points into the home is removed as a link (unlink acts on the name), not refused
            os.unlink(link)
            self.assertFalse(os.path.lexists(link))
        self.assertFalse(os.path.exists(gone))
        # reading the real home stays possible (Piper reads ~/.hole etc. through REAL_HOME only, but a read is harmless)
        self.assertTrue(os.path.isdir(REAL))

    def test_a_marked_child_refuses_the_real_home_without_a_test_runner(self):
        """PIPER_TEST=1 (set by _env and the e2e harness) reaches piperd children that never load unittest"""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        env.update(HOME=REAL, PIPER_TEST="1")
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import common" % PIPER],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("REAL home", r.stderr)
        with tempfile.TemporaryDirectory() as t:   # ... and with a fake home it writes only under the temp dirs
            env.update(HOME=t, PIPER_REAL_HOME=t, PIPER_HOME=os.path.join(t, "ph"))
            r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import common\n"
                                "open(%r, 'w')" % (PIPER, os.path.join(REAL, ".piper-guard-probe-x", "f"))],
                               env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("PermissionError", r.stderr)

    def test_real_data_dirs_refused_in_tests(self):
        """incident 2026-10-12 15:51: a live service's data dir inherited by a test process
        had its real files rewritten. An extension names its dirs; common refuses one outside the test dirs."""
        real = os.path.join(REAL, "www", "data", "x")
        with mock.patch.dict(os.environ, {"PIPER_TEST_DATA_X": real}):
            with self.assertRaisesRegex(RuntimeError, "REAL PIPER_TEST_DATA_X"):
                common.refuse_real_data_in_tests(("PIPER_TEST_DATA_X",))
        with mock.patch.dict(os.environ, {"PIPER_TEST_DATA_X": os.path.join(_env.ROOT, "x")}):
            common.refuse_real_data_in_tests(("PIPER_TEST_DATA_X",))
        common.refuse_real_data_in_tests(("PIPER_TEST_DATA_UNSET",))

    def test_e2e_harness_refuses_the_real_home_as_work_dir(self):
        run_py = os.path.join(UNIT, "..", "piper-e2e", "run.py")
        for work in (REAL, os.path.dirname(REAL), os.path.join(REAL, ".piper-e2e-probe")):
            r = subprocess.run([sys.executable, run_py, "--work", work, "--port", "17001"],
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0, work)
            self.assertIn("real home", r.stderr, work)
        self.assertFalse(os.path.exists(os.path.join(REAL, ".piper-e2e-probe")))

    def test_env_refuses_to_run_after_a_piper_module(self):
        """the exact incident order: guard (-> common) first, _env later"""
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, PIPER_REAL_HOME=t, PIPER_HOME=os.path.join(t, "piper"), HOME=t)
            r = _py("import guard\nimport _env", env)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("_env.py must be imported before any Piper module", r.stderr)
        self.assertIn("common", r.stderr)


if __name__ == "__main__":
    unittest.main()
