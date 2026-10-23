"""Files redesign, Manager side: create / rename / delete / restore
under fs_write's rules, git grep with the hidden filter, the files an agent edits (PostToolUse),
IntelliJ rename / usages, the turns that changed one file (piperd routes: test_piperd)."""
import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import projectfs
import sessions
import guard
import feed
import workbench
from test_workbench97 import git, wait, write

HERE = os.path.dirname(os.path.abspath(__file__))
EVENT_PY = os.path.join(HERE, "..", "..", "piper", "sandbox", "piper-event.py")


def case_insensitive(d: str) -> bool:
    """APFS by default: SRC opens src/."""
    return os.path.isdir(os.path.join(d, "SRC"))


class FilesOpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        cls.m = _env.manager(cls)
        cls.root = root = os.path.realpath(_env.make_project("fops", {
            "src/A.java": "class A {\n  int alpha;\n}\n",
            "config/secrets/db.properties": "db.password=SECRET\n",
            "notes.txt": "private notes needle SECRET\n",
            ".hole/settings.json": '{"files": {"exclude": ["notes.txt", "vault/keep.txt"]}}',
            ".gitignore": "development/\nnode_modules/\n",
            "README.md": "# Readme needle\n"}))
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.com")
        git(root, "config", "user.name", "Tester")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        cls.key = "team-x/fops"
        _env.register(cls.key, root)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        git(self.root, "checkout", "-q", "--", ".")
        for d in ("box", "vault", "new", "moved", "development", "node_modules", "a:b", "linkdir"):
            p = os.path.join(self.root, d)
            if os.path.islink(p):
                os.unlink(p)
            else:
                shutil.rmtree(p, ignore_errors=True)
        git(self.root, "clean", "-qfd", "--", "src")

    def _session(self, **kw):
        s = sessions.Session(**dict({"project": self.key, "path": self.root, "state": "waiting",
                                     "title": "fops"}, **kw))
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        return s

    def _read(self, rel):
        with open(os.path.join(self.root, rel), "rb") as f:
            return f.read()

    # ------------------------------------------------------------ create
    def test_create_file_and_dir(self):
        r = self.m.fs_create(self.key, "src/New.java", "file", "class New {}\n")
        self.assertEqual(r, {"path": "src/New.java", "sha": hashlib.sha256(b"class New {}\n").hexdigest()})
        self.assertEqual(self._read("src/New.java"), b"class New {}\n")
        self.assertFalse([n for n in os.listdir(os.path.join(self.root, "src")) if n.startswith(".piperd-")])
        ev = common.audit_tail(1)[0]
        self.assertEqual({k: ev[k] for k in ("event", "project", "path", "kind")},
                         {"event": "gui-create", "project": self.key, "path": "src/New.java", "kind": "file"})
        self.assertNotIn("class New", json.dumps(ev))                     # never the content
        self.assertEqual(self.m.fs_create(self.key, "new", "dir"), {"path": "new"})
        self.assertTrue(os.path.isdir(os.path.join(self.root, "new")))
        self.assertEqual(self.m.fs_create(self.key, "new/empty.txt", "file")["sha"], hashlib.sha256(b"").hexdigest())
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.fs_create(self.key, "src/New.java", "file", "other")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.fs_create(self.key, "new", "dir")
        self.assertEqual(self._read("src/New.java"), b"class New {}\n")   # never overwritten

    def test_create_refusals(self):
        for bad in (".git/x", ".GIT/hooks/x", "../fops2/x", "/etc/x", "a//b", "a/./b", "src/", "", "x\0y", "src/../x"):
            with self.assertRaises(projectfs.FsError, msg=repr(bad)):
                self.m.fs_create(self.key, bad, "file", "x")
        with self.assertRaisesRegex(ValueError, "255 bytes"):
            self.m.fs_create(self.key, "src/" + "é" * 128, "file")           # 256 bytes
        with self.assertRaisesRegex(projectfs.FsError, "does not exist"):
            self.m.fs_create(self.key, "missing/x.txt", "file")              # the parent must exist
        with self.assertRaises(ValueError):
            self.m.fs_create(self.key, "src/x", "symlink")
        with self.assertRaises(ValueError):
            self.m.fs_create(self.key, "src/x", "dir", "text")
        with self.assertRaises(KeyError):
            self.m.fs_create("team-x/nope", "x", "file")
        os.symlink("src", os.path.join(self.root, "linkdir"))               # a symlinked parent
        with self.assertRaisesRegex(projectfs.FsError, "symbolic link"):
            self.m.fs_create(self.key, "linkdir/x.txt", "file")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/x.txt")))
        if case_insensitive(self.root):   # APFS: CONFIG resolves to config/ - refused, spelled as on disk only
            with self.assertRaises(projectfs.FsError):
                self.m.fs_create(self.key, "SRC/x.txt", "file")
        w = self._session(state="working")   # a session at work: no reason to wait (see test_ops_while_sessions_live)
        self.assertEqual(self.m.fs_create(self.key, "src/W.java", "file")["path"], "src/W.java")
        w.state = "exited"
        self.assertEqual(self.m.fs_create(self.key, "config/secrets/x.properties", "file", "k=v")["path"],
                         "config/secrets/x.properties")
        os.unlink(os.path.join(self.root, "config/secrets/x.properties"))

    # ------------------------------------------------------------ rename
    def test_rename_file_and_dir(self):
        self.assertEqual(self.m.fs_rename(self.key, "src/A.java", "src/B.java"), {"from": "src/A.java", "to": "src/B.java"})
        self.assertEqual(self._read("src/B.java"), b"class A {\n  int alpha;\n}\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/A.java")))
        ev = common.audit_tail(1)[0]
        self.assertEqual({k: ev[k] for k in ("event", "from", "to")}, {"event": "gui-rename", "from": "src/A.java", "to": "src/B.java"})
        os.makedirs(os.path.join(self.root, "box"))
        self.m.fs_rename(self.key, "src/B.java", "box/B.java")               # into another directory
        self.m.fs_rename(self.key, "box", "moved")                            # a directory
        self.assertTrue(os.path.isfile(os.path.join(self.root, "moved/B.java")))
        if case_insensitive(self.root):
            self.m.fs_rename(self.key, "moved/B.java", "moved/b.java")        # a change of case only
            self.assertIn("b.java", os.listdir(os.path.join(self.root, "moved")))

    def test_rename_refusals(self):
        write(self.root, "src/C.java", "c\n")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.fs_rename(self.key, "src/C.java", "README.md")
        for src, dst in (("README.md", ".git/R"), ("README.md", "../x"), ("src/C.java", "src/x/../C2")):
            with self.assertRaises(projectfs.FsError, msg=(src, dst)):
                self.m.fs_rename(self.key, src, dst)
        # content hidden from Claude never moves to where a session sees it (a file, a directory holding one)
        for src, dst in (("notes.txt", "src/n.txt"), ("config/secrets", "cfg"), ("config/secrets/db.properties", "db.properties")):
            with self.assertRaisesRegex(ValueError, "would be shown to every session, the future ones too", msg=(src, dst)):
                self.m.fs_rename(self.key, src, dst)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "config/secrets/db.properties")))
        # hidden -> hidden and visible -> hidden: fine without a sandbox of the tree
        self.m.fs_rename(self.key, "config/secrets/db.properties", "config/secrets/db2.properties")
        self.m.fs_rename(self.key, "config/secrets/db2.properties", "config/secrets/db.properties")
        write(self.root, "box/keep.txt", "k\n")
        self.m.fs_rename(self.key, "box", "vault")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "vault/keep.txt")))
        with self.assertRaisesRegex(ValueError, "into itself"):
            self.m.fs_rename(self.key, "box", "box/inner")
        with self.assertRaisesRegex(projectfs.FsError, "does not exist"):
            self.m.fs_rename(self.key, "src/Missing.java", "src/M.java")
        with self.assertRaisesRegex(projectfs.FsError, "does not exist"):
            self.m.fs_rename(self.key, "src/C.java", "missing/C.java")
        os.symlink("../README.md", os.path.join(self.root, "src/link.md"))   # K13: the link itself moves
        self.m.fs_rename(self.key, "src/link.md", "src/link2.md")
        self.assertEqual(os.readlink(os.path.join(self.root, "src/link2.md")), "../README.md")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "README.md")))   # its target stays
        os.unlink(os.path.join(self.root, "src/link2.md"))
        os.symlink("src", os.path.join(self.root, "linkdir"))
        with self.assertRaises(projectfs.FsError):
            self.m.fs_rename(self.key, "linkdir/C.java", "src/C3.java")
        with self.assertRaises(projectfs.FsError):
            self.m.fs_rename(self.key, "src/C.java", "linkdir/C3.java")
        if case_insensitive(self.root):
            with self.assertRaises(projectfs.FsError):
                self.m.fs_rename(self.key, "CONFIG", "cfg")                   # past the exclude by letter case
            with self.assertRaises(projectfs.FsError):
                self.m.fs_rename(self.key, "SRC/C.java", "src/C4.java")
        w = self._session(state="working", since=time.time() - 5)
        w.fsedits.append({"path": "src/C.java", "ts": time.time()})   # Claude's file of this turn
        with self.assertRaisesRegex(ValueError, "current turn"):
            self.m.fs_rename(self.key, "src/C.java", "src/D.java")
        w.state = "waiting"

    # ------------------------------------------------------------ delete / restore
    def test_delete_to_trash_and_restore(self):
        write(self.root, "src/T.java", "trash me\n")
        r = self.m.fs_delete(self.key, "src/T.java")
        self.assertRegex(r["trash"], workbench.TRASH_ID.pattern)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/T.java")))
        tdir = os.path.join(workbench.TRASH_DIR, r["trash"])
        with open(os.path.join(tdir, "item"), "rb") as f:
            self.assertEqual(f.read(), b"trash me\n")
        meta = common.read_json(os.path.join(tdir, "meta.json"), {})
        self.assertEqual((meta["project"], meta["path"], meta["kind"]), (self.key, "src/T.java", "file"))
        self.assertEqual(common.audit_tail(1)[0]["event"], "gui-delete")
        # restore: refused while the path is taken, then back
        write(self.root, "src/T.java", "new one\n")
        with self.assertRaisesRegex(ValueError, "exists again"):
            self.m.fs_restore(self.key, r["trash"])
        os.unlink(os.path.join(self.root, "src/T.java"))
        self.assertEqual(self.m.fs_restore(self.key, r["trash"]), {"path": "src/T.java"})
        self.assertEqual(self._read("src/T.java"), b"trash me\n")
        self.assertFalse(os.path.exists(tdir))
        self.assertEqual(common.audit_tail(1)[0]["event"], "gui-restore")
        with self.assertRaises(projectfs.FsError):
            self.m.fs_restore(self.key, r["trash"])                         # gone
        # a directory
        write(self.root, "box/a.txt", "a\n")
        d = self.m.fs_delete(self.key, "box")["trash"]
        self.assertFalse(os.path.exists(os.path.join(self.root, "box")))
        self.assertEqual(self.m.fs_restore(self.key, d), {"path": "box"})
        self.assertTrue(os.path.isfile(os.path.join(self.root, "box/a.txt")))

    def test_delete_and_restore_refusals(self):
        for bad in (".git", ".git/HEAD", "../fops", ""):
            with self.assertRaises(projectfs.FsError, msg=bad):
                self.m.fs_delete(self.key, bad)
        # a directory holding a file hidden from Claude: deleted and restored (sandboxes: test_ops_while_sessions_live)
        write(self.root, "vault/keep.txt", "hidden by vault/keep.txt\n")
        v = self.m.fs_delete(self.key, "vault")["trash"]
        self.assertFalse(os.path.exists(os.path.join(self.root, "vault")))
        self.assertEqual(self.m.fs_restore(self.key, v), {"path": "vault"})
        self.assertTrue(os.path.isfile(os.path.join(self.root, "vault/keep.txt")))
        os.symlink("../README.md", os.path.join(self.root, "src/link.md"))   # K13: the link, never its target
        lt = self.m.fs_delete(self.key, "src/link.md")["trash"]
        self.assertFalse(os.path.lexists(os.path.join(self.root, "src/link.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "README.md")))
        self.assertEqual(common.read_json(os.path.join(workbench.TRASH_DIR, lt, "meta.json"), {})["kind"], "link")
        self.m.fs_restore(self.key, lt)
        self.assertEqual(os.readlink(os.path.join(self.root, "src/link.md")), "../README.md")
        os.unlink(os.path.join(self.root, "src/link.md"))
        for bad in ("../x", "x", "20261006-120000-0123abc", "20261006-120000-0123abcd/../../x"):
            with self.assertRaises(projectfs.FsError, msg=bad):
                self.m.fs_restore(self.key, bad)
        # EXDEV (the trash on another volume): refused, nothing moved, no trash entry left
        n = len(os.listdir(workbench.TRASH_DIR)) if os.path.isdir(workbench.TRASH_DIR) else 0
        real = os.rename

        def exdev(a, b, **kw):
            if str(b).endswith("/item"):
                raise OSError(errno.EXDEV, "cross-device link")
            return real(a, b, **kw)
        with mock.patch.object(workbench.os, "rename", exdev):
            with self.assertRaisesRegex(ValueError, "another volume"):
                self.m.fs_delete(self.key, "README.md")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "README.md")))
        self.assertEqual(len(os.listdir(workbench.TRASH_DIR)), n)
        # another project's trash entry, a path that is hidden by now
        write(self.root, "src/H.java", "h\n")
        t = self.m.fs_delete(self.key, "src/H.java")["trash"]
        other = os.path.realpath(_env.make_project("fops2", {"x.txt": "x\n"}))
        _env.register("team-x/fops2", other)
        with self.assertRaises(projectfs.FsError):
            self.m.fs_restore("team-x/fops2", t)
        orig = self.m.project_hidden
        live = self._session(state="ready", coverage=self._cov([]))
        with mock.patch.object(self.m, "project_hidden", lambda p: (lambda r: r.startswith("src/H") or orig(p)(r))):
            with self.assertRaisesRegex(ValueError, "would see it there"):   # hidden by now, a sandbox runs
                self.m.fs_restore(self.key, t)
        live.state = "exited"
        self.m.fs_restore(self.key, t)

    def test_ops_while_sessions_live(self):
        """2026-10-08: create / rename / delete / restore also while sessions live. Refused only: a path Claude
        edited in its current turn, and hidden content landing where a running sandbox would see it - over-mounts
        are fixed at its start, only an over-mounted DIRECTORY shields what appears inside it later."""
        self.addCleanup(git, self.root, "clean", "-qfd", "--", "config", "vault")
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        write(self.root, "vault/keep.txt", "hidden by vault/keep.txt\n")
        s = self._session(state="working", since=time.time() - 60,
                          coverage=self._cov(["config/secrets", "notes.txt"]))
        # visible files while Claude works: fine, unless it edited them in this turn
        self.m.fs_create(self.key, "src/W.java", "file", "w")
        self.m.fs_rename(self.key, "src/W.java", "src/W2.java")
        s.fsedits.append({"path": "src/A.java", "ts": time.time()})
        s.fsedits.append({"path": "README.md", "ts": time.time() - 3600})   # an earlier turn
        for op in (lambda: self.m.fs_rename(self.key, "src/A.java", "src/A2.java"),
                   lambda: self.m.fs_delete(self.key, "src"),                 # a directory holding it
                   lambda: self.m.fs_rename(self.key, "src/W2.java", "src/A.java")):
            with self.assertRaisesRegex(ValueError, "edited src/A.java in its current turn"):
                op()
        self.m.fs_delete(self.key, "README.md")
        # inside the over-mounted directory: invisible to the sandbox whatever happens there
        self.m.fs_create(self.key, "config/secrets/new.properties", "file", "k=v")
        self.m.fs_rename(self.key, "config/secrets/db.properties", "config/secrets/db2.properties")
        self.m.fs_rename(self.key, "notes.txt", "config/secrets/notes.txt")   # a file mountpoint moved in
        self.assertEqual(s.coverage[0]["detached"], ["notes.txt"])               # ... its over-mount is gone
        # hidden content where the sandbox would see it: refused
        with self.assertRaisesRegex(ValueError, "would see it there"):   # a file over-mount shields nothing new
            self.m.fs_create(self.key, "notes.txt", "file", "x")
        self.m.fs_create(self.key, "vault/new.txt", "file", "x")             # not hidden: just a file
        write(self.root, ".hole/settings.json", '{"files": {"exclude": ["notes.txt", "vault/keep.txt", "vault/k2.txt"]}}')
        self.addCleanup(write, self.root, ".hole/settings.json",
                        '{"files": {"exclude": ["notes.txt", "vault/keep.txt"]}}')
        with self.assertRaisesRegex(ValueError, "would see it there"):   # hidden -> hidden, not shielded
            self.m.fs_rename(self.key, "config/secrets/new.properties", "vault/k2.txt")
        with self.assertRaisesRegex(ValueError, "sees it"):   # vault/keep.txt: hidden, but not over-mounted there
            self.m.fs_write(self.key, "vault/keep.txt", "new secret", self._sha("vault/keep.txt"))
        self.m.fs_rename(self.key, "src/W2.java", "vault/k2.txt")   # visible content: nothing new for it
        # deleting shows nothing: fine, also a mountpoint (recorded) - restoring it is new content again
        t = self.m.fs_delete(self.key, "config/secrets")["trash"]
        self.assertEqual(s.coverage[0]["detached"], ["config/secrets", "notes.txt"])
        self.m.fs_delete(self.key, "vault/keep.txt")
        with self.assertRaisesRegex(ValueError, "would see it there"):
            self.m.fs_restore(self.key, t)
        # before its isolation check finished, a session's view is unknown: no hidden path appears
        s.state, starting = "exited", self._session(state="preflight")
        with self.assertRaisesRegex(ValueError, "isolation check has not finished"):
            self.m.fs_restore(self.key, t)
        starting.state = "exited"
        self.assertEqual(self.m.fs_restore(self.key, t), {"path": "config/secrets"})

    def test_l5_counts_a_detached_over_mount_as_uncovered(self):
        """After the GUI moved a mountpoint away, a sensitive file that reappears there (an IDE, a shell)
        is visible in the sandbox: L5 must see it."""
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        self.m._watch(s)
        self.assertEqual(s.watch, [])
        s.coverage[0]["detached"] = ["config/secrets"]
        self.m._watch(s)
        self.assertTrue([w for w in s.watch if w.endswith("config/secrets")], s.watch)
        # L5 is a security item of the feed, not an activity state (the session keeps waiting)
        self.assertEqual(s.state, "waiting")
        self.assertIn("security", [a["level"] for a in feed.derive_actions([feed.session_snap(s)], [], [], {}, {}, time.time(), 0)])

    def test_a_held_mountpoint_is_explained(self):
        """Docker Desktop refuses to move a directory a running container over-mounts (verified: EACCES)."""
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        with mock.patch.object(workbench.os, "rename", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(ValueError, "over-mounted by the sandbox of session"):
                self.m.fs_delete(self.key, "config/secrets")
        self.assertNotIn("detached", s.coverage[0])   # nothing moved: nothing recorded
        self.assertTrue(os.path.isfile(os.path.join(self.root, "config/secrets/db.properties")))

    def test_unreadable_policy_and_audit(self):
        """S2: without the base hole settings nothing can say what is hidden - one flag in the GUI, no move,
        nothing new while a sandbox runs; S7: the operations' audit says whether a hidden path was touched."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.m.fs_rename(self.key, "README.md", "R2.md")
        ev = common.audit_tail(1)[0]
        self.assertEqual((ev["hidden"], ev["hidden_from"], ev["hidden_to"]), (False, False, False))
        self.m.fs_rename(self.key, "R2.md", "README.md")
        self.m.fs_create(self.key, "config/secrets/a.properties", "file", "x")
        self.assertTrue(common.audit_tail(1)[0]["hidden"])
        t = self.m.fs_delete(self.key, "config/secrets/a.properties")["trash"]
        self.assertTrue(common.audit_tail(1)[0]["hidden"])
        self.m.fs_restore(self.key, t)
        self.assertTrue(common.audit_tail(1)[0]["hidden"])
        os.unlink(os.path.join(self.root, "config/secrets/a.properties"))
        with mock.patch.object(self.m, "_base_settings", side_effect=ValueError("broken base")):
            ls = self.m.fs(self.key, "list", {"path": "."})
            self.assertEqual((ls["policy_unreadable"], ls["policy_error"]),
                             (True, "the base hole settings cannot be read: broken base"))
            self.assertFalse([e for e in ls["entries"] if e["hidden"]])        # one flag, not every file marked
            self.assertFalse(self.m.fs(self.key, "read", {"path": "notes.txt"})["hidden"])
            self.assertTrue(sessions.policy_unreadable(self.m.project_hidden(self.m.gui_project(self.key))))
            with self.assertRaisesRegex(ValueError, "isolation policy unreadable .* does not move files"):
                self.m.fs_rename(self.key, "src/A.java", "src/A2.java")
            self.m.fs_create(self.key, "src/N.java", "file", "n")              # no sandbox: adding is fine
            live = self._session(state="waiting", coverage=self._cov([]))
            with self.assertRaisesRegex(ValueError, "add files while a session runs"):
                self.m.fs_create(self.key, "src/N2.java", "file", "n")
            self.m.fs_delete(self.key, "src/N.java")                          # deleting shows nothing
            live.state = "exited"
        self.assertFalse(self.m.fs(self.key, "list", {"path": "."})["policy_unreadable"])

    def _cov(self, over):
        """coverage as the preflight records it: the over-mounts and their identities pinned at the start"""
        return [{"root": self.root, "overmounts": list(over), "ids": guard.overmount_ids(self.root, over)}]

    def test_a_stand_in_for_a_moved_mountpoint_shields_nothing(self):
        """Reproduced with Docker Desktop: the sandbox moves the PARENT of an over-mounted
        directory (the mount travels with it) and creates a plain directory at the old path - the host sees
        config/secrets as a directory again. Only the identity pinned at the start tells them apart."""
        self.addCleanup(git, self.root, "clean", "-qfd", "--", "config", "config_old")
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        os.rename(os.path.join(self.root, "config"), os.path.join(self.root, "config_old"))   # what the sandbox does
        write(self.root, "config/secrets/db.properties", "planted by the sandbox\n")
        with self.assertRaisesRegex(ValueError, "would see it there"):
            self.m.fs_create(self.key, "config/secrets/new.properties", "file", "SECRET")
        with self.assertRaisesRegex(ValueError, "sees it"):   # editing the stand-in would hand it the secret
            self.m.fs_write(self.key, "config/secrets/db.properties", "db.password=SECRET",
                            self._sha("config/secrets/db.properties"))
        self.m._watch(s)                                      # L5: the path is uncovered now
        self.assertTrue([w for w in s.watch if w.endswith("config/secrets")], s.watch)
        old = self._session(state="waiting", coverage=[{"root": self.root, "overmounts": ["config/secrets"]}])
        s.state = "exited"                                    # no identities pinned (an older session): nothing shields
        os.rename(os.path.join(self.root, "config"), os.path.join(self.root, "config_x"))
        os.rename(os.path.join(self.root, "config_old"), os.path.join(self.root, "config"))
        shutil.rmtree(os.path.join(self.root, "config_x"))
        with self.assertRaisesRegex(ValueError, "would see it there"):
            self.m.fs_create(self.key, "config/secrets/new.properties", "file", "SECRET")
        old.state = "exited"

    def test_a_restore_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """N10 review of fs_restore (the GUI's way back from Piper's trash, where the N9 net also puts originals):
        it opened the destination directory by path, THEN _ops_guard checked the pinned over-mount by path. A
        sandbox that swaps a stand-in in (mv config config_old; mkdir -p config/secrets) and the real directory
        back between the two got the restored hidden file into the stand-in - where it reads it."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(self.root, d), ignore_errors=True)
                                 for d in ("config_old", "config_stand")])
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        self.m.fs_create(self.key, "config/secrets/r.properties", "file", "pw=PLANTED_R\n")   # shielded: fine
        t = self.m.fs_delete(self.key, "config/secrets/r.properties")["trash"]
        real = workbench.Workbench._ops_dir
        root = self.root

        def racing(r, parts):
            os.rename(os.path.join(root, "config"), os.path.join(root, "config_old"))      # the sandbox: a stand-in
            os.makedirs(os.path.join(root, "config", "secrets"))
            fd = real(r, parts)                                                            # the host opens it
            os.rename(os.path.join(root, "config"), os.path.join(root, "config_stand"))    # the real one back
            os.rename(os.path.join(root, "config_old"), os.path.join(root, "config"))
            return fd
        with mock.patch.object(self.m, "_ops_dir", racing):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_restore(self.key, t)
        self.assertFalse(os.path.exists(os.path.join(root, "config_stand", "secrets", "r.properties")))
        self.assertTrue(os.path.isdir(os.path.join(workbench.TRASH_DIR, t)))   # still in the trash
        shutil.rmtree(os.path.join(root, "config_stand"))
        self.assertEqual(self.m.fs_restore(self.key, t), {"path": "config/secrets/r.properties"})   # no race: back
        os.unlink(os.path.join(root, "config/secrets/r.properties"))
        s.state = "exited"

    def _net_entry(self, rel, data):
        """a trash entry as the git_run net leaves an original it could not put back (sessions._net_trash)"""
        tid = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(4).hex()
        d = os.path.join(workbench.TRASH_DIR, tid)
        os.makedirs(d, 0o700)
        with open(os.path.join(d, "item"), "wb") as f:
            f.write(data)
        common.write_json(os.path.join(d, "meta.json"), {"project": self.key, "path": rel, "at": time.time(),
                                                         "kind": "file", "from": "git_run the original: s"})
        self.addCleanup(shutil.rmtree, d, True)
        return tid

    def test_restore_replace_writes_the_original_over_the_file_in_place(self):
        """2026-10-10 (the net-failed row's Restore): fs_restore(replace) puts the trash item over the existing hidden
        file - in place (the inode an over-mount may hold stays, no tmp next to it), mode kept, audited; while a
        session runs only inside its pinned over-mounted directory. Refused: no longer hidden, not a regular file,
        an unknown entry. A free path is restored as without replace."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        rel = "config/secrets/db.properties"
        full = os.path.join(self.root, rel)
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        with open(full, "w") as f:
            f.write("db.url=attacker\n")     # what the run left there
        os.chmod(full, 0o640)
        ino = os.stat(full).st_ino
        tid = self._net_entry(rel, b"db.password=SECRET\n")
        self.assertEqual(self.m.fs_restore(self.key, tid, replace=True), {"path": rel, "replaced": True})
        with open(full) as f:
            self.assertEqual(f.read(), "db.password=SECRET\n")
        st = os.stat(full)
        self.assertEqual((st.st_ino, st.st_mode & 0o777), (ino, 0o640))
        self.assertFalse(os.path.exists(os.path.join(workbench.TRASH_DIR, tid)))
        self.assertEqual({k: common.audit_tail(1)[0][k] for k in ("event", "path", "trash", "replace", "hidden")},
                         {"event": "gui-restore", "path": rel, "trash": tid, "replace": True, "hidden": True})
        with self.assertRaises(projectfs.FsError):   # gone now
            self.m.fs_restore(self.key, tid, replace=True)
        # README.md is not hidden (the rule changed since): the original is never written where the agent reads it
        tid = self._net_entry("README.md", b"secret original\n")
        with self.assertRaisesRegex(ValueError, "no longer hidden from Claude"):
            self.m.fs_restore(self.key, tid, replace=True)
        self.assertTrue(os.path.isfile(os.path.join(workbench.TRASH_DIR, tid, "item")))
        # not a regular file at the path
        s.state = "exited"
        tid = self._net_entry("config/secrets/d", b"x\n")
        os.makedirs(os.path.join(self.root, "config/secrets/d"))
        self.addCleanup(shutil.rmtree, os.path.join(self.root, "config/secrets/d"), True)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.m.fs_restore(self.key, tid, replace=True)
        # nothing at the path: back as an ordinary restore
        tid = self._net_entry("config/secrets/gone.properties", b"pw=back\n")
        self.assertEqual(self.m.fs_restore(self.key, tid, replace=True), {"path": "config/secrets/gone.properties"})
        os.unlink(os.path.join(self.root, "config/secrets/gone.properties"))
        # another project's entry: unknown here
        tid = self._net_entry(rel, b"x\n")
        common.write_json(os.path.join(workbench.TRASH_DIR, tid, "meta.json"), {"project": "team-x/other", "path": rel,
                                                                               "at": time.time(), "kind": "file"})
        with self.assertRaises(projectfs.FsError):
            self.m.fs_restore(self.key, tid, replace=True)

    def test_the_net_row_goes_only_when_every_failed_file_is_back(self):
        """feed_net_restored: a restored entry leaves the row; the row goes with the last one - unless a failed
        file had no original to restore (a file the run created), then it stays until dismissed."""
        s = self._session(state="exited")
        off = dict(feed.load_config(), enabled=False)
        with mock.patch.object(feed, "load_config", lambda: off):
            rows = lambda: [a for a in self.m.queue_summary()["actions"] if a["key"].startswith("net-failed:%s:" % s.id)]
            self.m.feed_net_failed(s, ["merge", "x"], ["a", "b"], ["t1", "t2"], restore=[{"id": "t1", "path": "a"},
                                                                                        {"id": "t2", "path": "b"}])
            self.assertEqual([(r["project"], r["trash"]) for r in rows()],
                             [(self.key, [{"id": "t1", "path": "a"}, {"id": "t2", "path": "b"}])])
            self.m.feed_net_restored("t1")
            self.assertEqual([r["trash"] for r in rows()], [[{"id": "t2", "path": "b"}]])
            self.m.feed_net_restored("t2")
            self.assertEqual(rows(), [])
            self.m.feed_net_failed(s, ["merge", "x"], ["a", "new"], ["t3"], restore=[{"id": "t3", "path": "a"}])
            self.m.feed_net_restored("t3")
            self.assertEqual(len(rows()), 1)                   # "new" has no original: the human checks it
            self.assertNotIn("trash", rows()[0])               # nothing left to restore: no button
            self.m._allowed_failed = [x for x in self.m._allowed_failed if x["session"] != s.id]

    def test_restore_replace_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """fs_restore(replace) checks the over-mount by path (_hidden_write_guard), then writes through fds opened by
        path - a stand-in swapped in between must not get the original: the pinned fd check refuses, before a byte
        is written, no redo journal left, the entry stays in the trash. A live session without a pinned shield:
        refused too."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.addCleanup(self._swap_out)
        rel = "config/secrets/db.properties"
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        tid = self._net_entry(rel, b"db.password=ORIGINAL_N12\n")
        real = self.m._hidden_write_guard

        def racing(root, r):
            real(root, r)
            self._swap_in(file=rel)
        jr = self._journal()
        with mock.patch.object(self.m, "_hidden_write_guard", racing):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_restore(self.key, tid, replace=True)
        with open(os.path.join(self.root, rel)) as f:
            self.assertEqual(f.read(), "stand-in\n")   # the stand-in never got it
        self.assertEqual(self._journal(), jr)
        self.assertTrue(os.path.isfile(os.path.join(workbench.TRASH_DIR, tid, "item")))
        self._swap_out()
        s.coverage = []   # no pinned identities: Piper cannot tell what the sandbox sees
        with self.assertRaisesRegex(ValueError, "isolation check of session .* has not finished"):
            self.m.fs_restore(self.key, tid, replace=True)
        s.state = "exited"

    # N10 wave 2: the same class in every other GUI write of a hidden file while a session runs - the check
    # by path, then the write through fds opened by path (a stand-in swapped in between the two)
    def _swap_in(self, d="config", stand="config/secrets", file=None):
        """What the sandbox does: the real `d` to d_old (an over-mount travels with its parent), a plain stand-in
        at the old path. Undone by _swap_out."""
        os.rename(os.path.join(self.root, d), os.path.join(self.root, d + "_old"))
        os.makedirs(os.path.join(self.root, stand))
        if file:
            write(self.root, file, "stand-in\n")

    def _swap_out(self, d="config"):
        if os.path.isdir(os.path.join(self.root, d + "_old")):
            shutil.rmtree(os.path.join(self.root, d), ignore_errors=True)
            os.rename(os.path.join(self.root, d + "_old"), os.path.join(self.root, d))

    def _journal(self):
        return sorted(os.listdir(common.JOURNAL_DIR)) if os.path.isdir(common.JOURNAL_DIR) else []

    def test_a_hidden_save_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """fs_write of a hidden file: _hidden_write_guard checked the over-mount by path, overwrite_in_place then
        opened the file by path - a stand-in swapped in between got the human's hidden text. Now the fds the
        write goes through are checked (inside a pinned over-mounted directory, or the pinned over-mounted file
        itself); refused before a byte is written, no redo journal left behind."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.addCleanup(self._swap_out)
        rel = "config/secrets/db.properties"
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        real = self.m._hidden_write_guard

        def racing(root, r):
            real(root, r)
            self._swap_in(file=rel)
        jr = self._journal()
        with mock.patch.object(self.m, "_hidden_write_guard", racing):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_write(self.key, rel, "db.password=NEW_SECRET", self._sha(rel))
        self.assertEqual(self._read(rel), b"stand-in\n")                       # the stand-in got nothing
        self.assertEqual(self._journal(), jr)                                  # nothing to redo at the next start
        self._swap_out()
        self.assertEqual(self._read(rel), b"db.password=SECRET\n")
        self.m.fs_write(self.key, rel, "db.password=NEW", self._sha(rel))       # no race: written
        self.assertEqual(self._read(rel), b"db.password=NEW\n")
        s.state = "exited"

    def test_a_save_of_an_over_mounted_file_checks_the_file_it_opened(self):
        """A FILE over-mount (vault/keep.txt): the sandbox moves its parent (the mount goes along) and puts a
        stand-in file at the path after the check - the opened file is not the pinned one: refused."""
        self.addCleanup(shutil.rmtree, os.path.join(self.root, "vault_old"), True)
        rel = "vault/keep.txt"
        write(self.root, rel, "kept=SECRET\n")
        s = self._session(state="waiting", coverage=self._cov([rel]))
        real = self.m._hidden_write_guard

        def racing(root, r):
            real(root, r)
            self._swap_in("vault", "vault", file=rel)
        with mock.patch.object(self.m, "_hidden_write_guard", racing):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_write(self.key, rel, "kept=NEW_SECRET", self._sha(rel))
        self.assertEqual(self._read(rel), b"stand-in\n")
        self._swap_out("vault")
        self.m.fs_write(self.key, rel, "kept=NEW", self._sha(rel))             # the pinned file itself: written
        self.assertEqual(self._read(rel), b"kept=NEW\n")
        s.state = "exited"

    def test_a_hidden_create_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """fs_create of a hidden file or directory: _ops_guard by path, then the parent opened by path."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.addCleanup(self._swap_out)
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        real = self.m._ops_guard

        def racing(p, root, pairs):
            out = real(p, root, pairs)
            self._swap_in()
            return out
        for kind, rel in (("file", "config/secrets/new.properties"), ("dir", "config/secrets/newdir")):
            with mock.patch.object(self.m, "_ops_guard", racing):
                with self.assertRaisesRegex(ValueError, "would see it there", msg=kind):
                    self.m.fs_create(self.key, rel, kind, "pw=NEW_SECRET" if kind == "file" else None)
            self.assertEqual(os.listdir(os.path.join(self.root, "config/secrets")), [], kind)   # the stand-in
            self._swap_out()
        r = self.m.fs_create(self.key, "config/secrets/new.properties", "file", "pw=1\n")   # no race: created
        self.assertEqual(r["path"], "config/secrets/new.properties")
        os.unlink(os.path.join(self.root, "config/secrets/new.properties"))
        s.state = "exited"

    def test_a_hidden_rename_never_lands_in_a_stand_in_swapped_in_meanwhile(self):
        """fs_rename: (a) the target directory opened by path, a stand-in at that moment and the real one back
        before _ops_guard looks; (b) the source taken from the pinned directory opened before a stand-in was put
        at its path - _ops_guard then counted the moved content as visible and let it land on a hidden path the
        sandbox sees. Hidden content now lands only through a pinned over-mounted directory."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self.addCleanup(self._swap_out)
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(self.root, d), ignore_errors=True)
                                 for d in ("config_stand", "vault")])
        rel = "config/secrets/db.properties"
        os.makedirs(os.path.join(self.root, "config/secrets/sub"))
        s = self._session(state="waiting", coverage=self._cov(["config/secrets"]))
        real = workbench.Workbench._ops_dir
        root = self.root

        def racing_dst(r, parts):   # (a)
            if parts != ["config", "secrets", "sub"]:
                return real(r, parts)
            self._swap_in(stand="config/secrets/sub")
            fd = real(r, parts)
            os.rename(os.path.join(root, "config"), os.path.join(root, "config_stand"))
            os.rename(os.path.join(root, "config_old"), os.path.join(root, "config"))
            return fd
        with mock.patch.object(self.m, "_ops_dir", racing_dst):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_rename(self.key, rel, "config/secrets/sub/db.properties")
        self.assertEqual(os.listdir(os.path.join(root, "config_stand/secrets/sub")), [])
        self.assertEqual(self._read(rel), b"db.password=SECRET\n")
        os.makedirs(os.path.join(root, "vault"))

        def racing_src(r, parts):   # (b): the source's directory is opened, then the stand-in goes in
            fd = real(r, parts)
            if parts == ["config", "secrets"]:
                self._swap_in(file=rel)
            return fd
        with mock.patch.object(self.m, "_ops_dir", racing_src):
            with self.assertRaisesRegex(ValueError, "would see it there"):
                self.m.fs_rename(self.key, rel, "vault/keep.txt")             # hidden, not over-mounted
        self.assertFalse(os.path.exists(os.path.join(root, "vault/keep.txt")))
        self._swap_out()
        self.assertEqual(self._read(rel), b"db.password=SECRET\n")
        self.m.fs_rename(self.key, rel, "config/secrets/sub/db.properties")   # no race: moved
        self.assertTrue(os.path.isfile(os.path.join(root, "config/secrets/sub/db.properties")))
        write(root, "src/vis.txt", "visible\n")                                # visible content: no shield needed
        self.m.fs_rename(self.key, "src/vis.txt", "vault/keep.txt")
        self.assertEqual(self._read("vault/keep.txt"), b"visible\n")
        s.state = "exited"

    def test_hidden_writes_with_no_live_sandbox_work_as_before(self):
        """No running session (or only ended ones): no pinned identities needed - the GUI edits, creates and
        renames hidden files as before."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        self._session(state="exited", coverage=[])
        rel = "config/secrets/db.properties"
        self.m.fs_write(self.key, rel, "db.password=E", self._sha(rel))
        self.assertEqual(self._read(rel), b"db.password=E\n")
        self.m.fs_create(self.key, "config/secrets/n.properties", "file", "n=1\n")
        self.m.fs_create(self.key, "config/secrets/d", "dir")
        self.m.fs_rename(self.key, "config/secrets/n.properties", "config/secrets/d/n.properties")
        self.assertEqual(self._read("config/secrets/d/n.properties"), b"n=1\n")
        shutil.rmtree(os.path.join(self.root, "config/secrets/d"))

    def test_hidden_write_waits_for_the_isolation_check(self):
        """review 2026-10-08: a GUI save of a hidden file (and an .http save_to) is refused like _ops_guard's
        create while a session's coverage is unknown - none yet, or a start before hole runs whose coverage
        is still the previous run's (_start replaces it only when its preflight returns)."""
        self.addCleanup(git, self.root, "checkout", "-q", "--", ".")
        stale = self._cov(["notes.txt", "config/secrets"])   # the previous run's: would say covered
        s = self._session(state="preflight", coverage=stale)
        for state in ("preflight", "preparing", "spawning"):
            s.state = state
            with self.assertRaisesRegex(ValueError, 'isolation check of session "fops" has not finished'):
                self.m.fs_write(self.key, "notes.txt", "new notes", self._sha("notes.txt"))
            with self.assertRaisesRegex(ValueError, "isolation check has not finished"):
                self.m.fs_create(self.key, "config/secrets/new.properties", "file", "k=v")
        s.state, s.coverage = "waiting", []                     # running, but no coverage recorded
        with self.assertRaisesRegex(ValueError, "has not finished"):
            self.m.fs_write(self.key, "notes.txt", "new notes", self._sha("notes.txt"))
        s.coverage = stale                                      # known and over-mounted: written
        self.m.fs_write(self.key, "notes.txt", "new notes", self._sha("notes.txt"))
        self.assertEqual(self._read("notes.txt"), b"new notes\n")
        s.state = "exited"                                      # no sandbox: written
        self.m.fs_write(self.key, "notes.txt", "newer", self._sha("notes.txt"))

    def _sha(self, rel):
        import hashlib
        with open(os.path.join(self.root, rel), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_trash_prune(self):
        os.makedirs(workbench.TRASH_DIR, exist_ok=True)
        old = os.path.join(workbench.TRASH_DIR, "20200101-000000-00000000")
        new = os.path.join(workbench.TRASH_DIR, "20200101-000000-00000001")
        for d, at in ((old, time.time() - 8 * 86400), (new, time.time() - 86400)):
            os.makedirs(d, exist_ok=True)
            common.write_json(os.path.join(d, "meta.json"), {"project": "x", "path": "y", "at": at})
        workbench.Workbench._trash_prune()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        shutil.rmtree(new)

    # ------------------------------------------------------------ git grep (L4)
    def _grep_tree(self):
        write(self.root, "src/Untracked.java", "// needle untracked\n")
        write(self.root, "development/analysis.md", "needle in the analyses\n")
        write(self.root, "node_modules/pkg/i.js", "needle in a dependency\n")
        write(self.root, "a:b/c:d.txt", "needle with colons\r\nÖl\n")
        write(self.root, "docs/de.md", "öl ist da\n")
        write(self.root, "src/bin.dat", b"needle\0binary", "wb")
        os.symlink("../README.md", os.path.join(self.root, "src/link.md"))   # a symlinked file
        os.symlink("src", os.path.join(self.root, "linkdir"))                # a symlinked dir

    def test_git_grep_matches_the_python_walker(self):
        self._grep_tree()
        hidden = self.m.project_hidden(self.m.gui_project(self.key))
        top = self.m.repo_top(self.root)
        self.assertEqual(top, self.root)
        g = projectfs.git_grep(self.root, top, "needle", hidden)
        p = projectfs.grep(self.root, "needle", hidden)
        self.assertEqual((g["engine"], p["engine"]), ("git", "python"))
        key = lambda r: sorted((m["path"], m["line"], m["text"]) for m in r["matches"])   # noqa: E731
        self.assertEqual(key(g), key(p))
        self.assertEqual([m["path"] for m in g["matches"]],
                         ["README.md", "a:b/c:d.txt", "development/analysis.md", "src/Untracked.java"])
        self.assertEqual(g["matches"][1]["text"], "needle with colons")       # \r dropped like split_lines
        self.assertFalse(g["truncated"])
        # case-insensitive beyond ASCII in both passes (git grep runs in a UTF-8 locale)
        g = projectfs.git_grep(self.root, top, "öl", hidden, ignore_case=True)
        p = projectfs.grep(self.root, "öl", hidden, ignore_case=True)
        self.assertEqual(key(g), key(p))
        self.assertEqual([m["path"] for m in g["matches"]], ["a:b/c:d.txt", "docs/de.md"])
        # the Manager uses git when the root is trusted; cs / limit
        r = self.m.fs(self.key, "grep", {"q": "NEEDLE"})
        self.assertEqual((r["engine"], r["matches"]), ("git", []))
        r = self.m.fs(self.key, "grep", {"q": "NEEDLE", "cs": "0"})
        self.assertEqual(len(r["matches"]), 5)                                  # notes.txt too: hidden from Claude only
        r = self.m.fs(self.key, "grep", {"q": "needle", "limit": "2"})
        self.assertEqual((len(r["matches"]), r["truncated"]), (2, True))
        r = self.m.fs(self.key, "grep", {"q": "needle", "path": "src"})
        self.assertEqual([m["path"] for m in r["matches"]], ["src/Untracked.java"])
        r = self.m.fs(self.key, "grep", {"q": "needle", "glob": "*.md"})
        self.assertEqual([m["path"] for m in r["matches"]], ["README.md", "development/analysis.md"])
        r = self.m.fs(self.key, "grep", {"q": "(?<=# Readme )needle"})        # PCRE
        self.assertEqual([m["path"] for m in r["matches"]], ["README.md"])
        with self.assertRaises(projectfs.FsError):
            self.m.fs(self.key, "grep", {"q": "x" * 501})
        r = self.m.fs(self.key, "grep", {"q": "needle", "path": "notes.txt"})   # hidden from Claude only
        self.assertEqual([m["path"] for m in r["matches"]], ["notes.txt"])
        with self.assertRaises(projectfs.FsError):
            self.m.fs(self.key, "grep", {"q": "needle", "path": ".git"})

    def test_git_grep_drops_a_tracked_dir_replaced_by_a_symlink(self):
        hidden = self.m.project_hidden(self.m.gui_project(self.key))
        # a tracked file whose directory the sandbox replaced by a link into a hidden directory
        os.rename(os.path.join(self.root, "src"), os.path.join(self.root, "src.real"))
        self.addCleanup(os.rename, os.path.join(self.root, "src.real"), os.path.join(self.root, "src"))
        os.symlink("config/secrets", os.path.join(self.root, "src"))
        self.addCleanup(os.unlink, os.path.join(self.root, "src"))
        r = projectfs.git_grep(self.root, self.root, "password|needle", hidden)
        self.assertFalse([m for m in r["matches"] if "SECRET" in m["text"]])
        self.assertFalse([m for m in r["matches"] if m["path"].startswith("src/")])

    def test_posix_classes_in_the_python_pass(self):
        """git understands [[:digit:]]; the walker over ignored paths gets the same meaning, or says
        its answer is partial when it cannot."""
        self.assertEqual(projectfs.posix_to_python("[[:digit:]]+"), "[\\d]+")
        self.assertEqual(projectfs.posix_to_python("a[[:space:]]b"), "a[\\s]b")
        # Unicode in git (PCRE2 UCP), ASCII if translated: not translated; an escaped [: neither
        for bad in ("\\Qa.b\\E", "[[:^digit:]]", "[[:foo:]]", "[[.a.]]", "caf[[:alpha:]]", "[[:upper:]]", "\\[:digit:]"):
            self.assertIsNone(projectfs.posix_to_python(bad), bad)
        repo = os.path.realpath(tempfile.mkdtemp(prefix="piper-posix-", dir=_env.PP))
        self.addCleanup(shutil.rmtree, repo, True)
        write(repo, ".gitignore", "a/\n")
        write(repo, "t.txt", "year 2026\n")
        write(repo, "a/q.txt", "year 2027\n")
        write(repo, "a/p.txt", "no numbers here :]\n")
        git(repo, "init", "-q", "-b", "main")
        _env.register("team-x/posix", repo)
        r = self.m.fs("team-x/posix", "grep", {"q": "[[:digit:]]+"})
        self.assertEqual([m["path"] for m in r["matches"]], ["a/q.txt", "t.txt"])
        self.assertTrue(self.m.fs("team-x/posix", "grep", {"q": "[[:^digit:]]{9}"})["truncated"])

    def test_grep_nested_repository(self):
        """git grep does not enter a nested repository: the Python pass searches it."""
        repo = os.path.realpath(tempfile.mkdtemp(prefix="piper-nest-", dir=_env.PP))
        self.addCleanup(shutil.rmtree, repo, True)
        write(repo, "a.txt", "needle top\n")
        write(repo, "inner/b.txt", "needle nested\n")
        git(repo, "init", "-q", "-b", "main")
        git(os.path.join(repo, "inner"), "init", "-q", "-b", "main")
        _env.register("team-x/nest", repo)
        write(repo, "vendor/lib/c.txt", "needle deep\n")     # a repository inside an untracked directory
        git(os.path.join(repo, "vendor/lib"), "init", "-q", "-b", "main")
        r = self.m.fs("team-x/nest", "grep", {"q": "needle"})
        self.assertEqual((r["engine"], [m["path"] for m in r["matches"]]), ("git", ["a.txt", "inner/b.txt", "vendor/lib/c.txt"]))
        # a worktree's .git FILE is never a hit; an ignored dir holding a repository: its matches once
        write(repo, "inner/wt/.git", "gitdir: /x/needle\n")
        write(repo, ".gitignore", "tmp/\n")
        write(repo, "u/tmp/r/d.txt", "needle twice?\n")
        git(os.path.join(repo, "u/tmp/r"), "init", "-q", "-b", "main")
        paths = [m["path"] for m in self.m.fs("team-x/nest", "grep", {"q": "needle"})["matches"]]
        self.assertNotIn("inner/wt/.git", paths)
        self.assertEqual(paths.count("u/tmp/r/d.txt"), 1)

    def test_subdirectory_project_grep_and_turns(self):
        """A project inside a larger repository: git runs in the trusted root, paths come back
        project-relative, the rule applies to them, nothing from outside the project."""
        repo = os.path.realpath(tempfile.mkdtemp(prefix="piper-mono-", dir=_env.PP))
        self.addCleanup(shutil.rmtree, repo, True)
        write(repo, "outside.txt", "needle outside the project\n")
        write(repo, "app/src/M.java", "needle inside\n")
        write(repo, "app/notes.txt", "needle hidden\n")
        write(repo, "app/.hole/settings.json", '{"files": {"exclude": ["notes.txt"]}}')
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.com")
        git(repo, "config", "user.name", "Tester")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "init")
        _env.register("team-x/mono", os.path.join(repo, "app"))
        self.assertEqual(self.m.repo_top(os.path.join(repo, "app")), repo)
        show = self.m.git_show("team-x/mono", git(repo, "rev-parse", "HEAD").strip())   # K12 + L9: the whole commit, nothing called "hidden from Claude"
        self.assertIn("needle outside the project", show)
        self.assertNotIn("withheld", show)
        r = self.m.fs("team-x/mono", "grep", {"q": "needle"})
        self.assertEqual((r["engine"], [m["path"] for m in r["matches"]]), ("git", ["notes.txt", "src/M.java"]))
        s = sessions.Session(project="team-x/mono", path=os.path.join(repo, "app"), state="working", title="mono")
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        now = time.time() - 5
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now, "prompt": "p"})
        self.assertTrue(wait(lambda: len(s.turns) == 1 and not s.turn_busy))
        write(repo, "app/src/M.java", "changed\n")
        self.m._turn_event(s, "Stop", {"ts": now + 1})
        self.assertTrue(wait(lambda: s.turns[-1].get("to") and not s.turn_busy))
        h = self.m.turn_file_history(s.id, "src/M.java")
        self.assertEqual((h["diff_path"], [(t["add"], t["del"]) for t in h["turns"]]), ("app/src/M.java", [(1, 1)]))
        self.assertIn("+changed", self.m.turn_diff(s.id, h["turns"][0]["from"], h["turns"][0]["to"], h["diff_path"]))
        self.assertEqual(self.m.turn_file_history(s.id, "notes.txt")["diff_path"], "app/notes.txt")
        # K12: the human owns the whole repository - outside the project dir is not withheld, only .git
        self.assertEqual(self.m.turn_diff(s.id, h["turns"][0]["from"], h["turns"][0]["to"], "outside.txt"), "")
        with self.assertRaises(projectfs.FsError):
            self.m.turn_diff(s.id, h["turns"][0]["from"], h["turns"][0]["to"], ".git/config")
        self._events(s, {"ev": "PostToolUse", "ts": time.time(), "tool_name": "Edit",
                         "file_paths": [repo + "/app/src/M.java", repo + "/outside.txt", repo + "/app/notes.txt"]})
        self.assertEqual(sorted(self.m.fsedit_state(s)["paths"]), ["notes.txt", "src/M.java"])

    # ------------------------------------------------------------ File operations (S1-S3, S7, K1, K2, K10, K11, K13)
    def test_grep_falls_back_to_python_without_a_repository(self):
        plain = os.path.realpath(_env.make_project("fops-plain", {"a.txt": "needle\n"}))
        _env.register("team-x/fops-plain", plain)
        r = self.m.fs("team-x/fops-plain", "grep", {"q": "needle"})
        self.assertEqual((r["engine"], [m["path"] for m in r["matches"]]), ("python", ["a.txt"]))
        hidden = lambda r: False   # noqa: E731
        with self.assertRaises(projectfs.GrepUnavailable):
            projectfs.git_grep(plain, plain, "needle", hidden)

    # ------------------------------------------------------------ live sync (L6)
    def _events(self, s, *lines):
        os.makedirs(s.box, exist_ok=True)
        with open(os.path.join(s.box, "piper-events.jsonl"), "a") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        self.m._events(s)

    def test_post_tool_use_records_edited_files(self):
        s = self._session(state="working")
        now = time.time()
        self.assertNotIn("fsedit", [x for x in self.m.list() if x["id"] == s.id][0])
        self._events(s, {"ev": "PostToolUse", "ts": now, "tool_name": "Edit", "file_paths": [self.root + "/src/A.java"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Write", "file_paths": ["/etc/passwd"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Edit", "file_paths": [self.root + "/notes.txt"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Edit",
                      "file_paths": [self.root + "/config/secrets/db.properties"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Edit", "file_paths": [self.root + "/.git/config"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Edit", "file_paths": [self.root + "/../fops2/x"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "apply_patch", "file_paths": ["src/B.java", "README.md"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Bash", "file_paths": []},
                     {"ev": "PostToolUse", "ts": now - 600, "tool_name": "Edit", "file_paths": [self.root + "/old.txt"]},
                     {"ev": "PostToolUse", "ts": now, "tool_name": "Edit", "file_paths": [self.root + "/src/A.java"]})
        self.assertEqual(s.state, "working")                                 # the state machine is untouched
        st = [x for x in self.m.list() if x["id"] == s.id][0]["fsedit"]
        # files hidden from Claude are the GUI's too (2026-10-07); .git, outside and stale events are dropped
        self.assertEqual(st, {"seq": 5, "paths": ["src/A.java", "README.md", "src/B.java", "config/secrets/db.properties",
                                                  "notes.txt"], "unknown": 0})
        before = json.dumps(self.m.list(), sort_keys=True)
        self._events(s, {"ev": "PostToolUse", "ts": time.time(), "tool_name": "apply_patch", "file_paths": []})
        st = [x for x in self.m.list() if x["id"] == s.id][0]["fsedit"]
        self.assertEqual((st["seq"], st["unknown"]), (6, 6))                  # no path: the GUI re-reads every tab
        self.assertNotEqual(json.dumps(self.m.list(), sort_keys=True), before)   # the /api/state ETag moves
        self.assertEqual(s.state, "working")
        s.fsedits.extend({"path": "f%d" % i, "ts": 0} for i in range(60))
        self.assertEqual(len(s.fsedits), workbench.FSEDITS_MAX)
        self.assertEqual(len(self.m.fsedit_state(s)["paths"]), 20)

    def test_a_failing_edit_hint_never_loses_the_stop_after_it(self):
        s = self._session(state="working")
        with mock.patch.object(workbench.guard, "is_git_path", side_effect=RuntimeError("boom")):
            self._events(s, {"ev": "PostToolUse", "ts": time.time(), "tool_name": "Edit",
                             "file_paths": [self.root + "/src/A.java"]},
                         {"ev": "Stop", "ts": time.time()})
        self.assertEqual(s.state, "waiting")
        self.assertEqual(s.fsedit_seq, 0)

    def test_hook_keeps_the_path_never_the_content(self):
        out = os.path.join(tempfile.mkdtemp(prefix="piper-ev-"), "ev.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(out), True)

        def run(payload):
            subprocess.run([sys.executable, EVENT_PY, "PostToolUse", out], check=True, input=json.dumps(payload).encode())
            with open(out) as f:
                return json.loads(f.read().splitlines()[-1])
        r = run({"tool_name": "Edit", "tool_input": {"file_path": "/w/src/A.java", "old_string": "SECRET1",
                                                     "new_string": "SECRET2"}, "tool_response": {"x": "SECRET3"}})
        self.assertEqual((r["ev"], r["tool_name"], r["file_paths"]), ("PostToolUse", "Edit", ["/w/src/A.java"]))
        r = run({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/w/n.ipynb", "new_source": "SECRET4"}})
        self.assertEqual(r["file_paths"], ["/w/n.ipynb"])
        patch = "*** Begin Patch\n*** Update File: src/x.py\n*** Move to: src/y.py\n@@\n-SECRET5\n+z\n" \
                "*** Add File: new.txt\n+SECRET6\n*** Delete File: gone.txt\n*** End Patch\n"
        r = run({"tool_name": "apply_patch", "tool_input": {"input": patch}})
        self.assertEqual(r["file_paths"], ["src/x.py", "src/y.py", "new.txt", "gone.txt"])
        r = run({"tool_name": "shell", "tool_input": {"command": ["apply_patch", patch]}})
        self.assertEqual(r["file_paths"], ["src/x.py", "src/y.py", "new.txt", "gone.txt"])
        r = run({"tool_name": "Bash", "tool_input": {"command": "echo SECRET7"}})
        self.assertEqual(r["file_paths"], [])
        with open(out) as f:
            self.assertNotIn("SECRET", f.read())

    def test_git_status_marks_what_this_session_changed(self):
        write(self.root, "src/old-notes.md", "the user's scratch, before the session\n")
        s = self._session(state="working")
        now = time.time() - 10
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now, "prompt": "one"})
        self.assertTrue(wait(lambda: len(s.turns) == 1 and not s.turn_busy))
        write(self.root, "src/New.java", "class New {}\n")
        self.m._turn_event(s, "Stop", {"ts": now + 1})
        self.assertTrue(wait(lambda: s.turns[-1].get("to") and not s.turn_busy))
        st = self.m.git_status(s.id)
        self.assertIn("src/New.java", st["session_files"])
        self.assertNotIn("src/old-notes.md", st["session_files"])
        self.assertIn("src/old-notes.md", [f["path"] for f in st["files"]])   # still listed, only sorted apart

    # ------------------------------------------------------------ turns per file (L9)
    def test_turn_file_history(self):
        s = self._session(state="working")
        now = time.time() - 10
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now, "prompt": "one"})
        self.assertTrue(wait(lambda: len(s.turns) == 1 and not s.turn_busy))
        write(self.root, "src/A.java", "class A {\n  int beta;\n  int gamma;\n}\n")
        write(self.root, "notes.txt", "private notes TURNSECRET\n")
        self.m._turn_event(s, "Stop", {"ts": now + 1})
        self.assertTrue(wait(lambda: s.turns[-1].get("to") and not s.turn_busy))
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now + 2, "prompt": "two"})
        self.assertTrue(wait(lambda: len(s.turns) == 2 and not s.turn_busy))
        write(self.root, "README.md", "# Readme\n")
        self.m._turn_event(s, "Stop", {"ts": now + 3})
        self.assertTrue(wait(lambda: s.turns[-1].get("to") and not s.turn_busy))
        self.m._turn_event(s, "UserPromptSubmit", {"ts": now + 4, "prompt": "three (open)"})
        self.assertTrue(wait(lambda: len(s.turns) == 3 and not s.turn_busy))
        write(self.root, "src/A.java", "class A {}\n")
        r = self.m.turn_file_history(s.id, "src/A.java")
        self.assertEqual(r["diff_path"], "src/A.java")
        self.assertEqual([(t["index"], t["n"], t["add"], t["del"]) for t in r["turns"]], [(2, 3, 1, 4), (0, 1, 2, 1)])
        self.assertIsNone(r["turns"][0]["to"])                               # open: compared with now
        self.assertEqual(r["turns"][1]["prompt"], "one")
        self.assertEqual([t["n"] for t in self.m.turn_file_history(s.id, "README.md")["turns"]], [2])
        self.assertEqual(self.m.turn_file_history(s.id, "src/None.java")["turns"], [])
        # a directory is not a file: its files (a hidden one among them) never show up
        self.assertEqual(self.m.turn_file_history(s.id, "src")["turns"], [])
        self.assertEqual(self.m.turn_file_history(s.id, ".")["turns"], [])
        d = self.m.turn_diff(s.id, r["turns"][1]["from"], r["turns"][1]["to"], r["diff_path"])
        self.assertIn("+  int gamma;", d)
        self.assertEqual(self.m.turn_file_history(s.id, "notes.txt")["diff_path"], "notes.txt")   # the GUI's too
        with self.assertRaises(projectfs.FsError):
            self.m.turn_file_history(s.id, ".git/config")
        for bad in ("../x", "/etc/passwd", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.m.turn_file_history(s.id, bad)
        with self.assertRaises(KeyError):
            self.m.turn_file_history("nope", "README.md")

    def _rm(self, *rels):
        for rel in rels:
            p = os.path.join(self.root, rel)
            self.addCleanup(lambda p=p: os.unlink(p) if os.path.islink(p) or os.path.isfile(p) else shutil.rmtree(p, True))

    def test_s1_a_hidden_file_is_saved_while_a_session_works(self):
        """S1: a hidden file is saved while a session works (since 2026-10-07 a visible one too: the sha lock)."""
        self._session(state="working", coverage=self._cov(["notes.txt"]))   # over-mounted there
        sha = lambda rel: hashlib.sha256(self._read(rel)).hexdigest()   # noqa: E731
        self.m.fs_write(self.key, "notes.txt", "mine", sha("notes.txt"))
        self.assertEqual(self._read("notes.txt"), b"mine\n")
        self.assertTrue(common.audit_tail(1)[0]["hidden"])
        self.m.fs_write(self.key, "README.md", "x", sha("README.md"))
        self.assertEqual(self._read("README.md"), b"x\n")
        git(self.root, "checkout", "-q", "--", "README.md")

    def test_s2_an_unreadable_policy_is_one_flag(self):
        """S2: without the base settings everything stays hidden from Claude (fail closed), the GUI
        gets one flag instead of every file marked, and file operations say why they are refused."""
        with mock.patch.object(self.m, "_base_settings", side_effect=ValueError("broken settings")):
            rule = self.m.project_hidden(self.m.gui_project(self.key))
            self.assertTrue(rule("src/A.java"))                                  # the agent's rule: fail closed
            self.assertIn("broken settings", sessions.policy_unreadable(rule))
            ls = self.m.fs(self.key, "list", {"path": "."})
            self.assertTrue(ls["policy_unreadable"])
            self.assertIn("broken settings", ls["policy_error"])
            self.assertFalse(any(e["hidden"] for e in ls["entries"]))
            rd = self.m.fs(self.key, "read", {"path": "notes.txt"})
            self.assertEqual((rd["hidden"], rd["policy_unreadable"]), (False, True))
            fl = self.m.fs(self.key, "files", {})
            self.assertEqual((fl["hidden"], fl["policy_unreadable"]), ([], True))
            gr = self.m.fs(self.key, "grep", {"q": "needle"})
            self.assertTrue(gr["policy_unreadable"] and gr["matches"] and not any(m["hidden"] for m in gr["matches"]))
            write(self.root, "README.md", "# changed\n")             # git views: fail-closed marks + the flag
            st = self.m.project_git_status(self.key)
            self.assertTrue(st["policy_unreadable"] and all(f["sensitive"] for f in st["files"]))
            git(self.root, "checkout", "-q", "--", "README.md")
            w = self._session(state="waiting", coverage=self._cov(["notes.txt"]))
            w.state = "working"                         # unreadable: treated as hidden - written in place
            ino = os.stat(os.path.join(self.root, "notes.txt")).st_ino
            self.m.fs_write(self.key, "notes.txt", "x", hashlib.sha256(self._read("notes.txt")).hexdigest())
            self.assertEqual(os.stat(os.path.join(self.root, "notes.txt")).st_ino, ino)
        ls = self.m.fs(self.key, "list", {"path": "."})
        self.assertFalse(ls["policy_unreadable"])
        self.assertTrue(next(e for e in ls["entries"] if e["name"] == "notes.txt")["hidden"])

    def test_k1_k10_k13_noise_hidden_marks_and_skipped_files(self):
        """K1: one NOISE list - flagged in the tree, left out of quick open and search unless all=1;
        K10: quick open and grep say what is hidden from Claude; K13: what was skipped is counted,
        a too large or binary read is TooLarge / Binary (413 / 415), not a missing file."""
        write(self.root, ".vscode/n.txt", "needle in noise\n")
        write(self.root, "node_modules/m.txt", "needle in deps\n")   # ignored: the Python pass
        write(self.root, ".DS_Store", "needle\n")
        write(self.root, "big/huge.txt", "needle\n" + "x" * (3 << 20))
        write(self.root, "development/huge.txt", "needle\n" + "x" * (3 << 20))
        write(self.root, "src/bin.dat", "a\0b")
        os.symlink("../README.md", os.path.join(self.root, "src/link.md"))
        self._rm(".vscode", ".DS_Store", "big", "src/bin.dat", "src/link.md")
        ents = {e["name"]: e for e in self.m.fs(self.key, "list", {"path": "."})["entries"]}
        self.assertEqual((ents[".vscode"]["noise"], ents["node_modules"]["noise"], ents["src"]["noise"]), (True, True, False))
        self.assertTrue(ents["notes.txt"]["hidden"] and not ents["README.md"]["hidden"])
        self.m._files_cache.pop(self.key, None)
        fl = self.m.fs(self.key, "files", {})
        self.assertNotIn(".vscode/n.txt", fl["files"])
        self.assertNotIn(".DS_Store", fl["files"])
        self.assertIn("notes.txt", fl["hidden"])
        self.assertIn("config/secrets/db.properties", fl["hidden"])
        self.assertNotIn("README.md", fl["hidden"])
        self.assertGreaterEqual(fl["skipped_links"], 1)
        every = self.m.fs(self.key, "files", {"all": "1"})["files"]
        self.assertTrue({".vscode/n.txt", ".DS_Store", "node_modules/m.txt"} <= set(every))
        self.assertNotIn(".vscode/n.txt", self.m.fs(self.key, "files", {})["files"])   # cached apart
        gr = self.m.fs(self.key, "grep", {"q": "needle"})
        paths = {m["path"]: m["hidden"] for m in gr["matches"]}
        self.assertEqual(gr["engine"], "git")
        self.assertFalse({".vscode/n.txt", "node_modules/m.txt", ".DS_Store"} & set(paths))
        self.assertEqual((paths["notes.txt"], paths["README.md"]), (True, False))
        self.assertEqual(gr["skipped_large"], 2)                            # git grep's + the ignored path's
        ga = self.m.fs(self.key, "grep", {"q": "needle", "all": "1"})
        self.assertTrue({".vscode/n.txt", "node_modules/m.txt", ".DS_Store"} <= {m["path"] for m in ga["matches"]})
        hidden = lambda r: False   # noqa: E731 - the Python engine (no repository) the same way
        py = projectfs.grep(self.root, "needle", hidden)
        self.assertFalse({".vscode/n.txt", ".DS_Store"} & {m["path"] for m in py["matches"]})
        self.assertGreaterEqual(py["skipped_large"], 2)
        self.assertIn(".vscode/n.txt", {m["path"] for m in projectfs.grep(self.root, "needle", hidden, all=True)["matches"]})
        with self.assertRaises(projectfs.TooLarge):
            self.m.fs(self.key, "read", {"path": "big/huge.txt"})
        with self.assertRaises(projectfs.Binary):
            self.m.fs(self.key, "read", {"path": "src/bin.dat"})
        with self.assertRaisesRegex(projectfs.FsError, "inside .git"):   # the human is not told "isolation policy"
            self.m.fs(self.key, "read", {"path": ".git/HEAD"})

    def test_k2_k11_project_git_status_and_one_hidden_mark(self):
        """K2: the git status of a project without a session; K11: the git panel marks what Files
        marks (project_hidden), not only what one session pinned."""
        write(self.root, "notes.txt", "changed\n")
        write(self.root, "README.md", "# changed\n")
        st = self.m.project_git_status(self.key)
        self.assertEqual((st["root"], st["branch"], st["policy_unreadable"]), (self.root, "main", False))
        files = {f["path"]: f["sensitive"] for f in st["files"]}
        self.assertEqual((files["notes.txt"], files["README.md"]), (True, False))
        s = self._session(excludes=[])        # pinned nothing: before K11 notes.txt was not marked here
        files = {f["path"]: f["sensitive"] for f in self.m.git_status(s.id)["files"]}
        self.assertEqual((files["notes.txt"], files["README.md"]), (True, False))
        with self.assertRaises(KeyError):
            self.m.project_git_status("team-x/nope")


if __name__ == "__main__":
    unittest.main()
