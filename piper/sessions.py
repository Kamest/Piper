"""Session manager: the start pipeline and life of every orchestrated session.

  requested -> preflight (L1, L3) -> blocked | preparing -> spawning -> [trust] -> starting
            -> attesting (L4) -> ready -> working <-> waiting -> attention -> exited | error

The PTY lives in ptyhost; this module only drives it. Input stays locked in ptyhost
until attestation passes (except while hole asks about project trust or agent updates,
which only the human answers - piperd never passes --trust-project, K13). A failed attestation stops the
session with SIGTERM to its own `hole` process - never `hole destroy <path>`, which would
kill every sandbox of that project (K14).
"""
from __future__ import annotations

import base64
import copy
import datetime
import glob
import hashlib
import math
import itertools
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import stat as _stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque

import agentswitch
import codexcfg
import common
import dbtools
import digest
import directruntime
import doctor
import evidence
import extensions
import features
import quietwindow
import feed
import github
import gitlab
import gitops
import guard
import holehome
import hostterm
import httpclient
import inbox
import m2settings
import issuestate
import prodcheck
import mattermost
import projectfs
import propertyfiles
import projects
import sessioncfg
import security
import settings
import timesheet
import workbench
from common import audit, read_json, write_json
from ptyclient import PtyClient, PtyHostError, b64, connect

HOLE_BIN = os.environ.get("PIPER_HOLE_BIN", "hole")
DOCKER_BIN = os.environ.get("PIPER_DOCKER_BIN", "docker")
PROJECT_PATH = os.environ.get("PROJECT_PATH", "")
REPO_ROOTS_FILE = os.path.join(common.PIPER_HOME, "repo-roots.json")   # repo_top: the recorded roots
WORKTREE_STACKS_FILE = os.path.join(common.PIPER_HOME, "worktree-stacks.json")   # stack_path: per-worktree main/own
# 9.54: a pending one-shot `hole start -r` - {at, session?}; hole's agent image is one for every session
IMAGE_REBUILD_FILE = os.path.join(common.PIPER_HOME, "image-rebuild.json")
APPROVAL_GRANTS_FILE = os.path.join(common.PIPER_HOME, "approval-grants.json")
APPROVAL_HISTORY_FILE = os.path.join(common.PIPER_HOME, "approval-history.json")
_REBUILD_LOCK = threading.Lock()
GIT_GUARD_FILE = os.path.join(common.PIPER_HOME, "git-guard.json")   # _git_guard: .git baselines per common dir
GIT_GUARD_INTERVAL = float(os.environ.get("PIPER_GIT_GUARD_INTERVAL", "1"))


def protected_what(path: str) -> str:
    """What a forbidden() dir is, for the blocked-start panel: why a sandbox must not mount it."""
    named = dict({common.SK: "Piper's own code",
                  common.PIPER_HOME: "Piper's state",
                  os.path.join(common.REAL_HOME, ".hole"): "hole's configuration",
                  os.path.join(common.REAL_HOME, ".claude"): "your Claude login",
                  os.path.join(common.REAL_HOME, ".codex"): "your Codex login"},
                 **{d: "a Piper extension" for d in extensions.DIRS}, **extensions.merged("protected_what"))
    return next((v for k, v in named.items() if k and os.path.realpath(k) == os.path.realpath(path)),
                "a directory Piper protects")
MAX_SESSIONS = int(os.environ.get("PIPER_MAX_SESSIONS", "6"))
PIPER_PORT = int(os.environ.get("PIPER_PORT", "17895"))
WATCH_INTERVAL = float(os.environ.get("PIPER_WATCH_INTERVAL", "30"))
# hole 2.1.0 ignores SIGTERM until it attaches to the agent (verified 2026-09-30: a Stop 2 s
# after the spawn was dropped, the sandbox came up and sat at Claude's trust dialog for
# hours). A stopping session is signalled again every STOP_RETRY s, killed after STOP_KILL_AFTER s.
STOP_RETRY = float(os.environ.get("PIPER_STOP_RETRY", "5"))            # env: tests/piper-e2e only
STOP_KILL_AFTER = float(os.environ.get("PIPER_STOP_KILL_AFTER", "30"))
STOP_KILL_AFTER_ATTACHED = 120.0   # hole took the TERM (the agent ran): its teardown gets longer
DROP_MAX = 10 << 20
EVENTS_CHUNK = 256 << 10   # _events: at most this much of the hook file per poll
DROP_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
PERSONAL_SETTINGS = os.path.join(common.REAL_HOME, ".hole", "settings.json")
MAVEN_NOTICE = "Maven settings not generated from"
# _git_ask_kind: what git_run in another project may run on a git_run_project card under write_guard (else write_exec)
_GIT_PROJECT_PLAIN = frozenset(("add", "commit", "push", "fetch", "branch", "tag"))
# ... but not their forms that move a ref the work tree follows, rewrite the server or name a host file by a flag value
_GIT_PLAIN_BAD_OPTS = frozenset(("--force", "--force-with-lease", "--force-if-includes", "--force-create", "--delete",
                                 "--mirror", "--all", "--prune", "--prune-tags", "--update-head-ok", "--refmap", "--chmod",
                                 "--pathspec-from-file", "--move", "--copy", "--set-upstream-to", "--edit-description"))
_GIT_PLAIN_BAD_SHORT = {"branch": "fdDmMcCu", "tag": "fd", "push": "fd", "fetch": "fu"}


def _git_plain(argv: list[str]) -> bool:
    """A plain add / commit / push / fetch / branch / tag (_git_ask_kind): no forced, deleting or ref-planting form."""
    sub, rest = gitops._sub(argv)
    if sub not in _GIT_PROJECT_PLAIN:
        return False
    for a in rest:
        if a.split("=", 1)[0] in _GIT_PLAIN_BAD_OPTS:
            return False
        if a.startswith("-") and not a.startswith("--") and set(a[1:]) & set(_GIT_PLAIN_BAD_SHORT.get(sub, "")):
            return False
        # a forced or deleting refspec; fetch into a named destination (refs/heads/* of the checked-out branch)
        if sub in ("push", "fetch") and not a.startswith("-") and (a.startswith(("+", ":")) or sub == "fetch" and ":" in a):
            return False
    return True

_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>78cDEHMNOZ]")
PHASES = [  # (stripped-stderr prefix, phase) in hole v2.1.0 start.go:Start order
    ("Launching sandbox for:", "launch"),
    ("Running setupHost hook", "hooks"),
    ("Creating sandbox networks", "networks"),
    ("Starting the Docker image cache", "cache"),
    ("Starting network gateway", "gateway"),
    ("Starting Docker-in-Docker sidecar", "dind"),
    ("Starting {agent} agent", "agent"),   # .format(agent=s.agent)
    ("Attaching to {agent} agent", "attach"),
]
TRUST_PROMPT = "Trust this project?"
UPDATE_PROMPT = "Update agents now? [Y/n/s = skip these versions]"
# Claude Code's own screen, matched with ALL whitespace removed (its TUI moves the cursor
# instead of printing spaces): a dialog (folder trust, bypass-permissions, ...) waits for
# Enter - typed text + Enter would pick its default ("No, exit"); the main screen takes input
CLAUDE_DIALOG = ("Entertoconfirm",)
CLAUDE_READY = ("ClaudeCodev", "forshortcuts", "shift+tabtocycle")
# Codex's TUI the same way: its folder trust / model migration / login screens wait for Enter
CODEX_DIALOG = ("Pressentertocontinue", "Doyoutrustthecontents", "Trustthisfolder?", "SigninwithChatGPT",
                "Hooksneedreview", "hooksneedreviewbeforetheycanrun", "hookneedsreviewbeforeitcanrun")
CODEX_READY = ("forshortcuts", "contextleft", "%used·", "AskCodextodoanything")   # 0.162+ footer / 0.164 empty prompt
# the agent CLI hole starts in the sandbox (`hole start <agent>`); the Global agent and the
# production check are Claude only
AGENTS = ("claude", "codex")
APPROVED_LOCK = threading.Lock()   # approved-settings.json is read-modify-written by approve / Add excludes / repair
ACTIVE = {"preflight", "preparing", "spawning", "trust", "starting", "attesting", "ready", "working",
          "waiting", "attention", "stopping"}
# the agent runs in a config dir built for this run: its login is current (not one carried from before)
RUNNING = ACTIVE - {"preflight", "preparing", "spawning"}
LOGIN_LOCK = threading.Lock()   # one login copy-back at a time: the template and <session>/*-auth.sha256 agree
# who asked for a session: the GUI, the Global agent, an inbox rule, a handoff, the production check,
# or `piper attach` in a native terminal
ORIGINS = ("gui", "advisor", "inbox", "handoff", "prod", "tty")
# the GUI polls /api/state every 1.5 s (6 s in a hidden tab); none for this long = it is closed,
# so piperd tells the terminal user itself (a desktop notification on the host)
GUI_GONE_S = 90   # a long-hidden tab: Chrome throttles its chained timers to one a minute
DESKTOP_NOTIFY = os.environ.get("PIPER_DESKTOP_NOTIFY", "1") != "0"


class Unreadable:
    """project_hidden() when the isolation policy cannot be read: hides everything (every caller
    fails closed), and says so - `unreadable` is the reason; the GUI shows one flag
    (policy_unreadable) instead of marking every file hidden (S2)."""

    def __init__(self, why: str):
        self.unreadable = why

    def __call__(self, rel: str) -> bool:
        return True


class HiddenInTranscript(ValueError):
    """import_conversation: the transcript's tool calls touched files the project hides (R2, 2026-10-10) -
    `paths` = their project-relative names, never content; piperd answers 409 hidden_paths."""

    def __init__(self, msg: str, paths: list[str]):
        super().__init__(msg)
        self.paths = paths


_PATH_SPLIT = re.compile(r"[\s\"'`=:,;()<>|&\[\]{}]+")
_PATH_KEYS = ("filePath", "file_path", "filenames", "path", "notebook_path")


def transcript_paths(cfg: str, rel: str, agent: str) -> tuple[set[str], set[str]]:
    """Path-like tokens of a transcript: (any token of a tool INPUT, a path field of a tool result or the
    user's / agent's message text - a mention of a hidden path counts, a false positive only asks once more;
    absolute tokens of tool OUTPUT text). Output text is file content - only its absolute paths (Grep / Glob
    listings) count, so a word of a hidden file's content never becomes a "name"."""
    inputs: set[str] = set()
    outputs: set[str] = set()

    def strings(v, depth=0):
        if depth > 12:
            return
        if isinstance(v, str):
            yield v[:200000]
        elif isinstance(v, dict):
            for x in v.values():
                yield from strings(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:2000]:
                yield from strings(x, depth + 1)

    def add(into: set, v, absolute_only=False):
        for text in strings(v):
            for tok in _PATH_SPLIT.split(text)[:20000]:
                if tok and len(tok) < 4096 and (not absolute_only or tok.startswith("/")):
                    into.add(tok)

    for _, rec in digest.iter_records(cfg, rel):
        if not isinstance(rec, dict):
            continue
        if agent == "codex":
            pl = rec.get("payload") if rec.get("type") == "response_item" else None
            if not isinstance(pl, dict):
                continue
            kind = str(pl.get("type") or "")
            if kind == "message":
                if digest.readable_record(rec, "", agent) is None:   # Piper's / Codex's injected context (developer
                    continue                                          # instructions list the hidden patterns themselves)
                add(inputs, [c.get("text") for c in pl.get("content") or [] if isinstance(c, dict)])
            elif kind.endswith("_output"):
                add(outputs, pl.get("output"), True)
            elif kind in ("function_call", "custom_tool_call", "local_shell_call"):
                for k in ("arguments", "input", "action"):
                    v = pl.get(k)
                    if isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            pass
                    add(inputs, v)
            continue
        tur = rec.get("toolUseResult")
        if isinstance(tur, dict):
            stack = [tur]
            while stack:
                d = stack.pop()
                for k, v in d.items():
                    if k in _PATH_KEYS:
                        add(inputs, v)
                    elif isinstance(v, dict) and len(stack) < 64:
                        stack.append(v)
                    else:
                        add(outputs, v, True)
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            add(inputs, content)
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                add(inputs, block.get("text"))
            elif block.get("type") == "tool_use":
                add(inputs, block.get("input"))
            elif block.get("type") == "tool_result":
                add(outputs, block.get("content"), True)
    return inputs, outputs


def policy_unreadable(rule) -> str:
    """The reason a project rule could not be built, "" for a working rule."""
    return str(getattr(rule, "unreadable", "") or "")


def claude_screen(text: str, agent: str = "claude") -> str:
    """'dialog' while the agent CLI shows a dialog after its last main-screen draw, 'ready' once
    the main screen came after every dialog, '' before either (still starting)."""
    flat = re.sub(r"\s+", "", text)
    dialog = max(flat.rfind(m) for m in (CODEX_DIALOG if agent == "codex" else CLAUDE_DIALOG))
    ready = max(flat.rfind(m) for m in (CODEX_READY if agent == "codex" else CLAUDE_READY))
    if dialog > ready:
        return "dialog"
    return "ready" if ready >= 0 else ""


def had_dialog(text: str, agent: str = "claude") -> bool:
    """Did the agent CLI show a dialog anywhere in `text` (claude_screen's markers)?"""
    flat = re.sub(r"\s+", "", text)
    return any(m in flat for m in (CODEX_DIALOG if agent == "codex" else CLAUDE_DIALOG))


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s).replace("\r", "")


def _worktree_glob(rel: str) -> str:
    """An exclude for a file in a Claude Code agent worktree (.claude/worktrees/agent-<hash>,
    a full checkout, a new one per agent run) names every worktree: the exact path would be
    stale with the next one and block the next start again."""
    parts = rel.split("/")
    if len(parts) > 3 and parts[:2] == [".claude", "worktrees"]:
        return "/".join([".claude", "worktrees", "*"] + parts[3:])
    return rel


def _glob_escape(rel: str) -> str:
    """A literal path as a policy glob: metacharacters match only themselves."""
    return re.sub(r"[*?[]", lambda m: "[[]" if m.group(0) == "[" else "[%s]" % m.group(0), rel)


# What the user can unlock for one project session while it runs (no sandbox restart: it is
# served by piperd over the MCP mailbox, whose tools every session already lists):
#   projects  list_projects / list_dir / read_file / grep on every checkout under PROJECT_PATH,
#             read-only, each under its own session's hidden rule (the advisor's tools)
#   projects_write  + edit_file / write_file into those checkouts; implies projects. EVERY write
#             is confirmed on a card showing the exact diff (workbench.agent_write)
#   git       git_run: any git command on the HOST checkout of this session. No command filter:
#             EVERY call is confirmed on a card showing the exact argv (gitops.session_run)
# Locked, the tools answer NOT_UNLOCKED before any card exists.
CODEX_LOGIN_FILE = "codex-sub-auth.sha256"   # a Claude session's Codex subagent login, host-side digest
# codex_in_claude: hole installs the Codex CLI under nvm's node 22 and runs it by its absolute path (command.json),
# never on PATH - a Claude session's shell (nvm default: another node) had no `codex` (exit 127). This wrapper
# under the box is included onto ~/.local/bin/codex (on PATH); no node version pinned here
CODEX_WRAPPER = ("piper/codex", "~/.local/bin/codex")
CODEX_WRAPPER_SH = """#!/bin/sh
# generated by piperd (codex_in_claude): the Codex CLI of hole's agent image, with the node it was installed for
if [ -n "${CODEX_SQLITE_HOME:-}" ]; then
  set -- -c "sqlite_home=\\"$CODEX_SQLITE_HOME\\"" "$@"
fi
set -- -c "notify=[]" "$@"
for c in "$HOME"/.nvm/versions/node/*/bin/codex; do
  [ -x "$c" ] && exec "${c%/codex}/node" "$c" "$@"
done
echo "codex: not in this sandbox's agent image - restart the session (Piper adds it with codex_in_claude)" >&2
exit 127
"""
UNLOCKS = ("projects", "projects_write", "git", "db_write_other")   # db_write_other: writes to another project's databases (dbtools.py), each on a card
# hole start options the user picks for one session in the GUI's new-session dialog. They
# shape the sandbox, so they change only while the session is stopped (its Isolation panel,
# set_start_opt) and apply at the next start. Only the GUI token sets them - never
# spawn_session / propose_session:
#   docker        --with-docker, the DinD sidecar (a privileged container)
#   unrestricted  -u, no egress filter (policy unrestricted_network does it for every session)
#   idea          IntelliJ's MCP as `idea` via idea-bridge.py (policy idea_in_sessions does it for every session)
START_OPTS = ("docker", "unrestricted", "idea")
PROJECT_READ_TOOLS = ("list_projects", "list_dir", "read_file", "grep")
GL_PATH_RX = settings.GL_READ_RX   # group/[sub/]repo - the same rule Settings -> GitLab validates with
# what a session knows about its sandbox and how it proposes a change of the setup (a card each, never auto-approved)
SETTINGS_TOOLS = ("piper_session", "piper_settings", "piper_settings_change", "piper_allow_domain", "piper_worktree_stack")
GONE_S = 10   # F17: how long the queue shows a card that went unanswered


def ask_why(kind: str) -> dict:
    """F17: the Settings › Security switch behind a card - its group and label in security.ASK_GROUPS ({} for a kind
    outside the tree)."""
    for g in security.ASK_GROUPS:
        for k, label in g["kinds"]:
            if k == kind:
                return {"group": g["title"], "label": label}
    return {}


ALWAYS_ASK = ("settings_change", "allow_domain", "http_prod", "http_host",   # http_prod: production, every time; http_host: a .http run to a host outside its environment
              # databases (dbtools.py): a test/prod write, a prod read, unmasking - never without the human
              "db_write_test", "db_write_prod", "db_read_prod", "db_unmask",
              # gitlab_job_play on a protected branch (policy protected_branches) - never by auto_approve
              "gitlab_job_protected",
              # Writes outside mounted writable libraries still require a card, even with projects unlocked.
              "git_run_project",
              # policy write_guard: a host-run file of another project (hook, .claude, CI, build) - never auto
              "write_exec", "http_runtime_code", "http_cert_passphrase", "http_auth_signin")
PROJECT_WRITE_TOOLS = ("edit_file", "write_file")
HTTP_TOOLS = ("http_env_set", "http_envs", "http_run", "http_last_result")   # .http files run on the host (httpclient.py)
DROP_GITLAB_MAX = 200 << 20   # gitlab_upload files kept in a session's drop dir
REPLAYABLE_READS = frozenset(("gitlab_issue", "gitlab_issues", "gitlab_mr", "gitlab_mrs", "gitlab_mr_changes",
                             "gitlab_pipelines", "gitlab_pipeline_jobs",
                             "github_issue", "github_issues", "github_pr", "github_prs", "github_pr_files",
                             "github_runs", "github_run_jobs"))
DB_TOOLS = dbtools.SESSION_TOOLS   # databases through piperd's drivers (dbtools.py), project sessions only
UNLOCKED_ASK_S = 600   # a card of an unlocked tool: 10 min, then git up to RUN_TIMEOUT - within the MCP's 15 min
NOT_UNLOCKED = ("%s is locked for this session. Ask the user to unlock it in the Piper GUI "
                "by selecting this session, opening the right-hand context panel, expanding Isolation, "
                "and clicking Unlock… next to %s (not in Settings). It applies at once, no restart.")


# Names of a session's box (host dir <box>, mounted in the sandbox) per layout. "orch": a sandbox
# started before the hole orchestrator was renamed to Piper - its running process keeps
# those paths, so they stay until its next start, which builds the "piper" layout.
LAYOUTS = {"piper": {"dir": "piper", "mount": ".piper", "events": "piper-events.jsonl",
                     "mailbox": "piper-mailbox", "drop": "piper-drop"},
           "orch": {"dir": "orch", "mount": ".orch", "events": "orch-events.jsonl",
                    "mailbox": "orch-mailbox", "drop": "orch-drop"}}


def asking_detail(detail: str) -> bool:
    """The detail of a session waiting on its first prompt; an extension adds the details its older versions stored."""
    return detail.startswith(("Claude is asking", "Codex is asking", "the task waits") + tuple(extensions.items("ASKING_DETAILS")))


class Session:
    FIELDS = ("id", "kind", "project", "path", "title", "task", "state", "phase", "phase_since", "detail", "created",
              "pid", "instance", "quiet_generation", "stack_since", "blocks", "attest", "rc", "origin", "claude_session", "gitlab",
              "profile", "hidden", "notice", "task_sent", "network", "watch",
              # persisted so a piperd restart keeps attesting and watching what hole mounted
              "coverage", "excludes", "mount_roots", "claude_mode",
              "since",   # when the state last changed (GUI: "waiting 3 min")
              "issue",   # 9.8: {key, gl, type, iid, title, web_url} of the GitLab inbox item it was opened from
              "turns",   # 9.7 #2: [{n, prompt, started, ended, from, to}] snapshot trees per turn
              "review_pending", "review_summary", "reverts", "revert_note",
              "settings", "texts",   # the blocked settings files' hashes/texts: approve() after a restart
              "parent",   # handoff: id of the project session whose propose_session opened this one
              "handoff",  # handoff: {summary, reported, at, state, rc, facts} for the parent's handoff_result
              "handoff_notify",   # handoff: child ids the parent still has to be told about
              "idle",   # Claude sits at its prompt: after Stop, also through an idle_prompt Notification
              "submit_at", "clear_at",   # confirmed input boundaries survive controller restarts
              "subagents",   # native Claude / Codex agents still executing, preserved across piperd restarts
              "background_tasks",   # Claude shell/monitor tasks indexed from this session's transcript
              "background_since",   # current agent process: old resumed tasks are no longer running
              "unlock",   # {projects}: what the user unlocked for this session at runtime (GUI, warned)
              "approvals",   # card scopes explicitly allowed for this session, including environment class
              "gitlab_extra",   # GitLab projects outside s.gitlab allowed for this session
              "gitlab_auto_extra",   # subset granted by global policy, not by the human; rechecked on use
              "github",   # W23: the GitHub repositories at the start, while the module was on (GUI Network row)
              "opts",   # {docker, unrestricted, idea}: START_OPTS the user picked when opening it (GUI only)
              "notif_ts", "stop_ts",   # 9.12 E: the last Notification / Stop hook event - one prompt = one message, across restarts
              "layout",   # LAYOUTS key of the box the running process uses
              "hole_legacy",   # running Hole <2.4: settings location and library-exclude semantics
              "hole_home",   # pinned host/container HOME: old sessions keep their original paths on resume
              "security_sandbox_rev", "runtime", "agent",   # AGENTS: the CLI hole starts (claude_session holds Codex's thread id too)
              "host_claude",   # the sandbox has the real ~/.claude (Manager.host_claude, set at start)
              "host_codex",   # the sandbox has the real ~/.codex: a Codex session or a Claude one's Codex
              #               subagent under policy codex_dir=host (Manager.codex_subagent, set at start)
              "title_source", "native_title", "fork_from", "fork_pending", "import_from",
              "switched_from",   # 9.140: {session, agent, conversation, title, worktree?, branch?} - continued from the other agent's session
              "compact_first",   # W8: type /compact before the pending task (a resumed issue session)
              "compact_wait",   # W8: when /compact was typed; the task waits for SessionStart(compact)
              "rebuild",   # 9.54: this start took the pending image rebuild (hole -r)
              "interrupted")   # 9.141: {at, was, cause, resumes} - ptyhost lost it (a restart); cleared by a start / Remove
    LISTED = tuple(f for f in FIELDS if f not in ("turns", "reverts", "revert_note", "settings", "texts", "handoff", "handoff_notify", "idle",
                                                  "notif_ts", "stop_ts", "layout", "hole_home", "hole_legacy", "native_title",
                                                  "submit_at", "clear_at"))   # /api/state (turns: GET .../turns)

    def __init__(self, **kw):
        self.id = kw.get("id") or secrets.token_hex(4)
        self.quiet_generation = str(kw.get("quiet_generation") or "")[:40]
        # when this runtime's dev stack access was granted (an extension's session_up): a job the
        # provider attributes to it and started later is its own (quiet windows)
        ts = kw.get("stack_since")
        self.stack_since = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0
        self.kind = kw.get("kind", "project")
        self.project = kw.get("project", "")
        self.path = kw.get("path", "")
        self.title = kw.get("title") or self.project
        self.title_source = kw.get("title_source") or ""
        self.native_title = kw.get("native_title") or ""   # last observed /rename; a GUI rename survives a replay
        self.fork_from = kw.get("fork_from") if isinstance(kw.get("fork_from"), dict) else None
        self.import_from = kw.get("import_from") if isinstance(kw.get("import_from"), dict) else None
        self.switched_from = kw.get("switched_from") if isinstance(kw.get("switched_from"), dict) else None
        self.fork_pending = kw.get("fork_pending") is True
        self.task = kw.get("task") or ""
        self.state = kw.get("state", "preflight")
        self.phase = kw.get("phase", "")
        self.phase_since = kw.get("phase_since", kw.get("since", time.time()))
        self.detail = kw.get("detail", "")
        self.created = kw.get("created", time.time())
        self.pid = kw.get("pid")
        self.instance = kw.get("instance")
        self.blocks = kw.get("blocks") or []
        self.security_sandbox_rev = kw.get("security_sandbox_rev", 0)
        self.attest = kw.get("attest")
        self.rc = kw.get("rc")
        self.origin = kw.get("origin", "gui")
        self.claude_session = kw.get("claude_session")
        self.gitlab = kw.get("gitlab") or []
        self.github = [x for x in kw.get("github") or [] if isinstance(x, str) and x]
        self.profile = kw.get("profile")
        self.hidden = kw.get("hidden", False)
        self.notice = kw.get("notice", "")
        self.task_sent = kw.get("task_sent", False)
        self.compact_first = kw.get("compact_first") is True
        cw = kw.get("compact_wait")
        self.compact_wait = float(cw) if isinstance(cw, (int, float)) and not isinstance(cw, bool) and math.isfinite(cw) else 0.0
        self.network = kw.get("network") or {}
        self.watch = kw.get("watch") or []
        self.coverage = kw.get("coverage") or []
        self.excludes = kw.get("excludes") or []
        self.mount_roots = kw.get("mount_roots") or []
        # "session": its own ~/.claude (default) · "host": the host's ~/.claude (policy claude_dir=host)
        # · "none": a Codex session - no ~/.claude at all, its box is ~/.piper as with "host"
        self.claude_mode = kw.get("claude_mode") or "session"
        self.since = kw.get("since") or self.created
        self.issue = kw.get("issue") if isinstance(kw.get("issue"), dict) else None
        self.turns = kw.get("turns") or []
        self.review_pending = bool(kw.get("review_pending"))
        self.review_summary = kw.get("review_summary") or {}
        self.reverts = kw.get("reverts") or []
        self.revert_note = kw.get("revert_note") or ""
        self.turn_action_lock = threading.RLock()
        self.parent = kw.get("parent") or None
        self.handoff = kw.get("handoff") if isinstance(kw.get("handoff"), dict) else None
        self.handoff_notify = [x for x in kw.get("handoff_notify") or [] if isinstance(x, str)]
        self.idle = bool(kw.get("idle"))
        agents = kw.get("subagents")
        self.subagents = {k: v[:80] for k, v in list(agents.items())[:256]
                          if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", k) and isinstance(v, str)} \
            if isinstance(agents, dict) else {}
        tasks = kw.get("background_tasks")
        self.background_tasks = {k: v for k, v in list(tasks.items())[:256]
                                 if isinstance(k, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", k)
                                 and v in ("shell", "monitor")} if isinstance(tasks, dict) else {}
        since = kw.get("background_since")
        self.background_since = float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) and math.isfinite(since) else 0.0
        self.unlock = {k: True for k in UNLOCKS if (kw.get("unlock") or {}).get(k) is True} \
            if isinstance(kw.get("unlock"), dict) else {}
        self.approvals = sorted({x for x in kw.get("approvals", []) if isinstance(x, str)}) \
            if isinstance(kw.get("approvals"), list) else []
        self.gitlab_extra = [x for x in kw.get("gitlab_extra") or [] if isinstance(x, str) and x]
        # Older metadata did not distinguish automatic scopes from human grants. Reconfirm those
        # once when cards are on; explicit general session approvals below remain authoritative.
        self.gitlab_auto_extra = [x for x in kw.get("gitlab_auto_extra", self.gitlab_extra) or [] if x in self.gitlab_extra]
        self.opts = {k: True for k in START_OPTS if (kw.get("opts") or {}).get(k) is True} \
            if isinstance(kw.get("opts"), dict) else {}
        self.notif_ts = float(kw.get("notif_ts") or 0.0) if isinstance(kw.get("notif_ts"), (int, float)) else 0.0
        self.stop_ts = float(kw.get("stop_ts") or 0.0) if isinstance(kw.get("stop_ts"), (int, float)) else 0.0
        self.layout = kw.get("layout") if kw.get("layout") in LAYOUTS else "piper"
        self.hole_home = kw.get("hole_home") or common.REAL_HOME
        self.hole_legacy = bool(kw.get("hole_legacy", False))
        self.runtime = directruntime.validate(kw.get("runtime"))
        self.agent = kw.get("agent") if kw.get("agent") in AGENTS else "claude"
        self.host_claude = bool(kw.get("host_claude"))
        self.host_codex = bool(kw.get("host_codex"))
        self.rebuild = bool(kw.get("rebuild"))
        self.interrupted = kw.get("interrupted") if isinstance(kw.get("interrupted"), dict) else None
        # runtime only
        self.ev_live = False   # _events caught up with the hook file: what it reads now is written now
        self.screen_wait = False   # ready with no task: the screen is watched until Claude's prompt is up
        self.turn_q: list = []
        self.turn_busy = False
        self.turn_seen = 0.0
        self.fsedits: deque = deque(maxlen=workbench.FSEDITS_MAX)   # {path, ts} the agent edited
        self.fsedit_seq = 0       # ... bumped per edit event; /api/state "fsedit" (Workbench.fsedit_state)
        self.fsedit_unknown = 0   # ... the seq of the last edit whose file is not known
        self.snap_lock = threading.Lock()
        self.text = ""            # stripped startup output, until attestation
        self.trust_seen = False
        self.dialog_done = False  # _screen: the keys that answered a startup dialog were cleared
        self.git_note = ""        # _preflight: why git cannot write in this sandbox (told the agent)
        self.settings = kw.get("settings") or {}
        self.texts = kw.get("texts") or {}
        self.events_off = 0
        self.codex_permissions: dict[str, int] = {}   # rebuilt by replaying hooks, never stores tool arguments
        self.codex_review: dict = {}   # own rollout cursor and reviewer only; never transcript text
        self.events_lock = threading.Lock()   # _events reads at events_off and moves it: one reader at a time (N9);
        # taken before s.lock (_fsedit_event), never while s.lock / cancel_lock / Manager.lock is held
        self.cancel = False       # Stop before hole was spawned: _start ends at its next step
        self.cancel_lock = threading.Lock()   # stop() vs _start's pid assignment
        self.stop_first = 0.0     # _stop_nudge: when this process was first signalled to stop
        self.stop_kick = 0.0      # ... and last (0: not yet, e.g. found stopping after a piperd restart)
        self.stop_attached = False   # stopped while the agent ran: hole is attached, TERM is not dropped
        self.resume = kw.get("resume") or ((self.fork_from or {}).get("conversation") if self.fork_pending else None)
        self.lock = threading.Lock()
        self.hook_problems: list[str] = []   # guard.hook_problems of this start (diagnostics only)
        self.agent_log: threading.Thread | None = None   # _capture_agent_log of this start
        self.handoff_sending = False
        self.hold = ""            # 9.12: automatic text waits for the human's half-written prompt
        self.hold_try = 0.0       # the last check of it (the poll loop retries every 2 s)
        for name in ("submit_at", "clear_at"):
            value = kw.get(name, 0.0)
            setattr(self, name, float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 0 <= value <= time.time() else 0.0)
        self.gui_sent_at = 0.0    # the human's "Send" from the card / Composer (send())
        self.tty = 0              # a native terminal is attached (`piper attach`; ptyhost's count)
        self.tty_used = self.origin == "tty"   # ... or was, in this piperd's life: it gets desktop notifications
        self.desk_state = ""      # the state its last desktop notification was about

    @property
    def dir(self) -> str:
        return os.path.join(common.SESSIONS_DIR, self.id)

    @property
    def hole_settings(self) -> str:
        """Trusted global settings for this start; outside every sandbox mount."""
        return (os.path.join(self.hole_dir, "settings.json") if self.hole_legacy else
                os.path.join(self.dir, "hole-settings.json"))

    @property
    def hole_dir(self) -> str:
        """Hole's registry, trust, temporary mounts and logs for this session's HOME."""
        return os.path.join(self.hole_home, ".hole")

    @property
    def cfg(self) -> str:
        return os.path.join(self.dir, "claude")

    @property
    def box(self) -> str:
        """Host dir of Piper's pieces (mailbox, events, drop, MCP server)."""
        return self.cfg if self.claude_mode == "session" else os.path.join(self.dir, self.names["dir"])

    @property
    def names(self) -> dict:
        return LAYOUTS[self.layout]

    @property
    def claude_src(self) -> str:
        """Host dir that is the sandbox's ~/.claude (transcripts for the digest)."""
        if self.claude_mode == "none":
            return self.codex_home   # Codex rollouts (validate_conversation), the real ~/.codex under codex_dir=host
        return self.cfg if self.claude_mode == "session" else os.path.join(common.REAL_HOME, ".claude")

    @property
    def codex_dir(self) -> str:
        """A Codex session's own CODEX_HOME (codexcfg), the sandbox's ~/.codex."""
        return os.path.join(self.dir, "codex")

    @property
    def codex_home(self) -> str:
        """Host dir mounted as the sandbox's ~/.codex: its own, or the real one (codex_dir=host) - for a Codex
        session and for a Claude session's Codex subagent alike."""
        return os.path.join(common.REAL_HOME, ".codex") if self.host_codex else self.codex_dir

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in self.FIELDS}

    def save(self) -> None:
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        write_json(os.path.join(self.dir, "meta.json"), self.to_json())


class Manager(*extensions.mixins(), quietwindow.QuietWindows, workbench.Workbench, inbox.Inbox, issuestate.IssueState, prodcheck.ProdCheck, timesheet.Timesheet,
              feed.Feed, httpclient.HttpRunner, dbtools.DbTools):
    def __init__(self, connect_fn=None, *, background: bool = True):
        """background=False initializes state without connecting or starting services.

        connect_fn(on_event, on_close) -> PtyClient; default: the ptyhost socket in $PIPER_HOME."""
        self._background_enabled = background
        self._stop_event = threading.Event()
        self._service_lock = threading.RLock()
        self._services: list[threading.Thread] = []
        self._connect_fn = connect_fn or (lambda on_event, on_close: connect(
            common.PTY_SOCK, on_event=on_event, on_close=on_close))
        self.sessions: dict[str, Session] = {}
        self.pending: dict[str, dict] = {}
        self._gl_read_asks: dict[tuple, dict] = {}   # (session, GitLab project) -> the one card being asked
        self.lock = threading.RLock()
        # Serialize shared agent/trust preparation until hole has loaded it. Each session has its own
        # HOLE_SETTINGS_FILE, so its Maven includes cannot be replaced by another start.
        self._spawn_lock = threading.Lock()
        self._spawn_owner: str | None = None   # the session id holding it
        self._mail_busy: set[str] = set()
        self.version = 0
        self.pty: PtyClient | None = None
        self.policy = guard.load_policy(common.POLICY_FILE)
        gitops.FAIL_CLOSED = bool(self.policy.get("git_fail_closed"))
        self._files_cache: dict = {}   # GUI quick open: project key -> {all: (time, files)}
        self._conv_cache: dict = {}    # session id -> ((transcript, mtime), conversation)
        self._board_lock = threading.Lock()
        self._board_cache: dict = {}
        self._board_busy = False
        self._board_due = 0.0
        self._conversation_index = None
        self._conversation_lock = threading.Lock()
        self._switch_lock = threading.Lock()   # 9.140: switch_agent's in-flight sources
        self._switching: set[str] = set()
        self._background_cache: dict = {}   # per-session transcript cursor and IDs, never commands/output
        self._title_cache: dict = {}   # native rename cursors (Claude transcript / Codex session index)
        self._git_count: dict = {}     # session id -> (time, changed files)
        self._roots: dict | None = None   # repo_top: project realpath -> recorded root ("" = none)
        self._roots_audited: dict = {}      # project -> the last audited answer (_audit_once)
        self._roots_lock = threading.Lock()
        self._interrupted_lock = threading.Lock()   # 9.141: interrupted.json (boot + the notice seen)
        self._server_blobs: set[tuple[str, str]] = set()   # (GitLab project, blob oid) found there - _on_server
        self._guard: dict | None = None    # _git_guard: common dir -> baseline (git-guard.json)
        self._guard_sig: dict = {}          # common dir -> the signature last found clean
        self._guard_live: set = set()       # common dirs a sandbox could write at the last check
        self._guard_lock = threading.RLock()
        self._wb_init()                # 9.7 workbench state (workbench.py)
        self._inbox_init()             # 9.8 GitLab inbox (inbox.py)
        self._prod_init()              # 9.12 C morning production check (prodcheck.py)
        self._ts_init()                # 9.12 A timesheet (timesheet.py)
        self._feed_init()              # 9.14 Feed, off by default (feed.py)
        self._http_init()              # .http runs on the host (httpclient.py)
        self._db_init()                # databases (dbtools.py)
        self.hosts = hostterm.HostTerminals(lambda: self.pty, self.bump, lambda: features.on("host"))   # W2 host shells (D10: a module)
        # ptyclient delivers events on its reader thread, which must never block on a
        # pty.call (the reply would arrive on that same thread) - so events are queued.
        self._events_q: queue.Queue = queue.Queue()
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        self._quiet_init()
        self.gui_polled_at = time.time()
        self.gui_visible_at = 0.0
        self.mm = mattermost.Notifier(self)
        self.prod_notifiers.append(self._prod_to_mm)
        extensions.call("init", self)   # 9.150: an extension's own state
        if background:
            try:
                self._load()
                self._start_service(self._event_loop, "piper-pty-events")
                self._connect()
                self._start_service(self._poll_loop, "piper-poll")
                self._sync_watch()
                self._start_service(self._startup_progress_loop, "piper-start-progress")
                self._start_service(self._startup_upkeep, "piper-upkeep")
                self._start_service(self._guard_loop, "piper-gitguard")
                self._db_start_reaper()
                self._inbox_start_thread()
                self._prod_start_thread()
                self.mm.start()
                self._feed_start_thread()
                extensions.call("start", self)
            except BaseException:
                self.close()
                raise

    def _start_service(self, target, name: str, args=()):
        """Register background work before starting it; close excludes subsequent starts."""
        with self._service_lock:
            if not self._background_enabled or self._stop_event.is_set():
                return None
            def run():
                try:
                    target(*args)
                finally:
                    with self._service_lock:
                        self._services.remove(threading.current_thread())
            t = threading.Thread(target=run, name=name, daemon=True)
            self._services.append(t)
            t.start()
            return t

    def close(self, timeout: float = 10.0) -> None:
        """Stop this manager's services and connections, preserving the independent PTY host."""
        with self._service_lock:
            self._stop_event.set()
            self._watch_stop.set()
            self._events_q.put(None)
        self.mm.close()
        if self.pty is not None:
            self.pty.close()
        deadline = time.monotonic() + timeout
        with self._service_lock:
            threads = list(self._services)
        for t in threads:
            if t is not threading.current_thread():
                t.join(max(0, deadline - time.monotonic()))
        for w in list(self._db_workers.values()):
            w.kill()
        alive = [t.name for t in threads if t.is_alive() and t is not threading.current_thread()]
        if alive:
            raise RuntimeError("manager services did not stop: " + ", ".join(alive))

    # ------------------------------------------------------------ plumbing
    def bump(self) -> None:
        with self.lock:
            self.version += 1

    def _connect(self) -> None:
        client = self._connect_fn(self._events_q.put, self._on_pty_close)
        with self._service_lock:
            if self._stop_event.is_set():
                client.close()
                return
            self.pty = client
        alive = {s["id"]: s for s in self.pty.call("list")["sessions"]}
        self.hosts.reconcile(alive)   # W2: the host shells (not sessions)
        rebooted = self._boot_changed()
        for s in list(self.sessions.values()):
            info = alive.get(s.id)
            if info:
                s.tty = int(info.get("tty") or 0)   # ptyhost v8; an older one has no TTY clients
                s.tty_used = s.tty_used or bool(s.tty)
            if info and info["alive"]:
                s.pid = s.pid or info.get("pid")   # piperd died between spawn and saving the pid
                if s.state in ("spawning", "starting", "trust"):
                    # piperd restarted mid-start: re-read the whole output to find the phase
                    s.text, s.trust_seen = "", False
                    self.pty.call("subscribe", id=s.id, **{"from": 0})
                    continue
                if self._task_pending(s) and s.state in ("attesting", "ready", "attention"):
                    # the task is not typed yet: re-read the screen to know when it can be
                    s.text = ""
                    self.pty.call("subscribe", id=s.id, **{"from": 0})
                    if s.state == "attesting":
                        self._reattest(s)
                    continue
                if s.runtime == "direct":
                    # Native login failures can appear during any turn, including
                    # after a controller update. Keep future output, without replaying
                    # old error messages over the persisted activity state.
                    self.pty.call("subscribe", id=s.id, **{"from": info.get("bytes", 0)})
                    continue
                # past the start piperd needs only the exit event, never the output stream
                # (a stream nobody reads would fill ptyhost's per-client buffer)
                self.pty.call("subscribe", id=s.id, data=False)
                if s.state == "attesting":
                    self._reattest(s)
                elif s.state in ("stopping",) or s.detail == "ptyhost connection lost":
                    s.detail = ""
                    s.save()
            elif s.state in ACTIVE:
                was = s.state
                s.state = "exited"
                s.detail = ("lost (computer restarted)" if rebooted else "lost (ptyhost restarted)") if s.pid \
                    else "start interrupted (piperd restarted)"
                # 9.141: what it was doing, for the queue's Interrupted section and the one-time restore card;
                # a session the user was stopping, a hidden one and the Global agent / production check are not offered
                if was != "stopping" and s.kind == "project" and not s.hidden:
                    s.interrupted = {"at": time.time(), "was": was,
                                     "cause": "reboot" if rebooted else "ptyhost" if s.pid else "piperd",
                                     "resumes": self._resumable(s)}
                s.save()
        self.bump()

    def _boot_changed(self) -> bool:
        """9.141: True when the machine booted since the last connect (common.boot_id differs from the one
        stored then); the current boot is stored either way. Unknown boots never count as a restart."""
        boot = common.boot_id()
        with self._interrupted_lock:
            rec = read_json(common.INTERRUPTED_FILE, {})
            rec = rec if isinstance(rec, dict) else {}
            prev = rec.get("boot")
            if boot and prev != boot:
                try:
                    write_json(common.INTERRUPTED_FILE, dict(rec, boot=boot))
                except OSError as e:
                    audit({"event": "interrupted-store-failed", "error": str(e)})
        return bool(boot and isinstance(prev, str) and prev and prev != boot)

    def _resumable(self, s: Session) -> bool:
        """Continue (retry) of this exited session resumes its conversation; False = it starts again with its task."""
        if not s.claude_session or not self._has_transcript(s, s.claude_session):
            return False
        # _start drops a resume across the stricter switch to per-session dirs (retry says it is lost)
        return not (s.agent != "codex" and s.claude_mode == "host" and self.claude_dir() == "session" or
                    s.agent == "codex" and s.host_codex and self.codex_dir() != "host")

    def interrupted_notice(self) -> dict | None:
        """9.141 /api/state `interrupted_notice`: the sessions a restart interrupted that the GUI has not offered
        yet - {id, cause, at, limit, seen, sessions: [{id, title, project, was, at, resumes, issue}]}; id = the newest
        mark's time, POST seen with it and only a later interruption opens it on its own again (seen: the section's
        Restore… still offers it). Handoff children follow their parent."""
        with self.lock:
            marked = [s for s in self.sessions.values() if s.interrupted and s.state == "exited" and not s.hidden
                      and s.kind == "project" and not s.parent]
        if not marked:
            return None
        newest = max(float(s.interrupted.get("at") or 0) for s in marked)
        with self._interrupted_lock:
            seen = read_json(common.INTERRUPTED_FILE, {})
        shown = isinstance(seen, dict) and isinstance(seen.get("seen"), (int, float)) and seen["seen"] >= newest
        last = max(marked, key=lambda s: float(s.interrupted.get("at") or 0))
        return {"id": newest, "cause": last.interrupted.get("cause") or "ptyhost", "at": newest,
                "limit": self.max_sessions(),   # the card offers only the free slots (_check_limit: no queue)
                "seen": shown,
                "sessions": [{"id": s.id, "title": s.title, "project": s.project, "issue": s.issue,
                              "was": s.interrupted.get("was"), "at": s.interrupted.get("at"),
                              "since": s.since, "resumes": bool(s.interrupted.get("resumes"))}
                             for s in sorted(marked, key=lambda x: -(x.since or x.created))]}

    def interrupted_seen(self, notice_id) -> dict:
        """The GUI showed the card (once, whichever tab or device opened it first)."""
        if not isinstance(notice_id, (int, float)) or isinstance(notice_id, bool) or not math.isfinite(notice_id):
            raise ValueError("seen must be the notice id")
        with self._interrupted_lock:
            rec = read_json(common.INTERRUPTED_FILE, {})
            rec = rec if isinstance(rec, dict) else {}
            write_json(common.INTERRUPTED_FILE, dict(rec, seen=max(float(notice_id), float(rec.get("seen") or 0))))
        self.bump()
        return {"ok": True}

    def interrupted_dismiss(self, ids) -> dict:
        """Dismiss: the marks go (the sessions stay among the ended ones); ids = a list, or "all"."""
        if ids != "all" and not (isinstance(ids, list) and all(isinstance(x, str) for x in ids)):
            raise ValueError('ids must be a list of session ids or "all"')
        n = 0
        with self.lock:
            for s in self.sessions.values():
                if s.interrupted and (ids == "all" or s.id in ids):
                    s.interrupted = None
                    s.save()
                    n += 1
        audit({"event": "interrupted-dismiss", "sessions": n})
        self.bump()
        return {"dismissed": n}

    def _on_pty_close(self) -> None:
        if self._stop_event.is_set():
            return
        # The control connection dropped. Either ptyhost died (its sandboxes with it) or only
        # this connection did: _connect() after reconnecting asks ptyhost which sessions are
        # alive and marks only the others exited.
        for s in list(self.sessions.values()):
            if s.state in ACTIVE:
                s.detail = "ptyhost connection lost"
        self.bump()
        self._start_service(self._reconnect, "piper-reconnect")

    def _reconnect(self) -> None:
        # never give up: marking the sessions exited while ptyhost (and its sandboxes) may be
        # alive would hide them; a successful reconnect lists what is really alive
        for i in itertools.count():
            if self._stop_event.wait(0.5 if i < 50 else 5):
                return
            try:
                self._connect()
                return
            except (OSError, PtyHostError):
                if i == 50:
                    for s in list(self.sessions.values()):
                        if s.state in ACTIVE:
                            s.detail = "ptyhost unreachable - retrying"
                    self.bump()

    def _load(self) -> None:
        for meta in glob.glob(os.path.join(common.SESSIONS_DIR, "*", "meta.json")):
            data = read_json(meta, {})   # a broken one is reported (and kept as .corrupt) there
            if not isinstance(data, dict) or not data.get("id"):
                sid = os.path.basename(os.path.dirname(meta))
                if not re.fullmatch(r"[0-9a-f]{8}", sid):
                    continue
                # listed, not skipped: its hole may still run in ptyhost - Stop / Remove from list stay possible
                data = {"id": sid, "project": "?", "state": "error", "layout": "piper",
                        "detail": "meta.json was broken (kept as meta.json.corrupt) - stop it if it runs, then remove it"}
            try:
                # An intermediate piperd may have pinned HOME for an old, still-running Hole.
                # Only a 2.4+ start wrote the explicit per-session settings document.
                legacy = data.get("hole_home", common.HOLE_HOME) != common.REAL_HOME and not os.path.isfile(
                    os.path.join(os.path.dirname(meta), "hole-settings.json"))
                s = Session(**dict({"layout": "orch", "hole_home": common.HOLE_HOME,
                                    "hole_legacy": legacy}, **data))
                # Missing layout: before the rename. Missing hole_home: the isolated HOME before 15.41.
            except TypeError:
                continue
            self.sessions[s.id] = s
        self._repair_foreign_prompt_links()

    @staticmethod
    def _prompt_labels(s: Session) -> dict:
        prompts = [str(t.get("prompt") or "").strip() for t in s.turns
                   if t.get("human") is not False and not str(t.get("prompt") or "").lstrip().startswith("<task-notification>")]
        prompts = [p for p in prompts if p]
        return {"first_prompt": (s.task.strip() or (prompts[0] if prompts else ""))[:400],
                "last_prompt": (prompts[-1] if prompts else s.task.strip())[:400]}

    def list(self) -> list[dict]:
        with self.lock:
            rows = [({k: getattr(s, k) for k in Session.LISTED}, s)
                    for s in sorted(self.sessions.values(), key=lambda x: x.created) if not s.hidden and not s.id.startswith("provider-")]
        return [dict(row, **self._prompt_labels(s), approval_grants=self.approval_grants(s), branch=self._branch(s), hold=s.hold if s.state in ACTIVE else "",   # an ended one has no prompt
                     tty=s.tty,   # attached in a native terminal, which sizes the PTY
                     **({"fsedit": f} if (f := self.fsedit_state(s)) else {}),   # files the agent edited
                     alive=s.state in ACTIVE,   # `piper attach` picks from these
                     at_prompt=self._istate_at_prompt(s)) for row, s in rows]   # piperd may type into it (follow-up, issue status)   # file reads: outside the lock

    def _branch(self, s: Session) -> str:
        """The checkout's branch from its RECORDED git dir (repo_top) - never from a .git the
        sandbox could have planted in the project, and without running git; "" until recorded."""
        if s.kind != "project" or not s.path:
            return ""
        with self._roots_lock:
            if self._roots is None:
                self._roots = read_json(REPO_ROOTS_FILE, {})
            rec = self._roots.get(os.path.realpath(s.path))
        return gitops.head_branch(rec["gitdir"]) if isinstance(rec, dict) and rec.get("gitdir") else ""

    def reload_policy(self) -> None:
        """policy.json changed in Settings (9.13): new sessions and the L5 watcher use it."""
        prior_policy = dict(getattr(self, "policy", None) or {})
        prior_ask = prior_policy.get("ask")
        prior_masking = (getattr(self, "policy", None) or {}).get("db_masking") is not False
        self.policy = guard.load_policy(common.POLICY_FILE)
        if prior_masking != (self.policy.get("db_masking") is not False) or \
                prior_policy.get("security_strict") != self.policy.get("security_strict"):
            self._db_invalidate_agent_results()
        if prior_ask != self.policy.get("ask") or prior_policy != self.policy:
            for session in getattr(self, "sessions", {}).values():
                session.approvals = []
                session.save()
            self._db_ok.clear()
            self._http_session_ok.clear()
        gitops.FAIL_CLOSED = bool(self.policy.get("git_fail_closed"))
        with self.lock:
            for pid, item in list(self.pending.items()):
                if not item["_ev"].is_set() and self.auto_ok(item["kind"], item.get("payload")):
                    # Disabling a card is not approval of an already waiting operation.
                    item["_cancelled"] = "security control disabled"
                    item["_ev"].set()
                    self.pending.pop(pid, None)
        self._sync_watch()
        self.bump()

    def rename(self, sid: str, title: str) -> None:
        s = self.get(sid)
        if s.kind == "advisor":
            raise ValueError("the Global agent cannot be renamed")
        title = " ".join(common.no_bidi(str(title)).split())
        if not title or len(title) > 200:
            raise ValueError("title must be 1..200 characters")
        title = common.scrub(title)   # L1, as in create()
        # Drain older CLI records before saving the newer GUI choice, including large histories.
        while True:
            before = self._title_cache.get(s.id, {}).get("off", 0)
            self._sync_title(s)
            if self._title_cache.get(s.id, {}).get("off", 0) <= before:
                break
        s.title = title
        s.title_source = "manual"
        s.save()
        audit({"event": "rename", "session": s.id, "title": title})
        self.bump()

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None or s.hidden:
            raise KeyError(sid)
        return s

    def _set(self, s: Session, state: str | None = None, phase: str | None = None, detail: str | None = None
             ) -> None:
        with s.lock:
            if state is not None:
                if state != s.state:
                    s.since = time.time()
                s.state = state
            if phase is not None:
                if phase != s.phase:
                    s.phase_since = time.time()
                s.phase = phase
            if detail is not None:
                s.detail = detail
            s.save()
        self.bump()

    # ------------------------------------------------------------ create
    def create(self, project_key: str, task: str = "", title: str = "", origin: str = "gui",
               issue: dict | None = None, parent: str | None = None, opts: dict | None = None,
               agent: str = "claude", _fork_source: Session | None = None, image_rebuild: bool = False,
               _switched: tuple[dict, bytes] | None = None, _replaces: Session | None = None,
               runtime: str | None = None) -> Session:
        """`opts`: START_OPTS for this session - the GUI's dialog only (piperd checks the token).
        `agent`: AGENTS, the CLI hole starts - Claude for the Global agent.
        `image_rebuild`: the dialog's "Rebuild image at this start" (9.54) - reserved for this session.
        `_switched`: (switched_from, handoff document) of switch_agent (9.140) - kept host-side in the session dir,
        placed into the sandbox at every start (_place_handoff); `_replaces`: its source, stopped for it - not
        counted against the session limit."""
        runtime = directruntime.validate(runtime if runtime is not None else os.environ.get("PIPER_DEFAULT_RUNTIME", "hole"))
        if runtime == "direct" and (opts or image_rebuild):
            raise ValueError("Hole options and image rebuild do not apply to direct sessions")
        opts = opts or {}
        if agent not in AGENTS:
            raise ValueError("unknown agent %r (one of %s)" % (agent, ", ".join(AGENTS)))
        available = features.session_agents()
        if available is not None and not available.get(agent):
            raise ValueError(f"{agent.title()} is not installed or executable on this server. Install it in the service PATH; Piper detects it automatically.")
        bad = sorted(k for k in opts if k not in START_OPTS)
        if bad:
            raise ValueError("unknown start option(s) %s (one of %s)" % (", ".join(bad), ", ".join(START_OPTS)))
        if not all(isinstance(v, bool) for v in opts.values()):
            raise ValueError("start options must be true/false")
        opts = {k: True for k, v in opts.items() if v}
        if runtime == "hole" and origin not in ("gui", "tty") and not opts and project_key != "advisor" and \
                self.policy.get("automation_start_opts") is True:   # 9.46 automation_opts off: the dialog's defaults
            opts = {k: True for k in self.start_opts_default()}
        if origin not in ORIGINS:
            raise ValueError("unknown origin %r" % origin)
        if project_key == "advisor" and opts:   # the Global agent reads every checkout: never with DinD / hole -u
            raise ValueError("the Global agent takes no start options (%s)" % ", ".join(sorted(opts)))
        if project_key == "advisor" and agent != "claude":
            raise ValueError("the Global agent runs Claude only")
        self._check_limit(_replaces)
        if len(task) > 20000 or len(title) > 200:
            raise ValueError("task/title too long")
        # titles reach other Claudes (shared-tree note, handoff list) - L1; and every title sink
        # shows them: no bidi controls (Trojan Source) in the rail, the board, a card
        title = common.scrub(common.no_bidi(title))   # no_bidi first, as in rename(): a control mid-token must not hide a secret from scrub
        if issue and isinstance(issue.get("title"), str):
            issue = {**issue, "title": common.no_bidi(issue["title"])}
        if project_key == "advisor":
            if not PROJECT_PATH:
                raise ValueError("PROJECT_PATH is not set for piperd - the advisor has no projects to see")
            if any(s.kind == "advisor" and s.state in ACTIVE for s in list(self.sessions.values())):
                raise ValueError("the advisor is already running")
            os.makedirs(common.ADVISOR_DIR, exist_ok=True)
            # the advisor dir is the advisor's rw project: never follow a link planted there
            with open(os.path.join(common.PIPER_DIR, "advisor", "CLAUDE.md"), "rb") as f:
                template = f.read()
            try:
                current = common.safe_read(common.ADVISOR_DIR, "CLAUDE.md", 1 << 20)
            except FileNotFoundError:
                current = None
            except OSError:
                current = b"(unreadable - left alone)"
            # a missing file, or a copy of an older template an extension names (ADVISOR_OLD_TEMPLATES:
            # (first line, None | a name the copy is kept under - it may carry the user's edits)), gets the current one
            old = next(((head, keep) for head, keep in extensions.items("ADVISOR_OLD_TEMPLATES")
                        if current is not None and current.startswith(head.encode())), None)
            if old and old[1]:
                common.safe_write(common.ADVISOR_DIR, old[1], current)
            if current is None or old:
                common.safe_write(common.ADVISOR_DIR, "CLAUDE.md", template)
            allp = projects.load(PROJECT_PATH)
            s = Session(kind="advisor", project="advisor", path=common.ADVISOR_DIR, title=title or "Global agent",
                        task=task, origin=origin, profile="advisor", opts=opts,
                        gitlab=sorted({g for p in allp.values() for g in p["gitlab"]}))
            if features.on("github"):
                s.github = sorted({r for p in allp.values() if p["exists"] for r in self._gh_projects(p)})
        else:
            p = projects.find(project_key, PROJECT_PATH)
            if p is None:
                raise KeyError("unknown project %r (not in projects.json)" % project_key)
            if not p["exists"]:
                raise ValueError("project checkout %s does not exist" % p["path"])
            gl = p["gitlab"] or [x for x in [self.origin_gl(p["path"])] if x]
            s = Session(project=project_key, path=p["path"], title=title or self._free_title(p), task=task,
                        origin=origin, profile=p["profile"] or holehome.PROJECT_PROFILE, gitlab=gl, issue=issue,
                        parent=parent, opts=opts, agent=agent, claude_mode="none" if agent == "codex" else "session")
            s.title_source = "issue" if issue else "manual" if title else "auto"
            s.github = self._gh_projects(p) if features.on("github") else []
            # Settings -> Sessions -> New sessions: what every new project session starts unlocked (still lockable per session)
            dflt = [k for k in self.policy.get("default_unlocks") or [] if k in UNLOCKS]
            s.unlock.update({k: True for k in dflt}, **({"projects": True} if "projects_write" in dflt else {}))
        s.runtime = runtime
        if _fork_source is not None and runtime == "direct":
            s.claude_mode = "none" if agent == "codex" else "host"
            s.host_claude, s.host_codex = agent == "claude", agent == "codex"
            if os.path.realpath(_fork_source.claude_src) != os.path.realpath(s.claude_src):
                raise ValueError("Direct forks use this machine's native history. Resume this private Hole conversation with Hole, or switch agents with a handoff.")
            digest.validate_conversation(_fork_source.claude_src, _fork_source.claude_session, agent)
            s.resume = _fork_source.claude_session
            s.fork_pending = True
            s.fork_from = {"session": _fork_source.id, "conversation": s.resume}
            s.import_from = getattr(_fork_source, "import_from", None)
        elif _fork_source is not None:
            # The copy can pin absolute conversation paths; choose legacy HOME before copying.
            check = doctor._cached(("hole", HOLE_BIN), doctor.check_hole)
            if not check["ok"]:
                raise ValueError(check["title"] + ": " + check["text"])
            if not check.get("settings_override", False):
                s.hole_home = os.path.join(s.dir, "hole-home")
                s.hole_legacy = True
            self._copy_fork(_fork_source, s)
        with self.lock:   # checked again with the insert: two creates at the limit must not both pass
            try:
                self._check_limit(_replaces)
            except ValueError:
                if _fork_source is not None:   # the copied conversation goes with the refused session
                    shutil.rmtree(s.dir, ignore_errors=True)
                raise
            self.sessions[s.id] = s
        s.switched_from = _switched[0] if _switched is not None else s.switched_from
        s.save()
        if _switched is not None:   # before _start's thread: its _place_handoff reads it
            common.safe_write(s.dir, agentswitch.FILE, _switched[1], mode=0o600)
        audit({"event": "session-create", "session": s.id, "project": s.project, "origin": origin,
               **({"opts": sorted(s.opts)} if s.opts else {}), **({"agent": s.agent} if s.agent != "claude" else {})})
        if image_rebuild:
            self._reserve_rebuild(s)
        self.bump()
        threading.Thread(target=self._start, args=(s,), daemon=True, name="start-" + s.id).start()
        return s

    def _start_direct(self, s: Session) -> None:
        """Start the native CLI on this host; login and transcript remain owned by the CLI."""
        self._set(s, "preflight", "", "checking the agent on this machine")
        s.claude_mode = "none" if s.agent == "codex" else "host"
        s.host_claude, s.host_codex = s.agent == "claude", s.agent == "codex"
        s.hole_legacy = False
        s.instance, s.attest, s.blocks, s.watch = None, None, [], []
        s.excludes, s.mount_roots, s.coverage = [], [], {}
        if s.kind == "project":
            self.repo_top(s.path, record="start")
        argv, env = directruntime.prepare(s, self._mcp_role(s), self._lang_md() + self.issue_md(s))
        self._place_handoff(s)
        if s.task and not s.task_sent and argv[-1:] != [s.task]:
            argv += ["--", s.task]
        if self._canceled(s):
            return
        self._events_reset(s)
        s.rc = None
        s.task_sent = bool(s.task) or bool(s.resume) and s.task_sent
        self._set(s, "spawning", "agent", "starting directly with native agent permissions")
        r = self.pty.call("spawn", id=s.id, argv=argv, env=env, cwd=s.path, cols=120, rows=34,
                          locked=False, log=os.path.join(s.dir, "pty.log"))
        if not r.get("ok"):
            self._set(s, "error", "", "agent spawn failed")
            return
        with s.lock:
            with s.cancel_lock:
                s.pid = r["pid"]
                canceled, s.cancel = s.cancel, False
            if s.rc is None and s.state == "spawning":
                s.state, s.since, s.phase = "starting", time.time(), "agent"
                s.detail = "direct session: answer login/trust prompts in the terminal"
        self.pty.call("subscribe", id=s.id)
        if canceled or s.state == "stopping":
            self._set(s, "stopping")
            self.pty.call("signal", id=s.id, sig="TERM")
            s.stop_first = s.stop_kick = time.time()
        s.save()
        audit({"event": "direct-spawn", "session": s.id, "agent": s.agent})

    def _free_title(self, p: dict) -> str:
        """A second session of the same checkout gets `<title> · 2` (rail, board and palette
        must tell them apart); the first keeps the plain title."""
        taken = {x.title for x in list(self.sessions.values()) if not x.hidden and x.project == p["key"]}
        if p["title"] not in taken:
            return p["title"]
        n = 2
        while "%s · %d" % (p["title"], n) in taken:
            n += 1
        return "%s · %d" % (p["title"], n)

    def siblings(self, s: Session) -> list[Session]:
        """Other live sessions in the same working tree (same checkout, same git branch)."""
        me = os.path.realpath(s.path)
        return [x for x in list(self.sessions.values()) if x.id != s.id and x.kind == "project" and not x.hidden
                and x.state in ACTIVE and os.path.realpath(x.path) == me]

    def gitlab_host(self) -> str:
        return str(self.policy.get("gitlab_host") or extensions.default("gitlab_host", "gitlab.com"))

    def github_hosts(self) -> list[str]:
        return [str(h) for h in self.policy.get("github_hosts") or ["github.com"] if h]

    # ------------------------------------------------------------ start pipeline
    def forbidden(self, s: Session) -> list[tuple[str, str]]:
        if self.policy.get("protected_mounts") is False:   # 9.46 protected_mounts off: as a plain hole
            return []
        f = [(common.PIPER_HOME, "any"), (os.path.join(common.REAL_HOME, ".hole"), "any"), (common.SK, "rw")] + \
            [(d, "rw") for d in extensions.DIRS]
        if not s.host_codex:
            f.append((os.path.join(common.REAL_HOME, ".codex"), "any"))   # the host's Codex login and history
        if not self.host_claude(s):
            f.append((os.path.join(common.REAL_HOME, ".claude"), "any"))
        return f + extensions.collect("forbidden", self, s)

    @staticmethod
    def _login_file(s: Session) -> str:
        """Host-side digest of the login the session started with (<session>/ is not mounted)."""
        return "codex-auth.sha256" if s.claude_mode == "none" else "claude-auth.sha256"

    def _login_seeded(self, s: Session) -> str | None:
        try:
            return common.safe_read(s.dir, self._login_file(s), 128).decode()
        except OSError:
            return None   # unknown: started before 2026-10-05

    def _login_copy_back(self, s: Session) -> bool:
        """The session's login to its template when it changed there (a /login, a token refresh);
        the saved one is then what this session "started with", so the next event copies it again
        only when it changes once more. Claude per-session config and Codex only."""
        if s.claude_mode != "none" and not s.host_codex and os.path.isdir(s.codex_dir):
            self._codex_copy_back(s)
        if s.claude_mode == "session":
            mod, home = sessioncfg, s.cfg
        elif s.claude_mode == "none" and not s.host_codex:   # the real ~/.codex needs no copy back
            mod, home = codexcfg, s.codex_dir
        else:
            return False
        with LOGIN_LOCK:   # the poll thread (_events) and a start (_collect_logins) of the same session
            seeded = self._login_seeded(s)
            tpl_login = os.path.join(mod.TEMPLATE_DIR, ".credentials.json" if mod is sessioncfg else codexcfg.AUTH)
            if seeded is None and not os.path.exists(tpl_login):
                seeded = ""   # started before this version, the template has no login: nothing to overwrite
            elif seeded is None:
                # started before this version: what it holds now is its baseline - pushing it would put
                # an older login over a newer one in the template; a later change is copied as usual
                seeded = mod.copy_back_credentials(home, "", dry=True)
                try:
                    common.safe_write(s.dir, self._login_file(s), seeded.encode())
                except OSError:
                    pass
                return False
            saved = mod.copy_back_credentials(home, seeded)
            if saved:
                try:
                    common.safe_write(s.dir, self._login_file(s), saved.encode())
                except OSError:
                    pass
                if self.policy.get("login_spread"):
                    self._login_spread(s, mod, tpl_login, saved)
        return bool(saved)

    def _codex_copy_back(self, s: Session) -> None:
        """The Codex login of a Claude session's subagent: to the Codex template when it changed, then to
        every other running session that holds one."""
        with LOGIN_LOCK:
            try:
                seeded = common.safe_read(s.dir, CODEX_LOGIN_FILE, 128).decode()
            except OSError:
                return
            saved = codexcfg.copy_back_credentials(s.codex_dir, seeded)
            if saved:
                common.safe_write(s.dir, CODEX_LOGIN_FILE, saved.encode())
                if self.policy.get("login_spread"):
                    self._login_spread(s, codexcfg, os.path.join(codexcfg.TEMPLATE_DIR, codexcfg.AUTH), saved)

    def _login_spread(self, s: Session, mod, tpl_login: str, digest: str) -> None:
        """A refresh rotates the refresh token: the running sessions of the same agent still hold the
        old one and would be logged out at their next refresh - give them the new login too."""
        name = os.path.basename(tpl_login)
        try:
            with open(tpl_login, "rb") as f:
                raw = f.read(64 << 10)
        except OSError:
            return
        for x in list(self.sessions.values()):
            if x.id == s.id or x.state not in RUNNING:
                continue
            if mod is sessioncfg:
                if x.claude_mode != "session":
                    continue
                home, mark = x.cfg, self._login_file(x)
            elif x.claude_mode == "none":
                if x.host_codex:   # the real ~/.codex: its login is the host's own
                    continue
                home, mark = x.codex_dir, self._login_file(x)
            elif not x.host_codex and os.path.isdir(x.codex_dir):   # a Claude session's Codex subagent
                home, mark = x.codex_dir, CODEX_LOGIN_FILE
            else:
                continue
            try:
                # only over a login the recipient still has unchanged since its seed: one it refreshed or
                # logged in itself meanwhile is newer (or another account) - its own copy-back collects it
                seeded = common.safe_read(x.dir, mark, 128).decode()
                mine = common.safe_read(home, name, 64 << 10)
                if hashlib.sha256(mine).hexdigest() != seeded:
                    continue
                common.safe_write(home, name, raw, mode=0o600, exact_mode=True)
                common.safe_write(x.dir, mark, digest.encode())   # theirs now: no copy back
            except OSError:
                pass

    def _collect_logins(self, s: Session) -> None:
        """Before a start seeds its login: a /login in a running session of the same agent that no
        hook event has reported yet (the user logged in and typed nothing) reaches the template too."""
        for x in list(self.sessions.values()):
            if x.id != s.id and x.claude_mode == s.claude_mode and x.state in RUNNING:
                try:
                    if self._login_copy_back(x):
                        audit({"event": "login-collected", "session": s.id, "from": x.id})
                except OSError:
                    pass

    def host_claude(self, s: Session) -> bool:
        """Does this sandbox get the real ~/.claude? A Claude session in host mode, and under policy
        claude_dir=host a Codex one too: the include is in the base every sandbox shares (holehome)."""
        return s.claude_mode == "host" or (s.claude_mode == "none" and self.claude_dir() == "host")

    def box_in(self, s: Session) -> str:
        if s.runtime == "direct":
            return s.box
        home_in = s.hole_home
        if s.claude_mode == "none":   # Codex: where the shared env (_box_dir_in) expects a box
            return home_in + ("/.claude" if self.claude_dir() == "session" else "/" + s.names["mount"])
        return home_in + ("/.claude" if s.claude_mode == "session" else "/" + s.names["mount"])

    def _codex_hook_library(self, s: Session) -> tuple[str, str] | None:
        """The session snapshot, mounted at the paths the host Codex hooks already use."""
        path = os.path.join(s.dir, "codex-hooks")
        if (s.claude_mode == "none" and s.host_codex and not self.host_claude(s)
                and os.path.isdir(path) and os.listdir(path)):
            return path, os.path.join(common.REAL_HOME, ".claude", "hooks")
        return None

    def allowed_sources(self, s: Session) -> list[str]:
        """Exact mount sources exempt from forbidden(): the session's own ~/.claude (or, with
        claude_dir=host, the host's ~/.claude plus the session's ~/.piper box), and for the
        advisor its project dir (which lives under $PIPER_HOME)."""
        out = [s.cfg] if s.claude_mode == "session" else \
            [s.codex_home, s.box] if s.claude_mode == "none" else [os.path.join(common.REAL_HOME, ".claude"), s.box]
        if s.claude_mode == "none" and self.host_claude(s):
            out.append(os.path.join(common.REAL_HOME, ".claude"))
        if self.codex_subagent(s) and os.path.isdir(s.codex_home):
            out.append(s.codex_home)   # the Codex subagent's home (_start): Piper-written, or the real ~/.codex
        hook_library = self._codex_hook_library(s)
        if hook_library:
            out.append(hook_library[0])
        out += [host for host, _t, _k in self._maven_plan(s)]   # generated under the box, which lies in $PIPER_HOME
        if self.codex_subagent(s):
            out.append(os.path.join(s.box, CODEX_WRAPPER[0]))   # the same
        if s.kind == "advisor":
            out.append(common.ADVISOR_DIR)
        elif s.kind == "prodcheck":
            out.append(prodcheck.WORK_DIR)
        return out

    def _host_claude_dir(self, s: Session | None) -> bool:
        """The base settings' claude_dir: the session's own mode (a resumed or forked conversation keeps its
        per-session ~/.claude under claude_dir=host), the policy for a base without a session."""
        return self.host_claude(s) if s is not None else self.claude_dir() == "host"

    def _protected(self, s: Session | None = None) -> list[str]:
        """Dirs no generated include or library may overlap - the "any" part of forbidden()."""
        if self.policy.get("protected_mounts") is False:   # 9.46: plain hole also keeps its includes
            return []
        out = [common.PIPER_HOME, os.path.join(common.REAL_HOME, ".hole"), os.path.join(common.REAL_HOME, ".codex")]
        if not self._host_claude_dir(s):
            out.append(os.path.join(common.REAL_HOME, ".claude"))
        return out + extensions.collect("protected", self, s)

    def _idea_port_path(self, s: Session | None) -> tuple[int, str] | None:
        """IntelliJ's MCP port and path (from idea_mcp_url, default 64342/stream) when the session has it:
        policy idea_in_sessions for every session, or this session's own start option."""
        if not (self.policy.get("idea_in_sessions") or (s is not None and s.opts.get("idea"))):
            return None
        u = urllib.parse.urlsplit(str(self.policy.get("idea_mcp_url") or "").strip() or "http://127.0.0.1:64342/stream")
        try:
            return u.port or 80, u.path or "/stream"
        except ValueError:
            return None

    def _idea_args(self, s: Session) -> list | None:
        """idea-bridge.py args for this session's mcp.json (not the morning check: it reads logs only)."""
        pp = self._idea_port_path(s)
        if pp is None or s.kind == "prodcheck":
            return None
        return ["--port", str(pp[0]), "--path", pp[1]]

    def _box_dir_in(self, s: Session | None = None) -> str:
        """Container dir of Piper's files in the session's pinned HOME (its gitconfig, an extension's).

        The claude_dir policy selects the box name; Codex gets a box there too.
        """
        home_in = s.hole_home if s else common.REAL_HOME
        if s is not None and s.claude_mode != "none":   # a Claude session's box follows its own mode (box_in)
            return home_in + ("/.claude" if s.claude_mode == "session" else "/.piper") + "/piper"
        return home_in + ("/.claude" if self.claude_dir() == "session" else "/.piper") + "/piper"

    def _secret_env_names(self, personal: dict) -> list[str]:
        """Environment names in the personal global (base and profiles) that L4 would refuse."""
        names = set()
        for doc in [personal] + list((personal.get("profiles") or {}).values()):
            env = (doc or {}).get("environment")
            if isinstance(env, dict):
                names.update(guard.env_failures(["%s=" % k for k in env], self._policy()))
        return sorted(names)

    def _base_settings(self, s: Session | None = None) -> tuple[dict, list[str]]:
        personal = read_json(PERSONAL_SETTINGS, {})
        rules = dict(self.policy.get("holehome") or {},
                     hook_wrap_dir=os.path.join(common.PIPER_HOME, "hooks"),
                     container_home=s.hole_home if s else common.REAL_HOME,
                     claude_dir="host" if self._host_claude_dir(s) else "session", protected=self._protected(s))
        if s is not None and s.claude_mode == "session":
            # Includes overmount --library. A private resume/fork must keep its
            # history and MCP config even when Minimal keeps personal includes.
            rules["strip_includes"] = list(rules.get("strip_includes", holehome.DEFAULTS["strip_includes"]))
            if "~/.claude" not in rules["strip_includes"]:
                rules["strip_includes"].append("~/.claude")
        if self._gitlab_via_piper():
            rules["strip_allow"] = list(rules.get("strip_allow", holehome.DEFAULTS["strip_allow"]))
            if self.gitlab_host() not in rules["strip_allow"]:
                rules["strip_allow"].append(self.gitlab_host())
        rules["secret_env_names"] = self._secret_env_names(personal)
        if self._gitlab_via_piper():   # K5: GitLab only through piperd
            rules["strip_allow"] = list(rules.get("strip_allow", holehome.DEFAULTS["strip_allow"])) + [self.gitlab_host()]
        extensions.call("base_rules", self, s, rules)   # 9.150: e.g. an extension's host service for the sandbox
        idea = self._idea_port_path(s)
        if idea:   # idea-bridge.py reaches IntelliJ through the gateway
            rules["add_gateway"] = list(rules.get("add_gateway") or []) + ["host.internal:%d" % idea[0]]
        # once a Codex sandbox was attested it stays enabled (codexcfg.ENABLED_MARK): one agent image for
        # every session, no rebuild per switch - never because a Codex start was merely tried
        # codex_in_claude: the subagent needs the CLI too (a policy, so the image stays one for every session)
        if os.path.exists(codexcfg.ENABLED_MARK) or (s is not None and s.agent == "codex") or \
                self.policy.get("codex_in_claude"):
            rules["enable_agents"] = ["codex"]
        if s is not None and s.host_codex:   # the real ~/.codex at its own path (codex_in), Codex or its subagent
            rules["set_env"] = dict(rules.get("set_env") or {}, CODEX_HOME=s.codex_home,
                                   CODEX_SQLITE_HOME=codexcfg.sqlite_home())
        if not self._git_hidden_policy() and (not isinstance(self.policy.get("ask"), list) or not self._git_hole()):
            # Guarded / legacy mode uses Piper's sanitized config; plain Hole keeps the user's includes/env.
            rules["set_env"] = dict(rules.get("set_env") or {}, GIT_CONFIG_GLOBAL=self._box_dir_in(s) + "/gitconfig")
        rules["keep_worktree_links"] = self._git_hole()   # git.worktreeLinks / worktreePool as in a plain hole
        if self._git_hole():   # libraries keep the personal readwrite, includes stay writable, as in a plain hole
            rules["keep_rw_libraries"] = True
            rules["keep_rw_includes"] = True
        if s is not None:   # the session's Maven settings (_write_maven), onto ~/.m2/settings.xml & co.
            rules["add_includes"] = {host: target for host, target, _kind in self._maven_plan(s, personal)}
            if self.codex_subagent(s):
                rules["add_includes"][os.path.join(s.box, CODEX_WRAPPER[0])] = CODEX_WRAPPER[1]
        base, changes = holehome.generate(personal, common.REAL_HOME, PROJECT_PATH or None, rules)
        hook_library = self._codex_hook_library(s) if s is not None else None
        if hook_library:
            source, target = hook_library
            base.setdefault("libraries", {})[source] = {"path": target, "readwrite": False}
        for project in projects.load(PROJECT_PATH).values():
            try:
                _, names = projects.sandbox_env_config(project)
            except ValueError:
                if s and s.kind == "project" and s.project == project["key"]:
                    raise
                continue
            if names:
                parent = s.profile if s and s.project == project["key"] else project["profile"] or holehome.PROJECT_PROFILE
                profile = {"environment": {n: "$" + projects.sandbox_env_marker(project["key"], n) for n in names}}
                if parent:
                    profile["extends"] = parent
                base["profiles"][projects.sandbox_env_profile(project["key"])] = profile
        if s is not None and s.kind == "project":
            try:
                project_doc = guard.read_settings(os.path.join(s.path, ".hole", "settings.json"))[0]
                effective = guard.effective(base, project_doc, self._hole_profile(s))
            except (ValueError, OSError):
                effective = {}   # preflight owns blank-file repair and invalid settings/profile diagnostics
            git = effective.get("git") or {}
            pool = git.get("worktreePool") and git.get("worktreeLinks") in ("ro", "rw")
            if not pool and "HOLE_WORKTREES_DIR" not in (effective.get("environment") or {}):
                path = os.path.join(s.path, ".claude", "worktrees")
                base.setdefault("environment", {})["HOLE_WORKTREES_DIR"] = path
                changes.append("environment.HOLE_WORKTREES_DIR -> %s (inside the mounted project)" % path)
            shield = guard.shield_worktree_pools(effective, s.path, common.REAL_HOME, s.hole_home) \
                if self.policy.get("candidates") else []
            if shield:
                files = base.setdefault("files", {})
                files["exclude"] = list(files.get("exclude") or []) + shield
                changes.append("files.exclude: shield nested worktree pools, including future checkouts")
        return base, changes

    def _hole_profile(self, s: Session) -> str | None:
        project = projects.find(s.project, PROJECT_PATH) if s.kind == "project" else None
        if project and projects.sandbox_env_config(project)[1]:
            return projects.sandbox_env_profile(s.project)
        return s.profile

    def _maven_plan(self, s: Session, personal: dict | None = None) -> list[tuple[str, str, str]]:
        """m2settings.plan for this session (policy maven_settings, default "auto"); the morning check
        builds nothing."""
        if s.kind == "prodcheck":
            return []
        if personal is None:
            personal = read_json(PERSONAL_SETTINGS, {})
        try:   # an include of the project's own .hole/settings.json onto ~/.m2/... wins as well
            project_doc = guard.read_settings(os.path.join(s.path, ".hole", "settings.json"))[0]
        except (ValueError, OSError):
            project_doc = {}
        return m2settings.plan(personal, common.REAL_HOME, s.box,
                               enabled=str(self.policy.get("maven_settings", "auto")) != "off",
                               extra_docs=[project_doc])

    def _write_maven(self, s: Session) -> None:
        """<box>/piper/maven-settings.xml, maven-toolchains.xml (and settings-security.xml when the host
        has one) - what _base_settings included onto ~/.m2/. Invalid or unreadable host XML: a minimal valid
        document goes there instead (Maven refuses an empty settings file) and the session is told."""
        personal = read_json(PERSONAL_SETTINGS, {})
        jdks = m2settings.sandbox_jdks(personal.get("dependencies"))
        if (s.notice or "").startswith(MAVEN_NOTICE):
            s.notice = None   # last start's failure: this one decides anew
        for host, _target, kind in self._maven_plan(s, personal):
            if kind == "repository":   # the host's maven cache, shared as a plain hole's include (K21)
                os.makedirs(host, exist_ok=True)
                continue
            rel = os.path.relpath(host, s.box)
            try:
                if kind == "settings":
                    with open(os.path.join(common.REAL_HOME, ".m2", "settings.xml"), encoding="utf-8") as f:
                        notes: list[str] = []
                        data = m2settings.generate_settings(f.read(), jdks, notes)
                    audit({"event": "maven-settings", "session": s.id, "notes": notes, "jdks": sorted(jdks)})
                elif kind == "security":
                    with open(os.path.join(common.REAL_HOME, ".m2", "settings-security.xml"), encoding="utf-8") as f:
                        data = f.read()
                else:
                    data = m2settings.generate_toolchains(jdks)
            except (OSError, ET.ParseError) as e:
                audit({"event": "maven-settings-failed", "session": s.id, "file": kind, "error": str(e)})
                s.notice = "%s ~/.m2/%s.xml: %s" % (MAVEN_NOTICE, "settings-security" if kind == "security" else "settings", e)
                # the GUI shows notice only after the end; the agent reads the file itself when mvn fails
                data = {"settings": "<settings/>", "security": "<settingsSecurity/>"}.get(kind, "<toolchains/>") + \
                    "\n<!-- piperd: %s - fix the host file and restart the session -->\n" % s.notice.replace("--", "-")
            common.safe_write(s.box, rel, data.encode(), mode=0o644, exact_mode=True)   # read by the container user, like gitconfig

    def _git_hole(self) -> bool:
        """policy.git_mode "hole" (default since 2026-10-05): git in a session as in a plain hole -
        read-write libraries, the host's ~/.gitconfig copied in, git_run without unlock or card, no
        git guard. "guarded" brings the 2026-10-04 restrictions back."""
        return (self.policy.get("git_mode") or "hole") == "hole"

    def _git_hidden_policy(self) -> bool:
        """force_exclude hides `.git` from every sandbox again (git is open since 2026-10-01)."""
        return guard.git_excluded(dict(holehome.DEFAULTS, **(self.policy.get("holehome") or {}))["force_exclude"])

    _git_identity: tuple[float, dict] = (0.0, {})

    @classmethod
    def _sandbox_gitconfig(cls, host_config: bool = False) -> str:
        """The sandbox's global git config (GIT_CONFIG_GLOBAL; git is open there since
        2026-10-01, ~/.gitconfig is not mounted): safe.directory (the bind-mounted project
        looks root-owned there - "dubious ownership"), the host's identity (a repository's own
        user.email still wins, as on the host), and no automatic gc - its `worktree prune`
        would delete the host's other worktrees, whose directories the sandbox does not see."""
        at, ident = cls._git_identity
        if time.time() - at > 300:   # not git on every spawn
            ident = {}
            for key in ("user.name", "user.email"):
                try:
                    r = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True,
                                       timeout=5, stdin=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if r.returncode == 0 and r.stdout.strip():
                    ident[key] = r.stdout.strip()
            cls._git_identity = (time.time(), ident)

        def q(v: str) -> str:
            return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        lines = ["[safe]", "\tdirectory = *", "[gc]", "\tauto = 0", "\tworktreePruneExpire = never",
                 "[maintenance]", "\tauto = false"]
        if ident:
            lines.append("[user]")
            lines += ["\t%s = %s" % (k.split(".")[1], q(v)) for k, v in sorted(ident.items())]
        head = ""
        if host_config:   # git_mode "hole": aliases, pull.rebase, lfs ... as with hole's ~/.gitconfig include -
            try:          # a copy, so nothing the sandbox writes reaches the host's git (later lines win)
                with open(os.path.join(common.REAL_HOME, ".gitconfig"), "rb") as f:
                    head = cls._strip_gitconfig(f.read(256 << 10).decode("utf-8", "replace")).rstrip() + "\n# Piper:\n"
            except OSError:
                head = ""
        return head + "\n".join(lines) + "\n"

    GITCONFIG_DROP = ("credential", "http", "url", "include", "includeif")

    @classmethod
    def _strip_gitconfig(cls, text: str) -> str:
        """The host ~/.gitconfig without what may carry a secret or pull in more files: credential
        helpers, http.*.extraHeader, url.*.insteadOf (tokens in URLs), include / includeIf."""
        out, drop = [], False
        for line in text.splitlines():
            m = re.match(r"\s*\[\s*([A-Za-z0-9.-]+)", line)
            if m:
                drop = m.group(1).split(".")[0].lower() in cls.GITCONFIG_DROP
            if not drop:
                out.append(line)
        return "\n".join(out) + "\n"

    def _extra_md(self, s: Session) -> str:
        """The session's own CLAUDE.md / AGENTS.md additions: its issue, a git that cannot write."""
        project = projects.find(s.project, PROJECT_PATH) if s.kind == "project" else None
        names = projects.sandbox_env_config(project)[1] if project else []
        api = ("\n- The user granted this project's tests these environment variables: %s. "
               "They also reach commands in worktrees. Use them without printing values, cookies or "
               "authorization headers. Other credentials remain unavailable.\n" % ", ".join(names)) if names else ""
        return (self._lang_md() + self.issue_md(s) + api + ("\n" + s.git_note + "\n" if s.git_note else "") +
                "".join(extensions.call("session_md", self, s)) + self._hidden_md(s))

    def _hidden_md(self, s: Session) -> str:
        """Which of the project's files this sandbox cannot see (the effective hole excludes): the agent otherwise
        reads a masked file (a CI definition, a config dir) as a git error and guesses why CI fails."""
        pats = [str(x) for x in (s.excludes or []) if str(x).strip()]
        if not pats:
            return ""
        shown = ", ".join("`%s`" % p for p in pats[:12]) + (" (+%d more)" % (len(pats) - 12) if len(pats) > 12 else "")
        return ("\n- Hidden from this sandbox (secrets; the host has the real files): %s. They look deleted or "
                "unreadable to git here (\"unsupported file type\") - never commit those deletions or guess their "
                "content. A CI failure: read the pipeline's job log (gitlab_pipelines / gitlab_job_log), not a "
                "hidden CI file.\n" % shown)

    GIT_HOST_HINT = (
        "\n- Files hidden from this sandbox (secrets, excluded config dirs) look deleted or unreadable to git "
        "here, so `git switch` / `checkout <branch>` / `merge` / `stash` in the sandbox refuse (\"commit or stash\", "
        "\"unsupported file type\"). Run those through the git_run tool instead: it runs on the host checkout, which "
        "sees the real files (e.g. git_run [\"switch\", \"feature/x\"], [\"merge\", \"--no-ff\", \"origin/dev\"]). "
        "Never work around it with plumbing (commit-tree, update-ref) or by committing the masked deletions.\n")

    CODEX_SUBAGENT_HINT = (
        "\n- Codex launched inside this Hole container uses Hole's existing isolation. Run "
        "`codex --ask-for-approval never exec --sandbox danger-full-access` for a nested Codex task; "
        "this inner-sandbox choice is already authorized here and needs no additional confirmation. "
        "Codex plugins may explicitly force read-only/workspace-write through app-server, overriding config.toml "
        "and failing to create a bwrap namespace. In this container use direct codex exec with the same task "
        "and output requirements instead; do not patch the plugin or relax Hole's mounts/network/security. "
        "For read/check/review tasks, explicitly require no edits in the prompt (full access does not enforce "
        "read-only). Normal task scope and Piper's tool approvals still apply. Never use this container-only "
        "authorization to launch full-access Codex on the host.\n")

    def _lang_md(self) -> str:
        """Session language and opt-in Git / nested Codex instructions for the Hole container."""
        lang = " ".join(str(self.policy.get("language") or "").split())[:100]
        return (("\n- Answer the user in %s (code, commits and identifiers stay as the project has them).\n" % lang
                 if lang else "") + (self.GIT_HOST_HINT if self.policy.get("git_host_hint") else "") +
                (self.CODEX_SUBAGENT_HINT if self.policy.get("codex_in_claude") else ""))

    def _write_gitconfig(self, s: Session) -> None:
        """<box>/piper/gitconfig, where GIT_CONFIG_GLOBAL (_base_settings) points in the sandbox."""
        common.safe_write(s.box, "piper/gitconfig", self._sandbox_gitconfig(self._git_hole()).encode(), mode=0o644,
                          exact_mode=True)

    def claude_dir(self) -> str:
        return "host" if str(self.policy.get("claude_dir", "session")) == "host" else "session"

    def codex_dir(self) -> str:
        return "host" if str(self.policy.get("codex_dir", "session")) == "host" else "session"

    def _policy(self, s: Session | None = None) -> dict:
        p = dict(self.policy)
        project = projects.find(s.project, PROJECT_PATH) if s and s.kind == "project" else None
        if project:
            p["allowed_env"] = list(p.get("allowed_env") or []) + projects.sandbox_env_config(project)[1]
        p["allowed_env"] = list(p.get("allowed_env") or []) + extensions.collect("allowed_env", self, s)
        if p.get("security_strict"):   # only the agent's login and what an extension's sandbox needs
            p["allowed_env"] = ["CLAUDE_CODE_OAUTH_TOKEN"] + extensions.collect("allowed_env", self, s)
        if self.claude_dir() == "host":   # accepted exposure: do not scan (or block on) ~/.claude
            p["scan_skip_roots"] = list(p.get("scan_skip_roots") or []) + ["~/.claude"]
        p["forbidden_gateway_ports"] = sorted({str(x) for x in p.get("forbidden_gateway_ports") or []}
                                              | ({str(PIPER_PORT)} if p.get("piper_port_blocked") is not False else set()))
        return p

    def _preflight(self, s: Session, base: dict, progress=None) -> dict:
        approved = read_json(common.APPROVED_FILE, {})
        main_git = self._main_git(s)   # a worktree's main .git: a library like any other (L1, L3)
        # what hole hides in a library follows its version (2.2+ the global excludes too), not legacy HOME (<2.4)
        lib_global = doctor._cached(("hole", HOLE_BIN), doctor.check_hole).get("library_global_excludes",
                                                                                not s.hole_legacy)

        def run(extra):
            return guard.preflight(s.path, base, self._hole_profile(s), self._policy(s), approved,
                                   self.forbidden(s), common.REAL_HOME, allowed_sources=self.allowed_sources(s),
                                   extra_libraries=extra,
                                   container_home=s.hole_home, progress=progress,
                                   known_common=main_git, global_library_excludes=lib_global)
        r = run([(main_git, True)] if main_git else [])
        s.git_note = ""
        if main_git and guard.git_excluded(((r.get("effective") or {}).get("files") or {}).get("exclude") or []):
            r = run([])   # the effective excludes hide .git (personal/project/profile): _argv mounts no main .git
        elif main_git:
            lib = next(((hp, rw) for hp, rw in r.get("libraries") or []   # incl. a worktree link to the main repository
                        if hp != main_git and guard._inside(main_git, hp)), None)
            if lib:   # a library mounts it already, and a bind nested in it would not hold (K18)
                r = run([])
                if not lib[1]:
                    s.git_note = ("[piper] This is a linked worktree; its main repository's .git (%s) is inside "
                                  "the read-only library %s here, so git can read but not commit. Tell the user: "
                                  "%s makes it work."
                                  % (main_git, lib[0], "\"readwrite\": true for that library in ~/.hole/settings.json"
                                     if self._git_hole() else "listing that library in Settings -> holehome.rw_libraries"))
                    audit({"event": "main-git-read-only", "session": s.id, "git": main_git, "library": lib[0]})
        for path in r.get("repaired") or []:
            audit({"event": "settings-repaired", "session": s.id, "file": path, "detail": "blank -> {}"})
            if path in approved and path in r["settings"] and not any(
                    b["kind"] == "settings-changed" and b["path"] == path for b in r["blocks"]):
                # an approved blank file: the approval carries over to its {} (guard.repair_blank)
                approved[path] = {"sha": r["settings"][path], "approved": time.time(), "text": r["texts"][path]}
                with APPROVED_LOCK:
                    cur = read_json(common.APPROVED_FILE, {})
                    cur[path] = approved[path]
                    write_json(common.APPROVED_FILE, cur)
        if r.get("inherited"):   # the same bytes approved in another checkout of this repository (guard.preflight)
            with APPROVED_LOCK:
                cur = read_json(common.APPROVED_FILE, {})
                for path, twin in r["inherited"].items():
                    cur[path] = {"sha": r["settings"][path], "approved": time.time(),
                                 "text": (r["texts"].get(path) or "")[:65536], "via": twin}
                write_json(common.APPROVED_FILE, cur)
            audit({"event": "settings-inherited", "session": s.id, "files": r["inherited"]})
        if self._policy(s).get("settings_review", True) is False:
            # review off (9.46): starts take the files as written - remember them as the baseline, so
            # switching the review on later shows what changed since, not every file as never approved
            seen = {p: sha for p, sha in (r.get("settings") or {}).items()
                    if sha != "absent" and (approved.get(p) or {}).get("sha") != sha}
            if seen:
                with APPROVED_LOCK:
                    cur = read_json(common.APPROVED_FILE, {})
                    for path, sha in seen.items():
                        cur[path] = {"sha": sha, "approved": time.time(),
                                     "text": (r["texts"].get(path) or "")[:65536], "via": "review-off"}
                    write_json(common.APPROVED_FILE, cur)
        return r

    @quietwindow.managed("start")
    def _start(self, s: Session) -> None:
        s.quiet_generation = secrets.token_hex(16)  # runtime identity, even if a PID is reused
        # Negative revision records that this process was created with Strict sandbox inputs.
        s.security_sandbox_rev = -security.REV if self.policy.get("security_strict") else 0
        s.layout = "piper"   # a new process: the current box names, whatever the previous one used
        try:
            if self.policy.get("security_strict"):
                if s.runtime == "direct" or s.opts.get("unrestricted") or s.opts.get("docker"):
                    self._set(s, "blocked", "", "Strict requires Hole with filtered networking and no Docker start option")
                    return
                s.unlock = {}
            if s.runtime == "direct":
                self._start_direct(s)
                return
            self._set(s, "preflight", "", "checking isolation policy")
            if s.kind == "project":   # first, before this start's sandbox exists: records the root
                if self.repo_top(s.path, record="start") is None and \
                        self._may_record(os.path.realpath(s.path), "start") == "enclosing":
                    # its repository could be planted right now by the enclosing sandbox and
                    # would be trusted at a later start: not while that one runs
                    self._set(s, "blocked", "", "a session of an enclosing directory is running - this project's "
                                                "repository cannot be verified until it ends")
                    return
            # before L1: forbidden()/allowed_sources() depend on it
            # Forks stay with the native profile selected by _copy_fork.
            # A conversation kept in the session's own dir stays there when claude_dir / codex_dir now say host
            # (9.46 Minimal forces host over master's per-session default): `--resume` against ~/.claude found
            # nothing and the second Continue called it lost. Only that direction - a switch to per-session dirs
            # is a stricter choice and applies at once.
            keep = bool(s.resume) and not s.fork_pending and self._has_transcript(s, s.resume)
            if s.agent == "claude" and s.fork_pending and s.claude_mode == "host" and self.claude_dir() != "host":
                self._set(s, "error", "", "The Claude profile is now isolated. Import or fork this conversation again with the current setting.")
                return
            mode = "none" if s.agent == "codex" else s.claude_mode if s.fork_pending else self.claude_dir()
            was_mode = s.claude_mode
            if keep and s.claude_mode == "session" and mode == "host":
                audit({"event": "mode-pinned", "session": s.id, "kept": "session", "policy": "claude_dir=host"})
            else:
                s.claude_mode = mode
            if s.agent != "codex" and s.resume and not s.fork_pending and was_mode in ("session", "host") and \
                    was_mode != s.claude_mode and not self._has_transcript(s, s.resume):
                # the stricter switch (host -> session): the transcript stays in ~/.claude, `--resume` in the
                # fresh per-session dir would fail and the next Continue call it lost - a new conversation, as Codex
                audit({"event": "resume-dropped", "session": s.id, "thread": s.resume, "reason": "claude_dir changed"})
                s.resume = None
            s.host_claude = self.host_claude(s)
            was_host = s.host_codex
            # codex_dir=host covers a Claude session's Codex subagent too: the same login as the host Codex;
            # a pending Codex fork stays on the home _copy_fork put its rollout in (never resumed elsewhere)
            host_codex = self.codex_dir() == "host" and (s.agent == "codex" or self.codex_subagent(s))
            if s.agent == "codex" and keep and not s.host_codex and host_codex:
                audit({"event": "mode-pinned", "session": s.id, "kept": "session", "policy": "codex_dir=host"})
            elif not (s.agent == "codex" and s.fork_pending):
                s.host_codex = host_codex
            if s.agent == "codex" and s.resume and was_host != s.host_codex:
                # codex_dir changed since its last run: the thread's rollout lies in the other CODEX_HOME,
                # `codex resume <id>` would fail on every retry - a new conversation instead
                audit({"event": "resume-dropped", "session": s.id, "thread": s.resume, "reason": "codex_dir changed"})
                s.resume = None
            hole_check = doctor._cached(("hole", HOLE_BIN), doctor.check_hole)
            if not hole_check["ok"]:
                self._set(s, "error", "", hole_check["title"] + ": " + hole_check["text"])
                return
            legacy = not hole_check.get("settings_override", False)
            if legacy and s.hole_home == common.REAL_HOME:
                if s.pid or s.resume or s.fork_pending:
                    self._set(s, "error", "", "This session uses native HOME and needs Hole 2.4+. "
                              "Upgrade Hole or create a new compatibility session; resume paths are preserved.")
                    return
                # A private HOME gives each old Hole its own settings file, even if startup stalls.
                s.hole_home = os.path.join(s.dir, "hole-home")
            s.hole_legacy = legacy
            if legacy:
                s.notice = "Hole compatibility mode: isolated HOME. Upgrade to Hole 2.4+ for native HOME and VMM masks."
            if s.claude_mode == "none" and s.host_codex and not self.host_claude(s):
                codexcfg.copy_host_hooks(os.path.join(s.dir, "codex-hooks"))
            base, changes = self._base_settings(s)
            pf = self._preflight(s, base, progress=lambda msg: self._set(s, None, None, msg))
            if pf["ok"] and s.kind == "project" and self.policy.get("candidates"):
                # Only accepted mount sources may be written. Use the validated snapshot,
                # not a fresh read of the sandbox-writable project settings.
                guard.shield_worktree_pools(pf["effective"], s.path, common.REAL_HOME, s.hole_home, prepare=True)
                # The new empty directories must be part of the pre-start mount identities.
                pf = self._preflight(s, base, progress=lambda msg: self._set(s, None, None, msg))
            s.coverage, s.settings, s.texts = pf["coverage"], pf["settings"], pf.get("texts") or {}
            s.excludes = list(((pf.get("effective") or {}).get("files") or {}).get("exclude") or [])
            s.mount_roots = list(pf.get("mount_roots") or [])
            net = (pf.get("effective") or {}).get("network") or {}
            s.network = {"unrestricted": self._unrestricted(s),
                         "allow": list(net.get("allow") or []),
                         "gateway": list(net.get("hostGatewayDomains") or []), "denied": None}
            s.watch = []
            s.blocks = pf["blocks"]
            for b in s.blocks:
                if b["kind"] == "protected-mounted":
                    b["what"] = protected_what(b["path"])
            s.hook_problems = list(pf.get("hook_problems") or [])
            s.agent_log = None   # this start's capture (_on_pty_event)
            audit({"event": "preflight", "session": s.id, "ok": pf["ok"],
                   "blocks": [{k: b.get(k) for k in ("kind", "path")} for b in pf["blocks"]]})
            if self._canceled(s):
                return
            if not pf["ok"]:
                self._set(s, "blocked", "", "%d preflight finding(s)" % len(pf["blocks"]))
                return
            self._set(s, "preparing", "", "writing the session config")
            hh = s.hole_dir
            os.makedirs(hh, mode=0o700, exist_ok=True)
            self._spawn_lock.acquire()   # released by _spawn_done: after hole's first line, or in the finally
            self._spawn_owner = s.id
            write_json(s.hole_settings, base)
            if s.hole_home != common.REAL_HOME:
                changes += holehome.link_agents(os.path.join(common.REAL_HOME, ".hole", "agents"),
                                                  os.path.join(hh, "agents"))
                trust = os.path.join(hh, "trust.json")
                personal_trust = os.path.join(common.REAL_HOME, ".hole", "trust.json")
                if not os.path.exists(trust) and os.path.exists(personal_trust):
                    shutil.copy2(personal_trust, trust)
            home_in = s.hole_home
            if s.claude_mode in ("host", "none"):
                sessioncfg.fresh_dir(s.box)   # it was mounted rw: never write into the old one
                sessioncfg.build_box(s.box, home_in + "/.claude", self.box_in(s), self._mcp_role(s),
                                     extra_md=self._extra_md(s), idea=self._idea_args(s),
                                     guard_git=not isinstance(self.policy.get("ask"), list) or not self._git_hole())
            if (s.claude_mode == "none" or self.codex_subagent(s)) and not s.host_codex:   # host: -c (_argv) or nothing
                try:   # the template follows ~/.codex (configuration only - never auth.json, sessions, history)
                    updated, kept, skipped = codexcfg.sync_template(os.path.join(common.REAL_HOME, ".codex"))
                    if updated or skipped:
                        audit({"event": "codex-template-synced", "session": s.id, "updated": updated,
                               "kept": kept, "skipped": skipped})
                except OSError as e:   # the session starts with the template as it was
                    audit({"event": "codex-template-sync-failed", "session": s.id, "error": str(e)})
            if s.claude_mode == "none" and not s.host_codex:
                codexcfg.build(s.codex_dir, self.box_in(s), self._mcp_role(s), s.path, s.names["events"],
                               extra_md=self._extra_md(s), idea=self._idea_args(s))
                self._collect_logins(s)
                # what the session starts with, host-side (<session>/ is not mounted): copy-back compares it
                common.safe_write(s.dir, self._login_file(s), codexcfg.seed_credentials(s.codex_dir).encode())
            elif s.claude_mode == "session":
                # the template follows ~/.claude (configuration only - never transcripts, history
                # or file snapshots): a skill or setting changed on the host is in this session too,
                # and an empty template (nobody ran `task piper:template`) is seeded the same way
                try:
                    updated, kept, skipped = sessioncfg.sync_template(os.path.join(common.REAL_HOME, ".claude"))
                    if updated or skipped:
                        audit({"event": "template-synced", "session": s.id, "updated": updated,
                               "kept": kept, "skipped": skipped})
                except OSError as e:   # the session starts with the template as it was
                    audit({"event": "template-sync-failed", "session": s.id, "error": str(e)})
            if s.claude_mode == "session":
                sessioncfg.build(s.cfg, home_in, self._mcp_role(s), extra_md=self._extra_md(s), idea=self._idea_args(s),
                                 guard_git=not isinstance(self.policy.get("ask"), list) or not self._git_hole())
                self._collect_logins(s)
                common.safe_write(s.dir, self._login_file(s), sessioncfg.seed_credentials(s.cfg).encode())
            if self.codex_subagent(s) and not s.host_codex:
                # Codex as Claude's subagent (codex-rescue) gets Piper's Codex login - the host ~/.codex only
                # under codex_dir=host (mounted as it is, nothing written); no hooks.json: its events would
                # drive this Claude session's state
                codexcfg.build(s.codex_dir, self.box_in(s), self._mcp_role(s), s.path, s.names["events"],
                               extra_md=self._extra_md(s), idea=self._idea_args(s))
                try:
                    os.unlink(os.path.join(s.codex_dir, "hooks.json"))
                except FileNotFoundError:
                    pass
                common.safe_write(s.dir, CODEX_LOGIN_FILE, codexcfg.seed_credentials(s.codex_dir).encode())
            local_mcp = directruntime.local_provider(s, self.box_in(s))
            if local_mcp and not s.host_codex and (s.agent == "codex" or self.codex_subagent(s)):
                codexcfg.add_local_provider(s.codex_dir, local_mcp)
            extensions.call("session_up", self, s)   # 9.150: the box is fresh, nothing spawned yet
            self._write_gitconfig(s)
            self._write_maven(s)
            self._place_handoff(s)
            if self.codex_subagent(s):
                common.safe_write(s.box, CODEX_WRAPPER[0], CODEX_WRAPPER_SH.encode(), mode=0o755, exact_mode=True)
            s.rebuild = self._claim_rebuild(s)
            argv = self._argv(s, home_in)
            env = self._env(s)
            if self._canceled(s):
                return
            s.rc = None   # no process yet: an rc from here on is this hole's (the "starting" check below)
            self._set(s, "spawning", "", " ".join(argv[:4]))
            audit({"event": "spawn", "session": s.id, "argv": argv, "settings_changes": changes})
            r = self.pty.call("spawn", id=s.id, argv=argv, env=env, cwd=s.path, cols=120, rows=34,
                              locked=True, log=os.path.join(s.dir, "pty.log"))
            if not r.get("ok"):
                self._session_down(s)
                self._set(s, "error", "", "spawn failed: %s" % r.get("error"))
                return
            # a resumed conversation already has its task (unless it ended before it was typed)
            self._events_reset(s)   # never under cancel_lock: events_lock comes before s.lock (_fsedit_event), stop() takes s.lock, cancel_lock
            with s.cancel_lock:
                s.pid, s.text, s.trust_seen = r["pid"], "", False
                s.dialog_done = False
                # F1-1: the old process's screen watch / sandbox are not this one's - a leftover screen_wait
                # took the first data as "a turn runs" and unsubscribed: no phase, no attestation, locked
                s.screen_wait, s.instance, s.attest = False, None, None
                s.stop_first = s.stop_kick = 0.0   # a new process: the stop clock belongs to it
                s.idle = False   # a new process: not at its prompt until its first Stop (a trust dialog may come first)
                s.subagents = {}
                s.background_tasks = {}
                s.background_since = time.time()
                self._background_cache.pop(s.id, None)
                s.stop_ts = s.notif_ts = time.time()   # Mattermost keys of this process's prompts, not the old one's
                canceled, s.cancel = s.cancel, False
            s.task_sent = bool(s.resume) and s.task_sent
            if canceled or s.state == "stopping":   # Stop came while hole was being spawned
                self._set(s, "stopping")
                self.pty.call("subscribe", id=s.id, data=False)
                self.pty.call("signal", id=s.id, sig="TERM")
                s.stop_first = s.stop_kick = time.time()   # hole ignores it this early: _stop_nudge repeats it
                # The stopped hole may still load shared agent/trust data (TERM is dropped this early).
                self._spawn_done(s, also=("stopping",))
                return
            # state first, then the stream: an early trust prompt must not be overwritten. A hole that died at
            # once (a missing cwd: rc 127) may be exited by _on_exit already - its rc decides, under the state's
            # lock; overwritten, the session stayed in starting with no process (W34)
            with s.lock:   # _set's body: s.lock is not reentrant
                if s.rc is None:
                    if s.phase:
                        s.phase_since = time.time()
                    s.state, s.since, s.phase, s.detail = "starting", time.time(), "", ""
                    s.save()
            self.bump()
            self.pty.call("subscribe", id=s.id, **{"from": 0})
            self._spawn_done(s)
        except Exception as e:  # noqa: BLE001 - surface every failure in the GUI
            self._set(s, "error", "", "%s: %s" % (type(e).__name__, e))
        finally:
            self._spawn_done(s, wait=False)   # every other exit (a cancel, a failed spawn, an exception)

    SPAWN_SETTLE = 15.0   # s to wait for hole's first line (it loads settings.json before printing anything)

    def _spawn_done(self, s: Session, wait: bool = True, also: tuple = ()) -> None:
        """Release _spawn_lock after hole loads shared agent/trust data (its first output, bounded).
        Idempotent; wait=False on a spawn that failed before hole ran;
        `also`: more states to wait in (a hole stopped right after its spawn still reads the file)."""
        if self._spawn_owner != s.id:   # not held by this start (failed before the lock, or released already)
            return
        try:
            if wait:
                deadline = time.time() + self.SPAWN_SETTLE
                while time.time() < deadline and not s.text and (s.state in self.START_STATES or s.state in also):
                    time.sleep(0.05)
        finally:
            self._spawn_owner = None
            self._spawn_lock.release()

    @staticmethod
    def _mcp_role(s: Session) -> str:
        """piper-mcp's tool set: the advisor's, the production check's (9.12 C) or a project session's."""
        return {"advisor": "advisor", "prodcheck": "prodcheck"}.get(s.kind, "session")

    def _canceled(self, s: Session) -> bool:
        if not s.cancel:
            return False
        s.cancel = False
        self._session_down(s)
        self._set(s, "exited", "", "canceled")
        return True

    def codex_subagent(self, s: Session) -> bool:
        """policy codex_in_claude: a Claude project session gets a Codex home for codex as its subagent."""
        return s.claude_mode != "none" and s.kind == "project" and bool(self.policy.get("codex_in_claude"))

    @staticmethod
    def codex_in(s: Session, home_in: str) -> str:
        """Where the sandbox sees CODEX_HOME. The real ~/.codex (codex_dir=host) keeps its host path,
        CODEX_HOME pointing there (_base_settings): its state DB stores absolute rollout paths, and
        `codex resume` of a paginated thread fails on a path that does not exist (thread/resume:
        "no rollout found") - host threads in the sandbox, sandbox threads on the host."""
        return s.codex_home if s.host_codex else home_in + "/.codex"

    def _argv(self, s: Session, home_in: str) -> list[str]:
        profile = self._hole_profile(s)
        agent = "%s:%s" % (s.agent, profile) if profile else s.agent
        # -n: hole dumps allowed/denied names at exit - the inspector's "Network" (_network_log)
        if s.claude_mode == "none":
            # Codex: its own CODEX_HOME and Piper's box; hooks.json and the MCP server are in the
            # former (codexcfg), which only Piper wrote - no review prompt for its hooks
            argv = [HOLE_BIN, "start", agent, s.path, "-n", "--library", "%s:%s:rw" % (s.codex_home, self.codex_in(s, home_in)),
                    "--library", "%s:%s:rw" % (s.box, self.box_in(s)), "--", "--dangerously-bypass-hook-trust",
                    "--no-alt-screen"]
            if s.host_codex:   # the real ~/.codex (codex_dir=host): Piper's config, hooks and block as overrides
                argv += codexcfg.overrides(self.box_in(s), self._mcp_role(s), s.path, s.names["events"],
                                           extra_md=self._extra_md(s), idea=self._idea_args(s),
                                           local=read_json(os.path.join(s.box, "piper/mcp.json"), {}).get("mcpServers", {}).get("piper_local"))
            if s.resume:
                argv += ["fork" if s.fork_pending else "resume", s.resume]
        elif s.claude_mode == "host":
            real = os.path.join(common.REAL_HOME, ".claude")
            # the real ~/.claude is ~/.claude via the generated include; mounted again at its
            # own path so absolute paths in its settings (statusLine, hooks) resolve too
            argv = [HOLE_BIN, "start", agent, s.path, "-n", "--library", "%s:%s:rw" % (real, real),
                    "--library", "%s:%s/.piper:rw" % (s.box, home_in)]
            argv += ["--", "--mcp-config", "%s/.piper/piper/mcp.json" % home_in,
                     "--settings", "%s/.piper/piper/settings.json" % home_in,
                     "--append-system-prompt", "\n\n".join(x for x in (sessioncfg.CLAUDE_MD_BLOCK.strip(),
                                                                          self._extra_md(s).strip()) if x)]   # 9.12 D too
        else:
            argv = [HOLE_BIN, "start", agent, s.path, "-n", "--library", "%s:%s/.claude:rw" % (s.cfg, home_in)]
            argv += ["--", "--mcp-config", "%s/.claude/piper/mcp.json" % home_in]
        if self.codex_subagent(s) and os.path.isdir(s.codex_home):
            argv[argv.index("--"):argv.index("--")] = ["--library", "%s:%s:rw" % (s.codex_home, self.codex_in(s, home_in))]
        if s.kind == "prodcheck":   # 9.12 C: its report tool and the observability plugin without asking
            argv += prodcheck.CLAUDE_ARGS
        if self._unrestricted(s):   # opt-in: hole -u, no egress filter
            argv.insert(argv.index("--"), "-u")
        if s.opts.get("docker"):   # opt-in per session: the DinD sidecar (attest checks its mounts too)
            argv.insert(argv.index("--"), "--with-docker")
        if s.rebuild:   # 9.54: this start took the pending image rebuild
            argv.insert(argv.index("--"), "-r")
        main_git = self._main_git(s, s.excludes or [])
        if main_git and main_git in (s.mount_roots or []):   # a linked worktree: git there needs its main
            # repository's .git, read-write - only as _preflight scanned it (not under a library)
            argv[argv.index("--"):argv.index("--")] = ["--library", "%s:%s:rw" % (main_git, main_git)]
        if s.resume and s.claude_mode != "none":
            argv += ["--resume", s.resume]
            if s.fork_pending:
                argv += ["--fork-session"]
        return argv

    def _main_git(self, s: Session, excludes: list[str] | None = None) -> str | None:
        """The git dir outside the project that git there needs (a linked worktree's main
        `.git`, a submodule's `.git/modules/<x>`), when git is open (2026-10-01) - from
        repo_top's record (its common dir; a worktree's own git dir lies inside it), never
        from the project's `.git` file (the sandbox writes that one: it would choose any
        repository on the host).
        `excludes`: the session's effective hole excludes; one hiding `.git` hides this too."""
        if s.kind != "project" or not s.path or self._git_hidden_policy() or \
                (excludes is not None and guard.git_excluded(excludes)):
            return None
        proj = os.path.realpath(s.path)
        with self._roots_lock:
            if self._roots is None:
                self._roots = read_json(REPO_ROOTS_FILE, {})
            known = isinstance(self._roots.get(proj), dict)
        if not known or self.repo_top(s.path, record="") is None:   # unrecorded or changed: fail closed
            return None
        with self._roots_lock:
            rec = dict((self._roots or {}).get(proj) or {})
        gitdir, common_dir = rec.get("gitdir"), rec.get("common")
        if not gitdir or not common_dir or rec.get("top") != proj:   # a subdirectory project: its repo is not mounted
            return None
        if common_dir == proj or common_dir.startswith(proj.rstrip("/") + "/") or not os.path.isdir(common_dir):
            return None
        if any(path and (guard._inside(path, common_dir) or guard._inside(common_dir, path))
               for path, _ in self.forbidden(s)):
            return None   # e.g. a worktree of Piper's checkout (K11): it starts, only without git inside
        return common_dir

    def start_opts_default(self) -> list:
        """policy start_opts_default: START_OPTS the GUI's New session dialog ticks (the dialog's default only)."""
        v = self.policy.get("start_opts_default")
        return [k for k in START_OPTS if isinstance(v, list) and k in v]

    def _unrestricted(self, s: Session) -> bool:
        """hole -u: the policy for every session, or this session's own start option."""
        return bool(self.policy.get("unrestricted_network")) or bool(s.opts.get("unrestricted"))

    def _env(self, s: Session) -> dict:
        selected = set()
        for project in projects.load(PROJECT_PATH).values():
            try:
                selected.update(projects.sandbox_env_config(project)[1])
            except ValueError:
                continue
        keep = {k: v for k, v in os.environ.items()
                if k not in selected and k not in ("GITLAB_TOKEN", "GITLAB_PAT", "PIPER_HOME", "HOLE_SETTINGS_FILE")
                and not k.startswith(("PIPER_", "ORCH_"))}
        keep.update({
            "HOME": s.hole_home,
            "PIPER_REAL_HOME": common.REAL_HOME,
            "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG") or os.path.join(common.REAL_HOME, ".docker"),
            "TERM": "xterm-256color", "COLORTERM": "truecolor",
        })
        if not s.hole_legacy:
            keep["HOLE_SETTINGS_FILE"] = s.hole_settings
        if PROJECT_PATH:
            keep["PROJECT_PATH"] = PROJECT_PATH
        project = projects.find(s.project, PROJECT_PATH) if s and s.kind == "project" else None
        if project:
            keep.update(projects.sandbox_env_values(project))
        return keep

    # ------------------------------------------------------------ pty events
    def _event_loop(self) -> None:
        while True:
            ev = self._events_q.get()
            if ev is None or self._stop_event.is_set():
                return
            try:
                self._on_pty_event(ev)
            except Exception as e:  # noqa: BLE001
                print("piperd: pty event: %s: %s" % (type(e).__name__, e), flush=True)

    def _on_pty_event(self, ev: dict) -> None:
        s = self.sessions.get(ev.get("id", ""))
        if s is None:
            self.hosts.on_event(ev)   # W2: a host shell's exit, else an id piperd forgot
            return
        if ev.get("ev") == "exit":
            self._on_exit(s, ev.get("rc"))
            return
        if ev.get("ev") == "tty":   # `piper attach` came or went (a closed tab included)
            n = 1 if ev.get("n") else 0
            if n != s.tty:
                s.tty = n
                s.tty_used = s.tty_used or bool(n)
                audit({"event": "tty-attach" if n else "tty-detach", "session": s.id})
                self.bump()
            return
        if ev.get("ev") != "data":
            return
        if s.runtime == "direct":
            incoming = strip_ansi(base64.b64decode(ev["data"]).decode("utf-8", "replace"))
            before = re.sub(r"\s+", "", s.text[-256:]).lower()
            combined = before + re.sub(r"\s+", "", incoming).lower()
            auth_markers = ("youraccesstokencouldnotberefreshed", "pleaselogoutandsigninagain") if s.agent == "codex" else ("invalidapikey", "pleaserun/login")
            auth_failed = any(combined.rfind(marker) >= 0 and combined.rfind(marker) + len(marker) > len(before) for marker in auth_markers)
            s.text = (s.text + incoming)[-65536:]
            if auth_failed:
                login = "codex login" if s.agent == "codex" else "/login"
                self._set(s, "attention", "agent-auth", "Agent authentication failed; run %s on the execution machine, then retry the task. No automatic retry." % login)
                return
            # Codex may draw its empty prompt while loading, then show hook trust.
            # An idle native CLI must still be watched for that later dialog.
            if s.state in ("starting", "spawning", "ready", "waiting") or s.state == "attention" and s.phase == "agent":
                screen = self._screen(s)
                if screen == "dialog":
                    self._set(s, "attention", "agent", "answer the agent's login, trust or hook review prompt in the terminal")
                elif screen == "ready" and s.state in ("starting", "spawning", "attention"):
                    self._set(s, "ready", "", "direct session: native agent permissions apply")
            return
        if self._task_pending(s) and s.state in ("working", "waiting"):
            # the human typed into the terminal first: typing the task now would interleave with it
            s.task_sent = True
            s.notice = "the task was not sent - work was already running in the terminal; paste it yourself (Composer ⌘E)"
            s.save()
            self.bump()
            self.pty.call("subscribe", id=s.id, data=False)
            return
        if self._task_pending(s) and s.state in ("attesting", "ready", "attention"):
            s.text = (s.text + strip_ansi(base64.b64decode(ev["data"]).decode("utf-8", "replace")))[-65536:]
            if s.state != "attesting":
                self._deliver_task(s)
            return
        if s.screen_wait:
            if s.state in ("ready", "attention"):
                s.text = (s.text + strip_ansi(base64.b64decode(ev["data"]).decode("utf-8", "replace")))[-65536:]
                self._watch_screen(s)
            else:   # a turn runs (the human answered and typed): Claude's prompt was there
                s.screen_wait = False
                self.pty.call("subscribe", id=s.id, data=False)
            return
        if s.state not in ("starting", "trust", "spawning"):
            return
        s.text = (s.text + strip_ansi(base64.b64decode(ev["data"]).decode("utf-8", "replace")))[-65536:]
        at = s.text.rfind(TRUST_PROMPT)
        answered = at >= 0 and "Launching sandbox for:" in s.text[at:]   # a replay after a restart
        if at >= 0 and not s.trust_seen and not answered:
            s.trust_seen = True
            self.pty.call("unlock", id=s.id)
            self._set(s, "trust", "trust", "hole asks whether to trust this project's settings - answer in the terminal")
            audit({"event": "trust-prompt", "session": s.id})
            return
        # Hole asks about agent updates after the launch banner. Only progress AFTER
        # that prompt closes its input window; old phases must not immediately relock it.
        # The same rule handles chunked output and replay after a daemon reconnect.
        update_at = s.text.rfind(UPDATE_PROMPT)
        progress = s.text[update_at + len(UPDATE_PROMPT):] if update_at >= 0 else s.text
        phase = ""
        for needle, name in PHASES:
            if needle.format(agent=s.agent) in progress:
                phase = name
        if update_at >= 0 and not phase:
            if s.state != "trust" or s.phase != "update":
                self.pty.call("unlock", id=s.id)
                self._set(s, "trust", "update", "hole asks whether to update agents - answer Y, n or s in the terminal")
                audit({"event": "agent-update-prompt", "session": s.id})
            return
        if phase and s.state == "trust":
            s.clear_at = max(s.clear_at, time.time())   # the keys that answered hole's question are no draft (9.12)
            self.pty.call("lock", id=s.id)
            self._set(s, "starting")
        # "attach" too: both lines can arrive in one chunk when the agent dies at once
        if phase in ("agent", "attach") and s.agent_log is None and s.state in self.START_STATES:
            s.agent_log = threading.Thread(target=self._capture_agent_log, args=(s,), daemon=True)
            s.agent_log.start()
        if phase and phase != s.phase:
            self._advance_start_phase(s, phase)
        if phase == "attach" and s.state == "starting":
            self._set(s, "attesting", "attach", "" if self.policy.get("attestation") is False else "verifying the sandbox")
            if self.policy.get("attestation") is False:
                self._attest(s)
            else:
                threading.Thread(target=self._attest, args=(s,), daemon=True).start()

    def _reattest(self, s: Session) -> None:
        """Attestation interrupted by a piperd restart: coverage is runtime state, redo L3 first."""
        if self.policy.get("attestation") is False:
            self._attest(s)
        else:
            threading.Thread(target=self._attest, args=(s, True), daemon=True).start()

    def _advance_start_phase(self, s: Session, phase: str) -> None:
        """Observed startup progress only moves forward; it never grants readiness."""
        order = [name for _, name in PHASES]
        with s.lock:
            if s.state not in ("starting", "spawning"):
                return
            if s.phase in order and order.index(phase) <= order.index(s.phase):
                return
            s.phase, s.phase_since = phase, time.time()
            s.save()
        if phase in ("agent", "attach"):   # hole builds the image before it starts the agent
            self._rebuild_done(s)
        self.bump()

    # ------------------------------------------------------------ 9.54 image rebuild at the next start
    def image_rebuild(self) -> dict:
        """The pending image rebuild: {} = none, else {at} and {session} once a start took it."""
        cur = read_json(IMAGE_REBUILD_FILE, {})
        at = cur.get("at") if isinstance(cur, dict) else None
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            return {}
        out: dict = {"at": at}
        if isinstance(cur.get("session"), str) and cur["session"]:
            out["session"] = cur["session"]
        return out

    def set_image_rebuild(self, on: bool) -> dict:
        """Arm (on) or cancel the rebuild: the next start of any session runs `hole start -r`. Cancelling
        while a start already took it leaves that hole build alone (it is under way)."""
        with _REBUILD_LOCK:
            if on and not self.image_rebuild():
                write_json(IMAGE_REBUILD_FILE, {"at": time.time()})
            elif not on:
                try:
                    os.unlink(IMAGE_REBUILD_FILE)
                except FileNotFoundError:
                    pass
        audit({"event": "image-rebuild", "on": on})
        self.bump()
        return {"image_rebuild": self.image_rebuild()}

    def _claim_rebuild(self, s: Session) -> bool:
        """The start being spawned takes the pending rebuild - one `-r` at a time: while another session
        that took it is still starting, this one does not (two parallel builds of one image). A start that
        failed before the agent left it taken; the next start takes it over."""
        with _REBUILD_LOCK:
            cur = self.image_rebuild()
            if not cur:
                return False
            if self._rebuild_busy(cur, s):
                return False
            write_json(IMAGE_REBUILD_FILE, {"at": cur["at"], "session": s.id})
        audit({"event": "image-rebuild-start", "session": s.id})
        return True

    def _rebuild_busy(self, cur: dict, s: Session) -> bool:
        """Another session holds the rebuild and is still starting (it builds, or will): not a second `-r`."""
        other = self.sessions.get(cur.get("session", ""))
        return other is not None and other.id != s.id and other.state in self.START_STATES + ("preflight", "preparing")

    def _reserve_rebuild(self, s: Session) -> bool:
        """The start dialog's button: the rebuild belongs to this new session (no other start takes it
        meanwhile). Refused while another start builds - two `-r` of one image would race."""
        with _REBUILD_LOCK:
            cur = self.image_rebuild()
            if self._rebuild_busy(cur, s):
                audit({"event": "image-rebuild-busy", "session": s.id, "holder": cur.get("session")})
                return False
            write_json(IMAGE_REBUILD_FILE, {"at": cur.get("at") or time.time(), "session": s.id})
        audit({"event": "image-rebuild", "on": True, "session": s.id})
        return True

    def _rebuild_done(self, s: Session) -> None:
        """hole got past the build (it starts the agent): the rebuild this session took is done."""
        if not s.rebuild:
            return
        with _REBUILD_LOCK:
            if self.image_rebuild().get("session") != s.id:
                return
            try:
                os.unlink(IMAGE_REBUILD_FILE)
            except FileNotFoundError:
                return
        audit({"event": "image-rebuild-done", "session": s.id})

    def _startup_progress_loop(self) -> None:
        """Observe quiet Hole starts off the PTY/mailbox threads, at most once per 3 s."""
        while not self._stop_event.wait(3):
            for s in list(self.sessions.values()):
                try:
                    self._startup_progress(s)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass   # progress is best effort, never a startup gate

    def _startup_progress(self, s: Session) -> None:
        """Read this instance's container status, without logs, env or mount contents."""
        if s.runtime == "direct":
            return
        if s.state not in ("starting", "spawning") or not s.pid or s.phase == "attach":
            return
        inst = s.instance or self._instance(s)
        if not inst:
            return
        r = subprocess.run([DOCKER_BIN, "ps", "-a", "--filter", "label=com.docker.compose.project=" + inst,
                            "--format", '{{.Label "com.docker.compose.service"}}\t{{.State}}\t{{.Status}}'],
                           capture_output=True, text=True, timeout=2)
        if r.returncode:
            return
        rows = dict((parts[0], parts[1:]) for line in r.stdout.splitlines()
                    if len(parts := line.split("\t", 2)) == 3)
        agent = rows.get("agent", [])
        docker = rows.get("docker", [])
        if agent and agent[0] == "running":
            phase = "agent"
        elif docker and "(healthy)" not in docker[1]:
            phase = "dind"
        elif docker:
            phase = "agent"
        elif "gateway" in rows:
            phase = "gateway"
        else:
            return
        self._advance_start_phase(s, phase)

    def _instance(self, s: Session) -> str | None:
        for f in glob.glob(os.path.join(s.hole_dir, "instances", "*.json")):
            d = read_json(f, {})
            if d.get("cliPID") == s.pid:
                return d.get("instanceName")
        return None

    def _attest(self, s: Session, redo_preflight: bool = False) -> None:
        """L4. Any exception fails the session (fail-closed), never leaves it in `attesting`."""
        try:
            self._attest_inner(s, redo_preflight)
        except Exception as e:  # noqa: BLE001
            if self.policy.get("attestation") is False:
                self._attest_done(s, {"ok": True, "failures": [], "checks": 0, "skipped": True})
                return
            self._fail(s, ["attestation crashed: %s: %s" % (type(e).__name__, e)])

    def _attest_inner(self, s: Session, redo_preflight: bool) -> None:
        if self.policy.get("attestation") is False:
            # Instance metadata is also used by sandbox files/logs; one lookup, never a start gate.
            s.instance = s.instance or self._instance(s)
            self._attest_done(s, {"ok": True, "failures": [], "checks": 0, "skipped": True})
            return
        if redo_preflight or not s.coverage:
            # after a piperd restart: L3 again, so L4 has the candidate list it needs
            base, _ = self._base_settings(s)
            pf = self._preflight(s, base)
            if self.policy.get("attestation") is False:
                self._attest_done(s, {"ok": True, "failures": [], "checks": 0, "skipped": True})
                return
            # the sandbox may run already: keep the identities pinned at its start (and what the GUI detached).
            # Only over-mounts of the ORIGINAL coverage keep (their original) identity: a glob match that
            # appeared after the start is in the fresh list, but hole never mounted it - no id, covers nothing
            prev = {c.get("root"): c for c in s.coverage or []}
            for c in pf["coverage"]:
                old = prev.get(c.get("root")) or {}
                fresh = c.get("ids") if isinstance(c.get("ids"), dict) else {}
                if isinstance(old.get("ids"), dict):
                    c["ids"] = {o: v for o, v in old["ids"].items() if o in (c.get("overmounts") or [])}
                else:   # a session started before identities were recorded: the fresh ones, of its own mounts only
                    c["ids"] = {o: v for o, v in fresh.items() if o in (old.get("overmounts") or [])}
                # F-N11-1: the parents pinned at the start too (none for a session started before: fail closed)
                c["pids"] = {o: v for o, v in (old.get("pids") or {}).items() if o in (c.get("overmounts") or [])} \
                    if isinstance(old.get("ids"), dict) else \
                    {o: v for o, v in (c.get("pids") or {}).items() if o in (old.get("overmounts") or [])}
                if old.get("detached"):
                    c["detached"] = list(old["detached"])
            s.coverage, s.mount_roots = pf["coverage"], list(pf.get("mount_roots") or [])
            s.excludes = list(((pf.get("effective") or {}).get("files") or {}).get("exclude") or [])
            if pf["blocks"]:   # incl. settings-changed: hole runs what was approved, not what is on disk now
                self._fail(s, ["preflight at attestation: " + "; ".join(
                    "%s %s" % (b["kind"], b["path"]) for b in pf["blocks"][:5])])
                return
        time.sleep(0.3)
        inst = None
        for _ in range(40):
            if self.policy.get("attestation") is False:
                self._attest_done(s, {"ok": True, "failures": [], "checks": 0, "skipped": True})
                return
            inst = self._instance(s)
            if inst:
                break
            time.sleep(0.25)
        if not inst:
            self._fail(s, ["hole instance for pid %s not found in %s" % (s.pid, s.hole_home)])
            return
        s.instance = inst
        home_in = s.hole_home
        allowed_roots = list(s.mount_roots) + [s.path] + self.allowed_sources(s)   # incl. a worktree's main .git
        mount = (self.codex_in(s, home_in), s.codex_home) if s.claude_mode == "none" else \
            (home_in + "/.claude", s.cfg if s.claude_mode == "session" else os.path.join(common.REAL_HOME, ".claude"))
        if self.policy.get("attestation") is False:   # 9.46 attestation off: ready as soon as hole runs
            res = {"ok": True, "failures": [], "checks": 0, "skipped": True}
        else:
            res = guard.attest(DOCKER_BIN, inst, s.coverage, mount, self.forbidden(s),
                               self.allowed_sources(s), os.path.join(s.hole_dir, "tmp"), s.path,
                               self._policy(s), allowed_roots,
                               git_hidden=guard.git_excluded(s.excludes or []))
        if self.policy.get("attestation") is False:
            res = {"ok": True, "failures": [], "checks": 0, "skipped": True}
        self._attest_done(s, res)

    def _attest_done(self, s: Session, res: dict) -> None:
        """Release the startup input lock after verification, or immediately when L4 is disabled."""
        if self.policy.get("security_strict") and (res.get("skipped") or s.security_sandbox_rev not in (-security.REV, security.REV)):
            res = {"ok": False, "failures": ["Strict requires a session started with Strict and actual sandbox verification; start a new session"], "checks": 0}
        s.attest = res
        if self.policy.get("security_strict") and res.get("ok") and not res.get("skipped"):
            s.security_sandbox_rev = security.REV
        audit({"event": "attest", "session": s.id, "instance": s.instance, "ok": res["ok"],
               "failures": res["failures"]})
        if not res["ok"]:
            self._fail(s, res["failures"])
            return
        self.pty.call("unlock", id=s.id)
        self._set(s, "ready", "", "isolation not verified (off in Settings -> Security)" if res.get("skipped")
                  else "isolation verified (%d checks)" % res["checks"])
        if s.agent == "codex":
            codexcfg.mark_enabled()
        if self._task_pending(s):
            self._deliver_task(s)   # now, or from _on_pty_event once Claude's screen allows it
        elif self._screen(s) == "ready":
            self.pty.call("subscribe", id=s.id, data=False)   # from now on: exit events only
        else:   # a resumed session: nothing typed (inbox, issue state) before a dialog is answered (review 2026-10-05)
            s.screen_wait, s.text = True, ""
            self.pty.call("subscribe", id=s.id, **{"from": 0})   # the whole screen again: what came while attesting too

    @staticmethod
    def _dialog_up(s: Session) -> bool:
        """Claude / Codex shows a dialog (or its screen is not seen at the prompt yet): no typing."""
        return s.screen_wait or (s.runtime == "direct" and s.state == "attention" and s.phase == "agent") or (
            s.state == "attention" and s.detail.startswith(("Claude is asking", "Codex is asking")))

    def _watch_screen(self, s: Session) -> None:
        """A ready session with no task, its screen not seen at Claude's prompt yet: a dialog (folder
        trust, login) makes it attention; the prompt ends the watch (at_prompt allows typing then)."""
        screen = self._screen(s)
        if screen == "ready":
            s.screen_wait = False
            if s.state == "attention" and s.detail.startswith(("Claude is asking", "Codex is asking")):
                self._set(s, "ready", "", "isolation verified")
            self.pty.call("subscribe", id=s.id, data=False)   # from now on: exit events only
        elif screen == "dialog" and s.state == "ready":
            self._set(s, "attention", None, "%s is asking (e.g. whether you trust the folder%s) - answer in the terminal"
                      % (s.agent.capitalize(), ", or to log in" if s.agent == "codex" else ""))

    def _screen(self, s: Session) -> str:
        """claude_screen of the startup output. Once the main screen came after a dialog (folder
        trust...), the keys that answered it are no draft: they do not hold automatic text (9.12)."""
        screen = claude_screen(s.text, s.agent)
        if screen == "ready" and not s.dialog_done and had_dialog(s.text, s.agent):
            s.dialog_done = True
            s.clear_at = max(s.clear_at, time.time())
            if s.hold:
                s.hold = ""
                self.bump()
        return screen

    def _task_pending(self, s: Session) -> bool:
        return bool(s.task) and not s.task_sent and not s.compact_wait   # W8: not while /compact runs

    def _deliver_task(self, s: Session) -> None:
        """Type the task only into Claude's main screen. While a dialog (folder trust...) is
        up, the human answers it first - Enter would pick the dialog's default."""
        if hasattr(self, "quiet") and self.quiet.blocked(s.id):
            return
        screen = self._screen(s)
        if screen == "dialog":
            if s.state == "ready":
                self._set(s, "attention", None, "%s is asking (e.g. whether you trust the folder%s) - answer in the terminal, "
                                                "the task is sent afterwards"
                          % (s.agent.capitalize(), ", or to log in" if s.agent == "codex" else ""))
            return
        if screen != "ready" or getattr(s, "task_sending", False):
            return
        s.task_sending = True

        def send() -> None:
            try:
                time.sleep(1.5)   # let Claude Code finish drawing its prompt
                if claude_screen(s.text, s.agent) != "ready":   # a dialog came up meanwhile: wait for it
                    return
                if not self.auto_type_ok(s):   # the human writes already: the next output retries
                    if s.state == "attention" and s.detail.startswith(("Claude is asking", "Codex is asking")):
                        # the dialog is answered (its keys count as typing): say what holds the task now
                        self._set(s, None, None, "the task waits - you typed in the terminal; \"Send now\" on the "
                                                 "card types it")
                    return
                if s.compact_first:   # W8: shrink the resumed conversation first, the task follows its SessionStart
                    s.compact_first = False
                    if s.agent == "claude":
                        if not self.type_text(s, "/compact"):   # the human's keys came first: left to them
                            s.save()
                            return
                        stamp = s.compact_wait = time.time()
                        s.detail = "compacting the conversation - the new task follows"
                        s.save()
                        self.bump()   # (screen data stays subscribed: the task needs the prompt seen again)
                        threading.Thread(target=self._compact_watchdog, args=(s, stamp), daemon=True,
                                         name="compact-" + s.id).start()
                        return
                self.type_text(s, self._shared_tree_note(s) + s.task)
                s.task_sent = True
                if s.state == "attention" and asking_detail(s.detail):
                    s.state, s.detail = "ready", "isolation verified"
                s.save()
                self.bump()
                self.pty.call("subscribe", id=s.id, data=False)   # from now on: exit events only
            except Exception as e:  # noqa: BLE001
                print("piperd: task for %s: %s: %s" % (s.id, type(e).__name__, e), flush=True)
            finally:
                s.task_sending = False
        threading.Thread(target=send, daemon=True).start()

    COMPACT_MAX_S = 240   # W8: no SessionStart(compact) by then (nothing to compact, an old CLI): the task goes anyway

    def _compact_watchdog(self, s: Session, stamp: float) -> None:
        time.sleep(self.COMPACT_MAX_S)
        if s.compact_wait == stamp:
            self._compact_done(s, "the compaction did not report back - the task was typed anyway")

    def _compact_done(self, s: Session, notice: str = "") -> None:
        """W8: /compact finished (its SessionStart hook) or timed out: Claude sits at its prompt, the task goes."""
        with s.lock:
            if not s.compact_wait:
                return
            s.compact_wait = 0.0
            if s.state in ("working", "waiting") and not s.subagents and not s.background_tasks:
                s.state, s.since = "ready", time.time()   # a slash command is no turn: back at the prompt
            if s.detail.startswith("compacting the conversation"):
                s.detail = ""
            if notice:
                s.notice = notice
            s.save()
        self.bump()
        if self._task_pending(s) and s.state in ("ready", "attention"):
            self._deliver_task(s)

    def continue_session(self, sid: str, task: str) -> dict:
        """W8: go on with an ended session's conversation on a new task - resumed exactly (never a fresh
        start), compacted first (Claude), then the task typed as a start task is."""
        if not isinstance(task, str) or not task.strip() or len(task) > 20000:
            raise ValueError("task must be 1..20000 characters")
        return self.resume_conversation(sid, task=task, compact=True)

    def _shared_tree_note(self, s: Session) -> str:
        """Two Claudes in one checkout: tell the newcomer who else writes there."""
        sib = self.siblings(s)
        if not sib:
            return ""
        who = "; ".join("\"%s\"%s" % (x.title, (" (task: %s)" % x.task.strip().splitlines()[0][:160]) if x.task.strip() else "")
                        for x in sib)
        return ("[piper] %s is also working in the same working tree on the same git branch right now. "
                "Do not change the files it works on, and expect git status/diff to show its changes "
                "too. You share one repository: do not switch branches, rebase or reset, and commit "
                "only your own files.\n\n" % who)

    AGENT_LOG_MAX = 256 << 10
    START_STATES = ("spawning", "trust", "starting", "attesting")

    def _capture_agent_log(self, s: Session) -> None:
        """Follow the agent container's output from `Starting claude agent` until attestation
        ends, into <session>/agent-start.log. An agent that dies on start (a failing prestart
        hook, the CLI not found) is removed by hole within seconds - this is the only record
        of why. The exit code is appended when the container stops while still starting."""
        path = os.path.join(s.dir, "agent-start.log")
        deadline = time.time() + 120
        name = None
        while time.time() < deadline and s.state in self.START_STATES:
            inst = s.instance or self._instance(s)
            if not inst:
                time.sleep(0.2)
                continue
            name = inst + "-agent-1"   # compose project = instance, service = agent
            try:
                with open(path, "wb") as out:
                    p = subprocess.Popen([DOCKER_BIN, "logs", "-f", name], stdout=out,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                    while p.poll() is None:
                        if s.state not in self.START_STATES or out.tell() > self.AGENT_LOG_MAX:
                            p.terminate()   # attested (or failed): Claude's own screen is not logged
                            break
                        time.sleep(0.2)
                    try:
                        p.wait(5)
                    except subprocess.TimeoutExpired:   # a docker CLI that ignores TERM: never a zombie
                        p.kill()
                        p.wait(5)
            except (OSError, subprocess.TimeoutExpired) as e:
                audit({"event": "agent-log-failed", "session": s.id, "error": str(e)})
                return
            with open(path, "rb") as f:
                got = f.read(4096)
            if b"No such container" in got and len(got) < 300:
                time.sleep(0.2)   # not created yet
                continue
            if p.returncode == 0 and s.state in self.START_STATES:   # the container stopped by itself
                try:
                    r = subprocess.run([DOCKER_BIN, "inspect", "-f", "{{.State.ExitCode}} {{.State.Error}}", name],
                                       capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    return
                if r.returncode == 0:
                    with open(path, "ab") as f:
                        f.write(("\n[piperd] agent container exited: code %s\n" % r.stdout.strip()).encode())
            return

    def _agent_log_tail(self, s: Session, lines: int = 12) -> str:
        if s.agent_log:
            s.agent_log.join(5)   # the container is gone: `docker logs -f` has ended or is about to
        try:
            with open(os.path.join(s.dir, "agent-start.log"), "rb") as f:
                text = strip_ansi(f.read()[-8192:].decode("utf-8", "replace"))
        except OSError:
            return ""
        if "No such container" in text and len(text) < 300:
            return ""   # never caught it: hole removed it before `docker logs` got there
        return " | ".join([ln.strip() for ln in text.splitlines() if ln.strip()][-lines:])[-1500:]

    def _fail(self, s: Session, why: list[str]) -> None:
        died = ("no such object", "No such container", "is not running")   # incl. guard.AGENT_NOT_RUNNING
        if any(m in w for w in why for m in died):
            # the agent stopped (or hole already removed it) before attestation: it died on start
            tail = self._agent_log_tail(s)
            why = (["the agent container stopped right after start (hole: \"cannot attach to a stopped container\") - "
                    "most likely the prestart hook or the CLI start failed"]
                   + ["agent output: " + tail if tail else
                      "the agent output could not be captured (%s)" % os.path.join(s.dir, "agent-start.log")]
                   + s.hook_problems + why)
        self._set(s, "error", "", "attestation failed: " + "; ".join(why)[:3000])
        try:
            self.pty.call("signal", id=s.id, sig="TERM")
        except PtyHostError:
            pass

    def _session_down(self, s: Session) -> None:
        """The sandbox ended (or never started): what an extension granted it goes (9.150)."""
        for f in extensions.each("session_down"):
            try:
                f(self, s)
            except Exception as e:  # noqa: BLE001 - one extension's failure never blocks the exit
                audit({"event": "extension-session-down-failed", "session": s.id, "error": str(e)[:300]})

    def _on_exit(self, s: Session, rc) -> None:
        with s.lock:   # with _start's "starting": one of the two goes first, an exit is never overwritten (9.59)
            if s.rc is not None and s.state not in ACTIVE:
                return   # a second exit event of the same process (a ptyhost reconnect): handled once
            # ptyhost reports a signal death as -signum; hole itself exits 143 on SIGTERM, so a
            # KILLed hole (_stop_nudge) gets the same 128+n convention (the GUI reads 137)
            s.rc = 128 - rc if isinstance(rc, int) and rc < 0 else rc
            if s.state != "error":
                # no stale "isolation verified"; an end nobody asked for (a crash, an OOM kill) is said so -
                # the queue lists it under Needs you (feed.derive_actions)
                crashed = s.rc not in (0, 130, 143, None) and not s.stop_first
                # F1-6: hole failing before the agent ran (no docker compose, a bad setting) says why only
                # in the terminal - its last line goes with the message
                why = next((ln.strip() for ln in reversed(s.text.splitlines()) if ln.strip()), "") \
                    if crashed and s.state in self.START_STATES else ""
                why = common.scrub(common.no_bidi(why[:200]))   # terminal text: a token on the command line stays there
                s.state, s.since = "exited", time.time()
                s.detail = "%s ended unexpectedly (code %s)%s" % (s.agent.capitalize(), s.rc, ": " + why if why else "") \
                    if crashed else ""
        self._session_down(s)
        try:
            if self._login_copy_back(s):
                s.notice = "login saved to the template"
        except OSError:
            pass
        threading.Thread(target=self._network_log, args=(s,), daemon=True).start()
        s.save()
        self.bump()
        audit({"event": "exit", "session": s.id, "rc": rc})
        if s.parent and not (s.handoff or {}).get("reported"):
            threading.Thread(target=self._set_handoff, args=(s, "", False), daemon=True).start()

    def _network_log(self, s: Session) -> None:
        """hole -n writes <session HOME>/.hole/logs/<project>/network-access-<agent>-<id>.log at exit."""
        if s.runtime == "direct":
            return
        if not s.instance:
            s.instance = self._instance(s)
        if not s.instance:
            return
        iid = s.instance.rsplit("-", 1)[-1]
        for _ in range(40):   # the watchdog finishes teardown after the CLI exits
            files = glob.glob(os.path.join(s.hole_dir, "logs", "*",
                                           "network-access-%s-%s.log" % (s.agent, iid)))
            if files:
                break
            time.sleep(0.5)
        else:
            return
        allowed, denied = 0, []
        try:
            with open(files[0], errors="replace") as f:
                for line in f:
                    if "DENIED" in line:
                        name = line.replace("DENIED", "").strip(" \t:-\n")
                        if name and name not in denied:
                            denied.append(name[:200])
                    elif "ALLOWED" in line:
                        allowed += 1
        except OSError:
            return
        s.network = dict(s.network or {}, denied=denied[:200], allowed_count=allowed)
        try:
            s.save()
        except OSError:   # the session dir went away meanwhile (removed by hand)
            return
        self.bump()

    # ------------------------------------------------------------ L5 runtime watcher (6.6)
    def _sync_watch(self) -> None:
        """Clear disabled runtime warnings and start L5 only while enabled; cancel old scans on disable."""
        with self.lock:
            if not self._background_enabled or self._stop_event.is_set():
                return
            off = self.policy.get("l5_action") == "off"
            for s in self.sessions.values():
                legacy = s.state == "attention" and (
                    off and s.detail.startswith("L5:") or self._git_hole() and s.detail.startswith("git guard:"))
                if off and s.watch or legacy:
                    if off:
                        s.watch = []
                    if legacy:
                        s.state, s.since, s.detail = "waiting" if s.idle else "working", time.time(), ""
                    s.save()
            if off:
                self._watch_stop.set()
                return
            if self._watch_thread is not None and self._watch_thread.is_alive() and not self._watch_stop.is_set():
                return
            self._watch_stop = threading.Event()
            self._watch_thread = self._start_service(self._watch_loop, "piper-l5", (self._watch_stop,))

    def _watch_loop(self, stop: threading.Event) -> None:
        last: dict[str, float] = {}
        while not stop.wait(2):
            for s in list(self.sessions.values()):
                if s.state not in ("ready", "working", "waiting", "attention") or not s.coverage or \
                        self.policy.get("l5_action") == "off":   # 9.46 l5_watch off
                    continue
                every = WATCH_INTERVAL * (4 if s.kind == "advisor" else 1)   # $PROJECT_PATH is big
                if time.time() - last.get(s.id, 0) < every:
                    continue
                last[s.id] = time.time()
                try:
                    self._watch(s, stop)
                except Exception as e:  # noqa: BLE001
                    print("piperd: L5 %s: %s" % (s.id, e), flush=True)

    def _watch(self, s: Session, stop: threading.Event | None = None) -> None:
        """A sensitive file that appeared after the start is NOT over-mounted (globs are
        expanded once, K7): warn, or stop the session when policy says l5_action=stop."""
        if self.policy.get("l5_action") == "off" or stop is not None and stop.is_set():
            return
        new, cut = [], []
        skip = [guard._host(x, common.REAL_HOME) for x in self.policy.get("scan_skip_roots") or []]
        for c in s.coverage:
            if self.policy.get("l5_action") == "off" or stop is not None and stop.is_set():
                return
            if c.get("kind") == "include" or any(guard._inside(c["root"], sk) for sk in skip):
                continue   # includes are config dirs outside any project; L3 checked them at start
            # a GUI op detached some; a sandbox may have moved a mountpoint's parent (identity changed)
            intact = guard.intact_overmounts(c)
            over = set(c.get("overmounts") or []) - set(c.get("detached") or []) if intact is None else intact
            truncated: list[str] = []
            cands = guard.scan_candidates(c["root"], {**self.policy,
                "candidates": self.policy.get("l5_candidates", self.policy.get("candidates", []))}, over, truncated)
            new += [os.path.join(c["root"], x) for x in guard.uncovered(c["root"], cands, over)]
            cut += [os.path.join(c["root"], x) for x in truncated]
        with self.lock:
            if self.policy.get("l5_action") == "off" or stop is not None and stop.is_set():
                return
            if cut:   # a tree that grew past max_depth / the symlink budget: not silently unwatched
                new += ["[scan incomplete] " + p for p in cut[:5]]
            fresh = [p for p in new if p not in s.watch]
            legacy_flag = s.state == "attention" and s.detail.startswith("L5:")
            if not fresh and not legacy_flag:
                return
            if fresh:
                s.watch = sorted(set(s.watch) | set(fresh))
                audit({"event": "l5-uncovered", "session": s.id, "paths": fresh})
            if fresh and self.policy.get("l5_action") == "stop":
                self._fail(s, ["L5: new sensitive file(s) visible in the sandbox: " + ", ".join(fresh[:5])])
                return
            if legacy_flag:   # old flag-only warnings changed the activity state, even while the agent worked
                s.state, s.since, s.detail = "waiting" if s.idle else "working", time.time(), ""
            s.save()
            self.bump()

    # ------------------------------------------------------------ git on the host (8)
    def hidden_for(self, s: Session):
        """`hidden(repo-relative path)` for what this session must not see through piperd:
        policy candidates, the effective hole excludes of its project, anything outside its
        project dir (a project may be a subdirectory of a repository)."""
        cands = list(self.policy["candidates"])
        excludes = list(s.excludes or [])
        top = self._session_top(s)
        if top is None:
            return lambda p: True   # the repository cannot be told: nothing is shown
        moved = self._broken_overmounts(s)[0]
        if moved:   # N10: the real hidden files live on under names the rule does not hide - every file is hidden
            def hide_all(p: str) -> bool:
                return True
            hide_all.moved = moved
            return hide_all
        proj = os.path.realpath(s.path)

        def hidden(p: str) -> bool:
            rel = os.path.relpath(os.path.join(top, p), proj)
            if rel == ".." or rel.startswith("../"):
                return True
            return guard.hidden_by(cands, rel) or guard.hidden_by(excludes, rel)
        return hidden

    def _broken_overmounts(self, s: Session) -> tuple[list[str], list[str]]:
        """N10: the over-mounts of s's running sandbox whose identity pinned at the start is no longer at their path
        (and no GUI operation detached them): (moved, replaced). moved - anything but a regular file in the parent
        directory pinned at the start (F-N11-1: a stand-in FILE after the parent moved too): the
        sandbox moved the mount's parent (the mount goes along, verified with Docker Desktop) and the REAL hidden
        files now sit on the host under a name the isolation rule does not hide - host git / reads by name hand
        them out (`git add cfgx` staged them card-free). replaced - another regular file at the path in the pinned
        parent: a host editor's temp + rename or host git's checkout (the content keeps its hidden name, by-name
        checks still apply) - a card, unless the opt-in git_hidden_server_content is on (2026-10-12)."""
        moved: list[str] = []
        replaced: list[str] = []
        if s.state not in self.SANDBOX_STATES or s.state in self.PRE_SANDBOX_STATES:
            return moved, replaced
        for c in s.coverage or []:
            if c.get("kind") == "include" or not c.get("root") or not isinstance(c.get("ids"), dict):
                continue
            intact = guard.intact_overmounts(c) or set()
            for o in sorted(set(c.get("overmounts") or []) - set(c.get("detached") or []) - intact):
                if not isinstance(c["ids"].get(o), list):
                    continue   # not there at the start: nothing pinned, nothing moved
                full = os.path.join(c["root"], o)
                pid = (c.get("pids") or {}).get(o) if isinstance(c.get("pids"), dict) else None
                try:   # F-N11-1: a regular file in the very directory pinned at the start - else a stand-in
                    st, pst = os.lstat(full), os.lstat(os.path.dirname(full))
                    editor = _stat.S_ISREG(st.st_mode) and pid == [pst.st_dev, pst.st_ino] and \
                        guard.direct_path(c["root"], o)
                except OSError:
                    editor = False
                (replaced if editor else moved).append(full)
        return moved, replaced

    def project_hidden(self, p: dict):
        """What the Global agent may not read in project `p`: the same rule the project's own
        session lives under - policy candidates + the effective hole excludes (base, the
        project's .hole/settings.json, profile) - plus every .git.

        The project's settings file is sandbox-writable: removing an exclude from it (or
        breaking it) must not reveal anything, so the excludes are the union over its live,
        its last approved version and what a session of it pinned at start; without the base
        settings everything is hidden (fail closed) - an Unreadable rule, which the GUI shows as
        one "policy unreadable" flag (S2)."""
        cands = list(self.policy["candidates"])
        profile = p["profile"] or holehome.PROJECT_PROFILE
        try:
            base, _ = self._base_settings()
            excludes = set((guard.effective(base, {}, profile).get("files") or {}).get("exclude") or [])
        except (ValueError, OSError, KeyError, TypeError, RecursionError) as e:
            return Unreadable("the base hole settings cannot be read: %s" % e)
        docs = []
        try:
            docs.append(json.loads(common.safe_read(p["path"], ".hole/settings.json", 1 << 20).decode() or "{}"))
        except (OSError, ValueError):
            pass   # missing or unreadable now: the approved version still counts
        approved = read_json(common.APPROVED_FILE, {})
        real_file = os.path.realpath(os.path.join(p["path"], ".hole", "settings.json"))
        for k, v in approved.items():   # keyed by the path preflight saw (maybe not the real one)
            if k.endswith("/.hole/settings.json") and os.path.realpath(k) == real_file:
                try:
                    docs.append(json.loads((v or {}).get("text") or "{}"))
                except ValueError:
                    pass
        for d in docs:
            try:
                excludes |= set((guard.effective(base, d, profile).get("files") or {}).get("exclude") or [])
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
                continue
        real = os.path.realpath(p["path"])
        for s in list(self.sessions.values()):
            if s.path and os.path.realpath(s.path) == real:
                excludes |= set(s.excludes or [])
        git_hidden = guard.git_excluded(sorted(excludes))   # before the forced .git: does a sandbox see it hidden?
        if self.policy.get("hidden_git") is not False:   # 9.46 hidden_files off: hole's excludes only
            excludes.add(".git")
        excl = sorted(excludes)
        rule = lambda rel: guard.hidden_by(cands, rel) or guard.hidden_by(excl, rel)  # noqa: E731
        rule.git_hidden = git_hidden   # R1+: git_run project=<library> reads it (_git_run)
        return rule

    def _checkout_rule(self, p: dict):
        """project_hidden(p) for repository-relative paths (GitLab diffs): a checkout may be a
        subdirectory of its repository; a path outside it is another checkout's business."""
        rule = self.project_hidden(p)
        proj = os.path.realpath(p["path"])
        top = self.repo_top(proj)
        if top is None:
            return lambda path: True   # fail closed: an MR of this project shows no file content

        def hidden(path: str) -> bool:
            rel = os.path.relpath(os.path.join(top, path), proj)
            return not (rel == ".." or rel.startswith("../")) and rule(rel)
        return hidden

    def repo_top(self, path: str, record: str = "view") -> str | None:
        """The trusted repository root of a project dir, or None (callers fail closed).

        piperd runs git on the host in trees a sandbox writes; a .git planted there (a project
        without a repository, or a subdirectory project, has nothing for hole to mask) would
        hand git a sandbox-written config (filters, hooks, a gitdir: into another repository,
        another origin). So root, git dir and common dir are recorded per project once, while
        no sandbox can have written there, and git is trusted only while it answers the same.
        Since git is open in the sandbox (2026-10-01) its .git config is sandbox-written too:
        the record still pins WHICH repository (and origin, origin_gl), not its content.
        record: "start" (_start, before the sandbox) or "view" (GUI, poller) record only while
        no sandbox that could write the project runs; "" never records."""
        proj = os.path.realpath(path)
        with self._roots_lock:
            if self._roots is None or not isinstance(self._roots.get(proj), dict):
                self._roots = read_json(REPO_ROOTS_FILE, {})   # a row removed by hand stays removed
            rec = self._roots.get(proj)
            if not isinstance(rec, dict):   # never recorded
                why = self._may_record(proj, record)
                if why:
                    self._audit_once(proj, "repo-root-unrecorded", {"why": why})
                    return None
                top = self._first_top(proj)
                ids = self._git_ids(top) if top else None
                if ids and not self._outside_sandboxes(proj, ids):
                    self._audit_once(proj, "repo-root-untrusted", {"ids": ids})
                    return None
                rec = dict(zip(("top", "gitdir", "common"), ids)) if ids else {"top": ""}
                url = projects.origin_url(top) if ids else None
                if url is not None:
                    rec["origin"] = url   # origin_gl: the sandbox may write .git/config later
                self._roots[proj] = rec
                write_json(REPO_ROOTS_FILE, self._roots)
        if not rec.get("top"):
            return None
        # first (on the recorded dirs, no git run): a planted commondir would change the answer below
        if not self._git_guard(rec):   # a sandbox-written .git that could not be cleaned: no host git
            self._audit_once(proj, "git-guard-failed", {"common": rec.get("common")})
            return None
        live = self._git_ids(proj)
        if live != (rec["top"], rec.get("gitdir"), rec.get("common")):
            if os.path.isdir(proj):   # a deleted worktree is gone, not tampered with: no audit noise
                self._audit_once(proj, "repo-root-changed", {"recorded": rec, "now": live})
            return None
        return rec["top"]

    def stack_path(self, path: str) -> str:
        """The checkout whose dev stack (the provider's stack, dev DB connections, a session's stack access) the
        project dir `path` uses: in a linked worktree the same folder of the main checkout - most work in
        a worktree wants the main stack and its data - unless that worktree's own choice
        (worktree_stack_mode, the Stack panel) or else policy worktree_stack is "own"."""
        real = os.path.realpath(path)
        link = self.worktree_link(real)
        if not link or self.worktree_stack_mode(link["top"]) == "own":
            return real
        return link["main"]

    def worktree_link(self, path: str) -> dict | None:
        """{top, main_top, main} when `path` lies in a linked worktree whose main checkout is proven: only
        recorded roots decide (repo_top) - the main checkout must be the recorded one of the same common
        dir and `main` the same folder there; anything unproven is None (the path keeps its own stack)."""
        real = os.path.realpath(path)
        top = self.repo_top(real, record="")
        with self._roots_lock:
            common = ((self._roots or {}).get(real) or {}).get("common") if top else None
        if not common or os.path.basename(common) != ".git":
            return None
        main_top = os.path.dirname(common)
        if main_top == top:
            return None
        main = os.path.normpath(os.path.join(main_top, os.path.relpath(real, top)))
        if not os.path.isdir(main) or self.repo_top(main, record="") != main_top:
            return None
        with self._roots_lock:
            rec = (self._roots or {}).get(main) or {}
        if rec.get("common") != common or rec.get("gitdir") != common:
            return None
        return {"top": top, "main_top": main_top, "main": main}

    def _worktree_stack_info(self, s: Session) -> dict | None:
        """piper_session: the dev stack a linked worktree session uses - {mode main|own, main, own_stack}"""
        if s.kind != "project" or not s.path:
            return None
        link = self.worktree_link(s.path)
        if not link:
            return None
        try:
            own = self._stack().worktree(link["top"], within=link["top"])
        except Exception:  # noqa: BLE001 - provider down: unknown, not "none"
            own = None
        return {"mode": self.worktree_stack_mode(link["top"]), "main": link["main_top"],
                "own_stack": str(own.get("name") or own.get("root")) if own else None}

    def _worktree_stack_tool(self, s: Session, args: dict, reason: str) -> dict:
        """piper_worktree_stack: the agent proposes the main checkout's stack or the worktree's
        own - set up and started when it has none (the provider's adopt). One card, kind stack_mode (Security >
        Approvals > Agents and Piper); off = done on its own, audited. Nothing is stopped either way."""
        mode = str(args.get("mode") or "")
        if mode not in ("main", "own"):
            raise ValueError("mode must be main or own")
        if s.kind != "project" or not s.path:
            raise ValueError("only a project session in a linked worktree has a stack to choose")
        link = self.worktree_link(s.path)
        if not link:
            raise ValueError("this session's checkout is not a linked worktree of a recorded main checkout - "
                             "it always uses its own stack")
        top, td = link["top"], self._stack()
        own = td.worktree(top, within=top) if mode == "own" else None
        now = self.worktree_stack_mode(top)
        if now == mode and (mode == "main" or own):
            return {"mode": mode, "changed": False, "note": "already so - nothing to change"}
        setup = mode == "own" and not own
        if mode == "main":
            what = "use the main checkout's dev stack (%s); its own stack, if running, keeps running" % link["main_top"]
        elif setup:
            what = ("set up and start its OWN dev stack: own ports and data folder, data restored from the backup store "
                    "when missing (minutes), policy-checked start; the main checkout's stack keeps running")
        else:
            what = "use its own dev stack (%s)" % str(own.get("name") or own.get("root"))
        summary = "Session %s wants the worktree %s to %s.\n\nThe Stack panel, the database view and the agent's dev " \
                  "connection follow it; running sessions keep their stack access until restarted.%s" % (
                      s.title, top, what, "\n\nReason: " + reason if reason else "")
        if not self.ask("stack_mode", s, summary, {"worktree": top, "mode": mode, "setup": setup}):
            raise ValueError("the user declined")
        job = td.adopt(top) if setup else {}
        self.set_worktree_stack_mode(top, mode)
        audit({"event": "agent-stack-mode", "session": s.id, "mode": mode, "job": job.get("id")})
        out = {"mode": mode, "changed": True}
        if setup:
            out.update(job=str(job.get("id") or ""), note="setting up - the Stack panel shows the job; "
                       "the stack is usable when it finishes")
        return out

    def _worktree_stack_rows(self, drop: str = "") -> dict:
        """worktree-stacks.json without the rows of worktrees that are gone (or `drop`), rewritten when one was
        pruned - a removed worktree's choice must not stay behind for one recreated at that path. Under _roots_lock."""
        rows = read_json(WORKTREE_STACKS_FILE, {})
        rows = rows if isinstance(rows, dict) else {}
        kept = {k: v for k, v in rows.items() if isinstance(k, str) and k != drop and v in ("main", "own") and os.path.isdir(k)}
        if kept != rows and os.path.exists(WORKTREE_STACKS_FILE):
            write_json(WORKTREE_STACKS_FILE, kept)
        return kept

    def worktree_stack_mode(self, top: str) -> str:
        """"own" | "main" for the worktree checkout `top`: its own choice, else policy worktree_stack"""
        with self._roots_lock:
            own = self._worktree_stack_rows().get(os.path.realpath(top))
        if own in ("main", "own"):
            return own
        return "own" if self.policy.get("worktree_stack", "main") == "own" else "main"

    def set_worktree_stack_mode(self, top: str, mode: str) -> None:
        if mode not in ("main", "own"):
            raise ValueError("mode must be main or own")
        top = os.path.realpath(top)
        with self._roots_lock:
            rows = self._worktree_stack_rows()
            rows[top] = mode
            write_json(WORKTREE_STACKS_FILE, rows)

    def forget_worktree_stack(self, top: str) -> None:
        """the worktree `top` was removed: its stack choice goes with it"""
        with self._roots_lock:
            self._worktree_stack_rows(drop=os.path.realpath(top))

    def stack_worktree(self, path: str) -> dict | None:
        """The provider's stack of stack_path(path): registered at that checkout or above it inside its repository
        (a subdirectory project shares its repository's stack), never a stack around the repository - a
        worktree under .claude/worktrees lies inside the main checkout but is not its stack."""
        sp = self.stack_path(path)
        return self._stack().worktree(sp, within=self.repo_top(sp, record="") or sp)

    def record_worktree_root(self, base_path: str, wt: str) -> str | None:
        """Record the root of a linked worktree piperd itself just created in base's repository:
        _may_record would wait while a session of the base project runs (its sandbox
        writes the tree the worktree lies in). Recorded only when git answers exactly what was made:
        top = wt, git dir = the one its `.git` names and points back from, common = base's recorded one."""
        base_top = self.repo_top(base_path, record="")
        wt = os.path.realpath(wt)
        with self._roots_lock:
            base = dict((self._roots or {}).get(os.path.realpath(base_path)) or {})
        ids = self._git_ids(wt)
        gd = projects.worktree_gitdir(base_path, wt)
        if not base_top or not ids or not gd or ids != (wt, os.path.realpath(gd), base.get("common")):
            audit({"event": "repo-root-unrecorded", "project": wt, "why": "worktree ids", "ids": ids})
            return None
        rec = {"top": wt, "gitdir": ids[1], "common": ids[2]}
        if base.get("origin"):
            rec["origin"] = base["origin"]
        with self._roots_lock:
            self._roots = read_json(REPO_ROOTS_FILE, {})
            self._roots[wt] = rec
            write_json(REPO_ROOTS_FILE, self._roots)
        self._roots_audited.pop(wt, None)
        return wt

    def forget_root(self, path: str) -> None:
        """A removed worktree: a worktree added again under the same name is recorded anew."""
        proj = os.path.realpath(path)
        for s in list(self.sessions.values()):
            if os.path.realpath(s.path) == proj:
                self._drop_turn_snapshots(s)
        with self._roots_lock:
            self._roots = read_json(REPO_ROOTS_FILE, {})
            if self._roots.pop(proj, None) is not None:
                write_json(REPO_ROOTS_FILE, self._roots)
        self._roots_audited.pop(proj, None)

    def repo_top_why(self, path: str) -> str:
        """Why repo_top(path) answered None, for the error the GUI shows."""
        proj = os.path.realpath(path)
        with self._roots_lock:
            rec = (self._roots or {}).get(proj)
            last = self._roots_audited.get(proj)
        if isinstance(rec, dict) and not rec.get("top"):
            return "the project has no git repository"
        event = json.loads(last)[0] if last else ""
        return {
            "repo-root-unrecorded": "none recorded yet, and a sandbox that can write the project runs - "
                                    "stop its sessions (and any of an enclosing directory), then start one again",
            "repo-root-changed": "git now answers a different root than the one recorded - see the audit log",
            "repo-root-untrusted": "its git dir is where an enclosing project's sandbox could have written it "
                                   "- see the audit log",
        }.get(event, "see the audit log")

    def _audit_once(self, proj: str, event: str, data: dict) -> None:
        """One audit line per project and answer (the overview asks every 20 s)."""
        key = json.dumps([event, data], sort_keys=True, default=str)
        if self._roots_audited.get(proj) != key:
            self._roots_audited[proj] = key
            audit(dict({"event": event, "project": proj}, **data))

    def _outside_sandboxes(self, proj: str, ids: tuple) -> bool:
        """The git dir and common dir must not be where a session of an ENCLOSING project
        could have written them (hole masks only that project's own top-level .git)."""
        for x in list(self.sessions.values()):
            root = os.path.realpath(x.path) if x.kind == "project" and x.path else ""
            if not root or root == proj or not proj.startswith(root.rstrip("/") + "/"):
                continue
            for d in ids[1:]:
                inside = d == root or d.startswith(root.rstrip("/") + "/")
                masked = d == os.path.join(root, ".git") or d.startswith(os.path.join(root, ".git") + "/")
                if inside and not masked:
                    return False
        return True

    def _may_record(self, proj: str, mode: str) -> str:
        """"" when recording is allowed now, else why not ("same" / "enclosing": a sandbox
        that can write the project runs)."""
        # every session whose sandbox could write proj: its own, or one of an enclosing dir
        covers = [x for x in list(self.sessions.values()) if x.kind == "project" and x.path and (   # the advisor mounts none
            os.path.realpath(x.path) == proj or proj.startswith(os.path.realpath(x.path).rstrip("/") + "/"))]
        # "view" as "start": a project used before this record existed is recorded once none
        # of its sandboxes runs - the residual trust documented in 15.12
        if mode not in ("start", "view"):
            return "never"
        live = [x for x in covers if x.state in ACTIVE and x.pid]
        if any(os.path.realpath(x.path) != proj for x in live):
            return "enclosing"
        return "same" if live else ""

    def _config_writable(self, proj: str) -> bool:
        """Can a running sandbox write this project's .git/config now: _may_record's own or
        enclosing sessions, or - since git is open (2026-10-01) - a read-write mount of another
        session reaching in (a library over or inside it, a worktree's main .git)."""
        if self._may_record(proj, "view"):
            return True
        return any(guard._inside(c["root"], proj) or guard._inside(proj, c["root"])
                   for x in list(self.sessions.values())
                   if x.kind == "project" and x.path and x.state in ACTIVE and x.pid
                   for c in x.coverage or []
                   if isinstance(c, dict) and c.get("readwrite") and c.get("root") and c.get("kind") != "project")

    @staticmethod
    def _git_ids(d: str) -> tuple[str, str, str] | None:
        """(work tree root, git dir, common dir) as git answers from d, real paths."""
        try:
            out = gitops._git(d, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir",
                              session=True).splitlines()
        except gitops.GitError:
            return None
        if len(out) != 3:
            return None
        return (os.path.realpath(out[0]), os.path.realpath(out[1]), os.path.realpath(os.path.join(d, out[2])))

    @staticmethod
    def _first_top(proj: str) -> str | None:
        try:
            outer = os.path.realpath(gitops.toplevel(os.path.dirname(proj), session=True))
        except gitops.GitError:
            outer = None
        try:
            top = os.path.realpath(gitops.toplevel(proj, session=True))
        except gitops.GitError:
            return None
        if top == proj:
            # its own repository - unless the enclosing one tracks files inside it: then it is a
            # subdirectory project and this .git was planted (a submodule is its gitlink only)
            if outer:
                rel = os.path.relpath(proj, outer)
                try:
                    rows = gitops._git(outer, "ls-files", "-s", "-z", "--", gitops.literal(rel + "/"), session=True)
                except gitops.GitError:
                    rows = ""
                for row in filter(None, rows.split("\0")):
                    meta, _, path = row.partition("\t")
                    if not (meta.startswith("160000 ") and path == rel):
                        return outer
            return top
        return top if top == outer else None   # a work tree pointed elsewhere: never trusted

    def origin_gl(self, path: str) -> str | None:
        url = self._trusted_origin(path)
        return projects.gitlab_match(url, self.gitlab_host()) if url else None

    def origin_gh(self, path: str) -> str | None:
        """W23: `owner/repo` (github.com) or `host/owner/repo` (GHE) of a checkout's trusted origin."""
        url = self._trusted_origin(path)
        if not url:
            return None
        for host in self.github_hosts():
            r = projects.github_match(url, [host])
            if r:
                return r if host == "github.com" else "%s/%s" % (host, r)
        return None

    def _trusted_origin(self, path: str) -> str | None:
        """The origin URL of a checkout, read from its trusted repository only (a
        planted .git/config must not choose which GitLab project a session may read). Since
        git is open in the sandbox (2026-10-01) it may write .git/config: the origin is
        recorded with the repository root, while no sandbox that can write the project runs.
        Without one recorded (a record from before, a checkout that had no origin yet) it is
        read live only while no such sandbox runs, and recorded then; meanwhile None."""
        top = self.repo_top(path)
        if not top:
            return None
        proj = os.path.realpath(path)
        with self._roots_lock:
            rec = (self._roots or {}).get(proj)
            url = rec.get("origin") if isinstance(rec, dict) else None
        if not url:
            if self._config_writable(proj):
                return None   # the live .git/config may be the sandbox's
            url = projects.origin_url(top)
            if url:
                with self._roots_lock:
                    rec = (self._roots or {}).get(proj)
                    if isinstance(rec, dict):
                        rec["origin"] = url
                        write_json(REPO_ROOTS_FILE, self._roots)
        return url or None

    SNAP_KEEP_S = 14 * 86400   # what git gc gave the snapshot objects when they lived in .git

    def _startup_upkeep(self) -> None:
        with self._roots_lock:
            if self._roots is None:
                self._roots = read_json(REPO_ROOTS_FILE, {})
            recs = [r for r in self._roots.values() if isinstance(r, dict) and r.get("common")]
        for rec in recs:   # baselines (or a check, while a sandbox runs) before anyone polls
            try:
                self._git_guard(rec)
            except Exception as e:  # noqa: BLE001
                print("piperd: git guard %s: %s" % (rec.get("common"), e), flush=True)
        self._backfill_origins()
        self._expire_snapshots()
        for s in list(self.sessions.values()):
            if s.hidden or not s.turns:
                continue
            try:
                for t in s.turns:
                    self._pin_turn(s, t["from"], str(t["n"]) + "-start")
                    if t.get("to"):
                        self._pin_turn(s, t["to"], str(t["n"]))
            except (OSError, ValueError, gitops.GitError):
                pass   # old pruned objects: Review reports unavailable, never invents a backup

    def _expire_snapshots(self) -> None:
        """Turn snapshots of sessions ended over two weeks ago: their object store (copies of
        hidden files included) and the turns that name it."""
        for s in list(self.sessions.values()):
            if s.state in ACTIVE or time.time() - (s.since or s.created) < self.SNAP_KEEP_S:
                continue
            store = self._snap_objects(s)
            if not os.path.isdir(store) and not s.turns:
                continue
            self._drop_turn_snapshots(s)
            try:
                s.save()
            except OSError:
                pass

    # ------------------------------------------------------------ .git guard (step 1, 2026-10-04)
    def _guard_writers(self, cdir: str) -> list:
        """Running sessions whose sandbox can write this common git dir: a project of this
        repository with git open, or one mounting it read-write (a worktree's main .git, a library)."""
        out = []
        for x in list(self.sessions.values()):
            if x.kind != "project" or not x.path or x.state not in ACTIVE or not x.pid:
                continue
            with self._roots_lock:
                rec = (self._roots or {}).get(os.path.realpath(x.path))
            mine = isinstance(rec, dict) and rec.get("common") == cdir and \
                not self._git_hidden_policy() and not guard.git_excluded(x.excludes or [])
            mounted = any(isinstance(c, dict) and c.get("readwrite") and c.get("kind") != "project" and c.get("root")
                          and guard._inside(cdir, c["root"]) for c in x.coverage or [])
            if mine or mounted:
                out.append(x)
        return out

    def _git_guard(self, rec: dict) -> bool:
        """What a sandbox wrote into .git that host git would RUN (gitops.guard_*): reverted,
        the writing sessions flagged. With no sandbox able to write it, a change is the user's
        own and becomes the baseline. False: it could not be undone (callers fail closed).
        Off with git_mode "hole" (a plain hole has no such guard either): it only keeps the baseline
        current, so switching to "guarded" later reverts nothing that hole mode allowed."""
        cdir, gitdir = rec.get("common"), rec.get("gitdir")
        if not cdir or not gitdir or not os.path.isdir(cdir):
            return True
        if self._git_hole():
            with self._guard_lock:
                sig = gitops.guard_signature(cdir, gitdir)
                if self._guard_sig.get(cdir) != sig:
                    if self._guard is None:
                        self._guard = read_json(GIT_GUARD_FILE, {})
                    self._guard[cdir] = gitops.guard_state(cdir, gitdir)
                    write_json(GIT_GUARD_FILE, self._guard)
                    self._guard_sig[cdir] = sig
                self._guard_live.discard(cdir)
            return True
        with self._guard_lock:
            sig = gitops.guard_signature(cdir, gitdir)
            if self._guard_sig.get(cdir) == sig:
                return True
            if self._guard is None:
                self._guard = read_json(GIT_GUARD_FILE, {})
            state = gitops.guard_state(cdir, gitdir)
            writers = self._guard_writers(cdir)
            # quiet = no sandbox can write it now NOR could at the last check (what one planted
            # just before it ended is checked once more, not taken as the user's)
            quiet = not writers and cdir not in self._guard_live
            if writers:
                self._guard_live.add(cdir)
            else:
                self._guard_live.discard(cdir)
            base = self._guard.get(cdir)
            if quiet or base is None:
                base = state if quiet else gitops.guard_trusted(state)
                self._guard[cdir] = base
                write_json(GIT_GUARD_FILE, self._guard)
            bad = gitops.guard_violations(state, base)
            if bad:
                q = os.path.join(common.PIPER_HOME, "quarantine", "%s-%s" % (
                    time.strftime("%Y%m%d-%H%M%S"), hashlib.sha256(cdir.encode()).hexdigest()[:8]))
                failed = gitops.guard_revert(cdir, bad, q)
                audit({"event": "git-guard", "common": cdir, "quarantine": q, "failed": failed,
                       "sessions": [x.id for x in writers],
                       "found": [{k: str(v)[:200] for k, v in b.items()} for b in bad][:20]})
                what = ", ".join(sorted({b.get("key") or ("hook " + b["name"] if b["kind"] == "hook" else b["kind"])
                                         for b in bad}))[:300]
                for x in writers:
                    if x.state in ("ready", "working", "waiting", "attention"):
                        x.state, x.since = "attention", time.time()
                        x.detail = ("git guard: the sandbox wrote into .git what git on the host would run (%s) - %s, "
                                    "kept in %s. If it was you, do it again once the session ends."
                                    % (what, "reverted" if not failed else "NOT all reverted, host git stopped", q))
                        try:
                            x.save()
                        except OSError:
                            pass
                self.bump()
                if failed or gitops.guard_violations(gitops.guard_state(cdir, gitdir), base):
                    self._guard_sig.pop(cdir, None)
                    return False
                sig = gitops.guard_signature(cdir, gitdir)
            self._guard_sig[cdir] = sig
            return True

    def _guard_loop(self) -> None:
        """Every GIT_GUARD_INTERVAL s: the .git of every repository a running sandbox can write -
        an IDE's own git polling on the host should find nothing planted for long."""
        while not self._stop_event.wait(GIT_GUARD_INTERVAL):
            try:
                with self._roots_lock:
                    recs = {r.get("common"): r for r in (self._roots or {}).values()
                            if isinstance(r, dict) and r.get("common")}
                for cdir, rec in recs.items():
                    if cdir in self._guard_live or self._guard_writers(cdir):
                        self._git_guard(rec)
            except Exception as e:  # noqa: BLE001
                print("piperd: git guard: %s" % e, flush=True)

    def _backfill_origins(self) -> None:
        """Records from before origins were recorded (2026-10-01): record them now for every
        project no sandbox can write (origin_gl); the rest on their next quiet origin_gl."""
        with self._roots_lock:
            if self._roots is None:
                self._roots = read_json(REPO_ROOTS_FILE, {})
            todo = [p for p, r in self._roots.items() if isinstance(r, dict) and r.get("top") and not r.get("origin")]
        for proj in todo:
            try:
                self.origin_gl(proj)
            except Exception as e:  # noqa: BLE001 - one broken checkout must not stop the rest
                print("piperd: origin of %s: %s" % (proj, e), flush=True)

    def _session_top(self, s: Session) -> str | None:
        if s.kind != "project" or not s.path:
            return None
        return self.repo_top(s.path)

    def gui_sensitive(self, p: dict, top: str):
        """The GUI's one "hidden from Claude" mark (K11) for repository-relative paths of project p:
        project_hidden inside the project (as Files marks it), policy candidates outside it (the
        repository around a project in a subdirectory). Fail closed: an unreadable policy marks
        everything - the git panel never pre-selects a marked file for a commit."""
        rule, cands = self.project_hidden(p), list(self.policy["candidates"])
        proj = os.path.realpath(p["path"])

        def sensitive(path: str) -> bool:
            rel = os.path.relpath(os.path.join(top, path), proj)
            if rel == ".." or rel.startswith("../"):
                return guard.hidden_by(cands, path)
            return rel != "." and bool(rule(rel))
        return sensitive

    def policy_flag(self, p: dict) -> dict:
        """{policy_unreadable: bool, policy_error?: str} of project p for the GUI's answers (S2)."""
        why = policy_unreadable(self.project_hidden(p))
        return {"policy_unreadable": True, "policy_error": why} if why else {"policy_unreadable": False}

    def _git_sensitive(self, s: Session):
        """What a session's sandbox was told to hide (policy candidates + its hole excludes), for
        repository-relative paths: the push check (hidden_deletions) and GitLab - the GUI's mark
        is gui_sensitive."""
        cands, excludes = list(self.policy["candidates"]), list(s.excludes or [])
        top, proj = self._session_top(s) or os.path.realpath(s.path), os.path.realpath(s.path)

        def sensitive(p: str) -> bool:   # p is repo-relative, the excludes project-relative
            rel = os.path.relpath(os.path.join(top, p), proj)
            inside = not (rel == ".." or rel.startswith("../"))
            return guard.hidden_by(cands, p) or (inside and guard.hidden_by(excludes, rel))
        return sensitive

    def git_status(self, sid: str) -> dict:
        s = self.get(sid)
        top = self._turn_top(s)   # the trusted root, or refused: never git in a planted repository
        try:
            p = self.gui_project(s.project)
            mark, flag = self.gui_sensitive(p, top), self.policy_flag(p)
        except KeyError:   # the project left projects.json: what the sandbox was told
            mark, flag = self._git_sensitive(s), {"policy_unreadable": False}
        st = gitops.status(top, mark)
        counts = gitops.line_counts(top)
        for f in st["files"]:
            if f["path"] in counts:
                f["add"], f["del"] = counts[f["path"]]
        st["log"] = gitops.log(top, 5)
        st["session_files"] = self._session_paths(s, top)   # Changes vs Unversioned Files (IntelliJ)
        return dict(st, **flag)

    def project_git_status(self, key: str) -> dict:
        """GET /api/git/status?project= (K2): the git status of a project without a session -
        gitops.status of its trusted repository root ({root, branch, upstream, ahead, behind,
        files: [{path (repo-relative), x, y, orig?, sensitive}]}), `sensitive` = gui_sensitive,
        + policy_flag (an unreadable policy marks every file sensitive - fail closed - and says so)."""
        p = self.gui_project(key)
        top = self.repo_top(p["path"])
        if top is None:
            raise gitops.GitError("no trusted repository for %s: %s" % (p["key"], self.repo_top_why(p["path"])))
        return dict(gitops.status(top, self.gui_sensitive(p, top)), **self.policy_flag(p))

    SANDBOX_READ_MAX = 2 << 20

    def sandbox_file(self, sid: str, path: str) -> str:
        """A file only the session's sandbox has - its /tmp (the agent's scratchpad), never on the host: read
        with `docker exec ... head -c` inside the running agent container. Data only, bounded, audited."""
        s = self.get(sid)
        p = os.path.normpath(path or "") if "\0" not in (path or "") else ""
        if not p.startswith("/tmp/"):
            raise ValueError("only a file in the sandbox's /tmp is read this way")
        if not s.instance:
            s.instance = self._instance(s)
        if not s.instance or s.state not in ("ready", "working", "waiting", "attention"):
            raise ValueError("the session's sandbox is not running - its /tmp is gone")
        try:
            r = subprocess.run([DOCKER_BIN, "exec", s.instance + "-agent-1", "head", "-c", str(self.SANDBOX_READ_MAX + 1), "--", p],
                               capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ValueError("cannot read from the sandbox: %s" % e) from None
        if r.returncode != 0:
            raise ValueError("not readable in the sandbox: %s" % r.stderr.decode("utf-8", "replace").strip()[:200])
        if len(r.stdout) > self.SANDBOX_READ_MAX:
            raise ValueError("larger than 2 MB - open it from the session")
        if b"\0" in r.stdout[:8192]:
            raise ValueError("a binary file")
        audit({"event": "gui-sandbox-read", "session": s.id, "path": p})
        return r.stdout.decode("utf-8", "replace")

    def _session_paths(self, s: Session, top: str) -> list[str]:
        """What this session changed: its first stored snapshot against its latest (piperd's own, on the
        host), plus what it is editing in the running turn (fsedit, a hint - it only sorts the list)."""
        frm = s.turns[0].get("from") if s.turns else None
        to = (s.turns[-1].get("to") or s.turns[-1].get("from")) if s.turns else None
        out: set[str] = set()
        if frm and to and frm != to:
            try:
                for r in gitops.tree_files(top, frm, to, lambda p: False, self._snap_objects(s)):
                    out.update(x for x in (r.get("path"), r.get("orig")) if x)
            except gitops.GitError:
                pass
        out.update((self.fsedit_state(s) or {}).get("paths") or [])
        return sorted(out)

    def git_diff(self, sid: str, file: str | None, untracked: bool) -> str:
        s = self.get(sid)
        top = self._turn_top(s)
        # the GUI's panel: a file hidden from Claude shows its diff too (marked `sensitive` in the status)
        return gitops.strict_filter(gitops.diff(top, file, untracked), guard.is_git_path, "a .git path")

    def git_commit(self, sid: str, message: str, files: list[str], reviewed: dict | None = None, *,
                   agent: bool = False, amend: bool = False) -> dict:
        """agent: the session's own git_commit tool (approved on its card) - never the GUI's pinned review.
        amend (the GUI's Commit panel, 9.25): replaces HEAD - refused once HEAD is on a remote branch."""
        s = self.get(sid)
        if amend:
            if agent:
                raise ValueError("amend is the GUI's")
            gitops.check_amend(self._turn_top(s))
        # an amend of the message alone has no change to review
        r = (self.review_commit(sid, message, files, reviewed, amend=amend)
             if not agent and (files or not amend) else
             gitops.commit(self._turn_top(s), message, files, amend=amend))
        audit({"event": "git-commit", "session": s.id, "files": files, "commit": r["commit"],
               "hooks_disabled": r["hooks_disabled"], **({"amend": True} if amend else {})})
        self.bump()
        return r

    MESSAGE_DIFF_MAX = 60_000
    MESSAGE_TIMEOUT = 90
    MESSAGE_PROMPT = ("You write git commit messages. Answer with the commit message only: a subject line of at "
                      "most 72 characters, optionally a blank line and a short body of what and why. Follow the "
                      "style of the repository's recent subjects (prefixes like fix(scope): / feat:, language, "
                      "tense). No code fences, no quotes, no preamble.")

    @staticmethod
    def _claude_bin() -> str | None:
        for c in (os.environ.get("PIPER_CLAUDE_BIN") or "", shutil.which("claude") or "",
                  os.path.join(common.REAL_HOME, ".local", "bin", "claude")):
            if c and os.path.isabs(c) and os.access(c, os.X_OK):
                return c
        return None

    def git_message(self, sid: str, files: list[str], *, staged: bool = False) -> dict:
        """POST /api/sessions/<id>/git/message {files} (9.25, W6): a commit message drafted by a host
        `claude -p` (Haiku, no tools, no MCP, no project config, empty cwd) from the checked files' diff
        against HEAD and the last 20 subjects. Files hidden from Claude or sensitive go out by count only;
        the diff is scrubbed and capped. The answer is the human's draft - nothing is committed."""
        s = self.get(sid)
        top = self._turn_top(s)
        st = gitops.status(top)
        rows = {f["path"]: f for f in st["files"]}
        files = [f for f in files if isinstance(f, str) and f]
        if not files or any(f not in rows for f in files):
            raise ValueError("check changed files first (refresh the panel)")
        try:
            p = self.gui_project(s.project)
            hide, mark = self._repo_hidden(p, top), self.gui_sensitive(p, top)
        except KeyError:
            hide, mark = (lambda r: True), self._git_sensitive(s)
        send = [f for f in files if not (hide(f) or mark(f) or (rows[f].get("orig") and
                                                                 (hide(rows[f]["orig"]) or mark(rows[f]["orig"]))))]
        held = [f for f in files if f not in send]   # named in the GUI's note (the panel lists them anyway)
        if staged:
            if any(rows[f]["x"] in (" ", "?") for f in files):
                raise ValueError("stage changed files first (refresh the panel)")
            parts = [gitops._git(top, *gitops.VIEW, "diff", "--cached", "--no-ext-diff", "--no-color", "--no-textconv", "--",
                                 *([rows[f]["orig"]] if rows[f].get("orig") else []), f) for f in send]
        else:
            parts = [gitops.diff(top, f, rows[f]["x"] == "?") for f in send]
        diff = gitlab.scrub_text("".join(parts))
        cut = len(diff) > self.MESSAGE_DIFF_MAX
        if cut:
            diff = diff[:self.MESSAGE_DIFF_MAX] + "\n[diff truncated]\n"
        try:
            subjects = gitops._git(top, *gitops.VIEW, "log", "--no-color", "-n", "20", "--format=%s").splitlines()
        except gitops.GitError:
            subjects = []
        prompt = "Recent commit subjects of this repository (style reference):\n%s\n\n" % (
            "\n".join("- " + x for x in subjects) or "(none yet)")
        if held:
            prompt += "%d more changed file(s) are withheld (not shown).\n\n" % len(held)
        prompt += "The changes to commit:\n" + (diff or "(only withheld files)")
        out = self._haiku(prompt, self.MESSAGE_PROMPT)
        audit({"event": "git-message", "session": s.id, "files": len(send), "withheld": len(held),
               "bytes": len(prompt.encode("utf-8")), "truncated": cut})
        return {"message": out.strip("`").strip()[:5000], "withheld": held, "truncated": cut}

    MR_PROMPT = ("You write merge request descriptions. Answer with the title on the first line (at most 72 "
                 "characters, the style of the branch's commit subjects), a blank line, then a short Markdown "
                 "description: what changed and why, then how to verify it. No code fences around the answer, "
                 "no preamble.")

    def mr_message(self, sid: str, target: str) -> dict:
        """POST /api/sessions/<id>/git/mr-message {target} (commit A, 2026-10-19): an MR title and description
        drafted like git_message - from the branch's subjects and its diff against the merge base with the
        target. Files hidden from Claude or sensitive go out by count only; scrubbed, capped, nothing created."""
        s = self.get(sid)
        top = self._turn_top(s)
        target = gitops.check_branch(top, str(target or "").strip())
        base = next((r for r in ("origin/" + target, target) if gitops.ref_exists(top, r)), None)
        if not base:
            raise ValueError("unknown target branch %s" % target)
        mb = gitops._git(top, "merge-base", "--end-of-options", base, "HEAD").strip()
        try:
            p = self.gui_project(s.project)
            hide, mark = self._repo_hidden(p, top), self.gui_sensitive(p, top)
        except KeyError:
            hide, mark = (lambda r: True), self._git_sensitive(s)
        hidden = lambda r: hide(r) or mark(r)   # noqa: E731
        names = [n for n in gitops._git(top, "diff-tree", "-r", "--no-renames", "--name-only", "-z", mb, "HEAD").split("\0") if n]
        held = sorted(n for n in names if hidden(n))
        diff = gitlab.scrub_text(gitops.tree_diff(top, mb, "HEAD", hidden, label="hidden from Claude"))
        cut = len(diff) > self.MESSAGE_DIFF_MAX
        if cut:
            diff = diff[:self.MESSAGE_DIFF_MAX] + "\n[diff truncated]\n"
        subs = gitops.subjects(top, mb)
        prompt = "Commits of the branch, oldest first:\n%s\n\n" % ("\n".join("- " + x for x in subs) or "(none)")
        if held:
            prompt += "%d more changed file(s) are withheld (not shown).\n\n" % len(held)
        prompt += "The branch's changes against %s:\n%s" % (target, diff or "(only withheld files)")
        out = self._haiku(prompt, self.MR_PROMPT).strip("`").strip()
        title, _, rest = out.partition("\n")
        audit({"event": "mr-message", "session": s.id, "files": len(names) - len(held), "withheld": len(held),
               "bytes": len(prompt.encode("utf-8")), "truncated": cut})
        return {"title": title.strip().lstrip("#").strip()[:255], "description": rest.strip()[:20000],
                "withheld": held, "truncated": cut}

    def _haiku(self, prompt: str, system: str) -> str:
        """A host `claude -p` draft: Haiku, no tools, no MCP, no project config, an empty cwd."""
        exe = self._claude_bin()
        if not exe:
            raise ValueError("claude is not installed on the host (PIPER_CLAUDE_BIN)")
        env = {k: v for k, v in os.environ.items() if k not in ("GITLAB_TOKEN", "GITLAB_PAT")
               and not k.startswith(("PIPER_", "ORCH_"))}
        env["HOME"] = common.REAL_HOME
        with tempfile.TemporaryDirectory(prefix="piper-msg-") as cwd:
            try:
                r = subprocess.run([exe, "-p", "--model", "haiku", "--tools", "", "--safe-mode", "--strict-mcp-config",
                                    "--no-session-persistence", "--system-prompt", system],
                                   input=prompt.encode("utf-8"), capture_output=True, cwd=cwd, env=env,
                                   timeout=self.MESSAGE_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise ValueError("claude did not answer within %d s" % self.MESSAGE_TIMEOUT) from None
            except OSError as e:
                raise ValueError("cannot run claude: %s" % e) from None
        out = r.stdout.decode("utf-8", "replace").strip()
        if r.returncode != 0 or not out:
            raise ValueError("claude failed: %s" % (common.scrub(r.stderr.decode("utf-8", "replace").strip()[-300:]) or
                                                    "rc %d" % r.returncode))
        return out

    def hidden_deletions(self, s: Session, top: str, hidden=None) -> list[str]:
        """Hidden files (config/properties, ...) that the commits to push delete. git is open
        in the sandbox (2026-10-01) and sees those files as deleted (they are over-mounted):
        a `git commit -a` there would remove them from the repository."""
        try:
            gone = gitops.unpushed_deletions(top)
        except gitops.GitError as e:   # fail closed: an unchecked push could remove them
            raise ValueError("not pushed: cannot check whether the commits delete hidden files (%s); "
                             "push from a terminal" % e) from None
        sensitive = hidden if hidden is not None else self._git_sensitive(s)
        return [p for p in gone if sensitive(p)]

    def _deletions_why(self, top: str) -> str:
        """The note on a refused push when nothing was fetched from the remote yet."""
        try:
            known = gitops.remote_refs_known(top)
        except gitops.GitError:
            known = True
        return "" if known else (" Nothing was fetched from the remote yet, so the whole history was checked - "
                                 "a git fetch narrows it to the new commits.")

    def git_push(self, sid: str) -> dict:
        s = self.get(sid)
        top = self._turn_top(s)
        gone = self.hidden_deletions(s, top)
        if gone:
            raise ValueError("not pushed: the commits delete hidden files (%s) - most likely a commit from the "
                             "sandbox, where they look deleted. Fix the commits, or push from a terminal if the "
                             "deletion is intended.%s" % (", ".join(gone[:5]), self._deletions_why(top)))
        r = gitops.push(top)
        self.review_done(sid)
        audit({"event": "git-push", "session": s.id, "branch": r["branch"]})
        return r

    def git_push_preview(self, sid: str) -> dict:
        """The Push dialog: gitops.push_preview, plus git_push's own refusal as a block."""
        s = self.get(sid)
        top = self._turn_top(s)
        try:
            mark = self.gui_sensitive(self.gui_project(s.project), top)
        except KeyError:
            mark = self._git_sensitive(s)
        r = gitops.push_preview(top, mark, guard.is_git_path)
        gone = self.hidden_deletions(s, top)
        if gone:
            r["checks"].insert(0, {"level": "block", "text": "The commits delete hidden files (%s) - most likely a commit "
                                   "from the sandbox, where they look deleted. Fix the commits, or push from a terminal "
                                   "if the deletion is intended." % ", ".join(gone[:5])})
        return r

    # ------------------------------------------------------------ GUI workbench (9.6)
    # Views for the human in the host GUI. Files hidden from Claude (project_hidden: policy
    # candidates + hole excludes) are shown here like any other file, flagged `hidden` (2026-10-07,
    # the user's decision): the rule protects the sandbox, which never reaches piperd's port, and
    # nothing in the GUI sends file content to a session. Only `.git` is never served.
    def gui_project(self, key: str) -> dict:
        p = projects.find(str(key or ""), PROJECT_PATH)
        if p is None or not p["exists"]:
            raise KeyError("unknown project %r" % key)
        return p

    def fs(self, key: str, op: str, args: dict) -> dict:
        """Project files for the GUI: everything but `.git`. `hidden` marks what sessions do not see
        (list entries, a read, a grep hit; quick open: the list `hidden` of such paths);
        `policy_unreadable` (+ `policy_error`): the isolation policy cannot be read - nothing is
        marked then, the GUI says so once (S2). `noise` marks a list entry of projectfs.NOISE;
        files / grep leave NOISE out unless all=1. Opening a hidden file is audited without content.
        The old `hidden=1` parameter is accepted and changes nothing."""
        p = self.gui_project(key)
        hidden = guard.is_git_path
        rule = self.project_hidden(p)
        why = policy_unreadable(rule)
        mark = (lambda rel: False) if why else rule   # unreadable: one flag, not every file marked
        flag = {"policy_unreadable": bool(why)}
        if why:
            flag["policy_error"] = why
        every = str(args.get("all") or "0") == "1"
        if op == "list":
            rel = str(args.get("path") or ".")
            if rel.strip("/") == workbench.SCRATCH_DIR and not os.path.lexists(os.path.join(p["path"], workbench.SCRATCH_DIR)):
                return dict({"path": workbench.SCRATCH_DIR, "entries": []}, **flag)   # no scratch file yet - not an error
            out = projectfs.list_dir(p["path"], rel, hidden, compact=True)
            base = "" if out["path"] == "." else out["path"]
            for e in out["entries"]:
                child = os.path.join(base, e["name"]) if base else e["name"]
                e["hidden"] = bool(mark(child))
                e["noise"] = projectfs.is_noise(e["name"])
                segs = (e.get("chain") or "").split("/") if e.get("chain") else []
                for i in range(len(segs)):   # a compacted chain ends above a hidden or noise directory (its own tag)
                    if mark(child + "/" + "/".join(segs[:i + 1])) or projectfs.is_noise(segs[i]):
                        e["chain"] = "/".join(segs[:i])
                        break
            return dict(out, **flag)
        if op == "read":
            out = projectfs.read_file(p["path"], str(args.get("path") or ""), hidden,
                                      gitlab._whole(args.get("offset") or 0, "offset"), gitlab._whole(args.get("limit") or 5000, "limit"))
            out["hidden"] = bool(mark(out["path"]))
            if out["hidden"] and not out["offset"]:   # the offset read_file used (a negative one is 0 - N4)
                audit({"event": "gui-read-hidden", "project": p["key"], "path": out["path"]})
            return dict(out, **flag)
        if op == "grep":
            q = str(args.get("q") or "")
            if not q:
                raise ValueError("empty search")
            # cs: 1 = case sensitive (the default, as before), limit: max matches (200, up to 2000)
            icase = str(args.get("cs") or "1") == "0"
            limit = max(1, min(gitlab._whole(args.get("limit") or projectfs.MAX_MATCHES, "limit"), projectfs.GREP_LIMIT))
            rel, glob = str(args.get("path") or "."), args.get("glob") or None
            top = self.repo_top(p["path"])   # the trusted root only
            out = None
            if top is not None:
                try:
                    out = projectfs.git_grep(p["path"], top, q, hidden, rel, glob, icase, limit, all=every)
                except projectfs.GrepUnavailable:
                    pass
            if out is None:
                out = projectfs.grep(p["path"], q, hidden, rel, glob, icase, limit, all=every)
            for m in out["matches"]:
                m["hidden"] = bool(mark(m["path"]))
            return dict(out, **flag)
        if op == "files":
            slot = self._files_cache.setdefault(p["key"], {})   # popped per project on a change
            hit = slot.get(every)
            if hit and time.time() - hit[0] < 30:
                r = hit[1]
            else:
                r = projectfs.files(p["path"], hidden, all=every)
                slot[every] = (time.time(), r)
            return dict(r, hidden=[f for f in r["files"] if mark(f)], **flag)
        raise ValueError("unknown fs operation %r" % op)

    def _gl_projects(self, p: dict) -> list[str]:
        return p["gitlab"] or [x for x in [self.origin_gl(p["path"])] if x]

    def _gh_projects(self, p: dict) -> list[str]:
        """W23: the GitHub repositories of a project - projects.json "github", else its origin's."""
        return list(p.get("github") or []) or [x for x in [self.origin_gh(p["path"])] if x]

    def gh_scope(self, s: Session) -> list[str]:
        """The repositories a session's github_* tools reach: its project's (live, from the trusted
        origin record), every known project's for the advisor."""
        if s.kind == "advisor":
            return sorted({r for p in projects.load(PROJECT_PATH).values() if p["exists"] for r in self._gh_projects(p)})
        p = projects.find(s.project, PROJECT_PATH)
        return self._gh_projects(p) if p and p["exists"] else []

    def _gh_issues_pick(self, p: dict, gl_project: str):
        """W23: Start work's issue picker on a project without GitLab (or with the GitLab module off): (client,
        entry, allowed) of its GitHub repository; None while GitLab serves it (gl_project names no gh: path)."""
        if gl_project and not gl_project.startswith("gh:"):
            return None
        if not gl_project and ((features.on("gitlab") and self._gl_projects(p)) or not features.on("github")):
            return None
        gh, entry, allowed = self._gh_pick(p)
        if not entry:
            raise github.GitHubError(400, "no GitLab project and no GitHub repository is mapped to %s" % p["key"])
        if gh is None:
            raise github.GitHubError(503, "no GitHub login - run gh auth login on this Mac")
        if gl_project:
            entry = github._target({"repo": gl_project[3:]}, allowed)
        return gh, entry, allowed

    def issues(self, key: str, search: str = "", state: str = "opened", gl_project: str = "") -> dict:
        p = self.gui_project(key)
        pick = self._gh_issues_pick(p, gl_project)
        if pick:
            gh, entry, allowed = pick
            r = github.call(gh, "github_issues", {"repo": entry, "search": search, "per_page": 30,
                                                  "state": {"closed": "closed", "all": "all"}.get(state, "open")},
                            allowed, self.project_hidden(p), scrub=False)   # the human's picker (L1)
            return dict(r, projects=["gh:" + entry.lower()], forge="github")
        if not features.on("gitlab"):
            raise ValueError("GitLab module is off (Settings -> Features)")
        allowed = self._gl_projects(p)
        gl = gitlab.GitLab(self.gitlab_host(), gitlab.token())
        args = {"project": gl_project or (allowed[0] if allowed else ""), "search": search,
                "state": state if state in ("opened", "closed", "all") else "opened", "per_page": 30}
        r = gitlab.call(gl, "gitlab_issues", args, allowed, self.project_hidden(p), scrub=False)   # the human's picker (L1)
        r["projects"] = allowed
        return r

    def issue(self, key: str, iid: int, gl_project: str = "") -> dict:
        p = self.gui_project(key)
        pick = self._gh_issues_pick(p, gl_project)
        if pick:
            gh, entry, allowed = pick
            r = github.call(gh, "github_issue", {"repo": entry, "number": iid}, allowed, self.project_hidden(p), scrub=False)
            return dict(r, notes=r.pop("comments", []), forge="github")   # the picker's words (GitLab's notes)
        if not features.on("gitlab"):
            raise ValueError("GitLab module is off (Settings -> Features)")
        allowed = self._gl_projects(p)
        gl = gitlab.GitLab(self.gitlab_host(), gitlab.token())
        return gitlab.call(gl, "gitlab_issue", {"project": gl_project or (allowed[0] if allowed else ""), "iid": iid},
                           allowed, self.project_hidden(p), scrub=False)   # the human's picker (L1)

    def _fork_unwritten(self, s: Session) -> bool:
        """A fork keeps its copied context until the native child has readable history."""
        if not s.fork_pending:
            return False
        try:
            digest.validate_conversation(s.claude_src, s.claude_session, s.agent)
        except (OSError, ValueError):
            return True
        return False

    def _transcript_src(self, s: Session) -> tuple[str, str | None] | None:
        """(config dir, Claude session id) whose transcript is this session's. With the host's
        ~/.claude (claude_dir=host) an unknown id would pick the newest transcript of ANY of
        the user's own Claude Code sessions - so there it is required."""
        # SessionStart may bind a new ID before Claude writes a transcript (login,
        # trust prompt, no first turn yet). Keep displaying the copied context.
        native = s.claude_session
        if self._fork_unwritten(s):
            native = (s.fork_from or {}).get("conversation")
        if (s.claude_mode == "host" or s.agent == "codex") and not native:
            return None
        return s.claude_src, native

    def conversation(self, sid: str, turns: int = 400) -> dict:
        s = self.get(sid)
        src = self._transcript_src(s)
        if s.agent == "codex":
            if src is None:
                return {"turns": [], "usage": None, "agent": "codex", "note": "the Codex session ID is not known yet"}
            rel = digest.session_file(src[0], src[1], "codex")
            try:
                st = common.safe_stat(s.codex_home, rel) if rel else None
                key = (s.codex_home, rel, st.st_mtime_ns if st else 0, st.st_size if st else 0, turns)
            except OSError:
                key = None
            hit = self._conv_cache.get(s.id)
            if key is not None and hit and hit[0] == key:
                return hit[1]
            r = {"turns": digest.codex_digest(src[0], src[1], turns), "usage": None, "agent": "codex"}
            self._conv_cache[s.id] = (key, r)
            return r
        if src is None:
            return {"turns": [], "usage": None, "note": "the Claude session ID is not known yet"}
        path = self._transcript_file(*src)
        try:
            key = (path, os.path.getmtime(path) if path else 0.0)
        except OSError:
            key = (path, 0.0)
        hit = self._conv_cache.get(s.id)
        if hit and hit[0] == key:
            return hit[1]
        r = {"turns": digest.digest(src[0], src[1], turns, max_chars=20000, max_turns=2000, tools=True),
             "usage": digest.usage(src[0], src[1])}
        self._conv_cache[s.id] = (key, r)
        return r

    @staticmethod
    def _transcript_file(cfg: str, sid: str | None) -> str | None:
        rel = digest.transcript(cfg, sid)
        return os.path.join(cfg, rel) if rel else None

    def search_conversations(self, q: str) -> list[dict]:
        q = (q or "").strip().lower()
        if len(q) < 2:
            raise ValueError("search needs at least 2 characters")
        out = []
        for s in sorted(list(self.sessions.values()), key=lambda x: -x.created):
            if s.hidden:
                continue
            for i, t in enumerate(self.conversation(s.id)["turns"]):
                at = t["text"].lower().find(q)
                if at >= 0:
                    out.append({"session": s.id, "title": s.title, "turn": i, "role": t["role"], "ts": t["ts"],
                                "text": t["text"][max(0, at - 120):at + 240]})
                    if len(out) >= 200:
                        return out
        return out

    def _conversation_sources(self) -> list[dict]:
        grouped = {}
        all_projects = projects.load(PROJECT_PATH)
        for s in list(self.sessions.values()):
            if s.kind != "project":
                continue
            src = self._transcript_src(s)
            native = (src[1] if src else "") or ""
            key = hashlib.sha256((s.agent + "\0" + s.path + "\0" + (s.claude_session or s.id)).encode()).hexdigest()
            labels = self._prompt_labels(s)   # a dict: unpacking it gave its keys (N-GUI B1)
            first, last = labels["first_prompt"], labels["last_prompt"]
            row = {"key": key, "session": s.id, "native": native, "agent": s.agent, "project": s.project,
                   "cfg": s.claude_src, "fallback": s.title, "title_source": s.title_source,
                   "project_title": (all_projects.get(s.project) or {}).get("title", ""),
                   "task": s.task[:1000], "first_prompt": first, "last_prompt": last, "since": s.since,
                   "created": s.created, "source": "piper", "cwd": s.path, "import_from": s.import_from,
                   "state": s.state, "closed": bool(s.hidden), "issue": s.issue, "fork_from": s.fork_from,
                   "find": " ".join([s.title, s.project, s.task[:20000], first, last, native,
                                       str((s.issue or {}).get("iid", ""))])}
            old = grouped.get(key)
            if old is None or (s.state in ACTIVE, s.since, not s.hidden) > (old["state"] in ACTIVE, old["since"], not old["closed"]):
                grouped[key] = row
        return list(grouped.values())

    def _history_index(self):
        with self._conversation_lock:
            if self._conversation_index is None:
                self._conversation_index = digest.ConversationIndex(os.path.join(common.PIPER_HOME, "conversations.sqlite"))
            return self._conversation_index

    def _local_sources(self) -> list[dict]:
        idx = self._history_index()
        with idx.lock:
            local = list(idx.local)
        registered = projects.load(PROJECT_PATH)
        managed = {(s.agent, s.claude_session) for s in list(self.sessions.values()) if not s.import_from}
        out = []
        for s in local:
            if (s["agent"], s["native"]) in managed:
                continue
            # Never substitute a main checkout for a history recorded in a different worktree.
            key = next((k for k, p in registered.items() if os.path.realpath(p["path"]) == os.path.realpath(s["cwd"])), "")
            title = (registered.get(key) or {}).get("title", "")
            out.append({**s, "project": key, "project_title": title, "fallback": "Local " + s["agent"] + " conversation",
                        "title_source": "auto", "task": "", "first_prompt": "", "last_prompt": "", "since": s["created"],
                        "state": "external", "closed": False, "issue": None, "fork_from": None, "import_from": None,
                        "source": "local", "find": " ".join([key, title, s["cwd"], s["native"], s["native_branch"]])})
        return out

    def conversations(self, q: str = "", project: str = "", agent: str = "", period: str = "", offset: int = 0,
                      external: bool = False) -> dict:
        if len(q) > 300 or agent not in ("", "claude", "codex") or period not in ("", "today", "yesterday", "week"):
            raise ValueError("invalid history search")
        if not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise ValueError("invalid history page")
        sources = self._conversation_sources()
        idx = self._history_index()
        idx.refresh(sources, external)
        if external:
            sources = sources + self._local_sources()
        selected = [s for s in sources if (not project or s["project"] == project) and (not agent or s["agent"] == agent)]
        rows, status = idx.search(selected, q)
        now = datetime.datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out = []
        for row in rows:
            activity = row["activity"] or row["since"]
            if (period == "today" and activity < midnight) or (period == "yesterday" and not midnight - 86400 <= activity < midnight) \
                    or (period == "week" and activity < time.time() - 7 * 86400):
                continue
            fallback = row["fallback"]
            generic = re.sub(r" · \d+$", "", fallback).casefold() in {
                row["project"].casefold(), row["project"].split("/")[-1].casefold(), row["project_title"].casefold()}
            explicit = row["title_source"] in ("manual", "issue") or bool(row["issue"]) or (not row["title_source"] and not generic)
            title = fallback if explicit else row["title"] or row["first"] or row["first_prompt"] or row["task"] or fallback
            out.append({"session": row["session"], "conversation": row["native"], "project": row["project"],
                        "agent": row["agent"], "title": " ".join(title.split())[:200], "activity": activity,
                        "created": row["created"], "source": row["source"], "cwd": row["cwd"], "import_from": row["import_from"],
                        "state": row["state"], "closed": row["closed"], "messages": row["messages"] or 0,
                        "first": row["first"] or row["first_prompt"] or row["task"],
                        "last": row["last"] or row["last_prompt"], "answer": row["answer"], "branch": row["branch"] or row.get("native_branch", ""),
                        "snippet": row["snippet"], "available": bool(row["native"] and row["messages"]),
                        "issue": row["issue"], "fork_from": row["fork_from"]})
        for x in out:   # agent- or repo-written text the GUI shows: no bidi control reorders it (MERGE-2 #2)
            for k in ("title", "first", "last", "answer", "snippet", "branch"):
                if isinstance(x.get(k), str):
                    x[k] = common.no_bidi(x[k])
        out.sort(key=lambda x: (q.casefold() in x["title"].casefold() if q else False, x["activity"]), reverse=True)
        counts = {}
        for s in sources:
            counts[s["project"]] = counts.get(s["project"], 0) + 1
        return {"items": out[offset:offset + 50], "total": len(out), "projects": [
            {"key": k, "count": counts[k]} for k in sorted(counts) if k], "index": status,
                "targets": [{"key": k, "path": p["path"]} for k, p in projects.load(PROJECT_PATH).items() if p["exists"]] if external else []}

    def conversation_preview(self, sid: str) -> dict:
        idx = self._history_index()
        src = next((x for x in (self._local_sources() if sid.startswith("local-") else self._conversation_sources()) if x["session"] == sid), None)
        if src is None:
            raise KeyError(sid)
        rows, _ = idx.search([src], "")
        row = rows[0]
        messages = []
        with idx.db() as db:
            for msg in db.execute("SELECT role,substr(text,1,6000) AS text,ts FROM message WHERE key=? ORDER BY ordinal DESC LIMIT 20", (src["key"],)):
                messages.append(dict(msg))
        available, note = False, ""
        try:
            digest.validate_conversation(src["cfg"], src["native"], src["agent"])
            available = True
        except (OSError, ValueError) as e:
            note = str(e)
        for m in messages:
            if isinstance(m.get("text"), str):
                m["text"] = common.no_bidi(m["text"])
        nb = lambda v: common.no_bidi(v) if isinstance(v, str) else v  # noqa: E731
        return {"session": sid, "messages": list(reversed(messages)), "available": available, "note": nb(note),
                "first": nb(row["first"] or src["first_prompt"] or src["task"]), "last": nb(row["last"]), "answer": nb(row["answer"])}

    def _transcript_hidden(self, src: dict, p: dict) -> list[str]:
        """R2: the project-relative names of files project `p` hides that the transcript's tool calls touched
        (absolute paths under the conversation's cwd or the target checkout, relative ones as they stand)."""
        rel = digest.session_file(src["cfg"], src["native"], src["agent"])
        if not rel:
            return []
        rule = self.project_hidden(p)
        if policy_unreadable(rule):
            raise HiddenInTranscript("The isolation rule of this project cannot be read (%s), so Piper cannot tell "
                                     "whether the transcript holds content of files hidden from Claude. Import anyway?"
                                     % policy_unreadable(rule), [])
        inputs, outputs = transcript_paths(src["cfg"], rel, src["agent"])
        bases = []
        for b in (src.get("cwd"), p["path"]):
            if b and os.path.isabs(b):
                for x in (os.path.normpath(b), os.path.realpath(b)):
                    if x not in bases:
                        bases.append(x)
        found: set[str] = set()
        for tok in inputs | outputs:
            path = os.path.normpath(tok)
            if os.path.isabs(path):
                cands = [os.path.relpath(path, b) for b in bases if path == b or path.startswith(b.rstrip("/") + "/")]
            else:
                cands = [path]
            for r in cands:
                if r not in (".", "") and r != ".." and not r.startswith("../") and rule(r):
                    found.add(r)
                    break
        return sorted(found)[:50]

    def import_conversation(self, sid: str, project: str, confirm_checkout: bool = False,
                            confirm_hidden: bool = False) -> dict:
        src = next((x for x in self._local_sources() if x["session"] == sid), None)
        if src is None:
            raise KeyError(sid)
        p = projects.find(project, PROJECT_PATH)
        if p is None or not p["exists"]:
            raise ValueError("Choose an existing registered project for the imported conversation.")
        if os.path.realpath(src["cwd"]) != os.path.realpath(p["path"]) and confirm_checkout is not True:
            raise ValueError("This history belongs to another checkout. Confirm the target project explicitly.")
        # R2 (2026-10-10, the user's decision): warn, do not scrub - a transcript that read hidden files carries
        # their content into the sandbox; only an explicit confirm_hidden copies it
        if confirm_hidden is not True:
            hidden = self._transcript_hidden(src, p)
            if hidden:
                raise HiddenInTranscript("The transcript holds content of files hidden from Claude: %s. Import anyway?"
                                         % ", ".join(hidden), hidden)
        # A synthetic source contains history only; native login, hooks and permissions stay private.
        source = SimpleNamespace(id=sid, agent=src["agent"], project=project, path=p["path"],
                                 claude_session=src["native"], claude_src=src["cfg"], codex_dir=src["cfg"],
                                 import_from={"conversation": src["native"], "agent": src["agent"], "cwd": src["cwd"]})
        rows, _ = self._history_index().search([src], "")
        title = (rows[0]["title"] or rows[0]["first"] or src["fallback"])[:200]
        child = self.create(project, title=title, agent=src["agent"], _fork_source=source)
        return {"session": child.id, "imported": True, "fork": True}

    def switch_agent(self, sid: str, confirm_hidden: bool = False) -> dict:
        """9.140: continue a project session's work with the other agent (Claude <-> Codex) - typically after a
        usage limit. Neither reads the other's native conversation, so the new session gets a handoff document
        (agentswitch.document: the visible messages, where the agent last worked, the host's git facts) and a
        task pointing at it. The source is stopped first (two agents must not write one checkout); its
        conversation stays resumable. R2 as import_conversation: a transcript that touched hidden files needs
        confirm_hidden (warn, never scrub)."""
        s = self.sessions.get(sid)
        if s is None or s.kind != "project":
            raise KeyError(sid)
        with self._switch_lock:   # two requests at once (two tabs): one hand-over, never two agents in one checkout
            if s.id in self._switching:
                raise ValueError("This work is being handed over already.")
            taken = next((t for t in self.sessions.values() if (t.switched_from or {}).get("session") == s.id
                          and t.state in ACTIVE), None)
            if taken is not None:   # a second click while the first switch starts
                raise ValueError("This work already continues in %s." % taken.title)
            self._switching.add(s.id)
        try:
            return self._switch_agent(s, confirm_hidden)
        finally:
            with self._switch_lock:
                self._switching.discard(s.id)

    def _switch_agent(self, s: Session, confirm_hidden: bool) -> dict:
        target = "codex" if s.agent == "claude" else "claude"
        available = features.session_agents()
        if available is not None and not available.get(target):
            raise ValueError(f"{target.title()} is not installed or executable on this server.")
        if available is None and target == "codex" and not features.on("codex"):
            raise ValueError("The Codex module is off (Settings › Modules).")
        p = projects.find(s.project, PROJECT_PATH)
        if p is None or not p["exists"]:
            raise ValueError("The project checkout of this session no longer exists.")
        native = s.claude_session or (s.fork_from or {}).get("conversation")
        rows = agentswitch.messages(s.claude_src, native, s.agent) if native else []
        if not rows:
            raise ValueError("This session has no saved conversation to hand over. Start a new session instead.")
        src = {"cfg": s.claude_src, "native": native, "agent": s.agent, "cwd": s.path}
        if confirm_hidden is not True:
            hidden = self._transcript_hidden(src, p)
            if hidden:
                raise HiddenInTranscript("The conversation holds content of files hidden from agents: %s. "
                                         "Hand it over anyway?" % ", ".join(hidden), hidden)
        where = self._switch_where(s, native)
        if where.get("exists"):   # hole mounts a worktree outside the project only via git.worktreeLinks / Pool
            where["mounted"] = self._mounted(s, where["path"])
            where["masked"] = not where["mounted"] and guard._inside(where["path"], s.path)
        facts = self._switch_facts(s, where)
        doc = agentswitch.document({"agent": s.agent, "session": s.id, "title": s.title, "path": s.path,
                                    "issue": s.issue}, rows, where, facts)
        self._check_limit(s)   # before the stop: a refused limit check leaves the source running
        if (s.state in ACTIVE and s.state != "stopping") or (s.state == "error" and s.pid is not None and s.rc is None):
            self.stop(s.id)   # an "error" whose hole still runs is stopped too
        child = self.create(s.project, title=s.title if s.title_source == "manual" else "", origin="gui",
                            issue=s.issue, opts=dict(s.opts), agent=target, runtime=s.runtime,
                            _switched=({"session": s.id, "agent": s.agent, "conversation": native, "title": s.title,
                                        **({"worktree": where["path"], "branch": where.get("branch") or ""}
                                           if where["where"] == "worktree" else {})},
                                       doc.encode()), _replaces=s)
        if s.interrupted:   # 9.141: its work goes on in the child - nothing left to restore
            s.interrupted = None
            s.save()
        audit({"event": "agent-switch", "session": child.id, "from": s.id, "agent": target,
               "where": where.get("path") or "checkout", "hidden_confirmed": confirm_hidden is True})
        return {"session": child.id, "from": s.id, "agent": target}

    def _switch_where(self, s: Session, native: str) -> dict:
        rel = digest.session_file(s.claude_src, native, s.agent)
        top = self._session_top(s)
        try:
            wts = gitops.worktrees(top) if top else []
        except (OSError, gitops.GitError):
            wts = []
        return agentswitch.last_worktree(s.claude_src, rel, s.agent, s.path, wts) if rel else {"where": "checkout"}

    @staticmethod
    def _mounted(s: Session, path: str) -> bool:
        """Is the host `path` inside what the session's sandbox mounts (the project, its libraries incl. the
        worktree links / pool of its last start)? A containing root alone does not expose a
        child hidden behind an intact exclusion; an independent unmasked view still does."""
        views = [c for c in s.coverage or [] if isinstance(c.get("root"), str)
                 and guard._inside(path, c["root"])]
        if views:
            return any(not guard.covered(os.path.relpath(path, c["root"]), guard.intact_overmounts(c) or set())
                       for c in views)
        return any(guard._inside(path, r) for r in [s.path, *(s.mount_roots or [])])

    def _switch_facts(self, s: Session, where: dict) -> dict:
        """_handoff_facts of the place the agent worked: the checkout, or its worktree - names only, the
        session's hidden rule applied (a worktree mirrors the project, so its repo paths are project paths)."""
        if where["where"] != "worktree":
            return self._handoff_facts(s)
        if not where.get("exists"):
            return {"error": "the worktree no longer exists"}
        cands, excludes = list(self.policy["candidates"]), list(s.excludes or [])
        try:
            st = gitops.session_status(where["path"], lambda r: guard.hidden_by(cands, r) or guard.hidden_by(excludes, r))
            return {"branch": st["branch"], "upstream": st["upstream"] or None,
                    "ahead": st["ahead"] if st["upstream"] else None, "withheld": st["withheld"],
                    "uncommitted": [f["path"] for f in st["files"]][:50], "log": gitops.session_log(where["path"], 5)}
        except (ValueError, OSError, gitops.GitError) as e:
            return {"error": str(e)}

    def _place_handoff(self, s: Session) -> None:
        """9.140: every start of a switched session puts its handoff document into the sandbox's drop dir (the
        box is rebuilt at a start); the first one also types the task pointing at it."""
        if not s.switched_from:
            return
        try:
            doc = common.safe_read(s.dir, agentswitch.FILE, 4 << 20)
        except OSError:
            return
        os.makedirs(os.path.join(s.box, s.names["drop"]), mode=0o700, exist_ok=True)
        common.safe_write(s.box, s.names["drop"] + "/" + agentswitch.FILE, doc, mode=0o600)
        if not s.task_sent and not s.task:
            wt = s.switched_from.get("worktree")
            where = {"where": "worktree", "path": wt, "exists": os.path.isdir(wt), "branch": s.switched_from.get("branch") or "",
                     "mounted": self._mounted(s, wt)} if isinstance(wt, str) else {"where": "checkout"}
            if isinstance(wt, str):
                where["masked"] = not where["mounted"] and guard._inside(wt, s.path)
            s.task = agentswitch.task(s.switched_from.get("agent", ""), "%s/%s/%s" % (self.box_in(s), s.names["drop"], agentswitch.FILE),
                                      where, s.switched_from.get("title") or "")

    def resume_conversation(self, sid: str, fork: bool = False, task: str | None = None, compact: bool = False) -> dict:
        s = self.sessions.get(sid)
        if s is None or s.kind != "project":
            raise KeyError(sid)
        src = self._transcript_src(s)
        native = src[1] if src else None
        if fork:
            digest.validate_conversation(s.claude_src, native, s.agent)
            source = s if native == s.claude_session else SimpleNamespace(
                id=s.id, agent=s.agent, project=s.project, path=s.path,
                claude_session=native, claude_src=s.claude_src, codex_dir=s.codex_home,
                import_from=s.import_from)
            child = self.create(s.project, origin="gui", agent=s.agent, opts=dict(s.opts), _fork_source=source, runtime=s.runtime)
            return {"session": child.id, "fork": True}
        if s.state in ACTIVE:
            if task is not None:   # W8: a new task for a running session is a follow-up, not a resume
                raise ValueError("The session is running - send the task to it instead.")
            return {"session": s.id, "running": True}
        if s.state not in ("blocked", "error", "exited"):
            raise ValueError("This session cannot be resumed yet.")
        if s.pid is not None and s.rc is None and self._pty_alive(s):
            raise ValueError("The previous sandbox is still shutting down. Try again in a moment.")
        digest.validate_conversation(s.claude_src, native, s.agent)
        # like retry: the limit, a double click and a Stop during the start all go through _reserve (it moves
        # the state and clears cancel under the same locks a Stop takes - never reset cancel after it)
        try:
            self._reserve(s, ("blocked", "error", "exited"), fresh=True)
        except ValueError:
            if s.state in ACTIVE and task is None:   # a double click: the first one starts it, this one opens it
                return {"session": s.id, "running": True}
            raise
        with s.lock:
            s.hidden = False
            s.resume = native
            s.fork_pending = bool(s.fork_pending and native == (s.fork_from or {}).get("conversation"))
            s.rc, s.attest, s.blocks, s.pid = None, None, [], None
            if not s.profile:
                s.profile = holehome.PROJECT_PROFILE
            if s.state == "preflight":   # a Stop right after _reserve keeps its own text
                s.detail = "Resuming the saved conversation"
            if task is not None:   # W8: typed once Claude's prompt is up (_start keeps task_sent False)
                s.task, s.task_sent, s.compact_wait = task, False, 0.0
                s.compact_first = bool(compact)
            s.save()
        self.bump()
        threading.Thread(target=self._start, args=(s,), daemon=True, name="resume-" + s.id).start()
        return {"session": s.id}

    def _copy_fork(self, source: Session, target: Session) -> None:
        if source.project != target.project or source.path != target.path or source.agent != target.agent:
            raise ValueError("A fork must keep the original project and agent.")
        rel = digest.validate_conversation(source.claude_src, source.claude_session, source.agent)
        real = os.path.join(common.REAL_HOME, "." + target.agent)
        shared = self.codex_dir() == "host" if target.agent == "codex" else self.claude_dir() == "host"
        if shared and os.path.realpath(source.claude_src) == os.path.realpath(real):
            # The user already chose the native shared profile. Both CLIs can
            # fork its history in place without altering the original or copying login data.
            os.mkdir(target.dir, mode=0o700)
            if target.agent == "codex":
                target.host_codex = True
            else:
                target.claude_mode = "host"
            target.resume = source.claude_session
            target.fork_from = {"session": source.id, "conversation": source.claude_session}
            target.import_from = source.import_from
            target.fork_pending = True
            return
        cfg = target.codex_dir if target.agent == "codex" else target.cfg
        before = common.safe_stat(source.claude_src, rel)
        os.mkdir(target.dir, mode=0o700)
        try:
            os.makedirs(os.path.join(cfg, os.path.dirname(rel)), mode=0o700, exist_ok=True)
            with open(os.path.join(cfg, rel), "xb") as dst:
                os.chmod(dst.name, 0o600)
                at = 0
                while at < before.st_size:
                    raw = common.safe_read(source.claude_src, rel, min(1 << 20, before.st_size - at), at)
                    if not raw:
                        raise ValueError("The conversation changed while forking. Try again.")
                    dst.write(raw)
                    at += len(raw)
            with open(os.path.join(cfg, rel), "r+b") as dst:
                dst.seek(max(0, before.st_size - (8 << 20)))
                tail = dst.read()
                if tail and not tail.endswith(b"\n"):
                    end = tail.rfind(b"\n")
                    if end < 0:
                        raise ValueError("The conversation has no complete final record.")
                    dst.truncate(before.st_size - len(tail) + end + 1)
            after = common.safe_stat(source.claude_src, rel)
            if after.st_ino != before.st_ino or after.st_size < before.st_size:
                raise ValueError("The conversation changed while forking. Try again.")
            if target.agent == "codex":
                try:
                    raw = common.safe_read(source.claude_src, "session_index.jsonl", 8 << 20)
                    lines = []
                    for line in raw.splitlines():
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(rec, dict) and rec.get("id") == source.claude_session:
                            lines.append(line)
                    if lines:
                        common.safe_write(cfg, "session_index.jsonl", lines[-1] + b"\n", mode=0o600)
                except OSError:
                    pass
            digest.validate_conversation(cfg, source.claude_session, target.agent)
        except Exception:
            shutil.rmtree(target.dir, ignore_errors=True)
            raise
        target.resume = source.claude_session
        target.fork_from = {"session": source.id, "conversation": source.claude_session}
        target.import_from = source.import_from
        target.fork_pending = True

    def board(self) -> list[dict]:
        """Optional GUI previews never wait on scanning every transcript and repository."""
        if not self._background_enabled:
            return self._board()   # synchronous managers have no service workers
        visible = {s.id for s in list(self.sessions.values()) if not s.hidden}
        with self._board_lock:
            rows = copy.deepcopy([row for sid, row in self._board_cache.items() if sid in visible])
            if not self._board_busy and time.monotonic() >= self._board_due:
                self._board_busy = True
                if self._start_service(self._refresh_board, "piper-board-preview") is None:
                    self._board_busy = False
            return rows

    def _refresh_board(self) -> None:
        try:
            rows = self._board()
            with self._board_lock:
                self._board_cache = {row["id"]: row for row in rows}
        except Exception as e:  # optional previews must not disrupt other manager services
            print("piperd: session previews: %s" % type(e).__name__, flush=True)
        finally:
            with self._board_lock:
                self._board_due = time.monotonic() + 5
                self._board_busy = False

    def _board(self) -> list[dict]:
        """One card per visible session: state, last line of the conversation, changed files."""
        out = []
        for s in sorted(list(self.sessions.values()), key=lambda x: x.created):
            if self._stop_event.is_set():
                break
            if s.hidden:
                continue
            last, asked, usage = "", "", None
            try:
                conv = self.conversation(s.id)
                turns, usage = conv["turns"], conv["usage"]
                # the last real answer (not a bare tool call) and the last thing the human typed
                a = [t for t in turns if t["role"] == "assistant" and t["text"].replace("[tool", "").strip()
                     and not re.fullmatch(r"(\s*\[tool [^\]]+\]\s*)+", t["text"])]
                last = (a[-1]["text"] if a else "")[-700:]
                # not what Claude Code injects as a "user" turn (<task-notification>, <command-name>, ...)
                u = [t for t in turns if t["role"] == "user" and not t["text"].startswith("[tool")
                     and not re.match(r"\s*<[a-z][\w-]*>", t["text"])]
                asked = feed.CHAT_RX.sub("", u[-1]["text"] if u else "", count=1)[:300]   # 9.14: not the chat tag
            except OSError:
                pass
            changed, flow = None, None
            if s.kind == "project":
                hit = self._git_count.get(s.id)
                if hit and time.time() - hit[0] < 20:
                    changed = hit[1]
                else:
                    top = self._session_top(s)
                    try:
                        changed = len(gitops.status(top, lambda p: False)["files"]) if top else None
                    except gitops.GitError:
                        changed = None
                    self._git_count[s.id] = (time.time(), changed)
                try:   # conversation() above cached the transcript's (path, mtime): no second scan
                    flow = self.flow(s, (self._conv_cache.get(s.id) or (None,))[0])
                except OSError:
                    flow = None
            try:
                istate = self.issue_state_card(s)   # 9.12 D
            except OSError:
                istate = None
            out.append({"id": s.id, "last": last, "asked": asked, "changed": changed, "issue": s.issue, "flow": flow,
                        "istate": istate,
                        "tokens": (usage["input"] + usage["output"] + usage["cache_read"] + usage["cache_write"])
                        if usage else None})
        return out

    def audit_log(self, sid: str | None, limit: int) -> list[dict]:
        return common.audit_tail(max(1, min(int(limit), 2000)), sid or None)

    def send(self, sid: str, text: str, *, idle_only: bool = False) -> bool:
        """Text typed by the human (composer, review notes) - no confirmation, but audited."""
        s = self.get(sid)
        if not text.strip() or len(text) > 20000:
            raise ValueError("text must be 1..20000 characters")
        if s.state not in ("ready", "working", "waiting", "attention"):
            raise ValueError("session %s is %s" % (s.id, s.state))
        if idle_only and ((s.runtime == "direct" and self._dialog_up(s))
                          or (hasattr(self, "quiet") and self.quiet.blocked(s.id))):
            return False   # a background delivery (the revert note) waits for the dialog / the Quiet window
        if s.runtime == "direct" and self._dialog_up(s):
            raise ValueError("agent shows a login, trust or hook review dialog; answer it in the terminal first")
        with s.turn_action_lock:
            if idle_only and (self._turn_running(s) or not self._istate_at_prompt(s) or not self.auto_type_ok(s)):
                return False
            note = s.revert_note
            if note and self._turn_running(s):
                raise ValueError("Stop the turn first (Esc); a file-revert note is waiting")
            payload = (note + "\n\n" + text) if note and note != text else text
            if len(payload) > 20000:
                raise ValueError("text with the pending file-revert note exceeds 20000 characters")
            s.gui_sent_at = time.time()   # its UserPromptSubmit is the human's (the tracker does not see piperd's paste)
            sent = self.type_text(s, payload, idle_only=True) if idle_only else self.type_text(s, payload)
            if sent:
                s.revert_note = ""
                self.review_done(sid)
            audit({"event": "gui-send", "session": s.id, "chars": len(payload)})
            return sent

    DOMAIN_RX = re.compile(r"^(?=.{1,253}$)(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")

    def _hidden_push_msg(self, top: str, gone: list[str]) -> str:
        return ("the commits to push delete hidden files (%s); they only look deleted in this sandbox - rewrite "
                "the commits without those deletions (e.g. git reset --soft, then add your own files by name)%s"
                % (", ".join(gone[:5]), self._deletions_why(top)))

    @staticmethod
    def _library_access(s: Session, root: str, write: bool = False) -> bool:
        real = os.path.realpath(root)
        return any(c.get("kind") == "library" and (not write or c.get("readwrite") is True) and
                   isinstance(c.get("root"), str) and guard._inside(real, os.path.realpath(c["root"])) for c in s.coverage)

    def _worktree_path_allowed(self, top: str, path: str, hidden) -> bool:
        if not (guard._inside(path, top) or PROJECT_PATH and guard._inside(path, os.path.realpath(PROJECT_PATH))) or any(
                guard._inside(path, r) or guard._inside(r, path) for r in workbench.INFRA_ROOTS):
            return False
        rel = os.path.relpath(path, top)
        if guard.is_git_path(rel) or PROJECT_PATH and guard.is_git_path(os.path.relpath(path, PROJECT_PATH)):
            return False
        # The conventional worktree pool is inside .claude; only one checkout name under it
        # is allowed, never .claude itself or another host-run directory.
        parts = rel.split(os.sep)
        if len(parts) == 3 and parts[:2] == [".claude", "worktrees"]:
            parts = parts[2:]
        if workbench.AUTO_RUN & set(parts):
            return False
        if rel == ".." or rel.startswith("../"):
            # outside the checkout only a new sibling of it (<repo>-work): never into another project's tree
            if os.path.dirname(path) != os.path.dirname(top):
                return False
            # hidden_for deliberately rejects ../; a verified sibling maps to the same repository's paths.
            return not hidden(os.path.basename(path)) and not guard.hidden_by(
                self.policy["candidates"], os.path.relpath(path, os.path.realpath(PROJECT_PATH)))
        return not hidden(rel)

    def _git_run_location(self, top: str, argv: list[str], hidden):
        """Resolve a linked worktree in PROJECT_PATH only when it shares the pinned common dir."""
        cwd, explicit_dir, explicit_tree = top, None, None
        i = 0
        while i < len(argv) and argv[i].startswith("-"):
            option = argv[i]
            if option == "--no-pager":
                i += 1; continue
            name, eq, value = option.partition("=")
            if name not in ("-C", "--git-dir", "--work-tree"):
                return top, argv
            if not eq:
                if i + 1 == len(argv):
                    return top, argv
                value = argv[i + 1]; i += 1
            if name == "-C":
                cwd = os.path.realpath(os.path.join(cwd, value))
            elif name == "--git-dir":
                explicit_dir = os.path.realpath(os.path.join(cwd, value))
            else:
                explicit_tree = os.path.realpath(os.path.join(cwd, value))
            i += 1
        if not i or i == len(argv):
            return top, argv
        candidate = explicit_tree or cwd
        if not self._worktree_path_allowed(top, candidate, hidden):
            return top, argv
        with self._roots_lock:
            recorded = next((dict(r) for r in (self._roots or {}).values() if isinstance(r, dict) and r.get("top") == top), {})
        common_dir = recorded.get("common")
        if not common_dir:
            return top, argv
        if candidate == top:
            gitdir = recorded.get("gitdir")
        else:
            try:
                pointer = common.safe_read(candidate, ".git", 4096).decode().strip()
                if not pointer.startswith("gitdir: "):
                    return top, argv
                gitdir = os.path.realpath(os.path.join(candidate, pointer[8:]))
                if os.path.dirname(gitdir) != os.path.join(common_dir, "worktrees"):
                    return top, argv
                common_ref = common.safe_read(gitdir, "commondir", 4096).decode().strip()
                backlink = common.safe_read(gitdir, "gitdir", 4096).decode().strip()
                if os.path.realpath(os.path.join(gitdir, common_ref)) != common_dir or \
                        os.path.realpath(backlink) != os.path.join(candidate, ".git"):
                    return top, argv
            except (OSError, UnicodeError):
                return top, argv
        if explicit_dir and explicit_dir != gitdir:
            return top, argv
        rec = dict(recorded, top=candidate, gitdir=gitdir)
        if not self._git_guard(rec) or self._git_ids(candidate) != (candidate, gitdir, common_dir):
            return top, argv
        with self._roots_lock:
            self._roots[candidate] = rec
            write_json(REPO_ROOTS_FILE, self._roots)
        return candidate, argv[i:]

    def _worktree_remove_blockers(self, top: str, argv: list[str], hidden) -> list[str]:
        rest = argv[2:]
        if len(rest) == 2 and rest[0] == "--":
            rest = rest[1:]
        if len(rest) != 1 or rest[0].startswith("-"):
            return ["worktree removal must name one local checkout without force"]
        path = os.path.abspath(os.path.join(top, rest[0]))
        if path == top or os.path.realpath(path) != path or not self._worktree_path_allowed(top, path, hidden):
            return ["worktree removal targets the repository root, a link or a hidden path"]
        # Reuse the pinned common-dir and backlink checks; an arbitrary directory is never cleanup.
        resolved, args = self._git_run_location(top, ["-C", rest[0], "status"], hidden)
        if resolved != path or args != ["status"]:
            return ["worktree removal targets an unverified checkout"]
        if any(x.path and x.state in ACTIVE and guard._inside(os.path.realpath(x.path), path)
               for x in list(self.sessions.values())):
            return ["worktree removal would delete an active session's checkout"]
        return []

    def _git_worktree_command(self, s: Session, top: str, argv: list[str], hidden) -> tuple[tuple[str, ...], list[str]]:
        if len(argv) < 2 or argv[1] not in ("add", "lock", "unlock", "move", "remove", "prune"):
            return (), ["worktree command may change other checkouts; use a scoped operation"]
        op, pos, flags = argv[1], [], []
        opts = {"add": {"-q", "--quiet", "-d", "--detach", "--checkout", "--no-checkout", "--lock", "--orphan"},
                "lock": set(), "unlock": set(), "move": set(), "remove": set(),
                "prune": {"-n", "--dry-run", "-v", "--verbose"}}[op]
        i, after = 2, False
        while i < len(argv):
            a = argv[i]
            if not after and a == "--":
                after = True
            elif not after and (op == "add" and a == "-b" or op in ("add", "lock") and a == "--reason" or
                                op == "prune" and a == "--expire"):
                if i + 1 == len(argv) or argv[i + 1].startswith("-"):
                    return (), ["missing worktree option value"]
                i += 1
            elif not after and a.startswith("--reason=") and op in ("add", "lock") or \
                    not after and a.startswith("--expire=") and op == "prune":
                pass
            elif not after and a.startswith("-"):
                if a not in opts:
                    return (), ["forced or unchecked worktree option " + a[:60]]
                flags.append(a)
            else:
                pos.append(a)
            i += 1
        if op == "prune":
            if pos or not set(flags) & {"-n", "--dry-run"}:
                return (), ["worktree prune may delete other checkouts' metadata; only dry-run is automatic"]
            return tuple(argv), []
        if (op == "add" and len(pos) not in (1, 2)) or (op == "move" and len(pos) != 2) or \
                (op in ("lock", "unlock", "remove") and len(pos) != 1):
            return (), ["worktree command must name its checkout and destination explicitly"]
        if op in ("add", "move"):
            dest = os.path.abspath(os.path.join(top, pos[0] if op == "add" else pos[1]))
            if not self._worktree_path_allowed(top, dest, hidden) or os.path.realpath(dest) != dest or os.path.lexists(dest):
                return (), ["worktree destination must be new, visible and inside the project workspace"]
        if op == "remove":
            why = self._worktree_remove_blockers(top, argv, hidden)
            return (() if why else tuple(argv)), why
        if op != "add":
            path = os.path.abspath(os.path.join(top, pos[0]))
            resolved, args = self._git_run_location(top, ["-C", pos[0], "status"], hidden)
            if path != os.path.realpath(path) or resolved != path or args != ["status"]:
                return (), ["worktree operation targets an unverified checkout"]
            if any(x.path and x.state in ACTIVE and (op == "move" or x.id != s.id) and
                   guard._inside(os.path.realpath(x.path), path) for x in list(self.sessions.values())):
                return (), ["worktree operation would affect an active session's checkout"]
        elif len(pos) == 2 and gitops._auto_value_bad(top, pos[1]):
            return (), ["worktree start point is not a local reference"]
        return tuple(argv), []

    def _git_apply_input(self, original_top: str, top: str, argv: list[str], hidden):
        opts, files, after = [], [], False
        flags = {"--check", "--index", "--cached", "--3way", "--reverse", "-R", "--reject", "--allow-empty",
                 "--recount", "--inaccurate-eof", "--ignore-space-change", "--ignore-whitespace", "--verbose", "-v",
                 "--quiet", "-q", "--stat", "--numstat", "--summary", "--apply"}
        for a in argv[1:]:
            if not after and a == "--":
                after = True
            elif not after and a.startswith("-"):
                if a not in flags and not (re.fullmatch(r"-p\d+", a) or a.startswith(("--whitespace=", "--directory=", "--include=", "--exclude="))):
                    return argv, None, ["unchecked patch option " + a[:60]]
                if a.startswith("--directory=") and gitops._auto_value_bad(top, a.partition("=")[2]):
                    return argv, None, ["patch directory leaves the checkout"]
                opts.append(a)
            else:
                files.append(a)
        if len(files) != 1 or files[0] == "-":
            return argv, None, ["patch input must be one file in this repository or its verified worktree"]
        path = os.path.abspath(os.path.join(top, files[0]))
        root = next((r for r in (top, original_top) if guard._inside(path, r)), None)
        if root is None:
            return argv, None, ["patch input leaves the authorized checkouts"]
        rel = os.path.relpath(path, root)
        if guard.is_git_path(rel) or hidden(rel):
            raise ValueError("patch input is hidden or inside .git")
        # Read once without following links. Git consumes these exact bytes, even if the file is replaced.
        patch = common.safe_read(root, rel, projectfs.MAX_READ + 1)
        if len(patch) > projectfs.MAX_READ:
            raise ValueError("patch input exceeds MAX_READ")
        inspect = [a for a in opts if a not in {"--apply", "--check", "--stat", "--summary", "--numstat", "--index", "--cached", "--3way", "--reject"}]
        paths = [r.split("\t", 2)[2] for r in gitops._git(top, "apply", "--numstat", "-z", *inspect,
                                                        session=True, input=patch).split("\0") if r]
        directory = next((a.partition("=")[2] for a in opts if a.startswith("--directory=")), "")
        # numstat exposes only the destination of renames/copies; their source must be authorized too.
        for line in patch.decode("utf-8", "replace").splitlines():
            if line.startswith(("rename from ", "copy from ")):
                name = line.split(" ", 2)[2]
                if name.startswith('"'):
                    quoted = gitops._unquote(name)
                    if not quoted or quoted[1]:
                        raise ValueError("unparseable patch source")
                    name = quoted[0]
                paths.append(os.path.join(directory, name))
        if any(hidden(p) or guard.is_git_path(p) or gitops._auto_value_bad(top, p) for p in paths):
            raise ValueError("patch targets a hidden path, .git or a path outside the checkout")
        return ["apply", *opts, "--", "-"], patch, []

    def _git_run(self, s: Session, top: str, hidden, raw, card: bool = False, project: str = "",
                 read_only: bool = False) -> dict:
        """Unlocked full git: the exact argv on a card, run only when the human approves it."""
        argv = gitops.check_run_args(raw)
        # R1+: another project's own settings may hide its .git (project_hidden); unreadable = hidden
        other_git_hidden = isinstance(hidden, Unreadable) or getattr(hidden, "git_hidden", False) is True
        unmounted = getattr(hidden, "unmounted", False) is True
        original_top = top
        top, argv = self._git_run_location(top, argv, hidden)
        if top != original_top:
            prior_hidden = hidden
            hidden = lambda p: prior_hidden(p) or guard.hidden_by(self.policy["candidates"], p) or (  # noqa: E731
                (not project or project == s.project) and guard.hidden_by(s.excludes or [], p))
        if argv[0] == "archive":
            normalized = []
            i = 0
            while i < len(argv):
                if argv[i] in ("-o", "--output", "--format") and i + 1 < len(argv):
                    name = "--output" if argv[i] == "-o" else argv[i]
                    normalized.append(name + "=" + argv[i + 1]); i += 2
                else:
                    normalized.append(argv[i]); i += 1
            argv = normalized
        gitops.refuse_global_config(argv)   # a -c in the argv would beat the fail-closed ones
        refused = gitops.grep_refusal(top, argv, hidden)   # N7 (B-N6-4): no card for a grep that then fails anyway
        if refused:
            raise ValueError(refused)
        gitops.refuse_exec_config(top)   # before status / push_targets: they run host git too
        self._refuse_protected_push(top, argv)
        if self.policy.get("git_fail_closed") and self.policy.get("security_scoped"):
            gitops.refuse_program_options(argv)
        display_argv = argv
        patch, apply_why = None, []
        if argv[0] == "apply" and self._git_hole():
            argv, patch, apply_why = self._git_apply_input(original_top, top, argv, hidden)
        if gitops._sub(argv)[0] == "push":   # the same check as git_push (HEAD's commits), before the card
            gone = self.hidden_deletions(s, top, hidden) if top != original_top or project and project != s.project else self.hidden_deletions(s, top)
            if gone:
                raise ValueError(self._hidden_push_msg(top, gone))
        try:
            st = gitops.status(top)
            where = "branch %s%s" % (st["branch"] or "(detached)", " → %s" % st["upstream"] if st.get("upstream") else "")
        except gitops.GitError:
            where = "branch ?"
        busy = gitops.repo_state(top)
        out_ok = None
        if self.policy.get("git_run_output_in_repo"):
            # what host git writes there is what the sandbox could write itself - never a hidden file, .git,
            # a file the host runs on its own or in a build (workbench.AUTO_RUN / BUILD_FILES)
            out_ok = lambda v: (not hidden(os.path.normpath(v)) and not (workbench.AUTO_RUN & set(os.path.normpath(v).split(os.sep)))  # noqa: E731
                                and not workbench.BUILD_FILES.search(os.path.normpath(v).replace(os.sep, "/")))
        # R1 (2026-10-09): the card-free object reads are "git in the sandbox" only while the sandbox reads .git
        # itself (K1); with .git hidden again they would hand out committed content of hidden files (an object
        # id names no path, strict_filter withholds nothing) - then they ask, like any other host read
        git_hidden = self._git_hidden_policy() or guard.git_excluded(s.excludes or []) or other_git_hidden
        extra = gitops.READONLY_SUBS if self.policy.get("git_run_readonly_extra") and not git_hidden else frozenset()
        worktree_args, worktree_why = self._git_worktree_command(s, top, argv, hidden) if \
            argv[0] == "worktree" and argv[1:2] != ["list"] and self._git_hole() else ((), [])
        warns = gitops.run_warnings(argv, hidden, out_ok, top)
        if worktree_args:
            warns = [w for w in warns if w != gitops._W_OUTSIDE]
        warns += gitops.config_warnings(top)
        why = (gitops.auto_blockers(top, argv, out_ok, extra, worktree_args) + worktree_why + apply_why) if self._git_hole() else []
        stages, first = False, []   # first: the ⚠ lines above every other warning (a folded card shows them)
        # N8: a pull pull_plan models runs as fetch + the checked merge / rebase of what it fetched (_git_pull) -
        # that step is judged after the fetch; any other pull asks first
        pull_plan = gitops.pull_plan(top, argv) if self._git_hole() and argv[:1] == ["pull"] else None
        if self._git_hole() and gitops._sub(argv)[0] == "pull" and pull_plan is None:
            why.append("what this pull merges is not checked before it fetches (a form Piper does not model)")
        keep: set[str] = set()
        if self._git_hole() and pull_plan is None:
            first, stages = self._hidden_reasons(top, argv, hidden, why, keep)
            warns = first + warns
        if (git_hidden or unmounted) and self._git_hole() and not why:
            # N6: the sandbox cannot read this .git (hidden, or a checkout it does not mount - K1 is not about it):
            # card-free only what the allowlist knows prints names, ids, messages or diff blocks strict_filter judges
            # (N3's object reads, N4's colour / graph / --relative, N5's tree revisions are all outside it)
            why += ["%s (%s)" % (w, "git is hidden from this session" if git_hidden else
                                 "this project is not in the session's sandbox")
                    for w in gitops.exposed_blockers(top, argv)[:3]]
        # N10: hidden content may be under another name now. Opt-in git_hidden_server_content (2026-10-12): a file
        # replaced at its path in the pinned parent (host git's unlink + create) keeps its hidden name - the by-name
        # checks above still card what touches it and L5 asks for the restart, so it no longer cards every later run
        moved = getattr(hidden, "moved", None) or []
        replaced = [] if self.policy.get("git_hidden_server_content") else self._broken_overmounts(s)[1]
        if self._git_hole() and (moved or replaced):
            broken = ("an over-mounted path of the sandbox was moved (%s) - Piper cannot tell where its hidden files "
                      "are now, every file counts as hidden" % ", ".join(moved[:3]) if moved else
                      "an over-mounted file is not the one pinned at the sandbox's start (%s) - replaced on the host, or "
                      "a stand-in after its directory was moved: hidden content may be under another name"
                      % ", ".join(replaced[:3]))
            warns = [broken] + warns
            why.append("an over-mount of the sandbox is not the one pinned at its start")
            stages = True
        summary = "\n".join(["git %s" % shlex.join(display_argv), *["⚠ " + w for w in warns], "",   # warnings first: a folded card shows them
                             "in %s (%s%s)" % (top, where, ", %s in progress" % busy if busy else ""),
                             "Hooks do not run; the agent gets the output.",
                             *(["Not run without a card: " + "; ".join(why[:3])] if why else [])])
        ask_kind = self._git_ask_kind(card, argv, warns, read_only)
        targeted = bool(self.policy.get("security_scoped"))
        if targeted and gitops._sub(argv)[0] == "push":
            ask_kind = "git_push"
        must_card = targeted and (bool(why) or gitops._W_CODE in warns or gitops._W_OUTSIDE in warns)
        must_card = must_card or targeted and not self.auto_ok(ask_kind)
        card_free = False
        if not must_card and self._git_hole() and not card and not why and (not isinstance(self.policy.get("ask"), list) or self.auto_ok("git_run")) and not gitops.risky_warnings(warns) and gitops._W_DISCARD not in warns and "--amend" not in argv:   # git_mode "hole": runs right
            card_free = True
            # away, like git in the sandbox itself - but only an allowlisted argv in a repository whose config
            # runs nothing; host code, reads outside the repository / of hidden files and a rewrite of the
            # server's history still go on a card (git_run runs on the HOST, not in the sandbox)
            audit({"event": "confirm", "kind": "git_run", "session": s.id, "approved": True, "auto": True,
                   "why": "git_mode hole", "project": project or s.project})
            if self.policy.get("feed_auto_actions"):
                try:
                    self.feed_auto(s, "git_run", summary)
                except Exception as e:  # noqa: BLE001 - the feed is a view
                    print("piperd: feed_auto: %s" % e, flush=True)
        elif not self.ask(ask_kind, s, summary,
                          {"args": argv, "warnings": warns, "_force_approval": must_card,
                           **({"danger": True} if stages else {})}, timeout=UNLOCKED_ASK_S):
            raise ValueError("the user declined: git %s" % shlex.join(argv))
        if pull_plan is not None:
            r = self._git_pull(s, top, argv, hidden, pull_plan, card, card_free)
        elif card_free and gitops._sub(argv)[0] in gitops.WT_WRITERS:   # N8: no hidden file changes under it
            r, note = self._keeping_hidden(s, top, argv, hidden, lambda: gitops.session_run(top, argv, hidden, input=patch)
                                           if patch is not None else gitops.session_run(top, argv, hidden), keep=keep)
            r = dict(r, output=gitops._bounded(r["output"] + note)) if note else r
        else:
            r = gitops.session_run(top, argv, hidden, input=patch) if patch is not None else gitops.session_run(top, argv, hidden)
        audit({"event": "git-run", "session": s.id, "project": project or s.project, "args": display_argv[:20], "rc": r["rc"]})
        if gitops._sub(argv)[0] == "push" and r["rc"] == 0 and (not project or project == s.project):
            self.review_done(s.id)
        self.bump()
        return r

    def _git_ask_kind(self, card: bool, argv: list[str], warns: list[str], read_only: bool = False) -> str:
        """The card kind of a git_run. In another project (card) under policy write_guard, an argv that runs code or
        sets config (`config core.fsmonitor`, -c, --exec: the user's next host git runs it) or rewrites the work tree
        (its CI / build files) is a write_exec card, as edit_file of such a file is - git_run_project alone was
        approved on its own once the tree left it off (merge review R1, 2026-10-17). An allowlist: only a read
        (tool()'s read_only) or a plain add / commit / push / fetch / branch / tag stays git_run_project - a list of
        writers missed `init --template`, `clean`, `remote set-url`, `read-tree -u` (Fable review of the fix); a read
        naming a host file (`diff --no-index /etc/hosts`) or a forced / ref-planting form (`branch -f`, `fetch o
        main:main`) is not plain (second review)."""
        if not card:
            return "git_run"
        if not self.policy.get("write_guard") or gitops._W_CODE not in warns and gitops._W_OUTSIDE not in warns and (
                read_only or _git_plain(argv)):
            return "git_run_project"
        return "write_exec"

    def _hidden_reasons(self, top: str, argv: list[str], hidden, why: list[str],
                        keep: set | None = None) -> tuple[list[str], bool]:
        """(the ⚠ lines first on the card, red) for a run that takes a hidden file's real content into git, moves /
        deletes it or rewrites it with content from git; each appends its reason to `why` (a card). A branch switch /
        merge / pull whose hidden content is on the GitLab server asks nothing (2026-10-12, user's decision): its blobs
        go into `keep`, the N8 net leaves exactly those in place."""
        stages, first = False, []
        # N5: the real content of a hidden file into .git (the sandbox reads it) - N7: also
        took = gitops.stages_hidden(top, argv, hidden)   # when another reason already asks (the card is red)
        if took == ["?"]:   # N7 (B-N6-2): the dry run failed (a path that does not exist...) - say so
            if not why:
                why.append("could not check which files it takes into git (its dry run failed or printed a "
                           "name it could not read)")
        elif took:
            # N7 (B-N6-1): a red card (payload danger - no `y`, no auto_approve) with the reason first as ⚠
            stages = True
            first.append("takes the REAL content of a hidden file into git (%s) - the sandbox reads .git, "
                         "a push ships it" % ", ".join(sorted(took)[:3]))
            why.append("it takes a hidden file from the work tree into git")
        # N7: a hidden work-tree file moved out of hiding / deleted (mv, rm -f of its directory) or rewritten
        # with a commit's content (merge, checkout, cherry-pick... of a commit the sandbox wrote with .git open)
        moved = gitops.moves_hidden(top, argv, hidden)
        if moved:
            stages = stages or (gitops._sub(argv)[0] == "mv" and moved != ["?"])   # out of hiding: red
            first.append("moves or deletes a hidden file in the work tree (%s)%s" % (
                ", ".join(moved[:3]), " - the sandbox then reads it under the new name" if gitops._sub(argv)[0] == "mv" else ""))
            why.append("it moves or deletes a hidden file in the work tree")
        rewritten = gitops.rewrites_hidden(top, argv, hidden)
        blobs = gitops.rewrite_blobs(top, argv, rewritten) if rewritten and rewritten != ["?"] else None
        if blobs and self.policy.get("git_hidden_server_content") and self._on_server(top, blobs):
            rewritten = []
            if keep is not None:
                keep |= blobs
        if rewritten:
            first.append("rewrites a hidden file in the work tree with content from git (%s)" % ", ".join(rewritten[:3])
                         if rewritten != ["?"] else "could not check which hidden files it rewrites in the work tree")
            why.append("it rewrites a hidden file in the work tree")
        return first, stages

    def _on_server(self, top: str, blobs: set[str]) -> bool:
        """Every blob exists in the checkout's GitLab project - the origin recorded by the host (origin_gl), never the
        live .git/config the sandbox writes; content the sandbox committed itself is not there until a push."""
        pid = self.origin_gl(top)
        if not pid or len(blobs) > 20:
            return False
        gl = gitlab.GitLab(self.gitlab_host(), gitlab.token())
        for b in sorted(blobs):
            if (pid, b) in self._server_blobs:
                continue
            try:
                gl._open("GET", "%s/projects/%s/repository/blobs/%s/raw" % (gl.base, gl._pid(pid), b), limit=1)
            except gitlab.GitLabError:
                return False
            self._server_blobs.add((pid, b))
        return True

    def _keeping_hidden(self, s: Session, top: str, argv: list[str], hidden, run, card: bool = False,
                        keep=frozenset()) -> tuple[dict, str]:
        """N8: a card-free work-tree writer must leave every hidden file as it was (its checks said it would) - a ref
        the sandbox moved between the check and the run (K1) must not leave the agent's content there. Snapshot
        before, put back in place after; (run's result, a note for the agent - no names; the audit names the files).
        N9: a net that cannot be built (too many hidden files) asks first instead of running without it; the files
        go back only through the pinned over-mounted directory (never where the sandbox reads them); what the net
        displaced or could not put back goes to Piper's trash (the GUI restores it) - nothing hidden is lost."""
        try:
            snap = gitops.hidden_snapshot(top, hidden)
        except gitops.GitError:
            snap = None
        if snap is None:
            summary = "\n".join(["git %s" % shlex.join(argv), "⚠ Piper cannot keep the hidden files as they are during "
                                  "this run (more than %d files or %d MiB, or their list failed) - it runs without "
                                  "that net" % (gitops._SNAP_MAX[0], gitops._SNAP_MAX[1] >> 20), "",
                                  "in %s" % top, "Hooks do not run; the agent gets the output."])
            if not self.ask(self._git_ask_kind(card, argv, []), s, summary,
                            {"args": argv, "warnings": ["Piper cannot keep the hidden files as they are during this run"]},
                            timeout=UNLOCKED_ASK_S):
                raise ValueError("the user declined: git %s (Piper cannot keep the hidden files as they are during "
                                 "this run)" % shlex.join(argv))
        r, note = run(), ""
        if snap is not None:
            error = ""
            try:
                changed, failed, kept = gitops.hidden_restore(top, snap, hidden, self._overmount_shield(s, top), keep)
            except (gitops.GitError, OSError) as e:
                changed, failed, kept, error = ["?"], [str(e)[:200]], {}, str(e)[:200]
            entries = self._net_trash(s, top, kept)
            trash = [e["id"] for e in entries]
            if failed:   # N10 (N9 decision 1): the human sees it in the queue, not only in audit.log
                try:   # 2026-10-10: the originals of the failed files, restorable from the row
                    self.feed_net_failed(s, argv, failed, trash, error,
                                         restore=[{"id": e["id"], "path": e["path"]} for e in entries
                                                  if e["what"] == "was" and e["rel"] in failed])
                except Exception as fe:  # noqa: BLE001 - the feed is a view
                    print("piperd: feed_net_failed: %s" % fe, flush=True)
            if changed:
                audit({"event": "git-run-hidden-restored", "session": s.id, "args": argv[:20], "paths": changed[:20],
                       "failed": failed[:20], "trash": trash[:20]})
                print("piperd: git_run %s changed hidden files %s - put back (failed: %s, trash: %s)" % (
                    shlex.join(argv)[:200], changed[:5], failed[:5], trash[:5]), flush=True)
                note = "\n[piper: this run changed a file hidden from you in the work tree - %s]\n" % (
                    "put back as it was" if not failed else "it could not be put back; the user sees it in Piper's queue")
        return r, note

    def _overmount_shield(self, s: Session, top: str):
        """rel (below top) -> (over-mount below top, pinned identity) of the intact over-mounted DIRECTORY of s's
        sandbox it lies in, or None - only there may host content appear without the sandbox seeing it."""
        if s.state in self.PRE_SANDBOX_STATES:
            return lambda rel: None
        dirs = []
        for c in s.coverage or []:
            if c.get("kind") == "include" or not c.get("root") or not isinstance(c.get("ids"), dict):
                continue
            croot = os.path.realpath(c["root"])
            for o in guard.intact_overmounts(c) or set():
                full = os.path.join(croot, o)
                if os.path.isdir(full) and not os.path.islink(full):
                    dirs.append((os.path.relpath(full, os.path.realpath(top)), c["ids"][o]))

        def shield(rel: str):
            for over, ident in dirs:
                if not over.startswith("..") and rel.startswith(over + "/"):
                    return over, ident
            return None
        return shield

    def _net_trash(self, s: Session, top: str, kept: dict) -> list[dict]:
        """Content the net displaced or could not put back -> Piper's trash (host only, 0700; the GUI restores an
        entry where that is still allowed). Returns [{id, path (meta.json's), rel (below top), what: was|now}]."""
        ids = []
        root = os.path.realpath(s.path)
        for key, data in sorted(kept.items()):
            what, _, rel = key.partition("/")
            path = os.path.relpath(os.path.join(top, rel), root)
            tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
            tdir = os.path.join(workbench.TRASH_DIR, tid)
            try:
                os.makedirs(workbench.TRASH_DIR, mode=0o700, exist_ok=True)
                os.mkdir(tdir, 0o700)
                common.safe_write(tdir, "item", data, mode=0o600, exact_mode=True, create=True)
                common.write_json(os.path.join(tdir, "meta.json"),
                                  {"project": s.project, "path": rel if path.startswith("..") else path, "at": time.time(),
                                   "root": os.path.realpath(top), "rel": rel,   # the tree the net ran on (a sibling worktree)
                                   "kind": "file", "from": "git_run %s: %s" % (
                                       "the original the net could not put back" if what == "was"
                                       else "what the run left there", s.id)})
                ids.append({"id": tid, "path": rel if path.startswith("..") else path, "rel": rel, "what": what})
            except OSError as e:
                print("piperd: git_run net: trash %s: %s" % (rel, e), flush=True)
        return ids

    def _git_pull(self, s: Session, top: str, argv: list[str], hidden, plan: dict, card: bool, card_free: bool = False) -> dict:
        """N8 (F-N7-2 through the server): `git pull` as pull runs it - fetch, then the merge / rebase of the fetched
        commit by its oid (pull_step) - with that step checked like a merge the agent named: a hidden file it would
        rewrite (or take into git) asks on a card first. The agent gets pull's output and exit code."""
        env = {"GIT_REFLOG_ACTION": plan["reflog"]}
        try:
            early = gitops.pull_before_fetch(top, plan)
        except gitops.GitError as e:
            early = "fatal: %s\n" % e
        if early:
            return {"rc": 128, "output": early, "state": gitops.repo_state(top)}
        f = gitops.session_run(top, plan["fetch"], hidden, env=env)
        if f["rc"]:   # pull: a failed fetch is exit code 1
            return {"rc": 1, "output": f["output"], "state": f["state"]}
        try:
            step = gitops.pull_step(top, plan)
        except gitops.GitError as e:
            step = {"rc": 128, "output": "fatal: %s\n" % e}
        if "argv" not in step:   # pull stops here: its message on stderr after the fetch's
            return {"rc": step["rc"], "output": gitops._bounded(f["output"] + step["output"]), "state": gitops.repo_state(top)}
        why: list[str] = []
        keep: set[str] = set()
        first, stages = self._hidden_reasons(top, step["argv"], hidden, why, keep)
        if why:
            shown = [a if len(a) < 200 else a[:60] + "…" for a in step["argv"]]
            summary = "\n".join(["git %s" % shlex.join(argv), *["⚠ " + w for w in first], "",
                                  "in %s - fetched; pull now runs: git %s" % (top, shlex.join(shown)),
                                  "Hooks do not run; the agent gets the output.",
                                  "Not run without a card: " + "; ".join(why[:3])])
            if not self.ask(self._git_ask_kind(card, argv, []), s, summary,
                            {"args": argv, "step": step["argv"], "warnings": first, **({"danger": True} if stages else {})},
                            timeout=UNLOCKED_ASK_S):
                raise ValueError("the user declined: git %s (fetched, nothing merged)" % shlex.join(argv))
        def run():
            return gitops.session_run(top, step["argv"], hidden, env=env)
        r, note = self._keeping_hidden(s, top, argv, hidden, run, card, keep) if card_free and not why else (run(), "")
        # as a pull's own output reads through git_run: everything on stdout, then everything on stderr
        return {"rc": r["rc"], "state": r["state"],
                "output": gitops._bounded(f["streams"][0] + r["streams"][0] + f["streams"][1] + r["streams"][1] + note)}

    def _refuse_protected_push(self, top: str, argv: list[str]) -> None:
        """policy protected_branches: a session never pushes into them (like the user's host hook, but on the
        resolved target - upstream, refspec, HEAD - not on the command text). No card can allow it."""
        hit = gitops.protected_hit(gitops.push_targets(top, argv), self.policy.get("protected_branches") or [])
        if hit:
            raise ValueError("push into protected branch %r is refused (policy protected_branches) - push a "
                             "feature branch and open a merge request" % ("every branch" if hit == "*" else hit))

    def _refuse_protected_mr(self, tool: str, p: dict) -> None:
        tgt = p.get("target_branch") or ""
        if tool == "gitlab_mr_merge" and tgt in (self.policy.get("protected_branches") or []):
            raise ValueError("merging into protected branch %r is refused (policy protected_branches)" % tgt)
        if tool in ("gitlab_mr_create", "gitlab_mr_update", "github_pr_create", "github_pr_update") and tgt in (self.policy.get("mr_target_protected") or []):
            raise ValueError("a merge request targeting %r is refused (policy mr_target_protected) - target the "
                             "integration branch instead" % tgt)

    def set_unlock(self, sid: str, what: str, on: bool) -> dict:
        """Unlock (or lock again) `what` of UNLOCKS for one project session - the GUI warned
        the human first. Takes effect on the sandbox's next tool call; kept across restarts."""
        s = self.get(sid)
        if s.kind != "project":
            raise ValueError("only a project session can be unlocked")
        if what not in UNLOCKS:
            raise ValueError("unknown unlock %r (one of %s)" % (what, ", ".join(UNLOCKS)))
        with s.lock:
            if on:   # writing implies reading (edit_file needs read_file first)
                s.unlock.update({what: True, **({"projects": True} if what == "projects_write" else {})})
            else:   # locking reading locks writing too
                for k in [what] + (["projects_write"] if what == "projects" else []):
                    s.unlock.pop(k, None)
            s.save()
        audit({"event": "unlock", "session": s.id, "what": what, "on": bool(on)})
        self.bump()
        return {"unlock": dict(s.unlock)}

    def gitlab_read_always(self) -> list[str]:
        """GitLab projects every session reads without a card (Settings -> GitLab, or "Allow always")."""
        return [] if self.policy.get("security_strict") else [x for x in self.policy.get("gitlab_read_always") or [] if isinstance(x, str) and GL_PATH_RX.match(x)]

    def _gitlab_extra_scope(self, s: Session) -> list[str]:
        """Global auto-approval is not a remembered human grant when the card is enabled later.

        Preserve explicit project/session approvals with known provenance. Legacy project scopes
        are reconfirmed once with cards on; automatic scopes keep provenance across a restart.
        """
        if self.policy.get("security_strict"):
            return []
        auto = set(s.gitlab_auto_extra)
        approved = self.auto_ok("gitlab_read") or self.approval_scope("gitlab_read", {}) in s.approvals
        return [gp for gp in s.gitlab_extra if gp not in auto or approved]

    def _gitlab_read_extra(self, s: Session, tool: str, gp: str, args: dict) -> str:
        """A read tool named a GitLab project outside the session's scope (an issue of another
        client's project, say). The user allows it on a card: for this session (`gitlab_extra`,
        kept across restarts) or always (policy `gitlab_read_always`, every session). Calls of one
        session racing for the same project share one card. A project allowed here also takes
        `gitlab_comment` (each note on its own card); the inbox stays on the mapped scope."""
        if not GL_PATH_RX.match(gp):
            raise ValueError("project must be a GitLab path like group/repo, not %r" % gp[:120])
        if self.policy.get("security_strict"):
            if not self.unlocked(s, "projects"):
                raise ValueError(NOT_UNLOCKED % ("Reading another project", "Other projects"))
            if not self.ask("gitlab_read", s, "Read %s in %s (Strict: this operation only)" % (tool, gp),
                            {"project": gp, "tool": tool, "iid": args.get("iid")}, timeout=UNLOCKED_ASK_S):
                raise ValueError("the user declined reading the GitLab project %s" % gp)
            return gp
        if gp in self._gitlab_extra_scope(s) or gp in self.gitlab_read_always():
            return gp
        declined = "the user declined reading the GitLab project %s" % gp
        with self.lock:
            if gp in self._gitlab_extra_scope(s) or gp in self.gitlab_read_always():   # allowed while this call got here
                return gp
            wait = self._gl_read_asks.get((s.id, gp))
            mine = wait is None
            if mine:
                wait = self._gl_read_asks[(s.id, gp)] = {"ev": threading.Event(), "ok": False}
        if not mine:   # another call of this session is asking about the same project: its answer is ours
            wait["ev"].wait(UNLOCKED_ASK_S + 30)
            if not wait["ok"]:
                raise ValueError(declined)
            return gp
        try:
            item = {"gitlab_issue": "issue #%s", "gitlab_mr": "merge request !%s", "gitlab_mr_changes": "the changes of !%s",
                    "gitlab_issues": "the issues", "gitlab_milestones": "the milestones", "gitlab_mrs": "the merge requests",
                    "gitlab_pipelines": "the pipelines"
                    }.get(tool, tool).replace("%s", str(args.get("iid") or "?"))
            summary = ("Session %s wants to read %s of the GitLab project %s, which is outside its scope (%s).\n\n"
                       "Allow: the session reads this project's issues, merge requests, diffs (hidden files withheld), "
                       "pipelines and uploads until it ends, may ask for failed job logs and propose comments, "
                       "issue and MR changes there (each one on its own card).\n"
                       "Allow always: every session reads this project without a card and may propose the same "
                       "changes there, each on its own card (Settings -> GitLab).") % (
                s.title, item, gp, ", ".join(s.gitlab) or "no GitLab project")
            res: dict = {}
            wait["ok"] = self.ask("gitlab_read", s, summary, {"project": gp, "tool": tool, "iid": args.get("iid")},
                                  timeout=UNLOCKED_ASK_S, always="Allow always", result=res)
            if wait["ok"]:
                with s.lock:
                    if gp not in s.gitlab_extra:
                        s.gitlab_extra.append(gp)
                    if res.get("globally_approved"):
                        if gp not in s.gitlab_auto_extra:
                            s.gitlab_auto_extra.append(gp)
                    elif gp in s.gitlab_auto_extra:
                        s.gitlab_auto_extra.remove(gp)
                    s.save()
                always = bool(res.get("always")) and self._gitlab_read_keep(gp)
                audit({"event": "gitlab-read", "session": s.id, "project": gp, "tool": tool, "always": always})
                self.bump()
        finally:
            with self.lock:
                self._gl_read_asks.pop((s.id, gp), None)
            wait["ev"].set()
        if not wait["ok"]:
            raise ValueError(declined)
        return gp

    def _gitlab_read_keep(self, gp: str) -> bool:
        """"Allow always": add the project to policy gitlab_read_always (as Settings -> GitLab would)."""
        # from the file, unfiltered: never drop what the user wrote there meanwhile
        cur = [x for x in guard.load_policy(common.POLICY_FILE).get("gitlab_read_always") or [] if isinstance(x, str)]
        if gp in cur:
            return True
        try:
            settings.update("gitlab", {"gitlab_read_always": cur + [gp]}, confirm=["gitlab_read_always"],
                            on_change=lambda stores: self.reload_policy() if "policy" in stores else None)
            return True
        except Exception as e:   # the session keeps its allowance; the list stays as it was
            audit({"event": "gitlab-read-always-failed", "project": gp, "error": str(e)[:300]})
            return False

    def _allow_domain_check(self, s: Session, domain) -> tuple[str, dict]:
        """(the domain as written, the project's .hole/settings.json) - or why it cannot be allowed. Asked before the
        card too (B-N7-4: a card for a name that then failed after Allow)."""
        if s.kind != "project":
            raise ValueError("only a project session has a .hole/settings.json")
        d = str(domain or "").strip().lower().rstrip(".")
        d = re.sub(r":\d+$", "", d)
        if not self.DOMAIN_RX.match(d) or d == "*":
            raise ValueError("%r is not a domain name" % domain)
        rules = dict(holehome.DEFAULTS, **(self.policy.get("holehome") or {}))
        if d in rules["strip_allow"] or self._gitlab_via_piper() and (d == self.gitlab_host() or d.endswith("." + self.gitlab_host())):
            raise ValueError("%s is served by piperd only (K5) - it cannot be allowed" % d)
        try:
            doc = json.loads(common.safe_read(s.path, ".hole/settings.json", 1 << 20).decode() or "{}")
        except FileNotFoundError:
            doc = {}
        except (OSError, ValueError) as e:
            raise ValueError("%s/.hole/settings.json cannot be read: %s" % (s.path, e)) from None
        if not isinstance(doc, dict):
            raise ValueError("%s/.hole/settings.json is not an object" % s.path)
        return d, doc

    def _reaches_this_machine(self, s: Session, d: str, doc: dict) -> str:
        """N9: why domain d (as _allow_domain_check wrote it) names THIS machine - '' when it does not: localhost
        names, *.internal (Docker's / hole's host gateway: host.docker.internal, host.internal), a name of the
        hostGatewayDomains the project or the running sandbox lists, this machine's own host name."""
        base = d[2:] if d.startswith("*.") else d
        if base == "localhost" or base.endswith(".localhost"):
            return "a localhost name"
        if base.endswith(".internal"):
            return "a host gateway name (*.internal)"
        gw = set()
        for entry in list(((doc.get("network") or {}).get("hostGatewayDomains") or [])) + \
                list((s.network or {}).get("gateway") or []):
            name = str(entry).partition(":")[0].strip().lower().rstrip(".")
            if name:
                gw.add(name)
        if any(g == base or (d.startswith("*.") and g.endswith("." + base)) for g in gw):
            return "a host gateway name (hostGatewayDomains)"
        own = socket.gethostname().lower().rstrip(".")
        if own and base in (own, own.split(".")[0] + ".local"):
            return "this machine's own name"
        return ""

    def allow_domain(self, sid: str, domain: str) -> dict:
        """Add a domain to network.allow of the session's project (.hole/settings.json). The
        next start blocks on `settings-changed` until the human approves the diff again."""
        s = self.get(sid)
        d, doc = self._allow_domain_check(s, domain)
        root = s.path
        net = doc.setdefault("network", {})
        allow = list(net.get("allow") or [])
        if d in allow:
            return {"domain": d, "written": False}
        allow.append(d)
        net["allow"] = allow
        if not common.safe_is_dir(root, ".hole"):
            os.mkdir(os.path.join(root, ".hole"), 0o755)
        common.safe_write(root, ".hole/settings.json", (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode())
        audit({"event": "allow-domain", "session": s.id, "domain": d, "file": root + "/.hole/settings.json"})
        return {"domain": d, "written": True}

    # ------------------------------------------------------------ image drop (9.2)
    def drop(self, sid: str, ctype: str, data: bytes) -> str:
        """Store a pasted/dropped image in the session's own ~/.claude and return its
        path inside the container (the terminal pastes it into the prompt)."""
        s = self.get(sid)
        ext = DROP_TYPES.get(ctype.split(";")[0].strip().lower())
        if ext is None:
            raise ValueError("only png, jpeg, gif and webp images can be dropped")
        if not data or len(data) > DROP_MAX:
            raise ValueError("image must be 1 byte..10 MiB")
        name = "%s.%s" % (secrets.token_hex(6), ext)
        common.safe_write(s.box, s.names["drop"] + "/" + name, data)
        return "%s/%s/%s" % (self.box_in(s), s.names["drop"], name)

    # ------------------------------------------------------------ actions
    def type_text(self, s: Session, text: str, *, idle_only: bool = False, _quiet_control: bool = False) -> bool:
        """Paste `text` and press Enter; False when the human typed meanwhile (left in the prompt, not submitted)."""
        if not _quiet_control and hasattr(self, "quiet") and (why := self.quiet.blocked(s.id)):
            raise ValueError(why)
        t0 = time.time()
        if idle_only and self._turn_running(s):
            return False
        paste = "\x1b[200~" + text.replace("\x1b", "") + "\x1b[201~"
        r = self.pty.call("input", id=s.id, data=b64(paste.encode()), auto=True)
        if not r.get("ok"):
            raise ValueError("the terminal refused the input: %s" % r.get("error"))
        time.sleep(0.3)
        if idle_only and self._turn_running(s):
            return False
        k = self._last_key(s)
        if k is not None and k["key_at"] > t0:   # the human started typing in these 0.3 s: their keys are in it
            audit({"event": "auto-type-unsent", "session": s.id})   # left in the prompt, not submitted: they decide
            return False
        self.pty.call("input", id=s.id, data=b64(b"\r"), auto=True)
        return True

    # ------------------------------------------------------------ the human's half-written prompt (9.12)
    HOLD_TEXT = "you typed in the terminal since the last prompt - automatic text waits (or click Send now)"
    GUI_SEND_S = 30.0   # a UserPromptSubmit this soon after "Send" (card, Composer) is that prompt
    SUBMIT_HOOK_MATCH_S = 5.0   # bounded replay correlation for older metadata without input markers

    def _last_key(self, s: Session) -> dict | None:
        """{key_at, enter_at} of ptyhost's InputTracker: when the human last touched the prompt and
        when the last Enter was (the human's or piperd's); None when ptyhost cannot tell (an older
        ptyhost, not a live PTY)."""
        try:
            r = self.pty.call("pending", id=s.id, timeout=3)
        except PtyHostError:
            return None
        if not r.get("ok") or not isinstance(r.get("key_at"), (int, float)) or not isinstance(r.get("enter_at"), (int, float)):
            return None
        return {"key_at": float(r["key_at"]), "enter_at": float(r["enter_at"])}

    def input_pending(self, s: Session) -> bool | None:
        """Did the human type anything after the Enter of the last submitted prompt (UserPromptSubmit,
        /clear) or after "Send now"? Then the prompt may hold their text and piperd must not type
        into it. What an Enter did (submit, a new line after a backslash, an @ pick, a dialog answer)
        is never guessed: any key since then holds - the only possible mistake is a click too many."""
        k = self._last_key(s)
        if k is None:
            return None
        return k["key_at"] > max(s.submit_at, s.clear_at)

    def auto_type_ok(self, s: Session) -> bool:
        """May piperd type automatic text now? The card shows why not (`hold`, "Send now");
        an older ptyhost cannot tell = yes (as before 9.12)."""
        held = bool(self.input_pending(s))
        hold = self.HOLD_TEXT if held else ""
        if s.hold != hold:
            s.hold = hold
            self.bump()
        return not held

    def _note_submit(self, s: Session, e: dict) -> bool | None:
        """A UserPromptSubmit or SessionStart(source=clear): what was typed up to the last Enter in
        the terminal went out. The mark is that Enter's time from ptyhost - never the moment piperd
        reads the hook (a draft typed right after the Enter, or before a piperd restart replays the
        file, stays protected). Older events only restore a matching confirmed Enter boundary;
        they never become new activity. Returns whether
        the human sent it: they touched the prompt since the previous mark, or used "Send" on the
        card / in the Composer just before (the timesheet, 9.12 A); None when ptyhost cannot tell."""
        ts = e.get("ts")
        now = time.time()
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts):
            return None
        k = self._last_key(s) if s.pid is not None else None
        if abs(now - float(ts)) > workbench.EVENT_MAX_AGE:
            # Restore only a confirmed hook matching the latest terminal Enter.
            # A later draft still has key_at > enter_at; a later unconfirmed
            # Enter has enter_at > ts and must never be guessed to be a submit.
            if k and ts <= now and k["enter_at"] > 0 and 0 <= ts - k["enter_at"] <= self.SUBMIT_HOOK_MATCH_S:
                s.submit_at = max(s.submit_at, k["enter_at"])
            return None   # a replay never becomes new human activity/snapshots
        gui = now - s.gui_sent_at <= self.GUI_SEND_S
        s.gui_sent_at = 0.0
        if k is None:
            return None
        human = k["key_at"] > max(s.submit_at, s.clear_at) or gui
        s.submit_at = max(s.submit_at, k["enter_at"])
        s.hold_try = 0.0   # re-check the badge on the next poll
        return human

    def input_clear(self, sid: str) -> None:
        """\"Send now\" on the card: the human looked at the terminal and lets piperd type now."""
        s = self.get(sid)
        previous = s.clear_at, s.hold
        s.clear_at = time.time()
        s.hold = ""
        try:
            s.save()
        except Exception:
            s.clear_at, s.hold = previous
            raise
        self.bump()
        audit({"event": "gui-input-clear", "session": s.id})
        if s.handoff_notify:   # the line that waited for this prompt goes now (it types: not in the HTTP thread)
            threading.Thread(target=self._flush_handoff_notes, args=(s,), daemon=True, name="handoff-" + s.id).start()
        if self._task_pending(s) and s.state in ("ready", "attention"):   # the start task that waited, too
            self._deliver_task(s)

    # ------------------------------------------------------------ desktop notifications
    def gui_polled(self) -> None:
        """The GUI read /api/state: it is open (maybe hidden) and notifies by itself."""
        self.gui_polled_at = time.time()

    def _desktop_notify(self, s: Session, now: float | None = None) -> None:
        """A session used from a native terminal waits for the human while no GUI is open: tell
        them on the host. Once per state; `waiting` (a finished turn) only while no terminal is
        attached - whoever types in it sees the turn end, a notice per turn would be noise."""
        now = time.time() if now is None else now
        want = s.state == "attention" or (s.state == "waiting" and not s.tty)
        if not want:
            if s.state != s.desk_state:
                s.desk_state = ""
            return
        if s.desk_state == s.state or not s.tty_used or not DESKTOP_NOTIFY:
            return
        if now - getattr(self, "gui_polled_at", 0.0) < GUI_GONE_S:
            return
        s.desk_state = s.state
        title = "%s · %s" % (s.title or s.id, "needs you" if s.state == "attention" else "waiting for you")
        self._desk_run(title, "piper attach %s" % s.id)

    def _desk_run(self, title: str, body: str) -> None:
        """The host's notification centre (a test seam). Arguments, never a script built from text."""
        if sys.platform == "darwin":
            argv = ["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title \"Piper\" subtitle (item 1 of argv)",
                    "-e", "end run", title, body]
        elif shutil.which("notify-send"):
            argv = ["notify-send", "Piper: " + title, body]
        else:
            return
        try:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    # ------------------------------------------------------------ Mattermost (9.12 E)
    def presence(self, visible: bool) -> None:
        self.gui_visible_at = time.time() if visible else 0.0

    def mm_status(self) -> dict:
        return self.mm.status()

    def mm_test(self) -> dict:
        return self.mm.test()

    def _prod_to_mm(self, report: dict) -> None:
        fresh = report.get("fresh") or []
        what = {"error": "error", "rate": "error rate increase", "alert": "alert"}
        lines = ["Morning production check, new items: %d" % len(fresh)]
        if mattermost.load_config()["detail"] == "none":   # only the count: titles come from production logs
            self.mm.notify(lines[0] + " (details in the Overview)", defer=True)
            return
        lines += ["- %s · %s: %s (%s×)" % (it.get("project"), what.get(it.get("kind"), it.get("kind")),
                                          it.get("title"), it.get("count")) for it in fresh[:10]]
        if len(fresh) > 10:
            lines.append("- … and %d more in the Overview" % (len(fresh) - 10))
        self.mm.notify("\n".join(lines), defer=True)   # quiet hours: sent when they end, not dropped

    def mm_notify(self, text: str, force: bool = False) -> bool:
        """A one-off Mattermost message for other features (the morning summary, 9.12 C)."""
        return self.mm.notify(text, force)

    def stop(self, sid: str) -> None:
        s = self.get(sid)
        if s.pid is None:
            # under the locks _reserve (Approve / Retry / Add excludes) takes: either it ends this blocked
            # session first, or it sees the start that _reserve began and cancels it - a Stop is never lost
            with s.lock, s.cancel_lock:   # _start sets the pid and reads cancel under the same lock
                ended = s.state in ("blocked", "error")
                if ended:
                    s.since, s.state = time.time(), "exited"
                starting = s.pid is None and s.state in ("preflight", "preparing", "spawning")
                if starting:
                    s.cancel = True
            if ended:
                self._set(s, None, "", "canceled")
                return
            if starting:   # _start stops at its next step
                self._set(s, "stopping", "", "stops once the current start step finishes")
                return
            if s.pid is None:
                if s.state == "stopping":   # a second Stop while the start step still runs
                    return
                raise ValueError("session is %s - nothing to stop yet" % s.state)
        if s.rc is not None:   # the child already exited: nothing to signal, never "stopping"
            if s.state in ACTIVE:
                self._set(s, "exited")
            return
        if s.state not in ACTIVE and not self._pty_alive(s):   # lost with a ptyhost restart: its pid is
            return   # kept, nothing runs (an "error" whose hole still runs is stopped below, nudge included)
        s.stop_attached = s.state in ("ready", "working", "waiting", "attention")
        self._set(s, "stopping")
        r = self.pty.call("signal", id=s.id, sig="TERM")
        if (r.get("alive") is False or r.get("ok") is False) and s.state == "stopping":
            # exited before piperd heard of it / ptyhost no longer knows it (ok false: unknown session)
            self._set(s, "exited")
            return
        s.stop_first, s.stop_kick = s.stop_first or time.time(), time.time()   # _stop_nudge takes over

    def _stop_nudge(self, s: Session, now: float | None = None) -> None:
        """A stopping session whose hole is still alive (the poll loop, every tick): hole 2.1.0
        drops a SIGTERM that arrives before it attaches to the agent, so repeat it every
        STOP_RETRY s and after STOP_KILL_AFTER s kill it (STOP_KILL_AFTER_ATTACHED when the agent
        ran: hole took the TERM and is tearing the sandbox down) - `hole __watchdog` (its own session)
        tears the sandbox down when hole dies, and hole's GC finishes what an early kill leaves."""
        if s.state != "stopping" or s.pid is None or s.rc is not None:
            return
        now = time.time() if now is None else now
        if not s.stop_kick:   # found stopping after a piperd restart: the clock starts now
            s.stop_first = s.stop_kick = now
            return
        if now - s.stop_kick < STOP_RETRY:
            return
        s.stop_kick = now
        sig = "KILL" if now - s.stop_first >= (STOP_KILL_AFTER_ATTACHED if s.stop_attached else STOP_KILL_AFTER) \
            else "TERM"
        r = self.pty.call("signal", id=s.id, sig=sig)
        if r.get("alive") is False:
            return   # the exit event (or the reconnect) ends it
        if r.get("ok") is False:   # ptyhost does not know it (any more): no exit event will come
            self._set(s, "exited", "", "lost (ptyhost does not know the session)")
            return
        audit({"event": "stop-nudge", "session": s.id, "sig": sig, "after": round(now - s.stop_first, 1)})
        detail = ("hole ignores the stop while it is still starting - retrying" if sig == "TERM"
                  else "killed - hole did not stop; its watchdog removes the sandbox")
        if s.detail != detail:
            self._set(s, None, None, detail)

    def max_sessions(self) -> int:
        """The concurrent-session limit: policy max_sessions (Settings → Sessions, applied at once), else
        PIPER_MAX_SESSIONS."""
        n = self.policy.get("max_sessions")
        return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else MAX_SESSIONS

    def _check_limit(self, s: "Session | None" = None) -> None:
        """max_sessions(): a blocked / ended session is not counted, so its (re)start checks again."""
        if hasattr(self, "quiet") and (why := self.quiet.blocked()):
            raise ValueError(why)
        limit = self.max_sessions()
        with self.lock:
            running = [x for x in self.sessions.values() if x.state in ACTIVE and x is not s]
        if len(running) >= limit:
            raise ValueError("limit of %d concurrent sessions reached (Settings → Sessions)" % limit)

    def _reserve(self, s: "Session", from_states: tuple = ("blocked",), fresh: bool = False) -> None:
        """The limit check and the move into an ACTIVE state as one step: two Approve / Retry at the limit
        at once must not both pass (review 2026-10-07) - nor two of them on the SAME session (a double
        click, two tabs): the second finds it in preflight already and is refused, one start only."""
        with self.lock:
            if s.state not in from_states:
                raise ValueError("session is %s" % s.state)
            self._check_limit(s)
            with s.lock, s.cancel_lock:
                if s.state not in from_states:   # a Stop ended it meanwhile (it takes these two locks)
                    raise ValueError("session is %s" % s.state)
                if s.state != "preflight":
                    s.since = time.time()
                # with the state, not after it: a Stop that sees this preflight sets cancel and must keep it
                s.state, s.cancel = "preflight", False
                if fresh:   # retry / resume (the old hole is gone): with the state too - a Stop that saw the old
                    s.pid, s.rc = None, None   # pid with its rc marked the session exited and the start went on
                    s.interrupted = None   # 9.141: restored - no longer in the Interrupted section

    def approve(self, sid: str) -> None:
        """Approve the current settings files of a blocked session and retry its start."""
        s = self.get(sid)
        if s.state != "blocked":
            raise ValueError("session is not blocked")
        self._check_limit(s)
        if any(b["kind"] != "settings-changed" for b in s.blocks):
            raise ValueError("only changed settings can be approved here; fix the other findings "
                             "(policy.json / the settings files) and retry")
        with APPROVED_LOCK:   # read-modify-write: a concurrent approve / Add excludes must not lose this one
            approved = read_json(common.APPROVED_FILE, {})
            for path, sha in s.settings.items():
                approved[path] = {"sha": sha, "approved": time.time(), "text": (s.texts.get(path) or "")[:65536]}
            write_json(common.APPROVED_FILE, approved)
        audit({"event": "settings-approved", "session": s.id, "files": sorted(s.settings)})
        self._reserve(s)   # from here on a Stop cancels the start
        self._set(s, None, "", "checking isolation policy")
        threading.Thread(target=self._start, args=(s,), daemon=True).start()

    def fix_excludes(self, sid: str) -> dict:
        """Write the missing excludes of a blocked session into the settings file hole reads
        for each root: the project's own .hole/settings.json, or a library's (created, and
        kept out of commits via .git/info/exclude). Includes cannot exclude anything.
        The trees are sandbox-writable, so every access is symlink-safe (common.safe_*)."""
        s = self.get(sid)
        if s.state != "blocked":
            raise ValueError("session is not blocked")
        self._check_limit(s)
        written, skipped = [], []
        approved, reapproved = read_json(common.APPROVED_FILE, {}), []
        for c in s.coverage:
            unc = sorted({_worktree_glob(guard.real_rel(c["root"], u)) for u in c.get("uncovered") or []})
            if not unc:
                continue
            if c.get("kind") == "include":
                skipped.append("%s (a files.include - remove it or the files from your ~/.hole/settings.json)" % c["root"])
                continue
            root = c["root"]
            try:
                raw = common.safe_read(root, ".hole/settings.json", 1 << 20)
                doc, was = json.loads(raw.decode() or "{}"), hashlib.sha256(raw).hexdigest()
            except FileNotFoundError:
                doc, was = {}, "absent"
            except (OSError, ValueError) as e:
                skipped.append("%s/.hole/settings.json unreadable (%s)" % (root, e))
                continue
            excl = list(((doc.setdefault("files", {})).get("exclude")) or [])
            excl += [u for u in unc if u not in excl]
            doc["files"]["exclude"] = excl
            if not common.safe_is_dir(root, ".hole"):
                os.mkdir(os.path.join(root, ".hole"), 0o755)   # fails on a planted symlink: fine
            out = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
            common.safe_write(root, ".hole/settings.json", out)
            written.append("%s/.hole/settings.json (+%d)" % (root, len(unc)))
            # F1-3: what Piper itself wrote (approved file + excludes only) needs no second approval;
            # a file that was not approved before stays a settings-changed block (fail-closed), and
            # the sha is of these bytes - any later change by anyone else still blocks
            path = os.path.join(root, ".hole", "settings.json")
            prev = (approved.get(path) or {}).get("sha")
            if prev == was or (was == "absent" and not prev):
                approved[path] = {"sha": hashlib.sha256(out).hexdigest(), "approved": time.time(),
                                  "text": out.decode("utf-8", "replace")[:65536]}
                reapproved.append(path)
            if c.get("kind") == "library" and common.safe_is_dir(root, ".git/info"):
                try:
                    cur = common.safe_read(root, ".git/info/exclude", 1 << 20).decode()
                except FileNotFoundError:
                    cur = ""
                if ".hole/" not in cur.splitlines():
                    common.safe_write(root, ".git/info/exclude", (cur.rstrip("\n") + "\n.hole/\n").lstrip("\n").encode())
        if reapproved:
            with APPROVED_LOCK:   # merged into the file as it is now: never drops a concurrent approval
                cur = read_json(common.APPROVED_FILE, {})
                cur.update({p: approved[p] for p in reapproved})
                write_json(common.APPROVED_FILE, cur)
        audit({"event": "fix-excludes", "session": s.id, "written": written, "skipped": skipped,
               "approved": reapproved})
        if written:
            self._reserve(s)
            self._set(s, None, "", "checking isolation policy")
            threading.Thread(target=self._start, args=(s,), daemon=True).start()
        return {"written": written, "skipped": skipped}

    def allow_uncovered(self, sid: str) -> dict:
        """The other answer to `uncovered`: the files are meant to be visible (a project that needs
        them), so accept them in their mount root only - policy library_allow[root] (Settings would
        need the same per-root list) - and retry the start. The GUI confirms it first; it weakens
        the isolation for every later session of that root."""
        s = self.get(sid)
        if s.state != "blocked":
            raise ValueError("session is not blocked")
        if any(b.get("kind") == "protected-mounted" for b in s.blocks):
            raise ValueError("a protected mount cannot be allowed; fix the mount and retry")
        self._check_limit(s)
        allowed = []
        for c in s.coverage:
            unc = sorted({_worktree_glob(_glob_escape(guard.real_rel(c["root"], u))) for u in c.get("uncovered") or []})
            if not unc:
                continue
            new = settings.add_root_allow(c["root"], unc,
                                          on_change=lambda stores: self.reload_policy() if "policy" in stores else None)
            if new:
                allowed.append("%s (+%d)" % (c["root"], len(new)))
        audit({"event": "allow-uncovered", "session": s.id, "allowed": allowed})
        if allowed:
            self._reserve(s)
            self._set(s, None, "", "checking isolation policy")
            threading.Thread(target=self._start, args=(s,), daemon=True).start()
        return {"allowed": allowed}

    def _pty_alive(self, s: Session) -> bool:
        """Does ptyhost still run this session's process? (unknown: no)"""
        try:
            return any(x.get("id") == s.id and x.get("alive") for x in self.pty.call("list").get("sessions") or [])
        except Exception:  # noqa: BLE001 - no ptyhost: nothing of it runs
            return False

    def set_start_opt(self, sid: str, what: str, on: bool) -> dict:
        """A stopped session's START_OPTS, from its Isolation panel: they shape the sandbox (hole
        argv, mcp.json, gateway), so they apply at the next start (Continue)."""
        s = self.get(sid)
        if what not in START_OPTS:
            raise ValueError("unknown start option %r (one of %s)" % (what, ", ".join(START_OPTS)))
        if s.kind == "advisor":
            raise ValueError("the Global agent takes no start options")
        if s.state not in ("blocked", "error", "exited"):
            raise ValueError("stop the session first - it applies at the next start (Continue)")
        if on:
            s.opts[what] = True
        else:
            s.opts.pop(what, None)
        s.save()
        audit({"event": "start-opt", "session": s.id, "what": what, "on": on})
        self.bump()
        return {"opts": dict(s.opts)}

    def retry(self, sid: str) -> dict:
        """{"lost": True}: an ended session's conversation is gone, it starts a new one with its task (D8 - the GUI says so)."""
        s = self.get(sid)
        if s.state not in ("blocked", "error", "exited"):
            raise ValueError("session is %s" % s.state)
        if s.pid is not None and s.rc is None and self._pty_alive(s):
            raise ValueError("the previous hole of this session is still shutting down - retry in a moment")
        if s.fork_pending and not self._fork_unwritten(s):
            s.fork_pending = False   # the child wrote its history after SessionStart
        if s.fork_pending:   # before the reservation: a refusal after it would leave the session in preflight
            digest.validate_conversation(s.codex_home if s.agent == "codex" else s.claude_src,
                                         (s.fork_from or {}).get("conversation"), s.agent)
        prev = s.state
        self._reserve(s, ("blocked", "error", "exited"), fresh=True)
        # resume only a conversation that was written: an agent that never got a prompt has a session id
        # but no transcript, and `--resume <id>` then ends it at once ("No conversation found")
        if s.fork_pending:
            native = (s.fork_from or {}).get("conversation")
            s.resume = native
        else:
            s.resume = s.claude_session if prev == "exited" and s.claude_session and \
                self._has_transcript(s, s.claude_session) else None
        if s.resume and not s.fork_pending and (s.agent != "codex" and s.claude_mode == "host" and self.claude_dir() == "session" or
                         s.agent == "codex" and s.host_codex and self.codex_dir() != "host"):
            s.resume = None   # _start drops it (the stricter switch to per-session dirs): Continue says it is lost
        if s.kind == "project" and not s.profile:   # a session created before the project profile existed
            s.profile = holehome.PROJECT_PROFILE
        lost = prev == "exited" and bool(s.claude_session) and not s.fork_pending and s.resume is None
        s.rc, s.attest, s.blocks, s.pid = None, None, [], None   # the old pid is gone (cancel: _reserve)
        threading.Thread(target=self._start, args=(s,), daemon=True).start()
        return {"lost": True} if lost else {}

    def _has_transcript(self, s: Session, conv: str) -> bool:
        base = {"none": os.path.join(s.codex_home, "sessions") if s.codex_dir else "",
                "host": os.path.join(common.REAL_HOME, ".claude", "projects")}.get(
            s.claude_mode, os.path.join(s.cfg, "projects"))
        if not base or not os.path.isdir(base) or not re.match(r"^[0-9a-zA-Z-]{8,64}$", conv):
            return False
        for root, _dirs, files in os.walk(base):
            for name in files:   # Claude: <id>.jsonl, Codex: rollout-<time>-<id>.jsonl
                if name == conv + ".jsonl" or name.endswith("-" + conv + ".jsonl"):
                    try:
                        with open(os.path.join(root, name), "rb") as f:
                            if conv.encode() in f.read(65536):
                                return True
                    except OSError:
                        pass
        return False

    def forget(self, sid: str) -> None:
        s = self.get(sid)
        if s.state in ACTIVE:
            raise ValueError("stop the session first")
        try:
            r = self.pty.call("forget", id=s.id)
        except PtyHostError:
            r = {"ok": True}
        if not r.get("ok") and r.get("error") == "still running":
            raise ValueError("the sandbox is still running - stop the session first")
        s.hidden = True
        s.interrupted = None   # 9.141: Remove ends the offer too
        self._drop_turn_snapshots(s)
        s.save()
        self.bump()

    # ------------------------------------------------------------ confirmations
    def approval_grants(self, s: Session) -> list[dict]:
        """Active project grants, exposed beside this session's existing unlock controls."""
        now = time.time()
        with self.lock:
            grants = read_json(APPROVAL_GRANTS_FILE, [])
            return [g for g in grants if isinstance(g, dict) and s.project in (g.get("project"), g.get("source_project"))
                    and g.get("expires_at", 0) > now]

    def revoke_approval(self, sid: str, scope: str) -> None:
        s = self.get(sid)
        with self.lock:
            grants = read_json(APPROVAL_GRANTS_FILE, [])
            write_json(APPROVAL_GRANTS_FILE, [g for g in grants if g.get("scope") != scope or
                                             s.project not in (g.get("project"), g.get("source_project"))])
            if scope in s.approvals:
                s.approvals.remove(scope)
                try:
                    s.save()
                except Exception:
                    s.approvals.append(scope)
                    s.approvals.sort()
                    raise
        audit({"event": "approval-revoked", "session": sid, "scope": scope})
        self.bump()

    def _push_evidence_forge(self, s, remote, branch, budget):
        """Only the actual remote's scoped forge; never substitute another configured project."""
        root = self._session_top(s)
        url = gitops._git(root, "remote", "get-url", "--push", remote, timeout=budget.left()).strip()
        p = {"path": root, "key": s.project}
        glp, allowed = self._gl_pick(p)
        # The existing origin parsers are trusted; a non-origin remote must match it exactly.
        origin = gitops._git(root, "remote", "get-url", "origin", timeout=budget.left(), ok=(0, 2)).strip()
        if url != origin:
            return {}
        if glp and glp == self.origin_gl(root) and gitlab.token():
            gl = gitlab.GitLab(self.gitlab_host(), gitlab.token(), opener=self.gitlab_opener)
            opener = gl.opener
            gl.opener = lambda req, timeout: opener(req, timeout=min(timeout, budget.left()))
            return gitlab.push_evidence(gl, glp, branch, allowed, budget)
        gh, entry, allowed = self._gh_pick(p)
        if gh and entry and entry.lower() == (self.origin_gh(root) or "").lower():
            opener = gh.opener
            gh.opener = lambda req, timeout: opener(req, timeout=min(timeout, budget.left()))
            return github.push_evidence(gh, github.split(entry)[1], branch, allowed, budget)
        return {}

    def _card_evidence(self, kind, s, payload, summary):
        if kind == "git_push":
            plan = {"branch": payload["branch"], "remote": payload["remote"], "new": payload["push_new"]} \
                if payload.get("branch") and payload.get("remote") and isinstance(payload.get("push_new"), bool) else None
            return evidence.git_push(self._session_top(s), lambda r, b, budget: self._push_evidence_forge(s, r, b, budget), plan=plan)
        if kind in ("edit_file", "write_file", "write_exec"):
            p = projects.find(payload.get("project", ""), PROJECT_PATH)
            if not p or not p.get("exists"):
                return evidence.unavailable("project unavailable")
            try:
                branch = gitops._git(p["path"], "branch", "--show-current", timeout=1).strip() or "detached HEAD"
            except gitops.GitError:
                branch = "Could not check: branch unavailable"
            count = len(self._same_tree(p["path"]))
            diff = summary[summary.index("--- "):] if "--- " in summary else "Could not check: diff unavailable"
            return evidence.box(rows=[{"text": "Project %s · branch %s · %d sessions use it" %
                                     (p["key"], branch, count)}], diff=diff[:30000], inline_diff=True)
        return evidence.unavailable("no collector for this action", "What it sends" if kind.startswith("http_") else "What it reads"
                                    if kind.startswith("db_read_") else "What it changes")

    @staticmethod
    def approval_scope(kind: str, payload: dict) -> str:
        """Keep card grants separate by action, dev/test/prod and a payload's danger flag.

        Classes come from the host's HTTP/DB classifiers, never from the GUI's decision.
        An unrecognised environment class is production, as in the HTTP classifier.
        """
        cls = payload.get("cls", kind.rsplit("_", 1)[-1] if kind.rsplit("_", 1)[-1] in ("dev", "test", "prod") else "")
        if cls not in ("", "dev", "test", "prod"):
            cls = "prod"
        return json.dumps([kind, cls, payload.get("danger") is True or payload.get("weakens") is True], separators=(",", ":"))

    def auto_ok(self, kind: str, payload=None) -> bool:
        """Approved without a card? With the security tree (policy `ask`, 9.46): every kind not switched
        on there, except security.REQUIRED_ASK. Before it: auto_approve minus ALWAYS_ASK. A red card (payload danger: a hidden file into
        git, N7) is the human's every time while hidden_files is on."""
        if isinstance(payload, dict) and payload.get("_force_approval") is True:
            return False
        if kind in security.REQUIRED_ASK or kind in ('http_runtime_file', 'http_runtime_code', 'http_env_set', 'properties_set', 'http_cert_passphrase', 'http_auth_signin'):
            return False
        if isinstance(payload, dict) and payload.get("danger") is True and self.policy.get("danger_cards") is not False:
            return False
        if isinstance(self.policy.get("ask"), list):
            return kind not in self.policy["ask"]
        return kind in (self.policy.get("auto_approve") or []) and kind not in ALWAYS_ASK

    def unlocked(self, s: Session, what: str) -> bool:
        """An UNLOCKS switch of this session - always open while the security tree's `unlocks` is off."""
        return bool((getattr(s, "unlock", None) or {}).get(what)) or self.policy.get("unlocks_required") is False

    def _gitlab_via_piper(self) -> bool:
        """K5: the GitLab host only through piperd (security tree gitlab_via_piper; on before the tree)."""
        return self.policy.get("gitlab_via_piper") is not False

    def ask(self, kind: str, session: Session, summary: str, payload: dict, timeout: float = 840,
            always: str = "", result: dict | None = None) -> bool:
        """A card in the GUI; True when the human allowed it. `always`: the label of a third button
        ("Allow always") - the caller reads result["always"] to keep the decision."""
        if session.id.startswith("provider-") and getattr(session, "_provider_active", True) is False:
            return False
        scoped = bool(self.policy.get("security_scoped"))
        strict = bool(self.policy.get("security_strict"))
        if scoped:
            # A scoped profile never consumes legacy broad grants. Connection/content caches live
            # in the DB/HTTP callers and are keyed by their precise host-classified resources.
            always = always if not strict and kind in ("http_dev", "http_test") and not payload.get("_force_approval") else ""
        scope = self.approval_scope(kind, payload)
        payload = dict(payload)
        job = payload.pop("_evidence_job", None)
        if kind == "git_push":
            job = job or evidence.Job(lambda: self._card_evidence(kind, session, payload, summary))
            payload["evidence"] = job.initial()
            push = payload["evidence"].get("push") or {}
            payload["push_verified"] = push.get("verified") is True
            if push.get("verified"):
                payload.update(branch=push["branch"], remote=push["remote"], push_new=push["new"])
        project = payload.get("project") or session.project
        # A forced card (privileged/strict runs) and a scoped profile take no broad grant: no hour grant either.
        match = None if scoped or payload.get("_force_approval") else evidence.matching_scope(kind, project, payload)
        with self.lock:
            timed = next((g for g in read_json(APPROVAL_GRANTS_FILE, []) if match and g.get("scope") == match["scope"]
                          and g.get("expires_at", 0) > time.time()), None)
        with self.lock:
            session_ok = not scoped and scope in session.approvals and not payload.get("_force_approval")
        # Blind configuration writes are explicitly one-card-per-write, independent of Security switches.
        if kind not in ("http_env_set", "properties_set", "http_runtime_file", "http_runtime_code") and (session_ok or timed or self.auto_ok(kind, payload)):
            audit({"event": "confirm", "kind": kind, "session": session.id, "approved": True, "auto": True,
                   **({"session_approval": scope} if session_ok else {}),
                   **({"timed_approval": timed["scope"]} if timed else {})})
            if result is not None:
                result.update(always=False, session_always=False, session_approved=bool(session_ok or timed),
                              globally_approved=not (session_ok or timed), answered=True)
            try:
                if self.policy.get("feed_auto_actions"):
                    self.feed_auto(session, kind, summary)
            except Exception as e:  # noqa: BLE001 - the feed is a view, never a reason to refuse the action
                print("piperd: feed_auto: %s" % e, flush=True)
            return True
        pid = secrets.token_hex(4)
        ev = threading.Event()
        if job is None and "evidence" not in payload:
            job = evidence.Job(lambda: self._card_evidence(kind, session, payload, summary))
        if job:
            payload["evidence"] = job.initial()
        payload = evidence.clean(payload)
        item = {"id": pid, "kind": kind, "session": session.id, "title": session.title,
                "summary": evidence.clean(summary), "payload": payload, "created": time.time(),
                "_ev": ev, "_ok": False}
        cls = json.loads(scope)[1]
        if not scoped and not payload.get("_force_approval") and kind not in ("http_env_set", "properties_set", "http_runtime_file", "http_runtime_code"):
            item["session_always"] = "Allow for this session%s" % (" on " + cls.upper() if cls else "")
        item["_scope"] = scope
        if match:
            item["similar"] = match["description"]
            item["_match"] = dict(match, project=project, source_project=session.project)
            with self.lock:
                suggest = evidence.suggestion(read_json(APPROVAL_HISTORY_FILE, []), match["scope"], description=match["description"])
            if suggest:
                item["suggestion"] = suggest
        if always:
            item["always"] = always
        why = ask_why(kind)
        if why:   # F17: the card's "Asks because Security › Git › Push"
            item["why"] = why
        with self.lock:
            self.pending[pid] = item
        self.bump()
        if job and payload["evidence"].get("state") == "checking":
            def update_evidence():
                finished = job.done.wait(8)
                value = job.value if finished else evidence.unavailable("timed out", job.title)
                with self.lock:
                    if self.pending.get(pid) is not item or ev.is_set():
                        return
                    item["payload"] = dict(item["payload"], evidence=value)
                self.bump()
            threading.Thread(target=update_evidence, daemon=True, name="piper-evidence-update").start()
        answered = ev.wait(timeout)
        with self.lock:   # decide() answers under the same lock: after this pop no answer changes the card
            self.pending.pop(pid, None)
            answered = ev.is_set() and not item.get("_cancelled")
            if not answered or kind in ("http_runtime_file", "http_runtime_code") and not item["_ok"]:
                # Runtime inputs pause a live run: never claim earlier requests were not sent.
                now = time.time()
                self.pending_gone = [g for g in getattr(self, "pending_gone", []) if now - g["at"] < GONE_S][-20:] + [
                    {"id": pid, "kind": kind, "session": session.id, "title": session.title, "at": now,
                     **({"note": ("cancelled" if item.get("_cancelled") else "timed out" if not answered else "declined") +
                         " · earlier requests of this run may have run"} if kind in ("http_runtime_file", "http_runtime_code") else {})}]
        self.bump()
        # answered=False: the card timed out - nobody was there (the timesheet must not count it as attention)
        audit({"event": "confirm", "kind": kind, "session": session.id, "approved": item["_ok"], "answered": answered,
               "evidence": item["payload"]["evidence"], "decided_at": time.time(),
               **({"matching_scope": match["scope"]} if match else {}),
               **({"timed_approval": item["_match"]["scope"]} if item.get("_similar_hour") else {}),
               **({"suggestion_history_unavailable": True} if item.get("_history_unavailable") else {}),
               **({"cancelled": item["_cancelled"]} if item.get("_cancelled") else {}),
               **({"always": True} if item.get("_always") else {}),
               **({"session_approval": scope} if item.get("_session_always") else {})})
        if result is not None:
            result["always"] = bool(item.get("_always"))
            result["session_always"] = bool(item.get("_session_always"))
            result["answered"] = answered   # False: the card timed out (not a "no")
        return item["_ok"]

    def ask_gui(self, kind: str, project: str, summary: str, payload: dict, timeout: float) -> bool:
        """Mandatory one-shot card for the user's own run; never impersonates a session."""
        if kind != 'http_runtime_code':
            raise ValueError('unsupported sessionless approval')
        pid, ev = secrets.token_hex(4), threading.Event()
        title = project + ' · your run'
        item = dict(id=pid, kind=kind, session='', title=title, summary=common.show_bidi(summary),
                    payload=common.show_bidi(payload), created=time.time(), _ev=ev, _ok=False)
        with self.lock:
            self.pending[pid] = item
        self.bump()
        ev.wait(timeout)
        with self.lock:
            self.pending.pop(pid, None)
            answered = ev.is_set() and not item.get('_cancelled')
            if not answered or not item['_ok']:
                now = time.time()
                self.pending_gone = [g for g in getattr(self, 'pending_gone', []) if now - g['at'] < GONE_S][-20:] + [
                    dict(id=pid, kind=kind, session='', title=title, at=now,
                         note=('cancelled' if item.get('_cancelled') else 'timed out' if not answered else 'declined') +
                              ' · earlier requests of this run may have run')]
        self.bump()
        audit(dict(event='confirm', kind=kind, session='', project=project, approved=item['_ok'], answered=answered))
        return item['_ok']

    def ask_http_signin(self, session, project, summary, payload, flow):
        """Only a completed host flow answers this mandatory one-run sign-in card."""
        if session and session.id.startswith('provider-') and getattr(session, '_provider_active', True) is False:
            raise ValueError('authentication session is no longer active')
        pid, ev = secrets.token_hex(4), threading.Event()
        sid, title = (session.id, session.title) if session else ('', project + ' · your run')
        item = dict(id=pid, kind='http_auth_signin', session=sid, title=title, summary=common.show_bidi(summary),
                    payload=common.show_bidi(payload), created=time.time(), _ev=ev, _ok=False, _auth_open=flow.open,
                    _auth_closed=flow.browser_closed.set)
        with self.lock:
            self.pending[pid] = item
        self.bump()
        success, note = False, 'sign-in failed · earlier requests may have run; protected request was not sent'
        try:
            result = flow.wait(ev)
            with self.lock:
                if ev.is_set():
                    raise ValueError('authentication cancelled; protected request was not sent')
                item['_ok'] = success = True
                ev.set()
            return result
        except ValueError as error:
            message = str(error)
            note = ('sign-in cancelled' if ev.is_set() else 'sign-in not finished by the deadline' if 'deadline' in message else
                    message if isinstance(error, httpclient.httpauth.OAuthError) else 'sign-in failed') + ' · earlier requests may have run; protected request was not sent'
            raise
        finally:
            flow.close()
            with self.lock:
                self.pending.pop(pid, None)
                item.pop('_auth_open', None)
                item.pop('_auth_closed', None)
                if not success:
                    now = time.time()
                    self.pending_gone = [g for g in getattr(self, 'pending_gone', []) if now - g['at'] < GONE_S][-20:] + [
                        dict(id=pid, kind='http_auth_signin', session=sid, title=title, at=now, note=note)]
            audit(dict(event='confirm', kind='http_auth_signin', session=sid, approved=success))
            self.bump()

    def http_auth_open(self, pid):
        """GUI-only route opens the fixed provider URL, without approving the run."""
        with self.lock:
            item = self.pending.get(pid)
            if item is None or item.get('kind') != 'http_auth_signin' or item['_ev'].is_set():
                raise KeyError(pid)
            url = item['_auth_open']()
            item['payload']['state'] = 'waiting'
        self.bump()
        return dict(url=url)

    def http_auth_browser_closed(self, pid):
        """A GUI-owned popup closed; no token, URL or credential comes from it."""
        with self.lock:
            item = self.pending.get(pid)
            if (item is None or item.get('kind') != 'http_auth_signin' or item['_ev'].is_set() or
                    item['payload'].get('state') != 'waiting'):
                raise KeyError(pid)
            item['_auth_closed']()
        return dict(ok=True)

    def ask_http_secret(self, session, project: str, summary: str, payload: dict, timeout: float) -> str | None:
        """One-shot credential entry; the value never enters Pending, state or audit.

        Only the GUI decision endpoint supplies it to this local waiting stack.
        Session/global grants cannot supply a passphrase or suppress this prompt.
        """
        if session and session.id.startswith('provider-') and getattr(session, '_provider_active', True) is False:
            return None
        pid, ev, value = secrets.token_hex(4), threading.Event(), []
        def accept(secret):
            if not isinstance(secret, str) or not secret or '\0' in secret or len(secret.encode('utf-8')) > 8192:
                raise ValueError('enter a certificate passphrase (at most 8 KiB)')
            value.append(secret)
        title = session.title if session else project + ' · your run'
        sid = session.id if session else ''
        item = dict(id=pid, kind='http_cert_passphrase', session=sid, title=title, summary=common.show_bidi(summary),
                    payload=common.show_bidi(payload), created=time.time(), _ev=ev, _ok=False, _secret_sink=accept)
        with self.lock:
            self.pending[pid] = item
        self.bump()
        try:
            ev.wait(timeout)
            with self.lock:
                self.pending.pop(pid, None)
                answered = ev.is_set() and not item.get('_cancelled')
                accepted = answered and item['_ok'] and bool(value)
                if not accepted:
                    now = time.time()
                    self.pending_gone = [g for g in getattr(self, 'pending_gone', []) if now - g['at'] < GONE_S][-20:] + [
                        dict(id=pid, kind='http_cert_passphrase', session=sid, title=title, at=now,
                             note=('cancelled' if item.get('_cancelled') else 'timed out' if not answered else 'declined') +
                                  ' · certificate passphrase not given; no requests of this run were sent')]
            audit(dict(event='confirm', kind='http_cert_passphrase', session=sid, approved=bool(accepted), answered=bool(answered)))
            return value[0] if accepted else None
        finally:
            value.clear()
            item.pop('_secret_sink', None)
            self.bump()

    def decide(self, pid: str, ok: bool, always: bool = False, session_always: bool = False, secret: str | None = None,
               similar_hour: bool = False) -> None:
        """Answer once; persist a session grant before waking the action's waiting thread.

        `always` keeps the caller's existing content/project grant. `session_always` is
        independent: it covers only this session, action kind and environment class.
        """
        with self.lock:   # the first answer wins (N4: a later Allow from another tab overwrote a Deny)
            item = self.pending.get(pid)
            if item is None or item["_ev"].is_set():
                raise KeyError(pid)
            if item.get('kind') == 'http_auth_signin' and ok:
                raise ValueError('finish the browser sign-in; it cannot be approved as a permission')
            if item.get('kind') == 'http_cert_passphrase':
                if ok:
                    item['_secret_sink'](secret)
            elif secret is not None:
                raise ValueError('this approval does not accept a credential')
            keep_hour = bool(ok and similar_hour and item.get("_match"))
            keep_session = bool(ok and session_always and item.get("session_always") and not keep_hour)
            if keep_hour:
                m = item["_match"]
                now = time.time()
                grants = [g for g in read_json(APPROVAL_GRANTS_FILE, []) if g.get("expires_at", 0) > now and g.get("scope") != m["scope"]]
                grants.append(dict(m, expires_at=now + 3600))
                write_json(APPROVAL_GRANTS_FILE, grants)
            item["_similar_hour"] = keep_hour
            if keep_session:
                s = self.get(item["session"])
                scope = item["_scope"]
                if scope not in s.approvals:
                    s.approvals.append(scope)
                    s.approvals.sort()
                    try:
                        s.save()
                    except Exception:
                        s.approvals.remove(scope)
                        raise
            item["_session_always"] = keep_session
            item["_always"] = bool(always and item.get("always") and not keep_session and not keep_hour)
            item["_ok"] = bool(ok) or item["_always"] or keep_session
            if item["_ok"] and item.get("_match"):
                now = time.time()
                history = [r for r in read_json(APPROVAL_HISTORY_FILE, []) if r.get("decided_at", 0) >= now - evidence.WEEK]
                history.append({"event": "confirm", "approved": True, "answered": True,
                                "matching_scope": item["_match"]["scope"], "decided_at": now})
                try:
                    write_json(APPROVAL_HISTORY_FILE, history)
                except OSError:
                    # A suggestion counter cannot invalidate a grant already persisted above.
                    item["_history_unavailable"] = True
            item["_ev"].set()

    def pending_gone_list(self, include_providers: bool = False) -> list[dict]:
        """F17: the cards that went unanswered in the last GONE_S seconds (newest last)."""
        now = time.time()
        with self.lock:
            return [dict(g) for g in getattr(self, "pending_gone", []) if now - g["at"] < GONE_S
                    and (include_providers or not str(g.get("session", "")).startswith("provider-"))]

    def pending_list(self, include_providers: bool = False) -> list[dict]:
        """Public cards; notebook relay cards are projected once onto their remote owner."""
        with self.lock:
            return [{k: v for k, v in p.items() if not k.startswith("_")} for p in self.pending.values()
                    if include_providers or not str(p.get("session", "")).startswith("provider-")]

    # ------------------------------------------------------------ polling: hook events + mailbox
    def _poll_loop(self) -> None:
        busy = self._mail_busy
        while not self._stop_event.is_set():
            try:
                self._quiet_tick()
            except Exception as e:
                print("piperd: quiet coordinator: %s" % type(e).__name__, flush=True)
            for s in list(self.sessions.values()):
                if s.state not in ACTIVE:
                    continue
                try:
                    self._stop_nudge(s)
                    self._events(s)
                    self._sync_title(s)
                    self._background(s)
                    if s.revert_note and not s.turn_busy and self._istate_at_prompt(s):
                        self.send(s.id, s.revert_note, idle_only=True)
                    self._desktop_notify(s)
                    self._mailbox(s, busy)
                    if s.kind == "advisor":
                        self._chat_flush(s)   # 9.14: queued chat messages, same thread as the handoff line
                    if s.hold and time.time() - s.hold_try > 2:
                        s.hold_try = time.time()
                        # the badge first (it clears once the prompt is empty, also while Claude works),
                        # then the handoff line that waited for it
                        if self.auto_type_ok(s) and s.handoff_notify:
                            self._flush_handoff_notes(s)
                except Exception as e:  # noqa: BLE001
                    print("piperd: poll %s: %s" % (s.id, e), flush=True)
            self._stop_event.wait(0.4)

    def _sync_title(self, s: Session) -> None:
        """Follow explicit CLI renames, incrementally and only for this native conversation.

        Remember the observed name across restarts so an unchanged CLI record cannot undo a
        later rename in Piper. Automatic Claude titles never replace a user/issue title.
        """
        if s.kind != "project" or not s.claude_session:
            return
        cfg = s.claude_src
        key = (cfg, s.claude_session, s.agent)
        c = self._title_cache.get(s.id)
        if not c or c["key"] != key:
            rel = "session_index.jsonl" if s.agent == "codex" else digest.transcript(cfg, s.claude_session)
            if not rel:
                return
            c = self._title_cache[s.id] = {"key": key, "rel": rel, "off": 0, "ino": None, "title": ""}
        try:
            st = common.safe_stat(cfg, c["rel"])
            if c["ino"] != st.st_ino or st.st_size < c["off"]:
                c.update(off=0, ino=st.st_ino, title="")
            raw = common.safe_read(cfg, c["rel"], 1 << 20, c["off"]) if st.st_size > c["off"] else b""
        except OSError:
            return
        cut = raw.rfind(b"\n") + 1
        if not cut and len(raw) == 1 << 20:
            c["off"] += len(raw)   # an oversized malformed record cannot stall polling
            return
        c["off"] += cut
        for line in raw[:cut].splitlines():
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(rec, dict):
                continue
            if s.agent == "codex":
                title = rec.get("thread_name") if rec.get("id") == s.claude_session else None
            else:
                title = rec.get("customTitle") if (rec.get("type") == "custom-title"
                        and rec.get("sessionId", s.claude_session) == s.claude_session
                        and not rec.get("isSidechain") and not rec.get("isMeta")) else None
            if isinstance(title, str):
                title = common.scrub(" ".join(common.no_bidi(title).split()))[:200]
                if title:
                    c["title"] = title
        if c["off"] < st.st_size or not c["title"] or c["title"] == s.native_title:
            return   # finish a partial history before publishing its latest name
        s.native_title = c["title"]
        s.title, s.title_source = s.native_title, "manual"
        s.save()
        self.bump()

    def _background(self, s: Session) -> None:
        if s.agent != "claude" or not s.claude_session or s.state not in ("ready", "working", "waiting", "attention"):
            return
        src = self._transcript_src(s)
        if not src:
            return
        key = (*src, s.background_since)
        c = self._background_cache.get(s.id)
        if not c or c["key"] != key:
            rel = digest.transcript(*src)
            if not rel:
                return
            c = self._background_cache[s.id] = {"key": key, "rel": rel, "off": 0, "ino": None,
                                               "tasks": {}, "done": set(), "stops": {}}
        try:
            st = common.safe_stat(src[0], c["rel"])
            if c["ino"] != st.st_ino or st.st_size < c["off"]:
                c.update(off=0, ino=st.st_ino, tasks={}, done=set(), stops={})
            raw = common.safe_read(src[0], c["rel"], 1 << 20, c["off"]) if st.st_size > c["off"] else b""
        except OSError:
            return
        cut = raw.rfind(b"\n") + 1
        if not cut and len(raw) == 1 << 20:
            c["off"] += len(raw)   # malformed oversized record must not stall polling
            return
        c["off"] += cut
        for line in raw[:cut].splitlines():
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get("sessionId", s.claude_session) != s.claude_session:
                    continue
                if s.background_since:
                    ts = rec.get("timestamp")
                    if not isinstance(ts, str) or datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < s.background_since:
                        continue
            except (ValueError, TypeError):
                continue
            for tid, kind in digest.background_events(rec, c["stops"]):
                if kind == "done":
                    c["tasks"].pop(tid, None)
                    if len(c["done"]) < 4096:
                        c["done"].add(tid)
                elif tid not in c["done"] and len(c["tasks"]) < 256:
                    c["tasks"][tid] = kind
        if c["off"] < st.st_size:
            return   # rebuild the index before inferring activity from a partial history
        changed = s.background_tasks != c["tasks"]
        had_tasks = bool(s.background_tasks)
        s.background_tasks = dict(c["tasks"])
        if s.idle and s.state in ("working", "waiting", "attention"):
            new = "working" if s.background_tasks or s.subagents else "waiting" if had_tasks else s.state
            if new != s.state:
                s.state, s.since, s.detail = new, time.time(), ""
                changed = True
        if changed:
            s.save()
            self.bump()

    def _events_reset(self, s: Session) -> None:
        """A new process: its hook file is read from the start again, as a replay until caught up.
        Under events_lock: a poll that is reading the old offset right now cannot move it past the reset
        (N9 - it set events_off to old + chunk, or ev_live back to True, and the replay ran as live)."""
        with s.events_lock:
            s.events_off = 0
            s.codex_permissions.clear()
            s.codex_review.clear()
            s.ev_live = False   # live once _events has caught up with the (new) hook file

    def _codex_auto_review(self, s: Session) -> bool:
        """PermissionRequest precedes automatic review too; only user review needs a human.

        Read the running conversation's actual turn context, not mutable host defaults.
        Replay is incremental and bounded like the background-task index. Missing,
        incomplete or unfamiliar records retain the conservative permission hint.
        """
        if s.runtime != "direct" or s.agent != "codex" or not s.claude_session:
            return False
        key = (s.codex_home, s.claude_session)
        c = s.codex_review
        if c.get("key") != key:
            rel = digest.session_file(*key, "codex")
            if not rel:
                return False
            c.clear()
            c.update(key=key, rel=rel, off=0, ino=None, stamp=None, own=False, reviewer="")
        try:
            st = common.safe_stat(key[0], c["rel"])
            if c["ino"] != st.st_ino or st.st_size < c["off"] or (
                    st.st_size == c["off"] and c["stamp"] != st.st_mtime_ns):
                c.update(off=0, ino=st.st_ino, own=False, reviewer="")
            raw = common.safe_read(key[0], c["rel"], 1 << 20, c["off"]) if st.st_size > c["off"] else b""
        except OSError:
            return False
        c["stamp"] = st.st_mtime_ns
        cut = raw.rfind(b"\n") + 1
        c["off"] += cut or (len(raw) if len(raw) == 1 << 20 else 0)
        for line in raw[:cut].splitlines():
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
                continue
            if rec.get("type") == "session_meta":
                c["own"] = rec["payload"].get("id") == s.claude_session
                c["reviewer"] = ""
            elif rec.get("type") == "turn_context" and c["own"]:
                mode = rec["payload"].get("approvals_reviewer")
                c["reviewer"] = mode if mode in ("user", "auto_review") else ""
        return c["off"] >= st.st_size and c["reviewer"] == "auto_review"

    def _events(self, s: Session) -> None:
        with s.events_lock:   # N9: two readers at one offset both moved it - the next lines were skipped
            self._events_read(s)

    def _events_read(self, s: Session) -> None:
        auto_review = self._codex_auto_review(s)
        if auto_review:
            s.codex_permissions.clear()
            if s.state == "attention" and not s.phase and s.detail.startswith("Codex needs permission"):
                s.state, s.since, s.detail, s.idle = "working", time.time(), "", False
                s.save()
                self.bump()
        # the file is sandbox-written: symlink-safe reads only (common.safe_*)
        try:
            size = common.safe_size(s.box, s.names["events"])
        except OSError:
            s.ev_live = True   # no hook file yet: nothing to replay, the first events are live
            return
        if size <= s.events_off:
            s.events_off = min(s.events_off, size)
            s.ev_live = True   # caught up (a replay after a piperd restart is over)
            return
        live = s.ev_live
        try:
            raw = common.safe_read(s.box, s.names["events"], EVENTS_CHUNK, s.events_off)
        except OSError:
            return
        cut = raw.rfind(b"\n") + 1   # a line cut by the chunk end (or still being written) waits for the next poll
        if cut or len(raw) < EVENTS_CHUNK:
            raw = raw[:cut]
        s.events_off += len(raw)   # (a full chunk without any newline is skipped, never a stall)
        chunk = raw.decode("utf-8", "replace")
        compacted = False
        for line in chunk.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            ts = e.get("ts")
            if live and not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)
                             and abs(time.time() - ts) <= workbench.EVENT_MAX_AGE):
                # written just now, whatever the sandbox's clock says (Docker's VM drifts after a sleep):
                # otherwise every event would look like a replay and the prompt badge would never clear
                e["ts"] = time.time()
            # untrusted input: only a state hint, never an action
            ev = str(e.get("ev", ""))
            if ev in ("SubagentStart", "SubagentStop"):
                aid = e.get("agent_id")
                if not isinstance(aid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", aid) or \
                        (s.claude_session and e.get("session_id") != s.claude_session):
                    continue
                new = None
                if ev == "SubagentStart":
                    if aid not in s.subagents and len(s.subagents) >= 256:
                        continue
                    kind = e.get("agent_type")
                    s.subagents[aid] = kind[:80] if isinstance(kind, str) else "agent"
                    if s.state == "ready" or s.idle and s.state in ("waiting", "attention"):
                        new = "working"
                elif aid in s.subagents:
                    del s.subagents[aid]
                    # A child finishing cannot turn an active parent or a permission prompt into idle.
                    if not s.subagents and not s.background_tasks and s.idle and s.state in ("working", "waiting", "attention"):
                        new = "waiting"
                if new and new != s.state:
                    s.state, s.since, s.detail = new, time.time(), ""
                continue
            if s.agent == "codex" and ev in ("SessionStart", "UserPromptSubmit", "Stop"):
                s.codex_permissions.clear()
            if ev == "PermissionRequest":   # Codex has no Notification hook: its permission prompt is one
                if auto_review:
                    continue   # Hook runs before Guardian too, without any human approval dialog.
                if s.agent == "codex":
                    key = e.get("tool_key")
                    key = key if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) else "legacy"
                    # Identical parallel calls need one completion each. Uncorrelatable
                    # older scripts and overflow stay blocked until the next lifecycle hook.
                    if sum(s.codex_permissions.values()) >= 256:
                        s.codex_permissions["overflow"] = 1
                    else:
                        s.codex_permissions[key] = s.codex_permissions.get(key, 0) + 1
                ev = e["ev"] = "Notification"
                e["message"] = "Codex needs permission" + (": %s" % e["tool_name"] if isinstance(e.get("tool_name"), str) else "")
            if s.agent == "codex" and ev == "PostToolUse":
                key = e.get("tool_key")
                if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key) and key in s.codex_permissions:
                    s.codex_permissions[key] -= 1
                    if not s.codex_permissions[key]:
                        del s.codex_permissions[key]
                    if not s.codex_permissions and s.state == "attention" and not s.phase and \
                            s.detail.startswith("Codex needs permission"):
                        s.state, s.since, s.detail, s.idle = "working", time.time(), "", False
            if ev in ("Notification", "Stop"):
                ts = e.get("ts") if isinstance(e.get("ts"), (int, float)) else time.time()
                if ev == "Stop":
                    s.stop_ts = float(ts)
                else:
                    s.notif_ts = float(ts)
            e.pop("_human", None)   # piperd's own mark below, never taken from the sandbox-written line
            if ev == "UserPromptSubmit" or (ev == "SessionStart" and e.get("source") == "clear"):
                human = self._note_submit(s, e)   # the prompt is empty again (a compaction is no submit)
                if ev == "UserPromptSubmit":
                    e["_human"] = human
            if ev == "SessionStart" and e.get("source") == "compact" and s.compact_wait and live:
                compacted = True   # W8: the task waits for this (after the loop: the state lines come first)
            # Native Codex may run SessionStart before the user reviews new hooks.
            # The first later prompt/Stop hook can identify that same conversation;
            # never replace an already bound thread from these fallback events.
            bind_session = ev == "SessionStart" or (s.agent == "codex" and not s.claude_session
                                                     and ev in ("UserPromptSubmit", "Stop"))
            if bind_session and isinstance(e.get("session_id"), str) and \
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{7,63}", e["session_id"]):   # argv later: no leading "-"
                s.claude_session = e["session_id"]
                auto_review = self._codex_auto_review(s)
                s.fork_pending = e.get("source") != "clear" and self._fork_unwritten(s)
                if e.get("source") != "clear" and isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts):
                    # Match the transcript's sandbox clock, even when its hook timestamp needed host correction.
                    s.background_since = float(ts)
            if ev in ("Stop", "UserPromptSubmit", "Notification"):
                # a permission prompt is not idle: nothing may be typed into it
                # a resumed session sits in "ready" with no Stop yet: its idle_prompt is idle too
                s.idle = ev == "Stop" or (ev == "Notification" and (s.idle or s.state == "ready")
                                          and e.get("notification_type") == "idle_prompt")
            if ev == "PostToolUse" and s.kind == "project":
                try:   # a hint for the GUI - never at the cost of the lines after it (a Stop)
                    self._fsedit_event(s, e)
                except Exception as x:  # noqa: BLE001
                    print("piperd: fsedit %s: %s" % (s.id, x), flush=True)
            if s.runtime == "direct" and s.state in ("spawning", "starting") and ev in ("SessionStart", "UserPromptSubmit", "Stop", "Notification"):
                s.state, s.phase, s.detail = "ready", "", ""
            if s.state in ("ready", "working", "waiting", "attention"):
                new = {"UserPromptSubmit": "working", "Stop": "waiting", "Notification": "attention"}.get(ev)
                if (s.subagents or s.background_tasks) and (ev == "Stop" or ev == "Notification" and e.get("notification_type") == "idle_prompt"):
                    new = "working"
                if new:
                    if new != s.state:
                        s.since = time.time()
                    s.state = new
                    if s.runtime == "direct" and s.phase in ("agent", "agent-auth") and new != "attention":
                        s.phase = ""
                    # sandbox-written, shown to the human: bidi controls visible as on a card (N4)
                    s.detail = common.show_bidi(str(e.get("message") or ""))[:200] if new == "attention" else ""
                if ev in ("UserPromptSubmit", "Stop") and s.kind == "project":
                    self._turn_event(s, ev, e)   # a snapshot in a worker thread, never here
                if ev == "Stop" and s.issue and isinstance(e.get("ts"), (int, float)) and \
                        abs(time.time() - e["ts"]) <= workbench.EVENT_MAX_AGE:   # a replay after a piperd restart: no refresh per old turn
                    self.issue_state_refresh_soon(s)   # 9.12 D: piperd's facts block, worker thread
                if ev == "UserPromptSubmit":
                    if e.get("_human") and live:   # W10: the issue the human names becomes the session's
                        try:
                            self.issue_from_prompt(s, str(e.get("prompt") or ""))
                        except Exception as x:  # noqa: BLE001 - never at the cost of the lines after it
                            print("piperd: issue link %s: %s" % (s.id, x), flush=True)
                    self.ts_prompt_event(s, e, e.get("_human"))   # 9.12 A: when the user worked on what
        if chunk:
            # a /login (or a refreshed token) serves the next start right away, not only after this
            # session ends - a second session started meanwhile would ask for a login again
            try:
                if self._login_copy_back(s):
                    s.notice = "login saved to the template"
            except OSError:
                pass
        s.save()
        self.bump()
        if compacted:
            self._compact_done(s)
        self._flush_handoff_notes(s)

    def _mailbox(self, s: Session, busy: set[str]) -> None:
        with self.lock:
            box = s.names["mailbox"] + "/out"
            try:
                names = [n for n in common.safe_listdir(s.box, box) if n.endswith(".json") and not n.startswith(".")
                         and s.id + "/" + n not in busy]
            except OSError:
                return
            for n in names[:8]:
                key = s.id + "/" + n
                if key in busy:
                    continue
                rel = box + "/" + n
                try:
                    raw = common.safe_read(s.box, rel, (64 << 10) + 1)
                    if len(raw) > 64 << 10:
                        raise ValueError("oversized mailbox request")
                    req = json.loads(raw.decode("utf-8", "replace"))
                    if not isinstance(req, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(req.get("id", ""))) or req["id"] + ".json" != n:
                        raise ValueError("invalid mailbox request ID")
                    if str(req.get("tool", "")) in REPLAYABLE_READS:
                        # Retain idempotent reads until the reply exists, so a daemon restart can finish them.
                        try:
                            reply = "%s/in/%s" % (s.names["mailbox"], n)
                            st = common.safe_stat(s.box, reply)
                        except FileNotFoundError:
                            pass
                        else:
                            # An in-place reply starts empty. Its final newline marks a complete write;
                            # after a crash an empty/partial reservation must not suppress a safe read.
                            if req.get("reply_in_place") is not True or st.st_size and common.safe_read(
                                    s.box, reply, 1, st.st_size - 1) == b"\n":
                                common.safe_unlink(s.box, rel)
                                continue
                    else:
                        common.safe_unlink(s.box, rel)   # a write must never replay after losing its worker
                except (OSError, ValueError):
                    try:
                        common.safe_unlink(s.box, rel)
                    except OSError:
                        pass
                    continue
                busy.add(key)
                threading.Thread(target=self._serve_tool, args=(s, req, busy, key), daemon=True).start()

    def _serve_tool(self, s: Session, req: dict, busy: set, key: str) -> None:
        tool, args = str(req.get("tool", "")), req.get("args") or {}
        try:
            if not isinstance(args, dict):
                raise ValueError("args must be an object")
            self._db_req.t0 = time.time()   # dbtools: the run after a card must fit the sandbox's deadline (2b)
            result = self.tool(s, tool, args)
            resp = {"id": req["id"], "ok": True, "result": result}
        except (KeyError, ValueError, TypeError, OSError, gitlab.GitLabError, gitops.GitError, PtyHostError) as e:
            resp = {"id": req["id"], "ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - the sandbox must get an answer, not a 90 s timeout
            resp = {"id": req["id"], "ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        try:
            audit({"event": "tool", "session": s.id, "tool": tool, "ok": resp["ok"],
                   "error": resp.get("error")})
            def pin_reply(_dir_fd, fd):
                if fd is None or os.fstat(fd).st_nlink != 1:
                    raise common.NotPinned("mailbox reply must be a reserved file without hard links")
            common.safe_write(s.box, "%s/in/%s.json" % (s.names["mailbox"], req["id"]),
                              (json.dumps(resp, ensure_ascii=False) + "\n").encode(),
                              in_place=req.get("reply_in_place") is True,
                              pin=pin_reply if req.get("reply_in_place") is True else None)
            if tool in REPLAYABLE_READS:
                common.safe_unlink(s.box, "%s/out/%s.json" % (s.names["mailbox"], req["id"]))
        except (OSError, common.NotPinned) as e:
            audit({"event": "tool-reply-refused", "session": s.id, "error": str(e)})
            try:   # an unwritable reply (in/ made read-only, a full disk) must not replay the read every poll
                common.safe_unlink(s.box, "%s/out/%s.json" % (s.names["mailbox"], req["id"]))
            except OSError:
                pass
        finally:
            busy.discard(key)

    @quietwindow.managed("tool")
    def tool(self, s: Session, tool: str, args: dict):
        if tool.startswith("quiet_"):
            return self.quiet_tool(s, tool, args)
        if tool == "piper_session" and "quiet" in args:
            return self.settings_tool(s, tool, args)
        if self.policy.get("security_strict") and (s.runtime == "direct" or getattr(s, "security_sandbox_rev", 0) != security.REV):
            raise ValueError("Strict requires a new, successfully verified Hole session; this session retains its old sandbox")
        if self.policy.get("security_strict") and tool == "piper_allow_domain":
            raise ValueError("Strict does not let agents expand the network allow-list; change it in Settings")
        if s.kind == "prodcheck" or tool == "report_production":   # 9.12 C: nothing else, and only there
            return self.prod_tool(s, tool, args)
        if tool == "local_provider":
            import providers
            return providers.forward(s, args)
        if tool.startswith("git_"):
            if s.kind == "advisor":
                raise ValueError("the advisor has no project repository")
            if tool == "git_run" and not self.unlocked(s, "git") and not self._git_hole():   # before anything else
                raise ValueError(NOT_UNLOCKED % ("Full git (git_run)", "Git"))
            top = self._turn_top(s)   # the trusted root; git never discovers one in the project dir
            hidden = self.hidden_for(s)
            if tool == "git_status":
                return gitops.session_status(top, hidden)
            if tool == "git_diff":
                f = args.get("path")
                return gitops.session_diff(s.path, hidden, list(self.policy["candidates"]) + list(s.excludes or []),
                                           str(f) if f else None, top=top)
            if tool == "git_log":
                return gitops.session_log(top, gitlab._whole(args.get("n") or 20, "n"))
            if tool == "git_run":
                other = str(args.get("project") or "")
                library = library_card = read_only = False
                if other and other != s.project and not self.policy.get("git_run_projects") and not any(
                        c.get("kind") == "library" and c.get("readwrite") is True for c in s.coverage):
                    raise ValueError("git_run project= is off (policy git_run_projects)")
                if other and other != s.project:
                    p = projects.find(other, PROJECT_PATH)
                    if p is None or not p["exists"]:
                        raise ValueError("unknown project %r - see list_projects" % other)
                    real = os.path.realpath(p["path"])
                    argv = gitops.check_run_args(args.get("args"))
                    allowed = set(gitops.AUTO_SUBS)
                    if self.policy.get("git_run_readonly_extra"):
                        allowed.update(gitops.READONLY_SUBS)
                    sub, rest = gitops._sub(argv)
                    read_only = (sub in ({"status", "log", "diff", "show"} | ((allowed & gitops.READONLY_SUBS) -
                                 {"archive", "worktree", "symbolic-ref"})) or
                                 sub == "branch" and rest == ["--show-current"] or
                                 sub == "worktree" and rest[:1] == ["list"]) and not any(
                                     a.startswith(("-o", "--output")) for a in rest if sub != "grep") and not (
                                     # the writing forms auto_blockers cards too: not a read here (merge review)
                                     sub == "reflog" and rest[:1] and rest[0] in ("expire", "delete", "drop", "write") or
                                     sub == "remote" and [a for a in rest if not a.startswith("-")][:1] not in ([], ["get-url"]))
                    library = sub in allowed and self._library_access(s, real, write=True)
                    # Write access grants normal maintenance; forced discards/ref rewrites keep their card.
                    library_card = sub in ("branch", "checkout", "switch", "fetch", "pull", "push", "tag") and any(
                        a.split("=", 1)[0] in ("--force", "--force-with-lease", "--force-if-includes", "--force-create", "--discard-changes", "--delete") or
                        a.startswith("-") and not a.startswith(("--", "-b", "-c")) and bool(re.search(r"[fDCBMd]", a[1:])) for a in rest)
                    library_card = library_card or sub == "commit" and "--amend" in rest
                    library_card = library_card or gitops._W_DISCARD in gitops.run_warnings(argv, lambda p: False)
                    if not library and not self.policy.get("git_run_projects"):
                        raise ValueError("git_run project= is off (policy git_run_projects)")
                    if not library and not self.unlocked(s, "projects_write"):
                        raise ValueError(NOT_UNLOCKED % ("Editing other projects (git_run project=)", "Editing other projects"))
                    if self.policy.get("write_guard") and any(
                            real == r or real.startswith(r + os.sep) or r.startswith(real + os.sep) for r in workbench.INFRA_ROOTS):
                        raise ValueError("%s holds the scripts the host runs at every sandbox start - git_run never "
                                         "touches it" % other)
                    top = self.repo_top(p["path"])   # the pinned root only, never a planted .git
                    if top is None:
                        raise gitops.GitError("no trusted repository for %s: %s" % (other, self.repo_top_why(p["path"])))
                    if library and not self._library_access(s, top, write=True):
                        raise ValueError("the repository root is outside the writable library")
                    hidden = self.project_hidden(p)
                    if not self._library_access(s, real):   # N3: not mounted - this sandbox never reads that .git
                        hidden.unmounted = True   # (K1 is not about it): object content reads go on a card (_git_run)
                # Mounted write authority covers normal library maintenance; the host git checks still apply.
                return self._git_run(s, top, hidden, args.get("args"), card=bool(other and other != s.project and
                                     (isinstance(self.policy.get("ask"), list) or not library and not read_only or library_card)), project=other,
                                     read_only=read_only)
            if tool == "git_commit":   # the host commits the approved PATHS (their content may still change)
                msg, files = str(args.get("message") or "").strip(), args.get("files")
                if not isinstance(files, list) or not files or not all(isinstance(f, str) and f for f in files):
                    raise ValueError("files must be a non-empty list of paths from git_status")
                bad = [f for f in files if hidden(f)]   # the rule git_status/git_diff apply
                if bad:
                    raise ValueError("hidden files cannot be committed from a session: %s" % ", ".join(bad[:5]))
                st = gitops.session_status(top, hidden)
                unknown = [f for f in files if f not in {x["path"] for x in st["files"]}]
                if unknown:
                    raise ValueError("not in git_status: %s" % ", ".join(unknown[:5]))
                summary = ("Commit on branch %s (%d files). You approve these paths - the content is whatever "
                           "they hold at the moment of the commit:\n\n%s\n\n%s") % (
                    st["branch"], len(files), msg, "\n".join(files))
                if not self.ask(tool, s, summary, {"files": files}):
                    raise ValueError("the user declined the commit")
                return self.git_commit(s.id, msg, files, agent=True)
            if tool == "git_push":
                gitops.refuse_exec_config(top)
                self._refuse_protected_push(top, ["push"])
                gone = self.hidden_deletions(s, top)
                if gone:   # never offered: the card would make removing them one click away
                    raise ValueError(self._hidden_push_msg(top, gone))
                st = gitops.status(top)
                summary = "Push of branch %s → %s (%d commits ahead):\n\n%s" % (
                    st["branch"], st["upstream"] or "origin (new branch)", st["ahead"],
                    "\n".join(gitops.log(top, max(1, min(st["ahead"] or 5, 20)))))
                plan = gitops.push_plan(top)
                if not self.ask(tool, s, summary, {"branch": st["branch"], "remote": plan["remote"],
                                                  "push_new": plan["new"]}):
                    raise ValueError("the user declined the push")
                return self.git_push(s.id)
            raise ValueError("unknown tool %r" % tool)
        if tool.startswith("gitlab_") and not features.on("gitlab"):
            raise ValueError("GitLab module is off (Settings -> Features)")
        if tool == "gitlab_inbox":   # 9.8: the poller's items, only of this session's GitLab projects
            return self.inbox_tool(s, args)
        if tool.startswith("gitlab_"):
            t0 = time.time()   # a read card may come first: the second card gets what is left of the MCP's wait
            scope = list(s.gitlab or [])
            gp = str(args.get("project") or (scope[0] if len(scope) == 1 else ""))
            if tool in ("gitlab_mr_merge", "gitlab_job_play") and not self.policy.get("gitlab_extra_tools"):
                raise ValueError("%s is off (policy gitlab_extra_tools)" % tool)
            if tool in gitlab.WRITE_TOOLS:
                # the projects the user already let this session read (their card, or "Allow always") take
                # writes too - each one still on its own card; any other project: never a card that can only fail
                if not args.get("project"):   # the session's own project stays the default, never a borrowed one
                    if not gp:
                        raise ValueError("project is required for %s (one of: %s)" % (tool, ", ".join(
                            scope + [x for x in self._gitlab_extra_scope(s) + self.gitlab_read_always() if x not in scope]) or "none"))
                    args = dict(args, project=gp)
                scope += [x for x in self._gitlab_extra_scope(s) + self.gitlab_read_always() if x not in scope]
                if gp and gp not in scope:
                    raise ValueError("project %r is outside this session's scope (%s) - a session writes only "
                                     "in its own GitLab project(s) and in those it was allowed to read"
                                     % (gp, ", ".join(scope) or "none"))
            elif tool in gitlab.TOOLS and gp and gp not in scope:   # reading another GitLab project: one card per project, then for the session's life
                scope.append(self._gitlab_read_extra(s, tool, gp, args))
            gl = gitlab.GitLab(self.gitlab_host(), gitlab.token())
            cands = self.policy["candidates"]
            # the target project as gitlab.call resolves it; what its local checkout(s) hide
            # applies on top (a session's scope may hold another repository's project)
            mapped = self._ib_map()[0].get(gp)
            rules = [self._checkout_rule(p) for k, p in projects.load(PROJECT_PATH).items()
                     if gp and p["exists"] and (gp in (p["gitlab"] or []) or k == mapped
                                               or self._ib_origin_gl(p["path"], False) == gp)]
            # a project session: its own checkout's rule, scoped to it (a path elsewhere in the
            # repository is not "outside the project" here, just another checkout's)
            own = (lambda p: guard.hidden_by(cands, p)) if s.kind == "advisor" else self._git_sensitive(s)
            if tool in gitlab.CARD_TOOLS:   # validated and looked up first, then the card shows what is sent
                upload = None
                if tool == "gitlab_upload_file":
                    if s.kind != "project":
                        raise ValueError("upload a file from a project session")
                    top = self._turn_top(s)
                    hidden = self.hidden_for(s)
                    path = str(args.get("path") or "")
                    if not path.strip():
                        raise ValueError("a project-relative file path is required")
                    projectfs._resolve(s.path, path, lambda p: hidden(os.path.relpath(os.path.join(s.path, p), top)))
                    # Snapshot the approved bytes once; the sandbox cannot replace them while the card waits.
                    raw = common.safe_read(os.path.realpath(s.path), path.strip(), gitlab.UPLOAD_MAX + 1)
                    upload = (os.path.basename(path.strip()), raw)
                if tool == "gitlab_mr_create" and not args.get("source_branch") and s.kind == "project":
                    args = dict(args, source_branch=gitops.status(self._turn_top(s))["branch"])
                p = gitlab.plan(gl, tool, args, scope, **({"upload": upload} if upload is not None else {}))
                self._refuse_protected_mr(tool, p)
                kind = "gitlab_note_edit" if tool == "gitlab_comment" and args.get("note_id") else gitlab.CARD_TOOLS[tool]
                if tool == "gitlab_job_play" and p.get("ref") in (self.policy.get("ci_protected_branches", self.policy.get("protected_branches")) or []):
                    kind = "gitlab_job_protected"   # a job on a protected branch (a deploy, a release): always a card
                if not self.ask(kind, s, gitlab.card_text(p),
                                {"project": p["project"], "tool": tool, "iid": args.get("iid")},
                                timeout=max(30.0, 780 - (time.time() - t0))):   # + up to 60 s of trace, < the MCP's 900
                    raise ValueError("the user declined %s" % tool)
                return gitlab.execute(gl, p)
            out = gitlab.call(gl, tool, args, scope, lambda p: own(p) or any(r(p) for r in rules))
            if tool == "gitlab_upload":   # the file goes to the session's drop dir, the sandbox reads it there
                drop = os.path.join(s.box, s.names["drop"])
                used = sum(os.lstat(os.path.join(drop, n)).st_size
                           for n in common.safe_listdir(s.box, s.names["drop"]) if n.startswith("gitlab-"))
                if used + out["size"] > DROP_GITLAB_MAX:
                    raise ValueError("the session's GitLab downloads take %d MiB already (limit %d MiB): delete "
                                     "old gitlab-* files in %s/%s first" % (used >> 20, DROP_GITLAB_MAX >> 20,
                                                                            self.box_in(s), s.names["drop"]))
                ext = re.sub(r"[^A-Za-z0-9]", "", os.path.splitext(out["filename"])[1])[:10]
                name = "gitlab-%s%s" % (secrets.token_hex(6), "." + ext if ext else "")
                common.safe_write(s.box, s.names["drop"] + "/" + name, out.pop("_bytes"))
                out["path"] = "%s/%s/%s" % (self.box_in(s), s.names["drop"], name)
                out["hint"] = "read the file at `path` (an image shows as an image)"
            return out
        if tool.startswith("github_") and not features.on("github"):
            raise ValueError("the GitHub module is off (Settings -> Modules)")
        if tool == "github_inbox":   # W23: the poller's GitHub items of this session's repositories
            return self.inbox_tool(s, args, forge="github")
        if tool.startswith("github_"):
            t0 = time.time()
            scope = self.gh_scope(s)
            entry = github._target(args, scope)   # within the session's repositories, never another
            host, repo = github.split(entry)
            if host not in self.github_hosts():
                raise ValueError("GitHub host %r is not in policy github_hosts" % host)
            gh = github.GitHub(host, github.token(host), opener=getattr(self, "github_opener", None))
            args = dict(args, repo=entry)
            if tool in github.CARD_TOOLS:
                if tool == "github_pr_create" and not args.get("source_branch") and s.kind == "project":
                    args = dict(args, source_branch=gitops.status(self._turn_top(s))["branch"])
                p = github.plan(gh, tool, args, [entry])
                self._refuse_protected_mr(tool, p)
                kind = "github_note_edit" if tool == "github_comment" and args.get("comment_id") else github.CARD_TOOLS[tool]
                if not self.ask(kind, s, github.card_text(p),
                                {"project": entry, "tool": tool, "iid": args.get("number"), "forge": "github"},
                                timeout=max(30.0, 780 - (time.time() - t0))):
                    raise ValueError("the user declined %s" % tool)
                return github.execute(gh, p)
            cands = self.policy["candidates"]
            rules = [self._checkout_rule(p) for p in projects.load(PROJECT_PATH).values()
                     if p["exists"] and entry in self._gh_projects(p)]
            own = (lambda p: guard.hidden_by(cands, p)) if s.kind == "advisor" else self._git_sensitive(s)
            return github.call(gh, tool, args, [entry], lambda p: own(p) or any(r(p) for r in rules))
        if tool in ("propose_session", "report_result", "handoff_result"):
            return self.handoff_tool(s, tool, args)
        if tool in propertyfiles.TOOLS:
            return propertyfiles.tool(self, s, tool, args)
        if tool in HTTP_TOOLS:
            return self.http_tool(s, tool, args)
        if tool in DB_TOOLS:
            return self.db_tool(s, tool, args)
        if tool in SETTINGS_TOOLS:
            return self.settings_tool(s, tool, args)
        if tool in PROJECT_WRITE_TOOLS and s.kind == "project":
            key, path = str(args.get("project", "")), str(args.get("path", ""))
            p = projects.find(key, PROJECT_PATH) if key else None
            library = bool(p and p["exists"] and self._library_access(s, p["path"], write=True))
            if not library and not self.unlocked(s, "projects_write"):
                raise ValueError(NOT_UNLOCKED % ("Editing other projects (%s)" % tool, "Editing other projects"))
            def confirm(summary, payload):
                flagged = payload.get("flagged") and self.policy.get("write_guard")
                if library and not flagged:
                    audit({"event": "confirm", "kind": tool, "session": s.id, "project": key,
                           "approved": True, "auto": True, "why": "mounted writable library"})
                    return True
                return self.ask("write_exec" if flagged else tool, s, summary, payload, timeout=UNLOCKED_ASK_S)
            if tool == "write_file":
                if not isinstance(args.get("content"), str):
                    raise ValueError("content must be a string")
                return self.agent_write(s, key, path, content=args["content"], confirm=confirm)
            if not isinstance(args.get("old_string"), str) or not isinstance(args.get("new_string"), str):
                raise ValueError("old_string and new_string must be strings")
            return self.agent_write(s, key, path, old=args["old_string"], new=args["new_string"],
                                    replace_all=args.get("replace_all") is True, confirm=confirm)
        if tool in ("list_dir", "read_file", "grep"):
            p = projects.find(str(args.get("project", "")), PROJECT_PATH)
            if p is None or not p["exists"]:
                raise ValueError("unknown project %r: project must be a registered key from list_projects "
                                 "(e.g. client/repo), not a filesystem path. Read/search your own mounted "
                                 "project locally with the agent's file reader or shell (cat / rg); "
                                 "no Piper unlock is needed for local reads." % args.get("project"))
        if s.kind != "advisor":
            if tool not in PROJECT_READ_TOOLS or s.kind != "project":
                raise ValueError("tool %r is only available to the advisor" % tool)
            if not self.unlocked(s, "projects"):
                if tool == "list_projects" and any(c.get("kind") == "library" for c in s.coverage):
                    return [{"key": k, "title": p["title"], "gitlab": p["gitlab"]} for k, p in projects.load(PROJECT_PATH).items()
                            if p["exists"] and self._library_access(s, p["path"])]
                p = projects.find(str(args.get("project", "")), PROJECT_PATH)
                if p and p["exists"] and os.path.realpath(p["path"]) == os.path.realpath(s.path):
                    raise ValueError("This is your own mounted project. Read/search it locally with the "
                                     "agent's file reader or shell (cat / rg); no Piper unlock is needed. "
                                     "These Piper tools are for other checkouts.")
                if p is None or not p["exists"] or not self._library_access(s, p["path"]):
                    raise ValueError(NOT_UNLOCKED % ("Reading other projects (%s)" % tool, "Reading other projects"))
        if tool in ("list_dir", "read_file", "grep"):
            p = projects.find(str(args.get("project", "")), PROJECT_PATH)
            if p is None or not p["exists"]:
                raise ValueError("unknown project %r - see list_projects" % args.get("project"))
            hidden = self.project_hidden(p)
            if tool == "list_dir":
                return projectfs.list_dir(p["path"], str(args.get("path") or "."), hidden)
            if tool == "read_file":
                return projectfs.read_file(p["path"], str(args.get("path") or ""), hidden,
                                           gitlab._whole(args.get("offset") or 0, "offset"), gitlab._whole(args.get("limit") or 2000, "limit"))
            return projectfs.grep(p["path"], str(args.get("pattern") or ""), hidden,
                                  str(args.get("path") or "."), args.get("glob") or None)
        if tool == "list_projects":
            return [{"key": k, "title": p["title"], "gitlab": p["gitlab"]} for k, p in projects.load(PROJECT_PATH).items()
                    if p["exists"]]
        if tool == "spawn_session":   # a card (review 2026-10-05: a new session + its card-free git_run would
            # act in any project without a human step); Settings -> Approvals can turn it off
            # bidi controls dropped: the card must show the task in the order it is sent (E25)
            key, task = str(args.get("project", "")), common.no_bidi(str(args.get("task", "")))
            title = " ".join(common.no_bidi(str(args.get("title", ""))).split())
            p = projects.find(key, PROJECT_PATH)
            if p is None or not p["exists"]:
                raise ValueError("unknown project %r" % key)
            if len(task) > 20000 or len(title) > 200:
                raise ValueError("task/title too long")
            self._check_limit()   # before the card: never ask for a session that cannot start (create checks again)
            summary = "Global agent wants to start a session in project %s%s. The task as it will receive it:\n\n%s" % (
                key, " (\"%s\")" % title if title else "", task)
            if not self.ask(tool, s, summary, {"project": key}):
                raise ValueError("the user declined the new session")
            n = self._create_allowed(s, tool, key, task, title, origin="advisor")
            return {"id": n.id, "state": n.state}
        if tool == "list_sessions":
            return [{k: x[k] for k in ("id", "project", "title", "state", "detail", "origin")}
                    for x in self.list() if x["kind"] != "advisor"]
        tid = str(args.get("id") or "")
        if not tid:
            raise ValueError("id is required: a session id from list_sessions")
        try:
            target = self.get(tid)
        except KeyError:
            raise ValueError("unknown session %r - see list_sessions" % tid) from None
        if target.kind == "advisor":
            raise ValueError("the advisor cannot act on itself")
        if tool == "session_status":
            return {k: target.to_json()[k] for k in ("id", "project", "title", "state", "phase", "detail", "rc")}
        if tool == "session_digest":
            src = self._transcript_src(target)
            return digest.digest(src[0], src[1], gitlab._whole(args.get("turns") or 6, "turns")) if src else []
        if tool == "send_to_session":
            text = common.no_bidi(str(args.get("text", "")))   # the card shows what is typed, in order (E25)
            if not text or len(text) > 20000:
                raise ValueError("text must be 1..20000 characters")
            if target.state not in ("ready", "working", "waiting", "attention"):
                raise ValueError("session %s is %s" % (target.id, target.state))
            if self._dialog_up(target):   # Enter would answer it (folder trust: "No, exit")
                raise ValueError("session %s shows a dialog - the user answers it first, try again later" % target.id)
            if not self.auto_type_ok(target):   # before the card too: the user must not approve what cannot be sent
                raise ValueError("the user typed in %s since its last prompt - nothing was sent, try again later" % target.id)
            if not self.ask(tool, s, "Global agent wants to write to %s:\n\n%s" % (target.title, text),
                            {"target": target.id}):
                raise ValueError("the user declined")
            if not self.auto_type_ok(target):
                raise ValueError("the user typed in %s since its last prompt - nothing was sent, try again later" % target.id)
            self.type_text(target, text)
            return {"sent": True}
        if tool == "stop_session":
            if target.state in ("exited", "canceled"):   # the ask() sweep (B-N7-4): no card for a stop that does nothing
                raise ValueError("session %s is %s - nothing to stop" % (target.id, target.state))
            if not self.ask(tool, s, "Global agent wants to stop session %s" % target.title, {"target": target.id}):
                raise ValueError("the user declined")
            self.stop(target.id)
            return {"stopping": True}
        raise ValueError("unknown tool %r" % tool)

    # ------------------------------------------------------------ the sandbox and Piper's settings
    def settings_tool(self, s: Session, tool: str, args: dict):
        """A session reads how it is enclosed and proposes a change; the user approves each one on a card."""
        if tool == "piper_session":
            if "quiet" in args:
                control = args["quiet"]
                if not isinstance(control, dict) or control.get("action") not in (
                        "status", "ack", "wait", "renew", "work", "release", "cancel", "request"):
                    raise ValueError("quiet must be an object with a valid Quiet window action")
                # Pre-existing clients already expose piper_session. Keep the same
                # identity/role/state validation as the dedicated quiet_* tools.
                return self.quiet_tool(s, "quiet_" + control["action"], control)
            project = projects.find(s.project, PROJECT_PATH) if s.kind == "project" else None
            return {"kind": s.kind, "project": s.project, "agent": s.agent, "profile": s.profile,
                    "runtime": s.runtime, "native_permissions": s.runtime == "direct",
                    "sandbox_env": projects.sandbox_env_config(project)[1] if project else [],
                    "claude_dir": s.claude_mode, "codex_dir": "host" if s.host_codex else "session",
                    "git_mode": self.policy.get("git_mode"),
                    "network": {k: s.network.get(k) for k in ("unrestricted", "allow", "gateway")},
                    "hidden": list(s.excludes or []), "unlocked": sorted(k for k in UNLOCKS if self.unlocked(s, k) or k == "git" and self._git_hole()),
                    "auto_approve": [k for k in security.ASK_KINDS if self.auto_ok(k)],
                    "docker": bool(s.opts.get("docker")), "language": self.policy.get("language") or "",
                    **({"worktree_stack": w} if (w := self._worktree_stack_info(s)) else {})}
        if tool == "piper_settings":
            return settings.agent_describe()
        reason = " ".join(str(args.get("reason") or "").split())[:500]
        if tool == "piper_worktree_stack":
            return self._worktree_stack_tool(s, args, reason)
        if tool == "piper_allow_domain":
            if s.kind != "project":
                raise ValueError("only a project session has a .hole/settings.json")
            d, doc = self._allow_domain_check(s, args.get("domain"))   # B-N7-4: no card for what then fails
            if d in ((doc.get("network") or {}).get("allow") or []):
                return {"domain": d, "written": False, "note": "already allowed - nothing to change"}
            summary = "Session %s wants to allow the domain %s in %s/.hole/settings.json (network.allow).%s\n\n" \
                      "The next start of this project shows the changed settings for your approval." % (
                          s.title, d, s.path, "\n\nReason: " + reason if reason else "")
            local = self._reaches_this_machine(s, d, doc)
            if local:   # N9 (N8 decision 4): a red card, the reason first (B-N6-1 style)
                summary = "⚠ %s is %s - the sandbox would reach services on this machine\n\n%s" % (d, local, summary)
            if not self.ask("allow_domain", s, summary, {"domain": d, **({"danger": True} if local else {})}):
                raise ValueError("the user declined")
            return dict(self.allow_domain(s.id, d),
                        note="applies from the next start of a session in this project (the running sandbox keeps its network)")
        sid = str(args.get("section") or "")
        values = args.get("values") or {}
        reset = args.get("reset") or []
        if not isinstance(values, dict) or not isinstance(reset, list):
            raise ValueError("values must be an object and reset a list of keys")
        try:
            changes = settings.agent_preview(sid, values, reset)
        except settings.SettingsError as e:
            raise ValueError("%s: %s" % (e, json.dumps(e.errors, ensure_ascii=False)))
        except KeyError:
            raise ValueError("unknown section %r - see piper_settings" % sid)
        if not changes:
            return {"saved": [], "note": "nothing to change - the values are already set"}
        lines = []
        for c in changes:
            lines.append("%s: %s → %s" % (c["label"], json.dumps(c["old"], ensure_ascii=False),
                                          json.dumps(c["new"], ensure_ascii=False)))
            if c["warning"]:
                lines.append("⚠ " + c["warning"])
        summary = "Session %s wants to change Piper's settings (%s):\n\n%s%s" % (
            s.title, sid, "\n".join(lines), "\n\nReason: " + reason if reason else "")
        if not self.ask("settings_change", s, summary, {"section": sid, "keys": [c["key"] for c in changes],
                                                       "weakens": any(c["warning"] for c in changes)}):   # the GUI's red Allow
            raise ValueError("the user declined the change")
        keys = [c["key"] for c in changes]
        r = settings.update(sid, {k: v for k, v in values.items() if k in keys}, confirm=keys,
                            reset=[k for k in reset if k in keys],
                            on_change=lambda stores: self.reload_policy() if "policy" in stores else None)
        audit({"event": "settings-by-session", "session": s.id, "section": sid, "keys": r["saved"]})
        return {"saved": r["saved"], "apply": r["apply_text"]}

    # ------------------------------------------------------------ handoff between project sessions
    # A project session proposes a session in another project (the user confirms the whole task),
    # the child reports back with report_result and the parent pulls it with handoff_result. The
    # parent's terminal only ever gets a fixed line from piperd, never the child's text.
    HANDOFF_FOOTER = ("\n\n---\nThis session was proposed by session %s. When you are done (or hit a blocking "
                      "problem), call report_result(summary): what is done, commits, whether mvn install ran "
                      "(and the artifact version, if any), what remains.")
    MAX_HANDOFF_SUMMARY = 8000

    def _create_allowed(self, s: Session, tool: str, *a, **k) -> Session:
        """create() after the human allowed the card: a failure (B-N8-1: the session limit filled while the card
        waited) also goes to the feed - the human clicked Allow, the agent alone hearing of it left no trace."""
        try:
            return self.create(*a, **k)
        except ValueError as e:
            try:
                self.feed_allowed_failed(s, tool, str(e))
            except Exception as fe:  # noqa: BLE001 - the feed is a view
                print("piperd: feed_allowed_failed: %s" % fe, flush=True)
            raise

    def handoff_tool(self, s: Session, tool: str, args: dict):
        if s.kind != "project":
            raise ValueError("tool %r is only available to project sessions" % tool)
        if tool == "propose_session":
            if s.parent:
                raise ValueError("a proposed session cannot propose another one")
            key = str(args.get("project") or "").strip()
            task = common.no_bidi(str(args.get("task") or "")).strip()   # the card shows what is sent (E25)
            title = " ".join(common.no_bidi(str(args.get("title") or "")).split())
            if not task:
                raise ValueError("task must not be empty")
            if len(task) > 20000 or len(title) > 200:
                raise ValueError("task/title too long")
            p = projects.find(key, PROJECT_PATH)
            if p is None or not p["exists"]:
                raise ValueError("unknown project %r" % key)
            self._check_limit()   # before the card: never ask for a session that cannot start (create checks again)
            summary = "Session %s proposes a new session in project %s%s. The task as it will receive it:\n\n%s" % (
                s.title, key, " (\"%s\")" % title if title else "", task)
            if not self.ask(tool, s, summary, {"project": key}):
                raise ValueError("the user declined the proposed session")
            n = self._create_allowed(s, tool, key, task + self.HANDOFF_FOOTER % s.id, title, origin="handoff", parent=s.id)
            audit({"event": "handoff-open", "session": s.id, "child": n.id, "project": key})
            return {"id": n.id, "state": n.state,
                    "note": "Piper tells you in the terminal when it reports; then call handoff_result"}
        if tool == "report_result":
            if not s.parent:
                raise ValueError("this session was not proposed by another one")
            summary = str(args.get("summary") or "").strip()
            if not summary:
                raise ValueError("summary must not be empty")
            if len(summary) > self.MAX_HANDOFF_SUMMARY:
                raise ValueError("summary is longer than %d characters" % self.MAX_HANDOFF_SUMMARY)
            self._set_handoff(s, summary, True)
            return {"reported": True, "parent": s.parent}
        # handoff_result
        kids = sorted((x for x in list(self.sessions.values()) if x.parent == s.id), key=lambda x: x.created)
        cid = str(args.get("id") or "")
        if cid:
            kids = [x for x in kids if x.id == cid]
            if not kids:
                raise ValueError("session %r was not proposed by this session" % cid)
        with s.lock:
            s.handoff_notify = [x for x in s.handoff_notify if x not in {k.id for k in kids}]
            s.save()
        out = []
        for k in kids:
            h = k.handoff or {}
            out.append({"id": k.id, "project": k.project, "title": k.title, "state": k.state, "rc": k.rc,
                        "idle_at_prompt": k.idle and k.state in ("waiting", "attention"),
                        "reported": bool(h.get("reported")), "reported_at": h.get("at"),
                        "summary_from_other_agent": h.get("summary", ""),
                        "facts_from_piper": h.get("facts") or {}})
        return out

    def _handoff_facts(self, s: Session) -> dict:
        """Git facts of the child's project, computed on the host (the child cannot fake them)."""
        try:
            top = self._turn_top(s)
            st = gitops.session_status(top, self.hidden_for(s))
            # without an upstream `ahead` is 0 and means nothing: say so instead of "pushed"
            return {"branch": st["branch"], "upstream": st["upstream"] or None,
                    "ahead": st["ahead"] if st["upstream"] else None, "withheld": st["withheld"],
                    "uncommitted": [f["path"] for f in st["files"]][:50], "log": gitops.session_log(top, 5)}
        except (ValueError, OSError, gitops.GitError) as e:
            return {"error": str(e)}

    def _set_handoff(self, s: Session, summary: str, reported: bool) -> None:
        facts = self._handoff_facts(s)
        with s.lock:
            s.handoff = {"summary": "\n".join(common.scrub(x) for x in summary.splitlines()), "reported": reported,
                         "at": time.time(), "state": s.state, "rc": s.rc, "facts": facts}
            s.save()
        audit({"event": "handoff-report", "session": s.id, "parent": s.parent, "reported": reported})
        parent = self.sessions.get(s.parent or "")
        if parent is None:
            return
        with parent.lock:
            if s.id not in parent.handoff_notify:
                parent.handoff_notify.append(s.id)
            parent.save()
        self.bump()
        self._flush_handoff_notes(parent)

    def handoff_note(self, ids: list[str]) -> str:
        """The only text piperd types into a parent: ids and project keys, nothing the child wrote."""
        parts = []
        for cid in ids:
            k = self.sessions.get(cid)
            if k is not None:
                what = "sent a result" if (k.handoff or {}).get("reported") else "ended without a result"
                parts.append("%s (%s) %s" % (k.id, k.project, what))
        return "[piper] Session %s. Read it with handoff_result." % "; ".join(parts) if parts else ""

    def _flush_handoff_notes(self, s: Session) -> None:
        """Tell an idle parent that children reported; a busy one hears it at its next Stop."""
        if s.state not in ("waiting", "attention") or not s.idle or not s.handoff_notify:
            return
        with s.lock:   # claimed atomically: the poll loop, a hook event, handoff_report and "Send now" may race
            if s.handoff_sending:
                return
            s.handoff_sending = True
        s.hold_try = time.time()
        if not self.auto_type_ok(s):   # retried by the poll loop (_poll_loop) until the prompt is empty
            s.handoff_sending = False
            return

        def send() -> None:
            try:
                with s.lock:
                    ids = list(s.handoff_notify)
                text = self.handoff_note(ids)
                if text:
                    self.type_text(s, text)
                with s.lock:
                    s.handoff_notify = [x for x in s.handoff_notify if x not in ids]
                    s.save()
                audit({"event": "handoff-notify", "session": s.id, "children": ids})
            except Exception as e:  # noqa: BLE001 - stays queued for the next Stop
                print("piperd: handoff note for %s: %s: %s" % (s.id, type(e).__name__, e), flush=True)
            finally:
                s.handoff_sending = False
        threading.Thread(target=send, daemon=True).start()
