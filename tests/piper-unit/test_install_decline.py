"""install.sh remembers an explicit terminal "no" to "Load extension ...?" in the core's declined list (the
first `piper` command and the desktop app then do not ask again); no terminal records nothing. Runs with the
fixture of test_install_sh. The decline cases need a core whose extfind has `set_declined`;
PIPER_INSTALL_CORE names another core directory (its piper/ and scripts/) to test against."""
import fcntl
import json
import os
import select
import shutil
import subprocess
import termios
import time
import unittest
from pathlib import Path

import _env  # noqa: F401  (require the isolated OS test sandbox)
import test_install_sh as base

CORE = Path(os.environ.get("PIPER_INSTALL_CORE") or base.ROOT)
HAS_DECLINE = "def set_declined" in (CORE / "piper/extfind.py").read_text()


@unittest.skipUnless(base.SCRIPT.is_file(), "install.sh exists only in a Piper checkout")
class InstallDeclineTest(unittest.TestCase):
    setUp_fixture = base.InstallShTest.setUp
    stub = base.InstallShTest.stub
    install_env = base.InstallShTest.install_env
    run_install = base.InstallShTest.run_install
    extension = base.InstallShTest.extension
    trusts = base.InstallShTest.trusts

    def setUp(self):
        self.setUp_fixture()
        for name in ("extensions.py", "extfind.py"):
            shutil.copyfile(CORE / "piper" / name, self.checkout / "piper" / name)
        for name in ("piper.sh", "lib-service.sh"):
            shutil.copyfile(CORE / "scripts" / name, self.checkout / "scripts" / name)
        self.stub("hole", base.HOLE)

    def declined(self):
        path = self.home / ".piper/declined-extensions.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def answer(self, text):
        """Run the installer on a pseudo-terminal and answer every [y/N] with `text`; the output."""
        master, slave = os.openpty()

        def terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        process = subprocess.Popen(["sh", str(self.checkout / "install.sh"), "--no-open"], env=self.install_env(),
                                   cwd=self.home, stdin=slave, stdout=slave, stderr=slave, preexec_fn=terminal)
        os.close(slave)
        output, asked, deadline = b"", 0, time.monotonic() + 20
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
                    if output.count(b"[y/N]") > asked:
                        asked = output.count(b"[y/N]")
                        os.write(master, (text + "\n").encode())
                if process.poll() is not None:
                    break
            self.assertEqual(process.wait(timeout=2), 0, output.decode())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
        return output.decode()

    def test_no_terminal_records_no_decline(self):
        path = self.extension(registered=True, api=1)
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.trusts(), {})
        self.assertEqual(self.declined(), {})
        self.assertIn("extensions trust", r.stderr)
        self.assertIn(str(path), r.stderr)

    @unittest.skipUnless(HAS_DECLINE, "the core has no declined list (set PIPER_INSTALL_CORE to a newer core)")
    @unittest.skipUnless(_env.pty_available(), "PTYs unavailable")
    def test_terminal_no_is_remembered_and_not_asked_again(self):
        path = self.extension()
        text = self.answer("n")
        self.assertEqual(text.count("Load extension"), 1, text)
        self.assertEqual(self.trusts(), {})
        self.assertIn(str(path), self.declined())
        text = self.answer("y")
        self.assertNotIn("Load extension", text)
        self.assertIn("extensions trust", text)
        self.assertEqual(self.trusts(), {})
        # --yes does not override a remembered "no" for a registered extension either
        config = self.cfg / "piper/extensions.d"
        config.mkdir(parents=True, exist_ok=True)
        (config / "demo.json").write_text(json.dumps({"path": str(path)}))
        r = self.run_install("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.trusts(), {})
        # an explicit --extension still trusts it and clears the "no"
        r = self.run_install("--extension", str(path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str(path), self.trusts())
        self.assertNotIn(str(path), self.declined())

    @unittest.skipUnless(HAS_DECLINE, "the core has no declined list (set PIPER_INSTALL_CORE to a newer core)")
    @unittest.skipUnless(_env.pty_available(), "PTYs unavailable")
    def test_terminal_yes_records_no_decline(self):
        path = self.extension()
        self.answer("y")
        self.assertIn(str(path), self.trusts())
        self.assertEqual(self.declined(), {})

    @unittest.skipIf(HAS_DECLINE, "only for a core without the declined list")
    @unittest.skipUnless(_env.pty_available(), "PTYs unavailable")
    def test_older_core_terminal_no_still_works(self):
        path = self.extension()
        text = self.answer("n")
        self.assertEqual(text.count("Load extension"), 1, text)
        self.assertEqual(self.trusts(), {})
        self.assertFalse((self.home / ".piper/declined-extensions.json").exists())
        self.assertIn(str(path), text)


if __name__ == "__main__":
    unittest.main()
