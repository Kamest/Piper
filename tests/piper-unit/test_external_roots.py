"""External roots in Files (workbench.ext_*, fs_transfer): a folder outside every registered project used
like a project root by the human's GUI only - never by gui_project / projects.find, sessions or agent
tools; Piper's state, the agents' homes, extension protected / forbidden paths and `.git` refused on
every operation (piperd's token gates: test_piperd)."""
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _env
import common
import projectfs
import projects
import sessions
import workbench


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ExternalRootsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.proot = os.path.realpath(_env.make_project("extroots", {"src/A.java": "class A {}\n", "README.md": "# r\n"}))
        cls.pkey = "team-x/extroots"
        _env.register(cls.pkey, cls.proot)

    def setUp(self):
        temp = tempfile.mkdtemp(dir=os.environ["TEST_SANDBOX_WORK"])
        self.addCleanup(shutil.rmtree, temp, True)
        self.root = os.path.realpath(temp)
        self.key = workbench.EXTERNAL_PREFIX + self.root
        os.makedirs(os.path.join(self.root, "docs"))
        self.put("docs/notes.md", b"# Notes\r\nline\r\n")
        self.put("logo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 16)
        os.makedirs(os.path.join(self.proot, "inbox"), exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.join(self.proot, "inbox"), True)

    def put(self, rel, data: bytes, root=None):
        p = os.path.join(root or self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def get(self, rel, root=None) -> bytes:
        with open(os.path.join(root or self.root, rel), "rb") as f:
            return f.read()

    def events(self, name):
        return [e for e in common.audit_tail(400) if e.get("event") == name]

    # ------------------------------------------------------------ the root's operations
    def test_list_read_write_create_rename_delete_restore(self):
        m, key = self.m, self.key
        top = m.ext_fs(key, "list", {})
        self.assertEqual((top["path"], top["root"], top["key"], top["external"], top["policy_unreadable"]),
                         (".", self.root, key, True, False))
        self.assertEqual([(e["name"], e["type"], e["hidden"]) for e in top["entries"]],
                         [("docs", "dir", False), ("logo.png", "file", False)])
        # a non-canonical spelling of the root resolves to its real path (the trash entries carry that key)
        alias = os.path.join(os.path.dirname(self.root), ".", os.path.basename(self.root))
        self.assertEqual(m.ext_fs(workbench.EXTERNAL_PREFIX + alias, "list", {})["key"], key)
        r = m.ext_fs(key, "read", {"path": "docs/notes.md"})
        self.assertEqual((r["lines"], r["eol"], r["sha"], r["hidden"]),
                         (["1\t# Notes", "2\tline"], "\r\n", sha(self.get("docs/notes.md")), False))
        self.assertEqual(m.ext_raw(key, "logo.png")[0], "image/png")
        self.assertEqual(m.ext_download(key, "docs/notes.md"), self.get("docs/notes.md"))
        self.assertIn(self.root, m.ext_fs(key, "files", {})["root"])
        self.assertEqual(sorted(m.ext_fs(key, "files", {})["files"]), ["docs/notes.md", "logo.png"])
        with self.assertRaisesRegex(ValueError, "inside a project only"):
            m.ext_fs(key, "grep", {"q": "Notes"})
        # save: the sha lock, the file's line endings and mode
        os.chmod(os.path.join(self.root, "docs/notes.md"), 0o640)
        w = m.ext_write(key, "docs/notes.md", "# Notes\nline two", r["sha"])
        self.assertEqual(self.get("docs/notes.md"), b"# Notes\r\nline two\r\n")
        self.assertEqual(w["sha"], sha(self.get("docs/notes.md")))
        self.assertEqual(os.stat(os.path.join(self.root, "docs/notes.md")).st_mode & 0o777, 0o640)
        with self.assertRaisesRegex(ValueError, "changed since you opened it"):
            m.ext_write(key, "docs/notes.md", "stale", r["sha"])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            m.ext_write(key, "docs/missing.md", "x", r["sha"])
        # create / rename / delete / restore
        self.assertEqual(m.ext_create(key, "docs/new.txt", "file", "hi\n"), {"path": "docs/new.txt", "sha": sha(b"hi\n")})
        self.assertEqual(m.ext_create(key, "drafts", "dir"), {"path": "drafts"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            m.ext_create(key, "docs/new.txt", "file", "again")
        self.assertEqual(m.ext_rename(key, "docs/new.txt", "drafts/new.txt"), {"from": "docs/new.txt", "to": "drafts/new.txt"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            m.ext_rename(key, "drafts/new.txt", "docs/notes.md")
        trash = m.ext_delete(key, "drafts")["trash"]
        self.assertFalse(os.path.exists(os.path.join(self.root, "drafts")))
        self.assertTrue(os.path.isfile(os.path.join(workbench.TRASH_DIR, trash, "item", "new.txt")))
        with self.assertRaises(projectfs.FsError):   # another root's (or a project's) restore never takes it
            m.fs_restore(self.pkey, trash)
        self.assertEqual(m.ext_restore(key, trash), {"path": "drafts"})
        self.assertEqual(self.get("drafts/new.txt"), b"hi\n")
        with self.assertRaises(projectfs.FsError):
            m.ext_restore(key, trash)
        for name in ("gui-external-open", "gui-external-write", "gui-external-create", "gui-external-rename",
                     "gui-external-delete", "gui-external-restore"):
            ev = [e for e in self.events(name) if e.get("root") == self.root or self.root in str(e.get("path"))]
            self.assertTrue(ev, name)
            self.assertFalse({"text", "content", "lines"} & set().union(*ev), name)
        self.assertNotIn("line two", str(self.events("gui-external-write")))

    def test_root_must_be_an_existing_external_folder(self):
        m = self.m
        for bad in ("external:", "external:relative/path", "external:" + os.path.join(self.root, "missing"),
                    "external:" + os.path.join(self.root, "logo.png"), "external:" + self.proot,
                    "external:" + os.path.join(self.proot, "src"), "external:" + common.PIPER_HOME,
                    "external:" + os.path.join(common.REAL_HOME, ".claude")):
            with self.subTest(bad=bad), self.assertRaises(projectfs.FsError):
                m.ext_fs(bad, "list", {})
        with self.assertRaisesRegex(projectfs.FsError, "belongs to project " + self.pkey):
            m.ext_fs("external:" + self.proot, "list", {})

    # ------------------------------------------------------------ protected paths, on every operation
    def test_protected_paths_are_refused_on_every_operation(self):
        """A root like $HOME: Piper's state, the agents' homes, an extension's protected folder, `.git`, a link
        into Piper's state and (case-insensitive file systems) a case alias of a protected folder."""
        m = self.m
        home = os.path.realpath(common.REAL_HOME)   # holds .claude and .hole, as a real home does
        key = workbench.EXTERNAL_PREFIX + home
        self.put(".claude/settings.json", b"{}", home)
        self.put(".hole/secret", b"token", home)
        self.put("provider/state", b"credential", home)
        self.put("repo/.git/config", b"[core]", home)
        self.put("ok/readme.txt", b"public\n", home)
        state = self.put("external-roots/state", b"private", common.PIPER_HOME)
        for link in ("piper-link", "ok/piper-link"):
            p = os.path.join(home, link)
            if not os.path.lexists(p):
                os.symlink(os.path.dirname(state), p)
        self.addCleanup(lambda: [shutil.rmtree(os.path.join(home, d), True) for d in ("provider", "repo", "ok", "dest")])
        self.addCleanup(lambda: [os.unlink(os.path.join(home, link)) for link in ("piper-link", "ok/piper-link")
                                 if os.path.lexists(os.path.join(home, link))])
        provider = os.path.join(home, "provider")
        real_collect = workbench.extensions.collect

        def collect(name, *args):
            if name == "protected":
                return [provider]
            return real_collect(name, *args)
        self.enterContext(mock.patch.object(workbench.extensions, "collect", side_effect=collect))
        upper = os.path.join(home, ".CLAUDE")
        alias = os.path.exists(upper) and os.path.samefile(upper, os.path.join(home, ".claude"))
        names = {e["name"] for e in m.ext_fs(key, "list", {})["entries"]}
        self.assertIn("ok", names)
        self.assertFalse({".claude", ".hole", "provider", "piper-link"} & names, names)
        self.assertNotIn("piper-link", {e["name"] for e in m.ext_fs(key, "list", {"path": "ok"})["entries"]})
        self.assertNotIn(".git", {e["name"] for e in m.ext_fs(key, "list", {"path": "repo"})["entries"]})
        files = m.ext_fs(key, "files", {})["files"]
        self.assertIn("ok/readme.txt", files)
        self.assertFalse([f for f in files if f.startswith((".claude", ".hole", "provider", "repo/.git", "piper-link"))], files)
        bad = [".claude/settings.json", ".hole/secret", "provider/state", "repo/.git/config", "piper-link/state",
               "ok/piper-link/state"] + ([".CLAUDE/settings.json"] if alias else [])
        good_sha = sha(b"public\n")
        print("EXTERNAL_CASE_ALIAS exercised=%s" % alias, flush=True)
        for rel in bad:   # each refusal is the documented 404 (FsError) - never a later 409 by accident
            ops = {"read": lambda: m.ext_fs(key, "read", {"path": rel}), "raw": lambda: m.ext_raw(key, rel),
                   "download": lambda: m.ext_download(key, rel), "write": lambda: m.ext_write(key, rel, "x", good_sha),
                   "create": lambda: m.ext_create(key, rel + ".new", "file", "x"),
                   "rename-from": lambda: m.ext_rename(key, rel, "ok/moved"),
                   "rename-to": lambda: m.ext_rename(key, "ok/readme.txt", rel + ".new"),
                   "delete": lambda: m.ext_delete(key, rel),
                   "transfer-from": lambda: m.fs_transfer({"project": key, "path": rel}, {"project": self.pkey, "dir": "inbox"}),
                   "transfer-to": lambda: m.fs_transfer({"project": self.pkey, "path": "README.md"},
                                                        {"project": key, "dir": os.path.dirname(rel)}, name="x")}
            for op, call in ops.items():
                with self.subTest(rel=rel, op=op), self.assertRaises(projectfs.FsError):
                    call()
        for folder in (".claude", ".hole", "provider", "repo") + ((".CLAUDE",) if alias else ()):
            ops = {"delete": lambda: m.ext_delete(key, folder), "rename": lambda: m.ext_rename(key, folder, "renamed"),
                   "transfer": lambda: m.fs_transfer({"project": key, "path": folder}, {"project": self.pkey, "dir": "inbox"})}
            if folder != "repo":   # a repository is listed without its .git
                ops["list"] = lambda: m.ext_fs(key, "list", {"path": folder})
            for op, call in ops.items():
                with self.subTest(folder=folder, op=op), self.assertRaises(projectfs.FsError):
                    call()
        # a folder holding a link (into Piper's state or anywhere) is never copied - links are not followed
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            m.fs_transfer({"project": key, "path": "ok"}, {"project": self.pkey, "dir": "inbox"})
        # restore never puts an entry back into a protected place
        tid = "20990101-000000-0000beef"
        tdir = os.path.join(workbench.TRASH_DIR, tid)
        self.put("item", b"planted", tdir)
        common.write_json(os.path.join(tdir, "meta.json"), {"project": key, "path": ".claude/planted", "at": 4e9})
        self.addCleanup(shutil.rmtree, tdir, True)
        with self.assertRaises(projectfs.FsError):
            m.ext_restore(key, tid)
        self.assertFalse(os.path.exists(os.path.join(home, ".claude", "planted")))
        # nothing protected changed
        self.assertEqual(self.get(".hole/secret", home), b"token")
        self.assertEqual(self.get("provider/state", home), b"credential")
        self.assertEqual(self.get("external-roots/state", common.PIPER_HOME), b"private")
        # the whole home holds Piper's agents' homes: never moved or deleted as one
        parent = workbench.EXTERNAL_PREFIX + os.path.dirname(home)
        with self.assertRaisesRegex(projectfs.FsError, "holds Piper's own state"):
            m.ext_delete(parent, os.path.basename(home))

    def test_forbidden_hook_and_parent_swap(self):
        m, key = self.m, self.key
        self.put("vault/key", b"secret")
        vault = os.path.join(self.root, "vault")
        real_collect = workbench.extensions.collect
        with mock.patch.object(workbench.extensions, "collect",
                               side_effect=lambda name, *a: [(vault, "any")] if name == "forbidden" else real_collect(name, *a)):
            self.assertNotIn("vault", {e["name"] for e in m.ext_fs(key, "list", {})["entries"]})
            with self.assertRaises(projectfs.FsError):
                m.ext_fs(key, "read", {"path": "vault/key"})
            with self.assertRaises(projectfs.FsError):
                m.ext_delete(key, "vault")
        # a folder swapped for a link into Piper's state after the check: the no-follow I/O refuses it
        private = os.path.dirname(self.put("external-swap/state", b"private", common.PIPER_HOME))
        self.put("swap/state", b"public")
        original = m._ext_resolve

        def race(root, rel, g):
            out = original(root, rel, g)
            if not os.path.islink(os.path.join(self.root, "swap")):
                os.rename(os.path.join(self.root, "swap"), os.path.join(self.root, "swap-original"))
                os.symlink(private, os.path.join(self.root, "swap"))
            return out
        for call in (lambda: m.ext_fs(key, "read", {"path": "swap/state"}), lambda: m.ext_download(key, "swap/state")):
            with mock.patch.object(m, "_ext_resolve", side_effect=race):
                with self.assertRaises((projectfs.FsError, OSError)):
                    call()
            os.unlink(os.path.join(self.root, "swap"))
            os.rename(os.path.join(self.root, "swap-original"), os.path.join(self.root, "swap"))
        with self.assertRaises((projectfs.FsError, OSError)):   # a write never goes through the swapped link
            with mock.patch.object(m, "_ext_resolve", side_effect=race):
                m.ext_write(key, "swap/state", "x", sha(b"public"))
        self.assertEqual(self.get("external-swap/state", common.PIPER_HOME), b"private")

    def test_a_project_inside_an_external_root_is_read_but_never_changed_through_it(self):
        m = self.m
        outer = workbench.EXTERNAL_PREFIX + os.path.dirname(self.proot)
        self.assertIn("extroots", {e["name"] for e in m.ext_fs(outer, "list", {})["entries"]})
        self.assertEqual(m.ext_fs(outer, "read", {"path": "extroots/README.md"})["lines"], ["1\t# r"])
        r = m.ext_fs(outer, "read", {"path": "extroots/README.md"})
        for call in (lambda: m.ext_write(outer, "extroots/README.md", "x", r["sha"]),
                     lambda: m.ext_create(outer, "extroots/new.txt", "file", "x"),
                     lambda: m.ext_rename(outer, "extroots/README.md", "moved.md"),
                     lambda: m.ext_rename(outer, "extroots", "renamed"),
                     lambda: m.ext_delete(outer, "extroots/README.md"),
                     lambda: m.ext_delete(outer, "extroots"),
                     lambda: m.fs_transfer({"project": self.key, "path": "logo.png"}, {"project": outer, "dir": "extroots"}),
                     lambda: m.fs_transfer({"project": outer, "path": "extroots"}, {"project": self.key, "dir": ""}, "move")):
            with self.assertRaisesRegex(ValueError, "project " + self.pkey):
                call()
        self.assertEqual(self.get("README.md", self.proot), b"# r\n")

    # ------------------------------------------------------------ never a project, a session's or an agent's
    def test_gui_project_git_and_agent_tools_reject_external_keys(self):
        m, key = self.m, self.key
        self.assertIsNone(projects.find(key, sessions.PROJECT_PATH))
        with self.assertRaises(KeyError):
            m.gui_project(key)
        for call in (lambda: m.fs(key, "list", {}), lambda: m.fs_write(key, "docs/notes.md", "x", "0" * 64),
                     lambda: m.fs_create(key, "x", "file"), lambda: m.fs_delete(key, "logo.png"),
                     lambda: m.fs_rename(key, "logo.png", "x.png"), lambda: m.fs_raw(key, "logo.png"),
                     lambda: m.fs_marks(key, "logo.png"), lambda: m.fs_scratch(key, "md"),
                     lambda: m.git_log(key, "logo.png")):
            with self.assertRaises(KeyError):
                call()
        s = sessions.Session(project=self.pkey, path=self.proot, state="waiting", title="ext", kind="project")
        m.sessions[s.id] = s
        self.addCleanup(m.sessions.pop, s.id, None)
        m.set_unlock(s.id, "projects", True)
        m.set_unlock(s.id, "projects_write", True)   # so write_file reaches key resolution, not the unlock gate
        for tool, args in (("list_dir", {"project": key}), ("read_file", {"project": key, "path": "docs/notes.md"}),
                           ("grep", {"project": key, "pattern": "Notes"}),
                           ("write_file", {"project": key, "path": "x.txt", "content": "x"})):
            with self.subTest(tool=tool), self.assertRaisesRegex((ValueError, KeyError), "unknown project"):
                m.tool(s, tool, args)
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.txt")))

    # ------------------------------------------------------------ transfer
    def test_copy_and_move_files_and_folders_both_directions(self):
        m, key, pkey = self.m, self.key, self.pkey
        os.chmod(self.put("docs/run.sh", b"#!/bin/sh\n"), 0o755)
        dry = m.fs_transfer({"project": key, "path": "docs"}, {"project": pkey, "dir": "inbox"}, dry=True)
        self.assertEqual(dry, {"dry": True, "kind": "dir", "files": 2, "dirs": 1, "bytes": 15 + 10, "path": "inbox/docs",
                               "conflict": False, "keep_both": None})
        self.assertFalse(os.path.exists(os.path.join(self.proot, "inbox", "docs")))
        r = m.fs_transfer({"project": key, "path": "docs"}, {"project": pkey, "dir": "inbox"})
        self.assertEqual((r["path"], r["files"], r["dirs"], r["bytes"]), ("inbox/docs", 2, 1, 25))
        self.assertNotIn("trash", r)
        self.assertEqual(self.get("inbox/docs/notes.md", self.proot), self.get("docs/notes.md"))
        self.assertEqual(os.stat(os.path.join(self.proot, "inbox/docs/run.sh")).st_mode & 0o777, 0o755)
        self.assertIn("inbox/docs/notes.md", m.fs(pkey, "files", {})["files"])   # the project's caches were refreshed
        # project -> external: a file, then a move of a folder (source to Piper's trash, restorable)
        r = m.fs_transfer({"project": pkey, "path": "README.md"}, {"project": key, "dir": "docs"})
        self.assertEqual(r["path"], "docs/README.md")
        self.assertEqual(self.get("docs/README.md"), b"# r\n")
        r = m.fs_transfer({"project": pkey, "path": "inbox/docs"}, {"project": key, "dir": ""}, "move", "keep-both")
        self.assertEqual(r["path"], "docs copy")
        self.assertFalse(os.path.exists(os.path.join(self.proot, "inbox", "docs")))
        self.assertEqual(self.get("docs copy/notes.md"), self.get("docs/notes.md"))
        self.assertEqual(m.fs_restore(pkey, r["trash"]), {"path": "inbox/docs"})
        # external -> project move of a file: the original goes to the trash of its external root
        r = m.fs_transfer({"project": key, "path": "logo.png"}, {"project": pkey, "dir": "inbox"}, "move")
        self.assertFalse(os.path.exists(os.path.join(self.root, "logo.png")))
        self.assertTrue(self.get("inbox/logo.png", self.proot).startswith(b"\x89PNG"))
        self.assertEqual(m.ext_restore(key, r["trash"]), {"path": "logo.png"})
        ev = [e for e in self.events("gui-external-transfer") if e["from"] == {"project": key, "path": "logo.png"}]
        self.assertEqual([(e["mode"], e["to"], e["trash"]) for e in ev],
                         [("move", {"project": pkey, "path": "inbox/logo.png"}, r["trash"])])
        self.assertNotIn("PNG", str(ev))

    def test_conflicts_keep_both_replace_and_fail(self):
        m, key, pkey = self.m, self.key, self.pkey
        self.put("inbox/notes.md", b"old\n", self.proot)
        src, dst = {"project": key, "path": "docs/notes.md"}, {"project": pkey, "dir": "inbox"}
        dry = m.fs_transfer(src, dst, dry=True)
        self.assertEqual((dry["conflict"], dry["keep_both"], dry["path"]), (True, "notes copy.md", "inbox/notes.md"))
        with self.assertRaisesRegex(ValueError, "already exists in inbox - keep both or replace it"):
            m.fs_transfer(src, dst)
        self.assertEqual(self.get("inbox/notes.md", self.proot), b"old\n")
        self.assertEqual(m.fs_transfer(src, dst, conflict="keep-both")["path"], "inbox/notes copy.md")
        self.assertEqual(m.fs_transfer(src, dst, conflict="keep-both")["path"], "inbox/notes copy 2.md")
        r = m.fs_transfer(src, dst, conflict="replace")
        self.assertEqual(r["path"], "inbox/notes.md")
        self.assertEqual(self.get("inbox/notes.md", self.proot), self.get("docs/notes.md"))
        self.assertTrue(r["replaced"])
        os.unlink(os.path.join(self.proot, "inbox/notes.md"))
        self.assertEqual(m.fs_restore(pkey, r["replaced"]), {"path": "inbox/notes.md"})
        self.assertEqual(self.get("inbox/notes.md", self.proot), b"old\n")
        # a copy within one folder keeps both; a move onto itself is refused
        self.assertEqual(m.fs_transfer(src, {"project": key, "dir": "docs"})["path"], "docs/notes copy.md")
        with self.assertRaisesRegex(ValueError, "already there"):
            m.fs_transfer(src, {"project": key, "dir": "docs"}, "move")
        with self.assertRaisesRegex(ValueError, "into itself"):
            m.fs_transfer({"project": key, "path": "docs"}, {"project": key, "dir": "docs"})
        self.assertEqual(workbench.copy_name("docs", ["docs", "docs copy"], True), "docs copy 2")
        self.assertEqual(workbench.copy_name(".env", [".env"]), ".env copy")

    def test_limits_links_and_failures_write_nothing(self):
        m, key, pkey = self.m, self.key, self.pkey
        for i in range(5):
            self.put("many/f%d.txt" % i, b"x" * 10)
        dst = {"project": pkey, "dir": "inbox"}
        with mock.patch.object(workbench, "TRANSFER_MAX_ENTRIES", 3):
            with self.assertRaisesRegex(ValueError, "more than 3 files and folders"):
                m.fs_transfer({"project": key, "path": "many"}, dst, dry=True)
            with self.assertRaisesRegex(ValueError, "more than 3 files and folders"):
                m.fs_transfer({"project": key, "path": "many"}, dst)
        with mock.patch.object(workbench, "TRANSFER_MAX_BYTES", 12):
            with self.assertRaisesRegex(ValueError, "nothing was written"):
                m.fs_transfer({"project": key, "path": "many"}, dst)
            with self.assertRaisesRegex(ValueError, "too large"):
                m.fs_transfer({"project": key, "path": "docs/notes.md"}, dst)
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        os.symlink("f0.txt", os.path.join(self.root, "many", "link.txt"))
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            m.fs_transfer({"project": key, "path": "many"}, dst)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            m.fs_transfer({"project": key, "path": "many/link.txt"}, dst)
        os.unlink(os.path.join(self.root, "many", "link.txt"))
        self.put("many/.git/HEAD", b"ref")
        with self.assertRaises(projectfs.FsError):
            m.fs_transfer({"project": key, "path": "many"}, dst)
        shutil.rmtree(os.path.join(self.root, "many", ".git"))
        with self.assertRaises(projectfs.FsError):   # never into a project's .git
            m.fs_transfer({"project": key, "path": "many"}, {"project": pkey, "dir": ".git"})
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        # a failure in the middle removes what this request created - and nothing else - and a move keeps its source
        real = workbench.Workbench._copy_file
        calls = []

        def flaky(*a):
            calls.append(a)
            if len(calls) == 3:
                raise ValueError("disk full")
            return real(*a)
        with mock.patch.object(workbench.Workbench, "_copy_file", staticmethod(flaky)):
            with self.assertRaisesRegex(ValueError, "disk full"):
                m.fs_transfer({"project": key, "path": "many"}, dst, "move")
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        self.assertEqual(len(os.listdir(os.path.join(self.root, "many"))), 5)
        # a replace that fails puts the old destination back
        self.put("inbox/many/keep.txt", b"keep", self.proot)
        calls.clear()
        with mock.patch.object(workbench.Workbench, "_copy_file", staticmethod(flaky)):
            with self.assertRaisesRegex(ValueError, "disk full"):
                m.fs_transfer({"project": key, "path": "many"}, dst, conflict="replace")
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox", "many")), ["keep.txt"])

    def test_transfer_rejects_malformed_requests_and_names(self):
        m, key, pkey = self.m, self.key, self.pkey
        ok_src, ok_dst = {"project": key, "path": "docs"}, {"project": pkey, "dir": "inbox"}
        for args in ((None, ok_dst), (ok_src, None), ("docs", ok_dst), (ok_src, [pkey]),
                     (ok_src, ok_dst, "link"), (ok_src, ok_dst, "copy", "merge")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.fs_transfer(*args)
        for dst in (ok_dst, {"project": key, "dir": "box"}):
            self.put("box/keep.txt", b"k")
            for name in ("../x", "a/b", "..", ".", ".git", 5, ["x"]):
                with self.subTest(dst=dst["project"][:9], name=name), \
                        self.assertRaises((ValueError, projectfs.FsError)):
                    m.fs_transfer(ok_src, dst, name=name)
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        self.assertEqual(sorted(os.listdir(os.path.join(self.root, "box"))), ["keep.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "x")))
        self.assertTrue(os.path.isdir(os.path.join(self.root, "docs")))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a folder without permissions")
    def test_unreadable_subfolder_is_refused_not_copied_empty(self):
        m, key, pkey = self.m, self.key, self.pkey
        self.put("box/sealed/inner.txt", b"x")
        sealed = os.path.join(self.root, "box", "sealed")
        os.chmod(sealed, 0)
        self.addCleanup(os.chmod, sealed, 0o755)
        for call in (lambda: m.fs_transfer({"project": key, "path": "box"}, {"project": pkey, "dir": "inbox"}, dry=True),
                     lambda: m.fs_transfer({"project": key, "path": "box"}, {"project": pkey, "dir": "inbox"}, "move"),
                     lambda: m.ext_delete(key, "box")):
            with self.assertRaisesRegex(ValueError, "cannot be read"):
                call()
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        self.assertTrue(os.path.isdir(sealed))

    def test_move_a_folder_from_an_external_root_into_a_project_and_back(self):
        m, key, pkey = self.m, self.key, self.pkey
        r = m.fs_transfer({"project": key, "path": "docs"}, {"project": pkey, "dir": "inbox"}, "move")
        self.assertEqual((r["path"], r["files"], r["dirs"]), ("inbox/docs", 1, 1))
        self.assertFalse(os.path.exists(os.path.join(self.root, "docs")))
        self.assertEqual(self.get("inbox/docs/notes.md", self.proot), b"# Notes\r\nline\r\n")
        self.assertEqual(m.ext_restore(key, r["trash"]), {"path": "docs"})
        self.assertEqual(self.get("docs/notes.md"), b"# Notes\r\nline\r\n")

    def test_move_refused_up_front_when_the_trash_is_on_another_volume(self):
        m, key, pkey = self.m, self.key, self.pkey
        real = os.stat

        class Other:
            def __init__(self, st):
                self.st = st

            def __getattr__(self, name):
                return self.st.st_dev + 1 if name == "st_dev" else getattr(self.st, name)

        def stat(path, *a, **kw):   # the trash on another device than every external or project folder
            st = real(path, *a, **kw)
            return Other(st) if path == workbench.TRASH_DIR else st
        with mock.patch.object(workbench.os, "stat", side_effect=stat):
            with self.assertRaisesRegex(ValueError, "another volume"):
                m.fs_transfer({"project": key, "path": "logo.png"}, {"project": pkey, "dir": "inbox"}, "move")
            with self.assertRaisesRegex(ValueError, "another volume"):
                m.ext_delete(key, "logo.png")
        self.assertEqual(os.listdir(os.path.join(self.proot, "inbox")), [])
        self.assertTrue(os.path.exists(os.path.join(self.root, "logo.png")))


if __name__ == "__main__":
    unittest.main()
