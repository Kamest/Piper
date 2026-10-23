"""Mattermost notifications through an incoming webhook.

piperd posts OUTBOUND from the host to an incoming webhook (URL in
PIPER_HOME/mattermost-webhook, 0600, `sh scripts/piper.sh mattermost-webhook < file`); nothing
connects to piperd, which stays loopback-only. A webhook is one-way: it cannot read replies
or edit a post, so answering from the phone (step 2) is postponed until a bot account
exists. Outgoing webhooks and slash commands are not an option: both need Mattermost to
call piperd.

A message goes out when a session has waited > after_s (180 s) for the human - a finished
turn, a permission prompt, a confirmation card - while the GUI is not visible (the GUI
posts a presence heartbeat; none for 60 s = not visible), at most one per session per
min_gap_s (15 min) and once per prompt, never in quiet hours. `detail` (none | command |
full) decides how much of the tool call / Claude's last answer goes along. notify() sends
a one-off message (the morning production summary, 9.12 C). The webhook URL never reaches a
session; every message goes through scrub().

PIPER_HOME/mattermost.json: {"detail", "after_s", "min_gap_s", "quiet": {"from": "20:00",
"to": "07:30", "weekend": true}, "channel", "username", "icon_url"} - all optional.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import common
import digest
import features
from common import audit, scrub

CONFIG_FILE = os.path.join(common.PIPER_HOME, "mattermost.json")
WEBHOOK_FILE = os.path.join(common.PIPER_HOME, "mattermost-webhook")
STATE_FILE = os.path.join(common.PIPER_HOME, "mattermost-state.json")
TIMEOUT = 10
INTERVAL = float(os.environ.get("PIPER_MM_INTERVAL", "20"))   # 0 = no background thread
VISIBLE_S = 60   # a GUI heartbeat younger than this = the human sees the GUI
KEEP_S = 7 * 86400

DEFAULTS = {
    "detail": "command",   # none | command | full
    "after_s": 180, "min_gap_s": 900,
    "quiet": {"from": "", "to": "", "weekend": False},
    "channel": "", "username": "", "icon_url": "",       # webhook overrides
}


class MMError(Exception):
    pass


def url_ok(url: str) -> bool:
    # B-N7-2: printable ASCII only - urllib cannot send anything else (the test message failed "ordinal not in
    # range(128)"); a non-ASCII host or path is written percent-encoded / as punycode
    if not url or any(not (0x21 <= ord(c) < 0x7f) for c in url):
        return False
    u = urllib.parse.urlsplit(url)
    if u.scheme == "http":   # plain http only on loopback (tests)
        return (u.hostname or "") in ("127.0.0.1", "localhost", "::1")
    return u.scheme == "https" and bool(u.hostname)


def webhook_url() -> str:
    try:
        with open(WEBHOOK_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _read(path: str) -> dict:
    try:
        raw = common.read_json(path, {})
    except (OSError, ValueError):   # a hand-edited file with a typo: the defaults
        return {}
    return raw if isinstance(raw, dict) else {}


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    raw = _read(CONFIG_FILE)
    for k, v in raw.items():
        if k not in cfg or isinstance(v, bool) != isinstance(cfg[k], bool):
            continue
        if k == "quiet" and isinstance(v, dict):   # a partial block keeps the other defaults
            cfg[k].update({q: v[q] for q in ("from", "to", "weekend") if q in v})
        elif isinstance(cfg[k], int) and isinstance(v, (int, float)):
            cfg[k] = int(v)
        elif isinstance(v, type(cfg[k])):
            cfg[k] = v
    if cfg["detail"] not in ("none", "command", "full"):
        cfg["detail"] = "command"
    return cfg


_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(raw) -> list[str]:
    """Problems of a mattermost.json document (the GUI settings page saves only a clean one);
    [] = valid. Unknown keys are refused, so a typo does not silently do nothing."""
    if not isinstance(raw, dict):
        return ["mattermost.json must be an object"]
    errs = []
    for k, v in raw.items():
        if k not in DEFAULTS:
            errs.append("unknown key %r" % k)
        elif k == "detail" and v not in ("none", "command", "full"):
            errs.append("detail: none | command | full")
        elif k in ("after_s", "min_gap_s") and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 86400):
            errs.append("%s: a number 0..86400 (seconds)" % k)
        elif k in ("channel", "username", "icon_url") and not (isinstance(v, str) and len(v) <= 200):
            errs.append("%s: text up to 200 characters" % k)
        elif k == "icon_url" and v and not url_ok(v):
            errs.append("icon_url: an https address")
        elif k == "quiet":
            if not isinstance(v, dict) or set(v) - {"from", "to", "weekend"}:
                errs.append("quiet: {from, to, weekend}")
                continue
            for q in ("from", "to"):
                if v.get(q, "") and not _HHMM.match(str(v[q])):
                    errs.append("quiet.%s: HH:MM" % q)
            if bool(v.get("from")) != bool(v.get("to")):
                errs.append("quiet: fill in both from and to, or neither")
            if "weekend" in v and not isinstance(v["weekend"], bool):
                errs.append("quiet.weekend: true / false")
    return errs


def quiet_now(cfg: dict, now: float | None = None) -> bool:
    t = time.localtime(now if now is not None else time.time())
    q = cfg.get("quiet") if isinstance(cfg.get("quiet"), dict) else {}
    if q.get("weekend") and t.tm_wday >= 5:
        return True
    try:
        fh, fm = (int(x) for x in str(q.get("from") or "").split(":"))
        th, tm = (int(x) for x in str(q.get("to") or "").split(":"))
    except ValueError:
        return False
    cur, a, b = t.tm_hour * 60 + t.tm_min, fh * 60 + fm, th * 60 + tm
    return a <= cur < b if a <= b else cur >= a or cur < b


def webhook_post(url: str, payload: dict, opener=None) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=TIMEOUT) as r:
            r.read(1 << 16)
    except urllib.error.HTTPError as e:
        raise MMError("webhook: HTTP %s" % e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MMError("webhook unreachable: %s" % getattr(e, "reason", e)) from None
    except Exception as e:  # noqa: BLE001 - http.client.InvalidURL / BadStatusLine: its text may carry /hooks/<key>
        raise MMError("webhook unreachable: %s" % type(e).__name__) from None


_MD_LINK = re.compile(r"([\\\[\]])")
_MENTION = re.compile(r"(?i)(?<![\w@])@(?=(?:channel|all|here)\b)")
# a code span kept as it is | a bare URL | a lone backtick (it would pair with the span around a URL)
_MD_TOKEN = re.compile(r"(`[^`\n]+`)|((?:\b[a-z][a-z0-9+.-]*://|\bwww\.)[^\s`]+)|`", re.I)


def md_safe(text: str) -> str:
    """Untrusted text (a session title, Claude's answer, a production log title) for Mattermost markdown:
    `[` `]` escaped (no image - Mattermost would fetch its URL - and no link), a bare URL put in a code
    span (no link, no preview fetching it) and @channel / @all / @here broken by a zero-width space."""
    text, out, pos = str(text), [], 0
    for m in _MD_TOKEN.finditer(text):
        out += [_MD_LINK.sub(r"\\\1", text[pos:m.start()]),
                m.group(1) or ("`%s`" % m.group(2) if m.group(2) else "\\`")]
        pos = m.end()
    out.append(_MD_LINK.sub(r"\\\1", text[pos:]))
    return _MENTION.sub("@\u200b", "".join(out))


def _tool_text(name: str, inp: dict) -> str:
    for k in ("command", "file_path", "notebook_path", "path", "url", "pattern"):
        if isinstance(inp.get(k), str) and inp[k]:
            return "%s: %s" % (name, inp[k])
    return name


def last_activity(src) -> tuple[str, str]:
    """(the last tool call "Bash: mvn test", Claude's last answer) from the transcript."""
    tool, answer = "", ""
    for rec in (digest._records(*src) if src else ()):
        msg = rec.get("message")
        if rec.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            continue
        for c in msg["content"]:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and isinstance(c.get("input"), dict):
                tool = _tool_text(str(c.get("name") or "?"), c["input"])
            elif c.get("type") == "text" and str(c.get("text") or "").strip():
                answer = str(c["text"]).strip()
    return scrub(tool[:20000]), scrub(answer[:20000])   # cut first: scrub costs ~20 us/char


class Notifier:
    """One per piperd. `m` is the sessions.Manager (sessions, pending_list, _transcript_src,
    gui_visible_at); the opener is a test seam."""

    def __init__(self, m, opener=None):
        self.m = m
        self._stop = threading.Event()
        self._thread = None
        self.opener = opener
        self.lock = threading.Lock()
        st = _read(STATE_FILE)
        sent, deferred = st.get("sent"), st.get("deferred")
        self.sent: dict[str, float] = sent if isinstance(sent, dict) else {}
        # one-off messages held back by quiet hours or a Mattermost outage: [{text, at}]
        self.deferred: list[dict] = [d for d in deferred or [] if isinstance(d, dict) and isinstance(d.get("text"), str)
                                     and isinstance(d.get("at"), (int, float))] if isinstance(deferred, list) else []
        self.last_error = ""
        self.last_ok = 0.0

    def start(self) -> None:
        if INTERVAL > 0:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="piper-mm")
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(10)
            if self._thread.is_alive():
                raise RuntimeError("Mattermost notifier did not stop")

    def _loop(self) -> None:
        while not self._stop.wait(INTERVAL):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - Mattermost must never take piperd down
                self.last_error = "%s: %s" % (type(e).__name__, e)

    def _post(self, cfg: dict, text: str) -> None:
        payload = {"text": scrub(text[:20000])[:12000]}
        for k in ("channel", "username", "icon_url"):
            if cfg[k]:
                payload[k] = cfg[k]
        try:
            webhook_post(webhook_url(), payload, self.opener)
        except MMError as e:
            self.last_error = str(e)
            raise
        self.last_error, self.last_ok = "", time.time()

    def _save(self) -> None:
        """Under self.lock."""
        common.write_json(STATE_FILE, {"sent": self.sent, "deferred": self.deferred})

    def visible(self) -> bool:
        return time.time() - float(getattr(self.m, "gui_visible_at", 0.0) or 0.0) < VISIBLE_S

    # ------------------------------------------------------------ public
    def notify(self, text: str, force: bool = False, defer: bool = False) -> bool:
        """A one-off message (the morning production summary, 9.12 C). False when the webhook
        is not set up, or (without defer) in quiet hours unless force, or when the send failed.
        defer=True: quiet hours or a failed send keep it for the first tick after (≤ DEFERRED_MAX_AGE, 72 h) -
        True then. Never raises."""
        cfg = load_config()
        if not url_ok(webhook_url()) or not features.on("mattermost"):
            return False
        text = md_safe(text)   # built from production log titles; held back already safe
        if quiet_now(cfg) and not force:
            return self._defer(str(text), "quiet") if defer else False
        try:
            self._post(cfg, str(text))
        except MMError:
            return self._defer(str(text), "error") if defer else False
        audit({"event": "mm-notify", "via": "mattermost", "chars": len(str(text))})
        return True

    def _defer(self, text: str, why: str) -> bool:
        with self.lock:
            self.deferred = (self.deferred + [{"text": text, "at": time.time()}])[-5:]
            self._save()
        audit({"event": "mm-deferred", "via": "mattermost", "why": why, "chars": len(text)})
        return True

    # a summary held over a quiet weekend (Sat 07:30 -> Mon) or a long Mattermost outage is still
    # worth sending; older ones are dropped, with an audit entry
    DEFERRED_MAX_AGE = 3 * 86400

    def _flush_deferred(self, cfg: dict, now: float) -> None:
        with self.lock:
            todo = [d for d in self.deferred if now - d["at"] <= self.DEFERRED_MAX_AGE]
        for d in todo:
            try:
                self._post(cfg, d["text"])
            except MMError:
                return   # still down: the next tick
            with self.lock:
                self.deferred = [x for x in self.deferred if x is not d]
                self._save()
            audit({"event": "mm-notify", "via": "mattermost", "chars": len(d["text"]), "deferred": True})
        with self.lock:
            stale = [d for d in self.deferred if now - d["at"] > self.DEFERRED_MAX_AGE]
            if stale:
                self.deferred = [d for d in self.deferred if d not in stale]
                self._save()
        for d in stale:
            audit({"event": "mm-dropped", "via": "mattermost", "why": "stale", "chars": len(d["text"]),
                   "age_h": int((now - d["at"]) // 3600)})

    def status(self) -> dict:
        cfg = load_config()
        return {"configured": url_ok(webhook_url()), "mode": "webhook", "replies": False, "detail": cfg["detail"],
                "quiet": quiet_now(cfg), "visible": self.visible(), "last_error": self.last_error,
                "last_ok": self.last_ok, "webhook_file": WEBHOOK_FILE, "config_file": CONFIG_FILE,
                "note": "Incoming webhook: notifications only. Answering from Mattermost (👍/👎, text in the thread) "
                        "would need a bot account - step 2 is postponed."}

    def test(self) -> dict:
        cfg = load_config()
        if not url_ok(webhook_url()):
            raise ValueError("Mattermost is not set up: add the incoming webhook in Settings -> Mattermost "
                             "(or sh scripts/piper.sh mattermost-webhook < file; it lands in %s)" % WEBHOOK_FILE)
        try:
            self._post(cfg, "Piper: test message - notifications work.")
        except MMError as e:
            raise ValueError(str(e)) from None
        audit({"event": "mm-test", "via": "mattermost"})
        return self.status()

    # ------------------------------------------------------------ the tick
    def _waits(self) -> list[dict]:
        """Every prompt that waits for the human now; `key` identifies that one prompt."""
        out = []
        for s in list(self.m.sessions.values()):
            if s.hidden or s.pid is None:
                continue
            if s.state == "attention" and not s.idle:
                out.append({"key": "perm:%s:%s" % (s.id, getattr(s, "notif_ts", 0.0)), "kind": "permission",
                            "s": s, "since": s.since})
            elif s.state == "waiting" or (s.state == "attention" and s.idle):
                out.append({"key": "wait:%s:%s" % (s.id, getattr(s, "stop_ts", 0.0)), "kind": "waiting",
                            "s": s, "since": s.since})
        for p in self.m.pending_list():
            s = self.m.sessions.get(p.get("session", ""))
            if s is not None:
                out.append({"key": "confirm:%s" % p.get("id"), "kind": "confirm", "s": s,
                            "since": float(p.get("created") or 0), "summary": str(p.get("summary") or "")})
        return out

    def text(self, cfg: dict, w: dict, now: float) -> str:
        s = w["s"]
        mins = max(1, int((now - (w["since"] or now)) // 60))
        where = "Global agent" if s.kind == "advisor" else s.project
        # none = only the project and the state: no title either (a proposed session's title comes from a sandbox)
        head = "**%s** (%s)" % (where, s.id) if cfg["detail"] == "none" else "**%s** (%s)" % (md_safe(s.title), where)
        tool, answer = "", ""
        if cfg["detail"] != "none" and w["kind"] != "confirm":
            try:
                tool, answer = last_activity(self.m._transcript_src(s))
            except (OSError, AttributeError):
                pass
        cap = 3000 if cfg["detail"] == "full" else 300
        if w["kind"] == "permission":
            if cfg["detail"] == "none":
                what = "a tool"   # s.detail is the Notification's message: sandbox-written text
            else:
                # one code span: a line break could end it and let the rest render as markdown
                what = ("`%s`" % _MENTION.sub("@\u200b", " ".join(tool[:cap].replace("`", "'").splitlines()))
                        if tool else md_safe((s.detail or "a tool")[:200]))
            return "%s asks for permission: %s · waiting %d min" % (head, what, mins)
        if w["kind"] == "waiting":
            text = "%s is waiting for you · %d min" % (head, mins)
            if answer:
                a = answer if cfg["detail"] == "full" else answer.splitlines()[0]
                text += "\n> " + md_safe(a[:cap]).replace("\n", "\n> ")
            return text
        summ = md_safe(w.get("summary", "")) if cfg["detail"] != "none" else ""
        return "%s is waiting for your confirmation in Piper%s" % (head, (": " + summ[:cap]) if summ else "")

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        cfg = load_config()
        if not url_ok(webhook_url()) or quiet_now(cfg, now) or not features.on("mattermost"):
            return
        if self.deferred:
            self._flush_deferred(cfg, now)   # the morning summary held back by quiet hours
        if self.visible():
            return
        after = max(0, int(cfg["after_s"]))
        for w in self._waits():
            s, key = w["s"], w["key"]
            if key in self.sent or now - (w["since"] or now) < after:
                continue
            if now - float(self.sent.get("sid:" + s.id, 0)) < cfg["min_gap_s"]:
                continue
            try:
                self._post(cfg, self.text(cfg, w, now))
            except MMError:
                return   # Mattermost is down: the next tick tries again
            with self.lock:
                self.sent["sid:" + s.id] = self.sent[key] = now
                for k in [k for k, t in self.sent.items() if now - t > KEEP_S]:
                    del self.sent[k]
                self._save()
            audit({"event": "mm-sent", "via": "mattermost", "session": s.id, "kind": w["kind"]})
