"""Settings - one schema-driven GUI page over Piper's config.

Every knob the user used to set by hand-editing JSON or running `scripts/piper.sh ...` is a
field of a section in REGISTRY. A field names its store and its path in it:

  policy     $PIPER_HOME/policy.json (defaults: guard.DEFAULT_POLICY, holehome.DEFAULTS)
  projects   $PIPER_HOME/projects.json
  json:<n>   $PIPER_HOME/<n>.json, a feature's own file (<n> may be one subdirectory deep: obs/config)
  secret:<f> $PIPER_HOME/<f>, one secret per file (mode 600); GET never returns it
  env        read-only: the environment piperd (or its login service) was started with

Rules:
  - only the GUI's `*` token reaches /api/settings (piperd checks the scope); a sandbox has no
    piperd token at all, it talks to piperd only through its mailbox;
  - a write keeps unknown keys and the file's indentation and key order, is atomic (tmp +
    rename) and fails with 409 when the file changed since the page read it (`rev`);
  - a change that weakens the isolation (claude_dir=host, unrestricted_network, fewer scan
    candidates, ...) is refused with 409 `needs_confirm` until the request names the key in
    `confirm` - the GUI shows the warning first; the check is here, not only in the GUI;
  - every change is audited (`settings-change`: section, key, old -> new; a secret only as
    set / cleared).

A new feature adds ONE Section to REGISTRY (and, if needed, a `validate` callable).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import common
import dbsql
import extensions
import feed
import features
import guard
import security
import holehome
import ideamcp
import mattermost
import prodcheck
import reviewactions
import projects as projects_mod
import timesheet

LOCK = threading.Lock()
_SECRET = object()   # update(): marks a secret change (its old value is never kept)

APPLY = {
    "now": "takes effect immediately",
    "session": "applies to newly started sessions (running ones keep what they started with)",
    "restart": "takes effect after Piper restarts",
    "service": "comes from the environment Piper starts in; a change takes effect when Piper is started again with it",
}

TIME_RX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
GL_PATH_RX = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")
# a GitLab path a session may name (sessions.py): a segment never starts with "." or "-" (no "..")
GL_READ_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}){1,9}$")
PATTERN_RX = re.compile(r"^[^\s\x00]{1,300}$")        # a hole glob / path pattern, no spaces
ENV_NAME_RX = re.compile(r"^[A-Za-z0-9_*?]{1,100}$")
DOMAIN_RX = re.compile(r"^[A-Za-z0-9*.-]{1,253}$")
DOTTED_RX = re.compile(r"^[A-Za-z0-9_*]+(\.[A-Za-z0-9_*]+)*$")


class SettingsError(ValueError):
    def __init__(self, msg: str, errors: dict | None = None):
        super().__init__(msg)
        self.errors = errors or {}


class NeedsConfirm(Exception):
    def __init__(self, items: list[dict]):
        super().__init__("confirm the changes that weaken the isolation")
        self.items = items


class Conflict(Exception):
    pass


# ------------------------------------------------------------------ weakening checks
def _removed(old, new) -> list:
    return [x for x in (old or []) if x not in (new or [])]


def _added(old, new) -> list:
    return [x for x in (new or []) if x not in (old or [])]


def weaker_if_removed(what: str) -> Callable:
    def check(old, new):
        r = _removed(old, new)
        return "%s: removed %s" % (what, ", ".join(map(str, r))) if r else None
    return check


def weaker_if_added(what: str) -> Callable:
    def check(old, new):
        a = _added(old, new)
        return "%s: added %s" % (what, ", ".join(map(str, a))) if a else None
    return check


def weaker_if_lower(what: str) -> Callable:
    def check(old, new):
        return "%s: lowered from %s to %s" % (what, old, new) if old is not None and new is not None and new < old else None
    return check


def _db_higher(what: str, limit: int) -> Callable:
    def check(old, new):
        return "%s raised to %s - more data goes to the model provider" % (what, new) \
            if new is not None and new > limit and (old is None or new > old) else None
    return check


def weaker_if_on(text: str) -> Callable:
    def check(old, new):
        return text if new and not old else None
    return check


def weaker_if_off(text: str) -> Callable:
    def check(old, new):
        return text if old and not new else None
    return check


def _claude_dir(old, new):
    if new == "host" and old != "host":
        return ("K2: every session then mounts your real ~/.claude read-write. The sandbox reads the transcripts "
                "of ALL your sessions and can change hooks and settings that then run on the host.")
    return None


def _codex_dir(old, new):
    if new == "host" and old != "host":
        return ("Every Codex session (and, with \"Codex as Claude's subagent\", every Claude project session) then "
                "mounts your real ~/.codex read-write. The sandbox reads your whole Codex "
                "history, a token refresh in it rotates your host login, its config.toml and hooks run on the host "
                "next time, and its SQLite databases may break when the host Codex runs at the same time.")
    return None


def _unrestricted(old, new):
    if new and not old:
        return ("Sessions will run without the outbound network filter. The sandbox reaches GitLab and internal "
                "services directly, and the allowed domains in the GUI stop applying.")
    return None


def _git_mode(old, new):
    if new == "hole" and old != "hole":
        return ("git as in a plain hole: libraries become read-write, host git runs without a card, and "
                "nothing reverts what a sandbox writes into .git config/hooks (host git, IDEs and piperd then run it).")
    return None


def _idea_sessions(old, new):
    if new and not old:
        return ("IntelliJ's MCP server runs on the host and offers execute_terminal_command, run configurations and "
                "file access: a session can then run code outside the sandbox and read files the hidden-path filter "
                "withholds.")
    return None


def _l5(old, new):
    if old == "stop" and new != "stop":
        return "A sensitive file that appears while a session runs no longer stops the session, it only flags it."
    return None


def _gl_host(old, new):
    if old != new:
        return "The GitLab token will be sent to %s. Check that it really is your GitLab." % new
    return None


# ------------------------------------------------------------------ schema
@dataclass
class Field:
    key: str                      # unique within the section; the GUI's id
    label: str
    type: str                     # bool int time enum string list secret projects env action
    store: str = "policy"
    path: tuple = ()              # path inside the JSON store (default: (key,))
    default: Any = None
    help: str = ""
    apply: str = "session"
    choices: list = field(default_factory=list)   # enum: [[value, label], ...]
    min: int | None = None
    max: int | None = None
    item_rx: re.Pattern | None = None             # list: every item must match
    item_int: bool = False                        # list of ints (ports)
    rx: re.Pattern | None = None                  # string
    check: Callable | None = None                 # string: extra validation -> error text or None
    weakens: Callable | None = None               # (old, new) -> warning text (needs confirmation)
    env: str = ""                                 # env: the variable
    effective: Callable | None = None             # env: the value piperd uses now

    def jpath(self) -> tuple:
        return self.path or (self.key,)


@dataclass
class Section:
    id: str
    title: str
    help: str = ""
    fields: list = field(default_factory=list)
    hidden: bool = False          # a stub of a feature whose code is not merged yet
    validate: Callable | None = None   # (values: dict) -> {key: error}
    actions: list = field(default_factory=list)   # GUI-only buttons: [[id, label, help]]
    group: str = "general"        # the tree node in the left column, one of GROUPS
    module: str = ""              # a feature module (features.py): the section is not shown while it is off


GROUPS = [["general", "Workspace"], ["sessions", "Sessions"],
          ["vcs", "Version Control"], ["tools", "Tools"], ["advanced", "Advanced & isolation"]]


def _url_check(v: str) -> str | None:
    return None if not v or ideamcp.loopback(v) else "http on loopback only (127.0.0.1 / localhost), project paths must not leave the machine"


def _auto_approve(old, new) -> str | None:
    """auto_approve: every added kind is a weakening; gitlab_upload_file names its risk (R4a, 2026-10-10)."""
    w = weaker_if_added("actions without approval")(old, new)
    if w and "gitlab_upload_file" in _added(old, new):
        w += ". A visible project file a session names then leaves for GitLab without a card " \
             "(hidden files stay refused)"
    return w


def _env_int(name: str, default: str) -> Callable:
    return lambda: os.environ.get(name, default)


def _env_max_sessions() -> int:
    """PIPER_MAX_SESSIONS as the default of policy max_sessions (sessions.MAX_SESSIONS, not imported: it imports us)."""
    try:
        return max(1, int(os.environ.get("PIPER_MAX_SESSIONS", "6")))
    except ValueError:
        return 6


APPROVAL_KINDS = [["git_commit", "commit"], ["git_push", "push"],
                  ["gitlab_comment", "GitLab comment / thread reply"], ["gitlab_note_edit", "edit an existing own GitLab comment"],
                  ["gitlab_upload_file", "upload a file attachment to GitLab"], ["gitlab_job_log", "read a failed CI job's log"],
                  ["gitlab_issue_write", "create / edit a GitLab issue"], ["gitlab_mr_write", "create / edit a merge request, resolve a thread"], ["gitlab_mr_merge", "merge a merge request"], ["gitlab_job_play", "start / retry a CI job"], ["gitlab_read", "read a GitLab project outside the session's scope"],
                  ["github_comment", "GitHub comment"], ["github_note_edit", "edit an existing own GitHub comment"],
                  ["github_issue_write", "create / edit a GitHub issue"], ["github_pr_write", "open / edit a pull request"],
                  ["github_job_log", "read a failed GitHub Actions job's log"],
                  ["propose_session", "propose a new session"], ["spawn_session", "Global agent: start a session"],
                  ["edit_file", "edit another project"], ["write_file", "write a file in another project"],
                  ["git_run", "full git on the host"], ["send_to_session", "Global agent: write to a session"],
                  ["stop_session", "Global agent: stop a session"],
                  ["http_test", "run a .http file on a test environment"],
                  ["http_dev", "run a .http file on dev (when dev asks at all)"],
                  ["db_write_dev", "write to a dev database (the local stack)"],
                  ["db_read_test", "read a test database (first read of a session)"],
                  ["db_other", "use a database connection of another project"]]
# never auto-approved (sessions.ALWAYS_ASK): http_prod, db_write_test, db_write_prod, db_read_prod, db_unmask

def _module_field(m: str) -> Field:
    """One feature module's switch (features.json modules.<m>); `host` warns, it opens shells outside every sandbox."""
    label, text = next((lb, t) for i, lb, t in features.MODULES if i == m)
    if m != "host":   # composer (9.44) is off until switched on, like every NO_PRESET module
        return Field(m, label, "bool", store="json:features", path=("modules", m), default=m not in features.NO_PRESET,
                     apply="now", help=text)
    return Field(m, label, "bool", store="json:features", path=("modules", m), default=False, apply="now", help=text
                 + " Marked HOST, audited (open / close, never what is typed); never proposed by an agent.",
                 weakens=weaker_if_on("A shell on your Mac without any sandbox opens in the Host shells view: everything "
                                      "typed there runs as you. Only you (the GUI token) can open one, never a session."))


D = guard.DEFAULT_POLICY
H = holehome.DEFAULTS

REGISTRY: list[Section] = [
    # ---------------------------------------------------------------- Appearance & Behavior
    Section(
        # 9.40: the Integrations section folded in - one page, the welcome's gallery (Settings › Features in the GUI)
        "features", "Features", "What Piper does and what it talks to. A module that is off disappears from the GUI and "
        "its background work stops; its data and settings stay. Sessions, files, changes and the isolation are always on. "
        "A preset sets the modules and the services, never the host shells.",
        [_module_field(m) for m in features.CORE + features.INTEGRATIONS],
        actions=[["preset-" + p, "Preset: " + label, text] for p, label, text in features.PRESET_LABELS], group="general"),
    Section("interface", "Interface", "How the GUI behaves for you. Nothing here changes what a session may do.", [
        Field("theme", "Theme", "enum", default="dark", apply="now",
              choices=[["dark", "Dark (default)"], ["light", "Light"], ["auto", "Like the system (light / dark)"], ["indigo", "Indigo"]],
              help="The colours of the whole GUI and its terminals; after Save every open tab follows."),
        Field("nav_history", "Back / forward (mouse side buttons, ⌘[ ⌘])", "bool", default=True, apply="now",
              help="The mouse's back / forward buttons and ⌘[ ⌘] walk the places you moved to: sessions, views, files, lines."),
        Field("restore_view", "Reload returns to the last view", "bool", default=True, apply="now",
              help="After a reload the GUI opens the session and view you had, not the first running session."),
        Field("host_link_browser", "Browser for terminal links", "enum", default="default", apply="now",
              choices=[["default", "System default"], ["chrome", "Google Chrome"]],
              help="Enable “Terminal links opened by the host” in New sessions. Chrome bypasses the Safari Dock app."),
        Field("term_click_opens_files", "⌘+click on a file in the terminal opens Files", "bool", default=True, apply="now",
              help="A click on path:line offers a menu; ⌘/Ctrl+click opens the Files tab, ⇧+click IntelliJ (off: ⌘+click too)."),
    ], group="general"),
    Section("terminal", "Terminal", "Where Open in terminal (piper attach) opens a session: that terminal then sets the "
            "session's size, Ctrl-] in it detaches.", [
        Field("app", "Open sessions in", "enum", store="json:terminal", default="auto", apply="now",
              choices=[["auto", "Ghostty if installed, else Terminal (Linux: $TERMINAL / the desktop's terminal; WSL: Windows Terminal)"], ["ghostty", "Ghostty"], ["warp", "Warp"],
                       ["terminal", "Terminal (macOS)"]],
              help="Warp: Piper writes ~/.warp/launch_configurations/piper-attach.yaml and opens it. "
                   "Terminal: a .command file in run/ of the Piper home. Linux: $TERMINAL, x-terminal-emulator, then GNOME / KDE / Xfce / "
                   "kitty / Alacritty / WezTerm / foot / xterm. WSL: Windows Terminal (else a console via cmd.exe)."),
    ], group="general"),
    Section("feed", "Feed", "The chat with the Global agent (⌘0) and the extras of the Overview queue: Dismiss and Mute, "
            "the history of what happened (turns, sessions, new GitLab comments) and failed CI pipelines. It runs while "
            "the Global agent module is on (Settings › Features).", [
        Field("retention_days", "Event history (days)", "int", store="json:feed/config", default=30, min=1, max=365,
              apply="restart"),
        Field("feed_auto_actions", "Auto-approved actions in the feed", "bool", default=False, apply="now",
              help="What ran without a card shows in the feed."),
    ], validate=lambda v: _feature_check(feed.validate, {k: v[k] for k in ("retention_days",) if k in v},
                                       "retention_days"), group="general", module="agent"),
    # ---------------------------------------------------------------- Projects
    Section("projects", "Projects", "Every checkout Piper offers - found and added by hand. Fill in GitLab and "
            "observability right in the row; empty GitLab = from origin.", [
        Field("projects", "Projects", "projects", store="projects", apply="now"),
        Field("globs", "Where to look for more checkouts", "list", store="projects", default=[], apply="now",
              item_rx=re.compile(r"^[^\x00\n]{1,300}$"),
              help="Patterns, one per line, e.g. ~/development/* (key = the last two path segments)."),
    ], group="general", actions=[["prod-suggest", "Suggest observability mapping",
                                   "a short session looks up the projects without a mapping (except \"Don't map\")"]]),
    # ---------------------------------------------------------------- Sessions: the template, the sandbox, what a session may do alone
    Section("sessions", "New sessions", "What new sessions unlock, their language and tools. Start options and the agent "
            "are remembered per project in the New session dialog; a running session's options are in its "
            "Isolation panel.", [
        Field("max_sessions", "Concurrent sessions at most", "int", default=_env_max_sessions(), min=1, max=50,
              apply="now", help="Blocked and ended sessions do not count. The default comes from Piper's environment (%d here)." % _env_max_sessions()),
        Field("default_unlocks", "Unlocked in every new session", "list", default=[], apply="session",
              choices=[["projects", "read other projects"], ["projects_write", "edit other projects"],
                       ["git", "full git (guarded git mode)"], ["db_write_other", "write other projects' databases"]],
              weakens=weaker_if_added("default unlocks"),
              help="Each session can still be locked again in its Isolation panel."),
        Field("language", "Language of the answers", "string", default="",
              help="E.g. Français. Every session (Claude, Codex, the Global agent) answers you in it; code, commits "
                   "and identifiers stay as the project has them. Empty = the agent's own choice."),
        Field("maven_settings", "Maven settings in sessions", "enum", default="auto", apply="session",
              choices=[["auto", "generated from your ~/.m2/settings.xml (default)"],
                       ["off", "none - only what your ~/.hole/settings.json includes"]],
              help="Every session start writes its own ~/.m2/settings.xml and toolchains.xml from your host file: "
                   "the active profiles, the servers their repositories use (repository logins, a registry's "
                   "Deploy-Token), JAVA_*_HOME pointed at the image's JDKs; other profiles, unused servers and "
                   "proxies stay on the host. An include onto ~/.m2/settings.xml in ~/.hole/settings.json wins."),
        Field("idea_mcp_url", "IntelliJ MCP URL", "string", default="", apply="now", check=_url_check,
              help="Empty = the default http://127.0.0.1:64342/stream. Loopback only."),
        Field("idea_in_sessions", "IntelliJ MCP in sessions", "bool", default=False, weakens=_idea_sessions,
              help="For all sessions. For just one: tick it in the New session dialog. The agent gets IDEA's tools as "
                   "`idea` through a bridge to the URL above. From the next session start."),
        Field("codex_in_claude", "Codex as Claude's subagent", "bool", default=False, apply="session",
              weakens=weaker_if_on("Every Claude project session gets Piper's Codex login (read-write) - "
                                   "your real ~/.codex when ~/.codex for Codex sessions is \"your real\"."),
              help="Claude sessions get a Codex home with Piper's Codex login; with ~/.codex for Codex sessions = "
                   "\"your real\" they get the real ~/.codex instead (the host's login)."),
        # F26 (9.83, changes 9.46): Settings › Approvals merged here - with the Security tree on, auto_approve and
        # write_guard are its switches (hidden, _governed); the two GUI behaviours stay as plain fields
        Field("auto_approve", "Without approval", "list", default=D["auto_approve"], apply="now",
              choices=APPROVAL_KINDS, weakens=_auto_approve,
              help="A push whose commits delete hidden files is refused either way. Editing other projects (and "
                   "full git in the guarded git mode) must also be unlocked per session."),
        Field("write_guard", "Guard writes into other projects", "bool", default=D["write_guard"], apply="now",
              weakens=weaker_if_off("Sessions may then write Piper's own checkout (code the host runs), "
                                    "and auto-approved writes reach hooks / build files of other projects."),
              help="Never write Piper's own checkout; files the host runs (hooks, .claude, CI, build) always on a card."),
        Field("open_links_on_host", "Terminal links opened by the host", "bool", default=False, apply="now",
              weakens=weaker_if_on("A click on a link a session printed opens it with the host's `open`."),
              help="piperd opens terminal links with the host's open (Safari web app)."),
        Field("clipboard_arm_enter", "Clipboard after Enter", "bool", default=False, apply="now",
              weakens=weaker_if_on("What a session prints as an OSC 52 copy reaches your clipboard after Enter."),
              help="Enter or a drag ending outside the terminal lets the agent's OSC 52 copy reach the clipboard."),
    ], group="sessions"),
    # ---------------------------------------------------------------- Security (9.46): the GUI only, never an agent; next to session permissions
    Section("security", "Security", "What an agent may do on its own, and what it asks you first. Everything it does on its own is "
            "still written to Audit.", [
        Field("security", "Security tree", "security", path=("security",), default=security.default_value(), apply="now"),
        Field("db_dry_run", "Dry run test writes before asking", "bool", default=True, apply="now",
              help="Preview eligible test/dev DML in a rolled-back transaction with a two-second budget. Never production or schema changes."),
    ], group="sessions"),
    Section("sandbox", "Sandbox", "What a session's sandbox sees and how its isolation is checked at the start and "
            "while it runs.", [
        Field("claude_dir", "~/.claude for sessions", "enum", default="session",
              choices=[["session", "a separate one per session (from a template)"], ["host", "your real ~/.claude"]],
              weakens=_claude_dir,
              help="\"separate\" = isolated login and settings. \"your real\" = the same login, plugins and history, but the sandbox sees all transcripts."),
        Field("codex_dir", "~/.codex for Codex sessions", "enum", default="session",
              choices=[["session", "a separate one per session (from a template)"], ["host", "your real ~/.codex"]],
              weakens=_codex_dir,
              help="\"separate\" = isolated login and settings, log in once in a session. \"your real\" = the same "
                   "login, config and history as the host Codex, but the sandbox sees and writes all of it. Also "
                   "applies to Claude's Codex subagent."),
        Field("unrestricted_network", "Unfiltered network", "bool", default=False, weakens=_unrestricted,
              help="For all sessions. For just one: tick it in the New session dialog (Docker is there too)."),
        Field("stack_scope_project", "Dev stack access: own project only", "bool", default=D["stack_scope_project"], apply="session",
              weakens=weaker_if_off("Every project session then reaches every project's dev stack (logs, commands, start)."),
              help="A project session reaches its own project's dev stack only (the Global agent keeps all)."),
        Field("settings_auto_tighten", "Tightening settings pass the preflight", "bool", default=False, apply="session",
              weakens=weaker_if_on("A changed .hole/settings.json that only adds excludes (or egress lists under an "
                                   "unfiltered network) starts without your review."),
              help="A .hole/settings.json change that only adds excludes needs no approval."),
        Field("login_spread", "Spread a refreshed login", "bool", default=False, apply="now",
              weakens=weaker_if_on("A login one sandbox wrote is copied into the other running sessions."),
              help="A login refreshed in one session is written into the other running sessions."),
        Field("l5_action", "A sensitive file appears while a session runs", "enum", default="attention",
              choices=[["attention", "flag the session (default)"], ["stop", "stop the session"]], weakens=_l5,
              help="Checked every few seconds (Environment › Watch interval)."),
    ], group="sessions"),
    # ---------------------------------------------------------------- Version Control
    Section("git", "Git", "Git in sessions and git on the host: the mode, the protected branches and what runs "
            "without a card.", [
        Field("git_mode", "Git in sessions", "enum", default=D["git_mode"], apply="now",
              choices=[["hole", "as in a plain hole (default)"], ["guarded", "guarded (read-only libraries, host git on a card)"]],
              weakens=_git_mode,
              help="\"as in hole\": read-write libraries and your ~/.gitconfig (a copy) from the next session start, "
                   "host git without unlock or card (except a command that runs code, reaches outside the repository, names "
                   "a hidden file or force-pushes) and no git guard at once. git worktree prune/remove, gc and prune "
                   "ask in Claude's terminal in both modes (Claude only, best effort)."),
        Field("protected_branches", "Protected branches (never pushed or merged into)", "list",
              default=D["protected_branches"], apply="now", item_rx=re.compile(r"^[A-Za-z0-9._/-]{1,100}$"),
              weakens=weaker_if_removed("protected branches"),
              help="Checked on the real target of every push and merge; no card allows it."),
        Field("mr_target_protected", "Branches a merge request may not target", "list",
              default=D["mr_target_protected"], apply="now", item_rx=re.compile(r"^[A-Za-z0-9._/-]{1,100}$"),
              weakens=weaker_if_removed("protected MR targets")),
        Field("git_fail_closed", "Host git fail-closed", "bool", default=D["git_fail_closed"], apply="now",
              weakens=weaker_if_off("Host git then runs hooks, local-path remotes and programs a sandbox wrote into "
                                    ".git/config (filters, aliases, sshCommand...) - on the host, outside the sandbox."),
              help="Host git runs no hooks (also commits from the GUI) and refuses a repository whose .git/config names a program."),
        Field("git_run_projects", "Host git in another checkout", "bool", default=False, apply="now",
              weakens=weaker_if_on("Sessions with \"edit other projects\" unlocked may run host git in another "
                                   "checkout Piper knows (each call on a card)."),
              help="Other checkouts need \"edit other projects\" unlocked and a card. Mounted writable libraries already allow normal Git operations (reads, branches, fetch/pull, add/commit/push, merge/rebase and built-in local archives) under the own-repository checks. Forced/discarding operations and amendments still ask."),
        Field("git_run_readonly_extra", "More read-only git without a card", "bool", default=True, apply="now",
              weakens=weaker_if_on("Read-only host git (grep, history, ls-remote to the configured remote, local archive) runs without a card."),
              help="Ordinary Git searches, history, diffs and reference reads run without a card, including grep, merge-base, ls-remote (configured remote), rev-parse, rev-list, describe, show-ref, for-each-ref, ls-files, ls-tree, cat-file, blame, shortlog, name-rev, check-attr, check-ignore, count-objects and local archive (built-in tar/zip). grep excludes hidden files before searching; programs, unsafe inputs, destructive operations and protected branches retain their guards."),
        Field("git_hidden_server_content", "Branch switches with hidden files from GitLab without a card", "bool", default=False,
              apply="now",
              weakens=weaker_if_on("A switch / merge / pull that rewrites a hidden file (a CI definition, a secrets file, config) runs without a card when that content is on the GitLab server; a replaced hidden file no longer puts every later host git call on a card."),
              help="A branch switch, checkout of a branch, merge or pull that rewrites a hidden work-tree file runs without a card when every rewritten file's content exists in the project's GitLab repository (checked with Piper's token against the origin the host recorded). Content only the sandbox committed, a hidden file the target deletes or a local ignored file it would overwrite, a merge both sides changed, restore and checkout of paths still ask. The sandbox may then read the switched file until its session restarts - and while it does, later host git calls no longer ask for that alone."),
        Field("git_run_output_in_repo", "Host git may write files into the repository", "bool", default=True, apply="now",
              weakens=weaker_if_on("A git archive on the host may write its output file inside the repository without a card."),
              help="A git archive on the host that writes into the repository runs without a card; a hidden file, .git, a path through a symlink, a file the host runs (hooks, .claude, CI, build) still asks. A diff / log / show written to a file always asks: it would bypass the hidden-file filter."),
        Field("git_host_hint", "Branch switching through host git", "bool", default=False, apply="session",
              help="Sessions are told to run git switch / merge / stash on the host, because files hidden from the sandbox block them there."),
        Field("commit_requires_review", "Require Review before a commit", "bool", default=D["commit_requires_review"], apply="now",
              help="The Commit panel commits only after you viewed every checked file's current version in Review. "
                   "Off: Commit commits the pinned versions at once and only says how many files you did not view."),
    ], group="vcs"),
    Section("gitlab", "GitLab", "The token piperd uses to read the inbox, issues, MRs and CI. A session never gets it.", [
        Field("token", "Personal token (PAT)", "secret", store="secret:gitlab-token", apply="now",
              help="Scope api (read_api is enough for reading only). Stored in the Piper home as gitlab-token (mode 600). "
                   "Piper's start script overwrites it with the token from GITLAB_TOKEN/GITLAB_PAT if you have one in the environment."),
        Field("gitlab_host", "GitLab server", "string", default=extensions.default("gitlab_host", "gitlab.com"), apply="now",
              rx=re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$"), weakens=_gl_host,
              help="Where piperd sends the token. A change needs confirmation."),
        Field("gitlab_read_always", "Readable from every session", "list", default=[], apply="now",
              item_rx=GL_READ_RX, weakens=weaker_if_added("GitLab projects every session reads without a card"),
              help="GitLab projects (group/repo, one per line) any session may read without a card, besides its own: "
                   "issues, merge requests, diffs (hidden files withheld), pipelines, uploads; a session may also propose "
                   "comments, issue and MR changes and read failed job logs there - each on its own card. "
                   "\"Allow always\" on a card adds a project here."),
        Field("gitlab_extra_tools", "Sessions may merge MRs and run CI jobs", "bool",
              default=False, apply="now",
              weakens=weaker_if_on("Sessions may merge merge requests and start / retry CI jobs (each on a card unless "
                                   "approved automatically)."),
              help="Sessions may merge a merge request (on a card, bound to the head the card showed) and "
                   "start a manual / delayed CI job or retry a failed one (on a card)."),
    ], actions=[["verify-gitlab", "Verify token", "asks GitLab who the token belongs to (GET /user)"]], group="vcs",
        module="gitlab"),
    Section("github", "GitHub", "Issues, pull requests and Actions of GitHub repositories, through your own gh login. "
            "Piper takes the token from your gh login per host; a session never gets it.", [
        Field("github_hosts", "GitHub hosts", "list", default=["github.com"], apply="now",
              item_rx=re.compile(r"^[A-Za-z0-9.-]{1,253}(:\d{1,5})?$"),
              weakens=weaker_if_added("GitHub hosts piperd sends your gh token to"),
              help="github.com and/or a GitHub Enterprise host (API at https://<host>/api/v3). A checkout whose origin "
                   "points at one of them is a GitHub project; log in once with `gh auth login --hostname <host>`."),
    ], actions=[["verify-github", "Verify login", "asks GitHub who the gh login belongs to (GET /user)"]],
        group="vcs", module="github"),
    Section("automatika", "GitLab automation", "Rules for when a new assignment, mention or comment starts an \"analysis only\" session.", [
        Field("inbox_interval", "Inbox interval (s)", "env", env="PIPER_INBOX_INTERVAL", apply="service",
              effective=_env_int("PIPER_INBOX_INTERVAL", "90"), help="0 = the inbox is not read in the background."),
        Field("auto_per_hour", "Automatic sessions per hour", "env", env="PIPER_INBOX_AUTO_PER_HOUR",
              apply="service", effective=_env_int("PIPER_INBOX_AUTO_PER_HOUR", "3")),
    ], actions=[["open-rules", "Edit rules…", "the same dialog as Automation… in the inbox"]], group="vcs", module="gitlab"),
    Section("review", "Changes & review", "The Changes panel, and the review templates shared by Files and Review "
            "(changes apply to newly selected actions; existing review items keep their instructions).", [
        Field("actions", "Review actions", "review_actions", store="json:review/config", default=reviewactions.DEFAULT_ACTIONS,
              apply="now", help="Numbers 1–9 toggle actions; 0 clears the current target. Prompts accept {{target}}, {{file}}, {{range}}, {{side}}, {{code}} and {{note}}."),
    ], group="vcs"),
    # ---------------------------------------------------------------- Tools
    Section("databases", "Databases", "The DB client: sessions use db_* tools, you the Database view. Connections "
            "are edited in the Database view (Connections); here the limits and the policy for agents, and the "
            "Database view's own query timeout.", [
        Field("max_rows", "Rows to an agent per call", "int", store="json:db/config", default=200, min=1, max=5000,
              apply="now", weakens=_db_higher("rows per call", 2000)),
        Field("max_bytes", "Result size to an agent (bytes)", "int", store="json:db/config", default=256 << 10,
              min=4096, max=8 << 20, apply="now", weakens=_db_higher("result size", 2 << 20)),
        Field("max_cell", "Cell length to an agent (bytes)", "int", store="json:db/config", default=4096, min=64,
              max=1 << 20, apply="now", help="Longer cells end with a cut marker; the agent reads the full value with db_cell."),
        Field("statement_timeout_s", "Statement timeout for an agent (s)", "int", store="json:db/config", default=30,
              min=1, max=600, apply="now", help="An agent's db_* call that names no timeout_s. Your own queries in the "
              "Database view use the GUI timeout below."),
        Field("max_timeout_s", "Longest timeout an agent's call may ask for (s)", "int", store="json:db/config", default=120,
              min=1, max=600, apply="now", help="A write runs within the 15 min an agent waits for a confirmed tool."),
        Field("gui_timeout_s", "Query timeout in the Database view (s)", "int", store="json:db/config", default=300,
              min=1, max=3600, apply="now", help="Your console, inline edits and writes from the GUI; the agents' limits "
              "above do not apply to them. Cancel stops a running statement sooner - yours, or a session's query."),
        Field("prod_read", "Production reads by an agent", "enum", store="json:db/config", default="session", apply="now",
              choices=[["session", "a card on the first read of a session"], ["each", "a card on every read"]],
              weakens=lambda o, n: "production reads: one card per session instead of every read" if o == "each" and n != "each" else None),
        Field("prod_require_where", "Refuse UPDATE/DELETE without WHERE on production", "bool", store="json:db/config",
              default=True, apply="now", weakens=lambda o, n: "UPDATE/DELETE without WHERE allowed on production" if o and not n else None),
        Field("mask_test", "Mask personal data on test too", "bool", store="json:db/config", default=False, apply="now",
              help="Production is always masked for agents unless a connection says otherwise; test data of many clients is anonymized."),
        Field("mask_words", "Masked column name words", "list", store="json:db/config", default=list(dbsql.MASK_WORDS) + [w for w in extensions.items("MASK_WORDS") if w not in dbsql.MASK_WORDS], apply="now",
              item_rx=re.compile(r"^[a-z0-9]{2,40}\*?$"), weakens=weaker_if_removed("masked words"),
              help="Words of a column name (customer_email -> customer, email); `birth*` = a prefix. Empty = the built-in list."),
        Field("tunnel_idle_min", "Close an unused tunnel after (min)", "int", store="json:db/config", default=30, min=1,
              max=1440, apply="now"),
        Field("console_optimizer_hints", "Allow /*+ */ optimizer hints in your console", "bool", store="json:db/config",
              default=True, apply="now",
              weakens=lambda o, n: "your console runs MySQL /*+ */ optimizer hints (SET_VAR, MAX_EXECUTION_TIME)" if n and not o else None,
              help="Only the Database view's console (what you type); agents' db_query and /*! */ stay refused."),
        Field("ssh_user", "Default SSH user of a new connection", "string", store="json:db/config", default="", apply="now",
              check=lambda v: None if not v or re.match(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$", v) else "a user name",
              help="Pre-filled in the connection editor when the SSH user is empty. Empty = ~/.ssh/config decides."),
    ], actions=[["db-install", "Install drivers", "MySQL, PostgreSQL, Oracle and SQL Server drivers into dbvenv in the Piper home (pip, a minute)"]],
       group="tools", module="db"),
    Section("timesheet", "Timesheet", "How work blocks are computed from your activity. An overview only, nothing is sent. While the "
            "module is on, timesheet/events.jsonl records time and issue (no text).", [
        Field("gap_min", "Gap that ends a block (min)", "int", store="json:timesheet/config", default=15, min=1, max=240, apply="now"),
        Field("pad_min", "Block lead-in and lead-out (min)", "int", store="json:timesheet/config", default=0, min=0, max=60, apply="now",
              help="Default 0: the timesheet must not inflate time."),
    ], validate=lambda v: _feature_check(timesheet.validate, v, "gap_min"), module="timesheet", group="tools"),
    Section("prodcheck", "Morning production check", "Once a day in the morning, a short hole session through the Loki plugin. Which projects "
            "it checks is set by the Observability column in Projects; without a mapping nothing starts.", [
        Field("time", "Time", "time", store="json:obs/config", default="07:30", apply="now"),
        Field("days", "Days", "list", store="json:obs/config", default=[1, 2, 3, 4, 5], item_int=True, apply="now",
              choices=[[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"], [6, "Sat"], [7, "Sun"]]),
    ], validate=lambda v: _feature_check(prodcheck.validate_config, v, "days"), module="prod", group="tools"),
    # F22: the help says when it sends - only while Piper is not on screen (the GUI's presence beat)
    Section("mattermost", "Mattermost", "Sends only while Piper is not on screen, never in quiet hours. Replies are not "
            "supported yet (they need a bot account).", [
        Field("webhook", "Webhook URL", "secret", store="secret:mattermost-webhook", apply="now",
              check=lambda v: None if mattermost.url_ok(v) and "/hooks/" in v else "an https://…/hooks/… address from Incoming Webhooks",
              help="Integrations → Incoming Webhooks in a private channel. Stored in the Piper home as mattermost-webhook (mode 600)."),
        Field("detail", "Message detail", "enum", store="json:mattermost", default="command", apply="now",
              choices=[["none", "only the project and status (no text from the session or the logs)"],
                       ["command", "also the start of the command and the first line of Claude's answer, titles of new production errors"],
                       ["full", "the full text of the request and the answer"]]),
        Field("after_s", "Notify after (s of waiting)", "int", store="json:mattermost", default=180, min=0, max=86400, apply="now"),
        Field("min_gap_s", "At most one message per session every (s)", "int", store="json:mattermost", default=900, min=0, max=86400, apply="now"),
        Field("quiet_from", "Quiet hours from", "time", store="json:mattermost", path=("quiet", "from"), default="", apply="now",
              help="Empty = no quiet hours."),
        Field("quiet_to", "Quiet hours until", "time", store="json:mattermost", path=("quiet", "to"), default="", apply="now"),
        Field("quiet_weekend", "Quiet at weekends", "bool", store="json:mattermost", path=("quiet", "weekend"), default=False, apply="now"),
        Field("channel", "Channel (override)", "string", store="json:mattermost", default="", apply="now",
              rx=re.compile(r"^[a-z0-9_@.-]{0,64}$"), help="Empty = the webhook's channel."),
        Field("username", "Sender name", "string", store="json:mattermost", default="", apply="now",
              rx=re.compile(r"^[^\x00\n]{0,64}$")),
    ], validate=lambda v: _mm_check(v), actions=[["mm-test", "Send a test message", "one message through the webhook"]],
       module="mattermost", group="tools"),
    Section("http", "HTTP requests", "The .http files of a project run on the host with ijhttp. Their environments "
            "(http-client.env.json, private values) belong to a project and open in the HTTP tab's dialog.", [],
            actions=[["open-http-env", "Edit environments…", "the environments dialog of the HTTP tab"]],
            module="http", group="tools"),
    # ---------------------------------------------------------------- Advanced & isolation (folded)
    Section("scan", "Isolation check", "What the preflight and the watcher treat as sensitive. Weakening needs confirmation "
            "and applies to new sessions; the watcher of running sessions uses the new patterns from its next pass.", [
        Field("candidates", "Sensitive patterns", "list", default=guard.default_candidates(), item_rx=PATTERN_RX,
              weakens=weaker_if_removed("sensitive patterns"), help="`**` = any number of segments, `*` does not cross `/` (like hole)."),
        Field("allow", "Allowed exceptions", "list", default=D["allow"], item_rx=PATTERN_RX,
              weakens=weaker_if_added("exceptions"), help="Accepted false positives, e.g. a committed test keystore."),
        Field("skip_dirs", "Skipped directories", "list", default=D["skip_dirs"], item_rx=re.compile(r"^[^/\s\x00]{1,100}$"),
              weakens=weaker_if_added("skipped directories")),
        Field("scan_skip_roots", "Unscanned includes", "list", default=D["scan_skip_roots"], item_rx=PATTERN_RX,
              weakens=weaker_if_added("unscanned includes"), help="Still mounted, only the sensitive-file checks do not walk them."),
        Field("max_depth", "Maximum depth", "int", default=D["max_depth"], min=1, max=200,
              weakens=weaker_if_lower("scan depth")),
        Field("symlink_visits", "Visits of one directory via links", "int", default=4, min=1, max=100,
              weakens=weaker_if_lower("visits via links")),
        Field("max_entries", "Scan entry limit", "int", default=2_000_000, min=10_000, max=100_000_000,
              weakens=weaker_if_lower("entry limit")),
        Field("forbidden_project_keys", "Forbidden keys of a project's .hole/settings.json", "list",
              default=D["forbidden_project_keys"], item_rx=DOTTED_RX, weakens=weaker_if_removed("forbidden keys")),
        Field("forbidden_gateway_ports", "Forbidden host-gateway ports", "list", default=D["forbidden_gateway_ports"],
              item_int=True, weakens=weaker_if_removed("forbidden ports"), help="piperd adds its own port itself."),
        Field("secret_env", "Secret variables (attestation)", "list", default=D["secret_env"], item_rx=ENV_NAME_RX,
              weakens=weaker_if_removed("secret variables")),
        Field("allowed_env", "Allowed variables", "list", default=D["allowed_env"], item_rx=ENV_NAME_RX,
              weakens=weaker_if_added("allowed variables")),
    ], group="advanced",   # F19: the installation check moved here from Features
       actions=[["open-setup", "Check the installation…", "dependencies of the modules that are on"]]),
    Section("holehome", "hole defaults", "How Piper adapts your personal global config for sessions. "
            "Applies to new sessions.", [
        Field("strip_includes", "Removed includes", "list", path=("holehome", "strip_includes"), default=H["strip_includes"],
              item_rx=PATTERN_RX, weakens=weaker_if_removed("removed includes"),
              help="Never mounted: transcripts, history, OAuth, ~/.gitconfig, the glab token."),
        Field("rw_includes", "Writable includes", "list", path=("holehome", "rw_includes"), default=H["rw_includes"],
              item_rx=PATTERN_RX, weakens=weaker_if_added("writable includes"), help="The other directories are read-only - only with git_mode \"guarded\"; "
                   "with \"hole\" every include stays writable, as in a plain hole."),
        Field("rw_libraries", "Writable libraries", "list", path=("holehome", "rw_libraries"), default=H["rw_libraries"],
              item_rx=PATTERN_RX, weakens=weaker_if_added("writable libraries"),
              help="Host paths of libraries that stay read-write. Their .git/config and hooks then run on the host."),
        Field("strip_env", "Removed variables", "list", path=("holehome", "strip_env"), default=H["strip_env"],
              item_rx=ENV_NAME_RX, weakens=weaker_if_removed("removed variables")),
        Field("strip_env_values", "Removed variable values", "list", path=("holehome", "strip_env_values"),
              default=H["strip_env_values"], item_rx=re.compile(r"^[^\x00\n]{1,200}$"),
              weakens=weaker_if_removed("removed values")),
        Field("strip_allow", "Removed domains", "list", path=("holehome", "strip_allow"), default=H["strip_allow"],
              item_rx=DOMAIN_RX, weakens=weaker_if_removed("removed domains"), help="The GitLab host is removed while Security › GitLab only through Piper is on."),
        Field("strip_ports", "Removed ports", "list", path=("holehome", "strip_ports"), default=H["strip_ports"],
              item_int=True, weakens=weaker_if_removed("removed ports")),
        Field("force_exclude", "Always exclude", "list", path=("holehome", "force_exclude"), default=H["force_exclude"],
              item_rx=PATTERN_RX, weakens=weaker_if_removed("forced excludes")),
    ], group="advanced"),
    Section("env", "Environment", "Piper reads these values from its environment when it starts; they change only there "
            "(the variable's name is in each label's tooltip). Read-only here.", [
        Field("piper_home", "Piper's data folder", "env", env="PIPER_HOME", apply="service", effective=lambda: common.PIPER_HOME),
        Field("project_path", "Projects folder", "env", env="PROJECT_PATH", apply="service",
              effective=lambda: os.environ.get("PROJECT_PATH", "")),
        Field("port", "Port", "env", env="PIPER_PORT", apply="service", effective=_env_int("PIPER_PORT", "17895")),
        Field("bind", "Listens on", "env", env="PIPER_BIND", apply="service", effective=_env_int("PIPER_BIND", "127.0.0.1")),
        Field("watch_interval", "Watch interval (s)", "env", env="PIPER_WATCH_INTERVAL", apply="service",
              effective=_env_int("PIPER_WATCH_INTERVAL", "30")),
        Field("pty_buffer", "Terminal scrollback (B)", "env", env="PIPER_PTY_BUFFER", apply="service",
              effective=_env_int("PIPER_PTY_BUFFER", str(8 << 20))),
        Field("hole_bin", "hole program", "env", env="PIPER_HOLE_BIN", apply="service",
              effective=_env_int("PIPER_HOLE_BIN", "hole")),
        Field("docker_bin", "Docker program", "env", env="PIPER_DOCKER_BIN", apply="service",
              effective=_env_int("PIPER_DOCKER_BIN", "docker")),
        Field("mm_interval", "Mattermost check interval (s)", "env", env="PIPER_MM_INTERVAL", apply="service",
              effective=_env_int("PIPER_MM_INTERVAL", "20"), help="0 = Mattermost without a background thread."),
        Field("prod_tick", "Morning check tick (s)", "env", env="PIPER_PROD_TICK", apply="service",
              effective=_env_int("PIPER_PROD_TICK", "60"), help="0 = the morning check does not start on its own."),
        Field("prod_timeout", "Morning check time limit (s)", "env", env="PIPER_PROD_TIMEOUT", apply="service",
              effective=_env_int("PIPER_PROD_TIMEOUT", "1200"), help="A check that does not report by then is stopped."),
    ] + extensions.collect("env_fields", sys.modules[__name__]), group="advanced"),
]



def _feature_check(fn, values: dict, key: str) -> dict:
    """A feature module's own validator over the section's values ({} = fine); its message
    is shown at `key`."""
    try:
        fn(dict(values))
    except ValueError as e:
        return {key: str(e)}
    return {}


def _mm_check(values: dict) -> dict:
    q = {"from": values.get("quiet_from", ""), "to": values.get("quiet_to", ""), "weekend": bool(values.get("quiet_weekend"))}
    doc = {k: values[k] for k in ("detail", "after_s", "min_gap_s", "channel", "username") if k in values}
    errs = mattermost.validate(dict(doc, quiet=q))
    return {"quiet_from": "; ".join(errs)} if errs else {}


def section(sid: str) -> Section:
    for s in REGISTRY:
        if s.id == sid and not s.hidden:
            return s
    raise KeyError(sid)


# ------------------------------------------------------------------ stores
def store_file(store: str) -> str:
    if store == "policy":
        return common.POLICY_FILE
    if store == "projects":
        return common.PROJECTS_FILE
    kind, _, name = store.partition(":")
    if kind == "json" and re.match(r"^[a-z0-9][a-z0-9_-]{0,40}(/[a-z0-9][a-z0-9_-]{0,40})?$", name) or \
            kind == "secret" and re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
        return os.path.join(common.PIPER_HOME, name + (".json" if kind == "json" else ""))
    raise ValueError("unknown store %r" % store)


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:   # like broken JSON: this section shows the reason, the others still work
        raise SettingsError("%s is not valid UTF-8 - fix it by hand, otherwise it would be overwritten" % os.path.basename(path)) from None


def _load(path: str) -> dict:
    text = _read_text(path)
    if not text or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SettingsError("%s is not valid JSON (%s) - fix it by hand, otherwise it would be overwritten" % (os.path.basename(path), e))
    if not isinstance(data, dict):
        raise SettingsError("%s: expected a JSON object" % os.path.basename(path))
    return data


def _indent(text: str | None) -> int:
    for line in (text or "").splitlines()[1:]:
        n = len(line) - len(line.lstrip(" "))
        if n:
            return n
    return 2


def write_json_keep(path: str, data: dict, old_text: str | None) -> None:
    """Atomic write keeping the file's indentation and key order (json.load keeps the order)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)   # a feature's own dir (obs/, timesheet/)
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=_indent(old_text), ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def write_secret(path: str, value: str | None) -> None:
    if value is None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _get(data: dict, path: tuple):
    cur = data
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None, False
        cur = cur[p]
    return cur, True


def _set(data: dict, path: tuple, value) -> None:
    cur = data
    for p in path[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = cur[p] = {}
        cur = nxt
    cur[path[-1]] = value


def _unset(data: dict, path: tuple) -> None:
    cur = data
    for p in path[:-1]:
        cur = cur.get(p)
        if not isinstance(cur, dict):
            return
    cur.pop(path[-1], None)
    if len(path) > 1 and not cur:   # an emptied "holehome": {} goes too
        _unset(data, path[:-1])


def mask(value: str | None) -> dict:
    """What GET shows of a secret: set or not, and at most its last 4 characters."""
    if not value:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": ("…" + value[-4:]) if len(value) >= 12 else ""}


def _secret_value(path: str) -> str | None:
    t = _read_text(path)
    return t.strip() if t and t.strip() else None


def rev(sec: Section) -> str:
    """The optimistic lock of update(): a hash of the section's OWN values (not of whole files -
    sections share policy.json, and saving one must not make the others' drafts look stale).
    A secret counts through its own hash; an unreadable JSON file through its raw text."""
    h = hashlib.sha256()
    docs: dict[str, object] = {}
    for f in sec.fields:
        if f.type in ("env", "action"):
            continue
        path = store_file(f.store)
        if f.store.startswith("secret:"):
            try:
                t = _read_text(path)
            except SettingsError:
                t = "\x03not-utf8"
            part = hashlib.sha256(t.encode()).hexdigest() if t is not None else "\x01missing"
        else:
            if path not in docs:
                try:
                    text = _read_text(path)
                    docs[path] = json.loads(text) if text and text.strip() else {}
                except SettingsError:
                    docs[path] = "\x03not-utf8"
                except ValueError:
                    docs[path] = "\x02broken:" + hashlib.sha256(text.encode()).hexdigest()
            d = docs[path]
            if isinstance(d, str):
                part = d
            else:
                v, present = _get(d if isinstance(d, dict) else {}, f.jpath())
                if f.type == "projects":   # the table also writes the top-level `obs` map (_write_projects)
                    v = [v, _get(d if isinstance(d, dict) else {}, ("obs",))]
                part = json.dumps([present, v], sort_keys=True, ensure_ascii=False, default=str)
        h.update(f.key.encode() + b"\0" + part.encode() + b"\0")
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ describe (GET)
def _field_value(f: Field, cache: dict) -> dict:
    out = {"key": f.key, "label": f.label, "type": f.type, "help": f.help, "apply": f.apply,
           "apply_text": APPLY.get(f.apply, ""), "security": f.weakens is not None}
    if f.type == "env":
        out.update(env=f.env, value=str(f.effective() if f.effective else os.environ.get(f.env, "")),
                   from_env=f.env in os.environ, readonly=True)
        return out
    path = store_file(f.store)
    if f.type == "secret":
        out.update(mask(_secret_value(path)))
        if f.store == "secret:gitlab-token" and not out["set"] and (os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_PAT")):
            out["env_fallback"] = True   # gitlab.token() falls back to the environment
        return out
    data = cache.setdefault(path, _load(path))
    if f.type == "projects":
        rows = _projects_rows(data)
        out["value"] = rows
        ignored = set(prodcheck.load_ignored())
        mapped = {r["key"] for r in rows if r["obs"]}
        sugg = common.read_json(prodcheck.SUGGEST_FILE, {}) or {}
        out["suggestions"] = {str(x["project"]): {"obs": str(x.get("obs") or ""), "note": str(x.get("note") or "")[:200]}
                              for x in (sugg.get("suggestions") or []) if isinstance(x, dict) and x.get("project")
                              and x["project"] not in ignored and x["project"] not in mapped and x.get("obs")}
        out["ignored"] = sorted(ignored)
        return out
    v, present = _get(data, f.jpath())
    out.update(value=v if present else f.default, default=f.default, is_default=not present or v == f.default)
    if f.type == "security":   # 9.46: the tree, the presets, the stored state
        out.update(value=security.state(data), tree=security.schema(), presets=security.presets())
    elif security.ENABLED and f.store in ("policy", "json:db/config") and (g := security.governed_by(f.key)):
        st = security.state(cache.setdefault(common.POLICY_FILE, _load(common.POLICY_FILE)))
        out["governed_by"] = dict(g, on=st[g["id"]] or g["id"] == "sensitive_scan" and st["l5_watch"])
    if f.choices:
        out["choices"] = f.choices
    for k in ("min", "max"):
        if getattr(f, k) is not None:
            out[k] = getattr(f, k)
    if f.item_int:
        out["item_int"] = True
    return out


def _projects_rows(data: dict) -> list[dict]:
    """Every project Piper offers, in one list: the entries of projects.json first
    (explicit), then the checkouts found under PROJECT_PATH and the globs. `obs` = the mapping to
    an observability project (top-level `obs` in projects.json, or an entry's own `obs`)."""
    entries = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    top = data.get("obs") if isinstance(data.get("obs"), dict) else {}
    rows = []
    for key, p in entries.items():
        if isinstance(p, dict):
            rows.append({"key": key, "path": str(p.get("path") or ""), "gitlab": list(p.get("gitlab") or []),
                         "profile": p.get("profile") or "", "title": p.get("title") or "",
                         "obs": str(top.get(key) or p.get("obs") or ""), "explicit": True, "source": "projects.json",
                         "orig_key": key})   # a renamed row keeps its entry's other keys and its obs mapping
    known = {r["key"] for r in rows}
    found = [p for p in projects_mod.load(os.environ.get("PROJECT_PATH", "")).values()
             if p["source"] != "projects.json" and p["key"] not in known]
    for p in sorted(found, key=lambda p: p["key"]):
        rows.append({"key": p["key"], "path": p["path"], "gitlab": [], "profile": "", "title": "",
                     "obs": str(top.get(p["key"]) or ""), "explicit": False, "source": p["source"]})
    return rows


def describe() -> dict:
    cache: dict = {}
    out = []
    mods = features.load()["modules"]
    for sec in REGISTRY:
        if sec.hidden or sec.module and not mods.get(sec.module, True):
            continue
        try:
            fields = [_field_value(f, cache) for f in sec.fields if not _governed(f)]
            err = ""
        except SettingsError as e:
            fields, err = [], str(e)
        out.append({"id": sec.id, "title": sec.title, "help": sec.help, "fields": fields, "rev": rev(sec),
                    "actions": [{"id": a[0], "label": a[1], "help": a[2]} for a in sec.actions], "error": err,
                    "group": sec.group})
    order = [g for g, _ in GROUPS]
    out.sort(key=lambda s: order.index(s["group"]) if s["group"] in order else len(order))   # stable: registry order inside
    return {"sections": out, "apply": APPLY, "groups": GROUPS}


# ------------------------------------------------------------------ validation
def _validate(f: Field, v):
    """The normalised value or raises ValueError(message)."""
    t = f.type
    if t == "bool":
        if not isinstance(v, bool):
            raise ValueError("yes / no")
        return v
    if t == "int":
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError("a whole number")
        try:
            n = int(str(v).strip())
        except ValueError:
            raise ValueError("a whole number")
        if f.min is not None and n < f.min or f.max is not None and n > f.max:
            raise ValueError("range %s–%s" % (f.min, f.max))
        return n
    if t == "time":
        if f.default == "" and isinstance(v, str) and not v.strip():   # optional time (quiet hours)
            return ""
        if not isinstance(v, str) or not TIME_RX.match(v.strip()):
            raise ValueError("time HH:MM")
        return v.strip()
    if t == "enum":
        if v not in [c[0] for c in f.choices]:
            raise ValueError("one of the options")
        return v
    if t == "string":
        if not isinstance(v, str) or len(v) > 2000 or "\n" in v:
            raise ValueError("one line of text")
        v = v.strip()
        if f.rx and v and not f.rx.match(v):
            raise ValueError("invalid format")
        if f.check and (msg := f.check(v)):
            raise ValueError(msg)
        return v
    if t == "list":
        if not isinstance(v, list) or len(v) > 500:
            raise ValueError("a list (at most 500 items)")
        out = []
        for x in v:
            if f.item_int:
                try:
                    x = int(str(x).strip())
                except ValueError:
                    raise ValueError("%r is not a number" % x)
                if f.choices:
                    if x not in [c[0] for c in f.choices]:
                        raise ValueError("invalid item %r" % x)
                elif not 1 <= x <= 65535:
                    raise ValueError("port %d outside 1–65535" % x)
            else:
                if not isinstance(x, str):
                    raise ValueError("items are text")
                x = x.strip()
                if not x:
                    continue
                if f.item_rx and not f.item_rx.match(x) or f.choices and x not in [c[0] for c in f.choices]:
                    raise ValueError("invalid item %r" % x)
            if x not in out:
                out.append(x)
        return out
    if t == "secret":
        if not isinstance(v, str) or not v.strip() or len(v) > 4096 or re.search(r"\s", v.strip()):
            raise ValueError("one string without spaces (empty = unchanged, delete with the Delete button)")
        if f.check and (msg := f.check(v.strip())):
            raise ValueError(msg)
        return v.strip()
    if t == "projects":
        return _validate_projects(v)
    if t == "review_actions":
        return reviewactions.validate(v)
    if t == "security":
        return security.validate(v)
    raise ValueError("the field is read-only")


def _governed(f: Field) -> bool:
    """A policy key the security tree sets (9.46): its old field is hidden while the tree is on."""
    return security.ENABLED and (f.store == "policy" and f.key in security.GOVERNED or
                                 f.store == "json:db/config" and f.key == "prod_require_where")


# ------------------------------------------------------------------ a session's view (piper_settings*)
# A session reads and proposes changes through piperd's mailbox; every change is a card for the user.
# Never a secret, the environment, the project table or where the GitLab token goes.
AGENT_TYPES = ("bool", "int", "time", "enum", "string", "list")
AGENT_DENY = {("gitlab", "gitlab_host"), ("features", "host"),   # W2: never proposed by an agent
              ("github", "github_hosts"), ("security", "db_dry_run")}   # Security stays GUI-only


def agent_describe() -> list[dict]:
    """describe() without the env section and the fields a session may not propose."""
    out = []
    for sec in describe()["sections"]:
        fields = [{k: f[k] for k in ("key", "label", "type", "help", "value", "default", "choices", "min", "max",
                                     "apply_text", "security") if k in f}
                  for f in sec["fields"] if f["type"] in AGENT_TYPES and (sec["id"], f["key"]) not in AGENT_DENY]
        if fields:
            out.append({"section": sec["id"], "title": sec["title"], "help": sec["help"], "fields": fields})
    return out


def agent_preview(sid: str, values: dict, reset: list | None = None) -> list[dict]:
    """What update() would change, validated, without writing: [{key, label, old, new, warning, apply_text}].
    Raises SettingsError / KeyError like update()."""
    sec = section(sid)
    by_key = {f.key: f for f in sec.fields}
    values, reset = dict(values or {}), list(dict.fromkeys(reset or []))
    errors, new = {}, {}
    for k in list(values) + reset:
        f = by_key.get(k)
        home = next((x.id for x in REGISTRY if x.id != sid and any(g.key == k for g in x.fields)), "") if f is None else ""
        if home:   # W14 moved fields between sections; a session with an old listing learns where it went
            errors[k] = "this field is in section %s now" % home
        elif f is None or f.type not in AGENT_TYPES or (sid, k) in AGENT_DENY or _governed(f):
            errors[k] = "a session cannot change this field"
    if not errors:
        for k, v in values.items():
            if k in reset:
                continue
            try:
                new[k] = _validate(by_key[k], v)
            except ValueError as e:
                errors[k] = str(e)
    if sec.validate and not errors:
        errors.update(sec.validate(_merged(sec, new, reset)) or {})
    if errors:
        raise SettingsError("some fields are not valid", errors)
    out, docs = [], {}
    for k in list(new) + reset:
        f = by_key[k]
        p = store_file(f.store)
        if p not in docs:
            docs[p] = _load(p)
        ov, present = _get(docs[p], f.jpath())
        old = ov if present else f.default
        nv = f.default if k in reset else new[k]
        if old == nv:
            continue
        out.append({"key": k, "label": f.label, "old": old, "new": nv,
                    "warning": (f.weakens(old, nv) if f.weakens else None) or "", "apply_text": APPLY.get(f.apply, "")})
    return out


def _validate_projects(rows) -> list[dict]:
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("a list of projects")
    try:   # a stored entry whose checkout is gone must not block saving everything else
        stored = _load(store_file("projects")).get("projects") or {}
    except SettingsError:
        stored = {}
    unchanged = {(k, str(e.get("path") or "")) for k, e in stored.items() if isinstance(e, dict)} \
        if isinstance(stored, dict) else set()
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError("a project row")
        key = str(r.get("key") or "").strip()
        path = str(r.get("path") or "").strip()
        if not projects_mod.KEY_RX.match(key):
            raise ValueError("key %r: letters, digits, _ . - and at most two slashes" % key)
        if key in seen:
            raise ValueError("key %s appears twice" % key)
        seen.add(key)
        if not path or "\n" in path or len(path) > 1000:
            raise ValueError("%s: the path is missing" % key)
        real = os.path.realpath(os.path.expanduser(path))
        orig = str(r.get("orig_key") or "")
        if (orig or key, path) not in unchanged and not os.path.isdir(real):
            raise ValueError("%s: directory %s does not exist" % (key, path))
        piper = os.path.realpath(common.PIPER_HOME)
        if real == piper or real.startswith(piper + os.sep) or piper.startswith(real + os.sep):
            raise ValueError("%s: a project must not contain the Piper home (%s) or lie inside it" % (key, common.PIPER_HOME))
        gls = r.get("gitlab") or []
        if isinstance(gls, str):
            gls = [x for x in re.split(r"[,\s]+", gls) if x]
        if not isinstance(gls, list) or not all(isinstance(g, str) and GL_PATH_RX.match(g) for g in gls):
            raise ValueError("%s: GitLab project in the form group/project" % key)
        profile = str(r.get("profile") or "").strip()
        if profile and not re.match(r"^[A-Za-z0-9_.-]{1,64}$", profile):
            raise ValueError("%s: invalid profile" % key)
        title = str(r.get("title") or "").strip()[:120]
        obs = " ".join(str(r.get("obs") or "").split())
        if obs and not prodcheck.OBS_RX.match(obs):
            raise ValueError("%s: the project name in observability (letters, digits, _ . : / - space)" % key)
        explicit = bool(r.get("explicit"))
        source = str(r.get("source") or ("projects.json" if explicit else ""))[:20]
        row = {"key": key, "path": path, "gitlab": gls, "profile": profile, "title": title, "obs": obs,
               "explicit": explicit, "source": source}
        if explicit and orig and projects_mod.KEY_RX.match(orig):
            row["orig_key"] = orig
        out.append(row)
    return out


def _merged(sec: Section, new: dict, reset: list) -> dict:
    """The section as it will be after this save: stored values (or defaults), the reset
    ones back to their default, the new ones on top - a section validator sees the whole
    thing, not only the fields the GUI changed (it sends just those)."""
    out, docs = {}, {}
    for f in sec.fields:
        if f.type in ("env", "action", "secret", "projects"):
            continue
        p = store_file(f.store)
        if p not in docs:
            docs[p] = _load(p)
        v, present = _get(docs[p], f.jpath())
        out[f.key] = f.default if (not present or f.key in reset) else v
    out.update(new)
    return out


def _audit_value(f: Field, v):
    if f.type == "projects":   # the entries and mappings, not the found checkouts nobody touched
        return sorted(r["key"] + ("=" + r["obs"] if r.get("obs") else "") for r in v or []
                      if r.get("explicit") or r.get("gitlab") or r.get("profile") or r.get("title") or r.get("obs"))
    return v


# ------------------------------------------------------------------ update (POST)
def update(sid: str, values: dict, confirm: list | None = None, reset: list | None = None,
           clear: list | None = None, rev_seen: str | None = None,
           on_change: Callable[[set], None] | None = None) -> dict:
    """Validate and write one section. values: {key: new}; reset: keys back to the default
    (removed from the file); clear: secret keys to delete. Raises SettingsError (400),
    NeedsConfirm / Conflict (409), KeyError (404)."""
    sec = section(sid)
    by_key = {f.key: f for f in sec.fields}
    values, reset, clear, confirm = dict(values or {}), list(reset or []), list(clear or []), set(confirm or [])
    # one key once: the GUI's "default" sends the default in values AND the key in reset (reset wins)
    reset, clear = list(dict.fromkeys(reset)), list(dict.fromkeys(clear))
    values = {k: v for k, v in values.items() if k not in reset and k not in clear}
    for k in list(values) + reset + clear:
        f = by_key.get(k)
        if f is None or f.type in ("env", "action"):
            raise SettingsError("unknown or read-only field %r" % k, {k: "cannot be saved"})
        if k in reset and f.type in ("projects", "secret"):   # no default to go back to: edit / Delete instead
            raise SettingsError("field %r has no default value" % k, {k: "cannot be reset to the default"})
        if k in clear and f.type != "secret":   # Delete is for secrets only; the others go back with reset
            raise SettingsError("field %r cannot be deleted" % k, {k: "only a secret can be deleted"})
    with LOCK, common.PROJECTS_LOCK:   # projects.json has other writers (obs mapping, add checkout): same lock, same order
        if rev_seen is not None and rev_seen != rev(sec):
            raise Conflict("the file changed meanwhile (by hand or from another window) - reload Settings")
        errors, new = {}, {}
        for k, v in values.items():
            f = by_key[k]
            if f.type == "secret" and (v is None or v == ""):
                continue   # an empty secret input = unchanged
            try:
                new[k] = _validate(f, v)
            except ValueError as e:
                errors[k] = str(e)
        if sec.validate and not errors:
            errors.update(sec.validate(_merged(sec, new, reset)) or {})
        if errors:
            raise SettingsError("some fields are not valid", errors)
        docs: dict[str, tuple[dict, str | None]] = {}

        def doc(f: Field):
            p = store_file(f.store)
            if p not in docs:
                docs[p] = (_load(p), _read_text(p))
            return docs[p][0]

        # current values (for the audit and the weakening checks)
        changes, warn = [], []
        for k in list(new) + reset + clear:
            f = by_key[k]
            if f.type == "secret":
                old = _secret_value(store_file(f.store))
                nv = None if k in clear else new.get(k)
                if old != nv:
                    changes.append((f, _SECRET, nv))
                continue
            if f.type == "projects":
                old = _projects_rows(doc(f))
            else:
                ov, present = _get(doc(f), f.jpath())
                old = ov if present else f.default
            nv = f.default if k in reset else new[k]
            if old == nv and (f.type != "security" or (_get(doc(f), f.jpath())[1]
                                                       and doc(f).get("security_rev") == security.REV)) and (k not in reset or f.type == "projects" or not _get(doc(f), f.jpath())[1]):
                continue
            if f.weakens and (w := f.weakens(old, nv)) and k not in confirm:
                warn.append({"key": k, "label": f.label, "warning": w})
            changes.append((f, old, nv))
        if warn:
            raise NeedsConfirm(warn)
        stores, applies = set(), set()
        for f, old, nv in changes:
            path = store_file(f.store)
            if old is _SECRET:
                write_secret(path, nv)
                common.audit({"event": "settings-change", "section": sid, "key": f.key,
                              "secret": "cleared" if nv is None else "set"})
            else:
                data = doc(f)
                if f.type == "projects":
                    _write_projects(data, nv)
                elif f.key in reset:
                    _unset(data, f.jpath())
                    if f.type == "security":
                        data.pop("security_rev", None)
                else:
                    _set(data, f.jpath(), nv)
                    if f.type == "security":   # this choice is final, never read as an older profile
                        data["security_rev"] = security.REV
                common.audit({"event": "settings-change", "section": sid, "key": f.key,
                              "old": _audit_value(f, old), "new": _audit_value(f, nv),
                              "confirmed": f.key in confirm and f.weakens is not None})
            stores.add(f.store)
            applies.add(f.apply)
        for p, (data, text) in docs.items():
            if any(store_file(f.store) == p for f, o, _ in changes if o is not _SECRET):
                write_json_keep(p, data, text)
    if stores and on_change:
        on_change(stores)
    return {"saved": [f.key for f, _, _ in changes], "apply": sorted(applies),
            "apply_text": [APPLY[a] for a in sorted(applies) if a in APPLY], "rev": rev(sec)}


def _write_projects(data: dict, rows: list[dict]) -> None:
    """projects.json from the table: a row added by hand, or a found checkout given a GitLab
    project / profile / title, is an entry of `projects` (unknown keys of an entry stay); the
    mapping to observability goes into the top-level `obs` (an entry's own `obs` moves there).
    A mapping of a key not in the table (a project that is gone) is kept. Start preferences
    move together on rename, including when two projects swap keys in one save."""
    old = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    top = dict(data.get("obs") if isinstance(data.get("obs"), dict) else {})
    prefs = data.get("session_defaults")
    if isinstance(prefs, dict):
        renames = {r["orig_key"]: r["key"] for r in rows if r.get("orig_key") and r["orig_key"] != r["key"]}
        moved = {new: prefs[was] for was, new in renames.items() if was in prefs}
        for was in renames:
            prefs.pop(was, None)
        prefs.update(moved)
    out = {}
    for r in rows:
        was = r.get("orig_key") or r["key"]
        if was != r["key"]:   # renamed: the old key's mapping goes with it
            top.pop(was, None)
        if r["explicit"] or r["gitlab"] or r["profile"] or r["title"]:
            e = dict(old.get(was) or {}) if isinstance(old.get(was), dict) else {}
            e["path"] = r["path"]
            for k in ("gitlab", "profile", "title"):
                if r[k]:
                    e[k] = r[k]
                else:
                    e.pop(k, None)
            e.pop("obs", None)
            out[r["key"]] = e
        if r["obs"]:
            top[r["key"]] = r["obs"]
        else:
            top.pop(r["key"], None)
    data["projects"] = out
    data["obs"] = top


def verify_github(hosts: list, opener=None) -> dict:
    """Who the host's gh login belongs to on each GitHub host (GET /user); never returns the token."""
    import github
    if not github.gh_bin():
        return {"ok": False, "error": "the GitHub CLI (gh) is not installed - brew install gh"}
    who, errs = [], []
    for host in hosts or ["github.com"]:
        github.forget_token(host)   # the button re-asks gh: a fresh login shows at once
        tok = github.token(host)
        if not tok:
            errs.append("%s: not logged in (gh auth login --hostname %s)" % (host, host))
            continue
        try:
            me = github.GitHub(host, tok, opener=opener)._req("GET", "/user") or {}
            who.append("%s on %s" % (str(me.get("login") or "?"), host))
        except github.GitHubError as e:
            errs.append("%s answered %s: %s" % (host, e.status, common.scrub(str(e))[:200]))
    return {"ok": bool(who) and not errs, "username": ", ".join(who), "error": "; ".join(errs)}

def add_root_allow(root: str, patterns: list[str], on_change: Callable[[set], None] | None = None) -> list[str]:
    """Accept isolation-check candidates in ONE mount root: add `patterns` to policy
    library_allow[root] (guard.root_allow). Returns the patterns that were new. The blocked
    start's "Allow in this project" - the user confirmed it there; audited like a Settings change."""
    with LOCK:
        path = store_file("policy")
        text = _read_text(path)
        data = _load(path)
        la = data.get("library_allow") if isinstance(data.get("library_allow"), dict) else {}
        real = os.path.realpath(root)
        key = next((k for k in la if os.path.realpath(os.path.expandvars(os.path.expanduser(k))) == real), root)
        cur = [x for x in la.get(key) or [] if isinstance(x, str)]
        # a found path may hold spaces (PATTERN_RX is for typed patterns; guard matches any segment)
        new = [p for p in dict.fromkeys(patterns) if p and not re.search(r"[\x00\n]", p) and p not in cur]
        if not new:
            return []
        la[key] = cur + new
        data["library_allow"] = la
        write_json_keep(path, data, text)
        common.audit({"event": "settings-change", "section": "scan", "key": "library_allow", "root": key,
                      "added": new, "confirmed": True})
    if on_change:
        on_change({"policy"})
    return new


def verify_gitlab(host: str, opener=None) -> dict:
    """Who the stored token belongs to (GET /user); never returns the token."""
    import gitlab
    tok = gitlab.token()
    if not tok:
        return {"ok": False, "error": "the token is not set"}
    try:
        me = gitlab.GitLab(host, tok, opener=opener)._req("GET", "/user") or {}
    except gitlab.GitLabError as e:
        return {"ok": False, "error": "GitLab answered %s: %s" % (e.status, common.scrub(str(e))[:200])}
    except OSError as e:
        return {"ok": False, "error": "GitLab unreachable: %s" % str(e)[:200]}
    return {"ok": True, "username": str(me.get("username") or ""), "name": str(me.get("name") or "")}
