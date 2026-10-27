"""HTTP OS policy and exact-destination SOCKS broker regression contracts."""
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import _env
import httpjail as jail
import httpclient as hc


class PolicyTest(unittest.TestCase):
    def test_homebrew_font_libraries_are_transitive_exact_files_and_cycle_bounded(self):
        java = '/opt/homebrew/Cellar/openjdk@21/21/libexec/openjdk.jdk/Contents/Home/bin/java'
        font = java[:-len('bin/java')] + 'lib/libfontmanager.dylib'
        aliases = {'/opt/homebrew/opt/harfbuzz/lib/libharfbuzz.0.dylib':
                   '/opt/homebrew/Cellar/harfbuzz/14/lib/libharfbuzz.0.dylib',
                   '/opt/homebrew/opt/freetype/lib/libfreetype.6.dylib':
                   '/opt/homebrew/Cellar/freetype/2/lib/libfreetype.6.dylib'}
        hb, ft = aliases
        deps = {font: [hb], aliases[hb]: [ft], aliases[ft]: [hb, '/usr/lib/libSystem.B.dylib']}
        def inspect(argv, **kwargs):
            self.assertEqual(argv[:2], ['/usr/bin/otool', '-L'])
            return subprocess.CompletedProcess(argv, 0, argv[2] + ':\n' + ''.join(
                '\t' + dep + ' (compatibility version 1.0.0, current version 1.0.0)\n' for dep in deps[argv[2]]))
        paths = dict(aliases)
        paths.update({os.path.dirname(a): os.path.dirname(c) for a, c in aliases.items()})
        with mock.patch.object(jail.os.path, 'isfile', return_value=True), \
             mock.patch.object(jail.os.path, 'realpath', side_effect=lambda path: paths.get(path, path)), \
             mock.patch.object(jail.subprocess, 'run', side_effect=inspect) as run:
            libraries = jail.darwin_jdk_libraries(java)
        self.assertEqual(set(libraries), set(aliases) | set(aliases.values()))
        self.assertEqual(run.call_count, 3)

    def test_homebrew_leaf_symlink_is_an_exact_capability_too(self):
        java = '/opt/homebrew/Cellar/openjdk/21/Contents/Home/bin/java'
        alias = '/opt/homebrew/opt/graphite2/lib/libgraphite2.3.dylib'
        leaf = '/opt/homebrew/Cellar/graphite2/1/lib/libgraphite2.3.dylib'
        target = leaf.replace('.3.dylib', '.3.3.1.dylib')
        paths = {alias: target, os.path.dirname(alias): os.path.dirname(target)}
        def inspect(argv, **kwargs):
            dependencies = [] if argv[2] == target else [alias]
            return subprocess.CompletedProcess(argv, 0, argv[2] + ':\n' + ''.join(
                '\t' + dep + ' (compatibility version 1.0.0)\n' for dep in dependencies))
        with mock.patch.object(jail.os.path, 'isfile', return_value=True), \
             mock.patch.object(jail.os.path, 'realpath', side_effect=lambda path: paths.get(path, path)), \
             mock.patch.object(jail.os.path, 'islink', side_effect=lambda path: path == leaf), \
             mock.patch.object(jail.os, 'readlink', return_value=os.path.basename(target)), \
             mock.patch.object(jail.subprocess, 'run', side_effect=inspect):
            self.assertEqual(set(jail.darwin_jdk_libraries(java)), {alias, leaf, target})

    def test_homebrew_libraries_cannot_follow_an_external_symlink_or_add_a_directory(self):
        java = '/opt/homebrew/Cellar/openjdk@21/21/Contents/Home/bin/java'
        library = '/opt/homebrew/opt/fixture/lib/libfixture.dylib'
        result = subprocess.CompletedProcess([], 0, 'font:\n\t' + library + ' (compatibility version 1.0.0)\n')
        with mock.patch.object(jail.os.path, 'isfile', return_value=True), \
             mock.patch.object(jail.os.path, 'realpath', side_effect=lambda path: '/private/tmp/outside.dylib' if path == library else path), \
             mock.patch.object(jail.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(jail.IsolationError, 'runtime librar'):
                jail.darwin_jdk_libraries(java)

    def test_homebrew_profile_grants_only_exact_read_map_and_traversal_including_the_test_clamp(self):
        library = '/opt/homebrew/Cellar/fixture/1/lib/libfixture.dylib'
        with mock.patch.object(jail, 'darwin_jdk_libraries', return_value=[library]):
            profile = jail.darwin_profile('/jdk/bin/java', '/cli/lib', '/private/tmp/w/client', 12345,
                                          test_root='/private/tmp/w')
        self.assertIn('(allow file-map-executable (literal "' + library + '"))', profile)
        self.assertIn('(literal "' + library + '")', profile[profile.index('(deny file-read* file-map-executable'):])
        self.assertNotIn('(subpath "/opt/homebrew', profile)
        self.assertNotIn('(allow process-exec (literal "' + library, profile)

    def test_default_deny_no_mach_services_shell_or_broad_home(self):
        p = jail.darwin_profile('/jdk/bin/java', '/cli/lib', '/private/tmp/run', 12345)
        self.assertIn('(deny default)', p)
        self.assertNotIn('(allow default)', p)
        self.assertNotIn('mach-lookup', p)
        self.assertNotIn('/bin/sh', p)
        self.assertNotIn('(subpath "/System")', p)
        self.assertNotIn('(allow process-fork)', p)
        self.assertIn('(allow process-exec (literal "/jdk/bin/java"))', p)
        self.assertIn('localhost:12345', p)
        self.assertNotIn('localhost:*', p)
        self.assertIn('(subpath "/private/tmp/run/staged")', p)

    def test_test_profile_caps_are_independent_of_client_snapshot_symlinks(self):
        p = jail.darwin_profile('/jdk/bin/java', '/cli/lib', '/private/tmp/w/client', 12345, test_root='/private/tmp/w')
        clamp = p[p.index('(deny file-read* file-map-executable'):]
        self.assertIn('(subpath "/private/tmp/w")', clamp)
        self.assertNotIn('(subpath "/private/tmp/w/client")', clamp)
        self.assertIn('(deny file-write* (require-not (subpath "/private/tmp/w")))', clamp)
        self.assertIn('(allow file-read-metadata', clamp)

    def test_environment_does_not_inherit_credentials_or_jvm_injection(self):
        with mock.patch.dict(os.environ, {'SECRET': 'synthetic-secret', 'JAVA_TOOL_OPTIONS': '-javaagent:bad',
                                         'IJHTTP_JAVA_OPTS': '-javaagent:bad'}):
            env = jail.clean_env('/work')
        self.assertEqual(set(env), {'HOME', 'TMPDIR', 'PATH', 'LANG', 'LC_CTYPE'})
        self.assertNotIn('synthetic-secret', json.dumps(env))
        command = jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', ['file.http'])
        self.assertNotIn('-javaagent:bad', command)
        self.assertIn('-Duser.home=/work/runtime/home', command)

    def test_native_broker_deadline_uses_the_host_run_budget_on_both_platforms(self):
        command = jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', ['file.http'], timeout=600)
        self.assertIn('-Dpiper.http.rpc.timeout.ms=600000', command)
        with mock.patch.object(jail.shutil, 'which', return_value='/usr/bin/bwrap'):
            command = jail.linux_argv('/jdk/bin/java', '/cli/lib', '/work', ['file.http'],
                                      '/tmp/proxy/proxy.sock', timeout=600)
        self.assertIn('-Dpiper.http.rpc.timeout.ms=600000', json.loads(command[-1]))

    def spec(self):
        root = os.path.realpath(_env.ROOT)
        work = os.path.join(root, 'jail-policy')
        os.makedirs(work, exist_ok=True)
        return root, dict(work=work, port=12345, timeout=10,
                         args=['--no-progress', '-r', work + '/report', '--proxy',
                               'socks://127.0.0.1:12345', work + '/file.http'])

    def test_test_broker_never_accepts_a_command_profile_env_or_external_path(self):
        root, spec = self.spec()
        self.assertEqual(jail.validate_test_request(spec, root)[0], spec['work'])
        for override in ({'work': '/Users/outside'}, {'port': 17890}, {'port': True}, {'timeout': 0},
                         {'args': ['/bin/sh', '-c', 'echo escaped']},
                         {'args': ['--proxy', 'socks://127.0.0.1:12345', '/etc/hosts']},
                         {'args': ['--proxy', 'socks://127.0.0.1:12345', '-r', '/etc/report', spec['work'] + '/x']},
                         {'args': ['--proxy', 'socks://other:12345', spec['work'] + '/x']}):
            with self.subTest(override=override), self.assertRaises((ValueError, KeyError)):
                jail.validate_test_request(dict(spec, **override), root)

    def test_runtime_missing_java_or_nonstandard_launcher_fails_closed(self):
        with self.assertRaisesRegex(jail.IsolationError, 'Java 17'):
            jail.runtime('/cli/ijhttp', {'PATH': '/nonexistent'})
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as jdk:
            os.makedirs(os.path.join(jdk, 'bin'))
            java = os.path.join(jdk, 'bin', 'java')
            with open(java, 'w') as output:
                output.write('#!/bin/sh\nexit 0\n')
            os.chmod(java, 0o700)
            with self.assertRaisesRegex(jail.IsolationError, 'complete JDK'):
                jail.runtime('/cli/ijhttp', {'JAVA_HOME': jdk})
            with open(os.path.join(jdk, 'release'), 'w') as output:
                output.write('JAVA_VERSION="17"\n')
            with self.assertRaisesRegex(jail.IsolationError, 'official ijhttp'):
                jail.runtime('/cli/ijhttp', {'JAVA_HOME': jdk})

    def test_destinations_are_exact_and_unresolved_host_is_not_executed(self):
        self.assertEqual(hc.run_targets([{'url': '{{url}}/path/{{id}}'}, {'url': 'https://EXAMPLE.test/x'}],
                                        {'url': 'http://localhost:8123'}), [('example.test', 443), ('localhost', 8123)])
        for url in ('http://{{host}}/x', 'file:///etc/hosts', 'https://localhost:invalid', 'http://localhost:0/'):
            with self.subTest(url=url), self.assertRaises(hc.HttpError):
                hc.run_targets([{'url': url}], {})

    def test_linux_mounts_only_required_external_jdk_config_files(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as work:
            jdk = os.path.join(work, 'jdk')
            external = os.path.join(work, 'etc-java')
            os.makedirs(jdk + '/conf/security')
            os.makedirs(external + '/management')
            with open(external + '/java.security', 'w') as output:
                output.write('# synthetic runtime config')
            with open(external + '/management/jmxremote.password', 'w') as output:
                output.write('synthetic-management-credential')
            os.symlink(external + '/java.security', jdk + '/conf/security/java.security')
            with mock.patch.object(jail.shutil, 'which', return_value='/usr/bin/bwrap'):
                command = jail.linux_argv(jdk + '/bin/java', '/cli/lib', work, [], '/tmp/hpx/proxy.sock')
            self.assertIn(['--ro-bind', external + '/java.security', external + '/java.security'],
                          [command[i:i + 3] for i in range(len(command))])
            self.assertNotIn(external, command)
            self.assertNotIn(external + '/management', command)
            self.assertNotIn(external + '/management/jmxremote.password', command)

    def test_timeout_kills_and_reaps_the_whole_runner_group(self):
        child = mock.Mock(pid=123456)
        with mock.patch.object(jail.sys, 'platform', 'darwin'), mock.patch.object(jail.os.path, 'isfile', return_value=True), \
             mock.patch.object(jail.subprocess, 'Popen', return_value=child), mock.patch.object(jail.os, 'killpg') as kill, \
             mock.patch.object(jail, 'collect_output', side_effect=subprocess.TimeoutExpired('native', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                jail.execute('/jdk/bin/java', '/cli/lib', '/private/tmp/work', ['source.http'], 1, 12345, compat='')
        kill.assert_called_once_with(123456, jail.signal.SIGKILL)
        child.wait.assert_called_once()

    def test_linux_has_no_host_root_or_network_and_no_writable_snapshot(self):
        with mock.patch.object(jail.shutil, 'which', return_value='/usr/bin/bwrap'), \
             mock.patch.object(jail.os.path, 'isdir', return_value=True), \
             mock.patch.object(jail.os.path, 'exists', return_value=True):
            command = jail.linux_argv('/jdk/bin/java', '/cli/lib', '/work/run', ['source.http'], '/tmp/hpx/proxy.sock')
        self.assertIn('--unshare-all', command)
        self.assertIn('--cap-drop', command)
        self.assertIn('--new-session', command)
        self.assertIn('/piper-proxy', command)
        self.assertNotIn('--share-net', command)
        self.assertNotIn(['--ro-bind', '/', '/'], [command[i:i + 3] for i in range(len(command))])
        self.assertIn(['--ro-bind', '/work/run', '/work/run'], [command[i:i + 3] for i in range(len(command))])
        with mock.patch.object(jail.shutil, 'which', return_value=None), self.assertRaises(jail.IsolationError):
            jail.linux_argv('/jdk/bin/java', '/cli/lib', '/work/run', [], '/tmp/hpx/proxy.sock')

    def test_native_output_capture_preserves_both_byte_streams(self):
        import sys
        code = 'import os; os.write(1, bytes(range(256))*100); os.write(2, b"error"*100)'
        with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            out, err = jail.collect_output(child, 5)
        self.assertEqual(out, bytes(range(256))*100)
        self.assertEqual(err, b'error'*100)

    def test_stdout_observer_gets_original_bytes_while_child_is_waiting_for_the_host(self):
        import sys
        chunks = []
        code = 'import os,time; os.write(1, b"state-frame\\n"); time.sleep(.1); os.write(2, b"stderr"); os.write(1, b"last\\x00\\xff")'
        with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            def observe(data):
                self.assertIsNone(child.poll())
                chunks.append(data)
            # The first callback must run before the child exits, including before
            # a native body resolver could block awaiting its host reply.
            first = True
            def callback(data):
                nonlocal first
                if first:
                    observe(data)
                    first = False
                else:
                    chunks.append(data)
            out, err = jail.collect_output(child, 5, on_stdout=callback)
        self.assertEqual(b''.join(chunks), out)
        self.assertEqual(out, b'state-frame\nlast\x00\xff')
        self.assertEqual(err, b'stderr')

    def test_native_output_overflow_is_drained_and_never_reported_as_success(self):
        import sys
        code = 'import os; os.write(1, b"x"*1000000); os.write(2, b"y"*1000000)'
        with subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            with self.assertRaisesRegex(jail.OutputLimit, 'may already have run; not retried'):
                jail.collect_output(child, 5, limit=1024)
            self.assertEqual(child.returncode, 0)


class EgressTest(unittest.TestCase):
    def connect(self, proxy, host, port):
        c = socket.create_connection(('127.0.0.1', proxy.port), timeout=2)
        self.addCleanup(c.close)
        c.sendall(b'\x05\x01\x00')
        self.assertEqual(jail.Egress._read(c, 2), b'\x05\x00')
        name = host.encode()
        c.sendall(b'\x05\x01\x00\x03' + bytes([len(name)]) + name + struct.pack('!H', port))
        reply = jail.Egress._read(c, 10)
        return c, reply[1]

    def test_only_approved_host_and_port_and_payload_is_unchanged(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        self.addCleanup(listener.close)
        payload = bytes(range(256)) * 20
        def echo():
            with listener.accept()[0] as c:
                while True:
                    data = c.recv(65536)
                    if not data:
                        return
                    c.sendall(data)
        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        port = listener.getsockname()[1]
        proxy = jail.Egress([('127.0.0.1', port)], 10)
        self.addCleanup(proxy.close)
        for host, p in [('localhost', port), ('127.0.0.1', port + 1)]:
            self.assertEqual(self.connect(proxy, host, p)[1], 2)
        c, code = self.connect(proxy, '127.0.0.1', port)
        self.assertEqual(code, 0)
        c.sendall(payload)
        self.assertEqual(jail.Egress._read(c, len(payload)), payload)
        proxy.close()
        self.assertEqual(c.recv(1), b'')

    def test_http_connect_uses_the_same_exact_destination_and_unchanged_tunnel_bytes(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen()
        self.addCleanup(listener.close)
        def echo():
            with listener.accept()[0] as connection:
                data = connection.recv(4096)
                connection.sendall(data)
        threading.Thread(target=echo,daemon=True).start()
        port = listener.getsockname()[1]
        proxy = jail.Egress([('127.0.0.1',port)],5)
        self.addCleanup(proxy.close)
        with socket.create_connection(('127.0.0.1',proxy.port),timeout=2) as client:
            client.sendall(('CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % (port,port)).encode())
            self.assertEqual(client.recv(4096),b'HTTP/1.1 200 Connection Established\r\n\r\n')
            client.sendall(bytes(range(256)))
            self.assertEqual(jail.Egress._read(client,256),bytes(range(256)))
        with socket.create_connection(('127.0.0.1',proxy.port),timeout=2) as client:
            client.sendall(('CONNECT localhost:%d HTTP/1.1\r\nHost: localhost:%d\r\n\r\n' % (port,port)).encode())
            self.assertIn(b'403 Forbidden',client.recv(4096))
        self.assertEqual(proxy.connected,{('127.0.0.1',port)})

    def test_plain_websocket_proxy_preserves_upgrade_headers_and_filters_proxy_credentials(self):
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen()
        self.addCleanup(listener.close)
        observed = []
        def server():
            with listener.accept()[0] as connection:
                header = bytearray()
                while not header.endswith(b'\r\n\r\n'):
                    header.extend(connection.recv(1))
                observed.append(bytes(header))
                connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n')
                data = connection.recv(4096)
                connection.sendall(data)
        threading.Thread(target=server,daemon=True).start()
        port = listener.getsockname()[1]
        proxy = jail.Egress([('127.0.0.1',port)],5)
        self.addCleanup(proxy.close)
        with socket.create_connection(('127.0.0.1',proxy.port),timeout=2) as client:
            client.sendall(('GET http://127.0.0.1:%d/socket?test=42 HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                            'Connection: Upgrade\r\nUpgrade: websocket\r\nAuthorization: Bearer synthetic-resource-token\r\n'
                            'Proxy-Authorization: Basic synthetic-proxy-only\r\n\r\n' % (port,port)).encode())
            self.assertIn(b'101 Switching Protocols',client.recv(4096))
            client.sendall(b'frame-bytes-unchanged')
            self.assertEqual(jail.Egress._read(client,21),b'frame-bytes-unchanged')
        self.assertTrue(observed[0].startswith(b'GET /socket?test=42 HTTP/1.1\r\n'))
        self.assertIn(b'Authorization: Bearer synthetic-resource-token',observed[0])
        self.assertNotIn(b'synthetic-proxy-only',observed[0])

    def test_http_proxy_late_dns_connect_never_sends_resource_credentials_after_deadline(self):
        import time
        listener = socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen()
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        proxy = jail.Egress([('127.0.0.1',port)],.05)
        self.addCleanup(proxy.close)
        original = socket.create_connection
        received, done = [], threading.Event()
        def server():
            with listener.accept()[0] as connection:
                connection.settimeout(1)
                received.append(connection.recv(4096))
            done.set()
        threading.Thread(target=server,daemon=True).start()
        def connect(address, **kwargs):
            if address[1] == port:
                time.sleep(.1)
            return original(address,**kwargs)
        with mock.patch.object(jail.socket,'create_connection',side_effect=connect):
            with socket.create_connection(('127.0.0.1',proxy.port),timeout=2) as client:
                client.sendall(('GET http://127.0.0.1:%d/socket HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                                'Connection: Upgrade\r\nUpgrade: websocket\r\nAuthorization: Bearer synthetic-secret\r\n\r\n' % (port,port)).encode())
                self.assertEqual(client.recv(4096),b'')
            self.assertTrue(done.wait(2))
        self.assertEqual(received,[b''])
        self.assertEqual(proxy.connected,set())

    def test_http_proxy_rejects_smuggling_ambiguous_authorities_and_non_ws_forwarding_before_connect(self):
        proxy = jail.Egress([('127.0.0.1',42424)],5)
        self.addCleanup(proxy.close)
        bad = [b'GET http://127.0.0.1:42424/ HTTP/1.1\r\nHost: 127.0.0.1:42424\r\n\r\n',
               b'CONNECT 127.0.0.1:42424 HTTP/1.1\r\nHost: other.example:42424\r\n\r\n',
               b'CONNECT 127.0.0.1:42424 HTTP/1.1\r\nHost: 127.0.0.1:42424\r\nTransfer-Encoding: chunked\r\n\r\n',
               b'CONNECT 127.0.0.1:42424 HTTP/1.1\r\nHost: 127.0.0.1:42424\r\nHost: 127.0.0.1:42424\r\n\r\n',
               b'CONNECT user@127.0.0.1:42424 HTTP/1.1\r\nHost: 127.0.0.1:42424\r\n\r\n',
               b'CONNECT 127.0.0.1:42424/extra HTTP/1.1\r\nHost: 127.0.0.1:42424\r\n\r\n',
               b'CONNECT 127.0.0.1:42424 HTTP/1.1\r\nHost: 127.0.0.1:42424\r\nContent-Length: 1\r\n\r\nx',
               b'CONNECT 127.0.0.1:42424 HTTP/1.1\r\nHost: 127.0.0.1:42424\r\nX-Large: ' + b'x' * 16384 + b'\r\n\r\n']
        original = jail.socket.create_connection
        upstream = []
        def connect(address, **kwargs):
            if address[1] == 42424:
                upstream.append(address)
                raise AssertionError('unvalidated HTTP proxy connection')
            return original(address,**kwargs)
        with mock.patch.object(jail.socket,'create_connection',side_effect=connect):
            for raw in bad:
                with self.subTest(raw=raw[:80]), socket.create_connection(('127.0.0.1',proxy.port),timeout=2) as client:
                    client.sendall(raw)
                    try:
                        self.assertEqual(client.recv(4096),b'')
                    except ConnectionResetError:
                        pass
        self.assertEqual(upstream,[])

    def test_upstream_connect_timeout_and_refusal_are_preserved(self):
        proxy = jail.Egress([('127.0.0.1', 42424)], 5, connect_timeout=.123)
        self.addCleanup(proxy.close)
        real_connect = socket.create_connection
        attempted = []
        def connect(address, **kwargs):
            if address == ('127.0.0.1', 42424):
                attempted.append(kwargs['timeout'])
                raise ConnectionRefusedError('synthetic refusal')
            return real_connect(address, **kwargs)
        with mock.patch.object(jail.socket, 'create_connection', side_effect=connect):
            self.assertEqual(self.connect(proxy, '127.0.0.1', 42424)[1], 5)
        self.assertEqual(attempted, [.123])
        self.assertEqual(proxy.failures, {('127.0.0.1', 42424): 'connection refused'})
        self.assertEqual(proxy.connected, set())

    def test_deadline_and_unsupported_protocols_close_without_connecting(self):
        proxy = jail.Egress([], 5)
        self.addCleanup(proxy.close)
        c = socket.create_connection(('127.0.0.1', proxy.port), timeout=2)
        self.addCleanup(c.close)
        c.sendall(b'\x05\x01\x02')
        self.assertEqual(c.recv(2), b'\x05\xff')
