#!/usr/bin/env python3
"""piper attach - a Piper session in a native terminal (Ghostty, iTerm, Terminal).

  piper attach                   pick a live session (a number), or start one in this directory's project
  piper attach <id|project|.>    attach; a project without a live session gets a new one (`.` = this
                                 directory's project, a project may be named by its last path part)
  piper attach <project> --new   always a new session
  piper attach -l                list the live sessions

  --replay   replay the recent output first (it was drawn at the PTY's old size)
  --take     take the session over from another terminal attached to it

Ctrl-] detaches; the session keeps running and stays in the GUI. While attached, this terminal
sizes the PTY: the GUI shows the session's Conversation (its terminal scrolls), typing there
still works. Sessions are created by piperd only (preflight, Guard, attestation, audit as from
the GUI); the stream comes from ptyhost (v8: TTY clients), whose input lock this cannot bypass.
"""
from __future__ import annotations

import base64
import glob
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from ptyclient import PtyClient, PtyHostError, b64  # noqa: E402

USAGE = __doc__.split("\n\n")[1] + "\n\n" + __doc__.split("\n\n")[2]   # the commands and the flags
PTY_VERSION = 8           # ptyhost with TTY clients (subscribe tty, resize priority)
PORT = int(os.environ.get("PIPER_PORT", "17895"))
BASE = "http://127.0.0.1:%d" % PORT
DETACH = b"\x1d"          # Ctrl-]
PIPER_SH = os.path.join(common.SK, "scripts", "piper.sh")
# the states in which a session has a terminal worth attaching to (sessions.ACTIVE minus the
# ones before the spawn); `alive` in /api/state says whether it runs at all
STARTING = ("preflight", "preparing")
WORDS = {"preflight": "checking", "preparing": "preparing", "spawning": "starting", "trust": "trust prompt",
         "starting": "starting", "attesting": "checking isolation", "ready": "ready", "working": "working",
         "waiting": "waiting for you", "attention": "needs you", "stopping": "stopping"}
# Modes Claude Code may have left on in this terminal (mouse, focus, bracketed paste), cursor
# back, attributes off - so the shell in this tab is clean after a detach.
# Kitty keyboard flags popped, modifyOtherKeys off and autowrap on as well: a shell with them left
# over gets escape codes for arrows and Enter.
RESET = (b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[?25h\x1b[0m"
         b"\x1b[<99u\x1b[>4m\x1b[?7h")
GHOSTTY = ("/Applications/Ghostty.app", os.path.expanduser("~/Applications/Ghostty.app"))


class Fail(Exception):
    """A message for the user and the exit code."""

    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ piperd
class Api:
    def __init__(self):
        self.tok = common.own_token("tty")
        self.etag = ""
        self.state: dict = {}

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict:
        req = urllib.request.Request(BASE + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.tok,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or e.reason
            except ValueError:
                msg = e.reason
            raise Fail("piperd: %s" % msg) from None

    def poll(self, timeout: float = 5) -> dict:
        """GET /api/state with its ETag: the last state when nothing changed."""
        req = urllib.request.Request(BASE + "/api/state", headers={
            "Authorization": "Bearer " + self.tok, **({"If-None-Match": self.etag} if self.etag else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.state = json.loads(r.read())
                self.etag = r.headers.get("ETag", "")
        except urllib.error.HTTPError as e:
            if e.code != 304:
                raise Fail("piperd: HTTP %d on /api/state" % e.code) from None
        return self.state


def piperd_up() -> bool:
    try:
        with urllib.request.urlopen(BASE + "/piper/icon.svg", timeout=2):
            return True
    except (OSError, urllib.error.URLError):
        return False


def ensure_piperd() -> None:
    if piperd_up():
        return
    print("piper: starting piperd…", file=sys.stderr)
    subprocess.run(["sh", PIPER_SH, "ensure"], stdout=sys.stderr, check=False)
    if not piperd_up():
        raise Fail("piperd did not come up on %s - see %s/piperd.log" % (BASE, common.PIPER_HOME))


def ptyhost_check() -> None:
    try:
        pc = PtyClient(common.PTY_SOCK)
    except (FileNotFoundError, ConnectionRefusedError):
        raise Fail("ptyhost is not running (%s) - start a session in the GUI first: piper" % common.PTY_SOCK) from None
    try:
        v = int(pc.call("ping", timeout=3).get("version") or 0)
    finally:
        pc.close()
    if v < PTY_VERSION:
        raise Fail("the running ptyhost is v%d; attaching needs v%d. It updates after `task piper:stop -- all` "
                   "(this ENDS every running session) and the next start." % (v, PTY_VERSION))


# ------------------------------------------------------------------ resolving the target
def live(st: dict) -> list[dict]:
    rows = st.get("sessions") or []
    if rows and not any("alive" in s for s in rows):
        raise Fail("piperd is older than this client - restart it (sessions survive): piper restart")
    return [s for s in st.get("sessions") or [] if s.get("alive") and s.get("kind") != "prodcheck"]


def cwd_project(st: dict, cwd: str | None = None) -> dict | None:
    """The project whose checkout holds the current directory (the deepest one)."""
    here = os.path.realpath(cwd or os.getcwd())
    best = None
    for p in st.get("projects") or []:
        root = os.path.realpath(p.get("path") or "")
        if root and (here == root or here.startswith(root.rstrip("/") + "/")):
            if best is None or len(root) > len(os.path.realpath(best["path"])):
                best = p
    return best


def find_project(st: dict, name: str) -> dict | None:
    projects = st.get("projects") or []
    for p in projects:
        if p["key"] == name:
            return p
    tail = [p for p in projects if p["key"].rsplit("/", 1)[-1] == name]
    if len(tail) == 1:
        return tail[0]
    if len(tail) > 1:
        raise Fail("%r matches %s - use the full key" % (name, ", ".join(p["key"] for p in tail)), 2)
    return None


def resolve(st: dict, target: str) -> tuple[dict | None, dict | None]:
    """(session, None) to attach to, or (None, project) to start one in."""
    sessions = [s for s in st.get("sessions") or [] if s.get("kind") != "prodcheck"]
    for s in sessions:
        if s["id"] == target:
            return s, None   # an ended one: main() offers to Continue it
    pre = [s for s in live(st) if len(target) >= 4 and s["id"].startswith(target)]
    if len(pre) == 1:
        return pre[0], None
    if target in ("advisor", "global", "agent"):
        adv = [s for s in live(st) if s.get("kind") == "advisor"]
        if adv:
            return adv[0], None
        return None, {"key": "advisor", "title": "Global agent", "path": ""}
    p = cwd_project(st) if target == "." else find_project(st, target)
    if p is None:
        if target == ".":
            raise Fail("this directory is not in any Piper project - `piper attach -l` lists the sessions", 2)
        raise Fail("no session or project %r - `piper attach -l` lists the sessions; projects come from the "
                   "checkouts under PROJECT_PATH and %s" % (target, common.PROJECTS_FILE), 2)
    mine = [s for s in live(st) if s.get("project") == p["key"] and s.get("kind") == "project"]
    if len(mine) == 1:
        return mine[0], None
    if len(mine) > 1:
        print_list(mine, file=sys.stderr)
        raise Fail("%s has %d live sessions - pick one by its ID (or --new for another)" % (p["key"], len(mine)), 2)
    return None, p


def label(s: dict) -> str:
    return s.get("title") or s.get("project") or s["id"]


def print_list(rows: list[dict], numbered: bool = False, file=None) -> None:
    file = file or sys.stdout
    for i, s in enumerate(rows, 1):
        mark = " ⌨" if s.get("tty") else ""
        print("%s%-9s %-22s %-17s %s%s" % ("%2d  " % i if numbered else "", s["id"],
                                          ("Global agent" if s.get("kind") == "advisor" else s.get("project") or "?")[:22],
                                          WORDS.get(s.get("state"), s.get("state")), label(s), mark), file=file)


def pick(st: dict) -> tuple[dict | None, dict | None]:
    rows = live(st)
    here = cwd_project(st)
    if not rows and here is None:
        raise Fail("no live session - start one: piper attach <project>, or in the GUI: piper", 2)
    print("Live sessions (⌨ = attached in a terminal):" if rows else "No live session.")
    print_list(rows, numbered=True)
    opts = ("1" if len(rows) == 1 else "1-%d" % len(rows)) if rows else ""
    if here is not None:
        opts += (", " if opts else "") + "n = new session in %s" % here["key"]
    try:
        ans = input("Attach to [%s, Enter = cancel]: " % opts).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Fail("", 130) from None
    if not ans:
        raise Fail("", 0)
    if ans == "n" and here is not None:
        return None, here
    if ans.isdigit() and 1 <= int(ans) <= len(rows):
        return rows[int(ans) - 1], None
    raise Fail("no such choice: %s" % ans, 2)


# ------------------------------------------------------------------ a new session
def start(api: Api, project: dict) -> dict:
    """Create it through piperd and wait until it has a terminal (or failed its preflight)."""
    s = api.call("POST", "/api/sessions", {"project": project["key"], "origin": "tty"})
    print("piper: new session %s in %s" % (s["id"], project["key"]), file=sys.stderr)
    return wait_running(api, s["id"])


def pty_alive(sid: str) -> bool:
    try:
        pc = PtyClient(common.PTY_SOCK)
    except OSError:
        return False
    try:
        return any(x["id"] == sid and x.get("alive") for x in pc.call("list", timeout=3).get("sessions") or [])
    finally:
        pc.close()


def wait_running(api: Api, sid: str, restarting: bool = False) -> dict:
    """Until the session has a live terminal in ptyhost; `restarting`: Continue was just asked
    for, so the ended state it is still in does not count yet."""
    shown, deadline = "", time.time() + 90
    while time.time() < deadline:
        cur = next((x for x in api.poll().get("sessions") or [] if x["id"] == sid), None)
        if cur is None:
            raise Fail("session %s disappeared" % sid)
        st = cur.get("state")
        if restarting and st in ("exited", "error"):
            time.sleep(0.3)
            continue
        restarting = False
        if st != shown:
            shown = st
            print("piper: %s…" % WORDS.get(st, st), file=sys.stderr)
        if st == "blocked":
            why = [cur.get("detail") or "the preflight blocked the start"]
            why += ["  %s" % (b.get("path") or b.get("detail") or b) if isinstance(b, dict) else "  %s" % b
                    for b in cur.get("blocks") or []]
            raise Fail("\n".join(why) + "\nFix it in the GUI (Add excludes), then attach again: piper", 3)
        if st in ("error", "exited"):
            raise Fail("the start failed: %s" % (cur.get("detail") or st), 3)
        if st not in STARTING and cur.get("pid") and pty_alive(sid):
            return cur
        time.sleep(0.3)
    raise Fail("session %s is still %s after 90 s - see the GUI" % (sid, shown), 3)


# ------------------------------------------------------------------ the attached terminal
def out(data: bytes) -> None:
    view = memoryview(data)
    while view:   # a tty may take a large chunk only in part
        view = view[os.write(sys.stdout.fileno(), view):]


class Attach:
    def __init__(self, sid: str, title: str, api: Api | None):
        self.sid, self.title, self.api = sid, title, api
        self.done = threading.Event()
        self.why = ""
        self.ended = False  # the session itself ended (not only this terminal)
        self.winch = True   # send our size right after subscribing
        self.size: tuple[int, int] | None = None
        self.locked_said = False
        self.pc = PtyClient(common.PTY_SOCK, on_event=self.on_event, on_close=self.on_close)

    def on_event(self, ev: dict) -> None:
        if ev.get("id") != self.sid:
            return
        kind = ev.get("ev")
        if kind == "data":
            out(base64.b64decode(ev.get("data", "")))
        elif kind == "respawn":   # Continue/Retry under the same ID: a fresh screen at our size
            out(b"\x1b[H\x1b[2J")
            self.size, self.winch = None, True
        elif kind == "exit":
            self.why = "the session ended (rc %s)" % ev.get("rc")
            self.ended = True
            self.done.set()
        elif kind == "tty-taken":
            self.why = "taken over (Use here in the GUI, or piper attach --take)"
            self.done.set()

    def on_close(self) -> None:
        if not self.done.is_set():
            self.why = "ptyhost closed the connection"
            self.done.set()

    def say(self, text: str) -> None:
        out(b"\r\n\x1b[7m[piper] " + text.encode() + b"\x1b[0m\r\n")

    def send_size(self) -> None:
        try:
            cols, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return
        if (cols, rows) == self.size:   # a terminal sends SIGWINCH without a change too
            return
        first = self.size is None
        self.size = (cols, rows)
        if first and rows > 3:
            # Claude Code redraws only when the size really changes; a PTY already at our size
            # (a reattach) would leave this terminal blank - jiggle it by a row (verified 2026-09-30)
            self.pc.call("resize", id=self.sid, cols=cols, rows=rows - 1)
            time.sleep(0.15)
        self.pc.call("resize", id=self.sid, cols=cols, rows=rows)

    def send(self, data: bytes) -> None:
        if not data:
            return
        r = self.pc.call("input", id=self.sid, data=b64(data))
        if r.get("ok"):
            self.locked_said = False
        elif r.get("error") == "locked":
            if not self.locked_said:
                self.say("input is locked until the isolation check has passed - typing is dropped")
                self.locked_said = True
        elif r.get("error") != "exited":
            self.say("input refused: %s" % r.get("error"))

    def watch(self) -> None:
        """piperd knows what the bytes do not show: a confirmation card, a notice, a block."""
        seen_pending: set[str] = set()
        notice = None
        while not self.done.wait(2.0):
            try:
                st = self.api.poll(timeout=3)
            except (Fail, OSError, ValueError):
                continue   # piperd restarting: the stream goes on from ptyhost
            s = next((x for x in st.get("sessions") or [] if x["id"] == self.sid), None)
            if s is None:
                continue
            if notice is None:
                notice = s.get("notice") or ""
            elif (s.get("notice") or "") != notice:
                notice = s.get("notice") or ""
                if notice:
                    self.say(notice)
            for p in st.get("pending") or []:
                if p.get("session") == self.sid and p.get("id") not in seen_pending:
                    seen_pending.add(p.get("id"))
                    out(b"\x07")
                    self.say("waiting for your confirmation in the GUI: %s - open it: piper" % (
                        p.get("title") or p.get("kind") or "a request"))

    def run(self, replay: bool, take: bool) -> None:
        info = next((s for s in self.pc.call("list")["sessions"] if s["id"] == self.sid), None)
        if info is None:
            raise Fail("ptyhost has no terminal for session %s (yet)" % self.sid)
        if not info.get("alive"):
            raise Fail("session %s has ended (rc %s) - Continue it in the GUI" % (self.sid, info.get("rc")))
        req = {"tty": True, **({"take": True} if take else {}), **({} if replay else {"from": info["bytes"]})}
        r = self.pc.call("subscribe", id=self.sid, **req)
        if not r.get("ok"):
            self.pc.close()
            if r.get("error") == "tty-busy":
                raise Fail("session %s is attached in another terminal - take it over: piper attach %s --take"
                           % (self.sid, self.sid), 4)
            raise Fail("subscribe failed: %s" % r.get("error"))
        print("piper: attached to %s (%s) - Ctrl-] detaches, the session keeps running" % (self.title, self.sid),
              file=sys.stderr)
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "winch", True))
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda *_: (setattr(self, "why", "detached (signal)"), self.done.set()))
        # title stack push + our title, bracketed paste on (Claude Code turned it on before we came),
        # and a clean screen for Claude's redraw (without a replay nothing else clears it)
        out(b"\x1b[22;0t\x1b]2;" + self.title.encode() + b"\x07\x1b[?2004h" + (b"" if replay else b"\x1b[H\x1b[2J"))
        tty.setraw(fd)
        if self.api is not None:
            threading.Thread(target=self.watch, daemon=True).start()
        try:
            while not self.done.is_set():
                if self.winch:
                    self.winch = False
                    self.send_size()
                try:
                    ready, _, _ = select.select([fd], [], [], 0.2)
                except InterruptedError:
                    continue
                if not ready:
                    continue
                data = os.read(fd, 65536)
                if not data:
                    self.why = "the terminal closed"
                    break
                if DETACH in data:
                    self.send(data[:data.index(DETACH)])
                    self.why = "detached"
                    break
                self.send(data)
        except PtyHostError as e:
            self.why = str(e)
        finally:
            self.done.set()   # our own close below must not read as "ptyhost closed"
            try:
                if not self.pc.closed:
                    self.pc.call("unsubscribe", id=self.sid, timeout=2)
            except PtyHostError:
                pass
            self.pc.close()
            for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):   # a question may follow (Continue)
                signal.signal(sig, signal.SIG_DFL)
            try:   # a closed tab (SIGHUP) has no terminal left to restore
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
                # Claude's frame stays with the cursor inside it (the prompt box): clear the
                # visible screen so the shell starts clean (the scrollback is kept)
                out(RESET + b"\x1b[23;0t\x1b[H\x1b[2J")
                if self.ended:
                    print("piper: %s" % self.why)
                else:
                    print("piper: %s - the session keeps running (GUI: piper). Attach again: piper attach %s"
                          % (self.why or "detached", self.sid))
            except OSError:
                pass


def again(api: Api, sess: dict, wait: bool = True) -> bool:
    """The session ended: Continue it here (the GUI's Continue / Retry)."""
    if wait:
        time.sleep(0.5)   # piperd records the exit (state, claude_session)
    cur = next((x for x in api.poll().get("sessions") or [] if x["id"] == sess["id"]), sess)
    if cur.get("state") not in ("exited", "error"):
        return False
    resume = bool(cur.get("claude_session"))
    try:
        ans = input("Continue this session here (%s resumes the conversation)? [Y/n] "
                    % ("Codex" if cur.get("agent") == "codex" else "Claude") if resume
                    else "Start this session again here? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not (ans in ("y", "yes") or (resume and ans == "")):
        return False
    api.call("POST", "/api/sessions/%s/retry" % sess["id"], {})
    return True


# ------------------------------------------------------------------ "Open in terminal" (GUI)
TERMINALS = {"ghostty": ("Ghostty", GHOSTTY), "warp": ("Warp", ("/Applications/Warp.app", os.path.expanduser("~/Applications/Warp.app"))),
             "terminal": ("Terminal", ("/System/Applications/Utilities/Terminal.app", "/Applications/Utilities/Terminal.app"))}
TERMINAL_CONFIG = os.path.join(common.PIPER_HOME, "terminal.json")   # Settings > Terminal: {"app": auto|ghostty|warp|terminal}
WARP_CONFIG = os.path.join(common.REAL_HOME, ".warp", "launch_configurations", "piper-attach.yaml")


def terminal_app() -> tuple[str, str] | None:
    """(key, app bundle) of the terminal Settings names; auto = Ghostty if installed, else Terminal."""
    try:
        with open(TERMINAL_CONFIG) as f:
            want = str(json.load(f).get("app") or "auto")
    except (OSError, ValueError, AttributeError):
        want = "auto"
    for key in (["ghostty", "terminal"] if want == "auto" else [want]):
        app = next((a for a in TERMINALS.get(key, ("", ()))[1] if os.path.isdir(a)), None)
        if app:
            return key, app
    return None


def terminal_name() -> str:
    """D4: the app Open in terminal would use ("Ghostty"), for the GUI's "Attach in <app>"; "" = none / not macOS."""
    found = terminal_app() if sys.platform == "darwin" else None
    return TERMINALS[found[0]][0] if found else ""


# Linux: the argv before `sh -c <script>` per emulator (by its file name, so a symlink like
# x-terminal-emulator gets the syntax of what it points to); `auto` tries them in this order
LINUX_TERMINALS = {
    "ghostty": ["-e"], "ptyxis": ["--new-window", "--"], "gnome-terminal": ["--window", "--"], "kgx": ["--"],
    "konsole": ["-e"], "xfce4-terminal": ["-x"], "mate-terminal": ["-x"], "terminator": ["-x"],
    "kitty": [], "alacritty": ["-e"], "wezterm": ["start", "--"], "foot": [], "xterm": ["-e"],
}
OPEN_GRACE_S = 1.5   # an emulator that exits sooner than this with an error did not open a window


def _attach_script(sid: str, cwd: str) -> tuple[str, str]:
    """(the attach command, the whole shell script: cd, attach, then a login shell in the project)."""
    # The new window gets launchd's / the desktop's environment, not ours: absolute python, our home and port.
    q = shlex.quote
    # --take: asked for from the GUI, so a terminal that has the session now hands it over
    attach = "PIPER_HOME=%s PIPER_PORT=%d PIPER_PTY_SOCK=%s %s %s %s --take" % (
        q(common.PIPER_HOME), PORT, q(common.PTY_SOCK), q(sys.executable), q(os.path.abspath(__file__)), q(sid))
    shell = "/bin/zsh" if sys.platform == "darwin" else "/bin/sh"
    return attach, "cd %s 2>/dev/null; %s; exec \"${SHELL:-%s}\" -l" % (q(cwd or common.REAL_HOME), attach, shell)


def _linux_terminal() -> tuple[str, list[str]] | None:
    """(name, argv prefix) of the emulator to use: Settings' Ghostty when chosen, else $TERMINAL,
    the distribution's x-terminal-emulator, then the known ones in LINUX_TERMINALS order."""
    try:
        with open(TERMINAL_CONFIG) as f:
            want = str(json.load(f).get("app") or "auto")
    except (OSError, ValueError, AttributeError):
        want = "auto"
    names = ["ghostty"] if want == "ghostty" else []
    names += [n for n in (os.environ.get("TERMINAL", "").strip(), "x-terminal-emulator") if n] + list(LINUX_TERMINALS)
    for name in names:
        path = shutil.which(name)
        if not path:
            continue
        real = os.path.basename(os.path.realpath(path))
        for cand in (real, real.removesuffix(".wrapper"), os.path.basename(name)):
            # Debian's gnome-terminal.wrapper knows -e only: the emulator itself, by its own syntax
            if cand in LINUX_TERMINALS and (cand == real or shutil.which(cand)):
                return cand, [path if cand == real else shutil.which(cand)] + LINUX_TERMINALS[cand]
        return real, [path, "-e"]   # x-terminal-emulator / $TERMINAL: -e is the convention
    return None


def _wsl_terminal(script_path: str) -> tuple[str, list[str]] | None:
    """Windows Terminal (else a console through cmd.exe) running `sh <file>` in this distro. A file,
    not `sh -c`: wt.exe splits its command line at every `;`."""
    distro = os.environ.get("WSL_DISTRO_NAME", "")
    inner = ["wsl.exe"] + (["-d", distro] if distro else []) + ["--", "sh", script_path]
    # a systemd unit may lack the Windows PATH: the usual places too
    wt = shutil.which("wt.exe") or next(iter(sorted(glob.glob("/mnt/c/Users/*/AppData/Local/Microsoft/WindowsApps/wt.exe"))), None)
    if wt:
        return "Windows Terminal", [wt, "-w", "new"] + inner
    cmd = shutil.which("cmd.exe") or ("/mnt/c/Windows/System32/cmd.exe" if os.path.exists("/mnt/c/Windows/System32/cmd.exe") else None)
    if cmd:
        return "a Windows console", [cmd, "/c", "start", "", *inner]
    return None


def _desktop_env() -> dict:
    """Our environment plus the desktop session's sockets when a service manager left them out."""
    env = dict(os.environ)
    run = "/run/user/%d" % os.getuid()
    if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
        way = sorted(n for n in (os.listdir(run) if os.path.isdir(run) else []) if n.startswith("wayland-") and not n.endswith(".lock"))
        if way:
            env.setdefault("XDG_RUNTIME_DIR", run)
            env["WAYLAND_DISPLAY"] = way[0]
        x11 = sorted(n for n in (os.listdir("/tmp/.X11-unix") if os.path.isdir("/tmp/.X11-unix") else []) if n.startswith("X"))
        if x11:
            env["DISPLAY"] = ":" + x11[0][1:]
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(run + "/bus"):
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % run   # gnome-terminal / ptyxis ask their server over D-Bus
    return env


def _open_elsewhere(sid: str, cmd: str, cwd: str) -> dict:
    """Linux and WSL: a new window of the desktop's terminal (Windows Terminal under WSL)."""
    script = _attach_script(sid, cwd)[1]
    wsl = common.is_wsl()
    if wsl:
        run = os.path.join(common.PIPER_HOME, "run")
        os.makedirs(run, mode=0o700, exist_ok=True)
        path = os.path.join(run, "attach-%s.sh" % sid)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        os.write(fd, ("#!/bin/sh\n%s\n" % script).encode())
        os.close(fd)
        found = _wsl_terminal(path)
    else:
        found = _linux_terminal()
        if found:
            found = (found[0], found[1] + ["sh", "-c", script])
    if found is None:
        return {"ok": False, "command": cmd, "error": "no terminal found (Windows Terminal / cmd.exe)" if wsl else
                "no terminal emulator found - install one or set $TERMINAL for piperd"}
    name, argv = found
    # cmd.exe complains about a Linux working directory; the script cds itself
    where = "/mnt/c" if wsl and os.path.isdir("/mnt/c") else (common.REAL_HOME if os.path.isdir(common.REAL_HOME) else "/")
    try:
        # a file, not a pipe: an emulator like xterm keeps running (and writing) while its window is open
        with tempfile.TemporaryFile() as errf:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errf,
                                 env=_desktop_env(), cwd=where, start_new_session=True)
            try:
                rc = p.wait(OPEN_GRACE_S)
            except subprocess.TimeoutExpired:   # still running: the window is up; reaped when it closes
                rc = 0
                threading.Thread(target=p.wait, daemon=True, name="open-terminal").start()
            errf.seek(0)
            err = errf.read(2000).decode(errors="replace") if rc else ""
    except OSError as e:
        return {"ok": False, "command": cmd, "error": "%s: %s" % (name, e)}
    if rc:
        return {"ok": False, "command": cmd, "error": ("%s: %s" % (name, err.strip() or "exit %d" % rc))[:300]}
    common.audit({"event": "open-terminal", "session": sid, "terminal": name})
    return {"ok": True, "command": cmd, "terminal": name}


def open_terminal(sid: str, cwd: str) -> dict:
    """A new terminal window running `piper attach <id>`, then a shell in the project (git next to
    it), in the terminal Settings names (macOS), the desktop's one (Linux) or Windows Terminal (WSL).
    Without one: the command, for the GUI to copy."""
    cmd = "piper attach %s" % sid
    if sys.platform != "darwin":
        return _open_elsewhere(sid, cmd, cwd)
    found = terminal_app() if shutil.which("open") else None
    if found is None:
        return {"ok": False, "command": cmd, "error": "the terminal from Settings > Terminal is not installed"}
    key, app = found
    attach, script = _attach_script(sid, cwd)
    try:
        if key == "ghostty":
            # One `sh -c` string and no argument that is an existing path: macOS hands such arguments
            # to the app as files to open, and Ghostty opens each in a tab of its own (seen 2026-10-01).
            # Verified on Ghostty 1.3.1: `open -na Ghostty.app --args -e …` opens a new window.
            argv = ["open", "-na", app, "--args", "-e", "sh", "-c", script]
        elif key == "terminal":
            # Terminal runs a .command file it is asked to open; no Automation permission needed
            run = os.path.join(common.PIPER_HOME, "run")
            os.makedirs(run, mode=0o700, exist_ok=True)
            path = os.path.join(run, "attach-%s.command" % sid)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
            os.write(fd, ("#!/bin/sh\n%s\n" % script).encode())
            os.close(fd)
            argv = ["open", "-a", app, path]
        else:
            # a launch configuration (one file, rewritten per click) through Warp's URI scheme; its pane
            # is a shell already, so after a detach it simply stays (fixed upstream: warp#9007)
            os.makedirs(os.path.dirname(WARP_CONFIG), exist_ok=True)
            yaml = ("# written by Piper's Open in terminal (piper attach), rewritten on every click\n---\n"
                    "name: piper-attach\nwindows:\n  - tabs:\n      - title: %s\n        layout:\n"
                    "          cwd: %s\n          commands:\n            - exec: %s\n") % (
                json.dumps(cmd), json.dumps(cwd or common.REAL_HOME), json.dumps(attach))
            with open(WARP_CONFIG, "w") as f:
                f.write(yaml)
            # Warp looks the configuration up by its `name:` (not the path; warp#6303): the same word
            argv = ["open", "warp://launch/piper-attach"]
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "command": cmd, "error": str(e)}
    if r.returncode:
        return {"ok": False, "command": cmd, "error": (r.stderr or "open failed").strip()[:300]}
    common.audit({"event": "open-terminal", "session": sid, "terminal": key})
    return {"ok": True, "command": cmd, "terminal": TERMINALS[key][0]}


# ------------------------------------------------------------------ main
def main(argv: list[str]) -> int:
    flags = {a for a in argv if a.startswith("-")}
    args = [a for a in argv if not a.startswith("-")]
    unknown = flags - {"--new", "--replay", "--take", "-l", "--list", "-h", "--help"}
    if "-h" in flags or "--help" in flags or unknown or len(args) > 1:
        print(USAGE, file=sys.stderr if unknown or len(args) > 1 else sys.stdout)
        return 2 if unknown or len(args) > 1 else 0
    try:
        ensure_piperd()
        api = Api()
        st = api.poll()
        if "-l" in flags or "--list" in flags:
            rows = live(st)
            print_list(rows) if rows else print("no live session")
            return 0
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if not interactive:
            raise Fail("attach needs a terminal (stdin and stdout)")
        if not args:
            sess, proj = pick(st)
        elif "--new" in flags:
            proj = cwd_project(st) if args[0] == "." else find_project(st, args[0])
            if proj is None and args[0] in ("advisor", "global", "agent"):
                raise Fail("there is one Global agent - attach to it without --new", 2)
            if proj is None:
                raise Fail("no project %r" % args[0], 2)
            sess = None
        else:
            sess, proj = resolve(st, args[0])
        ptyhost_check()
        if sess is not None and not sess.get("alive"):
            print("piper: session %s (%s) has ended" % (sess["id"], label(sess)), file=sys.stderr)
            if sess.get("state") == "blocked" or not again(api, sess, wait=False):
                return 0 if sess.get("state") != "blocked" else 3
            sess = wait_running(api, sess["id"], restarting=True)
        new = sess is None
        if new:
            sess = start(api, proj)
        elif sess.get("state") in STARTING or not sess.get("pid"):   # started in the GUI a moment ago
            sess = wait_running(api, sess["id"])
            new = True   # its output so far: hole's start, a trust prompt
        title = label(sess)
        if sess.get("project") and sess["project"] not in title and sess.get("kind") != "advisor":
            title += " · " + sess["project"]
        # a session started just now: its whole output so far (hole's start, a trust prompt to answer)
        replay, take = new or "--replay" in flags, "--take" in flags
        while True:
            a = Attach(sess["id"], title, api)
            a.run(replay, take)
            if not a.ended or not again(api, sess):
                return 0
            sess = wait_running(api, sess["id"], restarting=True)
            replay, take = True, False
    except Fail as e:
        if str(e):
            print("piper: %s" % e, file=sys.stderr)
        return e.code
    except PtyHostError as e:
        print("piper: ptyhost: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
