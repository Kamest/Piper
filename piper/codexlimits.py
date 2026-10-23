"""Read-only Codex plan quotas, separate from token/cost estimates and Claude limits.

Uses the same WHAM usage endpoint as openai/codex's backend-client. Never refreshes
credentials: Codex owns token rotation. Only normalized quota numbers reach the GUI.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request

import common
import limits

URL = "https://chatgpt.com/backend-api/wham/usage"
CACHE_FILE = os.path.join(common.PIPER_HOME, "codex-limits.json")
TICK = limits.TICK
_LOCK = threading.Lock()
_STATE: dict | None = None
_DELAY = float(TICK)


def login_candidates() -> list[str]:
    return [os.path.join(common.REAL_HOME, ".codex", "auth.json"),
            os.path.join(common.PIPER_HOME, "codex-template", "auth.json")]


def _claims(token: str) -> dict:
    try:
        body = token.split(".")[1]
        result = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return result if isinstance(result, dict) else {}
    except (ValueError, IndexError):
        return {}


def read_login(paths: list[str] | None = None) -> dict | None:
    """Host login wins over the template, including logout/API-key mode. No account hopping."""
    for index, path in enumerate(paths if paths is not None else login_candidates()):
        try:
            with open(path, "rb") as f:
                doc = json.loads(f.read(64 << 10))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        if doc.get("auth_mode") == "apikey" or doc.get("OPENAI_API_KEY"):
            return {"mode": "apikey", "identity": "apikey", "source": "host" if index == 0 else "template"}
        tokens = doc.get("tokens")
        token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            return None
        claims = _claims(token)
        auth = claims.get("https://api.openai.com/auth")
        auth = auth if isinstance(auth, dict) else {}
        account = tokens.get("account_id") or auth.get("chatgpt_account_id") or ""
        if not isinstance(account, str) or "\n" in account or "\r" in account:
            return None
        # Only a one-way account fingerprint is persisted, never credentials or identifiers.
        identity = hashlib.sha256((path + "\0" + (account or token)).encode()).hexdigest()
        expires = claims.get("exp")
        return {"mode": "chatgpt", "token": token, "account": account, "identity": identity,
                "expires_at": expires if type(expires) in (int, float) and math.isfinite(expires) else None,
                "source": "host" if index == 0 else "template"}
    return None


def _window(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    percent, reset, duration = (value.get(k) for k in ("used_percent", "reset_at", "limit_window_seconds"))
    valid = lambda x: type(x) in (int, float) and math.isfinite(x) and x >= 0
    return {"percent": min(100, int(percent)) if valid(percent) else None,
            "resets_at": reset if valid(reset) and reset > 0 else None,
            "window_seconds": duration if valid(duration) and duration > 0 else None,
            "severity": "normal"}


def _normalize_windows(state: dict) -> dict:
    """Primary means the plan's main quota: on weekly-only plans it is the seven-day window."""
    session = state.get("session")
    if isinstance(session, dict) and session.get("window_seconds") == 604800:
        state["session"], state["week"] = state.get("week"), session
    return state


def parse(body: dict) -> dict:
    """Default Codex quota only; other model pools must not be mixed into its weekly bar."""
    if not isinstance(body, dict) or not isinstance(body.get("rate_limit"), dict):
        raise ValueError("plan limits unavailable")
    rate = body["rate_limit"]
    session, week = _window(rate.get("primary_window")), _window(rate.get("secondary_window"))
    if not any(w and w["percent"] is not None for w in (session, week)):
        raise ValueError("plan limits unavailable")
    plan = body.get("plan_type")
    return _normalize_windows({"session": session, "week": week, "scoped": [],
            "plan": plan if isinstance(plan, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,60}", plan) else ""})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(login: dict, urlopen=None) -> dict:
    """No redirect may forward the OAuth token. Errors contain no response body or headers."""
    headers = {"Authorization": "Bearer " + login["token"], "Accept": "application/json", "User-Agent": "piper"}
    if login["account"]:
        headers["ChatGPT-Account-Id"] = login["account"]
    req = urllib.request.Request(URL, headers=headers)
    opener = urlopen or urllib.request.build_opener(_NoRedirect()).open
    try:
        with opener(req, timeout=limits.TIMEOUT) as response:
            raw = response.read(256 << 10)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise limits.LoginError("login refused (HTTP %d)" % error.code) from None
        if error.code == 429:
            raise limits.RateLimited("rate limited (HTTP 429)", limits._retry_after(error.headers)) from None
        raise OSError("HTTP %d" % error.code) from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise OSError("usage unavailable (%s)" % type(error).__name__) from None
    try:
        return parse(json.loads(raw))
    except (ValueError, TypeError):
        raise ValueError("plan limits unavailable") from None


def _empty(identity: str = "") -> dict:
    return {"v": 1, "identity": identity, "status": "no-login", "fetched_at": 0}


def _load() -> dict:
    global _STATE
    if _STATE is None:
        doc = common.read_json(CACHE_FILE, None)
        _STATE = _normalize_windows(doc) if isinstance(doc, dict) and doc.get("v") == 1 else _empty()
    return _STATE


def _public(state: dict) -> dict:
    return copy.deepcopy({k: v for k, v in state.items() if k != "identity"})


def summary() -> dict:
    """Cached numbers; an account change/logout immediately hides the previous account's data."""
    login = read_login()
    identity = login["identity"] if login else ""
    with _LOCK:
        state = _load()
        return _public(state if state.get("identity") == identity else _empty(identity))


def refresh(urlopen=None, now: float | None = None, paths: list[str] | None = None) -> dict:
    global _STATE, _DELAY
    now = time.time() if now is None else now
    login = read_login(paths)
    identity = login["identity"] if login else ""
    with _LOCK:
        state = copy.deepcopy(_load()) if _load().get("identity") == identity else _empty(identity)
    delay, data = float(TICK), None
    state.update(status="ok", error="", source=login["source"] if login else "host")
    if login is None:
        state.update(status="no-login")
    elif login["mode"] == "apikey":
        state.update(status="unavailable", error="API key · plan limits unavailable")
    elif login["expires_at"] is not None and login["expires_at"] <= now:
        state.update(status="expired", error="login expired")
    else:
        try:
            data = fetch(login, urlopen)
        except limits.LoginError as error:
            state.update(status="expired", error=str(error))
        except limits.RateLimited as error:
            delay = min(limits.RETRY_AFTER_MAX, max(error.retry_after or 0,
                        min(limits.BACKOFF_MAX, 2 * max(_DELAY, TICK, 30))))
            if not state.get("fetched_at") or now - state["fetched_at"] >= limits.FRESH:
                state.update(status="error", error="rate limited (HTTP 429)")
        except (OSError, ValueError) as error:
            state.update(status="error", error=str(error))
        if data is not None:
            state.update(data, fetched_at=now)
    _DELAY = delay
    with _LOCK:
        _STATE = state
    # Network I/O is outside the lock used by /api/state. Persist normalized values only.
    if data is not None or state["status"] in ("no-login", "unavailable"):
        try:
            common.write_json(CACHE_FILE, state)
        except OSError:
            pass
    return _public(state)


def active() -> bool:
    import features
    return features.on("usage") and features.on("codex")


def _tick() -> None:
    if active():
        try:
            refresh()
        except Exception as error:  # keep the worker alive, never log credential-bearing exceptions
            print("piperd: codex limits: refresh failed (%s)" % type(error).__name__, flush=True)


def _loop() -> None:
    time.sleep(limits.FIRST_TICK)
    while True:
        _tick()
        time.sleep(max(10.0, _DELAY))


def start_thread() -> None:
    if TICK > 0:
        threading.Thread(target=_loop, daemon=True, name="piper-codex-limits").start()
