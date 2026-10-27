"""Guard: hole glob semantics, L3 coverage, L1 settings approval, L4 mount attestation."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper"))

import _env  # noqa: E402,F401  (first: an isolated home before any Piper module)
import guard  # noqa: E402


def touch(root, rel, content=""):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


class HookProblemsTest(unittest.TestCase):
    def test_missing_and_crlf_prestart_hooks(self):
        d = tempfile.mkdtemp()
        touch(d, "ok.sh", "#!/bin/sh\necho ok\n")
        touch(d, "crlf.sh", "#!/bin/sh\r\necho ok\r\n")
        eff = {"hooks": {"prestart": [{"script": "$HP/ok.sh"}, {"script": "$HP/crlf.sh"},
                                      {"script": "$HP/gone.sh"}],
                         "setup": [{"script": "$HP/gone-too.sh"}]}}
        out = guard.hook_problems(eff, "/home/x", {"HP": d})
        self.assertEqual(len(out), 2, out)
        self.assertIn("crlf.sh: CRLF", out[0])
        self.assertIn("gone.sh: cannot be read", out[1])


class GlobTest(unittest.TestCase):
    def test_star_does_not_cross_slash(self):
        self.assertTrue(guard.match("services/*/secrets", "services/a/secrets"))
        self.assertFalse(guard.match("services/*/secrets", "services/a/b/secrets"))
        self.assertFalse(guard.match("*", "a/b"))

    def test_doublestar_zero_or_more(self):
        self.assertTrue(guard.match("**/config/secrets", "config/secrets"))
        self.assertTrue(guard.match("**/config/secrets", "x/y/config/secrets"))
        self.assertTrue(guard.match("**/.git", ".git"))
        self.assertFalse(guard.match("**/.git", ".gitignore"))

    def test_star_matches_dotfiles_like_hole(self):
        self.assertTrue(guard.match("*", ".env"))

    def test_hidden_by_covers_files_inside_hidden_dirs(self):
        c = ["**/config/secrets", "**/.git"]
        self.assertTrue(guard.hidden_by(c, "config/secrets/db.properties"))
        self.assertTrue(guard.hidden_by(c, "a/b/config/secrets/x/y"))
        self.assertTrue(guard.hidden_by(c, ".git/config"))
        self.assertFalse(guard.hidden_by(c, "config/secretsX/a"))
        self.assertFalse(guard.hidden_by(c, "src/config.java"))

    def test_hidden_by_ignores_case_and_unicode_form(self):
        """APFS opens Config/Secrets/x for config/secrets/x and NFD for NFC (review 2026-10-05)"""
        import unicodedata
        c = ["**/config/secrets", "**/.git", ".env*", "privé"]
        for rel in ("Config/Secrets/db.properties", "CONFIG/SECRETS", ".GIT/config", ".Env", "./config/secrets/x",
                    unicodedata.normalize("NFD", "privé/x")):
            self.assertTrue(guard.hidden_by(c, rel), rel)
        self.assertTrue(guard.hidden_by([unicodedata.normalize("NFD", "privé")], "privé/x"))
        self.assertFalse(guard.hidden_by(c, "src/Config.java"))
        self.assertTrue(guard.is_git_path(".GIT/hooks/post-checkout"))
        self.assertTrue(guard.is_git_path("sub/.Git"))
        self.assertFalse(guard.is_git_path(".gitignore"))

    def test_hidden_by_agrees_with_match_over_every_prefix(self):
        """hidden_by uses one compiled regex per pattern; it must answer exactly what
        match() over each directory prefix answers (the original definition)."""
        import itertools
        import random

        def reference(cands, rel):
            parts = [p for p in rel.strip("/").split("/") if p]
            return any(guard.match(c, "/".join(parts[:k])) for k in range(1, len(parts) + 1) for c in cands)

        pats = ["**", "**/.git", "a", "a/**", "**/b", "a/**/b", "*/b", "a/*", "**/c/**", "x[!a]y", "x[0-9]",
                "*.xml", "**/config/secrets.yml", "**/webapp/services/*/secrets", ".env*", "a/b/c", "**/**/b", "./a", "/a/"]
        segs = ["a", "b", "c", "x/y", "xay", "x1", "xby", ".git", ".env.local", "config", "secrets.yml",
                "webapp", "services", "shop", "secrets", "d.xml", "q"]
        rnd = random.Random(7)
        paths = {"/".join(p) for n in (1, 2, 3) for p in itertools.product(segs[:8], repeat=n)}
        paths |= {"/".join(rnd.choice(segs) for _ in range(rnd.randint(1, 7))) for _ in range(3000)}
        for pat in pats:
            for rel in paths:
                self.assertEqual(guard.hidden_by([pat], rel), reference([pat], rel), (pat, rel))
        self.assertFalse(guard.hidden_by(["**"], ""))

    def test_class_and_negation(self):
        self.assertTrue(guard.match("f[0-9]", "f1"))
        self.assertTrue(guard.match("f[!0-9]", "fx"))
        self.assertFalse(guard.match("f[!0-9]", "f1"))


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        touch(self.root, "config/secrets/app-production.properties", "db.password=x")
        touch(self.root, "config/secrets.yml")
        touch(self.root, "webapp/services/shop/secrets/k8s-dev.js")
        touch(self.root, "webapp/node_modules/x/config/secrets/p")   # skipped dir
        touch(self.root, ".git/HEAD", "ref: refs/heads/master")
        touch(self.root, "src/Main.java")
        self.policy = guard.load_policy("/nonexistent")

    def test_candidates(self):
        c = guard.scan_candidates(self.root, self.policy)
        self.assertEqual(c, ["config/secrets", "config/secrets.yml", "webapp/services/shop/secrets"])   # .git is open

    def test_git_dir_content_is_no_candidate(self):
        """.git is open (2026-10-01): a submodule git dir or a ref named like a key inside it
        must neither block the start nor be "fixed" with an exclude inside .git"""
        touch(self.root, ".git/modules/services/shop/secrets/HEAD", "x")
        touch(self.root, ".git/refs/heads/release/server.key", "0" * 40)
        self.assertFalse([c for c in guard.scan_candidates(self.root, self.policy) if c.startswith(".git")])
        hidden = dict(self.policy, candidates=list(self.policy["candidates"]) + ["**/.git"])
        self.assertIn(".git", guard.scan_candidates(self.root, hidden))   # hidden again: still a candidate itself

    def test_uncovered_without_git_exclude(self):
        excl = ["config/secrets", "config/secrets.yml", "webapp/services/*/secrets"]
        self.assertEqual(guard.coverage(self.root, excl, self.policy)["uncovered"], [])   # git open (2026-10-01)
        pol = dict(self.policy, candidates=list(self.policy["candidates"]) + ["**/.git"])   # hidden again
        self.assertEqual(guard.coverage(self.root, excl, pol)["uncovered"], [".git"])

    def test_fully_covered(self):
        cov = guard.coverage(self.root, [".git", "config/secrets", "config/secrets.yml",
                                         "webapp/services/*/secrets", "missing/path"], self.policy)
        self.assertEqual(cov["uncovered"], [])

    def test_parent_exclude_covers_child(self):
        cov = guard.coverage(self.root, [".git", "config", "webapp/services"], self.policy)
        self.assertEqual(cov["uncovered"], [])

    def test_library_allow_applies_to_its_root_only(self):
        pol = dict(self.policy, library_allow={self.root: ["**/.git"]},   # .git a candidate again (hidden)
                   candidates=list(self.policy["candidates"]) + ["**/.git"])
        cov = guard.coverage(self.root, ["config", "webapp"], pol)
        self.assertEqual(cov["uncovered"], [])
        other = tempfile.mkdtemp()
        touch(other, ".git/HEAD", "ref: refs/heads/master")
        self.assertEqual(guard.coverage(other, [], pol)["uncovered"], [".git"])

    def test_allow_list(self):
        pol = dict(self.policy, allow=["**/.git"])
        cov = guard.coverage(self.root, ["config", "webapp"], pol)
        self.assertEqual(cov["uncovered"], [])

    def test_candidate_reached_only_through_a_symlink_is_found_and_uncovered(self):
        touch(self.root, "modules/shop/secrets/k8s-dev.js", "secret")
        os.makedirs(os.path.join(self.root, "webapp", "services"), exist_ok=True)
        os.symlink("../../modules/shop", os.path.join(self.root, "webapp", "services", "shop2"))
        cov = guard.coverage(self.root, [".git", "config/secrets", "config/secrets.yml", "webapp/services/*/secrets"],
                             self.policy)
        self.assertIn("webapp/services/shop2/secrets", cov["candidates"])
        self.assertIn("webapp/services/shop2/secrets", cov["uncovered"])   # hole's glob never descends the link
        cov = guard.coverage(self.root, [".git", "config", "webapp/services/*/secrets", "modules/shop/secrets"], self.policy)
        self.assertNotIn("webapp/services/shop2/secrets", cov["uncovered"])   # the resolved path is excluded

    def test_pnpm_store_project_links_are_not_reported_twice(self):
        """pnpm >= 10.27 registers every project using the store as a link in
        <store>/v10/projects/<hash>; a store inside the root showed each worktree twice."""
        touch(self.root, ".claude/worktrees/agent-a1/.env", "x")
        os.makedirs(os.path.join(self.root, ".pnpm-store", "v10", "projects"))
        os.symlink("../../../.claude/worktrees/agent-a1", os.path.join(self.root, ".pnpm-store", "v10", "projects", "093e"))
        c = guard.scan_candidates(self.root, self.policy)
        self.assertIn(".claude/worktrees/agent-a1/.env", c)
        self.assertFalse([x for x in c if x.startswith(".pnpm-store/")])

    def test_a_sibling_symlink_never_hides_the_real_dir(self):
        """scandir order is arbitrary (APFS: by hash): whatever the link's name, the real
        config/secrets must be found under its own name."""
        for name in ("cfglink", "aaa", "zzz", "b", "c0"):
            root = tempfile.mkdtemp()
            touch(root, "config/secrets/db.properties", "pw")
            os.symlink("config", os.path.join(root, name))
            c = guard.scan_candidates(root, self.policy)
            self.assertIn("config/secrets", c, name)
            cov = guard.coverage(root, [], self.policy)
            self.assertIn("config/secrets", cov["uncovered"], name)

    def test_symlink_cycles_and_outside_links_are_not_walked(self):
        os.symlink(self.root, os.path.join(self.root, "loop"))
        outside = tempfile.mkdtemp()
        touch(outside, "config/secrets/x", "y")
        os.symlink(outside, os.path.join(self.root, "ext"))
        c = guard.scan_candidates(self.root, self.policy)
        self.assertFalse(any(x.startswith("ext/") for x in c))
        self.assertEqual(len([x for x in c if x.endswith("config/secrets")]), 1)

    def test_symlink_dag_is_bounded_and_fail_closed(self):
        import time as _t
        root = tempfile.mkdtemp()
        for i in range(1, 21):
            os.makedirs(os.path.join(root, "d%d" % i), exist_ok=True)
        for i in range(1, 20):
            for k in range(3):
                os.symlink(os.path.join(root, "d%d" % (i + 1)), os.path.join(root, "d%d" % i, "l%d" % k))
        t0 = _t.time()
        cov = guard.coverage(root, [], self.policy)
        self.assertLess(_t.time() - t0, 10)
        self.assertTrue(cov["truncated"])

    def test_link_into_a_skipped_dir_is_not_walked(self):
        touch(self.root, "webapp/node_modules/x/config/secrets/p", "x")
        os.symlink("webapp/node_modules", os.path.join(self.root, "foo"))
        self.assertFalse(any(c.startswith("foo/") for c in guard.scan_candidates(self.root, self.policy)))

    def test_depth_limit_is_fail_closed(self):
        pol = dict(self.policy, max_depth=2)
        touch(self.root, "a/b/c/d/config/secrets/p", "x")
        cov = guard.coverage(self.root, [".git", "config", "webapp"], pol)
        self.assertTrue(cov["truncated"])

    def test_glob_does_not_descend_symlinked_dir(self):
        touch(self.root, "real/services/a/secrets/x")
        os.symlink(os.path.join(self.root, "real", "services"), os.path.join(self.root, "services"))
        self.assertEqual(guard.expand_exclude(self.root, "services/*/secrets"), [])
        self.assertEqual(guard.expand_exclude(self.root, "real/services/*/secrets"), ["real/services/a/secrets"])


class LoadPolicyTest(unittest.TestCase):
    def _load(self, user):
        import json
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "policy.json"), "w") as f:
            json.dump(user, f)
        return guard.load_policy(os.path.join(d, "policy.json"))

    def test_saved_candidates_lose_git_while_git_is_open(self):
        """a list saved in Settings before 2026-10-01 still names .git: it would block every start"""
        pol = self._load({"candidates": ["**/.git", "**/config/secrets", ".git"]})
        self.assertEqual(pol["candidates"], ["**/config/secrets"])
        pol = self._load({"candidates": ["**/.git", "**/config/secrets"],
                          "holehome": {"force_exclude": [".git"]}})   # hidden again: kept, as before
        self.assertEqual(pol["candidates"], ["**/.git", "**/config/secrets"])
        self.assertTrue(guard.git_excluded(["**/.git"]) and guard.git_excluded([".git"]))
        self.assertFalse(guard.git_excluded(["config/secrets", ".env"]))


class SettingsTest(unittest.TestCase):
    def test_merge_like_hole(self):
        m = guard.merge({"a": [1, 2], "o": {"x": 1}, "s": "lo"}, {"a": [2, 3], "o": {"y": 2}, "s": "hi"})
        self.assertEqual(m, {"a": [1, 2, 3], "o": {"x": 1, "y": 2}, "s": "hi"})

    def test_effective_order(self):
        base = {"files": {"exclude": [".git"]}, "profiles": {"p": {"files": {"exclude": ["b"]}}}}
        proj = {"files": {"exclude": ["c"]}, "profiles": {"p": {"files": {"exclude": ["d"]}}}}
        self.assertEqual(guard.effective(base, proj, "p")["files"]["exclude"], [".git", "b", "c", "d"])

    def test_extends_chain_like_hole(self):
        base = {"libraries": {"/base": {}}, "profiles": {"acme": {"libraries": {"/acme/webstore": {}}},
                                                        "top": {"extends": ["acme"], "files": {"exclude": ["t"]}}}}
        proj = {"profiles": {"dev": {"extends": "top"}}}
        self.assertEqual(guard.resolve_chain(base, proj, "dev"), ["acme", "top", "dev"])
        eff = guard.effective(base, proj, "dev")
        self.assertEqual(sorted(eff["libraries"]), ["/acme/webstore", "/base"])
        self.assertEqual(eff["files"]["exclude"], ["t"])
        with self.assertRaises(ValueError):
            guard.resolve_chain(base, {"profiles": {"a": {"extends": "b"}, "b": {"extends": "a"}}}, "a")
        with self.assertRaises(ValueError):
            guard.resolve_chain(base, proj, "nope")

    def test_forbidden_keys(self):
        pol = guard.load_policy("/nonexistent")
        self.assertEqual(guard.forbidden_keys({"git": {"worktreeLinks": "off"}}, pol), [])
        self.assertEqual(guard.forbidden_keys({"git": {"worktreeLinks": "rw", "worktreePool": True}}, pol), [])   # git_mode hole
        guarded = dict(pol, git_mode="guarded")
        self.assertEqual(guard.forbidden_keys({"git": {"worktreeLinks": "off"}}, guarded), [])
        self.assertEqual(guard.forbidden_keys({"git": {"worktreeLinks": "rw", "worktreePool": True}}, guarded),
                         ["git.worktreeLinks", "git.worktreePool"])
        self.assertEqual(guard.forbidden_keys({"profiles": {"x": {"agents": {}}}}, pol), ["profiles.x.agents"])
        self.assertEqual(guard.forbidden_keys({"container": {"baseImage": "x"}}, pol), ["container.baseImage"])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        touch(self.proj, "config/secrets/a.properties")
        touch(self.proj, ".git/HEAD")
        self.lib = tempfile.mkdtemp()
        touch(self.lib, "config/secrets/b.properties")
        self.policy = guard.load_policy("/nonexistent")
        self.base = {"files": {"exclude": [".git", "config/secrets"]}}

    def kinds(self, r):
        return sorted({b["kind"] for b in r["blocks"]})

    def test_absent_settings_need_no_approval_but_deleting_approved_ones_blocks(self):
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertTrue(r["ok"], r["blocks"])                          # nothing there, nothing to review
        path = os.path.join(self.proj, ".hole", "settings.json")
        r = guard.preflight(self.proj, self.base, None, self.policy, {path: {"sha": "abc"}}, [], "/home")
        self.assertEqual(self.kinds(r), ["settings-changed"])          # an approved file vanished (K15)

    def test_only_tightening_settings_change_passes_without_approval(self):
        old = '{"files": {"exclude": ["a.pem"]}, "network": {"allow": ["x.example"]}}'
        t = lambda new, **pol: guard.only_tightens(old, new, pol)  # noqa: E731
        self.assertTrue(t('{"files": {"exclude": ["a.pem", "b.key"]}, "network": {"allow": ["x.example"]}}'))
        self.assertFalse(t('{"files": {"exclude": []}, "network": {"allow": ["x.example"]}}'))           # exclude removed
        self.assertFalse(t('{"files": {"exclude": ["a.pem"], "include": {"~/.ssh": "~/.ssh"}}, "network": {"allow": ["x.example"]}}'))
        self.assertFalse(t('{"files": {"exclude": ["a.pem"]}, "network": {"allow": ["y.example"]}}'))   # filtered egress
        self.assertTrue(t('{"files": {"exclude": ["a.pem"]}, "network": {"allow": ["y.example"]}}', unrestricted_network=True))
        self.assertFalse(t('{"files": {"exclude": ["a.pem"]}, "network": {"hostGatewayDomains": ["h:22"]}}',
                           unrestricted_network=True))                                           # host ports, never
        self.assertFalse(guard.only_tightens(None, "{}", {}))                                      # nothing to compare

    def test_first_start_needs_approval_then_passes(self):
        touch(self.proj, ".hole/settings.json", "{}")
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertEqual(self.kinds(r), ["settings-changed"])
        approved = {p: {"sha": s} for p, s in r["settings"].items()}
        r2 = guard.preflight(self.proj, self.base, None, self.policy, approved, [], "/home")
        self.assertTrue(r2["ok"], r2["blocks"])

    def test_blank_settings_are_repaired_and_keep_their_approval(self):
        # hole refuses a blank .hole/settings.json - it becomes {} before the start (acme-app)
        touch(self.proj, ".hole/settings.json", "")
        touch(self.lib, ".hole/settings.json", " \n")
        path, lpath = (os.path.join(d, ".hole", "settings.json") for d in (self.proj, self.lib))
        blank = {"sha": hashlib.sha256(b"").hexdigest(), "text": ""}
        approved = {path: blank, lpath: {"sha": hashlib.sha256(b" \n").hexdigest(), "text": " \n"}}
        base = dict(self.base, libraries={self.lib: {"path": self.lib}})
        r = guard.preflight(self.proj, base, None, self.policy, approved, [], "/home")
        self.assertEqual(sorted(r["repaired"]), sorted([path, lpath]))
        self.assertNotIn("settings-changed", self.kinds(r))
        for p in (path, lpath):
            with open(p) as f:
                self.assertEqual(f.read(), "{}\n")
        # a blank file never approved still needs a review; a missing one is not created
        touch(self.proj, ".hole/settings.json", "")
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertEqual(self.kinds(r), ["settings-changed"])
        os.remove(path)
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertEqual(r["repaired"], [])
        self.assertFalse(os.path.exists(path))

    def test_changed_project_settings_block(self):
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        approved = {p: {"sha": s} for p, s in r["settings"].items()}
        touch(self.proj, ".hole/settings.json", json.dumps({"files": {"exclude": []}}))
        r2 = guard.preflight(self.proj, self.base, None, self.policy, approved, [], "/home")
        self.assertEqual(self.kinds(r2), ["settings-changed"])

    def test_library_without_own_excludes_uses_global_excludes(self):
        base = dict(self.base, libraries={self.lib: {"path": self.lib}})
        r = guard.preflight(self.proj, base, None, self.policy, {}, [], "/home")
        unc = [b for b in r["blocks"] if b["kind"] == "uncovered"]
        self.assertEqual(unc, [])
        self.assertIn("config/secrets", next(c for c in r["coverage"] if c["root"] == self.lib)["overmounts"])
        touch(self.lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["config/secrets"]}}))
        r2 = guard.preflight(self.proj, base, None, self.policy, {}, [], "/home")
        self.assertNotIn("uncovered", self.kinds(r2))

    def test_legacy_library_requires_its_own_excludes(self):
        base = dict(self.base, libraries={self.lib: {"path": self.lib}})
        r = guard.preflight(self.proj, base, None, self.policy, {}, [], "/home",
                            global_library_excludes=False)
        self.assertIn("uncovered", self.kinds(r))
        self.assertEqual(next(c for c in r["coverage"] if c["root"] == self.lib)["overmounts"], [])
        touch(self.lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["config/secrets"]}}))
        r = guard.preflight(self.proj, base, None, self.policy, {}, [], "/home",
                            global_library_excludes=False)
        self.assertNotIn("uncovered", self.kinds(r))

    def test_forbidden_mount(self):
        secret_home = os.path.join(self.lib, "piper")
        os.makedirs(secret_home)
        base = dict(self.base, libraries={self.lib: {"path": self.lib}})
        touch(self.lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["config/secrets"]}}))
        r = guard.preflight(self.proj, base, None, self.policy, {}, [(secret_home, "any")], "/home")
        self.assertIn("protected-mounted", self.kinds(r))
        r2 = guard.preflight(self.proj, base, None, self.policy, {}, [(self.lib, "rw")], "/home")
        self.assertNotIn("protected-mounted", self.kinds(r2))   # library is ro

    def test_forbidden_include_says_which_mount_once(self):
        # an include of Piper's own checkout (rw forbidden) is in roots AND includes: one block, with its origin
        base = dict(self.base, files={"include": {self.lib: self.lib}})
        r = guard.preflight(self.proj, base, None, self.policy, {}, [(self.lib, "rw")], "/home")
        pm = [b for b in r["blocks"] if b["kind"] == "protected-mounted"]
        self.assertEqual([(b["path"], b["source"], b["via"], b["rw"], b["mode"]) for b in pm],
                         [(self.lib, self.lib, "include", True, "rw")])

    def test_forbidden_project_says_project(self):
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [(self.proj, "rw")], "/home")
        pm = [b for b in r["blocks"] if b["kind"] == "protected-mounted"]
        self.assertEqual([(b["source"], b["via"]) for b in pm], [(self.proj, "project")])

    def test_library_via_extends_is_scanned(self):
        base = dict(self.base, profiles={"acme": {"libraries": {self.lib: {"path": self.lib}}}})
        touch(self.proj, ".hole/settings.json", json.dumps({"profiles": {"dev": {"extends": "acme"}}}))
        r = guard.preflight(self.proj, base, "dev", self.policy, {}, [], "/home")
        self.assertNotIn("uncovered", self.kinds(r))
        self.assertIn(self.lib, [c["root"] for c in r["coverage"]])

    def test_directory_include_is_scanned(self):
        base = dict(self.base, files={"exclude": [".git", "config/secrets"], "include": {self.lib: "~/x"}})
        r = guard.preflight(self.proj, base, None, self.policy, {}, [], "/home")
        self.assertIn(os.path.join(self.lib, "config/secrets"), [b["path"] for b in r["blocks"] if b["kind"] == "uncovered"])

    def test_orchestrator_port_in_gateway_blocks(self):
        base = dict(self.base, network={"hostGatewayDomains": ["host.internal:17890,17895"]})
        pol = dict(self.policy, forbidden_gateway_ports=["17895"])
        r = guard.preflight(self.proj, base, None, pol, {}, [], "/home")
        self.assertIn("forbidden-port", self.kinds(r))

    def test_settings_change_carries_a_diff(self):
        touch(self.proj, ".hole/settings.json", '{"a": 1}\n')
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        path = os.path.join(self.proj, ".hole", "settings.json")
        approved = {path: {"sha": r["settings"][path], "text": r["texts"][path]}}
        touch(self.proj, ".hole/settings.json", '{"a": 1, "files": {"include": {"~/.ssh": "~/x"}}}\n')
        r2 = guard.preflight(self.proj, self.base, None, self.policy, approved, [], "/home")
        diff = [b for b in r2["blocks"] if b["kind"] == "settings-changed" and b["path"] == path][0]["diff"]
        self.assertIn('+{"a": 1, "files": {"include": {"~/.ssh": "~/x"}}}', diff)

    def test_forbidden_project_key(self):
        touch(self.proj, ".hole/settings.json", json.dumps({"container": {"baseImage": "x"}}))
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertIn("forbidden-key", self.kinds(r))
        touch(self.proj, ".hole/settings.json", json.dumps({"git": {"worktreeLinks": "rw"}}))
        r = guard.preflight(self.proj, self.base, None, dict(self.policy, git_mode="guarded"), {}, [], "/home")
        self.assertIn("forbidden-key", self.kinds(r))


    def test_legacy_hole1_keys(self):
        touch(self.proj, ".hole/settings.json", json.dumps({
            "network": {"domainWhitelist": ["service.example.com"]},
            "profiles": {"x": {"network": {"allowedPorts": [443]}}}}))
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertFalse(r["ok"])
        self.assertEqual(sorted(b["detail"] for b in r["blocks"] if b["kind"] == "settings-legacy"),
                         ["network.domainWhitelist", "profiles.x.network.allowedPorts"])
        touch(self.proj, ".hole/settings.json", json.dumps({"network": {"allow": ["service.example.com"]}}))
        r = guard.preflight(self.proj, self.base, None, self.policy, {}, [], "/home")
        self.assertNotIn("settings-legacy", self.kinds(r))

    def test_legacy_key_in_library(self):
        lib = tempfile.mkdtemp()
        touch(lib, ".hole/settings.json", json.dumps({"network": {"allowedPorts": [443]}}))
        r = guard.preflight(self.proj, dict(self.base, libraries={lib: {"path": "~/libs/lib"}}),
                            None, self.policy, {}, [], "/home", container_home="/ch")
        self.assertIn({"kind": "settings-legacy", "path": os.path.join(lib, ".hole", "settings.json"),
                       "detail": "network.allowedPorts"}, r["blocks"])


def git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class WorktreeLayoutTest(unittest.TestCase):
    """git.worktreeLinks / worktreePool as hole 2.1.0 derives them (worktree.go:Derive)."""

    def setUp(self):
        self.top = os.path.realpath(tempfile.mkdtemp())
        self.main = os.path.join(self.top, "app")
        touch(self.main, "README", "x")
        touch(self.main, ".hole/settings.json", json.dumps({"files": {"exclude": ["config/secrets"]}}))
        git(self.top, "init", "-q", "-b", "main", self.main)
        git(self.main, "add", "-A")
        git(self.main, "commit", "-qm", "init")
        self.side = os.path.join(self.top, "side")
        git(self.main, "worktree", "add", "-q", "-b", "side", self.side)
        self.pool = self.main + "-worktrees"
        self.pooled = os.path.join(self.pool, "feat")
        git(self.main, "worktree", "add", "-q", "-b", "feat", self.pooled)
        self.policy = guard.load_policy("/nonexistent")
        self.base = {"files": {"exclude": ["config/secrets"]}}

    def layout(self, git_cfg, project=None, known=None):
        return guard.worktree_layout({"git": git_cfg}, project or self.main, known)

    def test_off_or_unset_derives_nothing(self):
        for cfg in ({}, {"worktreeLinks": "off", "worktreePool": True}):
            self.assertEqual(self.layout(cfg), {"links": [], "pool": None, "pool_worktrees": [], "blocks": []})

    def test_main_repository_links_its_worktrees_and_the_pool(self):
        out = self.layout({"worktreeLinks": "rw", "worktreePool": True})
        self.assertEqual(out["links"], [(self.side, True)])
        self.assertEqual((out["pool"], out["pool_worktrees"], out["blocks"]), (self.pool, [self.pooled], []))
        out = self.layout({"worktreeLinks": "ro"})   # no pool: the pooled checkout is a plain link
        self.assertEqual(sorted(out["links"]), sorted([(self.side, False), (self.pooled, False)]))

    def test_linked_worktree_links_only_the_recorded_main_repository(self):
        common = os.path.join(self.main, ".git")
        self.assertEqual(self.layout({"worktreeLinks": "rw", "worktreePool": True}, self.side, common)["links"],
                         [(self.main, True)])                                    # never a pool from a linked worktree
        out = self.layout({"worktreeLinks": "rw"}, self.side, None)               # unrecorded: refused
        self.assertEqual((out["links"], [b["kind"] for b in out["blocks"]]), ([], ["worktree-link-unverified"]))
        out = self.layout({"worktreeLinks": "rw"}, self.side, os.path.join(self.top, "other", ".git"))
        self.assertEqual([b["kind"] for b in out["blocks"]], ["worktree-link-unverified"])

    def test_planted_admin_entry_is_not_mounted(self):
        # a sandbox writes .git (git is open): an admin entry naming a directory outside every mount
        outside = os.path.realpath(tempfile.mkdtemp())
        touch(self.main, ".git/worktrees/evil/gitdir", outside + "/.git\n")
        touch(self.main, ".git/worktrees/evil/HEAD", "ref: refs/heads/main\n")
        touch(self.main, ".git/worktrees/evil/commondir", "../..\n")
        out = self.layout({"worktreeLinks": "rw"})
        self.assertNotIn(outside, [p for p, _ in out["links"]])
        self.assertIn({"kind": "worktree-link-unverified", "path": outside,
                       "detail": "git lists it as a worktree of %s, but its .git does not point back" % self.main},
                      out["blocks"])

    def test_preflight_scans_links_and_the_pool_with_each_checkouts_excludes(self):
        touch(self.side, "config/secrets/a.properties")
        touch(self.pooled, "config/secrets/b.properties")
        touch(self.pool, "loose/config/secrets/c.properties")   # not a checkout git lists: nothing hides it
        touch(self.main, ".hole/settings.json", json.dumps({"git": {"worktreeLinks": "rw", "worktreePool": True},
                                                            "files": {"exclude": ["config/secrets"]}}))
        r = guard.preflight(self.main, self.base, None, self.policy, {}, [], "/home")
        cov = {c["root"]: c for c in r["coverage"]}
        self.assertEqual(cov[self.side]["uncovered"], [])                         # its own .hole/settings.json
        self.assertEqual(cov[self.pool]["uncovered"], ["loose/config/secrets"])
        self.assertIn("feat/config/secrets", cov[self.pool]["overmounts"])
        self.assertIn((self.pool, True), r["libraries"])
        self.assertTrue({self.side, self.pool} <= set(r["mount_roots"]))
        self.assertEqual([b["path"] for b in r["blocks"] if b["kind"] == "uncovered"],
                         [os.path.join(self.pool, "loose/config/secrets")])


class SameRepositoryApprovalTest(unittest.TestCase):
    """One review per repository: settings bytes approved in one checkout are approved in the other
    verified checkouts of the same repository, never across repositories."""

    SETTINGS = json.dumps({"git": {"worktreeLinks": "rw"}, "network": {"allow": ["example.org"]}})

    def setUp(self):
        self.top = os.path.realpath(tempfile.mkdtemp())
        self.main = os.path.join(self.top, "app")
        touch(self.main, ".hole/settings.json", self.SETTINGS)
        git(self.top, "init", "-q", "-b", "main", self.main)
        git(self.main, "add", "-A")
        git(self.main, "commit", "-qm", "init")
        self.side = os.path.join(self.top, "app-917-fix")
        git(self.main, "worktree", "add", "-q", "-b", "fix", self.side)
        self.policy = guard.load_policy("/nonexistent")
        self.base = {"files": {"exclude": []}}
        self.main_file = os.path.join(self.main, ".hole", "settings.json")
        self.side_file = os.path.join(self.side, ".hole", "settings.json")

    def approved_main(self):
        r = guard.preflight(self.main, self.base, None, self.policy, {}, [], "/home")
        return {self.main_file: {"sha": r["settings"][self.main_file], "text": r["texts"][self.main_file]}}

    def changed(self, r):
        return {b["path"]: b for b in r["blocks"] if b["kind"] == "settings-changed"}

    def test_first_review_is_marked_never_approved(self):
        r = guard.preflight(self.main, self.base, None, self.policy, {}, [], "/home")
        b = self.changed(r)[self.main_file]
        self.assertIsNone(b["previous"])
        self.assertTrue(b["diff"].startswith("--- (never approved)\n+++ now\n"), b["diff"])
        approved = {self.main_file: {"sha": "old", "text": "{}\n"}}
        b = self.changed(guard.preflight(self.main, self.base, None, self.policy, approved, [], "/home"))[self.main_file]
        self.assertTrue(b["diff"].startswith("--- approved\n"), b["diff"])

    def test_linked_worktree_with_identical_bytes_inherits_the_main_approval(self):
        r = guard.preflight(self.main, self.base, None, self.policy, self.approved_main(), [], "/home")
        self.assertEqual(self.changed(r), {}, r["blocks"])
        self.assertEqual(r["inherited"], {self.side_file: self.main_file})

    def test_a_worktree_change_is_still_reviewed(self):
        touch(self.side, ".hole/settings.json", json.dumps({"git": {"worktreeLinks": "rw"},
                                                            "network": {"allow": ["evil.example"]}}))
        r = guard.preflight(self.main, self.base, None, self.policy, self.approved_main(), [], "/home")
        self.assertEqual(list(self.changed(r)), [self.side_file])
        self.assertEqual(r["inherited"], {})

    def test_worktree_as_the_project_inherits_from_the_recorded_main_only(self):
        approved = self.approved_main()
        common = os.path.join(self.main, ".git")
        r = guard.preflight(self.side, self.base, None, self.policy, approved, [], "/home", known_common=common)
        self.assertEqual(self.changed(r), {}, r["blocks"])
        self.assertEqual(r["inherited"], {self.side_file: self.main_file})
        # unrecorded main repository (known_common None): the sandbox-written .git file proves nothing
        r = guard.preflight(self.side, self.base, None, self.policy, approved, [], "/home")
        self.assertIn(self.side_file, self.changed(r))
        self.assertNotIn(self.side_file, r["inherited"])

    def test_identical_bytes_of_another_repository_are_not_inherited(self):
        other = os.path.join(self.top, "other")
        touch(other, ".hole/settings.json", self.SETTINGS)
        sha = hashlib.sha256(self.SETTINGS.encode()).hexdigest()
        approved = {os.path.join(other, ".hole", "settings.json"): {"sha": sha, "text": self.SETTINGS}}
        r = guard.preflight(self.main, self.base, None, self.policy, approved, [], "/home")
        self.assertEqual(sorted(self.changed(r)), sorted([self.main_file, self.side_file]))
        self.assertEqual(r["inherited"], {})

    def test_review_off_inherits_nothing_it_does_not_need(self):
        pol = dict(self.policy, settings_review=False)
        r = guard.preflight(self.main, self.base, None, pol, {}, [], "/home")
        self.assertEqual((self.changed(r), r["inherited"]), ({}, {}))


class LibraryWorktreeGlobalExcludesTest(unittest.TestCase):
    """A linked worktree mounted as a library hides the global files.exclude since hole 2.2 - a
    worktree without its own excludes must not be reported (hole 2.2/2.3)."""

    def setUp(self):
        self.top = os.path.realpath(tempfile.mkdtemp())
        self.main = os.path.join(self.top, "app")
        touch(self.main, ".hole/settings.json", json.dumps({"git": {"worktreeLinks": "ro"}}))
        git(self.top, "init", "-q", "-b", "main", self.main)
        git(self.main, "add", "-A")
        git(self.main, "commit", "-qm", "init")
        self.side = os.path.join(self.top, "app-wt")
        git(self.main, "worktree", "add", "-q", "-b", "wt", self.side)
        touch(self.main, "config/secrets/a.properties")
        touch(self.side, "config/secrets/a.properties")
        self.policy = guard.load_policy("/nonexistent")
        self.base = {"files": {"exclude": ["config/secrets"]}}

    def uncovered(self, **kw):
        r = guard.preflight(self.main, self.base, None, self.policy, {}, [], "/home", **kw)
        return [b["path"] for b in r["blocks"] if b["kind"] == "uncovered"]

    def test_global_excludes_cover_the_worktree_from_hole_2_2(self):
        self.assertEqual(self.uncovered(), [])
        self.assertEqual(self.uncovered(global_library_excludes=False),          # hole 2.1: its own file only
                         [os.path.join(self.side, "config/secrets")])


class WorktreeMirrorTest(unittest.TestCase):
    """Excludes repeated in .claude/worktrees/<name> of the project and of every library (hole resolves
    an exclude against its mount root only)."""

    def test_project_and_library_worktrees(self):
        proj, lib = os.path.realpath(tempfile.mkdtemp()), os.path.realpath(tempfile.mkdtemp())
        touch(proj, ".claude/worktrees/a/config/secrets/x")
        touch(proj, ".claude/worktrees/a/local.txt")
        touch(lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["keys/prod"]}}))
        touch(lib, ".claude/worktrees/1043/keys/prod/k.pem")
        touch(lib, ".claude/worktrees/1043/config/secrets/y")
        eff = {"files": {"exclude": ["config/secrets", "local.txt", "missing.txt"]},
               "libraries": {lib: {"path": lib}}}
        m = guard.worktree_mirror(eff, proj, "/home")
        self.assertEqual(m, [".claude/worktrees/*/config/secrets", ".claude/worktrees/*/local.txt",
                             ".claude/worktrees/*/keys/prod"])   # missing.txt matches nowhere: no hole warning
        base = {"files": {"exclude": eff["files"]["exclude"] + m}}
        pol = dict(guard.load_policy("/nonexistent", tree=False), candidates=["**/config/secrets", "**/*.pem"])
        r = guard.preflight(proj, dict(base, libraries=eff["libraries"]), None, pol, {}, [], "/home")
        self.assertEqual([b["path"] for b in r["blocks"] if b["kind"] == "uncovered"], [])
        cov = {c["root"]: c for c in r["coverage"]}
        self.assertIn(".claude/worktrees/1043/keys/prod", cov[lib]["overmounts"])
        self.assertIn(".claude/worktrees/a/config/secrets", cov[proj]["overmounts"])
        self.assertEqual(guard.worktree_mirror({"files": {"exclude": ["config/secrets"]}},
                                               os.path.realpath(tempfile.mkdtemp()), "/home"), [])

    def test_empty_pools_are_pinned_in_project_and_libraries_before_later_files_appear(self):
        proj, lib = os.path.realpath(tempfile.mkdtemp()), os.path.realpath(tempfile.mkdtemp())
        eff = {"libraries": {lib: {"path": "/library"}}}
        touch(lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["private"]}}))
        self.assertEqual(guard.shield_worktree_pools(eff, proj, "/home"), [guard.WORKTREES_IN])
        self.assertFalse(os.path.exists(os.path.join(proj, guard.WORKTREES_IN)))
        mask = guard.shield_worktree_pools(eff, proj, "/home", prepare=True)
        snapshots = [guard.coverage(root, mask, dict(guard.DEFAULT_POLICY, candidates=[])) for root in (proj, lib)]
        for root, snapshot in zip((proj, lib), snapshots):
            touch(root, ".claude/worktrees/late/private/secret", "fixture")
            self.assertTrue(guard.covered(".claude/worktrees/late/private/secret", guard.intact_overmounts(snapshot)))

    def test_project_pool_is_also_masked_through_an_enclosing_library(self):
        for name in ("app", "app[1]*?"):
            with self.subTest(name=name):
                library = os.path.realpath(tempfile.mkdtemp())
                project = os.path.join(library, "projects", name)
                os.makedirs(project)
                eff = {"libraries": {library: {"path": "/library"}}}
                mask = guard.shield_worktree_pools(eff, project, "/home", prepare=True)
                cov = guard.coverage(library, mask, dict(guard.DEFAULT_POLICY, candidates=[]))
                rel = ".claude/worktrees/late/config/properties/fixture"
                touch(project, rel, "hidden")
                self.assertTrue(guard.covered("projects/" + name + "/" + rel, guard.intact_overmounts(cov)))

    @unittest.skipIf(os.geteuid() == 0, "root bypasses the fixture's directory write permissions")
    def test_readonly_library_does_not_prevent_preparing_the_project_pool(self):
        project, library = os.path.realpath(tempfile.mkdtemp()), os.path.realpath(tempfile.mkdtemp())
        os.chmod(library, 0o555)
        try:
            guard.shield_worktree_pools({"libraries": {library: {"path": "/sdk"}}}, project, "/home", prepare=True)
            self.assertTrue(os.path.isdir(os.path.join(project, guard.WORKTREES_IN)))
            self.assertFalse(os.path.exists(os.path.join(library, ".claude")))
        finally:
            os.chmod(library, 0o755)

    def test_pool_preparation_refuses_symlink_parents_without_writing_through_them(self):
        for component in (".claude", ".claude/worktrees"):
            with self.subTest(component=component):
                proj, outside = os.path.realpath(tempfile.mkdtemp()), os.path.realpath(tempfile.mkdtemp())
                os.makedirs(os.path.dirname(os.path.join(proj, component)), exist_ok=True)
                os.symlink(outside, os.path.join(proj, component))
                with self.assertRaises(OSError):
                    guard.shield_worktree_pools({"files": {"exclude": ["private"]}}, proj, "/home", prepare=True)
                self.assertEqual(os.listdir(outside), [])



class LibraryDestinationTest(unittest.TestCase):
    def test_global_profile_excludes_reach_library_but_project_excludes_do_not(self):
        proj, lib = tempfile.mkdtemp(), tempfile.mkdtemp()
        touch(lib, "config/secrets/secret", "hidden")
        touch(lib, "global.txt", "hidden")
        touch(lib, "own.txt", "hidden")
        touch(lib, "project-only.txt", "visible")
        touch(lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["own.txt"]}}))
        touch(proj, ".hole/settings.json", json.dumps({
            "files": {"exclude": ["project-only.txt"]}, "profiles": {"local": {"extends": "global"}}}))
        base = {"libraries": {lib: "/lib"}, "files": {"exclude": ["global.txt"]},
                "profiles": {"global": {"files": {"exclude": ["config/secrets"]}}}}
        r = guard.preflight(proj, base, "local", guard.load_policy("/absent"), {}, [], "/home")
        cov = next(c for c in r["coverage"] if c["root"] == lib)
        self.assertEqual(cov["uncovered"], [])
        self.assertEqual(cov["overmounts"], ["config/secrets", "global.txt", "own.txt"])

    def test_dest_and_skip_roots(self):
        proj, lib, m2 = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
        touch(lib, ".hole/settings.json", json.dumps({"files": {"exclude": ["config/secrets"]}}))
        touch(lib, "config/secrets/a", "x")
        touch(m2, "repository/x.jar", "")
        base = {"files": {"exclude": [".git"], "include": {m2: "~/.m2"}},
                "libraries": {lib: {"path": "~/libs/lib"}}}
        pol = dict(guard.load_policy("/nonexistent"), scan_skip_roots=[m2])
        r = guard.preflight(proj, base, None, pol, {}, [], "/home", container_home="/ch")
        by = {c["root"]: c for c in r["coverage"]}
        self.assertEqual(by[lib.rstrip("/")]["dest"], "/ch/libs/lib")
        self.assertEqual(by[lib.rstrip("/")]["kind"], "library")
        self.assertNotIn(m2.rstrip("/"), by)          # the maven cache is not scanned
        self.assertIn(m2.rstrip("/"), r["mount_roots"])  # but it is an expected mount source


class ContainerPathTest(unittest.TestCase):
    def test_dest_mirrors_resolve_container_path(self):
        self.assertEqual(guard._dest("libs/dep", "/ch", "/p"), "/p/libs/dep")
        self.assertEqual(guard._dest("", "/ch", "/p"), "/p")
        self.assertEqual(guard._dest("$HOME/www/webstore", "/ch", "/p"), "/ch/www/webstore")   # HOME of the hole process
        self.assertEqual(guard._dest("~/x", "/ch", "/p"), "/ch/x")
        self.assertEqual(guard._dest("/abs/x/", "/ch", "/p"), "/abs/x")

    def test_dotdot_is_cleaned_as_docker_shows_the_mount(self):
        # a library written as /www/projects/../x: docker lists it at /www/x, L4 must find it there
        self.assertEqual(guard._dest("/www/projects/../x", "/ch", "/p"), "/www/x")
        self.assertEqual(guard._dest("../x", "/ch", "/w/p"), "/w/x")
        self.assertEqual(guard._host("/www/projects/../x/", "/h"), "/www/x")
        self.assertEqual(guard._host("~/a/../x", "/h"), "/h/x")
        roots = guard.library_roots({"libraries": {"/www/projects/../x": "/www/projects/../x"}}, "/h", "/ch", "/p")
        self.assertEqual(roots, [("/www/x", False, "/www/x")])
        cov = [{"root": roots[0][0], "dest": roots[0][2], "kind": "library", "candidates": [], "overmounts": []}]
        m = [{"Source": "/c", "Destination": "/h/.claude", "RW": True}, {"Source": "/www/x", "Destination": "/www/x", "RW": False}]
        self.assertEqual(guard.attest_mounts(m, cov, ("/h/.claude", "/c"), [], ["/c"], "/tmp/x"), [])

    def test_agent_must_have_every_root_where_expected(self):
        cov = [{"root": "/lib", "dest": "/ch/lib", "kind": "library", "candidates": [], "overmounts": []}]
        m = [{"Source": "/c", "Destination": "/h/.claude", "RW": True}, {"Source": "/lib", "Destination": "/lib", "RW": False}]
        fails = guard.attest_mounts(m, cov, ("/h/.claude", "/c"), [], ["/c"], "/tmp/x")
        self.assertEqual(fails, ["library root /lib is not mounted at /ch/lib - its checks cannot be trusted"])
        self.assertEqual(guard.attest_mounts(m, cov, None, [], ["/c"], "/tmp/x"), [])   # DinD: subset is fine


class AttestMountsTest(unittest.TestCase):
    cov = [{"root": "/p", "candidates": [".git", "config/secrets", "a/secrets.yml"]}]
    RUN = "/oh/hole-home/.hole/tmp"

    def mounts(self, *extra):
        base = [{"Source": "/p", "Destination": "/p", "RW": True},
                {"Source": self.RUN + "/run.1/excluded-dirs/p/.git", "Destination": "/p/.git", "RW": True},
                {"Source": self.RUN + "/run.1/excluded-dirs/p/config/secrets",
                 "Destination": "/p/config/secrets", "RW": True},
                {"Source": "/dev/null", "Destination": "/p/a/secrets.yml", "RW": False},
                {"Source": "/oh/sessions/s1/claude", "Destination": "/oh/hole-home/.claude", "RW": True}]
        return base + list(extra)

    def test_all_good(self):
        fails = guard.attest_mounts(self.mounts(), self.cov, ("/oh/hole-home/.claude", "/oh/sessions/s1/claude"),
                                    [("/oh", "any")], ["/oh/sessions/s1/claude"], self.RUN)
        self.assertEqual(fails, [])

    def test_hole24_volume_and_tmpfs_masks_and_wrong_instance(self):
        for kind in ("volume", "tmpfs"):
            with self.subTest(kind=kind):
                mounts = self.mounts()
                for m in mounts[1:3]:
                    m.update(Type=kind, Source="/var/lib/docker/volumes/masks/_data/0",
                             Name="hole-sandbox-masks-hole-sandbox-test-12345678-abc123")
                args = (mounts, self.cov, None, [], [], self.RUN)
                self.assertEqual(guard.attest_mounts(*args, instance="hole-sandbox-test-12345678-abc123"), [])
                if kind == "volume":
                    for inst in (None, "hole-sandbox-other-12345678-abc123"):
                        self.assertEqual(len(guard.attest_mounts(*args, instance=inst)), 2)
                    for m in mounts[1:3]:
                        m["Type"] = "nfs"
                    self.assertEqual(len(guard.attest_mounts(*args, instance="hole-sandbox-test-12345678-abc123")), 2)

    def test_a_volume_cannot_replace_the_session_config_bind(self):
        mounts = self.mounts()
        mounts[-1].update(Type="volume", Name="hole-sandbox-masks-test")
        failures = guard.attest_mounts(mounts, self.cov,
                                      ("/oh/hole-home/.claude", "/oh/sessions/s1/claude"), [], [], self.RUN,
                                      instance="test")
        self.assertEqual(failures, ["the agent's config dir is not mounted at /oh/hole-home/.claude"])

    def test_attest_reads_tmpfs_from_hostconfig_and_still_checks_content(self):
        import subprocess
        from unittest import mock
        info = {"Mounts": [{"Type": "bind", "Source": "/p", "Destination": "/p"},
                           {"Type": "bind", "Source": "/c", "Destination": "/h/.claude"}],
                "HostConfig": {"Tmpfs": {"/p/data": "exec"}}, "Config": {"Env": []}}
        cov = [{"root": "/p", "candidates": [], "overmounts": ["data"]}]
        for output, ok in (("1 0 /p/data\n", True), ("0 1 /p/data\n", False), ("", False)):
            with self.subTest(output=output), mock.patch.object(guard, "_docker", side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps([info]), ""),
                    subprocess.CompletedProcess([], 0, output, ""),
                    subprocess.CompletedProcess([], 1, "", "no such container")]):
                result = guard.attest("docker", "inst", cov, ("/h/.claude", "/c"), [], ["/c"],
                                      "/run-tmp", "/p", {}, ["/p", "/c"], git_hidden=False)
            self.assertEqual(result["ok"], ok, result)

    def test_missing_overmount(self):
        m = [x for x in self.mounts() if x["Destination"] != "/p/.git"]
        fails = guard.attest_mounts(m, self.cov, None, [], [], self.RUN)
        self.assertEqual(fails, ["not over-mounted: /p/.git"])

    def test_overmount_from_wrong_source_is_not_hiding(self):
        m = self.mounts({"Source": "/elsewhere", "Destination": "/p/x"})
        m = [x for x in m if x["Destination"] != "/p/config/secrets"] + [
            {"Source": "/p/config/secrets", "Destination": "/p/config/secrets"}]
        fails = guard.attest_mounts(m, self.cov, None, [], [], self.RUN)
        self.assertIn("not over-mounted: /p/config/secrets", fails)

    def test_wrong_claude_and_forbidden_source(self):
        m = self.mounts({"Source": "/Users/me/.claude", "Destination": "/x/.claude", "RW": True},
                        {"Source": "/oh", "Destination": "/oh", "RW": False})
        m[4] = {"Source": "/Users/me/.claude", "Destination": "/oh/hole-home/.claude", "RW": True}
        fails = guard.attest_mounts(m, self.cov, ("/oh/hole-home/.claude", "/oh/sessions/s1/claude"),
                                    [("/oh", "any"), ("/Users/me/.claude", "any")], ["/oh/sessions/s1/claude"],
                                    self.RUN)
        self.assertTrue(any("comes from /Users/me/.claude" in f for f in fails), fails)
        self.assertTrue(any("forbidden mount source /oh " in f for f in fails), fails)

    def test_rw_only_rule(self):
        m = self.mounts({"Source": "/sk", "Destination": "/sk", "RW": False})
        self.assertEqual(guard.attest_mounts(m, self.cov, None, [("/sk", "rw")], [], self.RUN), [])
        m = self.mounts({"Source": "/sk", "Destination": "/sk", "RW": True})
        self.assertEqual(len(guard.attest_mounts(m, self.cov, None, [("/sk", "rw")], [], self.RUN)), 1)

    def test_unexpected_source_and_hole_run_dir(self):
        m = self.mounts({"Source": self.RUN + "/run.1/prestart-scripts", "Destination": "/tmp/prestart-scripts", "RW": False},
                        {"Source": "/Users/me/.ssh", "Destination": "/x/.ssh", "RW": True})
        fails = guard.attest_mounts(m, self.cov, None, [("/oh", "any")], ["/oh/sessions/s1/claude"], self.RUN,
                                    allowed_roots=["/p", "/oh/sessions/s1/claude"])
        self.assertEqual(fails, ["unexpected mount source /Users/me/.ssh (not the project, a library, an include or the session)"])

    def test_inside_failures(self):
        out = "1 0 /p/.git\n0 12 /p/config/secrets\n1 0 /p/a/secrets.yml\n"
        fails = guard.inside_failures(out, ["/p/.git", "/p/config/secrets", "/p/a/secrets.yml", "/p/missing"])
        self.assertEqual(len(fails), 2)
        self.assertIn("visible inside the container: /p/config/secrets (12 entries/bytes, over-mount MISSING)", fails[0])
        self.assertIn("did not report /p/missing", fails[1])

    def test_inside_check_empty_worktree_config_requires_an_actual_git_overmount(self):
        import subprocess
        with tempfile.TemporaryDirectory() as root:
            gitdir = os.path.join(root, ".git")
            touch(root, ".git/config.worktree")
            mountinfo = os.path.join(root, "mountinfo")
            with open(mountinfo, "w") as f:
                f.write("1 2 3 4 %s 6\n" % gitdir)
            script = guard.INSIDE_SCRIPT.replace("/proc/self/mountinfo", mountinfo)
            def check(path=gitdir):
                r = subprocess.run(["sh", "-c", script], input=path + "\n", capture_output=True, text=True, check=True)
                return guard.inside_failures(r.stdout, [path])
            self.assertEqual(check(), [])
            touch(root, ".git/config.worktree", "[core]\nworktree = /outside\n")
            self.assertTrue(check())
            touch(root, ".git/config.worktree")
            touch(root, ".git/HEAD", "ref: refs/heads/main\n")
            self.assertTrue(check())
            os.unlink(os.path.join(gitdir, "HEAD"))
            os.unlink(os.path.join(gitdir, "config.worktree"))
            touch(root, "empty")
            os.symlink(os.path.join(root, "empty"), os.path.join(gitdir, "config.worktree"))
            self.assertTrue(check())
            os.unlink(os.path.join(gitdir, "config.worktree"))
            os.mkdir(os.path.join(gitdir, "config.worktree"))
            self.assertTrue(check())
            os.rmdir(os.path.join(gitdir, "config.worktree"))
            touch(root, ".git/config.worktree")
            with open(mountinfo, "w") as f:
                f.write("")
            self.assertTrue(check())
            touch(root, "data/config.worktree")
            data = os.path.join(root, "data")
            with open(mountinfo, "w") as f:
                f.write("1 2 3 4 %s 6\n" % data)
            self.assertTrue(check(data))

    def test_overmount_that_is_no_candidate_is_checked_too(self):
        """excludes like `data` (DB dumps) are not policy candidates, but K18 drops them too."""
        cov = [{"root": "/p", "candidates": [], "overmounts": ["data"]}]
        m = [{"Source": "/p", "Destination": "/p", "RW": True}]
        self.assertEqual(guard.attest_mounts(m, cov, None, [], [], "/oh/tmp"), ["not over-mounted: /p/data"])

    def test_attest_runs_the_inside_check_for_overmounts(self):
        import shutil
        d = tempfile.mkdtemp()
        proj = os.path.join(d, "p")
        touch(proj, "data/dump.sql", "SECRET")
        fake = os.path.join(d, "fake")
        os.makedirs(fake)
        run = os.path.join(d, "tmp")
        with open(os.path.join(fake, "inst-agent-1.json"), "w") as f:
            json.dump([{"Mounts": [{"Source": proj, "Destination": proj, "RW": True},
                                   {"Source": run + "/run.1/excluded-dirs" + proj + "/data", "Destination": proj + "/data", "RW": True},
                                   {"Source": "/c", "Destination": "/h/.claude", "RW": True}],
                        "Config": {"Env": []}}], f)
        os.environ["FAKE_DOCKER_DIR"] = fake
        cov = [{"root": proj, "candidates": [], "overmounts": ["data"], "dest": proj}]
        fake_docker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-docker.py")
        res = guard.attest(fake_docker, "inst", cov, ("/h/.claude", "/c"), [], ["/c"], run, proj,
                           guard.load_policy("/nonexistent"), [proj, "/c"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("visible inside the container: %s/data" % proj in f for f in res["failures"]), res)
        shutil.rmtree(d, True)

    def test_attest_reports_a_stopped_agent(self):
        import shutil
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "inst-agent-1.json"), "w") as f:
            json.dump([{"Mounts": [{"Source": "/c", "Destination": "/h/.claude", "RW": True}],
                        "Config": {"Env": []}, "State": {"Running": False, "ExitCode": 1}}], f)
        os.environ["FAKE_DOCKER_DIR"] = d
        fake_docker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-docker.py")
        res = guard.attest(fake_docker, "inst", [], ("/h/.claude", "/c"), [], ["/c"], d, "/nonexistent-proj",
                           guard.load_policy("/nonexistent"), ["/c"])
        self.assertIn("agent: %s (exit code 1)" % guard.AGENT_NOT_RUNNING, res["failures"])
        shutil.rmtree(d, True)

    def test_attest_names_where_git_found_the_repository(self):
        """a masked .git (worktree: /dev/null) can still leave a repository in reach - say which."""
        import shutil
        import subprocess
        d = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", d], check=True)
        proj = os.path.join(d, "sub")
        touch(proj, "f", "x")
        with open(os.path.join(d, "inst-agent-1.json"), "w") as f:
            json.dump([{"Mounts": [{"Source": "/c", "Destination": "/h/.claude", "RW": True}],
                        "Config": {"Env": []}}], f)
        os.environ["FAKE_DOCKER_DIR"] = d
        fake_docker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-docker.py")
        res = guard.attest(fake_docker, "inst", [], ("/h/.claude", "/c"), [], ["/c"], d, proj,
                           guard.load_policy("/nonexistent"), ["/c"])
        want = "agent: the project is a git repository inside the sandbox (.git visible); git dir: %s" \
            % os.path.realpath(os.path.join(d, ".git"))
        self.assertTrue(any(f.startswith(want) for f in res["failures"]), res)
        # git open (2026-10-01): a repository in the sandbox is the intent, no failure
        res = guard.attest(fake_docker, "inst", [], ("/h/.claude", "/c"), [], ["/c"], d, proj,
                           guard.load_policy("/nonexistent"), ["/c"], git_hidden=False)
        self.assertTrue(res["ok"], res)
        shutil.rmtree(d, True)

    def test_inside_script_against_a_real_tree(self):
        import shutil
        import subprocess
        d = tempfile.mkdtemp()
        touch(d, "full/f", "x")
        touch(d, "dotonly/.hidden", "x")
        os.makedirs(os.path.join(d, "empty"))
        touch(d, "emptyfile", "")
        touch(d, "we ird\\name", "x")
        paths = [os.path.join(d, x) for x in ("full", "empty", "nothing", "full/f", "dotonly", "emptyfile", "we ird\\name")]
        r = subprocess.run(["sh", "-c", guard.INSIDE_SCRIPT], input="\n".join(paths) + "\n", capture_output=True, text=True)
        fails = guard.inside_failures(r.stdout, paths)
        self.assertEqual(sorted(f.split(": ", 1)[1].split(" (")[0] for f in fails),
                         sorted([os.path.join(d, "full"), os.path.join(d, "full/f"), os.path.join(d, "dotonly"),
                                 os.path.join(d, "we ird\\name")]))
        for shell in ("dash", "bash"):
            if shutil.which(shell):
                r = subprocess.run([shell, "-c", guard.INSIDE_SCRIPT], input="\n".join(paths) + "\n",
                                   capture_output=True, text=True)
                self.assertEqual(len(guard.inside_failures(r.stdout, paths)), 4, shell)

    def test_env(self):
        pol = guard.load_policy("/nonexistent")
        self.assertEqual(guard.env_failures(["GITLAB_TOKEN=x", "A=1", "CLAUDE_CODE_OAUTH_TOKEN=y",
                                             "GITLAB_PAT=z", "DB_PASSWORD=q"], pol),
                         ["GITLAB_TOKEN", "GITLAB_PAT", "DB_PASSWORD"])




@unittest.skipUnless(os.environ.get("TEST_SANDBOX") == "dind", "requires the isolated DinD sandbox")
class FutureWorktreeMountTest(unittest.TestCase):
    def test_readonly_parent_mask_hides_future_worktree(self):
        self._check_parent_mask(readonly=True)

    def test_writable_parent_placeholder_is_not_the_host_worktree_pool(self):
        self._check_parent_mask(readonly=False)

    def _check_parent_mask(self, readonly):
        import secrets
        prefix = "piper-worktree-mask-" + secrets.token_hex(4)
        source, empty, parent, own = (prefix + s for s in ("-source", "-empty", "-parent", "-own"))

        def docker(*args):
            return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.STDOUT).strip()

        try:
            docker("volume", "create", source)
            docker("volume", "create", empty)
            docker("run", "--rm", "-v", source + ":/repo", "busybox:latest", "mkdir", "-p", "/repo/.claude/worktrees")
            docker("run", "-d", "--name", parent, "-v", source + ":/repo", "-v",
                   empty + ":/repo/.claude/worktrees" + (":ro" if readonly else ""), "busybox:latest", "sleep", "120")
            # Materialize the complete checkout only after the parent's sandbox exists.
            docker("run", "--rm", "-v", source + ":/repo", "busybox:latest", "sh", "-c",
                   "mkdir -p /repo/.claude/worktrees/late/config/properties; "
                   "echo fixture-secret > /repo/.claude/worktrees/late/config/properties/settings; "
                   "echo fixture-source > /repo/.claude/worktrees/late/README")
            docker("exec", parent, "test", "!", "-e", "/repo/.claude/worktrees/late")
            if not readonly:
                docker("exec", parent, "mkdir", "/repo/.claude/worktrees/container-only")
                docker("run", "--rm", "-v", source + ":/repo", "busybox:latest", "test", "!", "-e",
                       "/repo/.claude/worktrees/container-only")
            root = docker("volume", "inspect", "--format", "{{.Mountpoint}}", source)
            docker("run", "-d", "--name", own, "-v", root + "/.claude/worktrees/late:/repo", "-v",
                   empty + ":/repo/config/properties:ro", "busybox:latest", "sleep", "120")
            self.assertEqual(docker("exec", own, "cat", "/repo/README"), "fixture-source")
            docker("exec", own, "test", "!", "-e", "/repo/config/properties/settings")
            docker("exec", own, "sh", "-c", "echo edited > /repo/README")
            self.assertEqual(docker("exec", own, "cat", "/repo/README"), "edited")
            docker("exec", parent, "test", "!", "-e", "/repo/.claude/worktrees/late")
        finally:
            for container in (own, parent):
                subprocess.run(["docker", "rm", "-f", container], capture_output=True)
            for volume in (empty, source):
                subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True)


if __name__ == "__main__":
    unittest.main()
