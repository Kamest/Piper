"""Claude plan limits (limits.py): the login picked, the answer parsed, the summary's states."""
import http.client
import io
import json
import os
import time
import unittest
import urllib.error

import _env  # noqa: F401  (must be first: sets PIPER_HOME etc.)
import common
import limits

NOW = 1_800_000_000.0
ANSWER = {
    "five_hour": {"utilization": 4.0, "resets_at": "2027-01-15T15:30:00.234806+00:00"},
    "seven_day": {"utilization": 53.0, "resets_at": "2027-01-17T00:00:00+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 4, "severity": "normal", "resets_at": "2027-01-15T15:30:00.234806+00:00"},
        {"kind": "weekly_all", "group": "weekly", "percent": 53, "severity": "normal", "resets_at": "2027-01-17T00:00:00+00:00"},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 54, "severity": "normal", "resets_at": "2027-01-17T00:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def answering(body, calls):
    def urlopen(req, timeout=0):
        calls.append(req)
        if isinstance(body, Exception):
            raise body
        return FakeResponse(json.dumps(body).encode() if not isinstance(body, bytes) else body)
    return urlopen


class LimitsTest(unittest.TestCase):
    def setUp(self):
        limits._STATE = None
        limits._LAST_LOG = ""
        limits._DELAY = float(limits.TICK)
        if os.path.exists(limits.CACHE_FILE):
            os.unlink(limits.CACHE_FILE)
        self.dir = os.path.join(_env.ROOT, "logins")
        os.makedirs(self.dir, exist_ok=True)

    def login(self, name, expires_ms, token="tok-" + "x" * 20, plan="team"):
        p = os.path.join(self.dir, name)
        with open(p, "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": token, "refreshToken": "r", "expiresAt": expires_ms, "subscriptionType": plan}}, f)
        return p

    def test_the_login_that_expires_last_wins(self):
        a = self.login("a.json", int((NOW + 100) * 1000), token="older-token-aaaaaaaaaa")
        b = self.login("b.json", int((NOW + 5000) * 1000), token="newer-token-bbbbbbbbbb")
        self.assertEqual(limits.read_login([a, b])["token"], "newer-token-bbbbbbbbbb")
        self.assertEqual(limits.read_login([b, a])["token"], "newer-token-bbbbbbbbbb")
        self.assertIsNone(limits.read_login([os.path.join(self.dir, "none.json")]))
        with open(a, "w") as f:
            f.write("{broken")
        self.assertEqual(limits.read_login([a, b])["plan"], "team")

    def test_parse_prefers_limits_and_keeps_the_scoped_weekly(self):
        d = limits.parse(ANSWER)
        self.assertEqual(d["session"]["percent"], 4)
        self.assertAlmostEqual(d["session"]["resets_at"], 1800027000.234806, places=3)
        self.assertEqual(d["week"]["percent"], 53)
        self.assertEqual(d["scoped"], [{"label": "Fable", "percent": 54, "resets_at": 1800144000.0, "severity": "normal"}])
        old = limits.parse({"five_hour": {"utilization": 99.6, "resets_at": "2027-01-15T15:30:00Z"}, "seven_day": {"utilization": 10}})
        self.assertEqual((old["session"]["percent"], old["week"]["percent"], old["week"]["resets_at"], old["scoped"]), (99, 10, None, []))
        nul = limits.parse({"limits": [{"kind": "session", "percent": None, "resets_at": "2027-01-15T15:30:00Z"}],
                            "five_hour": {"utilization": 0.0, "resets_at": "2027-01-15T15:30:00Z"}})
        self.assertEqual(nul["session"]["percent"], 0)   # a null percent in `limits`: five_hour fills in
        with self.assertRaises(ValueError):
            limits.parse({"limits": []})
        with self.assertRaises(ValueError):
            limits.parse([])

    def test_refresh_ok_then_error_keeps_the_answer_and_the_cache_survives(self):
        p = self.login("a.json", int((NOW + 5000) * 1000))
        calls = []
        st = limits.refresh(answering(ANSWER, calls), now=NOW, paths=[p])
        self.assertEqual((st["status"], st["plan"], st["fetched_at"], st["session"]["percent"]), ("ok", "team", NOW, 4))
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer tok-" + "x" * 20)
        self.assertEqual(calls[0].get_header("Anthropic-beta"), "oauth-2025-04-20")
        self.assertNotIn("tok-", json.dumps(st))
        self.assertNotIn("tok-", open(limits.CACHE_FILE).read())
        st = limits.refresh(answering(urllib.error.HTTPError(limits.URL, 503, "down", {}, None), calls), now=NOW + 60, paths=[p])
        self.assertEqual((st["status"], st["error"], st["fetched_at"], st["session"]["percent"]), ("error", "HTTP 503", NOW, 4))
        self.assertNotIn("checked_at", st)   # nothing that moves every tick: the ETag of /api/state stays
        st = limits.refresh(answering(urllib.error.HTTPError(limits.URL, 401, "nope", {}, None), calls), now=NOW + 90, paths=[p])
        self.assertEqual((st["status"], st["error"]), ("expired", "login refused (HTTP 401)"))   # a /logout: the same advice as an expired login
        st = limits.refresh(answering(http.client.IncompleteRead(b"x"), calls), now=NOW + 100, paths=[p])
        self.assertEqual((st["status"], st["error"]), ("error", "bad answer (IncompleteRead)"))
        st = limits.refresh(answering(b"<html>", calls), now=NOW + 120, paths=[p])
        self.assertEqual(st["status"], "error")
        self.assertTrue(st["error"].startswith("unexpected answer"))
        limits._STATE = None   # a piperd restart: the last good answer is back from the file
        self.assertEqual((limits.summary()["status"], limits.summary()["week"]["percent"]), ("ok", 53))

    def test_429_backs_off_honours_retry_after_and_keeps_a_fresh_answer_ok(self):
        p = self.login("a.json", int((NOW + 90000) * 1000))
        calls = []
        limits.refresh(answering(ANSWER, calls), now=NOW, paths=[p])
        self.assertEqual(limits._DELAY, limits.TICK)
        too_many = urllib.error.HTTPError(limits.URL, 429, "slow down", {}, None)
        st = limits.refresh(answering(too_many, calls), now=NOW + 300, paths=[p])
        self.assertEqual((st["status"], st["error"], st["fetched_at"]), ("ok", "", NOW))   # fresh: no error in the panel
        self.assertEqual(limits._DELAY, min(limits.BACKOFF_MAX, 2 * max(limits.TICK, 30)))
        first = limits._DELAY
        limits.refresh(answering(too_many, calls), now=NOW + 900, paths=[p])
        self.assertEqual(limits._DELAY, min(limits.BACKOFF_MAX, 2 * first))      # doubles
        for _ in range(10):
            limits.refresh(answering(too_many, calls), now=NOW + 1000, paths=[p])
        self.assertEqual(limits._DELAY, limits.BACKOFF_MAX)                       # capped
        asked = urllib.error.HTTPError(limits.URL, 429, "slow down", {"Retry-After": "3600"}, None)
        limits.refresh(answering(asked, calls), now=NOW + 1100, paths=[p])
        self.assertEqual(limits._DELAY, 3600)                                     # a long Retry-After is honoured
        limits._DELAY = float(limits.TICK)
        short = urllib.error.HTTPError(limits.URL, 429, "slow down", {"Retry-After": "5"}, None)
        limits.refresh(answering(short, calls), now=NOW + 1150, paths=[p])
        self.assertEqual(limits._DELAY, min(limits.BACKOFF_MAX, 2 * max(limits.TICK, 30)))   # a short one is only a floor
        st = limits.refresh(answering(too_many, calls), now=NOW + limits.FRESH + 1, paths=[p])
        self.assertEqual((st["status"], st["error"]), ("error", "rate limited (HTTP 429)"))   # stale now: say why
        self.assertEqual(st["session"]["percent"], 4)                           # the last numbers stay
        st = limits.refresh(answering(ANSWER, calls), now=NOW + 5000, paths=[p])
        self.assertEqual((st["status"], limits._DELAY), ("ok", limits.TICK))   # a good answer resets the wait
        self.assertEqual(limits._retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, now=1445412470.0), 10.0)
        self.assertIsNone(limits._retry_after({"Retry-After": "soon"}))

    def test_expired_login_is_reported_and_never_sent(self):
        p = self.login("a.json", int((NOW - 1) * 1000))
        calls = []
        st = limits.refresh(answering(ANSWER, calls), now=NOW, paths=[p])
        self.assertEqual((st["status"], st["error"], calls), ("expired", "login expired", []))
        st = limits.refresh(answering(ANSWER, calls), now=NOW, paths=[os.path.join(self.dir, "none.json")])
        self.assertEqual((st["status"], calls), ("no-login", []))
        self.assertEqual(limits.summary()["v"], 1)

    def test_the_token_never_leaves_the_login_file(self):
        """Every path of refresh(): the access token is in no summary, no cache file, no log line (review 4)."""
        secret = "sk-ant-oat01-" + "Q" * 40
        p = self.login("a.json", int((NOW + 90000) * 1000), token=secret)
        bodies = [ANSWER, urllib.error.HTTPError(limits.URL, 401, "no", {}, None),
                  urllib.error.HTTPError(limits.URL, 429, "slow", {"Retry-After": "99999999999"}, None),
                  urllib.error.HTTPError(limits.URL, 500, secret, {}, None),
                  urllib.error.URLError("refused"), http.client.IncompleteRead(b"x"), b"not json", ANSWER]
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            for i, body in enumerate(bodies):
                calls = []
                st = limits.refresh(answering(body, calls), now=NOW + 10 * i, paths=[p])
                self.assertEqual(calls[0].get_header("Authorization"), "Bearer " + secret)   # sent only here
                self.assertNotIn(secret, json.dumps(st))
                self.assertNotIn(secret, json.dumps(limits.summary()))
                self.assertLessEqual(limits._DELAY, limits.RETRY_AFTER_MAX)   # an absurd Retry-After is capped
        self.assertNotIn(secret, out.getvalue())
        with open(limits.CACHE_FILE, encoding="utf-8") as f:
            self.assertNotIn(secret, f.read())
        self.assertEqual(limits.summary()["status"], "ok")

    def test_a_garbled_login_file_is_no_login(self):
        for i, text in enumerate(["", "[]", "{\"claudeAiOauth\": 5}", "{\"claudeAiOauth\": {\"accessToken\": 7}}",
                                  "{\"claudeAiOauth\": {\"accessToken\": \"\"}}", "\x00\xff"]):
            p = os.path.join(self.dir, "bad%d.json" % i)
            with open(p, "w", encoding="latin-1") as f:
                f.write(text)
            calls = []
            st = limits.refresh(answering(ANSWER, calls), now=NOW, paths=[p])
            self.assertEqual((st["status"], calls), ("no-login", []), text)

    def test_cache_of_another_version_is_ignored(self):
        common.write_json(limits.CACHE_FILE, {"v": 0, "status": "ok"})
        self.assertEqual(limits.summary()["status"], "no-login")

    def test_usage_module_off_stops_the_polling(self):
        """D16: the limits are part of the usage module - off = no request to the endpoint, piperd sends none"""
        from unittest import mock
        _env.set_modules(self, usage=False)
        self.assertFalse(limits.active())
        with mock.patch.object(limits, "refresh") as r:
            limits._tick()
            r.assert_not_called()
        _env.set_modules(self, usage=True)
        self.assertTrue(limits.active())
        with mock.patch.object(limits, "refresh") as r:
            limits._tick()
            r.assert_called_once()


if __name__ == "__main__":
    unittest.main()
