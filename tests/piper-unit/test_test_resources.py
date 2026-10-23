"""Run the actual task shell against stand-ins: coverage and serialization survive the rewrite."""
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import _env
import run_parallel

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'scripts/piper-test-lock.py'


class TestResourcesTest(unittest.TestCase):
    def test_backend_readiness_rejects_another_server_and_a_dead_child(self):
        # Load only the production harness function: importing run.py starts argument/home setup.
        import ast
        source = ROOT / 'tests/piper-e2e/run.py'
        function = next(n for n in ast.parse(source.read_text()).body
                        if isinstance(n, ast.FunctionDef) and n.name == 'start_piperd')
        code = compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec')
        for child_rc, status in ((None, 401), (1, 200), (None, 200)):
            with self.subTest(child_rc=child_rc, status=status), tempfile.TemporaryDirectory() as d:
                process = mock.Mock()
                process.poll.return_value = child_rc
                req = mock.Mock(return_value=(status, {}, b''))
                ns = dict(os=os, sys=sys, subprocess=mock.Mock(Popen=mock.Mock(return_value=process)),
                          WORK=d, BIN=d, ENV={}, PROC={}, req=req, wait=lambda pred, timeout: pred(),
                          kill_piperd=mock.Mock())
                exec(code, ns)
                try:
                    if child_rc is None and status == 200:
                        ns['start_piperd']()
                        ns['kill_piperd'].assert_not_called()
                    else:
                        with self.assertRaises(RuntimeError):
                            ns['start_piperd']()
                        ns['kill_piperd'].assert_called_once()
                    if req.called:
                        self.assertNotEqual(req.call_args.kwargs.get('auth'), False)
                        self.assertNotIn('tok', req.call_args.kwargs)
                finally:
                    for call in ns['subprocess'].Popen.call_args_list:
                        call.kwargs['stdout'].close()

    def test_default_python_budget_is_two_even_on_a_large_machine(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {'PIPER_UNIT_JOBS': '0'}), \
                mock.patch.object(run_parallel.os, 'cpu_count', return_value=32), \
                mock.patch.object(run_parallel.concurrent.futures, 'ThreadPoolExecutor', wraps=run_parallel.concurrent.futures.ThreadPoolExecutor) as pool:
            self.assertEqual(run_parallel.main(['-s', d]), 0)
            self.assertEqual(pool.call_args.args, (2,))

    def test_two_entrypoints_share_a_lock_and_release_it_after_exit(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, TMPDIR=d)
            first = Path(d) / 'first'
            second = Path(d) / 'second'
            release = Path(d) / 'release'
            code = 'from pathlib import Path; import sys,time; Path(sys.argv[1]).touch();\nwhile not Path(sys.argv[2]).exists(): time.sleep(.01)'
            a = subprocess.Popen([sys.executable, str(LOCK), '--', sys.executable, '-c', code, str(first), str(release)], env=env)
            b = None
            try:
                deadline = time.monotonic() + 5
                while not first.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(first.exists())
                b = subprocess.Popen([sys.executable, str(LOCK), '--', sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(second)], env=env, stdout=subprocess.PIPE, text=True)
                self.assertIn('waiting', b.stdout.readline())
                self.assertFalse(second.exists())
                release.touch()
                self.assertEqual(a.wait(5), 0)
                self.assertEqual(b.wait(5), 0)
                self.assertTrue(second.exists())
            finally:
                release.touch()
                for p in (a, b):
                    if p is not None:
                        if p.poll() is None:
                            p.kill()
                        p.wait()
                        if p.stdout:
                            p.stdout.close()

    def gate(self, fail_selftest=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'scripts').mkdir()
            (root / 'bin').mkdir()
            shutil.copyfile(LOCK, root / 'scripts/piper-test-lock.py')
            (root / 'scripts/piper-build.sh').write_text('exit 0\n')
            log = root / 'calls'
            (root / 'scripts/piper-sandbox.py').write_text('import json,sys\nfrom pathlib import Path\n'
                + f'with open({str(log)!r}, "a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                + ('sys.exit(1 if "--selftest" in sys.argv else 0)\n' if fail_selftest else ''))
            for name in ('docker', 'agent-browser', 'node'):
                p = root / 'bin' / name
                p.write_text('#!/bin/sh\nexit 0\n')
                p.chmod(0o700)
            # Piper's Taskfile.yml, or the tasks file of the checkout that embeds Piper
            files = [ROOT / 'Taskfile.yml', *sorted((ROOT / 'tasks').glob('*.yml'))]
            yaml = next(t for t in (p.read_text() for p in files if p.exists()) if '  piper:e2e:\n' in t)
            section = re.split(r'\n  [\w:-]+:\n', yaml.split('  piper:e2e:\n', 1)[1], maxsplit=1)[0]
            shell = textwrap.dedent(section.split('      - |\n', 1)[1]).replace('{{.SK}}', str(root))
            env = dict(os.environ, PATH=str(root / 'bin') + os.pathsep + os.environ['PATH'], TMPDIR=tmp, PIPER_UNIT_JOBS='2')
            result = subprocess.run(['sh', '-c', shell], env=env, capture_output=True, text=True, timeout=15)
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            # a call on a path outside the core (scripts/, piper/, tests/) is an extension's own suite
            core = tuple(str(root / d) + '/' for d in ('scripts', 'piper', 'tests'))
            self.ext = [c for c in calls if any(a.startswith(str(root) + '/') and not a.startswith(core) for a in c)]
            return result, calls

    def test_gate_preserves_all_suites_with_one_gui_fixture_and_bounded_workers(self):
        result, calls = self.gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # an extension's own suites may run beside these
        ext = self.ext
        calls = [c for c in calls if c not in ext]
        self.assertEqual(len(calls), 7)  # selftest, unit, node, run, http, DB, GUI
        self.assertEqual(calls[0], ['--selftest'])
        self.assertIn('run_parallel.py', ' '.join(calls[1]))
        self.assertEqual(calls[1][-2:], ['-j', '2'])
        self.assertIn('--test-concurrency=2', ' '.join(calls[2]))
        self.assertIn('/run.py', ' '.join(calls[3]))
        self.assertIn('/http_e2e.py', ' '.join(calls[4]))
        self.assertIn('--dind', calls[5])
        for c in ext:
            self.assertTrue(c[-2:] == ['-j', '2'] or '--dind' in c, c)   # bounded workers, Docker in its own dind
        gui = calls[6]
        self.assertIn('/gui.py', ' '.join(gui))
        self.assertEqual(gui[gui.index('--presets') + 1], 'basic,dev,all')
        self.assertEqual(gui[gui.index('--widths') + 1], '1024,1920')
        self.assertNotIn('--walk-only', gui)

    def test_failed_sandbox_selftest_stops_the_gate(self):
        result, calls = self.gate(fail_selftest=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [['--selftest']])
