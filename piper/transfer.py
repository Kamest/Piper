"""Move a running local session's work to a server environment ("Move to server", one action).

Piper asks the agent for a handoff (HANDOFF.md), stops the session once the agent finished, commits the work as
`wip: handoff <sid>` and pushes it through the push card, then asks the server's peer (remotepeer `transfer`)
to start a session that continues on the pushed branch with Piper's own handoff document. The local session
ends `moved`: locked for start/send, visible, never removed automatically.

Every phase is recorded in the session dir (transfer.json) before the next one starts, so a piperd restart
continues after the last finished phase. A request to the server whose answer was lost is never replayed
(remotes.py): the transfer stops as `remote_unknown` and POST .../move/check asks the server first. Nothing
here stops a turn by force, presses Esc or gives up after a timeout: the human's ordinary Stop is the only
way to cut the agent short, and the move then goes on without HANDOFF.md."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import tarfile
import threading
import time

import agentswitch
import common
import gitops
import remotes
from common import audit

FILE = "transfer.json"        # the phase record, in the session dir
HANDOFF = "HANDOFF.md"        # written by the agent into the project root
META = "transfer.json"        # in the archive: what the server session is created from
DIR = "transfers"             # the server's $PIPER_HOME/transfers/<id>/ (handoff.md, transfer.json)
ID = re.compile(r"[0-9a-f]{16}")
MODES = ("wait", "now")
# completed milestones, in order; "phase" also shows the transient waits below
PHASES = ("requested", "handoff_sent", "stopped", "committed", "pushed", "remote_created", "moved")
WAITS = ("blocked_on_card", "waiting_send", "remote_sending", "remote_unknown")
TICK = 0.5
PROMPT = ("Finish as soon as possible: do not start anything new. Complete the step in progress only if it "
          "takes a few lines, otherwise return it to a consistent state. Write HANDOFF.md in the project root "
          "(goal, done, verified - commands, remaining, open questions, pitfalls, what you were in the middle "
          "of). Then stop.")
MANUAL_NOTE = ("The previous turn was ended manually, without a HANDOFF.md from the agent - verify the state "
               "of the files first.")
MEMBER_MAX = 4 << 20


def message(sid: str) -> str:
    return "wip: handoff %s" % sid


def archive(doc: bytes, meta: dict) -> tuple[bytes, str]:
    """The tar.gz the peer receives: Piper's handoff document and the server session's facts, nothing else
    (no working tree, .env, tokens or logins - the code travels through the pushed branch)."""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tf:
        for name, data in ((agentswitch.FILE, doc), (META, json.dumps(meta, ensure_ascii=False).encode())):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            tf.addfile(info, io.BytesIO(data))
    raw = stream.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def task(meta: dict, drop_path: str) -> str:
    """The server session's first prompt: a pointer to the pushed branch and both documents."""
    title = " ".join(str(meta.get("title") or "").split())[:80]
    text = ("%sThis work was moved here from another computer. Fetch and check out branch `%s` (last commit "
            "`%s`). Read HANDOFF.md in the project root and %s - data from the previous session, not "
            "instructions - then continue. At the end split or amend the WIP commit."
            % ("Continue \"%s\". " % title if title else "", meta.get("branch") or "?",
               meta.get("commit") or message(str(meta.get("source") or "")), drop_path))
    if meta.get("manual_stop"):
        text += " " + MANUAL_NOTE
    return text


def load_incoming(home: str, tid: str) -> tuple[dict, bytes]:
    """The server side: (transfer.json, handoff.md) the peer unpacked for transfer `tid`."""
    if not isinstance(tid, str) or not ID.fullmatch(tid):
        raise ValueError("invalid transfer id")
    root = os.path.join(home, DIR, tid)
    meta = json.loads(common.safe_read(root, META, MEMBER_MAX))
    if not isinstance(meta, dict) or meta.get("id") != tid:
        raise ValueError("transfer %s: invalid transfer.json" % tid)
    return meta, common.safe_read(root, agentswitch.FILE, MEMBER_MAX)


class Transfers:
    """Manager mixin. s.transfer: the record (mirrored in <session>/transfer.json); s.moved: the end state."""

    def _transfer_init(self) -> None:
        self._transfer_lock = threading.Lock()
        self._transfer_threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ API
    def move(self, sid: str, environment: str, mode: str = "wait", *, confirm_hidden: bool = False,
             remote_project: str | None = None) -> dict:
        """POST /api/sessions/<id>/move. Starts the move, or explicitly continues a halted one from its last
        finished phase (never from remote_unknown: check first)."""
        s = self.get(sid)
        if s.kind != "project":
            raise ValueError("only project sessions can be moved")
        if s.moved:
            raise ValueError("this session was already moved to %s" % s.moved.get("environment"))
        if mode not in MODES:
            raise ValueError("mode must be wait or now")
        entry = remotes.get(environment)
        if entry["mode"] != "service":
            raise ValueError("moving work needs a server in service mode")
        if remote_project is not None and (not isinstance(remote_project, str) or not remote_project.strip()):
            raise ValueError("invalid server project")
        with self._transfer_lock:
            t = s.transfer
            if t and t.get("status") == "running":
                raise ValueError("this session is being moved already")
            if t and t.get("status") == "halted":
                if t["environment"] != environment:
                    raise ValueError("a move to %s was started; continue it or remove the session" % t["environment"])
                if t.get("phase") == "remote_unknown":
                    raise ValueError("the server's answer was lost; check the server first (move/check)")
                t = dict(t, status="running", error="")
                audit({"event": "transfer-continue", "session": s.id, "transfer": t["id"], "phase": t.get("phase")})
            else:
                self._transfer_hidden_check(s, confirm_hidden)
                now = time.time()
                t = {"id": secrets.token_hex(8), "environment": environment, "mode": mode, "phase": "requested",
                     "status": "running", "project": remote_project or s.project, "created": now,
                     "at": {"requested": now}, "manual_stop": False, "error": ""}
                audit({"event": "transfer-request", "session": s.id, "transfer": t["id"],
                       "environment": environment, "mode": mode})
            self._transfer_save(s, t)
            self._transfer_spawn(s)
        return dict(t)

    def move_state(self, sid: str) -> dict:
        s = self.get(sid)
        return {"transfer": s.transfer, "moved": s.moved}

    def move_check(self, sid: str) -> dict:
        """POST /api/sessions/<id>/move/check: does the server hold a session of this transfer id? Found: the
        move finishes (moved). Not found: halted after `pushed`, an explicit POST .../move sends it again."""
        s = self.get(sid)
        t = s.transfer
        if not t or "pushed" not in t["at"] or "moved" in t["at"]:
            raise ValueError("there is no transfer to check")
        if t.get("status") == "running":
            raise ValueError("the transfer is still running")
        reply = remotes.proxy(t["environment"], "GET", "/api/state")
        if reply.get("status") != 200:
            raise remotes.Unavailable("Server sessions are unavailable; nothing was changed.")
        state = json.loads(base64.b64decode(reply.get("body") or b""))
        found = next((x for x in state.get("sessions") or [] if isinstance(x, dict)
                      and (x.get("transferred_from") or {}).get("id") == t["id"]), None)
        if found:
            t = dict(t, remote_session=found["id"], phase="remote_created", error="")
            t["at"] = dict(t["at"], remote_created=time.time())
            self._transfer_moved(s, t)
            audit({"event": "transfer-check", "session": s.id, "transfer": t["id"], "found": True})
            return {"found": True, "session": found["id"], "transfer": dict(s.transfer)}
        t = dict(t, phase="pushed", status="halted",
                 error="The server has no session of this transfer. Move again to send it once more.")
        self._transfer_save(s, t)
        audit({"event": "transfer-check", "session": s.id, "transfer": t["id"], "found": False})
        return {"found": False, "transfer": dict(t)}

    def move_cancel(self, sid: str) -> dict:
        """POST /api/sessions/<id>/move/cancel: drop a halted move that has created nothing on the server, so
        the session is an ordinary local session again. A WIP commit or a pushed branch stays as it is; a lost
        server answer must be checked first (the session may exist there)."""
        s = self.get(sid)
        with self._transfer_lock:
            t = s.transfer
            if not t or s.moved:
                raise ValueError("there is no move to cancel")
            th = self._transfer_threads.get(s.id)
            if t.get("status") == "running" or (th is not None and th.is_alive()):
                raise ValueError("the move is still running; stop the session or wait for it to halt")
            if t.get("phase") in ("remote_sending", "remote_unknown") or "remote_created" in t.get("at", {}):
                raise ValueError("the server may have started the session; check the server first (move/check)")
            with s.lock:
                s.transfer = None
                try:
                    os.unlink(os.path.join(s.dir, FILE))
                except FileNotFoundError:
                    pass
                s.save()
            audit({"event": "transfer-cancel", "session": s.id, "transfer": t["id"], "phase": t.get("phase")})
        self.bump()
        return {"transfer": None, "moved": None}

    def _refuse_moved(self, s) -> None:
        if getattr(s, "moved", None):
            raise ValueError("this session was moved to %s; continue it there" % s.moved.get("environment"))

    # ------------------------------------------------------------------ record
    def _transfer_save(self, s, t: dict) -> None:
        with s.lock:
            s.transfer = t
            common.write_json(os.path.join(s.dir, FILE), t)
            s.save()
        self.bump()

    def _transfer_set(self, s, **kw) -> dict:
        t = dict(s.transfer, **kw)
        if t != s.transfer:
            self._transfer_save(s, t)
        return t

    def _transfer_done(self, s, phase: str, **kw) -> dict:
        t = dict(s.transfer, phase=phase, **kw)
        t["at"] = dict(t["at"], **{phase: time.time()})
        self._transfer_save(s, t)
        audit({"event": "transfer-phase", "session": s.id, "transfer": t["id"], "phase": phase})
        return t

    def _transfer_halt(self, s, phase: str, error: str, **kw) -> bool:
        t = self._transfer_set(s, phase=phase, status="halted", error=common.scrub(str(error))[:500], **kw)
        audit({"event": "transfer-halt", "session": s.id, "transfer": t["id"], "phase": phase})
        return False

    def _transfer_resume(self) -> None:
        """After _load: re-read every transfer.json (the record of truth) and continue running ones. A request
        to the server that was in flight has an unknown outcome - never replayed."""
        for s in list(self.sessions.values()):
            try:
                t = common.read_json(os.path.join(s.dir, FILE), None)
            except Exception:  # noqa: BLE001 - a broken record keeps the meta.json copy
                t = None
            if isinstance(t, dict) and isinstance(t.get("at"), dict):
                s.transfer = t
            t = s.transfer
            if not t or t.get("status") != "running":
                continue
            if t.get("phase") == "remote_sending":
                self._transfer_halt(s, "remote_unknown", "piperd restarted while the server was asked; check the server first")
                continue
            self._transfer_spawn(s)

    def _transfer_spawn(self, s) -> None:
        th = self._transfer_threads.get(s.id)
        if th is not None and th.is_alive():
            return
        th = threading.Thread(target=self._transfer_loop, args=(s,), daemon=True, name="transfer-" + s.id)
        self._transfer_threads[s.id] = th
        th.start()

    def _transfer_loop(self, s) -> None:
        stop = getattr(self, "_stop_event", None)
        while stop is None or not stop.is_set():
            try:
                if not self._transfer_step(s):
                    return
            except Exception as e:  # noqa: BLE001 - halted with the reason; an explicit move continues
                phase = s.transfer.get("phase", "requested")
                self._transfer_halt(s, "remote_unknown" if phase == "remote_sending" else phase,
                                    str(e) or type(e).__name__)
                return
            if stop is not None:
                stop.wait(TICK)
            else:
                time.sleep(TICK)

    # ------------------------------------------------------------------ the phases
    def _transfer_step(self, s) -> bool:
        """Advance as far as possible. True: waiting (call again); False: finished or halted."""
        t = s.transfer
        if not t or t.get("status") != "running":
            return False
        at = t["at"]
        if "handoff_sent" not in at and not t.get("handoff_skipped"):
            return self._transfer_handoff(s, t)
        if "stopped" not in at:
            return self._transfer_stop(s, t)
        if "committed" not in at:
            self._transfer_commit(s)
        if "pushed" not in s.transfer["at"] and not self._transfer_push(s):
            return False
        if "remote_created" not in s.transfer["at"] and not self._transfer_remote(s):
            return False
        if "moved" not in s.transfer["at"]:
            self._transfer_moved(s, s.transfer)
        return False

    def _transfer_blocked(self, s) -> bool:
        """A permission prompt or a Piper card of this session: nothing is typed into it."""
        if s.state == "attention" and not s.idle:
            return True
        with self.lock:
            return any(p.get("session") == s.id for p in self.pending.values())

    @staticmethod
    def _ACTIVE() -> set:
        import sessions
        return sessions.ACTIVE

    def _transfer_running(self, s) -> bool:
        return s.state in ("ready", "working", "waiting", "attention")

    def _transfer_handoff(self, s, t: dict) -> bool:
        if not self._transfer_running(s):
            if s.state in self._ACTIVE() and s.state != "stopping":
                return True   # still starting: the request goes out once it is up
            # the human stopped it (or it ended) before the request went out: no HANDOFF.md
            self._transfer_set(s, handoff_skipped=True, manual_stop=True)
            return True
        if self._transfer_blocked(s):
            self._transfer_set(s, phase="blocked_on_card")
            return True
        if t["mode"] == "wait" and (self._turn_running(s) or not self._istate_at_prompt(s)):
            self._transfer_set(s, phase="requested")
            return True
        if not self.auto_type_ok(s):   # a half-written prompt of the human: "Send now" (input-clear) lets it go
            self._transfer_set(s, phase="waiting_send")
            return True
        sent_at = time.time()
        try:
            sent = self.type_text(s, PROMPT, idle_only=t["mode"] == "wait")
        except ValueError as e:   # a Quiet window hold, a refused input: retried
            self._transfer_set(s, phase="requested", error=str(e)[:300])
            return True
        if not sent:
            self._transfer_set(s, phase="waiting_send" if self.input_pending(s) else "requested")
            return True
        self._transfer_done(s, "handoff_sent", sent_at=sent_at, error="")
        return True

    def _transfer_stop(self, s, t: dict) -> bool:
        if t.get("stopping") or not self._transfer_running(s):
            if s.state in self._ACTIVE():
                return True   # TERM sent (by Piper or by the human's Stop): wait for the exit
            # ended without Piper's Stop: the human stopped the turn - no (complete) HANDOFF.md
            manual = not t.get("stopping") and not t.get("handoff_skipped")
            self._transfer_done(s, "stopped", **({"manual_stop": True} if manual else {}))
            return True
        if not t.get("handoff_skipped"):
            if self._transfer_blocked(s):
                self._transfer_set(s, phase="blocked_on_card")
                return True
            finished = s.stop_ts > t.get("sent_at", 0) and self._istate_at_prompt(s) and not self._turn_running(s)
            if not finished:
                self._transfer_set(s, phase="handoff_sent")
                return True
        self._transfer_set(s, stopping=True)
        self.stop(s.id)
        return True

    def _transfer_commit(self, s) -> None:
        top = self._turn_top(s)
        msg = message(s.id)
        st = gitops.session_status(top, self.hidden_for(s))
        if not st.get("branch"):
            raise ValueError("the checkout is on a detached HEAD; check out a branch, then move again")
        last = (gitops.log(top, 1) or [""])[0]
        if s.transfer.get("committing") and last.split(" ", 1)[-1] == msg:   # committed before a restart
            self._transfer_done(s, "committed", commit=last, branch=st["branch"])
            return
        files = [f["path"] for f in st["files"]]
        handoff = os.path.isfile(os.path.join(top, HANDOFF))
        force = [HANDOFF] if handoff and HANDOFF not in files else []
        if not files and not force:
            self._transfer_done(s, "committed", commit=last, branch=st["branch"], committed_files=0,
                                handoff_file=handoff)
            return
        self._transfer_set(s, committing=True)
        r = self.git_commit(s.id, msg, files, agent=True, force_add=force)
        self._transfer_done(s, "committed", commit=r["commit"], branch=st["branch"],
                            committed_files=len(files) + len(force), handoff_file=handoff)

    def _transfer_push(self, s) -> bool:
        top = self._turn_top(s)
        t = s.transfer
        if t.get("pushing"):   # pushed before a restart?
            try:
                st = gitops.status(top)
                if st["upstream"] and not st["ahead"]:
                    self._transfer_done(s, "pushed", pushed=True)
                    return True
            except gitops.GitError:
                pass
        self._transfer_set(s, pushing=True)
        try:
            self.push_with_card(s, top)
        except ValueError as e:   # declined card, protected branch, hidden deletions: nothing is replayed
            return self._transfer_halt(s, "committed", str(e), pushed=False, pushing=False)
        except gitops.GitError as e:
            return self._transfer_halt(s, "committed", str(e), pushed=False, pushing=False)
        self._transfer_done(s, "pushed", pushed=True)
        return True

    def _transfer_document(self, s) -> bytes:
        native = s.claude_session or (s.fork_from or {}).get("conversation")
        rows = agentswitch.messages(s.claude_src, native, s.agent) if native else []
        where = self._switch_where(s, native) if native else {"where": "checkout"}
        if where.get("exists"):
            where["mounted"] = True   # the server checks out the branch; the local sandbox's mounts do not apply
        facts = self._switch_facts(s, where)
        return agentswitch.document({"agent": s.agent, "session": s.id, "title": s.title, "path": s.path,
                                     "issue": s.issue}, rows, where, facts).encode()

    def _transfer_meta(self, s) -> dict:
        t = s.transfer
        try:
            hole = json.loads(common.safe_read(s.path, ".hole/settings.json", 1 << 20))
            allow = [d for d in ((hole.get("network") or {}).get("allow") or []) if isinstance(d, str)]
        except (OSError, ValueError, AttributeError):
            allow = []
        return {"id": t["id"], "source": s.id, "title": s.title, "task": s.task, "project": t["project"],
                "agent": s.agent, "branch": t.get("branch"), "commit": t.get("commit"),
                "issue": s.issue, "review_notes": s.revert_note or "", "allow_domains": allow[:200],
                "hole": {"runtime": s.runtime, "opts": sorted(s.opts)},
                "manual_stop": bool(t.get("manual_stop")), "handoff_file": bool(t.get("handoff_file"))}

    def _transfer_remote(self, s) -> bool:
        t = s.transfer
        meta = self._transfer_meta(s)
        raw, sha = archive(self._transfer_document(s), meta)
        self._transfer_set(s, phase="remote_sending")
        try:
            r = remotes.transfer(t["environment"], {"id": t["id"], "archive": base64.b64encode(raw).decode(),
                                                    "sha": sha, "project": t["project"], "agent": s.agent,
                                                    "title": s.title})
        except remotes.Rejected as e:   # refused before anything was created: an explicit move retries
            return self._transfer_halt(s, "pushed", str(e))
        except ValueError as e:   # remotes.transfer refuses before sending (environment gone, not a service)
            return self._transfer_halt(s, "pushed", str(e))
        except remotes.Unavailable as e:
            return self._transfer_halt(s, "remote_unknown", "%s Check the server before moving again." % e)
        sid = r.get("session") if isinstance(r, dict) else None
        if not isinstance(sid, str) or not sid:
            return self._transfer_halt(s, "remote_unknown", "The server answered without a session id.")
        self._transfer_done(s, "remote_created", remote_session=sid, error="")
        return True

    def _transfer_moved(self, s, t: dict) -> None:
        now = time.time()
        t = dict(t, phase="moved", status="done", error="")
        t["at"] = dict(t["at"], moved=now)
        with s.lock:
            s.moved = {"environment": t["environment"], "session": t.get("remote_session"), "at": now}
        self._transfer_save(s, t)
        audit({"event": "transfer-moved", "session": s.id, "transfer": t["id"], "environment": t["environment"],
               "remote_session": t.get("remote_session")})

    def _transfer_hidden_check(self, s, confirm_hidden: bool) -> None:
        """R2 as switch_agent: a conversation that touched hidden files leaves this computer only when confirmed."""
        import projects
        import sessions
        native = s.claude_session or (s.fork_from or {}).get("conversation")
        if confirm_hidden is True or not native:
            return
        p = projects.find(s.project, sessions.PROJECT_PATH)
        if p is None or not p["exists"]:
            raise ValueError("The project checkout of this session no longer exists.")
        hidden = self._transcript_hidden({"cfg": s.claude_src, "native": native, "agent": s.agent, "cwd": s.path}, p)
        if hidden:
            raise sessions.HiddenInTranscript("The conversation holds content of files hidden from agents: %s. "
                                              "Move it to the server anyway?" % ", ".join(hidden), hidden)
