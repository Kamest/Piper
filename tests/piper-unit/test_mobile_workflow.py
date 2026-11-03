"""The hosted phone client deploys on a core release or by hand, from the checked build, and proves which commit it serves."""
import re
import unittest
from pathlib import Path

import _env  # noqa: F401  (require the isolated OS test sandbox)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/mobile.yml"


@unittest.skipUnless(WORKFLOW.is_file(), "the deploy workflow exists only in the public Piper checkout")
class MobileWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_release_or_manual_never_a_push(self):
        on = re.search(r"^on:\n((?:  .*\n)+)", self.text, re.M).group(1)
        self.assertEqual([l.strip() for l in on.splitlines() if l.startswith("  ") and not l.startswith("   ")],
                         ["release:", "workflow_dispatch:"], "a push must not replace the client the phones run")
        self.assertIn("types: [published]", on)
        guard = re.search(r"^    if: (.*)$", self.text, re.M).group(1)
        self.assertIn("github.event_name == 'workflow_dispatch'", guard)
        self.assertIn("startsWith(github.event.release.tag_name, 'v')", guard, "desktop-v* releases do not deploy it")
        self.assertIn("!github.event.release.prerelease", guard)

    def test_checked_build_exported_and_commit_verified(self):
        steps = [self.text.index(s) for s in ("piper-build.sh\" --check", "python3 scripts/piper-mobile-export.py", "npx --yes vercel@", "version.txt\" || true")]
        self.assertEqual(steps, sorted(steps))
        self.assertIn('echo "$GITHUB_SHA" > "$RUNNER_TEMP/mobile/version.txt"', self.text)
        self.assertRegex(self.text, r"vercel@\d+\.\d+\.\d+ ", "the Vercel CLI is pinned")

    def test_token_only_from_secrets_and_never_in_the_export(self):
        self.assertIn("VERCEL_TOKEN: ${{ secrets.VERCEL_TOKEN }}", self.text)
        self.assertEqual(self.text.count("secrets."), 1)
        self.assertIn("persist-credentials: false", self.text)


if __name__ == "__main__":
    unittest.main()
