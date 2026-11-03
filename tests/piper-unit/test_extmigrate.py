"""9.171: an old install's service starts the updated piperd (reboot / crash after a pull, nothing trusted):
the extension inside the checkout whose legacy_labels name the service label is trusted once, audited and loaded
- never when declined, already trusted something, overridden by $PIPER_EXTENSIONS, outside the checkout or
under another label. The CLI does the same for an installed legacy service. piperd under an old label of a
loaded extension reinstalls itself as `piperd` once (only when the service manager's pid is its own)."""
import functools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401
import common
import extapi
import extensions
import extfind

PIPER = Path(extfind.__file__).resolve().parent


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.checkout = self.tmp / "checkout"
        (self.checkout / "piper").mkdir(parents=True)
        self.env = {"HOME": str(self.tmp / "home"), "PIPER_HOME": str(self.tmp / "state"),
                    "XDG_CONFIG_HOME": str(self.tmp / "cfg"), "PROJECT_PATH": str(self.tmp)}
        self.enterContext(mock.patch.dict(os.environ, self.env))
        for k in ("PIPER_EXTENSIONS", "PIPER_NO_EXTENSIONS", "PIPER_SERVICE_LABEL", "XPC_SERVICE_NAME"):
            os.environ.pop(k, None)
        self.ext = self.extension(self.checkout / "acme-ext")

    def extension(self, path, name="acme", labels=("old.acme", "older.acme")):
        path.mkdir(parents=True, exist_ok=True)
        (path / "extension.py").write_text("import os\nAPI = 2\nNAME = %r\nSEEN = os.environ.get('ACME_PATH')\n" % name)
        (path / "acme.env").write_text("ACME_PATH=/from/env-file\n")
        (path / "piper-extension.json").write_text(json.dumps(
            {"name": name, "label": "Acme " + name, "api": 2, "env_file": "acme.env", "legacy_labels": list(labels)}))
        return path

    def migrate(self, labels):
        return extfind.migrate_legacy(labels, str(self.checkout))

    def test_label_sources_env_launchd_and_systemd_cgroup(self):
        cg = self.tmp / "cgroup"
        cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/old.acme.service\n")
        self.assertEqual(extfind.running_labels({}, str(cg)), ["old.acme"])
        # user@1000.service is not the last component: only the unit itself counts
        cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/term.scope\n")
        self.assertEqual(extfind.running_labels({}, str(cg)), [])
        self.assertEqual(extfind.running_labels({"XPC_SERVICE_NAME": "old.acme"}, str(self.tmp / "none")), ["old.acme"])
        self.assertEqual(extfind.running_labels({"PIPER_SERVICE_LABEL": "x.y", "XPC_SERVICE_NAME": "0"}, str(self.tmp / "none")),
                         ["x.y", "0"])
        self.assertEqual(extfind.running_labels({"XPC_SERVICE_NAME": "a b;rm"}, str(self.tmp / "none")), [])

    def test_trusts_the_checkout_extension_once_when_the_label_matches(self):
        self.assertEqual(self.migrate(["other.label", "0"]), [])
        self.assertEqual(extfind.trusted(), {})
        done = self.migrate(["older.acme"])
        self.assertEqual([e["path"] for e in done], [str(self.ext.resolve())])
        self.assertEqual(done[0]["label_matched"], "older.acme")
        record = extfind.trusted()[str(self.ext.resolve())]
        self.assertEqual(record["name"], "acme")   # the record of `piper extensions trust`
        self.assertEqual(self.migrate(["older.acme"]), [])   # trusted now: nothing more

    def test_an_old_service_that_loaded_it_through_piper_extensions_trusts_only_that_one(self):
        """9.172 (public CI): an older wrapper baked $PIPER_EXTENSIONS into the old service; piperd moves to the
        service piperd without it, so the extension that variable names is trusted - another one is not."""
        with mock.patch.dict(os.environ, {"PIPER_EXTENSIONS": str(self.tmp / "elsewhere")}):
            self.assertEqual(self.migrate(["old.acme"]), [])
        self.assertEqual(extfind.trusted(), {})
        with mock.patch.dict(os.environ, {"PIPER_EXTENSIONS": str(self.ext)}):
            done = self.migrate(["old.acme"])
        self.assertEqual([e["path"] for e in done], [str(self.ext.resolve())])

    def test_never_when_declined_something_trusted_overridden_or_outside_the_checkout(self):
        extfind.set_declined(str(self.ext.resolve()), True)
        self.assertEqual(self.migrate(["old.acme"]), [])
        extfind.set_declined(str(self.ext.resolve()), False)
        other = self.extension(self.tmp / "elsewhere" / "other", "other", labels=("x.other",))
        extfind.set_trust(str(other), True)   # the user chose an extension: no migration at all
        self.assertEqual(self.migrate(["old.acme"]), [])
        extfind.set_trust(str(other), False)
        os.unlink(extfind.declined_file())
        with mock.patch.dict(os.environ, {"PIPER_EXTENSIONS": ""}):
            self.assertEqual(self.migrate(["old.acme"]), [])
        with mock.patch.dict(os.environ, {"PIPER_NO_EXTENSIONS": "1"}):
            self.assertEqual(self.migrate(["old.acme"]), [])
        # a registration outside the checkout (a sandbox could not write the checkout, maybe ~/.config) is not enough
        shutil.rmtree(self.ext)
        reg = Path(self.env["XDG_CONFIG_HOME"]) / "piper/extensions.d"
        reg.mkdir(parents=True)
        (reg / "x.json").write_text(json.dumps({"path": str(self.extension(self.tmp / "registered" / "acme"))}))
        self.assertEqual(self.migrate(["old.acme"]), [])
        self.assertEqual(extfind.trusted(), {})

    def test_piperd_loader_migrates_audits_and_applies_env_file(self):
        """extensions.load() with MIGRATE (piperd sets it): in-process with the checkout patched in."""
        audit = Path(common.AUDIT_FILE)
        before = audit.read_text() if audit.exists() else ""
        real = extfind.migrate_legacy
        with mock.patch.object(extfind, "migrate_legacy", lambda labels: real(labels, str(self.checkout))), \
                mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "old.acme"}), \
                mock.patch.object(extensions, "MIGRATED", []):
            extensions._migrate(extfind)
            self.assertEqual([e["path"] for e in extensions.MIGRATED], [str(self.ext.resolve())])
        lines = [json.loads(x) for x in audit.read_text()[len(before):].splitlines()]
        self.assertEqual([(x["event"], x["label"], x["who"]) for x in lines], [("extension-migrated", "old.acme", "piperd")])
        self.assertTrue(extfind.trusted())

    def run_loader(self, migrate, **env):
        """A fresh process with this checkout's loader copied into the fake checkout (its parents[1])."""
        for name in ("extensions.py", "extfind.py"):
            shutil.copy(PIPER / name, self.checkout / "piper" / name)
        code = ("import json, os, sys, extensions; extensions.MIGRATE = %r; extensions.load(); "
                "m = sys.modules.get('piper_extension_0'); "
                "print(json.dumps([extensions.names(), os.environ.get('ACME_PATH'), getattr(m, 'SEEN', None)]))" % migrate)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(self.tmp),
                           env=dict(os.environ, PYTHONPATH=str(self.checkout / "piper"), **env))
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout), r.stderr

    def test_a_fresh_piperd_start_under_the_old_label_loads_the_extension_with_its_env(self):
        out, _ = self.run_loader(False, XPC_SERVICE_NAME="old.acme")   # the CLI's status --json: never
        self.assertEqual(out, [[], None, None])
        self.assertEqual(extfind.trusted(), {})
        out, _ = self.run_loader(True, XPC_SERVICE_NAME="unrelated")
        self.assertEqual(out, [[], None, None])
        out, err = self.run_loader(True, PIPER_SERVICE_LABEL="old.acme")
        self.assertEqual(out, [["acme"], "/from/env-file", "/from/env-file"])   # env_file before the import
        self.assertIn("now trusted", err)
        out, err = self.run_loader(True, PIPER_SERVICE_LABEL="old.acme")   # trusted: loads, no second migration
        self.assertEqual(out[0], ["acme"])
        self.assertNotIn("now trusted", err)

    def test_cli_trusts_for_an_installed_legacy_service_without_a_terminal(self):
        for name in ("extensions.py", "extfind.py"):
            shutil.copy(PIPER / name, self.checkout / "piper" / name)

        def delegate():
            return subprocess.run([sys.executable, str(self.checkout / "piper" / "extfind.py"), "--delegate", "--sh"],
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL, env=os.environ.copy())
        r = delegate()
        self.assertEqual(r.returncode, 0, r.stderr)
        # 9.172: the checkout's own extension names its old labels before (and without) any trust
        self.assertIn("EXT_LEGACY_LABELS='old.acme older.acme'", r.stdout)
        self.assertEqual(extfind.trusted(), {})
        unit = Path(self.env["XDG_CONFIG_HOME"]) / "systemd/user/older.acme.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\n")
        r = delegate()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("EXT_LEGACY_LABELS='old.acme older.acme'", r.stdout)
        self.assertIn("Piper ran as the service older.acme", r.stderr)
        self.assertNotIn("Use it?", r.stderr)
        audit = [json.loads(x) for x in (Path(self.env["PIPER_HOME"]) / "audit.log").read_text().splitlines()]
        self.assertEqual([(a["event"], a["who"], a["label"], a["path"]) for a in audit],
                         [("extension-migrated", "cli", "older.acme", str(self.ext.resolve()))])   # 9.172
        self.assertNotIn("Piper ran as", delegate().stderr)   # once
        # declined: the installed service is no consent
        extfind.set_trust(str(self.ext.resolve()), False)   # untrust records a decline
        self.assertNotIn("Piper ran as", delegate().stderr)
        self.assertEqual(extfind.trusted(), {})

    def test_piperd_reinstalls_itself_as_piperd_only_as_that_services_own_process(self):
        with mock.patch.object(extensions, "DIRS", [str(self.ext.resolve())]), \
                mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "old.acme"}):
            with mock.patch.object(extfind, "service_main_pid", return_value=os.getpid()):
                self.assertEqual(extapi.legacy_service_label(), "old.acme")
                with mock.patch.dict(os.environ, {"PIPER_SERVICE_LABEL": "old.acme"}):   # the target itself
                    self.assertEqual(extapi.legacy_service_label(), "")
            # a child inherited the label: launchd / systemd run another pid under it
            with mock.patch.object(extfind, "service_main_pid", return_value=os.getpid() + 1):
                self.assertEqual(extapi.legacy_service_label(), "")
                self.assertEqual(extapi.migrate_service(0), "")
            spawned = []
            with mock.patch.object(extfind, "service_main_pid", return_value=os.getpid()), \
                    mock.patch.object(extapi, "reinstall_service", spawned.append), \
                    mock.patch.object(extapi.threading, "Thread", lambda target, **kw: mock.Mock(start=target)):
                self.assertEqual(extapi.migrate_service(0), "old.acme")
            self.assertEqual(spawned, ["piperd"])
        with mock.patch.object(extensions, "DIRS", []), mock.patch.dict(os.environ, {"XPC_SERVICE_NAME": "old.acme"}), \
                mock.patch.object(extfind, "service_main_pid", return_value=os.getpid()):
            with mock.patch.object(extfind, "checkout_legacy_labels", return_value=[]):
                self.assertEqual(extapi.legacy_service_label(), "")   # not loaded, not in the checkout: unknown
            # 9.172: an extension inside the checkout names it, loaded or not (declined): it moves all the same
            with mock.patch.object(extfind, "checkout_legacy_labels", return_value=["old.acme"]):
                self.assertEqual(extapi.legacy_service_label(), "old.acme")

    def test_the_self_reinstall_waits_while_a_session_is_starting(self):
        """9.172: after a reboot the restore card's Continue may start sessions in the piperd still running under
        the old label - its reinstall (which ends it) waits until no session is in its start, bounded."""
        answers = iter([3, 1, 0])
        asked, spawned = [], []

        def busy():
            asked.append(1)
            return next(answers)
        with mock.patch.object(extapi, "legacy_service_label", return_value="old.acme"), \
                mock.patch.object(extapi, "reinstall_service", spawned.append), \
                mock.patch.object(extapi.time, "sleep"), \
                mock.patch.object(extapi.threading, "Thread", lambda target, **kw: mock.Mock(start=target)):
            self.assertEqual(extapi.migrate_service(0, busy=busy), "old.acme")
            self.assertEqual((len(asked), spawned), (3, ["piperd"]))
            # bounded: a session stuck in its start never blocks the move for good
            spawned.clear()
            self.assertEqual(extapi.migrate_service(0, busy=lambda: 1, wait_max=0), "old.acme")
            self.assertEqual(spawned, ["piperd"])

    def test_manager_counts_sessions_in_their_start(self):
        import sessions
        m = _env.manager(self)
        self.addCleanup(m.sessions.clear)
        for sid, state in (("a", "preflight"), ("b", "starting"), ("c", "attesting"), ("d", "working"),
                           ("e", "exited"), ("f", "ready")):
            m.sessions[sid] = sessions.Session(id=sid, project="team/app", state=state)
        self.assertEqual(m.starting(), 3)


if __name__ == "__main__":
    unittest.main()
