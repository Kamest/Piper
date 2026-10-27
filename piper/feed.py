"""Feed: notifications and a chat with the Global agent in one panel.

A mixin of sessions.Manager, **off by default** (PIPER_HOME/feed/config.json `enabled`, the
Settings page). Off means inert: the tick returns at once (no GitLab CI calls, no file
writes), /api/state carries `feed: {on: false}` and the GUI looks as before.

Nothing is hooked into the places that change state: session state is set in several
places (_events, _watch, _on_exit, _connect), so the feed DERIVES everything from a
snapshot on its own tick:
  - actions ("for you"): recomputed from the current state every tick - a permission
    prompt, a confirmation card, a session waiting for you, L5, a failed start, a failed
    pipeline, a new assignment / mention in GitLab, new production errors. One per thing
    (key), gone when the reason is gone. Never stored.
  - events ("event"): the diff of two snapshots - a turn ended, a session was created /
    ended, a handoff result, new GitLab comments, CI passed again, an action was resolved.
    Appended to PIPER_HOME/feed/events-YYYY-MM.jsonl, coalesced per key within 10 min.
  - chat ("message"): the user's messages to the Global agent are queued here and typed into
    its terminal when it idles at an empty prompt (the handoff rules: waiting / idle_prompt,
    never a permission prompt, auto_type_ok). The chat view is the agent's own transcript
    (Manager.conversation), so the terminal and the chat never disagree. Notification text
    (a GitLab comment) is never typed into the agent: the GUI hands over references only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import threading
import time
import uuid

import common
import features
from common import audit, scrub

FEED_DIR = os.path.join(common.PIPER_HOME, "feed")
CONFIG_FILE = os.path.join(FEED_DIR, "config.json")   # written by the GUI Settings page (settings.py)
STATE_FILE = os.path.join(FEED_DIR, "state.json")     # seen, dismissed, muted
CHAT_FILE = os.path.join(FEED_DIR, "chat.json")
DEFAULTS = {"enabled": False, "retention_days": 30}
LIMITS = {"retention_days": (1, 365)}
LEGACY = ("wait_min",)   # removed 2026-10-08 (the queue lists a waiting session at once); an old config still loads
ALLOWED_FAILED_S = 1800.0   # B-N8-1: an allowed card whose action failed stays in the queue this long (or until dismissed)
TICK = float(os.environ.get("PIPER_FEED_TICK", "2"))   # 0: no thread (tests drive _feed_tick)
CI_EVERY = 120.0          # a project's pipeline at most this often (only projects with a live session)
COALESCE_S = 600.0        # the same event key within this = one line with a count
MAX_EVENTS = 3000         # in memory
MAX_CHAT = 200
MAX_CHAT_TEXT = 8000
MAX_TASK = 20000          # sessions.Manager.create: the longest start task
SENT_SHOWN_S = 1800.0     # a sent message not in the transcript yet is shown this long
CHAT_RETRY_S = 2.0       # a held (half-written) prompt is re-checked this often
LIVE = ("ready", "working", "waiting", "attention")
ACTIVE = ("preflight", "preparing", "spawning", "trust", "starting", "attesting", "ready", "working",
          "waiting", "attention", "stopping")
# GitLab reasons that are addressed to the human (inbox.ts HUMAN_REASONS)
HUMAN = ("directly_addressed", "mentioned", "assigned", "build_failed", "unmergeable", "review_requested",
         "approval_required")
# the order of open actions: what must not wait first
LEVELS = ("security", "permission", "confirm", "blocked", "waiting", "ci", "gitlab", "prod")
UNMUTABLE = ("security",)
CHAT_TAG = "[chat %s]"
CHAT_HINT = ("a message from the Feed panel of the Piper GUI; answer briefly, the answer shows in the chat. "
             "Items of the form (ref: …) are references only, read their content with your own tools.")
CHAT_RX = re.compile(r"^\[chat (m\d{1,9}(?:,m\d{1,9})*)\b[^\]\n]*\]\s*", re.S)


def _write(path: str, data) -> None:
    os.makedirs(FEED_DIR, mode=0o700, exist_ok=True)
    common.write_json(path, data)


# ------------------------------------------------------------ config
def validate(cfg) -> dict:
    """{enabled: bool, retention_days: 1..365}; missing keys take defaults, LEGACY keys are dropped."""
    if not isinstance(cfg, dict):
        raise ValueError("feed config must be an object")
    cfg = {k: v for k, v in cfg.items() if k not in LEGACY}
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        raise ValueError("unknown feed option(s): %s" % ", ".join(unknown))
    out = dict(DEFAULTS)
    if "enabled" in cfg:
        if not isinstance(cfg["enabled"], bool):
            raise ValueError("enabled must be true or false")
        out["enabled"] = cfg["enabled"]
    for k, (lo, hi) in LIMITS.items():
        if k in cfg:
            v = cfg[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ValueError("%s must be a whole number %d..%d" % (k, lo, hi))
            out[k] = v
    return out


def load_config() -> dict:
    """config.json, re-read on every use; an invalid key falls back to its default (off stays off)."""
    try:
        raw = common.read_json(CONFIG_FILE, {})
    except (OSError, ValueError):   # a hand-broken file: defaults (off), never a 500 of /api/state
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw:
            try:
                out[k] = validate({k: raw[k]})[k]
            except ValueError:
                pass
    out["enabled"] = features.on("agent")   # D10 (9.37): the Feed runs while the Global agent module is on; the file's key is legacy
    return out


# ------------------------------------------------------------ pure helpers (tested directly)
def _clip(text, n: int = 300, raw: bool = False) -> str:
    """One line of at most n characters, scrubbed - raw: GitLab text for the human (L1); actions and events
    reach only the GUI token's /api/state, Mattermost scrubs on its own."""
    t = " ".join((str(text or "") if raw else scrub(str(text or ""))).split())
    return t if len(t) <= n else t[:n - 1] + "…"


def session_snap(s) -> dict:
    """What the feed needs of a Session (an object with the Session attributes)."""
    h = s.handoff if isinstance(getattr(s, "handoff", None), dict) else {}
    return {"id": s.id, "kind": s.kind, "project": s.project, "title": s.title, "state": s.state,
            "detail": s.detail or "", "since": float(s.since or 0), "idle": bool(getattr(s, "idle", False)),
            "watch": list(s.watch or []), "rc": s.rc, "origin": s.origin, "parent": getattr(s, "parent", None),
            "handoff_at": float(h.get("at") or 0), "handoff_ok": bool(h.get("reported")),
            "blocks": len(s.blocks or []), "hidden": bool(s.hidden),
            "issue": (s.issue or {}).get("key") if isinstance(getattr(s, "issue", None), dict) else None}


def _subject_session(s: dict) -> dict:
    return {"type": "session", "key": s["id"], "label": s["title"], "session": s["id"], "project": s["project"]}


def _subject_issue(it: dict) -> dict:
    ref = "%s%s" % ("!" if it.get("type") == "mr" and it.get("forge") != "github" else "#", it.get("iid"))   # a GitHub PR is #n
    return {"type": it.get("type") or "issue", "key": it["key"], "label": "%s %s" % (it.get("project") or it.get("gl"), ref),
            "project": it.get("project") or ""}


def reason_waiting(s: dict) -> str:
    """Why a live session stands still: 'permission' (a permission prompt - nothing may be
    typed into it), 'idle' (waits for the next prompt) or '' (works)."""
    if s["state"] == "waiting" or (s["state"] == "attention" and s["idle"]):
        return "idle"
    if s["state"] == "attention" and (not s["watch"] or s["detail"] and not s["detail"].startswith("L5:")):
        return "permission"
    return ""


def derive_actions(sessions: list[dict], pending: list[dict], inbox_items: list[dict], prod: dict,
                   ci: dict, now: float, wait_s: float) -> list[dict]:
    """The open actions of this moment, ordered (LEVELS, then oldest first). Pure."""
    out: list[dict] = []
    by_id = {s["id"]: s for s in sessions}
    confirming = {p.get("session") for p in pending}

    def add(key, level, source, title, subject, since, actions, body="", quote=None):
        out.append({"key": key, "kind": "action", "level": level, "source": source, "title": title,
                    "body": body, "quote": quote, "subject": subject, "since": since, "actions": actions})

    for s in sessions:
        if s["hidden"] or s["kind"] == "prodcheck":
            continue
        subj = _subject_session(s)
        open_ = [{"op": "open", "label": "Open", "args": {"session": s["id"]}}]
        if s["watch"] and s["state"] in ACTIVE:
            add("l5:" + s["id"], "security", "session", "%s: a new sensitive file is not covered" % s["title"], subj,
                s["since"], open_, body=_clip(", ".join(s["watch"][:3])))
        if s["state"] == "error":
            add("err:%s:%d" % (s["id"], int(s["since"])), "blocked", "session",
                "%s: start failed" % s["title"], subj, s["since"], open_, body=_clip(s["detail"]))
            continue
        if s["state"] == "exited" and str(s.get("detail") or "").startswith(("Claude ended unexpectedly",
                                                                              "Codex ended unexpectedly")):
            add("crash:%s:%d" % (s["id"], int(s["since"])), "blocked", "session",
                "%s: %s" % (s["title"], s["detail"]), subj, s["since"], open_)
            continue
        if s["state"] == "blocked":
            add("blocked:%s:%d" % (s["id"], int(s["since"])), "blocked", "session",
                "%s: start blocked by the isolation check" % s["title"], subj, s["since"], open_,
                body="%d finding%s" % (s["blocks"], "" if s["blocks"] == 1 else "s"))
            continue
        if s["state"] == "trust":
            add("trust:" + s["id"], "permission", "session", "%s: hole asks whether you trust the project" % s["title"],
                subj, s["since"], open_)
            continue
        if s["kind"] == "advisor":
            continue   # the agent waiting at its prompt is its normal state; the chat shows it
        why = reason_waiting(s)
        if why == "permission":
            add("perm:%s:%d" % (s["id"], int(s["since"])), "permission", "session",
                "%s asks for permission" % s["title"], subj, s["since"], open_, body=_clip(s["detail"], 200))
        elif why == "idle" and s["id"] not in confirming and now - s["since"] >= wait_s:
            add("wait:%s:%d" % (s["id"], int(s["since"])), "waiting", "session", "%s is waiting for you" % s["title"],
                subj, s["since"], [{"op": "reply", "label": "Reply", "args": {"session": s["id"]}}] + open_)
    for p in pending:
        s = by_id.get(p.get("session") or "")
        subj = _subject_session(s) if s else {"type": "session", "key": p.get("session") or "", "label": p.get("title") or ""}
        add("p:" + str(p["id"]), "confirm", "confirm", "%s: %s" % (p.get("title") or "", KIND_TEXT.get(p.get("kind"), p.get("kind"))),
            subj, float(p.get("created") or now),
            [{"op": "decide", "label": "Enter passphrase" if p.get("kind") == "http_cert_passphrase" else ("Open again" if p.get("payload", {}).get("state") == "waiting" else "Open sign-in") if p.get("kind") == "http_auth_signin" else "Allow", "args": dict({"id": p["id"], "ok": True},
                                                             **({"prompt": True, "waiting": p.get("payload", {}).get("state") == "waiting"} if p.get("kind") in ("http_cert_passphrase", "http_auth_signin") else {}),
                                                             **({"danger": p.get("kind")} if _danger(p) else {}))}]
            + ([{"op": "decide", "label": str(p["always"]), "args": {"id": p["id"], "ok": True, "always": True}}]
               if p.get("always") else [])
            + ([{"op": "decide", "label": str(p["session_always"]),
                 "args": dict({"id": p["id"], "ok": True, "session_always": True},
                              **({"danger": p.get("kind")} if _danger(p) else {}))}]
               if p.get("session_always") else [])
            # W13: one word for every card, the DB write card too (S3's "Decline" is gone)
            + [{"op": "decide", "label": "Deny",
                "args": {"id": p["id"], "ok": False}}],
            body=str(p.get("summary") or "")[:70000])   # what is approved is what is shown (a diff of edit_file)
    for key, c in sorted(ci.items()):
        if c.get("status") == "failed":
            s = next((x for x in sessions if x["project"] == c.get("project") and x["kind"] == "project"
                      and not x["hidden"]), None)
            add("ci:%s:%s:%s" % (c.get("project"), c.get("branch"), c.get("id")), "ci", "ci",
                "CI %s failed" % c.get("project"),
                {"type": "project", "key": c.get("project") or key, "label": c.get("project") or key,
                 "project": c.get("project") or "", "session": s["id"] if s else None},
                float(c.get("at") or 0) or _midnight(now),
                ([{"op": "open", "label": "Open", "args": {"session": s["id"]}}] if s else [])
                + ([{"op": "url", "label": "Pipeline ↗", "args": {"url": c["url"]}}] if c.get("url") else []),
                body="%s · #%s" % (c.get("branch"), c.get("id")))
    for it in inbox_items:
        reasons = [r for r in it.get("reasons") or [] if r in HUMAN]
        if not (it.get("new") and reasons):
            continue
        todo = next(iter(it.get("todos") or []), None)
        quote = {"author": todo.get("author") or "", "text": _clip(todo.get("body"), 400, raw=True)} if todo and todo.get("body") else None
        add("gl:" + it["key"], "gitlab", "gitlab", "%s: %s" % (REASON_TEXT.get(reasons[0], reasons[0]), _clip(it.get("title"), 120, raw=True)),
            _subject_issue(it), _iso(it.get("updated_at")) or _midnight(now),   # stable: `now` would move the signature
            [{"op": "inbox", "label": "In the Inbox", "args": {"key": it["key"]}}], quote=quote)
    if prod.get("new"):
        add("prod:" + dt.date.fromtimestamp(now).isoformat(), "prod", "prod",
            "Production: %d %s" % (prod["new"], "new error" if prod["new"] == 1 else "new errors"),
            {"type": "prod", "key": "prod", "label": "Production"}, float(prod.get("report_at") or prod.get("at") or 0) or _midnight(now),   # prod_summary: report_at (B-N7-1)
            [{"op": "prod", "label": "Open", "args": {}}])
    out.sort(key=lambda a: (LEVELS.index(a["level"]), a["since"]))
    return out


KIND_TEXT = {"git_commit": "wants to commit", "git_push": "wants to push", "send_to_session": "wants to write to a session",
             "stop_session": "wants to stop a session", "propose_session": "proposes a new session",
             "spawn_session": "wants to start a session",
             "gitlab_comment": "wants to post a GitLab comment",
             "gitlab_issue_write": "wants to create or change a GitLab issue", "gitlab_mr_write": "wants to create or change a merge request",
             "gitlab_mr_merge": "wants to merge a merge request", "gitlab_job_play": "wants to start or retry a CI job",
             "gitlab_job_protected": "wants to start or retry a CI job on a PROTECTED branch", "gitlab_note_edit": "wants to edit a posted GitLab note",
             "gitlab_upload_file": "wants to upload a file attachment to GitLab",
             "github_comment": "wants to post a GitHub comment", "github_note_edit": "wants to edit a posted GitHub comment",
             "github_issue_write": "wants to create or change a GitHub issue",
             "github_pr_write": "wants to create or change a pull request",
             "github_job_log": "wants to read a failed GitHub Actions job's log",
             "git_run_project": "wants to run git in another project",
             "gitlab_job_log": "wants to read a failed CI job's log", "gitlab_note": "wants to post a GitLab comment",
             "git_run": "wants to run git", "write_exec": "wants to change a file the host runs (hook, build, IDE) in another project",
             "edit_file": "wants to edit another project", "write_file": "wants to write to another project",
             "gitlab_read": "wants to read another GitLab project",
             "http_env_set": "wants to update HTTP environment values",
             "http_runtime_file": "wants to send a file a script picked",
             "http_runtime_code": "wants to run code loaded at run time",
             "http_cert_passphrase": "needs a client certificate passphrase",
             "http_auth_signin": "wants you to sign in for an HTTP run",
             "properties_set": "wants to update properties",
             "http_dev": "wants to run HTTP requests on dev", "http_test": "wants to run HTTP requests on test",
             "http_prod": "wants to run HTTP requests on PRODUCTION",
             "http_host": "wants to call a host outside the environment",
             "db_write_dev": "wants to write to a dev database", "db_write_test": "wants to write to a TEST database",
             "db_write_prod": "wants to write to a PRODUCTION database", "db_read_test": "wants to read a test database",
             "db_read_prod": "wants to read a PRODUCTION database", "db_other": "wants a database of another project",
             "db_unmask": "wants to see masked personal data",
             "allow_domain": "wants to allow a network domain", "settings_change": "wants to change Piper's settings",
             "stack_mode": "wants to switch or set up the worktree's dev stack"}
# a card whose Allow must not be one keystroke or a green button (the GUI makes it red, `y` does nothing)
DANGER_KINDS = {"http_prod", "http_host", "db_write_prod", "db_read_prod", "db_unmask", "gitlab_job_protected", "write_exec"}


def _danger(p: dict) -> bool:
    """args.danger of a card's Allow: a DANGER_KINDS kind, a settings change that weakens the
    isolation (sessions.py marks payload.weakens), a git_run that takes a hidden file into git or an allow_domain of a
    name of this machine (payload.danger) - every client of the actions (the queue row, the Feed
    drawer) gets it, not only the floating card's own rule."""
    if p.get("kind") in DANGER_KINDS:
        return True
    pl = p.get("payload")
    if p.get("kind") in ("git_run", "git_run_project", "allow_domain", "http_runtime_file", "http_runtime_code") and isinstance(pl, dict) and pl.get("danger") is True:
        return True   # N7 (B-N6-1): it takes a hidden file's real content into git (sessions._git_run); N9: a domain
        # that names this machine (sessions._reaches_this_machine)
    return p.get("kind") == "settings_change" and isinstance(pl, dict) and pl.get("weakens") is True


REASON_TEXT = {"assigned": "Assigned", "mentioned": "Mentioned", "directly_addressed": "Addressed",
               "review_requested": "Review requested", "build_failed": "Build failed", "unmergeable": "MR cannot be merged",
               "approval_required": "Waiting for approval"}
ORIGIN_TEXT = {"advisor": "Global agent opened session %s", "inbox": "GitLab automation opened session %s",
               "handoff": "Proposed session %s is running", "prod": "Production investigation: session %s",
               "gui": "Session %s opened"}


def _midnight(now: float) -> float:
    d = dt.date.fromtimestamp(now)
    return dt.datetime(d.year, d.month, d.day).timestamp()


def _iso(v) -> float:
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def exit_text(s: dict) -> str:
    if s["state"] == "error":
        return "%s: error – %s" % (s["title"], _clip(s["detail"], 160))
    rc = s["rc"]
    if rc in (0, None):
        return "%s ended" % s["title"]
    if rc in (-15, 143, 15):
        return "%s was stopped" % s["title"]
    return "%s ended with code %s" % (s["title"], rc)


def diff_events(prev: dict | None, cur: dict, now: float) -> list[dict]:
    """Events between two snapshots {sessions: {id: snap}, inbox: {key: {new_notes, title,...}},
    ci: {key: {status,...}}, actions: {key: action}}. The first snapshot (prev None) is a
    baseline: nothing happened. Pure."""
    if prev is None:
        return []
    out: list[dict] = []

    def ev(key, source, title, subject, body=""):
        out.append({"key": key, "kind": "event", "source": source, "title": title, "body": body,
                    "subject": subject, "ts": now})

    ps, cs = prev.get("sessions") or {}, cur.get("sessions") or {}
    for sid, s in cs.items():
        if s["hidden"] or s["kind"] == "prodcheck":
            continue
        subj = _subject_session(s)
        was = ps.get(sid)
        if was is None:
            ev("new:" + sid, "agent" if s["origin"] == "advisor" else "session",
               ORIGIN_TEXT.get(s["origin"], ORIGIN_TEXT["gui"]) % s["title"], subj)
            continue
        if s["kind"] != "advisor" and was["state"] == "working" and s["state"] in ("waiting", "attention") and s["idle"]:
            ev("turn:" + sid, "session", "%s finished a turn" % s["title"], subj)
        if s["state"] in ("exited", "error") and was["state"] not in ("exited", "error"):
            ev("exit:" + sid, "session", exit_text(s), subj)
        if s["handoff_at"] and s["handoff_at"] != was["handoff_at"]:
            parent = cs.get(s["parent"] or "")
            ev("handoff:" + sid, "session", "%s %s%s" % (s["title"], "returned a result" if s["handoff_ok"] else
                                                          "ended without a result",
                                                          " for %s" % parent["title"] if parent else ""), subj)
    pi, ci_ = prev.get("inbox") or {}, cur.get("inbox") or {}
    for key, it in ci_.items():
        was = pi.get(key)
        n = int(it.get("new_notes") or 0)
        if not n:
            continue
        if was is not None and "last_note" in was:   # F-N10-8: news = a note id above what was known;
            # an old note made new again ("Mark as unread") or my own comment (last_note only) is not
            news = max(it.get("new_note_ids") or [0]) > int(was["last_note"] or 0)
        else:
            news = was is None or n > int(was.get("new_notes") or 0)
        if news:
            ev("notes:" + key, "gitlab", "%s: %d %s" % (_clip(it.get("title"), 100, raw=True), n,
                                                       "new comment" if n == 1 else "new comments"),
               _subject_issue(it))
    pc, cc = prev.get("ci") or {}, cur.get("ci") or {}
    for key, c in cc.items():
        was = pc.get(key)
        if was and was.get("status") == "failed" and c.get("status") == "success":
            ev("ci:" + key, "ci", "CI %s passes again" % c.get("project"),
               {"type": "project", "key": c.get("project") or key, "label": c.get("project") or key,
                "project": c.get("project") or ""}, body="%s · #%s" % (c.get("branch"), c.get("id")))
    pa, ca = prev.get("actions") or {}, cur.get("actions") or {}
    for key, a in pa.items():
        if key not in ca and a["level"] != "waiting":   # "waiting" ends with your prompt: no news
            ev("done:" + key, a["source"], "resolved: " + a["title"], a["subject"])
    return out


def coalesce(events: list[dict], new: dict, window: float = COALESCE_S) -> dict | None:
    """The existing event `new` folds into (same key, within window), updated; None = append."""
    for e in reversed(events[-200:]):
        if new["ts"] - e["ts"] > window:
            return None
        if e["key"] == new["key"]:
            e["n"] = int(e.get("n") or 1) + 1
            e["ts"] = new["ts"]
            e["title"] = new["title"]
            return e
    return None


def chat_wrap(ids: list[str], texts: list[str]) -> str:
    """The text typed into the agent: a tag with the message ids and the hint, then the messages."""
    return ("[chat %s – %s]\n" % (",".join(ids), CHAT_HINT)) + "\n\n---\n\n".join(texts)


def chat_turn_ids(text: str) -> list[str]:
    """The message ids of a user turn typed by chat_wrap; [] = typed in the terminal."""
    m = CHAT_RX.match(text or "")
    return m.group(1).split(",") if m else []


def muted_by(item: dict, muted: dict, now: float) -> bool:
    if item.get("level") in UNMUTABLE:
        return False
    subj = item.get("subject") or {}
    for k in (subj.get("key"), subj.get("project"), "src:" + str(item.get("source"))):
        if k and float(muted.get(k) or 0) > now:
            return True
    return False


# ------------------------------------------------------------ the mixin
class Feed:
    def _feed_init(self) -> None:
        self._feed_lock = threading.RLock()
        self._feed_prev: dict | None = None
        self._feed_actions: list[dict] = []
        self._feed_events: list[dict] = []
        self._feed_v = 0
        self._feed_sig = ""
        self._feed_ci: dict[str, dict] = {}          # project -> {status, branch, id, url, at}
        self._feed_ci_at: dict[str, float] = {}
        self._feed_inbox_v = None
        self._feed_inbox: list[dict] = []
        self._feed_state = self._feed_load_state()
        self._feed_chat: list[dict] = self._feed_load_chat()
        self._chat_sending = False
        self._chat_try = 0.0
        self._feed_loaded = False

    def _feed_start_thread(self) -> None:
        if TICK > 0:
            self._start_service(self._feed_loop, "piper-feed")

    def _feed_loop(self) -> None:
        while not self._stop_event.wait(TICK):
            try:
                self._feed_tick()
            except Exception as e:  # noqa: BLE001
                print("piperd: feed: %s" % e, flush=True)

    def feed_settings(self) -> dict:
        return load_config()

    # ------------------------------------------------------------ storage
    @staticmethod
    def _feed_load_state() -> dict:
        st = common.read_json(STATE_FILE, {})
        st = st if isinstance(st, dict) else {}
        return {"seen": float(st.get("seen") or 0) if isinstance(st.get("seen"), (int, float)) else 0.0,
                "dismissed": {k: v for k, v in (st.get("dismissed") or {}).items() if isinstance(k, str)},
                "muted": {k: float(v) for k, v in (st.get("muted") or {}).items()
                          if isinstance(k, str) and isinstance(v, (int, float))}}

    def _feed_save_state(self) -> None:
        now = time.time()
        with self._feed_lock:
            self._feed_state["muted"] = {k: v for k, v in self._feed_state["muted"].items() if v > now}
            _write(STATE_FILE, self._feed_state)

    @staticmethod
    def _feed_load_chat() -> list[dict]:
        rows = common.read_json(CHAT_FILE, [])
        return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)][-MAX_CHAT:] \
            if isinstance(rows, list) else []

    def _feed_save_chat(self) -> None:
        with self._feed_lock:
            _write(CHAT_FILE, self._feed_chat[-MAX_CHAT:])

    @staticmethod
    def _events_file(ts: float) -> str:
        return os.path.join(FEED_DIR, "events-%s.jsonl" % dt.date.fromtimestamp(ts).strftime("%Y-%m"))

    def _feed_load_events(self, days: int) -> None:
        cut = time.time() - days * 86400
        byid: dict[str, dict] = {}
        try:
            names = sorted(n for n in os.listdir(FEED_DIR) if re.match(r"^events-\d{4}-\d{2}\.jsonl$", n))
        except OSError:
            names = []
        keep = dt.date.fromtimestamp(cut).strftime("%Y-%m")
        for n in [x for x in names if x[7:14] < keep]:   # a whole month older than the retention
            try:
                os.unlink(os.path.join(FEED_DIR, n))
            except OSError:
                pass
        for n in [x for x in names if x[7:14] >= keep]:
            try:
                with open(os.path.join(FEED_DIR, n), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                            if isinstance(e, dict) and isinstance(e.get("id"), str) and float(e.get("ts") or 0) >= cut:
                                e["ts"] = float(e["ts"])
                                byid[e["id"]] = e   # a coalesced event is rewritten as a later line
                        except (ValueError, TypeError):   # a broken line never breaks the whole Feed
                            continue
            except OSError:
                continue
        with self._feed_lock:
            self._feed_events = sorted(byid.values(), key=lambda e: e["ts"])[-MAX_EVENTS:]

    def _feed_append(self, e: dict) -> None:
        os.makedirs(FEED_DIR, mode=0o700, exist_ok=True)
        fd = os.open(self._events_file(e["ts"]), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(e, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(fd)

    # ------------------------------------------------------------ the tick
    def _feed_ci_poll(self, live: list) -> None:
        """The pipeline of each project with a live project session, at most every CI_EVERY."""
        if not features.on("gitlab"):
            self._feed_ci.clear()
            self._feed_ci_at.clear()
            return
        now = time.time()
        for key in sorted({s.project for s in live if s.kind == "project" and s.gitlab}):
            if now - self._feed_ci_at.get(key, 0) < CI_EVERY:
                continue
            self._feed_ci_at[key] = now
            try:
                r = self.git_ci(key)
            except Exception:  # noqa: BLE001 - GitLab down / no token: no CI item, nothing else
                continue
            pipe = r.get("pipeline") if isinstance(r, dict) else None
            if isinstance(r, dict) and r.get("error"):
                continue   # GitLab failed this time: keep what was known (no spurious "resolved")
            if isinstance(pipe, dict) and pipe.get("status"):
                was = self._feed_ci.get(key) or {}
                same = was.get("id") == pipe.get("id") and was.get("status") == pipe.get("status")
                self._feed_ci[key] = {"project": key, "branch": r.get("branch"), "id": pipe.get("id"),
                                      "status": pipe.get("status"), "url": pipe.get("web_url") or "",
                                      "at": was.get("at") if same else now}
            else:
                self._feed_ci.pop(key, None)

    def _feed_inbox_items(self) -> list[dict]:
        try:
            v = self.inbox_summary().get("v")
        except Exception:  # noqa: BLE001
            return self._feed_inbox
        if v != self._feed_inbox_v:
            try:
                items = [{k: it.get(k) for k in ("key", "forge", "gl", "type", "iid", "title", "project", "reasons",
                                                 "new", "new_notes", "todos", "updated_at")}
                         for it in self._ib_views()]
                with self._ib_lock:   # which notes are new and the newest known (diff_events: F-N10-8)
                    for it in items:
                        st = self._ib_state["items"].get(it["key"]) or {}
                        it["new_note_ids"] = list(st.get("new_note_ids") or [])
                        it["last_note"] = st.get("last_note")
                self._feed_inbox = items
                self._feed_inbox_v = v
            except Exception:  # noqa: BLE001
                pass
        return self._feed_inbox

    def _feed_tick(self, now: float | None = None) -> None:
        cfg = load_config()
        if not cfg["enabled"]:
            if self._feed_prev is not None:   # switched off: the next switch-on starts from a new baseline
                with self._feed_lock:
                    self._feed_prev, self._feed_actions, self._feed_sig = None, [], ""
            return
        today = dt.date.today().isoformat()
        if self._feed_loaded != today:   # once a day: the retention also applies to a long-running piperd
            self._feed_load_events(cfg["retention_days"])
            self._feed_loaded = today
        now = time.time() if now is None else now
        sess = [x for x in list(self.sessions.values())]
        self._feed_ci_poll([x for x in sess if x.state in LIVE])
        snaps = [session_snap(x) for x in sess]
        inbox_items = self._feed_inbox_items() if features.on("gitlab") or features.on("github") else []
        try:
            prod = self.prod_summary()
        except Exception:  # noqa: BLE001
            prod = {}
        actions = derive_actions(snaps, self.pending_list(), inbox_items, prod, dict(self._feed_ci), now, 0)
        cur = {"sessions": {s["id"]: s for s in snaps},
               "inbox": {it["key"]: it for it in inbox_items},
               "ci": {k: dict(c) for k, c in self._feed_ci.items()},
               "actions": {a["key"]: a for a in actions}}
        with self._feed_lock:
            fresh = diff_events(self._feed_prev, cur, now)
            self._feed_prev = cur
            changed = False
            for e in fresh:
                folded = coalesce(self._feed_events, e)
                if folded is None:
                    e["id"] = "e" + hashlib.sha1(("%s|%f|%d" % (e["key"], now, len(self._feed_events))).encode()).hexdigest()[:12]
                    e["n"] = 1
                    self._feed_events.append(e)
                    folded = e
                self._feed_append(folded)
                changed = True
            del self._feed_events[:-MAX_EVENTS]
            sig = hashlib.sha1(json.dumps(actions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if sig != self._feed_sig:
                self._feed_sig = sig
                changed = True
            self._feed_actions = actions
            if changed:
                self._feed_v += 1
        if changed:
            self.bump()

    def feed_auto(self, s, kind: str, summary: str) -> None:
        """An action Settings -> Approvals let run without a card: into the feed, so what the agent
        did on its own is seen afterwards (audit.log alone is read by nobody). Same-kind events of a
        session fold into one with a count."""
        if not load_config()["enabled"]:
            return
        now = time.time()
        what = KIND_TEXT.get(kind, kind).replace("wants to ", "")
        e = {"key": "auto:%s:%s" % (kind, s.id), "kind": "event", "source": "session",
             "title": "%s: %s (no card)" % (s.title, what), "body": _clip(common.show_bidi(scrub(summary or "")), 400),   # as on a card (N3)
             "subject": _subject_session(session_snap(s)), "ts": now}
        with self._feed_lock:
            folded = coalesce(self._feed_events, e)
            if folded is not None:
                folded["body"] = e["body"]   # the latest action, not the first one folded in
            if folded is None:
                e["id"] = "e" + hashlib.sha1(("%s|%f|%d" % (e["key"], now, len(self._feed_events))).encode()).hexdigest()[:12]
                e["n"] = 1
                self._feed_events.append(e)
                folded = e
            self._feed_append(folded)
            del self._feed_events[:-MAX_EVENTS]
            self._feed_v += 1
        self.bump()

    def feed_allowed_failed(self, s, kind: str, error: str) -> None:
        """B-N8-1: a card the human allowed, whose action then failed (a new session at the session limit - the slot
        filled while the card waited) - the error went to the agent and audit.log only. A queue row (the queue is
        shown with or without the Feed; it goes after ALLOWED_FAILED_S or when dismissed) and, with the Feed on,
        an event."""
        what = KIND_TEXT.get(kind, kind).replace("wants to ", "").replace("proposes ", "")
        self._blocked_row(s, "allowed-failed:%s" % kind, "%s: allowed, but it could not %s" % (
            s.title, "start a session" if "session" in what else what), error)

    def feed_net_failed(self, s, argv: list, failed: list, trash: list, error: str = "", restore=None) -> None:
        """N10 (N9 decision 1): a card-free git_run changed hidden files and the net could not put them back (no
        intact over-mounted directory to write through) - the human learnt it from audit.log only. A queue row
        that stays until dismissed (the hidden file keeps the run's content), naming the files and the trash.
        restore (2026-10-10): [{id, path}] - the trash entries of the originals of the failed files, the row's
        Restore (fs_restore replace); the row goes once each of them is back and no failed file lacks one."""
        body = "git %s changed files hidden from the agent; Piper could not put them back: %s.\n%s" % (
            shlex.join([str(a) for a in argv][:20]), "(%s)" % error if error else ", ".join(failed[:10]),
            "The originals and what the run left there are in Piper's trash (%s: %s)." % (
                os.path.join(common.PIPER_HOME, "trash"), ", ".join(trash[:10])) if trash else
            "Nothing went to Piper's trash - check these files by hand.")
        restore = [{"id": str(e["id"]), "path": str(e["path"])} for e in restore or []][:20]
        self._blocked_row(s, "net-failed", "%s: hidden files not put back after git" % s.title, body, sticky=True,
                          extra={"trash": restore, "project": s.project,
                                 "unrestorable": len(set(failed)) - len(restore) if not error else 1})

    def feed_net_restored(self, trash_id: str) -> None:
        """A trash entry of a net-failed row is back (fs_restore): off the row; the row goes with its last one."""
        changed = False
        with self._feed_lock:
            rows = getattr(self, "_allowed_failed", [])
            for x in list(rows):
                left = [e for e in x.get("trash") or [] if e["id"] != trash_id]
                if len(left) == len(x.get("trash") or []):
                    continue
                changed = True
                x["trash"] = left
                if not left and x.get("unrestorable", 0) <= 0:
                    rows.remove(x)
        if changed:
            self.bump()

    def _blocked_row(self, s, kind: str, title: str, body: str, sticky: bool = False, extra=None) -> None:
        now = time.time()
        row = {"key": "%s:%s:%s" % (kind, s.id, uuid.uuid4().hex), "session": s.id, "ts": now,
               "title": title, "body": _clip(common.show_bidi(scrub(body or "")), 400), "sticky": sticky, **(extra or {})}
        with self._feed_lock:
            self._allowed_failed = [x for x in getattr(self, "_allowed_failed", [])
                                    if x.get("sticky") or x["ts"] > now - ALLOWED_FAILED_S][-19:]
            self._allowed_failed.append(row)
            if load_config()["enabled"]:
                e = {"key": row["key"], "kind": "event", "source": "session", "title": row["title"], "body": row["body"],
                     "subject": _subject_session(session_snap(s)), "ts": now, "n": 1,
                     "id": "e" + hashlib.sha1(("%s|%f|%d" % (row["key"], now, len(self._feed_events))).encode()).hexdigest()[:12]}
                self._feed_events.append(e)
                self._feed_append(e)
                del self._feed_events[:-MAX_EVENTS]
                self._feed_v += 1
        self.bump()

    # ------------------------------------------------------------ API
    def feed_summary(self) -> dict:
        """/api/state: {on: false} when off (constant: the ETag does not move)."""
        if not load_config()["enabled"]:
            return {"on": False}
        now = time.time()
        with self._feed_lock:
            st = self._feed_state
            open_ = [a for a in self._feed_actions if not self._feed_hidden(a, now)]
            unread = sum(1 for e in self._feed_events if e["ts"] > st["seen"] and not muted_by(e, st["muted"], now))
            queued = sum(1 for m in self._feed_chat if m.get("state") == "queued")
            return {"on": True, "v": self._feed_v, "open": len(open_), "unread": unread, "chat_queued": queued}

    def queue_summary(self) -> dict:
        """/api/state `queue`: the open actions of this moment for the GUI's queue (Overview),
        with or without the Feed. Derived at request time (derive_actions is pure and cheap)
        with no wait threshold: a session that waits is in the queue at once. With the Feed on,
        its dismissed / muted items stay hidden and its CI results are included; off, CI is
        empty (the Feed alone polls pipelines). Keys match the Feed's, so e / m work on them."""
        now = time.time()
        try:
            actions = self._queue_all(now)
        except Exception as e:  # noqa: BLE001 - /api/state must never fail over the queue: it degrades to empty
            print("piperd: queue: %s" % e, flush=True)
            return {"actions": [], "error": str(e)[:200]}
        with self._feed_lock:   # B-N9-1: a dismissal holds with the Feed off too (the queue's e / Dismiss)
            dismissed = sum(1 for a in actions if self._feed_state["dismissed"].get(a["key"]) is not None)
            if not load_config()["enabled"]:
                return {"actions": [a for a in actions if self._feed_state["dismissed"].get(a["key"]) is None]}
            return {"actions": [a for a in actions if not self._feed_hidden(a, now)], "dismissed": dismissed}

    def _queue_all(self, now: float) -> list[dict]:
        snaps = [session_snap(x) for x in list(self.sessions.values())]
        try:
            prod = self.prod_summary()
        except Exception:  # noqa: BLE001
            prod = {}
        with self._feed_lock:
            ci = dict(self._feed_ci) if load_config()["enabled"] and features.on("gitlab") else {}
        if not features.on("prod"):   # a module that is off brings nothing into the queue (9.20)
            prod = {}
        inbox = self._feed_inbox_items() if features.on("gitlab") or features.on("github") else []
        out = derive_actions(snaps, self.pending_list(), inbox, prod, ci, now, 0)
        with self._feed_lock:   # B-N8-1: an allowed card whose action then failed, for ALLOWED_FAILED_S
            failed = [x for x in getattr(self, "_allowed_failed", []) if x.get("sticky") or x["ts"] > now - ALLOWED_FAILED_S]
        by_id = {x["id"]: x for x in snaps}
        for x in failed:
            sn = by_id.get(x["session"])
            out.append({"key": x["key"], "kind": "action", "level": "blocked", "source": "session", "title": x["title"],
                        "body": x["body"], "quote": None, "since": x["ts"],
                        "subject": _subject_session(sn) if sn else {"type": "session", "key": x["session"], "label": ""},
                        "actions": [{"op": "open", "label": "Open", "args": {"session": x["session"]}}] if sn else [],
                        **({"trash": [dict(e) for e in x["trash"]], "project": x.get("project", "")} if x.get("trash") else {})})
        out.sort(key=lambda a: (LEVELS.index(a["level"]), a["since"]))
        return out

    def _feed_hidden(self, a: dict, now: float) -> bool:
        st = self._feed_state
        return st["dismissed"].get(a["key"]) is not None or muted_by(a, st["muted"], now)

    def feed(self, limit: int = 300) -> dict:
        cfg = load_config()
        if not cfg["enabled"]:
            return {"on": False}
        now = time.time()
        with self._feed_lock:
            st = self._feed_state
            actions = [dict(a) for a in self._feed_actions if not self._feed_hidden(a, now)]
            dismissed = sum(1 for a in self._feed_actions if st["dismissed"].get(a["key"]) is not None)
            events = [dict(e) for e in self._feed_events if not muted_by(e, st["muted"], now)][-max(1, min(limit, 1000)):]
            return {"on": True, "v": self._feed_v, "actions": actions, "events": events, "seen": st["seen"],
                    "dismissed": dismissed, "muted": {k: v for k, v in st["muted"].items() if v > now}}

    def feed_seen(self, ts) -> dict:
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            raise ValueError("ts must be a number")
        with self._feed_lock:
            self._feed_state["seen"] = max(self._feed_state["seen"], min(float(ts), time.time()))
            self._feed_v += 1
        self._feed_save_state()
        self.bump()
        return {"seen": self._feed_state["seen"]}

    def feed_dismiss(self, key: str) -> dict:
        key = str(key or "")
        with self._feed_lock:
            a = next((x for x in self._feed_actions if x["key"] == key), None) \
                or next((x for x in self._queue_all(time.time()) if x["key"] == key), None)
            if a is None:
                raise KeyError(key)
            if a["level"] in UNMUTABLE:
                raise ValueError("a security item cannot be dismissed - resolve it")
            self._feed_state["dismissed"][key] = time.time()
            # keys carry the time of their state (wait:<sid>:<since>): old ones cannot come back
            if len(self._feed_state["dismissed"]) > 500:
                self._feed_state["dismissed"] = dict(sorted(self._feed_state["dismissed"].items(),
                                                            key=lambda kv: kv[1])[-300:])
            self._feed_v += 1
        self._feed_save_state()
        self.bump()
        return {"ok": True}

    def feed_undismiss(self) -> dict:
        """Bring back every dismissed action that is still open (a misclick must not lose one)."""
        with self._feed_lock:
            open_ = {a["key"] for a in self._feed_actions} | {a["key"] for a in self._queue_all(time.time())}
            back = [k for k in self._feed_state["dismissed"] if k in open_]
            for k in back:
                self._feed_state["dismissed"].pop(k, None)
            self._feed_v += 1
        self._feed_save_state()
        self.bump()
        return {"restored": len(back)}

    def feed_mute(self, key: str, minutes) -> dict:
        key = str(key or "")
        if not re.match(r"^[A-Za-z0-9_.:/#@!+ -]{1,240}$", key):
            raise ValueError("bad key")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 60 * 24 * 365:
            raise ValueError("minutes must be 0..525600 (0 = unmute)")
        with self._feed_lock:
            if minutes:
                self._feed_state["muted"][key] = time.time() + minutes * 60
            else:
                self._feed_state["muted"].pop(key, None)
            self._feed_v += 1
        self._feed_save_state()
        self.bump()
        return {"ok": True}

    # ------------------------------------------------------------ chat with the Global agent
    def _chat_agent(self):
        """The live Global agent, else the newest one (its transcript is the chat history)."""
        ads = sorted([s for s in list(self.sessions.values()) if s.kind == "advisor" and not s.hidden],
                     key=lambda s: s.created)
        live = [s for s in ads if s.state in ACTIVE]
        return (live or ads or [None])[-1]

    def chat_send(self, text: str) -> dict:
        if not load_config()["enabled"]:
            raise ValueError("Feed is off (Settings → Feed)")
        text = str(text or "").strip()
        if not text or len(text) > MAX_CHAT_TEXT:
            raise ValueError("text must be 1..%d characters" % MAX_CHAT_TEXT)
        with self._feed_lock:
            n = 1 + max([int(m["id"][1:]) for m in self._feed_chat if re.match(r"^m\d+$", m["id"])] or [0])
            m = {"id": "m%d" % n, "text": text, "ts": time.time(), "state": "queued"}
            self._feed_chat.append(m)
            del self._feed_chat[:-MAX_CHAT]
            self._feed_v += 1
        self._feed_save_chat()
        audit({"event": "chat-send", "id": m["id"], "chars": len(text)})
        self.bump()
        a = self._chat_agent()   # delivered by the poll loop (_chat_flush), the handoff line's thread
        return dict(m, agent=bool(a is not None and a.state in ACTIVE))

    def chat_retry(self, mid: str) -> dict:
        """A sent message the agent never read (it ended first): back into the queue."""
        with self._feed_lock:
            m = next((x for x in self._feed_chat if x["id"] == mid), None)
            if m is None:
                raise KeyError(mid)
            s = self.sessions.get(m.get("session") or "")
            if m["state"] == "error":   # left unsubmitted in the prompt: the human asks for it again
                pass
            elif m["state"] != "sent" or (s is not None and s.state not in ("exited", "error") and not s.hidden):
                raise ValueError("message %s is not lost - nothing to resend" % mid)
            m["state"], m["ts"] = "queued", time.time()
            m.pop("session", None)
            m.pop("error", None)
            self._feed_v += 1
        self._feed_save_chat()
        self.bump()
        return {"ok": True}

    def chat_cancel(self, mid: str) -> dict:
        with self._feed_lock:
            m = next((x for x in self._feed_chat if x["id"] == mid), None)
            if m is None:
                raise KeyError(mid)
            if m["state"] not in ("queued", "error"):   # error: left unsubmitted in the prompt, not wanted any more
                raise ValueError("message %s is %s - only a queued or unsent one can be canceled" % (mid, m["state"]))
            m["state"] = "canceled"
            self._feed_v += 1
        self._feed_save_chat()
        self.bump()
        return {"ok": True}

    def chat_start(self) -> dict:
        """Start the Global agent with the queued messages as its task (typed after attestation)."""
        if not load_config()["enabled"]:
            raise ValueError("Feed is off (Settings → Feed)")
        a = self._chat_agent()
        if a is not None and a.state in ACTIVE:
            raise ValueError("Global agent is already running")
        with self._feed_lock:
            q = [m for m in self._feed_chat if m["state"] == "queued"]
        while len(q) > 1 and len(chat_wrap([m["id"] for m in q], [m["text"] for m in q])) > MAX_TASK:
            q.pop()   # create() refuses a longer task; the rest stays queued and is typed after its first turn
        s = self.create("advisor", chat_wrap([m["id"] for m in q], [m["text"] for m in q]) if q else "", origin="gui")
        self._chat_mark([m["id"] for m in q], "sent", s.id)
        audit({"event": "chat-start", "session": s.id, "messages": [m["id"] for m in q]})
        return {"session": s.id, "sent": [m["id"] for m in q]}

    def _chat_mark(self, ids: list[str], state: str, sid: str = "", error: str = "") -> None:
        with self._feed_lock:
            for m in self._feed_chat:
                if m["id"] in ids and m["state"] == "queued":
                    m["state"] = state
                    m["at"] = time.time()
                    if sid:
                        m["session"] = sid
                    if error:
                        m["error"] = error
            self._feed_v += 1
        self._feed_save_chat()
        self.bump()

    def _chat_flush(self, s) -> None:
        """Type the queued messages into an idle Global agent (poll-loop thread, like the handoff line)."""
        if s.kind != "advisor" or self._chat_sending:
            return
        with self._feed_lock:
            q = [m for m in self._feed_chat if m["state"] == "queued"]
        if not q or s.state not in ("waiting", "attention") or not s.idle or s.handoff_notify:
            return
        if time.time() - self._chat_try < CHAT_RETRY_S:
            return
        self._chat_try = time.time()
        if not load_config()["enabled"] or not self.auto_type_ok(s):   # a half-written prompt: retried later
            return
        self._chat_sending = True

        def send() -> None:
            try:
                with self._feed_lock:   # canceled meanwhile: not typed
                    todo = [m for m in q if m["state"] == "queued"]
                if not todo:
                    return
                ids = [m["id"] for m in todo]
                if self.type_text(s, chat_wrap(ids, [m["text"] for m in todo])):
                    self._chat_mark(ids, "sent", s.id)
                    audit({"event": "chat-deliver", "session": s.id, "messages": ids})
                else:   # the human typed in those 0.3 s: the text waits in the agent's prompt, unsubmitted
                    self._chat_mark(ids, "error", s.id, "left unsent in the agent's prompt - send it with Enter in the terminal, or Send again")
            except Exception as e:  # noqa: BLE001 - stays queued, retried
                print("piperd: chat %s: %s" % (s.id, e), flush=True)
            finally:
                self._chat_sending = False

        threading.Thread(target=send, daemon=True).start()

    def chat(self) -> dict:
        """The chat view: the agent's transcript turns (a user turn tagged by chat_wrap carries
        its message ids) and the queue."""
        if not load_config()["enabled"]:
            return {"on": False}
        a = self._chat_agent()
        turns: list[dict] = []
        note = ""
        if a is not None:
            try:
                conv = self.conversation(a.id, 400)
                turns = conv.get("turns") or []
                note = conv.get("note") or ""
            except (KeyError, OSError) as e:
                note = str(e)
        out = []
        for t in turns[-120:]:
            ids = chat_turn_ids(t.get("text") or "") if t.get("role") == "user" else []
            text = CHAT_RX.sub("", t.get("text") or "", count=1) if ids else t.get("text") or ""
            out.append({"role": t.get("role"), "text": text, "ts": t.get("ts"), "chat": ids,
                        "from_terminal": t.get("role") == "user" and not ids})
        read_by: dict[str, set] = {}

        def read_ids(sid: str) -> set:
            """The chat ids in a session's transcript (its last 400 turns, not only the 120 shown)."""
            if sid not in read_by:
                try:
                    ts_ = turns if a is not None and sid == a.id else (self.conversation(sid, 400).get("turns") or [])
                except (KeyError, OSError):
                    ts_ = []
                read_by[sid] = {i for t in ts_ if t.get("role") == "user" for i in chat_turn_ids(t.get("text") or "")}
            return read_by[sid]

        now = time.time()
        queue = []
        with self._feed_lock:
            rows = [dict(m) for m in self._feed_chat]
        for m in rows:   # outside the lock: read_ids may read another agent's transcript
            if m["state"] == "queued" or m["state"] == "error" and m["id"] not in read_ids(m.get("session") or ""):
                queue.append(m)   # an unsubmitted one the human then sent with Enter is in the transcript: gone
            elif m["state"] == "sent" and now - float(m.get("at") or 0) < SENT_SHOWN_S \
                    and m["id"] not in read_ids(m.get("session") or (a.id if a else "")):
                # typed (or given as a start task) but not in the transcript yet: never silently gone
                s = self.sessions.get(m.get("session") or "")
                ended = s is None or s.state in ("exited", "error") or s.hidden
                queue.append(dict(m, state="lost" if ended else "sent"))
        return dict({"on": True, "agent": {"id": a.id, "state": a.state, "title": a.title, "hold": a.hold} if a else None,
                     "turns": out, "queue": queue, "note": note}, **self._chat_context(a))

    def _chat_context(self, a) -> dict:
        """F21: what the chat shows around the messages - the sessions the agent started (origin "advisor"), its
        cards waiting for an answer in the queue, and the counts the empty chat's suggestions come from (assigned
        issues, sessions waiting for you, MRs to review). Cheap: no GitLab call, no git."""
        live = [x for x in list(self.sessions.values()) if not x.hidden and x.kind != "advisor"]
        started = [{"id": x.id, "title": x.title, "project": x.project, "state": x.state}
                   for x in sorted(live, key=lambda x: -float(x.created or 0)) if x.origin == "advisor"][:8]
        cards = []
        if a is not None:
            for p in self.pending_list():
                if p.get("session") == a.id:
                    pay = p.get("payload") if isinstance(p.get("payload"), dict) else {}
                    cards.append({"kind": str(p.get("kind") or ""), "project": str(pay.get("project") or "")})
        assigned = review = 0
        lock, items = getattr(self, "_ib_lock", None), getattr(self, "_ib_items", None)
        if lock is not None and isinstance(items, dict):
            with lock:
                for it in items.values():
                    r = it.get("reasons") or []
                    assigned += 1 if it.get("type") == "issue" and "assigned" in r else 0
                    review += 1 if it.get("type") == "mr" and ("review_requested" in r or "approval_required" in r) else 0
        waiting = sum(1 for x in live if x.state in ("attention", "trust", "waiting", "blocked"))
        return {"started": started, "cards": cards, "context": {"assigned": assigned, "waiting": waiting, "review": review}}
