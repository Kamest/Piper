"""Activity and pending questions remain truthful across reminders, replies and lost hooks."""
import base64
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

    # ---- Claude: the own conversation corrects what the hooks cannot say
    def claude(self, s, age=20):
        s = s if isinstance(s, sessions.Session) else self.session('claude')
        s.background_since = time.time() - 600
        self.native(s, self.prompt(s, 'Inspect the example', age + 10), mode='w')
        return s

    def native(self, s, *records, mode='a'):
        self.rollout(s, records, mode)
        with mock.patch.object(self.m, '_turn_event'):
            self.m._activity(s)

    def prompt(self, s, text, age=0.0, **extra):
        return {'type': 'user', 'sessionId': s.claude_session, 'timestamp': self.stamp(time.time() - age),
                'message': {'role': 'user', 'content': text}, **extra}

    def tools(self, s, message, *calls, age=0.0, stop='tool_use', **extra):
        """One model message; calls are (tool id, tool name[, input])."""
        return {'type': 'assistant', 'sessionId': s.claude_session, 'timestamp': self.stamp(time.time() - age),
                'message': {'id': message, 'stop_reason': stop, 'content': [
                    {'type': 'tool_use', 'id': c[0], 'name': c[1], 'input': c[2] if len(c) > 2 else {}} for c in calls]
                    or [{'type': 'text', 'text': 'Done'}]}, **extra}

    def result(self, s, ident, age=0.0, error=False, **extra):
        return {'type': 'user', 'sessionId': s.claude_session, 'timestamp': self.stamp(time.time() - age),
                'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': ident, 'is_error': error}]}, **extra}

    def permission(self, s, age=0.0):
        self.hooks(s, {'ev': 'Notification', 'notification_type': 'permission_prompt',
                       'message': 'Claude needs your permission', 'ts': time.time() - age})
        with mock.patch.object(self.m, '_turn_event'):
            self.m._activity(s)

    def actions(self, s):
        return feed.derive_actions([feed.session_snap(s)], self.m.pending_list(), [], {}, {}, time.time(), 0)

    def test_answered_claude_permission_returns_to_working(self):
        s = self.claude(None)
        self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), ('tool-2', 'Read'), age=15))
        self.permission(s)
        self.assertEqual((s.state, s.idle), ('attention', False))
        self.native(s, self.result(s, 'tool-2'))   # a parallel call finished; the dialog is still up
        self.assertEqual((s.state, s.idle), ('attention', False))
        self.assertTrue([a for a in self.actions(s) if a['key'].startswith('perm:')])
        with mock.patch.object(sessions, 'audit') as record:
            self.native(s, self.result(s, 'tool-1'))
        self.assertEqual((s.state, s.idle, s.detail), ('working', False, ''))
        self.assertFalse([a for a in self.actions(s) if a['level'] in feed.LEVELS[:5]])
        entry = [c.args[0] for c in record.call_args_list if c.args[0].get('event') == 'activity-state']
        self.assertEqual([e['source'] for e in entry], ['native-progress'])
        self.assertNotIn('tool-1', json.dumps(entry))
        # the next dialog of the same turn blocks again, and a later model message proves it is gone
        self.native(s, self.tools(s, 'msg-2', ('tool-3', 'Bash')))
        self.permission(s)
        self.assertEqual(s.state, 'attention')
        self.native(s, self.tools(s, 'msg-3', ('tool-4', 'Read')))
        self.assertEqual(s.state, 'working')
        self.hooks(s, {'ev': 'Stop'})
        self.assertEqual((s.state, s.idle), ('waiting', True))

    def test_answered_permission_needs_native_progress_and_keeps_other_blockers(self):
        for blocker in ('quiet', 'history', 'child', 'card', 'auth', 'dialog', 'l5', 'sidechain', 'foreign', 'malformed'):
            with self.subTest(blocker=blocker):
                s = self.claude(None)
                self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=15))
                if blocker == 'history':   # everything before the prompt was seen is no answer to it
                    self.rollout(s, [self.result(s, 'tool-0'), self.tools(s, 'msg-0', ('tool-0', 'Read'))], 'a')
                self.permission(s)
                expect = s.state, s.idle, s.detail
                if blocker == 'child': s.subagents = {'worker': 'reviewer'}
                if blocker == 'card': self.m.pending['card'] = {'session': s.id}
                if blocker == 'auth': s.phase = 'agent-auth'
                if blocker == 'dialog': s.phase = 'agent'
                if blocker == 'l5': s.detail = expect = 'L5: a new sensitive file'
                extra = {'isSidechain': True} if blocker == 'sidechain' else {'sessionId': 'foreign-thread'} if blocker == 'foreign' else {}
                if blocker == 'malformed':
                    with self.rollout(s, [], 'a').open('a') as f: f.write('{broken record}\n')
                if blocker not in ('quiet', 'history'):
                    self.native(s, self.result(s, 'tool-1', **extra))
                    if blocker in ('sidechain', 'foreign'):
                        self.native(s, self.tools(s, 'msg-2', ('tool-2', 'Read'), **extra))
                self.m._activity(s)
                self.assertEqual(s.state, 'attention')
                self.assertEqual(s.detail if blocker == 'l5' else (s.state, s.idle, s.detail), expect)
                self.m.pending.clear()

    def test_denied_or_interrupted_permission_ends_the_turn_without_authorizing_input(self):
        for marker in ('[Request interrupted by user for tool use]', '[Request interrupted by user]'):
            with self.subTest(marker=marker):
                s = self.claude(None, age=60)
                self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=50))
                self.permission(s, age=40)
                self.native(s, self.result(s, 'tool-1', age=10, error=True), self.prompt(s, [{'type': 'text', 'text': marker}], age=10))
                self.m._activity(s)
                self.assertEqual((s.state, s.idle), ('waiting', False))
                self.assertIn('not confirmed', s.detail)
                self.assertFalse(self.m._istate_at_prompt(s))
                self.assertFalse([a for a in self.actions(s) if a['key'].startswith('perm:')])
                self.hooks(s, {'ev': 'Notification', 'notification_type': 'idle_prompt'})
                self.assertEqual((s.state, s.idle), ('waiting', True))

    def test_interrupt_and_api_error_end_a_working_turn_without_stop(self):
        for ending in ('interrupt', 'api-error'):
            with self.subTest(ending=ending):
                s = self.claude(None, age=60)
                last = self.prompt(s, [{'type': 'text', 'text': '[Request interrupted by user]'}], age=8) if ending == 'interrupt' else \
                    self.tools(s, 'msg-9', age=8, stop='stop_sequence', isApiErrorMessage=True)
                self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=50), self.result(s, 'tool-1', age=40), last)
                self.assertEqual((s.state, s.idle), ('waiting', False))
                self.assertIn('not confirmed', s.detail)

    def test_native_turn_started_without_a_prompt_hook_is_working(self):
        for start in ('queued', 'shell'):
            with self.subTest(start=start):
                s = self.claude(None)
                self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=15), self.result(s, 'tool-1', age=14),
                            self.tools(s, 'msg-2', age=12, stop='end_turn'))
                self.hooks(s, {'ev': 'UserPromptSubmit', 'prompt': 'Queued while the turn ran'}, {'ev': 'Stop'})
                self.m._activity(s)
                self.assertEqual((s.state, s.idle), ('waiting', True))
                opening = self.prompt(s, 'Queued while the turn ran' if start == 'queued' else '<bash-input>example</bash-input>')
                self.native(s, opening)
                self.assertEqual(s.state, 'waiting')   # nothing ran yet
                with mock.patch.object(sessions, 'audit') as record:
                    self.native(s, self.tools(s, 'msg-3', ('tool-2', 'Bash')))
                self.assertEqual((s.state, s.idle, s.detail), ('working', False, ''))
                self.assertFalse(self.m._istate_at_prompt(s))
                self.assertFalse([a for a in self.actions(s) if a['key'].startswith('wait:')])
                self.assertEqual([c.args[0]['source'] for c in record.call_args_list
                                  if c.args[0].get('event') == 'activity-state'], ['native-progress'])
                self.native(s, self.result(s, 'tool-2'), self.tools(s, 'msg-4', stop='end_turn'))
                self.hooks(s, {'ev': 'Stop'})
                self.assertEqual((s.state, s.idle), ('waiting', True))
                self.m._activity(s)
                self.assertEqual((s.state, s.idle), ('waiting', True))

    DIALOG = 'Bash command\n  sleep 600\nDo you want to proceed?\n\u276f 1. Yes\n  2. No\n\nEsc to cancel \u00b7 Tab to amend\n'
    MAIN = '\u273b Working\u2026 (12s \u00b7 1k tokens)\n' + '\u2500' * 20 + '\n\u276f \n' + '\u2500' * 20 + '\n  auto mode on (shift+tab to cycle)\n'

    def draw(self, s, text):
        data = text if isinstance(text, bytes) else text.encode()
        self.m._on_pty_event({'id': s.id, 'ev': 'data', 'data': base64.b64encode(data).decode()})

    def look(self, s, later=0.0):
        """One poll of the screen watch, `later` seconds after now."""
        now = time.monotonic() + later
        with mock.patch.object(sessions.time, 'monotonic', return_value=now):
            self.m._permission_screen(s)

    def asking(self, s, seed=None):
        """A long tool call whose permission dialog is up and reported; the direct session's screen is `seed`."""
        self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=15))
        s.text = self.MAIN + self.DIALOG if seed is None else seed
        self.permission(s)
        self.look(s)
        self.assertEqual((s.state, s.idle), ('attention', False))

    def test_approved_long_command_runs_as_soon_as_the_dialog_leaves_the_screen(self):
        s = self.claude(None)
        self.asking(s)
        self.draw(s, self.MAIN)
        self.look(s)   # one frame is not the screen standing
        self.assertEqual(s.state, 'attention')
        with mock.patch.object(sessions, 'audit') as record:
            self.look(s, 1)
        self.assertEqual((s.state, s.idle, s.detail, s.permission_ts), ('working', False, '', 0.0))
        self.assertEqual(record.call_args.args[0]['source'], 'screen')
        self.assertFalse([a for a in self.actions(s) if a['key'].startswith('perm:')])
        self.look(s, 2)
        self.assertEqual((s.permission_watch, s.permission_raw), (0.0, b''))
        self.native(s, self.result(s, 'tool-1'))   # the tool returns much later: nothing to correct
        self.assertEqual(s.state, 'working')

    def test_repainted_dialog_and_marker_text_inside_it_keep_the_session_blocked(self):
        s = self.claude(None)
        self.asking(s)
        self.draw(s, 'earlier output \u2500\u276f ? for shortcuts\n')   # a repaint draws the conversation first
        self.look(s, 0.1)
        self.draw(s, self.DIALOG)   # ... and the dialog last
        self.look(s, 5)
        self.assertEqual(s.state, 'attention')
        self.draw(s, 'Bash command\n  echo "\u2500\u276f (shift+tab to cycle)"\n' + self.DIALOG)   # the command quotes the markers
        self.look(s, 9)
        self.look(s, 10)
        self.assertEqual((s.state, s.permission_clear), ('attention', 0.0))
        self.assertTrue([a for a in self.actions(s) if a['key'].startswith('perm:')])

    def test_main_screen_split_between_terminal_chunks_still_ends_the_dialog(self):
        s = self.claude(None)
        self.asking(s)
        frame = ('\u2500' * 5 + '\x1b[2K\x1b[1G\u276f \n').encode()
        cut = frame.index(b'\x1b[2K') + 3   # inside the escape sequence between the box and its prompt
        self.draw(s, frame[:cut])
        self.draw(s, frame[cut:-4])   # ... and inside the prompt character
        self.draw(s, frame[-4:])
        self.look(s)
        self.look(s, 1)
        self.assertEqual(s.state, 'working')

    def test_screen_without_a_seen_dialog_never_ends_a_permission_request(self):
        for seed in ('', self.DIALOG + self.MAIN, self.MAIN):   # nothing, an older answered dialog, the plain prompt
            with self.subTest(seed=seed[:12]):
                s = self.claude(None)
                self.asking(s, seed)
                self.draw(s, self.MAIN)
                self.look(s)
                self.look(s, 5)
                self.assertEqual(s.state, 'attention')
                self.native(s, self.result(s, 'tool-1'))   # the conversation still answers it
                self.assertEqual(s.state, 'working')
                self.m.sessions.pop(s.id)

    def test_screen_of_an_earlier_dialog_never_answers_the_next_one(self):
        s = self.claude(None)
        self.asking(s)
        self.draw(s, self.MAIN)   # answered, the poll has not looked yet
        s.text = ''               # the next dialog is of a kind whose footer is not recognised
        self.permission(s)
        self.look(s, 1)
        self.look(s, 5)
        self.assertEqual((s.state, s.permission_seen), ('attention', False))

    def test_denied_dialog_leaves_the_screen_and_the_interrupt_ends_the_turn(self):
        s = self.claude(None)
        self.asking(s)
        self.draw(s, self.MAIN)
        self.look(s)
        self.look(s, 1)
        self.assertEqual(s.state, 'working')
        self.native(s, self.prompt(s, '[Request interrupted by user for tool use]'))
        with mock.patch.object(sessions.time, 'time', return_value=time.time() + 10):
            self.native(s)
        self.assertEqual((s.state, s.idle), ('waiting', False))

    def test_question_card_and_flag_are_not_cleared_by_the_screen(self):
        for blocker in ('question', 'card', 'flag', 'login'):
            with self.subTest(blocker=blocker):
                s = self.claude(None)
                self.asking(s)
                if blocker == 'question': s.questions = [{'id': 'ask-1', 'text': 'Which?', 'ts': time.time()}]
                if blocker == 'card': self.m.pending['card'] = {'session': s.id}
                if blocker == 'flag': s.detail = 'L5: a sensitive file appeared'
                if blocker == 'login': s.phase = 'agent-auth'
                self.draw(s, self.MAIN)
                self.look(s)
                self.look(s, 1)
                self.assertEqual(s.state, 'attention')
                self.m.pending.clear()
                self.m.sessions.pop(s.id)

    def test_sandboxed_session_watches_its_terminal_only_while_the_dialog_is_reported(self):
        s = self.claude(None)
        s.runtime = 'hole'
        self.m.pty = mock.Mock()
        self.addCleanup(setattr, self.m, 'pty', None)
        self.m.pty.call.return_value = {'ok': True, 'info': {'bytes': 100000}}
        self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=15))
        self.permission(s)
        self.look(s)
        self.assertEqual(self.m.pty.call.call_args_list, [
            mock.call('subscribe', id=s.id, data=False), mock.call('subscribe', id=s.id, **{'from': 100000 - sessions.PERMISSION_TAIL})])
        tail = (self.DIALOG + self.MAIN + self.DIALOG).encode()
        tail = b'x' * (sessions.PERMISSION_TAIL - len(tail)) + tail
        self.draw(s, tail[:9000])   # the screen as it stood, in two chunks
        self.assertFalse(s.permission_seen)
        self.draw(s, tail[9000:] + self.MAIN.encode()[:10])   # ... the second one already carries new output
        self.assertTrue(s.permission_seen)
        self.draw(s, self.MAIN.encode()[10:])
        self.look(s)
        self.look(s, 1)
        self.assertEqual(s.state, 'working')
        self.m.pty.call.reset_mock()
        self.look(s, 2)
        self.m.pty.call.assert_called_once_with('subscribe', id=s.id, data=False)
        self.assertFalse(s.permission_sub)

    def test_restart_restores_the_watch_of_a_dialog_that_is_still_up(self):
        s = self.claude(None)
        self.native(s, self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=95))
        asked = float(int(time.time()) - 90)
        s.state, s.idle, s.since, s.detail = 'attention', False, asked, 'Claude needs your permission'   # as persisted
        with open(os.path.join(s.box, s.names['events']), 'a') as f:   # what a restarted piperd reads again
            f.write(json.dumps({'ev': 'Notification', 'notification_type': 'permission_prompt', 'ts': asked,
                                'message': 'Claude needs your permission', 'session_id': s.claude_session}) + '\n')
        s.text = self.MAIN + self.DIALOG   # the terminal's tail, subscribed again
        self.assertFalse(s.ev_live)
        self.hooks(s)
        self.assertEqual((s.state, s.since, s.permission_ts), ('attention', asked, asked))
        self.look(s)
        self.assertTrue(s.permission_seen)
        self.draw(s, self.MAIN)
        self.look(s)
        self.look(s, 1)
        self.assertEqual(s.state, 'working')

    def test_claude_turn_without_stop_keeps_running_while_a_child_or_background_task_runs(self):
        for blocker in ('child', 'background', 'none'):
            with self.subTest(blocker=blocker):
                s = self.claude(None)
                if blocker == 'child': s.subagents = {'worker': 'reviewer'}
                if blocker == 'background': s.background_tasks = {'job': 'shell'}
                self.native(s, self.tools(s, 'msg-end', age=10, stop='end_turn'))
                self.assertEqual(s.state, 'working' if blocker != 'none' else 'waiting')
                self.m.sessions.pop(s.id)

    def test_finished_turn_stays_waiting_without_new_native_tool_activity(self):
        for case in ('unflushed', 'answer', 'sidechain', 'foreign', 'auth', 'codex'):
            with self.subTest(case=case):
                s = self.session('codex') if case == 'codex' else self.claude(None)
                tail = self.tools(s, 'msg-1', ('tool-1', 'Bash'), age=15)
                if case == 'codex':
                    self.completed_turn(s)
                else:
                    self.rollout(s, [tail], 'a')   # the turn's last records, seen only together with its Stop
                self.hooks(s, {'ev': 'Stop'})
                if case == 'auth': s.phase = 'agent-auth'
                expect = s.state, s.idle
                self.m._activity(s)
                extra = {'isSidechain': True} if case == 'sidechain' else {'sessionId': 'foreign-thread'} if case == 'foreign' else {}
                if case == 'answer':
                    self.native(s, self.prompt(s, 'Queued question'), self.tools(s, 'msg-2', stop='end_turn'))
                elif case not in ('unflushed', 'codex'):
                    self.native(s, self.prompt(s, 'Queued', **extra), self.tools(s, 'msg-2', ('tool-2', 'Bash'), **extra))
                self.m._activity(s)
                self.assertEqual((s.state, s.idle), expect)

    def ask(self, s, ident='ask-1', message='msg-ask', questions=('Which environment?',), age=0.0):
        return self.tools(s, message, (ident, 'AskUserQuestion', {'questions': [
            {'question': q, 'header': 'Target', 'options': [{'label': 'Test', 'description': 'Safe'}, {'label': 'Production'}]}
            for q in questions]}), age=age)

    def test_claude_question_is_a_question_and_not_a_permission_request(self):
        s = self.claude(None)
        with mock.patch.object(sessions, 'audit') as record:
            self.native(s, self.ask(s))
        self.assertEqual((s.state, s.idle, s.detail), ('attention', False, 'Claude has a question'))
        self.assertFalse(self.m._istate_at_prompt(s))
        self.assertEqual([(q['id'], q['tool']) for q in s.questions], [('ask-1', 'AskUserQuestion')])
        self.assertIn('Which environment?', s.questions[0]['text'])
        self.assertIn('- Test: Safe', s.questions[0]['text'])
        entries = [c.args[0] for c in record.call_args_list if c.args[0].get('event') == 'activity-state']
        self.assertNotIn('Which environment', json.dumps(entries))
        self.permission(s)   # Claude's reminder for the same dialog, about six seconds later
        self.assertEqual((s.state, s.idle, s.detail), ('attention', False, 'Claude has a question'))
        actions = self.actions(s)
        self.assertEqual([a['level'] for a in actions if a['key'].startswith('question:')], ['waiting'])
        self.assertFalse([a for a in actions if a['key'].startswith(('perm:', 'wait:'))])
        restored = sessions.Session(**s.to_json())
        self.assertEqual(restored.questions, s.questions)
        self.native(s, self.result(s, 'ask-1'))
        self.assertEqual((s.state, s.idle, s.detail, s.questions), ('working', False, '', []))
        self.assertFalse([a for a in self.actions(s) if a['level'] in feed.LEVELS[:5]])

    def test_claude_question_closes_with_its_dialog_and_ignores_old_history(self):
        for ending in ('cancelled', 'interrupted', 'next-message', 'next-prompt', 'stop'):
            with self.subTest(ending=ending):
                s = self.claude(None, age=60)
                self.native(s, self.ask(s, age=40))
                self.assertEqual(len(s.questions), 1)
                if ending == 'cancelled': self.native(s, self.result(s, 'ask-1', error=True))
                if ending == 'interrupted':
                    self.native(s, self.result(s, 'ask-1', age=10, error=True),
                                self.prompt(s, [{'type': 'text', 'text': '[Request interrupted by user for tool use]'}], age=10))
                if ending == 'next-message': self.native(s, self.tools(s, 'msg-2', ('tool-2', 'Bash')))
                if ending == 'next-prompt': self.native(s, self.prompt(s, 'Never mind'))
                if ending == 'stop':
                    self.native(s, self.result(s, 'ask-1'), self.tools(s, 'msg-2', stop='end_turn'))
                    self.hooks(s, {'ev': 'Stop'})
                self.m._activity(s)
                self.assertEqual(s.questions, [])
                self.assertNotEqual(s.detail, 'Claude has a question')
                self.assertFalse([a for a in self.actions(s) if a['key'].startswith('question:')])
                self.assertEqual(s.state, {'interrupted': 'waiting', 'stop': 'waiting'}.get(ending, 'working'))
        s = self.claude(None)   # an unanswered question of an earlier process, then the conversation went on
        self.rollout(s, [self.ask(s, age=500), self.prompt(s, 'Continue', age=30)], 'a')
        self.m._activity(s)
        self.assertEqual((s.state, s.questions), ('working', []))
        sidechain = self.claude(None)
        self.native(sidechain, {**self.ask(sidechain), 'isSidechain': True})
        self.assertEqual((sidechain.state, sidechain.questions), ('working', []))

    def test_codex_question_keeps_a_real_permission_request_visible(self):
        s = self.session()
        self.rollout(s, [{'type': 'session_meta', 'payload': {'id': s.claude_session}}, self.question()])
        self.m._questions(s)
        self.hooks(s, {'ev': 'PermissionRequest', 'tool_name': 'shell'})
        self.assertEqual(s.state, 'attention')
        keys = [a['key'].split(':')[0] for a in self.actions(s)]
        self.assertEqual(sorted(keys), ['perm', 'question'])

    def test_replayed_hooks_keep_the_time_a_session_started_waiting(self):
        s = self.session('claude')
        s.state, s.idle, s.since = 'waiting', True, float(int(time.time()) - 500)
        stopped = s.since
        with open(os.path.join(s.box, s.names['events']), 'a') as f:   # what a restarted piperd reads again
            for ev, ts in (('SessionStart', stopped - 100), ('UserPromptSubmit', stopped - 90), ('Stop', stopped)):
                f.write(json.dumps({'ev': ev, 'ts': ts, 'session_id': s.claude_session}) + '\n')
        before = [a['key'] for a in self.actions(s)]
        self.assertFalse(s.ev_live)
        with mock.patch.object(self.m, '_turn_event'), mock.patch.object(self.m, '_last_key', return_value=None):
            self.m._events(s)
        self.assertEqual((s.state, s.idle), ('waiting', True))
        self.assertAlmostEqual(s.since, stopped, delta=1)
        self.assertEqual([a['key'] for a in self.actions(s)], before)
        self.hooks(s)   # caught up: what follows is written now
        self.assertTrue(s.ev_live)
        self.hooks(s, {'ev': 'UserPromptSubmit', 'ts': time.time() - 60})   # live again: the host clock, as before
        self.assertAlmostEqual(s.since, time.time(), delta=5)
