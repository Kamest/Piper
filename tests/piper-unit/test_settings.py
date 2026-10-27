"""Settings: validation, atomic writes that keep
unknown keys, secret masking, the audit, the weakening confirmation and the piperd gate."""
import json
import os
import stat
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import piperd
import settings
from test_piperd import FakeManager, get, post


def read(p):
    with open(p) as f:
        return f.read()


def jload(p):
    return json.loads(read(p))


def audit_events(kind="settings-change"):
    try:
        with open(common.AUDIT_FILE) as f:
            return [json.loads(x) for x in f if kind in x]
    except FileNotFoundError:
        return []


class SettingsTest(unittest.TestCase):
    def setUp(self):
        for f in (common.POLICY_FILE, common.PROJECTS_FILE, common.GITLAB_TOKEN_FILE):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        open(common.AUDIT_FILE, "w").close()

    def sec(self, sid):
        return next(s for s in settings.describe()["sections"] if s["id"] == sid)

    def field(self, sid, key):
        return next(f for f in self.sec(sid)["fields"] if f["key"] == key)

    def test_root_allow_preserves_other_roots_and_unknown_settings_and_is_idempotent(self):
        root = os.path.realpath(_env.make_project("allowsettings", {"src/a": "x"}))
        alias = root + "/."
        common.write_json(common.POLICY_FILE, {"future": {"keep": True},
                          "library_allow": {alias: ["old.key"], "/other": ["other.key"]}})
        changed = []
        self.assertEqual(settings.add_root_allow(root, ["new.key", "new.key"], on_change=changed.append), ["new.key"])
        self.assertEqual(settings.add_root_allow(root, ["new.key"], on_change=changed.append), [])
        self.assertEqual(jload(common.POLICY_FILE), {"future": {"keep": True},
                         "library_allow": {alias: ["old.key", "new.key"], "/other": ["other.key"]}})
        self.assertEqual(changed, [{"policy"}])
        self.assertEqual(len(audit_events()), 1)

    def test_host_link_browser_choice(self):
        self.assertEqual(self.field("interface", "host_link_browser")["value"], "default")
        settings.update("interface", {"host_link_browser": "chrome"})
        self.assertEqual(jload(common.POLICY_FILE)["host_link_browser"], "chrome")
        with self.assertRaises(ValueError):
            settings.update("interface", {"host_link_browser": "/tmp/browser"})

    def test_defaults_without_files(self):
        f = self.field("sandbox", "claude_dir")
        self.assertEqual(f["value"], "session")
        self.assertTrue(f["is_default"])
        self.assertTrue(f["security"])
        self.assertEqual(self.field("scan", "candidates")["value"], settings.guard.DEFAULT_POLICY["candidates"])

    def test_http_timeouts_are_editable_without_overwriting_other_policy(self):
        common.write_json(common.POLICY_FILE, {"http": {"insecure": ["dev"]}, "future": True})
        defaults = {"timeout": 180, "connect_timeout_ms": 5000, "socket_timeout_ms": 60000}
        for key, value in defaults.items():
            self.assertEqual(self.field("http", key)["value"], value)
        settings.update("http", {"timeout": 240, "connect_timeout_ms": 12000, "socket_timeout_ms": 90000})
        self.assertEqual(jload(common.POLICY_FILE), {"http": {"insecure": ["dev"], "timeout": 240,
                         "connect_timeout_ms": 12000, "socket_timeout_ms": 90000}, "future": True})
        for key, maximum in (("timeout", 3600), ("connect_timeout_ms", 3600000), ("socket_timeout_ms", 3600000)):
            for bad in (0, maximum + 1, True, 1.5):
                with self.subTest(key=key, bad=bad), self.assertRaises(settings.SettingsError):
                    settings.update("http", {key: bad})

    def test_db_gui_timeout_and_feed_texts_describe_what_they_do(self):
        """L7 + G14 (2026-10-07): the Database view has its own timeout, stored where dbtools reads it;
        the Feed texts describe the queue + chat drawer, and wait_min is gone."""
        import dbtools
        self.addCleanup(lambda: os.path.exists(dbtools.CONFIG_FILE) and os.unlink(dbtools.CONFIG_FILE))
        settings.update("databases", {"gui_timeout_s": 900})
        self.assertEqual(dbtools.DbTools.db_config(None)["gui_timeout_s"], 900)
        self.assertIn("Database view", self.field("databases", "gui_timeout_s")["label"])
        self.assertIn("agents' limits", self.field("databases", "gui_timeout_s")["help"])
        self.assertIn("agent", self.field("databases", "statement_timeout_s")["label"])
        feed = self.sec("feed")
        self.assertNotIn("panel on the right", feed["help"])
        self.assertIn("queue", feed["help"])
        self.assertNotIn("wait_min", [f["key"] for f in feed["fields"]])   # removed 2026-10-08

    def test_security_speaks_of_the_agent(self):
        """9.152: the Security section is about Claude Code and Codex sessions alike."""
        self.assertTrue(self.sec("security")["help"].startswith("What agents may do"))

    def test_feature_sections_write_the_files_the_features_read(self):
        import mattermost
        import prodcheck
        import timesheet
        import features
        features.save(preset_name="all")
        ids = {s["id"] for s in settings.describe()["sections"]}
        self.assertTrue({"timesheet", "prodcheck", "mattermost"} <= ids)
        self.assertNotIn("issuestate", ids)   # 9.12 D has nothing to set
        settings.update("timesheet", {"gap_min": 20, "pad_min": 0})   # D10: `enabled` = the module
        self.assertEqual(timesheet.load_config(), {"enabled": True, "gap_min": 20, "pad_min": 0})
        settings.update("prodcheck", {"time": "08:15", "days": [1, 2, 3]})
        self.assertEqual(prodcheck.validate_config(jload(prodcheck.CONFIG_FILE)),
                         {"enabled": True, "time": "08:15", "days": [1, 2, 3]})
        settings.update("mattermost", {"detail": "none", "quiet_from": "19:00", "quiet_to": "07:00", "quiet_weekend": True})
        cfg = mattermost.load_config()
        self.assertEqual((cfg["detail"], cfg["quiet"]), ("none", {"from": "19:00", "to": "07:00", "weekend": True}))
        self.assertEqual(mattermost.validate(jload(mattermost.CONFIG_FILE)), [])

    def test_a_section_validator_sees_the_stored_values_too(self):
        """The GUI sends only changed fields: quiet.to alone must validate against the stored quiet.from."""
        import mattermost
        settings.update("mattermost", {"quiet_from": "20:00", "quiet_to": "07:30"})
        settings.update("mattermost", {"quiet_to": "08:00"})   # only this one changed
        self.assertEqual(mattermost.load_config()["quiet"]["to"], "08:00")
        with self.assertRaises(settings.SettingsError) as cm:   # clearing only one half: refused, not stored
            settings.update("mattermost", {"quiet_to": ""})
        self.assertIn("quiet_from", cm.exception.errors)
        self.assertEqual(mattermost.load_config()["quiet"], {"from": "20:00", "to": "08:00", "weekend": False})
        settings.update("mattermost", {"quiet_from": "", "quiet_to": ""})   # both: off
        self.assertEqual(mattermost.load_config()["quiet"]["from"], "")

    def test_f22_mattermost_help_says_when_it_sends(self):
        """F22: the section says it sends only while Piper is not on screen; Send test stays an action"""
        sec = settings.section("mattermost")
        self.assertTrue(sec.help.startswith("Sends only while Piper is not on screen, never in quiet hours."))
        self.assertEqual([a[0] for a in sec.actions], ["mm-test"])
        self.assertEqual([f.key for f in sec.fields], ["webhook", "detail", "after_s", "min_gap_s", "quiet_from", "quiet_to",
                                                      "quiet_weekend", "channel", "username"])   # the GUI regroups, the fields stay

    def test_mattermost_webhook_must_be_an_https_hook_url(self):
        import mattermost
        for bad in ("http://mm.example.com/hooks/abc", "https://mm.example.com/api/v4/posts", "mm.example.com/hooks/abc"):
            with self.assertRaises(settings.SettingsError) as cm:
                settings.update("mattermost", {"webhook": bad})
            self.assertIn("webhook", cm.exception.errors)
        self.assertEqual(mattermost.webhook_url(), "")
        settings.update("mattermost", {"webhook": "https://mm.example.com/hooks/abc123"})
        self.assertEqual(mattermost.webhook_url(), "https://mm.example.com/hooks/abc123")
        settings.update("mattermost", {}, clear=["webhook"])
        self.assertEqual(mattermost.webhook_url(), "")

    def test_reset_of_a_field_without_a_default_is_a_400_not_a_crash(self):
        for sid, key in (("projects", "projects"), ("gitlab", "token")):
            with self.assertRaises(settings.SettingsError) as cm:
                settings.update(sid, {}, reset=[key])
            self.assertIn(key, cm.exception.errors)
        for sid, key in (("mattermost", "after_s"), ("projects", "projects")):   # clear = secrets only
            with self.assertRaises(settings.SettingsError) as cm:
                settings.update(sid, {}, clear=[key])
            self.assertIn(key, cm.exception.errors)

    def test_reset_with_the_default_in_values_is_one_change(self):
        with open(common.POLICY_FILE, "w") as f:
            json.dump({"max_depth": 50}, f)
        default = self.field("scan", "max_depth")["default"]
        with self.assertRaises(settings.NeedsConfirm) as cm:   # a lower depth weakens: one warning, not two
            settings.update("scan", {"max_depth": default}, reset=["max_depth", "max_depth"])
        self.assertEqual([w["key"] for w in cm.exception.items], ["max_depth"])
        r = settings.update("scan", {"max_depth": default}, reset=["max_depth"], confirm=["max_depth"])
        self.assertEqual(r["saved"], ["max_depth"])
        self.assertEqual(len([e for e in audit_events() if e.get("key") == "max_depth"]), 1)
        self.assertNotIn("max_depth", jload(common.POLICY_FILE))

    def test_saving_one_policy_section_keeps_the_others_rev(self):
        """sessions, scan and holehome share policy.json: a draft of one survives saving another."""
        scan_rev = self.sec("scan")["rev"]
        settings.update("sessions", {"idea_mcp_url": "http://127.0.0.1:64342/stream"})
        self.assertEqual(self.sec("scan")["rev"], scan_rev)
        with open(common.POLICY_FILE) as f:
            doc = json.load(f)
        doc["max_depth"] = 77   # a real change of a scan field (by hand, another window)
        with open(common.POLICY_FILE, "w") as f:
            json.dump(doc, f)
        self.assertNotEqual(self.sec("scan")["rev"], scan_rev)
        with self.assertRaises(settings.Conflict):
            settings.update("scan", {"max_entries": 3_000_000}, rev_seen=scan_rev)

    def test_a_policy_file_that_is_not_utf8_breaks_only_its_sections(self):
        with open(common.POLICY_FILE, "wb") as f:
            f.write(b'{"a": "\xff"}')
        secs = {s["id"]: s for s in settings.describe()["sections"]}   # no exception: the page still loads
        self.assertIn("UTF-8", secs["sessions"]["error"])
        self.assertEqual(secs["projects"]["error"], "")
        with self.assertRaises(settings.SettingsError):
            settings.update("sessions", {"idea_mcp_url": ""})
        with open(common.POLICY_FILE, "rb") as f:
            self.assertEqual(f.read(), b'{"a": "\xff"}')   # never overwritten

    def test_feature_validators_refuse_bad_values(self):
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("prodcheck", {"days": [0, 9]})
        self.assertIn("days", cm.exception.errors)
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("mattermost", {"quiet_from": "19:00", "quiet_to": ""})
        self.assertIn("quiet_from", cm.exception.errors)

    def test_weekdays_are_choices_and_quiet_hours_an_optional_time(self):
        """The GUI draws `days` as Mon–Sun toggles and quiet hours as time pickers that may stay empty."""
        import prodcheck
        days = self.field("prodcheck", "days")
        self.assertEqual([c[0] for c in days["choices"]], [1, 2, 3, 4, 5, 6, 7])
        settings.update("prodcheck", {"days": [6, 7]})
        self.assertEqual(prodcheck.validate_config(jload(prodcheck.CONFIG_FILE))["days"], [6, 7])
        for bad in ([8], []):   # not a weekday / never: refused, not stored
            with self.assertRaises(settings.SettingsError) as cm:
                settings.update("prodcheck", {"days": bad})
            self.assertIn("days", cm.exception.errors)
        with self.assertRaises(settings.SettingsError) as cm:   # the check's own time is required
            settings.update("prodcheck", {"time": ""})
        self.assertIn("time", cm.exception.errors)
        self.assertEqual(self.field("mattermost", "quiet_from")["type"], "time")
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("mattermost", {"quiet_from": "7:3", "quiet_to": "08:00"})
        self.assertIn("quiet_from", cm.exception.errors)

    def test_write_keeps_unknown_keys_order_and_indent(self):
        with open(common.POLICY_FILE, "w") as f:
            f.write('{\n    "zzz_custom": {"a": 1},\n    "gitlab_host": "gitlab.com"\n}\n')
        settings.update("sessions", {"idea_mcp_url": "http://127.0.0.1:64342/stream"})
        text = read(common.POLICY_FILE)
        data = json.loads(text)
        self.assertEqual(data["zzz_custom"], {"a": 1})
        self.assertEqual(list(data)[:2], ["zzz_custom", "gitlab_host"])
        self.assertIn('\n    "zzz_custom"', text)   # 4-space indent kept
        self.assertEqual(stat.S_IMODE(os.stat(common.POLICY_FILE).st_mode), 0o600)
        self.assertFalse([n for n in os.listdir(common.PIPER_HOME) if ".tmp." in n])

    def test_validation_errors_per_field(self):
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("scan", {"max_depth": "x", "candidates": ["ok/**", "bad pattern"]})
        self.assertIn("max_depth", cm.exception.errors)
        self.assertIn("candidates", cm.exception.errors)
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("sessions", {"idea_mcp_url": "http://example.com/stream"})
        self.assertIn("loopback", cm.exception.errors["idea_mcp_url"])
        with self.assertRaises(settings.SettingsError):
            settings.update("env", {"port": "1"})   # read-only
        self.assertFalse(os.path.exists(common.POLICY_FILE))

    def test_weakening_needs_confirmation(self):
        with self.assertRaises(settings.NeedsConfirm) as cm:
            settings.update("sandbox", {"claude_dir": "host", "unrestricted_network": True})
        keys = {i["key"] for i in cm.exception.items}
        self.assertEqual(keys, {"claude_dir", "unrestricted_network"})
        self.assertIn("K2", next(i["warning"] for i in cm.exception.items if i["key"] == "claude_dir"))
        with self.assertRaises(settings.NeedsConfirm) as cx:
            settings.update("sandbox", {"codex_dir": "host"})
        self.assertIn("rotates your host login", cx.exception.items[0]["warning"])
        self.assertEqual(self.field("sandbox", "codex_dir")["value"], "session")
        self.assertFalse(os.path.exists(common.POLICY_FILE))   # nothing written
        settings.update("sandbox", {"claude_dir": "host", "unrestricted_network": True},
                        confirm=["claude_dir", "unrestricted_network"])
        self.assertEqual(jload(common.POLICY_FILE)["claude_dir"], "host")
        ev = [e for e in audit_events() if e["key"] == "claude_dir"][-1]
        self.assertEqual((ev["old"], ev["new"], ev["confirmed"]), ("session", "host", True))

    def test_start_opts_default(self):
        common.write_json(common.POLICY_FILE, {"start_opts_default": ["docker", "idea"]})
        self.assertNotIn("start_opts_default", {f["key"] for f in self.sec("sessions")["fields"]})
        with self.assertRaises(settings.SettingsError):
            settings.update("sessions", {"start_opts_default": ["docker"]})
        settings.update("sessions", {"language": "Français"})
        self.assertEqual(jload(common.POLICY_FILE)["start_opts_default"], ["docker", "idea"])

    def test_strengthening_needs_no_confirmation(self):
        more = settings.guard.DEFAULT_POLICY["candidates"] + ["**/secret.txt"]
        settings.update("scan", {"candidates": more})
        with self.assertRaises(settings.NeedsConfirm):   # removing one again weakens
            settings.update("scan", {"candidates": more[:-2]})
        with self.assertRaises(settings.NeedsConfirm):
            settings.update("scan", {"max_depth": 5})
        settings.update("scan", {"max_depth": 60})

    def test_host_hardening_off_and_widening_keys_on_need_confirmation(self):
        """2026-10-06: git_fail_closed / write_guard / stack_scope_project are on by default - turning one off
        weakens; every key that widens a session weakens when turned on, never when turned off"""
        home = {"git_fail_closed": "git", "write_guard": "sessions", "stack_scope_project": "sandbox",   # W14 sections
                "git_run_projects": "git", "gitlab_extra_tools": "gitlab", "settings_auto_tighten": "sandbox",
                "login_spread": "sandbox", "codex_in_claude": "sessions", "open_links_on_host": "sessions"}   # F26: Approvals merged into New sessions
        for k in ("git_fail_closed", "write_guard", "stack_scope_project"):
            self.assertIs(settings.guard.DEFAULT_POLICY[k], True, k)
            with self.assertRaises(settings.NeedsConfirm, msg=k):
                settings.update(home[k], {k: False})
        for k in ("git_run_projects", "gitlab_extra_tools", "settings_auto_tighten", "login_spread",
                  "codex_in_claude", "open_links_on_host"):
            with self.assertRaises(settings.NeedsConfirm, msg=k):
                settings.update(home[k], {k: True})
            settings.update(home[k], {k: True}, confirm=[k])
            settings.update(home[k], {k: False})
        settings.update("git", {"git_fail_closed": False}, confirm=["git_fail_closed"])
        settings.update("git", {"git_fail_closed": True})
        self.assertIs(jload(common.POLICY_FILE)["git_fail_closed"], True)

    def test_w14_tree_groups_in_order(self):
        """W14: the tree by meaning, IntelliJ-like; sections come grouped in GROUPS order"""
        d = settings.describe()
        self.assertEqual([g for g, _ in d["groups"]], ["general", "sessions", "vcs", "tools", "advanced"])
        self.assertEqual(dict(d["groups"])["vcs"], "Version Control")
        order = [g for g, _ in d["groups"]]
        seen = [order.index(s["group"]) for s in d["sections"]]
        self.assertEqual(seen, sorted(seen), "a group's sections stay together")
        by = {}
        for s in d["sections"]:
            by.setdefault(s["group"], []).append(s["id"])
        self.assertEqual(by["general"][:5], ["features", "interface", "terminal", "feed", "projects"])   # Settings A: project discovery belongs to the workspace
        self.assertEqual(by["sessions"], ["sessions", "security", "sandbox"])   # F26 (changes 9.46): no Approvals section
        self.assertEqual(by["vcs"], ["git", "gitlab", "github", "automatika", "review"])   # W23 github
        self.assertEqual(by["tools"], ["databases", "timesheet", "prodcheck", "mattermost", "http"])
        self.assertEqual(by["advanced"], ["scan", "holehome", "env"])
        self.assertEqual(self.sec("sessions")["title"], "New sessions")
        self.assertEqual(self.sec("review")["title"], "Changes & review")

    def test_w14_every_field_once_and_the_moves(self):
        keys = [(s.id, f.key) for s in settings.REGISTRY for f in s.fields
                if s.id not in ("features", "projects", "env")]   # generated / per-module / env names
        policy = [k for sid, k in keys if next(f for f in settings.section(sid).fields if f.key == k).store == "policy"]
        self.assertEqual(len(policy), len(set(policy)), "a policy key in two sections")
        home = {k: sid for sid, k in keys}
        self.assertEqual([f.key for f in settings.section("sessions").fields][-4:],
                         ["auto_approve", "write_guard", "open_links_on_host", "clipboard_arm_enter"],
                         "F26: the former Approvals fields close New sessions")
        with self.assertRaises(KeyError):
            settings.section("approvals")
        for k in ("nav_history", "restore_view", "term_click_opens_files"):
            self.assertEqual(home[k], "interface", k)
        self.assertFalse(any(f["security"] for f in self.sec("interface")["fields"]), "Interface never weakens the isolation")
        for k in ("git_mode", "protected_branches", "mr_target_protected", "git_fail_closed", "git_run_projects",
                  "git_run_readonly_extra", "git_run_output_in_repo", "git_host_hint", "commit_requires_review"):
            self.assertEqual(home[k], "git", k)
        for k in ("claude_dir", "codex_dir", "unrestricted_network", "stack_scope_project", "settings_auto_tighten",
                  "login_spread", "l5_action"):
            self.assertEqual(home[k], "sandbox", k)
        for k, sid in (("feed_auto_actions", "feed"), ("gitlab_extra_tools", "gitlab"),
                       ("default_unlocks", "sessions"), ("codex_in_claude", "sessions")):
            self.assertEqual(home[k], sid, k)

    def test_w14_interface_help_fits_one_line(self):
        """a short line under each Interface option (settings.ts folds help over 110 chars under "more")"""
        for f in settings.section("interface").fields:
            self.assertLessEqual(len(f.help), 110, f.key)

    def test_f19_installation_check_is_an_isolation_check_action(self):
        """F19: Features = presets + modules only; "Check the installation…" moved to Isolation check"""
        self.assertIn("open-setup", [a[0] for a in settings.section("scan").actions])
        self.assertNotIn("open-setup", [a[0] for a in settings.section("features").actions])
        self.assertTrue(all(a[0].startswith("preset-") for a in settings.section("features").actions))

    def test_w14_moved_fields_save_in_their_new_section(self):
        settings.update("interface", {"term_click_opens_files": False})
        settings.update("feed", {"feed_auto_actions": True})   # feed.validate must not see the policy key
        settings.update("git", {"protected_branches": ["main"]})
        p = jload(common.POLICY_FILE)
        self.assertIs(p["term_click_opens_files"], False)
        self.assertIs(p["feed_auto_actions"], True)
        self.assertEqual(p["protected_branches"], ["main"])
        with self.assertRaises(settings.SettingsError):
            settings.update("sessions", {"nav_history": False})   # the old section no longer has it

    def test_w14_agent_preview_names_the_new_section(self):
        with self.assertRaises(settings.SettingsError) as e:
            settings.agent_preview("sessions", {"git_mode": "hole"})
        self.assertEqual(e.exception.errors["git_mode"], "this field is in section git now")
        with self.assertRaises(settings.SettingsError) as e:
            settings.agent_preview("sessions", {"no_such_key": 1})
        self.assertEqual(e.exception.errors["no_such_key"], "a session cannot change this field")
        self.assertEqual([r["key"] for r in settings.agent_preview("git", {"git_host_hint": True})], ["git_host_hint"])

    def test_approvals(self):
        """Settings -> Approvals: commit and push run without a card by default (2026-10-04);
        asking again is a strengthening, skipping another card needs confirmation"""
        f = [x for sec in settings.describe()["sections"] if sec["id"] == "sessions" for x in sec["fields"] if x["key"] == "auto_approve"][0]
        self.assertEqual(f["value"], ["git_commit", "git_push"])
        self.assertTrue(set(f["value"]) <= {c[0] for c in f["choices"]})
        settings.update("sessions", {"auto_approve": ["git_commit"]})   # push asks again: no confirmation
        self.assertEqual(jload(common.POLICY_FILE)["auto_approve"], ["git_commit"])
        with self.assertRaises(settings.NeedsConfirm):
            settings.update("sessions", {"auto_approve": ["git_commit", "git_run"]})
        with self.assertRaises(ValueError):
            settings.update("sessions", {"auto_approve": ["rm_rf"]}, confirm=["auto_approve"])
        settings.update("sessions", {}, reset=["auto_approve"], confirm=["auto_approve"])
        self.assertNotIn("auto_approve", jload(common.POLICY_FILE))

    def test_pre_approving_the_gitlab_upload_is_a_named_weakening(self):
        """R4a (2026-10-10, the user's decision): auto-approving gitlab_upload_file sends a visible project
        file to GitLab without a card - the "Weakening the isolation" confirmation names that risk"""
        with self.assertRaises(settings.NeedsConfirm) as cm:
            settings.update("sessions", {"auto_approve": ["git_commit", "git_push", "gitlab_upload_file"]})
        warning = cm.exception.items[0]["warning"]
        self.assertIn("gitlab_upload_file", warning)
        self.assertIn("leaves for GitLab without a card", warning)
        self.assertFalse(os.path.exists(common.POLICY_FILE) and "auto_approve" in jload(common.POLICY_FILE))
        with self.assertRaises(settings.NeedsConfirm) as cm:   # another kind keeps the generic text
            settings.update("sessions", {"auto_approve": ["git_commit", "git_push", "gitlab_comment"]})
        self.assertNotIn("GitLab without a card", cm.exception.items[0]["warning"])
        settings.update("sessions", {"auto_approve": ["git_commit", "git_push", "gitlab_upload_file"]},
                        confirm=["auto_approve"])   # confirmed: works as before
        self.assertIn("gitlab_upload_file", jload(common.POLICY_FILE)["auto_approve"])

    def test_max_sessions_is_editable_with_the_env_as_its_default(self):
        """2026-10-12: the session limit was only PIPER_MAX_SESSIONS (a service reinstall) - now a setting."""
        f = self.field("sessions", "max_sessions")
        self.assertEqual((f["type"], f["value"], f["is_default"]), ("int", settings._env_max_sessions(), True))
        settings.update("sessions", {"max_sessions": 10})
        self.assertEqual(jload(common.POLICY_FILE)["max_sessions"], 10)
        for bad in (0, 51, "x"):
            with self.assertRaises(settings.SettingsError):
                settings.update("sessions", {"max_sessions": bad})
        settings.update("sessions", {}, reset=["max_sessions"])
        self.assertNotIn("max_sessions", jload(common.POLICY_FILE))   # back to the env value
        from unittest import mock
        with mock.patch.dict(os.environ, {"PIPER_MAX_SESSIONS": "9"}):
            self.assertEqual(settings._env_max_sessions(), 9)
        with mock.patch.dict(os.environ, {"PIPER_MAX_SESSIONS": "lots"}):
            self.assertEqual(settings._env_max_sessions(), 6)

    def test_reset_removes_the_key(self):
        settings.update("sessions", {"idea_mcp_url": "http://localhost:1/stream"})
        settings.update("sessions", {}, reset=["idea_mcp_url"])
        self.assertNotIn("idea_mcp_url", jload(common.POLICY_FILE))

    def test_holehome_nested_path(self):
        settings.update("holehome", {"force_exclude": [".git", "secrets"]})
        self.assertEqual(jload(common.POLICY_FILE)["holehome"], {"force_exclude": [".git", "secrets"]})
        settings.update("holehome", {}, reset=["force_exclude"], confirm=["force_exclude"])   # dropping "secrets" weakens
        self.assertNotIn("holehome", jload(common.POLICY_FILE))

    def test_secret_masked_written_0600_and_never_audited(self):
        tok = "glpat-abcdefghijklmnop1234"
        settings.update("gitlab", {"token": tok})
        self.assertEqual(read(common.GITLAB_TOKEN_FILE), tok)
        self.assertEqual(stat.S_IMODE(os.stat(common.GITLAB_TOKEN_FILE).st_mode), 0o600)
        f = self.field("gitlab", "token")
        self.assertEqual((f["set"], f["hint"]), (True, "…1234"))
        self.assertNotIn(tok, json.dumps(settings.describe()))
        self.assertNotIn(tok, read(common.AUDIT_FILE))
        self.assertEqual([e.get("secret") for e in audit_events()], ["set"])
        settings.update("gitlab", {"token": ""})   # empty = unchanged
        self.assertTrue(os.path.exists(common.GITLAB_TOKEN_FILE))
        settings.update("gitlab", {}, clear=["token"])
        self.assertFalse(os.path.exists(common.GITLAB_TOKEN_FILE))
        self.assertFalse(self.field("gitlab", "token")["set"])

    def test_rev_conflict(self):
        r = self.sec("sessions")["rev"]
        with open(common.POLICY_FILE, "w") as f:
            f.write('{"language": "Français"}\n')   # edited by hand meanwhile
        with self.assertRaises(settings.Conflict):
            settings.update("sessions", {"idea_mcp_url": ""}, rev_seen=r)

    def test_broken_json_is_not_overwritten(self):
        with open(common.POLICY_FILE, "w") as f:
            f.write("{broken")
        with self.assertRaises(settings.SettingsError):
            settings.update("sessions", {"idea_mcp_url": ""})
        self.assertEqual(read(common.POLICY_FILE), "{broken")
        self.assertIn("JSON", self.sec("sessions")["error"])

    def test_projects_editor_keeps_unknown_entry_keys(self):
        root = _env.make_project("settingsproj", {"README": "x"})
        _env.register("keep", root, obs="shop", gitlab=["a/b"])
        rows = self.field("projects", "projects")["value"]
        self.assertEqual(rows[0]["gitlab"], ["a/b"])
        self.assertEqual((rows[0]["obs"], rows[0]["explicit"]), ("shop", True))
        rows[0]["gitlab"] = "a/b, c/d"
        rows.append({"key": "new/one", "path": root, "gitlab": [], "profile": "", "title": "", "explicit": True})
        settings.update("projects", {"projects": rows, "globs": ["~/development/*"]})
        data = jload(common.PROJECTS_FILE)
        self.assertEqual(data["obs"]["keep"], "shop")               # an entry's own obs moves to the top-level map
        self.assertNotIn("obs", data["projects"]["keep"])
        self.assertEqual(data["projects"]["keep"]["gitlab"], ["a/b", "c/d"])
        self.assertEqual(data["projects"]["new/one"], {"path": root})
        self.assertEqual(data["globs"], ["~/development/*"])
        with self.assertRaises(settings.SettingsError):
            settings.update("projects", {"projects": [{"key": "x", "path": common.PIPER_HOME}]})
        with self.assertRaises(settings.SettingsError):
            settings.update("projects", {"projects": [{"key": "bad key", "path": root}]})

    def test_a_gone_checkout_does_not_block_saving_and_a_rename_keeps_the_entry(self):
        root = _env.make_project("settingsren", {"README": "x"})
        with open(common.PROJECTS_FILE, "w") as f:
            json.dump({"projects": {"old": {"path": root, "extra": 1}, "gone": {"path": "/nonexistent/gone"}},
                       "obs": {"old": "shop"}}, f)
        rows = self.field("projects", "projects")["value"]
        row = next(r for r in rows if r["key"] == "old")
        row["key"] = "new"                                     # renamed in the table
        settings.update("projects", {"projects": rows})        # "gone" is untouched: no "does not exist" error
        data = jload(common.PROJECTS_FILE)
        self.assertEqual(data["projects"]["new"], {"path": root, "extra": 1})
        self.assertNotIn("old", data["projects"])
        self.assertEqual(data["obs"], {"new": "shop"})
        self.assertIn("gone", data["projects"])
        rows = self.field("projects", "projects")["value"]
        next(r for r in rows if r["key"] == "gone")["path"] = "/nonexistent/other"
        with self.assertRaises(settings.SettingsError) as cm:  # a changed path is still checked
            settings.update("projects", {"projects": rows})
        self.assertIn("does not exist", str(cm.exception.errors))

    def found_checkouts(self, *names):
        """Git checkouts under PROJECT_PATH that projects.json does not list (discovery cache reset)."""
        import projects as projects_mod
        for n in names:
            os.makedirs(os.path.join(_env.make_project(n, {"README": "x"}), ".git"), exist_ok=True)
        projects_mod._DISCOVERED["at"] = 0.0
        self.addCleanup(lambda: projects_mod._DISCOVERED.__setitem__("at", 0.0))

    def test_one_table_found_checkouts_get_gitlab_and_observability_in_place(self):
        """A found checkout (not in projects.json) is edited in its row: GitLab makes it an entry,
        observability goes to the top-level obs map, an untouched one writes nothing."""
        self.found_checkouts("found-shop", "found-other")
        found = next(r for r in self.field("projects", "projects")["value"] if r["key"].endswith("found-shop"))
        self.assertFalse(found["explicit"])
        rows = self.field("projects", "projects")["value"]
        row = next(r for r in rows if r["key"] == found["key"])
        row["gitlab"], row["obs"] = ["grp/acme.shop"], "shop"
        settings.update("projects", {"projects": rows})
        data = jload(common.PROJECTS_FILE)
        self.assertEqual(data["projects"][found["key"]], {"path": found["path"], "gitlab": ["grp/acme.shop"]})
        self.assertEqual(data["obs"][found["key"]], "shop")
        self.assertEqual([k for k in data["projects"] if k != found["key"]], [])   # the untouched found ones: nothing
        rows = self.field("projects", "projects")["value"]
        row = next(r for r in rows if r["key"] == found["key"])
        self.assertEqual((row["explicit"], row["obs"]), (True, "shop"))
        row["obs"] = ""                                                            # unmap
        settings.update("projects", {"projects": rows})
        self.assertNotIn(found["key"], jload(common.PROJECTS_FILE)["obs"])
        with self.assertRaises(settings.SettingsError):
            row["obs"] = "bad;name"
            settings.update("projects", {"projects": rows})

    def test_an_obs_mapping_saved_meanwhile_is_a_conflict_not_lost(self):
        """The table's rev covers the top-level `obs` map too: a mapping saved by another window
        (or POST /api/prod/mapping, or by hand) after this page loaded must not be erased by the
        stale row's empty obs."""
        root = _env.make_project("revobs", {"README": "x"})
        _env.register("ra", root)
        _env.register("rb", root)
        sec = self.sec("projects")
        rows, seen = sec["fields"][0]["value"], sec["rev"]
        data = jload(common.PROJECTS_FILE)
        data.setdefault("obs", {})["rb"] = "rb-prod"
        common.write_json(common.PROJECTS_FILE, data)
        next(r for r in rows if r["key"] == "ra")["gitlab"] = ["g/ra"]
        with self.assertRaises(settings.Conflict):
            settings.update("projects", {"projects": rows}, rev_seen=seen)
        self.assertEqual(jload(common.PROJECTS_FILE)["obs"]["rb"], "rb-prod")

    def test_suggestions_skip_dont_map_and_mapped_projects(self):
        import prodcheck
        self.found_checkouts("sugg-a", "sugg-b")
        rows = self.field("projects", "projects")["value"]
        a = next(r["key"] for r in rows if r["key"].endswith("sugg-a"))
        b = next(r["key"] for r in rows if r["key"].endswith("sugg-b"))
        os.makedirs(prodcheck.OBS_DIR, exist_ok=True)
        common.write_json(prodcheck.SUGGEST_FILE, {"suggestions": [{"project": a, "obs": "x-a", "note": "n"},
                                                                  {"project": b, "obs": "x-b", "note": "n"}]})
        common.write_json(prodcheck.IGNORED_FILE, {"projects": [b]})
        self.addCleanup(lambda: [os.path.exists(f) and os.unlink(f) for f in (prodcheck.SUGGEST_FILE, prodcheck.IGNORED_FILE)])
        f = self.field("projects", "projects")
        self.assertEqual(list(f["suggestions"]), [a])     # b: "Don't map"
        self.assertEqual(f["ignored"], [b])

    def test_on_change_reports_stores(self):
        seen = []
        settings.update("scan", {"max_depth": 50}, on_change=seen.append)
        self.assertEqual(seen, [{"policy"}])
        seen.clear()
        settings.update("scan", {"max_depth": 50}, on_change=seen.append)   # unchanged: no callback
        self.assertEqual(seen, [])


class ReloadManager(FakeManager):
    reloaded = 0

    def reload_policy(self):
        ReloadManager.reloaded += 1

    def gitlab_host(self):
        return "gitlab.example"


class SettingsHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        piperd.MANAGER = ReloadManager()
        cls.tok = common.mint_token("test")
        cls.limited = "limited-" + os.urandom(8).hex()
        with open(common.TOKENS_FILE, "a") as f:
            f.write("%s\tdashboard\tlimited\n" % cls.limited)

    def setUp(self):
        for f in (common.POLICY_FILE,):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass

    def test_get_needs_the_gui_scope(self):
        self.assertEqual(get("/api/settings")[0], 401)
        self.assertEqual(get("/api/settings", token=self.limited)[0], 403)
        code, _, body = get("/api/settings", token=self.tok)
        self.assertEqual(code, 200)
        self.assertIn("sessions", {s["id"] for s in json.loads(body)["sections"]})

    def test_post_confirm_flow_and_reload(self):
        self.assertEqual(post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"}}, token=self.limited)[0], 403)
        code, _, body = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"}}, token=self.tok)
        self.assertEqual(code, 409)
        self.assertEqual(json.loads(body)["needs_confirm"][0]["key"], "claude_dir")
        before = ReloadManager.reloaded
        code, _, body = post("/api/settings", {"section": "sandbox", "values": {"claude_dir": "host"},
                                               "confirm": ["claude_dir"]}, token=self.tok)
        self.assertEqual(code, 200, body)
        self.assertEqual(ReloadManager.reloaded, before + 1)
        self.assertIn("applies to newly started sessions", json.loads(body)["apply_text"][0])

    def test_post_errors(self):
        code, _, body = post("/api/settings", {"section": "scan", "values": {"max_depth": 0}}, token=self.tok)
        self.assertEqual(code, 400)
        self.assertIn("max_depth", json.loads(body)["errors"])
        self.assertEqual(post("/api/settings", {"section": "nope", "values": {}}, token=self.tok)[0], 404)
        self.assertEqual(post("/api/settings", {"section": "scan", "values": [], "reset": "x"}, token=self.tok)[0], 400)
        for k in ("reset", "clear", "confirm"):   # a non-string item (unhashable): 400, not a dropped connection
            self.assertEqual(post("/api/settings", {"section": "scan", "values": {}, k: [{"a": 1}]}, token=self.tok)[0], 400, k)

    def test_verify_gitlab_without_token(self):
        try:
            os.unlink(common.GITLAB_TOKEN_FILE)
        except FileNotFoundError:
            pass
        code, _, body = post("/api/settings/verify-gitlab", {}, token=self.tok)
        self.assertEqual(code, 200)
        self.assertFalse(json.loads(body)["ok"])


if __name__ == "__main__":
    unittest.main()
