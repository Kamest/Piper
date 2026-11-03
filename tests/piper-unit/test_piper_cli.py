"""scripts/piper.sh without an extension and with a trusted one: the neutral service label, old services
(PIPER_LEGACY_LABEL or a trusted extension's legacy_labels) and `restart` installing the service once while
one of those is installed or the service still names PIPER_EXTENSIONS (9.167). launchctl / systemctl are stubs that log their calls."""
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)

SK = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# launchctl keeps a loaded job: bootstrap loads it, bootout unloads it, print names its pid - lib-service.sh's
# start waits for one (d4e9969c)
STUB = ('#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
        'case "$(basename "$0") $1" in\n'
        '  "launchctl bootstrap") : > "$STUB_LOG.loaded" ;;\n'
        '  "launchctl bootout") rm -f "$STUB_LOG.loaded" ;;\n'
        '  "launchctl print") [ -f "$STUB_LOG.loaded" ] || exit 113; echo "	pid = 4242" ;;\n'
        'esac\nexit 0\n')


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

    def trusted_extension(self, **manifest):
        """An API 2 extension the user trusted (9.167), in a throwaway PIPER_HOME."""
        ext = os.path.join(self.tmp, "ext")
        os.makedirs(ext)
        with open(os.path.join(ext, "piper-extension.json"), "w") as f:
            json.dump(dict({"name": "acme", "label": "Acme", "api": 2}, **manifest), f)
        with open(os.path.join(ext, "extension.py"), "w") as f:
            f.write("API = 2\n")
        r = self.run_sh("extensions", "trust", ext)
        self.assertEqual(r.returncode, 0, r.stderr)
        return ext

    def test_restart_moves_a_legacy_service_of_a_trusted_extension_to_piperd_once(self):
        ext = self.trusted_extension(service_env=["ACME_DIR"], legacy_labels=["acme.piperd"])
        old = self.install_fake("acme.piperd", "<key>PIPER_EXTENSIONS</key>")
        self.assertIn("the old login service acme.piperd", self.run_sh("status").stderr)
        r = self.run_sh("restart", ACME_DIR="/acme")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("installing piperd instead", r.stderr)
        self.assertIn("removed the old service acme.piperd", r.stderr)
        self.assertFalse(os.path.exists(old))
        with open(self.service_file("piperd")) as f:
            text = f.read()
        self.assertIn("/acme", text)            # service_env: the extension's names are kept
        self.assertNotIn("PIPER_EXTENSIONS", text)   # piperd follows the trust file
        self.assertNotIn(ext, text)
        open(self.log, "w").close()
        r = self.run_sh("restart")   # moved: a plain restart
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("installing", r.stderr)
        self.assertRegex(self.calls(), r"kickstart|restart|bootstrap")

    def test_restart_reinstalls_a_service_that_still_names_its_extensions(self):
        path = self.install_fake("piperd", "<key>PIPER_EXTENSIONS</key><string>/x</string>")
        r = self.run_sh("restart")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("names its extensions itself", r.stderr)
        with open(path) as f:
            self.assertNotIn("PIPER_EXTENSIONS", f.read())
        # an explicit PIPER_EXTENSIONS in the shell is never baked in any more
        r = self.run_sh("install-service", PIPER_EXTENSIONS="/somewhere")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(path) as f:
            self.assertNotIn("/somewhere", f.read())


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


if __name__ == "__main__":
    unittest.main()
