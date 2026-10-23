"""The core / extension boundary: Piper's core runs alone, names no
extension, and a broken extension never stops it."""
import os
import ast
import subprocess
import sys
import tempfile
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import extensions
import features

PIPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper")


class BoundaryTest(unittest.TestCase):
    def test_core_suite_runs_without_extensions(self):
        self.assertNotIn("PIPER_EXTENSIONS", os.environ)
        self.assertEqual(extensions.LOADED, [])
        self.assertNotIn("stack", features.IDS)   # the dev stack module is an extension's

    def test_no_core_module_imports_an_extension(self):
        allowed = sys.stdlib_module_names | {n[:-3] for n in os.listdir(PIPER) if n.endswith(".py")}
        hits = []
        for d, _, files in os.walk(PIPER):
            for n in files:
                if n.endswith(".py"):
                    with open(os.path.join(d, n), encoding="utf-8") as f:
                        # Database drivers run only in the separate worker's virtual environment.
                        worker = {"pymysql", "psycopg", "oracledb", "pytds"} if n == "dbworker.py" else set()
                        for node in ast.walk(ast.parse(f.read())):
                            modules = ([a.name for a in node.names] if isinstance(node, ast.Import) else
                                       [node.module or ""] if isinstance(node, ast.ImportFrom) and not node.level else [])
                            hits.extend((n, module) for module in modules if module.split(".")[0] not in allowed | worker)
        self.assertEqual(hits, [])

    def _child(self, ext_dirs, code):
        env = dict(os.environ, PIPER_EXTENSIONS=os.pathsep.join(ext_dirs))
        return subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r)\n" % PIPER + code],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_extension_hooks_and_defaults(self):
        d = tempfile.mkdtemp(dir=_env.ROOT)
        with open(os.path.join(d, "extension.py"), "w") as f:
            f.write("NAME = 'demo'\nMODULES = [('demo', 'Demo', 'help')]\nPRESETS = {'dev': ['demo']}\n"
                    "DEFAULTS = {'gitlab_host': 'git.example.org'}\n"
                    "def db_links(root): return [{'label': 'x', 'url': 'http://x/' + root}]\n")
        r = self._child([d], "import extensions, features\n"
                             "print(extensions.names(), 'demo' in features.IDS, 'demo' in features.PRESETS['dev'],\n"
                             "      extensions.default('gitlab_host', 'gitlab.com'), extensions.collect('db_links', 'r'),\n"
                             "      extensions.collect('nothing'), extensions.ERRORS)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(),
                         "['demo'] True True git.example.org [{'label': 'x', 'url': 'http://x/r'}] [] []")

    def test_extension_reserves_its_env_names(self):
        d = tempfile.mkdtemp(dir=_env.ROOT)
        with open(os.path.join(d, "extension.py"), "w") as f:
            f.write("def reserved_env(): return ['ACME_']\n")
        code = ("import projects\n"
                "def ok(n):\n"
                "    try: return bool(projects.sandbox_env_config({'key': 'p', 'sandbox_env': {'keys': [n]}}))\n"
                "    except ValueError: return False\n"
                "print(ok('ACME_URL'), ok('API_URL'), ok('PIPER_X'))")
        self.assertEqual(self._child([d], code).stdout.strip(), "False True False")
        self.assertEqual(self._child([], code).stdout.strip(), "True True False")

    def test_broken_extension_is_reported_not_fatal(self):
        d = tempfile.mkdtemp(dir=_env.ROOT)
        with open(os.path.join(d, "extension.py"), "w") as f:
            f.write("raise RuntimeError('boom')\n")
        missing = os.path.join(_env.ROOT, "no-such-extension")
        r = self._child([d, missing], "import extensions, features\n"
                                      "print(len(extensions.LOADED), len(extensions.ERRORS), 'diag' in features.IDS)")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0 2 True")
        self.assertIn("boom", r.stderr)


if __name__ == "__main__":
    unittest.main()
