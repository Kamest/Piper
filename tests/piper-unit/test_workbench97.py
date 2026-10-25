"""9.7 workbench, Manager side (workbench.py): marks, write lock, raw images, git history (files
hidden from Claude shown to the GUI, 2026-10-07), turn snapshots that never touch the index/HEAD/refs, CI + MR through a
fake GitLab, the stack through a fake provider, usage per day (piperd routes: test_piperd)."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import _env
import common
import gitlab
import gitops
import guard
import projectfs
import sessions

GENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=GENV).stdout.decode()


def write(root, rel, data, mode="w"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as f:
        f.write(data)


class FakeResp(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class FakeHttp:
    """urllib opener: routes[(method, path)] -> body (dict/list: JSON) or callable(req) -> body."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        self.calls.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query)),
                           json.loads(req.data) if req.data else None, dict(req.header_items())))
        r = self.routes.get((req.get_method(), u.path))
        if r is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b'{"error": "nope"}'))
        if callable(r):
            r = r(req)
        if isinstance(r, Exception):
            raise r
        return FakeResp(json.dumps(r).encode() if not isinstance(r, bytes) else r)


def wait(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Workbench97Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        cls.m = _env.manager(cls)
        cls.root = root = os.path.realpath(_env.make_project("wb97", {
            "src/A.java": "class A {\n  int a;\n  int b;\n  int c;\n}\n",
            "config/secrets/db.properties": "db.password=old\n",
            "notes.txt": "private notes\n",
            ".hole/settings.json": '{"files": {"exclude": ["notes.txt"]}}',
            "README.md": "# Readme\n", "img/icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>"}))
        write(root, "img/logo.png", PNG, "wb")
        write(root, "config/secrets/qr.png", PNG, "wb")
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Tester")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        write(root, "src/A.java", "class A {\n  int a;\n  int bb;\n  int c;\n}\n")
        write(root, "config/secrets/db.properties", "db.password=SECRET2\n")
        write(root, "notes.txt", "private notes TOPSECRET\n")
        git(root, "commit", "-q", "-am", "change A and the secrets")
        cls.c2 = git(root, "rev-parse", "HEAD").strip()
        cls.key = "team-x/wb97"
        _env.register(cls.key, root, gitlab=["grp/app"])

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        git(self.root, "checkout", "-q", "--", ".")
        git(self.root, "clean", "-qfd", "--", "src")

    def _session(self, **kw):
        s = sessions.Session(**dict({"project": self.key, "path": self.root, "state": "waiting",
                                     "title": "wb97"}, **kw))
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        return s

    # ------------------------------------------------------------ fs
    def test_read_carries_the_sha_of_the_file(self):
        r = self.m.fs(self.key, "read", {"path": "src/A.java"})
        with open(os.path.join(self.root, "src/A.java"), "rb") as f:
            self.assertEqual(r["sha"], hashlib.sha256(f.read()).hexdigest())

    def test_marks(self):
        self.assertEqual(gitops.parse_u0("@@ -2,0 +3,2 @@\n+x\n@@ -5 +7 @@\n@@ -9,2 +10,0 @@\n@@ -20,1 +19,3 @@\n"
                                         "@@ -30,3 +31,1 @@\n@@ -1,1 +0,0 @@\n"),
                         {"added": [[3, 4], [20, 21]], "changed": [[7, 7], [19, 19], [31, 31]], "deleted": [10, 31, 0]})
        write(self.root, "src/A.java", "class A {\n  int a;\n  int CHANGED;\n  int c;\n  int d;\n}\n")
        r = self.m.fs_marks(self.key, "src/A.java")
        self.assertEqual((r["changed"], r["added"], r["deleted"], r["untracked"]), ([[3, 3]], [[5, 5]], [], False))
        write(self.root, "src/New.java", "a\nb\n")
        self.assertEqual(self.m.fs_marks(self.key, "src/New.java"),
                         {"path": "src/New.java", "added": [[1, 2]], "changed": [], "deleted": [], "untracked": True})
        self.assertEqual(self.m.fs_marks(self.key, "README.md")["added"], [])
        for bad in (".git/HEAD", "../wb97/notes.txt"):
            with self.assertRaises(projectfs.FsError, msg=bad):
                self.m.fs_marks(self.key, bad)
        write(self.root, "notes.txt", "private notes\nmore\n")             # hidden from Claude: the GUI's anyway
        self.assertEqual(self.m.fs_marks(self.key, "notes.txt")["added"], [[2, 2]])
        plain = os.path.realpath(_env.make_project("norepo", {"a.txt": "x\n"}))
        _env.register("team-x/norepo", plain)
        self.assertEqual(self.m.fs_marks("team-x/norepo", "a.txt")["added"], [])

    def test_blob_head_snapshot_and_security(self):
        root = os.path.realpath(_env.make_project("blob97", {
            "visible.txt": "normal\n", "notes.txt": "private\n",
            ".hole/settings.json": '{"files": {"exclude": ["notes.txt"]}}',
            "sub/normal.txt": "nested\n", "binary.dat": "a\0b"}))
        os.symlink("visible.txt", os.path.join(root, "link.txt"))
        os.symlink("notes.txt", os.path.join(root, "secret-link.txt"))
        os.symlink("/tmp", os.path.join(root, "outside-link"))
        write(root, "large.txt", "x" * (projectfs.MAX_READ + 1))
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Tester")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "blob fixture")
        key = "team-x/blob97"
        _env.register(key, root)
        head = self.m.fs_blob(key, "visible.txt")
        self.assertEqual((head["state"], head["text"], head["mode"], head["final_newline"]),
                         ("text", "normal\n", "100644", True))
        link = self.m.fs_blob(key, "link.txt")
        self.assertEqual((link["text"], link["mode"]), ("visible.txt", "120000"))
        os.unlink(os.path.join(root, "link.txt"))
        os.symlink("sub/normal.txt", os.path.join(root, "link.txt"))
        patch = self.m.git_file_diff(key, "link.txt")   # D14: the link itself, never its target
        self.assertIn("-visible.txt", patch)
        self.assertIn("+sub/normal.txt", patch)
        self.assertNotIn("nested", patch)
        self.assertEqual(self.m.fs_blob(key, "missing.txt")["state"], "missing")
        for name, state in (("binary.dat", "binary"), ("large.txt", "too_large")):
            result = self.m.fs_blob(key, name)
            self.assertEqual(result["state"], state)
            self.assertNotIn("text", result)
        # 15.18: a file hidden from Claude is the GUI's too (only marked) - only .git and the outside are refused
        self.assertEqual(self.m.fs_blob(key, "notes.txt")["text"], "private\n")
        self.assertEqual(self.m.fs_blob(key, "secret-link.txt")["text"], "notes.txt")   # the link text, as any link
        self.assertEqual(self.m.fs_blob(key, "visible.txt", orig="notes.txt")["text"], "normal\n")
        for name in ("../visible.txt", ".git/HEAD", "outside-link/file"):
            with mock.patch.object(gitops, "read_blob") as read:
                with self.assertRaises(projectfs.FsError, msg=name):
                    self.m.fs_blob(key, name)
                read.assert_not_called()
        for tree in ("HEAD:notes.txt", "--all", "now", head["oid"]):
            with self.assertRaises(ValueError):
                self.m.fs_blob(key, "visible.txt", tree)
        _env.register("team-x/blob97-sub", os.path.join(root, "sub"))
        with self.assertRaises(projectfs.FsError):
            self.m.fs_blob("team-x/blob97-sub", "../visible.txt")
        self.assertEqual(self.m.fs_blob("team-x/blob97-sub", "normal.txt")["text"], "nested\n")
        session = self._session(project=key, path=root)
        write(root, "visible.txt", "snapshot without newline")
        tree = self.m._snapshot(session, "blob.index")
        self.assertEqual(self.m.fs_blob(key, "visible.txt", tree)["text"], "snapshot without newline")
        self.assertFalse(self.m.fs_blob(key, "visible.txt", tree)["final_newline"])
        self.assertEqual(self.m.fs_blob(key, "notes.txt", tree)["text"], "private\n")
        git(root, "mv", "notes.txt", "renamed.txt")
        self.assertEqual(self.m.fs_blob(key, "renamed.txt")["state"], "missing")   # not in HEAD under that name
        renamed = self.m._snapshot(session, "rename.index")
        self.assertEqual(self.m.fs_blob(key, "renamed.txt", renamed)["text"], "private\n")
        git(root, "reset", "-q", "HEAD")
        self.assertEqual(self.m.fs_blob(key, "renamed.txt", renamed)["text"], "private\n")

    def test_diff_review_directory_symlink_compares_target_text(self):
        os.symlink("src", os.path.join(self.root, "directory-link"))
        self.addCleanup(os.unlink, os.path.join(self.root, "directory-link"))
        self.assertEqual(self.m.fs_blob(self.key, "directory-link")["state"], "missing")
        patch = self.m.git_file_diff(self.key, "directory-link")
        with self.assertRaisesRegex(ValueError, "is a directory"):   # a real directory still is one
            self.m.git_file_diff(self.key, "src")
        self.assertIn("new file mode 120000", patch)
        self.assertIn("+src", patch)
        self.assertNotIn("class A", patch)

    def _review_fixture(self):
        name = "review-" + hashlib.sha256(self._testMethodName.encode()).hexdigest()[:12]
        root = os.path.realpath(_env.make_project(name, {
            "code.txt": "before\n", "old.txt": "rename me\n", "other.txt": "unrelated\n",
            "notes.txt": "private\n", ".hole/settings.json": '{"files":{"exclude":["notes.txt"]}}'}))
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Tester")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "initial")
        key = "team-x/" + name
        _env.register(key, root)
        session = self._session(project=key, path=root)
        frm = self.m._snapshot(session, "review-base.index")
        session.turns = [{"n": 1, "prompt": "Change code", "started": 1, "ended": None, "from": frm, "to": None}]
        write(root, "code.txt", "reviewed\n")
        return root, session

    def _reviewed(self, review, paths):
        rows = {r["path"]: r for r in review["files"]}
        versions = {}
        for path in paths:
            row = rows[path]
            versions[path] = row["after"]
            if row.get("orig"):
                versions[row["orig"]] = row["orig_after"]
        return {"review": review["id"], "head": review["head"], "versions": versions}

    def test_review_pins_one_snapshot_and_checks_both_rename_paths(self):
        root, session = self._review_fixture()
        git(root, "mv", "old.txt", "renamed.txt")
        git(root, "mv", "notes.txt", "public.txt")
        with mock.patch.object(self.m, "_snapshot", wraps=self.m._snapshot) as snapshot:
            r = self.m.review_files(session.id, "session")
            self.assertEqual(snapshot.call_count, 1)
            rows = {x["path"]: x for x in r["files"]}
            self.assertEqual((rows["renamed.txt"]["status"], rows["renamed.txt"]["orig"]), ("R", "old.txt"))
            self.assertEqual((rows["code.txt"]["add"], rows["code.txt"]["del"]), (1, 1))
            # 15.18: hidden from Claude = a mark; the human reviews it (both sides of the rename)
            self.assertTrue(rows["public.txt"]["sensitive"])
            self.assertFalse(rows["public.txt"]["withheld"])
            self.assertEqual(self.m.review_blob(session.id, r["id"], "public.txt", "left")["text"], "private\n")
            self.assertEqual(self.m.review_blob(session.id, r["id"], "renamed.txt", "left")["text"], "rename me\n")
            self.assertEqual(self.m.review_blob(session.id, r["id"], "code.txt", "right")["text"], "reviewed\n")
            write(root, "code.txt", "edited after refresh\n")
            self.assertEqual(self.m.review_blob(session.id, r["id"], "code.txt", "right")["text"], "reviewed\n")
            self.assertEqual(snapshot.call_count, 1)
        self.assertIn("code.txt", self.m.review_check(session.id, r["id"])["changed"])
        for path in ("notes.txt", "../code.txt", ".git/HEAD", "not-in-review.txt"):   # not rows of this review
            with self.assertRaises((ValueError, projectfs.FsError), msg=path):
                self.m.review_blob(session.id, r["id"], path, "right")
        other = self._session(project=session.project, path=root)
        with self.assertRaises(ValueError):
            self.m.review_blob(other.id, r["id"], "code.txt", "right")

    def test_reviewed_commit_rejects_changed_content_mode_and_head_without_staging(self):
        root, session = self._review_fixture()
        r = self.m.review_files(session.id, "head")
        reviewed = self._reviewed(r, ["code.txt"])
        head = git(root, "rev-parse", "HEAD")
        index = git(root, "write-tree")
        with self.assertRaisesRegex(ValueError, "review"):
            self.m.git_commit(session.id, "missing review", ["code.txt"])
        for change in (lambda: write(root, "code.txt", "unseen\n"),
                       lambda: os.chmod(os.path.join(root, "code.txt"), 0o755),
                       lambda: os.unlink(os.path.join(root, "code.txt"))):
            write(root, "code.txt", "reviewed\n")
            os.chmod(os.path.join(root, "code.txt"), 0o644)
            change()
            with self.assertRaisesRegex(ValueError, "changed|refresh"):
                self.m.git_commit(session.id, "must refuse", ["code.txt"], reviewed)
            self.assertEqual(git(root, "rev-parse", "HEAD"), head)
            self.assertEqual(git(root, "write-tree"), index)
        write(root, "code.txt", "reviewed\n")
        write(root, "other.txt", "a concurrent commit\n")
        git(root, "add", "other.txt")
        git(root, "commit", "-qm", "concurrent")
        with self.assertRaisesRegex(ValueError, "HEAD|refresh"):
            self.m.git_commit(session.id, "stale head", ["code.txt"], reviewed)
        self.assertTrue(self.m.review_check(session.id, r["id"])["head_changed"])

    def test_reviewed_commit_uses_exact_blobs_preserves_other_staging_and_commits_both_rename_paths(self):
        root, session = self._review_fixture()
        git(root, "mv", "old.txt", "renamed.txt")
        r = self.m.review_files(session.id, "session")
        reviewed = self._reviewed(r, ["code.txt", "renamed.txt"])
        files = list(reviewed["versions"])
        with self.assertRaisesRegex(ValueError, "versions|rename"):
            self.m.git_commit(session.id, "incomplete rename", ["code.txt", "renamed.txt"], reviewed)
        write(root, "other.txt", "keep staged\n")
        git(root, "add", "other.txt")
        original = gitops.commit_reviewed
        def racing_commit(*args, **kwargs):
            write(root, "code.txt", "agent wrote after validation\n")
            return original(*args, **kwargs)
        with mock.patch.object(gitops, "commit_reviewed", side_effect=racing_commit):
            result = self.m.git_commit(session.id, "reviewed only", files, reviewed)
        self.assertIn("reviewed only", result["commit"])
        self.assertEqual(git(root, "show", "HEAD:code.txt"), "reviewed\n")
        self.assertEqual(git(root, "show", "HEAD:other.txt"), "unrelated\n")
        self.assertIn("other.txt", git(root, "diff", "--cached", "--name-only"))
        self.assertIn("code.txt", git(root, "diff", "--name-only"))
        self.assertEqual(git(root, "show", "HEAD:renamed.txt"), "rename me\n")
        self.assertNotIn("old.txt", git(root, "ls-tree", "--name-only", "HEAD"))
        with self.assertRaisesRegex(ValueError, "HEAD|refresh"):
            self.m.git_commit(session.id, "replay", files, reviewed)

    def test_reviewed_amend_goes_through_the_review_and_keeps_heads_parent(self):
        """9.25: the GUI's amend is a reviewed commit replacing HEAD."""
        root, session = self._review_fixture()
        write(root, "other.txt", "second\n")
        git(root, "commit", "-qm", "second", "--", "other.txt")
        parent = git(root, "rev-parse", "HEAD~1")
        r = self.m.review_files(session.id, "head")
        reviewed = self._reviewed(r, ["code.txt"])
        result = self.m.git_commit(session.id, "amended", ["code.txt"], reviewed, amend=True)
        self.assertIn("amended", result["commit"])
        self.assertEqual(git(root, "rev-parse", "HEAD~1"), parent)
        self.assertEqual(git(root, "show", "HEAD:code.txt"), "reviewed\n")
        self.m.git_commit(session.id, "message only", [], None, amend=True)   # nothing to review
        self.assertEqual((git(root, "log", "-1", "--format=%s"), git(root, "rev-parse", "HEAD~1")), ("message only\n", parent))
        with mock.patch.object(gitops, "check_amend", side_effect=gitops.GitError("HEAD is already pushed")):
            with self.assertRaisesRegex(gitops.GitError, "pushed"):
                self.m.git_commit(session.id, "x", ["code.txt"], reviewed, amend=True)

    def test_reviewed_commit_refuses_head_race_without_touching_real_index(self):
        root, session = self._review_fixture()
        review = self.m.review_files(session.id)
        reviewed = self._reviewed(review, ["code.txt"])
        write(root, "other.txt", "staged independently\n")
        git(root, "add", "other.txt")
        staged = git(root, "diff", "--cached")
        original = gitops._git
        raced = []
        def update_head(top, *args, **kwargs):
            if args and args[0] == "update-ref" and not raced:
                tree = git(root, "rev-parse", "HEAD^{tree}").strip()
                winner = git(root, "commit-tree", tree, "-p", review["head"], "-m", "concurrent commit").strip()
                git(root, "update-ref", "HEAD", winner, review["head"])
                raced.append(winner)
            return original(top, *args, **kwargs)
        with mock.patch.object(gitops, "_git", side_effect=update_head):
            with self.assertRaises(gitops.GitError):
                self.m.git_commit(session.id, "losing commit", ["code.txt"], reviewed)
        self.assertEqual(git(root, "rev-parse", "HEAD").strip(), raced[0])
        self.assertEqual(git(root, "diff", "--cached"), staged)
        self.assertEqual(git(root, "show", "HEAD:code.txt"), "before\n")

    def test_review_snapshot_keeps_same_second_equal_size_edits(self):
        root, session = self._review_fixture()
        git(root, "config", "core.checkStat", "minimal")
        git(root, "config", "core.trustctime", "false")
        stamp = int(time.time()) - 10
        write(root, "code.txt", "before\n")
        os.utime(os.path.join(root, "code.txt"), (stamp, stamp))
        git(root, "add", "code.txt")
        git(root, "commit", "--allow-empty", "-qm", "old stat cache")
        os.utime(os.path.join(root, ".git", "index"), (stamp, stamp))
        session.turns = []
        write(root, "code.txt", "after!\n")
        os.utime(os.path.join(root, "code.txt"), (stamp, stamp))
        review = self.m.review_files(session.id, "head")
        self.assertIn("code.txt", [r["path"] for r in review["files"]])
        self.assertEqual(self.m.review_blob(session.id, review["id"], "code.txt", "right")["text"], "after!\n")

    def test_review_branch_scope_includes_commits_and_current_edits(self):
        root, session = self._review_fixture()
        git(root, "checkout", "-qb", "feat/review")
        write(root, "code.txt", "committed branch change\n")
        git(root, "add", "code.txt")
        git(root, "commit", "-qm", "branch change")
        write(root, "other.txt", "uncommitted change\n")
        review = self.m.review_files(session.id, "branch")
        self.assertEqual({r["path"] for r in review["files"]}, {"code.txt", "other.txt"})
        self.assertIn("Branch start", review["note"])
        self.assertEqual(self.m.review_blob(session.id, review["id"], "code.txt", "left")["text"], "before\n")
        self.assertEqual({r["path"] for r in self.m.review_files(session.id, "head")["files"]}, {"other.txt"})
        git(root, "branch", "mr-target", "HEAD")
        self.m._ci_cache[session.project] = (time.time(), {"branch": "feat/review", "mr": {
            "state": "opened", "source_branch": "feat/review", "target_branch": "mr-target"}})
        review = self.m.review_files(session.id, "branch")
        self.assertEqual({r["path"] for r in review["files"]}, {"other.txt"})
        self.assertIn("against mr-target", review["note"])
        with mock.patch("features.on", return_value=False), mock.patch.object(gitlab, "GitLab") as client:
            review = self.m.review_files(session.id, "branch")
            self.assertEqual({r["path"] for r in review["files"]}, {"other.txt"})
            self.assertIn("against mr-target", review["note"])
            client.assert_not_called()
        self.m._ci_cache[session.project][1]["mr"]["target_branch"] = "missing-target"
        with self.assertRaisesRegex(ValueError, "unavailable locally"):
            self.m.review_files(session.id, "branch")
        with mock.patch("features.on", return_value=False):
            review = self.m.review_files(session.id, "branch")
            self.assertEqual(len(review["files"]), 2)
            self.assertIn("Branch start", review["note"])
        self.m._ci_cache[session.project][1]["branch"] = "another-feature"
        self.assertEqual(len(self.m.review_files(session.id, "branch")["files"]), 2)
        # Checking out an existing remote feature does not mean its tip is the branch's base.
        git(root, "update-ref", "refs/remotes/origin/feat/remote", "HEAD")
        git(root, "checkout", "-qb", "feat/remote", "--no-track", "refs/remotes/origin/feat/remote")
        review = self.m.review_files(session.id, "branch")
        self.assertEqual({r["path"] for r in review["files"]}, {"code.txt", "other.txt"})
        self.assertIn("Common base with main", review["note"])

    def test_review_check_can_validate_one_row_without_reading_the_entire_review(self):
        root, session = self._review_fixture()
        git(root, "mv", "old.txt", "renamed.txt")
        review = self.m.review_files(session.id)
        write(root, "code.txt", "changed after review\n")
        with mock.patch.object(self.m, "_working_version", wraps=self.m._working_version) as reads:
            check = self.m.review_check(session.id, review["id"], "renamed.txt")
        self.assertEqual(check["changed"], [])
        self.assertEqual({call.args[2] for call in reads.call_args_list}, {"old.txt", "renamed.txt"})
        self.assertEqual(self.m.review_check(session.id, review["id"], "code.txt")["changed"], ["code.txt"])
        for path in ("notes.txt", "../code.txt", ".git/config", "missing.txt"):
            with self.assertRaises(projectfs.FsError):
                self.m.review_check(session.id, review["id"], path)

    def test_review_turn_scope_and_unsupported_scope(self):
        root, session = self._review_fixture()
        session.turns[0]["to"] = self.m._snapshot(session, "review-stop.index")
        session.turns[0]["ended"] = 2
        write(root, "code.txt", "later working file\n")
        with mock.patch.object(self.m, "_snapshot", wraps=self.m._snapshot) as snapshot:
            r = self.m.review_files(session.id, "turn", 1)
            self.assertEqual(snapshot.call_count, 0)
        self.assertFalse(r["files"][0]["committable"])
        self.assertEqual(self.m.review_blob(session.id, r["id"], "code.txt", "right")["text"], "reviewed\n")
        with self.assertRaises(ValueError):
            self.m.review_files(session.id, "mr")

    def test_review_stage_uses_pinned_blob_without_committing_or_touching_other_staging(self):
        root, session = self._review_fixture()
        write(root, "new.txt", "reviewed new file\n")
        write(root, "other.txt", "keep staged\n")
        git(root, "add", "other.txt")
        review = self.m.review_files(session.id, "head")
        rows = {row["path"]: row for row in review["files"]}
        self.assertEqual(rows["new.txt"]["live_status"], "??")
        head = git(root, "rev-parse", "HEAD")
        unrelated = git(root, "ls-files", "--stage", "other.txt")
        stage = gitops.stage_reviewed

        def change_after_validation(*args):
            write(root, "new.txt", "concurrent working edit\n")
            return stage(*args)

        with mock.patch.object(gitops, "stage_reviewed", side_effect=change_after_validation):
            self.assertEqual(self.m.review_stage(session.id, review["id"], "new.txt"), {"staged": "new.txt"})
        self.assertEqual(git(root, "show", ":new.txt"), "reviewed new file\n")
        self.assertEqual(git(root, "rev-parse", ":new.txt").strip(), rows["new.txt"]["after"]["oid"])
        self.assertEqual(git(root, "ls-files", "--stage", "other.txt"), unrelated)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        with open(os.path.join(root, "new.txt")) as f:
            self.assertEqual(f.read(), "concurrent working edit\n")

    def test_review_stage_refuses_stale_content_mode_head_and_busy_tree(self):
        root, session = self._review_fixture()
        review = self.m.review_files(session.id, "head")
        index = git(root, "write-tree")
        path = os.path.join(root, "code.txt")
        for kind in ("content", "mode", "missing", "busy"):
            with self.subTest(kind=kind):
                write(root, "code.txt", "reviewed\n")
                os.chmod(path, 0o644)
                if kind == "content":
                    write(root, "code.txt", "unseen\n")
                elif kind == "mode":
                    os.chmod(path, 0o755)
                elif kind == "missing":
                    os.unlink(path)
                else:   # 2026-10-08: a working session refuses only a file its agent edited in this turn
                    session.state, session.since = "working", time.time() - 5
                    session.fsedits.append({"path": "code.txt", "ts": time.time()})
                try:
                    with self.assertRaisesRegex(ValueError, "changed|Claude edited|refresh"):
                        self.m.review_stage(session.id, review["id"], "code.txt")
                    self.assertEqual(git(root, "write-tree"), index)
                    self.assertEqual(git(root, "rev-parse", "HEAD").strip(), review["head"])
                finally:
                    session.state = "waiting"
                    session.fsedits.clear()
        git(root, "commit", "--allow-empty", "-qm", "concurrent HEAD")
        with self.assertRaisesRegex(ValueError, "HEAD|refresh"):
            self.m.review_stage(session.id, review["id"], "code.txt")
        self.assertEqual(git(root, "write-tree"), index)

    def test_review_stage_refuses_historical_blob_that_differs_from_live_file(self):
        root, session = self._review_fixture()
        session.turns[0]["to"] = self.m._snapshot(session, "review-stop.index")
        write(root, "code.txt", "later change\n")
        review = self.m.review_files(session.id, "turn", 1)
        index = git(root, "write-tree")
        with self.assertRaisesRegex(ValueError, "pinned comparison"):
            self.m.review_stage(session.id, review["id"], "code.txt")
        self.assertEqual(git(root, "write-tree"), index)

    def test_review_write_preserves_exact_bytes_and_guards_undo(self):
        root, session = self._review_fixture()
        path = os.path.join(root, "code.txt")
        os.chmod(path, 0o755)
        review = self.m.review_files(session.id, "head")
        head, index = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
        sha = next(row["work_sha"] for row in review["files"] if row["path"] == "code.txt")
        for text in ("without final newline", "with final newline\n", "\u017elu\u0165ou\u010dk\u00fd\r\n\r\n", ""):
            with self.subTest(text=text):
                result = self.m.review_write(session.id, review["id"], "code.txt", text, sha)
                with open(path, "rb") as f:
                    self.assertEqual(f.read(), text.encode())
                self.assertEqual(result["sha"], hashlib.sha256(text.encode()).hexdigest())
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
                sha = result["sha"]
        # Undo reuses the original review, but must compare the post-write version.
        result = self.m.review_write(session.id, review["id"], "code.txt", "reviewed\n", sha)
        write(root, "code.txt", "external edit after replacement\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.m.review_write(session.id, review["id"], "code.txt", "before\n", result["sha"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"external edit after replacement\n")
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "write-tree"), index)

    def test_review_write_reports_line_count_of_exact_result(self):
        root, session = self._review_fixture()
        review = self.m.review_files(session.id, "head")
        sha = next(row["work_sha"] for row in review["files"] if row["path"] == "code.txt")
        result = self.m.review_write(session.id, review["id"], "code.txt", "restored\n", sha)
        self.assertEqual(result["total_lines"], 1)

    def test_review_write_removes_added_file_to_trash_and_undo_restores_exact_mode_and_bytes(self):
        root, session = self._review_fixture()
        original = "#!/bin/sh\r\necho \u017elu\u0165ou\u010dk\u00fd"
        write(root, "added.sh", original.encode(), "wb")
        path = os.path.join(root, "added.sh")
        os.chmod(path, 0o755)
        review = self.m.review_files(session.id, "head")
        row = next(r for r in review["files"] if r["path"] == "added.sh")
        head, index = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
        result = self.m.review_write(session.id, review["id"], "added.sh", None, row["work_sha"])
        self.assertIsNone(result["sha"])
        self.assertFalse(os.path.lexists(path))
        with open(os.path.join(common.PIPER_HOME, "trash", result["trash"], "item"), "rb") as f:
            self.assertEqual(f.read(), original.encode())
        result = self.m.review_write(session.id, review["id"], "added.sh", original, None)
        self.assertEqual(result["sha"], row["work_sha"])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original.encode())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "write-tree"), index)

    def test_review_write_restores_deleted_executable_and_guards_undo(self):
        root, session = self._review_fixture()
        original = "#!/bin/sh\necho restored\n"
        path = os.path.join(root, "old.txt")
        write(root, "old.txt", original)
        os.chmod(path, 0o755)
        git(root, "add", "old.txt")
        git(root, "commit", "-qm", "executable base")
        os.unlink(path)
        review = self.m.review_files(session.id, "head")
        row = next(r for r in review["files"] if r["path"] == "old.txt")
        self.assertIsNone(row["work_sha"])
        head, index = git(root, "rev-parse", "HEAD"), git(root, "write-tree")
        restored = self.m.review_write(session.id, review["id"], "old.txt", original, None)
        self.assertEqual(restored["sha"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)
        # An appeared file cannot be overwritten by reapplying restore.
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.review_write(session.id, review["id"], "old.txt", original, None)
        write(root, "old.txt", "edited after restore\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.m.review_write(session.id, review["id"], "old.txt", None, restored["sha"])
        with open(path) as f:
            self.assertEqual(f.read(), "edited after restore\n")
        write(root, "old.txt", original)
        undone = self.m.review_write(session.id, review["id"], "old.txt", None, restored["sha"])
        self.assertIsNone(undone["sha"])
        self.assertFalse(os.path.lexists(path))
        self.assertEqual(git(root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(root, "write-tree"), index)

    def test_review_write_add_delete_refuses_stale_busy_and_unreviewed_targets(self):
        root, session = self._review_fixture()
        write(root, "added.txt", "added\n")
        os.unlink(os.path.join(root, "old.txt"))
        review = self.m.review_files(session.id, "head")
        rows = {r["path"]: r for r in review["files"]}
        sha = rows["added.txt"]["work_sha"]
        write(root, "added.txt", "changed after review\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.m.review_write(session.id, review["id"], "added.txt", None, sha)
        other = self._session(project=session.project, path=root, state="working")
        other.since = time.time() - 5   # 2026-10-08: refused only where its agent edited in this turn
        other.fsedits.extend({"path": p, "ts": time.time()} for p in ("added.txt", "old.txt"))
        for path, text, expected in (("added.txt", None, sha), ("old.txt", "rename me\n", None)):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Claude edited"):
                self.m.review_write(session.id, review["id"], path, text, expected)
        other.state = "waiting"
        for path in ("notes.txt", ".git/HEAD", "../outside", "other.txt"):
            for text, expected in ((None, sha), ("new", None)):
                with self.subTest(path=path, text=text), self.assertRaises(projectfs.FsError):
                    self.m.review_write(session.id, review["id"], path, text, expected)
        for text, expected in ((None, None), (None, "bad-sha"), (42, None)):
            with self.subTest(text=text, sha=expected), self.assertRaises(ValueError):
                self.m.review_write(session.id, review["id"], "added.txt", text, expected)
        self.assertFalse(os.path.exists(os.path.join(root, "old.txt")))
        with open(os.path.join(root, "added.txt")) as f:
            self.assertEqual(f.read(), "changed after review\n")

    def test_review_write_add_delete_refuses_symlinks_and_parent_symlink_substitution(self):
        root, session = self._review_fixture()
        write(root, "dir/added.txt", "added\n")
        os.symlink("other.txt", os.path.join(root, "link.txt"))
        review = self.m.review_files(session.id, "head")
        rows = {r["path"]: r for r in review["files"]}
        for text, sha in ((None, rows["link.txt"]["work_sha"]), ("target", None)):
            with self.subTest(text=text), self.assertRaises((ValueError, projectfs.FsError)):
                self.m.review_write(session.id, review["id"], "link.txt", text, sha)
        os.rename(os.path.join(root, "dir"), os.path.join(root, "moved"))
        os.symlink("moved", os.path.join(root, "dir"))
        for text, sha in ((None, rows["dir/added.txt"]["work_sha"]), ("target", None)):
            with self.subTest(text=text), self.assertRaises((ValueError, projectfs.FsError)):
                self.m.review_write(session.id, review["id"], "dir/added.txt", text, sha)
        self.assertTrue(os.path.islink(os.path.join(root, "link.txt")))
        with open(os.path.join(root, "moved/added.txt")) as f:
            self.assertEqual(f.read(), "added\n")

    def test_review_write_add_delete_refuses_renames_and_rechecks_hidden_original(self):
        root, session = self._review_fixture()
        git(root, "mv", "old.txt", "renamed.txt")
        review = self.m.review_files(session.id, "head")
        row = next(r for r in review["files"] if r["path"] == "renamed.txt")
        self.assertEqual(row["orig"], "old.txt")
        with self.assertRaises((ValueError, projectfs.FsError)):
            self.m.review_write(session.id, review["id"], "renamed.txt", None, row["work_sha"])
        os.unlink(os.path.join(root, "renamed.txt"))
        with self.assertRaises((ValueError, projectfs.FsError)):
            self.m.review_write(session.id, review["id"], "renamed.txt", "rename me\n", None)
        # Even a normal write still authorizes the original rename path - for the GUI only `.git` / the outside
        # is refused (15.18): an original hidden from Claude is the human's too
        write(root, "renamed.txt", "rename me\n")
        with mock.patch.object(self.m, "_session_hidden", return_value=lambda path: path == "old.txt"):
            self.m.review_write(session.id, review["id"], "renamed.txt", "overwrite", row["work_sha"])
        with open(os.path.join(root, "renamed.txt")) as f:
            self.assertEqual(f.read(), "overwrite")

    def test_review_write_refuses_busy_tree_and_unreviewed_or_private_paths(self):
        root, session = self._review_fixture()
        review = self.m.review_files(session.id, "head")
        sha = next(row["work_sha"] for row in review["files"] if row["path"] == "code.txt")
        other = self._session(project=session.project, path=root, state="working")
        # 2026-10-08: a GUI save is allowed while a session works - the sha lock catches what Claude changed
        write(root, "code.txt", "Claude's edit\n")
        with self.assertRaisesRegex(ValueError, "changed since"):
            self.m.review_write(session.id, review["id"], "code.txt", "overwrite", sha)
        write(root, "code.txt", "reviewed\n")
        other.state = "waiting"
        for path in ("other.txt", "notes.txt", ".git/HEAD", "../code.txt"):
            with self.subTest(path=path), self.assertRaises(projectfs.FsError):
                self.m.review_write(session.id, review["id"], path, "overwrite", sha)
        with open(os.path.join(root, "code.txt")) as f:
            self.assertEqual(f.read(), "reviewed\n")

    def test_review_dispatch_new_session_preserves_agent_parent_and_options(self):
        root, session = self._review_fixture()
        text = "Explain the changed contract and add its regression test."
        body = {"text": text, "target": "new", "edit": True,
                "items": [{"path": "code.txt", "sha": hashlib.sha256(b"reviewed\n").hexdigest()}]}
        session.opts = {"docker": True, "idea": True}
        # Exercise real Manager.create, stubbing only the process-launch boundary.
        with mock.patch.dict(self.m.sessions, {session.id: session}, clear=True), mock.patch.object(self.m, "_start"):
            for agent in ("claude", "codex"):
                with self.subTest(agent=agent):
                    session.agent = agent
                    result = self.m.review_dispatch(session.id, body)
                    child = self.m.get(result["session"])
                    self.assertEqual(result, {"session": child.id, "sent": True, "agent": agent})
                    self.assertEqual((child.agent, child.parent, child.opts), (agent, session.id, session.opts))
                    self.assertEqual((child.project, child.path, child.task), (session.project, root, text))
                    self.assertEqual(child.title, "Review: " + session.title)

    def test_review_dispatch_current_preserves_prompt_and_refuses_stale_or_busy(self):
        root, session = self._review_fixture()
        body = {"text": "Explain first.\nThen test.\n", "target": "current", "edit": True,
                "items": [{"path": "code.txt", "sha": hashlib.sha256(b"reviewed\n").hexdigest()}]}
        with mock.patch.object(self.m, "send") as send, mock.patch.object(self.m, "create") as create:
            self.assertEqual(self.m.review_dispatch(session.id, body), {"session": session.id, "sent": True})
            send.assert_called_once_with(session.id, body["text"])
            create.assert_not_called()
            send.reset_mock()
            session.state = "working"
            with self.assertRaisesRegex(ValueError, "working"):
                self.m.review_dispatch(session.id, body)
            session.state = "waiting"
            # One stale item prevents dispatch of the entire batch, even after a valid item.
            write(root, "other.txt", "changed after annotation\n")
            body["items"].append({"path": "other.txt", "sha": hashlib.sha256(b"unrelated\n").hexdigest()})
            for target in ("current", "new"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "changed since review"):
                    self.m.review_dispatch(session.id, dict(body, target=target))
            send.assert_not_called()
            create.assert_not_called()

    def test_review_dispatch_waiting_keeps_existing_busy_and_response_contract(self):
        _, session = self._review_fixture()
        session.state = "waiting"
        session.subagents = {"native": "reviewer"}
        session.background_tasks = {"task": "shell"}
        self.assertIsNone(session.turns[-1]["to"])
        body = {"text": "Explain the change.", "target": "current", "edit": False,
                "items": [{"path": "code.txt", "sha": hashlib.sha256(b"reviewed\n").hexdigest()}]}
        with mock.patch.object(self.m, "send", return_value=False) as send:
            self.assertEqual(self.m.review_dispatch(session.id, body), {"session": session.id, "sent": True})
            send.assert_called_once_with(session.id, body["text"])

    def test_review_dispatch_edit_refuses_other_working_session(self):
        root, session = self._review_fixture()
        self._session(project=session.project, path=root, state="working")
        body = {"text": "Add tests.", "target": "new",
                "items": [{"path": "code.txt", "sha": hashlib.sha256(b"reviewed\n").hexdigest()}]}
        with mock.patch.object(self.m, "create") as create, mock.patch.object(self.m, "send") as send:
            for target in ("current", "new"):
                for edit in (None, True):
                    with self.subTest(target=target, edit=edit), self.assertRaisesRegex(ValueError, "working"):
                        self.m.review_dispatch(session.id, dict(body, target=target, edit=edit))
            create.assert_not_called()
            send.assert_not_called()

    def test_review_shows_a_hidden_file_to_the_human_but_never_sends_it_to_a_session(self):
        """15.18 + in the review a file hidden from Claude is marked and reviewable (content, history,
        writes); a task for a session naming it is refused before anything is sent."""
        root, session = self._review_fixture()
        write(root, "notes.txt", "private TOPSECRET\n")
        review = self.m.review_files(session.id, "head")
        row = next(r for r in review["files"] if r["path"] == "notes.txt")
        self.assertEqual((row["sensitive"], row["withheld"]), (True, False))
        self.assertEqual(self.m.review_blob(session.id, review["id"], "notes.txt", "right")["text"], "private TOPSECRET\n")
        self.assertEqual(self.m.review_blob(session.id, review["id"], "notes.txt", "left")["text"], "private\n")
        body = {"text": "Look at it.", "target": "current", "edit": False,
                "items": [{"path": "notes.txt", "sha": row["work_sha"]}]}
        with mock.patch.object(self.m, "send") as send, mock.patch.object(self.m, "create") as create:
            for target in ("current", "new"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "hidden from Claude"):
                    self.m.review_dispatch(session.id, dict(body, target=target))
            # a rename from the hidden file: the new name is visible, the old side (its code) is not
            git(root, "mv", "notes.txt", "public.txt")
            review = self.m.review_files(session.id, "head")
            row = next(r for r in review["files"] if r["path"] == "public.txt")
            with self.assertRaisesRegex(ValueError, "hidden from Claude"):
                self.m.review_dispatch(session.id, dict(body, items=[{"path": "public.txt", "sha": row["work_sha"]}]))
            send.assert_not_called()
            create.assert_not_called()

    def test_review_dispatch_refuses_a_branch_rename_from_a_hidden_file_and_a_prompt_naming_one(self):
        """R5 / N-I2 (2026-10-09): dispatch looked for a rename's original only in the live status (working tree
        vs HEAD) - a rename hidden -> visible committed on the branch (scope Entire branch) passed, and the GUI's
        "original side" code of the hidden file could go into the prompt. The prompt text itself was never
        checked: a text naming a file hidden from Claude now is refused as well."""
        root, session = self._review_fixture()
        git(root, "checkout", "-q", "-b", "feature")
        git(root, "add", "code.txt")
        git(root, "mv", "notes.txt", "public.txt")
        git(root, "commit", "-qm", "rename on the branch")
        self.assertNotIn("public.txt", git(root, "status", "--porcelain"))
        review = self.m.review_files(session.id, "branch")
        row = next(r for r in review["files"] if r["path"] == "public.txt")
        self.assertEqual(row.get("orig"), "notes.txt")
        code = next(r for r in review["files"] if r["path"] == "code.txt")
        with mock.patch.object(self.m, "send") as send, mock.patch.object(self.m, "create") as create:
            for target in ("current", "new"):
                with self.subTest(target=target), self.assertRaisesRegex(ValueError, "hidden from Claude"):
                    self.m.review_dispatch(session.id, {"text": "Original side:\nprivate\n", "target": target, "edit": False,
                                                        "items": [{"path": "public.txt", "sha": row["work_sha"]}]})
                with self.subTest(target=target, prompt=True), self.assertRaisesRegex(ValueError, "hidden from Claude"):
                    self.m.review_dispatch(session.id, {"text": "Compare code.txt with notes.txt", "target": target,
                                                        "edit": False, "items": [{"path": "code.txt", "sha": code["work_sha"]}]})
            send.assert_not_called()
            create.assert_not_called()
            # the control: the same visible target with a prompt naming nothing hidden goes - a visible path
            # that merely ends like a hidden one (src/notes.txt) is not the hidden notes.txt
            self.m.review_dispatch(session.id, {"text": "Explain code.txt and src/notes.txt", "target": "current",
                                                "edit": False, "items": [{"path": "code.txt", "sha": code["work_sha"]}]})
            send.assert_called_once()

    def test_review_dispatch_refuses_bad_requests_without_sending(self):
        root, session = self._review_fixture()
        body = {"text": "Explain.", "target": "current",
                "items": [{"path": "code.txt", "sha": hashlib.sha256(b"reviewed\n").hexdigest()}]}
        invalid = ({"text": " "}, {"text": "x" * 20001}, {"target": "unknown"}, {"items": []},
                   {"items": body["items"] * 201}, {"items": [{"path": "code.txt"}]},
                   {"items": [{"path": "notes.txt", "sha": "0" * 64}]},
                   {"items": [{"path": "../code.txt", "sha": "0" * 64}]})
        with mock.patch.object(self.m, "send") as send, mock.patch.object(self.m, "create") as create:
            for change in invalid:
                with self.subTest(change=repr(change)[:80]), self.assertRaises((ValueError, projectfs.FsError)):
                    self.m.review_dispatch(session.id, dict(body, **change))
            send.assert_not_called()
            create.assert_not_called()

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_queue_deduplicates_needs_and_tracks_native_subagents(self):
        import piperd
        import threading
        root, blocked = self._review_fixture()
        blocked.state, blocked.idle = "waiting", True
        blocked.since = time.time() - 3600
        blocked.watch = [root + "/.claude/worktrees/demo/config/secret-%03d.properties" % n for n in range(150)]
        worker = sessions.Session(project=blocked.project, path=root, state="working", title="Native agents", claude_session="parent-1234")
        os.makedirs(worker.box, exist_ok=True)
        def emit(ev, **fields):
            with open(os.path.join(worker.box, worker.names["events"]), "a") as f:
                f.write(json.dumps({"ev": ev, "session_id": worker.claude_session, **fields}) + "\n")
            self.m._events(worker)
        emit("SubagentStart", agent_id="a", agent_type="reviewer")
        emit("SubagentStart", agent_id="b", agent_type="tester")
        emit("Stop")
        old_manager, old_port, old_sessions = piperd.MANAGER, piperd.PORT, self.m.sessions
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        piperd.MANAGER, piperd.PORT, self.m.sessions = self.m, server.server_port, {blocked.id: blocked, worker.id: worker}
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = "piper-queue-" + worker.id
        def cli(*args):
            run = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(run.stdout)
            self.assertTrue(data["success"])
            return data.get("data", {}).get("result")
        def ready(code):
            self.assertTrue(wait(lambda: cli("eval", code) is True, timeout=20), code)
        blocked_rows = 'document.querySelectorAll(\'.q-sec.needs .qi[data-sid="%s"]\')' % blocked.id
        worker_row = 'document.querySelector(\'.q-sec.running .qi[data-sid="%s"]\')' % worker.id
        try:
            cli("open", piperd.gui_url())
            ready(blocked_rows + '.length===1')
            blocked.state = "working"
            self.m.bump()
            ready(blocked_rows + '.length===0')
            warning_row = 'document.querySelector(\'.q-sec.running .qi[data-sid="%s"]\')' % blocked.id
            ready(warning_row + '?.textContent.includes("L5 · 150")===true')
            self.assertTrue(cli("eval", warning_row + '.classList.contains("g-run")'))
            # orchestrator's queue starts folded to the strip (tiles): open the whole queue as a user would
            cli("eval", 'if(document.body.classList.contains("queue-mini")) document.querySelector("#actbar [aria-label=Overview]").click(); true')
            ready('!document.body.classList.contains("queue-mini")')
            cli("click", '.q-sec.running .qi[data-sid="%s"]' % blocked.id)
            ready(r'!!document.querySelector("button[aria-label=\"context panel\"]")')
            cli("eval", r'let b=document.querySelector("button[aria-label=\"context panel\"]");if(b.getAttribute("aria-pressed")!=="true") b.click();true')
            ready(r'document.querySelectorAll(".watch-preview li").length===3')
            self.assertEqual(cli("eval", r'document.querySelector("#inspector .alert h3").textContent'), "Isolation warning")
            self.assertFalse(cli("eval", r'document.querySelector(".watch-more").open'))
            self.assertEqual(cli("eval", r'document.querySelectorAll(".watch-findings li").length'), 147)
            self.assertTrue(cli("eval", r'[...document.querySelectorAll(".watch-preview code")].every(x=>!x.textContent.startsWith("/"))'))
            cli("click", '.watch-more summary')
            ready(r'document.querySelector(".watch-more").open && document.querySelector(".watch-findings").clientHeight<=160')
            self.assertTrue(cli("eval", r'document.querySelector(".watch-findings").scrollHeight>document.querySelector(".watch-findings").clientHeight'))
            ready(worker_row + '?.textContent.includes("2 subagents")===true')
            self.assertTrue(cli("eval", worker_row + '.classList.contains("g-run")'))
            emit("Notification", notification_type="permission_prompt", message="Claude needs permission")
            ready('document.querySelector(\'.q-sec.needs .qi[data-sid="%s"]\')?.textContent.includes("2 subagents")===true' % worker.id)
            self.assertIsNone(cli("eval", worker_row))
            emit("Stop")
            ready(worker_row + '?.textContent.includes("2 subagents")===true')
            emit("SubagentStop", agent_id="a")
            ready(worker_row + '?.textContent.includes("1 subagent")===true')
            emit("SubagentStop", agent_id="b")
            ready('document.querySelectorAll(\'.q-sec.needs .qi[data-sid="%s"]\').length===1' % worker.id)
            self.assertIsNone(cli("eval", worker_row))
            self.assertTrue(cli("eval", blocked_rows + '.length===0'))
            directory = os.path.join(worker.claude_src, "projects", "browser-background")
            os.makedirs(directory, exist_ok=True)
            transcript = os.path.join(directory, worker.claude_session + ".jsonl")
            def record(rec):
                with open(transcript, "a") as f:
                    f.write(json.dumps(dict(rec, sessionId=worker.claude_session)) + "\n")
                self.m._background(worker)
            record({"type": "user", "toolUseResult": {"backgroundTaskId": "shell-1"}})
            record({"type": "user", "toolUseResult": {"taskId": "monitor-1", "timeoutMs": 1800000, "persistent": False}})
            ready(worker_row + '?.textContent.includes("1 shell1 monitor")===true')
            self.assertTrue(cli("eval", worker_row + '.classList.contains("g-run")'))
            emit("Notification", notification_type="permission_prompt", message="Claude needs permission")
            ready('document.querySelector(\'.q-sec.needs .qi[data-sid="%s"]\')?.textContent.includes("1 shell1 monitor")===true' % worker.id)
            self.assertIsNone(cli("eval", worker_row))
            emit("Stop")
            ready(worker_row + '?.textContent.includes("1 shell1 monitor")===true')
            for tid in ("shell-1", "monitor-1"):
                record({"type": "queue-operation", "operation": "enqueue", "content": "<task-notification><task-id>%s</task-id><status>completed</status></task-notification>" % tid})
            ready('document.querySelectorAll(\'.q-sec.needs .qi[data-sid="%s"]\').length===1' % worker.id)
            self.assertIsNone(cli("eval", worker_row))
        finally:
            subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=20)
            server.shutdown()
            server.server_close()
            piperd.MANAGER, piperd.PORT, self.m.sessions = old_manager, old_port, old_sessions

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_resume_picker_filters_keyboard_preview_and_native_fork(self):
        import digest
        import piperd
        import threading
        import uuid
        from datetime import datetime, timezone
        root, original = self._review_fixture()
        original.state, original.hidden, original.title_source = "exited", True, "auto"
        original.claude_session = str(uuid.uuid4())
        original.title = original.project
        today = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
        yesterday = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
        def save(s, records):
            if s.agent == "codex":
                rel = "sessions/2026/10/08/rollout-demo-" + s.claude_session + ".jsonl"
                records = [{"type": "session_meta", "payload": {"id": s.claude_session}}] + records
            else:
                rel = "projects/-picker/" + s.claude_session + ".jsonl"
                records = [dict(r, sessionId=s.claude_session) for r in records]
            os.makedirs(os.path.dirname(os.path.join(s.claude_src, rel)), exist_ok=True)
            raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
            common.safe_write(s.claude_src, rel, raw)
            return rel, raw
        records = [{"type": "user", "timestamp": yesterday, "gitBranch": "pdf-branch", "message": {"content": "Investigate issue 1213"}}]
        records += [{"type": "assistant", "timestamp": yesterday, "message": {"content": "Earlier conversation %d" % n}} for n in range(420)]
        records += [{"type": "user", "timestamp": yesterday, "message": {"content": "context " * 1000 + "OldFulltext7319"}},
                    {"type": "assistant", "timestamp": yesterday, "message": {"content": "PDF generation fixed.\n" + "Verification succeeded.\n" * 350}},
                    {"type": "ai-title", "aiTitle": "Invoice PDF repair"}]
        original_rel, original_bytes = save(original, records)
        other_root = os.path.realpath(_env.make_project("picker-other", {"README.md": "Other project\n"}))
        _env.register("team-x/picker-other", other_root)
        codex = sessions.Session(project="team-x/picker-other", path=other_root, state="exited", agent="codex", claude_mode="none",
                                 claude_session=str(uuid.uuid4()), title_source="auto")
        save(codex, [{"type": "response_item", "timestamp": today, "payload": {"type": "message", "role": "user",
                      "content": [{"type": "input_text", "text": "Inspect schema migration"}]}},
                     {"type": "response_item", "timestamp": today, "payload": {"type": "message", "role": "assistant",
                      "content": [{"type": "output_text", "text": "Schema migration verified."}]}}])
        common.safe_write(codex.codex_dir, "session_index.jsonl", (json.dumps({"id": codex.claude_session, "thread_name": "Schema migration"}) + "\n").encode())
        missing = sessions.Session(project=original.project, path=root, state="error", title="Missing saved conversation", claude_session=str(uuid.uuid4()))
        running = sessions.Session(project=original.project, path=root, state="working", title="Running conversation", claude_session=str(uuid.uuid4()))
        save(running, [{"type": "user", "timestamp": today, "message": {"content": "Working on a report"}}])
        duplicate = sessions.Session(project=original.project, path=root, state="exited", claude_session=original.claude_session, since=original.since - 100)
        local_id = str(uuid.uuid4())
        local_rel = "projects/-picker-local/" + local_id + ".jsonl"
        local_cfg = os.path.join(common.REAL_HOME, ".claude")
        os.makedirs(os.path.dirname(os.path.join(local_cfg, local_rel)), exist_ok=True)
        local_bytes = (json.dumps({"type": "user", "sessionId": local_id, "cwd": root, "gitBranch": "local-pdf", "timestamp": yesterday,
                                  "message": {"content": "Continue local invoice work"}}) + "\n").encode()
        common.safe_write(local_cfg, local_rel, local_bytes)
        idx = digest.ConversationIndex(os.path.join(original.dir, "browser-index.sqlite"))
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "piper-resume-" + original.id
        started = []
        def fake_start(s):
            started.append(s)
            self.m._set(s, "ready")
        def cli(*args):
            run = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(run.stdout)
            self.assertTrue(data["success"], str(data)[:400])
            return data.get("data", {}).get("result")
        def ready(code):
            self.assertTrue(wait(lambda: cli("eval", code) is True, timeout=15), code)
        def click(code):
            self.assertTrue(cli("eval", "(()=>{const b=" + code + ";if(!b)return false;b.scrollIntoView({block:'center'});b.click();return true})()"))
        picker = 'document.querySelector(".resume-picker")'
        resume = 'document.querySelector("[aria-label=\\\"Resume selected conversation\\\"]")'
        fork = 'document.querySelector("[aria-label=\\\"Fork selected conversation\\\"]")'
        # All mutations below are confined to this temporary fixture and use the real HTTP routes.
        with mock.patch.object(piperd, "MANAGER", self.m), mock.patch.object(piperd, "PORT", server.server_port), \
                mock.patch.object(self.m, "sessions", {s.id: s for s in (original, duplicate, codex, missing, running)}), \
                mock.patch.object(self.m, "_conversation_index", idx), mock.patch.object(self.m, "_start", fake_start):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                ready('!!document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                click('document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                ready('document.querySelectorAll(".resume-result").length===4 && !document.querySelector(".resume-status progress")')
                self.assertEqual(cli("eval", 'document.querySelector(".resume-project").textContent'), "All projects ▾")
                self.assertTrue(cli("eval", '[...document.querySelectorAll(".resume-title")].some(x=>x.textContent==="Invoice PDF repair")'))
                self.assertTrue(cli("eval", '[...document.querySelectorAll(".resume-title")].some(x=>x.textContent==="Schema migration")'))
                self.assertFalse(cli("eval", 'document.querySelector("[aria-label=\\\"Include conversations outside Piper\\\"]").checked'))
                self.assertTrue(cli("eval", '[...document.querySelectorAll(".resume-result")].some(x=>x.textContent.includes("pdf-branch")&&x.textContent.includes("Created"))'))
                cli("fill", '[aria-label="Search saved conversations"]', "OldFulltext7319")
                ready('document.querySelectorAll(".resume-result").length===1 && document.querySelector(".resume-detail mark")?.textContent==="OldFulltext7319"')
                ready(resume + '.disabled===false && ' + fork + '.disabled===false')
                self.assertTrue(cli("eval", 'document.querySelector(".resume-detail").scrollHeight>document.querySelector(".resume-detail").clientHeight'))
                cli("eval", 'document.querySelector(".resume-detail").scrollTop=300;true')
                self.assertGreater(cli("eval", 'document.querySelector(".resume-detail").scrollTop'), 0)
                cli("fill", '[aria-label="Search saved conversations"]', "")
                ready('document.querySelectorAll(".resume-result").length===4')
                click('document.querySelector(".resume-project")')
                cli("fill", '[aria-label="Find a history project"]', "picker-other")
                click('[...document.querySelectorAll(".resume-project-options button")].find(x=>x.textContent.includes("team-x/picker-other"))')
                ready('document.querySelectorAll(".resume-result").length===1 && document.querySelector(".resume-title").textContent==="Schema migration"')
                click('document.querySelector("[aria-label=\\\"Clear project filter\\\"]")')
                ready('document.querySelectorAll(".resume-result").length===4')
                cli("select", '[aria-label="Filter conversations by agent"]', "codex")
                ready('document.querySelectorAll(".resume-result").length===1 && document.querySelector(".resume-title").textContent==="Schema migration"')
                cli("select", '[aria-label="Filter conversations by agent"]', "")
                ready('document.querySelectorAll(".resume-result").length===4')
                click('document.querySelector(\'.resume-result[data-session="' + missing.id + '"]\')')
                ready('document.querySelector(".resume-detail").textContent.includes("missing") && ' + resume + '.disabled && ' + fork + '.disabled')
                click('document.querySelector(\'.resume-result[data-session="' + running.id + '"]\')')
                ready(resume + '.textContent.includes("Open running session")')
                cli("focus", '[aria-label="Search saved conversations"]')
                cli("press", "ArrowDown")
                ready('document.querySelector(".resume-result[aria-selected=true]").dataset.session!=="' + running.id + '"')
                click('document.querySelector(\'.resume-result[data-session="' + original.id + '"]\')')
                ready(resume + '.disabled===false')
                cli("focus", '[aria-label="Search saved conversations"]')
                cli("press", "Enter")
                ready(picker + '===null')
                self.assertTrue(wait(lambda: bool(started)))
                self.assertIs(started[0], original)
                self.assertFalse(original.hidden)
                self.assertEqual(original.resume, original.claude_session)
                ready('!!document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                click('document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                ready('document.querySelectorAll(".resume-result").length===4')
                click('document.querySelector(\'.resume-result[data-session="' + original.id + '"]\')')
                ready(fork + '.disabled===false')
                click(fork)
                ready(picker + '===null')
                self.assertTrue(wait(lambda: len(started) == 2))
                child = started[1]
                self.assertNotEqual(child.id, original.id)
                self.assertTrue(child.fork_pending)
                self.assertEqual(child.resume, original.claude_session)
                self.assertEqual(common.safe_read(original.claude_src, original_rel), original_bytes)
                ready('!!document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                click('document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                ready('!!document.querySelector(".resume-results")')
                self.assertFalse(cli("eval", 'document.querySelector("[aria-label=\\\"Include conversations outside Piper\\\"]").checked'))
                cli("check", '[aria-label="Include conversations outside Piper"]')
                ready('!document.querySelector(".resume-status progress") && [...document.querySelectorAll(".resume-title")].some(x=>x.textContent==="Continue local invoice work")')
                click('[...document.querySelectorAll(".resume-result")].find(x=>x.textContent.includes("Continue local invoice work"))')
                ready(resume + '.textContent.includes("Import and continue") && !' + resume + '.disabled')
                self.assertTrue(cli("eval", 'document.querySelector(".resume-detail").textContent.includes("local-pdf")'))
                cli("select", '[aria-label="Project for imported conversation"]', "team-x/picker-other")
                ready(resume + '.disabled && !!document.querySelector("[aria-label=\\\"Confirm another checkout\\\"]")')
                cli("check", '[aria-label="Confirm another checkout"]')
                ready(resume + '.disabled===false')
                cli("select", '[aria-label="Project for imported conversation"]', original.project)
                ready(resume + '.disabled===false')
                click(resume)
                ready(picker + '===null')
                self.assertTrue(wait(lambda: len(started) == 3))
                imported = started[2]
                self.assertTrue(imported.fork_pending)
                self.assertEqual(imported.resume, local_id)
                self.assertEqual(imported.import_from["cwd"], root)
                self.assertEqual(common.safe_read(local_cfg, local_rel), local_bytes)
                self.assertEqual(common.safe_read(imported.claude_src, local_rel), local_bytes)
                click('document.querySelector("[aria-label=\\\"Find ended conversations\\\"]")')
                ready('!!document.querySelector(".resume-results")')
                self.assertFalse(cli("eval", 'document.querySelector("[aria-label=\\\"Include conversations outside Piper\\\"]").checked'))
                cli("set", "viewport", "650", "720")
                self.assertTrue(cli("eval", 'document.querySelector(".resume-picker").getBoundingClientRect().bottom<=innerHeight'))
                cli("press", "Escape")
                ready(picker + '===null')
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=20)
                server.shutdown()
                server.server_close()

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_review_shortcuts_open_source_before_editor_doc_end(self):
        import piperd
        root, session = self._review_fixture()
        baseline = "".join("line %d\n" % n for n in range(1, 83))
        write(root, "code.txt", baseline)
        write(root, "long.txt", "".join("before %d\n" % n for n in range(400)))
        write(root, "removed.txt", "Removed working code\n")
        for n in range(30):
            write(root, "file%02d.txt" % n, "before\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "browser baseline")
        git(root, "checkout", "-qb", "feat/browser-review")
        frm = self.m._snapshot(session, "browser-base.index")
        session.turns = [{"n": 1, "prompt": "Review lines", "started": 1, "ended": None, "from": frm, "to": None}]
        write(root, "code.txt", baseline.replace("line 9\n", "reviewed line 9\n").replace("line 66\n", "reviewed line 66\n"))
        write(root, "long.txt", "".join("after %d\n" % n for n in range(400)))
        os.unlink(os.path.join(root, "removed.txt"))
        write(root, "added.txt", "New working code\n")
        for n in range(30):
            write(root, "file%02d.txt" % n, "after\n")
        sent = []
        old_send = self.m.send
        self.m.send = lambda sid, text: sent.append((sid, text))
        old_manager, old_port, old_sessions = piperd.MANAGER, piperd.PORT, self.m.sessions
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        piperd.PORT, piperd.MANAGER = server.server_port, self.m
        self.m.sessions = {session.id: session}
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = "piper-regression-" + session.id
        def cli(*args):
            run = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, (run.stdout + run.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(run.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")
        def evaluate(code):
            return cli("eval", code)
        def ready(code):
            if not wait(lambda: evaluate(code) is True, timeout=20):
                diagnostic = evaluate(r'({banner:document.querySelector(".review-banner")?.textContent,alerts:document.getElementById("toasts")?.textContent,code:document.querySelector(".review-code .bad")?.textContent,editor:document.querySelector(".review-note-edit")?.textContent.slice(0,100),file:document.querySelector(".review-detail strong")?.textContent,classes:document.querySelector(".review-merge")?.className})')
                diagnostic["navigation"] = evaluate(r'({focus:document.activeElement?.outerHTML.slice(0,160),chunk:document.querySelector(".review-merge")?.dataset.activeChunk,marks:document.querySelectorAll(".cm-review-active-chunk").length,working:document.querySelector(".cm-merge-b .cm-content")?.textContent.slice(0,80),rect:document.querySelector(".cm-merge-b")?.getBoundingClientRect().toJSON()})')
                self.fail("browser UI did not settle: " + str(diagnostic))
        # F23 (9.87): block operations and the layout toggles sit in ⋯, the actions in "Ask Claude ▾"
        def more_click(selector):
            evaluate(r'(()=>{if(document.querySelector(".review-more").classList.contains("hidden"))document.querySelector(".review-more-btn").click();return true})()')
            cli("click", selector)
        def ask_open():
            evaluate(r'(()=>{if(document.querySelector(".review-ask-pop").classList.contains("hidden"))document.querySelector(".review-ask-btn").click();return true})()')
        def click_visible(selector):
            if selector.startswith((".review-palette", ".review-target-controls")):
                ask_open()
            encoded = json.dumps(selector)
            evaluate('document.querySelector(%s).scrollIntoView({block:"nearest"});true' % encoded)
            ready('(()=>{let b=document.querySelector(%s),r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()' % encoded)
            cli("click", selector)
        try:
            cli("open", piperd.gui_url())
            ready(r'!!document.querySelector("button[aria-label=\"context panel\"]")')
            evaluate(r'document.querySelector("button[aria-label=\"context panel\"]").click()')
            ready(r'!!document.querySelector("button[title=\"Review the whole session\"]")')
            evaluate(r'window.reviewHistoryCalls=0;window.reviewBaseFetch=window.fetch;window.fetch=(u,o)=>{if(String(u).includes("/review/history")){reviewHistoryCalls++;return new Promise(()=>{})}return reviewBaseFetch(u,o)};document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length === 2')
            self.assertEqual(evaluate(r'Array.from(document.querySelector("select[aria-label=\"Review scope\"]").options,o=>[o.value,o.textContent])'), [["branch", "Entire branch / MR"], ["head", "Uncommitted changes"], ["session", "This session’s changes"]])
            self.assertEqual(evaluate("reviewHistoryCalls"), 0)
            # A slow comparison clears old code, exposes an indeterminate progress bar, then settles.
            evaluate(r'window.reviewDelayFetch=window.fetch;window.fetch=(u,o)=>String(u).includes("/review?")?new Promise(resolve=>setTimeout(()=>resolve(reviewDelayFetch(u,o)),2200)):reviewDelayFetch(u,o)')
            cli("select", 'select[aria-label="Review scope"]', "head")
            self.assertTrue(evaluate(r'document.querySelector(".reviewpane").getAttribute("aria-busy")==="true" && !document.querySelector(".review-merge") && !document.querySelector(".review-loading").classList.contains("hidden") && !document.querySelector(".review-loading progress").hasAttribute("value")'))
            ready(r'document.querySelector(".review-loading").textContent.includes("1 s")')
            ready(r'document.querySelector(".reviewpane").getAttribute("aria-busy")==="false" && document.querySelectorAll(".review-merge .cm-editor").length===2')
            evaluate(r'window.fetch=reviewDelayFetch')
            for value, hint in (("head", "Last commit"), ("branch", "Branch base"), ("session", "Session start")):
                ready(r'!document.querySelector("select[aria-label=\"Review scope\"]").disabled')
                cli("select", 'select[aria-label="Review scope"]', value)
                ready(r'!document.querySelector("select[aria-label=\"Review scope\"]").disabled && document.querySelectorAll(".review-merge .cm-editor").length===2')
                self.assertIn(hint, evaluate(r'document.querySelector(".review-scope-hint").textContent'))
            self.assertEqual(evaluate("reviewHistoryCalls"), 0)
            self.assertIn("Session start", evaluate(r'document.querySelector(".review-scope-hint").textContent'))
            self.assertEqual(evaluate(r'document.querySelector("button[title=\"Next change\"] kbd").textContent'), "F7")
            self.assertEqual(evaluate(r'document.querySelector("button[title=\"Previous change\"] kbd").textContent'), "⇧F7")
            self.assertTrue(evaluate(r'document.querySelector("button[aria-label=\"Next file\"] kbd").textContent.includes("j")'))
            self.assertTrue(evaluate(r'document.querySelector("button[aria-label=\"Previous file\"] kbd").textContent.includes("p")'))
            # Space belongs to the working document; fullscreen and panel toggles preserve both editor instances.
            self.assertTrue(evaluate(r'(()=>{let a=document.querySelector(".cm-merge-a").clientWidth,b=document.querySelector(".cm-merge-b").clientWidth;return b>a*1.5})()'))
            evaluate(r'window.initialReviewEditors=[...document.querySelectorAll(".review-merge .cm-editor")];window.initialCodeWidth=document.querySelector(".review-code").clientWidth;true')
            more_click('button[aria-label="Toggle review fullscreen"]')
            ready(r'(()=>{let a=document.querySelector(".modal:has(.reviewpane)").getBoundingClientRect();return a.x===0 && a.y===0 && Math.abs(a.width-innerWidth)<2 && Math.abs(a.height-innerHeight)<2})()')
            more_click('button[aria-label="Toggle review file list"]')
            self.assertTrue(evaluate(r'document.querySelector(".review-code").clientWidth>initialCodeWidth+150'))
            self.assertTrue(evaluate(r'initialReviewEditors.every((x,i)=>x===document.querySelectorAll(".review-merge .cm-editor")[i])'))
            self.assertTrue(evaluate(r'getComputedStyle(document.querySelector(".review-list")).display==="none" && getComputedStyle(document.querySelector(".review-notes")).display==="none"'))
            cli("press", "6")
            ready(r'document.activeElement === document.querySelector("textarea[aria-label=\"Custom review instruction\"]") && document.querySelector("button[aria-label=\"Toggle review actions\"]").getAttribute("aria-expanded")==="true"')
            cli("press", "Escape")
            self.assertTrue(evaluate(r'document.querySelector(".reviewpane").classList.contains("review-fullscreen")'))
            cli("press", "Escape")
            self.assertTrue(evaluate(r'!!document.querySelector(".reviewpane") && !document.querySelector(".reviewpane").classList.contains("review-fullscreen")'))
            more_click('button[aria-label="Toggle review file list"]')
            more_click('button[aria-label="Give working code more space"]')
            ready(r'Math.abs(document.querySelector(".cm-merge-a").clientWidth-document.querySelector(".cm-merge-b").clientWidth)<2')
            more_click('button[aria-label="Give working code more space"]')
            more_click('button[aria-label="Toggle review actions"]')
            cli("click", 'button[aria-label="Close review"]')
            evaluate(r'document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length===2')
            self.assertEqual(evaluate(r'document.querySelector("button[aria-label=\"Toggle review actions\"]").getAttribute("aria-expanded")'), "false")
            self.assertTrue(evaluate(r'document.querySelector(".reviewpane").classList.contains("review-working-focus")'))
            # A modifier shortcut enters/exits fullscreen without dispatching a task.
            evaluate(r'document.querySelector("button[aria-label=\"Toggle review fullscreen\"]").focus();document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",metaKey:true,shiftKey:true,bubbles:true,cancelable:true}))')
            self.assertTrue(evaluate(r'document.querySelector(".reviewpane").classList.contains("review-fullscreen") && !document.querySelector(".review-task")'))
            cli("press", "Escape")
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"2",code:"Digit2",metaKey:true,altKey:true,bubbles:true,cancelable:true}))')
            self.assertEqual(evaluate(r'document.querySelector("button[aria-label=\"Toggle review actions\"]").getAttribute("aria-expanded")'), "true")
            evaluate(r'[...document.querySelectorAll(".review-group")].find(x=>x.querySelector("summary").textContent.startsWith("Unversioned")).open=true')
            evaluate(r'document.querySelector("button[aria-label=\"Review added.txt\"]").scrollIntoView({block:"center"});true')
            ready(r'(()=>{let b=document.querySelector("button[aria-label=\"Review added.txt\"]"),r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()')
            cli("click", 'button[aria-label="Review added.txt"]')
            ready(r'document.querySelector(".review-detail strong").textContent==="added.txt" && !!document.querySelector(".review-additions")')
            self.assertTrue(evaluate(r'document.querySelector(".cm-merge-b").clientWidth>document.querySelector(".cm-merge-a").clientWidth*2'))
            cli("click", 'button[aria-label="Review removed.txt"]')
            ready(r'!!document.querySelector(".review-removals")')
            self.assertTrue(evaluate(r'document.querySelector(".cm-merge-a").clientWidth>document.querySelector(".cm-merge-b").clientWidth*2'))
            evaluate(r'document.querySelector("button[aria-label=\"Review code.txt\"]").scrollIntoView({block:"center"});true')
            ready(r'(()=>{let b=document.querySelector("button[aria-label=\"Review code.txt\"]"),r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()')
            cli("click", 'button[aria-label="Review code.txt"]')
            ready(r'document.querySelector(".review-detail strong").textContent==="code.txt" && document.querySelectorAll(".review-merge .cm-editor").length===2')
            cli("press", "j")
            ready(r'document.querySelector(".review-detail strong").textContent === "file00.txt" && document.querySelectorAll(".review-merge .cm-editor").length === 2')
            cli("press", "1")
            cli("press", "p")
            ready(r'document.querySelector(".review-detail strong").textContent === "code.txt" && document.querySelectorAll(".review-merge .cm-editor").length === 2')
            evaluate(r'(async()=>{let cm=await import("cm");window.reviewView=cm.EditorView.findFromDOM(document.querySelectorAll(".review-merge .cm-editor")[1]);return true})()')
            cli("press", "F7")
            self.assertEqual(evaluate(r'reviewView.state.doc.lineAt(reviewView.state.selection.main.head).number'), 9)
            cli("click", 'button[title="Next change"]')
            self.assertEqual(evaluate(r'reviewView.state.doc.lineAt(reviewView.state.selection.main.head).number'), 66)
            self.assertTrue(evaluate(r'document.activeElement.closest(".cm-editor") === reviewView.dom'))
            # agent-browser's native modified press loses the key and modifiers on macOS.
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"F7",shiftKey:true,bubbles:true,cancelable:true}))')
            self.assertEqual(evaluate(r'reviewView.state.doc.lineAt(reviewView.state.selection.main.head).number'), 9)
            cli("press", "F7")
            cli("press", "F7")
            ready(r'document.querySelector(".review-detail strong").textContent === "file00.txt" && document.querySelectorAll(".review-merge .cm-editor").length === 2')
            self.assertTrue(evaluate(r'document.activeElement.closest(".cm-editor") !== null'))
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"F7",shiftKey:true,bubbles:true,cancelable:true}))')
            ready(r'document.querySelector(".review-detail strong").textContent === "code.txt" && document.querySelectorAll(".review-merge .cm-editor").length === 2')
            cli("press", "2")
            cli("set", "viewport", "960", "640")
            cli("press", "6")
            ready(r'document.activeElement === document.querySelector("textarea[aria-label=\"Custom review instruction\"]")')
            self.assertTrue(evaluate(r'(()=>{let a=document.activeElement.getBoundingClientRect(),b=document.querySelector(".review-notes").getBoundingClientRect();return a.top>=b.top && a.bottom<=b.bottom})()'))
            self.assertFalse(evaluate(r'document.querySelector(".review-instruction-code").open'))
            for width, height in ((760, 640), (1440, 900), (960, 640)):
                cli("set", "viewport", str(width), str(height))
                evaluate(r'document.querySelector(".reviewpane").focus();document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"6",code:"Digit6",bubbles:true,cancelable:true}));true')
                ready(r'(()=>{let a=document.activeElement.getBoundingClientRect(),b=document.querySelector(".review-notes").getBoundingClientRect();return a.top>=b.top && a.bottom<=b.bottom && b.bottom<=innerHeight})()')
                self.assertTrue(evaluate(r'(()=>{let c=document.querySelector(".review-code");return c.clientHeight>50 && c.clientHeight<document.querySelector(".reviewpane").clientHeight && document.querySelector(".review-toolbar").getBoundingClientRect().top>=0 && document.querySelector(".review-footer").getBoundingClientRect().bottom<=innerHeight})()'))
                cli("press", "Escape")
                evaluate(r'window.expandedCodeBox=document.querySelector(".review-code").getBoundingClientRect().toJSON();true')
                more_click('button[aria-label="Toggle review actions"]')
                more_click('button[aria-label="Toggle review file list"]')
                ready(r'document.querySelector(".review-code").clientWidth>expandedCodeBox.width+100')
                self.assertTrue(evaluate(r'document.querySelector(".review-code").clientHeight>=expandedCodeBox.height-1'))
                more_click('button[aria-label="Toggle review file list"]')
                cli("press", "6")
                ready(r'document.activeElement === document.querySelector("textarea[aria-label=\"Custom review instruction\"]")')
            self.assertTrue(evaluate(r'(()=>{let b=document.querySelector(".review-notes").getBoundingClientRect();return [...document.querySelectorAll(".review-note-edit button")].every(x=>{let a=x.getBoundingClientRect();return a.left>=b.left && a.right<=b.right})})()'))
            cli("fill", 'textarea[aria-label="Custom review instruction"]', "Return an empty list on timeout; reuse the fake clock.")
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'[...document.querySelectorAll(".review-note")].find(x=>x.textContent.includes("code.txt:"))?.textContent.includes("Return an empty list on timeout") === true')
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"+",code:"Digit1",bubbles:true,cancelable:true}))')
            self.assertIn("Tests", evaluate(r'[...document.querySelectorAll(".review-note")].find(x=>x.textContent.includes("code.txt:")).textContent'))
            cli("set", "viewport", "1280", "720")
            cli("press", "0")
            self.assertTrue(evaluate(r'![...document.querySelectorAll(".review-note")].some(x=>x.textContent.includes("code.txt:"))'))
            cli("press", "u")
            self.assertIn("Tests", evaluate(r'[...document.querySelectorAll(".review-note")].find(x=>x.textContent.includes("code.txt:")).textContent'))
            evaluate(r'document.querySelector(".review-footer .primary").click()')
            ready(r'!!document.querySelector("textarea[aria-label=\"Generated review prompt\"]")')
            prompt = evaluate(r'document.querySelector(".review-prompt").value')
            self.assertIn("Return an empty list on timeout", prompt)
            self.assertIn("Tests (edit)", prompt)
            self.assertNotIn("reviewed line 66", prompt)
            cli("click", 'input[aria-label="Include reviewed working code"]')
            prompt = evaluate(r'document.querySelector(".review-prompt").value')
            self.assertIn("reviewed line 66", prompt)
            cli("fill", '.review-prompt', prompt + "\nUse the existing timeout helper.")
            cli("select", 'select[aria-label="Review task destination"]', "current")
            evaluate(r'document.querySelector(".review-task .primary").click()')
            ready(r'!document.querySelector(".review-task")')
            self.assertEqual(sent, [(session.id, prompt + "\nUse the existing timeout helper.")])
            evaluate(r'document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length === 2')
            evaluate(r'document.querySelector("button[aria-label=\"Review code.txt\"]").click()')
            ready(r'document.querySelector(".review-detail strong").textContent === "code.txt" && document.querySelectorAll(".review-merge .cm-editor").length === 2')
            cli("click", '.review-chunk-actions[data-chunk="0"] button[title^="Revert"]')
            ready(r'!!document.querySelector(".review-note-edit .primary")')
            cli("click", '.review-note-edit .primary')
            ready(r'!document.querySelector("button[title^=\"Undo the last\"]").disabled')
            with open(os.path.join(root, "code.txt")) as restored:
                content = restored.read()
            self.assertIn("line 9\n", content)
            self.assertNotIn("reviewed line 9", content)
            self.assertIn("reviewed line 66", content)
            more_click('button[title^="Undo the last"]')
            ready(r'document.querySelector("button[title^=\"Undo the last\"]").disabled && document.querySelectorAll(".review-merge .cm-editor").length===2 && !document.querySelector("button[title^=\"Restore the active block\"]").disabled')
            with open(os.path.join(root, "code.txt")) as restored:
                self.assertIn("reviewed line 9", restored.read())
            ready(r'!!document.querySelector(".review-chunk-actions[data-chunk=\"0\"] button[title^=\"Delete\"]:not(:disabled)")')
            cli("click", '.review-chunk-actions[data-chunk="0"] button[title^="Delete"]')
            ready(r'!!document.querySelector(".review-note-edit .primary")')
            # Preview does not mutate either the disk file or the read-only comparison.
            with open(os.path.join(root, "code.txt")) as f:
                before_delete = f.read()
            self.assertIn("reviewed line 9", before_delete)
            cli("click", '.review-note-edit .primary')
            ready(r'!document.querySelector("button[title^=\"Undo the last\"]").disabled')
            with open(os.path.join(root, "code.txt")) as f:
                after_delete = f.read()
            self.assertNotIn("reviewed line 9", after_delete)
            self.assertIn("reviewed line 66", after_delete)
            more_click('button[title^="Undo the last"]')
            ready(r'document.querySelector("button[title^=\"Undo the last\"]").disabled && document.querySelectorAll(".review-merge .cm-editor").length===2 && !document.querySelector("button[title^=\"Restore the active block\"]").disabled')
            with open(os.path.join(root, "code.txt")) as f:
                self.assertEqual(f.read(), before_delete)
            ready(r'!!document.querySelector(".review-chunk-actions[data-chunk=\"0\"] button[title^=\"Accept\"]:not(:disabled)")')
            self.assertTrue(evaluate(r'(()=>{const b=[...document.querySelectorAll(".review-chunk-actions[data-chunk=\"0\"] button")].map(x=>x.getBoundingClientRect());return b[0].right<=b[1].left && b[1].right<=b[2].left})()'))
            cli("click", '.review-chunk-actions[data-chunk="0"] button[title^="Accept"]')
            ready(r'document.querySelector(".review-banner").textContent.includes("Accepted 1/2")')
            self.assertTrue(evaluate(r'document.querySelector(".review-chunk-actions[data-chunk=\"0\"] button[title^=\"Accept\"]").getAttribute("aria-pressed")==="true"'))
            cli("press", "F7")
            ready(r'!!document.querySelector(".review-chunk-actions[data-chunk=\"1\"] button[title^=\"Accept\"]:not(:disabled)")')
            self.assertTrue(evaluate(r'(()=>{let b=document.querySelector(".review-chunk-actions[data-chunk=\"1\"] button[title^=\"Accept\"]"),r=b.getBoundingClientRect();return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===b})()'))
            cli("click", '.review-chunk-actions[data-chunk="1"] button[title^="Accept"]')
            ready(r'document.querySelector(".review-banner").textContent.includes("Accepted 2/2")')
            with open(os.path.join(root, "code.txt")) as f:
                self.assertEqual(f.read(), before_delete)
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'!document.querySelector(".reviewpane") && !!document.querySelector(".files .cm-editor")')
            line = evaluate(r'(async()=>{let cm=await import("cm"),v=cm.EditorView.findFromDOM(document.querySelector(".files .cm-editor"));return v.state.doc.lineAt(v.state.selection.main.head).number})()')
            self.assertEqual(line, 66)
            evaluate(r'document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'!!document.querySelector("button[aria-label=\"Review long.txt\"]")')
            evaluate(r'document.querySelector("button[aria-label=\"Review long.txt\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-scroller").length === 2')
            self.assertTrue(evaluate(r'[...document.querySelectorAll(".review-merge .cm-scroller")].every(x=>x.scrollHeight>x.clientHeight && x.clientHeight<=document.querySelector(".review-code").clientHeight)'))
            # Working-row selection supports guarded deletion/revert, cancellation and keyboard confirmation.
            select_rows = r'(async()=>{let cm=await import("cm"),v=cm.EditorView.findFromDOM(document.querySelectorAll(".review-merge .cm-editor")[1]);v.dispatch({selection:{anchor:v.state.doc.line(100).from+1,head:v.state.doc.line(102).to-1},effects:cm.EditorView.scrollIntoView(v.state.doc.line(100).from,{y:"center"})});v.focus();return true})()'
            evaluate(select_rows)
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Backspace",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'!!document.querySelector(".review-note-edit .primary")')
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'!document.querySelector("button[title^=\"Undo the last\"]").disabled')
            with open(os.path.join(root, "long.txt")) as f:
                self.assertEqual(f.read(), "".join("after %d\n" % n for n in range(400) if n not in (99, 100, 101)))
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"z",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'document.querySelector("button[title^=\"Undo the last\"]").disabled && document.querySelectorAll(".review-merge .cm-editor").length===2 && !document.querySelector("button[title^=\"Restore the active block\"]").disabled')
            evaluate(select_rows)
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"z",metaKey:true,altKey:true,bubbles:true,cancelable:true}))')
            ready(r'!!document.querySelector(".review-note-edit .primary")')
            cli("press", "Escape")
            self.assertTrue(evaluate(r'document.querySelector(".review-note-edit").classList.contains("hidden")'))
            with open(os.path.join(root, "long.txt")) as f:
                self.assertEqual(f.read(), "".join("after %d\n" % n for n in range(400)))
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"z",metaKey:true,altKey:true,bubbles:true,cancelable:true}))')
            ready(r'!!document.querySelector(".review-note-edit .primary")')
            cli("click", '.review-note-edit .primary')
            ready(r'!document.querySelector("button[title^=\"Undo the last\"]").disabled')
            with open(os.path.join(root, "long.txt")) as f:
                self.assertEqual(f.read(), "".join(("before" if n in (99, 100, 101) else "after") + " %d\n" % n for n in range(400)))
            cli("press", "u")
            ready(r'document.querySelector("button[title^=\"Undo the last\"]").disabled && document.querySelectorAll(".review-merge .cm-editor").length===2 && !document.querySelector("button[title^=\"Restore the active block\"]").disabled')
            # Comments retain exact selections inside a single 400-line change, including an exclusive end.
            evaluate(r'(async()=>{let cm=await import("cm");window.selectedWorking=cm.EditorView.findFromDOM(document.querySelector(".cm-merge-b"));selectedWorking.dispatch({selection:{anchor:selectedWorking.state.doc.line(100).from,head:selectedWorking.state.doc.line(103).from},effects:cm.EditorView.scrollIntoView(selectedWorking.state.doc.line(100).from,{y:"center"})});selectedWorking.focus();return true})()')
            ready(r'document.querySelector(".review-palette strong").textContent==="Working · Selection · 100–102"')
            item_key = json.dumps("piper.review.items." + session.id)
            saved_items = 'JSON.parse(localStorage.getItem(%s)).filter(x=>x.path==="long.txt")' % item_key
            ask_open()
            evaluate(r'document.querySelector(".review-palette .review-action-menu button").scrollIntoView({block:"center"});true')
            ready(r'(()=>{let b=document.querySelector(".review-palette .review-action-menu button"),r=b.getBoundingClientRect();return b.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})()')
            click_visible('.review-palette .review-action-menu button:nth-child(1)')
            ready(saved_items + '.some(x=>x.from===100 && x.to===102 && x.actions.some(a=>a.id==="tests"))')
            click_visible('.review-palette .review-action-menu button:nth-child(6)')
            ready(r'document.activeElement===document.querySelector("textarea[aria-label=\"Custom review instruction\"]")')
            self.assertIn("long.txt:100–102", evaluate(r'document.querySelector(".review-note-edit").textContent'))
            cli("fill", 'textarea[aria-label="Custom review instruction"]', "Test only these three rows.")
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",metaKey:true,bubbles:true,cancelable:true}))')
            first = evaluate(saved_items)[0]
            self.assertEqual((first["from"], first["to"], first["code"], first["old"]), (100, 102, "after 99\nafter 100\nafter 101\n", False))
            self.assertEqual({x["id"] for x in first["actions"]}, {"tests", "custom"})
            click_visible('.review-target-controls button:last-child')
            ready(r'document.querySelector(".review-palette strong").textContent.includes("Whole file")')
            click_visible('.review-target-controls button:first-child')
            ready(r'document.querySelector(".review-palette strong").textContent==="Working · Selection · 100–102"')
            # Shift + arrows create a second independent range, rather than annotating the entire block.
            click_visible('.review-target-controls button:last-child')
            ready(r'document.querySelector(".review-palette strong").textContent.includes("Whole file")')
            evaluate(r'(async()=>{let cm=await import("cm");selectedWorking.dispatch({selection:{anchor:selectedWorking.state.doc.line(201).from},effects:cm.EditorView.scrollIntoView(selectedWorking.state.doc.line(201).from,{y:"center"})});selectedWorking.focus();return true})()')
            for _ in range(2):
                evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowDown",shiftKey:true,bubbles:true,cancelable:true}))')
            ready(r'document.querySelector(".review-palette strong").textContent==="Working · Selection · 201–202"')
            cli("press", "6")
            cli("fill", 'textarea[aria-label="Custom review instruction"]', "Explain only this separate range.")
            click_visible('.review-note-edit .primary')
            self.assertEqual(len(evaluate(saved_items)), 2)
            click_visible('.review-target-controls button:first-child')
            ready(r'document.querySelector(".review-palette strong").textContent.includes("Change block")')
            # Clicking a note gutter within an original-side selection keeps its range and focus on save.
            evaluate(r'(async()=>{let cm=await import("cm");window.selectedOriginal=cm.EditorView.findFromDOM(document.querySelector(".cm-merge-a"));selectedOriginal.dispatch({selection:{anchor:selectedOriginal.state.doc.line(100).from,head:selectedOriginal.state.doc.line(103).from},effects:cm.EditorView.scrollIntoView(selectedOriginal.state.doc.line(100).from,{y:"center"})});selectedOriginal.focus();return true})()')
            ready(r'document.querySelector(".review-palette strong").textContent==="Original · Selection · 100–102"')
            ready(r'(()=>{let p=selectedOriginal.coordsAtPos(selectedOriginal.state.doc.line(100).from),g=document.querySelector(".cm-merge-a .cm-marks").getBoundingClientRect();return !!document.elementFromPoint(g.x+g.width/2,(p.top+p.bottom)/2)?.closest(".cm-marks")})()')
            evaluate(r'(()=>{let p=selectedOriginal.coordsAtPos(selectedOriginal.state.doc.line(100).from),g=document.querySelector(".cm-merge-a .cm-marks").getBoundingClientRect();document.elementFromPoint(g.x+g.width/2,(p.top+p.bottom)/2).dispatchEvent(new MouseEvent("mousedown",{bubbles:true,cancelable:true}));return true})()')
            ready(r'document.activeElement===document.querySelector("textarea[aria-label=\"Custom review instruction\"]")')
            self.assertIn("long.txt:100–102 · original", evaluate(r'document.querySelector(".review-note-edit").textContent'))
            cli("fill", 'textarea[aria-label="Custom review instruction"]', "Compare these original rows.")
            click_visible('.review-note-edit .primary')
            self.assertTrue(evaluate(r'document.activeElement.closest(".cm-editor")===selectedOriginal.dom && !selectedOriginal.state.selection.main.empty'))
            original = next(x for x in evaluate(saved_items) if x["old"])
            self.assertEqual((original["from"], original["to"], original["code"]), (100, 102, "before 99\nbefore 100\nbefore 101\n"))
            evaluate(r'document.querySelector(".review-footer .primary").click()')
            ready(r'!!document.querySelector(".review-prompt")')
            prompt = evaluate(r'document.querySelector(".review-prompt").value')
            self.assertIn("Lines 100–102", prompt)
            self.assertIn("Lines 201–202", prompt)
            self.assertIn("before 99\nbefore 100\nbefore 101", prompt)
            self.assertNotIn("after 99", prompt)
            click_visible('input[aria-label="Include reviewed working code"]')
            prompt = evaluate(r'document.querySelector(".review-prompt").value')
            self.assertIn("after 99\nafter 100\nafter 101", prompt)
            self.assertNotIn("after 102\n", prompt)
            with open(os.path.join(root, "long.txt")) as f:
                self.assertEqual(f.read(), "".join("after %d\n" % n for n in range(400)))
            evaluate(r'[...document.querySelectorAll(".review-task button")].find(x=>x.textContent==="Back to review").click()')
            ready(r'!!document.querySelector("button[aria-label=\"Review long.txt\"]")')
            evaluate(r'document.querySelector("button[aria-label=\"Review long.txt\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-scroller").length===2')
            for side in ("a", "b"):
                selector = ".review-merge .cm-merge-%s .cm-scroller" % side
                cli("click", selector)
                cli("scroll", "down", "500", "--selector", selector)
                ready('document.querySelector(%s).scrollTop > 0' % json.dumps(selector))
                ready(r'(()=>{let a=[...document.querySelectorAll(".review-merge .cm-scroller")];return Math.abs(a[0].scrollTop-a[1].scrollTop)<2})()')
            ready(r'!!document.querySelector("button[aria-label=\"Review file29.txt\"]")')
            evaluate(r'document.querySelector("button[aria-label=\"Review file29.txt\"]").click()')
            self.assertTrue(evaluate(r'(()=>{let a=document.querySelector(".review-file.active").getBoundingClientRect(),b=document.querySelector(".review-list").getBoundingClientRect();return a.top>=b.top && a.bottom<=b.bottom})()'))
            cli("click", 'button[aria-label="Close review"]')
            ready(r'!document.querySelector(".reviewpane") && !document.querySelector("#overlay.open")')
            evaluate(r'document.querySelector("button[data-git-path=\"code.txt\"]").dispatchEvent(new MouseEvent("click",{metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'!document.querySelector(".reviewpane") && !!document.querySelector(".files .cm-editor")')
            evaluate(r'document.body.dispatchEvent(new KeyboardEvent("keydown",{key:"d",metaKey:true,bubbles:true,cancelable:true}))')
            ready(r'!!document.querySelector(".files .cm-deletedChunk")')
            cli("press", "F7")
            cli("press", "F7")
            cli("press", "F7")
            ready(r'document.querySelector(".ftab.on")?.textContent.includes("file00.txt") && !!document.querySelector(".files .cm-deletedChunk")')
            evaluate(r'document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"F7",shiftKey:true,bubbles:true,cancelable:true}))')
            ready(r'document.querySelector(".ftab.on")?.textContent.includes("code.txt") && !!document.querySelector(".files .cm-deletedChunk")')
            line = evaluate(r'(async()=>{let cm=await import("cm"),v=cm.EditorView.findFromDOM(document.activeElement.closest(".cm-editor"));return v.state.doc.lineAt(v.state.selection.main.head).number})()')
            self.assertEqual(line, 66)
            # A server without the new config route still shows code; a failed file load has Retry.
            evaluate(r'window.realReviewFetch=window.fetch;window.fetch=(u,o)=>String(u).includes("/review/config")?Promise.resolve(new Response(JSON.stringify({error:"old backend"}),{status:404})):realReviewFetch(u,o)')
            evaluate(r'document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length===2')
            self.assertIn("Review actions unavailable", evaluate(r'document.querySelector(".review-banner").textContent'))
            cli("click", 'button[aria-label="Close review"]')
            evaluate(r'window.fetch=(u,o)=>String(u).includes("/review?")?Promise.resolve(new Response(JSON.stringify({error:"comparison unavailable"}),{status:503})):realReviewFetch(u,o)')
            evaluate(r'document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelector(".review-code [role=alert]")?.textContent.includes("comparison unavailable")')
            evaluate(r'window.fetch=realReviewFetch;document.querySelector(".review-code [role=alert] button").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length===2')
            # Rapid keys cross files in sidebar order, keep focus, and visibly mark the destination block.
            ready(r'!document.querySelector("select[aria-label=\"Review scope\"]").disabled')
            evaluate(r'document.querySelector("button[aria-label=\"Review file25.txt\"]").click()')
            ready(r'document.querySelector(".review-detail strong").textContent==="file25.txt" && document.querySelectorAll(".review-merge .cm-editor").length===2')
            expected = evaluate(r'(()=>{let paths=[...document.querySelectorAll(".review-file-name:not(:disabled)")].map(x=>x.getAttribute("aria-label").slice(7));return paths[paths.indexOf("file25.txt")+3]})()')
            evaluate(r'(()=>{let root=document.querySelector(".reviewpane");for(let i=0;i<3;i++)root.dispatchEvent(new KeyboardEvent("keydown",{key:"F7",bubbles:true,cancelable:true}));return true})()')
            ready('document.querySelector(".review-detail strong").textContent===%s && document.querySelectorAll(".review-merge .cm-editor").length===2' % json.dumps(expected))
            ready(r'!!document.querySelector(".review-merge .cm-merge-b .cm-review-active-chunk") && document.activeElement.closest(".cm-editor")===document.querySelector(".review-merge .cm-merge-b")')
            evaluate(r'(()=>{let root=document.querySelector(".reviewpane");for(let i=0;i<3;i++)root.dispatchEvent(new KeyboardEvent("keydown",{key:"F7",shiftKey:true,bubbles:true,cancelable:true}));return true})()')
            ready(r'document.querySelector(".review-detail strong").textContent==="file25.txt" && !!document.querySelector(".review-merge .cm-merge-b .cm-review-active-chunk")')
            # Clear all includes custom text and resolved items, removes cached prompts, stays session-scoped.
            evaluate(r'window.fetch=reviewBaseFetch;localStorage.setItem("piper.review.items.other-session","[{}]");document.querySelector("button[title=\"Review the whole session\"]").click()')
            ready(r'document.querySelectorAll(".review-merge .cm-editor").length===2')
            ready(r'!!document.querySelector(".reviewpane button[aria-label=\"Clear all review items\"]")')
            if evaluate(r'document.querySelector("button[aria-label=\"Toggle review actions\"]").getAttribute("aria-expanded")') == "false":
                more_click('button[aria-label="Toggle review actions"]')
            click_visible('.reviewpane button[aria-label="Clear all review items"]')
            self.assertEqual(evaluate(r'JSON.parse(localStorage.getItem("piper.review.items.%s")).length' % session.id), 0)
            self.assertEqual(evaluate(r'localStorage.getItem("piper.review.items.other-session")'), "[{}]")
            self.assertIsNone(evaluate(r'localStorage.getItem("piper.review.prompt.%s")' % session.id))
            cli("press", "u")
            ready(r'!!document.querySelector("button[aria-label=\"Clear all review items\"]")')
            cli("click", 'button[aria-label="Close review"]')
            ready(r'!document.querySelector(".reviewpane")')
            click_visible('button[aria-label="Clear all review items"]')
            self.assertEqual(evaluate(r'JSON.parse(localStorage.getItem("piper.review.items.%s")).length' % session.id), 0)
            cli("reload")
            ready(r'!!document.querySelector("button[aria-label=\"context panel\"]")')
            if not evaluate(r'!!document.querySelector("button[title=\"Review the whole session\"]")'):
                evaluate(r'document.querySelector("button[aria-label=\"context panel\"]").click()')
            ready(r'!!document.querySelector("button[title=\"Review the whole session\"]")')
            self.assertFalse(evaluate(r'!!document.querySelector("button[aria-label=\"Clear all review items\"]")'))
        finally:
            subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=20)
            server.shutdown()
            server.server_close()
            piperd.MANAGER, piperd.PORT, self.m.sessions = old_manager, old_port, old_sessions
            self.m.send = old_send

    def _sha(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_write_lock_and_refusals(self):
        p = os.path.join(self.root, "src/A.java")
        os.chmod(p, 0o750)
        sha = self._sha("src/A.java")
        with self.assertRaisesRegex(ValueError, "changed since you opened it"):
            self.m.fs_write(self.key, "src/A.java", "x\n", "0" * 64)
        # review 2026-10-05: an unknown project is a 404 (KeyError), a bad sha says what a sha is
        with self.assertRaises(KeyError):
            self.m.fs_write("nope/nope", "src/A.java", "x\n", sha)
        with self.assertRaisesRegex(ValueError, "64 hex digits"):
            self.m.fs_write(self.key, "src/A.java", "x\n", "abc")
        # while the session works (2026-10-07): saved - the sha lock is what protects Claude's edits
        w = self._session(state="working", coverage=[{"root": self.root, "overmounts": ["notes.txt"],
                                                      "ids": guard.overmount_ids(self.root, ["notes.txt"])}])
        r = self.m.fs_write(self.key, "src/A.java", "class A { int mine; }", sha)
        self.assertEqual(common.audit_tail(1)[0]["while_working"], [w.id])
        write(self.root, "src/A.java", "class A { int claude; }\n")              # Claude edits it next
        with self.assertRaisesRegex(ValueError, "changed since you opened it"):
            self.m.fs_write(self.key, "src/A.java", "class A { int mine2; }", r["sha"])   # never over its edit
        with open(p) as f:
            self.assertEqual(f.read(), "class A { int claude; }\n")
        w.state = "waiting"
        os.chmod(p, 0o750)
        r = self.m.fs_write(self.key, "src/A.java", "class A {}\nint x;", self._sha("src/A.java"))   # lines joined by \n
        self.assertEqual(r, {"sha": hashlib.sha256(b"class A {}\nint x;\n").hexdigest(), "total_lines": 2})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o750)                 # the mode is kept
        with open(p) as f:
            self.assertEqual(f.read(), "class A {}\nint x;\n")
        self.assertFalse([n for n in os.listdir(os.path.dirname(p)) if n.startswith(".piperd-")])
        ev = common.audit_tail(5)
        self.assertEqual({k: ev[0][k] for k in ("event", "project", "path", "bytes")},
                         {"event": "gui-write", "project": self.key, "path": "src/A.java", "bytes": 18})
        os.chmod(p, 0o644)
        # a file hidden from Claude is saved like any other (2026-10-07), audited as hidden
        self.m.fs_write(self.key, "notes.txt", "private notes v2", self._sha("notes.txt"))
        with open(os.path.join(self.root, "notes.txt")) as f:
            self.assertEqual(f.read(), "private notes v2\n")
        self.assertTrue(common.audit_tail(1)[0]["hidden"])
        # .git, outside, missing, a symlink, a symlinked directory, a directory, too large
        for bad in (".git/config", "../x"):
            with self.assertRaises(projectfs.FsError, msg=bad):
                self.m.fs_write(self.key, bad, "x", self._sha("README.md"))
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.m.fs_write(self.key, "src/Missing.java", "x", "0" * 64)
        os.symlink("../README.md", os.path.join(self.root, "src/link.md"))
        os.symlink("../src", os.path.join(self.root, "img/srclink"))
        self.addCleanup(os.unlink, os.path.join(self.root, "img/srclink"))
        with self.assertRaises((ValueError, OSError)):
            self.m.fs_write(self.key, "src/link.md", "x", self._sha("README.md"))
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.m.fs_write(self.key, "img/srclink/A.java", "x", self._sha("src/A.java"))
        with self.assertRaises(OSError):
            self.m.fs_write(self.key, "src", "x", "0" * 64)
        os.symlink("/etc/hosts", os.path.join(self.root, "img/out.png"))   # a link leaving the project
        self.addCleanup(os.unlink, os.path.join(self.root, "img/out.png"))
        for fn in (lambda: self.m.fs_write(self.key, "img/out.png", "x", "0" * 64),
                   lambda: self.m.fs_raw(self.key, "img/out.png"), lambda: self.m.fs_marks(self.key, "img/out.png")):
            with self.assertRaises(projectfs.FsError):
                fn()
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            self.m.fs_write(self.key, "README.md", "x" * (3 << 20), self._sha("README.md"))
        with self.assertRaises(ValueError):
            self.m.fs_write(self.key, "README.md", "x", "not-a-sha")
        with open(os.path.join(self.root, "README.md")) as f:
            self.assertEqual(f.read(), "# Readme\n")

    def test_line_endings_are_read_and_kept(self):
        write(self.root, "src/crlf.txt", b"a\r\nb\x0cform\r\nc", "wb")        # CRLF, no final newline, a \f
        write(self.root, "src/lf.txt", b"x\ny\n\n", "wb")                      # LF, final newline, blank last line
        r = self.m.fs(self.key, "read", {"path": "src/crlf.txt"})
        self.assertEqual((r["lines"], r["eol"], r["final_newline"], r["total_lines"]),
                         (["1\ta", "2\tb\x0cform", "3\tc"], "\r\n", False, 3))
        w = self.m.fs_write(self.key, "src/crlf.txt", "a\nB\nc", r["sha"])
        with open(os.path.join(self.root, "src/crlf.txt"), "rb") as f:
            self.assertEqual(f.read(), b"a\r\nB\r\nc")
        self.assertEqual((w["sha"], w["total_lines"]), (self._sha("src/crlf.txt"), 3))
        r = self.m.fs(self.key, "read", {"path": "src/lf.txt"})
        self.assertEqual((r["lines"], r["eol"], r["final_newline"]), (["1\tx", "2\ty", "3\t"], "\n", True))
        text = "\n".join(l.split("\t", 1)[1] for l in r["lines"])            # what the editor holds
        self.m.fs_write(self.key, "src/lf.txt", text, r["sha"])
        with open(os.path.join(self.root, "src/lf.txt"), "rb") as f:
            self.assertEqual(f.read(), b"x\ny\n\n")                           # a round trip keeps the bytes
        self.assertEqual(self.m.fs(self.key, "read", {"path": "README.md"})["eol"], "\n")
        self.assertEqual(projectfs.split_lines("a\u2028b\x1cc\n"), ["a\u2028b\x1cc"])
        self.assertEqual(projectfs.eol_info(b""), ("\n", False))

    def test_a_hidden_file_is_written_in_place(self):
        """A file hidden from Claude: no temporary file next to it (a sandbox may see its directory - the
        exclude is a FILE) and no rename over it (on Linux a rename over a path another mount namespace
        over-mounts drops that over-mount): its inode is truncated and written."""
        seen = []
        real = common._write_all

        def spy(fd, data):
            seen.append(sorted(n for d in (self.root, os.path.join(self.root, "config/secrets"))
                               for n in os.listdir(d) if n.startswith(".piperd-")))
            real(fd, data)
        ino = lambda rel: os.stat(os.path.join(self.root, rel)).st_ino   # noqa: E731
        before = {r: ino(r) for r in ("README.md", "notes.txt")}
        with mock.patch.object(common, "_write_all", spy):
            self.m.fs_write(self.key, "README.md", "# Readme v2", self._sha("README.md"))   # visible: as before
            self.assertEqual(len(seen[-1]), 1)                                            # (the spy sees a tmp)
            self.m.fs_write(self.key, "notes.txt", "private notes v3", self._sha("notes.txt"))
            self.assertEqual(seen[-1], [])
            self.m.fs_create(self.key, "config/secrets/new.properties", "file", "k=v")
            self.assertEqual(seen[-1], [])
        self.addCleanup(os.unlink, os.path.join(self.root, "config/secrets/new.properties"))
        self.assertNotEqual(ino("README.md"), before["README.md"])     # atomic replace
        self.assertEqual(ino("notes.txt"), before["notes.txt"])         # the same inode: an over-mount stays
        with open(os.path.join(self.root, "notes.txt")) as f:
            self.assertEqual(f.read(), "private notes v3\n")
        with open(os.path.join(self.root, "config/secrets/new.properties")) as f:
            self.assertEqual(f.read(), "k=v")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.fs_create(self.key, "config/secrets/new.properties", "file", "other")
        with open(os.path.join(self.root, "config/secrets/new.properties")) as f:
            self.assertEqual(f.read(), "k=v")                             # never replaced

    def test_raw_images(self):
        self.assertEqual(self.m.fs_raw(self.key, "img/logo.png"), ("image/png", PNG))
        self.assertEqual(self.m.fs_raw(self.key, "img/icon.svg")[0], "image/svg+xml")
        with self.assertRaisesRegex(ValueError, "not an image"):
            self.m.fs_raw(self.key, "README.md")
        self.assertEqual(self.m.fs_raw(self.key, "config/secrets/qr.png"), ("image/png", PNG))   # hidden from Claude only
        os.symlink("../.git/HEAD", os.path.join(self.root, "img/leak.png"))
        self.addCleanup(os.unlink, os.path.join(self.root, "img/leak.png"))
        with self.assertRaises(projectfs.FsError):
            self.m.fs_raw(self.key, "img/leak.png")

    # ------------------------------------------------------------ git history
    def test_file_diff_log_blame_graph(self):
        write(self.root, "src/A.java", "class A {\n  int a;\n  int bb;\n  int c;\n  int NEW;\n}\n")
        d = self.m.git_file_diff(self.key, "src/A.java")
        self.assertIn("+  int NEW;", d)
        write(self.root, "src/U.java", "untracked\n")
        self.assertIn("new file mode", self.m.git_file_diff(self.key, "src/U.java"))
        write(self.root, "notes.txt", "private notes NOW\n")   # hidden from Claude: the GUI's anyway (2026-10-07)
        self.assertIn("+private notes NOW", self.m.git_file_diff(self.key, "notes.txt"))
        self.assertEqual(len(self.m.git_log(self.key, "config/secrets/db.properties")["commits"]), 2)
        self.assertEqual(self.m.git_blame(self.key, "config/secrets/db.properties")["lines"][0]["text"],
                         "db.password=SECRET2")
        for bad in (".git/HEAD", "../wb97/notes.txt"):
            with self.assertRaises(projectfs.FsError):
                self.m.git_file_diff(self.key, bad)
            with self.assertRaises(projectfs.FsError):
                self.m.git_log(self.key, bad)
            with self.assertRaises(projectfs.FsError):
                self.m.git_blame(self.key, bad)
        log = self.m.git_log(self.key)["commits"]
        self.assertEqual([c["subject"] for c in log], ["change A and the secrets", "init"])
        self.assertEqual((log[0]["hash"], log[0]["author"], log[0]["refs"]), (self.c2, "Tester", "HEAD -> main"))
        self.assertEqual(len(self.m.git_log(self.key, "README.md")["commits"]), 1)
        self.assertEqual(len(self.m.git_log(self.key, "", 1)["commits"]), 1)
        b = self.m.git_blame(self.key, "src/A.java")["lines"]
        self.assertEqual([x["commit"] == "" for x in b], [False, False, False, False, True, False])
        self.assertEqual((b[2]["commit"], b[2]["summary"], b[2]["author"], b[2]["text"]),
                         (self.c2[:8], "change A and the secrets", "Tester", "  int bb;"))
        self.assertGreater(b[0]["time"], 1_600_000_000)
        self.assertEqual({x["commit"] for x in self.m.git_blame(self.key, "src/U.java")["lines"]}, {""})
        g = self.m.git_graph(self.key)["lines"]
        self.assertTrue(g[0].startswith("* ") and "change A" in g[0])

    def test_a_file_named_dotdot_something_is_in_the_repository(self):
        write(self.root, "..notes.md", "x\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "..notes.md"))
        self.assertIn("new file mode", self.m.git_file_diff(self.key, "..notes.md"))

    def test_show_includes_files_hidden_from_claude(self):
        out = self.m.git_show(self.key, self.c2[:10])
        self.assertIn("+  int bb;", out)
        self.assertIn("change A and the secrets", out)
        for secret in ("+db.password=SECRET2", "+private notes TOPSECRET"):   # the GUI's view (2026-10-07)
            self.assertIn(secret, out)
        self.assertNotIn("withheld", out)
        blob = git(self.root, "rev-parse", "HEAD:config/secrets/db.properties").strip()
        tree = git(self.root, "rev-parse", "HEAD^{tree}").strip()
        for bad in (blob, tree, "HEAD", "--output=/tmp/x", "zz", "abc"):
            with self.assertRaises(gitops.GitError, msg=bad):
                self.m.git_show(self.key, bad)

    def test_session_git_panel_shows_hidden_diffs_the_session_tool_does_not(self):
        """2026-10-07: the GUI's commit panel marks a file hidden from Claude (`sensitive`) and shows
        its diff; the session's own git_diff tool still withholds it."""
        s = self._session(kind="project", excludes=["notes.txt"])   # what its preflight pinned
        write(self.root, "notes.txt", "private notes PANEL\n")
        st = {f["path"]: f for f in self.m.git_status(s.id)["files"]}
        self.assertTrue(st["notes.txt"]["sensitive"])
        self.assertIn("+private notes PANEL", self.m.git_diff(s.id, "notes.txt", False))
        self.assertIn("+private notes PANEL", self.m.git_diff(s.id, None, False))
        tool = self.m.tool(s, "git_diff", {})
        self.assertNotIn("PANEL", json.dumps(tool))
        self.assertNotIn("notes.txt", [f["path"] for f in self.m.tool(s, "git_status", {})["files"]])

    def test_strict_filter_fails_closed(self):
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        text = ('commit x\n\ndiff --git a/src/A b/src/A\n+ok\n'
                'diff --git "a/config/x\\tq" "b/config/x\\tq"\n+SECRET\n'
                'diff --git a/a b/c d/e\n+ODD\n'
                'diff --cc src/merge\n+COMBINED\n'
                'diff --cc config/merge\n+HIDDENCC\n'
                'diff --git "a/src/\\303\\241.txt" "b/src/\\303\\241.txt"\n+utf\n')
        out = gitops.strict_filter(text, hidden)
        self.assertIn("+ok", out)
        self.assertIn("+utf", out)
        self.assertIn("+COMBINED", out)   # a combined diff (merge) names one path: filtered by it
        for s in ("SECRET", "ODD", "HIDDENCC"):
            self.assertNotIn(s, out)
        self.assertEqual(gitops.header_paths('diff --git "a/src/\\303\\241.txt" "b/src/\\303\\241.txt"'),
                         ["src/á.txt", "src/á.txt"])
        self.assertEqual(gitops.header_paths("diff --git a/x y/z b/x y/z"), ["x y/z"])

    # ------------------------------------------------------------ turn snapshots
    def _repo_state(self):
        with open(os.path.join(self.root, ".git", "index"), "rb") as f:
            idx = f.read()
        return idx, git(self.root, "for-each-ref"), git(self.root, "rev-parse", "HEAD"), \
            git(self.root, "symbolic-ref", "HEAD"), sorted(os.listdir(os.path.join(self.root, ".git", "refs", "heads")))

    def test_turn_snapshots_never_touch_index_head_or_refs(self):
        s = self._session(state="working")
        before = self._repo_state()
        now = time.time() - 5   # hook stamps are clamped to "now"
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now, "prompt": "fix it, password=abc"})
        self.assertTrue(wait(lambda: len(s.turns) == 1 and not s.turn_busy))
        write(self.root, "src/A.java", "class A {\n  int TURN;\n}\n")
        write(self.root, "src/T.java", "new in turn\n")
        write(self.root, "notes.txt", "private notes TURNSECRET\n")
        write(self.root, "config/secrets/db.properties", "db.password=TURNDB\n")
        self.m._turn_event(s, "Stop", {"ts": now + 1})
        self.assertTrue(wait(lambda: s.turns[-1].get("to") and not s.turn_busy))
        self.assertEqual(self._repo_state(), before)                       # index, refs, HEAD unchanged
        # the sandbox reads .git (git open, 2026-10-01): no snapshot object may land there
        repo_objs = git(self.root, "cat-file", "--batch-all-objects", "--batch-check")
        secret = git(self.root, "hash-object", "config/secrets/db.properties").strip()
        self.assertNotIn(secret, repo_objs)
        self.assertNotIn(s.turns[-1]["to"], repo_objs)
        self.assertTrue(os.listdir(os.path.join(s.dir, "snap-objects")))
        t = s.turns[-1]
        self.assertEqual((t["n"], t["prompt"], t["started"], t["ended"]), (1, "fix it, password=[scrubbed]", now, now + 1))
        files = {f["path"]: f for f in self.m.turn_files(s.id, t["from"], t["to"])["files"]}
        self.assertEqual({k: (v["status"], v["sensitive"]) for k, v in files.items()},
                         {"src/A.java": ("M", False), "src/T.java": ("A", False), "notes.txt": ("M", True),
                          "config/secrets/db.properties": ("M", True)})
        d = self.m.turn_diff(s.id, t["from"], t["to"], ".")   # files hidden from Claude: shown, marked above
        self.assertIn("+  int TURN;", d)
        self.assertIn("+private notes TURNSECRET", d)
        self.assertIn("+db.password=TURNDB", d)
        self.assertNotIn("withheld", d)
        self.assertIn("+new in turn", self.m.turn_diff(s.id, t["from"], t["to"], "src/T.java"))
        self.assertIn("TURNSECRET", self.m.turn_diff(s.id, t["from"], t["to"], "notes.txt"))
        with self.assertRaises(projectfs.FsError):
            self.m.turn_diff(s.id, t["from"], t["to"], ".git/HEAD")
        with self.assertRaises(ValueError):
            self.m.turn_diff(s.id, t["from"], t["to"], "../x")
        # "now": a fresh snapshot, not stored
        write(self.root, "src/T.java", "changed after the turn\n")
        now_files = self.m.turn_files(s.id, t["to"], "")
        self.assertEqual([f["path"] for f in now_files["files"]], ["src/T.java"])
        self.assertEqual(len(s.turns), 1)
        self.assertEqual(self._repo_state(), before)
        # only snapshot ids of this session's turns, and only trees
        head_tree = git(self.root, "rev-parse", "HEAD~1^{tree}").strip()   # a tree, but no turn of this session
        for bad in (head_tree, self.c2, "0" * 40, "x"):
            with self.assertRaises(ValueError, msg=bad):
                self.m.turn_files(s.id, bad, t["to"])
        # persisted (meta.json), but not in /api/state
        with open(os.path.join(s.dir, "meta.json")) as f:
            self.assertEqual(json.load(f)["turns"][0]["from"], t["from"])
        self.assertNotIn("turns", [x for x in self.m.list() if x["id"] == s.id][0])
        self.assertEqual(self.m.turns(s.id)["turns"][0]["n"], 1)

    def test_turn_event_rules(self):
        s = self._session(state="working")
        self.m._turn_event(s, "UserPromptSubmit", {"ts": time.time() - 600})   # a replay after a restart
        self.m._turn_event(s, "Stop", {"ts": time.time()})                     # no open turn: nothing
        time.sleep(0.5)
        self.assertEqual(s.turns, [])
        s.turns = [{"n": i, "prompt": "", "started": 1.0 + i, "ended": 2.0 + i, "from": "a" * 40, "to": "b" * 40}
                   for i in range(1, 51)]
        ts = time.time()
        self.m._turn_event(s, "UserPromptSubmit", {"ts": ts, "prompt": "p1"})
        self.m._turn_event(s, "UserPromptSubmit", {"ts": ts - 1, "prompt": "older: ignored"})
        self.m._turn_event(s, "UserPromptSubmit", {"ts": ts + 1, "prompt": "p2"})   # closes p1
        self.assertTrue(wait(lambda: s.turns[-1]["prompt"] == "p2" and not s.turn_busy))
        self.assertEqual(len(s.turns), 52)   # Review retains all turns since session start
        self.assertEqual([t["n"] for t in s.turns[-2:]], [51, 52])
        self.assertEqual(s.turns[-2]["to"], s.turns[-1]["from"])
        self.assertIsNone(s.turns[-1]["to"])
        # a future stamp from the (hostile) hook file must not block every later turn
        self.m._turn_event(s, "Stop", {"ts": time.time() + 10 ** 9})
        self.assertTrue(wait(lambda: s.turns[-1]["to"] and not s.turn_busy))
        self.assertLessEqual(s.turns[-1]["ended"], time.time())
        self.m._turn_event(s, "UserPromptSubmit", {"ts": time.time() + 1, "prompt": "p3"})
        self.assertTrue(wait(lambda: s.turns[-1]["prompt"] == "p3" and not s.turn_busy))
        self.assertLessEqual(s.turns[-1]["started"], time.time())
        # a bidi control in a prompt (automation text from GitLab) must not reorder the Last turn line (E25)
        self.m._turn_event(s, "UserPromptSubmit", {"ts": time.time() + 2, "prompt": "fix \u202egnp.exe\u202c now"})
        self.assertTrue(wait(lambda: s.turns[-1]["prompt"] == "fix gnp.exe now" and not s.turn_busy))
        adv = sessions.Session(kind="advisor", project="advisor", path=common.ADVISOR_DIR)
        self.m.sessions[adv.id] = adv
        self.assertEqual(self.m.turns(adv.id)["turns"], [])
        self.assertEqual(self.m.turns(adv.id)["note"], "The Global agent has no working tree")
        # GUI pass D: the morning production check said "Global agent has no working tree"
        prod = sessions.Session(kind="prodcheck", project="prodcheck", path=common.ADVISOR_DIR)
        self.m.sessions[prod.id] = prod
        self.assertEqual(self.m.turns(prod.id), {"turns": [], "note": "The production check has no working tree"})
        # B-N3-3: a session without a snapshot yet - the toast speaks of prompts and turns, not hook event names
        fresh = sessions.Session(kind="project", project=s.project, path=s.path)
        self.m.sessions[fresh.id] = fresh
        note = self.m.turns(fresh.id)["note"]
        self.assertIn("A snapshot is taken", note)   # gitpanel.ts hides the note when it starts so
        for hook in ("UserPromptSubmit", "(Stop)"):
            self.assertNotIn(hook, note)

    def test_hook_keeps_a_bounded_prompt(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper", "sandbox",
                              "piper-event.py")
        out = os.path.join(tempfile.mkdtemp(prefix="piper-ev-"), "ev.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)
        subprocess.run([sys.executable, script, "UserPromptSubmit", out], check=True,
                       input=json.dumps({"prompt": "x" * 1000, "session_id": "s", "other": "dropped"}).encode())
        with open(out) as f:
            rec = json.loads(f.read())
        self.assertEqual((rec["ev"], len(rec["prompt"]), rec["session_id"]), ("UserPromptSubmit", 300, "s"))
        self.assertNotIn("other", rec)

    def test_pre_tool_use_hook_asks_before_prune_class_git_however_spelled(self):
        """review 2026-10-05 R5: the permissions.ask prefix rules miss `git -C x gc` & co."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper", "sandbox",
                              "piper-event.py")

        def decide(cmd):
            r = subprocess.run([sys.executable, script, "PreToolUse"], check=True, capture_output=True,
                               input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}).encode())
            return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None
        for cmd in ("git gc", "git -C ../x gc --prune=now", "cd a && git repack -a -d", "sh -c 'git prune; true'",
                    "env A=1 /usr/bin/git worktree remove ../w", "git --git-dir=/x worktree prune", "y=$(git prune)"):
            self.assertEqual(decide(cmd), "ask", cmd)
        for cmd in ("git status", "git log --grep gc", "git commit -m 'prune old'", "git worktree list", "ls"):
            self.assertIsNone(decide(cmd), cmd)

    def test_events_file_triggers_a_snapshot(self):
        s = self._session(state="waiting")
        os.makedirs(s.box, exist_ok=True)
        with open(os.path.join(s.box, "piper-events.jsonl"), "w") as f:
            f.write(json.dumps({"ev": "UserPromptSubmit", "ts": time.time(), "prompt": "from the hook"}) + "\n")
        self.m._events(s)
        self.assertEqual(s.state, "working")
        self.assertTrue(wait(lambda: s.turns and s.turns[0]["prompt"] == "from the hook"))

    # ------------------------------------------------------------ CI + MR
    def _origin(self):
        bare = tempfile.mkdtemp(prefix="piper-origin-")
        self.addCleanup(shutil.rmtree, bare, True)
        git(bare, "init", "-q", "--bare", "-b", "main")
        git(self.root, "remote", "add", "origin", bare)
        self.addCleanup(git, self.root, "remote", "remove", "origin")
        git(self.root, "push", "-q", "origin", "main")
        git(self.root, "checkout", "-q", "-b", "feature/x")
        self.addCleanup(git, self.root, "branch", "-q", "-D", "feature/x")
        self.addCleanup(git, self.root, "checkout", "-q", "main")
        write(self.root, "README.md", "# Readme\none\n")
        git(self.root, "commit", "-q", "-am", "First change")
        write(self.root, "README.md", "# Readme\none\ntwo\n")
        git(self.root, "commit", "-q", "-am", "Second change")

    def test_disabled_forges_ignore_cached_ci_and_do_not_contact_the_host(self):
        import features
        from unittest import mock
        _env.set_modules(self, gitlab=False, github=False)
        self.m._ci_cache[self.key] = (time.time(), {"pipeline": {"status": "failed"}}, (True, False))
        with mock.patch.object(self.m, "_gl_projects") as gl, mock.patch.object(self.m, "_gh_projects") as gh:
            result = self.m.git_ci(self.key)
        gl.assert_not_called()
        gh.assert_not_called()
        self.assertIsNone(result["pipeline"])
        self.assertNotIn(self.key, self.m._ci_cache)
        with mock.patch.object(self.m, "_gl_projects") as gl:
            self.assertEqual(self.m._gl_pick({"path": self.root}), (None, []))
        gl.assert_not_called()
        for call in (lambda: self.m.issues(self.key, gl_project="grp/app"),
                     lambda: self.m.issue(self.key, 1, gl_project="grp/app")):
            with self.assertRaisesRegex(ValueError, "GitLab module is off"):
                call()

    def test_gitlab_off_preserves_github_ci_and_ignores_old_gitlab_cache(self):
        import github
        _env.set_modules(self, gitlab=False, github=True)
        self.m._ci_cache[self.key] = (time.time(), {"pipeline": {"status": "failed"}}, (True, False))
        branch = git(self.root, "branch", "--show-current").strip()
        client = mock.Mock()
        pipeline = {"id": 9, "status": "success"}
        with mock.patch.object(self.m, "_gh_pick", return_value=(client, "team/app", ["team/app"])), \
                mock.patch.object(self.m, "_gl_projects") as gl, \
                mock.patch.object(github, "latest_pipeline", return_value=pipeline) as fetch, \
                mock.patch.object(github, "open_mr", return_value=None):
            result = self.m.git_ci(self.key)
        gl.assert_not_called()
        fetch.assert_called_once_with(client, "team/app", branch, ["team/app"])
        self.assertEqual((result["github"], result["pipeline"]), ("team/app", pipeline))

    def test_ci_mr_draft_and_create(self):
        base = "/api/v4/projects/grp%2Fapp"
        created = {}

        def post_mr(req):
            created.update(json.loads(req.data))
            return {"iid": 9, "title": created["title"], "state": "opened", "web_url": "https://gl/mr/9",
                    "source_branch": created["source_branch"], "target_branch": created["target_branch"], "x": 1}
        gl = FakeHttp({("GET", base + "/pipelines"): [{"id": 5, "status": "failed", "ref": "feature/x", "sha": "s",
                                                       "web_url": "https://gl/p/5", "updated_at": "u", "user": {}}],
                       ("GET", base + "/merge_requests"): [],
                       ("GET", base): {"default_branch": "main"},
                       ("POST", base + "/merge_requests"): post_mr})
        self.m.gitlab_opener = gl
        self.addCleanup(setattr, self.m, "gitlab_opener", None)
        self._origin()
        with mock.patch.object(gitlab, "token", return_value="tok"):
            self.m._ci_cache.clear()
            ci = self.m.git_ci(self.key)
            self.assertEqual(ci["pipeline"], {"id": 5, "status": "failed", "ref": "feature/x", "sha": "s",
                                              "web_url": "https://gl/p/5", "updated_at": "u"})
            self.assertEqual((ci["project"], ci["branch"], ci["mr"], ci["gitlab"]), (self.key, "feature/x", None, "grp/app"))
            self.assertEqual([c[2].get("ref") for c in gl.calls if c[1].endswith("/pipelines")], ["feature/x"])
            n = len(gl.calls)
            self.m.git_ci(self.key)
            self.assertEqual(len(gl.calls), n)                              # cached 30 s
            d = self.m.mr_draft(self.key)
            self.assertEqual(d, {"project": self.key, "source": "feature/x", "target": "main", "title": "First change",
                                 "description": "- First change\n- Second change"})
            with self.assertRaises(gitops.GitError):
                self.m.mr_draft(self.key, "--upload-pack=evil")
            with self.assertRaisesRegex(ValueError, "push first"):
                self.m.create_mr(self.key, "T", "D", "main", True)
            git(self.root, "push", "-q", "-u", "origin", "feature/x")
            mr = self.m.create_mr(self.key, "Add things", "- a", "main", True)
            self.assertEqual(mr, {"iid": 9, "title": "Draft: Add things", "state": "opened", "web_url": "https://gl/mr/9",
                                  "source_branch": "feature/x", "target_branch": "main"})
            self.assertEqual(created["description"], "- a")
            self.assertEqual(common.audit_tail(1)[0]["event"], "gui-mr")
            write(self.root, "README.md", "three\n")
            git(self.root, "commit", "-q", "-am", "Third")
            with self.assertRaisesRegex(ValueError, "not pushed"):
                self.m.create_mr(self.key, "T", "", "main", False)
        with mock.patch.object(gitlab, "token", return_value=None):
            self.m._ci_cache.clear()
            ci = self.m.git_ci(self.key)
            self.assertEqual((ci["pipeline"], ci["mr"]), (None, None))
            self.assertIn("token", ci["note"])
            self.assertEqual(self.m.mr_draft(self.key)["target"], "main")   # fallback: origin/main exists
        self.m._ci_cache.clear()

    # ------------------------------------------------------------ stack (an extension's provider, 9.150)
    def test_stack_without_a_provider(self):
        r = self.m.stack_status(self.key)
        self.assertEqual((r["stack"], r["services"], r["dashboard"]), (None, [], ""))
        self.assertIn("no dev stack provider", r["error"])
        self.assertFalse(self.m._stack_up())
        with self.assertRaises(ValueError):
            self.m.stack_logs(self.key, "db")

    def test_stack_through_a_fake_provider(self):
        root = self.root

        class Fake:
            NO_STACK = "register it first"
            rows = [{"root": root, "name": "wb97"}]

            def up(self):
                return True

            def dashboard_url(self):
                return "http://127.0.0.1:1/"

            def worktree(self, path):
                return next((w for w in self.rows if w["root"] == os.path.realpath(path)), None)

            def status(self, r):
                return {"services": [{"service": "db", "state": "running", "status": "Up 3 minutes (healthy)"}]}

            def logs(self, r, service, tail):
                return "line %s %d\n" % (service, tail)
        fake = Fake()
        with mock.patch.object(self.m, "_stack", return_value=fake):
            self.assertTrue(self.m._stack_up())
            r = self.m.stack_status(self.key)
            self.assertEqual((r["stack"], r["dashboard"]), ("wb97", "http://127.0.0.1:1/"))
            self.assertEqual(r["services"], [{"name": "db", "state": "running", "health": "healthy", "age": "3 minutes"}])
            self.assertEqual(self.m.stack_logs(self.key, "db", 5), "line db 5\n")
            fake.rows = []
            self.assertIn("no dev stack for %s (register it first)" % root, self.m.stack_status(self.key)["error"])

    # ------------------------------------------------------------ usage
    def test_usage_per_day_and_project(self):
        for x in self.m.sessions.values():
            x.hidden = True
        a = self._session(claude_session="ua")
        b = self._session(claude_session="ub", project="team-x/other")

        def tr(s, recs):
            d = os.path.join(s.cfg, "projects", "-x")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, s.claude_session + ".jsonl"), "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

        def msg(mid, ts, i, o, cr=0):
            return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "usage": {
                "input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr}}}
        tr(a, [msg("m1", "2026-09-20T10:00:00Z", 10, 1), msg("m1", "2026-09-20T10:00:01Z", 10, 1),
               msg("m2", "2026-09-21T10:00:00Z", 5, 2, 100), {"type": "user", "timestamp": "2026-09-21T10:00:00Z"}])
        tr(b, [msg("m2", "2026-09-21T10:00:00Z", 5, 2, 100), msg("m3", "2026-09-21T11:00:00Z", 1, 1),
               msg("m4", "bad-ts", 7, 7)])
        u = self.m.usage()
        self.assertEqual([d["day"] for d in u["days"]], ["2026-09-21", "2026-09-20"])
        self.assertEqual(u["days"][0], {"day": "2026-09-21", "input": 6, "output": 3, "cache_read": 100, "cache_write": 0})
        self.assertEqual(u["total"], {"input": 16, "output": 4, "cache_read": 100, "cache_write": 0})
        self.assertEqual({p["project"]: (p["sessions"], p["input"]) for p in u["projects"]},
                         {self.key: (1, 15), "team-x/other": (1, 1)})
        self.assertIs(self.m.usage(), u)                                   # cached by transcript mtimes
        c = self._session(claude_session="uc", project="prodcheck")         # 9.12 C: hides itself when done
        c.kind, c.hidden = "prodcheck", True
        tr(c, [msg("m9", "2026-09-22T05:30:00Z", 3, 3)])
        self.assertIn("prodcheck", {p["project"] for p in self.m.usage()["projects"]})


if __name__ == "__main__":
    unittest.main()
