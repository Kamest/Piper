"""Feature modules: presets for a new and an existing home, saving,
the Settings section, hidden sections and the background work a module stops."""
import json
import os
import shutil
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import features
import feed
import mattermost
import settings
import timesheet


class FeaturesTest(unittest.TestCase):
    def setUp(self):
        with open(features.FILE) as f:
            self.saved = f.read()
        self.policy = os.path.exists(common.POLICY_FILE)
        features._CACHE.clear()

    def tearDown(self):
        with open(features.FILE, "w") as f:
            f.write(self.saved)
        if not self.policy and os.path.exists(common.POLICY_FILE):
            os.unlink(common.POLICY_FILE)
        features._CACHE.clear()

    def _restore_sessions(self):
        shutil.rmtree(common.SESSIONS_DIR, ignore_errors=True)
        os.rename(common.SESSIONS_DIR + ".aside", common.SESSIONS_DIR)

    def fresh(self, policy=False, session=False):
        os.unlink(features.FILE)
        if os.path.exists(common.POLICY_FILE):
            os.unlink(common.POLICY_FILE)
        if policy:
            with open(common.POLICY_FILE, "w") as f:
                f.write("{}")
        # other tests leave sessions behind: this home is "new" only with that directory set aside
        if os.path.isdir(common.SESSIONS_DIR) and not os.path.isdir(common.SESSIONS_DIR + ".aside"):
            os.rename(common.SESSIONS_DIR, common.SESSIONS_DIR + ".aside")
            self.addCleanup(self._restore_sessions)
        if session:
            os.makedirs(os.path.join(common.SESSIONS_DIR, "s-test"))
        features._CACHE.clear()
        return features.load()

    def test_new_home_gets_basic_and_the_setup_card(self):
        d = self.fresh()
        self.assertEqual(d["base"], "basic")
        self.assertFalse(d["onboarded"])
        self.assertEqual(sorted(m for m, v in d["modules"].items() if v), sorted(features.PRESETS["basic"]))
        with open(features.FILE) as f:   # written once, with every module
            self.assertEqual(set(json.load(f)["modules"]), set(features.IDS))

    def test_existing_home_keeps_everything(self):
        for kw in ({"policy": True}, {"session": True}):
            d = self.fresh(**kw)
            self.assertEqual(d["base"], "all", kw)
            self.assertFalse(d["onboarded"], kw)   # the setup card once, nothing hidden
            want = features.preset("all")
            want.update(features._migrate(want))   # D10: only what did not run before goes off
            self.assertEqual(d["modules"], want, kw)
            self.assertFalse(d["modules"]["host"], kw)   # no policy host_terminal, no host shells

    def test_module_added_later_takes_the_base_value_and_is_written_out(self):
        with open(features.FILE, "w") as f:
            json.dump({"base": "basic", "onboarded": True, "modules": {"diag": True}}, f)
        features._CACHE.clear()
        self.assertFalse(features.on("gitlab"))
        self.assertTrue(features.on("diag"))       # in the basic preset
        self.assertFalse(features.on("no-such-module"))
        with open(features.FILE) as f:
            self.assertEqual(set(json.load(f)["modules"]), set(features.IDS))

    def test_broken_file_means_everything_on(self):
        with open(features.FILE, "w") as f:
            f.write("{not json")
        features._CACHE.clear()
        self.assertTrue(features.on("gitlab"))

    def test_summary_carries_the_preset_texts_and_their_modules(self):
        s = features.summary()
        self.assertEqual([p[0] for p in s["presets"]], ["basic", "dev", "all"])
        self.assertEqual(s["preset_modules"]["basic"], features.PRESETS["basic"])
        self.assertEqual(set(s["preset_modules"]["all"]), set(features.IDS) - features.NO_PRESET)
        self.assertFalse(any("host" in m for m in s["preset_modules"].values()))
        for _, _, text in s["presets"]:   # the card sells, it does not say what is hidden
            self.assertNotIn("hidden", text)

    def test_module_texts_name_no_agent_tool(self):
        """B-N4-1: Settings -> Features and the setup card are for a human: no `db_*` / tool names."""
        import re
        for mid, label, text in features.MODULES:
            with self.subTest(module=mid):
                self.assertIsNone(re.search(r"\b[a-z]+_(\*|[a-z]+\b)", label + " " + text), text)

    def test_core_module_texts_fit_one_line(self):
        """9.37 D10: over 110 characters settings.ts folds a help into "more" - a core row stays readable."""
        for mid, label, text in features.MODULES:
            if mid not in features.INTEGRATIONS:
                with self.subTest(module=mid):
                    self.assertLessEqual(len(text), 110, text)

    def test_save_preset_modules_onboarded_and_audit(self):
        self.fresh()
        out = features.save(preset_name="dev")
        self.assertEqual(out["preset"], "dev")
        out = features.save({"prod": True}, onboarded=True)
        self.assertEqual(out["preset"], "custom")
        self.assertTrue(out["onboarded"])
        self.assertTrue(features.on("prod") and features.on("gitlab"))
        with open(common.AUDIT_FILE) as f:
            ev = [json.loads(x) for x in f if "features-change" in x][-1]
        self.assertEqual(ev["on"], ["prod"])
        with self.assertRaises(ValueError):
            features.save({"nope": True})
        with self.assertRaises(ValueError):
            features.save({"gitlab": "yes"})
        with self.assertRaises(ValueError):
            features.save(preset_name="tiny")

    def test_settings_hides_the_sections_of_modules_that_are_off(self):
        features.save(preset_name="all")
        ids = {s["id"] for s in settings.describe()["sections"]}
        self.assertTrue({"features", "automatika", "timesheet", "prodcheck", "mattermost", "databases", "feed"} <= ids)
        features.save(preset_name="basic")
        ids = {s["id"] for s in settings.describe()["sections"]}
        self.assertFalse(ids & {"automatika", "timesheet", "prodcheck", "mattermost", "databases", "feed"})
        self.assertTrue({"features", "interface", "git", "projects", "sessions", "sandbox", "scan"} <= ids)
        self.assertFalse(ids & {"gitlab", "http"})   # D10: the GitLab token and HTTP follow their modules
        self.assertEqual(settings.describe()["sections"][0]["id"], "features")
        # a hidden section can still be saved (an agent's proposal, an open page)
        settings.update("timesheet", {"gap_min": 20})

    def test_settings_switch_writes_the_module(self):
        features.save(preset_name="basic")
        sec = next(s for s in settings.describe()["sections"] if s["id"] == "features")
        settings.update("features", {"gitlab": True}, rev_seen=sec["rev"])
        self.assertTrue(features.on("gitlab"))
        with open(features.FILE) as f:
            self.assertEqual(json.load(f)["base"], "basic")   # the rest of the file is kept

    def test_9_40_one_features_section_with_the_services_and_host(self):
        """9.40: Integrations folded into Features - every module on one page, host still asks before it turns on."""
        ids = [s.id for s in settings.REGISTRY]
        self.assertNotIn("integrations", ids)
        keys = [f.key for f in settings.section("features").fields]
        self.assertEqual(keys, features.CORE + features.INTEGRATIONS)
        self.assertEqual(keys[-1], "host")
        features.save(preset_name="basic")
        with self.assertRaises(settings.NeedsConfirm) as cm:
            settings.update("features", {"host": True, "gitlab": True})
        self.assertEqual([i["key"] for i in cm.exception.items], ["host"])
        self.assertFalse(features.on("host"))   # nothing written without the confirmation
        settings.update("features", {"host": True}, confirm=["host"])
        self.assertTrue(features.on("host"))

    # ---------------------------------------------------------------- D10: one switch per function
    def _old_file(self, modules=None, **cfgs):
        """a features.json from before D10 (no `v`) plus the features' own config files / the policy"""
        with open(features.FILE, "w") as f:
            json.dump({"base": "all", "onboarded": True, "modules": modules or features.preset("all")}, f)
        for rel, doc in cfgs.items():
            path = common.POLICY_FILE if rel == "policy" else os.path.join(common.PIPER_HOME, rel, "config.json")
            existed = os.path.exists(path)
            saved = open(path).read() if existed else None
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(doc, f)
            self.addCleanup(self._put_back, path, saved)
        features._CACHE.clear()

    @staticmethod
    def _put_back(path, saved):
        if saved is None:
            if os.path.exists(path):   # tearDown may have removed the policy already
                os.unlink(path)
        else:
            with open(path, "w") as f:
                f.write(saved)

    def test_migration_takes_over_the_second_switches(self):
        self._old_file(timesheet={"enabled": False}, obs={"enabled": False, "time": "07:30"},
                       policy={"host_terminal": True})
        d = features.load()
        self.assertFalse(d["modules"]["timesheet"])   # did not record: stays silent
        self.assertFalse(d["modules"]["prod"])        # the morning check was off
        self.assertTrue(d["modules"]["host"])         # policy host_terminal -> the host module
        self.assertTrue(d["modules"]["gitlab"] and d["modules"]["agent"])
        with open(features.FILE) as f:
            self.assertEqual(json.load(f)["v"], features.MIGRATION)
        with open(common.AUDIT_FILE) as f:
            ev = [json.loads(x) for x in f if "features-migrate" in x][-1]
        self.assertEqual(ev["changed"], {"timesheet": False, "prod": False, "host": True})

    def test_migration_keeps_what_ran_and_runs_once(self):
        self._old_file(timesheet={"enabled": True}, obs={"time": "07:30"}, policy={})
        d = features.load()
        self.assertTrue(d["modules"]["timesheet"] and d["modules"]["prod"])
        self.assertFalse(d["modules"]["host"])
        features.save({"timesheet": False, "host": True})
        features._CACHE.clear()
        d = features.load()   # v=2: the old keys are never read again
        self.assertFalse(d["modules"]["timesheet"])
        self.assertTrue(d["modules"]["host"])

    def test_migration_without_a_writable_home_works_in_memory(self):
        self._old_file(timesheet={"enabled": False}, policy={})
        real = common.write_json
        def deny(*a, **k):
            raise OSError("read-only")
        common.write_json = deny
        self.addCleanup(setattr, common, "write_json", real)
        self.assertFalse(features.load()["modules"]["timesheet"])

    def test_the_loaders_follow_the_module_not_their_legacy_key(self):
        import prodcheck
        self._old_file(feed={"enabled": False}, timesheet={"enabled": False})
        self.assertFalse(features.load()["modules"]["timesheet"])   # migrated off: it did not record
        features.save({"agent": True, "timesheet": True})           # from now on the module alone decides
        self.assertTrue(feed.load_config()["enabled"])         # Feed = the agent module
        self.assertTrue(timesheet.load_config()["enabled"])    # recording = the timesheet module
        features.save({"agent": False, "timesheet": False})
        self.assertFalse(feed.load_config()["enabled"])
        self.assertFalse(timesheet.load_config()["enabled"])
        self.assertEqual(prodcheck.validate_config({"enabled": False, "time": "07:30"})["time"], "07:30")   # legacy key tolerated

    def test_a_preset_never_touches_the_host_shells(self):
        self.fresh()
        features.save({"host": True})
        for p in features.PRESETS:
            self.assertTrue(features.save(preset_name=p)["modules"]["host"], p)
        features.save({"host": False})
        self.assertFalse(features.save(preset_name="all")["modules"]["host"])
        self.assertEqual(features.matching_preset(dict(features.preset("dev"), host=True)), "dev")

    def test_9_44_composer_is_off_until_switched_on(self):
        """9.44: the Composer is a module in no preset - a new home, an existing one, Everything: off; its own switch."""
        for kw in ({}, {"policy": True}):
            self.assertFalse(self.fresh(**kw)["modules"]["composer"], kw)
        with open(features.FILE, "w") as f:   # an existing features.json from before 9.44
            json.dump({"base": "all", "onboarded": True, "modules": {"gitlab": True}, "v": features.MIGRATION}, f)
        features._CACHE.clear()
        self.assertFalse(features.on("composer"))
        self.assertTrue(features.on("grid"))
        self.assertFalse(features.save(preset_name="all")["modules"]["composer"])
        features.save({"composer": True})
        self.assertTrue(features.save(preset_name="basic")["modules"]["composer"])   # a preset leaves it alone
        self.assertEqual(features.matching_preset(dict(features.preset("dev"), composer=True)), "dev")
        f = next(x for x in settings.section("features").fields if x.key == "composer")
        self.assertIs(f.default, False)
        self.assertIn("composer", features.CORE)

    def test_modules_off_stop_their_work(self):
        features.save(preset_name="all")
        os.makedirs(os.path.dirname(feed.CONFIG_FILE), exist_ok=True)
        with open(feed.CONFIG_FILE, "w") as f:
            json.dump({"enabled": True}, f)
        self.addCleanup(os.unlink, feed.CONFIG_FILE)
        self.assertTrue(feed.load_config()["enabled"])
        features.save({"agent": False})
        self.assertFalse(feed.load_config()["enabled"])
        os.makedirs(os.path.dirname(timesheet.CONFIG_FILE), exist_ok=True)
        with open(timesheet.CONFIG_FILE, "w") as f:
            json.dump({"enabled": True}, f)
        self.addCleanup(os.unlink, timesheet.CONFIG_FILE)
        self.assertTrue(timesheet.load_config()["enabled"])
        features.save({"timesheet": False})
        self.assertFalse(timesheet.load_config()["enabled"])
        with open(mattermost.WEBHOOK_FILE, "w") as f:
            f.write("https://mm.example/hooks/x")
        self.addCleanup(os.unlink, mattermost.WEBHOOK_FILE)
        features.save({"mattermost": False})
        n = mattermost.Notifier.__new__(mattermost.Notifier)
        self.assertFalse(n.notify("x", force=True))   # never reaches _post


if __name__ == "__main__":
    unittest.main()
