"""install.sh, the one-command install of the public Piper repository: run from the checkout with stub hole,
Docker and curl in a throwaway HOME, without a terminal (as under `curl | sh` in CI)."""
import json
import fcntl
import os
import shlex
import shutil
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

import _env  # noqa: F401  (require the isolated OS test sandbox)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
if not SCRIPT.is_file():
    SCRIPT = Path(os.environ.get("PIPER_INSTALL_SCRIPT") or SCRIPT)   # a public checkout's install.sh
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
        self.stub("curl", "#!/bin/sh\nexit 1\n")
        self.checkout = self.tmp / "projects/core"
        for directory in ("piper", "scripts", "examples"):
            (self.checkout / directory).mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.checkout / "install.sh")
        for name in ("extensions.py", "extfind.py"):
            shutil.copyfile(ROOT / "piper" / name, self.checkout / "piper" / name)
        for name in ("piper.sh", "lib-service.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.checkout / "scripts" / name)
        (self.checkout / "piper/piperd.py").write_text("# checkout marker\n")
        (self.checkout / "piper/doctor.py").write_text("print('ready')\n")
        (self.checkout / "examples/hole-settings.json").write_text('{"files": {}}\n')
        self.cfg = self.tmp / "cfg"
        self.trace = self.tmp / "wrapper.log"

    def stub(self, name, body, where=None):
        p = (where or self.bin) / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    def install_env(self):
        return {"HOME": str(self.home), "PATH": "%s:/usr/bin:/bin" % self.bin, "PIPER_HOME": str(self.home / ".piper"),
               "TMPDIR": tempfile.gettempdir(), "PROJECT_PATH": str(self.checkout.parent),
               "XDG_CONFIG_HOME": str(self.cfg), "PIPER_PORT": "19999", "TRACE": str(self.trace)}

    def run_install(self, *args, open_gui=False):
        # a new session has no controlling terminal: ask() must not wait for an answer
        argv = ["sh", str(self.checkout / "install.sh"), *([] if open_gui else ["--no-open"]), *args]
        # cwd: as `curl | sh` from the user's home; a read-only cwd breaks sh's here-documents
        return subprocess.run(argv, env=self.install_env(), cwd=self.home, capture_output=True, text=True,
                              timeout=120, start_new_session=True)

    def test_copies_the_example_settings_and_runs_the_doctor(self):
        self.stub("hole", HOLE)
        r = self.run_install()
        self.assertIn("checking this machine", r.stderr)
        settings = self.home / ".hole/settings.json"
        self.assertEqual(settings.read_text(), (self.checkout / "examples/hole-settings.json").read_text())
        if r.returncode:
            self.assertIn("fix the FAIL lines above", r.stderr)
        else:
            self.assertIn("done. Start Piper with: sh %s/scripts/piper.sh open" % self.checkout, r.stderr)

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

    def extension(self, name="demo", registered=False, api=1):
        path = self.checkout.parent / (name + " repo") / "extension"
        path.mkdir(parents=True)
        (path / "piper-extension.json").write_text(json.dumps(
            {"name": name, "label": "Demo " + name, "api": api, "wrapper": "wrapper.sh"}))
        (path / "wrapper.sh").write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$TRACE"\n')
        (path / "wrapper.sh").chmod(0o755)
        if registered:
            config = self.cfg / "piper/extensions.d"
            config.mkdir(parents=True, exist_ok=True)
            (config / (name + ".json")).write_text(json.dumps({"path": str(path)}))
        return path

    def trusts(self):
        path = self.home / ".piper/trusted-extensions.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_noninteractive_registered_needs_consent_and_prints_command(self):
        self.stub("hole", HOLE)
        path = self.extension(registered=True)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.trusts(), {})
        self.assertIn("Demo demo", r.stderr)
        self.assertIn("extensions trust " + shlex.quote(str(path)), r.stderr)
        self.assertFalse(self.trace.exists())

    def test_yes_accepts_registered_and_remaining_commands_use_wrapper(self):
        self.stub("hole", HOLE)
        path = self.extension(registered=True)
        r = self.run_install("--yes", "--service", open_gui=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(path), self.trusts())
        self.assertEqual(self.trace.read_text().splitlines(), ["doctor", "install-service", "open"])
        r = self.run_install("--yes")
        self.assertNotIn("trusting extension", r.stderr)

    def test_yes_never_accepts_scanned_extension(self):
        self.stub("hole", HOLE)
        path = self.extension()
        r = self.run_install("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.trusts(), {})
        self.assertIn("remote: unknown, commit: unknown", r.stderr)
        self.assertIn(str(path), r.stderr)
        self.assertFalse(self.trace.exists())

    def test_explicit_extension_accepts_scan_and_undiscovered_path(self):
        self.stub("hole", HOLE)
        path = self.extension()
        outside = self.tmp / "outside/deeper/extension"
        outside.parent.mkdir(parents=True)
        shutil.move(str(path), outside)
        r = self.run_install("--extension", str(outside))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(outside), self.trusts())
        self.assertEqual(self.trace.read_text().splitlines(), ["doctor"])

    def test_no_extensions_skips_discovery_and_previously_trusted_wrapper(self):
        self.stub("hole", HOLE)
        path = self.extension(registered=True)
        self.run_install("--extension", str(path))
        self.trace.unlink()
        before = self.trusts()
        r = self.run_install("--yes", "--no-extensions")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.trusts(), before)
        self.assertNotIn("Extension Demo", r.stderr)
        self.assertFalse(self.trace.exists())

    def test_incompatible_or_invalid_explicit_path_fails_before_doctor(self):
        self.stub("hole", HOLE)
        path = self.extension(api=99)
        r = self.run_install("--extension", str(path))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("incompatible", r.stderr)
        self.assertNotIn("checking this machine", r.stderr)
        self.assertEqual(self.trusts(), {})
        self.assertNotEqual(self.run_install("--extension", str(self.tmp / "missing")).returncode, 0)

    @unittest.skipUnless(_env.pty_available(), "PTYs unavailable")
    def test_terminal_scan_asks_once_with_metadata_and_respects_answer(self):
        self.stub("hole", HOLE)
        for answer in ("n", "y"):
            with self.subTest(answer=answer):
                path = self.extension(name=answer)
                master, slave = os.openpty()
                def terminal():
                    os.setsid()
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                process = subprocess.Popen(["sh", str(self.checkout / "install.sh"), "--no-open", "--yes"],
                                           env=self.install_env(), cwd=self.home, stdin=slave, stdout=slave, stderr=slave,
                                           preexec_fn=terminal)
                os.close(slave)
                output = b""
                answered = False
                deadline = time.monotonic() + 20
                try:
                    while time.monotonic() < deadline:
                        if select.select([master], [], [], .1)[0]:
                            try:
                                chunk = os.read(master, 65536)
                            except OSError:
                                break
                            if not chunk:
                                break
                            output += chunk
                            if b"[y/N]" in output and not answered:
                                os.write(master, (answer + "\n").encode())
                                answered = True
                        if process.poll() is not None:
                            break
                    self.assertEqual(process.wait(timeout=2), 0, output.decode())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                    os.close(master)
                text = output.decode()
                self.assertEqual(text.count("Load extension"), 1, text)
                self.assertIn("remote: unknown, commit: unknown", text)
                self.assertIn(str(path), text)
                self.assertEqual(str(path) in self.trusts(), answer == "y")
                shutil.rmtree(path.parent)

    def test_conflicting_extension_flags_fail_with_message(self):
        r = self.run_install("--extension", "/example", "--no-extensions")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot be combined", r.stderr)



if __name__ == "__main__":
    unittest.main()
