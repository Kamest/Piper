"""One-shot SSH peer (sent compressed by remotes). No daemon needed for probe/SSH mode.

Server service uses its own ~/.piper-server state and native agent logins. This file
has only stdlib dependencies so the service can be bootstrapped before Piper exists.
"""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection, HTTPException
import io
import json
import os
from pathlib import Path
import re
import selectors
import signal
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
import urllib.request
import platform
import plistlib

HOME = Path.home()
def _tool_path(path):
    """~/.local/bin once: first on macOS, whose /usr/bin stubs (java, python3) must not shadow tools installed
    for Piper there; last elsewhere, as before."""
    local = str(HOME / '.local/bin')
    parts = [p for p in path.split(':') if p and p != local]
    return ':'.join([local] + parts if sys.platform == 'darwin' else parts + [local])


os.environ['PATH'] = _tool_path(os.environ.get('PATH', '/usr/bin:/bin'))
STATE = HOME / '.piper-server'
APP = HOME / '.local/share/piper-server'
PORT = 17896
SERVER_LABEL = "dev.piper.server"
MOBILE_LABEL = "dev.piper.mobile"


class RebindRefused(ValueError):
    """A reconfiguration rejected before the release or service configuration was changed."""


class NotExecuted(ValueError):
    """A request refused before anything was created on the server; the caller may retry explicitly."""


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
    if sys.platform == "darwin":
        info = _launch_info(SERVER_LABEL)
        if info.get("pid"):
            return info.get("env", {}).get(key)
        return None
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
    linger = sys.platform == "darwin"
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


# Setup operations remain self-contained: this file is sent to a bare SSH account.
SETUP_MAX = 32 << 20
SETUP_KEY = 'id_ed25519_piper'
HOLE_MIN = (2, 1, 0)
HOST_KEY_LINE = re.compile(r'(?:\|1\|[A-Za-z0-9+/=]{1,100}\|[A-Za-z0-9+/=]{1,100}|[A-Za-z0-9_.,:\[\]-]{1,300}) '
                           r'(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]{40,800}={0,3}')


def _home_path(value):
    path = Path(value).expanduser().resolve()
    if not path.is_relative_to(HOME.resolve()) or path == HOME.resolve():
        raise NotExecuted('Setup may only write inside the SSH account home.')
    return path


def _private_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.setup-')
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _setup_run(argv, timeout=20, *, cwd=None, extra_env=None):
    """Never inherit peer credentials or invoke a shell. Output stays in this peer process."""
    env = {'HOME': str(HOME), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
           'LANG': 'C.UTF-8', 'GIT_TERMINAL_PROMPT': '0'}
    # `systemctl --user` reaches the user manager through these sockets (paths, not credentials);
    # without them it fails with "Failed to connect to bus" and every service step breaks.
    runtime = os.environ.get('XDG_RUNTIME_DIR') or '/run/user/%d' % os.getuid()
    if os.path.isdir(runtime):
        env['XDG_RUNTIME_DIR'] = runtime
        bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS') or ('unix:path=%s/bus' % runtime
                                                            if os.path.exists(runtime + '/bus') else '')
        if bus:
            env['DBUS_SESSION_BUS_ADDRESS'] = bus
    env.update(extra_env or {})
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, cwd=str(cwd or HOME), env=env)


def _ok(argv, timeout=10):
    try:
        return _setup_run(argv, timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _launch_domain():
    return "user/%d" % os.getuid()


def _launch_path(label):
    return _config_path(str(HOME / "Library/LaunchAgents" / (label + ".plist")))


def _launch_info(label):
    """Read live pid/environment without exposing launchctl output."""
    try:
        reply = _setup_run(["launchctl", "print", _launch_domain() + "/" + label])
        if reply.returncode:
            return {}
        text = reply.stdout.decode("utf-8", errors="replace")
        pid = re.search(r"^\s*pid = ([0-9]+)\s*$", text, re.M)
        block = re.search(r"^\t*environment = \{\n(.*?)^\t*\}", text, re.M | re.S)
        env = dict(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*) => (.*)$", block[1], re.M)) if block else {}
        return {"pid": int(pid[1]) if pid else None, "env": env}
    except (OSError, subprocess.TimeoutExpired):
        return {}


def _launch_content(label, argv, env):
    return plistlib.dumps(dict(Label=label, ProgramArguments=[str(x) for x in argv],
                              EnvironmentVariables=dict(env, HOME=str(HOME)), WorkingDirectory=str(HOME),
                              RunAtLoad=True, KeepAlive=True, AbandonProcessGroup=True,
                              # user/<uid> is the Background session: launchd refuses an Aqua (default) agent there.
                              LimitLoadToSessionType='Background',
                              StandardOutPath=str(STATE / (label + ".log")),
                              StandardErrorPath=str(STATE / (label + ".log"))))


def _launch_write(label, content):
    path = _launch_path(label)
    _atomic_setup_file(path, content)


def _launch_read(label):
    path = _launch_path(label)
    try:
        data = plistlib.loads(path.read_bytes())
        env = data.get("EnvironmentVariables", {})
        if data.get("Label") == label and isinstance(env, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    raise NotExecuted("Invalid managed LaunchAgent; inspect " + str(path))


def _launch_restart(label):
    """Wait for asynchronous bootout before bootstrap in the SSH user domain."""
    target = _launch_domain() + "/" + label
    _setup_run(["launchctl", "bootout", target])  # absent on first install
    for _ in range(20):
        if not _ok(["launchctl", "print", target]):
            break
        time.sleep(.25)
    else:
        raise ValueError("LaunchAgent did not unload; inspect launchctl print " + target)
    for argv in (["launchctl", "enable", target],
                 ["launchctl", "bootstrap", _launch_domain(), str(_launch_path(label))]):
        if _setup_run(argv).returncode:
            raise ValueError("LaunchAgent setup failed; inspect launchctl print " + target)


def _git_public_key():
    key = _home_path(HOME / '.ssh' / SETUP_KEY)
    pub = key.with_suffix('.pub')
    if pub.is_symlink() or (HOME / '.ssh' / SETUP_KEY).is_symlink():
        raise NotExecuted('Server setup key files must not be symlinks.')
    if not key.is_file() or not pub.is_file():
        return None
    with pub.open() as source:
        value = source.read(1025).strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{40,100}={0,3}(?: [A-Za-z0-9_.-]+)?', value):
        raise NotExecuted('The server public key is invalid; inspect it on the server.')
    return value


def setup_status(req):
    result = probe(req)
    result['home'] = str(HOME)
    result['state'] = str(STATE)
    version = APP / 'current/version'
    result['bundle_sha'] = version.read_text().strip() if version.is_file() else None
    docker = bool(shutil.which('docker'))
    usable = docker and _ok(['docker', 'info', '--format', '{{.ServerVersion}}'])
    result['docker'] = {'present': docker, 'usable': bool(usable)}
    hole = bool(shutil.which('hole'))
    hv = None
    if hole:
        try:
            p = _setup_run(['hole', 'version'], 10)
            match = re.search(rb'\b(\d+\.\d+\.\d+)\b', p.stdout)
            hv = match[1].decode() if p.returncode == 0 and match else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    images = False
    if usable:
        try:
            image_reply = _setup_run(['docker', 'image', 'ls', '--filter', 'reference=hole-sandbox/agent-*',
                                      '--format', '{{.ID}}'], 10)
            images = image_reply.returncode == 0 and bool(image_reply.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass
    result['hole'] = {'present': hole, 'version': hv, 'image': images,
                      'current': bool(hv) and tuple(int(n) for n in hv.split('.')) >= HOLE_MIN}
    # CLI support varies. Only positive authentication evidence is ok; silence/errors are unknown.
    result['agent_login'] = {'claude': 'unknown', 'codex': 'unknown'}
    if shutil.which('claude'):
        try:
            auth = _setup_run(['claude', 'auth', 'status'], 10)
            data = json.loads(auth.stdout)
            if auth.returncode == 0 and isinstance(data, dict) and data.get('loggedIn') is True:
                result['agent_login']['claude'] = 'ok'
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    if shutil.which('codex') and _ok(['codex', 'login', 'status']):
        result['agent_login']['codex'] = 'ok'
    info = _service_info() or {}
    result['extensions'] = info.get('extensions')  # live daemon, not merely files on disk
    saved = STATE / 'setup-extensions.json'
    result['extensions_sha'] = json.loads(saved.read_text()).get('sha') if saved.is_file() else None
    result['git_key'] = {'present': bool(_git_public_key()), 'public_key': _git_public_key()}
    result['projects'] = ssh_projects(req.get('project_path', ''))
    checks = req.get('project_checks', [])
    if not isinstance(checks, list) or len(checks) > 100:
        raise NotExecuted('Invalid project status checks.')
    origins = {}
    for relative in checks:
        _, target, _ = _clone_target(dict(req, relative_path=relative, url='git@example.com:acme/shop.git'))
        if (target / '.git').is_dir():
            try:
                reply = _setup_run(['git', '-C', str(target), 'config', '--get', 'remote.origin.url'], 5)
                if reply.returncode == 0:
                    origins[str(target)] = hashlib.sha256(reply.stdout.strip()).hexdigest()
            except (OSError, subprocess.TimeoutExpired):
                pass
    result['project_origins'] = origins
    return result


# Configuration stays in the one-shot peer: no remote import, shell, listener or service restart.
CONFIG_FILE_MAX = 50 * 1024 * 1024
# Server files already in the mirrored folders, including ones the mirror never sends (screenshots,
# builds). Writes stay at 10000; inspection must not fail where the mirror's own budget succeeded.
CONFIG_MANIFEST_MAX = 50000
CONFIG_MAX = 128 * 1024 * 1024


def _config_unsafe(name):
    n = name.lower()
    return (n.startswith(('.env', 'id_')) or n in {'auth.json', '.claude.json', 'settings.local.json',
            'http-client.private.env.json', 'plugins', '.git', '__pycache__', 'node_modules'} or
            n.endswith(('.pem', '.key', '.p12', '.sql.gz', '.dump')) or
            bool(re.search(r'token|secret|credential|password', n)))


def _config_base(path, root=None):
    """The directory a setup path is confined to: the SSH account home, or the verified service projects
    folder (``root``) when the server keeps its projects outside home, for example /srv/projects."""
    if path.is_relative_to(HOME) and path != HOME:
        return HOME
    if root is not None and path.is_relative_to(root) and path != root:
        return root
    raise NotExecuted('Setup files must be inside the SSH account home or the service projects folder.')


def _config_path(value, root=None):
    """Lexical HOME (or verified projects folder) boundary and no symlink in any component, including the final file."""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise NotExecuted('Invalid setup file path.')
    path = Path(str(HOME) + '/' + value[2:] if value.startswith('~/') else value)
    if not path.is_absolute() or '..' in path.parts:
        raise NotExecuted('Setup files must be inside the SSH account home.')
    base = _config_base(path, root)
    cur = base
    for part in path.relative_to(base).parts:
        cur /= part
        if cur.is_symlink():
            raise NotExecuted('Setup file paths must not contain symlinks.')
    return path


def _config_root(req):
    """The projects folder the running service was installed with; only that folder is writable outside home."""
    try:
        require_service_root(req.get('project_path'))
    except ValueError as error:
        raise NotExecuted(str(error)) from None  # refused before any write
    root = Path(canonical_root(req.get('project_path')))
    if root == Path('/') or root.is_symlink():
        raise NotExecuted('Invalid service projects folder.')
    return root


def _config_scopes(req):
    scopes = req.get('scopes')
    if not isinstance(scopes, list) or len(scopes) > 2000:
        raise NotExecuted('Invalid configuration scopes.')
    root = _config_root(req)
    out = []
    for row in scopes:
        if not isinstance(row, dict) or set(row) != {'path', 'tree'} or type(row['tree']) is not bool:
            raise NotExecuted('Invalid configuration scope.')
        path = _config_path(row['path'], root)
        home = path.is_relative_to(HOME) and path != HOME
        rel = path.relative_to(HOME).parts if home else ('',)
        agent = home and rel[0] in ('.claude', '.codex') and len(rel) > 1
        runtime = {'projects', 'sessions', 'history.jsonl', 'file-history', 'paste-cache', 'debug',
                   'backups', 'todos', 'shell-snapshots', 'statsig', 'ide', 'session-env', 'plans',
                   'local', 'piper', 'piper-mailbox', '__store.db'}
        memory = home and rel[0] == '.claude' and len(rel) == 4 and rel[1] == 'projects' and rel[3] == 'memory'
        agent = agent and (memory or rel[1].lower() not in runtime) and not any(_config_unsafe(x) for x in rel[1:])
        state = path in (STATE / 'policy.json', STATE / 'projects.json') and not row['tree']
        dev = path.is_relative_to(root) and path != root and path.name == 'development' and row['tree']
        if not (agent or state or dev):
            raise NotExecuted('Configuration scope is outside the mirrored configuration roots.')
        out.append((path, row['tree']))
    return root, out


def _config_metadata(path, root=None):
    import stat
    parent = _config_parent(path, root=root)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > CONFIG_FILE_MAX:
                raise NotExecuted('Configuration target is not a bounded regular private file.')
            digest, size = hashlib.sha256(), 0
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > CONFIG_FILE_MAX:
                    raise NotExecuted('Configuration file grew during inspection.')
                digest.update(chunk)
            after = os.fstat(fd)
            if any(getattr(after, k) != getattr(st, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')):
                raise NotExecuted('Configuration target changed during inspection.')
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    return dict(sha256=digest.hexdigest(), mode=0o700 if st.st_mode & 0o111 else 0o600)


def _config_parent(path, create=False, root=None):
    _config_path(str(path), root)
    base = _config_base(path, root)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(base).parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def _config_manifest(req):
    root, scopes = _config_scopes(req)
    files, left = {}, []
    def visit(path):
        if _config_unsafe(path.name):
            left.append(dict(path=str(path), reason='secret or unsafe name')); return
        if path.is_symlink():
            left.append(dict(path=str(path), reason='symlink')); return
        if not path.exists():
            return
        if path.is_dir():
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            try:
                if path.name.lower().endswith('.env.json'):
                    def has_value(v):
                        return any(has_value(x) for x in v.values()) if isinstance(v, dict) else (
                            any(has_value(x) for x in v) if isinstance(v, list) else v not in ('', None))
                    if has_value(json.loads(_secret_text(path, root))):
                        left.append(dict(path=str(path), reason='environment JSON contains values')); return
                files[str(path)] = _config_metadata(path, root)
            except (NotExecuted, ValueError):
                left.append(dict(path=str(path), reason='unsafe, unreadable or over 50 MB'))
        if len(files) + len(left) > CONFIG_MANIFEST_MAX:
            raise NotExecuted('Configuration manifest exceeds the setup limit.')
    for path, tree in scopes:
        if not tree and path.is_dir():
            raise NotExecuted('Configuration file target is a directory.')
        visit(path)
    previous = _config_saved()
    previous = {p: v for p, v in previous.items() if any(
        Path(p) == root or tree and Path(p).is_relative_to(root) for root, tree in scopes)}
    data = dict(files=files, previous=previous, left_out=left)
    data['revision'] = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data


def configuration_manifest(req):
    with service_lock(exclusive=True):
        return _config_manifest(req)


def _config_saved():
    saved = _config_path(str(STATE / 'setup-configuration.json'))
    if not saved.exists():
        return {}
    _config_metadata(saved)
    if saved.stat().st_size > 24 << 20:
        raise NotExecuted('Invalid configuration baseline.')
    data = json.loads(saved.read_text())
    if not isinstance(data, dict) or len(data) > CONFIG_MANIFEST_MAX or not all(
            isinstance(p, str) and isinstance(v, dict) and set(v) == {'sha256', 'mode'} and
            isinstance(v['sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', v['sha256']) and
            v['mode'] in (0o600, 0o700) for p, v in data.items()):
        raise NotExecuted('Invalid configuration baseline.')
    return data


def _atomic_setup_file(path, raw, mode=0o600, root=None):
    parent = _config_parent(path, create=True, root=root)
    tmp = '.setup-' + uuid.uuid4().hex
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(tmp, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def configuration_apply(req):
    with service_lock(exclusive=True):
        before = _config_manifest(req)
        if before['revision'] != req.get('revision'):
            raise NotExecuted('Configuration plan is stale; review a new plan.')
        writes, removes = req.get('writes'), req.get('removes')
        if not isinstance(writes, dict) or not isinstance(removes, list) or len(writes) + len(removes) > 10000:
            raise NotExecuted('Invalid configuration changes.')
        root, scopes = _config_scopes(req)
        blocked = [Path(x['path']) for x in before['left_out']]
        for name in list(writes) + removes:
            path = _config_path(name, root)
            if not any(path == scope or tree and path.is_relative_to(scope) for scope, tree in scopes) or any(
                    _config_unsafe(x) for x in path.relative_to(_config_base(path, root)).parts) or any(
                    path == p or path.is_relative_to(p) for p in blocked):
                raise NotExecuted('Unsafe configuration destination.')
            if name in removes and name not in before['files']:
                raise NotExecuted('Invalid configuration removal.')
        try:
            raw = base64.b64decode(req.get('archive', ''), validate=True)
            if len(raw) > CONFIG_MAX + (4 << 20) or hashlib.sha256(raw).hexdigest() != req.get('sha'):
                raise ValueError()
            payloads, total = {}, 0
            paths = sorted(writes)
            with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
                for member in tar:
                    n = member.name
                    if not member.isfile() or not n.isdigit() or str(int(n)) != n or int(n) >= len(paths) or n in payloads or member.size > CONFIG_FILE_MAX:
                        raise ValueError()
                    data = tar.extractfile(member).read(CONFIG_FILE_MAX + 1)
                    total += len(data)
                    expected = writes[paths[int(n)]]
                    if total > CONFIG_MAX or hashlib.sha256(data).hexdigest() != expected['sha256'] or expected['mode'] not in (0o600, 0o700):
                        raise ValueError()
                    payloads[n] = data
            if len(payloads) != len(paths) or set(writes) & set(removes):
                raise ValueError()
        except Exception:
            raise NotExecuted('Invalid configuration archive or hashes.') from None
        # Validate the complete archive and every destination before the first write.
        if _config_manifest(req)['revision'] != req['revision']:
            raise NotExecuted('Configuration plan is stale; review a new plan.')
        for n, name in enumerate(paths):
            _atomic_setup_file(Path(name), payloads[str(n)], writes[name]['mode'], root)
        for name in removes:
            path = _config_path(name, root)
            parent = _config_parent(path, root=root)
            try:
                os.unlink(path.name, dir_fd=parent)
            finally:
                os.close(parent)
        saved = _config_path(str(STATE / 'setup-configuration.json'))
        previous = _config_saved()
        previous.update(before['files'])
        previous.update(writes)
        for name in removes:
            previous.pop(name, None)
        _atomic_setup_file(saved, json.dumps(previous).encode())
        return {'applied': True}


def _secret_specs(req):
    rows = req.get('secrets')
    if not isinstance(rows, list) or len(rows) > 1000:
        raise NotExecuted('Invalid server secret names.')
    out, root = {}, None
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'file', 'key'} or not isinstance(row['id'], str) or not re.fullmatch(r'[0-9a-f]{24}', row['id']) or not isinstance(row['key'], str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', row['key']):
            raise NotExecuted('Invalid server secret names.')
        try:
            path = _config_path(row['file'])
        except NotExecuted:
            # A project .env under a projects folder outside home: only the verified service folder.
            root = root or _config_root(req)
            path = _config_path(row['file'], root)
        if row['id'] in out:
            raise NotExecuted('Duplicate server secret name.')
        out[row['id']] = (path, row['key'])
    return out, root


def _secret_text(path, root=None):
    import stat
    if not path.exists():
        return ''
    parent = _config_parent(path, root=root)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 1 << 20:
                raise NotExecuted('Invalid secret target size or type.')
            with os.fdopen(fd, 'rb', closefd=False) as source:
                raw = source.read((1 << 20) + 1)
            if len(raw) > 1 << 20:
                raise NotExecuted('Secret target exceeds the setup size limit.')
            return raw.decode('utf-8')
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _secret_layout():
    path = _config_path(str(STATE / 'setup-secret-layout.json'))
    return path, json.loads(path.read_text()) if path.is_file() else {}


def _secret_env(path):
    # .env, .env.local, .env.sentry-build-plugin, prod.env: key=value lines, not placeholders.
    return path.name.startswith('.env') or path.name.endswith('.env')


def secrets_status(req):
    try:
        with service_lock(exclusive=True):
            specs, root = _secret_specs(req)
            _, layout = _secret_layout()
            states = []
            for sid, (path, key) in specs.items():
                text = _secret_text(path, root)
                if _secret_env(path):
                    match = re.search(r'(?m)^\s*(?:export\s+)?' + re.escape(key) + r'\s*=([^\r\n]*)', text)
                    parts = shlex.split(match[1], comments=True) if match else []
                    value = ' '.join(parts)
                    filled = bool(value) and not re.search(r'\{\{|\$\{', value)
                else:
                    saved = layout.get(str(path), {})
                    filled = (saved.get('sha256') == hashlib.sha256(text.encode()).hexdigest() and
                              bool(saved.get('fields', {}).get(key)) and '{{' + key + '}}' not in text)
                states.append(dict(id=sid, state='set' if filled else 'unset'))
            return {'secrets': states}
    except Exception:
        raise NotExecuted('Cannot inspect server secret targets; check paths and templates on the server.') from None


def secrets_write(req):
    """Values exist only in this request and the destination file. Layout stores offsets, no values.

    Offsets permit rotating a filled placeholder without retaining a second copy of its secret.
    A manually edited template invalidates offsets and requires placeholders to be restored.
    """
    started = False
    try:
        with service_lock(exclusive=True):
            specs, root = _secret_specs(req)
            values = req.get('values')
            if not isinstance(values, dict) or not values or set(values) - set(specs) or not all(
                    isinstance(v, str) and 0 < len(v) <= 16384 and not any(ord(c) < 32 for c in v) for v in values.values()):
                raise ValueError()
            layout_path, layout = _secret_layout()
            grouped = {}
            for sid, value in values.items():
                path, key = specs[sid]
                grouped.setdefault(path, {})[key] = value
            pending = {}
            for path, updates in grouped.items():
                text = _secret_text(path, root)
                if _secret_env(path):
                    for key, value in updates.items():
                        # Quote and escape interpolation characters; a value cannot inject a line.
                        quoted = json.dumps(value, ensure_ascii=False).replace('$', '\\$').replace('`', '\\`')
                        line = key + '=' + quoted
                        pattern = r'(?m)^\s*(?:export\s+)?' + re.escape(key) + r'\s*=[^\r\n]*'
                        text = re.sub(pattern, lambda m: line, text) if re.search(pattern, text) else text.rstrip('\n') + ('\n' if text else '') + line + '\n'
                else:
                    saved = layout.get(str(path), {})
                    fields = saved.get('fields', {}) if saved.get('sha256') == hashlib.sha256(text.encode()).hexdigest() else {}
                    spans = []
                    for key in set(fields) | set(updates):
                        positions = [list(m.span()) for m in re.finditer(re.escape('{{' + key + '}}'), text)] or fields.get(key, [])
                        if key in updates and not positions:
                            raise ValueError()
                        for start, end in positions:
                            value = updates.get(key, text[start:end])
                            if key in updates and path.suffix.lower() == '.xml':
                                from xml.sax.saxutils import escape
                                value = escape(value, {'\"': '&quot;', "'": '&apos;'})
                            spans.append((start, end, key, value))
                    chunks, cursor, length, new_fields = [], 0, 0, {}
                    for start, end, key, value in sorted(spans):
                        if start < cursor or end > len(text):
                            raise ValueError()
                        prefix = text[cursor:start]
                        chunks += [prefix, value]
                        length += len(prefix)
                        new_fields.setdefault(key, []).append([length, length + len(value)])
                        length += len(value)
                        cursor = end
                    text = ''.join(chunks) + text[cursor:]
                    layout[str(path)] = dict(sha256=hashlib.sha256(text.encode()).hexdigest(), fields=new_fields)
                pending[path] = text.encode()
            for path, raw in pending.items():
                started = True
                _atomic_setup_file(path, raw, root=root)
            _atomic_setup_file(layout_path, json.dumps(layout).encode())
            return {'written': True}
    except Exception:
        error = ValueError if started else NotExecuted
        raise error('Secret write could not be completed; inspect the server files before retrying.') from None


def setup_git_key(req):
    """A dedicated server identity; never read or return private key bytes."""
    with service_lock(exclusive=True):
        key = _home_path(HOME / '.ssh' / SETUP_KEY)
        existing = _git_public_key()
        if existing:
            return {'public_key': existing}
        if key.exists() or key.with_suffix('.pub').exists():
            raise NotExecuted('Incomplete server key; inspect it on the server before retrying.')
        if not shutil.which('ssh-keygen'):
            raise NotExecuted('Install ssh-keygen on the server first.')
        key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _setup_run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'piper-server', '-f', str(key)]).returncode:
            raise ValueError('Server key generation failed; inspect the server key before retrying.')
        key.chmod(0o600)
        return {'public_key': _git_public_key()}


def requirement_argv(argv):
    """Trusted hook argv only; shells, privilege launchers and shell wrappers are refused."""
    if not isinstance(argv, list) or not 1 <= len(argv) <= 100 or not all(
            isinstance(x, str) and len(x) <= 65536 and not any(ord(c) < 32 and c not in '\n\t' for c in x)
            for x in argv) or sum(map(len, argv)) > 100000:
        raise NotExecuted('Requirement must be a bounded argv list.')
    forbidden = {'sudo', 'doas', 'su', 'sh', 'bash', 'dash', 'zsh', 'fish', 'ksh', 'csh',
                 'env', 'xargs', 'eval', 'exec', 'busybox', 'powershell', 'pwsh'}
    if any(Path(x).name in forbidden for x in argv) or not argv[0] or argv[0].startswith('-'):
        raise NotExecuted('Shells and privilege launchers are not allowed in requirements.')
    if any(x.endswith(('.sh', '.bash', '.zsh', '.fish')) for x in argv):
        raise NotExecuted('Shell scripts are not allowed in requirements.')
    return argv


def run_requirement(req):
    argv = requirement_argv(req.get('argv'))
    timeout = req.get('timeout', 60)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise NotExecuted('Requirement timeout must be 1–300 seconds.')
    executable = shutil.which(argv[0])
    if executable:
        try:
            with open(executable, 'rb') as source:
                first = source.read(256).split(b'\n', 1)[0]
            if first.startswith(b'#!') and re.search(rb'\b(?:ba|da|z|fi|k|c)?sh\b', first):
                raise NotExecuted('Shell entrypoints are not allowed in requirements.')
        except OSError:
            pass
    # Arbitrary output cannot be proven secret-free, even after a pattern scrub. Drain a bounded
    # amount without storing it, return a short diagnostic, and kill the entire tool group on timeout.
    env = {'HOME': str(HOME), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8'}
    size, timed_out, limited = 0, False, False
    try:
        p = subprocess.Popen(argv, cwd=str(HOME), env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        return {'returncode': 127, 'timed_out': False, 'truncated': False, 'output': 'Executable unavailable.'}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)
            selector.register(p.stderr, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(left, .1)):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    size += len(chunk)
                if size > 1 << 20:
                    limited = True
                    break
        if timed_out or limited:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            rc = p.wait(timeout=max(.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            rc = p.wait(timeout=5)
    finally:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait(timeout=5)
        p.stdout.close()
        p.stderr.close()
    rc = 124 if timed_out else 125 if limited else rc
    return {'returncode': rc, 'timed_out': timed_out, 'truncated': bool(size),
            'output': ('Command output withheld (%d bytes); inspect it on the server.' % size)[:256] if size else ''}


def _projects_root(value):
    """The folder setup mirrors projects into: inside the SSH home, or the live service's verified
    projects folder when the server keeps projects elsewhere (for example /srv/projects)."""
    root = Path(canonical_root(value))
    home = HOME.resolve()
    if root.is_relative_to(home) and root != home:
        return root
    if root == Path('/'):
        raise NotExecuted('Invalid service projects folder.')
    try:
        require_service_root(str(root))
    except ValueError as error:
        raise NotExecuted(str(error)) from None  # refused before any write
    return root


def _clone_target(req):
    root = _projects_root(req.get('project_path'))
    relative = req.get('relative_path')
    if not isinstance(relative, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*){0,8}', relative) \
            or any(x in ('.', '..') for x in relative.split('/')):
        raise NotExecuted('Invalid mirrored project path.')
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or target == root:
        raise NotExecuted('Project path escapes the projects folder.')
    url = req.get('url', '')
    # SSH only: HTTP userinfo/credential helpers and local/file/ext transports never travel.
    if not isinstance(url, str) or not (re.fullmatch(r'[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+', url)
            or re.fullmatch(r'ssh://[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+(?::[0-9]{1,5})?/[A-Za-z0-9_./-]+', url)):
        raise NotExecuted('Clone needs a credential-free SSH repository URL.')
    return root, target, url


def _git_ssh_env(identity='setup'):
    """'setup': only Piper's server key. 'account': the SSH account's own ssh config and keys
    (they never leave the server). Both are batch-mode with strict host key checking."""
    if identity not in ('setup', 'account'):
        raise NotExecuted('Unknown git identity.')
    key = _home_path(HOME / '.ssh' / SETUP_KEY)
    ssh_binary = shutil.which('ssh')
    if not ssh_binary:
        raise NotExecuted('Install the SSH client on the server first.')
    # GIT_SSH (unlike core.sshCommand/GIT_SSH_COMMAND) executes a file directly, without a shell.
    wrapper = _home_path(STATE / ('setup-git-ssh' if identity == 'setup' else 'setup-git-ssh-account'))
    wrapper.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    ssh_argv = [ssh_binary, '-oBatchMode=yes', '-oStrictHostKeyChecking=yes']
    if identity == 'setup':
        ssh_argv += ['-oIdentitiesOnly=yes', '-i', str(key)]
    wrapper.write_text('#!' + sys.executable + '\nimport os,sys\nos.execv(' + repr(ssh_binary) + ', ' + repr(ssh_argv) + ' + sys.argv[1:])\n')
    wrapper.chmod(0o700)
    return {'GIT_SSH': str(wrapper), 'GIT_SSH_VARIANT': 'ssh', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}


def git_access(req):
    """Pin forge host keys the laptop already trusts (public data, append-only) and test read access.

    Idempotent: re-sending only re-checks. Never accepts a new host key on the server's own judgement."""
    lines, urls = req.get('known_hosts', []), req.get('urls', [])
    if not isinstance(lines, list) or len(lines) > 50 or not all(isinstance(x, str) and HOST_KEY_LINE.fullmatch(x) for x in lines):
        raise NotExecuted('Invalid forge host keys.')
    if not isinstance(urls, list) or len(urls) > 100:
        raise NotExecuted('Invalid repository list.')
    for url in urls:
        _clone_target(dict(req, relative_path='check', url=url))
    if not shutil.which('git'):
        raise NotExecuted('Install git on the server first.')
    with service_lock(exclusive=True):
        known = _home_path(HOME / '.ssh/known_hosts')
        if known.is_symlink():
            raise NotExecuted('known_hosts must not be a symlink.')
        present = set(known.read_text().splitlines()) if known.is_file() else set()
        missing = [x for x in dict.fromkeys(lines) if x not in present]
        if missing:
            known.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(known, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'a') as out:
                if present and known.read_bytes()[-1:] not in (b'', b'\n'):
                    out.write('\n')
                out.write('\n'.join(missing) + '\n')
        envs = [('account', _git_ssh_env('account'))] + ([('setup', _git_ssh_env('setup'))] if _git_public_key() else [])
    # The account's own SSH setup first: a server that can already read the repositories needs no new key.
    access = {}
    for url in dict.fromkeys(urls):
        access[url] = None
        for identity, env in envs:
            try:
                reply = _setup_run(['git', '-c', 'protocol.allow=never', '-c', 'protocol.ssh.allow=always',
                                    'ls-remote', '--heads', '--', url], 30, extra_env=env)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if reply.returncode == 0:
                access[url] = identity
                break
    return {'access': access, 'host_keys_added': len(missing)}


def clone_project(req):
    root, target, url = _clone_target(req)
    with service_lock(exclusive=True):
        if target.exists():
            if (target / '.git').is_dir():
                p = _setup_run(['git', '-C', str(target), 'config', '--get', 'remote.origin.url'])
                if p.returncode == 0 and p.stdout.decode().strip() == url:
                    return {'path': str(target), 'existing': True}
            raise NotExecuted('Project destination already exists and is not the requested clone.')
        identity = req.get('identity', 'setup')
        if not shutil.which('git') or (identity == 'setup' and not _git_public_key()):
            raise NotExecuted('Git and the server SSH key are required before cloning.')
        target.parent.mkdir(parents=True, exist_ok=True)
        git_env = _git_ssh_env(identity)
        argv = ['git', '-c', 'protocol.allow=never', '-c', 'protocol.ssh.allow=always', 'clone', '--', url, str(target)]
        if _setup_run(argv, 300, extra_env=git_env).returncode:
            reason = 'Clone failed. Verify the server public key, repository access and known_hosts on the server.'
            # git removes a failed clone: nothing was created, so the refusal is definite, not a lost answer.
            raise ValueError(reason) if target.exists() else NotExecuted(reason)
        return {'path': str(target), 'existing': False}


def _download(url, limit):
    if not url.startswith('https://'):
        raise NotExecuted('Downloads require HTTPS.')
    with urllib.request.urlopen(url, timeout=30) as response:
        if not response.geturl().startswith('https://'):
            raise ValueError('Download redirect requires HTTPS.')
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('Download exceeds the setup size limit.')
    return raw


def _agent_target(agent):
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64',
            'amd64': 'x86_64'}.get(platform.machine().lower())
    if sys.platform not in ('darwin', 'linux') or not arch:
        raise NotExecuted('Automatic %s installation does not support this platform. '
                          'Install it yourself in ~/.local/bin, then run Fix.' % agent)
    if sys.platform == 'darwin' and arch == 'x86_64':
        try:
            translated = _setup_run(['/usr/sbin/sysctl', '-n', 'sysctl.proc_translated'], 5)
            if translated.returncode == 0 and translated.stdout.strip() == b'1':
                arch = 'aarch64'
        except (OSError, subprocess.TimeoutExpired):
            pass
    if agent == 'codex':
        return arch + ('-apple-darwin' if sys.platform == 'darwin' else '-unknown-linux-musl')
    target = ('darwin-' if sys.platform == 'darwin' else 'linux-') + ('arm64' if arch == 'aarch64' else 'x64')
    if sys.platform == 'linux':
        try:
            glibc = bool(os.confstr('CS_GNU_LIBC_VERSION'))
        except (AttributeError, OSError, ValueError):
            glibc = False
        if not glibc or (Path('/lib') / ('libc.musl-%s.so.1' % arch)).exists():
            target += '-musl'
    return target


def _agent_path(path):
    """Do not follow existing parent links out of the install layout, including CODEX_HOME."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise NotExecuted('Invalid agent installation path: %s' % path)
    for parent in path.parents:
        if parent.is_symlink():
            raise NotExecuted('Agent installation path is a symlink: %s' % parent)
    return path


def _agent_link_path(path):
    path = _agent_path(path)
    if os.path.lexists(path) and not path.is_symlink():
        raise NotExecuted('Preserving existing %s; move it aside yourself, then run Fix.' % path)
    return path


def _agent_link(path, target):
    path = _agent_link_path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name('.%s.%s' % (path.name, uuid.uuid4().hex))
    try:
        temporary.symlink_to(target)
        _agent_link_path(path)
        os.replace(temporary, path)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


class _AgentRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != 'https':
            raise NotExecuted('Agent download redirect requires HTTPS.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@contextmanager
def _agent_response(url, agent):
    """HTTPS, including every redirect. urllib retains the SSH account's proxy configuration."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise NotExecuted('Agent downloads require credential-free HTTPS.')
    headers = {'User-Agent': 'piper-setup'}
    if parsed.hostname == 'api.github.com':
        headers['Accept'] = 'application/vnd.github+json'
    try:
        with urllib.request.build_opener(_AgentRedirect()).open(
                urllib.request.Request(url, headers=headers), timeout=30) as response:
            if urlsplit(response.geturl()).scheme != 'https':
                raise NotExecuted('Agent download redirect requires HTTPS.')
            yield response
    except (OSError, HTTPException):
        raise NotExecuted('Cannot download %s from the server. Enable outbound HTTPS to %s or configure '
                          'HTTPS_PROXY for the SSH account, then run Fix.' % (agent, parsed.hostname)) from None


def _agent_metadata(url, agent, limit=1 << 20):
    with _agent_response(url, agent) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise NotExecuted('%s release metadata exceeds the setup size limit.' % agent)
    return raw


@contextmanager
def _agent_download(url, agent, parent, checksum, limit, size=None):
    """Bound memory to one chunk; the untrusted file stays private until its full hash is checked."""
    if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
        raise NotExecuted('Invalid %s release SHA-256.' % agent)
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.download-', dir=parent)
    temporary = Path(name)
    try:
        digest, count, deadline = hashlib.sha256(), 0, time.monotonic() + 600
        with os.fdopen(fd, 'wb') as out, _agent_response(url, agent) as response:
            while True:
                if time.monotonic() > deadline:
                    raise NotExecuted('%s download exceeded the setup time limit; run Fix.' % agent)
                chunk = response.read(min(1 << 20, limit - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > limit:
                    raise NotExecuted('%s download size mismatch or setup size limit exceeded.' % agent)
                digest.update(chunk)
                out.write(chunk)
        if (size is not None and count != size) or digest.hexdigest() != checksum.lower():
            raise NotExecuted('%s download SHA-256 or size mismatch; no agent installed. Check the download/proxy, then run Fix.' % agent)
        yield temporary
    finally:
        if temporary.exists():
            temporary.unlink()


def _agent_version(binary, agent, version=None, codex_home=None):
    try:
        with binary.open('rb') as source:
            if source.read(2) == b'#!':
                raise NotExecuted('Downloaded %s entrypoint is a script; automatic setup requires a native binary.' % agent)
        reply = _setup_run([str(binary), '--version'], 30,
                           extra_env={'CODEX_HOME': str(codex_home)} if codex_home else None)
        if reply.returncode == 0 and (version is None or re.search(
                rb'(?<![0-9A-Za-z.+-])' + re.escape(version.encode()) + rb'(?![0-9A-Za-z.+-])', reply.stdout)):
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise NotExecuted('%s version verification failed; inspect the server platform, then run Fix.' % agent)


def _install_claude(target, link):
    base = 'https://downloads.claude.ai/claude-code-releases'
    version = _agent_metadata(base + '/latest', 'claude', 256).decode().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise NotExecuted('Invalid Claude Code release version.')
    manifest = json.loads(_agent_metadata(base + '/' + version + '/manifest.json', 'claude'))
    entry = manifest['platforms'][target]
    size, checksum = entry['size'], entry['checksum']
    if type(size) is not int or not 0 < size <= 600 << 20:
        raise NotExecuted('Invalid Claude Code release size (maximum 600 MB).')
    binary = _agent_path(HOME / '.local/share/claude/versions' / version)
    if os.path.lexists(binary):
        raise NotExecuted('Preserving existing %s; inspect it yourself, then run Fix.' % binary)
    with _agent_download(base + '/' + version + '/' + target + '/claude', 'claude',
                         binary.parent, checksum, size, size) as temporary:
        temporary.chmod(0o755)
        _agent_version(temporary, 'Claude Code')
        os.rename(temporary, binary)
    try:
        _agent_version(binary, 'Claude Code')
        _agent_link(link, binary)
    except Exception:
        binary.unlink()
        raise


def _codex_extract(path, stage, version, target):
    """Extract only regular package files/directories; never trust tar paths, modes or links."""
    seen, total = set(), 0
    with tarfile.open(path, mode='r:gz') as archive:
        for index, member in enumerate(archive, 1):
            parts = Path(member.name).parts
            total += member.size
            if (index > 5000 or not parts or member.name.startswith('/') or '..' in parts or
                    parts[0] not in ('bin', 'codex-path', 'codex-resources', 'codex-package.json') or
                    not (member.isdir() or member.isfile()) or member.size < 0 or total > 1 << 30 or
                    Path(member.name) in seen):
                raise NotExecuted('Unsafe or oversized Codex package archive member.')
            seen.add(Path(member.name))
            destination = stage / member.name
            if member.isdir():
                destination.mkdir(mode=0o755, parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with archive.extractfile(member) as source, destination.open('xb') as out:
                    shutil.copyfileobj(source, out, 1 << 20)
                destination.chmod(0o644)
    metadata = stage / 'codex-package.json'
    if not metadata.is_file() or metadata.stat().st_size > 1 << 20:
        raise NotExecuted('Invalid Codex package metadata.')
    package = json.loads(metadata.read_bytes())
    if (package.get('version') != version or package.get('target') != target or
            package.get('entrypoint') != 'bin/codex'):
        raise NotExecuted('Codex package version/target/entrypoint mismatch.')
    executables = ['bin/codex', 'bin/codex-code-mode-host', 'bin/codex-exec-server', 'codex-path/rg']
    if sys.platform == 'linux':
        executables.append('codex-resources/bwrap')
    for name in executables:
        binary = stage / name
        if not binary.is_file():
            raise NotExecuted('Codex package executable is missing: %s' % name)
        binary.chmod(0o755)
    (stage / 'codex').symlink_to('bin/codex')


def _install_codex(target, link):
    codex_home = Path(os.environ.get('CODEX_HOME') or HOME / '.codex').expanduser()
    root = _agent_path(codex_home / 'packages/standalone')
    current = _agent_link_path(root / 'current')
    host_link = _agent_link_path(link.with_name('codex-code-mode-host')) if sys.platform == 'darwin' else None
    marker = _agent_path(root / 'auto-update-version')
    if os.path.lexists(marker) and (marker.is_symlink() or not marker.is_file()):
        raise NotExecuted('Invalid Codex update marker path: %s' % marker)
    release = json.loads(_agent_metadata('https://api.github.com/repos/openai/codex/releases/latest', 'codex'))
    tag = release['tag_name']
    if not isinstance(tag, str) or not re.fullmatch(r'rust-v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', tag):
        raise NotExecuted('Invalid Codex release version.')
    version = tag[len('rust-v'):]
    name = version + '-' + target
    directory = _agent_path(root / 'releases' / name)
    if os.path.lexists(directory):
        raise NotExecuted('Preserving existing %s; inspect it yourself, then run Fix.' % directory)
    assets = {asset['name']: asset for asset in release['assets']}
    def asset_url(asset_name):
        url = assets[asset_name]['browser_download_url']
        if url != 'https://github.com/openai/codex/releases/download/' + tag + '/' + asset_name:
            raise NotExecuted('Invalid Codex release asset URL.')
        return url
    sums_name = 'codex-package_SHA256SUMS'
    digest = assets[sums_name]['digest']
    if not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise NotExecuted('Missing Codex SHA256SUMS release digest.')
    sums = _agent_metadata(asset_url(sums_name), 'codex')
    if hashlib.sha256(sums).hexdigest() != digest[7:].lower():
        raise NotExecuted('Codex SHA256SUMS SHA-256 mismatch; no agent installed.')
    archive_name = 'codex-package-' + target + '.tar.gz'
    matches = re.findall(r'^([0-9a-fA-F]{64})  ' + re.escape(archive_name) + r'$', sums.decode(), re.M)
    if len(matches) != 1:
        raise NotExecuted('Missing or ambiguous Codex package SHA-256.')
    with _agent_download(asset_url(archive_name), 'codex', directory.parent, matches[0], 400 << 20) as download:
        stage = Path(tempfile.mkdtemp(prefix='.staging.%s.%s.' % (name, os.getpid()), dir=directory.parent))
        try:
            _codex_extract(download, stage, version, target)
            _agent_version(stage / 'bin/codex', 'Codex', version, codex_home)
            os.rename(stage, directory)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    # All package validation happens before publication. I/O failure after this point has an
    # uncertain outcome, so leave it to durable setup reconciliation rather than a definite refusal.
    try:
        _agent_link(current, directory)
        fd, temporary = tempfile.mkstemp(prefix='.auto-update-', dir=root)
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(name)
            os.replace(temporary, marker)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        _agent_link(link, current / 'bin/codex')
        if host_link:
            _agent_link(host_link, current / 'bin/codex-code-mode-host')
        _agent_version(link, 'Codex', version, codex_home)
    except (NotExecuted, OSError):
        raise ValueError('Codex publication could not be verified; inspect the current package and command links before retrying.') from None


def install_agent(req):
    """Install missing agents from vendor-verified latest releases; existing agents self-update."""
    agent = req.get('agent')
    if agent not in ('claude', 'codex'):
        raise NotExecuted('Unknown coding agent; choose claude or codex.')
    if shutil.which(agent):
        return {'installed': True, 'existing': True}
    target = _agent_target(agent)
    link = _agent_link_path(HOME / '.local/bin' / agent)
    try:
        if agent == 'claude':
            _install_claude(target, link)
        else:
            _install_codex(target, link)
    except (KeyError, TypeError, AttributeError, UnicodeError, json.JSONDecodeError, tarfile.TarError):
        raise NotExecuted('Invalid or unsafe %s release metadata/package; inspect the download, then run Fix.' % agent) from None
    except OSError:
        raise NotExecuted('Cannot prepare %s installation paths; check the SSH account permissions and available '
                          'disk space, then run Fix.' % agent) from None
    return {'installed': True, 'existing': False}


PYTHON_RELEASE = '20261026'
PYTHON_VERSION = '3.12.16'
PYTHON_SHA256 = {
    'aarch64-apple-darwin': 'fbf4afef53c2a6b72bb741bc06d5671a9dfaf87bd84e5aa73ac9b3d0d589f14e',
    'x86_64-apple-darwin': 'a962e47df522889eaa7ff0d6a56203f1079f68aabb65ffdbe8455f362bc769f3',
    'x86_64-unknown-linux-gnu': '6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de',
    'aarch64-unknown-linux-gnu': 'a945427411dc6851a1393533b74a4a1ac434321ae397f2ec69897a9b4fde032b',
}


def _python_target():
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64'}.get(platform.machine())
    suffix = 'apple-darwin' if sys.platform == 'darwin' else None
    if sys.platform == 'linux':
        try:
            if os.confstr('CS_GNU_LIBC_VERSION').startswith('glibc '):
                suffix = 'unknown-linux-gnu'
        except (AttributeError, OSError, ValueError):
            pass
    target = '%s-%s' % (arch, suffix)
    if target not in PYTHON_SHA256:
        raise NotExecuted('Automatic Python installation does not support %s/%s (glibc Linux or macOS required). '
                          'Alternatively, install Python 3.10+ in ~/.local/bin and run Fix.' % (sys.platform, platform.machine()))
    return target


def _python_ready(directory):
    try:
        return _setup_run([str(directory / 'bin/python3.12'), '-c',
                           'import sys, ssl, sqlite3, venv; assert sys.version_info[:2] == (3, 12)'], 20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _python_extract(raw, stage):
    """Strip the python/ prefix; create links last so no file is written through a link."""
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        members = archive.getmembers()
        seen, links, total = set(), [], 0
        if len(members) > 50000:
            raise ValueError('Too many Python archive members.')
        for member in members:
            parts = Path(member.name).parts
            if not parts or parts[0] != 'python' or '..' in parts or member.name.startswith('/'):
                raise ValueError('Unsafe Python archive member.')
            if len(parts) == 1 and member.isdir():
                continue
            relative = Path(*parts[1:])
            target = stage / relative
            total += member.size
            if relative == Path('.') or relative in seen or member.size > 256 << 20 or total > 1 << 30:
                raise ValueError('Unsafe or oversized Python archive member.')
            seen.add(relative)
            if member.issym() or member.islnk():
                source = target.parent / member.linkname if member.issym() else stage / Path(*Path(member.linkname).parts[1:])
                if member.islnk() and (not member.linkname.startswith('python/') or '..' in Path(member.linkname).parts):
                    raise ValueError('Unsafe Python archive link.')
                if not source.resolve().is_relative_to(stage) or Path(member.linkname).is_absolute():
                    raise ValueError('Unsafe Python archive link.')
                links.append((member, target, source))
            elif member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('xb') as dest:
                    shutil.copyfileobj(src, dest)
                target.chmod(0o700 if member.mode & 0o111 else 0o600)
            else:
                raise ValueError('Unsafe Python archive member.')
        for member, target, source in sorted(links, key=lambda item: item[0].issym()):
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if member.issym():
                target.symlink_to(member.linkname)
            elif source.is_file() and not source.is_symlink():
                shutil.copyfile(source, target)
                target.chmod(source.stat().st_mode & 0o700)
            else:
                raise ValueError('Unsafe Python archive hard link.')
        for _, target, _ in links:
            if not target.resolve().is_relative_to(stage):
                raise ValueError('Unsafe Python archive link.')


def enable_linger(req):
    """Keep the user service running after the last SSH login ends. systemd allows it for the own account
    without sudo on common distributions; a refusal leaves linger off and setup asks the user."""
    if sys.platform == 'linux' and shutil.which('loginctl'):
        try:
            _setup_run(['loginctl', 'enable-linger'], 20)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'linger': probe(req)['linger']}


def install_python(req):
    """Publish a verified user-only runtime; never replace existing user commands or system Python."""
    target = _python_target()
    with service_lock(exclusive=True):
        directory = HOME / '.local/opt/python3.12'
        binary = directory / 'bin/python3.12'
        _home_path(directory)
        _home_path(binary)
        bindir = _home_path(HOME / '.local/bin')
        links = [bindir / name for name in ('python3.12', 'python3')]
        if directory.is_symlink():
            raise NotExecuted('Managed Python path is a symlink; inspect ~/.local/opt/python3.12 before Fix.')
        for link in links:
            if os.path.lexists(link) and (not link.is_symlink() or link.resolve() != binary.resolve()):
                raise NotExecuted('Preserving existing ~/.local/bin/%s; move it aside or provide Python 3.10+ there, then Fix.' % link.name)
        existing = directory.exists()
        if existing and not _python_ready(directory):
            raise NotExecuted('Existing ~/.local/opt/python3.12 is not working; repair or move it aside, then Fix.')
        if not existing:
            name = 'cpython-%s+%s-%s-install_only.tar.gz' % (PYTHON_VERSION, PYTHON_RELEASE, target)
            url = 'https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s' % (PYTHON_RELEASE, name.replace('+', '%2B'))
            try:
                raw = _download(url, 128 << 20)
            except Exception:
                raise NotExecuted('Cannot download Python 3.12 from the server. Enable outbound HTTPS to GitHub/release assets '
                                  'or configure the SSH account HTTPS_PROXY, then run Fix.') from None
            if hashlib.sha256(raw).hexdigest() != PYTHON_SHA256[target]:
                raise NotExecuted('Python release SHA-256 mismatch; no runtime installed. Check the server download/proxy, then Fix.')
            directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.python3.12-', dir=directory.parent)).resolve()
            try:
                try:
                    _python_extract(raw, stage)
                    if not _python_ready(stage):
                        raise ValueError('Python runtime verification failed (ssl, sqlite3, venv).')
                except (OSError, ValueError, tarfile.TarError, RuntimeError):
                    raise NotExecuted('Python archive is unsafe or the runtime verification failed (ssl, sqlite3, venv). '
                                      'No runtime installed; check the server platform/download and run Fix.') from None
                os.rename(stage, directory)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        bindir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for link in links:
            if not link.is_symlink():
                link.symlink_to(binary)
        return {'installed': True, 'existing': existing}


def install_hole(req):
    """Install a checksummed official Linux binary without running an installer shell script."""
    if sys.platform == "darwin":
        raise NotExecuted("Hole sandbox is not available on macOS; sessions run directly.")
    if sys.platform != 'linux' or not shutil.which('docker') or not _ok(['docker', 'info']):
        raise NotExecuted('A usable Docker Engine on Linux is required; Piper never installs Docker.')
    with service_lock(exclusive=True):
        binary = _home_path(HOME / '.local/bin/hole')
        current = False
        if binary.is_file():
            version = _setup_run([str(binary), 'version'], 10)
            match = re.search(rb'\b(\d+)\.(\d+)\.(\d+)\b', version.stdout)
            current = version.returncode == 0 and bool(match) and tuple(int(n) for n in match.groups()) >= HOLE_MIN
        if not current:
            arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
            if not arch:
                raise NotExecuted('This server architecture is not supported by the Hole installer.')
            release = json.loads(_download('https://api.github.com/repos/lukashornych/hole/releases/latest', 1 << 20))
            assets = {x['name']: x['browser_download_url'] for x in release.get('assets', [])}
            candidates = [n for n in assets if re.fullmatch(r'hole[_-](?:v?[0-9.]+[_-])?linux[_-]' + arch + r'(?:\.tar\.gz)?', n)]
            sums = next((n for n in assets if n.lower() in ('checksums.txt', 'sha256sums', 'sha256sums.txt')), None)
            if len(candidates) != 1 or not sums:
                raise NotExecuted('Official Hole release has no matching binary/checksum pair.')
            name = candidates[0]
            for url in (assets[name], assets[sums]):
                if not url.startswith('https://github.com/lukashornych/hole/releases/download/'):
                    raise NotExecuted('Unexpected Hole release download source.')
            checksums = _download(assets[sums], 1 << 20).decode()
            raw = _download(assets[name], SETUP_MAX)
            expected = next((line.split()[0] for line in checksums.splitlines()
                             if len(line.split()) == 2 and line.split()[1].lstrip('*') == name), None)
            if expected != hashlib.sha256(raw).hexdigest():
                raise ValueError('Hole release checksum mismatch.')
            if name.endswith('.tar.gz'):
                with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
                    files = [m for m in archive.getmembers() if m.isfile() and m.name == 'hole' and m.size <= SETUP_MAX]
                    if len(files) != 1:
                        raise ValueError('Invalid Hole binary archive.')
                    raw = archive.extractfile(files[0]).read()
            binary.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=binary.parent, prefix='.hole-')
            try:
                with os.fdopen(fd, 'wb') as out:
                    out.write(raw)
                os.chmod(tmp, 0o700)
                os.replace(tmp, binary)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        # Hole builds its images on the first sandboxed session; setup never starts an agent.
        return {'installed': True}


def sync_extensions(req):
    """Publish only a prevalidated content-addressed source snapshot; trust follows setup consent."""
    mac = sys.platform == "darwin"
    if not ((mac and shutil.which("launchctl")) or (sys.platform == 'linux' and shutil.which('systemctl'))) \
            or not (APP / 'current/piper/piperd.py').is_file():
        raise NotExecuted("Install the server service before syncing extensions (Linux/systemd or macOS/launchd).")
    if mac:
        _launch_read(SERVER_LABEL)
    try:
        raw = base64.b64decode(req.get('archive', ''), validate=True)
    except (ValueError, TypeError):
        raise NotExecuted('Invalid extension archive.') from None
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) > SETUP_MAX or sha != req.get('sha'):
        raise NotExecuted('Extension archive checksum/size mismatch.')
    files, manifests, total = {}, {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not member.isfile() or len(parts) < 2 or member.name != '/'.join(parts) or member.name.startswith('/') or '..' in parts \
                    or member.name in files or member.size > 4 << 20 or len(files) >= 10000 \
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,90}', parts[0]) \
                    or any(ord(c) < 32 for c in member.name) or ':' in member.name or '\\' in member.name \
                    or any(_unsafe_source_name(p) for p in parts):
                raise NotExecuted('Unsafe extension archive member.')
            total += member.size
            if total > SETUP_MAX:
                raise NotExecuted('Extension archive exceeds the expanded size limit.')
            files[member.name] = (archive.extractfile(member).read(), member.mode)
            if parts[1:] == ('piper-extension.json',):
                data = json.loads(files[member.name][0])
                if not isinstance(data, dict) or data.get('api') not in (1, 2) or not re.fullmatch(
                        r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', str(data.get('name', ''))):
                    raise NotExecuted('Invalid extension manifest.')
                manifests[parts[0]] = data
    roots = {Path(n).parts[0] for n in files}
    if roots != set(manifests) or any(root + '/extension.py' not in files for root in roots):
        raise NotExecuted('Incomplete extension snapshot.')
    with service_lock(exclusive=True):
        require_service_root(req.get('project_path'))
        directory = _home_path(APP / 'extensions' / sha)
        if not directory.exists():
            directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.extensions-', dir=directory.parent))
            try:
                for name, (data, mode) in files.items():
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o700 if mode & 0o111 else 0o600)
                os.replace(stage, directory)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        paths = [str(directory / root) for root in sorted(roots)]
        trust_path = _home_path(STATE / 'trusted-extensions.json')
        trust = json.loads(trust_path.read_text()) if trust_path.exists() else {}
        # Retain unrelated explicit server trusts; retire only our previous managed snapshots.
        managed = _home_path(APP / 'extensions')
        trust = {p: v for p, v in trust.items() if not Path(p).is_relative_to(managed)}
        trust.update({str(directory / root): {'name': data['name'], 'remote': '', 'at': time.time()}
                      for root, data in manifests.items()})
        _private_json(trust_path, trust)
        if mac:
            agent = _launch_read(SERVER_LABEL)
            agent.setdefault("EnvironmentVariables", {})["PIPER_EXTENSIONS"] = ':'.join(paths)
            _launch_write(SERVER_LABEL, plistlib.dumps(agent))
            _launch_restart(SERVER_LABEL)
        else:
            drop = _home_path(HOME / '.config/systemd/user/piper-server.service.d/20-extensions.conf')
            drop.parent.mkdir(parents=True, exist_ok=True)
            value = ':'.join(paths).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
            drop.write_text('[Service]\nEnvironment="PIPER_EXTENSIONS=' + value + '"\n')
            drop.chmod(0o600)
            for argv in (['systemctl', '--user', 'daemon-reload'], ['systemctl', '--user', 'restart', 'piper-server.service']):
                if _setup_run(argv).returncode:
                    raise ValueError('Extension service update failed; inspect the user service on the server.')
        # Verify the *new* live daemon; a failed import cannot masquerade as successful provisioning.
        names = sorted(data['name'] for data in manifests.values())
        for _ in range(20):
            info = _service_info() or {}
            if sorted(info.get('extensions') or []) == names and _service_env('PIPER_EXTENSIONS') == ':'.join(paths):
                _private_json(_home_path(STATE / 'setup-extensions.json'), {'sha': sha, 'names': names})
                return {'sha': sha, 'extensions': names}
            time.sleep(.25)
        raise ValueError('Extensions were published but their live loading could not be verified.')


def _unsafe_source_name(name):
    lower = name.lower()
    return name.startswith('.') or lower == 'auth.json' or bool(re.search(r'(token|secret|credential|password)', lower)) \
        or lower.startswith('id_') or lower.endswith(('.pem', '.key', '.p12', '.dump', '.env.json', '.sql.gz'))


def _mobile_config(req):
    """Only a public relay origin travels. No relay capabilities or caller-supplied commands."""
    if req.get('mode') != 'service':
        raise NotExecuted('Phone backup needs service mode.')
    value, name = req.get('relay'), req.get('name')
    try:
        u = urlsplit(value) if isinstance(value, str) else None
        valid = (u and u.scheme == 'https' and u.hostname and not u.username and not u.password
                 and u.path in ('', '/') and not u.query and not u.fragment and u.port != 0
                 and not re.search(r'[\\\s\x00-\x1f\x7f-\uffff]', value))
    except ValueError:
        valid = False
    if not valid:
        raise NotExecuted('Phone backup requires a public HTTPS relay origin without credentials.')
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise NotExecuted('Phone backup needs a server label of 1–80 printable characters.')
    return value.rstrip('/'), name


def _mobile_paths():
    release = _home_path(APP / 'current')
    runtime = HOME / '.piper/mobile-venv'
    unit = _launch_path(MOBILE_LABEL) if sys.platform == "darwin" else HOME / '.config/systemd/user/piper-mobile.service'
    for path in (runtime, unit):
        _home_path(path)
        if path.is_symlink():
            raise NotExecuted('Managed mobile runtime and unit must not be symlinks.')
    return release, runtime, unit


def _mobile_pins(release):
    path = release / 'piper/mobile-requirements.txt'
    if not (release / 'piper/mobile.py').is_file() or not path.is_file() or path.stat().st_size > 4096:
        raise NotExecuted('Update the server service first; mobile runtime files are missing.')
    lines = [x.strip() for x in path.read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
    if not lines or not all(re.fullmatch(r'[A-Za-z0-9_-]+==[0-9]+(?:\.[0-9]+)*', x) for x in lines):
        raise NotExecuted('Mobile requirements must contain pinned package versions only.')
    return dict(x.split('==') for x in lines)


def _mobile_runtime(release, runtime):
    if not (runtime / 'bin/python').is_file() or not (runtime / 'pyvenv.cfg').is_file():
        return False
    pins = _mobile_pins(release)
    code = 'import importlib.metadata as m; assert all(m.version(k)==v for k,v in ' + repr(pins) + '.items())'
    return _ok([str(runtime / 'bin/python'), '-c', code], 15)


def _mobile_unit(relay, name, runtime, release):
    if sys.platform == "darwin":
        return _launch_content(MOBILE_LABEL,
                               [runtime / "bin/python", release / "piper/mobile.py", "serve", "--relay", relay, "--name", name],
                               dict(PIPER_HOME=str(STATE), PIPER_PORT=str(PORT), PIPER_PTY_SOCK=str(STATE / "ptyhost.sock"),
                                    PATH=os.environ.get("PATH", "/usr/bin:/bin"))).decode()
    # systemd expands specifiers and ExecStart environment references even without a shell.
    def arg(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    argv = [runtime / 'bin/python', release / 'piper/mobile.py', 'serve', '--relay', relay, '--name', name]
    return ('[Unit]\nDescription=Piper phone backup relay bridge\nAfter=piper-server.service\n'
            '[Service]\nEnvironment=' + arg('PIPER_HOME=' + str(STATE)) + '\nEnvironment=PIPER_PORT=' + str(PORT) +
            '\nEnvironment=' + arg('PIPER_PTY_SOCK=' + str(STATE / 'ptyhost.sock')) +
            '\nExecStart=:' + ' '.join(arg(x) for x in argv) +
            '\nRestart=on-failure\nRestartSec=5\nKillMode=process\n[Install]\nWantedBy=default.target\n')


def _mobile_status(req):
    relay, name = _mobile_config(req)
    release, runtime, unit = _mobile_paths()
    matches = unit.is_file() and unit.read_text() == _mobile_unit(relay, name, runtime, release)
    mac = sys.platform == "darwin"
    loaded = _launch_info(MOBILE_LABEL) if mac else {}
    active = bool(loaded.get("pid")) if mac else _ok(['systemctl', '--user', 'is-active', '--quiet', 'piper-mobile.service'])
    paired = 0
    try:
        with (STATE / 'mobile.json').open() as source:
            mobile = json.load(source)
        # Only the count: device ids and keys stay on the server.
        devices = mobile.get('devices') if isinstance(mobile, dict) else None
        paired = len(devices) if isinstance(devices, (dict, list)) else 0
        code, status = _service_call('GET', '/api/mobile', timeout=5)
        ready = code == 200 and isinstance(status, dict) and status.get('ready') is True
        # A restarting bridge may still report the previous process's readiness briefly.
        ready = ready and mobile.get('relay') == relay and mobile.get('name') == name
    except (OSError, ValueError, AttributeError):
        ready = False
    return {'venv_ok': _mobile_runtime(release, runtime), 'unit_active': active,
            'relay_configured': matches, 'ready': ready, 'paired': paired,
            'unit_enabled': bool(loaded) if mac else _ok(['systemctl', '--user', 'is-enabled', '--quiet', 'piper-mobile.service'])}


def mobile_status(req):
    with service_lock():
        require_service_root(req.get('project_path'))
        return _mobile_status(req)


def setup_mobile(req):
    """Idempotent, user-only runtime and bridge; never restart the controller or PTY host."""
    relay, name = _mobile_config(req)
    mac = sys.platform == "darwin"
    if not ((mac and shutil.which("launchctl")) or (sys.platform == 'linux' and shutil.which('systemctl'))):
        raise NotExecuted("Phone backup requires Linux with systemd --user or macOS with launchd.")
    with service_lock(exclusive=True):
        require_service_root(req.get('project_path'))
        release, runtime, unit = _mobile_paths()
        _mobile_pins(release)
        if mac:
            if not _ok(["launchctl", "print", _launch_domain()]):
                raise NotExecuted("The launchd user domain is unavailable; configure it before setting up phone backup.")
        elif not _ok(['systemctl', '--user', 'show-environment']):
            raise NotExecuted('The systemd user manager is unavailable.')
        changed = not _mobile_runtime(release, runtime)
        if changed:
            runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            failed = 'Mobile runtime installation failed or timed out; inspect Python venv and package access on the server.'
            pip = [str(runtime / 'bin/python'), '-m', 'pip']
            if run_requirement({'argv': [sys.executable, '-m', 'venv', str(runtime)], 'timeout': 60})['returncode']:
                # Debian/Ubuntu ship ensurepip separately (python3-venv) and installing it needs root. The
                # account's own pip can fill a pip-less venv instead; setup never runs sudo.
                shutil.rmtree(runtime, ignore_errors=True)
                if run_requirement({'argv': [sys.executable, '-m', 'venv', '--without-pip', str(runtime)], 'timeout': 60})['returncode'] \
                        or run_requirement({'argv': [sys.executable, '-m', 'pip', '--version'], 'timeout': 60})['returncode']:
                    raise ValueError(failed)
                pip = [sys.executable, '-m', 'pip', '--python', str(runtime / 'bin/python')]
            reply = run_requirement({'argv': pip + ['install', '--disable-pip-version-check', '--no-input', '-r',
                                                    str(release / 'piper/mobile-requirements.txt')], 'timeout': 300})
            if reply['returncode']:
                raise ValueError(failed)
            if not _mobile_runtime(release, runtime):
                raise ValueError('Mobile runtime versions could not be verified.')
        content = _mobile_unit(relay, name, runtime, release)
        if not unit.is_file() or unit.read_text() != content:
            if mac:
                _launch_write(MOBILE_LABEL, content.encode())
            else:
                unit.parent.mkdir(parents=True, exist_ok=True)
                unit.write_text(content)
                unit.chmod(0o600)
            changed = True
        if mac:
            if changed or not _launch_info(MOBILE_LABEL).get("pid"):
                _launch_restart(MOBILE_LABEL)
            return {'installed': True}
        commands = [['daemon-reload'], ['enable', 'piper-mobile.service']]
        if changed or not _ok(['systemctl', '--user', 'is-active', '--quiet', 'piper-mobile.service']):
            commands.append(['restart', 'piper-mobile.service'])
        for argv in commands:
            if _setup_run(['systemctl', '--user'] + argv).returncode:
                raise ValueError('Mobile user service setup failed; inspect systemctl --user status piper-mobile.')
        return {'installed': True}


def mobile_pair(req):
    """Return the server's exact Settings invitation, never store/log a bearer link in the peer."""
    with service_lock():
        require_service_root(req.get('project_path'))
        if not all(_mobile_status(req).values()):
            raise NotExecuted('Set up phone backup and wait for the relay bridge to become ready first.')
        # The GUI bearer is minted on this server by install; it never travels over SSH.
        try:
            code, reply = _service_call('POST', '/api/mobile/invite', {'label': 'Phone backup'}, timeout=5)
            if code != 200 or not isinstance(reply, dict) or set(reply) != {'url', 'id', 'expires'}:
                raise ValueError()
            return reply
        except Exception:
            raise ValueError('Phone invitation is unavailable; check the server mobile bridge.') from None


def install(req):
    with service_lock(exclusive=True):
        return _install(req)


def _install(req):
    # Prerequisites are checked before anything is written: a refusal is definite, never an unknown outcome.
    if sys.version_info < (3, 10):
        raise NotExecuted('Python 3.10 or newer is required on the server (found %d.%d). Install it in ~/.local/bin and run Fix again.'
                          % sys.version_info[:2])
    mac = sys.platform == "darwin"
    launch_env = {}
    if mac:
        if not shutil.which("launchctl") or not _ok(["launchctl", "print", _launch_domain()]):
            raise NotExecuted("launchd user domain is unavailable. Configure user/%d before installing the service." % os.getuid())
        path = _launch_path(SERVER_LABEL)
        if path.exists():
            launch_env = _launch_read(SERVER_LABEL).get("EnvironmentVariables", {})
    elif sys.platform != 'linux' or not shutil.which('systemctl'):
        raise NotExecuted('Automatic service installation needs Linux with systemd --user or macOS with launchd (this server is %s). '
                          'Use SSH mode on this server.' % sys.platform)
    elif run(['systemctl', '--user', 'show-environment']).returncode:
        raise NotExecuted('systemd user manager is unavailable. Use SSH mode or configure the user manager first.')
    root = canonical_root(req.get('project_path'))
    create_root = req.get('setup') is True and not os.path.exists(root)
    if create_root:
        _home_path(root)
    elif not os.path.isdir(root) or not os.access(root, os.R_OK | os.X_OK):
        raise ValueError('Projects folder does not exist or is not accessible to this SSH account.')
    check_service_rebind(root)
    raw = base64.b64decode(req['archive'], validate=True)
    if hashlib.sha256(raw).hexdigest() != req['sha']:
        raise ValueError('archive checksum mismatch')
    if create_root:
        Path(root).mkdir(mode=0o700, parents=True)
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
                   PROJECT_PATH=req['project_path'], PATH=_tool_path(os.environ.get('PATH', '')))
        runner.write_text('import os,sys\nos.environ.update(' + repr(env) + ')\nos.execv(sys.executable,[sys.executable,' + repr(str(APP / 'current/piper/piperd.py')) + '])\n')
        link = APP / ('current-' + uuid.uuid4().hex)
        link.symlink_to(stage.name)
        os.replace(link, APP / 'current')
        # Mint service-only API credentials on the server; they never leave it.
        tokenenv = dict(os.environ, **env)
        mint = 'import sys;sys.path.insert(0,' + repr(str(stage / 'piper')) + ');import common;common.ensure_home();common.own_token("gui")'
        if run([sys.executable, '-c', mint], env=tokenenv).returncode:
            raise ValueError('could not initialize server state')
        if mac:
            launch_env.update(env)
            _launch_write(SERVER_LABEL, _launch_content(SERVER_LABEL, [sys.executable, APP / 'current/serve.py'], launch_env))
            _launch_restart(SERVER_LABEL)
        else:
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


def transfer(req):
    """"Move to server": unpack Piper's handoff document and the session facts into
    STATE/transfers/<id>/ and start the continuing session through the service. Only these two files
    travel (no working tree, .env, tokens or logins). A session of the same transfer id is returned
    instead of a second one."""
    if not req.get('expected_identity'):
        raise NotExecuted('Moving work needs a verified server identity; no session was created.')
    if req.get('mode') != 'service':
        raise NotExecuted('Moving work needs the server service; no session was created.')
    with service_lock():
        try:
            require_service_root(req.get('project_path'))
        except ValueError as e:
            raise NotExecuted(str(e)) from None
        return _transfer(req)


def _service_call(method, path, body=None, timeout=30):
    token = (STATE / 'gui-token').read_text().strip()
    connection = HTTPConnection('127.0.0.1', PORT, timeout=timeout)
    try:
        connection.request(method, path, json.dumps(body).encode() if body is not None else None,
                           {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        r = connection.getresponse()
        return r.status, json.loads(r.read(32 << 20) or b'{}')
    finally:
        connection.close()


def _transfer(req):
    tid = req.get('id')
    if not isinstance(tid, str) or not re.fullmatch(r'[0-9a-f]{16}', tid):
        raise NotExecuted('invalid transfer id')
    info = _service_info() or {}
    if not (info.get('capabilities') or {}).get('transfer'):
        raise NotExecuted('This server service cannot take moved work yet. Use Update service in Settings > Servers.')
    raw = base64.b64decode(req.get('archive', ''), validate=True)
    if hashlib.sha256(raw).hexdigest() != req.get('sha'):
        raise NotExecuted('archive checksum mismatch')
    state = _service_json('/api/state') or {}
    existing = next((x for x in state.get('sessions') or [] if isinstance(x, dict)
                     and (x.get('transferred_from') or {}).get('id') == tid), None)
    if existing:
        return {'session': existing['id'], 'existing': True}
    allowed = {'handoff.md', 'transfer.json'}
    target = STATE / 'transfers' / tid
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tf:
        for member in tf.getmembers():
            if not member.isfile() or member.name not in allowed or member.name in seen or member.size > 4 << 20:
                raise NotExecuted('unsafe transfer archive member')
            seen.add(member.name)
            tmp = target / (member.name + '.tmp-' + uuid.uuid4().hex)
            with tf.extractfile(member) as source, tmp.open('wb') as dest:
                shutil.copyfileobj(source, dest)
            tmp.chmod(0o600)
            os.replace(tmp, target / member.name)
    if seen != allowed:
        raise NotExecuted('incomplete transfer archive')
    meta = json.loads((target / 'transfer.json').read_text())
    if not isinstance(meta, dict) or meta.get('id') != tid:
        raise NotExecuted('transfer.json does not match the transfer id')
    body = {'project': req.get('project') or meta.get('project'), 'agent': req.get('agent') or meta.get('agent') or 'claude',
            'title': str(req.get('title') or meta.get('title') or '')[:200], 'transfer': tid}
    status, reply = _service_call('POST', '/api/sessions', body)
    if status not in (200, 202) or not isinstance(reply, dict) or not reply.get('id'):
        # the service refused before creating anything (unknown project, agent missing, limit)
        raise NotExecuted('The server did not start the session: %s' % str((reply or {}).get('error') or status)[:300])
    return {'session': reply['id']}


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
        setup_ops = {'setup_status': setup_status, 'install_python': install_python, 'install_hole': install_hole,
                     'install_agent': install_agent, 'enable_linger': enable_linger, 'setup_git_key': setup_git_key,
                     'sync_extensions': sync_extensions, 'run_requirement': run_requirement, 'clone_project': clone_project,
                     'git_access': git_access, 'configuration_manifest': configuration_manifest,
                     'configuration_apply': configuration_apply, 'secrets_status': secrets_status, 'secrets_write': secrets_write,
                     'mobile_status': mobile_status, 'setup_mobile': setup_mobile, 'mobile_pair': mobile_pair}
        if op in setup_ops:
            if op != 'setup_status' and not req.get('expected_identity'):
                raise NotExecuted('Setup writes need a verified server identity.')
            result = setup_ops[op](req)
        else:
            result = probe(req) if op == 'probe' else install(req) if op == 'install' else http(req) if op == 'http' else provider(req) if op == 'provider' else transfer(req) if op == 'transfer' else {'error':'unknown operation'}
        result['_peer_identity'] = identity
        print(json.dumps(result))
    except Exception as e:
        # Controlled error; no subprocess stderr or token values in the response.
        error = {'error': str(e) if isinstance(e, ValueError) else 'Server operation failed; inspect server configuration and service logs.'}
        if isinstance(e, (RebindRefused, NotExecuted)):
            error.update(not_executed=True, _peer_identity=identity)
        print(json.dumps(error))


if __name__ == '__main__':
    main()
