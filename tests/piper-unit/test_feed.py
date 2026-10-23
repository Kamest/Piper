"""Feed: actions derived from the state, events from snapshot
diffs, persistence, seen / dismiss / mute, the chat queue typed into an idle Global agent,
and that the feature is inert while it is off."""
import json
import os
import time
import unittest
from unittest import mock

import _env
import common
import features
import feed


def snap(**kw):
    base = {"id": "s1", "kind": "project", "project": "shop", "title": "shop", "state": "working", "detail": "",
            "since": 1000.0, "idle": False, "watch": [], "rc": None, "origin": "gui", "parent": None,
            "handoff_at": 0.0, "handoff_ok": False, "blocks": 0, "hidden": False, "issue": None}
    base.update(kw)
    return base


def item(**kw):
    base = {"key": "grp/shop#issue#7", "gl": "grp/shop", "type": "issue", "iid": 7, "title": "Price list",
            "project": "shop", "reasons": ["assigned"], "new": True, "new_notes": 0, "todos": [],
            "updated_at": "2026-09-29T10:00:00Z"}
    base.update(kw)
    return base


def derive(sessions_=(), pending=(), inbox=(), prod=None, ci=None, now=2000.0, wait_s=180):
    return feed.derive_actions(list(sessions_), list(pending), list(inbox), prod or {}, ci or {}, now, wait_s)


class ConfigTest(unittest.TestCase):
    def test_defaults_off_and_validation(self):
        self.assertEqual(feed.validate({}), {"enabled": False, "retention_days": 30})
        self.assertTrue(feed.validate({"enabled": True})["enabled"])
        self.assertEqual(feed.validate({"wait_min": 7}), {"enabled": False, "retention_days": 30})   # removed, still loads
        for bad in ({"enabled": "yes"}, {"retention_days": 0}, {"retention_days": True}, {"x": 1}):
            with self.assertRaises(ValueError):
                feed.validate(bad)

    def test_load_config_falls_back_key_by_key(self):
        feed._write(feed.CONFIG_FILE, {"enabled": "on", "wait_min": 7, "retention_days": 0})
        self.addCleanup(os.unlink, feed.CONFIG_FILE)
        self.assertEqual(feed.load_config(), {"enabled": True, "retention_days": 30})   # D10: the agent module, not the key
        _env.set_modules(self, agent=False)
        self.assertEqual(feed.load_config(), {"enabled": False, "retention_days": 30})


class DeriveTest(unittest.TestCase):
    def test_permission_prompt_vs_idle(self):
        a = derive([snap(state="attention", detail="Claude needs your permission to use Bash")])
        self.assertEqual([x["level"] for x in a], ["permission"])
        self.assertEqual([x["op"] for x in a[0]["actions"]], ["open"])   # never Yes/No: \r would pick the default
        idle = snap(state="attention", idle=True, since=1000.0)
        self.assertEqual([x["level"] for x in derive([idle], now=1100.0)], [])   # < wait_min
        self.assertEqual([x["level"] for x in derive([idle], now=1200.0)], ["waiting"])
        self.assertEqual(derive([snap(state="waiting", idle=True)], now=1200.0)[0]["actions"][0]["op"], "reply")

    def test_advisor_prodcheck_and_hidden_never_wait(self):
        rows = [snap(id="a", kind="advisor", state="waiting", idle=True), snap(id="p", kind="prodcheck", state="waiting"),
                snap(id="h", state="waiting", hidden=True)]
        self.assertEqual(derive(rows, now=99999.0), [])

    def test_l5_is_security_and_first(self):
        a = derive([snap(id="x", state="waiting", idle=True, since=0.0),
                    snap(id="y", state="attention", watch=["/p/config/secrets/new.properties"])], now=5000.0)
        self.assertEqual([x["level"] for x in a], ["security", "waiting"])
        self.assertIn("new.properties", a[0]["body"])

    def test_blocking_need_supersedes_the_same_sessions_idle_row(self):
        waiting = snap(state="attention", idle=True, watch=["new.secret"])
        self.assertEqual([x["level"] for x in derive([waiting])], ["security", "waiting"])
        permission = snap(state="attention", watch=["new.secret"], detail="Claude needs permission")
        self.assertEqual([x["level"] for x in derive([permission])], ["security", "permission"])
        waiting = snap(state="waiting", idle=True)
        pending = [{"id": "p1", "session": waiting["id"], "kind": "git_push", "title": "shop"}]
        self.assertEqual([x["level"] for x in derive([waiting], pending=pending)], ["confirm"])
        self.assertEqual([x["level"] for x in derive([waiting])], ["waiting"])

    def test_confirmations_errors_blocked_trust(self):
        a = derive([snap(id="e", state="error", detail="attestation failed: x"), snap(id="b", state="blocked", blocks=2),
                    snap(id="t", state="trust")],
                   pending=[{"id": "p1", "kind": "git_push", "session": "t", "title": "shop", "summary": "Push of branch", "created": 5}])
        self.assertEqual([x["level"] for x in a], ["permission", "confirm", "blocked", "blocked"])
        conf = a[1]
        self.assertEqual([(x["label"], x["args"]["ok"]) for x in conf["actions"]], [("Allow", True), ("Deny", False)])
        self.assertIn("push", conf["title"])
        self.assertEqual([x.get("body") for x in a if x["key"].startswith("blocked:")], ["2 findings"])
        one = derive([snap(id="b", state="blocked", blocks=1)])
        self.assertEqual(one[0].get("body"), "1 finding")   # not "1 findings"
        db = derive(pending=[{"id": "p2", "kind": "db_write", "session": "t", "title": "shop", "summary": "UPDATE", "created": 5}])
        self.assertEqual(db[0]["actions"][-1]["label"], "Deny")   # W13: one word, the DB card too (S3 Decline gone)

    def test_every_client_gets_the_red_allow_of_a_dangerous_card(self):
        # B-N3-1: a job on a protected branch, a file the host runs and a settings change that weakens the
        # isolation are red on the floating card - the queue row, the Feed drawer and Mattermost read args.danger
        def allow(kind, payload=None):
            p = {"id": "p", "kind": kind, "session": "t", "title": "shop", "summary": "x", "created": 5}
            if payload is not None:
                p["payload"] = payload
            return derive(pending=[p])[0]["actions"][0]["args"].get("danger")
        for kind in ("gitlab_job_protected", "write_exec", "http_prod", "http_host", "db_write_prod", "db_read_prod",
                     "db_unmask"):
            self.assertEqual(allow(kind), kind, kind)
        self.assertEqual(allow("settings_change", {"section": "sessions", "keys": ["claude_dir"], "weakens": True}),
                         "settings_change")
        self.assertIsNone(allow("settings_change", {"section": "sessions", "keys": ["x"], "weakens": False}))
        self.assertIsNone(allow("settings_change"))
        self.assertIsNone(allow("gitlab_job_play"))
        self.assertIsNone(allow("git_push"))

        # N7 (B-N6-1): a git_run that takes / moves a hidden file (sessions marks payload.danger) - both kinds
        for kind in ("git_run", "git_run_project"):
            self.assertEqual(allow(kind, {"args": ["commit", "-am", "x"], "warnings": [], "danger": True}), kind)
            self.assertIsNone(allow(kind, {"args": ["status"], "warnings": []}))
            self.assertIsNone(allow(kind, {"args": ["status"], "danger": "yes"}))   # only a real true


    def test_session_grant_is_a_separate_feed_action_and_keeps_production_danger(self):
        for kind, danger in (("http_prod", "http_prod"), ("db_write_prod", "db_write_prod"), ("http_test", None)):
            p = {"id": "p", "kind": kind, "session": "s", "title": "shop", "created": 5,
                 "always": "Allow this content", "session_always": "Allow for this session on PROD"}
            actions = derive(pending=[p])[0]["actions"]
            grant = next(a for a in actions if a["args"].get("session_always"))
            self.assertTrue(grant["args"]["ok"])
            self.assertNotIn("always", grant["args"])
            self.assertEqual(grant["args"].get("danger"), danger)
            self.assertEqual(grant["label"], p["session_always"])

    def test_every_fixed_card_kind_has_words_for_its_title(self):
        # B-N3-5: "shop: allow_domain" - a literal kind passed to ask() needs KIND_TEXT (feed.test.mjs keeps the GUI copy equal)
        import glob
        import re
        root = os.path.join(os.path.dirname(os.path.abspath(feed.__file__)))
        kinds = set()
        for f in glob.glob(os.path.join(root, "*.py")):
            with open(f, encoding="utf-8") as fh:
                kinds |= set(re.findall(r'\bask\(\s*"([a-z_]+)"', fh.read()))
        self.assertGreaterEqual(kinds, {"allow_domain", "settings_change", "write_exec"})
        self.assertEqual(sorted(k for k in kinds if k not in feed.KIND_TEXT), [])
        a = derive(pending=[{"id": "p", "kind": "allow_domain", "session": "t", "title": "shop", "summary": "x", "created": 5}])
        self.assertNotIn("allow_domain", a[0]["title"])

    def test_gitlab_only_new_and_addressed(self):
        todo = {"author": "Jan", "body": "Please @me review this", "id": 1, "action": "mentioned", "created_at": "", "note_id": 3}
        a = derive(inbox=[item(reasons=["mentioned"], todos=[todo]), item(key="k2", new=False),
                          item(key="k3", reasons=["authored"])])
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["quote"], {"author": "Jan", "text": "Please @me review this"})   # a quote, never the agent
        self.assertEqual(a[0]["actions"][0], {"op": "inbox", "label": "In the Inbox", "args": {"key": "grp/shop#issue#7"}})

    def test_gitlab_text_is_raw_for_the_human(self):
        """L1 (2026-10-08): the queue / Feed rows show GitLab text as GitLab has it - they reach only the
        GUI token's /api/state; Mattermost scrubs on its own."""
        todo = {"author": "Jan", "body": "token=abc123 please", "id": 1, "action": "mentioned", "created_at": "", "note_id": 3}
        a = derive(inbox=[item(reasons=["mentioned"], todos=[todo], title="rotate password=hunter2")])
        self.assertEqual(a[0]["quote"]["text"], "token=abc123 please")
        self.assertIn("password=hunter2", a[0]["title"])

    def test_ci_and_prod(self):
        a = derive([snap(state="working")], ci={"shop": {"project": "shop", "branch": "main", "id": 9, "status": "failed",
                                                         "url": "https://gl/p/9", "at": 5}},
                   prod={"new": 2})
        self.assertEqual([x["level"] for x in a], ["ci", "prod"])
        self.assertEqual([x["op"] for x in a[0]["actions"]], ["open", "url"])
        self.assertEqual(derive(ci={"shop": {"project": "shop", "status": "success"}}), [])

    def test_the_production_row_is_as_old_as_its_report(self):
        # B-N7-1: prod_summary sends report_at - the row read prod["at"] and was always as old as midnight
        a = derive(prod={"new": 2, "report_at": 1950.5}, now=2000.0)
        self.assertEqual(a[0]["since"], 1950.5)


class DiffTest(unittest.TestCase):
    def cur(self, sessions_=(), inbox=(), ci=None, actions=None):
        return {"sessions": {s["id"]: s for s in sessions_}, "inbox": {i["key"]: i for i in inbox},
                "ci": ci or {}, "actions": actions or {}}

    def test_baseline_is_silent(self):
        self.assertEqual(feed.diff_events(None, self.cur([snap()]), 1.0), [])

    def test_session_lifecycle(self):
        a = self.cur([snap(state="working")])
        b = self.cur([snap(state="waiting", idle=True), snap(id="n", title="web", origin="advisor")])
        t = [e["title"] for e in feed.diff_events(a, b, 2.0)]
        self.assertEqual(t, ["shop finished a turn", "Global agent opened session web"])
        c = self.cur([snap(state="exited", rc=143)])
        self.assertEqual([e["title"] for e in feed.diff_events(b, c, 3.0)], ["shop was stopped"])
        self.assertEqual(feed.exit_text(snap(state="exited", rc=0)), "shop ended")

    def test_advisor_turns_are_not_events(self):
        a = self.cur([snap(id="a", kind="advisor", state="working")])
        b = self.cur([snap(id="a", kind="advisor", state="waiting", idle=True)])
        self.assertEqual(feed.diff_events(a, b, 1.0), [])

    def test_handoff_notes_ci_and_resolved(self):
        parent = snap(id="p", title="lib")
        a = self.cur([parent, snap(id="c", parent="p")], [item(new_notes=1)],
                     ci={"shop": {"project": "shop", "status": "failed", "branch": "m", "id": 1}},
                     actions={"p:1": {"key": "p:1", "level": "confirm", "source": "confirm", "title": "shop: wants to push",
                                      "subject": {}},
                              "wait:x:1": {"key": "wait:x:1", "level": "waiting", "source": "session", "title": "x",
                                           "subject": {}}})
        b = self.cur([parent, snap(id="c", parent="p", handoff_at=5.0, handoff_ok=True)], [item(new_notes=3)],
                     ci={"shop": {"project": "shop", "status": "success", "branch": "m", "id": 2}})
        t = [e["title"] for e in feed.diff_events(a, b, 9.0)]
        self.assertEqual(t, ["shop returned a result for lib", "Price list: 3 new comments", "CI shop passes again",
                             "resolved: shop: wants to push"])   # a resolved "waiting" is no news

    def test_an_old_comment_marked_unread_again_is_no_news(self):
        # F-N10-8: the count grows back after "Mark as unread", no note id above what was known: not news
        a = self.cur(inbox=[item(new_notes=0, new_note_ids=[], last_note=12)])
        b = self.cur(inbox=[item(new_notes=1, new_note_ids=[12], last_note=12)])
        self.assertEqual(feed.diff_events(a, b, 2.0), [])
        c = self.cur(inbox=[item(new_notes=2, new_note_ids=[12, 15], last_note=15)])
        self.assertEqual([e["title"] for e in feed.diff_events(b, c, 3.0)], ["Price list: 2 new comments"])
        # read meanwhile, then a new one: news although the count fell from 2 to 1
        d = self.cur(inbox=[item(new_notes=1, new_note_ids=[17], last_note=17)])
        self.assertEqual([e["title"] for e in feed.diff_events(c, d, 4.0)], ["Price list: 1 new comment"])
        # my own comment raises last_note only: no news even with an old unread one
        e = self.cur(inbox=[item(new_notes=1, new_note_ids=[17], last_note=20)])
        self.assertEqual(feed.diff_events(d, e, 5.0), [])

    def test_coalesce_same_key_within_window(self):
        evs = [{"key": "turn:s1", "ts": 100.0, "title": "a", "n": 1}]
        self.assertIs(feed.coalesce(evs, {"key": "turn:s1", "ts": 200.0, "title": "b"}), evs[0])
        self.assertEqual((evs[0]["n"], evs[0]["ts"]), (2, 200.0))
        self.assertIsNone(feed.coalesce(evs, {"key": "turn:s1", "ts": 200.0 + feed.COALESCE_S + 1, "title": "c"}))
        self.assertIsNone(feed.coalesce(evs, {"key": "other", "ts": 201.0, "title": "d"}))


class ChatTextTest(unittest.TestCase):
    def test_wrap_and_parse(self):
        t = feed.chat_wrap(["m3", "m4"], ["first", "second"])
        self.assertTrue(t.startswith("[chat m3,m4 – "))
        self.assertEqual(feed.chat_turn_ids(t), ["m3", "m4"])
        self.assertEqual(feed.CHAT_RX.sub("", t, count=1), "first\n\n---\n\nsecond")
        self.assertEqual(feed.chat_turn_ids("[chat] nothing"), [])
        self.assertEqual(feed.chat_turn_ids("typed in the terminal"), [])

    def test_mute(self):
        muted = {"s1": 500.0, "src:gitlab": 500.0}
        self.assertTrue(feed.muted_by({"subject": {"key": "s1"}, "source": "session"}, muted, 100.0))
        self.assertTrue(feed.muted_by({"subject": {"key": "k"}, "source": "gitlab"}, muted, 100.0))
        self.assertFalse(feed.muted_by({"subject": {"key": "s1"}, "source": "session"}, muted, 600.0))
        self.assertFalse(feed.muted_by({"subject": {"key": "s1"}, "level": "security"}, muted, 100.0))


class FeedManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def setUp(self):
        if os.path.isdir(feed.FEED_DIR):
            for n in os.listdir(feed.FEED_DIR):
                os.unlink(os.path.join(feed.FEED_DIR, n))
        self.m._feed_init()
        for sid in [k for k, s in self.m.sessions.items() if s.id.startswith("fd")]:
            self.m.sessions.pop(sid)
        _env.set_modules(self, agent=False)   # D10: the Feed runs while the Global agent module is on

    def enable(self, **kw):
        feed._write(feed.CONFIG_FILE, kw)
        features.save({"agent": True})   # setUp's cleanup restores the file

    def add(self, sid, **kw):
        import sessions
        s = sessions.Session(id=sid, **dict({"project": "shop", "state": "working"}, **kw))
        self.m.sessions[s.id] = s
        return s

    def test_off_is_inert(self):
        self.add("fd01", state="waiting", idle=True, since=0)
        with mock.patch.object(self.m, "git_ci") as ci, mock.patch.object(self.m, "_ib_views") as ib:
            self.m._feed_tick()
            self.m._feed_tick()
        ci.assert_not_called()
        ib.assert_not_called()
        self.assertFalse(os.path.isdir(feed.FEED_DIR) and [n for n in os.listdir(feed.FEED_DIR) if n.startswith("events")])
        self.assertEqual(self.m.feed_summary(), {"on": False})
        self.assertEqual(self.m.feed(), {"on": False})
        self.assertEqual(self.m.chat(), {"on": False})
        with self.assertRaises(ValueError):
            self.m.chat_send("ahoj")

    def test_failed_rows_at_same_time_are_dismissed_independently(self):
        rows = mock.patch.object(self.m, "_allowed_failed", [], create=True)
        rows.start()
        self.addCleanup(rows.stop)
        s = self.add("fd-same-time")
        now = time.time()
        for enabled in (False, True):
            with self.subTest(enabled=enabled), mock.patch.object(feed, "load_config", return_value={
                    "enabled": enabled, "retention_days": 30}), mock.patch.object(feed, "time") as clock:
                clock.time.return_value = now
                self.m.feed_allowed_failed(s, "spawn_session", "first")
                first = self.m._allowed_failed[-1]["key"]
                self.m.feed_dismiss(first)
                self.m.feed_allowed_failed(s, "spawn_session", "second")
                second = self.m._allowed_failed[-1]["key"]
                self.assertNotEqual(first, second)
                keys = [a["key"] for a in self.m.queue_summary()["actions"]]
                self.assertNotIn(first, keys)
                self.assertIn(second, keys)
                self.m.feed_dismiss(second)
                self.assertNotIn(second, [a["key"] for a in self.m.queue_summary()["actions"]])

    def test_events_persist_and_reload(self):
        self.enable()
        T = time.time() - 100
        s = self.add("fd02", state="working")
        self.m._feed_tick(now=T + 0)             # baseline
        s.state, s.idle = "waiting", True
        self.m._feed_tick(now=T + 1)
        s.state, s.idle = "working", False
        self.m._feed_tick(now=T + 2)
        s.state, s.idle = "waiting", True
        self.m._feed_tick(now=T + 3)             # the second turn folds into the first line
        evs = [e for e in self.m.feed()["events"] if e["key"] == "turn:fd02"]
        self.assertEqual([(e["title"], e["n"]) for e in evs], [("shop finished a turn", 2)])
        self.m._feed_init()
        self.m._feed_load_events(30)
        again = [e for e in self.m._feed_events if e["key"] == "turn:fd02"]
        self.assertEqual([e["n"] for e in again], [2])
        old = os.path.join(feed.FEED_DIR, "events-2001-01.jsonl")   # a month past the retention is removed
        open(old, "w").write('{"id": "x", "ts": 978307200, "key": "k"}\n')
        self.m._feed_load_events(30)
        self.assertFalse(os.path.exists(old))

    def test_actions_seen_dismiss_mute(self):
        self.enable(wait_min=0)
        self.add("fd03", state="waiting", idle=True, since=time.time() - 10)
        self.add("fd04", state="attention", watch=["/x/secret"])
        self.m._feed_tick()
        levels = [a["level"] for a in self.m.feed()["actions"] if a["subject"].get("key", "").startswith("fd")]
        self.assertEqual(levels, ["security", "waiting"])
        with mock.patch.object(self.m, "prod_summary", return_value={"v": 1, "status": "ok", "new": 1, "on": True}):
            self.m._feed_tick()
            v = self.m.feed_summary()["v"]
            self.m._feed_tick(now=time.time() + 5)
            self.assertEqual(self.m.feed_summary()["v"], v)   # nothing changed: the GUI does not refetch
            self.assertIn("prod", [a["level"] for a in self.m.feed()["actions"]])
        wait = next(a for a in self.m.feed()["actions"] if a["level"] == "waiting")
        self.m.feed_dismiss(wait["key"])
        self.assertNotIn(wait["key"], [a["key"] for a in self.m.feed()["actions"]])
        self.assertEqual(self.m.feed_undismiss(), {"restored": 1})   # a misclick is not final
        self.assertIn(wait["key"], [a["key"] for a in self.m.feed()["actions"]])
        self.m.feed_dismiss(wait["key"])
        sec = next(a for a in self.m.feed()["actions"] if a["level"] == "security")
        with self.assertRaises(ValueError):
            self.m.feed_dismiss(sec["key"])
        self.m.feed_mute("fd04", 60)
        self.assertIn(sec["key"], [a["key"] for a in self.m.feed()["actions"]])   # security is never muted
        self.m.feed_seen(time.time())
        self.assertEqual(self.m.feed_summary()["unread"], 0)
        st = common.read_json(feed.STATE_FILE, {})
        self.assertIn(wait["key"], st["dismissed"])
        self.assertIn("fd04", st["muted"])

    def test_queue_works_with_the_feed_off(self):
        # the Overview queue: derived at request time, no wait threshold, no Feed needed
        self.add("fd20", state="waiting", idle=True, since=time.time() - 5)
        self.add("fd21", state="blocked", blocks=[])
        with mock.patch.object(self.m, "git_ci") as ci:
            q = self.m.queue_summary()
        ci.assert_not_called()
        mine = [(a["level"], a["subject"]["key"]) for a in q["actions"] if a["subject"].get("key", "").startswith("fd")]
        self.assertEqual(mine, [("blocked", "fd21"), ("waiting", "fd20")])
        self.assertNotIn("dismissed", q)
        self.assertFalse(os.path.isdir(feed.FEED_DIR) and [n for n in os.listdir(feed.FEED_DIR) if n.startswith("events")])

    def test_queue_never_breaks_api_state(self):
        with mock.patch.object(self.m, "pending_list", side_effect=RuntimeError("boom")):
            q = self.m.queue_summary()
        self.assertEqual(q["actions"], [])
        self.assertIn("boom", q["error"])

    def test_queue_dismiss_and_undismiss(self):
        self.enable(wait_min=30)                          # a removed key of an old config: ignored
        s = self.add("fd22", state="waiting", idle=True, since=time.time() - 5)
        self.m._feed_tick()
        self.assertIn("fd22", [a["subject"]["key"] for a in self.m.feed()["actions"]])   # at once, like the queue
        key = next(a["key"] for a in self.m.queue_summary()["actions"] if a["subject"]["key"] == "fd22")
        self.m.feed_dismiss(key)                          # the queue's own key is accepted
        q = self.m.queue_summary()
        self.assertNotIn(key, [a["key"] for a in q["actions"]])
        self.assertEqual(q["dismissed"], 1)
        self.assertEqual(self.m.feed_undismiss(), {"restored": 1})
        self.assertIn(key, [a["key"] for a in self.m.queue_summary()["actions"]])
        s.since = time.time() - 1                         # a new wait is a new key: a dismiss does not carry over
        self.assertNotEqual(key, next(a["key"] for a in self.m.queue_summary()["actions"] if a["subject"]["key"] == "fd22"))

    def test_gitlab_off_stops_ci_polling_and_removes_cached_queue_actions(self):
        self.enable()
        self.add("fd-ci-off", state="working", gitlab=["grp/shop"])
        r = {"project": "shop", "branch": "main", "pipeline": {"id": 4, "status": "failed", "web_url": "u"}}
        with mock.patch.object(self.m, "git_ci", return_value=r):
            self.m._feed_tick()
        self.assertIn("ci", [a["level"] for a in self.m.queue_summary()["actions"]])
        features.save({"gitlab": False})
        self.assertNotIn("ci", [a["level"] for a in self.m.queue_summary()["actions"]])
        with mock.patch.object(self.m, "git_ci") as ci:
            self.m._feed_tick()
        ci.assert_not_called()
        self.assertEqual(self.m._feed_ci, {})
        features.save({"gitlab": True})
        with mock.patch.object(self.m, "git_ci", return_value=r) as ci:
            self.m._feed_tick()
        ci.assert_called_once_with("shop")

    def test_ci_polled_only_for_live_projects_and_rate_limited(self):
        self.enable()
        self.add("fd05", state="working", gitlab=["grp/shop"])
        self.add("fd06", project="web", state="exited", gitlab=["grp/web"])
        r = {"project": "shop", "branch": "main", "pipeline": {"id": 4, "status": "failed", "web_url": "u"}}
        with mock.patch.object(self.m, "git_ci", return_value=r) as ci:
            self.m._feed_tick()
            self.m._feed_tick()
        self.assertEqual(ci.call_args_list, [mock.call("shop")])
        self.assertIn("ci", [a["level"] for a in self.m.feed()["actions"]])
        self.m._feed_ci_at.clear()
        with mock.patch.object(self.m, "git_ci", return_value={"project": "shop", "pipeline": None, "error": "HTTP 502"}):
            self.m._feed_tick()
        self.assertIn("ci", [a["level"] for a in self.m.feed()["actions"]])   # a GitLab hiccup resolves nothing
        self.assertNotIn("resolved: CI shop failed", [e["title"] for e in self.m.feed()["events"]])

    # ------------------------------------------------------------ chat
    def agent(self, **kw):
        return self.add("fd10", **dict({"kind": "advisor", "project": "advisor", "title": "Global agent",
                                        "state": "working"}, **kw))

    def flush(self, a):
        self.m._chat_try = 0.0
        self.m._chat_flush(a)
        for _ in range(100):
            if not self.m._chat_sending:
                break
            time.sleep(0.02)

    def test_chat_waits_for_an_idle_empty_prompt(self):
        self.enable()
        a = self.agent()
        with mock.patch.object(self.m, "type_text") as typed, mock.patch.object(self.m, "auto_type_ok", return_value=True):
            m1 = self.m.chat_send("What is shop doing?")
            self.flush(a)
            typed.assert_not_called()                       # working
            a.state, a.idle = "attention", False            # a permission prompt
            self.flush(a)
            typed.assert_not_called()
            a.state, a.idle, a.handoff_notify = "waiting", True, ["x"]   # the handoff line goes first
            self.flush(a)
            typed.assert_not_called()
            a.handoff_notify = []
            self.m.chat_send("And web?")
            self.flush(a)
        typed.assert_called_once()
        text = typed.call_args[0][1]
        self.assertEqual(feed.chat_turn_ids(text), [m1["id"], "m%d" % (int(m1["id"][1:]) + 1)])
        self.assertIn("What is shop doing?", text)
        self.assertEqual(self.m.feed_summary()["chat_queued"], 0)

    def test_chat_left_unsubmitted_is_an_error_with_retry_not_sent(self):
        """The human typed in type_text's 0.3 s: the text sits in the prompt without Enter -
        never marked sent (it would vanish after 30 min), and Send again puts it back."""
        self.enable()
        a = self.agent(state="waiting", idle=True)
        with mock.patch.object(self.m, "type_text", return_value=False), mock.patch.object(self.m, "auto_type_ok", return_value=True):
            m = self.m.chat_send("What is shop doing?")
            self.flush(a)
        q = {x["id"]: x for x in self.m.chat()["queue"]}
        self.assertEqual(q[m["id"]]["state"], "error")
        self.assertIn("with Enter", q[m["id"]]["error"])
        self.m.chat_retry(m["id"])
        self.assertEqual({x["id"]: x["state"] for x in self.m.chat()["queue"]}[m["id"]], "queued")
        with mock.patch.object(self.m, "type_text", return_value=False), mock.patch.object(self.m, "auto_type_ok", return_value=True):
            self.flush(a)
        self.m.chat_cancel(m["id"])   # not wanted any more: it can be thrown away, not only resent
        self.assertNotIn(m["id"], [x["id"] for x in self.m.chat()["queue"]])

    def test_a_message_canceled_while_typing_is_not_typed(self):
        self.enable()
        a = self.agent(state="waiting", idle=True)
        m = self.m.chat_send("canceled")

        def cancel_first(*_a, **_k):   # the cancel lands between the snapshot and the typing
            self.m.chat_cancel(m["id"])
            return True
        with mock.patch.object(self.m, "type_text") as typed, \
                mock.patch.object(self.m, "auto_type_ok", side_effect=cancel_first):
            self.flush(a)
        typed.assert_not_called()

    def test_broken_config_and_event_lines_never_break_the_feed(self):
        feed._write(feed.CONFIG_FILE, {})
        with open(feed.CONFIG_FILE, "w") as f:
            f.write("{bad json")
        self.assertFalse(feed.load_config()["enabled"])   # defaults, not a 500 of /api/state
        self.enable()
        os.makedirs(feed.FEED_DIR, exist_ok=True)
        with open(self.m._events_file(time.time()), "a") as f:
            f.write('{"id": "bad", "ts": "abc", "key": "k"}\n{"id": "okk", "ts": %f, "key": "k2", "title": "t"}\n' % time.time())
        self.m._feed_load_events(30)
        self.assertIn("okk", [e["id"] for e in self.m._feed_events])

    def test_chat_held_by_a_half_written_prompt(self):
        self.enable()
        a = self.agent(state="waiting", idle=True)
        with mock.patch.object(self.m, "type_text") as typed, mock.patch.object(self.m, "auto_type_ok", return_value=False):
            self.m.chat_send("ahoj")
            self.flush(a)
        typed.assert_not_called()
        self.assertEqual([q["state"] for q in self.m.chat()["queue"]], ["queued"])
        mid = self.m.chat()["queue"][0]["id"]
        self.m.chat_cancel(mid)
        self.assertEqual(self.m.chat()["queue"], [])
        with self.assertRaises(ValueError):
            self.m.chat_cancel(mid)

    def test_chat_start_passes_the_queue_as_the_task(self):
        self.enable()
        self.m.chat_send("Plan #12")

        class S:
            id = "fd11"
        with mock.patch.object(self.m, "create", return_value=S()) as create:
            r = self.m.chat_start()
        project, task = create.call_args[0][:2]
        self.assertEqual(project, "advisor")
        self.assertEqual(feed.chat_turn_ids(task), r["sent"])
        self.assertEqual(self.m.feed_summary()["chat_queued"], 0)

    def test_http_routes(self):
        import piperd
        from test_piperd import get, post
        was = piperd.MANAGER
        piperd.MANAGER = self.m
        self.addCleanup(setattr, piperd, "MANAGER", was)
        tok = common.mint_token("test-feed")
        code, _, body = get("/api/state", token=tok)
        self.assertEqual((code, json.loads(body)["feed"]), (200, {"on": False}))   # off: the GUI builds nothing
        self.assertEqual(json.loads(get("/api/feed", token=tok)[2]), {"on": False})
        self.assertEqual(post("/api/feed/chat", {"text": "x"}, token=tok)[0], 409)
        self.enable(wait_min=0)
        self.add("fd20", state="waiting", idle=True, since=time.time() - 5)
        self.m._feed_tick()
        self.assertEqual(post("/api/feed/seen", {"ts": 1}), (401, mock.ANY, mock.ANY))   # no token
        st = json.loads(get("/api/state", token=tok)[2])["feed"]
        self.assertTrue(st["on"])
        self.assertGreaterEqual(st["open"], 1)
        acts = json.loads(get("/api/feed", token=tok)[2])["actions"]
        key = next(a["key"] for a in acts if a["subject"]["key"] == "fd20")
        self.assertEqual(post("/api/feed/dismiss", {"key": key}, token=tok)[0], 200)
        self.assertEqual(post("/api/feed/dismiss", {"key": "nope"}, token=tok)[0], 404)
        self.assertEqual(post("/api/feed/mute", {"key": "fd20", "minutes": "x"}, token=tok)[0], 409)
        self.assertEqual(post("/api/feed/seen", {"ts": time.time()}, token=tok)[0], 200)
        code, _, body = post("/api/feed/chat", {"text": "Ahoj"}, token=tok)
        mid = json.loads(body)["id"]
        self.assertEqual(code, 200)
        self.assertEqual([q["id"] for q in json.loads(get("/api/feed/chat", token=tok)[2])["queue"]], [mid])
        self.assertEqual(post("/api/feed/chat/cancel", {"id": mid}, token=tok)[0], 200)
        self.assertEqual(post("/api/feed/nope", {}, token=tok)[0], 404)

    def test_a_sent_message_never_silently_disappears(self):
        self.enable()
        a = self.agent(state="waiting", idle=True)
        with mock.patch.object(self.m, "type_text"), mock.patch.object(self.m, "auto_type_ok", return_value=True):
            mid = self.m.chat_send("Status?")["id"]
            self.flush(a)
        self.assertEqual([(q["id"], q["state"]) for q in self.m.chat()["queue"]], [(mid, "sent")])   # not read yet
        with self.assertRaises(ValueError):
            self.m.chat_retry(mid)                 # the agent still runs: it will read it
        a.state = "exited"                         # it ended before reading
        self.assertEqual([q["state"] for q in self.m.chat()["queue"]], ["lost"])
        self.m.chat_retry(mid)
        self.assertEqual([q["state"] for q in self.m.chat()["queue"]], ["queued"])
        self.assertEqual(self.m.feed_summary()["chat_queued"], 1)

    def test_chat_view_from_the_transcript(self):
        self.enable()
        a = self.agent(state="waiting", claude_session="c0ffee00")
        d = os.path.join(a.cfg, "projects", "-x")
        os.makedirs(d, exist_ok=True)
        rows = [{"type": "user", "message": {"content": feed.chat_wrap(["m1"], ["Status?"])}, "timestamp": "t1"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Two sessions are working."}]}},
                {"type": "user", "message": {"content": "from the terminal"}}]
        with open(os.path.join(d, "c0ffee00.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows))
        v = self.m.chat()
        self.assertEqual(v["agent"]["id"], "fd10")
        self.assertEqual([(t["role"], t["text"], t["chat"], t["from_terminal"]) for t in v["turns"]],
                         [("user", "Status?", ["m1"], False), ("assistant", "Two sessions are working.", [], False),
                          ("user", "from the terminal", [], True)])

    def test_chat_context_started_cards_and_suggestion_counts(self):
        # F21: the sessions the agent started (newest first, hidden ones out), its cards in the queue, and the counts
        # the empty chat's suggestions come from - assigned issues, sessions waiting for you, MRs to review
        self.enable()
        a = self.agent(state="ready")
        self.add("fd31", origin="advisor", title="Reproduce #512", state="exited", created=10.0)
        self.add("fd32", origin="advisor", title="Fix OrderService", state="waiting", created=20.0)
        self.add("fd33", origin="advisor", title="hidden one", hidden=True, created=30.0)
        self.add("fd34", origin="gui", title="mine", state="attention", created=40.0)
        ib = {"g#issue#1": {"type": "issue", "reasons": ["assigned"]}, "g#issue#2": {"type": "issue", "reasons": ["assigned", "mentioned"]},
                            "g#issue#3": {"type": "issue", "reasons": ["mentioned"]}, "g#mr#4": {"type": "mr", "reasons": ["review_requested"]}}
        pend = [{"id": "c1", "kind": "spawn_session", "session": "fd10", "payload": {"project": "shop"}},
                {"id": "c2", "kind": "write", "session": "fd32", "payload": {}}]
        with mock.patch.object(self.m, "pending_list", return_value=pend), mock.patch.object(self.m, "_ib_items", ib):
            v = self.m.chat()
        self.assertEqual([(x["id"], x["state"]) for x in v["started"]], [("fd32", "waiting"), ("fd31", "exited")])
        self.assertEqual(v["cards"], [{"kind": "spawn_session", "project": "shop"}])   # only the agent's own card
        self.assertEqual(v["context"], {"assigned": 2, "waiting": 2, "review": 1})
        self.assertEqual(a.state, "ready")

    def transcript(self, s, rows):
        d = os.path.join(s.cfg, "projects", "-x")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "%s.jsonl" % s.claude_session)
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows))
        self.addCleanup(os.unlink, path)   # an agent without a Claude id would read it as its newest transcript

    def test_chat_start_keeps_the_task_within_the_session_limit(self):
        # regression: 3 queued messages of 8000 chars made create() refuse the task (> 20000), every time
        self.enable()
        ids = [self.m.chat_send(c * 8000)["id"] for c in "abc"]

        class S:
            id = "fd11"
        with mock.patch.object(self.m, "create", return_value=S()) as create:
            r = self.m.chat_start()
        task = create.call_args[0][1]
        self.assertLessEqual(len(task), 20000)
        self.assertEqual(feed.chat_turn_ids(task), r["sent"])
        self.assertTrue(r["sent"] and r["sent"] == ids[:len(r["sent"])])
        self.assertEqual([q["id"] for q in self.m.chat()["queue"] if q["state"] == "queued"], ids[len(r["sent"]):])

    def test_a_message_read_long_ago_is_not_pending_again(self):
        # regression: `read` was taken from the last 120 turns only - an older answered message came back as "sent"
        self.enable()
        a = self.agent(state="waiting", idle=True, claude_session="c0ffee01")
        with mock.patch.object(self.m, "type_text"), mock.patch.object(self.m, "auto_type_ok", return_value=True):
            mid = self.m.chat_send("Status?")["id"]
            self.flush(a)
        rows = [{"type": "user", "message": {"content": feed.chat_wrap([mid], ["Status?"])}}]
        for i in range(130):
            rows += [{"type": "assistant", "message": {"content": [{"type": "text", "text": "answer %d" % i}]}},
                     {"type": "user", "message": {"content": "go on %d" % i}}]
        self.transcript(a, rows)
        self.assertEqual(self.m.chat()["queue"], [])

    def test_a_message_read_by_the_previous_agent_is_not_lost(self):
        # regression: only the current agent's transcript counted - a message the ended agent had read
        # showed as "not delivered" with Send again once a new Global agent started
        self.enable()
        a = self.agent(state="waiting", idle=True, claude_session="c0ffee02")
        with mock.patch.object(self.m, "type_text"), mock.patch.object(self.m, "auto_type_ok", return_value=True):
            mid = self.m.chat_send("Status?")["id"]
            self.flush(a)
        self.transcript(a, [{"type": "user", "message": {"content": feed.chat_wrap([mid], ["Status?"])}},
                            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}}])
        a.state = "exited"
        b = self.add("fd12", kind="advisor", project="advisor", title="Global agent", state="waiting",
                     claude_session="c0ffee03")
        b.created = a.created + 1
        self.assertEqual(self.m.chat()["agent"]["id"], "fd12")
        self.assertEqual(self.m.chat()["queue"], [])


if __name__ == "__main__":
    unittest.main()
