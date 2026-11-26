"""A linked worktree an agent adds under `<checkout>/.claude/worktrees/<name>` while its sandbox runs: piperd
trusts it only as a worktree of the checkout's recorded repository, proven on the files (no git run), and
shows it under the checkout's hidden-file rule too (2026-11-26, Git and Files in the agent's worktree)."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import _env
import gitops
import projects
import sessions

GENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="T",
            GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=GENV,
                          stdin=subprocess.DEVNULL).stdout.decode()


def write(root, rel, data, mode="w"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as f:
        f.write(data)


class WorktreeTrustTest(unittest.TestCase):
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
        self.name = "wt%d" % id(self)
        self.root = os.path.realpath(_env.make_project(self.name, {"README.md": "r\n", "secret.txt": "s\n"}))
        write(self.root, ".hole/settings.json", json.dumps({"files": {"exclude": ["secret.txt"]}}))
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@e")
        git(self.root, "config", "user.name", "T")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        self.key = "team-x/" + self.name
        _env.register(self.key, self.root)
        self.assertEqual(self.m.repo_top(self.root, record="start"), self.root)
        self.live = self._session(self.root)   # the agent's sandbox writes the checkout from now on

    def _session(self, path, key=None):
        s = sessions.Session(project=key or self.key, path=path, state="working", title="s")
        s.pid = os.getpid()
        self.m.sessions[s.id] = s
        self.addCleanup(self.m.sessions.pop, s.id, None)
        return s

    def _agent_worktree(self, name, branch=None):
        """`git worktree add` as the agent runs it in its sandbox: nothing recorded by piperd."""
        wt = os.path.join(self.root, ".claude", "worktrees", name)
        git(self.root, "worktree", "add", "-q", "-b", branch or "agent-" + name, wt)
        return wt

    def _recorded(self, wt):
        return isinstance(sessions.read_json(sessions.REPO_ROOTS_FILE, {}).get(os.path.realpath(wt)), dict)

    def test_an_agent_worktree_is_trusted_as_a_worktree_of_the_recorded_checkout(self):
        wt = self._agent_worktree("fix-1")
        self.assertIsNone(self.m.repo_top(wt, record=""))   # "" never records
        self.assertFalse(self._recorded(wt))
        self.assertIsNone(self.m.repo_top(wt, record="other"))   # nor does any other mode
        self.assertFalse(self._recorded(wt))
        self.assertEqual(self.m.repo_top(wt), wt)
        rec = sessions.read_json(sessions.REPO_ROOTS_FILE, {})[wt]
        self.assertEqual((rec["top"], rec["common"]), (wt, os.path.join(self.root, ".git")))
        self.assertEqual(rec["gitdir"], os.path.join(self.root, ".git", "worktrees", "fix-1"))
        # the checkout no longer lists its agent's worktrees as untracked (nor would a snapshot store a gitlink)
        self.assertNotIn(".claude", git(self.root, "status", "--porcelain", "--untracked-files=all"))
        st = self.m.project_git_status(self.key + "@fix-1")
        self.assertEqual(st["branch"], "agent-fix-1")
        # later tampering with the worktree's commondir is caught before any git runs
        write(rec["gitdir"], "commondir", "/nonexistent/other/.git\n")
        self.assertIsNone(self.m.repo_top(wt))

    def test_git_in_a_trusted_worktree_never_rediscovers_its_repository(self):
        """N5: the `.git` file swapped between the check and the run must not hand git another repository's config"""
        wt = self._agent_worktree("pin")
        self.assertEqual(self.m.repo_top(wt), wt)
        evil = os.path.realpath(tempfile.mkdtemp(prefix="evil"))
        self.addCleanup(shutil.rmtree, evil, True)
        git(evil, "init", "-q", "-b", "main")
        git(evil, "config", "filter.f.clean", "touch %s/PWNED; cat" % evil)
        git(evil, "commit", "-q", "--allow-empty", "-m", "e")
        git(evil, "worktree", "add", "-q", "-b", "e2", os.path.join(evil, "w"))
        write(wt, ".gitattributes", "* filter=f\n")
        write(wt, "README.md", "changed\n")
        write(wt, ".git", "gitdir: %s/.git/worktrees/w\n" % evil)   # after repo_top answered
        st = gitops.status(wt, lambda p: False)
        self.assertEqual(st["branch"], "agent-pin")
        self.assertIn("README.md", [f["path"] for f in st["files"]])
        self.assertFalse(os.path.exists(os.path.join(evil, "PWNED")))
        self.assertIsNone(self.m.repo_top(wt))   # the next check sees the swap: refused, unpinned
        self.assertNotIn(wt, gitops.PINS)

    def test_forged_worktrees_are_not_trusted(self):
        other = os.path.realpath(tempfile.mkdtemp(prefix="other"))
        self.addCleanup(shutil.rmtree, other, True)
        git(other, "init", "-q", "-b", "main")
        git(other, "commit", "-q", "--allow-empty", "-m", "x")
        wts = os.path.join(self.root, ".claude", "worktrees")

        def refused(name):
            wt = os.path.join(wts, name)
            self.assertIsNone(self.m.repo_top(wt), name)
            self.assertFalse(self._recorded(wt), name)

        # (first: the forgeries below break `git worktree add`) a worktree nested in a worktree is no `<checkout>/.claude/worktrees/<name>`
        y = self._agent_worktree("y")
        self.assertEqual(self.m.repo_top(y), y)
        z = os.path.join(y, ".claude", "worktrees", "z")
        git(y, "worktree", "add", "-q", "-b", "z", z)
        self.assertIsNone(self.m.repo_top(z))
        # a worktree of another repository
        git(other, "worktree", "add", "-q", "-b", "o", os.path.join(wts, "foreign"))
        refused("foreign")
        # a separate clone
        git(self.root, "clone", "-q", other, os.path.join(wts, "clone"))
        refused("clone")
        # a real worktree whose commondir names another repository
        wt = self._agent_worktree("commondir")
        write(self.root, ".git/worktrees/commondir/commondir", os.path.join(other, ".git") + "\n")
        refused("commondir")
        # ... or is a symlink
        wt = self._agent_worktree("cdlink")
        os.remove(os.path.join(self.root, ".git/worktrees/cdlink/commondir"))
        os.symlink("../..", os.path.join(self.root, ".git/worktrees/cdlink/commondir"))
        refused("cdlink")
        # the admin dir a symlink to another repository's
        git(other, "worktree", "add", "-q", "-b", "o2", os.path.join(other, "w2"))
        os.makedirs(os.path.join(wts, "adminlink"))
        os.symlink(os.path.join(other, ".git/worktrees/w2"), os.path.join(self.root, ".git/worktrees/adminlink"))
        write(wts, "adminlink/.git", "gitdir: %s/.git/worktrees/adminlink\n" % self.root)
        refused("adminlink")
        # a .git that is no text
        os.makedirs(os.path.join(wts, "bytes"))
        write(wts, "bytes/.git", b"gitdir: \xff\xfe\n", mode="wb")
        refused("bytes")
        self.assertIsNone(projects.worktree_gitdir(self.root, os.path.join(wts, "bytes")))
        # a plain folder
        os.makedirs(os.path.join(wts, "plain"))
        refused("plain")

    def test_a_checkout_whose_git_is_replaced_by_a_symlink_lends_no_trust(self):
        wt = self._agent_worktree("fix-2")
        moved = self.root + "-dotgit"
        os.rename(os.path.join(self.root, ".git"), moved)
        self.addCleanup(lambda: os.path.islink(os.path.join(self.root, ".git")) and (
            os.remove(os.path.join(self.root, ".git")), os.rename(moved, os.path.join(self.root, ".git"))))
        os.symlink(moved, os.path.join(self.root, ".git"))
        self.assertIsNone(self.m.repo_top(wt))
        self.assertFalse(self._recorded(wt))

    def test_a_worktree_shows_under_the_checkouts_hidden_rule_too(self):
        wt = self._agent_worktree("hide")
        p = projects.find(self.key + "@hide", _env.PP)
        self.assertTrue(self.m.project_hidden(p)("secret.txt"))
        # the agent removes the exclude in its worktree: nothing is revealed
        write(wt, ".hole/settings.json", json.dumps({"files": {"exclude": []}}))
        self.assertTrue(self.m.project_hidden(p)("secret.txt"))
        # what a session of the checkout pinned at its start counts as well
        self.live.excludes = ["pinned.txt"]
        self.assertTrue(self.m.project_hidden(p)("pinned.txt"))
        self.assertFalse(self.m.project_hidden(p)("README.md"))
        write(wt, "secret.txt", "changed\n")
        st = self.m.project_git_status(self.key + "@hide")
        self.assertEqual([(f["path"], f["sensitive"]) for f in st["files"] if f["path"] != ".hole/settings.json"],
                         [("secret.txt", True)])


    # ------------------------------------------------------------ the session's Git panel over the worktree
    def test_the_sessions_git_panel_and_review_work_in_its_agents_worktree(self):
        wt = self._agent_worktree("fix-3")
        write(wt, "README.md", "agent work\n")
        write(wt, "secret.txt", "agent edit\n")
        write(self.root, "checkout.txt", "checkout work\n")
        tid = self.live.id + "~fix-3"
        st = self.m.git_status(tid)
        self.assertEqual((st["root"], st["branch"]), (wt, "agent-fix-3"))
        self.assertEqual({f["path"]: f["sensitive"] for f in st["files"]}, {"README.md": False, "secret.txt": True})
        self.assertIn("+agent work", self.m.git_diff(tid, "README.md", False))
        self.assertEqual(self.m.turns(tid)["turns"], [])
        # reviewed commit in the worktree: the checkout's HEAD, index and files stay as they were
        head = git(self.root, "rev-parse", "HEAD")
        review = self.m.review_files(tid, "head")
        self.assertEqual(review["head"], git(wt, "rev-parse", "HEAD").strip())
        row = next(r for r in review["files"] if r["path"] == "README.md")
        with self.assertRaises(ValueError):   # a review of the worktree is no review of the checkout
            self.m.git_commit(self.live.id, "x", ["README.md"], {"review": review["id"], "head": review["head"],
                                                                "versions": {"README.md": row["after"]}})
        r = self.m.git_commit(tid, "agent work", ["README.md"], {"review": review["id"], "head": review["head"],
                                                                "versions": {"README.md": row["after"]}})
        self.assertTrue(r["commit"])
        self.assertEqual(git(wt, "log", "-1", "--format=%s"), "agent work\n")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.root, "status", "--porcelain"), "?? checkout.txt\n")
        self.assertEqual([f["path"] for f in self.m.git_status(tid)["files"]], ["secret.txt"])
        # push of the worktree's branch
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(gitops, "FAIL_CLOSED", False):
            git(bare, "init", "-q", "--bare", "-b", "main")
            git(self.root, "remote", "add", "origin", bare)
            self.assertEqual(self.m.git_push_preview(tid)["branch"], "agent-fix-3")
            self.assertEqual(self.m.git_push(tid)["branch"], "agent-fix-3")
            self.assertEqual(git(bare, "log", "-1", "--format=%s", "agent-fix-3"), "agent work\n")
            self.assertNotIn("main", git(bare, "branch"))

    def test_a_tree_id_names_only_a_worktree_of_the_sessions_own_checkout(self):
        self._agent_worktree("ok")
        for bad in ("../x", ".", "a/b", "none"):
            with self.subTest(bad=bad), self.assertRaises((KeyError, ValueError)):
                self.m.git_status(self.live.id + "~" + bad)
        with self.assertRaises(KeyError):   # never an id of the session list, terminals, stop, ...
            self.m.get(self.live.id + "~ok")
        wsess = self._session(os.path.join(self.root, ".claude", "worktrees", "ok"), key=self.key + "@ok")
        with self.assertRaises(KeyError):   # a worktree session has no worktrees of its own here
            self.m.git_status(wsess.id + "~ok")
        shutil.rmtree(os.path.join(self.root, ".claude", "worktrees", "ok"))
        git(self.root, "worktree", "prune")
        with self.assertRaises(ValueError):
            self.m.git_status(self.live.id + "~ok")

    def test_turn_snapshots_of_a_session_in_a_pinned_worktree_keep_their_own_store(self):
        """Review finding: the pin mixed the snapshot store (its own GIT_DIR) with the repository - `init --bare`
        and `update-ref` failed, every turn of a worktree session was "Snapshot unavailable"."""
        wt = self._agent_worktree("turns")
        self.assertEqual(self.m.repo_top(wt), wt)
        self.assertIn(wt, gitops.PINS)
        ws = self._session(wt, key=self.key + "@turns")
        write(wt, "README.md", "turn 1\n")
        before = self.m._snapshot(ws)
        self.m._pin_turn(ws, before, "1-start")   # a new store
        write(wt, "README.md", "turn 2\n")
        after = self.m._snapshot(ws)
        self.m._pin_turn(ws, after, "1")   # an existing store
        self.assertNotEqual(before, after)
        self.assertEqual(git(wt, "status", "--porcelain"), " M README.md\n")   # the repository untouched

    def test_a_tree_id_never_names_an_external_worktree(self):
        ext = os.path.realpath(tempfile.mkdtemp(prefix="ext"))
        self.addCleanup(shutil.rmtree, ext, True)
        os.rmdir(ext)
        git(self.root, "worktree", "add", "-q", "-b", "ext", ext)
        self.addCleanup(git, self.root, "worktree", "prune")
        with self.assertRaises((KeyError, ValueError)):
            self.m.git_status(self.live.id + "~" + os.path.basename(ext))

    def test_a_forgotten_worktree_is_unpinned(self):
        wt = self._agent_worktree("gone")
        self.assertEqual(self.m.repo_top(wt), wt)
        self.m.forget_root(wt)
        self.assertNotIn(wt, gitops.PINS)


if __name__ == "__main__":
    unittest.main()
