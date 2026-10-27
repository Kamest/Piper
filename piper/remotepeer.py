"""One-shot SSH peer (also sent over stdin by remotes). No daemon needed for probe/SSH mode.

Server service uses its own ~/.piper-server state and native agent logins. This file
has only stdlib dependencies so the service can be bootstrapped before Piper exists.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit

HOME = Path.home()
os.environ['PATH'] = os.environ.get('PATH','/usr/bin:/bin') + ':' + str(HOME / '.local/bin')
STATE = HOME / '.piper-server'
APP = HOME / '.local/share/piper-server'
PORT = 17896


class RebindRefused(ValueError):
    """A reconfiguration rejected before the release or service configuration was changed."""


def peer_identity(expected=None):
    """Private account identity survives service updates and also works without the service."""
    import fcntl
    directory = HOME / '.cache/piper-ssh'
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (directory / 'identity.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / 'identity'
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as target:
                target.write(uuid.uuid4().hex)
        identity = path.read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid server identity; verify server state before reconnecting.')
        if expected is not None and expected != identity:
            raise ValueError('SSH target identity changed. Verify the server and use a new environment ID; no operation was executed.')
        return identity


def run(argv, **kw):
    return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, **kw)


def _service_json(path):
    """Read authenticated service metadata without exporting its token or process environment."""
    if not (STATE / 'gui-token').is_file():
        return None
    connection = HTTPConnection('127.0.0.1', PORT, timeout=5)
    try:
        connection.request('GET', path, headers={'Authorization': 'Bearer ' + (STATE / 'gui-token').read_text().strip()})
        response = connection.getresponse()
        if response.status != 200:
            return None
        result = json.loads(response.read(32 << 20))
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        connection.close()


def canonical_root(value):
    if not isinstance(value, str) or not value.startswith('/') or any(ord(c) < 32 for c in value):
        raise ValueError('Projects folder must be an absolute server path.')
    return os.path.realpath(value)


def _service_env(key):
    """Read one key from the verified live service process, never export its environment."""
    if sys.platform != 'linux':
        return None
    command = ['systemctl', '--user', 'show', 'piper-server.service', '-p', 'MainPID', '--value']
    try:
        pid = run(command).stdout.strip()
        if pid.isdigit() and int(pid) > 0:
            entries = (Path('/proc') / pid.decode() / 'environ').read_bytes().split(b'\0')
            prefix = key.encode() + b'='
            value = next((v[len(prefix):].decode() for v in entries if v.startswith(prefix)), None)
            if run(command).stdout.strip() == pid:
                return value
    except (OSError, ValueError):
        pass
    return None


def _service_info():
    info = _service_json('/api/ping')
    if info is not None and not info.get('project_root'):
        root = _service_env('PROJECT_PATH')
        if root:
            info = dict(info, project_root=canonical_root(root))
    return info


def service_agents():
    """Legacy services: inspect executables in the actual launch PATH, not the SSH shell's."""
    path = _service_env('PATH')
    return {name: bool(shutil.which(name, path=path)) for name in ('claude', 'codex')} if path is not None else None


@contextmanager
def service_lock(exclusive=False):
    """Serialize account-wide reconfiguration against requests checked against the old root."""
    import fcntl
    directory = HOME / '.cache/piper-ssh'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def require_service_root(expected):
    expected = canonical_root(expected)
    info = _service_info()
    actual = info.get('project_root') if info else None
    if not actual:
        raise ValueError('Service projects folder could not be verified. Use Test again and Update service in Settings > Servers before connecting.')
    actual = canonical_root(actual)
    if actual != expected:
        raise ValueError(f'Projects folder differs: this connection expects {expected}; the service uses {actual}. Use Update service in Settings > Servers to change it explicitly.')


def check_service_rebind(expected):
    expected = canonical_root(expected)
    info = _service_info()
    if info and info.get('project_root') and canonical_root(info['project_root']) == expected:
        return
    if info:
        state = _service_json('/api/state')
        if not state or not isinstance(state.get('sessions'), list):
            raise RebindRefused('Could not check server sessions. Projects folder was not changed; check the service and try again.')
        active = [s for s in state['sessions'] if s.get('alive') or s.get('state') not in ('exited', 'error', 'blocked')]
        if active:
            raise RebindRefused(f"Can't update yet: {len(active)} server sessions are active. Stop them, then try Update service again. No configuration was changed.")
    elif (STATE / 'ptyhost.sock').exists():
        raise RebindRefused('Could not check server sessions while the service is unavailable. Start the existing service before changing its projects folder.')


def probe(req=None):
    linger = False
    if sys.platform == 'linux' and shutil.which('loginctl'):
        linger = run(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value']).stdout.strip() == b'yes'
    info = _service_info()
    running = info is not None
    actual = info.get('project_root') if info else None
    expected = canonical_root(req['project_path']) if req and req.get('project_path') else None
    return {'python': list(sys.version_info[:3]), 'platform': sys.platform, 'service_running': running,
            'service_installed': (APP / 'current/piper/piperd.py').is_file(),
            'systemd': bool(shutil.which('systemctl')), 'linger': linger,
            'tmux': bool(shutil.which('tmux')), 'claude': bool(shutil.which('claude')),
            'codex': bool(shutil.which('codex')), 'protocol': 1,
            'service_project_root': actual,
            'requested_project_root': expected,
            'project_root_matches': bool(actual and expected and canonical_root(actual) == expected)}


def install(req):
    with service_lock(exclusive=True):
        return _install(req)


def _install(req):
    if sys.version_info < (3, 10):
        raise ValueError('Python 3.10 or newer is required')
    if sys.platform != 'linux' or not shutil.which('systemctl'):
        raise ValueError('Automatic service installation needs Linux with systemd --user. Use SSH mode on this server.')
    if run(['systemctl', '--user', 'show-environment']).returncode:
        raise ValueError('systemd user manager is unavailable. Use SSH mode or configure the user manager first.')
    root = canonical_root(req.get('project_path'))
    if not os.path.isdir(root) or not os.access(root, os.R_OK | os.X_OK):
        raise ValueError('Projects folder does not exist or is not accessible to this SSH account.')
    check_service_rebind(root)
    raw = base64.b64decode(req['archive'], validate=True)
    if hashlib.sha256(raw).hexdigest() != req['sha']:
        raise ValueError('archive checksum mismatch')
    APP.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='release-', dir=APP))
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:
            for member in tf.getmembers():
                target = stage / member.name
                if not member.isfile() or member.name.startswith('/') or '..' in Path(member.name).parts or member.size > 16 << 20:
                    raise ValueError('unsafe archive member')
                target.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(member) as source, target.open('wb') as dest:
                    shutil.copyfileobj(source, dest)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
        (stage / 'version').write_text(req['sha'])
        # Never kill ptyhost on an update; it has the same state/socket across releases.
        runner = stage / 'serve.py'
        env = dict(PIPER_HOME=str(STATE), PIPER_PTY_SOCK=str(STATE / 'ptyhost.sock'),
                   PIPER_SERVICE_LABEL='piper-server',
                   PIPER_DEFAULT_RUNTIME='direct', PIPER_PORT=str(PORT), PIPER_BIND='127.0.0.1',
                   PROJECT_PATH=req['project_path'], PATH=os.environ.get('PATH', '') + ':' + str(HOME / '.local/bin'))
        runner.write_text('import os,sys\nos.environ.update(' + repr(env) + ')\nos.execv(sys.executable,[sys.executable,' + repr(str(APP / 'current/piper/piperd.py')) + '])\n')
        link = APP / ('current-' + uuid.uuid4().hex)
        link.symlink_to(stage.name)
        os.replace(link, APP / 'current')
        # Mint service-only API credentials on the server; they never leave it.
        tokenenv = dict(os.environ, **env)
        mint = 'import sys;sys.path.insert(0,' + repr(str(stage / 'piper')) + ');import common;common.ensure_home();common.own_token("gui")'
        if run([sys.executable, '-c', mint], env=tokenenv).returncode:
            raise ValueError('could not initialize server state')
        units = HOME / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        # Systemd quotes are separate from shell quoting; percent is a systemd specifier.
        def unitarg(v):
            return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
        unit = '[Unit]\nDescription=Piper remote session controller\n[Service]\nExecStart=' + unitarg(sys.executable) + ' ' + unitarg(APP / 'current/serve.py') + '\nRestart=on-failure\nKillMode=process\n[Install]\nWantedBy=default.target\n'
        (units / 'piper-server.service').write_text(unit)
        for args in (['daemon-reload'], ['enable', 'piper-server.service'], ['restart', 'piper-server.service']):
            if run(['systemctl', '--user'] + args).returncode:
                raise ValueError('service setup failed; inspect systemctl --user status piper-server')
        result = probe(req)
        result['installed'] = True
        result['autonomous_after_logout'] = result['linger']
        result['notice'] = '' if result['linger'] else 'Enable lingering once with: loginctl enable-linger. Without it systemd may stop the service after your last login ends.'
        return result
    except Exception:
        # Preserve a selected release and all previous versions for diagnosis/recovery.
        if not (APP / 'current').exists() or (APP / 'current').resolve() != stage:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def http(req):
    if req['mode'] == 'ssh':
        return ssh_http(req)
    with service_lock():
        require_service_root(req.get('project_path'))
        return _proxy_http(req)


def _proxy_http(req):
    token = (STATE / 'gui-token').read_text().strip()
    timeout = req.get('timeout', 28)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 2 * 3600 + 180:
        raise ValueError('Invalid service request timeout')
    connection = HTTPConnection('127.0.0.1', PORT, timeout=timeout)
    try:
        connection.request(req['method'], req['path'], base64.b64decode(req['body']),
                           {'Authorization': 'Bearer ' + token, 'Content-Type': req['ctype']})
        r = connection.getresponse()
        data = r.read(32 << 20)
        if r.status == 200 and req['method'] == 'GET' and urlsplit(req['path']).path == '/api/state':
            state = json.loads(data)
            if state.get('agent_availability') is None:
                state['agent_availability'] = service_agents()
                data = json.dumps(state).encode()
        return {'status': r.status, 'ctype': r.getheader('Content-Type', 'application/json'),
                'body': base64.b64encode(data).decode()}
    finally:
        connection.close()


def tmux(*args):
    return run(['tmux', '-L', 'piper-ssh', *args])


def ssh_session_status(sid):
    result = tmux('display-message','-p','-t','piper-'+sid,'#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}')
    if result.returncode:
        return False, None, None
    values = result.stdout.decode().strip().split('\t')
    alive = len(values) >= 2 and values[1] != '1'
    pid = int(values[0]) if values and values[0].isdigit() else None
    rc = int(values[2]) if len(values) > 2 and values[2].isdigit() else None
    return alive, pid, rc


def provider(req):
    with service_lock():
        require_service_root(req.get('project_path'))
        return _provider(req)


def _provider(req):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,2}', req.get('project','')):
        raise ValueError('invalid project key')
    if not (APP / 'current/piper/piperd.py').is_file():
        raise ValueError('install the server service before enabling a local tool provider')
    STATE.mkdir(parents=True,mode=0o700,exist_ok=True)
    path=STATE/'local-providers.json'
    import fcntl
    with (STATE/'local-providers.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=json.loads(path.read_text()) if path.exists() else {}
        config[req['project']]={k:req[k] for k in ('enabled','url','token')}
        if 'host' in req:
            if not isinstance(req['host'],str) or not re.fullmatch(r'127\.0\.0\.1:[0-9]{1,5}',req['host']):
                raise ValueError('invalid local provider Host')
            config[req['project']]['host']=req['host']
        tmp=STATE/('local-providers-'+uuid.uuid4().hex)
        tmp.write_text(json.dumps(config));tmp.chmod(0o600);os.replace(tmp,path)
    return {'ok':True}


def ssh_projects(root):
    result = []
    if not os.path.isdir(root):
        return result
    def walk(path, depth):
        if depth > 3 or len(result) >= 1000:
            return
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for e in entries:
            if e.name.startswith('.') or e.name in ('node_modules', 'target') or e.is_symlink() or not e.is_dir():
                continue
            if os.path.exists(os.path.join(e.path, '.git')):
                key = os.path.relpath(e.path, root)
                result.append(dict(key=key, title=key, path=e.path, exists=True, gitlab=[], source='ssh'))
            else:
                walk(e.path, depth + 1)
    if os.path.exists(os.path.join(root, '.git')):
        result.append(dict(key=os.path.basename(root), title=os.path.basename(root), path=root, exists=True, gitlab=[], source='ssh'))
    else:
        walk(root, 1)
    return result


def ssh_http(req):
    """Service-free tmux sessions. No Piper files/binaries/credentials are installed."""
    cache = HOME / '.cache/piper-ssh'
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = urlsplit(req['path']).path
    body = json.loads(base64.b64decode(req['body']) or b'{}')
    ps = ssh_projects(req['project_path'])
    def response(code, value):
        return dict(status=code, ctype='application/json', body=base64.b64encode(json.dumps(value).encode()).decode())
    def load(sid):
        if not re.fullmatch(r'[0-9a-f]{16}', sid):
            raise ValueError('invalid session id')
        return json.loads((cache / (sid + '.json')).read_text())
    def store(row):
        dest = cache / (row['id'] + '.json')
        tmp = cache / (row['id'] + '.tmp-' + uuid.uuid4().hex)
        tmp.write_text(json.dumps(row))
        tmp.chmod(0o600)
        os.replace(tmp, dest)
    if req['method'] == 'GET' and path == '/api/state':
        rows = []
        for f in cache.glob('*.json'):
            row = load(f.stem)
            alive, row['pid'], row['rc'] = ssh_session_status(row['id'])
            row['state'] = 'working' if alive else 'exited'
            row['process_only'] = True
            row['detail'] = 'SSH-only: agent state and approvals are visible in the terminal.' if alive else 'tmux session ended; exit code unavailable'
            rows.append(row)
        return response(200, dict(sessions=rows, projects=ps, pending=[], inbox={}, prod={}, feed={}, queue={},
                                 agent_availability={name: bool(shutil.which(name)) for name in ('claude', 'codex')},
                                 start_opts_default=[], start_opts_enforced=[], default_runtime='direct',
                                 features={'onboarded': True, 'preset': 'basic', 'list': [], 'presets': [], 'preset_modules': {}, 'modules': {k: k in ('grid','codex','composer') for k in ('gitlab','agent','grid','http','db','diag','stack','prod','timesheet','usage','mattermost','codex','github','host','composer')}},
                                 gui_flags={}, terminal_app='', gitlab_host=''))
    if req['method'] == 'POST' and path == '/api/sessions':
        p = next((p for p in ps if p['key'] == body.get('project')), None)
        agent = body.get('agent', 'claude')
        if not p or agent not in ('claude', 'codex'):
            return response(400, {'error':'choose a discovered project and Claude or Codex'})
        if body.get('opts') or body.get('runtime', 'direct') != 'direct' or body.get('image_rebuild'):
            return response(400, {'error':'SSH-only supports direct execution without Hole options'})
        executable = shutil.which(agent)
        if not executable or not shutil.which('tmux'):
            return response(400, {'error':'install tmux and the chosen agent on the server first'})
        task, title = body.get('task',''), body.get('title','')
        if not isinstance(task,str) or not isinstance(title,str) or len(task)>20000 or len(title)>200:
            raise ValueError('invalid task or title')
        sid = uuid.uuid4().hex[:16]
        foreground = ['--no-daemon'] if agent == 'codex' and b'--no-daemon' in run([executable,'--help']).stdout else []
        argv = [executable] + foreground + (['--no-alt-screen','--sandbox','workspace-write','--ask-for-approval','on-request'] if agent == 'codex' else [])
        if task:
            argv += ['--', task]
        now = time.time()
        row = dict(id=sid,kind='project',project=p['key'],path=p['path'],title=title or p['title'],task=task,
                   agent=agent,runtime='direct',state='starting',phase='',detail='',created=now,since=now,
                   pid=None,instance=None,blocks=[],attest=None,rc=None,origin='gui',claude_session=None,
                   gitlab=[],profile=None,hidden=False,notice='',claude_mode='host' if agent=='claude' else 'none',
                   task_sent=bool(task),network={},watch=[])
        # Persist before launch: a lost reply is reconciled by listing, never by replaying create.
        store(row)
        command = shlex.join(['tmux','-L','piper-ssh','set-option','-t','piper-'+sid,'remain-on-exit','on']) + '; exec ' + shlex.join(argv)
        r = tmux('new-session','-d','-s','piper-'+sid,'-c',p['path'],command)
        if r.returncode:
            row['state']='error'; row['detail']='tmux could not start the agent'; store(row)
            return response(500, {'error':row['detail'], 'id':sid})
        return response(200,row)
    match = re.fullmatch(r'/api/sessions/([0-9a-f]{16})/(stop|forget|title)', path)
    if req['method']=='POST' and match:
        sid, action = match.groups()
        row = load(sid)
        if action=='stop':
            tmux('kill-session','-t','piper-'+sid)
        elif action=='forget':
            if ssh_session_status(sid)[0]:
                return response(409, {'error':'stop the remote session before forgetting it'})
            tmux('kill-session','-t','piper-'+sid)
            (cache/(sid+'.json')).unlink()
        else:
            title=body.get('title','')
            if not isinstance(title,str) or not title.strip() or len(title)>200:
                raise ValueError('invalid title')
            row['title']=title;store(row)
        return response(200,{'ok':True})
    return response(501, {'error':'SSH-only supports session start, terminal, stop and rename. Install the server service for files, Git, hooks, resume and Piper MCP.'})


def terminal():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(STATE / 'ptyhost.sock'))
    sel = selectors.DefaultSelector()
    sel.register(sys.stdin.buffer, selectors.EVENT_READ, 'in')
    sel.register(sock, selectors.EVENT_READ, 'out')
    while True:
        for key, _ in sel.select():
            if key.data == 'in':
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    return
                sock.sendall(data)
            else:
                data = sock.recv(65536)
                if not data:
                    return
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()


def main():
    for argument in sys.argv[1:]:
        if argument.startswith('--identity='):
            peer_identity(argument.split('=', 1)[1])
    if len(sys.argv) >= 3 and sys.argv[-2] == 'attach':
        sid = sys.argv[-1]
        binary = shutil.which('tmux')
        if not binary or not re.fullmatch(r'[0-9a-f]{16}', sid):
            raise ValueError('tmux unavailable or invalid session id')
        os.execv(binary, [binary, '-L', 'piper-ssh', 'attach-session', '-t', 'piper-' + sid])
        return
    if sys.argv[-1:] == ['terminal']:
        requested = next((arg.split('=', 1)[1] for arg in sys.argv[1:] if arg.startswith('--project-root=')), None)
        with service_lock():
            require_service_root(requested)
        terminal()
        return
    try:
        req = json.load(sys.stdin)
        identity = peer_identity(req.get('expected_identity'))
        op = req.get('op')
        result = probe(req) if op == 'probe' else install(req) if op == 'install' else http(req) if op == 'http' else provider(req) if op == 'provider' else {'error':'unknown operation'}
        result['_peer_identity'] = identity
        print(json.dumps(result))
    except Exception as e:
        # Controlled error; no subprocess stderr or token values in the response.
        error = {'error': str(e) if isinstance(e, ValueError) else 'Server operation failed; inspect server configuration and service logs.'}
        if isinstance(e, RebindRefused):
            error.update(not_executed=True, _peer_identity=identity)
        print(json.dumps(error))


if __name__ == '__main__':
    main()
