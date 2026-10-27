"""Exercise the real Piper restart command with disposable service stand-ins."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import _env

ROOT = Path(__file__).resolve().parents[2]


class PiperRestartTest(unittest.TestCase):
    def restart(self, kind, start_fails=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "scripts").mkdir()
            (root / "bin").mkdir()
            (root / "scripts/piper.sh").write_bytes((ROOT / "scripts/piper.sh").read_bytes())
            (root / "piper").mkdir()   # piper.sh discovers extensions with it (none here)
            for name in ("extfind.py", "extensions.py"):
                (root / "piper" / name).write_bytes((ROOT / "piper" / name).read_bytes())
            (root / "scripts/lib-service.sh").write_text('''
svc_kind() { printf '%s\\n' "$TEST_SERVICE_KIND"; }
svc_installed() { [ "$1" = piperd ]; }
svc_stop() { printf 'stop %s\\n' "$1" >> "$TEST_SERVICE_LOG"; }
svc_start() { printf 'start %s\\n' "$1" >> "$TEST_SERVICE_LOG"; return "$TEST_START_RC"; }
svc_restart() { printf 'restart %s\\n' "$1" >> "$TEST_SERVICE_LOG"; }
''')
            curl = root / "bin/curl"
            curl.write_text("#!/bin/sh\nexit 0\n")
            curl.chmod(0o700)
            log = root / "service.log"
            env = dict(os.environ, TEST_SERVICE_KIND=kind, TEST_SERVICE_LOG=str(log),
                       TEST_START_RC="7" if start_fails else "0",
                       PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            env.pop("PIPER_SERVICE_LABEL", None)
            result = subprocess.run(["sh", str(root / "scripts/piper.sh"), "restart"],
                                    env=env, text=True, capture_output=True, timeout=5)
            return result, log.read_text().splitlines()

    def test_launchd_restart_reloads_definition_instead_of_kickstarting_stale_launcher(self):
        result, calls = self.restart("launchd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["stop piperd", "start piperd"])

    def test_systemd_keeps_its_normal_restart(self):
        result, calls = self.restart("systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["restart piperd"])

    def test_failed_reload_is_not_reported_as_success(self):
        result, calls = self.restart("launchd", start_fails=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("restarted; sessions survive", result.stderr)


if __name__ == "__main__":
    unittest.main()
