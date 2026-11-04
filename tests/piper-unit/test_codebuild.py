"""The displayed product release is the running daemon's identity, never its API protocol."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock
import _env
import codebuild

class ReleaseVersionTest(unittest.TestCase):
    def test_service_instance_is_stable_until_a_new_process_starts(self):
        self.assertRegex(codebuild.INSTANCE, r"^[0-9a-f]{24}$")
        self.assertEqual(codebuild.state_fields()["service_instance"], codebuild.INSTANCE)
        fresh = subprocess.run(["python3", "-c", "import codebuild; print(codebuild.INSTANCE)"],
                               cwd=os.path.dirname(codebuild.__file__), capture_output=True, text=True, check=True)
        self.assertRegex(fresh.stdout.strip(), r"^[0-9a-f]{24}$")
        self.assertNotEqual(fresh.stdout.strip(), codebuild.INSTANCE)

    def test_release_build_and_modified_tree(self):
        for description, expected in [
            ("v0.1.4-0-g123abcd", "0.1.4"),
            ("v0.1.4-4-g123abcd", "0.1.4+4.g123abcd"),
            ("v0.1.4-0-g123abcd-dirty", "0.1.4+modified"),
            ("v0.1.4-4-g123abcd-dirty", "0.1.4+4.g123abcd.modified"),
            ("desktop-v0.1.8-0-g123abcd", None),
            ("not-a-release", None)]:
            with self.subTest(description=description), mock.patch.object(codebuild.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], 0, description)):
                self.assertEqual(codebuild.release_version(), expected)

    def test_missing_git_tags_and_timeouts_are_not_fabricated_versions(self):
        with mock.patch.object(codebuild.subprocess, "run", return_value=subprocess.CompletedProcess([], 128, "")):
            self.assertIsNone(codebuild.release_version())
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("git", 2)):
            with mock.patch.object(codebuild.subprocess, "run", side_effect=error):
                self.assertIsNone(codebuild.release_version())

    def test_state_keeps_the_loaded_identity_when_disk_changes(self):
        with mock.patch.object(codebuild, "RELEASE", "0.1.4"), mock.patch.object(codebuild, "on_disk", return_value="new-code"):
            with mock.patch.object(codebuild, "release_version", side_effect=AssertionError("re-read current checkout")):
                state = codebuild.state_fields()
        self.assertEqual(state["service_version"], "0.1.4")
        self.assertEqual(state["code_build"], codebuild.LOADED)
        self.assertTrue(state["code_outdated"])

    def test_real_git_repository_uses_only_core_tags(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, text=True)
            git("init", "-q")
            git("config", "user.name", "Piper test")
            git("config", "user.email", "piper-test@example.invalid")
            os.mkdir(os.path.join(root, "piper"))
            source = os.path.join(root, "piper", "app.py")
            with open(source, "w") as out:
                out.write("initial\n")
            git("add", ".")
            git("commit", "-qm", "Initial")
            git("tag", "v0.1.4")
            git("tag", "desktop-v0.1.8")
            self.assertEqual(codebuild.release_version(os.path.join(root, "piper")), "0.1.4")
            with open(source, "a") as out:
                out.write("changed\n")
            self.assertEqual(codebuild.release_version(os.path.join(root, "piper")), "0.1.4+modified")
            git("commit", "-qam", "Next")
            self.assertRegex(codebuild.release_version(os.path.join(root, "piper")), r"^0\.1\.4\+1\.g[0-9a-f]+$")


class RestartReadinessTest(unittest.TestCase):
    def test_compiles_without_importing_and_returns_safe_diagnostics(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as folder:
            root = os.path.join(folder, "piper")
            os.mkdir(root)
            source = os.path.join(root, "example.py")
            with open(source, "w") as out:
                out.write("raise AssertionError('must not execute')\n")
            self.assertIsNone(codebuild.restart_error(root))
            with open(source, "w") as out:
                out.write("<<<<<<< HEAD\nprivate_example = 1\n=======\n>>>>>>> example\n")
            error = codebuild.restart_error(root)
            self.assertIn("update is not ready", error)
            self.assertIn("piper/example.py", error)
            self.assertNotIn(folder, error)
            self.assertNotIn("private_example", error)
            with mock.patch.object(codebuild, "_files", return_value=[source]), \
                    mock.patch("builtins.open", side_effect=OSError("private details")):
                error = codebuild.restart_error(root)
            self.assertIn("update is not ready", error)
            self.assertNotIn("private details", error)

    def test_unfinished_merge_blocks_a_syntactically_valid_checkout_and_linked_worktree(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as folder:
            root = os.path.join(folder, "piper")
            os.mkdir(root)
            with open(os.path.join(root, "example.py"), "w") as out:
                out.write("pass\n")
            gitdir = os.path.join(folder, ".git")
            os.mkdir(gitdir)
            merge = os.path.join(gitdir, "MERGE_HEAD")
            with open(merge, "w") as out:
                out.write("a" * 40 + "\n")
            self.assertIn("merge", codebuild.restart_error(root))
            os.unlink(merge)
            self.assertIsNone(codebuild.restart_error(root))
            os.rename(gitdir, os.path.join(folder, "linked-git"))
            with open(gitdir, "w") as out:
                out.write("gitdir: linked-git\n")
            with open(os.path.join(folder, "linked-git", "MERGE_HEAD"), "w") as out:
                out.write("b" * 40 + "\n")
            self.assertIn("merge", codebuild.restart_error(root))
