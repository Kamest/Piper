"""9.7 #10: the IntelliJ MCP client (streamable HTTP: JSON and SSE answers, session id, re-init)
against a fake opener and - where the sandbox allows binding a port - a real local HTTP server;
and the Manager side: hidden files never reach IDEA, a symbol declared in a hidden file is withheld."""
import email.message
import http.server
import io
import json
import os
import threading
import unittest
import urllib.error

import _env
import ideamcp
import projectfs

URL = "http://127.0.0.1:64342/stream"


def headers(d):
    m = email.message.Message()
    for k, v in d.items():
        m[k] = v
    return m


class Resp(io.BytesIO):
    def __init__(self, body=b"", hdrs=None):
        super().__init__(body)
        self.headers = headers(hdrs or {})


def sse(*msgs):
    return b"".join(b"event: message\r\ndata: " + json.dumps(m).encode() + b"\r\n\r\n" for m in msgs)


class FakeIdea:
    """The IDEA server as an urllib opener: answers per JSON-RPC method."""

    def __init__(self, tools=None, sse_calls=True, expire_once=False):
        self.tools = tools or {}
        self.sse_calls = sse_calls
        self.expire_once = expire_once
        self.reqs = []
        self.sessions = 0

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.reqs.append((req.get_method(), req.full_url, dict(req.header_items()), body))
        if req.get_method() == "GET":
            raise urllib.error.URLError("no legacy endpoint")
        if body["method"] == "initialize":
            self.sessions += 1
            return Resp(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2025-06-18", "serverInfo": {"name": "IntelliJ IDEA MCP Server"}}}).encode(),
                {"Content-Type": "application/json", "mcp-session-id": "sid-%d" % self.sessions})
        if body["method"] == "notifications/initialized":
            return Resp(b"null", {"Content-Type": "application/json"})
        if body["method"] == "tools/call":
            if self.expire_once:
                self.expire_once = False
                raise urllib.error.HTTPError(req.full_url, 404, "session gone", headers({}), io.BytesIO(b""))
            name = body["params"]["name"]
            res = self.tools.get(name, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
            if callable(res):
                res = res(body["params"]["arguments"])
            msg = {"jsonrpc": "2.0", "id": body["id"], "result": res}
            if self.sse_calls:   # an SSE-framed answer, preceded by an unrelated notification
                return Resp(sse({"jsonrpc": "2.0", "method": "notifications/progress"}, msg),
                            {"Content-Type": "text/event-stream"})
            return Resp(json.dumps(msg).encode(), {"Content-Type": "application/json"})
        raise AssertionError(body)


class ClientTest(unittest.TestCase):
    def test_initialize_session_header_and_sse_answer(self):
        f = FakeIdea({"t": {"content": [{"type": "text", "text": '{"a": 1}'}]}})
        c = ideamcp.Client(URL, opener=f)
        r = c.call("t", {"x": 1})
        self.assertEqual(r, {"data": {"a": 1}, "text": '{"a": 1}', "error": False})
        self.assertEqual([x[3].get("method") for x in f.reqs], ["initialize", "notifications/initialized", "tools/call"])
        h = {k.lower(): v for k, v in f.reqs[2][2].items()}
        self.assertEqual((h["mcp-session-id"], h["mcp-protocol-version"]), ("sid-1", "2025-06-18"))
        self.assertIn("text/event-stream", h["accept"])
        self.assertIn("application/json", h["accept"])
        self.assertEqual(c.server, "IntelliJ IDEA MCP Server")

    def test_json_answer_structured_content_and_reinit(self):
        f = FakeIdea({"t": {"content": [{"type": "text", "text": "x"}], "structuredContent": {"b": 2}}},
                     sse_calls=False, expire_once=True)
        c = ideamcp.Client(URL, opener=f)
        self.assertEqual(c.call("t", {})["data"], {"b": 2})
        self.assertEqual(f.sessions, 2)                     # re-initialized once after the 404

    def test_errors_and_loopback_only(self):
        with self.assertRaises(ideamcp.McpError):
            ideamcp.Client("http://example.com:64342/stream")
        with self.assertRaises(ideamcp.McpError):
            ideamcp.Client("https://127.0.0.1:64342/stream")

        def down(req, timeout=None):
            raise urllib.error.URLError("refused")
        with self.assertRaises(ideamcp.McpError):
            ideamcp.Client(URL, opener=down).initialize()

        def rpc_error(req, timeout=None):
            b = json.loads(req.data)
            return Resp(json.dumps({"jsonrpc": "2.0", "id": b["id"], "error": {"code": -1, "message": "no"}}).encode(),
                        {"Content-Type": "application/json"})
        with self.assertRaisesRegex(ideamcp.McpError, "no"):
            ideamcp.Client(URL, opener=rpc_error).initialize()

        def endless(req, timeout=None):   # an event stream that never answers our id
            return Resp(sse(*[{"jsonrpc": "2.0", "method": "ping"}] * 3), {"Content-Type": "text/event-stream"})
        with self.assertRaises(ideamcp.McpError):
            ideamcp.Client(URL, opener=endless).initialize()

    def test_problem_parsing(self):
        data = {"filePath": "a", "errors": [{"severity": "WARNING", "description": "unused", "lineContent": "SECRET",
                                             "line": 3, "column": 5}, {"line": "x"}, "junk"]}
        self.assertEqual(ideamcp.problems(data, ""), [{"line": 3, "col": 5, "severity": "warning", "message": "unused"}])
        self.assertEqual(ideamcp.problems(None, "Error at line 12, column 4: bad thing")[0]["line"], 12)


class LiveHttpTest(unittest.TestCase):
    """The same client over a real socket (skipped where the sandbox forbids binding)."""

    def test_real_server(self):
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in b:
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                res = ({"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake"}} if b["method"] == "initialize"
                       else {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"documentation": "doc"}})
                raw = ("data: " + json.dumps({"jsonrpc": "2.0", "id": b["id"], "result": res}) + "\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Mcp-Session-Id", "live")
                self.end_headers()
                self.wfile.write(raw)   # the stream stays open: the client must stop at its answer
                self.wfile.flush()
                threading.Event().wait(1.5)
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        except OSError as e:
            self.skipTest("cannot bind a local port here: %s" % e)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        c = ideamcp.Client("http://127.0.0.1:%d/stream" % srv.server_address[1])
        self.assertEqual(c.call("get_symbol_info", {})["data"], {"documentation": "doc"})
        self.assertEqual(c.sid, "live")


class ManagerIdeaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = _env.make_project("idea", {
            "src/A.java": 'class A {\n  @Value("${db.password}") String p;\n  Foo foo;\n}\n',
            "config/secrets/db.properties": "db.password=x\n", ".git/HEAD": "ref"})
        cls.key = "team-x/idea"

    def setUp(self):
        self.m._idea_status = None

    def test_unavailable_and_cached(self):
        def down(req, timeout=None):
            raise urllib.error.URLError("refused")
        self.m.idea_opener = down
        st = self.m.idea_status()
        self.assertEqual((st["available"], st["url"]), (False, URL))
        self.assertIn("MCP Server", st["note"])
        self.assertEqual(self.m.idea_problems(self.key, "src/A.java")["available"], False)
        self.m.policy["idea_mcp_url"] = "http://10.0.0.1:1/stream"
        self.m._idea_status = None
        try:
            self.assertIn("loopback", self.m.idea_status()["note"].replace("127.0.0.1", "loopback"))
        finally:
            self.m.policy.pop("idea_mcp_url")

    def test_problems_and_symbols_show_hidden_files_to_the_gui(self):
        tools = {
            "get_file_problems": lambda a: {"content": [{"type": "text", "text": "{}"}], "structuredContent": {
                "filePath": a["filePath"], "timedOut": True,
                "errors": [{"severity": "ERROR", "description": "bad", "lineContent": "x", "line": 2, "column": 3}]}},
            "get_symbol_info": lambda a: {"content": [], "structuredContent": {
                "documentation": "db.password=x" if a["line"] == 2 else "class Foo",
                "symbolInfo": ({"name": "db.password", "declarationText": "db.password=x",
                                "declarationFile": "config/secrets/db.properties"} if a["column"] == 99 else
                               {"name": "db.password", "declarationText": "db.password=x",   # an IDEA on Windows (WSL)
                                "declarationFile": "\\\\wsl.localhost\\Ubuntu\\w\\config\\secrets\\db.properties"}
                               if a["column"] == 98 else
                               None if a["line"] == 2 else {"name": "Foo", "declarationText": "class Foo {}",
                                                             "declarationFile": "src/Foo.java"})}}}
        f = FakeIdea(tools, sse_calls=False)
        self.m.idea_opener = f
        self.assertTrue(self.m.idea_status()["available"])
        r = self.m.idea_problems(self.key, "src/A.java")
        self.assertEqual(r["problems"], [{"line": 2, "col": 3, "severity": "error", "message": "bad"}])
        self.assertIn("incomplete", r["note"])
        call = [x for x in f.reqs if x[3].get("method") == "tools/call"][-1][3]["params"]["arguments"]
        self.assertEqual((call["filePath"], call["projectPath"], call["errorsOnly"]), ("src/A.java", os.path.realpath(self.root), False))
        n = len(f.reqs)
        for bad in (".git/HEAD", "../idea/config/secrets/db.properties"):
            with self.assertRaises(projectfs.FsError):
                self.m.idea_problems(self.key, bad)
            with self.assertRaises(projectfs.FsError):
                self.m.idea_symbol(self.key, bad, 1, 1)
        self.assertEqual(len(f.reqs), n)                  # IDEA never heard of .git or another project
        # a file hidden from Claude is the GUI's like any other (2026-10-07)
        self.assertEqual(self.m.idea_problems(self.key, "config/secrets/db.properties")["problems"][0]["line"], 2)
        ok = self.m.idea_symbol(self.key, "src/A.java", 3, 3)
        self.assertEqual(ok, {"available": True, "info": "class Foo\n\nclass Foo {}"})
        w = self.m.idea_symbol(self.key, "src/A.java", 2, 99)   # declared in the hidden properties file
        self.assertEqual(w["info"], "db.password=x")
        with self.assertRaises(ValueError):
            self.m.idea_symbol(self.key, "src/A.java", 0, 1)


if __name__ == "__main__":
    unittest.main()
