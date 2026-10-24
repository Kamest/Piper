"""Manifest-only discovery, bounded scans, private trust and API compatibility."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401
import extfind
import extensions


class ExtfindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "projects"
        self.project.mkdir()
        self.checkout = self.tmp / "checkout" / "core"
        self.checkout.mkdir(parents=True)
        self.env = {"HOME": str(self.tmp / "home"), "PIPER_HOME": str(self.tmp / "state"),
                    "XDG_CONFIG_HOME": str(self.tmp / "cfg"), "PROJECT_PATH": str(self.project)}
        self.enterContext(mock.patch.dict(os.environ, self.env))
        # Avoid scanning the real checkout's siblings in tests.

    def extension(self, path, name="demo", api=1, **extra):
        path.mkdir(parents=True, exist_ok=True)
        (path / "piper.sh").write_text("#!/bin/sh\nexit 0\n")
        (path / "extension.py").write_text("raise RuntimeError('discovery must not import this')\n")
        (path / "piper-extension.json").write_text(json.dumps(
            dict(name=name, label="Demo " + name, wrapper="piper.sh", api=api, **extra)))
        return path

    def register(self, path, name="demo"):
        config = Path(self.env["XDG_CONFIG_HOME"]) / "piper/extensions.d"
        config.mkdir(parents=True, exist_ok=True)
        (config / (name + ".json")).write_text(json.dumps({"path": str(path)}))

    def discover(self):
        return extfind.discover(checkout=str(self.checkout))

    def test_scan_depth_skips_hidden_modules_and_symlinks(self):
        one = self.extension(self.project / "one")
        two = self.extension(self.project / "repo/two", "two")
        self.extension(self.project / "repo/nested/three", "three")
        self.extension(self.project / ".hidden/ext", "hidden")
        self.extension(self.project / "node_modules/ext", "modules")
        outside = self.extension(self.tmp / "outside" / "deep/ext", "linked")
        (self.project / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual({e["path"] for e in self.discover()}, {str(one), str(two)})
        self.assertTrue(all(not e["trusted"] for e in self.discover()))
        self.assertFalse(extfind.trust_file().exists())

    def test_parent_roots_and_checkout_parent_are_scanned(self):
        sibling = self.extension(self.tmp / "sibling/ext", "sibling")
        neighbor = self.extension(self.checkout.parent / "neighbor/ext", "neighbor")
        paths = {e["path"] for e in self.discover()}
        self.assertIn(str(sibling), paths)
        self.assertIn(str(neighbor), paths)

    def test_registered_precedes_scan_realpath_dedup_and_invalid_entries(self):
        path = self.extension(self.project / "repo/ext")
        alias = self.tmp / "alias"
        alias.symlink_to(path, target_is_directory=True)
        self.register(alias)
        config = Path(self.env["XDG_CONFIG_HOME"]) / "piper/extensions.d"
        (config / "broken.json").write_text("invalid")
        (config / "missing.json").write_text('{"path": "/does/not/exist"}')
        (config / "list.json").write_text("[]")
        result = self.discover()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], str(path))
        self.assertEqual(result[0]["source"], "registered")
        self.assertEqual(result[0]["wrapper"], str(path / "piper.sh"))

    def test_trust_and_untrust_are_private_explicit_and_survive_outside_roots(self):
        path = self.extension(self.tmp / "outside/deep/ext")
        self.assertEqual(self.discover(), [])
        extfind.set_trust(str(path), True)
        record = extfind.trusted()[str(path)]
        self.assertEqual(record["name"], "demo")
        self.assertIn("at", record)
        self.assertEqual(extfind.trust_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual([e["path"] for e in extfind.selected(self.discover())], [str(path)])
        extfind.set_trust(str(path), False)
        self.assertEqual(extfind.trusted(), {})

    def test_incompatible_api_and_invalid_manifests_are_never_trusted(self):
        path = self.extension(self.project / "future", api=99)
        self.assertFalse(self.discover()[0]["compatible"])
        with self.assertRaisesRegex(ValueError, "incompatible"):
            extfind.set_trust(str(path), True)
        for data in ([], {}, {"name": "demo", "label": "Demo", "wrapper": "piper.sh", "api": True},
                     {"name": "demo", "label": "Demo", "wrapper": "../escape.sh", "api": 1}):
            (path / "piper-extension.json").write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                extfind.manifest(str(path))

    def test_broken_trust_record_is_not_implicit_consent_and_name_change_revokes_selection(self):
        path = self.extension(self.project / "demo")
        trust = extfind.trust_file()
        trust.parent.mkdir(parents=True)
        for data in ("invalid", "[]", json.dumps({str(path): True})):
            trust.write_text(data)
            self.assertFalse(self.discover()[0]["trusted"])
        extfind.set_trust(str(path), True)
        self.extension(path, "renamed")
        self.assertFalse(self.discover()[0]["trusted"])

    def test_delegate_reads_trust_only_never_scans(self):
        trusted = self.extension(self.tmp / "trusted/ext", "trusted")
        extfind.set_trust(str(trusted), True)
        self.register(self.extension(self.project / "registered", "registered"), "registered")
        out = []
        with mock.patch.object(extfind, "discover", side_effect=AssertionError("scan")), \
                mock.patch.object(extfind, "_git", side_effect=AssertionError("git")), \
                mock.patch.object(sys, "argv", ["extfind.py", "--delegate"]), \
                mock.patch("builtins.print", lambda *a, **k: out.append(a)):
            self.assertEqual(extfind.main(), 0)
        self.assertEqual(out, [(str(trusted / "piper.sh"),)])

    def test_wrapper_cannot_escape_through_symlink(self):
        path = self.extension(self.project / "demo")
        (path / "piper.sh").unlink()
        (path / "piper.sh").symlink_to(Path(__file__))
        with self.assertRaisesRegex(ValueError, "inside"):
            extfind.manifest(str(path))

    def test_git_metadata_strips_url_credentials_query_and_fragment(self):
        path = self.extension(self.project / "repo/ext")
        def git(*args):
            subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
        git("init", "-q")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "test")
        for remote, expected in (("https://user:secret@example.com/team/repo.git?token=secret#secret",
                                  "https://example.com/team/repo.git"),
                                 ("git@example.com:team/repo.git", "example.com:team/repo.git")):
            git("config", "remote.origin.url", remote)
            result = self.discover()[0]
            self.assertEqual(result["remote"], expected)
            self.assertRegex(result["commit"], r"^[a-f0-9]{7,}$")

    def test_scan_directory_and_time_budgets(self):
        for i in range(8):
            self.extension(self.project / ("repo%d" % i), str(i))
        with mock.patch.object(extfind, "MAX_DIRS", 3):
            self.assertLessEqual(len(self.discover()), 3)
        with mock.patch.object(extfind, "SCAN_SECONDS", 0):
            self.assertEqual(self.discover(), [])

    def test_default_config_and_cli_json_trust_untrust(self):
        path = self.extension(self.project / "demo")
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME")
        config = Path(env["HOME"]) / ".config/piper/extensions.d"
        config.mkdir(parents=True)
        (config / "demo.json").write_text(json.dumps({"path": str(path)}))
        cli = Path(extfind.__file__)
        def run(*args):
            return subprocess.run([sys.executable, str(cli), *args], env=env, capture_output=True, text=True)
        result = json.loads(run("--json").stdout)
        self.assertTrue(any(e["path"] == str(path) and e["source"] == "registered" for e in result))
        self.assertEqual(run("trust", str(path)).returncode, 0)
        self.assertEqual(run("untrust", str(path)).returncode, 0)
        self.assertNotEqual(run("trust").returncode, 0)
        env.pop("PIPER_HOME")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(extfind.trust_file(), Path(env["HOME"]) / ".piper/trusted-extensions.json")

    def test_loader_rejects_manifest_api_before_import_and_module_api_after_import(self):
        path = self.extension(self.project / "future", api=99)
        def load():
            return subprocess.run([sys.executable, "-c", "import extensions; extensions.load(); "
                                   "print(len(extensions.LOADED)); print(extensions.ERRORS)"],
                                  env=dict(os.environ, PYTHONPATH=str(Path(extensions.__file__).parent),
                                           PIPER_EXTENSIONS=str(path)), capture_output=True, text=True)
        result = load()
        self.assertIn("manifest API 99", result.stdout)
        self.assertNotIn("discovery must not import", result.stderr)
        (path / "extension.py").write_text("API = 99\n")
        (path / "piper-extension.json").write_text('{"api": 1}')
        result = load()
        self.assertTrue(result.stdout.startswith("0\n"))
        self.assertIn("module API 99", result.stdout)
        (path / "piper-extension.json").unlink()
        self.assertIn("module API 99", load().stdout)

    def test_doctor_rows_report_found_incompatible_and_ambiguous(self):
        import doctor
        path = self.extension(self.project / "demo")
        future = self.extension(self.project / "future", api=99)
        with mock.patch.object(extfind, "discover", return_value=self.discover()):
            rows = doctor.check_extensions()
        self.assertTrue(any("piper extensions trust" in r["fix"] for r in rows))
        self.assertTrue(any("API 99" in r["text"] for r in rows))
        extfind.set_trust(str(path), True)
        other = self.extension(self.project / "other", "other")
        extfind.set_trust(str(other), True)
        with mock.patch.object(extfind, "discover", return_value=self.discover()):
            rows = doctor.check_extensions()
        self.assertTrue(any("Multiple trusted" in r["title"] for r in rows))
        self.assertTrue(future.is_dir())


if __name__ == "__main__":
    unittest.main()
