"""Security regression matrix for blind HTTP env writes and shared safe file reads."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import httpclient as hc
import sessions


class BlindEnvTest(unittest.TestCase):
    def setUp(self):
        self.m = _env.manager(self)
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rel = 'http-client/http-client.env.json'
        self.file = self.root / self.rel
        self.file.parent.mkdir()
        self.original = b'{"dev":{"password":"old-secret","keep":"other-secret"},"prod":{"token":"prod-secret"}}'
        self.file.write_bytes(self.original)
        self.file.chmod(0o640)
        self.s = sessions.Session(kind='project', project='test/env', path=str(self.root), state='exited')
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(self.m.sessions.pop, self.s.id, None)
        patch = mock.patch.object(self.m, '_session_top', return_value=str(self.root))
        patch.start(); self.addCleanup(patch.stop)
        self.m.ask = mock.Mock(return_value=True)

    def call(self, **kw):
        return self.m.tool(self.s, 'http_env_set', {'file': self.rel, 'env': 'dev', 'values': {'password': 'new'}, **kw})

    def test_success_preserves_hidden_inode_mode_other_values_no_disclosure_or_value_audit(self):
        before = self.file.stat()
        with mock.patch.object(hc, 'audit') as audit:
            self.assertEqual(self.call(), {'ok': True})
        self.assertEqual(self.file.stat().st_ino, before.st_ino)
        self.assertEqual(self.file.stat().st_mode, before.st_mode)
        doc = json.loads(self.file.read_bytes())
        self.assertEqual(doc['dev'], {'password': 'new', 'keep': 'other-secret'})
        self.assertEqual(doc['prod'], {'token': 'prod-secret'})
        for secret in ('old-secret', 'other-secret', 'prod-secret'):
            self.assertNotIn(secret, str(self.m.ask.call_args))
            self.assertNotIn(secret, str(audit.call_args_list))
        self.assertNotIn('password', str(audit.call_args_list))

    def test_deny_or_timeout_keeps_original_bytes(self):
        self.m.ask.return_value = False
        with self.assertRaisesRegex(hc.HttpError, 'not approved'):
            self.call()
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_multiple_directories_public_private_and_literal_dotted_key(self):
        for rel in ('a/http-client.env.json', 'b/http-client.env.json', 'a/http-client.private.env.json'):
            p = self.root / rel; p.parent.mkdir(exist_ok=True); p.write_text('{"dev":{"keep":"unchanged"}}')
        self.call(file='a/http-client.private.env.json', values={'a.b': {'nested': False}, 'count': 0})
        self.assertEqual(json.loads((self.root/'a/http-client.private.env.json').read_text()),
                         {'dev': {'keep': 'unchanged', 'a.b': {'nested': False}, 'count': 0}})
        for rel in ('a/http-client.env.json', 'b/http-client.env.json'):
            self.assertEqual(json.loads((self.root/rel).read_text()), {'dev': {'keep': 'unchanged'}})
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_new_environment_and_missing_file(self):
        self.call(env='new', values={'token': 'new-token'})
        self.assertEqual(json.loads(self.file.read_bytes())['new'], {'token': 'new-token'})
        self.call(file='http-client/http-client.private.env.json', values={'token': 'new-token'})
        p = self.file.parent/'http-client.private.env.json'
        self.assertEqual(json.loads(p.read_bytes()), {'dev': {'token': 'new-token'}})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_bad_paths_never_show_a_card(self):
        for rel in ('/http-client.env.json', '../http-client.env.json', 'a/../http-client.env.json',
                    '.git/http-client.env.json', '.GIT/http-client.env.json', 'a//http-client.env.json',
                    'a\\http-client.env.json', 'not-env.json', 'x\0/http-client.env.json', None):
            with self.subTest(rel=rel), self.assertRaises(hc.HttpError):
                self.call(file=rel)
        self.m.ask.assert_not_called()

    def test_symlink_directory_leaf_hardlink_and_fifo_refused(self):
        outside = self.root / 'outside.json'; outside.write_bytes(self.original)
        (self.root/'alias').symlink_to(self.file.parent, target_is_directory=True)
        symlinkdir = self.root/'symlink'; symlinkdir.mkdir()
        (symlinkdir/'http-client.env.json').symlink_to(outside)
        harddir = self.root/'hard'; harddir.mkdir()
        os.link(outside, harddir/'http-client.env.json')
        fifodir = self.root/'fifo'; fifodir.mkdir()
        os.mkfifo(fifodir/'http-client.env.json')
        for rel in ('alias/http-client.env.json', 'symlink/http-client.env.json', 'hard/http-client.env.json', 'fifo/http-client.env.json'):
            with self.subTest(rel=rel), self.assertRaises(hc.HttpError):
                self.call(file=rel)
        self.m.ask.assert_not_called()
        self.assertEqual(outside.read_bytes(), self.original)

    def test_malformed_documents_and_encoding_errors_do_not_quote_values(self):
        for data in (b'{"dev":{"token":"old-secret",}}', b'{"dev":[]}', b'{"dev":{"token":NaN}}',
                     b'{"dev":{"token":"old-secret","token":"hidden"}}', b'{"dev":{},"dev":{}}',
                     b'\xffold-secret', b'["old-secret"]'):
            self.file.write_bytes(data)
            with self.subTest(data=data), self.assertRaises(hc.HttpError) as error:
                self.call()
            self.assertNotIn('old-secret', str(error.exception))
            self.assertEqual(self.file.read_bytes(), data)
        self.m.ask.assert_not_called()

    def test_invalid_values_or_oversized_file_never_show_a_card(self):
        for kw in ({'values': {}}, {'values': []}, {'values': None}, {'env': ''}, {'env': []},
                   {'values': {'': 'x'}}, {'values': {'x': 'y'*16001}}, {'values': {'x': float('nan')}}):
            with self.subTest(kw=list(kw)), self.assertRaises(ValueError): self.call(**kw)
        with mock.patch.object(hc, 'MAX_FILE', 10), self.assertRaises(hc.HttpError): self.call()
        self.m.ask.assert_not_called()
        self.assertEqual(self.file.read_bytes(), self.original)

    def test_concurrent_edit_deletion_and_symlink_swap_refused(self):
        outside = self.root/'outside.json'; outside.write_bytes(self.original)
        for op in ('edit', 'delete', 'symlink'):
            self.file.unlink(missing_ok=True); self.file.write_bytes(self.original)
            def approve(*a, **kw):
                if op == 'edit': self.file.write_text('{"dev":{"race":"preserve"}}')
                else:
                    self.file.unlink()
                    if op == 'symlink': self.file.symlink_to(outside)
                return True
            self.m.ask.side_effect = approve
            with self.subTest(op=op), self.assertRaises(hc.HttpError): self.call()
            self.assertEqual(outside.read_bytes(), self.original)
            if op == 'edit': self.assertIn('race', self.file.read_text())
            if op == 'delete': self.assertFalse(self.file.exists())

    def test_proposal_is_frozen_before_the_card(self):
        values = {'password': {'nested': 'approved'}}
        def approve(*a, **kw):
            values['password']['nested'] = 'not-approved'
            return True
        self.m.ask.side_effect = approve
        self.call(values=values)
        self.assertEqual(json.loads(self.file.read_bytes())['dev']['password'], {'nested': 'approved'})

    def test_hidden_guard_and_pin_refusals_keep_file_and_hide_error_details(self):
        for method in ('_hidden_write_guard', '_ops_pin'):
            with mock.patch.object(self.m, method, side_effect=ValueError('private diagnostic')):
                with self.assertRaises(hc.HttpError) as e: self.call()
                self.assertNotIn('private diagnostic', str(e.exception))
                self.assertEqual(self.file.read_bytes(), self.original)

    def test_live_uncovered_session_refuses_new_hidden_file(self):
        self.s.state = 'preflight'
        self.s.excludes = ['http-client/http-client.private.env.json']
        with self.assertRaises(hc.HttpError): self.call(file='http-client/http-client.private.env.json')
        self.assertFalse((self.file.parent/'http-client.private.env.json').exists())

    def test_each_real_write_requires_a_new_card_even_with_stored_grant_and_security_off(self):
        self.m.ask = sessions.Manager.ask.__get__(self.m)
        self.m.policy['ask'] = []
        self.m.policy['auto_approve'] = ['http_env_set']
        self.s.approvals.append(self.m.approval_scope('http_env_set', {'file': self.rel, 'env': 'dev'}))
        for approved in (True, False, True):
            out, errors = [], []
            def worker():
                try: out.append(self.call())
                except hc.HttpError as e: errors.append(str(e))
            t = threading.Thread(target=worker); t.start()
            try:
                deadline = time.monotonic()+5
                while not self.m.pending_list() and time.monotonic()<deadline: time.sleep(.01)
                cards = self.m.pending_list(); self.assertEqual(len(cards), 1)
                card = cards[0]
                self.assertNotIn('always', card); self.assertNotIn('session_always', card)
                self.assertNotIn('old-secret', json.dumps(card))
                self.m.decide(card['id'], approved, always=True, session_always=True)
            finally:
                for card in self.m.pending_list():
                    try: self.m.decide(card['id'], False)
                    except KeyError: pass
                t.join(5)
            self.assertFalse(t.is_alive())
            self.assertEqual(out, [{'ok': True}] if approved else [])
            self.assertEqual(bool(errors), not approved)


class SafeReadTest(unittest.TestCase):
    def test_fifo_never_blocks_a_blind_file_tool(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkfifo(os.path.join(root, 'fifo'))
            with self.assertRaises(common.UnsafePath): common.safe_read(root, 'fifo')

    def test_single_link_is_checked_on_opened_fd_not_just_the_path(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'a').write_bytes(b'fixture')
            os.link(Path(root, 'a'), Path(root, 'b'))
            self.assertEqual(common.safe_read(root, 'a'), b'fixture')
            with self.assertRaises(common.UnsafePath): common.safe_read(root, 'a', single_link=True)


if __name__ == '__main__': unittest.main()
