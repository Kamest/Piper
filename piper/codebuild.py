"""Is the running piperd older than its checkout? (9.171)

After a `git pull` the running piperd still executes the Python it imported at start, while it already
serves the new GUI files from disk. piperd fingerprints its own piper/*.py once at start (`LOADED`) and
compares that with the files now on disk (`on_disk()`, re-hashed only when a file's mtime / size changed):
/api/state reports `code_build` (the loaded one), `code_disk` and `code_outdated`. A daemon from before 9.171 has neither.

`python3 codebuild.py check <base-url>` (scripts/piper.sh, its open / ensure / status): exit 0 = restart the
daemon - it answered /api/state (the `tty` token, like `piper attach`) with `code_outdated` true, or without
the field (a pre-9.171 daemon). Exit 1 otherwise, also on any error (no answer, 401: say nothing). One
attempt per daemon state: `$PIPER_HOME/update-restart` holds what was answered when the CLI last restarted,
the same answer again means the restart did not help (another checkout's daemon, a foreground `serve`) and
is not retried; an up-to-date answer drops the file.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def _files(root: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, "*.py")))


def fingerprint(root: str = ROOT) -> str:
    """The hash of piper/*.py (names and contents): the Python a piperd started from this checkout runs."""
    digest = hashlib.sha256()
    for path in _files(root):
        digest.update(os.path.basename(path).encode() + b"\0")
        try:
            with open(path, "rb") as f:
                digest.update(f.read())
        except OSError:
            digest.update(b"?")
    return digest.hexdigest()[:20]


def release_version(root: str = ROOT) -> str | None:
    """Release/build identity at process start, separate from the numeric API protocol version."""
    try:
        result = subprocess.run(
            ["git", "-C", os.path.dirname(root), "describe", "--tags", "--match", "v[0-9]*",
             "--long", "--dirty"], capture_output=True, text=True, timeout=2,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        if result.returncode:
            return None
        match = re.fullmatch(r"v(\d+\.\d+\.\d+)-(\d+)-g([0-9a-f]+)(-dirty)?", result.stdout.strip())
        if not match:
            return None
        version, distance, revision, dirty = match.groups()
        build = ([distance, "g" + revision] if int(distance) else []) + (["modified"] if dirty else [])
        return version + ("+" + ".".join(build) if build else "")
    except (OSError, subprocess.SubprocessError):
        return None


LOADED = fingerprint()   # at import: what this process runs
RELEASE = release_version()   # never relabel a running daemon after a pull or checkout change
_cache: tuple = (None, "", 0.0)


def on_disk(root: str = ROOT, ttl: float = 5.0) -> str:
    """fingerprint() of the files now, re-hashed only when one's mtime / size / the set changed."""
    global _cache
    now = time.monotonic()
    if _cache[0] is not None and now - _cache[2] < ttl:
        return _cache[1]
    stamp = []
    for path in _files(root):
        try:
            st = os.stat(path)
            stamp.append((path, st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((path, 0, 0))
    stamp = tuple(stamp)
    value = _cache[1] if stamp == _cache[0] else fingerprint(root)
    _cache = (stamp, value, now)
    return value


def state_fields() -> dict:
    disk = on_disk()
    return {"code_build": LOADED, "code_disk": disk, "code_outdated": disk != LOADED,
            "service_version": RELEASE}


def check(base: str, home: str) -> bool:
    """True = the daemon at `base` runs older code than its checkout and was not restarted for it yet."""
    import urllib.request
    sys.path.insert(0, ROOT)
    import common
    try:
        req = urllib.request.Request(base.rstrip("/") + "/api/state",
                                     headers={"Authorization": "Bearer " + common.own_token("tty")})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    marker = os.path.join(home, "update-restart")
    if "code_outdated" in data and not data["code_outdated"]:
        try:
            os.unlink(marker)
        except OSError:
            pass
        return False
    answer = "outdated %s %s" % (data.get("code_build"), data.get("code_disk")) if "code_outdated" in data else "pre-9.171 %s" % data.get("gui_build", "")
    try:
        with open(marker) as f:
            if f.read().strip() == answer:
                return False
    except OSError:
        pass
    try:
        with open(marker, "w") as f:
            f.write(answer + "\n")
    except OSError:
        return False   # no marker = no loop guard: better no restart than one per command
    return True


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "check":
        sys.exit(0 if check(sys.argv[2], os.environ.get("PIPER_HOME") or os.path.expanduser("~/.piper")) else 1)
    print(LOADED)
