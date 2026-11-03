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
            with mock.patch.dict(sys.modules, {"common": None}):   # the CLI: common.py never imported
                self.assertEqual(extfind.trust_file(), Path(env["HOME"]) / ".piper/trusted-extensions.json")
            # inside piperd: the home common.py resolved (a legacy ~/.hole-orch too)
            with mock.patch.dict(sys.modules, {"common": mock.Mock(PIPER_HOME="/resolved/home")}):
                self.assertEqual(extfind.trust_file(), Path("/resolved/home/trusted-extensions.json"))

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

    def test_doctor_rows_report_found_incompatible_origin_and_legacy_service_with_actions(self):
        import doctor
        path = self.extension(self.project / "demo")
        future = self.extension(self.project / "future", api=99)
        with mock.patch.object(extfind, "discover", return_value=self.discover()):
            rows = doctor.check_extensions()
        found = next(r for r in rows if "piper extensions trust" in r["fix"])
        self.assertEqual(found["action"], "extension-enable")
        self.assertTrue(any("API 99" in r["text"] for r in rows))
        # several trusted ones load together (9.167): no "multiple" warning any more
        extfind.set_trust(str(path), True)
        other = self.extension(self.project / "other", "other", legacy_labels=["old.piperd"])
        extfind.set_trust(str(other), True)
        services = self.tmp / "services"
        services.mkdir()
        with mock.patch.object(extfind, "discover", return_value=self.discover()), \
                mock.patch.object(doctor, "service_file", lambda label: str(services / label)):
            rows = doctor.check_extensions()
            self.assertEqual([r for r in rows if r["key"] != "extension" or "incompatible" not in r["title"]], [])
            (services / "old.piperd").write_text("x")
            legacy = [r for r in doctor.check_extensions() if r["key"] == "service-legacy"]
            self.assertEqual([(r["action"], "old.piperd" in r["text"]) for r in legacy], [("service-reinstall", True)])
            (services / "old.piperd").unlink()
            (services / doctor.SERVICE_LABEL).write_text("<key>PIPER_EXTENSIONS</key>")
            legacy = [r for r in doctor.check_extensions() if r["key"] == "service-legacy"]
            self.assertEqual([r["action"] for r in legacy], ["service-reinstall"])
        self.assertTrue(future.is_dir())

    def git_repo(self, path, remote):
        for args in (("init", "-q"), ("config", "remote.origin.url", remote)):
            subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    def test_trust_records_origin_and_a_changed_origin_is_not_loaded_until_trusted_again(self):
        path = self.extension(self.project / "repo", api=2)
        (path / "extension.py").write_text("API = 2\nNAME = 'demo'\n")
        self.git_repo(path, "https://user:pw@example.com/team/ext.git")
        extfind.set_trust(str(path), True)
        self.assertEqual(extfind.trusted()[str(path)]["remote"], "https://example.com/team/ext.git")
        self.assertEqual([e["path"] for e in extfind.selected(extfind.trusted_found())], [str(path)])
        self.assertEqual(self.load()[0], ["demo"])
        self.git_repo(path, "https://example.com/attacker/ext.git")
        entry = extfind.trusted_found()[0]
        self.assertTrue(entry["origin_changed"])
        self.assertEqual(extfind.selected([entry]), [])
        self.assertTrue(self.discover()[0]["origin_changed"])
        names, errors, blocked = self.load()
        self.assertEqual((names, errors), ([], []))
        self.assertEqual(blocked[0]["remote"], "https://example.com/attacker/ext.git")
        self.assertEqual(blocked[0]["trusted_remote"], "https://example.com/team/ext.git")
        import doctor
        with mock.patch.object(extfind, "discover", return_value=self.discover()):
            row = next(r for r in doctor.check_extensions() if "origin" in r["title"])
        self.assertEqual(row["action"], "extension-enable")
        extfind.set_trust(str(path), True)   # trusted again: the new origin is recorded
        self.assertEqual(self.load()[0], ["demo"])

    def load(self, **env):
        """extensions.load() in a fresh process: (names, ERRORS, BLOCKED)."""
        code = ("import json, os, extensions; extensions.load(); print(json.dumps([extensions.names(), "
                "extensions.ERRORS, extensions.BLOCKED, {k: os.environ.get(k) for k in ('DEMO_A', 'DEMO_B', 'DEMO_SEEN')}]))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=str(Path(extensions.__file__).parent), **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.last_env = json.loads(r.stdout)[3]
        return json.loads(r.stdout)[:3]

    def test_loader_reads_the_trust_file_loads_several_and_applies_env_file_before_import(self):
        one = self.extension(self.project / "one", "one", api=2, env_file="../one.env")
        (one / "extension.py").write_text("import os\nAPI = 2\nNAME = 'one'\nos.environ['DEMO_SEEN'] = os.environ.get('DEMO_A', '')\n")
        (self.project / "one.env").write_text("# comment\nexport DEMO_A='from file'\nDEMO_B=kept-out\nBAD LINE\n"
                                              "DEMO_C=$(touch %s)\n" % (self.tmp / "pwned"))
        two = self.extension(self.project / "two", "two", api=1)
        (two / "extension.py").write_text("API = 1\nNAME = 'two'\n")
        self.assertEqual(self.load(), [[], [], []])   # nothing trusted: core alone
        for path in (two, one):
            extfind.set_trust(str(path), True)
        names, errors, _ = self.load(DEMO_B="mine")
        self.assertEqual((names, errors), (["one", "two"], []))   # sorted by name, both at once
        self.assertEqual(self.last_env, {"DEMO_A": "from file", "DEMO_B": "mine", "DEMO_SEEN": "from file"})
        self.assertFalse((self.tmp / "pwned").exists())
        # the override still wins; set but empty = none; PIPER_NO_EXTENSIONS too
        self.assertEqual(self.load(PIPER_EXTENSIONS=str(two))[0], ["two"])
        self.assertEqual(self.load(PIPER_EXTENSIONS="")[0], [])
        self.assertEqual(self.load(PIPER_NO_EXTENSIONS="1")[0], [])
        # a trusted extension that became incompatible is reported, not imported
        (one / "piper-extension.json").write_text(json.dumps({"name": "one", "label": "One", "api": 99}))
        names, errors, _ = self.load()
        self.assertEqual(names, ["two"])
        self.assertIn("manifest API 99", errors[0])

    def test_manifest_api2_fields_and_validation(self):
        path = self.extension(self.project / "v2", api=2, description=" Adds things. ", env_file="../x.env",
                              service_env=["A_B", "C"], legacy_labels=["old.label", "older-one"])
        (path / "piper-extension.json").write_text(json.dumps(dict(json.loads((path / "piper-extension.json")
                                                                         .read_text()), wrapper=None)))
        info = extfind.manifest(str(path))
        self.assertEqual((info["wrapper"], info["description"], info["service_env"], info["legacy_labels"]),
                         ("", "Adds things.", ["A_B", "C"], ["old.label", "older-one"]))
        self.assertEqual(info["env_file"], str(self.project / "x.env"))
        self.assertTrue(info["compatible"])
        base = {"name": "demo", "label": "Demo", "api": 2}
        for bad in ({"service_env": ["A B"]}, {"service_env": "A"}, {"legacy_labels": ["../x"]},
                    {"env_file": "/etc/passwd"}, {"description": 3}, {"wrapper": "../escape.sh"}):
            (path / "piper-extension.json").write_text(json.dumps(dict(base, **bad)))
            with self.assertRaises(ValueError, msg=bad):
                extfind.manifest(str(path))
        (path / "piper-extension.json").write_text(json.dumps({"name": "demo", "label": "Demo", "api": 1}))
        with self.assertRaisesRegex(ValueError, "wrapper"):   # an API 1 manifest always named its wrapper
            extfind.manifest(str(path))

    def test_parse_env_file_never_evaluates(self):
        f = self.tmp / "e.env"
        f.write_text("A=1\nexport B=\"two words\"\nC='x # y'\nD=plain # note\nE=$HOME\nF=`id`\n"
                     "G=~/dir\n  export   H=h\n1X=no\nI=a b\n")
        self.assertEqual(extfind.parse_env_file(str(f)), {
            "A": "1", "B": "two words", "C": "x # y", "D": "plain", "G": os.path.expanduser("~/dir"), "H": "h"})
        self.assertEqual(extfind.parse_env_file(str(self.tmp / "missing")), {})

    def test_service_info_shell_output_is_quoted_and_delegation_is_api1_only(self):
        one = self.extension(self.project / "one", "one", api=2, env_file="e.env", service_env=["ONE_DIR"],
                             legacy_labels=["one.piperd"])
        (one / "e.env").write_text("ONE_DIR=\"/x'; touch %s; '\"\n" % (self.tmp / "pwned"))
        two = self.extension(self.project / "two", "two", api=1)
        for path in (one, two):
            extfind.set_trust(str(path), True)
        info = extfind.service_info()
        self.assertEqual((info["service_env"], info["legacy_labels"]), (["ONE_DIR"], ["one.piperd"]))
        cli = Path(extfind.__file__)
        out = subprocess.run([sys.executable, str(cli), "--delegate", "--sh"], capture_output=True, text=True,
                             env=os.environ).stdout
        r = subprocess.run(["sh", "-c", out + '\nprintf "%s|%s|%s|%s" "$EXT_WRAPPER" "$ONE_DIR" "$EXT_SERVICE_ENV" '
                            '"$EXT_LEGACY_LABELS"'], capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.stdout, "|/x'; touch %s; '|ONE_DIR|one.piperd" % (self.tmp / "pwned"))
        self.assertFalse((self.tmp / "pwned").exists())
        # one API 1 extension alone is still entered through its wrapper; an API 2 one never
        self.assertEqual(extfind.delegate(extfind.selected(extfind.trusted_found())), "")
        extfind.set_trust(str(one), False)
        self.assertEqual(extfind.delegate(extfind.selected(extfind.trusted_found())), str(two / "piper.sh"))
        extfind.set_trust(str(two), False)
        extfind.set_trust(str(one), True)
        self.assertEqual(extfind.delegate(extfind.selected(extfind.trusted_found())), "")

    def test_register_and_checkout_sources(self):
        inside = self.extension(self.checkout / "bundled", "bundled")
        outside = self.extension(self.tmp / "far/away/deep/ext", "far")
        self.assertEqual(extfind.register(str(outside))["path"], str(outside))
        extfind.register(str(outside))   # idempotent
        files = list((Path(self.env["XDG_CONFIG_HOME"]) / "piper/extensions.d").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text()), {"path": str(outside), "added": True})
        sources = {e["path"]: e["source"] for e in self.discover()}
        self.assertEqual(sources, {str(inside): "checkout", str(outside): "added"})
        self.assertEqual(extfind.trusted(), {})


if __name__ == "__main__":
    unittest.main()
