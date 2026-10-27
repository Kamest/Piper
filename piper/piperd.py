#!/usr/bin/env python3
"""piperd - the Piper daemon.

piperd serves the sessions GUI, a WebSocket per open terminal, and the Guard; optional parts
outside the core come from extensions ($PIPER_EXTENSIONS, extensions.py). PTYs live in ptyhost (restarting piperd never ends a session).

Security model:
  - binds loopback only (PIPER_BIND must be a loopback address) on PIPER_PORT (17895);
    that port must never appear in a sandbox's hostGatewayDomains (K17, Guard checks);
  - every /api call and every WebSocket needs a token from $PIPER_HOME/tokens (mode 600);
    the page itself is tokenless and receives its token in the URL fragment;
  - Host must be 127.0.0.1:<port> or localhost:<port>, and a browser Origin must match
    it, on every request (DNS rebinding / cross-site requests);
  - remote access only through `tailscale serve`: a host named in
    PIPER_ALLOWED_HOSTS (exact names, no wildcard) is accepted as Host, its Origin only as https://;
    with PIPER_ALLOWED_TS_USERS set such a request needs a listed Tailscale-User-Login - on top of
    the token, never instead of it. Without the variable nothing changes (fail-closed);
  - a WebSocket authenticates with its FIRST message {"auth": token} within 3 s - never
    a query-string token (logs, history);
  - CSP: scripts only from self + the boot nonce; inline styles are allowed because
    xterm.js 6 creates <style> elements (13.14).

Routes:
  GET  /                          GUI shell (boot blob with nonce)
  GET  /piper/<file>               static allowlist (web/, including themes.json)
  GET  /api/state                 sessions, pending confirmations, projects (ETag)
  POST /api/sessions              {project, task?, title?, origin?, opts?, agent?, remember_for_project?, image_rebuild?}  ("advisor" = the advisor;
                                   origin "tty" = `piper attach`)
                                   opts?: {docker?, unrestricted?, idea?} - hole --with-docker / -u / IntelliJ MCP, GUI token only
                                   agent?: "claude" (default) | "codex" - the CLI hole starts
  POST /api/sessions/<id>/<act>   stop | approve | retry | forget | fix-excludes | allow-uncovered
                                   | open-terminal (`piper attach <id>` in a new Ghostty window)
  POST /api/pending/<id>          {approve: bool, always?: bool}   always: the card's "Allow always"
  POST /api/projects/import       add the checkouts an extension registers (project_roots)
  GET  /api/sessions/<id>/git     host-side status (+ ?diff=<path>[&untracked=1] for one diff); files hidden
                                   from Claude are marked `sensitive`, their diff is shown (9.6, 2026-10-07)
  POST /api/sessions/<id>/git/commit {message, files}   POST .../git/push (the GUI confirms first)
  POST /api/sessions/<id>/drop    raw image body (Content-Type image/*, <= 10 MiB) -> container path
  GET  /ws/<session id>           terminal stream (binary out, JSON in; `size` / `tty` when a
                                   native terminal attached with `piper attach` sizes the PTY)
  W2 host terminal (hostterm.py, feature module host, off by default; the GUI's full token only):
  POST /api/host-terminals {project, cols?, rows?}   a login shell on the host, no sandbox
  POST /api/host-terminals/<id>/close   GET /ws/host-<hex>   /api/state `host_terms` {on, items}
  GUI workbench (9.6, read-only unless noted; project = key like acme/shop in the query).
  File content (/api/fs/*, /api/git/*, /api/idea/* but status, /api/sessions/<id>/git|turn|turns|diagnostics|
  conversation, /api/conversations/search, /api/stack/logs|job, /api/http/run|runs) needs the GUI's full
  token, not `piper attach`'s. A file hidden from Claude is served like any other (2026-10-07): the rule
  protects the sandbox, which never reaches this port; only `.git` is never served.
  GET  /api/fs/list|read|files|grep ?project=&path=&offset=&limit=&q=&glob=&all=
                                  project files through projectfs; every answer has `policy_unreadable` (+
                                  `policy_error`): the isolation policy cannot be read, nothing is marked hidden
                                  list: entries + `hidden` (hidden from Claude), `noise` (projectfs.NOISE)
                                  read: + `hidden`; 413 larger than 2 MiB, 415 binary
                                  files: {files, truncated, skipped_links, hidden: [paths hidden from Claude]}
                                  grep: + cs=0|1 (case sensitive, default 1), limit (200, max 2000);
                                  {matches: [{path, line, text, hidden}], truncated, skipped_large, skipped_links,
                                  engine: git|python, regex?: pcre|ere} - git grep in the trusted repo root + the
                                  ignored paths by Python; files / grep leave NOISE out unless all=1
  GET  /api/gitlab/issues ?project=&search=&state=   GET /api/gitlab/issue ?project=&iid=
  GET  /api/sessions/<id>/conversation   transcript turns + token usage
  GET  /api/conversations/search ?q=     GET /api/board     GET /api/audit ?session=&limit=
  POST /api/sessions/<id>/send    {text}    typed into the terminal (audited)
  POST /api/sessions/<id>/allow   {domain}  network.allow of the project's .hole/settings.json
  POST /api/sessions/<id>/issue-state   9.12 D: prompt the live session / piperd's summary into the issue's status file
  9.7 (workbench.py):
  GET  /api/fs/read               + `sha` (sha256 of the file) - the lock of /api/fs/write; opening a file
                                   hidden from Claude is audited (gui-read-hidden, no content)
  GET  /api/fs/marks ?project=&path=          changed lines vs HEAD (git diff -U0)
  GET  /api/fs/symbols ?project=&name=&q=     regex definition index (symbols.py)
  GET  /api/fs/raw ?project=&path=            image bytes (png jpg gif webp svg ico bmp, <= 10 MiB)
  POST /api/fs/write {project, path, text, sha}   existing file; 409 when changed / a session works
  (the `*` GUI token; paths spelled as on disk, no symlink, no `.git` at the source or the
  destination - for a directory no entry inside; 409 exists / a path Claude edited in its current turn /
  content hidden from Claude would land where a running sandbox sees it - only an over-mounted directory
  shields what appears inside it after the sandbox's start):
  POST /api/fs/create {project, path, kind: file|dir, text?}   -> {path, sha?}  the parent must exist
  POST /api/fs/scratch {project, ext}   -> {path, sha}   a new development/scratch/scratch[_N].<ext>, kept out of git
  POST /api/fs/rename {project, from, to}   -> {from, to}   file or dir, into an existing dir too
  POST /api/fs/delete {project, path}       -> {trash}      moved to $PIPER_HOME/trash/<id> (7 days)
  POST /api/fs/restore {project, trash}     -> {path}       back where it was, when that is free
  GET  /api/git/diff|log|blame|graph|show ?project=&path=&n=&commit=   (diff, show: text/plain; the whole
                                   repository, only `.git` withheld)
  GET  /api/git/status ?project=   the project's git status without a session: {root, branch, upstream, ahead,
                                   behind, files: [{path (repo-relative), x, y, orig?, sensitive}]}
  GET  /api/git/ci ?project=      GET /api/git/mr-draft ?project=&target=
  POST /api/git/mr {project, title, description, target, draft}   (the GUI confirms first)
  Git window:
  GET  /api/git/branches ?project=   {current, detached, local, remote, worktrees: [{path, branch, key, sessions}]}
  GET  /api/git/rows ?project=&n=&ref=   log --graph as rows {graph, hash?, short, author, date, subject, refs, parents}
  GET  /api/git/commit ?project=&commit=   {hash, parents, author, date, message, files: [{path, old_path?, status,
                                   add, del, binary, withheld}]}   GET /api/git/blob ?...&path=&side=old|new -> {text, exists}
  POST /api/git/branch {project, action: switch|create|delete, name, start?, checkout?, force?}
  POST /api/git/fetch|pull {project}   fetch --all --prune / pull --ff-only (refused while a session works there)
  POST /api/git/worktree {project, action: add|remove, name, branch?, new?, start?}   <base>/.claude/worktrees/<name>,
                                   then project `<base>@<name>`
  POST /api/sessions/<id>/git/message {files}   -> {message, withheld: [path], truncated}   host claude -p (Haiku, no tools)
  POST /api/sessions/<id>/git/mr-message {target}   -> {title, description, withheld, truncated}   the same, from the branch
  POST /api/sessions/<id>/git/commit {..., amend: true}   replaces HEAD; refused once HEAD is on a remote branch
  GET  /api/stack ?project=       GET /api/stack/logs ?project=&service=&tail= (text/plain)
  POST /api/stack/restart {project, service}   through the dev stack provider (stackview.py)
  POST /api/stack/action {project, verb, service?}   start stop restart down up build rebuild only
  POST /api/stack/mode {project, mode}   a linked worktree: the main checkout's stack or its own
  POST /api/stack/setup {project}        a linked worktree's own stack: the provider's adopt (approval)
  GET  /api/stack/job ?project=&id=&offset=   POST /api/stack/cancel {project, id}   a job of that stack
  GET  /api/image   POST /api/image/rebuild {on}   9.54: hole's agent images; arm / cancel `hole start -r`
                                    for the next start of any session (/api/state image_rebuild {at, session?})
  POST /api/interrupted {seen: <notice id>} | {dismiss: [ids] | "all"}   9.141: sessions a restart interrupted
                                    (/api/state interrupted_notice; a session's `interrupted` {at, was, cause, resumes})
  GET  /api/sessions/<id>/turns   GET /api/sessions/<id>/turn ?from=&to=[&diff=<path>|.]
  GET  /api/sessions/<id>/turns/file ?path=   turns that changed one file
  /api/state: a session's `fsedit` {seq, paths, unknown} - files its agent edited (PostToolUse, L6)
  GET  /api/sessions/<id>/diagnostics   errors of the session's last build (diagnostics.py)
  GET  /api/idea/status   GET /api/idea/problems ?project=&path=   GET /api/idea/symbol ?...&line=&col=
  GET  /api/usage                 tokens per day and per project over all transcripts
  /api/state: `limits` {status, plan, session, week, scoped} - the Claude plan limits under the sessions (limits.py; null while the usage module is off, D16)
  9.8 GitLab inbox (inbox.py; metadata of issues / MRs / notes only, never file content;
  GitLab writes only from these GUI routes, never from a sandbox tool):
  GET  /api/inbox                 items, rules, limits (/api/state carries `inbox` {v, status, new, total, needs})
  GET  /api/inbox/item ?key=      preview: description + notes (scrubbed)
  GET  /api/sessions/<id>/draft   the session's comment draft in development/ (never a hidden file)
  POST /api/inbox/refresh {}      POST /api/inbox/seen {keys|all}   POST /api/inbox/unseen {key}
  POST /api/inbox/done {key}      pending to-dos of the item -> done (the GUI confirms first)
  POST /api/inbox/start {key, mode, project?, note_id?, task?, session?}   new session / follow-up
  POST /api/inbox/comment {key, body}   POST /api/inbox/rules {rules}   POST /api/projects/add {path, gl}
  9.12 C morning production check (prodcheck.py; /api/state carries `prod` {v, status, new, on}):
  GET  /api/prod                  status, schedule, mapping, the latest report, mapping suggestions
  POST /api/prod/run {mode?}      check (or suggest a mapping) now   POST /api/prod/mapping {project, obs}
  POST /api/prod/mute {key, days} (0 = forever)   POST /api/prod/unmute {key}   POST /api/prod/investigate {key}
  POST /api/prod/ignore {project, on?}   "Do not map": never suggest a mapping for it (on=false undoes it)
  9.14 Feed (feed.py, off by default; /api/state carries `feed` {on, v, open, unread, chat_queued}):
  /api/state `queue` {actions}: the open actions for the Overview queue, also with the Feed off (Feed.queue_summary)
  GET  /api/feed                  open actions, events, seen, mutes   GET /api/feed/chat   the Global agent's chat
  POST /api/feed/seen {ts}        POST /api/feed/dismiss {key}   POST /api/feed/undismiss {}   POST /api/feed/mute {key, minutes} (0 = unmute)
  POST /api/feed/chat {text}      queue a message   POST /api/feed/chat/cancel {id}   POST /api/feed/chat/retry {id}   POST /api/feed/chat/start {}
  9.12 E Mattermost (incoming webhook, outbound only) and the half-written prompt:
  GET  /api/mattermost            status (configured, quiet hours, GUI visible, last error)
  POST /api/mattermost/test {}    one test message   POST /api/presence {visible}   GUI heartbeat
  POST /api/sessions/<id>/input-clear   "Send now": piperd may type its waiting text now
  9.12 A Timesheet (timesheet.py; overview only, never written to GitLab or elsewhere):
  GET  /api/timesheet ?week=2026-W40   GET /api/timesheet/settings
  (options: PIPER_HOME/timesheet/config.json, the Settings page)   POST /api/timesheet/focus {session}
  POST /api/timesheet/edit {week, day, key, minutes|null, text|null}
  POST /api/timesheet/manual {week, id?, day, label, minutes, text, delete?}   POST /api/timesheet/assign {session, key, title?}
  .http requests (httpclient.py; run on the host by ijhttp, POSTs need the `*` GUI token):
  GET  /api/http/files ?project=             every .http file of the project: requests, last run (the HTTP tab)
  GET  /api/http/envs ?project=&path=        environments (names, class), requests, ijhttp status
  GET  /api/http/runs ?project=&path=        recent runs (summaries)   GET /api/http/run ?id=   one run
  POST /api/http/run {project, path, env, index?, sha?}   runs now (the click is the approval), the run back;
                                   sha = of the text the GUI showed: 409 when the file changed since
  POST /api/http/share {id, on}   a session's http_last_result sees the bodies   POST /api/http/install {}
  POST /api/http/run {…, index, save_to}   the request's whole response into a project file (a PDF)
  GET  /api/http/envcfg ?project=&path=   environments: Piper's settings (tunnel, certificate), the tunnel in
                                   effect + its state, the env files (hidden?), dev stack tunnel targets
  POST /api/http/envcfg {project, path, env, cfg: {tunnel: {kind: auto|none|ssh|stack, ssh?, forward?,
                                   target?, port?}, verify_tls: true|false|null}}   stored in $PIPER_HOME/http
  POST /api/http/tunnel {project, path, env, op: open|close|test}
  databases (dbtools.py; every route needs the `*` GUI token - results are raw, unmasked data):
  GET  /api/db/all   every connection of every project, grouped (the Database view)
  GET  /api/db/connections ?project=&session=   connections (no passwords), tunnel targets, IDEA harvest, drivers
  GET  /api/db/meta ?project=&connection=&what=schemas|objects|describe&schema=&name=&session=&refresh=1
  GET  /api/db/history ?project=&session=&limit=   GET /api/db/status
  POST /api/db/query {project, connection, sql, schema?, params?, max_rows?, offset?, session?, explain?}   read path
  POST /api/db/fetch {result_id, offset, max_rows}   POST /api/db/cancel {project, connection, owner?}
                                   owner "" = the GUI's own statements; a session id = that session's reads (L7)
  GET  /api/db/running ?project=&connection=   {running: [{connection, owner, title, op, since, cancellable}]}
  POST /api/db/execute {project, connection, statements, schema?, session?, confirm?, preview_only?}   the click
       is the approval; prod needs confirm "prod"
  POST /api/db/save {project, connection: {...}, password?}   POST /api/db/delete {project, name}
  POST /api/db/unpin {name}   POST /api/db/tunnel {project, connection, op: open|close}   POST /api/db/install {}
  POST /api/db/tx {project, connection, op: exec|commit|rollback|state, statements?, confirm?, max_rows?}   the console's
       read-write mode: one open transaction (its own worker) until commit / rollback
  9.13 Settings (settings.py; the `*` GUI token only, secrets never returned):
  GET  /api/settings              sections with values, secrets as set/unset
  POST /api/settings {section, values, reset?, clear?, confirm?, rev?}   409 needs_confirm / conflict
  POST /api/settings/verify-gitlab {}   who the stored GitLab token belongs to
  POST /api/settings/verify-github {}   who the host's gh login belongs to (W23)

Usage: piperd.py [--print-url [--remote]]   --remote: https://<first PIPER_ALLOWED_HOSTS>/ with the remote token
Env:   PIPER_HOME (~/.piper) PIPER_PORT (17895) PIPER_BIND (127.0.0.1) PIPER_HOLE_BIN (hole)
       PIPER_ALLOWED_HOSTS PIPER_ALLOWED_TS_USERS (comma lists, unset = loopback only; 9.56)
       PIPER_DOCKER_BIN (docker) PIPER_MAX_SESSIONS (6) PROJECT_PATH PIPER_EXTENSIONS
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import doctor  # noqa: E402
import extensions  # noqa: E402
import features  # noqa: E402
import gitlab  # noqa: E402
import gitops  # noqa: E402
import hostterm  # noqa: E402
import limits  # noqa: E402
import codexlimits  # noqa: E402
import projectfs  # noqa: E402
import projects  # noqa: E402
import security  # noqa: E402
import settings  # noqa: E402
import remotes  # noqa: E402
import providers  # noqa: E402
import wsproto  # noqa: E402
from ptyclient import PtyClient, PtyHostError, b64  # noqa: E402
import ttyattach  # noqa: E402

VERSION = 1
PORT = int(os.environ.get("PIPER_PORT", "17895"))
BIND = os.environ.get("PIPER_BIND", "127.0.0.1")
WEB = os.path.join(common.PIPER_DIR, "web")
THEMES = os.path.join(WEB, "themes.json")
PUBLIC = ("index.html", "app.css", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "dist/", "vendor/",
          "img/")   # img/modules/<module>.svg: the setup card's gallery (scripts/piper-module-art.py)
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
        ".map": "application/json", ".json": "application/json", ".svg": "image/svg+xml",
        ".webmanifest": "application/manifest+json", ".woff2": "font/woff2", ".png": "image/png"}
MANAGER = None   # sessions.Manager, created in main()
DROP_MAX = 10 << 20   # same bound as sessions.DROP_MAX (checked again there)


def _security_notice() -> dict | None:
    """/api/state `security_notice`: the one-time Security screen (security.notice) for a user who had
    finished the setup before the update; None once dismissed, for a new user and without the tree."""
    f = features.load()
    if not security.ENABLED or not f["onboarded"] or security.NOTICE in f["seen"]:
        return None
    pol = common.read_json(common.POLICY_FILE, {})
    return security.notice(pol if isinstance(pol, dict) else {})


def _settings_changed(stores: set) -> None:
    """A saved Settings section: piperd re-reads policy.json (new sessions, the L5 watcher)."""
    if "policy" in stores and hasattr(MANAGER, "reload_policy"):
        MANAGER.reload_policy()
    if "json:db/config" in stores and hasattr(MANAGER, "_db_invalidate_agent_results"):
        MANAGER._db_invalidate_agent_results()


_DOCTOR: dict = {}
_DOCTOR_LOCK = threading.Lock()


CONTENT_SCOPE = "file content needs the GUI token"
# what `piper attach` (ttyattach.py) reads of a session in /api/state - a whitelist: the task, the prompts,
# the GitLab issue, edited paths and agent bookkeeping are the session's content (N4, as /api/board in N3)
TTY_SESSION_KEYS = ("id", "kind", "project", "path", "title", "state", "phase", "detail", "blocks", "pid", "rc",
                    "notice", "claude_session", "agent", "alive", "tty", "origin", "created", "since", "hidden")


def _doctor(deep: bool, fresh: bool = False) -> dict:
    """doctor.report, the quick one cached 30 s (`hole version` + `docker info` per poll would be
    seconds); one run at a time."""
    key = json.dumps(features.load()["modules"], sort_keys=True)   # a module turned on adds its rows
    with _DOCTOR_LOCK:
        if fresh:   # Check again: nothing from a cache
            _DOCTOR.clear()
            doctor._SLOW.clear()
        hit = _DOCTOR.get("quick")
        if not deep and hit and hit[0] == key and hit[1]["at"] > time.time() - 30:
            return hit[1]
        rep = doctor.report(deep)
        _DOCTOR["quick"] = (key, rep)
        return rep


class BadQuery(Exception):
    """A malformed query parameter: 400 (not a ValueError - the GUI reads 409 as refused)."""


def qint(arg, name: str, default: int) -> int:
    """An int query parameter ("" = default); BadQuery "invalid <name>" rather than Python's message."""
    v = arg(name, "")
    try:
        return int(v) if v else default
    except ValueError:
        raise BadQuery("invalid %s" % name) from None


def unknown(e: KeyError) -> str:
    """The 404 text of a KeyError - "unknown project 'x'" is not prefixed again."""
    msg = str(e.args[0] if e.args else e)
    return msg if msg.startswith("unknown") else "unknown: %s" % msg


def pty_client(on_event) -> PtyClient:
    """One ptyhost connection per open terminal (tests replace this with a socketpair)."""
    return PtyClient(common.PTY_SOCK, on_event=on_event)


def allowed_hosts() -> set[str]:
    return {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT}


def remote_names() -> list[str]:
    """PIPER_ALLOWED_HOSTS: the tailnet names `tailscale serve` answers on (9.56), read per call like the
    tokens. Exact host names only; anything else makes piperd refuse to start (check_remote_names)."""
    return [h.strip().lower() for h in os.environ.get("PIPER_ALLOWED_HOSTS", "").split(",") if h.strip()]


def check_remote_names() -> str | None:
    """Why PIPER_ALLOWED_HOSTS is unusable, or None: no wildcard, port, path, IP literal or loopback name."""
    for h in remote_names():
        if not all(c.isalnum() or c in "-." for c in h) or h.startswith(".") or h.endswith(".") or "." not in h:
            return "PIPER_ALLOWED_HOSTS: %r is not an exact host name" % h
        try:
            ipaddress.ip_address(h)
            return "PIPER_ALLOWED_HOSTS: %r is an IP address, name the tailnet host" % h
        except ValueError:
            pass
        if h == "localhost" or h.endswith(".localhost"):
            return "PIPER_ALLOWED_HOSTS: %r is loopback" % h
    return None


def is_piper_url(url: str) -> bool:
    """Recognize this daemon's exact loopback/tailnet origins, never a lookalike host or another port."""
    try:
        u = urlsplit(url)
        return (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1") and u.port == PORT
                or u.scheme == "https" and u.hostname in remote_names() and u.port in (None, 443))
    except ValueError:
        return False


def remote_hosts() -> set[str]:
    """Host header values of the remote names: bare (the https default port) and with :443."""
    return {v for h in remote_names() for v in (h, h + ":443")}


def ts_users() -> set[str]:
    return {u.strip().lower() for u in os.environ.get("PIPER_ALLOWED_TS_USERS", "").split(",") if u.strip()}


def csp(nonce: str) -> str:
    return ("default-src 'self'; script-src 'self' 'nonce-%s'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'; manifest-src 'self'" % nonce)


def static_path(rel: str) -> str | None:
    """Resolve /piper/<rel> inside web/ (allowlisted subtrees and extensions only)."""
    if rel == "themes.json":
        return THEMES
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or "\\" in rel:
        return None
    if not any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in PUBLIC):
        return None
    if os.path.splitext(rel)[1] not in MIME:
        return None
    path = os.path.realpath(os.path.join(WEB, rel))
    if not path.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(path):
        return None
    return path


_gui_build_cache = (None, "")


def gui_build():
    """Identify served GUI revisions without rehashing unchanged files on every poll."""
    global _gui_build_cache
    files = [os.path.join(WEB, p) for p in ("dist/main.js", "dist/quiet.js", "app.css")]
    try:
        fingerprint = tuple((os.stat(p).st_mtime_ns, os.stat(p).st_size) for p in files)
        if fingerprint != _gui_build_cache[0]:
            digest = hashlib.sha256()
            for path in files:
                with open(path, "rb") as stream:
                    digest.update(stream.read())
            _gui_build_cache = (fingerprint, digest.hexdigest()[:20])
        return _gui_build_cache[1]
    except OSError:
        return ""


def shell(remote: bool = False) -> tuple[bytes, str]:
    nonce = secrets.token_urlsafe(16)
    with open(os.path.join(WEB, "index.html")) as f:
        page = f.read()
    boot = {"version": VERSION, "guiBuild": gui_build(), "port": PORT,
            "origins": sorted({"http://" + h for h in allowed_hosts()} | {"https://" + h for h in remote_names()}),
            # the Stacks link: the dev stack provider's own page, if it has one
            # (none through `tailscale serve`: a loopback URL means nothing on the remote device, 9.56)
            "dashboardUrl": "" if remote or not hasattr(MANAGER, "_stack_dash") else MANAGER._stack_dash()}
    try:
        with open(THEMES) as f:
            boot["themes"] = json.load(f)
    except (OSError, ValueError):
        boot["themes"] = {}
    # N1: the chosen theme paints the first frame (the state poll comes later - no dark flash before Light)
    boot["theme"] = str((getattr(MANAGER, "policy", None) or {}).get("theme") or "dark")
    blob = json.dumps(boot).replace("<", "\\u003c")
    page = page.replace("<!--PIPER_BOOT-->",
                        '<script nonce="%s">window.__PIPER__=%s</script>' % (html.escape(nonce), blob))
    page = page.replace("<!--PIPER_NONCE-->", html.escape(nonce))
    return page.encode(), nonce


def terminal_post(parts: list[str]) -> bool:
    """The only POST routes a token without the GUI's full scope may call (`piper attach`'s, label
    tty, and a scoped one): start a session (start options still need the GUI token) and
    retry one - what a terminal client needs. Every other POST, a new one too, is the GUI's."""
    return parts == ["api", "sessions"] or (len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "retry")


class Handler(BaseHTTPRequestHandler):
    server_version = "piperd/%d" % VERSION
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, fmt, *args):  # keep the log to errors and security decisions
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):   # a reload drops an idle keep-alive connection
            self.close_connection = True

    # ------------------------------------------------------------ responses (all framed)
    def _finish(self, code: int, ctype: str, body: bytes, headers: dict | None = None) -> None:
        if len(body) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)
            headers = dict(headers or {}, **{"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        try:
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):   # the tab went away (reload, presence beacon)
            self.close_connection = True

    def send_response(self, code, message=None):
        self._responded = True   # a status line went out (_finish, 304, the WebSocket 101): no 500 after it
        super().send_response(code, message)

    def _guarded(self, route) -> None:
        """Run a do_* body; an exception no route maps (a bug) becomes one JSON 500 instead of a connection
        closed without an answer (F-N10-7). The traceback and the route go to stderr = piperd.log, never the
        request headers (the bearer token) and nothing of the exception to the client."""
        self._responded = False
        try:
            route()
        except (ConnectionResetError, BrokenPipeError):
            raise   # handle() closes quietly
        except Exception:  # noqa: BLE001 - the last net of every route
            print("piperd: unhandled error in %s %s\n%s" % (self.command, urlsplit(self.path).path,
                                                           traceback.format_exc()), file=sys.stderr, flush=True)
            self.close_connection = True
            if not self._responded:
                self._err(500, "internal error - see piperd.log")

    def _json(self, code: int, data) -> None:
        self._finish(code, "application/json", json.dumps(data, ensure_ascii=False).encode())

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    # ------------------------------------------------------------ gates
    def _remote(self) -> bool:
        """A request through `tailscale serve` (9.56): Host or X-Forwarded-Host is a PIPER_ALLOWED_HOSTS
        name, or Tailscale's identity headers came along - whether serve keeps the Host or rewrites it to
        loopback. Forging any of these only makes a local request face the stricter rules."""
        rh = remote_hosts()
        h = self.headers
        return (h.get("Host", "").lower() in rh or h.get("X-Forwarded-Host", "").lower() in rh
                or h.get("Tailscale-User-Login") is not None or h.get("Tailscale-Headers-Info") is not None)

    def _host_ok(self) -> bool:
        host = self.headers.get("Host", "")
        rh = remote_hosts()
        if host not in allowed_hosts() and host.lower() not in rh:
            self._err(421, "unexpected Host header")
            return False
        origin = self.headers.get("Origin")
        if not self._remote():
            if origin is not None and urlsplit(origin).netloc not in allowed_hosts():
                self._err(403, "cross-origin request refused")
                return False
            return True
        if not rh:   # Tailscale headers but no remote name configured: nothing remote is allowed
            self._err(403, "remote access is off (PIPER_ALLOWED_HOSTS)")
            return False
        o = urlsplit(origin) if origin is not None else None
        if o is not None and (o.scheme != "https" or o.netloc.lower() not in rh):
            self._err(403, "cross-origin request refused")
            return False
        users = ts_users()
        if users and self.headers.get("Tailscale-User-Login", "").strip().lower() not in users:
            common.audit({"event": "remote-user-refused", "path": urlsplit(self.path).path})
            self._err(403, "this tailnet user may not use Piper")
            return False
        return True

    def _token(self) -> dict | None:
        h = self.headers.get("Authorization", "")
        tok = h[7:].strip() if h.startswith("Bearer ") else ""
        return common.load_tokens().get(tok) if tok else None

    def _auth(self) -> dict | None:
        t = self._token()
        if t is None:
            self._err(401, "missing or unknown bearer token")
        return t

    # ------------------------------------------------------------ GET
    def do_GET(self):
        self._guarded(self._get)

    def _get(self):
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            body, nonce = shell(self._remote())
            self._finish(200, MIME[".html"], body, {"Content-Security-Policy": csp(nonce)})
            return
        if path.startswith("/piper/"):
            f = static_path(unquote(path[len("/piper/"):]))
            if f is None:
                self._err(404, "not found")
                return
            st = os.stat(f)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            with open(f, "rb") as fh:
                body = fh.read()
            self._finish(200, MIME[os.path.splitext(f)[1]], body,
                         {"ETag": etag, "Cache-Control": "no-cache"})
            return
        if path.startswith("/ws/"):
            self._websocket(unquote(path[len("/ws/"):]))
            return
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) == 3 and parts[:2] == ["api", "local-provider"]:
            try:
                providers.active_grant(parts[2], self.headers.get("Authorization", "").removeprefix("Bearer "))
                self._json(200, {"ok": True})
            except PermissionError:
                self._err(403, "local provider is revoked or unavailable")
            return
        if self._auth() is None:
            return
        if parts[:2] == ["api", "mobile"]:
            self._mobile_api(parts)
            return
        if self._environment_proxy():
            return
        if path == "/api/environments":
            if self._gui_scope():
                self._json(200, {"environments": remotes.entries(), "providers": [{k:g[k] for k in ("environment", "remote_project", "local_project", "enabled")} for g in providers.grants()]})
            return
        parts = [unquote(p) for p in path.strip("/").split("/")]
        if len(parts) == 4 and parts[:2] == ["api", "environments"] and parts[3] == "sessions":
            if not self._gui_scope():
                return
            try:
                if parts[2] == "local":
                    rows = MANAGER.list()
                else:
                    reply = remotes.proxy(parts[2], "GET", "/api/state")
                    if reply["status"] != 200:
                        raise remotes.Unavailable("Server sessions are unavailable; no process state was inferred.")
                    rows = json.loads(base64.b64decode(reply["body"]))["sessions"]
                fields = ("id", "title", "project", "agent", "state", "runtime")
                self._json(200, {"sessions": [{k: row.get(k) for k in fields} for row in rows], "checked_at": time.time()})
            except (ValueError, KeyError, remotes.Unavailable):
                self._err(502, "Sessions unavailable; the server may still be running.")
            return
        if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "git":
            if not self._gui_scope(CONTENT_SCOPE):
                return
            q = parse_qs(url.query)
            try:
                if "push" in q:   # the Push dialog: what a push would send
                    self._json(200, MANAGER.git_push_preview(parts[2]))
                elif "diff" in q:
                    text = MANAGER.git_diff(parts[2], q["diff"][0] or None, q.get("untracked", ["0"])[0] == "1")
                    self._finish(200, "text/plain; charset=utf-8", text.encode())
                else:
                    self._json(200, MANAGER.git_status(parts[2]))
            except KeyError:
                self._err(404, "unknown session")
            except (gitops.GitError, ValueError) as e:   # ValueError: a session without a working tree (F-N10-7)
                self._err(409, str(e))
            return
        if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "sandbox-file":
            # a file only the sandbox has (its /tmp scratchpad): read-only, the GUI token only
            if not self._gui_scope("reading a sandbox's file needs the GUI token"):
                return
            try:
                self._text(MANAGER.sandbox_file(parts[2], parse_qs(url.query).get("path", [""])[0]))
            except KeyError:
                self._err(404, "unknown session")
            except ValueError as e:
                self._err(409, str(e))
            return
        if self._workbench(parts, parse_qs(url.query)):
            return
        if parts == ["api", "doctor"]:   # 9.20: the setup card; deep = the K18 mount test (seconds)
            q = parse_qs(url.query)
            deep = q.get("deep", ["0"])[0] == "1"
            if deep and not self._gui_scope("the mount test runs a container: the GUI token only"):
                return
            self._json(200, _doctor(deep, q.get("fresh", ["0"])[0] == "1"))
            return
        if parts == ["api", "image"]:   # 9.54: the Setup row - hole's agent images (docker image ls / ps)
            if self._gui_scope():
                self._json(200, doctor.image_info())
            return
        if parts == ["api", "settings"]:
            if self._gui_scope():
                self._json(200, settings.describe())
            return
        if path == "/api/ping":
            health = {"ok": True, "version": VERSION,
                      "pid": os.getpid(), "capabilities": {"quiet": getattr(MANAGER, "quiet", None) is not None},
                      "project_root": os.path.realpath(os.environ["PROJECT_PATH"]) if os.environ.get("PROJECT_PATH") else None}
            if not health["capabilities"]["quiet"]:
                cls = type(MANAGER)
                module = sys.modules.get(cls.__module__)
                init = getattr(cls.__init__, "__code__", None)
                quiet_init = getattr(getattr(cls, "_quiet_init", None), "__code__", None)
                health["quiet_diagnostics"] = {
                    "manager": cls.__module__ + "." + cls.__qualname__,
                    "module_file": getattr(module, "__file__", None),
                    "cache_file": getattr(module, "__cached__", None),
                    "initializer_file": getattr(init, "co_filename", None),
                    "initializer_calls_quiet": "_quiet_init" in getattr(init, "co_names", ()),
                    "initializer_hash": hashlib.sha256(init.co_code).hexdigest() if init else None,
                    "class_has_quiet_initializer": hasattr(cls, "_quiet_init"),
                    "quiet_initializer_file": getattr(quiet_init, "co_filename", None),
                    "mro": [c.__module__ + "." + c.__qualname__ for c in cls.__mro__],
                }
            self._json(200, health)
        elif path == "/api/state":
            tty = (self._token() or {}).get("label") == "tty"
            if not tty:   # `piper attach` polls too: it is not the GUI
                MANAGER.gui_polled()
            pend = MANAGER.pending_list()
            if tty:   # `piper attach` only rings for a card: its summary / payload hold diffs and DB rows (L2)
                pend = [{k: x.get(k) for k in ("id", "session", "title", "kind")} for x in pend]
            rows = MANAGER.list()
            if tty:
                rows = [{k: r[k] for k in TTY_SESSION_KEYS if k in r} for r in rows]
            st = {"sessions": rows, "pending": pend,
                  "pending_gone": [] if tty else MANAGER.pending_gone_list(),   # F17: "cancelled - nothing was sent"
                  # `piper attach` reads sessions / projects / pending only: no GitLab text, no Feed (L1 raw)
                  "inbox": {} if tty else MANAGER.inbox_summary(), "prod": {} if tty else MANAGER.prod_summary(),
                  "feed": {} if tty else MANAGER.feed_summary(), "queue": {} if tty else MANAGER.queue_summary(),
                  "start_opts_default": MANAGER.start_opts_default(),
                  "default_runtime": os.environ.get("PIPER_DEFAULT_RUNTIME", "hole"),
                  "gui_build": gui_build(),
                  "agent_availability": features.session_agents(),
                  "environment": {"id": "local", "label": "This computer", "host": "", "mode": "local"},
                  "image_rebuild": {} if tty else MANAGER.image_rebuild(),   # 9.54
                  "interrupted_notice": None if tty else MANAGER.interrupted_notice(),   # 9.141
                  "project_session_defaults": projects.session_defaults(),
                  "start_opts_enforced": [k for k, policy_key in
                                         (("unrestricted", "unrestricted_network"), ("idea", "idea_in_sessions"))
                                         if (getattr(MANAGER, "policy", None) or {}).get(policy_key)],
                  # W2: the host shells - the GUI's only (`piper attach` and a scoped token never see them)
                  "host_terms": MANAGER.hosts.summary() if self._full_scope() and hasattr(MANAGER, "hosts") else {},
                  "quiet": MANAGER.quiet_summary() if not tty and hasattr(MANAGER, "quiet_summary") else None,
                  "features": features.summary(), "security_notice": None if tty else _security_notice(), "stack_up": features.on("stack") and MANAGER._stack_up(), "limits": limits.summary() if limits.active() else None,
                  "codex_limits": codexlimits.summary() if codexlimits.active() else None,
                  "gitlab_host": "" if tty else MANAGER.gitlab_host(),
                  "terminal_app": "" if tty else ttyattach.terminal_name(),   # D4: "Attach in <app>"
                  "gui_flags": {**{k: bool((getattr(MANAGER, "policy", None) or {}).get(k)) for k in
                                   ("open_links_on_host", "clipboard_arm_enter", "restore_view",
                                    "term_click_opens_files", "nav_history", "commit_requires_review")},
                                "git_hole": ((getattr(MANAGER, "policy", None) or {}).get("git_mode") or "hole") == "hole",
                                "theme": str((getattr(MANAGER, "policy", None) or {}).get("theme") or "dark"),   # N1
                                **{k: (getattr(MANAGER, "policy", None) or {}).get(k) is not False
                                   for k in ("unlocks_required", "attestation", "gitlab_notifications")},
                                "l5_watch": (getattr(MANAGER, "policy", None) or {}).get("l5_action") != "off"},
                  "projects": [{**{k: p[k] for k in ("key", "title", "path", "gitlab", "exists", "source")},
                                "issue_tracker": getattr(MANAGER, "issue_tracker", lambda p: None)(p)}
                               for p in projects.load(os.environ.get("PROJECT_PATH", "")).values()]}
            raw = json.dumps(st, ensure_ascii=False, sort_keys=True).encode()
            etag = '"%s"' % hashlib.sha1(raw).hexdigest()[:16]
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self._finish(200, "application/json", raw, {"ETag": etag})
        else:
            self._err(404, "not found")

    def _workbench(self, parts: list[str], q: dict) -> bool:
        """GET routes of the GUI workbench; False when the path is not one of them."""
        arg = lambda k, d="": (q.get(k) or [d])[0]   # noqa: E731
        # the inbox / Feed / GitLab texts (raw since L1), a comment draft and the Feed's card bodies: content
        # N3 review: the board (each session's last answer / prompt, S6) and the production report (L1, as in
        # /api/state) too
        if (parts in (["api", "inbox"], ["api", "feed"], ["api", "feed", "chat"], ["api", "inbox", "item"],
                      ["api", "gitlab", "issues"], ["api", "gitlab", "issue"], ["api", "board"], ["api", "prod"],
                      ["api", "audit"], ["api", "timesheet"])   # N4: git_run argv / titles; the prompts as notes
                or (len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "draft")) \
                and not self._gui_scope(CONTENT_SCOPE):
            return True
        try:
            if self._workbench97(parts, arg):
                pass
            elif parts == ["api", "inbox"]:
                self._json(200, MANAGER.inbox())
            elif parts == ["api", "prod"]:
                self._json(200, MANAGER.prod())
            elif parts == ["api", "feed"]:
                self._json(200, MANAGER.feed())
            elif parts == ["api", "feed", "chat"]:
                self._json(200, MANAGER.chat())
            elif parts == ["api", "inbox", "item"]:
                self._json(200, MANAGER.inbox_item(arg("key")))
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "draft":
                self._json(200, MANAGER.session_draft(parts[2]))
            elif len(parts) == 3 and parts[:2] == ["api", "fs"]:
                qint(arg, "offset", 0), qint(arg, "limit", 0)   # parsed by the Manager: a 400 here, not its 409
                if not self._gui_scope(CONTENT_SCOPE):
                    return True
                self._json(200, MANAGER.fs(arg("project"), parts[2], {k: v[0] for k, v in q.items()}))
            elif parts == ["api", "gitlab", "issues"]:
                self._json(200, MANAGER.issues(arg("project"), arg("search"), arg("state", "opened"), arg("gl")))
            elif parts == ["api", "gitlab", "issue"]:
                self._json(200, MANAGER.issue(arg("project"), qint(arg, "iid", 0), arg("gl")))
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "conversation":
                if not self._gui_scope(CONTENT_SCOPE):   # S6: tool results hold file content
                    return True
                self._json(200, MANAGER.conversation(parts[2]))
            elif parts == ["api", "conversations"]:
                if not self._gui_scope("conversation history needs the GUI token"):
                    return True
                if arg("external") not in ("", "0", "1"):
                    self._err(400, "invalid external history filter")
                    return True
                self._json(200, MANAGER.conversations(arg("q"), arg("project"), arg("agent"), arg("period"), qint(arg, "offset", 0), arg("external") == "1"))
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "preview":
                if not self._gui_scope("conversation history needs the GUI token"):
                    return True
                self._json(200, MANAGER.conversation_preview(parts[2]))
            elif parts == ["api", "conversations", "search"]:
                if not self._gui_scope(CONTENT_SCOPE):
                    return True
                self._json(200, {"hits": MANAGER.search_conversations(arg("q"))})
            elif parts == ["api", "board"]:
                self._json(200, {"cards": MANAGER.board()})
            elif parts == ["api", "audit"]:
                self._json(200, {"events": MANAGER.audit_log(arg("session") or None, qint(arg, "limit", 300))})
            elif parts == ["api", "mattermost"]:
                self._json(200, MANAGER.mm_status())
            elif parts == ["api", "timesheet"]:
                self._json(200, MANAGER.timesheet(arg("week")))
            elif parts == ["api", "timesheet", "settings"]:
                self._json(200, MANAGER.ts_settings())
            elif parts[:2] == ["api", "db"] and len(parts) == 3:
                if not self._gui_scope("the database view needs the GUI token"):
                    return True
                sess = arg("session")
                if parts[2] == "all":
                    self._json(200, MANAGER.db_gui_all())
                elif parts[2] == "connections":
                    self._json(200, MANAGER.db_gui_connections(arg("project"), sess))
                elif parts[2] == "meta":
                    self._json(200, MANAGER.db_gui_meta(arg("project"), arg("connection"), arg("what"), arg("schema"),
                                                        arg("name"), sess, arg("refresh") == "1"))
                elif parts[2] == "history":
                    self._json(200, MANAGER.db_gui_history(arg("project"), sess, qint(arg, "limit", 100)))
                elif parts[2] == "status":
                    self._json(200, MANAGER.db_status())
                elif parts[2] == "running":   # L7: what runs now, the GUI's and the sessions'
                    self._json(200, MANAGER.db_gui_running(arg("project"), arg("connection"), sess))
                else:
                    return False
            elif parts == ["api", "http", "envs"]:
                if not self._gui_scope(CONTENT_SCOPE):   # the URLs with the private env values filled in (L3c)
                    return True
                self._json(200, MANAGER.http_gui_envs(arg("project"), arg("path")))
            elif parts == ["api", "http", "envfile"]:
                if not self._gui_scope(CONTENT_SCOPE):
                    return True
                self._json(200, MANAGER.http_gui_envfile(arg("project"), arg("path")))
            elif parts == ["api", "http", "envcfg"]:
                if not self._gui_scope("environment settings need the GUI token"):
                    return True
                self._json(200, MANAGER.http_gui_envcfg(arg("project"), arg("path"), arg("targets") == "1"))
            elif parts == ["api", "http", "files"]:
                if not self._gui_scope(CONTENT_SCOPE):   # request lines, of files hidden from Claude too
                    return True
                self._json(200, MANAGER.http_gui_files(arg("project")))
            elif parts == ["api", "http", "runs"]:
                if not self._gui_scope(CONTENT_SCOPE):   # S6: responses of the project's requests
                    return True
                self._json(200, {"runs": MANAGER.http_runs(arg("project"), arg("path"), qint(arg, "limit", 30)),
                                 **MANAGER.http_status()})
            elif parts == ["api", "http", "run"]:
                if not self._gui_scope(CONTENT_SCOPE):
                    return True
                self._json(200, MANAGER.http_run_record(arg("id")))
            else:
                return False
        except BadQuery as e:
            self._err(400, str(e))
        except KeyError as e:
            self._err(404, unknown(e))
        except projectfs.TooLarge as e:   # K13: it exists - not a 404
            self._err(413, str(e))
        except projectfs.Binary as e:
            self._err(415, str(e))
        except projectfs.FsError as e:
            self._err(404, str(e))
        except gitlab.GitLabError as e:
            self._err(e.status if 400 <= e.status < 600 else 502, str(e))
        except gitops.GitError as e:
            self._err(409, "git: %s" % e)
        except (ValueError, OSError) as e:
            self._err(409, str(e))
        return True

    def _text(self, text: str) -> None:
        self._finish(200, "text/plain; charset=utf-8", text.encode("utf-8", "replace"))

    def _workbench97(self, parts: list[str], arg) -> bool:
        """GET routes of 9.7 (the Manager side: workbench.py); False when not one of them."""
        m, pj = MANAGER, arg("project")
        # S6: file content, transcripts' tool output, build and stack logs - never `piper attach`'s token
        content = (parts[:2] in (["api", "fs"], ["api", "git"], ["api", "idea"]) and len(parts) == 3
                   or len(parts) >= 4 and parts[:2] == ["api", "sessions"] and parts[3] in ("turns", "turn", "diagnostics")
                   or parts in (["api", "stack", "logs"], ["api", "stack", "job"]))
        if content and parts != ["api", "idea", "status"] and not self._gui_scope(CONTENT_SCOPE):
            return True
        if len(parts) in (4, 5) and parts[:2] == ["api", "sessions"] and parts[3] == "review":
            if not self._gui_scope("code review needs the GUI token"):
                return True
            if len(parts) == 4:
                self._json(200, m.review_files(parts[2], arg("scope", "session"), qint(arg, "n", 0)))
            elif parts[4] == "blob":
                self._json(200, m.review_blob(parts[2], arg("review"), arg("path"), arg("side")))
            elif parts[4] == "check":
                self._json(200, m.review_check(parts[2], arg("review"), arg("path")))
            elif parts[4] == "history":
                self._json(200, m.review_history(parts[2], arg("review"), arg("path")))
            elif parts[4] == "config":
                import reviewactions
                self._json(200, {"actions": reviewactions.load()})
            elif parts[4] == "revert":
                self._json(200, m.turn_revert_preview(parts[2], qint(arg, "n", 0), arg("undo")))
            elif parts[4] == "diff":
                self._text(m.review_diff(parts[2], arg("review")))
            else:
                return False
            return True
        if parts == ["api", "fs", "download"]:
            data = m.fs_download(pj, arg("path"))
            self._finish(200, "application/octet-stream", data, {"Content-Disposition": "attachment",
                         "Content-Security-Policy": "default-src 'none'; sandbox", "X-Content-Type-Options": "nosniff"})
            return True
        if parts == ["api", "fs", "blob"]:
            if not self._gui_scope("file blobs need the GUI token"):
                return True
            self._json(200, m.fs_blob(pj, arg("path"), arg("tree", "HEAD"), arg("orig")))
            return True
        if parts[:2] == ["api", "fs"] and len(parts) == 3 and parts[2] in ("marks", "symbols", "raw"):
            if parts[2] == "marks":
                self._json(200, m.fs_marks(pj, arg("path")))
            elif parts[2] == "symbols":
                self._json(200, m.fs_symbols(pj, arg("name"), arg("q")))
            else:
                ctype, data = m.fs_raw(pj, arg("path"))
                # an SVG opened on its own must not run script in piperd's origin
                self._finish(200, ctype, data, {"Content-Security-Policy": "default-src 'none'; "
                                                "style-src 'unsafe-inline'; sandbox",
                                                "Content-Disposition": "inline"})
        elif parts[:2] == ["api", "git"] and len(parts) == 3:
            op = parts[2]
            if op == "diff":
                self._text(m.git_file_diff(pj, arg("path")))
            elif op == "log":
                self._json(200, m.git_log(pj, arg("path"), qint(arg, "n", 50)))
            elif op == "blame":
                self._json(200, m.git_blame(pj, arg("path")))
            elif op == "graph":
                self._json(200, m.git_graph(pj, qint(arg, "n", 80)))
            elif op == "rows":   # 9.25 Git window: the log with lanes, one branch or all
                self._json(200, m.git_graph_rows(pj, qint(arg, "n", 300), arg("ref")))
            elif op == "branches":
                self._json(200, m.git_branches(pj))
            elif op == "commit":
                self._json(200, m.git_commit_detail(pj, arg("commit")))
            elif op == "blob":
                self._json(200, m.git_blob(pj, arg("commit"), arg("path"), arg("side")))
            elif op == "show":
                self._text(m.git_show(pj, arg("commit")))
            elif op == "status":   # K2: a project without a session
                self._json(200, m.project_git_status(pj))
            elif op == "ci":
                self._json(200, m.git_ci(pj))
            elif op == "mr-draft":
                self._json(200, m.mr_draft(pj, arg("target")))
            else:
                return False
        elif parts == ["api", "stack"]:
            self._json(200, m.stack_view(pj))
        elif parts == ["api", "stack", "job"]:
            self._json(200, m.stack_job_log(pj, arg("id"), qint(arg, "offset", 0)))
        elif parts == ["api", "stack", "logs"]:
            self._text(m.stack_logs(pj, arg("service"), qint(arg, "tail", 300)))
        elif len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] in ("turns", "turn", "diagnostics"):
            if parts[3] == "turns":
                self._json(200, m.turns(parts[2]))
            elif parts[3] == "diagnostics":
                self._json(200, m.diagnostics(parts[2]))
            elif arg("diff"):
                self._text(m.turn_diff(parts[2], arg("from"), arg("to"), arg("diff")))
            else:
                self._json(200, m.turn_files(parts[2], arg("from"), arg("to")))
        elif parts == ["api", "idea", "status"]:
            self._json(200, m.idea_status())
        elif parts == ["api", "idea", "problems"]:
            self._json(200, m.idea_problems(pj, arg("path")))
        elif parts == ["api", "idea", "symbol"]:
            self._json(200, m.idea_symbol(pj, arg("path"), qint(arg, "line", 0), qint(arg, "col", 0)))
        elif len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3:] == ["turns", "file"]:
            self._json(200, m.turn_file_history(parts[2], arg("path")))
        elif parts == ["api", "usage"]:
            self._json(200, m.usage())
        else:
            return False
        return True

    # ------------------------------------------------------------ POST
    def _no_method(self):
        if self._host_ok():
            self._err(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = _no_method

    def do_POST(self):
        self._guarded(self._post)

    def _post(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:   # read(-1) would wait for the client to close
            self.close_connection = True
            self._err(400, "bad Content-Length")
            return
        if not self._host_ok():   # before reading a body nobody may send
            self.close_connection = True
            return
        parts = [unquote(p) for p in urlsplit(self.path).path.strip("/").split("/")]
        if len(parts) == 3 and parts[:2] == ["api", "local-provider"]:
            if length > 64 << 10:
                self.close_connection = True
                self._err(413, "provider request too large")
                return
            try:
                raw = self.rfile.read(length)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                tok = self.headers.get("Authorization", "").removeprefix("Bearer ")
                self._json(200, providers.invoke(MANAGER, parts[2], tok, body))
            except PermissionError:
                self._err(403, "local provider is revoked or unavailable")
            except ValueError as e:
                self._err(400, str(e))
            return
        if parts[:2] == ["api", "mobile"]:
            if not self._host_ok() or self._auth() is None or not self._mobile_admin():
                self.close_connection = True
                return
        if self.headers.get("X-Piper-Environment") and parts[:2] != ["api", "environments"]:
            if length > DROP_MAX:
                self.close_connection = True
                self._err(413, "body too large")
                return
            if self._auth() is None or not self._gui_scope("remote environments need the full GUI token"):
                self.close_connection = True
                return
            self._environment_proxy(self.rfile.read(length))
            return
        if len(parts) == 4 and parts[:2] == ["api", "sessions"] and parts[3] == "drop":
            self._drop(parts[2], length)
            return
        if length > (6 << 20 if parts in (["api", "fs", "write"], ["api", "fs", "create"]) else 1 << 20):   # 2 MiB of text as JSON
            self.close_connection = True
            self._err(413, "body too large")
            return
        raw = self.rfile.read(length) if length else b""
        if not self._host_ok() or self._auth() is None:
            return
        if not terminal_post(parts) and not self._gui_scope("this action needs the GUI token"):
            return   # `piper attach`'s token and a scoped one: terminals only (token-scope audit 2026-10-07)
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except ValueError as e:
            self._err(400, "invalid JSON: %s" % e)
            return
        if parts[:2] == ["api", "mobile"]:
            self._mobile_api(parts, body)
            return
        if parts[:2] == ["api", "environments"]:
            if not self._gui_scope():
                return
            try:
                if parts == ["api", "environments", "provider"]:
                    self._json(200, providers.configure(body, MANAGER))
                elif len(parts) == 2:
                    self._json(200, remotes.save(body))
                elif len(parts) == 4 and parts[3] in ("probe", "install", "remove"):
                    action = parts[3]
                    if action == "remove":
                        providers.revoke_environment(parts[2], MANAGER)
                        remotes.remove(parts[2])
                        self._json(200, {"ok": True})
                    else:
                        self._json(200, (remotes.probe if action == "probe" else remotes.install)(parts[2]))
                else:
                    self._err(404, "unknown environment action")
            except ValueError as e:
                self._err(400, str(e))
            except remotes.Unavailable as e:
                self._err(502, str(e))
            return
        if parts[:2] == ["api", "settings"]:
            self._settings_post(parts, body)
            return
        if parts[:2] == ["api", "host-terminals"]:   # W2: the GUI's own (terminal_post never lets another token here)
            self._host_terminal_post(parts, body)
            return
        if parts == ["api", "image", "rebuild"]:   # 9.54: arm / cancel `hole start -r` for the next start
            if not self._gui_scope("the image rebuild needs the GUI token"):
                return
            on = body.get("on")
            if not isinstance(on, bool):
                self._err(400, "on must be true or false")
                return
            self._json(200, MANAGER.set_image_rebuild(on))
            return
        if parts == ["api", "quiet"]:
            try:
                action = body.get("action")
                q = getattr(MANAGER, "quiet", None)
                if q is None:
                    raise ValueError("Quiet window is unavailable in this running controller. Restart Piper after updating it.")
                if action == "queue_start":
                    result = MANAGER.quiet_queue_start(body)
                elif action == "cancel_start":
                    result = MANAGER.quiet_cancel_start(body.get("id"), checked=body.get("checked") is True)
                elif action == "request":
                    result = q.request(str(body.get("session") or ""), body.get("reason"), body.get("minutes"), operator=True)
                elif action == "release":
                    result = q.release("gui", str(body.get("id") or ""), finished=body.get("finished") is True, admin=True)
                elif action == "cancel":
                    result = q.cancel("gui", str(body.get("id") or ""), admin=True)
                elif action == "ack":
                    result = q.ack(str(body.get("session") or ""), str(body.get("id") or ""), done=body.get("finished") is True,
                                   detail="Confirmed by the operator after checking this session", operator=True)
                elif action == "renew":
                    result = q.renew(str(body.get("session") or ""), str(body.get("id") or ""), operator=True)
                elif action == "exempt":
                    # Only the operator's GUI shares a window; agents have no path to it.
                    if not self._gui_scope("an exception from a quiet window needs the GUI token"):
                        return
                    result = q.exempt(str(body.get("session") or ""), str(body.get("id") or ""), body.get("allow"))
                else:
                    raise ValueError("Unknown quiet window action")
                self._json(200, result)
            except (ValueError, OSError) as e:
                self._err(409, str(e))
            return
        if parts == ["api", "features"]:   # 9.20: a preset, modules, the setup card's Done
            if not self._gui_scope("features need the GUI token"):
                return
            try:
                ob = body.get("onboarded")
                pr = body.get("preset")
                sn = body.get("seen")
                self._json(200, features.save(body.get("modules") or {}, ob if isinstance(ob, bool) else None,
                                              pr if isinstance(pr, str) and pr else None,
                                              str(sn) if sn is not None else None))
            except ValueError as e:
                self._err(400, str(e))
            except OSError as e:
                self._err(500, "write failed: %s" % e)
            return
        try:
            if parts == ["api", "fs", "write"]:
                if not self._gui_scope(CONTENT_SCOPE):
                    return
                self._json(200, MANAGER.fs_write(str(body.get("project", "")), str(body.get("path", "")),
                                                 body.get("text"), body.get("sha")))
                return
            if parts[:2] == ["api", "fs"] and len(parts) == 3 and parts[2] in ("create", "scratch", "rename", "delete", "restore"):
                if not self._gui_scope("file operations need the GUI token"):
                    return
                pj = str(body.get("project", ""))
                if parts[2] == "scratch":
                    self._json(200, MANAGER.fs_scratch(pj, str(body.get("ext", ""))))
                elif parts[2] == "create":
                    self._json(200, MANAGER.fs_create(pj, str(body.get("path", "")), str(body.get("kind", "")),
                                                      body.get("text")))
                elif parts[2] == "rename":
                    self._json(200, MANAGER.fs_rename(pj, str(body.get("from", "")), str(body.get("to", ""))))
                elif parts[2] == "delete":
                    self._json(200, MANAGER.fs_delete(pj, str(body.get("path", ""))))
                else:
                    self._json(200, MANAGER.fs_restore(pj, str(body.get("trash", "")), **(
                        {"replace": True} if body.get("replace") is True else {})))
                return
            if parts[:2] == ["api", "git"] and len(parts) == 3 and parts[2] in ("branch", "fetch", "pull", "worktree"):
                if not self._gui_scope("branches and worktrees need the GUI token"):   # 9.25: the human's git
                    return
                pj = str(body.get("project", ""))
                if parts[2] == "branch":
                    self._json(200, MANAGER.git_branch_op(pj, body))
                elif parts[2] == "worktree":
                    self._json(200, MANAGER.git_worktree_op(pj, body))
                else:
                    self._json(200, MANAGER.git_remote_op(pj, parts[2]))
                return
            if parts == ["api", "git", "mr"]:
                self._json(200, MANAGER.create_mr(str(body.get("project", "")), str(body.get("title", "")),
                                                  str(body.get("description", "")), str(body.get("target", "")),
                                                  bool(body.get("draft"))))
                return
            if len(parts) == 5 and parts[:3] == ['api', 'http', 'auth'] and parts[4] in ('open', 'closed'):
                if not self._gui_scope('HTTP sign-in needs the full GUI token'):
                    return
                self._json(200, MANAGER.http_auth_open(parts[3]) if parts[4] == 'open' else MANAGER.http_auth_browser_closed(parts[3]))
            elif parts[:2] == ["api", "http"] and len(parts) == 3:
                if not self._gui_scope(".http runs need the GUI token"):
                    return
                if parts[2] == "run":
                    idx = body.get("index")
                    if idx is not None and (not isinstance(idx, int) or isinstance(idx, bool)):
                        raise ValueError("index must be an integer")
                    self._json(200, MANAGER.http_gui_run(str(body.get("project", "")), str(body.get("path", "")),
                                                         str(body.get("env", "")), idx, str(body.get("sha") or ""),
                                                         str(body.get("save_to") or ""), body.get('dependency_shas')))
                elif parts[2] == "envfile":
                    self._json(200, MANAGER.http_gui_envfile_save(str(body.get("project", "")),
                        str(body.get("path", "")), body.get("text"), body.get("sha")))
                elif parts[2] == "envcfg":
                    self._json(200, MANAGER.http_gui_envcfg_save(str(body.get("project", "")), str(body.get("path", "")),
                                                                 str(body.get("env", "")), body.get("cfg") or {}))
                elif parts[2] == "tunnel":
                    self._json(200, MANAGER.http_gui_tunnel(str(body.get("project", "")), str(body.get("path", "")),
                                                            str(body.get("env", "")), str(body.get("op", ""))))
                elif parts[2] == "share":
                    self._json(200, MANAGER.http_share(str(body.get("id", "")), body.get("on") is True))
                elif parts[2] == "install":
                    self._json(202, MANAGER.http_install_start())
                else:
                    self._err(404, "not found")
                return
            if parts[:2] == ["api", "db"] and len(parts) == 3:
                if not self._gui_scope("the database view needs the GUI token"):
                    return
                self._db_post(parts[2], body)
                return
            if parts == ["api", "open-url"]:
                # a Safari web app (Add to Dock) drops window.open from the terminal: the host opens it
                if not self._gui_scope("opening a URL needs the GUI token"):
                    return
                if not MANAGER.policy.get("open_links_on_host"):
                    self._err(403, "off (policy open_links_on_host)")
                    return
                url = str(body.get("url") or "")
                if not url.startswith(("http://", "https://")) or len(url) > 8192 or any(c in url for c in "\x00\n\r"):
                    self._err(400, "only http(s) URLs")
                    return
                if is_piper_url(url):
                    self._err(409, "Open Piper links inside Piper")
                    return
                import subprocess
                chrome = MANAGER.policy.get("host_link_browser") == "chrome"
                try:
                    if sys.platform == "darwin":
                        argv = ["open", "-a", "Google Chrome", url] if chrome else ["open", url]
                        result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                        if result.returncode:
                            self._err(502, "Could not open the selected browser; check that it is installed")
                            return
                    else:
                        # A newly launched Linux browser can stay in the foreground for its whole lifetime.
                        subprocess.Popen(["google-chrome" if chrome else "xdg-open", url],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except (OSError, subprocess.TimeoutExpired):
                    self._err(502, "Could not open the selected browser; check that it is installed and responding")
                    return
                self._json(200, {"ok": True})
                return
            if parts == ["api", "presence"]:
                MANAGER.presence(body.get("visible") is True)
                self._json(200, {"ok": True})
                return
            if parts == ["api", "mattermost", "test"]:
                self._json(200, MANAGER.mm_test())
                return
            if parts == ["api", "interrupted"]:   # 9.141: the restore card shown / the Interrupted marks dismissed
                if not self._gui_scope("the restore card needs the GUI token"):
                    return
                try:
                    self._json(200, MANAGER.interrupted_seen(body.get("seen")) if "seen" in body
                               else MANAGER.interrupted_dismiss(body.get("dismiss")))
                except ValueError as e:
                    self._err(400, str(e))
                except OSError as e:
                    self._err(500, "write failed: %s" % e)
                return
            if parts[:2] == ["api", "stack"] and not self._gui_scope("stack actions need the GUI token"):
                return   # host containers: not `piper attach`'s terminals-only token, not a scoped one
            if parts == ["api", "stack", "restart"]:
                self._json(200, MANAGER.stack_restart(str(body.get("project", "")), str(body.get("service", ""))))
                return
            if parts == ["api", "stack", "action"]:
                self._json(200, MANAGER.stack_action(str(body.get("project", "")), str(body.get("verb", "")),
                                                     str(body.get("service", ""))))
                return
            if parts == ["api", "stack", "mode"]:
                self._json(200, MANAGER.stack_mode(str(body.get("project", "")), str(body.get("mode", ""))))
                return
            if parts == ["api", "stack", "setup"]:
                self._json(200, MANAGER.stack_setup(str(body.get("project", ""))))
                return
            if parts == ["api", "stack", "cancel"]:
                self._json(200, MANAGER.stack_job_cancel(str(body.get("project", "")), str(body.get("id", ""))))
                return
            if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "review":
                if not self._gui_scope("review actions need the GUI token"):
                    return
                sid, action = parts[2], parts[4]
                if action == "stage":
                    self._json(200, MANAGER.review_stage(sid, str(body.get("review", "")), str(body.get("path", ""))))
                elif action == "unstage":
                    self._json(200, MANAGER.review_unstage(sid, str(body.get("review", "")), str(body.get("path", ""))))
                elif action == "write":
                    self._json(200, MANAGER.review_write(sid, str(body.get("review", "")), str(body.get("path", "")), body.get("text"), body.get("sha")))
                elif action == "dispatch":
                    self._json(202, MANAGER.review_dispatch(sid, body))
                elif action == "done":
                    self._json(200, MANAGER.review_done(sid))
                elif action == "revert":
                    self._json(200, MANAGER.turn_revert(sid, gitlab._whole(body.get("n", 0), "n"),
                                                      str(body.get("current", "")), str(body.get("undo", ""))))
                else:
                    self._err(404, "unknown review action")
                return
            if len(parts) == 5 and parts[:2] == ["api", "sessions"] and parts[3] == "git":
                if parts[4] in ("commit", "message"):
                    files = body.get("files")
                    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
                        raise ValueError("files must be a list of paths")
                amend = {"amend": True} if body.get("amend") is True else {}
                if amend and not self._gui_scope("amend needs the GUI token"):
                    return
                if parts[4] == "commit":
                    if not self._gui_scope("reviewed commits need the GUI token"):
                        return
                    self._json(200, MANAGER.git_commit(parts[2], str(body.get("message", "")), files, body.get("reviewed"), **amend))
                elif parts[4] == "message":   # 9.25 W6: host claude -p drafts it
                    if not self._gui_scope("a drafted commit message needs the GUI token"):
                        return
                    self._json(200, MANAGER.git_message(parts[2], files, **({"staged": True} if body.get("staged") is True else {})))
                elif parts[4] == "mr-message":   # commit A: the MR dialog's Generate, the same host claude -p
                    if not self._gui_scope("a drafted MR description needs the GUI token"):
                        return
                    self._json(200, MANAGER.mr_message(parts[2], str(body.get("target", ""))))
                elif parts[4] == "push":
                    self._json(200, MANAGER.git_push(parts[2]))
                else:
                    self._err(404, "unknown git action")
                return
            if parts[:2] == ["api", "prod"] and len(parts) == 3:
                self._prod_post(parts[2], body)
                return
            if parts[:2] == ["api", "feed"] and len(parts) in (3, 4):
                self._feed_post(parts[2:], body)
                return
            if parts[:2] == ["api", "inbox"] and len(parts) == 3 or parts == ["api", "projects", "add"]:
                self._inbox_post(parts, body)
                return
            if parts[:2] == ["api", "timesheet"] and len(parts) == 3:
                self._timesheet_post(parts[2], body)
                return
            if parts == ["api", "sessions"]:
                opts = self._start_opts(body)
                if opts is None:
                    return
                rebuild = body.get("image_rebuild", False)   # 9.54: the dialog's "Rebuild image at this start"
                if not isinstance(rebuild, bool):
                    raise ValueError("image_rebuild must be true/false")
                if rebuild and not self._gui_scope("the image rebuild needs the GUI token"):
                    return
                s = MANAGER.create(str(body.get("project", "")), str(body.get("task", "")),
                                   str(body.get("title", "")), origin="tty" if body.get("origin") == "tty" else "gui",
                                   agent=str(body.get("agent") or "claude"), runtime=body.get("runtime"), **({"opts": opts} if opts else {}),
                                   **({"image_rebuild": True} if rebuild else {}))
                result = s.to_json()
                self._remember_start(body, result)
                self._json(202, result)
            elif len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                if parts[3] == "fix-excludes":
                    self._json(200, MANAGER.fix_excludes(parts[2]))
                    return
                if parts[3] == "allow-uncovered":   # the blocked start's "Allow in this project" (confirmed in the GUI)
                    self._json(200, MANAGER.allow_uncovered(parts[2]))
                    return
                if parts[3] == "open-terminal":   # `piper attach <id>` in a new Ghostty window
                    s = MANAGER.get(parts[2])
                    self._json(200, ttyattach.open_terminal(s.id, s.path))
                    return
                if parts[3] == "send":
                    MANAGER.send(parts[2], str(body.get("text", "")))
                    self._json(200, {"ok": True})
                    return
                if parts[3] == "title":
                    MANAGER.rename(parts[2], str(body.get("title", "")))
                    renamed = MANAGER.get(parts[2])
                    self._json(200, {"ok": True, "title": renamed.title, "title_source": renamed.title_source})
                    return
                if parts[3] == "input-clear":
                    MANAGER.input_clear(parts[2])
                    self._json(200, {"ok": True})
                    return
                if parts[3] == "allow":
                    self._json(200, MANAGER.allow_domain(parts[2], str(body.get("domain", ""))))
                    return
                if parts[3] == "unlock":
                    if not self._gui_scope("unlocks need the GUI token"):
                        return
                    self._json(200, MANAGER.set_unlock(parts[2], str(body.get("what", "")), body.get("on") is True))
                    return
                if parts[3] == "revoke-approval":
                    if not self._gui_scope("approval grants need the GUI token"):
                        return
                    MANAGER.revoke_approval(parts[2], str(body.get("scope", "")))
                    self._json(200, {"ok": True})
                    return
                if parts[3] == "start-opt":   # a stopped session's START_OPTS, for its next start
                    if not self._gui_scope("start options need the GUI token"):
                        return
                    self._json(200, MANAGER.set_start_opt(parts[2], str(body.get("what", "")), body.get("on") is True))
                    return
                if parts[3] == "issue-state":
                    self._json(200, MANAGER.issue_state_update(parts[2]))
                    return
                if parts[3] == "switch-agent":   # 9.140: continue with the other agent
                    if not self._gui_scope("continuing with another agent needs the GUI token"):
                        return
                    try:   # R2 as an import: hidden paths in the transcript - only confirm_hidden: true hands it over
                        self._json(200, MANAGER.switch_agent(parts[2], body.get("confirm_hidden") is True))
                    except ValueError as e:   # sessions.HiddenInTranscript
                        if not isinstance(getattr(e, "paths", None), list):
                            raise
                        self._json(409, {"error": str(e), "hidden_paths": e.paths})
                    return
                act = {"stop": MANAGER.stop, "approve": MANAGER.approve, "retry": MANAGER.retry,
                       "forget": MANAGER.forget}.get(parts[3])
                if act is None:
                    self._err(404, "unknown action")
                    return
                r = act(parts[2])
                self._json(200, {"ok": True, **(r if isinstance(r, dict) else {})})
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] in ("resume", "fork"):
                if not self._gui_scope("resuming or forking a conversation needs the GUI token"):
                    return
                self._json(200, MANAGER.resume_conversation(parts[2], parts[3] == "fork"))
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "continue":
                if not self._gui_scope("continuing a conversation needs the GUI token"):
                    return
                self._json(200, MANAGER.continue_session(parts[2], body.get("task")))   # W8
            elif len(parts) == 4 and parts[:2] == ["api", "conversations"] and parts[3] == "import":
                if not self._gui_scope("importing a local conversation needs the GUI token"):
                    return
                try:   # R2: hidden paths in the transcript - the GUI asks, only confirm_hidden: true copies it
                    self._json(200, MANAGER.import_conversation(parts[2], str(body.get("project", "")),
                                                                body.get("confirm_checkout") is True,
                                                                body.get("confirm_hidden") is True))
                except ValueError as e:   # sessions.HiddenInTranscript (sessions is imported in main())
                    if not isinstance(getattr(e, "paths", None), list):
                        raise
                    self._json(409, {"error": str(e), "hidden_paths": e.paths})
            elif len(parts) == 3 and parts[:2] == ["api", "pending"]:
                pending = MANAGER.pending.get(parts[2], {})
                if ('secret' in body or pending.get('kind') == 'http_cert_passphrase') and not self._gui_scope('certificate passphrases require the full GUI token'):
                    return
                MANAGER.decide(parts[2], bool(body.get("approve")), body.get("always") is True,
                               **({"session_always": True} if body.get("session_always") is True else {}),
                               **({'secret':body['secret']} if 'secret' in body else {}),
                               **({"similar_hour": True} if body.get("similar_hour") is True else {}))
                self._json(200, {"ok": True})
            elif parts == ["api", "projects", "import"]:
                self._json(200, {"added": projects.import_roots(extensions.collect("project_roots"), MANAGER.gitlab_host(),
                                                                MANAGER.origin_gl)})
            else:
                self._err(404, "not found")
        except KeyError as e:
            self._err(404, unknown(e))
        except projectfs.FsError as e:   # a hidden / outside path (FsError is a ValueError)
            self._err(404, str(e))
        except gitlab.GitLabError as e:
            self._err(e.status if 400 <= e.status < 600 else 502, str(e))
        except ValueError as e:
            self._err(409, str(e))
        except gitops.GitError as e:
            self._err(409, "git: %s" % e)
        except PtyHostError as e:
            self._err(503, "ptyhost: %s" % e)
        except OSError as e:
            self._err(409, str(e))

    def _prod_post(self, op: str, body: dict) -> None:
        """POST routes of the morning production check (9.12 C); errors map like the other POST routes."""
        m, key = MANAGER, str(body.get("key", ""))
        if op == "run":
            self._json(202, m.prod_run_now(str(body.get("mode") or "check")))
        elif op == "mapping":
            self._json(200, m.prod_set_mapping(str(body.get("project", "")), str(body.get("obs") or "")))
        elif op == "ignore":   # "Do not map" / undo (Settings → Projects)
            self._json(200, m.prod_ignore(str(body.get("project", "")), body.get("on", True) is not False))
        elif op == "mute":
            self._json(200, m.prod_mute(key, body.get("days") or 0))
        elif op == "unmute":
            self._json(200, m.prod_unmute(key))
        elif op == "investigate":
            self._json(202, m.prod_investigate(key))
        else:
            self._err(404, "not found")

    def _feed_post(self, op: list[str], body: dict) -> None:
        """POST routes of Feed (9.14); errors map like the other POST routes."""
        m = MANAGER
        if op == ["seen"]:
            self._json(200, m.feed_seen(body.get("ts")))
        elif op == ["dismiss"]:
            self._json(200, m.feed_dismiss(str(body.get("key", ""))))
        elif op == ["undismiss"]:
            self._json(200, m.feed_undismiss())
        elif op == ["mute"]:
            self._json(200, m.feed_mute(str(body.get("key", "")), body.get("minutes")))
        elif op == ["chat"]:
            self._json(200, m.chat_send(str(body.get("text", ""))))
        elif op == ["chat", "cancel"]:
            self._json(200, m.chat_cancel(str(body.get("id", ""))))
        elif op == ["chat", "retry"]:
            self._json(200, m.chat_retry(str(body.get("id", ""))))
        elif op == ["chat", "start"]:
            self._json(202, m.chat_start())
        else:
            self._err(404, "not found")

    def _start_opts(self, body: dict) -> dict | None:
        """A new session's `opts` (sessions.START_OPTS; create checks the names). They weaken
        the sandbox like Settings does: the GUI token only. None = the error is already sent."""
        remember = body.get("remember_for_project", False)
        if not isinstance(remember, bool):
            raise ValueError("remember_for_project must be true/false")
        if remember and (body.get("session") or body.get("project") == "advisor"):
            raise ValueError("project defaults apply to a new project session only")
        if remember and not self._gui_scope("project defaults need the GUI token"):
            return None
        opts = body.get("opts") or {}
        if not isinstance(opts, dict) or not all(isinstance(v, bool) for v in opts.values()):
            raise ValueError("opts must be an object of true/false")   # "true" or 1 would start without it
        if any(opts.values()) and not self._gui_scope("start options need the GUI token"):
            return None
        return opts

    def _remember_start(self, body: dict, result: dict) -> None:
        """A refused start never gets here; a failed preference write must not duplicate a session."""
        if body.get("remember_for_project") is not True or not result.get("id"):
            return
        try:
            result["project_defaults"] = projects.remember_session_defaults(
                result["project"], result.get("agent", "claude"), result.get("opts") or {},
                keep_agent=features.session_agents() is None and not features.on("codex"), session_path=result.get("path"))
        except (OSError, ValueError, KeyError):
            result["defaults_warning"] = "Session created; project options were not saved."

    def _db_post(self, op: str, body: dict) -> None:
        g = lambda k: str(body.get(k) or "")   # noqa: E731
        m = MANAGER
        if op == "query":
            self._json(200, m.db_gui_query(g("project"), g("connection"), g("sql"), g("schema"), body.get("params"),
                                           int(body.get("max_rows") or 0), g("session"), body.get("explain") is True,
                                           int(body.get("offset") or 0), g("request_tag")))
        elif op == "fetch":
            self._json(200, m.db_gui_fetch(g("result_id"), int(body.get("offset") or 0), int(body.get("max_rows") or 1000)))
        elif op == "execute":
            st = body.get("statements")
            if not isinstance(st, list):
                raise ValueError("statements must be a list")
            self._json(200, m.db_gui_execute(g("project"), g("connection"), st, g("schema"), g("session"), g("confirm"),
                                             body.get("preview_only") is True))
        elif op == "cancel":
            self._json(200, m.db_gui_cancel(g("project"), g("connection"), g("session"), g("owner"), g("request_tag")))
        elif op == "tx":
            st = body.get("statements")
            if st is not None and not isinstance(st, list):
                raise ValueError("statements must be a list")
            self._json(200, m.db_gui_tx(g("project"), g("connection"), g("op"), st, g("schema"), g("session"), g("confirm"),
                                        g("tx"), int(body.get("max_rows") or 0), g("request_tag")))
        elif op == "save":
            c = body.get("connection")
            if not isinstance(c, dict):
                raise ValueError("connection must be an object")
            pw = body.get("password")
            self._json(200, m.db_gui_save(g("project"), c, pw if isinstance(pw, str) else None))
        elif op == "delete":
            self._json(200, m.db_gui_delete(g("project"), g("name")))
        elif op == "unpin":
            self._json(200, m.db_unpin(g("name")))
        elif op == "tunnel":
            self._json(200, m.db_gui_tunnel(g("project"), g("connection"), g("op") or "open", g("session")))
        elif op == "install":
            self._json(202, m.db_install_start())
        else:
            self._err(404, "not found")

    def _full_scope(self) -> bool:
        """_gui_scope() without the 403: is this the GUI's full-scope token?"""
        t = self._token()
        return bool(t) and t.get("scope") == "*" and t.get("label") != "tty"

    def _gui_scope(self, why: str = "settings need the GUI token") -> bool:
        """Only the GUI's full-scope token (not `piper attach`'s tty token): settings and isolation
        changes (9.13), file content and what holds it (CONTENT_SCOPE), the database, file operations."""
        t = self._token()
        if not t or t.get("scope") != "*" or t.get("label") == "tty":   # `piper attach`: terminals only
            self._err(403, why)
            return False
        return True

    def _mobile_admin(self) -> bool:
        """Pairing administration belongs only to the local desktop's GUI token."""
        t = self._token()
        if (not t or t.get("scope") != "*" or t.get("label") != "gui"
                or self._remote() or self.headers.get("X-Piper-Environment")
                or self.client_address[0] not in ("127.0.0.1", "::1")):
            self._err(403, "Manage phones in Settings > Mobile access on this computer.")
            return False
        return True

    def _mobile_api(self, parts: list[str], body: dict | None = None) -> None:
        if body is None and parts[2:] == ["events"]:
            token = self._token() or {}
            if (token.get("scope") != "*" or token.get("label") != "mobile-bridge"
                    or self._remote() or self.headers.get("X-Piper-Environment")
                    or self.client_address[0] not in ("127.0.0.1", "::1")):
                self._err(403, "Notification events need the local mobile bridge.")
                return
            import mobilepush
            self._json(200, mobilepush.snapshot(MANAGER))
            return
        if not self._mobile_admin():
            return
        import mobile  # stdlib only; optional native iroh stays in the separate bridge
        try:
            if body is None and len(parts) == 2:
                self._json(200, mobile.desktop_status())
            elif body is not None and parts[2:] == ["invite"]:
                self._json(200, mobile.desktop_invitation(body.get("label", "")))
            elif body is not None and parts[2:] == ["cancel"]:
                self._json(200, {"ok": mobile.cancel_invitation(body.get("id", ""))})
            elif body is not None and parts[2:] == ["revoke"]:
                peer = body.get("id", "")
                if not isinstance(peer, str) or not re.fullmatch(r"[0-9a-f]{64}", peer):
                    raise ValueError("invalid device")
                self._json(200, {"ok": mobile.revoke(peer)})
            else:
                self._err(404, "unknown mobile action")
        except (OSError, ValueError):
            # Private state may include relay credentials: never serialize the exception.
            self._err(409, "Mobile access is unavailable. Check that this computer is online and try again.")

    def _host_terminal_post(self, parts: list[str], body: dict) -> None:
        """POST /api/host-terminals {project, cols?, rows?} opens a host shell (W2, hostterm.py);
        POST /api/host-terminals/<id>/close closes one. GUI token only, 403 while the policy is off."""
        if not self._gui_scope("the host terminal needs the GUI token"):
            return
        if len(parts) == 4 and parts[3] == "close":
            if not MANAGER.hosts.close(parts[2]):
                self._err(404, "unknown host terminal")
                return
            self._json(200, {"ok": True})
            return
        if len(parts) != 2:
            self._err(404, "not found")
            return
        if not MANAGER.hosts.enabled():
            self._err(403, "the host terminal is off (Settings → Terminal → Host terminal)")
            return
        sid = str(body.get("session") or "")
        if sid:
            s = MANAGER.get(sid)
            p = MANAGER.gui_project(s.project)
            if s.kind != "project" or os.path.realpath(p["path"]) != os.path.realpath(s.path):
                self._err(409, "the session checkout changed; open a terminal from the current project")
                return
            p = dict(p, path=s.path, title=s.title)
        else:
            p = projects.find(str(body.get("project") or ""), os.environ.get("PROJECT_PATH", ""))
        if p is None:
            self._err(404, "unknown project")
            return
        try:
            self._json(200, MANAGER.hosts.open(p, body.get("cols") or 120, body.get("rows") or 32,
                                               **({"session": sid} if sid else {})))
        except PermissionError as e:
            self._err(403, str(e))
        except (ValueError, TypeError) as e:
            self._err(400, str(e))
        except (RuntimeError, OSError, PtyHostError) as e:
            self._err(502, str(e))

    def _settings_post(self, parts: list[str], body: dict) -> None:
        """POST /api/settings {section, values, reset?, clear?, confirm?, rev?} and
        POST /api/settings/verify-gitlab {}."""
        if not self._gui_scope():
            return
        try:
            if parts == ["api", "settings", "verify-gitlab"]:
                self._json(200, settings.verify_gitlab(MANAGER.gitlab_host(), getattr(MANAGER, "gitlab_opener", None)))
                return
            if parts == ["api", "settings", "verify-github"]:
                self._json(200, settings.verify_github(MANAGER.github_hosts(), getattr(MANAGER, "github_opener", None)))
                return
            if parts != ["api", "settings"]:
                self._err(404, "not found")
                return
            lists = {k: body.get(k) or [] for k in ("reset", "clear", "confirm")}
            if not isinstance(body.get("values") or {}, dict) or not all(
                    isinstance(v, list) and all(isinstance(x, str) for x in v) for v in lists.values()):
                raise settings.SettingsError("values must be an object, reset/clear/confirm lists")
            self._json(200, settings.update(str(body.get("section", "")), body.get("values") or {},
                                            lists["confirm"], lists["reset"], lists["clear"],
                                            body.get("rev"), on_change=_settings_changed,
                                            security_profile=body.get("security_profile")))
        except KeyError as e:
            self._err(404, "unknown section: %s" % e)
        except settings.NeedsConfirm as e:
            self._json(409, {"error": str(e), "needs_confirm": e.items})
        except settings.Conflict as e:
            self._json(409, {"error": str(e), "conflict": True})
        except settings.SettingsError as e:
            self._json(400, {"error": str(e), "errors": e.errors})
        except OSError as e:
            self._err(500, "write failed: %s" % e)

    def _inbox_post(self, parts: list[str], body: dict) -> None:
        """POST routes of the GitLab inbox (9.8); errors map like the other POST routes."""
        m, op = MANAGER, parts[-1]
        if parts == ["api", "projects", "add"]:
            self._json(200, m.inbox_add_project(str(body.get("path", "")), str(body.get("gl", ""))))
        elif op == "refresh":
            self._json(200, m.inbox_refresh())
        elif op == "seen":
            self._json(200, m.inbox_seen(body.get("keys"), body.get("all") is True))
        elif op == "unseen":
            self._json(200, m.inbox_unseen(str(body.get("key", ""))))
        elif op == "done":
            self._json(200, m.inbox_done(str(body.get("key", ""))))
        elif op == "start":
            opts = self._start_opts(body)
            if opts is None:
                return
            r = m.inbox_start(str(body.get("key", "")), str(body.get("mode") or "work"), body.get("project") or None,
                              body.get("note_id"), body.get("task"), body.get("session") or None, origin="gui",
                              agent=str(body.get("agent") or "claude"), **({"runtime": body["runtime"]} if "runtime" in body else {}), **({"opts": opts} if opts else {}))
            self._remember_start(body, r)
            self._json(200 if r.get("sent") else 202, r)
        elif op == "comment":
            self._json(200, m.inbox_comment(str(body.get("key", "")), str(body.get("body", ""))))
        elif op == "rules":
            self._json(200, m.inbox_rules(body.get("rules")))
        else:
            self._err(404, "not found")

    def _timesheet_post(self, op: str, body: dict) -> None:
        """POST routes of the Timesheet view (9.12 A); local files only, nothing leaves piperd."""
        m, week = MANAGER, str(body.get("week", ""))
        if op == "focus":
            self._json(200, m.ts_focus(str(body.get("session", ""))))
        elif op == "edit":
            self._json(200, m.ts_edit(week, str(body.get("day", "")), str(body.get("key", "")),
                                      body.get("minutes"), body.get("text")))
        elif op == "manual":
            self._json(200, m.ts_manual(week, body))
        elif op == "assign":
            self._json(200, m.ts_assign(str(body.get("session", "")), str(body.get("key", "")), str(body.get("title", ""))))
        else:
            self._err(404, "not found")

    def _drop(self, sid: str, length: int) -> None:
        if length > DROP_MAX:
            self.close_connection = True
            self._err(413, "image larger than 10 MiB")
            return
        data = self.rfile.read(length) if length else b""
        if not self._host_ok() or self._auth() is None or not self._gui_scope("dropping a file needs the GUI token"):
            return
        try:
            self._json(200, {"path": MANAGER.drop(sid, self.headers.get("Content-Type", ""), data)})
        except KeyError:
            self._err(404, "unknown session")
        except (ValueError, OSError) as e:
            self._err(409, str(e))

    # ------------------------------------------------------------ WebSocket terminal
    def _environment_proxy(self, raw: bytes = b"") -> bool:
        eid = self.headers.get("X-Piper-Environment")
        if not eid or urlsplit(self.path).path.startswith("/api/environments"):
            return False
        if not self._gui_scope("remote environments need the full GUI token"):
            return True
        try:
            parts = urlsplit(self.path).path.strip("/").split("/")
            provider_decision = len(parts) == 3 and parts[:2] == ["api", "pending"] and parts[2].startswith("local-")
            provider_auth = (len(parts) == 5 and parts[:3] == ["api", "http", "auth"]
                             and parts[3].startswith("local-") and parts[4] in ("open", "closed"))
            if self.command == "POST" and (provider_decision or provider_auth):
                pid = parts[3 if provider_auth else 2][len("local-"):]
                row = MANAGER.pending.get(pid)
                if row is None or providers.remote_session(eid, row.get("session")) is None:
                    self._err(404, "unknown local provider approval")
                    return True
                if provider_auth:
                    try:
                        result = MANAGER.http_auth_open(pid) if parts[4] == "open" else MANAGER.http_auth_browser_closed(pid)
                    except KeyError:
                        self._err(404, "unknown local provider sign-in")
                        return True
                    self._json(200, result)
                else:
                    body = json.loads(raw)
                    MANAGER.decide(pid, body.get("approve") is True, False, False, **({"secret": body["secret"]} if "secret" in body else {}))
                    self._json(200, {"ok": True})
                return True
            r = remotes.proxy(eid, self.command, self.path, raw, self.headers.get("Content-Type", "application/json"))
            if r["status"] == 401:
                self._err(502, "Server API credentials were refused; repair the server service. Your local Piper login is unchanged.")
                return True
            data = base64.b64decode(r["body"])
            if self.path == "/api/state" and r["status"] == 200:
                providers.ensure_tunnel(eid)
                state = json.loads(data)
                entry = remotes.get(eid)
                state["environment"] = {k: entry[k] for k in ("id", "label", "host", "mode")}
                providers.decorate_state(MANAGER, eid, state)
                data = json.dumps(state).encode()
            self._finish(r["status"], r["ctype"], data)
        except ValueError as e:
            self._err(400, str(e))
        except remotes.Unavailable as e:
            self._err(502, str(e))
        return True

    def _websocket(self, sid: str) -> None:
        accept = wsproto.handshake_ok(self.headers)
        if accept is None:
            self._err(400, "not a WebSocket upgrade")
            return
        if self.headers.get("Origin") is None:
            self._err(403, "Origin required")
            return
        query = parse_qs(urlsplit(self.path).query)
        eid = query.get("environment", [""])[0]
        checkpoint = None
        if query.get("checkpoint") == ["1"]:
            checkpoint = {}
            try:
                offset, pid = int(query["from"][0]), int(query["pid"][0])
                started = float(query["started"][0])
                if 0 <= offset <= 2**53 - 1 and 0 < pid <= 2**53 - 1 and math.isfinite(started) and started > 0:
                    checkpoint = dict(offset=offset, pid=pid, started=started)
            except (KeyError, ValueError, OverflowError):
                pass  # invalid/stale checkpoint gets a clean full replay, never input replay
        host = hostterm.is_host_id(sid)   # W2: a host shell - the GUI's full token only, while the policy is on
        if eid:
            try:
                remotes.get(eid)
            except ValueError:
                self._err(404, "unknown environment")
                return
        elif host:
            if not MANAGER.hosts.enabled() or MANAGER.hosts.get(sid) is None:
                self._err(404 if MANAGER.hosts.enabled() else 403, "unknown host terminal" if MANAGER.hosts.enabled()
                          else "the host terminal is off")
                return
        else:
            try:
                MANAGER.get(sid)
            except KeyError:
                self._err(404, "unknown session")
                return
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        ws = wsproto.WebSocket(self.connection, self.rfile)
        self.connection.settimeout(3)
        try:
            op, msg = ws.recv()
            auth = json.loads(msg) if op == wsproto.OP_TEXT else {}
            tok = auth.get("auth")
        except (wsproto.WSClosed, wsproto.WSProtocolError, OSError, ValueError, AttributeError):
            tok = None
        info = common.load_tokens().get(tok) if tok else None
        if info is None:
            ws.close(4401, "auth required")
            return
        if (host or eid) and (info.get("scope") != "*" or info.get("label") == "tty"):   # `piper attach`, a scoped token
            common.audit({"event": "host-terminal-refused", "terminal": sid, "token": info.get("label")})
            ws.close(4403, "the host terminal needs the GUI token")
            return
        self.connection.settimeout(None)
        revoked = threading.Event()

        def authorized():
            # Pin the original scope/identity as well as the bearer. Once observed
            # revoked, this connection must never become usable again.
            if revoked.is_set():
                return False
            try:
                valid = common.load_tokens().get(tok) == info
            except (OSError, ValueError):
                valid = False
            if not valid:
                revoked.set()
            return valid

        self._terminal(ws, sid, eid, authorized=authorized, replay_geometry=auth.get("replay_geometry") is True, checkpoint=checkpoint)

    def _terminal(self, ws: wsproto.WebSocket, sid: str, eid: str = "", *, authorized,
                  replay_geometry: bool = False, checkpoint: dict | None = None) -> None:
        # Subscribe replies and events are read on another thread. Gate every event,
        # not just bytes: a size delivered before hello could be reset by stale info.
        gate = {"open": False, "held": []}
        gate_lock = threading.Lock()
        replay_done = threading.Event()
        checkpoint_enabled = False
        stopped = threading.Event()

        def require_auth():
            if not authorized():
                ws.close(4401, "access revoked")
                raise wsproto.WSClosed("access revoked")

        def watch_auth():
            while not stopped.wait(0.5):
                try:
                    require_auth()
                except wsproto.WSClosed:
                    return

        def forward(ev):
            require_auth()
            if ev.get("ev") == "data":
                data = base64.b64decode(ev["data"])
                if checkpoint_enabled:
                    ws.send_text(json.dumps({"t": "chunk", "off": ev["off"], "bytes": len(data)}))
                ws.send_binary(data)
            elif ev.get("ev") == "exit":
                ws.send_text(json.dumps({"t": "exit", "rc": ev.get("rc")}))
            elif ev.get("ev") == "respawn":   # retry/resume: a new child under this id
                ws.send_text(json.dumps({"t": "respawn"}))
            elif ev.get("ev") == "size":   # #13: the PTY's new size (a native terminal sets it)
                msg = {"t": "size", "c": ev.get("cols"), "r": ev.get("rows")}
                if ev.get("replay"):
                    msg["replay"] = True
                ws.send_text(json.dumps(msg))
            elif ev.get("ev") == "replay-end":
                ws.send_text(json.dumps({"t": "replay-end"}))
            elif ev.get("ev") == "tty":    # #13: a native terminal came (n=1) or went (n=0)
                ws.send_text(json.dumps({"t": "tty", "n": ev.get("n")}))

        def on_event(ev):
            try:
                with gate_lock:
                    if not gate["open"]:
                        gate["held"].append(ev)
                        if ev.get("ev") == "replay-end":
                            replay_done.set()
                    else:
                        forward(ev)
            except (OSError, wsproto.WSClosed):
                pass

        original_sid = sid

        def peer():
            require_auth()
            client = remotes.terminal(eid, original_sid, on_event) if eid else pty_client(on_event)
            if eid:
                client.on_close = lambda: ws.close(1011, "SSH disconnected; server session may still be running")
            return client

        try:
            pc = peer()
            sid = getattr(pc, "remote_id", original_sid)
        except wsproto.WSClosed:
            return
        except (OSError, ValueError, remotes.Unavailable) as e:
            ws.close(1011, "ptyhost unavailable: %s" % e)
            return
        watcher = threading.Thread(target=watch_auth, daemon=True)
        watcher.start()
        try:
            require_auth()
            resume = checkpoint is not None and bool(checkpoint)
            r = pc.call("subscribe", id=sid, replay_geometry=replay_geometry or checkpoint is not None,
                        **({"from": checkpoint["offset"]} if resume else {}))

            def supports_checkpoint(reply):
                info = reply.get("info") or {}
                return (info.get("replay_geometry") is True and type(info.get("pid")) is int
                        and info["pid"] > 0 and type(info.get("started")) in (int, float)
                        and math.isfinite(info["started"]) and info["started"] > 0
                        and type(info.get("bytes")) is int and 0 <= info["bytes"] <= 2**53 - 1)

            if resume and r.get("ok"):
                info = r.get("info") or {}
                valid = (supports_checkpoint(r) and info["pid"] == checkpoint["pid"]
                         and info["started"] == checkpoint["started"] and checkpoint["offset"] <= info["bytes"])
                # subscribe replies precede replay. Confirm the actual first offset:
                # the PTY may have trimmed the ring while this client was asleep.
                if valid:
                    valid = replay_done.wait(5)
                if valid:
                    with gate_lock:
                        first = next((e.get("off") for e in gate["held"] if e.get("ev") == "data"), info["bytes"])
                        valid = first == checkpoint["offset"]
                if not valid:
                    # A fresh channel prevents queued partial/live events from the
                    # rejected subscription leaking into the replacement full replay.
                    pc.on_close = None
                    pc.close()
                    with gate_lock:
                        gate["held"] = []
                        replay_done.clear()
                    pc = peer()
                    sid = getattr(pc, "remote_id", original_sid)
                    r = pc.call("subscribe", id=sid, replay_geometry=True)
                    resume = False
            checkpoint_enabled = checkpoint is not None and supports_checkpoint(r)
            if not r.get("ok"):
                ws.send_text(json.dumps({"t": "error", "error": r.get("error")}))
                ws.close(1011)
                return
            with gate_lock:
                require_auth()
                ws.send_text(json.dumps({"t": "hello", "info": r.get("info"),
                                         **({"checkpoint": checkpoint_enabled, "resumed": resume} if checkpoint is not None else {})}))
                for event in gate["held"]:
                    forward(event)
                gate["held"], gate["open"] = [], True
            while True:
                op, payload = ws.recv()
                require_auth()
                if op == wsproto.OP_BIN:
                    data = payload
                    msg = {"t": "in"}
                else:
                    msg = json.loads(payload)
                    data = str(msg.get("d", "")).encode() if msg.get("t") == "in" else b""
                if msg.get("t") == "in" and data:
                    rr = pc.call("input", id=sid, data=b64(data))
                    if not rr.get("ok"):
                        ws.send_text(json.dumps({"t": "refused", "error": rr.get("error")}))
                elif msg.get("t") == "take":   # "Use here": the attached terminal detaches, this view sizes again
                    # take = this connection becomes the TTY (the old one gets tty-taken), the plain
                    # subscribe right after drops the flag again; `from` past the end: no replay
                    pc.call("subscribe", id=sid, tty=True, take=True, **{"from": 1 << 62})
                    pc.call("subscribe", id=sid, **{"from": 1 << 62})
                    common.audit({"event": "tty-take-gui", "session": sid})
                elif msg.get("t") == "resize":
                    cols, rows = int(msg.get("c", 0)), int(msg.get("r", 0))
                    if 10 <= cols <= 1000 and 3 <= rows <= 500:
                        pc.call("resize", id=sid, cols=cols, rows=rows)
        except (wsproto.WSClosed, wsproto.WSProtocolError, OSError, ValueError, PtyHostError, remotes.Unavailable):
            pass
        finally:
            stopped.set()
            pc.close()
            ws.close()
            watcher.join(timeout=1)


class Server(ThreadingHTTPServer):
    """socketserver's listen backlog is 5, and macOS answers a SYN over a full backlog with a reset.
    `tailscale serve` (9.56) turns the browser's one HTTP/2 connection into a backend connection per
    request: the ~60 ES modules of the GUI shell arrive at once and most came back 502."""
    request_queue_size = 128   # kern.ipc.somaxconn's default; the kernel caps a larger value
    daemon_threads = True


def gui_url(remote: bool = False) -> str:
    """The GUI link: loopback with the GUI token, or (9.56) the first PIPER_ALLOWED_HOSTS name over https
    with the separate `remote` token - revoked on its own (`piper.sh remote-url --revoke`) when a device
    is lost. The token only ever travels in the fragment."""
    if not remote:
        return "http://127.0.0.1:%d/#token=%s" % (PORT, common.own_token("gui"))
    names = remote_names()
    if not names:
        raise SystemExit("piperd: no PIPER_ALLOWED_HOSTS - remote access is off")
    return "https://%s/#token=%s" % (names[0], common.own_token("remote"))


class StampedStream:
    """piperd.log had no times: a hang could not be placed against audit.log or a session's events.
    Prefixes every line written to the log with the local time; anything that
    is not a line start (a print's trailing parts, a traceback's middle) passes through as it is."""

    def __init__(self, raw, clock=time.localtime):
        self._raw, self._clock, self._bol = raw, clock, True
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            out = []
            for piece in text.splitlines(keepends=True):
                if self._bol:
                    out.append(time.strftime("%Y-%m-%d %H:%M:%S ", self._clock()))
                out.append(piece)
                self._bol = piece.endswith("\n")
            self._raw.write("".join(out))
        return len(text)

    def __getattr__(self, name):   # flush, fileno, isatty, encoding ... of the real stream
        return getattr(self._raw, name)


def main() -> int:
    global MANAGER
    if "--print-url" not in sys.argv:
        moved = common.migrate_home()
        if moved == "moved":   # every path was computed from the old home: start over
            print("piperd: moved %s to %s/.piper - restarting" % (common.LEGACY_HOME, common.REAL_HOME), flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        if moved == "busy":
            print("piperd: %s stays the home while its sessions run; it moves to ~/.piper at the first start "
                  "after `task piper:stop -- all` or a reboot" % common.LEGACY_HOME, flush=True)
    common.ensure_home()
    bad = check_remote_names()
    if bad:
        sys.exit("piperd: " + bad)
    if "--print-url" in sys.argv:
        print(gui_url("--remote" in sys.argv))
        return 0
    try:
        if not ipaddress.ip_address(BIND).is_loopback:
            sys.exit("piperd: PIPER_BIND must be a loopback address (got %s)" % BIND)
    except ValueError:
        sys.exit("piperd: PIPER_BIND must be an IP address (got %s)" % BIND)
    if not sys.stdout.isatty():   # the log (piper.sh nohup, launchd/systemd), not a terminal run
        sys.stdout, sys.stderr = StampedStream(sys.stdout), StampedStream(sys.stderr)
    import sessions   # after ensure_home: the manager reads $PIPER_HOME at import
    if security.ENABLED:
        settings.migrate_security_approvals()
        settings.migrate_new_cards()
    MANAGER = sessions.Manager()
    providers.start()
    limits.start_thread()   # plan limits are polled independently: neither provider stalls the other
    codexlimits.start_thread()
    srv = Server((BIND, PORT), Handler)
    print("piperd: listening on http://%s:%d (home %s)" % (BIND, PORT, common.PIPER_HOME), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        providers.close()
        MANAGER.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
