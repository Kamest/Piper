"""projectfs.py directly: the hidden-file gate of every host-side read (list, read, quick open, grep)."""
import os
import shutil
import subprocess
import tempfile
import unicodedata
import unittest

import _env  # noqa: F401 - sets up the environment before Piper modules load
import guard
import projectfs

CANDIDATES = ["config/secrets", "config/secrets.yml", "**/*.jks"]


def hidden(rel):
    return guard.hidden_by(CANDIDATES, rel)


class ProjectFsTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "proj")
        files = {"README.md": "hello PLANTED_NO\n", "config/secrets/db.properties": "password=PLANTED_1\n",
                 "config/secrets.yml": "PLANTED_2\n", "certs/key.jks": "PLANTED_4\n",
                 "src/main/java/com/acme/App.java": "class App {}\n", ".git/config": "[core] PLANTED_G\n"}
        for rel, text in files.items():
            p = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        with open(os.path.join(self.base, "outside.txt"), "w") as f:
            f.write("PLANTED_OUT\n")

    def names(self, rel="."):
        return [e["name"] for e in projectfs.list_dir(self.root, rel, hidden)["entries"]]

    def test_paths_must_stay_relative_and_inside(self):
        for bad in ("/etc/passwd", "../outside.txt", "src/../../outside.txt", "a\0b"):
            with self.assertRaises(projectfs.FsError, msg=bad):
                projectfs.read_file(self.root, bad, hidden)

    def test_candidates_and_git_are_never_read_listed_or_searched(self):
        for rel in ("config/secrets/db.properties", "config/secrets.yml", "certs/key.jks", ".git/config",
                    "config/secrets"):
            with self.assertRaises(projectfs.FsError, msg=rel):
                projectfs.read_file(self.root, rel, hidden)
        self.assertNotIn(".git", self.names())
        self.assertEqual(self.names("config"), [])
        self.assertEqual(self.names("certs"), [])
        out = projectfs.grep(self.root, "PLANTED", hidden)
        self.assertEqual([m["path"] for m in out["matches"]], ["README.md"])
        self.assertNotIn("config/secrets.yml", projectfs.files(self.root, hidden)["files"])

    def test_the_gui_rule_lists_hidden_files_but_never_git(self):
        """The GUI as the human's IDE: show_hidden is gone - the GUI passes only
        guard.is_git_path (sessions.Manager.fs marks what Claude does not see); the agent's rule still
        lists none of them, and `.git` (any letter case) is never listed by either"""
        gui = guard.is_git_path
        e = projectfs.list_dir(self.root, "config", gui)["entries"]
        self.assertEqual({x["name"] for x in e}, {"secrets", "secrets.yml"})
        self.assertEqual(self.names("config"), [])   # the agent's rule
        top = [x["name"] for x in projectfs.list_dir(self.root, ".", gui)["entries"]]
        self.assertNotIn(".git", top)
        self.assertNotIn(".GIT", top)
        with self.assertRaises(projectfs.FsError):
            projectfs.read_file(self.root, ".git/config", gui)
        with self.assertRaises(projectfs.FsError):
            projectfs.read_file(self.root, ".Git/config", gui)

    def test_letter_case_and_unicode_variants_are_hidden_too(self):
        for rel in ("Config/Secrets.yml", "CONFIG/SECRETS/db.properties", ".GIT/config", ".Git/config"):
            with self.assertRaises(projectfs.FsError, msg=rel):
                projectfs.read_file(self.root, rel, hidden)
        nfd = unicodedata.normalize("NFD", "config/secrets/café.properties")
        self.assertTrue(hidden(nfd))

    def test_symlinks_leaving_or_into_hidden_are_refused(self):
        os.symlink(os.path.join(self.base, "outside.txt"), os.path.join(self.root, "out.txt"))
        os.symlink(os.path.join(self.root, "config", "secrets.yml"), os.path.join(self.root, "sec.txt"))
        os.symlink(os.path.join(self.root, ".git"), os.path.join(self.root, "g"))
        for rel in ("out.txt", "sec.txt", "g/config"):
            with self.assertRaises(projectfs.FsError, msg=rel):
                projectfs.read_file(self.root, rel, hidden)
        names = self.names()
        self.assertFalse({"out.txt", "sec.txt", "g"} & set(names))
        self.assertNotIn("PLANTED", str(projectfs.grep(self.root, "PLANTED_(OUT|2|G)", hidden)["matches"]))
        self.assertFalse({"out.txt", "sec.txt"} & set(projectfs.files(self.root, hidden)["files"]))

    def test_a_gitlink_file_is_not_offered_by_quick_open(self):
        # a worktree's or submodule's `.git` FILE: never listed, read or searched - quick open included
        sub = os.path.join(self.root, "lib")
        os.makedirs(sub)
        with open(os.path.join(sub, ".git"), "w") as f:
            f.write("gitdir: /somewhere/PLANTED_GL\n")
        listed = projectfs.files(self.root, hidden)["files"]
        self.assertFalse([p for p in listed if guard.is_git_path(p)], listed)
        self.assertNotIn(".git", self.names("lib"))
        self.assertEqual(projectfs.grep(self.root, "PLANTED_GL", hidden)["matches"], [])

    def test_compact_chain_and_read_window(self):
        e = {x["name"]: x for x in projectfs.list_dir(self.root, "src", hidden, compact=True)["entries"]}
        self.assertEqual(e["main"]["chain"], "java/com/acme")
        r = projectfs.read_file(self.root, "README.md", hidden, offset=0, limit=1)
        self.assertEqual(r["lines"], ["1\thello PLANTED_NO"])
        self.assertEqual(r["eol"], "\n")

    def test_binary_and_huge_files_are_refused(self):
        with open(os.path.join(self.root, "b.bin"), "wb") as f:
            f.write(b"\0\1\2")
        with self.assertRaises(projectfs.FsError):
            projectfs.read_file(self.root, "b.bin", hidden)
        with self.assertRaises(projectfs.FsError):
            projectfs.grep(self.root, "x" * 501, hidden)

    def test_git_grep_respects_the_gate(self):
        if shutil.which("git") is None:
            self.skipTest("no git")
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        shutil.rmtree(os.path.join(self.root, ".git"))
        subprocess.run(["git", "init", "-q", self.root], check=True, env=env)
        with open(os.path.join(self.root, ".gitignore"), "w") as f:
            f.write("development/\n")
        os.makedirs(os.path.join(self.root, "development"))
        with open(os.path.join(self.root, "development", "notes.md"), "w") as f:
            f.write("PLANTED_DEV\n")
        out = projectfs.git_grep(self.root, self.root, "PLANTED", hidden)
        self.assertEqual(sorted(m["path"] for m in out["matches"]), ["README.md", "development/notes.md"])


if __name__ == "__main__":
    unittest.main()
