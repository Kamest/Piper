"""Host terminals: a login shell on the host, without a sandbox, in the GUI's
Terminal view - what IDEA's ⌥F12 is next to its editor.

Opt-in (feature module `host`, off by default and in no preset, never proposable by an agent: settings.AGENT_DENY) and the
GUI's own: piperd opens, reads and types into one only for the GUI's full-scope token, never for `piper
attach`'s tty token, a scoped token or a session's MCP (piper-mcp.py has no tool for it). Every open, close
and exit is audited with the project and the folder - never what is typed or printed (no pty.log either).

The shell runs in ptyhost like a session (it survives a piperd restart) under the id `host-<hex>`;
piperd keeps only the list (host-terminals.json in $PIPER_HOME) and the exit codes. It does not count
towards max_sessions, has no Session object and no state machine: a shell is either running or exited.
"""
from __future__ import annotations

import os
import pwd
import secrets
import threading
import time

import common

PREFIX = "host-"
MAX_OPEN = 12          # running host shells at once
KEEP_EXITED = 8        # exited ones kept in the list (their output stays in ptyhost until closed)
# the shell's environment: piperd's own (tokens, PIPER_*, the GitLab token...) never leaks into it
ENV_PASS = ("USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SSH_AUTH_SOCK")
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def is_host_id(tid: str) -> bool:
    return isinstance(tid, str) and tid.startswith(PREFIX)


def login_shell() -> str:
    """$SHELL when it is an absolute executable, else the passwd entry's, else /bin/zsh."""
    for sh in (os.environ.get("SHELL", ""), _pw_shell()):
        if sh and os.path.isabs(sh) and os.access(sh, os.X_OK):
            return sh
    return "/bin/zsh"


def _pw_shell() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_shell
    except (KeyError, OSError):
        return ""


def _user() -> str:
    """who the shell runs as (the GUI's HOST banner)"""
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER", "")


def shell_env(shell: str) -> dict:
    env = {k: os.environ[k] for k in ENV_PASS if os.environ.get(k)}
    env.update({"HOME": common.REAL_HOME, "SHELL": shell, "PATH": os.environ.get("PATH") or DEFAULT_PATH,
                "TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "Piper",
                "PIPER_HOST_TERMINAL": "1"})
    env.setdefault("LANG", "en_US.UTF-8")
    return env


class HostTerminals:
    """The host shells of one piperd. `pty()` returns the Manager's current PtyClient (it reconnects)."""

    def __init__(self, pty, bump, enabled, state_file: str | None = None):
        self._pty, self._bump, self._enabled = pty, bump, enabled
        self.file = state_file or os.path.join(common.PIPER_HOME, "host-terminals.json")
        self.lock = threading.Lock()
        self.items: list[dict] = self._load()

    # ------------------------------------------------------------ state
    def _load(self) -> list[dict]:
        data = common.read_json(self.file, [])
        return [x for x in data if isinstance(x, dict) and is_host_id(x.get("id"))] if isinstance(data, list) else []

    def _save(self) -> None:
        common.write_json(self.file, self.items)

    def enabled(self) -> bool:
        return bool(self._enabled())

    def get(self, tid: str) -> dict | None:
        return next((x for x in self.items if x["id"] == tid), None)

    def summary(self) -> dict:
        """/api/state `host_terms`: the flag and the list (no output, no environment)."""
        with self.lock:
            rows = [{k: x.get(k) for k in ("id", "project", "path", "title", "created", "rc", "alive", "ended")}
                    for x in self.items]
        return {"on": self.enabled(), "user": _user(), "items": rows}

    # ------------------------------------------------------------ actions
    def open(self, project: dict, cols: int = 120, rows: int = 32) -> dict:
        """A login shell in the project's folder. `project` = projects.find()'s dict (key, path, title)."""
        if not self.enabled():
            raise PermissionError("the host terminal is off (Settings → Terminal → Host terminal)")
        path = os.path.realpath(project["path"])
        if not os.path.isdir(path):
            raise ValueError("project checkout %s does not exist" % path)
        with self.lock:
            if sum(1 for x in self.items if x.get("alive")) >= MAX_OPEN:
                raise ValueError("%d host shells are open - close one first" % MAX_OPEN)
            tid = PREFIX + secrets.token_hex(4)
            shell = login_shell()
            cols = max(10, min(int(cols or 120), 1000))
            rows = max(3, min(int(rows or 32), 500))
            r = self._pty().call("spawn", id=tid, argv=[shell, "-l"], env=shell_env(shell), cwd=path,
                                 cols=cols, rows=rows, locked=False)   # no log: a host shell's output stays in memory
            if not r.get("ok"):
                raise RuntimeError("spawn failed: %s" % r.get("error"))
            self._pty().call("subscribe", id=tid, data=False)   # the exit event only
            n = 1 + sum(1 for x in self.items if x.get("project") == project["key"])
            item = {"id": tid, "project": project["key"], "path": path, "created": time.time(), "rc": None,
                    "alive": True, "title": "%s%s" % (project.get("title") or project["key"], " %d" % n if n > 1 else "")}
            self.items.append(item)
            self._trim()
            self._save()
        common.audit({"event": "host-terminal-open", "terminal": tid, "project": project["key"], "cwd": path,
                      "shell": shell, "pid": r.get("pid")})
        self._bump()
        return dict(item)

    def close(self, tid: str) -> bool:
        """Close = HUP the shell (as closing a terminal window does) and drop it from the list."""
        with self.lock:
            item = self.get(tid)
            if item is None:
                return False
            self.items.remove(item)
            self._save()
        pty = self._pty()
        try:
            if item.get("alive"):
                pty.call("signal", id=tid, sig="HUP")
                threading.Thread(target=self._kill_later, args=(tid,), daemon=True).start()
            else:
                pty.call("forget", id=tid)
        except Exception:  # noqa: BLE001 - ptyhost gone: nothing left to close
            pass
        common.audit({"event": "host-terminal-close", "terminal": tid, "project": item.get("project"),
                      "cwd": item.get("path")})
        self._bump()
        return True

    def _kill_later(self, tid: str, wait: float = 3.0) -> None:
        """A shell that ignores HUP (a job holding it) gets KILL; then its buffer is dropped."""
        time.sleep(wait)
        try:
            pty = self._pty()
            info = next((x for x in pty.call("list").get("sessions") or [] if x.get("id") == tid), None)
            if info and info.get("alive"):
                pty.call("signal", id=tid, sig="KILL")
                time.sleep(0.3)
            pty.call("forget", id=tid)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ ptyhost
    def on_event(self, ev: dict) -> None:
        """Manager._on_pty_event for an id that is not a session's."""
        if ev.get("ev") != "exit" or not is_host_id(ev.get("id", "")):
            return
        with self.lock:
            item = self.get(ev["id"])
            if item is None or not item.get("alive"):
                return
            item["alive"], item["rc"], item["ended"] = False, ev.get("rc"), time.time()   # D5: the GUI's auto-close
            self._save()
        common.audit({"event": "host-terminal-exit", "terminal": item["id"], "project": item.get("project"),
                      "rc": ev.get("rc")})
        self._bump()

    def reconcile(self, alive: dict) -> None:
        """After (re)connecting to ptyhost: `alive` = id -> list() info. A shell ptyhost no longer has is gone
        (ptyhost restarted); a running one gets its exit subscription again."""
        changed = False
        with self.lock:
            for item in self.items:
                info = alive.get(item["id"])
                if info and info.get("alive"):
                    try:
                        self._pty().call("subscribe", id=item["id"], data=False)
                    except Exception:  # noqa: BLE001
                        pass
                    if not item.get("alive"):
                        item["alive"], changed = True, True
                        item.pop("ended", None)
                elif item.get("alive"):
                    item["alive"], changed = False, True
                    item["rc"] = info.get("rc") if info else None
                    item["ended"] = time.time()
            # an exited one ptyhost dropped (restart) is only a dead row
            if changed:
                self._save()
        if changed:
            self._bump()

    def _trim(self) -> None:
        dead = [x for x in self.items if not x.get("alive")]
        for x in dead[:max(0, len(dead) - KEEP_EXITED)]:
            self.items.remove(x)
            try:
                self._pty().call("forget", id=x["id"])
            except Exception:  # noqa: BLE001
                pass
