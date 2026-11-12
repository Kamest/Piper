"""coreupdate: the update prompt is offered only where `piper update` (git pull --ff-only) can take it - a branch
with an upstream, nothing ahead, no modified tracked file. A checkout detached at a release tag (an extension's
wrapper pins it) is never offered one and never asks the network. The newest release comes from `git ls-remote`
of the upstream remote, not the GitHub API; /api/update is the local GUI's only."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first)
import common
import coreupdate
import piperd
from test_extapi import REMOTE_HOST, call
from test_piperd import FakeManager

GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT_ENV.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


class Repo:
    """A bare 'origin' with release tags and a clone of it (the Piper checkout under test)."""

    def __init__(self, root: Path):
        self.origin, self.seed, self.work = root / "origin.git", root / "seed", root / "work"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "init", "-q", "-b", "main", str(self.seed))
        self.commit(self.seed, "one")
        git(self.seed, "tag", "-a", "v0.1.0", "-m", "Piper 0.1.0")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-q", "origin", "main", "--tags")
        git(root, "clone", "-q", str(self.origin), str(self.work))

    @staticmethod
    def commit(cwd, name):
        (Path(cwd) / "piper").mkdir(exist_ok=True)
        (Path(cwd) / "piper" / (name + ".py")).write_text(name + "\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", name)

    def release(self, tag):
        self.commit(self.seed, tag)
        git(self.seed, "tag", "-a", tag, "-m", tag)
        git(self.seed, "push", "-q", "origin", "main", "--tags")


class CoreUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Repo(self.tmp)
        self.enterContext(mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}))
        self.addCleanup(coreupdate._cached.clear)
        self.addCleanup(coreupdate._undeliverable.clear)

    def check(self):
        coreupdate.check(str(self.repo.work), str(self.repo.work / "piper"))
        return coreupdate.state(False, True)

    def test_a_branch_checkout_is_offered_a_newer_release_tag(self):
        self.assertEqual(self.check()["available"], "")          # up to date: nothing to offer
        self.repo.release("v0.2.0")
        st = self.check()
        self.assertEqual((st["available"], st["current"], st["updatable"]), ("0.2.0", "0.1.0", True))
        # a pending restart (piperd runs older code than its checkout) comes first
        self.assertEqual(coreupdate.state(True, True)["available"], "")
        ok, message = coreupdate.update(str(self.repo.work))
        self.assertTrue(ok, message)
        self.assertEqual(git(self.repo.work, "describe", "--tags"), "v0.2.0")
        self.assertEqual(coreupdate.state(False, True)["available"], "")   # until the restarted piperd checks

    def test_a_checkout_pinned_to_a_tag_is_never_offered_and_never_asks_the_network(self):
        git(self.repo.work, "-c", "advice.detachedHead=false", "checkout", "-q", "v0.1.0")
        self.repo.release("v0.2.0")
        with mock.patch.object(coreupdate, "remote_latest", side_effect=AssertionError("network")):
            st = self.check()
        self.assertEqual((st["available"], st["updatable"], st["reason"]), ("", False, "pinned"))
        ok, message = coreupdate.update(str(self.repo.work))
        self.assertFalse(ok)
        self.assertIn("pinned", message)
        self.assertEqual(git(self.repo.work, "describe", "--tags"), "v0.1.0")

    def test_modified_ahead_or_upstream_less_checkouts_are_not_offered(self):
        self.repo.release("v0.2.0")
        (self.repo.work / "piper" / "one.py").write_text("changed\n")
        self.assertEqual(self.check()["reason"], "modified")
        git(self.repo.work, "checkout", "-q", "--", ".")
        (self.repo.work / "untracked.txt").write_text("x")      # an untracked file does not block a fast-forward
        self.assertEqual(self.check()["available"], "0.2.0")
        Repo.commit(self.repo.work, "mine")
        st = self.check()
        self.assertEqual((st["available"], st["reason"]), ("", "ahead"))
        git(self.repo.work, "checkout", "-q", "-b", "own")
        self.assertEqual(self.check()["reason"], "no-upstream")
        self.assertFalse(coreupdate.update(str(self.repo.work))[0])

    def test_only_release_tags_count_and_an_unreachable_remote_offers_nothing(self):
        self.repo.release("v0.10.0")
        for tag in ("v0.9.0", "v1.0.0-rc1", "desktop-v9.0.0", "x9.0.0"):
            git(self.repo.seed, "tag", tag)
        git(self.repo.seed, "push", "-q", "origin", "--tags")
        self.assertEqual(coreupdate.remote_latest(str(self.repo.work), "origin"), "0.10.0")
        git(self.repo.work, "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        self.assertEqual(self.check()["available"], "")


    def test_a_branch_without_the_release_tag_is_not_offered_it_again_after_a_pull(self):
        git(self.repo.seed, "push", "-q", "origin", "main:feature")
        git(self.repo.work, "fetch", "-q")
        git(self.repo.work, "checkout", "-q", "-b", "feature", "--track", "origin/feature")
        self.repo.release("v0.2.0")                                   # tagged on main only
        self.assertEqual(self.check()["available"], "0.2.0")
        ok, message = coreupdate.update(str(self.repo.work))
        self.assertFalse(ok)
        self.assertIn("does not have Piper 0.2.0", message)
        self.assertEqual(self.check()["available"], "")              # a later check does not bring it back

    def test_piper_inside_another_repository_is_never_offered_its_tags(self):
        inner = self.repo.work / "bundled"
        inner.mkdir()
        Repo.commit(inner, "core")                                   # bundled/piper/core.py in the outer repository
        self.repo.release("v0.2.0")
        with mock.patch.object(coreupdate, "remote_latest", side_effect=AssertionError("network")):
            coreupdate.check(str(inner), str(inner / "piper"))
        st = coreupdate.state(False, True)
        self.assertEqual((st["available"], st["reason"]), ("", "embedded"))
        ok, message = coreupdate.update(str(inner))
        self.assertFalse(ok)
        self.assertIn("another repository", message)

    def test_git_never_prompts_over_ssh(self):
        with mock.patch.object(coreupdate.subprocess, "run", wraps=subprocess.run) as run:
            coreupdate._git(str(self.repo.work), "status")
        self.assertEqual(run.call_args.kwargs["env"]["GIT_SSH_COMMAND"], "ssh -oBatchMode=yes")


class UpdateRouteTest(unittest.TestCase):
    def setUp(self):
        piperd.MANAGER = FakeManager()
        self.enterContext(mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": REMOTE_HOST}))
        self.audit = []
        self.enterContext(mock.patch.object(common, "audit", self.audit.append))
        self.gui = common.own_token("gui")
        piperd.RESTART.clear()
        self.addCleanup(piperd.RESTART.clear)

    def test_only_the_local_gui_updates_and_never_restarts_by_itself(self):
        with mock.patch.object(coreupdate, "update", return_value=(True, "")) as update, \
                mock.patch.object(piperd, "schedule_restart") as restart:
            for tok, host, extra in ((common.own_token("remote"), REMOTE_HOST, ""), (common.own_token("tty"), None, ""),
                                     (self.gui, None, "X-Piper-Environment: dev\r\n")):
                kw = {"host": host} if host else {}
                self.assertEqual(call("POST", "/api/update", tok, {}, extra=extra, **kw)[0], 403)
            update.assert_not_called()
            update.return_value = (False, "This Piper is pinned to a release by whoever installed it; update it there.")
            status, data = call("POST", "/api/update", self.gui, {})
            self.assertEqual(status, 409)
            self.assertIn("pinned", data["error"])
            update.return_value = (True, "")
            self.assertEqual(call("POST", "/api/update", self.gui, {}), (200, {"ok": True}))
            restart.assert_not_called()   # the GUI asks POST /api/restart and shows its status screen
        self.assertEqual([e["ok"] for e in self.audit if e.get("event") == "piper-update"], [False, True])

    def test_state_reports_the_cached_offer_and_who_may_take_it(self):
        self.addCleanup(coreupdate._cached.clear)
        coreupdate._cached.update(updatable=True, reason="", current="0.1.0", latest="0.2.0")
        with mock.patch.object(piperd.codebuild, "on_disk", return_value=piperd.codebuild.LOADED), \
                mock.patch.object(coreupdate, "_git", side_effect=AssertionError("state never runs git")):
            st = call("GET", "/api/state", self.gui)[1]["core_update"]
            self.assertEqual((st["available"], st["can_update"]), ("0.2.0", True))
            st = call("GET", "/api/state", common.own_token("remote"), host=REMOTE_HOST)[1]["core_update"]
            self.assertEqual((st["available"], st["can_update"]), ("0.2.0", False))
            self.assertIsNone(call("GET", "/api/state", common.own_token("tty"))[1]["core_update"])

    def test_a_test_process_never_starts_the_network_check(self):
        with mock.patch.object(coreupdate.threading, "Thread") as thread:
            coreupdate.start_thread(True)
            thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
