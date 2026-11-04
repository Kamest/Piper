"""scripts/piper.sh without an extension and with a trusted one: the neutral service label, old services
(PIPER_LEGACY_LABEL or a trusted extension's legacy_labels) and `restart` installing the service once while
one of those is installed or the service still names PIPER_EXTENSIONS (9.167). launchctl / systemctl are stubs that log their calls."""
import functools
import glob
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
# start waits for one (d4e9969c); systemctl the same (enable/start/restart, disable/stop, show MainPID). One
# piperd for all labels: the stand-in answers `up` while it is loaded ($STUB_LOG.loaded) or `.foreign` exists
STUB = ('#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\n'
        'case "$(basename "$0") $1 $2" in\n'
        '  "launchctl bootstrap"*) : > "$STUB_LOG.loaded" ;;\n'
        '  "launchctl bootout"*) rm -f "$STUB_LOG.loaded" ;;\n'
        '  "launchctl print"*) [ -f "$STUB_LOG.loaded" ] || exit 113; echo "	pid = 4242" ;;\n'
        '  "systemctl --user enable"|"systemctl --user start"|"systemctl --user restart") : > "$STUB_LOG.loaded" ;;\n'
        '  "systemctl --user disable"|"systemctl --user stop") rm -f "$STUB_LOG.loaded" ;;\n'
        '  "systemctl --user show") if [ -f "$STUB_LOG.loaded" ]; then echo 4242; else echo 0; fi ;;\n'
        'esac\nexit 0\n')


class CliHarness(unittest.TestCase):
    UNAME = None   # "Darwin" / "Linux": lib-service.sh's svc_kind sees that system (launchd / systemd stubs)

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
        if getattr(self, "UNAME", None):
            with open(os.path.join(self.bin, "uname"), "w") as f:
                f.write("#!/bin/sh\necho %s\n" % self.UNAME)
            os.chmod(os.path.join(self.bin, "uname"), 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        # `up` asks for /piper/icon.svg: a stand-in piperd answers it
        www = os.path.join(self.tmp, "www", "piper")
        os.makedirs(www)
        open(os.path.join(www, "icon.svg"), "w").close()
        handler = functools.partial(QuietHandler, directory=os.path.dirname(www))
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.srv.gate = os.path.join(self.tmp, "calls.log")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.env = {"HOME": os.path.join(self.tmp, "home"), "PATH": self.bin + ":/usr/bin:/bin",
                    "PIPER_HOME": os.path.join(self.tmp, "piper-home"), "PIPER_PORT": str(self.srv.server_address[1]),
                    "PROJECT_PATH": self.tmp, "STUB_LOG": self.log, "XDG_CONFIG_HOME": os.path.join(self.tmp, "cfg")}
        os.makedirs(self.env["HOME"])

    def running(self, on=True, what=".loaded"):
        """The stand-in piperd: answers (a service of the stubs runs it) or not; `.foreign` = nothing known runs it."""
        path = self.log + what
        if on:
            open(path, "w").close()
        elif os.path.exists(path):
            os.unlink(path)

    def run_sh(self, *args, checkout=SK, **env):
        return subprocess.run(["sh", os.path.join(checkout, "scripts", "piper.sh"), *args], env=dict(self.env, **env),
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


class PiperCliTest(CliHarness):
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

    def do_GET(self):   # `up`: only while the stubs run piperd (CliHarness.running)
        gate = getattr(self.server, "gate", "")
        if gate and self.path.endswith("/icon.svg") and not any(os.path.exists(gate + x) for x in (".loaded", ".foreign")):
            self.send_error(404)
            return
        super().do_GET()


class StateHandler(QuietHandler):
    """The stand-in piperd's /api/state: server.state (a dict, or an int = that HTTP status)."""
    def do_GET(self):
        if self.path != "/api/state":
            return super().do_GET()
        self.server.asked.append(self.headers.get("Authorization", ""))
        state = self.server.state
        body = json.dumps(state if isinstance(state, dict) else {"error": "x"}).encode()
        self.send_response(state if isinstance(state, int) else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UpdatedDaemonTest(CliHarness):
    """9.171: open / ensure / status restart a piperd that runs older code than its checkout - once."""
    UNAME = "Darwin"   # restarts() counts launchd calls: the same on a Linux host (CI)
    def setUp(self):
        super().setUp()
        self.srv.RequestHandlerClass = functools.partial(StateHandler, directory=os.path.join(self.tmp, "www"))
        self.srv.state, self.srv.asked = 404, []
        os.makedirs(self.env["PIPER_HOME"])   # a running piperd has its home
        self.install_fake("piperd")
        self.running()

    def restarts(self):
        return self.calls().count("launchctl bootout")

    def test_no_restart_when_the_daemon_runs_the_checkouts_code(self):
        self.srv.state = {"code_build": "a", "code_disk": "a", "code_outdated": False, "gui_build": "g"}
        for args in (("status",), ("ensure",), ("status", "--json")):
            r = self.run_sh(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("Piper was updated", r.stderr)
        self.assertEqual(self.restarts(), 0)
        self.assertTrue(self.srv.asked and all(a.startswith("Bearer ") for a in self.srv.asked))
        json.loads(r.stdout)   # status --json: stdout stays JSON
        for state in (401, 500):   # no answer to trust: say nothing, restart nothing
            self.srv.state = state
            self.assertNotIn("Piper was updated", self.run_sh("ensure").stderr)
        self.assertEqual(self.restarts(), 0)
        r = self.run_sh("ensure", PIPER_NO_AUTO_RESTART="1")
        self.assertEqual(self.restarts(), 0)

    def test_an_outdated_daemon_is_restarted_once_per_update(self):
        self.srv.state = {"code_build": "a", "code_disk": "b", "code_outdated": True}
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Piper was updated - restarting the service (sessions keep running)", r.stderr)
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(r.stdout.count("piperd answers on"), 1)
        r = self.run_sh("ensure")   # the same answer again: that restart did not help - no loop
        self.assertNotIn("Piper was updated", r.stderr)
        self.assertEqual(self.restarts(), 1)
        self.srv.state = {"code_build": "a", "code_disk": "c", "code_outdated": True}   # another pull
        self.assertIn("Piper was updated", self.run_sh("ensure").stderr)
        self.assertEqual(self.restarts(), 2)
        self.srv.state = {"code_build": "c", "code_disk": "c", "code_outdated": False}
        self.run_sh("ensure")
        self.assertFalse(os.path.exists(os.path.join(self.env["PIPER_HOME"], "update-restart")))

    def test_a_daemon_from_before_the_update_is_restarted_and_open_print_stays_the_url(self):
        self.srv.state = {"gui_build": "g", "sessions": []}   # master's piperd: no code_outdated at all
        r = self.run_sh("open", "--print")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"\Ahttp://127\.0\.0\.1:\d+/#token=[^\s]+\n\Z")   # the URL alone
        self.assertIn("Piper was updated", r.stderr)
        self.assertEqual(self.restarts(), 1)
        r = self.run_sh("open", "--print")
        self.assertNotIn("Piper was updated", r.stderr)
        self.assertEqual(self.restarts(), 1)

    def test_an_old_legacy_service_with_nothing_trusted_moves_to_piperd(self):
        """An old install: an extension inside the checkout, its legacy service installed, nothing trusted, no
        terminal (the desktop app): the CLI trusts it (no question), restarts = install-service onto piperd."""
        os.unlink(self.service_file("piperd"))
        ext = legacy = None   # the checkout's own extension with legacy_labels (none in a core-only checkout)
        for m in sorted(glob.glob(os.path.join(SK, "*", "piper-extension.json"))):
            with open(m) as f:
                labels = json.load(f).get("legacy_labels") or []
            if labels:
                ext, legacy = os.path.dirname(m), labels[0]
                break
        if not ext:
            self.skipTest("no extension with legacy_labels inside this checkout")
        import plistlib   # the old service's PATH (a shell one): the desktop's short PATH has no hole
        old = self.install_fake(legacy, plistlib.dumps({"Label": legacy, "EnvironmentVariables": {
            "PATH": self.bin + ":/opt/shell/bin:/usr/bin:/bin"}}).decode())
        self.srv.state = {"gui_build": "g", "sessions": []}
        r = self.run_sh("ensure")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Piper ran as the service %s" % legacy, r.stderr)
        self.assertIn("installing piperd instead", r.stderr)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(self.service_file("piperd")))
        with open(os.path.join(self.env["PIPER_HOME"], "trusted-extensions.json")) as f:
            self.assertIn(os.path.realpath(ext), json.load(f))
        self.assertIn("keeps the PATH of %s" % legacy, r.stderr)
        with open(self.service_file("piperd"), "rb") as f:
            self.assertIn("/opt/shell/bin", plistlib.load(f)["EnvironmentVariables"]["PATH"])


class OldServiceCases:
    """9.172 (a real upgrade run on macOS and Linux): the old service of the checkout's own extension is replaced
    whatever that extension's trust; `ensure` replaces a stopped one; install-service never runs a second job
    next to a piperd it does not know; the old service's interpreter and baked variables are kept."""
    def setUp(self):
        super().setUp()
        self.srv.RequestHandlerClass = functools.partial(StateHandler, directory=os.path.join(self.tmp, "www"))
        self.srv.state, self.srv.asked = {"gui_build": "g", "sessions": []}, []   # a piperd from before 9.171
        os.makedirs(self.env["PIPER_HOME"])
        self.oldpy = os.path.join(self.tmp, "old-python", "bin", "python3")
        os.makedirs(os.path.dirname(self.oldpy))
        os.symlink(sys.executable, self.oldpy)

    def old_service(self, label, env, python=None):
        """The old service as lib-service.sh wrote it (plist / unit): `python3 piperd.py` with baked variables."""
        argv = [python or self.oldpy, "/old/checkout/piper/piperd.py"]
        if self.UNAME == "Darwin":
            import plistlib
            text = plistlib.dumps({"Label": label, "ProgramArguments": argv, "EnvironmentVariables": env,
                                   "KeepAlive": True}).decode()
        else:
            text = "[Unit]\nDescription=%s\n[Service]\nExecStart=%s\n%sRestart=always\nKillMode=process\n" % (
                label, " ".join('"%s"' % a for a in argv) + " ",
                "".join('Environment="%s=%s"\n' % kv for kv in env.items()))
        return self.install_fake(label, text)

    def new_service(self):
        """(argv, env) of the installed `piperd`."""
        path = self.service_file("piperd")
        if self.UNAME == "Darwin":
            import plistlib
            with open(path, "rb") as f:
                data = plistlib.load(f)
            return data["ProgramArguments"], data["EnvironmentVariables"]
        import re
        import shlex
        with open(path) as f:
            text = f.read()
        argv = shlex.split(re.search(r"^ExecStart=(.*)$", text, re.M).group(1))
        return argv, dict(re.findall(r'^Environment="([A-Za-z_0-9]+)=(.*)"$', text, re.M))

    def checkout(self):
        """A core checkout (only what the CLI runs) with its own extension `acme-ext` naming an old label."""
        co = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(co, "scripts"))
        os.makedirs(os.path.join(co, "piper"))
        for rel in ("scripts/piper.sh", "scripts/lib-service.sh"):
            shutil.copy(os.path.join(SK, rel), os.path.join(co, rel))
        for f in glob.glob(os.path.join(SK, "piper", "*.py")):   # codebuild's check imports common & co
            shutil.copy(f, os.path.join(co, "piper"))
        ext = os.path.join(co, "acme-ext")
        os.makedirs(ext)
        with open(os.path.join(ext, "piper-extension.json"), "w") as f:
            json.dump({"name": "acme", "label": "Acme", "api": 2, "service_env": ["ACME_DIR"],
                       "legacy_labels": ["old.acme"]}, f)
        with open(os.path.join(ext, "extension.py"), "w") as f:
            f.write("API = 2\n")
        return co, ext

    def assert_moved(self, r, old):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("installing piperd instead", r.stderr)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(self.service_file("piperd")))
        self.assertNotIn("run 'piper install-service'", r.stderr)   # the same run does exactly that

    def test_b1_a_declined_checkout_extension_still_replaces_its_old_running_service(self):
        co, ext = self.checkout()
        r = self.run_sh("extensions", "decline", ext, checkout=co)
        self.assertEqual(r.returncode, 0, r.stderr)
        old = self.old_service("old.acme", {"PATH": self.bin + ":/usr/bin:/bin", "ACME_DIR": "/acme",
                                            "PIPER_ALLOWED_HOSTS": "box.example.ts.net", "PIPER_EXTENSIONS": "/x"})
        self.running()
        r = self.run_sh("status", checkout=co)
        self.assert_moved(r, old)
        self.assertNotIn("did not finish", r.stderr)
        argv, env = self.new_service()
        self.assertEqual(argv[0], self.oldpy, "F-D1: the old service's python3, not this shell's")
        self.assertIn("keeps the python3 of old.acme", r.stderr)
        self.assertEqual(env.get("ACME_DIR"), "/acme", "baked variables of the old service are kept")
        self.assertEqual(env.get("PIPER_ALLOWED_HOSTS"), "box.example.ts.net")
        self.assertNotIn("PIPER_EXTENSIONS", env)   # piperd follows the trust file (9.167)
        self.assertFalse(os.path.exists(os.path.join(self.env["PIPER_HOME"], "trusted-extensions.json")))
        self.assertFalse(os.path.exists(os.path.join(self.env["PIPER_HOME"], "piperd.pid")))

    def test_b1_with_piper_no_extensions_the_old_service_is_replaced_too(self):
        co, _ = self.checkout()
        old = self.old_service("old.acme", {"PATH": self.bin + ":/usr/bin:/bin"})
        self.running()
        r = self.run_sh("restart", checkout=co, PIPER_NO_EXTENSIONS="1")
        self.assert_moved(r, old)
        _, env = self.new_service()
        self.assertNotIn("ACME_DIR", env)

    def test_b2_ensure_installs_the_service_instead_of_a_bare_piperd_while_the_old_one_is_stopped(self):
        old = self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin"})
        r = self.run_sh("ensure", PIPER_LEGACY_LABEL="old.piperd")
        self.assert_moved(r, old)
        self.assertNotIn("piperd started (pid", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.env["PIPER_HOME"], "piperd.pid")))

    def test_status_names_the_old_service_and_stop_stops_it(self):
        self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin"})
        self.running()
        r = self.run_sh("status", PIPER_LEGACY_LABEL="old.piperd", PIPER_NO_AUTO_RESTART="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the old service old.piperd installed", r.stdout)
        self.assertNotIn("no login service", r.stdout)
        self.assertIn("run 'piper install-service'", r.stderr)   # nothing replaces it in this run
        r = self.run_sh("stop", PIPER_LEGACY_LABEL="old.piperd", PIPER_NO_AUTO_RESTART="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("service old.piperd) stopped", r.stdout)

    def test_a_vanished_old_interpreter_falls_back_to_this_python3(self):
        old = self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin"}, python=os.path.join(self.tmp, "gone", "python3"))
        r = self.run_sh("install-service", PIPER_LEGACY_LABEL="old.piperd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(old))
        argv, _ = self.new_service()
        self.assertEqual(argv[0], os.path.join(self.bin, "python3"))
        self.assertNotIn("keeps the python3", r.stderr)

    def test_a_reinstall_of_piperd_keeps_its_own_interpreter(self):
        """F-D1b: Settings > Reinstall service from a piperd the desktop app started (its bundled runtime first
        on PATH) bakes the interpreter the installed piperd already had, not the caller's python3."""
        self.old_service("piperd", {"PATH": self.bin + ":/usr/bin:/bin"})
        r = self.run_sh("install-service")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.new_service()[0][0], self.oldpy)

    def test_the_old_services_port_and_variables_follow_a_value_set_here_wins(self):
        """Linux-B1: a shell without PIPER_PORT (the desktop app) - the service keeps the old port, and the CLI
        waits for it there; a variable this shell sets is baked with its value, one it lacks with the old one."""
        port = self.env["PIPER_PORT"]
        old = self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin", "PIPER_PORT": port,
                                              "FOO_DIR": "/old", "BAR_DIR": "/bar"})
        r = self.run_sh("install-service", PIPER_LEGACY_LABEL="old.piperd", PIPER_PORT="", FOO_DIR="/new")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(old))
        self.assertIn("piperd is up on http://127.0.0.1:%s" % port, r.stderr)
        _, env = self.new_service()
        self.assertEqual((env.get("PIPER_PORT"), env.get("FOO_DIR"), env.get("BAR_DIR")), (port, "/new", "/bar"))

    def test_the_old_services_project_path_beats_the_default_not_a_value_set_here(self):
        """An old service with its own projects folder (Settings > Environment): a shell without PROJECT_PATH
        (the desktop app) keeps it - the checkout's parent is only a default; one set here still wins."""
        old = self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin", "PROJECT_PATH": "/old/projects"})
        r = self.run_sh("install-service", PIPER_LEGACY_LABEL="old.piperd", PROJECT_PATH="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.new_service()[1].get("PROJECT_PATH"), "/old/projects")
        self.old_service("old.piperd", {"PATH": self.bin + ":/usr/bin:/bin", "PROJECT_PATH": "/old/projects"})
        r = self.run_sh("install-service", PIPER_LEGACY_LABEL="old.piperd", PROJECT_PATH="/mine")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.new_service()[1].get("PROJECT_PATH"), "/mine")

    def test_install_never_starts_a_second_job_next_to_a_piperd_it_does_not_know(self):
        self.running(what=".foreign")   # e.g. an old service under a label nobody named
        r = self.run_sh("install-service")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no service known here runs it", r.stderr)
        self.assertFalse(os.path.exists(self.service_file("piperd")))
        self.assertNotRegex(self.calls(), r"bootstrap|enable")


class OldServiceLaunchdTest(OldServiceCases, CliHarness):
    UNAME = "Darwin"


class OldServiceSystemdTest(OldServiceCases, CliHarness):
    UNAME = "Linux"


class UpdateTest(CliHarness):
    """9.173 `piper update` (the desktop app's tray "Update Piper"): fast-forward the checkout, restart a running
    piperd onto it; local changes / a diverged branch / no git: a message, nothing changed."""
    UNAME = "Darwin"
    GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_NOSYSTEM": "1"}

    def git(self, cwd, *args):
        r = subprocess.run(["git", "-C", cwd, *args], env=dict(self.env, **self.GIT), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def setUp(self):
        super().setUp()
        # origin: the published Piper; co: the user's install (a clone, as install.sh makes it)
        self.origin = os.path.join(self.tmp, "origin")
        os.makedirs(os.path.join(self.origin, "scripts"))
        os.makedirs(os.path.join(self.origin, "piper"))
        for rel in ("scripts/piper.sh", "scripts/lib-service.sh"):
            shutil.copy(os.path.join(SK, rel), os.path.join(self.origin, rel))
        for f in glob.glob(os.path.join(SK, "piper", "*.py")):
            shutil.copy(f, os.path.join(self.origin, "piper"))
        self.git(self.origin, "init", "-q", "-b", "main")
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-q", "-m", "one")
        self.co = os.path.join(self.tmp, "co")
        r = subprocess.run(["git", "clone", "-q", self.origin, self.co], env=dict(self.env, **self.GIT), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def publish(self, name="NEWS"):
        with open(os.path.join(self.origin, name), "w") as f:
            f.write(name)
        self.git(self.origin, "add", "-A")
        self.git(self.origin, "commit", "-q", "-m", name)
        return self.git(self.origin, "rev-parse", "--short", "HEAD")

    def update(self):
        return self.run_sh("update", checkout=self.co, **self.GIT)

    def test_up_to_date_changes_nothing(self):
        head = self.git(self.co, "rev-parse", "--short", "HEAD")
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "Piper is up to date (%s)" % head)
        self.assertEqual(self.calls(), "")

    def test_a_running_piperd_is_restarted_onto_the_update(self):
        os.makedirs(self.env["PIPER_HOME"])
        self.install_fake("piperd")
        self.running()
        before = self.git(self.co, "rev-parse", "--short", "HEAD")
        after = self.publish()
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Piper updated %s -> %s" % (before, after), r.stdout)
        self.assertEqual(self.git(self.co, "rev-parse", "--short", "HEAD"), after)
        self.assertEqual(self.calls().count("launchctl bootout"), 1)
        self.assertIn("restarted; sessions survive", r.stderr)
        self.assertNotIn("stop-all", self.calls())

    def test_a_stopped_piperd_is_left_stopped(self):
        after = self.publish()
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-> %s" % after, r.stdout)
        self.assertIn("piperd is not running - the next start uses the update", r.stderr)
        self.assertNotRegex(self.calls(), r"bootstrap|bootout|kickstart")

    def test_a_diverged_checkout_is_not_touched(self):
        self.publish("THEIRS")
        with open(os.path.join(self.co, "MINE"), "w") as f:
            f.write("mine")
        self.git(self.co, "add", "-A")
        self.git(self.co, "commit", "-q", "-m", "mine")
        mine = self.git(self.co, "rev-parse", "HEAD")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("git pull --ff-only failed", r.stderr)
        self.assertIn("nothing was changed", r.stderr)
        self.assertEqual(self.git(self.co, "rev-parse", "HEAD"), mine)
        self.assertTrue(os.path.exists(os.path.join(self.co, "MINE")))

    def test_an_install_without_git_says_so(self):
        plain = os.path.join(self.tmp, "plain")
        shutil.copytree(self.co, plain, ignore=shutil.ignore_patterns(".git"))
        r = self.run_sh("update", checkout=plain, GIT_CEILING_DIRECTORIES=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a git checkout", r.stderr)

    def test_usage_lists_update(self):
        r = self.run_sh("no-such-command")
        self.assertEqual(r.returncode, 2)
        self.assertIn("piper.sh update", r.stdout)
        self.assertIn("piper.sh uninstall-service", r.stdout)


if __name__ == "__main__":
    unittest.main()


class RestartPreflightTest(UpdatedDaemonTest):
    def test_bad_checkout_never_stops_the_running_service_or_consumes_auto_restart(self):
        co = os.path.join(self.tmp, "incomplete-checkout")
        for directory in ("piper", "scripts"):
            os.makedirs(os.path.join(co, directory))
        for rel in ("scripts/piper.sh", "scripts/lib-service.sh"):
            shutil.copy(os.path.join(SK, rel), os.path.join(co, rel))
        for source in glob.glob(os.path.join(SK, "piper", "*.py")):
            shutil.copy(source, os.path.join(co, "piper"))
        self.srv.state = {"code_build": "old", "code_disk": "new", "code_outdated": True}
        bad = os.path.join(co, "piper", "incomplete.py")
        with open(bad, "w") as out:
            out.write("<<<<<<< HEAD\nprivate_example = 1\n=======\n>>>>>>> example\n")
        result = self.run_sh("restart", checkout=co)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("update is not ready", result.stderr)
        self.assertNotIn("private_example", result.stderr)
        self.assertEqual(self.restarts(), 0)
        self.assertTrue(os.path.exists(self.log + ".loaded"))
        result = self.run_sh("status", checkout=co)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("piperd answers on", result.stdout)
        self.assertEqual(self.restarts(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.env["PIPER_HOME"], "update-restart")))
        with open(bad, "w") as out:
            out.write("pass\n")
        os.mkdir(os.path.join(co, ".git"))
        merge = os.path.join(co, ".git", "MERGE_HEAD")
        with open(merge, "w") as out:
            out.write("a" * 40 + "\n")
        self.assertNotEqual(self.run_sh("restart", checkout=co).returncode, 0)
        self.assertEqual(self.restarts(), 0)
        os.unlink(merge)
        result = self.run_sh("status", checkout=co)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.restarts(), 1, "a completed update is still eligible for its automatic restart")
