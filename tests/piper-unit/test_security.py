"""The security tree: every isolation off unless switched on, whatever
policy.json says; the hardcoded sites follow the effective policy."""
import json
import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import dbtools
import github
import gitlab
import guard
import holehome
import security
import sessions
import settings


def touch(root, rel, text="x"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def policy_file(doc):
    fd, p = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f)
    return p


def fake_mgr(pol):
    m = types.SimpleNamespace(policy=pol)
    for name in ("auto_ok", "unlocked", "_gitlab_via_piper", "forbidden", "_protected", "_host_claude_dir", "host_claude",
                 "claude_dir", "_git_hole"):
        setattr(m, name, getattr(sessions.Manager, name).__get__(m))
    m._worktree_stack_info = lambda s: None   # no linked worktree here (test_worktree_stack has them)
    return m


class TreeTest(unittest.TestCase):
    def test_gitlab_via_piper_says_what_it_strips(self):
        # since 2026-10-15 a custom agent's allow.txt is left as written: the text may not promise more
        item = next(i for g in security.TREE for i in g.get("items", []) if i["id"] == "gitlab_via_piper")
        self.assertNotIn("the sandbox's allowed domains", item["help"])
        self.assertIn("allow.txt", item["help"])

    def test_suite_runs_on_the_legacy_policy(self):
        self.assertFalse(security.ENABLED)   # _env.py: the other modules assert the guarded defaults
        self.assertNotIn("ask", guard.load_policy("/nonexistent"))

    def test_missing_security_key_uses_the_selected_defaults(self):
        pol = guard.load_policy("/nonexistent", tree=True)
        self.assertEqual(pol["candidates"], [])
        self.assertEqual(pol["forbidden_project_keys"], guard.DEFAULT_POLICY["forbidden_project_keys"])   # forbidden_keys
        self.assertTrue(pol["forbidden_project_keys"])
        self.assertEqual(pol["secret_env"], guard.DEFAULT_POLICY["secret_env"])
        self.assertEqual(set(pol["security"]), security.DEFAULT_ON)
        self.assertEqual(pol["ask"], [k for k in security.ASK_KINDS if "ask." + k in security.DEFAULT_ON])
        self.assertIn("write_exec", pol["ask"])
        self.assertEqual((pol["claude_dir"], pol["codex_dir"], pol["git_mode"]), ("host", "host", "hole"))
        self.assertEqual(pol["l5_action"], "attention")
        for k in ("settings_review", "attestation", "gitlab_via_piper", "unlocks_required"):
            self.assertIs(pol[k], False, k)
        # 2026-10-16 Minimal: the host protections that block nothing in everyday work
        for k in ("protected_mounts", "piper_port_blocked", "git_fail_closed", "write_guard", "stack_scope_project"):
            self.assertIs(pol[k], True, k)
        self.assertIs(pol["db_prod_where"], True)
        self.assertIs(pol["git_run_projects"], True)
        self.assertIs(pol["gitlab_extra_tools"], True)
        self.assertIs(pol["login_spread"], True)
        self.assertIs(pol["automation_start_opts"], False)
        rules = dict(holehome.DEFAULTS, **pol["holehome"])
        self.assertEqual(rules["strip_includes"], [])   # all host includes follow the plain hole configuration
        self.assertEqual(rules["strip_allow"], [])
        self.assertTrue(rules["strip_env"])
        self.assertEqual(rules["strip_ports"], [2375])   # docker_proxy: the host Docker API stays out

    def test_minimal_is_the_agreed_selection(self):
        self.assertEqual(security.MINIMAL_ON, (security.V2_MINIMAL_ON - {"ask.http_test", "ask.db_write_test"}) | {"hidden_files", "ask.stack_mode"})
        self.assertLess(security.MINIMAL_ON, security.RECOMMENDED_ON)
        self.assertEqual(len(security.RECOMMENDED_ON), 38)

    def test_a_stored_old_profile_follows_the_profile(self):
        # Settings saved Minimal / Recommended before 2026-10-16: the stored copy is the old selection
        for old, now in ((security.LEGACY_MINIMAL, security.V2_MINIMAL_ON),
                         (security.LEGACY_RECOMMENDED_KEY, security.V2_RECOMMENDED_ON)):
            p = policy_file({"security": {i: True for i in old}})
            try:
                pol = guard.load_policy(p, tree=True)
            finally:
                os.unlink(p)
            self.assertEqual(set(pol["security"]), now)
            self.assertIs(pol["git_fail_closed"], True)

    def test_a_custom_choice_is_not_migrated(self):
        custom = set(security.LEGACY_MINIMAL) - {"ask.allow_domain"}
        st = security.state({"security": {i: True for i in custom}})
        self.assertEqual({i for i, v in st.items() if v}, custom)
        st = security.state({"security": {i: True for i in security.LEGACY_MINIMAL | {"strip_secrets"}}})
        self.assertEqual({i for i, v in st.items() if v}, security.LEGACY_MINIMAL | {"strip_secrets"})

    def test_explicit_empty_security_is_still_plain_hole(self):
        p = policy_file({"security": {}})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        self.assertEqual(set(pol["security"]), security.REQUIRED_ON)
        self.assertEqual(set(pol["ask"]), security.REQUIRED_ASK)
        self.assertFalse(pol["db_prod_where"])

    def test_an_existing_guarded_policy_is_switched_off(self):
        # what users have today: everything set to the guarded values by hand - the tree wins
        p = policy_file({"claude_dir": "session", "git_mode": "guarded", "auto_approve": [], "write_guard": True,
                         "candidates": ["**/*.pem"], "protected_branches": ["master"], "l5_action": "stop",
                         "holehome": {"strip_allow": ["gitlab.example"], "rw_libraries": ["~/lib"]}})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        self.assertEqual((pol["claude_dir"], pol["git_mode"], pol["write_guard"]), ("host", "hole", True))   # Minimal
        self.assertEqual((pol["candidates"], pol["protected_branches"], pol["l5_action"]), ([], ["master"], "stop"))
        self.assertEqual(pol["holehome"]["strip_allow"], [])
        self.assertEqual(pol["holehome"]["rw_libraries"], ["~/lib"])   # not governed by the tree: kept
        self.assertEqual(pol["ask"], [k for k in security.ASK_KINDS if "ask." + k in security.DEFAULT_ON])

    def test_switched_on_items_keep_the_users_configuration(self):
        p = policy_file({"security": {"sensitive_scan": True, "protected_branches": True, "l5_watch": True,
                                      "claude_isolated": True, "git_guarded": True, "ask.git_push": True,
                                      "unknown": True, "ask.db_write_prod": "yes"},
                         "candidates": ["**/*.pem"], "protected_branches": ["master"], "l5_action": "stop",
                         "claude_dir": "host"})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        self.assertEqual(pol["candidates"], ["**/*.pem"])
        self.assertEqual(pol["protected_branches"], ["master"])
        self.assertEqual(pol["l5_action"], "stop")
        self.assertEqual((pol["claude_dir"], pol["git_mode"]), ("session", "guarded"))   # forced on values
        self.assertEqual(set(pol["ask"]), {"git_push"} | security.REQUIRED_ASK)   # "yes" is not true
        self.assertNotIn("unknown", pol["security"])

    def test_l5_on_without_an_action_attends(self):
        pol = security.apply({"security": {"l5_watch": True}, "l5_action": "off"})
        self.assertEqual(pol["l5_action"], "attention")

    def test_guarded_preset_is_every_item(self):
        pol = security.apply({"security": security.presets()["guarded"]})
        self.assertEqual(pol["ask"], security.ASK_KINDS)
        self.assertEqual(set(pol["security"]), set(security.IDS))

    def test_plain_hole_preset_is_the_selected_first_use_default(self):
        self.assertEqual(security.presets()["recommended"], security.default_value())
        pol = security.apply({"security": security.presets()["hole"]})
        self.assertEqual(set(pol["security"]), security.MINIMAL_ON)

    def test_recommended_protects_host_and_production_without_strict_workflow(self):
        preset = security.presets()["recommended"]
        self.assertLess(security.MINIMAL_ON, set(preset))
        self.assertLess(set(preset), set(security.IDS))
        self.assertEqual(set(preset), security.RECOMMENDED_ON)
        p = policy_file({"security": preset})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        self.assertFalse(pol["settings_review"])
        self.assertFalse(pol["attestation"])
        self.assertTrue(pol["db_masking"])
        self.assertTrue(pol["db_prod_where"])
        self.assertTrue(pol["write_guard"])
        self.assertFalse(pol["candidates"])
        self.assertEqual(pol["l5_action"], "attention")
        self.assertTrue(pol["secret_env"])
        self.assertTrue(pol["protected_mounts"])
        self.assertTrue(pol["piper_port_blocked"])
        self.assertTrue(pol["stack_scope_project"])
        self.assertTrue(pol["git_fail_closed"])
        personal = {"network": {"hostGatewayDomains": ["host.docker.internal:2375,5432"]},
                    "environment": {"GITLAB_TOKEN": "fake-test-token", "SAFE": "yes"}}
        generated, _ = holehome.generate(personal, common.REAL_HOME, None, pol["holehome"])
        self.assertEqual(generated["network"]["hostGatewayDomains"], ["host.docker.internal:5432"])
        self.assertEqual(generated["environment"], {"SAFE": "yes"})
        self.assertEqual((pol["claude_dir"], pol["codex_dir"]), ("host", "host"))
        self.assertEqual(pol["git_mode"], "hole")
        self.assertTrue(pol["login_spread"])
        self.assertFalse(pol["unlocks_required"])
        self.assertFalse(pol["gitlab_via_piper"])
        for kind in ("git_push", "gitlab_mr_merge", "gitlab_job_play", "db_unmask", "db_read_prod", "db_write_prod", "write_exec"):
            self.assertIn(kind, pol["ask"])
        for kind in ("http_dev", "db_write_dev", "db_read_test", "gitlab_job_log", "github_job_log",
                     "git_commit", "git_run", "gitlab_mr_write", "github_pr_write"):
            self.assertNotIn(kind, pol["ask"])
        self.assertTrue(pol["gitlab_extra_tools"], "merge/CI use approval, not a blanket ban")

    def test_old_recommended_choices_remain_authoritative(self):
        old_off = {"login_isolated", "gitlab_extra", "ask.gitlab_job_log", "ask.gitlab_read",
                   "ask.github_job_log", "ask.http_dev", "ask.db_write_dev", "ask.db_read_test",
                   "ask.propose_session"}
        old = {i: True for i in security.IDS if i not in old_off}
        self.assertNotEqual(old, security.presets()["recommended"])
        self.assertEqual({k for k, on in security.state({"security": old}).items() if on}, set(old))
        self.assertEqual(set(security.apply({"security": old})["security"]), set(old))

    def test_every_configurable_card_kind_has_a_switch(self):
        kinds = set(security.ASK_KINDS)
        self.assertEqual(len(kinds), len(security.ASK_KINDS))
        # These one-run input/code cards carry no remembered permission and
        # cannot be disabled by the general approval tree.
        mandatory = {'http_runtime_code', 'http_auth_signin', 'http_cert_passphrase'}
        self.assertTrue(mandatory.issubset(sessions.ALWAYS_ASK))
        self.assertTrue(mandatory.isdisjoint(kinds))
        missing = ({k for k, _ in settings.APPROVAL_KINDS} | set(sessions.ALWAYS_ASK) | set(dbtools.DB_ALWAYS)
                   | set(gitlab.CARD_TOOLS.values()) | set(github.CARD_TOOLS.values())
                   | {"gitlab_note_edit", "github_note_edit", "gitlab_job_protected", "git_run_project", "write_exec",
                      "allow_domain", "settings_change", "db_other"}) - kinds - mandatory
        self.assertEqual(missing, set())

    def test_schema_lists_every_id_once(self):
        ids = [it["id"] for g in security.schema() for it in g.get("items", [])] + \
              [it["id"] for g in security.schema() for sub in g.get("groups", []) for it in sub["items"]]
        self.assertEqual(sorted(ids), sorted(security.IDS))

    def test_f26_every_kind_and_guard_has_a_short_phrase(self):
        """F26: Settings › Security builds "asks before … / On its own: …" and "Always protected: …" from these."""
        self.assertEqual(sorted(k for k in security.ASK_KINDS if not security.ASK_SHORT.get(k)), [])
        self.assertEqual(sorted(it["id"] for it in security.ITEMS if not security.TREE_SHORT.get(it["id"])), [])
        sch = security.schema()
        appr = next(g for g in sch if g["id"] == "approvals")
        git = next(sg for sg in appr["groups"] if sg["id"] == "git")
        self.assertEqual(git["short"], "Git")
        self.assertEqual({it["kind"]: it["short"] for it in git["items"]}["git_push"], "pushing")
        self.assertEqual(next(sg for sg in appr["groups"] if sg["id"] == "agents")["short"], "Agents & Piper")
        self.assertTrue(all(it["short"] for g in sch for it in g.get("items", [])))
        for it in (it for g in sch for it in g.get("items", [])):   # no tool ids in the words
            self.assertNotIn("_", it["short"], it["id"])
        for it in (it for sg in appr["groups"] for it in sg["items"]):
            self.assertNotIn("_", it["short"], it["id"])


class SitesTest(unittest.TestCase):
    def test_mandatory_http_cards_cannot_be_auto_approved_with_all_switches_off(self):
        kinds = ('http_runtime_code', 'http_auth_signin', 'http_cert_passphrase')
        m = fake_mgr(security.apply({'security':{}}))
        m.policy.update(ask=[], auto_approve=list(kinds))
        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertFalse(m.auto_ok(kind, {'cls':'dev'}))

    def test_ask_follows_the_tree(self):
        m = fake_mgr(security.apply({"security": {"ask.git_push": True, "ask.db_write_prod": True}}))
        self.assertTrue(m.auto_ok("git_commit"))
        self.assertFalse(m.auto_ok("http_prod"))                 # production cannot be disabled globally
        self.assertFalse(m.auto_ok("git_push"))
        self.assertFalse(m.auto_ok("db_write_prod"))
        self.assertTrue(m.auto_ok("git_commit", {"danger": True}))   # hidden_files off: no red card

    def test_red_card_while_hidden_files_on(self):
        m = fake_mgr(security.apply({"security": {"hidden_files": True}}))
        self.assertFalse(m.auto_ok("git_run", {"danger": True}))
        self.assertTrue(m.auto_ok("git_run", {}))

    def test_ask_before_the_tree_unchanged(self):
        m = fake_mgr(guard.load_policy("/nonexistent", tree=False))
        self.assertTrue(m.auto_ok("git_commit"))
        self.assertFalse(m.auto_ok("gitlab_comment"))
        self.assertFalse(m.auto_ok("git_commit", {"danger": True}))
        m.policy["auto_approve"] = ["http_prod"]
        self.assertFalse(m.auto_ok("http_prod"))

    def test_unlocks(self):
        s = types.SimpleNamespace(unlock={})
        self.assertTrue(fake_mgr(security.apply({})).unlocked(s, "projects_write"))
        self.assertFalse(fake_mgr(security.apply({"security": {"unlocks": True}})).unlocked(s, "projects_write"))
        self.assertFalse(fake_mgr(guard.load_policy("/nonexistent", tree=False)).unlocked(s, "git"))
        s.unlock = {"git": True}
        self.assertTrue(fake_mgr(security.apply({"security": {"unlocks": True}})).unlocked(s, "git"))

    def test_where_switch_overrides_a_saved_connection_setting(self):
        db = types.SimpleNamespace(policy=security.apply({"security": {"db_prod_where": True}}))
        with mock.patch.object(dbtools, "read_json", return_value={"prod_require_where": False}):
            self.assertTrue(dbtools.DbTools.db_config(db)["prod_require_where"])

    def test_forced_excludes_follow_hidden_files_switch(self):
        pol = security.apply({"security": {}, "holehome": {"force_exclude": [".git"]}})
        self.assertEqual(pol["holehome"]["force_exclude"], [])
        pol = security.apply({"security": {"hidden_files": True}, "holehome": {"force_exclude": [".git"]}})
        self.assertEqual(pol["holehome"]["force_exclude"], [".git"])

    def test_scan_ignores_git_when_forced_excludes_are_off(self):
        p = policy_file({"security": {"sensitive_scan": True, "l5_watch": True}, "candidates": [".git", "*.pem"],
                         "holehome": {"force_exclude": [".git"]}})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        self.assertEqual(pol["candidates"], ["*.pem"])
        self.assertEqual(pol["l5_candidates"], ["*.pem"])

    def test_watcher_finds_sensitive_files_without_start_scan(self):
        with tempfile.TemporaryDirectory() as root:
            touch(root, "secret.pem")
            pol = security.apply(guard.load_policy("/nonexistent", tree=False) | {"security": {"l5_watch": True}})
            self.assertEqual(pol["candidates"], [])
            sess = types.SimpleNamespace(id="watch", coverage=[{"root": root, "kind": "project"}],
                                         watch=[], state="ready", detail="", save=lambda: None)
            mgr = types.SimpleNamespace(policy=pol, lock=threading.RLock(), bump=lambda: None)
            sessions.Manager._watch(mgr, sess)
            self.assertIn(os.path.join(root, "secret.pem"), sess.watch)

    def test_reload_invalidates_automatic_approvals(self):
        mgr = types.SimpleNamespace(policy={"ask": []}, _db_ok={("s", "c", "read")},
                                    _http_session_ok={("s", "dev", "sha")}, lock=threading.RLock(), pending={},
                                    _sync_watch=lambda: None, bump=lambda: None)
        with mock.patch.object(guard, "load_policy", return_value={"ask": ["db_read_test", "http_dev"]}):
            sessions.Manager.reload_policy(mgr)
        self.assertEqual(mgr._db_ok, set())
        self.assertEqual(mgr._http_session_ok, set())

    def test_session_reports_effective_unlocks(self):
        sess = sessions.Session(kind="project", project="", path="/unused", state="ready")
        mgr = fake_mgr(security.apply({"security": {}}))
        reply = sessions.Manager.settings_tool(mgr, sess, "piper_session", {})
        self.assertEqual(reply["unlocked"], sorted(sessions.UNLOCKS))
        mgr.policy = security.apply({"security": {"unlocks": True, "git_guarded": True}})
        self.assertEqual(sessions.Manager.settings_tool(mgr, sess, "piper_session", {})["unlocked"], [])

    def test_protected_mounts_and_gitlab(self):
        m = fake_mgr(security.apply({"security": {}}))
        self.assertEqual(m.forbidden(types.SimpleNamespace(host_codex=False)), [])
        self.assertEqual(m._protected(), [])   # Hole includes are not filtered when this guard is off
        guarded = fake_mgr(security.apply({"security": {"protected_mounts": True}}))
        self.assertTrue(guarded._protected())
        self.assertFalse(m._gitlab_via_piper())
        self.assertTrue(fake_mgr(security.apply({"security": {"gitlab_via_piper": True}}))._gitlab_via_piper())

    def test_db_sites(self):
        db = types.SimpleNamespace(policy=security.apply({"security": {}}))
        self.assertFalse(dbtools.DbTools._db_masked(db, {"cls": "prod"}))
        self.assertFalse(dbtools.DbTools._db_masked(db, {"cls": "prod", "mask": True}))   # the connection's own
        self.assertFalse(dbtools.DbTools.db_config(db)["prod_require_where"])
        on = types.SimpleNamespace(policy=security.apply({"security": {"db_masking": True, "db_prod_where": True}}))
        on.db_config = lambda: dbtools.DbTools.db_config(on)
        self.assertTrue(dbtools.DbTools._db_masked(on, {"cls": "prod"}))
        self.assertTrue(dbtools.DbTools.db_config(on)["prod_require_where"])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        touch(self.proj, "config/secrets/a.properties")
        touch(self.proj, ".hole/settings.json", '{"agents": {}}')

    def test_off_tree_starts_what_hole_starts(self):
        p = policy_file({"security": {}})
        try:
            pol = guard.load_policy(p, tree=True)
        finally:
            os.unlink(p)
        r = guard.preflight(self.proj, {}, None, pol, {}, [], "/home")
        self.assertTrue(r["ok"], r["blocks"])   # unreviewed settings, an uncovered candidate, a forbidden key
        self.assertEqual(r["coverage"][0]["candidates"], [])

    def test_minimal_blocks_only_a_forbidden_key(self):
        pol = guard.load_policy("/nonexistent", tree=True)   # first use = Minimal
        r = guard.preflight(self.proj, {}, None, pol, {}, [], "/home")
        self.assertEqual({b["kind"] for b in r["blocks"]}, {"forbidden-key"})   # no review, no scan
        os.unlink(os.path.join(self.proj, ".hole/settings.json"))
        self.assertTrue(guard.preflight(self.proj, {}, None, pol, {}, [], "/home")["ok"])

    def test_on_tree_blocks_as_before(self):
        pol = security.apply(guard.load_policy("/nonexistent", tree=False) | {"security": security.presets()["guarded"]})
        r = guard.preflight(self.proj, {}, None, pol, {}, [], "/home")
        kinds = {b["kind"] for b in r["blocks"]}
        self.assertTrue({"settings-changed", "uncovered"} <= kinds, kinds)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        security.ENABLED = True
        try:
            os.unlink(common.POLICY_FILE)
        except FileNotFoundError:
            pass

    def tearDown(self):
        security.ENABLED = False

    def fields(self):
        return {(sec["id"], f["key"]): f for sec in settings.describe()["sections"] for f in sec["fields"]}

    def test_section_and_hidden_fields(self):
        f = self.fields()
        tree = f[("security", "security")]
        self.assertEqual(tree["default"], security.default_value())
        self.assertEqual(tree["value"], security.state({}))
        self.assertEqual([g["id"] for g in tree["tree"]], ["start", "sandbox", "tools", "approvals"])
        for sid, k in (("databases", "prod_require_where"), ("sandbox", "claude_dir"), ("sessions", "auto_approve"), ("git", "git_mode"),
                       ("sessions", "write_guard"), ("git", "git_fail_closed")):
            self.assertNotIn((sid, k), f)   # the tree sets them
        # F26 (changes 9.46): no Approvals page - its two GUI fields live in New sessions
        self.assertNotIn("approvals", {sid for sid, _ in f})
        self.assertIn(("sessions", "open_links_on_host"), f)
        self.assertIn(("sessions", "clipboard_arm_enter"), f)
        self.assertEqual(f[("git", "protected_branches")]["governed_by"],
                         {"id": "protected_branches", "label": "Protected branches", "on": True})
        self.assertEqual(f[("scan", "candidates")]["governed_by"]["id"], "sensitive_scan")
        self.assertEqual(f[("holehome", "strip_allow")]["governed_by"]["id"], "gitlab_via_piper")

    def test_db_dry_run_is_default_on_and_independent_of_guard_presets(self):
        field = self.fields()[("security", "db_dry_run")]
        self.assertEqual((field["type"], field["label"], field["default"], field["value"]),
                         ("bool", "Dry run test writes before asking", True, True))
        self.assertNotIn("db_dry_run", security.IDS)
        self.assertFalse(any(s["section"] == "security" for s in settings.agent_describe()))
        settings.update("security", {"db_dry_run": False})
        self.assertIs(self.fields()[("security", "db_dry_run")]["value"], False)
        for choice in security.presets().values():
            path = policy_file({"db_dry_run": False, "security": choice, "security_rev": security.REV})
            try:
                effective = guard.load_policy(path, tree=True)
            finally:
                os.unlink(path)
            self.assertIs(effective["db_dry_run"], False)
            self.assertNotIn("db_dry_run", effective["security"])

    def test_update_stores_only_switched_on_items(self):
        settings.update("security", {"security": {"attestation": True, "ask.git_push": True, "write_guard": False}})
        with open(common.POLICY_FILE) as fh:
            self.assertEqual(json.load(fh)["security"], {"attestation": True, "ask.git_push": True,
                                                     **dict.fromkeys(security.REQUIRED_ON, True)})
        pol = guard.load_policy(common.POLICY_FILE)
        self.assertIs(pol["attestation"], True)
        self.assertEqual(set(pol["ask"]), {"git_push"} | security.REQUIRED_ASK)
        self.assertIs(self.fields()[("security", "security")]["value"]["attestation"], True)
        with self.assertRaises(settings.SettingsError):
            settings.update("security", {"security": {"nope": True}})
        with self.assertRaises(settings.SettingsError):
            settings.update("security", {"security": {"attestation": "yes"}})

    def test_explicit_minimal_choice_is_stored_even_when_equal_to_default(self):
        settings.update("security", {"security": security.default_value()})
        with open(common.POLICY_FILE) as f:
            self.assertEqual(json.load(f)["security"], security.default_value())

    def test_an_agent_never_changes_the_tree(self):
        with self.assertRaises(settings.SettingsError) as e:
            settings.agent_preview("security", {"security": {"attestation": False}})
        self.assertIn("security", e.exception.errors)
        with self.assertRaises(settings.SettingsError):
            settings.agent_preview("sessions", {"write_guard": False})
        self.assertNotIn("security", [x["section"] for x in settings.agent_describe()])



class DisabledControlsTest(unittest.TestCase):
    def manager(self, **policy):
        m = sessions.Manager.__new__(sessions.Manager)
        m._background_enabled = True
        m._stop_event = threading.Event()
        m._service_lock = threading.RLock()
        m._services = []
        m.policy = policy
        m.lock = threading.RLock()
        m.sessions, m.pending = {}, {}
        m._watch_stop, m._watch_thread = threading.Event(), None
        m._db_ok, m._http_session_ok = set(), set()
        m.bump = mock.Mock()
        return m

    def test_l5_off_does_not_scan_and_clears_old_findings_without_a_thread(self):
        m = self.manager(l5_action="off")
        s = sessions.Session(id="off-watch", state="attention", idle=True, detail="L5: old finding", watch=["old.pem"])
        other = sessions.Session(id="permission", state="attention", detail="Claude needs permission", watch=["old.pem"])
        m.sessions = {s.id: s, other.id: other}
        with mock.patch.object(s, "save"), mock.patch.object(other, "save"), \
                mock.patch.object(sessions.threading, "Thread") as worker, mock.patch.object(guard, "scan_candidates") as scan:
            m._sync_watch()
            m._watch(s)
        worker.assert_not_called()
        scan.assert_not_called()
        self.assertEqual((s.watch, s.state, s.detail), ([], "waiting", ""))
        self.assertEqual((other.watch, other.state, other.detail), ([], "attention", "Claude needs permission"))

    def test_hole_git_clears_legacy_guard_attention_but_keeps_active_l5(self):
        m = self.manager(l5_action="attention", git_mode="hole")
        s = sessions.Session(id="old-guard", state="attention", idle=False,
                             detail="git guard: old finding", watch=["active.pem"])
        other = sessions.Session(id="real-permission", state="attention", detail="Claude needs permission")
        m.sessions = {s.id: s, other.id: other}
        with mock.patch.object(s, "save"), mock.patch.object(other, "save"), \
                mock.patch.object(sessions.threading, "Thread"):
            m._sync_watch()
        self.assertEqual((s.state, s.detail, s.watch), ("working", "", ["active.pem"]))
        self.assertEqual((other.state, other.detail), ("attention", "Claude needs permission"))

    def test_l5_enable_disable_enable_has_one_worker_and_cancels_the_old_generation(self):
        m = self.manager(l5_action="off")
        with mock.patch.object(sessions.threading, "Thread") as worker:
            m._sync_watch()
            m.policy["l5_action"] = "attention"
            m._sync_watch()
            first = m._watch_stop
            m._sync_watch()
            self.assertEqual(worker.call_count, 1)
            m.policy["l5_action"] = "off"
            m._sync_watch()
            self.assertTrue(first.is_set())
            m.policy["l5_action"] = "attention"
            m._sync_watch()
            self.assertEqual(worker.call_count, 2)
            self.assertIsNot(first, m._watch_stop)
            self.assertFalse(m._watch_stop.is_set())

    def test_disabling_l5_during_a_scan_does_not_publish_its_result(self):
        with tempfile.TemporaryDirectory() as root:
            touch(root, "secret.pem")
            m = self.manager(l5_action="attention", candidates=["*.pem"])
            s = sessions.Session(id="in-flight", state="ready", watch=[], coverage=[{"root": root, "kind": "project"}])
            def scan(*args):
                m.policy["l5_action"] = "off"
                m._sync_watch()
                return ["secret.pem"]
            with mock.patch.object(guard, "scan_candidates", side_effect=scan), mock.patch.object(s, "save") as save:
                m._watch(s, m._watch_stop)
            self.assertEqual(s.watch, [])
            save.assert_not_called()

    def test_attestation_off_is_ready_without_preflight_waiting_or_docker(self):
        m = self.manager(attestation=False)
        s = sessions.Session(id="off-attest", state="attesting")
        m.pty = mock.Mock()
        m._instance = mock.Mock(return_value=None)
        m._preflight = mock.Mock(side_effect=AssertionError("preflight while off"))
        m._task_pending = mock.Mock(return_value=False)
        m._screen = mock.Mock(return_value="ready")
        m._set = lambda sess, state, phase, detail: setattr(sess, "state", state)
        # time.sleep is the process-wide one: another thread sleeping meanwhile (a loop left by an earlier
        # test in the full suite) must not count - this one stands in for it
        stop = threading.Event()
        other = threading.Thread(target=lambda: [time.sleep(0.005) for _ in iter(lambda: stop.is_set(), True)], daemon=True)
        me, real_sleep, slept = threading.get_ident(), time.sleep, []
        def sleep(sec):   # this thread's waits are recorded, every other thread really sleeps
            if threading.get_ident() == me:
                slept.append(sec)
            else:
                real_sleep(sec)
        with mock.patch.object(s, "save"), mock.patch.object(sessions.time, "sleep", side_effect=sleep), \
                mock.patch.object(guard, "attest") as attest:
            other.start()
            threading.Event().wait(0.05)   # the other thread runs (time.sleep here would be the mock)
            m._attest_inner(s, True)
            stop.set()
            other.join(2)
        self.assertEqual(s.state, "ready")
        self.assertTrue(s.attest["skipped"])
        self.assertEqual(slept, [])
        attest.assert_not_called()
        m.pty.call.assert_any_call("unlock", id=s.id)

    def test_disabled_gitlab_tools_do_not_reach_the_client(self):
        m = self.manager()
        s = sessions.Session(id="gitlab-off", kind="project", gitlab=["grp/app"])
        with mock.patch("features.on", return_value=False), mock.patch.object(gitlab, "GitLab") as client:
            for tool in ("gitlab_issue", "gitlab_mr_create", "gitlab_inbox"):
                with self.assertRaisesRegex(ValueError, "GitLab module is off"):
                    m.tool(s, tool, {})
        client.assert_not_called()

    def test_gitlab_toggle_does_not_permanently_disable_a_running_sessions_tools(self):
        m = self.manager()
        s = sessions.Session(id="gitlab-toggle", kind="project")
        m.inbox_tool = mock.Mock(return_value={"items": []})
        for enabled in (False, True, False, True):
            with mock.patch("features.on", return_value=enabled):
                if enabled:
                    self.assertEqual(m.tool(s, "gitlab_inbox", {}), {"items": []})
                else:
                    with self.assertRaisesRegex(ValueError, "GitLab module is off"):
                        m.tool(s, "gitlab_inbox", {})
        self.assertEqual(m.inbox_tool.call_count, 2)

    def test_reload_cancels_disabled_cards_without_approving_them(self):
        m = self.manager(ask=["http_test", "git_push"], l5_action="off")
        changed = {"kind": "http_test", "payload": {}, "_ev": threading.Event(), "_ok": False}
        kept = {"kind": "git_push", "payload": {}, "_ev": threading.Event(), "_ok": False}
        danger = {"kind": "http_test", "payload": {"danger": True}, "_ev": threading.Event(), "_ok": False}
        answered = {"kind": "http_test", "payload": {}, "_ev": threading.Event(), "_ok": True}
        answered["_ev"].set()
        m.pending = {"changed": changed, "kept": kept, "danger": danger, "answered": answered}
        with mock.patch.object(guard, "load_policy", return_value={"ask": ["git_push"], "l5_action": "off"}):
            m.reload_policy()
        self.assertEqual(list(m.pending), ["kept", "danger", "answered"])
        self.assertTrue(changed["_ev"].is_set())
        self.assertFalse(changed["_ok"])
        self.assertFalse(kept["_ev"].is_set())
        self.assertFalse(danger["_ev"].is_set())
        self.assertTrue(answered["_ok"])


class MinimalEffectsTest(unittest.TestCase):
    """2026-10-16 Minimal (no `security` key, tree on): each added item has its effect through the real
    Manager paths - policy reload, generated hole settings, preflight, host git, writes."""

    @classmethod
    def setUpClass(cls):
        import _env as env
        cls.m = env.manager(cls, background=True)

    def setUp(self):
        security.ENABLED = True
        with open(sessions.PERSONAL_SETTINGS, "rb") as f:   # _env's personal hole settings: restored after
            self._personal = f.read()
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            os.unlink(common.POLICY_FILE)
        except FileNotFoundError:
            pass
        with open(sessions.PERSONAL_SETTINGS, "wb") as f:
            f.write(self._personal)
        security.ENABLED = False
        self.m.reload_policy()   # gitops.FAIL_CLOSED back to the suite's policy

    def policy(self, doc):
        """Historical Minimal fixture: explicitly retained REV-2 selection, not the new default."""
        if not doc:
            doc = {"security": dict.fromkeys(security.V2_MINIMAL_ON, True), "security_rev": 2}
        with open(common.POLICY_FILE, "w") as f:
            json.dump(doc, f)
        self.m.reload_policy()

    def personal(self, doc):
        os.makedirs(os.path.dirname(sessions.PERSONAL_SETTINGS), exist_ok=True)
        with open(sessions.PERSONAL_SETTINGS, "w") as f:
            json.dump(doc, f)

    def test_gitlab_only_strips_the_configured_host_from_personal_profiles(self):
        host = "gitlab.example.test"
        self.personal({"network": {"allow": [host, "registry.example.test"]},
                       "profiles": {"extra": {"network": {"allow": [host, "other.example.test"]}}}})
        for enabled in (False, True):
            self.policy({"gitlab_host": host, "security": {"gitlab_via_piper": enabled}})
            base, _ = self.m._base_settings()
            self.assertEqual(host in base["network"]["allow"], not enabled)
            self.assertEqual(host in base["profiles"]["extra"]["network"]["allow"], not enabled)
            self.assertIn("registry.example.test", base["network"]["allow"])

    def project(self, name, files=None):
        import _env as env
        root = env.make_project(name, files or {"src/a.txt": "a\n"})
        env.register(name, root)
        return root

    def session(self, root, name):
        # claude_mode host: what _start sets under Minimal's claude_dir=host
        return sessions.Session(kind="project", project=name, path=root, profile=None, claude_mode="host")

    def test_protected_mounts_filters_piper_dirs_and_keeps_normal_includes(self):
        root = self.project("minmounts")
        s = self.session(root, "minmounts")
        os.makedirs(os.path.join(common.REAL_HOME, ".m2"), exist_ok=True)
        touch(common.REAL_HOME, ".gitconfig", "[user]\n")
        inc = {common.PIPER_HOME: "~/pd", os.path.join(common.REAL_HOME, ".hole"): "~/hd",
               "~/.m2": "~/.m2", "~/.gitconfig": "~/.gitconfig", "~/.claude": "~/.claude"}
        self.personal({"files": {"include": inc}})
        self.policy(None)
        self.assertEqual(self.m._protected()[:2], [common.PIPER_HOME, os.path.join(common.REAL_HOME, ".hole")])
        base, changes = self.m._base_settings(s)
        kept = set(base["files"]["include"].values())
        self.assertFalse({"~/pd", "~/hd"} & kept, base["files"]["include"])
        self.assertTrue({"~/.m2", "~/.gitconfig", "~/.claude"} <= kept, kept)
        self.assertTrue(any("overlaps protected" in c for c in changes), changes)
        r = self.m._preflight(s, base)
        self.assertTrue(r["ok"], r["blocks"])   # the everyday includes block nothing
        # the include reaching preflight anyway (e.g. a profile): blocked
        raw = json.loads(json.dumps(base))
        raw["files"]["include"][common.PIPER_HOME] = "~/pd"
        kinds = {(b["kind"], b["path"]) for b in self.m._preflight(s, raw)["blocks"]}
        self.assertIn(("protected-mounted", common.PIPER_HOME), kinds)
        # plain hole ({}): mounted as configured, no block
        self.policy({"security": {}})
        base, _ = self.m._base_settings(s)
        self.assertIn("~/pd", set(base["files"]["include"].values()))
        self.assertNotIn("protected-mounted", {b["kind"] for b in self.m._preflight(s, base)["blocks"]})

    def test_piper_port_in_the_gateway_blocks_the_start(self):
        root = self.project("minport")
        s = self.session(root, "minport")
        self.personal({"network": {"hostGatewayDomains": ["host.internal:%d" % sessions.PIPER_PORT]}})
        self.policy(None)
        base, _ = self.m._base_settings(s)
        r = self.m._preflight(s, base)
        self.assertEqual([b["kind"] for b in r["blocks"]], ["forbidden-port"])
        self.assertIn(str(sessions.PIPER_PORT), r["blocks"][0]["detail"])
        self.personal({"network": {"hostGatewayDomains": ["host.internal:19998"]}})
        base, _ = self.m._base_settings(s)
        self.assertTrue(self.m._preflight(s, base)["ok"])
        self.personal({"network": {"hostGatewayDomains": ["host.internal:%d" % sessions.PIPER_PORT]}})
        self.policy({"security": {}})
        base, _ = self.m._base_settings(s)
        self.assertTrue(self.m._preflight(s, base)["ok"])

    def test_docker_port_is_dropped_from_the_personal_gateway(self):
        self.personal({"network": {"hostGatewayDomains": ["host.internal:2375,19998"]}})
        self.policy(None)
        base, changes = self.m._base_settings()
        self.assertEqual(base["network"]["hostGatewayDomains"], ["host.internal:19998"])
        self.assertTrue(any("2375" in c for c in changes), changes)
        self.policy({"security": {}})
        self.assertEqual(self.m._base_settings()[0]["network"]["hostGatewayDomains"], ["host.internal:2375,19998"])

    def _repo(self, name):
        import subprocess
        repo = os.path.realpath(self.project(name, {"a.txt": "a\n"}))
        env = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T",
                   GIT_COMMITTER_EMAIL="t@e", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
            subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env)
        marker = os.path.join(repo, "..", name + "-hook-ran")
        hook = os.path.join(repo, ".git", "hooks", "pre-commit")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\ntouch '%s'\n" % marker)
        os.chmod(hook, 0o755)
        return repo, os.path.realpath(marker)

    def test_host_git_runs_no_planted_hook_and_refuses_program_config(self):
        import gitops
        import subprocess
        repo, marker = self._repo("minhook")
        self.policy(None)
        self.assertIs(gitops.FAIL_CLOSED, True)
        touch(repo, "a.txt", "b\n")
        r = gitops.commit(repo, "minimal", ["a.txt"])
        self.assertTrue(r["hooks_disabled"])
        self.assertFalse(os.path.exists(marker))   # the sandbox-planted hook did not run on the host
        subprocess.run(["git", "-C", repo, "config", "core.sshCommand", "/bin/true"], check=True)
        touch(repo, "a.txt", "c\n")
        with self.assertRaisesRegex(gitops.GitError, "names programs"):
            gitops.commit(repo, "refused", ["a.txt"])
        self.policy({"security": {}})
        self.assertIs(gitops.FAIL_CLOSED, False)
        r = gitops.commit(repo, "plain", ["a.txt"])   # as a plain hole: the config is not refused, the hook runs
        self.assertFalse(r["hooks_disabled"])
        self.assertTrue(os.path.exists(marker))

    def test_host_run_file_of_another_project_is_a_write_exec_card(self):
        target = self.project("minwtarget", {"src/a.txt": "old\n", "pom.xml": "<old/>\n", ".gitlab-ci.yml": "old: 1\n"})
        home = self.project("minwhome")
        s = self.session(home, "minwhome")
        s.state = "exited"
        self.m.sessions[s.id] = s
        kinds = []
        orig = self.m.ask

        def spy(kind, *a, **k):   # the real ask auto-approves; a real card is answered "no" at once
            kinds.append(kind)
            return False if not self.m.auto_ok(kind, a[2] if len(a) > 2 else None) else orig(kind, *a, **k)
        self.m.ask = spy
        self.addCleanup(setattr, self.m, "ask", orig)
        self.policy(None)
        self.assertIs(self.m.unlocked(s, "projects_write"), True)   # unlocks off under Minimal
        edit = {"project": "minwtarget", "old_string": "old", "new_string": "new"}
        self.m.tool(s, "edit_file", dict(edit, path="src/a.txt"))
        self.assertEqual(kinds, ["edit_file"])                    # a plain file: approved on its own
        with open(os.path.join(target, "src/a.txt")) as f:
            self.assertEqual(f.read(), "new\n")
        for path in ("pom.xml", ".gitlab-ci.yml"):
            with self.assertRaisesRegex(ValueError, "declined"):
                self.m.tool(s, "edit_file", dict(edit, path=path))
            self.assertEqual(kinds[-1], "write_exec")
            with open(os.path.join(target, path)) as f:
                self.assertIn("old", f.read())
        self.policy({"security": {}})
        self.m.tool(s, "edit_file", dict(edit, path="pom.xml"))
        self.assertEqual(kinds[-1], "edit_file")                  # plain hole: like any file, no card
        with open(os.path.join(target, "pom.xml")) as f:
            self.assertEqual(f.read(), "<new/>\n")

    def test_git_run_planting_config_in_another_project_is_a_write_exec_card(self):
        # R1 (merge review 2026-10-17): `git config core.fsmonitor <cmd>` in another project ran card-free and the
        # user's next host `git status` ran the program; edit_file of a host-run file there was already a card
        import subprocess
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="T",
                   GIT_AUTHOR_EMAIL="t@e", GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@e")
        target = os.path.realpath(self.project("mingtarget", {"a.txt": "a\n"}))
        home = os.path.realpath(self.project("minghome", {"b.txt": "b\n"}))
        for repo in (target, home):
            for a in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "i"]):
                subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=env)
        s = self.session(home, "minghome")
        s.state = "exited"
        self.m.sessions[s.id] = s
        kinds = []
        orig = self.m.ask

        def spy(kind, *a, **k):   # a real card is answered "no" at once
            kinds.append(kind)
            return False if not self.m.auto_ok(kind, a[2] if len(a) > 2 else None) else orig(kind, *a, **k)
        self.m.ask = spy
        self.addCleanup(setattr, self.m, "ask", orig)
        self.policy(None)
        marker = os.path.join(os.path.dirname(target), "ming-planted")
        for argv in (["config", "core.fsmonitor", "touch %s; echo" % marker],   # (-c: git_fail_closed refuses it)
                     ["checkout", "HEAD", "--", "a.txt"],
                     # an allowlist, not a list of writers (Fable review of the fix): re-init copies hooks from a
                     # template the agent wrote, clean / read-tree rewrite the tree, remote set-url sets config
                     ["init", "--template=%s" % os.path.join(home, "tpl")], ["clean", "-fdx"],
                     ["read-tree", "-u", "--reset", "HEAD"], ["remote", "add", "x", "/tmp/x"],
                     # second review: forced / ref-planting forms of the plain set, a read of a host file
                     ["branch", "-f", "main", "HEAD"], ["fetch", "--update-head-ok", "origin", "main:main"],
                     ["fetch", "origin", "+refs/heads/*:refs/heads/*"], ["push", "--force", "origin", "main"],
                     ["add", "--chmod=+x", "a.txt"], ["add", "--pathspec-from-file=/etc/hosts"],
                     ["diff", "--no-index", "/etc/hosts", "/dev/null"]):
            with self.assertRaisesRegex(ValueError, "declined", msg=argv):
                self.m.tool(s, "git_run", {"project": "mingtarget", "args": argv})
            self.assertEqual(kinds[-1], "write_exec", argv)
        cfg = subprocess.run(["git", "-C", target, "config", "--get", "core.fsmonitor"], capture_output=True, text=True)
        self.assertEqual(cfg.stdout, "")
        subprocess.run(["git", "-C", target, "status"], capture_output=True, env=env)
        self.assertFalse(os.path.exists(marker))
        self.m.tool(s, "git_run", {"project": "mingtarget", "args": ["log", "-1", "--format=%s"]})
        self.assertEqual(kinds[-1], "git_run_project")   # a read stays as the tree has it
        for argv in (["worktree", "list"], ["tag", "ming-t"], ["commit", "-m", "+1", "--allow-empty"],
                     ["branch", "-v"]):   # a read the writers list carded; plain tag / commit / branch
            self.m.tool(s, "git_run", {"project": "mingtarget", "args": argv})
            self.assertEqual(kinds[-1], "git_run_project", argv)
        self.policy({"security": {}})                    # plain hole: write_guard off, no write_exec card
        for argv in (["config", "user.name", "T"], ["remote", "add", "y", "/tmp/y"], ["reflog", "expire", "--all"]):
            self.m.tool(s, "git_run", {"project": "mingtarget", "args": argv})
            self.assertEqual(kinds[-1], "git_run_project", argv)   # never the own-project git_run kind

    def test_project_agent_image_blocks_the_start(self):
        root = self.project("minkeys", {"src/a.txt": "a\n", ".hole/settings.json": '{"container": {"baseImage": "x"}}'})
        s = self.session(root, "minkeys")
        self.policy(None)
        base, _ = self.m._base_settings(s)
        r = self.m._preflight(s, base)
        self.assertEqual([(b["kind"], b["detail"]) for b in r["blocks"]], [("forbidden-key", "container.baseImage")])
        self.policy({"security": {}})
        self.assertTrue(self.m._preflight(s, self.m._base_settings(s)[0])["ok"])

    def test_stored_old_profiles_through_settings_and_the_manager(self):
        def field():
            sec = next(x for x in settings.describe()["sections"] if x["id"] == "security")
            f = next(x for x in sec["fields"] if x["key"] == "security")
            return {i for i, v in f["value"].items() if v}, f
        cases = ((security.LEGACY_MINIMAL, security.V2_MINIMAL_ON, "hole"),
                 (security.LEGACY_RECOMMENDED_KEY, security.V2_RECOMMENDED_ON, "recommended"))
        for old, now, preset in cases:
            self.assertEqual(len(old), {"hole": 9, "recommended": 30}[preset])
            self.policy({"security": {i: True for i in old}})
            on, f = field()
            self.assertEqual(on, now)
            self.assertNotEqual(on, set(f["presets"][preset]))
            self.assertEqual(f["profile"], "custom")
            self.assertEqual(len(on), {"hole": 17, "recommended": 31}[preset])
            self.assertIs(self.m.policy["git_fail_closed"], True)   # the Manager's effective policy too
            self.assertIs(gitops_closed(), True)
        # saving the Minimal card stores today's 17
        settings.update("security", {"security": security.presets()["hole"]})
        with open(common.POLICY_FILE) as fh:
            self.assertEqual(set(json.load(fh)["security"]), security.MINIMAL_ON)

    def test_a_saved_choice_equal_to_an_old_profile_stays_as_chosen(self):
        # Minimal with every new guard switched off again = the old 9 set, saved from today's Settings
        self.policy({})
        for old in (security.LEGACY_MINIMAL, security.LEGACY_RECOMMENDED_KEY):
            settings.update("security", {"security": {i: True for i in old}})
            with open(common.POLICY_FILE) as fh:
                saved = json.load(fh)
            self.assertEqual((set(saved["security"]), saved["security_rev"]), (set(old), security.REV))
            self.assertEqual({i for i, v in security.state(saved).items() if v}, set(old))
            self.m.reload_policy()
            self.assertEqual(set(self.m.policy["security"]), set(old))
        # an old stored profile without the marker: saving it unchanged confirms it as chosen
        self.policy({"security": {i: True for i in security.LEGACY_MINIMAL}})
        settings.update("security", {"security": {i: True for i in security.LEGACY_MINIMAL}})
        with open(common.POLICY_FILE) as fh:
            self.assertEqual(json.load(fh).get("security_rev"), security.REV)
        # reset = the first-use default again, without the marker
        settings.update("security", {}, reset=["security"])
        with open(common.POLICY_FILE) as fh:
            saved = json.load(fh)
        self.assertNotIn("security", saved)
        self.assertNotIn("security_rev", saved)

    def test_an_existing_custom_choice_is_not_altered(self):
        # the user's own choice before 2026-10-16: old Minimal + strip_secrets and two GitLab cards (12)
        mine = security.LEGACY_MINIMAL | {"strip_secrets", "ask.gitlab_comment", "ask.gitlab_mr_merge"}
        self.assertEqual(len(mine), 12)
        self.policy({"security": {i: True for i in mine}})
        self.assertEqual(set(self.m.policy["security"]), mine)
        for k in ("protected_mounts", "git_fail_closed", "write_guard", "stack_scope_project", "piper_port_blocked"):
            self.assertIs(self.m.policy[k], False, k)
        self.assertEqual(self.m.policy["forbidden_project_keys"], [])
        self.assertIs(gitops_closed(), False)
        sec = next(x for x in settings.describe()["sections"] if x["id"] == "security")
        f = next(x for x in sec["fields"] if x["key"] == "security")
        self.assertEqual({i for i, v in f["value"].items() if v}, mine)


def gitops_closed():
    import gitops
    return gitops.FAIL_CLOSED


def shutil_rmtree(p):
    import shutil
    shutil.rmtree(p, True)


class UpgradeResumeTest(unittest.TestCase):
    """An upgrade from master (per-session ~/.claude / ~/.codex) to the tree's Minimal (claude_dir / codex_dir host):
    Continue of an existing conversation kept its own dir - `--resume` against ~/.claude found nothing and the
    second Continue reported the conversation lost (merge review R3, 2026-10-17)."""
    SID = "11111111-2222-3333-4444-555555555555"

    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, background=True)

    def setUp(self):
        security.ENABLED = True
        with open(sessions.PERSONAL_SETTINGS, "rb") as f:   # _env's personal include of ~/.claude: a
            self._personal = f.read()                        # per-session start blocks it (as on master)
        with open(sessions.PERSONAL_SETTINGS, "w") as f:
            json.dump({}, f)
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            os.unlink(common.POLICY_FILE)
        except FileNotFoundError:
            pass
        with open(sessions.PERSONAL_SETTINGS, "wb") as f:
            f.write(self._personal)
        security.ENABLED = False
        self.m.reload_policy()

    def policy(self, doc):
        try:
            os.unlink(common.POLICY_FILE)
        except FileNotFoundError:
            pass
        if doc is not None:
            with open(common.POLICY_FILE, "w") as f:
                json.dump(doc, f)
        self.m.reload_policy()

    def start(self, s):
        argvs = []
        orig = self.m._argv
        self.m._argv = lambda s2, h: argvs.append(orig(s2, h)) or argvs[-1]
        try:
            t = threading.Thread(target=self.m._start, args=(s,), daemon=True)
            t.start()
            t.join(20)
        finally:
            self.m._argv = orig
        return argvs[-1] if argvs else []

    def claude(self, name, mode):
        root = _env.make_project(name, {"a.txt": "a\n"})
        _env.register(name, root)
        s = sessions.Session(kind="project", project=name, path=root, profile=None, agent="claude",
                             claude_mode=mode, state="exited", resume=self.SID, claude_session=self.SID)
        base = s.cfg if mode == "session" else os.path.join(common.REAL_HOME, ".claude")
        touch(base, "projects/x/%s.jsonl" % self.SID, '{"sessionId":"%s"}\n' % self.SID)
        self.m.sessions[s.id] = s
        s.save()
        return s

    def test_minimal_keeps_a_claude_conversation_in_its_session_dir(self):
        s = self.claude("upres1", "session")
        self.policy(None)   # no security key: Minimal, claude_dir forced to host
        self.assertEqual(self.m.claude_dir(), "host")
        argv = self.start(s)
        self.assertEqual(s.claude_mode, "session")
        self.assertIn(self.SID, argv, (s.state, s.detail, s.blocks))
        self.assertFalse(any(x.startswith(os.path.join(common.REAL_HOME, ".claude") + ":") for x in argv), argv)
        self.assertTrue(self.m._has_transcript(s, self.SID))   # retry() finds it: no "conversation lost"
        # a new session follows the switch
        n = sessions.Session(kind="project", project="upres1", path=s.path, profile=None, agent="claude", state="exited")
        self.m.sessions[n.id] = n
        self.start(n)
        self.assertEqual(n.claude_mode, "host")

    def test_a_stricter_switch_to_session_dirs_applies_at_once(self):
        s = self.claude("upres2", "host")
        self.policy({"security": {"claude_isolated": True, "codex_isolated": True}})
        self.assertEqual(self.m.claude_dir(), "session")
        argv = self.start(s)
        self.assertEqual(s.claude_mode, "session")
        # its transcript stays in ~/.claude: no `--resume` into the fresh per-session dir (Fable review of R3)
        self.assertIsNone(s.resume)
        self.assertNotIn("--resume", argv, (s.state, s.detail, s.blocks))

    def test_continue_after_a_stricter_switch_says_the_conversation_is_new(self):
        # second review of R3: retry() resumed by the old mode and answered "not lost", _start then dropped it
        for policy, lost in ((None, {}), ({"security": {"claude_isolated": True, "codex_isolated": True}}, {"lost": True})):
            s = self.claude("upres4", "host")
            self.policy(policy)
            with mock.patch.object(self.m, "_start"):
                self.assertEqual(self.m.retry(s.id), lost, policy)
            self.assertEqual(s.resume, None if lost else self.SID)

    def test_continue_keeps_a_pending_codex_fork_on_its_home(self):
        # third review: _start keeps a pending Codex fork on the home _copy_fork chose - retry() must not drop it
        root = _env.make_project("upres5", {"a.txt": "a\n"})
        _env.register("upres5", root)
        s = sessions.Session(kind="project", project="upres5", path=root, profile=None, agent="codex",
                             claude_mode="none", host_codex=True, state="exited", fork_pending=True,
                             fork_from={"conversation": self.SID})
        self.m.sessions[s.id] = s
        self.policy({"security": {"claude_isolated": True, "codex_isolated": True}})
        # Exercise actual validation: no native child ID/history exists yet. A blanket
        # validator mock also "validated" None and falsely pretended the child had history.
        raw = "\n".join(json.dumps(record) for record in [
            {"type": "session_meta", "payload": {"id": self.SID}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "Saved pending fork context"}]}}
        ]) + "\n"
        touch(s.codex_home, "sessions/2026/10/rollout-2026-10-15T10-00-00-%s.jsonl" % self.SID, raw)
        with mock.patch.object(self.m, "_start"):
            self.assertEqual(self.m.retry(s.id), {})
        self.assertEqual(s.resume, self.SID)
        self.assertTrue(s.fork_pending)
        self.assertTrue(s.host_codex)

    def test_minimal_keeps_a_codex_thread_in_its_own_codex_home(self):
        root = _env.make_project("upres3", {"a.txt": "a\n"})
        _env.register("upres3", root)
        s = sessions.Session(kind="project", project="upres3", path=root, profile=None, agent="codex",
                             claude_mode="none", host_codex=False, state="exited", resume=self.SID)
        touch(s.codex_dir, "sessions/2026/10/rollout-2026-10-15T10-00-00-%s.jsonl" % self.SID, '{"id":"%s"}\n' % self.SID)
        self.m.sessions[s.id] = s
        s.save()
        self.policy(None)
        self.assertEqual(self.m.codex_dir(), "host")
        self.start(s)
        self.assertFalse(s.host_codex)
        self.assertEqual(s.resume, self.SID)


class RequiredApprovalsTest(unittest.TestCase):
    REQUIRED = {"db_read_prod", "db_write_prod", "http_prod", "http_host"}
    RESTORE = REQUIRED | {"db_write_test", "http_test", "settings_change", "allow_domain"}

    def test_production_cannot_be_globally_auto_approved(self):
        for pol in ({"ask": []}, {"auto_approve": list(self.REQUIRED)},
                    security.apply({"security": {}})):
            mgr = fake_mgr(pol)
            for kind in self.REQUIRED:
                with self.subTest(pol=pol, kind=kind):
                    self.assertFalse(mgr.auto_ok(kind))

    def test_tree_presets_and_validation_keep_required_cards(self):
        required = {"ask." + k for k in self.REQUIRED}
        self.assertTrue(required <= {k for k, v in security.state({"security": {}}).items() if v})
        self.assertTrue(required <= set(security.validate({})))
        for preset in security.presets().values():
            self.assertTrue(required <= set(preset))
        schema = {i["kind"]: i for g in security.schema() for sg in g.get("groups", []) for i in sg["items"]}
        self.assertEqual({k for k, i in schema.items() if i.get("required")}, self.REQUIRED)

    def test_startup_migrates_exact_profiles_once_and_preserves_other_choices(self):
        for original in ({}, {"security": {}},
                         {"security": {"ask.gitlab_comment": True, "ask.http_dev": False}, "security_rev": security.REV},
                         {"security": {k: True for k in security.LEGACY_MINIMAL}}):
            with self.subTest(original=original), tempfile.TemporaryDirectory() as root:
                path = os.path.join(root, "policy.json")
                doc = dict(original, future={"keep": 42})
                common.write_json(path, doc)
                expected = security.MINIMAL_ON if "security" not in doc or doc.get("security") == {k: True for k in security.LEGACY_MINIMAL} else \
                    {k for k, v in security.state(doc).items() if v}
                with mock.patch.object(common, "POLICY_FILE", path):
                    self.assertTrue(settings.migrate_security_approvals())
                    saved = common.read_json(path, {})
                    self.assertEqual(set(saved["security"]), expected)
                    self.assertEqual(saved["future"], {"keep": 42})
                    self.assertEqual(saved["security_rev"], security.REV)
                    saved["security"].pop("ask.http_test", None)
                    common.write_json(path, saved)
                    with open(path) as f:
                        before = f.read()
                    self.assertFalse(settings.migrate_security_approvals())
                    with open(path) as f:
                        self.assertEqual(f.read(), before)

    def test_ci_branch_list_applies_without_enabling_push_merge_block(self):
        path = policy_file({"protected_branches": ["main"], "security": {"ask.gitlab_job_protected": True}})
        self.addCleanup(os.unlink, path)
        with mock.patch.object(common, "POLICY_FILE", path), mock.patch.object(security, "ENABLED", True):
            sec = next(s for s in settings.describe()["sections"] if s["id"] == "git")
        field = next(f for f in sec["fields"] if f["key"] == "protected_branches")
        self.assertEqual(field["governed_by"], {"id": "ask.gitlab_job_protected", "label": "CI job on a protected branch", "on": True})

    def test_migration_refuses_broken_policy_without_overwriting(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "policy.json")
            for raw in ('{broken', '[]', '{"security": []}'):
                with open(path, "w") as f:
                    f.write(raw)
                with mock.patch.object(common, "POLICY_FILE", path):
                    with self.assertRaises((settings.SettingsError, ValueError)):
                        settings.migrate_security_approvals()
                with open(path) as f:
                    self.assertEqual(f.read(), raw)


if __name__ == "__main__":
    unittest.main()
