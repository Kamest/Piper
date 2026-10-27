#!/usr/bin/env python3
"""tests/piper-e2e/run.py - T2 end-to-end checks of a REAL piperd (PLAN T2, K6/K7).

A piperd + ptyhost from this checkout run over fake-hole (tests/piper-unit/fake-hole.sh behind a small
wrapper) in a throwaway PIPER_HOME; the in-container attestation is the only stub (it needs Docker, T3
covers it). Session states are driven as Claude Code / Codex drive them: hook lines through
piper/sandbox/piper-event.py, tool calls through the session's MCP mailbox. Self-checking: every check
prints PASS/FAIL, the summary goes to <work>/results.json, the exit code is the number of failures (max 1).

Run it OUTSIDE any sandbox (sockets, PTYs):  python3 tests/piper-e2e/run.py [--port 17941] [--work DIR]
    [--only L1,I12,...] [--keep]
Never against the live piperd: own PIPER_HOME / HOME / port, everything it starts is killed by PID.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import pwd
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SK = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIPER = os.path.join(SK, "piper")
UNIT = os.path.join(SK, "tests", "piper-unit")
EVENT = os.path.join(PIPER, "sandbox", "piper-event.py")
sys.path.insert(0, PIPER)
import testsandbox  # noqa: E402  only inside the OS sandbox of scripts/piper-sandbox.py
testsandbox.require()
sys.path.pop(0)

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=17941)
ap.add_argument("--work", default="")
ap.add_argument("--only", default="")
ap.add_argument("--keep", action="store_true", help="leave the work dir (logs) behind")
A = ap.parse_args()
if A.port == 17895:
    sys.exit("never the live piperd's port")
WORK = os.path.abspath(A.work) if A.work else tempfile.mkdtemp(prefix="piper-e2e-")
_REAL = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
if os.path.realpath(WORK) == _REAL or _REAL.startswith(os.path.realpath(WORK).rstrip(os.sep) + os.sep) \
        or os.path.realpath(WORK).startswith(os.path.join(_REAL, ".")):
    sys.exit("--work %s is (or holds, or lies in a dot dir of) the real home - never" % WORK)
if os.path.exists(os.path.join(WORK, "ph")):
    shutil.rmtree(os.path.join(WORK, "ph"))
PH = os.path.join(WORK, "ph")                 # PIPER_HOME
HOMEF = os.path.join(WORK, "fakehome")        # HOME of piperd
REAL = os.path.join(WORK, "realhome")         # PIPER_REAL_HOME (the "personal" ~/.hole)
PP = os.path.join(WORK, "projects")              # PROJECT_PATH
CTL = os.path.join(WORK, "ctl")               # per-project switches of the hole wrapper
FAKED = os.path.join(WORK, "fake-docker")
BIN = os.path.join(WORK, "bin")
for d in (PH, HOMEF, os.path.join(REAL, ".hole"), os.path.join(REAL, ".claude"), PP, CTL, FAKED, BIN):
    os.makedirs(d, exist_ok=True)
SOCK = os.path.join(tempfile.gettempdir(), "pe2e-%d" % os.getpid(), "ptyhost.sock")
BASE = "http://127.0.0.1:%d" % A.port
HOSTH = "127.0.0.1:%d" % A.port
ONLY = {x.strip() for x in A.only.split(",") if x.strip()}

results: list[dict] = []
cur = {"id": ""}


def check(name: str, cond, detail="") -> bool:
    ok = bool(cond)
    results.append({"id": cur["id"], "check": name, "ok": ok, "detail": "" if ok else str(detail)[:800]})
    print(("PASS " if ok else "FAIL ") + "[%s] %s" % (cur["id"], name) + ("" if ok else "  -> " + str(detail)[:800]),
          flush=True)
    return ok


def wait(pred, timeout=10.0, step=0.1):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = pred()
        except Exception as e:  # noqa: BLE001
            last = None
            _ = e
        if last:
            return last
        time.sleep(step)
    return last


def sh(*argv, cwd=None, check_rc=True, env=None):
    r = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, env=env)
    if check_rc and r.returncode:
        raise RuntimeError("%s: %s" % (" ".join(argv), r.stderr.strip()))
    return r.stdout


# ------------------------------------------------------------------ projects
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="e2e", GIT_AUTHOR_EMAIL="e2e@x", GIT_COMMITTER_NAME="e2e",
               GIT_COMMITTER_EMAIL="e2e@x", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def git(root, *args, check_rc=True):
    return sh("git", "-C", root, *args, env=GIT_ENV, check_rc=check_rc)


def make_project(key: str, files: dict, commit: list | None = None, remote: bool = True) -> str:
    root = os.path.join(PP, *key.split("/"))
    os.makedirs(root, exist_ok=True)
    for rel, content in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    if not os.path.isdir(os.path.join(root, ".git")):
        git(root, "init", "-q", "-b", "main")
        if remote:
            bare = os.path.join(WORK, "remotes", key.replace("/", "_") + ".git")
            os.makedirs(os.path.dirname(bare), exist_ok=True)
            sh("git", "init", "-q", "--bare", "-b", "main", bare, env=GIT_ENV)
            git(root, "remote", "add", "origin", bare)
    for rel in commit if commit is not None else [r for r in files]:
        git(root, "add", "-f", rel)
    git(root, "commit", "-q", "-m", "init %s" % key, "--allow-empty")
    if remote:
        git(root, "push", "-q", "-u", "origin", "main")
        # Piper's host git never takes file:// (protocol.file.allow=never): the remote is a git daemon
        git(root, "remote", "set-url", "origin", "git://127.0.0.1:%d/%s.git" % (A.port + 1, key.replace("/", "_")))
    return os.path.realpath(root)


PROJECTS: dict[str, str] = {}
PLANTED = {   # hidden from sessions (candidates covered by the project's excludes, or an exclude itself)
    "config/secrets/db.properties": "password=PLANTED_1\n",
    "config/secrets.yml": "<secret>PLANTED_2</secret>\n",
    "certs/x.pem": "-----BEGIN PLANTED_4-----\n",
    "http-client/http-client.env.json": '{"test": {"host": "PLANTED_5"}}\n',
    "deploy/k8s/secret.yaml": "token: PLANTED_6\n",
    ".gitlab-ci.yml": "variables: {X: PLANTED_7}\n",
    ".env.x": "x=PLANTED_9\n",
    "notes/private.txt": "PLANTED_X excluded by the project itself\n",
}
LEAK_EXCLUDES = ["config/secrets", "config/secrets.yml", "certs/*.pem", "http-client/http-client.env.json",
                 "deploy/k8s", ".gitlab-ci.yml", ".env.*", "notes/private.txt"]


def setup_projects() -> None:
    plain = {"README.md": "# plain\nhello\n", "src/App.java": "class App { void run() {} }\n",
             ".hole/settings.json": "{}\n"}
    for k in ("e2e/plain", "e2e/other", "e2e/codex", "e2e/switch", "e2e/stop", "e2e/crash", "e2e/kill", "e2e/w11", "e2e/lim1",
              "e2e/lim2", "e2e/lim3", "e2e/corrupt"):
        PROJECTS[k] = make_project(k, dict(plain))
    files = dict(plain, **{".hole/settings.json": json.dumps({"files": {"exclude": LEAK_EXCLUDES}}, indent=2) + "\n",
                           "certs/tracked.pem": "TRACKED_SECRET\n", "src/Leak.java": "class Leak {}\n"})
    files.update(PLANTED)
    PROJECTS["e2e/leak"] = make_project("e2e/leak", files, commit=["README.md", "src/App.java", "src/Leak.java",
                                                                    ".hole/settings.json", "certs/tracked.pem"])
    PROJECTS["e2e/nest"] = make_project("e2e/nest", dict(plain))
    inner = os.path.join(PP, "e2e", "nest", "inner")
    os.makedirs(inner, exist_ok=True)
    PROJECTS["e2e/nest/inner"] = make_project("e2e/nest/inner", dict(plain), remote=False)
    git(PROJECTS["e2e/nest"], "rm", "-q", "--cached", "-r", "--ignore-unmatch", "inner", check_rc=False)
    with open(os.path.join(PROJECTS["e2e/nest"], ".git", "info", "exclude"), "a") as f:
        f.write("inner/\n")
    # 2026-10-09 rows (PLAN-ADD): review, git_run matrix (.git open / hidden), GitLab, libraries, conversations
    hid = {"config/secrets/db.properties": "password=PLANTED_G1\n"}
    rev = dict(plain, **{"code.txt": "before\n", "notes.txt": "PLANTED_RV1 private notes\n", "other.txt": "unrelated\n",
                         ".hole/settings.json": json.dumps({"files": {"exclude": ["notes.txt"]}}) + "\n"})
    PROJECTS["e2e/rev"] = make_project("e2e/rev", rev)
    for key, excl in (("e2e/gitm", ["config/secrets"]), ("e2e/githid", [".git", "config/secrets"])):
        PROJECTS[key] = make_project(key, dict(plain, **hid, **{
            "TODO.md": "TODO: e2e\n", ".hole/settings.json": json.dumps({"files": {"exclude": excl}}) + "\n"}))
    PROJECTS["e2e/gl"] = make_project("e2e/gl", dict(plain, **hid, **{
        "development/shot.txt": "an upload e2e\n", ".hole/settings.json":
        json.dumps({"files": {"exclude": ["config/secrets"]}}) + "\n"}),
        commit=["README.md", "src/App.java", ".hole/settings.json"])
    PROJECTS["e2e/lib"] = make_project("e2e/lib", dict(plain, **{
        "src/Lib.java": "class Lib {}\n", "pom.xml": "<project/>\n", "config/secrets/lib.properties": "k=PLANTED_8\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": ["config/secrets"]}}) + "\n"}),
        commit=["README.md", "src/App.java", "src/Lib.java", "pom.xml", ".hole/settings.json"])
    lib = PROJECTS["e2e/lib"]
    PROJECTS["e2e/libuser"] = make_project("e2e/libuser", dict(plain, **{".hole/settings.json": json.dumps(
        {"libraries": {lib: {"path": lib, "readwrite": True}}}) + "\n"}))
    PROJECTS["e2e/conv"] = make_project("e2e/conv", dict(plain))
    # N5-HTTP: a .http file (committed) with dev / test / prod environments; the env files are hidden (L5 candidates)
    PROJECTS["e2e/http"] = make_project("e2e/http", dict(plain, **{
        "http-client/ping.http": "### ping\nGET {{host}}/ping\nAuthorization: Bearer {{token}}\n",
        "http-client/http-client.env.json": json.dumps({e: {"host": "http://127.0.0.1:%d" % (A.port + 7)}
                                                       for e in ("dev", "test", "prod")}) + "\n",
        "http-client/http-client.private.env.json": json.dumps({e: {"token": "PLANTED_HTTP_%s" % e.upper()}
                                                               for e in ("dev", "test", "prod")}) + "\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": ["http-client/http-client.env.json",
                                                                 "http-client/http-client.private.env.json"]}}) + "\n"}),
        commit=["README.md", "src/App.java", ".hole/settings.json", "http-client/ping.http"])
    write_projects()


def start_git_daemon() -> None:
    base = os.path.join(WORK, "remotes")
    os.makedirs(base, exist_ok=True)
    PROC["gitd"] = subprocess.Popen(["git", "daemon", "--reuseaddr", "--export-all", "--enable=receive-pack",
                                     "--listen=127.0.0.1", "--port=%d" % (A.port + 1), "--base-path=" + base, base],
                                    env=GIT_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_projects(garbage: bool = False) -> None:
    path = os.path.join(PH, "projects.json")
    if garbage:
        with open(path, "w") as f:
            f.write("{not json")
        return
    with open(path, "w") as f:
        json.dump({"projects": {k: dict({"path": v}, **({"gitlab": ["grp/app"]} if k == "e2e/gl" else {}))
                                for k, v in PROJECTS.items()}}, f)


# ------------------------------------------------------------------ the fake GitLab (inside piperd)
GL_HOST = "gitlab.e2e.test"
GL_STATE = os.path.join(WORK, "gitlab-state.json")
GL_CALLS = os.path.join(WORK, "gitlab-calls.jsonl")
GL_ISSUE = {"iid": 7, "project_id": 5, "title": "Login fails password=PLANTED_T1 for admin", "state": "opened",
            "description": "steps: token: PLANTED_T2", "labels": ["bug"], "author": {"username": "rep", "name": "Rep"},
            "assignees": [{"username": "e2e", "name": "E2E"}], "references": {"full": "grp/app#7"},
            "web_url": "https://%s/grp/app/-/issues/7" % GL_HOST, "updated_at": "2026-10-09T08:00:00Z",
            "user_notes_count": 0}
GL_INITIAL = {
    "issues": {"7": GL_ISSUE, "9": dict(GL_ISSUE, iid=9, title="Slow issue", references={"full": "grp/app#9"},
                                        web_url="https://%s/grp/app/-/issues/9" % GL_HOST, assignees=[])},
    "assigned": [GL_ISSUE], "slow": {"9": 0},
    "jobs": {"101": {"id": 101, "name": "deploy-test", "stage": "deploy", "status": "manual", "ref": "feature",
                     "pipeline": {"id": 55}},
             "102": {"id": 102, "name": "unit", "stage": "test", "status": "failed", "ref": "feature", "pipeline": {"id": 55}},
             "103": {"id": 103, "name": "build", "stage": "build", "status": "running", "ref": "feature", "pipeline": {"id": 56}},
             "104": {"id": 104, "name": "deploy-prod", "stage": "deploy", "status": "manual", "ref": "release",
                     "pipeline": {"id": 57}}},
}
FAKE_GITLAB = r"""# e2e: a fake GitLab REST API behind gitlab._OPENER (no network). State: a JSON file the test edits;
# every request is appended to a JSONL file (method, path, query, a sha256 of a multipart body).
import hashlib, io, json, time, types, urllib.error, urllib.parse

def install(state_file, calls_file):
    import gitlab

    def reply(obj, status=200):
        r = io.BytesIO(json.dumps(obj).encode())
        r.headers = {"Content-Type": "application/json"}
        r.status = status
        return r

    def open_(req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        path, q, m = u.path, dict(urllib.parse.parse_qsl(u.query)), req.get_method()
        data = req.data or b""
        rec = {"m": m, "path": path, "q": q, "ts": time.time()}
        if data and (req.get_header("Content-type") or "").startswith("multipart/"):
            head, _, rest = data.partition(b"\r\n\r\n")
            body = rest[:rest.rfind(b"\r\n--")]
            rec.update(multipart_sha=hashlib.sha256(body).hexdigest(), multipart_len=len(body),
                       filename=head.split(b'filename="')[1].split(b'"')[0].decode("utf-8", "replace"))
        elif data:
            rec["body"] = json.loads(data.decode() or "null")
        with open(calls_file, "a") as f:
            f.write(json.dumps(rec) + "\n")
        with open(state_file) as f:
            st = json.load(f)
        P = "/api/v4/projects/grp%2Fapp"
        parts = path[len(P):].strip("/").split("/") if path.startswith(P) else []
        if path == "/api/v4/user":
            return reply({"id": 1, "username": "e2e", "name": "E2E"})
        if path == "/api/v4/todos":
            return reply(st.get("todos", []) if q.get("page", "1") == "1" else [])
        if path == "/api/v4/issues":
            return reply(st.get("assigned", []) if q.get("page", "1") == "1" else [])
        if path in ("/api/v4/merge_requests", "/api/v4/users"):
            return reply([])
        if path == "/api/graphql":
            return reply({"data": {"project": {"workItems": {"nodes": []}}}})
        if path == P:
            return reply({"id": 5, "path_with_namespace": "grp/app", "default_branch": "main"})
        if parts[:1] == ["issues"] and len(parts) == 1:
            return reply(list(st["issues"].values()) if q.get("page", "1") == "1" else [])
        if parts[:1] == ["issues"] and len(parts) >= 2 and parts[1] in st["issues"]:
            if len(parts) == 2:
                time.sleep(float(st.get("slow", {}).get(parts[1]) or 0))
                return reply(st["issues"][parts[1]])
            if parts[2] == "notes" and len(parts) == 4:
                return reply({"id": int(parts[3]), "body": "old note", "author": {"username": "e2e"}, "system": False})
            if parts[2] in ("discussions", "links", "notes") and m == "GET":
                return reply([])
            if parts[2] == "notes" and m == "POST":
                return reply({"id": 900, "body": (rec.get("body") or {}).get("body"), "author": {"username": "e2e"}})
        if parts in (["milestones"], ["pipelines"], ["merge_requests"]):
            return reply([])
        if parts == ["uploads"] and m == "POST":
            name = rec.get("filename", "f")
            return reply({"alt": name, "url": "/uploads/0123abcd/" + name, "full_path": "/grp/app/uploads/0123abcd/" + name,
                          "markdown": "[%s](/uploads/0123abcd/%s)" % (name, name)}, 201)
        if parts[:1] == ["jobs"] and len(parts) >= 2 and parts[1] in st["jobs"]:
            if len(parts) == 2:
                return reply(st["jobs"][parts[1]])
            if parts[2] in ("play", "retry") and m == "POST":
                return reply(dict(st["jobs"][parts[1]], status="pending"))
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"404 Not found"}'))

    gitlab._OPENER = types.SimpleNamespace(open=open_)
"""
# N-I21 / N-I22: the host's Codex config (a fake): a remote MCP with a plain header and credential headers,
# and remote MCPs at private / metadata / gateway addresses
CODEX_HOST_TOML = """model = "gpt-5"
[mcp_servers.grafana]
url = "https://mcp-grafana.example.test/mcp"
tool_timeout_sec = 120
[mcp_servers.grafana.http_headers]
"X-Region" = "eu"
Authorization = "Bearer PLANTED_H1"
"X-Api-Key" = "PLANTED_H2"
[mcp_servers.metadata]
url = "http://169.254.169.254/latest/meta-data"
[mcp_servers.lan]
url = "http://10.1.2.3/mcp"
[mcp_servers.gateway]
url = "http://host.internal:17895/mcp"
[mcp_servers.localjava]
command = "java"
"""


def gl_state(**kw) -> None:
    with open(GL_STATE) as f:
        st = json.load(f)
    st.update(kw)
    with open(GL_STATE, "w") as f:
        json.dump(st, f)


def gl_token(on: bool) -> None:
    """The fake GitLab token for piperd ($PIPER_HOME/gitlab-token, read on every call) - never a real one."""
    path = os.path.join(PH, "gitlab-token")
    if on:
        with open(path, "w") as f:
            f.write("e2e-fake-gitlab-token\n")
        os.chmod(path, 0o600)
    elif os.path.exists(path):
        os.unlink(path)


def gl_calls(pred=None) -> list:
    try:
        with open(GL_CALLS) as f:
            rows = [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []
    return [r for r in rows if pred is None or pred(r)]


# ------------------------------------------------------------------ the daemon
# sessions.ALWAYS_ASK; gitlab_note_edit left it in orchestrator (its own configurable approval, merge 2026-10-09)
ALWAYS_ASK = ["settings_change", "allow_domain", "http_prod", "http_host", "db_write_test", "db_write_prod",
              "db_read_prod", "db_unmask", "gitlab_job_protected", "git_run_project", "write_exec"]


def setup_home() -> None:
    with open(os.path.join(REAL, ".hole", "settings.json"), "w") as f:
        json.dump({"files": {"exclude": []}, "environment": {"GITLAB_TOKEN": "$GITLAB_PAT", "MY_TOKEN": "PLANTED_E"},
                   "network": {"allow": ["example.org"]}}, f)
    # limits.py reads the host's Claude login: an EXPIRED one (never sent anywhere) whose token must not reach
    # /api/state, limits.json or a log (I8) - a valid one would make piperd call api.anthropic.com
    os.makedirs(os.path.join(REAL, ".claude"), exist_ok=True)
    with open(os.path.join(REAL, ".claude", ".credentials.json"), "w") as f:
        json.dump({"claudeAiOauth": {"accessToken": "PLANTED_LIMITS_TOKEN", "refreshToken": "PLANTED_LIMITS_REFRESH",
                                     "expiresAt": 1000, "subscriptionType": "team"}}, f)
    with open(os.path.join(HOMEF, ".gitconfig"), "w") as f:   # the host user's identity (host git commits)
        f.write("[user]\n\tname = e2e\n\temail = e2e@example.org\n")
    with open(os.path.join(PH, "features.json"), "w") as f:
        json.dump({"base": "all", "onboarded": True, "modules": {}}, f)
    ijhttp_spec("dev", '{"pong": "default"}')   # N5: every .http run answers (N5-HTTP sets its own per run)
    # W1: the ALWAYS_ASK kinds written into policy.json by hand must still ask; git_commit is the control
    with open(os.path.join(PH, "policy.json"), "w") as f:
        json.dump({"auto_approve": ["git_commit"] + ALWAYS_ASK, "gitlab_host": GL_HOST, "protected_branches": ["release"]}, f)
    # the fake GitLab (BIN/e2e_gitlab.py, inside piperd); its fake token is written by N-I19 only (gl_token):
    # with a token the inbox poller maps every project (records repository roots) - L10 needs one unrecorded
    with open(GL_STATE, "w") as f:
        json.dump(GL_INITIAL, f)
    with open(os.path.join(BIN, "e2e_gitlab.py"), "w") as f:
        f.write(FAKE_GITLAB)
    # N-L11: a feed config from before 2026-10-08 still carries wait_min
    os.makedirs(os.path.join(PH, "feed"), exist_ok=True)
    with open(os.path.join(PH, "feed", "config.json"), "w") as f:
        json.dump({"enabled": True, "wait_min": 5}, f)
    # N-I21/N-I22: the host's ~/.codex/config.toml (fake) with remote MCP servers - a static credential header and
    # private / metadata / gateway URLs must never reach a sandbox's generated config.toml (R3)
    os.makedirs(os.path.join(REAL, ".codex"), exist_ok=True)
    with open(os.path.join(REAL, ".codex", "config.toml"), "w") as f:
        f.write(CODEX_HOST_TOML)
    with open(os.path.join(BIN, "hole"), "w") as f:
        f.write("""#!/bin/sh
# e2e wrapper of fake-hole.sh: per-project switches in $E2E_CTL/<project dir name>.<switch>
name=$(basename "$3")
c="$E2E_CTL/$name"
if [ -f "$c.exit" ]; then printf 'Error: e2e forced failure\\n' >&2; exit "$(cat "$c.exit")"; fi
[ -f "$c.trust" ] && export FAKE_HOLE_TRUST=1
[ -f "$c.ignore-term" ] && trap '' TERM
exec /bin/sh %s "$@"
""" % os.path.join(UNIT, "fake-hole.sh"))
    os.chmod(os.path.join(BIN, "hole"), 0o755)
    # macOS `open` (Open in terminal, links) would put windows on the tester's real desktop: logged only
    with open(os.path.join(BIN, "open"), "w") as f:
        f.write('#!/bin/sh\nprintf "%%s\\n" "$*" >> %s\n' % os.path.join(WORK, "open.log"))
    os.chmod(os.path.join(BIN, "open"), 0o755)
    with open(os.path.join(BIN, "piperd_e2e.py"), "w") as f:
        f.write("""import os, sys
sys.path.insert(0, %r)
import guard
guard.attest = lambda *a, **k: {"ok": True, "failures": [], "checks": 3, "resealed": {}}
import e2e_gitlab
e2e_gitlab.install(%r, %r)
import piperd
if os.environ.get("PIPER_IJHTTP", "").endswith("fake-ijhttp.py"):
    sys.path.insert(0, %r)
    import httpjail, _httpfake
    httpjail.run = _httpfake.run
sys.exit(piperd.main())
""" % (PIPER, GL_STATE, GL_CALLS, UNIT))


# GITLAB_CI stays: with CI / CI_JOB_ID it is how piperd's own test-sandbox check knows the CI job
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("PIPER_", "ORCH_", "GITLAB_")) or k in ("GITLAB_CI", "PIPER_TEST_HTTP_BROKER")}
ENV.update({
    "HOME": HOMEF, "PIPER_HOME": PH, "PIPER_REAL_HOME": REAL, "PROJECT_PATH": PP,
    # every Piper process of the harness is a test process: no real home, no write outside WORK / temp (15.28)
    "PIPER_TEST": "1", "PIPER_TEST_ROOTS": WORK,
    # the suites assert the guards (cards, masking, L1-L5): they run on the policy as before the tree (9.46, like _env.py)
    "PIPER_SECURITY_TREE": "0",
    "PIPER_PTY_SOCK": SOCK, "PIPER_PORT": str(A.port),
    "PIPER_HOLE_BIN": os.path.join(BIN, "hole"), "PIPER_DOCKER_BIN": os.path.join(UNIT, "fake-docker.py"),
    "FAKE_DOCKER_DIR": FAKED, "FAKE_DOCKER_TMP": os.path.join(FAKED, "tmp"), "E2E_CTL": CTL, "PIPER_DESKTOP_NOTIFY": "0",
    "PIPER_MAX_SESSIONS": "6", "PIPER_WATCH_INTERVAL": "2", "PIPER_STOP_RETRY": "1", "PIPER_STOP_KILL_AFTER": "4",
    # L11: the background loops on, with short intervals
    "PIPER_INBOX_INTERVAL": "2", "PIPER_FEED_TICK": "1", "PIPER_PROD_TICK": "2", "PIPER_MM_INTERVAL": "1",
    "PIPER_GIT_GUARD_INTERVAL": "1", "PIPER_IJHTTP": os.path.join(UNIT, "fake-ijhttp.py"),
    # N5: the fake ijhttp's console / report (N5-HTTP rewrites it per run) and its argv log - without them every
    # .http run of the harness ended in a traceback of the fake
    "FAKE_IJHTTP": os.path.join(WORK, "ijhttp-spec.json"), "FAKE_IJHTTP_LOG": os.path.join(WORK, "ijhttp.log"),
    "PATH": BIN + os.pathsep + os.environ.get("PATH", ""),
})

PROC: dict = {"piperd": None, "log": None, "gitd": None}


def start_piperd() -> None:
    log = open(os.path.join(WORK, "piperd.out"), "ab")
    PROC["piperd"] = subprocess.Popen([sys.executable, os.path.join(BIN, "piperd_e2e.py")], env=ENV, stdout=log,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    # A different test server can already own the port. Only our live child plus our
    # freshly minted token count as ready; an unauthenticated 401 proves neither.
    def controller_ready():
        if PROC["piperd"].poll() is not None:
            return True
        status, _, raw = req("GET", "/api/ping")
        if status != 200:
            return False
        health = json.loads(raw)
        return health.get("pid") == PROC["piperd"].pid and health.get("capabilities", {}).get("quiet") is True
    ready = wait(controller_ready, 20)
    if not ready or PROC["piperd"].poll() is not None:
        kill_piperd()
        raise RuntimeError("piperd did not come up (see %s/piperd.out)" % WORK)


def kill_piperd(sig=signal.SIGKILL) -> None:
    p = PROC["piperd"]
    if p and p.poll() is None:
        os.kill(p.pid, sig)   # by PID only
        p.wait(10)


def ptyhost_pid() -> int | None:
    sys.path.insert(0, PIPER)
    import ptyclient   # noqa: E402
    try:
        c = ptyclient.PtyClient(SOCK)
        try:
            return c.call("ping").get("pid")
        finally:
            c.close()
    except Exception:  # noqa: BLE001
        return None


def tokens() -> dict:
    out = {}
    with open(os.path.join(PH, "tokens")) as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3 and not line.startswith("#"):
                out[p[2]] = p[0]
    return out


TOK: dict = {}


def mint() -> None:
    with open(os.path.join(PH, "tokens"), "a") as f:
        for label in ("gui", "tty"):
            t = base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
            f.write("%s\t*\t%s\n" % (t, label))
            TOK[label] = t


# ------------------------------------------------------------------ HTTP helpers
def req(method, path, body=None, tok="gui", auth=True, headers=None, raw=None, timeout=30):
    c = http.client.HTTPConnection("127.0.0.1", A.port, timeout=timeout)
    h = {"Host": HOSTH}
    if auth and tok:
        h["Authorization"] = "Bearer " + TOK[tok]
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
    finally:
        c.close()
    try:
        j = json.loads(b) if b and b[:1] in (b"{", b"[") else None
    except ValueError:
        j = None
    return r.status, j, b


def get(path, tok="gui"):
    return req("GET", path, tok=tok)


def post(path, body=None, tok="gui"):
    return req("POST", path, body if body is not None else {}, tok=tok)


def state() -> dict:
    return get("/api/state")[1]


def sess(sid) -> dict:
    for s in state()["sessions"]:
        if s["id"] == sid:
            return s
    return {}


def pending(sid=None, kind=None) -> list:
    return [p for p in state()["pending"] if (sid is None or p["session"] == sid) and (kind is None or p["kind"] == kind)]


def wait_state(sid, want, timeout=15.0):
    want = want if isinstance(want, tuple) else (want,)
    s = wait(lambda: (lambda x: x if x.get("state") in want else None)(sess(sid)), timeout)
    return s or sess(sid)


def create(project, **kw) -> str:
    st, j, b = post("/api/sessions", dict({"project": project}, **kw))
    if st != 202:
        raise RuntimeError("create %s: %s %s" % (project, st, b[:300]))
    return j["id"]


def ready_session(project, approve=True, **kw) -> str:
    """A session of `project` up to ready (the first start of a project blocks for its settings approval)."""
    sid = create(project, **kw)
    s = wait_state(sid, ("blocked", "ready", "waiting", "working", "attention", "error", "exited"), 20)
    if s.get("state") == "blocked" and approve:
        st, _, b = post("/api/sessions/%s/approve" % sid)
        if st != 200:
            raise RuntimeError("approve %s: %s %s" % (sid, st, b[:300]))
        s = wait_state(sid, ("ready", "waiting", "working", "attention", "error", "exited"), 20)
    if s.get("state") not in ("ready", "waiting", "working", "attention"):
        raise RuntimeError("%s did not get ready: %s" % (project, json.dumps(s)[:500]))
    return sid


def stop(sid, timeout=15.0) -> dict:
    post("/api/sessions/%s/stop" % sid)
    return wait_state(sid, ("exited", "error"), timeout)


def sess_dir(sid) -> str:
    return os.path.join(PH, "sessions", sid)


def box(sid) -> str:
    s = sess(sid)
    return os.path.join(sess_dir(sid), "claude") if s.get("claude_mode", "session") == "session" else \
        os.path.join(sess_dir(sid), "piper")


def hook(sid, event, **fields) -> None:
    """One hook line as Claude Code's hook writes it (piper-event.py, stdin = the hook input)."""
    inp = dict(fields, hook_event_name=event)
    r = subprocess.run([sys.executable, EVENT, event, os.path.join(box(sid), "piper-events.jsonl")],
                       input=json.dumps(inp), capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("piper-event %s: %s" % (event, r.stderr))


def tool(sid, name, args=None, timeout=30.0, answer=None) -> dict:
    """A tool call through the mailbox, as piper-mcp.py makes it. answer(card) decides a card if one comes."""
    rid = os.urandom(16).hex()
    old = {p["id"] for p in pending(sid)}
    mb = os.path.join(box(sid), "piper-mailbox")
    os.makedirs(os.path.join(mb, "out"), exist_ok=True)
    tmp = os.path.join(mb, "out", ".%s.tmp" % rid)
    with open(tmp, "w") as f:
        json.dump({"id": rid, "tool": name, "args": args or {}}, f)
    os.rename(tmp, os.path.join(mb, "out", rid + ".json"))
    path = os.path.join(mb, "in", rid + ".json")
    end = time.time() + timeout
    seen_card = None
    while time.time() < end:
        if os.path.exists(path):
            with open(path) as f:
                r = json.load(f)
            os.unlink(path)
            r["_card"] = seen_card
            return r
        if answer is not None and seen_card is None:
            cards = [p for p in pending(sid) if p["id"] not in old]
            if cards:
                seen_card = cards[0]
                answer(seen_card)
        time.sleep(0.1)
    return {"ok": False, "error": "TIMEOUT (no answer in %ss)" % timeout, "_card": seen_card}


def decide(card, ok, always=False, session_always=False):
    return post("/api/pending/%s" % card["id"], {"approve": ok, "always": always, "session_always": session_always})


# ------------------------------------------------------------------ WebSocket client (masked frames)
class WS:
    def __init__(self, sid, origin=True):
        self.s = socket.create_connection(("127.0.0.1", A.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        h = ["GET /ws/%s HTTP/1.1" % sid, "Host: " + HOSTH, "Upgrade: websocket", "Connection: Upgrade",
             "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        if origin:
            h.append("Origin: http://" + HOSTH)
        self.s.sendall(("\r\n".join(h) + "\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            d = self.s.recv(4096)
            if not d:
                break
            self.buf += d
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.status = int(head.split(b" ")[1]) if head else 0

    def send(self, op, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytes([0x80 | op])
        hdr += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_json(self, obj):
        self.send(1, json.dumps(obj).encode())

    def _read(self, n):
        while len(self.buf) < n:
            d = self.s.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b1, b2 = self._read(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        return b1 & 0x0F, self._read(n)

    def collect(self, secs):
        """(binary bytes, [text messages], close code or None) within secs."""
        out, texts, code = b"", [], None
        end = time.time() + secs
        self.s.settimeout(0.3)
        while time.time() < end:
            try:
                op, p = self.recv()
            except socket.timeout:
                continue
            except (EOFError, OSError):
                break
            if op == 2:
                out += p
            elif op == 1:
                texts.append(json.loads(p))
            elif op == 8:
                code = struct.unpack(">H", p[:2])[0] if len(p) >= 2 else 1005
                break
        return out, texts, code

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def term(sid, secs=2.0) -> bytes:
    """The terminal's replay through the GUI's WebSocket."""
    w = WS(sid)
    try:
        w.send_json({"auth": TOK["gui"]})
        out, _, _ = w.collect(secs)
        return out
    finally:
        w.close()


def audit_events(sid=None, event=None) -> list:
    st, j, _ = get("/api/audit?limit=2000" + ("&session=%s" % sid if sid else ""))
    ev = (j or {}).get("events") or []
    return [e for e in ev if event is None or e.get("event") == event]


# ------------------------------------------------------------------ checks
SECTIONS = []


def section(sid_):
    def deco(fn):
        SECTIONS.append((sid_, fn))
        return fn
    return deco


S: dict = {}   # shared sessions between sections


@section("L1")
def l1():
    seen: list = []
    sid = create("e2e/plain")
    stop_poll = threading.Event()
    blocked_polled = threading.Event()

    def poll():
        last = None
        while not stop_poll.is_set():
            try:
                st = sess(sid).get("state")
            except Exception:  # noqa: BLE001
                st = None
            if st and st != last:
                seen.append(st)
                last = st
                if st == "blocked":
                    blocked_polled.set()
            time.sleep(0.03)
    t = threading.Thread(target=poll, daemon=True)
    t.start()
    s = wait_state(sid, "blocked", 20)
    check("a first start blocks for the settings approval (L1)", s.get("state") == "blocked" and
          {b["kind"] for b in s.get("blocks") or []} == {"settings-changed"} if isinstance(s.get("blocks"), list)
          else s.get("state") == "blocked", s)
    # Approval ends blocked immediately. Let the independent observer see it first;
    # wait_state above can otherwise win the race against the 30 ms poll interval.
    blocked_polled.wait(5)
    post("/api/sessions/%s/approve" % sid)
    s = wait_state(sid, "ready", 20)
    stop_poll.set()
    t.join()
    check("approve -> ready", s.get("state") == "ready", s)
    if seen[-1:] != ["ready"]:
        seen.append(sess(sid).get("state"))
    # polled every 30 ms: a short state (preparing) may slip between two polls - these may not
    order = ["preflight", "blocked", "attesting", "ready"]
    it = iter(seen)
    check("states in order %s (seen %s)" % ("→".join(order), "→".join(seen)),
          all(any(x == want for x in it) for want in order), seen)
    check("the terminal shows the agent (WS replay)", b"fake claude ready" in term(sid), "")
    hook(sid, "SessionStart", session_id="0f1e2d3c-aaaa-bbbb-cccc-000000000001", source="startup")
    check("SessionStart sets claude_session", wait(lambda: sess(sid).get("claude_session") ==
                                                       "0f1e2d3c-aaaa-bbbb-cccc-000000000001", 5), sess(sid))
    for ev, want, extra in (("UserPromptSubmit", "working", {"prompt": "do it"}), ("Stop", "waiting", {}),
                            ("Notification", "attention", {"message": "Claude needs your permission to use Bash"}),
                            ("UserPromptSubmit", "working", {"prompt": "go on"}), ("Stop", "waiting", {})):
        hook(sid, ev, session_id="0f1e2d3c-aaaa-bbbb-cccc-000000000001", **extra)
        s = wait_state(sid, want, 5)
        check("%s -> %s" % (ev, want), s.get("state") == want, s)
        if ev == "Notification":
            check("attention carries the message", "permission" in (s.get("detail") or ""), s)
    S["plain"] = sid
    # Codex: its own CODEX_HOME, Codex hooks
    cid = ready_session("e2e/codex", agent="codex")
    s = sess(cid)
    check("Codex session ready (claude_mode none)", s.get("claude_mode") == "none" and s.get("agent") == "codex", s)
    check("Codex box gets its own hook file", os.path.isdir(box(cid)), box(cid))
    hook(cid, "SessionStart", session_id="0199aaaa-bbbb-cccc-dddd-000000000001")
    hook(cid, "UserPromptSubmit", prompt="x")
    check("Codex UserPromptSubmit -> working", wait_state(cid, "working", 5).get("state") == "working", sess(cid))
    hook(cid, "PermissionRequest", tool_name="Bash")
    s = wait_state(cid, "attention", 5)
    check("Codex PermissionRequest -> attention 'Codex needs permission: Bash'",
          s.get("detail") == "Codex needs permission: Bash", s)
    hook(cid, "Stop")
    check("Codex Stop -> waiting", wait_state(cid, "waiting", 5).get("state") == "waiting", sess(cid))
    check("Codex: no host ~/.codex in the codex dir", not os.path.islink(os.path.join(sess_dir(cid), "codex")), "")
    stop(cid)
    check("Codex stop -> exited", sess(cid).get("state") == "exited", sess(cid))


@section("L2")
def l2():
    # a) Stop right after create (before the spawn)
    sid = create("e2e/stop")
    post("/api/sessions/%s/stop" % sid)
    s = wait_state(sid, ("exited", "error"), 15)
    check("Stop during preflight -> exited", s.get("state") == "exited", s)
    # b) Stop while hole asks the trust question and ignores TERM: TERM repeated, KILL after STOP_KILL_AFTER
    for f in ("trust", "ignore-term"):
        open(os.path.join(CTL, "stop." + f), "w").close()
    try:
        sid = create("e2e/stop")
        s = wait_state(sid, ("blocked", "trust"), 15)
        if s.get("state") == "blocked":
            post("/api/sessions/%s/approve" % sid)
        s = wait_state(sid, "trust", 15)
        check("hole's trust question -> state trust", s.get("state") == "trust", s)
        t0 = time.time()
        post("/api/sessions/%s/stop" % sid)
        s = wait_state(sid, ("exited", "error"), 20)
        dt = time.time() - t0
        check("a TERM-ignoring hole is killed after STOP_KILL_AFTER (4 s here): %.1fs" % dt,
              s.get("state") == "exited" and 3 <= dt <= 15, s)
        sigs = [e.get("sig") for e in audit_events(sid, "stop-nudge")][::-1]   # the audit tail is newest first
        check("stop-nudge repeated TERM, then KILL (%s)" % sigs, "TERM" in sigs and sigs[-1:] == ["KILL"], sigs)
        check("killed hole reports 137", s.get("rc") == 137, s)
    finally:
        for f in ("trust", "ignore-term"):
            os.unlink(os.path.join(CTL, "stop." + f))
    # c) trust answered in the terminal (WebSocket input) -> the start goes on
    open(os.path.join(CTL, "stop.trust"), "w").close()
    try:
        post("/api/sessions/%s/retry" % sid)
        s = wait_state(sid, "trust", 15)
        w = WS(sid)
        w.send_json({"auth": TOK["gui"]})
        w.collect(0.5)
        w.send_json({"t": "in", "d": "y\r"})
        w.close()
        s = wait_state(sid, "ready", 15)
        check("answering hole's trust question in the terminal -> ready", s.get("state") == "ready", s)
    finally:
        os.unlink(os.path.join(CTL, "stop.trust"))
    # d) Stop of a ready session: TERM, exited
    s = stop(sid)
    check("Stop of a ready session -> exited (rc 0/143)", s.get("state") == "exited" and s.get("rc") in (0, 143), s)
    check("no 'ended unexpectedly' after a Stop", "unexpectedly" not in (s.get("detail") or ""), s)
    S["stopped"] = sid
    # e) a second Stop of an exited session is harmless
    st, _, _ = post("/api/sessions/%s/stop" % sid)
    check("Stop of an exited session -> no error", st == 200 and sess(sid).get("state") == "exited", st)


@section("L3")
def l3():
    sid = S.get("stopped") or ready_session("e2e/stop")
    if sess(sid).get("state") != "exited":
        stop(sid)
    post("/api/sessions/%s/retry" % sid)
    s = wait_state(sid, "ready", 15)
    check("Retry of an exited session -> ready", s.get("state") == "ready", s)
    st, j, _ = post("/api/sessions/%s/retry" % sid)
    check("Retry of a running session refused (409)", st == 409, (st, j))
    st, j, _ = post("/api/sessions/%s/forget" % sid)
    check("Forget of a running session refused (409)", st == 409, (st, j))
    # resume: a conversation with a transcript -> --resume <id>
    conv = "5e55a0b1-1111-2222-3333-444455556666"
    hook(sid, "SessionStart", session_id=conv)
    wait(lambda: sess(sid).get("claude_session") == conv, 5)
    tdir = os.path.join(box(sid), "projects", "-x")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, conv + ".jsonl"), "w") as f:
        f.write(json.dumps({"sessionId": conv, "type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    stop(sid)
    post("/api/sessions/%s/retry" % sid)
    s = wait_state(sid, "ready", 15)
    spawns = audit_events(sid, "spawn")
    argv = (spawns[0] if spawns else {}).get("argv") or []   # newest first
    check("Continue resumes the conversation (--resume %s)" % conv, "--resume" in argv and conv in argv, argv)
    stop(sid)
    st, _, _ = post("/api/sessions/%s/forget" % sid)
    check("Forget of an exited session", st == 200, st)
    check("a forgotten session is not listed", not sess(sid) or sess(sid).get("hidden") is True, sess(sid))


@section("L6")
def l6():
    with open(os.path.join(CTL, "crash.exit"), "w") as f:
        f.write("3")
    try:
        sid = create("e2e/crash")
        s = wait_state(sid, ("blocked", "exited", "error"), 15)
        if s.get("state") == "blocked":
            post("/api/sessions/%s/approve" % sid)
        s = wait_state(sid, ("exited", "error"), 15)
        check("hole exiting 3 during the start -> exited, rc 3", s.get("state") == "exited" and s.get("rc") == 3, s)
        check("the message says why (F1-6): %r" % s.get("detail"),
              (s.get("detail") or "").startswith("Claude ended unexpectedly (code 3)") and "e2e forced failure" in s["detail"], s)
    finally:
        os.unlink(os.path.join(CTL, "crash.exit"))
    sid = ready_session("e2e/kill")
    pid = sess(sid).get("pid")
    os.kill(pid, signal.SIGKILL)   # our own fake hole (its pid from /api/state)
    s = wait_state(sid, "exited", 10)
    check("a hole killed from outside -> 'ended unexpectedly (code 137)'",
          s.get("rc") == 137 and (s.get("detail") or "").startswith("Claude ended unexpectedly (code 137)"), s)
    q = wait(lambda: [a for a in (state().get("queue") or {}).get("actions") or [] if sid in json.dumps(a)], 5)
    check("the crash is a Needs-you item in the queue (L11 feed tick)", q, state().get("queue"))


@section("L10")
def l10():
    # nested: a running session of e2e/nest blocks a first start of e2e/nest/inner (its repo is not recorded yet)
    outer = ready_session("e2e/nest")
    sid = create("e2e/nest/inner")
    s = wait_state(sid, ("blocked", "ready", "error"), 15)
    check("a start inside a running session's directory blocks", s.get("state") == "blocked" and
          "enclosing" in (s.get("detail") or ""), s)
    stop(outer)
    post("/api/sessions/%s/stop" % sid)
    # the Global agent: one only, no start options
    adv = ready_session("advisor")
    S["advisor"] = adv
    st, j, _ = post("/api/sessions", {"project": "advisor"})
    check("a second Global agent refused (409)", st == 409 and "already running" in (j or {}).get("error", ""), (st, j))
    st, j, _ = post("/api/sessions", {"project": "advisor", "opts": {"docker": True}})
    check("the Global agent takes no start options (409)", st == 409, (st, j))
    # the session limit (PIPER_MAX_SESSIONS=6)
    running = [x for x in state()["sessions"] if x["state"] in ("preflight", "blocked", "preparing", "spawning", "trust",
                                                                    "starting", "attesting", "ready", "working",
                                                                    "waiting", "attention", "stopping")]
    extra = []
    try:
        for k in ("e2e/lim1", "e2e/lim2", "e2e/lim3", "e2e/w11", "e2e/other", "e2e/corrupt"):
            if len(running) + len(extra) >= 6:
                break
            extra.append(ready_session(k))
        st, j, _ = post("/api/sessions", {"project": "e2e/lim1"})
        check("session %d refused: limit of 6 (409)" % (len(running) + len(extra) + 1),
              st == 409 and "limit" in (j or {}).get("error", ""), (st, j, len(running), len(extra)))
    finally:
        for x in extra:
            post("/api/sessions/%s/stop" % x)
        for x in extra:
            wait_state(x, ("exited", "error"), 15)


@section("I12")
def i12():
    st, _, _ = req("GET", "/api/state", headers={"Host": "evil.example:%d" % A.port})
    check("wrong Host -> 421", st == 421, st)
    st, _, _ = req("GET", "/api/state", headers={"Origin": "http://evil.example"})
    check("foreign Origin -> 403", st == 403, st)
    st, _, _ = req("POST", "/api/sessions", {"project": "e2e/plain"}, headers={"Origin": "http://evil.example"})
    check("foreign Origin POST -> 403", st == 403, st)
    st, _, _ = req("GET", "/api/state", auth=False)
    check("no token -> 401", st == 401, st)
    st, _, _ = req("GET", "/api/state?token=" + TOK["gui"], auth=False)
    check("a token in the query is never accepted -> 401", st == 401, st)
    st, _, _ = req("GET", "/api/state", headers={"Authorization": "Bearer nope"}, auth=False)
    check("unknown token -> 401", st == 401, st)
    for m in ("PUT", "DELETE", "PATCH"):
        st, _, _ = req(m, "/api/state")
        check("%s -> 405" % m, st == 405, st)

    def big(path, n):
        s = socket.create_connection(("127.0.0.1", A.port), timeout=10)
        s.sendall(("POST %s HTTP/1.1\r\nHost: %s\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n"
                   "Content-Length: %d\r\n\r\n" % (path, HOSTH, TOK["gui"], n)).encode())
        d = s.recv(4096)
        s.close()
        return int(d.split(b" ")[1]) if d else 0
    check("POST body > 1 MiB -> 413", big("/api/sessions", (1 << 20) + 1) == 413, "")
    check("fs/write > 6 MiB -> 413", big("/api/fs/write", (6 << 20) + 1) == 413, "")
    sid = S.get("plain")
    if sid:
        check("image drop > 10 MiB -> 413", big("/api/sessions/%s/drop" % sid, (10 << 20) + 1) == 413, "")
        w = WS(sid, origin=False)
        check("WebSocket without Origin -> 403", w.status == 403, w.status)
        w.close()
        w = WS(sid)
        t0 = time.time()
        _, _, code = w.collect(6)
        w.close()
        check("WebSocket without auth: closed 4401 after ~3 s (%.1fs)" % (time.time() - t0), code == 4401, code)
        w = WS(sid)
        w.send_json({"auth": "wrong"})
        _, _, code = w.collect(4)
        w.close()
        check("WebSocket with a wrong token -> 4401", code == 4401, code)
    st, _, b = req("GET", "/")
    check("GUI shell has a CSP with a nonce", st == 200 and b"nonce-" not in b"" and b"__PIPER__" in b, st)
    st, _, _ = req("GET", "/piper/../tokens")
    check("static path traversal -> 404", st == 404, st)


@section("I9")
def i9():
    sid = S.get("plain")
    t = "tty"
    for m, path, body in (("GET", "/api/settings", None), ("POST", "/api/settings", {"section": "sessions", "values": {}}),
                          ("POST", "/api/sessions/%s/unlock" % sid, {"what": "projects", "on": True}),
                          ("POST", "/api/sessions/%s/start-opt" % sid, {"what": "docker", "on": True}),
                          ("POST", "/api/fs/write", {"project": "e2e/leak", "path": "config/secrets.yml",
                                                     "text": "x", "sha": "", "hidden": True}),
                          ("GET", "/api/fs/read?project=e2e/leak&path=config/secrets.yml&hidden=1", None),
                          ("POST", "/api/sessions", {"project": "e2e/plain", "opts": {"docker": True}}),
                          ("POST", "/api/features", {"preset": "basic"}),
                          ("GET", "/api/db/all", None), ("POST", "/api/fs/delete", {"project": "e2e/plain", "path": "README.md"}),
                          ("GET", "/api/doctor?deep=1", None),
                          # N3 review: each session's last answer (S6) and the production report (L1)
                          ("GET", "/api/board", None), ("GET", "/api/prod", None)):
        st, j, _ = req(m, path, body, tok=t)
        check("tty token: %s %s -> 403" % (m, path.split("?")[0]), st == 403, (st, j))
    # token-scope audit 2026-10-07: every state-changing POST is the GUI's; tty may only start + retry
    n_pending = len((get("/api/state")[1] or {}).get("pending") or [])
    for path, body in (("/api/pending/x", {"approve": True}), ("/api/sessions/%s/approve" % sid, {}),
                       ("/api/sessions/%s/allow" % sid, {"domain": "example.org"}),
                       ("/api/sessions/%s/send" % sid, {"text": "x"}), ("/api/sessions/%s/stop" % sid, {}),
                       ("/api/sessions/%s/git/push" % sid, {}), ("/api/git/mr", {"project": "e2e/plain", "title": "T"}),
                       ("/api/inbox/done", {"key": "k"}), ("/api/timesheet/manual", {}), ("/api/prod/run", {}),
                       ("/api/feed/chat/start", {}), ("/api/projects/add", {"path": "/tmp"}),
                       ("/api/stack/action", {"project": "e2e/plain", "verb": "down"}), ("/api/no/such/route", {})):
        st, j, _ = req("POST", path, body, tok=t)
        check("tty token: POST %s -> 403" % path.replace(sid or "-", "<id>"), st == 403, (st, j))
    check("tty token answered no card", len((get("/api/state")[1] or {}).get("pending") or []) == n_pending)
    st, _, _ = get("/api/state", tok=t)
    check("tty token reads /api/state (terminals only)", st == 200, st)


@section("I8")
def i8():
    """K7 leak sweep: nothing PLANTED reaches a session tool, a log, or a GET answer that is not one of the
    GUI's file views. Since the GUI is the human's IDE, those views show files hidden from
    Claude to the human - marked hidden, a read audited - so they are no longer swept (HUMAN_VIEWS)."""
    sid = ready_session("e2e/leak")
    S["leak"] = sid
    root = PROJECTS["e2e/leak"]
    hits = []

    def scan(label, body):
        b = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        if b"PLANTED" in b:
            i = b.index(b"PLANTED")
            hits.append("%s: ...%s..." % (label, b[max(0, i - 80):i + 40].decode("utf-8", "replace")))
    # a turn that changes a hidden file and a visible one (turn diffs, marks)
    hook(sid, "UserPromptSubmit", prompt="edit")
    time.sleep(0.5)
    with open(os.path.join(root, "config", "secrets.yml"), "a") as f:
        f.write("PLANTED_2b\n")
    with open(os.path.join(root, "README.md"), "a") as f:
        f.write("visible change\n")
    hook(sid, "Stop")
    wait_state(sid, "waiting", 5)
    time.sleep(1.5)
    pj = "project=e2e/leak"
    paths = list(PLANTED) + ["certs/tracked.pem", "README.md"]
    urls = ["/api/state", "/api/fs/list?%s&path=." % pj, "/api/fs/files?%s" % pj, "/api/fs/grep?%s&q=PLANTED" % pj,
            "/api/fs/grep?%s&q=PLANTED&cs=0" % pj, "/api/fs/grep?%s&q=planted&cs=0&path=config" % pj,
            "/api/fs/symbols?%s&q=PLANTED" % pj, "/api/git/log?%s" % pj, "/api/git/graph?%s" % pj,
            "/api/git/show?%s&commit=HEAD" % pj, "/api/git/ci?%s" % pj, "/api/git/mr-draft?%s" % pj,
            "/api/sessions/%s/git" % sid, "/api/sessions/%s/turns" % sid, "/api/sessions/%s/conversation" % sid,
            "/api/sessions/%s/diagnostics" % sid, "/api/sessions/%s/draft" % sid, "/api/conversations/search?q=PLANTED",
            "/api/board", "/api/audit?limit=2000", "/api/usage", "/api/http/files?%s" % pj, "/api/inbox", "/api/feed",
            "/api/timesheet", "/api/db/connections?%s" % pj, "/api/stack?%s" % pj, "/api/idea/problems?%s&path=README.md" % pj]
    for d in ("config", "config/secrets", "certs", "http-client", "development", "deploy/k8s", "notes"):
        urls.append("/api/fs/list?%s&path=%s" % (pj, d))
    for p in paths:
        for op in ("read", "marks", "raw"):
            urls.append("/api/fs/%s?%s&path=%s" % (op, pj, p))
        urls += ["/api/git/diff?%s&path=%s" % (pj, p), "/api/git/log?%s&path=%s" % (pj, p),
                 "/api/git/blame?%s&path=%s" % (pj, p), "/api/sessions/%s/git?diff=%s&untracked=1" % (sid, p),
                 "/api/sessions/%s/turns/file?path=%s" % (sid, p),
                 "/api/http/envs?%s&path=%s" % (pj, p)]
    turns = (get("/api/sessions/%s/turns" % sid)[1] or {}).get("turns") or []
    if turns:
        t = turns[-1]
        urls += ["/api/sessions/%s/turn?from=%s&to=%s" % (sid, t.get("from"), t.get("to")),
                 "/api/sessions/%s/turn?from=%s&to=%s&diff=." % (sid, t.get("from"), t.get("to"))]
        for p in paths:
            urls.append("/api/sessions/%s/turn?from=%s&to=%s&diff=%s" % (sid, t.get("from"), t.get("to"), p))
    check("the turn was snapshotted (turn diff endpoints exercised)", turns, turns)
    codes = {}
    human = ("/api/fs/", "/api/git/", "/api/sessions/%s/git" % sid, "/api/sessions/%s/turn" % sid, "/api/http/")
    for u in urls:
        st, _, b = get(u)
        codes[u] = st
        if not u.startswith(human):   # the human's file views show hidden files
            scan("GET " + u, b)
    st, j, b = get("/api/fs/read?%s&path=config/secrets.yml" % pj)
    check("GUI fs/read of a hidden file -> 200, marked hidden, audited (the human's IDE)",
          st == 200 and (j or {}).get("hidden") is True and audit_events(None, "gui-read-hidden"), (st, b[:200]))
    st, j, _ = get("/api/fs/grep?%s&q=PLANTED" % pj)
    check("GUI grep marks every hit in a hidden file", all(m.get("hidden") for m in (j or {}).get("matches") or []
                                                            if m.get("path") != "README.md"), j)
    check("fs/read of .git/config -> refused", get("/api/fs/read?%s&path=.git/config" % pj)[0] in (404, 409), "")
    st, _, _ = get("/api/fs/read?%s&path=.git/config&hidden=1" % pj)
    check("?hidden=1 never opens .git", st in (404, 409), st)
    # the session's own tools
    def approve_all(card):
        decide(card, True)
    for name, args in (("git_status", {}), ("git_diff", {}), ("git_diff", {"path": "config/secrets.yml"}),
                       ("git_log", {"n": 50}), ("piper_session", {}), ("http_envs", {}),
                       ("http_envs", {"path": "http-client/http-client.env.json"}), ("db_connections", {}),
                       ("git_run", {"args": ["status", "--porcelain", "--ignored"]}),
                       ("git_run", {"args": ["diff"]}), ("git_run", {"args": ["log", "-p", "-n", "5"]}),
                       ("git_run", {"args": ["grep", "-n", "PLANTED"]}),
                       ("git_run", {"args": ["show", "HEAD"]}),
                       ("git_run", {"args": ["ls-files", "--others"]}),
                       ("git_run", {"args": ["diff", "--no-index", "config/secrets.yml", "README.md"]}),
                       ("git_run", {"args": ["show", ":config/secrets.yml"]})):
        r = tool(sid, name, args, answer=approve_all)
        scan("tool %s %s" % (name, args), {k: v for k, v in r.items() if k != "_card"})
        if r.get("error", "").startswith("TIMEOUT"):
            check("tool %s answers" % name, False, r)
    # reading this project from another session (unlocked "projects")
    other = S.get("plain")
    if other and sess(other).get("state") in ("ready", "waiting", "working", "attention"):
        post("/api/sessions/%s/unlock" % other, {"what": "projects", "on": True})
        for name, args in (("list_dir", {"project": "e2e/leak", "path": "."}),
                           ("list_dir", {"project": "e2e/leak", "path": "config"}),
                           ("read_file", {"project": "e2e/leak", "path": "config/secrets.yml"}),
                           ("read_file", {"project": "e2e/leak", "path": "notes/private.txt"}),
                           ("read_file", {"project": "e2e/leak", "path": "Config/Secrets.yml"}),
                           ("read_file", {"project": "e2e/leak", "path": "./config/../config/secrets.yml"}),
                           ("grep", {"project": "e2e/leak", "pattern": "PLANTED"}),
                           ("grep", {"project": "e2e/leak", "pattern": "PLANTED", "path": "config"})):
            r = tool(other, name, args)
            scan("tool(other) %s %s" % (name, args), {k: v for k, v in r.items() if k != "_card"})
        post("/api/sessions/%s/unlock" % other, {"what": "projects", "on": False})
    lim = (get("/api/state")[1] or {}).get("limits") or {}
    check("limits read the planted (expired) login and never show its token", lim.get("status") == "expired", lim)
    for f in (os.path.join(PH, "limits.json"), os.path.join(PH, "audit.log"), os.path.join(WORK, "piperd.out")):
        name = os.path.basename(f)
        if os.path.exists(f):
            with open(f, "rb") as fh:
                b = fh.read()
            if b"PLANTED_" in b:   # the marker values; a bare "PLANTED" is the tests' own search pattern (git grep argv)
                i = b.index(b"PLANTED_")
                hits.append("%s: ...%s..." % (name, b[max(0, i - 80):i + 40].decode("utf-8", "replace")))
    check("no PLANTED in any GET answer or tool result (%d URLs)" % len(urls), not hits, "\n".join(hits[:12]))


@section("I7")
def i7():
    sid = S.get("leak") or ready_session("e2e/leak")
    if sess(sid).get("state") not in ("ready", "waiting", "working", "attention"):
        sid = ready_session("e2e/leak")
    p = os.path.join(PROJECTS["e2e/leak"], "new.key")
    with open(p, "w") as f:
        f.write("PLANTED_W\n")
    try:
        # L5 no longer changes the activity state (flag only) - the path goes on the
        # session's watch list, which the queue / feed show as a security item
        s = wait(lambda: (lambda x: x if any(w.endswith("/new.key") for w in x.get("watch") or []) else None)(sess(sid)), 15)
        check("L5: a new uncovered candidate -> on the session's watch list within 15 s", s, sess(sid))
        check("L5: the activity state is not hijacked (no attention)", s and s.get("state") != "attention", s and s.get("state"))
        check("audit l5-uncovered", audit_events(sid, "l5-uncovered"), "")
    finally:
        os.unlink(p)
    stop(sid)
    S.pop("leak", None)


@section("I10")
def i10():
    sid = S.get("plain")
    r = tool(sid, "git_run", {"args": ["status"]}, answer=lambda c: decide(c, False))
    check("git_run status: runs without a card (git_mode hole)", r.get("ok") and not r.get("_card"), r)
    risky = [["-c", "core.pager=sh", "log"], ["-C", "/tmp", "status"], ["--git-dir=/tmp/x", "status"],
             ["--work-tree=/tmp", "status"], ["diff", "--no-index", "/etc/hosts", "README.md"],
             # targets in WORK (the only writable dir of the test sandbox): a write there would really happen
             ["diff", "--output=%s" % os.path.join(WORK, "pe2e-out"), "HEAD"], ["push", "--force"],
             ["config", "core.sshCommand", "touch %s" % os.path.join(WORK, "pwn")]]
    for argv in risky:
        cards = []
        r = tool(sid, "git_run", {"args": argv}, answer=lambda c: (cards.append(c), decide(c, False)), timeout=20)
        carded = bool(cards)
        refused = not r.get("ok")
        check("git_run %s: a card or a refusal, never silently run (%s)" % (" ".join(argv), "card" if carded else
              (r.get("error") or "")[:80]), carded or refused, r)
    check("nothing written by --output", not os.path.exists(os.path.join(WORK, "pe2e-out")), "")
    check("no planted sshCommand ran", not os.path.exists(os.path.join(WORK, "pwn")), "")


@section("W1")
def w1():
    sid = S.get("plain")
    for name, args, kind in (("piper_allow_domain", {"domain": "evil.example.com"}, "allow_domain"),
                             ("piper_settings_change", {"section": "sandbox", "values": {"claude_dir": "host"}},
                              "settings_change")):
        cards, danger = [], []
        r = tool(sid, name, args, answer=lambda c: (cards.append(c), danger.append(feed_danger(c)), decide(c, False)))
        check("%s asks although auto_approve lists %s (ALWAYS_ASK)" % (name, kind),
              cards and cards[0]["kind"] == kind and not r.get("ok"), (cards, r))
        want = "settings_change" if kind == "settings_change" else None   # claude_dir=host weakens the isolation
        check("B-N3-1: queue Allow of %s: args.danger %r" % (kind, want), danger == [want], danger)
    # N9 (N8 decision 4): a name of this machine is a red card, the \u26a0 reason first
    for d in ("host.docker.internal", "api.localhost"):
        cards, danger = [], []
        tool(sid, "piper_allow_domain", {"domain": d}, answer=lambda c: (cards.append(c), danger.append(feed_danger(c)), decide(c, False)))
        first = (cards[0].get("summary") or "").split("\n")[0] if cards else ""
        check("N9: piper_allow_domain %s: red card (args.danger %s), \u26a0 first: %r" % (d, danger, first[:90]),
              danger == ["allow_domain"] and first.startswith("\u26a0") and "services on this machine" in first, (cards, danger))
    # B-N9-1: with the Feed off a dismissed queue row stays dismissed (it came back before).
    # D10 (9.37): the Feed follows the Global agent module, feed/config.json `enabled` is legacy
    agent_was = ((state().get("features") or {}).get("modules") or {}).get("agent", True)
    post("/api/features", {"modules": {"agent": False}})
    try:
        check("N9 B-N9-1: the Feed is off for this check", not (state().get("feed") or {}).get("on"), state().get("feed"))
        seen = []

        def dismiss_then_deny(c):
            key = "p:" + c["id"]
            in_q = lambda: key in [a["key"] for a in (state().get("queue") or {}).get("actions") or []]  # noqa: E731
            before = in_q()
            st, _, _ = post("/api/feed/dismiss", {"key": key})
            seen.append((before, st, in_q()))
            post("/api/feed/undismiss", {})
            decide(c, False)
        tool(sid, "piper_allow_domain", {"domain": "dismiss-check.example.com"}, answer=dismiss_then_deny)
        check("N9 B-N9-1: Feed off - a dismissed queue row is gone (in queue, dismiss status, still in queue: %s)" % seen,
              seen and seen[0] == (True, 200, False), seen)
    finally:
        post("/api/features", {"modules": {"agent": agent_was}})
    # B-N7-4: an invalid domain is refused before a card (it failed after Allow before)
    for d in ("api.evil\u202e.example", "*"):
        kind, r, _ = outcome(sid, "piper_allow_domain", {"domain": d}, approve=True)
        check("N7 B-N7-4: piper_allow_domain %r: refused without a card (got %s)" % (d, kind), kind == "refused", r)
    st, j, _ = get("/api/settings")
    pol = json.load(open(os.path.join(PH, "policy.json")))
    check("policy.json still lists them (written by hand) - the card is the guard", "allow_domain" in pol.get("auto_approve", []), pol)
    r = tool(sid, "piper_session", {})
    aa = ((r.get("result") or {}).get("auto_approve")) or []
    check("piper_session hides ALWAYS_ASK kinds from auto_approve %s" % aa, not set(aa) & set(ALWAYS_ASK), aa)


@section("W2")
def w2():
    sid = S.get("plain")
    got = []
    t = threading.Thread(target=lambda: got.append(tool(sid, "piper_allow_domain", {"domain": "a.example.com"})), daemon=True)
    t.start()
    card = wait(lambda: (pending(sid) or [None])[0], 10)
    check("card shown", card, "")
    if card:
        st1, _, _ = decide(card, False, always=True)   # "always" on a card that does not offer it: no approval
        st2, _, _ = decide(card, True)
        t.join(10)
        check("the first answer counts, a second one is 404 (%s/%s)" % (st1, st2), st1 == 200 and st2 == 404, (st1, st2))
        check("always on a card without 'Allow always' does not approve a deny", got and not got[0].get("ok"), got)
    ev = [e for e in audit_events(sid, "confirm") if e.get("kind") == "allow_domain"]
    check("confirm audited with answered", ev and "answered" in ev[-1], ev[-1:] if ev else ev)


@section("W5")
def w5():
    sid = S.get("plain")
    root = PROJECTS["e2e/plain"]
    with open(os.path.join(root, "src", "New.java"), "w") as f:
        f.write("class New {}\n")
    r = tool(sid, "git_commit", {"message": "add New", "files": ["src/New.java"]})
    check("git_commit (auto_approve) commits without a card", r.get("ok") and not r.get("_card"), r)
    check("the commit is on the host", "add New" in git(root, "log", "-1", "--format=%s"), "")
    cards = []
    r = tool(sid, "git_push", {}, answer=lambda c: (cards.append(c), decide(c, True)))
    check("git_push asks (not in auto_approve here) and pushes", cards and r.get("ok"), (cards, r))
    bare = os.path.join(WORK, "remotes", "e2e_plain.git")
    check("the bare remote has the commit", "add New" in sh("git", "--git-dir", bare, "log", "-1", "--format=%s", env=GIT_ENV), "")
    # a commit deleting a hidden (tracked) file: push refused before any card
    leak = S.get("leak2") or ready_session("e2e/leak")
    S["leak2"] = leak
    lroot = PROJECTS["e2e/leak"]
    # committed in the sandbox, where the file is over-mounted: deleted in the commit, still on the host's disk
    git(lroot, "rm", "-q", "--cached", "certs/tracked.pem")
    git(lroot, "commit", "-q", "-m", "drop tracked pem")
    cards = []
    r = tool(leak, "git_push", {}, answer=lambda c: (cards.append(c), decide(c, True)))
    check("git_push deleting a hidden file refused without a card", not r.get("ok") and not cards and
          "hidden" in (r.get("error") or ""), (r, cards))
    st, j, _ = post("/api/sessions/%s/git/push" % leak)
    check("GUI push of the same refused (409)", st == 409 and "hidden" in json.dumps(j), (st, j))
    git(lroot, "reset", "-q", "--hard", "HEAD~1")


@section("W8")
def w8():
    st, j, _ = get("/api/settings")
    secs = {s["id"]: s for s in (j or {}).get("sections") or []} if isinstance(j, dict) else {}
    check("GET /api/settings lists sections (%d)" % len(secs), len(secs) >= 10, list(secs))
    rev = (secs.get("sandbox") or {}).get("rev")   # claude_dir lives in sandbox since W14
    st, j, _ = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"}})
    check("weakening (claude_dir=host) -> 409 needs_confirm", st == 409 and (j or {}).get("needs_confirm"), (st, j))
    st, j, _ = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"}, "rev": "stale"})
    check("stale rev -> 409 conflict", st == 409 and (j or {}).get("conflict"), (st, j))
    st, j, _ = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "nonsense"}})
    check("invalid value -> 400", st == 400, (st, j))
    st, j, _ = post("/api/settings", {"section": "nope", "values": {}})
    check("unknown section -> 404", st == 404, (st, j))
    st, j, _ = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"}, "confirm": ["claude_dir"],
                                      "rev": rev})
    check("confirmed weakening saved", st == 200 and "claude_dir" in (j or {}).get("saved", []), (st, j))
    ev = [e for e in audit_events(None, "settings-change") if e.get("key") == "claude_dir"]
    check("audit settings-change confirmed", ev and ev[-1].get("confirmed") is True, ev[-1:])
    st, j, _ = post("/api/settings", {"section": "sandbox", "values": {}, "reset": ["claude_dir"]})
    check("reset back to default", st == 200, (st, j))
    pol = json.load(open(os.path.join(PH, "policy.json")))
    check("policy.json back without claude_dir and keeps the hand-written keys", "claude_dir" not in pol and
          pol.get("auto_approve"), pol)


@section("W9")
def w9():
    sid = S.get("plain")
    for name, args in (("read_file", {"project": "e2e/other", "path": "README.md"}), ("list_projects", {}),
                       ("edit_file", {"project": "e2e/other", "path": "README.md", "old_string": "hello", "new_string": "hi"}),
                       ("write_file", {"project": "e2e/other", "path": "x.txt", "content": "x"})):
        r = tool(sid, name, args)
        check("%s before the unlock -> NOT_UNLOCKED" % name, not r.get("ok") and "locked" in (r.get("error") or ""), r)
    post("/api/sessions/%s/unlock" % sid, {"what": "projects", "on": True})
    r = tool(sid, "read_file", {"project": "e2e/other", "path": "README.md"})
    check("read_file after the unlock", r.get("ok") and "hello" in json.dumps(r), r)
    r = tool(sid, "edit_file", {"project": "e2e/other", "path": "README.md", "old_string": "hello", "new_string": "hi"})
    check("edit_file still locked (projects_write)", not r.get("ok") and "locked" in (r.get("error") or ""), r)
    post("/api/sessions/%s/unlock" % sid, {"what": "projects_write", "on": True})
    cards = []
    r = tool(sid, "edit_file", {"project": "e2e/other", "path": "README.md", "old_string": "hello", "new_string": "hi"},
             answer=lambda c: (cards.append(c), decide(c, False)))
    check("edit_file after the unlock: a card with the diff; Deny -> nothing written", cards and not r.get("ok") and
          "hello" in open(os.path.join(PROJECTS["e2e/other"], "README.md")).read(), (cards, r))
    post("/api/sessions/%s/unlock" % sid, {"what": "projects", "on": False})
    r = tool(sid, "read_file", {"project": "e2e/other", "path": "README.md"})
    check("locking reading locks writing too", not r.get("ok"), r)
    st, j, _ = post("/api/sessions/%s/unlock" % sid, {"what": "nope", "on": True})
    check("unknown unlock -> 409", st == 409, st)


@section("W10")
def w10():
    sid = S.get("plain")
    st, j, _ = post("/api/sessions/%s/start-opt" % sid, {"what": "docker", "on": True})
    check("start option on a running session refused (409)", st == 409, (st, j))
    st, j, _ = post("/api/sessions", {"project": "e2e/plain", "opts": {"docker": "yes"}})
    check("opts must be booleans (409)", st == 409, (st, j))
    st, j, _ = post("/api/sessions", {"project": "e2e/plain", "opts": {"rootless": True}})
    check("unknown start option (409)", st == 409, (st, j))
    adv = S.get("advisor")
    if adv:
        st, j, _ = post("/api/sessions/%s/start-opt" % adv, {"what": "docker", "on": True})
        check("start option on the Global agent refused", st == 409, (st, j))
    ex = [x for x in state()["sessions"] if x["state"] == "exited" and x["kind"] == "project"]
    if ex:
        st, j, _ = post("/api/sessions/%s/start-opt" % ex[0]["id"], {"what": "docker", "on": True})
        check("start option on an exited session saved", st == 200 and (j or {}).get("opts", {}).get("docker"), (st, j))
        post("/api/sessions/%s/start-opt" % ex[0]["id"], {"what": "docker", "on": False})


@section("W11")
def w11():
    pj = "e2e/w11"
    sid = ready_session(pj)
    st, j, _ = get("/api/fs/read?project=%s&path=README.md" % pj)
    sha = (j or {}).get("sha")
    check("fs/read gives the sha lock", st == 200 and sha, (st, j))
    st, j, _ = post("/api/fs/write", {"project": pj, "path": "README.md", "text": "x\n", "sha": "0" * 64})
    check("fs/write with a stale sha -> 409", st == 409, (st, j))
    hook(sid, "UserPromptSubmit", prompt="work")
    wait_state(sid, "working", 5)
    # a save while a session of the tree works is no longer refused - the sha lock
    # holds, and the write is audited with while_working
    st, j, _ = post("/api/fs/write", {"project": pj, "path": "README.md", "text": "x\n", "sha": sha})
    check("fs/write while a session of the tree works -> 200, audited while_working",
          st == 200 and any(sid in (e.get("while_working") or []) for e in audit_events(None, "gui-write")), (st, j))
    sha = (j or {}).get("sha") or sha
    hook(sid, "Stop")
    wait_state(sid, "waiting", 5)
    st, j, _ = post("/api/fs/write", {"project": pj, "path": "README.md", "text": "# new\n", "sha": sha})
    check("fs/write with the right sha", st == 200, (st, j))
    st, j, _ = post("/api/fs/write", {"project": "e2e/leak", "path": "config/secrets.yml", "text": "x", "sha": "0" * 64})
    check("fs/write of a hidden file: the human's, but the sha lock holds -> 409", st == 409, (st, j))
    st, j, _ = post("/api/fs/write", {"project": pj, "path": ".git/config", "text": "x", "sha": "0" * 64})
    check("fs/write of .git/config refused", st in (404, 409), (st, j))
    st, j, _ = post("/api/fs/create", {"project": pj, "path": "src/Made.java", "kind": "file", "text": "class Made {}\n"})
    check("fs/create", st == 200, (st, j))
    st, j, _ = post("/api/fs/create", {"project": pj, "path": "src/Made.java", "kind": "file", "text": ""})
    check("fs/create of an existing file -> 409", st == 409, (st, j))
    st, j, _ = post("/api/fs/create", {"project": "e2e/leak", "path": "config/secrets/new.properties", "kind": "file"})
    check("fs/create inside a hidden dir: the human's -> 200", st == 200, (st, j))
    st, j, _ = post("/api/fs/delete", {"project": "e2e/leak", "path": "config/secrets/new.properties"})
    check("fs/delete of it", st == 200, (st, j))
    st, j, _ = post("/api/fs/rename", {"project": pj, "from": "src/Made.java", "to": "src/Made2.java"})
    check("fs/rename", st == 200, (st, j))
    st, j, _ = post("/api/fs/delete", {"project": pj, "path": "src/Made2.java"})
    trash = (j or {}).get("trash")
    check("fs/delete -> trash", st == 200 and trash and not os.path.exists(os.path.join(PROJECTS[pj], "src", "Made2.java")), (st, j))
    st, j, _ = post("/api/fs/restore", {"project": pj, "trash": trash or ""})
    check("fs/restore (Undo)", st == 200 and os.path.exists(os.path.join(PROJECTS[pj], "src", "Made2.java")), (st, j))
    st, j, _ = post("/api/fs/delete", {"project": pj, "path": ".git"})
    check("fs/delete .git refused", st in (404, 409) and os.path.isdir(os.path.join(PROJECTS[pj], ".git")), (st, j))
    st, j, _ = post("/api/fs/delete", {"project": "e2e/leak", "path": "config"})
    trash = (j or {}).get("trash")
    check("fs/delete of a dir holding hidden files: the human's -> Piper's trash", st == 200 and trash and
          not os.path.exists(os.path.join(PROJECTS["e2e/leak"], "config")), (st, j))
    live = [x["id"] for x in state()["sessions"] if x.get("project") == "e2e/leak"
            and x.get("state") not in ("exited", "error", "blocked")]
    st, j, _ = post("/api/fs/restore", {"project": "e2e/leak", "trash": trash or ""})
    check("manual fs/restore of hidden content remains available with live sessions", st == 200 and
          os.path.isfile(os.path.join(PROJECTS["e2e/leak"], "config", "secrets.yml")), (st, j))
    for x in live:
        check("manual restore does not stop the live session", sess(x).get("state") not in ("exited", "error"), sess(x).get("state"))
        stop(x)
    st, j, _ = post("/api/fs/rename", {"project": pj, "from": "README.md", "to": "../escape.md"})
    check("fs/rename out of the project refused", st in (404, 409), (st, j))
    stop(sid)


@section("L12")
def l12():
    parent = S.get("plain")
    st, _, _ = post("/api/sessions/%s/unlock" % parent, {"what": "projects", "on": False})
    r = tool(parent, "propose_session", {"project": "nope/nope", "task": "x"})
    check("propose_session of an unknown project -> error, no card", not r.get("ok") and not r.get("_card"), r)
    cards = []
    r = tool(parent, "propose_session", {"project": "e2e/other", "task": "do y"}, answer=lambda c: (cards.append(c), decide(c, False)))
    check("propose_session Deny -> error", cards and not r.get("ok") and "declined" in (r.get("error") or ""), r)
    cards = []
    r = tool(parent, "propose_session", {"project": "e2e/other", "task": "do z", "title": "child"},
             answer=lambda c: (cards.append(c), decide(c, True)))
    check("propose_session Allow -> a child session", cards and r.get("ok"), r)
    child = ((r.get("result") or {}).get("id"))
    if not child:
        return
    s = wait_state(child, ("blocked", "ready", "waiting", "working"), 20)
    if s.get("state") == "blocked":
        post("/api/sessions/%s/approve" % child)
    s = wait_state(child, ("ready", "working", "waiting"), 20)
    check("child origin handoff, parent set", s.get("origin") == "handoff" and s.get("parent") == parent, s)
    r = tool(child, "propose_session", {"project": "e2e/plain", "task": "grandchild"})
    check("a child cannot propose another session", not r.get("ok"), r)
    r = tool(child, "report_result", {"summary": "done: z"})
    check("report_result", r.get("ok"), r)
    r = tool(parent, "handoff_result", {})
    res = (r.get("result") or [])
    check("handoff_result: reported summary + Piper's git facts", res and res[0].get("reported") and
          res[0].get("summary_from_other_agent") == "done: z" and "branch" in (res[0].get("facts_from_piper") or {}), r)
    stop(child)


@section("I13")
def i13():
    adv = S.get("advisor")
    if not adv or sess(adv).get("state") not in ("ready", "waiting", "working", "attention"):
        adv = ready_session("advisor")
        S["advisor"] = adv
    r = tool(adv, "list_sessions", {})
    check("list_sessions without the agent itself", r.get("ok") and all(x["id"] != adv for x in r["result"]), r)
    r = tool(adv, "stop_session", {"id": adv})
    check("acting on itself refused", not r.get("ok") and "itself" in (r.get("error") or ""), r)
    r = tool(adv, "git_status", {})
    check("the agent has no repository", not r.get("ok"), r)
    cards = []
    r = tool(adv, "spawn_session", {"project": "e2e/other", "task": "t"}, answer=lambda c: (cards.append(c), decide(c, False)))
    check("spawn_session: card with the whole task; Deny -> nothing", cards and "t" in cards[0]["summary"] and not r.get("ok"), (cards, r))
    tgt = S.get("plain")
    hook(tgt, "Stop")
    wait_state(tgt, "waiting", 5)
    cards = []
    r = tool(adv, "send_to_session", {"id": tgt, "text": "hello-from-agent"}, answer=lambda c: (cards.append(c), decide(c, True)))
    check("send_to_session: card, then typed into the target", cards and r.get("ok") and
          wait(lambda: b"got:" in term(tgt, 1.0) and b"hello-from-agent" in term(tgt, 1.0), 6), r)
    r = tool(adv, "read_file", {"project": "e2e/leak", "path": "config/secrets.yml"})
    check("the agent reads no hidden file", not r.get("ok") or "PLANTED" not in json.dumps(r), r)
    r = tool(adv, "session_digest", {"id": tgt})
    check("session_digest answers", r.get("ok"), r)
    cards = []
    r = tool(adv, "stop_session", {"id": tgt}, answer=lambda c: (cards.append(c), decide(c, False)))
    check("stop_session: card; Deny keeps it running", cards and not r.get("ok") and sess(tgt).get("state") != "exited", r)


@section("L4")
def l4():
    sid = S.get("plain")
    before = sess(sid)
    pid = PROC["piperd"].pid
    kill_piperd(signal.SIGKILL)
    start_piperd()
    check("piperd restarted (new pid %s, was %s)" % (PROC["piperd"].pid, pid), PROC["piperd"].pid != pid, "")
    s = wait(lambda: (lambda x: x if x.get("state") in ("ready", "waiting", "working", "attention") else None)(sess(sid)), 15)
    check("the session survives a piperd restart (same pid)", s and s.get("pid") == before.get("pid"), (s, before))
    out = term(sid)
    check("the terminal replays after the restart", b"fake claude ready" in out, out[-200:])
    hook(sid, "UserPromptSubmit", prompt="after restart")
    check("hook events still drive the state", wait_state(sid, "working", 8).get("state") == "working", sess(sid))
    hook(sid, "Stop")
    wait_state(sid, "waiting", 5)


@section("L5")
def l5():
    sid = ready_session("e2e/corrupt")
    stop(sid)
    meta = os.path.join(PH, "sessions", sid, "meta.json")
    kill_piperd(signal.SIGTERM)
    with open(meta, "w") as f:
        f.write("{garbage")
    write_projects(garbage=True)
    start_piperd()
    s = sess(sid)
    check("a corrupt meta.json -> the session listed as error", s.get("state") == "error", s)
    check("meta.json.corrupt kept once", os.path.exists(meta + ".corrupt") or
          any(n.startswith("meta.json.corrupt") for n in os.listdir(os.path.dirname(meta))), os.listdir(os.path.dirname(meta)))
    check("projects.json.corrupt kept", any(n.startswith("projects.json.corrupt") for n in os.listdir(PH)), os.listdir(PH))
    st, _, _ = post("/api/sessions/%s/stop" % sid)
    st2, _, _ = post("/api/sessions/%s/forget" % sid)
    check("the error session can be stopped/removed (%s/%s)" % (st, st2), st == 200 and st2 == 200, "")
    write_projects()
    kill_piperd(signal.SIGTERM)
    start_piperd()
    n = [x for x in os.listdir(PH) if x.startswith("projects.json.corrupt")]
    check("the .corrupt copy is made once (%s)" % n, len(n) == 1, n)
    check("projects back", any(p["key"] == "e2e/plain" for p in state()["projects"]), "")


@section("I14")
def i14():
    sid = S.get("plain")
    if sid and sess(sid).get("state") in ("ready", "waiting", "working", "attention"):
        post("/api/sessions/%s/send" % sid, {"text": "password=PLANTED_S1 token: PLANTED_S2 https://u:PLANTED_S3@h.example/"})
        time.sleep(1.5)
    hits = []
    for root, dirs, files in os.walk(PH):
        dirs[:] = [d for d in dirs if d not in ("snapshots", "trash", "turns")]
        for n in files:
            if not (n.endswith((".log", ".jsonl", ".json", ".out")) or n in ("audit.log",)):
                continue
            p = os.path.join(root, n)
            try:
                with open(p, "rb") as f:
                    b = f.read()
            except OSError:
                continue
            for m in (b"PLANTED_S1", b"PLANTED_S2", b"PLANTED_S3", b"PLANTED_1", b"PLANTED_2", b"PLANTED_E", b"PLANTED_X"):
                if m in b:
                    hits.append("%s: %s" % (os.path.relpath(p, PH), m.decode()))
    for n in ("piperd.out",):
        with open(os.path.join(WORK, n), "rb") as f:
            b = f.read()
        if b"PLANTED" in b:
            hits.append(n)
    check("no planted secret in Piper's logs/state files (pty.log echo = %s)" %
          ("present" if any("pty.log" in h for h in hits) else "absent"), not [h for h in hits if "pty.log" not in h], hits)
    # pty.log is the raw terminal recording - like the scrollback it holds
    # what was typed; it stays owner-only: PIPER_HOME 0700
    check("PIPER_HOME and sessions/ are 0700 (pty.log = raw terminal recording)",
          os.stat(PH).st_mode & 0o077 == 0 and os.stat(os.path.join(PH, "sessions")).st_mode & 0o077 == 0,
          oct(os.stat(PH).st_mode))


@section("L11")
def l11():
    st = state()
    check("inbox poller ran (status set without a token)", (st.get("inbox") or {}).get("status") not in (None, ""), st.get("inbox"))
    with open(os.path.join(WORK, "piperd.out"), "rb") as f:
        out = f.read().decode("utf-8", "replace")
    tb = [ln for ln in out.splitlines() if "Traceback" in ln or "piperd: poll" in ln or "Exception" in ln]
    check("no traceback / poll error in piperd's output", not tb, "\n".join(tb[:10]))


# ================================================================== 2026-10-09: PLAN-ADD rows (N-I / N-L / N-W)
def feed_danger(card):
    """B-N3-1: args.danger of the card's Allow in the queue (/api/state `queue`, feed.derive_actions - the same
    rows the Feed drawer gets with the Feed on) - None when the Allow is an ordinary one."""
    acts = ((state() or {}).get("queue") or {}).get("actions") or []
    a = next((x for x in acts if x.get("key") == "p:" + card["id"]), None)
    allow = next((o for o in (a or {}).get("actions") or [] if o.get("op") == "decide" and o["args"].get("ok")), None)
    return (allow or {}).get("args", {}).get("danger", "<no feed row>" if a is None else None)


def outcome(sid, name, args, approve=False, timeout=30.0):
    """(card | auto | refused, result, card): every card is answered (deny unless approve)."""
    cards = []
    r = tool(sid, name, args, answer=lambda c: (cards.append(c), decide(c, approve)), timeout=timeout)
    if cards:
        return "card", r, cards[0]
    return ("auto" if r.get("ok") else "refused"), r, None


def has_planted(x) -> bool:
    b = x if isinstance(x, (bytes, bytearray)) else json.dumps(x, ensure_ascii=False).encode()
    return b"PLANTED_" in b


def pty_log(sid) -> bytes:
    p = os.path.join(sess_dir(sid), "pty.log")
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return b""


def live(project) -> list:
    return [x["id"] for x in state()["sessions"] if x.get("project") == project and x.get("state") in
            ("preflight", "blocked", "preparing", "spawning", "trust", "starting", "attesting", "ready", "working",
             "waiting", "attention", "stopping")]


def stop_all(project) -> None:
    for x in live(project):
        stop(x)


@section("N-I5")
def n_i5():
    """Token matrix (PLAN-ADD §1, N-I5, I9/I12 changed): every new route - GUI token answers, `piper attach`'s
    tty token gets 403; /api/state for tty strips the pending cards and carries no inbox / prod / feed / queue."""
    sid = S.get("plain")
    if not sid or sess(sid).get("state") not in ("ready", "waiting", "working", "attention"):
        sid = S["plain"] = ready_session("e2e/plain")
    pj = "project=e2e/plain"
    conv = (get("/api/conversations")[1] or {}).get("items") or [{}]
    cid = conv[0].get("session") or sid
    routes = [
        ("GET", "/api/sessions/%s/sandbox-file?path=/tmp/pe2e-none" % sid, None),
        ("GET", "/api/git/status?%s" % pj, None), ("GET", "/api/db/running?%s&connection=x" % pj, None),
        ("POST", "/api/db/cancel", {"project": "e2e/plain", "connection": "x", "owner": sid}),
        ("GET", "/api/conversations", None), ("GET", "/api/conversations/%s/preview" % cid, None),
        ("POST", "/api/conversations/%s/resume" % cid, {}), ("POST", "/api/conversations/%s/fork" % cid, {}),
        ("POST", "/api/conversations/local-x/import", {"project": "e2e/plain"}),
        ("GET", "/api/sessions/%s/review?scope=head" % sid, None),
        ("GET", "/api/sessions/%s/review/blob?review=x&path=README.md&side=left" % sid, None),
        ("GET", "/api/sessions/%s/review/check?review=x&path=README.md" % sid, None),
        ("GET", "/api/sessions/%s/review/history?review=x&path=README.md" % sid, None),
        ("GET", "/api/sessions/%s/review/config" % sid, None),
        ("POST", "/api/sessions/%s/review/stage" % sid, {"review": "x", "path": "README.md"}),
        ("POST", "/api/sessions/%s/review/write" % sid, {"review": "x", "path": "README.md", "text": "x", "sha": "0" * 64}),
        ("POST", "/api/sessions/%s/review/dispatch" % sid, {"text": "x", "target": "current", "items": []}),
        ("POST", "/api/sessions/%s/git/commit" % sid, {"message": "m", "files": ["README.md"]}),
        ("GET", "/api/fs/blob?%s&path=README.md&tree=HEAD" % pj, None),
        ("GET", "/api/fs/list?%s&path=.&all=1" % pj, None), ("GET", "/api/fs/read?%s&path=README.md" % pj, None),
        ("GET", "/api/fs/files?%s&all=1" % pj, None), ("GET", "/api/fs/grep?%s&q=hello&all=1" % pj, None),
        ("GET", "/api/sessions/%s/git" % sid, None), ("GET", "/api/sessions/%s/turns" % sid, None),
        ("GET", "/api/sessions/%s/turn?from=x&to=y" % sid, None), ("GET", "/api/sessions/%s/conversation" % sid, None),
        ("GET", "/api/sessions/%s/diagnostics" % sid, None), ("GET", "/api/sessions/%s/draft" % sid, None),
        ("GET", "/api/conversations/search?q=x", None), ("GET", "/api/stack/logs?%s" % pj, None),
        ("GET", "/api/stack/job?id=x", None), ("GET", "/api/http/runs?%s" % pj, None),
        ("GET", "/api/http/envs?%s" % pj, None), ("GET", "/api/http/files?%s" % pj, None),
        ("GET", "/api/inbox", None), ("GET", "/api/inbox/item?key=x", None), ("GET", "/api/feed", None),
        ("GET", "/api/feed/chat", None), ("GET", "/api/gitlab/issues?project=e2e/gl", None),
        ("GET", "/api/gitlab/issue?project=e2e/gl&iid=7", None), ("GET", "/api/idea/problems?%s&path=README.md" % pj, None),
        ("GET", "/api/git/log?%s" % pj, None)]
    for m, path, body in routes:
        st, j, _ = req(m, path, body, tok="tty")
        check("tty: %s %s -> 403" % (m, path.split("?")[0].replace(sid, "<id>").replace(cid, "<conv>")), st == 403, (st, j))
    # the GUI token gets an answer on the read routes that need no state (never 401/403)
    for m, path, body in routes:
        if m != "GET":
            continue
        st, _, b = req(m, path, body)
        check("GUI: GET %s -> not 401/403 (%s)" % (path.split("?")[0].replace(sid, "<id>").replace(cid, "<conv>"), st),
              st not in (401, 403), (st, b[:200]))
    for path in ("/api/git/status?%s" % pj, "/api/conversations", "/api/db/running?%s" % pj,
                 "/api/fs/blob?%s&path=README.md&tree=HEAD" % pj, "/api/sessions/%s/review?scope=head" % sid,
                 "/api/sessions/%s/review/config" % sid):
        st, j, b = get(path)
        check("GUI: GET %s -> 200" % path.split("?")[0].replace(sid, "<id>"), st == 200, (st, b[:300]))
    # /api/state for tty with a pending card: id/session/title/kind only, no inbox/prod/feed/queue
    got = []
    t = threading.Thread(target=lambda: got.append(tool(sid, "piper_allow_domain", {"domain": "tty-check.example.com"})),
                         daemon=True)
    t.start()
    card = wait(lambda: (pending(sid) or [None])[0], 10)
    st, j, _ = get("/api/state", tok="tty")
    pend = (j or {}).get("pending") or []
    check("tty /api/state: the card is there, stripped to id/session/title/kind",
          card and pend and all(set(p) <= {"id", "session", "title", "kind"} for p in pend), pend)
    check("tty /api/state: inbox / prod / feed / queue empty",
          all((j or {}).get(k) == {} for k in ("inbox", "prod", "feed", "queue")), {k: (j or {}).get(k) for k in ("inbox", "feed")})
    st, j, _ = get("/api/state")
    full = [p for p in (j or {}).get("pending") or [] if card and p["id"] == card["id"]]
    check("GUI /api/state: the same card with its summary", full and "summary" in full[0], full)
    if card:
        decide(card, False)
    t.join(10)
    flags = (j or {}).get("gui_flags") or {}
    check("gui_flags carry the new keys (all default on)", all(flags.get(k) is True for k in (
        "restore_view", "term_click_opens_files", "nav_history")), flags)


@section("N4-TTY")
def n4_tty():
    """N4 review: EVERY GET route piperd has, with `piper attach`'s tty token, against a session that has a task,
    a prompt and a git_run in the audit log. Open to tty: ping, state (sessions whitelisted, cards stripped), doctor
    (no deep), mattermost / timesheet settings (config), usage, stack (service list), idea status; every other
    route 403 - and no answer carries the session's task / prompt / argv."""
    sid = ready_session("e2e/plain", task="PLANTED_N4_TASK")
    hook(sid, "UserPromptSubmit", prompt="PLANTED_N4_PROMPT")
    wait(lambda: sess(sid).get("state") == "working", 10)
    tool(sid, "git_run", {"args": ["log", "--grep=PLANTED_N4_ARGV", "-n", "1"]}, answer=lambda c: decide(c, False))
    pj = "project=e2e/plain"
    open_ = {"/api/ping", "/api/state", "/api/doctor", "/api/mattermost", "/api/timesheet/settings", "/api/usage",
             "/api/stack", "/api/idea/status"}
    routes = ["/api/ping", "/api/state", "/api/doctor", "/api/doctor?deep=1", "/api/settings", "/api/mattermost",
              "/api/timesheet", "/api/timesheet/settings", "/api/usage", "/api/audit", "/api/board", "/api/prod",
              "/api/inbox", "/api/inbox/item?key=x", "/api/feed", "/api/feed/chat", "/api/gitlab/issues?%s" % pj,
              "/api/gitlab/issue?%s&iid=1" % pj, "/api/conversations", "/api/conversations/x/preview",
              "/api/conversations/search?q=PLANTED", "/api/sessions/%s/git" % sid, "/api/sessions/%s/sandbox-file?path=/tmp/x" % sid,
              "/api/sessions/%s/draft" % sid, "/api/sessions/%s/conversation" % sid, "/api/sessions/%s/turns" % sid,
              "/api/sessions/%s/turn?from=x&to=y" % sid, "/api/sessions/%s/diagnostics" % sid,
              "/api/sessions/%s/turns/file?path=README.md" % sid, "/api/sessions/%s/review" % sid,
              "/api/sessions/%s/review/config" % sid, "/api/fs/list?%s&path=." % pj, "/api/fs/read?%s&path=README.md" % pj,
              "/api/fs/grep?%s&q=x" % pj, "/api/fs/files?%s" % pj, "/api/fs/marks?%s&path=README.md" % pj,
              "/api/fs/symbols?%s&name=x" % pj, "/api/fs/raw?%s&path=README.md" % pj, "/api/fs/blob?%s&path=README.md" % pj,
              "/api/git/diff?%s&path=README.md" % pj, "/api/git/log?%s" % pj, "/api/git/blame?%s&path=README.md" % pj,
              "/api/git/graph?%s" % pj, "/api/git/show?%s&commit=HEAD" % pj, "/api/git/status?%s" % pj,
              "/api/git/ci?%s" % pj, "/api/git/mr-draft?%s" % pj, "/api/stack?%s" % pj, "/api/stack/job?id=x",
              "/api/stack/logs?%s" % pj, "/api/idea/status", "/api/idea/problems?%s&path=README.md" % pj,
              "/api/idea/symbol?%s&path=README.md" % pj, "/api/db/all", "/api/db/connections?%s" % pj,
              "/api/db/meta?%s" % pj, "/api/db/history?%s" % pj, "/api/db/status", "/api/db/running?%s" % pj,
              "/api/http/envs?%s" % pj, "/api/http/envcfg?%s" % pj, "/api/http/files?%s" % pj, "/api/http/runs?%s" % pj,
              "/api/http/run?id=x"]
    table = []
    for path in routes:
        st, j, b = get(path, tok="tty")
        keys = sorted(j)[:8] if isinstance(j, dict) else type(j).__name__
        table.append((path.split("?")[0].replace(sid, "<id>"), st, keys))
        name = path.split("?")[0].replace(sid, "<id>") + ("?deep=1" if "deep=1" in path else "")
        if name in open_:
            check("tty GET %s -> 200, no session content" % name, st == 200 and b"PLANTED_" not in b, (st, b[:300]))
        else:
            check("tty GET %s -> 403" % name, st == 403 and b"PLANTED_" not in b, (st, b[:200]))
    with open(os.path.join(WORK, "n4-tty-routes.json"), "w") as f:
        json.dump(table, f, indent=1)
    st, j, _ = get("/api/state")
    me = next((x for x in (j or {}).get("sessions") or [] if x["id"] == sid), {})
    check("GUI /api/state keeps the task (first / last prompt)", me.get("task") == me.get("first_prompt") == "PLANTED_N4_TASK",
          {k: me.get(k) for k in ("task", "first_prompt", "last_prompt", "state")})
    stop(sid)


@section("N-I8")
def n_i8():
    """I8 split (PLAN-ADD §9): the agent's paths (tools, MCP mailbox) and the tty token never carry PLANTED_*;
    the GUI token may (the human's IDE) - but never `.git`."""
    sid = ready_session("e2e/leak")
    pj = "project=e2e/leak"
    urls = ["/api/fs/read?%s&path=config/secrets.yml" % pj, "/api/fs/grep?%s&q=PLANTED&all=1" % pj,
            "/api/fs/list?%s&path=config&all=1" % pj, "/api/git/status?%s" % pj,
            "/api/sessions/%s/git" % sid, "/api/sessions/%s/review?scope=head" % sid, "/api/state", "/api/inbox",
            "/api/conversations", "/api/feed", "/api/fs/raw?%s&path=config/secrets.yml" % pj,
            "/api/fs/blob?%s&path=config/secrets.yml&tree=HEAD" % pj, "/api/http/envs?%s&path=http-client/http-client.env.json" % pj]
    tty_hits, gui_ok = [], 0
    for u in urls:
        st, _, b = get(u, tok="tty")
        if b"PLANTED_" in b:
            tty_hits.append(u)
        st2, _, b2 = get(u)
        gui_ok += b"PLANTED_" in b2
    check("tty token: no PLANTED_* in any answer (%d URLs)" % len(urls), not tty_hits, tty_hits)
    check("GUI token: hidden content shown to the human (%d URLs carry it)" % gui_ok, gui_ok >= 2, gui_ok)
    for u in ("/api/fs/read?%s&path=.git/config" % pj, "/api/fs/blob?%s&path=.git/config&tree=HEAD" % pj,
              "/api/fs/list?%s&path=.git&all=1" % pj, "/api/fs/read?%s&path=.Git/config" % pj):
        st, _, b = get(u)
        check("GUI: %s refused (.git never)" % u.split("&path=")[1].split("&")[0], st in (400, 404, 409, 413, 415), (st, b[:120]))
    hits = []
    for name, args in (("git_status", {}), ("git_diff", {}), ("git_log", {"n": 20}), ("list_dir", {"path": "."}),
                       ("list_dir", {"path": "config"}), ("read_file", {"path": "config/secrets.yml"}),
                       ("read_file", {"path": "notes/private.txt"}), ("grep", {"pattern": "PLANTED"}),
                       ("git_run", {"args": ["grep", "PLANTED"]}), ("git_run", {"args": ["log", "-p", "--all"]}),
                       ("git_run", {"args": ["show", "HEAD", "--", "certs/tracked.pem"]}),
                       ("session_digest", {}), ("piper_session", {}), ("http_last_result", {}),
                       ("http_envs", {"path": "http-client/http-client.env.json"})):
        r = tool(sid, name, args, answer=lambda c: decide(c, True), timeout=20)
        if has_planted({k: v for k, v in r.items() if k != "_card"}):
            hits.append("%s %s" % (name, args))
    check("agent tools: no PLANTED_* in any result", not hits, hits)
    stop(sid)


@section("N-I1")
def n_i1():
    """review routes + review_dispatch (N-I1, N-I2, N-I3 server side, N-W1 reviewed commit, N-W2/W3, fs/blob)."""
    pj, root = "e2e/rev", PROJECTS["e2e/rev"]
    sid = ready_session(pj)
    hook(sid, "SessionStart", session_id="7e57a0b1-1111-2222-3333-000000000001")
    hook(sid, "UserPromptSubmit", session_id="7e57a0b1-1111-2222-3333-000000000001", prompt="edit")
    time.sleep(0.6)
    with open(os.path.join(root, "code.txt"), "w") as f:
        f.write("after\n")
    with open(os.path.join(root, "notes.txt"), "a") as f:
        f.write("PLANTED_RV2\n")
    if not os.path.lexists(os.path.join(root, "link.txt")):
        os.symlink("notes.txt", os.path.join(root, "link.txt"))
    hook(sid, "Stop", session_id="7e57a0b1-1111-2222-3333-000000000001")
    wait_state(sid, "waiting", 5)
    time.sleep(1.0)
    st, rv, b = get("/api/sessions/%s/review?scope=head" % sid)
    check("GET review scope=head (Uncommitted) -> 200", st == 200 and rv and rv.get("id"), (st, b[:300]))
    rows = {r["path"]: r for r in (rv or {}).get("files") or []}
    check("review rows: code.txt, notes.txt (marked sensitive), link.txt", {"code.txt", "notes.txt", "link.txt"} <= set(rows)
          and rows.get("notes.txt", {}).get("sensitive") is True, sorted(rows))
    for scope in ("session", "branch", "turn"):
        st, j, b = get("/api/sessions/%s/review?scope=%s" % (sid, scope))
        check("GET review scope=%s -> 200 or a reason (409)" % scope, st in (200, 409), (st, b[:200]))
    rid = (rv or {}).get("id", "")
    st, j, _ = get("/api/sessions/%s/review/blob?review=%s&path=code.txt&side=right" % (sid, rid))
    check("review/blob right side", st == 200 and "after" in json.dumps(j), (st, j))
    st, j, _ = get("/api/sessions/%s/review/blob?review=%s&path=notes.txt&side=right" % (sid, rid))
    check("review/blob of a hidden file: the human's (GUI)", st == 200 and "PLANTED_RV2" in json.dumps(j), st)
    st, j, _ = get("/api/sessions/%s/review/check?review=%s&path=code.txt" % (sid, rid))
    check("review/check", st == 200, (st, j))
    st, j, _ = get("/api/sessions/%s/review/history?review=%s&path=code.txt" % (sid, rid))
    check("review/history", st == 200, (st, j))
    st, j, _ = get("/api/sessions/%s/review/config" % sid)
    check("review/config: 6 default actions", st == 200 and len((j or {}).get("actions") or []) >= 6, j)
    st, j, _ = get("/api/sessions/%s/review/blob?review=%s&path=.git/config&side=right" % (sid, rid))
    check("review/blob of .git refused", st in (400, 404, 409), (st, j))
    st, j, _ = get("/api/fs/blob?project=%s&path=code.txt&tree=HEAD" % pj)
    check("fs/blob HEAD", st == 200 and "before" in json.dumps(j), (st, j))
    st, j, _ = get("/api/fs/blob?project=%s&path=.git/HEAD&tree=HEAD" % pj)
    check("fs/blob .git refused", st in (400, 404, 409), (st, j))
    st, j, _ = get("/api/git/status?project=%s" % pj)
    files = {f["path"]: f for f in (j or {}).get("files") or []}
    check("git/status without a session: notes.txt marked sensitive, code.txt plain",
          st == 200 and files.get("notes.txt", {}).get("sensitive") and not files.get("code.txt", {}).get("sensitive"), j)
    before_log = len(pty_log(sid))
    # dispatch: the mix, each refused alone, nothing typed
    bad = [("hidden", [{"path": "notes.txt", "sha": rows.get("notes.txt", {}).get("work_sha")}], "Look at it"),
           ("..", [{"path": "../e2e/plain/README.md", "sha": "0" * 64}], "Look"),
           ("stale sha", [{"path": "code.txt", "sha": "0" * 64}], "Look"),
           ("prompt names a hidden file", [{"path": "code.txt", "sha": rows.get("code.txt", {}).get("work_sha")}],
            "Compare with notes.txt")]
    for label, items, text in bad:
        for target in ("current", "new"):
            n = len(state()["sessions"])
            st, j, _ = post("/api/sessions/%s/review/dispatch" % sid, {"text": text, "target": target, "items": items})
            check("dispatch %s (%s) -> refused" % (label, target), st in (400, 404, 409) and len(state()["sessions"]) == n, (st, j))
    # a symlink to a hidden file: its name is lexical (PLAN-ADD N-I1) - the row is the link, its blob the link text;
    # it may go to the session, the target's content never does
    st, j, _ = post("/api/sessions/%s/review/dispatch" % sid, {"text": "Check the link e2e-link", "target": "current", "edit": False,
                                                               "items": [{"path": "link.txt", "sha": rows.get("link.txt", {}).get("work_sha")}]})
    check("dispatch of a symlink (named lexically) -> 202, no target content typed", st == 202 and
          wait(lambda: b"e2e-link" in pty_log(sid), 6) and b"PLANTED_" not in pty_log(sid)[before_log:], (st, j))
    hook(sid, "Stop", session_id="7e57a0b1-1111-2222-3333-000000000001")
    wait_state(sid, "waiting", 5)
    st, j, _ = post("/api/sessions/%s/review/dispatch" % sid, {"text": "Mix", "target": "current", "items": [
        {"path": "code.txt", "sha": rows.get("code.txt", {}).get("work_sha")},
        {"path": "notes.txt", "sha": rows.get("notes.txt", {}).get("work_sha")}]})
    check("dispatch mix visible + hidden -> refused as a whole", st == 409, (st, j))
    time.sleep(0.5)
    check("nothing of the refused dispatches reached the terminal", b"PLANTED_" not in pty_log(sid)[before_log:]
          and b"Compare with notes" not in pty_log(sid)[before_log:], pty_log(sid)[before_log:][-300:])
    # a visible target goes (current): typed into the session's terminal
    st, j, _ = post("/api/sessions/%s/review/dispatch" % sid, {"text": "Explain code.txt e2e-review", "target": "current",
                                                               "edit": False, "items": [{"path": "code.txt", "sha": rows.get("code.txt", {}).get("work_sha")}]})
    check("dispatch of a visible target -> 202, typed into the session", st == 202 and
          wait(lambda: b"e2e-review" in pty_log(sid), 6), (st, j))
    hook(sid, "Stop", session_id="7e57a0b1-1111-2222-3333-000000000001")
    wait_state(sid, "waiting", 5)
    # N-W1: reviewed commit
    st, j, _ = post("/api/sessions/%s/git/commit" % sid, {"message": "no review", "files": ["code.txt"]})
    check("GUI commit without `reviewed` refused", st in (400, 409), (st, j))
    st, rv, _ = get("/api/sessions/%s/review?scope=head" % sid)
    rows = {r["path"]: r for r in (rv or {}).get("files") or []}
    reviewed = {"review": rv["id"], "head": rv["head"], "versions": {"code.txt": rows["code.txt"]["after"]}}
    with open(os.path.join(root, "code.txt"), "w") as f:
        f.write("changed by the agent after the review\n")
    st, j, _ = post("/api/sessions/%s/git/commit" % sid, {"message": "stale", "files": ["code.txt"], "reviewed": reviewed})
    check("reviewed commit after the file changed -> refused", st == 409 and "changed" in json.dumps(j), (st, j))
    with open(os.path.join(root, "code.txt"), "w") as f:
        f.write("after\n")
    st, j, _ = post("/api/sessions/%s/git/commit" % sid, {"message": "reviewed", "files": ["code.txt"], "reviewed": reviewed},
                    tok="tty")
    check("reviewed commit with the tty token -> 403", st == 403, st)
    st, j, _ = post("/api/sessions/%s/git/commit" % sid, {"message": "reviewed e2e", "files": ["code.txt"], "reviewed": reviewed})
    check("reviewed commit of the pinned blob", st == 200 and "reviewed e2e" in git(root, "log", "-1", "--format=%s"), (st, j))
    # N-W3 stage / N-W2 write
    with open(os.path.join(root, "other.txt"), "w") as f:
        f.write("other changed\n")
    st, rv, _ = get("/api/sessions/%s/review?scope=head" % sid)
    rows = {r["path"]: r for r in (rv or {}).get("files") or []}
    st, j, _ = post("/api/sessions/%s/review/stage" % sid, {"review": rv["id"], "path": "other.txt"})
    check("review/stage (Add to Git) of the pinned blob", st == 200 and "other.txt" in git(root, "diff", "--cached", "--name-only"), (st, j))
    git(root, "reset", "-q", "--", "other.txt")
    st, j, _ = post("/api/sessions/%s/review/write" % sid, {"review": rv["id"], "path": "other.txt", "text": "unrelated\n",
                                                           "sha": rows["other.txt"]["work_sha"]})
    check("review/write (Revert) exact bytes", st == 200 and open(os.path.join(root, "other.txt")).read() == "unrelated\n", (st, j))
    st, j, _ = post("/api/sessions/%s/review/write" % sid, {"review": rv["id"], "path": "other.txt", "text": "x", "sha": "0" * 64})
    check("review/write with a stale sha refused", st == 409, (st, j))
    # N-I2: a rename hidden -> visible committed on a branch (Entire branch scope)
    git(root, "checkout", "-q", "-b", "feature")
    git(root, "add", "-A", "--", "code.txt", "other.txt")
    git(root, "mv", "notes.txt", "public.txt")
    git(root, "commit", "-q", "-m", "rename hidden on the branch", "--allow-empty")
    st, rb, b = get("/api/sessions/%s/review?scope=branch" % sid)
    brow = next((r for r in (rb or {}).get("files") or [] if r["path"] == "public.txt"), {})
    check("branch review: public.txt renamed from notes.txt", st == 200 and brow.get("orig") == "notes.txt", (st, b[:300]))
    before_log = len(pty_log(sid))
    for target in ("current", "new"):
        n = len(state()["sessions"])
        st, j, _ = post("/api/sessions/%s/review/dispatch" % sid, {"text": "Original side:\nPLANTED_RV1", "target": target,
                                                                   "edit": False, "items": [{"path": "public.txt", "sha": brow.get("work_sha")}]})
        check("N-I2 dispatch of a branch rename from a hidden file (%s) -> refused" % target,
              st == 409 and len(state()["sessions"]) == n, (st, j))
    check("N-I2 nothing reached the terminal", b"PLANTED_" not in pty_log(sid)[before_log:], "")
    st, j, _ = get("/api/sessions/%s/review/blob?review=%s&path=public.txt&side=left" % (sid, (rb or {}).get("id")))
    check("branch review blob left of the rename: the human sees the hidden original", st == 200, (st, j))
    git(root, "checkout", "-q", "-f", "main")
    stop(sid)


@section("N-W8")
def n_w8():
    """The default-on keys (5 since D10 / D1) (PLAN-ADD §3): on by default; off saves without a confirm; on again: the two
    git_run keys weaken (409 needs_confirm "Weakening the isolation"), the GUI ones do not. + N-L11 wait_min."""
    keys = ("nav_history", "term_click_opens_files", "restore_view",
            "git_run_readonly_extra", "git_run_output_in_repo")
    st, j, _ = get("/api/settings")
    where = {}
    for sec in (j or {}).get("sections") or []:
        for f in sec.get("fields") or []:
            where[f.get("key")] = (sec["id"], f)
    for k in keys:
        sec, f = where.get(k, (None, {}))
        check("%s in Settings, default on" % k, sec and f.get("default") is True and f.get("value") is True, f)
        if not sec:
            continue
        st, j, _ = post("/api/settings", {"section": sec, "values": {k: False}})
        check("%s off -> saved without a confirm" % k, st == 200, (st, j))
        st, j, _ = post("/api/settings", {"section": sec, "values": {k: True}})
        weak = k.startswith("git_run_")
        if weak:
            check("%s on again -> 409 needs_confirm (weakening)" % k, st == 409 and k in json.dumps((j or {}).get("needs_confirm")), (st, j))
            st, j, _ = post("/api/settings", {"section": sec, "values": {k: True}, "confirm": [k]})
            check("%s on with the confirm -> saved" % k, st == 200, (st, j))
        else:
            check("%s on again -> saved (a GUI key does not weaken)" % k, st == 200, (st, j))
    pol = json.load(open(os.path.join(PH, "policy.json")))
    check("policy.json: all keys on again", all(pol.get(k, True) is True for k in keys), {k: pol.get(k) for k in keys})
    ev = [e for e in audit_events(None, "settings-change") if e.get("key") == "git_run_readonly_extra"]
    check("audit: the confirmed weakening", any(e.get("confirmed") for e in ev), ev[-2:])
    # N-L11: an old feed config with wait_min loads; the field is gone
    feed = next((s for s in (get("/api/settings")[1] or {}).get("sections") or [] if s["id"] == "feed"), {})
    check("feed: an old config with wait_min loads (no section error)", feed and not feed.get("error"), feed.get("error"))
    check("feed: no wait_min field", not any(f.get("key") == "wait_min" for f in feed.get("fields") or []),
          [f.get("key") for f in feed.get("fields") or []])
    # R10: console_optimizer_hints default
    sec, f = where.get("console_optimizer_hints", (None, {}))
    check("console_optimizer_hints: default recorded (%s) - disabled by default" % f.get("default"), sec is not None, where.keys())


@section("N-I13")
def n_i13():
    """git_run card table (PLAN-ADD §4, N-I13..N-I17, N-J10..N-J13): each argv -> card / no card / refused;
    nothing PLANTED in an answer. .git open (K1): object reads of a hidden file need no card - recorded for the
    user's decision; .git hidden (excludes [".git"]): they ask (R1, fixed 2026-10-09)."""
    pj, root = "e2e/gitm", PROJECTS["e2e/gitm"]
    sid = ready_session(pj)
    oid = git(root, "rev-parse", "HEAD:config/secrets/db.properties").strip()
    os.makedirs(os.path.join(root, "development"), exist_ok=True)
    os.makedirs(os.path.join(root, "target"), exist_ok=True)
    patches = {
        "ok.patch": "--- a/README.md\n+++ b/README.md\n@@ -1,2 +1,3 @@\n # plain\n hello\n+patched e2e\n",
        "hidden.patch": "--- a/config/secrets/db.properties\n+++ b/config/secrets/db.properties\n@@ -1 +1,2 @@\n"
                        " password=PLANTED_G1\n+x=1\n",
        "renfrom.patch": "diff --git a/config/secrets/db.properties b/src/db.properties\nsimilarity index 100%\n"
                         "rename from config/secrets/db.properties\nrename to src/db.properties\n",
        "git.patch": "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1,2 @@\n [core]\n+\tx = 1\n"}
    for n, t in patches.items():
        with open(os.path.join(root, "development", n), "w") as f:
            f.write(t)
    if not os.path.lexists(os.path.join(root, "development", "link.patch")):
        os.symlink("ok.patch", os.path.join(root, "development", "link.patch"))
    sib = os.path.join(os.path.dirname(root), "gitm-work")
    table = [
        # AUTO_SUBS / READONLY_SUBS / scoped grep - no card
        (["status"], "auto"), (["log", "-n", "3", "--oneline"], "auto"), (["diff"], "auto"), (["show", "--stat", "HEAD"], "auto"),
        (["rev-parse", "HEAD"], "auto"), (["merge-base", "HEAD", "HEAD"], "auto"), (["ls-files"], "auto"),
        (["describe", "--always"], "auto"), (["for-each-ref"], "auto"), (["count-objects"], "auto"), (["version"], "auto"),
        (["grep", "TODO"], "auto"), (["grep", "PLANTED"], "auto"), (["grep", "PLANTED", "HEAD"], "auto"),
        (["grep", "-e", "password", "--", "config/*"], "auto"), (["reflog", "-n", "2"], "auto"), (["remote", "-v"], "auto"),
        (["symbolic-ref", "HEAD"], "auto"), (["ls-remote", "origin"], "auto"), (["worktree", "list"], "auto"),
        (["archive", "--format=tar", "--output=target/src.tar", "HEAD"], "auto"),
        (["apply", "development/ok.patch"], "auto"),
        # always a card
        (["diff", "--output=target/d.patch"], "card"), (["diff", "--color=always"], "card"),
        (["blame", "--contents", "README.md", "README.md"], "card"), (["log", "--line-prefix=>"], "card"),
        (["log", "--output-indicator-new=X", "-p"], "card"), (["blame", "--ignore-revs-file", "README.md", "README.md"], "card"),
        (["show", "HEAD:config/secrets/db.properties"], "card"), (["archive", "--format=tar", "--output=.claude/x.tar", "HEAD"], "card"),
        (["archive", "--format=tgz", "HEAD"], "card"), (["reset", "--hard"], "card"), (["commit", "--amend", "-m", "x"], "card"),
        (["reflog", "expire", "--all"], "card"), (["remote", "add", "x", "https://example.org/x.git"], "card"),
        (["worktree", "add", ".mvn/x"], "card"), (["worktree", "prune"], "card"), (["-C", "/tmp", "status"], "card"),
        (["diff", "--no-index", "README.md", "TODO.md"], "card"), (["config", "core.sshCommand", "id"], "card"),
        (["push", "--force"], "card"),
        # refused without a card
        (["-c", "core.pager=sh", "log"], "refused"), (["push", "origin", "HEAD:release"], "refused"),
        (["apply", "development/hidden.patch"], "refused"), (["apply", "development/renfrom.patch"], "refused"),
        (["apply", "development/git.patch"], "refused"), (["apply", "config/secrets/db.properties"], "refused"),
        (["apply", "development/link.patch"], "refused"),
    ]
    got_rows = []
    for argv, want in table:
        kind, r, card = outcome(sid, "git_run", {"args": argv}, timeout=25)
        if want == "card" and kind == "refused":
            kind = "refused"   # a refusal is stricter than a card - checked below as its own value
        got_rows.append((argv, want, kind))
        ok = kind == want or (want == "card" and kind == "refused")
        check("git_run %s -> %s (got %s)" % (" ".join(argv), want, kind), ok, (r.get("error") or "")[:200])
        check("git_run %s: no PLANTED in the answer" % " ".join(argv), not has_planted({k: v for k, v in r.items() if k != "_card"}),
              json.dumps(r)[:300])
    # grep -f <hidden file>: the argv names a hidden file -> a card; even approved, the run refuses to read it
    # N7 (B-N6-4): that card allowed nothing - now refused before it, like grep of a revision:path
    for argv in (["grep", "-f", "config/secrets/db.properties"], ["grep", "-n", "class", "HEAD:src"], ["grep", "-n", "x", "HEAD:"]):
        kind, r, _ = outcome(sid, "git_run", {"args": argv}, approve=True)
        check("N7 B-N6-4: git_run %s: refused before a card (got %s)" % (" ".join(argv), kind),
              kind == "refused" and not has_planted(r), r)
    kind, r, _ = outcome(sid, "git_run", {"args": ["grep", "-n", "class", "HEAD:src"]})
    check("N7 B-N6-4: the refusal says what to write instead", "HEAD -- src" in (r.get("error") or ""), r)
    git(root, "checkout", "-q", "--", "README.md")
    # N7 (B-N6-1): taking a changed hidden file into git -> a red card (queue row args.danger), reason first as ⚠
    hid = os.path.join(root, "config", "secrets", "db.properties")
    with open(hid) as f:
        hid_before = f.read()
    with open(hid, "w") as f:
        f.write("password=PLANTED_N7_WORKTREE\n")
    try:
        for argv in (["commit", "-am", "x"], ["stash"], ["add", "-A"]):
            cards, danger = [], []
            r = tool(sid, "git_run", {"args": argv}, answer=lambda c: (cards.append(c), danger.append(feed_danger(c)), decide(c, False)))
            line2 = (cards[0].get("summary") or "").split("\n")[1:2] if cards else []
            check("N7 B-N6-1: git_run %s with a changed hidden file: queue Allow args.danger git_run (got %s)" % (" ".join(argv), danger),
                  danger == ["git_run"], danger)
            check("N7 B-N6-1: git_run %s: the card's first line under the command is the ⚠ reason" % " ".join(argv),
                  bool(line2) and line2[0].startswith("⚠ ") and "hidden file" in line2[0], line2)
        kind, r, card = outcome(sid, "git_run", {"args": ["add", "does-not-exist"]})
        summ = (card or {}).get("summary") or ""
        check("N7 B-N6-2: add does-not-exist: card text says the check failed, no \"(?)\"",
              kind == "card" and "(?)" not in summ and "could not check" in summ, summ[-300:])
        cards, danger = [], []
        tool(sid, "git_run", {"args": ["add", "README.md"]}, answer=lambda c: (cards.append(c), danger.append(feed_danger(c)), decide(c, False)))
        check("N7 B-N6-1: add of a visible file stays without a card", cards == [], cards)
    finally:
        with open(hid, "w") as f:
            f.write(hid_before)
        git(root, "reset", "-q")
    check("N7 B-N6-1: nothing PLANTED_N7 reached .git", b"PLANTED_N7" not in subprocess.run(
        ["git", "-C", root, "cat-file", "--batch-all-objects", "--batch"], capture_output=True).stdout, "")
    # N10: the agent's commit (K1) named by a message search `:/<text>` or as a tree before the paths (no `--`)
    # rewrote the hidden file card-free - the check appended ^{commit} to the string / looked for a tree after `--` only
    blob = subprocess.run(["git", "-C", root, "hash-object", "-w", "--stdin"], input=b"password=N10_AGENT\n",
                          capture_output=True, env=GIT_ENV).stdout.decode().strip()
    idx = dict(GIT_ENV, GIT_INDEX_FILE=os.path.join(root, ".git", "n10-index"))
    sh("git", "-C", root, "read-tree", "HEAD", env=idx)
    sh("git", "-C", root, "update-index", "--cacheinfo", "100644,%s,config/secrets/db.properties" % blob, env=idx)
    tree = sh("git", "-C", root, "write-tree", env=idx).strip()
    os.unlink(os.path.join(root, ".git", "n10-index"))
    git(root, "branch", "-f", "n10evil", sh("git", "-C", root, "commit-tree", tree, "-p", "HEAD", "-m", "n10 innocent",
                                           env=GIT_ENV).strip())
    try:
        for argv in (["checkout", ":/n10 innocent"], ["merge", ":/n10 innocent"], ["checkout", "n10evil:", "config"]):
            kind, r, _ = outcome(sid, "git_run", {"args": argv})
            with open(hid) as f:
                kept = f.read() == hid_before
            check("N10: git_run %s -> card (got %s), the hidden file unchanged" % (" ".join(argv), kind),
                  kind == "card" and kept, (kind, (r.get("error") or json.dumps(r))[:200]))
    finally:
        git(root, "checkout", "-q", "main", check_rc=False)
        git(root, "branch", "-q", "-D", "n10evil", check_rc=False)
    # object reads of a hidden file with .git open (K1): record what happens (user's decision, R1)
    recorded = {}
    for argv in (["cat-file", "-p", oid], ["show", oid], ["archive", "--format=tar", "HEAD"], ["ls-tree", "-r", "HEAD"],
                 ["rev-list", "--objects", "--all"], ["range-diff", "HEAD...HEAD"]):
        kind, r, _ = outcome(sid, "git_run", {"args": argv})
        recorded[" ".join(argv[:2])] = (kind, has_planted(r))
    check("R1 (.git open, K1) recorded: cat-file/show <blob> runs without a card and returns the hidden content "
          "(%s)" % recorded, True, recorded)
    S["r1_open"] = recorded
    # worktree lifecycle (N-I17, N-J11): sibling add, -C, remove, prune -n
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "add", "../gitm-work", "-b", "feat-e2e"]})
    check("worktree add ../<repo>-work (sibling) -> no card", kind == "auto" and os.path.isdir(sib), (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["-C", "../gitm-work", "status"]})
    check("-C <verified linked worktree> status -> no card", kind == "auto", (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "prune", "-n"]})
    check("worktree prune -n -> no card", kind == "auto", (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "add", "../../e2e-elsewhere"]})
    check("worktree add outside the sibling level -> card or refused", kind in ("card", "refused"), (kind, r))
    with open(os.path.join(sib, "dirty.txt"), "w") as f:
        f.write("dirty\n")
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "remove", "../gitm-work"]})
    check("worktree remove of a dirty one: git refuses, the content stays", os.path.exists(os.path.join(sib, "dirty.txt")), (kind, r))
    os.unlink(os.path.join(sib, "dirty.txt"))
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "remove", "--force", "../gitm-work"]})
    check("worktree remove --force -> card", kind in ("card", "refused") and os.path.isdir(sib), (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["worktree", "remove", "../gitm-work"]})
    check("worktree remove (clean, no session in it) -> no card, removed", kind == "auto" and not os.path.isdir(sib), (kind, r))
    git(root, "branch", "-q", "-D", "feat-e2e", check_rc=False)
    stop(sid)
    # .git hidden from the session (project excludes ".git"): object reads ask (R1 fix)
    pj2, root2 = "e2e/githid", PROJECTS["e2e/githid"]
    sid2 = ready_session(pj2)
    oid2 = git(root2, "rev-parse", "HEAD:config/secrets/db.properties").strip()
    for argv in (["cat-file", "-p", oid2], ["show", oid2], ["archive", "--format=tar", "HEAD"], ["ls-tree", "-r", "HEAD"],
                 ["rev-list", "--objects", "--all"], ["range-diff", "HEAD...HEAD"]):
        kind, r, _ = outcome(sid2, "git_run", {"args": argv})
        check("R1 .git hidden: git %s -> card (got %s), nothing PLANTED" % (" ".join(argv[:2]), kind),
              kind == "card" and not has_planted(r), (kind, json.dumps(r)[:200]))
    kind, r, _ = outcome(sid2, "git_run", {"args": ["status"]})
    check("R1 .git hidden: git status still without a card", kind == "auto", (kind, r))
    stop(sid2)


@section("N-I18")
def n_i18():
    """Mounted read-write library (N-I18, N-W11, W9 changed): list_projects without an unlock = the libraries,
    read/edit/write/git without a card; a hidden file of the library never; write_exec for a build file; an
    unrelated project NOT_UNLOCKED."""
    sid = ready_session("e2e/libuser")
    s = sess(sid)
    lib = PROJECTS["e2e/lib"]
    r = tool(sid, "list_projects", {})
    keys = [p.get("key") for p in (r.get("result") or [])] if r.get("ok") else []
    check("list_projects without an unlock -> the mounted library only", r.get("ok") and keys == ["e2e/lib"], r)
    r = tool(sid, "read_file", {"project": "e2e/lib", "path": "src/Lib.java"})
    check("read_file in the library, no unlock", r.get("ok"), r)
    kind, r, _ = outcome(sid, "edit_file", {"project": "e2e/lib", "path": "src/Lib.java", "old_string": "class Lib",
                                            "new_string": "class Lib2"})
    check("edit_file in the rw library -> no card", kind == "auto" and "Lib2" in open(os.path.join(lib, "src/Lib.java")).read(), (kind, r))
    kind, r, _ = outcome(sid, "write_file", {"project": "e2e/lib", "path": "src/New.java", "content": "class New {}\n"})
    check("write_file in the rw library -> no card", kind == "auto", (kind, r))
    kind, r, card = outcome(sid, "write_file", {"project": "e2e/lib", "path": "pom.xml", "content": "<x/>\n"})
    check("write_file of a build file (pom.xml) -> write_exec card", kind == "card" and card["kind"] == "write_exec", (kind, card))
    for name, args in (("read_file", {"project": "e2e/lib", "path": "config/secrets/lib.properties"}),
                       ("grep", {"project": "e2e/lib", "pattern": "PLANTED"}),
                       ("list_dir", {"project": "e2e/lib", "path": "config/secrets"}),
                       ("write_file", {"project": "e2e/lib", "path": "config/secrets/lib.properties", "content": "x"}),
                       ("read_file", {"project": "e2e/lib", "path": ".git/config"})):
        r = tool(sid, name, args)
        check("%s %s: hidden / .git of the library never" % (name, args.get("path") or args.get("pattern")),
              not has_planted(r) and (name != "write_file" or not r.get("ok")) and (".git" not in str(args.get("path")) or not r.get("ok")), r)
    check("the hidden library file is unchanged", open(os.path.join(lib, "config/secrets/lib.properties")).read() == "k=PLANTED_8\n", "")
    r = tool(sid, "read_file", {"project": "e2e/plain", "path": "README.md"})
    check("an unrelated project -> NOT_UNLOCKED", not r.get("ok") and "locked" in (r.get("error") or ""), r)
    kind, r, _ = outcome(sid, "git_run", {"args": ["status"], "project": "e2e/lib"})
    check("git_run project=<rw library> status -> no card", kind == "auto", (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["checkout", "-b", "lib-e2e"], "project": "e2e/lib"})
    check("git_run project=<rw library> checkout -b -> no card", kind == "auto", (kind, r))
    kind, r, _ = outcome(sid, "git_run", {"args": ["commit", "--amend", "-m", "x"], "project": "e2e/lib"})
    check("git_run project=<rw library> --amend -> card", kind == "card", (kind, r))
    git(lib, "checkout", "-q", "main")
    git(lib, "branch", "-q", "-D", "lib-e2e", check_rc=False)
    git(lib, "checkout", "-q", "--", "src/Lib.java")
    try:
        os.unlink(os.path.join(lib, "src", "New.java"))
    except OSError:
        pass
    _ = s
    stop(sid)


def transcript(cfg_dir, conv, words, cwd=None, sub="-e2e") -> str:
    d = os.path.join(cfg_dir, "projects", sub)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, conv + ".jsonl")
    rows = []
    for i, (role, text) in enumerate(words):
        r = {"type": role, "sessionId": conv, "timestamp": "2026-10-09T0%d:00:00Z" % (i % 10),
             "message": {"role": role, "content": [{"type": "text", "text": text}]}}
        if cwd:
            r["cwd"] = cwd
        rows.append(r)
    rows.append({"type": "ai-title", "sessionId": conv, "aiTitle": "E2E ‮invoice‬ repair"})
    with open(p, "w") as f:
        f.write("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


@section("N-L1")
def n_l1():
    """Conversations (N-L1..N-L4, finding 1 + 2): list, fulltext, preview, exact resume, missing transcript ->
    an error (never a fresh conversation), fork, import of a CLI conversation, the limit / a double click /
    Stop on resume, bidi-free titles."""
    pj, root = "e2e/conv", PROJECTS["e2e/conv"]
    sid = ready_session(pj)
    conv = "c0ffee00-1111-2222-3333-444455556666"
    hook(sid, "SessionStart", session_id=conv)
    wait(lambda: sess(sid).get("claude_session") == conv, 5)
    transcript(box(sid), conv, [("user", "Fix the invoice zebrafish rounding"), ("assistant", "Rounded with HALF_EVEN, tests pass.")])
    hook(sid, "UserPromptSubmit", session_id=conv, prompt="Fix the invoice zebrafish rounding")
    hook(sid, "Stop", session_id=conv)
    wait_state(sid, "waiting", 5)
    stop(sid)
    rows = wait(lambda: [r for r in (get("/api/conversations?q=zebrafish")[1] or {}).get("items") or [] if r["session"] == sid], 15)
    check("conversations: fulltext finds the closed session's conversation", rows, get("/api/conversations?q=zebrafish")[1])
    row = (rows or [{}])[0]
    check("conversation title without bidi controls (finding 2)", not any(c in json.dumps(row, ensure_ascii=False)
                                                                           for c in "‪‫‬‭‮⁦⁧⁨⁩"), row)
    st, j, _ = get("/api/conversations/%s/preview" % sid)
    check("preview: messages, available", st == 200 and (j or {}).get("available") and "HALF_EVEN" in json.dumps(j), (st, j))
    check("preview without bidi controls", "‮" not in json.dumps(j, ensure_ascii=False), "")
    st, j, _ = get("/api/conversations?project=e2e/conv&agent=claude&period=today")
    check("filters project / agent / period", st == 200, (st, j))
    st, j, _ = get("/api/conversations?agent=gpt")
    check("an invalid filter -> 409/400", st in (400, 409), st)
    # resume of the ended session: the exact conversation
    st, j, _ = post("/api/conversations/%s/resume" % sid)
    s = wait_state(sid, ("ready", "error", "exited"), 20)
    spawns = audit_events(sid, "spawn")
    argv = (spawns[0] if spawns else {}).get("argv") or []
    check("resume -> ready with --resume <conversation>", st == 200 and s.get("state") == "ready" and "--resume" in argv and conv in argv, (st, j, argv))
    st, j, _ = post("/api/conversations/%s/resume" % sid)
    check("resume of a running session -> opens it (running)", st == 200 and (j or {}).get("running"), (st, j))
    # fork: a second session, the original kept
    n = len(state()["sessions"])
    st, j, _ = post("/api/conversations/%s/fork" % sid)
    child = (j or {}).get("session")
    check("fork -> a new session", st == 200 and child and child != sid and len(state()["sessions"]) == n + 1, (st, j))
    if child:
        c = wait_state(child, ("blocked", "ready", "error", "exited"), 20)
        if c.get("state") == "blocked":
            post("/api/sessions/%s/approve" % child)
            c = wait_state(child, ("ready", "error", "exited"), 20)
        sp = (audit_events(child, "spawn") or [{}])[0].get("argv") or []
        check("fork starts with --resume <conv> --fork-session", "--fork-session" in sp and conv in sp, sp)
        check("the original transcript is kept", os.path.exists(os.path.join(box(sid), "projects", "-e2e", conv + ".jsonl")), "")
        stop(child)
    stop(sid)
    # finding 1: resume at the limit, a double click, Stop right after
    running = [x for x in state()["sessions"] if x["state"] in ("preflight", "ready", "working", "waiting", "attention",
                                                                   "starting", "attesting", "spawning", "preparing", "trust")]
    extra = []
    try:
        for k in ("e2e/lim1", "e2e/lim2", "e2e/lim3", "e2e/other", "e2e/corrupt", "e2e/w11"):
            if len(running) + len(extra) >= 6:
                break
            extra.append(ready_session(k))
        st, j, _ = post("/api/conversations/%s/resume" % sid)
        check("resume at the session limit -> 409 limit (finding 1)", st == 409 and "limit" in json.dumps(j), (st, j))
        check("...and the session stays exited", sess(sid).get("state") == "exited", sess(sid))
    finally:
        for x in extra:
            post("/api/sessions/%s/stop" % x)
        for x in extra:
            wait_state(x, ("exited", "error"), 15)
    res = []
    spawns_before = audit_events(sid, "spawn")
    ts = [threading.Thread(target=lambda: res.append(post("/api/conversations/%s/resume" % sid)[:2])) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    wait_state(sid, ("ready", "error"), 20)
    time.sleep(1.0)
    check("a double resume starts one sandbox, the second click opens it (finding 1): answers %s" % [r[0] for r in res],
          [r[0] for r in res] == [200, 200] and sum(1 for r in res if (r[1] or {}).get("running")) == 1
          and len([e for e in audit_events(sid, "spawn")]) == len(spawns_before) + 1, res)
    stop(sid)
    post("/api/conversations/%s/resume" % sid)
    st, _, _ = post("/api/sessions/%s/stop" % sid)
    s = wait_state(sid, ("exited", "error"), 20)
    check("Stop right after a resume ends it (never lost)", s.get("state") == "exited", s)
    # the transcript gone -> an explicit error, never a fresh conversation
    os.rename(os.path.join(box(sid), "projects", "-e2e", conv + ".jsonl"), os.path.join(box(sid), "projects", "-e2e", "gone.bak"))
    n_spawn = len(audit_events(sid, "spawn"))
    st, j, _ = post("/api/conversations/%s/resume" % sid)
    check("resume with the transcript gone -> 409 'missing', no start", st == 409 and "missing" in json.dumps(j)
          and sess(sid).get("state") == "exited" and len(audit_events(sid, "spawn")) == n_spawn, (st, j))
    # import of a CLI conversation outside Piper (N-L3)
    ext = "e0e0e0e0-aaaa-bbbb-cccc-000000000002"
    tp = transcript(os.path.join(REAL, ".claude"), ext, [("user", "Outside piper narwhal question"),
                                                         ("assistant", "narwhal answer; config/secrets/db.properties says PLANTED_IMPORT")],
                    cwd=root, sub="-ext")
    with open(os.path.join(REAL, ".claude", "settings.json"), "w") as f:
        f.write('{"hooks": "PLANTED_HOOKS must not copy"}')
    before = open(tp, "rb").read()
    st, j, _ = get("/api/conversations?q=narwhal")
    check("external history is opt-in (external=0 does not list it)", not [r for r in (j or {}).get("items") or [] if r.get("source") == "local"], j)
    rows = wait(lambda: [r for r in (get("/api/conversations?external=1&q=narwhal")[1] or {}).get("items") or []
                         if r.get("source") == "local"], 15)
    check("external=1 lists the CLI conversation", rows, get("/api/conversations?external=1&q=narwhal")[1])
    if rows:
        lid = rows[0]["session"]
        st, j, _ = post("/api/conversations/%s/import" % lid, {"project": "e2e/plain"})
        check("import into another checkout without confirm_checkout -> refused", st == 409 and "checkout" in json.dumps(j), (st, j))
        # R2 (2026-10-10, the user's decision): a transcript mentioning / reading a hidden path asks first
        st, j, _ = post("/api/conversations/%s/import" % lid, {"project": "e2e/conv"})
        check("R2: import of a transcript naming a hidden path -> 409 hidden_paths (names only), nothing created",
              st == 409 and (j or {}).get("hidden_paths") == ["config/secrets/db.properties"]
              and "PLANTED_IMPORT" not in json.dumps(j), (st, j))
        st, j, _ = post("/api/conversations/%s/import" % lid, {"project": "e2e/conv", "confirm_hidden": "true"})
        check("R2: confirm_hidden must be a real true", st == 409 and (j or {}).get("hidden_paths"), (st, j))
        st, j, _ = post("/api/conversations/%s/import" % lid, {"project": "e2e/conv", "confirm_hidden": True})
        imp = (j or {}).get("session")
        check("import into its own checkout, confirmed -> a new session (fork of the copy)", st == 200 and imp, (st, j))
        if imp:
            check("the CLI transcript is untouched", open(tp, "rb").read() == before, "")
            copied = [os.path.join(dp, n) for dp, _, fs in os.walk(sess_dir(imp)) for n in fs]
            contents = {}
            for p in copied:
                if not os.path.isfile(p) or os.path.islink(p):
                    continue
                try:
                    with open(p, "rb") as f:
                        contents[p] = f.read()
                except FileNotFoundError:
                    pass   # a live session atomically replaces/removes its temporary metadata files
            check("only the transcript copied, never the CLI's settings/hooks",
                  not any(b"PLANTED_HOOKS" in data for data in contents.values()), copied[:20])
            carried = [p for p, data in contents.items() if b"PLANTED_IMPORT" in data]
            S["r2_import"] = [os.path.relpath(p, sess_dir(imp)) for p in carried]
            check("R2 (user's decision): confirmed, the transcript is copied unscrubbed into the session's claude dir: %s"
                  % S["r2_import"], bool(S["r2_import"]), "")
            c = wait_state(imp, ("blocked", "ready", "error", "exited"), 20)
            if c.get("state") == "blocked":
                post("/api/sessions/%s/approve" % imp)
                wait_state(imp, ("ready", "error", "exited"), 20)
            stop(imp)


@section("N-I26")
def n_i26():
    """Native subagent hooks (N-I26, N-L5): IDs / types only, never a payload; the parent runs until the last
    one stops; a repeated start counts once; the IDs survive a piperd restart."""
    sid = ready_session("e2e/other")
    conv = "5ab5ab00-1111-2222-3333-000000000001"
    hook(sid, "SessionStart", session_id=conv)
    wait(lambda: sess(sid).get("claude_session") == conv, 5)
    hook(sid, "UserPromptSubmit", session_id=conv, prompt="go")
    wait_state(sid, "working", 5)
    for aid in ("sa-1", "sa-2", "sa-1"):
        hook(sid, "SubagentStart", session_id=conv, agent_id=aid, agent_type="reviewer",
             prompt="PLANTED_SA1 secret task", last_assistant_message="PLANTED_SA2", transcript_path="/x/PLANTED_SA3")
    hook(sid, "Stop", session_id=conv)
    time.sleep(1.0)
    s = sess(sid)
    check("two subagents running (a repeated start counts once): %s" % s.get("subagents"),
          len(s.get("subagents") or {}) == 2, s.get("subagents"))
    check("the parent stays working while subagents run", s.get("state") == "working", s.get("state"))
    st, _, b = get("/api/state")
    check("no subagent payload in /api/state", b"PLANTED_SA" not in b, "")
    kill_piperd(signal.SIGKILL)
    start_piperd()
    s = wait(lambda: sess(sid) if sess(sid).get("subagents") else None, 15) or sess(sid)
    check("subagent IDs survive a piperd restart", set((s.get("subagents") or {})) == {"sa-1", "sa-2"}, s.get("subagents"))
    hook(sid, "SubagentStop", session_id=conv, agent_id="sa-1")
    time.sleep(0.8)
    check("one stopped: still working", sess(sid).get("state") == "working", sess(sid).get("state"))
    hook(sid, "SubagentStop", session_id=conv, agent_id="sa-2")
    s = wait_state(sid, "waiting", 6)
    check("the last one stopped: waiting", s.get("state") == "waiting", s)
    hook(sid, "SubagentStart", session_id="foreign-session-id", agent_id="sa-x", agent_type="x")
    time.sleep(0.8)
    check("a subagent event of another conversation is ignored", not sess(sid).get("subagents"), sess(sid).get("subagents"))
    hits = []
    for dp, _, fs in os.walk(PH):
        for n in fs:
            if n.endswith((".json", ".log")) and "sessions" in dp and n != "piper-events.jsonl":
                try:
                    if b"PLANTED_SA" in open(os.path.join(dp, n), "rb").read():
                        hits.append(os.path.join(dp, n))
                except OSError:
                    pass
    check("no subagent payload in Piper's state files", not hits, hits)
    stop(sid)


@section("N-I21")
def n_i21():
    """Codex config.toml (N-I21, N-I22, R3): danger-full-access + approval never; a remote MCP of the host's
    template arrives with its plain header only; no credential, no private / metadata / gateway URL; the
    host config is unchanged."""
    host = os.path.join(REAL, ".codex", "config.toml")
    before = open(host, "rb").read()
    sid = ready_session("e2e/codex", agent="codex")
    cfg = os.path.join(sess_dir(sid), "codex", "config.toml")
    if not os.path.exists(cfg):
        cands = [os.path.join(dp, "config.toml") for dp, _, fs in os.walk(sess_dir(sid)) if "config.toml" in fs]
        cfg = cands[0] if cands else cfg
    text = open(cfg).read() if os.path.exists(cfg) else ""
    check("generated config.toml found", text, cfg)
    check("sandbox_mode = danger-full-access, approval_policy = never",
          'sandbox_mode = "danger-full-access"' in text and 'approval_policy = "never"' in text, text[:400])
    check("the remote MCP (grafana) arrives with its plain header", "mcp-grafana.example.test" in text and "X-Region" in text, text)
    check("no credential header from the host template (R3)", "PLANTED_H" not in text, text)
    check("no private / metadata / gateway URL (R3)", not any(x in text for x in ("169.254.169.254", "10.1.2.3", "host.internal")), text)
    check("no stdio MCP of the host", "localjava" not in text, text)
    check("the host ~/.codex/config.toml unchanged", open(host, "rb").read() == before, "")
    tpl = os.path.join(PH, "codex-template", "config.toml")
    check("the template is the host's copy (credentials stay host-side)", os.path.exists(tpl), tpl)
    stop(sid)


@section("N-I20")
def n_i20():
    """sandbox-file (N-I20), db/running + db/cancel owner (N-W10, the route part; a live query: dbprod / T3)."""
    sid = S.get("plain")
    if not sid or sess(sid).get("state") not in ("ready", "waiting", "working", "attention"):
        sid = S["plain"] = ready_session("e2e/plain")
    name = "/tmp/pe2e-%d.log" % os.getpid()   # the agent container's /tmp = FAKE_DOCKER_TMP (fake-docker.py)
    os.makedirs(os.path.join(FAKED, "tmp"), exist_ok=True)
    host = os.path.join(FAKED, "tmp", os.path.basename(name))
    with open(host, "w") as f:
        f.write("sandbox scratch line\n")
    try:
        st, _, b = get("/api/sessions/%s/sandbox-file?path=%s" % (sid, name))
        check("sandbox-file of /tmp/x.log in a running session -> its text", st == 200 and b"sandbox scratch" in b, (st, b[:200]))
        check("audit gui-sandbox-read", audit_events(sid, "gui-sandbox-read"), "")
        for p in ("/tmp/../etc/passwd", "/etc/hosts", "tmp/x", "/tmp"):
            st, _, b = get("/api/sessions/%s/sandbox-file?path=%s" % (sid, p))
            check("sandbox-file %s -> 409" % p, st == 409, (st, b[:200]))
        with open(host, "wb") as f:
            f.write(b"\x00\x01binary")
        st, _, b = get("/api/sessions/%s/sandbox-file?path=%s" % (sid, name))
        check("sandbox-file of a binary file -> 409", st == 409, (st, b[:120]))
        with open(host, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024 + 10))
        st, _, b = get("/api/sessions/%s/sandbox-file?path=%s" % (sid, name))
        check("sandbox-file > 2 MB -> 409", st == 409, (st, b[:120]))
        st, _, _ = get("/api/sessions/%s/sandbox-file?path=%s" % (sid, name), tok="tty")
        check("sandbox-file with the tty token -> 403", st == 403, st)
        ex = next((x["id"] for x in state()["sessions"] if x["state"] == "exited" and x["kind"] == "project"), None)
        if ex:
            st, _, b = get("/api/sessions/%s/sandbox-file?path=%s" % (ex, name))
            check("sandbox-file of an ended session -> 409", st == 409, (st, b[:120]))
    finally:
        os.unlink(host)
    st, j, _ = get("/api/db/running?project=e2e/plain&connection=")
    check("db/running -> 200 {running: [...]}", st == 200 and isinstance((j or {}).get("running"), list), (st, j))
    st, j, _ = post("/api/db/cancel", {"project": "e2e/plain", "connection": "x", "owner": sid})
    check("db/cancel owner=<session> with nothing running -> 200 cancelled 0", st == 200 and (j or {}).get("cancelled") == 0, (st, j))
    st, j, _ = post("/api/db/cancel", {"project": "e2e/plain", "connection": "x", "owner": "no-such-session"})
    check("db/cancel owner=<unknown> -> 404", st == 404, (st, j))
    st, j, _ = post("/api/db/cancel", {"project": "e2e/plain", "connection": "x"})
    check("db/cancel owner '' (the GUI's own) -> 200", st == 200, (st, j))


@section("N-I19")
def n_i19():
    """GitLab against the fake API (N-I19, N-W5, N-W6, N-W7, N-I24, N3, finding 3, N-L8): upload scope / 10 MiB /
    hidden files, the exact snapshot sent; job play / retry / running / protected; titles scrubbed for agents."""
    pj, root = "e2e/gl", PROJECTS["e2e/gl"]
    gl_token(True)
    try:
        n_i19_body(pj, root)
    finally:
        gl_token(False)


def n_i19_body(pj, root):
    sid = ready_session(pj)
    # N3
    for name in ("gitlab_issues", "gitlab_milestones"):
        r = tool(sid, name, {"page": "x"})
        check("%s page:'x' -> a clear tool error (N3)" % name, not r.get("ok") and (r.get("error") or "").startswith("page must be")
              and "invalid literal" not in r.get("error", ""), r)
    # upload: a visible file, the card shows name/size/sha256; the bytes approved are the bytes sent
    path = os.path.join(root, "development", "shot.txt")
    raw = open(path, "rb").read()
    import hashlib
    sha = hashlib.sha256(raw).hexdigest()

    def swap_then_allow(card):
        with open(path, "w") as f:
            f.write("SWAPPED while the card waited\n")
        decide(card, True)
    n0 = len(gl_calls(lambda c: c["path"].endswith("/uploads")))
    cards = []
    r = tool(sid, "gitlab_upload_file", {"path": "development/shot.txt"}, answer=lambda c: (cards.append(c), swap_then_allow(c)))
    card = cards[0] if cards else {}
    check("upload: a card gitlab_upload_file with name, size, sha256", card.get("kind") == "gitlab_upload_file" and sha in card.get("summary", "")
          and "shot.txt" in card.get("summary", ""), card)
    ups = gl_calls(lambda c: c["path"].endswith("/uploads"))
    check("upload: the approved snapshot was sent (not the file swapped during the card)",
          len(ups) == n0 + 1 and ups[-1].get("multipart_sha") == sha, ups[-1:])
    check("upload: Markdown returned, no comment created", r.get("ok") and "markdown" in json.dumps(r)
          and not gl_calls(lambda c: c["m"] == "POST" and c["path"].endswith("/notes")), r)
    with open(path, "wb") as f:
        f.write(raw)
    n0 = len(gl_calls(lambda c: c["path"].endswith("/uploads")))
    kind, r, card = outcome(sid, "gitlab_upload_file", {"path": "development/shot.txt"}, approve=False)
    check("upload Deny -> no POST", kind == "card" and len(gl_calls(lambda c: c["path"].endswith("/uploads"))) == n0, (kind, r))
    with open(os.path.join(root, "development", "big.bin"), "wb") as f:
        f.write(b"\0" * ((10 << 20) + 1))
    if not os.path.lexists(os.path.join(root, "development", "link.txt")):
        os.symlink("../config/secrets/db.properties", os.path.join(root, "development", "link.txt"))
    with open(os.path.join(root, "new.pem"), "w") as f:
        f.write("PLANTED_PEM\n")
    for p, why in (("config/secrets/db.properties", "hidden"), (".git/config", ".git"), ("development/link.txt", "symlink"),
                   ("../e2e/plain/README.md", ".."), ("/etc/hosts", "absolute"), ("development/big.bin", "> 10 MiB"),
                   ("new.pem", "new L5 candidate"), ("development/shot.txt\nx", "newline")):
        kind, r, _ = outcome(sid, "gitlab_upload_file", {"path": p})
        check("upload %s (%s) -> refused without a card" % (p.replace("\n", "\\n"), why), kind == "refused" and not has_planted(r), (kind, r))
    kind, r, _ = outcome(sid, "gitlab_upload_file", {"path": "development/shot.txt", "project": "other/proj"})
    check("upload into a GitLab project outside the scope -> refused without a card", kind == "refused", (kind, r))
    check("no upload POST from any refusal", len(gl_calls(lambda c: c["path"].endswith("/uploads"))) == n0, "")
    os.unlink(os.path.join(root, "development", "big.bin"))
    os.unlink(os.path.join(root, "new.pem"))
    adv = S.get("advisor")
    if adv and sess(adv).get("state") in ("ready", "waiting", "working", "attention"):
        r = tool(adv, "gitlab_upload_file", {"path": "README.md", "project": "grp/app"})
        check("the Global agent has no gitlab_upload_file", not r.get("ok"), r)
    # job play
    kind, r, _ = outcome(sid, "gitlab_job_play", {"job_id": 101})
    check("gitlab_job_play with gitlab_extra_tools off -> refused without a card", kind == "refused" and "gitlab_extra_tools" in r.get("error", ""), r)
    secs = {sct["id"]: sct for sct in (get("/api/settings")[1] or {}).get("sections") or []}
    gsec = next((sid_ for sid_, sct in secs.items() if any(f.get("key") == "gitlab_extra_tools" for f in sct.get("fields") or [])), None)
    st, j, _ = post("/api/settings", {"section": gsec, "values": {"gitlab_extra_tools": True}})
    check("gitlab_extra_tools on -> 409 needs_confirm", st == 409 and (j or {}).get("needs_confirm"), (st, j))
    st, j, _ = post("/api/settings", {"section": gsec, "values": {"gitlab_extra_tools": True}, "confirm": ["gitlab_extra_tools"]})
    check("gitlab_extra_tools on (confirmed)", st == 200, (st, j))
    kind, r, card = outcome(sid, "gitlab_job_play", {"job_id": 101}, approve=True)
    check("manual job on a feature branch -> card gitlab_job_play, Allow -> play", kind == "card" and card["kind"] == "gitlab_job_play"
          and gl_calls(lambda c: c["m"] == "POST" and c["path"].endswith("/jobs/101/play")), (kind, r, card and card["kind"]))
    kind, r, card = outcome(sid, "gitlab_job_play", {"job_id": 102}, approve=True)
    check("failed job -> card, Allow -> retry", kind == "card" and gl_calls(lambda c: c["path"].endswith("/jobs/102/retry")), (kind, r))
    kind, r, _ = outcome(sid, "gitlab_job_play", {"job_id": 103})
    check("running job -> refused (409) without a card", kind == "refused" and "running" in r.get("error", ""), (kind, r))
    asec = next((sid_ for sid_, sct in secs.items() if any(f.get("key") == "auto_approve" for f in sct.get("fields") or [])), None)
    cur = next((f.get("value") for f in secs.get(asec, {}).get("fields") or [] if f.get("key") == "auto_approve"), []) or []
    cur = [x for x in cur if x not in ALWAYS_ASK]   # W1's hand-written ALWAYS_ASK kinds are no valid Settings value
    st, j, _ = post("/api/settings", {"section": asec, "values": {"auto_approve": list(cur) + [
        "gitlab_job_play", "gitlab_upload_file", "gitlab_comment"]}, "confirm": ["auto_approve"]})
    check("auto_approve gitlab_job_play / gitlab_upload_file / gitlab_comment saved (confirmed)", st == 200, (st, j))
    n0 = len(gl_calls(lambda c: c["path"].endswith("/uploads")))
    kind, r, _ = outcome(sid, "gitlab_upload_file", {"path": "development/shot.txt"})
    check("R4 recorded (user's decision): a pre-approved gitlab_upload_file uploads a visible file without a card (%s)" % kind,
          kind == "auto" and len(gl_calls(lambda c: c["path"].endswith("/uploads"))) == n0 + 1, (kind, r))
    kind, r, _ = outcome(sid, "gitlab_upload_file", {"path": "config/secrets/db.properties"})
    check("...a hidden file stays refused with the auto-approve", kind == "refused", (kind, r))
    kind, r, card = outcome(sid, "gitlab_comment", {"kind": "issue", "iid": 7, "body": "e2e", "note_id": 900})
    check("N-W7: an auto-approved gitlab_comment does not cover a note edit -> card gitlab_note_edit",
          kind == "card" and card["kind"] == "gitlab_note_edit", (kind, card, r))
    kind, r, card = outcome(sid, "gitlab_job_play", {"job_id": 101})
    check("pre-approved gitlab_job_play on a feature branch -> no card", kind == "auto", (kind, r))
    danger = []
    r = tool(sid, "gitlab_job_play", {"job_id": 104}, answer=lambda c: (danger.append((c["kind"], feed_danger(c))), decide(c, False)))
    check("a job on a protected branch (release) -> card gitlab_job_protected despite the auto-approve",
          danger and danger[0][0] == "gitlab_job_protected", (danger, r))
    check("B-N3-1: queue Allow of gitlab_job_protected carries args.danger", danger and danger[0][1] == "gitlab_job_protected", danger)
    post("/api/settings", {"section": asec, "values": {"auto_approve": list(cur)}})
    post("/api/settings", {"section": gsec, "values": {"gitlab_extra_tools": False}})
    # finding 3 / N-I24: inbox titles raw for the human, scrubbed on every path to an agent
    items = wait(lambda: [i for i in ((get("/api/inbox")[1] or {}).get("items") or []) if i.get("iid") == 7], 20)
    check("GUI /api/inbox: the issue with its raw title (the human's view)", items and "PLANTED_T1" in json.dumps(items), get("/api/inbox")[1])
    r = tool(sid, "gitlab_inbox", {})
    check("MCP gitlab_inbox: the title scrubbed", r.get("ok") and "PLANTED_T1" not in json.dumps(r) and "7" in json.dumps(r), r)
    r = tool(sid, "gitlab_issue", {"iid": 7})
    check("MCP gitlab_issue: title and description scrubbed", r.get("ok") and not has_planted(r), json.dumps(r)[:300])
    if items:
        n = len(state()["sessions"])
        st, j, _ = post("/api/inbox/start", {"key": items[0]["key"], "mode": "analyze", "project": pj})
        child = (j or {}).get("id")
        check("inbox start -> a session", st in (200, 202) and child, (st, j))
        if child:
            c = wait_state(child, ("blocked", "ready", "waiting", "working", "error"), 20)
            if c.get("state") == "blocked":
                post("/api/sessions/%s/approve" % child)
                c = wait_state(child, ("ready", "waiting", "working", "error"), 20)
            meta = json.load(open(os.path.join(sess_dir(child), "meta.json")))
            check("the new session's title / task / issue carry no unscrubbed title",
                  not has_planted({k: meta.get(k) for k in ("title", "task", "issue")}) and not has_planted(sess(child)), meta.get("title"))
            time.sleep(1.5)
            check("nothing unscrubbed typed into its terminal", b"PLANTED_T" not in pty_log(child), "")
            stop(child)
        _ = n
    # N-L8: a GitLab read in flight when piperd dies is answered after the restart, under the same id
    gl_state(slow={"9": 4})
    rid = os.urandom(16).hex()
    mb = os.path.join(box(sid), "piper-mailbox")
    os.makedirs(os.path.join(mb, "out"), exist_ok=True)
    with open(os.path.join(mb, "out", ".%s.tmp" % rid), "w") as f:
        json.dump({"id": rid, "tool": "gitlab_issue", "args": {"iid": 9}}, f)
    os.rename(os.path.join(mb, "out", ".%s.tmp" % rid), os.path.join(mb, "out", rid + ".json"))
    wait(lambda: gl_calls(lambda c: c["path"].endswith("/issues/9")), 8)
    kill_piperd(signal.SIGKILL)
    gl_state(slow={"9": 0})
    start_piperd()
    ans = os.path.join(mb, "in", rid + ".json")
    check("N-L8: the interrupted gitlab_issue is answered after the restart (same id)", wait(lambda: os.path.exists(ans), 20), "")
    if os.path.exists(ans):
        a = json.load(open(ans))
        os.unlink(ans)
        check("N-L8: the answer is the issue", a.get("ok") and a.get("id") == rid, a)
    check("N-L8: the read was repeated at most once (%d GETs)" % len(gl_calls(lambda c: c["path"].endswith("/issues/9"))),
          len(gl_calls(lambda c: c["path"].endswith("/issues/9"))) <= 2, "")
    # a write waiting on its card when piperd dies is never replayed
    rid = os.urandom(16).hex()
    with open(os.path.join(mb, "out", ".%s.tmp" % rid), "w") as f:
        json.dump({"id": rid, "tool": "gitlab_comment", "args": {"kind": "issue", "iid": 7, "body": "never twice"}}, f)
    os.rename(os.path.join(mb, "out", ".%s.tmp" % rid), os.path.join(mb, "out", rid + ".json"))
    check("the comment waits on its card", wait(lambda: pending(sid, "gitlab_comment"), 10), pending(sid))
    kill_piperd(signal.SIGKILL)
    start_piperd()
    time.sleep(4)
    for c in pending(sid, "gitlab_comment"):
        decide(c, True)
    time.sleep(2)
    check("N-L8: a write interrupted on its card is never replayed (no POST note)",
          not gl_calls(lambda c: c["m"] == "POST" and c["path"].endswith("/issues/7/notes")), gl_calls(lambda c: c["m"] == "POST"))
    stop(sid)


@section("F5")
def f5():
    """MERGE-2 finding 5: deleting a directory holding over-mounted hidden files while a session runs - allowed
    into Piper's trash; the over-mount is recorded detached; a candidate created there afterwards is uncovered
    for L5. Manual Files operations remain available independently of that coverage. Real mounts: T3."""
    pj, root = "e2e/leak", PROJECTS["e2e/leak"]
    stop_all(pj)
    sid = ready_session(pj)
    cov = json.load(open(os.path.join(sess_dir(sid), "meta.json"))).get("coverage") or []
    pinned = [c for c in cov if c.get("ids")]
    check("preflight pinned the over-mounts (coverage ids)", pinned, cov[:2])
    st, j, _ = post("/api/fs/delete", {"project": pj, "path": "config"})
    trash = (j or {}).get("trash")
    check("delete of config/ (over-mounted hidden files) while the session runs -> 200 into the trash", st == 200 and trash, (st, j))
    det = wait(lambda: [c for c in json.load(open(os.path.join(sess_dir(sid), "meta.json"))).get("coverage") or [] if c.get("detached")], 5)
    check("the removed over-mounts are recorded detached", det, det)
    check("audit overmount-detached", audit_events(sid, "overmount-detached"), "")
    os.makedirs(os.path.join(root, "config", "secrets"), exist_ok=True)
    with open(os.path.join(root, "config", "secrets", "x.properties"), "w") as f:
        f.write("pw=PLANTED_NEW\n")
    s = wait(lambda: (lambda x: x if any("config/secrets" in w for w in x.get("watch") or []) else None)(sess(sid)), 15)
    check("L5: a candidate re-created in the deleted over-mounted dir is uncovered (watch list)", s, sess(sid).get("watch"))
    st, j, _ = post("/api/fs/create", {"project": pj, "path": "config/secrets/y.properties", "kind": "file", "text": "pw=PLANTED_GUI\n"})
    check("manual GUI create in an uncovered stand-in dir -> 200", st == 200 and
          os.path.isfile(os.path.join(root, "config", "secrets", "y.properties")), (st, j))
    st, j, _ = post("/api/fs/restore", {"project": pj, "trash": trash or ""})
    check("restore still protects an existing destination from collision", st == 409 and "exists again" in str(j), (st, j))
    st, j, _ = post("/api/fs/delete", {"project": pj, "path": "config"})
    check("the human can trash the stand-in without stopping the session", st == 200 and (j or {}).get("trash"), (st, j))
    st, j, _ = post("/api/fs/restore", {"project": pj, "trash": trash or ""})
    check("manual restore of hidden content while the session runs -> 200", st == 200 and
          os.path.isfile(os.path.join(root, "config", "secrets.yml")), (st, j))
    check("manual restore leaves the session alive", sess(sid).get("state") not in ("exited", "error"), sess(sid).get("state"))
    stop(sid)


def ijhttp_spec(env: str, body: str) -> None:
    """The next fake ijhttp run answers 200 with `body` (N5-HTTP)."""
    with open(os.path.join(WORK, "ijhttp-spec.json"), "w") as f:
        json.dump({"rc": 0, "console": "Request 'ping' GET http://127.0.0.1:%d/ping\n\n<= response =\nHTTP/1.1 200 OK\n"
                   "content-type: application/json\n\n%s\n\nResponse code: 200 (OK); Time: 5ms (5 ms); Content length: "
                   "%d bytes (%d B)\n" % (A.port + 7, body, len(body), len(body)),
                   "xml": '<?xml version="1.0"?><testsuites tests="1"><testsuite tests="1" name="t"><testcase '
                          'classname="http.ping" name="Response" time="0.1" /></testsuite></testsuites>'}, f)


def ijhttp_runs() -> list:
    try:
        with open(os.path.join(WORK, "ijhttp.log")) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except OSError:
        return []


@section("N5-HTTP")
def n5_http():
    """N5: an .http run through the session tool http_run (dev at once, test on a card with "for the session",
    prod on a card every time, bodies withheld) and through the GUI route POST /api/http/run (the click is the
    approval: no card on any class) - against the fake ijhttp (FAKE_IJHTTP), no network."""
    pj, f = "e2e/http", "http-client/ping.http"
    sid = ready_session(pj)
    r = tool(sid, "http_envs", {}).get("result") or {}
    envs = {e["name"]: e["cls"] for d in r.get("env_dirs") or [] for e in d.get("envs") or []}
    check("http_envs: dev / test / prod classes, ijhttp found", envs == {"dev": "dev", "test": "test", "prod": "prod"}
          and r.get("ijhttp") is True, r)
    check("http_envs: no environment value / secret to the session", not has_planted(r), r)
    # dev: no card
    ijhttp_spec("dev", '{"pong": "dev", "echo": "PLANTED_HTTP_DEV"}')
    n0 = len(ijhttp_runs())
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "dev"}, approve=True)
    r = r.get("result") or r
    runs = ijhttp_runs()
    check("http_run dev -> runs at once, no card", kind == "auto" and r.get("ok") and r.get("class") == "dev"
          and len(runs) == n0 + 1, (kind, r, card))
    check("http_run dev: the run used env dev with the env files (-e dev, -v, -p)", runs and "-e" in runs[-1]["argv"]
          and runs[-1]["argv"][runs[-1]["argv"].index("-e") + 1] == "dev" and "-p" in runs[-1]["argv"], runs[-1:])
    reqs = r.get("requests") or [{}]
    check("http_run dev: status 200 and the body (a secret value of the env masked)", reqs[0].get("status") == 200
          and '"pong": "dev"' in (reqs[0].get("body") or "") and not has_planted(r), r)
    # test: a card; Deny -> nothing ran
    ijhttp_spec("test", '{"pong": "test"}')
    n0 = len(ijhttp_runs())
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "test"}, approve=False)
    check("http_run test -> card http_test, Deny -> nothing ran", kind == "card" and (card or {}).get("kind") == "http_test"
          and not r.get("ok") and len(ijhttp_runs()) == n0, (kind, r, card and card.get("kind")))
    # test: Allow this content for the session -> runs; the same content again runs without a card
    cards = []
    r = tool(sid, "http_run", {"file": f, "env": "test"}, answer=lambda c: (cards.append(c), decide(c, True, always=True)))
    check("http_run test -> card, 'Allow this content for the session' -> ran", cards and cards[0]["kind"] == "http_test"
          and r.get("ok") and len(ijhttp_runs()) == n0 + 1, (r, cards[:1]))
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "test"})
    check("http_run test again, same content -> no card (allowed for the session)", kind == "auto"
          and len(ijhttp_runs()) == n0 + 2, (kind, r, card))
    # prod: a card every time, even after Allow; bodies withheld without include_body
    ijhttp_spec("prod", '{"pong": "prod", "customer": "PLANTED_HTTP_PRODBODY"}')
    n0 = len(ijhttp_runs())
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "prod"}, approve=False)
    check("http_run prod -> card http_prod, Deny -> nothing ran", kind == "card" and (card or {}).get("kind") == "http_prod"
          and len(ijhttp_runs()) == n0, (kind, r, card and card.get("kind")))
    for n in (1, 2):
        cards = []
        r = tool(sid, "http_run", {"file": f, "env": "prod"}, answer=lambda c: (cards.append(c), decide(c, True, always=True)))
        r = dict(r.get("result") or {}, ok=r.get("ok"))
        check("http_run prod #%d -> card http_prod every time (always is no option), Allow -> ran" % n, cards
              and cards[0]["kind"] == "http_prod" and r.get("ok") and len(ijhttp_runs()) == n0 + n, (r, cards[:1]))
        check("http_run prod #%d: the response body is withheld from the session" % n, not has_planted(r)
              and "withheld" in (r.get("bodies") or ""), r)
    # New session/environment grant: different content is covered, another session still asks.
    cards = []
    r = tool(sid, "http_run", {"file": f, "env": "prod"},
             answer=lambda c: (cards.append(c), decide(c, True, session_always=True)))
    check("prod session grant is offered and executes the approved request", cards and
          cards[0].get("session_always") == "Allow for this session on PROD" and r.get("ok"), (r, cards[:1]))
    with open(os.path.join(PROJECTS[pj], f), "a") as out:
        out.write("\n# different content after the session grant\n")
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "prod"})
    check("prod session grant covers changed content without another card", kind == "auto" and r.get("ok"), (kind, r, card))
    host_pid = ptyhost_pid()
    kill_piperd(signal.SIGTERM)   # only this harness's daemon; its ptyhost and sessions stay alive
    start_piperd()
    wait_state(sid, ("ready", "waiting", "working", "attention"), 10)
    check("session grant restart preserves the ptyhost and running session", host_pid and ptyhost_pid() == host_pid
          and sess(sid).get("state") in ("ready", "waiting", "working", "attention"), sess(sid))
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "prod"})
    check("production grant survives an actual piperd restart", kind == "auto" and r.get("ok"), (kind, r, card))
    second = ready_session(pj)
    kind, r, card = outcome(second, "http_run", {"file": f, "env": "prod"}, approve=False)
    check("prod grant does not cover another session", kind == "card" and not r.get("ok"), (kind, r, card))
    stop(second)
    kind, r, card = outcome(sid, "http_run", {"file": f, "env": "test"}, approve=False)
    check("prod grant does not cover test after content changes", kind == "card" and not r.get("ok"), (kind, r, card))
    check("no http card left waiting", not pending(sid), pending(sid))
    # the GUI route: the click is the approval - no card on any class (prod asks once more in the page)
    for env in ("dev", "test", "prod"):
        ijhttp_spec(env, '{"pong": "gui-%s"}' % env)
        n0, p0 = len(ijhttp_runs()), len(state()["pending"])
        st, j, b = post("/api/http/run", {"project": pj, "path": f, "env": env})
        check("GUI POST /api/http/run %s -> 200, ran, no card" % env, st == 200 and (j or {}).get("ok") and (j or {}).get("cls") == env
              and len(ijhttp_runs()) == n0 + 1 and len(state()["pending"]) == p0, (st, b[:300]))
        check("GUI run %s: the full record, no agent copy" % env, j and "agent" not in j and
              (j.get("requests") or [{}])[0].get("body") == '{"pong": "gui-%s"}' % env, j)
    st, j, _ = post("/api/http/run", {"project": pj, "path": f, "env": "prod"}, tok="tty")
    check("GUI route with the tty token -> 403, nothing ran", st == 403, (st, j))
    st, j, _ = get("/api/http/runs?project=%s&path=%s" % (pj, f))
    by = [x.get("by") for x in (j or {}).get("runs") or []] if isinstance(j, dict) else []
    check("the run list holds the session's and the GUI's runs", st == 200 and "gui" in by and any(b_ != "gui" for b_ in by),
          (st, str(j)[:300]))
    r = tool(sid, "http_last_result", {"file": f}).get("result") or {}
    check("http_last_result: the GUI's prod run reaches the session without its body", r.get("by") == "user (GUI)"
          and r.get("class") == "prod" and "gui-prod" not in json.dumps(r), r)
    ev = [e for e in audit_events(None, "http-run") if e.get("project") == pj]
    check("audit http-run per run: 8 of the session, 3 of the GUI", len([e for e in ev if e.get("by") != "gui"]) == 8
          and len([e for e in ev if e.get("by") == "gui"]) == 3, [(e.get("by"), e.get("env")) for e in ev])
    stop(sid)


@section("BLIND-CONFIG")
def blind_config():
    """Real mailbox, pending API and pinned hidden-file writes: no originals in session replies/cards."""
    project = "e2e/leak"
    root = PROJECTS[project]
    prop = "config/secrets/db.properties"
    env = "http-client/http-client.env.json"
    paths = {rel: os.path.join(root, rel) for rel in (prop, env)}
    originals = {}
    for rel, path in paths.items():
        with open(path, "rb") as f:
            originals[rel] = f.read()
    sid = ready_session(project)
    try:
        result = tool(sid, "properties_keys", {"file": prop})
        check("properties mailbox exposes only decoded keys", result.get("ok") and
              result.get("result") == {"keys": ["password"]} and not has_planted(result), result)
        for name, args, rel in (("properties_set", {"file": prop, "values": {"password": "replacement"}}, prop),
                                ("http_env_set", {"file": env, "env": "test", "values": {"new": "replacement"}}, env)):
            for approve in (False, True, False):
                cards = []
                def answer(card):
                    cards.append(card)
                    decide(card, approve, always=True, session_always=True)
                with open(paths[rel], "rb") as f:
                    before = f.read()
                result = tool(sid, name, args, answer=answer)
                check(name + " gets a fresh card each time", len(cards) == 1 and cards[0]["kind"] == name, cards)
                check(name + " exposes no old values in replies or cards", not has_planted(result) and not has_planted(cards),
                      {"ok": result.get("ok"), "cards": len(cards)})
                check(name + " has no persistent approval", bool(cards) and "always" not in cards[0] and
                      "session_always" not in cards[0], cards)
                check(name + " obeys approval/denial including forged always", bool(result.get("ok")) == approve, result)
                with open(paths[rel], "rb") as f:
                    after = f.read()
                check(name + " denial preserves bytes / approval changes data", after != before if approve else after == before)
                if approve:
                    check(name + " success reply contains no content or hash", result.get("result") == {"ok": True}, result)
            def race(card):
                with open(paths[rel], "wb") as f:
                    f.write(b"password=concurrent\n" if rel == prop else b'{"test":{"race":"concurrent"}}')
                decide(card, True)
            result = tool(sid, name, args, answer=race)
            check(name + " rejects a concurrent host edit", not result.get("ok") and "changed" in result.get("error", ""), result)
    finally:
        for rel, data in originals.items():
            with open(paths[rel], "wb") as f:
                f.write(data)
        stop(sid)


@section("AS1")
def agent_switch():
    """9.140: a Codex session's work continues with Claude through the real piperd - the Codex session is stopped,
    a Claude session starts in the same project with handoff.md in its sandbox's drop dir (the conversation, the
    worktree Codex last worked in, the host's git facts; no tool output) and the task pointing at it is typed."""
    root = PROJECTS["e2e/switch"]
    wt = os.path.join(root, ".claude", "worktrees", "as-wt")
    if not os.path.isdir(wt):
        git(root, "worktree", "add", "-q", "-b", "as-branch", wt)
    with open(os.path.join(wt, "Changed.java"), "w") as f:
        f.write("class Changed {}\n")
    cid = ready_session("e2e/switch", agent="codex")
    native = "0199aaaa-bbbb-cccc-dddd-0000000000a5"
    hook(cid, "SessionStart", session_id=native)
    check("AS: the Codex session has its conversation", wait(lambda: sess(cid).get("claude_session") == native, 5), sess(cid))
    with open(os.path.join(sess_dir(cid), "meta.json")) as f:
        meta = json.load(f)
    cfg = os.path.join(sess_dir(cid), "codex")
    rel = "sessions/2026/10/21/rollout-2026-10-21T10-00-00-%s.jsonl" % native
    say = lambda role, text: {"type": "response_item", "timestamp": "2026-10-21T10:00:00Z", "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}
    recs = [{"type": "session_meta", "payload": {"id": native, "cwd": root}},
            {"type": "turn_context", "timestamp": "2026-10-21T10:00:00Z", "payload": {"cwd": root}},
            say("user", "AS_REQUEST finish the sharepoint import"),
            {"type": "response_item", "timestamp": "2026-10-21T10:01:00Z", "payload": {"type": "function_call", "name": "exec",
             "arguments": json.dumps({"cmd": "ls", "workdir": wt})}},
            {"type": "response_item", "timestamp": "2026-10-21T10:01:01Z", "payload": {"type": "function_call_output",
             "output": "PLANTED_AS_TOOL_OUTPUT .claude/worktrees/other-wt"}},
            say("assistant", "AS_ANSWER the import half done in as-wt")]
    os.makedirs(os.path.dirname(os.path.join(cfg, rel)), exist_ok=True)
    with open(os.path.join(cfg, rel), "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    st, j, b = post("/api/sessions/%s/switch-agent" % cid, {}, tok="tty")
    check("AS: switch-agent needs the GUI token (403 for tty)", st == 403, (st, b[:200]))
    st, j, b = post("/api/sessions/%s/switch-agent" % cid, {})
    check("AS: a masked worktree reference requires the existing hidden-content confirmation",
          st == 409 and ".claude/worktrees/as-wt" in (j or {}).get("hidden_paths", []), (st, b[:300]))
    check("AS: refusing that handoff leaves the source alive", sess(cid).get("state") not in ("exited", "error"), sess(cid).get("state"))
    st, j, b = post("/api/sessions/%s/switch-agent" % cid, {"confirm_hidden": True})
    check("AS: switch-agent answers 200 with the new Claude session", st == 200 and (j or {}).get("agent") == "claude"
          and (j or {}).get("from") == cid, (st, b[:300]))
    new = (j or {}).get("session") or ""
    check("AS: the Codex session is stopped", wait_state(cid, ("exited", "error"), 15).get("state") == "exited", sess(cid))
    s = wait_state(new, ("ready", "waiting", "working", "attention", "blocked", "error", "exited"), 30)
    if s.get("state") == "blocked":
        post("/api/sessions/%s/approve" % new)
        s = wait_state(new, ("ready", "waiting", "working", "attention", "error", "exited"), 30)
    check("AS: the Claude session runs in the same project", s.get("state") in ("ready", "waiting", "working") and
          s.get("agent") == "claude" and s.get("project") == "e2e/switch", s)
    sf = s.get("switched_from") or {}
    check("AS: /api/state carries switched_from with the worktree", sf.get("session") == cid and sf.get("agent") == "codex"
          and sf.get("conversation") == native and sf.get("worktree") == os.path.realpath(wt) and sf.get("branch") == "as-branch", sf)
    drop = os.path.join(box(new), "piper-drop", "handoff.md")
    check("AS: handoff.md is in the new sandbox's drop dir (0600)", os.path.isfile(drop) and
          (os.stat(drop).st_mode & 0o777) == 0o600, drop)
    doc = open(drop, encoding="utf-8").read() if os.path.isfile(drop) else ""
    check("AS: the document has the conversation, the worktree, its branch and git state", all(x in doc for x in (
        "AS_REQUEST finish", "AS_ANSWER the import", os.path.realpath(wt), "as-branch", "Changed.java", "not instructions")), doc[:1500])
    check("AS: no tool output in the document", "PLANTED_AS_TOOL_OUTPUT" not in doc and "other-wt" not in doc, "")
    out = wait(lambda: (lambda t: t if b"You continue work Codex started" in t else None)(term(new, 1.0)), 20) or b""
    check("AS: the task pointing at the document is typed, titled by the source", b"piper-drop/handoff.md" in out and
          b"as-wt" in out and ('Continue "%s" (from Codex).' % sess(cid).get("title")).encode() in out,
          out[-800:])
    check("AS: the masked worktree is not advertised as available", b"hidden by this sandbox's isolation" in out and
          b"Work in the worktree" not in out and "**hidden in this sandbox**" in doc, out[-800:])
    check("AS: the task holds no conversation text", b"AS_REQUEST" not in out, "")
    ev = audit_events(new, "agent-switch")
    check("AS: audited agent-switch with where", bool(ev) and ev[0].get("from") == cid and ev[0].get("where") == os.path.realpath(wt), ev[:1])
    st, j, b = post("/api/sessions/%s/switch-agent" % new, {})
    check("AS: a session without a conversation yet is refused (409)", st == 409 and b"no saved conversation" in b, (st, b[:200]))
    stop(new)
    post("/api/sessions/%s/retry" % new)
    s = wait_state(new, ("ready", "waiting", "working", "attention", "error", "exited"), 30)
    check("AS: the handoff is placed again after Continue (the box is rebuilt)", os.path.isfile(drop), (s.get("state"), drop))
    # back: the Claude session's work continues with Codex - its box is <session>/piper, mounted at box_in
    conv = "5e55a0b1-aaaa-bbbb-cccc-0000000000a5"
    hook(new, "SessionStart", session_id=conv)
    wait(lambda: sess(new).get("claude_session") == conv, 5)
    tdir = os.path.join(box(new), "projects", "-x")
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, conv + ".jsonl"), "w") as f:
        for r in ({"type": "user", "message": {"role": "user", "content": "AS_BACK_REQUEST go on"}},
                  {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "AS_BACK_ANSWER done"}]}}):
            f.write(json.dumps(dict(r, sessionId=conv, cwd=root, timestamp="2026-10-21T11:00:00Z")) + "\n")
    st, j, b = post("/api/sessions/%s/switch-agent" % new, {})
    check("AS: the Claude session continues with Codex", st == 200 and (j or {}).get("agent") == "codex", (st, b[:300]))
    back = (j or {}).get("session") or ""
    s = wait_state(back, ("ready", "waiting", "working", "attention", "error", "exited"), 30)
    check("AS: the Codex session runs (claude_mode none)", s.get("state") in ("ready", "waiting", "working") and
          s.get("claude_mode") == "none", s)
    drop2 = os.path.join(box(back), "piper-drop", "handoff.md")
    doc2 = open(drop2, encoding="utf-8").read() if os.path.isfile(drop2) else ""
    check("AS: Codex's handoff.md is in its box's drop dir (0600) with the Claude conversation", os.path.isfile(drop2) and
          (os.stat(drop2).st_mode & 0o777) == 0o600 and "AS_BACK_REQUEST" in doc2 and "# Handoff from Claude" in doc2, drop2)
    out = wait(lambda: (lambda t: t if b"You continue work Claude started" in t else None)(term(back, 1.0)), 20) or b""
    check("AS: Codex gets the task pointing at its drop dir", b"piper-drop/handoff.md" in out, out[-600:])
    stop(back)
    # a worktree outside the project (git.worktreePool's place) is not mounted without git.worktreeLinks: say so
    out_wt = os.path.realpath(root) + "-worktrees/as-out"
    if not os.path.isdir(out_wt):
        git(root, "worktree", "add", "-q", "-b", "as-out", out_wt)
    cx = ready_session("e2e/switch", agent="codex")
    native = "0199aaaa-bbbb-cccc-dddd-0000000000a6"
    hook(cx, "SessionStart", session_id=native)
    wait(lambda: sess(cx).get("claude_session") == native, 5)
    rel = "sessions/2026/10/21/rollout-2026-10-21T12-00-00-%s.jsonl" % native
    recs = [{"type": "session_meta", "payload": {"id": native, "cwd": root}}, say("user", "AS_OUT_REQUEST go on"),
            {"type": "response_item", "timestamp": "2026-10-21T12:01:00Z", "payload": {"type": "function_call", "name": "exec",
             "arguments": json.dumps({"cmd": "make", "workdir": out_wt})}}, say("assistant", "AS_OUT_ANSWER built")]
    cfg = os.path.join(sess_dir(cx), "codex")
    os.makedirs(os.path.dirname(os.path.join(cfg, rel)), exist_ok=True)
    with open(os.path.join(cfg, rel), "w") as f:
        f.write("\n".join(json.dumps(r) for r in recs) + "\n")
    st, j, b = post("/api/sessions/%s/switch-agent" % cx, {})
    out_new = (j or {}).get("session") or ""
    s = wait_state(out_new, ("ready", "waiting", "working", "attention", "error", "exited"), 30) if st == 200 else {}
    check("AS: an outside worktree is recorded", (s.get("switched_from") or {}).get("worktree") == out_wt, (st, b[:300], s))
    out = wait(lambda: (lambda t: t if b"You continue work Codex started" in t else None)(term(out_new, 1.0)), 20) or b""
    check("AS: the task says the outside worktree is not mounted, never 'Work in' it",
          b"not mounted in your sandbox - ask me" in out and b"Work in the worktree" not in out, out[-800:])
    stop(out_new)


@section("QW")
def quiet_windows():
    """Real controller/mailbox coordination; fake CLIs do not claim OS-level quiet."""
    for s in state()["sessions"]:
        if s["state"] in ("ready", "working", "waiting", "attention", "preflight", "starting", "attesting"):
            stop(s["id"])
    check("QW: feature is enabled by default", state()["features"]["modules"]["quiet"] is True)
    ids = [ready_session(p) for p in ("e2e/plain", "e2e/other", "e2e/codex", "e2e/switch", "e2e/stop")]
    owner = ids[0]
    rejected = tool(owner, "quiet_request", {"reason": "Thorough tests"})
    check("QW: workload alone cannot request quiet", not rejected.get("ok") and state()["quiet"]["active"] is None, rejected)
    rejected = tool(owner, "piper_session", {"quiet": {"action": "request", "reason": "Build"}})
    check("QW: legacy request also requires explicit user instruction", not rejected.get("ok") and state()["quiet"]["active"] is None, rejected)
    r = tool(owner, "quiet_request", {"reason": "Full build", "user_requested": True})
    a = (r.get("result") or {}).get("active") or {}
    rid = a.get("id", "")
    check("QW: authenticated owner captures the other four sessions", r.get("ok") and a.get("owner") == owner and len(a.get("participants", [])) == 4, r)
    check("QW: implicit agent request has no hidden duration cap", a.get("minutes") is None, a)
    check("QW: disabling an active feature is refused", post("/api/features", {"modules": {"quiet": False}})[0] == 400)
    progress = tool(ids[1], "piper_session", {"quiet": {"action": "ack", "id": rid, "finished": False,
                                                    "detail": "Finishing the existing test job"}})
    check("QW: legacy progress is reported without acknowledging quiet", progress.get("ok") and
          progress.get("result", {}).get("active", {}).get("participants", [{}])[0].get("status") == "finishing", progress)
    # Real wall-clock regression: the old 120-second preparation timeout must not recur.
    end = time.monotonic() + 123
    supervised = True
    while time.monotonic() < end:
        renewal = tool(owner, "quiet_renew", {"id": rid})
        current = state()["quiet"]["active"]
        supervised = supervised and renewal.get("ok", False) and bool(current and current["phase"] == "draining")
        time.sleep(min(25, max(0, end - time.monotonic())))
    a = state()["quiet"]["active"] or {}
    check("QW: supervised real 123-second drain remains pending and blocks work", supervised and a.get("phase") == "draining" and
          not tool(owner, "quiet_work", {"id": rid, "running": True}).get("ok"), a)
    check("QW: unanswered and stale progress stay visible without granting", any(p.get("id") == ids[1] and p.get("stale") for p in a.get("participants", [])) and all(p.get("status") == "finishing" for p in a.get("participants", [])), a)
    fresh = tool(ids[1], "quiet_ack", {"id": rid, "finished": False, "detail": "Existing job ended; checking children"})
    check("QW: fresh progress clears the warning without confirming", fresh.get("ok") and
          not fresh.get("result", {}).get("active", {}).get("participants", [{}])[0].get("stale") and
          fresh.get("result", {}).get("active", {}).get("phase") == "draining", fresh)
    for sid in ids[1:]:
        r = tool(sid, "quiet_ack", {"id": rid, "finished": True})
        check("QW: participant can acknowledge through its mailbox", r.get("ok"), r)
    a = wait(lambda: (lambda x: x if x and x["phase"] == "ready" else None)(state()["quiet"]["active"]), 10)
    check("QW: all acknowledgements and settling grant the window", bool(a), a)
    check("QW: default grant has no deadline", a and a.get("deadline") is None, a)
    process_before, pty_before = PROC["piperd"].pid, ptyhost_pid()
    session_pids = {sid: sess(sid).get("pid") for sid in ids}
    kill_piperd()
    start_piperd()
    health = json.loads(req("GET", "/api/ping")[2])
    check("QW: restart proves the new actual coordinator", health.get("pid") == PROC["piperd"].pid != process_before and health.get("capabilities", {}).get("quiet") is True, health)
    check("QW: restart preserves ptyhost and every runtime", ptyhost_pid() == pty_before and all(sess(sid).get("pid") == session_pids[sid] for sid in ids), "")
    check("QW: persisted hold recovers before admitting any work", state()["quiet"]["active"]["phase"] == "recovering", state()["quiet"])
    denied = tool(owner, "quiet_renew", {"id": rid})
    check("QW: old owner cannot renew a recovering grant", not denied.get("ok"), denied)
    r = tool(owner, "quiet_release", {"id": rid, "finished": True})
    check("QW: checked recovery can release", r.get("ok"), r)
    r = tool(owner, "quiet_request", {"reason": "Build after recovery", "minutes": 15, "user_requested": True})
    rid = (r.get("result") or {}).get("active", {}).get("id", "")
    for sid in ids[1:]:
        tool(sid, "quiet_ack", {"id": rid, "finished": True})
    a = wait(lambda: (lambda x: x if x and x["phase"] == "ready" else None)(state()["quiet"]["active"]), 10)
    check("QW: a fresh request can be granted after checked recovery", bool(a), a)
    r = tool(ids[1], "list_projects")
    check("QW: held participant cannot start managed work", not r.get("ok") and "Quiet window" in r.get("error", ""), r)
    for running in (True, False, True, False):
        r = tool(owner, "quiet_work", {"id": rid, "running": running})
        check("QW: consecutive work preserves the reservation", r.get("ok") and (r.get("result") or {}).get("active", {}).get("phase") == ("working" if running else "ready"), r)
    r = tool(owner, "quiet_renew", {"id": rid})
    check("QW: owner explicitly renews supervision", r.get("ok"), r)
    r = tool(owner, "quiet_release", {"id": rid, "finished": True})
    check("QW: release clears the reservation", r.get("ok") and (r.get("result") or {}).get("active") is None, r)
    r = tool(ids[1], "quiet_wait", {"id": rid, "seconds": 0})
    check("QW: waiting participants observe release", r.get("ok") and (r.get("result") or {}).get("active") is None, r)
    for sid in ids:
        stop(sid)


@section("END")
def end():
    with open(os.path.join(WORK, "piperd.out"), "rb") as f:
        out = f.read().decode("utf-8", "replace")
    tb = [ln for ln in out.splitlines() if "Traceback" in ln or "Exception in thread" in ln]
    check("no traceback in piperd's output after the 2026-10-09 rows", not tb, "\n".join(tb[:10]))


def teardown():
    try:
        for s in state()["sessions"]:
            if s["state"] in ("ready", "waiting", "working", "attention", "trust", "starting", "attesting", "spawning",
                              "preflight", "preparing", "blocked", "stopping"):
                post("/api/sessions/%s/stop" % s["id"])
        wait(lambda: all(s["state"] not in ("stopping",) for s in state()["sessions"]), 15)
    except Exception:  # noqa: BLE001
        pass
    pp = ptyhost_pid()   # piperd starts ptyhost (detached) as soon as it runs
    kill_piperd(signal.SIGTERM)
    if PROC.get("gitd") and PROC["gitd"].poll() is None:
        PROC["gitd"].terminate()
    if pp:
        try:
            os.kill(pp, signal.SIGTERM)   # by PID only
        except ProcessLookupError:
            pass


def main() -> int:
    setup_home()
    start_git_daemon()
    setup_projects()
    mint()
    try:
        start_piperd()
        for sid_, fn in SECTIONS:
            if ONLY and sid_ not in ONLY:
                continue
            cur["id"] = sid_
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                check("section ran to the end", False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:]))
    finally:
        teardown()
    fails = [r for r in results if not r["ok"]]
    by = {}
    for r in results:
        by.setdefault(r["id"], [0, 0])[0 if r["ok"] else 1] += 1
    with open(os.path.join(WORK, "results.json"), "w") as f:
        json.dump({"results": results, "summary": by, "work": WORK}, f, indent=1)
    print("\n== %d checks, %d failed  (%s)  work: %s" % (len(results), len(fails),
          " ".join("%s %d/%d" % (k, v[0], v[0] + v[1]) for k, v in by.items()), WORK))
    if not A.keep and not fails and not A.work:
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
