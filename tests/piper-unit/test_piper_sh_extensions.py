"""The CLI enters only a trusted compatible fake wrapper, preserving argv and its environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import _env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


class PiperShExtensionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.core = self.tmp / "projects/core"
        for directory in ("scripts", "piper"):
            (self.core / directory).mkdir(parents=True)
        for name in ("piper.sh", "lib-service.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.core / "scripts" / name)
        for name in ("extensions.py", "extfind.py"):
            shutil.copyfile(ROOT / "piper" / name, self.core / "piper" / name)
        (self.core / "piper/piperd.py").write_text("print('http://127.0.0.1:19999/#token=test-token')\n")
        (self.core / "piper/doctor.py").write_text("print('core doctor')\n")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.stub("curl", "exit 0")
        self.log = self.tmp / "wrapper.json"
        self.env = {"HOME": str(self.tmp / "home"), "PIPER_HOME": str(self.tmp / "state"),
                    "XDG_CONFIG_HOME": str(self.tmp / "cfg"), "PROJECT_PATH": str(self.core.parent),
                    "PATH": str(self.bin) + ":/usr/bin:/bin", "PIPER_PORT": "19999", "TRACE": str(self.log)}
        Path(self.env["HOME"]).mkdir()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def extension(self, name="demo", api=1, at=None):
        path = at or self.core.parent / (name + " repo") / "extension"
        path.mkdir(parents=True)
        (path / "piper-extension.json").write_text(json.dumps(
            {"name": name, "label": "Demo " + name, "api": api, "wrapper": "wrapper.sh"}))
        (path / "extension.py").write_text("API = 1\nNAME = %r\n" % name)
        (path / "wrapper.sh").write_text('''#!/bin/sh
export PIPER_CLI="$0" PIPER_SERVICE_LABEL=demo.piperd DEMO_ENV=wrapper-owned
EXT=$(dirname -- "$0")
export PIPER_EXTENSIONS="$EXT" PIPER_ENV_FILE="$EXT/environment"
python3 - "$@" <<'PY_TRACE'
import json, os, sys
with open(os.environ["TRACE"], "w") as f:
    json.dump({"args": sys.argv[1:], "src": os.environ.get("PIPER_SRC"), "cli": os.environ["PIPER_CLI"]}, f)
PY_TRACE
exec sh "$PIPER_SRC/scripts/piper.sh" "$@"
''')
        (path / "wrapper.sh").chmod(0o755)
        (path / "environment").write_text("export DEMO_ENV=wrapper-owned\n")
        return path

    def run_sh(self, *args, **extra):
        return subprocess.run(["sh", str(self.core / "scripts/piper.sh"), *args], env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=20, cwd=self.tmp)

    def trust(self, path):
        result = self.run_sh("extensions", "trust", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_untrusted_never_delegates_management_lists_json_and_text(self):
        path = self.extension()
        self.assertEqual(self.run_sh("status").returncode, 0)
        self.assertFalse(self.log.exists())
        info = json.loads(self.run_sh("extensions", "--json").stdout)[0]
        self.assertFalse(info["trusted"])
        self.assertEqual(info["path"], str(path))
        self.assertIn("piper extensions trust", self.run_sh("extensions").stdout)
        self.assertFalse((Path(self.env["PIPER_HOME"]) / "trusted-extensions.json").exists())

    def test_trusted_wrapper_gets_exact_argv_src_and_owns_the_environment(self):
        path = self.extension()
        self.trust(path)
        # The wrapper's own doctor can inspect its environment after re-entering the core.
        (self.core / "piper/doctor.py").write_text("import os; print(os.environ['DEMO_ENV'])\n")
        result = self.run_sh("doctor", "--json", "argument with spaces", "'")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "wrapper-owned\n")
        trace = json.loads(self.log.read_text())
        self.assertEqual(trace["args"], ["doctor", "--json", "argument with spaces", "'"])
        self.assertEqual(trace["src"], str(self.core))
        self.assertEqual(trace["cli"], str(path / "wrapper.sh"))

    def test_wrapper_uses_its_own_interpreter(self):
        path = self.extension()
        (path / "wrapper.sh").write_text("#!%s\nimport json, os, sys\n"
                                         "print(json.dumps([sys.argv[1:], os.environ['PIPER_SRC']]))\n" % sys.executable)
        self.trust(path)
        result = self.run_sh("doctor", "argument with spaces")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [["doctor", "argument with spaces"], str(self.core)])

    def test_untrust_management_never_delegates_even_after_trust(self):
        path = self.extension()
        self.trust(path)
        self.assertEqual(self.run_sh("extensions", "untrust", str(path)).returncode, 0)
        self.assertFalse(self.log.exists())
        self.run_sh("status")
        self.assertFalse(self.log.exists())

    def test_disabled_or_existing_wrapper_never_delegates(self):
        self.trust(self.extension())
        for extra in ({"PIPER_NO_EXTENSIONS": "1"}, {"PIPER_CLI": "/already/in/wrapper"}):
            self.assertEqual(self.run_sh("status", **extra).returncode, 0)
            self.assertFalse(self.log.exists())

    def test_multiple_trusted_load_together_and_none_is_entered(self):
        # 9.167: several trusted extensions load at once; with two the CLI enters no wrapper (core decides)
        paths = [self.extension(name) for name in ("one", "two")]
        for path in paths:
            self.trust(path)
        result = self.run_sh("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["extensions"], ["one", "two"])
        self.assertEqual(json.loads(result.stdout)["label"], "piperd")
        self.assertFalse(self.log.exists())

    def api2(self, name="neo", **manifest):
        path = self.core.parent / (name + " repo") / "extension"
        path.mkdir(parents=True)
        (path / "piper-extension.json").write_text(json.dumps(
            dict({"name": name, "label": "Neo", "api": 2}, **manifest)))
        (path / "extension.py").write_text("API = 2\nNAME = %r\n" % name)
        return path

    def test_api2_extension_is_never_entered_and_lends_its_env_file_to_the_cli(self):
        path = self.api2(env_file="../conf.env", legacy_labels=["neo.old"])
        (path.parent / "conf.env").write_text("export PROJECT_PATH=/from/env-file\nNEO_VALUE='a b'\n")
        (self.core / "piper/doctor.py").write_text(
            "import os; print(os.environ['PROJECT_PATH'], os.environ.get('NEO_VALUE'))\n")
        env = {k: v for k, v in self.env.items() if k != "PROJECT_PATH"}
        def run(*args):
            return subprocess.run(["sh", str(self.core / "scripts/piper.sh"), *args], env=env,
                                  capture_output=True, text=True, timeout=20, cwd=self.tmp)
        self.assertEqual(run("doctor").stdout, "%s None\n" % self.core.parent)   # untrusted: nothing of it
        self.trust(path)
        self.assertEqual(run("doctor").stdout, "/from/env-file a b\n")
        env["PROJECT_PATH"] = "/mine"   # the user's own value wins
        self.assertEqual(run("doctor").stdout, "/mine a b\n")
        self.assertEqual(json.loads(run("status", "--json").stdout)["extensions"], ["neo"])
        self.assertFalse(self.log.exists())

    def test_api_changed_after_trust_never_delegates(self):
        path = self.extension()
        self.trust(path)
        manifest = path / "piper-extension.json"
        data = json.loads(manifest.read_text())
        data["api"] = 99
        manifest.write_text(json.dumps(data))
        self.run_sh("status")
        self.assertFalse(self.log.exists())

    def test_open_print_keeps_the_tokened_url_and_status_json_has_no_token(self):
        self.trust(self.extension())
        result = self.run_sh("open", "--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "http://127.0.0.1:19999/#token=test-token\n")
        info = self.run_sh("status", "--json")
        self.assertEqual(json.loads(info.stdout), {"up": True, "url": "http://127.0.0.1:19999/",
                                                  "label": "demo.piperd", "extensions": ["demo"]})   # API 1 wrapper
        self.assertNotIn("token", info.stdout)
        self.stub("curl", "exit 1")
        self.assertFalse(json.loads(self.run_sh("status", "--json").stdout)["up"])


    def run_tty(self, *args, answer=""):
        """stdin and stderr on a pseudo terminal (an interactive user), stdout captured as piper.sh does."""
        import pty
        master, slave = pty.openpty()
        try:
            os.write(master, answer.encode() + b"\n")
            result = subprocess.run(["sh", str(self.core / "scripts/piper.sh"), *args], env=self.env,
                                    stdin=slave, stderr=slave, stdout=subprocess.PIPE, text=True, timeout=20,
                                    cwd=self.tmp)
            os.set_blocking(master, False)
            seen = b""
            try:
                while chunk := os.read(master, 65536):
                    seen += chunk
            except (BlockingIOError, OSError):
                pass
            return result, seen.decode(errors="replace")
        finally:
            os.close(slave)
            os.close(master)

    def test_first_terminal_run_asks_once_for_an_extension_inside_the_checkout_yes_delegates(self):
        # 9.166: after a pull an extension sits in the checkout, unregistered; a bare `piper` asks
        path = self.extension(at=self.core / "fg-ext")
        result, seen = self.run_tty("status", answer="y")
        self.assertEqual(result.returncode, 0, seen)
        self.assertIn("found the extension Demo demo at %s" % path, seen)
        self.assertIn("Use it? [y/N]", seen)
        self.assertTrue(self.log.exists(), "yes must delegate in the same run")
        self.assertIn(str(path), json.loads((Path(self.env["PIPER_HOME"]) / "trusted-extensions.json").read_text()))
        self.log.unlink()
        result, seen = self.run_tty("status")
        self.assertNotIn("Use it?", seen)
        self.assertTrue(self.log.exists())

    def test_first_terminal_run_no_is_remembered_and_untrust_never_reasks(self):
        path = self.extension()
        reg = Path(self.env["XDG_CONFIG_HOME"]) / "piper/extensions.d"
        reg.mkdir(parents=True)
        (reg / "demo.json").write_text(json.dumps({"path": str(path)}))
        result, seen = self.run_tty("status", answer="n")
        self.assertIn("Use it? [y/N]", seen)
        self.assertIn("piper extensions trust", seen)
        self.assertFalse(self.log.exists())
        self.assertFalse((Path(self.env["PIPER_HOME"]) / "trusted-extensions.json").exists())
        result, seen = self.run_tty("status", answer="y")
        self.assertNotIn("Use it?", seen)
        self.assertFalse(self.log.exists())
        # an explicit trust clears the "no"; an untrust is a "no" again
        self.trust(path)
        self.assertEqual(self.run_sh("extensions", "untrust", str(path)).returncode, 0)
        result, seen = self.run_tty("status", answer="y")
        self.assertNotIn("Use it?", seen)
        self.assertFalse(self.log.exists())

    def test_no_terminal_never_asks_and_writes_nothing(self):
        self.extension(at=self.core / "fg-ext")
        result = self.run_sh("status")
        self.assertNotIn("Use it?", result.stderr)
        self.assertFalse(self.log.exists())
        for name in ("trusted-extensions.json", "declined-extensions.json"):
            self.assertFalse((Path(self.env["PIPER_HOME"]) / name).exists())


    def test_decline_command_marks_the_listing_and_stops_the_terminal_question(self):
        # 9.169: the desktop app records "Not now" without a running service
        path = self.extension(at=self.core / "fg-ext")
        self.assertFalse(json.loads(self.run_sh("extensions", "--json").stdout)[0]["declined"])
        self.assertEqual(self.run_sh("extensions", "decline", str(path)).returncode, 0)
        self.assertTrue(json.loads(self.run_sh("extensions", "--json").stdout)[0]["declined"])
        result, seen = self.run_tty("status", answer="y")
        self.assertNotIn("Use it?", seen)
        self.assertFalse(self.log.exists())
        self.trust(path)   # an explicit trust clears the "no"
        self.assertFalse(json.loads(self.run_sh("extensions", "--json").stdout)[0]["declined"])


if __name__ == "__main__":
    unittest.main()
