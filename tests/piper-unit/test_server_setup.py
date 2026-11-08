"""Server setup contracts and write-ahead recovery. Synthetic peers only; no network/services."""
import base64
import copy
import hashlib
import io
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tarfile
import shutil
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import extensions
import extfind
import mobile
import projects
import remotes
import remotepeer
import server_setup
import server_config

PUBLIC = 'ssh-ed25519 ' + 'A' * 68 + ' piper-server'
HOST_KEY = 'example.com ssh-ed25519 ' + 'B' * 68


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as tar:
        for name, data in files.items():
            raw = data if isinstance(data, bytes) else data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    raw = stream.getvalue()
    return dict(archive=base64.b64encode(raw).decode(), sha=hashlib.sha256(raw).hexdigest())


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.entry = dict(id='dev', mode='service', project_path='/home/server/projects', host='dev.example.com')
        patch = mock.patch.object(remotes, 'get', side_effect=lambda eid: dict(self.entry) if eid == 'dev' else self._missing())
        patch.start(); self.addCleanup(patch.stop)
        self.setup = server_setup.Setup(home=self.tmp.name)
        self.addCleanup(self.setup.close)
        self.status = dict(service_running=True, project_root_matches=True, bundle_sha='bundle',
                           docker={'present': True, 'usable': True}, hole={'version': '2.4.0', 'image': True, 'current': True},
                           git_key={'public_key': PUBLIC}, extensions=[], extensions_sha='extensions',
                           agent_login={'claude': 'ok', 'codex': 'unknown'}, project_origins={},
                           home='/home/server', state='/home/server/.piper-server', requested_project_root='/home/server/projects',
                           platform='linux', python=[3, 12, 0], projects=[])
        patch = mock.patch.object(server_config, 'snapshot', return_value=({}, [], []))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(remotes, 'bundle', return_value=(b'app', 'bundle'))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(remotes, 'extension_bundle', return_value=(b'ext', 'extensions', []))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(extensions, 'server_requirements', return_value=[])
        patch.start(); self.addCleanup(patch.stop)
        self.calls = []
        self.mobile_status = dict(venv_ok=True, unit_active=True, relay_configured=True, unit_enabled=True, ready=True)
        patch = mock.patch.object(mobile, 'load_state', return_value={'relay': 'https://relay.example.com'})
        patch.start(); self.addCleanup(patch.stop)
        def peer(eid, op, body=None):
            self.calls.append((op, copy.deepcopy(body)))
            if op == 'setup_status':
                return copy.deepcopy(self.status)
            if op == 'configuration_manifest':
                return dict(files={}, previous={}, left_out=[], revision='manifest')
            if op == 'secrets_status':
                return {'secrets': []}
            if op == 'mobile_status':
                return dict(self.mobile_status)
            return {}
        self.peer = mock.patch.object(remotes, 'setup_request', side_effect=peer).start()
        self.addCleanup(mock.patch.stopall)

    @staticmethod
    def _missing():
        raise ValueError('unknown environment')

    def wait(self, setup=None):
        setup = setup or self.setup
        th = setup.threads.get('dev')
        if th:
            th.join(3)
            self.assertFalse(th.is_alive())
        return setup.state('dev')

    def row(self, state, key):
        return next(s for s in state['steps'] if s['id'] == key)

    def test_ten_ordered_steps_deferred_without_actions_and_get_is_local(self):
        state = self.setup.state('dev')
        self.assertEqual([s['number'] for s in state['steps']], list(range(1, 11)))
        self.assertEqual([s['id'] for s in state['steps']], [s[0] for s in server_setup.STEPS])
        self.peer.assert_not_called()
        self.setup.start('dev', {'projects': []})
        state = self.wait()
        self.assertEqual(self.row(state, 'service')['state'], 'ok')
        for key in server_setup.DEFERRED:
            self.assertEqual(self.row(state, key)['state'], 'todo')
            self.assertNotIn('action', self.row(state, key))
        self.assertEqual(self.row(state, 'git')['action']['public_key'], PUBLIC)
        self.assertEqual(state['status'], 'halted')  # key registration still needs the user
        self.assertNotIn('operations', state)
        self.assertNotIn('inflight', state)
        self.assertEqual(self.setup._path('dev').stat().st_mode & 0o777, 0o600)

    def test_macos_hole_is_informational_and_does_not_block_remaining_steps(self):
        self.status.update(platform='darwin', docker={'present': False, 'usable': False}, hole={})
        self.setup.start('dev', {'projects': []})
        state = self.wait()
        row = self.row(state, 'hole')
        self.assertEqual(row['state'], 'needs_user')
        self.assertIn('sessions run directly', row['detail'])
        self.assertIsNone(row.get('action'))
        self.assertEqual(self.row(state, 'extensions')['state'], 'ok')
        self.assertEqual(self.row(state, 'tools')['state'], 'ok')
        self.assertNotIn('install_hole', [op for op, _ in self.calls])

    def test_failed_step_names_the_transport_reason_and_logs_it(self):
        # Regression: a live run ended with a generic "Setup step failed" and no trace in piperd.log.
        side = self.peer.side_effect
        def peer(eid, op, body=None):
            if op == 'setup_status' and any(c[0] == 'setup_status' for c in self.calls):
                self.calls.append((op, body))
                raise remotes.Unavailable('SSH request failed. token=ghp_' + 'a' * 36)
            return side(eid, op, body)
        self.peer.side_effect = peer
        with mock.patch('builtins.print') as out:
            self.setup.start('dev', {'projects': []})
            state = self.wait()
        row = self.row(state, 'hole')
        self.assertEqual(row['state'], 'failed')
        self.assertTrue(row['detail'].startswith('SSH request failed.'))
        self.assertNotIn('ghp_' + 'a' * 36, row['detail'])
        lines = [str(c.args[0]) for c in out.call_args_list if c.args]
        logged = [l for l in lines if l.startswith('piperd: server setup dev step hole: Unavailable')]
        self.assertTrue(logged, lines)
        self.assertNotIn('ghp_' + 'a' * 36, logged[0])

    def test_setup_log_records_what_ran_and_the_refusal_reason(self):
        # Regression: a new server stopped at step 1 and the GUI showed no trace of what ran or why it failed.
        self.status['bundle_sha'] = 'old'
        refused = remotes.Rejected('Python 3.10 or newer is required on the server (found 3.9).')
        with mock.patch.object(remotes, 'request', side_effect=refused) as install:
            self.setup.start('dev', {}, 'service')
            state = self.wait()
        install.assert_called_once()
        log = [(e['step'], e['state'], e['text']) for e in state['log']]
        self.assertIn(('service', 'running', 'Checking server…'), log)
        self.assertEqual(log[-1][:2], ('service', 'needs_user'))
        self.assertIn('Python 3.10', log[-1][2])
        self.assertTrue(all(isinstance(e['at'], float) for e in state['log']))
        self.assertNotEqual(state['phase'], 'remote_unknown')
        for _ in range(server_setup.LOG_LIMIT + 5):
            server_setup._note(state, 'hole', 'running', 'x%d' % _)
        self.assertEqual(len(state['log']), server_setup.LOG_LIMIT)

    def test_failed_step_shows_the_setup_reason_instead_of_a_generic_line(self):
        # Regression: "Configuration snapshot exceeds the setup size limit" reached only piperd.log; the row
        # said "Setup step failed" and the user could not tell what to do.
        with mock.patch.object(server_config, 'snapshot', side_effect=ValueError('Configuration snapshot exceeds the setup size limit.')), \
                mock.patch('builtins.print'):
            self.setup.start('dev', {}, 'configuration')
            row = self.row(self.wait(), 'configuration')
        self.assertEqual((row['state'], row['detail']), ('failed', 'Configuration snapshot exceeds the setup size limit.'))

    def test_repeated_pass_checks_but_does_not_repeat_satisfied_mutations(self):
        for _ in range(2):
            self.setup.start('dev', {'projects': []})
            self.wait()
        self.assertEqual({op for op, _ in self.calls}, {'setup_status', 'configuration_manifest', 'secrets_status', 'mobile_status'})

    def python_peer(self, lose=False):
        self.status.update(python=[3, 9, 6], service_running=False, bundle_sha=None)
        original = self.peer.side_effect
        def peer(eid, op, body=None):
            reply = original(eid, op, body)
            if op == 'install_python':
                saved = json.loads(self.setup._path(eid).read_text())
                self.assertEqual(saved['inflight'], {'step': 'service', 'key': 'service:python'})
                self.assertEqual(saved['phase'], 'remote_sending')
                self.status['python'] = [3, 12, 16]
                if lose:
                    raise remotes.Unavailable('lost answer')
                return {'installed': True}
            return reply
        self.peer.side_effect = peer
        def install(entry, body, **kw):
            self.calls.append(('install', None))
            self.assertEqual(self.status['python'], [3, 12, 16])
            self.assertEqual(body['op'], 'install')
            self.status.update(service_running=True, bundle_sha='bundle')
            return {'installed': True}
        return install

    def test_step_one_installs_python_once_then_service_and_logs_progress(self):
        install = self.python_peer()
        with mock.patch.object(remotes, 'request', side_effect=install):
            for _ in range(2):
                self.setup.start('dev', {}, 'service')
                state = self.wait()
                self.assertEqual(self.row(state, 'service')['state'], 'ok')
        ops = [op for op, _ in self.calls]
        self.assertEqual([op for op in ops if op in ('install_python', 'install')], ['install_python', 'install'])
        logs = [entry['text'] for entry in state['log']]
        self.assertIn('Installing Python 3.12 for Piper…', logs)
        self.assertIn('Python 3.12 is ready. Installing Piper service…', logs)
        self.assertIn('Python 3.12 is ready. Service is current.', logs)

    def test_python_lost_answer_requires_reconciliation_and_never_replays(self):
        install = self.python_peer(lose=True)
        with mock.patch.object(remotes, 'request', side_effect=install) as request, mock.patch('builtins.print'):
            self.setup.start('dev', {}, 'service')
            state = self.wait()
            self.assertEqual(state['phase'], 'remote_unknown')
            self.assertEqual(state['unknown']['operation'], 'service:python')
            request.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'Reconcile'):
                self.setup.start('dev', {}, 'service')
            restarted = server_setup.Setup(home=self.tmp.name)
            self.addCleanup(restarted.close)
            restarted.resume()
            self.assertEqual(restarted.state('dev')['phase'], 'remote_unknown')
            self.setup.start('dev', {'reconcile': True}, 'service')
            state = self.wait()
            self.assertTrue(state['unknown']['checked'])
            request.assert_not_called()  # reconciliation is read-only, including service installation
            self.setup.start('dev', {'retry_unknown': True}, 'service')
            self.assertEqual(self.row(self.wait(), 'service')['state'], 'ok')
        self.assertEqual([op for op, _ in self.calls].count('install_python'), 1)

    def test_python_refusal_is_definite_and_blocks_service_installation(self):
        self.status['python'] = [3, 9, 6]
        original = self.peer.side_effect
        def peer(eid, op, body=None):
            if op == 'install_python':
                raise remotes.Rejected('Cannot download Python 3.12 from the server. Enable outbound HTTPS, then Fix.')
            return original(eid, op, body)
        self.peer.side_effect = peer
        with mock.patch.object(remotes, 'request') as install:
            self.setup.start('dev', {}, 'service')
            state = self.wait()
        install.assert_not_called()
        self.assertNotEqual(state['phase'], 'remote_unknown')
        self.assertIn('Cannot download Python', self.row(state, 'service')['detail'])

    def test_duplicate_post_does_not_create_worker_or_change_selection(self):
        with mock.patch.object(self.setup, '_spawn') as spawn:
            self.setup.threads['dev'] = mock.Mock(is_alive=lambda: True)
            self.setup.start('dev', {'projects': []})
            spawn.assert_not_called()

    def test_fix_runs_only_selected_step_and_invalid_requests_never_dispatch(self):
        for key in ('missing',):
            with self.assertRaises(ValueError):
                self.setup.start('dev', {}, key)
        for body in ({'token': 'fixture'}, {'reconcile': 'yes'}, {'retry_unknown': True}, {'projects': 'shop'}):
            with self.assertRaises(ValueError):
                self.setup.start('dev', body)
        self.peer.assert_not_called()
        self.setup.start('dev', {}, 'agents')
        state = self.wait()
        self.assertEqual(self.row(state, 'agents')['state'], 'ok')
        self.assertEqual(self.row(state, 'service')['state'], 'todo')
        self.assertEqual(len(self.calls), 1)

    def test_lost_answer_is_not_replayed_by_post_fix_or_restart(self):
        self.status['git_key'] = {}
        def peer(eid, op, body=None):
            self.calls.append((op, body))
            if op == 'setup_status':
                return copy.deepcopy(self.status)
            self.assertEqual(self.setup._load(eid)['inflight']['step'], 'git')
            saved = json.loads(self.setup._path(eid).read_text())
            self.assertEqual(saved['phase'], 'remote_sending')
            raise remotes.Unavailable('lost answer')
        self.peer.side_effect = peer
        self.setup.start('dev', {}, 'git')
        state = self.wait()
        self.assertEqual((state['phase'], self.row(state, 'git')['state']), ('remote_unknown', 'needs_user'))
        for step in (None, 'git'):
            with self.assertRaises(ValueError):
                self.setup.start('dev', {}, step)
        restarted = server_setup.Setup(home=self.tmp.name)
        restarted.resume()
        self.assertEqual(restarted.state('dev')['phase'], 'remote_unknown')
        self.assertEqual([op for op, _ in self.calls].count('setup_git_key'), 1)
        self.assertNotIn('lost answer', self.setup._path('dev').read_text())
        # Read-only reconciliation discovers the key created before the reply was lost.
        self.status['git_key'] = {'public_key': PUBLIC}
        self.setup.start('dev', {'reconcile': True}, 'git')
        self.assertEqual(self.wait()['phase'], 'reconciled')
        self.assertEqual([op for op, _ in self.calls].count('setup_git_key'), 1)

    def test_unconfirmed_outcome_requires_check_then_explicit_retry(self):
        record = self.setup._load('dev')
        record['inflight'] = {'step': 'git', 'key': 'git'}
        record['status'] = 'running'
        self.setup._save(record)
        restarted = server_setup.Setup(home=self.tmp.name)
        restarted.resume()
        self.assertEqual(restarted.state('dev')['phase'], 'remote_unknown')
        with self.assertRaises(ValueError):
            restarted.start('dev', {'retry_unknown': True}, 'git')
        self.status['git_key'] = {}
        restarted.start('dev', {'reconcile': True}, 'git')
        state = self.wait(restarted)
        self.assertTrue(state['unknown']['checked'])
        self.assertEqual(self.row(state, 'git')['action']['body'], {'retry_unknown': True})
        self.assertEqual({op for op, _ in self.calls}, {'setup_status'})
        self.peer.side_effect = lambda eid, op, body=None: self.status if op == 'setup_status' else {'public_key': PUBLIC}
        restarted.start('dev', {'retry_unknown': True}, 'git')
        self.assertNotIn('unknown', self.wait(restarted))

    def test_restart_resumes_completed_phase_without_reissuing_write(self):
        record = self.setup._load('dev')
        record.update(status='running', selection=['git'], targets=[], operations={'git': {'public_key': PUBLIC}})
        self.status['git_key'] = {}
        self.setup._save(record)
        restarted = server_setup.Setup(home=self.tmp.name)
        restarted.resume()
        self.assertEqual(self.row(self.wait(restarted), 'git')['public_key'], PUBLIC)
        self.assertEqual({op for op, _ in self.calls}, {'setup_status'})

    def test_authenticated_refusal_clears_intent_and_allows_explicit_fix(self):
        record = self.setup._load('dev')
        with self.assertRaises(remotes.Rejected):
            self.setup._write(record, 'hole', 'hole', mock.Mock(side_effect=remotes.Rejected('not executed')))
        self.assertIsNone(record['inflight'])
        self.assertNotIn('unknown', record)

    def test_default_project_selection_mirrors_relative_layout_and_never_accepts_raw_urls(self):
        root = Path(self.tmp.name) / 'projects'
        path = root / 'acme/shop'
        path.mkdir(parents=True)
        self.setup.manager = mock.Mock(list=lambda: [dict(project='acme/shop@work', state='ready'),
                                                     dict(project='acme/old', state='exited')])
        project = dict(path=str(path), exists=True)
        with mock.patch.dict(os.environ, PROJECT_PATH=str(root)), mock.patch.object(projects, 'find', return_value=project), \
                mock.patch.object(projects, 'origin_url', return_value='https://example.com/acme/shop.git'):
            selected = self.setup._selected_projects(None)
        self.assertEqual(selected[0]['relative_path'], 'acme/shop')
        self.assertEqual(selected[0]['url'], 'git@example.com:acme/shop.git')
        for url in ('https://name:fixture@example.com/acme/shop', 'https://example.com/acme/shop?token=fixture', '/tmp/repo'):
            with mock.patch.dict(os.environ, PROJECT_PATH=str(root)), mock.patch.object(projects, 'find', return_value=project), \
                    mock.patch.object(projects, 'origin_url', return_value=url), self.assertRaises(ValueError):
                self.setup._selected_projects(['acme/shop'])

    def test_projects_outside_the_local_root_mirror_under_their_key(self):
        # Regression: a live session in a project kept outside PROJECT_PATH (another root such as
        # ~/development) failed Git and Projects for every project.
        root = Path(self.tmp.name) / 'projects'
        (root / 'acme/shop').mkdir(parents=True)
        outside = Path(self.tmp.name) / 'development/tool'
        outside.mkdir(parents=True)
        broken = Path(self.tmp.name) / 'development/local-only'
        broken.mkdir(parents=True)
        paths = {'acme/shop': root / 'acme/shop', 'development/tool': outside, 'development/local-only': broken}
        self.setup.manager = mock.Mock(list=lambda: [dict(project=k, state='ready') for k in paths])
        find = lambda key: dict(path=str(paths[key]), exists=True)
        origin = lambda path: '' if path.endswith('local-only') else 'git@example.com:acme/x.git'
        skipped = []
        with mock.patch.dict(os.environ, PROJECT_PATH=str(root)), mock.patch.object(projects, 'find', side_effect=find), \
                mock.patch.object(projects, 'origin_url', side_effect=origin):
            selected = self.setup._selected_projects(None, skipped)
            self.assertEqual({p['key']: p['relative_path'] for p in selected},
                             {'acme/shop': 'acme/shop', 'development/tool': 'development/tool'})
            self.assertEqual(skipped, ['development/local-only'])
            self.assertEqual(len(self.setup._selected_projects(['development/tool'])), 1)
            with self.assertRaises(ValueError):  # an explicit choice is never trimmed silently
                self.setup._selected_projects(['acme/shop', 'development/local-only'])

    def test_tools_check_install_check_and_metadata_only_persistence(self):
        rows = [dict(id='tool', title='Tool', detail='Required compiler', check=['compiler', '--version'],
                     install=['python3', 'install.py'], secrets=[dict(file='.config/tool.env', key='ACCESS_TOKEN', label='Access token')])]
        extensions.server_requirements.return_value = rows
        replies = iter([{'returncode': 1}, {'returncode': 0, 'output': 'withheld'}, {'returncode': 0}])
        self.peer.side_effect = lambda eid, op, body=None: copy.deepcopy(self.status) if op == 'setup_status' else next(replies)
        self.setup.start('dev', {}, 'tools')
        state = self.wait()
        self.assertEqual(self.row(state, 'tools')['state'], 'ok')
        self.assertEqual(state['secrets'][0]['key'], 'ACCESS_TOKEN')
        self.assertEqual(extensions.server_requirements.call_args.args[0]['home'], '/home/server')
        saved = self.setup._path('dev').read_text()
        for text in ('install.py', '--version', 'withheld', 'argv'):
            self.assertNotIn(text, saved)

    def test_requirements_without_installer_and_unknown_agent_status_need_user(self):
        extensions.server_requirements.return_value = [dict(id='manual', title='Tool', detail='Install manually',
                                                           check=['missing'], install=None, secrets=[])]
        self.peer.side_effect = lambda eid, op, body=None: self.status if op == 'setup_status' else {'returncode': 127}
        self.setup.start('dev', {}, 'tools')
        self.assertEqual(self.row(self.wait(), 'tools')['state'], 'needs_user')
        self.status['agent_login'] = dict(claude='unknown', codex='unknown')
        self.setup.start('dev', {}, 'agents')
        self.assertEqual(self.row(self.wait(), 'agents')['action']['commands']['codex'], ['codex', 'login', '--device-auth'])

    def test_project_reconciliation_checks_origin_not_only_directory(self):
        record = self.setup._load('dev')
        record['targets'] = [dict(key='acme/shop', local_path='/local/acme/shop', relative_path='acme/shop',
                                  url='git@example.com:acme/shop.git')]
        target = self.entry['project_path'] + '/acme/shop'
        self.status['projects'] = [{'path': target}]
        self.assertFalse(self.setup._step(record, 'projects', check_only=True))
        self.status['project_origins'][target] = hashlib.sha256(b'git@example.com:acme/shop.git').hexdigest()
        self.assertTrue(self.setup._step(record, 'projects', check_only=True))
        self.assertEqual(record['path_map'][0]['remote_path'], target)
        self.assertEqual({op for op, _ in self.calls}, {'setup_status'})

    def test_post_never_discovers_projects_on_the_request_thread(self):
        with mock.patch.object(self.setup, '_spawn'), mock.patch.object(self.setup, '_selected_projects') as selected:
            state = self.setup.start('dev', {'projects': ['acme/shop']})
        selected.assert_not_called()
        self.peer.assert_not_called()
        self.assertEqual(state['status'], 'running')

    def test_every_write_uses_a_durable_intent_and_blocks_following_writes_after_lost_reply(self):
        for step in ('service', 'hole', 'git', 'extensions', 'tools', 'projects', 'phone'):
            with self.subTest(step=step):
                self.setup.records = {}
                path = self.setup._path('dev')
                if path.exists():
                    path.unlink()
                record = self.setup._load('dev')
                def lose():
                    saved = json.loads(path.read_text())
                    self.assertEqual(saved['inflight'], {'step': step, 'key': step})
                    raise remotes.Unavailable('lost')
                with self.assertRaises(server_setup.UnknownOutcome):
                    self.setup._write(record, step, step, lose)
                with self.assertRaises(server_setup.UnknownOutcome):
                    self.setup._write(record, step, step, lambda: self.fail('replayed'))
                self.assertEqual(record['phase'], 'remote_unknown')

    def test_docker_refusal_and_hole_install_reprobe(self):
        self.status['docker']['usable'] = False
        self.setup.start('dev', {}, 'hole')
        self.assertEqual(self.row(self.wait(), 'hole')['state'], 'needs_user')
        self.assertEqual({op for op, _ in self.calls}, {'setup_status'})
        self.status['docker']['usable'] = True
        self.status['hole'].update(version='2.0.0', current=False, image=False)
        def peer(eid, op, body=None):
            self.calls.append((op, body))
            if op == 'install_hole':
                self.status['hole'].update(version='2.4.0', current=True)
                return {'installed': True}
            return copy.deepcopy(self.status)
        self.peer.side_effect = peer
        self.setup.start('dev', {}, 'hole')
        row = self.row(self.wait(), 'hole')
        self.assertEqual(row['state'], 'ok')
        # A missing image is not a prerequisite: Hole builds it on the first sandboxed session.
        self.assertIn('first sandboxed session', row['detail'])
        self.assertEqual(sum(op == 'install_hole' for op, _ in self.calls), 1)

    def test_service_update_waits_for_the_restarted_service_to_answer(self):
        # Regression: the check right after the restart ran before the new service answered its ping,
        # so a successful update was reported as "could not be verified".
        self.status['bundle_sha'] = 'old'
        self.setup.settle = (5, 0)
        def install(entry, body, timeout):
            self.status.update(bundle_sha='bundle', service_running=False)
            return {'installed': True}
        checks = []
        def peer(eid, op, body=None):
            if op == 'setup_status':
                checks.append(1)
                if len(checks) == 3:
                    self.status['service_running'] = True
            return copy.deepcopy(self.status)
        self.peer.side_effect = peer
        with mock.patch.object(remotes, 'request', side_effect=install):
            self.setup.start('dev', {}, 'service')
            self.assertEqual(self.row(self.wait(), 'service')['state'], 'ok')
        self.assertEqual(len(checks), 3)

    def test_service_extension_updates_and_project_clone_are_followed_by_state_checks(self):
        self.status['bundle_sha'] = 'old'
        def install(entry, body, timeout):
            self.assertTrue(body['setup'])
            self.status['bundle_sha'] = 'bundle'
            return {'installed': True}
        with mock.patch.object(remotes, 'request', side_effect=install) as service:
            self.setup.start('dev', {}, 'service')
            self.assertEqual(self.row(self.wait(), 'service')['state'], 'ok')
            service.assert_called_once()
        self.status['extensions_sha'] = 'old'
        def peer(eid, op, body=None):
            if op == 'sync_extensions':
                self.status['extensions_sha'] = body['sha']
                return {'sha': body['sha'], 'extensions': []}
            if op == 'clone_project':
                return {'path': self.entry['project_path'] + '/' + body['relative_path'], 'existing': False}
            return copy.deepcopy(self.status)
        self.peer.side_effect = peer
        self.setup.start('dev', {}, 'extensions')
        self.assertEqual(self.row(self.wait(), 'extensions')['state'], 'ok')
        self.setup._load('dev')['targets'] = [dict(key='acme/shop', local_path='/local/acme/shop',
            relative_path='acme/shop', url='git@example.com:acme/shop.git')]
        self.setup.start('dev', {}, 'projects')
        state = self.wait()
        self.assertEqual(self.row(state, 'projects')['state'], 'ok')
        self.assertEqual(state['path_map'][0]['remote_path'], self.entry['project_path'] + '/acme/shop')


    def test_completion_record_write_failure_also_requires_reconciliation(self):
        record = self.setup._load('dev')
        real_save = self.setup._save
        writes = []
        def save(value):
            writes.append(value['phase'])
            if len(writes) > 1:
                raise OSError('fixture disk full')
            real_save(value)
        call = mock.Mock(return_value={'public_key': PUBLIC})
        with mock.patch.object(self.setup, '_save', side_effect=save):
            with self.assertRaises(server_setup.UnknownOutcome):
                self.setup._write(record, 'git', 'git', call)
            with self.assertRaises(server_setup.UnknownOutcome):
                self.setup._write(record, 'git', 'git', call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(record['phase'], 'remote_unknown')
        self.assertEqual(record['unknown']['step'], 'git')
        self.assertEqual(json.loads(self.setup._path('dev').read_text())['phase'], 'remote_sending')

    def test_git_access_pins_laptop_host_keys_and_is_ok_only_when_repositories_are_readable(self):
        self.setup._load('dev')['targets'] = [dict(key='acme/shop', local_path='/local/acme/shop',
            relative_path='acme/shop', url='git@example.com:acme/shop.git')]
        access = {'git@example.com:acme/shop.git': None}
        def peer(eid, op, body=None):
            self.calls.append((op, copy.deepcopy(body)))
            if op == 'git_access':
                return {'access': dict(access), 'host_keys_added': 0}
            return copy.deepcopy(self.status)
        self.peer.side_effect = peer
        with mock.patch.object(server_setup, 'known_hosts', return_value=[HOST_KEY]) as pinned:
            self.setup.start('dev', {}, 'git')
            row = self.row(self.wait(), 'git')
            self.assertEqual(row['state'], 'needs_user')
            self.assertEqual((row['action']['kind'], row['action']['public_key'], row['action']['hosts']),
                             ('public_key', PUBLIC, ['example.com']))
            self.assertEqual(row['action']['fix']['path'], '/api/environments/dev/setup/git')
            body = next(b for op, b in self.calls if op == 'git_access')
            self.assertEqual(body, {'urls': ['git@example.com:acme/shop.git'], 'known_hosts': [HOST_KEY]})
            pinned.assert_called_with(['git@example.com:acme/shop.git'])
            access['git@example.com:acme/shop.git'] = 'setup'
            self.setup.start('dev', {}, 'git')
            row = self.row(self.wait(), 'git')
        self.assertEqual(row['state'], 'ok')
        self.assertEqual(self.setup._load('dev')['git_identity'], {'git@example.com:acme/shop.git': 'setup'})
        self.assertNotIn('action', row)
        self.assertNotIn('git_access', self.setup._load('dev')['operations'])  # an idempotent check, not a write

    def test_account_git_access_needs_no_new_key_and_clone_uses_that_identity(self):
        del self.status['git_key']
        url = 'git@example.com:acme/shop.git'
        self.setup._load('dev')['targets'] = [dict(key='acme/shop', local_path='/local/acme/shop',
            relative_path='acme/shop', url=url)]
        def peer(eid, op, body=None):
            self.calls.append((op, copy.deepcopy(body)))
            if op == 'git_access':
                return {'access': {url: 'account'}, 'host_keys_added': 0}
            if op == 'clone_project':
                return {'path': self.entry['project_path'] + '/acme/shop', 'existing': False}
            return copy.deepcopy(self.status)
        self.peer.side_effect = peer
        with mock.patch.object(server_setup, 'known_hosts', return_value=[]):
            self.setup.start('dev', {}, 'git')
            self.assertEqual(self.row(self.wait(), 'git')['state'], 'ok')
            self.setup.start('dev', {}, 'projects')
            self.assertEqual(self.row(self.wait(), 'projects')['state'], 'ok')
        ops = [op for op, _ in self.calls]
        self.assertNotIn('setup_git_key', ops)
        self.assertEqual(next(b for op, b in self.calls if op == 'clone_project')['identity'], 'account')

    def test_forge_host_and_known_hosts_use_only_valid_public_lines(self):
        self.assertEqual(server_setup.forge_host('git@example.com:acme/shop.git'), 'example.com')
        self.assertEqual(server_setup.forge_host('ssh://git@example.com:2222/acme/shop.git'), 'example.com')
        reply = mock.Mock(stdout='# Host example.com found: line 3\n' + HOST_KEY + '\nexample.com garbage\n')
        with mock.patch('subprocess.run', return_value=reply) as run:
            self.assertEqual(server_setup.known_hosts(['git@example.com:acme/shop.git']), [HOST_KEY])
        self.assertEqual(run.call_args.args[0], ['ssh-keygen', '-F', 'example.com'])

    def test_failed_hook_does_not_persist_exception_values(self):
        extensions.server_requirements.side_effect = RuntimeError('fixture-private-value')
        self.setup.start('dev', {}, 'tools')
        state = self.wait()
        self.assertEqual(self.row(state, 'tools')['state'], 'failed')
        self.assertNotIn('fixture-private-value', self.setup._path('dev').read_text())



    def test_phone_missing_or_private_relay_needs_user_without_peer_or_secret_persistence(self):
        for relay in (None, 'https://relay.example.com?token=' + 'a' * 43, 'https://user:fixture@example.com'):
            with mock.patch.object(mobile, 'load_state', return_value={'relay': relay}):
                self.assertEqual(self.row(self.setup.state('dev'), 'phone')['state'], 'needs_user')
                self.setup.start('dev', {}, 'phone')
                row = self.row(self.wait(), 'phone')
                self.assertEqual(row['state'], 'needs_user')
                self.assertIn('Configure Mobile access', row['detail'])
                self.assertNotIn('action', row)
                with self.assertRaises(ValueError):
                    self.setup.pair_phone('dev', {})
        self.peer.assert_not_called()
        self.assertNotIn('a' * 43, self.setup._path('dev').read_text())

    def test_phone_installs_once_with_intent_then_pairs_outside_phase_record(self):
        self.mobile_status['venv_ok'] = False
        def peer(eid, op, body=None):
            self.calls.append((op, body))
            self.assertEqual(body, {'relay': 'https://relay.example.com', 'name': 'dev'})
            if op == 'setup_mobile':
                saved = json.loads(self.setup._path(eid).read_text())
                self.assertEqual(saved['inflight'], {'step': 'phone', 'key': 'phone'})
                self.mobile_status['venv_ok'] = True
                return {'installed': True}
            return dict(self.mobile_status)
        self.peer.side_effect = peer
        for _ in range(2):
            self.setup.start('dev', {}, 'phone')
            row = self.row(self.wait(), 'phone')
            self.assertEqual(row['state'], 'ok')
            self.assertEqual(row['action'], {'kind': 'pair_phone', 'label': 'Pair phone…', 'method': 'POST',
                                             'path': '/api/environments/dev/setup/phone/pair', 'body': {}})
        self.assertEqual([op for op, _ in self.calls].count('setup_mobile'), 1)
        self.assertIn('Pair this server as backup on the phone', row['detail'])
        # Regression: after pairing the row still asked to pair.
        self.mobile_status['paired'] = 1
        self.peer.side_effect = peer
        self.setup.start('dev', {}, 'phone')
        row = self.row(self.wait(), 'phone')
        self.assertEqual((row['state'], row['detail']), ('ok', 'Paired as backup on 1 phone. The server relay bridge is ready.'))
        self.assertEqual(row['action']['kind'], 'pair_phone')
        before = self.setup._path('dev').read_bytes()
        invitation = {'url': 'https://phone.example.com/#iroh=fixture', 'id': 'b' * 64, 'expires': time.time() + 60}
        self.peer.side_effect = None
        self.peer.return_value = dict(invitation, _peer_identity='a' * 32)
        with mock.patch.object(common, 'audit') as audit, mock.patch('builtins.print') as output:
            self.assertEqual(self.setup.pair_phone('dev', {}), invitation)
        self.peer.assert_called_with('dev', 'mobile_pair', {'relay': 'https://relay.example.com', 'name': 'dev'})
        audit.assert_not_called(); output.assert_not_called()
        self.assertEqual(self.setup._path('dev').read_bytes(), before)
        self.assertNotIn(invitation['url'], json.dumps(self.setup.state('dev')))

    def test_phone_lost_install_reconciles_without_reinstalling_or_issuing_invitation(self):
        self.mobile_status['venv_ok'] = False
        def peer(eid, op, body=None):
            self.calls.append((op, body))
            if op == 'setup_mobile':
                self.mobile_status['venv_ok'] = True
                raise remotes.Unavailable('fixture')
            return dict(self.mobile_status)
        self.peer.side_effect = peer
        self.setup.start('dev', {}, 'phone')
        self.assertEqual(self.wait()['phase'], 'remote_unknown')
        with self.assertRaises(ValueError):
            self.setup.pair_phone('dev', {})
        self.setup.start('dev', {'reconcile': True}, 'phone')
        self.assertEqual(self.wait()['phase'], 'reconciled')
        self.assertEqual([op for op, _ in self.calls], ['mobile_status', 'setup_mobile', 'mobile_status'])

    def test_phone_running_failed_refused_and_not_ready_states(self):
        with mock.patch.object(self.setup, '_spawn'):
            self.setup.start('dev', {}, 'phone')
        record = self.setup._load('dev')
        self.setup._set(record, 'phone', 'running', 'Checking server…')
        self.assertEqual(self.row(self.setup.state('dev'), 'phone')['state'], 'running')
        with mock.patch.object(self.setup, '_phone', side_effect=RuntimeError('fixture')):
            self.setup._run(record)
        self.assertEqual(self.row(self.setup.state('dev'), 'phone')['state'], 'failed')
        self.peer.side_effect = remotes.Rejected('Install the Linux server service first.')
        self.setup.start('dev', {}, 'phone')
        self.assertEqual(self.row(self.wait(), 'phone')['state'], 'needs_user')
        self.peer.side_effect = None
        self.peer.return_value = dict(self.mobile_status, ready=False)
        self.setup.start('dev', {}, 'phone')
        self.assertEqual(self.row(self.wait(), 'phone')['state'], 'needs_user')
        self.peer.side_effect = remotes.Unavailable('https://phone.example.com/#iroh=secret-fixture')
        with self.assertRaises(ValueError) as error:
            self.setup.pair_phone('dev', {})
        self.assertNotIn('secret-fixture', str(error.exception))
        self.assertNotIn('secret-fixture', self.setup._path('dev').read_text())
        for body in ({'label': 'Phone'}, None, []):
            with self.assertRaises(ValueError):
                self.setup.pair_phone('dev', body)
        self.entry['mode'] = 'ssh'
        with self.assertRaises(ValueError):
            self.setup.pair_phone('dev', {})


class RequirementContractTest(unittest.TestCase):
    def row(self, **kw):
        return dict(id='compiler', title='Compiler', detail='Required toolchain', check=['compiler', '--version'],
                    install=['python3', 'install.py'], secrets=[dict(file='.config/tool.env', key='ACCESS_TOKEN', label='Access token')], **kw)

    def test_optional_hook_and_valid_contract(self):
        with mock.patch.object(extensions, 'call', return_value=[]):
            self.assertEqual(extensions.server_requirements({}), [])
        with mock.patch.object(extensions, 'call', return_value=[[self.row()]]):
            rows = extensions.server_requirements({'platform': 'linux'})
        self.assertEqual(rows[0]['secrets'][0]['key'], 'ACCESS_TOKEN')
        local = dict(file='~/.m2/settings.xml', xml=[".//{*}server[{*}id='repo']/{*}password"])
        with mock.patch.object(extensions, 'call', return_value=[[dict(self.row(), secrets=[
                dict(file='.m2/settings.xml', key='PASSWORD', label='Password', local=local)])]]):
            rows = extensions.server_requirements({})
        self.assertEqual(rows[0]['secrets'][0]['local'], local)

    def test_malformed_duplicate_secret_value_shell_and_sudo_are_refused(self):
        valid = self.row()
        cases = [None, {}, [dict(valid, id='../tool')], [valid, valid], [dict(valid, check='compiler --version')],
                 [dict(valid, install=['sudo', 'install'])], [dict(valid, check=['bash', '-c', 'true'])],
                 [dict(valid, secrets=[dict(file='.env', key='KEY', label='Key', value='fixture')])],
                 [dict(valid, secrets=[dict(file='/etc/tool', key='KEY', label='Key')])],
                 [dict(valid, secrets=[dict(file='../tool', key='KEY', label='Key')])],
                 [dict(valid, install=['tool', '--token', 'fixture-private-value'])]] + [
                 [dict(valid, secrets=[dict(file='.env', key='KEY', label='Key', local=local)])] for local in (
                     'settings.xml', dict(file='/etc/settings.xml', xml=['.//x']), dict(file='~/../x', xml=['.//x']),
                     dict(file='~/.m2/settings.xml', xml=[]), dict(file='~/.m2/settings.xml', xml=['.//x'] * 9),
                     dict(file='~/.m2/settings.xml', xml=['.//x'], value='fixture'))]
        for group in cases:
            with self.subTest(group=group), mock.patch.object(extensions, 'call', return_value=[group]):
                with self.assertRaises(ValueError):
                    extensions.server_requirements({})


class SetupPeerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.multiple(remotepeer, HOME=self.home, STATE=self.home / 'state', APP=self.home / 'app')
        patch.start(); self.addCleanup(patch.stop)
        self.req = dict(expected_identity='a' * 32, mode='service', project_path=str(self.home / 'projects'))
        # Existing systemd fixtures stay deterministic on either host OS; no native launchctl.
        patch = mock.patch.object(remotepeer.sys, 'platform', 'linux')
        patch.start(); self.addCleanup(patch.stop)

    def test_setup_commands_reach_the_user_systemd_manager(self):
        # Regression: a scrubbed env without XDG_RUNTIME_DIR made `systemctl --user` fail
        # ("Failed to connect to bus"), so syncing extensions could never restart the service.
        runtime = self.home / 'run'
        runtime.mkdir()
        (runtime / 'bus').write_text('')
        seen = {}
        def run(argv, **kw):
            seen.update(kw['env'])
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        secret = {'XDG_RUNTIME_DIR': str(runtime), 'GITLAB_TOKEN': 'placeholder-value', 'SSH_AUTH_SOCK': '/tmp/agent'}
        with mock.patch.dict(os.environ, secret), mock.patch.object(subprocess, 'run', side_effect=run):
            remotepeer._setup_run(['systemctl', '--user', 'daemon-reload'])
        self.assertEqual(seen['XDG_RUNTIME_DIR'], str(runtime))
        self.assertEqual(seen['DBUS_SESSION_BUS_ADDRESS'], 'unix:path=%s/bus' % runtime)
        self.assertNotIn('GITLAB_TOKEN', seen)
        self.assertNotIn('SSH_AUTH_SOCK', seen)

    def key(self):
        path = self.home / '.ssh' / remotepeer.SETUP_KEY
        path.parent.mkdir()
        path.write_text('PRIVATE PLACEHOLDER')
        path.with_suffix('.pub').write_text(PUBLIC)

    def mobile_release(self):
        release = self.home / 'app/current'
        (release / 'piper').mkdir(parents=True)
        (release / 'piper/mobile.py').write_text('# fixture\n')
        (release / 'piper/mobile-requirements.txt').write_text('# pinned\niroh==1.2.0\nwebsockets==15.0.1\npywebpush==2.5.0\n')
        return dict(self.req, relay='https://relay.example.com', name='Development $HOST %n "server"')

    def test_projects_folder_outside_home_is_used_only_as_the_verified_service_root(self):
        # Regression: a server keeping projects in /srv-like folders refused every project check and clone
        # ("Setup may only write inside the SSH account home").
        srv = Path(self.tmp.name).parent / (Path(self.tmp.name).name + '-srv')
        srv.mkdir()
        self.addCleanup(shutil.rmtree, srv, True)
        req = dict(self.req, project_path=str(srv), relative_path='development/tool', url='git@example.com:acme/tool.git')
        with mock.patch.object(remotepeer, 'require_service_root') as verified:
            root, target, _ = remotepeer._clone_target(req)
            verified.assert_called_once_with(str(srv.resolve()))
        self.assertEqual(target, srv.resolve() / 'development/tool')
        with mock.patch.object(remotepeer, 'require_service_root', side_effect=ValueError('Projects folder differs')), \
                self.assertRaises(remotepeer.NotExecuted):
            remotepeer._clone_target(req)
        (srv / 'link').symlink_to(self.home)
        with mock.patch.object(remotepeer, 'require_service_root'), self.assertRaises(remotepeer.NotExecuted):
            remotepeer._clone_target(dict(req, relative_path='link'))

    def test_mobile_runtime_without_ensurepip_uses_the_account_pip(self):
        # Regression: Ubuntu without python3-venv cannot create a venv with pip (installing it needs root),
        # so phone backup always failed. A pip-less venv is filled by the account's own pip instead.
        req = self.mobile_release()
        runtime = self.home / '.piper/mobile-venv'
        installed = []
        def run(argv, timeout=20, **kw):
            if argv[0] == str(runtime / 'bin/python'):
                return mock.Mock(returncode=0 if installed else 1)
            return mock.Mock(returncode=0)
        def install(body):
            argv = body['argv']
            installed.append(argv)
            if argv[1:3] == ['-m', 'venv'] and '--without-pip' not in argv:
                (runtime / 'bin').mkdir(parents=True, exist_ok=True)  # ensurepip fails after a partial venv
                return {'returncode': 1, 'output': '', 'truncated': False}
            if '--without-pip' in argv:
                self.assertFalse(runtime.exists(), 'the partial venv is removed first')
                (runtime / 'bin').mkdir(parents=True)
                (runtime / 'bin/python').write_text('# fixture')
                (runtime / 'pyvenv.cfg').write_text('home = fixture')
            return {'returncode': 0, 'output': '', 'truncated': False}
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=run), \
                mock.patch.object(remotepeer, 'run_requirement', side_effect=install):
            self.assertEqual(remotepeer.setup_mobile(req), {'installed': True})
        self.assertEqual(installed[-1][:6], [remotepeer.sys.executable, '-m', 'pip', '--python', str(runtime / 'bin/python'), 'install'])
        self.assertTrue(all(Path(x).name not in ('sudo', 'sh', 'bash') for argv in installed for x in argv))

    def test_mobile_runtime_and_unit_setup_are_idempotent_pinned_and_shell_free(self):
        req = self.mobile_release()
        runtime = self.home / '.piper/mobile-venv'
        versions_ok = False
        commands = []
        def run(argv, timeout=20, **kw):
            commands.append((argv, timeout))
            # The real metadata probe, not a stamp, controls whether pip must run again.
            if argv[0] == str(runtime / 'bin/python'):
                self.assertIn('importlib.metadata', argv[-1])
                self.assertIn("'iroh': '1.2.0'", argv[-1])
                return mock.Mock(returncode=0 if versions_ok else 1)
            return mock.Mock(returncode=0)
        def install(body):
            nonlocal versions_ok
            argv = body['argv']
            self.assertIsInstance(argv, list)
            self.assertNotIn(argv[0], ('sh', 'bash', 'sudo'))
            if 'venv' in argv:
                self.assertEqual(body['timeout'], 60)
                (runtime / 'bin').mkdir(parents=True, exist_ok=True)
                (runtime / 'bin/python').write_text('# fixture')
                (runtime / 'pyvenv.cfg').write_text('home = fixture')
            else:
                self.assertEqual(argv[1:], ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-r',
                                           str(self.home / 'app/current/piper/mobile-requirements.txt')])
                self.assertEqual(body['timeout'], 300)
                versions_ok = True
            return {'returncode': 0, 'output': 'withheld', 'truncated': True}
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=run), \
                mock.patch.object(remotepeer, 'run_requirement', side_effect=install) as installer:
            self.assertEqual(remotepeer.setup_mobile(req), {'installed': True})
            unit = self.home / '.config/systemd/user/piper-mobile.service'
            content, before = unit.read_text(), unit.stat().st_mtime_ns
            self.assertIn('ExecStart=:"' + str(runtime / 'bin/python') + '"', content)
            self.assertIn('"' + str(self.home / 'app/current/piper/mobile.py') + '" "serve"', content)
            self.assertIn('"--relay" "https://relay.example.com"', content)
            self.assertIn(r'$HOST %%n \"server\"', content)
            self.assertIn('Environment="PIPER_HOME=' + str(self.home / 'state') + '"', content)
            self.assertIn('Environment=PIPER_PORT=17896', content)
            self.assertIn('Restart=on-failure', content)
            self.assertIn('WantedBy=default.target', content)
            self.assertNotIn('token', content.lower())
            self.assertEqual(remotepeer.setup_mobile(req), {'installed': True})
            self.assertEqual(unit.stat().st_mtime_ns, before)
            self.assertEqual(installer.call_count, 2)
        self.assertEqual(sum(argv == ['systemctl', '--user', 'restart', 'piper-mobile.service'] for argv, _ in commands), 1)
        self.assertTrue(all('sudo' not in argv and 'piper-server.service' not in argv for argv, _ in commands))

    def test_macos_mobile_setup_status_update_and_restart_are_shell_free(self):
        req = self.mobile_release()
        loaded, commands, installs = {}, [], []
        def run(argv, *args, **kw):
            commands.append(argv)
            self.assertEqual(argv[0], 'launchctl')
            self.assertTrue(argv[2].startswith('user/'))
            if argv[1] == 'print' and argv[2].endswith('/' + remotepeer.MOBILE_LABEL):
                return subprocess.CompletedProcess(argv, 0 if loaded else 1,
                                                   b'\tpid = 123\n' if loaded else b'', b'')
            if argv[1] == 'bootout':
                loaded.clear()
            if argv[1] == 'bootstrap':
                loaded['active'] = True
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        def install(body):
            installs.append(body['argv'])
            return {'returncode': 0}
        with mock.patch.object(remotepeer.sys, 'platform', 'darwin'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/launchctl'), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=run), \
                mock.patch.object(remotepeer, '_mobile_runtime', side_effect=lambda *args: bool(installs)), \
                mock.patch.object(remotepeer, 'run_requirement', side_effect=install):
            (self.home / 'state').mkdir()
            (self.home / 'state/mobile.json').write_text(json.dumps(dict(relay=req['relay'], name=req['name'], devices={'fixture': {}})))
            with mock.patch.object(remotepeer, '_service_call', return_value=(200, {'ready': True})):
                remotepeer.setup_mobile(req)
                self.assertTrue(all(remotepeer.mobile_status(req).values()))
                path = remotepeer._launch_path(remotepeer.MOBILE_LABEL)
                before = path.stat().st_mtime_ns
                agent = plistlib.loads(path.read_bytes())
                self.assertEqual(agent['ProgramArguments'][-4:], ['--relay', req['relay'], '--name', req['name']])
                self.assertEqual(agent['ProgramArguments'][0], str(self.home / '.piper/mobile-venv/bin/python'))
                self.assertEqual(agent['EnvironmentVariables']['PIPER_HOME'], str(self.home / 'state'))
                remotepeer.setup_mobile(req)
                self.assertEqual(path.stat().st_mtime_ns, before)
                self.assertEqual(sum(c[1] == 'bootstrap' for c in commands), 1)
                changed = dict(req, name='Renamed server')
                remotepeer.setup_mobile(changed)
                self.assertFalse(remotepeer.mobile_status(changed)['ready'])
                self.assertEqual(sum(c[1] == 'bootstrap' for c in commands), 2)
        self.assertEqual(installs[0][:3], [sys.executable, '-m', 'venv'])
        self.assertEqual(len(installs), 2)  # venv and pinned pip install only once
        self.assertFalse(any(remotepeer.SERVER_LABEL in ' '.join(c) for c in commands))

    def test_macos_mobile_unavailable_domain_is_not_executed_without_writes(self):
        req = self.mobile_release()
        with mock.patch.object(remotepeer.sys, 'platform', 'darwin'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/launchctl'), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_setup_run', return_value=subprocess.CompletedProcess([], 1, b'', b'')), \
                mock.patch.object(remotepeer, 'run_requirement') as installer:
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'launchd user domain'):
                remotepeer.setup_mobile(req)
            installer.assert_not_called()
        self.assertFalse((self.home / '.piper').exists())
        self.assertFalse((self.home / 'Library').exists())

    def test_mobile_status_is_public_and_checks_runtime_unit_and_actual_relay(self):
        req = self.mobile_release()
        release, runtime, unit = remotepeer._mobile_paths()
        unit.parent.mkdir(parents=True)
        unit.write_text(remotepeer._mobile_unit(req['relay'], req['name'], runtime, release))
        (self.home / 'state').mkdir()
        state = self.home / 'state/mobile.json'
        state.write_text(json.dumps({'relay': req['relay'], 'name': req['name'], 'key': 'private-fixture'}))
        with mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_mobile_runtime', return_value=True), \
                mock.patch.object(remotepeer, '_ok', return_value=True), \
                mock.patch.object(remotepeer, '_service_call', return_value=(200, {'ready': True, 'devices': ['private-fixture']})):
            self.assertEqual(remotepeer.mobile_status(req), dict(venv_ok=True, unit_active=True,
                             relay_configured=True, unit_enabled=True, ready=True, paired=0))
            # Only how many phones are paired: device ids and keys stay on the server.
            state.write_text(json.dumps({'relay': req['relay'], 'name': req['name'], 'key': 'private-fixture',
                                         'devices': {'private-device-a': {}, 'private-device-b': {}}}))
            self.assertEqual(remotepeer.mobile_status(req)['paired'], 2)
            self.assertNotIn('private', json.dumps(remotepeer.mobile_status(req)))
            state.write_text(json.dumps({'relay': 'https://other.example.com', 'name': req['name']}))
            self.assertFalse(remotepeer.mobile_status(req)['ready'])
            unit.write_text('outdated unit')
            self.assertFalse(remotepeer.mobile_status(req)['relay_configured'])

    def test_mobile_release_update_changes_unit_even_when_runtime_versions_stay_pinned(self):
        req = self.mobile_release()
        current = self.home / 'app/current'
        previous = current.rename(self.home / 'app/release-one')
        current.symlink_to(previous.name)
        release, runtime, _ = remotepeer._mobile_paths()
        first = remotepeer._mobile_unit(req['relay'], req['name'], runtime, release)
        self.assertIn(str(previous / 'piper/mobile.py'), first)
        current.unlink()
        (self.home / 'app/release-two').mkdir()
        current.symlink_to('release-two')
        release, runtime, _ = remotepeer._mobile_paths()
        second = remotepeer._mobile_unit(req['relay'], req['name'], runtime, release)
        self.assertNotEqual(first, second)
        self.assertIn(str(self.home / 'app/release-two/piper/mobile.py'), second)

    def test_mobile_refusals_never_install_or_write_unit(self):
        req = self.mobile_release()
        with mock.patch.object(remotepeer, 'run_requirement') as install, \
                mock.patch.object(remotepeer, '_setup_run') as run, \
                mock.patch.object(remotepeer, 'require_service_root') as root, \
                mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/systemctl'):
            for fields in ({'relay': 'http://relay.example.com'}, {'relay': 'https://user:fixture@example.com'},
                           {'relay': 'https://relay.example.com?token=' + 'a' * 43}, {'relay': 'https://example.com/path'},
                           {'name': 'bad\nExecStart=sudo'}, {'name': 'x' * 81}, {'name': None}, {'mode': 'ssh'}):
                with self.subTest(fields=fields), self.assertRaises(remotepeer.NotExecuted):
                    remotepeer.setup_mobile(dict(req, **fields))
            root.side_effect = ValueError('wrong project root')
            with self.assertRaises(ValueError):
                remotepeer.setup_mobile(req)
            root.side_effect = None
            with mock.patch.object(remotepeer.sys, 'platform', 'freebsd'), self.assertRaises(remotepeer.NotExecuted):
                remotepeer.setup_mobile(req)
            run.assert_not_called(); install.assert_not_called()
            runtime = self.home / '.piper/mobile-venv'
            runtime.parent.mkdir()
            runtime.symlink_to(self.home / 'other')
            with self.assertRaises(remotepeer.NotExecuted):
                remotepeer.setup_mobile(req)
            runtime.unlink()
            unit = self.home / '.config/systemd/user/piper-mobile.service'
            unit.parent.mkdir(parents=True)
            unit.symlink_to(self.home / 'other.service')
            with self.assertRaises(remotepeer.NotExecuted):
                remotepeer.setup_mobile(req)
            unit.unlink()
            pins = self.home / 'app/current/piper/mobile-requirements.txt'
            pins.write_text('--extra-index-url https://example.com\n')
            with self.assertRaises(remotepeer.NotExecuted):
                remotepeer.setup_mobile(req)
            run.assert_not_called(); install.assert_not_called()
        self.assertFalse((self.home / '.config/systemd/user/piper-mobile.service').exists())

    def test_mobile_install_failure_never_enables_service_or_returns_command_output(self):
        req = self.mobile_release()
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/systemctl'), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_ok', return_value=True), \
                mock.patch.object(remotepeer, 'run_requirement', return_value={'returncode': 124, 'output': 'private-fixture'}), \
                mock.patch.object(remotepeer, '_setup_run') as run:
            with self.assertRaises(ValueError) as error:
                remotepeer.setup_mobile(req)
            self.assertNotIn('private-fixture', str(error.exception))
            run.assert_not_called()
        self.assertFalse((self.home / '.config/systemd/user/piper-mobile.service').exists())

    def test_mobile_pair_uses_exact_server_settings_invitation_without_persisting_or_logging_link(self):
        req = self.mobile_release()
        state = self.home / 'mobile.json'
        ready = dict(venv_ok=True, unit_active=True, unit_enabled=True, relay_configured=True, ready=True)
        with mock.patch.object(mobile, 'STATE', str(state)), mock.patch.object(mobile, 'desktop_status', return_value={'ready': True}), \
                mock.patch.object(remotepeer, 'require_service_root'), \
                mock.patch.object(remotepeer, '_mobile_status', return_value=ready), \
                mock.patch.object(common, 'audit') as audit, mock.patch('builtins.print') as output:
            mobile.save_state({'v': 1, 'devices': {}, 'endpoint': '1' * 64, 'relay': req['relay'], 'client': 'https://phone.example.com'})
            def call(method, path, body, timeout):
                self.assertEqual((method, path, body, timeout), ('POST', '/api/mobile/invite', {'label': 'Phone backup'}, 5))
                return 200, mobile.desktop_invitation(body['label'])
            with mock.patch.object(remotepeer, '_service_call', side_effect=call):
                first = remotepeer.mobile_pair(req)
                second = remotepeer.mobile_pair(req)
            self.assertNotEqual(first['url'], second['url'])
            ticket = json.loads(base64.urlsafe_b64decode(second['url'].split('#iroh=')[1] + '==='))
            self.assertAlmostEqual(second['expires'] - time.time(), 60, delta=2)
            self.assertNotIn(ticket['code'], state.read_text())
            self.assertNotIn(second['url'], state.read_text())
            audit.assert_not_called(); output.assert_not_called()
            self.assertTrue(mobile.pair('2' * 64, ticket['code']))
            audit.reset_mock()  # pairing itself audits only an event name; invitation never audits
            with mock.patch.object(remotepeer, '_service_call', side_effect=ValueError(second['url'])):
                with self.assertRaises(ValueError) as error:
                    remotepeer.mobile_pair(req)
                self.assertNotIn(second['url'], str(error.exception))
            ready['ready'] = False
            with mock.patch.object(remotepeer, '_service_call') as call, self.assertRaises(remotepeer.NotExecuted):
                remotepeer.mobile_pair(req)
            call.assert_not_called(); audit.assert_not_called(); output.assert_not_called()

    def test_key_generation_and_idempotency_only_public_key_returns(self):
        def generate(argv, **kw):
            self.assertEqual(argv[:4], ['ssh-keygen', '-t', 'ed25519', '-N'])
            self.assertNotIn('shell', kw)
            path = Path(argv[-1])
            path.write_text('PRIVATE PLACEHOLDER')
            path.with_suffix('.pub').write_text(PUBLIC)
            return mock.Mock(returncode=0)
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/ssh-keygen'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=generate) as run:
            self.assertEqual(remotepeer.setup_git_key(self.req), {'public_key': PUBLIC})
            self.assertEqual(remotepeer.setup_git_key(self.req), {'public_key': PUBLIC})
            self.assertEqual(run.call_count, 1)
        self.assertEqual((self.home / '.ssh' / remotepeer.SETUP_KEY).stat().st_mode & 0o777, 0o600)

    def test_key_incomplete_or_missing_binary_refuses_before_generation(self):
        with mock.patch.object(remotepeer.shutil, 'which', return_value=None), mock.patch.object(remotepeer, '_setup_run') as run:
            with self.assertRaises(remotepeer.NotExecuted):
                remotepeer.setup_git_key(self.req)
            run.assert_not_called()
        self.key()
        (self.home / '.ssh' / remotepeer.SETUP_KEY).with_suffix('.pub').unlink()
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'Incomplete'):
            remotepeer.setup_git_key(self.req)

    def test_setup_status_returns_only_metadata_and_best_effort_login(self):
        self.key()
        (self.home / 'app/current').mkdir(parents=True)
        (self.home / 'app/current/version').write_text('b' * 64)
        with mock.patch.object(remotepeer, 'probe', return_value={'platform': 'linux'}), \
                mock.patch.object(remotepeer, '_service_info', return_value={'extensions': ['acme']}), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/tool'), \
                mock.patch.object(remotepeer, '_ok', side_effect=lambda argv: argv[0] != 'codex'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=lambda argv, *a, **k: mock.Mock(
                    returncode=0, stdout=b'{\"loggedIn\": true}' if argv[0] == 'claude' else b'2.4.0\n')), \
                mock.patch.object(remotepeer, 'ssh_projects', return_value=[]):
            reply = remotepeer.setup_status(self.req)
        self.assertTrue(reply['docker']['usable'])
        self.assertEqual(reply['hole']['version'], '2.4.0')
        self.assertEqual(reply['agent_login'], {'claude': 'ok', 'codex': 'unknown'})
        self.assertEqual(reply['git_key']['public_key'], PUBLIC)
        self.assertNotIn('PRIVATE PLACEHOLDER', json.dumps(reply))

    def test_git_access_appends_valid_host_keys_once_and_checks_read_access_without_shell(self):
        self.key()
        known = self.home / '.ssh/known_hosts'
        known.write_text('other.example.com ssh-ed25519 ' + 'C' * 68)
        url = 'git@example.com:acme/shop.git'
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/git'), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0)) as run:
            first = remotepeer.git_access(dict(self.req, urls=[url], known_hosts=[HOST_KEY, HOST_KEY]))
            second = remotepeer.git_access(dict(self.req, urls=[url], known_hosts=[HOST_KEY]))
        self.assertEqual((first, second['host_keys_added']), ({'access': {url: 'account'}, 'host_keys_added': 1}, 0))
        self.assertEqual(known.read_text().splitlines(), ['other.example.com ssh-ed25519 ' + 'C' * 68, HOST_KEY])
        argv, env = run.call_args.args[0], run.call_args.kwargs['extra_env']
        self.assertEqual(argv[-4:], ['ls-remote', '--heads', '--', url])
        wrapper = Path(env['GIT_SSH']).read_text()
        self.assertTrue(wrapper.startswith('#!'))
        self.assertIn('-oStrictHostKeyChecking=yes', wrapper)
        self.assertNotIn(remotepeer.SETUP_KEY, wrapper)  # the account's own ssh setup is tried first
        # Without account access the server setup key is tried next.
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/git'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=[mock.Mock(returncode=128), mock.Mock(returncode=0)]) as run:
            self.assertEqual(remotepeer.git_access(dict(self.req, urls=[url]))['access'], {url: 'setup'})
        self.assertIn(remotepeer.SETUP_KEY, Path(run.call_args.kwargs['extra_env']['GIT_SSH']).read_text())

    def test_git_access_refuses_bad_host_keys_and_urls_before_writing(self):
        for body in (dict(known_hosts=['example.com ssh-ed25519 short']), dict(known_hosts=[HOST_KEY + '\nevil']),
                     dict(known_hosts=['@cert-authority * ssh-ed25519 ' + 'B' * 68]), dict(urls=['https://example.com/a.git']),
                     dict(urls=['ext::sh -c id'])):
            with self.subTest(body=body), mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/git'), \
                    mock.patch.object(remotepeer, '_setup_run') as run, self.assertRaises(remotepeer.NotExecuted):
                remotepeer.git_access(dict(self.req, **body))
            run.assert_not_called()
        self.assertFalse((self.home / '.ssh/known_hosts').exists())

    def test_secret_findings_flag_values_not_words(self):
        for text in ('PASSWORD = "{{MAVEN_PASSWORD}}"', 'def login(token: str, password=None): pass',
                     'https://user:{{PASS}}@example.com/x', '-----BEGIN PRIVATE KEY----- is a header we mention'):
            self.assertEqual(common.secret_findings(text), [], text)
        planted = {'forge or chat token': 'x = "glpat-' + 'a' * 20 + '"',
                   'AWS access key': 'AKIA' + 'A' * 16,
                   'URL password': 'https://user:hunter22@example.com/x',
                   'private key': '-----BEGIN OPENSSH PRIVATE KEY-----\n' + 'b' * 64 + '\n'}
        for name, text in planted.items():
            findings = common.secret_findings(text)
            self.assertEqual(findings, [name])
            self.assertNotIn(text, ''.join(findings))

    def test_requirement_no_shell_sudo_wrappers_and_bad_timeout(self):
        bad = (['sudo', 'true'], ['/bin/sh', '-c', 'true'], ['env', 'sudo', 'true'],
               ['python3', 'script.sh'], ['busybox', 'sh'], 'true', [], ['tool', '\x00'], ['tool', '\x1b[2J'], ['tool', 'x' * 65537],
               ['python3', '-c', 'x' * 60000, 'y' * 60000])
        with mock.patch.object(remotepeer.subprocess, 'Popen') as run:
            for argv in bad:
                with self.subTest(argv=argv), self.assertRaises(remotepeer.NotExecuted):
                    remotepeer.run_requirement({'argv': argv})
            for timeout in (0, 301, True, '20'):
                with self.assertRaises(remotepeer.NotExecuted):
                    remotepeer.run_requirement({'argv': ['tool'], 'timeout': timeout})
            run.assert_not_called()
        # Extension hooks ship self-contained multi-line programs (python3 -c <code>), e.g. 12 KB.
        program = 'import sys\n' + '# padding\n' * 1200 + 'sys.exit(0)\n'
        self.assertEqual(remotepeer.requirement_argv(['python3', '-c', program, 'check', 'jdk-17'])[2], program)

    def test_requirement_timeout_and_output_are_bounded_without_secret_export(self):
        script = self.home / 'tool.py'
        script.write_text('import os\nassert "EXAMPLE_TOKEN" not in os.environ\nprint("EXAMPLE_TOKEN=fixture-value" * 1000)\n')
        with mock.patch.dict(os.environ, EXAMPLE_TOKEN='fixture'):
            reply = remotepeer.run_requirement({'argv': [sys.executable, str(script)]})
        self.assertEqual(reply['returncode'], 0)
        self.assertTrue(reply['truncated'])
        self.assertLess(len(reply['output']), 256)
        self.assertNotIn('fixture-value', json.dumps(reply))
        script.write_text('import time\ntime.sleep(10)\n')
        reply = remotepeer.run_requirement({'argv': [sys.executable, str(script)], 'timeout': 1})
        self.assertTrue(reply['timed_out'])
        self.assertEqual(reply['returncode'], 124)
        script.write_text('import sys\nsys.stdout.write("x" * (2 << 20))\n')
        reply = remotepeer.run_requirement({'argv': [sys.executable, str(script)]})
        self.assertEqual(reply['returncode'], 125)
        self.assertTrue(reply['truncated'])

    def test_clone_mirrors_home_uses_direct_ssh_wrapper_and_existing_origin(self):
        self.key()
        req = dict(self.req, relative_path='acme/shop', url='git@example.com:acme/shop.git')
        def run(argv, timeout=20, **kw):
            if 'clone' in argv:
                self.assertNotIn('core.sshCommand', ' '.join(argv))
                self.assertNotIn('GIT_SSH_COMMAND', kw['extra_env'])
                self.assertEqual(kw['extra_env']['GIT_SSH_VARIANT'], 'ssh')
                wrapper = Path(kw['extra_env']['GIT_SSH'])
                self.assertIn('os.execv(', wrapper.read_text())
                (Path(argv[-1]) / '.git').mkdir(parents=True)
            return mock.Mock(returncode=0, stdout=req['url'].encode())
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/ssh'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=run) as command:
            result = remotepeer.clone_project(req)
            self.assertFalse(result['existing'])
            self.assertTrue(remotepeer.clone_project(req)['existing'])
            self.assertEqual(sum('clone' in call.args[0] for call in command.call_args_list), 1)
        self.assertEqual(result['path'], str(self.home / 'projects/acme/shop'))

    def test_failed_clone_that_left_nothing_is_a_definite_refusal(self):
        # Regression: a clone refused by the forge (key not added yet) showed "The server answer was lost".
        self.key()
        req = dict(self.req, relative_path='acme/shop', url='git@example.com:acme/shop.git')
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/ssh'), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=128, stdout=b'')):
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'Clone failed'):
                remotepeer.clone_project(req)
        def partial(argv, timeout=20, **kw):
            Path(argv[-1]).mkdir(parents=True)
            return mock.Mock(returncode=128, stdout=b'')
        with mock.patch.object(remotepeer.shutil, 'which', return_value='/usr/bin/ssh'), \
                mock.patch.object(remotepeer, '_setup_run', side_effect=partial):
            with self.assertRaises(ValueError) as caught:
                remotepeer.clone_project(req)
            self.assertNotIsInstance(caught.exception, remotepeer.NotExecuted)

    def test_clone_refusals_no_writes_or_commands(self):
        good = dict(self.req, relative_path='acme/shop', url='git@example.com:acme/shop.git')
        cases = [dict(good, relative_path='../shop'), dict(good, relative_path='/shop'),
                 dict(good, url='https://name:fixture@example.com/acme/shop'), dict(good, url='ext::sh -c true'),
                 dict(good, project_path='/etc/projects')]
        (self.home / 'escape').symlink_to('/etc', target_is_directory=True)
        cases.append(dict(good, project_path=str(self.home / 'escape')))
        with mock.patch.object(remotepeer, '_setup_run') as run:
            for req in cases:
                with self.subTest(req=req), self.assertRaises(remotepeer.NotExecuted):
                    remotepeer.clone_project(req)
            run.assert_not_called()
        (self.home / 'projects/acme/shop').mkdir(parents=True)
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'already exists'):
            remotepeer.clone_project(good)

    def extension_req(self, **extra):
        return dict(self.req, **archive({'acme/extension.py': 'API = 2\nNAME = "acme"\n',
                                         'acme/piper-extension.json': '{"name":"acme","label":"Tools","api":2}'}), **extra)

    def service(self):
        (self.home / 'app/current/piper').mkdir(parents=True, exist_ok=True)
        (self.home / 'app/current/piper/piperd.py').write_text('pass')
        return mock.patch.multiple(remotepeer, require_service_root=mock.Mock(),
                                   _service_info=mock.Mock(return_value={'extensions': ['acme']}),
                                   _service_env=mock.Mock(side_effect=lambda key: str(self.home / 'app/extensions' / self.ext_sha / 'acme')))

    def test_extension_sync_checksum_trust_dropin_restart_and_live_verification(self):
        req = self.extension_req()
        self.ext_sha = req['sha']
        with self.service(), mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0)) as run:
            reply = remotepeer.sync_extensions(req)
        self.assertEqual(reply['sha'], req['sha'])
        trust = json.loads((self.home / 'state/trusted-extensions.json').read_text())
        self.assertEqual(next(iter(trust.values()))['name'], 'acme')
        drop = (self.home / '.config/systemd/user/piper-server.service.d/20-extensions.conf').read_text()
        self.assertIn('PIPER_EXTENSIONS=', drop)
        self.assertNotIn('TOKEN', drop)
        self.assertEqual([c.args[0] for c in run.call_args_list], [['systemctl', '--user', 'daemon-reload'],
                                                                ['systemctl', '--user', 'restart', 'piper-server.service']])
        self.assertFalse(any('ptyhost' in ' '.join(c.args[0]) for c in run.call_args_list))

    def test_macos_extension_sync_rewrites_plist_and_checks_live_environment(self):
        req = self.extension_req()
        self.ext_sha = req['sha']
        commands, loaded, live_env = [], {}, {}
        reader = remotepeer._service_env
        with self.service(), mock.patch.object(remotepeer.sys, 'platform', 'darwin'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/launchctl'):
            remotepeer._launch_write(remotepeer.SERVER_LABEL, remotepeer._launch_content(
                remotepeer.SERVER_LABEL, [sys.executable, self.home / 'app/current/serve.py'], {'PATH': '/usr/bin:/bin'}))
            def run(argv, *args, **kw):
                commands.append(argv)
                self.assertEqual(argv[0], 'launchctl')
                if argv[1] == 'print':
                    text = '\tpid = 321\n\tenvironment = {\n\t\tPIPER_EXTENSIONS => ' + live_env.get('PIPER_EXTENSIONS', '') + '\n\t}\n'
                    return subprocess.CompletedProcess(argv, 0 if loaded else 1, text.encode(), b'')
                if argv[1] == 'bootout':
                    loaded.clear()
                if argv[1] == 'bootstrap':
                    live_env.update(plistlib.loads(Path(argv[3]).read_bytes())['EnvironmentVariables'])
                    loaded['pid'] = 321
                return subprocess.CompletedProcess(argv, 0, b'', b'')
            # Use the real live-environment reader instead of service()'s Linux fixture.
            with mock.patch.object(remotepeer, '_service_env', side_effect=reader), \
                    mock.patch.object(remotepeer, '_setup_run', side_effect=run):
                reply = remotepeer.sync_extensions(req)
                self.assertEqual(reply['extensions'], ['acme'])
                agent = remotepeer._launch_read(remotepeer.SERVER_LABEL)
                self.assertEqual(agent['EnvironmentVariables']['PIPER_EXTENSIONS'], str(self.home / 'app/extensions' / req['sha'] / 'acme'))
                self.assertEqual(agent['EnvironmentVariables']['PATH'], '/usr/bin:/bin')
                remotepeer.sync_extensions(req)  # repeated publication also verifies the restarted job
        self.assertFalse((self.home / '.config/systemd').exists())
        self.assertEqual(sum(c[1] == 'bootstrap' for c in commands), 2)

    def test_extension_archive_rejects_traversal_credentials_checksum_and_incomplete(self):
        good = self.extension_req()
        cases = [dict(good, sha='0' * 64)]
        for name in ('../escape', 'acme/.env', 'acme/access-token.txt', 'acme/credentials.json', 'acme/id_ed25519'):
            cases.append(dict(self.req, **archive({name: 'fixture'})))
        cases.append(dict(self.req, **archive({'acme/extension.py': 'pass'})))
        self.ext_sha = good['sha']
        with self.service(), mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, '_setup_run') as run:
            for req in cases:
                with self.subTest(req=req['sha']), self.assertRaises(ValueError):
                    remotepeer.sync_extensions(req)
            run.assert_not_called()
        self.assertFalse((self.home / 'app/extensions').exists())

    def test_extension_live_import_failure_is_not_reported_as_success(self):
        req = self.extension_req()
        self.ext_sha = req['sha']
        with self.service(), mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(remotepeer, '_service_info', return_value={'extensions': []}), \
                mock.patch.object(remotepeer.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'live loading'):
                remotepeer.sync_extensions(req)
        self.assertFalse((self.home / 'state/setup-extensions.json').exists())

    def test_hole_refuses_missing_docker_and_unsupported_os_without_download(self):
        with mock.patch.object(remotepeer, '_download') as download:
            for system, binary in (('darwin', '/bin/docker'), ('linux', None)):
                with mock.patch.object(remotepeer.sys, 'platform', system), \
                        mock.patch.object(remotepeer.shutil, 'which', return_value=binary), self.assertRaises(remotepeer.NotExecuted):
                    remotepeer.install_hole(self.req)
            download.assert_not_called()

    def test_hole_download_checksum_and_user_home_install_without_shell(self):
        binary = b'ELF PLACEHOLDER'
        digest = hashlib.sha256(binary).hexdigest()
        prefix = 'https://github.com/lukashornych/hole/releases/download/v2.4.0/'
        release = {'assets': [{'name': name, 'browser_download_url': prefix + name}
                             for name in ('hole_linux_amd64', 'checksums.txt')]}
        def download(url, limit):
            if url.endswith('latest'):
                return json.dumps(release).encode()
            return (digest + '  hole_linux_amd64\n').encode() if url.endswith('checksums.txt') else binary
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/docker'), \
                mock.patch.object(remotepeer, '_ok', return_value=True), \
                mock.patch.object(remotepeer, '_download', side_effect=download), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0, stdout=b'')) as run:
            reply = remotepeer.install_hole(self.req)
        self.assertEqual(reply, {'installed': True})
        self.assertEqual((self.home / '.local/bin/hole').read_bytes(), binary)
        self.assertTrue(all(c.args[0][0] not in ('sh', 'bash', 'sudo') for c in run.call_args_list))

    def test_hole_bad_checksum_never_publishes_or_executes_binary(self):
        prefix = 'https://github.com/lukashornych/hole/releases/download/v2.4.0/'
        release = {'assets': [{'name': n, 'browser_download_url': prefix + n}
                             for n in ('hole_linux_amd64', 'checksums.txt')]}
        def download(url, limit):
            return json.dumps(release).encode() if url.endswith('latest') else b'bad checksum/binary'
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/docker'), \
                mock.patch.object(remotepeer, '_ok', return_value=True), \
                mock.patch.object(remotepeer, '_download', side_effect=download), \
                mock.patch.object(remotepeer, '_setup_run') as run:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                remotepeer.install_hole(self.req)
            run.assert_not_called()
        self.assertFalse((self.home / '.local/bin/hole').exists())

    def test_extension_symlink_archive_and_unsupported_platform_are_refused(self):
        req = self.extension_req()
        with mock.patch.object(remotepeer.sys, 'platform', 'freebsd'), self.assertRaises(remotepeer.NotExecuted):
            remotepeer.sync_extensions(req)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            member = tarfile.TarInfo('acme/link.py')
            member.type, member.linkname = tarfile.SYMTYPE, '/etc/passwd'
            tar.addfile(member)
        raw = stream.getvalue()
        req.update(archive=base64.b64encode(raw).decode(), sha=hashlib.sha256(raw).hexdigest())
        self.ext_sha = req['sha']
        with self.service(), mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, '_setup_run') as run:
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'Unsafe'):
                remotepeer.sync_extensions(req)
            run.assert_not_called()

    def test_shell_entrypoint_and_public_key_symlink_are_refused(self):
        script = self.home / 'shell-tool'
        script.write_text('#!/bin/sh\nexit 0\n')
        script.chmod(0o700)
        with mock.patch.object(remotepeer.subprocess, 'Popen') as run, self.assertRaises(remotepeer.NotExecuted):
            remotepeer.run_requirement({'argv': [str(script)]})
        run.assert_not_called()
        self.key()
        public = (self.home / '.ssh' / remotepeer.SETUP_KEY).with_suffix('.pub')
        public.unlink()
        public.symlink_to(self.home / '.ssh' / remotepeer.SETUP_KEY)
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'symlinks'):
            remotepeer.setup_git_key(self.req)

    def test_service_setup_can_create_only_missing_home_projects_root_after_preflight(self):
        request = dict(self.req, setup=True, **archive({'piper/piperd.py': 'pass\n'}))
        root = Path(request['project_path'])
        def rebind(expected):
            self.assertEqual(expected, str(root))
            self.assertFalse(root.exists())
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, 'run', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(remotepeer, 'check_service_rebind', side_effect=rebind), \
                mock.patch.object(remotepeer, 'probe', return_value={'linger': True}):
            reply = remotepeer.install(request)
        self.assertTrue(reply['installed'])
        self.assertTrue(root.is_dir())
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/systemctl'), \
                mock.patch.object(remotepeer, 'run', return_value=mock.Mock(returncode=0)):
            with self.assertRaises(remotepeer.NotExecuted):
                remotepeer.install(dict(request, project_path='/etc/piper-setup-test-projects'))

    def test_current_hole_is_kept_and_never_starts_an_agent_or_builds(self):
        binary = self.home / '.local/bin/hole'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'PLACEHOLDER')
        with mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer.shutil, 'which', return_value='/bin/docker'), \
                mock.patch.object(remotepeer, '_ok', return_value=True), \
                mock.patch.object(remotepeer, '_setup_run', return_value=mock.Mock(returncode=0, stdout=b'hole 2.4.0')) as run, \
                mock.patch.object(remotepeer, '_download') as download:
            self.assertEqual(remotepeer.install_hole(self.req), {'installed': True})
        download.assert_not_called()
        self.assertEqual([c.args[0][1:] for c in run.call_args_list], [['version']])

