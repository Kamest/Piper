"""OS boundary for one native HTTP run; no unsandboxed fallback.

The broker owns network access. The JVM sees only its snapshot/runtime and connects
only to a per-run SOCKS endpoint. No credentials are inherited from piperd's env.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import select
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


class IsolationError(ValueError):
    pass


class OutputLimit(IsolationError):
    pass


MAX_CONSOLE = 32 << 20


def collect_output(process, timeout: float, limit: int = MAX_CONSOLE, *, on_stdout=None):
    """Drain both native pipes with a fixed aggregate memory budget. Never stop
    draining a running child or retry requests because output was too large."""
    buffers = [bytearray(), bytearray()]
    kept, cut = 0, False
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for index, stream in enumerate((process.stdout, process.stderr)):
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired('ijhttp', timeout)
            for key, _ in selector.select(min(.2, remaining)):
                data = os.read(key.fd, 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                take = min(len(data), max(0, limit - kept))
                buffers[key.data].extend(data[:take])
                kept += take
                cut |= take < len(data)
                if key.data == 0 and on_stdout and take:
                    on_stdout(data[:take])
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    if cut:
        raise OutputLimit('ijhttp output exceeds %d MiB; requests may already have run; not retried' % (limit >> 20))
    return bytes(buffers[0]), bytes(buffers[1])


def host_name(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value.rstrip('.').encode('idna').decode('ascii').lower()


class Egress:
    """SOCKS5/HTTP CONNECT and WS upgrade, exact destination, bounded lifetime/concurrency.

    DNS is performed here, never by the confined JVM. Connections carry native HTTP
    and TLS bytes unchanged. There is no retry and no logging of payloads/credentials.
    """
    def __init__(self, targets, timeout: float, *, unix: bool = False, connect_timeout: float = 10):
        self.targets = {(host_name(h), int(p)) for h, p in targets}
        self.deadline = time.monotonic() + timeout
        self.connect_timeout = connect_timeout
        self.failures = {}
        self.connected = set()
        self.lock = threading.Lock()
        self.sockets = set()
        self.slots = threading.BoundedSemaphore(32)
        self.closed = threading.Event()
        self.directory = tempfile.mkdtemp(prefix='hpx-') if unix else ''
        self.unix_path = os.path.join(self.directory, 'proxy.sock') if unix else ''
        self.listener = socket.socket(socket.AF_UNIX if unix else socket.AF_INET)
        self.listener.bind(self.unix_path if unix else ('127.0.0.1', 0))
        self.listener.listen(32)
        self.listener.settimeout(.2)
        self.port = 1080 if unix else self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    @staticmethod
    def _read(sock, count):
        result = bytearray()
        while len(result) < count:
            data = sock.recv(count - len(result))
            if not data:
                raise OSError('closed SOCKS connection')
            result.extend(data)
        return bytes(result)

    def _accept(self):
        while not self.closed.is_set() and time.monotonic() < self.deadline:
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self.slots.acquire(blocking=False):
                client.close()
                continue
            with self.lock:
                self.sockets.add(client)
            threading.Thread(target=self._connect, args=(client,), daemon=True).start()

    def _http_target(self, client, first):
        """Parse one bounded proxy handshake, without logging headers or URLs."""
        import re
        import urllib.parse
        block = bytearray(first)
        while not block.endswith(b'\r\n\r\n'):
            if len(block) >= 16384:
                raise ValueError('HTTP proxy header limit')
            block.extend(self._read(client, 1))
        lines = bytes(block[:-4]).split(b'\r\n')
        if len(lines) > 65 or len(lines[0]) > 2048:
            raise ValueError('HTTP proxy header limit')
        method, target, version = lines[0].decode('ascii').split(' ')
        if method not in ('CONNECT', 'GET') or version != 'HTTP/1.1':
            raise ValueError('unsupported HTTP proxy handshake')
        headers = []
        for line in lines[1:]:
            name, value = line.split(b':', 1)
            if (not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) or
                    any(c < 32 and c != 9 or c == 127 for c in value)):
                raise ValueError('invalid HTTP proxy header')
            headers.append((name.lower(), value.strip(), line))
        hosts = [value.decode('ascii') for name, value, _ in headers if name == b'host']
        if len(hosts) != 1 or any(name == b'transfer-encoding' or name == b'content-length' and value != b'0' for name, value, _ in headers):
            raise ValueError('invalid HTTP proxy framing')
        if method == 'CONNECT':
            parsed = urllib.parse.urlsplit('http://' + target)
            if parsed.path or parsed.query or parsed.fragment or not parsed.port:
                raise ValueError('invalid HTTP CONNECT authority')
            initial = b''
        else:
            if target.startswith('/'):
                parsed = urllib.parse.urlsplit('http://' + hosts[0] + target)
            else:
                parsed = urllib.parse.urlsplit(target)
            if parsed.scheme not in ('http', 'ws') or parsed.fragment:
                raise ValueError('invalid WebSocket proxy URL')
            upgrade = [value.lower() for name, value, _ in headers if name == b'upgrade']
            connection = b','.join(value.lower() for name, value, _ in headers if name == b'connection').split(b',')
            if upgrade != [b'websocket'] or b'upgrade' not in [token.strip() for token in connection]:
                raise ValueError('HTTP proxy GET requires WebSocket upgrade')
            path = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
            initial = b'GET ' + path.encode('ascii') + b' HTTP/1.1\r\n' + b'\r\n'.join(
                line for name, _, line in headers if name not in (b'proxy-authorization', b'proxy-connection')) + b'\r\n\r\n'
        host, port = parsed.hostname, parsed.port or 80
        header_target = urllib.parse.urlsplit('http://' + hosts[0])
        if (not host or parsed.username is not None or parsed.password is not None or
                header_target.username is not None or header_target.password is not None or
                header_target.path or header_target.query or header_target.fragment or
                host_name(header_target.hostname or '') != host_name(host) or (header_target.port or 80) != port or not 1 <= port <= 65535):
            raise ValueError('ambiguous HTTP proxy destination')
        return host, port, initial, method == 'CONNECT'

    def _connect(self, client):
        upstream = None
        try:
            client.settimeout(min(10, max(.1, self.deadline - time.monotonic())))
            first = self._read(client, 1)
            http_proxy, initial, connect = first != b'\x05', b'', False
            if http_proxy:
                if first not in (b'C', b'G'):
                    return
                host, port, initial, connect = self._http_target(client, first)
            else:
                count = self._read(client, 1)[0]
                methods = self._read(client, count)
                if 0 not in methods:
                    client.sendall(b'\x05\xff')
                    return
                client.sendall(b'\x05\x00')
                version, command, reserved, kind = self._read(client, 4)
                if version != 5 or command != 1 or reserved:
                    return
                if kind == 1:
                    host = socket.inet_ntop(socket.AF_INET, self._read(client, 4))
                elif kind == 4:
                    host = socket.inet_ntop(socket.AF_INET6, self._read(client, 16))
                elif kind == 3:
                    host = self._read(client, self._read(client, 1)[0]).decode('ascii')
                else:
                    return
                port = struct.unpack('!H', self._read(client, 2))[0]
            if (host_name(host), port) not in self.targets or self.closed.is_set():
                with self.lock:
                    self.failures[(host_name(host), port)] = 'destination not approved'
                client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n' if http_proxy else b'\x05\x02\x00\x01' + b'\x00' * 6)
                return
            try:
                upstream = socket.create_connection((host, port), timeout=min(self.connect_timeout, max(.001, self.deadline - time.monotonic())))
            except OSError as error:
                with self.lock:
                    self.failures[(host_name(host), port)] = ('connection refused' if isinstance(error, ConnectionRefusedError)
                                                            else 'connection timed out' if isinstance(error, TimeoutError)
                                                            else 'connection failed')
                client.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n' if http_proxy else b'\x05\x05\x00\x01' + b'\x00' * 6)
                return
            with self.lock:
                if self.closed.is_set() or time.monotonic() >= self.deadline:
                    return
                self.sockets.add(upstream)
                self.connected.add((host_name(host), port))
            for s in (client, upstream):
                s.settimeout(max(.001, self.deadline - time.monotonic()))
            if self.closed.is_set() or time.monotonic() >= self.deadline:
                return
            if initial:
                upstream.sendall(initial)
            elif connect:
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                client.sendall(b'\x05\x00\x00\x01' + b'\x00' * 6)
            while not self.closed.is_set() and time.monotonic() < self.deadline:
                ready, _, _ = select.select([client, upstream], [], [], .2)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except (OSError, ValueError, UnicodeError):
            pass
        finally:
            with self.lock:
                for s in (client, upstream):
                    if s:
                        self.sockets.discard(s)
                        s.close()
            self.slots.release()

    def close(self):
        self.closed.set()
        self.listener.close()
        with self.lock:
            for s in self.sockets:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()
            self.sockets.clear()
        self.thread.join(timeout=1)
        if self.directory:
            shutil.rmtree(self.directory, ignore_errors=True)


def runtime(binary: str, env: dict) -> tuple[str, str]:
    """Use the official launcher's fixed Java entry point, without executing a shell.

    Custom shell launchers are deliberately not interpreted: their extra capabilities
    cannot be inferred safely. The configured installation must contain lib/ijhttp.jar.
    """
    lib = os.path.join(os.path.dirname(os.path.realpath(binary)), 'lib')
    java_home = env.get('JAVA_HOME', '')
    java = os.path.realpath(os.path.join(java_home, 'bin', 'java') if java_home else
                            (shutil.which('java', path=env.get('PATH')) or '/nonexistent/java'))
    if not os.path.isfile(java) or not os.access(java, os.X_OK):
        raise IsolationError('ijhttp needs Java 17 or newer (Linux / WSL: sudo apt install openjdk-17-jre-headless)')
    if not java.endswith('/bin/java') or not os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(java)), 'release')):
        raise IsolationError('HTTP requires a complete JDK/JRE with bin/java and release metadata')
    if not os.path.isfile(os.path.join(lib, 'ijhttp.jar')):
        raise IsolationError('isolated HTTP requires the official ijhttp installation (lib/ijhttp.jar); no request was sent')
    return java, os.path.realpath(lib)


def clean_env(work: str) -> dict:
    return {'HOME': os.path.join(work, 'runtime', 'home'), 'TMPDIR': os.path.join(work, 'runtime', 'tmp'),
            'PATH': '/nonexistent', 'LANG': 'C.UTF-8' if sys.platform.startswith('linux') else 'en_US.UTF-8',
            'LC_CTYPE': 'C.UTF-8' if sys.platform.startswith('linux') else 'en_US.UTF-8'}


def java_argv(java: str, lib: str, work: str, args: list[str], compat: str = '', *, timeout: float = 3600) -> list[str]:
    parser = args == ['--piper-parse-modules']
    if parser and not compat:
        raise IsolationError('parse-only modules require the supported native CLI')
    proxy_port = 0
    if '--proxy' in args:
        import urllib.parse
        proxy = urllib.parse.urlsplit(args[args.index('--proxy') + 1])
        if proxy.scheme != 'socks' or proxy.hostname != '127.0.0.1' or not proxy.port:
            raise IsolationError('HTTP proxy does not match the confined loopback broker')
        proxy_port = proxy.port
    return [java, '--add-opens=java.base/java.util=ALL-UNNAMED',
            '-Dpiper.http.proxy=' + str(proxy_port),
            '-Dpiper.http.insecure=' + ('true' if '--insecure' in args else 'false'),
            '-Djava.awt.headless=true', '-Dfile.encoding=UTF-8', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8',
            '-Dpiper.http.work=' + work,
            '-Dpiper.http.rpc.timeout.ms=' + str(max(1, int(timeout * 1000))),
            '-Duser.home=' + os.path.join(work, 'runtime', 'home'),
            '-Djava.io.tmpdir=' + os.path.join(work, 'runtime', 'tmp'), '-XX:-UsePerfData',
            '-cp', (compat + os.pathsep if compat else '') + os.path.join(lib, '*'),
            *(['piper.bridge.PiperModules'] if parser else ['com.intellij.httpClient.cli.HttpClientMain', *args])]


def _sb(value: str) -> str:
    # JSON's quoted strings share SBPL's escaping for the characters admitted here.
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        # SBPL accepts UTF-8 literals; do not turn them into JSON-only \u escapes.
        if any(ord(c) < 32 for c in value):
            raise IsolationError('control character in HTTP isolation path')
    return json.dumps(value, ensure_ascii=False)


def darwin_jdk_libraries(java: str) -> list[str]:
    """Resolve Homebrew's external font runtime as exact, read-only dylib files.

    Response export initializes AWT even in headless ijhttp. Homebrew links AWT
    against other formulae; granting their directories would expose unrelated
    files. Inspect only this selected JDK's dependency closure, before confinement.
    """
    prefix = next((p for p in ('/opt/homebrew', '/usr/local') if java.startswith(p + '/Cellar/')), '')
    if not prefix:
        return []
    jdk = os.path.dirname(os.path.dirname(java))
    font = os.path.join(jdk, 'lib', 'libfontmanager.dylib')
    if not os.path.isfile(font):
        return []
    pending, visited, libraries = [font], set(), set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        if len(visited) > 128:
            raise IsolationError('HTTP Java runtime library dependency limit exceeded')
        try:
            result = subprocess.run(['/usr/bin/otool', '-L', path], check=True, capture_output=True,
                                    text=True, timeout=5, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        except (OSError, subprocess.SubprocessError) as error:
            raise IsolationError('Cannot inspect HTTP Java runtime libraries') from error
        for line in result.stdout.splitlines()[1:]:
            dependency = line.strip().split(' (compatibility version', 1)[0]
            if dependency.startswith(('@', jdk + '/', '/System/Library/', '/usr/lib/')):
                continue
            canonical = os.path.realpath(dependency)
            if (not dependency.startswith(prefix + '/') or not canonical.startswith(prefix + '/Cellar/') or
                    not dependency.endswith('.dylib') or not canonical.endswith('.dylib') or
                    not os.path.isfile(canonical)):
                raise IsolationError('Unsupported HTTP Java runtime library location')
            libraries.update((dependency, canonical))
            # dyld checks leaf symlinks after resolving the formula's opt alias.
            # Keep each exact spelling, e.g. graphite2's .3 -> .3.3.1 link.
            leaf = os.path.join(os.path.realpath(os.path.dirname(dependency)), os.path.basename(dependency))
            links = set()
            while leaf != canonical:
                if (leaf in links or len(links) >= 32 or not leaf.startswith(prefix + '/Cellar/') or
                        not leaf.endswith('.dylib') or not os.path.islink(leaf)):
                    raise IsolationError('Unsupported HTTP Java runtime library symlink')
                links.add(leaf)
                libraries.add(leaf)
                target = os.readlink(leaf)
                leaf = os.path.normpath(os.path.join(os.path.dirname(leaf), target))
            if canonical not in visited:
                pending.append(canonical)
    return sorted(libraries)


def darwin_profile(java: str, lib: str, work: str, proxy_port: int, *, test_root: str = "", compat: str = '') -> str:
    """Deny by default, including Mach/XPC, host files, exec, signals and direct sockets."""
    jdk = os.path.dirname(os.path.dirname(java))
    reads = [work, lib, jdk, '/System/Library', '/usr/lib', '/usr/share/locale', '/usr/share/zoneinfo',
             '/private/var/db/timezone', '/Library/Apple/System/Library']
    literals = ['/dev/null', '/dev/zero', '/dev/random', '/dev/urandom', '/private/etc/localtime',
                '/private/etc/hosts', '/private/etc/resolv.conf', '/private/etc/nsswitch.conf']
    if compat:
        literals.append(compat)
    libraries = darwin_jdk_libraries(java)
    literals.extend(libraries)
    ancestors = set(['/'])
    for p in reads + literals:
        p = os.path.dirname(p)
        while p != '/':
            ancestors.add(p)
            p = os.path.dirname(p)
    rules = ['(version 1)', '(deny default)', '(import "dyld-support.sb")',
             '(allow syscall*)', '(allow sysctl-read)', '(allow mach-bootstrap)',
             '(allow signal (target self))',
             '(allow process-exec (literal %s))' % _sb(java),
             '(allow file-read* %s)' % ' '.join('(subpath %s)' % _sb(p) for p in reads),
             '(allow file-map-executable %s)' % ' '.join('(subpath %s)' % _sb(p) for p in reads if p != work),
             '(allow file-read* %s)' % ' '.join('(literal %s)' % _sb(p) for p in literals),
             '(allow file-read-metadata %s)' % ' '.join('(literal %s)' % _sb(p) for p in sorted(ancestors)),
             '(allow file-write* (subpath %s) (subpath %s) (subpath %s))' %
             (_sb(os.path.join(work, 'runtime')), _sb(os.path.join(work, 'report')), _sb(os.path.join(work, 'staged'))),
             '(allow file-write-data (literal "/dev/null") (literal "/dev/zero"))',
             '(allow network-outbound (remote tcp "localhost:%d"))' % proxy_port]
    if libraries:
        rules.append('(allow file-map-executable %s)' % ' '.join('(literal %s)' % _sb(p) for p in libraries))
    if test_root:
        # The caller controls its subdirectories and can race realpath validation.
        # Clamp compiled path grants to immutable wrapper-owned capabilities, even
        # if SBPL resolves a raced work/runtime symlink while compiling the profile.
        roots = [test_root, *reads[1:],
                 '/System/Volumes/Preboot/Cryptexes/App/System',
                 '/System/Volumes/Preboot/Cryptexes/Incoming/OS',
                 '/System/Volumes/Preboot/Cryptexes/OS', '/System/Volumes/Preboot/Cryptexes/Rosetta',
                 '/System/Cryptexes/App', '/System/Cryptexes/OS', '/System/Cryptexes/Rosetta']
        filters = ['(subpath %s)' % _sb(p) for p in roots]
        filters += ['(literal %s)' % _sb(p) for p in [*literals, '/']]
        rules.append('(deny file-read* file-map-executable (require-not (require-any %s)))' % ' '.join(filters))
        # Traversal metadata grants do not grant directory listing or file data.
        parents = set(['/'])
        for path in roots + literals:
            path = os.path.dirname(path)
            while path != '/':
                parents.add(path)
                path = os.path.dirname(path)
        rules.append('(allow file-read-metadata %s)' % ' '.join('(literal %s)' % _sb(p) for p in sorted(parents)))
        rules.append('(deny file-write* (require-not (subpath %s)))' % _sb(test_root))
    return '\n'.join(rules) + '\n'


def execute(java: str, lib: str, work: str, args: list[str], timeout: int, proxy_port: int, proxy_path: str = "", *, test_root: str = "", compat: str | None = None, on_stdout=None):
    if compat is None:
        import httpcompat
        compat = httpcompat.install(lib, os.path.join(work, 'compat'))
    if sys.platform.startswith('linux'):
        command = linux_argv(java, lib, work, args, proxy_path, compat, timeout=timeout)
    elif sys.platform != 'darwin':
        raise IsolationError('isolated HTTP runner is unavailable on this platform; no request was sent')
    elif not os.path.isfile('/usr/bin/sandbox-exec'):
        raise IsolationError('macOS sandbox-exec is unavailable; no request was sent')
    else:
        command = ['/usr/bin/sandbox-exec', '-p', darwin_profile(java, lib, work, proxy_port, test_root=test_root, compat=compat),
                   *java_argv(java, lib, work, args, compat, timeout=timeout)]
    p = subprocess.Popen(command, cwd=work, env=clean_env(work), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = collect_output(p, timeout, on_stdout=on_stdout)
        result = subprocess.CompletedProcess(command, p.returncode, stdout, stderr)
        result.http_binary_safe = bool(compat)
        return result
    finally:
        # Also reap descendants on normal completion. No host process is in this group.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        p.stdout.close()
        p.stderr.close()


def run(binary: str, work: str, args: list[str], env: dict, timeout: int, proxy_port: int, *, proxy_path: str = "", on_stdout=None):
    java, lib = runtime(binary, env)
    work = os.path.realpath(work)
    for p in ('runtime/home', 'runtime/tmp', 'report', 'staged'):
        os.makedirs(os.path.join(work, p), mode=0o700, exist_ok=True)
    if os.environ.get('TEST_SANDBOX') == 'seatbelt':
        return test_request(work, args, timeout, proxy_port, on_stdout=on_stdout)
    return execute(java, lib, work, args, timeout, proxy_port, proxy_path, on_stdout=on_stdout)


def test_request(work, args, timeout, proxy_port, *, on_stdout=None):
    """Only the mandatory test wrapper supplies this socket. Its broker launches a
    strictly smaller sandbox, never a general command or a client-provided profile."""
    endpoint = os.environ.get('PIPER_TEST_HTTP_BROKER')
    if not endpoint:
        raise IsolationError('native isolated HTTP tests need the scripts/piper-sandbox.py broker')
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(timeout + 10)
        channel.connect(endpoint)
        channel.sendall(json.dumps(dict(work=work, args=args, timeout=timeout, port=proxy_port)).encode() + b'\n')
        with channel.makefile('rb') as response:
            total = 0
            while True:
                result = json.loads(response.readline(64 << 20))
                if set(result) != {'stdout'}:
                    break
                data = base64.b64decode(result['stdout'], validate=True)
                total += len(data)
                if total > MAX_CONSOLE:
                    raise OutputLimit('ijhttp output exceeds its limit; requests may already have run; not retried')
                if on_stdout:
                    on_stdout(data)
    if result.get('error'):
        if result.get('timeout'):
            raise subprocess.TimeoutExpired('ijhttp', timeout)
        if result.get('output_limit'):
            raise OutputLimit(result['error'])
        raise IsolationError(result['error'])
    completed = subprocess.CompletedProcess('ijhttp', result['rc'], base64.b64decode(result['out'], validate=True),
                                           base64.b64decode(result['err'], validate=True))
    completed.http_binary_safe = result.get('binary_safe') is True
    return completed


def validate_test_request(spec, work_root):
    """Validate a broker request without filesystem mutations or caller runtime paths.
    All paths/capabilities are subsets of the wrapper's existing test sandbox.
    """
    if set(spec) != {'work', 'args', 'timeout', 'port'}:
        raise IsolationError('unexpected HTTP test capability')
    work = spec['work']
    if not isinstance(work, str) or work != os.path.realpath(work) or not work.startswith(work_root + '/'):
        raise IsolationError('HTTP test work must stay inside TEST_SANDBOX_WORK')
    port, timeout = spec['port'], spec['timeout']
    if type(port) is not int or not 1 <= port <= 65535 or port in (17890, 17895, 17896):
        raise IsolationError('invalid HTTP test proxy port')
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        raise IsolationError('invalid HTTP test deadline')
    args = spec['args']
    if args == ['--piper-parse-modules']:
        return work, args, timeout, port
    if not isinstance(args, list) or not args or len(args) > 256 or any(not isinstance(a, str) for a in args):
        raise IsolationError('invalid HTTP test arguments')
    paths = {'-r', '-v', '-p'}
    values = {'-L', '-e', '-V', '--connect-timeout', '--socket-timeout'}
    switches = {'--no-progress', '--insecure'}
    i = 0
    while i < len(args) - 1:
        flag = args[i]
        if flag in switches:
            i += 1
            continue
        if flag not in paths | values | {'--proxy'} or i + 1 >= len(args) - 1:
            raise IsolationError('invalid HTTP test CLI option')
        value = args[i + 1]
        if flag in paths and not os.path.realpath(value).startswith(work + '/'):
            raise IsolationError('HTTP test CLI path outside snapshot')
        if flag == '--proxy' and value != 'socks://127.0.0.1:%d' % port:
            raise IsolationError('HTTP test proxy mismatch')
        i += 2
    if '--proxy' not in args or not os.path.realpath(args[-1]).startswith(work + '/'):
        raise IsolationError('HTTP test source/proxy missing')
    return work, args, timeout, port


def linux_argv(java, lib, work, args, proxy_path, compat='', *, timeout=3600):
    """Empty root + private network/pid/user namespaces. The only route out is a
    Unix socket to Egress. A trusted Python relay provides SOCKS TCP on private lo.
    The JVM gets a seccomp filter denying process creation (threads are allowed).
    """
    import sysconfig
    bwrap = shutil.which('bwrap')
    if not bwrap or not proxy_path:
        raise IsolationError('isolated HTTP requires bubblewrap, Python 3 and libseccomp on Linux; no request was sent')
    python = os.path.realpath(sys.executable)
    helper = os.path.realpath(__file__)
    jdk = os.path.dirname(os.path.dirname(java))
    command = [bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
               '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    paths = ['/usr/lib', '/lib', '/lib64', jdk, lib, sysconfig.get_path('stdlib')]
    mounted = []
    for path in paths:
        if not os.path.isdir(path) or any(path == parent or path.startswith(parent + '/') for parent in mounted):
            continue
        command += ['--ro-bind', path, path]
        mounted.append(path)
    # Debian/Ubuntu's JDK conf/security files are symlinks into /etc/java-*. Do
    # not mount that entire directory (management credentials may live there).
    java_files = ['conf/security/java.security', 'conf/security/java.policy', 'conf/logging.properties',
                  'conf/net.properties', 'lib/security/cacerts']
    java_files += ['conf/security/policy/' + policy + '/' + name for policy in ('limited', 'unlimited')
                   for name in ('default_local.policy', 'default_US_export.policy', 'exempt_local.policy')]
    runtime_files = [os.path.realpath(os.path.join(jdk, rel)) for rel in java_files
                     if os.path.isfile(os.path.join(jdk, rel))]
    if compat:
        runtime_files.append(compat)
    python_library = os.path.join(sysconfig.get_config_var('LIBDIR') or '', sysconfig.get_config_var('LDLIBRARY') or '')
    if os.path.isfile(python_library):
        runtime_files.append(os.path.realpath(python_library))
    for path in runtime_files + ['/etc/ld.so.cache', '/etc/localtime', '/etc/hosts', '/etc/nsswitch.conf', '/etc/ssl/certs', python, helper]:
        if os.path.exists(path) and not any(path.startswith(parent + '/') for parent in mounted):
            command += ['--ro-bind', path, path]
    command += ['--ro-bind', work, work,
                '--bind', os.path.join(work, 'runtime'), os.path.join(work, 'runtime'),
                '--bind', os.path.join(work, 'report'), os.path.join(work, 'report'),
                '--bind', os.path.join(work, 'staged'), os.path.join(work, 'staged'),
                '--ro-bind', os.path.dirname(proxy_path), '/piper-proxy']
    command += ['--chdir', work, '--', python, '-I', '-S', helper, '--linux-child', work,
                json.dumps(java_argv(java, lib, work, args, compat, timeout=timeout))]
    return command


def _no_process_creation():
    """Install in the JVM child just before exec. No fallback if libseccomp is absent.
    clone3 returns ENOSYS so libc uses inspectable clone flags for JVM threads.
    """
    import ctypes
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    context = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not context:
        raise IsolationError('cannot allocate HTTP syscall filter')
    class Compare(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                    ('a', ctypes.c_uint64), ('b', ctypes.c_uint64)]
    try:
        for name in ('fork', 'vfork', 'unshare', 'setns', 'ptrace', 'setsid'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, 0x50001, number, 0) != 0:
                raise IsolationError('cannot deny HTTP process syscall')
        number = lib.seccomp_syscall_resolve_name(b'clone3')
        if number >= 0 and lib.seccomp_rule_add(context, 0x50026, number, 0) != 0:  # ENOSYS
            raise IsolationError('cannot filter HTTP clone3')
        number = lib.seccomp_syscall_resolve_name(b'clone')
        if number < 0 or lib.seccomp_rule_add(context, 0x50001, number, 1, Compare(0, 7, 0x10000, 0)) != 0:
            raise IsolationError('cannot filter HTTP clone')
        if lib.seccomp_load(context) != 0:
            raise IsolationError('cannot install HTTP process filter')
    finally:
        lib.seccomp_release(context)


def _linux_child(work, command):
    """Trusted relay only. It never interprets HTTP/script data or grants a target."""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 1080))
    listener.listen(32)
    slots = threading.BoundedSemaphore(32)
    def forward(client):
        upstream = socket.socket(socket.AF_UNIX)
        try:
            upstream.connect('/piper-proxy/proxy.sock')
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 1)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (upstream if source is client else client).sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()
            client.close()
            slots.release()
    def accept():
        while True:
            client, _ = listener.accept()
            if not slots.acquire(blocking=False):
                client.close()
                continue
            threading.Thread(target=forward, args=(client,), daemon=True).start()
    # Start relay threads only after fork: no Python locks may be inherited locked.
    # preexec runs after fork but before native code; only the trusted relay can fork.
    child = subprocess.Popen(command, env=clean_env(work), stdin=subprocess.DEVNULL,
                             preexec_fn=_no_process_creation)
    threading.Thread(target=accept, daemon=True).start()
    return child.wait()


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--linux-child':
        sys.exit(_linux_child(sys.argv[2], json.loads(sys.argv[3])))
    sys.exit('HTTP isolation helper: no direct command interface')
