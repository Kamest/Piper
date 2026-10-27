"""Morning production check: schedule, the check session, the
report_production schema, novelty against 7 days, mutes, mapping, investigate."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import _env
import common
import holehome
import prodcheck

MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper", "sandbox", "piper-mcp.py")


def wait_for(pred, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class PromptToolsTest(unittest.TestCase):
    def test_the_prompts_describe_the_tools_unless_an_extension_names_them(self):
        task = prodcheck.check_task({"shop": "shop-prod"}) + prodcheck.suggest_task({"web": ""})
        for kind in ("errors", "rate", "alerts", "lookup"):
            self.assertIn(prodcheck.TOOLS[kind], task)
        self.assertNotIn("_", "".join(prodcheck.TOOLS.values()))   # descriptions, not some server's tool names


class ProdCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, background=True)
        a = _env.make_project("shop", {"src/a.txt": "a"})
        b = _env.make_project("web", {"src/b.txt": "b"})
        _env.register("shop", a, gitlab=["grp/shop"])
        _env.register("web", b)

    def setUp(self):
        for f in os.listdir(prodcheck.OBS_DIR) if os.path.isdir(prodcheck.OBS_DIR) else []:
            os.unlink(os.path.join(prodcheck.OBS_DIR, f))
        data = common.read_json(common.PROJECTS_FILE, {"projects": {}})
        data["obs"] = {"shop": "webstore-prod"}
        common.write_json(common.PROJECTS_FILE, data)
        self.m._prod_run = None
        self.m._prod_status = {"status": "idle", "at": None, "error": None}
        self.m.prod_notifiers = []
        if os.path.exists(prodcheck.CONFIG_FILE):
            os.unlink(prodcheck.CONFIG_FILE)

    def _live_check(self):
        """A running check session as _prod_launch leaves it (without spawning)."""
        import sessions
        s = sessions.Session(kind="prodcheck", project="prodcheck", path=prodcheck.WORK_DIR, state="working",
                             profile="advisor")
        for d in ("out", "in"):
            os.makedirs(os.path.join(s.cfg, "piper-mailbox", d), exist_ok=True)
        self.m.sessions[s.id] = s
        self.addCleanup(lambda: setattr(s, "state", "exited"))
        self.m._prod_run = {"sid": s.id, "mode": "check", "date": date.today().isoformat(), "started": time.time(),
                            "mapped": {"shop": "webstore-prod"}, "unmapped": {"web"}, "reported": False}
        return s

    def _tool(self, s, tool, args):
        rid = os.urandom(16).hex()
        common.write_json(os.path.join(s.cfg, "piper-mailbox", "out", rid + ".json"),
                          {"id": rid, "tool": tool, "args": args})
        self.m._mailbox(s, set())
        path = os.path.join(s.cfg, "piper-mailbox", "in", rid + ".json")
        self.assertTrue(wait_for(lambda: os.path.exists(path)))
        return common.read_json(path, {})

    # ---------------------------------------------------------------- schedule
    def test_due_on_workday_after_the_time_once_a_day(self):
        mon_early, mon = datetime(2026, 9, 28, 7, 10), datetime(2026, 9, 28, 7, 31)
        self.assertFalse(self.m._prod_due(mon_early))
        self.assertTrue(self.m._prod_due(mon))
        self.assertFalse(self.m._prod_due(datetime(2026, 9, 27, 9, 0)))            # Sunday
        os.makedirs(prodcheck.OBS_DIR, exist_ok=True)
        with open(prodcheck.LAST_RUN, "w") as f:
            f.write("2026-09-28\n")
        self.assertFalse(self.m._prod_due(mon))                                    # already ran today
        self.assertTrue(self.m._prod_due(datetime(2026, 9, 29, 12, 0)))            # catch-up later the next day
        common.write_json(prodcheck.CONFIG_FILE, {"time": "10:15", "days": [7]})   # re-read, no restart
        self.assertTrue(self.m._prod_due(datetime(2026, 9, 27, 10, 15)))
        common.write_json(prodcheck.CONFIG_FILE, {"enabled": False})              # legacy key (D10): the prod module decides
        self.assertTrue(self.m._prod_due(datetime(2026, 9, 29, 12, 0)))
        common.write_json(prodcheck.CONFIG_FILE, {"time": "31:00"})           # broken: defaults + the reason
        cfg = self.m.prod_config()
        self.assertEqual((cfg["time"], cfg["days"]), ("07:30", [1, 2, 3, 4, 5]))
        self.assertIn("HH:MM", cfg["config_error"])

    def test_a_check_that_died_unreported_is_retried_once(self):
        mon = datetime(2026, 9, 28, 8, 0)
        prodcheck.write_last("2026-09-28", "started", 1)
        self.assertFalse(self.m._prod_due(mon))                 # not straight away: the cause (VPN) may pass
        old = time.time() - prodcheck.RETRY_GAP - 1
        os.utime(prodcheck.LAST_RUN, (old, old))
        self.assertTrue(self.m._prod_due(mon))                  # died (piperd restart, no report): once more
        prodcheck.write_last("2026-09-28", "started", 2)
        self.assertFalse(self.m._prod_due(mon))                 # not a third time
        prodcheck.write_last("2026-09-28", "done")
        self.assertFalse(self.m._prod_due(mon))
        self.assertEqual(prodcheck.read_last(), ("2026-09-28", "done", prodcheck.MAX_ATTEMPTS))

    def test_a_manual_check_after_a_done_day_keeps_it_done(self):
        import sessions
        from unittest import mock
        today = date.today().isoformat()
        prodcheck.write_last(today, "done")
        with mock.patch.object(self.m, "_prod_create", return_value=sessions.Session(kind="prodcheck", project="prodcheck",
                                                                                     path=prodcheck.WORK_DIR)):
            self.m._prod_launch("check", today)
        self.addCleanup(lambda: setattr(self.m, "_prod_run", None))
        self.assertEqual(prodcheck.read_last()[:2], (today, "done"))   # a failed manual run is not retried by the tick

    def test_a_running_check_is_adopted_after_an_piperd_restart(self):
        s = self._live_check()
        with self.m._prod_lock:
            run = self.m._prod_run
            self.m._prod_run = None                             # what a fresh piperd has in memory
        common.write_json(prodcheck.RUN_FILE, dict(run, unmapped=sorted(run["unmapped"])))
        with mock.patch.object(self.m, "_prod_watch"):
            self.m._prod_tick()
        self.assertEqual(self.m._prod_run["sid"], s.id)
        with mock.patch.object(self.m, "_prod_end"):
            r = self.m.prod_tool(s, "report_production", {"items": []})
        self.assertEqual(r["saved"], 0)                         # its report is accepted, not refused
        self.assertEqual(prodcheck.read_last()[1], "done")

    def test_a_malformed_run_record_is_dropped_not_adopted(self):
        s = self._live_check()
        with self.m._prod_lock:
            self.m._prod_run = None
        os.makedirs(prodcheck.OBS_DIR, exist_ok=True)
        for bad in ({"sid": s.id}, {"sid": s.id, "mode": "check", "date": "x", "started": "soon", "mapped": {},
                                    "unmapped": []}, {"sid": s.id, "mode": "check", "date": "x", "started": 1.0,
                                                      "mapped": [], "unmapped": []}):
            common.write_json(prodcheck.RUN_FILE, bad)
            with mock.patch.object(self.m, "_prod_due", return_value=False):
                self.m._prod_tick()                              # no KeyError on every tick
            self.assertIsNone(self.m._prod_run, bad)
            self.assertFalse(os.path.exists(prodcheck.RUN_FILE), bad)

    def test_the_tick_and_run_now_never_start_two_checks(self):
        import sessions
        import threading
        started = []

        def create(task):
            time.sleep(0.2)   # slow enough for the other caller to arrive meanwhile
            s = sessions.Session(kind="prodcheck", project="prodcheck", path=prodcheck.WORK_DIR, state="working")
            self.m.sessions[s.id] = s
            self.addCleanup(lambda: setattr(s, "state", "exited"))
            started.append(s.id)
            return s
        errors = []
        with mock.patch.object(self.m, "_prod_create", create):
            th = threading.Thread(target=lambda: self.m._prod_launch("check", "2026-09-28", quiet=True))
            th.start()
            time.sleep(0.05)
            try:
                self.m._prod_launch("check", "2026-09-28")
            except ValueError as e:
                errors.append(str(e))
            th.join(5)
        self.assertEqual(len(started), 1)
        self.assertEqual(errors, ["a check is already running"])
        with self.m._prod_lock:
            self.m._prod_run = None

    def test_check_now_right_after_a_run_ended_is_not_refused(self):
        # Suggest finished and its session exited; the tick (60 s) has not reaped the run yet
        s = self._live_check()
        self.m._prod_run.update(mode="suggest", reported=True, reported_at=time.time())
        s.state = "exited"
        with mock.patch.object(self.m, "_prod_launch", return_value={"sid": "x"}) as launch:
            self.assertEqual(self.m.prod_run_now("check"), {"session": "x", "mode": "check"})
        launch.assert_called_once()
        self.assertTrue(s.hidden)
        s2 = self._live_check()   # a run whose session still works stays refused
        with self.assertRaisesRegex(ValueError, "already running"):
            self.m.prod_run_now("check")
        self.assertEqual(self.m._prod_run["sid"], s2.id)
        with self.m._prod_lock:
            self.m._prod_run = None

    def test_reaping_an_old_run_never_forgets_the_one_started_meanwhile(self):
        """Review 2026-10-07: the tick's _prod_watch(old run) cleared _prod_run (and the run file) even when
        the GUI's Check now had already started the next one."""
        s = self._live_check()
        old = self.m._prod_run
        old.update(reported=True, reported_at=time.time())
        s.state = "exited"
        self._live_check()
        new = self.m._prod_run
        common.write_json(prodcheck.RUN_FILE, {"sid": new["sid"]})
        self.m._prod_watch(old)                     # a late watch of the run that already ended
        self.assertIs(self.m._prod_run, new)
        self.assertEqual(common.read_json(prodcheck.RUN_FILE, {}).get("sid"), new["sid"])
        self.m._prod_fail(old, "late")
        self.assertIs(self.m._prod_run, new)
        self.assertTrue(os.path.exists(prodcheck.RUN_FILE))
        with self.m._prod_lock:
            self.m._prod_run = None

    def test_a_second_run_the_same_day_does_not_announce_the_same_items_again(self):
        got = []
        self.m.prod_notifiers.append(got.append)
        self.addCleanup(self.m.prod_notifiers.remove, got.append)
        item = {"project": "shop", "kind": "alert", "title": "DiskFull"}
        for n in range(2):   # the morning run, then "Check now" at noon
            s = self._live_check()
            with mock.patch.object(self.m, "_prod_end"):
                r = self._tool(s, "report_production", {"items": [item]})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"]["new"], 1)   # still new against the 7 days before
        self.assertEqual(len(got), 1)                 # but announced once

    def test_dont_map_leaves_every_suggestion(self):
        _, unmapped = self.m.prod_mapping()
        self.assertIn("web", unmapped)
        r = self.m.prod_ignore("web", True)
        self.assertEqual(r["ignored"], ["web"])
        self.addCleanup(lambda: self.m.prod_ignore("web", False))
        _, unmapped = self.m.prod_mapping()
        self.assertNotIn("web", unmapped)                 # no suggestion run asks about it
        common.write_json(prodcheck.SUGGEST_FILE, {"suggestions": [{"project": "web", "obs": "w", "note": ""}]})
        self.assertEqual(self.m.prod()["suggestions"], [])   # nor does the board show an old suggestion
        self.m.prod_ignore("web", False)
        self.assertIn("web", self.m.prod_mapping()[1])
        with self.assertRaises(ValueError):
            self.m.prod_ignore("bad key!", True)

    def test_tick_waits_for_two_free_slots_and_does_not_mark_the_day(self):
        import sessions
        with mock.patch.object(sessions, "MAX_SESSIONS", 1), \
                mock.patch.object(self.m, "_prod_launch") as launch:
            self.m._prod_tick(datetime(2026, 9, 28, 8, 0))
        launch.assert_not_called()
        self.assertEqual(self.m.prod()["status"], "waiting")
        self.assertFalse(os.path.exists(prodcheck.LAST_RUN))

    def test_no_mapping_no_check(self):
        common.write_json(common.PROJECTS_FILE, dict(common.read_json(common.PROJECTS_FILE, {}), obs={}))
        with mock.patch.object(self.m, "_prod_launch") as launch:
            self.m._prod_tick(datetime(2026, 9, 28, 8, 0))
        launch.assert_not_called()
        self.assertFalse(self.m.prod_summary()["on"])
        with self.assertRaises(ValueError):
            self.m.prod_run_now()

    def test_launch_starts_a_check_session_without_a_project(self):
        run = self.m._prod_launch("check", date.today().isoformat())
        s = self.m.sessions[run["sid"]]
        self.addCleanup(lambda: setattr(s, "state", "exited"))
        self.assertEqual((s.kind, s.profile, s.path, s.origin), ("prodcheck", "advisor", prodcheck.WORK_DIR, "prod"))
        self.assertIn("webstore-prod", s.task)
        self.assertIn("report_production", s.task)
        self.assertEqual(prodcheck.read_last(), (date.today().isoformat(), "started", 1))
        self.assertEqual(common.read_json(prodcheck.RUN_FILE, {})["sid"], s.id)   # a piperd restart adopts it
        with self.assertRaises(ValueError):   # one run at a time
            self.m.prod_run_now()
        # the start: an empty dir, no project mounted, the prodcheck tool set, the plugin allowed
        self.assertTrue(wait_for(lambda: s.state in ("starting", "attesting", "ready", "error", "blocked"), 10))
        self.assertEqual(s.blocks, [])
        if _env.pty_available():   # a failed spawn must fail the test (a sandbox without PTYs cannot spawn)
            self.assertNotIn(s.state, ("error", "blocked"), s.detail)
        mcp = common.read_json(os.path.join(s.cfg, "piper", "mcp.json"), {})
        self.assertIn("prodcheck", mcp["mcpServers"]["piper"]["args"])
        argv = self.m._argv(s, holehome.container_home(common.HOLE_HOME))
        self.assertEqual(argv[argv.index("--allowedTools") + 1], prodcheck.CLAUDE_ARGS[prodcheck.CLAUDE_ARGS.index("--allowedTools") + 1])
        self.assertGreater(argv.index("--allowedTools"), argv.index("--"))
        denied = argv[argv.index("--disallowedTools") + 1].split(",")   # skip-permissions: only a deny restricts
        self.assertEqual(argv[argv.index("--tools") + 1], "TodoWrite")   # no code-running built-in exists at all
        for tool in ("Bash", "Monitor", "REPL", "Workflow", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task", "Agent"):
            self.assertIn(tool, denied)
        self.assertGreater(argv.index("--disallowedTools"), argv.index("--"))

    # ---------------------------------------------------------------- the report
    def test_report_is_validated_scrubbed_stored_and_novelty_is_mechanical(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        os.makedirs(prodcheck.OBS_DIR, exist_ok=True)
        old_key = prodcheck.item_key("shop", "error", "NullPointerException at CartService.total")
        common.write_json(os.path.join(prodcheck.OBS_DIR, yesterday + ".json"),
                          {"date": yesterday, "items": [{"key": old_key}]})
        got = []
        self.m.prod_notifiers.append(got.append)
        s = self._live_check()
        with mock.patch.object(self.m, "_prod_end") as end:
            r = self._tool(s, "report_production", {"items": [
                {"project": "shop", "kind": "error", "title": "NPE in the cart",
                 "fingerprint": "NullPointerException at CartService.total:120", "count": 14,
                 "url": "https://grafana.example.com/explore?x=1", "detail": "db.password=hunter2 in the message"},
                {"project": "shop", "kind": "alert", "title": "HighErrorRate", "url": "javascript:alert(1)"},
                {"project": "web", "kind": "error", "title": "not mapped - dropped"},
            ], "errors": [{"project": "shop", "error": "prom timeout"}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["result"]["saved"], r["result"]["new"], r["result"]["dropped"]), (2, 1, 1))
        end.assert_called()   # piperd ends the session
        rep = common.read_json(os.path.join(prodcheck.OBS_DIR, date.today().isoformat() + ".json"), {})
        npe, alert = rep["items"]
        self.assertFalse(npe["new"])                          # the line number does not make it new
        self.assertTrue(alert["new"])
        self.assertNotIn("hunter2", npe["detail"])            # scrub()
        self.assertEqual(npe["url"], "https://grafana.example.com/explore?x=1")
        self.assertEqual(alert["url"], "")                    # only https links
        self.assertEqual(rep["errors"], [{"project": "shop", "error": "prom timeout"}])
        self.assertEqual([i["title"] for i in got[0]["fresh"]], ["HighErrorRate"])
        self.assertEqual(self.m.prod_summary()["new"], 1)
        self.assertEqual(self.m.prod()["status"], "ok")
        # a second report from the same run is refused
        r = self._tool(s, "report_production", {"items": []})
        self.assertFalse(r["ok"])
        # mute: the item stays in the report but no longer counts
        self.m.prod_mute(alert["key"], 7)
        self.assertEqual(self.m.prod_summary()["new"], 0)
        self.assertTrue(next(i for i in self.m.prod()["report"]["items"] if i["key"] == alert["key"])["muted"])
        self.m.prod_unmute(alert["key"])
        self.assertEqual(self.m.prod_summary()["new"], 1)
        # done: once the session has exited it leaves the rail and the run is over
        s.state = "exited"
        self.m._prod_tick()
        self.assertTrue(s.hidden)
        self.assertIsNone(self.m._prod_run)

    def test_malformed_reports_are_refused(self):
        s = self._live_check()
        for bad in ({"items": "x"}, {"items": [{"project": "shop", "kind": "boom", "title": "t"}]},
                    {"items": [{"project": "shop", "kind": "error", "title": ""}]},
                    {"items": [{"project": "shop", "title": "t"}] * 201}):
            r = self._tool(s, "report_production", bad)
            self.assertFalse(r["ok"], bad)
        self.assertFalse(self.m._prod_run["reported"])

    def test_a_report_whose_every_item_was_dropped_is_refused_not_an_all_clear(self):
        # the model used the observability name instead of the project key: nothing may read as "No errors"
        s = self._live_check()
        with mock.patch.object(self.m, "_prod_end"):
            r = self._tool(s, "report_production", {"items": [
                {"project": "webstore-prod", "kind": "error", "title": "NPE in the cart"}]})
        self.assertFalse(r["ok"], r)
        self.assertIn("shop", r["error"])                    # it names the keys to use
        self.assertFalse(self.m._prod_run["reported"])       # the model can call again
        self.assertFalse(os.path.exists(os.path.join(prodcheck.OBS_DIR, date.today().isoformat() + ".json")))

    def test_a_check_where_every_project_failed_is_an_error_not_an_all_clear(self):
        # VPN down: no items, a query error for every mapped project
        s = self._live_check()
        today = date.today().isoformat()
        prodcheck.write_last(today, "started", 1)            # as _prod_launch leaves it
        with mock.patch.object(self.m, "_prod_end") as end:
            r = self._tool(s, "report_production", {"items": [],
                                                    "errors": [{"project": "shop", "error": "connection refused"}]})
        self.assertTrue(r["ok"], r)
        end.assert_called()                                  # the session still ends
        st = self.m.prod()
        self.assertEqual(st["status"], "error")
        self.assertIn("connection refused", st["error"])
        self.assertFalse(os.path.exists(os.path.join(prodcheck.OBS_DIR, date.today().isoformat() + ".json")))
        self.assertEqual(prodcheck.read_last(), (today, "started", 1))   # not done: the once-a-day retry applies
        self.assertIsNone(self.m._prod_run)

    def test_only_the_running_check_session_may_report(self):
        import sessions
        other = sessions.Session(kind="project", project="shop", path="/x", state="ready")
        for d in ("out", "in"):
            os.makedirs(os.path.join(other.cfg, "piper-mailbox", d), exist_ok=True)
        self.m.sessions[other.id] = other
        self.addCleanup(lambda: setattr(other, "state", "exited"))
        self._live_check()
        r = self._tool(other, "report_production", {"items": []})
        self.assertFalse(r["ok"])
        self.assertIn("only available to the production check", r["error"])
        stale = sessions.Session(kind="prodcheck", project="prodcheck", path=prodcheck.WORK_DIR, state="working")
        for d in ("out", "in"):
            os.makedirs(os.path.join(stale.cfg, "piper-mailbox", d), exist_ok=True)
        self.m.sessions[stale.id] = stale
        self.addCleanup(lambda: setattr(stale, "state", "exited"))
        self.assertFalse(self._tool(stale, "report_production", {"items": []})["ok"])   # not this run's session
        s = self.m.sessions[self.m._prod_run["sid"]]
        r = self._tool(s, "gitlab_issue", {"project": "grp/shop", "iid": 1})
        self.assertFalse(r["ok"])                                                       # nothing but the report
        self.assertIn("only report_production", r["error"])

    def test_a_session_that_ends_without_report_or_times_out_is_an_error(self):
        s = self._live_check()
        s.state, s.detail = "blocked", "1 preflight finding(s)"
        self.m._prod_tick()
        st = self.m.prod()
        self.assertEqual(st["status"], "error")
        self.assertIn("blocked", st["error"])
        self.assertIsNone(self.m._prod_run)
        self.assertFalse(s.hidden)                           # stays in the rail to be looked at
        s2 = self._live_check()
        self.m._prod_run["started"] -= prodcheck.TIMEOUT + 1
        with mock.patch.object(self.m, "stop") as stop:
            self.m._prod_tick()
        stop.assert_called_with(s2.id)
        self.assertIn("did not finish", self.m.prod()["error"])

    def test_suggest_mode_stores_suggestions_for_unmapped_projects_only(self):
        s = self._live_check()
        self.m._prod_run["mode"] = "suggest"
        with mock.patch.object(self.m, "_prod_end"):
            r = self._tool(s, "report_production", {"items": [], "suggestions": [
                {"project": "web", "obs": "web-prod", "note": "the only match"},
                {"project": "shop", "obs": "x"}, {"project": "web", "obs": "bad\nname;rm"}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.m.prod()["suggestions"], [{"project": "web", "obs": "web-prod", "note": "the only match"}])
        self.assertFalse(os.path.exists(os.path.join(prodcheck.OBS_DIR, date.today().isoformat() + ".json")))

    def test_a_failed_suggestion_run_keeps_its_mode_for_try_again(self):
        s = self._live_check()
        self.m._prod_run["mode"] = "suggest"
        s.state, s.detail = "blocked", "1 preflight finding(s)"
        self.m._prod_tick()
        st = self.m.prod()
        self.assertEqual((st["status"], st["mode"]), ("error", "suggest"))   # the GUI retries a suggestion, not a check

    # ---------------------------------------------------------------- mapping, investigate
    def test_mapping_is_written_to_projects_json(self):
        self.m.prod_set_mapping("web", "  web-prod ")
        mapped, unmapped = self.m.prod_mapping()
        self.assertEqual(mapped, {"shop": "webstore-prod", "web": "web-prod"})
        self.m.prod_set_mapping("web", "")
        self.assertNotIn("web", self.m.prod_mapping()[0])
        with self.assertRaises(ValueError):
            self.m.prod_set_mapping("web", "bad;name\n")
        with self.assertRaises(KeyError):
            self.m.prod_set_mapping("nope", "x")
        # an explicit entry's own "obs" counts too
        data = common.read_json(common.PROJECTS_FILE, {})
        data["projects"]["web"]["obs"] = "from-entry"
        common.write_json(common.PROJECTS_FILE, data)
        self.assertEqual(self.m.prod_mapping()[0]["web"], "from-entry")
        self.m.prod_set_mapping("web", "")
        self.assertNotIn("obs", common.read_json(common.PROJECTS_FILE, {})["projects"]["web"])

    def test_investigate_opens_a_project_session_with_the_summary(self):
        import sessions
        os.makedirs(prodcheck.OBS_DIR, exist_ok=True)
        key = prodcheck.item_key("shop", "error", "IOE at Feed.read")
        common.write_json(os.path.join(prodcheck.OBS_DIR, date.today().isoformat() + ".json"), {
            "date": date.today().isoformat(), "items": [{
                "key": key, "project": "shop", "obs": "webstore-prod", "kind": "error", "title": "Feed fails to load",
                "fingerprint": "IOE at Feed.read", "count": 3, "first_seen": "06:12", "trend": "", "detail": "",
                "service": "", "url": "", "new": True}]})
        with mock.patch.object(self.m, "create", return_value=sessions.Session(id="abcd1234")) as create:
            r = self.m.prod_investigate(key)
        self.assertEqual(r, {"id": "abcd1234"})
        project, task, title = create.call_args[0]
        self.assertEqual((project, create.call_args[1]["origin"]), ("shop", "prod"))
        self.assertIn("Feed fails to load", task)
        self.assertIn("webstore-prod", task)
        self.assertTrue(title.startswith("Production: "))
        with self.assertRaises(KeyError):
            self.m.prod_investigate("0" * 16)


class ProdMcpTest(unittest.TestCase):
    def test_prodcheck_role_has_report_and_quiet_participation(self):
        lst = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        home = os.path.join(_env.ROOT, "mcp-home")
        os.makedirs(home, exist_ok=True)
        p = subprocess.run([sys.executable, MCP, "--role", "prodcheck"], input=json.dumps(lst) + "\n",
                           capture_output=True, text=True, timeout=20, env={"HOME": home, "PATH": os.environ["PATH"]})
        names = [t["name"] for t in json.loads(p.stdout.splitlines()[0])["result"]["tools"]]
        self.assertEqual(names, ["report_production", "quiet_status", "quiet_ack", "quiet_wait"])


class PureTest(unittest.TestCase):
    def test_validate_config(self):
        self.assertEqual(prodcheck.validate_config(None), {"enabled": True, "time": "07:30", "days": [1, 2, 3, 4, 5]})
        self.assertEqual(prodcheck.validate_config({"time": "6:05", "days": [5, 1, 1]}),
                         {"enabled": True, "time": "06:05", "days": [1, 5]})
        for bad in ([], {"enabled": "yes"}, {"time": "7"}, {"days": []}, {"days": [0]}, {"days": [True]}, {"x": 1}):
            with self.assertRaises(ValueError, msg=bad):
                prodcheck.validate_config(bad)

    def test_validate_report_rejects_bad_shapes_cleanly(self):
        mapped = {"shop": "webstore-prod"}
        r = prodcheck.validate_report({"items": [{"project": "shop", "title": "T", "count": float("inf")}]}, mapped, set())
        self.assertEqual(r["items"][0]["count"], 0)
        for bad in ({"errors": 5}, {"errors": {"project": "shop"}}, {"errors": "shop"}, {"suggestions": 7},
                    {"suggestions": "web"}):
            with self.assertRaises(ValueError, msg=bad):
                prodcheck.validate_report(bad, mapped, {"web"})

    def test_fingerprint_and_time(self):
        self.assertEqual(prodcheck.norm_fingerprint("  NPE at A.b:120 "), "npe at a.b")
        self.assertEqual(prodcheck.item_key("p", "error", "X at Y:1"), prodcheck.item_key("p", "error", "x  at y:22"))
        self.assertNotEqual(prodcheck.item_key("p", "error", "X"), prodcheck.item_key("p", "alert", "X"))
        self.assertEqual(prodcheck.parse_hhmm("6:05"), (6, 5))
        self.assertEqual(prodcheck.parse_hhmm("25:00"), (7, 30))


if __name__ == "__main__":
    unittest.main()
