"""Agent host commands (`host_run`): a session asks to run ONE command in its host shell - the W2 shell beside
it (hostterm.py), on this Mac, without a sandbox - and the human allows every single command on a red card.

The last resort, not a shortcut: a command that a Piper tool covers (git -> git_run, a database client ->
db_*, curl -> http_run, glab/gh -> gitlab_*/github_*, docker compose -> piper_worktree_stack, a file reader ->
read_file) reaches the card only after this session called that tool, and the agent says why it was not
enough (`why_no_tool`). The router is a guard rail for an honest agent, not a sandbox: the card is the gate.

Opt-in (feature module `host_run`, in no preset, never proposable by an agent: settings.AGENT_DENY) and only
together with the Host shells module. The card is `_force_approval`: no Security switch, session grant, hour
grant or late grant ever runs a command without the human's click.

The command is typed into the session's shell (one per session, opened when there is none) once that shell is
idle: the shell itself in the foreground and the human not typing. A marker the shell prints after it (OSC 133
D with a per-run nonce, invisible in the terminal) gives the exit code; the output between is read from
ptyhost's buffer, never written to disk. The agent gets its scrubbed tail unless the human picked "Run, keep
output here". The audit keeps the command (scrubbed, cut), the folder and the exit code - never the output.
"""
from __future__ import annotations

import base64
import os
import re
import secrets
import shlex
import subprocess
import threading
import time

import common
import evidence
import features

TOOL = "host_run"
KIND = "host_run"
MODULE = "host_run"
MAX_CMD = 4096
MAX_REASON = 600
OUT_TAIL = 16 << 10        # what the agent gets back
CAPTURE_MAX = 4 << 20      # what piperd keeps of one run's output while waiting for the marker
CARD_S = 600               # piper-mcp waits 900 s for a CONFIRMED tool: card, idle wait and the run share it
CALL_BUDGET = 860
IDLE_WAIT = 120            # how long an allowed command waits for a busy shell
QUIET_KEYS = 5.0           # the human's last key in the shell must be this old
KEEP_HERE = "Run, keep output here"

# category -> (the programs, the Piper tools that cover them, the one to name)
CATEGORIES: dict[str, tuple[frozenset, tuple, str]] = {
    "git": (frozenset({"git"}), ("git_run", "git_status", "git_diff", "git_log", "git_commit", "git_push"), "git_run"),
    "gitlab": (frozenset({"glab"}), ("gitlab_",), "gitlab_* tools"),
    "github": (frozenset({"gh"}), ("github_",), "github_* tools"),
    "db": (frozenset({"mysql", "mariadb", "psql", "pg_dump", "pg_dumpall", "pg_restore", "mysqldump", "mariadb-dump",
                      "sqlite3", "redis-cli", "mongosh", "mongo", "clickhouse-client", "sqlcmd", "usql"}),
           ("db_",), "db_query / db_execute"),
    "http": (frozenset({"curl", "wget", "http", "https", "xh", "httpie"}), ("http_run", "http_envs", "http_last_result"),
             "http_run"),
    "stack": (frozenset({"docker-compose"}), ("piper_worktree_stack",), "piper_worktree_stack"),
    "files": (frozenset({"cat", "head", "less", "more", "bat"}), ("read_file", "grep", "list_dir"),
              "read_file (your own project: your sandboxed shell)"),
}
# wrappers whose own options come first: the number of option arguments some of them take
WRAPPERS = {"sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T"}, "env": {"-u", "-C", "-S", "-P"},
            "command": set(), "exec": {"-a"}, "builtin": set(), "nohup": set(), "time": set(), "caffeinate": {"-t", "-w"},
            "nice": {"-n"}, "stdbuf": {"-i", "-o", "-e"}, "xargs": {"-I", "-n", "-L", "-P", "-s", "-d", "-E", "-J", "-R"},
            "timeout": {"-s", "-k"}, "doas": {"-u", "-C"}, "arch": set(), "watch": {"-n", "-d"}}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")", ";;", "!", "{", "}"}
REDIRECTS = re.compile(r"^\d*(<|>|>>|<<|<<<|<>|>&|<&|&>|&>>|>\|)$")
ANSI = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[PX^_][^\x1b]*\x1b\\|\x1b[@-Z\\-_]|\x1b.")


class Refused(ValueError):
    """The agent's call was refused before any card (the message tells it what to do instead)."""


def enabled() -> bool:
    return features.on("host") and features.on(MODULE)


# ------------------------------------------------------------ the command
def check_command(cmd) -> str:
    """One line, printable, balanced quotes, no comment, no continuation; returns it stripped."""
    if not isinstance(cmd, str) or not cmd.strip():
        raise Refused("command must be a non-empty string")
    cmd = cmd.strip()
    if len(cmd) > MAX_CMD:
        raise Refused("command is longer than %d characters - put it into a script in the project and run that" % MAX_CMD)
    bad = next((c for c in cmd if (ord(c) < 0x20 and c != "\t") or ord(c) == 0x7f or 0x80 <= ord(c) < 0xa0
                or c in "  "), None)
    if bad is not None:
        raise Refused("command must be one line without control characters (found U+%04X) - put a multi-line "
                      "script into a file in the project and run that" % ord(bad))
    if common.show_bidi(cmd) != cmd:
        raise Refused("command contains invisible or bidirectional characters")
    cmd = cmd.replace("\t", " ")
    quote, i, prev = "", 0, " "
    while i < len(cmd):
        c = cmd[i]
        if quote == "'":
            if c == "'":
                quote = ""
        elif c == "\\":
            if i + 1 >= len(cmd):
                raise Refused("command must not end with a line continuation")
            i += 1
        elif quote == '"':
            if c == '"':
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "#" and (prev.isspace() or prev in ";&|()"):
            raise Refused("command must not contain a shell comment (#) - the result marker would be lost")
        prev = cmd[i]
        i += 1
    if quote:
        raise Refused("command has an unclosed %s quote" % quote)
    return cmd


def _substitutions(cmd: str) -> list[str]:
    """The bodies of $( ), <( ), >( ) and `...` - commands run as part of this one."""
    out, i = [], 0
    while i < len(cmd):
        if cmd[i] in "$<>" and cmd[i + 1:i + 2] == "(" and cmd[i + 2:i + 3] != "(":
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            out.append(cmd[i + 2:j - 1])
            i = j
            continue
        if cmd[i] == "`":
            j = cmd.find("`", i + 1)
            if j > i:
                out.append(cmd[i + 1:j])
                i = j + 1
                continue
        i += 1
    return out


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split, lex.commenters = True, ""
    try:
        return list(lex)
    except ValueError:
        return cmd.split()


def _segments(tokens: list[str]) -> list[list[str]]:
    segs, cur, skip = [], [], False
    for t in tokens:
        if skip:
            skip = False
            continue
        if REDIRECTS.match(t) or t in ("<", ">"):
            skip = True
            continue
        if t in SEPARATORS or set(t) <= set(";&|()"):
            if cur:
                segs.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _programs(cmd: str, depth: int = 0) -> list[list[str]]:
    """Every simple command's argv (from the program on), wrappers and `sh -c` unwrapped."""
    if depth > 4:
        return []
    out = []
    for sub in _substitutions(cmd):
        out += _programs(sub, depth + 1)
    for seg in _segments(_tokens(cmd)):
        argv = list(seg)
        while argv:
            while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
                argv.pop(0)
            if not argv:
                break
            name = os.path.basename(argv[0])
            if name in WRAPPERS:
                takes, argv = WRAPPERS[name], argv[1:]
                while argv and (argv[0].startswith("-") or (name == "env" and "=" in argv[0])
                                or (name in ("timeout", "nice") and re.match(r"^[\d.]+[smhd]?$", argv[0]))):
                    flag = argv.pop(0)
                    if flag == "--":
                        break
                    if flag in takes and argv:
                        argv.pop(0)
                continue
            if name == "eval":
                out += _programs(" ".join(argv[1:]), depth + 1)
                argv = []
                break
            if name in SHELLS:
                script = next((argv[i + 1] for i, a in enumerate(argv[1:-1], 1)
                               if a.startswith("-") and not a.startswith("--") and "c" in a[1:]), None)
                if script is not None:
                    out += _programs(script, depth + 1)
                    argv = []
                    break
            break
        if argv:
            out.append(argv)
    return out


def categories(cmd: str) -> set[str]:
    """The Piper tool categories this command reaches around (git, db, http, ...)."""
    found = set()
    for argv in _programs(cmd):
        name = os.path.basename(argv[0])
        if name == "docker" and "compose" in argv[1:4]:
            found.add("stack")
            continue
        for cat, (progs, _tools, _name) in CATEGORIES.items():
            if name in progs and (cat != "files" or _reads_a_file(argv[1:])):
                found.add(cat)
    return found


def _reads_a_file(args: list[str]) -> bool:
    """`cat x`, `head -n 5 x`: a file operand. `| head -3`, `| tail -n 40`: a filter of the pipe, nothing to route."""
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a in ("-n", "-c"):
            skip = True
        elif a == "-" or a.startswith("-"):
            continue
        else:
            return True
    return False


def covers(cat: str, tool: str) -> bool:
    return any(tool.startswith(t) if t.endswith("_") else tool == t for t in CATEGORIES[cat][1])


# ------------------------------------------------------------ the output
def strip_ansi(data: bytes) -> str:
    text = ANSI.sub(b"", data).decode("utf-8", "replace")
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        lines.append(line.rsplit("\r", 1)[-1] if "\r" in line.rstrip("\r") else line.rstrip("\r"))   # a progress bar: its last state
    return "\n".join(lines)


def output_text(raw: bytes, nonce: str) -> tuple[str, bool]:
    """What the command printed: after the echo of the typed line (it names the nonce), before the marker.
    Returns (tail, truncated)."""
    text = strip_ansi(raw)
    at = text.rfind("piper-" + nonce)
    if at >= 0:
        nl = text.find("\n", at)
        text = text[nl + 1:] if nl >= 0 else ""
    text = text.strip("\n")
    cut = len(text) > OUT_TAIL
    if cut:
        text = text[-OUT_TAIL:]
        text = text[text.find("\n") + 1:] if "\n" in text[:200] else text
    return "\n".join(common.scrub(ln) for ln in text.split("\n")), cut


def marker(nonce: str) -> re.Pattern:
    return re.compile(rb"\x1b\]133;D;(-?\d+);piper-" + nonce.encode() + rb"\x07")


def _quote(cmd: str, fish: bool) -> str:
    if fish:
        return "'" + cmd.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return "'" + cmd.replace("'", "'\\''") + "'"


def typed_line(cmd: str, nonce: str, shell: str) -> str:
    """The line piperd types: a leading space (kept out of the history where the shell does that), the command as
    ONE quoted eval argument, then the marker with its exit code. eval keeps the typed line complete whatever the
    command is: a trailing ; or &, an unterminated `for`/`(`/`|` or a here-document is the command's own syntax
    error (rc 1/2, the marker still comes) - never a continuation prompt the next command would be appended to."""
    fish = os.path.basename(shell) == "fish"
    status = "$status" if fish else "$?"
    return " eval %s; printf '\\033]133;D;%%s;piper-%s\\007' \"%s\"\r" % (_quote(cmd, fish), nonce, status)


# ------------------------------------------------------------ Manager mixin
class HostRun:
    """sessions.Manager's half: the tool, the card, the shell and the run."""

    def note_tool_use(self, s, tool: str) -> None:
        """Every Piper tool a session called (the router's proof that the agent tried it). In memory: after a
        piperd restart the agent calls the tool again."""
        if tool == TOOL:
            return
        used = self.__dict__.setdefault("_host_run_used", {})
        used.setdefault(s.id, set()).add(tool)

    def _host_run_tried(self, s, cat: str) -> list[str]:
        return sorted(t for t in self.__dict__.get("_host_run_used", {}).get(s.id, ()) if covers(cat, t))

    def host_run_tool(self, s, args: dict) -> dict:
        t0 = time.time()
        if not enabled():
            raise Refused("host_run is off: only the user can switch it on (Settings › Features › Agent host "
                          "commands, with Host shells). Ask the user to run the command instead.")
        if s.kind != "project" or s.id.startswith("provider-"):
            raise Refused("host_run needs a project session on this computer")
        cmd = check_command(args.get("command"))
        reason = str(args.get("reason") or "").strip()
        why = str(args.get("why_no_tool") or "").strip()
        if not reason:
            raise Refused("reason is required: one sentence for the card - what the command does and why")
        if len(reason) > MAX_REASON or len(why) > MAX_REASON:
            raise Refused("reason and why_no_tool must be at most %d characters" % MAX_REASON)
        cats = categories(cmd)
        untried = sorted(c for c in cats if not self._host_run_tried(s, c))
        if untried:
            common.audit({"event": "host-run-routed", "session": s.id, "project": s.project, "categories": untried})
            raise Refused("Piper has its own tool for this: %s. Use it first - it keeps the user's approvals, "
                          "environments and masking. Call host_run only when that tool cannot do it, and say why in "
                          "why_no_tool." % "; ".join("%s -> %s" % (c, CATEGORIES[c][2]) for c in untried))
        if cats and not why:
            raise Refused("why_no_tool is required: this command does what %s does - say why that tool was not "
                          "enough" % ", ".join(CATEGORIES[c][2] for c in sorted(cats)))
        p = self.gui_project(s.project)
        if os.path.realpath(p["path"]) != os.path.realpath(s.path):
            raise Refused("the session checkout changed; ask the user to open a terminal from the current project")
        p = dict(p, path=s.path, title=s.title)
        existing = self._host_run_shell(s)
        tried = sorted({t for c in cats for t in self._host_run_tried(s, c)})
        summary = "\n".join(["Run on this Mac without a sandbox:", "$ " + cmd, "in " + s.path, "Why: " + reason]
                            + (["⚠ Piper's own tool covers this (%s); the agent tried %s and says: %s"
                                % (", ".join(CATEGORIES[c][2] for c in sorted(cats)), ", ".join(tried), why)] if cats else []))
        rows = [{"text": "Shell: " + ("the session's open host shell (%s)" % existing["title"] if existing
                                      else "a new host shell beside the session")},
                {"text": "Runs as you, with your ~/.ssh, keychain and network - nothing of the session's box applies"},
                {"text": "Output: the agent gets its last %d kB unless you pick “%s”" % (OUT_TAIL >> 10, KEEP_HERE)}]
        payload = {"command": cmd, "cwd": s.path, "project": s.project, "reason": reason, "danger": True,
                   "_force_approval": True, "shell": existing["id"] if existing else "",
                   "evidence": evidence.box("What it runs", rows=rows)}
        if cats:
            payload.update(covered=[CATEGORIES[c][2] for c in sorted(cats)], tried=tried, why_no_tool=why)
        result: dict = {}
        if not self.ask(KIND, s, summary, payload, timeout=CARD_S, always=KEEP_HERE, result=result):
            if not result.get("answered", True):
                raise ValueError("The card timed out in the Piper GUI; nothing ran. Tell the user what you need run.")
            raise ValueError("The user declined; nothing ran. Do not retry the same command - ask the user.")
        keep = bool(result.get("always"))
        lock = self.__dict__.setdefault("_host_run_locks", {}).setdefault(s.id, threading.Lock())
        if not lock.acquire(timeout=max(1.0, min(IDLE_WAIT, t0 + CALL_BUDGET - time.time() - 30))):
            raise ValueError("Another host_run of this session is still running; nothing ran. Try again later.")
        try:
            return self._host_run(s, p, cmd, keep, t0)
        finally:
            lock.release()

    def _host_run_shell(self, s) -> dict | None:
        with self.hosts.lock:
            item = next((x for x in reversed(self.hosts.items) if x.get("session") == s.id and x.get("alive")), None)
            return dict(item) if item else None

    def _host_pty_info(self, tid: str) -> dict | None:
        return next((x for x in self.pty.call("list").get("sessions") or [] if x.get("id") == tid), None)

    def _host_fg_idle(self, pid: int, info: dict | None = None) -> bool:
        """Is the shell itself the terminal's foreground process group (no command running in it)? ptyhost says so
        (`shell_in_front`); an older ptyhost: ps. Neither can tell: idle (the human checks still apply)."""
        if info is not None and isinstance(info.get("shell_in_front"), bool):
            return info["shell_in_front"]
        try:
            out = subprocess.run(["ps", "-o", "tpgid=,pgid=", "-p", str(int(pid))], capture_output=True, text=True,
                                 timeout=3).stdout.split()
        except (OSError, subprocess.SubprocessError, ValueError):
            return True
        return len(out) != 2 or out[0] == out[1]

    def _host_busy(self, tid: str) -> str:
        """'' when the allowed command may be typed now, else why not."""
        info = self._host_pty_info(tid)
        if not info or not info.get("alive"):
            return "the host shell exited"
        k = self.pty.call("pending", id=tid)
        key_at, enter_at = float(k.get("key_at") or 0), float(k.get("enter_at") or 0)
        if key_at > enter_at:
            return "the user has unsent text at the shell's prompt"
        if time.time() - key_at < QUIET_KEYS:
            return "the user is typing in the shell"
        if not self._host_fg_idle(info.get("pid") or 0, info):
            return "a command is still running in the shell"
        return ""

    def _host_run(self, s, p: dict, cmd: str, keep: bool, t0: float) -> dict:
        item = self._host_run_shell(s) or self.hosts.open(p, session=s.id)
        tid = item["id"]
        self.hosts.mark_run(tid)   # the GUI puts the shell beside the session
        deadline = min(time.time() + IDLE_WAIT, t0 + CALL_BUDGET - 30)   # one deadline with the card and the run
        why = self._host_busy(tid)
        while why and time.time() < deadline and why != "the host shell exited":
            time.sleep(0.5)
            why = self._host_busy(tid)
        if why:
            raise ValueError("Allowed, but not run: %s. Nothing was typed; ask the user." % why)
        info = self._host_pty_info(tid) or {}
        nonce = secrets.token_hex(6)
        done = threading.Event()
        buf = bytearray()
        rx = marker(nonce)
        rc_box: list = []

        def feed(data: bytes) -> None:
            if rc_box:
                return
            start = max(0, len(buf) - 64)   # a marker split across two reads
            buf.extend(data)
            m = rx.search(buf, start)
            if m:
                rc_box.append(int(m.group(1)))
                del buf[m.start():]
                done.set()
            elif len(buf) > CAPTURE_MAX:   # the head (the typed line's echo) and the newest output stay
                del buf[64 << 10:len(buf) - CAPTURE_MAX // 2]

        self.hosts.capture(tid, feed, lambda: done.set())
        try:
            self.pty.call("subscribe", id=tid, **{"from": int(info.get("bytes") or 0)})
            r = self.pty.call("input", id=tid, data=base64.b64encode(
                typed_line(cmd, nonce, self.hosts.shell_of(tid)).encode()).decode(), auto=True)
            if not r.get("ok"):
                raise ValueError("Allowed, but the shell did not take the command (%s); nothing ran." % r.get("error"))
            done.wait(max(5.0, t0 + CALL_BUDGET - time.time()))
        finally:
            self.hosts.capture(tid, None)
            try:
                self.pty.call("subscribe", id=tid, data=False)
            except Exception:  # noqa: BLE001 - ptyhost gone: the shell is gone too
                pass
        rc = rc_box[0] if rc_box else None
        text, cut = output_text(bytes(buf), nonce)
        common.audit({"event": "host-run", "session": s.id, "project": s.project, "cwd": s.path, "terminal": tid,
                      "command": common.scrub(cmd)[:2048], "rc": rc, "running": rc is None, "output_to_agent": not keep})
        out = {"rc": rc, "terminal": item.get("title") or tid, "cwd": s.path}
        if rc is None:
            out["running"] = True
            out["note"] = ("Still running in the session's host shell (the user sees it). Its exit code is not known; "
                           "a next host_run waits until the shell is free.")
        if keep:
            out["output_withheld"] = "The user kept the output in the host shell; ask them what you need from it."
        else:
            out["output"] = text
            if cut:
                out["truncated"] = "only the last %d kB" % (OUT_TAIL >> 10)
        return out


HOST_RUN_MD = ("\n- `host_run` runs ONE shell command on this Mac, outside the sandbox, in a host shell beside this "
               "session - after the user allows it on a card, every time. It is the last resort: use Piper's tools first "
               "(git_run, db_*, http_run, gitlab_*/github_*, piper_worktree_stack, read_file); a command they cover is "
               "refused until you called that tool in this session, then pass why_no_tool. One line, no comments, no "
               "interactive programs; write longer scripts into the project and run them. Never use it to reach "
               "credentials or hidden files.\n")

