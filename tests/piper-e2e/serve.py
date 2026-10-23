#!/usr/bin/env python3
"""tests/piper-e2e/serve.py - a piperd + ptyhost from this checkout with demo data, for GUI testing (gui.py, manual passes).

run.py's harness (fake-hole with HOME=<work>/fakehome, PIPER_HOME under <work>, FAKE_IJHTTP, a logging stand-in
`open`, a git daemon) plus:
  - a fake GitLab over HTTP on port+2 (user, assigned issues, notes, /issues/:iid/discussions, jobs, pipelines, uploads);
    piperd's GitLab client is pointed at it, the token is a fake one
  - inbox data: to-dos (addressed, build failed), own MRs, grp/shop with two discovered checkouts; a second wave
    (mention, three reasons, cannot merge, new comments) with `curl -X POST 127.0.0.1:<port+2>/api/v4/_fake/news`
  - demo projects: grp/demo (history, a branch, hidden + tracked-hidden files, odd file names, a .http file,
    working-tree changes; mapped to GitLab grp/demo), demo/hidden (planted files hidden from sessions),
    demo/http (.http with dev / test / prod), demo/plain; grp/demo has an observability project (Production view on,
    no automatic morning check; its last report is 3 days old: new / known / muted items, mapping suggestions)
  - sessions: grp/demo and demo/hidden ready with a conversation, demo/plain exited with a transcript; token usage on
    six of the last ten days (demo/hidden); audit records: a hidden file read 10x, git_run actions, a push, prod HTTP
  - F05: grp/demo's conversation has markdown (headings, bold, code, a list, a path:line) and tool calls with their
    results; the session works (UserPromptSubmit), so the composer holds what is sent; `touch <work>/f05-stop`
    ends the turn (Stop) and the held message goes
  - F22: a Mattermost incoming webhook on the fake server: http://127.0.0.1:<port+2>/hooks/ok answers 200, any other
    /hooks/… 404 (the status line's error)
  - F25: --welcome = a new install (the stepper) with Docker down until <work>/fake-docker/info.txt exists
  - optional database connections: SERVE_MYSQL / SERVE_PG = host:port:user:password:database (dbseed/start.sh
    containers), SERVE_DBVENV = a driver venv to copy into PIPER_HOME/dbvenv (else the DB view offers the install),
    SERVE_DBDEMO=1 = three registered connections without a server (dev direct, test over ssh without a password,
    prod over a tunnel) for the connection list
Prints `SERVE <gui url with token>` and writes <work>/serve.json, then stays up until SIGTERM / SIGINT; tears down
by PID only (piperd, ptyhost, git daemon) and removes <work> unless --keep or --work was given.

Run in the test sandbox:
    python3 scripts/piper-sandbox.py -- python3 tests/piper-e2e/serve.py [--port 17961] [--work DIR] [--keep]
Ports used: port (piperd), +1 (git daemon), +2 (fake GitLab), +7 (http env host, nothing listens).
Never the live piperd (:17895) or ~/.piper.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--port", type=int, default=17961)
ap.add_argument("--work", default="", help="work dir (kept); default: a temp dir, removed at the end")
ap.add_argument("--keep", action="store_true", help="keep the temp work dir (logs)")
ap.add_argument("--security-tree", action="store_true", help="exercise the effective Security tree instead of legacy policy")
ap.add_argument("--welcome", action="store_true", help="F25: a new install (features.json onboarded=false): the welcome with its stepper; Docker is down until <work>/fake-docker/info.txt exists (the re-check then clears the blocker)")
OPT = ap.parse_args()
if OPT.port == 17895:
    sys.exit("serve.py: 17895 is the live piperd's port - pick another (--port)")
WORKDIR = os.path.abspath(OPT.work) if OPT.work else tempfile.mkdtemp(prefix="piper-serve-")
# run.py parses its own argv at import time
sys.argv = [sys.argv[0], "--port", str(OPT.port), "--work", WORKDIR, "--keep"]
sys.path.insert(0, HERE)
import run as R  # noqa: E402

if OPT.security_tree:
    R.ENV["PIPER_SECURITY_TREE"] = "1"
R.SECTIONS.clear()
GLPORT = OPT.port + 2
GL_HOST = "gitlab.example.test"
STOP = threading.Event()


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


ODD = "‮evil‬ \U0001F468‍\U0001F469‍\U0001F467 <script>x</script> $(id)"   # bidi, ZWJ, markup
ISSUES: list = []
MRS: list = []   # the MRs the GUI created (POST .../merge_requests)
JOBS: dict = {}


TODOS: list = []      # pending to-dos (GET /todos)
OWN_MRS: list = []    # my open MRs (GET /merge_requests)
OTHER: list = []      # issues / MRs only a to-do points at (not assigned)
EXTRA_NOTES: dict = {}   # iid -> notes by others added by POST /_fake/news


def _obj(gl: str, kind: str, iid: int, title: str, age: float, **kw) -> dict:
    sep, coll = ("#", "work_items") if kind == "issue" else ("!", "merge_requests")
    d = {"id": 9000 + iid, "iid": iid, "title": title, "state": "opened", "labels": ["bug"],
         "author": {"username": "bob", "name": "A K"}, "assignees": [{"username": "alice"}],
         "updated_at": iso(time.time() - age), "created_at": iso(time.time() - 86400), "due_date": None,
         "milestone": None, "user_notes_count": 2, "project_id": 1, "description": "issue body " + ODD,
         "web_url": "https://%s/%s/-/%s/%d" % (GL_HOST, gl, coll, iid), "references": {"full": "%s%s%d" % (gl, sep, iid)}}
    d.update(kw)
    return d


def _issue(iid: int, title: str, gl: str = "grp/demo", **kw) -> dict:
    d = _obj(gl, "issue", iid, title, iid * 600, **kw)
    ISSUES.append(d)
    return d


def _todo(action: str, target: dict, kind: str = "issue", body: str = "") -> None:
    gl = target["references"]["full"].rsplit("#" if kind == "issue" else "!", 1)[0]
    TODOS.append({"id": 700 + len(TODOS), "action_name": action, "target_type": "Issue" if kind == "issue" else "MergeRequest",
                  "target": target, "project": {"path_with_namespace": gl}, "author": {"username": "bob", "name": "A K"},
                  "created_at": iso(time.time() - 3600), "target_url": target["web_url"] + "#note_4241", "body": body})


_issue(101, "Plain issue: PDF invoice fails")
_issue(102, "Emoji \U0001F389 and " + ODD, due_date="2026-10-01")
_issue(103, "L" * 200)
# F15 (9.6x inbox rows): a waiting to-do, an own MR whose build failed in a project without a checkout, an issue of
# a project with two discovered checkouts (Add checkout… chooses), more waves through POST /_fake/news
_todo("directly_addressed", _obj("grp/demo", "issue", 105, "Configurator", 4 * 86400), body="@alice can you look?")
OWN_MRS.append(_obj("grp/other", "mr", 88, "Import feed from the ERP", 86400, source_branch="feature/erp", target_branch="main",
                    merge_status="can_be_merged", head_pipeline={"status": "failed"}))
_todo("build_failed", OWN_MRS[-1], "mr")
_issue(207, "Shop: cart fails when VAT is recalculated", gl="grp/shop", labels=["bug", "shop", "prio::high", "customer"])


def fake_news() -> None:
    """POST /_fake/news: what arrives after the baseline - a mention, an issue with three reasons, an MR that cannot
    merge, two new comments on #101 (the GUI shows them as New)"""
    t = time.time()
    _todo("mentioned", _obj("grp/demo", "issue", 106, "Worktree support in the dev tool", 8 * 3600), body="@alice fyi")
    i107 = _issue(107, "Background jobs drop the admin → search connection")
    _todo("mentioned", i107)
    _todo("directly_addressed", i107, body="@alice please")
    mr = _obj("grp/demo", "mr", 12, "Refactor the invoice export", 7200, source_branch="feat/demo", target_branch="main",
              merge_status="cannot_be_merged", head_pipeline={"status": "success"})
    OWN_MRS.append(mr)
    _todo("unmergeable", mr, "mr")
    for d in ISSUES:
        if d["iid"] == 101:
            d["user_notes_count"] += 2
            d["updated_at"] = iso(t)
    EXTRA_NOTES[101] = EXTRA_NOTES.get(101, []) + [
        {"id": 4300 + k, "body": "new comment %d from a colleague" % k, "author": {"username": "bob", "name": "A"},
         "created_at": iso(t), "system": False} for k in range(2)]
for _jid, (_name, _st, _ref) in {501: ("deploy-manual", "manual", "feature/x"), 502: ("test-failed", "failed", "feature/x"),
                                 503: ("deploy-prod", "manual", "main"), 504: ("running", "running", "feature/x")}.items():
    JOBS[_jid] = {"id": _jid, "name": _name, "stage": "deploy", "status": _st, "ref": _ref, "pipeline": {"id": 77},
                  "web_url": "https://%s/grp/demo/-/jobs/%d" % (GL_HOST, _jid), "failure_reason": None}


class FakeGitLab(http.server.BaseHTTPRequestHandler):
    """The REST paths piperd's inbox, issue view and job actions use; anything else is a 404 like GitLab's."""

    def log_message(self, *a):
        pass

    def _send(self, code: int, obj) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _route(self, method: str) -> None:
        u = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(u.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if path.startswith("/api/graphql"):
            return self._send(200, {"data": {}})
        if path.startswith("/hooks/ok") and method == "POST":   # F22: a Mattermost incoming webhook that accepts
            return self._send(200, {"ok": True})
        path = path[len("/api/v4"):]
        page = int(q.get("page") or 1)
        if path == "/user":
            return self._send(200, {"username": "alice", "name": "S K", "id": 7})
        if path == "/_fake/news" and method == "POST":
            fake_news()
            return self._send(200, {"ok": True})
        if path == "/todos":
            return self._send(200, TODOS if page == 1 else [])
        if path == "/merge_requests":
            return self._send(200, OWN_MRS if page == 1 else [])
        if path == "/issues":
            return self._send(200, ISSUES if page == 1 else [])
        rest = path.split("/")[2:] if path.startswith("/projects/") else []
        if not rest:
            return self._send(404, {"message": "404 Not Found"})
        if rest[-1] == "uploads" and method == "POST":
            h = uuid.uuid4().hex
            return self._send(201, {"alt": "f", "url": "/uploads/%s/f.png" % h, "markdown": "![f](/uploads/%s/f.png)" % h})
        if "jobs" in rest and rest.index("jobs") + 1 < len(rest):
            jid = int(rest[rest.index("jobs") + 1])
            if jid not in JOBS:
                return self._send(404, {"message": "404 Job Not Found"})
            if method == "POST":
                JOBS[jid]["status"] = "pending"
            return self._send(200, JOBS[jid])
        if rest[-1] == "jobs":
            return self._send(200, list(JOBS.values()))
        if rest[-1] == "pipelines":
            return self._send(200, [{"id": 77, "status": "manual", "ref": "feature/x", "sha": "abc",
                                     "web_url": "https://%s/grp/demo/-/pipelines/77" % GL_HOST}])
        if rest[-1] == "notes":
            if method == "POST":
                return self._send(201, {"id": 4242, "body": json.loads(body or b"{}").get("body"), "author": {"username": "alice"}})
            extra = EXTRA_NOTES.get(int(rest[-2]), []) if rest[-2].isdigit() and rest[-3] == "issues" else []
            return self._send(200, extra[::-1] + [{"id": 4241, "body": "a note " + ODD, "author": {"username": "bob", "name": "A"},
                                                   "created_at": iso(time.time()), "system": False}] if page == 1 else [])
        if rest[-1] == "discussions":
            return self._send(200, [] if page > 1 else [{"id": "abcdef0123456789", "notes": [
                {"id": 4241, "body": "a note " + ODD, "author": {"username": "bob", "name": "A"},
                 "created_at": iso(time.time()), "system": False},
                {"id": 4243, "body": "reply " + "R" * 300, "author": {"username": "alice", "name": "S"},
                 "created_at": iso(time.time()), "system": False}]}])
        if len(rest) >= 3 and rest[-2] == "merge_requests" and rest[-1].isdigit():
            for d in OWN_MRS:
                if str(d["iid"]) == rest[-1]:
                    return self._send(200, d)
            return self._send(404, {"message": "404 Merge Request Not Found"})
        if len(rest) >= 3 and rest[-2] == "issues":
            for d in ISSUES + [t["target"] for t in TODOS if t["target_type"] == "Issue"]:
                if str(d["iid"]) == rest[-1]:
                    return self._send(200, d)
            return self._send(404, {"message": "404 Issue Not Found"})
        if rest[-1] == "issues":
            return self._send(200, ISSUES if page == 1 else [])
        if rest[-1] == "merge_requests" and method == "POST":   # a created MR answers like GitLab: number + link
            mr = json.loads(body or b"{}")
            MRS.append(mr)
            return self._send(201, {"iid": len(MRS), "title": mr.get("title"), "state": "opened",
                                    "web_url": "http://127.0.0.1/%s/-/merge_requests/%d" % ("/".join(rest[:-1]), len(MRS))})
        if rest[-1] == "merge_requests":   # F27: the MRs the GUI created, so the CI row shows the MR afterwards
            gl = "/".join(rest[:-1])
            return self._send(200, [dict(iid=i + 1, title=m.get("title"), state="opened", source_branch=m.get("source_branch"),
                                         target_branch=m.get("target_branch"), web_url="http://127.0.0.1/%s/-/merge_requests/%d" % (gl, i + 1))
                                    for i, m in enumerate(MRS) if q.get("source_branch") in (None, m.get("source_branch"))] if page == 1 else [])
        if rest[-1] in ("milestones", "links"):
            return self._send(200, [])
        if len(rest) == 1:
            return self._send(200, {"id": 1, "default_branch": "main", "path_with_namespace": rest[0]})
        return self._send(404, {"message": "404 Not Found"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")


# piperd as run.py starts it, with the attestation stubbed (no Docker) and the GitLab client on the fake above
PIPERD_WRAPPER = """import sys
sys.path.insert(0, %r)
import guard
guard.attest = lambda *a, **k: {"ok": True, "failures": [], "checks": 3, "resealed": {}}
import gitlab
_init = gitlab.GitLab.__init__
def _i(self, host, tok, opener=None):
    _init(self, host, tok, opener)
    self.base = "http://127.0.0.1:%d/api/v4"
gitlab.GitLab.__init__ = _i
import piperd
sys.exit(piperd.main())
"""


def _write(root: str, rel: str, data, mode: str = "w") -> None:
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as f:
        f.write(data)


def demo_project() -> str:
    """grp/demo: history, a branch, hidden (and one tracked hidden) files, odd names, a .http file, local changes."""
    files = {
        "README.md": "# Demo\n\nSee [App](src/main/java/App.java).\n\n```sh\necho hi\n```\n",
        "src/main/java/App.java": "package demo;\n\npublic class App {\n    // TODO fix\n    public void run() {\n"
                                  "        System.out.println(\"hi\");\n    }\n}\n",
        "src/main/java/Util.java": "package demo;\n\nclass Util {\n    static int add(int a, int b) { return a + b; }\n}\n",
        "web/app.ts": "export function hello(name: string): string {\n  return `hi ${name}`;\n}\n",
        "api/demo.http": "### ping\nGET https://{{host}}/ping\n\n### post\nPOST https://{{host}}/x\n"
                         "Content-Type: application/json\n\n{\"a\": 1}\n",
        "http-client.env.json": json.dumps({"dev": {"host": "localhost:1"}, "test": {"host": "test.example.org"},
                                            "prod": {"host": "prod.example.com"}}) + "\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": ["config/secrets", "http-client.private.env.json",
                                                                 "secret-notes.md"]}}, indent=2) + "\n",
        "config/secrets/db.properties": "password=PLANTED_DEMO\n",
        "secret-notes.md": "# hidden notes PLANTED_NOTE\n",
        "docs/" + "/".join("l%d" % i for i in range(15)) + "/deep.md": "deep file\n",
        "docs/<script>alert(1)</script>.md": "xss name\n",
        "docs/emoji-\U0001F389.md": "emoji name\n",
        "docs/fam-\U0001F468‍\U0001F469‍\U0001F467.md": "emoji zwj name\n",
        "docs/rtl-‮txt.md": "x ‪ lre ‬ and ⁨ fsi ⁩ and ‮ rlo\n",
        "node_modules/pkg/index.js": "module.exports = 1;\n",
        "target/classes/App.class": "x\n",
    }
    root = R.make_project("grp/demo", files,
                          commit=[k for k in files if not k.startswith(("config/", "secret-", "node_modules", "target"))])
    for i in range(1, 4):
        _write(root, "src/main/java/App.java", "// rev %d\n" % i, "a")
        R.git(root, "commit", "-qam", "rev %d of App" % i)
    R.git(root, "push", "-q", "origin", "main", check_rc=False)
    _write(root, "web/app.ts", "// odd author\n", "a")
    R.git(root, "-c", "user.name=Eve ‮live", "-c", "user.email=eve@example.test", "commit", "-qam",
          "fix ‮exe.txt \U0001F468‍\U0001F469‍\U0001F467 ok")
    R.git(root, "switch", "-qc", "feat/demo")
    _write(root, ".gitignore", "node_modules/\ntarget/\n")
    _write(root, "big.log", "x" * (3 << 20))
    _write(root, "blob.bin", bytes(range(256)) * 10, "wb")
    _write(root, "docs/pic.png", base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8"
                                                  "jx0gAAAABJRU5ErkJggg=="), "wb")
    os.symlink("src/main/java/App.java", os.path.join(root, "app-link.java"))
    R.git(root, "add", "-f", "secret-notes.md")   # a TRACKED file hidden from sessions
    R.git(root, "commit", "-qm", "track the notes")
    _write(root, "secret-notes.md", "PLANTED_NOTE_W2\n", "a")
    _write(root, "docs/café-nfd.md", "nfd name\n")
    _write(root, "src/main/java/Util.java", "// changed\n", "a")
    _write(root, "NEWFILE.txt", "untracked\n")
    return root


def setup_projects() -> None:
    plain = {"README.md": "# plain\nhello\n", "src/App.java": "class App { void run() {} }\n", ".hole/settings.json": "{}\n"}
    R.PROJECTS["grp/demo"] = demo_project()
    R.PROJECTS["demo/hidden"] = R.make_project("demo/hidden", dict(plain, **R.PLANTED, **{
        ".hole/settings.json": json.dumps({"files": {"exclude": R.LEAK_EXCLUDES}}, indent=2) + "\n"}),
        commit=["README.md", "src/App.java", ".hole/settings.json"])
    R.PROJECTS["demo/http"] = R.make_project("demo/http", dict(plain, **{
        "http-client/ping.http": "### ping\nGET {{host}}/ping\nAuthorization: Bearer {{token}}\n",
        "http-client/http-client.env.json": json.dumps({e: {"host": "http://127.0.0.1:%d" % (OPT.port + 7)}
                                                       for e in ("dev", "test", "prod")}) + "\n",
        "http-client/http-client.private.env.json": json.dumps({e: {"token": "PLANTED_HTTP_%s" % e.upper()}
                                                               for e in ("dev", "test", "prod")}) + "\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": ["http-client/http-client.env.json",
                                                                 "http-client/http-client.private.env.json"]}}) + "\n"}),
        commit=["README.md", "src/App.java", ".hole/settings.json", "http-client/ping.http"])
    R.PROJECTS["demo/allow"] = R.make_project("demo/allow", {"README.md": "# Allow fixture\n", "fixture.key": "fake-test-only"})
    R.PROJECTS["demo/plain"] = R.make_project("demo/plain", dict(plain))
    # F15: two discovered checkouts of GitLab grp/shop (not in projects.json): its inbox row offers Add checkout…
    for k in ("shop/webstore", "shop/webstore-old"):
        R.git(R.make_project(k, {"README.md": "# shop\n"}, remote=False), "remote", "add", "origin",
              "git@%s:grp/shop.git" % GL_HOST)
    # grp/demo: GitLab grp/demo and an observability project (the Production view is on; its morning check never
    # starts on its own here: PIPER_PROD_TICK=0)
    with open(os.path.join(R.PH, "projects.json"), "w") as f:
        json.dump({"projects": {k: dict({"path": v}, **({"gitlab": ["grp/demo"]} if k == "grp/demo" else {}))
                                for k, v in R.PROJECTS.items()}, "obs": {"grp/demo": "demo-obs"}}, f)


def setup_piper_home() -> None:
    R.setup_home()
    R.ENV["PIPER_PROD_TICK"] = "0"
    with open(os.path.join(R.BIN, "piperd_e2e.py"), "w") as f:   # replaces run.py's wrapper (its in-process GitLab)
        f.write(PIPERD_WRAPPER % (R.PIPER, GLPORT))
    with open(os.path.join(R.PH, "policy.json"), "w") as f:
        json.dump({"gitlab_extra_tools": True, "gitlab_host": GL_HOST, "protected_branches": ["main"], "max_sessions": 10}, f)
    os.makedirs(os.path.join(R.PH, "feed"), exist_ok=True)   # F21: the Global agent's chat (the Feed module) on
    with open(os.path.join(R.PH, "feed", "config.json"), "w") as f:
        json.dump({"enabled": True}, f)
    if OPT.welcome:   # F25: the first open - run.py writes an onboarded features.json
        with open(os.path.join(R.PH, "features.json"), "w") as f:
            json.dump({"base": "all", "onboarded": False, "modules": {}}, f)
    with open(os.path.join(R.PH, "gitlab-token"), "w") as f:
        f.write("fake-gitlab-token-serve\n")
    os.chmod(os.path.join(R.PH, "gitlab-token"), 0o600)
    # an external CLI conversation in the "personal" ~/.claude (Resume conversation)
    nat, demo = str(uuid.uuid4()), R.PROJECTS["grp/demo"]
    _transcript(os.path.join(R.REAL, ".claude"), demo, nat, ("External CLI: refactor the Util class", "Util refactored."))
    setup_db()
    setup_prod()


def _prod_key(project: str, kind: str, fp: str) -> str:
    """prodcheck.item_key (copied: importing prodcheck here would bind it to this process's PIPER_HOME)."""
    import hashlib
    import re
    s = " ".join(re.sub(r"(?<=\S):\d+\b", "", fp.lower()).split())
    return hashlib.sha1(("%s|%s|%s" % (project, kind, s)).encode()).hexdigest()[:16]


def setup_prod() -> None:
    """F16: the last morning check is 3 days old (stale) - new errors, an alert, a rate jump, known ones from
    an older report, one muted forever, and mapping suggestions for two unmapped projects."""
    from datetime import date, timedelta
    obs, now = os.path.join(R.PH, "obs"), time.time()
    os.makedirs(obs, mode=0o700, exist_ok=True)
    g = "https://grafana.example.test/explore?left=demo-obs"
    rows = [  # kind, title, fingerprint, count, first_seen, trend, new, url
        ("alert", "HighErrorRate on checkout", "alert HighErrorRate", 3, "Oct 16 05:10", "firing 2 h", True, g),
        ("error", "IllegalArgumentException - unknown locale in the request", "IllegalArgumentException @ LocaleResolver", 701, "Oct 15", "↑ 3× vs last week", True, g),
        ("error", "Malformed HTML in a product description", "ParseException @ HtmlSanitizer", 312, "Oct 14", "", True, ""),
        ("error", "Query filter compares a column with NULL", "IllegalStateException @ QueryBuilder", 141, "Oct 16", "", True, g),
        ("rate", "5xx rate on /api/cart", "5xx /api/cart", 86, "", "2.1 % → 6.4 %", True, g),
        ("error", "Scheduled job missed its start time", "Missed start @ JobScheduler", 86, "Oct 13", "", True, ""),
        ("error", "NullPointerException in the file listing", "NPE @ FileListing", 22, "Sep 30", "steady", False, g),
        ("error", "ConcurrentModificationException in template", "CME @ TemplateModel", 13, "Sep 28", "", False, ""),
        ("error", "Search reindex aborted: the field mapping changed", "Reindex aborted @ SearchIndexer", 24, "Oct 01", "", True, ""),
    ]
    items = [{"project": "grp/demo", "obs": "demo-obs", "kind": k, "title": t, "fingerprint": fp, "count": c,
              "first_seen": fs, "trend": tr, "service": "webstore", "detail": "%s\n  at %s" % (t, fp), "url": u,
              "key": _prod_key("grp/demo", k, fp), "new": n} for k, t, fp, c, fs, tr, n, u in rows]
    day = date.today() - timedelta(days=3)
    old = [dict(i, new=True) for i in items if not i["new"]]
    with open(os.path.join(obs, "%s.json" % (day - timedelta(days=2)).isoformat()), "w") as f:
        json.dump({"date": (day - timedelta(days=2)).isoformat(), "at": now - 5 * 86400, "session": "x", "items": old,
                   "errors": []}, f)
    with open(os.path.join(obs, "%s.json" % day.isoformat()), "w") as f:
        json.dump({"date": day.isoformat(), "at": now - 3 * 86400 - 1800, "session": "x", "items": items,
                   "errors": []}, f)
    with open(os.path.join(obs, "last-run"), "w") as f:
        f.write("%s\n" % day.isoformat())
    with open(os.path.join(obs, "mutes.json"), "w") as f:   # the indexing one: muted forever
        json.dump({items[-1]["key"]: 0}, f)
    with open(os.path.join(obs, "suggestions.json"), "w") as f:
        json.dump({"at": now, "suggestions": [{"project": p, "obs": p.replace("/", "-"), "note": "same name"}
                                              for p in ("demo/http", "demo/plain")]}, f)


def setup_db() -> None:
    conns, secrets = [], {}
    for eng, var, name in (("mysql", "SERVE_MYSQL", "grp/demo/dev/mysql"), ("postgres", "SERVE_PG", "grp/demo/dev/pg")):
        spec = os.environ.get(var)
        if not spec:
            continue
        host, port, user, pw, db = spec.split(":", 4)
        conns.append({"cls": "dev", "database": db, "engine": eng, "host": host, "name": name, "port": int(port),
                      "project": "grp/demo", "user": user})
        secrets[name] = pw
    # SERVE_DBDEMO=1: registered connections that need no server - the overview list (classes, reach, a missing
    # password), the context panel's section; opening one fails to connect (nothing listens on port+11)
    if os.environ.get("SERVE_DBDEMO"):
        dead = OPT.port + 11
        conns += [
            {"cls": "dev", "database": "shop", "engine": "mysql", "host": "127.0.0.1", "name": "grp/demo/dev/shop",
             "port": dead, "project": "grp/demo", "user": "shop"},
            {"cls": "test", "database": "shop_test", "engine": "mysql", "host": "db.internal", "name": "grp/demo/test/shop",
             "port": 3306, "project": "grp/demo", "user": "shop", "ssh": {"host": "bastion.example", "user": "projects"}},
            {"cls": "prod", "database": "acme", "engine": "postgres", "name": "demo/http/prod/acme",
             "project": "demo/http", "user": "reader", "tunnel": {"env": "prod", "target": "postgres"}},
        ]
        secrets["grp/demo/dev/shop"] = "e2e-dev-pw"
        secrets["demo/http/prod/acme"] = "e2e-prod-pw"
    if not conns:
        return
    os.makedirs(os.path.join(R.PH, "db"), exist_ok=True)
    with open(os.path.join(R.PH, "db", "connections.json"), "w") as f:
        json.dump({"always": [], "pins": {}, "connections": conns}, f, indent=1)
    # the secrets through Piper's own code, in a child with the harness's environment (PIPER_HOME = ours)
    code = "import sys, json; sys.path.insert(0, %r); import dbtools\nfor n, p in json.loads(sys.argv[1]).items(): " \
           "dbtools.write_secret(n, p)" % R.PIPER
    subprocess.run([sys.executable, "-c", code, json.dumps(secrets)], env=R.ENV, check=True)
    venv = os.environ.get("SERVE_DBVENV")
    if venv and os.path.isdir(venv):
        shutil.copytree(venv, os.path.join(R.PH, "dbvenv"), symlinks=True)


def _transcript(cfg_dir: str, cwd: str, conv: str, turns: tuple, title: str = "") -> None:
    d = os.path.join(cfg_dir, "projects", "-" + cwd.strip("/").replace("/", "-").replace(".", "-"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, conv + ".jsonl"), "w") as f:
        for role, text in zip(("user", "assistant"), turns):
            f.write(json.dumps({"type": role, "sessionId": conv, "cwd": cwd, "gitBranch": "main",
                                "timestamp": "2026-10-08T10:00:00Z",
                                "message": {"role": role, "content": [{"type": "text", "text": text}]}}) + "\n")
        if title:
            f.write(json.dumps({"type": "ai-title", "sessionId": conv, "aiTitle": title}) + "\n")


def seed_sessions() -> dict:
    sids = {}
    for key in ("grp/demo", "demo/hidden", "demo/plain"):
        sids[key] = R.ready_session(key, task="")
        conv = str(uuid.uuid4())
        R.hook(sids[key], "SessionStart", session_id=conv, source="startup")
        _transcript(os.path.join(R.sess_dir(sids[key]), "claude"), R.PROJECTS[key], conv,
                    ("Fix issue 1213 PDF in %s" % key, "PdfRenderer fixed in %s." % key),
                    title="PDF invoice repair %s %s" % (key, ODD))
    R.stop(sids["demo/plain"])
    seed_convo(sids["grp/demo"])
    seed_usage(sids["demo/hidden"])
    seed_audit(sids)
    seed_agent(sids)
    return sids


def seed_agent(sids: dict) -> None:
    """F21: the Global agent runs and started sessions through spawn_session (origin "advisor"): one done, one
    waiting, and a third whose card still waits in the queue."""
    adv = sids["advisor"] = R.ready_session("advisor")
    ok = lambda card: R.decide(card, True)   # noqa: E731
    started = []
    for title, task in (("Reproduce #512 on test", "Reproduce issue 512 on the test server."),
                        ("Fix OrderService.place()", "Fix OrderService.place() and add a regression test.")):
        r = R.tool(adv, "spawn_session", {"project": "grp/demo", "title": title, "task": task}, answer=ok)
        sid = (r.get("result") or {}).get("id") if isinstance(r.get("result"), dict) else None
        if not sid:
            print("seed_agent: spawn_session: %s" % json.dumps(r)[:300], flush=True)
            continue
        started.append(sid)
    if started:
        R.wait_state(started[0], ("ready", "waiting", "working", "attention", "blocked"), 20)
        R.stop(started[0])
    # the third waits for the human: its card stays in the queue (the call itself gives up quietly)
    threading.Thread(target=R.tool, args=(adv, "spawn_session", {"project": "demo/plain", "title": "Review CSV export MR",
                                                                 "task": "Review the CSV export merge request."}),
                     kwargs={"timeout": 3600}, daemon=True).start()


def seed_usage(sid: str) -> None:
    """F20: token usage on six of the last ten days (zero runs between them) in demo/hidden's transcript."""
    from datetime import date, timedelta
    path = R.sess_dir(sid)
    d = os.path.join(path, "claude", "projects", "-" + R.PROJECTS["demo/hidden"].strip("/").replace("/", "-").replace(".", "-"))
    tr = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")), key=os.path.getmtime)[-1]
    with open(tr, "a") as f:
        for ago, (i, o, cw, cr) in {1: (724, 188_000, 1_300_000, 57_700_000), 2: (10, 3_000, 87_000, 337_000),
                                   3: (708, 247_000, 1_300_000, 55_500_000), 4: (2_000, 510_000, 2_200_000, 147_600_000),
                                   7: (1_000, 278_000, 2_300_000, 67_700_000), 8: (1_000, 279_000, 1_700_000, 68_000_000)}.items():
            day = (date.today() - timedelta(days=ago)).isoformat()
            f.write(json.dumps({"type": "assistant", "sessionId": "u", "timestamp": "%sT12:00:00" % day,
                                "message": {"id": "msg-usage-%d" % ago, "role": "assistant", "content": [{"type": "text", "text": "Done."}],
                                            "usage": {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw,
                                                      "cache_read_input_tokens": cr}}}) + "\n")


def seed_convo(sid: str) -> None:
    """F05: a transcript worth reading - markdown, tool calls (one failed, one still running) - and a working turn."""
    d = os.path.join(R.sess_dir(sid), "claude", "projects", "-" + R.PROJECTS["grp/demo"].strip("/").replace("/", "-").replace(".", "-"))
    tr = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")), key=os.path.getmtime)[-1]
    conv = os.path.basename(tr)[:-6]
    rec = lambda role, ts, content, **m: {"type": role, "sessionId": conv, "timestamp": "2026-10-08T10:%s" % ts,  # noqa: E731
                                          "message": dict({"role": role, "content": content}, **m)}
    use = lambda i, name, inp: {"type": "tool_use", "id": i, "name": name, "input": inp}  # noqa: E731
    res = lambda i, text, err=False: {"type": "tool_result", "tool_use_id": i, "content": text, "is_error": err}  # noqa: E731
    md = ("## What is left before production\n\n### 3. PROD\n\n7. **shop prod** can go right away - the config and "
          "0.1.3 recreate the jobs.\n8. **web prod** (after install): enable `IndexJob` and "
          "`ReconcileIndexJob` again.\n\nThe failing check is in src/main/java/SearchIndex.java:42 - see "
          "[the runbook](https://example.com/runbook).\n\n**Unverified:** the WAR build for release.")
    recs = [rec("user", "01:00Z", [{"type": "text", "text": "Summarize what is left before production."}]),
            rec("assistant", "01:05Z", [{"type": "text", "text": "Checking the tests first."},
                                        use("t1", "Bash", {"command": "mvn -q test -pl search-core"}),
                                        use("t2", "Read", {"file_path": "/work/search/src/main/java/SearchIndex.java"})],
                id="msg-f05-1", usage={"input_tokens": 478, "output_tokens": 141_000, "cache_read_input_tokens": 36_300_000,
                                       "cache_creation_input_tokens": 916_000}),
            rec("user", "01:06.200Z", [res("t1", "BUILD SUCCESS"), res("t2", "a\nb\nc")]),
            rec("assistant", "01:07Z", [use("t3", "Grep", {"pattern": "recreate", "path": "/work"}),
                                        use("t4", "Bash", {"command": "git push origin HEAD:release"})], id="msg-f05-2"),
            rec("user", "01:09Z", [res("t3", "x\ny"), res("t4", "rejected", True)]),
            rec("assistant", "01:10Z", [{"type": "text", "text": md}], id="msg-f05-3"),
            rec("user", "02:00Z", [{"type": "text", "text": "And prepare the comment for #312."}]),
            rec("assistant", "02:01Z", [use("t5", "WebFetch", {"url": "https://gitlab.example.com/grp/demo/-/issues/312"})], id="msg-f05-4")]
    with open(tr, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    R.hook(sid, "UserPromptSubmit", session_id=conv, prompt="And prepare the comment for #312.")
    R.post("/api/features", {"modules": {"composer": True}})   # the Composer (opt-in since 9.44) holds the queue
    flag = os.path.join(R.WORK, "f05-stop")

    def stopper() -> None:   # `touch <work>/f05-stop`: the turn ends, the composer delivers what it held
        while not STOP.wait(1):
            if os.path.exists(flag):
                os.unlink(flag)
                R.hook(sid, "Stop", session_id=conv)
    threading.Thread(target=stopper, daemon=True).start()


def seed_audit(sids: dict) -> None:
    """F20: audit records as the real ones look - a hidden file read 10x in a row, git_run actions (confirm · git-run ·
    tool of one call), a push, a production HTTP run, a declined card."""
    demo, now = sids["grp/demo"], time.time()
    recs = []
    def at(t, **e):
        recs.append(dict(e, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now - t))))
    for i in range(10):   # oldest first, as the log is written
        at(3600 - i * 140, event="gui-read-hidden", project="grp/demo", path="config/.env.development")
    at(1500, event="confirm", kind="write", session=demo, approved=False, answered=True)
    at(1200, event="http-run", project="demo/http", file="ping.http", env="prod", cls="prod", by="gui", index=0, ok=True,
       requests=1, failed=0)
    for t, args in ((900, ["show", "--stat", "4d890c0"]), (600, ["push", "origin", "HEAD:release"])):
        at(t, event="confirm", kind="git_run", session=demo, approved=True, auto=True, why="git_mode hole", project="grp/demo")
        at(t - 1, event="git-run", session=demo, project="grp/demo", args=args, rc=0)
        at(t - 1, event="tool", session=demo, tool="git_run", ok=True, error=None)
    with open(os.path.join(R.PH, "audit.log"), "a") as f:
        for r in recs:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def _on_signal(signum, frame):  # noqa: ARG001
    STOP.set()


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    gl = http.server.ThreadingHTTPServer(("127.0.0.1", GLPORT), FakeGitLab)
    threading.Thread(target=gl.serve_forever, daemon=True).start()
    rc = 0
    try:
        R.start_git_daemon()
        setup_projects()
        setup_piper_home()
        R.mint()
        R.start_piperd()
        sids = seed_sessions()
        url = "http://127.0.0.1:%d/#token=%s" % (OPT.port, R.TOK["gui"])
        with open(os.path.join(R.WORK, "serve.json"), "w") as f:
            json.dump({"url": url, "port": OPT.port, "gui_token": R.TOK["gui"], "tty_token": R.TOK["tty"],
                       "work": R.WORK, "sessions": sids, "projects": R.PROJECTS, "gitlab_port": GLPORT,
                       "pid": os.getpid()}, f, indent=1)
        print("SERVE %s" % url, flush=True)
        print("work %s  (SIGTERM / Ctrl-C ends it)" % R.WORK, flush=True)
        while not STOP.wait(1):
            if R.PROC["piperd"].poll() is not None:
                print("piperd exited (rc %s) - see %s/piperd.out" % (R.PROC["piperd"].returncode, R.WORK), flush=True)
                rc = 1
                break
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)   # one teardown, not interrupted by a second TERM
        R.teardown()
        gl.shutdown()
        shutil.rmtree(os.path.dirname(R.SOCK), ignore_errors=True)   # pe2e-<our pid>
        if not OPT.work and not OPT.keep and rc == 0:
            shutil.rmtree(WORKDIR, ignore_errors=True)
        print("TORN DOWN", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
