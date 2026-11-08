"""Activity and pending questions remain truthful across reminders, replies and lost hooks."""
import datetime
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import _env
import common
import feed
import mobilepush
import sessions


class WaitingTest(unittest.TestCase):
    def setUp(self):
        self.m = _env.manager(self, background=False)
        self.addCleanup(self.m.sessions.clear)

    def session(self, agent='codex'):
        s = sessions.Session(project='acme/shop', path=_env.PP, agent=agent, runtime='direct', state='working')
        s.claude_mode = 'none' if agent == 'codex' else 'host'
        s.claude_session = '12345678-abcd'
        s.pid, s.instance = 123, 'example-process'
        s.background_since = time.time() - 30
        self.m.sessions[s.id] = s
        Path(s.box).mkdir(parents=True, exist_ok=True)
        return s

    def hooks(self, s, *records):
        with open(os.path.join(s.box, s.names['events']), 'a') as f:
            for record in records:
                f.write(json.dumps({'ts': time.time(), 'session_id': s.claude_session, **record}) + '\n')
        with mock.patch.object(self.m, '_turn_event'), mock.patch.object(self.m, '_last_key', return_value=None):
            self.m._events(s)

    def rollout(self, s, records, mode='w'):
        if s.agent == 'codex':
            path = Path(s.codex_home) / ('sessions/2026/11/08/rollout-waiting-' + s.claude_session + '.jsonl')
        else:
            path = Path(s.claude_src) / ('projects/example/' + s.claude_session + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode) as f:
            for rec in records:
                f.write(json.dumps(rec) + '\n')
        return path

    @staticmethod
    def stamp(ts):
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()

    def question(self, ident='ask-1', titles=('Choose an environment',)):
        return {'type': 'response_item', 'timestamp': self.stamp(time.time()), 'payload': {
            'type': 'function_call', 'name': 'request_user_input_async', 'call_id': ident,
            'arguments': json.dumps({'questions': [{'title': title} for title in titles]})}}

    def user(self, text):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': text}]}}

    def answer(self, ident, index=0):
        return self.user('<send_user_message_question_reply>\n' + json.dumps([{
            'answer': 'Test', 'questionItemId': json.dumps(['request_user_input_async', ident, index])
        }]) + '\n</send_user_message_question_reply>')

    def test_status_message_preserves_question_and_its_action_after_reload(self):
        s = self.session()
        path = self.rollout(s, [{'type': 'session_meta', 'payload': {'id': s.claude_session}}, self.question()])
        self.m._questions(s)
        original = list(s.questions)
        self.rollout(s, [self.user('How long will this take?')], 'a')
        self.m._questions(s)
        self.assertEqual(s.questions, original)
        restored = sessions.Session(**s.to_json())
        self.m._question_cache.clear()
        self.m._questions(restored)
        self.assertEqual(restored.questions, original)
        actions = feed.derive_actions([feed.session_snap(restored)], [], [], {}, {}, time.time(), 0)
        self.assertEqual(len([a for a in actions if a['key'].startswith('question:')]), 1)
        self.assertIn('ask-1', path.read_text())

    def test_structured_reply_closes_only_its_tool_call_and_item(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': s.claude_session}},
                         self.question('ask-1', ('First choice', 'Second choice')), self.question('ask-2')])
        self.m._questions(s)
        self.rollout(s, [self.answer('ask-1', 0)], 'a')
        self.m._questions(s)
        self.assertEqual([q['id'] for q in s.questions], ['ask-1', 'ask-2'])
        self.assertNotIn('First choice', s.questions[0]['text'])
        self.assertIn('Second choice', s.questions[0]['text'])
        restored = sessions.Session(**s.to_json())
        self.m._question_cache.clear()
        self.m._questions(restored)
        self.assertEqual(restored.questions, s.questions)
        self.rollout(s, [self.answer('ask-1', 1)], 'a')
        self.m._questions(s)
        self.assertEqual([q['id'] for q in s.questions], ['ask-2'])
        self.rollout(s, [self.answer('foreign'), self.user('Continue working')], 'a')
        self.m._questions(s)
        self.assertEqual([q['id'] for q in s.questions], ['ask-2'])

    def test_explicit_tool_cancellation_closes_only_its_question(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': s.claude_session}},
                         self.question('ask-1'), self.question('ask-2'),
                         {'type': 'response_item', 'payload': {'type': 'function_call_output',
                          'call_id': 'ask-1', 'output': '{"cancelled":true}'}}])
        self.m._questions(s)
        self.assertEqual([q['id'] for q in s.questions], ['ask-2'])

    def test_sync_partial_and_empty_answers_preserve_unanswered_items(self):
        s = self.session()
        q = self.question(titles=('First', 'Second'))
        q['payload']['name'] = 'request_user_input'
        q['payload']['arguments'] = json.dumps({'questions': [{'id': 'first', 'question': 'First'}, {'id': 'second', 'question': 'Second'}]})
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': s.claude_session}}, q])
        self.m._questions(s)
        for answers, expected in (({'first': {'answers': []}}, 'First'), ({'first': {'answers': ['Test']}}, 'Second')):
            self.rollout(s, [{'type': 'response_item', 'payload': {'type': 'function_call_output',
                'call_id': 'ask-1', 'output': json.dumps({'answers': answers})}}], 'a')
            self.m._questions(s)
            self.assertIn(expected, s.questions[0]['text'])
        self.assertNotIn('First', s.questions[0]['text'])

    def test_idle_reminder_keeps_waiting_identity_and_does_not_push_twice(self):
        s = self.session('claude')
        detector = mobilepush.Events()
        self.assertFalse(detector.changed(mobilepush.snapshot(self.m)))
        self.hooks(s, {'ev': 'Stop'})
        self.assertTrue(detector.changed(mobilepush.snapshot(self.m)))
        since, stop = s.since, s.stop_ts
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'idle_prompt', 'message': 'Waiting for input'})
        self.assertEqual((s.state, s.idle, s.since, s.stop_ts), ('waiting', True, since, stop))
        self.assertFalse(detector.changed(mobilepush.snapshot(self.m)))
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'permission_prompt', 'message': 'Allow tool?'})
        self.assertEqual((s.state, s.idle), ('attention', False))
        self.assertTrue(detector.changed(mobilepush.snapshot(self.m)))

    def test_informational_notifications_preserve_activity_and_permission(self):
        for kind in ('auth_success', 'agent_completed'):
            for state, idle in (('working', False), ('waiting', True), ('attention', False)):
                with self.subTest(kind=kind, state=state):
                    s = self.session('claude')
                    s.state, s.idle, s.detail = state, idle, 'Existing state'
                    before = s.state, s.idle, s.detail, s.since
                    self.hooks(s, {'ev': 'Notification', 'notification_type': kind, 'message': 'Informational'})
                    self.assertEqual((s.state, s.idle, s.detail, s.since), before)

    def test_idle_reminder_does_not_clear_permission_and_legacy_mobile_does_not_duplicate(self):
        s = self.session('claude')
        detector = mobilepush.Events()
        s.state, s.idle, s.stop_ts = 'waiting', True, time.time() - 60
        self.assertFalse(detector.changed(mobilepush.snapshot(self.m)))
        s.state, s.since = 'attention', time.time()
        self.assertFalse(detector.changed(mobilepush.snapshot(self.m)))
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'permission_prompt', 'message': 'Allow?'})
        before = s.state, s.idle, s.since, s.detail
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'idle_prompt'})
        self.assertEqual((s.state, s.idle, s.since, s.detail), before)

    def completed_turn(self, s, *, own=True, complete=True):
        started, ended = time.time() - 12, time.time() - 8
        if s.agent == 'codex':
            records = [{'type': 'session_meta', 'payload': {'id': s.claude_session if own else 'foreign-thread'}},
                       {'type': 'event_msg', 'timestamp': self.stamp(started),
                        'payload': {'type': 'task_started', 'turn_id': 'turn-1'}}]
            if complete:
                records.append({'type': 'event_msg', 'timestamp': self.stamp(ended),
                                'payload': {'type': 'task_complete', 'turn_id': 'turn-1'}})
        else:
            records = [{'type': 'user', 'sessionId': s.claude_session if own else 'foreign-thread',
                        'timestamp': self.stamp(started), 'message': {'content': 'Inspect the example'}}]
            if complete:
                records.append({'type': 'assistant', 'sessionId': records[0]['sessionId'],
                                'timestamp': self.stamp(ended), 'message': {
                                    'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Done'}]}})
        self.rollout(s, records)
        return ended

    def test_missing_stop_recovers_completed_own_turn_without_authorizing_input(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                s = self.session(agent)
                self.completed_turn(s)
                with mock.patch.object(self.m, '_turn_event') as turn:
                    self.m._activity(s)
                    self.m._activity(s)
                self.assertEqual((s.state, s.idle), ('waiting', False))
                self.assertIn('not confirmed', s.detail)
                self.assertFalse(self.m._istate_at_prompt(s))
                self.assertEqual(turn.call_count, 1)
                s.handoff_notify = ['example-child']
                with mock.patch.object(self.m, 'type_text') as typed:
                    self.m._flush_handoff_notes(s)
                    typed.assert_not_called()

    def test_native_recovery_preserves_newer_activity_and_all_blockers(self):
        for blocker in ('foreign', 'incomplete', 'process', 'newer-hook', 'child', 'background', 'permission', 'card', 'auth'):
            with self.subTest(blocker=blocker):
                s = self.session()
                self.completed_turn(s, own=blocker != 'foreign', complete=blocker != 'incomplete')
                if blocker == 'process': s.background_since = time.time()
                if blocker == 'newer-hook': self.hooks(s, {'ev': 'PostToolUse', 'tool_name': 'Bash'})
                if blocker == 'child': s.subagents = {'worker': 'reviewer'}
                if blocker == 'background': s.background_tasks = {'job': 'shell'}
                if blocker == 'permission': s.codex_permissions = {'legacy': 1}
                if blocker == 'card': self.m.pending['card'] = {'session': s.id}
                if blocker == 'auth': s.phase = 'agent-auth'
                self.m._activity(s)
                self.assertEqual((s.state, s.idle), ('working', False))
                self.m.pending.clear()

    def test_native_recovery_waits_for_complete_records_and_rejects_a_new_turn(self):
        for agent in ('claude', 'codex'):
            with self.subTest(agent=agent):
                s = self.session(agent)
                self.completed_turn(s)
                path = self.rollout(s, [], 'a')
                if agent == 'codex':
                    next_turn = {'type': 'event_msg', 'timestamp': self.stamp(time.time() - 1),
                                 'payload': {'type': 'task_started', 'turn_id': 'turn-2'}}
                else:
                    next_turn = {'type': 'user', 'sessionId': s.claude_session,
                                 'timestamp': self.stamp(time.time() - 1), 'message': {'content': 'Continue'}}
                with path.open('a') as f: f.write(json.dumps(next_turn))
                self.m._activity(s)
                self.assertEqual(s.state, 'working')
                with path.open('a') as f: f.write('\n')
                self.m._activity(s)
                self.assertEqual(s.state, 'working')

    def test_native_recovery_rejects_malformed_history_and_rebuilds_rewritten_files(self):
        s = self.session()
        self.completed_turn(s)
        path = self.rollout(s, [], 'a')
        with path.open('a') as f: f.write('{broken record}\n')
        self.m._activity(s)
        self.assertEqual(s.state, 'working')
        self.completed_turn(s, own=False)
        self.m._activity(s)
        self.assertEqual(s.state, 'working')
        self.completed_turn(s)
        with mock.patch.object(self.m, '_turn_event'):
            self.m._activity(s)
        self.assertEqual((s.state, s.idle), ('waiting', False))
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'idle_prompt'})
        self.assertEqual((s.state, s.idle), ('waiting', True))

    def test_new_native_turn_without_a_valid_clock_invalidates_previous_completion(self):
        for agent in ('claude', 'codex'):
            for stamp in (None, 'invalid', self.stamp(time.time() + 30)):
                with self.subTest(agent=agent, stamp=stamp):
                    s = self.session(agent)
                    self.completed_turn(s)
                    rec = {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-2'}} if agent == 'codex' else {
                        'type': 'user', 'sessionId': s.claude_session, 'message': {'content': 'Continue'}}
                    rec['timestamp'] = stamp
                    self.rollout(s, [rec], 'a')
                    self.m._activity(s)
                    self.assertEqual((s.state, s.idle), ('working', False))

    def test_late_stop_confirms_recovered_prompt_without_a_second_push(self):
        s = self.session()
        detector = mobilepush.Events()
        detector.changed(mobilepush.snapshot(self.m))
        self.completed_turn(s)
        with mock.patch.object(self.m, '_turn_event'):
            self.m._activity(s)
        self.assertTrue(detector.changed(mobilepush.snapshot(self.m)))
        self.assertFalse(next(row for row in self.m.list() if row['id'] == s.id)['at_prompt'])
        self.hooks(s, {'ev': 'Stop'})
        self.assertTrue(next(row for row in self.m.list() if row['id'] == s.id)['at_prompt'])
        self.assertFalse(detector.changed(mobilepush.snapshot(self.m)))
        self.hooks(s, {'ev': 'UserPromptSubmit'})
        detector.changed(mobilepush.snapshot(self.m))
        self.hooks(s, {'ev': 'Stop'})
        self.assertTrue(detector.changed(mobilepush.snapshot(self.m)))

    def test_activity_audit_records_only_changes_and_safe_source_metadata(self):
        s = self.session('claude')
        with mock.patch.object(sessions, 'audit') as record:
            self.hooks(s, {'ev': 'Notification', 'notification_type': 'permission_prompt',
                           'message': 'PLANTED_PRIVATE_PROMPT'})
            self.m._events(s)
        entries = [c.args[0] for c in record.call_args_list if c.args[0].get('event') == 'activity-state']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['source'], 'hook')
        self.assertIn('event_ts', entries[0])
        self.assertNotIn('PLANTED_PRIVATE_PROMPT', json.dumps(entries))
        with mock.patch.object(sessions, 'audit', side_effect=OSError('fixture failure')):
            self.hooks(s, {'ev': 'UserPromptSubmit'})
        self.assertEqual((s.state, s.idle), ('working', False))
