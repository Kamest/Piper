"""The GUI harness must keep explicit polling waits and fail on unstable UI."""
import ast
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import _env


class GuiTimingTest(unittest.TestCase):
    def helper(self, name="settle"):
        source = Path(__file__).resolve().parents[1] / 'piper-e2e/gui.py'
        tree = ast.parse(source.read_text())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        ns = dict(js=mock.Mock(), ab=mock.Mock(return_value={'success': True}), SETTLED='real-browser-condition', time=mock.Mock(), record_timing=mock.Mock())
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

    def test_reload_refuses_to_continue_after_failed_navigation(self):
        ns = self.helper('reload')
        ns['ab'].return_value = {'success': False, 'error': 'synthetic reload refusal'}
        with self.assertRaisesRegex(RuntimeError, 'reload'):
            ns['reload']()

    def test_reload_refuses_to_continue_without_the_initial_application_state(self):
        ns = self.helper('reload')
        ns['ab'].side_effect = [{'success': True}, {'success': False}]
        with self.assertRaisesRegex(RuntimeError, 'initial.*state'):
            ns['reload']()

    def test_reload_waits_for_new_document_and_a_rendered_session(self):
        ns = self.helper('reload')
        ns['reload']()
        wait = ns['ab'].call_args_list[-1].args
        self.assertEqual(wait[:2], ('wait', '--fn'))
        for gate in ('!window.__e2eOldPage', "document.readyState === 'complete'", 'dataset.sid'):
            self.assertIn(gate, wait[2])


class GuiHttpFixtureTest(unittest.TestCase):
    def wrapper_transport(self, binary):
        source = Path(__file__).resolve().parents[1] / 'piper-e2e/serve.py'
        tree = ast.parse(source.read_text())
        assignment = next(n for n in tree.body if isinstance(n, ast.Assign) and
                          any(isinstance(t, ast.Name) and t.id == 'PIPERD_WRAPPER' for t in n.targets))
        wrapper = ast.literal_eval(assignment.value) % ('/synthetic/piper', 17963, '/synthetic/unit')
        native, fake = mock.Mock(), mock.Mock()
        jail = SimpleNamespace(run=native)
        modules = dict(guard=SimpleNamespace(),
                       gitlab=SimpleNamespace(GitLab=type('GitLab', (), {'__init__': lambda *a, **kw: None})),
                       piperd=SimpleNamespace(main=mock.Mock(return_value=0)),
                       httpjail=jail, _httpfake=SimpleNamespace(run=fake))
        with mock.patch.dict(sys.modules, modules), mock.patch.object(sys, 'path', []), \
                mock.patch.object(sys, 'exit'), mock.patch.dict(os.environ, PIPER_IJHTTP=binary):
            exec(compile(wrapper, str(source), 'exec'), {})
        return jail.run, native, fake

    def test_gui_fixture_uses_its_synthetic_transport(self):
        actual, native, fake = self.wrapper_transport('/synthetic/fake-ijhttp.py')
        self.assertIs(actual, fake)
        native.assert_not_called()

    def test_real_cli_keeps_its_actual_isolation(self):
        actual, native, fake = self.wrapper_transport('/synthetic/ijhttp/ijhttp')
        self.assertIs(actual, native)
        fake.assert_not_called()
