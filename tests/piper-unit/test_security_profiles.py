"""Migrate exact legacy profiles, preserve custom choices and exercise the approval boundary."""
import copy
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import _env
import common
import dbtools
import gitops
import guard
import security
import sessions
import settings


def selected(name):
    return security.apply(copy.deepcopy(guard.DEFAULT_POLICY) | {
        "security": security.presets()[name], "security_rev": security.REV, "security_profile": name})


class ProfilePolicyTest(unittest.TestCase):
    def test_presets_and_new_default(self):
        p = security.presets()
        self.assertEqual([len(p[k]) for k in ("hole", "recommended", "guarded")], [17, 38, 65])   # + ask.stack_mode (2026-10-27)
        self.assertLess(set(p["hole"]), set(p["recommended"]))
        self.assertLess(set(p["recommended"]), set(p["guarded"]))
        self.assertEqual(security.default_value(), p["recommended"])
        self.assertEqual(security.profile({}), "recommended")
        for name in ("hole", "recommended", "guarded"):
            self.assertEqual(security.profile({"security": p[name], "security_rev": 2}), "custom")
            self.assertFalse(security.apply({"security": p[name], "security_rev": 2})["security_strict"])

    def test_migration_preserves_custom_choices_and_is_idempotent(self):
        choices = [{}, {"ask.gitlab_comment": True},
                   {i: True for i in security.V2_MINIMAL_ON | {"ask.http_dev"}},
                   {i: True for i in security.V2_RECOMMENDED_ON - {"strip_secrets"}}]
        for choice in choices:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "policy.json")
                original = {"security": choice, "security_rev": 2, "future": {"keep": 1}}
                common.write_json(path, original)
                before = security.state(original)
                with mock.patch.object(common, "POLICY_FILE", path):
                    self.assertTrue(settings.migrate_security_approvals())
                    after = common.read_json(path, {})
                    self.assertEqual(security.state(after), before)
                    self.assertEqual(security.profile(after), "custom")
                    self.assertEqual(after["future"], original["future"])
                    raw = Path(path).read_text()
                    self.assertFalse(settings.migrate_security_approvals())
                    self.assertEqual(Path(path).read_text(), raw)

    def test_a_new_card_follows_its_sibling_once(self):
        """ask.stack_mode came after choices were stored - on where ask.settings_change is"""
        self.assertIn("ask.stack_mode", security.MINIMAL_ON)
        self.assertIn("ask.stack_mode", security.RECOMMENDED_ON)
        p = security.presets()
        for choice, want in ((dict((k, v) for k, v in p["recommended"].items() if k != "ask.stack_mode"), True),
                             (dict((k, v) for k, v in p["hole"].items() if k != "ask.stack_mode"), True),
                             ({"ask.gitlab_comment": True}, False)):
            with self.subTest(choice=sorted(choice)[:3]), tempfile.TemporaryDirectory() as root, \
                    mock.patch.object(common, "POLICY_FILE", os.path.join(root, "policy.json")):
                common.write_json(common.POLICY_FILE, {"security": choice, "security_rev": security.REV,
                                                       "security_profiles_migration": security.PROFILES_MIGRATION})
                self.assertTrue(settings.migrate_new_cards())
                saved = common.read_json(common.POLICY_FILE, {})
                self.assertEqual(saved["security"].get("ask.stack_mode", False), want)
                self.assertEqual(saved["security_new_cards"], ["ask.stack_mode"])
                saved["security"].pop("ask.stack_mode", None)   # switched off by the user later: stays off
                common.write_json(common.POLICY_FILE, saved)
                self.assertFalse(settings.migrate_new_cards())
                self.assertNotIn("ask.stack_mode", common.read_json(common.POLICY_FILE, {})["security"])
        with tempfile.TemporaryDirectory() as root, mock.patch.object(common, "POLICY_FILE", os.path.join(root, "policy.json")):
            self.assertFalse(settings.migrate_new_cards(), "no policy yet: the defaults have it")

    def test_existing_implicit_minimal_migrates_new_install_gets_recommended(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(common, "POLICY_FILE", os.path.join(root, "policy.json")):
            self.assertTrue(settings.migrate_security_approvals())
            initial = common.read_json(common.POLICY_FILE, {})
            self.assertEqual(security.profile(initial), "recommended")
            self.assertEqual(set(initial["security"]), security.RECOMMENDED_ON)
            initial["unrelated_setting"] = True
            common.write_json(common.POLICY_FILE, initial)
            self.assertFalse(settings.migrate_security_approvals())
            self.assertEqual(security.profile(common.read_json(common.POLICY_FILE, {})), "recommended")
            common.write_json(common.POLICY_FILE, {"title": "existing"})
            settings.migrate_security_approvals()
            saved = common.read_json(common.POLICY_FILE, {})
            self.assertEqual(set(saved["security"]), security.MINIMAL_ON)
            self.assertEqual(security.profile(saved), "hole")

    def test_exact_legacy_profiles_migrate_to_their_new_versions(self):
        cases = [(None, security.LEGACY_MINIMAL, "hole"),
                 (1, security.LEGACY_RECOMMENDED_KEY, "recommended"),
                 (2, security.V2_MINIMAL_ON, "hole"),
                 (2, security.V2_RECOMMENDED_ON, "recommended"),
                 (2, security.IDS, "guarded")]
        for revision, old, name in cases:
            with self.subTest(revision=revision, name=name), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "policy.json")
                doc = {"security": {i: i in old for i in security.IDS}, "future": {"keep": 1}, "l5_action": "attention"}
                if revision is not None:
                    doc["security_rev"] = revision
                common.write_json(path, doc)
                with mock.patch.object(common, "POLICY_FILE", path):
                    self.assertTrue(settings.migrate_security_approvals())
                    saved = common.read_json(path, {})
                    self.assertEqual(saved["security"], security.presets()[name])
                    self.assertEqual(security.profile(saved), name)
                    self.assertEqual(saved["future"], doc["future"])
                    self.assertEqual(saved["l5_action"], "attention")
                    self.assertEqual(guard.load_policy(path, tree=True)["security_strict"], name == "guarded")
                    raw = Path(path).read_text()
                    self.assertFalse(settings.migrate_security_approvals())
                    self.assertEqual(Path(path).read_text(), raw)

    def test_one_optional_switch_difference_from_any_legacy_profile_stays_custom(self):
        for old in (security.V2_MINIMAL_ON, security.V2_RECOMMENDED_ON, set(security.IDS)):
            for switch in set(security.IDS) - security.REQUIRED_ON:
                with self.subTest(size=len(old), switch=switch), tempfile.TemporaryDirectory() as root:
                    path = os.path.join(root, "policy.json")
                    doc = {"security": {i: True for i in set(old) ^ {switch}}, "security_rev": 2}
                    before = security.state(doc)
                    common.write_json(path, doc)
                    with mock.patch.object(common, "POLICY_FILE", path):
                        settings.migrate_security_approvals()
                    saved = common.read_json(path, {})
                    self.assertEqual(security.profile(saved), "custom")
                    self.assertEqual(security.state(saved), before)

    def test_current_manual_custom_and_previously_frozen_choices_never_become_profiles(self):
        for marker in (None, 3):
            for old in (security.V2_MINIMAL_ON, security.V2_RECOMMENDED_ON, set(security.IDS)):
                with self.subTest(marker=marker, size=len(old)), tempfile.TemporaryDirectory() as root:
                    path = os.path.join(root, "policy.json")
                    doc = {"security": {i: True for i in old}, "security_rev": security.REV}
                    if marker is not None:
                        doc["security_profiles_migration"] = marker
                    common.write_json(path, doc)
                    with mock.patch.object(common, "POLICY_FILE", path):
                        settings.migrate_security_approvals()
                    saved = common.read_json(path, {})
                    self.assertEqual(security.profile(saved), "custom")
                    self.assertEqual(security.state(saved), security.state(doc))

    def test_previous_migration_preserves_current_explicit_profiles_and_reset_default(self):
        for name in (None, "hole", "recommended", "guarded"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "policy.json")
                doc = {"security_profiles_migration": 3}
                if name:
                    doc.update(security=security.presets()[name], security_rev=security.REV, security_profile=name)
                common.write_json(path, doc)
                with mock.patch.object(common, "POLICY_FILE", path):
                    settings.migrate_security_approvals()
                saved = common.read_json(path, {})
                self.assertEqual(security.profile(saved), name or "recommended")
                self.assertEqual(security.state(saved), security.state(doc))
                self.assertEqual(saved["security_profiles_migration"], security.PROFILES_MIGRATION)

    def test_explicit_apply_and_manual_save_with_same_bits_have_different_semantics(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(common, "POLICY_FILE", os.path.join(root, "policy.json")), mock.patch.object(security, "ENABLED", True):
            values = {"security": security.presets()["guarded"]}
            settings.update("security", values, security_profile="guarded")
            self.assertTrue(guard.load_policy(common.POLICY_FILE)["security_strict"])
            settings.update("security", values)
            pol = guard.load_policy(common.POLICY_FILE)
            self.assertFalse(pol["security_strict"])
            self.assertEqual(set(pol["security"]), set(security.IDS))
            with self.assertRaises(settings.SettingsError):
                settings.update("security", {"security": security.presets()["hole"]}, security_profile="guarded")

    def test_strict_effective_fields_are_visible_readonly_without_changing_stored_values(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(common, "POLICY_FILE", os.path.join(root, "policy.json")), mock.patch.object(security, "ENABLED", True):
            settings.update("security", {"security": security.presets()["guarded"]}, security_profile="guarded")
            fields = {f["key"]: f for sec in settings.describe()["sections"] for f in sec["fields"]}
            self.assertEqual(fields["prod_read"]["value"], "each")
            self.assertTrue(fields["mask_test"]["value"])
            self.assertTrue(fields["mask_test"]["readonly"])
            self.assertEqual(fields["mask_test"]["readonly_reason"], "Strict profile")
            with self.assertRaises(settings.SettingsError):
                settings.update("databases", {"prod_read": "session"})
            settings.update("security", {"security": security.presets()["guarded"]})
            fields = {f["key"]: f for sec in settings.describe()["sections"] for f in sec["fields"]}
            self.assertFalse(fields["mask_test"].get("readonly", False))

    def test_no_branch_protection_is_promised_for_an_empty_list(self):
        pol = {"security": security.presets()["recommended"], "security_rev": security.REV}
        tree = security.schema(pol)
        self.assertIn("no branch is blocked", next(g for g in tree if g["id"] == "tools")["help"])
        tree = security.schema(dict(pol, protected_branches=["main"]))
        self.assertNotIn("no branch is blocked", next(g for g in tree if g["id"] == "tools")["help"])

    def test_strict_suboptions_cannot_silently_weaken(self):
        pol = selected("guarded")
        self.assertTrue(pol["security_strict"])
        self.assertEqual(pol["l5_action"], "stop")
        self.assertEqual(pol["default_unlocks"], [])
        self.assertNotIn("GH_TOKEN", pol["allowed_env"])
        db = type("Db", (), {"policy": pol})()
        with mock.patch.object(dbtools, "read_json", return_value={"prod_read": "session", "mask_test": False}):
            cfg = dbtools.DbTools.db_config(db)
        self.assertEqual(cfg["prod_read"], "each")
        self.assertTrue(cfg["mask_test"])

    def test_strict_clears_explicit_writable_library_grants_before_generation(self):
        import holehome
        policy = selected("guarded")
        policy["holehome"]["rw_libraries"] = ["~/library"]
        policy = security.apply(policy)
        personal = {"libraries": {"~/library": {"path": "/library", "readwrite": False}}}
        out, _ = holehome.generate(personal, common.REAL_HOME, "/project", policy["holehome"])
        self.assertEqual(policy["holehome"]["rw_libraries"], [])
        self.assertFalse(out["profiles"]["project"]["libraries"][common.REAL_HOME + "/library"]["readwrite"])

    def test_strict_preflight_checks_project_overlay_tokens_network_and_rw_libraries(self):
        with tempfile.TemporaryDirectory() as root:
            project, library = os.path.join(root, "project"), os.path.join(root, "library")
            os.makedirs(os.path.join(project, ".hole"))
            os.makedirs(library)
            common.write_json(os.path.join(project, ".hole", "settings.json"), {
                "environment": {"CUSTOM_SERVICE_TOKEN": "fixture-only"},
                "network": {"allow": ["*"]},
                "libraries": {library: {"path": library, "readwrite": True}}})
            pol = selected("guarded")
            result = guard.preflight(project, {}, None, pol, {}, [], common.REAL_HOME)
            kinds = {b["kind"] for b in result["blocks"]}
            self.assertTrue({"strict-secret", "strict-network", "strict-library"} <= kinds, result["blocks"])

    def test_host_program_options_are_rejected_without_running_git(self):
        for args in (["fetch", "--upload-pack=evil", "origin"], ["push", "--exec=evil"],
                     ["rebase", "-x", "evil"], ["bisect", "run", "evil"],
                     ["fetch", "--upload-p=evil", "origin"], ["push", "--receiv=evil"],
                     ["clone", "-uevil", "https://example.invalid/repo"]):
            with self.subTest(args=args), self.assertRaises(gitops.GitError):
                gitops.refuse_program_options(args)
        gitops.refuse_program_options(["fetch", "origin"])
        gitops.refuse_program_options(["cherry-pick", "-x", "HEAD"])


class ProfileApprovalTest(unittest.TestCase):
    def setUp(self):
        self.m = sessions.Manager(background=False)
        self.s = sessions.Session(kind="project", project="profile-fixture", path="/fixture", runtime="hole", state="ready")
        self.m.sessions[self.s.id] = self.s

    def test_new_profiles_do_not_consume_legacy_broad_grants(self):
        self.m.policy = selected("recommended")
        self.s.approvals = [self.m.approval_scope("git_push", {})]
        self.assertFalse(self.m.ask("git_push", self.s, "Push", {}, timeout=0))
        self.assertTrue(self.m.auto_ok("git_commit"))
        self.assertFalse(self.m.auto_ok("http_dev", {"_force_approval": True}))

    def test_legacy_custom_keeps_its_existing_grants(self):
        self.m.policy = security.apply({"security": {"ask.git_push": True}, "security_rev": 2})
        self.s.approvals = [self.m.approval_scope("git_push", {})]
        self.assertTrue(self.m.ask("git_push", self.s, "Push", {}, timeout=0))

    def test_strict_refuses_direct_start_before_any_process_is_spawned(self):
        self.m.policy = selected("guarded")
        self.s.runtime = "direct"
        with mock.patch.object(self.m, "_start_direct") as start:
            self.m._start(self.s)
        start.assert_not_called()
        self.assertEqual(self.s.state, "blocked")

    def test_switching_to_strict_during_attestation_cannot_certify_old_sandbox(self):
        self.m.policy = selected("guarded")
        with mock.patch.object(self.m, "_fail") as fail:
            self.m._attest_done(self.s, {"ok": True, "failures": [], "checks": 1})
        fail.assert_called_once()
        self.assertFalse(self.s.attest["ok"])
        self.assertEqual(self.s.security_sandbox_rev, 0)
        self.s.security_sandbox_rev = -security.REV
        with mock.patch.object(self.m, "pty", mock.Mock()), mock.patch.object(self.m, "_set"), \
                mock.patch.object(self.m, "_task_pending", return_value=False), mock.patch.object(self.m, "_screen", return_value="ready"):
            self.m._attest_done(self.s, {"ok": True, "failures": [], "checks": 1})
        self.assertEqual(self.s.security_sandbox_rev, security.REV)

    def test_strict_tools_refuse_old_unverified_sandbox(self):
        self.m.policy = selected("guarded")
        with self.assertRaisesRegex(ValueError, "new, successfully verified"):
            self.m.tool(self.s, "git_status", {})


if __name__ == "__main__":
    unittest.main()
