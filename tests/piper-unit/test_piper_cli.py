"""scripts/piper.sh without an extension and with a wrapper's settings: the neutral service
label, an old service named only by the wrapper, and `restart` installing the service again when it was
installed without the wrapper's extension. launchctl / systemctl are stubs that log their calls."""
import functools
import http.server
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)

SK = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
STUB = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 0\n'


class PiperCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(dir=_env.ROOT)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name in ("launchctl", "systemctl"):
            with open(os.path.join(self.bin, name), "w") as f:
                f.write(STUB)
            os.chmod(os.path.join(self.bin, name), 0o755)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.log = os.path.join(self.tmp, "calls.log")
        # `up` asks for /piper/icon.svg: a stand-in piperd answers it
        www = os.path.join(self.tmp, "www", "piper")
        os.makedirs(www)
        open(os.path.join(www, "icon.svg"), "w").close()
        handler = functools.partial(QuietHandler, directory=os.path.dirname(www))
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.env = {"HOME": os.path.join(self.tmp, "home"), "PATH": self.bin + ":/usr/bin:/bin",
                    "PIPER_HOME": os.path.join(self.tmp, "piper-home"), "PIPER_PORT": str(self.srv.server_address[1]),
                    "PROJECT_PATH": self.tmp, "STUB_LOG": self.log, "XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg")}
        os.makedirs(self.env["HOME"])

    def run_sh(self, *args, **env):
        return subprocess.run(["sh", os.path.join(SK, "scripts", "piper.sh"), *args], env=dict(self.env, **env),
                              capture_output=True, text=True, timeout=60, cwd=self.tmp)   # sh's here-documents need a writable cwd here

    def service_file(self, label):
        r = subprocess.run(["sh", "-c", '. "$1"; svc_file "$2"', "sh", os.path.join(SK, "scripts", "lib-service.sh"), label],
                           env=self.env, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()

    def install_fake(self, label, text="installed by hand"):
        path = self.service_file(label)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def calls(self):
        try:
            with open(self.log) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_default_service_label_is_neutral(self):
        self.install_fake("piperd")
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("service piperd installed", r.stdout)

    def test_old_service_warned_only_when_a_wrapper_names_it(self):
        self.install_fake("old.piperd")
        self.assertNotIn("old login service", self.run_sh("status").stderr)
        r = self.run_sh("status", PIPER_LEGACY_LABEL="old.piperd")
        self.assertIn("the old login service old.piperd", r.stderr)

    def test_install_still_removes_a_single_legacy_label(self):
        older = self.install_fake("old.piperd")
        result = self.run_sh("install-service", PIPER_LEGACY_LABEL="old.piperd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(older))
        self.assertTrue(os.path.isfile(self.service_file("piperd")))

    def test_install_removes_multiple_legacy_labels_and_skips_current_label(self):
        current = self.install_fake("acme.piperd")
        older = [self.install_fake(label) for label in ("old.piperd", "older.piperd")]
        result = self.run_sh("install-service", PIPER_SERVICE_LABEL="acme.piperd",
                             PIPER_LEGACY_LABEL="old.piperd acme.piperd older.piperd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.isfile(current))
        for path in older:
            self.assertFalse(os.path.exists(path))
        self.assertNotIn("removed the old service acme.piperd", result.stderr)

    def test_restart_reinstalls_a_service_installed_without_the_extension(self):
        ext = os.path.join(self.tmp, "ext")
        path = self.install_fake("acme.piperd")
        r = self.run_sh("restart", PIPER_SERVICE_LABEL="acme.piperd", PIPER_EXTENSIONS=ext,
                        PIPER_SERVICE_ENV="ACME_DIR", ACME_DIR="/acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("installed without %s" % ext, r.stderr)
        with open(path) as f:
            text = f.read()
        self.assertIn(ext, text)
        self.assertIn("/acme", text)   # PIPER_SERVICE_ENV: the wrapper's own names are kept too
        # installed with it: a plain restart
        open(self.log, "w").close()
        r = self.run_sh("restart", PIPER_SERVICE_LABEL="acme.piperd", PIPER_EXTENSIONS=ext)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("installed without", r.stderr)
        self.assertRegex(self.calls(), r"kickstart|restart|bootstrap")   # launchd reloads with bootout + bootstrap
        # no extension: never reinstalled
        open(self.log, "w").close()
        r = self.run_sh("restart", PIPER_SERVICE_LABEL="acme.piperd")
        self.assertNotIn("installed without", r.stderr)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


if __name__ == "__main__":
    unittest.main()
