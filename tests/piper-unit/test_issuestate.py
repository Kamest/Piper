"""9.12 D: issue memory across days (issuestate.py) - the state file's parsing and piperd's facts
block, the CLAUDE.md block, the board card's stale flag, the Edit guard (working session,
changed file, hidden file, symlink), "Update issue status" for a live and an ended
session, and the inbox pointer + preview."""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

import _env
import common
import extensions
import gitlab
import issuestate
import projects
import sessioncfg
from test_inbox import FakeGitLab, checkout, iso

ISSUE = {"key": "grp/ist#issue#7", "gl": "grp/ist", "type": "issue", "iid": 7, "title": "Shipping", "web_url": "u7"}
BODY = """# #7 Shipping

## Status
The recalculation fix is done, waiting for the client.

## Done
- test

## Waiting on
- client: confirm the rate
"""


class PureTest(unittest.TestCase):
    def test_paths_and_sections(self):
        self.assertEqual(issuestate.rel_path(ISSUE), "development/gitlab-7-status.md")
        self.assertEqual(issuestate.rel_path(dict(ISSUE, type="mr")), "development/gitlab-mr-7-status.md")
        t = issuestate.with_facts(BODY, "## Facts (piperd, x)\n\n- Branch: `a`")
        sec = issuestate.sections(t)
        self.assertEqual(sec["Status"], "The recalculation fix is done, waiting for the client.")
        self.assertEqual(sec["Waiting on"], "- client: confirm the rate")
        self.assertNotIn("Facts", "".join(sec))
        t2 = issuestate.with_facts(t, "## Facts (piperd, y)\n\n- Branch: `b`")
        self.assertEqual(t2.count(issuestate.FACTS_BEGIN), 1)   # replaced, never appended twice
        self.assertIn("`b`", t2)
        self.assertTrue(t2.startswith(BODY.rstrip("\n")))      # Claude's sections untouched
        tpl = issuestate.template(ISSUE)
        self.assertEqual(list(issuestate.sections(tpl)), list(issuestate.SECTIONS))
        self.assertIn("development/gitlab-mr-7-status.md", issuestate.claude_md(dict(ISSUE, type="mr")))


    def test_an_extensions_older_headings_and_markers_still_parse(self):
        """An extension maps older headings onto the sections and names older facts markers: an existing
        file parses, and the next facts write replaces the old block instead of appending a second one."""
        hooks = {"issue_sections": {"Progress": "Status", "Blocked by": "Waiting on", "Junk": "Nope"},
                 "issue_facts_markers": ("old:facts",)}
        old = ("# #7 X\n\n## Progress\nin progress\n\n## Blocked by:\n- client\n\n"
               "<!-- old:facts -->\n## Facts (old, x)\n\n- Branch: `a`\n<!-- /old:facts -->\n")
        self.assertEqual(issuestate.sections(old).get("Status"), None)   # without the extension: not a section
        with mock.patch.object(extensions, "default", side_effect=lambda k, d: hooks.get(k, d)):
            self.assertEqual(issuestate.legacy_sections(), {"progress": "Status", "blocked by": "Waiting on"})
            sec = issuestate.sections(old)
            self.assertEqual((sec["Status"], sec["Waiting on"]), ("in progress", "- client"))
            new = issuestate.with_facts(old, "## Facts (piperd, y)\n\n- Branch: `b`")
        self.assertNotIn("old:facts", new)
        self.assertEqual(new.count(issuestate.FACTS_BEGIN), 1)
        self.assertTrue(new.startswith("# #7 X\n\n## Progress\nin progress"))

    def test_markers_quoted_in_the_prose_never_swallow_it(self):
        """Claude may echo the CLAUDE.md sentence naming the markers into its prose: only a marker on
        its own line is piperd's block, so the sections after the quote survive piperd's writes."""
        body = ("# #7 X\n\n## Status\ndone\n\n## Decisions\n- The block between `%s` and `%s` is maintained by piperd\n"
                "- only `%s` mentioned alone\n\n## Remaining\n- IMPORTANT\n" % (issuestate.FACTS_BEGIN, issuestate.FACTS_END,
                                                                            issuestate.FACTS_BEGIN))
        t = issuestate.with_facts(body, "## Facts (piperd, a)\n\n- a")
        t2 = issuestate.with_facts(t, "## Facts (piperd, b)\n\n- b")
        self.assertTrue(t2.startswith(body.rstrip("\n")))                     # the prose untouched
        self.assertEqual(t2.count("\n" + issuestate.FACTS_BEGIN + "\n"), 1)  # one block, replaced
        self.assertIn("- b", t2)
        self.assertNotIn("- a\n", t2)
        sec = issuestate.sections(t2)
        self.assertEqual(sec["Remaining"], "- IMPORTANT")
        self.assertIn("is maintained by piperd", sec["Decisions"])
        # Claude dropped the END line and kept BEGIN alone: piperd appends a new block, the prose after BEGIN stays
        stray = body + "\n" + issuestate.FACTS_BEGIN + "\n## Done\n- SECOND\n"
        t3 = issuestate.with_facts(issuestate.with_facts(stray, "## Facts (piperd, a)\n\n- a"), "## Facts (piperd, b)\n\n- b")
        self.assertTrue(t3.startswith(stray.rstrip("\n")))
        self.assertEqual(issuestate.sections(t3)["Done"], "- SECOND")

    def test_a_crlf_file_gets_its_block_replaced_in_its_own_line_endings(self):
        """A file saved with CRLF (Windows editor): the block is found and replaced, not appended again."""
        crlf = BODY.replace("\n", "\r\n")
        t = issuestate.with_facts(crlf, "## Facts (piperd, a)\n\n- a")
        self.assertNotIn("\n", t.replace("\r\n", ""))
        t = t.replace("\r\n", "\n").replace("\n", "\r\n")   # the editor saved the whole file with CRLF
        t2 = issuestate.with_facts(t, "## Facts (piperd, b)\n\n- b")
        self.assertEqual(t2.count(issuestate.FACTS_BEGIN), 1)
        self.assertIn("- b", t2)
        self.assertNotIn("- a", t2)
        self.assertTrue(t2.startswith(crlf.rstrip("\r\n")))
        self.assertNotIn("\n", t2.replace("\r\n", ""))   # no bare LF mixed in


class IssueStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, pty=True)
        cls.app = checkout("istapp", "grp/ist", {".hole/settings.json": json.dumps(
            {"files": {"exclude": ["development/gitlab-9-status.md"]}})})
        _env.register("istapp", cls.app, gitlab=["grp/ist"])
        cls.m.repo_top(cls.app, record="start")
        projects._DISCOVERED["at"] = 0.0

    def setUp(self):
        if os.path.exists(issuestate.SEEN_FILE):
            os.unlink(issuestate.SEEN_FILE)
        self.dev = os.path.join(self.app, "development")
        subprocess.run(["rm", "-rf", self.dev], check=True)
        self.addCleanup(subprocess.run, ["rm", "-rf", self.dev])
        self.gl = FakeGitLab()
        self.m.gitlab_opener = self.gl
        self.addCleanup(setattr, self.m, "gitlab_opener", None)
        p = mock.patch.object(gitlab, "token", return_value="tok")
        p.start()
        self.addCleanup(p.stop)

    def session(self, state="waiting", issue=ISSUE, pid=None, turns=None):
        import sessions
        s = sessions.Session(project="istapp", path=self.app, state=state, issue=dict(issue), pid=pid,
                             turns=turns if turns is not None else [{"n": 1, "prompt": "p", "started": time.time() - 60,
                                                                     "ended": time.time() - 30}])
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        self.addCleanup(setattr, s, "state", "exited")
        return s

    def write(self, rel, text):
        full = os.path.join(self.app, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
        return full

    def test_claude_md_block(self):
        import sessions
        s = self.session()
        md = self.m.issue_md(s)
        self.assertIn("development/gitlab-7-status.md", md)
        box = os.path.join(_env.ROOT, "istbox")
        sessioncfg.fresh_dir(box)
        sessioncfg.build_box(box, "/h/.claude", "/h/.piper", "session", extra_md=md)
        with open(os.path.join(box, "piper", "system-prompt.md")) as f:
            self.assertIn("## Work status of #7", f.read())
        self.assertEqual(self.m.issue_md(self.session(issue=dict(ISSUE, iid=9, key="grp/ist#issue#9"))), "")   # hidden
        plain = sessions.Session(project="istapp", path=self.app, state="waiting")
        self.assertEqual(self.m.issue_md(plain), "")

    def test_card_stale(self):
        s = self.session()
        c = self.m.issue_state_card(s)
        self.assertEqual((c["path"], c["exists"], c["stale"]), ("development/gitlab-7-status.md", False, True))
        full = self.write("development/gitlab-7-status.md", BODY)
        os.utime(full, (time.time() - 120, time.time() - 120))
        self.assertTrue(self.m.issue_state_card(s)["stale"])    # last written before the turn ended
        self.write("development/gitlab-7-status.md", BODY + "\n- next step\n")
        self.assertFalse(self.m.issue_state_card(s)["stale"])   # updated after the turn ended
        s.state = "working"
        self.assertFalse(self.m.issue_state_card(s)["stale"])   # never while Claude works
        self.assertFalse(self.m.issue_state_card(self.session(turns=[]))["stale"])   # nothing done yet
        self.assertIsNone(self.m.issue_state_card(self.session(issue=dict(ISSUE, iid=9, key="grp/ist#issue#9"))))
        card = {x["id"]: x for x in self.m.board()}[s.id]
        self.assertEqual(card["istate"]["path"], "development/gitlab-7-status.md")

    def test_a_state_written_during_the_turn_is_fresh(self):
        """Claude updates the file in its turn, then writes its answer: Stop comes later - not stale."""
        now = time.time()
        s = self.session(state="waiting", pid=123, turns=[{"n": 1, "prompt": "a", "started": now - 60, "ended": now - 5}])
        s.idle = True
        full = self.write("development/gitlab-7-status.md", BODY)
        os.utime(full, (now - 45, now - 45))          # during the turn, 40 s before its Stop
        c = self.m.issue_state_card(s)
        self.assertFalse(c["stale"])
        self.assertTrue(c["prompt"])
        s.turns.append({"n": 2, "prompt": "b", "started": now - 3, "ended": now - 1})   # a turn without an update
        self.assertTrue(self.m.issue_state_card(s)["stale"])
        s.state, s.idle = "attention", False           # a permission question: no button for a live session
        self.assertFalse(self.m.issue_state_card(s)["prompt"])

    def test_a_cut_off_last_turn_counts_once_the_session_ended(self):
        """The Stop button (or a crash) before the Stop hook leaves the turn open: an ended session still gets the button."""
        now = time.time()
        s = self.session(state="exited", pid=4242,    # the pid stays set after an exit (_on_exit, stop)
                         turns=[{"n": 1, "prompt": "a", "started": now - 1200, "ended": None}])
        c = self.m.issue_state_card(s)
        self.assertTrue((c["stale"], c["exists"], c["live"]) == (True, False, False))
        full = self.write("development/gitlab-7-status.md", BODY)
        os.utime(full, (now - 600, now - 600))          # written in that turn
        self.assertFalse(self.m.issue_state_card(s)["stale"])
        s.turns = [{"n": 1, "prompt": "a", "started": now - 1200, "ended": now - 900},
                   {"n": 2, "prompt": "b", "started": now - 300, "ended": None}]   # the cut-off one did not update it
        self.assertTrue(self.m.issue_state_card(s)["stale"])
        s.state = "working"                              # a turn in progress is not "over"
        self.assertFalse(self.m.issue_state_card(s)["stale"])
        s.state, s.idle = "waiting", True                # Stop came, the turn's snapshot is still being taken
        self.assertFalse(self.m.issue_state_card(s)["stale"])   # no button blinking in that window
        s.state, s.pid = "preparing", None               # Continue: starting again
        self.assertFalse(self.m.issue_state_card(s)["stale"])
        s.turns[-1]["ended"] = now - 200                  # every turn closed, the prose still old: stale ...
        for st in ("preparing", "spawning", "trust", "starting", "attesting"):
            s.state = st
            c = self.m.issue_state_card(s)
            self.assertEqual((c["stale"], c["live"], c["prompt"]), (True, True, False), st)   # ... but live: no button
            with self.assertRaises(ValueError):          # and no summary claiming the session "ended"
                self.m.issue_state_update(s.id)
        row = next(r for r in self.m.list() if r["id"] == s.id)
        self.assertIs(row["at_prompt"], False)            # the flag the GUI uses (inbox follow-up)

    def test_a_broken_seen_file_does_not_break_the_board(self):
        s = self.session()
        self.write("development/gitlab-7-status.md", BODY)
        with open(issuestate.SEEN_FILE, "w") as f:
            f.write("{broken")
        self.assertIn("stale", self.m.issue_state_card(s))

    def test_piperds_facts_write_does_not_count_as_an_update(self):
        """After every turn piperd rewrites its facts block: the prose is still as old as it was."""
        s = self.session(state="waiting", pid=123)
        s.idle = True
        full = self.write("development/gitlab-7-status.md", BODY)
        old = time.time() - 300
        os.utime(full, (old, old))
        self.assertTrue(self.m.issue_state_card(s)["stale"])
        self.write("src/B.java", "changed in the turn\n")   # the facts (uncommitted files) change
        self.assertEqual(self.m._istate_write(s, self.m.gui_project("istapp")), "development/gitlab-7-status.md")
        self.assertGreater(os.path.getmtime(full), old + 200)   # the file itself is new ...
        self.assertTrue(self.m.issue_state_card(s)["stale"])    # ... its Status / Done are not

    def test_facts_write_and_guard(self):
        s = self.session(state="exited")
        self.assertIsNone(self.m._istate_write(s, self.m.gui_project("istapp")))   # no file, no create
        full = self.write("development/gitlab-7-status.md", BODY)
        self.write("development/gitlab-7-analysis.md", "a")
        self.write("src/A.java", "x")
        self.write("development/gitlab-9-status.md", "TOPSECRET")   # hidden by the project's excludes
        rel = self.m._istate_write(s, self.m.gui_project("istapp"))
        self.assertEqual(rel, "development/gitlab-7-status.md")
        with open(full) as f:
            text = f.read()
        self.assertTrue(text.startswith(BODY.rstrip("\n")))
        self.assertIn("## Facts (piperd", text)
        self.assertIn("`development/gitlab-7-analysis.md`", text)
        self.assertIn("Issue: u7", text)
        self.assertIn("`src/A.java`", text)
        self.assertNotIn("gitlab-9-status", text)   # a hidden file is never named
        self.assertIsNone(self.m._istate_write(s, self.m.gui_project("istapp")))   # facts unchanged: no write
        # another session of the tree works: refused
        w = self.session(state="working", issue={"key": "x#issue#1", "gl": "grp/ist", "type": "issue", "iid": 1,
                                                 "title": "", "web_url": ""})
        self.write("src/B.java", "y")
        with self.assertRaises(ValueError):
            self.m._istate_write(s, self.m.gui_project("istapp"))
        w.state = "exited"
        # the file changes between read and write (Claude edits it): refused
        real = self.m._istate_facts

        def racing(*a, **kw):
            with open(full, "a") as f:
                f.write("\n- Claude is writing right now\n")
            return real(*a, **kw)
        with mock.patch.object(self.m, "_istate_facts", racing):
            with self.assertRaises(ValueError):
                self.m._istate_write(s, self.m.gui_project("istapp"))
        # a symlink is never read nor written through
        os.unlink(full)
        target = os.path.join(_env.ROOT, "ist-outside.md")
        with open(target, "w") as f:
            f.write("outside")
        os.symlink(target, full)
        with self.assertRaises(ValueError):
            self.m._istate_write(s, self.m.gui_project("istapp"), create=True)
        with open(target) as f:
            self.assertEqual(f.read(), "outside")
        self.assertIsNone(self.m.issue_state_summary(self.m.gui_project("istapp"), ISSUE))

    def test_a_non_utf8_file_gets_its_facts_and_keeps_its_bytes(self):
        """A cp1250 status file: the facts are written, the human's prose stays byte for byte."""
        s = self.session(state="exited")
        prose = BODY.replace("waiting for the client", "für den Kunden geändert").encode("cp1250")
        full = os.path.join(self.dev, "gitlab-7-status.md")
        os.makedirs(self.dev, exist_ok=True)
        with open(full, "wb") as f:
            f.write(prose)
        self.assertEqual(self.m._istate_write(s, self.m.gui_project("istapp")), "development/gitlab-7-status.md")
        with open(full, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(prose.rstrip(b"\n")))
        self.assertIn(b"## Facts (piperd", data)

    def test_hidden_never_written(self):
        s = self.session(state="exited", issue=dict(ISSUE, iid=9, key="grp/ist#issue#9"))
        with self.assertRaises(ValueError):
            self.m.issue_state_update(s.id)
        self.assertFalse(os.path.exists(os.path.join(self.dev, "gitlab-9-status.md")))

    def test_a_burst_of_stop_events_is_one_refresh_worker(self):
        import threading
        s = self.session(state="waiting", pid=123)
        calls, gate = [], threading.Event()

        def slow(sess, p, *a, **k):
            calls.append(1)
            gate.wait(5)
        with mock.patch.object(self.m, "_istate_write", slow):
            for _ in range(30):                          # 30 sandbox-written Stop lines at once
                self.m.issue_state_refresh_soon(s)
            time.sleep(0.2)
            gate.set()
            end = time.time() + 5
            while s.id in self.m._istate_busy and time.time() < end:
                time.sleep(0.05)
        self.assertEqual(len(calls), 2)                  # the running one + one more for those that came meanwhile
        self.assertNotIn(s.id, self.m._istate_busy)

    def test_update_live_and_ended(self):
        live = self.session(state="waiting", pid=123)
        live.idle = True   # at its prompt after a turn
        typed = []
        with mock.patch.object(self.m, "type_text", lambda s, t: typed.append((s.id, t))):
            with mock.patch.object(self.m, "input_pending", lambda s: 5):   # a half-written prompt
                with self.assertRaises(ValueError):
                    self.m.issue_state_update(live.id)
            self.assertEqual(typed, [])
            r = self.m.issue_state_update(live.id)
            self.assertEqual(r, {"sent": True, "path": "development/gitlab-7-status.md"})
            self.assertIn("development/gitlab-7-status.md", typed[0][1])
            live.state, live.idle = "attention", True   # idle_prompt ~60 s after Stop: still at its prompt
            self.assertEqual(self.m.issue_state_update(live.id)["sent"], True)
            live.idle = False                           # a real question (permission): not now
            with self.assertRaises(ValueError):
                self.m.issue_state_update(live.id)
            live.state = "working"
            with self.assertRaises(ValueError):
                self.m.issue_state_update(live.id)
            # a fresh start whose task is not typed yet: the task goes first (it waits for Claude's screen)
            live.state, live.idle, live.task, live.task_sent = "ready", False, "fix #7", False
            with self.assertRaises(ValueError):
                self.m.issue_state_update(live.id)
            live.task_sent = True   # a resumed session past attestation: at its prompt (like FOLLOW_STATES)
            self.assertTrue(self.m.issue_state_update(live.id)["sent"])
        live.state = "exited"
        ended = self.session(state="exited")
        ended.claude_session = "0f1e2d3c-aaaa-bbbb"
        tdir = os.path.join(ended.cfg, "projects", "-x")
        os.makedirs(tdir, exist_ok=True)
        recs = [{"type": "user", "message": {"content": "fix the shipping recalculation"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done, the test passed. password=hunter2"}]}}]
        with open(os.path.join(tdir, ended.claude_session + ".jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        r = self.m.issue_state_update(ended.id)
        self.assertEqual(r["written"], "development/gitlab-7-status.md")
        with open(os.path.join(self.dev, "gitlab-7-status.md")) as f:
            text = f.read()
        self.assertEqual(list(issuestate.sections(text)), list(issuestate.SECTIONS))   # created from the template
        self.assertIn("- You: fix the shipping recalculation", text)
        self.assertIn("Done, the test passed.", text)
        self.assertNotIn("hunter2", text)
        self.assertTrue([e for e in common.audit_tail(50) if e.get("event") == "issue-state-write"])
        self.assertFalse(self.m.issue_state_card(ended)["stale"])

    def test_inbox_pointer_and_preview(self):
        full = self.write("development/gitlab-7-status.md", BODY)
        old = time.time() - 3600
        os.utime(full, (old, old))
        g = self.gl
        g.issues.append(g.obj("grp/ist", "issue", 7))
        g.note("grp/ist", "issue", 7, created=old - 60)
        new = g.note("grp/ist", "issue", 7, created=time.time() - 10)
        hint = self.m.issue_state_hint(self.m.gui_project("istapp"), ISSUE)
        self.assertIn("development/gitlab-7-status.md", hint)
        self.assertIn("note_%d" % new["id"], hint)
        self.assertEqual(hint.count("note_"), 1)
        summ = self.m.issue_state_summary(self.m.gui_project("istapp"), ISSUE)
        self.assertEqual(summ["status"], "The recalculation fix is done, waiting for the client.")
        self.assertEqual(summ["waiting"], "- client: confirm the rate")
        with mock.patch.object(gitlab, "token", return_value=None):   # no token: the pointer only
            self.assertNotIn("note_", self.m.issue_state_hint(self.m.gui_project("istapp"), ISSUE))
        os.unlink(full)
        self.assertEqual(self.m.issue_state_hint(self.m.gui_project("istapp"), ISSUE), "")

    def test_the_inbox_summary_is_unscrubbed_the_template_title_scrubbed(self):
        """L5 (2026-10-07): the human's preview shows the file as Files opens it; the template a session
        reads names the issue with its title scrubbed (the inbox keeps GitLab's raw title, L1)."""
        self.write("development/gitlab-7-status.md", BODY.replace("waiting for the client.", "db password=hunter2"))
        summ = self.m.issue_state_summary(self.m.gui_project("istapp"), ISSUE)
        self.assertIn("password=hunter2", summ["status"])
        tpl = issuestate.template(dict(ISSUE, title="Rotate password=hunter2"))
        self.assertNotIn("hunter2", tpl)

    def test_inbox_start_and_item(self):
        self.write("development/gitlab-7-status.md", BODY)
        g = self.gl
        g.issues.append(g.obj("grp/ist", "issue", 7))
        self.m._inbox_init()
        self.m._inbox_poll()
        with mock.patch.object(self.m, "_start", lambda s: None):
            r = self.m.inbox_start("grp/ist#issue#7", "work", "istapp")
        s = self.m.get(r["id"])
        self.addCleanup(setattr, s, "hidden", True)
        self.assertIn("The status of the previous work is in development/gitlab-7-status.md", s.task)
        item = self.m.inbox_item("grp/ist#issue#7")
        self.assertEqual(item["state_file"]["path"], "development/gitlab-7-status.md")


if __name__ == "__main__":
    unittest.main()
