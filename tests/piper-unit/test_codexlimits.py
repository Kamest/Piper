"""Codex quotas: real response shape, account separation and credential-safe failure paths."""
import _env  # noqa: F401

import base64
import contextlib
import io
import json
import math
import os
from pathlib import Path
import unittest
import urllib.error
from unittest import mock

import codexlimits as c
import common
import limits

NOW = 1_800_000_000
ANSWER = {"plan_type": "pro", "rate_limit": {
    "primary_window": {"used_percent": 24, "limit_window_seconds": 18000, "reset_at": NOW + 7200},
    "secondary_window": {"used_percent": 61, "limit_window_seconds": 604800, "reset_at": NOW + 200000}},
    "additional_rate_limits": [{"limit_name": "other", "rate_limit": {"primary_window": {"used_percent": 99}}}]}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class CodexLimitsTest(unittest.TestCase):
    def setUp(self):
        c._STATE = None
        c._DELAY = float(c.TICK)
        if os.path.exists(c.CACHE_FILE):
            os.unlink(c.CACHE_FILE)
        self.paths = [os.path.join(_env.ROOT, "codex-auth-%s.json" % n) for n in ("host", "template")]
        for path in self.paths:
            if os.path.exists(path):
                os.unlink(path)
        self.patch = mock.patch.object(c, "login_candidates", return_value=self.paths)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.calls = []

    def login(self, account="account-placeholder", expires=NOW + 10000, path=None):
        claims = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
        token = "header." + claims + ".signature-placeholder"
        common.write_json(path or self.paths[0], {"auth_mode": "chatgpt", "tokens": {
            "access_token": token, "refresh_token": "refresh-placeholder", "account_id": account}})
        return token

    def opener(self, body):
        def call(request, timeout):
            self.calls.append(request)
            if isinstance(body, Exception):
                raise body
            return Response(json.dumps(body).encode())
        return call

    def test_default_quota_keeps_nulls_and_ignores_other_model_pools(self):
        data = c.parse(ANSWER)
        self.assertEqual((data["session"]["percent"], data["week"]["percent"], data["plan"]), (24, 61, "pro"))
        self.assertEqual(data["session"]["window_seconds"], 18000)
        self.assertEqual(data["scoped"], [])
        for value in (None, True, math.nan, "24", -1):
            data = c.parse({**ANSWER, "rate_limit": {**ANSWER["rate_limit"], "primary_window": {"used_percent": value}}})
            self.assertIsNone(data["session"]["percent"])
        for value in (None, [], {}, {"rate_limit": None}):
            with self.assertRaises(ValueError):
                c.parse(value)

    def test_host_login_wins_even_if_template_has_a_different_newer_account(self):
        self.login("host", NOW + 1)
        self.login("template", NOW + 99999, self.paths[1])
        self.assertEqual(c.read_login()["account"], "host")
        common.write_json(self.paths[0], {"auth_mode": "apikey", "OPENAI_API_KEY": "key-placeholder"})
        self.assertEqual(c.refresh(self.opener(ANSWER), NOW)["status"], "unavailable")
        self.assertEqual(self.calls, [])

    def test_week_only_primary_window_and_old_cache_are_not_a_session_limit(self):
        self.login()
        answer = {"plan_type": "business", "rate_limit": {"primary_window": {
            "used_percent": 89, "limit_window_seconds": 604800, "reset_at": NOW + 200000},
            "secondary_window": None}}
        normalized = c.refresh(self.opener(answer), NOW)
        self.assertIsNone(normalized["session"])
        self.assertEqual(normalized["week"]["percent"], 89)
        # A cache from the first deployment may still store this quota in Session.
        old = {**normalized, "identity": c.read_login()["identity"],
               "session": normalized["week"], "week": None}
        common.write_json(c.CACHE_FILE, old)
        c._STATE = None
        self.assertIsNone(c.summary()["session"])
        self.assertEqual(c.summary()["week"]["percent"], 89)
        answer["rate_limit"]["secondary_window"] = ANSWER["rate_limit"]["primary_window"]
        reversed_windows = c.parse(answer)
        self.assertEqual((reversed_windows["session"]["percent"], reversed_windows["week"]["percent"]), (24, 89))

    def test_refresh_cache_and_errors_never_expose_credentials_or_identifiers(self):
        token = self.login()
        response = c.refresh(self.opener(ANSWER), NOW)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(self.calls[0].get_header("Authorization"), "Bearer " + token)
        self.assertEqual(self.calls[0].get_header("Chatgpt-account-id"), "account-placeholder")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for error, expected in [(urllib.error.URLError(token), "error"), (ValueError(token), "error"),
                    (urllib.error.HTTPError(c.URL, 401, token, {}, None), "expired"),
                    (urllib.error.HTTPError(c.URL, 503, token, {}, None), "error")]:
                response = c.refresh(self.opener(error), NOW + 60)
                self.assertEqual((response["status"], response["session"]["percent"], response["fetched_at"]), (expected, 24, NOW))
                for secret in (token, "refresh-placeholder", "account-placeholder"):
                    self.assertNotIn(secret, json.dumps(response))
                    self.assertNotIn(secret, Path(c.CACHE_FILE).read_text())
                    self.assertNotIn(secret, out.getvalue())
        c._STATE = None
        self.assertEqual(c.summary()["week"]["percent"], 61)

    def test_account_change_and_logout_never_show_previous_account_numbers(self):
        self.login("first")
        c.refresh(self.opener(ANSWER), NOW)
        self.login("second")
        self.assertNotIn("session", c.summary())
        changed = c.refresh(self.opener(urllib.error.URLError("offline")), NOW + 10)
        self.assertNotIn("session", changed)
        os.unlink(self.paths[0])
        self.assertEqual(c.summary()["status"], "no-login")

    def test_expired_credentials_are_never_sent_or_refreshed(self):
        self.login(expires=NOW - 1)
        before = Path(self.paths[0]).read_text()
        self.assertEqual(c.refresh(self.opener(ANSWER), NOW)["status"], "expired")
        self.assertEqual(self.calls, [])
        self.assertEqual(Path(self.paths[0]).read_text(), before)

    def test_429_backoff_and_usage_module_off(self):
        self.login()
        c.refresh(self.opener(ANSWER), NOW)
        error = urllib.error.HTTPError(c.URL, 429, "slow", {"Retry-After": "3600"}, None)
        self.assertEqual(c.refresh(self.opener(error), NOW + 20)["status"], "ok")
        self.assertEqual(c._DELAY, 3600)
        self.assertEqual(c.refresh(self.opener(error), NOW + limits.FRESH + 1)["status"], "error")
        c.refresh(self.opener(ANSWER), NOW + 5000)
        self.assertEqual(c._DELAY, c.TICK)

    def test_polling_requires_both_codex_and_usage_features(self):
        for modules in ({"usage": False, "codex": True}, {"usage": True, "codex": False},
                        {"usage": False, "codex": False}, {"usage": True, "codex": True}):
            _env.set_modules(self, **modules)
            with mock.patch.object(c, "refresh") as refresh:
                c._tick()
                self.assertEqual(refresh.call_count, int(all(modules.values())))

    def test_redirects_are_not_followed(self):
        self.assertIsNone(c._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid"))
