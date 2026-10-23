"""Per-project start preferences: persistence, project identity and GUI-only HTTP writes."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import _env  # noqa: F401
import common
import piperd
import projects
import settings
from test_piperd import FakeManager, get, post


class ProjectDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.previous = common.read_json(common.PROJECTS_FILE, {"projects": {}})
        self.a = _env.make_project("start-a", {".git/HEAD": "ref: refs/heads/main"})
        self.b = _env.make_project("start-b", {".git/HEAD": "ref: refs/heads/main"})
        common.write_json(common.PROJECTS_FILE, {"projects": {"a": {"path": self.a}, "b": {"path": self.b}},
                                                "future": {"keep": True}})
        self.addCleanup(common.write_json, common.PROJECTS_FILE, self.previous)
        self.manager = FakeManager()
        self.patch = mock.patch.object(piperd, "MANAGER", self.manager)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.token = common.own_token("gui")

    def read(self):
        return common.read_json(common.PROJECTS_FILE, {})

    def response(self, project="a", agent="claude", opts=None):
        return {"id": "created", "project": project, "agent": agent, "opts": opts or {}}

    def test_separate_projects_empty_choice_and_preserving_other_data(self):
        projects.remember_session_defaults("a", "codex", {"docker": True, "idea": False})
        projects.remember_session_defaults("b", "claude", {})
        self.assertEqual(projects.session_defaults(), {
            "a": {"agent": "codex", "opts": ["docker"]}, "b": {"agent": "claude", "opts": []}})
        projects.remember_session_defaults("a", "claude", {})
        self.assertEqual(projects.session_defaults()["a"]["opts"], [])
        self.assertEqual(self.read()["future"], {"keep": True})
        self.assertEqual(set(self.read()["projects"]), {"a", "b"})

    def test_discovered_project_and_worktree_share_the_base_without_registering_it(self):
        _env.make_project("start-discovered", {".git/HEAD": "ref: refs/heads/main"})
        projects._DISCOVERED["at"] = 0
        projects.remember_session_defaults("team-x/start-discovered", "claude", {"idea": True})
        self.assertNotIn("team-x/start-discovered", self.read()["projects"])
        base = projects.find("a")
        with mock.patch.object(projects, "find", side_effect=lambda key: dict(base, key="a@topic", base="a") if key == "a@topic" else base):
            projects.remember_session_defaults("a@topic", "codex", {"docker": True})
        self.assertEqual(projects.session_defaults()["a"], {"agent": "codex", "opts": ["docker"]})
        self.assertNotIn("a@topic", self.read()["session_defaults"])

    def test_renamed_key_keeps_profile_changed_path_cannot_inherit_it(self):
        projects.remember_session_defaults("a", "codex", {"idea": True})
        data = self.read()
        rows = settings._projects_rows(data)
        row = next(r for r in rows if r["key"] == "a")
        row["key"] = "renamed"
        settings._write_projects(data, rows)
        common.write_json(common.PROJECTS_FILE, data)
        self.assertNotIn("a", projects.session_defaults())
        self.assertEqual(projects.session_defaults()["renamed"]["agent"], "codex")
        data["projects"]["renamed"]["path"] = self.b
        common.write_json(common.PROJECTS_FILE, data)
        self.assertNotIn("renamed", projects.session_defaults())

    def test_corrupt_entries_are_ignored_and_inputs_validated(self):
        data = self.read()
        data["session_defaults"] = {"a": {"path": self.a, "agent": "unknown", "opts": []}, "b": None}
        common.write_json(common.PROJECTS_FILE, data)
        self.assertEqual(projects.session_defaults(), {})
        for key, agent, opts in (("missing", "claude", {}), ("advisor", "claude", {}),
                                 ("a", "unknown", {}), ("a", "claude", {"bad": True}),
                                 ("a", "claude", {"docker": 1})):
            with self.assertRaises(ValueError):
                projects.remember_session_defaults(key, agent, opts)
        self.assertEqual(self.read(), data)

    def test_swapping_project_keys_in_one_save_keeps_both_profiles(self):
        projects.remember_session_defaults("a", "codex", {"docker": True})
        projects.remember_session_defaults("b", "claude", {"idea": True})
        data = self.read()
        rows = settings._projects_rows(data)
        for row in rows:
            if row["key"] in ("a", "b"):
                row["key"] = "b" if row["key"] == "a" else "a"
        settings._write_projects(data, rows)
        common.write_json(common.PROJECTS_FILE, data)
        self.assertEqual(projects.session_defaults(), {
            "b": {"agent": "codex", "opts": ["docker"]}, "a": {"agent": "claude", "opts": ["idea"]}})

    def test_disabled_codex_is_preserved_without_overwriting_options(self):
        projects.remember_session_defaults("a", "codex", {"docker": True})
        projects.remember_session_defaults("a", "claude", {"idea": True}, keep_agent=True)
        self.assertEqual(projects.session_defaults()["a"], {"agent": "codex", "opts": ["idea"]})

    def test_only_explicit_gui_creation_remembers_even_empty_options(self):
        result = self.response()
        with mock.patch.object(self.manager, "create", return_value=SimpleNamespace(to_json=lambda: dict(result))) as create:
            self.assertEqual(post("/api/sessions", {"project": "a"}, self.token)[0], 202)
            self.assertNotIn("session_defaults", self.read())
            code, _, raw = post("/api/sessions", {"project": "a", "remember_for_project": True}, self.token)
            self.assertEqual(code, 202)
            self.assertEqual(json.loads(raw)["project_defaults"], {"agent": "claude", "opts": []})
            self.assertEqual(create.call_count, 2)
        self.assertEqual(projects.session_defaults()["a"]["opts"], [])
        code, _, raw = get("/api/state", token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["project_session_defaults"]["a"]["opts"], [])

    def test_remember_scope_and_type_are_checked_before_creating_session(self):
        with open(common.TOKENS_FILE, "a") as f:
            f.write("project-defaults-scoped\tsession:s1\thook\n")
        with mock.patch.object(self.manager, "create") as create:
            for token in (common.own_token("tty"), "project-defaults-scoped"):
                self.assertEqual(post("/api/sessions", {"project": "a", "remember_for_project": True}, token)[0], 403)
            for body in ({"project": "a", "remember_for_project": "true"},
                         {"project": "advisor", "remember_for_project": True}):
                self.assertEqual(post("/api/sessions", body, self.token)[0], 409)
            self.assertEqual(post("/api/inbox/start", {"key": "k", "session": "old", "remember_for_project": True}, self.token)[0], 409)
            create.assert_not_called()
        self.assertNotIn("session_defaults", self.read())

    def test_rejected_start_does_not_save_and_failed_save_does_not_fail_created_session(self):
        with mock.patch.object(self.manager, "create", side_effect=ValueError("limit reached")):
            self.assertEqual(post("/api/sessions", {"project": "a", "remember_for_project": True}, self.token)[0], 409)
        self.assertNotIn("session_defaults", self.read())
        with mock.patch.object(self.manager, "create", return_value=SimpleNamespace(to_json=lambda: self.response())) as create, \
                mock.patch.object(projects, "remember_session_defaults", side_effect=OSError("disk full")):
            code, _, raw = post("/api/sessions", {"project": "a", "remember_for_project": True}, self.token)
            result = json.loads(raw)
            self.assertEqual((code, result["id"]), (202, "created"))
            self.assertIn("not saved", result["defaults_warning"])
            create.assert_called_once()

    def test_manual_inbox_start_remembers_actual_session_project(self):
        with mock.patch.object(self.manager, "inbox_start", create=True, return_value=self.response("b", "codex", {"idea": True})):
            code, _, _ = post("/api/inbox/start", {"key": "k", "remember_for_project": True}, self.token)
            self.assertEqual(code, 202)
        self.assertEqual(projects.session_defaults()["b"], {"agent": "codex", "opts": ["idea"]})
        self.assertNotIn("a", projects.session_defaults())

    def test_global_enforcement_is_separate_from_explicit_project_defaults(self):
        self.manager.policy = {"unrestricted_network": True, "idea_in_sessions": True}
        code, _, raw = get("/api/state", token=self.token)
        self.assertEqual((code, json.loads(raw)["start_opts_enforced"]), (200, ["unrestricted", "idea"]))
        self.assertEqual(json.loads(raw)["project_session_defaults"], {})

    def test_changed_checkout_during_start_does_not_receive_the_old_sessions_choices(self):
        with self.assertRaises(ValueError):
            projects.remember_session_defaults("a", "claude", {"docker": True}, session_path=self.b)
        self.assertNotIn("session_defaults", self.read())

    def test_concurrent_project_settings_save_preserves_the_preference(self):
        entered, release = threading.Event(), threading.Event()
        original_write = projects.write_json
        errors = []

        def paused_write(path, data):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("timed out waiting for concurrent settings write")
            original_write(path, data)

        def remember():
            try:
                projects.remember_session_defaults("a", "claude", {"docker": True})
            except Exception as e:
                errors.append(e)

        def change_title():
            try:
                data = self.read()
                rows = settings._projects_rows(data)
                next(r for r in rows if r["key"] == "a")["title"] = "Project A"
                settings.update("projects", {"projects": rows})
            except Exception as e:
                errors.append(e)

        with mock.patch.object(projects, "write_json", side_effect=paused_write):
            first = threading.Thread(target=remember)
            first.start()
            self.assertTrue(entered.wait(3))
            second = threading.Thread(target=change_title)
            second.start()
            release.set()
            first.join(6)
            second.join(6)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.read()["projects"]["a"]["title"], "Project A")
        self.assertEqual(projects.session_defaults()["a"]["opts"], ["docker"])
