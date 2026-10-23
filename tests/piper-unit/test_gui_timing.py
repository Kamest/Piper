"""The GUI harness must keep explicit polling waits and fail on unstable UI."""
import ast
from pathlib import Path
import unittest
from unittest import mock

import _env


class GuiTimingTest(unittest.TestCase):
    def helper(self):
        source = Path(__file__).resolve().parents[1] / 'piper-e2e/gui.py'
        tree = ast.parse(source.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'settle')
        ns = dict(js=mock.Mock(), SETTLED='real-browser-condition', time=mock.Mock(), record_timing=mock.Mock())
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), ns)
        return ns

    def test_default_wait_uses_the_browser_condition_without_fixed_sleep(self):
        ns = self.helper()
        ns['settle']()
        ns['js'].assert_called_once_with(ns['SETTLED'])
        ns['time'].sleep.assert_not_called()

    def test_explicit_polling_interval_is_preserved_and_checked_afterwards(self):
        ns = self.helper()
        ns['settle'](3.3)
        ns['time'].sleep.assert_called_once_with(3.3)
        self.assertEqual(ns['js'].call_args_list, [mock.call(ns['SETTLED'])] * 2)

    def test_unstable_page_failure_is_not_swallowed(self):
        ns = self.helper()
        ns['js'].side_effect = RuntimeError('GUI did not settle')
        with self.assertRaisesRegex(RuntimeError, 'did not settle'):
            ns['settle']()
        ns['record_timing'].assert_called_once()
