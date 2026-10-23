"""sessioncfg.py directly: a session's config dir (build), its hooks and the login hand-over."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import _env
import common
import sessioncfg


def _read(path):
    with open(path) as f:
        return f.read()


class SessionCfgTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, True)
        shutil.rmtree(common.TEMPLATE_DIR, ignore_errors=True)
        os.makedirs(common.TEMPLATE_DIR)
        self.addCleanup(shutil.rmtree, common.TEMPLATE_DIR, True)
        self.cfg = os.path.join(self.base, "claude")

    def tpl(self, name, data):
        p = os.path.join(common.TEMPLATE_DIR, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def settings(self):
        return json.loads(_read(os.path.join(self.cfg, "settings.json")))

    def test_build_adds_hooks_mcp_and_the_claude_md_block_once(self):
        host = os.path.join(_env.HOME, ".claude")
        self.tpl("settings.json", {"statusLine": {"command": "bash %s/statusline.sh" % host},
                                   "permissions": {"deny": list(sessioncfg.GIT_ASK)}})
        self.tpl("CLAUDE.md", "# mine\n")
        self.tpl("projects/x.jsonl", "never copied")
        with mock.patch.object(sessioncfg.features, "on", side_effect=lambda m: m != "github"):   # W23 flag: its own test
            sessioncfg.build(self.cfg, "/home/u", "session")
        s = self.settings()
        self.assertIn("/home/u/.claude/statusline.sh", s["statusLine"]["command"])
        self.assertEqual(set(sessioncfg.EVENTS) | {"PreToolUse", "PostToolUse"}, set(s["hooks"]))
        self.assertEqual(s["permissions"]["deny"], [])                       # moved to ask (2026-10-05)
        self.assertLessEqual(set(sessioncfg.GIT_ASK), set(s["permissions"]["ask"]))
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "projects", "x.jsonl")))
        mcp = json.loads(_read(os.path.join(self.cfg, "piper", "mcp.json")))["mcpServers"]["piper"]["args"]
        self.assertEqual(mcp[1:], ["--role", "session", "--mailbox", "/home/u/.claude/piper-mailbox"])
        md = _read(os.path.join(self.cfg, "CLAUDE.md"))
        self.assertTrue(md.startswith("# mine"))
        self.assertIn("Read and search your own mounted project locally", md)
        self.assertIn("A missing native `Read` tool is not a Piper lock", md)
        self.assertIn("user to allow Python file reads or unlock Piper's reader merely", md)
        self.assertIn("file locally before editing", md)
        self.assertIn("Honour explicit user/project restrictions", md)
        self.assertIn("never a filesystem path", md)
        self.assertIn("right-hand context panel", md)
        self.assertIn("`Reading other projects` (not in Settings)", md)
        sessioncfg.build(self.cfg, "/home/u", "session")   # a restart: still one block, one hook each
        self.assertEqual(_read(os.path.join(self.cfg, "CLAUDE.md")).count("<!-- Piper:"), 1)
        self.assertEqual(len(self.settings()["hooks"]["Stop"]), 1)

    def test_plain_hole_adds_no_git_approval_rules_and_preserves_user_permissions(self):
        self.tpl("settings.json", {"permissions": {"ask": ["Bash(custom:*)"], "deny": ["Bash(git gc:*)"]}})
        sessioncfg.build(self.cfg, "/home/u", "session", guard_git=False)
        cfg = self.settings()
        self.assertEqual(cfg["permissions"], {"ask": ["Bash(custom:*)"], "deny": ["Bash(git gc:*)"]})
        self.assertNotIn("PreToolUse", cfg["hooks"])
        box = json.loads(_read(os.path.join(self.cfg, "piper", "settings.json")))
        self.assertEqual(box["permissions"]["ask"], [])
        self.assertNotIn("PreToolUse", box["hooks"])
        self.assertIn("PostToolUse", box["hooks"])
        self.assertIn("Settings > Security", _read(os.path.join(self.cfg, "CLAUDE.md")))

    def test_host_box_without_guard_has_no_git_approval_hook(self):
        sessioncfg.build_box(self.cfg, "/home/u/.claude", "/home/u/.piper", "session", guard_git=False)
        cfg = json.loads(_read(os.path.join(self.cfg, "piper", "settings.json")))
        self.assertEqual(cfg["permissions"]["ask"], [])
        self.assertNotIn("PreToolUse", cfg["hooks"])

    def test_disabled_gitlab_does_not_pin_the_module_off_for_the_session(self):
        import features
        from unittest import mock
        with tempfile.TemporaryDirectory() as box, mock.patch.object(features, "on", return_value=False):
            sessioncfg.build_box(box, "/home/agent/.claude", "/home/agent/.piper", "session")
            with open(os.path.join(box, "piper", "mcp.json")) as f:
                args = json.load(f)["mcpServers"]["piper"]["args"]
        self.assertNotIn("--no-gitlab", args)

    def test_generated_git_guidance_allows_authorized_work_in_own_checkout_without_a_pool(self):
        self.tpl("CLAUDE.md", "# mine\nNever switch branches without my explicit request.\n")
        for guarded in (False, True):
            with self.subTest(guarded=guarded):
                sessioncfg.build(self.cfg, "/home/u", "session", guard_git=guarded)
                sessioncfg.build_box(self.cfg, "/home/u/.claude", "/home/u/.piper", "session",
                                     guard_git=guarded)
                for name in ("CLAUDE.md", "piper/system-prompt.md"):
                    with self.subTest(output=name):
                        md = " ".join(_read(os.path.join(self.cfg, name)).split())
                        self.assertIn("Branch switches and commits in this session's checkout", md)
                        self.assertIn("follow the user's task, project instructions and Security", md)
                        self.assertIn("Do not change another checkout's HEAD", md)
                        self.assertIn("An unset `HOLE_WORKTREES_DIR` does not prohibit", md)
                        self.assertIn("Piper defaults it to `.claude/worktrees` inside this session's mounted project", md)
                        self.assertIn("`git.worktreeLinks: rw` and `git.worktreePool: true`", md)
                        self.assertIn("Do not invent a pool path or use an unmounted sibling", md)
                        self.assertNotIn("or change the main checkout's HEAD", md)
                self.assertIn("Never switch branches without my explicit request.",
                              _read(os.path.join(self.cfg, "CLAUDE.md")))

    def test_github_paragraph_only_while_the_module_is_on(self):
        # W23: the github_* tools exist only with the GitHub module; a prodcheck never gets them
        for mod, role, want in ((True, "session", True), (False, "session", False), (True, "prodcheck", False)):
            with mock.patch.object(sessioncfg.features, "on", side_effect=lambda m, v=mod: v if m == "github" else True):
                sessioncfg.build(self.cfg, "/home/u", role)
            md = _read(os.path.join(self.cfg, "CLAUDE.md"))
            self.assertEqual("github_issue" in md, want, (mod, role))
            self.assertIn("`gitlab_mr_create` creates a GitLab MR", md)
            self.assertIn("`github_pr_create` creates a GitHub PR", md)
            self.assertIn("a local `gh` binary does not supply the host authentication", md)
            if want:
                self.assertIn("`github_pr`, `github_pr_create`, `github_inbox`", md)
            self.assertEqual(md.count("<!-- Piper:"), 1)
            args = json.loads(_read(os.path.join(self.cfg, "piper", "mcp.json")))["mcpServers"]["piper"]["args"]
            self.assertEqual("--github" in args, want, (mod, role))

    def test_build_survives_odd_settings_in_the_template(self):
        # the template is the user's own ~/.claude/settings.json: valid JSON of an unexpected shape
        # must not stop every session from starting
        for odd in ([], "text", {"hooks": []}, {"hooks": {"Stop": {}, "PreToolUse": None}}, {"permissions": []}):
            self.tpl("settings.json", odd)
            sessioncfg.build(self.cfg, "/home/u", "session")
            s = self.settings()
            self.assertIsInstance(s, dict, odd)
            self.assertEqual(len(s["hooks"]["Stop"]), 1, odd)
            self.assertLessEqual(set(sessioncfg.GIT_ASK), set(s["permissions"]["ask"]), odd)

    def test_build_keeps_transcripts_and_login_but_nothing_else_of_the_previous_dir(self):
        sessioncfg.build(self.cfg, "/home/u", "session")
        os.makedirs(os.path.join(self.cfg, "projects"))
        with open(os.path.join(self.cfg, "projects", "t.jsonl"), "w") as f:
            f.write("{}")
        with open(os.path.join(self.cfg, ".credentials.json"), "w") as f:
            f.write('{"k": 1}')
        with open(os.path.join(self.cfg, "planted.sh"), "w") as f:
            f.write("evil")
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        sessioncfg.build(self.cfg, "/home/u", "session")
        self.assertTrue(os.path.exists(os.path.join(self.cfg, "projects", "t.jsonl")))
        self.assertEqual(_read(os.path.join(self.cfg, ".credentials.json")), '{"k": 1}')
        self.assertFalse(os.path.exists(os.path.join(self.cfg, "planted.sh")))
        self.assertFalse([n for n in os.listdir(self.base) if ".prev-" in n])

    def test_a_symlinked_login_is_never_copied_back(self):
        os.makedirs(self.cfg)
        secret = os.path.join(self.base, "gitlab-token")
        with open(secret, "w") as f:
            f.write('{"token": "PLANTED"}')
        os.symlink(secret, os.path.join(self.cfg, ".credentials.json"))
        self.assertEqual(sessioncfg.copy_back_credentials(self.cfg, ""), "")
        self.assertFalse(os.path.exists(os.path.join(common.TEMPLATE_DIR, ".credentials.json")))

    def test_login_goes_back_only_when_it_changed(self):
        os.makedirs(self.cfg)
        self.tpl(".credentials.json", '{"a": 1}')
        seeded = sessioncfg.seed_credentials(self.cfg)
        self.assertEqual(sessioncfg.copy_back_credentials(self.cfg, seeded), "")
        with open(os.path.join(self.cfg, ".credentials.json"), "w") as f:
            f.write('{"a": 2}')
        self.assertEqual(sessioncfg.copy_back_credentials(self.cfg, seeded, dry=True),
                         sessioncfg.login_digest(b'{"a": 2}'))
        self.assertEqual(_read(os.path.join(common.TEMPLATE_DIR, ".credentials.json")), '{"a": 1}')
        self.assertTrue(sessioncfg.copy_back_credentials(self.cfg, seeded))
        self.assertEqual(_read(os.path.join(common.TEMPLATE_DIR, ".credentials.json")), '{"a": 2}')
        with open(os.path.join(self.cfg, ".credentials.json"), "w") as f:
            f.write("not json")
        self.assertEqual(sessioncfg.copy_back_credentials(self.cfg, seeded), "")

    def test_referenced_files_stay_below_claude(self):
        host = "/Users/me/.claude"
        p = os.path.join(self.base, "s.json")
        with open(p, "w") as f:
            json.dump({"a": "bash /Users/me/.claude/hooks/x.sh", "b": "~/.claude/../.ssh/id_rsa",
                       "c": "$HOME/.claude/statusline.sh"}, f)
        self.assertEqual(sorted(sessioncfg.referenced_files(p, host)), ["hooks/x.sh", "statusline.sh"])

    def test_fresh_dir_never_follows_a_planted_link(self):
        target = os.path.join(self.base, "keep")
        os.makedirs(target)
        open(os.path.join(target, "f"), "w").close()
        d = os.path.join(self.base, "box")
        os.symlink(target, d)
        sessioncfg.fresh_dir(d)
        self.assertTrue(os.path.isdir(d) and not os.path.islink(d))
        self.assertTrue(os.path.exists(os.path.join(target, "f")))


if __name__ == "__main__":
    unittest.main()
