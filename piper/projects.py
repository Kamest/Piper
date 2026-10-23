"""The project whitelist: key -> checkout, GitLab project(s), profile.

Sessions are spawned only from this list - the Global agent names a key, never a path. It is
$PIPER_HOME/projects.json (explicit entries, e.g. a GitLab path or a profile) merged with
every git checkout DISCOVERED under $PROJECT_PATH (depth <= 3, key `<client>/<repo>`, e.g.
`acme/shop`) and every git checkout matched by a `"globs"` pattern of projects.json
(e.g. `~/development/*`, key = the last two path segments, `development/my-app`). Entries can
also be imported from an extension's registry (project_roots). The GitLab path is derived on the host
from `origin` when not given. Nothing here is read from inside a sandbox.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import subprocess
import time

import common
import extensions
from common import PROJECTS_FILE, read_json, write_json

KEY_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,2}$")
SKIP = {"node_modules", "target", ".git", ".idea"}   # an extension adds its own (PROJECT_SKIP)
_DISCOVERED: dict = {"at": 0.0, "root": None, "items": [], "skipped": []}
KEY_RULE = "a project name holds only letters, digits, . _ - (up to 64 characters per level)"


def discover(project_path: str, max_depth: int = 3, ttl: float = 60.0) -> list[tuple[str, str]]:
    """(key, path) of every git checkout under project_path; a checkout is not descended.
    Symlinks are followed (a checkout or a folder of checkouts elsewhere); a target already
    seen is skipped, so a loop or a second link to the same place adds nothing."""
    now = time.time()
    if _DISCOVERED["root"] == project_path and now - _DISCOVERED["at"] < ttl:
        return _DISCOVERED["items"]
    found: list[tuple[str, str]] = []
    skipped: list[str] = []
    seen = {os.path.realpath(project_path)} if project_path else set()

    skip = SKIP | set(extensions.items("PROJECT_SKIP"))

    def walk(d: str, rel: str, depth: int) -> None:
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in skip or not e.is_dir():
                continue
            real = os.path.realpath(e.path)
            if real in seen:
                continue
            seen.add(real)
            child_rel = e.name if not rel else rel + "/" + e.name
            if os.path.exists(os.path.join(e.path, ".git")):
                if KEY_RX.match(child_rel):
                    found.append((child_rel, e.path))
                else:
                    skipped.append(child_rel)
                continue   # a checkout: its own subdirectories are not projects
            if depth < max_depth:
                walk(e.path, child_rel, depth + 1)

    if project_path and os.path.isdir(project_path):
        walk(project_path, "", 1)
    _DISCOVERED.update(at=now, root=project_path, items=found, skipped=skipped)
    return found


def skipped(project_path: str | None = None) -> list[str]:
    """Checkouts and projects.json entries never offered because their name is no valid key (KEY_RULE):
    the doctor names them - otherwise a folder like `my project` just never shows up in New session."""
    load(project_path)
    bad = [k for k in (read_json(PROJECTS_FILE, {"projects": {}}).get("projects") or {}) if not KEY_RX.match(k)]
    return bad + [k for k in _DISCOVERED["skipped"] if k not in bad]


def find(key: str, project_path: str | None = None) -> dict | None:
    """One project by key; a miss re-runs the discovery (a checkout cloned a moment ago).
    `<key>@<name>`: the linked worktree `<name>` of project `<key>` (worktree())."""
    base_key, at, name = str(key or "").partition("@")
    if at:
        base = find(base_key, project_path)
        return worktree(base, name) if base else None
    p = load(project_path).get(key)
    if p is None:
        _DISCOVERED["at"] = 0.0
        p = load(project_path).get(key)
    return p


WORKTREES = os.path.join(".claude", "worktrees")
WORKTREE_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def worktree_path(base: dict, name: str) -> str:
    if not WORKTREE_RX.match(name or "") or name in (".", ".."):
        raise ValueError("a worktree name holds only letters, digits, . _ - (up to 64 characters)")
    return os.path.join(base["path"], WORKTREES, name)


def worktree_gitdir(base_path: str, wt: str) -> str | None:
    """The git dir of linked worktree `wt` when it is one of base's repository and points back
    (its `.git` file names `<base>/.git/worktrees/<id>`, whose `gitdir` names `wt/.git`); no git runs."""
    common = os.path.realpath(os.path.join(base_path, ".git"))
    dotgit = os.path.join(wt, ".git")
    if not os.path.isdir(common) or os.path.islink(dotgit) or not os.path.isfile(dotgit):
        return None
    try:
        with open(dotgit, encoding="utf-8") as f:
            line = f.read(4096).strip()
        if not line.startswith("gitdir:"):
            return None
        gd = os.path.realpath(os.path.join(wt, line[7:].strip()))
        if os.path.dirname(gd) != os.path.join(common, "worktrees"):
            return None
        with open(os.path.join(gd, "gitdir"), encoding="utf-8") as f:
            back = f.read(4096).strip()
    except OSError:
        return None
    return gd if os.path.realpath(back) == os.path.realpath(dotgit) else None


def worktree(base: dict, name: str) -> dict | None:
    """The derived project `<key>@<name>`: base's linked worktree in
    `<base>/.claude/worktrees/<name>`, with base's GitLab projects and profile. Never in load():
    the lists show the base project, a worktree is opened from it."""
    try:
        path = os.path.realpath(worktree_path(base, name))
    except ValueError:
        return None
    if os.path.dirname(path) != os.path.realpath(os.path.join(base["path"], WORKTREES)) \
            or not worktree_gitdir(base["path"], path):
        return None
    return {"key": "%s@%s" % (base["key"], name), "path": path, "gitlab": list(base["gitlab"]),
            "github": list(base.get("github") or []),
            "profile": base["profile"], "title": "%s @ %s" % (base["title"], name), "exists": True,
            "source": "worktree", "base": base["key"], "worktree": name}


def load(project_path: str | None = None) -> dict:
    """projects.json merged with its `globs` checkouts and those discovered under PROJECT_PATH."""
    data = read_json(PROJECTS_FILE, {"projects": {}})
    out = {}
    for key, p in (data.get("projects") or {}).items():
        if not KEY_RX.match(key) or not isinstance(p, dict) or not p.get("path"):
            continue
        path = os.path.realpath(os.path.expanduser(str(p["path"])))
        out[key] = {"key": key, "path": path, "gitlab": list(p.get("gitlab") or []),
                    "github": [str(x) for x in p.get("github") or [] if isinstance(x, str)],   # W23: owner/repo
                    "profile": p.get("profile") or None, "title": p.get("title") or key,
                    "exists": os.path.isdir(path), "source": "projects.json"}
        if "sandbox_env" in p:
            out[key]["sandbox_env"] = p["sandbox_env"]
    known = {p["path"] for p in out.values()}
    for pattern in data.get("globs") or []:
        for path in sorted(glob.glob(os.path.expanduser(str(pattern)))):
            real = os.path.realpath(path)
            key = "/".join(real.rstrip("/").split("/")[-2:])
            if real in known or key in out or not KEY_RX.match(key) \
                    or not os.path.exists(os.path.join(real, ".git")):
                continue
            out[key] = {"key": key, "path": real, "gitlab": [], "profile": None,
                        "title": key, "exists": True, "source": "glob"}
            known.add(real)
    pp = project_path if project_path is not None else os.environ.get("PROJECT_PATH", "")
    for key, path in discover(pp):
        real = os.path.realpath(path)
        if real in known or key in out:
            continue
        out[key] = {"key": key, "path": real, "gitlab": [], "profile": None,
                    "title": key, "exists": True, "source": "discovered"}
    return out


def sandbox_env_config(project: dict) -> tuple[str, list[str]]:
    """Host-owned selection of literal dotenv values available to this project's sandboxes."""
    config = project.get("sandbox_env")
    if config is None:
        return "", []
    key = project["key"]
    if not isinstance(config, dict):
        raise ValueError("%s: sandbox_env must contain file and keys" % key)
    source, names = config.get("file", ".env.local"), config.get("keys")
    if not isinstance(source, str) or not source or source.startswith("/") or "\\" in source or \
            any(p in ("", ".", "..") for p in source.split("/")):
        raise ValueError("%s: sandbox_env.file must be a relative path inside the project" % key)
    reserved = {"HOME", "PATH", "PROJECT_PATH", "GITLAB_TOKEN", "GITLAB_PAT", "BASH_ENV", "ENV",
                "NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME", "LD_PRELOAD", "LD_LIBRARY_PATH"}
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and
            re.fullmatch(r"[A-Z_][A-Z0-9_]*", n) and n not in reserved and
            not n.startswith(("PIPER_", "ORCH_", "HOLE_", "GIT_", "DOCKER_", "DYLD_") + tuple(extensions.collect("reserved_env")))
            for n in names) or len(names) != len(set(names)):
        raise ValueError("%s: sandbox_env.keys must be unique environment names, excluding process-control variables" % key)
    return source, names


def session_defaults(project_path: str | None = None) -> dict:
    """Host-owned start preferences for registered and discovered base projects.

    Bind a preference to its checkout as well as its key so reusing a key for a different
    directory cannot give that project the previous project's privileges.
    """
    data = read_json(PROJECTS_FILE, {})
    saved = data.get("session_defaults")
    out = {}
    available = load(project_path)
    for key, pref in (saved.items() if isinstance(saved, dict) else ()):
        if not isinstance(pref, dict):
            continue
        p = available.get(key)
        if not p or pref.get("path") != p["path"] or pref.get("agent") not in ("claude", "codex") \
                or not isinstance(pref.get("opts"), list):
            continue
        out[key] = {"agent": pref["agent"],
                    "opts": [k for k in ("docker", "unrestricted", "idea") if k in pref["opts"]]}
    return out


def remember_session_defaults(key: str, agent: str, opts: dict, keep_agent: bool = False,
                              session_path: str | None = None) -> dict:
    """Remember a GUI-created session's explicit choices; linked worktrees share their base.

    Empty options are a saved choice too. All projects.json writers take PROJECTS_LOCK.
    keep_agent preserves an unavailable agent's preference while its module is switched off.
    """
    if agent not in ("claude", "codex") or not isinstance(opts, dict) \
            or any(k not in ("docker", "unrestricted", "idea") or not isinstance(v, bool) for k, v in opts.items()):
        raise ValueError("invalid project session defaults")
    with common.PROJECTS_LOCK:
        p = find(key)
        if p is None or key == "advisor":
            raise ValueError("project session defaults need a known project")
        if session_path is not None and os.path.realpath(session_path) != p["path"]:
            raise ValueError("the project's checkout changed during the start")
        base = find(p["base"]) if p.get("base") else p
        if base is None:
            raise ValueError("unknown base project")
        data = read_json(PROJECTS_FILE, {"projects": {}})
        saved = data.get("session_defaults")
        if not isinstance(saved, dict):
            saved = data["session_defaults"] = {}
        old = saved.get(base["key"])
        if keep_agent and isinstance(old, dict) and old.get("path") == base["path"] \
                and old.get("agent") in ("claude", "codex"):
            agent = old["agent"]
        pref = {"path": base["path"], "agent": agent,
                "opts": [k for k in ("docker", "unrestricted", "idea") if opts.get(k)]}
        saved[base["key"]] = pref
        write_json(PROJECTS_FILE, data)
        common.audit({"event": "project-session-defaults", "project": base["key"],
                      "agent": agent, "opts": pref["opts"]})
        return {"agent": agent, "opts": pref["opts"]}


def sandbox_env_profile(key: str) -> str:
    """A stable profile keeps concurrent starts independent of the last project to write settings."""
    return "piper-api-" + hashlib.sha256(key.encode()).hexdigest()[:16]


def sandbox_env_marker(key: str, name: str) -> str:
    return "PIPER_API_ENV_%s_%s" % (hashlib.sha256(key.encode()).hexdigest()[:16].upper(), name)


def sandbox_env_values(project: dict) -> dict[str, str]:
    """Read only selected keys without executing dotenv content, expanding variables or following links.
    The source is the registered checkout, so linked and agent-created worktrees inherit the same access.
    """
    source, names = sandbox_env_config(project)
    if not names:
        return {}
    try:
        text = common.safe_read(project["path"], source, 64 << 10).decode("utf-8-sig")
    except (OSError, ValueError, UnicodeError):
        raise ValueError("%s: cannot read sandbox_env.file %s as a regular project file" % (project["key"], source)) from None
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)", line)
        if not match or match[1] not in names:
            continue
        name, value = match[1], match[2].strip()
        if value.startswith(("'", '"')):
            quoted = re.fullmatch(r"(['\"])(.*?)\1\s*(?:#.*)?", value)
            if not quoted:
                raise ValueError("%s: invalid quoted sandbox_env value for %s" % (project["key"], name))
            value = quoted[2]
            if quoted[1] == '"':
                value = value.replace("\\n", "\n").replace("\\r", "\r")
        else:
            value = value.split("#", 1)[0].rstrip()
        values[name] = value
    missing = [n for n in names if not values.get(n)]
    if missing:
        raise ValueError("%s: sandbox_env.file %s is missing %s" % (project["key"], source, ", ".join(missing)))
    return {sandbox_env_marker(project["key"], n): values[n] for n in names}


def origin_url(checkout: str) -> str | None:
    """The checkout's origin URL ("" without one), None when git cannot tell."""
    try:
        return subprocess.run(["git", "-C", checkout, "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def gitlab_path(checkout: str, host: str) -> str | None:
    """`group/project` of the checkout's origin when it points at `host`."""
    return gitlab_match(origin_url(checkout) or "", host)


def gitlab_match(url: str, host: str) -> str | None:
    """`group/project` of an origin URL when it points at `host`."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)" + re.escape(host) +
                 r"(?::\d+)?[:/](.+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def github_match(url: str, hosts) -> str | None:
    """`owner/repo` of an origin URL when it points at one of the GitHub `hosts` (W23)."""
    for host in hosts or ():
        m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|[^@/]+@)" + re.escape(host) +
                     r"(?::\d+)?[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url, re.I)
        if m:
            return m.group(1)
    return None


def import_roots(roots: list[str], gitlab_host: str, origin_gl=None) -> list[str]:
    """Add checkouts (an extension's registry, extensions.py project_roots) missing from projects.json;
    returns the keys added. `origin_gl`
    (Manager.origin_gl): the GitLab project from the trusted record, not a .git/config a
    sandbox may have written (git is open there since 2026-10-01)."""
    with common.PROJECTS_LOCK:   # the settings page and the obs mapping write it too
        data = read_json(PROJECTS_FILE, {"projects": {}})
        projects = data.setdefault("projects", {})
        known = {os.path.realpath(os.path.expanduser(p.get("path", ""))) for p in projects.values()}
        added = []
        for root in roots:
            if os.path.realpath(root) in known:
                continue
            key = re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(root.rstrip("/")))
            base, n = key, 2
            while key in projects:
                key, n = "%s-%d" % (base, n), n + 1
            gl = origin_gl(root) if origin_gl else gitlab_path(root, gitlab_host)
            projects[key] = {"path": root, "gitlab": [gl] if gl else []}
            added.append(key)
        if added:
            write_json(PROJECTS_FILE, data)
        return added


def add_checkout(path: str, gl: str, field: str = "gitlab") -> str:
    """Add a checkout to projects.json mapped to GitLab `gl` (the GUI inbox, 9.8 #4); key =
    its basename (`-2`, `-3` ... when taken). An entry with the same path gains `gl`.
    field="github": `gl` is a GitHub repository (owner/repo, host/owner/repo) - W23."""
    with common.PROJECTS_LOCK:   # the settings page and the obs mapping write it too
        data = read_json(PROJECTS_FILE, {"projects": {}})
        projects = data.setdefault("projects", {})
        real = os.path.realpath(path)
        for key, p in projects.items():
            if isinstance(p, dict) and os.path.realpath(os.path.expanduser(str(p.get("path") or ""))) == real:
                gls = list(p.get(field) or [])
                if gl not in gls:
                    p[field] = gls + [gl]
                    write_json(PROJECTS_FILE, data)
                return key
        key = re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(real.rstrip("/"))).lstrip(".-_") or "project"
        base, n = key[:60], 2
        key = base
        while key in projects:
            key, n = "%s-%d" % (base, n), n + 1
        projects[key] = {"path": real, field: [gl]}
        write_json(PROJECTS_FILE, data)
        return key
