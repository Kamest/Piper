"""Dev stacks a session started: stopped when it ends, a sub-stack's data released later.

A mixin of sessions.Manager like prodcheck.ProdCheck. Piper's core runs no stack itself; it asks the
extension's stack provider (stackview.py). Nothing here is done in the exit path: an exit only writes a
record ($PIPER_HOME/stack-reap.json) and the `piper-stack-reap` thread acts on it, so a piperd restart
loses nothing and a session that comes straight back (a resume, an interrupted sandbox) keeps its stack.

  start   the spawn notes when the runtime began (`since`) and which stack roots ran then
          (provider.running_stacks; unknown = None). A new session in a worktree cancels that
          worktree's pending data release.
  end     _on_exit notes the end. GRACE_S later, if the session is not running again, its stacks
          are the roots of the provider's jobs that this runtime started (job origin in
          provider.session_labels(id), started >= since, verb in START_VERBS) plus the worktrees whose
          own stack it set up through piper_worktree_stack (adopt runs with the provider's own label).
          A root is NEVER stopped when it ran already at the start (or that is unknown), when another
          running session uses it (stack_worktree) or the provider cannot say, or when a later start
          came from another origin (the user's dashboard, another session). Policy stack_stop_on_end.
  data    such a root that is a linked worktree's own stack (never a main checkout's) gets a release
          due policy stack_data_cleanup after the end. When due it is checked again - the worktree
          still exists, no running session works in it, its stack is not running - and the provider's
          optional release_data(root) removes its containers, volumes and data, never the checkout.

Every decision is audited (stack-reap-*); a provider without these methods makes it a no-op.
"""
from __future__ import annotations

import os
import threading
import time

import common
from common import audit

REAP_FILE = os.path.join(common.PIPER_HOME, "stack-reap.json")
GRACE_S = 60.0          # an exit this recent may be a restart of the same session: wait
TICK_S = 30.0
GIVE_UP_S = 86400.0     # a provider unreachable for a day: the end record goes (audited)
START_VERBS = ("up", "start", "rebuild", "restart", "worktree_adopt")
CLEANUP = {"off": None, "end": 0, "1d": 86400, "3d": 3 * 86400, "7d": 7 * 86400}


def _real(p) -> str:
    return os.path.realpath(p) if isinstance(p, str) and os.path.isabs(p) and "\0" not in p else ""


def _num(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def started_roots(jobs, labels, since: float) -> dict:
    """{root: newest start time} of the jobs `labels` (this runtime's origins) started at/after `since`."""
    out: dict = {}
    for j in jobs or []:
        if not isinstance(j, dict) or j.get("origin") not in labels or str(j.get("verb") or "") not in START_VERBS:
            continue
        root, at = _real(j.get("root")), _num(j.get("started"))
        if root and at >= int(since):   # a job JSON carries whole seconds
            out[root] = max(out.get(root, 0.0), at)
    return out


def taken_over(jobs, labels, root: str, after: float) -> bool:
    """Whether another origin started `root` after this runtime's last start (it is theirs now)."""
    for j in jobs or []:
        if (isinstance(j, dict) and j.get("origin") not in labels and str(j.get("verb") or "") in START_VERBS
                and _real(j.get("root")) == root and _num(j.get("started")) > after):
            return True
    return False


class StackReap:
    def _reap_init(self) -> None:
        self._reap_lock = threading.RLock()

    def _reap_start_thread(self) -> None:
        self._start_service(self._reap_loop, "piper-stack-reap")

    # ------------------------------------------------------------------ records
    def _reap_read(self) -> dict:
        d = common.read_json(REAP_FILE, {})
        d = d if isinstance(d, dict) else {}
        for k in ("sessions", "data"):
            if not isinstance(d.get(k), dict):
                d[k] = {}
        return d

    def _reap_write(self, d: dict) -> None:
        common.write_json(REAP_FILE, d)

    def _reap_provider(self):
        import features
        if not features.on("stack"):
            return None
        try:
            return self._stack()
        except Exception:  # noqa: BLE001 - no provider: nothing to reap
            return None

    @staticmethod
    def _reap_running(prov) -> set | None:
        """the roots the provider says run now; None = it cannot say"""
        inventory = getattr(prov, "running_stacks", None)
        if not callable(inventory):
            return None
        try:
            return {r for r in (_real(x.get("root")) for x in inventory() or [] if isinstance(x, dict)
                                and isinstance(x.get("running"), int) and x["running"] > 0) if r}
        except Exception:  # noqa: BLE001 - provider down: unknown
            return None

    # ------------------------------------------------------------------ lifecycle hooks
    def _reap_started(self, s) -> None:
        """At a spawn (before the agent runs): the runtime's start and the roots running now (off-thread)."""
        if s.kind == "prodcheck":
            return
        since = time.time()
        with self._reap_lock:
            d = self._reap_read()
            d["sessions"][s.id] = {"since": since, "running": None, "own": (d["sessions"].get(s.id) or {}).get("own") or []}
            me = _real(s.path)
            for root in [r for r in d["data"] if me and (me == r or me.startswith(r.rstrip("/") + "/"))]:
                d["data"].pop(root, None)
                audit({"event": "stack-reap-data-canceled", "session": s.id, "root": root, "why": "a new session works there"})
            self._reap_write(d)

        def snap():
            prov = self._reap_provider()
            running = self._reap_running(prov) if prov is not None else None
            with self._reap_lock:
                d = self._reap_read()
                rec = d["sessions"].get(s.id)
                if rec and rec.get("since") == since and running is not None:
                    rec["running"] = sorted(running)
                    self._reap_write(d)
        threading.Thread(target=snap, daemon=True).start()

    def _reap_adopted(self, s, top: str) -> None:
        """piper_worktree_stack set up the worktree's own stack for this session (the provider's label, not its)."""
        root = _real(top)
        if not root:
            return
        with self._reap_lock:
            d = self._reap_read()
            rec = d["sessions"].setdefault(s.id, {"since": time.time(), "running": None, "own": []})
            if root not in rec.setdefault("own", []):
                rec["own"].append(root)
            self._reap_write(d)

    def _reap_ended(self, s) -> None:
        with self._reap_lock:
            d = self._reap_read()
            rec = d["sessions"].get(s.id)
            if rec is None:
                return
            rec["ended"] = time.time()
            self._reap_write(d)

    # ------------------------------------------------------------------ the loop
    def _reap_loop(self) -> None:
        while not self._stop_event.wait(TICK_S):
            try:
                self._reap_tick()
            except Exception as e:  # noqa: BLE001 - the next tick tries again
                print("piperd: stack reap: %s" % common.brief_error(e), flush=True)

    def _reap_tick(self, now: float | None = None) -> None:
        import sessions as _s
        now = time.time() if now is None else now
        with self._reap_lock:
            d = self._reap_read()
        for sid, rec in list(d["sessions"].items()):
            ended = _num(rec.get("ended"))
            s = self.sessions.get(sid)
            if s is not None and s.state in _s.ACTIVE:
                continue   # running (again): a later exit decides
            if not ended:
                if s is None:   # removed without an exit we saw: nothing to attribute
                    self._reap_drop(sid)
                continue
            if now - ended < GRACE_S:
                continue
            done = self._reap_session(sid, rec, now)
            if done or now - ended > GIVE_UP_S:
                if not done:
                    audit({"event": "stack-reap-gave-up", "session": sid})
                self._reap_drop(sid)
        for root, rec in list(d["data"].items()):
            if _num(rec.get("due")) <= now:
                self._reap_data(root, rec)

    def _reap_drop(self, sid: str) -> None:
        with self._reap_lock:
            d = self._reap_read()
            d["sessions"].pop(sid, None)
            self._reap_write(d)

    def _reap_session(self, sid: str, rec: dict, now: float) -> bool:
        """Stop what the ended session `sid` started; True = decided (the record can go)."""
        import sessions as _s
        prov = self._reap_provider()
        if prov is None:
            return True
        labels_fn = getattr(prov, "session_labels", None)
        labels = list(labels_fn(sid)) if callable(labels_fn) else []
        try:
            jobs = prov.jobs() if labels else []
        except Exception:  # noqa: BLE001 - provider down: try again later
            return False
        since = _num(rec.get("since"))
        mine = started_roots(jobs, labels, since)
        for r in rec.get("own") or []:
            if _real(r):
                mine.setdefault(_real(r), since)
        if not mine:
            return True
        before = rec.get("running")
        before = set(before) if isinstance(before, list) else None
        running = self._reap_running(prov)
        others = [x for x in list(self.sessions.values()) if x.id != sid and x.state in _s.ACTIVE
                  and x.kind == "project" and x.path]
        stop_on = bool(self.policy.get("stack_stop_on_end", True))
        delay = CLEANUP.get(str(self.policy.get("stack_data_cleanup", "3d")), CLEANUP["3d"])
        for root, last in sorted(mine.items()):
            why = ""
            if before is None:
                why = "unknown whether it ran before the session"
            elif root in before:
                why = "it ran already when the session started"
            elif taken_over(jobs, labels, root, last):
                why = "started again by someone else"
            else:
                for x in others:
                    try:
                        w = self.stack_worktree(x.path)
                    except Exception:  # noqa: BLE001 - unknown use = in use
                        w = {"root": root}
                    if w and _real(w.get("root")) == root:
                        why = "session %s uses it" % x.id
                        break
            if why:
                audit({"event": "stack-reap-kept", "session": sid, "root": root, "why": why})
                continue
            if stop_on and (running is None or root in running):
                try:
                    job = prov.verb(root, "stop", "") or {}
                    audit({"event": "stack-reap-stopped", "session": sid, "root": root, "job": str(job.get("id") or "")})
                except Exception as e:  # noqa: BLE001 - one root's failure never blocks the rest
                    audit({"event": "stack-reap-stop-failed", "session": sid, "root": root, "error": str(e)[:300]})
            self._reap_arm_data(sid, root, now, delay)
        return True

    def _reap_arm_data(self, sid: str, root: str, now: float, delay) -> None:
        """a linked worktree's own stack (never a main checkout's) gets its data release due"""
        if delay is None:
            return
        link = self.worktree_link(root)
        if not link or _real(link["top"]) != root or _real(link["main_top"]) == root:
            return
        with self._reap_lock:
            d = self._reap_read()
            due = now + delay
            prev = d["data"].get(root) or {}
            d["data"][root] = {"session": sid, "due": max(due, _num(prev.get("due"))), "armed": now}
            self._reap_write(d)
        audit({"event": "stack-reap-data-armed", "session": sid, "root": root, "due": int(due)})

    def _reap_data(self, root: str, rec: dict) -> None:
        import sessions as _s
        delay = CLEANUP.get(str(self.policy.get("stack_data_cleanup", "3d")), CLEANUP["3d"])
        why = ""
        if delay is None:
            why = "data cleanup is off"
        elif not os.path.isdir(root):
            why = "the worktree is gone"
        elif any(x.state in _s.ACTIVE and _real(x.path) and (_real(x.path) == root or _real(x.path).startswith(root + "/"))
                 for x in list(self.sessions.values())):
            return   # a session works there: due again at its own end
        prov = self._reap_provider()
        release = getattr(prov, "release_data", None) if prov is not None else None
        if not why and not callable(release):
            why = "the stack provider cannot release data"
        if not why:
            running = self._reap_running(prov)
            if running is None:
                return   # cannot say: try again next tick
            if root in running:
                if time.time() - _num(rec.get("armed")) < 3600:
                    return   # the stop at the end may still be running: look again
                why = "its stack runs again"
        if not why:
            link = self.worktree_link(root)
            if not link or _real(link["top"]) != root or _real(link["main_top"]) == root:
                why = "not a linked worktree's own stack"
        if why:
            audit({"event": "stack-reap-data-kept", "root": root, "session": rec.get("session"), "why": why})
        else:
            try:
                job = release(root) or {}
                audit({"event": "stack-reap-data-released", "root": root, "session": rec.get("session"),
                       "job": str(job.get("id") or "")})
            except Exception as e:  # noqa: BLE001 - the provider refused (main stack, busy): kept, audited
                audit({"event": "stack-reap-data-failed", "root": root, "session": rec.get("session"), "error": str(e)[:300]})
        with self._reap_lock:
            d = self._reap_read()
            if d["data"].get(root) == rec:
                d["data"].pop(root, None)
                self._reap_write(d)
