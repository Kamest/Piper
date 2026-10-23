"""Every Piper test runs inside an OS sandbox: scripts/piper-sandbox.py starts it, this module
refuses a test process that is not in one.

The in-process guards of 15.28 / 15.31 (common._guard_test_writes ...) do not reach a test's children (git, rm -rf,
ijhttp, Chrome, docker); a kernel sandbox where only the run's own WORK dir is writable does. The check probes the
sandbox, it does not trust the marker alone:
  - every OS: the checkout is not writable, the host's Docker socket does not answer;
  - macOS (Seatbelt): a file in the real home cannot be created;
  - Linux (bwrap, the Docker fallback, the dind runner): every writable mount is a pseudo fs, a fresh tmpfs or the
    container's own root - or lies under WORK; host disks are read-only or not there;
  - CI (GitLab): the job's throwaway container is the sandbox (CI, GITLAB_CI, CI_JOB_ID and /.dockerenv) - and its
    writable mounts are only Docker volumes (the runner's /builds, /cache) and the job's Docker socket. A hole / Piper
    sandbox is a container too, but it binds the project from the host, so CI=true there is refused.
There is no switch to run without it (decision D2).

Stdlib only: imported by common.py (test processes), tests/piper-unit/_env.py, the e2e scripts, and run as
`python3 piper/testsandbox.py` by tests/piper-unit/_sandbox.mjs.
"""
from __future__ import annotations

import errno
import os
import re
import socket
import sys

MODES = ("seatbelt", "bwrap", "docker", "dind", "ci")
CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOW = "run the tests through the sandbox: task piper:test / task piper:e2e, or python3 scripts/piper-sandbox.py -- <cmd>"

# writable mounts that hold nothing of the host's
_PSEUDO = {"proc", "sysfs", "devpts", "mqueue", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "bpf",
           "pstore", "fusectl", "configfs", "hugetlbfs", "binfmt_misc", "autofs", "nsfs", "efivarfs", "selinuxfs"}
_FRESH = {"tmpfs", "devtmpfs", "ramfs"}            # fine when mounted fresh (root "/"), not a bind of a host dir
_DOCKER_FILES = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}   # the container's own files, Docker binds them
_VOLUME = re.compile(r"(^|/)volumes/[^/]+/_data(/|$)")   # a Docker volume (CI: /builds, /cache), never a host dir
_CI_SOCKETS = {"/var/run/docker.sock", "/run/docker.sock"}   # a CI job's own docker-in-docker socket


def _real_home() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


def _creatable(directory: str) -> bool | None:
    """True when a new file can be created in `directory` (it is removed again), False when the sandbox refuses,
    None when the directory is not there."""
    p = os.path.join(directory, ".piper-sbx-selfcheck-%d" % os.getpid())
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return True
    except OSError as e:
        if e.errno in (errno.EPERM, errno.EACCES, errno.EROFS):
            return False
        if e.errno == errno.ENOENT:
            return None
        raise
    os.close(fd)
    os.unlink(p)
    return True


def _connects(path: str) -> bool:
    if not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(2)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _unescape(s: str) -> str:   # mountinfo octal escapes: \040 = space
    return s.encode().decode("unicode_escape") if "\\" in s else s


def _under(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def mount_problems(mountinfo: str, work: str, ci: bool = False) -> list:
    """The writable mounts of /proc/self/mountinfo that could hold host data outside `work`. `ci`: a GitLab job
    container, whose Docker volumes and Docker socket are the runner's, not a person's files."""
    bad = []
    for line in mountinfo.splitlines():
        pre, sep, post = line.partition(" - ")
        f, g = pre.split(), post.split()
        if not sep or len(f) < 6 or len(g) < 1:
            continue
        root, point, opts, fstype = _unescape(f[3]), _unescape(f[4]), f[5].split(","), g[0]
        if "rw" not in opts or (work and _under(point, work)):
            continue
        if fstype in _PSEUDO:
            continue
        if fstype in _FRESH and root == "/":
            continue
        if fstype in _FRESH and point.startswith(("/dev/", "/proc/")):
            continue   # bwrap --dev: device nodes bound from the outer /dev; Docker masks /proc files with /dev/null
        if fstype == "overlay" and point == "/":   # a container's own root layer
            continue
        if point in _DOCKER_FILES:
            continue
        if ci and (_VOLUME.search(root) or point in _CI_SOCKETS):
            continue
        bad.append("%s (%s%s)" % (point, fstype, "" if root == "/" else ", bind of " + root))
    return bad


def problems(env=None, platform: str = sys.platform, home: str = "", checkout: str = CHECKOUT,
             mountinfo: str | None = None, dockerenv: bool | None = None) -> list:
    """Why this process is NOT in a Piper test sandbox; [] when it is. Parameters are for the tests."""
    env = os.environ if env is None else env
    dockerenv = os.path.exists("/.dockerenv") if dockerenv is None else dockerenv
    mode = env.get("TEST_SANDBOX", "")
    if mode == "ci" or (mode not in MODES and (env.get("CI") == "true" or env.get("GITLAB_CI") == "true")):
        if not (env.get("CI") == "true" and env.get("GITLAB_CI") == "true" and env.get("CI_JOB_ID") and dockerenv
                and platform.startswith("linux")):
            return ["CI=true outside a GitLab CI container"]
        if mountinfo is None:
            try:
                with open("/proc/self/mountinfo") as f:
                    mountinfo = f.read()
            except OSError as e:
                return ["cannot read /proc/self/mountinfo: %s" % e]
        return ["CI=true, but a writable host mount %s" % m for m in mount_problems(mountinfo, "", ci=True)]
    if mode not in MODES:
        return ["not started by scripts/piper-sandbox.py (TEST_SANDBOX=%r)" % mode]
    out = []
    work = os.path.realpath(env.get("TEST_SANDBOX_WORK", "")) if env.get("TEST_SANDBOX_WORK") else ""
    home = home or _real_home()
    if not work or work == "/" or _under(os.path.realpath(checkout), work) or _under(os.path.realpath(home), work):
        out.append("TEST_SANDBOX_WORK %r is missing or holds the checkout / the real home" % work)
        work = ""
    if _creatable(checkout):
        out.append("the checkout %s is writable" % checkout)
    for sock in ("/var/run/docker.sock", os.path.join(home, ".docker", "run", "docker.sock")):
        if _connects(sock):
            out.append("the host's Docker answers on %s" % sock)
    if platform == "darwin":
        if _creatable(home) is not False:
            out.append("a file can be created in the real home %s" % home)
    else:
        if mountinfo is None:
            try:
                with open("/proc/self/mountinfo") as f:
                    mountinfo = f.read()
            except OSError as e:
                return out + ["cannot read /proc/self/mountinfo: %s" % e]
        out += ["writable host mount %s" % m for m in mount_problems(mountinfo, work)]
    return out


def require() -> None:
    """Exit unless this process runs in a Piper test sandbox."""
    p = problems()
    if p:
        sys.stderr.write("piper tests: NOT in the test sandbox - refusing to run:\n  - %s\n%s\n"
                         % ("\n  - ".join(p), HOW))
        sys.stderr.flush()
        os._exit(3)   # no atexit cleanup: nothing of a test may run outside


if __name__ == "__main__":
    require()
