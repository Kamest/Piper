"""Quiet control delivery through real Manager/Session objects and legacy mailboxes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import features
from ptyclient import PtyHostError
import quietwindow as qw
import sessions


def legacy_client_source():
    source = Path(common.PIPER_DIR, 'sandbox/piper-mcp.py').read_text()
    source = source[:source.index('\nQUIET = [')] + source[source.index('\nCONFIRMED = ', source.index('\nQUIET = [')):]
    return source.replace('QUIET + ', '').replace('PROD + [t for t in QUIET if t["name"] in ("quiet_status", "quiet_ack", "quiet_wait")]', 'PROD')


class QuietDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='quiet-delivery-')
        self.addCleanup(self.tmp.cleanup)
        self.manager = sessions.Manager(background=False)
        self.addCleanup(self.manager.close)
        self.rows = [sessions.Session(id='d000000' + str(i), project='demo', title='Demo',
                                     path=_env.PP, pid=100+i, state='waiting', idle=True,
                                     runtime='direct', agent='codex', claude_mode='none') for i in range(5)]
        self.manager.sessions = {s.id: s for s in self.rows}
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True,
                                            path=os.path.join(self.tmp.name, 'quiet.json'))
        for s in self.rows:
            Path(s.box, 'piper').mkdir(parents=True, exist_ok=True)
        self.stack = mock.patch.object(self.manager, '_stack_up', return_value=False)
        self.stack.start(); self.addCleanup(self.stack.stop)
        self.manager._last_key = mock.Mock(return_value={'key_at': 0, 'enter_at': 0})
        self.manager.type_text = mock.Mock(return_value=True)
        self.rid = self.manager.quiet.request(self.rows[0].id, 'Tests')['active']['id']

    def sent(self):
        return {c.args[0].id: c.args[1] for c in self.manager.type_text.call_args_list}

    def test_idle_real_sessions_receive_requests_without_invented_at_prompt_attribute(self):
        self.assertFalse(hasattr(self.rows[1], 'at_prompt'))
        self.manager._quiet_tick()
        self.assertEqual(set(self.sent()), {s.id for s in self.rows})
        self.assertIn('quiet_ack', self.sent()[self.rows[1].id])
        self.assertIsNone(self.manager.quiet.snapshot()['active']['participants'][0]['at'])

    def test_working_agent_receives_a_control_prompt_without_waiting_for_entire_turn(self):
        self.rows[1].state, self.rows[1].idle = 'working', False
        self.manager._quiet_tick()
        self.assertIn(self.rows[1].id, self.sent())
        self.assertTrue(next(c for c in self.manager.type_text.call_args_list if c.args[0] is self.rows[1]).kwargs['_quiet_control'])

    def test_human_draft_is_preserved_and_delivery_retries_after_clear(self):
        target = self.rows[1]
        self.manager._last_key.side_effect = lambda s: {'key_at': 50 if s is target else 0, 'enter_at': 0}
        self.manager._quiet_tick()
        self.assertNotIn(target.id, self.sent())
        target.clear_at = 51
        self.manager._quiet_tick()
        self.assertIn(target.id, self.sent())

    def test_direct_approval_dialog_is_never_answered_by_a_control_prompt(self):
        target = self.rows[1]
        target.state, target.phase = 'attention', 'agent'
        self.manager._quiet_tick()
        self.assertNotIn(target.id, self.sent())
        target.state, target.phase = 'waiting', ''
        self.manager._quiet_tick()
        self.assertIn(target.id, self.sent())

    def test_screen_wait_is_not_a_safe_prompt(self):
        self.rows[1].screen_wait = True
        self.manager._quiet_tick()
        self.assertNotIn(self.rows[1].id, self.sent())

    def test_delivery_is_not_repeated_and_does_not_fake_an_ack(self):
        self.manager._quiet_tick()
        self.manager._quiet_tick()
        self.assertEqual(self.manager.type_text.call_count, 5)
        self.assertTrue(all(p['status'] == 'finishing' for p in self.manager.quiet.snapshot()['active']['participants']))

    def test_one_terminal_failure_does_not_starve_other_sessions_and_is_retryable(self):
        target = self.rows[1]
        def send(s, *a, **kw):
            if s is target: raise PtyHostError('disconnected')
            return True
        self.manager.type_text.side_effect = send
        self.manager._quiet_tick()
        self.assertIn(self.rows[-1].id, self.sent())
        self.assertNotIn(target.id, self.manager._quiet_notice)
        self.manager.type_text.side_effect = None
        self.manager._quiet_tick()
        self.assertIn(target.id, self.manager._quiet_notice)

    def test_one_unwritable_hint_does_not_starve_control_delivery(self):
        target = self.rows[1]
        write = common.safe_write
        def fail_one(root, path, value):
            if root == target.box: raise OSError('read-only')
            return write(root, path, value)
        with mock.patch.object(common, 'safe_write', side_effect=fail_one):
            self.manager._quiet_tick()
        self.assertIn(target.id, self.sent())
        self.assertIn(self.rows[-1].id, self.sent())
        self.assertNotIn(target.id, self.manager._quiet_hints)

    def test_declined_auto_input_is_retried(self):
        self.manager.type_text.return_value = False
        self.manager._quiet_tick()
        self.assertEqual(self.manager._quiet_notice, {})
        self.manager.type_text.return_value = True
        self.manager._quiet_tick()
        self.assertEqual(len(self.manager._quiet_notice), 5)

    def test_delivery_progress_distinguishes_draft_sent_and_agent_report(self):
        target = self.rows[1]
        self.manager._last_key.side_effect = lambda s: {'key_at': 50 if s is target else 0, 'enter_at': 0}
        self.manager._quiet_tick()
        p = self.manager.quiet.snapshot()['active']['participants'][0]
        self.assertIsNone(p['delivered_at'])
        self.assertIn('draft', p['delivery_detail'])
        target.clear_at = 51
        self.manager._quiet_tick()
        p = self.manager.quiet.snapshot()['active']['participants'][0]
        self.assertIsNotNone(p['delivered_at'])
        self.assertIsNone(p['at'])
        self.assertIn('waiting for an update', p['delivery_detail'])
        self.manager.tool(target, 'piper_session', {'quiet': {'action': 'ack', 'id': self.rid,
                          'finished': False, 'detail': 'Stopping existing compiler'}})
        p = self.manager.quiet.snapshot()['active']['participants'][0]
        self.assertEqual(p['detail'], 'Stopping existing compiler')
        self.assertEqual(p['status'], 'finishing')
        self.assertIsNotNone(p['at'])

    def test_progress_can_report_real_background_work_without_false_confirmation(self):
        target = self.rows[1]
        target.background_tasks = {'job': 'running'}
        target.subagents = {'child': 'finishing'}
        replies = self.legacy_calls(target, [{'action': 'ack', 'id': self.rid, 'finished': False,
                                             'detail': 'Waiting for the existing child test'}])
        self.assertFalse(replies[0]['isError'])
        p = self.manager.quiet.snapshot()['active']['participants'][0]
        self.assertIn('1 background task', p['activity'])
        self.assertIn('1 subagent', p['activity'])
        self.assertEqual(p['status'], 'finishing')
        self.assertTrue(self.manager.quiet.blocked(self.rows[0].id))

    def test_delivery_save_failure_isolated_from_other_participants_and_retried(self):
        target = self.rows[1]
        original = self.manager.quiet.delivery
        def delivery(sid, *a, **kw):
            if sid == target.id: raise OSError('disk full')
            return original(sid, *a, **kw)
        with mock.patch.object(self.manager.quiet, 'delivery', side_effect=delivery):
            self.manager._quiet_tick()
        self.assertIn(self.rows[-1].id, self.manager._quiet_notice)
        self.assertNotIn(target.id, self.manager._quiet_notice)
        self.manager._quiet_tick()
        self.assertIsNotNone(self.manager.quiet.snapshot()['active']['participants'][0]['delivered_at'])

    def test_release_notice_is_delivered_once_and_clears_the_local_hold(self):
        self.manager._quiet_tick()
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(self.manager.type_text.call_count, 5)
        self.assertIn('released', self.sent()[self.rows[1].id])
        self.assertFalse(json.loads(Path(self.rows[1].box, 'piper/quiet.json').read_text())['blocked'])
        self.manager._quiet_tick()
        self.assertEqual(self.manager.type_text.call_count, 5)

    def test_owner_renewal_notice_is_throttled(self):
        self.manager._quiet_tick()
        key, _ = self.manager._quiet_notice[self.rows[0].id]
        self.manager._quiet_notice[self.rows[0].id] = (key, time.monotonic()-31)
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(set(self.sent()), {self.rows[0].id})

    def test_resume_outbox_survives_restart_retries_drafts_and_never_resumes_new_runtime(self):
        self.manager._quiet_tick()
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        # A restart erases the volatile notice cache; the durable release still owes a notice.
        self.manager._quiet_notice.clear()
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True, path=self.manager.quiet.path)
        target = self.rows[1]
        self.manager._last_key.side_effect = lambda s: {'key_at': 50 if s is target else 0, 'enter_at': 0}
        self.rows[2].pid += 1
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertNotIn(target.id, self.sent())
        self.assertNotIn(self.rows[2].id, self.sent())
        self.assertIn(self.rows[-1].id, self.sent())
        parts = self.manager.quiet.snapshot()['last']['participants']
        self.assertEqual(parts[0]['resume']['status'], 'pending')
        self.assertIn('draft', parts[0]['resume']['detail'])
        self.assertIn('runtime', parts[1]['resume']['detail'])
        self.assertEqual(parts[-1]['resume']['status'], 'sent')
        target.clear_at = 51
        self.manager._quiet_tick()
        self.assertIn(target.id, self.sent())
        self.manager.type_text.reset_mock()
        self.manager._quiet_notice.clear()
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True, path=self.manager.quiet.path)
        self.manager._quiet_tick()
        self.assertEqual(self.sent(), {}, 'persisted sent receipts must survive restart too')

    def test_native_cleanup_is_cooperative_until_ack_and_sealed_before_confirmation(self):
        target = self.rows[1]
        self.manager.quiet.before_ack = self.manager._quiet_seal
        self.manager._quiet_tick()
        hint = Path(target.box, 'piper/quiet.json')
        def native_decision():
            result = subprocess.run([sys.executable, str(Path(common.PIPER_DIR, 'sandbox/piper-event.py')),
                                     'QuietCheckpoint', str(Path(target.box, 'events.jsonl'))],
                                    input=json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'stop own fixture'}}),
                                    text=True, capture_output=True, check=True)
            return json.loads(result.stdout) if result.stdout else None
        self.assertIs(json.loads(hint.read_text())['cleanup_allowed'], True)
        self.assertFalse(json.loads(hint.read_text())['blocked'], 'old native hooks must allow existing-job cleanup too')
        self.assertIsNone(native_decision())
        self.manager.tool(target, 'quiet_ack', {'id': self.rid, 'finished': False, 'detail': 'Stopping own server'})
        with mock.patch.object(common, 'safe_write', side_effect=OSError('read-only')):
            with self.assertRaises(OSError):
                self.manager.tool(target, 'quiet_ack', {'id': self.rid, 'finished': True})
        self.assertEqual(self.manager.quiet.snapshot()['active']['participants'][0]['status'], 'finishing')
        self.manager.tool(target, 'quiet_ack', {'id': self.rid, 'finished': True})
        self.assertIs(json.loads(hint.read_text())['cleanup_allowed'], False)
        self.assertTrue(json.loads(hint.read_text())['blocked'])
        self.assertEqual(native_decision()['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertEqual(self.manager.quiet.snapshot()['active']['participants'][0]['status'], 'waiting')

    def test_resume_write_failure_is_retryable_without_starving_other_sessions(self):
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        target = self.rows[1]
        original = self.manager.quiet.resume_delivery
        def receipt(sid, *a, **kw):
            if sid == target.id:
                raise OSError('disk full')
            return original(sid, *a, **kw)
        with mock.patch.object(self.manager.quiet, 'resume_delivery', side_effect=receipt):
            self.manager._quiet_tick()
        self.assertIn(self.rows[-1].id, self.sent())
        self.assertNotIn(target.id, self.manager._quiet_notice)
        self.assertEqual(self.manager.quiet.snapshot()['last']['participants'][0]['resume']['status'], 'pending')
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(set(self.sent()), {target.id})

    def test_upgrade_of_legacy_release_seeds_continue_delivery_only_once(self):
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        path = Path(self.manager.quiet.path)
        old = json.loads(path.read_text())
        old.pop('resumes')
        for p in old['last']['participants']:
            p.pop('resume')
        path.write_text(json.dumps(old))
        self.manager._quiet_notice.clear()
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True, path=str(path))
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(len(self.sent()), 5)
        self.manager.quiet = qw.Coordinator(lambda: self.rows, lambda: True, path=str(path))
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(self.sent(), {})

    def test_old_resume_outbox_does_not_starve_the_new_runtime_initial_task(self):
        self.manager._quiet_tick()
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        target = self.rows[1]
        target.pid += 1
        with mock.patch.object(self.manager, '_task_pending', side_effect=lambda s: s is target), \
                mock.patch.object(self.manager, '_deliver_task') as deliver:
            self.manager._quiet_tick()
        deliver.assert_called_once_with(target)
        self.assertNotIn(target.id, self.manager._quiet_notice)

    def test_queued_window_keeps_everyone_held_and_resumes_once_after_last_release(self):
        self.manager._quiet_tick()
        next_owner = self.rows[1]
        self.manager.quiet.request(next_owner.id, 'Next work')
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        second = self.manager.quiet.snapshot()['active']['id']
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertFalse(any('window is released.' in msg for msg in self.sent().values()))
        self.manager.quiet.cancel(next_owner.id, second)
        self.manager.type_text.reset_mock()
        self.manager._quiet_tick()
        self.assertEqual(len(self.sent()), 5)
        self.assertTrue(all('window is released.' in msg for msg in self.sent().values()))
        self.assertEqual(self.manager.quiet.snapshot()['resumes'], [])

    def test_legacy_owner_receives_work_state_controls_and_can_renew_during_long_commands(self):
        self.manager._quiet_tick()
        msg = self.sent()[self.rows[0].id]
        self.assertIn('asynchronously', msg)
        args = [json.loads(line)['quiet'] for line in msg.splitlines() if line.startswith('{"quiet":')]
        self.assertEqual([a['action'] for a in args], ['status', 'renew', 'work', 'work', 'release'])
        self.assertEqual([a['running'] for a in args if a['action'] == 'work'], [True, False])
        early = self.legacy_calls(self.rows[0], [args[2]])
        self.assertTrue(early[0]['isError'], 'a copied work example cannot start work before grant')
        for row in self.rows[1:]:
            self.manager.quiet.ack(row.id, self.rid, done=True)
        self.manager.quiet.tick()
        self.manager.quiet.settle = time.monotonic() - 1
        self.manager.quiet.tick()
        working = self.legacy_calls(self.rows[0], [args[2], args[1]])
        self.assertTrue(all(not r['isError'] for r in working))
        self.assertEqual(self.manager.quiet.snapshot()['active']['phase'], 'working')
        ended = self.legacy_calls(self.rows[0], [args[3], args[4]])
        self.assertTrue(all(not r['isError'] for r in ended))
        self.assertIsNone(self.manager.quiet.snapshot()['active'])

    def test_ready_idle_and_runtime_layout_matrix(self):
        target = self.rows[1]
        for runtime, agent, mode, layout in (
                ('direct', 'codex', 'none', 'piper'),
                ('direct', 'claude', 'session', 'piper'),
                ('hole', 'codex', 'none', 'piper'),
                ('hole', 'claude', 'host', 'piper'),
                ('hole', 'claude', 'session', 'orch')):
            with self.subTest(runtime=runtime, agent=agent, mode=mode, layout=layout):
                target.runtime, target.agent, target.claude_mode, target.layout = runtime, agent, mode, layout
                target.state, target.idle = 'ready', False
                Path(target.box, 'piper').mkdir(parents=True, exist_ok=True)
                self.manager._quiet_notice.clear()
                self.manager.type_text.reset_mock()
                self.manager._quiet_tick()
                self.assertIn(target.id, self.sent())
                self.assertIn('piper_session', self.sent()[target.id])

    def test_ready_with_undelivered_task_does_not_receive_control_into_unknown_screen(self):
        self.rows[1].state, self.rows[1].idle = 'ready', False
        self.rows[1].task, self.rows[1].task_sent = 'pending task', False
        self.manager._quiet_tick()
        self.assertNotIn(self.rows[1].id, self.sent())

    def test_background_work_is_not_mistaken_for_confirmation(self):
        target = self.rows[1]
        target.background_tasks = {'job': 'running'}
        self.manager._quiet_tick()
        self.assertIn(target.id, self.sent())
        with self.assertRaises(ValueError):
            self.manager.tool(target, 'quiet_ack', {'id': self.rid, 'finished': True})
        self.assertEqual(self.manager.quiet.snapshot()['active']['participants'][0]['status'], 'finishing')

    def test_old_window_cannot_acknowledge_a_new_window(self):
        target = self.rows[1]
        self.manager.quiet.cancel(self.rows[0].id, self.rid)
        current = self.manager.quiet.request(self.rows[0].id, 'Next')['active']['id']
        with self.assertRaises(ValueError):
            self.manager.tool(target, 'quiet_ack', {'id': self.rid, 'finished': True})
        self.assertEqual(self.manager.quiet.snapshot()['active']['id'], current)
        self.assertIsNone(self.manager.quiet.snapshot()['active']['participants'][0]['at'])

    def legacy_calls(self, row, controls):
        client = Path(row.box, 'piper/piper-mcp.py')
        client.write_text(legacy_client_source())
        messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}] + [
            {'jsonrpc': '2.0', 'id': i+2, 'method': 'tools/call',
             'params': {'name': 'piper_session', 'arguments': {'quiet': control}}}
            for i, control in enumerate(controls)]
        busy, stop = set(), threading.Event()
        def pump():
            while not stop.wait(.01):
                self.manager._mailbox(row, busy)
        worker = threading.Thread(target=pump, daemon=True); worker.start()
        try:
            result = subprocess.run([sys.executable, str(client), '--mailbox', os.path.join(row.box, row.names['mailbox'])],
                                    input='\n'.join(json.dumps(m) for m in messages)+'\n', text=True,
                                    capture_output=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line)['result'] for line in result.stdout.splitlines()]
            names = {tool['name'] for tool in replies[0]['tools']}
            self.assertIn('piper_session', names)
            self.assertNotIn('quiet_ack', names)
            return replies[1:]
        finally:
            stop.set(); worker.join(2)

    def test_legacy_mcp_executes_exact_control_arguments_from_the_notice(self):
        self.manager._quiet_tick()
        arguments = [json.loads(line)['quiet'] for line in self.sent()[self.rows[1].id].splitlines() if line.startswith('{"quiet":')]
        self.assertEqual([a['action'] for a in arguments], ['status', 'ack', 'ack', 'wait'])
        arguments[-1]['seconds'] = 0
        replies = self.legacy_calls(self.rows[1], arguments)
        self.assertTrue(all(not r['isError'] for r in replies), replies)
        self.assertEqual(self.manager.quiet.snapshot()['active']['participants'][0]['status'], 'waiting')
        self.assertTrue(all(p['status'] == 'finishing' for p in self.manager.quiet.snapshot()['active']['participants'][1:]))

    def test_five_legacy_clients_complete_ack_grant_work_renew_release_and_resume(self):
        for row in self.rows[1:]:
            replies = self.legacy_calls(row, [{'action': 'ack', 'id': self.rid, 'finished': True}])
            self.assertFalse(replies[0]['isError'], replies)
        self.manager.quiet.tick()
        self.manager.quiet.settle = time.monotonic()-1
        self.manager.quiet.tick()
        self.assertEqual(self.manager.quiet.snapshot()['active']['phase'], 'ready')
        replies = self.legacy_calls(self.rows[0], [
            {'action': 'work', 'id': self.rid, 'running': True},
            {'action': 'renew', 'id': self.rid},
            {'action': 'work', 'id': self.rid, 'running': False},
            {'action': 'release', 'id': self.rid, 'finished': True}])
        self.assertTrue(all(not r['isError'] for r in replies), replies)
        for row in self.rows[1:]:
            replies = self.legacy_calls(row, [{'action': 'wait', 'id': self.rid, 'seconds': 0}])
            self.assertIsNone(json.loads(replies[0]['content'][0]['text'])['active'])

    def test_compatibility_controls_keep_role_ownership_and_background_validation(self):
        target = self.rows[1]
        for value in (None, [], {}, {'action': 'unknown'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.tool(target, 'piper_session', {'quiet': value})
        target.background_tasks = {'job': 'running'}
        with self.assertRaises(ValueError):
            self.manager.tool(target, 'piper_session', {'quiet': {'action': 'ack', 'id': self.rid, 'finished': True}})
        target.background_tasks = {}
        with self.assertRaises(ValueError):
            self.manager.tool(target, 'piper_session', {'quiet': {'action': 'release', 'id': self.rid, 'finished': True}})
        target.kind = 'prodcheck'
        with self.assertRaises(ValueError):
            self.manager.tool(target, 'piper_session', {'quiet': {'action': 'request', 'reason': 'not allowed'}})


class QuietRestartInputTest(unittest.TestCase):
    def setUp(self):
        self.manager = _env.manager(self)
        self.row = sessions.Session(id='f0000001', project='demo', pid=123, state='waiting', idle=True)
        self.now = time.time()
        self.enter = self.now - 3600
        self.manager._last_key = mock.Mock(return_value={'key_at': self.enter, 'enter_at': self.enter})

    def test_old_confirmed_submit_restores_only_the_corresponding_enter_boundary(self):
        self.assertIsNone(self.manager._note_submit(self.row, {'ts': self.enter + .2}))
        self.assertEqual(self.row.submit_at, self.enter)
        self.assertFalse(self.manager.input_pending(self.row))

    def test_draft_after_historical_submit_remains_protected(self):
        self.manager._last_key.return_value['key_at'] = self.enter + 10
        self.manager._note_submit(self.row, {'ts': self.enter + .2})
        self.assertTrue(self.manager.input_pending(self.row))

    def test_newer_unconfirmed_enter_is_not_inferred_to_be_a_submit(self):
        self.manager._last_key.return_value = {'key_at': self.enter + 5, 'enter_at': self.enter + 5}
        self.manager._note_submit(self.row, {'ts': self.enter + .2})
        self.assertEqual(self.row.submit_at, 0)
        self.assertTrue(self.manager.input_pending(self.row))

    def test_uncorrelated_historical_hook_does_not_clear_the_prompt(self):
        self.manager._note_submit(self.row, {'ts': self.enter + 60})
        self.assertEqual(self.row.submit_at, 0)
        self.assertTrue(self.manager.input_pending(self.row))

    def test_confirmed_markers_survive_real_metadata_reload(self):
        self.row.submit_at, self.row.clear_at = self.enter, self.enter + 1
        self.row.save()
        loaded = sessions.Session(**common.read_json(os.path.join(self.row.dir, 'meta.json'), {}))
        self.assertEqual(loaded.submit_at, self.enter)
        self.assertEqual(loaded.clear_at, self.enter + 1)
        self.assertFalse(self.manager.input_pending(loaded))
        self.manager._last_key.return_value['key_at'] = self.enter + 2
        self.assertTrue(self.manager.input_pending(loaded))

    def test_explicit_input_clear_is_persisted_for_restart(self):
        self.manager.sessions[self.row.id] = self.row
        self.manager.input_clear(self.row.id)
        loaded = sessions.Session(**common.read_json(os.path.join(self.row.dir, 'meta.json'), {}))
        self.assertGreater(loaded.clear_at, self.enter)

    def test_failed_persistence_does_not_clear_the_draft_in_memory(self):
        self.manager.sessions[self.row.id] = self.row
        self.row.hold = 'draft remains'
        with mock.patch.object(self.row, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.manager.input_clear(self.row.id)
        self.assertEqual((self.row.clear_at, self.row.hold), (0, 'draft remains'))

    def test_invalid_or_future_historical_hooks_do_not_clear_the_prompt(self):
        for timestamp in (None, True, 'old', float('inf'), float('nan'), self.now+3600):
            with self.subTest(timestamp=timestamp):
                self.assertIsNone(self.manager._note_submit(self.row, {'ts': timestamp}))
                self.assertEqual(self.row.submit_at, 0)
                self.assertTrue(self.manager.input_pending(self.row))

    def test_invalid_or_future_persisted_markers_are_ignored(self):
        for value in (True, 'future', -1, float('inf'), float('nan'), self.now + 3600):
            with self.subTest(value=value):
                row = sessions.Session(submit_at=value, clear_at=value)
                self.assertEqual((row.submit_at, row.clear_at), (0, 0))


class QuietCompatibilityHookTest(unittest.TestCase):
    def test_only_valid_compatibility_controls_remain_reachable_during_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'piper').mkdir()
            (root / 'piper/quiet.json').write_text('{"blocked":"Held for testing"}')
            script = str(Path(common.PIPER_DIR, 'sandbox/piper-event.py'))
            def run(name, arguments):
                result = subprocess.run([sys.executable, script, 'QuietCheckpoint', str(root / 'events.jsonl')],
                    input=json.dumps({'tool_name': name, 'tool_input': arguments}), text=True,
                    capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout
            for action in ('status', 'ack', 'wait', 'renew', 'work', 'release', 'cancel', 'request'):
                with self.subTest(action=action):
                    self.assertEqual(run('mcp__legacy__piper_session', {'quiet': {'action': action}}), '')
            for arguments in ({}, {'quiet': None}, {'quiet': {'action': 'exec'}}, {'quiet': []}):
                self.assertIn('deny', run('mcp__legacy__piper_session', arguments))
            self.assertIn('deny', run('mcp__legacy__not_piper_session', {'quiet': {'action': 'status'}}))
            self.assertFalse((root / 'events.jsonl').exists())


@unittest.skipUnless(_env.pty_available(), 'PTY unavailable in this OS sandbox')
class QuietDeliveryPtyTest(unittest.TestCase):
    def test_busy_ptys_stop_real_jobs_ack_via_legacy_mcp_and_resume_only_after_release(self):
        with tempfile.TemporaryDirectory(prefix='quiet-busy-pty-') as folder:
            root = Path(folder)
            agent = root / 'agent.py'
            agent.write_text(r'''
import json, pathlib, subprocess, sys
client, mailbox, logfile, loadfile, owner = sys.argv[1:]
job = None
def record(event, **data):
    with open(logfile, 'a') as output:
        output.write(json.dumps(dict(event=event, **data))+'\n')
def start():
    global job
    job = subprocess.Popen([sys.executable, '-u', '-c',
        "import sys\nf=open(sys.argv[1],'a')\nwhile True:\n sum(i*i for i in range(20000)); f.write('work\\n'); f.flush()", loadfile])
    record('started', pid=job.pid)
def stop():
    if job and job.poll() is None:
        job.terminate(); job.wait(timeout=3)
        record('stopped', pid=job.pid, rc=job.returncode)
def control(arguments):
    request = {'jsonrpc':'2.0','id':1,'method':'tools/call',
               'params':{'name':'piper_session','arguments':{'quiet':arguments}}}
    call = subprocess.run([sys.executable, client, '--mailbox', mailbox],
        input=json.dumps(request)+'\n', capture_output=True, text=True, timeout=8, check=True)
    result = json.loads(call.stdout)['result']
    assert not result.get('isError'), result
    return json.loads(result['content'][0]['text'])
try:
    if owner != 'yes': start()
    record('ready')
    for line in sys.stdin:
        if 'Piper requests a quiet window (' in line:
            stop()
            active = control({'action':'status'})['active']
            control({'action':'ack','id':active['id'],'finished':True})
            record('acknowledged')
        elif 'Piper: the quiet window is released.' in line:
            if control({'action':'status'})['active'] is None and owner != 'yes':
                start(); record('resumed')
finally:
    stop()
''')
            manager = _env.manager(self, pty=True)
            rows, logs, loads = [], [], []
            for i in range(5):
                row = sessions.Session(id='e100000' + str(i), project='demo', title='Demo',
                    state='waiting' if i == 0 else 'working', idle=i == 0,
                    runtime='direct', claude_mode='none')
                client = Path(row.box, 'piper/piper-mcp.py')
                client.parent.mkdir(parents=True, exist_ok=True)
                client.write_text(legacy_client_source())
                log, load = root / (row.id + '.jsonl'), root / (row.id + '.load')
                result = manager.pty.call('spawn', id=row.id, argv=[sys.executable, '-u', str(agent),
                    str(client), str(Path(row.box, row.names['mailbox'])), str(log), str(load), 'yes' if i == 0 else 'no'],
                    env=dict(os.environ), cols=100, rows=30, locked=False, log=str(root / (row.id + '.pty')))
                self.assertTrue(result['ok'], result)
                row.pid = result['pid']
                rows.append(row); logs.append(log); loads.append(load)
            manager.sessions = {s.id: s for s in rows}
            manager.quiet = qw.Coordinator(lambda: rows, lambda: True, path=str(root / 'quiet.json'))
            def records(log):
                return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            def wait_for(predicate):
                deadline = time.monotonic() + 12
                busy = set()
                while time.monotonic() < deadline:
                    for row in rows:
                        manager._mailbox(row, busy)
                    manager.quiet.tick()
                    if predicate(): return
                    time.sleep(.02)
                self.fail('Timed out: ' + repr([records(log) for log in logs]))
            wait_for(lambda: all(any(r['event'] == 'ready' for r in records(log)) for log in logs)
                     and all(load.exists() and load.stat().st_size > 0 for load in loads[1:]))
            with mock.patch.object(manager, '_stack_up', return_value=False):
                rid = manager.quiet.request(rows[0].id, 'Busy PTY integration')['active']['id']
                manager._quiet_tick()
                wait_for(lambda: manager.quiet.snapshot()['active']['phase'] == 'ready')
                sizes = [load.stat().st_size for load in loads[1:]]
                for log in logs[1:]:
                    self.assertEqual([r['event'] for r in records(log)], ['started', 'ready', 'stopped', 'acknowledged'])
                    stopped = next(r for r in records(log) if r['event'] == 'stopped')
                    with self.assertRaises(ProcessLookupError): os.kill(stopped['pid'], 0)
                # The owner can do consecutive work while every other real job stays stopped.
                manager.quiet.work(rows[0].id, rid, True)
                manager.quiet.renew(rows[0].id, rid)
                self.assertEqual(sizes, [load.stat().st_size for load in loads[1:]])
                self.assertTrue(all(not any(r['event'] == 'resumed' for r in records(log)) for log in logs))
                manager.quiet.work(rows[0].id, rid, False)
                manager.quiet.release(rows[0].id, rid, finished=True)
                manager._quiet_tick()
                wait_for(lambda: all(any(r['event'] == 'resumed' for r in records(log)) for log in logs[1:])
                         and all(load.stat().st_size > size for load, size in zip(loads[1:], sizes)))

    def test_five_actual_ptys_receive_control_and_release_without_a_session_at_prompt_field(self):
        with tempfile.TemporaryDirectory(prefix='quiet-pty-') as folder:
            manager = _env.manager(self, pty=True)
            rows, logs = [], []
            for i in range(5):
                sid = 'e000000' + str(i)
                log = Path(folder, sid + '.log')
                result = manager.pty.call('spawn', id=sid,
                    argv=[sys.executable, '-u', '-c',
                          "import sys; print('READY',flush=True); [print('DELIVERED:'+line,flush=True) for line in sys.stdin]"],
                    env=dict(os.environ), cols=100, rows=30, locked=False, log=str(log))
                self.assertTrue(result['ok'], result)
                row = sessions.Session(id=sid, project='demo', title='Demo', pid=result['pid'],
                                       state='waiting', idle=True, runtime='direct', claude_mode='none')
                Path(row.box, 'piper').mkdir(parents=True, exist_ok=True)
                rows.append(row); logs.append(log)
            manager.sessions = {s.id: s for s in rows}
            manager.quiet = qw.Coordinator(lambda: rows, lambda: True, path=os.path.join(folder, 'quiet.json'))
            rid = manager.quiet.request(rows[0].id, 'PTY integration')['active']['id']
            with mock.patch.object(manager, '_stack_up', return_value=False):
                manager._quiet_tick()
                self.assertEqual(len(manager._quiet_notice), 5)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not all('DELIVERED:' in p.read_text(errors='replace') for p in logs):
                    time.sleep(.02)
                for log in logs:
                    data = log.read_text(errors='replace')
                    self.assertIn('DELIVERED:', data)
                    self.assertIn(rid, data)
                manager.quiet.cancel(rows[0].id, rid)
                manager._quiet_tick()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not all('window is released' in p.read_text(errors='replace') for p in logs):
                    time.sleep(.02)
                self.assertTrue(all('window is released' in p.read_text(errors='replace') for p in logs))
                self.assertEqual(manager._quiet_notice, {})


if __name__ == '__main__':
    unittest.main()
