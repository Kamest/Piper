"""sandbox/idea-bridge.py: stdio MCP -> IntelliJ's streamable HTTP with the Host rewritten."""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _env  # noqa: F401

BRIDGE = os.path.join(os.path.dirname(__file__), "..", "..", "piper", "sandbox", "idea-bridge.py")


class FakeIdea(BaseHTTPRequestHandler):
    """IDEA's rules: 403 unless Host is loopback, 404 for a session it no longer knows."""
    live = set()
    seen = []

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sid = self.headers.get("Mcp-Session-Id")
        FakeIdea.seen.append((msg.get("method"), sid))
        if not self.headers["Host"].startswith(("127.0.0.1:", "localhost:")):
            return self._send(403)
        if msg.get("method") == "initialize":
            sid = "s%d" % len(FakeIdea.seen)
            FakeIdea.live.add(sid)
            return self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}}, sid)
        if sid not in FakeIdea.live:
            return self._send(404)
        if "id" not in msg:
            return self._send(202, None)
        self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [], "sid": sid}})

    def _send(self, code, body=None, sid=None):
        data = json.dumps(body).encode() if body is not None else b"null" if code == 202 else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if sid:
            self.send_header("Mcp-Session-Id", sid)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class IdeaBridgeTest(unittest.TestCase):
    def setUp(self):
        FakeIdea.live.clear()
        FakeIdea.seen.clear()
        try:
            self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeIdea)
        except OSError as e:
            self.skipTest("cannot bind a socket here: %s" % e)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.p = subprocess.Popen([sys.executable, BRIDGE, "--host", "127.0.0.1", "--port", str(self.srv.server_port)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.p.stdout.close)
        self.addCleanup(self.p.stdin.close)
        self.addCleanup(self.p.wait)
        self.addCleanup(self.p.kill)

    def send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline()) if "id" in msg else None

    def test_session_is_renewed_when_the_ide_forgets_it(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        self.assertEqual(self.send(init)["result"]["serverInfo"]["name"], "fake")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})   # `null` answer: nothing on stdout
        self.assertEqual(self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["sid"], "s1")
        FakeIdea.live.clear()   # the IDE restarted
        r = self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual((r["id"], r["result"]["sid"]), (3, "s5"))   # one answer, for id 3, on a new session
        self.assertEqual([m for m, _ in FakeIdea.seen][-3:], ["initialize", "notifications/initialized", "tools/list"])

    def test_unreachable_ide_is_a_json_rpc_error(self):
        self.srv.shutdown()
        self.srv.server_close()
        r = self.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
        self.assertEqual(r["id"], 7)
        self.assertIn("not reachable", r["error"]["message"])


if __name__ == "__main__":
    unittest.main()
