"""SSH environment transport. SSH owns host verification; no public server ports or copied tokens.

Every mutation is sent once. A lost response has an unknown outcome and is never replayed.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shlex
import socket
import subprocess
import tarfile
import threading
from urllib.parse import urlsplit

import common
import extensions
from httpcompat import HELPER_NAMES
from ptyclient import PtyClient

LOCK = threading.RLock()
FILE = os.path.join(common.PIPER_HOME, "environments.json")
ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")
HOST = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.@:-]{0,253}$")


class Unavailable(RuntimeError):
    pass


class Rejected(ValueError):
    """Authenticated peer refused a mutation before executing it; safe to show explicit retry."""


def entries() -> list[dict]:
    with LOCK:
        return common.read_json(FILE, {}).get("environments", [])


def get(eid: str) -> dict:
    for entry in entries():
        if entry["id"] == eid:
            return entry
    raise ValueError("unknown environment")


def save(body: dict) -> dict:
    eid, host = body.get("id", ""), body.get("host", "")
    if not isinstance(eid, str) or not ID.fullmatch(eid) or eid == "local":
        raise ValueError("environment id: 1–40 letters, digits, _ or -; local is reserved")
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise ValueError("use an SSH config alias or user@host, without shell options")
    path = body.get("project_path", "")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 2048 or any(ord(c) < 32 for c in path):
        raise ValueError("project root must be an absolute server path")
    mode = body.get("mode", "service")
    if mode not in ("service", "ssh"):
        raise ValueError("mode must be service or ssh")
    label = body.get("label", eid)
    if not isinstance(label, str) or not label.strip() or len(label) > 100 or any(ord(c) < 32 for c in label):
        raise ValueError("invalid environment label")
    entry = dict(id=eid, host=host, label=common.no_bidi(label), project_path=path, mode=mode)
    with LOCK:
        doc = common.read_json(FILE, {})
        identities = doc.get("identities", {})
        for previous in doc.get("environments", []):
            identities.setdefault(previous["id"], {k: previous[k] for k in ("host", "project_path", "mode")})
        identity = {k: entry[k] for k in ("host", "project_path", "mode")}
        if eid in identities and identities[eid] != identity:
            raise ValueError("This id belongs to a different host, project root or mode. Use a new id; existing tabs and providers must keep their original target.")
        identities[eid] = identity
        if eid in doc.get("peer_identities", {}):
            entry["identity"] = doc["peer_identities"][eid]
        rows = [x for x in doc.get("environments", []) if x["id"] != eid] + [entry]
        common.write_json(FILE, dict(doc, environments=rows, identities=identities))
        os.chmod(FILE, 0o600)
    return entry


def remove(eid: str) -> None:
    with LOCK:
        get(eid)
        doc = common.read_json(FILE, {})
        identities = doc.get("identities", {})
        for row in doc.get("environments", []):
            identities.setdefault(row["id"], {k: row[k] for k in ("host", "project_path", "mode")})
        common.write_json(FILE, dict(doc, environments=[x for x in entries() if x["id"] != eid], identities=identities))


def ssh(entry: dict, command: list[str], *, terminal=False) -> list[str]:
    # No accept-new/StrictHostKeyChecking=no. A new or changed host must be verified by
    # the user using their regular SSH client. BatchMode never opens a hidden password prompt.
    return ["ssh", "-tt" if terminal else "-T", "-oBatchMode=yes", "-oConnectTimeout=8",
            "-oServerAliveInterval=15", "-oServerAliveCountMax=2", "--", entry["host"], shlex.join(command)]


def peer_source() -> str:
    with open(os.path.join(common.PIPER_DIR, "remotepeer.py"), encoding="utf-8") as f:
        return f.read()


def request(entry: dict, payload: dict, timeout=35) -> dict:
    # Establish identity by a read before the first write; the peer verifies it BEFORE dispatch.
    if not entry.get("identity") and payload.get("op") != "probe" and not (payload.get("op") == "http" and payload.get("method") == "GET"):
        reply = request(entry, {"op": "probe"}, timeout)
        entry = dict(entry, identity=reply["_peer_identity"])
    payload = dict(payload, project_path=entry["project_path"], mode=entry["mode"], expected_identity=entry.get("identity"))
    try:
        p = subprocess.run(ssh(entry, ["python3", "-c", peer_source()]),
                           input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable("SSH unavailable; the server session may still be running. The request was not retried.") from e
    if p.returncode:
        # stderr can contain remote banners/config and commands; never return or log raw credentials.
        raise Unavailable("SSH request failed. Check the host key, SSH alias, login and Python 3 on the server. No request was retried.")
    try:
        result = json.loads(p.stdout)
        if not isinstance(result, dict):
            raise ValueError()
        if "error" in result:
            if result.get('not_executed') is True and entry.get('identity') and result.get('_peer_identity') == entry['identity']:
                raise Rejected(str(result['error']))
            raise Unavailable(str(result["error"]))
        identity = result.get("_peer_identity", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError()
        if entry.get("identity") and identity != entry["identity"]:
            raise Unavailable("SSH target identity changed. Verify the server and use a new environment ID.")
        with LOCK:
            doc = common.read_json(FILE, {})
            pins = doc.get("peer_identities", {})
            if entry["id"] in pins and pins[entry["id"]] != identity:
                raise Unavailable("SSH target identity changed. Verify the server and use a new environment ID.")
            if pins.get(entry["id"]) != identity and any(row["id"] == entry["id"] for row in doc.get("environments", [])):
                pins[entry["id"]] = identity
                rows = [dict(row, identity=identity) if row["id"] == entry["id"] else row for row in doc["environments"]]
                common.write_json(FILE, dict(doc, environments=rows, peer_identities=pins))
                os.chmod(FILE, 0o600)
        return result
    except Rejected:
        raise
    except ValueError as e:
        raise Unavailable("Invalid server response; operation outcome is unknown. Refresh before repeating a write.") from e


def probe(eid: str) -> dict:
    return request(get(eid), {"op": "probe"})


def bundle() -> tuple[bytes, str]:
    """Ship only application files; never personal state, repo config, tokens or .git."""
    stream = io.BytesIO()
    http_helpers = {'piper/httpcompat/' + name for name in HELPER_NAMES}
    with tarfile.open(fileobj=stream, mode="w:gz") as tf:
        for root in ("piper", "scripts"):
            for directory, dirs, files in os.walk(os.path.join(common.SK, root)):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ("design", "__pycache__", "node_modules", "src")]
                for name in sorted(files):
                    path = os.path.join(directory, name)
                    relative = os.path.relpath(path, common.SK)
                    if name.startswith('.') or os.path.islink(path):
                        continue
                    if root == "scripts" and not name.endswith((".sh", ".py")):
                        continue
                    if root == "piper" and not (name.endswith('.py') or relative in http_helpers or relative == 'piper/advisor/CLAUDE.md' or relative.startswith('piper/web/') and (name.endswith(('.js','.map','.mjs','.css','.html','.svg','.png','.woff2','.webmanifest')) or name in ('LICENSE','VERSIONS','SHA256SUMS'))):
                        continue
                    tf.add(path, arcname=relative, recursive=False)
        tf.add(os.path.join(common.SK, "piper", "web", "themes.json"), arcname="piper/web/themes.json")   # piperd.THEMES
    raw = stream.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def install(eid: str) -> dict:
    entry = get(eid)
    if entry["mode"] != "service":
        raise ValueError("switch to service mode before installing")
    raw, sha = bundle()
    return request(entry, {"op": "install", "archive": base64.b64encode(raw).decode(), "sha": sha}, timeout=120)


def proxy(eid: str, method: str, path: str, raw: bytes = b"", ctype="application/json") -> dict:
    if method not in ("GET", "POST") or not path.startswith("/api/") or urlsplit(path).netloc:
        raise ValueError("only Piper API requests can be forwarded")
    if path.startswith("/api/environments"):
        raise ValueError("nested environment forwarding is disabled")
    timeout = common.gui_proxy_timeout(method, path)
    return request(get(eid), {"op": "http", "method": method, "path": path, "timeout": timeout,
                             "body": base64.b64encode(raw).decode(), "ctype": ctype}, timeout=timeout + 7)


def terminal(eid: str, sid: str, callback) -> PtyClient:
    """Bridge the existing ptyhost protocol over SSH, with no publicly exposed PTY socket."""
    entry = get(eid)
    if not entry.get("identity"):
        probe(eid)
        entry = get(eid)
    identity_arg = "--identity=" + entry["identity"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", sid):
        raise ValueError("invalid session id")
    if entry["mode"] == "ssh":
        # The local PTY holds only an SSH attachment. tmux and the agent live on the server.
        import ptyclient
        pc = ptyclient.connect(common.PTY_SOCK)
        attachid = "remote-" + hashlib.sha256((eid + ":" + sid).encode()).hexdigest()[:24]
        rows = pc.call("list").get("sessions", [])
        if not any(x["id"] == attachid and x["alive"] for x in rows):
            result = pc.call("spawn", id=attachid, argv=ssh(entry, ["python3", "-c", peer_source(), identity_arg, "attach", sid], terminal=True),
                             env={k:v for k,v in os.environ.items() if not k.startswith(("PIPER_", *extensions.collect("daemon_env")))}, cwd=common.REAL_HOME, cols=120, rows=34, locked=False)
            if not result.get("ok"):
                pc.close()
                raise Unavailable("could not attach SSH terminal")
        pc.remote_id = attachid
        pc.on_event = callback
        return pc
    # socketpair keeps PtyClient's timeout/framing and streaming implementation unchanged.
    left, right = socket.socketpair()
    command = ["python3", "-c", peer_source(), identity_arg, "--project-root=" + entry["project_path"], "terminal"]
    p = subprocess.Popen(ssh(entry, command), stdin=right, stdout=right, stderr=subprocess.DEVNULL)
    right.close()
    class RemoteClient(PtyClient):
        def close(self):
            super().close()
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)
    return RemoteClient(None, on_event=callback, sock=left)
