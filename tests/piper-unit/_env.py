"""Shared test environment: a throwaway $PIPER_HOME / home / PROJECT_PATH, set BEFORE any
Piper module is imported (common.py reads the environment at import time)."""
import atexit
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

# A Piper module imported before this one has already read the REAL home (common.py reads the
# environment at import time) - and the tests' cleanups rmtree REAL_HOME/.claude, .ssh, .m2 (incident
# 2026-10-12). Refuse instead of running on. common.py refuses on its own as well.
_PIPER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper")
# first of all: only inside the OS sandbox of scripts/piper-sandbox.py
sys.path.insert(0, _PIPER_DIR)
import testsandbox  # noqa: E402
testsandbox.require()
sys.path.remove(_PIPER_DIR)
del sys.modules["testsandbox"]
_early = sorted(n[:-3] for n in os.listdir(_PIPER_DIR) if n.endswith(".py") and n[:-3] in sys.modules)
if _early:
    raise RuntimeError("tests/piper-unit/_env.py must be imported before any Piper module; already "
                       "imported: %s - add `import _env` at the top of the test file" % ", ".join(_early))

# the core suite runs Piper alone (test_boundary.py): never the extensions of the shell's piperd; an
# extension's own tests set PIPER_EXTENSIONS after importing this (extensions load lazily)
os.environ.pop("PIPER_EXTENSIONS", None)

ROOT = tempfile.mkdtemp(prefix="piper-test-")
# a free loopback port per process: run_parallel.py runs modules side by side (test_piperd binds it)
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    _PORT = _s.getsockname()[1]
atexit.register(shutil.rmtree, ROOT, True)
HOME = os.path.join(ROOT, "home")
PP = os.path.join(ROOT, "projects")
for d in (HOME, os.path.join(HOME, ".hole"), os.path.join(HOME, ".claude"), PP):
    os.makedirs(d, exist_ok=True)
os.environ.update({
    "PIPER_HOME": os.path.join(ROOT, "piper"),
    "PIPER_REAL_HOME": HOME,
    "PIPER_TEST": "1",   # common refuses the real home and writes outside the temp dirs, here and in children
    "HOME": HOME,   # expanduser("~") too: nothing a test runs may reach the real home
    "PROJECT_PATH": PP,
    "PIPER_PTY_SOCK": os.path.join(ROOT, "piper", "ptyhost.sock"),
    "PIPER_HOLE_BIN": os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-hole.sh"),
    "PIPER_DOCKER_BIN": os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-docker.py"),
    "PIPER_PORT": str(_PORT),
    "PIPER_INBOX_INTERVAL": "0",   # no background GitLab poll: tests drive Manager._inbox_poll()
    "PIPER_PROD_TICK": "0",        # no background production check: tests drive Manager._prod_tick()
    "PIPER_FEED_TICK": "0",        # no background Feed tick: tests drive Manager._feed_tick()
    "PIPER_DESKTOP_NOTIFY": "0",   # never the host's notification centre from a test
    "PIPER_SECURITY_TREE": "0",    # the suite asserts the guarded policy; test_security switches the tree on (9.46)
})
# a git identity in the fake home (as CI's `git config --global`); ~/.config/git/config, not ~/.gitconfig,
# which test_git_mode_hole_copies_the_host_gitconfig writes and removes
os.makedirs(os.path.join(HOME, ".config", "git"), exist_ok=True)
with open(os.path.join(HOME, ".config", "git", "config"), "w") as f:
    f.write("[user]\n\tname = Piper Test\n\temail = piper-test@example.invalid\n")
for _k in ("GITLAB_TOKEN", "GITLAB_PAT"):   # never the user's real token in a test
    os.environ.pop(_k, None)
with open(os.path.join(HOME, ".hole", "settings.json"), "w") as f:
    json.dump({"files": {"exclude": ["config/secrets"]},
               "environment": {"GITLAB_TOKEN": "$GITLAB_PAT"},
               "network": {"allow": ["gitlab.com", "nexus.example.com"]}}, f)

# every feature module on: the tests of a module must not depend on the preset a
# brand-new home gets; test_features.py covers the presets themselves
os.makedirs(os.environ["PIPER_HOME"], exist_ok=True)
with open(os.path.join(os.environ["PIPER_HOME"], "features.json"), "w") as f:
    json.dump({"base": "all", "onboarded": True, "modules": {}, "v": 2}, f)   # v: past the D10 migration

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper"))

import common  # noqa: E402

common.ensure_home()


def pty_available() -> bool:
    try:
        m, s = os.openpty()
    except OSError:
        return False
    os.close(m)
    os.close(s)
    return True


class InThreadPtyHost:
    """ptyhost's loop in a thread, reachable through socketpairs (no bind needed)."""

    def __init__(self):
        import ptyhost
        self.clients = []
        self._closed = False
        self.host = ptyhost.Host()
        self.thread = threading.Thread(target=self.host.loop, daemon=True)
        self.thread.start()

    def client(self, on_event=None, on_close=None):
        import ptyclient
        a, b = socket.socketpair()
        self.host.add_client(a)
        c = ptyclient.PtyClient(None, on_event=on_event, on_close=on_close, sock=b)
        self.clients.append(c)
        return c

    def stop(self):
        if self._closed:
            return
        for c in self.clients:
            c.close()
        for s in list(self.host.sessions.values()):
            if s.rc is None:
                try:
                    os.kill(s.pid, 9)
                except ProcessLookupError:
                    pass
        self.host.running = False
        self.thread.join(3)
        if self.thread.is_alive():
            raise RuntimeError("test PTY host did not stop")
        self.host._shutdown()
        for c in list(self.host.clients):
            self.host._drop(c)
        self.host.sel.close()
        self._closed = True


def manager(owner, *, background=False, pty=False):
    """An owned fixture: no runtime by default, real PTY/services for integration tests."""
    import sessions
    cleanup = owner.addClassCleanup if isinstance(owner, type) else owner.addCleanup
    if background or pty:
        ph = InThreadPtyHost()
        cleanup(ph.stop)
        m = sessions.Manager(connect_fn=lambda event, close: ph.client(event, close), background=background)
        if not background:
            m.pty = ph.client()
        owner.ph = ph
    else:
        m = sessions.Manager(background=False)
    cleanup(m.close)  # LIFO: close the manager before its PTY host
    return m


def make_project(name: str, files: dict) -> str:
    root = os.path.join(PP, "team-x", name)
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return root


def register(key: str, path: str, **extra):
    data = common.read_json(common.PROJECTS_FILE, {"projects": {}})
    data.setdefault("projects", {})[key] = dict({"path": path}, **extra)
    common.write_json(common.PROJECTS_FILE, data)


def set_modules(tc, **mods):
    """switch feature modules for one test (D10: a module is the feature's on/off), restored afterwards"""
    import features
    saved = open(features.FILE).read()

    def back():
        with open(features.FILE, "w") as f:
            f.write(saved)
        features._CACHE.clear()
    tc.addCleanup(back)
    features.save(mods)


def clear_sessions(m):
    """Release this test's sessions through its private PTY connection, never host PIDs."""
    for session in list(m.sessions.values()):
        with session.cancel_lock:
            session.cancel = True
        session.state = "exited"
        session.rc = session.rc if session.rc is not None else -9
    # Starts may still be preparing the fake process. Wait for their serialized spawn section.
    if not m._spawn_lock.acquire(timeout=10):
        raise AssertionError("test left a session start running")
    m._spawn_lock.release()
    for pending in list(m.pending.values()):
        pending["_cancelled"] = "test cleanup"
        for value in pending.values():
            if isinstance(value, threading.Event):
                value.set()
    if m.pty is not None:
        for row in m.pty.call("list")["sessions"]:
            if row["alive"]:
                m.pty.call("signal", id=row["id"], sig="KILL")
        deadline = time.monotonic() + 5
        while any(row["alive"] for row in m.pty.call("list")["sessions"]):
            if time.monotonic() > deadline:
                raise AssertionError("test left a fake-hole process running")
            time.sleep(0.01)
        for row in m.pty.call("list")["sessions"]:
            m.pty.call("forget", id=row["id"])
    for session in list(m.sessions.values()):
        shutil.rmtree(session.dir, ignore_errors=True)
    m.sessions.clear()
    m.pending.clear()
