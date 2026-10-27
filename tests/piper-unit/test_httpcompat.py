"""A compatibility bridge is local, version-pinned, immutable, and never a user jar."""
import os
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import struct
import tempfile
import shutil
import subprocess
import unittest
from unittest import mock
import zipfile

import _env
import httpcompat as compat
import httpjail as jail


class CompatTest(unittest.TestCase):
    def test_native_rpc_obeys_short_and_long_host_selected_budgets(self):
        home, binary = os.environ.get('JAVA_HOME'), os.environ.get('PIPER_IJHTTP')
        if not home or not binary:
            self.skipTest('requires the configured JDK and CLI')
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            source = os.path.join(root, 'RpcDeadline.java')
            with open(source, 'w') as output:
                output.write('''package piper.bridge;
import java.nio.file.*;
public class RpcDeadline {
  public static void main(String[] args) throws Exception {
    Path root = Path.of(args[0]);
    Path broker = Files.createDirectories(root.resolve("runtime/broker"));
    System.setProperty("piper.http.work", root.toString());
    System.setProperty("piper.http.rpc.timeout.ms", "30");
    try {
      PiperBinary.rpc(new com.google.gson.JsonObject());
      throw new AssertionError("Missing reply must reach the host-selected deadline");
    } catch (java.io.IOException expected) {
      if (!expected.getMessage().contains("deadline")) throw expected;
    }
    System.setProperty("piper.http.rpc.timeout.ms", "2000");
    Thread host = new Thread(() -> {
      try {
        while (!Files.exists(broker.resolve("request-1.json"))) Thread.sleep(1);
        Thread.sleep(80);
        Files.writeString(broker.resolve("reply.tmp"), "{\\"ok\\":true}");
        Files.move(broker.resolve("reply.tmp"), broker.resolve("reply-1.json"), StandardCopyOption.ATOMIC_MOVE);
      } catch (Exception e) { throw new RuntimeException(e); }
    });
    host.start();
    if (!PiperBinary.rpc(new com.google.gson.JsonObject()).get("ok").getAsBoolean())
      throw new AssertionError("Longer budget must allow the delayed host reply");
    host.join();
  }
}''')
            helpers = os.path.join(root, 'piper', 'bridge')
            os.makedirs(helpers)
            for name in compat.HELPER_NAMES:
                shutil.copyfile(os.path.join(os.path.dirname(compat.__file__), 'httpcompat', name),
                                os.path.join(helpers, name))
            classpath = root + os.pathsep + os.path.join(os.path.dirname(os.path.realpath(binary)), 'lib', '*')
            subprocess.run([os.path.join(home, 'bin', 'javac'), '--release', '17', '-cp', classpath,
                            '-d', root, source], check=True, capture_output=True, timeout=30)
            subprocess.run([os.path.join(home, 'bin', 'java'), '-cp', classpath, 'piper.bridge.RpcDeadline', root],
                           check=True, capture_output=True, timeout=5)

    def test_real_tunnel_tls_exception_is_endpoint_scoped_even_across_native_redirects(self):
        home, binary = os.environ.get('JAVA_HOME'), os.environ.get('PIPER_IJHTTP')
        if not home or not binary or not os.environ.get('PIPER_TEST_HTTP_BROKER'):
            self.skipTest('requires the configured JDK, CLI and native sandbox broker')
        import httpclient as hc
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            key, certificate = os.path.join(root, 'key.pem'), os.path.join(root, 'cert.pem')
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
                            '-out', certificate, '-days', '1', '-subj', '/CN=untrusted-tunnel.invalid'],
                           check=True, capture_output=True, timeout=15)
            hits, servers = [], []
            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    hits.append((self.server.server_port, self.path))
                    redirect = self.path in ('/redirect', '/local-redirect')
                    body = b'{"ok":true}'
                    self.send_response(302 if redirect else 200)
                    if redirect:
                        destination = servers[0] if self.path == '/local-redirect' else servers[1]
                        self.send_header('Location', 'https://localhost:%d/redirected' % destination.server_port)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            for _ in range(2):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(certificate, key)
                server.socket = context.wrap_socket(server.socket, server_side=True)
                servers.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
            def run(source, *, insecure=False, tunnel=True, identity=False, tunnel_port=None):
                work = tempfile.mkdtemp(dir=root)
                target = os.path.join(work, 'main.http')
                with open(target, 'w') as output: output.write(source)
                if tunnel:
                    with open(os.path.join(work, 'tunnel-tls.json'), 'w') as output:
                        json.dump({'ports':[tunnel_port or servers[0].server_port]}, output)
                if identity:
                    for name, path in [('certificate', certificate), ('key', key)]:
                        shutil.copyfile(path, os.path.join(work, name))
                    with open(os.path.join(work, 'ssl-client.json'), 'w') as output:
                        json.dump({name:{'path':os.path.join(work, name), 'format':'PEM'} for name in ('certificate', 'key')}, output)
                return hc.run_ijhttp(binary, work, target, '', None, {}, insecure, 20,
                                    targets=[('localhost', port) for port in
                                             {server.server_port for server in servers} | ({tunnel_port} if tunnel_port else set())])
            local, remote = (server.server_port for server in servers)
            rc, console, _ = run('GET https://localhost:%d/allowed\n' % local)
            self.assertEqual(rc, 0, console)
            self.assertEqual(hits, [(local, '/allowed')])
            rc, console, _ = run('GET https://localhost:%d/rejected\n' % remote)
            self.assertNotEqual(rc, 0, console)
            self.assertEqual(hits, [(local, '/allowed')])
            rc, console, _ = run('GET https://localhost:%d/redirect\n' % local)
            self.assertNotEqual(rc, 0, console)
            self.assertEqual(hits[-1], (local, '/redirect'))
            self.assertNotIn((remote, '/redirected'), hits)
            rc, console, _ = run('GET https://localhost:%d/explicit\n' % remote, insecure=True)
            self.assertEqual(rc, 0, console)
            self.assertEqual(hits[-1], (remote, '/explicit'))
            rc, console, _ = run('GET https://localhost:%d/verified\n' % local, tunnel=False)
            self.assertNotEqual(rc, 0, console)
            self.assertNotIn((local, '/verified'), hits)
            rc, console, _ = run('GET https://localhost:%d/local-redirect\n' % local)
            self.assertEqual(rc, 0, console)
            self.assertEqual(hits[-2:], [(local, '/local-redirect'), (local, '/redirected')])
            servers[0].socket.context.load_verify_locations(certificate)
            servers[0].socket.context.verify_mode = ssl.CERT_REQUIRED
            rc, console, _ = run('GET https://localhost:%d/missing-identity\n' % local)
            self.assertNotEqual(rc, 0, console)
            rc, console, _ = run('GET https://localhost:%d/mtls\n' % local, identity=True)
            self.assertEqual(rc, 0, console)
            self.assertEqual(hits[-1], (local, '/mtls'))
            # A tunnel must retain the CLI's HTTP/2 selection and ALPN, too.
            source = os.path.join(root, 'Http2Fixture.java')
            with open(source, 'w') as output:
                output.write('''public class Http2Fixture {
  public static void main(String[] args) {
    var server = reactor.netty.http.server.HttpServer.create().host("127.0.0.1").port(0)
        .protocol(reactor.netty.http.HttpProtocol.H2)
        .secure(spec -> spec.sslContext(reactor.netty.http.Http2SslContextSpec.forServer(new java.io.File(args[0]), new java.io.File(args[1]))))
        .handle((request, response) -> response.sendString(reactor.core.publisher.Mono.just("h2 fixture"))).bindNow();
    System.out.println(server.port());
    server.onDispose().block();
  }
}''')
            dependencies = os.path.join(os.path.dirname(binary), 'lib', '*')
            subprocess.run([os.path.join(home, 'bin', 'javac'), '-cp', dependencies, '-d', root, source],
                           check=True, capture_output=True, timeout=30)
            process = subprocess.Popen([os.path.join(home, 'bin', 'java'), '-cp', root + os.pathsep + dependencies,
                                        'Http2Fixture', certificate, key], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                import select
                self.assertTrue(select.select([process.stdout], [], [], 15)[0], 'HTTP/2 fixture did not start')
                line = process.stdout.readline().strip()
                self.assertTrue(line.isdigit(), line)
                port = int(line)
                rc, console, _ = run('GET https://localhost:%d/h2 HTTP/2\n' % port, tunnel_port=port)
                self.assertEqual(rc, 0, console)
                self.assertIn('h2 fixture', console)
            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()

    def test_real_bridge_preserves_native_exception_and_error_identity(self):
        home = os.environ.get('JAVA_HOME')
        binary = os.environ.get('PIPER_IJHTTP')
        if not home or not binary:
            self.skipTest('requires the configured JDK and installed CLI dependencies')
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            directory = os.path.join(root, 'piper', 'bridge')
            os.makedirs(directory)
            source = os.path.join(root, 'NativeFailure.java')
            with open(source, 'w') as output:
                output.write('''public class NativeFailure {
  static final RuntimeException EXIT = new RuntimeException("native exit fixture");
  static final AssertionError ERROR = new AssertionError("native error fixture");
  private static Object __piper_exit() { throw EXIT; }
  private static Object __piper_error() { throw ERROR; }
  public static void main(String[] args) throws Exception {
    var method = Class.forName("piper.bridge.PiperBinary").getDeclaredMethod("invokeOriginal",
        Class.class, Object.class, String.class, Class[].class, Object[].class);
    method.setAccessible(true);
    for (String name : new String[]{"exit", "error"}) {
      try { method.invoke(null, NativeFailure.class, null, name, new Class<?>[0], new Object[0]);
        throw new AssertionError("native exception lost");
      } catch (java.lang.reflect.InvocationTargetException failure) {
        if (failure.getCause() != (name.equals("exit") ? EXIT : ERROR))
          throw new AssertionError("native exception wrapped", failure);
      }
    }
  }
}''')
            helpers = os.path.join(os.path.dirname(compat.__file__), 'httpcompat')
            for name in os.listdir(helpers):
                if name.startswith('PiperBinary') and name.endswith('.class'):
                    shutil.copyfile(os.path.join(helpers, name), os.path.join(directory, name))
            subprocess.run([os.path.join(home, 'bin', 'javac'), '--release', '17', '-d', root, source],
                           check=True, capture_output=True, timeout=30)
            classpath = root + os.pathsep + os.path.join(os.path.dirname(binary), 'lib', '*')
            subprocess.run([os.path.join(home, 'bin', 'java'), '-Xverify:all', '-cp', classpath, 'NativeFailure'],
                           check=True, capture_output=True, timeout=30)

    def test_typed_adapters_are_verified_by_the_jvm_and_keep_original_methods(self):
        home = os.environ.get('JAVA_HOME')
        javac = os.path.join(home, 'bin', 'javac') if home else shutil.which('javac')
        java = os.path.join(home, 'bin', 'java') if home else shutil.which('java')
        if not java or not javac:
            self.skipTest('requires the configured JDK')
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            source = os.path.join(root, 'Probe.java')
            helper = os.path.join(root, 'piper/bridge/PiperBinary.java')
            os.makedirs(os.path.dirname(helper))
            with open(source, 'w') as output:
                output.write('''public class Probe {
  public java.util.List<String> tests;
  public Probe() {}
  public Probe(java.util.List<String> tests) { this.tests = tests; }
  public static Object mixed(Object a, boolean b, Object c) { return "original"; }
  public static String stringCall(String text) { return text.toUpperCase(); }
  private static String nativeStatic(String text) { return "original:" + text; }
  public static String staticCall(String text) { return nativeStatic(text); }
  public String instance(String a, boolean b, long n, double d, Object[] array) { return "original"; }
  public static void consume(Object a, int n) { throw new AssertionError("original void called"); }
  public static void main(String[] args) throws Exception {
    var result = new Probe(java.util.List.of("original"));
    if (!result.tests.equals(java.util.List.of("original", "completed"))) throw new AssertionError("constructor completion");
    if (!mixed("a", true, "c").equals("a:true:c")) throw new AssertionError("mixed slots");
    if (!stringCall("abc").equals("static:ABC")) throw new AssertionError("virtual receiver");
    if (!staticCall("abc").equals("static:ABC") || !nativeStatic("abc").equals("original:abc"))
      throw new AssertionError("static call site");
    if (!new Probe().instance("v", true, 17L, 2.5, new Object[]{42}).equals("Probe:v:true:17:2.5:42"))
      throw new AssertionError("instance wide slots");
    consume("ok", 19);
    if (!piper.bridge.PiperBinary.seen.equals("ok:19")) throw new AssertionError("void slots");
    if (!Probe.class.getDeclaredMethod("__piper_mixed", Object.class, boolean.class, Object.class)
        .invoke(null, "a", false, "c").equals("original")) throw new AssertionError("original changed");
    System.out.println("verified adapters");
  }
}''')
            with open(helper, 'w') as output:
                output.write('''package piper.bridge;
public class PiperBinary {
  public static String seen;
  public static String uppercase(String value) { return "static:" + value.toUpperCase(); }
  public static void complete(Object value) throws Exception {
    var field = value.getClass().getField("tests");
    if (!field.get(value).equals(java.util.List.of("original"))) throw new AssertionError("constructor not complete");
    field.set(value, java.util.List.of("original", "completed"));
  }
  public static Object mixed(Object a, boolean b, Object c) { return a+":"+b+":"+c; }
  public static String instance(Object receiver, Object a, boolean b, long n, double d, Object array) {
    return receiver.getClass().getName()+":"+a+":"+b+":"+n+":"+d+":"+((Object[]) array)[0];
  }
  public static void consume(Object a, int n) { seen=a+":"+n; }
}''')
            subprocess.run([javac, '--release', '17', '-g:none', '-d', root, source, helper], check=True, capture_output=True, timeout=30)
            path = os.path.join(root, 'Probe.class')
            with open(path, 'rb') as compiled:
                edited = compat.ClassFile(compiled.read())
            edited.wrap_static('mixed', '(Ljava/lang/Object;ZLjava/lang/Object;)Ljava/lang/Object;', 'mixed', 'Ljava/lang/Object;')
            edited.wrap_static('instance', '(Ljava/lang/String;ZJD[Ljava/lang/Object;)Ljava/lang/String;', 'instance', 'Ljava/lang/String;', instance=True)
            edited.wrap_static('consume', '(Ljava/lang/Object;I)V', 'consume', 'V')
            edited.await_constructor('(Ljava/util/List;)V', 'complete', b'\x2a')
            for owner, offset in (('java/lang/String', 0), ('java/lang/Object', 1)):
                with self.assertRaises(ValueError):
                    edited.redirect_call('stringCall', '(Ljava/lang/String;)Ljava/lang/String;', offset,
                                            owner, 'toUpperCase', '()Ljava/lang/String;', 'uppercase')
            edited.redirect_call('stringCall', '(Ljava/lang/String;)Ljava/lang/String;', 1,
                                    'java/lang/String', 'toUpperCase', '()Ljava/lang/String;', 'uppercase')
            edited.redirect_call('staticCall', '(Ljava/lang/String;)Ljava/lang/String;', 1,
                                    'Probe', 'nativeStatic', '(Ljava/lang/String;)Ljava/lang/String;', 'uppercase', virtual=False)
            with open(path, 'wb') as compiled:
                compiled.write(edited.bytes())
            result = subprocess.run([java, '-Xverify:all', '-cp', root, 'Probe'], check=True, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.stdout.strip(), 'verified adapters')

    def test_unknown_native_class_versions_are_never_patched(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            with zipfile.ZipFile(os.path.join(root, 'ijhttp.jar'), 'w') as archive:
                for name in compat.HASHES:
                    archive.writestr(name, b'unknown revision')
            self.assertIsNone(compat.overlay(root))
            self.assertEqual(compat.install(root, os.path.join(root, 'cache')), '')
            self.assertFalse(os.path.exists(os.path.join(root, 'cache')))

    def test_missing_class_or_invalid_zip_is_an_unsupported_engine(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            path = os.path.join(root, 'ijhttp.jar')
            with zipfile.ZipFile(path, 'w'):
                pass
            self.assertIsNone(compat.overlay(root))
            with open(path, 'wb') as output:
                output.write(b'not a jar')
            self.assertIsNone(compat.overlay(root))

    def test_cache_never_overwrites_corrupted_content_or_follows_a_file_symlink(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root, mock.patch.object(compat, 'overlay', return_value=b'owned jar'):
            path = compat.install('/trusted/lib', root)
            self.assertEqual(compat.install('/trusted/lib', root), path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)
            os.chmod(path, 0o600)
            with open(path, 'wb') as output:
                output.write(b'changed jar')
            with self.assertRaisesRegex(ValueError, 'integrity'):
                compat.install('/trusted/lib', root)
            os.unlink(path)
            target = os.path.join(root, 'outside')
            with open(target, 'wb') as output:
                output.write(b'owned jar')
            os.symlink(target, path)
            with self.assertRaises(OSError):
                compat.install('/trusted/lib', root)

    def test_fixed_java_entrypoint_has_only_the_trusted_optional_overlay(self):
        command = jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', ['file.http'], '/trusted/compat.jar')
        self.assertEqual(command[command.index('-cp') + 1], '/trusted/compat.jar' + os.pathsep + '/cli/lib/*')
        self.assertIn('com.intellij.httpClient.cli.HttpClientMain', command)
        profile = jail.darwin_profile('/jdk/bin/java', '/cli/lib', '/work', 12345, compat='/trusted/compat.jar')
        self.assertIn('(literal "/trusted/compat.jar")', profile)
        self.assertNotIn('(subpath "/trusted")', profile)

    def test_parser_entrypoint_is_fixed_and_has_no_arbitrary_main_class_or_new_capability(self):
        args = ['--piper-parse-modules']
        with self.assertRaisesRegex(jail.IsolationError, 'supported'):
            jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', args)
        command = jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', args, '/trusted/compat.jar')
        self.assertEqual(command[-1], 'piper.bridge.PiperModules')
        self.assertNotIn('com.intellij.httpClient.cli.HttpClientMain', command)
        root = os.path.realpath(_env.ROOT)
        spec = dict(work=root + '/parser', args=args, timeout=30, port=12345)
        self.assertEqual(jail.validate_test_request(spec, root)[1], args)
        for extra in ('AnotherMain', '--insecure', '-cp', '/host/file'):
            with self.subTest(extra=extra), self.assertRaises(jail.IsolationError):
                jail.validate_test_request(dict(spec, args=args + [extra]), root)

    def test_native_tls_override_is_a_fixed_host_flag_and_defaults_to_verification(self):
        for args, expected in ((['file.http'], 'false'), (['--insecure', 'file.http'], 'true')):
            command = jail.java_argv('/jdk/bin/java', '/cli/lib', '/work', args)
            self.assertIn('-Dpiper.http.insecure=' + expected, command)

    def test_own_helper_class_is_java17_and_has_reviewable_source(self):
        path = os.path.join(os.path.dirname(compat.__file__), 'httpcompat', 'PiperBinary.class')
        with open(path, 'rb') as compiled:
            raw = compiled.read()
        self.assertEqual(raw[:4], b'\xca\xfe\xba\xbe')
        self.assertEqual(struct.unpack_from('>H', raw, 6)[0], 61)
        with open(path[:-5] + 'java') as source:
            text = source.read()
        self.assertIn('64L << 20', text)
        self.assertNotIn('System.out', text)

    def test_malformed_class_does_not_reach_an_unsafe_mutation(self):
        with self.assertRaises(ValueError):
            compat.ClassFile(b'not class')

    @unittest.skipUnless(os.environ.get('PIPER_TEST_HTTP_BROKER'), 'requires macOS native test broker')
    def test_wrapper_cache_cannot_be_written_unlinked_or_renamed_by_a_test(self):
        directory = os.path.join(os.environ['TEST_SANDBOX_WORK'], 'http-native-compat')
        if not os.path.isdir(directory):
            self.skipTest('installed CLI has no supported compatibility overlay')
        names = os.listdir(directory)
        self.assertEqual(len(names), 1)
        path = os.path.join(directory, names[0])
        for action in (lambda: os.chmod(path, 0o600), lambda: os.unlink(path),
                       lambda: os.rename(directory, directory + '-replaced'),
                       lambda: open(os.path.join(directory, 'injected.jar'), 'wb')):
            with self.subTest(action=action), self.assertRaises(PermissionError):
                action()
