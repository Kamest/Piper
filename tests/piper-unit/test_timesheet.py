"""9.12 A Timesheet (timesheet.py): attention blocks per issue, overlap split by event
counts, no padding by default, events.jsonl only when enabled, piperd-typed prompts left
out, the user's own commits with the issue from the branch, edits that a recomputation
keeps, manual rows, assignment of a session (piperd routes: test_piperd style, direct here)."""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import _env
import common
import extensions
import gitops
import projects
import timesheet

GENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *args, env=None):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, env=env or GENV).stdout.decode()


class PureTest(unittest.TestCase):
    def test_blocks_join_within_the_gap_and_never_pad_by_default(self):
        ev = [{"ts": t, "key": "a"} for t in (0, 300, 600, 3000)] + [{"ts": 100, "key": "b"}]
        b = timesheet.blocks(ev, gap=900, pad=0)
        self.assertEqual([(x["key"], x["start"], x["end"], x["n"]) for x in b],
                         [("a", 0, 600, 3), ("b", 100, 100, 1), ("a", 3000, 3000, 1)])
        b = timesheet.blocks(ev, gap=900, pad=60)
        self.assertEqual((b[0]["start"], b[0]["end"]), (-60, 660))

    def test_overlap_is_split_by_event_counts(self):
        blks = [{"key": "a", "start": 0, "end": 600, "n": 3}, {"key": "b", "start": 0, "end": 600, "n": 1}]
        per = timesheet.allocate(blks, [-10 ** 9, 10 ** 9])
        self.assertAlmostEqual(per[("a", 0)], 450)
        self.assertAlmostEqual(per[("b", 0)], 150)
        self.assertAlmostEqual(sum(per.values()), 600)   # never more than the real time

    def test_a_block_over_midnight_is_split_into_days(self):
        per = timesheet.allocate([{"key": "a", "start": 900, "end": 1200, "n": 2}], [0, 1000, 2000])
        self.assertEqual(per, {("a", 0): 100, ("a", 1): 200})

    def test_project_only_events_join_the_nearest_issue_of_the_project(self):
        ev = [{"ts": 0, "key": "g#issue#1", "project": "p"}, {"ts": 500, "key": None, "project": "p"},
              {"ts": 5000, "key": None, "project": "p"}, {"ts": 10, "key": None, "project": "q"}]
        out = timesheet.attach_project_events(ev, 900)
        self.assertEqual([e["key"] for e in out], ["g#issue#1", "g#issue#1", "p:p", "p:q"])

    def test_issue_numbers(self):
        self.assertEqual(timesheet.issue_from_branch("refs/heads/123-fix-shipping"), 123)
        self.assertEqual(timesheet.issue_from_branch("refs/remotes/origin/feature/45_x"), 45)
        self.assertIsNone(timesheet.issue_from_branch("refs/heads/master"))
        self.assertEqual(timesheet.issue_from_text("fix: shipping (#77)"), 77)
        self.assertIsNone(timesheet.issue_from_text("color &#123; no"))

    def test_validate_the_config(self):
        self.assertEqual(timesheet.validate({}), {"enabled": False, "gap_min": 15, "pad_min": 0})
        self.assertEqual(timesheet.validate({"enabled": True, "gap_min": 30}), {"enabled": True, "gap_min": 30, "pad_min": 0})
        for bad in ({"enabled": "yes"}, {"gap_min": 0}, {"pad_min": 61}, {"gap_min": 1.5}, {"gap_min": True}, {"x": 1}, []):
            with self.assertRaises(ValueError, msg=bad):
                timesheet.validate(bad)

    def test_week_days(self):
        days, start, end = timesheet.week_days("2026-W40")
        self.assertEqual(days[0], "2026-09-28")
        self.assertEqual(days[-1], "2026-10-04")
        self.assertEqual(timesheet.week_of(start + 3600), "2026-W40")
        self.assertGreater(end, start)
        with self.assertRaises(ValueError):
            timesheet.week_days("2026-40")


class ManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = root = os.path.realpath(_env.make_project("ts1", {"src/A.java": "class A {}\n"}))
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "me@example.com")
        git(root, "config", "user.name", "Me")
        git(root, "remote", "add", "origin", "git@gitlab.example.com:grp/ts1.git")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "init")
        cls.key = "team-x/ts1"

    def setUp(self):
        for f in (timesheet.EVENTS_FILE, timesheet.CONFIG_FILE, timesheet.ASSIGN_FILE):
            try:
                os.remove(f)
            except FileNotFoundError:
                pass
        self.week = timesheet.week_of(time.time())
        try:
            os.remove(self.m._ts_week_file(self.week))
        except FileNotFoundError:
            pass
        self.m._ts_git.clear()
        self.m._ts_focus.clear()

    def _session(self, **kw):
        import sessions
        s = sessions.Session(**dict({"project": self.key, "path": self.root, "state": "waiting", "title": "ts1",
                                     "gitlab": ["grp/ts1"]}, **kw))
        self.m.sessions[s.id] = s
        self.addCleanup(self.m.sessions.pop, s.id, None)
        return s

    def _enable(self, **kw):
        os.makedirs(timesheet.TS_DIR, exist_ok=True)
        common.write_json(timesheet.CONFIG_FILE, kw)
        _env.set_modules(self, timesheet=True)   # D10: the module is the switch

    def _events(self):
        try:
            with open(timesheet.EVENTS_FILE) as f:
                return [json.loads(x) for x in f]
        except FileNotFoundError:
            return []

    def test_config_is_reread_and_an_invalid_value_falls_back(self):
        self._enable(gap_min=30)
        self.assertEqual(self.m.ts_settings(), {"enabled": True, "gap_min": 30, "pad_min": 0})
        common.write_json(timesheet.CONFIG_FILE, {"enabled": "yes", "gap_min": 999, "pad_min": 5})
        self.assertEqual(self.m.ts_settings(), {"enabled": True, "gap_min": 15, "pad_min": 5})   # legacy enabled ignored
        _env.set_modules(self, timesheet=False)
        self.assertFalse(self.m.ts_settings()["enabled"])

    def test_broken_files_and_odd_lines_never_break_events_or_the_view(self):
        """A hand-broken config.json / assign.json must not raise into the session event loop,
        and one odd events.jsonl line must not kill every week's view."""
        os.makedirs(timesheet.TS_DIR, exist_ok=True)
        for f in (timesheet.CONFIG_FILE, timesheet.ASSIGN_FILE):
            with open(f, "w") as fh:
                fh.write("{bad json")
        _env.set_modules(self, timesheet=False)
        s = self._session()
        self.assertFalse(self.m.ts_settings()["enabled"])
        self.m.ts_prompt_event(s, {"ts": time.time(), "prompt": "x"})   # no exception
        with open(timesheet.EVENTS_FILE, "w") as fh:
            fh.write(json.dumps({"ts": time.time(), "sid": "sX", "project": "p", "kind": "prompt", "key": 5, "title": 7}) + "\n")
        self.assertIn("rows", self.m.timesheet(self.week))
        self.assertEqual(timesheet._num(float("inf"), 0, 600, 30), 30)

    def test_nothing_is_recorded_until_enabled(self):
        _env.set_modules(self, timesheet=False)
        s = self._session()
        self.assertFalse(self.m.ts_settings()["enabled"])
        self.m.ts_prompt_event(s, {"ts": time.time(), "prompt": "fix it"})
        self.assertFalse(self.m.ts_focus(s.id)["recorded"])
        self.assertEqual(self._events(), [])

    def test_recorded_events_carry_no_prompt_text(self):
        self._enable()
        s = self._session(issue={"key": "grp/ts1#issue#12", "iid": 12, "title": "Shipping"})
        self.m.ts_prompt_event(s, {"ts": time.time(), "prompt": "SECRET prompt TEXT"})
        ev = self._events()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["key"], ev[0]["title"]), ("prompt", "grp/ts1#issue#12", "Shipping"))
        self.assertNotIn("SECRET", json.dumps(ev))

    def test_piperd_typed_prompts_and_replays_are_not_attention(self):
        self._enable()
        auto = self._session(origin="inbox", task="Analyze issue grp/ts1#12 and write the analysis")
        self.m.ts_prompt_event(auto, {"ts": time.time(), "prompt": "Analyze issue grp/ts1#12 and write the analysis"})
        self.m.ts_prompt_event(auto, {"ts": time.time(), "prompt": "[piper] Session abc sent a result."})
        # an older prefix an extension names (an older piperd, a replayed events file)
        with mock.patch.object(extensions, "items", side_effect=lambda k: ["[old]"] if k == "AUTO_PROMPT_PREFIXES" else []):
            self.m.ts_prompt_event(auto, {"ts": time.time(), "prompt": "[old] Session abc sent a result."})
        self.m.ts_prompt_event(auto, {"ts": time.time() - 3600, "prompt": "an old event (replay)"})
        self.assertEqual(self._events(), [])
        self.m.ts_prompt_event(auto, {"ts": time.time(), "prompt": "now fix it"})
        self.assertEqual(len(self._events()), 1)

    def test_a_prompt_counts_only_after_the_humans_keystrokes(self):
        """piper-events.jsonl is sandbox-written: a UserPromptSubmit line alone is no work of the user."""
        self._enable()
        s = self._session(issue={"key": "grp/ts1#issue#13", "iid": 13, "title": "Thirteen"})
        now = time.time()
        self.m.ts_prompt_event(s, {"ts": now, "prompt": "x"}, human=False)   # no Enter of the human behind it
        self.assertEqual(self._events(), [])
        self.m.ts_prompt_event(s, {"ts": now - 50, "prompt": "fix it"}, human=True)   # typed, then Enter
        ev = self._events()
        self.assertEqual(len(ev), 1)
        self.assertGreaterEqual(ev[0]["ts"], round(now, 3) - 0.001)   # the host time, not the (possibly drifting) container's

    def test_a_turn_nobody_typed_is_not_work(self):
        """A UserPromptSubmit line forged by the sandbox (or text piperd typed) makes a turn - not a timesheet block."""
        now = time.time()
        if timesheet.day_of(now - 600) != timesheet.day_of(now):
            self.skipTest("just after midnight")
        s = self._session(issue={"key": "grp/ts1#issue#15", "iid": 15, "title": "Fifteen"},
                          turns=[{"n": 1, "prompt": "x", "started": now - 300, "ended": now - 200, "human": False}])
        rows = [r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#15"]
        self.assertTrue(not rows or not rows[0]["days"][timesheet.day_of(now)]["blocks"])
        s.turns[0]["human"] = True
        row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#15")
        self.assertEqual(len(row["days"][timesheet.day_of(now)]["blocks"]), 1)

    def test_two_real_prompts_seconds_apart_are_two(self):
        now = time.time()
        if timesheet.day_of(now - 600) != timesheet.day_of(now):
            self.skipTest("just after midnight")
        s = self._session(issue={"key": "grp/ts1#issue#17", "iid": 17, "title": "Seventeen"},
                          turns=[{"n": 1, "prompt": "fix it", "started": now - 60, "ended": now - 52, "human": True},
                                 {"n": 2, "prompt": "yes", "started": now - 50, "ended": now - 40, "human": True}])
        row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#17")
        self.assertEqual(sum(b["n"] for b in row["days"][timesheet.day_of(now)]["blocks"]) >= 2, True)
        self.assertIn("yes", json.dumps(row))   # the second prompt's text is not dropped from the notes

    def test_a_turn_and_its_prompt_event_count_once_despite_clock_drift(self):
        self._enable()
        now = time.time()
        if timesheet.day_of(now - 600) != timesheet.day_of(now):
            self.skipTest("just after midnight")
        s = self._session(issue={"key": "grp/ts1#issue#16", "iid": 16, "title": "Sixteen"},
                          turns=[{"n": 1, "prompt": "x", "started": now - 107, "ended": None, "human": True}])
        def n():
            self.m._ts_git.clear()
            row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#16")
            return sum(b["n"] for b in row["days"][timesheet.day_of(now)]["blocks"])
        before = n()   # the turn (and whatever else this repository has today, e.g. its commit)
        self.m.ts_record(s, "prompt", now - 100)   # the same prompt, host time 7 s later
        self.assertEqual(n(), before)

    def test_a_confirmation_that_timed_out_is_not_attention(self):
        now = time.time()
        if timesheet.day_of(now - 600) != timesheet.day_of(now):
            self.skipTest("just after midnight")
        s = self._session(issue={"key": "grp/ts1#issue#14", "iid": 14, "title": "Fourteen"})
        common.audit({"event": "confirm", "kind": "git_commit", "session": s.id, "approved": False, "answered": False})
        common.audit({"event": "confirm", "kind": "git_commit", "session": s.id, "approved": True, "auto": True})
        rows = [r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#14"]
        self.assertTrue(not rows or not rows[0]["days"][timesheet.day_of(now)]["blocks"])
        common.audit({"event": "confirm", "kind": "git_commit", "session": s.id, "approved": True, "answered": True})
        row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#14")
        self.assertEqual(len(row["days"][timesheet.day_of(now)]["blocks"]), 1)

    def test_focus_is_rate_limited(self):
        self._enable()
        s = self._session()
        self.assertTrue(self.m.ts_focus(s.id)["recorded"])
        self.assertFalse(self.m.ts_focus(s.id)["recorded"])
        with self.assertRaises(KeyError):
            self.m.ts_focus("nope")

    def test_report_blocks_split_and_raw_minutes(self):
        now = time.time()
        # N9: 3 h back always (and the report of that week) - the "today, 10 min ago" fallback for 00:00-03:00 put the
        # turns within the gap of the session's own events of now: [599] != [300] every night (seen at 00:35)
        base = now - 3 * 3600
        a = self._session(issue={"key": "grp/ts1#issue#12", "iid": 12, "title": "Shipping"},
                          turns=[{"n": 1, "prompt": "fix the shipping recalculation", "started": base},
                                 {"n": 2, "prompt": "add a test", "started": base + 300}])
        report = self.m.timesheet(timesheet.week_of(base))
        row = next(r for r in report["rows"] if r["key"] == "grp/ts1#issue#12")
        cell = row["days"][timesheet.day_of(base)]
        self.assertEqual([round(b["end"] - b["start"]) for b in cell["blocks"]], [300])   # first..last, no padding
        self.assertLessEqual(cell["raw_min"], 5.0)
        self.assertIn("fix the shipping recalculation", cell["auto_text"])
        self.assertEqual(row["title"], "Shipping")
        self.assertEqual(row["project"], self.key)
        self.assertEqual([x["id"] for x in row["sessions"]], [a.id])

    def test_a_block_over_the_week_boundary_keeps_both_halves(self):
        """Sunday 23:55 and Monday 00:05 are one 10-minute block (gap 15): 5 min in each week, not 0."""
        days, _, end = timesheet.week_days("2026-W40")
        self._session(issue={"key": "grp/ts1#issue#18", "iid": 18, "title": "Eighteen"},
                      turns=[{"n": 1, "prompt": "a", "started": end - 300, "human": True},
                             {"n": 2, "prompt": "b", "started": end + 300, "human": True}])

        def cell(week, day):
            row = next(r for r in self.m.timesheet(week)["rows"] if r["key"] == "grp/ts1#issue#18")
            return row["days"][day]["raw_min"]
        self.assertEqual(cell("2026-W40", days[-1]), 5.0)
        self.assertEqual(cell("2026-W41", timesheet.week_days("2026-W41")[0][0]), 5.0)

    def test_own_commits_count_with_the_issue_from_the_branch(self):
        git(self.root, "checkout", "-q", "-b", "34-fix")
        self.addCleanup(git, self.root, "checkout", "-q", "main")
        with open(os.path.join(self.root, "src/A.java"), "a") as f:
            f.write("// x\n")
        git(self.root, "commit", "-q", "-am", "Fix the calculation")
        env = dict(GENV, GIT_AUTHOR_EMAIL="other@example.com", GIT_COMMITTER_EMAIL="other@example.com")
        with open(os.path.join(self.root, "src/A.java"), "a") as f:
            f.write("// y\n")
        git(self.root, "commit", "-q", "-am", "Foreign commit", env=env)
        _env.register(self.key, self.root, gitlab=["grp/ts1"])
        report = self.m.timesheet(self.week)
        row = next((r for r in report["rows"] if r["key"] == "grp/ts1#issue#34"), None)
        self.assertIsNotNone(row, report["rows"])
        texts = " ".join(c["auto_text"] for c in row["days"].values())
        self.assertIn("Fix the calculation", texts)
        self.assertNotIn("Foreign", json.dumps(report))

    def test_edits_manual_rows_and_assignment(self):
        now = time.time()
        s = self._session(title="no issue", turns=[{"n": 1, "prompt": "help", "started": now - 60}])
        report = self.m.timesheet(self.week)
        self.assertIn(s.id, [u["id"] for u in report["unassigned"]])
        self.m.ts_assign(s.id, "grp/ts1#99", "New issue")
        report = self.m.timesheet(self.week)
        self.assertNotIn(s.id, [u["id"] for u in report["unassigned"]])
        row = next(r for r in report["rows"] if r["key"] == "grp/ts1#issue#99")
        day = timesheet.day_of(now - 60)
        self.m.ts_edit(self.week, day, row["key"], 90, "Analysis and fix")
        cell = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == row["key"])["days"][day]
        self.assertEqual((cell["minutes"], cell["text"]), (90, "Analysis and fix"))
        self.m.ts_edit(self.week, day, row["key"], None, None)
        cell = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == row["key"])["days"][day]
        self.assertNotIn("minutes", cell)
        self.m.ts_manual(self.week, {"day": day, "label": "Meeting with the client", "minutes": 45})
        man = self.m.timesheet(self.week)["manual"]
        self.assertEqual([(x["label"], x["minutes"]) for x in man], [("Meeting with the client", 45)])
        self.m.ts_manual(self.week, {"id": man[0]["id"], "delete": True})
        self.assertEqual(self.m.timesheet(self.week)["manual"], [])
        for bad in (-30, 0, None, "x"):   # F3b: these became a 0:00 row
            with self.assertRaisesRegex(ValueError, "minutes"):
                self.m.ts_manual(self.week, {"day": day, "label": "Meeting", "minutes": bad})
        self.assertEqual(self.m.timesheet(self.week)["manual"], [])
        with self.assertRaises(ValueError):
            self.m.ts_assign(s.id, "not a key")
        with self.assertRaises(ValueError):
            self.m.ts_edit(self.week, "1999-01-01", row["key"], 1, None)

    def test_a_session_without_an_issue_is_not_pulled_into_another_issue(self):
        now = time.time()
        self._session(issue={"key": "grp/ts1#issue#8", "iid": 8, "title": "Eight"},
                      turns=[{"n": 1, "prompt": "a", "started": now - 100}])
        loose = self._session(title="no number", turns=[{"n": 1, "prompt": "b", "started": now - 90}])
        rep = self.m.timesheet(self.week)
        row = next(r for r in rep["rows"] if r["key"] == "p:%s" % self.key)
        self.assertEqual([x["id"] for x in row["sessions"]], [loose.id])
        self.assertIn(loose.id, [u["id"] for u in rep["unassigned"]])

    def test_a_planted_head_fifo_does_not_block_the_view(self):
        """A checkout whose .git/HEAD is a FIFO (planted by a sandbox) is skipped, not waited on."""
        import shutil
        import threading
        shutil.rmtree(os.path.join(_env.PP, "team-x", "tsfifo"), ignore_errors=True)
        # warm the per-project git cache before planting: in the full suite ~66 projects took 7 s cold on a
        # loaded machine (round 13), which tripped the 5 s join below without anything blocking
        self.m.timesheet(self.week)
        root = _env.make_project("tsfifo", {"a.txt": "a\n"})
        os.makedirs(os.path.join(root, ".git"))
        fifo = os.path.join(root, ".git", "HEAD")
        os.mkfifo(fifo)

        def unplant():   # a git that did open the FIFO (a regression) gets EOF instead of outliving the suite
            w = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            os.unlink(fifo)
            time.sleep(0.2)
            os.close(w)
            shutil.rmtree(root, ignore_errors=True)
        self.addCleanup(unplant)
        self.assertFalse(gitops.head_is_plain(root))
        self.assertTrue(gitops.head_is_plain(self.root))
        projects._DISCOVERED["at"] = 0   # discover() caches 60 s: without this the view may never see tsfifo
        self.assertIn(os.path.realpath(root), [p["path"] for p in projects.load(os.environ.get("PROJECT_PATH", "")).values()])
        out = []
        th = threading.Thread(target=lambda: out.append(self.m.timesheet(self.week)), daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "timesheet() blocked on a planted .git/HEAD")

    def test_a_reply_from_the_gui_extends_the_block(self):
        now = time.time()
        if timesheet.day_of(now - 240) != timesheet.day_of(now):
            self.skipTest("just after midnight")
        s = self._session(issue={"key": "grp/ts1#issue#5", "iid": 5, "title": "Five"},
                          turns=[{"n": 1, "prompt": "start", "started": now - 240}])
        common.audit({"event": "gui-send", "session": s.id, "chars": 3})
        row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#5")
        spans = [b["end"] - b["start"] for b in row["days"][timesheet.day_of(now)]["blocks"]]
        self.assertEqual(len(spans), 1)
        self.assertGreaterEqual(spans[0], 239)   # the turn and the reply are one block

    def test_report_survives_a_deleted_session_record(self):
        self._enable()
        s = self._session(project="team-x/ts-gone", issue={"key": "grp/ts1#issue#7", "iid": 7, "title": "Seven"})
        t0 = time.time() - 90
        self.m.ts_record(s, "prompt", t0)
        self.m._ts_focus.clear()
        self.m.ts_record(s, "focus", t0 + 60)
        self.m.sessions.pop(s.id)
        row = next(r for r in self.m.timesheet(self.week)["rows"] if r["key"] == "grp/ts1#issue#7")
        cell = row["days"][timesheet.day_of(t0)]
        # other test repositories' commits of this moment may take a share (overlap split)
        self.assertEqual([(round(b["end"] - b["start"]), b["n"]) for b in cell["blocks"]], [(60, 2)])
        self.assertLessEqual(cell["raw_min"], 1.0)
        self.assertEqual(row["title"], "Seven")


class FakeTs:
    def __init__(self):
        self.calls = []

    def timesheet(self, week):
        if week == "bad":
            raise ValueError("week must look like 2026-W40")
        return {"week": week or "now"}

    def ts_settings(self):
        return dict(timesheet.DEFAULTS)

    def ts_focus(self, sid):
        if sid != "s1":
            raise KeyError(sid)
        return {"ok": True, "recorded": False}

    def ts_edit(self, week, day, key, minutes, text):
        self.calls.append(("edit", week, day, key, minutes, text))
        return {"ok": True}

    def ts_manual(self, week, body):
        self.calls.append(("manual", week, body.get("label")))
        return {"ok": True}

    def ts_assign(self, sid, key, title):
        if key == "bad":
            raise ValueError("expected group/project#123")
        return {"ok": True}


class RoutesTest(unittest.TestCase):
    """piperd routes of the view (socketpair helpers of test_piperd, no port is bound)."""

    def setUp(self):
        import piperd
        from test_piperd import get, post
        self.get, self.post = get, post
        self.old, piperd.MANAGER = piperd.MANAGER, FakeTs()
        self.addCleanup(setattr, piperd, "MANAGER", self.old)
        self.fake, self.tok = piperd.MANAGER, common.mint_token("ts-test")

    def test_routes(self):
        t = self.tok
        self.assertEqual(self.get("/api/timesheet?week=2026-W40")[0], 401)
        self.assertEqual(json.loads(self.get("/api/timesheet?week=2026-W40", token=t)[2])["week"], "2026-W40")
        self.assertEqual(self.get("/api/timesheet?week=bad", token=t)[0], 409)
        self.assertFalse(json.loads(self.get("/api/timesheet/settings", token=t)[2])["enabled"])
        self.assertEqual(self.post("/api/timesheet/focus", {"session": "s1"}, t)[0], 200)
        self.assertEqual(self.post("/api/timesheet/focus", {"session": "zz"}, t)[0], 404)
        self.assertEqual(self.post("/api/timesheet/edit", {"week": "2026-W40", "day": "2026-09-29", "key": "k", "minutes": None}, t)[0], 200)
        self.assertEqual(self.fake.calls[-1], ("edit", "2026-W40", "2026-09-29", "k", None, None))
        self.assertEqual(self.post("/api/timesheet/manual", {"week": "2026-W40", "label": "x"}, t)[0], 200)
        self.assertEqual(self.post("/api/timesheet/assign", {"session": "s1", "key": "bad"}, t)[0], 409)
        self.assertEqual(self.post("/api/timesheet/settings", {"enabled": True}, t)[0], 404)   # no own settings UI
        self.assertEqual(self.post("/api/timesheet/nope", {}, t)[0], 404)


if __name__ == "__main__":
    unittest.main()
