"""install.sh, the one-command install of the public Piper repository: run from the checkout with stub hole,
Docker and curl in a throwaway HOME, without a terminal (as under `curl | sh` in CI)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _env  # noqa: F401  (require the isolated OS test sandbox)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
HOLE = "#!/bin/sh\necho 'hole 2.4.0'\n"


@unittest.skipUnless(SCRIPT.is_file(), "install.sh exists only in a Piper checkout")
class InstallShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.symlink(sys.executable, self.bin / "python3")
        os.symlink(shutil.which("git"), self.bin / "git")
        self.stub("docker", "#!/bin/sh\nexit 0\n")

    def stub(self, name, body, where=None):
        p = (where or self.bin) / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    def run_install(self, *args):
        env = {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin, "PIPER_HOME": str(self.home / ".piper"),
               "TMPDIR": tempfile.gettempdir()}
        # a new session has no controlling terminal: ask() must not wait for an answer
        return subprocess.run(["sh", str(SCRIPT), "--no-open", *args], env=env, capture_output=True, text=True,
                              timeout=120, start_new_session=True)

    def test_copies_the_example_settings_and_runs_the_doctor(self):
        self.stub("hole", HOLE)
        r = self.run_install()
        self.assertIn("checking this machine", r.stderr)
        settings = self.home / ".hole/settings.json"
        self.assertEqual(settings.read_text(), (ROOT / "examples/hole-settings.json").read_text())
        if r.returncode:
            self.assertIn("fix the FAIL lines above", r.stderr)
        else:
            self.assertIn("done. Start Piper with: sh %s/scripts/piper.sh open" % ROOT, r.stderr)

    def test_never_overwrites_existing_hole_settings(self):
        self.stub("hole", HOLE)
        (self.home / ".hole").mkdir()
        mine = self.home / ".hole/settings.json"
        mine.write_text('{"files": {}}\n')
        r = self.run_install()
        self.assertEqual(mine.read_text(), '{"files": {}}\n', r.stderr)
        self.assertNotIn("wrote ~/.hole/settings.json", r.stderr)

    def test_missing_hole_without_a_terminal_downloads_nothing(self):
        marker = self.tmp / "curl-called"
        self.stub("curl", "#!/bin/sh\ntouch %s\n" % marker)
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Piper needs hole: curl -fsSL https://raw.githubusercontent.com/lukashornych/hole/main/install.sh",
                      r.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / ".hole/settings.json").exists())

    def test_yes_runs_the_hole_installer_and_puts_local_bin_on_path(self):
        # the "installer" curl prints, as hole's does: put hole into ~/.local/bin
        installer = self.tmp / "hole-install.sh"
        installer.write_text('mkdir -p "$HOME/.local/bin"\ncp %s "$HOME/.local/bin/hole"\n' % self.stub("hole", HOLE, self.tmp))
        self.stub("curl", "#!/bin/sh\ncat %s\n" % installer)
        r = self.run_install("--yes")
        self.assertTrue((self.home / ".local/bin/hole").is_file(), r.stderr)
        self.assertIn("checking this machine", r.stderr)
        self.assertIn("hole is in ~/.local/bin, which is not on your PATH", r.stderr)

    def test_old_python_stops_before_anything_is_written(self):
        os.unlink(self.bin / "python3")
        self.stub("python3", "#!/bin/sh\nexit 1\n")
        self.stub("hole", HOLE)
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Piper needs Python 3.10 or newer", r.stderr)
        self.assertFalse((self.home / ".hole").exists())

    def test_no_running_docker_names_the_runtimes(self):
        self.stub("docker", "#!/bin/sh\nexit 1\n")
        self.stub("podman", "#!/bin/sh\nexit 1\n")
        self.stub("hole", HOLE)
        r = self.run_install()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no running Docker or Podman", r.stderr)

    def test_unknown_option_fails_with_usage(self):
        r = self.run_install("--bogus")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown option --bogus", r.stderr)
        self.assertIn("--dir DIR", r.stderr)


if __name__ == "__main__":
    unittest.main()
