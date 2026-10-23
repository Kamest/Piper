"""A minimal MCP client for IntelliJ's built-in MCP server.

IDEA 2025.2+ (Settings > Tools > MCP Server) serves MCP on loopback: streamable HTTP at
http://127.0.0.1:64342/stream (verified 2026-09-24 against IntelliJ IDEA 2026.2.1: JSON
responses, an `mcp-session-id` header, `notifications/initialized` -> 202) and the legacy
SSE transport at /sse. This client speaks streamable HTTP only - POST JSON-RPC with
`Accept: application/json, text/event-stream`, a JSON or an SSE-framed answer, the session
id echoed back, one re-initialize when the server forgot the session. An IDE that offers
only /sse is reported as unavailable with a note. Every call is time-bounded; the URL must
be loopback (a project path must never leave the machine).

piperd uses two read-only tools: get_file_problems and get_symbol_info, for the human's GUI
only (sessions.Workbench.idea_*): a file hidden from Claude is asked about like any other,
`.git` and paths outside the project never.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 3.0
PROTOCOL = "2025-06-18"
DEFAULT_URLS = ("http://127.0.0.1:64342/stream",)
LEGACY_URLS = ("http://127.0.0.1:64342/sse",)
MAX_BODY = 8 << 20
STATUS_TTL = 30.0


class McpError(Exception):
    pass


def loopback(url: str) -> bool:
    u = urllib.parse.urlsplit(url)
    return u.scheme == "http" and (u.hostname or "") in ("127.0.0.1", "localhost", "::1")


def _events(resp, want_id, deadline: float):
    """JSON-RPC messages of an SSE body (`data:` lines joined per event)."""
    data: list[str] = []
    total = 0
    while time.monotonic() < deadline:
        raw = resp.readline()
        if not raw:
            break
        total += len(raw)
        if total > MAX_BODY:
            raise McpError("MCP answer larger than %d bytes" % MAX_BODY)
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            continue
        if line == "" and data:
            try:
                msg = json.loads("\n".join(data))
            except ValueError:
                msg = None
            data = []
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict) and m.get("id") == want_id:
                    return m
    if data:
        try:
            m = json.loads("\n".join(data))
            if isinstance(m, dict) and m.get("id") == want_id:
                return m
        except ValueError:
            pass
    raise McpError("no answer to request %s in the event stream" % want_id)


class Client:
    def __init__(self, url: str, timeout: float = TIMEOUT, opener=None):
        if not loopback(url):
            raise McpError("the IntelliJ MCP URL must be http on loopback: %s" % url)
        self.url = url
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen
        self.sid: str | None = None
        self.proto: str | None = None
        self.next_id = 1
        self.server = ""

    def _post(self, msg: dict):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        if self.proto:
            headers["MCP-Protocol-Version"] = self.proto
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), method="POST", headers=headers)
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener(req, timeout=self.timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.sid = sid.strip()
                if "id" not in msg:
                    return None   # a notification: 202 Accepted, no body
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/event-stream" in ctype:
                    return _events(r, msg["id"], deadline)
                body = r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            raise McpError("HTTP %d" % e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise McpError("not reachable: %s" % getattr(e, "reason", e)) from None
        if len(body) > MAX_BODY:
            raise McpError("MCP answer larger than %d bytes" % MAX_BODY)
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise McpError("invalid JSON from the MCP server") from None
        for m in data if isinstance(data, list) else [data]:
            if isinstance(m, dict) and m.get("id") == msg["id"]:
                return m
        raise McpError("no answer to request %s" % msg["id"])

    def _rpc(self, method: str, params: dict) -> dict:
        self.next_id += 1
        m = self._post({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        if m.get("error"):
            err = m["error"]
            raise McpError("%s: %s" % (method, err.get("message") if isinstance(err, dict) else err))
        res = m.get("result")
        if not isinstance(res, dict):
            raise McpError("%s: no result" % method)
        return res

    def initialize(self) -> dict:
        self.sid, self.proto = None, None
        res = self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "piperd", "version": "1"}})
        self.proto = str(res.get("protocolVersion") or PROTOCOL)
        self.server = str((res.get("serverInfo") or {}).get("name") or "")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def call(self, tool: str, args: dict) -> dict:
        """tools/call -> {data (structuredContent or the text parsed as JSON), text, error}."""
        if self.proto is None:
            self.initialize()
        try:
            res = self._rpc("tools/call", {"name": tool, "arguments": args})
        except McpError as e:
            if not str(e).startswith(("HTTP 400", "HTTP 404")):
                raise
            self.initialize()   # the server dropped our session (IDE restarted)
            res = self._rpc("tools/call", {"name": tool, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text")
        data = res.get("structuredContent")
        if not isinstance(data, dict):
            try:
                data = json.loads(text) if text.strip().startswith("{") else None
            except ValueError:
                data = None
        return {"data": data if isinstance(data, dict) else None, "text": text[:65536],
                "error": bool(res.get("isError"))}


def legacy_only(url: str, opener=None, timeout: float = TIMEOUT) -> bool:
    """Does a legacy SSE endpoint answer (GET text/event-stream)? Reads only the headers."""
    if not loopback(url):
        return False
    req = urllib.request.Request(url, method="GET", headers={"Accept": "text/event-stream"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
            return "text/event-stream" in (r.headers.get("Content-Type") or "").lower()
    except (urllib.error.URLError, OSError, ValueError):
        return False


def problems(data: dict | None, text: str) -> list[dict]:
    """get_file_problems -> [{line, col, severity, message}] (best effort; no source lines)."""
    rows = []
    items = (data or {}).get("errors") if isinstance(data, dict) else None
    if isinstance(items, list):
        for e in items:
            if not isinstance(e, dict):
                continue
            try:
                line = int(e.get("line") or 0)
            except (TypeError, ValueError):
                continue
            col = e.get("column")
            rows.append({"line": line, "col": int(col) if isinstance(col, int) else None,
                         "severity": str(e.get("severity") or "").lower(),
                         "message": str(e.get("description") or e.get("message") or "")[:1000]})
        return rows[:1000]
    import re
    for m in re.finditer(r"(?im)^.*?\bline\s*[:=]?\s*(\d+)(?:.*?\bcol(?:umn)?\s*[:=]?\s*(\d+))?.*?[:-]\s*(.+)$", text):
        rows.append({"line": int(m.group(1)), "col": int(m.group(2)) if m.group(2) else None,
                     "severity": "", "message": m.group(3)[:1000]})
    return rows[:1000]
