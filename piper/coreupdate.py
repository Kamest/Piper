"""Is a newer Piper release available, and can this checkout take it by itself? (the update prompt)

piperd checks in the background - shortly after start and every CHECK_EVERY seconds, never in a test process -
and /api/state only reads the cached answer, so a poll never waits for git or the network.

The prompt is offered only where `piper update` (`git pull --ff-only`) works: the checkout is on a branch with an
upstream, nothing is committed ahead of it and no tracked file is modified. A checkout detached at a tag or commit
has its version chosen by whoever placed it there (an extension's wrapper pins a release), so it is never offered
an update and is not checked over the network at all. The newest release is the highest `vX.Y.Z` tag of the
upstream remote, read with `git ls-remote` (git's own protocol, not the rate-limited GitHub REST API), compared
with the version of the files on disk; while piperd still runs older code than its checkout (codebuild's
`code_outdated`) the restart notice comes first and no update is offered.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time

import codebuild

CHECKOUT = os.path.dirname(codebuild.ROOT)
CHECK_EVERY = int(os.environ.get("PIPER_UPDATE_CHECK_EVERY", "21600"))   # 6 h; 0 = off
FIRST_CHECK = 60.0
_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_lock = threading.Lock()
_cached: dict = {}
_undeliverable: set[str] = set()   # versions a successful pull did not bring (the branch lacks the release tag)


def _git(root: str, *args: str, timeout: float = 20) -> tuple[int, str]:
    """git without the caller's GIT_* overrides, never prompting for credentials."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
    if "sshCommand" not in _ssh_config(env):   # an ssh remote never asks for a passphrase, password or host key
        env["GIT_SSH_COMMAND"] = "ssh -oBatchMode=yes"
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _ssh_config(env: dict) -> str:
    """"sshCommand" when git config sets core.sshCommand (it is kept as configured)."""
    try:
        r = subprocess.run(["git", "config", "--get", "core.sshCommand"], capture_output=True, text=True, timeout=5,
                           env=env, stdin=subprocess.DEVNULL)
        return "sshCommand" if r.returncode == 0 and r.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _version(text: str) -> tuple[int, int, int] | None:
    m = _TAG.fullmatch(text) or re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\+.*)?", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def local(root: str = CHECKOUT) -> dict:
    """Can `git pull --ff-only` update this checkout? {"updatable", "reason", "remote"}; reason is one of
    "", "not-git", "embedded" (inside another repository), "pinned" (detached HEAD), "no-upstream", "ahead", "modified"."""
    rc, top = _git(root, "rev-parse", "--show-toplevel", timeout=5)
    if rc or not top:
        return {"updatable": False, "reason": "not-git", "remote": ""}
    if os.path.realpath(top) != os.path.realpath(root):
        # Piper's files inside another repository (a project that bundles it): its tags and branch are not Piper's
        return {"updatable": False, "reason": "embedded", "remote": ""}
    rc, branch = _git(root, "symbolic-ref", "-q", "--short", "HEAD", timeout=5)
    if rc or not branch:
        return {"updatable": False, "reason": "pinned", "remote": ""}
    rc, remote = _git(root, "config", "--get", "branch.%s.remote" % branch, timeout=5)
    rc2, _ = _git(root, "rev-parse", "--verify", "-q", "@{upstream}", timeout=5)
    if rc or rc2 or not remote or remote == ".":
        return {"updatable": False, "reason": "no-upstream", "remote": ""}
    rc, ahead = _git(root, "rev-list", "--count", "@{upstream}..HEAD", timeout=10)
    if rc or ahead != "0":
        return {"updatable": False, "reason": "ahead", "remote": remote}
    rc, status = _git(root, "status", "--porcelain", "--untracked-files=no", timeout=10)
    if rc or status:
        return {"updatable": False, "reason": "modified", "remote": remote}
    return {"updatable": True, "reason": "", "remote": remote}


def remote_latest(root: str, remote: str) -> str:
    """The highest vX.Y.Z tag of `remote` ("" when there is none or it cannot be reached)."""
    rc, out = _git(root, "ls-remote", "--tags", "--refs", remote, "refs/tags/v*")
    if rc:
        return ""
    best = None
    for line in out.splitlines():
        name = line.rpartition("refs/tags/")[2]
        v = _version(name) if _TAG.fullmatch(name) else None
        if v and (best is None or v > best):
            best = v
    return "%d.%d.%d" % best if best else ""


def check(root: str = CHECKOUT, package: str | None = None) -> dict:
    """One check; the network only for an updatable checkout. Caches and returns the answer."""
    loc = local(root)
    current = codebuild.release_version(package or os.path.join(root, "piper")) or ""
    latest = remote_latest(root, loc["remote"]) if loc["updatable"] else ""
    answer = {"updatable": loc["updatable"], "reason": loc["reason"], "current": current, "latest": latest,
              "checked": time.time()}
    with _lock:
        _cached.clear()
        _cached.update(answer)
    return answer


def state(code_outdated: bool, can_update: bool) -> dict:
    """/api/state's `core_update`: `available` is the version to offer, "" when none may be offered."""
    with _lock:
        c = dict(_cached)
    current, latest = _version(c.get("current", "")), _version(c.get("latest", ""))
    offer = bool(c.get("updatable") and current and latest and latest > current and not code_outdated
                 and c.get("latest") not in _undeliverable)
    return {"available": c["latest"] if offer else "", "current": c.get("current", ""),
            "updatable": bool(c.get("updatable")), "reason": c.get("reason", ""), "can_update": bool(can_update)}


def update(root: str = CHECKOUT) -> tuple[bool, str]:
    """`git pull --ff-only` under the same rule as the offer: (ok, message)."""
    loc = local(root)
    if not loc["updatable"]:
        return False, {"pinned": "This Piper is pinned to a release by whoever installed it; update it there.",
                       "no-upstream": "This Piper's branch has no upstream to update from.",
                       "ahead": "This Piper has commits that are not pushed; update it yourself.",
                       "modified": "This Piper has modified files; update it yourself.",
                       "embedded": "This Piper is part of another repository; update it there.",
                       }.get(loc["reason"], "This Piper is not a git checkout; update it the way it was installed.")
    with _lock:
        latest = _cached.get("latest", "")
    rc, _ = _git(root, "pull", "--ff-only", "-q", timeout=180)
    if rc:
        return False, "git pull --ff-only failed; nothing was changed. Run piper update in a terminal to see why."
    now = _version(codebuild.release_version(os.path.join(root, "piper")) or "")
    if _version(latest) and (not now or now < _version(latest)):
        # the release tag is not on this checkout's branch: never offer it again in this piperd
        with _lock:
            _undeliverable.add(latest)
        return False, "This Piper's branch does not have Piper %s yet; switch to main to get releases." % latest
    with _lock:
        _cached["updatable"] = False   # until the restarted piperd checks again
    return True, ""


def _loop() -> None:
    time.sleep(FIRST_CHECK)
    while True:
        try:
            check()
        except Exception as e:  # noqa: BLE001 - a failed check never stops piperd
            print("piperd: update check failed: %s" % type(e).__name__, flush=True)
        time.sleep(CHECK_EVERY)


def start_thread(testing: bool) -> None:
    if CHECK_EVERY > 0 and not testing:
        threading.Thread(target=_loop, daemon=True, name="piper-update-check").start()
