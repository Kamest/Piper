"""N6 (2026-10-09): git_run where the sandbox cannot read that .git - another project it does not mount, a
session whose .git is hidden. Four review rounds each found one more argv that returned a hidden file's content
card-free (D4, R-3, R5-1, R5-2); there a card-free run is now an allowlist (gitops.EXPOSED). This file:
- a property test: argv generated from git's own option lists (`git <cmd> --git-completion-helper-all`,
  `git <cmd> -h`, `git rev-list -h`) over a repository with planted hidden content (committed twice, in a stash,
  changed in the work tree, ignored and untracked, a tag of its blob, a symlink to it) - the output never carries
  it unless a card was asked; nor does a visible work-tree file afterwards;
- the card table of ordinary commands: nothing new on a card in the open-.git own project (K1, git_mode "hole");
- the K1 leaks of the work-tree content the same review found (a symlink read as a message / graft file, --check).
"""
import io
import json
import os
import random
import re
import shutil
import subprocess
import time
import unittest
import urllib.error
from unittest import mock

import _env
import common
import gitops
import guard
import httpclient
import projectfs
import sessions

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T",
           GIT_COMMITTER_EMAIL="t@e", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
HIDDEN = "config/secrets"
COMMITTED = ("PLANTED_C1", "PLANTED_C2", "PLANTED_S3")   # in .git: K1 lets the sandbox read these
WORKTREE = ("PLANTED_W4", "PLANTED_U5")                  # only in the host work tree: never, in any situation
# N6_FUZZ_SEEDS=1,2,3: other seeds for a long run (N7); the suite keeps the fixed three
SEEDS = tuple(int(x) for x in os.environ["N6_FUZZ_SEEDS"].split(",")) if os.environ.get("N6_FUZZ_SEEDS") else (6, 61, 617)
BUDGET_S = float(os.environ.get("N6_FUZZ_BUDGET") or 12)   # per situation: the three property tests < 60 s
MIN_PER_SEED = 15   # ... but never fewer cases than this per seed (3 seeds: > 40), whatever the machine's load
# the checked pull reproduces the messages of a current git (2.4x+); an older one (Debian bookworm's 2.39 in CI) words
# a few differently ("Please" / "please", the ff-only hint) - there only rc and the end state are compared
_GV = tuple(int(x) for x in re.findall(r"\d+", subprocess.run(["git", "--version"], capture_output=True, text=True).stdout)[:2])
SAME_WORDING = _GV >= (2, 45)


def _g(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=ENV, text=True).stdout.strip()


def build(name: str) -> str:
    """README / src visible, config/secrets hidden: two commits of the hidden file, a stash of a third
    version, a fourth in the work tree, an ignored untracked fifth, a tag of its blob and of its tree, a
    symlink to it (the sandbox can plant one in its own work tree)."""
    repo = os.path.realpath(_env.make_project(name, {
        "README.md": "one\n", "src/a.txt": "a  \n", HIDDEN + "/db.properties": "pw=PLANTED_C1\n",
        ".gitignore": HIDDEN + "/local.properties\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": [HIDDEN]}})}))
    _g(repo, "init", "-q", "-b", "main")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "one")
    for rel, t in ((HIDDEN + "/db.properties", "pw=PLANTED_C2 \n"), ("README.md", "two\n")):
        with open(os.path.join(repo, rel), "w") as f:
            f.write(t)
    _g(repo, "commit", "-qam", "two")
    _g(repo, "tag", "blobtag", "HEAD:" + HIDDEN + "/db.properties")
    _g(repo, "tag", "treetag", "HEAD:config")
    _g(repo, "tag", "-a", "-m", "release", "v1", "HEAD")
    for rel, t in ((HIDDEN + "/db.properties", "pw=PLANTED_S3\n"), ("README.md", "three\n")):
        with open(os.path.join(repo, rel), "w") as f:
            f.write(t)
    _g(repo, "stash", "-q")
    with open(os.path.join(repo, HIDDEN, "db.properties"), "w") as f:
        f.write("pw=PLANTED_W4 \nsecond=PLANTED_W4\t\n")   # trailing whitespace: what --check prints
    with open(os.path.join(repo, HIDDEN, "local.properties"), "w") as f:
        f.write("pw=PLANTED_U5\n")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("four\n")
    os.symlink(HIDDEN + "/db.properties", os.path.join(repo, "lnk"))
    os.symlink("config", os.path.join(repo, "lnkdir"))
    return repo


def oid(repo: str, rev: str) -> str:
    return _g(repo, "rev-parse", rev)


def hidden_state(repo: str) -> dict:
    """The hidden work-tree files as they are on disk (name -> bytes)."""
    out = {}
    for d, _dirs, files in os.walk(os.path.join(repo, HIDDEN)):
        for fn in files:
            p = os.path.join(d, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, repo)] = f.read()
    return out


def visible_leaks(repo: str, words) -> list[str]:
    """Visible regular files of the work tree that carry a planted word (content moved out of hiding)."""
    hits = []
    for d, dirs, files in os.walk(repo):
        rel = os.path.relpath(d, repo)
        dirs[:] = [x for x in dirs if x != ".git" and not os.path.normpath(os.path.join(rel, x)).startswith(HIDDEN)]
        for fn in files:
            p = os.path.join(d, fn)
            if os.path.islink(p):
                continue
            try:
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            hits += [os.path.relpath(p, repo) for w in words if w.encode() in data]
    return hits


_HTTP: dict = {}


def serve_origin(repo: str) -> str:
    """N8: a bare `origin` the repository tracks (main -> origin/main), served over (dumb) HTTP from a thread -
    host git never fetches a local path without a card. Returns the bare repository's path (push into it directly,
    then update-server-info)."""
    import functools
    import http.server
    import threading
    root = os.path.dirname(repo)
    if root not in _HTTP:
        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_a):
                pass
        srv_ = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=root))
        threading.Thread(target=srv_.serve_forever, daemon=True).start()
        _HTTP[root] = srv_
    srv = repo + ".srv.git"
    shutil.rmtree(srv, ignore_errors=True)
    _g(repo, "init", "-q", "--bare", "-b", "main", srv)
    _g(repo, "push", "-q", srv, "main")
    _g(srv, "update-server-info")
    _g(repo, "remote", "add", "origin", "http://127.0.0.1:%d/%s" % (_HTTP[root].server_port, os.path.basename(srv)))
    _g(repo, "fetch", "-q", "origin")
    _g(repo, "branch", "-q", "-u", "origin/main", "main")
    return srv


def evil_commit(repo: str, rel: str, text: str, name: str) -> None:
    """a branch whose one commit changes `rel` - what the sandbox can build with its own open .git"""
    blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input=text, capture_output=True,
                          text=True, env=ENV, check=True).stdout.strip()
    env = dict(ENV, GIT_INDEX_FILE=os.path.join(repo, ".git", "n7-index"))
    subprocess.run(["git", "-C", repo, "read-tree", "HEAD"], env=env, check=True)
    subprocess.run(["git", "-C", repo, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, rel)], env=env, check=True)
    tree = subprocess.run(["git", "-C", repo, "write-tree"], env=env, capture_output=True, text=True, check=True).stdout.strip()
    os.unlink(os.path.join(repo, ".git", "n7-index"))
    _g(repo, "branch", name, _g(repo, "commit-tree", tree, "-p", "HEAD", "-m", "innocent"))


class GitRunExposedBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, background=True)
        cls._old = {k: cls.m.policy.get(k, cls) for k in ("git_mode", "git_run_readonly_extra", "git_run_projects")}
        cls.m.policy.update(git_mode="hole", git_run_readonly_extra=True, git_run_projects=True)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._old.items():
            cls.m.policy.pop(k, None) if v is cls else cls.m.policy.__setitem__(k, v)

    def _live(self):
        s = sessions.Session(kind="project", project="live", path="/x", state="ready", gitlab=["grp/live"])
        os.makedirs(os.path.join(s.cfg, "piper-mailbox", "out"), exist_ok=True)
        os.makedirs(os.path.join(s.cfg, "piper-mailbox", "in"), exist_ok=True)
        self.m.sessions[s.id] = s
        def cleanup():
            s.state = "exited"
            self.m.sessions.pop(s.id, None)
            shutil.rmtree(s.dir, ignore_errors=True)
        self.addCleanup(cleanup)
        return s

    def _tool(self, s, args):
        rid = os.urandom(16).hex()
        # Match the real MCP client: the background poller must only see a complete request.
        common.write_json(os.path.join(s.cfg, "piper-mailbox", "out", rid + ".json"),
                          {"id": rid, "tool": "git_run", "args": args})
        self.m._mailbox(s, self.m._mail_busy)
        path = os.path.join(s.cfg, "piper-mailbox", "in", rid + ".json")
        deadline = time.time() + 30
        while not os.path.exists(path) and time.time() < deadline:
            time.sleep(0.005)
        with open(path) as f:
            return json.load(f)

    def situation(self, kind: str, tag: str):
        """(session, extra tool args, repository): k1 = own project, .git open; hidgit = own project, .git
        hidden; unmounted = another project the session does not mount (git_run_projects, unlocked)."""
        name = "n6%s%s" % (kind, tag)
        repo = build(name)
        self.m.repo_top(repo, record="start")
        s = self._live()
        if kind == "unmounted":
            _env.register("team-x/" + name, repo)
            own = build(name + "own")
            self.m.repo_top(own, record="start")
            s.path, s.excludes, s.coverage = own, [HIDDEN], []
            s.unlock.update(projects=True, projects_write=True)
            return s, {"project": "team-x/" + name}, repo
        s.path, s.excludes, s.coverage = repo, [HIDDEN] + ([".git"] if kind == "hidgit" else []), []
        return s, {}, repo

    def run_git(self, sit, argv):
        s, extra, _repo = sit
        cards = []
        with mock.patch.object(self.m, "ask", lambda kind, sess, *a, **k: (cards.append(kind) if sess is s else None) or False):
            r = self._tool(s, dict(extra, args=argv))
        return cards, r


class MailboxPublicationTest(GitRunExposedBase):
    def test_requests_are_complete_before_the_poller_sees_them(self):
        """A poll during json.dump must not consume a partial test request and lose its reply."""
        from test_prodcheck import ProdCheckTest
        from test_sessions import SessionsTest
        s = self._live()
        dump = json.dump

        def poll_during_write(data, f, *a, **kw):
            if isinstance(data, dict) and data.get("tool") == "git_run":
                f.write("{")
                f.flush()
                out = os.path.join(s.cfg, "piper-mailbox", "out")
                self.assertFalse(any(n.endswith(".json") for n in os.listdir(out)), "partial request published")
                self.m._mailbox(s, self.m._mail_busy)
                f.seek(0)
                f.truncate()
            return dump(data, f, *a, **kw)

        args = {"args": ["status"]}
        for name, send in (("fuzz", lambda: self._tool(s, args)),
                           ("sessions", lambda: SessionsTest._tool(self, s, "git_run", args)),
                           ("production", lambda: ProdCheckTest._tool(self, s, "git_run", args))):
            with self.subTest(publisher=name), mock.patch.object(json, "dump", poll_during_write), \
                    mock.patch.object(self.m, "tool", return_value={"stdout": "complete request"}) as tool:
                reply = send()
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["result"], {"stdout": "complete request"})
                tool.assert_called_once_with(s, "git_run", args)


# ------------------------------------------------------------------ the argv generator (git's own option lists)
_HELP_CACHE: dict = {}


def _git_out(*a) -> str:
    r = subprocess.run(["git", *a], capture_output=True, text=True, env=ENV)
    return r.stdout + r.stderr


def options_of(sub: str) -> list[tuple[str, bool]]:
    """(option, takes a value) from git itself: --git-completion-helper-all (long), `-h` (short, value marked by
    `<arg>`), and for the revision walkers rev-list -h plus diff's list. A fixed hand list adds the forms earlier
    rounds found (short pickaxe, -L, colour words)."""
    if sub in _HELP_CACHE:
        return _HELP_CACHE[sub]
    opts: dict[str, bool] = {}
    helper = _git_out(sub, "--git-completion-helper-all")
    if sub in ("log", "show", "whatchanged", "reflog", "diff-tree", "diff-index", "diff-files", "stash"):
        helper += " " + _git_out("diff", "--git-completion-helper-all")
    for w in helper.split():
        if w.startswith("--") and w != "--":
            opts[w.rstrip("=")] = w.endswith("=")
    usage = _git_out(sub, "-h")
    if sub in ("log", "show", "whatchanged", "reflog", "rev-list", "shortlog"):
        usage += _git_out("rev-list", "-h")
    for m in re.finditer(r"^\s+(-[A-Za-z0-9])(?:,\s+(--[\w-]+))?(\s+<[^>]+>|\[=?<[^>]+>\])?", usage, re.M):
        opts[m.group(1)] = bool(m.group(3) and m.group(3).strip().startswith("<"))
    for m in re.finditer(r"^\s+(--[\w-]+)(=<[^>]+>|\[=[^\]]+\])?", usage, re.M):
        opts.setdefault(m.group(1), bool(m.group(2) and m.group(2).startswith("=<")))
    for o, v in (("-S", True), ("-G", True), ("-L", True), ("-p", False), ("--graph", False), ("--color-words", False),
                 ("--word-diff", False), ("--relative", False), ("--check", False), ("-F", True), ("-t", True),
                 ("-c", False), ("--cc", False), ("-m", True), ("--format", True), ("--pretty", True), ("-O", True),
                 ("--textconv", False), ("--binary", False), ("-R", False), ("--stat", False), ("--no-prefix", False),
                 # N7: options that take a FILE (read or written on the host) - through a symlink / another case
                 ("--pathspec-from-file", True), ("--template", True), ("--output", True), ("--contents", True),
                 ("--ignore-revs-file", True), ("--file", True), ("--exclude-from", True), ("--output-directory", True),
                 ("-o", True), ("-C", True), ("-c", True)):
        opts.setdefault(o, v)
    _HELP_CACHE[sub] = sorted(opts.items())
    return _HELP_CACHE[sub]


SUBS = ("status", "log", "show", "diff", "diff-tree", "diff-index", "diff-files", "grep", "blame", "ls-files", "ls-tree",
        "rev-list", "rev-parse", "describe", "name-rev", "for-each-ref", "show-ref", "shortlog", "reflog", "tag",
        "branch", "stash", "commit", "add", "rm", "mv", "checkout", "restore", "reset", "switch", "merge", "cherry-pick",
        "revert", "cat-file", "archive", "range-diff", "notes", "show-branch", "cherry", "merge-base", "whatchanged",
        "check-attr", "check-ignore", "count-objects", "format-patch",
        # N7: subcommands that read a file named as an argument (through a symlink they read a hidden one)
        "config", "interpret-trailers", "merge-file", "hash-object", "stripspace", "apply", "mailinfo",
        # N8: what comes through the server (origin's main rewrites a clean hidden file - serve_origin)
        "pull", "fetch")
READS = frozenset(("status", "log", "show", "diff", "diff-tree", "diff-index", "diff-files", "grep", "blame", "ls-files",
                   "ls-tree", "rev-list", "rev-parse", "describe", "name-rev", "for-each-ref", "show-ref", "shortlog",
                   "reflog", "cat-file", "range-diff", "show-branch", "cherry", "merge-base", "whatchanged",
                   "check-attr", "check-ignore", "count-objects"))


# N7: the hidden file by another name - a case variant (APFS / NTFS are case-insensitive), a symlink in another
# case, a path with ./, //, a detour through ..; and the global options that move git elsewhere
CASE_VARIANTS = ["CONFIG/secrets/db.properties", "config/SECRETS/db.properties", "Config/Secrets",
                 "CONFIG/secrets/local.properties", "LNK", "lnkDIR/secrets/db.properties", "LnkDir/Secrets",
                 "config//secrets/./db.properties", "src/../config/secrets/db.properties", "./lnk", "lnk/"]
GLOBALS = [["-C", "lnkdir"], ["-C", "LNKDIR"], ["-C", "config"], ["--git-dir=lnkdir"], ["--work-tree=lnkdir"],
           ["--work-tree", "config"], ["--no-pager"], ["--literal-pathspecs"], ["--icase-pathspecs"], ["--glob-pathspecs"],
           ["--noglob-pathspecs"], ["--no-replace-objects"], ["--no-optional-locks"], ["-P"]]


def value_pool(repo: str) -> list[str]:
    return ["always", "3", "1,2", "config", HIDDEN, "lnk", "lnkdir/secrets/db.properties", "HEAD:config",
            oid(repo, "HEAD:config"), oid(repo, "HEAD:" + HIDDEN + "/db.properties"), "%(raw)", "%(contents)", "%B",
            "%x0adiff --git a/README.md b/README.md", ".", "blobtag", "treetag", "pw=", "raw", "color", "HEAD", "x",
            "porcelain", "%H %s", "%(refname) %(subject)", "-committerdate", "short", "5"] + CASE_VARIANTS


def positional_pool(repo: str) -> list[list[str]]:
    return [[], ["HEAD"], ["HEAD~1"], ["HEAD~1..HEAD"], ["HEAD~1", "HEAD"], ["HEAD:config"], ["HEAD~1:config", "HEAD:config"],
            [oid(repo, "HEAD:config")], [oid(repo, "HEAD:config/secrets")],
            [oid(repo, "HEAD:" + HIDDEN + "/db.properties")], ["blobtag"], ["treetag"], ["v1"], ["stash@{0}"],
            ["stash@{0}^2"], ["README.md"], ["config"], ["lnk"], ["--", "config"], ["--", "."], ["--", "lnk"],
            ["--", ":(glob)**/*.properties"], [":/two"], ["main"], ["HEAD^{tree}"], ["HEAD", "--", "lnkdir"],
            ["-"], ["x"], ["stash@{0}", "--", "config"]] + [[v] for v in CASE_VARIANTS] + [
            ["--", v] for v in CASE_VARIANTS] + [["--", ":(icase)CONFIG"], ["HEAD", "--", ":(icase)Config/Secrets"],
                                                 ["README.md", "lnk", "README.md"], ["-", "lnk"],
                                                 # N7: two arguments (mv / rm of a directory with hidden files)
                                                 ["config", "moved"], ["Config", "src"], ["lnkdir", "moved"],
                                                 ["config/secrets/..", "moved"], ["conf*"], ["--", "config", "moved"],
                                                 # N8: the remote and its branches
                                                 ["origin"], ["origin", "main"], ["origin", "evil"], ["FETCH_HEAD"],
                                                 ["@{u}"], ["origin/main"], ["origin/evil"], ["1"], ["0"], ["b9", "1"]]


def _forms(o: str, takes: bool, values: list[str]) -> list[list[str]]:
    if not takes:
        return [[o]]
    return [[o + "=" + v] if o.startswith("--") else [o, v] for v in values] + [[o, v] for v in values if o.startswith("--")]


_ALLOWED: dict = {}


def allowed_options(repo: str, sub: str, values: list[str]) -> list[list[str]]:
    """git's own options of `sub` (with pool values) that the allowlist lets through - the directed half of the
    property test aims at what runs card-free."""
    if sub not in _ALLOWED:   # the option's shape only (revisions are checked when the case runs): no git here
        with mock.patch.object(gitops, "_rev_kind", lambda top, rev: True):
            _ALLOWED[sub] = [f for o, t in options_of(sub) for f in _forms(o, t, values)
                             if not gitops.exposed_blockers(repo, [sub, *f])] or [[]]
    return _ALLOWED[sub]


def gen_cases(repo: str, seed: int, n: int):
    """Half: any option git lists for the subcommand; half: only options the allowlist accepts (so the card-free
    space itself is searched), each with a revision / path from a pool of hidden-content handles."""
    rnd = random.Random(seed)
    values, positionals = value_pool(repo), positional_pool(repo)
    directed_subs = [x for x in SUBS if x in gitops.EXPOSED]
    for k in range(n):
        if k % 8 == 6:   # N8: through the server - a pull / a merge of what a fetch brought, few options
            sub = rnd.choice(("pull", "pull", "pull", "merge", "rebase", "cherry-pick", "checkout", "switch", "reset",
                              "restore"))
            argv = [sub] + rnd.choice(([], [], ["-q"], ["--ff-only"], ["--ff-only"], ["-q", "--ff-only"], ["--rebase"],
                                       ["--no-rebase"], ["--no-ff"],
                                       ["--autostash"], ["--keep"], ["--merge"], ["-s", "origin/evil"],
                                       ["--source=origin/evil"], ["--stat"], ["--squash"]))
            argv += rnd.choice(([], [], ["origin"], ["origin", "main"], ["origin", "evil"]) if sub == "pull" else
                               ([], ["FETCH_HEAD"], ["@{u}"], ["origin/main"], ["origin/evil"], ["origin/evil", "--", "config"],
                                ["--", "config"]))
            if sub != "pull" and rnd.random() < 0.5:
                yield ["fetch"] + rnd.choice(([], ["origin"], ["origin", "evil"]))
            yield argv
            continue
        directed = k % 2 == 1
        sub = rnd.choice(directed_subs if directed else SUBS)
        argv = [sub]
        if sub == "stash" and rnd.random() < 0.7:
            argv.append(rnd.choice(["show", "list", "push", "apply", "pop", "branch", "create"]))
        for _k in range(rnd.choice((0, 1, 1, 2, 2, 3))):
            if directed:
                argv += rnd.choice(allowed_options(repo, sub, values))
            else:
                o, takes = rnd.choice(options_of(sub))
                argv += rnd.choice(_forms(o, takes, values))
        argv += rnd.choice(positionals)
        if rnd.random() < 0.12:   # N7: global options first (-C / --work-tree through a link, icase pathspecs)
            argv = rnd.choice(GLOBALS) + argv
        yield argv


class ExposedPropertyTest(GitRunExposedBase):
    def _fuzz(self, kind: str, words):
        sit = self.situation(kind, "fz")
        repo = sit[2]
        # N8: a CLEAN hidden file (git may overwrite it) and an origin whose main / evil rewrite it - the agent's
        # commit come through the server (pull, fetch + merge FETCH_HEAD / @{u} / origin/evil ...)
        with open(os.path.join(repo, HIDDEN, "clean.properties"), "w") as f:
            f.write("pw=CLEAN\n")
        _g(repo, "add", "-f", HIDDEN + "/clean.properties")
        _g(repo, "commit", "-q", "-m", "clean", "--", HIDDEN + "/clean.properties")
        srv = serve_origin(repo)
        self.addCleanup(shutil.rmtree, srv, True)
        evil_commit(repo, HIDDEN + "/clean.properties", "pw=AGENT\n", "evil")
        _g(repo, "push", "-q", srv, "evil:main", "evil:evil")
        _g(srv, "update-server-info")
        _g(repo, "branch", "-D", "evil")
        pristine = repo + ".pristine"
        shutil.rmtree(pristine, ignore_errors=True)
        shutil.copytree(repo, pristine, symlinks=True)
        self.addCleanup(shutil.rmtree, pristine, True)
        leaks, ran, free = [], 0, 0
        hidden0 = hidden_state(repo)
        start = time.time()
        for j, seed in enumerate(SEEDS):
            for k, argv in enumerate(gen_cases(repo, seed, int(os.environ.get("N6_FUZZ_N") or 400))):
                # every seed gets its share of the time - and (N9: a loaded machine ran 21 in the budget) at least
                # MIN_PER_SEED cases, however long they take: the floor is a count, not a time
                if k >= MIN_PER_SEED and time.time() - start > BUDGET_S * (j + 1) / len(SEEDS):
                    break
                cards, r = self.run_git(sit, argv)
                ran += 1
                if cards:
                    continue
                free += 1
                txt = json.dumps(r)
                hit = [w for w in words if w in txt]
                moved = visible_leaks(repo, words) if argv[0] not in READS else []
                # N7: a card-free run never writes, moves or deletes a hidden work-tree file (through a link either)
                touched = hidden_state(repo) != hidden0
                # N8: the post-run net (Manager._keeping_hidden) puts a changed hidden file back - its note means
                # the checks before the run missed it: a finding even though nothing stayed changed
                net = "changed a file hidden from you" in txt
                if hit or moved or touched or net:
                    leaks.append((argv, hit, moved, "hidden file changed" if touched else "net put it back" if net else "",
                                  txt[:300]))
                if argv[0] not in READS:   # a card-free write changed the repository: back to the planted state
                    shutil.rmtree(repo)
                    shutil.copytree(pristine, repo, symlinks=True)
        print("\nN6 fuzz %s: %d argv, %d card-free, %.1f s" % (kind, ran, free, time.time() - start))
        self.assertEqual(leaks, [])
        self.assertGreater(ran, 40, "too few cases ran in the budget")

    def test_unmounted_project_never_returns_hidden_content_without_a_card(self):
        self._fuzz("unmounted", COMMITTED + WORKTREE)

    def test_hidden_git_session_never_returns_hidden_content_without_a_card(self):
        self._fuzz("hidgit", COMMITTED + WORKTREE)

    def test_open_git_session_never_returns_work_tree_hidden_content_without_a_card(self):
        """K1: the sandbox reads .git (committed content is its own), never the host's work-tree version."""
        self._fuzz("k1", WORKTREE)


# ordinary commands of a working session (read-only: they leave the fixture as it is)
ORDINARY_READS = [
    ["status"], ["status", "-s"], ["status", "--porcelain=v1", "-b"], ["log"], ["log", "--oneline", "-n", "5"],
    ["log", "-p", "-n", "2"], ["log", "--stat"], ["log", "--graph", "--oneline", "--all"], ["log", "--format=%H%x09%s"],
    ["log", "-p", "--", "README.md"], ["log", "--oneline", "HEAD~1..HEAD"], ["log", "--author=T", "--since=2.weeks"],
    ["diff"], ["diff", "--cached"], ["diff", "HEAD~1"], ["diff", "HEAD~1", "HEAD", "--stat"], ["diff", "--name-only"],
    ["diff", "HEAD~1..HEAD", "--", "README.md"], ["show", "HEAD"], ["show", "--stat", "HEAD"],
    ["show", "-s", "--format=%B", "HEAD"], ["show", "v1"], ["show"], ["branch"], ["branch", "-a"], ["branch", "-vv"],
    ["branch", "--show-current"], ["tag"], ["tag", "-l"], ["tag", "-n"], ["rev-parse", "HEAD"],
    ["rev-parse", "--abbrev-ref", "HEAD"], ["rev-parse", "--show-toplevel"], ["merge-base", "HEAD", "main"],
    ["describe", "--tags", "--always"], ["ls-files"], ["ls-tree", "-r", "--name-only", "HEAD"], ["blame", "README.md"],
    ["blame", "-L", "1,1", "README.md"], ["grep", "-n", "one"], ["grep", "-n", "two", "HEAD"], ["shortlog", "-sn", "HEAD"],
    ["reflog", "-n", "5"], ["stash", "list"], ["stash", "show"], ["stash", "show", "-p"], ["log", "-p", "-1", "stash@{0}"],
    ["rev-list", "--count", "HEAD"], ["show-ref"], ["name-rev", "HEAD"], ["cherry", "-v", "main"]]
# ordinary writes, run in this order on their own repository
ORDINARY_WRITES = [
    ["add", "README.md"], ["commit", "-m", "readme"], ["checkout", "-b", "feat"], ["switch", "main"],
    ["branch", "tmp"], ["branch", "-m", "tmp", "tmp2"], ["tag", "t1"], ["tag", "-a", "t2", "-m", "release"],
    ["checkout", "feat"], ["mv", "src/a.txt", "src/b.txt"], ["commit", "-qm", "move"], ["rm", "--cached", "src/b.txt"],
    ["reset", "-q", "HEAD"], ["restore", "--staged", "src/b.txt"], ["switch", "-c", "feat2"], ["merge", "main"],
    ["cherry-pick", "--no-commit", "main"], ["reset", "--soft", "HEAD"], ["commit", "--allow-empty", "-m", "e"],
    ["revert", "--no-edit", "HEAD"], ["rebase", "main"], ["checkout", "main"]]


class CardTableTest(GitRunExposedBase):
    """N6: what the allowlist costs - an ordinary command must not need a new card in the open-.git own project
    (K1, git_mode "hole"); where the sandbox cannot read .git the measured table below holds."""

    def test_new_profile_push_uses_push_card_and_status_stays_free(self):
        sit = self.situation("k1", "profilepush")
        with mock.patch.dict(self.m.policy, {"security_scoped": True, "ask": ["git_push"]}):
            cards, reply = self.run_git(sit, ["status"])
            self.assertEqual(cards, [])
            self.assertTrue(reply.get("ok"), reply)
            cards, reply = self.run_git(sit, ["push", "origin", "main"])
            self.assertEqual(cards, ["git_push"])
            self.assertFalse(reply.get("ok"), reply)

    def test_security_approval_switch_covers_ordinary_own_and_other_git(self):
        for kind, ask_kind in (("k1", "git_run"), ("unmounted", "git_run_project")):
            sit = self.situation(kind, "security")
            with mock.patch.dict(self.m.policy, {"ask": [ask_kind]}):
                cards, reply = self.run_git(sit, ["status"])
            self.assertEqual(cards, [ask_kind])
            self.assertFalse(reply.get("ok"), reply)

    def _table(self, kind, argvs, tag):
        sit = self.situation(kind, tag)
        return {tuple(a): self.run_git(sit, a) for a in argvs}

    def test_no_ordinary_command_needs_a_new_card_in_an_open_git_own_project(self):
        for argvs, tag in ((ORDINARY_READS, "tr"), (ORDINARY_WRITES, "tw")):
            res = self._table("k1", argvs, tag)
            carded = [(a, r.get("error")) for a, (cards, r) in res.items() if cards]
            self.assertEqual(carded, [])
            failed = [(a, r) for a, (cards, r) in res.items() if not r.get("ok")]
            self.assertEqual(failed, [], "the ordinary commands should succeed on the fixture")

    def test_ordinary_reads_where_the_sandbox_cannot_read_git(self):
        """Every ordinary read stays card-free where it was (log, diff, status, branch, show of a commit...),
        except what an allowlist cannot judge; the hidden content never comes back either way."""
        carded_expected = {
            # git_hidden: READONLY_SUBS need a card there since N3 (grep, blame, ls-files... - not new)
            "hidgit": {("rev-parse", "HEAD"), ("rev-parse", "--abbrev-ref", "HEAD"), ("rev-parse", "--show-toplevel"),
                       ("merge-base", "HEAD", "main"), ("describe", "--tags", "--always"), ("ls-files",),
                       ("ls-tree", "-r", "--name-only", "HEAD"), ("blame", "README.md"), ("blame", "-L", "1,1", "README.md"),
                       ("grep", "-n", "one"), ("grep", "-n", "two", "HEAD"), ("shortlog", "-sn", "HEAD"),
                       ("reflog", "-n", "5"), ("rev-list", "--count", "HEAD"), ("show-ref",), ("name-rev", "HEAD"),
                       ("cherry", "-v", "main")},
            # another checkout: not read-only subcommands ask on a git_run_project card (a26ac9a routing - not new)
            "unmounted": {("branch",), ("branch", "-a"), ("branch", "-vv"), ("tag",), ("tag", "-l"), ("tag", "-n"),
                          ("stash", "list"), ("stash", "show"), ("stash", "show", "-p")},
        }
        for kind in ("hidgit", "unmounted"):
            with self.subTest(kind=kind):
                res = self._table(kind, ORDINARY_READS, "tx")
                carded = {a for a, (cards, _r) in res.items() if cards}
                self.assertEqual(carded, carded_expected[kind])
                for a, (cards, r) in res.items():
                    self.assertFalse(any(w in json.dumps(r) for w in COMMITTED + WORKTREE), a)

    def test_ordinary_writes_in_a_hidden_git_session(self):
        res = self._table("hidgit", ORDINARY_WRITES, "txw")
        self.assertEqual([a for a, (cards, _r) in res.items() if cards], [])

    def test_show_ref_expansion_cannot_print_hidden_blobs_without_a_card(self):
        for kind in ("hidgit", "unmounted", "k1"):
            sit = self.situation(kind, "showrefs")
            _g(sit[2], "update-ref", "refs/remotes/origin/blobtag", oid(sit[2], "blobtag"))
            for argv in (["show", "--tags", "stash@{0}", "--", "config"],
                         ["show", "--all", "--", "config"],
                         ["show", "--branches", "--", "config"],
                         ["show", "--remotes", "--", "config"]):
                with self.subTest(kind=kind, argv=argv):
                    cards, reply = self.run_git(sit, argv)
                    if kind == "k1":
                        self.assertEqual(cards, [])  # committed objects are already readable in this session
                    else:
                        self.assertTrue(cards, "implicit refs can name a blob/tree, bypassing revision checks")
                        self.assertFalse(any(word in json.dumps(reply) for word in COMMITTED + WORKTREE))

    def test_what_an_allowlist_sends_to_a_card_where_the_sandbox_cannot_read_git(self):
        """The forms review rounds found one by one, and ones they had not: on a card, never card-free."""
        for kind in ("hidgit", "unmounted"):
            sit = self.situation(kind, "ty")
            repo = sit[2]
            blob, tree = oid(repo, "HEAD:" + HIDDEN + "/db.properties"), oid(repo, "HEAD:config")
            for argv in (["show", blob], ["show", "blobtag"], ["show", "treetag"], ["show", "HEAD:README.md"],
                         ["log", "-p", "--color-words"], ["log", "-p", "--word-diff"], ["log", "-p", "--relative=config"],
                         ["diff", tree, oid(repo, "HEAD~1:config")], ["diff", "HEAD~1:config", "HEAD:config"],
                         ["log", "-S", "pw=", "--oneline"], ["log", "-G", "pw", "--oneline"],
                         ["diff", "--src-prefix=x/", "HEAD~1"], ["log", "--show-signature"], ["for-each-ref", "--format=%(raw)"],
                         ["log", "--format=%GS"], ["log", "--notes"], ["tag", "--format=%(raw)"], ["branch", "--format=%(raw)"],
                         ["tag", "x", blob], ["branch", "--sort=raw"], ["for-each-ref", "--format=%(contents)"],
                         ["tag", "--format=%(contents:body)"], ["cat-file", "-p", blob],
                         ["archive", "--format=tar", "HEAD"], ["range-diff", "HEAD~1...HEAD"], ["rev-list", "--format=%B", "HEAD"],
                         ["ls-tree", "--format=%(objectname)", "HEAD"], ["restore", "--source=" + tree, "secrets"],
                         ["checkout", tree, "--", "secrets"], ["status", "-v"],
                         ["merge", "-s", "custom", "main"], ["log", "--end-of-options", "HEAD"]):
                with self.subTest(kind=kind, argv=argv):
                    cards, r = self.run_git(sit, argv)
                    self.assertTrue(cards, r)


class K1WorkTreeLeakTest(GitRunExposedBase):
    """N6 review: in the open-.git own project (K1) the sandbox reads committed content itself - but the host's
    WORK-TREE version of a hidden file only ever reaches it through git_run. These argv returned it card-free."""

    def test_a_file_option_through_a_symlink_never_reads_a_hidden_file(self):
        for i, argv in enumerate((["commit", "--allow-empty", "-F", "lnk"], ["commit", "--allow-empty", "-Flnk"],
                                  ["commit", "--allow-empty", "-F", "lnkdir/secrets/db.properties"],
                                  ["tag", "-F", "lnk", "t9"], ["tag", "-a", "-F", "lnk", "t9"], ["merge", "-F", "lnk", "HEAD~1"],
                                  ["blame", "-S", "lnk", "README.md"], ["blame", "-S" + HIDDEN + "/db.properties", "README.md"],
                                  ["commit", "--allow-empty", "-t", "lnk"])):
            with self.subTest(argv=argv):
                sit = self.situation("k1", "ln%d" % i)
                cards, r = self.run_git(sit, argv)
                after = _g(sit[2], "log", "-1", "--format=%B") + _g(sit[2], "for-each-ref", "--format=%(contents)")
                self.assertFalse(any(w in json.dumps(r) + after for w in WORKTREE) and not cards, (argv, r, after))

    def test_check_prints_no_line_of_a_hidden_file(self):
        """--check prints each whitespace error as `<path>:<n>: ...` plus the line - outside any diff block."""
        sit = self.situation("k1", "ck")
        with open(os.path.join(sit[2], "src", "a.txt"), "w") as f:
            f.write("b  \n")   # a visible whitespace error: its line stays
        for argv in (["diff", "--check"], ["diff", "--check", "HEAD"], ["log", "-p", "--check", "-1"],
                     ["stash", "show", "--check"], ["diff-files", "--check"], ["diff", "--check", "--", "src"]):
            with self.subTest(argv=argv):
                cards, r = self.run_git(sit, argv)
                self.assertEqual(cards, [], r)   # an ordinary check (a26ac9a) - card-free, the hidden line withheld
                self.assertFalse(any(w in json.dumps(r) for w in WORKTREE), (argv, r))
                if argv[1:] in (["--check"], ["--check", "--", "src"]):
                    self.assertIn("src/a.txt:1: trailing whitespace.\n+b  \n", r["result"]["output"])
        for kind in ("hidgit", "unmounted"):   # the allowlist keeps --check: check_filter judges its lines
            sit = self.situation(kind, "ck")
            cards, r = self.run_git(sit, ["diff", "--check"])
            self.assertEqual(cards, [], r)
            self.assertNotIn("PLANTED_", json.dumps(r))

    def _side(self, repo):
        """a branch `side` off HEAD~1 with a visible change: merging it is a real (non fast-forward) merge"""
        wt = repo + ".side"
        _g(repo, "worktree", "add", "-q", "-b", "side", wt, "HEAD~1")
        with open(os.path.join(wt, "src", "a.txt"), "w") as f:
            f.write("side\n")
        _g(wt, "commit", "-qam", "side")
        _g(repo, "worktree", "remove", "--force", wt)
        _g(repo, "checkout", "-q", "--", "README.md")

    def test_a_merge_never_hashes_a_dirty_hidden_file_into_git(self):
        """N6 review of stages_hidden (R5-2): a real merge runs `stash create` first (builtin/merge.c save_state) -
        the dirty hidden file's WORK-TREE content lands in .git/objects, which the sandbox reads (K1). The same for
        --autostash and checkout/switch --merge. A fast-forward / up-to-date merge writes nothing: no card."""
        def objects(repo):
            return subprocess.run(["git", "-C", repo, "cat-file", "--batch-all-objects", "--batch"],
                                  capture_output=True, env=ENV).stdout
        for i, argv in enumerate((["merge", "--no-edit", "side"], ["merge", "--no-ff", "--no-edit", "side"],
                                  ["merge", "--squash", "side"], ["merge", "--no-commit", "side"],
                                  ["merge", "--autostash", "--no-edit", "side"], ["checkout", "-m", "side"],
                                  ["switch", "-m", "side"], ["checkout", "--merge", "side"],
                                  ["rebase", "--autostash", "side"])):
            with self.subTest(argv=argv):
                sit = self.situation("k1", "mg%d" % i)
                self._side(sit[2])
                cards, r = self.run_git(sit, argv)
                self.assertFalse(any(w.encode() in objects(sit[2]) for w in WORKTREE) and not cards, (argv, r))
        sit = self.situation("k1", "mgff")   # controls: nothing merged, nothing hashed - card-free as before
        repo = sit[2]
        _g(repo, "branch", "ahead")
        _g(repo, "branch", "behind", "HEAD~1")
        for argv in (["merge", "behind"], ["merge", "--ff-only", "ahead"], ["merge", "ahead"]):
            cards, r = self.run_git(sit, argv)
            self.assertEqual((cards, r.get("ok")), ([], True), (argv, r))
        self.assertNotIn(b"PLANTED_W4", objects(repo))
        hidden = lambda p: p.startswith(HIDDEN)  # noqa: E731
        self.assertEqual(gitops.stages_hidden(repo, ["pull"], hidden), [HIDDEN + "/db.properties"])
        self.assertEqual(gitops.stages_hidden(repo, ["pull", "--ff-only"], hidden), [])

    def test_add_dry_run_names_fail_closed(self):
        """N6 review: `git add --dry-run` prints names raw in single quotes; a hidden file whose name has a newline
        split into `add 'config/secrets/a` + `b'` - the first part (with its quote) was judged visible."""
        repo = build("n6dryrun")
        with open(os.path.join(repo, HIDDEN, "a\nb"), "w") as f:
            f.write("PLANTED_NL\n")
        hidden = lambda p: os.path.normpath(p).startswith(HIDDEN + "/")  # noqa: E731
        self.assertTrue(gitops.stages_hidden(repo, ["add", "--", "config/secrets/a*"], hidden))
        self.assertEqual(gitops.stages_hidden(repo, ["add", "README.md"], hidden), [])   # visible: no card
        with open(os.path.join(repo, "it's here.txt"), "w") as f:
            f.write("x\n")
        self.assertEqual(gitops.stages_hidden(repo, ["add", "it's here.txt"], hidden), [])

    def test_a_commit_of_visible_paths_takes_no_hidden_file(self):
        """N6 review of R5-2: `commit -m x -- README.md` commits README.md's work tree only - yet it asked whenever ANY
        hidden file was dirty (in many projects the local config nearly always is). A pathspec that matches a dirty
        hidden file (`-- .`, `config`, magic) still asks."""
        repo = build("n6commitps")
        hidden = lambda p: os.path.normpath(p).startswith(HIDDEN + "/")  # noqa: E731
        for argv in (["commit", "-m", "x", "--", "README.md"], ["commit", "-m", "x", "README.md"],
                     ["commit", "-o", "-m", "x", "src"], ["commit", "-m", "x"], ["commit", "-qm", "x", "--", ":(icase)readme.md"],
                     ["commit", "-m", "x", "--", "README.md", ":(exclude)src"]):
            self.assertEqual(gitops.stages_hidden(repo, argv, hidden), [], argv)
        for argv in (["commit", "-m", "x", "--", "."], ["commit", "-m", "x", "config"], ["commit", "-am", "x"],
                     ["commit", "-i", "-m", "x", "--", ":(glob)**/*.properties"], ["commit", "-m", "x", "--", ":/"],
                     ["commit", "--all", "-m", "x"]):
            self.assertEqual(gitops.stages_hidden(repo, argv, hidden), [HIDDEN + "/db.properties"], argv)

    def test_grep_without_a_pattern_never_greps_the_hidden_excludes(self):
        """`git grep -v -r` has no pattern: git took grep_scope's first `:(exclude,literal)<hidden>` pathspec for
        it - nothing excluded, every line of the hidden file printed (found by the N6 fuzz)."""
        for kind in ("k1", "unmounted"):
            sit = self.situation(kind, "gp")
            for argv in (["grep", "--invert-match"], ["grep", "-v", "-r"], ["grep", "-c"], ["grep", "-l", "--untracked"],
                         ["grep", "--no-show-function", "-r", "--invert-match"]):
                with self.subTest(kind=kind, argv=argv):
                    cards, r = self.run_git(sit, argv)
                    self.assertFalse(any(w in json.dumps(r) for w in WORKTREE + COMMITTED) and not cards, (argv, r))


class K1WorkTreeWriteTest(GitRunExposedBase):
    """N7 review: card-free host git that MOVES, deletes or rewrites a hidden work-tree file. `mv config cfg2`
    put the real hidden files under a visible name (the sandbox reads them); `merge` / `checkout` / `cherry-pick`
    ... of a commit the sandbox wrote itself (K1: .git is open) replaced a hidden file with the agent's content -
    and a host rewrite (unlink + create) can detach the sandbox's over-mount of it."""

    def _evil(self, repo, rel, text, name):
        """a branch whose one commit changes `rel` - what the sandbox can build with its own open .git"""
        blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input=text, capture_output=True,
                              text=True, env=ENV, check=True).stdout.strip()
        env = dict(ENV, GIT_INDEX_FILE=os.path.join(repo, ".git", "n7-index"))
        subprocess.run(["git", "-C", repo, "read-tree", "HEAD"], env=env, check=True)
        subprocess.run(["git", "-C", repo, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, rel)], env=env, check=True)
        tree = subprocess.run(["git", "-C", repo, "write-tree"], env=env, capture_output=True, text=True, check=True).stdout.strip()
        os.unlink(os.path.join(repo, ".git", "n7-index"))
        _g(repo, "branch", name, _g(repo, "commit-tree", tree, "-p", "HEAD", "-m", "innocent"))

    def _pristine(self, repo):
        pristine = repo + ".pristine"
        shutil.rmtree(pristine, ignore_errors=True)
        shutil.copytree(repo, pristine, symlinks=True)
        self.addCleanup(shutil.rmtree, pristine, True)
        return pristine

    def _restore(self, repo, pristine):
        shutil.rmtree(repo)
        shutil.copytree(pristine, repo, symlinks=True)

    def test_moving_or_deleting_a_directory_with_hidden_files_goes_on_a_card(self):
        for kind in ("k1", "hidgit"):
            sit = self.situation(kind, "mvh")
            repo = sit[2]
            pristine = self._pristine(repo)
            for argv in (["mv", "config", "cfg2"], ["mv", "config", "src"], ["mv", "-k", "config", "c4"],
                         ["mv", "-f", "config", "c5"], ["mv", "Config", "c6"], ["rm", "-rf", "config"], ["rm", "-r", "-f", "Config"],
                         ["mv", "--", "config", "c7"], ["mv", "config/secrets/..", "c8"], ["rm", "-rf", "conf*"]):
                with self.subTest(kind=kind, argv=argv):
                    if "Config" in argv and not os.path.exists(os.path.join(repo, "Config")):
                        continue   # a case-sensitive file system (Linux CI): "Config" is no such path, git fails itself
                    try:
                        cards, r = self.run_git(sit, argv)
                        self.assertTrue(cards, r)
                        self.assertEqual(visible_leaks(repo, WORKTREE), [], argv)
                        self.assertTrue(os.path.exists(os.path.join(repo, HIDDEN, "db.properties")), argv)
                    finally:
                        self._restore(repo, pristine)
            for argv in (["mv", "src", "src2"], ["mv", "src2", "src"], ["rm", "-q", "README.md"]):   # controls
                with self.subTest(kind=kind, argv=argv):
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual((cards, r.get("ok")), ([], True), r)

    def test_a_moved_over_mount_hides_every_file_from_host_git(self):
        """F-N10-5: the sandbox moves the PARENT of an over-mounted directory (`mv config cfgx` - the mount goes
        along, verified with Docker Desktop). On the host the REAL hidden files now sit under cfgx/, a name the
        isolation rule does not hide: card-free `git add cfgx` staged PLANTED_W4 (and the ignored PLANTED_U5 -
        .gitignore names the old path) into .git, which the sandbox reads (K1). A pinned over-mount no longer at
        its path: every file counts as hidden for the session (cards, filtered output), the card says why. A file
        over-mount replaced at its path (a host editor's temp + rename, host git's checkout): names unchanged, a
        card - with the opt-in git_hidden_server_content (2026-10-12) none of its own, a hidden name still asks."""
        import guard
        sit = self.situation("k1", "mvo")
        s, _extra, repo = sit
        s.coverage = [{"root": repo, "overmounts": [HIDDEN, "README.md"],
                       "ids": guard.overmount_ids(repo, [HIDDEN, "README.md"]),
                       "pids": guard.overmount_ids(repo, [HIDDEN, "README.md"], parent=True)}]   # F-N11-1

        def run(argv):
            cards = []
            with mock.patch.object(self.m, "ask", lambda kind, sess, summary, *a, **k:
                                   (cards.append(summary) if sess is s else None) or False):
                return cards, self._tool(s, {"args": argv})
        cards, r = run(["add", "src/a.txt"])                                   # intact: card-free as before
        self.assertEqual((cards, r.get("ok")), ([], True), r)
        os.rename(os.path.join(repo, "config"), os.path.join(repo, "cfgx"))     # what the sandbox does
        self.addCleanup(lambda: os.path.isdir(os.path.join(repo, "cfgx")) and
                        os.rename(os.path.join(repo, "cfgx"), os.path.join(repo, "config")))
        for argv in (["add", "cfgx"], ["add", "cfgx/secrets/db.properties"], ["add", "-f", "cfgx"]):
            with self.subTest(argv=argv):
                cards, r = run(argv)
                self.assertTrue(cards, r)
                self.assertIn("was moved", cards[0])
        staged = subprocess.run(["git", "-C", repo, "grep", "--cached", "-l", "PLANTED_"], capture_output=True,
                                text=True, env=ENV).stdout
        self.assertNotIn("cfgx", staged)
        cards, r = run(["status", "--porcelain", "-uall"])
        self.assertNotIn("cfgx/secrets", json.dumps(r))
        self.assertTrue(getattr(self.m.hidden_for(s), "moved", None))
        s.coverage[0]["detached"] = [HIDDEN]                                   # a GUI move: recorded, not broken
        self.assertFalse(getattr(self.m.hidden_for(s), "moved", None))
        os.rename(os.path.join(repo, "cfgx"), os.path.join(repo, "config"))
        s.coverage[0]["detached"] = []
        with open(os.path.join(repo, "README.tmp"), "w") as f:                 # a host editor's safe write
            f.write("edited\n")
        os.replace(os.path.join(repo, "README.tmp"), os.path.join(repo, "README.md"))
        self.assertFalse(getattr(self.m.hidden_for(s), "moved", None))         # names still hide what they hid
        db = HIDDEN + "/db.properties"                                         # a HIDDEN-named file over-mount
        s.coverage[0]["overmounts"].append(db)
        s.coverage[0]["ids"].update(guard.overmount_ids(repo, [db]))
        s.coverage[0]["pids"].update(guard.overmount_ids(repo, [db], parent=True))
        with open(os.path.join(repo, db + ".tmp"), "w") as f:                  # replaced the same way
            f.write("PLANTED_EDITED\n")
        os.replace(os.path.join(repo, db + ".tmp"), os.path.join(repo, db))
        self.assertEqual(self.m._broken_overmounts(s), ([], [os.path.join(repo, "README.md"), os.path.join(repo, db)]))
        cards, r = run(["add", "src/a.txt"])   # the default: a card
        self.assertTrue(cards, r)
        self.assertIn("not the one pinned", cards[0])
        with mock.patch.dict(self.m.policy, git_hidden_server_content=True):   # opt-in 2026-10-12: no card of its own
            cards, r = run(["add", "src/a.txt"])
            self.assertEqual((cards, r.get("ok")), ([], True), r)
            cards, r = run(["add", "-f", db])  # what touches the replaced hidden name still asks
            self.assertTrue(cards, r)

    def test_a_stand_in_file_after_a_moved_parent_still_hides_every_file(self):
        """F-N11-1: F-N10-5 hid every file only when NOTHING or a directory sat at the moved over-mount's path; a
        regular FILE there counted as a host editor's safe write (names keep hiding). The sandbox moves the parent
        (`mv config cfgx`, the mounts go along) and puts a stand-in FILE back - at a file over-mount's path or at a
        directory over-mount's path - and the real hidden files sat under cfgx/ unhidden for every by-name reader
        (git_status, .http `< ./x`, db_script, uploads). The parent pinned at the start tells the two apart."""
        import guard
        for over, stand_in in (([HIDDEN + "/db.properties"], HIDDEN + "/db.properties"), ([HIDDEN], HIDDEN)):
            with self.subTest(over=over):
                s, _extra, repo = self.situation("k1", "sif%d" % len(over[0]))
                s.coverage = [guard.coverage(repo, over, {"candidates": []})]
                self.assertFalse(getattr(self.m.hidden_for(s), "moved", None))
                os.rename(os.path.join(repo, "config"), os.path.join(repo, "cfgx"))   # what the sandbox does
                os.makedirs(os.path.dirname(os.path.join(repo, stand_in)), exist_ok=True)
                with open(os.path.join(repo, stand_in), "w") as f:
                    f.write("stand-in\n")
                hidden = self.m.hidden_for(s)
                self.assertTrue(getattr(hidden, "moved", None), over)
                self.assertTrue(hidden("cfgx/secrets/db.properties"), over)
                st = gitops.session_status(repo, hidden)
                self.assertNotIn("cfgx", json.dumps(st))
        for over in ([HIDDEN + "/db.properties"], [HIDDEN]):   # a symlink back to the moved parent: lstat of the
            with self.subTest(link=over):                      # over-mount followed it - "intact", names unhidden
                s, _extra, repo = self.situation("k1", "sil%d" % len(over[0]))
                s.coverage = [guard.coverage(repo, over, {"candidates": []})]
                os.rename(os.path.join(repo, "config"), os.path.join(repo, "cfgx"))
                os.symlink("cfgx", os.path.join(repo, "config"))
                hidden = self.m.hidden_for(s)
                self.assertTrue(getattr(hidden, "moved", None), over)
                self.assertTrue(hidden("cfgx/secrets/db.properties"), over)
                self.assertEqual(guard.intact_overmounts(s.coverage[0]), set())
        s, _extra, repo = self.situation("k1", "sied")   # control: a host editor's safe write keeps names hiding
        s.coverage = [guard.coverage(repo, ["README.md"], {"candidates": []})]
        with open(os.path.join(repo, "README.tmp"), "w") as f:
            f.write("edited\n")
        os.replace(os.path.join(repo, "README.tmp"), os.path.join(repo, "README.md"))
        self.assertFalse(getattr(self.m.hidden_for(s), "moved", None))
        self.assertTrue(self.m._broken_overmounts(s)[1])

    def test_a_run_that_rewrites_a_hidden_work_tree_file_goes_on_a_card(self):
        sit = self.situation("k1", "rwh")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")   # clean: git would overwrite it
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        self._evil(repo, "src/good.txt", "good\n", "good")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        for argv in (["merge", "evil"], ["merge", "--ff-only", "evil"], ["merge", "--no-ff", "-m", "m", "evil"],
                     ["checkout", "evil"], ["switch", "evil"], ["switch", "--detach", "evil"], ["checkout", "-B", "x2", "evil"],
                     ["switch", "-C", "x3", "evil"], ["cherry-pick", "evil"], ["cherry-pick", "main..evil"], ["rebase", "evil"],
                     ["rebase", "--onto", "evil", "HEAD"], ["reset", "--merge", "evil"], ["reset", "--keep", "evil"],
                     ["checkout", "evil", "config"], ["restore", "--source=evil", "config"], ["restore", "-s", "evil", "config"],
                     ["revert", "--no-edit", "evil"], ["switch", "--orphan", "o1"], ["checkout", "-"], ["merge", "-"],
                     ["cherry-pick", "^main", "evil"]):
            with self.subTest(argv=argv):
                try:
                    if argv[-1] == "-":   # the previous branch: evil
                        _g(repo, "checkout", "-q", "evil")
                        _g(repo, "checkout", "-q", "main")
                    cards, r = self.run_git(sit, argv)
                    with open(hid) as f:
                        self.assertEqual(f.read(), before, (argv, r))
                    self.assertTrue(cards, (argv, r))
                finally:
                    self._restore(repo, pristine)
        for argv in (["merge", "good"], ["checkout", "good"], ["cherry-pick", "good"], ["switch", "-c", "feat", "good"],
                     ["restore", "--staged", "config"]):
            with self.subTest(argv=argv):   # controls: a branch that changes only visible files - no card
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual((cards, r.get("ok"), r.get("result", {}).get("rc")), ([], True, 0), (argv, r))
                finally:
                    self._restore(repo, pristine)
        for argv in (["merge", "nosuchbranch"], ["cherry-pick", "typo"], ["rebase", "nosuch"], ["reset", "--keep", "nosuch"],
                     ["restore", "--source=nosuch", "config"]):
            with self.subTest(argv=argv):   # a target git refuses anyway: no card that then fails (like B-N6-4)
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual(cards, [], (argv, r))
                finally:
                    self._restore(repo, pristine)

    def _on_server_of(self, repo, *branches):
        """_on_server as the GitLab server answers: the blobs of `branches` exist there, nothing else"""
        there = set()
        for b in branches:
            there |= set(_g(repo, "ls-tree", "-r", "--format=%(objectname)", b).split())
        return mock.patch.object(self.m, "_on_server", lambda top, blobs: set(blobs) <= there)

    def test_a_branch_switch_with_hidden_content_from_the_server_asks_nothing(self):
        """2026-10-12 (user's decision): switching feature branches / merging / pulling rewrote .gitlab-ci.yml or
        secrets.yml - a card every time. Content that is on the GitLab server runs card-free and stays (the N8 net
        keeps exactly those blobs); content only the sandbox committed, a hidden file the target deletes and a ref
        the sandbox moves to its own commit during the run still ask / are put back."""
        sit = self.situation("k1", "srv")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://team-db/x\n", "feature")   # on the server
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")  # local only
        pristine = self._pristine(repo)
        self.enterContext(mock.patch.dict(self.m.policy, git_hidden_server_content=True))   # the opt-in
        with self._on_server_of(repo, "main", "feature"):
            for argv in (["switch", "feature"], ["checkout", "feature"], ["switch", "-c", "f2", "feature"], ["merge", "feature"]):
                with self.subTest(argv=argv):
                    try:
                        cards, r = self.run_git(sit, argv)
                        self.assertEqual((cards, r.get("ok"), r.get("result", {}).get("rc")), ([], True, 0), (argv, r))
                        with open(hid) as f:
                            self.assertIn("team-db", f.read(), (argv, r))
                    finally:
                        self._restore(repo, pristine)
            with mock.patch.dict(self.m.policy, git_hidden_server_content=False):   # the default: the card
                try:
                    cards, r = self.run_git(sit, ["switch", "feature"])
                    self.assertTrue(cards, r)
                finally:
                    self._restore(repo, pristine)
            for argv in (["switch", "evil"], ["merge", "evil"], ["restore", "--source=feature", "config"]):
                with self.subTest(argv=argv):   # not on the server / a form the check does not model: a card
                    try:
                        cards, r = self.run_git(sit, argv)
                        self.assertTrue(cards, (argv, r))
                        with open(hid) as f:
                            self.assertEqual(f.read(), before, (argv, r))
                    finally:
                        self._restore(repo, pristine)
            real_run = gitops.session_run

            def race(top, args, *a, **k):   # the sandbox moves the checked ref to its own commit (K1)
                _g(repo, "update-ref", "refs/heads/feature", oid(repo, "evil"))
                return real_run(top, args, *a, **k)
            try:
                self._cover(sit[0], repo)   # the net writes only through an intact over-mount (N9)
                with mock.patch.object(gitops, "session_run", race):
                    cards, r = self.run_git(sit, ["merge", "feature"])
                with open(hid) as f:
                    self.assertNotIn("attacker", f.read(), r)
            finally:
                self._restore(repo, pristine)
        try:   # a hidden file the target lacks: git deletes it, the net would put it back - a card
            _g(repo, "branch", "nohid", _g(repo, "commit-tree", _g(repo, "write-tree"), "-p", "HEAD", "-m", "x"))
            _g(repo, "rm", "-q", "--cached", HIDDEN + "/db.properties")
            _g(repo, "branch", "-f", "nohid", _g(repo, "commit-tree", _g(repo, "write-tree"), "-p", "HEAD", "-m", "x"))
            _g(repo, "reset", "-q")
            with self._on_server_of(repo, "main", "nohid"):
                cards, r = self.run_git(sit, ["switch", "nohid"])
            self.assertTrue(cards, r)
            self.assertTrue(os.path.isfile(hid))
        finally:
            self._restore(repo, pristine)

    def test_a_merge_of_a_hidden_file_both_sides_changed_still_asks(self):
        """review 2026-10-12: HEAD and the target both changed the hidden file (all blobs on the server) - git merges
        their content into a NEW blob (or conflict markers) the server lacks, the net put the old one back under a
        finished merge: card-free, silently diverged. Only a merge whose result is the target's blob (or HEAD's)
        runs card-free; an octopus asks."""
        sit = self.situation("k1", "both")
        self.enterContext(mock.patch.dict(self.m.policy, git_hidden_server_content=True))   # the opt-in
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://team-db/x\n", "feature")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://other-db/x\n", "side")
        self._evil(repo, "README.md", "five\n", "readme")
        _g(repo, "checkout", "-q", "side")
        with self._on_server_of(repo, "main", "feature", "side", "readme"):
            cards, r = self.run_git(sit, ["merge", "feature"])
            self.assertTrue(cards, r)
            self.assertIsNone(gitops.rewrite_blobs(repo, ["merge", "readme", "feature"], [HIDDEN + "/db.properties"]))

    def test_a_branch_that_adds_a_hidden_file_over_a_local_ignored_one_still_asks(self):
        """review 2026-10-12: git overwrites an IGNORED file a target tracks (local.properties: the user's local
        secrets); its server blob was in `keep`, so the net let it stand - the original gone, no trash, no card."""
        sit = self.situation("k1", "addl")
        self.enterContext(mock.patch.dict(self.m.policy, git_hidden_server_content=True))   # the opt-in
        repo = sit[2]
        loc = os.path.join(repo, HIDDEN, "local.properties")
        self._evil(repo, HIDDEN + "/local.properties", "pw=team\n", "addlocal")
        pristine = self._pristine(repo)
        with self._on_server_of(repo, "main", "addlocal"):
            for argv in (["switch", "addlocal"], ["merge", "addlocal"]):
                with self.subTest(argv=argv):
                    try:
                        cards, r = self.run_git(sit, argv)
                        self.assertTrue(cards, (argv, r))
                        with open(loc) as f:
                            self.assertIn("PLANTED_U5", f.read(), (argv, r))
                    finally:
                        self._restore(repo, pristine)

    def test_on_server_reads_the_recorded_origin_and_the_blob_api(self):
        """_on_server: the host-recorded origin (never the live .git/config), GitLab's blob API; 404 = not there."""
        import gitlab
        calls = []

        def opener(req, timeout=0):
            calls.append(req.full_url)
            if "bad" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return io.BytesIO(b"x")
        with mock.patch.object(self.m, "origin_gl", lambda top: "grp/repo"), \
                mock.patch.object(gitlab, "token", lambda: "t"), \
                mock.patch.object(gitlab, "_OPENER", mock.Mock(open=opener)):
            self.assertTrue(self.m._on_server("/x", {"good1"}))
            self.assertFalse(self.m._on_server("/x", {"good1", "bad2"}))
            self.assertIn("/projects/grp%2Frepo/repository/blobs/good1/raw", calls[0])
            n = len(calls)
            self.assertTrue(self.m._on_server("/x", {"good1"}))
            self.assertEqual(len(calls), n)   # cached
        with mock.patch.object(self.m, "origin_gl", lambda top: None):
            self.assertFalse(self.m._on_server("/x", {"good1"}))

    # ------------------------------------------------------------------ N8: through the server (pull)
    def _server(self, repo):
        # N9: no detached auto maintenance after a fetch / merge - a copytree of .git raced its maintenance.lock (load run)
        _g(repo, "config", "maintenance.auto", "false")
        _g(repo, "config", "gc.auto", "0")
        srv = serve_origin(repo)
        self.addCleanup(shutil.rmtree, srv, True)
        return srv

    def _pushed(self, repo, rel, text, name="evil", base="main"):
        """the agent's commit on the server's main (K1: it writes the commit itself, push is card-free); the
        local remote-tracking ref still shows the old main - the pull fetches it"""
        self._evil(repo, rel, text, name)
        srv = repo + ".srv.git"
        _g(repo, "push", "-q", srv, "%s:%s" % (name, base))
        _g(srv, "update-server-info")
        _g(repo, "branch", "-D", name)

    def test_a_pull_of_hidden_content_the_server_has_asks_nothing(self):
        """2026-10-12: a teammate's change of a hidden file on the server - `git pull` runs card-free and keeps it."""
        sit = self.situation("k1", "pus")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties", "README.md")
        self._server(repo)
        self._pushed(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://team-db/x\n")
        blobs = set(_g(repo + ".srv.git", "ls-tree", "-r", "--format=%(objectname)", "main").split())
        self.enterContext(mock.patch.dict(self.m.policy, git_hidden_server_content=True))   # the opt-in
        with mock.patch.object(self.m, "_on_server", lambda top, b: set(b) <= blobs | set(
                _g(repo, "ls-tree", "-r", "--format=%(objectname)", "HEAD").split())):
            cards, r = self.run_git(sit, ["pull", "--ff-only"])
        self.assertEqual((cards, r.get("ok"), r.get("result", {}).get("rc")), ([], True, 0), r)
        with open(os.path.join(repo, HIDDEN, "db.properties")) as f:
            self.assertIn("team-db", f.read())

    def test_a_pull_never_rewrites_a_hidden_file_with_what_the_agent_pushed(self):
        """N8 (F-N7-2, decision 1): with .git open the agent pushes a commit that changes a hidden file, then a
        card-free `git pull` wrote it into the host work tree. The card-free pull now fetches, checks what it fetched
        and merges / rebases exactly that commit - a hidden file it would rewrite goes on a card."""
        sit = self.situation("k1", "pul")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties", "README.md")   # clean: git would overwrite it
        self._server(repo)
        self._pushed(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        for seq in ([["pull"]], [["pull", "origin", "main"]], [["pull", "--rebase"]], [["pull", "--no-rebase"]],
                    [["pull", "--ff-only"]], [["pull", "-q"]], [["pull", "--no-ff"]], [["pull", "--rebase", "origin"]],
                    [["fetch"], ["merge", "FETCH_HEAD"]], [["fetch", "origin", "main"], ["merge", "FETCH_HEAD"]],
                    [["fetch"], ["merge", "@{u}"]], [["fetch"], ["merge", "--ff-only", "origin/main"]],
                    [["fetch"], ["rebase", "@{u}"]], [["fetch"], ["reset", "--hard", "origin/main"]],
                    [["fetch"], ["checkout", "origin/main", "--", "config"]], [["fetch"], ["restore", "-s", "@{u}", "config"]]):
            with self.subTest(seq=seq):
                try:
                    cards = []
                    for argv in seq:
                        c, r = self.run_git(sit, argv)
                        cards += c
                        if argv[0] == "fetch":
                            self.assertEqual((c, r.get("ok")), ([], True), r)   # a fetch stays card-free
                    with open(hid) as f:
                        self.assertEqual(f.read(), before, (seq, r))
                    self.assertTrue(cards, (seq, r))
                finally:
                    self._restore(repo, pristine)
        asked = []   # the card itself: the ⚠ line first, the pull as typed, the step it runs after Allow
        try:
            with mock.patch.object(self.m, "ask", lambda kind, sess, summary, payload, **k: asked.append(
                    (kind, summary, payload)) or False):
                r = self._tool(sit[0], {"args": ["pull"]})
        finally:
            self._restore(repo, pristine)
        self.assertEqual(len(asked), 1, asked)
        kind, summary, payload = asked[0]
        self.assertEqual(summary.split("\n")[:2], ["git pull", "⚠ rewrites a hidden file in the work tree with content "
                                                                "from git (config/secrets/db.properties)"])
        self.assertEqual((kind, payload["args"], payload["step"][0], payload["step"][-1][:7]),
                         ("git_run", ["pull"], "merge", "%s" % oid(repo + ".srv.git", "main")[:7]))
        self.assertIn("fetched, nothing merged", r["error"])

    def test_a_card_free_pull_looks_like_pull(self):
        """N8: the checked pull prints what pull prints and ends where pull ends (the same run on a copy with the
        real `git pull`): fast-forward, merge, rebase, up to date, divergent without a mode, --ff-only, no
        upstream, a dirty work tree under --rebase, pull.rebase / pull.ff config."""
        sit = self.situation("k1", "pll")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties", "README.md")
        self._server(repo)
        self._pushed(repo, "src/good.txt", "good\n", "good")
        pristine = self._pristine(repo)
        twin = repo + ".twin"
        self.addCleanup(shutil.rmtree, twin, True)

        def local_commit(r):
            with open(os.path.join(r, "README.md"), "w") as f:
                f.write("local\n")
            _g(r, "commit", "-qam", "local")

        def dirty(r):
            with open(os.path.join(r, "README.md"), "w") as f:
                f.write("dirty\n")

        def nobranch(r):
            _g(r, "checkout", "-q", "-b", "nou")

        def cfg(k, v):
            return lambda r: _g(r, "config", k, v)

        scenarios = [("ff", None, ["pull"]), ("ff -q", None, ["pull", "-q"]), ("ff rebase", None, ["pull", "--rebase"]),
                     ("ff origin main", None, ["pull", "origin", "main"]), ("ff --stat", None, ["pull", "--stat"]),
                     ("diverged", local_commit, ["pull"]), ("diverged no-rebase", local_commit, ["pull", "--no-rebase"]),
                     ("diverged log", local_commit, ["pull", "--no-rebase", "--log"]),
                     ("diverged rebase", local_commit, ["pull", "--rebase"]), ("diverged ff-only", local_commit, ["pull", "--ff-only"]),
                     ("diverged pull.rebase", lambda r: (local_commit(r), cfg("pull.rebase", "true")(r)), ["pull"]),
                     ("diverged pull.ff=only", lambda r: (local_commit(r), cfg("pull.ff", "only")(r)), ["pull"]),
                     ("pull.ff=only + --rebase", lambda r: (local_commit(r), cfg("pull.ff", "only")(r)), ["pull", "--rebase"]),
                     ("diverged no-ff", local_commit, ["pull", "--no-ff"]), ("ff squash", None, ["pull", "--squash"]),
                     ("diverged squash", local_commit, ["pull", "--no-rebase", "--squash"]),
                     ("ff --all --progress", None, ["pull", "--all", "--progress"]),
                     ("diverged set-upstream", local_commit, ["pull", "--no-rebase", "--set-upstream", "origin", "main"]),
                     ("dirty rebase", dirty, ["pull", "--rebase"]), ("no upstream", nobranch, ["pull"]),
                     ("no upstream rebase", nobranch, ["pull", "--rebase"]), ("no such ref", None, ["pull", "origin", "nosuch"])]
        for name, prep, argv in scenarios:
            with self.subTest(name=name, argv=argv):
                try:
                    if prep:
                        prep(repo)
                    shutil.rmtree(twin, ignore_errors=True)
                    shutil.copytree(repo, twin, symlinks=True)
                    real = subprocess.run(["git", "-C", twin, *argv], capture_output=True, env=dict(ENV, LC_ALL="C"))
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual(cards, [], r)
                    res = r.get("result") or {}
                    self.assertEqual(res.get("rc"), real.returncode, (r, real))
                    if SAME_WORDING:
                        self.assertEqual(res.get("output"), (real.stdout + real.stderr).decode(), r)

                    def end(x):   # tree, parents, message, reflog subject, work tree - not the dates in the oids
                        return [_g(x, "log", "-1", "--format=%T|%p|%B").count("|"), _g(x, "log", "-1", "--format=%T"),
                                len(_g(x, "log", "-1", "--format=%P").split()), _g(x, "log", "-1", "--format=%B"),
                                _g(x, "reflog", "-1", "--format=%gs"), _g(x, "status", "--porcelain"),
                                _g(x, "rev-list", "--count", "HEAD")]
                    self.assertEqual(end(repo), end(twin))
                finally:
                    self._restore(repo, pristine)

    def test_a_worktree_never_puts_hidden_content_under_a_visible_name(self):
        """N8: `worktree add wt <rev>` checks the commit out under wt/ - its hidden files at wt/config/secrets,
        which no root-anchored hidden rule names. Where the sandbox cannot read .git that is committed hidden
        content in the open; in K1 the real hidden file of the main checkout stays as it is."""
        for kind in ("k1", "hidgit", "unmounted"):
            with self.subTest(kind=kind):
                sit = self.situation(kind, "wta")
                repo = sit[2]
                hid = os.path.join(repo, HIDDEN, "db.properties")
                with open(hid, "rb") as f:
                    before = f.read()
                cards, r = self.run_git(sit, ["worktree", "add", "wt", "HEAD"])
                with open(hid, "rb") as f:
                    self.assertEqual(f.read(), before)
                if kind != "k1":
                    self.assertFalse(not cards and visible_leaks(repo, COMMITTED), (cards, r, visible_leaks(repo, COMMITTED)))

    def test_submodule_update_goes_on_a_card(self):
        for kind in ("k1", "hidgit"):
            sit = self.situation(kind, "smu")
            for argv in (["submodule", "update"], ["submodule", "update", "--init"], ["pull", "--recurse-submodules"],
                         ["checkout", "--recurse-submodules", "HEAD~1"]):
                with self.subTest(kind=kind, argv=argv):
                    cards, r = self.run_git(sit, argv)
                    self.assertTrue(cards, r)

    def test_a_ref_moved_between_the_check_and_the_run_never_rewrites_a_hidden_file(self):
        """N8 review: rewrites_hidden resolves `good` (visible changes only), then host git runs `merge good` by its
        name - with .git open (K1) the sandbox moves refs/heads/good to its evil commit in between (a loop of
        update-ref wins that race). The run must not leave the agent's content in the hidden file."""
        sit = self.situation("k1", "toc")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        self._evil(repo, "src/good.txt", "good\n", "good")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        real_run = gitops.session_run

        def racing(top, args, *a, **k):   # the sandbox moves the ref after the check, before the run
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        for argv in (["merge", "good"], ["checkout", "good"], ["switch", "good"], ["cherry-pick", "good"],
                     ["rebase", "good"], ["reset", "--keep", "good"], ["switch", "-c", "x5", "good"]):
            with self.subTest(argv=argv):
                try:
                    self._cover(sit[0], repo)   # N9: the net writes only inside the pinned over-mounted directory
                    with mock.patch.object(gitops, "session_run", racing):
                        cards, r = self.run_git(sit, argv)
                    with open(hid) as f:
                        self.assertFalse(f.read() != before and not cards, (argv, r))
                    self.assertFalse(os.path.islink(hid))
                finally:
                    self._restore(repo, pristine)

    def test_a_mv_or_rm_that_moves_nothing_needs_no_card(self):
        """N8 review of moves_hidden: a dry run (-n / --dry-run) moves nothing, `rm config` without -r is refused by
        git - neither may ask a card (a card that allows nothing, B-N6-4 class)."""
        sit = self.situation("k1", "mvn")
        repo = sit[2]
        pristine = self._pristine(repo)
        for argv in (["mv", "-n", "config", "c2"], ["mv", "--dry-run", "config", "c2"], ["rm", "config"],
                     ["rm", "-n", "-r", "-f", "config"], ["rm", "--dry-run", "-rf", "config"]):
            with self.subTest(argv=argv):
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual(cards, [], r)
                    self.assertEqual(visible_leaks(repo, WORKTREE), [])
                    self.assertTrue(os.path.exists(os.path.join(repo, HIDDEN, "db.properties")))
                finally:
                    self._restore(repo, pristine)
        hidden = lambda p: guard.hidden_by([HIDDEN], p)  # noqa: E731
        for argv in (["rm", "-r", "config"], ["rm", "-rf", "config"], ["rm", "-R", "config"], ["rm", "--recursive", "config"],
                     ["rm", "conf*"], ["rm", HIDDEN + "/db.properties"], ["mv", "config", "c2"]):
            self.assertTrue(gitops.moves_hidden(repo, argv, hidden), argv)   # still judged

    def test_a_stash_named_by_its_number_is_checked_too(self):
        """N8 review: `git stash apply 1` is stash@{1} - the check resolved "1" to no commit and let it run."""
        sit = self.situation("k1", "stn")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties", "README.md")
        with open(os.path.join(repo, "src", "a.txt"), "w") as f:
            f.write("zz\n")
        _g(repo, "stash", "-q")   # the fixture's stash (it changes the hidden file) is stash@{1} now
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        for argv in (["stash", "apply", "1"], ["stash", "pop", "1"], ["stash", "apply", "--index", "1"],
                     ["stash", "branch", "b1", "1"]):
            with self.subTest(argv=argv):
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertTrue(cards, r)
                    with open(hid) as f:
                        self.assertEqual(f.read(), before)
                finally:
                    self._restore(repo, pristine)

    def test_signoff_takes_no_value_in_cherry_pick_and_revert(self):
        """N8 fuzz: `revert -s main` reverted the commit that added a hidden file (deleted it) card-free - for
        cherry-pick / revert `-s` is --signoff, but the check took it for merge's `-s <strategy>` and skipped the
        target as its value."""
        sit = self.situation("k1", "sgn")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        hidden = lambda p: guard.hidden_by([HIDDEN], p)  # noqa: E731
        for argv in (["cherry-pick", "-s", "evil"], ["cherry-pick", "-x", "-s", "evil"], ["revert", "-s", "--no-edit", "HEAD"],
                     ["cherry-pick", "--signoff", "evil"]):
            with self.subTest(argv=argv):
                self.assertTrue(gitops.rewrites_hidden(repo, argv, hidden), argv)   # the check itself, not only the net
        pristine = self._pristine(repo)
        for argv in (["cherry-pick", "-s", "evil"], ["revert", "-s", "--no-edit", "HEAD"]):
            with self.subTest(argv=argv):
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertTrue(cards, r)
                    with open(hid) as f:
                        self.assertEqual(f.read(), before)
                finally:
                    self._restore(repo, pristine)

    def test_the_previous_branch_shorthand_is_checked_too(self):
        """N8: `-` is @{-1} for cherry-pick and rebase as for checkout / switch / merge."""
        sit = self.situation("k1", "dsh")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        for argv in (["cherry-pick", "-"], ["rebase", "-"], ["revert", "--no-edit", "-"]):
            with self.subTest(argv=argv):
                try:
                    _g(repo, "checkout", "-q", "evil")
                    _g(repo, "checkout", "-q", "main")
                    cards, r = self.run_git(sit, argv)
                    with open(hid) as f:
                        self.assertFalse(f.read() != before and not cards, (argv, r))
                finally:
                    self._restore(repo, pristine)


    # ------------------------------------------------------------------ N9: review of the N8 pull emulation and the net
    def _cover(self, s, repo, over=HIDDEN):
        """the session's coverage as the preflight pins it: `over` over-mounted, its host identity recorded"""
        s.coverage = [{"root": repo, "overmounts": [over], "ids": guard.overmount_ids(repo, [over])}]

    def _good(self, repo):
        self._evil(repo, "src/good.txt", "good\n", "good")   # a branch that changes only a visible file

    def test_the_net_never_puts_hidden_content_where_the_sandbox_sees_it(self):
        """N9 review of the F-N8-2 net: it put the snapshot back BY NAME. (a) The sandbox moves config/ away during
        the run and creates a plain config/secrets (the over-mount travels with the moved directory, verified on
        Docker Desktop): the net wrote the real hidden files into the plain directory - the sandbox reads them.
        (b) A file over-mount: host git replaced the file (unlink + create: the over-mount is gone) - the net wrote
        the real content into the new inode. Only inside the pinned, intact over-mounted directory may it write."""
        sit = self.situation("k1", "nsw")
        s, _x, repo = sit
        self._good(repo)
        # an evil commit that adds the ignored hidden file (git overwrites ignored files) - the ref race of F-N8-2
        self._evil(repo, HIDDEN + "/local.properties", "agent=1\n", "evil")
        pristine = self._pristine(repo)
        real_run = gitops.session_run

        def swap(top, args, *a, **k):   # the sandbox: mv config config_old && mkdir -p config/secrets
            os.rename(os.path.join(repo, "config"), os.path.join(repo, "config_old"))
            os.makedirs(os.path.join(repo, HIDDEN))
            return real_run(top, args, *a, **k)

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        for name, over, runner, argv in (("dir moved away", HIDDEN, swap, ["merge", "good"]),
                                         ("dir moved away", HIDDEN, swap, ["checkout", "good"]),
                                         ("file over-mount replaced", HIDDEN + "/local.properties", race, ["merge", "good"])):
            with self.subTest(name=name, argv=argv):
                try:
                    self._cover(s, repo, over)
                    pinned = s.coverage[0]["ids"][over]
                    with mock.patch.object(gitops, "session_run", runner):
                        cards, r = self.run_git(sit, argv)
                    for rel in ("db.properties", "local.properties") if over == HIDDEN else (os.path.basename(over),):
                        p = os.path.join(repo, HIDDEN, rel)
                        if not os.path.isfile(p):
                            continue
                        st = os.lstat(os.path.join(repo, over))
                        intact = [st.st_dev, st.st_ino] == pinned and os.path.isdir(os.path.join(repo, over))
                        with open(p, "rb") as f:
                            data = f.read()
                        self.assertFalse(any(w.encode() in data for w in WORKTREE) and not intact, (name, rel, r))
                finally:
                    self._restore(repo, pristine)

    def test_the_net_still_puts_back_inside_an_intact_over_mounted_directory(self):
        """N9: the control of the test above - the ref race of F-N8-2 with config/secrets over-mounted and
        intact: the hidden files are put back (the agent gets the note) and the original survives in Piper's trash
        only if the net replaced something (here: the agent's content)."""
        sit = self.situation("k1", "nok")
        s, _x, repo = sit
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._good(repo)
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        self._cover(s, repo)
        with mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["merge", "good"])
        self.assertEqual(cards, [], r)
        with open(hid) as f:
            self.assertEqual(f.read(), before)
        self.assertIn("put back as it was", r["result"]["output"])

    def test_the_net_cannot_be_switched_off_by_planting_hidden_names(self):
        """N9 review: more than 400 hidden entries switched the net off silently - the index entries alone count
        (`update-index --add --cacheinfo` needs no file; K1 writes .git), then the F-N8-2 ref race lands. A net
        that cannot be built asks first; names without a file do not count."""
        sit = self.situation("k1", "nof")
        s, _x, repo = sit
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        blob = _g(repo, "hash-object", "-w", "README.md")
        info = "".join("100644 %s\t%s/x%d.properties\n" % (blob, HIDDEN, i) for i in range(450))
        subprocess.run(["git", "-C", repo, "update-index", "--index-info"], input=info, text=True, env=ENV, check=True)
        _g(repo, "commit", "-qm", "planted names")
        subprocess.run(["git", "-C", repo, "update-index", "--skip-worktree", "--stdin"], text=True, env=ENV, check=True,
                       input="".join("%s/x%d.properties\n" % (HIDDEN, i) for i in range(450)))   # no file: not dirty
        self._good(repo)
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        self._cover(s, repo)
        with mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["merge", "good"])
        with open(hid) as f:
            self.assertFalse(f.read() != before and not cards, r)

    def test_a_net_too_big_to_keep_asks_first(self):
        """N9: more than 400 real hidden files (or 32 MiB): no silent run without the net - a card."""
        sit = self.situation("k1", "nbg")
        s, _x, repo = sit
        self._good(repo)
        for i in range(420):
            with open(os.path.join(repo, HIDDEN, "x%d.properties" % i), "w") as f:
                f.write("v\n")
        with open(os.path.join(repo, ".gitignore"), "a") as f:
            f.write(HIDDEN + "/x*.properties\n")
        _g(repo, "add", ".gitignore")
        _g(repo, "commit", "-qm", "ignore")
        _g(repo, "branch", "-f", "good", _g(repo, "commit-tree", _g(repo, "rev-parse", "good^{tree}"), "-p", "HEAD", "-m", "g"))
        self._cover(s, repo)
        asked = []
        with mock.patch.object(self.m, "ask", lambda kind, sess, summary, payload, **k: asked.append(summary) or False):
            r = self._tool(s, {"args": ["merge", "good"]})
        self.assertEqual(len(asked), 1, r)
        self.assertIn("cannot keep the hidden files", asked[0])
        self.assertIn("declined", r.get("error", ""))

    def test_a_merge_the_config_makes_real_takes_no_hidden_file_card_free(self):
        """N9 review: `merge good` (a fast-forward) was judged "hashes nothing" - but merge.ff=false or
        branch.<b>.mergeOptions=--no-ff|--squash|--autostash (K1: the sandbox writes .git/config) make it a real
        merge: save_state() runs `stash create` and the REAL dirty hidden file lands in .git, where the sandbox
        reads it. The pull path inherits it (its step is `merge <oid>`)."""
        sit = self.situation("k1", "mff")
        s, _x, repo = sit
        self._server(repo)
        self._pushed(repo, "src/good.txt", "good\n", "good")   # origin/main after a fetch: a fast-forward
        self._good(repo)
        hid = os.path.join(repo, HIDDEN, "db.properties")
        w4 = _g(repo, "hash-object", HIDDEN + "/db.properties")   # the dirty real content (PLANTED_W4)
        pristine = self._pristine(repo)
        for key, val in (("merge.ff", "false"), ("branch.main.mergeOptions", "--no-ff"),
                         ("branch.main.mergeOptions", "--squash"), ("branch.main.mergeOptions", "--autostash")):
            for argv in (["merge", "good"], ["pull"]):
                with self.subTest(key=key, val=val, argv=argv):
                    try:
                        _g(repo, "config", key, val)
                        cards, r = self.run_git(sit, argv)
                        inside = subprocess.run(["git", "-C", repo, "cat-file", "-e", w4], env=ENV).returncode == 0
                        self.assertFalse(inside and not cards, (r, cards))
                        with open(hid) as f:
                            self.assertIn("PLANTED_W4", f.read())
                    finally:
                        self._restore(repo, pristine)

    def test_the_checks_read_the_config_the_run_reads(self):
        """N9 review: the checks read the repository config only (GIT_CONFIG_GLOBAL=/dev/null), the run reads the
        user's ~/.gitconfig too - a global merge.autoStash / rebase.autoStash / merge.ff=false stashed (hashed) the
        dirty hidden file card-free."""
        sit = self.situation("k1", "gcf")
        s, _x, repo = sit
        self._good(repo)
        w4 = _g(repo, "hash-object", HIDDEN + "/db.properties")
        pristine = self._pristine(repo)
        gc = os.path.join(_env.ROOT, "n9-gitconfig")
        self.addCleanup(lambda: os.path.exists(gc) and os.unlink(gc))
        for cfg, argv in (("[merge]\n\tautoStash = true\n", ["merge", "good"]), ("[merge]\n\tff = false\n", ["merge", "good"]),
                          ("[rebase]\n\tautoStash = true\n", ["rebase", "good"])):
            with self.subTest(cfg=cfg, argv=argv):
                try:
                    with open(gc, "w") as f:
                        f.write(cfg)
                    with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": gc}):
                        cards, r = self.run_git(sit, argv)
                    inside = subprocess.run(["git", "-C", repo, "cat-file", "-e", w4], env=ENV).returncode == 0
                    self.assertFalse(inside and not cards, (r, cards))
                finally:
                    self._restore(repo, pristine)

    def test_a_card_free_pull_looks_like_pull_in_more_situations(self):
        """N9: the parity test of N8 extended - a conflicting merge / rebase (the state left behind: MERGE_HEAD,
        MERGE_MSG, rebase-merge/), unrelated histories, --all over two remotes, merge.ff=false, the user's global
        pull.rebase, and the forms that ask (each goes on a card before anything is fetched)."""
        sit = self.situation("k1", "pl2")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties", "README.md")
        srv = self._server(repo)
        self._pushed(repo, "README.md", "server\n", "srvreadme")   # the server's main changes README
        # an unrelated branch on the server
        blob = subprocess.run(["git", "-C", repo, "hash-object", "-w", "--stdin"], input="u\n", capture_output=True,
                              text=True, env=ENV, check=True).stdout.strip()
        tree = subprocess.run(["git", "-C", repo, "mktree"], input="100644 blob %s\tunrelated.txt\n" % blob,
                              capture_output=True, text=True, env=ENV, check=True).stdout.strip()
        _g(repo, "push", "-q", srv, "%s:refs/heads/other" % _g(repo, "commit-tree", tree, "-m", "root"))
        _g(srv, "update-server-info")
        # a second remote with the same main
        srv2 = repo + ".srv2.git"
        shutil.rmtree(srv2, ignore_errors=True)
        self.addCleanup(shutil.rmtree, srv2, True)
        _g(repo, "clone", "-q", "--bare", srv, srv2)
        _g(srv2, "update-server-info")
        url = _g(repo, "remote", "get-url", "origin")
        _g(repo, "remote", "add", "second", url.rsplit("/", 1)[0] + "/" + os.path.basename(srv2))
        pristine = self._pristine(repo)
        twin = repo + ".twin"
        self.addCleanup(shutil.rmtree, twin, True)
        gc = os.path.join(_env.ROOT, "n9-gitconfig-pull")
        self.addCleanup(lambda: os.path.exists(gc) and os.unlink(gc))

        def conflict(r):
            with open(os.path.join(r, "README.md"), "w") as f:
                f.write("local\n")
            _g(r, "commit", "-qam", "local")

        def cfg(k, v):
            return lambda r: _g(r, "config", k, v)

        def glob_cfg(text):
            def prep(r):
                conflict_free(r)
                with open(gc, "w") as f:
                    f.write(text)
            return prep

        def conflict_free(r):
            with open(os.path.join(r, "src", "a.txt"), "w") as f:
                f.write("mine\n")
            _g(r, "commit", "-qam", "mine")

        scenarios = [("conflict merge", conflict, ["pull", "--no-rebase"], None),
                     ("conflict merge log", conflict, ["pull", "--no-rebase", "--log"], None),
                     ("conflict rebase", conflict, ["pull", "--rebase"], None),
                     ("unrelated", None, ["pull", "--no-rebase", "--allow-unrelated-histories", "origin", "other"], None),
                     ("unrelated refused", None, ["pull", "--no-rebase", "origin", "other"], None),
                     ("all two remotes", None, ["pull", "--all"], None),
                     ("merge.ff=false ff", cfg("merge.ff", "false"), ["pull"], None),
                     ("merge.ff=false diverged", lambda r: (conflict_free(r), cfg("merge.ff", "false")(r)), ["pull", "--no-rebase"], None),
                     ("global pull.rebase", glob_cfg("[pull]\n\trebase = true\n"), ["pull"], gc),
                     ("global pull.ff only", glob_cfg("[pull]\n\tff = only\n"), ["pull"], gc),
                     ("tags", None, ["pull", "--tags"], None)]
        for name, prep, argv, glob in scenarios:
            with self.subTest(name=name, argv=argv):
                try:
                    if prep:
                        prep(repo)
                    shutil.rmtree(twin, ignore_errors=True)
                    shutil.copytree(repo, twin, symlinks=True)
                    real = subprocess.run(["git", "-C", twin, *argv], capture_output=True,
                                          env=dict(ENV, LC_ALL="C", **({"GIT_CONFIG_GLOBAL": glob} if glob else {})))
                    ident = {k: v for k, v in ENV.items() if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
                    with mock.patch.dict(os.environ, dict(ident, GIT_CONFIG_GLOBAL=glob or "/dev/null")):
                        cards, r = self.run_git(sit, argv)
                    self.assertEqual(cards, [], r)
                    res = r.get("result") or {}
                    self.assertEqual(res.get("rc"), real.returncode, (r, real))
                    if SAME_WORDING:
                        self.assertEqual(res.get("output"), (real.stdout + real.stderr).decode().replace(twin, repo), r)

                    def end(x):
                        def rd(rel):
                            p = os.path.join(x, ".git", rel)
                            if not os.path.isfile(p):
                                return None
                            with open(p) as f:
                                return f.read().replace(x, "<repo>")
                        return [_g(x, "log", "-1", "--format=%T"), len(_g(x, "log", "-1", "--format=%P").split()),
                                _g(x, "log", "-1", "--format=%B"), _g(x, "reflog", "-1", "--format=%gs"),
                                _g(x, "status", "--porcelain"), _g(x, "rev-list", "--count", "HEAD"),
                                rd("MERGE_HEAD"), rd("MERGE_MSG"), rd("MERGE_MODE"),
                                rd("rebase-merge/head-name"), rd("rebase-merge/onto"), rd("rebase-merge/orig-head"),
                                rd("rebase-merge/autostash"), sorted(os.listdir(os.path.join(x, ".git", "rebase-merge")))
                                if os.path.isdir(os.path.join(x, ".git", "rebase-merge")) else None]
                    self.assertEqual(end(repo), end(twin))
                finally:
                    self._restore(repo, pristine)
        for argv in (["pull", "--rebase=interactive"], ["pull", "-i"], ["pull", "--edit"], ["pull", "-e"],
                     ["pull", "--verify-signatures"], ["pull", "origin", "main", "other"], ["pull", "origin", "main:x"],
                     ["pull", "origin", "+main"], ["pull", "--recurse-submodules"]):
            with self.subTest(asks=argv):   # forms the emulation does not model: a card before the fetch
                try:
                    before = _g(repo, "rev-parse", "refs/remotes/origin/main")
                    cards, r = self.run_git(sit, argv)
                    self.assertTrue(cards, r)
                    self.assertEqual(_g(repo, "rev-parse", "refs/remotes/origin/main"), before)
                finally:
                    self._restore(repo, pristine)

    def test_a_tree_as_the_source_goes_on_a_card(self):
        """N9 fuzz (seed set 552060...): `restore --source=HEAD:config config` changed the hidden work-tree file
        card-free - the check resolved the source as a commit only (a tree names none: "git refuses"), but restore
        and checkout take any tree-ish; its paths are not the work tree's."""
        sit = self.situation("k1", "trs")
        repo = sit[2]
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        tree = oid(repo, "HEAD:config")
        for argv in (["restore", "--source=HEAD:config", "config"], ["restore", "-s", "HEAD:config", "config"],
                     ["restore", "--source=" + tree, "config"], ["checkout", "HEAD:config", "--", "config"]):
            with self.subTest(argv=argv):
                try:
                    cards, r = self.run_git(sit, argv)
                    with open(hid) as f:
                        self.assertFalse(f.read() != before and not cards, (argv, r))
                    self.assertTrue(cards, (argv, r))
                finally:
                    self._restore(repo, pristine)
        with self.subTest(control=True):   # a commit source of visible paths stays card-free
            cards, r = self.run_git(sit, ["restore", "--source=HEAD", "src"])
            self.assertEqual(cards, [], r)

    def test_a_message_search_or_a_tree_before_the_paths_goes_on_a_card(self):
        """N10 review of _tree_only: `:/<text>` names the youngest commit whose message matches - the check appended
        ^{commit} to the string (`:/innocent^{commit}` searches for that text: no commit, "git refuses") and
        checkout / switch / merge / cherry-pick of the agent's commit rewrote the hidden file card-free; a tree
        source without `--` (`checkout evil: config`) skipped the N9 tree check."""
        sit = self.situation("k1", "msg")
        repo = sit[2]
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")   # "innocent"
        _g(repo, "branch", "vis", _g(repo, "commit-tree", oid(repo, "HEAD^{tree}"), "-p", "HEAD", "-m", "visible only"))
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            before = f.read()
        pristine = self._pristine(repo)
        for argv in (["checkout", ":/innocent"], ["switch", "--detach", ":/innocent"], ["merge", ":/innocent"],
                     ["cherry-pick", ":/innocent"], ["checkout", "evil:", "config"], ["checkout", "evil^{tree}", "config"],
                     ["checkout", ":/innocent", "config"], ["restore", "--source=:/innocent", "config"]):
            with self.subTest(argv=argv):
                try:
                    cards, r = self.run_git(sit, argv)
                    with open(hid) as f:
                        self.assertEqual(f.read(), before, (argv, r))
                    self.assertTrue(cards, (argv, r))
                finally:
                    self._restore(repo, pristine)
        for argv in (["merge", ":/visible only"], ["checkout", ":/nosuchmessage"]):
            with self.subTest(control=argv):   # a commit of visible files only / no such commit: no card
                try:
                    cards, r = self.run_git(sit, argv)
                    self.assertEqual(cards, [], (argv, r))
                finally:
                    self._restore(repo, pristine)

    def test_a_net_that_could_not_put_back_is_a_queue_row(self):
        """N10 (N9 decision 1): the net's fail-closed branch (no pinned, intact over-mounted directory: a file
        exclude, a session without identities) leaves the run's content in the hidden file and moves the original to
        Piper's trash - the human learnt it from audit.log only. Now a blocked queue row names the files and the
        trash entries; it stays (no 30-min expiry) until dismissed, with the Feed off too (B-N9-1)."""
        import feed
        sit = self.situation("k1", "nqr")
        s, _x, repo = sit
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        self._good(repo)
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        off = dict(feed.load_config(), enabled=False)
        with mock.patch.object(feed, "load_config", lambda: off), mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["merge", "good"])   # s.coverage = []: nothing pinned, the net cannot write
            self.assertEqual(cards, [], r)
            self.assertIn("could not be put back", r["result"]["output"])
            rows = [a for a in self.m.queue_summary()["actions"] if a["key"].startswith("net-failed:%s:" % s.id)]
            self.assertEqual(len(rows), 1, self.m.queue_summary())
            row = rows[0]
            self.assertEqual((row["level"], row["kind"]), ("blocked", "action"))
            self.assertIn(HIDDEN + "/db.properties", row["body"])
            self.assertIn("git merge good", row["body"])
            trash = os.path.join(common.PIPER_HOME, "trash")
            ids = [t for t in os.listdir(trash) if t in row["body"]]
            self.assertTrue(ids, row["body"])   # the trash entries it names exist
            with mock.patch.object(time, "time", lambda: row["since"] + 7200):   # past ALLOWED_FAILED_S: still there
                self.assertIn(row["key"], [a["key"] for a in self.m.queue_summary()["actions"]])
            # through the HTTP API the GUI reads: /api/state `queue`, POST /api/feed/dismiss
            import piperd
            from test_piperd import get, post
            old = piperd.MANAGER
            piperd.MANAGER = self.m
            self.addCleanup(setattr, piperd, "MANAGER", old)
            t = common.mint_token("n10-net")
            code, _, body = get("/api/state", token=t)
            self.assertEqual(code, 200)
            self.assertIn(row["key"], [a["key"] for a in json.loads(body)["queue"]["actions"]])
            self.assertEqual(post("/api/feed/dismiss", {"key": row["key"]}, token=t)[0], 200)
            code, _, body = get("/api/state", token=t)
            self.assertNotIn(row["key"], [a["key"] for a in json.loads(body)["queue"]["actions"]])

    def test_the_net_row_restores_the_original_from_the_trash(self):
        """2026-10-10 (the user's decision): the net-failed row carries the trash entries of the originals it could
        not put back ([{id, path}], never content) and its Restore writes the original OVER the run's content
        (fs_restore replace, GUI token only), even without sandbox coverage; once every
        failed file is back the row goes."""
        import feed
        import piperd
        from test_piperd import get, post
        sit = self.situation("k1", "nrs")
        s, _x, repo = sit
        key = "team-x/n6k1nrs"
        _env.register(key, repo)
        s.project = key
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        hid = os.path.join(repo, HIDDEN, "db.properties")
        with open(hid) as f:
            original = f.read()
        self._good(repo)
        self._evil(repo, HIDDEN + "/db.properties", "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        off = dict(feed.load_config(), enabled=False)
        old = piperd.MANAGER
        piperd.MANAGER = self.m
        self.addCleanup(setattr, piperd, "MANAGER", old)
        with mock.patch.object(feed, "load_config", lambda: off), mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["merge", "good"])
            self.assertEqual(cards, [], r)
            with open(hid) as f:
                self.assertIn("attacker", f.read())   # the run's content stayed (no pinned directory)
            rows = [a for a in self.m.queue_summary()["actions"] if a["key"].startswith("net-failed:%s:" % s.id)]
            self.assertEqual(len(rows), 1, self.m.queue_summary())
            row = rows[0]
            self.assertEqual(row["project"], key)
            self.assertEqual([e["path"] for e in row["trash"]], [HIDDEN + "/db.properties"])
            self.assertEqual(set(row["trash"][0]), {"id", "path"})   # ids and paths, never content
            tid = row["trash"][0]["id"]
            self.assertIn(tid, row["body"])
            with open(os.path.join(common.PIPER_HOME, "trash", tid, "item")) as f:
                self.assertEqual(f.read(), original)   # the entry is the original, not what the run left
            t = common.mint_token("n12-net")
            # The sandbox still runs without identities; this does not block a manual GUI restore.
            # Without replace, the occupied path is still refused.
            code, _, body = post("/api/fs/restore", {"project": key, "trash": tid}, token=t)
            self.assertNotEqual(code, 200, body)
            self.assertIn("exists again", json.loads(body)["error"])
            code, _, body = post("/api/fs/restore", {"project": key, "trash": tid, "replace": True}, token=t)
            self.assertEqual(code, 200, body)
            self.assertEqual(json.loads(body), {"path": HIDDEN + "/db.properties", "replaced": True})
            with open(hid) as f:
                self.assertEqual(f.read(), original)
            self.assertFalse(os.path.exists(os.path.join(common.PIPER_HOME, "trash", tid)))
            ev = common.audit_tail(1)[0]
            self.assertEqual((ev["event"], ev["path"], ev["trash"], ev["replace"], ev["hidden"]),
                             ("gui-restore", HIDDEN + "/db.properties", tid, True, True))
            code, _, body = get("/api/state", token=t)
            self.assertNotIn(row["key"], [a["key"] for a in json.loads(body)["queue"]["actions"]])   # the row went
            # the entry is gone now: refused
            code, _, body = post("/api/fs/restore", {"project": key, "trash": tid, "replace": True}, token=t)
            self.assertEqual(code, 404, body)

    def test_the_net_row_restores_into_the_sibling_worktree_the_run_changed(self):
        """2026-10-10 (N10 minor): `git_run -C ../<repo>-work merge` - the net's trash entry was relative to that
        worktree but restored relative to the project: Restore wrote the original into the MAIN checkout and left the
        run's content in the worktree. The entry records the tree; Restore goes there, only while it is a trusted
        work tree of the same repository."""
        import feed
        sit = self.situation("k1", "nwt")
        s, _x, repo = sit
        key = "team-x/n6k1nwt"
        _env.register(key, repo)
        s.project = key
        _g(repo, "checkout", "-q", "--", HIDDEN + "/db.properties")
        sib = repo + "-work"
        self.addCleanup(shutil.rmtree, sib, True)
        _g(repo, "worktree", "add", "-q", "-b", "wt", sib, "HEAD")
        rel = HIDDEN + "/db.properties"
        with open(os.path.join(repo, rel)) as f:
            main_before = f.read()
        with open(os.path.join(sib, rel)) as f:
            original = f.read()
        self._good(repo)
        self._evil(repo, rel, "db.url=jdbc:mysql://attacker.example/x\n", "evil")
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        off = dict(feed.load_config(), enabled=False)
        with mock.patch.object(feed, "load_config", lambda: off), mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["-C", "../" + os.path.basename(sib), "merge", "good"])
            self.assertEqual(cards, [], r)
            with open(os.path.join(sib, rel)) as f:
                self.assertIn("attacker", f.read(), r)   # the run changed the worktree, the net could not put it back
            rows = [a for a in self.m.queue_summary()["actions"] if a["key"].startswith("net-failed:%s:" % s.id)]
            self.assertEqual(len(rows), 1, self.m.queue_summary())
            tid = rows[0]["trash"][0]["id"]
            s.state = "exited"
            meta_file = os.path.join(common.PIPER_HOME, "trash", tid, "meta.json")
            meta = common.read_json(meta_file, {})
            # an entry naming a tree that is not a trusted work tree of this repository: refused, kept
            other = os.path.realpath(build("n6k1nwtother"))
            common.write_json(meta_file, dict(meta, root=other))
            with self.assertRaisesRegex(ValueError, "not a trusted work tree"):
                self.m.fs_restore(key, tid, replace=True)
            common.write_json(meta_file, meta)
            self.assertEqual(self.m.fs_restore(key, tid, replace=True), {"path": rel, "replaced": True})
        with open(os.path.join(sib, rel)) as f:
            self.assertEqual(f.read(), original)          # back where the run changed it
        with open(os.path.join(repo, rel)) as f:
            self.assertEqual(f.read(), main_before)       # the main checkout untouched
        self.assertEqual(common.audit_tail(1)[0].get("tree"), os.path.realpath(sib))
        self.assertFalse([a for a in self.m.queue_summary()["actions"] if a["key"].startswith("net-failed:%s:" % s.id)])

    def test_a_hidden_file_ignored_by_the_global_excludes_is_kept_too(self):
        """N10 review of F-N9-4: the net listed the ignored hidden files without the user's global config, the run
        reads it - a hidden file ignored only by the global core.excludesFile was not in the snapshot; in the F-N8-2
        ref race git overwrote it and the net took it for a file the run created: the real original was gone (not
        put back, not in Piper's trash)."""
        sit = self.situation("k1", "gex")
        s, _x, repo = sit
        with open(os.path.join(repo, ".gitignore"), "w") as f:
            f.write("")   # the repository ignores nothing ...
        _g(repo, "commit", "-qam", "no repository ignore")
        gi, gc = os.path.join(_env.ROOT, "n10-global-ignore"), os.path.join(_env.ROOT, "n10-gitconfig")
        self.addCleanup(lambda: [os.unlink(p) for p in (gi, gc) if os.path.exists(p)])
        with open(gi, "w") as f:
            f.write("local.properties\n")   # ... the user's global excludes file does
        with open(gc, "w") as f:
            f.write("[core]\n\texcludesFile = %s\n" % gi)
        self._good(repo)
        self._evil(repo, HIDDEN + "/local.properties", "agent=1\n", "evil")
        real_run = gitops.session_run

        def race(top, args, *a, **k):
            _g(repo, "update-ref", "refs/heads/good", oid(repo, "evil"))
            return real_run(top, args, *a, **k)
        loc = os.path.join(repo, HIDDEN, "local.properties")
        trash = os.path.join(common.PIPER_HOME, "trash")
        had = set(os.listdir(trash)) if os.path.isdir(trash) else set()
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": gc}), mock.patch.object(gitops, "session_run", race):
            cards, r = self.run_git(sit, ["merge", "good"])
        kept = []
        for t in (set(os.listdir(trash)) if os.path.isdir(trash) else set()) - had:
            with open(os.path.join(trash, t, "item"), "rb") as f:
                kept.append(f.read())
        with open(loc, "rb") as f:
            now = f.read()
        self.assertTrue(b"PLANTED_U5" in now or any(b"PLANTED_U5" in k for k in kept), (r, now, kept))

class HiddenCheckPerfTest(unittest.TestCase):
    """N8: the hidden-file checks run before every card-free git_run - on a big repository (10k files, a hidden
    config, an ignored build dir, a 200-commit side branch) each stays under a second."""

    def test_each_check_stays_under_a_second_on_a_big_repository(self):
        repo = os.path.realpath(_env.make_project("n8perf", {HIDDEN + "/db.properties": "pw=1\n",
                                                             HIDDEN + "/local.properties": "l\n",
                                                             ".gitignore": "target/\n" + HIDDEN + "/local.properties\n"}))
        for d in range(100):
            os.makedirs(os.path.join(repo, "src", "m%d" % d))
            for n in range(100):
                with open(os.path.join(repo, "src", "m%d" % d, "f%d.txt" % n), "w") as f:
                    f.write("x%d\n" % n)
        os.makedirs(os.path.join(repo, "target"))
        for n in range(2000):
            with open(os.path.join(repo, "target", "o%d" % n), "w") as f:
                f.write("o")
        _g(repo, "init", "-q", "-b", "main")
        _g(repo, "add", "-A")
        _g(repo, "commit", "-q", "-m", "init")
        stream = "".join("commit refs/heads/side\ncommitter T <t@e> %d +0000\ndata 3\nc%d\n%sM 100644 inline src/m1/f1.txt\n"
                         "data %d\nv%d\n\n" % (1700000000 + i, i % 10, "from refs/heads/main^0\n" if i == 0 else "",
                                               len("v%d\n" % i) - 1, i) for i in range(200))
        subprocess.run(["git", "-C", repo, "fast-import", "--quiet"], input=stream, text=True, check=True, env=ENV)
        hidden = lambda p: guard.hidden_by([HIDDEN], p)  # noqa: E731
        for fn, argv in ((gitops.moves_hidden, ["mv", "src", "src2"]), (gitops.moves_hidden, ["rm", "-rf", "."]),
                         (gitops.moves_hidden, ["rm", "-r", "src/*"]), (gitops.rewrites_hidden, ["merge", "side"]),
                         (gitops.rewrites_hidden, ["checkout", "side"]), (gitops.rewrites_hidden, ["cherry-pick", "main..side"]),
                         (gitops.rewrites_hidden, ["revert", "main..side"]), (gitops.rewrites_hidden, ["rebase", "side"]),
                         (gitops.rewrites_hidden, ["switch", "--orphan", "o"]), (gitops.rewrites_hidden, ["restore", "-s", "side", "."]),
                         (gitops.stages_hidden, ["add", "-A"]), (gitops.stages_hidden, ["commit", "-am", "x"])):
            with self.subTest(fn=fn.__name__, argv=argv):
                t0 = time.time()
                fn(repo, argv, hidden)
                self.assertLess(time.time() - t0, 1.0)
        t0 = time.time()
        snap = gitops.hidden_snapshot(repo, hidden)
        self.assertEqual(gitops.hidden_restore(repo, snap, hidden), ([], [], {}))
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual(sorted(snap), [HIDDEN + "/db.properties", HIDDEN + "/local.properties"])


class AgentFileToolVariantTest(unittest.TestCase):
    """N7: the agent's other file tools (read_file / list_dir / grep of another project, http_run save_to, db_script
    path) at their choke points (projectfs._resolve, httpclient._save_target + common.safe_write, dbtools
    _db_statements + common.safe_read) - the hidden file by every other name: a case variant, a symlink (also in
    another case), ./ // detours, a trailing slash. Nothing PLANTED comes back, nothing hidden is written."""
    VARIANTS = ["config/secrets/db.properties"] + CASE_VARIANTS + [
        "lnkdir/secrets/db.properties", "LNKDIR/SECRETS/DB.PROPERTIES", "config/secrets/db.properties/",
        "config/./secrets//db.properties", "lnkdir", "LNKDIR", "lnkdir/secrets", "config", "CONFIG/SECRETS",
        "config/secrets", "config/secrets/local.properties", "Config/Secrets/Local.Properties"]

    @classmethod
    def setUpClass(cls):
        cls.repo = build("n7filetools")
        cls.hidden = staticmethod(lambda p: guard.hidden_by([HIDDEN], p))

    def _no_planted(self, fn, *a, **k):
        try:
            out = fn(*a, **k)
        except Exception as e:   # noqa: BLE001 - a refusal is the expected answer for most variants
            out = {"error": str(e)}
        self.assertFalse(any(w in json.dumps(out) for w in WORKTREE + COMMITTED), (fn.__name__, a, out))
        return out

    def test_project_reads_never_return_a_hidden_file_by_another_name(self):
        h, repo = self.hidden, self.repo
        for v in self.VARIANTS:
            with self.subTest(v=v):
                self._no_planted(projectfs.read_file, repo, v, h)
                out = self._no_planted(projectfs.list_dir, repo, v, h)
                self.assertFalse([e for e in out.get("entries", []) if e["name"] in ("db.properties", "local.properties")],
                                 (v, out))
                self._no_planted(projectfs.grep, repo, "PLANTED", h, rel=v)
                self._no_planted(projectfs.grep, repo, "pw", h, rel=v, ignore_case=True, all=True)
                try:
                    self._no_planted(projectfs.git_grep, repo, repo, "PLANTED", h, rel=v)
                except projectfs.GrepUnavailable:
                    pass
        out = self._no_planted(projectfs.files, repo, h, all=True)
        self.assertFalse([f for f in out["files"] if "secrets" in f.lower()], out)
        self._no_planted(projectfs.grep, repo, "PLANTED", h, all=True)

    def test_save_to_never_writes_a_hidden_file_by_another_name(self):
        repo = self.repo
        before = {f: open(os.path.join(repo, HIDDEN, f), "rb").read() for f in ("db.properties", "local.properties")}
        runner = object.__new__(httpclient.HttpRunner)
        ctx = {"root": repo, "top": repo, "hidden": self.hidden}
        for v in self.VARIANTS + ["development/../config/secrets/db.properties", "LNK", "lnk"]:
            with self.subTest(v=v):
                try:
                    rel = runner._save_target(ctx, v)
                    common.safe_write(os.path.realpath(repo), rel, b"OVERWRITTEN")
                except (httpclient.HttpError, OSError, ValueError):
                    pass
                after = {f: open(os.path.join(repo, HIDDEN, f), "rb").read() for f in ("db.properties", "local.properties")}
                self.assertEqual(after, before, v)

    def test_db_script_never_reads_a_hidden_sql_file_by_another_name(self):
        repo = self.repo
        with open(os.path.join(repo, HIDDEN, "seed.sql"), "w") as f:
            f.write("select 'PLANTED_W4';\n")
        os.symlink(HIDDEN + "/seed.sql", os.path.join(repo, "seed.sql"))
        self.addCleanup(os.unlink, os.path.join(repo, "seed.sql"))
        self.addCleanup(os.unlink, os.path.join(repo, HIDDEN, "seed.sql"))
        m = object.__new__(sessions.Manager)
        for v in ("config/secrets/seed.sql", "CONFIG/secrets/seed.sql", "config//secrets/./seed.sql",
                  "lnkdir/secrets/seed.sql", "LNKDIR/secrets/seed.sql", "seed.sql", "SEED.SQL", "/config/secrets/seed.sql"):
            with self.subTest(v=v):
                self._no_planted(m._db_statements, {"path": v}, "mysql", repo, self.hidden)

if __name__ == "__main__":
    unittest.main()
