#!/usr/bin/env python3
"""piper-sandbox.py - run a Piper test command in an OS sandbox.

Every Piper test runs through it (task piper:test / piper:e2e); the tests refuse to run outside one
(piper/testsandbox.py) and there is no switch to skip it.

    python3 scripts/piper-sandbox.py [--] <cmd...>         unit, mjs, run.py, http_e2e, gui.py
    python3 scripts/piper-sandbox.py --dind [--] <cmd...>  the Docker suites (stack.py, dbprod): own dind daemon
    python3 scripts/piper-sandbox.py --selftest            attack probes inside, judged from outside

The sandbox, chosen by the OS:
  macOS      sandbox-exec + scripts/piper-sandbox.sb: writable only WORK; the real home unreadable except the checkout
             and read-only toolchains; network only loopback (not the live piperd / dev stack daemon); no docker, open, launchctl
  Linux      bwrap: / read-only, empty tmpfs over $HOME /tmp /run /var/tmp, writable only WORK, own pid/net/ipc ns;
             without a working bwrap (or PIPER_SANDBOX_MODE=docker): a container, the checkout read-only, no network
  GitLab CI  the job's throwaway container is the sandbox: the command runs directly
  --dind     a privileged docker:27-dind on the host's Docker + a runner container in its network namespace: the
             tests' docker calls reach only that daemon (PIPER_SANDBOX_IMAGES loaded into it from the host, never
             pulled), the checkout is bound read-only, nothing else of the host
Every run gets a fresh WORK (HOME=WORK/home, TMPDIR=WORK/tmp, cwd WORK) and a cleared environment (PASS below);
what it started is killed at the end. WORK is removed after a passing run, kept (path printed) after a failing one.
Env: PIPER_SANDBOX_READ=<dir>:<dir> more read-only dirs (macOS / bwrap); PIPER_SANDBOX_X11=1 the X display (bwrap).
Run it outside any other sandbox (a Seatbelt cannot be nested: in Claude Code, dangerouslyDisableSandbox).
"""
from __future__ import annotations

import hashlib
import os
import pwd
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

SK = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOME = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
DARWIN = sys.platform == "darwin"
BASE = "/private/tmp" if DARWIN else "/tmp"   # short: AF_UNIX paths end at 104 bytes
PASS = {"LANG", "TERM", "USER", "LOGNAME", "SHELL", "TZ", "NO_COLOR", "COLUMNS", "JAVA_HOME", "PIPER_IJHTTP",
        "PIPER_UNIT_JOBS", "AGENT_BROWSER", "AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_ARGS", "DBSEED_SUFFIX", "WITH"}
PASS_PREFIX = ("LC_", "N6_FUZZ_", "DBPROD_", "PIPER_E2E_")
GITCONFIG = "[user]\n\tname = Piper Test\n\temail = piper-test@example.invalid\n"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LINUX_IMAGE = "piper-sandbox-linux:2"
RUNNER_IMAGE = "piper-sandbox-runner:2"
DIND_IMAGE = "docker:27-dind"
SCOPE_SHIM = os.path.join(SK, "tests", "piper-e2e", "docker-scope.py")


def die(msg: str) -> None:
    sys.exit("piper-sandbox: " + msg)


def in_ci() -> bool:
    """a real GitLab job container (testsandbox's CI check, mounts included); CI=true spoofed in a hole sandbox
    falls through to the normal sandbox"""
    if not (os.environ.get("CI") == "true" and os.environ.get("GITLAB_CI") == "true"):
        return False
    sys.path.insert(0, os.path.join(SK, "piper"))
    import testsandbox
    return not testsandbox.problems(env=dict(os.environ))


def new_work() -> str:
    w = os.path.realpath(tempfile.mkdtemp(prefix="psbx-", dir=BASE))
    os.makedirs(os.path.join(w, "home", ".config", "git"))
    os.makedirs(os.path.join(w, "tmp"))
    # a git identity as CI's `git config --global` (piperd's commits in the e2e need one)
    with open(os.path.join(w, "home", ".config", "git", "config"), "w") as f:
        f.write(GITCONFIG)
    return w


def remove_work(w: str, keep: bool) -> None:
    if not w.startswith(BASE + "/psbx-") or "/" in w[len(BASE) + 1:]:
        return
    if keep:
        print("piper-sandbox: kept %s" % w, file=sys.stderr)
        return

    # runs on the host, unsandboxed: a test may have left a read-only dir with a symlink out (chmod follows it) -
    # unlink links, chmod only real dirs / files inside w, never w's parent
    retried = set()

    def onerror(func, path, _exc):
        try:
            for p in (os.path.dirname(path), path):
                if (p.startswith(w + "/") or p == w) and not os.path.islink(p):
                    os.chmod(p, 0o700)
            if func in (os.unlink, os.remove, os.rmdir) or os.path.islink(path):
                (os.rmdir if func is os.rmdir and not os.path.islink(path) else os.unlink)(path)
            elif path not in retried and os.path.isdir(path):   # could not be opened / listed: once more
                retried.add(path)
                shutil.rmtree(path, onerror=onerror)
        except OSError:
            pass
    shutil.rmtree(w, onerror=onerror)


def sandbox_env(w: str, mode: str, home: str = "", path: bool = True) -> dict:
    home = home or w
    e = {k: v for k, v in os.environ.items() if k in PASS or k.startswith(PASS_PREFIX)}
    if path:
        e["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
    e.update({"HOME": home + "/home", "TMPDIR": home + "/tmp", "TEST_SANDBOX": mode, "TEST_SANDBOX_WORK": home,
              "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1", "npm_config_cache": home + "/npm",
              "AGENT_BROWSER_PROFILE": home + "/chrome"})
    e.setdefault("AGENT_BROWSER_ARGS", "--no-sandbox")   # Chrome's own sandbox cannot nest in this one
    if DARWIN and "AGENT_BROWSER_EXECUTABLE_PATH" not in e and os.access(CHROME, os.X_OK):
        e["AGENT_BROWSER_EXECUTABLE_PATH"] = CHROME   # agent-browser's own download lives in the (hidden) home
    return e


def extra_reads() -> list:
    """Dirs a test reads besides the checkout: toolchains under the (otherwise hidden) home."""
    cand = [p for p in os.environ.get("PIPER_SANDBOX_READ", "").split(":") if p]
    if os.environ.get("JAVA_HOME"):
        cand.append(os.environ["JAVA_HOME"])
    if os.environ.get("PIPER_IJHTTP"):
        cand.append(os.path.dirname(os.path.realpath(os.environ["PIPER_IJHTTP"])))
    for b in ("python3", "node", "git", "task", "agent-browser", "java", os.environ.get("AGENT_BROWSER", "")):
        p = b and shutil.which(b)
        if p:
            p = os.path.realpath(p)
            if p.startswith(HOME + "/"):
                cand.append(os.path.dirname(os.path.dirname(p)))
    out = []
    for d in cand:
        d = os.path.realpath(d)
        if d in ("/", HOME) or (HOME + "/").startswith(d + "/"):
            die("never readable as a whole: %s" % d)
        if os.path.exists(d) and d not in out:
            out.append(d)
    return out


def x11_display() -> tuple:
    """PIPER_SANDBOX_X11=1 (an Electron e2e under xvfb-run, bwrap only): the display's socket dir and cookie come
    back read-only over the tmpfs /tmp, DISPLAY/XAUTHORITY are passed. Opt-in: by default no test sees a display."""
    if os.environ.get("PIPER_SANDBOX_X11") != "1" or not os.environ.get("DISPLAY"):
        return [], {}
    binds, env = [], {"DISPLAY": os.environ["DISPLAY"]}
    if os.path.isdir("/tmp/.X11-unix"):
        binds += ["--ro-bind", "/tmp/.X11-unix", "/tmp/.X11-unix"]
    auth = os.environ.get("XAUTHORITY", "")
    if auth and os.path.isfile(auth):
        binds += ["--ro-bind", auth, auth]
        env["XAUTHORITY"] = auth
    return binds, env


def sweep_darwin(w: str, pgid: int) -> None:
    """Kill what the run left behind: its process group, then every process with a file (cwd, socket, profile)
    in WORK - daemons that left the group (agent-browser, Chrome, a detached piperd)."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(3):
        r = subprocess.run(["lsof", "-nP", "-u", str(os.getuid()), "-Fpn"], capture_output=True, text=True)
        pids, pid = set(), None
        for line in r.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid and (line[1:] + "/").startswith(w + "/") and pid != os.getpid():
                pids.add(pid)
        if not pids:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for p in pids:
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
            time.sleep(1)


def run_darwin(cmd: list, w: str) -> int:
    if not shutil.which("sandbox-exec"):
        die("sandbox-exec not found")
    dt = os.path.realpath(subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True,
                                         check=True).stdout.strip())
    reads, ups = [], []
    for d in [SK] + extra_reads():
        if '"' in d or "\\" in d:
            die("unsupported path for the sandbox profile: %s" % d)
        if d != SK:
            reads.append('(subpath "%s")' % d)
        u = os.path.dirname(d)
        while u.startswith(HOME + "/"):
            if '(literal "%s")' % u not in ups:
                ups.append('(literal "%s")' % u)
            u = os.path.dirname(u)
    with open(os.path.join(SK, "scripts", "piper-sandbox.sb")) as f:
        prof = f.read()
    # regex rules and extra paths must be literals in SBPL: bake them into the run's copy
    prof = prof.replace("@DARWIN_TMP_RE@", dt.replace(".", "\\.")).replace("@EXTRA_READ@", " ".join(reads))
    prof = prof.replace("@ANCESTORS@", " ".join(ups) or '(literal "/")')
    with open(os.path.join(w, "profile.sb"), "w") as f:
        f.write(prof)
    argv = ["sandbox-exec", "-f", os.path.join(w, "profile.sb"), "-D", "WORK=" + w, "-D", "HOME_DIR=" + HOME,
            "-D", "CHECKOUT=" + SK] + cmd
    env = sandbox_env(w, "seatbelt")
    # Separate, stricter native HTTP children: Seatbelt cannot nest. The broker
    # accepts no executable/profile/env and grants only a subset of this sandbox.
    import importlib.util
    spec = importlib.util.spec_from_file_location("piper_http_test_broker", os.path.join(SK, "scripts", "piper-http-test-broker.py"))
    broker_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker_module)
    broker = broker_module.start(w, env)
    if broker:
        env['PIPER_TEST_HTTP_BROKER'] = broker[0]
    p = subprocess.Popen(argv, env=env, cwd=w, start_new_session=True)
    try:
        return p.wait()
    except KeyboardInterrupt:
        os.killpg(p.pid, signal.SIGTERM)
        return 130
    finally:
        if broker:
            broker[2].set()
            broker[1].close()
        sweep_darwin(w, p.pid)


def bwrap_ok() -> bool:
    return bool(shutil.which("bwrap")) and subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-pid", "--unshare-net",
         "--die-with-parent", "true"], capture_output=True).returncode == 0


def run_bwrap(cmd: list, w: str) -> int:
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for t in ("/tmp", "/run", "/var/tmp", HOME):
        argv += ["--tmpfs", t]
    # after the tmpfs over $HOME: what lies under it comes back read-only
    argv += ["--ro-bind", SK, SK]
    for d in extra_reads():
        argv += ["--ro-bind", d, d]
    x11_binds, x11_env = x11_display()
    argv += x11_binds
    argv += ["--bind", w, w, "--unshare-pid", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
             "--new-session", "--chdir", w, "--"] + cmd
    p = subprocess.Popen(argv, env=dict(sandbox_env(w, "bwrap"), **x11_env), cwd=w)
    try:
        return p.wait()   # --die-with-parent + own pid namespace: everything inside ends with it
    except KeyboardInterrupt:
        p.terminate()
        return 130


def docker_endpoint() -> str:
    """the daemon the host's docker CLI talks to: DOCKER_HOST, else the current context's (DOCKER_CONTEXT too)"""
    if os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    r = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    # a privileged dind / the tests' containers only on this machine's daemon, never a remote one
    host = docker_endpoint()
    if not host.startswith("unix://"):
        die("docker points at %r, not a local daemon - unset DOCKER_HOST / DOCKER_CONTEXT or "
            "`docker context use default`" % host)
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def ensure_image(tag: str, dockerfile: str, context: str = "") -> None:
    if subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0:
        print("piper-sandbox: building %s" % tag, file=sys.stderr)
        subprocess.run(["docker", "build", "-q", "-t", tag] + (["-f", "-", context] if context else ["-"]),
                       input=dockerfile, text=True, check=True, stdout=subprocess.DEVNULL)


_INIT = ('mkdir -p /work/home/.config/git /work/tmp && printf "%s" > /work/home/.config/git/config && exec "$@"'
         % GITCONFIG.replace("\n", "\\n").replace("\t", "\\t"))


def container_argv(name: str, mode: str, image: str, cmd: list, extra: list) -> tuple:
    """docker run argv + env: values travel in the environment of the docker CLI (-e NAME), not on its argv."""
    env = sandbox_env("/work", mode, path=False)
    argv = ["docker", "run", "--rm", "--init", "--name", name, "--tmpfs", "/work:rw,exec,mode=1777",
            "-v", "%s:%s:ro" % (SK, SK), "-w", "/work"] + extra
    for k in sorted(env):
        argv += ["-e", k]
    return argv + [image, "sh", "-c", _INIT, "sh"] + cmd, dict(os.environ, **env)


def run_docker(cmd: list) -> int:
    if not docker_ok():
        die("Linux without a working bwrap (install bubblewrap; Ubuntu 24.04: allow unprivileged user namespaces) "
            "and without Docker - no sandbox, no tests")
    ensure_image(LINUX_IMAGE, "FROM node:22-bookworm\nRUN apt-get update -qq && apt-get install -y -qq "
                 "--no-install-recommends python3 git git-lfs >/dev/null && rm -rf /var/lib/apt/lists/* && mkdir /home/piper\n")
    name = "piper-sbx-%s" % secrets.token_hex(4)
    # the host uid as a user of the container (getpwuid: git, ssh, the tests' REAL_HOME); HOME is /work/home
    fd, passwd = tempfile.mkstemp(prefix="psbx-passwd-", dir=BASE)
    with os.fdopen(fd, "w") as f:
        f.write("root:x:0:0:root:/root:/bin/sh\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
                "piper:x:%d:%d:piper:/home/piper:/bin/sh\n" % (os.getuid(), os.getgid()))
    os.chmod(passwd, 0o644)
    argv, env = container_argv(name, "docker", LINUX_IMAGE, cmd,
                               ["--network", "none", "-u", "%d:%d" % (os.getuid(), os.getgid()),
                                "-v", "%s:/etc/passwd:ro" % passwd])
    try:
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        os.unlink(passwd)


DIND_HOST = "tcp://127.0.0.1:2375"   # inside the runner only: the host's docker CLI must never get it (a foreign 2375)


def run_dind(cmd: list) -> int:
    if not docker_ok():
        print("piper-sandbox: no Docker daemon - the Docker suite cannot run", file=sys.stderr)
        return 77
    # `docker` = tests/piper-e2e/docker-scope.py over the real CLI (no strict marker: the daemon is this dind's);
    # the tag carries the shim's hash, so a changed shim builds a new image
    with open(SCOPE_SHIM, "rb") as f:
        image = "%s-%s" % (RUNNER_IMAGE, hashlib.sha256(f.read()).hexdigest()[:10])
    ensure_image(image, "FROM python:3.12-bookworm\n"
                 "COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/libexec/piper/docker.real\n"
                 "COPY --from=docker:27-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins\n"
                 "COPY --chmod=755 docker-scope.py /usr/local/bin/docker\n", context=os.path.dirname(SCOPE_SHIM))
    dind = "piper-sbx-dind-%s" % secrets.token_hex(4)
    runner = dind.replace("dind", "run")
    try:
        subprocess.run(["docker", "run", "-d", "--privileged", "--name", dind, "--label", "piper-sandbox=dind",
                        "-e", "DOCKER_TLS_CERTDIR=", DIND_IMAGE], check=True, stdout=subprocess.DEVNULL)
        for _ in range(90):
            if subprocess.run(["docker", "exec", dind, "docker", "info"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            die("the dind daemon did not come up (docker logs %s)" % dind)
        images = [i for i in os.environ.get("PIPER_SANDBOX_IMAGES", "").split()
                  if subprocess.run(["docker", "image", "inspect", i], capture_output=True).returncode == 0]
        if images:   # from the host's local images, never pulled
            save = subprocess.Popen(["docker", "save"] + images, stdout=subprocess.PIPE)
            subprocess.run(["docker", "exec", "-i", dind, "docker", "load", "-q"], stdin=save.stdout, check=True,
                           stdout=subprocess.DEVNULL)
            save.stdout.close()
            if save.wait() != 0:
                die("docker save %s failed" % " ".join(images))
        argv, env = container_argv(runner, "dind", image, cmd,
                                   ["--network", "container:" + dind, "-e", "DOCKER_HOST=" + DIND_HOST])
        return subprocess.run(argv, env=env).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        subprocess.run(["docker", "rm", "-f", runner], capture_output=True)
        subprocess.run(["docker", "rm", "-f", "-v", dind], capture_output=True)


def run(cmd: list) -> int:
    mode = os.environ.get("PIPER_SANDBOX_MODE") or ("seatbelt" if DARWIN else "bwrap" if bwrap_ok() else "docker")
    if mode == "docker":
        return run_docker(cmd)
    if mode not in ("seatbelt", "bwrap"):
        die("PIPER_SANDBOX_MODE: seatbelt, bwrap or docker")
    w = new_work()
    rc = 1
    try:
        rc = run_darwin(cmd, w) if mode == "seatbelt" else run_bwrap(cmd, w)
        return rc
    finally:
        remove_work(w, rc != 0)


def selftest() -> int:
    """The attack probes of piper-sandbox-attack.sh inside; their targets made and checked out here."""
    probe = os.path.join(HOME, ".piper-sbx-probe-%s" % secrets.token_hex(4))
    os.mkdir(probe)
    with open(os.path.join(probe, "file"), "w") as f:
        f.write("secret\n")
    foreign = subprocess.Popen(["sleep", "300"])
    news = [os.path.join(HOME, ".piper-sbx-new"), os.path.join(HOME, ".piper-sbx-dir"),
            "/tmp/piper-sbx-new-%d" % foreign.pid, "/var/tmp/piper-sbx-new-%d" % foreign.pid,
            os.path.join(SK, ".piper-sbx-new"), os.path.join(probe, "file2")]
    pre = [p for p in news if os.path.lexists(p)]
    if pre:
        die("leftovers of an earlier selftest, look and remove them: %s" % ", ".join(pre))
    out = os.path.join(BASE, "psbx-selftest-%s.out" % secrets.token_hex(4))
    try:
        with open(out, "w") as f:
            so = os.dup(1)
            os.dup2(f.fileno(), 1)
            try:
                rc = run(["sh", os.path.join(SK, "scripts", "piper-sandbox-attack.sh"), HOME, probe, str(foreign.pid),
                          SK])
            finally:
                os.dup2(so, 1)
                os.close(so)
        with open(out) as f:
            lines = f.read().split("\n")
        bad = []
        for line in lines:
            parts = line.split()
            if len(parts) != 3:
                continue
            kind, name, res = parts
            print("  %-6s %-18s %s" % (kind, name, res))
            if (kind == "must" and res != "BLOCKED") or (kind == "works" and res != "ESCAPED"):
                bad.append("%s %s" % (name, res))
        if sum(1 for x in lines if len(x.split()) == 3) < 25:
            bad.append("the probes did not run (exit %s)" % rc)
        try:
            with open(os.path.join(probe, "file")) as f:
                if f.read() != "secret\n":
                    bad.append("the probe file in the home changed")
        except OSError as e:
            bad.append("the probe file in the home is gone: %s" % e)
        bad += ["%s was created" % p for p in news if os.path.lexists(p)]
        if foreign.poll() is not None:
            bad.append("a foreign process was killed")
        if bad:
            print("piper-sandbox selftest FAILED:\n  - " + "\n  - ".join(bad), file=sys.stderr)
            return 1
        print("piper-sandbox selftest: every escape blocked, the host untouched")
        return 0
    finally:
        foreign.kill()
        os.unlink(out)
        shutil.rmtree(probe)


def main() -> int:
    args = sys.argv[1:]
    dind = sel = False
    while args and args[0].startswith("--"):
        a = args.pop(0)
        if a == "--":
            break
        if a == "--dind":
            dind = True
        elif a == "--selftest":
            sel = True
        else:
            die("unknown option %s" % a)
    if sel:
        if in_ci():
            print("piper-sandbox: GitLab CI - the job container is the sandbox, no selftest")
            return 0
        return selftest()
    if not args:
        die("usage: piper-sandbox.py [--dind|--selftest] [--] <cmd...>")
    if dind:
        return run_dind(args)
    if in_ci():
        os.execvpe(args[0], args, dict(os.environ, TEST_SANDBOX="ci"))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
