"""Files > New scratch file (IDEA Scratches): development/scratch of the checkout, the next free
name per type, a starter text, never committed (info/exclude unless git already ignores it), fs_create's rules."""
import os
import shutil
import subprocess
import unittest

import _env
import workbench
from test_workbench97 import git


class ScratchNameTest(unittest.TestCase):
    def test_first_free_name_per_type_any_case(self):
        self.assertEqual(workbench.scratch_name([], "sql"), "scratch.sql")
        self.assertEqual(workbench.scratch_name(["scratch.sql", "scratch.http"], "sql"), "scratch_2.sql")
        self.assertEqual(workbench.scratch_name(["Scratch.SQL", "scratch_2.sql"], "sql"), "scratch_3.sql")
        self.assertEqual(workbench.scratch_name(["scratch.sql"], "http"), "scratch.http")


class ScratchFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        cls.m = _env.manager(cls)
        cls.repo = os.path.realpath(_env.make_project("scratchrepo", {"README.md": "# r\n", "app/pom.xml": "<project/>\n"}))
        git(cls.repo, "init", "-q", "-b", "main")
        git(cls.repo, "add", "-A")
        git(cls.repo, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m", "init")
        cls.key, cls.sub = "team-x/scratchrepo", "team-x/scratchapp"
        _env.register(cls.key, cls.repo)
        _env.register(cls.sub, os.path.join(cls.repo, "app"))

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        for d in (os.path.join(self.repo, "development"), os.path.join(self.repo, "app", "development")):
            if os.path.islink(d):
                os.unlink(d)
            else:
                shutil.rmtree(d, ignore_errors=True)
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "w") as f:
            f.write("")

    def _untracked(self):
        out = subprocess.run(["git", "-C", self.repo, "status", "--porcelain", "--untracked-files=all"],
                             capture_output=True, text=True, check=True).stdout
        return [line[3:] for line in out.splitlines()]

    def test_creates_numbered_files_with_a_starter_and_keeps_them_out_of_git(self):
        a = self.m.fs_scratch(self.key, "sql")
        b = self.m.fs_scratch(self.key, "sql")
        c = self.m.fs_scratch(self.key, "http")
        self.assertEqual([a["path"], b["path"], c["path"]],
                         ["development/scratch/scratch.sql", "development/scratch/scratch_2.sql", "development/scratch/scratch.http"])
        with open(os.path.join(self.repo, c["path"])) as f:
            self.assertEqual(f.read(), workbench.SCRATCH_TYPES["http"])
        self.assertEqual(self._untracked(), [], "a scratch file shows up as untracked")
        with open(os.path.join(self.repo, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read().count("/development/scratch/"), 1, "the exclude is written once")

    def test_already_ignored_folder_leaves_info_exclude_alone(self):
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "w") as f:
            f.write("development/\n")
        self.m.fs_scratch(self.key, "md")
        with open(os.path.join(self.repo, ".git", "info", "exclude")) as f:
            self.assertEqual(f.read(), "development/\n")

    def test_subdirectory_project_anchors_the_exclude_at_its_folder(self):
        r = self.m.fs_scratch(self.sub, "json")
        self.assertEqual(r["path"], "development/scratch/scratch.json")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "app", r["path"])))
        self.assertEqual(self._untracked(), [])
        with open(os.path.join(self.repo, ".git", "info", "exclude")) as f:
            self.assertIn("/app/development/scratch/", f.read())

    def test_refusals(self):
        with self.assertRaises(ValueError):
            self.m.fs_scratch(self.key, "exe")
        with self.assertRaises(ValueError):
            self.m.fs_scratch(self.key, "../x")
        out = os.path.realpath(_env.make_project("scratch-outside", {"keep.txt": "x\n"}))
        os.symlink(out, os.path.join(self.repo, "development"))
        with self.assertRaises(ValueError):
            self.m.fs_scratch(self.key, "sql")
        self.assertEqual(sorted(os.listdir(out)), ["keep.txt"], "nothing was written through the link")

    def test_listing_a_missing_scratch_folder_is_empty_not_an_error(self):
        # Files lists development/scratch for its Scratches count on every draw: a 404 there was a console error (CI piper-gui)
        self.assertEqual(self.m.fs(self.key, "list", {"path": "development/scratch"})["entries"], [])
        self.assertEqual(self.m.fs(self.key, "list", {"path": "development/scratch/"})["entries"], [])
        with self.assertRaises(Exception):
            self.m.fs(self.key, "list", {"path": "development/other"})   # any other missing folder still is one
        self.m.fs_scratch(self.key, "sql")
        self.assertEqual([e["name"] for e in self.m.fs(self.key, "list", {"path": "development/scratch"})["entries"]], ["scratch.sql"])
        os.makedirs(os.path.join(self.repo, "development"), exist_ok=True)
        out = os.path.realpath(_env.make_project("scratch-link-out", {"x.sql": "x\n"}))
        shutil.rmtree(os.path.join(self.repo, "development", "scratch"))
        os.symlink(out, os.path.join(self.repo, "development", "scratch"))
        with self.assertRaises(Exception):
            self.m.fs(self.key, "list", {"path": "development/scratch"})   # a link leaving the project is still refused


if __name__ == "__main__":
    unittest.main()
