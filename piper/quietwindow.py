"""Cooperative quiet windows on this Piper host; no process suspension or CPU guarantee.

State is durable before a grant or release is published. A restart, expired owner lease or
changed runtime requires human reconciliation: missing evidence never resumes competitors.
The admission lock orders requests and Piper-managed starts; it is not held during work.
"""
from __future__ import annotations

import copy
import functools
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager

import common
from ptyclient import PtyHostError

CONFIG_LOCK = threading.RLock()
FILE = os.path.join(common.PIPER_HOME, "quiet-window.json")
LIVE = {"preflight", "preparing", "spawning", "trust", "starting", "attesting", "ready", "working", "waiting", "attention", "stopping"}
TERMINAL = {"released", "cancelled"}
LEASE_S = 90
PROGRESS_STALE_S = 120
SETTLE_S = 3
MAX_QUEUE = 20
MAX_EVENTS = 500
MAX_WINDOW_EVENTS = 200
SCOPE = "Piper only asks its own sessions to wait. Apps and processes outside Piper may keep running."


def check_feature_change(modules: dict) -> None:
    """Do not hide a coordinator with outstanding holds, including after a daemon restart."""
    if modules.get("quiet") is False:
        doc = common.read_json(FILE, None) if os.path.exists(FILE) else {}
        if not isinstance(doc, dict) or doc.get("active") or doc.get("queue") or doc.get("starts"):
            raise ValueError("Release or cancel all quiet windows before turning the feature off.")


class QuietBlocked(ValueError):
    """Admission refused before a managed operation began."""


def managed(kind: str):
    """Register a real dispatch, not just a GUI click, so an in-flight operation blocks a grant."""
    def decorate(fn):
        @functools.wraps(fn)
        def run(self, *args, **kwargs):
            q = getattr(self, "quiet", None)
            if q is None:
                return fn(self, *args, **kwargs)
            sid = getattr(args[0], "id", "") if args else ""
            if kind == "tool" and len(args) > 1 and (args[1].startswith("quiet_") or args[1] in ("db_cancel", "http_auth_browser_closed") or
                    args[1] == "piper_session" and len(args) > 2 and isinstance(args[2], dict) and "quiet" in args[2]):
                return fn(self, *args, **kwargs)
            if kind == "start":
                # A request can win after create's final check but before its worker starts.
                # Keep that start queued; never re-run a function that already began spawning.
                while not self._stop_event.is_set():
                    if self._canceled(args[0]):
                        return None
                    ctx = q.operation(sid, kind)
                    try:
                        ctx.__enter__()
                    except QuietBlocked as e:
                        if args[0].phase != "quiet":
                            self._set(args[0], "preflight", "quiet", str(e))
                        self._stop_event.wait(0.4)
                        continue
                    try:
                        return fn(self, *args, **kwargs)
                    finally:
                        ctx.__exit__(None, None, None)
                return None
            with q.operation(sid, kind):
                return fn(self, *args, **kwargs)
        return run
    return decorate


class Coordinator:
    """One FIFO coordinator for this daemon. Participants are bound to runtime instances."""

    def __init__(self, sessions, enabled, *, path=FILE, clock=time.monotonic, wall=time.time, before_ack=None):
        self.sessions = sessions
        self.enabled = enabled
        self.path = path
        self.clock, self.wall = clock, wall
        self.before_ack = before_ack
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.operations = {}
        self.lease = self.settle = self.deadline = 0.0
        self.external = []
        self.time_anchor = (self.clock(), self.wall())
        self.doc = {"v": 1, "active": None, "queue": [], "starts": [], "last": None, "resumes": []}
        self._previous = self._requests(self.doc)
        if os.path.exists(path):
            raw = common.read_json(path, None)
            if not isinstance(raw, dict) or raw.get("v") != 1 or not isinstance(raw.get("queue"), list) or not isinstance(raw.get("resumes", []), list):
                raise ValueError("Quiet window state is invalid; restore it before starting Piper.")
            self.doc = raw
            self._previous = self._requests(raw)
            self.doc.setdefault("starts", [])
            legacy_resume = "resumes" not in self.doc
            self.doc.setdefault("resumes", [])
            recovered = False
            last = self.doc.get("last")
            if legacy_resume and last and last.get("phase") == "released":
                # Old controllers did not retain delivery receipts. Seed once on
                # upgrade; a duplicate continue notice only asks to check status.
                for p in [{"id": last["owner"], "generation": last["generation"]}] + last["participants"]:
                    p["resume"] = {"status": "pending", "detail": "Checking continue delivery after update"}
                    self.doc["resumes"].append({"window": last["id"], "session": p["id"], "generation": p["generation"], **p["resume"]})
                recovered = True
            for r in self.doc["starts"]:
                if r.get("phase") == "checking":
                    r.update(phase="uncertain", error="The controller restarted during this start. Check Overview before removing this record.")
                    recovered = True
            if self.doc.get("active"):
                self.doc["active"].update(phase="recovering", problem="Checking after a restart. Verify that the owner's work has ended before releasing.")
                recovered = True
            if recovered:
                self._save()

    @staticmethod
    def _requests(doc):
        return copy.deepcopy({r["id"]: r for r in
                              [doc.get("active"), doc.get("last"), *doc.get("queue", [])] if r})

    def _event(self, request, kind, *, participant=None, detail="", source="Piper"):
        """Append factual transitions only; state and events share one durable transaction."""
        subject = participant or {"id": request["owner"], "title": request.get("title", ""), "generation": request.get("generation", "")}
        self.doc["event_seq"] = self.doc.get("event_seq", 0) + 1
        self.doc.setdefault("events", []).append({
            "seq": self.doc["event_seq"], "at": self.wall(), "window": request["id"], "kind": kind,
            "session": subject["id"], "generation": subject.get("generation", ""),
            "title": common.scrub(common.no_bidi(str(subject.get("title") or subject["id"])))[:200],
            "detail": common.scrub(common.no_bidi(str(detail or "")))[:300], "source": source})

    def _record_changes(self):
        current = self._requests(self.doc)
        for rid, r in current.items():
            old = self._previous.get(rid, {})
            if not old:
                self._event(r, "requested", detail=r.get("reason"), source=r.get("requested_by", "agent"))
            if r.get("phase") != old.get("phase") or r.get("problem") != old.get("problem"):
                kind = "work_ended" if r["phase"] == "ready" and old.get("phase") == "working" else r["phase"]
                if kind == "released" and r.get("outcome") == "cancelled":
                    kind = "cancelled"
                self._event(r, kind, detail=r.get("problem", ""),
                            source=r.get("released_by", "agent") if kind in ("released", "cancelled") else "agent report" if kind in ("working", "work_ended") else "Piper")
            previous_parts = {p["id"]: p for p in old.get("participants", [])}
            for p in r.get("participants", []):
                prev = previous_parts.get(p["id"], {})
                if old and not prev:
                    self._event(r, "participant_added", participant=p, source="Piper observed")
                if p.get("delivered_at") and not prev.get("delivered_at"):
                    self._event(r, "delivered", participant=p, source="Piper observed")
                changed_report = any(p.get(k) != prev.get(k) for k in ("detail", "confirmed_by"))
                if p.get("status") in ("waiting", "unknown") and (p.get("status") != prev.get("status") or changed_report):
                    self._event(r, p["status"], participant=p, detail=p.get("detail"),
                                source=p.get("confirmed_by", "agent report") if p["status"] == "waiting" else "Piper observed")
                elif p.get("status") == "finishing" and p.get("detail") and p.get("detail") != prev.get("detail"):
                    self._event(r, "progress", participant=p, detail=p["detail"], source=p.get("confirmed_by", "agent report"))
                if old and prev and p.get("exempt") != prev.get("exempt") and p.get("exempt"):
                    self._event(r, "exempt_granted" if p["exempt"] == "allowed" else "exempt_revoked", participant=p,
                                detail="Window is shared, not quiet" if p["exempt"] == "allowed" else "Must stop its work and confirm again",
                                source="operator")
                resume, before = p.get("resume", {}), prev.get("resume", {})
                if resume and resume != before:
                    self._event(r, "resume_sent" if resume.get("status") == "sent" else "resume_pending",
                                participant=p, detail=resume.get("detail"), source="Piper observed")
            jobs, before_jobs = r.get("observed_jobs", []), old.get("observed_jobs", [])
            for job in before_jobs:
                if job not in jobs:
                    self._event(r, "job_ended", participant={"id": job["session"], "title": job.get("title", "Piper")},
                                detail=job["kind"], source="Piper observed")
            for job in jobs:
                if job not in before_jobs:
                    self._event(r, "job_running", participant={"id": job["session"], "title": job.get("title", "Piper")},
                                detail=job["kind"], source="Piper observed")
        events = self.doc.setdefault("events", [])
        dropped = self.doc.setdefault("event_dropped", {})
        counts = {}
        kept = []
        size = 0
        for e in reversed(events):
            rid = e["window"]
            counts[rid] = counts.get(rid, 0) + 1
            cost = len(json.dumps(e, ensure_ascii=False).encode())
            if counts[rid] > MAX_WINDOW_EVENTS or len(kept) >= MAX_EVENTS or size + cost > 196608:
                dropped[rid] = dropped.get(rid, 0) + 1
            else:
                kept.append(e)
                size += cost
        self.doc["events"] = list(reversed(kept))
        self.doc["event_dropped"] = {rid: n for rid, n in dropped.items() if rid in current}

    def _save(self):
        self._record_changes()
        common.write_json(self.path, self.doc)
        self._previous = self._requests(self.doc)
        with self.changed:
            self.changed.notify_all()

    @contextmanager
    def _transaction(self):
        """Rollback in memory on write failure: no unpersisted permission may escape."""
        before = copy.deepcopy(self.doc)
        timers = (self.lease, self.settle, self.deadline, self.time_anchor)
        try:
            yield
            self._save()
        except BaseException:
            self.doc = before
            self.lease, self.settle, self.deadline, self.time_anchor = timers
            raise

    def _rows(self):
        return {s.id: s for s in self.sessions()}

    @staticmethod
    def generation(s):
        return "%s:%s:%s" % (s.created, s.pid, getattr(s, "quiet_generation", ""))

    def _participant(self, s):
        return {"id": s.id, "generation": self.generation(s), "title": s.title, "created": s.created,
                "meta": str(getattr(s, "project", "")), "status": "finishing", "detail": "", "at": None,
                "delivered_at": None, "delivery_detail": "Waiting to send request"}

    def _active(self, rid):
        a = self.doc.get("active")
        if not a or a["id"] != rid:
            raise ValueError("This quiet window is no longer current. Refresh its status.")
        return a

    def _owner(self, a, sid):
        s = self._rows().get(sid)
        if a["owner"] != sid or not s or self.generation(s) != a["generation"]:
            raise ValueError("Only this quiet window's original owner can do that.")
        return s

    def _select(self, req):
        rows = self._rows()
        s = rows.get(req["owner"])
        if not s or s.state not in LIVE or self.generation(s) != req["generation"]:
            return False
        req.update(phase="draining", participants=[self._participant(x) for x in rows.values() if x.id != s.id and x.state in LIVE],
                   problem="", work=False, selected=self.wall(), lease_until=self.wall() + LEASE_S)
        self.doc["active"] = req
        self.lease, self.settle, self.deadline = self.clock() + LEASE_S, 0, 0
        self.time_anchor = (self.clock(), self.wall())  # a checked, newly selected window gets a fresh clock baseline
        return True

    def request(self, sid, reason, minutes=None, key="", *, operator=False):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
            raise ValueError("Describe the work in 1–300 characters.")
        if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in (10, 15, 30)):
            raise ValueError("Choose 10, 15 or 30 minutes.")
        if not isinstance(key, str) or len(key) > 100:
            raise ValueError("Invalid request key.")
        with CONFIG_LOCK, self.lock:
            if not self.enabled():
                raise ValueError("Quiet window is off in Settings → Features.")
            s = self._rows().get(sid)
            if not s or s.state not in ("ready", "working", "waiting", "attention"):
                raise ValueError("Choose a running session that has finished starting.")
            requests = ([self.doc["active"]] if self.doc.get("active") else []) + self.doc["queue"]
            last = self.doc.get("last")
            if key and last and last["owner"] == sid and last.get("key") == key and last["generation"] == self.generation(s):
                return self.snapshot()
            existing = next((x for x in requests if x["owner"] == sid), None)
            if existing:
                return self.snapshot()
            if len(self.doc["queue"]) >= MAX_QUEUE:
                raise ValueError("The quiet window queue is full.")
            req = {"id": uuid.uuid4().hex, "owner": sid, "generation": self.generation(s), "title": s.title,
                   "reason": common.scrub(common.no_bidi(reason.strip())), "minutes": minutes, "key": key,
                   "requested": self.wall(), "phase": "queued", "requested_by": "operator" if operator else "agent"}
            with self._transaction():
                if not self.doc.get("active"):
                    self._select(req)
                else:
                    self.doc["queue"].append(req)
            common.audit({"event": "quiet-request", "session": sid, "window": req["id"]})
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            doc = copy.deepcopy(self.doc)
            a = doc.get("active")
            if a:
                a["now"] = int(self.wall() // 15) * 15
                a["shared"] = any(p.get("exempt") for p in a["participants"])
                rows = self._rows()
                for p in a["participants"]:
                    p["stale"] = p["status"] == "finishing" and self.wall() - (p.get("at") or p.get("delivered_at") or a["selected"]) >= PROGRESS_STALE_S
                    s = rows.get(p["id"])
                    if s and p["status"] == "finishing":
                        counts = [(len(getattr(s, field, {}) or {}), name) for field, name in
                                  (("background_tasks", "background task"), ("subagents", "subagent"))]
                        p["activity"] = ", ".join("%d %s%s" % (n, name, "s" if n != 1 else "") for n, name in counts if n)
            return {"events": [], "event_dropped": {}, **doc, "enabled": self.enabled(), "scope": SCOPE,
                    "operations": [{"session": s, "kind": k} for s, k in self.operations.values()] + copy.deepcopy(self.external)}

    def blocked(self, sid=""):
        with self.lock:
            self.tick()
            a = self.doc.get("active")
            if not a:
                return ""
            owner = self._rows().get(sid)
            if sid == a["owner"] and owner and self.generation(owner) == a["generation"] and a["phase"] in ("ready", "working"):
                return ""
            if owner and self._exempt_ids(a, ("allowed",)).get(sid) == self.generation(owner):
                return ""   # the operator's exception: this runtime keeps working in a shared window
            return "Quiet window for %s: %s. Check quiet_status; finish existing work, acknowledge with quiet_ack, then quiet_wait." % (a["title"], a["phase"])

    @contextmanager
    def operation(self, sid, kind):
        token = uuid.uuid4().hex
        with self.lock:
            why = self.blocked(sid)
            if why:
                raise QuietBlocked(why)
            self.operations[token] = (sid, kind)
        try:
            yield
        finally:
            with self.lock:
                self.operations.pop(token, None)
                self.changed.notify_all()

    @staticmethod
    def _exempt_ids(a, states=("allowed", "returning")):
        """Participants whose own work no longer holds this window: {sid: generation}."""
        return {p["id"]: p["generation"] for p in a["participants"] if p.get("exempt") in states}

    def exempt(self, sid, rid, allow):
        """Operator only: let one running participant keep working in this window.

        The window becomes shared, not quiet. The exception is bound to this window and
        runtime; a restart or the next window never inherits it. Removing it holds the
        session again and only an ACK carrying the new revision restores the hold."""
        if not isinstance(allow, bool):
            raise ValueError("allow must be true or false")
        with self.lock:
            self.tick()
            a = self._active(rid)
            s = self._rows().get(sid)
            p = next((x for x in a["participants"] if x["id"] == sid), None)
            if not s or not p or s.state not in LIVE or p["generation"] != self.generation(s) or p["status"] == "unknown":
                raise ValueError("Choose a running session of this window. An ended or restarted session cannot get an exception.")
            if allow and p.get("exempt") == "allowed" or not allow and p.get("exempt") != "allowed":
                return self.snapshot()
            with self._transaction():
                p["rev"] = p.get("rev", 0) + 1
                if allow:
                    p.update(exempt="allowed", status="exempt", detail="Exception from the operator; keeps working", at=self.wall(), confirmed_by="operator")
                else:
                    p.update(exempt="returning", status="finishing", detail="Exception removed; stopping its work", at=self.wall(), confirmed_by="operator")
            common.audit({"event": "quiet-exempt", "session": sid, "window": rid, "allow": allow, "revision": p["rev"]})
            return self.snapshot()

    def delivery(self, sid, rid, detail, *, sent=False):
        """Record control delivery separately from the participant's own confirmation."""
        with self.lock:
            a = self.doc.get("active")
            if not a or a["id"] != rid:
                return
            p = next((p for p in a["participants"] if p["id"] == sid), None)
            s = self._rows().get(sid)
            if not p or not s or p["generation"] != self.generation(s) or p.get("delivered_at"):
                return
            if not sent and p.get("delivery_detail") == detail:
                return
            with self._transaction():
                p["delivery_detail"] = detail
                if sent:
                    p["delivered_at"] = self.wall()

    def ack(self, sid, rid, *, done=False, detail="", operator=False, revision=None):
        with self.lock:
            self.tick()
            a = self._active(rid)
            s = self._rows().get(sid)
            p = next((x for x in a["participants"] if x["id"] == sid), None)
            if not s or not p or p["generation"] != self.generation(s):
                raise ValueError("This session is not a participant of this window.")
            if p.get("exempt") == "allowed" and not operator:
                raise ValueError("This session has an exception from this window and may keep working. Do not confirm; check quiet_status.")
            if done and p.get("rev") and not operator and (isinstance(revision, bool) or revision != p["rev"]):
                # A confirmation sent before the exception changed must never restore the hold.
                raise ValueError("The exception for this session changed. Finish your work, then confirm with revision=%d from quiet_status." % p["rev"])
            if done and s.state not in ("ready", "working", "waiting", "attention"):
                raise ValueError("Wait until this session has finished starting before confirming.")
            if done and (any(x[0] == sid for x in self.operations.values()) or getattr(s, "subagents", {}) or getattr(s, "background_tasks", {})):
                raise ValueError("Finish all managed work, subagents and background tasks before confirming.")
            if not isinstance(detail, str) or len(detail) > 300:
                raise ValueError("Detail must be at most 300 characters.")
            if done and self.before_ack:
                # Close native admission before publishing the confirmation. A stale
                # cleanup hint must never authorize work after the owner receives ready.
                self.before_ack(s)
            with self._transaction():
                p.update(status="waiting" if done else "finishing", detail=common.scrub(common.no_bidi(detail)), at=self.wall(), confirmed_by="operator" if operator else "agent report")
                if done:
                    p.pop("exempt", None)
                # A returning exception was already working by the operator's choice;
                # its progress is expected and must not cost the owner the window.
                if not done and a["phase"] in ("ready", "working") and p.get("exempt") != "returning":
                    a.update(phase="recovering", problem="A participant resumed work. Check the work before continuing.")
            return self.snapshot()

    def renew(self, sid, rid, *, operator=False):
        with self.lock:
            a = self._active(rid)
            self._owner(a, sid)
            self.tick()
            if a["phase"] == "recovering":
                raise ValueError("Supervision was lost. Check and release this window; an expired grant cannot be renewed.")
            with self._transaction():
                if operator:
                    self._event(a, "manual_renew", source="operator")
                self.lease = self.clock() + LEASE_S
                a["lease_until"] = self.wall() + LEASE_S
            return self.snapshot()

    def work(self, sid, rid, running):
        with self.lock:
            a = self._active(rid)
            self._owner(a, sid)
            self.tick()
            if a["phase"] not in ("ready", "working"):
                raise ValueError("Wait until the window is ready; a recovering grant cannot start more work.")
            if not isinstance(running, bool):
                raise ValueError("running must be true or false")
            with self._transaction():
                a.update(work=running, phase="working" if running else "ready")
                self.lease = self.clock() + LEASE_S
                a["lease_until"] = self.wall() + LEASE_S
            return self.snapshot()

    def release(self, sid, rid, *, finished=False, admin=False, cancelled=False):
        with self.lock:
            last = self.doc.get("last")
            if not self.doc.get("active") and last and last["id"] == rid:
                if not admin:
                    self._owner(last, sid)
                return self.snapshot()
            a = self._active(rid)
            if not admin:
                s = self._owner(a, sid)
                if getattr(s, "background_tasks", {}) or getattr(s, "subagents", {}):
                    raise ValueError("Finish the owner's background work before releasing.")
            free = self._exempt_ids(a)
            if a["phase"] not in ("draining", "settling") and (any(x.get("session", "") not in free for x in self.external)
                                                                or any(x[0] == a["owner"] for x in self.operations.values())):
                raise ValueError("The owner's managed work is still running.")
            if not finished:
                raise ValueError("Confirm that all work in this window has ended before releasing.")
            with self._transaction():
                a.update(phase="released", ended=self.wall(), work=False, released_by="operator" if admin else "agent",
                         outcome="cancelled" if cancelled else "released")
                recipients = [{"id": a["owner"], "generation": a["generation"]}] + a["participants"]
                for p in recipients:
                    resume = {"status": "pending", "detail": "Piper will send a continue notice"}
                    p["resume"] = resume
                    self.doc["resumes"] = [r for r in self.doc["resumes"] if r["session"] != p["id"]]
                    self.doc["resumes"].append({"window": rid, "session": p["id"], "generation": p["generation"], **resume})
                self.doc.update(last=copy.deepcopy(a), active=None)
                while self.doc["queue"]:
                    queued = self.doc["queue"].pop(0)
                    if self._select(queued):
                        break
                    self._event(queued, "skipped", detail="Session ended or restarted", source="Piper observed")
            common.audit({"event": "quiet-release", "session": sid, "window": rid, "operator": admin})
            return self.snapshot()

    def resume_delivery(self, sid, rid, detail, *, sent=False):
        """Durable at-least-once outbox; never mistake PTY delivery for resumed work."""
        with self.lock:
            r = next((r for r in self.doc["resumes"] if r["session"] == sid and r["window"] == rid), None)
            if not r or not sent and r.get("detail") == detail:
                return
            with self._transaction():
                receipt = {"status": "sent" if sent else "pending", "detail": detail}
                last = self.doc.get("last")
                if last and last["id"] == rid:
                    for p in last["participants"]:
                        if p["id"] == sid:
                            p["resume"] = receipt
                if sent:
                    self.doc["resumes"].remove(r)
                else:
                    r.update(receipt)

    def cancel(self, sid, rid, *, admin=False):
        with self.lock:
            a = self.doc.get("active")
            if a and a["id"] == rid:
                if a["phase"] not in ("draining", "settling") and not (
                        a["phase"] == "recovering" and not a.get("ready") and not a.get("work")):
                    raise ValueError("The window may have running work. Use Release after checking it.")
                return self.release(sid, rid, finished=True, admin=admin, cancelled=True)
            r = next((x for x in self.doc["queue"] if x["id"] == rid), None)
            if not r:
                return self.snapshot()   # cancel is idempotent
            if not admin:
                self._owner(r, sid)
            with self._transaction():
                self._event(r, "cancelled", source="operator" if admin else "agent")
                self.doc["queue"].remove(r)
            return self.snapshot()

    def wait(self, sid, rid, seconds=25):
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 <= seconds <= 25:
            raise ValueError("Wait must be 0–25 seconds.")
        with self.changed:
            end = time.monotonic() + seconds
            while True:
                self.tick()
                a = self.doc.get("active")
                if not a or a["id"] != rid or sid == a["owner"] and a["phase"] in ("ready", "working", "recovering"):
                    break
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self.changed.wait(min(1, remaining))
            return self.snapshot()

    def tick(self):
        with self.lock:
            a = self.doc.get("active")
            if not a:
                return
            before = copy.deepcopy(self.doc)
            timers = (self.lease, self.settle, self.deadline)
            rows, now = self._rows(), self.clock()
            a["observed_jobs"] = sorted([
                {"session": sid, "title": getattr(rows.get(sid), "title", "Piper"), "kind": kind}
                for sid, kind in self.operations.values()] + [
                {"session": x.get("session", ""), "title": getattr(rows.get(x.get("session")), "title", "Piper"), "kind": x["kind"]}
                for x in self.external], key=lambda j: (j["session"], j["kind"]))
            owner = rows.get(a["owner"])
            # A synchronous managed job is observed by this daemon for its whole lifetime.
            # Keep its owner's lease alive; an explicitly requested legacy cap still applies.
            if a["phase"] in ("ready", "working") and any(sid == a["owner"] for sid, _ in self.operations.values()):
                self.lease = now + LEASE_S
                if a.get("lease_until", 0) < self.wall() + LEASE_S - 30:
                    a["lease_until"] = self.wall() + LEASE_S
            if abs((self.wall() - self.time_anchor[1]) - (now - self.time_anchor[0])) > 15:
                a.update(phase="recovering", problem="The computer clock or sleep state changed. Check the work before releasing.")
            if a["phase"] != "recovering" and (not owner or owner.state not in LIVE or self.generation(owner) != a["generation"] or now >= self.lease):
                a.update(phase="recovering", problem="The owner stopped responding. Verify that its work has ended before releasing.")
            for s in rows.values():
                if s.id != a["owner"] and s.state in LIVE and not any(p["id"] == s.id for p in a["participants"]):
                    a["participants"].append(self._participant(s))
                    self.settle = 0
                    if a["phase"] in ("ready", "working"):
                        a.update(phase="recovering", problem="Another session started. Check the work before continuing.")
            for p in a["participants"]:
                s = rows.get(p["id"])
                if s and p.get("at") is None and ":None:" in p["generation"] and p.get("created") == s.created:
                    p["generation"] = self.generation(s)  # the originally captured start has now obtained a PID
                if s and s.pid is None and not getattr(s, "quiet_generation", "") and s.state == "preflight" and getattr(s, "phase", "") == "quiet":
                    p.update(status="waiting", detail="No agent started; admission is queued until release", at=self.wall(), confirmed_by="Piper observed")
                if s and p["status"] == "waiting" and (getattr(s, "subagents", {}) or getattr(s, "background_tasks", {})):
                    p.update(status="finishing", detail="Background work is still running", at=self.wall(), confirmed_by="Piper observed")
                    if a["phase"] in ("ready", "working"):
                        a.update(phase="recovering", problem="A participant has background work. Check before continuing.")
                if not s or self.generation(s) != p["generation"] or s.state not in LIVE:
                    p.pop("exempt", None)   # an exception never carries over to another runtime
                    p.update(status="unknown", detail="Session ended or restarted; verify its background work.")
                    a.update(phase="recovering", problem="A participant ended or restarted. Check for remaining work before releasing.")
            if a["phase"] in ("draining", "settling"):
                free = self._exempt_ids(a, ("allowed",))
                clear = (all(p["status"] == "waiting" or p["id"] in free for p in a["participants"])
                         and not any(sid not in free for sid, _ in self.operations.values())
                         and not any(x.get("session", "") not in free for x in self.external))
                if clear:
                    if not self.settle:
                        self.settle = now + SETTLE_S
                    a["phase"] = "settling"
                    if now >= self.settle:
                        duration = a.get("minutes")
                        a.update(phase="ready", ready=self.wall(), deadline=self.wall() + duration * 60 if duration else None)
                        self.deadline = now + duration * 60 if duration else 0
                else:
                    a["phase"], self.settle = "draining", 0
            if a["phase"] in ("ready", "working") and self.deadline and now >= self.deadline:
                a.update(phase="recovering", problem="The quiet window reached its time limit. Verify that all work has ended before releasing.")
            if self.doc != before:
                try:
                    self._save()
                except BaseException:
                    self.doc = before
                    self.lease, self.settle, self.deadline = timers
                    raise


class QuietWindows:
    """Manager adapter: session-authenticated tools and polite, retryable terminal notices."""

    def _quiet_init(self):
        import features
        self.quiet = Coordinator(lambda: list(self.sessions.values()), lambda: features.on("quiet"), before_ack=self._quiet_seal)
        self._quiet_notice = {}
        self._quiet_hints = {}
        self._quiet_jobs_at = 0.0
        self._quiet_stack_seen = False

    def quiet_summary(self):
        return self.quiet.snapshot()

    def quiet_queue_start(self, body):
        """Durable GUI-only intent. No process or project change until the host gate opens."""
        import features
        project, task, title = (body.get(k, "") for k in ("project", "task", "title"))
        if not all(isinstance(v, str) for v in (project, task, title)) or not project or len(project) > 512 or len(task) > 20000 or len(title) > 200:
            raise ValueError("Invalid queued session")
        opts, agent, runtime = body.get("opts") or {}, body.get("agent", "claude"), body.get("runtime", "hole")
        if not isinstance(opts, dict) or any(k not in ("docker", "unrestricted", "idea") or not isinstance(v, bool) for k, v in opts.items()):
            raise ValueError("Invalid start options")
        if agent not in ("claude", "codex") or runtime not in ("hole", "direct") or runtime == "direct" and opts:
            raise ValueError("Invalid agent or runtime")
        key = body.get("request_key")
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            raise ValueError("A request key is required")
        worktree = body.get("worktree")
        if worktree is not None and (not isinstance(worktree, dict) or set(worktree) - {"action", "name", "branch", "new", "start"}
                or worktree.get("action") != "add" or worktree.get("new") is not True
                or any(not isinstance(worktree.get(k), str) or not 1 <= len(worktree[k]) <= 300 for k in ("name", "branch"))
                or "start" in worktree and (not isinstance(worktree["start"], str) or not 1 <= len(worktree["start"]) <= 300)):
            raise ValueError("Invalid queued worktree")
        with CONFIG_LOCK, self.quiet.lock:
            if not features.on("quiet"):
                raise ValueError("Quiet window is off in Settings → Features.")
            if len(self.quiet.doc["starts"]) >= MAX_QUEUE:
                raise ValueError("The queued starts list is full")
            if not any(x["id"] == key for x in self.quiet.doc["starts"]):
                with self.quiet._transaction():
                    self.quiet.doc["starts"].append({"id": key, "project": project, "task": task, "title": common.no_bidi(title),
                                                    "opts": opts, "agent": agent, "runtime": runtime, "remember": body.get("remember_for_project") is True, "phase": "queued",
                                                    **({"worktree": copy.deepcopy(worktree)} if worktree else {})})
            return self.quiet.snapshot()

    def quiet_cancel_start(self, key, *, checked=False):
        with self.quiet.lock:
            if (self.quiet.doc.get("last_start") or {}).get("id") == key:
                raise ValueError("This session already started. Check it in Overview before stopping it.")
            r = next((x for x in self.quiet.doc["starts"] if x["id"] == key), None)
            if r and r["phase"] in ("checking", "started"):
                raise ValueError("This session is already being started. Check Overview; cancelling its queue entry cannot stop it.")
            if r and r["phase"] == "uncertain" and not checked:
                raise ValueError("Check Overview and explicitly confirm removal of this uncertain start record.")
            with self.quiet._transaction():
                self.quiet.doc["starts"] = [x for x in self.quiet.doc["starts"] if x["id"] != key]
        return self.quiet.snapshot()

    def _quiet_dispatch_starts(self):
        with self.quiet.lock:
            if self.quiet.doc["active"]:
                return
            r = next((x for x in self.quiet.doc["starts"] if x["phase"] == "queued"), None)
            if not r:
                return
            # A managed admission token covers dispatch below. A crash after this durable
            # marker is reconciled by the operator, never replayed.
            with self.quiet._transaction():
                r["phase"] = "checking"
            r = copy.deepcopy(r)  # transactions elsewhere may roll back the shared document
        def record():
            return next(x for x in self.quiet.doc["starts"] if x["id"] == r["id"])
        try:
            with self.quiet.operation("", "queued start"):
                project = r.get("resolved_project") or r["project"]
                if r.get("worktree") and not r.get("resolved_project"):
                    project = self.git_worktree_op(project, r["worktree"])["key"]
                    with self.quiet.lock, self.quiet._transaction():
                        record()["resolved_project"] = project
                s = self.create(project, task=r["task"], title=r["title"], opts=r["opts"], agent=r["agent"], runtime=r["runtime"])
        except Exception as e:
            with self.quiet.lock, self.quiet._transaction():
                if isinstance(e, ValueError) and ("Quiet window" in str(e) or str(e).startswith("limit of ")):
                    record()["phase"] = "queued"  # admission refused before create inserted a session
                else:
                    record().update(phase="failed", error=common.scrub(str(e))[:300])
            return
        with self.quiet.lock, self.quiet._transaction():
            record().update(phase="started", session=s.id)
            self.quiet.doc["last_start"] = {"id": r["id"], "session": s.id}
        if r.get("remember"):
            import projects
            import features
            try:
                projects.remember_session_defaults(s.project, s.agent, s.opts,
                    keep_agent=features.session_agents() is None and not features.on("codex"), session_path=s.path)
            except (OSError, ValueError, KeyError):
                common.audit({"event": "quiet-start-preferences-failed", "session": s.id})
        with self.quiet.lock, self.quiet._transaction():
            self.quiet.doc["starts"] = [x for x in self.quiet.doc["starts"] if x["id"] != r["id"]]

    def quiet_tool(self, s, tool, args):
        result = self._quiet_tool(s, tool, args)
        # A ready/released reply must not leave this caller with the preceding
        # poll's native gate. Participants are sealed synchronously before ACK.
        try:
            self._quiet_hint(s)
        except OSError:
            pass  # a closed stale hint fails safe; retry on the next poll
        return {k: v for k, v in result.items() if k not in ("events", "event_dropped")}

    def _quiet_tool(self, s, tool, args):
        if getattr(s, "kind", "") == "prodcheck" and tool not in ("quiet_status", "quiet_ack", "quiet_wait"):
            raise ValueError("Production checks can only participate in quiet windows, not own them.")
        rid = args.get("id")
        if tool == "quiet_request":
            if args.get("user_requested") is not True:
                raise ValueError("Request a quiet window only when the user explicitly asks for quiet or exclusive machine time. "
                                 "Heavy work or thorough testing alone is not authorization. Set user_requested=true only for that explicit instruction.")
            return self.quiet.request(s.id, args.get("reason"), args.get("minutes"), args.get("request_key", ""))
        if tool == "quiet_status":
            self.quiet.tick()
            return self.quiet.snapshot()
        if not isinstance(rid, str):
            raise ValueError("A quiet window id is required.")
        if tool == "quiet_ack":
            if not isinstance(args.get("finished"), bool):
                raise ValueError("finished must be true or false")
            return self.quiet.ack(s.id, rid, done=args["finished"], detail=args.get("detail", ""), revision=args.get("revision"))
        if tool == "quiet_wait":
            return self.quiet.wait(s.id, rid, args.get("seconds", 25))
        if tool == "quiet_renew":
            return self.quiet.renew(s.id, rid)
        if tool == "quiet_work":
            return self.quiet.work(s.id, rid, args.get("running"))
        if tool == "quiet_release":
            return self.quiet.release(s.id, rid, finished=args.get("finished") is True)
        if tool == "quiet_cancel":
            return self.quiet.cancel(s.id, rid)
        raise ValueError("Unknown quiet window tool.")

    def _quiet_hint(self, s, *, seal=False):
        with self.quiet.lock:
            blocked = self.quiet.blocked(s.id)
            a = self.quiet.doc.get("active")
            p = next((p for p in a["participants"] if p["id"] == s.id), None) if a else None
            cleanup = bool(not seal and p and p["status"] == "finishing" and p["generation"] == self.quiet.generation(s))
            # An empty native gate during cleanup also works with already-running
            # clients' old hooks. Central managed admission remains closed.
            hint = {"blocked": "" if cleanup else blocked, "id": a["id"] if a else None, "cleanup_allowed": cleanup}
            if self._quiet_hints.get(s.id) != hint and os.path.isdir(os.path.join(s.box, "piper")):
                common.safe_write(s.box, "piper/quiet.json", (json.dumps(hint) + "\n").encode())
                self._quiet_hints[s.id] = hint

    def _quiet_seal(self, s):
        self._quiet_hint(s, seal=True)

    def _quiet_tick(self):
        self._quiet_dispatch_starts()
        if not self.quiet.snapshot()["active"]:
            self._quiet_stack_seen = False
        # Asynchronous dev stack jobs outlive stack_action's HTTP response. While a request is
        # active, sample their real state; an unavailable configured stack provider is not idle.
        if self.quiet.snapshot()["active"] and time.monotonic() >= self._quiet_jobs_at:
            import features
            with self.quiet.lock:
                self.quiet.external = []
                if features.on("stack") and self._stack_up():
                    self._quiet_stack_seen = True
                    try:
                        prov = self._stack()
                        self.quiet.external = [{"session": self._quiet_job_session(j, prov), "kind": "stack job " + str(j.get("id", ""))}
                                               for j in prov.jobs() if j.get("state") == "running"]
                    except Exception:
                        self.quiet.external = [{"session": "", "kind": "dev stack state could not be verified"}]
                elif self._quiet_stack_seen:
                    self.quiet.external = [{"session": "", "kind": "dev stack disconnected; check for remaining jobs"}]
            self._quiet_jobs_at = time.monotonic() + 2
        self.quiet.tick()
        snap = self.quiet.snapshot()
        a = snap["active"]
        if not a:
            for r in snap["resumes"]:
                s = self.sessions.get(r["session"])
                if not s or s.state not in LIVE:
                    try:
                        self.quiet.resume_delivery(r["session"], r["window"], "Session ended or was removed; inspect any remaining work")
                    except OSError:
                        pass
        for s in list(self.sessions.values()):
            try:
                self._quiet_hint(s)
            except OSError:
                pass  # retry without starving other sessions
            if s.state not in ("ready", "working", "waiting", "attention"):
                continue
            if not a and not any(r["session"] == s.id and r["generation"] == self.quiet.generation(s) for r in snap["resumes"]) and self._task_pending(s):
                self._quiet_notice.pop(s.id, None)
                self._deliver_task(s)
            if a:
                role = "owner" if a["owner"] == s.id else "participant"
                me = next((p for p in a["participants"] if p["id"] == s.id), {})
                shared = sum(1 for p in a["participants"] if p.get("exempt"))
                # The request token is control data; never paste another session's reason/title into a prompt.
                key = (a["id"], role, (a["phase"], shared) if role == "owner" else ("hold", me.get("exempt", ""), me.get("rev", 0)))
                msg = ("Piper quiet window %s: %s. You own this request. Check quiet_status; start work only when ready, "
                       "mark quiet_work running=true before work and running=false when it ends. Run long native commands "
                       "asynchronously so you can renew with quiet_renew at least every 60 seconds. Release after all work ends." % (a["id"], a["phase"])
                       + (" The operator let %d other session%s keep working: this window is shared, not quiet; results may be affected."
                          % (shared, "" if shared == 1 else "s") if shared else "")) if role == "owner" else (
                       "Piper quiet window %s: the operator gave this session an exception. Continue your work; do not confirm "
                       "with quiet_ack. Check quiet_status if unsure." % a["id"]) if me.get("exempt") == "allowed" else (
                       "Piper quiet window %s: the operator removed this session's exception. Do not start new work. "
                       "Checkpoint and stop your own jobs, background tasks and subagents, then call quiet_ack with "
                       "finished=true and revision=%d, then quiet_wait." % (a["id"], me.get("rev", 0))) if me.get("exempt") == "returning" else (
                       "Piper requests a quiet window (%s). Do not start new work. First report quiet_ack finished=false with "
                       "a short detail of what is still running; update it at least every 60 seconds while finishing. "
                       "Checkpoint and promptly interrupt/cancel your own heavy jobs where safe; do not wait for an entire "
                       "long test suite when cancellation is safe. Stop your own background services and ask subagents to do likewise. "
                       "Native cleanup tools remain available until your confirmation; use them only to stop/check existing work. "
                       "Wait for all background tasks/subagents to end, "
                       "then call quiet_ack with finished=true and quiet_wait. "
                       "Check quiet_status before resuming; another window may be queued." % a["id"])
            else:
                resume = next((r for r in snap["resumes"] if r["session"] == s.id), None)
                if not resume:
                    self._quiet_notice.pop(s.id, None)
                    continue
                if resume["generation"] != self.quiet.generation(s):
                    self._quiet_notice.pop(s.id, None)
                    try:
                        self.quiet.resume_delivery(s.id, resume["window"], "Session runtime changed; inspect it before continuing")
                    except OSError:
                        pass
                    continue
                key = ("released",)
                msg = "Piper: the quiet window is released. Check quiet_status; if no new window holds you, continue your previous work."
            prev = self._quiet_notice.get(s.id)
            if prev and prev[0] == key and (role != "owner" if a else True):
                continue
            if prev and prev[0] == key and time.monotonic() - prev[1] < 30:
                continue
            # at_prompt is a computed API field, not a Session attribute. Working
            # agents can receive a follow-up, just like send_to_session; waiting
            # agents use the existing prompt predicate. Never answer a dialog or
            # overwrite the human's unfinished input.
            pending = ("Waiting for an approval prompt to close" if self._dialog_up(s) else
                       "Waiting for a safe terminal prompt" if not (s.state == "working" or self._istate_at_prompt(s)) else
                       "Waiting for draft input to be sent or cleared" if not self.auto_type_ok(s) else "")
            if pending:
                if a:
                    try:
                        self.quiet.delivery(s.id, a["id"], pending)
                    except OSError:
                        pass
                else:
                    try:
                        self.quiet.resume_delivery(s.id, resume["window"], pending)
                    except OSError:
                        pass
                continue
            msg += self._quiet_legacy_controls(s, a)
            try:
                # type_text's admission gate is bypassed only for our fixed control notice.
                if self.type_text(s, msg, _quiet_control=True):
                    if a:
                        self.quiet.delivery(s.id, a["id"], "Request sent; waiting for an update", sent=True)
                        self._quiet_notice[s.id] = (key, time.monotonic())
                    else:
                        self.quiet.resume_delivery(s.id, resume["window"], "Piper asked this session to continue", sent=True)
                        self._quiet_notice.pop(s.id, None)
                elif a:
                    self.quiet.delivery(s.id, a["id"], "Waiting for a safe terminal prompt")
                else:
                    self.quiet.resume_delivery(s.id, resume["window"], "Waiting for a safe terminal prompt")
            except (ValueError, OSError, PtyHostError):
                if a:
                    try:
                        self.quiet.delivery(s.id, a["id"], "Delivery failed; Piper will retry")
                    except OSError:
                        pass
                else:
                    try:
                        self.quiet.resume_delivery(s.id, resume["window"], "Delivery failed; Piper will retry")
                    except OSError:
                        pass

    def _quiet_job_session(self, job, prov=None):
        """The session runtime that started a dev stack job, from the provider's authenticated origin
        (its optional session_labels, stackview.py).

        Only a job started after this runtime's stack access was granted is attributed; an older
        runtime's job, a GUI/global job or a provider without origins stays unattributed and
        keeps holding the window."""
        origin, started = job.get("origin"), job.get("started")
        labels = getattr(prov, "session_labels", None)
        if not callable(labels) or not isinstance(origin, str) or not isinstance(started, (int, float)) or isinstance(started, bool):
            return ""
        for s in list(self.sessions.values()):
            since = getattr(s, "stack_since", 0)
            if since and origin in labels(s.id) and started >= int(since) and s.state in LIVE:
                return s.id
        return ""

    def _quiet_legacy_controls(self, s, active):
        """Existing sessions retain their original MCP tool list after a daemon update.

        The existing piper_session tool accepts the same controls, authenticated
        by its session mailbox. Normal quiet_tool validation still applies.
        """
        calls = [{"action": "status"}]
        if active:
            rid = active["id"]
            me = next((p for p in active["participants"] if p["id"] == s.id), {})
            calls += ([{"action": "renew", "id": rid}, {"action": "work", "id": rid, "running": True},
                       {"action": "work", "id": rid, "running": False}, {"action": "release", "id": rid, "finished": True}]
                      if active["owner"] == s.id else [] if me.get("exempt") == "allowed" else
                      [{"action": "ack", "id": rid, "finished": False, "detail": "Describe the existing work still finishing"},
                       {"action": "ack", "id": rid, "finished": True, **({"revision": me["rev"]} if me.get("rev") else {})},
                       {"action": "wait", "id": rid, "seconds": 25}])
        lines = ["\nIf quiet_* tools are not listed, call your existing piper_session tool with these arguments.",
                 "First check status. Use finished=false only to report progress; it never grants quiet. "
                 "Work controls apply only after ready. Use finished=true/release only after verifying all your work has ended; "
                 "wait until released before resuming."]
        lines.extend(json.dumps({"quiet": args}) for args in calls)
        return "\n".join(lines)
