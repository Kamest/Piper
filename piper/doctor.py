"""Doctor: is this machine ready for Piper, and what fixes it if not.

The same rows feed three places: `task piper:doctor` / `piper.sh doctor` (text like `task doctor`:
ok / warn / FAIL, every non-ok line names its fix), GET /api/doctor and the GUI's setup card.

A row: {key, level, ok, title, text, fix, settings, action}
  level     fail (sessions cannot start) | warn (something later breaks) | info (an optional module)
  fix       one command or step, "" when ok
  settings  a Settings section that fixes it ("" = none); action: a GUI button (http-install,
            db-install, deep) or ""

Only cheap checks run by default (files, PATH, one `docker info`, `hole version`); `deep=True`
adds the K18 test: a short container with a directory mask, looked at after 3 s.
A check for an optional module runs only while that module is on (features.py).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse

import common
import extensions
import features
import guard

HOLE_MIN = (2, 1, 0)
VERSION_RX = re.compile(r"(\d+)\.(\d+)\.(\d+)")
DOCKER_DESKTOP_SETTINGS = os.path.join(common.REAL_HOME, "Library", "Group Containers", "group.com.docker",
                                       "settings-store.json")
PERSONAL_SETTINGS = os.path.join(common.REAL_HOME, ".hole", "settings.json")
SERVICE_LABEL = os.environ.get("PIPER_SERVICE_LABEL", "piperd")
DEEP_IMAGE = os.environ.get("PIPER_DOCTOR_IMAGE", "busybox:latest")


def row(key: str, level: str, ok: bool, title: str, text: str = "", fix: str = "", settings: str = "",
        action: str = "") -> dict:
    return {"key": key, "level": level, "ok": ok, "title": title, "text": text, "fix": "" if ok else fix,
            "settings": settings, "action": "" if ok else action}


def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    except OSError as e:
        return 127, str(e)


_SLOW: dict = {}   # (check, binary) -> (until, row): `hole version` (it may ask GitHub) and `docker info`


def _cached(key: tuple, fn, ttl: float = 60.0) -> dict:
    """A slow check's row, reused for `ttl` seconds - a preset click re-runs the checks for its module rows."""
    hit = _SLOW.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    r = fn()
    _SLOW[key] = (time.monotonic() + ttl, r)
    return r


MAC = sys.platform == "darwin"


def _which(name: str) -> str | None:
    return name if os.path.isabs(name) and os.access(name, os.X_OK) else shutil.which(name)


# ------------------------------------------------------------------ the checks
def check_python() -> dict:
    v = sys.version_info
    ok = v >= (3, 10)
    return row("python", "fail", ok, "Python %d.%d" % (v.major, v.minor) + ("" if ok else " is too old"),
               "piperd runs on the host's python3." if ok else "Piper needs Python 3.10 or newer.",
               "install a newer python3 (%s) and run task piper:install-service again"
               % ("brew install python" if MAC else "sudo apt install python3"))


def check_hole() -> dict:
    hb = os.environ.get("PIPER_HOLE_BIN", "hole")
    path = _which(hb)
    if not path:
        return row("hole", "fail", False, "hole not found",
                   "Every session runs in a hole sandbox. It has to be on piperd's PATH (a login service keeps "
                   "the PATH it was installed with).",
                   "install hole (https://github.com/lukashornych/hole), then task piper:install-service")
    rc, out = _run([path, "version"], 10)
    m = VERSION_RX.search(out)
    if not m or rc != 0:
        return row("hole", "fail", False, "hole: version unknown", "`%s version` said: %s" % (os.path.basename(hb), out[:200]),
                   "check that `hole version` works in a shell")
    ver = tuple(int(x) for x in m.groups())
    ok = ver >= HOLE_MIN
    modern = ver >= (2, 4, 0)
    result = row("hole", "fail", ok, "hole %s" % m.group(0) + ("" if ok else " is too old"),
                 ("Sessions use native HOME and explicit settings." if modern else
                  "Compatibility mode: isolated HOME and legacy directory masks. Hole 2.4+ is recommended.")
                 if ok else "Piper needs hole 2.1.0 or newer.",
                 "update hole (https://github.com/lukashornych/hole)")
    result["settings_override"] = modern
    # hole 2.2+ hides the global files.exclude in every library and worktree too (addCheckoutExclusions);
    # 2.1 applied only a library's own file (addOwnExclusions) - preflight must count what hole hides
    result["library_global_excludes"] = ver >= (2, 2, 0)
    return result


def check_hole_settings() -> dict:
    if not os.path.isfile(PERSONAL_SETTINGS):
        return row("hole-settings", "warn", False, "No ~/.hole/settings.json",
                   "Sessions then start with hole's defaults: no excludes, libraries or network rules of your own.",
                   "create ~/.hole/settings.json (examples/hole-settings.json is the team template)")
    try:
        with open(PERSONAL_SETTINGS) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return row("hole-settings", "fail", False, "~/.hole/settings.json is not valid JSON", str(e)[:200],
                   "fix the file - hole refuses to start with it")
    legacy = guard.legacy_keys(doc if isinstance(doc, dict) else {})
    if legacy:
        return row("hole-settings", "fail", False, "~/.hole/settings.json has hole 1.x keys",
                   "hole 2 refuses to start with: %s." % ", ".join(legacy),
                   "replace them with network.allow (hole 2 settings)")
    return row("hole-settings", "fail", True, "~/.hole/settings.json", "Your hole config; Piper derives the sessions' one from it.")


def check_docker() -> dict:
    db = os.environ.get("PIPER_DOCKER_BIN", "docker")
    if not _which(db):
        return row("docker", "fail", False, "docker not found", "hole runs every sandbox as a Docker container.",
                   "install Docker Desktop (or another engine) and make sure docker is on piperd's PATH")
    rc, out = _run([db, "info", "--format", "{{.ServerVersion}}"], 10)
    if rc != 0:
        return row("docker", "fail", False, "Docker is not running", out.splitlines()[-1][:200] if out else "",
                   "open -a Docker" if MAC else   # F25: a command the Setup copies in one click (Copy: open -a Docker)
                   ("start Docker Desktop (WSL integration on for this distro) or " if common.is_wsl() else "") +
                   "start the engine (sudo service docker start); permission denied on the socket: "
                   "sudo usermod -aG docker $USER, then log in again")
    return row("docker", "fail", True, "Docker %s" % out.splitlines()[-1][:40] if out else "Docker",
               "hole runs every sandbox as a Docker container.")


def check_vmm(path: str = DOCKER_DESKTOP_SETTINGS) -> dict | None:
    """K18: Docker Desktop's VMM (libkrun) drops nested directory binds a moment after the
    start. Hole 2.4 uses volume/tmpfs masks instead; this reports the VM type. Read
    from Docker Desktop's own settings; None where there is no Docker Desktop (Linux)."""
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    krun = doc.get("UseLibkrun") is True
    # a warning, not a FAIL: the drop was seen with Docker VMM on Docker 4.90 but did not reproduce on 29.8
    # (2026-10-02); only the mount test itself (check_deep_mounts) proves it
    return row("docker-vmm", "warn", not krun,
               "Docker Desktop: Docker VMM" if krun else "Docker Desktop: virtual machine OK",
               "Docker VMM may drop nested host directory binds (K18). Hole 2.4 uses volume or tmpfs masks instead. "
               "The mount test checks masks for the installed Hole version." if krun else
               "Excluded directories stay hidden inside sandboxes.",
               "run the mount test; if it fails: Docker Desktop → Settings → General → Virtual Machine Manager: "
               "Apple Virtualization framework, then restart Docker", action="deep")


def check_deep_mounts(wait: float = 3.0, legacy: bool = False) -> dict:
    """The K18 test: P/s/secret mounted at /m, a tmpfs over /m/s; after `wait`
    seconds the secret must still be hidden. Legacy Hole uses an empty host bind instead.
    Pulls DEEP_IMAGE when missing (a few MB)."""
    db = os.environ.get("PIPER_DOCKER_BIN", "docker")
    tmp = tempfile.mkdtemp(prefix="piper-doctor-")
    try:
        p = os.path.join(tmp, "p")
        os.makedirs(os.path.join(p, "s"))
        with open(os.path.join(p, "s", "secret"), "w") as f:
            f.write("x")
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        mask = ["-v", "%s:/m/s" % empty] if legacy else ["--tmpfs", "/m/s:exec"]
        rc, out = _run([db, "run", "--rm", "--pull", "missing", "-v", "%s:/m:ro" % p] + mask + [
                        DEEP_IMAGE, "sh", "-c", "sleep %d; ls -A /m/s" % int(wait)], 120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if rc != 0:
        return row("deep-mounts", "warn", False, "Mount test did not run", out[-300:],
                   "check that Docker runs and can pull %s" % DEEP_IMAGE)
    leak = "secret" in out.split()
    kind = "host-bind" if legacy else "tmpfs"
    return row("deep-mounts", "fail", not leak,
               "Excluded directories stay hidden (mount test)" if not leak else "Excluded directories become visible (K18)",
               "A %s directory mask still hid its content after %ds." % (kind, int(wait)) if not leak else
               "The %s directory mask was dropped after the start. Upgrade to Hole 2.4+." % kind,
               "Docker Desktop → Settings → General → Virtual Machine Manager: Apple Virtualization framework")


def check_git() -> dict:
    ok = bool(shutil.which("git"))
    return row("git", "fail", ok, "git" if ok else "git not found", "Git runs on the host for every session.",
               "install git (%s)" % ("xcode-select --install" if MAC else "sudo apt install git"))


def check_login(policy: dict) -> dict:
    host = str(policy.get("claude_dir", "session")) == "host"
    tpl = os.path.isfile(os.path.join(common.TEMPLATE_DIR, ".credentials.json"))
    hostc = os.path.isfile(os.path.join(common.REAL_HOME, ".claude", ".credentials.json"))
    if host:
        return row("login", "warn", True, "Claude: your own ~/.claude", "Sessions use your login (Settings › Sessions › Sandbox).",
                   settings="sandbox")
    ok = tpl or hostc
    return row("login", "warn", ok, "Claude login ready" if ok else "No Claude login yet",
               "Sessions start from the saved login." if ok else
               "The first session asks for /login in its terminal; Piper keeps that login for the next ones.",
               "start a session and answer /login once")


def check_projects() -> dict:
    import projects as projects_mod
    pp = os.environ.get("PROJECT_PATH", "")
    try:
        n = len(projects_mod.load(pp))
    except Exception:  # noqa: BLE001 - a broken projects.json shows here, not as a 500
        n = 0
    if not pp or not os.path.isdir(pp):
        return row("projects", "warn", n > 0, "PROJECT_PATH is not set" if not pp else "PROJECT_PATH does not exist",
                   "Piper offers every git checkout under it as a project (%d found otherwise)." % n,
                   "export PROJECT_PATH=~/projects in your shell profile, then task piper:install-service",
                   settings="projects")
    try:
        bad = projects_mod.skipped(pp)
    except Exception:  # noqa: BLE001
        bad = []
    text = "Every git checkout there is offered in New session." if n else "Clone a project there or add one by hand."
    if bad:
        text += " Not offered: %s%s - %s." % (", ".join(bad[:5]), " and %d more" % (len(bad) - 5) if len(bad) > 5 else "",
                                              projects_mod.KEY_RULE)
    return row("projects", "warn", n > 0 and not bad, "%d %s in %s" % (n, "project" if n == 1 else "projects", pp) if n
               else "No project found in %s" % pp, text,
               "rename the folder (or the key in projects.json)" if bad else "add a project in Settings → Projects",
               settings="projects")


def _systemd_user() -> bool:
    """The same probe as scripts/lib-service.sh: WSL ships systemctl even where systemd does not run."""
    return bool(shutil.which("systemctl")) and _run(["systemctl", "--user", "show-environment"], 5)[0] == 0


def check_service() -> dict | None:
    if sys.platform == "darwin":
        path = os.path.join(common.REAL_HOME, "Library", "LaunchAgents", SERVICE_LABEL + ".plist")
    elif _systemd_user():
        path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(common.REAL_HOME, ".config"),
                            "systemd", "user", SERVICE_LABEL + ".service")
    elif common.is_wsl():
        return row("service", "warn", False, "Not started at login (no systemd in WSL)",
                   "task piper:install-service needs systemd; without it Piper runs only while task piper:open does.",
                   "add [boot] systemd=true to /etc/wsl.conf, run wsl --shutdown in Windows, then task piper:install-service")
    else:
        return None
    ok = os.path.isfile(path)
    return row("service", "warn", ok, "Starts at login" if ok else "Not started at login",
               "launchd/systemd keeps piperd running." if ok else
               "Piper is gone after a restart until you run task piper:open again.",
               "task piper:install-service")


def check_gitlab() -> dict:
    import gitlab
    ok = bool(gitlab.token())
    return row("gitlab", "warn", ok, "GitLab token set" if ok else "No GitLab token",
               "The inbox lists your issues, MRs and comments." if ok else
               "Without it the inbox stays empty. piperd keeps the token; a session never sees it.",
               "Settings → GitLab → Personal token (scope api)", settings="gitlab")


def check_github(policy: dict) -> dict:
    """W23: gh installed and logged in on the first GitHub host (info: GitHub is optional)."""
    import github
    host = (policy.get("github_hosts") or ["github.com"])[0]
    if not github.gh_bin():
        return row("github", "info", False, "GitHub CLI not installed",
                   "The GitHub module reads issues and pull requests through your gh login.", "brew install gh",
                   settings="github")
    ok = bool(github.token(host))
    return row("github", "info", ok, "gh logged in to %s" % host if ok else "gh not logged in to %s" % host,
               "GitHub issues and pull requests reach the inbox." if ok else
               "Without a login the GitHub part of the inbox stays empty. piperd asks gh; a session never sees the token.",
               "gh auth login --hostname %s" % host, settings="github")


def check_ijhttp(policy: dict) -> dict:
    import httpclient
    ok = bool(httpclient.find_ijhttp(policy.get("http") or {}))
    return row("ijhttp", "info", ok, "ijhttp installed" if ok else "ijhttp not installed",
               "Runs .http files on the host." if ok else "The HTTP tab needs it (about 190 MB, needs Java 17).",
               "install it from the HTTP tab", action="http-install")


def check_db() -> dict:
    # B-N7-3: the Database view's own check (the venv's python imports a driver), not only the venv's python -
    # a failed pip leaves a venv without drivers. Starts a python: once a minute
    return _cached(("db",), _check_db)


def _check_db() -> dict:
    import dbtools
    st = dbtools.driver_check()
    ok = bool(st.get("ready"))
    venv = os.path.isfile(os.path.join(dbtools.VENV, "bin", "python"))
    return row("db", "info", ok, "Database drivers installed" if ok else
               "Database drivers not usable" if venv else "Database drivers not installed",
               "MySQL, PostgreSQL, Oracle and SQL Server drivers in %s." % dbtools.VENV if ok else
               "The Database view needs them (pip, about a minute)." + (" %s." % st["missing"] if st.get("missing") and venv else
                                                                        " The last install did not finish - install them again." if venv else ""),
               "install them", settings="databases", action="db-install")


def check_idea(policy: dict) -> dict | None:
    if not policy.get("idea_in_sessions"):
        return None
    u = urllib.parse.urlsplit(str(policy.get("idea_mcp_url") or "").strip() or "http://127.0.0.1:64342/stream")
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 80), timeout=0.5):
            ok = True
    except (OSError, ValueError):
        ok = False
    return row("idea", "warn", ok, "IntelliJ MCP answers" if ok else "IntelliJ MCP does not answer",
               "Sessions get IDEA's tools." if ok else "Sessions start without IDEA's tools.",
               "start IntelliJ IDEA with the MCP server enabled"
               + ("; an IDEA on Windows answers on WSL's 127.0.0.1 only with networkingMode=mirrored "
                  "in %UserProfile%\\.wslconfig (then wsl --shutdown)" if common.is_wsl() else ""), settings="sessions")


def check_wsl() -> list[dict]:
    """WSL only: what tells the setups apart (Docker Desktop vs an engine in the distro, NAT vs
    mirrored networking) and what quietly breaks there (projects on the Windows drive, no linger)."""
    out = []
    rc, eng = _run([os.environ.get("PIPER_DOCKER_BIN", "docker"), "info", "--format", "{{.OperatingSystem}}"], 10)
    eng = (eng.splitlines() or [""])[-1][:60] if rc == 0 else ""
    rc, net = _run(["wslinfo", "--networking-mode"], 5)
    net = net.strip()[:20] if rc == 0 else "unknown"
    out.append(row("wsl", "info", True, "WSL: %s, networking %s" % (eng or "Docker engine unknown", net),
                   "Docker Desktop reaches host.internal on the Windows host; an engine in the distro on its bridge."))
    pp = os.path.realpath(os.environ.get("PROJECT_PATH", "")) if os.environ.get("PROJECT_PATH") else ""
    if re.match(r"^/mnt/[a-z]/", pp):   # no PROJECT_PATH: check_projects says so already
        out.append(row("wsl-projects", "warn", False, "Projects on the Windows drive (%s)" % pp,
                       "Git, search and the sandbox checks run 10-50x slower over /mnt, file modes do not stick, "
                       "and the Files trash cannot move across volumes.",
                       "clone the projects into the Linux home (e.g. ~/www) and point PROJECT_PATH there"))
    if _systemd_user():
        import getpass
        try:
            user = getpass.getuser()
        except (OSError, KeyError):   # no USER/LOGNAME and no passwd entry
            user = str(os.getuid())
        rc, lg = _run(["loginctl", "show-user", user, "-p", "Linger"], 5)
        ok = "Linger=yes" in lg
        out.append(row("wsl-linger", "warn", ok, "systemd keeps Piper after the last shell" if ok else "No linger",
                       "Sessions survive closing the terminals." if ok else
                       "systemd stops the user's services, Piper and its sessions included, when the last WSL shell closes.",
                       "loginctl enable-linger $USER"))
    return out


def check_codex_headers() -> dict | None:
    """R3+: a remote MCP header of the Codex template that sessions never get (it looks like a credential,
    codexcfg._plain_headers) - only when there is one; names only, never a value."""
    import codexcfg
    try:
        tpl = common.safe_read(codexcfg.TEMPLATE_DIR, "config.toml", 1 << 20).decode("utf-8", "replace")
    except OSError:
        return None
    dropped = codexcfg.dropped_headers(tpl)
    if not dropped:
        return None
    return row("codex-headers", "warn", False, "Codex sessions run %d remote MCP header(s) short" % len(dropped),
               "Not passed into the sandbox because they look like credentials: %s. Such a server may refuse the "
               "session's calls." % ", ".join(dropped[:10]),
               "a sandbox never holds a static credential: use that server outside Piper sessions, or drop the "
               "header from ~/.codex/config.toml if the server works without it")


def check_codex_servers() -> dict | None:
    """2026-10-10 (the user): a remote MCP server of the Codex template that sessions never get (its URL points
    at this machine / a private network, codexcfg._blocked_address) - only when there is one; names only."""
    import codexcfg
    try:
        tpl = common.safe_read(codexcfg.TEMPLATE_DIR, "config.toml", 1 << 20).decode("utf-8", "replace")
    except OSError:
        return None
    skipped = codexcfg.skipped_servers(tpl)
    if not skipped:
        return None
    return row("codex-servers", "warn", False, "Codex sessions run without %d remote MCP server(s)" % len(skipped),
               "Not passed into the sandbox: %s. Its address is on this machine or a private network (or holds a "
               "user name / an odd form), which no sandbox may reach." % ", ".join(skipped[:10]),
               "use that server outside Piper sessions, or give it a public address in ~/.codex/config.toml")


def check_mattermost() -> dict:
    import mattermost
    ok = bool(mattermost.webhook_url())
    return row("mattermost", "info", ok, "Mattermost webhook set" if ok else "No Mattermost webhook",
               "Notifications go to your channel." if ok else "The module is on but has nowhere to send.",
               "Settings → Mattermost → Webhook URL", settings="mattermost")


def checks(deep: bool = False) -> list[dict]:
    """Every row in display order: the hard prerequisites, then the optional modules that are on."""
    policy = guard.load_policy(common.POLICY_FILE)
    mods = features.load()["modules"]
    out = [_cached(("hole", os.environ.get("PIPER_HOLE_BIN", "hole")), check_hole),
           _cached(("docker", os.environ.get("PIPER_DOCKER_BIN", "docker")), check_docker)]
    vmm = check_vmm()
    if vmm is not None:
        out.append(vmm)
    if deep:
        out.append(check_deep_mounts(legacy=not out[0].get("settings_override", False)))
    out += [check_hole_settings(), check_git(), check_python(), check_login(policy), check_projects()]
    svc = check_service()
    if svc is not None:
        out.append(svc)
    if common.is_wsl():
        out += _cached(("wsl",), check_wsl)   # docker info, wslinfo, loginctl: once a minute
    if mods.get("gitlab"):
        out.append(check_gitlab())
    if mods.get("github"):
        out += _cached(("github",), lambda: [check_github(policy)])   # gh is a subprocess: once a minute
    if mods.get("http"):
        out.append(check_ijhttp(policy))
    if mods.get("db"):
        out.append(check_db())
    if mods.get("mattermost"):
        out.append(check_mattermost())
    idea = check_idea(policy)
    if idea is not None:
        out.append(idea)
    codex = check_codex_headers()
    if codex is not None:
        out.append(codex)
    servers = check_codex_servers()
    if servers is not None:
        out.append(servers)
    out += [r for r in extensions.collect("doctor_rows", mods) if r]   # an extension's own rows
    out += [row("extension", "warn", False, "Extension not loaded", e,
                "fix it or remove it from $PIPER_EXTENSIONS") for e in extensions.ERRORS]
    return out


def image_info() -> dict:
    """9.54: hole's agent images, newest first, and how many containers run each - the Setup row.
    {"ok": False, "error"} when docker does not answer."""
    db = os.environ.get("PIPER_DOCKER_BIN", "docker")
    if not _which(db):
        return {"ok": False, "error": "docker not found"}
    rc, out = _run([db, "image", "ls", "--filter", "reference=hole-sandbox/agent-*",
                    "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}\t{{.CreatedAt}}"], 10)
    if rc != 0:
        return {"ok": False, "error": (out.splitlines() or ["docker image ls failed"])[-1][:200]}
    rc2, ps = _run([db, "ps", "--filter", "label=com.hole.image=agent", "--format", "{{.Image}}"], 10)
    used: dict = {}
    for ref in ps.splitlines() if rc2 == 0 else []:
        used[ref.strip()] = used.get(ref.strip(), 0) + 1
    images = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0].endswith(":<none>"):
            continue
        images.append({"ref": parts[0], "size": parts[1], "age": parts[2], "created": parts[3],
                       "in_use": used.get(parts[0], 0)})
    images.sort(key=lambda x: x["created"], reverse=True)   # "2026-10-16 11:04:33 +0200 CEST" sorts as text
    return {"ok": True, "images": images, "in_use": sum(used.values())}


def report(deep: bool = False) -> dict:
    rows = checks(deep)
    return {"rows": rows, "at": time.time(), "deep": deep,
            "fail": sum(1 for r in rows if not r["ok"] and r["level"] == "fail"),
            "warn": sum(1 for r in rows if not r["ok"] and r["level"] == "warn")}


def text(rep: dict) -> str:
    """The `task doctor` format: ok / warn / FAIL / info, the fix after a dash."""
    lines = []
    for r in rep["rows"]:
        tag = "ok  " if r["ok"] else {"fail": "FAIL", "warn": "warn", "info": "info"}[r["level"]]
        line = "%s %s" % (tag, r["title"])
        if not r["ok"]:
            line += (" - " + r["text"] if r["text"] else "") + (" Fix: " + r["fix"] if r["fix"] else "")
        lines.append(line)
    if not rep["deep"]:
        lines.append("     (the K18 mount test runs with --deep: a short container, a few seconds)")
    return "\n".join(lines)


if __name__ == "__main__":
    deep = "--deep" in sys.argv
    rep = report(deep)
    print(json.dumps(rep, indent=1) if "--json" in sys.argv else text(rep))
    sys.exit(1 if rep["fail"] else 0)
