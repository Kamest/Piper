"""Review lane and file rewind: real temporary repositories, no live daemon or agent."""
import json
import os
import secrets
import stat
import time
import unittest
from unittest import mock

import _env
import common
import gitops
import sessions
from test_workbench97 import git, wait, write


class TurnReviewTest(unittest.TestCase):
    def setUp(self):
        name = "review-" + secrets.token_hex(4)
        self.root = os.path.realpath(_env.make_project(name, {
            "src/app.txt": "base\n", "index.txt": "base index\n",
            ".gitattributes": "*.txt text eol=lf\n", ".gitignore": "scratch/\n"}))
        self.key = "acme/" + name
        _env.register(self.key, self.root)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Tester")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "Initial files")
        self.m = _env.manager(self)
        self.s = sessions.Session(project=self.key, path=self.root, state="waiting", idle=True)
        self.m.sessions[self.s.id] = self.s
        self.ts = time.time() - 60

    def event(self, ev):
        self.ts += 1
        self.s.state = "working" if ev == "UserPromptSubmit" else "waiting"
        self.s.idle = ev != "UserPromptSubmit"
        self.m._turn_event(self.s, ev, {"ts": self.ts, "prompt": "Update the example"})
        self.assertTrue(wait(lambda: not self.s.turn_busy), "snapshot worker did not finish")

    def turn(self, data, **files):
        self.event("UserPromptSubmit")
        write(self.root, "src/app.txt", data, "wb")
        for path, content in files.items():
            write(self.root, path, content, "wb")
        self.event("Stop")
        self.assertTrue(self.s.turns[-1]["to"], "finished turn has no snapshot")

    def preview(self, n=1, undo=""):
        return self.m.turn_revert_preview(self.s.id, n, undo)

    def apply(self, preview):
        return self.m.turn_revert(self.s.id, preview["n"], preview["current"], preview["undo"])

    def read(self, path):
        with open(os.path.join(self.root, path), "rb") as f:
            return f.read()

    def git_state(self):
        return (git(self.root, "rev-parse", "HEAD"), self.read(".git/index"), git(self.root, "show-ref"))

    def test_turn_refs_are_private_and_survive_both_gcs(self):
        self.turn(b"first\r\n\x00\xff")
        tree = self.s.turns[-1]["to"]
        private = self.m._snap_gitdir(self.s)
        ref = "refs/piper/turns/" + self.s.id + "/1"
        self.assertEqual(git(self.root, "--git-dir=" + private, "rev-parse", ref).strip(), tree)
        self.assertNotIn("refs/piper/", git(self.root, "show-ref"))
        self.assertNotIn(tree, git(self.root, "cat-file", "--batch-all-objects", "--batch-check"))
        git(self.root, "gc", "--prune=now")
        git(self.root, "--git-dir=" + private, "gc", "--prune=now")
        env = gitops.snap_env(self.root, self.m._snap_objects(self.s))
        oid = gitops.tree_versions(self.root, tree, ["src/app.txt"], self.m._snap_objects(self.s))["src/app.txt"]["oid"]
        self.assertEqual(gitops.read_blob(self.root, oid, env), b"first\r\n\x00\xff")
        self.assertTrue(gitops.is_tree(self.root, self.s.turns[0]["from"], self.m._snap_objects(self.s)))

    def test_revert_exact_bytes_modes_added_files_index_head_and_durable_undo(self):
        self.turn(b"one\r\n\x00\xff")
        os.chmod(os.path.join(self.root, "src/app.txt"), 0o755)
        # Capture the executable bit in the target turn too.
        self.turn(b"two\r\n\x00\xfe")
        self.turn(b"three\n", **{"src/later.txt": b"added later\n"})
        os.chmod(os.path.join(self.root, "src/app.txt"), 0o644)
        write(self.root, "index.txt", "staged by the user\n")
        git(self.root, "add", "index.txt")
        write(self.root, "unrelated.txt", "not part of any turn\n")
        before = self.git_state()
        p = self.preview(2)
        self.assertEqual({r["path"] for r in p["files"]}, {"src/app.txt", "src/later.txt", "index.txt"})
        removed = next(r for r in p["files"] if r["path"] == "src/later.txt")
        self.assertEqual((removed["removed"], removed["added_in"]), (True, 3))
        result = self.apply(p)
        self.assertIn("src/app.txt", self.m.fsedit_state(self.s)["paths"])
        private = self.m._snap_gitdir(self.s)
        self.assertEqual(git(self.root, "--git-dir=" + private, "rev-parse", "refs/piper/turns/" + self.s.id + "/undo-" + result["undo"]).strip(), self.s.reverts[-1]["before"])
        git(self.root, "--git-dir=" + private, "gc", "--prune=now")
        self.assertEqual(self.read("src/app.txt"), b"two\r\n\x00\xfe")
        self.assertTrue(os.stat(os.path.join(self.root, "src/app.txt")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/later.txt")))
        self.assertEqual(self.read("unrelated.txt"), b"not part of any turn\n")
        self.assertEqual(self.git_state(), before)
        self.assertEqual([t["n"] for t in self.s.turns if t.get("reverted")], [3])
        self.assertIn("src/later.txt", self.s.revert_note)
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            self.s = sessions.Session(**json.load(f))   # daemon restart
        self.m.sessions[self.s.id] = self.s
        self.apply(self.preview(undo=result["undo"]))
        self.assertEqual(self.read("src/app.txt"), b"three\n")
        self.assertFalse(os.stat(os.path.join(self.root, "src/app.txt")).st_mode & stat.S_IXUSR)
        self.assertEqual(self.read("src/later.txt"), b"added later\n")
        self.assertEqual(self.read("index.txt"), b"staged by the user\n")
        self.assertEqual(self.git_state(), before)
        self.assertFalse(any(t.get("reverted") for t in self.s.turns))

    def test_revert_refuses_running_turn_and_stale_confirmation(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        write(self.root, "src/app.txt", "changed after preview\n")
        with self.assertRaisesRegex(ValueError, "Files changed since the preview"):
            self.apply(p)
        self.s.state = "working"
        for action in (lambda: self.preview(), lambda: self.apply(p)):
            with self.assertRaisesRegex(ValueError, "Stop the turn first"):
                action()
        self.assertEqual(self.read("src/app.txt"), b"changed after preview\n")
        self.assertFalse(self.s.reverts)

    def test_sibling_running_and_symlink_guards(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        other = sessions.Session(project=self.key, path=self.root, state="working")
        self.m.sessions[other.id] = other
        with self.assertRaisesRegex(ValueError, "Stop the turn first"):
            self.apply(p)
        other.hidden = True
        os.unlink(os.path.join(self.root, "src/app.txt"))
        os.symlink("../index.txt", os.path.join(self.root, "src/app.txt"))
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.read("index.txt"), b"base index\n")

    def test_oversized_target_refused_before_any_file_write(self):
        self.turn(b"x" * ((2 << 20) + 1))
        self.turn(b"small\n")
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            self.apply(self.preview())
        self.assertEqual(self.read("src/app.txt"), b"small\n")
        self.assertFalse(self.s.reverts)

    def test_partial_write_has_a_pinned_undo_and_no_false_applied_status(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        with mock.patch.object(common, "safe_write", side_effect=OSError("fixture write refusal")):
            with self.assertRaises(OSError):
                self.apply(p)
        self.assertEqual(self.s.reverts[-1]["status"], "saved")
        self.assertEqual(common.audit_tail(1, self.s.id)[0]["status"], "saved")

    def test_review_every_changed_turn_done_prompt_push_and_persistence(self):
        self.turn(b"one\n")
        self.assertTrue(self.s.review_pending)
        self.assertEqual(self.s.review_summary["files"], 1)
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            restored = sessions.Session(**json.load(f))
        self.assertTrue(restored.review_pending)
        self.assertEqual(restored.review_summary, self.s.review_summary)
        self.m.review_done(self.s.id)
        self.assertFalse(self.s.review_pending)
        self.turn(b"two\n")
        self.assertTrue(self.s.review_pending)
        self.event("UserPromptSubmit")
        self.assertFalse(self.s.review_pending)
        self.event("Stop")   # unchanged turn does not enter
        self.assertFalse(self.s.review_pending)
        self.turn(b"three\n")
        with mock.patch.object(self.m, "hidden_deletions", return_value=[]), mock.patch.object(gitops, "push", return_value={"branch": "main"}):
            self.m.git_push(self.s.id)
        self.assertFalse(self.s.review_pending)

    def test_refs_removed_with_session_or_worktree(self):
        self.turn(b"one\n")
        private = self.m._snap_gitdir(self.s)
        self.m.forget_root(self.root)
        self.assertFalse(os.path.exists(private))
        self.assertFalse(self.s.turns)
        self.assertFalse(self.s.review_pending)
        # Same lifecycle method used by Remove, with the PTY explicitly stubbed.
        self.turn(b"new turn\n")
        self.assertTrue(os.path.isdir(private))
        self.s.state = "exited"
        self.m.pty = mock.Mock()
        self.m.pty.call.return_value = {"ok": True}
        self.m.forget(self.s.id)
        self.assertTrue(self.s.hidden)
        self.assertFalse(os.path.exists(private))

    def test_queued_note_uses_send_only_at_idle_and_has_no_file_contents(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        self.apply(self.preview())
        self.assertNotIn("one\n", self.s.revert_note)
        self.s.pid = 123
        self.s.state = "working"
        with mock.patch.object(self.m, "type_text", return_value=True) as typed:
            self.assertFalse(self.m.send(self.s.id, self.s.revert_note, idle_only=True))
            typed.assert_not_called()
            self.s.state, self.s.idle = "waiting", True
            with mock.patch.object(self.m, "auto_type_ok", return_value=True):
                self.assertTrue(self.m.send(self.s.id, self.s.revert_note, idle_only=True))
            self.assertEqual(typed.call_args.kwargs, {"idle_only": True})
        self.assertEqual(self.s.revert_note, "")

    def test_old_turns_are_pinned_before_revert_is_offered(self):
        tree = self.m._snapshot(self.s)
        self.s.turns = [{"n": 1, "from": tree, "to": tree, "started": 1, "ended": 2, "prompt": "Example"}]
        self.m.turns(self.s.id)
        self.assertEqual(git(self.root, "--git-dir=" + self.m._snap_gitdir(self.s), "rev-parse",
                             "refs/piper/turns/" + self.s.id + "/1").strip(), tree)

    def test_subproject_notes_use_the_agents_relative_paths(self):
        self.key = "acme/subproject"
        _env.register(self.key, os.path.join(self.root, "src"))
        self.s.project, self.s.path = self.key, os.path.join(self.root, "src")
        self.turn(b"one\n")
        self.turn(b"two\n")
        self.assertEqual(self.m.review_files(self.s.id)["root"], self.root)
        self.apply(self.preview())
        self.assertIn("- app.txt", self.s.revert_note)
        self.assertNotIn("- src/app.txt", self.s.revert_note)

    def test_hidden_file_guard_and_notes_keep_hidden_names_from_agent(self):
        self.turn(b"one\n", **{"config/secrets/config.txt": b"fixture one\n"})
        self.turn(b"two\n", **{"config/secrets/config.txt": b"fixture two\n"})
        p = self.preview()
        with mock.patch.object(self.m, "_hidden_write_guard", side_effect=ValueError("hidden write refused")):
            with self.assertRaisesRegex(ValueError, "hidden write refused"):
                self.apply(p)
        self.assertFalse(self.s.reverts)
        self.s.state = "exited"   # no sandbox can see the restored bytes
        self.apply(self.preview())
        self.assertNotIn("config/secrets/config.txt", self.s.revert_note)
        events = json.dumps(common.audit_tail(20, self.s.id))
        self.assertNotIn("fixture one", events)
        self.assertNotIn("fixture two", events)


if __name__ == "__main__":
    unittest.main()
