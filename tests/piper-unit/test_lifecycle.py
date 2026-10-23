"""Manager fixtures must own and release their background work, without killing PTYs."""
import threading
import os
import unittest
from unittest import mock

import _env
import sessions


class LifecycleTest(unittest.TestCase):
    def test_passive_manager_never_connects_or_starts_threads(self):
        connect = mock.Mock(side_effect=AssertionError('passive manager connected'))
        before = set(threading.enumerate())
        m = sessions.Manager(connect_fn=connect, background=False)
        try:
            self.assertEqual(set(threading.enumerate()), before)
            self.assertIsNone(m.pty)
            m._sync_watch()
            self.assertEqual(set(threading.enumerate()), before)
        finally:
            m.close()
        connect.assert_not_called()

    def test_close_joins_services_and_does_not_reconnect(self):
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        connect = mock.Mock(side_effect=lambda event, close: ph.client(event, close))
        before = set(threading.enumerate())
        m = sessions.Manager(connect_fn=connect)
        m.close()
        m.close()
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(set(threading.enumerate()) - before, set())
        self.assertTrue(ph.thread.is_alive(), 'manager close must preserve PTY host')

    def test_pty_fixture_stop_joins_reader_and_host(self):
        before = set(threading.enumerate())
        ph = _env.InThreadPtyHost()
        client = ph.client()
        self.assertEqual(client.call('list')['sessions'], [])
        ph.stop()
        ph.stop()
        self.assertEqual(set(threading.enumerate()), before)

    def test_close_preserves_a_live_pty_child(self):
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        client = ph.client()
        client.call('spawn', id='survivor', argv=['/bin/sh', '-c', 'read line'], env=dict(os.environ), cols=80, rows=24)
        m = sessions.Manager(connect_fn=lambda event, close: ph.client(event, close))
        m.close()
        self.assertTrue(client.call('list')['sessions'][0]['alive'])

    def test_session_cleanup_reaps_children_and_clears_state(self):
        m = _env.manager(self, background=True)
        s = sessions.Session(project='fixture', state='working')
        m.sessions[s.id] = s
        m.pty.call('spawn', id=s.id, argv=['/bin/sh', '-c', 'read line'], env=dict(os.environ), cols=80, rows=24)
        _env.clear_sessions(m)
        self.assertEqual(m.sessions, {})
        self.assertEqual(m.pty.call('list')['sessions'], [])
        self.assertFalse(os.path.exists(s.dir))

    def test_long_interval_services_are_interruptible(self):
        import inbox, prodcheck, feed
        before = set(threading.enumerate())
        ph = _env.InThreadPtyHost()
        with mock.patch.object(inbox, 'INTERVAL', 600), mock.patch.object(inbox, 'FIRST_POLL', 600), \
                mock.patch.object(prodcheck, 'TICK', 600), mock.patch.object(prodcheck, 'FIRST_TICK', 600), \
                mock.patch.object(feed, 'TICK', 600):
            m = sessions.Manager(connect_fn=lambda event, close: ph.client(event, close))
            m.close()
        ph.stop()
        self.assertEqual(set(threading.enumerate()), before)

    def test_parser_modules_do_not_import_the_session_runtime(self):
        import subprocess
        import sys
        modules = ['test_symbols', 'test_timesheet', 'test_feed', 'test_diagnostics',
                   'test_httpclient', 'test_input', 'test_issuestate', 'test_ideamcp',
                   'test_prodcheck', 'test_dbtools']
        code = 'import sys,importlib; sys.path.insert(0,sys.argv[1]);\nfor n in sys.argv[2:]:\n importlib.import_module(n)\n assert "sessions" not in sys.modules, n'
        result = subprocess.run([sys.executable, '-c', code, os.path.dirname(__file__), *modules],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pty_cleanup_also_closes_clients_after_the_loop_already_ended(self):
        before = set(threading.enumerate())
        ph = _env.InThreadPtyHost()
        self.addCleanup(ph.stop)
        ph.client().call('list')
        ph.host.running = False
        ph.thread.join(3)
        self.assertFalse(ph.thread.is_alive())
        ph.stop()
        self.assertEqual(set(threading.enumerate()), before)
