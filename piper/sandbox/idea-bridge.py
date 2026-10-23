#!/usr/bin/env python3
"""idea-bridge - stdio MCP <-> IntelliJ's MCP server on the host (policy idea_in_sessions).

IDEA serves streamable HTTP on 127.0.0.1:<port>/stream and answers 403 to any Host header
other than 127.0.0.1 / localhost (verified 2026-10-05, IDEA 2026.2). The sandbox reaches the
host only as host.internal, and Claude Code (Node fetch) cannot override Host - so this bridge
forwards each JSON-RPC line from stdin to http://host.internal:<port>/stream with
Host: 127.0.0.1:<port> and writes the answers to stdout. The server->client GET stream is not
opened (IDEA's tools do not need it).

Usage (from the generated mcp.json): python3 idea-bridge.py --port 64342 [--path /stream] [--host host.internal]
"""
import json
import sys
import urllib.error
import urllib.request

TIMEOUT = 600   # tool calls (a build, a run configuration) can take minutes


def arg(name, default):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


PORT = int(arg("--port", "64342"))
URL = "http://%s:%d%s" % (arg("--host", "host.internal"), PORT, arg("--path", "/stream"))
HOST = "127.0.0.1:%d" % PORT
state = {"sid": None, "proto": None}


def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def error(msg, text):
    if isinstance(msg, dict) and "id" in msg:
        out({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": "idea-bridge: " + text}})


def sse(resp):
    data = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif line == "" and data:
            out_raw("\n".join(data))
            data = []
    if data:
        out_raw("\n".join(data))


def out_raw(text):
    try:
        m = json.loads(text)
    except ValueError:
        return
    if not isinstance(m, (dict, list)):
        return   # IDEA answers a notification with a bare `null`
    res = m.get("result") if isinstance(m, dict) else None
    if isinstance(res, dict) and res.get("protocolVersion") and "serverInfo" in res:
        state["proto"] = str(res["protocolVersion"])   # the initialize answer
    out(m)


def post(line, emit=True):
    headers = {"Host": HOST, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if state["sid"]:
        headers["Mcp-Session-Id"] = state["sid"]
    if state["proto"]:
        headers["MCP-Protocol-Version"] = state["proto"]
    req = urllib.request.Request(URL, data=line.encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            state["sid"] = sid.strip()
        if not emit:
            r.read()
        elif "text/event-stream" in (r.headers.get("Content-Type") or "").lower():
            sse(r)
        else:
            body = r.read()
            if body.strip():
                out_raw(body.decode("utf-8", "replace"))


def reinit():
    """IDEA forgot our MCP session (404 - e.g. the IDE restarted): initialize again with the
    client's own initialize request, without passing that answer on (its id is long answered)."""
    state["sid"], state["proto"] = None, None
    post(state["init"], emit=False)
    msg = json.loads(state["init"])
    state["proto"] = (msg.get("params") or {}).get("protocolVersion")
    post(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), emit=False)


def forward(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return
    if isinstance(msg, dict) and msg.get("method") == "initialize":
        state["init"], state["sid"], state["proto"] = line, None, None
    try:
        try:
            post(line)
        except urllib.error.HTTPError as e:
            if e.code != 404 or not state["sid"] or not state.get("init") or line == state["init"]:
                raise
            reinit()
            post(line)
    except urllib.error.HTTPError as e:
        error(msg, "HTTP %d from IntelliJ at %s" % (e.code, URL))
    except (urllib.error.URLError, OSError) as e:
        error(msg, "IntelliJ not reachable at %s (%s) - is IDEA running with Settings > Tools > MCP Server?"
              % (URL, getattr(e, "reason", e)))


def main():
    for line in sys.stdin:
        if line.strip():
            forward(line.strip())


if __name__ == "__main__":
    main()
