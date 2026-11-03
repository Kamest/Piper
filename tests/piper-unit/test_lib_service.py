"""lib-service.sh on launchd: a restart must survive bootout finishing after it returned.

`launchctl bootout` returns while launchd still tears the job down. A bootstrap in that window
fails with "5: Input/output error" and a kickstart finds no job, so `task piper:restart` left
piperd down (2026-10-27, 2026-11-01). A fake launchctl keeps the job "unloading" for a few calls.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import _env

ROOT = Path(__file__).resolve().parents[2]

FAKE_LAUNCHCTL = r'''#!/bin/sh
# state: $S/loaded (job loaded, running pid 4242), $S/tearing (calls left until bootout completes)
S=$FAKE_STATE
echo "$*" >> "$S/calls"
tick() {   # one call during the teardown; the job is gone after the last one
  n=$(cat "$S/tearing"); n=$((n - 1))
  if [ "$n" -le 0 ]; then rm -f "$S/tearing"; else echo "$n" > "$S/tearing"; fi
}
case $1 in
  bootout)
    [ -f "$S/loaded" ] || exit 113
    rm -f "$S/loaded"; echo "$FAKE_TEARDOWN" > "$S/tearing"; exit 0 ;;
  print)
    if [ -f "$S/tearing" ]; then tick; echo "state = exiting"; exit 0; fi
    [ -f "$S/loaded" ] || { echo "Could not find service" >&2; exit 113; }
    echo "state = running"; echo "pid = 4242"; exit 0 ;;
  bootstrap)
    if [ -f "$S/tearing" ]; then tick; echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi
    [ -f "$S/loaded" ] && { echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; }
    touch "$S/loaded"; exit 0 ;;
  kickstart)
    [ -f "$S/tearing" ] && { tick; exit 113; }
    [ -f "$S/loaded" ] || { echo "Could not find service" >&2; exit 113; }
    exit 0 ;;
esac
exit 64
'''


class LaunchdRaceTest(unittest.TestCase):
    def run_lib(self, script, teardown=4, loaded=True):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "bin").mkdir()
            state = root / "state"
            state.mkdir()
            if loaded:
                (state / "loaded").touch()
            fake = root / "bin/launchctl"
            fake.write_text(FAKE_LAUNCHCTL)
            fake.chmod(0o700)
            env = dict(os.environ, FAKE_STATE=str(state), FAKE_TEARDOWN=str(teardown), HOME=str(root),
                       SVC_WAIT_STEPS="10", PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            body = f'. "{ROOT}/scripts/lib-service.sh"\nsvc_kind() {{ echo launchd; }}\n{script}\n'
            result = subprocess.run(["sh", "-c", body], env=env, text=True, capture_output=True, timeout=30)
            calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
            return result, (state / "loaded").exists(), calls

    def test_restart_waits_out_a_slow_bootout_and_ends_loaded(self):
        result, loaded, calls = self.run_lib('svc_stop dev.example.piperd && svc_start dev.example.piperd && svc_pid dev.example.piperd')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(loaded, calls)
        self.assertEqual(result.stdout.strip(), "4242")
        self.assertEqual([c.split()[0] for c in calls].count("bootstrap"), 1, calls)   # only once the job is gone

    def test_start_retries_a_bootstrap_refused_during_teardown(self):
        # stop gave up waiting (job still unloading): start keeps trying instead of a single kickstart
        result, loaded, calls = self.run_lib('svc_stop dev.example.piperd; svc_start dev.example.piperd', teardown=14)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + "\n".join(calls))
        self.assertTrue(loaded, calls)

    def test_start_of_a_running_job_does_not_reload_it(self):
        result, loaded, calls = self.run_lib('svc_start dev.example.piperd')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(loaded)
        self.assertEqual([c.split()[0] for c in calls], ["print", "print"])

    def test_a_job_that_never_loads_is_reported(self):
        result, loaded, _ = self.run_lib('svc_stop dev.example.piperd; svc_start dev.example.piperd', teardown=1000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not start", result.stderr)


if __name__ == "__main__":
    unittest.main()
