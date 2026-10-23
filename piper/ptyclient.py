"""Client for ptyhost's AF_UNIX protocol (see ptyhost.py docstring).

One PtyClient = one connection. Requests are synchronous (call() waits for the reply
with the same rid); `data` / `exit` events are delivered to the on_event callback from
the reader thread. piperd keeps one control connection and opens one extra connection
per attached browser terminal, so a slow browser never stalls the control path.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Callable


class PtyHostError(RuntimeError):
    pass


class PtyClient:
    def __init__(self, path: str | None, on_event: Callable[[dict], None] | None = None,
                 on_close: Callable[[], None] | None = None, sock: socket.socket | None = None):
        self.path = path
        self.on_event = on_event
        self.on_close = on_close
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(path)
        self.sock = sock
        self._wlock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._plock = threading.Lock()
        self._rid = 0
        self.closed = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        if self._reader is not threading.current_thread():
            self._reader.join(5)
            if self._reader.is_alive():
                raise RuntimeError("PTY client reader did not stop")

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("ptyhost closed the connection")
            buf += chunk
        return bytes(buf)

    def _read_loop(self) -> None:
        try:
            while True:
                (n,) = struct.unpack("!I", self._read_exact(4))
                msg = json.loads(self._read_exact(n))
                rid = msg.get("rid")
                if rid is not None:
                    with self._plock:
                        slot = self._pending.pop(rid, None)
                    if slot is not None:
                        slot.append(msg)
                        slot[0].set()
                    continue
                if self.on_event:
                    try:
                        self.on_event(msg)
                    except Exception as e:  # noqa: BLE001
                        print("ptyclient: event handler failed: %s" % e, file=sys.stderr)
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            notify = not self.closed
            self.closed = True
            with self._plock:
                for slot in self._pending.values():
                    slot[0].set()
                self._pending.clear()
            self.sock.close()
            if notify and self.on_close:
                self.on_close()

    def call(self, op: str, timeout: float = 10.0, **kw) -> dict:
        """Send one request and wait for its reply."""
        if self.closed:
            raise PtyHostError("connection closed")
        with self._plock:
            self._rid += 1
            rid = self._rid
            slot: list = [threading.Event()]   # the reader appends the reply, then sets
            self._pending[rid] = slot
        raw = json.dumps(dict(kw, op=op, rid=rid), separators=(",", ":")).encode()
        with self._wlock:
            self.sock.sendall(struct.pack("!I", len(raw)) + raw)
        if not slot[0].wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise PtyHostError("ptyhost did not answer %r within %ss" % (op, timeout))
        if len(slot) < 2:
            raise PtyHostError("ptyhost connection closed during %r" % op)
        return slot[1]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def connect(path: str, autostart: bool = True, **kw) -> PtyClient:
    """Connect to ptyhost, starting it detached (own session) when it is not running."""
    try:
        return PtyClient(path, **kw)
    except (FileNotFoundError, ConnectionRefusedError):
        if not autostart:
            raise
    here = os.path.dirname(os.path.abspath(__file__))
    # the socket dir may be the short temp dir (common._pty_sock) that nothing created yet
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    log = open(os.path.join(os.path.dirname(path), "ptyhost.log"), "ab")
    subprocess.Popen([sys.executable, os.path.join(here, "ptyhost.py")],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, env=dict(os.environ, PIPER_PTY_SOCK=path))
    deadline = time.time() + 5
    while True:
        try:
            return PtyClient(path, **kw)
        except (FileNotFoundError, ConnectionRefusedError):
            if time.time() > deadline:
                raise PtyHostError("ptyhost did not come up at %s" % path)
            time.sleep(0.05)
