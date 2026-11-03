"""Settings > Extensions behind piperd: the list, trust / untrust / decline / add, the
requests a remote GUI or a phone makes, and the detached service reinstall.

Who may do what is decided by piperd (`Handler._ext_caller`): "local" = the GUI token on this computer's
loopback, "remote" = a full-scope token through `tailscale serve`, "phone" = the mobile bridge. Agents,
MCP, `piper attach` and scoped tokens never reach this module. Turning an extension ON runs its code on the
host outside any sandbox, so only "local" applies a trust or an added folder; the others leave a request
(PENDING) that the local GUI approves or denies. Turning one OFF (untrust, decline) only removes capability
and is applied for every caller. Every change is audited (path, git origin, commit, who).
"""
from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

import common
import extensions
import extfind

PENDING_TTL = 3600.0
PENDING_MAX = 20
_LOCK = threading.Lock()
PENDING: dict[str, dict] = {}   # id -> {id, kind, path, label, remote, commit, from, at, owner}


class Conflict(Exception):
    """The request no longer matches what it showed (HTTP 409)."""


def _expire(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for k in [k for k, p in PENDING.items() if now - p["at"] > PENDING_TTL]:
        PENDING.pop(k, None)


def pending_count() -> int:
    with _LOCK:
        _expire()
        return len(PENDING)


def _state(e: dict, declined: dict) -> str:
    if e["path"] in extensions.DIRS:
        return "loaded"
    if e["trusted"] and e.get("origin_changed"):
        return "origin-changed"
    if not e["compatible"]:
        return "incompatible"
    if e["path"] in extensions.FAILED:
        return "failed"
    if not e["trusted"] and e["path"] in declined:
        return "declined"
    return "found"


def _entry(e: dict, declined: dict) -> dict:
    state = _state(e, declined)
    return {"name": e["name"], "label": e["label"], "description": e.get("description", ""), "path": e["path"],
            "remote": e.get("remote", ""), "commit": e.get("commit", ""), "source": e.get("source", "scan"),
            "api": e["api"], "compatible": e["compatible"], "trusted": e["trusted"], "state": state,
            "error": extensions.FAILED.get(e["path"], ""), "adds": extensions.adds(e["path"]),
            "trusted_remote": e.get("trusted_remote", ""),
            # trusted but not running yet, or running but no longer trusted: a restart applies it
            "restart_needed": extensions.SOURCE == "trust" and (
                (state == "found" and e["trusted"]) or (state == "loaded" and not e["trusted"]))}


def listing(owner: str = "") -> dict:
    """GET /api/extensions. `owner` marks the caller's own requests (`mine`), so it can cancel them."""
    found = extfind.discover()
    declined = extfind.declined()
    out = [_entry(e, declined) for e in found]
    seen = {e["path"] for e in out}
    for d in extensions.DIRS:   # loaded from $PIPER_EXTENSIONS outside every scan root
        if d not in seen:
            try:
                info = extfind.manifest(d)
            except (OSError, ValueError, RuntimeError):
                info = {"name": os.path.basename(d), "label": os.path.basename(d), "path": d, "api": 1,
                        "compatible": True}
            info.update(source="env", trusted=False, remote="", commit="")
            out.append(_entry(info, declined))
    offer = []
    if not any(e["trusted"] for e in found) and not extensions.DIRS:
        offer = [e["path"] for e in extfind.offerable()]   # never trusted, declined or incompatible
    with _LOCK:
        _expire()
        pending = [dict({k: v for k, v in p.items() if k != "owner"}, mine=bool(owner) and p["owner"] == owner)
                   for p in sorted(PENDING.values(), key=lambda p: p["at"])]
    return {"extensions": out, "pending": pending, "offer": offer, "core_api": extensions.API,
            "apis": list(extensions.APIS), "source": extensions.SOURCE,
            "restart_needed": any(e["restart_needed"] for e in out)}


def _path(body: dict) -> str:
    path = body.get("path")
    if not isinstance(path, str) or not path.strip() or "\0" in path or len(path) > 4096:
        raise ValueError("path must be an extension directory")
    return path


def _audit(event: str, info: dict, who: str, **extra) -> None:
    common.audit(dict({"event": event, "path": info.get("path", ""), "remote": info.get("remote", ""),
                       "commit": info.get("commit", ""), "who": who}, **extra))


def _origin(path: str) -> dict:
    remote, commit = extfind._git(path, time.monotonic() + 1.0)
    return {"path": path, "remote": remote, "commit": commit}


def trust(body: dict, who: str) -> dict:
    """The local GUI's "Turn on": checks the manifest, records trust with the git origin."""
    info = extfind.manifest(_path(body))
    if not info["compatible"]:
        raise ValueError("extension API %s is incompatible with Piper API %s" % (info["api"], extensions.API))
    extfind.set_trust(info["path"], True)
    _audit("extension-trust", _origin(info["path"]), who)
    return {"ok": True, "path": info["path"], "restart": True}


def untrust(body: dict, who: str) -> dict:
    """Turn off (every caller): the trust goes, the path is remembered as declined (no first-start card)."""
    path = str(Path(_path(body)).expanduser().resolve())
    extfind.set_trust(path, False)
    _audit("extension-untrust", _origin(path), who)
    return {"ok": True, "path": path, "restart": True}


def decline(body: dict, who: str) -> dict:
    """The first-start card's "Not now": never offered again (trusting later still works)."""
    path = str(Path(_path(body)).expanduser().resolve())
    extfind.set_declined(path, True)
    _audit("extension-decline", {"path": path}, who)
    return {"ok": True, "path": path}


def add(body: dict, who: str) -> dict:
    """An explicit folder outside the scan roots: a valid manifest is registered (no trust)."""
    info = extfind.register(_path(body))
    _audit("extension-add", _origin(info["path"]), who)
    return {"ok": True, "path": info["path"]}


def request(kind: str, body: dict, who: str, owner: str) -> dict:
    """A remote GUI's or phone's trust / add: kept for the local GUI to approve (never applied here)."""
    info = extfind.manifest(_path(body))
    if kind == "trust" and not info["compatible"]:
        raise ValueError("extension API %s is incompatible with Piper API %s" % (info["api"], extensions.API))
    origin = _origin(info["path"])
    with _LOCK:
        _expire()
        for p in PENDING.values():
            if p["path"] == info["path"] and p["kind"] == kind and p["owner"] == owner:   # the same asker again
                p.update(at=time.time(), remote=origin["remote"], commit=origin["commit"], **{"from": who})
                break
        else:
            if len(PENDING) >= PENDING_MAX:
                raise OverflowError("too many waiting requests - approve or deny them on the computer first")
            p = {"id": secrets.token_hex(8), "kind": kind, "path": info["path"], "label": info["label"],
                 "remote": origin["remote"], "commit": origin["commit"], "from": who, "at": time.time(),
                 "owner": owner}
            PENDING[p["id"]] = p
        out = {k: v for k, v in p.items() if k != "owner"}
    _audit("extension-request", origin, who, kind=kind, id=out["id"])
    return out


def _take(rid) -> dict:
    if not isinstance(rid, str):
        raise KeyError(rid)
    with _LOCK:
        _expire()
        return PENDING.pop(rid)


def approve(body: dict) -> dict:
    """Local GUI only: apply a waiting request as if it was made here."""
    rid = body.get("id")
    with _LOCK:
        _expire()
        p = PENDING.get(rid) if isinstance(rid, str) else None
        if p is None:
            raise KeyError(rid)
        now = _origin(p["path"])["remote"]
        if p["remote"] and now != p["remote"]:   # the approver saw the old origin: never trust a different one
            PENDING.pop(rid)
            raise Conflict("the extension's origin changed since the request (%s, now %s) - ask again"
                           % (p["remote"], now or "none"))
        PENDING.pop(rid)
    who = "local (approved %s request %s)" % (p["from"], p["id"])
    if p["kind"] == "trust":
        return trust({"path": p["path"]}, who)
    return add({"path": p["path"]}, who)


def deny(body: dict) -> dict:
    p = _take(body.get("id"))
    _audit("extension-request-denied", p, "gui", id=p["id"])
    return {"ok": True}


def cancel(body: dict, owner: str, local: bool) -> dict:
    """The requester withdraws its own request (the local GUI may withdraw any)."""
    rid = body.get("id")
    with _LOCK:
        p = PENDING.get(rid) if isinstance(rid, str) else None
        if p is None:
            raise KeyError(rid)
        if not local and p["owner"] != owner:
            raise PermissionError("not your request")
        PENDING.pop(rid)
    _audit("extension-request-cancelled", p, "gui" if local else p["from"], id=p["id"])
    return {"ok": True}


def reinstall_service(who: str) -> dict:
    """`piper install-service` detached in its own session: it may end this piperd (an older label is
    booted out, the new one started) and must outlive it. Output goes to piperd.log."""
    script = os.path.join(common.SK, "scripts", "piper.sh")
    log = os.path.join(common.PIPER_HOME, "piperd.log")
    with open(log, "ab") as out:
        proc = subprocess.Popen(["sh", script, "install-service"], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=out, start_new_session=True, close_fds=True,
                                env=dict(os.environ, PIPER_HOME=common.PIPER_HOME))
    common.audit({"event": "service-reinstall", "who": who, "pid": proc.pid})
    return {"ok": True, "started": True}


def legacy_service_label(pid: int | None = None) -> str:
    """9.171: the legacy label (a loaded extension's legacy_labels) this very process runs under as a login
    service - checked with launchd / systemd (their pid of that label is ours), so an inherited
    XPC_SERVICE_NAME or cgroup of a child started inside such a service never counts. "" otherwise."""
    target = os.environ.get("PIPER_SERVICE_LABEL") or "piperd"
    legacy = set()
    for d in extensions.DIRS:
        try:
            legacy.update(extfind.manifest(d)["legacy_labels"])
        except (OSError, ValueError, RuntimeError):
            continue
    pid = os.getpid() if pid is None else pid
    for label in extfind.running_labels():
        if label != target and label in legacy and extfind.service_main_pid(label) == pid:
            return label
    return ""


def migrate_service(delay: float = 3.0) -> str:
    """At start under an old label (a reboot or crash after the pull: launchd / systemd started the new code
    under the user's old service): `install-service` once, detached, after `delay` (the server listens
    first). It installs `piperd`, removes the old label (that ends this process; ptyhost and the sessions
    keep running) - the next start runs under `piperd`, so it happens once."""
    label = legacy_service_label()
    if not label:
        return ""

    def later():
        time.sleep(delay)
        common.audit({"event": "service-migrate", "label": label, "who": "piperd"})
        print("piperd: runs as the old service %s - installing the service piperd (sessions keep running)" % label,
              flush=True)
        reinstall_service("piperd")
    threading.Thread(target=later, name="service-migrate", daemon=True).start()
    return label


def drop_applied_env() -> None:
    """Before a self-restart: the env_file values this process took go, so an extension turned off no
    longer leaves its environment behind (the re-executed piperd applies the current ones again)."""
    for k, v in extensions.APPLIED_ENV.items():
        if os.environ.get(k) == v:
            os.environ.pop(k, None)


def restart_argv() -> list[str]:
    return [sys.executable] + sys.argv
