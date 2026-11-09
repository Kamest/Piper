"""Git approvals explain the active guard and tolerate ordinary atomic config writes."""
import json
import os
import threading
import time
from unittest import mock

import _env
import guard
import security
from test_gitrun_exposed import GitRunExposedBase, HIDDEN


class GitApprovalReasonsTest(GitRunExposedBase):
    def test_atomic_config_replacement_does_not_card_unrelated_reads(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped), mock.patch.dict(self.m.policy, ask=[], security_scoped=scoped):
                s, extra, repo = self.situation('k1', 'config%d' % scoped)
                s.excludes.append('.git/config')
                s.coverage = [guard.coverage(repo, ['.git/config'], {'candidates': []})]
                config = os.path.join(repo, '.git/config')
                with open(config) as source, open(config + '.tmp', 'w') as target:
                    target.write(source.read())
                os.replace(config + '.tmp', config)
                self.assertEqual(self.m._broken_overmounts(s), ([], [config]))
                cards, reply = self.run_git((s, extra, repo), ['show', 'HEAD~1', '--', 'src/a.txt'])
                self.assertEqual((cards, reply.get('ok')), ([], True), reply)
                self.assertEqual(reply['result']['rc'], 0)
                self.assertIn('src/a.txt', reply['result']['output'])

    def test_replaced_hidden_content_still_requires_its_own_approval(self):
        with mock.patch.dict(self.m.policy, ask=[], danger_cards=True):
            s, extra, repo = self.situation('k1', 'hidden-replaced')
            rel = HIDDEN + '/db.properties'
            s.coverage = [guard.coverage(repo, [rel], {'candidates': []})]
            path = os.path.join(repo, rel)
            with open(path + '.tmp', 'w') as target:
                target.write('pw=PLANTED_REPLACEMENT\n')
            os.replace(path + '.tmp', path)
            cards, reply = self.run_git((s, extra, repo), ['add', '-f', rel])
            self.assertTrue(cards, reply)
            self.assertFalse(reply.get('ok'))

    def test_moved_parent_remains_mandatory_with_optional_guards_off(self):
        plain = security.apply(dict(self.m.policy, security={}, security_rev=security.REV, security_profile='custom'))
        with mock.patch.dict(self.m.policy, plain):
            self.assertFalse(self.m.policy['danger_cards'])
            self.assertTrue(self.m.auto_ok('git_run'))
            s, _extra, repo = self.situation('k1', 'moved-reason')
            s.coverage = [guard.coverage(repo, [HIDDEN], {'candidates': []})]
            os.rename(os.path.join(repo, 'config'), os.path.join(repo, 'cfgx'))
            payloads = []
            with mock.patch.object(self.m, 'ask', side_effect=lambda kind, sess, summary, payload, **kw:
                                   payloads.append((summary, payload)) or False):
                reply = self._tool(s, {'args': ['show', 'HEAD', '--', 'src/a.txt']})
            summary, payload = payloads[0]
            self.assertTrue(payload['_force_approval'], reply)
            self.assertIsNone(payload['_approval_reason']['rule'])
            self.assertIn('sandbox', payload['_approval_reason']['label'].lower())
            self.assertIn('off', payload['_approval_reason']['detail'].lower())
            self.assertIn('config/secrets', summary)
            self.assertNotIn('stand-in', summary)

    def test_hidden_file_card_names_its_guard_instead_of_disabled_git_switch(self):
        s = self._live()
        with mock.patch.dict(self.m.policy, ask=[], danger_cards=True):
            item = self.pending_card(s, {'danger': True})
        self.assertEqual(item['why']['rule'], 'hidden_files')
        self.assertIn('Hidden files', item['why']['label'])
        self.assertIn('off', item['why']['detail'].lower())

    def test_mandatory_reason_survives_pending_transport_without_a_fake_rule(self):
        reason = {'group': '', 'label': 'Sandbox integrity check', 'rule': None,
                  'detail': 'Required even when optional Security checks are off.'}
        with mock.patch.dict(self.m.policy, ask=[], danger_cards=False):
            item = self.pending_card(self._live(), {'_force_approval': True, '_approval_reason': reason})
        self.assertEqual(item['why'], reason)
        self.assertNotIn('_approval_reason', item['payload'])
        self.assertNotIn('Full git', json.dumps(item['why']))

    def test_routine_git_card_keeps_its_own_rule(self):
        for scoped in (False, True):
            with self.subTest(scoped=scoped), mock.patch.dict(self.m.policy, ask=['git_run'],
                                                             danger_cards=True, security_scoped=scoped):
                item = self.pending_card(self._live(), {'_force_approval': scoped})
                self.assertEqual(item['why']['label'], 'Full git on the host (git_run)')
                self.assertNotIn('detail', item['why'])

    def test_host_safety_check_does_not_blame_disabled_routine_approval(self):
        with mock.patch.dict(self.m.policy, ask=[], danger_cards=False, security_scoped=True):
            s, _extra, _repo = self.situation('k1', 'host-reason')
            payloads = []
            with mock.patch.object(self.m, 'ask', side_effect=lambda kind, sess, summary, payload, **kw:
                                   payloads.append(payload) or False):
                reply = self._tool(s, {'args': ['diff', '--no-index', '../outside.txt', 'README.md']})
            self.assertTrue(payloads, reply)
            self.assertTrue(payloads[0]['_force_approval'])
            reason = payloads[0]['_approval_reason']
            self.assertIsNone(reason['rule'])
            self.assertIn('host Git safety check', reason['label'])
            self.assertIn('whether or not Git approvals are on', reason['detail'])

    def pending_card(self, session, payload):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.m.ask(
            'git_run', session, 'git show HEAD -- src/a.txt', dict(payload, evidence={}), timeout=5)), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                pending = [p for p in self.m.pending_list() if p['session'] == session.id]
                if pending:
                    return pending[0]
                if not thread.is_alive():
                    break
                time.sleep(.01)
            self.fail('expected an approval card')
        finally:
            for item in self.m.pending_list():
                if item['session'] == session.id:
                    self.m.decide(item['id'], False)
            thread.join(2)
