"""GitHub served by piperd on the host - gitlab.py's twin.

The same contract as GitLab (11.3.4a): piperd holds the token, a sandbox gets a fixed set of
operations - never a generic path proxy - each mapped to one API endpoint template:

  read   issues (detail + comments, list), pull requests (detail + conversation, review comments
         with file/line and reviews, list, changed files with policy-matching files withheld),
         Actions workflow runs and their jobs, the tail of a FAILED job's log (scrubbed, on a card)
  write  comments (new, or edit an own one); issues (create, edit, close); pull requests (open,
         edit) - each only after a human confirms it in the GUI

Denied by construction (no operation reaches them): contents, git blobs / trees, archives,
releases' assets, gists, logs of jobs that did not fail. Every operation is confined to the
repositories (`owner/repo`) mapped to the calling session (projects.json "github", or the
checkout's trusted origin). The token is the host's own `gh auth token` (the user's GitHub CLI
login, in the macOS keychain), read per call with a short in-memory cache: never written to a
file, logged, or passed to a sandbox. GitHub Enterprise: policy github_hosts.

The shapes piperd hands on match gitlab.py's where the GUI and the inbox share code: a pull
request is `iid` / `source_branch` / `target_branch` / state "opened"; a workflow run's status
uses GitLab's pipeline words (success, failed, running, pending, canceled, skipped, manual).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import gitlab
from common import no_bidi
from gitlab import (TRACE_DEADLINE, TRACE_KEEP, LOG_LINES, LOG_MAX_LINES, _bool, _drop_none, _labels, _one_line,
                    _scrub_obj, _text, _whole, card_text, scrub_text)

TIMEOUT = 20
MAX_PER_PAGE = 50
WRITE_TOOLS = {"github_comment": "github_comment",
               "github_issue_create": "github_issue_write", "github_issue_update": "github_issue_write",
               "github_pr_create": "github_pr_write", "github_pr_update": "github_pr_write"}
# a failed job's log runs after a card like GitLab's: a job can print anything (K5)
CARD_TOOLS = dict(WRITE_TOOLS, github_job_log="github_job_log")

TOOLS = {
    "github_issue": "Issue detail with all comments (they carry as much of the requirement as the description).",
    "github_issues": "List issues of a repository (state, search, labels, page).",
    "github_pr": "Pull request detail with its conversation, review comments (file/line) and reviews.",
    "github_prs": "List pull requests of a repository (state).",
    "github_pr_files": "Changed files of a pull request; files matching the isolation policy are withheld.",
    "github_runs": "Recent GitHub Actions workflow runs of a repository (branch).",
    "github_run_jobs": "Jobs of a workflow run (name, status, conclusion, failed steps).",
    "github_job_log": "The scrubbed tail of a failed job's log. Needs the user's confirmation.",
    "github_comment": "Comment on an issue or pull request, or with comment_id edit an own comment. Needs the user's confirmation.",
    "github_issue_create": "Create an issue. Needs the user's confirmation.",
    "github_issue_update": "Edit an issue (title, body, labels, assignees, milestone, state). Needs the user's confirmation.",
    "github_pr_create": "Open a pull request. Needs the user's confirmation.",
    "github_pr_update": "Edit a pull request (title, body, base, state). Needs the user's confirmation.",
}

REPO_RX = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RX = gitlab.BRANCH_RX
GH_TS_RX = re.compile(rb"(?m)^\d{4}-\d\d-\d\dT[\d:.]+Z ")   # Actions timestamps every log line
# where a job log's 302 may lead (presigned, fetched without the token): GitHub's own storage
LOG_HOSTS = (".actions.githubusercontent.com", ".blob.core.windows.net")


class GitHubError(gitlab.GitLabError):
    """A GitLabError, so every handler that reports a forge failure to the GUI / agent takes it as is."""


# ------------------------------------------------------------------ the token: the host's gh login
TOKEN_TTL = 300
_TOKENS: dict[str, tuple[float, str | None]] = {}


def gh_bin() -> str | None:
    """The gh CLI - piperd's service PATH is thin, Homebrew's prefix is tried too."""
    for c in (shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/home/linuxbrew/.linuxbrew/bin/gh"):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def token(host: str = "github.com") -> str | None:
    """`gh auth token --hostname host`, cached in memory for TOKEN_TTL s (a logout / switch shows within
    minutes, the keychain is not asked on every poll). None without gh or a login."""
    hit = _TOKENS.get(host)
    if hit and time.monotonic() - hit[0] < TOKEN_TTL:
        return hit[1]
    tok = None
    gh = gh_bin()
    if gh:
        try:
            r = subprocess.run([gh, "auth", "token", "--hostname", host], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL)
            tok = r.stdout.strip() or None if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            tok = None
    _TOKENS[host] = (time.monotonic(), tok)
    return tok


def forget_token(host: str = "github.com") -> None:
    """After a 401: the next call asks gh again (the user re-logged in)."""
    _TOKENS.pop(host, None)


def api_base(host: str) -> str:
    return "https://api.github.com" if host in ("github.com", "api.github.com") else "https://%s/api/v3" % host


class GitHub:
    def __init__(self, host: str, tok: str | None, opener=None):
        self.host = host
        self.base = api_base(host)
        self.tok = tok
        self.opener = opener or gitlab._OPENER.open   # redirects refused: the token would go along

    def _req(self, method: str, path: str, query: dict | None = None, body: dict | None = None,
             headers: dict | None = None):
        raw, _, _ = self._open(method, self.base + path, query, body, headers=headers)
        try:
            return json.loads(raw.decode() or "null")
        except ValueError:
            raise GitHubError(502, "GitHub %s %s: not JSON" % (method, path)) from None

    def _open(self, method: str, url: str, query: dict | None = None, body: dict | None = None,
              tail: int = 0, headers: dict | None = None, auth: bool = True) -> tuple[bytes, str, dict]:
        """(body, content type, response headers); tail > 0 streams and keeps the last `tail` bytes.
        A 304 (If-Modified-Since) answers b"" with status 304 in the headers. auth=False: a presigned
        URL (a job log's storage) - no token."""
        if auth and not self.tok:
            raise GitHubError(503, "no GitHub token for piperd - run `gh auth login --hostname %s` on this Mac" % self.host)
        path = urllib.parse.urlsplit(url).path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "piper"}
        if auth:
            h["Authorization"] = "Bearer " + self.tok
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener(req, timeout=TIMEOUT) as r:
                hdrs = dict(getattr(r, "headers", None) or {})
                ctype = str(hdrs.get("Content-Type") or "")
                if tail:
                    buf, deadline = bytearray(), time.monotonic() + TRACE_DEADLINE
                    while True:
                        if time.monotonic() > deadline:
                            raise GitHubError(504, "GitHub log: still streaming after %d s" % TRACE_DEADLINE)
                        chunk = (getattr(r, "read1", None) or r.read)(1 << 16)
                        if not chunk:
                            return bytes(buf[-tail:]), ctype, hdrs
                        buf += chunk
                        if len(buf) > 2 * tail:
                            del buf[:-tail]
                return r.read(), ctype, hdrs
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return b"", "", {"status": 304}
            if 300 <= e.code < 400:
                loc = str((e.headers or {}).get("Location") or "")
                raise GitHubError(302 if loc else 502, loc or "GitHub %s %s: redirected (HTTP %d) - not followed"
                                  % (method, path, e.code)) from None
            if e.code == 401 and auth:
                forget_token(self.host)
            if e.code == 403 and str((e.headers or {}).get("X-RateLimit-Remaining") or "") == "0":
                raise GitHubError(429, "GitHub rate limit reached - try again later") from None
            raise GitHubError(e.code, "GitHub %s %s: HTTP %d" % (method, path, e.code)) from None
        except urllib.error.URLError as e:
            raise GitHubError(502, "GitHub unreachable: %s" % e.reason) from None
        except OSError as e:
            raise GitHubError(502, "GitHub %s %s: %s" % (method, path, type(e).__name__)) from None

    def log(self, path: str, tail: int) -> bytes:
        """A job log: the API answers 302 to presigned storage - followed once, only to GitHub's
        storage hosts (or the GHE host itself), and without the token."""
        try:
            raw, _, _ = self._open("GET", self.base + path, tail=tail)
            return raw
        except GitHubError as e:
            if e.status != 302:
                raise
            loc = str(e)
        u = urllib.parse.urlsplit(loc)
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == self.host.lower() or host.endswith(LOG_HOSTS)):
            raise GitHubError(502, "GitHub sent the log to an unexpected host - not followed")
        raw, _, _ = self._open("GET", loc, tail=tail, auth=False)
        return raw


def _repo(v) -> str:
    r = str(v or "").strip()
    if not REPO_RX.match(r) or ".." in r:
        raise GitHubError(400, "repo is owner/name")
    return r


def _num(v) -> int:
    n = _whole(v, "number")
    if n <= 0:
        raise ValueError("number must be positive")
    return n


def _list(rows, path: str) -> list:
    if not isinstance(rows, list):
        raise GitHubError(502, "GitHub %s: unexpected answer" % path)
    return [r for r in rows if isinstance(r, dict)]


def _dict(d, path: str) -> dict:
    if not isinstance(d, dict):
        raise GitHubError(502, "GitHub %s: unexpected answer" % path)
    return d


def _login(o):
    if isinstance(o, dict) and "login" in o:   # the author's own text: no bidi tricks (E25)
        return no_bidi(str(o["login"]))
    if isinstance(o, list):
        return [_login(x) for x in o]
    return o


STATE = {"open": "opened", "closed": "closed"}


def _shape(d: dict) -> dict:
    """An issue / PR in gitlab.py's words: iid, description, web_url, state opened|closed|merged."""
    out = {"iid": d.get("number"), "title": no_bidi(str(d.get("title") or "")),
           "state": "merged" if d.get("merged_at") else STATE.get(d.get("state"), d.get("state")),
           "description": d.get("body") or "", "author": _login(d.get("user")),
           "assignees": _login(d.get("assignees") or []),
           "labels": [no_bidi(str(x.get("name"))) for x in d.get("labels") or [] if isinstance(x, dict)],
           "milestone": no_bidi(str((d.get("milestone") or {}).get("title"))) if d.get("milestone") else None,
           "created_at": d.get("created_at"), "updated_at": d.get("updated_at"), "web_url": d.get("html_url")}
    if "head" in d or d.get("pull_request"):
        out["source_branch"] = (d.get("head") or {}).get("ref")
        out["target_branch"] = (d.get("base") or {}).get("ref")
        out["draft"] = bool(d.get("draft"))
        if "requested_reviewers" in d:
            out["reviewers"] = _login(d.get("requested_reviewers") or [])
        if d.get("mergeable_state"):
            out["merge_status"] = d.get("mergeable_state")
    return {k: v for k, v in out.items() if v is not None}


def _row(d: dict) -> dict:
    s = _shape(d)
    return {k: s[k] for k in ("iid", "title", "state", "labels", "milestone", "updated_at", "web_url",
                              "source_branch", "draft") if k in s}


def _comment(c: dict) -> dict:
    return {"id": c.get("id"), "body": c.get("body") or "", "author": _login(c.get("user")),
            "created_at": c.get("created_at")}


def split(entry: str) -> tuple[str, str]:
    """A scope entry -> (host, owner/repo): `owner/repo` is github.com, `host/owner/repo` a GHE host."""
    parts = str(entry).split("/")
    return (parts[0], "/".join(parts[1:])) if len(parts) == 3 else ("github.com", str(entry))


def _same(entry: str, want: str) -> bool:
    """`want` (owner/repo or host/owner/repo) names scope entry `entry` - GitHub names are case-insensitive."""
    e, w = entry.lower(), want.lower()
    return e == w or (w.count("/") == 1 and split(e)[1] == w)


def _target(args: dict, allowed: list[str]) -> str:
    """The scope entry a call is for, confined to the session's repositories."""
    if not allowed:
        raise GitHubError(400, "no GitHub repository is mapped to this project (an origin on GitHub, or "
                               "projects.json \"github\")")
    repo = str(args.get("repo") or (allowed[0] if len(allowed) == 1 else "")).strip()
    if not repo:
        raise GitHubError(400, "repo is required (one of: %s)" % ", ".join(allowed))
    _repo(split(repo)[1])
    hits = [a for a in allowed if _same(a, repo)]
    if len(hits) != 1:
        raise GitHubError(403, "repo %r is outside this session's scope (%s)" % (repo, ", ".join(allowed))
                          if not hits else "repo %r is ambiguous - name its host too (%s)" % (repo, ", ".join(hits)))
    return hits[0]


def _scoped(repo: str, allowed: list[str]) -> str:
    for a in allowed:
        if repo and _same(a, repo):
            return split(a)[1]
    raise GitHubError(403, "repo %r is outside this project's GitHub scope (%s)" % (repo, ", ".join(allowed) or "none"))


def _branch(v, key: str) -> str:
    v = str(v or "")
    if not BRANCH_RX.match(v):
        raise GitHubError(400, "%s is not a branch name" % key)
    return v


def _people(v) -> list[str] | None:
    """Logins (with or without @) as GitHub takes them; [] clears."""
    if v is None:
        return None
    items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v)) if x]
    if len(items) > 10:
        raise GitHubError(400, "at most 10 people")
    out = []
    for x in items:
        x = _one_line(str(x).lstrip("@"), "a login")
        if not re.match(r"^[A-Za-z0-9-]{1,39}$", x):
            raise GitHubError(400, "%r is not a GitHub login" % x)
        out.append(x)
    return out


def _milestone(gh: GitHub, repo: str, title) -> int | None:
    """A milestone title -> its number; "" removes it (0 -> null)."""
    if title is None:
        return None
    if not str(title).strip():
        return 0
    want = _one_line(str(title).strip(), "milestone")
    for m in _pages(gh, "/repos/%s/milestones" % repo, {"state": "all"}, 2):
        if m.get("title") == want:
            return m.get("number")
    raise GitHubError(400, "no milestone %r in %s" % (want, repo))


def _state(v) -> str | None:
    if v in (None, ""):
        return None
    m = {"close": "closed", "closed": "closed", "reopen": "open", "open": "open"}
    if v not in m:
        raise GitHubError(400, "state must be 'close' or 'reopen'")
    return m[v]


def _log_tail(raw: bytes, lines: int) -> str:
    return gitlab._log_tail(GH_TS_RX.sub(b"", raw), lines)


# ------------------------------------------------------------------ carded tools: plan -> card -> execute
def plan(gh: GitHub, tool: str, args: dict, allowed: list[str]) -> dict:
    """gitlab.plan's twin: validated and looked up before the card; card_text shows every value sent."""
    if tool not in CARD_TOOLS:
        raise GitHubError(400, "unknown GitHub tool %r" % tool)
    entry = _target(args, allowed)
    repo = split(entry)[1]
    show: dict = {}
    long: dict = {}
    steps: list[dict] = []
    p = {"tool": tool, "project": entry, "show": show, "long": long, "steps": steps, "out": {}}

    def step(m, path, what, res=None, body=None):
        steps.append({"m": m, "path": path, "what": what, "res": res, "query": None, "body": body})

    def put(key, value):
        if value is not None:
            show[key] = value if value not in ("", []) else "(cleared)"
        return value

    if tool == "github_comment":
        kind = args.get("kind") or "issue"
        if kind not in ("issue", "pr"):
            raise GitHubError(400, "kind must be 'issue' or 'pr'")
        body = str(args.get("body") or "").strip()
        if not body or len(body) > 65000:
            raise GitHubError(400, "body must be 1..65000 characters")
        n = _num(args.get("number"))
        what = "%s %s #%d" % (repo, "issue" if kind == "issue" else "pull request", n)
        if args.get("comment_id"):
            cid = _num(args["comment_id"])
            old = _dict(gh._req("GET", "/repos/%s/issues/comments/%d" % (repo, cid)), "comment")
            if not str(old.get("issue_url") or "").endswith("/issues/%d" % n):
                raise GitHubError(400, "comment %d is not on #%d" % (cid, n))
            p["head"] = "Edit of GitHub comment %d on %s" % (cid, what)
            long["before"] = str(old.get("body") or "")
            long["body"] = body
            p["out"] = {"repo": repo, "number": n, "comment_id": cid}
            step("PATCH", "/repos/%s/issues/comments/%d" % (repo, cid), "edit the comment", "comment", {"body": body})
            return p
        p["head"] = "GitHub comment on " + what
        long["body"] = body
        p["out"] = {"repo": repo, "number": n}
        step("POST", "/repos/%s/issues/%d/comments" % (repo, n), "post the comment", "comment", {"body": body})
    elif tool in ("github_issue_create", "github_issue_update"):
        create = tool == "github_issue_create"
        n = None if create else _num(args.get("number"))
        p["head"] = "New GitHub issue in %s" % repo if create else "Change of %s issue #%d" % (repo, n)
        labels = _labels(args.get("labels"))
        mid = _milestone(gh, repo, args.get("milestone"))
        if mid is not None:
            show["milestone"] = str(args["milestone"]).strip() or "(none)"
        body = _drop_none({
            "title": put("title", _text(args, "title", 256, create) or None),
            "labels": put("labels", [x for x in labels.split(",") if x] if labels is not None else None),
            "assignees": put("assignees", _people(args.get("assignees"))),
            "milestone": (mid or None) if mid is not None else None,
            "state": None if create else put("state", _state(args.get("state")))})
        if mid == 0:
            body["milestone"] = None   # JSON null removes it
        description = _text(args, "description", 65000)
        if description is not None:
            body["body"] = long["description"] = description
        if not create and not body and not any(args.get(k) for k in ("add_labels", "remove_labels")):
            raise GitHubError(400, "nothing to change")
        if create:
            step("POST", "/repos/%s/issues" % repo, "create the issue", "issue", body)
        else:
            if body:
                step("PATCH", "/repos/%s/issues/%d" % (repo, n), "change the issue", "issue", body)
            add = _labels(args.get("add_labels"))
            if add:
                show["add_labels"] = add
                step("POST", "/repos/%s/issues/%d/labels" % (repo, n), "add labels", None, {"labels": add.split(",")})
            for lab in [x for x in (_labels(args.get("remove_labels")) or "").split(",") if x]:
                show.setdefault("remove_labels", []).append(lab)
                step("DELETE", "/repos/%s/issues/%d/labels/%s" % (repo, n, urllib.parse.quote(lab, safe="")),
                     "remove label %s" % lab)
            p["out"] = {"repo": repo, "number": n}
    elif tool == "github_pr_create":
        title = _text(args, "title", 256, True)
        head = _branch(args.get("source_branch"), "source_branch")
        base = _branch(args.get("target_branch") or default_branch(gh, repo, [entry]), "target_branch")
        p["head"] = "New pull request in %s" % repo
        p["target_branch"] = base
        show.update(title=title, source_branch=head, target_branch=base)
        body = {"title": title, "head": head, "base": base}
        if _bool(args, "draft"):
            body["draft"] = show["draft"] = True
        description = _text(args, "description", 65000)
        if description is not None:
            body["body"] = long["description"] = description
        step("POST", "/repos/%s/pulls" % repo, "open the pull request", "pr", body)
    elif tool == "github_pr_update":
        n = _num(args.get("number"))
        p["head"] = "Change of %s pull request #%d" % (repo, n)
        if args.get("draft") is not None:
            raise GitHubError(400, "draft cannot be changed through the REST API - ask the user to do it on GitHub")
        if args.get("target_branch"):
            p["target_branch"] = str(args["target_branch"]).strip()
        body = _drop_none({
            "title": put("title", _text(args, "title", 256) or None),
            "base": put("target_branch", _branch(args["target_branch"], "target_branch") if args.get("target_branch") else None),
            "state": put("state", _state(args.get("state")))})
        description = _text(args, "description", 65000)
        if description is not None:
            body["body"] = long["description"] = description
        if not body:
            raise GitHubError(400, "nothing to change")
        step("PATCH", "/repos/%s/pulls/%d" % (repo, n), "change the pull request", "pr", body)
    else:   # github_job_log
        jid = _num(args.get("job_id"))
        job = _dict(gh._req("GET", "/repos/%s/actions/jobs/%d" % (repo, jid)), "job")
        if job.get("conclusion") != "failure":
            raise GitHubError(403, "only the log of a failed job is available (job %d is %s)"
                              % (jid, job.get("conclusion") or job.get("status")))
        p["lines"] = max(1, min(_whole(args.get("lines") or LOG_LINES, "lines"), LOG_MAX_LINES))
        p["head"] = "Read the last %d lines of the log of failed job %s (#%d) in %s" % (p["lines"], job.get("name"), jid, repo)
        long["note"] = ("The log is scrubbed of values that look secret (password/token/key-like names, JSON keys, "
                        "private keys, long base64 lines), but a job can print anything - configuration, environment.")
        p["out"] = {"job": {"id": jid, "name": job.get("name"), "conclusion": job.get("conclusion"),
                            "web_url": job.get("html_url"), "run_id": job.get("run_id")}, "lines": p["lines"]}
        p["ref"] = str(job.get("head_branch") or "")
        step("LOG", "/repos/%s/actions/jobs/%d/logs" % (repo, jid), "read the log", "log")
    return p


def execute(gh: GitHub, p: dict) -> dict:
    """gitlab.execute's twin: the steps in order; a later failure says what already happened."""
    out, done = dict(p["out"]), []
    for st in p["steps"]:
        try:
            if st["m"] == "LOG":
                out["log"] = _log_tail(gh.log(st["path"], TRACE_KEEP), p["lines"])
            else:
                r = gh._req(st["m"], st["path"], st["query"], st["body"])
                if st["res"] == "comment":
                    out.update(comment_id=(r or {}).get("id"), web_url=(r or {}).get("html_url"))
                elif st["res"] in ("issue", "pr"):
                    d = _dict(r, st["res"])
                    s = _shape(d)
                    out.update({k: s[k] for k in ("iid", "title", "state", "labels", "assignees", "milestone",
                                                  "source_branch", "target_branch", "draft", "web_url") if k in s})
                    out["number"] = d.get("number")
        except (GitHubError, ValueError, KeyError, TypeError) as e:
            if not done:
                raise
            out.update(partial=True, done=done, failed=st["what"], error=str(e),
                       hint="the steps in `done` happened - do not repeat them")
            break
        done.append(st["what"])
    return _scrub_obj(out)


# ------------------------------------------------------------------ reads
FILE_PAGES = 30   # github_pr_files: GitHub lists at most 3000 files
NOTE_PAGES = 10
INBOX_PAGES = 3
INBOX_PER_PAGE = 100
PREVIEW_MAX = 20000
RUN_KEYS = ("id", "name", "status", "conclusion", "head_branch", "head_sha", "event", "created_at", "html_url")


def _pages(gh: GitHub, path: str, query: dict, pages: int = INBOX_PAGES, key: str | None = None) -> list:
    """Up to `pages` pages of a list endpoint (a short page is the last); `key`: the list sits in an object."""
    out: list = []
    for page in range(1, pages + 1):
        r = gh._req("GET", path, dict(query, per_page=INBOX_PER_PAGE, page=page))
        rows = (r or {}).get(key) if key and isinstance(r, dict) else r
        if not isinstance(rows, list):
            raise GitHubError(502, "GitHub %s: unexpected answer" % path)
        out += [x for x in rows if isinstance(x, dict)]
        if len(rows) < INBOX_PER_PAGE:
            break
    return out


def call(gh: GitHub, tool: str, args: dict, allowed: list[str], hidden, scrub: bool = True) -> dict:
    """gitlab.call's twin. `hidden(path) -> bool` withholds policy files from a PR's files.
    scrub=False: the human's GUI; a session's call always scrubs, a carded tool regardless."""
    if tool not in TOOLS:
        raise GitHubError(400, "unknown GitHub tool %r" % tool)
    if tool in CARD_TOOLS:
        return execute(gh, plan(gh, tool, args, allowed))
    repo = split(_target(args, allowed))[1]
    per_page = max(1, min(_whole(args.get("per_page") or 20, "per_page"), MAX_PER_PAGE))
    if tool in ("github_issue", "github_pr"):
        n = _num(args.get("number"))
        item = _dict(gh._req("GET", "/repos/%s/%s/%d" % (repo, "issues" if tool == "github_issue" else "pulls", n)), "item")
        if tool == "github_issue" and item.get("pull_request"):
            raise GitHubError(400, "#%d is a pull request - read it with github_pr" % n)
        out = _shape(item)
        rows = _pages(gh, "/repos/%s/issues/%d/comments" % (repo, n), {}, NOTE_PAGES)
        out["comments"] = [_comment(c) for c in rows]
        if len(rows) >= NOTE_PAGES * INBOX_PER_PAGE:
            out["comments_truncated"] = "%d comments or more - the newest may be missing" % (NOTE_PAGES * INBOX_PER_PAGE)
        if tool == "github_pr":
            out["review_comments"] = [dict(_comment(c), path=c.get("path"), line=c.get("line") or c.get("original_line"),
                                           in_reply_to=c.get("in_reply_to_id"))
                                      for c in _pages(gh, "/repos/%s/pulls/%d/comments" % (repo, n), {}, NOTE_PAGES)]
            out["reviews"] = [{"id": r.get("id"), "author": _login(r.get("user")), "state": r.get("state"),
                               "body": r.get("body") or "", "submitted_at": r.get("submitted_at")}
                              for r in _pages(gh, "/repos/%s/pulls/%d/reviews" % (repo, n), {}, 2)
                              if r.get("body") or r.get("state") != "COMMENTED"]
    elif tool == "github_issues":
        page = max(1, _whole(args.get("page") or 1, "page"))
        state = {"opened": "open", "open": "open", "closed": "closed", "all": "all"}.get(args.get("state") or "open")
        if not state:
            raise GitHubError(400, "state must be open, closed or all")
        if args.get("search"):
            q = "repo:%s is:issue %s %s" % (repo, "" if state == "all" else "is:" + state,
                                            _one_line(str(args["search"])[:200], "search"))
            r = _dict(gh._req("GET", "/search/issues", {"q": q, "per_page": per_page, "page": page}), "search")
            rows = _list(r.get("items") or [], "search")
        else:
            labels = _labels(args.get("labels"))
            rows = _list(gh._req("GET", "/repos/%s/issues" % repo, {"state": state, "labels": labels,
                                                                   "per_page": per_page, "page": page}), "issues")
        out = {"issues": [_row(r) for r in rows if not r.get("pull_request")], "page": page}
        if len(rows) >= per_page:
            out["next_page"] = page + 1
    elif tool == "github_prs":
        state = {"opened": "open", "open": "open", "closed": "closed", "merged": "closed", "all": "all"}.get(
            args.get("state") or "open")
        if not state:
            raise GitHubError(400, "state must be open, closed or all")
        rows = _list(gh._req("GET", "/repos/%s/pulls" % repo, {"state": state, "per_page": per_page}), "pulls")
        out = {"pull_requests": [_row(r) for r in rows]}
    elif tool == "github_pr_files":
        n = _num(args.get("number"))
        rows = _pages(gh, "/repos/%s/pulls/%d/files" % (repo, n), {}, FILE_PAGES)
        files = []
        for f in rows:
            paths = [f.get("previous_filename") or "", f.get("filename") or ""]
            if any(x and hidden(x) for x in paths):
                files.append({"old_path": paths[0] or paths[1], "new_path": paths[1],
                              "diff": "[withheld: matches the isolation policy]"})
            else:
                files.append({"old_path": paths[0] or paths[1], "new_path": paths[1], "status": f.get("status"),
                              "diff": f.get("patch") if "patch" in f else "[no text diff: binary or too large]"})
        out = {"files": files}
        if len(rows) >= FILE_PAGES * INBOX_PER_PAGE:
            out["truncated"] = True
    elif tool == "github_runs":
        r = _dict(gh._req("GET", "/repos/%s/actions/runs" % repo,
                          {"branch": args.get("branch") or args.get("ref"), "per_page": per_page}), "runs")
        out = {"runs": [dict({k: x.get(k) for k in RUN_KEYS}, pipeline_status=run_status(x))
                        for x in _list(r.get("workflow_runs") or [], "runs")]}
    elif tool == "github_run_jobs":
        rid = _num(args.get("run_id"))
        rows = _pages(gh, "/repos/%s/actions/runs/%d/jobs" % (repo, rid), {}, 2, key="jobs")
        out = {"jobs": [{"id": j.get("id"), "name": j.get("name"), "status": j.get("status"),
                         "conclusion": j.get("conclusion"), "web_url": j.get("html_url"),
                         "failed_steps": [s.get("name") for s in j.get("steps") or []
                                          if isinstance(s, dict) and s.get("conclusion") == "failure"]}
                        for j in rows]}
    else:
        raise GitHubError(400, "unknown GitHub tool %r" % tool)
    return _scrub_obj(out) if scrub else out


# ------------------------------------------------------------------ the GUI's CI state and PR (9.7 #7)
# For the HUMAN's GUI only, never through `call` / TOOLS - shaped like gitlab.py's helpers.
PIPELINE_KEYS = ("id", "status", "ref", "sha", "web_url", "updated_at")
MR_ROW_KEYS = ("iid", "title", "state", "web_url", "source_branch", "target_branch")
RUNNING = ("queued", "in_progress", "waiting", "requested", "pending")


def run_status(run: dict) -> str:
    """A workflow run's state in GitLab's pipeline words (the GUI's CI badge and feed speak those)."""
    st = run.get("status")
    if st in RUNNING:
        return "pending" if st in ("queued", "requested", "pending") else "running"
    return {"success": "success", "neutral": "success", "failure": "failed", "timed_out": "failed",
            "startup_failure": "failed", "cancelled": "canceled", "stale": "canceled", "skipped": "skipped",
            "action_required": "manual"}.get(run.get("conclusion") or "", "failed" if st == "completed" else str(st))


def latest_pipeline(gh: GitHub, repo: str, ref: str, allowed: list[str]) -> dict | None:
    """The newest commit's workflow runs on `ref` as ONE pipeline (a push starts one run per workflow):
    failed if any failed, running while any runs, else the newest's state. id = the newest run's."""
    repo = _scoped(repo, allowed)
    r = _dict(gh._req("GET", "/repos/%s/actions/runs" % repo, {"branch": ref, "per_page": 20}) or {}, "runs")
    runs = _list(r.get("workflow_runs") or [], "runs")
    if not runs:
        return None
    head = runs[0]
    same = [x for x in runs if x.get("head_sha") == head.get("head_sha")]
    sts = [run_status(x) for x in same]
    status = next((s for s in ("failed", "running", "pending", "manual", "canceled") if s in sts), sts[0])
    return _scrub_obj({"id": head.get("id"), "status": status, "ref": head.get("head_branch"), "sha": head.get("head_sha"),
                       "web_url": head.get("html_url") if len(same) == 1 else
                       "https://%s/%s/actions?query=branch%%3A%s" % (gh.host, repo, urllib.parse.quote(ref, safe="")),
                       "updated_at": max(str(x.get("updated_at") or "") for x in same) or None})


def _mr_row(d: dict) -> dict:
    s = _shape(d)
    return {k: s.get(k) for k in MR_ROW_KEYS}


def open_mr(gh: GitHub, repo: str, branch: str, allowed: list[str]) -> dict | None:
    """The open pull request from `branch` (in the same repository - forks are not looked at)."""
    repo = _scoped(repo, allowed)
    rows = _list(gh._req("GET", "/repos/%s/pulls" % repo, {"head": "%s:%s" % (repo.split("/")[0], branch),
                                                           "state": "open", "per_page": 1}) or [], "pulls")
    return _scrub_obj(_mr_row(rows[0])) if rows else None


def default_branch(gh: GitHub, repo: str, allowed: list[str]) -> str | None:
    repo = _scoped(repo, allowed)
    return (gh._req("GET", "/repos/%s" % repo) or {}).get("default_branch") or None


def create_mr(gh: GitHub, repo: str, allowed: list[str], source: str, target: str, title: str, description: str,
              draft: bool = False) -> dict:
    """Open a pull request (the GUI has already asked the human)."""
    repo = _scoped(repo, allowed)
    title = title.strip()
    if not title or len(title) > 256:
        raise GitHubError(400, "title must be 1..256 characters")
    if len(description) > 65000:
        raise GitHubError(400, "description is too long")
    pr = gh._req("POST", "/repos/%s/pulls" % repo, body={"head": _branch(source, "source_branch"),
                                                         "base": _branch(target, "target_branch"),
                                                         "title": title, "body": description,
                                                         "draft": bool(draft)}) or {}
    return _mr_row(pr)


# ------------------------------------------------------------------ the GUI's inbox (9.8, W23)
# GUI-only like gitlab.todos & co.: never in TOOLS. Polling keeps to the cheap endpoints
# (notifications with If-Modified-Since, /issues); the search API (30 req/min) only for the
# pull requests asking for my review and my own open ones.
_ME: dict[str, str] = {}


def me(gh: GitHub) -> str | None:
    if not _ME.get(gh.tok or ""):
        try:
            _ME[gh.tok or ""] = str(_dict(gh._req("GET", "/user") or {}, "/user").get("login") or "")
        except GitHubError:
            return None
    return _ME[gh.tok or ""] or None


def notifications(gh: GitHub, since: str = "") -> tuple[list | None, str]:
    """(unread notification threads, Last-Modified) - (None, since) when nothing changed (304)."""
    raw, _, hdrs = gh._open("GET", gh.base + "/notifications", {"per_page": 50},
                            headers={"If-Modified-Since": since} if since else None)
    if hdrs.get("status") == 304:
        return None, since
    try:
        rows = json.loads(raw.decode() or "[]")
    except ValueError:
        raise GitHubError(502, "GitHub /notifications: not JSON") from None
    return _list(rows, "/notifications"), str(hdrs.get("Last-Modified") or "")


def assigned_issues(gh: GitHub) -> list:
    """Open issues and PRs assigned to me (a PR has `pull_request`)."""
    return _pages(gh, "/issues", {"filter": "assigned", "state": "open"})


def _search(gh: GitHub, q: str) -> list:
    r = _dict(gh._req("GET", "/search/issues", {"q": q, "per_page": INBOX_PER_PAGE}) or {}, "search")
    return _list(r.get("items") or [], "search")


def review_requests(gh: GitHub) -> list:
    return _search(gh, "is:open is:pr archived:false review-requested:@me")


def own_prs(gh: GitHub) -> list:
    return _search(gh, "is:open is:pr archived:false author:@me")


REPO_URL_RX = re.compile(r"/repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/|$)")


def repo_of(d: dict) -> str | None:
    """owner/repo of an issue / search hit / notification."""
    rep = d.get("repository")
    if isinstance(rep, dict) and rep.get("full_name"):
        return str(rep["full_name"])
    m = REPO_URL_RX.search(str(d.get("repository_url") or d.get("url") or ""))
    return m.group(1) if m else None


def mark_notification_read(gh: GitHub, thread_id) -> None:
    gh._req("PATCH", "/notifications/threads/%d" % _num(thread_id))


def recent_notes(gh: GitHub, repo: str, kind: str, number: int, n: int = 50) -> list[dict]:
    """The newest `n` comments, newest first: {id, user (login), created_at} (GitHub lists ascending)."""
    rows = _pages(gh, "/repos/%s/issues/%d/comments" % (_repo(repo), _num(number)), {}, INBOX_PAGES)
    return [{"id": r.get("id"), "user": (r.get("user") or {}).get("login"), "created_at": r.get("created_at")}
            for r in reversed(rows[-n:]) if isinstance(r.get("id"), int)]


def post_note(gh: GitHub, repo: str, kind: str, number: int, body: str, allowed: list[str]) -> dict:
    """A comment from the GUI (the human wrote / confirmed it there); issue and PR alike."""
    repo = _scoped(repo, allowed)
    body = str(body or "")
    if not body.strip() or len(body) > 65000:
        raise GitHubError(400, "body must be 1..65000 characters")
    c = gh._req("POST", "/repos/%s/issues/%d/comments" % (repo, _num(number)), body={"body": body}) or {}
    return {"note_id": c.get("id")}


def item_preview(gh: GitHub, repo: str, kind: str, number: int, allowed: list[str], scrub: bool = True) -> dict:
    """gitlab.item_preview's shape: metadata, description, the newest <= 100 comments ascending."""
    repo = _scoped(repo, allowed)
    n = _num(number)
    pr = kind in ("mr", "pr")
    d = _dict(gh._req("GET", "/repos/%s/%s/%d" % (repo, "pulls" if pr else "issues", n)) or {}, "item")
    rows = _pages(gh, "/repos/%s/issues/%d/comments" % (repo, n), {}, INBOX_PAGES)[-100:]
    s = _shape(d)
    out = {k: s[k] for k in ("iid", "title", "state", "web_url", "author", "created_at", "assignees", "labels") if k in s}
    out["type"] = "mr" if pr else "issue"
    out["forge"] = "github"
    out["description"] = str(d.get("body") or "")[:PREVIEW_MAX]
    out["notes"] = [{"id": r.get("id"), "author": _login(r.get("user")), "user": (r.get("user") or {}).get("login"),
                     "body": str(r.get("body") or "")[:PREVIEW_MAX], "created_at": r.get("created_at")} for r in rows]
    if pr:
        pipe = None
        try:
            pipe = latest_pipeline(gh, repo, s.get("source_branch") or "", allowed) if s.get("source_branch") else None
        except GitHubError:
            pass
        out["mr"] = {"source_branch": s.get("source_branch"), "target_branch": s.get("target_branch"),
                     "merge_status": d.get("mergeable_state"), "pipeline": (pipe or {}).get("status"),
                     "draft": bool(d.get("draft"))}
    return _scrub_obj(out) if scrub else out
