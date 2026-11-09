"""Manager fixtures must own and release their background work, without killing PTYs."""
import threading
import os
import subprocess
import sys
import unittest
from unittest import mock

import _env
import sessions


class LifecycleTest(unittest.TestCase):
    def log_capture(self, manager):
        s = sessions.Session(kind='advisor', state='attesting', instance='fixture')
        manager.sessions[s.id] = s
        os.makedirs(s.dir, exist_ok=True)
        started, children = threading.Event(), []
        original = subprocess.Popen
        def spawn(argv, **kwargs):
            if argv[:2] != [sessions.DOCKER_BIN, 'logs']:
                return original(argv, **kwargs)
            child = original([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
            children.append(child)
            started.set()
            return child
        self.enterContext(mock.patch.object(sessions.subprocess, 'Popen', side_effect=spawn))
        def cleanup():
            s.state = 'exited'
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(3)
        self.addCleanup(cleanup)
        return s, started, children

    def test_capture_deadline_stops_and_reaps_a_child_that_keeps_streaming(self):
        m = _env.manager(self)
        s, started, children = self.log_capture(m)
        with mock.patch.object(m, 'AGENT_LOG_TIMEOUT', 0.1, create=True):
            thread = threading.Thread(target=m._capture_agent_log, args=(s,))
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                thread.join(1)
                self.assertFalse(thread.is_alive(), 'deadline must apply while docker logs is still running')
                self.assertIsNotNone(children[0].poll())
            finally:
                s.state = 'exited'
                thread.join(2)

    def test_close_reaps_active_log_services_without_stopping_the_ptyhost(self):
        m = _env.manager(self, background=True)
        s, started, children = self.log_capture(m)
        thread = m._start_service(m._capture_agent_log, 'fixture-log', (s,))
        try:
            self.assertTrue(started.wait(2))
            m.close(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(children[0].poll())
            self.assertTrue(self.ph.thread.is_alive())
        finally:
            s.state = 'exited'
            thread.join(2)

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
