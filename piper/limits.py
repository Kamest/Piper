"""Claude plan limits for the left panel.

piperd asks api.anthropic.com/api/oauth/usage - the call Claude Code's /usage makes - with the
saved login and keeps only the numbers: the 5-hour window ("session"), the 7-day window ("week",
all models) and the model-scoped weekly limits the answer lists. The GUI draws the bars and
counts down to the resets in the browser, from `resets_at`, so the countdown runs while a
session is exhausted, the login expired or piperd unreachable.

The login: the newest of the host's ~/.claude/.credentials.json (what Claude Code on this machine
refreshes, claude_dir=host) and the template's (the last one a session saved, claude_dir=session)
by its expiry. An expired access token is reported, never refreshed: the refresh token is shared
with Claude Code and the sessions (sessioncfg: a rotation here could log them out).

One thread, every TICK seconds (PIPER_LIMITS_TICK, 0 = no loop: tests call refresh()); the last
good answer survives a piperd restart in $PIPER_HOME/limits.json (percentages and times only -
never the token, never the raw answer). A failed fetch keeps that answer and says so in `status`.
The endpoint rate-limits (HTTP 429 every few minutes at a 60 s tick, 2026-10-07): a 429 waits its
Retry-After at least and twice the last wait (up to BACKOFF_MAX), and the next good answer goes back to
TICK. While the last answer is younger than FRESH a 429 is no error for the panel - the numbers
move slowly and the countdowns run in the browser.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import common

URL = "https://api.anthropic.com/api/oauth/usage"
TICK = int(os.environ.get("PIPER_LIMITS_TICK", "300"))
BACKOFF_MAX = 1800   # s: the longest wait after repeated 429s without a Retry-After
RETRY_AFTER_MAX = 6 * 3600   # s: a garbled or absurd Retry-After must not silence the panel until a restart
FRESH = 2400         # s: a 429 within this age of the last good answer keeps status "ok"
FIRST_TICK = 3.0
TIMEOUT = 15
CACHE_FILE = os.path.join(common.PIPER_HOME, "limits.json")
V = 1

_LOCK = threading.Lock()
_STATE: dict | None = None   # the summary (loaded from CACHE_FILE on first use)
_LAST_LOG = ""               # the last error printed: once per change, not once per tick
_DELAY = float(TICK)         # the loop's next sleep: TICK, or the 429 backoff


def login_candidates() -> list[str]:
    return [os.path.join(common.REAL_HOME, ".claude", ".credentials.json"),
            os.path.join(common.TEMPLATE_DIR, ".credentials.json")]


def read_login(paths: list[str] | None = None) -> dict | None:
    """{"token", "expires_at" (epoch s), "plan"} of the login that expires last; None without one."""
    best = None
    for p in paths if paths is not None else login_candidates():
        try:
            with open(p, "rb") as f:
                d = json.loads(f.read(64 << 10).decode())
        except (OSError, ValueError):
            continue
        o = d.get("claudeAiOauth") if isinstance(d, dict) else None
        tok = o.get("accessToken") if isinstance(o, dict) else None
        if not isinstance(tok, str) or not tok:
            continue
        exp = o.get("expiresAt")
        exp_s = float(exp) / 1000.0 if isinstance(exp, (int, float)) else 0.0
        cand = {"token": tok, "expires_at": exp_s, "plan": str(o.get("subscriptionType") or "")}
        if best is None or cand["expires_at"] > best["expires_at"]:
            best = cand
    return best


def _epoch(iso) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _pct(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and v >= 0 else None   # floor: 99.6 % is not "limit reached"


def parse(body: dict) -> dict:
    """The numbers of an answer: {"session": {...}, "week": {...}, "scoped": [{label, percent, ...}]}.
    `limits` (session / weekly_all / weekly_scoped) when the answer has it, else five_hour / seven_day.
    Raises ValueError when neither carries a session window."""
    if not isinstance(body, dict):
        raise ValueError("not an object")
    win = lambda d: {"percent": _pct(d.get("utilization") if "utilization" in d else d.get("percent")),  # noqa: E731
                     "resets_at": _epoch(d.get("resets_at")),
                     "severity": str(d.get("severity") or "normal")}
    session = week = None
    scoped: list[dict] = []
    for lim in body.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        kind = lim.get("kind")
        if kind == "session":
            session = win(lim)
        elif kind == "weekly_all":
            week = win(lim)
        elif kind == "weekly_scoped":
            scope = lim.get("scope") or {}
            model = (scope.get("model") or {}) if isinstance(scope, dict) else {}
            label = str(model.get("display_name") or model.get("id") or "model")
            scoped.append(dict(win(lim), label=label))
    if (session is None or session["percent"] is None) and isinstance(body.get("five_hour"), dict):
        session = win(body["five_hour"])
    if week is None and isinstance(body.get("seven_day"), dict):
        week = win(body["seven_day"])
    if session is None:
        raise ValueError("no session window in the answer")
    return {"session": session, "week": week, "scoped": scoped}


class LoginError(OSError):
    """The API refused the token (401 / 403): a /logout or a revoked login, not a network problem."""


class RateLimited(OSError):
    """HTTP 429; `retry_after` = the seconds the API asked for (Retry-After), or None."""
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _retry_after(headers, now: float | None = None) -> float | None:
    """Retry-After as seconds from now: delta-seconds or an HTTP date; None when absent or garbled."""
    v = str((headers.get("Retry-After") if headers else None) or "").strip()
    if not v:
        return None
    if v.isdigit():
        return float(v)
    try:
        when = parsedate_to_datetime(v).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (time.time() if now is None else now))


def fetch(token: str, urlopen=urllib.request.urlopen) -> dict:
    """GET the usage with `token`; the parsed numbers. OSError / ValueError on failure (the
    message carries the HTTP status, never a header or body)."""
    req = urllib.request.Request(URL, headers={"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
                                               "Accept": "application/json", "User-Agent": "piper"})
    try:
        with urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read(256 << 10)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LoginError("login refused (HTTP %d)" % e.code) from None
        if e.code == 429:
            raise RateLimited("rate limited (HTTP 429)", _retry_after(e.headers)) from None
        raise OSError("HTTP %d" % e.code) from None
    except urllib.error.URLError as e:
        raise OSError("unreachable (%s)" % getattr(e, "reason", e)) from None
    except http.client.HTTPException as e:   # a cut-off answer (IncompleteRead, BadStatusLine): not an OSError
        raise OSError("bad answer (%s)" % type(e).__name__) from None
    try:
        return parse(json.loads(raw.decode()))
    except ValueError as e:
        raise ValueError("unexpected answer: %s" % e) from None


def _load() -> dict:
    global _STATE
    if _STATE is None:
        d = common.read_json(CACHE_FILE, None)
        _STATE = d if isinstance(d, dict) and d.get("v") == V else {"v": V, "status": "no-login", "fetched_at": 0}
    return _STATE


def _log(msg: str) -> None:
    global _LAST_LOG
    if msg != _LAST_LOG:
        _LAST_LOG = msg
        if msg:
            print("piperd: limits: %s" % msg, flush=True)


def refresh(urlopen=urllib.request.urlopen, now: float | None = None, paths: list[str] | None = None) -> dict:
    """One tick: read the login, fetch, update the summary. The network runs OUTSIDE _LOCK - summary()
    answers every /api/state and must never wait for api.anthropic.com (inbox.py does the same). Never raises.
    Sets _DELAY, the loop's next sleep (the 429 backoff)."""
    global _DELAY
    now = time.time() if now is None else now
    delay = float(TICK)
    login = read_login(paths)
    data: dict | None = None
    status, error = "ok", ""
    if login is None:
        status = "no-login"
    elif login["expires_at"] <= now:
        status, error = "expired", "login expired"
    else:
        try:
            data = fetch(login["token"], urlopen)
        except LoginError as e:
            status, error = "expired", str(e)
            _log("%s - run claude once on this machine" % e)
        except RateLimited as e:
            # Retry-After is a floor, not the wait: the endpoint sends a few seconds and a retry that
            # soon only draws the next 429 (live, 2026-10-07) - the doubling goes on underneath it;
            # capped: a garbled or absurd Retry-After must not silence the panel until a restart
            delay = min(RETRY_AFTER_MAX, max(e.retry_after or 0.0, min(BACKOFF_MAX, 2 * max(_DELAY, TICK, 30.0))))
            with _LOCK:
                fresh = now - float(_load().get("fetched_at") or 0) < FRESH
            if not fresh:   # no recent answer to show: say why the numbers are old
                status, error = "error", str(e)
            _log("%s - next try in %d s" % (e, delay))
        except (OSError, ValueError) as e:
            status, error = "error", str(e)
            _log("%s (the panel keeps the last answer)" % e)
        else:
            _log("")
    _DELAY = delay
    with _LOCK:
        st = dict(_load())
        st["status"], st["error"] = status, error
        if login is not None:
            st["plan"] = login["plan"]
        if data is not None:
            st.update(data, fetched_at=now)
        _STATE.clear()
        _STATE.update(st)
        out = dict(st)
    if data is not None:
        try:
            common.write_json(CACHE_FILE, out)
        except OSError:
            pass
    return out


def summary() -> dict:
    """/api/state `limits`: the cached summary, cheap; only what the GUI draws (no tick timestamp,
    so the ETag of /api/state moves only when a number did)."""
    with _LOCK:
        return dict(_load())


def active() -> bool:
    """D16: the limits belong to the usage module - off = no block in the GUI and no polling of the endpoint"""
    import features   # lazy: features is piperd's, the unit tests import limits alone
    return features.on("usage")


def _tick() -> None:
    if not active():
        return
    try:
        refresh()
    except Exception as e:  # noqa: BLE001 - the loop must survive anything
        print("piperd: limits: %s: %s" % (type(e).__name__, e), flush=True)


def _loop() -> None:
    time.sleep(FIRST_TICK)
    while True:
        _tick()
        time.sleep(max(10.0, _DELAY))


def start_thread() -> None:
    if TICK > 0:
        threading.Thread(target=_loop, daemon=True, name="piper-limits").start()
