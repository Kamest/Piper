"""Regression coverage for lost hooks, missing questions and unavailable turn history."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

import _env
import common
import feed
import gitops
import sessions


class LogRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.m = _env.manager(self)

    def session(self, agent='codex'):
        s = sessions.Session(project='acme/shop', path=_env.PP, agent=agent,
                             runtime='direct', state='working')
        s.claude_mode = 'none' if agent == 'codex' else 'host'
        os.makedirs(s.box, exist_ok=True)
        self.m.sessions[s.id] = s
        return s

    def hooks(self, s, *records):
        with open(os.path.join(s.box, s.names['events']), 'a') as f:
            for record in records:
                f.write(json.dumps(dict(ts=time.time(), **record)) + '\n')

    def test_disk_full_does_not_hide_following_permission_and_save_retries(self):
        s = self.session('claude')
        self.hooks(s, {'ev': 'UserPromptSubmit', 'prompt': 'Inspect the example'},
                   {'ev': 'Notification', 'notification_type': 'permission_prompt',
                    'message': 'Claude needs your permission'})
        with mock.patch.object(s, 'save', side_effect=OSError(28, 'No space left on device')), \
                mock.patch.object(self.m, '_snapshot', side_effect=gitops.GitError('unavailable')):
            self.m._events(s)
        self.assertEqual(s.state, 'attention')
        self.assertEqual(s.detail, 'Claude needs your permission')
        self.assertTrue(s.storage_error)
        s.storage_retry = 0
        self.m._events(s)  # No new hook is needed to retry the pending metadata write.
        self.assertFalse(s.storage_error)
        saved = common.read_json(os.path.join(s.dir, 'meta.json'), {})
        self.assertEqual(saved['state'], 'attention')
        self.assertFalse(saved['storage_error'])

    def test_failed_hook_keeps_following_records_unread_without_replaying_prior_ones(self):
        s = self.session('claude')
        self.hooks(s, {'ev': 'UserPromptSubmit'}, {'ev': 'Notification', 'message': 'Needs permission'})
        with mock.patch.object(self.m, '_turn_event', side_effect=RuntimeError('temporary failure')) as turn:
            with self.assertRaises(RuntimeError):
                self.m._events(s)
        self.assertLess(s.events_off, os.path.getsize(os.path.join(s.box, s.names['events'])))
        self.m._events(s)
        self.assertEqual(s.state, 'attention')
        self.assertEqual(s.detail, 'Needs permission')
        self.assertEqual(turn.call_count, 1)

    def test_expired_codex_token_is_attention_and_ansi_chunks_are_supported(self):
        s = self.session()
        for text in ['Your authentication \x1b[31mtoken has ', 'expired. Please try refreshing it.']:
            self.m._on_pty_event({'id': s.id, 'ev': 'data',
                                 'data': base64.b64encode(text.encode()).decode()})
        self.assertEqual((s.state, s.phase), ('attention', 'agent-auth'))
        s.subagents = {'native-worker': time.time()}
        self.hooks(s, {'ev': 'Stop'})
        with mock.patch.object(self.m, '_turn_event'):
            self.m._events(s)
        self.assertEqual((s.state, s.phase, s.idle), ('attention', 'agent-auth', False))
        self.assertIn('authentication failed', s.detail)
        s.subagents = {}
        self.hooks(s, {'ev': 'UserPromptSubmit'})
        with mock.patch.object(self.m, '_turn_event'):
            self.m._events(s)
        self.assertEqual((s.state, s.phase), ('working', ''))

    def test_hook_write_failure_is_visible_without_failing_the_agent_hook(self):
        path = os.path.join(_env.ROOT, 'missing-directory', 'events.jsonl')
        script = Path(common.PIPER_DIR, 'sandbox/piper-event.py')
        result = subprocess.run([sys.executable, str(script), 'Stop', path], input='{}',
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('Piper could not record this hook', result.stderr)
        self.assertNotIn(path, result.stderr)

    def test_quoted_auth_error_in_test_output_is_not_a_native_login_failure(self):
        s = self.session()
        for text in ["78 + b'Your access token could not be refreshed').decode()\n",
                     'auth_markers = ("youraccesstokencouldnotberefreshed",)\n',
                     '> Your authentication token has expired. Please try refreshing it.\n']:
            self.m._on_pty_event({'id': s.id, 'ev': 'data',
                                 'data': base64.b64encode(text.encode()).decode()})
            self.assertEqual((s.state, s.phase), ('working', ''))

    def test_new_native_tool_activity_clears_a_stale_auth_warning(self):
        s = self.session()
        s.state, s.phase, s.detail = 'attention', 'agent-auth', 'Agent authentication failed'
        s.phase_since = time.time() - 1
        self.hooks(s, {'ev': 'PostToolUse', 'tool_name': 'exec_command'})
        self.m._events(s)
        self.assertEqual((s.state, s.phase, s.detail), ('working', '', ''))

    def test_old_tool_activity_cannot_clear_a_new_auth_failure(self):
        s = self.session()
        s.state, s.phase, s.detail = 'attention', 'agent-auth', 'Agent authentication failed'
        s.phase_since = time.time()
        with open(os.path.join(s.box, s.names['events']), 'a') as f:
            f.write(json.dumps(dict(ev='PostToolUse', ts=s.phase_since-2, tool_name='exec_command'))+'\n')
        self.m._events(s)
        self.assertEqual((s.state, s.phase), ('attention', 'agent-auth'))

    def test_native_error_prefix_and_other_expired_token_wording_are_detected(self):
        s = self.session()
        text = 'Working\n■ Provided authentication token is expired.\n'
        self.m._on_pty_event({'id': s.id, 'ev': 'data', 'data': base64.b64encode(text.encode()).decode()})
        self.assertEqual((s.state, s.phase), ('attention', 'agent-auth'))

    def test_snapshot_failure_keeps_actual_stop_and_next_start_boundaries(self):
        s = self.session()
        start, stop, again = time.time() - 30, time.time() - 20, time.time() - 10
        s.turns = [{'n': 1, 'started': start, 'ended': None, 'from': 'a' * 40, 'to': None}]
        s.turn_q = [('Stop', stop, '', None), ('UserPromptSubmit', again, 'Next task', True)]
        s.turn_busy = True
        with mock.patch.object(self.m, '_snapshot', side_effect=gitops.GitError('refused')):
            self.m._turn_worker(s)
        self.assertEqual(s.turns[0]['ended'], stop)
        self.assertTrue(s.turns[0]['unavailable'])
        self.assertEqual(s.turns[1]['started'], again)
        self.assertIsNone(s.turns[1]['from'])
        self.assertTrue(s.turns[1]['unavailable'])
        self.assertIsNone(s.turns[1]['ended'])
        self.assertFalse(s.review_pending)

    def test_failed_snapshot_does_not_offer_revert_even_if_end_snapshot_exists(self):
        s = self.session()
        s.turns = [{'n': 1, 'started': time.time()-30, 'ended': None, 'from': None, 'to': None,
                    'unavailable': 'Snapshot unavailable'}]
        s.turn_q = [('Stop', time.time(), '', None)]
        with mock.patch.object(self.m, '_snapshot', return_value='b' * 40), \
                mock.patch.object(self.m, '_pin_turn'), \
                mock.patch.object(self.m, '_turn_summary') as summary:
            self.m._turn_worker(s)
        self.assertIsNotNone(s.turns[-1]['ended'])
        self.assertTrue(s.turns[-1]['unavailable'])
        summary.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Snapshot unavailable'):
            self.m.review_files(s.id, scope='turn', n=1)
        with self.assertRaisesRegex(ValueError, 'snapshots are unavailable'):
            self.m.review_files(s.id, scope='session')

    def rollout(self, s, records, mode='w'):
        s.claude_session = '12345678-abcd'
        rel = 'sessions/2026/11/04/rollout-audit-' + s.claude_session + '.jsonl'
        path = os.path.join(s.codex_home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode) as f:
            for rec in records:
                f.write(json.dumps(rec) + '\n')

    def question(self, ident='question-1'):
        return {'type': 'response_item', 'timestamp': '2026-11-04T10:00:00Z', 'payload': {
            'type': 'function_call', 'name': 'request_user_input_async', 'call_id': ident,
            'arguments': json.dumps({'questions': [{'title': 'Which environment?', 'options': ['Test', 'Development']}]})}}

    def test_question_has_open_action_while_agent_keeps_working_and_survives_reload(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': '12345678-abcd'}}, self.question()])
        self.m._questions(s)
        self.assertEqual(s.state, 'working')
        actions = feed.derive_actions([feed.session_snap(s)], [], [], {}, {}, time.time(), 0)
        question = next(a for a in actions if a['key'].startswith('question:'))
        self.assertIn('Which environment?', question['body'])
        self.assertEqual([a['op'] for a in question['actions']], ['open'])
        self.assertEqual(question['actions'][0]['args']['session'], s.id)
        restored = sessions.Session(**s.to_json())
        self.assertEqual(restored.questions, s.questions)
        turn = sessions.digest.codex_digest(s.codex_home, s.claude_session)[0]
        self.assertIn('Which environment?', turn['text'])
        self.rollout(s, [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                            'content': [{'type': 'input_text', 'text': 'Use test'}]}}], 'a')
        self.m._questions(s)
        self.assertEqual(s.questions, [])

    def test_foreign_questions_and_injected_developer_messages_do_not_affect_pending_question(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': 'another-thread'}}, self.question()])
        self.m._questions(s)
        self.assertEqual(s.questions, [])
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': '12345678-abcd'}}, self.question(),
                         {'type': 'response_item', 'payload': {'type': 'message', 'role': 'developer', 'content': 'context'}}])
        self.m._questions(s)
        self.assertEqual(len(s.questions), 1)

    def test_async_acceptance_is_not_an_answer_but_sync_result_clears_its_question(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': '12345678-abcd'}}, self.question(),
                         {'type': 'response_item', 'payload': {'type': 'function_call_output',
                          'call_id': 'question-1', 'output': '{"accepted":true}'}}])
        self.m._questions(s)
        self.assertEqual(len(s.questions), 1)
        self.rollout(s, [{'type': 'response_item', 'payload': {'type': 'function_call_output',
                         'call_id': 'question-1', 'output': '{"answers":{"environment":{"answers":["Test"]}}}'}}], 'a')
        self.m._questions(s)
        self.assertEqual(s.questions, [])

    def test_failed_closed_snapshot_does_not_block_other_finished_turns(self):
        s = self.session()
        s.state = 'waiting'
        s.turns = [{'n': 1, 'from': 'a' * 40, 'to': 'b' * 40, 'started': 1, 'ended': 2},
                   {'n': 2, 'from': 'b' * 40, 'to': None, 'started': 3, 'ended': 4,
                    'unavailable': 'Snapshot unavailable'}]
        with mock.patch.object(self.m, '_pin_turn'):
            self.assertEqual(self.m._revert_target(s, 1)[0], 'b' * 40)
        with self.assertRaisesRegex(ValueError, 'no finished snapshot'):
            self.m._revert_target(s, 2)


if __name__ == '__main__':
    unittest.main()
