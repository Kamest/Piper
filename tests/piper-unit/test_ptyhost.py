"""ptyhost: spawn, input lock, replay from a safe point, resize, signal, exit.

Runs the host loop in a thread and talks to it over socket.socketpair(), so no socket
is ever bound (works inside sandboxes that forbid bind). The child is fake-hole.sh.
"""
import base64
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "piper"))

import _env  # noqa: E402,F401  (first: an isolated home before any Piper module)
import ptyclient  # noqa: E402
import ptyhost  # noqa: E402

FAKE = os.path.join(HERE, "fake-hole.sh")


def _pty_available():
    try:
        m, s = os.openpty()
    except OSError:
        return False
    os.close(m)
    os.close(s)
    return True


NEEDS_PTY = unittest.skipUnless(_pty_available(), "no PTY devices here (e.g. a sandbox denying /dev/ptmx)")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class PtyHostTest(unittest.TestCase):
    def test_geometry_replay_preserves_boundaries_and_legacy_subscribe(self):
        s = ptyhost.Session('geometry', 0, -1, 96, 47, None, False)
        s.rc = 0   # synthetic completed session: teardown must never signal pid 0
        self.host.sessions[s.id] = s
        s.append(b'wide')
        self.client.call('resize', id=s.id, cols=48, rows=47)
        s.append('narrow é'.encode())
        self.client.call('resize', id=s.id, cols=72, rows=30)
        events = []
        result = self._client(events).call('subscribe', id=s.id, replay_geometry=True)
        self.assertTrue(result['info']['replay_geometry'])
        self.assertTrue(wait_for(lambda: any(e['ev'] == 'exit' for e in events)))
        self.assertEqual([e['ev'] for e in events], ['size', 'data', 'size', 'data', 'size', 'replay-end', 'exit'])
        self.assertEqual([(e['cols'], e['rows']) for e in events if e['ev'] == 'size'], [(96,47),(48,47),(72,30)])
        self.assertEqual(self._output(events, "geometry"), b'wide' + 'narrow é'.encode())
        self.assertEqual([e['off'] for e in events if e['ev'] == 'data'], [0,4])
        legacy = []
        result = self._client(legacy).call('subscribe', id=s.id)
        self.assertNotIn('replay_geometry', result['info'])
        self.assertTrue(wait_for(lambda: any(e['ev'] == 'exit' for e in legacy)))
        self.assertEqual([e['ev'] for e in legacy], ['data','exit'])
        self.assertEqual(self._output(legacy, "geometry"), self._output(events, "geometry"))

    def test_geometry_safe_point_trim_and_future_offset(self):
        from unittest import mock
        s = ptyhost.Session('geometry',0,-1,96,47,None,False)
        s.append(b'obsolete\x1b[2J\x1b[H')
        s.resize(48,47)
        s.append(b'current')
        events = list(s.replay(s.replay_from()))
        self.assertEqual(events[0]['cols'], 48)
        self.assertEqual(self._output(events, "geometry"), b'current')
        self.assertEqual([e['ev'] for e in s.replay(1 << 62)], ['size','replay-end'])
        with mock.patch.object(ptyhost, 'BUFFER_CAP', 10), mock.patch.object(ptyhost, 'GEOMETRY_CAP', 3):
            for i in range(20):
                s.resize(50+i,30+i)
                s.append(b'xy')
            self.assertLessEqual(len(s.buf),10)
            self.assertLessEqual(len(s.geometry),3)
            self.assertLessEqual(s.geometry[0][0],s.base)
            self.assertEqual(self._output(list(s.replay(s.base)), "geometry"),bytes(s.buf))
            self.assertEqual(list(s.replay(s.base))[-2]['ev'], 'data')
        # Multiple resizes with no intervening output need only their final size.
        before = len(s.geometry)
        s.resize(101,40)
        s.resize(102,41)
        self.assertEqual(len(s.geometry),before+1)
        self.assertEqual(s.geometry[-1],(s.end,102,41))

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = ptyhost.Host()
        self.thread = threading.Thread(target=self.host.loop, daemon=True)
        self.thread.start()
        self.events = []
        self.client = self._client(self.events)
        self.env = {"PATH": os.environ["PATH"], "HOME": self.tmp, "TERM": "xterm-256color"}

    def tearDown(self):
        for s in list(self.host.sessions.values()):
            if s.rc is None:
                try:
                    os.kill(s.pid, 9)
                except ProcessLookupError:   # reaped between the rc check and the kill (CI 2026-10-12)
                    pass
        wait_for(lambda: all(s.rc is not None for s in self.host.sessions.values()), 3)
        self.host.running = False
        self.thread.join(3)

    def _client(self, sink):
        a, b = socket.socketpair()
        self.host.add_client(a)
        return ptyclient.PtyClient(None, on_event=sink.append, sock=b)

    def _spawn(self, sid="s1", locked=True, **env):
        e = dict(self.env, **env)
        r = self.client.call("spawn", id=sid, argv=[FAKE, "start", "claude", "/tmp/proj"],
                             env=e, cols=100, rows=30, locked=locked,
                             log=os.path.join(self.tmp, sid + ".log"))
        self.assertTrue(r["ok"], r)
        return r

    def _output(self, sink, sid="s1"):
        return b"".join(base64.b64decode(e["data"]) for e in sink
                        if e.get("ev") == "data" and e["id"] == sid)

    @NEEDS_PTY
    def test_spawn_sentinel_and_lock(self):
        self._spawn()
        sub = []
        c2 = self._client(sub)
        self.assertTrue(c2.call("subscribe", id="s1")["ok"])
        self.assertTrue(wait_for(lambda: b"fake claude ready" in self._output(sub)))
        self.assertIn(b"Attaching to claude agent...", self._output(sub))
        # locked: input refused by the host itself, not just by the GUI
        r = self.client.call("input", id="s1", data=ptyclient.b64(b"early\r"))
        self.assertEqual(r, {"ok": False, "error": "locked", "rid": r["rid"]})
        self.client.call("unlock", id="s1")
        self.assertTrue(self.client.call("input", id="s1", data=ptyclient.b64(b"hello\r"))["ok"])
        self.assertTrue(wait_for(lambda: b"got:hello" in self._output(sub)))
        self.assertNotIn(b"got:early", self._output(sub))

    def test_one_failing_event_does_not_end_the_host(self):
        """review 2026-10-05: an exception in one handler killed the loop - and with ptyhost every sandbox"""
        orig, boom = self.host._read_client, []

        def read_client(c):
            if not boom:
                boom.append(1)
                raise RuntimeError("bad frame")
            return orig(c)
        self.host._read_client = read_client
        bad = self._client([])
        self.assertRaises(ptyclient.PtyHostError, bad.call, "list", timeout=1)   # its connection is dropped
        self.assertTrue(self.thread.is_alive())
        self.assertTrue(self._client([]).call("list")["ok"])

    @NEEDS_PTY
    def test_shutdown_kills_a_child_that_ignores_term(self):
        """review 2026-10-05: a hole that drops the TERM (before it attaches) outlived ptyhost"""
        from unittest import mock
        # the trap must be in place before the TERM, so wait for "up". This test process is multi-threaded
        # (the Host loop runs in a thread), and a forkpty() child of a multi-threaded process can hang
        # before its exec (CPython warns about it); the real ptyhost is single-threaded. Twice a child
        # printed nothing for 15 s on a saturated machine (rounds 12/13): such a child is killed and
        # spawned again instead of failing the test.
        for _ in range(3):
            r = self.client.call("spawn", id="deaf", argv=["sh", "-c", "trap '' TERM HUP; echo up; while :; do sleep 1; done"],
                                 env=self.env, cols=80, rows=24, locked=False, log=os.path.join(self.tmp, "deaf.log"))
            self.assertTrue(r["ok"], r)
            pid = r["pid"]
            if wait_for(lambda: b"up" in bytes(self.host.sessions["deaf"].buf), 15):
                break
            os.kill(pid, 9)
            self.assertTrue(wait_for(lambda: self.host.sessions["deaf"].rc is not None, 5))
        else:
            self.fail("the child never printed 'up' in three spawns")
        with mock.patch.object(ptyhost, "SHUTDOWN_WAIT", 0.5):
            self.host._shutdown()
        self.assertTrue(wait_for(lambda: self.host.sessions["deaf"].rc is not None or not _alive(pid), 3))

    @NEEDS_PTY
    def test_replay_starts_at_safe_point(self):
        self._spawn(locked=False)
        self.assertTrue(wait_for(lambda: self.host.sessions["s1"].end > 0 and
                                 b"ready" in bytes(self.host.sessions["s1"].buf)))
        s = self.host.sessions["s1"]
        # fake-hole clears the screen (ESC[2J ESC[H) right before "fake claude ready"
        self.assertGreater(s.replay_from(), 0)
        sub = []
        c2 = self._client(sub)
        c2.call("subscribe", id="s1")
        self.assertTrue(wait_for(lambda: b"ready" in self._output(sub)))
        out = self._output(sub)
        self.assertTrue(out.startswith(b"fake claude ready"), out[:80])
        self.assertNotIn(b"Launching sandbox", out)
        # explicit offset 0 gets the whole history
        sub0 = []
        self._client(sub0).call("subscribe", id="s1", **{"from": 0})
        self.assertTrue(wait_for(lambda: b"Launching sandbox" in self._output(sub0)))

    def test_ring_buffer_trim_keeps_safe_points_valid(self):
        s = ptyhost.Session("x", 0, -1, 80, 24, None, False)
        old = ptyhost.BUFFER_CAP
        ptyhost.BUFFER_CAP = 64
        try:
            s.append(b"a" * 40 + b"\x1b[3J")
            s.append(b"b" * 40)
            s.append(b"c" * 40)
            self.assertLessEqual(len(s.buf), 64)
            self.assertGreaterEqual(s.replay_from(), s.base)
            frm, data = s.slice(s.replay_from())
            self.assertEqual(frm, s.replay_from())
            self.assertTrue(set(data) <= set(b"bc"))
        finally:
            ptyhost.BUFFER_CAP = old

    def test_replay_after_a_final_clear_is_empty(self):
        s = ptyhost.Session("x", 0, -1, 80, 24, None, False)
        s.append(b"old screen\x1b[3J")
        self.assertEqual(s.slice(s.replay_from())[1], b"")

    @NEEDS_PTY
    def test_respawn_keeps_the_open_terminal(self):
        self._spawn(locked=False)
        sub = []
        c2 = self._client(sub)
        c2.call("subscribe", id="s1")
        self.client.call("signal", id="s1", sig="TERM")
        self.assertTrue(wait_for(lambda: any(e.get("ev") == "exit" for e in sub)))
        self._spawn(locked=False)   # retry/resume under the same id
        self.assertTrue(wait_for(lambda: any(e.get("ev") == "respawn" for e in sub)))
        self.client.call("input", id="s1", data=ptyclient.b64(b"again\r"))
        self.assertTrue(wait_for(lambda: b"got:again" in self._output(sub)))

    @NEEDS_PTY
    def test_exit_only_subscription_gets_no_stream(self):
        self._spawn(locked=False)
        ev = []
        c2 = self._client(ev)
        c2.call("subscribe", id="s1", data=False)
        self.client.call("input", id="s1", data=ptyclient.b64(b"x\r"))
        time.sleep(0.5)
        self.assertFalse([e for e in ev if e.get("ev") == "data"])
        self.client.call("signal", id="s1", sig="TERM")
        self.assertTrue(wait_for(lambda: any(e.get("ev") == "exit" for e in ev)))

    @NEEDS_PTY
    def test_large_input_does_not_block_the_loop(self):
        self._spawn(locked=False)
        t0 = time.time()
        r = self.client.call("input", id="s1", data=ptyclient.b64(b"y" * 300000 + b"\r"))
        self.assertTrue(r["ok"])
        self.assertLess(time.time() - t0, 1.5)
        self.assertTrue(self.client.call("list")["ok"])   # the loop still answers

    def test_marker_split_across_reads(self):
        s = ptyhost.Session("x", 0, -1, 80, 24, None, False)
        s.append(b"xxxx\x1b[")
        s.append(b"3Jyyyy")
        self.assertEqual(s.safe, [0, 8])   # right after the split ESC[3J (bytes 4..7)

    @NEEDS_PTY
    def test_signal_term_exits_and_reports(self):
        self._spawn(locked=False)
        sub = []
        c2 = self._client(sub)
        c2.call("subscribe", id="s1")
        self.assertTrue(wait_for(lambda: b"ready" in self._output(sub)))
        self.client.call("signal", id="s1", sig="TERM")
        self.assertTrue(wait_for(lambda: any(e.get("ev") == "exit" for e in sub)))
        self.assertIn(b"Sandbox destroyed", self._output(sub))
        info = self.client.call("list")["sessions"][0]
        self.assertFalse(info["alive"])
        self.assertEqual(info["rc"], 0)
        self.assertTrue(self.client.call("forget", id="s1")["ok"])

    @NEEDS_PTY
    def test_resize_and_unknown(self):
        self._spawn(locked=False)
        self.assertTrue(self.client.call("resize", id="s1", cols=140, rows=40)["ok"])
        self.assertEqual(self.host.sessions["s1"].cols, 140)
        r = self.client.call("input", id="nope", data="")
        self.assertFalse(r["ok"])
        self.assertIn("unknown session", r["error"])
        self.assertFalse(self.client.call("signal", id="s1", sig="BOGUS")["ok"])

    # ---- TTY clients: a native terminal attached with `piper attach`
    def _evs(self, sink, kind):
        return [e for e in sink if e.get("ev") == kind]

    @NEEDS_PTY
    def test_tty_size_wins_over_the_gui(self):
        self._spawn(locked=False)
        ctl, gui, tty = [], [], []
        self._client(ctl).call("subscribe", id="s1", data=False)   # piperd's control connection
        g = self._client(gui)
        g.call("subscribe", id="s1")
        t = self._client(tty)
        r = t.call("subscribe", id="s1", tty=True, **{"from": self.host.sessions["s1"].end})
        self.assertTrue(r["ok"])
        self.assertEqual(r["info"]["tty"], 1)
        self.assertTrue(wait_for(lambda: self._evs(gui, "tty") and self._evs(ctl, "tty")))
        self.assertEqual(self._evs(gui, "tty")[-1]["n"], 1)
        self.assertTrue(t.call("resize", id="s1", cols=180, rows=50)["ok"])
        r = g.call("resize", id="s1", cols=90, rows=25)
        self.assertEqual(r.get("ignored"), "tty")   # the browser does not resize the terminal's PTY
        s = self.host.sessions["s1"]
        self.assertEqual((s.cols, s.rows), (180, 50))
        self.assertTrue(wait_for(lambda: self._evs(gui, "size") and self._evs(ctl, "size")))
        self.assertEqual({(e["cols"], e["rows"]) for e in self._evs(gui, "size")}, {(180, 50)})
        # the same size again: no event (the terminal sends SIGWINCH without a change)
        n = len(self._evs(gui, "size"))
        t.call("resize", id="s1", cols=180, rows=50)
        time.sleep(0.2)
        self.assertEqual(len(self._evs(gui, "size")), n)
        # detach: the browser sizes the PTY again
        self.assertTrue(t.call("unsubscribe", id="s1")["ok"])
        self.assertTrue(wait_for(lambda: self._evs(gui, "tty")[-1]["n"] == 0))
        self.assertNotIn("ignored", g.call("resize", id="s1", cols=90, rows=25))
        self.assertEqual((s.cols, s.rows), (90, 25))
        self.assertEqual(self.client.call("list")["sessions"][0]["tty"], 0)

    @NEEDS_PTY
    def test_second_tty_is_busy_unless_it_takes_over(self):
        self._spawn(locked=False)
        first, second = [], []
        a = self._client(first)
        self.assertTrue(a.call("subscribe", id="s1", tty=True)["ok"])
        b = self._client(second)
        r = b.call("subscribe", id="s1", tty=True)
        self.assertEqual((r["ok"], r.get("error")), (False, "tty-busy"))
        self.client.call("input", id="s1", data=ptyclient.b64(b"x\r"))
        time.sleep(0.3)
        self.assertFalse(self._evs(second, "data"))
        self.assertTrue(b.call("subscribe", id="s1", tty=True, take=True)["ok"])
        self.assertTrue(wait_for(lambda: self._evs(first, "tty-taken")))
        self.assertEqual(a.call("resize", id="s1", cols=70, rows=20).get("ignored"), "tty")
        self.assertNotIn("ignored", b.call("resize", id="s1", cols=150, rows=45))
        self.assertIsNotNone(self.host.sessions["s1"].tty)

    @NEEDS_PTY
    def test_take_over_from_a_dead_terminal_keeps_the_new_one(self):
        self._spawn(locked=False)
        gui = []
        self._client(gui).call("subscribe", id="s1")
        a = self._client([])
        a.call("subscribe", id="s1", tty=True)
        a.close()   # its socket dies; `take` may come before ptyhost noticed
        b = self._client([])
        self.assertTrue(b.call("subscribe", id="s1", tty=True, take=True)["ok"])
        time.sleep(0.3)
        self.assertIsNotNone(self.host.sessions["s1"].tty)
        self.assertEqual(self.client.call("resize", id="s1", cols=60, rows=20).get("ignored"), "tty")
        self.assertEqual(self._evs(gui, "tty")[-1]["n"], 1)

    @NEEDS_PTY
    def test_crashed_tty_client_releases_the_size(self):
        self._spawn(locked=False)
        gui = []
        g = self._client(gui)
        g.call("subscribe", id="s1")
        t = self._client([])
        t.call("subscribe", id="s1", tty=True)
        self.assertTrue(wait_for(lambda: self._evs(gui, "tty")))
        t.close()   # the tab was closed / the client died: ptyhost drops the connection
        self.assertTrue(wait_for(lambda: self._evs(gui, "tty")[-1]["n"] == 0))
        self.assertIsNone(self.host.sessions["s1"].tty)
        self.assertNotIn("ignored", g.call("resize", id="s1", cols=90, rows=25))

    @NEEDS_PTY
    def test_tty_survives_a_respawn(self):
        self._spawn(locked=False)
        tty = []
        t = self._client(tty)
        t.call("subscribe", id="s1", tty=True)
        self.client.call("signal", id="s1", sig="TERM")
        self.assertTrue(wait_for(lambda: self._evs(tty, "exit")))
        self._spawn(locked=False)   # Continue from the GUI under the same id
        self.assertTrue(wait_for(lambda: self._evs(tty, "respawn")))
        self.assertIsNotNone(self.host.sessions["s1"].tty)
        self.assertEqual(self.client.call("resize", id="s1", cols=60, rows=20).get("ignored"), "tty")

    def test_version(self):
        self.assertGreaterEqual(ptyhost.VERSION, 8)

    @NEEDS_PTY
    def test_log_file_written(self):
        self._spawn(locked=False)
        self.assertTrue(wait_for(lambda: b"ready" in open(os.path.join(self.tmp, "s1.log"), "rb").read()))


class ConnectAutostartTest(unittest.TestCase):
    def test_autostart_creates_the_socket_dir(self):
        """common._pty_sock falls back to a short temp dir nobody created yet: connect() must
        create it before it opens ptyhost.log there (piperd died with FileNotFoundError)."""
        from unittest import mock
        d = os.path.join(tempfile.mkdtemp(), "not-yet")
        with mock.patch.object(ptyclient.subprocess, "Popen", side_effect=RuntimeError("spawned")):
            with self.assertRaisesRegex(RuntimeError, "spawned"):
                ptyclient.connect(os.path.join(d, "ptyhost.sock"))
        self.assertTrue(os.path.isfile(os.path.join(d, "ptyhost.log")))
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
