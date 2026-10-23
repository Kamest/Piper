"""Timesheet: blocks of the USER's attention per issue.

A mixin of sessions.Manager. Overview only: nothing is written to GitLab or any other
system - the GUI copies text for the user to retype into the company's own tool.

Time is not the agents' running time (they work in parallel): it is built from the
user's own events - a prompt, a reply from a card, a confirmation, a commit, a GitLab
comment, a GUI edit, the GUI's focus on a session while the user is active - joined into
blocks per issue when the gap is < gap_min. A block spans first..last event, plus pad_min
on each side (default 0: the report must not inflate). Where blocks of several issues
overlap, the time is split by their event counts, so a day never holds more than it had.

Sources, read lazily when the view is opened:
  - PIPER_HOME/timesheet/events.jsonl: append-only, one line per prompt / focus event
    (ts, kind, sid, project, issue key + title - never a prompt's or a file's text), so
    the report survives a deleted session record;
  - the sessions' own `turns` (prompt times) and audit.log (sends, confirmations, commits,
    comments, GUI writes);
  - git: the user's own commits (user.email) in every project, also from IntelliJ; the
    issue comes from the branch (`123-fix`) or `#123` in the subject.
Options live in PIPER_HOME/timesheet/config.json ({enabled, gap_min, pad_min}; the GUI
Settings page writes it, `validate` checks it), re-read on every use, so a save applies
without a restart. Recording (events.jsonl) runs only with `enabled`; the view only reads.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import threading
import time

import common
import extensions
import features
import gitops
import projects
from common import scrub

TS_DIR = os.path.join(common.PIPER_HOME, "timesheet")
EVENTS_FILE = os.path.join(TS_DIR, "events.jsonl")
CONFIG_FILE = os.path.join(TS_DIR, "config.json")   # written by the GUI Settings page (settings.py)
ASSIGN_FILE = os.path.join(TS_DIR, "assign.json")
DEFAULTS = {"enabled": False, "gap_min": 15, "pad_min": 0}
LIMITS = {"gap_min": (1, 240), "pad_min": (0, 60)}
EVENT_MAX_AGE = 120.0     # an older hook event is a replay (piperd restarted)
DUP_S = 20.0              # a turn and its prompt event closer than this are one prompt (clock drift, poll delay)
FOCUS_EVERY = 50.0        # at most one focus event per session and this many seconds
GIT_TTL = 120.0
AUDIT_MAX_BYTES = 32 << 20
KEY_RX = re.compile(r"^[A-Za-z0-9_.\-/]{1,200}#(issue|mr)#\d{1,9}$")
WEEK_RX = re.compile(r"^(\d{4})-W(\d{2})$")
DAY_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# audit events that are the user's own action, and what they carry
AUDIT_SESSION = ("gui-send", "confirm", "git-commit", "git-push")
AUDIT_KEY = ("gui-comment", "gui-inbox-start", "gui-todo-done")
AUDIT_PROJECT = ("gui-write", "gui-mr")
KIND_LABEL = {"git-push": "push", "gui-comment": "GitLab comment", "gui-mr": "MR"}


# ------------------------------------------------------------ pure helpers (tested directly)
def week_of(ts: float) -> str:
    y, w, _ = dt.date.fromtimestamp(ts).isocalendar()
    return "%04d-W%02d" % (y, w)


def week_days(week: str) -> tuple[list[str], float, float]:
    """(7 local YYYY-MM-DD, start ts, end ts) of an ISO week `YYYY-Www`; ValueError else."""
    m = WEEK_RX.match(week or "")
    if not m:
        raise ValueError("week must look like 2026-W40")
    mon = dt.date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    days = [mon + dt.timedelta(days=i) for i in range(8)]
    stamps = [time.mktime(d.timetuple()) for d in days]   # local midnights (DST-safe)
    return [d.isoformat() for d in days[:7]], stamps[0], stamps[7]


def day_of(ts: float) -> str:
    return dt.date.fromtimestamp(ts).isoformat()


def blocks(events: list[dict], gap: float, pad: float) -> list[dict]:
    """Blocks per key: events of one key closer than `gap` join; a block spans first-pad ..
    last+pad. events: {ts, key}. Returns [{key, start, end, n, ts:[...]}] sorted by start."""
    by: dict[str, list[float]] = {}
    for e in events:
        by.setdefault(e["key"], []).append(float(e["ts"]))
    res = []
    for key, tss in by.items():
        tss.sort()
        start = 0
        for i in range(1, len(tss) + 1):
            if i == len(tss) or tss[i] - tss[i - 1] >= gap:
                c = tss[start:i]
                res.append({"key": key, "start": c[0] - pad, "end": c[-1] + pad, "n": len(c), "ts": c})
                start = i
    res.sort(key=lambda b: (b["start"], b["key"]))
    return res


def allocate(blks: list[dict], cuts: list[float]) -> dict:
    """Seconds per (key, day index) and per block. Elementary intervals between all block
    edges and the `cuts` (local midnights, cuts[i] = start of day i); where blocks overlap,
    each gets the interval in proportion to its event count. Sets blk["seconds"]."""
    edges = sorted({b["start"] for b in blks} | {b["end"] for b in blks} | set(cuts))
    per: dict[tuple, float] = {}
    for b in blks:
        b["seconds"] = 0.0
    for a, z in zip(edges, edges[1:]):
        if z <= a:
            continue
        cover = [b for b in blks if b["start"] <= a and b["end"] >= z]
        if not cover:
            continue
        di = None
        for i in range(len(cuts) - 1):
            if cuts[i] <= a < cuts[i + 1]:
                di = i
                break
        if di is None:
            continue
        total = sum(b["n"] for b in cover)
        for b in cover:
            part = (z - a) * b["n"] / total
            b["seconds"] += part
            per[(b["key"], di)] = per.get((b["key"], di), 0.0) + part
    return per


def attach_project_events(events: list[dict], gap: float) -> list[dict]:
    """An event with only a project (a commit without an issue, a GUI edit) joins the issue
    of the same project whose event is nearest within `gap`; else it counts for `p:<project>`."""
    keyed: dict[str, list[tuple[float, str]]] = {}
    for e in events:
        if e.get("key"):
            keyed.setdefault(e.get("project") or "", []).append((float(e["ts"]), e["key"]))
    out = []
    for e in events:
        if e.get("key"):
            out.append(e)
            continue
        best = None
        for t, k in keyed.get(e.get("project") or "", []):
            d = abs(t - float(e["ts"]))
            if d < gap and (best is None or d < best[0]):
                best = (d, k)
        out.append(dict(e, key=best[1] if best else "p:%s" % (e.get("project") or "?")))
    return out


def issue_from_branch(ref: str) -> int | None:
    seg = (ref or "").rsplit("/", 1)[-1]
    m = re.match(r"^(\d{1,9})(?:[-_]|$)", seg)
    return int(m.group(1)) if m else None


def issue_from_text(text: str) -> int | None:
    m = re.search(r"(?<![\w&])#(\d{1,9})\b", text or "")
    return int(m.group(1)) if m else None


def validate(cfg) -> dict:
    """The options of config.json, checked: {enabled: bool, gap_min: 1..240, pad_min: 0..60}.
    Missing keys take the defaults; a wrong type or range raises ValueError (for the
    Settings page, before it saves)."""
    if not isinstance(cfg, dict):
        raise ValueError("timesheet config must be an object")
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        raise ValueError("unknown timesheet option(s): %s" % ", ".join(unknown))
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


def _read_json(path: str, default):
    """common.read_json, but a broken file (bad JSON, not UTF-8) is the default too: the
    timesheet must never break the session event loop or the view."""
    try:
        return common.read_json(path, default)
    except (OSError, ValueError):
        return default


def load_config() -> dict:
    """config.json, re-read on every use; an unreadable or invalid file falls back to the
    defaults key by key; `enabled` = the timesheet module (D10)."""
    raw = _read_json(CONFIG_FILE, {})
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in raw:
            try:
                out[k] = validate({k: raw[k]})[k]
            except ValueError:
                pass
    out["enabled"] = features.on("timesheet")   # D10 (9.37): the module records; the file's `enabled` is legacy
    return out


def _num(v, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):   # 1e999 parses as inf
        return default
    return max(lo, min(hi, n))


class Timesheet:
    def _ts_init(self) -> None:
        self._ts_lock = threading.Lock()
        self._ts_focus: dict[str, float] = {}
        self._ts_git: dict = {}

    # ------------------------------------------------------------ settings
    def ts_settings(self) -> dict:
        return load_config()

    # ------------------------------------------------------------ recording
    def _ts_session_key(self, s, assign: dict | None = None) -> tuple[str | None, str]:
        """(issue key, title) of a session: an assignment in the view, the inbox item it was
        opened from, or `#123` in its title with the session's GitLab project."""
        a = (assign if assign is not None else self._ts_assign()).get(s.id)
        if isinstance(a, dict) and KEY_RX.match(str(a.get("key") or "")):
            return a["key"], str(a.get("title") or "")
        if isinstance(s.issue, dict) and KEY_RX.match(str(s.issue.get("key") or "")):
            return s.issue["key"], str(s.issue.get("title") or "")
        n = issue_from_text(s.title or "")
        if n and s.gitlab:
            return "%s#issue#%d" % (s.gitlab[0], n), ""
        return None, ""

    def ts_record(self, s, kind: str, ts: float | None = None) -> None:
        """One line to events.jsonl (only when enabled): WHEN and WHICH issue, never text."""
        if s is None or s.kind != "project" or not self.ts_settings()["enabled"]:
            return
        key, title = self._ts_session_key(s)
        rec = {"ts": round(float(ts if ts is not None else time.time()), 3), "kind": kind, "sid": s.id,
               "project": s.project, "key": key, "title": scrub(title)[:200]}
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with self._ts_lock:
            os.makedirs(TS_DIR, mode=0o700, exist_ok=True)
            fd = os.open(EVENTS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode())
            finally:
                os.close(fd)

    def _ts_auto_prompt(self, s, prompt: str) -> bool:
        """A prompt piperd typed itself (the task of an inbox / advisor / handoff session, a
        handoff note) is not the user's attention."""
        p = (prompt or "").strip()
        if p.startswith(("[piper]",) + tuple(extensions.items("AUTO_PROMPT_PREFIXES"))):   # + an extension's older ones
            return True
        if s.origin != "gui" and s.task:
            a, b = " ".join(p.split())[:80], " ".join(s.task.split())[:80]
            return bool(a) and (a.startswith(b[:len(a)]) or b.startswith(a))
        return False

    def ts_prompt_event(self, s, e: dict, human: bool | None = None) -> None:
        """A UserPromptSubmit hook event. The file is sandbox-written, so it only counts when the
        human sent it (`human`: they touched the prompt since the previous one - Manager._note_submit; None =
        an older ptyhost that cannot tell - counted as before). Replies from the card / Composer
        are counted through the audit (gui-send). Recorded at the host time (the container's
        clock may drift); its own time only tells a replay after a piperd restart."""
        try:
            ts = e.get("ts")
            now = time.time()
            if not isinstance(ts, (int, float)) or isinstance(ts, bool) or abs(now - float(ts)) > EVENT_MAX_AGE:
                return
            if human is False:
                return   # not the human's Enter: piperd typed it, or the sandbox wrote the line
            if self._ts_auto_prompt(s, str(e.get("prompt") or "")):
                return
            self.ts_record(s, "prompt", now)
        except (OSError, ValueError) as err:   # never breaks the event loop
            print("piperd: timesheet: %s" % err, flush=True)

    def ts_focus(self, sid: str) -> dict:
        """The GUI shows this session and the user is active (at most every FOCUS_EVERY s)."""
        s = self.get(sid)
        if not self.ts_settings()["enabled"]:
            return {"ok": True, "recorded": False}
        now = time.time()
        with self._ts_lock:
            if now - self._ts_focus.get(sid, 0.0) < FOCUS_EVERY:
                return {"ok": True, "recorded": False}
            self._ts_focus[sid] = now
        self.ts_record(s, "focus", now)
        return {"ok": True, "recorded": s.kind == "project"}

    # ------------------------------------------------------------ edits
    def _ts_assign(self) -> dict:
        a = _read_json(ASSIGN_FILE, {})
        return a if isinstance(a, dict) else {}

    def _ts_week_file(self, week: str) -> str:
        week_days(week)
        return os.path.join(TS_DIR, "%s.json" % week)

    def _ts_week(self, week: str) -> dict:
        w = _read_json(self._ts_week_file(week), {})
        w = w if isinstance(w, dict) else {}
        return {"edits": w.get("edits") if isinstance(w.get("edits"), dict) else {},
                "manual": [m for m in w.get("manual") or [] if isinstance(m, dict)]}

    def _ts_save_week(self, week: str, w: dict) -> None:
        os.makedirs(TS_DIR, mode=0o700, exist_ok=True)
        common.write_json(self._ts_week_file(week), w)

    def ts_edit(self, week: str, day: str, key: str, minutes, text) -> dict:
        """The user's own hours / text of one cell; a recomputation never overwrites it.
        minutes/text None = back to the computed value."""
        days, _, _ = week_days(week)
        if day not in days or not key or len(key) > 220:
            raise ValueError("unknown day or key")
        with self._ts_lock:
            w = self._ts_week(week)
            cell = {}
            if minutes is not None:
                cell["minutes"] = _num(minutes, 0, 1440, 0)
            if text is not None:
                cell["text"] = str(text)[:500]
            if cell:
                w["edits"]["%s|%s" % (day, key)] = cell
            else:
                w["edits"].pop("%s|%s" % (day, key), None)
            self._ts_save_week(week, w)
        return {"ok": True}

    def ts_manual(self, week: str, body: dict) -> dict:
        """Add / change / delete a manual row (a meeting, a call with the client)."""
        days, _, _ = week_days(week)
        with self._ts_lock:
            w = self._ts_week(week)
            rid = str(body.get("id") or "")
            rows = [m for m in w["manual"] if m.get("id") != rid] if rid else list(w["manual"])
            if not body.get("delete"):
                day = str(body.get("day") or "")
                label = str(body.get("label") or "").strip()[:200]
                if day not in days or not label:
                    raise ValueError("a day of the week and a label are required")
                minutes = _num(body.get("minutes"), 0, 1440, 0)
                if minutes < 1:   # -30 or nothing used to become a 0:00 row
                    raise ValueError("minutes must be 1..1440")
                rows.append({"id": rid or secrets.token_hex(4), "day": day, "label": label,
                             "minutes": minutes, "text": str(body.get("text") or "")[:500]})
            w["manual"] = rows
            self._ts_save_week(week, w)
        return {"ok": True}

    def ts_assign(self, sid: str, key: str, title: str = "") -> dict:
        """Assign a session to an issue (`group/project#123` or a full key); "" removes it."""
        key = (key or "").strip()
        m = re.match(r"^([A-Za-z0-9_.\-/]{1,200})#(\d{1,9})$", key)
        if m:
            key = "%s#issue#%s" % (m.group(1), m.group(2))
        if key and not KEY_RX.match(key):
            raise ValueError("expected group/project#123")
        if sid not in self.sessions and sid not in self._ts_sids():
            raise KeyError(sid)
        with self._ts_lock:
            a = self._ts_assign()
            if key:
                a[sid] = {"key": key, "title": str(title or "")[:200]}
            else:
                a.pop(sid, None)
            os.makedirs(TS_DIR, mode=0o700, exist_ok=True)
            common.write_json(ASSIGN_FILE, a)
        return {"ok": True}

    # ------------------------------------------------------------ the report
    def _ts_events_file(self, start: float, end: float) -> list[dict]:
        out = []
        try:
            with open(EVENTS_FILE, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        t = float(e.get("ts"))
                    except (ValueError, TypeError, AttributeError):
                        continue
                    if start <= t < end and isinstance(e.get("sid"), str):
                        out.append(e)
        except FileNotFoundError:
            pass
        return out

    def _ts_sids(self) -> dict:
        """sid -> {project, key, title} from events.jsonl (sessions whose record is gone)."""
        out = {}
        for e in self._ts_events_file(0, float("inf")):
            key = e.get("key")   # a hand-edited line must not break every week's view
            out[e["sid"]] = {"project": str(e.get("project") or ""), "key": key if isinstance(key, str) and KEY_RX.match(key) else None,
                             "title": str(e.get("title") or "")}
        return out

    def _ts_audit(self, start: float, end: float) -> list[dict]:
        out = []
        for r in common.audit_tail(limit=10 ** 9, max_bytes=AUDIT_MAX_BYTES):
            ev = r.get("event")
            if ev not in AUDIT_SESSION + AUDIT_KEY + AUDIT_PROJECT:
                continue
            if ev == "confirm" and (r.get("answered") is False or r.get("auto")):   # timed out / auto-approved: nobody was there
                continue
            try:
                t = dt.datetime.strptime(str(r.get("ts")), "%Y-%m-%dT%H:%M:%S%z").timestamp()
            except ValueError:
                continue
            if start <= t < end:
                out.append(dict(r, _t=t))
        return out

    def _ts_commits(self, start: float, end: float) -> list[dict]:
        """The user's own commits (user.email of each repository) in [start, end)."""
        out = []
        since = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(start))
        until = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(end))
        for p in projects.load(os.environ.get("PROJECT_PATH", "")).values():
            if not p.get("exists", True) or not gitops.head_is_plain(p["path"]):
                continue   # a planted .git/HEAD FIFO would block this view for git's whole timeout
            ck = (p["key"], start, end)
            hit = self._ts_git.get(ck)
            if hit and time.time() - hit[0] < GIT_TTL:
                out.extend(hit[1])
                continue
            rows = []
            try:
                top = self.repo_top(p["path"])
                if top:
                    email = gitops._git(top, "config", "user.email", ok=(0, 1)).strip()
                    if email:
                        raw = gitops._git(top, "log", "--all", "--source", "--since=" + since, "--until=" + until,
                                          "--fixed-strings", "--author=" + email, "--format=%ct%x09%S%x09%s")
                        gl = (p.get("gitlab") or [None])[0] or self.origin_gl(p["path"])
                        for line in raw.splitlines():
                            parts = line.split("\t", 2)
                            if len(parts) < 3:
                                continue
                            t = float(parts[0])
                            if not start <= t < end:
                                continue
                            n = issue_from_branch(parts[1]) or issue_from_text(parts[2])
                            rows.append({"ts": t, "kind": "commit", "project": p["key"],
                                         "key": "%s#issue#%d" % (gl, n) if (n and gl) else None,
                                         "subject": scrub(parts[2][:8192])[:120]})
            except (gitops.GitError, OSError, ValueError):
                pass
            self._ts_git[ck] = (time.time(), rows)
            out.extend(rows)
        return out

    def timesheet(self, week: str = "") -> dict:
        week = week or week_of(time.time())
        days, start, end = week_days(week)
        st = self.ts_settings()
        gap, pad = st["gap_min"] * 60.0, st["pad_min"] * 60.0
        lo, hi = start - gap - pad, end + gap + pad   # a block over the week boundary is read whole; allocate() clips it
        assign = self._ts_assign()
        titles: dict[str, str] = {}
        sid_info: dict[str, dict] = {}
        for sid, info in self._ts_sids().items():
            sid_info[sid] = dict(info)
        for sid, s in list(self.sessions.items()):
            if s.kind != "project":
                continue
            key, title = self._ts_session_key(s, assign)
            sid_info[sid] = {"project": s.project, "key": key, "title": title, "name": s.title}
        for sid, a in assign.items():
            if sid in sid_info and isinstance(a, dict) and KEY_RX.match(str(a.get("key") or "")):
                sid_info[sid].update(key=a["key"], title=a.get("title") or sid_info[sid].get("title") or "")
        for info in sid_info.values():
            if info.get("key") and info.get("title"):
                titles.setdefault(info["key"], info["title"])

        events: list[dict] = []
        notes: dict[tuple, list[str]] = {}   # (key, day, priority) -> text pieces
        turn_at: dict[str, list[float]] = {}   # sid -> turn starts: a turn and its events.jsonl prompt are one event

        def add(t: float, sid: str | None, project: str, key: str | None, kind: str, text: str = "") -> None:
            if sid and kind == "turn":
                turn_at.setdefault(sid, []).append(t)
            elif sid and kind == "prompt":
                # the turn carries the container's time, the prompt event the host's: the same prompt within DUP_S;
                # each turn pairs with one prompt event, and two real prompts (turns) are never merged
                ts_ = turn_at.get(sid, [])
                hit = next((x for x in ts_ if abs(t - x) <= DUP_S), None)
                if hit is not None:
                    ts_.remove(hit)
                    return
            events.append({"ts": t, "sid": sid, "project": project, "key": key, "kind": kind, "text": text})

        for sid, s in list(self.sessions.items()):   # first: the turns carry the prompt text
            if s.kind != "project":
                continue
            info = sid_info.get(sid, {})
            for tn in s.turns or []:
                t = tn.get("started")
                if tn.get("human") is False:   # no keystroke of the human before it (piperd typed it, or a forged hook line)
                    continue
                if isinstance(t, (int, float)) and lo <= t < hi and not self._ts_auto_prompt(s, str(tn.get("prompt") or "")):
                    add(float(t), sid, s.project, info.get("key"), "turn", str(tn.get("prompt") or ""))
        for e in self._ts_events_file(lo, hi):
            info = sid_info.get(e["sid"], {})
            add(float(e["ts"]), e["sid"], info.get("project") or str(e.get("project") or ""),
                info.get("key", e.get("key")), str(e.get("kind") or ""))
        for r in self._ts_audit(lo, hi):
            ev = r["event"]
            if ev in AUDIT_SESSION:
                sid = str(r.get("session") or "")
                info = sid_info.get(sid)
                if info is None:
                    continue
                add(r["_t"], sid, info.get("project") or "", info.get("key"), ev)
            elif ev in AUDIT_KEY:
                k = str(r.get("key") or "")
                if KEY_RX.match(k):
                    proj = ""
                    for info in sid_info.values():
                        if info.get("key") == k:
                            proj = info.get("project") or ""
                    add(r["_t"], None, proj, k, ev)
            else:
                add(r["_t"], None, str(r.get("project") or ""), None, ev)
        for c in self._ts_commits(lo, hi):
            add(c["ts"], None, c["project"], c["key"], "commit", c["subject"])

        for e in events:   # a session without an issue stays "no issue" (assign it in the view)
            if e["sid"] and not e["key"]:
                e["key"] = "p:%s" % (e["project"] or "?")
        events = attach_project_events(events, gap)   # commits / GUI edits without an issue
        for k in {e["key"] for e in events}:
            it = (getattr(self, "_ib_items", None) or {}).get(k)
            if isinstance(it, dict) and it.get("title"):
                titles.setdefault(k, str(it["title"]))
        blks = blocks(events, gap, pad)
        cuts = [time.mktime((dt.date.fromisoformat(d)).timetuple()) for d in days] + [end]
        per = allocate(blks, cuts)
        for e in events:   # a lone commit is a row with 0 h: the user adds the time, it is never invented
            for i in range(7):
                if cuts[i] <= e["ts"] < cuts[i + 1]:
                    per.setdefault((e["key"], i), 0.0)
        # texts: commit subjects first (the best description), then prompts, then markers
        for e in events:
            d = day_of(e["ts"])
            if e["kind"] == "commit" and e["text"]:
                notes.setdefault((e["key"], d, 0), []).append(e["text"])
            elif e["kind"] == "turn" and e["text"]:
                notes.setdefault((e["key"], d, 1), []).append(" ".join(e["text"].split())[:80])
            elif e["kind"] in KIND_LABEL:
                notes.setdefault((e["key"], d, 2), []).append(KIND_LABEL[e["kind"]])
        w = self._ts_week(week)
        rows: dict[str, dict] = {}
        for (key, di), sec in per.items():
            r = rows.setdefault(key, {"key": key, "title": titles.get(key, ""), "project": "", "days": {}, "sessions": []})
            day = days[di]
            pieces: list[str] = []
            for pri in (0, 1, 2):   # up to 3 of each: commits, prompts, markers
                uniq = [x for x in dict.fromkeys(notes.get((key, day, pri), [])) if x not in pieces]
                pieces += uniq[:3]
            cell = {"raw_min": round(sec / 60.0, 1), "auto_text": "; ".join(pieces)[:300],
                    "blocks": [{"start": b["start"], "end": b["end"], "n": b["n"], "min": round(b["seconds"] / 60.0, 1)}
                               for b in blks if b["key"] == key and b["start"] < cuts[di + 1] and b["end"] > cuts[di]]}
            ed = w["edits"].get("%s|%s" % (day, key))
            if isinstance(ed, dict):
                if "minutes" in ed:
                    cell["minutes"] = ed["minutes"]
                if "text" in ed:
                    cell["text"] = ed["text"]
            r["days"][day] = cell
        for ekey, ed in w["edits"].items():   # an edited cell whose computed time is gone stays
            day, _, key = ekey.partition("|")
            if day in days and key and isinstance(ed, dict) and day not in rows.get(key, {}).get("days", {}):
                r = rows.setdefault(key, {"key": key, "title": titles.get(key, ""), "project": "", "days": {}, "sessions": []})
                r["days"][day] = dict({"raw_min": 0.0, "auto_text": "", "blocks": []}, **ed)
        week_sids = {e["sid"] for e in events if start <= e["ts"] < end}   # not the margin read around the week
        for sid, info in sid_info.items():
            k = info.get("key") or "p:%s" % info.get("project")
            if k in rows and sid in week_sids:
                rows[k]["sessions"].append({"id": sid, "name": info.get("name") or sid, "assigned": sid in assign})
        for r in rows.values():
            if r["key"].startswith("p:"):
                r["project"] = r["key"][2:]
            else:
                for info in sid_info.values():
                    if info.get("key") == r["key"] and info.get("project"):
                        r["project"] = info["project"]
                if not r["project"]:
                    for e in events:
                        if e["key"] == r["key"] and e["project"]:
                            r["project"] = e["project"]
                            break
            r["title"] = scrub(r["title"])[:200]
        unassigned = [{"id": sid, "name": info.get("name") or sid, "project": info.get("project") or ""}
                      for sid, info in sid_info.items()
                      if not info.get("key") and sid in week_sids]
        order = sorted(rows.values(), key=lambda r: (r["key"].startswith("p:"),
                                                     -sum(c.get("minutes", c["raw_min"]) for c in r["days"].values())))
        return {"week": week, "days": days, "settings": st, "rows": order, "manual": w["manual"],
                "unassigned": unassigned,
                "prev": week_of(start - 86400), "next": week_of(end + 3600), "current": week_of(time.time())}
