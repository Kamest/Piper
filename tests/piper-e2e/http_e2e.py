#!/usr/bin/env python3
"""tests/piper-e2e/http_e2e.py - .http tunnels / save_to / hints with REAL parts: JetBrains' ijhttp CLI, an HTTPS server
(self-signed, CN not localhost), an ssh stand-in that really forwards TCP (Piper's _db_ssh_open path), urllib, the
Manager through the session tools (tests/piper-unit/_env.py: a throwaway PIPER_HOME, no piperd).

Needs ijhttp (PIPER_IJHTTP=<path>, else `ijhttp` on PATH; https://jb.gg/ijhttp/latest), java for it and openssl;
without them it prints SKIP and exits 77. Named http_e2e.py, not http.py: a script dir is first on sys.path, and a
http.py there would shadow the stdlib `http` package for every script of this directory.

Run through scripts/piper-sandbox.py (sockets and all fixtures stay inside its test sandbox).
"""
import base64
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IJHTTP = os.environ.get("PIPER_IJHTTP") or shutil.which("ijhttp")
_missing = [n for n, ok in (("ijhttp (PIPER_IJHTTP or PATH)", IJHTTP and os.access(IJHTTP, os.X_OK)),
                            ("java (JAVA_HOME or PATH)", os.environ.get("JAVA_HOME") or shutil.which("java")),
                            ("openssl", shutil.which("openssl"))) if not ok]
if _missing:
    print("SKIP http_e2e.py: missing %s" % ", ".join(_missing))
    sys.exit(77)
sys.path.insert(0, os.path.join(SK, "tests", "piper-unit"))
os.environ["PIPER_DB_NO_REAPER"] = "1"
import _env  # noqa: E402  (isolated PIPER_HOME etc.)
import common  # noqa: E402
import httpclient as hc  # noqa: E402
import sessions  # noqa: E402

WORK = os.path.join(_env.ROOT, "e2e")
os.makedirs(WORK, exist_ok=True)
PDF = b"%PDF-1.7\n" + bytes(range(256)) * 800 + b"\n%%EOF"   # ~200 kB binary
FEED = b'<?xml version="1.0" encoding="UTF-8"?>\r\n<feed>' + b'<item>\xc4\x8d &amp; a</item>\r\n' * 30000 + b'</feed>\r\n'
PW = "Secret-pass-42"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)[:600]), flush=True)


# --- an HTTPS server with a self-signed certificate for another name
crt, key = os.path.join(WORK, "c.pem"), os.path.join(WORK, "k.pem")
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "1",
                "-subj", "/CN=test-app.example"], check=True, capture_output=True)
hits = []
uploads = []
cookie_headers = {}
cookie_redirects = {}
stream_closures = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        hits.append((self.path, dict(self.headers)))
        if self.path.startswith('/api/sse-open?'):
            closed = stream_closures.setdefault(self.path, threading.Event())
            self.protocol_version = 'HTTP/1.1'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                for index in range(200):
                    chunk = ('data: tick%d\n\n' % index).encode()
                    self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    time.sleep(.05)
                self.wfile.write(b'0\r\n\r\n')
            except (OSError, ssl.SSLError):
                pass
            finally:
                self.close_connection = True
                closed.set()
            return
        if self.path in ('/api/sse-fragments', '/api/sse-truncated'):
            self.protocol_version = 'HTTP/1.1'
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'close')
            if self.path == '/api/sse-fragments':
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                for chunk in (b'data: \xc3', b'\xa9\n\n', b'data: tail'):
                    self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                    self.wfile.flush()
                    time.sleep(.03)
                self.wfile.write(b'0\r\n\r\n')
            else:
                self.send_header('Content-Length', '1000')
                self.end_headers()
                self.wfile.write(b'data: premature\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path in cookie_redirects:
            self.send_response(302)
            self.send_header('Location', cookie_redirects[self.path])
            for value in cookie_headers.get(self.path, []):
                self.send_header('Set-Cookie', value)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if self.path.startswith('/api/slow'):
            time.sleep(0.5)
        if self.path.startswith('/api/iteration'):
            body, ct = b'\x00\xff' + self.path.encode(), 'application/octet-stream'
        elif self.path.startswith('/api/console-overflow'):
            body, ct = b'x' * (33 << 20), 'text/plain'
        elif self.path.startswith('/api/sse-finite'):
            body, ct = b'data: first\n\ndata: second\n\n', 'text/event-stream'
        elif self.path.startswith("/api/cookie/"):
            body, ct = json.dumps({"cookie": self.headers.get("Cookie", "")}).encode(), "application/json"
        elif self.path.startswith("/api/login"):
            body, ct = b'{"sessionId": "S-9"}', "application/json"
        elif self.path.startswith('/api/code-root-v5'):
            body, ct = b'request.variables.set("computedValue", 47);\n', 'application/javascript'
        elif self.path.startswith('/api/code-v5'):
            body, ct = b'export const value = 45;\n', 'application/javascript'
        elif self.path.startswith(("/api/feed-short", "/api/slow")):
            body, ct = b"<short/>\r\n", "application/xml"
        elif self.path.startswith("/api/feed"):
            body, ct = FEED, "application/xml"
        elif self.path.startswith("/api/pdf") and {k.lower(): v for k, v in self.headers.items()}.get("x-session-id") != "S-9":
            body, ct = b"no session", "text/plain"
            self.send_response(401)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path.startswith("/api/pdf"):
            body, ct = PDF, "application/pdf"
        elif self.path.startswith('/api/binary-overflow'):
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str((64 << 20) + 1))
            self.end_headers()
            return
        elif self.path.startswith("/api/echo"):
            body, ct = json.dumps({"auth": self.headers.get("Authorization"), "host": self.headers.get("Host")}).encode(), "application/json"
        elif self.path.startswith("/api/redirect"):
            self.send_response(302)
            self.send_header("Location", "https://evil.example/")
            self.end_headers()
            return
        else:
            body, ct = b"not found", "text/plain"
        self.send_response(200 if body != b"not found" else 404)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        if self.path == "/api/cookie/set":
            self.send_header("Set-Cookie", "probe=synthetic; Path=/")
        for value in cookie_headers.get(self.path, []):
            self.send_header('Set-Cookie', value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass   # intentional socket-timeout regression closes before the slow response

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        uploads.append((self.path, body))
        if self.path.startswith('/api/auth-'):
            self.send_response(302 if self.path == '/api/auth-redirect' else 401 if self.path == '/api/auth-error' else 200)
            self.send_header('Location', '/api/auth-never-follow')
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(dict(access_token='native-access-v5-42', id_token='native-id-v5-42',
                refresh_token='native-refresh-v5-42', expires_in=3600, token_type='Bearer')).encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_PROPFIND = do_GET


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
plain_srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
threading.Thread(target=plain_srv.serve_forever, daemon=True).start()
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(crt, key)
srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
SPORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# --- ssh stand-in: parses -L 127.0.0.1:P:host:rport and forwards P -> the server (logs argv)
fake_ssh = os.path.join(WORK, "fake-ssh")
with open(fake_ssh, "w") as f:
    f.write('''#!/usr/bin/env python3
import os, socket, sys, threading
open(os.environ["E2E_SSH_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
L = sys.argv[sys.argv.index("-L") + 1].split(":")
lp, target = int(L[1]), int(os.environ["E2E_TARGET"])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", lp)); s.listen(16)
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d: break
            b.sendall(d)
    except OSError: pass
    finally:
        for x in (a, b):
            try: x.shutdown(socket.SHUT_RDWR)
            except OSError: pass
while True:
    c, _ = s.accept()
    u = socket.create_connection(("127.0.0.1", target))
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
''')
os.chmod(fake_ssh, 0o755)
ssh_log = os.path.join(WORK, "ssh.log")
os.environ.update(PIPER_SSH_BIN=fake_ssh, E2E_SSH_LOG=ssh_log, E2E_TARGET=str(SPORT),
                  PIPER_IJHTTP=IJHTTP)

# --- the project
TUN = "test with tunnel `ssh -L 8443:192.0.2.1:443 projects@test-k.example`"
ENV = {"dev": {"domain": "https://localhost:%d" % SPORT},
       TUN: {"domain": "https://localhost:8443"},
       "test direct": {"domain": "https://localhost:%d" % SPORT},
       "test ssl off": {"domain": "https://localhost:%d" % SPORT, "SSLConfiguration": {"verifyHostCertificate": False}},
       "test refused": {"domain": "https://localhost:18443"}}
PRIV = {TUN: {"apiPassword": PW}, "test direct": {"apiPassword": PW}, "test ssl off": {"apiPassword": PW},
        "dev": {"apiPassword": PW}, "test refused": {"apiPassword": PW}}
HTTP = """### Login
GET {{domain}}/api/login

> {%
  client.test("logged in", () => client.assert(response.status === 200));
  client.global.set("sessionId", response.body.sessionId);
%}

### PDF
GET {{domain}}/api/pdf
Accept: application/pdf
X-Session-Id: {{sessionId}}

### Echo
GET {{domain}}/api/echo
Authorization: Bearer {{apiPassword}}

### Redirect
GET {{domain}}/api/redirect
"""
root = _env.make_project("e2ehttp", {"http-client/vn.http": HTTP, "http-client/http-client.env.json": json.dumps(ENV),
                                     "http-client/http-client.private.env.json": json.dumps(PRIV), "README.md": "x\n"})
for a in (["init", "-q"], ["add", "http-client/vn.http", "README.md"], ["commit", "-qm", "i"]):
    subprocess.run(["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)
KEY = "team-x/e2ehttp"
_env.register(KEY, root)

ph = _env.InThreadPtyHost()
m = sessions.Manager(connect_fn=lambda on_event, on_close: ph.client(on_event, on_close))
cards = []
m.ask = lambda kind, s, summary, payload, timeout=840, always="", result=None: (cards.append((kind, summary)), True)[1]
s = sessions.Session(kind="project", project=KEY, path=root, state="ready",
                     excludes=["http-client/http-client.env.json", "http-client/http-client.private.env.json"])
m.sessions[s.id] = s


def tool(**a):
    try:
        return m.tool(s, "http_run", a)
    except Exception as e:  # noqa: BLE001
        return {"exception": "%s: %s" % (type(e).__name__, e)}


def persistent_cookie_check():
    import socket
    from unittest import mock
    folder = 'http-client/cookie-v5'
    os.makedirs(os.path.join(root, folder), exist_ok=True)
    plain = 'http://localhost:%d' % plain_srv.server_port
    tls = 'https://localhost:%d' % SPORT
    with open(os.path.join(root, folder, hc.ENV_FILE), 'w') as output:
        json.dump({'dev cookie-a': {'base': plain}, 'dev cookie-b': {'base': plain}}, output)
    a, b = folder + '/login.http', folder + '/echo.http'
    def run(source, file=a, env='dev cookie-a'):
        with open(os.path.join(root, file), 'w') as output:
            output.write(source)
        return m.http_gui_run(KEY, file, env, None)
    def contains(record, name):
        return record.get('ok') and any(t['name'] == name and t['ok'] for r in record['requests'] for t in r['tests'])
    def echo(expected, base='{{base}}', pre=''):
        return pre + 'GET ' + base + '/api/cookie/echo\n\n> {% client.test("cookie expectation", () => client.assert(' + expected + ')); %}\n'
    cookie_headers['/api/cookie/v5-login'] = ['persistent=v5-secret; Path=/; HttpOnly; SameSite=Lax']
    result = run('GET {{base}}/api/cookie/v5-login\n')
    check('cookie login succeeds in its first native run', result.get('ok'), result)
    result = run(echo('response.body.cookie.includes("persistent=v5-secret")'), b)
    check('cookie survives a second native run and another HTTP file', contains(result, 'cookie expectation'), result)
    check('stored cookie credentials are masked in agent result', 'v5-secret' not in json.dumps(result.get('agent')), result.get('agent'))
    result = run(echo('response.body.cookie === ""'), b, 'dev cookie-b')
    check('cookie state is isolated between environment names', contains(result, 'cookie expectation'), result)
    other = folder + '/other'
    os.makedirs(os.path.join(root, other), exist_ok=True)
    with open(os.path.join(root, other, hc.ENV_FILE), 'w') as output:
        json.dump({'dev cookie-a': {'base': plain}}, output)
    result = run(echo('response.body.cookie === ""'), other + '/echo.http')
    check('same environment name in another env directory has no shared cookie', contains(result, 'cookie expectation'), result)
    cookie_headers['/api/cookie/v5-ignore'] = ['persistent=replaced; Path=/']
    result = run('### Ignore response cookie\n< {% request.variables.set("x", 1); %}\n# @no-cookie-jar\n'
                 'GET {{base}}/api/cookie/v5-ignore\n\n### Echo\nGET {{base}}/api/cookie/echo\nCookie: manual=authored\n\n'
                 '> {% client.test("both cookies", () => client.assert(response.body.cookie.includes("persistent=v5-secret") && '
                 'response.body.cookie.includes("manual=authored"))); %}\n')
    check('no-cookie-jar preserves existing state and authored Cookie header', contains(result, 'both cookies'), [(r['status'], r['body'], r['tests']) for r in result.get('requests', [])])
    cookie_headers['/api/cookie/v5-secure'] = ['tls=v5-tls; Secure; Path=/']
    result = run('### Set secure\nGET ' + tls + '/api/cookie/v5-secure\n\n### Plain\n' +
                 echo('!response.body.cookie.includes("tls=v5-tls")'))
    check('Secure response cookie is withheld from HTTP within the same run', contains(result, 'cookie expectation'), result)
    result = run(echo('response.body.cookie.includes("tls=v5-tls")', tls), b)
    check('Secure cookie remains available to HTTPS in a later run', contains(result, 'cookie expectation'), result)
    result = run(echo('response.body.cookie === ""', 'https://127.0.0.1:%d' % SPORT), b)
    check('host-only cookies never leak to a different hostname', contains(result, 'cookie expectation'), result)
    cookie_headers['/api/cookie/v5-redirect'] = ['redirect=v5-redirect; Path=/']
    cookie_redirects['/api/cookie/v5-redirect'] = plain + '/api/cookie/echo'
    result = run('GET {{base}}/api/cookie/v5-redirect\n\n'
                 '> {% client.test("redirect cookie", () => client.assert(response.body.cookie.includes("redirect=v5-redirect"))); %}\n')
    check('redirect Set-Cookie is saved before redirected request selection', contains(result, 'redirect cookie'), result)
    cookie_headers['/api/cookie/v5-failure'] = ['completed=v5-completed; Path=/']
    result = run('GET {{base}}/api/cookie/v5-failure\n\n> {% throw new Error("synthetic later failure"); %}\n')
    failed = not result.get('ok')
    result = run(echo('response.body.cookie.includes("completed=v5-completed")'), b)
    check('completed login cookie survives a later handler failure without replay', failed and contains(result, 'cookie expectation'), result)
    cookie_headers['/api/cookie/v5-delete'] = ['persistent=; Path=/; Max-Age=0']
    result = run('GET {{base}}/api/cookie/v5-delete\n')
    result = run(echo('!response.body.cookie.includes("persistent=")'), b)
    check('cookie deletion persists across native runs', contains(result, 'cookie expectation'), result)
    cookie_headers['/api/cookie/v5-cross-host'] = ['hostOnly=v5-host-only; Path=/']
    other_host = 'http://127.0.0.1:%d' % plain_srv.server_port
    cookie_redirects['/api/cookie/v5-cross-host'] = other_host + '/api/cookie/echo'
    result = run('### Redirect\nGET {{base}}/api/cookie/v5-cross-host\nCookie: explicit=v5-private\n\n'
                 '> {% client.test("cross-host redirect", () => client.assert(response.body.cookie === "")); %}\n\n'
                 '### Approve the second host\n' + echo('response.body.cookie === ""', other_host))
    check('redirect does not leak host-only state or an authored Cookie to another host',
          contains(result, 'cross-host redirect') and contains(result, 'cookie expectation'), result)
    cookie_headers['/api/cookie/v5-domain'] = ['domainCookie=v5-domain; Domain=example.com; Path=/',
                                             'exactCookie=v5-exact; Path=/', 'superCookie=invalid; Domain=com; Path=/']
    lookup = socket.getaddrinfo
    def resolve(name, *args, **kwargs):
        if name in ('app.example.com', 'sub.app.example.com', 'other.example.com'):
            name = '127.0.0.1'
        return lookup(name, *args, **kwargs)
    with mock.patch.object(socket, 'getaddrinfo', side_effect=resolve):
        result = run('### Domain seed\nGET http://app.example.com:%d/api/cookie/v5-domain\n\n' % plain_srv.server_port +
                     '### Domain subhost\n' + echo('response.body.cookie === "domainCookie=v5-domain"',
                                                 'http://sub.app.example.com:%d' % plain_srv.server_port) +
                     '\n### Domain sibling\n' + echo('response.body.cookie === "domainCookie=v5-domain"',
                                                     'http://other.example.com:%d' % plain_srv.server_port))
    check('Domain cookies reach permitted subhosts while host-only and public-suffix cookies do not',
          result.get('ok') and len(result['requests']) == 3 and all(r['ok'] for r in result['requests']), result)
    clone = _env.make_project('cookie-v5-worktree', {folder + '/' + hc.ENV_FILE:json.dumps({'dev cookie-a':{'base':plain}}),
                                                    b:echo('response.body.cookie === ""')})
    clone_key = 'team-x/cookie-v5-worktree'
    _env.register(clone_key, clone)
    result = m.http_gui_run(clone_key, b, 'dev cookie-a', None)
    check('another physical project/worktree never inherits the original login', contains(result, 'cookie expectation'), result)
    before = len(hits)
    result = run('### Collection\n< {% request.variables.set("id", Array.from({length:260}, (_, i) => i)); %}\n'
                 'GET {{base}}/api/cookie/iteration?id={{id}}\n')
    check('native cookie selection does not introduce a 256-request collection limit', result.get('ok') and
          len(result['requests']) == 260 and len(hits) == before + 260, {'ok':result.get('ok'), 'count':len(result.get('requests', [])),
                                                                     'hits':len(hits)-before, 'output':result.get('output')})
    before = len(hits)
    result = run('< {% request.variables.set("id", [1,2]); request.variables.set("out", "exports/cookie-" + request.iteration() + ".json"); %}\n'
                 'GET {{base}}/api/cookie/iteration?id={{id}}\n\n>>! {{out}}\n')
    def masked_export(row, headers):
        sent = next((v for k,v in headers.items() if k.lower() == 'cookie'), '')
        with open(os.path.join(root, row['saved'])) as output:
            saved = json.load(output)['cookie']
        names = lambda value: {part.strip().partition('=')[0] for part in value.split(';') if part.strip()}
        return names(sent) == names(saved) and all(part.strip().partition('=')[2] == hc.MASK for part in saved.split(';'))
    check('cookie-bearing collection exports mask credentials on each saved response', result.get('ok') and
          len(result['requests']) == 2 and len(hits) == before + 2 and all(
              masked_export(row, headers) for row, (_, headers) in zip(result['requests'], hits[before:])),
          {'ok':result.get('ok'), 'count':len(result.get('requests', [])), 'hits':len(hits)-before})


def transport_host_check():
    from unittest import mock
    import socket
    rel = 'http-client/dns.http'
    name = 'piper-http-dns.invalid'
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET http://' + name + ':' + str(plain_srv.server_port) + '/api/feed-short\n')
    lookup = socket.getaddrinfo
    delegated = []
    def resolve(host, *args, **kwargs):
        if host == name:
            delegated.append(host)
            host = '127.0.0.1'
        return lookup(host, *args, **kwargs)
    before = len(hits)
    with mock.patch.object(socket, 'getaddrinfo', side_effect=resolve):
        record = m.http_gui_run(KEY, rel, '', None)
    check('native DNS is delegated to the broker and original Host is preserved', record.get('ok') and
          delegated == [name] and len(hits) == before + 1 and
          hits[-1][1].get('Host', hits[-1][1].get('host')) == name + ':' + str(plain_srv.server_port),
          {'ok': record.get('ok'), 'delegated': delegated, 'requests': record.get('requests'), 'output': record.get('output')})
    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
    try:
        server = IPv6Server(('::1', 0), H)
    except OSError as error:
        print('SKIP native IPv6 loopback: ' + str(error), flush=True)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with open(os.path.join(root, rel), 'w') as output:
            output.write('GET http://[::1]:' + str(server.server_port) + '/api/feed-short\n')
        before = len(hits)
        record = m.http_gui_run(KEY, rel, '', None)
        check('native IPv6 destination passes the exact SOCKS grant', record.get('ok') and len(hits) == before + 1, record)
    finally:
        server.shutdown()
        server.server_close()


def transport_timeout_check():
    with open(os.path.join(root, 'http-client/timeout.http'), 'w') as f:
        f.write('GET http://localhost:%d/api/slow\n' % plain_srv.server_port)
    old_http_policy = dict(m.policy.get('http') or {})
    try:
        m.policy['http'] = dict(old_http_policy, connect_timeout_ms=2000, socket_timeout_ms=100)
        before = len(hits)
        short = m.http_gui_run(KEY, 'http-client/timeout.http', '', None)
        m.policy['http']['socket_timeout_ms'] = 2000
        long = m.http_gui_run(KEY, 'http-client/timeout.http', '', None)
        check('configured socket timeout changes the real native transport without retries', short.get('ok') is False and
              long.get('ok') is True and [p for p, _ in hits[before:]] == ['/api/slow', '/api/slow'],
              {'short': short, 'long': long, 'hits': hits[before:]})
    finally:
        m.policy['http'] = old_http_policy


def runtime_metadata_check():
    rel = 'http-client/runtime-metadata.http'
    source = ('### Callback\n< {% client.log("native pre log"); %}\nGET {{domain}}/api/feed-short\n\n'
              '> {% import "./runtime-side-effect.js"; client.log("native response log"); client.test("native callback", () => {'
              'client.log("native callback log"); client.global.set("fromCallback", {value:42});}); %}\n\n'
              '### Exit\nGET {{domain}}/api/feed-short\n\n> {% client.log("native exit log"); '
              'client.global.set("fromExit", {value:43}); client.test("canceled test", () => {throw Error("not run");}); '
              'client.exit(); throw Error("unreachable"); %}\n')
    with open(os.path.join(root, rel), 'w') as output:
        output.write(source)
    with open(os.path.join(root, 'http-client/runtime-side-effect.js'), 'w') as output:
        output.write('client.log("module side effect"); client.test("module side effect callback", () => client.log("module callback log"));')
    before = len(hits)
    result = m.http_gui_run(KEY, rel, TUN, None)
    rows = result.get('requests', [])
    check('native pre/response/callback logs arrive independently of JUnit registrations', result.get('ok') and
          len(rows) == 2 and all(text in rows[0].get('script_log', '') for text in
          ('native pre log', 'native response log', 'native callback log', 'module side effect', 'module callback log')) and
          [t['name'] for t in rows[0]['tests']] == ['module side effect callback', 'native callback'], result)
    check('native exit logs/state survive without executing canceled tests', len(rows) == 2 and
          rows[1].get('script_log') == 'native exit log\n' and rows[1].get('script_exited') and
          rows[1]['tests'] == [] and
          common.read_json(hc.GLOBALS_FILE, {}).get(m._globals_key(KEY, rel, TUN), {}).get('fromExit') == {'value':43}, result)
    check('runtime metadata never repeats HTTP or edits authored scripts', len(hits) == before + 2 and
          open(os.path.join(root, rel)).read() == source, result)


def tunnel_tls_scope_check():
    from unittest import mock
    folder = os.path.join(root, 'http-client/tls-scope')
    os.makedirs(folder, exist_ok=True)
    rel = 'http-client/tls-scope/main.http'
    original_policy = m.policy.get('http')
    configuration = {'domain':'http://localhost:%d' % plain_srv.server_port,
        'Security':{'Auth':{'issuer':{'Type':'OAuth2', 'Grant Type':'Client Credentials',
            'Client ID':'scoped-client', 'Client Secret':PW, 'Token URL':'https://localhost:%d/api/auth-token' % SPORT}}}}
    def configure():
        with open(os.path.join(folder, hc.PRIVATE_FILE), 'w') as output:
            json.dump({'test scoped':configuration}, output)
        with open(os.path.join(root, rel), 'w') as output:
            output.write('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("issuer")}}\n')
    s.excludes.append('http-client/tls-scope/' + hc.PRIVATE_FILE)
    try:
        m.policy['http'] = dict(original_policy or {}, insecure=[])
        configure()
        before, before_tokens = len(hits), len(uploads)
        tunnel = {'label':'synthetic resource tunnel', 'port':plain_srv.server_port}
        with mock.patch.object(m, '_http_tunnel', return_value=tunnel), mock.patch.object(
                m, '_http_tunnel_open', return_value=plain_srv.server_port):
            result = tool(file=rel, env='test scoped')
        check('resource tunnel never disables TLS verification for an unrelated OAuth issuer',
              not result.get('ok') and len(hits) == before and len(uploads) == before_tokens, result)
        configuration['SSLConfiguration'] = {'verifyHostCertificate':False}
        configure()
        with mock.patch.object(m, '_http_tunnel', return_value=tunnel), mock.patch.object(
                m, '_http_tunnel_open', return_value=plain_srv.server_port):
            result = tool(file=rel, env='test scoped')
        check('explicit TLS opt-out still works for managed OAuth beside a tunnel',
              result.get('ok') and len(hits) == before + 1 and len(uploads) == before_tokens + 1, result)
        configuration.pop('SSLConfiguration')
        configuration['domain'] = 'https://localhost:8443'
        configuration['Security']['Auth']['issuer']['Token URL'] = 'https://localhost:8443/api/auth-token'
        configure()
        tunnel['port'] = 8443
        before, before_tokens = len(hits), len(uploads)
        with mock.patch.object(m, '_http_tunnel', return_value=tunnel), mock.patch.object(
                m, '_http_tunnel_open', return_value=SPORT):
            result = tool(file=rel, env='test scoped')
        check('OAuth and native HTTPS retain the automatic certificate exception inside their actual tunnel',
              result.get('ok') and len(hits) == before + 1 and len(uploads) == before_tokens + 1, result)
        m.http_gui_envcfg_save(KEY, rel, 'test scoped', {'verify_tls':True})
        before, before_tokens = len(hits), len(uploads)
        with mock.patch.object(m, '_http_tunnel', return_value=tunnel), mock.patch.object(
                m, '_http_tunnel_open', return_value=SPORT):
            result = tool(file=rel, env='test scoped')
        check('explicit server verification wins over the automatic tunnel exception',
              not result.get('ok') and len(hits) == before and len(uploads) == before_tokens, result)
    finally:
        if original_policy is None:
            m.policy.pop('http', None)
        else:
            m.policy['http'] = original_policy
        s.excludes.remove('http-client/tls-scope/' + hc.PRIVATE_FILE)


def managed_auth_check():
    name = 'test auth v5'
    rel = 'http-client/auth-v5.http'
    token_url = 'http://127.0.0.1:%d/api/auth-token' % plain_srv.server_port
    config = {'Type':'OAuth2', 'Grant Type':'Client Credentials', 'Client ID':'native-client-v5',
              'Token URL':token_url}
    ENV[name] = {'domain':'https://localhost:%d' % SPORT, 'SSLConfiguration':{'verifyHostCertificate':False},
                 'Security':{'Auth':{'cc':config, 'mock':{'Type':'Mock'},
                    'error':dict(config, **{'Token URL':token_url.replace('auth-token', 'auth-error')}),
                    'redirect':dict(config, **{'Token URL':token_url.replace('auth-token', 'auth-redirect')})}}}
    PRIV[name] = {'Security':{'Auth':{'cc':{'Client Secret':PW}, 'error':{'Client Secret':PW},
                                    'redirect':{'Client Secret':PW}, 'mock':{'Token':'native-mock-v5-42', 'ID Token':'native-mock-id-v5-42'}}}}
    def run(source):
        with open(os.path.join(root, rel), 'w') as output:
            output.write(source)
        return tool(file=rel, env=name, include_body=True)
    for file, doc in ((hc.ENV_FILE, ENV), (hc.PRIVATE_FILE, PRIV)):
        with open(os.path.join(root, 'http-client', file), 'w') as output:
            json.dump(doc, output)
    try:
        before, before_tokens = len(hits), len(uploads)
        result = run('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("cc")}}\n\n'
                     '> {% client.test("native managed token", () => client.assert(response.body.auth === "Bearer native-access-v5-42")); %}\n'
                     '\n### Same run cache\nGET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.idToken("cc")}}\n')
        check('native original auth.token and idToken resolve lazily and share one acquisition per run', result.get('ok') and
              len(hits) == before + 2 and len(uploads) == before_tokens + 1 and
              hits[-2][1].get('Authorization') == 'Bearer native-access-v5-42' and hits[-1][1].get('Authorization') == 'Bearer native-id-v5-42', result)
        check('managed native token response test really runs and returned echoes and logs are masked',
              any(test['ok'] and test['name'] == 'native managed token' for test in result.get('requests', [{}])[0].get('tests', [])) and
              not any(value in json.dumps(result) for value in ('native-access-v5-42', 'native-id-v5-42', 'native-refresh-v5-42', PW)), result)
        before_tokens = len(uploads)
        result = run('< {% const id="mock"; request.variables.set("fromAuth", $auth.token(id)); %}\n'
                     'GET {{domain}}/api/echo\nAuthorization: Bearer {{fromAuth}}\n')
        check('native JS managed auth function accepts a computed configuration ID without an issuer request', result.get('ok') and
              hits[-1][1].get('Authorization') == 'Bearer native-mock-v5-42' and len(uploads) == before_tokens, result)
        for label in ('error', 'redirect', 'unknown'):
            before = len(hits)
            result = run('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("' + label + '")}}\n')
            check('managed auth ' + label + ' never sends an unresolved token to the protected resource',
                  result.get('ok') is False and len(hits) == before and 'native-access-v5-42' not in json.dumps(result), result)
        before, before_tokens = len(hits), len(uploads)
        original = m.ask
        m.ask = lambda *args, **kwargs:False
        try:
            result = run('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("cc")}}\n')
        finally:
            m.ask = original
        check('denying the initial HTTP card sends neither token nor protected resource request',
              len(hits) == before and len(uploads) == before_tokens and not result.get('ok'), result)
    finally:
        ENV.pop(name, None)
        PRIV.pop(name, None)
        for file, doc in ((hc.ENV_FILE, ENV), (hc.PRIVATE_FILE, PRIV)):
            with open(os.path.join(root, 'http-client', file), 'w') as output:
                json.dump(doc, output)


def browser_auth_check():
    import socket
    import urllib.parse
    import urllib.request
    import hashlib
    import base64
    issuer_hits, authorized, challenges = [], threading.Event(), {}
    class Issuer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def send(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            fields = dict(urllib.parse.parse_qsl(parsed.query))
            issuer_hits.append(('GET', parsed.path))
            if parsed.path == '/authorize':
                challenges['value'] = fields.get('code_challenge')
                challenges['redirect'] = fields.get('redirect_uri')
                target = fields['redirect_uri'] + '?' + urllib.parse.urlencode({'code':'native-code42','state':fields['state']})
                self.send_response(302)
                self.send_header('Location', target)
                self.send_header('Content-Length', '0')
                self.end_headers()
            else:
                authorized.set()
                self.send({'ok':True})
        def do_POST(self):
            fields = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()))
            issuer_hits.append(('POST', self.path))
            if self.path == '/device':
                self.send({'device_code':'native-device-secret42','user_code':'NATIVE-CODE','verification_uri':base + '/verify',
                           'expires_in':60,'interval':.05})
            elif fields.get('grant_type') == 'authorization_code':
                verifier = fields.get('code_verifier', '')
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                good = challenge == challenges.get('value') and fields.get('redirect_uri') == challenges.get('redirect') and fields.get('code') == 'native-code42'
                self.send({'access_token':'native-browser-access42','id_token':'native-browser-id42','expires_in':3600} if good else {'error':'invalid_grant'}, 200 if good else 400)
            elif authorized.is_set() and fields.get('device_code') == 'native-device-secret42':
                self.send({'access_token':'native-browser-access42','id_token':'native-browser-id42','expires_in':3600})
            else:
                self.send({'error':'authorization_pending','error_description':PW},400)
    issuer = ThreadingHTTPServer(('127.0.0.1', 0), Issuer)
    base = 'http://127.0.0.1:%d' % issuer.server_port
    threading.Thread(target=issuer.serve_forever, daemon=True).start()
    name, rel = 'test browser v5', 'http-client/browser-v5.http'
    original_public = open(root + '/http-client/' + hc.ENV_FILE).read()
    original_private = open(root + '/http-client/' + hc.PRIVATE_FILE).read()
    try:
        for grant in ('Authorization Code', 'Device Authorization'):
            for allow in (False, True):
                authorized.clear()
                with socket.socket() as listener:
                    listener.bind(('127.0.0.1',0))
                    callback = 'http://127.0.0.1:%d/callback' % listener.getsockname()[1]
                config = {'Type':'OAuth2','Grant Type':grant,'Client ID':'native-browser-fixture','Client Credentials':'none',
                          'Token URL':base + '/token','Auth URL':base + '/authorize','Device Auth URL':base + '/device',
                          'Redirect URL':callback,'PKCE':True}
                public, private = json.loads(original_public), json.loads(original_private)
                public[name] = {'domain':'https://localhost:%d' % SPORT,'SSLConfiguration':{'verifyHostCertificate':False},'Security':{'Auth':{'browser':config}}}
                for file, document in ((hc.ENV_FILE, public),(hc.PRIVATE_FILE, private)):
                    with open(root + '/http-client/' + file,'w') as output:
                        json.dump(document,output)
                source = ('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("browser")}}\n\n'
                          '> {% client.test("browser token really resolved", () => client.assert(response.body.auth === "Bearer native-browser-access42")); %}\n')
                with open(root + '/' + rel,'w') as output:
                    output.write(source)
                before = len(hits)
                answers = []
                thread = threading.Thread(target=lambda:answers.append(tool(file=rel, env=name, include_body=True)))
                thread.start()
                deadline = time.monotonic() + 45
                card = None
                while time.monotonic() < deadline:
                    card = next((p for p in m.pending_list() if p['kind'] == 'http_auth_signin'), None)
                    if card:
                        break
                    time.sleep(.05)
                try:
                    check('native ' + grant + ' pauses on a mandatory one-shot sign-in card',
                          card and card['session'] == s.id and not card.get('always') and not card.get('session_always') and len(hits) == before and
                          not any(value in json.dumps(card) for value in ('native-device-secret42','code_verifier','native-code42')), card)
                    if card:
                        if allow:
                            with urllib.request.urlopen(m.http_auth_open(card['id'])['url'],timeout=5) as response:
                                response.read()
                        else:
                            m.decide(card['id'],False)
                finally:
                    thread.join(60)
                result = answers[0] if answers else {'exception':'native sign-in did not finish'}
                check('native ' + grant + (' completes original auth syntax and native response tests' if allow else ' Deny never sends protected request'),
                      not thread.is_alive() and ((result.get('ok') and len(hits) == before + 1 and
                      any(t['ok'] and t['name'] == 'browser token really resolved' for row in result.get('requests',[]) for t in row.get('tests',[]))) if allow else
                      (not result.get('ok') and len(hits) == before)) and
                      not any(value in json.dumps(result) for value in ('native-browser-access42','native-browser-id42','native-device-secret42')), result)
    finally:
        for file, source in ((hc.ENV_FILE,original_public),(hc.PRIVATE_FILE,original_private)):
            with open(root + '/http-client/' + file,'w') as output:
                output.write(source)
        issuer.shutdown()
        issuer.server_close()


def runtime_code_check():
    directory = os.path.join(root, 'http-client/code-v5')
    os.makedirs(directory, exist_ok=True)
    rel = 'http-client/code-v5/main.http'
    def write(name, source):
        with open(os.path.join(directory, name), 'w') as output:
            output.write(source)
    def run(source):
        write('main.http', source)
        return tool(file=rel, env=TUN)
    pre = '< {% const name = "./computed.js"; const {value} = await import(name); request.variables.set("computedValue", value); %}\n'
    request = 'GET {{domain}}/api/feed-short\nX-Computed: {{computedValue}}\n'
    original = m.ask
    seen = []
    def approve(kind, session, summary, payload, **kwargs):
        if kind == 'http_runtime_code':
            seen.append((summary, payload))
            write('computed.js', 'throw Error("changed after approval");')
            write('literal.js', 'export const value = 99;')
        return original(kind, session, summary, payload, **kwargs)
    write('computed.js', 'export {value} from "./literal.js"; // ' + PW)
    write('literal.js', 'export const value = 42;')
    before = len(hits)
    m.ask = approve
    try:
        result = run(pre + request)
    finally:
        m.ask = original
    check('computed pre import approves its complete literal graph and runs only frozen bytes', result.get('ok') and
          len(hits) == before + 1 and hits[-1][1].get('X-Computed') == '42' and len(seen) == 1 and
          len(seen[0][1]['deps']) == 1 and seen[0][1]['files'] == 2 and PW not in json.dumps(seen) and
          len(seen[0][1]['entry']['sha256']) == 64, result)
    write('computed.js', 'const inner = "./inner.js"; export const value = (await import(inner)).value;')
    write('inner.js', 'export const value = 43;')
    before_cards = len(cards)
    result = run(pre + request)
    check('a computed import inside newly approved code asks separately before executing', result.get('ok') and
          hits[-1][1].get('X-Computed') == '43' and
          sum(kind == 'http_runtime_code' for kind, _ in cards[before_cards:]) == 2, result)
    write('computed.js', 'export const value = 44;')
    before = len(hits)
    m.ask = lambda kind, *args, **kwargs: False if kind == 'http_runtime_code' else original(kind, *args, **kwargs)
    try:
        declined_pre = run(pre + request)
        middle = len(hits)
        declined_response = run('GET {{domain}}/api/feed-short\n\n> {% const name = "./computed.js"; await import(name); %}\n'
                               '\n### Never sent\nGET {{domain}}/api/login\n')
    finally:
        m.ask = original
    check('denying computed pre code sends no HTTP and never replays the pre-handler', declined_pre.get('ok') is False and
          middle == before, declined_pre)
    check('denying computed response code keeps the received response and stops later HTTP', declined_response.get('ok') is False and
          len(hits) == middle + 1 and declined_response.get('requests', [{}])[0].get('status') == 200, declined_response)
    for label, name in [('hidden', '../http-client.private.env.json'), ('outside', '../../../outside.js')]:
        before, before_cards = len(hits), len(cards)
        source = '< {% const name = ' + json.dumps(name) + '; await import(name); %}\nGET {{domain}}/api/feed-short\n'
        result = run(source)
        check('computed code refuses ' + label + ' before execution or a code card', result.get('ok') is False and
              len(hits) == before and not any(kind == 'http_runtime_code' for kind, _ in cards[before_cards:]), result)
    source = '< {% if (true) {} /import("ignore.js")/; const y = {} / 2; const name = "./computed.js"; '
    result = run(source + 'const {value} = await import(name); request.variables.set("computedValue", value); %}\n' + request)
    check('native parser accepts regex and division beside computed imports without lexer refusals', result.get('ok') and
          hits[-1][1].get('X-Computed') == '44', result)
    for label, export, loader in [
        ('computed', '>>! ./downloaded.js', 'const name = "./downloaded.js"; const {value} = await import(name);'),
        ('literal', '>>! ./downloaded-static.js', 'import {value} from "./downloaded-static.js";'),
        ('callback filename', '> {% client.test("filename", () => client.global.set("codeName", "downloaded-callback.js")); %}\n>>! ./{{codeName}}',
         'const name = "./downloaded-callback.js"; const {value} = await import(name);')]:
        before, before_cards = len(hits), len(cards)
        source = 'GET {{domain}}/api/code-v5\n\n' + export + '\n\n### Use downloaded code\n< {% ' + loader + ' request.variables.set("computedValue", value); %}\n' + request
        result = run(source)
        check('downloaded ' + label + ' module needs a separate execution approval without HTTP replay', result.get('ok') and
              len(hits) == before + 2 and hits[-1][1].get('X-Computed') == '45' and
              sum(kind == 'http_runtime_code' for kind, _ in cards[before_cards:]) == 1, result)
    os.makedirs(os.path.join(directory, 'nested'), exist_ok=True)
    write('nested/dep.js', 'export const value = 46;')
    write('nested/pre.js', 'import {value} from "./dep.js"; request.variables.set("computedValue", value + shared);')
    before, before_cards = len(hits), len(cards)
    source = ('={% const shared = 7; %}\nGET {{domain}}/api/feed-short\n\n'
              '> {% client.test("script filename", () => client.global.set("scriptName", "nested/pre.js")); %}\n'
              '\n### Runtime pre file\n< {{scriptName}}\n' + request)
    result = run(source)
    check('variable pre-script filename keeps native scope common bindings and imports relative to the script', result.get('ok') and
          len(hits) == before + 2 and hits[-1][1].get('X-Computed') == '53' and
          sum(kind == 'http_runtime_code' for kind, _ in cards[before_cards:]) == 1,
          dict(errors=[r.get('error') for r in result.get('requests', [])], result=result))
    write('nested/post.js', 'import {value} from "./dep.js"; client.test("runtime response file", () => '
          'client.assert(response.status === 200 && value === 46 && shared === 7));')
    result = run('={% const shared = 7; %}\n< {% request.variables.set("handler", "nested/post.js"); %}\n'
                 'GET {{domain}}/api/feed-short\n\n> {{handler}}\n')
    check('variable response-script filename uses current native request variables and shared lexical scope', result.get('ok') and
          any(test['name'] == 'runtime response file' and test['ok'] for test in result.get('requests', [{}])[0].get('tests', [])),
          dict(errors=[r.get('error') for r in result.get('requests', [])], result=result))
    before = len(hits)
    result = run('GET {{domain}}/api/code-root-v5\n\n>>! ./downloaded-root.js\n\n'
                 '### Downloaded pre\n< ./downloaded-root.js\n' + request)
    check('downloaded external pre-script executes its approved bytes through the native root scope', result.get('ok') and
          len(hits) == before + 2 and hits[-1][1].get('X-Computed') == '47', result)
    write('numbered.js', '// existing file must survive unchanged')
    for name in ('numbered-1.js', 'numbered-2.js'):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            os.unlink(path)
    before = len(hits)
    result = run('GET {{domain}}/api/code-root-v5\n\n>> ./numbered.js\n\n'
                 '### Numbered downloaded pre\n< ./numbered-1.js\n' + request)
    check('numbered response export can execute as an approved external pre-script without replacing its original',
          result.get('ok') and len(hits) == before + 2 and hits[-1][1].get('X-Computed') == '47' and
          open(os.path.join(directory, 'numbered.js')).read() == '// existing file must survive unchanged', result)
    write('nested/imported.http', '={% const shared = 7; %}\nGET {{domain}}/api/feed-short\n\n'
          '> {% client.global.set("importedScriptV5", "pre.js"); %}\n\n### Imported dynamic root\n'
          '< {{importedScriptV5}}\n' + request)
    before = len(hits)
    result = run('run ./nested/imported.http\n')
    check('imported dynamic external script retains its HTTP owner common scope and relative module directory',
          result.get('ok') and len(hits) == before + 2 and hits[-1][1].get('X-Computed') == '53', result)
    runtime_code_errors_check()


def runtime_code_errors_check():
    directory = os.path.join(root, 'http-client/code-errors-v5')
    os.makedirs(directory + '/nested', exist_ok=True)
    for case, phase, common in [('pre','pre',''),('response','response',''),
                                ('common-pre','pre','={%\nconst commonA = 1;\nconst commonB = 2;\n%}\n'),
                                ('common-response','response','={%\nconst commonA = 1;\nconst commonB = 2;\n%}\n')]:
        variable = 'failingScriptV5' + case.replace('-', '')
        script = 'nested/failing-' + case + '.js'
        with open(directory + '/' + script, 'w') as output:
            output.write('// original line one\nthrow Error("native source map probe");\n')
        source = common + ('GET {{domain}}/api/feed-short\n\n> {% client.global.set("' + variable + '", "' + script + '"); %}\n'
                 '\n### Dynamic failing script\n')
        if phase == 'pre':
            source += '< {{' + variable + '}}\nGET {{domain}}/api/feed-short\n'
        else:
            source += 'GET {{domain}}/api/feed-short\n\n> {{' + variable + '}}\n'
        with open(directory + '/main.http', 'w') as output:
            output.write(source)
        before = len(hits)
        result = tool(file='http-client/code-errors-v5/main.http', env=TUN)
        check('dynamic ' + case + ' errors identify original filename and line without losing earlier results',
              not result.get('ok') and len(hits) == before + (1 if phase == 'pre' else 2) and
              script + ':2' in json.dumps(result) and 'code-roots/' not in json.dumps(result) and
              len(result.get('requests', [])) == 2 and result['requests'][0]['ok'], result)


def completion_v3_check():
    directory = os.path.join(root, 'http-client/completion')
    files = {
        'value.js': 'export const value = 42; export function check(){client.assert(response.status === 200);}',
        'nested/pre.js': 'request.variables.set("commonValue", value);',
        'nested/post.js': 'check(); client.test("common external", () => client.assert(value === 42));',
        'common.http': '={% import {value, check} from "./value.js"; %}\n### Inline\n'
                       '< {% request.variables.set("commonValue", value); %}\nGET {{domain}}/api/feed-short\nX-Common: {{commonValue}}\n\n'
                       '> {% check(); client.test("common inline", () => client.assert(value === 42)); %}\n\n'
                       '### External\n< ./nested/pre.js\nGET {{domain}}/api/feed-short\nX-Common: {{commonValue}}\n\n> ./nested/post.js\n',
        'main.http': 'run ./common.http\n',
        'extensionless.http': 'GET {{domain}}/api/feed-short\n\n> {% import {value} from "./value"; '
                              'client.test("extensionless native module", () => client.assert(value === 42)); %}\n',
        'bad-common.http': '={%\nthrow Error("native common source");\n%}\n'
                           '< {% client.log("unreachable"); %}\nGET {{domain}}/api/feed-short\n',
        'bad-external.http': '={% const value = 42; %}\nGET {{domain}}/api/feed-short\n\n> ./nested/bad.js\n',
        'nested/bad.js': 'client.log(value);\nthrow Error("native external source");\n',
        'bad-graph.http': 'run ./bad-common.http\n',
        'export-timing.http': '< {% client.global.set("exportDestination", {path:"exports/native-before.bin"}); %}\n'
                              'GET {{domain}}/api/iteration?timing=before\n\n'
                              '> {% client.global.set("exportDestination", {path:"exports/native-after.bin"}); %}\n'
                              '>>! {{exportDestination.path}}\n',
        'set.http': 'GET {{domain}}/api/feed-short\n\n> {% client.global.set("sharedAcrossFiles", {id:42}); %}\n',
        'get.http': '< {% if(client.global.get("sharedAcrossFiles").id !== 42) throw Error("shared state missing"); %}\n'
                    'GET {{domain}}/api/feed-short\nX-Shared: {{sharedAcrossFiles.id}}\n',
        'iterations.http': '### Ordinary\nGET {{domain}}/api/feed-short\n\n'
                           '### Collection\n< {% request.variables.set("id", ["first","second"]); '
                           'request.variables.set("destination", "exports/iteration-" + request.iteration() + ".bin"); %}\n'
                           'GET {{domain}}/api/iteration?id={{id}}\n\n'
                           '> {% client.test("native iteration " + request.iteration(), () => client.assert(response.status === 200)); %}\n'
                           '>>! {{destination}}\n'}
    for path, content in files.items():
        full = os.path.join(directory, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as output:
            output.write(content)
    for filename, index in (('common.http', None), ('common.http', 1), ('main.http', None)):
        before = len(hits)
        result = tool(file='http-client/completion/' + filename, env=TUN, index=index)
        expected = 1 if index is not None else 2
        check('native common inline/external scope and graph ' + filename + '/' + str(index),
              result.get('ok') and len(hits) == before + expected and
              all(headers.get('X-Common') == '42' for _, headers in hits[before:]) and
              sum(len(row['tests']) for row in result.get('requests', [])) == expected, result)
    first = tool(file='http-client/completion/set.http', env=TUN)
    second = tool(file='http-client/completion/get.http', env=TUN)
    check('native Run all preserves globals between different files sharing the same environment source',
          first.get('ok') and second.get('ok') and hits[-1][1].get('X-Shared') == '42', second)
    before = len(hits)
    result = tool(file='http-client/completion/iterations.http', env=TUN)
    rows = result.get('requests', [])
    paths = ['http-client/completion/exports/iteration-%d.bin' % i for i in range(2)]
    check('native collection exports retain exact distinct binary bytes after an ordinary request without replay',
          result.get('ok') and len(hits) == before + 3 and len(rows) == 3 and
          [row.get('iteration') for row in rows[1:]] == [0,1] and
          [row.get('saved') for row in rows[1:]] == paths and
          all(os.path.isfile(os.path.join(root, path)) and open(os.path.join(root,path), 'rb').read() ==
              b'\x00\xff/api/iteration?id=' + identity for path, identity in zip(paths, (b'first',b'second'))), result)
    result = tool(file='http-client/completion/extensionless.http', env=TUN)
    check('native JS import with omitted extension keeps original source', result.get('ok') and
          any(test['name'] == 'extensionless native module' and test['ok']
              for row in result.get('requests', []) for test in row.get('tests', [])), result)
    for filename, expected, count in [('bad-common.http','bad-common.http:2',0),
                                      ('bad-external.http','nested/bad.js:2',1),
                                      ('bad-graph.http','bad-common.http:2',0)]:
        before = len(hits)
        result = tool(file='http-client/completion/' + filename, env=TUN)
        check('native adapted error maps to the authored source ' + filename, not result.get('ok') and
              len(hits) == before + count and 'http-client/completion/' + expected in json.dumps(result), result)
    before = len(hits)
    result = tool(file='http-client/completion/export-timing.http', env=TUN)
    path = 'http-client/completion/exports/native-before.bin'
    check('native export JSONPath resolves before response handler changes globals', result.get('ok') and
          len(hits) == before + 1 and result['requests'][0].get('saved') == path and
          open(os.path.join(root,path),'rb').read() == b'\x00\xff/api/iteration?timing=before' and
          not os.path.exists(os.path.join(directory,'exports/native-after.bin')), result)
    check('common/export/global adapters preserve every authored file',
          all(open(os.path.join(directory,path)).read() == content for path, content in files.items()))


def finite_sse_check():
    rel = 'http-client/sse-finite.http'
    source_path = os.path.join(root, rel)
    base = 'http://127.0.0.1:%d' % plain_srv.server_port
    for failed in (False, True):
        source = ('GET ' + base + '/api/sse-finite\n\n> {%\nlet messages = [];\n'
                  'response.body.onEachLine((line) => {\n'
                  '  messages.push(line); client.log("sse-line:" + line);\n'
                  '  client.test("stream line " + messages.length, () => client.assert(' +
                  ('false' if failed else 'true') + ', "finite-sse-late-assertion"));\n'
                  '}, () => {\n'
                  '  client.global.set("sseFinished", "native-sse-done");\n'
                  '  client.test("stream finished", () => client.assert(messages.includes("data: first") && messages.includes("data: second") && messages.every(line => typeof line === "string")));\n'
                  '});\n%}\n\n### after stream\nGET ' + base + '/api/echo\nX-SSE: {{sseFinished}}\n')
        with open(source_path, 'w') as authored:
            authored.write(source)
        before = len(hits)
        old_policy = dict(m.policy.get('http') or {})
        try:
            m.policy['http'] = dict(old_policy, timeout=15)
            result = tool(file=rel, env=TUN)
        finally:
            m.policy['http'] = old_policy
        requests = result.get('requests', [])
        tests = requests[0].get('tests', []) if requests else []
        check('finite SSE reports native late subscriber tests and onFinish' + (' failures' if failed else ''),
              bool(result.get('ok')) is (not failed) and len(requests) == 2 and
              [path for path, _ in hits[before:]] == ['/api/sse-finite', '/api/echo'] and
              open(source_path).read() == source and
              any(test['name'] == 'stream finished' and test['ok'] for test in tests) and
              any(test['name'].startswith('stream line ') and test['ok'] is (not failed) for test in tests), result)
        check('SSE callback globals reach the following native request without replay' + (' after failed test' if failed else ''),
              len(hits) == before + 2 and hits[-1][1].get('X-SSE') == 'native-sse-done', result)
    for path in ('/api/sse-fragments', '/api/sse-truncated'):
        source = ('GET ' + base + path + '\n\n> {%\nlet lines = [];\n'
                  'response.body.onEachLine(line => lines.push(line), () => {\n'
                  '  client.log(JSON.stringify(lines));\n'
                  '  client.test("complete UTF-8 and final line", () => client.assert(' +
                  ('lines.includes("data: é") && lines.includes("data: tail")' if path.endswith('fragments') else 'true') + '));\n'
                  '});\n%}\n')
        with open(source_path, 'w') as authored:
            authored.write(source)
        before = len(hits)
        old_policy = dict(m.policy.get('http') or {})
        try:
            m.policy['http'] = dict(old_policy, timeout=15)
            result = tool(file=rel, env=TUN)
        finally:
            m.policy['http'] = old_policy
        check('finite SSE preserves split UTF-8 and its final unterminated line' if path.endswith('fragments') else
              'truncated SSE fails without a successful end or replay',
              bool(result.get('ok')) is path.endswith('fragments') and len(hits) == before + 1 and
              hits[-1][0] == path and 'exception' not in result, result)
        if path.endswith('fragments') and not result.get('ok'):
            print('SSE fragment detail:', json.dumps(result), flush=True)
    for mode in ('unsubscribe', 'exit', 'timeout'):
        path = '/api/sse-open?case=' + mode
        ending = 'unsubscribe();' if mode == 'unsubscribe' else 'client.exit();' if mode == 'exit' else ''
        source = ('GET ' + base + path + '\n\n> {%\n'
                  'response.body.onEachLine((line, unsubscribe) => {\n'
                  '  client.global.set("sseReleased", "native-sse-released");\n'
                  '  client.test("received real data before stopping", () => client.assert(line.startsWith("data: tick")));\n'
                  '  ' + ending + '\n}, () => client.test("subscriber finished", () => client.assert(true)));\n%}\n')
        if mode != 'timeout':
            source += '\n### after stopping\nGET ' + base + '/api/echo\nX-SSE: {{sseReleased}}\n'
        with open(source_path, 'w') as authored:
            authored.write(source)
        before = len(hits)
        old_policy = dict(m.policy.get('http') or {})
        try:
            m.policy['http'] = dict(old_policy, timeout=5 if mode == 'timeout' else 15)
            result = tool(file=rel, env=TUN)
        finally:
            m.policy['http'] = old_policy
        closed = stream_closures.get(path)
        released = closed is not None and closed.wait(2)
        if mode == 'exit' and not result.get('ok'):
            print('SSE exit detail:', json.dumps(result), flush=True)
        check('SSE ' + mode + ' releases the actual transport without replay or an orphan connection',
              released and open(source_path).read() == source and
              ([item for item, _ in hits[before:]] == [path] and 'did not finish' in result.get('exception', '') if mode == 'timeout' else
               result.get('ok') and [item for item, _ in hits[before:]] == [path, '/api/echo'] and
               hits[-1][1].get('X-SSE') == 'native-sse-released'), result)


def websocket_adapter_check():
    """Confined native transport probes; public WS stays gated until result lifecycle is complete."""
    import httpcerts
    import wsproto
    received, upgraded = [], []

    class WebSocketHandler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_GET(self):
            accept = wsproto.handshake_ok(self.headers)
            if not accept:
                self.send_error(400)
                return
            upgraded.append((self.server.server_port, self.path, dict(self.headers)))
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            self.wfile.flush()
            stream = wsproto.WebSocket(self.connection, self.rfile)
            self.connection.settimeout(5)
            try:
                _, message = stream.recv()
                received.append(message.decode())
                stream.send_text('echo:' + message.decode())
                if self.path == '/output':
                    _, message = stream.recv()
                    received.append(message.decode())
                stream.close(1000)
            except (OSError, wsproto.WSClosed):
                pass

    client_key, client_csr, client_crt = [os.path.join(WORK, 'ws-' + name) for name in ('key.pem', 'client.csr', 'client.pem')]
    for args in (
            ['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', client_key, '-out', client_csr, '-subj', '/CN=piper-ws-fixture'],
            ['x509', '-req', '-in', client_csr, '-CA', crt, '-CAkey', key, '-set_serial', '3', '-out', client_crt, '-days', '1'],
            ['pkcs12', '-export', '-inkey', client_key, '-in', client_crt, '-out', WORK + '/ws-client.p12', '-passout', 'pass:'],
            ['x509', '-in', client_crt, '-outform', 'DER', '-out', WORK + '/ws-client.der'],
            ['pkcs8', '-topk8', '-nocrypt', '-in', client_key, '-outform', 'DER', '-out', WORK + '/ws-key.der']):
        subprocess.run(['openssl', *args], check=True, capture_output=True)
    servers = []
    for tls, identity in ((False, False), (True, False), (True, True)):
        server = ThreadingHTTPServer(('127.0.0.1', 0), WebSocketHandler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(crt, key)
            if identity:
                context.load_verify_locations(crt)
                context.verify_mode = ssl.CERT_REQUIRED
            server.socket = context.wrap_socket(server.socket, server_side=True)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    number = 0
    def run(server, *, tls=False, insecure=False, failure=False, identity=None, output=False):
        nonlocal number
        number += 1
        work = os.path.join(WORK, 'ws-adapter-' + str(number))
        os.mkdir(work)
        if identity:
            httpcerts.snapshot(WORK, '', identity, work)
        source = ('WEBSOCKET %s://127.0.0.1:%d/%s\nAuthorization: Bearer ws-fixture\n\nhello\n\n'
                  '> {%%\nlet seen = 0;\nresponse.body.onEachMessage((message, unsubscribe, send) => {\n'
                  '  seen++; %s\n  client.test("subscriber message", () => client.assert(%s, "ws-late-assertion"));\n'
                  '}, () => client.test("subscriber finished", () => client.assert(seen === 1)));\n%%}\n'
                  % ('wss' if tls else 'ws', server.server_port, 'output' if output else 'echo',
                     'send("callback-ran");' if output else '', 'false' if failure else 'message === "echo:hello"'))
        original = work + '/original.http'
        with open(original, 'x') as authored:
            authored.write(source)
        target = work + '/private.http'
        with open(target, 'x') as private_copy:
            private_copy.write('\n'.join(line for line, _ in hc.instrument_rows(
                [(line, i) for i, line in enumerate(source.split('\n'))], [], force=True)))
        before = len(upgraded)
        rc, console, xml = hc.run_ijhttp(IJHTTP, work, target, '', None, {}, insecure, 20,
                                       targets=[('127.0.0.1', server.server_port)])
        rows = hc.parse_junit(xml)
        return dict(rc=rc, connected=len(upgraded) - before,
                    tests=[test for row in rows for test in row['tests']],
                    unchanged=open(original).read() == source, console=console[-2000:])

    try:
        result = run(servers[0], output=True)
        check('confined WS preserves native subscriber output, late tests and onFinish without replay',
              result['rc'] == 0 and result['connected'] == 1 and result['unchanged'] and
              [t['name'] for t in result['tests']] == ['subscriber message', 'subscriber finished'] and
              all(t['ok'] for t in result['tests']) and received == ['hello', 'callback-ran'], result)
        result = run(servers[0], failure=True)
        check('native late subscriber assertion really fails the native JUnit run',
              result['rc'] == 1 and result['connected'] == 1 and result['unchanged'] and
              any(t['name'] == 'subscriber message' and not t['ok'] and 'ws-late-assertion' in t['message']
                  for t in result['tests']), result)
        result = run(servers[1], tls=True)
        check('confined WSS keeps server verification on by default', result['connected'] == 0, result)
        result = run(servers[1], tls=True, insecure=True)
        check('confined WSS honors only the explicit host TLS override',
              result['rc'] == 0 and result['connected'] == 1 and len(result['tests']) == 2 and
              all(t['ok'] for t in result['tests']), result)
        result = run(servers[2], tls=True, insecure=True)
        check('WSS mTLS cannot silently use a missing identity', result['connected'] == 0, result)
        for label, identity in (
                ('PEM', dict(clientCertificate=client_crt, clientCertificateKey=client_key)),
                ('DER', dict(clientCertificate={'path':WORK + '/ws-client.der', 'format':'DER'},
                             clientCertificateKey={'path':WORK + '/ws-key.der', 'format':'DER'})),
                ('P12', dict(clientCertificate=WORK + '/ws-client.p12'))):
            result = run(servers[2], tls=True, insecure=True, identity=identity)
            check('confined WSS uses the same frozen ' + label + ' client identity',
                  result['rc'] == 0 and result['connected'] == 1 and len(result['tests']) == 2 and
                  all(t['ok'] for t in result['tests']), result)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def client_certificate_check():
    directory = os.path.join(root, 'http-client/certificates')
    os.makedirs(directory, exist_ok=True)
    client_key, client_csr, client_crt = [os.path.join(directory, name) for name in ('client.key', 'client.csr', 'client.pem')]
    commands = [
        ['req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', client_key, '-out', client_csr, '-subj', '/CN=piper-fixture-client'],
        ['x509', '-req', '-in', client_csr, '-CA', crt, '-CAkey', key, '-set_serial', '2', '-out', client_crt, '-days', '1'],
        ['pkcs12', '-export', '-inkey', client_key, '-in', client_crt, '-out', directory + '/client.p12', '-passout', 'pass:'],
        ['x509', '-in', client_crt, '-outform', 'DER', '-out', directory + '/client.der'],
        ['pkcs8', '-topk8', '-nocrypt', '-in', client_key, '-outform', 'DER', '-out', directory + '/key.der'],
        ['pkey', '-in', client_key, '-aes-256-cbc', '-out', directory + '/encrypted.key', '-passout', 'pass:fixture-passphrase-v5'],
        ['pkcs8', '-topk8', '-in', client_key, '-outform', 'DER', '-out', directory + '/encrypted.der', '-passout', 'pass:fixture-passphrase-v5'],
        ['pkcs12', '-export', '-inkey', client_key, '-in', client_crt, '-out', directory + '/encrypted.p12', '-passout', 'pass:fixture-passphrase-v5'],
    ]
    for argv in commands:
        subprocess.run(['openssl', *argv], check=True, capture_output=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), H)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(crt, key)
    context.load_verify_locations(crt)
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = 'test client identity'
    env_path = os.path.join(root, 'http-client')
    rel = 'http-client/certificate.http'
    source = 'GET {{domain}}/api/feed-short\n'
    with open(os.path.join(root, rel), 'w') as output:
        output.write(source)
    original_public, original_private = open(env_path + '/' + hc.ENV_FILE).read(), open(env_path + '/' + hc.PRIVATE_FILE).read()
    public, private = json.loads(original_public), json.loads(original_private)
    public[env] = dict(domain='https://localhost:%d' % server.server_port)
    def configure(configuration):
        private[env] = {hc.SSL_KEY: dict(verifyHostCertificate=False, **configuration)}
        for filename, document in ((hc.ENV_FILE, public), (hc.PRIVATE_FILE, private)):
            with open(env_path + '/' + filename, 'w') as output:
                json.dump(document, output)
    try:
        configure({})
        before = len(hits)
        result = tool(file=rel, env=env)
        check('mTLS server refuses a request without a configured identity', not result.get('ok') and len(hits) == before, result)
        variants = [
            ('PEM', dict(clientCertificate='certificates/client.pem', clientCertificateKey='certificates/client.key')),
            ('DER', dict(clientCertificate={'path':'certificates/client.der', 'format':'DER'},
                         clientCertificateKey={'path':'certificates/key.der', 'format':'DER'})),
            ('P12', dict(clientCertificate='certificates/client.p12')),
        ]
        for label, configuration in variants:
            configure(configuration)
            before = len(hits)
            result = tool(file=rel, env=env)
            check('native configured client certificate authenticates using ' + label, result.get('ok') and len(hits) == before + 1 and
                  open(os.path.join(root, rel)).read() == source, result)
            check('client private key bytes stay out of tool results and approval text: ' + label,
                  open(client_key).read().splitlines()[1] not in json.dumps(result) + json.dumps(cards))
            private[env]['Security'] = {'Auth':{'mtls':{'Type':'OAuth2', 'Grant Type':'Client Credentials',
                'Client ID':'mtls-fixture', 'Client Secret':PW,
                'Token URL':'https://localhost:%d/api/auth-token' % server.server_port}}}
            with open(env_path + '/' + hc.PRIVATE_FILE, 'w') as output:
                json.dump(private, output)
            with open(os.path.join(root, rel), 'w') as output:
                output.write('GET {{domain}}/api/echo\nAuthorization: Bearer {{$auth.token("mtls")}}\n')
            before, before_tokens = len(hits), len(uploads)
            result = tool(file=rel, env=env)
            check('managed token issuer and resource both authenticate using the frozen ' + label + ' identity',
                  result.get('ok') and len(hits) == before + 1 and len(uploads) == before_tokens + 1 and
                  hits[-1][1].get('Authorization') == 'Bearer native-access-v5-42', result)
            with open(os.path.join(root, rel), 'w') as output:
                output.write(source)
        configure(variants[0][1])
        original_key = open(client_key, 'rb').read()
        original_ask = m.ask
        mutated = []
        def mutate_after_card(*args, **kwargs):
            if not mutated:
                with open(client_key, 'wb') as output:
                    output.write(b'synthetic-malformed-private-key-after-approval')
                mutated.append(True)
            return original_ask(*args, **kwargs)
        m.ask = mutate_after_card
        before = len(hits)
        try:
            result = tool(file=rel, env=env)
        finally:
            m.ask = original_ask
            with open(client_key, 'wb') as output:
                output.write(original_key)
        check('native mTLS uses the exact key snapshot taken before its approval',
              result.get('ok') and bool(mutated) and len(hits) == before + 1, result)
        with open(directory + '/invalid.key', 'wb') as output:
            output.write(b'synthetic-malformed-client-key-never-log-this-value')
        configure(dict(variants[0][1], clientCertificateKey='certificates/invalid.key'))
        before = len(hits)
        result = tool(file=rel, env=env)
        check('invalid client material never falls back to another identity or exposes key bytes',
              not result.get('ok') and len(hits) == before and
              'synthetic-malformed-client-key-never-log-this-value' not in json.dumps(result) + json.dumps(cards) and
              '/tls/key' not in json.dumps(result), result)
        configure(variants[0][1])
        m.http_gui_envcfg_save(KEY, rel, env, {'verify_tls':True})
        before = len(hits)
        result = tool(file=rel, env=env)
        check('mTLS retains explicit server verification and rejects the untrusted fixture certificate',
              not result.get('ok') and len(hits) == before, result)
        m.http_gui_envcfg_save(KEY, rel, env, {'verify_tls':False})
        collection = '< {% request.variables.set("id", ["one", "two"]); %}\nGET {{domain}}/api/feed-short?id={{id}}\n'
        with open(os.path.join(root, rel), 'w') as output:
            output.write(collection)
        before = len(hits)
        result = tool(file=rel, env=env)
        check('native collection copies retain their client identity and GUI TLS override',
              result.get('ok') and len(hits) == before + 2 and len(result.get('requests', [])) == 2, result)
        original_secret = m.ask_http_secret
        try:
            encrypted = [dict(variants[0][1], clientCertificateKey='certificates/encrypted.key'),
                         dict(variants[1][1], clientCertificateKey={'path':'certificates/encrypted.der', 'format':'DER'}),
                         dict(clientCertificate='certificates/encrypted.p12')]
            for label, configuration in zip(('PEM', 'DER', 'P12'), encrypted):
                configure(dict(configuration, hasCertificatePassphrase=True))
                for password, outcome in ((None, 'not given'), ('wrong-fixture-passphrase', 'passphrase is wrong'), ('fixture-passphrase-v5', 'accepted')):
                    asked = []
                    def enter(*args, **kwargs):
                        asked.append(args)
                        return password
                    m.ask_http_secret = enter
                    before = len(hits)
                    result = tool(file=rel, env=env)
                    check('encrypted ' + label + ' one-shot passphrase ' + outcome,
                          len(asked) == 1 and ((result.get('ok') and len(hits) == before + 2) if outcome == 'accepted' else
                          (not result.get('ok') and len(hits) == before and outcome in json.dumps(result))) and
                          'fixture-passphrase-v5' not in json.dumps(result), result)
        finally:
            m.ask_http_secret = original_secret
    finally:
        for filename, document in ((hc.ENV_FILE, original_public), (hc.PRIVATE_FILE, original_private)):
            with open(env_path + '/' + filename, 'w') as output:
                output.write(document)
        server.shutdown()
        server.server_close()


def runtime_inputs_check():
    directory = os.path.join(root, 'http-client/runtime-inputs')
    os.makedirs(directory, exist_ok=True)
    files = {'first.bin': b'\x00\xfffirst', 'second.bin': b'\x00\xffsecond',
             'template.json': b'{"value":"{{runtimeValue}}"}', 'swap.bin': b'approved-before-card',
             'hidden.bin': b'synthetic-hidden-runtime-value', 'hardlink.bin': b'hardlink-source'}
    for path, data in files.items():
        with open(os.path.join(directory,path), 'wb') as output:
            output.write(data)
    os.link(os.path.join(directory,'hardlink.bin'), os.path.join(directory,'hardlink-copy.bin'))
    outside = os.path.join(WORK, 'runtime-outside.bin')
    with open(outside,'wb') as output:
        output.write(b'synthetic-outside-runtime-value')
    os.symlink(outside, os.path.join(directory,'link.bin'))
    s.excludes.append('http-client/runtime-inputs/hidden.bin')
    sources = []
    def run(source):
        path = os.path.join(directory,'main.http')
        with open(path,'w') as output:
            output.write(source)
        sources.append(source)
        result = tool(file='http-client/runtime-inputs/main.http',env=TUN)
        check('runtime input adapter preserves authored source #' + str(len(sources)), open(path).read() == source)
        return result
    source = ('< {% request.variables.set("file", "first.bin"); %}\nPOST {{domain}}/runtime-upload\n'
              'Content-Type: application/octet-stream\n\n< {{file}}\n')
    before, before_cards = len(uploads), len(cards)
    result = run(source)
    check('native runtime binary body is approved concretely and uploaded exactly once',
          result.get('ok') and uploads[before:] == [('/runtime-upload', files['first.bin'])] and
          any(kind == 'http_runtime_file' and 'first.bin' in summary and 'sha256' in summary
              for kind,summary in cards[before_cards:]), result)
    result = run('@runtimeValue = native-template\n< {% request.variables.set("file", "template.json"); %}\n'
                 'POST {{domain}}/runtime-template\nContent-Type: application/json\n\n< {{file}}\n')
    check('runtime textual input retains native body templating', result.get('ok') and
          uploads[-1] == ('/runtime-template', b'{"value":"native-template"}'), result)
    before = len(uploads)
    result = run('< {% request.variables.set("id", ["first","second"]); %}\nPOST {{domain}}/runtime-iteration?id={{id}}\n'
                 'Content-Type: application/octet-stream\n\n< {{id}}.bin\n')
    check('runtime input paths use native collection substitution with distinct exact bytes',
          result.get('ok') and uploads[before:] == [('/runtime-iteration?id=first', files['first.bin']),
                                                    ('/runtime-iteration?id=second', files['second.bin'])], result)
    for candidate in ('hidden.bin', 'link.bin', 'hardlink-copy.bin', outside, '../../.git/config'):
        before, before_cards = len(uploads), len(cards)
        result = run('< {% request.variables.set("file", ' + json.dumps(candidate) + '); %}\n'
                     'POST {{domain}}/runtime-refused\nContent-Type: application/octet-stream\n\n< {{file}}\n')
        check('runtime input refuses hidden/link/outside/git data before upload: ' + os.path.basename(candidate),
              not result.get('ok') and len(uploads) == before and
              all(kind != 'http_runtime_file' for kind,_ in cards[before_cards:]) and
              all(value not in json.dumps(result) for value in ('synthetic-hidden-runtime-value','synthetic-outside-runtime-value')), result)
    original_ask = m.ask
    def swap_ask(kind, *args, **kwargs):
        if kind == 'http_runtime_file':
            with open(os.path.join(directory,'swap.bin'),'wb') as output:
                output.write(b'changed-while-card-was-open')
        return original_ask(kind,*args,**kwargs)
    m.ask = swap_ask
    try:
        result = run(source.replace('first.bin','swap.bin'))
    finally:
        m.ask = original_ask
    check('runtime file approval binds the snapshot instead of reopening a changed source',
          result.get('ok') and uploads[-1] == ('/runtime-upload', files['swap.bin']), result)
    m.ask = lambda kind,*args,**kwargs: False if kind == 'http_runtime_file' else original_ask(kind,*args,**kwargs)
    before_hits, before_uploads = len(hits), len(uploads)
    try:
        result = run('### Earlier\nGET {{domain}}/api/feed-short\n\n### Runtime\n' + source + '\n### Later\nGET {{domain}}/api/feed-short?must-not-run\n')
    finally:
        m.ask = original_ask
    check('declined runtime input never uploads or replays an earlier request', not result.get('ok') and
          len(hits) == before_hits + 1 and len(uploads) == before_uploads and
          'not retried' in json.dumps(result), result)
    before_hits, before_uploads, before_cards = len(hits), len(uploads), len(cards)
    result = run('### Earlier\nGET {{domain}}/api/feed-short\n\n### Missing\n' +
                 source.replace('request.variables.set("file", "first.bin")', 'request.variables.set("irrelevant", true)') +
                 '\n### Later\nGET {{domain}}/api/feed-short?must-not-run\n')
    check('unresolved native runtime path stops later requests before any file approval or upload',
          not result.get('ok') and len(hits) == before_hits + 1 and len(uploads) == before_uploads and
          all(kind != 'http_runtime_file' for kind,_ in cards[before_cards:]) and
          len(result.get('requests', [])) == 3 and result['requests'][-1].get('status') is None, result)
    generated = ('< {% request.variables.set("destination", "generated.bin"); %}\n'
                 'GET {{domain}}/api/iteration?generated=first\n\n>>! {{destination}}\n\n'
                 '### Consume\nPOST {{domain}}/runtime-generated\nContent-Type: application/octet-stream\n\n< generated.bin\n')
    with open(os.path.join(directory,'generated.bin'),'wb') as output:
        output.write(b'old-file-must-not-win')
    before_hits, before_uploads = len(hits), len(uploads)
    result = run(generated)
    check('dynamic response output feeds a later literal body from its exact native stage', result.get('ok') and
          len(hits) == before_hits + 1 and uploads[before_uploads:] == [('/runtime-generated', b'\x00\xff/api/iteration?generated=first')] and
          open(os.path.join(directory,'generated.bin'),'rb').read() == b'\x00\xff/api/iteration?generated=first', result)
    before_uploads = len(uploads)
    result = run(generated.replace('>>! {{destination}}','>> {{destination}}').replace('< generated.bin','< generated-1.bin'))
    check('dynamic non-overwrite output keeps its original file and feeds the native suffix', result.get('ok') and
          uploads[before_uploads:] == [('/runtime-generated', b'\x00\xff/api/iteration?generated=first')] and
          result['requests'][0].get('saved','').endswith('/generated-1.bin'), result)
    collection = ('< {% request.variables.set("id", ["first","second"]); '
                  'request.variables.set("destination", "generated-" + request.iteration() + ".bin"); %}\n'
                  'GET {{domain}}/api/iteration?generated={{id}}\n\n>>! {{destination}}\n\n'
                  '### Consume first\nPOST {{domain}}/runtime-generated-first\nContent-Type: application/octet-stream\n\n< generated-0.bin\n\n'
                  '### Consume second\nPOST {{domain}}/runtime-generated-second\nContent-Type: application/octet-stream\n\n< generated-1.bin\n')
    before_uploads = len(uploads)
    result = run(collection)
    check('runtime body resolver distinguishes every preceding generated collection stage', result.get('ok') and
          uploads[before_uploads:] == [('/runtime-generated-first',b'\x00\xff/api/iteration?generated=first'),
                                      ('/runtime-generated-second',b'\x00\xff/api/iteration?generated=second')], result)
    deferred = ('GET {{domain}}/api/iteration?deferred=response\n\n'
                '> {% client.global.set("deferredResponsePathV4", "before-callback.bin"); '
                'client.test("deferred callback path", () => client.global.set("deferredResponsePathV4", "deferred-response.bin")); %}\n'
                '>>! {{deferredResponsePathV4}}\n\n### Consume deferred output\n'
                'POST {{domain}}/runtime-deferred\nContent-Type: application/octet-stream\n\n< deferred-response.bin\n')
    before_uploads = len(uploads)
    result = run(deferred)
    check('response-created filename feeds later body without source changes or a repeated request', result.get('ok') and
          uploads[before_uploads:] == [('/runtime-deferred',b'\x00\xff/api/iteration?deferred=response')] and
          open(os.path.join(directory,'deferred-response.bin'),'rb').read() == b'\x00\xff/api/iteration?deferred=response', result)
    cleared = ('< {% client.global.clearAll(); %}\nGET {{domain}}/api/iteration?deferred=cleared\n\n'
               '>>! {{deferredResponsePathV4}}\n\n### Consume must stop\n'
               'POST {{domain}}/runtime-cleared\nContent-Type: application/octet-stream\n\n< deferred-response.bin\n')
    before_hits, before_uploads = len(hits), len(uploads)
    result = run(cleared)
    check('cleared runtime export names never reuse a stale global or a published input', not result.get('ok') and
          len(hits) == before_hits + 1 and len(uploads) == before_uploads and
          open(os.path.join(directory,'deferred-response.bin'),'rb').read() == b'\x00\xff/api/iteration?deferred=response', result)
    declined = generated.replace('generated.bin','generated-denied.bin')
    before_hits, before_uploads = len(hits), len(uploads)
    m.ask = lambda kind,*args,**kwargs: False if kind == 'http_runtime_file' else original_ask(kind,*args,**kwargs)
    try:
        result = run(declined)
    finally:
        m.ask = original_ask
    check('declining a generated body sends no later upload and publishes no incomplete export', not result.get('ok') and
          len(hits) == before_hits + 1 and len(uploads) == before_uploads and
          not os.path.exists(os.path.join(directory,'generated-denied.bin')), result)


def run_graph_check():
    directory = os.path.join(root, 'http-client/graph')
    files = {
        'main.http': 'import ./defs.http\n### Named import\nrun #named (@id=overridden)\n\n'
                     '### Nested file\nrun ./nested/flow.http\n',
        'defs.http': '@id = original\n### Unselected\nGET https://unapproved.invalid/never\n\n'
                     '### Definition\n# @name named\nGET {{domain}}/api/feed-short?id={{id}}\n\n'
                     '> {% client.test("native override", () => client.assert(request.url().endsWith("id=overridden"))); '
                     'client.global.set("graphValue", 42); %}\n>>! from-definition.xml\n',
        'nested/flow.http': 'run ./request.http\n',
        'nested/request.http': '### Nested request\nPOST {{domain}}/api/graph-upload\nContent-Type: application/json\n\n'
                               '< ./payload.json\n\n> ./handler.js\n',
        'nested/payload.json': '{"graph":true}',
        'nested/handler.js': 'import {check} from "./helper.js"; check(); client.log("nested graph log");',
        'nested/helper.js': 'export function check(){client.test("native nested module", () => '
                            'client.assert(response.status === 200 && client.global.get("graphValue") === 42));}'}
    for path, content in files.items():
        full = os.path.join(directory, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as output:
            output.write(content)
    before = len(hits), len(uploads)
    result = tool(file='http-client/graph/main.http', env=TUN)
    rows = result.get('requests', [])
    check('native run/import keeps named overrides nested files modules and shared execution globals', result.get('ok') and
          len(rows) == 2 and [t['name'] for row in rows for t in row['tests'] if t['name'] != 'Response save'] ==
          ['native override', 'native nested module'] and 'nested graph log' in rows[1].get('script_log', ''), result)
    exported = os.path.join(directory, 'from-definition.xml')
    check('imported export resolves relative to its source and preserves exact bytes once',
          os.path.isfile(exported) and open(exported, 'rb').read() == b'<short/>\r\n' and
          [path for path, _ in hits[before[0]:]] == ['/api/feed-short?id=overridden'] and
          uploads[before[1]:] == [('/api/graph-upload', b'{"graph":true}')], result)
    check('HTTP graph leaves every original HTTP module and input unchanged',
          all(open(os.path.join(directory, path)).read() == content for path, content in files.items()), result)
    repeated = ('@id = first\n### Local\n# @name local\nGET {{domain}}/api/feed-short?id={{id}}\n\n'
                '> {% client.test("occurrence " + request.url().split("=")[1], () => client.assert(response.status === 200)); %}\n\n'
                '### Again\nrun #local (@id=second)\nrun #local (@id=third)\n')
    with open(os.path.join(directory, 'repeat.http'), 'w') as output:
        output.write(repeated)
    before = len(hits)
    result = tool(file='http-client/graph/repeat.http', env=TUN)
    rows = result.get('requests', [])
    check('native local named runs repeat safely with distinct overrides and identities', result.get('ok') and
          [t['name'] for row in rows for t in row['tests']] == ['occurrence first', 'occurrence second', 'occurrence third'] and
          [row.get('root_index') for row in rows] == [0, 1, 2] and
          [path for path, _ in hits[before:]] == ['/api/feed-short?id=first', '/api/feed-short?id=second', '/api/feed-short?id=third'] and
          open(os.path.join(directory, 'repeat.http')).read() == repeated, result)


def native_save_errors_check():
    rel = 'http-client/save-errors.http'
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/not-found\n\n> {% client.test("native expected 404",()=>client.assert(response.status===404)); %}\n')
    before = len(hits)
    result = tool(file=rel, env=TUN, index=0, save_to='not-found.pdf')
    check('native save_to keeps real status tests but never publishes an HTTP error as the requested file',
          not result.get('ok') and '404' in result['requests'][0]['error'] and
          any(t['name']=='native expected 404' and t['ok'] for t in result['requests'][0]['tests']) and
          [path for path, _ in hits[before:]] == ['/api/not-found'] and
          not os.path.exists(os.path.join(root,'development/not-found.pdf')), result)
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/binary-overflow\n\n>>! xml-feed-output/overflow.pdf\n')
    before = len(hits)
    old_policy = dict(m.policy.get('http') or {})
    try:
        m.policy['http'] = dict(old_policy, timeout=15)
        result = tool(file=rel, env=TUN)
    finally:
        m.policy['http'] = old_policy
    check('native binary size bound fails without exporting or replaying the request', not result.get('ok') and
          '64 MiB' in json.dumps(result) and
          [path for path, _ in hits[before:]] == ['/api/binary-overflow'] and
          not os.path.exists(os.path.join(root,'http-client/xml-feed-output/overflow.pdf')), result)


def runtime_guard_check():
    """Real OS probes: targets are synthetic files/servers, never host credentials."""
    rel = 'http-client/runtime-guard.http'
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/feed-short\n')
    native = hc.run_ijhttp
    last_native = []
    def capture_native(*args, **kwargs):
        result = native(*args, **kwargs)
        last_native[:] = [result]
        return result
    hc.run_ijhttp = capture_native
    try:
        baseline = tool(file=rel, env=TUN)
    finally:
        hc.run_ijhttp = native
    if not baseline.get('ok') and last_native:
        print('NATIVE BASELINE FAILURE', last_native[0][0], hc.redactor([PW])('\n'.join(line for line in last_native[0][1].splitlines() if not line.startswith('\tat '))[-4000:]), flush=True)
    check('native isolated runner baseline', baseline.get('ok'), baseline.get('output', '')[-1800:])
    if not baseline.get('ok'):
        return
    outside = os.path.join(WORK, 'outside-guard.js')
    hidden = os.path.join(root, 'http-client/http-secret.js')
    for path in (outside, hidden):
        with open(path, 'w') as output:
            output.write('export const value = 42;')
    s.excludes.append('http-client/http-secret.js')
    spec = json.dumps(outside)
    invocation = json.dumps('import(' + spec + ')')
    cases = {
        'eval outside import': 'await eval(' + invocation + ');',
        'indirect eval outside import': 'await globalThis["ev"+"al"](' + invocation + ');',
        'Function outside import': 'await Function(' + json.dumps('return import(' + spec + ')') + ')();',
        'function constructor outside import': 'await (function(){}).constructor(' + json.dumps('return import(' + spec + ')') + ')();',
        'hidden original file': 'await eval(' + json.dumps('import(' + json.dumps(hidden) + ')') + ');',
        'load outside file': 'load(' + spec + ');',
        'loadWithNewGlobal outside file': 'loadWithNewGlobal(' + spec + ');',
        'host process printf': 'execSync("/usr/bin/printf piper-probe");',
        'host shell': 'execSync("/bin/sh -c true");',
        'child JVM': 'execSync(' + json.dumps(os.path.join(os.environ['JAVA_HOME'], 'bin/java') + ' -version') + ');',
    }
    rel = 'http-client/runtime-guard.http'
    for label, code in cases.items():
        with open(os.path.join(root, rel), 'w') as output:
            output.write('< {% ' + code + ' %}\nGET {{domain}}/api/feed-short\n')
        before = len(hits)
        last_native.clear()
        hc.run_ijhttp = capture_native
        try:
            record = tool(file=rel, env=TUN)
        finally:
            hc.run_ijhttp = native
        # Runtime module denial deliberately halts this dedicated JVM before
        # native exception/report writers. Assert its actual refusal exit code,
        # rather than requiring the word "Exception" in a scrubbed result.
        refused_code = bool(last_native) and last_native[0][0] == 90
        check('native OS isolation blocks ' + label, record.get('ok') is False and len(hits) == before and
              (refused_code if 'import' in label or label == 'hidden original file' else
               'Exception' in json.dumps(record) or 'Error' in json.dumps(record)), record)
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/feed-short\n\n> {% client.test("native dynamic JS", async () => { '
                     'client.assert(eval("21 * 2") === 42); '
                     'client.assert(Function("return 42")() === 42); '
                     'client.assert((function(){}).constructor("return 42")() === 42); '
                     'client.assert(await Object.getPrototypeOf(async function(){}).constructor("return 42")() === 42); '
                     'client.assert(Object.getPrototypeOf(function*(){}).constructor("yield 42")().next().value === 42); '
                     '}); %}\n')
    before = len(hits)
    record = tool(file=rel, env=TUN)
    if not record.get('ok'):
        print('NATIVE JS FAILURE', record.get('output', '')[-1800:], flush=True)
    check('native eval and constructor semantics restored inside OS isolation', record.get('ok') and len(hits) == before + 1,
          record)
    with open(os.path.join(root, rel), 'w') as output:
        output.write('< {% const regex = /[[]/; const divided = {} / 2; '
                     'request.variables.set("nativeRegex", regex.test("[")); %}\nGET {{domain}}/api/feed-short\n\n'
                     '> {% client.test("native grammar", () => client.assert(request.variables.get("nativeRegex") === true)); %}\n')
    before = len(hits)
    record = tool(file=rel, env=TUN)
    check('JS without module dependencies retains native regex and division grammar', record.get('ok') and len(hits) == before + 1, record)
    # A script mutates a previously approved destination to a different port.
    # Both loopback servers are synthetic; only the original HTTPS port is granted.
    with open(os.path.join(root, rel), 'w') as output:
        output.write('< {% request.variables.set("domain", ' + json.dumps('http://127.0.0.1:%d' % plain_srv.server_port) +
                     '); %}\nGET {{domain}}/api/feed-short\n')
    before = len(hits)
    record = tool(file=rel, env=TUN)
    check('runtime destination mutation cannot bypass exact egress grant', record.get('ok') is False and len(hits) == before,
          record)
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/console-overflow\n')
    before = len(hits)
    record = tool(file=rel, env=TUN)
    check('oversized native console is bounded and never triggers a replay',
          'output exceeds 32 MiB' in record.get('exception', '') and 'may already have run' in record.get('exception', '') and
          len(hits) == before + 1, record)
    # JVM injection and piperd env are not inherited by the native process.
    with open(os.path.join(root, rel), 'w') as output:
        output.write('GET {{domain}}/api/feed-short\n')
    os.environ['IJHTTP_JAVA_OPTS'] = '-javaagent:/nonexistent/piper-synthetic.jar'
    try:
        record = tool(file=rel, env=TUN)
        check('host JVM injection options are not inherited', record.get('ok'), record)
    finally:
        os.environ.pop('IJHTTP_JAVA_OPTS', None)


F = "http-client/vn.http"
try:
    if '--finite-sse-only' in sys.argv:
        finite_sse_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--websocket-adapter-only' in sys.argv:
        websocket_adapter_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--cookies-only' in sys.argv:
        persistent_cookie_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--client-certificates-only' in sys.argv:
        client_certificate_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--runtime-inputs-only' in sys.argv:
        runtime_inputs_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--completion-v3-only' in sys.argv:
        completion_v3_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--run-graph-only' in sys.argv:
        run_graph_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--runtime-metadata-only' in sys.argv:
        runtime_metadata_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--runtime-code-only' in sys.argv:
        runtime_code_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--browser-auth-only' in sys.argv:
        browser_auth_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--managed-auth-only' in sys.argv:
        managed_auth_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--runtime-code-errors-only' in sys.argv:
        runtime_code_errors_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if '--save-errors-only' in sys.argv:
        native_save_errors_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if "--profile-symlink-only" in sys.argv:
        target = os.environ.get('PIPER_E2E_CAP_TARGET', '')
        if sys.platform != 'darwin' or not target.startswith('/private/tmp/http-cap-') or not os.path.isdir(target):
            raise RuntimeError('this macOS probe requires a precreated synthetic /private/tmp/http-cap-* directory')
        native_work = os.path.join(WORK, 'native-cap')
        os.mkdir(native_work)
        os.symlink(target, os.path.join(native_work, 'staged'))
        source = os.path.join(native_work, 'probe.http')
        with open(source, 'w') as output:
            output.write('GET http://localhost:' + str(plain_srv.server_port) + '/api/feed-short\n\n>>! ' +
                         os.path.join(native_work, 'staged/response-0') + '\n')
        before = len(hits)
        rc, console, xml = hc.run_ijhttp(IJHTTP, native_work, source, '', None, {}, False, 30,
                                        targets=[('localhost', plain_srv.server_port)])
        check('test launcher cannot grant writes through an outside stage symlink',
              rc != 0 and len(hits) == before + 1 and not os.path.lexists(os.path.join(target, 'response-0')),
              {'rc': rc, 'hits': len(hits) - before, 'console': console[-1000:]})
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if "--export-only" in sys.argv:
        with open(os.path.join(root, 'http-client/export-probe.http'), 'w') as f:
            f.write('GET {{domain}}/api/feed\n\n>>! feed-probe.xml\n')
        before = len(hits)
        record = tool(file='http-client/export-probe.http', env=TUN)
        path = os.path.join(root, 'http-client/feed-probe.xml')
        check('native private stage preserves large XML bytes once', record.get('ok') and len(hits) == before + 1 and
              os.path.isfile(path) and open(path, 'rb').read() == FEED, record)
        if not record.get('ok') and record.get('run'):
            print(m.http_run_record(record['run']).get('output', '')[-6000:], flush=True)
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if "--guard-only" in sys.argv:
        runtime_guard_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if "--network-only" in sys.argv:
        transport_host_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    if "--timeouts-only" in sys.argv:
        transport_timeout_check()
        raise SystemExit(0 if all(ok for _, ok in results) else 1)
    # 1. http_envs shows the tunnel derived from the name
    envs = m.http_gui_envs(KEY, F)
    check("http_envs (GUI) lists the ssh tunnel", "projects@test-k.example -> 192.0.2.1:443" in envs.get("tunnels", {}).get(TUN, ""), envs.get("tunnels"))
    check("http_envs: no SSLConfiguration variable", "SSLConfiguration" not in json.dumps(envs.get("variables")), envs.get("variables"))

    # 2. a real-world flow: whole file (login, PDF, ...) through Piper's tunnel with the real ijhttp
    r = tool(file=F, env=TUN, reason="e2e")
    q = r.get("requests") or []
    check("whole file: native login tests survive a later binary response", q and q[0]["status"] == 200 and
          q[0]["tests"] and q[0]["tests"][0]["name"] == "logged in" and q[0]['tests'][0]['ok'], q[:1])
    check("whole file: PDF 200 through Piper's port, body replaced by a hint",
          len(q) > 1 and q[1]["status"] == 200 and "save_to" in q[1].get("body", ""), q[1:2])
    check("whole file: continues after binary and reports refused redirect honestly", len(q) == 4 and
          q[2]['status'] == 200 and not q[3]['ok'] and q[3]['error'] and
          all('not run - the ijhttp CLI stops' not in x['error'] for x in q[2:]), q[2:])
    check("card shows the tunnel", any("Through Piper's tunnel" in c[1] for c in cards), [c[1][:300] for c in cards])
    with open(ssh_log) as f:
        argv = f.read().split("\n")[0]
    check("ssh started with the declared server/forward", "-l projects" in argv and ":192.0.2.1:443 -- test-k.example" in argv, argv)

    # 3. save_to before the login of this env: names the request that sets the global
    common.write_json(hc.GLOBALS_FILE, {})
    r = tool(file=F, env=TUN, index=1, save_to="posudek.pdf")
    check("save_to without the global: names the Login request", "set by request #0 'Login'" in
          (r.get('exception', '') + (r.get("requests") or [{}])[0].get("error", "")), r)
    # 4. index 0 stores the global, then the PDF saved whole
    r = tool(file=F, env=TUN, index=0)
    check("login alone: ok, tests evaluated", r.get("ok") is True and r["requests"][0]["tests"][0]["name"] == "logged in", r)
    r = tool(file=F, env=TUN, index=1, save_to="posudek.pdf")
    p = os.path.join(root, "development", "posudek.pdf")
    check("save_to wrote the exact PDF bytes (global carried)", os.path.exists(p) and open(p, "rb").read() == PDF, r)
    check("save_to result names the file and size", (r.get("requests") or [{}])[0].get("saved") == "development/posudek.pdf"
          and r["requests"][0].get("saved_bytes") == len(PDF), r)
    check("agent record says it went through the tunnel", r.get("tunnel") == "through Piper's tunnel", r)
    r = tool(file=F, env=TUN, index=1)
    check("single binary request without tests: ok", r.get("ok") is True and "binary application/pdf, 204815 bytes" in r["requests"][0]["body"], r)
    r = tool(file=F, env=TUN, index=2)
    q = r.get("requests") or []
    check("echo 200, password masked", q and q[0]["status"] == 200 and PW not in json.dumps(r), r)
    host_hdr = [{k.lower(): v for k, v in h.items()}.get("host", "") for p_, h in hits if p_.startswith("/api/echo")]
    check("request reached the server via the tunnel (Host = Piper's port)", host_hdr and host_hdr[-1].startswith("localhost:43"), host_hdr)
    with open(ssh_log) as f:
        check("tunnel reused (one ssh process)", len([x for x in f.read().split("\n") if x]) == 1)

    # 5. save_to of a text response holding the secret: saved masked; a redirect: not followed
    r = tool(file=F, env=TUN, index=2, save_to="echo.json")
    ep = os.path.join(root, "development", "echo.json")
    txt = open(ep).read() if os.path.exists(ep) else ""
    check("save_to text: secret masked in the file", txt and PW not in txt and hc.MASK in txt, (txt, r))
    r = tool(file=F, env=TUN, index=3, save_to="redir.html")
    check("save_to redirect: 302 shown, not followed, nothing saved",
          "302" in (r.get("requests") or [{}])[0].get("error", "") and not os.path.exists(os.path.join(root, "development", "redir.html")), r)
    envs = m.tool(s, "http_envs", {"file": F})
    check("http_envs (session): tunnel without details", envs["tunnels"].get(TUN) == "Piper opens a tunnel for this environment", envs.get("tunnels"))

    # 6. PKIX with the real ijhttp: hint
    r = tool(file=F, env="test direct", index=2)
    err = (r.get("requests") or [{}])[0].get("error", "") or r.get("output", "") or r.get("exception", "")
    check("test direct: PKIX error carries the SSLConfiguration hint", "verifyHostCertificate" in err, r)

    # 7. SSLConfiguration in the hidden env file -> --insecure -> passes
    r = tool(file=F, env="test ssl off", index=2)
    check("SSLConfiguration false: the real ijhttp passes", (r.get("requests") or [{}])[0].get("status") == 200, r)

    # 8. connection refused on localhost: tunnel hint
    r = tool(file=F, env="test refused", index=2)
    err = (r.get("requests") or [{}])[0].get("error", "") or r.get("output", "") or r.get("exception", "")
    check("refused on localhost: hint to declare the tunnel", '"piper"' in err, r)

    # 9. save_to on a tracked file refused (also another case)
    r = tool(file=F, env=TUN, index=0, save_to="./readme.MD")
    check("save_to over a tracked file (other case) refused", "tracked by git" in r.get("exception", ""), r)

    # 10. GUI run through the tunnel and the stored record
    rec = m.http_gui_run(KEY, F, TUN, 2)
    check("GUI run through the tunnel, label stored", rec["requests"][0]["status"] == 200 and
          "test-k.example" in m.http_run_record(rec["id"]).get("tunnel", ""), rec)

    # 11. the agent's last result is the scrubbed one
    lr = m.tool(s, "http_last_result", {"file": F})
    check("http_last_result scrubbed", PW not in json.dumps(lr), lr)

    # 12. Real ijhttp ignores no-cookie-jar below a pre-request script. Piper honours the explicit
    # directive in its temporary copy; the project file stays unchanged (inline and external scripts).
    cookie_rel = "http-client/cookies.http"
    with open(os.path.join(root, "http-client/cookie-pre.js"), "w") as f:
        f.write('request.variables.set("probe", "synthetic");\n')
    for label, pre, directive in (("inline", '< {% request.variables.set("probe", "synthetic"); %}\n', '# @no-cookie-jar'),
                                  ("external", "< ./cookie-pre.js\n", '//   @no-cookie-jar')):
        source = ("### Set isolated cookie\n" + pre + directive + "\nGET {{domain}}/api/cookie/set\n\n"
                  '> {% client.test("Pre-request Handler", () => client.assert(response.status === 200)); %}\n\n'
                  "### Observe jar\nGET {{domain}}/api/cookie/echo\n\n"
                  '> {% client.test("empty jar", () => client.assert(response.body.cookie === "")); %}\n')
        with open(os.path.join(root, cookie_rel), "w") as f:
            f.write(source)
        r = tool(file=cookie_rel, env=TUN)
        q = r.get("requests", [])
        check("cookie directive after %s script: no cookie stored" % label,
              r.get("ok") is True and len(q) == 2 and q[1]["status"] == 200 and
              any(t["name"] == "empty jar" and t["ok"] for t in q[1]["tests"]),
              [(row.get("name"), row.get("body"), row.get("tests")) for row in q])
        check("%s script: user test named Pre-request Handler stays on its response" % label,
              len(q) == 2 and any(t["name"] == "Pre-request Handler" and t["ok"] for t in q[0]["tests"]),
              [row.get("tests") for row in q])
        with open(os.path.join(root, cookie_rel)) as f:
            check("cookie normalization keeps the %s source unchanged" % label, f.read() == source)

    # An absent directive still stores cookies. A later directive only suppresses saving: it must
    # preserve both the jar already present and the explicit Cookie header, even after a script.
    source = ('### Store cookie normally\nGET {{domain}}/api/cookie/set\n\n'
              '### Explicit cookie with existing jar\n< {% request.variables.set("probe", "synthetic"); %}\n'
              '# @no-cookie-jar\nGET {{domain}}/api/cookie/echo\nCookie: manual=synthetic\n\n'
              '> {% client.test("both cookies", () => { client.assert(response.body.cookie.includes("manual=synthetic"));'
              ' client.assert(response.body.cookie.includes("probe=synthetic")); }); %}\n')
    with open(os.path.join(root, cookie_rel), "w") as f:
        f.write(source)
    r = tool(file=cookie_rel, env=TUN)
    q = r.get("requests", [])
    check("cookie normalization preserves existing jar and explicit cookies", r.get("ok") is True and len(q) == 2 and
          any(t["name"] == "both cookies" and t["ok"] for t in q[1]["tests"]), r)

    # Redirects run natively in one ijhttp invocation; only their output location is staged.
    # Keep the source, request variables, handlers and login state exactly as authored.
    export_rel = "http-client/export.http"
    source = ('### Login\nGET {{domain}}/api/login\n\n'
              '> {% client.global.set("sessionId", response.body.sessionId); %}\n\n'
              '### Feed\n< {% request.variables.set("part", "feed"); %}\n'
              'GET {{domain}}/api/{{part}}\nX-Session-Id: {{sessionId}}\n\n'
              '> {% client.test("feed ok", () => client.assert(response.status === 200));'
              ' client.global.set("exportName", "products"); %}\n'
              '>> xml-feed-output/cs/{{exportName}}.xml\n\n'
              '### After export\nGET {{domain}}/api/echo\nX-Session-Id: {{sessionId}}\n')
    with open(os.path.join(root, export_rel), "w") as f:
        f.write(source)
    before = len(hits)
    r = tool(file=export_rel, env=TUN)
    q = r.get("requests", [])
    output = os.path.join(root, "http-client/xml-feed-output/cs/products.xml")
    if not r.get('ok'):
        print('EXPORT DIAGNOSTIC', m.http_run_record(r['run']).get('output', '')[-6000:], flush=True)
    check("native XML redirect: all requests and handler pass", r.get("ok") is True and len(q) == 3 and
          any(t["name"] == "feed ok" and t["ok"] for t in q[1]["tests"]), r)
    check("native XML redirect: large response saved byte for byte", os.path.isfile(output) and
          open(output, "rb").read() == FEED, [(x.get("saved"), x.get("error")) for x in q])
    check("native XML redirect: exactly one request each in order", [p for p, _ in hits[before:]] ==
          ["/api/login", "/api/feed", "/api/echo"], hits[before:])
    with open(os.path.join(root, export_rel)) as f:
        check("native XML redirect: source unchanged", f.read() == source)
    r = tool(file=export_rel, env=TUN)
    suffix = output.replace(".xml", "-1.xml")
    check("native XML redirect: soft redirect creates -1", r.get("ok") is True and os.path.isfile(suffix) and
          open(suffix, "rb").read() == FEED, r)
    source = source.replace('>> xml-feed-output/cs/{{exportName}}.xml',
                            '>>! {{$projectRoot}}/http-client/xml-feed-output/cs/products.xml')
    with open(os.path.join(root, export_rel), "w") as f:
        f.write(source)
    with open(output, "wb") as f:
        f.write(b"replace me")
    r = tool(file=export_rel, env=TUN)
    check("native XML redirect: force replaces projectRoot target", r.get("ok") is True and
          open(output, "rb").read() == FEED, r)
    source = ('### Export\nGET {{domain}}/api/feed\n\n>>! xml-feed-output/flow.xml\n\n'
              '### Upload first\nPOST {{domain}}/upload\nContent-Type: application/xml\n\n< ./xml-feed-output/flow.xml\n\n'
              '### Replace\nGET {{domain}}/api/feed-short\n\n>>! xml-feed-output/flow.xml\n\n'
              '### Upload second\nPOST {{domain}}/upload\nContent-Type: application/xml\n\n< ./xml-feed-output/flow.xml\n')
    with open(os.path.join(root, export_rel), "w") as f:
        f.write(source)
    r = tool(file=export_rel, env=TUN)
    check("native redirects: later file bodies use the right response in order", r.get("ok") is True and
          uploads == [("/upload", FEED), ("/upload", b"<short/>\r\n")],
          {"ok": r.get("ok"), "uploads": [(p, len(b)) for p, b in uploads],
           "errors": [q.get("error") for q in r.get("requests", [])]})
    external = 'client.test("external handler", () => client.assert(response.status === 200));\nclient.global.set("externalName", "external");\n'
    with open(os.path.join(root, "http-client/export-handler.js"), "w") as f:
        f.write(external)
    source = ('### External handler\nGET {{domain}}/api/feed-short\n\n> ./export-handler.js\n'
              '>> xml-feed-output/{{externalName}}.xml\n')
    with open(os.path.join(root, export_rel), "w") as f:
        f.write(source)
    r = tool(file=export_rel, env=TUN)
    external_output = os.path.join(root, "http-client/xml-feed-output/external.xml")
    check("native redirect: path from external handler works", r.get("ok") is True and
          os.path.isfile(external_output) and open(external_output, "rb").read() == b"<short/>\r\n" and
          any(t["name"] == "external handler" and t["ok"] for t in r["requests"][0]["tests"]), r)
    with open(os.path.join(root, "http-client/export-handler.js")) as f:
        check("native redirect: external handler source unchanged", f.read() == external)

    # Native module resolution uses the private project mirror, including nested and cyclic
    # dependencies; neither the authored sources nor their relative specifiers are rewritten.
    module_dir = os.path.join(root, 'http-client/modules/nested')
    os.makedirs(module_dir, exist_ok=True)
    module_sources = {
        'http-client/modules/handler.js': 'import {check} from "./nested/check.js"; check();',
        'http-client/modules/nested/check.js': 'import {value} from "../value.js"; '
            'export function check() { client.test("nested module", () => client.assert(value === 42)); }',
        'http-client/modules/value.js': 'import "./nested/check.js"; export const value = 42;',
        'http-client/modules.http': '### External module\nGET {{domain}}/api/login\n\n> ./modules/handler.js\n\n'
            '### Inline module\nGET {{domain}}/api/echo\n\n'
            '> {% import {value} from "./modules/value.js"; '
            'client.test("inline module", () => client.assert(value === 42)); %}\n'}
    for path, content in module_sources.items():
        with open(os.path.join(root, path), 'w') as f:
            f.write(content)
    before = len(hits)
    r = tool(file='http-client/modules.http', env=TUN)
    q = r.get('requests', [])
    check('native JS modules: external, inline, nested and cyclic dependencies',
          r.get('ok') is True and len(q) == 2 and
          any(t['name'] == 'nested module' and t['ok'] for t in q[0]['tests']) and
          any(t['name'] == 'inline module' and t['ok'] for t in q[1]['tests']), r)
    check('native JS modules: each request sent once and sources unchanged',
          [p for p, _ in hits[before:]] == ['/api/login', '/api/echo'] and
          all(open(os.path.join(root, p)).read() == s for p, s in module_sources.items()), hits[before:])
    with open(os.path.join(root, 'http-client/modules.http'), 'w') as f:
        f.write(module_sources['http-client/modules.http'].replace('### External module\n',
                                                                '### External module\n# @no-log\n'))
    before = len(hits)
    r = tool(file='http-client/modules.http', env=TUN)
    check('native no-log: immediate tests returned, no batch history persisted',
          r.get('ok') is True and len(hits[before:]) == 2 and
          not os.path.exists(os.path.join(hc.RUNS_DIR, r['run'] + '.json')), r)
    for label, bad_source in (
            ('hidden', 'import "../http-client.private.env.json";'),
            ('outside project', 'import "../../../outside.js";'),
            ('missing', 'import "./does-not-exist.js";')):
        with open(os.path.join(root, 'http-client/modules/handler.js'), 'w') as f:
            f.write(bad_source)
        before, before_cards = len(hits), len(cards)
        r = tool(file='http-client/modules.http', env=TUN)
        check('native JS module preflight refuses ' + label + ' before any request/card',
              'exception' in r and len(hits) == before and len(cards) == before_cards, r)
    with open(os.path.join(root, 'http-client/modules/handler.js'), 'w') as f:
        f.write('// {{$processEnv HOME}}\n')
    with open(os.path.join(root, 'http-client/modules.http'), 'w') as f:
        f.write('### Script\nGET {{domain}}/api/login\n\n> ./modules/handler.js\n\n'
                '### Body\nPOST {{domain}}/upload\n\n< ./modules/handler.js\n')
    before, before_cards = len(hits), len(cards)
    r = tool(file='http-client/modules.http', env=TUN)
    check('module reused as a body cannot template the host environment before any request/card',
          'processEnv' in r.get('exception', '') and len(hits) == before and len(cards) == before_cards, r)

    completion_v3_check()
    runtime_inputs_check()
    client_certificate_check()

    # Compatibility fixes use the real engine and actual received bytes, never a re-fetch.
    tmp = os.path.join(WORK, 'java-tmp')
    os.makedirs(tmp)
    os.environ['IJHTTP_JAVA_OPTS'] = '-Djava.io.tmpdir=' + tmp
    compat_rel = 'http-client/compat.http'
    def compat(source, index=None):
        with open(os.path.join(root, compat_rel), 'w') as f:
            f.write(source)
        return tool(file=compat_rel, env=TUN, index=index)

    with open(os.path.join(root, 'http-client/template.json'), 'w') as f:
        f.write('{"path":"{{templateValue}}"}')
    source = '@templateValue = substituted\nPOST {{domain}}/template-upload\nContent-Type: application/json\n\n<@ ./template.json\n'
    before = len(uploads)
    r = compat(source)
    check('template <@: original input is substituted and sent exactly once', r.get('ok') is True and
          uploads[before:] == [('/template-upload', b'{"path":"substituted"}')], r)
    with open(os.path.join(root, compat_rel)) as f:
        check('template <@: original source is unchanged', f.read() == source)

    source = ('@output = file.xml\n< {% request.variables.set("output", "xml-feed-output/runtime.xml"); %}\n'
              'GET {{domain}}/api/feed-short\n\n>>! {{output}}\n')
    r = compat(source)
    runtime_output = os.path.join(root, 'http-client/xml-feed-output/runtime.xml')
    check('runtime output overrides file variable and saves exact XML', r.get('ok') is True and
          os.path.isfile(runtime_output) and open(runtime_output, 'rb').read() == b'<short/>\r\n', r)
    for unsafe in ('../../escape.xml', '../README.md', '../.git/config'):
        r = compat(source.replace('xml-feed-output/runtime.xml', unsafe))
        check('runtime output retains file guard: ' + unsafe, r.get('ok') is False and
              not any(q.get('saved') for q in r.get('requests', [])), r)

    source = ('### Set\nGET {{domain}}/api/login\n\n> {% client.global.set("state", {value:"S-9", delimiter:String.fromCharCode(37,125)}); %}\n\n'
              '### Use\n< {% request.variables.set("check", client.global.get("state").value); %}\n'
              'GET {{domain}}/api/echo\nX-State: {{check}}\n\n'
              '> {% client.test("actual global restored", () => {client.assert(client.global.get("state").value === "S-9");'
              'client.assert(client.global.get("state").delimiter === String.fromCharCode(37,125));}); %}\n\n'
              '### Clear\nGET {{domain}}/api/echo\n\n> {% client.global.clearAll(); %}\n')
    r = compat(source, 0)
    check('object global is captured', r.get('ok') is True, r)
    before = len(hits)
    r = compat(source, 1)
    check('single request restores object global before its pre-handler', r.get('ok') is True and
          any(t['name'] == 'actual global restored' and t['ok'] for t in r['requests'][0]['tests']) and
          hits[before:][0][1].get('X-State') == 'S-9', r)
    r = compat(source, 2)
    stored = common.read_json(hc.GLOBALS_FILE, {}).get(m._globals_key(KEY, compat_rel, TUN))
    check('clearAll removes the stored global instead of resurrecting it', r.get('ok') is True and stored == {},
          {'result': r, 'stored': stored})
    r = compat(source, 0)
    r = compat('GET {{domain}}/api/echo\n\n> {% client.global.clearAll(); %}\n')
    check('Run all restores shared state and explicit clearAll removes it', r.get('ok') is True and
          common.read_json(hc.GLOBALS_FILE, {}).get(m._globals_key(KEY, compat_rel, TUN)) == {}, r)
    r = compat(source, 0)
    external_pre = 'request.variables.set("state", client.global.get("state").value);'
    with open(os.path.join(root, 'http-client/restore-pre.js'), 'w') as f:
        f.write(external_pre)
    r = compat('< ./restore-pre.js\nGET {{domain}}/api/echo\nX-State: {{state}}\n', 0)
    check('globals are restored before an external pre-handler without editing it', r.get('ok') is True and
          hits[-1][1].get('X-State') == 'S-9' and
          open(os.path.join(root, 'http-client/restore-pre.js')).read() == external_pre, r)

    common.write_json(hc.GLOBALS_FILE, {})
    computed = ('### Set computed\n< {% '
                'const key = "from" + "Pre"; client.global.set(key, {value:7}); %}\n'
                'GET {{domain}}/api/feed-short\n\n> {% const key = "from" + "Post"; '
                'client.global.set(key, {value:42}); client.global.set("null" + "Key", null); '
                'client.log("response log"); client.log(request.environment.get("apiPassword")); '
                'client.test("user test with log", () => {client.log("inside test"); client.assert(true);}); %}\n\n'
                '### Restore computed\nGET {{domain}}/api/feed-short\n\n'
                '> {% client.test("computed values restored", () => {client.assert(client.global.get("fromPre").value === 7); '
                'client.assert(client.global.get("fromPost").value === 42); client.assert(client.global.get("nullKey") === undefined);}); %}\n\n'
                '### Clear computed\nGET {{domain}}/api/feed-short\n\n> {% client.global.clearAll(); %}\n')
    r = compat(computed, 0)
    check('computed globals preserve pre/post objects and null without private global keys', r.get('ok') is True and
          common.read_json(hc.GLOBALS_FILE, {}).get(m._globals_key(KEY, compat_rel, TUN)) ==
          {'fromPre': {'value': 7}, 'fromPost': {'value': 42}, 'nullKey': None}, r)
    log = (r.get('requests') or [{}])[0].get('script_log', '')
    # The native CLI serializes the global store between phases (dropping null), and
    # snapshots its tests before executing callbacks. Match those native boundaries.
    check('response handler log is scrubbed and not counted as tests', r.get('ok') is True and
          'response log' in log and 'inside test' in log and PW not in json.dumps(r) and
          [t['name'] for t in r['requests'][0]['tests']] == ['user test with log'], r)
    m.http_share(r['run'], False)
    withheld = m.tool(s, 'http_last_result', {'file': compat_rel})
    check('response logs obey the body sharing boundary', all('script_log' not in q and 'body' not in q
          for q in withheld['requests']), withheld)
    r = compat(computed, 1)
    check('computed globals restore in the next single-request run', r.get('ok') is True and
          any(t['name'] == 'computed values restored' and t['ok'] for t in r['requests'][0]['tests']), r)
    r = compat(computed, 2)
    check('clearAll removes previously computed global keys', r.get('ok') is True and
          common.read_json(hc.GLOBALS_FILE, {}).get(m._globals_key(KEY, compat_rel, TUN)) == {}, r)

    with open(os.path.join(root, 'http-client/static-payload.json'), 'w') as f:
        f.write('{"native":"payload"}')
    handler = 'client.log("external response log"); client.test("variable handler", () => client.assert(response.status === 200));'
    with open(os.path.join(root, 'http-client/static-handler.js'), 'w') as f:
        f.write(handler)
    source = ('@body = {{$projectRoot}}/http-client/static-payload.json\n@handler = ./static-handler.js\n'
              '### Variable upload\nPOST {{domain}}/upload-static\nContent-Type: application/json\n\n'
              '< {{body}}\n\n> {{handler}}\n')
    before = len(uploads)
    r = compat(source)
    check('native file/projectRoot paths and variable external handler work unchanged', r.get('ok') is True and
          uploads[before:] == [('/upload-static', b'{"native":"payload"}')] and
          'external response log' in r['requests'][0].get('script_log', '') and
          open(os.path.join(root, compat_rel)).read() == source and
          open(os.path.join(root, 'http-client/static-handler.js')).read() == handler, r)

    source = ('@output = xml-feed-output/static-flow.xml\n### Export\nGET {{domain}}/api/feed-short\n\n>>! {{output}}\n\n'
              '### Consume\nPOST {{domain}}/upload-static-flow\nContent-Type: application/xml\n\n< ./xml-feed-output/static-flow.xml\n')
    before = len(uploads)
    r = compat(source)
    check('static variable export feeds the exact next native upload', r.get('ok') is True and
          uploads[before:] == [('/upload-static-flow', b'<short/>\r\n')], r)

    before = len(hits)
    # Different hostname keeps this exact-byte fixture free of persisted login cookies.
    iteration_url = 'http://127.0.0.1:%d/api/cookie/iteration?id={{id}}' % plain_srv.server_port
    r = compat('< {% request.variables.set("id", [1,2]); request.variables.set("out", "xml-feed-output/iteration-" + request.iteration() + ".json"); %}\nGET ' + iteration_url + '\n\n'
               '> {% client.test("iteration", () => client.assert(response.status === 200)); %}\n'
               '>>! {{out}}\n')
    check('iterated native exports preserve separate bytes and report identities without replay', r.get('ok') is True and
          len(hits[before:]) == 2 and [row.get('iteration') for row in r['requests']] == [0,1] and
          [row.get('saved') for row in r['requests']] == ['http-client/xml-feed-output/iteration-0.json',
                                                        'http-client/xml-feed-output/iteration-1.json'] and
          all(open(os.path.join(root,row['saved']), 'rb').read() == b'{"cookie": ""}' for row in r['requests']), r)

    r = compat('PROPFIND {{domain}}/api/feed-short\n')
    check('custom HTTPS method reaches the native runner with an honest result',
          r['requests'][0]['method'] == 'PROPFIND' and (r.get('ok') is True or
          any(t['name'] == 'Request Validation' and not t['ok'] for t in r['requests'][0]['tests'])), r)
    plain_url = 'http://localhost:%d/api/feed-short' % plain_srv.server_port
    r = compat('PROPFIND ' + plain_url + '\n')
    check('custom method runs natively over HTTP', r.get('ok') is True and
          r['requests'][0]['method'] == 'PROPFIND', r)
    with open(os.path.join(root, compat_rel), 'w') as f:
        f.write('GET ' + plain_url + '\n')
    r = m.http_gui_run(KEY, compat_rel, '', None)
    check('no env works natively without reading environment credentials', r.get('ok') is True and
          r['cls'] == 'prod' and r['env_dir'] is None, r)
    transport_timeout_check()
    transport_host_check()
    runtime_metadata_check()
    runtime_code_check()
    run_graph_check()
    native_save_errors_check()
    runtime_guard_check()
    before = len(hits)
    r = compat('GET {{domain}}/api/pdf\nX-Session-Id: S-9\n\n>>! xml-feed-output/unsafe.pdf\n')
    binary_output = os.path.join(root, 'http-client/xml-feed-output/unsafe.pdf')
    check('binary redirect publishes exact native bytes without repeating the request', r.get('ok') is True and
          os.path.isfile(binary_output) and open(binary_output, 'rb').read() == PDF and
          [p for p, _ in hits[before:]] == ['/api/pdf'], r)

    tunnel_tls_scope_check()
    managed_auth_check()
    browser_auth_check()
    finite_sse_check()
    websocket_adapter_check()
    persistent_cookie_check()
    shared_dir = os.path.join(root, 'shared')
    os.makedirs(shared_dir)
    for name, doc in ((hc.ENV_FILE, {'$shared': {'domain': 'https://localhost:%d' % SPORT}}),
                      (hc.PRIVATE_FILE, {'$shared': {'sharedPassword': 'synthetic-shared-secret'}, 'dev': {}})):
        with open(os.path.join(shared_dir, name), 'w') as f:
            json.dump(doc, f)
    source = 'GET {{domain}}/api/feed-short\nX-Shared: {{sharedPassword}}\n'
    with open(os.path.join(shared_dir, 'main.http'), 'w') as f:
        f.write(source)
    s.excludes.extend(['shared/' + hc.ENV_FILE, 'shared/' + hc.PRIVATE_FILE])
    before = len(hits)
    r = tool(file='shared/main.http', env='dev')
    check('$shared is used by the actual client and remains hidden in agent results', r.get('ok') is True and
          hits[before:][0][1].get('X-Shared') == 'synthetic-shared-secret' and
          'synthetic-shared-secret' not in json.dumps(r), r)
finally:
    for tk in list(m._db_tunnels):
        m._db_tunnel_close(tk, "e2e end")
    srv.shutdown()
    plain_srv.shutdown()
    ph.stop()
    print("ssh left running:", subprocess.run(["pgrep", "-f", fake_ssh], capture_output=True, text=True).stdout.strip() or "none")

print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
sys.exit(0 if all(ok for _, ok in results) else 1)
