"""9.8 GitLab inbox (inbox.py) against a fake GitLab: baseline, new items and notes, seen /
unseen, to-do done, start + follow-up, comment, rules + automation limits, add-project,
the comment draft (hidden / symlink / outside development refused), gitlab_inbox scope,
the board flow, /api/state and the HTTP routes."""
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from unittest import mock

import _env
import common
import extensions
import gitlab
import inbox
import piperd
import projects
from test_piperd import get, post

GENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
HOST = "gitlab.com"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def checkout(name: str, origin: str, files=None) -> str:
    root = os.path.realpath(_env.make_project(name, dict({"README.md": "x\n"}, **(files or {}))))
    subprocess.run(["git", "-C", root, "init", "-q", "-b", "main"], check=True, env=GENV)
    subprocess.run(["git", "-C", root, "remote", "add", "origin", "https://%s/%s.git" % (HOST, origin)],
                   check=True, env=GENV)
    return root


class IssueFilesTest(unittest.TestCase):
    def test_an_extension_renames_the_issue_files_and_the_comment_style(self):
        it = {"gl": "grp/app", "type": "issue", "iid": 7, "title": "T", "web_url": "https://gitlab.example.com/x"}
        self.assertIn("development/gitlab-7-comment.md (a short summary on top", inbox.task_for(it, "work"))
        self.assertNotIn("memo/", inbox.analysis_dirs())
        hooks = {"issue_files": {"analysis": "notes", "comment": "reply", "comment_style": "formal"}}
        with mock.patch.object(extensions, "default", side_effect=lambda k, d: hooks.get(k, d)), \
                mock.patch.object(extensions, "items", side_effect=lambda k: ["memo/"] if k == "ANALYSIS_DIRS" else []):
            self.assertEqual(inbox.issue_files()["status"], "status")   # what the extension leaves out stays
            self.assertIn("development/gitlab-7-reply.md (formal)", inbox.task_for(it, "work"))
            self.assertIn("development/gitlab-7-notes.md", inbox.task_for(it, "analyze"))
            self.assertEqual(inbox.analysis_dirs()[-1], "memo/")


class FakeGitLab:
    """A tiny stateful GitLab: /user, /todos, /issues, /merge_requests, notes, mark_as_done, notes POST."""

    def __init__(self):
        self.me = "alice"
        self.todos, self.issues, self.mrs = [], [], []
        self.notes: dict = {}     # (gl, coll, iid) -> [note] (ascending)
        self.calls = []
        self.fail = None
        self.next_id = 1000

    def obj(self, gl, kind, iid, title="T", notes=0, **kw):
        sep = "#" if kind == "issue" else "!"
        coll = "issues" if kind == "issue" else "merge_requests"
        d = {"iid": iid, "title": title, "state": "opened", "labels": ["bug"], "author": {"username": "bob", "name": "A K"},
             "updated_at": iso(time.time()), "due_date": None, "milestone": None, "user_notes_count": notes,
             "web_url": "https://%s/%s/-/%s/%d" % (HOST, gl, "work_items" if kind == "issue" else coll, iid),
             "references": {"full": "%s%s%d" % (gl, sep, iid)}, "project_id": 1, "description": "issue body"}
        d.update(kw)
        return d

    def todo(self, gl, kind, iid, action, created=None, note_id=None, notes=0, title="T"):
        self.next_id += 1
        t = {"id": self.next_id, "action_name": action, "target_type": "Issue" if kind == "issue" else "MergeRequest",
             "target": self.obj(gl, kind, iid, title, notes), "project": {"path_with_namespace": gl},
             "author": {"username": "bob", "name": "A K"}, "created_at": iso(created or time.time()),
             "body": "@alice please password=hunter2", "state": "pending",
             "target_url": "https://%s/%s/-/work_items/%d%s" % (HOST, gl, iid, "#note_%d" % note_id if note_id else "")}
        self.todos.append(t)
        return t

    def note(self, gl, kind, iid, user="bob", created=None, system=False):
        self.next_id += 1
        coll = "issues" if kind == "issue" else "merge_requests"
        n = {"id": self.next_id, "body": "a comment token=abc", "author": {"username": user, "name": user.upper()},
             "created_at": iso(created or time.time()), "system": system}
        self.notes.setdefault((gl, coll, iid), []).append(n)
        if not system:
            for rows in (self.issues, self.mrs, [t["target"] for t in self.todos]):
                for r in rows:
                    if r["references"]["full"] in ("%s#%d" % (gl, iid), "%s!%d" % (gl, iid)) and \
                            (coll == "issues") == ("#" in r["references"]["full"]):
                        r["user_notes_count"] = r.get("user_notes_count", 0) + 1
        return n

    def __call__(self, req, timeout=None):
        from test_workbench97 import FakeResp
        u = urllib.parse.urlsplit(req.full_url)
        path = urllib.parse.unquote(u.path)[len("/api/v4"):]
        q = dict(urllib.parse.parse_qsl(u.query))
        method = req.get_method()
        body = json.loads(req.data) if req.data else None
        self.calls.append((method, path, q, body))
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "x", {}, io.BytesIO(b"{}"))
        out = self.route(method, path, q, body)
        if out is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO(b"{}"))
        return FakeResp(json.dumps(out).encode())

    def route(self, method, path, q, body):
        page = int(q.get("page") or 1)
        if method == "GET" and path == "/user":
            return {"username": self.me}
        if method == "GET" and path == "/todos":
            return [t for t in self.todos if t["state"] == "pending"] if page == 1 else []
        if method == "GET" and path == "/issues":
            return self.issues if page == 1 else []
        if method == "GET" and path == "/merge_requests":
            return self.mrs if page == 1 else []
        if method == "POST" and path.startswith("/todos/") and path.endswith("/mark_as_done"):
            tid = int(path.split("/")[2])
            for t in self.todos:
                if t["id"] == tid:
                    t["state"] = "done"
                    return t
            return None
        parts = path.split("/")   # /projects/<gl...>/<coll>/<iid>[/notes]
        if parts[1] != "projects":
            return None
        if parts[-1] == "notes":
            coll, iid, gl = parts[-3], int(parts[-2]), "/".join(parts[2:-3])
            if method == "POST":
                self.next_id += 1
                n = {"id": self.next_id, "body": body["body"], "author": {"username": self.me}, "system": False,
                     "created_at": iso(time.time())}
                self.notes.setdefault((gl, coll, iid), []).append(n)
                return n
            rows = list(self.notes.get((gl, coll, iid), []))
            if q.get("sort") == "desc":
                rows.reverse()
            return rows[:int(q.get("per_page") or 20)]
        coll, iid, gl = parts[-2], int(parts[-1]), "/".join(parts[2:-2])
        for r in (self.issues if coll == "issues" else self.mrs) + [t["target"] for t in self.todos]:
            if r["iid"] == iid and r["references"]["full"].startswith(gl):
                return dict(r, head_pipeline={"status": "failed"}) if coll == "merge_requests" else r
        return None


class InboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls, pty=True)
        cls.app = checkout("inbapp", "grp/app", {".hole/settings.json": json.dumps(
            {"files": {"exclude": ["development/secret-7-comment.md"]}})})
        _env.register("inbapp", cls.app, gitlab=["grp/app"])
        cls.disc = checkout("inbdisc", "grp/disc")
        cls.dup1 = checkout("inbdup1", "grp/dup")
        cls.dup2 = checkout("inbdup2", "grp/dup")
        projects._DISCOVERED["at"] = 0.0

    def setUp(self):
        gitlab._ME.clear()
        for f in (inbox.INBOX_FILE, inbox.RULES_FILE):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        self.m._inbox_init()
        self.gl = FakeGitLab()
        self.m.gitlab_opener = self.gl
        self.addCleanup(setattr, self.m, "gitlab_opener", None)
        p = mock.patch.object(gitlab, "token", return_value="tok")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(self.m, "_start", lambda s: None)   # no sandbox: the session stays "preflight"
        p.start()
        self.addCleanup(p.stop)
        self.started = []

    def tearDown(self):
        for s in list(self.m.sessions.values()):
            if s.issue or s.project == "inbapp":
                s.state, s.hidden = "exited", True
                s.save()

    def poll(self):
        self.m._inbox_poll()
        return {v["key"]: v for v in self.m.inbox()["items"]}

    def audit_events(self, name):
        return [r for r in common.audit_tail(300) if r.get("event") == name]

    # ------------------------------------------------------------ polling
    def test_no_token(self):
        with mock.patch.object(gitlab, "token", return_value=None):
            self.m._inbox_poll()
        ib = self.m.inbox()
        self.assertEqual((ib["status"], ib["items"]), ("no-token", []))
        self.assertEqual(self.gl.calls, [])
        self.assertEqual(self.m.inbox_summary()["status"], "no-token")

    def test_baseline_new_items_and_notes(self):
        g = self.gl
        g.note("grp/app", "issue", 7)
        g.note("grp/app", "issue", 7, system=True)
        g.todo("grp/app", "issue", 7, "assigned", notes=1)
        g.issues.append(g.obj("grp/app", "issue", 7, notes=1))
        g.issues.append(g.obj("grp/none", "issue", 3))
        g.mrs.append(g.obj("grp/app", "mr", 5))
        items = self.poll()
        self.assertEqual(set(items), {"grp/app#issue#7", "grp/none#issue#3", "grp/app#mr#5"})
        self.assertFalse(any(v["new"] or v["new_notes"] for v in items.values()))   # baseline: no flood
        it = items["grp/app#issue#7"]
        self.assertEqual((it["reasons"], it["project"], it["notes_count"]), (["assigned"], "inbapp", 1))
        self.assertEqual(it["todos"][0]["body"], "@alice please password=hunter2")   # the human's view: raw (L1)
        self.assertEqual((items["grp/none#issue#3"]["project"], items["grp/none#issue#3"]["candidates"]), (None, []))
        self.assertEqual(items["grp/app#mr#5"]["reasons"], ["authored"])
        self.assertEqual((it["listed"], items["grp/app#mr#5"]["listed"]), (["assigned"], ["authored"]))
        self.assertEqual(self.m.inbox_summary()["needs"], 1)   # the pending to-do
        self.assertEqual(self.m.inbox_summary()["new"], 0)
        v0 = self.m.inbox_summary()["v"]
        calls = len(g.calls)
        self.poll()   # nothing changed: no notes fetched again, `v` stays
        self.assertFalse([c for c in g.calls[calls:] if c[1].endswith("/notes")])
        self.assertEqual(self.m.inbox_summary()["v"], v0)
        # a new to-do on a discovered checkout's project, a note by someone else, then my own
        g.todo("grp/disc", "issue", 9, "mentioned")
        n = g.note("grp/app", "issue", 7)
        items = self.poll()
        self.assertTrue(items["grp/disc#issue#9"]["new"])
        self.assertEqual(items["grp/disc#issue#9"]["project"], "team-x/inbdisc")
        self.assertEqual((items["grp/app#issue#7"]["new_notes"], items["grp/app#issue#7"]["first_new_note"]), (1, n["id"]))
        g.note("grp/app", "issue", 7, user="alice")
        items = self.poll()
        self.assertEqual(items["grp/app#issue#7"]["new_notes"], 1)   # own notes never count
        order = [v["key"] for v in self.m.inbox()["items"]]
        self.assertEqual(order[-1], "grp/app#mr#5")   # authored without news last
        self.assertEqual(set(order[:2]), {"grp/disc#issue#9", "grp/app#issue#7"})
        self.assertEqual(self.m.inbox_summary()["new"], 2)
        self.assertGreater(self.m.inbox_summary()["v"], v0)
        saved = common.read_json(inbox.INBOX_FILE, {})
        self.assertTrue(saved["baseline_done"])
        self.assertEqual(os.stat(inbox.INBOX_FILE).st_mode & 0o777, 0o600)
        # errors keep the last good items
        g.fail = 500
        ib = (self.m._inbox_poll(), self.m.inbox())[1]
        self.assertEqual((ib["status"], len(ib["items"])), ("error", 3 + 1))
        self.assertIn("HTTP 500", ib["error"])

    def test_listed_tells_a_stale_assigned_todo_from_a_current_assignment(self):
        """2026-10-11: the Assigned count is GitLab's "assigned to me" (26), not every item with an
        "assigned" to-do (31: closed or reassigned issues keep theirs)."""
        g = self.gl
        g.todo("grp/app", "issue", 7, "assigned")   # closed / reassigned since: only the to-do is left
        g.issues.append(g.obj("grp/app", "issue", 8))
        items = self.poll()
        self.assertEqual((items["grp/app#issue#7"]["reasons"], items["grp/app#issue#7"]["listed"]), (["assigned"], []))
        self.assertEqual((items["grp/app#issue#8"]["reasons"], items["grp/app#issue#8"]["listed"]), (["assigned"], ["assigned"]))

    def test_bidi_controls_never_reach_a_title(self):
        """F3b E25 2026-10-06: an RLO in an issue title showed the text in another order than stored"""
        self.gl.issues.append(self.gl.obj("grp/app", "issue", 8, title="invoice \u202egpj.exe\u202c fix",
                                          labels=["a\u2066b"], milestone={"title": "m\u200f1"}))
        it = self.poll()["grp/app#issue#8"]
        self.assertEqual((it["title"], it["labels"], it["milestone"]), ("invoice gpj.exe fix", ["ab"], "m1"))

    def test_candidates_and_add_project(self):
        g = self.gl
        g.issues.append(g.obj("grp/dup", "issue", 4))
        it = self.poll()["grp/dup#issue#4"]
        self.assertIsNone(it["project"])
        self.assertEqual(sorted(c["path"] for c in it["candidates"]), sorted([self.dup1, self.dup2]))
        for path, glp in ((self.app, "grp/nope"), ("/etc", "grp/dup"), (self.disc, "grp/dup"),
                          (os.path.join(self.dup1, ".."), "grp/dup")):
            with self.assertRaises(ValueError):
                self.m.inbox_add_project(path, glp)
        r = self.m.inbox_add_project(self.dup1, "grp/dup")
        self.assertEqual(r, {"key": "inbdup1"})
        self.assertEqual(projects.load()["inbdup1"]["gitlab"], ["grp/dup"])
        self.assertEqual(self.m.inbox()["items"][0]["project"], "inbdup1")
        self.assertEqual(self.audit_events("gui-project-add")[0]["key"], "inbdup1")
        data = common.read_json(common.PROJECTS_FILE, {})
        data["projects"].pop("inbdup1")
        common.write_json(common.PROJECTS_FILE, data)

    def test_seen_unseen(self):
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        n = g.note("grp/app", "issue", 7)
        g.todo("grp/app", "issue", 8, "directly_addressed")
        items = self.poll()
        self.assertEqual(items["grp/app#issue#7"]["new_notes"], 1)
        self.assertEqual(self.m.inbox_seen(["grp/app#issue#7"])["seen"], 1)
        it = self.poll()["grp/app#issue#7"]
        self.assertEqual((it["new"], it["new_notes"]), (False, 0))
        self.m.inbox_unseen("grp/app#issue#7")
        it = self.poll()["grp/app#issue#7"]
        self.assertEqual((it["new"], it["new_notes"], it["first_new_note"]), (True, 1, n["id"]))
        self.m.inbox_seen(all_=True)
        self.assertFalse(any(v["new"] or v["new_notes"] for v in self.poll().values()))
        with self.assertRaises(ValueError):
            self.m.inbox_seen("grp/app#issue#7")
        with self.assertRaises(ValueError):
            self.m.inbox_unseen("../etc")

    def test_marking_an_item_unread_is_no_feed_news(self):
        """F-N10-8 (GUI pass D): "Mark as unread" (key e) put the item's old comment back among the new ones -
        the Feed / Recently showed "<title>: 1 new comment" for the user's own action. A comment that really
        arrives afterwards is still news."""
        import feed
        os.makedirs(feed.FEED_DIR, exist_ok=True)
        feed._write(feed.CONFIG_FILE, {"enabled": True})
        self.addCleanup(os.unlink, feed.CONFIG_FILE)
        g, key = self.gl, "grp/app#issue#7"
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        g.note("grp/app", "issue", 7)
        self.assertEqual(self.poll()[key]["new_notes"], 1)
        self.m._feed_init()
        notes = lambda: [e["title"] for e in self.m._feed_events if e["key"] == "notes:" + key]
        with mock.patch.object(self.m, "git_ci", return_value={}):
            self.m._feed_tick()                          # baseline
            self.m.inbox_seen([key])
            self.m._feed_tick()
            self.m.inbox_unseen(key)                     # back to unread: with the undo of "seen"
            self.m._feed_tick()
            self.m.inbox_seen([key])
            self.m._feed_tick()
            self.m.inbox_unseen(key)
            self.m.inbox_seen([key])
            self.m.inbox_unseen(key)
            self.m.inbox_unseen(key)                     # without an undo: the newest comment by someone else
            self.m._feed_tick()
            self.assertEqual(self.poll()[key]["new_notes"], 1)   # unread as the user asked
            self.assertEqual(notes(), [])
            g.note("grp/app", "issue", 7)                # a real new comment
            self.poll()
            self.m._feed_tick()
        self.assertEqual(notes(), ["T: 2 new comments"])

    def test_done(self):
        g = self.gl
        t1 = g.todo("grp/app", "issue", 7, "mentioned")
        t2 = g.todo("grp/app", "issue", 7, "assigned")
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        self.assertEqual(self.m.inbox_done("grp/app#issue#7"), {"done": 2})
        posts = [c[1] for c in g.calls if c[0] == "POST"]
        self.assertEqual(posts, ["/todos/%d/mark_as_done" % t1["id"], "/todos/%d/mark_as_done" % t2["id"]])
        it = self.m.inbox()["items"][0]
        self.assertEqual((it["todos"], it["reasons"]), ([], ["assigned"]))   # still assigned by the issue list
        self.assertEqual(it["listed"], ["assigned"])
        self.assertEqual(self.audit_events("gui-todo-done")[0]["key"], "grp/app#issue#7")
        with self.assertRaises(KeyError):
            self.m.inbox_done("grp/app#issue#99")

    # ------------------------------------------------------------ start / follow-up / comment
    def test_the_human_sees_raw_text_a_session_gets_it_scrubbed(self):
        """L1 (2026-10-07): items and the preview keep GitLab's text; the task, the session title and
        Session.issue (-> the issue state file) scrub it, and so does gitlab_inbox."""
        import sessions
        g = self.gl
        g.todo("grp/app", "issue", 7, "assigned", title="Rotate password=hunter2")
        g.note("grp/app", "issue", 7)
        self.poll()
        it = {v["key"]: v for v in self.m.inbox()["items"]}["grp/app#issue#7"]
        self.assertEqual(it["title"], "Rotate password=hunter2")
        prev = self.m.inbox_item("grp/app#issue#7")
        self.assertEqual(prev["title"], "Rotate password=hunter2")
        self.assertEqual([n["body"] for n in prev["notes"]], ["a comment token=abc"])
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#7", "work")["id"]]
        for text in (s.title, s.issue["title"], s.task):
            self.assertNotIn("hunter2", text)
        self.assertIn("password=[scrubbed]", s.task)
        s2 = sessions.Session(project="inbapp", path=self.app, state="ready", gitlab=["grp/app"])
        self.assertNotIn("hunter2", json.dumps(self.m.tool(s2, "gitlab_inbox", {})))

    def test_start_task_issue_and_seen(self):
        g = self.gl
        g.todo("grp/app", "issue", 7, "assigned", title="Colors")
        g.mrs.append(g.obj("grp/app", "mr", 5, title="Fix"))
        g.issues.append(g.obj("grp/none", "issue", 3))
        self.poll()
        self.m.inbox_unseen("grp/app#issue#7")
        r = self.m.inbox_start("grp/app#issue#7", "work", note_id=992307)
        s = self.m.sessions[r["id"]]
        self.assertEqual((s.project, s.title, s.origin), ("inbapp", "#7 Colors", "gui"))
        self.assertEqual(s.issue, {"key": "grp/app#issue#7", "gl": "grp/app", "type": "issue", "iid": 7, "title": "Colors",
                                   "web_url": "https://gitlab.com/grp/app/-/work_items/7"})
        self.assertTrue(s.task.startswith("GitLab issue #7: Colors\nhttps://gitlab.com/grp/app/-/work_items/7\n\n"))
        self.assertIn('the tool gitlab_issue (project "grp/app", iid 7)', s.task)
        self.assertIn("Handle comment note_992307 (and anything newer)", s.task)
        self.assertNotIn("including all comments", s.task)
        self.assertIn("development/gitlab-7-comment.md", s.task)
        self.assertNotIn("issue body", s.task)   # references only, never the issue text
        self.assertEqual(common.read_json(os.path.join(s.dir, "meta.json"), {})["issue"]["key"], "grp/app#issue#7")
        it = {v["key"]: v for v in self.m.inbox()["items"]}["grp/app#issue#7"]
        self.assertEqual((it["new"], it["sessions"]), (False, [s.id]))
        self.assertIn("issue", self.m.list()[-1])
        r = self.m.inbox_start("grp/app#mr#5", "analyze")
        t = self.m.sessions[r["id"]].task
        self.assertEqual(self.m.sessions[r["id"]].title, "!5 Fix")   # an MR is !5, never #5
        self.assertIn("GitLab merge request !5: Fix", t)
        self.assertIn('gitlab_mr (project "grp/app", iid 5)', t)
        self.assertIn("Analyze only", t)
        self.assertIn("development/gitlab-mr-5-analysis.md", t)
        self.assertEqual(self.m.sessions[self.m.inbox_start("grp/app#issue#7", task="my own task")["id"]].task,
                         "my own task")
        with self.assertRaises(ValueError):
            self.m.inbox_start("grp/none#issue#3")   # not mapped
        with self.assertRaises(ValueError):
            self.m.inbox_start("grp/app#issue#7", project="team-x/inbdisc")   # that project's scope lacks grp/app
        with self.assertRaises(ValueError):
            self.m.inbox_start("grp/app#issue#7", mode="implement")
        with self.assertRaises(KeyError):
            self.m.inbox_start("grp/app#issue#70")
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#7", opts={"docker": True, "unrestricted": False})["id"]]
        self.assertEqual(s.opts, {"docker": True})   # the dialog's start options reach the new session
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#7", agent="codex")["id"]]
        self.assertEqual(s.agent, "codex")   # Edit task... -> Agent: Codex
        with self.assertRaises(ValueError):
            self.m.inbox_start("grp/app#issue#7", agent="gemini")

    def test_target_note_prefers_a_mention_then_new_then_the_latest_by_others(self):
        """W9: which comment a session from the inbox answers."""
        base = {"todos": [], "first_new_note": None, "last_note": None}
        self.assertIsNone(inbox.target_note(base))
        self.assertEqual(inbox.target_note(dict(base, last_note=40)), 40)
        self.assertEqual(inbox.target_note(dict(base, last_note=40, first_new_note=30)), 30)
        todos = [{"action": "mentioned", "note_id": 25}, {"action": "directly_addressed", "note_id": 27},
                 {"action": "assigned", "note_id": None}]
        self.assertEqual(inbox.target_note(dict(base, last_note=40, first_new_note=30, todos=todos)), 27)

    def test_a_discussed_issue_starts_on_its_latest_comment_not_the_whole_issue(self):
        """W9: comments seen long ago (nothing new) - the task still targets the latest one by others,
        never my own reply and never "read everything"."""
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 21))
        n1 = g.note("grp/app", "issue", 21)
        g.note("grp/app", "issue", 21, user="alice")   # my own reply is newer: no task
        items = self.poll()   # first sight: the notes belong to the item, none of them is new
        it = items["grp/app#issue#21"]
        self.assertEqual((it["new_notes"], it["last_note"], it["target_note"]), (0, n1["id"], n1["id"]))
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#21", "work")["id"]]
        self.assertIn("Handle comment note_%d (and anything newer)" % n1["id"], s.task)
        self.assertIn("context only", s.task)
        g.issues.append(g.obj("grp/app", "issue", 22))
        self.poll()
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#22", "work")["id"]]
        self.assertIn("including all comments", s.task)   # no discussion: the whole issue it is

    def test_an_ended_linked_session_is_resumed_with_the_new_task(self):
        """W8: the session that worked on the issue goes on (continue_session = resume + /compact + task)."""
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 23))
        self.poll()
        sid = self.m.inbox_start("grp/app#issue#23")["id"]
        n = g.note("grp/app", "issue", 23)
        self.poll()
        s = self.m.sessions[sid]
        s.state = "exited"
        calls = []
        with mock.patch.object(self.m, "continue_session", lambda i, t: calls.append((i, t)) or {"session": i}):
            self.assertEqual(self.m.inbox_start("grp/app#issue#23", session=sid), {"session": sid})
            self.assertEqual(calls[0][0], sid)
            self.assertIn("Handle comment note_%d" % n["id"], calls[0][1])
            self.assertIn('gitlab_issue (project "grp/app", iid 23)', calls[0][1])
            self.m.inbox_start("grp/app#issue#23", session=sid, task="my next step")
            self.assertTrue(calls[1][1].startswith("my next step"))
            with self.assertRaises(ValueError):
                self.m.inbox_start("grp/app#issue#23", session=sid, task=5)
        it = self.poll()["grp/app#issue#23"]
        self.assertEqual((it["new"], it["new_notes"]), (False, 0))   # handled + seen
        self.assertEqual(self.audit_events("gui-inbox-resume")[-1]["session"], sid)

    def test_mention_key_finds_the_issue_a_prompt_names(self):
        """W10: URL > group/project#N > a bare #N (only with one GitLab project in scope)."""
        one, two = ["grp/app"], ["grp/app", "grp/lib"]
        mk = inbox.mention_key
        self.assertEqual(mk("see https://gitlab.com/grp/app/-/work_items/12#note_5 and #3", one), "grp/app#issue#12")
        self.assertEqual(mk("https://gitlab.com/grp/app/-/merge_requests/4", one), "grp/app#mr#4")
        self.assertIsNone(mk("https://gitlab.com/grp/other/-/issues/9", ["grp/app"]))   # out of scope
        self.assertEqual(mk("fix grp/lib#8 then #2", two), "grp/lib#issue#8")
        self.assertEqual(mk("review grp/lib!8", two), "grp/lib#mr#8")
        self.assertIsNone(mk("fix #24", two))   # a bare #24 is ambiguous with two projects
        self.assertEqual(mk("fix #24 please", one), "grp/app#issue#24")
        self.assertEqual(mk("(#24)", one), "grp/app#issue#24")
        self.assertEqual(mk("grp/app#2", one), "grp/app#issue#2")   # a full reference: any number
        for text in ("item#24", "color #2a2a2a", "#24-b", "a#24", "grp/other#5 only", "", "#0", "#1 priority", "step #2"):
            self.assertIsNone(mk(text, one), text)

    def test_an_issue_named_in_a_human_prompt_links_the_session(self):
        """W10: from the inbox right away, else after GitLab confirms it; never over an existing link,
        never from piperd's own typing."""
        import sessions
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 31, title="Inbox one"))
        g.issues.append(g.obj("grp/app", "issue", 32, title="Not listed"))
        self.poll()
        g.issues.pop()   # 32 exists in GitLab but is not in the inbox
        s = sessions.Session(project="inbapp", path=self.app, state="working", gitlab=["grp/app"])
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        self.m.issue_from_prompt(s, "pls look at #31")
        self.assertEqual((s.issue["key"], s.issue["title"]), ("grp/app#issue#31", "Inbox one"))
        self.m.issue_from_prompt(s, "now #32")
        self.assertEqual(s.issue["key"], "grp/app#issue#31")   # the first link stays
        s.issue = None
        self.m.issue_from_prompt(s, "now #32")
        self.assertTrue(self._wait(lambda: s.issue is not None))
        self.assertEqual((s.issue["key"], s.issue["title"], s.issue["type"]), ("grp/app#issue#32", "Not listed", "issue"))
        self.assertEqual(common.read_json(os.path.join(s.dir, "meta.json"), {})["issue"]["key"], "grp/app#issue#32")
        s.issue = None
        before = len(g.calls)
        self.m.issue_from_prompt(s, "and #404")
        self.m.issue_from_prompt(s, "and #404")   # probed once only
        self._wait(lambda: len(g.calls) > before)
        time.sleep(0.2)
        self.assertIsNone(s.issue)
        self.assertEqual(len([c for c in g.calls[before:] if c[1].endswith("/issues/404")]), 1)
        # through the hook file: only a human prompt counts
        os.makedirs(s.cfg, exist_ok=True)
        ev = os.path.join(s.cfg, "piper-events.jsonl")
        with open(ev, "w") as f:
            f.write(json.dumps({"ev": "SessionStart", "source": "startup"}) + "\n")
        self.m._events(s)
        self.m._events(s)
        with mock.patch.object(self.m, "_note_submit", lambda sess, e: False):   # piperd typed it
            with open(ev, "a") as f:
                f.write(json.dumps({"ev": "UserPromptSubmit", "prompt": "fix #31"}) + "\n")
            self.m._events(s)
        self.assertIsNone(s.issue)
        with mock.patch.object(self.m, "_note_submit", lambda sess, e: True):
            with open(ev, "a") as f:
                f.write(json.dumps({"ev": "UserPromptSubmit", "prompt": "fix #31"}) + "\n")
            self.m._events(s)
        self.assertEqual(s.issue["key"], "grp/app#issue#31")

    def _reported_project_sessions(self, suffix):
        """Real main + linked worktree and an unrelated checkout, using only sandbox fixtures."""
        nw_gl, acme_gl = "northwind/portal", "acme/cms"
        nw = checkout("issue-nw-" + suffix, nw_gl)
        acme = checkout("issue-acme-" + suffix, acme_gl)
        for args in (("add", "README.md"),
                     ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")):
            subprocess.run(["git", "-C", nw, *args], check=True, env=GENV)
        wt = os.path.join(nw, ".claude", "worktrees", "chatbot")
        subprocess.run(["git", "-C", nw, "worktree", "add", "-qb", "chatbot", wt], check=True, env=GENV)
        base, other = "report-nw-" + suffix, "report-acme-" + suffix
        _env.register(base, nw, gitlab=[nw_gl])
        _env.register(other, acme, gitlab=[acme_gl])
        main = self.m.create(base)
        chatbot = self.m.create(base + "@chatbot")
        unrelated = self.m.create(other)
        for s in (main, chatbot, unrelated):
            s.state = "working"
            self.addCleanup(setattr, s, "hidden", True)
            self.addCleanup(setattr, s, "state", "exited")
        return main, chatbot, unrelated

    def test_reported_worktree_issue_does_not_leak_through_hooks_state_or_board(self):
        """Northwind main + chatbot worktree + acme: only the originating session gets issue 831."""
        main, chatbot, acme = self._reported_project_sessions("cached")
        self.gl.issues.append(self.gl.obj(chatbot.gitlab[0], "issue", 831, title="Chatbot fixture"))
        self.poll()
        prompts = [(chatbot, "https://gitlab.example.com/northwind/portal/-/work_items/831"),
                   (acme, "suggest a commit message"), (main, "continue")]
        with mock.patch.object(self.m, "_note_submit", return_value=True), \
                mock.patch.object(self.m, "_turn_event"), \
                mock.patch.object(self.m, "ts_prompt_event"):
            for s, prompt in prompts:
                os.makedirs(s.box, exist_ok=True)
                event_file = os.path.join(s.box, s.names["events"])
                with open(event_file, "w") as f:
                    f.write(json.dumps({"ev": "SessionStart", "source": "startup"}) + "\n")
                self.m._events(s)
                self.m._events(s)   # replay caught up: subsequent human submissions are live
                with open(event_file, "a") as f:
                    f.write(json.dumps({"ev": "UserPromptSubmit", "prompt": prompt, "ts": time.time()}) + "\n")
                self.m._events(s)
        self.assertEqual(chatbot.issue["key"], "northwind/portal#issue#831")
        self.assertIsNone(main.issue)
        self.assertIsNone(acme.issue)
        for rows in (self.m.list(), self.m.board()):
            by_id = {r["id"]: r for r in rows}
            self.assertEqual(by_id[chatbot.id]["issue"]["gl"], chatbot.gitlab[0])
            self.assertIsNone(by_id[acme.id]["issue"])
            self.assertIsNone(by_id[main.id]["issue"])
        linked = self.m._ib_linked()
        self.assertEqual([s.id for s in linked[chatbot.issue["key"]]], [chatbot.id])
        for s in (main, acme):
            self.assertIsNone(common.read_json(os.path.join(s.dir, "meta.json"), {})["issue"])
        # Even a full Northwind URL in an unrelated acme prompt cannot attach the Northwind issue.
        self.m.issue_from_prompt(acme, prompts[0][1])
        self.assertIsNone(acme.issue)

    def test_same_issue_number_is_scoped_per_project_in_shared_inbox(self):
        _, chatbot, acme = self._reported_project_sessions("same-number")
        for s in (chatbot, acme):
            self.gl.issues.append(self.gl.obj(s.gitlab[0], "issue", 831, title=s.project))
        self.poll()
        for s in (chatbot, acme):
            self.m.issue_from_prompt(s, "fix #831")
        self.assertEqual(chatbot.issue["key"], "northwind/portal#issue#831")
        self.assertEqual(acme.issue["key"], "acme/cms#issue#831")
        self.assertNotEqual(chatbot.issue["web_url"], acme.issue["web_url"])

    def test_foreign_issue_in_read_scope_does_not_become_the_session_issue(self):
        """Read permissions for Northwind do not turn a contextual URL into acme's issue."""
        _, chatbot, acme = self._reported_project_sessions("broad-scope")
        self.gl.issues.append(self.gl.obj(chatbot.gitlab[0], "issue", 831, title="Chatbot fixture"))
        self.poll()
        acme.gitlab.append(chatbot.gitlab[0])
        self.m.issue_from_prompt(acme, "Context: https://gitlab.example.com/northwind/portal/-/work_items/831")
        self.m.issue_from_prompt(acme, "suggest a commit message")
        self.assertTrue(acme.project.startswith("report-acme-"))
        self.assertIsNone(acme.issue)
        row = next(r for r in self.m.board() if r["id"] == acme.id)
        self.assertIsNone(row["issue"])
        self.assertFalse([e for e in self.audit_events("issue-linked") if e.get("session") == acme.id])
        # A bare number still names the checkout's own issue despite the extra read scope.
        self.gl.issues.append(self.gl.obj("acme/cms", "issue", 831, title="Acme fixture"))
        self.poll()
        self.m.issue_from_prompt(acme, "look at #831")
        self.assertEqual(acme.issue["key"], "acme/cms#issue#831")

    def test_old_foreign_prompt_link_is_repaired_but_explicit_cross_project_link_stays(self):
        _, chatbot, acme = self._reported_project_sessions("repair-old")
        it = inbox.session_issue({"key": "northwind/portal#issue#831", "gl": "northwind/portal",
                                  "type": "issue", "iid": 831, "title": "Chatbot", "web_url": None})
        acme.issue = dict(it)
        chatbot.issue = dict(it)
        # An explicit picker link must survive even if it targets a different repository.
        explicit = self.m.create(acme.project)
        self.addCleanup(setattr, explicit, "hidden", True)
        self.addCleanup(setattr, explicit, "state", "exited")
        explicit.issue = dict(it)
        # Partial or malformed legacy audit records must not prevent daemon startup.
        with open(common.AUDIT_FILE, "a") as f:
            f.write('{broken\n' + json.dumps({"event": "issue-linked", "session": []}) + '\n')
        common.audit({"event": "issue-linked", "session": acme.id, "key": it["key"], "via": "prompt"})
        self.m._repair_foreign_prompt_links()
        self.assertIsNone(acme.issue)
        self.assertIsNone(common.read_json(os.path.join(acme.dir, "meta.json"), {})["issue"])
        self.assertEqual(chatbot.issue, it)
        self.assertEqual(explicit.issue, it)
        # Legacy task adoption must not restore the same foreign link from a URL.
        acme.gitlab.append("northwind/portal")
        acme.task = "https://gitlab.example.com/northwind/portal/-/work_items/831"
        self.m._ib_items[it["key"]] = it
        self.m._ib_linked()
        self.assertIsNone(acme.issue)
        self.assertTrue([e for e in self.audit_events("issue-unlinked") if e.get("session") == acme.id])

    def test_concurrent_issue_probes_keep_their_session_when_replies_arrive_reversed(self):
        _, chatbot, acme = self._reported_project_sessions("concurrent")
        sessions = (chatbot, acme)
        entered = {s.gitlab[0]: threading.Event() for s in sessions}
        release = {g: threading.Event() for g in entered}
        for s in sessions:
            self.gl.issues.append(self.gl.obj(s.gitlab[0], "issue", 831, title=s.project))
        def delayed(req, timeout=None):
            path = urllib.parse.unquote(urllib.parse.urlsplit(req.full_url).path)
            for gl in entered:
                if path.endswith("/projects/%s/issues/831" % gl):
                    entered[gl].set()
                    if not release[gl].wait(5):
                        raise OSError("fixture response not released")
            return self.gl(req, timeout)
        self.m.gitlab_opener = delayed
        try:
            for s in sessions:
                self.m.issue_from_prompt(s, "fix #831")
            for ready in entered.values():
                self.assertTrue(ready.wait(5))
            release[acme.gitlab[0]].set()
            self.assertTrue(self._wait(lambda: acme.issue is not None))
            self.assertIsNone(chatbot.issue)
            release[chatbot.gitlab[0]].set()
            self.assertTrue(self._wait(lambda: chatbot.issue is not None))
            for s in sessions:
                self.assertEqual(s.issue["gl"], s.gitlab[0])
                self.assertEqual(s.issue["key"], s.gitlab[0] + "#issue#831")
        finally:
            for ready in release.values():
                ready.set()

    def _wait(self, cond, timeout=5.0):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    def test_follow_up_to_live_session(self):
        import sessions
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        sid = self.m.inbox_start("grp/app#issue#7")["id"]
        n = g.note("grp/app", "issue", 7)
        self.poll()
        s = self.m.sessions[sid]
        s.state, s.idle, s.pid = "attention", False, s.pid or 4242   # a permission dialog: Enter would answer it
        typed = []
        with mock.patch.object(self.m, "type_text", lambda sess, text: typed.append((sess.id, text))):
            with self.assertRaises(ValueError):
                self.m.inbox_start("grp/app#issue#7", session=sid)
            s.state, s.idle = "waiting", True   # at its prompt after a turn
            with mock.patch.object(self.m, "input_pending", lambda sess: 4):   # a half-written prompt: refused
                with self.assertRaises(ValueError):
                    self.m.inbox_start("grp/app#issue#7", session=sid)
            self.assertEqual(typed, [])
            with self.assertRaises(ValueError):   # the running sandbox cannot get them any more
                self.m.inbox_start("grp/app#issue#7", session=sid, opts={"docker": True})
            with self.assertRaises(ValueError):   # nor another agent
                self.m.inbox_start("grp/app#issue#7", session=sid, agent="codex")
            self.assertEqual(typed, [])
            self.assertEqual(self.m.inbox_start("grp/app#issue#7", session=sid), {"sent": True})
            self.assertEqual(typed[0][0], sid)
            self.assertIn("#7 has a new comment note_%d" % n["id"], typed[0][1])
            self.assertIn('gitlab_issue (project "grp/app", iid 7)', typed[0][1])
            s.state = "working"
            with self.assertRaises(ValueError):
                self.m.inbox_start("grp/app#issue#7", session=sid)
            other = sessions.Session(project="inbapp", path=self.app, state="waiting")
            self.m.sessions[other.id] = other
            self.addCleanup(setattr, other, "hidden", True)
            with self.assertRaises(ValueError):
                self.m.inbox_start("grp/app#issue#7", session=other.id)   # not linked
        self.assertEqual(len(typed), 1)

    def test_follow_up_disarms_the_automation_for_that_note(self):
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 15))
        self.poll()
        sid = self.m.inbox_start("grp/app#issue#15")["id"]
        self._rule(on=("new_note",))
        g.note("grp/app", "issue", 15)
        self.poll()   # a live session: the note waits
        s = self.m.sessions[sid]
        s.state, s.idle, s.pid = "waiting", True, s.pid or 4242   # at its prompt after a turn
        with mock.patch.object(self.m, "type_text", lambda sess, text: None):
            self.m.inbox_start("grp/app#issue#15", session=sid)
        s.state = "exited"
        self.poll()
        self.assertEqual(self._auto_sessions(), [])

    def test_comment(self):
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        r = self.m.inbox_comment("grp/app#issue#7", "Hello, done.")
        self.assertEqual(r["web_url"], "https://gitlab.com/grp/app/-/work_items/7#note_%d" % r["note_id"])
        self.assertEqual(g.calls[-1][:2], ("POST", "/projects/grp/app/issues/7/notes"))
        self.assertEqual(g.calls[-1][3], {"body": "Hello, done."})
        self.assertEqual(self.audit_events("gui-comment")[0]["key"], "grp/app#issue#7")
        self.assertEqual(self.poll()["grp/app#issue#7"]["new_notes"], 0)   # my own note
        for key, body in (("grp/app#issue#7", " "), ("grp/app#issue#7", "x" * 20001), ("grp/other#issue#1", "x")):
            with self.assertRaises(ValueError):
                self.m.inbox_comment(key, body)

    # ------------------------------------------------------------ rules + automation
    def test_rules_validation(self):
        bad = [{"id": "a", "gl": "../x", "on": ["assigned"], "enabled": True},
               {"id": "a", "gl": "*", "on": [], "enabled": True},
               {"id": "a", "gl": "*", "on": ["pushed"], "enabled": True},
               {"id": "a", "gl": "*", "on": ["assigned"], "action": "work", "enabled": True},
               {"id": "a", "gl": "*", "on": ["assigned"], "enabled": "yes"},
               {"id": "a b", "gl": "*", "on": ["assigned"], "enabled": True}]
        for r in bad:
            with self.assertRaises(ValueError):
                self.m.inbox_rules([r])
        with self.assertRaises(ValueError):
            self.m.inbox_rules([{"id": "a", "gl": "*", "on": ["assigned"]}] * 2)
        with self.assertRaises(ValueError):
            self.m.inbox_rules({"id": "a"})
        t0 = time.time()
        out = self.m.inbox_rules([{"id": "r1", "gl": "grp/app", "on": ["new_note", "assigned"], "enabled": True,
                                   "since": 1.0}])["rules"]
        self.assertEqual(out[0]["on"], ["assigned", "new_note"])
        self.assertGreaterEqual(out[0]["since"], t0)   # a past `since` from the client is ignored
        again = self.m.inbox_rules([dict(out[0], since=0)])["rules"]
        self.assertEqual(again[0]["since"], out[0]["since"])   # kept while it stays enabled
        self.assertEqual(common.read_json(inbox.RULES_FILE, {})["rules"], again)
        self.assertEqual(self.m.inbox()["rules"], again)

    def _rule(self, on=("mentioned", "new_note"), gl="*"):
        self.m.inbox_rules([{"id": "r", "gl": gl, "on": list(on), "enabled": True}])
        rule = self.m._ib_rules[0]
        rule["since"] -= 100   # events of the fake are "now": let them count as after `since`
        return rule

    def _auto_sessions(self):
        return [s for s in self.m.sessions.values() if s.origin == "inbox" and not s.hidden]

    def test_automation(self):
        g = self.gl
        self._rule()
        g.todo("grp/app", "issue", 7, "mentioned")
        self.poll()   # baseline: never
        self.assertEqual(self._auto_sessions(), [])
        g.todo("grp/app", "issue", 8, "mentioned")
        g.todo("grp/none", "issue", 3, "mentioned")   # unmapped: never
        g.todo("grp/app", "issue", 11, "assigned")    # not in the rule's `on`
        self.poll()
        auto = self._auto_sessions()
        self.assertEqual([s.issue["key"] for s in auto], ["grp/app#issue#8"])
        self.assertIn("Analyze only", auto[0].task)
        self.assertEqual(self.audit_events("inbox-auto")[0]["session"], auto[0].id)
        it = {v["key"]: v for v in self.m.inbox()["items"]}["grp/app#issue#8"]
        self.assertEqual(it["auto"][0]["session"], auto[0].id)
        # once per event, and not while its session lives
        g.todo("grp/app", "issue", 8, "mentioned")
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 1)
        # the second to-do came while the session was live: it fires once that session is gone
        auto[0].state = "exited"
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 2)
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 2)

    def test_automation_new_note_and_limits(self):
        import sessions
        g = self.gl
        self._rule(on=("new_note",))
        g.issues.append(g.obj("grp/app", "issue", 7))
        g.issues.append(g.obj("grp/app", "issue", 8))
        g.issues.append(g.obj("grp/app", "issue", 9))
        self.poll()
        n = g.note("grp/app", "issue", 7)
        g.note("grp/app", "issue", 7, user="alice")   # own: not an event
        self.poll()
        auto = self._auto_sessions()
        self.assertEqual(len(auto), 1)
        self.assertIn("note_%d" % n["id"], auto[0].task)
        # per-hour cap
        with mock.patch.object(inbox, "PER_HOUR", 1):
            g.note("grp/app", "issue", 8)
            self.poll()
            self.assertEqual(len(self._auto_sessions()), 1)
            self.assertEqual(self.audit_events("inbox-auto-skipped")[0]["why"], "per-hour limit")
        # free slots: the live auto session + MAX_SESSIONS=2 leaves 1 slot
        with mock.patch.object(sessions, "MAX_SESSIONS", 2):
            g.note("grp/app", "issue", 9)
            self.poll()
            self.assertEqual(len(self._auto_sessions()), 1)
            self.assertIn("free session slots", self.audit_events("inbox-auto-skipped")[0]["why"])
        # a rule enabled after the event does not fire for it
        self.m.inbox_rules([{"id": "r2", "gl": "grp/app", "on": ["new_note"], "enabled": True}])
        g.note("grp/app", "issue", 9, created=time.time() - 3600)
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 1)
        # disabled rules never fire
        self.m.inbox_rules([{"id": "r2", "gl": "grp/app", "on": ["new_note"], "enabled": False}])
        self.m._ib_rules[0]["since"] = 0
        g.note("grp/app", "issue", 9)
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 1)

    def test_automation_skips_what_the_human_handled(self):
        g = self.gl
        self._rule(on=("mentioned",))
        self.poll()
        g.todo("grp/app", "issue", 12, "mentioned")
        with mock.patch.object(self.m, "_inbox_automate"):   # the human is faster than the rule
            self.poll()
        s = self.m.sessions[self.m.inbox_start("grp/app#issue#12", "work")["id"]]
        s.state = "exited"
        self.poll()   # the to-do is still pending (never auto-done), but it was answered
        self.assertEqual(self._auto_sessions(), [])

    def test_automation_hour_limit_counts_sessions_and_notes_wait(self):
        g = self.gl
        self._rule(on=("mentioned", "assigned", "new_note"))
        for iid in (13, 14):
            g.issues.append(g.obj("grp/app", "issue", iid))
        self.poll()
        g.todo("grp/app", "issue", 13, "mentioned")
        g.todo("grp/app", "issue", 13, "assigned")
        g.note("grp/app", "issue", 13)
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 1)
        self.assertEqual(self.m.inbox()["limits"]["used_hour"], 1)   # one session, three events
        with mock.patch.object(inbox, "PER_HOUR", 1):
            n = g.note("grp/app", "issue", 14)
            self.poll()
            self.assertEqual(len(self._auto_sessions()), 1)
        self.poll()   # the limit is gone: the comment skipped meanwhile still gets its analysis
        auto = [s for s in self._auto_sessions() if s.issue["iid"] == 14]
        self.assertEqual(len(auto), 1)
        self.assertIn("note_%d" % n["id"], auto[0].task)
        self.poll()
        self.assertEqual(len(self._auto_sessions()), 2)

    # ------------------------------------------------------------ draft + board flow
    def _clean_repo(self) -> str:
        """A committed checkout without untracked files (the flow counts uncommitted code)."""
        repo = tempfile.mkdtemp(dir=_env.ROOT)
        self.addCleanup(subprocess.run, ["rm", "-rf", repo])
        git = lambda *a: subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)   # noqa: E731
        git("init", "-q")
        with open(os.path.join(repo, "A.java"), "w") as f:
            f.write("a")
        git("add", "-A")
        git("-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "init")
        return os.path.realpath(repo)

    def _linked_session(self, iid=7, kind="issue"):
        import sessions
        s = sessions.Session(project="inbapp", path=self.app, state="waiting",
                             issue={"key": "grp/app#%s#%d" % (kind, iid), "gl": "grp/app", "type": kind, "iid": iid,
                                    "title": "T", "web_url": "u"})
        self.m.sessions[s.id] = s
        self.addCleanup(setattr, s, "hidden", True)
        return s

    def test_draft(self):
        import sessions
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        dev = os.path.join(self.app, "development")
        os.makedirs(dev, exist_ok=True)
        self.addCleanup(subprocess.run, ["rm", "-rf", dev])
        s = self._linked_session()
        self.assertEqual(self.m.session_draft(s.id), {"path": None})
        with open(os.path.join(dev, "secret-7-comment.md"), "w") as f:   # hidden by the project's excludes
            f.write("TOPSECRET")
        with open(os.path.join(dev, "draft-77-comment.md"), "w") as f:   # another iid
            f.write("x")
        with open(os.path.join(self.app, "gitlab-7-comment.md"), "w") as f:   # outside development/
            f.write("x")
        self.addCleanup(os.unlink, os.path.join(self.app, "gitlab-7-comment.md"))
        self.assertEqual(self.m.session_draft(s.id), {"path": None})
        with open(os.path.join(dev, "draft-7-comment.md"), "w") as f:
            f.write("Hello,\nfallback")
        self.assertEqual(self.m.session_draft(s.id)["path"], "development/draft-7-comment.md")
        with open(os.path.join(dev, "gitlab-7-comment.md"), "w") as f:
            f.write("Hello,\ndone.")
        d = self.m.session_draft(s.id)
        self.assertEqual((d["path"], d["text"], d["key"], d["sent"]),
                         ("development/gitlab-7-comment.md", "Hello,\ndone.", "grp/app#issue#7", False))
        time.sleep(0.01)
        self.m.inbox_comment("grp/app#issue#7", d["text"])
        self.assertTrue(self.m.session_draft(s.id)["sent"])
        # a symlink (to a hidden file) is never followed
        os.unlink(os.path.join(dev, "gitlab-7-comment.md"))
        os.unlink(os.path.join(dev, "draft-7-comment.md"))
        os.symlink(os.path.join(dev, "secret-7-comment.md"), os.path.join(dev, "gitlab-7-comment.md"))
        self.assertEqual(self.m.session_draft(s.id), {"path": None})
        # nor a symlinked development/ dir
        subprocess.run(["rm", "-rf", dev], check=True)
        other = os.path.join(_env.ROOT, "elsewhere")
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "gitlab-7-comment.md"), "w") as f:
            f.write("outside")
        os.symlink(other, dev)
        self.assertEqual(self.m.session_draft(s.id), {"path": None})
        os.unlink(dev)
        os.makedirs(dev)
        with open(os.path.join(dev, "gitlab-7-comment.md"), "w") as f:
            f.write("x" * (65 << 10))
        with self.assertRaises(ValueError):
            self.m.session_draft(s.id)
        s2 = sessions.Session(project="inbapp", path=self.app, state="waiting")   # not linked
        self.m.sessions[s2.id] = s2
        self.addCleanup(setattr, s2, "hidden", True)
        self.assertEqual(self.m.session_draft(s2.id), {"path": None})

    def test_issue_tracker_uses_explicit_and_cached_trusted_origins_without_git(self):
        p = {"key": "demo", "path": "/fixture/checkout", "gitlab": [], "github": []}
        with mock.patch("features.on", return_value=True), \
                mock.patch.object(self.m, "origin_gl") as gl, mock.patch.object(self.m, "origin_gh") as gh:
            self.m._ib_origin.clear()
            self.assertIsNone(self.m.issue_tracker(p))
            self.assertTrue(self.m.issue_tracker(dict(p, github=["owner/repo"])))
            self.m._ib_origin[p["path"]] = (0, "group/repo")
            self.assertTrue(self.m.issue_tracker(p))
            self.m._ib_origin[p["path"]] = (0, None)
            self.assertIsNone(self.m.issue_tracker(p))
            self.m._ib_origin["gh:" + p["path"]] = (0, "gh:owner/repo")
            self.assertTrue(self.m.issue_tracker(p))
            self.m._ib_origin["gh:" + p["path"]] = (0, None)
            self.assertFalse(self.m.issue_tracker(p))
            gl.assert_not_called()
            gh.assert_not_called()
        with mock.patch("features.on", return_value=False):
            self.assertFalse(self.m.issue_tracker(dict(p, gitlab=["group/repo"])))

    def test_workflow_can_be_disabled_without_losing_issue_link(self):
        s = self._linked_session()
        with mock.patch("features.on", side_effect=lambda module: module != "workflow"), \
                mock.patch.object(self.m, "_flow_tools") as tools, mock.patch.object(self.m, "_flow_gitpart") as git:
            self.assertIsNone(self.m.flow(s))
            tools.assert_not_called()
            git.assert_not_called()
            card = {c["id"]: c for c in self.m.board()}[s.id]
            self.assertIsNone(card["flow"])
            self.assertEqual(card["issue"]["iid"], 7)

    def test_board_flow(self):
        s = self._linked_session()
        s.path = repo = self._clean_repo()
        s.claude_session = "0f1e2d3c-aaaa-bbbb"
        tdir = os.path.join(s.cfg, "projects", "-x")
        os.makedirs(tdir, exist_ok=True)

        def use(name, **inp):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
        recs = [use("Write", file_path=repo + "/development/gitlab-7-analysis.md", content="a"),
                use("Write", file_path=repo + "/development/gitlab-7-comment.md", content="k"),
                use("Write", file_path="/tmp/scratch/x.java", content="x"),
                use("Skill", skill="verify"), use("Agent", subagent_type="independent-reviewer", prompt="p")]
        with open(os.path.join(tdir, s.claude_session + ".jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        card = {c["id"]: c for c in self.m.board()}[s.id]
        self.assertEqual(card["issue"]["key"], "grp/app#issue#7")
        fl = card["flow"]
        self.assertEqual((fl["analysis"], fl["impl"], fl["verify"]), (True, False, 1))   # the reviewer is /verify's own
        self.assertEqual((fl["commits"], fl["ahead"], fl["pushed"], fl["draft"]), (0, None, False, None))
        with open(os.path.join(tdir, s.claude_session + ".jsonl"), "a") as f:
            f.write(json.dumps(use("Edit", file_path=repo + "/src/A.java", old_string="a", new_string="b")) + "\n")
        os.utime(os.path.join(tdir, s.claude_session + ".jsonl"), (time.time() + 5, time.time() + 5))
        self.assertTrue({c["id"]: c for c in self.m.board()}[s.id]["flow"]["impl"])

    def test_legacy_session_linked_by_task_url(self):
        """A session started from the issue picker before 9.8 (no Session.issue) is linked by the
        issue URL in its task, within its own GitLab scope only, and adopts the link."""
        import sessions
        g = self.gl
        g.issues.append(g.obj("grp/app", "issue", 7))
        self.poll()
        url = "https://gitlab.example/grp/app/-/work_items/7"
        s = sessions.Session(project="inbapp", path=self.app, state="waiting", gitlab=["grp/app"],
                             task="GitLab issue #7: T\n%s\n\nRead the task" % url)
        foreign = sessions.Session(project="inbapp", path=self.app, state="waiting", gitlab=["grp/other"],
                                   task="GitLab issue #7: T\n%s" % url)
        for x in (s, foreign):
            self.m.sessions[x.id] = x
            self.addCleanup(setattr, x, "hidden", True)
        item = {v["key"]: v for v in self.m.inbox()["items"]}["grp/app#issue#7"]
        self.assertEqual(item["sessions"], [s.id])
        self.assertEqual(s.issue["key"], "grp/app#issue#7")
        self.assertIsNone(foreign.issue)

    def test_flow_impl_from_git(self):
        """Edits made through Bash (no Edit/Write record) still count: uncommitted code changes do,
        notes in development/ do not."""
        repo = self._clean_repo()
        self.m.repo_top(repo, record="start")
        s = self._linked_session()
        s.path = repo
        os.makedirs(os.path.join(repo, "development"))
        with open(os.path.join(repo, "development", "notes.md"), "w") as f:
            f.write("n")
        self.m._flow_git.clear()
        fl = self.m.flow(s)
        self.assertEqual((fl["impl"], fl["analysis"]), (False, False))
        with open(os.path.join(repo, "A.java"), "w") as f:
            f.write("changed by sed -i")
        self.m._flow_git.clear()
        fl = self.m.flow(s)
        self.assertEqual((fl["impl"], fl["analysis"]), (True, True))
        self.assertNotIn("code_changes", fl)

    def test_flow_impl_counts_only_the_projects_subdirectory(self):
        """A project in a subdirectory of a repository: changes elsewhere in the repository are not its work."""
        repo = self._clean_repo()
        sub = os.path.join(repo, "sub")
        os.makedirs(os.path.join(sub, "development"))
        s = self._linked_session()
        s.path = sub
        self.m.repo_top(sub, record="start")
        with open(os.path.join(repo, "A.java"), "w") as f:
            f.write("changed outside the project")
        with open(os.path.join(sub, "development", "notes.md"), "w") as f:
            f.write("n")
        self.m._flow_git.clear()
        self.assertFalse(self.m.flow(s)["impl"])
        with open(os.path.join(sub, "B.java"), "w") as f:
            f.write("b")
        self.m._flow_git.clear()
        self.assertTrue(self.m.flow(s)["impl"])

    # ------------------------------------------------------------ agents + HTTP
    def test_gitlab_inbox_tool_scope(self):
        import sessions
        g = self.gl
        g.todo("grp/app", "issue", 7, "mentioned")
        g.todo("grp/disc", "issue", 9, "assigned")
        self.poll()
        g.todo("grp/app", "issue", 8, "assigned")
        self.poll()
        self.assertNotIn("gitlab_inbox", gitlab.TOOLS)   # never through gitlab.call
        s = sessions.Session(project="inbapp", path=self.app, state="ready", gitlab=["grp/app"])
        r = self.m.tool(s, "gitlab_inbox", {})
        self.assertEqual({i["key"] for i in r["items"]}, {"grp/app#issue#7", "grp/app#issue#8"})
        self.assertEqual(r["items"][0]["todos"][0], {"action": "assigned", "author": "A K",
                                                     "body": "@alice please password=[scrubbed]"})
        self.assertNotIn("id", r["items"][0]["todos"][0])
        r = self.m.tool(s, "gitlab_inbox", {"only_new": True})
        self.assertEqual([i["key"] for i in r["items"]], ["grp/app#issue#8"])
        adv = sessions.Session(kind="advisor", project="advisor", path="/x", gitlab=[])
        self.assertEqual(self.m.tool(adv, "gitlab_inbox", {})["items"], [])

    def test_mcp_lists_gitlab_inbox(self):
        with open(os.path.join(common.PIPER_DIR, "sandbox", "piper-mcp.py")) as f:
            src = f.read()
        self.assertIn('tool("gitlab_inbox"', src)

    def test_http_routes_and_state(self):
        g = self.gl
        g.todo("grp/app", "issue", 7, "mentioned")
        self.poll()
        old = piperd.MANAGER
        piperd.MANAGER = self.m
        self.addCleanup(setattr, piperd, "MANAGER", old)
        t = common.mint_token("inbox-test")
        code, _, body = get("/api/state", token=t)
        self.assertEqual(code, 200)
        st = json.loads(body)["inbox"]
        self.assertEqual((st["status"], st["total"], st["needs"], st["new"]), ("ok", 1, 1, 0))
        code, _, body = get("/api/inbox", token=t)
        ib = json.loads(body)
        self.assertEqual((code, ib["status"], ib["me"], ib["limits"]["per_hour"]), (200, "ok", "alice", inbox.PER_HOUR))
        code, _, body = get("/api/inbox/item?key=grp%2Fapp%23issue%237", token=t)
        pv = json.loads(body)
        self.assertEqual((code, pv["iid"], pv["description"], pv["key"]), (200, 7, "issue body", "grp/app#issue#7"))
        self.assertEqual(get("/api/inbox/item?key=nope", token=t)[0], 409)
        self.assertEqual(get("/api/inbox/item?key=grp%2Fx%23issue%231", token=t)[0], 404)
        self.assertEqual(post("/api/inbox/seen", {"all": True}, token=t)[0], 200)
        self.assertEqual(post("/api/inbox/seen", {"all": True})[0], 401)
        self.assertEqual(post("/api/inbox/unseen", {"key": "grp/app#issue#7"}, token=t)[0], 200)
        self.assertEqual(post("/api/inbox/rules", {"rules": [{"id": "x"}]}, token=t)[0], 409)
        self.assertEqual(post("/api/inbox/start", {"key": "grp/app#issue#7", "mode": "x"}, token=t)[0], 409)
        code, _, body = post("/api/inbox/start", {"key": "grp/app#issue#7", "mode": "analyze"}, token=t)
        self.assertEqual((code, json.loads(body)["issue"]["iid"]), (202, 7))
        code, _, body = post("/api/inbox/refresh", {}, token=t)
        self.assertEqual((code, json.loads(body)["status"]), (200, "ok"))
        code, _, body = post("/api/inbox/comment", {"key": "grp/app#issue#7", "body": "Done."}, token=t)
        self.assertEqual(code, 200)
        self.assertIn("note_id", json.loads(body))
        self.assertEqual(post("/api/inbox/done", {"key": "grp/app#issue#7"}, token=t)[0], 200)
        self.assertEqual(post("/api/projects/add", {"path": "/etc", "gl": "grp/app"}, token=t)[0], 409)
        self.assertEqual(post("/api/inbox/nope", {}, token=t)[0], 404)
        s = self._linked_session()
        code, _, body = get("/api/sessions/%s/draft" % s.id, token=t)
        self.assertEqual((code, json.loads(body)), (200, {"path": None}))
        self.assertEqual(get("/api/sessions/nope/draft", token=t)[0], 404)
        with mock.patch.object(gitlab, "token", return_value=None):
            self.assertEqual(post("/api/inbox/comment", {"key": "grp/app#issue#7", "body": "x"}, token=t)[0], 503)


if __name__ == "__main__":
    unittest.main()
