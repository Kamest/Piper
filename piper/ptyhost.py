#!/usr/bin/env python3
"""ptyhost - holds the PTYs of every orchestrated hole session.

A deliberately small, long-lived process. It owns one pseudo-terminal per session and
nothing else: no network listener, no policy, no knowledge of hole. piperd (restartable)
talks to it over an AF_UNIX socket in $PIPER_HOME (directory 0700, socket 0600), so an
piperd restart or upgrade never closes a PTY - which matters because closing a session's
PTY sends SIGHUP to `hole`, and hole tears the sandbox down on SIGHUP
(hole v2.1.0 start.go:watchSignals).

Protocol: length-prefixed frames, each `!I` big-endian length + UTF-8 JSON. Binary data
travels base64-encoded in the `data` field. Requests carry an optional `rid`, echoed on
the reply. Ops:

  spawn     {id, argv, env, cwd?, cols, rows, log?, locked?}  -> {ok, pid}
  input     {id, data, auto?}   rejected while the session is locked (attestation, 6.5);
                                auto=true: typed by piperd, not counted as the human's input
  pending   {id}                -> {ok, key_at, enter_at}: when the human last touched the
                                prompt and when the last Enter was (InputTracker; 9.12) - piperd
                                types automatic text only while nothing was typed after the
                                Enter of the last submitted prompt
  lock      {id} / unlock {id}
  resize    {id, cols, rows}    -> {ok} · {ok, ignored: "tty"} while another client is the
                                session's TTY (the native terminal's size wins)
  signal    {id, sig}           "TERM" | "HUP" | "INT" | "KILL"
  subscribe {id, from?, data?, tty?, take?}  replay from the latest safe point (or byte offset),
                                then live `data` events; `exit` event when the child ends.
                                data=false: no replay, no stream - the `exit` event only.
                                tty=true: this client is the session's native terminal:
                                its `resize` wins, the others' is ignored while it stays;
                                one TTY per session - a second gets {ok: false, error:
                                "tty-busy"}, unless take=true (the old one gets `tty-taken`)
  unsubscribe {id}
  forget    {id}                drop an exited session's buffer
  list      {}                  -> {sessions: [...]}
  ping      {}                  -> {ok, pid, version}

Events (no rid): {ev: "data", id, off, data} · {ev: "exit", id, rc} · {ev: "respawn", id}
(a new child under the same id - retry/resume - keeps its subscribers; they reset the screen)
· {ev: "size", id, cols, rows} when the PTY size changed · {ev: "tty", id, n} when a TTY
client came (n=1) or went (n=0) · {ev: "tty-taken", id} to the TTY client another one
replaced. `size` and `tty` also reach the exit-only (control) connections.

Replay (4.3): the ring buffer keeps the last PIPER_PTY_BUFFER bytes (default 8 MiB).
Replay starts at the newest "safe point" at or after the buffer start - the session start,
or the byte right after a full-screen clear (ESC c, or ESC[2J ... ESC[3J) - so a trimmed
buffer is never replayed from the middle of an escape sequence or a half-drawn frame.
"""
from __future__ import annotations

import base64
import errno
import fcntl
import json
import os
import pty
import selectors
import signal
import socket
import stat
import struct
import sys
import termios
import time

VERSION = 8   # 8: TTY clients (subscribe tty/take, resize priority, size/tty events); 7: `pending` -> key_at + enter_at (9.12 simplified); 2-6: earlier input tracking
BUFFER_CAP = int(os.environ.get("PIPER_PTY_BUFFER", str(8 << 20)))
MAX_FRAME = 16 << 20
READ_CHUNK = 65536
SHUTDOWN_WAIT = 10.0   # _shutdown: TERM, this long for the children to end, then KILL
SIGNALS = {"TERM": signal.SIGTERM, "HUP": signal.SIGHUP, "INT": signal.SIGINT,
           "KILL": signal.SIGKILL}

# Full-screen clears after which the screen can be rebuilt from scratch.
_SAFE_MARKERS = (b"\x1bc", b"\x1b[3J", b"\x1b[2J\x1b[H", b"\x1b[H\x1b[2J")


def _winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class InputTracker:
    """When the human last touched Claude Code's prompt: piperd types
    automatic text (a handoff line, a task, a follow-up) only while nothing was typed since the
    last submitted prompt - one signal, no guessing what an Enter did (it may submit, end a line
    after a backslash, pick an @ file or answer a dialog; piperd cannot see the screen).

    `key_at` = the last byte that can put text into the prompt or change it: printable
    characters, a paste, Enter, Tab, Backspace, Ctrl keys, Up/Down (history). The terminal's own
    answers are not the human: OSC / DCS / APC / PM / SOS strings (xterm.js reporting colours,
    DECRQSS) are swallowed until BEL / ESC \\ or the end of the write (every answer arrives in one
    write, so Alt+] typed by the human does not swallow the keys after it); other CSI sequences
    (focus and mouse reports, cursor position) and a lone Esc count nothing. piperd's own input
    (`input.auto`) never counts."""

    def __init__(self):
        self.key_at = 0.0
        self.enter_at = 0.0    # the last Enter (the human's, or piperd's own): where a submitted prompt ended
        self._st = ""          # "" | "esc" | "csi" | "ss3" | "str" | "str_esc"
        self._csi = b""
        self._strlen = 0
        self._paste = False

    def auto(self, data: bytes) -> None:
        """piperd typed this: never the human's touch, but its Enter submits the prompt."""
        if b"\r" in data:
            self.enter_at = time.time()

    def feed(self, data: bytes) -> None:
        key = enter = after = False   # after: a key followed the last Enter in this write
        for b in data:
            st = self._st
            if st in ("str", "str_esc"):   # a terminal's answer: swallowed
                self._strlen += 1
                if b == 0x07 or (st == "str_esc" and b == 0x5c) or self._strlen > 4096:
                    self._st = ""
                else:
                    self._st = "str_esc" if b == 0x1b else "str"
                continue
            if st == "esc":
                self._st = ""
                if b == 0x5b:            # [
                    self._st, self._csi = "csi", b""
                elif b == 0x4f:          # O: SS3 (application keys)
                    self._st = "ss3"
                elif b in (0x5d, 0x50, 0x5f, 0x5e, 0x58) and not self._paste:   # ] P _ ^ X: a string
                    self._st, self._strlen = "str", 0
                elif b == 0x0d or b >= 0x20:   # ESC CR (Shift/Option+Enter), Alt+key: the human's
                    key = True
                elif b == 0x1b:
                    self._st = "esc"
                continue
            if st == "ss3":
                self._st = ""
                if b in (0x41, 0x42):    # Up/Down: history recall puts text into the prompt
                    key = True
                continue
            if st == "csi":
                if 0x40 <= b <= 0x7e:    # final byte
                    self._st = ""
                    seq = self._csi
                    if b == 0x7e and seq == b"200":
                        self._paste, key = True, True
                    elif b == 0x7e and seq == b"201":
                        self._paste = False
                    elif b in (0x41, 0x42) and not seq:   # Up/Down (plain): history
                        key = True
                    elif b == 0x7e and seq.isdigit():     # Delete, Insert, Home/End... (not the paste marks)
                        key = True
                    # everything else is the terminal reporting (focus, mouse, cursor position) or moving the cursor
                else:
                    self._csi = (self._csi + bytes([b]))[-16:]
                continue
            if b == 0x1b:
                self._st = "esc"
            else:
                key = True               # printable, Enter, Tab, Backspace, Ctrl keys, pasted text
                if b == 0x0d and not self._paste:
                    enter, after = True, False
                elif enter:
                    after = True
        if self._st in ("esc", "ss3", "str", "str_esc") or (self._st == "csi" and not self._csi):
            self._st = ""                # a lone Esc key; every sequence arrives in one write
        if key:
            self.key_at = time.time()
        if enter:   # keys after the Enter in the same write are a new draft: the Enter comes just before them
            self.enter_at = self.key_at - (0.001 if after else 0.0)


class Session:
    """One child process on one PTY, with its output ring buffer."""

    def __init__(self, sid: str, pid: int, fd: int, cols: int, rows: int, log: str | None,
                 locked: bool):
        self.id = sid
        self.pid = pid
        self.fd = fd
        self.cols = cols
        self.rows = rows
        self.locked = locked
        self.rc: int | None = None
        self.started = time.time()
        self.buf = bytearray()
        self.base = 0              # absolute offset of buf[0]
        self.safe = [0]            # absolute offsets of safe replay points (ascending)
        self.log = open(log, "ab", buffering=0) if log else None
        self.subscribers: set[Client] = set()   # data + exit
        self.exit_only: set[Client] = set()     # exit only (piperd's control connection)
        self.tty: Client | None = None          # the native terminal attached: its size wins
        self.wbuf = bytearray()                 # input not yet accepted by the PTY
        self.typed = InputTracker()             # the human's unsent prompt (9.12)
        self._tail = b""           # last bytes of the previous chunk (markers split across reads)

    @property
    def end(self) -> int:
        return self.base + len(self.buf)

    def append(self, chunk: bytes) -> int:
        """Store output; return its absolute start offset."""
        start = self.end
        scan = self._tail + chunk
        for marker in _SAFE_MARKERS:
            i = scan.find(marker)
            while i != -1:
                off = start - len(self._tail) + i + len(marker)
                if not self.safe or off > self.safe[-1]:
                    self.safe.append(off)
                i = scan.find(marker, i + 1)
        self.safe.sort()
        self._tail = scan[-8:]
        self.buf += chunk
        if self.log:
            try:
                self.log.write(chunk)
            except OSError:
                pass
        if len(self.buf) > BUFFER_CAP:
            drop = len(self.buf) - BUFFER_CAP
            del self.buf[:drop]
            self.base += drop
            self.safe = [s for s in self.safe if s >= self.base]
        return start

    def replay_from(self) -> int:
        """Where a fresh terminal should start: the newest safe point still in the buffer
        (a clear at the very end means an empty screen: nothing to replay)."""
        candidates = [s for s in self.safe if s >= self.base]
        if candidates:
            return candidates[-1]
        return self.base  # no safe point left: best effort from the buffer start

    def slice(self, frm: int) -> tuple[int, bytes]:
        frm = max(frm, self.base)
        return frm, bytes(self.buf[frm - self.base:])

    def info(self) -> dict:
        return {"id": self.id, "pid": self.pid, "alive": self.rc is None, "rc": self.rc,
                "locked": self.locked, "cols": self.cols, "rows": self.rows,
                "bytes": self.end, "started": self.started, "key_at": self.typed.key_at,
                "tty": 1 if self.tty is not None else 0}


class Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbuf = bytearray()
        self.outbuf = bytearray()


class Host:
    def __init__(self, path: str | None = None):
        self.path = path
        self.sel = selectors.DefaultSelector()
        self.sessions: dict[str, Session] = {}
        self.by_fd: dict[int, Session] = {}
        self.clients: set[Client] = set()
        self.running = True

    # ---------------------------------------------------------------- lifecycle
    def serve(self) -> None:
        """Bind the socket (0600 in a 0700 dir) and run until SIGTERM/SIGINT."""
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            srv.bind(self.path)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        srv.listen(8)
        srv.setblocking(False)
        self.sel.register(srv, selectors.EVENT_READ, ("listen", None))
        signal.signal(signal.SIGCHLD, lambda *_: None)   # wake select; reaped below
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        self.loop()
        self._shutdown(srv)

    def add_client(self, sock: socket.socket) -> None:
        """Adopt an already connected socket (tests use socket.socketpair())."""
        sock.setblocking(False)
        c = Client(sock)
        self.clients.add(c)
        self.sel.register(sock, selectors.EVENT_READ, ("client", c))

    def loop(self) -> None:
        while self.running:
            try:
                events = self.sel.select(timeout=1.0)
            except InterruptedError:
                events = []
            for key, mask in events:
                kind, obj = key.data
                try:
                    if kind == "listen":
                        self._accept(key.fileobj)
                    elif kind == "pty":
                        if mask & selectors.EVENT_READ:
                            self._read_pty(obj)
                        if mask & selectors.EVENT_WRITE and obj.fd in self.by_fd:
                            self._write_pty(obj)
                    elif kind == "client":
                        if mask & selectors.EVENT_READ:
                            self._read_client(obj)
                        if mask & selectors.EVENT_WRITE and obj in self.clients:
                            self._flush(obj)
                except Exception as e:  # noqa: BLE001 - one bad event must not end every sandbox (review 2026-10-05)
                    print("ptyhost: %s event: %s: %s" % (kind, type(e).__name__, e), file=sys.stderr, flush=True)
                    if kind == "client" and obj in self.clients:
                        self._drop(obj)
            try:
                self._reap()
            except Exception as e:  # noqa: BLE001
                print("ptyhost: reap: %s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)

    def _stop(self, *_):
        self.running = False

    def _shutdown(self, srv=None) -> None:
        # Closing a PTY hangs up its hole process, which tears the sandbox down.
        for s in list(self.sessions.values()):
            if s.rc is None:
                try:
                    os.kill(s.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + SHUTDOWN_WAIT
        while time.time() < deadline and any(s.rc is None for s in self.sessions.values()):
            self._reap()
            time.sleep(0.2)
        for s in list(self.sessions.values()):   # hole drops a TERM before it attaches: never leave one
            if s.rc is None:                     # behind (its `hole __watchdog` removes the sandbox)
                try:
                    os.kill(s.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._reap()
        if srv is None:
            return
        srv.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------- children
    def _spawn(self, req: dict) -> dict:
        sid = str(req["id"])
        if sid in self.sessions and self.sessions[sid].rc is None:
            return {"ok": False, "error": "session already running"}
        argv = [str(a) for a in req["argv"]]
        env = {str(k): str(v) for k, v in (req.get("env") or {}).items()}
        cols, rows = int(req.get("cols", 120)), int(req.get("rows", 32))
        cwd = req.get("cwd") or None
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], argv, env)
            except BaseException as e:  # noqa: BLE001 - child must never return
                os.write(2, ("ptyhost: exec %s failed: %s\r\n" % (argv[0], e)).encode())
            os._exit(127)
        _winsize(fd, cols, rows)
        os.set_blocking(fd, False)
        s = Session(sid, pid, fd, cols, rows, req.get("log"), bool(req.get("locked", True)))
        old = self.sessions.get(sid)
        if old is not None:   # retry/resume under the same id: the open terminals follow
            s.subscribers, s.exit_only, s.tty = old.subscribers, old.exit_only, old.tty
            for c in list(s.subscribers):
                self._send(c, {"ev": "respawn", "id": sid})
        self.sessions[sid] = s
        self.by_fd[fd] = s
        self.sel.register(fd, selectors.EVENT_READ, ("pty", s))
        return {"ok": True, "pid": pid}

    def _read_pty(self, s: Session) -> None:
        try:
            chunk = os.read(s.fd, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError as e:
            if e.errno != errno.EIO:  # EIO = slave side closed (child exited)
                raise
            chunk = b""
        if not chunk:
            self._close_pty(s)
            return
        off = s.append(chunk)
        data = base64.b64encode(chunk).decode()
        for c in list(s.subscribers):
            self._send(c, {"ev": "data", "id": s.id, "off": off, "data": data})

    def _write_pty(self, s: Session) -> None:
        """Flush queued input without ever blocking the loop (a full PTY buffer waits for
        EVENT_WRITE instead of stalling every other session)."""
        try:
            while s.wbuf:
                n = os.write(s.fd, s.wbuf)
                del s.wbuf[:n]
        except BlockingIOError:
            pass
        except OSError:
            s.wbuf.clear()
        want = selectors.EVENT_READ | (selectors.EVENT_WRITE if s.wbuf else 0)
        try:
            self.sel.modify(s.fd, want, ("pty", s))
        except (KeyError, ValueError):
            pass

    def _close_pty(self, s: Session) -> None:
        if s.fd in self.by_fd:
            self.sel.unregister(s.fd)
            del self.by_fd[s.fd]
            try:
                os.close(s.fd)
            except OSError:
                pass
        s.fd = -1   # the number may be reused by the next accept/spawn: never touch it again

    def _reap(self) -> None:
        for s in list(self.sessions.values()):
            if s.rc is not None or not s.pid or s.pid < 0:   # waitpid(0) would reap ANY child of the group
                continue
            try:
                pid, status = os.waitpid(s.pid, os.WNOHANG)
            except ChildProcessError:
                pid, status = s.pid, 0
            if pid == 0:
                continue
            # drain what the child wrote last before announcing the exit
            if s.fd in self.by_fd:
                while True:
                    try:
                        chunk = os.read(s.fd, READ_CHUNK)
                    except OSError:
                        break
                    if not chunk:
                        break
                    off = s.append(chunk)
                    for c in list(s.subscribers):
                        self._send(c, {"ev": "data", "id": s.id, "off": off,
                                       "data": base64.b64encode(chunk).decode()})
                self._close_pty(s)
            s.rc = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") \
                else (status >> 8)
            if s.log:
                s.log.close()
                s.log = None
            for c in list(s.subscribers | s.exit_only):
                self._send(c, {"ev": "exit", "id": s.id, "rc": s.rc})

    # ---------------------------------------------------------------- clients
    def _accept(self, srv) -> None:
        try:
            sock, _ = srv.accept()
        except BlockingIOError:
            return
        self.add_client(sock)

    def _drop(self, c: Client) -> None:
        if c not in self.clients:
            return
        self.clients.discard(c)
        for s in self.sessions.values():
            s.subscribers.discard(c)
            s.exit_only.discard(c)
            if s.tty is c:   # a closed tab or a crashed client: the others size the PTY again
                self._set_tty(s, None)
        try:
            self.sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        c.sock.close()

    def _set_tty(self, s: Session, c: Client | None) -> None:
        was = s.tty is not None
        s.tty = c
        if was != (c is not None):
            self._broadcast(s, {"ev": "tty", "id": s.id, "n": 1 if c is not None else 0})

    def _broadcast(self, s: Session, msg: dict) -> None:
        """To every client of the session, the exit-only (control) connections included."""
        for c in list(s.subscribers | s.exit_only):
            self._send(c, msg)

    def _read_client(self, c: Client) -> None:
        try:
            data = c.sock.recv(READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(c)
            return
        c.inbuf += data
        while len(c.inbuf) >= 4:
            (n,) = struct.unpack("!I", c.inbuf[:4])
            if n > MAX_FRAME:
                self._drop(c)
                return
            if len(c.inbuf) < 4 + n:
                break
            raw = bytes(c.inbuf[4:4 + n])
            del c.inbuf[:4 + n]
            req: dict = {}
            try:
                req = json.loads(raw)
                if not isinstance(req, dict):
                    raise ValueError("a request is a JSON object")
                reply = self._handle(c, req)
            except Exception as e:  # noqa: BLE001 - a bad request must not kill the host
                reply = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            if reply is not None:
                if isinstance(req, dict) and "rid" in req:
                    reply["rid"] = req["rid"]
                self._send(c, reply)

    def _send(self, c: Client, msg: dict) -> None:
        if c not in self.clients:
            return
        raw = json.dumps(msg, separators=(",", ":")).encode()
        c.outbuf += struct.pack("!I", len(raw)) + raw
        if len(c.outbuf) > 64 << 20:   # a stuck reader must not grow memory forever
            self._drop(c)
            return
        self._flush(c)

    def _flush(self, c: Client) -> None:
        try:
            while c.outbuf:
                n = c.sock.send(c.outbuf)
                del c.outbuf[:n]
        except BlockingIOError:
            pass
        except OSError:
            self._drop(c)
            return
        want = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.outbuf else 0)
        try:
            self.sel.modify(c.sock, want, ("client", c))
        except (KeyError, ValueError):
            pass

    def _session(self, req: dict) -> Session:
        sid = str(req.get("id", ""))
        if sid not in self.sessions:
            raise KeyError("unknown session %r" % sid)
        return self.sessions[sid]

    def _handle(self, c: Client, req: dict) -> dict | None:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "version": VERSION}
        if op == "spawn":
            return self._spawn(req)
        if op == "list":
            return {"ok": True, "sessions": [s.info() for s in self.sessions.values()]}
        s = self._session(req)
        if op == "input":
            if s.locked:
                return {"ok": False, "error": "locked"}
            if s.rc is not None or s.fd < 0:
                return {"ok": False, "error": "exited"}
            data = base64.b64decode(req.get("data", ""))
            if len(s.wbuf) + len(data) > 4 << 20:
                return {"ok": False, "error": "input backlog full"}
            if req.get("auto"):   # piperd's own input is never the human's
                s.typed.auto(data)
            else:
                s.typed.feed(data)
            s.wbuf += data
            self._write_pty(s)
            return {"ok": True}
        if op == "pending":   # when the human last touched the prompt (piperd decides what that means)
            return {"ok": True, "key_at": s.typed.key_at, "enter_at": s.typed.enter_at}
        if op in ("lock", "unlock"):
            s.locked = op == "lock"
            return {"ok": True}
        if op == "resize":
            if s.tty is not None and s.tty is not c:   # the native terminal sizes the PTY
                return {"ok": True, "ignored": "tty"}
            cols, rows = int(req["cols"]), int(req["rows"])
            changed = (cols, rows) != (s.cols, s.rows)
            s.cols, s.rows = cols, rows
            if changed:
                self._broadcast(s, {"ev": "size", "id": s.id, "cols": cols, "rows": rows})
            if s.rc is None and s.fd >= 0:
                _winsize(s.fd, s.cols, s.rows)
                try:
                    os.killpg(os.getpgid(s.pid), signal.SIGWINCH)
                except (ProcessLookupError, PermissionError):
                    pass
            return {"ok": True}
        if op == "signal":
            sig = SIGNALS.get(str(req.get("sig", "TERM")))
            if sig is None:
                return {"ok": False, "error": "unknown signal"}
            if s.rc is None:
                try:
                    os.kill(s.pid, sig)
                except ProcessLookupError:
                    pass
            return {"ok": True, "alive": s.rc is None}
        if op == "subscribe" and req.get("data") is False:
            s.subscribers.discard(c)
            s.exit_only.add(c)
            if s.rc is not None:
                self._send(c, {"ev": "exit", "id": s.id, "rc": s.rc})
            return {"ok": True, "info": s.info()}
        if op == "subscribe":
            if req.get("tty") and s.tty is not None and s.tty is not c:
                if not req.get("take"):
                    return {"ok": False, "error": "tty-busy"}
                old, s.tty = s.tty, c   # still one TTY: no `tty` event, the count did not change
                self._send(old, {"ev": "tty-taken", "id": s.id})   # first s.tty: a dead old socket drops without clearing it
            elif req.get("tty"):
                self._set_tty(s, c)
            elif s.tty is c:
                self._set_tty(s, None)
            s.exit_only.discard(c)
            frm = req.get("from", "safe")
            start = s.replay_from() if frm == "safe" else int(frm)
            start, data = s.slice(start)
            reply = {"ok": True, "info": s.info()}
            if "rid" in req:
                reply["rid"] = req["rid"]
            self._send(c, reply)
            if data:
                self._send(c, {"ev": "data", "id": s.id, "off": start,
                               "data": base64.b64encode(data).decode()})
            if s.rc is not None:
                self._send(c, {"ev": "exit", "id": s.id, "rc": s.rc})
            s.subscribers.add(c)   # also when exited: a resume under this id reaches the terminal
            return None
        if op == "unsubscribe":
            s.subscribers.discard(c)
            if s.tty is c:
                self._set_tty(s, None)
            return {"ok": True}
        if op == "forget":
            if s.rc is None:
                return {"ok": False, "error": "still running"}
            del self.sessions[s.id]
            return {"ok": True}
        return {"ok": False, "error": "unknown op %r" % op}


def main() -> None:
    home = os.environ.get("PIPER_HOME") or os.path.join(os.path.expanduser("~"), ".piper")
    path = os.environ.get("PIPER_PTY_SOCK") or os.path.join(home, "ptyhost.sock")
    sockdir = os.path.dirname(path)
    os.makedirs(sockdir, mode=0o700, exist_ok=True)
    st = os.lstat(sockdir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        sys.exit("ptyhost: %s is not a directory owned by this user - refusing to put the socket there" % sockdir)
    os.chmod(sockdir, 0o700)   # the socket is the whole access control: keep its dir private
    Host(path).serve()


if __name__ == "__main__":
    sys.exit(main())
