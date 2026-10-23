"""The dev stack panel's format: what a stack provider returns, turned into what the
GUI's Stack section shows.

Piper's core runs no stack itself. An extension (extensions.py) supplies a provider through its
`stack_provider(manager)`: an object with
  worktree(path) -> {"root", "name"?} | None    the stack whose root is the checkout or contains it
  status(root) -> {"services": [{service, state, status, location?}], "ps_err"?}
  dash(root) -> card | None                     per-service verbs, primary action, links (dash_view)
  verb(root, verb, service) -> job              one of VERBS; restart(root, service) the same
  logs(root, service, tail) -> str
  jobs() -> [job]   job(id) -> job   job_log(id, offset) -> {offset, chunk, state}   cancel(id) -> {state}
  tunnel(root, env, target, port) -> job        a port-forward to a test/prod target (dbtools, httpclient):
                                                job(id)["tunnel"] = {state: connecting|open|signin|failed, local, url}
  up() -> bool                                  answers now (cheap, cached: /api/state asks on every poll)
  dashboard_url() -> str                        the provider's own page ("" = none)
  NO_STACK = "..."                              what to do when worktree() finds nothing
A provider raises StackError when it refuses or cannot be reached.
"""
from __future__ import annotations

import os
import re

SERVICE_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
JOB_RX = re.compile(r"^[0-9a-f]{6,32}$")
VERBS = ("start", "stop", "restart", "down", "up", "build", "rebuild")
JOB_RECENT = 120   # a finished job of the stack is still shown this many seconds after its end
JOBS_SHOWN = 5


class StackError(ValueError):
    """The stack provider refused or is unreachable (piperd: 409, or an `error` text in the status)."""


def service_rows(st: dict) -> list[dict]:
    """The provider's service rows -> StackService {name, state, health?, age?}."""
    out = []
    for s in st.get("services") or []:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status") or "")
        row = {"name": str(s.get("service") or s.get("container") or "?"), "state": str(s.get("state") or "")}
        m = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
        if m:
            row["health"] = "starting" if m.group(1) == "health: starting" else m.group(1)
        m = re.match(r"^(?:Up|Exited \(\d+\))\s+(.+?)(?:\s+\(.*\))?(?:\s+ago)?$", status)
        if m:
            row["age"] = m.group(1)
        if s.get("location") and s.get("location") != "host":
            row["location"] = str(s["location"])
        _exit_code(row, status)
        out.append(row)
    return out


def _exit_code(row: dict, status: str) -> None:
    """`Exited (137) 2 minutes ago` -> exitCode 137: a container that ended or was killed is not a stopped one."""
    m = re.match(r"^Exited \((\d+)\)", status or "")
    if m and int(m.group(1)):
        row["exitCode"] = int(m.group(1))


def _health(status: str) -> str | None:
    m = re.search(r"\((healthy|unhealthy|health: starting)\)", status or "")
    return None if not m else "starting" if m.group(1) == "health: starting" else m.group(1)


def dash_view(st: dict) -> dict:
    """the provider's dashboard card -> the StackStatus extras: per-service verbs and links, the
    primary action and the overflow - only verbs in VERBS survive (the rest is the dashboard's)."""
    rows = []
    for s in st.get("services") or []:
        if not isinstance(s, dict):
            continue
        row = {"name": str(s.get("service") or "?"), "state": str(s.get("state") or ""),
               "verbs": [v for v in s.get("verbs") or [] if v in VERBS]}
        h = _health(str(s.get("status") or ""))
        if h:
            row["health"] = h
        if s.get("age"):
            row["age"] = str(s["age"])
        if s.get("location") and s.get("location") != "host":
            row["location"] = str(s["location"])
        if s.get("oneShot"):
            row["oneShot"] = True
        _exit_code(row, str(s.get("status") or ""))
        # the provider's card: [{host, target}] - the host port is what a client connects to
        ports = [str(p.get("host")) if isinstance(p, dict) else str(p) for p in s.get("ports") or []]
        ports = [p for p in dict.fromkeys(ports) if p and p != "None"]
        if ports:
            row["ports"] = ports[:10]
        links = [str(u) for u in s.get("links") or [] if str(u).startswith(("http://", "https://"))]
        if links:
            row["links"] = links[:5]
        rows.append(row)
    pr = st.get("primary") if isinstance(st.get("primary"), dict) else None
    out = {"services": rows, "running": int(st.get("running") or 0), "total": int(st.get("total") or 0),
           "primary": {"verb": pr["verb"], "label": str(pr.get("label") or pr["verb"])}
           if pr and pr.get("verb") in VERBS else None,
           "verbs": [v for v in st.get("verbs") or [] if v in VERBS],
           "domainBase": str(st.get("domainBase") or ""), "sandbox": bool(st.get("sandbox"))}
    clock = st.get("clock") if isinstance(st.get("clock"), dict) else {}
    if clock.get("value"):
        out["clock"] = {"kind": str(clock.get("kind") or ""), "value": str(clock["value"])}
    if st.get("psErr"):
        out["error"] = str(st["psErr"])
    elif st.get("error"):
        out["error"] = str(st["error"])
    return out


def job_view(j: dict) -> dict:
    """the provider's job JSON -> StackJob (nothing of the job's argv or log path). `services`: what a targeted
    verb names ([] = the whole stack, or a provider too old to say)."""
    svcs = j.get("services") if isinstance(j.get("services"), list) else []
    return {"id": str(j.get("id") or ""), "verb": str(j.get("verb") or ""), "state": str(j.get("state") or ""),
            "stage": str(j.get("stage") or ""), "rc": j.get("rc") if isinstance(j.get("rc"), int) else None,
            "error": str(j.get("error") or "")[-1500:], "started": int(j.get("started") or 0),
            "services": [str(x) for x in svcs if SERVICE_RX.match(str(x))][:20]}


def stack_jobs(jobs: list[dict], root: str, now: float, seen: dict) -> list[dict]:
    """The jobs to show for this stack, newest first: every running one, and those that ended in
    the last JOB_RECENT seconds. the provider's job JSON has no end time, so `seen` (the caller's, kept
    across polls: id -> None while seen running, then the time it was first seen finished) supplies
    it; a job never seen running counts from its start."""
    me = os.path.realpath(root)
    mine = [j for j in jobs if j.get("root") and os.path.realpath(str(j["root"])) == me
            and str(j.get("verb") or "") in VERBS and j.get("id")]
    out = []
    for j in mine:
        jid, started = str(j["id"]), int(j.get("started") or 0)
        if j.get("state") == "running":
            seen[jid] = None
            out.append(j)
            continue
        if jid in seen and seen[jid] is None:
            seen[jid] = now
        end = seen[jid] if isinstance(seen.get(jid), (int, float)) else started
        if now - end < JOB_RECENT:
            out.append(j)
    if len(seen) > 500:   # ids the provider pruned long ago
        for k in list(seen)[:len(seen) - 500]:
            seen.pop(k, None)
    out.sort(key=lambda j: (j.get("state") != "running", -int(j.get("started") or 0)))
    return [job_view(j) for j in out[:JOBS_SHOWN]]


def check_job(jid: str) -> str:
    if not JOB_RX.match(jid or ""):
        raise ValueError("invalid job id %r" % jid)
    return jid


def check_service(name: str) -> str:
    if name and not SERVICE_RX.match(name):
        raise ValueError("invalid service name %r" % name)
    return name
