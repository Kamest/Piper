#!/usr/bin/env python3
"""tests/piper-e2e/http_e2e.py - .http tunnels / save_to / hints with REAL parts: JetBrains' ijhttp CLI, an HTTPS server
(self-signed, CN not localhost), an ssh stand-in that really forwards TCP (Piper's _db_ssh_open path), urllib, the
Manager through the session tools (tests/piper-unit/_env.py: a throwaway PIPER_HOME, no piperd).

Needs ijhttp (PIPER_IJHTTP=<path>, else `ijhttp` on PATH; https://jb.gg/ijhttp/latest), java for it and openssl;
without them it prints SKIP and exits 77. Named http_e2e.py, not http.py: a script dir is first on sys.path, and a
http.py there would shadow the stdlib `http` package for every script of this directory.

Run OUTSIDE any sandbox (sockets):  python3 tests/piper-e2e/http_e2e.py
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
PW = "Secret-pass-42"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(detail)[:600]))


# --- an HTTPS server with a self-signed certificate for another name
crt, key = os.path.join(WORK, "c.pem"), os.path.join(WORK, "k.pem")
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt, "-days", "1",
                "-subj", "/CN=test-app.example"], check=True, capture_output=True)
hits = []


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        hits.append((self.path, dict(self.headers)))
        if self.path.startswith("/api/cookie/"):
            body, ct = json.dumps({"cookie": self.headers.get("Cookie", "")}).encode(), "application/json"
        elif self.path.startswith("/api/login"):
            body, ct = b'{"sessionId": "S-9"}', "application/json"
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
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
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
TUN = "test with tunel `ssh -L 8443:192.0.2.1:443 projects@test-k.example`"
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


F = "http-client/vn.http"
try:
    # 1. http_envs shows the tunnel derived from the name
    envs = m.http_gui_envs(KEY, F)
    check("http_envs (GUI) lists the ssh tunnel", "projects@test-k.example -> 192.0.2.1:443" in envs.get("tunnels", {}).get(TUN, ""), envs.get("tunnels"))
    check("http_envs: no SSLConfiguration variable", "SSLConfiguration" not in json.dumps(envs.get("variables")), envs.get("variables"))

    # 2. the colleague's flow: whole file (login, PDF, ...) through Piper's tunnel with the real ijhttp
    r = tool(file=F, env=TUN, reason="e2e")
    q = r.get("requests") or []
    check("whole file: login ran, tests marked not evaluated (CLI stop)", q and q[0]["status"] == 200 and
          q[0]["tests"] and q[0]["tests"][0]["name"] == "(not evaluated)", q[:1])
    check("whole file: PDF 200 through Piper's port, body replaced by a hint",
          len(q) > 1 and q[1]["status"] == 200 and "save_to" in q[1].get("body", ""), q[1:2])
    check("whole file: the rest reported as not run", len(q) == 4 and all("not run - the ijhttp CLI stops" in x["error"] for x in q[2:]), q[2:])
    check("card shows the tunnel", any("Through Piper's tunnel" in c[1] for c in cards), [c[1][:300] for c in cards])
    with open(ssh_log) as f:
        argv = f.read().split("\n")[0]
    check("ssh started with the declared server/forward", "-l projects" in argv and ":192.0.2.1:443 -- test-k.example" in argv, argv)

    # 3. save_to before the login of this env: names the request that sets the global
    common.write_json(hc.GLOBALS_FILE, {})
    r = tool(file=F, env=TUN, index=1, save_to="posudek.pdf")
    check("save_to without the global: names the Login request", "set by request #0 'Login'" in (r.get("requests") or [{}])[0].get("error", ""), r)
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
finally:
    for tk in list(m._db_tunnels):
        m._db_tunnel_close(tk, "e2e end")
    srv.shutdown()
    ph.stop()
    print("ssh left running:", subprocess.run(["pgrep", "-f", fake_ssh], capture_output=True, text=True).stdout.strip() or "none")

print("\n%d/%d passed" % (sum(ok for _, ok in results), len(results)))
sys.exit(0 if all(ok for _, ok in results) else 1)
