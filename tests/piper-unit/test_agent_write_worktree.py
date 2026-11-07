"""Writes use the actual linked checkout while preserving busy-tree and approval guards."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import _env
import sessions


class AgentWriteWorktreeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.key = "team-x/write-trees"
        _env.register(self.key, self.root)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Piper Test")
        self.git("config", "user.email", "test@example.invalid")
        with open(os.path.join(self.root, "file.txt"), "w") as stream:
            stream.write("base\n")
        self.git("add", "file.txt")
        self.git("commit", "-qm", "Base")
        self.mine = os.path.join(self.root, ".claude", "worktrees", "mine")
        self.other = os.path.join(self.root, ".claude", "worktrees", "other")
        self.git("worktree", "add", "--detach", self.mine)
        self.git("worktree", "add", "--detach", self.other)
        self.m = _env.manager(self)
        self.m.policy["write_guard"] = False
        self.m.ask = mock.Mock(return_value=True)
        home = _env.make_project("agent-write-caller", {"file.txt": "caller"})
        self.caller = sessions.Session(kind="project", project="team-x/agent-write-caller",
                                       path=home, state="working",
                                       unlock={"projects": True, "projects_write": True})
        self.busy = sessions.Session(kind="project", project=self.key + "@other",
                                     path=self.other, state="working", title="Other checkout")
        for session in (self.caller, self.busy):
            self.m.sessions[session.id] = session

    def git(self, *args):
        return subprocess.run(["git", "-C", self.root, *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, name="mine", **extra):
        return self.m.tool(self.caller, "write_file", {
            "project": self.key, "path": ".claude/worktrees/" + name + "/file.txt",
            "content": "updated\n", **extra})

    def test_gui_listing_returns_real_derived_root_without_registering_worktree(self):
        self.assertNotIn(self.key + "@mine", sessions.projects.load())
        listing = self.m.fs(self.key + "@mine", "list", {"path": "."})
        self.assertEqual(listing["root"], os.path.realpath(self.mine))
        self.assertEqual(listing["path"], ".")
        self.assertIn("file.txt", [entry["name"] for entry in listing["entries"]])
        self.assertNotIn(self.key + "@mine", sessions.projects.load())
        scratch = self.m.fs(self.key + "@mine", "list", {"path": sessions.workbench.SCRATCH_DIR})
        self.assertEqual(scratch["root"], os.path.realpath(self.mine))

    def test_base_project_path_targets_only_the_validated_worktree(self):
        result = self.write()
        self.assertEqual(result["project"], self.key + "@mine")
        self.assertEqual(result["path"], "file.txt")
        with open(os.path.join(self.mine, "file.txt")) as stream:
            self.assertEqual(stream.read(), "updated\n")
        for root in (self.root, self.other):
            with open(os.path.join(root, "file.txt")) as stream:
                self.assertEqual(stream.read(), "base\n")
        self.assertEqual(self.m.ask.call_args.args[3]["project"], self.key + "@mine")

    def test_explicit_worktree_key_uses_the_same_scope(self):
        result = self.m.tool(self.caller, "write_file", {
            "project": self.key + "@mine", "path": "file.txt", "content": "explicit\n"})
        self.assertEqual(result["project"], self.key + "@mine")

    def test_the_working_target_is_still_blocked(self):
        with self.assertRaisesRegex(ValueError, "working in that tree"):
            self.write("other")
        self.m.ask.assert_not_called()

    def test_a_parent_tree_mount_still_blocks_the_nested_target(self):
        self.busy.coverage = [{"kind": "project", "root": self.root}]
        with self.assertRaisesRegex(ValueError, "working in that tree"):
            self.write()

    def test_a_session_starting_in_the_target_during_approval_is_still_blocked(self):
        def approve(*args, **kwargs):
            self.busy.path = self.mine
            return True
        self.m.ask.side_effect = approve
        with self.assertRaisesRegex(ValueError, "working in that tree"):
            self.write()
        self.m.ask.assert_called_once()
        with open(os.path.join(self.mine, "file.txt")) as stream:
            self.assertEqual(stream.read(), "base\n")

    def test_an_unvalidated_worktree_directory_does_not_narrow_the_lock(self):
        fake = os.path.join(self.root, ".claude", "worktrees", "fake")
        os.makedirs(fake)
        with self.assertRaisesRegex(ValueError, "working in that tree"):
            self.write("fake")
