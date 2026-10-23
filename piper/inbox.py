"""The GitLab inbox.

A mixin of sessions.Manager like workbench.Workbench. A poller thread on the host reads the
token owner's pending to-dos, assigned issues and own open merge requests (all GET), keeps
what the human has seen in $PIPER_HOME/inbox.json, and fetches notes only where a count
changed. The GUI opens a session from an item (task = references only: the session reads
the issue itself through its project-scoped `gitlab_issue`), marks to-dos done and posts
notes - GitLab writes are GUI clicks only, never a sandbox tool.

Security (9.8, 11.3.4a, 5.4):
  - the PAT stays in piperd; every endpoint here serves issue / MR / note METADATA only
    (never repository files, blobs or diffs);
  - issue text is untrusted: it reaches Claude only through the scoped gitlab_* tools
    (gitlab_inbox answers from the poller's items, filtered to the session's projects, scrubbed);
  - the items keep GitLab's text as it is - the human's own view (L1, 2026-10-07); whatever of it
    goes on to a session (task_for, the session title, Session.issue) is scrubbed on the way:
    session_issue(), task_for(), inbox_start. The Feed, Mattermost and the timesheet scrub their own;
  - automation starts sessions only from rules the GUI wrote ($PIPER_HOME/inbox-rules.json,
    POST /api/inbox/rules); hook events never trigger it, and an automatic session is an
    ordinary sandboxed "analyze only" session without any extra privilege;
  - the comment draft is read only from the issue's comment file in `development/` of the session's own
    project, never through a symlink (common.safe_*) and never when project_hidden hides it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime

import common
import digest
import extensions
import features
import github
import gitlab
import gitops
import projects
from common import audit, read_json, scrub, write_json

INBOX_FILE = os.path.join(common.PIPER_HOME, "inbox.json")
RULES_FILE = os.path.join(common.PIPER_HOME, "inbox-rules.json")
INTERVAL = int(os.environ.get("PIPER_INBOX_INTERVAL", "90"))   # 0: no background poll (tests)
PER_HOUR = int(os.environ.get("PIPER_INBOX_AUTO_PER_HOUR", "3"))
FIRST_POLL = 3.0
DEBOUNCE = 5.0
REFRESH_WAIT = 20.0
KEEP = 30 * 86400          # state of items no source lists any more
ORIGIN_TTL = 600.0         # discovered checkout -> GitLab path (a git subprocess each)
MAP_TTL = 10.0
FLOW_GIT_TTL = 20.0
DRAFT_MAX = 64 << 10
TODO_BODY = 300
FOLLOW_STATES = ("ready", "waiting", "attention")
RESUMABLE = ("blocked", "error", "exited")   # W8: a linked session that ended is resumed, not replaced
RULE_ON = ("assigned", "mentioned", "directly_addressed", "new_note")
# reasons that need the human, in the order the list shows them
NEEDS = ("directly_addressed", "mentioned", "assigned", "build_failed", "unmergeable", "review_requested",
         "approval_required", "marked")
_SEG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"   # a GitLab path segment (never . or ..)
GL_RX = re.compile(r"^%s(?:/%s){1,5}$" % (_SEG, _SEG))
# W23: a GitHub item's path is gh:owner/repo (gh:host/owner/repo on GitHub Enterprise) - a pull request is type mr
GH_PREFIX = "gh:"
GH_GL_RX = re.compile(r"^gh:(?:%s/)?%s/%s$" % (_SEG, _SEG, _SEG))
KEY_RX = re.compile(r"^(?P<gl>(?:gh:)?%s(?:/%s){1,5})#(?P<type>issue|mr)#(?P<iid>[1-9][0-9]{0,9})$" % (_SEG, _SEG))
NOTE_RX = re.compile(r"#note_(\d+)")
URL_RX = re.compile(r"^https?://[^/]+/(.+?)/-/(?:issues|work_items|merge_requests)/\d+")
ANALYSIS_DIRS = ("development/", "analysis/", "docs/analysis/")   # notes, not code; an extension adds its own
# an issue's files in development/ (<stem>-<name>.md) and how its comment draft reads; an extension
# renames them (DEFAULTS issue_files), the GUI gets them with the inbox (issue_files)
ISSUE_FILES = {"status": "status", "analysis": "analysis", "comment": "comment",
               "comment_style": "a short summary on top, technical detail in <details>"}
TASK_URL_RX = re.compile(r"https?://[^/\s]+/(\S+?)/-/(issues|work_items|merge_requests)/(\d+)\b")
# W10: an issue / MR named in a prompt - group/project#7 (or !7), or a bare #7 after a space
REF_RX = re.compile(r"(?<![\w/.-])(%s(?:/%s){1,5})([#!])([1-9][0-9]{0,6})(?![\w-])" % (_SEG, _SEG))
# (a bare one needs 2+ digits: "#1 priority", "step #2" are prose, not issue 1 / 2)
BARE_REF_RX = re.compile(r"(?:^|(?<=[\s(]))([#!])([1-9][0-9]{1,6})(?![\w-])")
TYPES = {"Issue": "issue", "WorkItem": "issue", "MergeRequest": "mr"}
PUBLIC = ("key", "forge", "gl", "type", "iid", "title", "state", "web_url", "labels", "author", "updated_at", "due_date",
          "milestone", "reasons", "todos", "notes_count")


def _ts(s) -> float:
    """Epoch of a GitLab ISO timestamp (0 when missing / unparsable)."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def parse_key(key: str) -> tuple[str, str, int]:
    m = KEY_RX.match(str(key or ""))
    if not m or (m.group("gl").startswith(GH_PREFIX) and not GH_GL_RX.match(m.group("gl"))):
        raise ValueError("invalid inbox key %r" % key)
    return m.group("gl"), m.group("type"), int(m.group("iid"))


def is_gh(gl: str) -> bool:
    """W23: an inbox path of a GitHub repository (gh:owner/repo)."""
    return str(gl or "").startswith(GH_PREFIX)


def gh_entry(gl: str) -> str:
    """gh:owner/repo -> the scope entry owner/repo (host/owner/repo on GHE)."""
    return str(gl)[len(GH_PREFIX):]


def _gl_of(obj: dict, kind: str) -> str | None:
    full = str(((obj.get("references") or {}).get("full")) or "")
    sep = "#" if kind == "issue" else "!"
    if sep in full:
        gl = full.rsplit(sep, 1)[0]
        if GL_RX.match(gl):
            return gl
    m = URL_RX.match(str(obj.get("web_url") or ""))
    return m.group(1) if m and GL_RX.match(m.group(1)) else None


def task_for(item: dict, mode: str, note_id: int | None = None) -> str:
    """The task of a session opened from an inbox item: references only - the session
    reads the issue itself (its gitlab_issue / gitlab_mr is scoped to the project)."""
    mr = item["type"] == "mr"
    iid, gl = int(item["iid"]), item["gl"]
    if is_gh(gl):   # W23: the session's github_* tools take repo + number
        forge, tool, args = "GitHub", "github_pr" if mr else "github_issue", 'repo "%s", number %d' % (gh_entry(gl), iid)
        stem = "github-pr-%d" % iid if mr else "github-%d" % iid
        what = "pull request #%d" % iid if mr else "issue #%d" % iid
    else:
        forge, tool, args = "GitLab", "gitlab_mr" if mr else "gitlab_issue", 'project "%s", iid %d' % (gl, iid)
        stem = "gitlab-mr-%d" % iid if mr else "gitlab-%d" % iid
        what = "merge request !%d" % iid if mr else "issue #%d" % iid
    head = "%s %s: %s\n%s\n\n" % (forge, what, scrub(str(item.get("title") or "")), item.get("web_url") or "")
    if note_id:   # W9: a discussion is under way - answer its latest point, do not start the issue over
        head += ("Handle comment note_%d (and anything newer) - that is the task now. Read it with the tool %s "
                 "(%s). The description and older comments are context only: do not "
                 "re-analyse the whole %s, look back only where the comment needs it.\n"
                 % (int(note_id), tool, args, ("pull request" if is_gh(gl) else "merge request") if mr else "issue"))
    else:
        head += ("Read the task including all comments with the tool %s (%s) - the comments "
                 "are part of the task.\n" % (tool, args))
    if mode == "analyze":
        return head + ("Analyze only: change no code, do not commit, send nothing. Clarify the task, find the "
                       "affected code, propose solution options with a recommendation and an estimate and save it "
                       "to %s; in your reply give a short summary and questions for the "
                       "user." % issue_file(stem, "analysis"))
    return head + ("Clarify the task first, then implement and verify.\n"
                   "Save a %s comment draft to %s (%s). Do not post it - the user sends it from the GUI."
                   % (forge, issue_file(stem, "comment"), issue_files()["comment_style"]))


def analysis_dirs() -> tuple:
    """ANALYSIS_DIRS and every extension's ANALYSIS_DIRS."""
    return ANALYSIS_DIRS + tuple(d for d in extensions.items("ANALYSIS_DIRS") if d not in ANALYSIS_DIRS)


def issue_files() -> dict:
    """ISSUE_FILES with an extension's issue_files over it."""
    return {**ISSUE_FILES, **extensions.default("issue_files", {})}


def issue_file(stem: str, kind: str) -> str:
    """development/<stem>-<name>.md of an issue's status / analysis / comment file."""
    return "development/%s-%s.md" % (stem, issue_files()[kind])


def target_note(item: dict) -> int | None:
    """W9: the comment a session opened from `item` answers - the newest one addressed to me (a to-do
    of a mention), else the first new one by others, else the latest comment at all; None = no
    discussion yet (the session reads the whole issue)."""
    addressed = [int(t["note_id"]) for t in item.get("todos") or [] if t.get("note_id") and
                 t.get("action") in ("directly_addressed", "mentioned")]
    return max(addressed) if addressed else item.get("first_new_note") or item.get("last_note") or None


def mention_key(text: str, scope: list[str]) -> str | None:
    """W10: the inbox key of the first issue / MR a human prompt names in the session's GitLab scope:
    a URL, else group/project#N, else a bare #N when the session has exactly one GitLab project."""
    text = str(text or "")[:4000]
    for m in TASK_URL_RX.finditer(text):
        if m.group(1) in scope:
            return "%s#%s#%s" % (m.group(1), "mr" if m.group(2) == "merge_requests" else "issue", m.group(3))
    for m in REF_RX.finditer(text):
        if m.group(1) in scope:
            return "%s#%s#%s" % (m.group(1), "mr" if m.group(2) == "!" else "issue", m.group(3))
    if len(scope) == 1:
        m = BARE_REF_RX.search(TASK_URL_RX.sub(" ", text))
        if m:
            return "%s#%s#%s" % (scope[0], "mr" if m.group(1) == "!" else "issue", m.group(2))
    return None


# W23: a GitHub issue / PR URL - host, owner, repo, issues|pull, number
GH_URL_RX = re.compile(r"https://([A-Za-z0-9.-]+)/(%s)/(%s)/(issues|pull)/([1-9][0-9]{0,6})(?![\w-])" % (_SEG, _SEG))


def gh_mention_key(text: str, scope: list[str], bare: bool = True) -> tuple[str, bool] | None:
    """W10 for GitHub: (inbox key, kind sure) of the first issue / PR a human prompt names among the session's
    repositories (owner/repo, host/owner/repo on GHE): a URL (kind known), else owner/repo#N, else a bare #N
    when `bare` and the session has exactly one repository. #N is an issue or a PR alike on GitHub: the key
    says issue and `sure` is False - the inbox or GitHub decides."""
    text = str(text or "")[:4000]
    entries = {e.lower(): e for e in scope if isinstance(e, str)}

    def key(entry: str, kind: str, n: str) -> str:
        return "%s%s#%s#%s" % (GH_PREFIX, entry.lower(), kind, n)
    for m in GH_URL_RX.finditer(text):
        host, repo = m.group(1).lower(), ("%s/%s" % (m.group(2), m.group(3))).lower()
        hit = repo if host == "github.com" and repo in entries else "%s/%s" % (host, repo)
        if hit in entries:
            return key(hit, "mr" if m.group(4) == "pull" else "issue", m.group(5)), True
    for m in REF_RX.finditer(text):
        if m.group(2) == "#" and m.group(1).lower() in entries:
            return key(m.group(1), "issue", m.group(3)), False
    if bare and len(entries) == 1:
        m = BARE_REF_RX.search(GH_URL_RX.sub(" ", TASK_URL_RX.sub(" ", text)))
        if m and m.group(1) == "#":
            return key(next(iter(entries)), "issue", m.group(2)), False
    return None


def session_issue(item: dict) -> dict:
    """Session.issue of an inbox item: its title scrubbed - it goes on to the issue state file the
    session reads (issuestate.template) and to the timesheet."""
    out = {k: item[k] for k in ("key", "gl", "type", "iid", "title", "web_url")}
    out["title"] = scrub(str(out["title"] or ""))[:500]
    if is_gh(out["gl"]):
        out["forge"] = "github"
    return out


class Inbox:
    def _inbox_init(self) -> None:
        self._ib_lock = threading.RLock()
        self._ib_poll_lock = threading.Lock()
        self._ib_status = {"status": "starting", "at": None, "error": None, "me": None}
        self._ib_items: dict[str, dict] = {}
        self._ib_sig = ""
        self._ib_v = 1
        self._ib_polled = 0.0
        st = read_json(INBOX_FILE, None)
        self._ib_state = st if isinstance(st, dict) and isinstance(st.get("items"), dict) else \
            {"items": {}, "baseline_done": False}
        rules = (read_json(RULES_FILE, {}) or {}).get("rules")
        self._ib_rules = [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        self._ib_origin: dict[str, tuple[float, str | None]] = {}   # checkout path -> (time, gl path)
        self._ib_map_cache = None
        self._flow_cache: dict = {}   # session id -> (transcript key, tool flags)
        self._flow_git: dict = {}     # session id -> (time, git + draft part)
        self._ib_probes: set[tuple[str, str]] = set()   # W10: (session, key) GitLab was asked about
        # W23: GitHub's own poll status, its items' signature and each host's notifications (If-Modified-Since)
        self._ib_gh_status = {"status": "off", "at": None, "error": None, "me": None}
        self._ib_gh_sig = ""
        self._ib_gh_notif: dict[str, tuple[str, list]] = {}

    def _inbox_start_thread(self) -> None:
        if INTERVAL > 0:
            self._start_service(self._inbox_loop, "piper-inbox")

    def _inbox_loop(self) -> None:
        if self._stop_event.wait(FIRST_POLL):
            return
        while not self._stop_event.is_set():
            try:
                if features.on("gitlab"):   # the module off: no background GitLab calls
                    with self._ib_poll_lock:
                        self._inbox_poll()
                if features.on("github"):   # W23: the same inbox, GitHub's half
                    with self._ib_poll_lock:
                        self._inbox_poll_github()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                print("piperd: inbox: %s: %s" % (type(e).__name__, e), flush=True)
                self._ib_failed(e)
            self._stop_event.wait(max(10, INTERVAL))

    def _ib_failed(self, e: Exception) -> None:
        """An unexpected poll failure shows in the GUI like a GitLab error (the last good items stay)."""
        with self._ib_lock:
            self._ib_status = dict(self._ib_status, status="error", at=time.time(),
                                   error=("%s: %s" % (type(e).__name__, e))[:500])
        self._ib_bump()

    # ------------------------------------------------------------ plumbing
    def _ib_bump(self) -> None:
        with self._ib_lock:
            self._ib_v += 1
        self.bump()

    def _ib_save(self) -> None:
        with self._ib_lock:
            write_json(INBOX_FILE, self._ib_state)

    def _ib_gl(self, tok: str) -> gitlab.GitLab:
        return gitlab.GitLab(self.gitlab_host(), tok, opener=self.gitlab_opener)

    def _ib_pp(self) -> str:
        import sessions   # noqa: PLC0415 - sessions imports this module
        return sessions.PROJECT_PATH

    # ------------------------------------------------------------ poll
    def _inbox_poll(self) -> None:
        self._ib_polled = time.time()
        tok = gitlab.token()
        if not tok:
            with self._ib_lock:
                changed = self._ib_status["status"] != "no-token"
                self._ib_status = {"status": "no-token", "at": time.time(), "error": None, "me": None}
                self._ib_items = {k: v for k, v in self._ib_items.items() if is_gh(v["gl"])}
            if changed:
                self._ib_bump()
            return
        gl = self._ib_gl(tok)
        try:
            me = gitlab._me(gl)
            if not me:
                raise gitlab.GitLabError(502, "GitLab /user did not answer (is the token valid?)")
            todos, issues, mrs = gitlab.todos(gl), gitlab.assigned_issues(gl), gitlab.own_mrs(gl)
        except gitlab.GitLabError as e:
            with self._ib_lock:   # the last good items stay
                self._ib_status = dict(self._ib_status, status="error", error=str(e)[:500], at=time.time())
            self._ib_bump()
            return
        fresh = self._ib_collect(todos, issues, mrs)
        self._ib_map(refresh=True)   # the poller is the only caller that may run git for origins
        self._ib_absorb(fresh, me, lambda it: gitlab.recent_notes(gl, it["gl"], it["type"], it["iid"]), gh=False)

    def _ib_absorb(self, fresh: dict, me: str, fetch_notes, gh: bool) -> None:
        """One forge's poll result into the inbox: note counts, what is new, automation events.
        The other forge's items stay as its own last poll left them (W23)."""
        now = time.time()
        bkey = "gh_baseline_done" if gh else "baseline_done"
        with self._ib_lock:
            baseline = not self._ib_state.get(bkey)
            sitems = self._ib_state["items"]
            fetch = [k for k, it in fresh.items() if it["notes_count"] != (sitems.get(k) or {}).get("notes_count")]
        notes: dict[str, list] = {}
        for k in fetch:
            it = fresh[k]
            if not it["notes_count"]:
                notes[k] = []
                continue
            try:
                notes[k] = fetch_notes(it)
            except gitlab.GitLabError:   # GitHubError is one too
                continue   # the stored count stays: retried on the next poll
        events: list[tuple[str, dict]] = []   # new notes by others that appeared in this poll
        with self._ib_lock:
            for k, it in fresh.items():
                st = sitems.get(k)
                if st is None:
                    st = sitems[k] = {"first_seen": now, "seen_at": now if baseline else None, "seen_count": None,
                                      "seen_note": None, "notes_count": None, "last_note": None,
                                      "new_note_ids": [], "others": [], "auto": []}
                st["listed_at"] = now
                if baseline:   # what is pending at the baseline is never an automation event
                    st["handled"] = sorted(set(st.get("handled") or []) | {"todo:%s" % t["id"] for t in it["todos"]})
                if k not in notes:
                    continue
                rows = notes[k]
                last = max([n["id"] for n in rows] + [int(st.get("last_note") or 0)])
                if st.get("seen_note") is None:   # first sight: what is there now belongs to the item
                    st["seen_note"], st["seen_count"] = last, it["notes_count"]
                prev = set(st.get("new_note_ids") or [])
                others = [n for n in rows if n["user"] != me]
                new = {n["id"] for n in others if n["id"] > st["seen_note"]} | {i for i in prev if i > st["seen_note"]}
                events += [(k, n) for n in others if n["id"] in new and n["id"] not in prev]
                st.update(notes_count=it["notes_count"], last_note=last or None, new_note_ids=sorted(new),
                          others=sorted(set(st.get("others") or []) | {n["id"] for n in others})[-50:])
            for k in [k for k, st in sitems.items() if now - float(st.get("listed_at") or now) > KEEP]:
                sitems.pop(k, None)
            self._ib_state[bkey] = True
            self._ib_items = dict({k: v for k, v in self._ib_items.items() if is_gh(v["gl"]) != gh}, **fresh)
            sig = hashlib.sha1(json.dumps([fresh, {k: {a: b for a, b in st.items() if a != "listed_at"}
                                                   for k, st in sitems.items()}],
                                          sort_keys=True, default=str).encode()).hexdigest()
            if gh:
                was_ok = self._ib_gh_status["status"] == "ok"
                self._ib_gh_status = {"status": "ok", "at": now, "error": None, "me": me}
                changed, self._ib_gh_sig = sig != self._ib_gh_sig or not was_ok, sig
            else:
                was_ok = self._ib_status["status"] == "ok"
                self._ib_status = {"status": "ok", "at": now, "error": None, "me": me}
                changed, self._ib_sig = sig != self._ib_sig or not was_ok, sig
            self._ib_save()
        if changed:
            self._ib_bump()
        if not baseline:
            self._inbox_automate(events)

    @staticmethod
    def _ib_collect(todos: list, issues: list, mrs: list) -> dict[str, dict]:
        items: dict[str, dict] = {}

        def item(gl: str, kind: str, obj: dict) -> dict | None:
            try:
                iid = int(obj.get("iid"))
            except (TypeError, ValueError):
                return None
            if not gl or not GL_RX.match(gl) or iid <= 0:
                return None
            key = "%s#%s#%d" % (gl, kind, iid)
            it = items.setdefault(key, {"key": key, "forge": "gitlab", "gl": gl, "type": kind, "iid": iid,
                                        "reasons": [], "todos": [], "listed": []})
            ms = obj.get("milestone")
            it.update({
                # raw: the human's view (L1) - session_issue() scrubs what goes on to a session; no bidi
                # controls in any title the GUI shows (Trojan Source)
                "title": common.no_bidi(str(obj.get("title") or ""))[:500], "state": obj.get("state"),
                "web_url": obj.get("web_url"),
                "labels": [common.no_bidi(str(x)) for x in obj.get("labels") or []][:30],
                "author": gitlab._people(obj.get("author")), "updated_at": obj.get("updated_at"),
                "due_date": obj.get("due_date"), "milestone": common.no_bidi(ms["title"]) if isinstance(ms, dict) and isinstance(ms.get("title"), str) else None,
                "notes_count": int(obj.get("user_notes_count") or 0)})
            return it

        def reason(it: dict, r: str) -> None:
            if r and r not in it["reasons"]:
                it["reasons"].append(r)

        for t in todos:
            kind = TYPES.get(str(t.get("target_type") or ""))
            target = t.get("target") if isinstance(t.get("target"), dict) else None
            if not kind or target is None:
                continue   # epics, designs, commits, alerts: not an issue / MR
            gl = str((t.get("project") or {}).get("path_with_namespace") or "") or _gl_of(target, kind) or ""
            it = item(gl, kind, target)
            if it is None:
                continue
            action = str(t.get("action_name") or "")
            reason(it, action)
            m = NOTE_RX.search(str(t.get("target_url") or ""))
            it["todos"].append({"id": t.get("id"), "action": action, "author": gitlab._people(t.get("author")),
                                "created_at": t.get("created_at"), "note_id": int(m.group(1)) if m else None,
                                "body": str(t.get("body") or "")[:TODO_BODY]})   # raw (L1): gitlab_inbox scrubs its copy
        for kind, rows, why in (("issue", issues, "assigned"), ("mr", mrs, "authored")):
            for r in rows:
                it = item(_gl_of(r, kind) or "", kind, r)
                if it is not None:
                    reason(it, why)
                    it["listed"].append(why)
        return items

    # ------------------------------------------------------------ W23: GitHub's half
    def _inbox_poll_github(self) -> None:
        """Every host of policy github_hosts with a `gh` login: unread notifications (If-Modified-Since,
        a 304 costs no rate limit), open issues / PRs assigned to me, review requests, my open PRs.
        All GET; the token stays in piperd (github.token)."""
        self._ib_polled = time.time()
        fresh: dict[str, dict] = {}
        me = None
        tokens = [(h, github.token(h)) for h in self.github_hosts()]
        if not any(t for _, t in tokens):
            with self._ib_lock:
                changed = self._ib_gh_status["status"] != "no-token"
                self._ib_gh_status = {"status": "no-token", "at": time.time(), "error": None, "me": None}
                self._ib_items = {k: v for k, v in self._ib_items.items() if not is_gh(v["gl"])}
            if changed:
                self._ib_bump()
            return
        clients = {}
        try:
            for host, tok in tokens:
                if not tok:
                    continue
                gh = clients[host] = github.GitHub(host, tok, opener=getattr(self, "github_opener", None))
                login = github.me(gh)
                if not login:
                    raise github.GitHubError(502, "GitHub /user on %s did not answer (is `gh auth status` ok?)" % host)
                me = me or login
                since, last = self._ib_gh_notif.get(host, ("", []))
                rows, since = github.notifications(gh, since)
                if rows is None:   # 304: nothing changed since the last poll
                    rows = last
                self._ib_gh_notif[host] = (since, rows)
                fresh.update(self._ib_collect_gh(host, rows, github.assigned_issues(gh), github.review_requests(gh),
                                                 github.own_prs(gh)))
        except gitlab.GitLabError as e:   # GitHubError: the last good items stay
            with self._ib_lock:
                self._ib_gh_status = dict(self._ib_gh_status, status="error", error=str(e)[:500], at=time.time())
            self._ib_bump()
            return
        self._ib_map(refresh=True)

        def notes(it: dict) -> list:
            host, repo = github.split(gh_entry(it["gl"]))
            return github.recent_notes(clients[host], repo, it["type"], it["iid"])
        self._ib_absorb(fresh, me, notes, gh=True)

    # a notification's reason in the inbox's words (NEEDS); the rest only says something happened
    GH_REASONS = {"mention": "mentioned", "team_mention": "mentioned", "review_requested": "review_requested",
                  "assign": "assigned", "approval_requested": "approval_required"}
    GH_SUBJECT_RX = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/(issues|pulls)/([1-9][0-9]{0,9})$")
    GH_COMMENT_RX = re.compile(r"/issues/comments/([1-9][0-9]{0,15})$")

    @classmethod
    def _ib_collect_gh(cls, host: str, notifs: list, assigned: list, reviews: list, own: list) -> dict[str, dict]:
        """Items keyed gh:owner/repo#issue#7 / gh:owner/repo#mr#7 (lower case: GitHub names are
        case-insensitive); a notification thread is the item's to-do (inbox_done marks it read)."""
        items: dict[str, dict] = {}

        def entry(repo: str) -> str:
            return (repo if host == "github.com" else "%s/%s" % (host, repo)).lower()

        def item(repo: str, kind: str, num, obj: dict | None) -> dict | None:
            try:
                iid = int(num)
            except (TypeError, ValueError):
                return None
            gl = GH_PREFIX + entry(repo or "")
            if not repo or not GH_GL_RX.match(gl) or iid <= 0:
                return None
            key = "%s#%s#%d" % (gl, kind, iid)
            it = items.setdefault(key, {"key": key, "forge": "github", "gl": gl, "type": kind, "iid": iid,
                                        "reasons": [], "todos": [], "listed": [], "title": "", "state": "opened",
                                        "web_url": "https://%s/%s/%s/%d" % (host, repo, "pull" if kind == "mr" else "issues", iid),
                                        "labels": [], "author": None, "updated_at": None, "due_date": None,
                                        "milestone": None, "notes_count": 0})
            if obj is not None:   # an issue / search row: the whole metadata (a notification has only a title)
                sh = github._shape(obj)
                url = sh.get("web_url")
                it.update({"title": common.no_bidi(str(sh.get("title") or ""))[:500], "state": sh.get("state"),
                           "web_url": url if isinstance(url, str) and url.startswith("https://") else it["web_url"],
                           "labels": [common.no_bidi(str(x)) for x in sh.get("labels") or []][:30],
                           "author": sh.get("author"), "updated_at": sh.get("updated_at"),
                           "milestone": sh.get("milestone"), "notes_count": int(obj.get("comments") or 0)})
            return it

        def reason(it: dict, r: str) -> None:
            if r and r not in it["reasons"]:
                it["reasons"].append(r)

        for kind_rows, why in ((assigned, "assigned"), (reviews, "review_requested"), (own, "authored")):
            for r in kind_rows:
                if not isinstance(r, dict):
                    continue
                kind = "mr" if r.get("pull_request") else "issue"
                it = item(github.repo_of(r) or "", kind, r.get("number"), r)
                if it is not None:
                    reason(it, why)
                    it["listed"].append(why)
        for n in notifs:
            subj = n.get("subject") if isinstance(n, dict) else None
            if not isinstance(subj, dict) or subj.get("type") not in ("Issue", "PullRequest"):
                continue   # releases, discussions, CI: not an issue / PR
            m = cls.GH_SUBJECT_RX.search(str(subj.get("url") or ""))
            if not m:
                continue
            kind = "mr" if m.group(2) == "pulls" else "issue"
            it = item(m.group(1), kind, m.group(3), None)
            if it is None:
                continue
            if not it["title"]:
                it["title"] = common.no_bidi(str(subj.get("title") or ""))[:500]
                it["updated_at"] = n.get("updated_at")
            action = cls.GH_REASONS.get(str(n.get("reason") or ""), "activity")
            reason(it, action)
            c = cls.GH_COMMENT_RX.search(str(subj.get("latest_comment_url") or ""))
            it["todos"].append({"id": str(n.get("id") or ""), "action": action, "author": None,
                                "created_at": n.get("updated_at"), "note_id": int(c.group(1)) if c else None,
                                "body": ""})
        return items

    # ------------------------------------------------------------ project mapping
    def _ib_origin_gh(self, path: str, refresh: bool) -> str | None:
        """W23: gh:owner/repo of a checkout's origin, cached like _ib_origin_gl."""
        hit = self._ib_origin.get(GH_PREFIX + path)
        if hit and (not refresh or time.time() - hit[0] < ORIGIN_TTL):
            return hit[1]
        if not refresh:
            return None
        r = self.origin_gh(path)
        gl = GH_PREFIX + r.lower() if r else None
        self._ib_origin[GH_PREFIX + path] = (time.time(), gl)
        return gl

    def _ib_origin_gl(self, path: str, refresh: bool) -> str | None:
        hit = self._ib_origin.get(path)
        if hit and (not refresh or time.time() - hit[0] < ORIGIN_TTL):
            return hit[1]
        if not refresh:
            return None   # only the poller runs git for this (never a GET)
        gl = self.origin_gl(path)
        self._ib_origin[path] = (time.time(), gl)
        return gl

    def _ib_map(self, refresh: bool = False) -> tuple[dict, dict]:
        """(gl -> local project key, gl -> [{path, key}] of unmapped candidates).
        projects.json entries win (their `gitlab` list, else their origin); a GitLab project with
        exactly one discovered checkout maps to it; several discovered ones are candidates."""
        hit = self._ib_map_cache
        if hit and not refresh and time.time() - hit[0] < MAP_TTL:
            return hit[1]
        explicit: dict[str, list] = {}
        disc: dict[str, list] = {}
        for key, p in sorted(projects.load(self._ib_pp()).items()):
            if not p["exists"]:
                continue
            gls = p["gitlab"] or [g for g in [self._ib_origin_gl(p["path"], refresh)] if g]
            if features.on("github"):   # W23: gh:owner/repo paths next to GitLab's
                gls = list(gls) + ([GH_PREFIX + r.lower() for r in p.get("github") or []]
                                   or [g for g in [self._ib_origin_gh(p["path"], refresh)] if g])
            for g in gls:
                if p["source"] == "projects.json":
                    explicit.setdefault(g, []).append((key, p["path"]))
                else:
                    disc.setdefault(g, []).append({"path": p["path"], "key": key})
        mapped = {}
        for g, lst in explicit.items():   # several entries may list g for read scope: its own checkout wins
            own = [k for k, path in lst if len(lst) > 1 and
                   (self._ib_origin_gh if is_gh(g) else self._ib_origin_gl)(path, refresh) == g]
            mapped[g] = (own or [lst[0][0]])[0]
        for g, lst in disc.items():
            if g not in mapped and len(lst) == 1:
                mapped[g] = lst[0]["key"]
        cands = {g: lst for g, lst in disc.items() if g not in mapped}
        self._ib_map_cache = (time.time(), (mapped, cands))
        return mapped, cands

    def _ib_linked(self) -> dict[str, list]:
        """inbox key -> sessions of it, newest first: Session.issue, or - for a session started
        from the issue picker before 9.8 - the issue URL at the top of its task in its own scope."""
        out: dict[str, list] = {}
        for s in sorted(list(self.sessions.values()), key=lambda x: -x.created):
            if s.hidden:
                continue
            key = s.issue.get("key") if isinstance(s.issue, dict) else None
            if not key and s.kind == "project":
                m = TASK_URL_RX.search((s.task or "")[:2000])
                if m and m.group(1) in (s.gitlab or []):
                    key = "%s#%s#%s" % (m.group(1), "mr" if m.group(2) == "merge_requests" else "issue", m.group(3))
                    it = self._ib_items.get(key)
                    if it is not None:   # adopt the link: draft, follow-up and the card chip need it
                        s.issue = session_issue(it)
                        s.save()
            if key:
                out.setdefault(key, []).append(s)
        return out

    # ------------------------------------------------------------ views
    def _ib_view(self, it: dict, mapped: dict, cands: dict, linked: dict) -> dict:
        st = self._ib_state["items"].get(it["key"]) or {}
        new_ids = list(st.get("new_note_ids") or [])
        out = {k: it.get(k) for k in PUBLIC}
        out["reasons"], out["todos"], out["labels"] = list(it["reasons"]), [dict(t) for t in it["todos"]], list(it["labels"])
        out["listed"] = list(it.get("listed") or [])
        project = mapped.get(it["gl"])
        out.update(new_notes=len(new_ids), first_new_note=min(new_ids) if new_ids else None,
                   # W9: the latest comment by someone else (my own last reply is no task), else the latest
                   last_note=max(st.get("others") or [0]) or st.get("last_note") or None,
                   new=st.get("seen_at") is None, seen_at=st.get("seen_at"), project=project,
                   candidates=[] if project else [dict(c) for c in cands.get(it["gl"], [])],
                   sessions=[s.id for s in linked.get(it["key"], [])],
                   auto=[dict(a) for a in st.get("auto") or []])
        out["target_note"] = target_note(out)
        return out

    @staticmethod
    def _ib_order(v: dict):
        news = v["new"] or v["new_notes"] > 0
        ranks = [NEEDS.index(r) for r in v["reasons"] if r in NEEDS]
        rank = min(ranks) if ranks else (len(NEEDS) + (1 if v["reasons"] == ["authored"] else 0))
        return (0 if news else 1, rank, -_ts(v.get("updated_at")))

    def _ib_views(self) -> list[dict]:
        mapped, cands = self._ib_map()
        linked = self._ib_linked()
        with self._ib_lock:
            # a module that is off shows nothing of its forge (W23: rows of the other forge stay)
            on = {"gitlab": features.on("gitlab"), "github": features.on("github")}
            views = [self._ib_view(it, mapped, cands, linked) for it in self._ib_items.values()
                     if on[it.get("forge") or "gitlab"]]
        return sorted(views, key=self._ib_order)

    def _ib_used_hour(self) -> int:
        cut = time.time() - 3600
        with self._ib_lock:
            # sessions, not events: one start records every event it answered
            return len({a["session"] for st in self._ib_state["items"].values() for a in st.get("auto") or []
                        if a.get("session") and float(a.get("at") or 0) > cut})

    def inbox(self) -> dict:
        items = self._ib_views()
        with self._ib_lock:
            st = dict(self._ib_status)
            rules = [dict(r) for r in self._ib_rules]
            gh = dict(self._ib_gh_status) if features.on("github") else None
        if not features.on("gitlab"):   # W23: only GitHub's half is on - GitLab's status says so, not "starting"
            st = {"status": "off", "at": None, "error": None, "me": None}
        return {"status": st["status"], "at": st["at"], "error": st["error"], "me": st["me"], "interval": INTERVAL,
                "items": items, "rules": rules, "limits": {"per_hour": PER_HOUR, "used_hour": self._ib_used_hour()},
                "github": gh, "issue_files": issue_files()}

    def inbox_summary(self) -> dict:
        """The /api/state part: cheap (no mapping, no git); `v` moves when the GUI must refetch."""
        with self._ib_lock:
            sitems = self._ib_state["items"]
            new = needs = total = 0
            on = {"gitlab": features.on("gitlab"), "github": features.on("github")}
            for k, it in self._ib_items.items():
                if not on[it.get("forge") or "gitlab"]:   # as _ib_views: a module that is off counts nothing
                    continue
                total += 1
                st = sitems.get(k) or {}
                fresh_notes = bool(st.get("new_note_ids"))
                new += 1 if (st.get("seen_at") is None or fresh_notes) else 0
                needs += 1 if (it["todos"] or fresh_notes) else 0
            return {"v": self._ib_v, "status": self._ib_status["status"] if features.on("gitlab") else "off", "new": new, "total": total,
                    "needs": needs, "github": self._ib_gh_status["status"] if features.on("github") else None}

    def _ib_item(self, key: str) -> dict:
        parse_key(key)
        with self._ib_lock:
            it = self._ib_items.get(key)
        if it is None:
            raise KeyError("%s is not in the inbox" % key)
        mapped, cands = self._ib_map()
        return self._ib_view(it, mapped, cands, self._ib_linked())

    # ------------------------------------------------------------ W10: an issue named in a prompt
    def issue_from_prompt(self, s, prompt: str) -> None:
        """A human prompt names an issue / MR: an unlinked session gets it as its issue (the header chip,
        the inbox's resume, the timesheet) - from the inbox when it is there, else once GitLab confirms it
        exists (a bare #333 may be a colour). An existing link is never replaced. W23: GitLab first, then
        the session's GitHub repositories (a bare #N only without a GitLab project)."""
        if isinstance(s.issue, dict) or s.kind != "project":
            return
        gl_scope = [g for g in s.gitlab or [] if isinstance(g, str)] if features.on("gitlab") else []
        key, sure = mention_key(prompt, gl_scope) if gl_scope else None, True
        if not key and features.on("github"):   # W23: the session's GitHub repositories
            hit = gh_mention_key(prompt, self.gh_scope(s), bare=not gl_scope)
            key, sure = hit if hit else (None, True)
        if not key:
            return
        with self._ib_lock:
            it = self._ib_items.get(key)
            if it is None and not sure:   # a GitHub #N the inbox knows as a PR
                it = self._ib_items.get(key.replace("#issue#", "#mr#"))
        if it is not None:
            self._link_issue(s, session_issue(it))
            return
        if (s.id, key) in self._ib_probes:   # asked once: a missing one stays missing
            return
        self._ib_probes.add((s.id, key))
        threading.Thread(target=self._issue_probe, args=(s, key), daemon=True, name="issue-probe").start()

    def _issue_probe(self, s, key: str) -> None:
        gl_path, kind, iid = parse_key(key)
        if is_gh(gl_path):
            return self._issue_probe_gh(s, gl_path, iid)
        tok = gitlab.token()
        if not tok:
            return
        try:
            d = self._ib_gl(tok)._req("GET", "/projects/%s/%s/%d" % (gitlab.GitLab._pid(gl_path),
                                                                     "merge_requests" if kind == "mr" else "issues", iid))
        except (gitlab.GitLabError, OSError, ValueError):
            return
        if not isinstance(d, dict) or d.get("iid") != iid:
            return
        url = d.get("web_url")
        self._link_issue(s, session_issue({"key": key, "gl": gl_path, "type": kind, "iid": iid, "title": d.get("title"),
                                           "web_url": url if isinstance(url, str) and url.startswith("https://") else None}))

    def _issue_probe_gh(self, s, gl_path: str, n: int) -> None:
        """W23: GitHub's /issues/N answers for a PR too (pull_request set) - that decides the kind."""
        try:
            gh, repo = self._ib_gh(gl_path)
            d = gh._req("GET", "/repos/%s/issues/%d" % (repo, n))
        except (github.GitHubError, OSError, ValueError):
            return
        if not isinstance(d, dict) or d.get("number") != n:
            return
        kind = "mr" if d.get("pull_request") else "issue"
        url = d.get("html_url")
        self._link_issue(s, session_issue({"key": "%s#%s#%d" % (gl_path, kind, n), "gl": gl_path, "type": kind, "iid": n,
                                           "title": d.get("title"),
                                           "web_url": url if isinstance(url, str) and url.startswith("https://") else None}))

    def _link_issue(self, s, issue: dict) -> None:
        with s.lock:
            if isinstance(s.issue, dict):
                return
            s.issue = issue
            s.save()
        audit({"event": "issue-linked", "session": s.id, "key": issue["key"], "via": "prompt"})
        self._ib_bump()

    # ------------------------------------------------------------ GUI actions
    def inbox_refresh(self) -> dict:
        if time.time() - self._ib_polled >= DEBOUNCE and self._ib_poll_lock.acquire(timeout=REFRESH_WAIT):
            try:
                if time.time() - self._ib_polled >= DEBOUNCE:   # a poll that just ran counts
                    try:
                        if features.on("gitlab"):
                            self._inbox_poll()
                        if features.on("github"):
                            self._inbox_poll_github()
                    except Exception as e:  # noqa: BLE001 - shown in the GUI, then the request fails
                        self._ib_failed(e)
                        raise
            finally:
                self._ib_poll_lock.release()
        audit({"event": "gui-inbox-refresh"})
        return self.inbox()

    def _ib_mark_seen(self, key: str) -> bool:
        with self._ib_lock:
            st = self._ib_state["items"].get(key)
            if st is None:
                return False
            st["undo"] = {"seen_note": st.get("seen_note"), "new_note_ids": list(st.get("new_note_ids") or []),
                          "seen_at": st.get("seen_at")}
            st.update(seen_at=time.time(), seen_count=st.get("notes_count"),
                      seen_note=max(int(st.get("last_note") or 0), int(st.get("seen_note") or 0)), new_note_ids=[])
            return True

    def inbox_seen(self, keys=None, all_: bool = False) -> dict:
        if all_:
            with self._ib_lock:
                keys = list(self._ib_items)
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys) or len(keys) > 1000:
            raise ValueError("keys must be a list of inbox keys (or all: true)")
        n = sum(1 for k in keys if self._ib_mark_seen(k))
        self._ib_save()
        self._ib_bump()
        audit({"event": "gui-inbox-seen", "keys": keys[:50], "count": n})
        return {"ok": True, "seen": n}

    def inbox_unseen(self, key: str) -> dict:
        parse_key(key)
        with self._ib_lock:
            st = self._ib_state["items"].get(key)
            if st is None:
                raise KeyError("%s is not in the inbox" % key)
            undo = st.pop("undo", None)
            if undo:   # back to what was new before the last "seen"
                st.update(seen_at=None, seen_note=undo.get("seen_note"), new_note_ids=undo.get("new_note_ids") or [])
            else:
                st["seen_at"] = None
            if not st.get("new_note_ids") and st.get("others"):
                st["new_note_ids"] = [max(st["others"])]   # at least the newest note by someone else
            self._ib_save()
        self._ib_bump()
        audit({"event": "gui-inbox-unseen", "key": key})
        return {"ok": True}

    def _ib_gh(self, gl: str) -> tuple[github.GitHub, str]:
        """W23: (client, owner/repo) of a gh: inbox path; its host must be in policy github_hosts."""
        if not features.on("github"):
            raise ValueError("the GitHub module is off")
        host, repo = github.split(gh_entry(gl))
        if host not in self.github_hosts():
            raise ValueError("%s is not in policy github_hosts" % host)
        tok = github.token(host)
        if not tok:
            raise github.GitHubError(503, "no GitHub login for piperd - run `gh auth login --hostname %s`" % host)
        return github.GitHub(host, tok, opener=getattr(self, "github_opener", None)), repo

    def inbox_done(self, key: str) -> dict:
        it = self._ib_item(key)
        done = []
        if is_gh(it["gl"]):   # W23: a GitHub "to-do" is an unread notification thread
            gh, _ = self._ib_gh(it["gl"])
            for t in it["todos"]:
                github.mark_notification_read(gh, int(t["id"]))
                done.append(t["id"])
        else:
            tok = gitlab.token()
            if not tok:
                raise gitlab.GitLabError(503, "no GitLab token for piperd")
            gl = self._ib_gl(tok)
            for t in it["todos"]:
                gitlab.mark_todo_done(gl, int(t["id"]))
                done.append(t["id"])
        with self._ib_lock:
            raw = self._ib_items.get(key)
            if raw is not None:   # until the next poll: what the lists still say, and the other to-dos
                raw["todos"] = [t for t in raw["todos"] if t["id"] not in done]
                keep = {t["action"] for t in raw["todos"]} | set(raw.get("listed") or [])
                raw["reasons"] = [r for r in raw["reasons"] if r in keep]
        self._ib_bump()
        audit({"event": "gui-todo-done", "key": key, "todos": done})
        return {"done": len(done)}

    def _ib_handled(self, key: str, it: dict, note_id=None) -> None:
        """What a session was just given (new or a follow-up) never starts an automatic one later."""
        with self._ib_lock:
            st = self._ib_state["items"].get(key)
            if st is None:
                return
            ev = {"todo:%s" % t["id"] for t in it["todos"]} | {"note:%d" % n["id"] for n in st.get("pending_notes") or []}
            ev |= {"note:%d" % i for i in st.get("new_note_ids") or []}
            if note_id:
                ev.add("note:%d" % int(note_id))
            st["handled"] = sorted(set(st.get("handled") or []) | ev)
            st["pending_notes"] = []

    def inbox_start(self, key: str, mode: str = "work", project: str | None = None, note_id=None,
                    task: str | None = None, session: str | None = None, origin: str = "gui",
                    opts: dict | None = None, agent: str = "claude") -> dict:
        """`opts`: a new session's START_OPTS (the GUI's dialog only; refused with `session`);
        `agent`: the new session's CLI (sessions.AGENTS)."""
        if mode not in ("work", "analyze"):
            raise ValueError("mode must be 'work' or 'analyze'")
        if note_id is not None:
            try:
                note_id = int(note_id)
            except (TypeError, ValueError):
                raise ValueError("note_id must be an integer") from None
            if note_id <= 0:
                raise ValueError("note_id must be positive")
        it = self._ib_item(key)
        issue = session_issue(it)
        if session:
            if opts and any(opts.values()) or agent != "claude":   # they shape a sandbox at its start: that one is running
                raise ValueError("start options apply to a new session only, not to a follow-up")
            s = self.get(str(session))
            if not isinstance(s.issue, dict) or s.issue.get("key") != key:
                raise ValueError("session %s is not linked to %s" % (s.id, key))
            if s.state in RESUMABLE:   # W8: the session that worked on it goes on - resumed, compacted, new task
                if task is not None and not isinstance(task, str):
                    raise ValueError("task must be a string")
                note = note_id or it["target_note"]
                text = task if task and task.strip() else task_for(it, mode, note)
                try:   # 9.12 D: the pointer to the issue's state file
                    text += self.issue_state_hint(self.gui_project(s.project), issue)
                except (KeyError, ValueError, OSError):
                    pass
                r = self.continue_session(s.id, text)
                self._ib_handled(key, it, note)
                self._ib_mark_seen(key)
                self._ib_save()
                self._ib_bump()
                audit({"event": "gui-inbox-resume", "key": key, "session": s.id, "note": note})
                return r
            if s.state not in FOLLOW_STATES:
                raise ValueError("session %s is %s" % (s.id, s.state))
            if not self._istate_at_prompt(s):   # attention without idle = a dialog (permission): Enter would answer it
                raise ValueError("the session is waiting for your answer in the terminal (a dialog) - answer it and try again")
            n = note_id or it["first_new_note"]
            what = "a new comment note_%d" % n if n else "something new"
            if is_gh(it["gl"]):   # W23
                text = ("#%d has %s - read it with the tool %s (repo \"%s\", number %d) and handle it."
                        % (it["iid"], what, "github_pr" if it["type"] == "mr" else "github_issue", gh_entry(it["gl"]), it["iid"]))
            else:
                ref = ("!%d" if it["type"] == "mr" else "#%d") % it["iid"]
                tool = "gitlab_mr" if it["type"] == "mr" else "gitlab_issue"
                text = ("%s has %s - read it with the tool %s (project \"%s\", iid %d) and handle it."
                        % (ref, what, tool, it["gl"], it["iid"]))
            try:   # 9.12 D: the pointer to the issue's state file
                text += self.issue_state_hint(self.gui_project(s.project), issue)
            except (KeyError, ValueError, OSError):
                pass
            if not self.auto_type_ok(s):   # a canned prompt must not take the human's half-written one with it
                raise ValueError("you typed in the session terminal since the last prompt - send or delete it, or click Send now on the card")
            self.type_text(s, text)
            self._ib_handled(key, it, n)
            self._ib_mark_seen(key)
            self._ib_save()
            self._ib_bump()
            audit({"event": "gui-inbox-followup", "key": key, "session": s.id, "note": n})
            return {"sent": True}
        pk = str(project or it["project"] or "")
        if not pk:
            raise ValueError("%s is not mapped to a local project - add the checkout first" % it["gl"])
        p = projects.find(pk, self._ib_pp())
        if p is None:
            raise KeyError("unknown project %r" % pk)
        if is_gh(it["gl"]):   # W23: the session's github_* scope (case-insensitive names)
            if gh_entry(it["gl"]) not in {r.lower() for r in self._gh_projects(p)}:
                raise ValueError("project %s is not mapped to GitHub %s" % (pk, gh_entry(it["gl"])))
        elif it["gl"] not in self._gl_projects(p):   # the session could not read the issue (scope)
            raise ValueError("project %s is not mapped to GitLab %s" % (pk, it["gl"]))
        if task is not None and not isinstance(task, str):
            raise ValueError("task must be a string")
        note_id = note_id or it["target_note"]
        text = task if task and task.strip() else task_for(it, mode, note_id) + self.issue_state_hint(p, issue)
        # scrubbed (session_issue): other Claudes, notifications see it; `!` for a merge request
        title = ("%s%d %s" % ("!" if it.get("type") == "mr" and not is_gh(it["gl"]) else "#", it["iid"], issue["title"] or ""))[:120]
        s = self.create(pk, text, title, origin=origin, issue=issue, agent=agent, **({"opts": opts} if opts else {}))
        self._ib_handled(key, it, note_id)
        self._ib_mark_seen(key)
        self._ib_save()
        self._ib_bump()
        audit({"event": "gui-inbox-start" if origin == "gui" else "inbox-start", "key": key, "session": s.id,
               "mode": mode, "project": pk})
        return s.to_json()

    def inbox_comment(self, key: str, body: str) -> dict:
        gl_path, kind, iid = parse_key(key)
        with self._ib_lock:
            it = self._ib_items.get(key)
        if is_gh(gl_path):   # W23: in the inbox, or a repository of a session's project
            scope = {gl_path} if it is not None else \
                {GH_PREFIX + r.lower() for s in list(self.sessions.values()) if not s.hidden and s.kind == "project"
                 for r in self.gh_scope(s)}
        else:
            scope = {gl_path} if it is not None else \
                {g for s in list(self.sessions.values()) if not s.hidden for g in s.gitlab or []}
        if gl_path not in scope:
            raise ValueError("%s is neither in the inbox nor in a session's %s scope"
                             % (gl_path, "GitHub" if is_gh(gl_path) else "GitLab"))
        body = str(body or "")
        if not body.strip() or len(body) > 20000:
            raise ValueError("body must be 1..20000 characters")
        if is_gh(gl_path):
            gh, repo = self._ib_gh(gl_path)
            r = github.post_note(gh, repo, kind, iid, body, [repo])
        else:
            tok = gitlab.token()
            if not tok:
                raise gitlab.GitLabError(503, "no GitLab token for piperd")
            r = gitlab.post_note(self._ib_gl(tok), gl_path, kind, iid, body, [gl_path])
        now = time.time()
        with self._ib_lock:
            st = self._ib_state["items"].get(key)
            if st is not None:
                st["commented_at"] = now
            else:
                self._ib_state.setdefault("comments", {})[key] = now
            self._ib_save()
        audit({"event": "gui-comment", "key": key, "note_id": r.get("note_id"), "chars": len(body)})
        self._ib_bump()
        url = (it or {}).get("web_url") or ""
        anchor = "#issuecomment-%s" if is_gh(gl_path) else "#note_%s"
        return {"note_id": r.get("note_id"), "web_url": (url + anchor % r.get("note_id")) if url else None}

    def _ib_sent_at(self, key: str) -> float:
        with self._ib_lock:
            st = self._ib_state["items"].get(key) or {}
            return max(float(st.get("commented_at") or 0), float((self._ib_state.get("comments") or {}).get(key) or 0))

    def inbox_rules(self, rules) -> dict:
        if not isinstance(rules, list) or len(rules) > 50:
            raise ValueError("rules must be a list (at most 50)")
        now = time.time()
        with self._ib_lock:
            old = {r.get("id"): r for r in self._ib_rules}
        out, ids = [], set()
        for r in rules:
            if not isinstance(r, dict):
                raise ValueError("a rule must be an object")
            rid = str(r.get("id") or "") or secrets.token_hex(4)
            if not re.match(r"^[A-Za-z0-9_-]{1,40}$", rid) or rid in ids:
                raise ValueError("invalid or duplicate rule id %r" % rid)
            ids.add(rid)
            gl = str(r.get("gl") or "")
            if gl != "*" and not (GH_GL_RX.match(gl) if is_gh(gl) else GL_RX.match(gl)):
                raise ValueError("rule %s: gl must be \"*\", a GitLab project path or gh:owner/repo" % rid)
            on = r.get("on")
            if not isinstance(on, list) or not on or not all(isinstance(x, str) and x in RULE_ON for x in on):
                raise ValueError("rule %s: on must be a non-empty subset of %s" % (rid, ", ".join(RULE_ON)))
            if str(r.get("action") or "analyze") != "analyze":
                raise ValueError("rule %s: the only action is \"analyze\"" % rid)
            enabled = r.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError("rule %s: enabled must be true or false" % rid)
            prev = old.get(rid)
            # `since` never moves back: a rule (re-)enabled now does not fire on older events
            since = float(prev.get("since") or now) if prev and prev.get("enabled") and enabled else now
            want = r.get("since")
            if isinstance(want, (int, float)) and not isinstance(want, bool) and now >= want > since:
                since = float(want)
            out.append({"id": rid, "gl": gl, "on": sorted(set(on), key=RULE_ON.index), "action": "analyze",
                        "enabled": enabled, "since": since})
        with self._ib_lock:
            self._ib_rules = out
            write_json(RULES_FILE, {"rules": out})
        audit({"event": "gui-inbox-rules", "rules": out})
        self._ib_bump()
        return {"rules": [dict(r) for r in out]}

    def inbox_add_project(self, path: str, gl: str) -> dict:
        gl = str(gl or "")
        with self._ib_lock:
            known = {it["gl"] for it in self._ib_items.values()}
        if not (GH_GL_RX.match(gl) if is_gh(gl) else GL_RX.match(gl)) or gl not in known:
            raise ValueError("%r is not a GitLab project / GitHub repository of the inbox" % gl)
        real = os.path.realpath(os.path.expanduser(str(path or "")))
        pp = self._ib_pp()
        disc = {os.path.realpath(p) for _, p in projects.discover(pp, ttl=0)} if pp else set()
        if real not in disc:
            raise ValueError("%s is not a checkout discovered under PROJECT_PATH" % path)
        if is_gh(gl):   # W23: projects.json "github" gets owner/repo
            if (self.origin_gh(real) or "").lower() != gh_entry(gl):
                raise ValueError("the origin of %s is not %s" % (path, gh_entry(gl)))
            key = projects.add_checkout(real, gh_entry(gl), field="github")
        elif self.origin_gl(real) != gl:
            raise ValueError("the origin of %s is not %s" % (path, gl))
        else:
            key = projects.add_checkout(real, gl)
        self._ib_map_cache = None
        audit({"event": "gui-project-add", "key": key, "path": real, "gitlab": gl})
        self._ib_bump()
        return {"key": key}

    def inbox_item(self, key: str) -> dict:
        """Preview for the GUI: metadata and notes, never file content."""
        it = self._ib_item(key)
        if is_gh(it["gl"]):   # W23
            gh, repo = self._ib_gh(it["gl"])
            r = github.item_preview(gh, repo, it["type"], it["iid"], [repo], scrub=False)
        else:
            tok = gitlab.token()
            if not tok:
                raise gitlab.GitLabError(503, "no GitLab token for piperd")
            r = gitlab.item_preview(self._ib_gl(tok), it["gl"], it["type"], it["iid"], [it["gl"]], scrub=False)
        with self._ib_lock:
            st = self._ib_state["items"].get(key) or {}
            new = set(st.get("new_note_ids") or [])
            me = self._ib_status.get("me")
        for n in r["notes"]:
            n["new"], n["mine"] = n["id"] in new, bool(me) and n.get("user") == me
            n.pop("user", None)
        r["key"] = key
        r["state_file"] = None   # 9.12 D: Status + Waiting on of the issue's status file
        if it.get("project"):
            try:
                r["state_file"] = self.issue_state_summary(self.gui_project(it["project"]), it)
            except (KeyError, ValueError, OSError):
                pass
        return r

    # ------------------------------------------------------------ automation (9.8 #9)
    def _inbox_automate(self, note_events: list[tuple[str, dict]]) -> None:
        """Start an "analyze only" session for events that match an enabled GUI-written rule.
        Each (key, event) once; at most PER_HOUR per hour; never with < 2 free session slots;
        only mapped items without a live linked session; never on the baseline poll (caller)."""
        import sessions   # noqa: PLC0415
        with self._ib_lock:
            rules = [r for r in self._ib_rules if r.get("enabled")]
        if not rules:
            return
        notes: dict[str, list] = {}
        for k, n in note_events:
            notes.setdefault(k, []).append({"id": int(n["id"]), "created_at": n.get("created_at")})
        mapped, _ = self._ib_map()
        linked = self._ib_linked()
        with self._ib_lock:
            items = list(self._ib_items.values())
        limit = None   # once the hour / slot limit is hit this poll, every later hit only waits
        for it in sorted(items, key=lambda x: x["key"]):
            key = it["key"]
            with self._ib_lock:
                st = self._ib_state["items"].get(key)
                if st is None:
                    continue
                done = {a.get("event") for a in st.get("auto") or []} | set(st.get("handled") or [])
                waiting = list(st.get("pending_notes") or [])
            # a note event exists only in the poll it appeared in: one that could not fire yet
            # (limit, no slot, a live session, unmapped) waits in pending_notes, a to-do is re-listed
            cand = {n["id"]: n for n in waiting + notes.get(key, [])}
            evs = [("todo:%s" % t["id"], t["action"], _ts(t["created_at"]), t["note_id"]) for t in it["todos"]]
            evs += [("note:%d" % i, "new_note", _ts(n["created_at"]), i) for i, n in sorted(cand.items())]
            hits = [e for e in evs if e[0] not in done and any(
                (r["gl"] == "*" or r["gl"] == it["gl"]) and e[1] in r["on"] and e[2] > float(r["since"]) for r in rules)]
            pend = [cand[e[3]] for e in hits if e[1] == "new_note"][-20:]
            why = None
            if not hits:
                pass
            elif not mapped.get(it["gl"]):
                why = "unmapped"
            elif any(s.state in sessions.ACTIVE for s in linked.get(key, [])):
                why = "live session"
            elif limit:
                why = limit
            elif self._ib_used_hour() >= PER_HOUR:
                why = limit = "per-hour limit"
                audit({"event": "inbox-auto-skipped", "key": key, "why": why})
            elif self.max_sessions() - sum(1 for s in list(self.sessions.values()) if s.state in sessions.ACTIVE) < 2:
                why = limit = "fewer than 2 free session slots"
                audit({"event": "inbox-auto-skipped", "key": key, "why": why})
            if not hits or why:
                if pend != waiting:
                    with self._ib_lock:
                        st["pending_notes"] = pend
                        self._ib_save()
                continue
            note = next((e[3] for e in hits if e[1] == "new_note"), None) or next((e[3] for e in hits if e[3]), None)
            sid, err = None, None
            try:
                sid = self.inbox_start(key, "analyze", note_id=note, origin="inbox")["id"]
            except Exception as e:  # noqa: BLE001 - recorded, never retried in a loop
                err = "%s: %s" % (type(e).__name__, e)
            now = time.time()
            with self._ib_lock:
                st = self._ib_state["items"].get(key)
                if st is not None:
                    st.setdefault("auto", []).extend({"event": e[0], "session": sid, "at": now} for e in hits)
                    st["pending_notes"] = []
                self._ib_save()
            audit({"event": "inbox-auto", "key": key, "events": [e[0] for e in hits], "session": sid, "error": err})
            self._ib_bump()

    # ------------------------------------------------------------ gitlab_inbox (agents)
    def inbox_tool(self, s, args: dict, forge: str = "gitlab") -> dict:
        """Read-only inbox for a session: only items of GitLab projects in its scope
        (forge="github": github_inbox - the GitHub repositories of its scope, W23)."""
        only_new = bool(args.get("only_new"))
        scope = {GH_PREFIX + r.lower() for r in self.gh_scope(s)} if forge == "github" else set(s.gitlab or [])
        out = []
        for v in self._ib_views():
            if v["gl"] not in scope or (only_new and not (v["new"] or v["new_notes"])):
                continue
            row = {k: v[k] for k in ("key", "gl", "type", "iid", "title", "reasons", "new_notes", "project",
                                     "web_url", "updated_at")}
            row["todos"] = [{k: t.get(k) for k in ("action", "author", "body")} for t in v["todos"]]
            out.append(row)
        with self._ib_lock:
            status = (self._ib_gh_status if forge == "github" else self._ib_status)["status"]
        return gitlab._scrub_obj({"status": status, "items": out})

    # ------------------------------------------------------------ comment draft + board flow (9.8 #7 #8)
    def _draft_file(self, s):
        """(root, rel, stat) of the session's comment draft, or None. Only a regular file
        (no symlink anywhere: safe_*), only in development/, never one project_hidden hides."""
        if s.kind != "project" or not isinstance(s.issue, dict):
            return None
        try:
            p = self.gui_project(s.project)
            iid = int(s.issue.get("iid"))
        except (KeyError, TypeError, ValueError):
            return None
        root = os.path.realpath(p["path"])
        hidden = self.project_hidden(p)
        mr = s.issue.get("type") == "mr"
        word = issue_files()["comment"]
        want = issue_file(("github-%s%d" % ("pr-" if mr else "", iid) if is_gh(s.issue.get("gl")) else
                           "gitlab-%s%d" % ("mr-" if mr else "", iid)), "comment")
        if not hidden(want) and not hidden("development"):
            try:
                return root, want, common.safe_stat(root, want)
            except OSError:
                pass
        try:
            names = common.safe_listdir(root, "development")
        except OSError:
            return None
        rx = re.compile(r"(?<!\d)%d(?!\d)" % iid)
        best = None
        for n in names:
            if n.startswith(".") or not n.endswith(".md") or word[:6].lower() not in n.lower() or not rx.search(n):
                continue
            rel = "development/" + n
            if hidden(rel):
                continue
            try:
                st = common.safe_stat(root, rel)
            except OSError:
                continue
            if best is None or st.st_mtime > best[2].st_mtime:
                best = (root, rel, st)
        return best

    def session_draft(self, sid: str) -> dict:
        s = self.get(sid)
        f = self._draft_file(s)
        if f is None:
            return {"path": None}
        root, rel, st = f
        if st.st_size > DRAFT_MAX:
            raise ValueError("%s is larger than 64 KiB" % rel)
        data = common.safe_read(root, rel, DRAFT_MAX + 1)
        if len(data) > DRAFT_MAX:
            raise ValueError("%s is larger than 64 KiB" % rel)
        key = s.issue["key"]
        return {"path": rel, "text": data.decode("utf-8", "replace"), "mtime": st.st_mtime, "key": key,
                "sent": self._ib_sent_at(key) > st.st_mtime}

    def _flow_tools(self, s, tkey) -> dict:
        """analysis / impl / verify from the transcript's tool_use records, cached by (path, mtime)."""
        hit = self._flow_cache.get(s.id)
        if hit and hit[0] == tkey:
            return hit[1]
        out = {"analysis": False, "impl": False, "verify": 0}
        reviewers = 0   # /verify spawns its own reviewer agents: they count only without a /verify
        src = self._transcript_src(s)
        proj = os.path.realpath(s.path) + os.sep
        for rec in (digest._records(*src) if src else ()):
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if rec.get("type") == "user" and isinstance(content, str) and "<command-name>/verify</command-name>" in content:
                out["verify"] += 1
            if rec.get("type") != "assistant" or not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool_use" or not isinstance(c.get("input"), dict):
                    continue
                name, inp = c.get("name"), c["input"]
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
                    if fp.startswith("/"):   # the project is mounted at its host path
                        if not fp.startswith(proj):
                            continue   # scratchpad, ~/.claude, /tmp: not the project
                        fp = fp[len(proj):]
                    rel = os.path.normpath(fp)
                    if rel.startswith(analysis_dirs()):
                        if rel.endswith(".md") and issue_files()["comment"][:6].lower() not in os.path.basename(rel).lower():
                            out["analysis"] = True
                    elif rel and not rel.startswith(".."):
                        out["impl"] = True
                elif name == "Skill":
                    sk = str(inp.get("skill") or inp.get("name") or "")
                    if sk == "verify" or sk.endswith(":verify"):
                        out["verify"] += 1
                elif name in ("Agent", "Task"):
                    sub = str(inp.get("subagent_type") or "")
                    if sub == "independent-reviewer" or sub.endswith(":independent-reviewer"):
                        reviewers += 1
        out["verify"] = out["verify"] or reviewers
        self._flow_cache[s.id] = (tkey, out)
        return out

    def _flow_gitpart(self, s) -> dict:
        hit = self._flow_git.get(s.id)
        if hit and time.time() - hit[0] < FLOW_GIT_TTL:
            return hit[1]
        out = {"draft": None, "commits": 0, "ahead": None, "pushed": False, "code_changes": 0}
        try:
            f = self._draft_file(s)
        except OSError:
            f = None
        if f is not None:
            out["draft"] = {"path": f[1], "mtime": f[2].st_mtime,
                            "sent": self._ib_sent_at(s.issue["key"]) > f[2].st_mtime}
        try:
            top = self._session_top(s)   # the trusted root: never git in a repository planted in the project
            if top is None:
                raise gitops.GitError("no trusted repository")
            email = gitops._git(top, "config", "user.email", ok=(0, 1)).strip()
            since = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(s.created))
            args = ["rev-list", "--count", "--since=" + since]
            if email:
                args += ["--fixed-strings", "--author=" + email]
            # uncommitted changes in the project (a repository's subdirectory, maybe) outside the analysis / draft notes
            proj = os.path.realpath(s.path)
            rels = [os.path.relpath(os.path.join(top, str(f["path"])), proj)
                    for f in gitops.status(top, lambda p: False)["files"]]
            out["code_changes"] = sum(1 for r in rels
                                      if not (r == ".." or r.startswith("../") or r.startswith(analysis_dirs())))
            out["commits"] = int(gitops._git(top, *args, "HEAD").strip() or 0)
            if gitops.upstream(top):
                out["ahead"] = int(gitops._git(top, "rev-list", "--count", "@{upstream}..HEAD").strip() or 0)
                out["pushed"] = out["commits"] > 0 and out["ahead"] == 0
        except (gitops.GitError, ValueError):
            pass
        self._flow_git[s.id] = (time.time(), out)
        return out

    def flow(self, s, tkey=None) -> dict | None:
        """The workflow strip of a board card (project sessions only)."""
        if s.kind != "project":
            return None
        if tkey is None:
            src = self._transcript_src(s)
            path = self._transcript_file(*src) if src else None
            try:
                tkey = (path, os.path.getmtime(path) if path else 0.0)
            except OSError:
                tkey = (path, 0.0)
        out = dict(self._flow_tools(s, tkey), **self._flow_gitpart(s))
        # git is the ground truth for "implemented": Claude also edits through Bash (sed -i, patches)
        if out["commits"] or out.pop("code_changes", 0):
            out["impl"] = True
        out.pop("code_changes", None)
        out["analysis"] = out["analysis"] or out["impl"]   # an implementation has had its analysis
        return out
