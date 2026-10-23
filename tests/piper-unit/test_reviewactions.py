"""Shared review-action configuration: defaults, validation and Settings persistence."""
import copy
import os
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401 (isolate Piper paths before importing its modules)
import common
import reviewactions
import settings


class ReviewActionsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(prefix="piper-review-actions-")
        self.addCleanup(self.home.cleanup)
        patch = mock.patch.object(common, "PIPER_HOME", self.home.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.config = os.path.join(self.home.name, "review", "config.json")
        os.makedirs(os.path.dirname(self.config))

    def test_defaults_are_valid_and_loading_does_not_share_mutable_templates(self):
        expected = copy.deepcopy(reviewactions.DEFAULT_ACTIONS)
        self.assertEqual(reviewactions.validate(expected), expected)
        loaded = reviewactions.load()
        self.assertEqual(loaded, expected)
        loaded[0]["prompt"] = "Changed locally"
        loaded.append(dict(loaded[0], id="other", key=9))
        self.assertEqual(reviewactions.load(), expected)
        self.assertEqual(reviewactions.DEFAULT_ACTIONS, expected)

    def test_validator_normalizes_copies_and_accepts_all_documented_variables(self):
        row = {"id": "inspect_contract", "key": 9, "label": "  Inspect contract  ", "kind": "read",
               "prompt": "  {{target}} {{file}} {{range}} {{side}} {{code}} {{note}}  "}
        before = copy.deepcopy(row)
        result = reviewactions.validate([row])
        self.assertEqual(result, [dict(row, label=row["label"].strip(), prompt=row["prompt"].strip())])
        self.assertEqual(row, before)
        self.assertIsNot(result[0], row)

    def test_invalid_templates_and_conflicting_shortcuts_are_rejected(self):
        base = copy.deepcopy(reviewactions.DEFAULT_ACTIONS[0])
        invalid = [None, {}, [], [base] * 10,
                   [base, dict(base, key=9)], [base, dict(base, id="another")],
                   [dict(base, unexpected=True)], [{k: v for k, v in base.items() if k != "prompt"}], ["action"]]
        for key, values in {
            "id": ("", "Uppercase", "two words", "a" * 41, None),
            "key": (0, 10, True, 1.0, "1"),
            "label": (" ", "two\nlines", "x" * 61, None),
            "kind": ("execute", None),
            "prompt": (" ", "x" * 4001, None, "Inspect {{unknown}}"),
        }.items():
            invalid.extend([dict(base, **{key: value})] for value in values)
        for value in invalid:
            with self.subTest(value=repr(value)[:100]), self.assertRaises(ValueError):
                reviewactions.validate(value)

    def test_settings_round_trip_preserves_unrelated_config_and_rejects_invalid_update(self):
        common.write_json(self.config, {"unrelated": {"keep": True}})
        actions = [{"id": "explain_api", "key": 8, "label": " Explain API ", "kind": "read",
                    "prompt": " Describe {{target}} using {{code}}. {{note}} "}]
        settings.update("review", {"actions": actions})
        normalized = reviewactions.validate(actions)
        self.assertEqual(reviewactions.load(), normalized)
        doc = common.read_json(self.config, {})
        self.assertEqual(doc, {"unrelated": {"keep": True}, "actions": normalized})
        section = next(s for s in settings.describe()["sections"] if s["id"] == "review")
        field = next(f for f in section["fields"] if f["key"] == "actions")
        self.assertEqual(field["value"], normalized)
        self.assertFalse(field["is_default"])
        with self.assertRaises(settings.SettingsError) as error:
            settings.update("review", {"actions": [dict(actions[0], key=0)]})
        self.assertIn("actions", error.exception.errors)
        self.assertEqual(common.read_json(self.config, {}), doc)


if __name__ == "__main__":
    unittest.main()
