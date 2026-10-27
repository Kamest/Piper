"""wsproto.py directly: handshake validation and frame parsing (piperd's terminal and state socket)."""
import base64
import contextlib
import os
import socket
import struct
import threading
import time
import unittest

import _env  # noqa: F401
import wsproto


def frame(op, payload=b"", fin=True, mask=True, rsv=0):
    b1 = (0x80 if fin else 0) | rsv | op
    n = len(payload)
    if n < 126:
        head = struct.pack("!BB", b1, (0x80 if mask else 0) | n)
    elif n < 1 << 16:
        head = struct.pack("!BBH", b1, (0x80 if mask else 0) | 126, n)
    else:
        head = struct.pack("!BBQ", b1, (0x80 if mask else 0) | 127, n)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i & 3] for i, b in enumerate(payload))


class WsProtoTest(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.a.close)
        self.addCleanup(self.b.close)
        self.ws = wsproto.WebSocket(self.a)

    def feed(self, *frames):   # from a thread: a socketpair buffers little, the reader is this one
        threading.Thread(target=self.b.sendall, args=(b"".join(frames),), daemon=True).start()

    def test_handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        ok = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
              "Sec-WebSocket-Key": key}
        self.assertEqual(wsproto.handshake_ok(ok), wsproto.accept_key(key))
        # RFC 6455 example
        self.assertEqual(wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        for k, v in (("Upgrade", "h2c"), ("Connection", "close"), ("Sec-WebSocket-Version", "8"),
                     ("Sec-WebSocket-Key", "short"), ("Sec-WebSocket-Key", "!!!not base64!!!")):
            self.assertIsNone(wsproto.handshake_ok(dict(ok, **{k: v})), k)

    def test_text_binary_and_fragments(self):
        self.feed(frame(wsproto.OP_TEXT, "grün".encode()))
        self.assertEqual(self.ws.recv(), (wsproto.OP_TEXT, "grün".encode()))
        big = os.urandom(70000)
        self.feed(frame(wsproto.OP_BIN, big))
        self.assertEqual(self.ws.recv(), (wsproto.OP_BIN, big))
        self.feed(frame(wsproto.OP_TEXT, b"ab", fin=False), frame(wsproto.OP_PING, b"p"),
                  frame(wsproto.OP_CONT, b"cd"))
        self.assertEqual(self.ws.recv(), (wsproto.OP_TEXT, b"abcd"))
        pong = self.b.recv(16)
        self.assertEqual(pong, bytes([0x80 | wsproto.OP_PONG, 1]) + b"p")

    def test_protocol_errors(self):
        for frames in ([frame(wsproto.OP_TEXT, b"x", mask=False)],
                       [frame(wsproto.OP_TEXT, b"x", rsv=0x40)],
                       [frame(wsproto.OP_CONT, b"x")],
                       [frame(wsproto.OP_TEXT, b"a", fin=False), frame(wsproto.OP_TEXT, b"b")],
                       [frame(0x3, b"x")]):
            a, b = socket.socketpair()
            ws = wsproto.WebSocket(a)
            b.sendall(b"".join(frames))
            with self.assertRaises(wsproto.WSProtocolError):
                ws.recv()
            a.close()
            b.close()

    def test_oversized_frame_is_refused_before_reading_it(self):
        self.feed(struct.pack("!BBQ", 0x80 | wsproto.OP_BIN, 0x80 | 127, wsproto.MAX_MESSAGE + 1))
        with self.assertRaises(wsproto.WSProtocolError):
            self.ws.recv()

    def test_oversized_fragmented_message_is_refused(self):
        part = b"x" * (1 << 20)
        frames = [frame(wsproto.OP_BIN, part, fin=False)] + [frame(wsproto.OP_CONT, part, fin=False)] * 4
        t = threading.Thread(target=lambda: self.b.sendall(b"".join(frames)), daemon=True)
        t.start()
        with self.assertRaises(wsproto.WSProtocolError):
            self.ws.recv()

    def test_close_and_eof(self):
        self.feed(frame(wsproto.OP_CLOSE, struct.pack("!H", 1000)))
        with self.assertRaises(wsproto.WSClosed):
            self.ws.recv()
        self.assertTrue(self.ws.closed)
        with self.assertRaises(wsproto.WSClosed):
            self.ws.send_text("late")
        a, b = socket.socketpair()
        ws = wsproto.WebSocket(a)
        b.close()
        with self.assertRaises(wsproto.WSClosed):
            ws.recv()
        a.close()

    def test_close_interrupts_a_blocked_writer(self):
        self.a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        finished = threading.Event()

        def write():
            try:
                self.ws.send_binary(b"x" * (1 << 20))
            except (OSError, wsproto.WSClosed):
                pass
            finally:
                finished.set()

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        deadline = time.monotonic() + 2
        while not self.ws._wlock.locked() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(self.ws._wlock.locked())
        closer = threading.Thread(target=self.ws.close, args=(4401, "access revoked"), daemon=True)
        closer.start()
        closer.join(1)
        try:
            self.assertFalse(closer.is_alive(), "revocation must not wait for a slow reader")
            self.assertTrue(self.ws.closed)
            self.assertTrue(finished.wait(1), "shutdown must unblock the sender")
        finally:
            with contextlib.suppress(OSError):
                self.b.shutdown(socket.SHUT_RDWR)
            writer.join(2)
            closer.join(2)

    def test_close_is_bounded_when_send_buffer_is_already_full(self):
        self.a.setblocking(False)
        try:
            while True:
                self.a.send(b"x" * 4096)
        except BlockingIOError:
            pass
        self.a.setblocking(True)
        closer = threading.Thread(target=self.ws.close, daemon=True)
        closer.start()
        closer.join(1)
        try:
            self.assertFalse(closer.is_alive(), "the close frame itself must have a deadline")
            self.assertTrue(self.ws.closed)
        finally:
            with contextlib.suppress(OSError):
                self.b.shutdown(socket.SHUT_RDWR)
            closer.join(2)

    def test_close_preserves_revocation_code_and_is_idempotent(self):
        self.ws.close(4401, "access revoked")
        self.ws.close(1000)
        self.b.settimeout(1)
        self.assertEqual(self.b.recv(256), frame(wsproto.OP_CLOSE,
                         struct.pack("!H", 4401) + b"access revoked", mask=False))
        self.assertEqual(self.b.recv(1), b"")

    def test_server_frames_are_unmasked_with_the_right_length_form(self):
        for n, head in ((5, 2), (300, 4), (70000, 10)):
            t = threading.Thread(target=self.ws.send_binary, args=(b"y" * n,), daemon=True)
            t.start()   # a socketpair buffers little: send while this side reads
            got = b""
            while len(got) < head + n:
                got += self.b.recv(1 << 17)
            self.assertEqual(got[0], 0x80 | wsproto.OP_BIN)
            self.assertFalse(got[1] & 0x80)
            t.join(5)
            self.assertEqual(len(got), head + n)


if __name__ == "__main__":
    unittest.main()
