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
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import common
import extensions
from common import PROJECTS_FILE, read_json, write_json

KEY_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}(/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}){0,2}$")
SKIP = {"node_modules", "target", ".git", ".idea"}   # an extension adds its own (PROJECT_SKIP)
_DISCOVERED: dict = {"at": 0.0, "root": None, "items": [], "skipped": []}
# Keep the legacy `at = 0` invalidation used by discovery consumers. Each root has
# its own bounded entry; cold scans are shared by concurrent HTTP request threads.
_DISCOVERY_CACHE: OrderedDict = OrderedDict()
_DISCOVERY_LOCK = threading.RLock()
KEY_RULE = "a project name holds only letters, digits, . _ - (up to 64 characters per level)"

# GUI project creation is asynchronous: network Git operations never hold an HTTP request.
_CREATE_LOCK = threading.Lock()
_CREATE_JOBS: dict[str, dict] = {}


def _url_token(value: str, punctuation: str) -> bool:
    """Validate bounded Git URL components in linear time, without backtracking."""
    return bool(value) and value.isascii() and all(c.isalnum() or c in punctuation for c in value)


def _clone_source(value) -> str:
    """Accept Git URLs without embedded credentials, shell transports or control characters."""
    if not isinstance(value, str) or not value or len(value) > 4096 or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Enter a repository URL without embedded credentials.")
    if os.path.isabs(value):
        if not os.path.isdir(value):
            raise ValueError("The local source repository does not exist.")
        return os.path.realpath(value)
    if "://" not in value:
        authority, colon, repository = value.partition(":")
        user, at, host = authority.rpartition("@")
        if (not colon or not _url_token(repository, "_./~-") or not _url_token(host, ".-")
                or not host[0].isalnum() or (at and not _url_token(user, "_.-"))):
            raise ValueError("Use an HTTPS or SSH repository URL.")
        return value
    try:
        url = urlsplit(value)
        if url.scheme not in ("https", "http", "ssh", "git", "file") or url.query or url.fragment or url.password is not None:
            raise ValueError()
        if url.scheme == "file":
            if url.netloc or not os.path.isabs(url.path) or not os.path.isdir(url.path):
                raise ValueError()
        elif not url.hostname or not _url_token(url.hostname, ".:-") or not url.hostname[0].isalnum() or not url.path:
            raise ValueError()
        if url.scheme in ("https", "http", "git") and url.username is not None:
            raise ValueError()
        if url.scheme == "ssh" and url.username is not None and not _url_token(url.username, "_.-"):
            raise ValueError()
        if url.port is not None and not 0 < url.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("Use a repository URL without passwords, tokens, query parameters or fragments.") from None
    return value


def _register_created(path: str) -> str:
    """Register a completed checkout without overwriting another project's key or preferences."""
    with common.PROJECTS_LOCK:
        available = load()
        data = read_json(PROJECTS_FILE, {"projects": {}})
        entries = data.setdefault("projects", {})
        key = next((key for key, row in available.items() if row["path"] == path), "")
        if key in entries:
            return key
        if not key:
            base = re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(path)).lstrip(".-_")[:60] or "project"
            key, n = base, 2
            while key in available:
                key, n = "%s-%d" % (base, n), n + 1
        entries[key] = {"path": path, "gitlab": []}
        write_json(PROJECTS_FILE, data)
        return key


def creation_directories(path: str = "") -> dict:
    """List folder choices for the authenticated human, never file contents or agent access."""
    path = path or common.REAL_HOME
    if not isinstance(path, str) or len(path) > 4096 or not os.path.isabs(path) or any(ord(c) < 32 for c in path):
        raise ValueError("Choose an absolute folder path.")
    path = os.path.realpath(path)
    if ".git" in path.split(os.sep):
        raise ValueError("Choose a folder outside .git.")
    folders, empty, more = [], True, False
    try:
        with os.scandir(path) as entries:
            for n, entry in enumerate(entries):
                empty = False
                if n >= 10000 or len(folders) >= 500:
                    more = True
                    break
                if entry.name == ".git" or any(ord(c) < 32 for c in entry.name):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders.append({"name": entry.name, "path": os.path.join(path, entry.name)})
    except (FileNotFoundError, NotADirectoryError):
        raise FileNotFoundError("This folder does not exist.") from None
    except PermissionError:
        raise PermissionError("Piper isn't allowed to open this folder.") from None
    except OSError:
        raise ValueError("The folder could not be listed. Check its path and permissions.") from None
    return {"path": path, "parent": os.path.dirname(path), "empty": empty,
            "directories": sorted(folders, key=lambda row: row["name"].casefold()), "more": more}


def create_start(body: dict) -> dict:
    """Reserve a new destination and start clone/init; only the authenticated GUI calls this."""
    mode, path = body.get("mode"), body.get("path")
    if mode not in ("clone", "empty"):
        raise ValueError("Choose Clone repository or Empty project.")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("Enter an absolute destination folder.")
    path = os.path.normpath(os.path.expanduser(path.strip()))
    if not os.path.isabs(path) or ".git" in path.replace("\\", "/").split("/"):
        raise ValueError("Choose an absolute destination outside .git.")
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
        raise ValueError("The destination already exists and is not an empty folder.")
    path = os.path.realpath(path)
    if ".git" in path.replace("\\", "/").split("/"):
        raise ValueError("Choose a destination outside .git.")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ValueError("The destination's parent folder must already exist.")
    source = _clone_source(body.get("url")) if mode == "clone" else ""
    with _CREATE_LOCK:
        if sum(job["public"]["state"] == "running" for job in _CREATE_JOBS.values()) >= 2:
            raise ValueError("Two projects are already being created. Wait for one to finish.")
        if any(job["public"]["state"] == "running" and job["public"]["path"] == path for job in _CREATE_JOBS.values()):
            raise ValueError("A project is already being created at this destination.")
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isdir(path):
                raise ValueError("The destination already exists and is not an empty folder.")
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    raise ValueError("The destination is not empty. Choose a new or empty folder.")
        else:
            # mkdir, not exist_ok=True: concurrent requests cannot silently reuse a new target.
            os.mkdir(path)
        while len(_CREATE_JOBS) >= 32:
            oldest = next((key for key, job in _CREATE_JOBS.items() if job["public"]["state"] != "running"), None)
            if oldest is None:
                break
            del _CREATE_JOBS[oldest]
        ident = secrets.token_hex(8)
        public = {"id": ident, "mode": mode, "path": path, "state": "running", "can_cancel": True}
        # Pin the reserved directory for Git's cwd, so replacing its pathname with a symlink
        # cannot redirect a background clone into a different directory.
        dirfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if os.name == "posix" else None
        identity = os.fstat(dirfd) if dirfd is not None else os.stat(path)
        job = {"public": public, "cancel": threading.Event(), "dirfd": dirfd,
               "identity": (identity.st_dev, identity.st_ino)}
        _CREATE_JOBS[ident] = job
        threading.Thread(target=_create_run, args=(job, source), name="piper-project-create", daemon=True).start()
        return dict(public)


def create_job(ident: str) -> dict:
    """Read a bounded public job record; repository credentials and Git output are never retained."""
    with _CREATE_LOCK:
        if ident not in _CREATE_JOBS:
            raise KeyError("unknown project creation job")
        return dict(_CREATE_JOBS[ident]["public"])


def create_cancel(ident: str) -> dict:
    """Request cancellation; the worker owns and stops its Git process before reporting cancelled."""
    with _CREATE_LOCK:
        if ident not in _CREATE_JOBS:
            raise KeyError("unknown project creation job")
        job = _CREATE_JOBS[ident]
        if job["public"]["state"] == "running" and job["public"]["can_cancel"]:
            job["cancel"].set()
            job["public"]["message"] = "Cancelling…"
        return dict(job["public"])


def _stop_create(proc) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait()
    except ProcessLookupError:
        pass


def _create_run(job: dict, source: str) -> None:
    """Run Git off the request thread, then register only successful, uncancelled checkouts."""
    public, cancel = job["public"], job["cancel"]
    proc = None
    state, message, extra = "error", "Git could not create the project. Check Git and destination permissions.", {}
    try:
        args = (["clone", "--no-recurse-submodules", "--no-hardlinks", "--", source, "."]
                if public["mode"] == "clone" else ["init", "--", "."])
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat")
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(name, None)
        command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
        if job["dirfd"] is not None:
            # /dev/fd/N cannot be chdir'ed on macOS. A fixed stdlib launcher pins cwd
            # with fchdir before exec; no shell or repository-supplied code runs.
            command = [sys.executable, "-I", "-c", "import os,sys; os.fchdir(int(sys.argv[1])); "
                       "os.close(int(sys.argv[1])); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)",
                       str(job["dirfd"]), *command]
        proc = subprocess.Popen(command,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=os.name == "posix",
                                **({"pass_fds": (job["dirfd"],)} if job["dirfd"] is not None else {"cwd": public["path"]}))
        deadline = time.monotonic() + 900
        while proc.poll() is None:
            if cancel.wait(.1) or time.monotonic() >= deadline:
                _stop_create(proc)
                break
        with _CREATE_LOCK:
            public["can_cancel"] = False
        if cancel.is_set():
            state, message = "cancelled", "Creation cancelled. The destination folder was kept; inspect it before retrying."
        elif time.monotonic() >= deadline:
            message = "Git timed out. The destination folder was kept; inspect it before retrying."
        elif proc.returncode:
            message = "Git failed (exit %d). Check repository access and the destination folder before retrying." % proc.returncode
        else:
            current = os.lstat(public["path"])
            if os.path.islink(public["path"]) or (current.st_dev, current.st_ino) != job["identity"]:
                message = "The destination changed while Git was running. No project was registered."
                raise ValueError("destination changed")
            extra["key"] = _register_created(public["path"])
            state, message = "done", "Project ready."
    except Exception:
        # Do not return subprocess exceptions or stderr: credential helpers can print secrets.
        pass
    finally:
        if proc is not None:
            _stop_create(proc)
        if job["dirfd"] is not None:
            os.close(job["dirfd"])
        with _CREATE_LOCK:
            public.update(state=state, message=message, can_cancel=False, **extra)


def linked_worktree(checkout: str) -> bool:
    """A `git worktree add` checkout: never a project of its own (New session offers the ones in
    `<base>/.claude/worktrees/` as Existing worktree of the base project). Its `.git` file names a per-worktree
    git dir with a `commondir` (a submodule's git dir has none). No git runs."""
    dotgit = os.path.join(checkout, ".git")
    if not os.path.isfile(dotgit):
        return False
    try:
        with open(dotgit, encoding="utf-8") as f:
            line = f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return False
    return line.startswith("gitdir:") and os.path.isfile(os.path.join(checkout, line[7:].strip(), "commondir"))


def discover(project_path: str, max_depth: int = 3, ttl: float = 60.0) -> list[tuple[str, str]]:
    """(key, path) of every git checkout under project_path; a checkout is not descended.
    Symlinks are followed (a checkout or a folder of checkouts elsewhere); a target already
    seen is skipped, so a loop or a second link to the same place adds nothing."""
    with _DISCOVERY_LOCK:
        return _discover(project_path, max_depth, ttl)


def _discover(project_path: str, max_depth: int, ttl: float) -> list[tuple[str, str]]:
    skip = SKIP | set(extensions.items("PROJECT_SKIP"))
    key = (project_path, max_depth, frozenset(skip))
    if not _DISCOVERED["at"]:
        _DISCOVERY_CACHE.clear()
    cached = _DISCOVERY_CACHE.get(key)
    if cached and time.monotonic() - cached["at"] < ttl:
        _DISCOVERY_CACHE.move_to_end(key)
        _DISCOVERED.update(cached, root=project_path)
        return cached["items"]
    found: list[tuple[str, str]] = []
    skipped: list[str] = []
    seen = {os.path.realpath(project_path)} if project_path else set()

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
                if linked_worktree(e.path):
                    continue   # a worktree is no project (New session: Existing worktree)
                if KEY_RX.match(child_rel):
                    found.append((child_rel, e.path))
                else:
                    skipped.append(child_rel)
                continue   # a checkout: its own subdirectories are not projects
            if depth < max_depth:
                walk(e.path, child_rel, depth + 1)

    if project_path and os.path.isdir(project_path):
        walk(project_path, "", 1)
    entry = dict(at=time.monotonic(), items=found, skipped=skipped)
    _DISCOVERY_CACHE[key] = entry
    _DISCOVERY_CACHE.move_to_end(key)
    while len(_DISCOVERY_CACHE) > 256:
        _DISCOVERY_CACHE.popitem(last=False)
    _DISCOVERED.update(entry, root=project_path)
    return found


def skipped(project_path: str | None = None) -> list[str]:
    """Checkouts and projects.json entries never offered because their name is no valid key (KEY_RULE):
    the doctor names them - otherwise a folder like `my project` just never shows up in New session."""
    with _DISCOVERY_LOCK:
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
        with _DISCOVERY_LOCK:
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
    """The derived project `<key>@<name>`: base's linked worktree in `<base>/.claude/worktrees/<name>`, else
    the one git registered as `<base>/.git/worktrees/<name>` wherever it lies (`git worktree add` elsewhere,
    another tool's worktree folder), with base's GitLab projects and profile. Both must point back to base's
    repository (worktree_gitdir). Never in load(): the lists show the base project, a worktree is opened
    from it (New session: Existing worktree)."""
    try:
        path = os.path.realpath(worktree_path(base, name))
    except ValueError:
        return None
    if os.path.dirname(path) != os.path.realpath(os.path.join(base["path"], WORKTREES)) \
            or not worktree_gitdir(base["path"], path):
        path = external_worktree(base["path"], name)
        if not path:
            return None
    return {"key": "%s@%s" % (base["key"], name), "path": path, "gitlab": list(base["gitlab"]),
            "github": list(base.get("github") or []),
            "profile": base["profile"], "title": "%s @ %s" % (base["title"], name), "exists": True,
            "source": "worktree", "base": base["key"], "worktree": name}


def external_worktree(base_path: str, name: str) -> str | None:
    """The checkout of base's linked worktree registered as `.git/worktrees/<name>` outside `.claude/worktrees`
    (its `gitdir` file names `<checkout>/.git`, which names it back); no git runs."""
    if not WORKTREE_RX.match(name or "") or name in (".", ".."):
        return None
    gd = os.path.join(os.path.realpath(os.path.join(base_path, ".git")), "worktrees", name)
    try:
        with open(os.path.join(gd, "gitdir"), encoding="utf-8") as f:
            dotgit = f.read(4096).strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not os.path.isabs(dotgit) or os.path.basename(dotgit) != ".git":
        return None
    wt = os.path.realpath(os.path.dirname(dotgit))
    if os.path.dirname(wt) == os.path.realpath(os.path.join(base_path, WORKTREES)):
        return None   # inside .claude/worktrees: only by its own folder name
    return wt if worktree_gitdir(base_path, wt) == os.path.realpath(gd) else None


def load(project_path: str | None = None) -> dict:
    """projects.json merged with its `globs` checkouts and those discovered under PROJECT_PATH."""
    data = read_json(PROJECTS_FILE, {"projects": {}})
    # Overlapping globs and discovery can name the same checkout repeatedly.
    # Keep resolution local to this inventory; later calls still see moved links.
    paths = {}
    def real(path):
        if path not in paths:
            paths[path] = os.path.realpath(path)
        return paths[path]
    excluded = {real(p) for p in (data.get("excluded_paths") or []) if isinstance(p, str)}
    out = {}
    for key, p in (data.get("projects") or {}).items():
        if not KEY_RX.match(key) or not isinstance(p, dict) or not p.get("path"):
            continue
        path = real(os.path.expanduser(str(p["path"])))
        out[key] = {"key": key, "path": path, "gitlab": list(p.get("gitlab") or []),
                    "github": [str(x) for x in p.get("github") or [] if isinstance(x, str)],   # W23: owner/repo
                    "profile": p.get("profile") or None, "title": p.get("title") or key,
                    "exists": os.path.isdir(path), "source": "projects.json"}
        if "sandbox_env" in p:
            out[key]["sandbox_env"] = p["sandbox_env"]
    known = {p["path"] for p in out.values()}
    for pattern in data.get("globs") or []:
        for path in sorted(glob.glob(os.path.expanduser(str(pattern)))):
            canonical = real(path)
            # Accept both checkout patterns and folders containing checkouts. A plain
            # custom root used to match only the folder itself and silently find nothing.
            if os.path.exists(os.path.join(canonical, ".git")):
                candidates = [] if linked_worktree(canonical) else [("/".join(canonical.rstrip("/").split("/")[-2:]), canonical)]
            else:
                candidates = [(key if "/" in key else os.path.basename(canonical) + "/" + key, child)
                              for key, child in discover(canonical)]
            for key, child in candidates:
                child = real(child)
                if child in known or child in excluded or key in out or not KEY_RX.match(key):
                    continue
                out[key] = {"key": key, "path": child, "gitlab": [], "profile": None,
                            "title": key, "exists": True, "source": "glob"}
                known.add(child)
    pp = project_path if project_path is not None else os.environ.get("PROJECT_PATH", "")
    for key, path in discover(pp):
        canonical = real(path)
        if canonical in known or canonical in excluded or key in out:
            continue
        out[key] = {"key": key, "path": canonical, "gitlab": [], "profile": None,
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


def session_defaults(project_path: str | None = None, *, available: dict | None = None) -> dict:
    """Host-owned start preferences for registered and discovered base projects.

    Bind a preference to its checkout as well as its key so reusing a key for a different
    directory cannot give that project the previous project's privileges.
    """
    data = read_json(PROJECTS_FILE, {})
    saved = data.get("session_defaults")
    out = {}
    if not isinstance(saved, dict) or not saved:
        return out
    if available is None:
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
