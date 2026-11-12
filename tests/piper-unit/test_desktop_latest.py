"""scripts/desktop-latest.py: the Pages manifest the desktop app checks instead of the rate-limited GitHub API,
and the workflows that publish it."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

import _env  # noqa: F401  (require the isolated OS test sandbox)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "desktop-latest.py"
PAGE = "https://github.com/Kamest/Piper/releases/tag/"


def rel(tag, **extra):
    return {"tag_name": tag, "html_url": PAGE + tag, "draft": False, "prerelease": False, **extra}


def run(data):
    return subprocess.run([sys.executable, "-I", str(SCRIPT)], input=json.dumps(data), capture_output=True, text=True)


class DesktopLatestTest(unittest.TestCase):
    def test_newest_published_desktop_release_by_version(self):
        pages = [[rel("v0.9.0"), rel("desktop-v0.1.9"), rel("desktop-v0.1.10")],
                 [rel("desktop-v0.2.0", draft=True), rel("desktop-v0.3.0", prerelease=True),
                  rel("desktop-v0.1.2"), {**rel("desktop-v0.4.0"), "html_url": "https://example.com/x"},
                  rel("desktop-v0.5.0-beta")]]
        r = run(pages)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"tag_name": "desktop-v0.1.10", "html_url": PAGE + "desktop-v0.1.10"})
        self.assertEqual(json.loads(run(pages[0]).stdout)["tag_name"], "desktop-v0.1.10")   # one unpaged list

    def test_no_desktop_release_fails_instead_of_publishing_an_empty_manifest(self):
        for data in ([], [[rel("v0.1.0")]], {"message": "rate limited"}):
            r = run(data)
            self.assertEqual(r.returncode, 1, data)
            self.assertEqual(r.stdout, "")

    def test_pages_publishes_the_manifest_and_a_release_redeploys_pages(self):
        pages = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("scripts/desktop-latest.py", pages)
        self.assertIn("_site/desktop/latest.json", pages)
        release = (ROOT / ".github/workflows/desktop-release.yml").read_text()
        self.assertIn("gh workflow run pages.yml", release)
        self.assertIn("actions: write", release)


if __name__ == "__main__":
    unittest.main()
