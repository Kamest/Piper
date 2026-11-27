"""A linked worktree an agent adds under `<checkout>/.claude/worktrees/<name>` while its sandbox runs: piperd
trusts it only as a worktree of the checkout's recorded repository, proven on the files (no git run), and
shows it under the checkout's hidden-file rule too (2026-11-26, Git and Files in the agent's worktree)."""
import json
import os
import shutil
import subprocess
import tempfile
import time
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

    def test_the_agents_git_tools_act_in_its_worktree_under_the_checkouts_hidden_rule(self):
        wt = self._agent_worktree("fix-7")
        write(wt, "README.md", "agent work\n")
        write(wt, "secret.txt", "agent edit\n")
        # the worktree's settings file is the agent's to edit: the checkout's rule still hides secret.txt
        write(wt, ".hole/settings.json", json.dumps({"files": {"exclude": []}}))
        write(self.root, "checkout.txt", "checkout work\n")
        tool = lambda name, **a: self.m.tool(self.live, name, dict(a, worktree="fix-7"))  # noqa: E731
        st = tool("git_status")
        self.assertEqual(st["branch"], "agent-fix-7")
        self.assertNotIn("secret.txt", [f["path"] for f in st["files"]])
        self.assertIn("README.md", [f["path"] for f in st["files"]])
        self.assertNotIn("checkout.txt", [f["path"] for f in st["files"]])
        self.assertGreaterEqual(st["withheld"], 1)
        diff = json.dumps(tool("git_diff"))
        self.assertIn("agent work", diff)
        self.assertNotIn("agent edit", diff)
        with self.assertRaisesRegex((ValueError, gitops.GitError), "hidden"):
            tool("git_diff", path="secret.txt")
        with mock.patch.object(self.m, "ask", return_value=False), self.assertRaises((ValueError, gitops.GitError)):
            tool("git_run", args=["show", ":secret.txt"])
        self.assertNotIn("agent edit", json.dumps(tool("git_run", args=["diff"])))
        with mock.patch.object(sessions, "audit") as audit:
            self.assertEqual(tool("git_run", args=["branch", "--show-current"])["output"].strip(), "agent-fix-7")
        runs = [a[0][0] for a in audit.call_args_list if a[0][0].get("event") == "git-run"]
        self.assertEqual(runs[-1].get("worktree"), "fix-7")   # the audit tells it from the checkout's
        # the joined rule keeps the flags _git_run acts on (N10: a moved over-mount forces a card)
        def moved(p):
            return True
        moved.moved = ["/x"]
        with mock.patch.object(self.m, "hidden_for", return_value=moved), \
                mock.patch.object(self.m, "_git_run", return_value={}) as run:
            tool("git_run", args=["status"])
        self.assertEqual(getattr(run.call_args[0][2], "moved", None), ["/x"])
        with self.assertRaises(ValueError):
            tool("git_commit", message="x", files=["secret.txt"])
        with mock.patch.object(self.m, "ask", return_value=True) as ask:
            r = tool("git_commit", message="agent work", files=["README.md"])
        self.assertIn("worktree fix-7", ask.call_args[0][2])
        self.assertIs(ask.call_args[0][1], self.live)   # the card is the session's
        self.assertTrue(r["commit"])
        self.assertEqual(git(wt, "log", "-1", "--format=%s"), "agent work\n")
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), "init\n")
        self.assertIn("agent work", json.dumps(tool("git_log", n=1)))
        with tempfile.TemporaryDirectory() as bare, mock.patch.object(gitops, "FAIL_CLOSED", False), \
                mock.patch.object(self.m, "ask", return_value=True) as ask:
            git(bare, "init", "-q", "--bare", "-b", "main")
            git(self.root, "remote", "add", "origin", bare)
            self.assertEqual(tool("git_push")["branch"], "agent-fix-7")
            self.assertIn("(worktree fix-7)", ask.call_args[0][2])
            self.assertIs(ask.call_args[0][1], self.live)
            self.assertEqual(git(bare, "log", "-1", "--format=%s", "agent-fix-7"), "agent work\n")
        # the checkout's tools stay on the checkout
        self.assertIn("checkout.txt", [f["path"] for f in self.m.tool(self.live, "git_status", {})["files"]])
        with self.assertRaisesRegex(ValueError, "not both"):
            self.m.tool(self.live, "git_run", {"project": self.key, "worktree": "fix-7", "args": ["status"]})
        for bad in ("../x", "none", "a/b"):
            with self.assertRaises((ValueError, KeyError)):
                self.m.tool(self.live, "git_status", {"worktree": bad})

    def test_a_worktree_added_again_under_another_admin_dir_is_derived_anew(self):
        wt = self._agent_worktree("fix-9")
        self.assertEqual(self.m.repo_top(wt, record="view"), wt)
        shutil.rmtree(wt)   # the agent removes it by hand; git still knows it until a prune
        git(self.root, "worktree", "prune")
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, True)
        git(self.root, "worktree", "add", "-q", "-b", "elsewhere", os.path.join(other, "fix-9"))   # takes worktrees/fix-9
        git(self.root, "worktree", "add", "-q", wt, "agent-fix-9")
        gd = git(wt, "rev-parse", "--absolute-git-dir").strip()
        self.assertNotEqual(os.path.basename(gd), "fix-9")
        self.assertIsNone(self.m.repo_top(wt, record=""))   # a check that never records keeps refusing
        guard = self.m._git_guard   # the new admin dir is guarded before git runs there
        with mock.patch.object(self.m, "_git_guard", side_effect=lambda r: r.get("gitdir") != os.path.realpath(gd) and guard(r)):
            self.assertIsNone(self.m.repo_top(wt, record="view"))
        self.assertEqual(self.m.repo_top(wt, record="view"), wt)
        rec = sessions.read_json(sessions.REPO_ROOTS_FILE, {})[os.path.realpath(wt)]
        self.assertEqual(rec["gitdir"], os.path.realpath(gd))
        # another repository's worktree at that path is never adopted
        shutil.rmtree(wt)
        git(self.root, "worktree", "prune")
        foreign = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, foreign, True)
        git(foreign, "init", "-q", "-b", "main")
        git(foreign, "commit", "-q", "--allow-empty", "-m", "f")
        git(foreign, "worktree", "add", "-q", "-b", "x", wt)
        self.assertIsNone(self.m.repo_top(wt, record="view"))

    def test_a_tree_id_over_http_and_in_review_dispatch_and_the_message_draft(self):
        import threading
        import urllib.request
        import common
        import piperd
        wt = self._agent_worktree("fix-8")
        write(wt, "README.md", "agent work\n")
        write(wt, "secret.txt", "agent edit\n")
        tid = self.live.id + "~fix-8"
        old = piperd.MANAGER, piperd.PORT
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        piperd.MANAGER, piperd.PORT = self.m, server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop():
            server.shutdown()
            server.server_close()
            piperd.MANAGER, piperd.PORT = old
        self.addCleanup(stop)

        def http(path):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (server.server_port, path),
                                         headers={"Authorization": "Bearer " + common.own_token("gui")})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode()
        code, body = http("/api/sessions/%s/git" % tid)
        self.assertEqual(code, 200, body)
        st = json.loads(body)
        self.assertEqual((st["root"], st["branch"]), (wt, "agent-fix-8"))
        code, body = http("/api/sessions/%s/git?diff=README.md" % tid)
        self.assertEqual(code, 200, body)
        self.assertIn("+agent work", body)
        self.assertIn(http("/api/sessions/%s~none/git" % self.live.id)[0], (404, 409))   # no such worktree
        self.assertIn(http("/api/sessions/%s~..%%2Fx/git" % self.live.id)[0], (404, 409))
        # Review's "Ask Claude": the agent's own session, told where the files are; a hidden file never goes
        review = self.m.review_files(tid, "head")
        row = next(r for r in review["files"] if r["path"] == "README.md")
        sent = []
        with mock.patch.object(self.m, "send", side_effect=lambda sid, text: sent.append((sid, text))):
            r = self.m.review_dispatch(tid, {"text": "check README.md", "target": "current",
                                             "items": [{"path": "README.md", "sha": row["work_sha"]}]})
            self.assertEqual(r["session"], self.live.id)
            self.assertIn(".claude/worktrees/fix-8/", sent[0][1])
            with self.assertRaisesRegex(ValueError, "hidden"):
                self.m.review_dispatch(tid, {"text": "look at secret.txt", "target": "current",
                                             "items": [{"path": "README.md", "sha": row["work_sha"]}]})
        self.assertEqual(len(sent), 1)
        # the commit message draft: the worktree's diff, the hidden file by count only
        with mock.patch.object(self.m, "_haiku", return_value="docs: agent work") as haiku:
            r = self.m.git_message(tid, ["README.md", "secret.txt"])
        self.assertEqual(r["withheld"], ["secret.txt"])
        self.assertIn("agent work", haiku.call_args[0][0])
        self.assertNotIn("agent edit", haiku.call_args[0][0])

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


    def _event(self, s, ev):
        """A hook's turn boundary, as Manager.hook passes it; waits for the snapshot worker."""
        self._ts = getattr(self, "_ts", time.time() - 60) + 1
        s.state = "working" if ev == "UserPromptSubmit" else "waiting"
        s.idle = ev != "UserPromptSubmit"
        self.m._turn_event(s, ev, {"ts": self._ts, "prompt": "work %d" % self._ts})
        deadline = time.time() + 30
        while s.turn_busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(s.turn_busy, "snapshot worker did not finish")
        deadline = time.time() + 30
        while s.id in self.m._seeding and time.time() < deadline:
            time.sleep(0.02)

    def test_turns_review_and_revert_work_in_the_agents_worktree(self):
        """Step 7: the session's turns snapshot the worktree its agent edits in - Review All turns, a turn's diff
        and Revert work there; an Undo never crosses between the checkout and the worktree."""
        wt = self._agent_worktree("turns2")
        s, tid = self.live, self.live.id + "~turns2"
        # turn 1: the agent enters the worktree during the turn (its first edit is seen mid-turn)
        self._event(s, "UserPromptSubmit")
        write(wt, "README.md", "turn one\n")
        self.m._fsedit_event(s, {"ts": time.time(), "file_paths": [os.path.join(wt, "README.md")], "tool_name": "Edit"})
        self.assertEqual(s.agent_wt, "turns2")
        write(self.root, "checkout.txt", "checkout\n")
        self._event(s, "Stop")
        # turn 2: started in the worktree
        self._event(s, "UserPromptSubmit")
        write(wt, "README.md", "turn two\n")
        write(wt, "new.txt", "new\n")
        self._event(s, "Stop")
        out = self.m.turns(tid)
        self.assertEqual([t["n"] for t in out["turns"]], [1, 2])
        self.assertEqual(out["turns"][0]["base"], "branch")
        self.assertIn("branch left the checkout", out["note"])
        self.assertFalse([t for t in out["turns"] if t.get("unavailable")])
        self.assertEqual(out["turns"][0]["summary"]["files"], 1)
        self.assertEqual(out["turns"][1]["summary"]["files"], 2)
        t2 = out["turns"][1]
        self.assertEqual({f["path"] for f in self.m.turn_files(tid, t2["from"], t2["to"])["files"]},
                         {"README.md", "new.txt"})
        self.assertIn("+turn two", self.m.turn_diff(tid, t2["from"], t2["to"], "README.md"))
        self.assertEqual(self.m.turn_file_history(tid, "README.md")["diff_path"], "README.md")
        # the checkout's turns are untouched by the worktree's
        mine = self.m.turns(s.id)["turns"]
        self.assertEqual(len(mine), 2)
        self.assertEqual({f["path"] for f in self.m.turn_files(s.id, mine[0]["from"], mine[0]["to"])["files"]},
                         {"checkout.txt"})
        # Revert the worktree to the end of turn 1: the checkout stays as it is
        pv = self.m.turn_revert_preview(tid, 1)
        self.assertEqual({f["path"] for f in pv["files"]}, {"README.md", "new.txt"})
        r = self.m.turn_revert(tid, 1, pv["current"])
        with open(os.path.join(wt, "README.md")) as f:
            self.assertEqual(f.read(), "turn one\n")
        self.assertFalse(os.path.exists(os.path.join(wt, "new.txt")))
        with open(os.path.join(self.root, "checkout.txt")) as f:
            self.assertEqual(f.read(), "checkout\n")
        self.assertIn(".claude/worktrees/turns2/README.md", s.revert_note)
        self.assertEqual([x["id"] for x in self.m.turns(tid)["reverts"]], [r["undo"]])
        self.assertEqual(self.m.turns(s.id)["reverts"], [])
        with self.assertRaises(ValueError):   # the worktree's Undo is no Undo of the checkout
            self.m.turn_revert_preview(s.id, 0, r["undo"])
        undo = self.m.turn_revert_preview(tid, 0, r["undo"])
        self.m.turn_revert(tid, 0, undo["current"], r["undo"])
        with open(os.path.join(wt, "README.md")) as f:
            self.assertEqual(f.read(), "turn two\n")
        # the pins live under the session: removing it unpins the worktree's too
        refs = gitops.snapshot_refs(wt, os.path.join(s.dir, "trees", "turns2", "snap-objects"),
                                    os.path.join(s.dir, "trees", "turns2", "snap.git"))
        self.assertTrue([k for k in refs if k.startswith("refs/piper/turns/%s/w" % s.id)])
        # the agent removes the worktree and adds another under the same name: its turns start anew
        git(self.root, "worktree", "remove", "--force", wt)
        git(self.root, "branch", "-D", "agent-turns2")
        write(self.root, "README.md", "main moved on\n")
        git(self.root, "commit", "-q", "-am", "main moved")
        git(self.root, "worktree", "add", "-q", "-b", "agent-turns2", wt)
        self._event(s, "UserPromptSubmit")
        write(wt, "other.txt", "fresh\n")
        self._event(s, "Stop")
        out = self.m.turns(tid)
        self.assertEqual([t["n"] for t in out["turns"]], [3])   # turns 1-2 belonged to the removed one
        self.assertEqual(out["turns"][0]["summary"]["files"], 1)
        # an expired session's worktree stores go with its own
        store = os.path.join(s.dir, "trees", "turns2", "snap-objects")
        self.assertTrue(os.path.isdir(store))
        self.m._drop_turn_snapshots(s)
        self.assertFalse(os.path.exists(store))


if __name__ == "__main__":
    unittest.main()
