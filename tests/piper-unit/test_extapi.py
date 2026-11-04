"""9.167 Settings > Extensions over piperd: who may turn an extension on (only this computer's GUI; a remote
GUI or a phone leaves a request), turning off for everyone, agents / attach / scoped tokens / a remote
environment's proxy header refused, the audit, the self-restart and the detached service reinstall."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first)
import common
import extapi
import extensions
import extfind
import piperd
from test_piperd import FakeManager, HOST, request

REMOTE_HOST = "piper.example"
DISCOVER = extfind.discover


def call(method, path, token=None, body=None, host=HOST, extra=""):
    raw = json.dumps(body).encode() if body is not None else b""
    hdr = "%s %s HTTP/1.1\r\nHost: %s\r\n" % (method, path, host)
    if method == "POST":
        hdr += "Content-Type: application/json\r\nContent-Length: %d\r\n" % len(raw)
    if token:
        hdr += "Authorization: Bearer %s\r\n" % token
    status, headers, data = request((hdr + extra + "Connection: close\r\n\r\n").encode() + raw)
    try:
        return status, json.loads(data or b"null")
    except ValueError:
        return status, data


class ExtensionsApiTest(unittest.TestCase):
    def setUp(self):
        piperd.MANAGER = FakeManager()
        piperd.MANAGER.bump = lambda: None
        self.tmp = Path(tempfile.mkdtemp(dir=_env.ROOT))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.enterContext(mock.patch.dict(os.environ, {"PIPER_ALLOWED_HOSTS": REMOTE_HOST,
                                                       "XDG_CONFIG_HOME": str(self.tmp / "cfg")}))
        self.enterContext(mock.patch.object(extfind, "trust_file", lambda: self.tmp / "state/trusted-extensions.json"))
        self.enterContext(mock.patch.object(extfind, "offerable", lambda checkout=None: []))
        extapi.PENDING.clear()
        self.addCleanup(extapi.PENDING.clear)
        self.ext = self.extension(self.tmp / "ext")
        self.audit = []
        self.enterContext(mock.patch.object(common, "audit", self.audit.append))
        self.gui = common.own_token("gui")

    def extension(self, path, name="demo", api=2):
        path.mkdir(parents=True)
        (path / "extension.py").write_text("API = 2\nMODULES = [('demo', 'Demo stack', 'help')]\n")
        (path / "piper-extension.json").write_text(json.dumps(
            {"name": name, "label": "Demo " + name, "api": api, "description": "A demo."}))
        return path.resolve()

    def discover(self):
        return DISCOVER(project_path=str(self.tmp / "none"), checkout=str(self.tmp / "core"))

    def test_local_gui_turns_on_audited_and_the_list_shows_the_contract(self):
        with mock.patch.object(extfind, "discover", lambda *a, **k: self.discover()):
            status, data = call("POST", "/api/extensions/trust", self.gui, {"path": str(self.ext)})
            self.assertEqual((status, data), (200, {"ok": True, "path": str(self.ext), "restart": True}))
            self.assertIn(str(self.ext), extfind.trusted())
            self.assertEqual(self.audit[-1]["event"], "extension-trust")
            self.assertEqual(self.audit[-1]["who"], "local")
            self.assertEqual(self.audit[-1]["path"], str(self.ext))
            status, data = call("GET", "/api/extensions", self.gui)
        self.assertEqual(status, 200, data)
        self.assertEqual(set(data), {"extensions", "pending", "offer", "core_api", "apis", "source",
                                     "restart_needed", "caller", "can_approve"})
        self.assertEqual((data["caller"], data["can_approve"], data["core_api"]), ("local", True, extensions.API))
        entry = next(e for e in data["extensions"] if e["path"] == str(self.ext))
        self.assertEqual(set(entry), {"name", "label", "description", "path", "remote", "commit", "source", "api",
                                      "compatible", "trusted", "state", "error", "adds", "trusted_remote",
                                      "restart_needed"})
        self.assertEqual((entry["state"], entry["trusted"], entry["description"]), ("found", True, "A demo."))
        # registration by the GUI's Add folder: source "added"
        other = self.extension(self.tmp / "elsewhere/deep/x", "other")
        status, data = call("POST", "/api/extensions/add", self.gui, {"path": str(other)})
        self.assertEqual(status, 200, data)
        self.assertNotIn(str(other), extfind.trusted())   # adding is no trust
        found = {e["path"]: e for e in self.discover()}
        self.assertEqual(found[str(other)]["source"], "added")

    def test_remote_gui_and_phone_only_request_the_local_gui_approves(self):
        remote = common.own_token("remote")
        phone = common.own_token("mobile-bridge")
        remote_hdr = dict(host=REMOTE_HOST)
        status, data = call("POST", "/api/extensions/trust", remote, {"path": str(self.ext)}, **remote_hdr)
        self.assertEqual(status, 202, data)
        req = data["pending"]
        self.assertEqual({k: req[k] for k in ("kind", "path", "label", "from")},
                         {"kind": "trust", "path": str(self.ext), "label": "Demo demo", "from": "remote"})
        self.assertEqual(extfind.trusted(), {})
        # the same asker again is one request; a phone's is its own
        self.assertEqual(call("POST", "/api/extensions/trust", remote, {"path": str(self.ext)}, **remote_hdr)[1]
                         ["pending"]["id"], req["id"])
        status, data = call("POST", "/api/extensions/trust", phone, {"path": str(self.ext)})
        self.assertEqual((status, data["pending"]["from"]), (202, "phone"))
        theirs = data["pending"]
        with mock.patch.object(extfind, "discover", return_value=[]):
            listed = call("GET", "/api/extensions", remote, **remote_hdr)[1]
            self.assertFalse(listed["can_approve"])
            self.assertEqual([(p["id"], p["mine"]) for p in listed["pending"]], [(req["id"], True), (theirs["id"], False)])
            self.assertEqual(set(listed["pending"][0]), {"id", "kind", "path", "label", "remote", "commit", "from",
                                                         "at", "mine"})
            self.assertEqual(call("GET", "/api/state", self.gui)[1]["extension_requests"], 2)
            # neither may approve; each cancels only its own
            self.assertEqual(call("POST", "/api/extensions/approve", remote, {"id": req["id"]}, **remote_hdr)[0], 403)
            self.assertEqual(call("POST", "/api/extensions/approve", phone, {"id": req["id"]})[0], 403)
            self.assertEqual(call("POST", "/api/extensions/cancel", remote, {"id": theirs["id"]}, **remote_hdr)[0], 403)
            self.assertEqual(call("POST", "/api/extensions/cancel", phone, {"id": theirs["id"]})[0], 200)
            self.assertEqual(call("POST", "/api/extensions/approve", self.gui, {"id": theirs["id"]})[0], 404)
            # the local GUI approves the remote one -> trusted, audited as approved
            self.assertEqual(call("POST", "/api/extensions/approve", self.gui, {"id": req["id"]})[0], 200)
        self.assertIn(str(self.ext), extfind.trusted())
        self.assertIn("approved remote request", self.audit[-1]["who"])
        extfind.set_trust(str(self.ext), False)
        # the origin changed between the request and the approval: the approver saw another one -> 409
        def git(*args):
            subprocess.run(["git", "-C", str(self.ext), *args], check=True, capture_output=True)
        git("init", "-q")
        git("config", "remote.origin.url", "https://example.com/team/ext.git")
        req = call("POST", "/api/extensions/trust", remote, {"path": str(self.ext)},
                   host=REMOTE_HOST, extra="Tailscale-User-Login: someone@example.com\r\n")[1]["pending"]
        self.assertEqual((req["remote"], req["from"]), ("https://example.com/team/ext.git", "remote (someone@example.com)"))
        git("config", "remote.origin.url", "https://example.com/other/ext.git")
        status, data = call("POST", "/api/extensions/approve", self.gui, {"id": req["id"]})
        self.assertEqual(status, 409, data)
        self.assertEqual(extfind.trusted(), {})
        self.assertEqual(extapi.PENDING, {})
        extfind.set_trust(str(self.ext), True)
        # turning off is fine from anywhere, and is remembered as declined
        status, data = call("POST", "/api/extensions/untrust", remote, {"path": str(self.ext)}, **remote_hdr)
        self.assertEqual(status, 200, data)
        self.assertEqual(extfind.trusted(), {})
        self.assertIn(str(self.ext), extfind.declined())
        # a remote add is a request too; deny drops it
        req = call("POST", "/api/extensions/add", remote, {"path": str(self.ext)}, **remote_hdr)[1]["pending"]
        self.assertEqual(req["kind"], "add")
        self.assertEqual(call("POST", "/api/extensions/deny", self.gui, {"id": req["id"]})[0], 200)
        self.assertEqual(extapi.PENDING, {})

    def test_agents_attach_scoped_tokens_and_proxied_requests_are_refused(self):
        tty = common.own_token("tty")
        scoped = "scoped-token-fixture"
        with open(common.TOKENS_FILE, "a") as f:
            f.write("%s\tsession:s1\tmcp\n" % scoped)
        import remotes
        with mock.patch.object(remotes, "proxy") as proxy:
            self.assertEqual(call("GET", "/api/extensions")[0], 401)
            for tok in (tty, scoped):
                self.assertEqual(call("GET", "/api/extensions", tok)[0], 403)
                for action in ("trust", "untrust", "add", "approve", "decline"):
                    self.assertEqual(call("POST", "/api/extensions/" + action, tok, {"path": str(self.ext)})[0], 403)
                self.assertEqual(call("POST", "/api/restart", tok, {})[0], 403)
                self.assertEqual(call("POST", "/api/service/reinstall", tok, {})[0], 403)
            env = "X-Piper-Environment: dev\r\n"
            self.assertEqual(call("GET", "/api/extensions", self.gui, extra=env)[0], 403)
            self.assertEqual(call("POST", "/api/extensions/trust", self.gui, {"path": str(self.ext)}, extra=env)[0], 403)
            self.assertEqual(call("POST", "/api/restart", self.gui, {}, extra=env)[0], 403)
            proxy.assert_not_called()
        self.assertEqual(extfind.trusted(), {})

    def test_incomplete_update_refuses_restart_without_stopping_the_api(self):
        error = "Piper update is not ready: finish the merge before restarting."
        piperd.RESTART.clear()
        self.addCleanup(piperd.RESTART.clear)
        with mock.patch.object(piperd.codebuild, "restart_error", return_value=error), \
                mock.patch.object(piperd, "schedule_restart") as restart:
            status, data = call("POST", "/api/restart", self.gui, {})
            self.assertEqual((status, data), (409, {"error": error}))
            restart.assert_not_called()
        self.assertFalse(piperd.RESTART.is_set())
        self.assertFalse(any(e.get("event") == "piperd-restart" for e in self.audit))
        self.assertEqual(call("GET", "/api/state", self.gui)[0], 200)

    def test_bad_paths_and_incompatible_extensions(self):
        for body in ({}, {"path": 1}, {"path": str(self.tmp / "missing")}):
            self.assertEqual(call("POST", "/api/extensions/trust", self.gui, body)[0], 400, body)
        future = self.extension(self.tmp / "future", "future", api=99)
        status, data = call("POST", "/api/extensions/trust", self.gui, {"path": str(future)})
        self.assertEqual(status, 400)
        self.assertIn("incompatible", data["error"])

    def test_decline_hides_the_first_start_offer(self):
        with mock.patch.object(extfind, "offerable", lambda checkout=None: [
                {"path": p} for p in [str(self.ext)] if p not in extfind.declined()]), \
                mock.patch.object(extfind, "discover", return_value=[]):
            self.assertEqual(call("GET", "/api/extensions", self.gui)[1]["offer"], [str(self.ext)])
            self.assertEqual(call("POST", "/api/extensions/decline", common.own_token("remote"),
                                  {"path": str(self.ext)}, host=REMOTE_HOST)[0], 200)
            self.assertEqual(call("GET", "/api/extensions", self.gui)[1]["offer"], [])

    def test_restart_answers_then_stops_the_server_and_reinstall_is_local_and_detached(self):
        stopped = threading.Event()

        class Srv:
            def shutdown(self):
                stopped.set()
        with mock.patch.object(piperd, "SERVER", Srv()):
            piperd.RESTART.clear()
            self.addCleanup(piperd.RESTART.clear)
            status, data = call("POST", "/api/restart", common.own_token("remote"), {}, host=REMOTE_HOST)
            self.assertEqual((status, data), (202, {"ok": True, "restarting": True}))
            self.assertTrue(stopped.wait(5))
            self.assertTrue(piperd.RESTART.is_set())
        self.assertEqual(self.audit[-1], {"event": "piperd-restart", "who": "remote"})
        with mock.patch.object(subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            self.assertEqual(call("POST", "/api/service/reinstall", common.own_token("remote"), {},
                                  host=REMOTE_HOST)[0], 403)
            popen.assert_not_called()
            status, data = call("POST", "/api/service/reinstall", self.gui, {})
            self.assertEqual((status, data), (202, {"ok": True, "started": True}))
            argv = popen.call_args[0][0]
            self.assertEqual(argv[1:], [os.path.join(common.SK, "scripts", "piper.sh"), "install-service"])
            self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_restart_drops_only_the_env_file_values_it_applied(self):
        with mock.patch.dict(os.environ, {"FIXTURE_APPLIED": "x", "FIXTURE_CHANGED": "new"}), \
                mock.patch.dict(extensions.APPLIED_ENV, {"FIXTURE_APPLIED": "x", "FIXTURE_CHANGED": "old"}):
            extapi.drop_applied_env()
            self.assertNotIn("FIXTURE_APPLIED", os.environ)
            self.assertEqual(os.environ["FIXTURE_CHANGED"], "new")


class TrustFilesNeverReachASandbox(unittest.TestCase):
    """9.167 S2.6: trusting = host code at the next start, so a sandbox must never see or write the trust
    files. They live in $PIPER_HOME, which every session forbids (any mode); a mount of it is blocked."""

    def test_trust_files_are_inside_the_forbidden_piper_home(self):
        import guard
        import sessions
        for f in (extfind.trust_file(), extfind.declined_file()):
            self.assertEqual(os.path.dirname(os.path.realpath(f)), os.path.realpath(common.PIPER_HOME))
        m = sessions.Manager.__new__(sessions.Manager)
        m.policy = {}
        s = mock.Mock(host_codex=False)
        with mock.patch.object(sessions.Manager, "host_claude", return_value=False):
            forbidden = sessions.Manager.forbidden(m, s)
        self.assertIn((common.PIPER_HOME, "any"), forbidden)
        # a project whose sandbox config includes $PIPER_HOME (here: the trust file's directory) is blocked
        proj = tempfile.mkdtemp(dir=_env.ROOT)
        os.makedirs(common.PIPER_HOME, exist_ok=True)
        base = {"files": {"include": {common.PIPER_HOME: common.PIPER_HOME}}}
        r = guard.preflight(proj, base, None, {}, {}, forbidden, common.REAL_HOME)
        self.assertIn("protected-mounted", [b["kind"] for b in r["blocks"]])


if __name__ == "__main__":
    unittest.main()
