"""S2 through a real in-process bare peer, with synthetic homes and no network/commands."""
import base64
import copy
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import tarfile
import tempfile
import threading
import unittest
from unittest import mock

import _env
import common
import extensions
import remotepeer
import remotes
import server_config
import server_setup


class ConfigSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.local, self.remote = self.root / 'laptop', self.root / 'server'
        self.local.mkdir(); self.remote.mkdir()
        self.state = self.remote / '.piper-server'
        self.local_state = self.local / '.piper'
        self.project = self.local / 'projects/acme/shop'
        self.remote_projects = self.remote / 'projects'
        self.target = self.remote / 'projects/acme/shop'
        self.project.mkdir(parents=True); self.target.mkdir(parents=True)
        patches = [mock.patch.multiple(common, REAL_HOME=str(self.local), PIPER_HOME=str(self.local_state)),
                   mock.patch.dict(os.environ, PROJECT_PATH=str(self.local / 'projects')),
                   mock.patch.multiple(remotepeer, HOME=self.remote, STATE=self.state),
                   mock.patch.object(remotes, 'get', side_effect=lambda eid: dict(id='dev', mode='service',
                                     project_path=str(self.remote_projects), host='dev.example.com')),
                   # The live service's projects folder is the one each request names (no service runs here).
                   mock.patch.object(remotepeer, 'require_service_root'),
                   mock.patch.object(extensions, 'server_requirements', return_value=[]),
                   mock.patch.object(remotepeer.subprocess, 'run', side_effect=AssertionError('No commands')),
                   mock.patch.object(remotepeer.subprocess, 'Popen', side_effect=AssertionError('No commands'))]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.setup = server_setup.Setup(home=self.local_state)
        record = self.setup._load('dev')
        record['targets'] = [dict(key='acme/shop', local_path=str(self.project), relative_path='acme/shop',
                                  url='git@example.com:acme/shop.git')]
        record['path_map'] = [dict(project='acme/shop', local_path=str(self.project), remote_path=str(self.target))]
        self.status = dict(home=str(self.remote), state=str(self.state), platform='linux',
                           requested_project_root=str(self.remote / 'projects'))
        self.calls = []
        def peer(eid, op, body=None):
            self.calls.append(op)  # never capture bodies that could hold secrets
            if op == 'setup_status':
                return copy.deepcopy(self.status)
            try:
                return getattr(remotepeer, op)(dict(body or {}, project_path=str(self.remote_projects)))
            except remotepeer.NotExecuted as error:
                raise remotes.Rejected(str(error)) from None
        patch = mock.patch.object(remotes, 'setup_request', side_effect=peer)
        patch.start(); self.addCleanup(patch.stop)

    def put(self, relative, content='plain\n', *, remote=False):
        path = (self.remote if remote else self.local) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def plan(self):
        return self.setup.configuration_plan('dev', {})

    def apply(self, plan):
        return self.setup.configuration_apply('dev', {'plan_id': plan['plan_id']})

    def test_mirror_add_change_remove_edit_detection_and_confirmation(self):
        source = self.put('.claude/CLAUDE.md', 'first')
        plan = self.plan()
        dest = self.remote / '.claude/CLAUDE.md'
        self.assertIn(str(dest), plan['added'])
        self.assertFalse(dest.exists())
        self.assertEqual(self.apply(plan), dict(applied=True, plan_id=plan['plan_id']))
        self.assertEqual(dest.read_text(), 'first')
        dest.write_text('server edit')
        source.write_text('laptop wins')
        obsolete = self.put('.claude/agents/old.md', remote=True)
        plan = self.plan()
        self.assertIn(str(dest), plan['changed'])
        self.assertIn(str(dest), plan['edited_on_server'])
        self.assertIn(str(obsolete), plan['removed_on_server'])
        self.assertEqual(dest.read_text(), 'server edit')
        self.apply(plan)
        self.assertEqual(dest.read_text(), 'laptop wins')
        self.assertFalse(obsolete.exists())
        self.assertEqual(self.plan()['changed'], [])
        source.unlink()
        self.assertIn(str(dest), self.plan()['removed_on_server'])

    def test_both_local_and_remote_stale_plans_refuse_without_writes(self):
        source = self.put('.codex/AGENTS.md', 'first')
        plan = self.plan()
        source.write_text('changed locally')
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.apply(plan)
        self.assertNotIn('configuration_apply', self.calls)
        plan = self.plan()
        self.put('.codex/rules/new.rules', remote=True)
        with self.assertRaisesRegex(remotes.Rejected, 'stale'):
            self.apply(plan)
        self.assertFalse((self.remote / '.codex/AGENTS.md').exists())
        fresh = self.plan()
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.apply(plan)
        self.apply(fresh)
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.apply(fresh)

    def test_exclusions_scan_rules_and_unsafe_files_preserved_on_server(self):
        dev = 'projects/acme/shop/development/'
        unsafe = ['.env', '.ENV.local', '.env.example', 'http-client.private.env.json',
                  'values.env.json', 'tls.pem', 'tls.key', 'tls.p12', 'id_ed25519',
                  'AccessToken.txt', 'SECRET.txt', 'credentials.json', 'PASSWORD.md',
                  'backup.sql.gz', 'database.dump']
        for name in unsafe:
            self.put(dev + name, '{"dev":{"KEY":"fixture"}}')
        self.put(dev + 'empty.env.json', '{"dev":{"KEY":""}}')
        self.put(dev + 'plan.md', 'def login(token, password=None): pass\n')
        planted = 'glpat-' + 'a' * 20
        self.put(dev + 'request.http', 'Authorization: Bearer ' + planted)
        self.put(dev + 'opaque.bin', '\x00' + planted)
        huge = self.project / 'development/large.bin'
        with huge.open('wb') as out:
            out.truncate(server_config.MAX_FILE + 1)
        (self.project / 'development/outside').symlink_to(self.local_state, target_is_directory=True)
        keep = self.put(dev + 'request.http', 'keep remotely', remote=True)
        self.put('.claude/.credentials.json', planted)
        self.put('.codex/auth.json', planted)
        self.put('.claude/settings.local.json', planted)
        self.put('.claude/plugins/private/plugin.py', planted)
        plan = self.plan()
        excluded = {Path(x['path']).name: x['reason'] for x in plan['left_out']}
        for name in unsafe + ['outside', 'request.http']:
            self.assertIn(name, excluded)
        # Binary and oversized development files are never copied; one summary row counts them.
        self.assertTrue(excluded['development'].startswith('2 files or folders not copied'))
        self.assertIn('forge or chat token', excluded['request.http'])
        self.assertNotIn(planted, json.dumps(plan))
        self.assertNotIn(str(keep), plan['removed_on_server'])
        self.apply(plan)
        self.assertEqual(keep.read_text(), 'keep remotely')
        for name in unsafe:
            self.assertFalse((self.target / 'development' / name).exists())
        self.assertTrue((self.target / 'development/plan.md').exists())
        self.assertTrue((self.target / 'development/empty.env.json').exists())
        self.assertFalse((self.remote / '.codex/auth.json').exists())

    def test_project_paths_policy_filter_memory_references_and_safe_seed_links(self):
        self.put('.claude/settings.json', json.dumps({'hooks': {'run': str(self.local / '.claude/bin/check.py')}}))
        self.put('.claude/bin/check.py', 'print("check")')
        self.put('.agents/skills/test/SKILL.md', 'Agent skill')
        (self.local / '.codex/skills').mkdir(parents=True)
        (self.local / '.codex/skills/test').symlink_to(self.local / '.agents/skills/test', target_is_directory=True)
        self.put('.codex/config.toml', 'path = "' + str(self.project) + '"\n')
        memory_key = lambda p: re.sub(r'[^A-Za-z0-9]', '-', str(p))
        self.put('.claude/projects/' + memory_key(self.project) + '/memory/MEMORY.md', 'Project memory')
        document = dict(security={'guard': True}, auto_approve={'read': True},
                        secrets={'X': 'fixture'}, providers=[{'url': 'https://example.com'}], pairing={'id': 'fixture'},
                        jobs={'scheduled': True}, nested={'token': 'fixture', 'safe': True})
        self.put('.piper/policy.json', json.dumps(document))
        self.put('.piper/projects.json', json.dumps(dict(projects={'acme/shop': dict(path=str(self.project),
                         providers={'x': 'fixture'}, sandbox_env={'X': 'fixture'}, obs='acme')},
                         globs=[str(self.local / 'projects') + '/*'], jobs={'x': True})))
        plan = self.plan()
        self.apply(plan)
        policy = json.loads((self.state / 'policy.json').read_text())
        self.assertEqual(policy, dict(security={'guard': True}, auto_approve={'read': True}, nested={'safe': True}))
        data = json.loads((self.state / 'projects.json').read_text())
        self.assertEqual(data['projects']['acme/shop'], dict(path=str(self.target), obs='acme'))
        self.assertEqual(data['globs'], [str(self.remote / 'projects') + '/*'])
        self.assertIn(str(self.remote / '.claude/bin/check.py'), (self.remote / '.claude/settings.json').read_text())
        self.assertIn(str(self.target), (self.remote / '.codex/config.toml').read_text())
        self.assertTrue((self.remote / '.claude/projects' / memory_key(self.target) / 'memory/MEMORY.md').exists())
        self.assertEqual((self.remote / '.codex/skills/test/SKILL.md').read_text(), 'Agent skill')
        self.assertEqual(server_config.rewrite('/tmp/shop /tmp/shopping', [('/tmp/shop', '/srv/shop')]), '/srv/shop /tmp/shopping')

    def requirement(self):
        rows = [dict(id='build-settings', title='Build settings', detail='Template',
                     secrets=[dict(file='~/.m2/settings.xml', key=key, label=key) for key in ('USER', 'PASSWORD')])]
        extensions.server_requirements.return_value = rows
        return rows

    def test_secret_placeholder_env_states_rotation_and_values_never_escape(self):
        self.requirement()
        self.put('.m2/settings.xml', '<user>{{USER}}</user><pass>{{PASSWORD}}</pass>', remote=True)
        self.put('projects/acme/shop/.env.example', 'DATABASE_PASSWORD=template-default\nexport URL=https://example.com\n')
        self.put('projects/acme/shop/.env', 'LOCAL_ONLY=never-copy')
        env = self.put('projects/acme/shop/.env', 'OTHER="keep"\nDATABASE_PASSWORD=\n', remote=True)
        response = self.setup.secrets_state('dev')
        self.assertEqual({s['state'] for s in response['secrets']}, {'unset'})
        self.assertNotIn('template-default', json.dumps(response))
        values = {s['id']: 'fixture<&"' + s['key'] for s in response['secrets'] if s['key'] != 'URL'}
        log = io.StringIO()
        handler = logging.StreamHandler(log)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        reply = self.setup.secrets_write('dev', {'values': values})
        self.assertEqual(sum(s['state'] == 'set' for s in reply['secrets']), 3)
        self.assertIn('OTHER="keep"', env.read_text())
        self.assertNotIn('LOCAL_ONLY', env.read_text())
        xml = self.remote / '.m2/settings.xml'
        self.assertIn('&lt;&amp;&quot;', xml.read_text())
        self.assertEqual(xml.stat().st_mode & 0o777, 0o600)
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        password = next(s['id'] for s in reply['secrets'] if s['key'] == 'PASSWORD')
        reply = self.setup.secrets_write('dev', {'values': {password: 'rotated-fixture'}})
        self.assertIn('rotated-fixture', xml.read_text())
        saved = self.setup._path('dev').read_text()
        public = json.dumps(reply) + json.dumps(self.setup.state('dev')) + saved + log.getvalue()
        for value in list(values.values()) + ['rotated-fixture', 'template-default', 'LOCAL_ONLY']:
            self.assertNotIn(value, public)
        # The server's layout stores offsets only, no second copy of the values.
        self.assertNotIn('rotated-fixture', (self.state / 'setup-secret-layout.json').read_text())

    def test_secrets_copy_from_the_matching_local_env_without_reaching_the_response(self):
        self.put('projects/acme/shop/.env.example', 'DATABASE_PASSWORD=\nAPI_URL=\nMISSING=\n')
        self.put('projects/acme/shop/.env', 'export DATABASE_PASSWORD="fixture-local-pass"\nAPI_URL=https://example.com # note\nMISSING=\n')
        env = self.put('projects/acme/shop/.env', 'OTHER=keep\n', remote=True)
        rows = {s['key']: s for s in self.setup.secrets_state('dev')['secrets']}
        self.assertEqual({k: s['local'] for k, s in rows.items()}, {'DATABASE_PASSWORD': True, 'API_URL': True, 'MISSING': False})
        reply = self.setup.secrets_write('dev', {'values': {}, 'local': [rows['DATABASE_PASSWORD']['id'], rows['API_URL']['id']]})
        text = env.read_text()
        self.assertIn('DATABASE_PASSWORD="fixture-local-pass"', text)
        self.assertIn('API_URL="https://example.com"', text)
        self.assertIn('OTHER=keep', text)
        public = json.dumps(reply) + json.dumps(self.setup.state('dev')) + self.setup._path('dev').read_text()
        self.assertNotIn('fixture-local-pass', public)
        for body in ({'values': {}, 'local': [rows['MISSING']['id']]}, {'values': {}, 'local': []},
                     {'values': {rows['API_URL']['id']: 'x'}, 'local': [rows['API_URL']['id']]}):
            with self.assertRaises(ValueError):
                self.setup.secrets_write('dev', body)

    def test_env_template_without_a_local_env_is_optional_and_does_not_block_the_step(self):
        self.put('projects/acme/shop/backend/.env.example', 'PRODUCTION_PASSWORD=\n')
        rows = self.setup.secrets_state('dev')['secrets']
        self.assertEqual([(s['key'], s['optional'], s['local']) for s in rows], [('PRODUCTION_PASSWORD', True, False)])
        step = next(s for s in self.setup.state('dev')['steps'] if s['id'] == 'secrets')
        self.assertEqual(step['state'], 'ok')
        self.assertIn('1 values from .env templates are unset on this computer too', step['detail'])
        self.put('projects/acme/shop/backend/.env', 'OTHER=1\n')
        rows = self.setup.secrets_state('dev')['secrets']
        self.assertFalse(rows[0]['optional'], 'a local .env without the key still asks for it')
        step = next(s for s in self.setup.state('dev')['steps'] if s['id'] == 'secrets')
        self.assertEqual(step['state'], 'needs_user')

    def test_secret_bad_values_paths_and_lost_answers_are_safe(self):
        self.requirement()
        self.put('.m2/settings.xml', '{{USER}} {{PASSWORD}}', remote=True)
        names = self.setup.secrets_state('dev')['secrets']
        sid = names[0]['id']
        for value in ('', 'line\ninjection', 'bad\x00value', 123):
            with self.assertRaises(ValueError) as raised:
                self.setup.secrets_write('dev', {'values': {sid: value}})
            self.assertNotIn('injection', str(raised.exception))
        for file in ('/etc/settings.xml', '../escape', str(self.remote / '../escape')):
            req = dict(secrets=[dict(id=sid, file=file, key='USER')], values={sid: 'fixture-private-value'})
            with self.assertRaises(remotepeer.NotExecuted) as raised:
                remotepeer.secrets_write(req)
            self.assertNotIn('fixture-private-value', str(raised.exception))
        (self.remote / 'escape').symlink_to(self.local, target_is_directory=True)
        with self.assertRaises(remotepeer.NotExecuted):
            remotepeer.secrets_write(dict(secrets=[dict(id=sid, file=str(self.remote / 'escape/x.env'), key='USER')], values={sid: 'fixture'}))
        with mock.patch.object(remotes, 'setup_request', side_effect=lambda eid, op, body=None:
                self.status if op == 'setup_status' else (_ for _ in ()).throw(remotes.Unavailable('fixture-private-value'))):
            with self.assertRaisesRegex(server_setup.UnknownOutcome, 'outcome is unknown'):
                self.setup.secrets_write('dev', {'values': {sid: 'fixture-private-value'}})
        self.assertEqual(self.setup.state('dev')['phase'], 'remote_unknown')
        self.assertNotIn('fixture-private-value', self.setup._path('dev').read_text())
        with self.assertRaises(ValueError):
            self.setup.secrets_write('dev', {'values': {sid: 'fixture-private-value'}})

    def test_nested_env_template_creates_only_entered_keys_and_placeholder_drift_refuses(self):
        self.put('projects/acme/shop/service/.env.example', 'FIRST=never-copy-default\nSECOND=\n')
        rows = self.setup.secrets_state('dev')['secrets']
        first = next(s['id'] for s in rows if s['key'] == 'FIRST')
        reply = self.setup.secrets_write('dev', {'values': {first: 'one $value `quoted`'}})
        target = self.target / 'service/.env'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('never-copy-default', target.read_text())
        self.assertNotIn('SECOND=', target.read_text())
        self.assertEqual({s['key']: s['state'] for s in reply['secrets']}, dict(FIRST='set', SECOND='unset'))
        self.setup.secrets_write('dev', {'values': {first: 'replacement'}})
        self.assertEqual(target.read_text().count('FIRST='), 1)
        self.requirement()
        xml = self.put('.m2/settings.xml', '{{USER}} {{PASSWORD}} {{USER}}', remote=True)
        rows = self.setup.secrets_state('dev')['secrets']
        user = next(s['id'] for s in rows if s['key'] == 'USER')
        self.setup.secrets_write('dev', {'values': {user: 'fixture-user'}})
        self.assertEqual(xml.read_text().count('fixture-user'), 2)
        xml.write_text(xml.read_text() + ' manual edit')
        rows = self.setup.secrets_state('dev')['secrets']
        self.assertEqual(next(s['state'] for s in rows if s['id'] == user), 'unset')
        before = xml.read_text()
        with self.assertRaises(ValueError):
            self.setup.secrets_write('dev', {'values': {user: 'second-user'}})
        self.assertEqual(xml.read_text(), before)

    def test_remote_secret_environment_json_and_links_are_never_removed(self):
        env = self.put('projects/acme/shop/development/private.env.json', '{"dev":{"VALUE":"fixture"}}', remote=True)
        link = self.target / 'development/link'
        link.symlink_to(self.local, target_is_directory=True)
        plan = self.plan()
        self.assertIn(str(env), [x['path'] for x in plan['left_out']])
        self.assertIn(str(link), [x['path'] for x in plan['left_out']])
        self.assertNotIn(str(env), plan['removed_on_server'])
        self.apply(plan)
        self.assertTrue(env.exists())
        self.assertTrue(link.is_symlink())

    def test_malformed_archive_scope_checksum_and_hashes_never_write(self):
        source = self.put('.claude/CLAUDE.md', 'first')
        plan = self.plan()
        record = self.setup._load('dev')['configuration_plan']
        files, scopes, _ = server_config.snapshot(self.setup._load('dev'), self.status)
        raw, sha = server_config.archive(files, files)
        req = dict(project_path=str(self.remote / 'projects'), scopes=scopes, revision=record['revision'],
                   writes=server_config.hashes(files), removes=[], archive=base64.b64encode(raw).decode(), sha=sha)
        cases = [dict(req, sha='0' * 64), dict(req, writes={str(self.remote / '.claude/CLAUDE.md'): dict(sha256='0' * 64, mode=0o600)}),
                 dict(req, scopes=[dict(path='/etc', tree=True)]),
                 dict(req, scopes=[dict(path=str(self.remote / '.ssh'), tree=True)]),
                 dict(req, writes={str(self.remote / '.claude/auth.json'): dict(sha256='0' * 64, mode=0o600)})]
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as tar:
            member = tarfile.TarInfo('0'); member.type = tarfile.SYMTYPE; member.linkname = str(source)
            tar.addfile(member)
        bad = stream.getvalue()
        cases.append(dict(req, archive=base64.b64encode(bad).decode(), sha=hashlib.sha256(bad).hexdigest()))
        for body in cases:
            with self.subTest(fields=list(body)), self.assertRaises((remotepeer.NotExecuted, ValueError)):
                remotepeer.configuration_apply(body)
        self.assertFalse((self.remote / '.claude/CLAUDE.md').exists())

    def test_projects_folder_outside_the_account_home_mirrors_development(self):
        # Regression: a server keeping projects in /srv-like folders outside home refused every
        # configuration review ("Setup files must be inside the SSH account home").
        self.remote_projects = self.root / 'srv'
        self.target = self.remote_projects / 'acme/shop'
        self.target.mkdir(parents=True)
        record = self.setup._load('dev')
        record['path_map'] = [dict(project='acme/shop', local_path=str(self.project), remote_path=str(self.target))]
        self.status['requested_project_root'] = str(self.remote_projects)
        self.put('projects/acme/shop/development/notes.md', 'notes')
        self.put('.claude/CLAUDE.md', 'rules')
        plan = self.plan()
        self.assertIn(str(self.target / 'development/notes.md'), plan['added'])
        self.assertIn(str(self.remote / '.claude/CLAUDE.md'), plan['added'])
        self.apply(plan)
        self.assertEqual((self.target / 'development/notes.md').read_text(), 'notes')
        self.assertEqual((self.remote / '.claude/CLAUDE.md').read_text(), 'rules')
        # Project secrets live next to the mirrored project in that folder as well.
        self.put('projects/acme/shop/.env.example', 'API_KEY=\n')
        state = self.setup.secrets_state('dev')
        self.assertEqual([(s['key'], s['state']) for s in state['secrets']], [('API_KEY', 'unset')])
        self.setup.secrets_write('dev', {'values': {state['secrets'][0]['id']: 'fixture-value'}})
        self.assertIn('API_KEY="fixture-value"', (self.target / '.env').read_text())
        (self.root / 'elsewhere').mkdir()
        (self.remote_projects / 'link').symlink_to(self.root / 'elsewhere')
        base = dict(project_path=str(self.remote_projects))
        for scope in (dict(path=str(self.target / 'src'), tree=True),  # only development is mirrored
                      dict(path=str(self.remote_projects / 'link/development'), tree=True),
                      dict(path=str(self.root / 'elsewhere/development'), tree=True),
                      dict(path=str(self.remote_projects), tree=True)):
            with self.subTest(scope=scope['path']), self.assertRaises(remotepeer.NotExecuted):
                remotepeer.configuration_manifest(dict(base, scopes=[scope]))

    def test_development_mirrors_text_only_and_counts_what_it_leaves_out(self):
        # Regression: a development/ folder with gigabytes of screenshots and builds failed the whole
        # Configuration step ("Configuration snapshot exceeds the setup size limit").
        self.put('projects/acme/shop/development/plan.md', 'plan')
        self.put('projects/acme/shop/development/shot.png', 'binary')
        self.put('projects/acme/shop/development/big.md', 'x' * (server_config.DEV_FILE + 1))
        self.put('projects/acme/shop/development/build/notes.md', 'generated')
        for n in range(3):
            self.put('projects/acme/shop/development/batch/%d.md' % n, 'y' * 40)
        kept = self.put('projects/acme/shop/development/server-shot.png', 'server only', remote=True)
        stale = self.put('projects/acme/shop/development/old.md', 'gone locally', remote=True)
        reads = []
        real = server_config.common.safe_read
        def counted(root, rel, *args, **kwargs):
            reads.append(rel); return real(root, rel, *args, **kwargs)
        with mock.patch.object(server_config, 'MAX_TOTAL', 100), mock.patch.object(server_config.common, 'safe_read', counted):
            plan = self.plan()
        # Once the budget is spent the rest is counted without being read (it took minutes on a large folder).
        self.assertLess(sum('development/batch/' in r for r in reads), 3, reads)
        dev = str(self.target / 'development')
        copied = sorted(p for p in plan['added'] if p.startswith(dev))
        self.assertIn(dev + '/plan.md', copied)
        self.assertTrue(len(copied) < 4, copied)  # the size budget leaves the rest out instead of failing
        for name in ('shot.png', 'big.md', 'build/notes.md'):
            self.assertNotIn(dev + '/' + name, copied)
        summary = [x for x in plan['left_out'] if x['path'] == dev]
        self.assertEqual(len(summary), 1)
        self.assertIn('not copied', summary[0]['reason'])
        self.assertIn(str(stale), plan['removed_on_server'])
        self.assertNotIn(str(kept), plan['removed_on_server'])

    def test_step_actions_recovery_and_peer_argument_budget(self):
        self.assertEqual(self.setup.state('dev')['steps'][6]['action']['path'], '/api/environments/dev/setup/configuration/plan')
        self.assertEqual(self.setup.state('dev')['steps'][8]['action']['method'], 'GET')
        self.put('.claude/CLAUDE.md')
        plan = self.plan()
        original = remotepeer.configuration_apply
        def lose(req):
            original(req)
            raise remotes.Unavailable('answer lost')
        with mock.patch.object(remotepeer, 'configuration_apply', side_effect=lose):
            with self.assertRaises(server_setup.UnknownOutcome):
                self.apply(plan)
        self.assertEqual(self.setup.state('dev')['unknown']['step'], 'configuration')
        self.setup.start('dev', {'reconcile': True}, 'configuration')
        self.setup.threads['dev'].join(3)
        self.assertEqual(self.setup.state('dev')['phase'], 'reconciled')
        self.assertEqual(self.calls.count('configuration_apply'), 1)
        self.assertLess(len(remotes.peer_source().encode()), 100000)

    def test_interactive_ssh_does_not_block_snapshots_or_allow_concurrent_setup(self):
        entered, release, read = threading.Event(), threading.Event(), threading.Event()
        def slow():
            with self.setup._interactive('dev'):
                entered.set()
                release.wait(3)
        worker = threading.Thread(target=slow)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reader = threading.Thread(target=lambda: (self.setup.state('dev'), read.set()))
            reader.start()
            self.assertTrue(read.wait(1), 'snapshot waited for SSH')
            reader.join(1)
            with self.assertRaises(ValueError):
                self.setup.start('dev', {}, 'configuration')
            with self.assertRaises(ValueError):
                self.setup.secrets_state('dev')
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(self.setup.busy)
        self.assertEqual(server_config.filtered({'sandbox_env': {'file': '.env', 'keys': ['ACCESS_TOKEN']}}),
                         {'sandbox_env': {'file': '.env', 'keys': ['ACCESS_TOKEN']}})
