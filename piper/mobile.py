"""Opt-in iroh mobile transport. The relay never receives a Piper bearer token.

Pairing grants the same access as the local GUI. Only a locally issued, single-use
60-second invitation may add an authenticated endpoint key. Revocation closes live
connections. The HTTP destination is fixed to this machine's loopback Piper.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
from urllib.parse import urlsplit

import common

ALPN = b"piper/mobile/1"
MAX_HEADER = 8192
MAX_BODY = 16 << 20
MAX_RESPONSE = 32 << 20
MAX_FRAME = 4 << 20
TERMINAL_CHUNK = 65536
MAX_TERMINAL_MESSAGE = 16 << 20  # local ptyhost protocol bound; mobile frames stay small
PAIR_TTL = 60
MAX_HANDSHAKES = 16
MAX_PAIRED_CONNECTIONS = 64
MAX_UNPAIRED_CONNECTIONS = 4
STATE = os.path.join(common.PIPER_HOME, "mobile.json")
PORT = int(os.environ.get("PIPER_PORT", "17895"))


def https_url(value: str) -> str:
    """Accept a fixed HTTPS origin, never credentials, query parameters or fragments."""
    u = urlsplit(value)
    if (u.scheme != "https" or not u.hostname or u.username or u.password
            or u.path not in ("", "/") or u.query or u.fragment
            or "\\" in value or any(ord(c) <= 32 or ord(c) > 126 for c in value)):
        raise ValueError("an HTTPS origin without credentials, path, query or fragment is required")
    port = u.port  # reject malformed ports too
    host = u.hostname
    if ":" in host:
        ipaddress.IPv6Address(host)
        host = "[" + host + "]"
    elif not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError("invalid HTTPS hostname")
    # Match URL.origin in the browser (case and the implicit HTTPS port).
    return "https://" + host + (":" + str(port) if port is not None and port != 443 else "")


def relay_url(value: str) -> str:
    """A private relay may carry one scoped capability; public client origins never do."""
    origin, separator, query = value.partition("?")
    origin = https_url(origin)
    if separator and not re.fullmatch(r"token=[A-Za-z0-9_-]{43,128}", query):
        raise ValueError("invalid relay capability")
    return origin + ("?" + query if separator else "")


def relay_token(path) -> str:
    """Read a bounded owner-only regular file, keeping relay credentials out of argv."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 256:
            raise ValueError("relay token requires an owner-only regular file")
        token = source.read(256).decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("invalid relay capability")
    return token


@contextlib.contextmanager
def state_lock():
    """Serialize CLI and server mutations; never lose a concurrent revoke or pairing."""
    common.ensure_home()
    fd = os.open(STATE + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_state() -> dict:
    """Missing state means disabled; malformed state is an error, never an empty allowlist."""
    try:
        with open(STATE) as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"v": 1, "devices": {}}
    if not isinstance(state, dict) or state.get("v") != 1 or not isinstance(state.get("devices"), dict):
        raise ValueError("invalid mobile state")
    return state


def save_state(state: dict) -> None:
    """Replace the private state atomically; caller holds state_lock()."""
    path = STATE + ".tmp"
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(path, 0o600)
    os.replace(path, STATE)


def invitation(client: str, label: str = "Mobile") -> str:
    """Create one short-lived bearer invitation, with the PC key pinned in its fragment."""
    client = https_url(client)
    if not isinstance(label, str) or not label or len(label) > 80 or any(ord(c) < 32 for c in label):
        raise ValueError("device label must contain 1–80 printable characters")
    with state_lock():
        state = load_state()
        if not state.get("endpoint") or not state.get("relay"):
            raise ValueError("start the mobile transport with an explicit relay first")
        code = secrets.token_urlsafe(32)
        state["pending"] = {"digest": hashlib.sha256(code.encode()).hexdigest(),
                            "expires": time.time() + PAIR_TTL, "label": label}
        state["client"] = client
        save_state(state)
        ticket = {"v": 1, "endpoint": state["endpoint"], "relay": state["relay"], "code": code}
    encoded = base64.urlsafe_b64encode(json.dumps(ticket, separators=(",", ":")).encode()).decode().rstrip("=")
    return client + "/#iroh=" + encoded


def pair(peer: str, code: str) -> bool:
    """Bind the invitation to the TLS-authenticated peer, never to a claimed request field."""
    if not re.fullmatch(r"[0-9a-f]{64}", peer) or not isinstance(code, str) or len(code) > 128:
        return False
    with state_lock():
        state = load_state()
        pending = state.get("pending", {})
        if (pending.get("expires", 0) <= time.time()
                or not hmac.compare_digest(pending.get("digest", ""), hashlib.sha256(code.encode()).hexdigest())):
            return False
        state["devices"][peer] = {"label": pending["label"], "at": time.time(), "grant": secrets.token_hex(16)}
        state["last_pairing"] = pending["digest"]
        state.pop("pending", None)
        save_state(state)
    common.audit({"event": "mobile-paired"})
    return True


def revoke(peer: str) -> bool:
    """Remove exactly one grant; live connections observe its removal within one second."""
    with state_lock():
        state = load_state()
        existed = state["devices"].pop(peer, None) is not None
        save_state(state)
    if existed:
        common.audit({"event": "mobile-revoked"})
    return existed


def grant(peer: str) -> str | None:
    return load_state()["devices"].get(peer, {}).get("grant")


def cancel_invitation(identity: str) -> bool:
    """Cancel only the invitation this dialog created, never a newer tab's invitation."""
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("invalid invitation")
    with state_lock():
        state = load_state()
        if not hmac.compare_digest(state.get("pending", {}).get("digest", ""), identity):
            return False
        state.pop("pending")
        save_state(state)
    return True


@contextlib.contextmanager
def runtime_lock():
    """One native bridge per identity; the lock also provides local GUI health."""
    common.ensure_home()
    with open(STATE + ".running", "a+") as running:
        os.chmod(running.name, 0o600)
        try:
            fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("mobile transport is already running") from None
        running.seek(0)
        running.truncate()
        try:
            yield running
        finally:
            fcntl.flock(running, fcntl.LOCK_UN)


def desktop_status() -> dict:
    """No private key, relay capability, device grant or invitation code leaves this API."""
    state = load_state()
    ready = False
    try:
        with open(STATE + ".running", "r") as running:
            try:
                fcntl.flock(running, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(running, fcntl.LOCK_UN)
            except BlockingIOError:
                ready = running.read(16) == "ready"
    except FileNotFoundError:
        pass
    pending = state.get("pending", {})
    return {"ready": ready, "configured": bool(state.get("endpoint") and state.get("relay")),
            "computer": state.get("name", "Piper"),
            "install_url": https_url(state.get("client", "https://piper.kamest.dev")) + "/"
                           if state.get("endpoint") and state.get("relay") else None,
            "devices": [{"id": peer, "label": device["label"], "at": device["at"]}
                        for peer, device in state["devices"].items()],
            "pending": {"id": pending["digest"], "expires": pending["expires"]}
                       if pending.get("expires", 0) > time.time() else None,
            "last_pairing": state.get("last_pairing")}


def desktop_invitation(label: str) -> dict:
    if not desktop_status()["ready"]:
        raise ValueError("Mobile access is unavailable. Check that this computer is online and try again.")
    client = load_state().get("client", "https://piper.kamest.dev")
    expires = time.time() + PAIR_TTL
    url = invitation(client, label)
    # Derive metadata from this exact ticket; another tab may have replaced it already.
    ticket = json.loads(base64.urlsafe_b64decode(url.split("#iroh=")[1] + "==="))
    return {"url": url, "id": hashlib.sha256(ticket["code"].encode()).hexdigest(),
            "expires": expires}


def header_bytes(value: dict) -> bytes:
    body = json.dumps(value, separators=(",", ":")).encode()
    if len(body) > MAX_HEADER:
        raise ValueError("header too large")
    return struct.pack("!I", len(body)) + body


async def read_header(recv) -> dict:
    size = struct.unpack("!I", await recv.read_exact(4))[0]
    if not 0 < size <= MAX_HEADER:
        raise ValueError("invalid header size")
    value = json.loads(await recv.read_exact(size))
    if not isinstance(value, dict) or value.get("v") != 1:
        raise ValueError("unsupported mobile protocol")
    return value


def request_target(value: dict) -> tuple[str, str, dict]:
    """Only relative Piper API paths; no caller-controlled upstream or security headers."""
    method, path, headers = value.get("method", "GET"), value.get("path", ""), value.get("headers", {})
    if (method not in ("GET", "POST") or not isinstance(path, str) or not path.startswith("/api/")
            or "\\" in path or "#" in path or any(ord(c) < 33 or ord(c) > 126 for c in path)):
        raise ValueError("invalid Piper request")
    if not isinstance(headers, dict):
        raise ValueError("invalid request headers")
    if value.get("accept_encoding") not in (None, "gzip"):
        raise ValueError("unsupported mobile compression")
    allowed = {"content-type", "if-none-match", "x-piper-environment"}
    clean = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("invalid request header")
        if name.lower() in allowed:
            if any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError("invalid request header value")
            clean[name.lower()] = value
    return method, path, clean


class _HttpCancellation:
    """Stop a discarded mobile request, including a blocking loopback response read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled = False
        self.socket = None

    def attach(self, sock) -> bool:
        with self.lock:
            if self.cancelled:
                return False
            self.socket = sock
            return True

    def detach(self):
        with self.lock:
            self.socket = None

    def cancel(self):
        with self.lock:
            self.cancelled = True
            sock, self.socket = self.socket, None
        # close() alone does not interrupt a socket read held by HTTPResponse.
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def proxy_http(value: dict, body: bytes, token: str, authorized, cancellation=None, *, timeout=None) -> tuple[dict, bytes]:
    """One request, no redirects or retries. The destination and credentials stay local."""
    method, path, headers = request_target(value)
    if timeout is None:
        timeout = max(120, common.gui_proxy_timeout(method, path) + 30)  # Above the forwarded SSH deadline, too.
    if value.get("accept_encoding") == "gzip":
        headers["Accept-Encoding"] = "gzip"
    if len(body) > MAX_BODY:
        raise ValueError("request body too large")
    headers["Authorization"] = "Bearer " + token
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        # A request can wait in the executor after the async authorization check.
        # Recheck this connection's exact grant at the actual dispatch boundary.
        if not authorized() or (cancellation is not None and cancellation.cancelled):
            raise PermissionError("device revoked before dispatch")
        if cancellation is not None:
            conn.connect()
            if not cancellation.attach(conn.sock) or not authorized():
                raise PermissionError("request cancelled before dispatch")
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError("response too large")
        response_headers = response.getheaders()
        encoding = next((v for name, v in response_headers if name.lower() == "content-encoding"), "")
        if encoding and (encoding != "gzip" or value.get("accept_encoding") != "gzip"):
            raise ValueError("unsupported upstream compression")
        meta = {"v": 1, "status": response.status, "headers": {
            name: value for name, value in response_headers
            if name.lower() in ("content-type", "etag", "content-disposition")}}
        if encoding:
            meta["encoding"] = encoding
        return meta, data
    finally:
        if cancellation is not None:
            cancellation.detach()
        conn.close()


async def read_frame(recv) -> tuple[int, bytes]:
    size, kind = struct.unpack("!IB", await recv.read_exact(5))
    if size > MAX_FRAME or kind not in (1, 2, 8):
        raise ValueError("invalid terminal frame")
    return kind, await recv.read_exact(size)


async def write_frame(send, kind: int, data: bytes) -> None:
    if len(data) > MAX_FRAME:
        raise ValueError("terminal frame too large")
    await send.write_all(struct.pack("!IB", len(data), kind) + data)


async def proxy_terminal(value: dict, recv, send, token: str, authorized) -> None:
    """Adapt a QUIC stream to the existing terminal WebSocket, without exposing its bearer."""
    from websockets.asyncio.client import connect
    path = value.get("path", "")
    if (not isinstance(path, str) or not path.startswith("/ws/") or "\\" in path or "#" in path
            or any(ord(c) < 33 or ord(c) > 126 for c in path)):
        raise ValueError("invalid terminal path")
    async with connect("ws://127.0.0.1:%d%s" % (PORT, path), origin="http://127.0.0.1:%d" % PORT,
                       proxy=None, max_size=MAX_TERMINAL_MESSAGE, open_timeout=5) as ws:
        await ws.send(json.dumps({"auth": token, "replay_geometry": True}))
        await send.write_all(header_bytes({"v": 1, "status": 101}))

        async def upstream():
            while True:
                kind, data = await read_frame(recv)
                if not authorized():
                    raise PermissionError("device revoked")
                if kind == 8:
                    return
                await ws.send(data.decode() if kind == 1 else data)

        async def downstream():
            async for data in ws:
                if not authorized():
                    raise PermissionError("device revoked")
                if isinstance(data, str):
                    await write_frame(send, 1, data.encode())
                else:
                    # A replay can fill the 8 MiB PTY ring. Keep binary bytes and
                    # geometry events ordered, without a multi-megabyte mobile frame.
                    for start in range(0, len(data), TERMINAL_CHUNK):
                        if not authorized():
                            raise PermissionError("device revoked")
                        await write_frame(send, 2, data[start:start + TERMINAL_CHUNK])

        tasks = [asyncio.create_task(f()) for f in (upstream, downstream)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve_connection(conn, token: str, on_paired=None) -> None:
    """Enforce authorization on authenticated keys, including idle and already open streams."""
    peer = conn.remote_id().to_bytes().hex()
    original_grant = grant(peer)
    tasks = set()

    def authorized():
        return original_grant is not None and grant(peer) == original_grant

    async def watch():
        while True:
            await asyncio.sleep(1)
            try:
                valid = authorized()
            except (OSError, ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                conn.close(4401, b"device authorization ended")
                return

    async def handle_stream(bi, cancellation):
        nonlocal original_grant
        recv, send = bi.recv(), bi.send()
        stream_grant = original_grant

        def stream_authorized():
            # A later re-pairing on this connection must not revive an old request.
            return stream_grant is not None and grant(peer) == stream_grant

        replied = False
        try:
            value = await asyncio.wait_for(read_header(recv), 5)
            if value.get("op") == "hello":
                await asyncio.wait_for(recv.read_to_end(0), 2)
                body = json.dumps({"computer": load_state().get("name", "Piper"), "paired": authorized()}).encode()
                await send.write_all(header_bytes({"v": 1, "status": 200,
                    "headers": {"Content-Type": "application/json"}}) + body)
                replied = True
            elif value.get("op") == "pair":
                await asyncio.wait_for(recv.read_to_end(0), 2)
                if not pair(peer, value.get("code")):
                    raise PermissionError("invitation expired or already used")
                original_grant = grant(peer)
                if on_paired is not None:
                    on_paired()
                await send.write_all(header_bytes({"v": 1, "status": 200}))
                replied = True
            elif not stream_authorized():
                raise PermissionError("device not paired")
            elif value.get("op") == "push":
                await asyncio.wait_for(recv.read_to_end(0), 2)
                import mobilepush
                result = await asyncio.to_thread(mobilepush.action, peer, stream_grant, value)
                if not stream_authorized():
                    raise PermissionError("device revoked")
                await send.write_all(header_bytes({"v": 1, "status": 200,
                    "headers": {"Content-Type": "application/json"}}) + json.dumps(result).encode())
                replied = True
            elif value.get("op") == "http":
                request_target(value)
                body = await asyncio.wait_for(recv.read_to_end(MAX_BODY), 30)
                if not stream_authorized():
                    raise PermissionError("device revoked")
                meta, data = await asyncio.to_thread(proxy_http, value, body, token, stream_authorized, cancellation)
                if not stream_authorized():
                    raise PermissionError("device revoked")
                await send.write_all(header_bytes(meta) + data)
                replied = True
            elif value.get("op") == "ws":
                replied = True  # errors after upgrade terminate the stream, never corrupt its framing
                await proxy_terminal(value, recv, send, token, stream_authorized)
            else:
                raise ValueError("unknown operation")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exception messages may contain user data, URLs or tokens; keep them off the wire/log.
            if not replied:
                with contextlib.suppress(Exception):
                    await send.write_all(header_bytes({"v": 1, "status": 403, "error": "mobile request refused"}))
        finally:
            with contextlib.suppress(Exception):
                await send.finish()

    async def stream(bi):
        # STOP_SENDING is the phone's deadline/cancellation, not a connection failure.
        # Free its stream slot and interrupt any upstream read without replaying it.
        cancellation = _HttpCancellation()
        work = asyncio.create_task(handle_stream(bi, cancellation))
        stopped = asyncio.create_task(bi.send().stopped())
        try:
            done, _ = await asyncio.wait([work, stopped], return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                await work
        finally:
            cancellation.cancel()
            work.cancel()
            stopped.cancel()
            await asyncio.gather(work, stopped, return_exceptions=True)

    watcher = None
    try:
        # Unpaired peers get exactly one short pairing attempt and no idle connection.
        if original_grant is None:
            for attempt in range(2):  # a public hello, then explicit pairing consent on the phone
                bi = await asyncio.wait_for(conn.accept_bi(), 5 if attempt == 0 else PAIR_TTL)
                await asyncio.wait_for(stream(bi), 5)
                if authorized():
                    break
            if not authorized():
                return
        watcher = asyncio.create_task(watch())
        while True:
            bi = await conn.accept_bi()
            if not authorized():
                return
            if len(tasks) >= 16:
                # Refuse only this stream. Closing the connection here also killed
                # healthy terminals when a GUI refresh briefly exceeded the limit.
                async def refuse():
                    await bi.send().write_all(header_bytes({"v": 1, "status": 503,
                                                           "error": "mobile request capacity reached"}))
                    await bi.send().finish()
                    await bi.recv().stop(503)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(refuse(), 1)
                continue
            task = asyncio.create_task(stream(bi))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        conn.close(4401, b"mobile connection ended")
        if watcher:
            watcher.cancel()
            tasks.add(watcher)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def mobile_endpoint(iroh, key: bytes, relay: str):
    """Browser clients use the relay; never expose the FFI's default UDP listeners on the LAN.

    The current FFI cannot clear IP transports. Explicit defaults override both
    wildcard listeners upstream; verify the resulting sockets before serving.
    """
    class LoopbackPreset(iroh.Preset):
        def apply(self, builder):
            builder.apply_minimal()  # no public DNS publication / discovery
            builder.bind_addr("127.0.0.1:0")
            builder.bind_addr("[::1]:0")
    endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(
        preset=LoopbackPreset(), secret_key=key, alpns=[ALPN],
        relay_mode=iroh.RelayMode.custom_from_urls([relay_url(relay)])))
    if not all(ipaddress.ip_address(addr.rsplit(":", 1)[0].strip("[]")).is_loopback
               for addr in endpoint.bound_sockets()):
        await endpoint.close()
        raise ValueError("mobile transport unexpectedly bound a network UDP socket")
    return endpoint


async def serve_endpoint(endpoint, token: str) -> None:
    """Separate short handshakes and unpaired consent waits from authorized capacity.

    Unknown peers must complete authentication before choosing their budget. A
    saturated unpaired budget cannot reserve slots belonging to paired phones.
    This bounds application resources; relay admission/rate limits are still
    required against handshake floods and network-level denial of service.
    """
    tasks, handshakes, paired, unpaired = set(), set(), set(), set()

    async def accept(incoming):
        task = asyncio.current_task()
        connection, group = None, None
        try:
            accepting = await asyncio.wait_for(incoming.accept(), 5)
            connection = await asyncio.wait_for(accepting.connect(), 5)
            handshakes.discard(task)
            peer = connection.remote_id().to_bytes().hex()
            is_paired = grant(peer) is not None
            group = paired if is_paired else unpaired
            limit = MAX_PAIRED_CONNECTIONS if is_paired else MAX_UNPAIRED_CONNECTIONS
            if len(group) >= limit:
                return
            group.add(task)

            def promote():
                nonlocal group
                if group is unpaired:
                    if len(paired) >= MAX_PAIRED_CONNECTIONS:
                        raise PermissionError("paired connection budget full")
                    unpaired.discard(task)
                    paired.add(task)
                    group = paired
            await serve_connection(connection, token, promote)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            handshakes.discard(task)
            if group is not None:
                group.discard(task)
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close(4401, b"mobile connection ended")

    try:
        while (incoming := await endpoint.accept_next()) is not None:
            if len(handshakes) >= MAX_HANDSHAKES:
                await incoming.refuse()
                continue
            task = asyncio.create_task(accept(incoming))
            tasks.add(task)
            handshakes.add(task)
            task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(relay: str, name: str = "Piper") -> None:
    with runtime_lock() as running:
        await _run(relay, name, running)


async def _run(relay: str, name: str, running) -> None:
    """Start only on explicit invocation; no n0 DNS publication or default public relay."""
    import iroh
    relay = relay_url(relay)
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("invalid computer name")
    with state_lock():
        state = load_state()
        state.setdefault("key", secrets.token_hex(32))
        key = bytes.fromhex(state["key"])
        state["relay"] = relay
        state["name"] = name
        save_state(state)
    endpoint = await mobile_endpoint(iroh, key, relay)
    try:
        await asyncio.wait_for(endpoint.online(), 30)
        with state_lock():
            state = load_state()
            state["endpoint"] = endpoint.id().to_bytes().hex()
            save_state(state)
        token = common.own_token("mobile-bridge")
        running.write("ready")
        running.flush()
        print("Mobile transport ready; pair phones in Piper Settings > Mobile access.", flush=True)

        import mobilepush
        sender = asyncio.create_task(mobilepush.run(token))
        try:
            await serve_endpoint(endpoint, token)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
    finally:
        await endpoint.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--relay", required=True)
    serve.add_argument("--relay-token-file", help="owner-only file containing a private relay capability")
    serve.add_argument("--name", default="Piper")
    link = sub.add_parser("link", help="authorize one phone with full Piper access for pairing within 60 seconds")
    link.add_argument("--client", required=True)
    link.add_argument("--label", default="Mobile")
    sub.add_parser("devices")
    remove = sub.add_parser("revoke")
    remove.add_argument("endpoint")
    args = parser.parse_args()
    try:
        if args.command == "serve":
            relay = (https_url(args.relay) + "?token=" + relay_token(args.relay_token_file)
                     if args.relay_token_file else relay_url(args.relay))
            asyncio.run(run(relay, args.name))
        elif args.command == "link":
            print(invitation(args.client, args.label))
        elif args.command == "devices":
            print(json.dumps({peer: {k: device[k] for k in ("label", "at")}
                              for peer, device in load_state()["devices"].items()}, indent=2))
        elif args.command == "revoke":
            print("Device revoked." if revoke(args.endpoint) else "Device was not paired.")
    except Exception:
        raise SystemExit("mobile transport unavailable; check configuration and install the optional mobile runtime") from None


if __name__ == "__main__":
    main()
