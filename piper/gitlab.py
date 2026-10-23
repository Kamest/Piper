"""GitLab served by piperd on the host.

Sandboxes get neither GITLAB_TOKEN nor the GitLab host in network.allow: a token plus
`repository/files` would read every excluded file from the remote (K5), and a PAT cannot
be scoped by path. Instead piperd exposes a fixed set of operations - never a generic
path proxy - each mapped to one API endpoint template:

  read   issues (detail + discussions, list, links, parent/children, time), merge requests
         (detail + discussions with file/line, list, changes with policy-matching files withheld),
         pipelines and their jobs, the tail of a FAILED job's log (scrubbed), an issue/MR upload
         (a screenshot) - piperd stores it in the session's drop dir
  write  notes, thread replies and resolving; issues (create, edit, close, link, parent, time)
         and merge requests (create, edit) - each only after a human confirms it in the GUI

Denied by construction (no operation reaches them): repository/files, blobs, archive,
raw, snippets, logs of jobs that did not fail. Every operation is confined to the GitLab projects mapped to
the calling session in projects.json; the advisor may use every mapped project. A read
tool may name another project: piperd asks the user on a card once per project and
session, or reads it without one when policy gitlab_read_always lists it (sessions.py
_gitlab_read_extra). A write may go to the mapping or to such an allowed project (each note
still on its own card); the inbox never leaves the mapping.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import mimetypes
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from common import GITLAB_TOKEN_FILE, MYSQL_LINE, SCRUB, no_bidi

TIMEOUT = 20
MAX_PER_PAGE = 50
# a write tool -> the kind of its card (Settings -> approvals can pre-approve a kind)
WRITE_TOOLS = {"gitlab_comment": "gitlab_comment", "gitlab_resolve": "gitlab_mr_write",
               "gitlab_upload_file": "gitlab_upload_file",
               "gitlab_issue_create": "gitlab_issue_write", "gitlab_issue_update": "gitlab_issue_write",
               "gitlab_issue_link": "gitlab_issue_write",
               "gitlab_mr_create": "gitlab_mr_write", "gitlab_mr_update": "gitlab_mr_write",
               "gitlab_mr_merge": "gitlab_mr_merge", "gitlab_job_play": "gitlab_job_play"}
# tools that run only after a card (plan -> card -> execute): the writes, and a failed job's log -
# a job can print anything (configuration, environment), the scrub only catches what looks secret (K5)
CARD_TOOLS = dict(WRITE_TOOLS, gitlab_job_log="gitlab_job_log")
UPLOAD_MAX = 10 << 20
TEXT_SCRUB_MAX = 4 << 20   # a text upload is scrubbed: ~1 s per hostile MiB at worst
TRACE_KEEP = 2 << 20   # bytes of a job trace kept while it streams (the endpoint ignores Range)
TRACE_DEADLINE = 60    # s for streaming a trace - within the MCP's wait after a card
LOG_LINES, LOG_MAX_LINES, LOG_MAX_BYTES = 200, 500, 64 << 10

TOOLS = {
    "gitlab_issue": "Issue detail with all notes (the notes carry as much of the requirement as the description).",
    "gitlab_issues": "List issues of a project (state, search, labels, milestone, page).",
    "gitlab_milestones": "Milestones of a project and its groups (title, description, state, dates).",
    "gitlab_mr": "Merge request detail with all notes.",
    "gitlab_mrs": "List merge requests of a project (state, search).",
    "gitlab_mr_changes": "Changed files of a merge request; files matching the isolation policy are withheld.",
    "gitlab_pipelines": "Recent pipelines of a project.",
    "gitlab_pipeline_jobs": "Jobs of a pipeline (name, stage, status, failure reason).",
    "gitlab_job_log": "The scrubbed tail of a failed job's log. Needs the user's confirmation.",
    "gitlab_upload": "An upload (screenshot, attachment) of an issue / MR; piperd stores it for the session.",
    "gitlab_upload_file": "Upload a visible local file to GitLab and return its Markdown link. Needs the user's confirmation.",
    "gitlab_comment": "Post a note on an issue or MR, a reply in a thread, or with note_id edit an own note. Needs the user's confirmation.",
    "gitlab_resolve": "Resolve / unresolve an MR thread. Needs the user's confirmation.",
    "gitlab_issue_create": "Create an issue or task. Needs the user's confirmation.",
    "gitlab_issue_update": "Edit an issue (fields, state, parent, time). Needs the user's confirmation.",
    "gitlab_issue_link": "Link two issues. Needs the user's confirmation.",
    "gitlab_mr_create": "Open a merge request. Needs the user's confirmation.",
    "gitlab_mr_update": "Edit a merge request (fields, draft, state). Needs the user's confirmation.",
    "gitlab_mr_merge": "Merge a merge request. Needs the user's confirmation.",
    "gitlab_job_play": "Start a manual / delayed CI job, or retry a failed / canceled one. Needs the user's confirmation.",
}
class GitLabError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def token() -> str | None:
    try:
        with open(GITLAB_TOKEN_FILE) as f:
            t = f.read().strip()
            if t:
                return t
    except FileNotFoundError:
        pass
    return os.environ.get("GITLAB_TOKEN") or os.environ.get("GITLAB_PAT")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an error: urllib would send PRIVATE-TOKEN along to any host (object storage)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class GitLab:
    def __init__(self, host: str, tok: str | None, opener=None):
        self.base = "https://%s/api/v4" % host
        self.tok = tok
        self.opener = opener or _OPENER.open

    def _req(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
             upload: tuple[str, bytes] | None = None):
        raw, _ = self._open(method, self.base + path, query, body, upload=upload)
        try:
            return json.loads(raw.decode() or "null")
        except ValueError:   # a non-JSON answer (a proxy's error page)
            raise GitLabError(502, "GitLab %s %s: not JSON" % (method, path)) from None

    def _open(self, method: str, url: str, query: dict | None = None, body: dict | None = None,
              limit: int = 0, tail: int = 0, upload: tuple[str, bytes] | None = None) -> tuple[bytes, str]:
        """(body, content type) of one request; limit > 0 reads at most limit + 1 bytes, tail > 0
        streams the whole body and keeps only its last `tail` bytes."""
        if not self.tok:
            raise GitLabError(503, "no GitLab token for piperd ($PIPER_HOME/gitlab-token or GITLAB_TOKEN)")
        path = urllib.parse.urlsplit(url).path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        content_type = "application/json"
        if upload is not None:
            name, raw = upload
            boundary = "piper-" + secrets.token_hex(24)
            escaped = name.replace("\\", "\\\\").replace('"', '\\"')
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            data = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                     'Content-Type: %s\r\n\r\n' % (boundary, escaped, mime)).encode() + raw +
                    ('\r\n--%s--\r\n' % boundary).encode())
            content_type = "multipart/form-data; boundary=" + boundary
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"PRIVATE-TOKEN": self.tok, "Accept": "application/json",
                                              "Content-Type": content_type})
        try:
            with self.opener(req, timeout=TIMEOUT) as r:
                ctype = str((getattr(r, "headers", None) or {}).get("Content-Type") or "")
                if tail:
                    buf, deadline = bytearray(), time.monotonic() + TRACE_DEADLINE
                    while True:
                        if time.monotonic() > deadline:
                            raise GitLabError(504, "GitLab %s %s: still streaming after %d s" % (method, path, TRACE_DEADLINE))
                        chunk = (getattr(r, "read1", None) or r.read)(1 << 16)   # what has arrived, not a full 64 KiB
                        if not chunk:
                            return bytes(buf[-tail:]), ctype
                        buf += chunk
                        if len(buf) > 2 * tail:
                            del buf[:-tail]
                return (r.read(limit + 1) if limit else r.read()), ctype
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise GitLabError(502, "GitLab %s %s: redirected (HTTP %d) - not followed, the token would go along"
                                  % (method, path, e.code)) from None
            raise GitLabError(e.code, "GitLab %s %s: HTTP %d" % (method, path, e.code)) from None
        except urllib.error.URLError as e:
            raise GitLabError(502, "GitLab unreachable: %s" % e.reason) from None
        except OSError as e:   # a read timeout, a reset
            raise GitLabError(502, "GitLab %s %s: %s" % (method, path, type(e).__name__)) from None

    def graphql(self, query: str, variables: dict) -> dict:
        raw, _ = self._open("POST", self.base[:-len("/v4")] + "/graphql", None, {"query": query, "variables": variables})
        try:
            d = _dict(json.loads(raw.decode() or "null"), "graphql")
        except ValueError:   # a proxy's or maintenance page
            raise GitLabError(502, "GitLab GraphQL: not JSON") from None
        if d.get("errors"):
            raise GitLabError(502, "GitLab GraphQL: %s" % "; ".join(str(e.get("message")) for e in d["errors"]
                                                                    if isinstance(e, dict))[:300])
        return d.get("data") or {}

    @staticmethod
    def _pid(project: str) -> str:
        return urllib.parse.quote(project, safe="")


def _list(rows, path: str) -> list:
    """A list endpoint's answer, its non-object rows dropped; another shape is an error."""
    if not isinstance(rows, list):
        raise GitLabError(502, "GitLab %s: unexpected answer" % path)
    return [r for r in rows if isinstance(r, dict)]


def _dict(d, path: str) -> dict:
    if not isinstance(d, dict):
        raise GitLabError(502, "GitLab %s: unexpected answer" % path)
    return d


def _whole(v, name: str) -> int:
    """A tool's numeric argument: a clear error for the agent, never Python's "invalid literal for int()" (N3)."""
    try:
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise TypeError
        return int(v)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("%s must be a whole number, not %s" % (name, json.dumps(v)[:40])) from None


def _iid(v) -> int:
    i = _whole(v, "iid")
    if i <= 0:
        raise ValueError("iid must be positive")
    return i


def _scrub_obj(o):
    if isinstance(o, str):
        return scrub_strict(o)
    if isinstance(o, list):
        return [_scrub_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: _scrub_obj(v) for k, v in o.items()}
    return o


def _pick(d: dict, keys) -> dict:
    return {k: d.get(k) for k in keys if k in d}


ISSUE_KEYS = ("iid", "title", "state", "description", "labels", "milestone", "assignees", "author",
              "created_at", "updated_at", "web_url", "due_date", "issue_type", "time_stats")
MR_KEYS = ("iid", "title", "state", "description", "labels", "source_branch", "target_branch",
           "author", "assignees", "reviewers", "created_at", "updated_at", "web_url", "merge_status", "draft")
NOTE_KEYS = ("id", "body", "author", "created_at", "system")
ISSUE_ROW_KEYS = ("iid", "title", "state", "labels", "milestone", "updated_at", "web_url")
MILESTONE_KEYS = ("id", "iid", "title", "description", "state", "start_date", "due_date", "web_url")


def _people(o):
    if isinstance(o, dict) and "username" in o:   # a display name is the author's own text: no bidi tricks (E25)
        return no_bidi(str(o.get("name") or o["username"]))
    if isinstance(o, list):
        return [_people(x) for x in o]
    return o


def _shape(d: dict, keys) -> dict:
    out = _pick(d, keys)
    for k in ("author", "assignees", "reviewers"):
        if k in out:
            out[k] = _people(out[k])
    if isinstance(out.get("milestone"), dict):
        out["milestone"] = out["milestone"].get("title")
    for k in ("title", "milestone"):   # titles are the author's own text: no bidi tricks (E25)
        if isinstance(out.get(k), str):
            out[k] = no_bidi(out[k])
    if isinstance(out.get("labels"), list):
        out["labels"] = [no_bidi(x) if isinstance(x, str) else x for x in out["labels"]]
    return out


_ME: dict[str, str] = {}   # token -> username of its owner


def _me(gl: GitLab) -> str | None:
    if not _ME.get(gl.tok):   # an empty answer is not cached: the next poll asks again
        try:
            _ME[gl.tok] = _dict(gl._req("GET", "/user") or {}, "/user").get("username") or ""
        except GitLabError:
            return None
    return _ME[gl.tok] or None


def _mine_first(gl: GitLab, pid: str, q: dict, rows: list, triage: bool = True) -> list:
    """Issue rows with `mine` = ["assigned"] / ["mentioned"] for the token's owner, those first.
    Assigned issues beyond the page are fetched too (triage only: an enumeration - a milestone, an
    explicit page - keeps the page as it is, or the next page would repeat them); a mention counts in
    the description or as a pending to-do (a comment). Extra lookups failing only drops the flags."""
    me = _me(gl)
    if not me:
        return [_shape(r, ISSUE_ROW_KEYS) for r in rows]
    seen = {r.get("iid") for r in rows}
    if triage:
        try:
            more = _list(gl._req("GET", "/projects/%s/issues" % pid, dict(q, assignee_username=me)), "issues")
            rows = rows + [r for r in more if r.get("iid") not in seen]
        except GitLabError:
            pass
    todo = set()
    try:
        numeric = next((r["project_id"] for r in rows if r.get("project_id")), None)
        for t in _list((gl._req("GET", "/todos", {"project_id": numeric, "type": "Issue", "state": "pending",
                                                   "per_page": 100}) if numeric else None) or [], "/todos"):
            if t.get("action_name") in ("mentioned", "directly_addressed"):
                todo.add((t.get("target") or {}).get("iid"))
    except GitLabError:
        pass
    at = re.compile(r"(?<![\w@])@%s(?![\w-]|\.\w)" % re.escape(me), re.I)
    out = []
    for r in rows:
        row = _shape(r, ISSUE_ROW_KEYS)
        mine = []
        if any(isinstance(a, dict) and a.get("username") == me for a in r.get("assignees") or []):
            mine.append("assigned")
        if r.get("iid") in todo or at.search(r.get("description") or ""):
            mine.append("mentioned")
        if mine:
            row["mine"] = mine
        out.append(row)
    out.sort(key=lambda x: 0 if x.get("mine") else 1)   # stable: GitLab's order within each group
    return out


UPLOAD_RX = re.compile(r"/uploads/([0-9a-f]{32})/([^\s)\]\"'<>?#]+)")
BRANCH_RX = re.compile(r"^(?!-)(?!.*\.\.)[^\s~^:?*\[\\\x00-\x1f]{1,255}$")
DURATION_RX = re.compile(r"^(\d+(mo|w|d|h|m) ?)+$")
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOG_TS_RX = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z \d\d[OE]\+? ?")   # GitLab >= 17 timestamps every line
ANSI_RX = re.compile(r"\x1b\[[0-9;?]{0,32}[A-Za-z]|section_(start|end):\d{1,20}:[\w.-]{1,256}(\[[^\]\n]{0,256}\])?")
HIERARCHY_Q = """query($p: ID!, $iid: String!) { project(fullPath: $p) { workItems(iid: $iid) { nodes {
  widgets { ... on WorkItemWidgetHierarchy {
    parent { iid title state webUrl workItemType { name } }
    children { nodes { iid title state webUrl workItemType { name } } } } } } } } }"""
SET_PARENT_Q = """mutation($id: WorkItemID!, $parent: WorkItemID) {
  workItemUpdate(input: {id: $id, hierarchyWidget: {parentId: $parent}}) { errors } }"""


ONE_LINE_BAD = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def _one_line(v: str, key: str) -> str:
    """A value that is one row of a card: a line break could push the next rows (state, confidential)
    below the card's visible part."""
    if ONE_LINE_BAD.search(v):
        raise GitLabError(400, "%s must be one line, without control characters" % key)
    return v


def _text(args: dict, key: str, cap: int, required: bool = False) -> str | None:
    v = args.get(key)
    if v is None:
        if required:
            raise GitLabError(400, "%s is required" % key)
        return None
    v = str(v)
    if key == "title":
        _one_line(v, key)
    if (required and not v.strip()) or len(v) > cap:
        raise GitLabError(400, "%s must be %d..%d characters" % (key, 1 if required else 0, cap))
    return v.strip() if key == "title" else v


def _labels(v) -> str | None:
    """A list or a comma-separated string -> GitLab's comma-separated form."""
    if v is None:
        return None
    items = v if isinstance(v, list) else str(v).split(",")
    items = [_one_line(str(x).strip(), "a label") for x in items]
    if any(len(x) > 255 or "," in x for x in items):
        raise GitLabError(400, "a label must be at most 255 characters, without a comma")
    return ",".join(x for x in items if x)


def _user_ids(gl: GitLab, names) -> list[int] | None:
    """Usernames (with or without @) -> user ids; [] clears."""
    if names is None:
        return None
    if isinstance(names, str):
        names = [x for x in re.split(r"[,\s]+", names) if x]
    out = []
    if len(names) > 20:
        raise GitLabError(400, "at most 20 people")
    for n in names:
        n = _one_line(str(n).lstrip("@"), "a username")
        rows = _list(gl._req("GET", "/users", {"username": n}) or [], "users")
        if not rows:
            raise GitLabError(400, "unknown GitLab user %r" % n)
        out.append(rows[0]["id"])
    return out


def _milestone_id(gl: GitLab, pid: str, title) -> int | None:
    """A milestone title of the project or its groups -> its id; "" removes it (0)."""
    if title is None:
        return None
    if not str(title).strip():
        return 0
    _one_line(str(title), "milestone")
    rows = _list(gl._req("GET", "/projects/%s/milestones" % pid,
                         {"title": str(title).strip(), "include_ancestors": "true"}) or [], "milestones")
    if not rows:
        raise GitLabError(400, "no milestone %r in this project or its groups" % title)
    return rows[0]["id"]


def _date(v) -> str | None:
    if v is None:
        return None
    if v and not DATE_RX.match(str(v)):
        raise GitLabError(400, "a date is YYYY-MM-DD (or \"\" to clear it)")
    return str(v)


def _state_event(v) -> str | None:
    if v in (None, ""):
        return None
    if v not in ("close", "reopen"):
        raise GitLabError(400, "state must be 'close' or 'reopen'")
    return v


def _branch(v, key: str) -> str:
    v = str(v or "")
    if not BRANCH_RX.match(v):
        raise GitLabError(400, "%s is not a branch name" % key)
    return v


def _bool(args: dict, key: str) -> bool | None:
    """A flag the agent sent: true/false or "true"/"false" (a string "false" must not mean True)."""
    if key not in args or args[key] is None:
        return None
    v = args[key]
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    if not isinstance(v, bool):
        raise GitLabError(400, "%s must be true or false" % key)
    return v


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _did(v) -> str:
    did = str(v or "")
    if not re.match(r"^[0-9a-f]{8,64}$", did):
        raise GitLabError(400, "discussion_id is the hex id from the notes")
    return did


def _thread(gl: GitLab, pid: str, coll: str, iid: int) -> tuple[list[dict], bool]:
    """The non-system notes of an issue / MR in thread order: each with its discussion_id (reply /
    resolve with it), and for an MR review comment the file and line it is on and whether it is resolved.
    True when there were more threads than the pages read (the newest are missing)."""
    out = []
    threads = _pages(gl, "/projects/%s/%s/%d/discussions" % (pid, coll, iid), {}, NOTE_PAGES, until_empty=True)
    for d in threads:
        for i, n in enumerate(d.get("notes") or []):
            if not isinstance(n, dict) or n.get("system"):
                continue
            row = _shape(n, NOTE_KEYS)
            row.pop("system", None)
            row["discussion_id"] = d.get("id")
            if i:
                row["reply"] = True
            pos = n.get("position")
            if isinstance(pos, dict):
                row["position"] = {"path": pos.get("new_path") or pos.get("old_path"),
                                   "new_line": pos.get("new_line"), "old_line": pos.get("old_line")}
            if n.get("resolvable"):
                row["resolved"] = bool(n.get("resolved"))
            out.append(row)
    return out, len(threads) >= NOTE_PAGES * INBOX_PER_PAGE


def _uploads(*texts) -> list[str]:
    seen: list[str] = []
    for t in texts:
        for m in UPLOAD_RX.finditer(str(t or "")):
            u = "/uploads/%s/%s" % m.groups()
            if u not in seen:
                seen.append(u)
    return seen


def _gid(issue_id) -> str:
    return "gid://gitlab/WorkItem/%d" % int(issue_id)


def _hierarchy(gl: GitLab, project: str, iid: int) -> dict:
    nodes = (((gl.graphql(HIERARCHY_Q, {"p": project, "iid": str(iid)}).get("project") or {})
              .get("workItems") or {}).get("nodes") or [])
    out: dict = {}
    for w in (nodes[0].get("widgets") or []) if nodes else []:
        if not isinstance(w, dict) or ("parent" not in w and "children" not in w):
            continue
        row = lambda x: {"iid": x.get("iid"), "title": no_bidi(str(x.get("title") or "")), "state": x.get("state"),  # noqa: E731
                         "type": (x.get("workItemType") or {}).get("name"), "web_url": x.get("webUrl")}
        if w.get("parent"):
            out["parent"] = row(w["parent"])
        kids = [row(x) for x in ((w.get("children") or {}).get("nodes") or []) if isinstance(x, dict)]
        if kids:
            out["children"] = kids
    return out


# Scrubbing GitLab output for the sandbox. common.scrub is quadratic on one
# long line - `\b[\w.-]*` restarts at every word boundary: a minute on 64 KiB of `a-a-a`, and re holds
# the GIL, so all of piperd stops. Here every rule runs over the ORIGINAL text and only reports spans to
# hide; the spans are merged and replaced once - so no rule hides a value from another one (the order of
# common.scrub's rules matters, a union of spans does not), and the result hides at least what each rule
# alone would. A NAME=value pair is found by one pass over the name runs, every other rule starts at a
# rare literal; all linear in the text.
# scrub_strict: what common.scrub catches (issue texts, notes, diffs). scrub_text, for logs and text
# uploads, more: any NAME with pass/secret/token/key/credential/auth, `--password x`, `docker login -p x`,
# `mysql -px`, `curl -u u:p`, JSON keys like that, private key blocks, long base64 lines. Over-scrubbing
# a log is fine.
NAME_RX = re.compile(r"[\w.-]+")
SEP_CHARS = " \t=:>\"'\\"
LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
WS_RX = re.compile(r"\s")
# strict: the word as common.scrub takes it - the whole name or a part between _ . - ("db.password",
# not "Passwords:" or "Secretary:" in an issue's prose)
STRICT_WORDS = re.compile(r"(?i)(?:^|[_.-])(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
                          r"|private[_-]?key)(?:$|[_.-])")
BROAD_WORDS = re.compile(r"(?i)pass|pwd|secret|token|key|credential|auth|cookie|session")
# (pattern): group 1 is kept, the rest of the match is hidden; no group: the whole match is hidden
COMMON_RULES = [   # common.scrub's own two, exactly
    re.compile(r"(?i)\b(authorization\s*:\s*)\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
]
# and, for the union only, the authorization rule that never eats the next header: common.scrub reaches
# a second header when its first rule already took the first one (`x-token: Authorization: a\n
# Authorization: Basic x`); a union of both readings hides what either would
UNION_RULES = COMMON_RULES + [
    re.compile(r"(?i)\b(authorization\s*:\s*)(?!authorization\b)\S+(?:\s+(?!authorization\b)\S+)?"),
]
LOG_RULES = [
    re.compile(r"(?i)(\b(?:login|sshpass)\b[^\n]{0,60}?[ \t]-p[ \t]{1,8})(?!-)\S+"),    # docker login -p x
    re.compile(r"(?i)(\bmysql(?:dump|admin)?\b[^\n]{0,60}?[ \t]-p)(?=[^\s-])\S+"),      # mysql -psecret
    re.compile(r"(?i)([ \t]-u[ \t]{1,8}[^\s:]{1,128}:)\S+"),                             # curl -u user:pass
    re.compile(r"^[ \t]*[A-Za-z0-9+/]{60,}={0,2}[ \t]*$", re.M),                          # env | base64
]


def _closing(text: str, k: int, quote: str, stop: int) -> int:
    """Index of the quote that closes the string opened just before k (an even run of \\ before it), or -1."""
    q = k
    while True:
        q = text.find(quote, q, stop)
        if q == -1:
            return -1
        b = 0
        while b < 64 and q - 1 - b >= k and text[q - 1 - b] == "\\":
            b += 1
        if b % 2 == 0:
            return q
        q += 1


def _value_end(text: str, k: int, run_end: int) -> int:
    """End of the value at k: at least the whitespace-free run (common.scrub's \\S+), further when a
    quoted string (within 4 KiB, on the line, \\\\-escapes skipped; also an escaped \\\\"x\\\\") closes later."""
    stop = min(len(text), k + 4097)
    for quote in ('\\"', "\\'", '"', "'"):
        if text.startswith(quote, k):
            q = _closing(text, k + len(quote), quote, stop) if len(quote) == 1 else text.find(quote, k + 2, stop)
            if q != -1 and "\n" not in text[k:q] and q + len(quote) > run_end:
                # past the quote up to whitespace again: a name inside the string has common.scrub's \S+ value
                # running on after it (`token=" token=r"SECRET`)
                w = WS_RX.search(text, q + len(quote))
                return w.start() if w else len(text)
            break
    return run_end


def _secret_spans(text: str, words: re.Pattern, options: bool) -> list:
    """Spans of the values of NAME = x / NAME: x / "NAME": x (and with `options` --NAME x) where NAME
    holds a secret word; whitespace around the separator may hold newlines (as common.scrub's \\s*).
    A name inside a value already hidden is skipped - except one followed only by separator characters
    up to the value's end, whose own value comes after it; the end of a run is looked up once - linear."""
    spans, last, n = [], 0, len(text)
    run_from = run_end = -1
    for m in NAME_RX.finditer(text):
        i, j = m.start(), m.end()
        # inside a hidden value: only a name whose separator closes the value (`x:token: y`, `a:x--password = y`)
        if (i < last and (last - j > 8 or text[j:last].strip(SEP_CHARS))) or not words.search(m.group(0)):
            continue
        if j < n and text[j] in "\"'" and i and text[i - 1] == text[j]:
            j += 1   # a JSON / dict key
        elif text.startswith('\\"', j) and text[max(0, i - 2):i] == '\\"':
            j += 2   # a key of a JSON encoded in a string: {\"password\":\"x\"}
        k = j
        while k < n and text[k].isspace():
            k += 1
        if k < n and text[k] in "=:":
            sep = text[k]
            k += 1
            if k < n and text[k] in "=>" and text[k] != sep:   # := and =>
                k += 1
            while k < n and text[k].isspace():
                k += 1
        elif not (options and m.group(0).startswith("-") and k > j and k < n and text[k] != "-"
                  and not any(c in LINE_BREAKS for c in text[j:k]) and (not i or text[i - 1].isspace())):
            continue
        if k >= n:
            continue
        if not run_from <= k < run_end:
            w = WS_RX.search(text, k)
            run_from, run_end = k, (w.start() if w else n)
        end = _value_end(text, k, run_end)
        if end > k:
            spans.append((k, end))
            last = max(last, end)
    return spans


def _url_spans(text: str, cap: int = 0) -> list:
    """scheme://user:PASS@host -> the PASS span; PASS may hold "/" and ":" (as common.scrub's [^@\\s]+); the
    user may be empty (redis://:PASS@). cap: common.scrub's bounds (user <= cap, PASS 1..cap) - without it
    any length (stricter). The next whitespace and the next "@" are looked up once and reused until passed - linear."""
    spans, i, n = [], 0, len(text)
    ws_end = at = -1
    while True:
        p = text.find("://", i)
        if p == -1:
            break
        i = e = p + 3
        while e < n and text[e] not in "/:@" and not text[e].isspace():
            e += 1
        if e >= n or text[e] != ":" or (cap and e - i > cap):
            continue
        if ws_end <= e:
            w = WS_RX.search(text, e)
            ws_end = w.start() if w else n
        if at <= e:
            at = text.find("@", e)
            at = n + 1 if at == -1 else at
        if e + 1 < at < ws_end and not (cap and at - e - 1 > cap):
            spans.append((e + 1, at))
            i = at
    return spans


def _rule_spans(text: str, rules) -> list:
    return [(m.end(1) if rx.groups else m.start(), m.end()) for rx in rules for m in rx.finditer(text)
            if m.end() > (m.end(1) if rx.groups else m.start())]


def _hide(text: str, spans: list) -> str:
    """Replace the union of the spans by [scrubbed] (a quoted span keeps its quotes)."""
    out, pos, merged = [], 0, []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in merged:
        seg = text[s:e]
        q = next((x for x in ('\\"', '"', "'") if len(seg) >= 2 * len(x) and seg.startswith(x) and seg.endswith(x)), "")
        out.append(text[pos:s] + q + "[scrubbed]" + q)
        pos = e
    out.append(text[pos:])
    return "".join(out)


KEY_BODY_RX = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}|[A-Za-z0-9+/]{1,15}={1,2}|(?:Proc-Type|DEK-Info):.{0,200}")


def _private_keys(text: str) -> str:
    """Private key blocks out - str.find from left to right (the next END is looked up once and reused).
    A key is < 16 KiB: a BEGIN without an END that close takes only the body right after it - a glued
    run, then base64 / Proc-Type / DEK-Info / blank lines - so an error message quoting the header keeps
    the rest of the log."""
    out, i, n, end = [], 0, len(text), -1
    while True:
        b = text.find("-----BEGIN ", i)
        if b == -1:
            break
        h = text.find("-----", b + 11, b + 80)
        if h == -1 or "PRIVATE KEY" not in text[b:h]:
            out.append(text[i:b + 11])
            i = b + 11
            continue
        out.append(text[i:b] + "[scrubbed: private key]")
        if end != -2 and end < h:
            end = text.find("-----END ", h + 5)
            end = -2 if end == -1 else end   # -2: no END anywhere further
        if end >= 0 and end - h < 16384:
            t = text.find("-----", end + 9, end + 80)
            i = t + 5 if t != -1 else end + 9
            continue
        i = h + 5
        if i < n and not text[i].isspace():   # a body glued to the header (a one-line PEM)
            w = WS_RX.search(text, i)
            i = w.start() if w else n
        if text.startswith("\n", i) or text.startswith("\r\n", i):
            j = text.index("\n", i) + 1
            while j < n:   # each line looked at once: strip, then a full match of one short pattern
                nl = text.find("\n", j)
                stop = n if nl == -1 else nl + 1
                body = text[j:stop].strip()
                if body and not KEY_BODY_RX.fullmatch(body):
                    break
                j = stop
            out.append("\n")
            i = j
    out.append(text[i:])
    return "".join(out)


CONTROL_BYTES = bytes(c for c in range(32) if c not in (9, 10, 12, 13, 27)) + b"\x7f"   # ESC: ANSI colours
HIGH_BYTES = bytes(range(128, 256))
# what a real UTF-16/32 text holds; text misread as UTF-16 (UTF-8 bytes in pairs) lands in CJK and fails
# (ASCII misread in pairs: the HIGH byte of each unit decides - a letter or digit there lands at U+30xx and up,
# CJK, never here; so no two neighbouring ASCII alphanumerics survive a misread, and every secret the scrub
# catches has them)
TEXT_CHARS_RX = re.compile(r"[\t\n\f\r\x1b\x20-\x7e -ͯͰ-ϿЀ-ԯ -⯿﻿�"
                           "\U0001f000-\U0001faff]*")
UTF16_BOMS = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe", "utf-16-le"),
              (b"\xfe\xff", "utf-16-be"))


def _upload_text(raw: bytes) -> tuple[str, str | None]:
    """(kind, text) of an upload by its CONTENT - never by the declared type, never by a sample:
    "image" (png/jpeg/gif/webp) or "pdf" by their magic bytes - handed over as they are (text: None);
    a file without NULs must have <= 1 % control bytes (ESC excepted), then:
    "utf8" - strict UTF-8 (an optional BOM; a last character cut off allowed), unambiguous;
    "bytes" - other 8-bit text (cp1250 & co.): >= 32 bytes, <= 30 % bytes over 127, a line break per 400 bytes,
    read as latin-1 - every ASCII character stays itself and nothing reads back as something else;
    "utf16" - UTF-16/32 by a BOM or by NULs at one byte parity, decoded strictly as a whole, and only when
    every character is one a text in those holds (TEXT_CHARS_RX);
    "binary" - anything else (archives, DER, compressed data, MessagePack, CJK in UTF-16): refused (text: None)."""
    if raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")) or (
            raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        return "image", None
    if raw.startswith(b"%PDF-"):
        return "pdf", None
    if b"\x00" not in raw:
        n = len(raw)
        if n - len(raw.translate(None, CONTROL_BYTES)) > n // 100:   # TLV / protobuf: tags and lengths
            return "binary", None
        try:
            return "utf8", raw.decode("utf-8").lstrip("﻿")
        except UnicodeDecodeError as e:
            if e.start >= n - 3 and e.end == n and e.reason == "unexpected end of data":   # a log cut inside its last character
                return "utf8", raw[:e.start].decode("utf-8").lstrip("﻿")
        # 8-bit text has lines; MessagePack / CBOR (length bytes over 127, no separators) do not
        if n >= 32 and n - len(raw.translate(None, HIGH_BYTES)) <= n * 3 // 10 and raw.count(b"\n") >= max(1, n // 400):
            return "bytes", raw.decode("latin-1")
        return "binary", None
    enc = next((e for bom, e in UTF16_BOMS if raw.startswith(bom)), None)
    body = raw[len(next(bom for bom, e in UTF16_BOMS if e == enc)):] if enc else raw
    if not enc:
        even, odd = raw[0::2].count(0), raw[1::2].count(0)
        if max(even, odd) >= 0.9 * (even + odd):
            enc = "utf-16-be" if even > odd else "utf-16-le"
    if enc:
        try:
            text = body.decode(enc)
        except UnicodeDecodeError:
            return "binary", None
        if TEXT_CHARS_RX.fullmatch(text):
            return "utf16", text
    return "binary", None


COMMON_WORD_RX = re.compile(r"(?i)(?:(?<=[_.-])|^)(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key"
                            r"|private[_-]?key)(?=[_.-]|$)")
_BOUNDARY = re.compile(r"\b")
COMMON_AUTH_RX = re.compile(r"(?i)\b(authorization)(\s*:\s*)\S+(\s+\S+)?")
COMMON_BEARER_RX = re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]+")


def _render(text: str, spans: list) -> tuple[str, list]:
    """text with each span (overlapping ones merged, touching ones not - as successive sub() calls leave
    them) replaced by [scrubbed]; also [rendered start, orig start, orig end] per placeholder."""
    merged: list = []
    for s1, e1 in sorted(spans):
        if merged and s1 < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e1)
        else:
            merged.append([s1, e1])
    out, pos, marks, shift = [], 0, [], 0
    for s1, e1 in merged:
        out.append(text[pos:s1] + "[scrubbed]")
        marks.append([s1 + shift, s1, e1])
        shift += 10 - (e1 - s1)
        pos = e1
    out.append(text[pos:])
    return "".join(out), marks


def _orig_pos(marks: list, starts: list, p: int, end: bool) -> int:
    """Position p of a rendered text in the original; inside a placeholder: where what it replaced starts
    (end=False) or ends (end=True, exclusive). end=True takes p as the last character of a span."""
    i = bisect.bisect_right(starts, p) - 1
    if i >= 0 and p < marks[i][0] + 10:
        return marks[i][2] if end else marks[i][1]
    return p + ((marks[i][2] - marks[i][0] - 10) if i >= 0 else 0) + (1 if end else 0)


def _to_orig(marks: list, starts: list, a: int, b: int) -> tuple[int, int]:
    return _orig_pos(marks, starts, a, False), _orig_pos(marks, starts, b - 1, True)


def _common_spans(text: str) -> list:
    """What common.scrub hides, as spans of `text` - its exact behaviour in linear time (its first pattern
    backtracks quadratically).

    The NAME=value rule `\\b((?:[\\w.-]*[_.-])?WORD(?:[_.-][\\w.-]*)?)(\\s*[=:]\\s*)\\S+`: a name is all of a
    [\\w.-] run up to its end (any other end is followed by a run character, never by \\s or [=:]); leftmost-
    first it starts at the run's first word boundary, so a run matches when WORD stands at that boundary or
    after one of _ . - within the run, and ends the run or is followed by one of them; then whitespace, = or
    :, whitespace, and the value is the next \\S run; sub() goes on after it. common.scrub's other three rules
    run, as there, one after another over the text the previous ones left - their matches are mapped back."""
    spans, pos, n = [], 0, len(text)
    for m in NAME_RX.finditer(text):
        s0, r = m.span()
        if s0 < pos:
            continue
        if _name_start(text, s0, r) is None:
            continue
        k = r + (1 if r < n and text[r] in "\"'" else 0)   # "password": x - a quoted name, as in JSON
        while k < n and text[k].isspace():
            k += 1
        if k >= n or text[k] not in "=:":
            continue
        k += 1
        while k < n and text[k].isspace():
            k += 1
        if k >= n:
            continue
        pos = _quoted_end(text, k)
        if pos is None:
            w = WS_RX.search(text, k)
            pos = w.start() if w else n
        spans.append((k, pos))
    for rx in (COMMON_AUTH_RX, COMMON_BEARER_RX, None) + _COMMON_TAIL:
        cur, marks = _render(text, spans)
        starts = [x[0] for x in marks]
        if rx is None:
            found = _url_spans(cur, 256)
        elif isinstance(rx, tuple):   # common.SCRUB[i], groups up to `keep` stay
            found = [(m.end(rx[1]) if rx[1] else m.start(), m.end()) for m in SCRUB[rx[0]].finditer(cur)]
        else:
            found = [(m.end(2), m.end()) for m in rx.finditer(cur)]
        spans += [_to_orig(marks, starts, a, b) for a, b in found if b > a]
    cur, marks = _render(text, spans)
    if MYSQL_LINE.search(cur):   # common.scrub's last rule: -pSECRET on a line that runs mysql
        starts, found, off = [x[0] for x in marks], [], 0
        for ln in cur.split("\n"):
            if MYSQL_LINE.search(ln):
                found += [(off + m.end(1), off + m.end()) for m in SCRUB[10].finditer(ln)]
            off += len(ln) + 1
        spans += [_to_orig(marks, starts, a, b) for a, b in found if b > a]
    return spans


# common.scrub's rules after the URL one, in its order: (SCRUB index, groups kept) - linear regexes
_COMMON_TAIL = ((4, 1), (5, 2), (6, 1), (7, 1), (8, 0), (9, 0))


def _quoted_end(text: str, k: int) -> int | None:
    """common.scrub's NAME rule takes a quoted value whole ("a b", 'a b': up to 4096 characters, one line)."""
    q = text[k]
    if q not in "\"'":
        return None
    j = text.find(q, k + 1, k + 4098)
    nl = text.find("\n", k + 1, j if j != -1 else k + 4098)
    return j + 1 if j != -1 and nl == -1 else None


def _name_start(text: str, s0: int, r: int) -> int | None:
    """Where common.scrub's NAME rule starts a name that ends the run text[s0:r], or None: leftmost-first, at
    a word boundary, the key word preceded by a separator (or at that boundary) within 65 characters and
    followed by the run's end or by a separator within 65 (its {0,64} affixes) - each key word looks back
    at most 65 characters: linear."""
    sub, best = text[s0:r], None
    for m in COMMON_WORD_RX.finditer(sub):
        kp, ke = m.span()
        if ke < len(sub) and len(sub) - ke - 1 > 64:
            continue
        if _BOUNDARY.match(text, s0 + kp):
            cand = kp
        else:
            cand = None
        if kp > 0 and sub[kp - 1] in "_.-":
            for b in range(max(0, kp - 65), kp):
                if _BOUNDARY.match(text, s0 + b):
                    cand = b
                    break
        if cand is not None and (best is None or cand < best):
            best = cand
    return None if best is None else s0 + best


def _common_linear(text: str) -> str:
    """common.scrub(text), the same string (tests compare them)."""
    return _render(text, _common_spans(text))[0]


def scrub_strict(text: str) -> str:
    """Hides every character common.scrub hides (its spans, exactly) and more - all over the original text."""
    return _hide(text, _common_spans(text) + _secret_spans(text, STRICT_WORDS, False) + _rule_spans(text, UNION_RULES)
                 + _url_spans(text))


def scrub_text(text: str) -> str:
    """For job logs and text uploads: scrub_strict's catches and more (any name with pass/key/auth/..., CLI
    options, private keys, base64 lines)."""
    # every span over the ORIGINAL text (common's key rule needs the header _private_keys would eat), the
    # key blocks after: the rest of a block that the spans left goes too
    return _private_keys(_hide(text, _common_spans(text) + _secret_spans(text, BROAD_WORDS, True)
                               + _rule_spans(text, UNION_RULES + LOG_RULES) + _url_spans(text)))


def _log_tail(raw: bytes, lines: int) -> str:
    """The last `lines` lines, cleaned and scrubbed BEFORE the cut (a cut line keeps no half secret);
    the byte cap drops whole lines from the top, and cuts the start of a last line that alone is too long."""
    text = ANSI_RX.sub("", raw.decode("utf-8", "replace"))
    rows = [LOG_TS_RX.sub("", ln.rsplit("\r", 1)[-1] if "\r" in ln.rstrip("\r") else ln.rstrip("\r"))
            for ln in text.split("\n")]
    while rows and not rows[-1]:
        rows.pop()   # the trace ends with a newline: no empty last "line" counted
    if len(raw) >= TRACE_KEEP and len(rows) > 1:
        rows = rows[1:]   # the first line of a kept tail is cut
    rows = scrub_text("\n".join(rows[-(lines + 200):])).split("\n")[-lines:]   # a key block above the tail is still seen
    while len(rows) > 1 and len("\n".join(rows).encode()) > LOG_MAX_BYTES:
        rows = rows[1:]
    out = "\n".join(rows)
    if len(out.encode()) > LOG_MAX_BYTES:
        out = "[cut] " + out.encode()[-LOG_MAX_BYTES:].decode("utf-8", "ignore")
    return out


def _target(args: dict, allowed_projects: list[str]) -> tuple[str, str]:
    project = str(args.get("project") or (allowed_projects[0] if len(allowed_projects) == 1 else ""))
    if not allowed_projects:
        raise GitLabError(400, "no GitLab project is mapped to this project (Settings -> Projects)")
    if not project:
        raise GitLabError(400, "project is required (one of: %s)" % ", ".join(allowed_projects))
    if project not in allowed_projects:
        raise GitLabError(403, "project %r is outside this session's scope (%s)"
                          % (project, ", ".join(allowed_projects) or "none"))
    return project, GitLab._pid(project)


# ------------------------------------------------------------------ carded tools: plan -> card -> execute
def plan(gl: GitLab, tool: str, args: dict, allowed_projects: list[str], *, upload: tuple[str, bytes] | None = None) -> dict:
    """Validate a carded call and look up what it needs - reads only, before the card, so a card that
    can only fail is never shown. card_text(plan) shows `head`, `show` (every value that is sent,
    derived ones included) and `long`; execute(plan) sends exactly `steps`, in order."""
    if tool not in CARD_TOOLS:
        raise GitLabError(400, "unknown GitLab tool %r" % tool)
    project, pid = _target(args, allowed_projects)
    show: dict = {}
    long: dict = {}
    steps: list[dict] = []
    p = {"tool": tool, "project": project, "show": show, "long": long, "steps": steps, "out": {}}

    def step(m, path, what, res=None, query=None, body=None):
        steps.append({"m": m, "path": path, "what": what, "res": res, "query": query, "body": body})

    def parent_gid(v) -> tuple[int, str | None]:
        n = int(v)
        if n < 0:
            raise GitLabError(400, "parent_iid must be an issue number (0 removes the parent)")
        if not n:
            return 0, None
        return n, _gid(_dict(gl._req("GET", "/projects/%s/issues/%d" % (pid, n)), "issue")["id"])

    def people(key, ids):
        if ids is not None:
            show[key] = [str(x).lstrip("@") for x in (args[key] if isinstance(args[key], list)
                                                      else re.split(r"[,\s]+", str(args[key]))) if x] or "(none)"
        return ids

    def put(key, value, label=None):
        if value is not None:
            show[label or key] = value if value != "" else "(cleared)"
        return value

    if tool == "gitlab_upload_file":
        if upload is None:
            raise GitLabError(400, "a validated local file is required")
        name, raw = upload
        if not name or any(c in name for c in '\r\n\0/') or len(raw) > UPLOAD_MAX:
            raise GitLabError(400, "invalid upload filename or file larger than 10 MiB")
        p["head"] = "Upload a file to " + project
        show.update(filename=name, size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        try:
            text = raw.decode("utf-8")
            if '\0' not in text:
                long["file preview"] = text[:16000] + ("\n[preview truncated]" if len(text) > 16000 else "")
        except UnicodeError:
            pass
        p["out"].update(project=project, **show)
        step("POST", "/projects/%s/uploads" % pid, "upload attachment", "upload")
        steps[-1]["upload"] = upload
    elif tool == "gitlab_comment":
        kind = args.get("kind")
        coll = _coll(kind)
        body = str(args.get("body") or "").strip()
        if not body or len(body) > 20000:
            raise GitLabError(400, "body must be 1..20000 characters")
        iid = _iid(args["iid"])
        p["head"] = "GitLab note on %s %s %s%d" % (project, kind, "#" if kind == "issue" else "!", iid)
        path = "/projects/%s/%s/%d/notes" % (pid, coll, iid)
        if args.get("note_id"):
            # edit in place: GitLab lets only the note's author change it (403 otherwise)
            nid = _iid(args["note_id"])
            old = _dict(gl._req("GET", "%s/%d" % (path, nid)), "note")
            p["head"] = "Edit of GitLab note %d on %s %s %s%d" % (nid, project, kind, "#" if kind == "issue" else "!", iid)
            long["before"] = str(old.get("body") or "")
            long["body"] = body
            p["out"] = {"project": project, "kind": kind, "iid": iid, "note_id": nid}
            step("PUT", "%s/%d" % (path, nid), "edit the note", "note", body={"body": body})
            return p
        if args.get("discussion_id"):
            did = _did(args["discussion_id"])
            p["head"] += ", reply in thread %s" % did
            path = "/projects/%s/%s/%d/discussions/%s/notes" % (pid, coll, iid, did)
        long["body"] = body
        p["out"] = {"project": project, "kind": kind, "iid": iid}
        step("POST", path, "post the note", "note", body={"body": body})
    elif tool == "gitlab_resolve":
        iid, did = _iid(args["iid"]), _did(args.get("discussion_id"))
        thread = _dict(gl._req("GET", "/projects/%s/merge_requests/%d/discussions/%s" % (pid, iid, did)), "discussion")
        if not any(isinstance(x, dict) and x.get("resolvable") for x in thread.get("notes") or []):
            raise GitLabError(400, "thread %s cannot be resolved (a plain comment, not a review thread)" % did)
        resolved = _bool(args, "resolved") is not False
        p["head"] = "%s thread %s on %s MR !%d" % ("Resolve" if resolved else "Unresolve", did, project, iid)
        p["out"] = {"iid": iid, "discussion_id": did, "resolved": resolved}
        step("PUT", "/projects/%s/merge_requests/%d/discussions/%s" % (pid, iid, did),
             "resolve" if resolved else "unresolve", query={"resolved": "true" if resolved else "false"})
    elif tool == "gitlab_issue_create":
        kind = args.get("type") or "issue"
        if kind not in ("issue", "task"):
            raise GitLabError(400, "type must be 'issue' or 'task'")
        p["head"] = "New GitLab %s in %s" % (kind, project)
        body = _drop_none({
            "title": put("title", _text(args, "title", 255, True)), "issue_type": kind,
            "labels": put("labels", _labels(args.get("labels"))),
            "assignee_ids": people("assignees", _user_ids(gl, args.get("assignees"))),
            "milestone_id": _milestone_id(gl, pid, args.get("milestone")) or None,
            "due_date": put("due_date", _date(args.get("due_date")) or None),
            "confidential": put("confidential", _bool(args, "confidential"))})
        if body.get("milestone_id"):
            show["milestone"] = str(args["milestone"]).strip()
        parent = parent_gid(args["parent_iid"]) if args.get("parent_iid") else (0, None)
        if parent[0]:
            show["parent"] = "#%d" % parent[0]
        description = _text(args, "description", 100000)
        if description is not None:
            body["description"] = long["description"] = description
        step("POST", "/projects/%s/issues" % pid, "create the issue", "created", body=body)
        if parent[0]:
            steps.append({"parent": parent[1], "parent_iid": parent[0], "of": "created",
                          "what": "set the parent #%d" % parent[0]})
    elif tool == "gitlab_issue_update":
        iid = _iid(args["iid"])
        p["head"] = "Change of %s issue #%d" % (project, iid)
        title = _text(args, "title", 255) or None
        mid = _milestone_id(gl, pid, args.get("milestone"))
        body = _drop_none({
            "title": put("title", title), "labels": put("labels", _labels(args.get("labels"))),
            "add_labels": put("add_labels", _labels(args.get("add_labels"))),
            "remove_labels": put("remove_labels", _labels(args.get("remove_labels"))),
            "assignee_ids": people("assignees", _user_ids(gl, args.get("assignees"))),
            "milestone_id": mid, "due_date": put("due_date", _date(args.get("due_date"))),
            "state_event": put("state", _state_event(args.get("state"))),
            "confidential": put("confidential", _bool(args, "confidential"))})
        if mid is not None:
            show["milestone"] = str(args["milestone"]).strip() or "(none)"
        description = _text(args, "description", 100000)
        if description is not None:
            body["description"] = long["description"] = description
        for k in ("spend", "estimate"):
            if args.get(k) is not None and not DURATION_RX.match(str(args[k]).strip()):
                raise GitLabError(400, "%s is a GitLab duration like 1h30m or 2d" % k)
        if not body and not any(args.get(k) is not None for k in ("spend", "estimate", "parent_iid")):
            raise GitLabError(400, "nothing to change")
        # the step most likely to fail first, the one that must not run twice (spent time) last
        if args.get("parent_iid") is not None:
            n, gid = parent_gid(args["parent_iid"])
            issue = _dict(gl._req("GET", "/projects/%s/issues/%d" % (pid, iid)), "issue")
            show["parent"] = "#%d" % n if n else "(none)"
            steps.append({"parent": gid, "parent_iid": n or None, "of": issue["id"],
                          "what": "set the parent" if n else "remove the parent"})
        step("PUT" if body else "GET", "/projects/%s/issues/%d" % (pid, iid), "change the issue", "issue",
             body=body or None)
        if args.get("estimate") is not None:
            step("POST", "/projects/%s/issues/%d/time_estimate" % (pid, iid), "set the estimate", "time",
                 query={"duration": put("estimate", str(args["estimate"]).strip())})
        if args.get("spend") is not None:
            step("POST", "/projects/%s/issues/%d/add_spent_time" % (pid, iid), "log the spent time", "time",
                 query={"duration": put("spend", str(args["spend"]).strip())})
    elif tool == "gitlab_issue_link":
        iid, target_iid = _iid(args["iid"]), _iid(args.get("target_iid"))
        target = str(args.get("target_project") or project)
        if target not in allowed_projects:
            raise GitLabError(403, "target project %r is outside this session's scope" % target)
        link = args.get("type") or "relates_to"
        if link not in ("relates_to", "blocks", "is_blocked_by"):
            raise GitLabError(400, "type must be relates_to, blocks or is_blocked_by")
        if target == project and target_iid == iid:
            raise GitLabError(400, "an issue cannot be linked to itself")
        for gp_, n_ in ((pid, iid), (GitLab._pid(target), target_iid)):   # both exist - before the card
            _dict(gl._req("GET", "/projects/%s/issues/%d" % (gp_, n_)), "issue")
        for r in _list(gl._req("GET", "/projects/%s/issues/%d/links" % (pid, iid)) or [], "links"):
            if r.get("iid") == target_iid and (r.get("references") or {}).get("full", "").split("#")[0].lower() == target.lower():
                raise GitLabError(409, "%s#%d is already linked to #%d (%s)" % (target, target_iid, iid, r.get("link_type")))
        p["head"] = "Link %s#%d %s %s#%d" % (project, iid, link, target, target_iid)
        p["out"] = {"iid": iid, "target": "%s#%d" % (target, target_iid), "type": link}
        step("POST", "/projects/%s/issues/%d/links" % (pid, iid), "link the issues",
             body={"target_project_id": target, "target_issue_iid": target_iid, "link_type": link})
    elif tool == "gitlab_mr_create":
        title = _text(args, "title", 255, True)
        if _bool(args, "draft") and not re.match(r"^(draft:|\[draft\]|\(draft\))", title, re.I):
            title = "Draft: " + title
        source = _branch(args.get("source_branch"), "source_branch")
        target = _branch(args.get("target_branch") or (gl._req("GET", "/projects/%s" % pid) or {}).get("default_branch"),
                         "target_branch")
        p["head"] = "New merge request in %s" % project
        p["target_branch"] = target
        show.update(title=title, source_branch=source, target_branch=target)
        mid = _milestone_id(gl, pid, args.get("milestone")) or None
        if mid:
            show["milestone"] = str(args["milestone"]).strip()
        body = _drop_none({
            "source_branch": source, "target_branch": target, "title": title,
            "labels": put("labels", _labels(args.get("labels"))),
            "assignee_ids": people("assignees", _user_ids(gl, args.get("assignees"))),
            "reviewer_ids": people("reviewers", _user_ids(gl, args.get("reviewers"))), "milestone_id": mid,
            "remove_source_branch": put("remove_source_branch", _bool(args, "remove_source_branch")),
            "squash": put("squash", _bool(args, "squash"))})
        description = _text(args, "description", 100000)
        if description is not None:
            body["description"] = long["description"] = description
        step("POST", "/projects/%s/merge_requests" % pid, "open the merge request", "mr", body=body)
    elif tool == "gitlab_job_play":
        jid = _iid(args.get("job_id"))
        job = _dict(gl._req("GET", "/projects/%s/jobs/%d" % (pid, jid)), "job")
        st = str(job.get("status") or "")
        # manual / delayed (scheduled): play = "Run" / "Start now"; finished: retry = a new job of the same definition
        verb = "play" if st in ("manual", "scheduled") else "retry" if st in ("failed", "canceled", "success") else ""
        if not verb:
            raise GitLabError(409, "job %d is %s - only a manual / delayed job starts, a finished one is retried" % (jid, st))
        p["head"] = "%s CI job %s (#%d, stage %s, %s) in %s" % ("Start" if verb == "play" else "Retry", job.get("name"), jid,
                                                               job.get("stage"), st, project)
        show.update(ref=job.get("ref"), pipeline=(job.get("pipeline") or {}).get("id"))
        p["ref"] = str(job.get("ref") or "")
        p["out"] = {"job_id": jid, "action": verb}
        step("POST", "/projects/%s/jobs/%d/%s" % (pid, jid, verb), verb, "job")
    elif tool == "gitlab_mr_merge":
        iid = _iid(args["iid"])
        mr = _dict(gl._req("GET", "/projects/%s/merge_requests/%d" % (pid, iid)), "merge request")
        if mr.get("state") != "opened":
            raise GitLabError(400, "MR !%d is %s, not open" % (iid, mr.get("state")))
        if not mr.get("sha"):
            raise GitLabError(409, "MR !%d has no head commit yet - try again in a moment" % iid)
        p["head"] = "Merge %s MR !%d (%s -> %s)" % (project, iid, mr.get("source_branch"), mr.get("target_branch"))
        p["target_branch"] = str(mr.get("target_branch") or "")
        show["title"] = mr.get("title")
        # sha: GitLab merges only the head the plan saw - a push in between makes it refuse (409)
        body = _drop_none({"sha": mr.get("sha") or None, "squash": put("squash", _bool(args, "squash")),
                           "should_remove_source_branch": put("remove_source_branch", _bool(args, "remove_source_branch")),
                           "merge_when_pipeline_succeeds": put("when_pipeline_succeeds", _bool(args, "when_pipeline_succeeds"))})
        p["out"] = {"iid": iid}
        # the target may be changed while the card waits: re-read it right before the merge
        steps.append({"m": "GET", "path": "/projects/%s/merge_requests/%d" % (pid, iid), "what": "re-check the target",
                      "res": "expect_target", "expect": p["target_branch"], "query": None, "body": None})
        step("PUT", "/projects/%s/merge_requests/%d/merge" % (pid, iid), "merge", "mr_state", body=body or None)
    elif tool == "gitlab_mr_update":
        iid = _iid(args["iid"])
        p["head"] = "Change of %s MR !%d" % (project, iid)
        if args.get("target_branch"):
            p["target_branch"] = str(args["target_branch"]).strip()
        title = _text(args, "title", 255) or None
        draft = _bool(args, "draft")
        if draft is not None:   # GitLab's draft flag is the title prefix
            cur = title or str(_dict(gl._req("GET", "/projects/%s/merge_requests/%d" % (pid, iid)), "mr").get("title"))
            bare = re.sub(r"^\s*(draft:|\[draft\]|\(draft\))\s*", "", cur, flags=re.I)
            title = ("Draft: " + bare) if draft else bare
        mid = _milestone_id(gl, pid, args.get("milestone"))
        body = _drop_none({
            "title": put("title", title),
            "target_branch": put("target_branch", _branch(args["target_branch"], "target_branch")
                                 if args.get("target_branch") else None),
            "labels": put("labels", _labels(args.get("labels"))),
            "add_labels": put("add_labels", _labels(args.get("add_labels"))),
            "remove_labels": put("remove_labels", _labels(args.get("remove_labels"))),
            "assignee_ids": people("assignees", _user_ids(gl, args.get("assignees"))),
            "reviewer_ids": people("reviewers", _user_ids(gl, args.get("reviewers"))),
            "milestone_id": mid, "state_event": put("state", _state_event(args.get("state"))),
            "remove_source_branch": put("remove_source_branch", _bool(args, "remove_source_branch")),
            "squash": put("squash", _bool(args, "squash"))})
        if mid is not None:
            show["milestone"] = str(args["milestone"]).strip() or "(none)"
        description = _text(args, "description", 100000)
        if description is not None:
            body["description"] = long["description"] = description
        if not body:
            raise GitLabError(400, "nothing to change")
        step("PUT", "/projects/%s/merge_requests/%d" % (pid, iid), "change the merge request", "mr_state", body=body)
    else:   # gitlab_job_log
        jid = _iid(args.get("job_id"))
        job = _dict(gl._req("GET", "/projects/%s/jobs/%d" % (pid, jid)), "job")
        if job.get("status") != "failed":
            raise GitLabError(403, "only the log of a failed job is available (job %d is %s)" % (jid, job.get("status")))
        p["lines"] = max(1, min(_whole(args.get("lines") or LOG_LINES, "lines"), LOG_MAX_LINES))
        p["head"] = "Read the last %d lines of the log of failed job %s (#%d, stage %s) in %s" % (
            p["lines"], job.get("name"), jid, job.get("stage"), project)
        long["note"] = ("The log is scrubbed of values that look secret (password/token/key-like names, JSON keys, "
                        "private keys, long base64 lines), but a job can print anything - configuration, environment.")
        p["out"] = {"job": _pick(job, ("id", "name", "stage", "status", "failure_reason", "web_url")), "lines": p["lines"]}
        step("TRACE", "/projects/%s/jobs/%d/trace" % (pid, jid), "read the log", "log")
    return p


def card_text(p: dict) -> str:
    """What the confirmation card shows - every value that will be sent, long texts in full."""
    rows = ["%s: %s" % (k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in p["show"].items()]
    return "\n".join([p["head"] + ":"] + rows + ["\n%s:\n%s" % (k, v) for k, v in p["long"].items()])


def execute(gl: GitLab, p: dict) -> dict:
    """Send a plan's steps in order. A step failing after another one went through is not an error
    the agent should retry blindly: the answer says what happened (`done`) and what did not."""
    out, done, created = dict(p["out"]), [], None
    for st in p["steps"]:
        try:
            if "parent" in st:
                issue_id = created["id"] if st["of"] == "created" else st["of"]
                res = gl.graphql(SET_PARENT_Q, {"id": _gid(issue_id), "parent": st["parent"]})
                errs = (res.get("workItemUpdate") or {}).get("errors") or []
                if errs:
                    raise GitLabError(400, "GitLab refused the parent: %s" % "; ".join(map(str, errs))[:300])
                out["parent_iid"] = st["parent_iid"]
            elif st["m"] == "TRACE":
                raw, _ = gl._open("GET", gl.base + st["path"], tail=TRACE_KEEP)
                out["log"] = _log_tail(raw, p["lines"])
            else:
                r = gl._req(st["m"], st["path"], st["query"], st["body"], **({"upload": st["upload"]} if "upload" in st else {}))
                if st["res"] == "note":
                    out["note_id"] = (r or {}).get("id")
                elif st["res"] == "upload":
                    out.update(_pick(_dict(r, "upload"), ("id", "url", "full_path", "markdown", "alt")))
                elif st["res"] == "created":
                    created = _dict(r, "issue")
                    out.update(_pick(created, ("iid", "title", "web_url", "issue_type")))
                elif st["res"] == "issue":
                    out.update(_shape(_dict(r, "issue"), ("iid", "title", "state", "labels", "assignees", "milestone",
                                                          "due_date", "web_url")))
                elif st["res"] == "time":
                    out["time_stats"] = r
                elif st["res"] == "mr":
                    out.update(_pick(_dict(r, "merge request"), MR_ROW_KEYS))
                elif st["res"] == "job":
                    out.update(_pick(_dict(r, "job"), ("id", "name", "stage", "status", "web_url")))
                elif st["res"] == "expect_target":
                    if (r or {}).get("target_branch") != st["expect"]:
                        raise GitLabError(409, "the merge request now targets %r, not %r as on the card - nothing "
                                               "merged" % ((r or {}).get("target_branch"), st["expect"]))
                    continue
                elif st["res"] == "mr_state":
                    out.update(_shape(_dict(r, "merge request"), ("iid", "title", "state", "labels", "assignees",
                                                                  "reviewers", "target_branch", "web_url")))
        except (GitLabError, ValueError, KeyError, TypeError) as e:
            if not done:
                raise
            out.update(partial=True, done=done, failed=st["what"], error=str(e),
                       hint="the steps in `done` happened - do not repeat them")
            break
        done.append(st["what"])
    return _scrub_obj(out)


def call(gl: GitLab, tool: str, args: dict, allowed_projects: list[str], hidden, scrub: bool = True) -> dict:
    """Run one allowlisted operation. `hidden(path) -> bool` withholds policy files from diffs.
    A carded tool runs here without its card (tests, the human's GUI): piperd plans, asks, executes.
    gitlab_upload answers {"_bytes": ...} - the caller stores them and never passes them on.
    scrub=False: the human's GUI (the issue picker) - the text as GitLab shows it; a session's call
    always scrubs. A carded tool and gitlab_upload scrub regardless (L1, 2026-10-07)."""
    if tool not in TOOLS:
        raise GitLabError(400, "unknown GitLab tool %r" % tool)
    if tool in CARD_TOOLS:
        return execute(gl, plan(gl, tool, args, allowed_projects))
    project, pid = _target(args, allowed_projects)
    per_page = max(1, min(_whole(args.get("per_page") or 20, "per_page"), MAX_PER_PAGE))   # a negative one offered next_page forever
    if tool in ("gitlab_issue", "gitlab_mr"):
        iid = _iid(args["iid"])
        coll = "issues" if tool == "gitlab_issue" else "merge_requests"
        item = _dict(gl._req("GET", "/projects/%s/%s/%d" % (pid, coll, iid)), coll)
        out = _shape(item, ISSUE_KEYS if tool == "gitlab_issue" else MR_KEYS)
        out["notes"], truncated = _thread(gl, pid, coll, iid)
        if truncated:
            out["notes_truncated"] = "%d threads or more - the newest may be missing" % (NOTE_PAGES * INBOX_PER_PAGE)
        ups = _uploads(item.get("description"), *[n.get("body") for n in out["notes"]])
        if ups:
            out["uploads"] = ups   # fetch one with gitlab_upload
        if tool == "gitlab_issue":
            links = _list(gl._req("GET", "/projects/%s/issues/%d/links" % (pid, iid)) or [], "links")
            if links:
                out["links"] = [dict(_pick(r, ("iid", "title", "state", "link_type", "web_url")),
                                     project=(r.get("references") or {}).get("full", "").split("#")[0] or None)
                                for r in links]
            try:
                out.update(_hierarchy(gl, project, iid))
            except GitLabError:   # an instance without work items: no parent / children
                pass
    elif tool == "gitlab_issues":
        page = max(1, _whole(args.get("page") or 1, "page"))
        ms = args.get("milestone")
        q = {"state": args.get("state") or "opened", "search": args.get("search"),
             "labels": args.get("labels"), "milestone": _one_line(str(ms), "milestone") if ms else None,
             "per_page": per_page, "page": page}
        rows = _list(gl._req("GET", "/projects/%s/issues" % pid, q), "issues")
        items = _mine_first(gl, pid, q, rows, triage=not ms and not args.get("page"))
        out = {"issues": items, "page": page}
        # `_req` has no headers: a full page may have a next one. A triage answer that appended assigned
        # issues from later pages offers none - page 2 (no triage) would repeat them; enumerate with page: 1
        if len(rows) >= per_page and len(items) == len(rows):
            out["next_page"] = page + 1
    elif tool == "gitlab_milestones":
        page = max(1, _whole(args.get("page") or 1, "page"))
        rows = _list(gl._req("GET", "/projects/%s/milestones" % pid,
                             {"state": args.get("state"), "search": args.get("search"),
                              "include_ancestors": "true", "per_page": per_page, "page": page}), "milestones")
        out = {"milestones": [_shape(r, MILESTONE_KEYS) for r in rows], "page": page}   # _shape: no bidi in titles (E25)
        if len(rows) >= per_page:
            out["next_page"] = page + 1
    elif tool == "gitlab_mrs":
        rows = _list(gl._req("GET", "/projects/%s/merge_requests" % pid,
                             {"state": args.get("state") or "opened", "search": args.get("search"),
                              "per_page": per_page}), "merge_requests")
        out = {"merge_requests": [_shape(r, ("iid", "title", "state", "source_branch", "updated_at",
                                             "web_url")) for r in rows]}
    elif tool == "gitlab_mr_changes":
        iid = _iid(args["iid"])
        diffs = _pages(gl, "/projects/%s/merge_requests/%d/diffs" % (pid, iid), {}, DIFF_PAGES)
        files = []
        for d in diffs:
            paths = [d.get("old_path") or "", d.get("new_path") or ""]
            if any(p and hidden(p) for p in paths):
                files.append({"old_path": paths[0], "new_path": paths[1],
                              "diff": "[withheld: matches the isolation policy]"})
            else:
                files.append(_pick(d, ("old_path", "new_path", "new_file", "deleted_file",
                                       "renamed_file", "diff")))
        out = {"files": files}
        if len(diffs) >= DIFF_PAGES * INBOX_PER_PAGE:
            out["truncated"] = True   # more files than the pages read
    elif tool == "gitlab_pipelines":
        rows = _list(gl._req("GET", "/projects/%s/pipelines" % pid,
                             {"ref": args.get("ref"), "per_page": per_page}), "pipelines")
        out = {"pipelines": [_pick(r, ("id", "status", "ref", "sha", "source", "created_at", "web_url"))
                             for r in rows]}
    elif tool == "gitlab_pipeline_jobs":
        rows = _pages(gl, "/projects/%s/pipelines/%d/jobs" % (pid, _iid(args.get("pipeline_id"))), {}, 2)
        out = {"jobs": [_pick(r, ("id", "name", "stage", "status", "failure_reason", "duration", "allow_failure",
                                  "web_url")) for r in rows]}
    elif tool == "gitlab_upload":
        m = UPLOAD_RX.search(str(args.get("url") or ""))
        if not m:
            raise GitLabError(400, "url must contain /uploads/<32 hex>/<file> (as listed in `uploads`)")
        secret, fname = m.group(1), urllib.parse.unquote(m.group(2))
        if "/" in fname or "\\" in fname or fname in (".", "..") or not fname:
            raise GitLabError(400, "bad upload file name")
        tail = "/uploads/%s/%s" % (secret, urllib.parse.quote(fname))
        try:   # GitLab >= 17.4, any role; the web path of an upload takes only a browser session
            raw, ctype = gl._open("GET", gl.base + "/projects/%s%s" % (pid, tail), limit=UPLOAD_MAX)
        except GitLabError as e:
            if e.status == 404:
                raise GitLabError(404, "upload not found (or GitLab older than 17.4, which cannot serve uploads "
                                       "to a token) - ask the user for the file") from None
            raise
        if "text/html" in ctype.lower():
            raise GitLabError(403, "GitLab answered with a web page (a login?) instead of the file")
        if len(raw) > UPLOAD_MAX:
            raise GitLabError(413, "the upload is larger than %d MiB" % (UPLOAD_MAX >> 20))
        ctype = ctype.split(";")[0].strip().lower()
        out = {"filename": fname, "size": len(raw), "content_type": ctype or None}
        kind, text = _upload_text(raw)
        if kind == "binary":
            raise GitLabError(415, "not handed over: it is neither an image/PDF nor text the scrub can read safely "
                                   "(UTF-8, UTF-16 in Latin/Cyrillic/Greek, plain 8-bit text) - an archive, a "
                                   "certificate, binary data, or text in another encoding; ask the user for what you "
                                   "need from it (as UTF-8 text)")
        if text is not None:   # a config, log or key a client attached: scrubbed like a job log
            if len(raw) > TEXT_SCRUB_MAX:
                raise GitLabError(413, "a text upload over %d MiB is not handed over (scrubbing it would hold piperd "
                                       "for seconds) - ask the user for the part you need" % (TEXT_SCRUB_MAX >> 20))
            if kind == "bytes":
                out["note"] = "not UTF-8: read as latin-1 - accents may be garbled"
            raw = scrub_text(text).encode("utf-8")
            out.update(scrubbed=True, size=len(raw), stored_as="utf-8")
        out = _scrub_obj(out)
        out["_bytes"] = raw
        return out
    else:
        raise GitLabError(400, "unknown GitLab tool %r" % tool)
    return _scrub_obj(out) if scrub else out


# ------------------------------------------------------------------ the GUI's CI state and MR (9.7 #7)
# Called by piperd for the HUMAN's GUI only, never through `call` / TOOLS (a session opens its
# merge requests with gitlab_mr_create, on a card).
PIPELINE_KEYS = ("id", "status", "ref", "sha", "web_url", "updated_at")
MR_ROW_KEYS = ("iid", "title", "state", "web_url", "source_branch", "target_branch")


def _scoped(project: str, allowed_projects: list[str]) -> str:
    if not project or project not in allowed_projects:
        raise GitLabError(403, "project %r is outside this project's GitLab scope (%s)"
                          % (project, ", ".join(allowed_projects) or "none"))
    return GitLab._pid(project)


def latest_pipeline(gl: GitLab, project: str, ref: str, allowed_projects: list[str]) -> dict | None:
    pid = _scoped(project, allowed_projects)
    rows = _list(gl._req("GET", "/projects/%s/pipelines" % pid,
                         {"ref": ref, "per_page": 1, "order_by": "id", "sort": "desc"}) or [], "pipelines")
    return _scrub_obj(_pick(rows[0], PIPELINE_KEYS)) if rows else None


def open_mr(gl: GitLab, project: str, branch: str, allowed_projects: list[str]) -> dict | None:
    pid = _scoped(project, allowed_projects)
    rows = _list(gl._req("GET", "/projects/%s/merge_requests" % pid,
                         {"source_branch": branch, "state": "opened", "per_page": 1}) or [], "merge_requests")
    return _scrub_obj(_pick(rows[0], MR_ROW_KEYS)) if rows else None


def default_branch(gl: GitLab, project: str, allowed_projects: list[str]) -> str | None:
    pid = _scoped(project, allowed_projects)
    return (gl._req("GET", "/projects/%s" % pid) or {}).get("default_branch") or None


def create_mr(gl: GitLab, project: str, allowed_projects: list[str], source: str, target: str, title: str,
              description: str) -> dict:
    """Open a merge request (the GUI has already asked the human)."""
    pid = _scoped(project, allowed_projects)
    title = title.strip()
    if not title or len(title) > 255:
        raise GitLabError(400, "title must be 1..255 characters")
    if len(description) > 100000:
        raise GitLabError(400, "description is too long")
    mr = gl._req("POST", "/projects/%s/merge_requests" % pid,
                 body={"source_branch": source, "target_branch": target, "title": title,
                       "description": description}) or {}
    return _pick(mr, MR_ROW_KEYS)


# ------------------------------------------------------------------ the GUI's GitLab inbox (9.8)
# GUI-only like the CI helpers above: never in TOOLS, never reachable through `call` - a
# sandbox reaches every name in TOOLS, and must neither list the user's whole account nor
# mark to-dos done or post notes without a click. The agents' `gitlab_inbox` is served by
# piperd from the poller's items (scoped to the session's projects, inbox.py).
INBOX_PAGES = 3
NOTE_PAGES = 10   # gitlab_issue / gitlab_mr: up to 1000 notes
DIFF_PAGES = 10   # gitlab_mr_changes: up to 1000 files
INBOX_PER_PAGE = 100
PREVIEW_MAX = 20000


def _pages(gl: GitLab, path: str, query: dict, pages: int = INBOX_PAGES, until_empty: bool = False) -> list:
    """Up to `pages` pages of a list endpoint (`_req` has no headers: a short page is the last - until_empty: only
    an empty one, for endpoints GitLab filters after paginating: discussions answered 99 with a page 3 behind)."""
    out: list = []
    for page in range(1, pages + 1):
        rows = gl._req("GET", path, dict(query, per_page=INBOX_PER_PAGE, page=page))
        if not isinstance(rows, list):
            raise GitLabError(502, "GitLab %s: unexpected answer" % path)
        out += [r for r in rows if isinstance(r, dict)]
        if not rows if until_empty else len(rows) < INBOX_PER_PAGE:
            break
    return out


def todos(gl: GitLab) -> list:
    return _pages(gl, "/todos", {"state": "pending"})


def assigned_issues(gl: GitLab) -> list:
    return _pages(gl, "/issues", {"scope": "assigned_to_me", "state": "opened"})


def own_mrs(gl: GitLab) -> list:
    return _pages(gl, "/merge_requests", {"scope": "created_by_me", "state": "opened"})


def _coll(kind: str) -> str:
    if kind not in ("issue", "mr"):
        raise GitLabError(400, "kind must be 'issue' or 'mr'")
    return "issues" if kind == "issue" else "merge_requests"


def recent_notes(gl: GitLab, project: str, kind: str, iid: int, n: int = 50) -> list[dict]:
    """The newest `n` non-system notes, newest first: {id, user (username), created_at}."""
    rows = gl._req("GET", "/projects/%s/%s/%d/notes" % (GitLab._pid(project), _coll(kind), _iid(iid)),
                   {"sort": "desc", "order_by": "created_at", "per_page": n}) or []
    return [{"id": r.get("id"), "user": (r.get("author") or {}).get("username"), "created_at": r.get("created_at")}
            for r in rows if isinstance(r, dict) and not r.get("system") and isinstance(r.get("id"), int)]


def mark_todo_done(gl: GitLab, todo_id: int) -> None:
    gl._req("POST", "/todos/%d/mark_as_done" % _iid(todo_id))


def post_note(gl: GitLab, project: str, kind: str, iid: int, body: str, allowed_projects: list[str]) -> dict:
    """A note from the GUI (the human wrote / confirmed it there)."""
    pid = _scoped(project, allowed_projects)
    body = str(body or "")
    if not body.strip() or len(body) > 20000:
        raise GitLabError(400, "body must be 1..20000 characters")
    note = gl._req("POST", "/projects/%s/%s/%d/notes" % (pid, _coll(kind), _iid(iid)), body={"body": body}) or {}
    return {"note_id": note.get("id")}


def item_preview(gl: GitLab, project: str, kind: str, iid: int, allowed_projects: list[str], scrub: bool = True) -> dict:
    """Issue / MR metadata and its newest <= 100 non-system notes (ascending) for the GUI.
    Notes keep `user` (the author's username) for the caller's new/mine flags.
    scrub=False: the human's inbox preview - the text as GitLab shows it (L1); nothing of it goes
    to a session (a task carries references only, the session reads the issue with its own tool)."""
    pid = _scoped(project, allowed_projects)
    coll = _coll(kind)
    iid = _iid(iid)
    d = _dict(gl._req("GET", "/projects/%s/%s/%d" % (pid, coll, iid)) or {}, coll)
    rows = _list(gl._req("GET", "/projects/%s/%s/%d/notes" % (pid, coll, iid),
                         {"sort": "desc", "order_by": "created_at", "per_page": 100}) or [], "notes")
    out = _shape(d, ("iid", "title", "state", "web_url", "author", "created_at", "assignees", "labels"))
    out["type"] = kind
    out["description"] = str(d.get("description") or "")[:PREVIEW_MAX]
    out["notes"] = [{"id": r.get("id"), "author": _people(r.get("author")), "user": (r.get("author") or {}).get("username"),
                     "body": str(r.get("body") or "")[:PREVIEW_MAX], "created_at": r.get("created_at")}
                    for r in reversed(rows) if isinstance(r, dict) and not r.get("system")]
    if kind == "mr":
        pipe = d.get("head_pipeline") or d.get("pipeline")
        out["mr"] = {"source_branch": d.get("source_branch"), "target_branch": d.get("target_branch"),
                     "merge_status": d.get("detailed_merge_status") or d.get("merge_status"),
                     "pipeline": pipe.get("status") if isinstance(pipe, dict) else None,
                     "draft": bool(d.get("draft") or d.get("work_in_progress"))}
    return _scrub_obj(out) if scrub else out
