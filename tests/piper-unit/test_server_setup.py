"""Server setup contracts and write-ahead recovery. Synthetic peers only; no network/services."""
import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

import _env
import common
import extensions
import extfind
import projects
import remotes
import remotepeer
import server_setup

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
                           home='/home/server', platform='linux', python=[3, 12, 0], projects=[])
        patch = mock.patch.object(remotes, 'bundle', return_value=(b'app', 'bundle'))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(remotes, 'extension_bundle', return_value=(b'ext', 'extensions', []))
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(extensions, 'server_requirements', return_value=[])
        patch.start(); self.addCleanup(patch.stop)
        self.calls = []
        def peer(eid, op, body=None):
            self.calls.append((op, copy.deepcopy(body)))
            if op == 'setup_status':
                return copy.deepcopy(self.status)
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

    def test_repeated_pass_checks_but_does_not_repeat_satisfied_mutations(self):
        for _ in range(2):
            self.setup.start('dev', {'projects': []})
            self.wait()
        self.assertEqual({op for op, _ in self.calls}, {'setup_status'})

    def test_duplicate_post_does_not_create_worker_or_change_selection(self):
        with mock.patch.object(self.setup, '_spawn') as spawn:
            self.setup.threads['dev'] = mock.Mock(is_alive=lambda: True)
            self.setup.start('dev', {'projects': []})
            spawn.assert_not_called()

    def test_fix_runs_only_selected_step_and_invalid_requests_never_dispatch(self):
        for key in ('configuration', 'secrets', 'phone', 'missing'):
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
        for step in ('service', 'hole', 'git', 'extensions', 'tools', 'projects'):
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
        access = {'git@example.com:acme/shop.git': False}
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
            access['git@example.com:acme/shop.git'] = True
            self.setup.start('dev', {}, 'git')
            row = self.row(self.wait(), 'git')
        self.assertEqual(row['state'], 'ok')
        self.assertNotIn('action', row)
        self.assertNotIn('git_access', self.setup._load('dev')['operations'])  # an idempotent check, not a write

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

    def test_malformed_duplicate_secret_value_shell_and_sudo_are_refused(self):
        valid = self.row()
        cases = [None, {}, [dict(valid, id='../tool')], [valid, valid], [dict(valid, check='compiler --version')],
                 [dict(valid, install=['sudo', 'install'])], [dict(valid, check=['bash', '-c', 'true'])],
                 [dict(valid, secrets=[dict(file='.env', key='KEY', label='Key', value='fixture')])],
                 [dict(valid, secrets=[dict(file='/etc/tool', key='KEY', label='Key')])],
                 [dict(valid, secrets=[dict(file='../tool', key='KEY', label='Key')])],
                 [dict(valid, install=['tool', '--token', 'fixture-private-value'])]]
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

    def key(self):
        path = self.home / '.ssh' / remotepeer.SETUP_KEY
        path.parent.mkdir()
        path.write_text('PRIVATE PLACEHOLDER')
        path.with_suffix('.pub').write_text(PUBLIC)

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
        self.assertEqual((first, second['host_keys_added']), ({'access': {url: True}, 'host_keys_added': 1}, 0))
        self.assertEqual(known.read_text().splitlines(), ['other.example.com ssh-ed25519 ' + 'C' * 68, HOST_KEY])
        argv, env = run.call_args.args[0], run.call_args.kwargs['extra_env']
        self.assertEqual(argv[-4:], ['ls-remote', '--heads', '--', url])
        self.assertTrue(Path(env['GIT_SSH']).read_text().startswith('#!'))
        self.assertIn('-oStrictHostKeyChecking=yes', Path(env['GIT_SSH']).read_text())

    def test_git_access_refuses_bad_host_keys_urls_and_missing_key_before_writing(self):
        url = 'git@example.com:acme/shop.git'
        for body in (dict(known_hosts=['example.com ssh-ed25519 short']), dict(known_hosts=[HOST_KEY + '\nevil']),
                     dict(known_hosts=['@cert-authority * ssh-ed25519 ' + 'B' * 68]), dict(urls=['https://example.com/a.git']),
                     dict(urls=['ext::sh -c id']), dict(urls=[url])):
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
        with mock.patch.object(remotepeer.sys, 'platform', 'darwin'), self.assertRaises(remotepeer.NotExecuted):
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

