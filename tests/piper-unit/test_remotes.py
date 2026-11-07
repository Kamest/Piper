"""SSH transport boundaries, one-shot peer, service archive and service-free tmux lifecycle."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import remotes
import remotepeer


class RemoteTest(unittest.TestCase):
    def test_slow_database_reply_survives_the_peer_and_outer_ssh_budgets_without_retry(self):
        import http.client
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import time
        seen, budgets = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                seen.append(self.path)
                time.sleep(.06)  # Scaled clock: old 28-second peer deadline becomes 28 ms.
                self.send_response(200)
                self.end_headers()
                try: self.wfile.write(b'{"committed":true}')
                except BrokenPipeError: pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        home = Path(self.tmp.name)
        (home / 'gui-token').write_text('fixture-token')
        connection = http.client.HTTPConnection
        def upstream(host, port, *, timeout):
            budgets.append(timeout)
            return connection('127.0.0.1', server.server_port, timeout=timeout / 1000)
        def ssh_response(*args, input, timeout, **kwargs):
            payload = json.loads(input)
            if payload['op'] == 'probe':
                reply = {}
            else:
                reply = remotepeer._proxy_http(payload)
                self.assertGreater(timeout, budgets[-1])
            return mock.Mock(returncode=0, stdout=json.dumps(dict(reply, _peer_identity='a' * 32)).encode())
        with mock.patch.object(remotepeer, 'STATE', home), \
                mock.patch.object(remotepeer, 'HTTPConnection', side_effect=upstream), \
                mock.patch.object(remotes.subprocess, 'run', side_effect=ssh_response):
            result = remotes.proxy('dev', 'POST', '/api/db/execute', b'{}')
        self.assertEqual(json.loads(base64.b64decode(result['body'])), {'committed': True})
        self.assertEqual(seen, ['/api/db/execute'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='remote-test-', dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.object(remotes, 'FILE', os.path.join(self.tmp.name, 'environments.json'))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.entry = remotes.save(dict(id='dev', label='Development', host='dev-alias', project_path='/srv/projects', mode='service'))

    def test_service_root_mismatch_blocks_reads_and_writes_before_dispatch(self):
        for method in ('GET', 'POST'):
            req = dict(mode='service', project_path='/www/', method=method,
                       path='/api/state' if method == 'GET' else '/api/sessions', body='', ctype='application/json')
            with mock.patch.object(remotepeer, '_service_json', return_value={'project_root': '/old/projects'}), \
                    mock.patch.object(remotepeer, '_proxy_http') as dispatch, \
                    mock.patch.object(remotepeer, 'HOME', Path(self.tmp.name)):
                with self.assertRaisesRegex(ValueError, 'Projects folder differs'):
                    remotepeer.http(req)
                dispatch.assert_not_called()

    def test_editing_a_removed_server_does_not_recreate_its_connection(self):
        """A15-02: label Save from an existing editor is never an implicit Add."""
        stale = dict(self.entry, label='My unfinished name', existing=True)
        remotes.remove(self.entry['id'])
        before = Path(remotes.FILE).read_bytes()
        with self.assertRaisesRegex(ValueError, 'removed'):
            remotes.save(stale)
        self.assertEqual(remotes.entries(), [])
        self.assertEqual(Path(remotes.FILE).read_bytes(), before)

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_removed_server_save_keeps_the_draft_and_connection_removed(self):
        """A15-02: the production Servers editor and real local API, without any SSH calls."""
        import piperd
        html = b'''<!doctype html><html><head><link rel="stylesheet" href="/piper/app.css"></head>
        <body><script type="module">
        import {absorbFragmentToken} from '/piper/dist/api.js';
        import {createServerSettings} from '/piper/dist/environments.js';
        absorbFragmentToken();
        window.serversRegression=createServerSettings({changed(){},toast(){},modal(){},closeModal(){}});
        document.body.append(serversRegression.el);serversRegression.show();
        </script></body></html>'''

        class Handler(piperd.Handler):
            def do_GET(self):
                if self.path == '/server-regression':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(html)))
                    self.end_headers()
                    self.wfile.write(html)
                else:
                    super().do_GET()

        manager = _env.manager(self)
        old_manager, old_port = piperd.MANAGER, piperd.PORT
        server = piperd.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        piperd.MANAGER, piperd.PORT = manager, server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = 'piper-server-stale-%s' % os.getpid()

        def cli(*args):
            run = subprocess.run(['agent-browser', '--session', browser, '--json', *args],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token('gui'), '[token]'))
            reply = json.loads(run.stdout)
            self.assertTrue(reply['success'], reply)
            return reply.get('data', {}).get('result')

        def ready(code):
            until = time.monotonic() + 20
            while time.monotonic() < until:
                if cli('eval', code) is True:
                    return
                time.sleep(.05)
            self.fail(code)

        try:
            with mock.patch.object(remotes, 'probe', side_effect=AssertionError('No SSH probe permitted')) as probe:
                cli('open', piperd.gui_url().replace('/#', '/server-regression#'))
                ready('!!document.querySelector(".server-row")')
                cli('click', '.server-row')
                cli('fill', '#server-label', 'My unfinished name')
                self.assertTrue(cli('eval', 'serversRegression.dirty()'))
                # Another client removes the connection while this form remains open.
                cli('eval', "(async()=>{const {call}=await import('/piper/dist/api.js');await call('POST','/api/environments/dev/remove',{});return true})()")
                self.assertEqual(remotes.entries(), [])
                cli('click', '.st-save')
                ready('document.querySelector(".server-status[role=alert]")?.textContent.includes("was removed")===true')
                self.assertEqual(remotes.entries(), [])
                self.assertEqual(cli('eval', 'document.querySelector("#server-label").value'), 'My unfinished name')
                self.assertTrue(cli('eval', 'serversRegression.dirty()'))
                probe.assert_not_called()
                path = os.path.join(os.environ['TEST_SANDBOX_WORK'], 'server-audit-stale-save.png')
                cli('screenshot', path)
                print('SERVER_SCREENSHOT ' + path, flush=True)
        finally:
            subprocess.run(['agent-browser', '--session', browser, 'close'], capture_output=True, timeout=20)
            server.shutdown()
            server.server_close()
            piperd.MANAGER, piperd.PORT = old_manager, old_port

    def test_service_probe_compares_canonical_absolute_roots_without_home_prefix(self):
        home = Path(self.tmp.name)
        (home / 'state').mkdir()
        (home / 'state/gui-token').write_text('fixture-only')
        with mock.patch.multiple(remotepeer, HOME=home, STATE=home/'state'), \
                mock.patch.object(remotepeer, '_service_json', return_value={'project_root': '/www'}), \
                mock.patch.object(remotepeer, 'run', return_value=mock.Mock(stdout=b'no')):
            result = remotepeer.probe({'project_path': '/www/'})
            self.assertTrue(result['service_running'])
            self.assertTrue(result['project_root_matches'])
            self.assertEqual(result['service_project_root'], '/www')
            result = remotepeer.probe({'project_path': '/different'})
            self.assertFalse(result['project_root_matches'])

    def test_unknown_legacy_service_root_is_not_accepted(self):
        with mock.patch.object(remotepeer, '_service_json', return_value={'ok': True}):
            with self.assertRaisesRegex(ValueError, 'Update service'):
                remotepeer.require_service_root('/www')

    def test_rebind_refuses_active_sessions_and_unavailable_session_check(self):
        for sessions in ([{'alive': True, 'state': 'working'}], [{'alive': False, 'state': 'starting'}], None):
            def response(path):
                return {'project_root': '/old/projects'} if path == '/api/ping' else None if sessions is None else {'sessions': sessions}
            with mock.patch.object(remotepeer, '_service_json', side_effect=response):
                with self.assertRaisesRegex(ValueError, 'session'):
                    remotepeer.check_service_rebind('/www')
        with mock.patch.object(remotepeer, '_service_json', side_effect=lambda path: {'project_root': '/old/projects'} if path == '/api/ping' else {'sessions': []}):
            remotepeer.check_service_rebind('/www')

    def test_matching_root_can_update_with_active_sessions(self):
        with mock.patch.object(remotepeer, '_service_json', return_value={'project_root': '/www'}) as get:
            remotepeer.check_service_rebind('/www/')
            self.assertEqual(get.call_args_list, [mock.call('/api/ping')])

    def test_legacy_root_comes_from_running_process_and_never_exports_environment(self):
        response = mock.Mock(stdout=b'123\n')
        with mock.patch.object(remotepeer, '_service_json', return_value={'ok': True}), \
                mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer, 'run', return_value=response), \
                mock.patch.object(Path, 'read_bytes', return_value=b'FAKE_SECRET=do-not-export\0PROJECT_PATH=/www/\0') as read:
            info = remotepeer._service_info()
            self.assertEqual(info, {'ok': True, 'project_root': '/www'})
            read.assert_called_once()
        with mock.patch.object(remotepeer, '_service_json', return_value={'ok': True}), \
                mock.patch.object(remotepeer.sys, 'platform', 'linux'), \
                mock.patch.object(remotepeer, 'run', side_effect=[mock.Mock(stdout=b'123'), mock.Mock(stdout=b'124')]), \
                mock.patch.object(Path, 'read_bytes', return_value=b'PROJECT_PATH=/www\0'):
            self.assertNotIn('project_root', remotepeer._service_info(), 'changed process cannot prove the running root')

    def test_legacy_agents_use_service_path_and_refresh_without_environment_export(self):
        with tempfile.TemporaryDirectory() as folder:
            agent = Path(folder) / 'codex'
            with mock.patch.object(remotepeer, '_service_env', return_value=folder):
                self.assertEqual(remotepeer.service_agents(), {'claude': False, 'codex': False})
                agent.write_text('#!/bin/sh\n'); agent.chmod(0o700)
                self.assertEqual(remotepeer.service_agents(), {'claude': False, 'codex': True})
                agent.unlink()
                self.assertFalse(remotepeer.service_agents()['codex'])
        with mock.patch.object(remotepeer, '_service_env', return_value=None):
            self.assertIsNone(remotepeer.service_agents())

    def test_service_proxy_supplies_fresh_legacy_availability_but_preserves_native_metadata(self):
        home = Path(self.tmp.name); (home / 'gui-token').write_text('fixture-only')
        req = dict(method='GET',path='/api/state',body='',ctype='application/json')
        conn = mock.Mock(); response = conn.getresponse.return_value
        response.status = 200; response.getheader.return_value = 'application/json'
        for native in (None, {'claude': True, 'codex': False}):
            response.read.return_value = json.dumps({'agent_availability': native, 'features': {'modules': {'codex':False}}}).encode()
            with mock.patch.object(remotepeer, 'STATE', home), mock.patch.object(remotepeer, 'HTTPConnection', return_value=conn), mock.patch.object(remotepeer, 'service_agents', return_value={'claude':False,'codex':True}) as detect:
                result = json.loads(base64.b64decode(remotepeer._proxy_http(req)['body']))
                self.assertEqual(result['agent_availability'], native or {'claude':False,'codex':True})
                self.assertFalse(result['features']['modules']['codex'])
                self.assertEqual(detect.call_count, 0 if native else 1)

    def test_ssh_snapshot_refreshes_both_agents_without_service_or_module_changes(self):
        home = Path(self.tmp.name)
        with mock.patch.object(remotepeer, 'HOME', home), mock.patch.object(remotepeer, 'ssh_projects', return_value=[]), mock.patch.object(remotepeer.shutil, 'which') as which:
            req = dict(method='GET',path='/api/state',body='',project_path=str(home))
            for installed in ((), ('claude',), ('codex',), ('claude','codex'), ()):
                which.side_effect = lambda name: '/fixture/'+name if name in installed else None
                snapshot = json.loads(base64.b64decode(remotepeer.ssh_http(req)['body']))
                self.assertEqual(snapshot['agent_availability'], {name:name in installed for name in ('claude','codex')})

    def test_authenticated_known_rejection_is_distinct_from_unknown_outcome(self):
        entry = dict(self.entry, identity='a' * 32)
        reply = {'error': 'Server sessions are active', 'not_executed': True, '_peer_identity': entry['identity']}
        with mock.patch.object(remotes.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(reply).encode())):
            with self.assertRaises(remotes.Rejected):
                remotes.request(entry, {'op': 'install'})
        reply['_peer_identity'] = 'b' * 32
        with mock.patch.object(remotes.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(reply).encode())):
            with self.assertRaises(remotes.Unavailable):
                remotes.request(entry, {'op': 'install'})

    def test_reconfigure_waits_for_checked_request(self):
        entered, release, attempted, installed = (threading.Event() for _ in range(4))
        errors = []
        def dispatch(req):
            entered.set()
            if not release.wait(3): raise TimeoutError('fixture request not released')
            return {}
        def update():
            attempted.set()
            try: remotepeer.install({})
            except Exception as e: errors.append(e)
        with mock.patch.object(remotepeer, 'HOME', Path(self.tmp.name)), \
                mock.patch.object(remotepeer, '_service_json', return_value={'project_root': '/www'}), \
                mock.patch.object(remotepeer, '_proxy_http', side_effect=dispatch), \
                mock.patch.object(remotepeer, '_install', side_effect=lambda req: installed.set()):
            reader = threading.Thread(target=lambda: remotepeer.http({'mode': 'service', 'project_path': '/www'}))
            writer = threading.Thread(target=update)
            reader.start()
            try:
                self.assertTrue(entered.wait(2)); writer.start(); self.assertTrue(attempted.wait(2))
                self.assertFalse(installed.wait(.1), 'install cannot race a request checked under the previous root')
            finally:
                release.set(); reader.join(3)
                if writer.ident: writer.join(3)
            self.assertFalse(reader.is_alive()); self.assertFalse(writer.is_alive()); self.assertTrue(installed.is_set())
            self.assertEqual(errors, [])

    def test_two_account_installers_cannot_reconfigure_concurrently(self):
        entered, release, attempted, second = (threading.Event() for _ in range(4))
        def install(req):
            if req['first']:
                entered.set()
                if not release.wait(3): raise TimeoutError('fixture install not released')
            else: second.set()
        def next_install():
            attempted.set(); remotepeer.install({'first': False})
        with mock.patch.object(remotepeer, 'HOME', Path(self.tmp.name)), mock.patch.object(remotepeer, '_install', side_effect=install):
            first = threading.Thread(target=lambda: remotepeer.install({'first': True}))
            last = threading.Thread(target=next_install)
            first.start()
            try:
                self.assertTrue(entered.wait(2)); last.start(); self.assertTrue(attempted.wait(2))
                self.assertFalse(second.wait(.1), 'account-wide installs must be exclusive')
            finally:
                release.set(); first.join(3)
                if last.ident: last.join(3)
            self.assertFalse(first.is_alive()); self.assertFalse(last.is_alive()); self.assertTrue(second.is_set())

    def test_registry_validates_shell_boundaries_and_keeps_private_file(self):
        for host in ('-oProxyCommand=x', 'host;touch x', 'a\nb', 'a b', '$(id)', ''):
            with self.assertRaises(ValueError):
                remotes.save(dict(self.entry, host=host))
        for eid in ('local', '../x', 'a/b', ''):
            with self.assertRaises(ValueError):
                remotes.save(dict(self.entry, id=eid))
        self.assertEqual(os.stat(remotes.FILE).st_mode & 0o777, 0o600)
        remotes.remove('dev')
        self.assertEqual(remotes.entries(), [])

    def test_ssh_verifies_known_hosts_and_has_no_secret_on_command_line(self):
        argv = remotes.ssh(self.entry, ['python3','-c', 'print("hello")'])
        self.assertIn('-oBatchMode=yes', argv)
        self.assertIn('--', argv)
        self.assertNotIn('StrictHostKeyChecking=no', ' '.join(argv))
        self.assertEqual(__import__('shlex').split(argv[-1]), ['python3','-c','print("hello")'])

    def test_environment_identity_cannot_redirect_existing_tabs_even_after_removal(self):
        for field, value in (('host','another-server'),('project_path','/different'),('mode','ssh')):
            with self.assertRaisesRegex(ValueError,'new id'):
                remotes.save(dict(self.entry,**{field:value}))
        remotes.remove('dev')
        with self.assertRaisesRegex(ValueError,'new id'):
            remotes.save(dict(self.entry,host='another-server'))
        self.assertEqual(remotes.save(dict(self.entry,label='Renamed'))['host'],'dev-alias')

    def test_authenticated_peer_identity_is_pinned_and_survives_registry_removal(self):
        def reply(identity):
            return mock.Mock(returncode=0,stdout=json.dumps({'_peer_identity':identity}).encode())
        with mock.patch.object(remotes.subprocess,'run',return_value=reply('a'*32)):
            remotes.probe('dev')
        self.assertEqual(remotes.get('dev')['identity'],'a'*32)
        remotes.remove('dev'); remotes.save(self.entry)
        self.assertEqual(remotes.get('dev')['identity'],'a'*32)
        with mock.patch.object(remotes.subprocess,'run',return_value=reply('b'*32)):
            with self.assertRaisesRegex(remotes.Unavailable,'identity changed'):
                remotes.probe('dev')

    def test_peer_rejects_changed_target_before_dispatching_a_write(self):
        env=dict(os.environ,HOME=self.tmp.name)
        def call(body):
            result=subprocess.run([sys.executable,'-c',remotes.peer_source()],input=json.dumps(body).encode(),capture_output=True,env=env,timeout=5)
            return json.loads(result.stdout)
        first=call({'op':'probe'})
        self.assertEqual(first['_peer_identity'],call({'op':'probe'})['_peer_identity'])
        result=call({'op':'install','expected_identity':'0'*32})
        self.assertIn('identity changed',result['error'])
        self.assertFalse((Path(self.tmp.name)/'.local/share/piper-server').exists())

    def test_lost_write_is_sent_exactly_once(self):
        with mock.patch.object(remotes.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)) as run:
            with self.assertRaisesRegex(remotes.Unavailable, 'not retried'):
                remotes.proxy('dev','POST','/api/git/push',b'{}')
        self.assertEqual(run.call_count, 1)
        for path in ('http://elsewhere/api/state', '/api/environments', '/etc/passwd'):
            with self.assertRaises(ValueError):
                remotes.proxy('dev','GET',path)

    def test_invalid_peer_reply_does_not_leak_stderr(self):
        with mock.patch.object(remotes.subprocess, 'run', return_value=mock.Mock(returncode=255, stderr=b'fake-secret', stdout=b'')):
            with self.assertRaises(remotes.Unavailable) as e:
                remotes.probe('dev')
        self.assertNotIn('fake-secret', str(e.exception))

    def test_bundle_contains_runtime_but_no_personal_state_or_design(self):
        raw, sha = remotes.bundle()
        self.assertEqual(__import__('hashlib').sha256(raw).hexdigest(), sha)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:
            names = tf.getnames()
        self.assertIn('piper/piperd.py',names)
        self.assertIn('piper/directruntime.py',names)
        self.assertIn('piper/mobile.py', names)
        self.assertIn('piper/mobilepush.py', names)
        self.assertIn('piper/mobile-requirements.txt', names)
        self.assertFalse(any('mobile-venv/' in name for name in names))
        self.assertIn('piper/advisor/CLAUDE.md',names)
        self.assertIn('scripts/piper.sh',names)
        helpers = {path.name for path in (Path(common.SK) / 'piper/httpcompat').glob('Piper*.class')}
        self.assertTrue(helpers)
        self.assertEqual({name.removeprefix('piper/httpcompat/') for name in names
                          if name.startswith('piper/httpcompat/')}, helpers)
        self.assertFalse(any('/design/' in p or '.git/' in p or '__pycache__' in p or p.endswith('.pen') for p in names))
        self.assertFalse(any(p.startswith('config/') for p in names))

    def test_native_http_overlay_can_be_built_from_the_actual_extracted_server_release(self):
        import httpcompat
        import zipfile
        binary = os.environ.get('PIPER_IJHTTP')
        if not binary:
            self.skipTest('requires the configured native CLI')
        release = Path(self.tmp.name) / 'release'
        raw, _ = remotes.bundle()
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for member in archive.getmembers():
                destination = release / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.extractfile(member).read())
        with mock.patch.object(httpcompat, '__file__', str(release / 'piper/httpcompat.py')):
            overlay = httpcompat.overlay(os.path.join(os.path.dirname(os.path.realpath(binary)), 'lib'))
        self.assertIsNotNone(overlay, 'the verification uses the supported native CLI revision')
        with zipfile.ZipFile(io.BytesIO(overlay)) as jar:
            self.assertTrue(all('piper/bridge/' + name in jar.namelist() for name in httpcompat.HELPER_NAMES))

    def test_bundle_uses_runtime_allowlist_and_omits_env_config_and_source_drafts(self):
        root=Path(self.tmp.name)/'package'
        files={'piper/main.py':'pass','piper/.env':'PRIVATE_PLACEHOLDER','piper/settings.json':'PRIVATE_PLACEHOLDER','piper/advisor/CLAUDE.md':'# Global agent template','piper/advisor/private.md':'PRIVATE_PLACEHOLDER','piper/httpcompat/PiperBinary.class':'runtime helper','piper/httpcompat/PiperBinary.java':'source draft','piper/httpcompat/private.class':'PRIVATE_PLACEHOLDER','piper/web/dist/main.js':'export {};','piper/web/src/draft.ts':'draft','piper/web/private.json':'PRIVATE_PLACEHOLDER','scripts/run.sh':'true','scripts/.private.sh':'PRIVATE_PLACEHOLDER','piper/web/themes.json':'{}','dashboard/themes.json':'{}'}
        for name,value in files.items():
            path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
        # Even Python files inside an accidentally colocated optional runtime must not ship.
        runtime = root / 'piper/mobile-venv/lib/python3/site-packages/private.py'
        runtime.parent.mkdir(parents=True)
        runtime.write_text('PRIVATE_PLACEHOLDER')
        with mock.patch.object(common,'SK',str(root)):
            raw,_=remotes.bundle()
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
            self.assertEqual(set(archive.getnames()),{'piper/main.py','piper/advisor/CLAUDE.md','piper/httpcompat/PiperBinary.class','piper/web/dist/main.js','scripts/run.sh','piper/web/themes.json'})

    def test_one_shot_peer_can_probe_without_installing_piper(self):
        env = dict(os.environ, HOME=self.tmp.name)
        p = subprocess.run([sys.executable,'-c',remotes.peer_source()],input=b'{"op":"probe"}',stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=15)
        self.assertEqual(p.returncode,0,p.stderr.decode())
        data = json.loads(p.stdout)
        self.assertFalse(data['service_installed'])
        self.assertEqual(data['protocol'],1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name,'.local/share/piper-server')))

    def test_terminal_recovery_attaches_same_remote_session_without_starting_an_agent(self):
        import ptyclient
        import shlex
        calls, rows = [], []
        class Client:
            def call(self, op, **kwargs):
                calls.append((op, kwargs))
                return {"sessions": list(rows)} if op == "list" else {"ok": True}
            def close(self): pass
        entry = {"id": "synthetic", "host": "ssh.example.com", "identity": "a" * 32, "mode": "ssh"}
        with mock.patch.object(remotes, "get", return_value=entry), mock.patch.object(ptyclient, "connect", side_effect=lambda _: Client()):
            first = remotes.terminal("synthetic", "existing-session", lambda _: None)
            spawn = [args for op, args in calls if op == "spawn"]
            self.assertEqual(len(spawn), 1)
            self.assertEqual(shlex.split(spawn[0]["argv"][-1])[-2:], ["attach", "existing-session"])
            rows[:] = [{"id": first.remote_id, "alive": True}]
            remotes.terminal("synthetic", "existing-session", lambda _: None)
            self.assertEqual(len([op for op, _ in calls if op == "spawn"]), 1)
            rows[0]["alive"] = False
            recovered = remotes.terminal("synthetic", "existing-session", lambda _: None)
            spawn = [args for op, args in calls if op == "spawn"]
            self.assertEqual(len(spawn), 2)
            self.assertEqual(recovered.remote_id, first.remote_id)
            self.assertEqual(shlex.split(spawn[1]["argv"][-1])[-2:], ["attach", "existing-session"])

    def test_attach_resolves_tmux_from_server_local_bin_without_a_service(self):
        home=Path(self.tmp.name)
        binary=home/'.local/bin/tmux'; binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n'); binary.chmod(0o700)
        bare=home/'bare'; bare.mkdir()   # a PATH without tmux: a runner's /usr/bin/tmux would win over ~/.local/bin
        result=subprocess.run([sys.executable,'-c',remotes.peer_source(),'attach','0123456789abcdef'],env=dict(os.environ,HOME=str(home),PATH=str(bare)),capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.splitlines(),['-L','piper-ssh','attach-session','-t','piper-0123456789abcdef'])
        self.assertFalse((home/'.piper-server').exists())

    @unittest.skipUnless(shutil.which('tmux'), 'tmux not installed')
    def test_ssh_only_runs_without_piper_service_and_survives_attachment_loss(self):
        self.exercise_tmux('claude')

    @unittest.skipUnless(shutil.which('tmux'), 'tmux not installed')
    def test_ssh_only_codex_uses_foreground_native_sandbox_and_survives_attachment_loss(self):
        self.exercise_tmux('codex')

    def exercise_tmux(self, agentname):
        home = Path(self.tmp.name)
        project = home/'projects'/'repo'; (project/'.git').mkdir(parents=True)
        binary = home/'bin'; binary.mkdir()
        agent = binary/agentname; agent.write_text('#!/bin/sh\nif [ \"$1\" = \"--help\" ]; then echo --no-daemon; exit 0; fi\nprintf \"%s\\n\" \"$@\" > \"$HOME/agent-argv\"\nprintf agent-ready\\n\nsleep 30\n'); agent.chmod(0o700)
        env = dict(os.environ, HOME=str(home), PATH=str(binary)+':'+os.environ['PATH'], TMUX_TMPDIR=str(home))
        def request(method,path,body=None):
            req=dict(op='http', mode='ssh', project_path=str(home/'projects'),method=method,path=path,body=base64.b64encode(json.dumps(body or {}).encode()).decode(),ctype='application/json')
            p=subprocess.run([sys.executable,'-c',remotes.peer_source()],input=json.dumps(req).encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=15)
            self.assertEqual(p.returncode,0,p.stderr.decode())
            r=json.loads(p.stdout); self.assertNotIn('error',r)
            return r['status'],json.loads(base64.b64decode(r['body']))
        self.addCleanup(lambda: subprocess.run(['tmux','-L','piper-ssh','kill-server'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        code, result=request('POST','/api/sessions',dict(project='repo',agent=agentname,runtime='direct',task='hello'))
        self.assertEqual(code,200,result)
        sid=result['id']
        code,state=request('GET','/api/state')
        self.assertEqual(state['sessions'][0]['state'],'working')
        self.assertTrue(state['sessions'][0]['process_only'])
        deadline=__import__('time').time()+3
        while not (home/'agent-argv').exists() and __import__('time').time()<deadline: __import__('time').sleep(.02)
        if agentname == 'codex':
            args=(home/'agent-argv').read_text().splitlines()
            self.assertIn('--no-daemon',args)
            self.assertIn('workspace-write',args)
            self.assertIn('on-request',args)
        # Each peer process exits. The independent tmux/agent must still exist on next connection.
        code,state=request('GET','/api/state')
        self.assertEqual(state['sessions'][0]['id'],sid)
        self.assertFalse((home/'.local/share/piper-server').exists())
        self.assertEqual(request('POST',f'/api/sessions/{sid}/forget')[0],409)
        self.assertEqual(request('POST',f'/api/sessions/{sid}/stop')[0],200)
        self.assertEqual(request('GET','/api/state')[1]['sessions'][0]['state'],'exited')
        self.assertEqual(request('POST',f'/api/sessions/{sid}/forget')[0],200)
        self.assertEqual(request('GET','/api/state')[1]['sessions'],[])

    def test_service_install_refuses_unsupported_host_before_writes(self):
        with mock.patch.object(remotepeer.sys, 'platform', 'darwin'):
            with self.assertRaisesRegex(ValueError,'systemd'):
                remotepeer.install({})

    def test_service_install_rejects_bad_archive_before_extracting(self):
        with mock.patch.object(remotepeer.sys,'platform','linux'), mock.patch.object(remotepeer.shutil,'which',return_value='/bin/systemctl'), mock.patch.object(remotepeer,'run',return_value=mock.Mock(returncode=0)):
            with self.assertRaisesRegex(ValueError,'checksum'):
                remotepeer.install(dict(archive=base64.b64encode(b'bad').decode(),sha='wrong',project_path=self.tmp.name))

    def test_service_archive_rejects_traversal_and_links_without_publishing_release(self):
        home=Path(self.tmp.name)
        for name,link in (('../escaped',False),('linked',True)):
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as archive:
                member=tarfile.TarInfo(name)
                if link:
                    member.type=tarfile.SYMTYPE;member.linkname='/etc'
                archive.addfile(member)
            raw=stream.getvalue()
            with mock.patch.multiple(remotepeer,HOME=home,APP=home/'app',STATE=home/'state'),mock.patch.object(remotepeer.sys,'platform','linux'),mock.patch.object(remotepeer.shutil,'which',return_value='/test/systemctl'),mock.patch.object(remotepeer,'run',return_value=mock.Mock(returncode=0)):
                with self.assertRaisesRegex(ValueError,'unsafe archive'):
                    remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=__import__('hashlib').sha256(raw).hexdigest(),project_path=str(home)))
            self.assertFalse((home/'app/current').exists())
            self.assertFalse((home/'app/escaped').exists())


class TransferPeerTest(unittest.TestCase):
    """Move to server: the peer's `transfer` op unpacks only Piper's two files and starts the session once."""

    def setUp(self):
        import transfer
        self.transfer = transfer
        self.tmp = tempfile.TemporaryDirectory(prefix='transfer-peer-', dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patch = mock.patch.multiple(remotepeer, HOME=self.home, STATE=self.home / 'state')
        patch.start(); self.addCleanup(patch.stop)
        (self.home / 'state').mkdir()
        self.tid = 'c' * 16
        raw, sha = transfer.archive(b'# Handoff\n', {'id': self.tid, 'project': 'acme/shop', 'agent': 'codex',
                                                    'title': 'Shop', 'branch': 'feature/x'})
        self.req = {'op': 'transfer', 'mode': 'service', 'project_path': '/srv/projects', 'expected_identity': 'a' * 32,
                    'id': self.tid, 'archive': base64.b64encode(raw).decode(), 'sha': sha, 'project': 'acme/shop',
                    'agent': 'codex', 'title': 'Shop'}

    def service(self, sessions=(), capable=True):
        info = {'project_root': '/srv/projects', 'capabilities': {'transfer': capable}}
        return mock.patch.multiple(remotepeer, _service_info=mock.Mock(return_value=info),
                                   _service_json=mock.Mock(return_value={'sessions': list(sessions)}),
                                   require_service_root=mock.Mock())

    def test_transfer_unpacks_two_files_and_starts_one_session(self):
        with self.service(), mock.patch.object(remotepeer, '_service_call', return_value=(202, {'id': 'srv00001'})) as call:
            self.assertEqual(remotepeer.transfer(self.req), {'session': 'srv00001'})
        method, path, body = call.call_args.args
        self.assertEqual((method, path), ('POST', '/api/sessions'))
        self.assertEqual(body, {'project': 'acme/shop', 'agent': 'codex', 'title': 'Shop', 'transfer': self.tid})
        target = self.home / 'state' / 'transfers' / self.tid
        self.assertEqual(sorted(p.name for p in target.iterdir()), ['handoff.md', 'transfer.json'])
        self.assertEqual((target / 'handoff.md').stat().st_mode & 0o777, 0o600)
        meta, doc = self.transfer.load_incoming(str(self.home / 'state'), self.tid)
        self.assertEqual((meta['branch'], doc), ('feature/x', b'# Handoff\n'))

    def test_an_existing_session_of_the_transfer_is_returned_not_duplicated(self):
        existing = {'id': 'srv00002', 'transferred_from': {'id': self.tid}}
        with self.service([existing]), mock.patch.object(remotepeer, '_service_call') as call:
            self.assertEqual(remotepeer.transfer(self.req), {'session': 'srv00002', 'existing': True})
        call.assert_not_called()

    def test_refusals_before_creating_anything_are_not_executed(self):
        bad_sha = dict(self.req, sha='0' * 64)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as tf:
            for name in ('handoff.md', 'transfer.json', '.env'):
                info = tarfile.TarInfo(name); info.size = 2
                tf.addfile(info, io.BytesIO(b'{}'))
        extra = dict(self.req, archive=base64.b64encode(stream.getvalue()).decode(),
                     sha=__import__('hashlib').sha256(stream.getvalue()).hexdigest())
        cases = ((dict(self.req, expected_identity=None), True, 'identity'), (dict(self.req, mode='ssh'), True, 'service'),
                 (self.req, False, 'Update service'), (bad_sha, True, 'checksum'), (extra, True, 'unsafe'),
                 (dict(self.req, id='../x'), True, 'invalid transfer id'))
        for req, capable, why in cases:
            with self.subTest(why=why), self.service(capable=capable), mock.patch.object(remotepeer, '_service_call') as call:
                with self.assertRaisesRegex(remotepeer.NotExecuted, why):
                    remotepeer.transfer(req)
                call.assert_not_called()
        self.assertFalse((self.home / 'state' / 'transfers' / self.tid / '.env').exists())
        with self.service(), mock.patch.object(remotepeer, '_service_call', return_value=(409, {'error': 'unknown project'})):
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'unknown project'):
                remotepeer.transfer(self.req)

    def test_peer_without_identity_reports_not_executed_over_the_wire(self):
        env = dict(os.environ, HOME=self.tmp.name)
        result = subprocess.run([sys.executable, '-c', remotes.peer_source()], capture_output=True, env=env, timeout=10,
                                input=json.dumps(dict(self.req, expected_identity=None)).encode())
        reply = json.loads(result.stdout)
        self.assertTrue(reply.get('not_executed'), reply)
        self.assertIn('identity', reply['error'])

    def test_install_on_an_unsupported_server_is_refused_before_anything_is_written(self):
        # An old Python or a server without systemd is a definite refusal, not a lost answer (remote_unknown).
        for patch, why in ((mock.patch.object(remotepeer.sys, 'version_info', (3, 9, 6)), 'Python 3.10 or newer.*3.9'),
                           (mock.patch.object(remotepeer.sys, 'platform', 'darwin'), 'systemd.*darwin')):
            with self.subTest(why=why), patch, mock.patch.object(remotepeer, 'run') as run:
                with self.assertRaisesRegex(remotepeer.NotExecuted, why):
                    remotepeer._install(dict(self.req, archive='', sha=''))
                run.assert_not_called()
        self.assertFalse((self.home / '.local').exists())

    def test_lost_transfer_answer_is_unknown_and_sent_once(self):
        with mock.patch.object(remotes, 'FILE', os.path.join(self.tmp.name, 'environments.json')):
            remotes.save(dict(id='dev', label='Dev', host='dev-alias', project_path='/srv/projects', mode='service'))
            with mock.patch.object(remotes, 'get', return_value=dict(remotes.get('dev'), identity='a' * 32)), \
                    mock.patch.object(remotes.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)) as run:
                with self.assertRaisesRegex(remotes.Unavailable, 'not retried'):
                    remotes.transfer('dev', {k: self.req[k] for k in ('id', 'archive', 'sha', 'project', 'agent', 'title')})
            self.assertEqual(run.call_count, 1)
            sent = json.loads(run.call_args.kwargs['input'])
            self.assertEqual((sent['op'], sent['sha'], sent['expected_identity']), ('transfer', self.req['sha'], 'a' * 32))
            remotes.save(dict(id='plain', label='SSH', host='dev-alias', project_path='/srv/projects', mode='ssh'))
            with self.assertRaisesRegex(ValueError, 'service mode'):
                remotes.transfer('plain', {})


class SetupTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_setup_ops_use_existing_authenticated_ssh_channel_once(self):
        entry = dict(id='dev', host='dev.example.com', mode='service', identity='a' * 32, project_path='/home/server/projects')
        for op in sorted(remotes.SETUP_OPS):
            with self.subTest(op=op), mock.patch.object(remotes, 'get', return_value=entry), \
                    mock.patch.object(remotes.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 1)) as run:
                with self.assertRaises(remotes.Unavailable):
                    remotes.setup_request('dev', op, {'argv': ['tool', '--version']})
                self.assertEqual(run.call_count, 1)
                payload = json.loads(run.call_args.kwargs['input'])
                self.assertEqual((payload['op'], payload['expected_identity']), (op, 'a' * 32))
                self.assertIn('-T', run.call_args.args[0])
        with self.assertRaises(ValueError):
            remotes.setup_request('dev', 'arbitrary-command')
        with mock.patch.object(remotes, 'get', return_value=dict(entry, mode='ssh')):
            with self.assertRaises(ValueError):
                remotes.setup_request('dev', 'setup_git_key')

    def test_setup_wire_refuses_unpinned_writes_before_dispatch(self):
        env = dict(os.environ, HOME=str(self.root))
        for op in sorted(remotes.SETUP_OPS - {'setup_status'}):
            result = subprocess.run([sys.executable, '-c', remotes.peer_source()], capture_output=True,
                                    env=env, timeout=10, input=json.dumps({'op': op}).encode())
            reply = json.loads(result.stdout)
            self.assertTrue(reply.get('not_executed'), (op, reply))
            self.assertIn('identity', reply['error'])
        self.assertFalse((self.root / '.ssh').exists())
        self.assertFalse((self.root / '.local').exists())

    def extension(self):
        source = self.root / 'acme'
        source.mkdir()
        (source / 'piper-extension.json').write_text('{"name":"acme","api":2,"label":"Tools","env_file":"tool.env"}')
        (source / 'extension.py').write_text('API = 2\nNAME = "acme"\n')
        info = dict(path=str(source), name='acme', label='Tools', compatible=True, trusted=True)
        return source, info

    def test_extension_snapshot_requires_current_trust_excludes_env_links_and_runtime(self):
        import extensions
        import extfind
        import gitlab
        source, info = self.extension()
        for name in ('.env', 'tool.env', 'auth.json', 'access-token.txt', 'id_ed25519', 'private.key'):
            (source / name).write_text('PRIVATE PLACEHOLDER')
        (source / 'linked.py').symlink_to(source / 'extension.py')
        # Real extension code names passwords and tokens; only secret VALUES refuse a snapshot.
        (source / 'server.py').write_text('LABELS = {"MAVEN_PASSWORD": "Maven password"}\n'
                                          'def login(token: str): return {"Authorization": "Bearer " + token}\n')
        (source / 'e2e').mkdir()
        (source / 'e2e/stack.py').write_text('PLANTED = "glpat-' + 'a' * 20 + '"\n')  # test fixtures stay local
        with mock.patch.object(extensions, 'load'), mock.patch.object(extensions, 'DIRS', [str(source)]), \
                mock.patch.object(extfind, 'trusted_found', return_value=[info]):
            raw, sha, names = remotes.extension_bundle()
            second, second_sha, _ = remotes.extension_bundle()
        self.assertEqual((raw, sha), (second, second_sha))
        self.assertEqual(names, ['acme'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), sha)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
            self.assertEqual({Path(n).name for n in tar.getnames()}, {'extension.py', 'piper-extension.json', 'server.py'})
        self.assertNotIn(b'PRIVATE PLACEHOLDER', raw)
        with mock.patch.object(extensions, 'load'), mock.patch.object(extensions, 'DIRS', [str(source)]), \
                mock.patch.object(extfind, 'trusted_found', return_value=[]):
            with self.assertRaisesRegex(ValueError, 'trusted'):
                remotes.extension_bundle()

    def test_extension_secret_scan_refuses_entire_archive_without_secret_in_error(self):
        import extensions
        import extfind
        import gitlab
        source, info = self.extension()
        (source / 'extension.py').write_text('ACCESS_TOKEN = "glpat-fixture-private-value"\n')
        with mock.patch.object(extensions, 'load'), mock.patch.object(extensions, 'DIRS', [str(source)]), \
                mock.patch.object(extfind, 'trusted_found', return_value=[info]):
            with self.assertRaisesRegex(ValueError, 'secret scan') as error:
                remotes.extension_bundle()
        self.assertNotIn('fixture-private-value', str(error.exception))


class PeerSizeTest(unittest.TestCase):
    def test_peer_command_stays_below_linux_single_argument_limit(self):
        # The peer travels as one `python3 -c <source>` argument through the remote login shell;
        # Linux refuses a single argument over 128 KiB (MAX_ARG_STRLEN). Keep headroom.
        import shlex
        self.assertLess(len(shlex.join(['python3', '-c', remotes.peer_source()]).encode()), 100_000)
