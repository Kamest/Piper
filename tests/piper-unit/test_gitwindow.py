"""Git window: worktree projects `<key>@<name>`, branches / worktrees / fetch / pull for the
GUI, the log as rows, a commit's files and blobs, amend, the drafted commit message (host claude -p)."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import _env
import gitops
import projects
import sessions

GENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Orig",
            GIT_AUTHOR_EMAIL="o@e", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")


def git(cwd, *args):
    # stdin closed: a git that reads it (hash-object --stdin, ...) must not hang the suite on an open stdin
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=GENV,
                          stdin=subprocess.DEVNULL).stdout.decode()


def write(root, rel, data):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(data)


class GitWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        cls.m = _env.manager(cls)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.name = "gw%d" % id(self)
        self.root = os.path.realpath(_env.make_project(self.name, {"src/A.java": "class A {}\n", "README.md": "r\n"}))
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@e")
        git(self.root, "config", "user.name", "T")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "feat: init")
        self.key = "team-x/" + self.name
        _env.register(self.key, self.root, gitlab=["grp/app"])
        self.assertEqual(self.m.repo_top(self.root, record="start"), self.root)

    def _session(self, path=None, state="working", key=None):
        s = sessions.Session(project=key or self.key, path=path or self.root, state=state, title="s")
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        return s

    # ------------------------------------------------------------ worktree projects
    def test_a_worktree_is_added_recorded_and_opened_as_a_derived_project(self):
        live = self._session()   # a session of the base project runs: _may_record alone would refuse
        r = self.m.git_worktree_op(self.key, {"action": "add", "name": "feat-x", "branch": "feat/x", "new": True})
        wt = os.path.join(self.root, ".claude", "worktrees", "feat-x")
        self.assertEqual((r["key"], r["path"], r["branch"]), (self.key + "@feat-x", wt, "feat/x"))
        p = projects.find(self.key + "@feat-x", _env.PP)
        self.assertEqual((p["path"], p["base"], p["worktree"], p["gitlab"], p["source"]),
                         (wt, self.key, "feat-x", ["grp/app"], "worktree"))
        self.assertEqual(p["title"], self.key + " @ feat-x")
        self.assertNotIn(self.key + "@feat-x", projects.load(_env.PP))   # never in the lists
        self.assertEqual(self.m.repo_top(wt), wt)
        self.assertEqual(self.m.gui_project(self.key + "@feat-x")["path"], wt)
        self.assertEqual(self.m.project_git_status(self.key + "@feat-x")["branch"], "feat/x")
        # the main checkout does not list it as untracked (info/exclude)
        self.assertEqual(self.m.project_git_status(self.key)["files"], [])
        # a session in the worktree mounts the repository's .git (the linked-worktree path)
        s = self._session(path=wt, key=self.key + "@feat-x", state="exited")
        self.assertEqual(self.m._main_git(s), os.path.join(self.root, ".git"))
        b = self.m.git_branches(self.key)
        w = {x["path"]: x for x in b["worktrees"]}
        self.assertEqual(w[wt]["key"], self.key + "@feat-x")
        self.assertEqual(w[self.root]["sessions"], [live.id])
        self.assertTrue(w[self.root]["main"])

    def test_a_worktree_key_is_refused_unless_its_git_file_points_into_the_base_repository(self):
        self.assertIsNone(projects.find(self.key + "@nope", _env.PP))
        self.assertIsNone(projects.find(self.key + "@../x", _env.PP))
        fake = os.path.join(self.root, ".claude", "worktrees", "planted")
        other = os.path.realpath(tempfile.mkdtemp())
        git(other, "init", "-q")
        write(fake, "x.txt", "x\n")
        with open(os.path.join(fake, ".git"), "w") as f:
            f.write("gitdir: %s/.git\n" % other)
        self.assertIsNone(projects.find(self.key + "@planted", _env.PP))
        # a gitdir inside the base repository that does not point back
        os.makedirs(os.path.join(self.root, ".git", "worktrees", "planted"))
        with open(os.path.join(self.root, ".git", "worktrees", "planted", "gitdir"), "w") as f:
            f.write("/elsewhere/.git\n")
        with open(os.path.join(fake, ".git"), "w") as f:
            f.write("gitdir: %s/.git/worktrees/planted\n" % self.root)
        self.assertIsNone(projects.find(self.key + "@planted", _env.PP))
        with self.assertRaises(ValueError):
            self.m.git_worktree_op(self.key, {"action": "add", "name": "../up", "branch": "x", "new": True})

    def test_worktree_remove_is_refused_with_changes_or_a_live_session_and_forgets_the_root(self):
        self.m.git_worktree_op(self.key, {"action": "add", "name": "wt", "branch": "wt", "new": True})
        wt = os.path.join(self.root, ".claude", "worktrees", "wt")
        s = self._session(path=wt, key=self.key + "@wt")
        with self.assertRaisesRegex(ValueError, "works in this tree"):
            self.m.git_worktree_op(self.key, {"action": "remove", "name": "wt"})
        s.state = "exited"
        write(wt, "new.txt", "n\n")
        with self.assertRaisesRegex(gitops.GitError, "has changes"):
            self.m.git_worktree_op(self.key, {"action": "remove", "name": "wt"})
        os.unlink(os.path.join(wt, "new.txt"))
        self.m.git_worktree_op(self.key, {"action": "remove", "name": "wt"})
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn(wt, sessions.read_json(sessions.REPO_ROOTS_FILE, {}))
        # added again under the same name: recorded anew, opens
        self.m.git_worktree_op(self.key, {"action": "add", "name": "wt", "branch": "wt"})
        self.assertEqual(self.m.repo_top(wt), wt)

    # ------------------------------------------------------------ branches
    def test_switch_create_delete_with_gits_own_refusals(self):
        r = self.m.git_branch_op(self.key, {"action": "create", "name": "feat/a"})
        self.assertEqual((r["branch"], r["current"]), ("feat/a", "main"))
        live = self._session()
        with self.assertRaisesRegex(ValueError, "works in this tree"):
            self.m.git_branch_op(self.key, {"action": "switch", "name": "feat/a"})
        live.state = "exited"
        self.assertEqual(self.m.git_branch_op(self.key, {"action": "switch", "name": "feat/a"})["branch"], "feat/a")
        write(self.root, "src/A.java", "class A { int x; }\n")
        git(self.root, "commit", "-q", "-am", "fix: on a")
        self.m.git_branch_op(self.key, {"action": "switch", "name": "main"})
        # unmerged: not deleted, the commits named; force deletes
        r = self.m.git_branch_op(self.key, {"action": "delete", "name": "feat/a"})
        self.assertFalse(r["deleted"])
        self.assertTrue(r["unmerged"][0].endswith("fix: on a"))
        self.assertTrue(self.m.git_branch_op(self.key, {"action": "delete", "name": "feat/a", "force": True})["deleted"])
        with self.assertRaisesRegex(gitops.GitError, "checked out"):
            self.m.git_branch_op(self.key, {"action": "delete", "name": "main"})
        # a change the switch would overwrite: git refuses, nothing forced
        self.m.git_branch_op(self.key, {"action": "create", "name": "other"})
        self.m.git_branch_op(self.key, {"action": "switch", "name": "other"})
        write(self.root, "README.md", "other\n")
        git(self.root, "commit", "-q", "-am", "other")
        self.m.git_branch_op(self.key, {"action": "switch", "name": "main"})
        write(self.root, "README.md", "dirty\n")
        with self.assertRaises(gitops.GitError):
            self.m.git_branch_op(self.key, {"action": "switch", "name": "other"})
        with open(os.path.join(self.root, "README.md")) as f:
            self.assertEqual(f.read(), "dirty\n")
        for bad in ("-D", "a..b", "--force", "x y"):
            with self.assertRaises(gitops.GitError):
                self.m.git_branch_op(self.key, {"action": "create", "name": bad})

    def test_remote_branch_fetch_pull_and_tracking(self):
        bare = os.path.realpath(tempfile.mkdtemp())
        git(bare, "init", "-q", "--bare", "-b", "main")
        git(self.root, "remote", "add", "origin", bare)
        git(self.root, "push", "-q", "-u", "origin", "main")
        clone = os.path.realpath(tempfile.mkdtemp())
        git(clone, "clone", "-q", bare, ".")
        git(clone, "switch", "-q", "-c", "feat/remote")
        write(clone, "r.txt", "r\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "feat: remote")
        git(clone, "push", "-q", "-u", "origin", "feat/remote")
        git(clone, "switch", "-q", "main")
        write(clone, "m.txt", "m\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-q", "-m", "feat: main moved")
        git(clone, "push", "-q")
        with mock.patch.object(gitops, "FAIL_CLOSED", False):   # file:// transport in a test
            self.m.git_remote_op(self.key, "fetch")
            b = self.m.git_branches(self.key)
            self.assertIn("origin/feat/remote", [x["name"] for x in b["remote"]])
            self.assertNotIn("origin/HEAD", [x["name"] for x in b["remote"]])
            main = next(x for x in b["local"] if x["name"] == "main")
            self.assertEqual((main["upstream"], main["behind"], main["ahead"]), ("origin/main", 1, 0))
            self.m.git_remote_op(self.key, "pull")
            self.assertTrue(os.path.exists(os.path.join(self.root, "m.txt")))
            r = self.m.git_branch_op(self.key, {"action": "switch", "name": "origin/feat/remote"})
            self.assertEqual(r["branch"], "feat/remote")
            self.assertEqual(gitops.upstream(self.root), "origin/feat/remote")
            # amend is refused once HEAD is on a remote branch
            with self.assertRaisesRegex(gitops.GitError, "already pushed"):
                gitops.check_amend(self.root)

    # ------------------------------------------------------------ log, commit, blob
    def test_rows_commit_files_and_blobs(self):
        write(self.root, "src/A.java", "class A { int x; }\n")
        git(self.root, "mv", "README.md", "README.txt")
        write(self.root, ".gitmodules", "")
        with open(os.path.join(self.root, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "feat: second\n\nthe body")
        rows = self.m.git_graph_rows(self.key, 50)["rows"]
        self.assertEqual([r["subject"] for r in rows if r.get("hash")], ["feat: second", "feat: init"])
        self.assertTrue(rows[0]["graph"].startswith("*"))
        self.assertIn("HEAD -> main", rows[0]["refs"])
        self.assertEqual(len(rows[0]["parents"]), 1)
        self.assertEqual(len(self.m.git_graph_rows(self.key, 50, "main")["rows"]), 2)
        with self.assertRaises(gitops.GitError):
            self.m.git_graph_rows(self.key, 50, "--all")
        head = rows[0]["hash"]
        d = self.m.git_commit_detail(self.key, head)
        self.assertEqual((d["message"], d["author"], d["parents"]), ("feat: second\n\nthe body", "Orig", [rows[1]["hash"]]))
        f = {x["path"]: x for x in d["files"]}
        self.assertEqual((f["src/A.java"]["status"], f["src/A.java"]["add"], f["src/A.java"]["del"]), ("M", 1, 1))
        self.assertEqual((f["README.txt"]["status"], f["README.txt"]["old_path"]), ("R", "README.md"))
        self.assertTrue(f["bin.dat"]["binary"])
        self.assertEqual(self.m.git_blob(self.key, head, "src/A.java", "new")["text"], "class A { int x; }\n")
        self.assertEqual(self.m.git_blob(self.key, head, "src/A.java", "old")["text"], "class A {}\n")
        self.assertEqual(self.m.git_blob(self.key, head, "README.md", "old")["text"], "r\n")
        self.assertFalse(self.m.git_blob(self.key, head, "README.md", "new")["exists"])
        with self.assertRaisesRegex(gitops.GitError, "binary"):
            self.m.git_blob(self.key, head, "bin.dat", "new")
        with self.assertRaises(gitops.GitError):
            self.m.git_blob(self.key, head, ".git/config", "new")
        root = self.m.git_commit_detail(self.key, rows[1]["hash"])
        self.assertEqual(sorted(x["path"] for x in root["files"]), ["README.md", "src/A.java"])
        self.assertEqual(self.m.git_blob(self.key, rows[1]["hash"], "src/A.java", "old"), {"text": "", "exists": False})

    # ------------------------------------------------------------ commit panel
    def test_amend_keeps_author_and_parent_on_both_commit_paths(self):
        s = self._session(state="exited")
        first = git(self.root, "rev-parse", "HEAD").strip()
        write(self.root, "src/A.java", "class A { int y; }\n")
        git(self.root, "commit", "-q", "-am", "wip")
        write(self.root, "src/B.java", "class B {}\n")
        gitops.commit(self.root, "feat: A and B", ["src/B.java"], amend=True)   # the unreviewed path (D14: message-only amend)
        self.assertEqual(git(self.root, "log", "-1", "--format=%s|%an|%P").strip(), "feat: A and B|Orig|" + first)
        self.assertEqual(git(self.root, "show", "--name-only", "--format=", "HEAD").split(), ["src/A.java", "src/B.java"])
        self.m.git_commit(s.id, "feat: message only", [], amend=True)
        self.assertEqual(git(self.root, "log", "-1", "--format=%s|%P").strip(), "feat: message only|" + first)
        with self.assertRaisesRegex(ValueError, "the GUI's"):
            self.m.git_commit(s.id, "x", ["src/A.java"], agent=True, amend=True)

    def test_reviewed_amend_replaces_head_with_its_parents(self):
        objs = tempfile.mkdtemp()
        idx = os.path.join(tempfile.mkdtemp(), "i")
        first = git(self.root, "rev-parse", "HEAD").strip()
        write(self.root, "src/A.java", "class A { int y; }\n")
        git(self.root, "commit", "-q", "-am", "wip")
        head = git(self.root, "rev-parse", "HEAD").strip()
        write(self.root, "src/C.java", "class C {}\n")
        coid = git(self.root, "hash-object", "-w", "src/C.java").strip()
        gitops.commit_reviewed(self.root, "feat: reviewed amend", head, {"src/C.java": {"oid": coid, "mode": "100644"}},
                               objs, idx, amend=True)
        self.assertEqual(git(self.root, "log", "-1", "--format=%s|%an|%P").strip(), "feat: reviewed amend|Orig|" + first)
        self.assertIn("src/C.java", git(self.root, "show", "--name-only", "--format=", "HEAD").split())

    def test_message_is_drafted_by_host_claude_without_hidden_files(self):
        s = self._session(state="exited")
        d = tempfile.mkdtemp()
        seen, argv = os.path.join(d, "stdin"), os.path.join(d, "argv")
        exe = os.path.join(d, "claude")
        with open(exe, "w") as f:
            f.write('#!/bin/sh\nprintf "%%s\\n" "$@" > %s\npwd >> %s\ncat > %s\necho "fix(app): tidy A"\n' % (argv, argv, seen))
        os.chmod(exe, stat.S_IRWXU)
        write(self.root, "src/A.java", "class A { String token = \"glpat-abcdefghijklmnopqrst\"; }\n")
        write(self.root, "config/secrets/db.properties", "db.password=SECRET\n")
        with mock.patch.dict(os.environ, {"PIPER_CLAUDE_BIN": exe}):
            r = self.m.git_message(s.id, ["src/A.java", "config/secrets/db.properties"])
        self.assertEqual((r["message"], r["withheld"], r["truncated"]), ("fix(app): tidy A", ["config/secrets/db.properties"], False))
        with open(seen) as f:
            sent = f.read()
        self.assertIn("- feat: init", sent)
        self.assertIn("src/A.java", sent)
        self.assertNotIn("db.properties", sent)
        self.assertNotIn("SECRET", sent)
        self.assertNotIn("glpat-abcdefghijklmnopqrst", sent)
        with open(argv) as f:
            a = f.read().split("\n")
        for flag in ("-p", "haiku", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertNotEqual(os.path.realpath(a[-2]), self.root)   # an empty temp cwd, not the project
        with self.assertRaisesRegex(ValueError, "refresh"):
            self.m.git_message(s.id, ["not/changed.txt"])
        with mock.patch.dict(os.environ, {"PIPER_CLAUDE_BIN": "/nonexistent/claude"}), \
                mock.patch.object(sessions.shutil, "which", lambda n: None), \
                mock.patch.object(sessions.common, "REAL_HOME", d):
            with self.assertRaisesRegex(ValueError, "not installed"):
                self.m.git_message(s.id, ["src/A.java"])

    def test_mr_message_is_drafted_from_the_branch_without_hidden_files(self):
        """commit A: Generate in the MR dialog - the branch's subjects and diff against the target, hidden files by count"""
        s = self._session(state="exited")
        git(self.root, "switch", "-q", "-c", "feat/x")
        write(self.root, "src/A.java", "class A { String token = \"glpat-abcdefghijklmnopqrst\"; }\n")
        write(self.root, "config/secrets/db.properties", "db.password=SECRET\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "fix(app): tidy A")
        d = tempfile.mkdtemp()
        seen, exe = os.path.join(d, "stdin"), os.path.join(d, "claude")
        with open(exe, "w") as f:
            f.write('#!/bin/sh\ncat > %s\nprintf "# Tidy A\\n\\nWhat: tidy.\\n"\n' % seen)
        os.chmod(exe, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {"PIPER_CLAUDE_BIN": exe}):
            r = self.m.mr_message(s.id, "main")
        self.assertEqual((r["title"], r["description"], r["withheld"], r["truncated"]),
                         ("Tidy A", "What: tidy.", ["config/secrets/db.properties"], False))
        with open(seen) as f:
            sent = f.read()
        self.assertIn("- fix(app): tidy A", sent)
        self.assertNotIn("feat: init", sent)   # only the branch's own commits
        self.assertIn("src/A.java", sent)
        for leak in ("db.properties", "SECRET", "glpat-abcdefghijklmnopqrst"):
            self.assertNotIn(leak, sent)
        with self.assertRaises(gitops.GitError):
            self.m.mr_message(s.id, "-x")
        with self.assertRaisesRegex(ValueError, "unknown target"):
            self.m.mr_message(s.id, "nope")


if __name__ == "__main__":
    unittest.main()
