"""Install a real server release and exercise its daemon/PTY across controller restarts.

Systemd activation is stubbed on macOS. Runtime executes the matching read-only source
checkout: the mandatory sandbox refuses importing application code from writable fixtures.
PIPER_REMOTE_TEST_RELEASE optionally selects an externally prepared read-only release.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import remotepeer
import remotes
from ptyclient import PtyClient


class PythonInstallTest(unittest.TestCase):
    """Downloads and runtime execution are stubbed; all writes stay in a disposable home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.directory = self.home / '.local/opt/python3.12'
        self.target = 'aarch64-apple-darwin'
        self.url = ('https://github.com/astral-sh/python-build-standalone/releases/download/'
                    '20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only.tar.gz')
        patches = [mock.patch.object(remotepeer, 'HOME', self.home),
                   mock.patch.object(remotepeer.sys, 'platform', 'darwin'),
                   mock.patch.object(remotepeer.platform, 'machine', return_value='arm64')]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        self.download = mock.patch.object(remotepeer.urllib.request, 'urlopen').start()
        self.addCleanup(mock.patch.stopall)
        self.download.side_effect = AssertionError('Unexpected download')
        self.runtime = mock.patch.object(remotepeer, '_setup_run', side_effect=self.verify_runtime).start()

    def verify_runtime(self, argv, timeout):
        binary = Path(argv[0])
        self.assertTrue(binary.is_file())
        self.assertEqual(binary.name, 'python3.12')
        self.assertIn('import sys, ssl, sqlite3, venv', argv[2])
        self.assertEqual(timeout, 20)
        if binary.parent.parent != self.directory:
            self.assertFalse(self.directory.exists(), 'verify before publishing')
            self.assertEqual(binary.parent.parent.parent, self.directory.parent)
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    def content(self, extra=()):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            binary = tarfile.TarInfo('python/bin/python3.12')
            binary.size, binary.mode = 7, 0o755
            archive.addfile(binary, io.BytesIO(b'fixture'))
            link = tarfile.TarInfo('python/bin/python3')
            link.type, link.linkname = tarfile.SYMTYPE, 'python3.12'
            archive.addfile(link)
            for member in extra:
                archive.addfile(member)
        raw = stream.getvalue()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = raw
        response.geturl.return_value = self.url
        self.download.side_effect = None
        self.download.return_value = response
        patch = mock.patch.dict(remotepeer.PYTHON_SHA256, {self.target: hashlib.sha256(raw).hexdigest()})
        patch.start(); self.addCleanup(patch.stop)
        return raw, response

    def test_install_is_atomic_user_only_python39_and_reuses_healthy_runtime(self):
        _, response = self.content()
        with mock.patch.object(remotepeer.sys, 'version_info', (3, 9, 6)):
            self.assertEqual(remotepeer.install_python({}), {'installed': True, 'existing': False})
            for name in ('python3', 'python3.12'):
                (self.home / '.local/bin' / name).unlink()
            self.assertEqual(remotepeer.install_python({}), {'installed': True, 'existing': True})
        self.download.assert_called_once_with(self.url, timeout=30)
        response.read.assert_called_once_with((128 << 20) + 1)
        self.assertEqual(self.runtime.call_count, 2)
        for name in ('python3', 'python3.12'):
            link = self.home / '.local/bin' / name
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), self.directory / 'bin/python3.12')
        self.assertEqual((self.directory / 'bin/python3').resolve(), self.directory / 'bin/python3.12')
        self.assertEqual(list(self.directory.parent.glob('.python3.12-*')), [])
        self.assertFalse((self.home / '.zshrc').exists())
        self.assertFalse((self.home / '.bashrc').exists())

    def test_hash_mismatch_is_refused_before_extraction(self):
        self.content()
        with mock.patch.dict(remotepeer.PYTHON_SHA256, {self.target: '0' * 64}), \
                mock.patch.object(remotepeer, '_python_extract') as extract:
            with self.assertRaisesRegex(remotepeer.NotExecuted, 'SHA-256 mismatch'):
                remotepeer.install_python({})
        extract.assert_not_called()
        self.runtime.assert_not_called()
        self.assertFalse(self.directory.parent.exists())

    def test_unsafe_members_links_special_files_and_failed_runtime_are_never_published(self):
        for kind in ('traversal', 'absolute', 'symlink', 'hardlink', 'fifo', 'runtime'):
            with self.subTest(kind=kind):
                member = tarfile.TarInfo('python/../escape' if kind == 'traversal' else
                                         '/absolute' if kind == 'absolute' else 'python/unsafe')
                if kind in ('symlink', 'hardlink'):
                    member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
                    member.linkname = '../../escape'
                if kind == 'fifo':
                    member.type = tarfile.FIFOTYPE
                self.content([] if kind == 'runtime' else [member])
                with mock.patch.object(remotepeer, '_python_ready', wraps=remotepeer._python_ready) as ready:
                    if kind == 'runtime':
                        ready.return_value = False
                    with self.assertRaisesRegex(remotepeer.NotExecuted, 'unsafe|verification failed'):
                        remotepeer.install_python({})
                self.assertFalse(self.directory.exists())
                self.assertFalse((self.home / '.local/bin').exists())
                self.assertEqual(list(self.directory.parent.glob('.python3.12-*')), [])

    def test_unknown_platform_arch_and_musl_are_not_executed(self):
        for system, arch in (('freebsd', 'x86_64'), ('darwin', 'riscv64'), ('linux', 'x86_64')):
            with self.subTest(system=system, arch=arch), \
                    mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer.os, 'confstr', return_value=None):
                with self.assertRaisesRegex(remotepeer.NotExecuted, r'install Python 3.10\+ in ~/.local/bin'):
                    remotepeer.install_python({})
        self.download.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_all_four_pins_are_complete_and_target_mapping_requires_glibc(self):
        for system, arch, expected in (('darwin', 'arm64', 'aarch64-apple-darwin'),
                                       ('darwin', 'x86_64', 'x86_64-apple-darwin'),
                                       ('linux', 'x86_64', 'x86_64-unknown-linux-gnu'),
                                       ('linux', 'aarch64', 'aarch64-unknown-linux-gnu')):
            with mock.patch.object(remotepeer.sys, 'platform', system), \
                    mock.patch.object(remotepeer.platform, 'machine', return_value=arch), \
                    mock.patch.object(remotepeer.os, 'confstr', return_value='glibc 2.31'):
                self.assertEqual(remotepeer._python_target(), expected)
                self.assertRegex(remotepeer.PYTHON_SHA256[expected], r'^[0-9a-f]{64}$')

    def test_download_failure_and_size_cap_are_clear_retryable_refusals(self):
        for failure in (OSError('proxy credentials must not be returned'), None):
            with self.subTest(failure=type(failure).__name__):
                _, response = self.content()
                if failure:
                    self.download.side_effect = failure
                else:
                    # Exercise the size guard without allocating a 128 MiB fixture.
                    response.read.return_value = mock.MagicMock(__len__=lambda _: (128 << 20) + 1)
                with self.assertRaisesRegex(remotepeer.NotExecuted, 'Cannot download.*HTTPS_PROXY') as error:
                    remotepeer.install_python({})
                self.assertNotIn('credentials', str(error.exception))
                self.assertFalse(self.directory.exists())

    def test_existing_user_command_and_broken_install_are_preserved(self):
        link = self.home / '.local/bin/python3'
        link.parent.mkdir(parents=True)
        link.write_text('user command')
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'Preserving existing'):
            remotepeer.install_python({})
        self.assertEqual(link.read_text(), 'user command')
        link.unlink()
        self.directory.mkdir(parents=True)
        with mock.patch.object(remotepeer, '_setup_run', side_effect=FileNotFoundError), \
                self.assertRaisesRegex(remotepeer.NotExecuted, 'not working'):
            remotepeer.install_python({})
        self.download.assert_not_called()
        self.assertTrue(self.directory.exists())


class LaunchdServiceTest(unittest.TestCase):
    """All launchctl calls are stubbed, including status and the SSH user domain probe."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launchd ' & ", dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.app, self.state = self.home / 'app', self.home / 'state'
        self.root = self.home / 'projects'
        self.python = str(self.home / '.local/bin/python3.12')
        self.target = 'user/%d/%s' % (os.getuid(), remotepeer.SERVER_LABEL)
        self.loaded, self.pid, self.delayed, self.available = False, 0, 0, True
        self.commands = []
        patches = [mock.patch.multiple(remotepeer, HOME=self.home, APP=self.app, STATE=self.state),
                   mock.patch.object(remotepeer.sys, 'platform', 'darwin'),
                   mock.patch.object(remotepeer.sys, 'executable', self.python),
                   mock.patch.object(remotepeer.shutil, 'which', side_effect=lambda name: '/bin/launchctl' if name == 'launchctl' else None),
                   mock.patch.object(remotepeer, '_setup_run', side_effect=self.launchctl),
                   mock.patch.object(remotepeer, 'run', side_effect=self.mint),
                   mock.patch.object(remotepeer, '_service_info', side_effect=lambda: {'project_root': str(self.root)} if self.loaded else None),
                   mock.patch.object(remotepeer.time, 'sleep')]
        for patch in patches:
            patch.start(); self.addCleanup(patch.stop)
        raw, sha = remotes.bundle()
        self.req = dict(archive=base64.b64encode(raw).decode(), sha=sha, setup=True, project_path=str(self.root))

    def mint(self, argv, **kw):
        self.assertEqual(argv[:2], [self.python, '-c'])
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    def launchctl(self, argv, *args, **kw):
        self.commands.append(argv)
        self.assertEqual(argv[0], 'launchctl')
        self.assertTrue(argv[2].startswith('user/'), argv)
        rc, output = 0, b''
        if argv[1] == 'print':
            if argv[2] != self.target:
                rc = 0 if self.available else 1
            elif self.delayed:
                self.delayed -= 1
                output = b'job still unloading'
            elif self.loaded:
                output = ('\tpid = %d\n\tenvironment = {\n\t\tPATH => /usr/bin:/bin\n\t}\n' % self.pid).encode()
            else:
                rc = 1
        elif argv[1] == 'bootout':
            self.delayed = 2 if self.loaded else 0
            rc = 0 if self.loaded else 3
            self.loaded = False
        elif argv[1] == 'bootstrap':
            self.assertFalse(self.delayed, 'bootstrap must wait for asynchronous bootout')
            self.assertEqual(argv[3], str(self.home / 'Library/LaunchAgents/dev.piper.server.plist'))
            self.loaded = True; self.pid += 1
        else:
            self.assertEqual(argv[1], 'enable')
        return subprocess.CompletedProcess(argv, rc, output, b'')

    def test_install_update_use_user_domain_selected_python_and_private_plist(self):
        for _ in range(2):
            result = remotepeer.install(self.req)
            self.assertTrue(result['installed'])
            self.assertTrue(result['service_running'])
            self.assertTrue(result['linger'])
            self.assertTrue(result['autonomous_after_logout'])
            self.assertEqual(result['notice'], '')
        self.assertEqual(self.pid, 2)
        path = self.home / 'Library/LaunchAgents/dev.piper.server.plist'
        agent = plistlib.loads(path.read_bytes())
        self.assertEqual(agent['ProgramArguments'], [self.python, str(self.app / 'current/serve.py')])
        self.assertEqual(agent['Label'], 'dev.piper.server')
        self.assertEqual(agent['WorkingDirectory'], str(self.home))
        self.assertTrue(all(agent[k] for k in ('RunAtLoad', 'KeepAlive', 'AbandonProcessGroup')))
        # Regression: a real Mac refused bootstrap into user/<uid> (error 5) without the Background session type.
        self.assertEqual(agent['LimitLoadToSessionType'], 'Background')
        # Regression: /usr/bin/java on macOS is a stub; tools installed for Piper in ~/.local/bin must come first.
        self.assertEqual(agent['EnvironmentVariables']['PATH'].split(':')[0], str(self.home / '.local/bin'))
        self.assertEqual(agent['StandardOutPath'], str(self.state / 'dev.piper.server.log'))
        self.assertEqual(agent['StandardErrorPath'], agent['StandardOutPath'])
        self.assertEqual(agent['EnvironmentVariables']['PIPER_DEFAULT_RUNTIME'], 'direct')
        self.assertEqual(agent['EnvironmentVariables']['PROJECT_PATH'], str(self.root))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_HOME'], str(self.state))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_PTY_SOCK'], str(self.state / 'ptyhost.sock'))
        self.assertEqual(agent['EnvironmentVariables']['PIPER_BIND'], '127.0.0.1')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / '.config/systemd').exists())
        self.assertEqual(remotepeer._service_env('PATH'), '/usr/bin:/bin')

    def test_unavailable_launchd_is_not_executed_before_managed_writes(self):
        self.available = False
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'launchd user domain'):
            remotepeer._install(self.req)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / 'Library').exists())
        self.assertEqual([c[1] for c in self.commands], ['print'])

    def test_old_python_and_symlinked_launchagent_are_definite_refusals(self):
        with mock.patch.object(remotepeer.sys, 'version_info', (3, 9, 6)):
            with self.assertRaisesRegex(remotepeer.NotExecuted, r'Python 3.10.*~/.local/bin'):
                remotepeer._install(self.req)
        self.assertEqual(self.commands, [])
        directory = self.home / 'Library/LaunchAgents'
        directory.mkdir(parents=True)
        (directory / 'dev.piper.server.plist').symlink_to(self.home / 'unmanaged.plist')
        with self.assertRaisesRegex(remotepeer.NotExecuted, 'symlinks'):
            remotepeer._install(self.req)
        self.assertFalse(self.app.exists())
        self.assertFalse(self.state.exists())
        self.assertFalse(self.root.exists())

    def test_a_job_without_a_pid_cannot_supply_live_environment(self):
        output = b'\tinherited environment = {\n\t\tPATH => /wrong\n\t}\n\tenvironment = {\n\t\tPATH => /live\n\t}\n'
        with mock.patch.object(remotepeer, '_setup_run', return_value=subprocess.CompletedProcess([], 0, output, b'')) as run:
            self.assertIsNone(remotepeer._service_env('PATH'))
            run.return_value.stdout = b'\tpid = 123\n' + output
            self.assertEqual(remotepeer._service_env('PATH'), '/live')

    def test_a_job_that_will_not_unload_is_bounded_and_never_bootstrapped(self):
        self.loaded = True
        def stuck(argv, *args, **kw):
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'job still unloading', b'')
        with mock.patch.object(remotepeer, '_setup_run', side_effect=stuck):
            with self.assertRaisesRegex(ValueError, 'did not unload'):
                remotepeer._launch_restart(remotepeer.SERVER_LABEL)
        self.assertEqual(len(self.commands), 21)
        self.assertFalse(any(c[1] == 'bootstrap' for c in self.commands))

    def test_update_preserves_extension_environment(self):
        remotepeer.install(self.req)
        path = self.home / 'Library/LaunchAgents/dev.piper.server.plist'
        agent = plistlib.loads(path.read_bytes())
        agent['EnvironmentVariables']['PIPER_EXTENSIONS'] = str(self.app / 'extensions/snapshot/acme')
        path.write_bytes(plistlib.dumps(agent))
        remotepeer.install(self.req)
        self.assertEqual(plistlib.loads(path.read_bytes())['EnvironmentVariables']['PIPER_EXTENSIONS'],
                         agent['EnvironmentVariables']['PIPER_EXTENSIONS'])

    def test_bootstrap_failure_is_not_a_definite_prerequisite_refusal(self):
        real = self.launchctl
        def fail(argv, *args, **kw):
            if argv[1] == 'bootstrap':
                return subprocess.CompletedProcess(argv, 5, b'', b'private diagnostic')
            return real(argv, *args, **kw)
        with mock.patch.object(remotepeer, '_setup_run', side_effect=fail):
            with self.assertRaisesRegex(ValueError, 'LaunchAgent setup failed') as error:
                remotepeer.install(self.req)
        self.assertNotIsInstance(error.exception, remotepeer.NotExecuted)
        self.assertNotIn('private diagnostic', str(error.exception))
        self.assertTrue((self.app / 'current').exists())


class RemoteServiceTest(unittest.TestCase):
    def test_installed_release_claude_survives_controller_restart(self):
        self.exercise_release("claude")

    @unittest.skipUnless(sys.version_info >= (3,11), "effective Codex config fixture requires tomllib")
    def test_installed_release_codex_survives_controller_restart(self):
        self.exercise_release("codex")

    def exercise_release(self, agentname):
        with tempfile.TemporaryDirectory(prefix='rs-') as directory:
            home=Path(directory)
            app=home/'app'; state=home/'state'; root=home/'projects'; project=root/'repo'
            project.mkdir(parents=True)
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'hello.txt').write_text('server-owned file\n')
            subprocess.run(['git','-C',str(project),'add','hello.txt'],check=True)
            subprocess.run(['git','-C',str(project),'commit','-qm','initial'],check=True)
            binary=home/'bin';binary.mkdir()
            agent=binary/agentname
            agent.write_text('#!'+sys.executable+'\nimport json,os,shlex,subprocess,sys,time\n'
                'if "--help" in sys.argv: print("--no-daemon");sys.exit(0)\n'
                'if "--settings" in sys.argv: cfg=json.load(open(sys.argv[sys.argv.index("--settings")+1]))\n'
                'else:\n'
                ' cfg={"hooks":{}}\n'
                ' for i,arg in enumerate(sys.argv[:-1]):\n'
                '  if arg=="-c" and sys.argv[i+1].startswith("hooks."):\n'
                '   cfg["hooks"].update(__import__("tomllib").loads(sys.argv[i+1])["hooks"])\n'
                'for ev in ("SessionStart","UserPromptSubmit"):\n'
                ' command=cfg["hooks"][ev][0]["hooks"][0]["command"]\n'
                ' subprocess.run(shlex.split(command),input=json.dumps({"session_id":"12345678-abcd","source":"startup","prompt":"test"}).encode(),check=True)\n'
                'print("server-agent-running",flush=True)\ntime.sleep(60)\n')
            agent.chmod(0o700)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            raw,sha=remotes.bundle()
            if agentname == 'claude':
                state.mkdir()
                (state/'features.json').write_text(json.dumps({'v':2,'base':'basic','modules':{'codex':False},'onboarded':True}))
            real_run=remotepeer.run
            def systemd(argv,**kw):
                if argv[0] in ('systemctl','loginctl'):
                    return subprocess.CompletedProcess(argv,0,stdout=b'yes\n',stderr=b'')
                if argv[:2] == [sys.executable,'-c']:
                    argv=list(argv)
                    # Use the sandbox's read-only source for minting, not writable test code.
                    argv[2]=argv[2].replace(str(app/'current/piper'),common.PIPER_DIR)
                    for release in app.glob('release-*'):
                        argv[2]=argv[2].replace(str(release/'piper'),common.PIPER_DIR)
                result=real_run(argv,**kw)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                return result
            env=dict(os.environ,PIPER_CLAUDE_BIN=str(binary/'claude'),PIPER_CODEX_BIN=str(binary/'codex'),HOME=str(home),PIPER_REAL_HOME=str(home),PATH=str(binary)+':'+os.environ['PATH'])
            processes=[]
            log=(home/'daemon.log').open('ab')
            self.addCleanup(log.close)
            def start():
                release=os.environ.get('PIPER_REMOTE_TEST_RELEASE') or common.SK
                serverenv=dict(env,PIPER_HOME=str(state),PIPER_PTY_SOCK=str(state/'ptyhost.sock'),PIPER_SERVICE_LABEL='piper-server',PIPER_DEFAULT_RUNTIME='direct',PIPER_PORT=str(port),PIPER_BIND='127.0.0.1',PROJECT_PATH=str(root))
                p=subprocess.Popen([sys.executable,os.path.join(release,'piper/piperd.py')],env=serverenv,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                processes.append(p)
                deadline=time.time()+10
                while time.time()<deadline:
                    try:
                        r=remotepeer.http(dict(mode='service',project_path=str(root),method='GET',path='/api/state',body='',ctype='application/json'))
                        if r['status']==200:return p,json.loads(base64.b64decode(r['body']))
                    except (OSError,ValueError):pass
                    if p.poll() is not None:break
                    time.sleep(.1)
                self.fail('installed server did not start: '+(home/'daemon.log').read_text()[-3000:])
            def request(method,path,body=None):
                r=remotepeer.http(dict(mode='service',project_path=str(root),method=method,path=path,body=base64.b64encode(json.dumps(body or {}).encode()).decode(),ctype='application/json'))
                return r['status'],json.loads(base64.b64decode(r['body']))
            real_which=__import__('shutil').which
            with mock.patch.multiple(remotepeer,HOME=home,APP=app,STATE=state,PORT=port), \
                    mock.patch.dict(os.environ,env,clear=True), \
                    mock.patch.object(remotepeer.sys,'platform','linux'), \
                    mock.patch.object(remotepeer.shutil,'which',side_effect=lambda name: '/bin/'+name if name in ('systemctl','loginctl') else real_which(name)), \
                    mock.patch.object(remotepeer,'run',side_effect=systemd):
                result=remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(root)))
                self.assertTrue(result['installed'])
                self.assertTrue(result['autonomous_after_logout'])
                self.assertNotIn('token',result)
                try:
                    p,st=start()
                    self.assertEqual(st['default_runtime'],'direct')
                    self.assertFalse(st['features']['modules']['codex'], 'installation never rewrites module preferences')
                    self.assertTrue(st['agent_availability'][agentname])
                    agent.rename(binary / (agentname + '.aside'))
                    self.assertFalse(request('GET','/api/state')[1]['agent_availability'][agentname])
                    code, missing = request('POST','/api/sessions',dict(project='repo',runtime='direct',agent=agentname))
                    self.assertEqual(code,409,missing)
                    self.assertIn('not installed',missing['error'])
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    (binary / (agentname + '.aside')).rename(agent)
                    self.assertTrue(request('GET','/api/state')[1]['agent_availability'][agentname])
                    self.assertEqual(st['projects'][0]['path'],str(project))
                    self.assertTrue(remotepeer.probe({'project_path': str(root) + '/'})['project_root_matches'])
                    for method in ('GET', 'POST'):
                        with self.assertRaisesRegex(ValueError, 'Projects folder differs'):
                            remotepeer.http(dict(mode='service',project_path=str(home/'other-projects'),method=method,
                                path='/api/state' if method=='GET' else '/api/sessions',body='',ctype='application/json'))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    code,row=request('POST','/api/sessions',dict(project='repo',task='test native hooks',runtime='direct',agent=agentname,remember_for_project=True))
                    self.assertEqual(code,202,row)
                    self.assertEqual(row['project_defaults']['agent'],agentname)
                    sid=row['id']
                    deadline=time.time()+8
                    while time.time()<deadline:
                        st=request('GET','/api/state')[1]
                        s=next(x for x in st['sessions'] if x['id']==sid)
                        if s['state']=='working':break
                        if s['state']=='error':self.fail(s['detail'])
                        time.sleep(.1)
                    self.assertEqual(s['state'],'working',s)
                    self.assertEqual(s['runtime'],'direct')
                    self.assertEqual(s['claude_session'],'12345678-abcd')
                    original_pid=s['pid']
                    other = home / 'other-projects'; other.mkdir()
                    with self.assertRaisesRegex(remotepeer.RebindRefused, 'sessions are active'):
                        remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(other)))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'][0]['pid'], original_pid)
                    code,data=request('GET','/api/fs/read?project=repo&path=hello.txt')
                    self.assertEqual(code,200,data)
                    self.assertIn('server-owned file','\n'.join(data['lines']))
                    self.assertEqual(request('GET',f'/api/sessions/{sid}/git')[0],200)
                    if agentname == 'claude':
                        self.exercise_local_provider(home, state, project, port, env, sid)
                    # Restart only the throwaway controller, never the user's daemon/ptyhost.
                    p.terminate();p.wait(timeout=5)
                    p,st=start()
                    restored=next(x for x in st['sessions'] if x['id']==sid)
                    self.assertEqual(restored['pid'],original_pid)
                    self.assertIn(restored['state'],('working','ready','waiting'))
                    os.kill(original_pid,0)
                    pc=PtyClient(str(state/'ptyhost.sock'))
                    try:
                        reply=pc.call('subscribe',id=sid)
                        self.assertTrue(reply['ok'])
                        self.assertTrue(reply['info']['alive'])
                    finally:pc.close()
                    self.exercise_terminal_bridge(state, port, sid, original_pid)
                    self.assertEqual(request('POST',f'/api/sessions/{sid}/stop')[0],200)
                finally:
                    # Terminate children through the private test PTY socket and reap its test host.
                    try:
                        pc=PtyClient(str(state/'ptyhost.sock'))
                        hostpid=pc.call('ping')['pid']
                        for row in pc.call('list').get('sessions',[]):
                            if row['alive']:pc.call('signal',id=row['id'],sig='KILL')
                        pc.close()
                        os.kill(hostpid,signal.SIGTERM)
                    except OSError:pass
                    for p in processes:
                        if p.poll() is None:p.terminate();p.wait(timeout=5)
                    pidfile=state/'ptyhost.pid'
                    if pidfile.exists():
                        pid=int(pidfile.read_text());os.kill(pid,signal.SIGTERM)
                    log.close()

    def exercise_terminal_bridge(self, state, port, sid, agent_pid):
        """Actual binary PTY protocol over peer stdio; SSH process creation alone is substituted."""
        identity=remotepeer.peer_identity()
        entry=dict(id='fixture',host='fixture',project_path=str(state.parent/'projects'),mode='service',identity=identity)
        source=remotes.peer_source().replace("STATE = HOME / '.piper-server'",'STATE = Path('+repr(str(state))+')').replace('PORT = 17896','PORT = '+str(port))
        def local_transport(entry,command,**kw):
            return [sys.executable,*command[1:]]
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'peer_source',return_value=source),mock.patch.object(remotes,'ssh',side_effect=local_transport):
            bridge=remotes.terminal('fixture',sid,lambda event: None)
            try:
                reply=bridge.call('subscribe',id=sid)
                self.assertTrue(reply['ok'])
                self.assertTrue(reply['info']['alive'])
                self.assertTrue(bridge.call('resize',id=sid,cols=100,rows=30)['ok'])
            finally: bridge.close()
        # Closing the client/transport is independent of the server-owned agent lifetime.
        os.kill(agent_pid,0)

    def exercise_local_provider(self, home, state, project, port, env, sid):
        """Real stdio MCP -> session mailbox -> host capability HTTP -> existing project HTTP tools, no mocked Manager."""
        (project/'http-client.env.json').write_text(json.dumps({'test':{'host':'http://127.0.0.1:9'}}))
        (project/'http-client.private.env.json').write_text(json.dumps({'test':{'token':'PLANTED_PROVIDER_SECRET'}}))
        grant=dict(id='a'*32,token='test-capability-placeholder',enabled=True,environment='fixture',remote_project='server/repo',local_project='repo')
        path=state/'provider-grants.json'
        path.write_text(json.dumps({'grants':[grant]})); path.chmod(0o600)
        config=state/'sessions'/sid/'local-provider.json'
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        config.chmod(0o600)
        wrapper=os.path.join(common.PIPER_DIR,'sandbox/piper-local-mcp.py')
        def mcp(messages):
            result=subprocess.run([sys.executable,wrapper,'--mailbox',str(state/'sessions'/sid/'piper/piper-mailbox')],input=''.join(json.dumps(m)+'\n' for m in messages),text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('PLANTED_PROVIDER_SECRET',result.stdout)
            return [json.loads(line)['result'] for line in result.stdout.splitlines()]
        call=dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='http_envs',arguments={}))
        listed,reply,denied=mcp([dict(jsonrpc='2.0',id=1,method='tools/list'),call,dict(call,id=3,params=dict(name='git_run',arguments={}))])
        names={t['name'] for t in listed['tools']}
        self.assertIn('http_run',names)
        self.assertIn('db_query',names)
        self.assertNotIn('git_run',names)
        self.assertFalse(reply['isError'],reply)
        self.assertIn('test',reply['content'][0]['text'])
        self.assertTrue(denied['isError'])
        # Reverse SSH preserves the tunnel port in HTTP Host. Forward real bytes
        # through a distinct loopback port, while the private config names the
        # controller's expected Host. Do not weaken its global Host/Origin gate.
        import socketserver, selectors, threading
        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection(('127.0.0.1',port)) as upstream, selectors.DefaultSelector() as selector:
                    selector.register(self.request,selectors.EVENT_READ,upstream)
                    selector.register(upstream,selectors.EVENT_READ,self.request)
                    while True:
                        for key,_ in selector.select(timeout=5):
                            data=key.fileobj.recv(65536)
                            if not data:return
                            key.data.sendall(data)
        proxy=socketserver.ThreadingTCPServer(('127.0.0.1',0),Forward)
        proxy.daemon_threads=True
        thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
        try:
            config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{proxy.server_address[1]}/api/local-provider/'+grant['id'],host=f'127.0.0.1:{port}',token=grant['token'])))
            tunneled=mcp([call])[0]
            self.assertFalse(tunneled['isError'],tunneled)
            self.assertIn('test',tunneled['content'][0]['text'])
        finally:
            proxy.shutdown();proxy.server_close();thread.join(5)
        records=list((state/'provider-calls'/grant['id']).glob('*.json'))
        self.assertEqual(len(records),2)
        self.assertEqual(json.loads(records[0].read_text())['state'],'completed')
        self.assertNotIn('PLANTED_PROVIDER_SECRET',records[0].read_text())
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        grant['enabled']=False
        path.write_text(json.dumps({'grants':[grant]}))
        refused=mcp([call])[0]
        self.assertTrue(refused['isError'])
        self.assertIn('revoked',refused['content'][0]['text'])
        self.assertIn('No operation was sent',refused['content'][0]['text'])
        self.assertEqual(len(list((state/'provider-calls'/grant['id']).glob('*.json'))),2)
