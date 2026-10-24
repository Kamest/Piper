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

    def extension(self, name="demo", api=1):
        path = self.core.parent / (name + " repo") / "extension"
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

    def test_multiple_trusted_never_guess_and_print_both_paths(self):
        paths = [self.extension(name) for name in ("one", "two")]
        for path in paths:
            self.trust(path)
        result = self.run_sh("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("multiple trusted extensions", result.stderr)
        for path in paths:
            self.assertIn(str(path), result.stderr)
        self.assertEqual(json.loads(result.stdout)["extensions"], [])
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
                                                  "label": "demo.piperd", "extensions": ["demo"]})
        self.assertNotIn("token", info.stdout)
        self.stub("curl", "exit 1")
        self.assertFalse(json.loads(self.run_sh("status", "--json").stdout)["up"])


if __name__ == "__main__":
    unittest.main()
