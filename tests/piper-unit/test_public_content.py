"""Public examples use neutral identities, reserved domains and documentation addresses."""
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import _env  # noqa: F401  (require the isolated OS test sandbox)

ROOT = Path(__file__).resolve().parents[2]


class PublicContentTest(unittest.TestCase):
    def test_maven_example_uses_neutral_identity_and_reserved_hosts(self):
        source = (ROOT / "tests/piper-unit/test_m2settings.py").read_text()
        xml = re.search(r'HOST_XML = """(.*?)"""', source, re.S).group(1)
        doc = ET.fromstring(xml)
        ns = {"m": "http://maven.apache.org/SETTINGS/1.0.0"}
        release = next(n for n in doc.findall(".//m:server", ns) if n.findtext("m:id", namespaces=ns) == "release")
        self.assertEqual(release.findtext("m:username", namespaces=ns), "build-user")
        for node in doc.findall(".//m:url", ns):
            self.assertTrue(urlsplit(node.text).hostname.endswith(".example.com"), node.text)

    def test_tunnel_placeholder_uses_documentation_address(self):
        source = (ROOT / "piper/web/src/httpview.ts").read_text()
        self.assertTrue("input(t0.forward ?? '', '192.0.2.1:443')" in source)

    @unittest.skipUnless((ROOT / "NOTICE").is_file(), "the repository's own documents exist only in a Piper checkout")
    def test_markdown_references_have_public_documents(self):
        # AGENTS.md / CLAUDE.md: the agents' own instruction files Piper writes into a session, not documents here
        documents = {p.name for p in ROOT.rglob("*.md") if ".git" not in p.parts} | {"AGENTS.md", "CLAUDE.md"}
        for base in (ROOT / "piper", ROOT / "scripts"):
            for path in base.rglob("*"):
                if not path.is_file() or "vendor" in path.parts or path.suffix not in {".py", ".ts", ".mjs", ".sh", ".md", ".sb"}:
                    continue
                for name in re.findall(r"\b[A-Z][A-Z0-9_-]*\.md\b", path.read_text()):
                    self.assertIn(name, documents, str(path.relative_to(ROOT)))

    def test_fixture_domains_use_reserved_names(self):
        for path in [p for d in ("piper-unit", "piper-e2e") for p in (ROOT / "tests" / d).rglob("*")]:
            if path.is_file() and path.suffix in {".py", ".mjs"}:
                self.assertIsNone(re.search(r"(?<![\w.-])(?:[a-z0-9-]+\.)+(?:cz|one)\b", path.read_text()),
                                  str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
