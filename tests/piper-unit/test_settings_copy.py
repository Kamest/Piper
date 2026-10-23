"""N12: the Settings copy speaks plainly - no CLI commands, no tool IDs or PIPER_* names in
labels, no internal references (L5, K2, W23, 11.3.4); the technical name stays in the tooltip / the choice value."""
import re
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import settings

CLI = re.compile(r"task piper:|piper:install-service|hole -u|hole --with|scripts/piper\.sh|gh auth token")
REF = re.compile(r"\((?:L\d|K\d+|W\d+|\d+\.\d+(?:\.\d+)?)\)|\bL3/L5\b|\bL5:")
TOOL = re.compile(r"\b(?:git_run|git_push|git_commit|gitlab_mr_merge|gitlab_job_play)\b|PIPER_[A-Z_]+")


class SettingsCopyTest(unittest.TestCase):
    def texts(self):
        for sec in settings.REGISTRY:
            if sec.hidden:
                continue
            yield sec.id, "section help", sec.help
            for f in sec.fields:
                yield sec.id, f.key + " label", f.label
                yield sec.id, f.key + " help", f.help
                for c in f.choices or []:
                    yield sec.id, f.key + " choice", str(c[1])

    def test_no_cli_commands_or_internal_references(self):
        bad = [(s, w, t) for s, w, t in self.texts() if CLI.search(t) or REF.search(t)]
        self.assertEqual(bad, [])
        for text in settings.APPLY.values():
            self.assertIsNone(CLI.search(text), text)
            self.assertNotIn("piperd", text)

    def test_labels_and_choices_carry_no_tool_ids(self):
        bad = [(s, w, t) for s, w, t in self.texts() if (w.endswith(" label") or w.endswith(" choice")) and TOOL.search(t)]
        self.assertEqual(bad, [])
        kinds = dict(settings.APPROVAL_KINDS)
        self.assertEqual(kinds["git_push"], "push", "the ID stays the value - the GUI puts it in the chip's tooltip")

    def test_environment_labels_are_words_the_variable_stays_the_field_env(self):
        env = next(s for s in settings.REGISTRY if s.id == "env")
        self.assertTrue(all(f.env and not f.label.startswith(("PIPER_", "PROJECT_PATH")) for f in env.fields))
        self.assertEqual(next(f for f in env.fields if f.key == "port").env, "PIPER_PORT")


if __name__ == "__main__":
    unittest.main()
