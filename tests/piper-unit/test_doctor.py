"""Doctor: the rows, their fixes, the K18 checks and the /api/doctor route."""
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import doctor
import features
import piperd
import security
from test_piperd import FakeManager, get, post


_PATCHES = []


def setUpModule():
    """never the machine's gh login (keychain): the rows are built from stand-ins"""
    import github
    _PATCHES.extend([mock.patch.object(github, "token", return_value=None)])
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in _PATCHES:
        p.stop()


def script(body: str) -> str:
    d = tempfile.mkdtemp(prefix="doc-")
    p = os.path.join(d, "bin")
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, stat.S_IRWXU)
    return p


class DoctorTest(unittest.TestCase):
    def by_key(self, rows):
        return {r["key"]: r for r in rows}

    def test_a_checkout_with_an_invalid_name_is_named_not_silently_dropped(self):
        import projects
        pp = tempfile.mkdtemp(prefix="doc-pp-")
        for name in ("good", "my project", "x" * 70):
            os.makedirs(os.path.join(pp, name, ".git"))
        projects._DISCOVERED["at"] = 0.0
        self.addCleanup(projects._DISCOVERED.__setitem__, "at", 0.0)
        with mock.patch.dict(os.environ, {"PROJECT_PATH": pp}):
            r = doctor.check_projects()
        self.assertFalse(r["ok"])
        self.assertIn("Not offered: my project, " + "x" * 70, r["text"])
        self.assertIn("letters, digits", r["text"])
        os.rename(os.path.join(pp, "my project"), os.path.join(pp, "my-project"))
        os.rename(os.path.join(pp, "x" * 70), os.path.join(pp, "x" * 10))
        projects._DISCOVERED["at"] = 0.0
        with mock.patch.dict(os.environ, {"PROJECT_PATH": pp}):
            r = doctor.check_projects()
        self.assertNotIn("Not offered", r["text"])

    def test_hole_version(self):
        for out, ok in (("hole 2.4.0", True), ("hole v2.5.11 (abc)", True), ("hole 2.3.11", True), ("hole 2.1.0", True), ("hole 2.0.9", False), ("hole 1.9.0", False)):
            with mock.patch.dict(os.environ, {"PIPER_HOLE_BIN": script("echo '%s'" % out)}):
                r = doctor.check_hole()
            self.assertEqual(r["ok"], ok, out)
            self.assertEqual(bool(r["fix"]), not ok)
        with mock.patch.dict(os.environ, {"PIPER_HOLE_BIN": "/nonexistent/hole"}):
            r = doctor.check_hole()
        self.assertFalse(r["ok"])
        self.assertEqual(r["level"], "fail")
        self.assertIn("install-service", r["fix"])

    def test_hole_capability_and_deep_mount_match_the_installed_version(self):
        for version, modern, lib_global in (("2.1.0", False, False), ("2.2.0", False, True), ("2.3.0", False, True),
                                            ("2.4.0", True, True)):
            with self.subTest(version=version), mock.patch.dict(os.environ, {
                    "PIPER_HOLE_BIN": script("echo 'hole %s'" % version)}):
                result = doctor.check_hole()
                self.assertTrue(result["ok"])
                self.assertEqual(result["settings_override"], modern)
                # hole 2.2 hides the global files.exclude in libraries/worktrees too (addCheckoutExclusions)
                self.assertEqual(result["library_global_excludes"], lib_global)
                with mock.patch.object(doctor, "_run", return_value=(0, "")) as call:
                    result = doctor.check_deep_mounts(wait=0, legacy=not modern)
                argv = call.call_args.args[0]
                self.assertEqual("--tmpfs" in argv, modern)
                self.assertEqual(sum(a.endswith(":/m/s") for a in argv), int(not modern))
                self.assertTrue(result["ok"])
                self.assertIn("tmpfs" if modern else "host-bind", result["text"])
        with mock.patch.object(doctor, "_run", return_value=(0, "secret")):
            self.assertFalse(doctor.check_deep_mounts(wait=0, legacy=True)["ok"])

    def test_docker_down_is_a_fail_with_a_fix(self):
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": script("echo 'Cannot connect' >&2; exit 1")}), \
                mock.patch.object(doctor, "MAC", True):
            r = doctor.check_docker()
        self.assertFalse(r["ok"])
        self.assertEqual(r["fix"], "open -a Docker")   # F25: the Setup's one action is "Copy: open -a Docker"
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": script("echo 29.8.0")}):
            self.assertTrue(doctor.check_docker()["ok"])

    def test_vmm_from_docker_desktop_settings(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "settings-store.json")
        self.assertIsNone(doctor.check_vmm(p))
        with open(p, "w") as f:
            json.dump({"UseLibkrun": True}, f)
        r = doctor.check_vmm(p)
        self.assertFalse(r["ok"])
        self.assertEqual(r["level"], "warn")   # only the mount test proves the drop
        self.assertIn("Virtual Machine Manager", r["fix"])
        self.assertEqual(r["action"], "deep")
        with open(p, "w") as f:
            json.dump({"UseLibkrun": False, "UseVirtualizationFramework": True}, f)
        self.assertTrue(doctor.check_vmm(p)["ok"])

    def test_deep_mount_test(self):
        # the fake docker lists what it is told: the dropped mount shows the secret
        for out, ok in (("", True), ("secret", False)):
            with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": script("echo '%s'" % out)}):
                r = doctor.check_deep_mounts(wait=0)
            self.assertEqual(r["ok"], ok, out)
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": script("echo 'pull denied' >&2; exit 125")}):
            r = doctor.check_deep_mounts(wait=0)
        self.assertEqual((r["ok"], r["level"]), (False, "warn"))

    def test_no_hole_settings_points_at_the_example(self):
        """9.152: the fix names the example file that ships next to Piper, not a team template."""
        with mock.patch.object(doctor, "PERSONAL_SETTINGS", os.path.join(tempfile.mkdtemp(), "none.json")):
            r = doctor.check_hole_settings()
        self.assertEqual((r["ok"], r["level"]), (False, "warn"))
        self.assertIn("examples/hole-settings.json is a starting point", json.dumps(r))
        self.assertNotIn("team", json.dumps(r))

    def test_legacy_hole_settings(self):
        with open(doctor.PERSONAL_SETTINGS) as f:
            orig = f.read()
        def restore():
            with open(doctor.PERSONAL_SETTINGS, "w") as f:
                f.write(orig)
        self.addCleanup(restore)
        self.assertTrue(doctor.check_hole_settings()["ok"])
        with open(doctor.PERSONAL_SETTINGS, "w") as f:
            json.dump({"network": {"domainWhitelist": ["x"]}}, f)
        r = doctor.check_hole_settings()
        self.assertEqual((r["ok"], r["level"]), (False, "fail"))

    def test_a_dropped_codex_credential_header_is_a_doctor_row_without_its_value(self):
        """R3+ (N3, 2026-10-09): codexcfg drops a credential-looking http_headers entry of the Codex template
        (R3) - silently, so a remote MCP failed in the sandbox with no explanation. The doctor names it
        (server + header name), never the value."""
        import codexcfg
        tpl = os.path.join(codexcfg.TEMPLATE_DIR, "config.toml")
        os.makedirs(codexcfg.TEMPLATE_DIR, exist_ok=True)
        old = open(tpl, "rb").read() if os.path.exists(tpl) else None
        def restore():
            if old is None:
                if os.path.exists(tpl):
                    os.unlink(tpl)
            else:
                with open(tpl, "wb") as f:
                    f.write(old)
        self.addCleanup(restore)
        for text, want in (
                ('[mcp_servers.grafana]\nurl = "https://g.example.test/mcp"\n[mcp_servers.grafana.http_headers]\n'
                 '"X-Region" = "eu"\nAuthorization = "Bearer PLANTED_D1"\n"X-Scope" = "PLANTED_D2xxxxxxxxxxxxxxxxxxxxxx"\n',
                 ["grafana: Authorization", "grafana: X-Scope"]),
                ('[mcp_servers.grafana]\nurl = "https://g.example.test/mcp"\n[mcp_servers.grafana.http_headers]\n'
                 '"X-Region" = "eu"\n', None),
                ("not toml [[[", None)):
            with self.subTest(want=want):
                with open(tpl, "w") as f:
                    f.write(text)
                r = doctor.check_codex_headers()
                if want is None:
                    self.assertIsNone(r)
                    self.assertNotIn("codex-headers", self.by_key(doctor.checks()))
                    continue
                self.assertEqual((r["ok"], r["level"]), (False, "warn"))
                for name in want:
                    self.assertIn(name, r["text"])
                self.assertNotIn("PLANTED_D", json.dumps(r))
                self.assertNotIn("X-Region", r["text"])
                self.assertIn("codex-headers", self.by_key(doctor.checks()))
                self.assertNotIn("PLANTED_D", doctor.text(doctor.report()))
        os.unlink(tpl)
        self.assertIsNone(doctor.check_codex_headers())

    def test_a_skipped_codex_remote_server_is_a_doctor_row_without_its_url(self):
        """2026-10-10 (the user): a remote MCP server of the Codex template whose address is on this machine / a
        private network never reaches a sandbox - silently, so it was just missing. The doctor names it, not its URL."""
        import codexcfg
        tpl = os.path.join(codexcfg.TEMPLATE_DIR, "config.toml")
        os.makedirs(codexcfg.TEMPLATE_DIR, exist_ok=True)
        old = open(tpl, "rb").read() if os.path.exists(tpl) else None
        def restore():
            if old is None:
                if os.path.exists(tpl):
                    os.unlink(tpl)
            else:
                with open(tpl, "wb") as f:
                    f.write(old)
        self.addCleanup(restore)
        for text, want in (
                ('[mcp_servers.lan]\nurl = "http://10.0.0.5:8080/PLANTED_S1"\n[mcp_servers.local]\n'
                 'url = "http://localhost:9000/mcp"\n[mcp_servers.ok]\nurl = "https://mcp.example.test/mcp?author=me"\n',
                 ["lan", "local"]),
                ('[mcp_servers.ok]\nurl = "https://mcp.example.test/mcp"\n[mcp_servers.cmd]\ncommand = "x"\n', None),
                ("not toml [[[", None)):
            with self.subTest(want=want):
                with open(tpl, "w") as f:
                    f.write(text)
                r = doctor.check_codex_servers()
                if want is None:
                    self.assertIsNone(r)
                    self.assertNotIn("codex-servers", self.by_key(doctor.checks()))
                    continue
                self.assertEqual((r["ok"], r["level"]), (False, "warn"))
                for name in want:
                    self.assertIn(name, r["text"])
                listed = r["text"].split("sandbox: ", 1)[1].split(". ", 1)[0].split(", ")
                self.assertEqual(listed, want)   # the public server (a query naming "author") is not listed
                self.assertNotIn("PLANTED_S", json.dumps(r))
                self.assertNotIn("10.0.0.5", json.dumps(r))
                self.assertIn("codex-servers", self.by_key(doctor.checks()))

    def test_module_rows_follow_the_features(self):
        features.save(preset_name="basic")
        self.addCleanup(features.save, preset_name="all")
        keys = set(self.by_key(doctor.checks()))
        self.assertNotIn("gitlab", keys)
        self.assertNotIn("ijhttp", keys)
        features.save(preset_name="all")
        keys = set(self.by_key(doctor.checks()))
        self.assertTrue({"gitlab", "ijhttp", "db", "mattermost"} <= keys)

    def test_report_counts_and_text(self):
        rep = {"rows": [doctor.row("a", "fail", False, "A broken", "why", "do x"),
                        doctor.row("b", "warn", True, "B fine"), doctor.row("c", "info", False, "C off", "", "y")],
               "deep": False}
        t = doctor.text(rep)
        self.assertIn("FAIL A broken - why Fix: do x", t)
        self.assertIn("ok   B fine", t)
        self.assertIn("info C off Fix: y", t)
        r = doctor.report()
        self.assertEqual(r["fail"], sum(1 for x in r["rows"] if not x["ok"] and x["level"] == "fail"))


class DoctorRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        piperd.MANAGER = FakeManager()
        cls.tok = common.mint_token("test")

    def test_image_info_lists_agent_images_newest_first_with_use(self):
        # 9.54 Setup row: hole's agent images and the running sandboxes on each; <none> tags are skipped
        fake = script("""case "$1" in
image) printf 'hole-sandbox/agent-global:f3a82d025ae6\\t6.1GB\\t3 weeks ago\\t2026-09-25 10:00:00 +0200 CEST\\n'
       printf 'hole-sandbox/agent-global:63db1184bb43\\t6.2GB\\t2 days ago\\t2026-10-14 09:00:00 +0200 CEST\\n'
       printf 'hole-sandbox/agent-global:<none>\\t6GB\\t4 weeks ago\\t2026-09-18 09:00:00 +0200 CEST\\n' ;;
ps) printf 'hole-sandbox/agent-global:f3a82d025ae6\\nhole-sandbox/agent-global:f3a82d025ae6\\n' ;;
esac""")
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": fake}):
            r = doctor.image_info()
        self.assertTrue(r["ok"])
        self.assertEqual([i["ref"] for i in r["images"]],
                         ["hole-sandbox/agent-global:63db1184bb43", "hole-sandbox/agent-global:f3a82d025ae6"])
        self.assertEqual([i["in_use"] for i in r["images"]], [0, 2])
        self.assertEqual((r["images"][0]["size"], r["images"][0]["age"], r["in_use"]), ("6.2GB", "2 days ago", 2))
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": script("echo 'Cannot connect' >&2; exit 1")}):
            r = doctor.image_info()
        self.assertEqual(r, {"ok": False, "error": "Cannot connect"})
        with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": "/nonexistent/docker"}):
            self.assertFalse(doctor.image_info()["ok"])

    def test_api_image_and_rebuild(self):
        # 9.54: GET /api/image (Setup row) and POST /api/image/rebuild need the GUI token; the armed
        # rebuild shows in /api/state for the GUI
        self.assertEqual(get("/api/image")[0], 401)
        with mock.patch.object(doctor, "image_info", return_value={"ok": True, "images": [], "in_use": 0}):
            st, _, body = get("/api/image", token=self.tok)
        self.assertEqual((st, json.loads(body)["ok"]), (200, True))
        self.assertEqual(post("/api/image/rebuild", {"on": True})[0], 401)
        self.assertEqual(post("/api/image/rebuild", {"on": "yes"}, token=self.tok)[0], 400)
        st, _, body = post("/api/image/rebuild", {"on": True}, token=self.tok)
        self.assertEqual((st, json.loads(body)), (200, {"image_rebuild": {"at": 1.0}}))
        self.assertEqual(json.loads(get("/api/state", token=self.tok)[2])["image_rebuild"], {"at": 1.0})
        st, _, body = post("/api/image/rebuild", {"on": False}, token=self.tok)
        self.assertEqual(json.loads(body)["image_rebuild"], {})
        self.assertEqual(piperd.MANAGER.created[-2:], [("image-rebuild", True), ("image-rebuild", False)])
        # the start dialog's "Rebuild image at this start": a strict boolean with the GUI token
        st, _, _ = post("/api/sessions", {"project": "p", "image_rebuild": True}, token=self.tok)
        self.assertEqual(st, 202)
        self.assertEqual(piperd.MANAGER.created[-1], ("p", "", "gui", "image_rebuild"))
        self.assertNotEqual(post("/api/sessions", {"project": "p", "image_rebuild": "yes"}, token=self.tok)[0], 202)
        post("/api/sessions", {"project": "p"}, token=self.tok)
        self.assertEqual(piperd.MANAGER.created[-1], ("p", "", "gui"))

    def test_api_doctor_and_features(self):
        self.assertEqual(get("/api/doctor")[0], 401)
        st, _, body = get("/api/doctor", token=self.tok)
        self.assertEqual(st, 200)
        self.assertIn("rows", json.loads(body))
        st, _, body = post("/api/features", {"preset": "dev", "onboarded": True}, token=self.tok)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["preset"], "dev")
        self.assertEqual(post("/api/features", {"modules": {"bogus": True}}, token=self.tok)[0], 400)
        features.save(preset_name="all")

    def test_wsl_without_systemd_names_wsl_conf(self):
        # Ubuntu on WSL ships systemctl although systemd does not run: lib-service.sh's probe decides
        with mock.patch.object(doctor.sys, "platform", "linux"), \
                mock.patch.object(doctor, "_systemd_user", return_value=False), \
                mock.patch.object(common, "is_wsl", return_value=True):
            r = doctor.check_service()
        self.assertFalse(r["ok"])
        self.assertIn("systemd=true", r["fix"])
        with mock.patch.object(doctor.sys, "platform", "linux"), \
                mock.patch.object(doctor, "_systemd_user", return_value=False), \
                mock.patch.object(common, "is_wsl", return_value=False):
            self.assertIsNone(doctor.check_service())

    def test_wsl_rows(self):
        def run(argv, timeout):
            if argv[:2] == ["wslinfo", "--networking-mode"]:
                return 0, "nat"
            if argv[0] == "loginctl":
                return 0, "Linger=no"
            return 0, "Docker Desktop"
        with mock.patch.object(doctor, "_run", side_effect=run), \
                mock.patch.object(doctor, "_systemd_user", return_value=True), \
                mock.patch.dict(os.environ, {"PROJECT_PATH": "/mnt/c/Users/me/www"}):
            rows = {r["key"]: r for r in doctor.check_wsl()}
        self.assertIn("Docker Desktop", rows["wsl"]["title"])
        self.assertIn("nat", rows["wsl"]["title"])
        self.assertFalse(rows["wsl-projects"]["ok"])
        self.assertIn("enable-linger", rows["wsl-linger"]["fix"])
        with mock.patch.object(doctor, "_run", side_effect=run), \
                mock.patch.object(doctor, "_systemd_user", return_value=False), \
                mock.patch.dict(os.environ, {"PROJECT_PATH": ""}):
            self.assertEqual([r["key"] for r in doctor.check_wsl()], ["wsl"])   # no PROJECT_PATH: not piperd's cwd

    def test_docker_fix_names_wsl_only_on_wsl(self):
        down = script("echo 'Cannot connect' >&2; exit 1")
        for wsl in (True, False):
            with mock.patch.dict(os.environ, {"PIPER_DOCKER_BIN": down}), mock.patch.object(doctor, "MAC", False), \
                    mock.patch.object(common, "is_wsl", return_value=wsl):
                self.assertEqual("WSL integration" in doctor.check_docker()["fix"], wsl)


    def test_db_drivers_row_says_what_the_database_view_says(self):
        """B-N7-3: after a failed install (venv made, pip failed) the row said "installed" while /api/db/status was
        ready:false - the row now asks the same driver check."""
        import dbtools
        doctor._SLOW.clear()
        self.addCleanup(doctor._SLOW.clear)
        with mock.patch.object(dbtools, "driver_check", lambda: {"ready": False, "missing": "no driver imports"}):
            r = doctor.check_db()
        self.assertFalse(r["ok"], r)
        doctor._SLOW.clear()
        with mock.patch.object(dbtools, "driver_check", lambda: {"ready": True, "drivers": {"pymysql": "1.1"}}):
            self.assertTrue(doctor.check_db()["ok"])

if __name__ == "__main__":
    unittest.main()


class W14SettingsLinksTest(unittest.TestCase):
    """W14: a doctor row's Settings button opens a section that exists"""

    def test_host_login_points_at_the_sandbox_section(self):
        import settings
        r = doctor.check_login({"claude_dir": "host"})
        self.assertEqual(r["settings"], "sandbox")
        self.assertIn("Settings › Sessions › Sandbox", r["text"])
        self.assertIn("claude_dir", [f.key for f in settings.section(r["settings"]).fields])


class SecurityNoticeTest(unittest.TestCase):
    """The one-time Security screen after the Minimal change: only for a user who finished
    the setup before it, once, the stored profile preselected and an own mix only informed."""

    def setUp(self):
        with open(features.FILE) as f:
            self.saved = f.read()
        self.policy = None
        if os.path.exists(common.POLICY_FILE):
            with open(common.POLICY_FILE) as f:
                self.policy = f.read()
        features._CACHE.clear()
        self.tree = mock.patch.object(security, "ENABLED", True)
        self.tree.start()

    def tearDown(self):
        self.tree.stop()
        with open(features.FILE, "w") as f:
            f.write(self.saved)
        if self.policy is None:
            if os.path.exists(common.POLICY_FILE):
                os.unlink(common.POLICY_FILE)
        else:
            with open(common.POLICY_FILE, "w") as f:
                f.write(self.policy)
        features._CACHE.clear()

    def write(self, feat, pol=None):
        with open(features.FILE, "w") as f:
            json.dump(dict({"base": "all", "modules": {}, "v": features.MIGRATION}, **feat), f)
        if pol is None:
            if os.path.exists(common.POLICY_FILE):
                os.unlink(common.POLICY_FILE)
        else:
            with open(common.POLICY_FILE, "w") as f:
                json.dump(pol, f)
        features._CACHE.clear()

    def test_an_onboarded_user_without_a_choice_sees_minimal_preselected(self):
        self.write({"onboarded": True})
        self.assertEqual(piperd._security_notice(), {"kind": "profile", "profile": "hole",
                                                     "on": len(security.DEFAULT_ON), "total": len(security.IDS)})

    def test_a_stored_old_profile_is_preselected_as_today_s(self):
        self.write({"onboarded": True}, {"security": {i: True for i in security.LEGACY_MINIMAL}})
        self.assertEqual(piperd._security_notice()["profile"], "hole")
        self.write({"onboarded": True}, {"security": {i: True for i in security.LEGACY_RECOMMENDED_KEY}})
        self.assertEqual(piperd._security_notice()["profile"], "recommended")
        self.write({"onboarded": True}, {"security": {i: True for i in security.IDS}})
        self.assertEqual(piperd._security_notice()["profile"], "guarded")

    def test_an_own_mix_and_plain_hole_are_only_informed(self):
        self.write({"onboarded": True}, {"security": {"db_prod_where": True, "piper_port": True}})
        self.assertEqual(piperd._security_notice(), {"kind": "custom", "profile": "custom", "on": 2,
                                                     "total": len(security.IDS)})
        self.write({"onboarded": True}, {"security": {}})
        self.assertEqual(piperd._security_notice()["kind"], "custom")

    def test_a_new_user_never_sees_it_and_finishing_the_setup_marks_it(self):
        self.write({"onboarded": False})
        self.assertIsNone(piperd._security_notice())
        features.save(onboarded=True)
        self.assertEqual(features.load()["seen"], [security.NOTICE])
        self.assertIsNone(piperd._security_notice())

    def test_shown_once_through_the_route(self):
        self.write({"onboarded": True})
        tok = common.mint_token("test")
        piperd.MANAGER = FakeManager()
        st, _, body = post("/api/features", {"seen": security.NOTICE}, token=tok)
        self.assertEqual(st, 200, body)
        self.assertIsNone(piperd._security_notice())
        with open(features.FILE) as f:
            self.assertEqual(json.load(f)["seen"], [security.NOTICE])
        self.assertEqual(post("/api/features", {"seen": "security-9"}, token=tok)[0], 400)
        features.save(preset_name="all")   # a later save keeps it seen
        self.assertEqual(features.load()["seen"], [security.NOTICE])

    def test_without_the_tree_nothing_shows(self):
        self.write({"onboarded": True})
        with mock.patch.object(security, "ENABLED", False):
            self.assertIsNone(piperd._security_notice())

    def test_the_notice_id_is_one_features_knows(self):
        self.assertIn(security.NOTICE, features.NOTICES)
