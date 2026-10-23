"""Minimal RFC 6455 WebSocket server side, stdlib only.

Only what piperd needs: the handshake answer, reading client frames (always masked,
fragmented messages reassembled, ping answered, close honoured) and writing unmasked
server frames. No extensions (permessage-deflate is not negotiated), no subprotocols.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 4 << 20


class WSClosed(Exception):
    pass


class WSProtocolError(Exception):
    pass


def accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


def handshake_ok(headers) -> str | None:
    """Return the Sec-WebSocket-Accept value, or None when this is no valid upgrade."""
    if headers.get("Upgrade", "").lower() != "websocket":
        return None
    if "upgrade" not in headers.get("Connection", "").lower():
        return None
    if headers.get("Sec-WebSocket-Version", "") != "13":
        return None
    key = headers.get("Sec-WebSocket-Key", "")
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return None
    except ValueError:
        return None
    return accept_key(key)


class WebSocket:
    """One accepted connection. recv() from one thread, send() from any thread."""

    def __init__(self, sock: socket.socket, rfile=None):
        self.sock = sock
        self.rfile = rfile or sock.makefile("rb")
        self._wlock = threading.Lock()
        self.closed = False

    def _read(self, n: int) -> bytes:
        data = self.rfile.read(n)
        if data is None or len(data) < n:
            raise WSClosed("connection closed")
        return data

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read(2)
        fin, op = bool(b1 & 0x80), b1 & 0x0F
        if b1 & 0x70:
            raise WSProtocolError("reserved bits set")
        if not b2 & 0x80:
            raise WSProtocolError("client frames must be masked")
        n = b2 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._read(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read(8))
        if n > MAX_MESSAGE:
            raise WSProtocolError("frame too large")
        mask = self._read(4)
        payload = bytearray(self._read(n))
        for i in range(n):
            payload[i] ^= mask[i & 3]
        return fin, op, bytes(payload)

    def recv(self) -> tuple[int, bytes]:
        """Next complete data message as (OP_TEXT|OP_BIN, payload). Raises WSClosed."""
        msg_op, parts, size = None, [], 0
        while True:
            fin, op, payload = self._frame()
            if op == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                self.close(1000)
                raise WSClosed("closed by peer")
            if op in (OP_TEXT, OP_BIN):
                if msg_op is not None:
                    raise WSProtocolError("new message inside a fragmented one")
                msg_op = op
            elif op == OP_CONT:
                if msg_op is None:
                    raise WSProtocolError("continuation without a message")
            else:
                raise WSProtocolError("unknown opcode %d" % op)
            parts.append(payload)
            size += len(payload)
            if size > MAX_MESSAGE:
                raise WSProtocolError("message too large")
            if fin:
                return msg_op, b"".join(parts)

    def _send(self, op: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | op, n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | op, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | op, 127, n)
        with self._wlock:
            if self.closed and op != OP_CLOSE:
                raise WSClosed("already closed")
            self.sock.sendall(head + payload)

    def send_text(self, text: str) -> None:
        self._send(OP_TEXT, text.encode())

    def send_binary(self, data: bytes) -> None:
        self._send(OP_BIN, data)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self._send(OP_CLOSE, struct.pack("!H", code) + reason.encode()[:120])
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
