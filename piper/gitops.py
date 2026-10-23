"""Git on the host. Since 2026-10-01 git is open in the sandbox too (K1
accepted); these tools remain for the GUI, for push (no GitLab access in a sandbox) and for
sessions whose `.git` is hidden again (force_exclude [".git"]).

Two audiences:

  the GUI (the human)   full status, diffs, commit of chosen files, push after a confirm
  a session (MCP)       status and diff with every policy path removed - by pathspec AND
                        by dropping diff blocks whose header names such a path - plus a
                        one-line log. Never `show <rev>:<path>` or `log -p`: history
                        can rename a secret past a pathspec.

The working tree is agent-writable, so nothing in it may run on the host through git:
diffs use --no-ext-diff --no-textconv, core.fsmonitor is forced off, and when
core.hooksPath points INTO the working tree (husky & co.) a commit from the GUI runs with
hooks disabled and says so. Calls a SESSION triggers (MCP) additionally ignore the global
and system git config (GIT_CONFIG_GLOBAL=/dev/null): only the human's own clicks use it
(identity, credential helpers for push).

Since 2026-10-01 the sandbox can write `.git` itself (git is open there, K1 accepted): the
repository's own config and hooks are then sandbox-written too, and every host git call
here - including automatic ones (the GUI's status poll, snapshot() on every turn) - reads
that config (filter drivers run on `status`/`add`). Accepted with the decision; see
Settings → Security for the options to narrow it.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess

import common
import extensions


TIMEOUT = 30
MAX_DIFF = 1 << 20
ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
# for calls a sandbox can trigger: no global/system config at all
SESSION_ENV = dict(ENV, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
SAFE = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
NO_COLOR = [x for k in ("ui", "diff", "grep", "branch", "status", "showBranch", "interactive", "decorate")
            for x in ("-c", "color.%s=never" % k)]
# policy git_fail_closed (Manager sets it, on by default): host git runs no hooks at all (a sandbox writes
# .git/hooks and core.hooksPath in git_mode "hole"), no local-path / file:// transport (a push into a
# sandbox-built repository runs ITS hooks on the host, whatever core.hooksPath says here) and refuses a
# repository whose config names a program (refuse_exec_config)
FAIL_CLOSED = False


def _closed() -> list[str]:
    return ["-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never"] if FAIL_CLOSED else []


def _closed_env(env: dict) -> dict:
    """piperd's own environment must not re-open what _closed() shuts: GIT_ALLOW_PROTOCOL overrides every
    protocol.*.allow, GIT_CONFIG_PARAMETERS / GIT_CONFIG_COUNT add config after the -c options."""
    if FAIL_CLOSED:
        for k in ("GIT_ALLOW_PROTOCOL", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
            env.pop(k, None)
    return env


# git-lfs exactly as `git lfs install --local [--skip-smudge]` writes it: the host's own git-lfs, nothing a
# sandbox chose - such a repository stays usable under git_fail_closed
LFS_FILTER = {"filter.lfs.clean": ("git-lfs clean -- %f",),
              "filter.lfs.smudge": ("git-lfs smudge -- %f", "git-lfs smudge --skip -- %f"),
              "filter.lfs.process": ("git-lfs filter-process", "git-lfs filter-process --skip"),
              "filter.lfs.required": ("true",)}
# git-lfs keys that name a program (custom transfer agents, extensions): refused like the git ones
_LFS_EXEC_RX = re.compile(r"^lfs\.(customtransfer\.|extension\.|standalonetransferagent$)")


class GitError(Exception):
    pass


def _git(cwd: str, *args: str, timeout: int = TIMEOUT, ok=(0,), session: bool = False,
         env: dict | None = None, input: bytes | None = None) -> str:
    env = _closed_env(dict(os.environ, **(SESSION_ENV if session else ENV), **(env or {})))
    try:
        r = subprocess.run(["git", *SAFE, *_closed(), "-C", cwd, *args], capture_output=True, timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL if input is None else None, input=input)
    except subprocess.TimeoutExpired:
        raise GitError("git %s timed out" % args[0]) from None
    except OSError as e:
        raise GitError("git not available: %s" % e) from None
    if r.returncode not in ok:
        raise GitError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[:2000] or
                       "git %s failed (rc %d)" % (args[0], r.returncode))
    return r.stdout.decode("utf-8", "replace")


def read_blob(top: str, oid: str, env: dict | None = None, limit: int = 2 << 20) -> bytes:
    """Read a validated, size-checked blob with a hard output cap; no revision:path parsing."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
        raise GitError("invalid blob id")
    child_env = _closed_env(dict(os.environ, **ENV, **(env or {})))
    with subprocess.Popen(["git", *SAFE, *_closed(), "-C", top, "cat-file", "blob", oid],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, env=child_env) as proc:
        try:
            raw = proc.stdout.read(limit + 1)
            if len(raw) > limit:
                raise GitError("blob exceeds read limit")
            if proc.wait(timeout=TIMEOUT):
                raise GitError("cannot read blob")
            return raw
        finally:
            if proc.poll() is None:
                proc.kill()


def toplevel(path: str, session: bool = False) -> str:
    return _git(path, "rev-parse", "--show-toplevel", session=session).strip()


def status(path: str, hidden=None, session: bool = False) -> dict:
    """{root, branch, upstream, ahead, behind, files: [{path, x, y, orig?, sensitive}]}.
    `hidden(path_relative_to_the_repo_root) -> bool` marks files the sandbox must not see."""
    root = toplevel(path, session)
    out = _git(root, "status", "--porcelain=v1", "-z", "-b", "--untracked-files=all", session=session)
    parts = out.split("\0")
    info = {"root": root, "branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": []}
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("## "):
            head = rec[3:]
            branch, _, rest = head.partition("...")
            info["branch"] = branch.replace("No commits yet on ", "")
            up, _, track = rest.partition(" [")
            info["upstream"] = up.strip()
            for item in track.rstrip("]").split(", "):
                if item.startswith("ahead "):
                    info["ahead"] = int(item[6:])
                elif item.startswith("behind "):
                    info["behind"] = int(item[7:])
            continue
        x, y, p = rec[0], rec[1], rec[3:]
        f = {"path": p, "x": x, "y": y}
        if x in "RC":
            f["orig"] = parts[i]
            i += 1
        f["sensitive"] = bool(hidden and (hidden(p) or ("orig" in f and hidden(f["orig"]))))
        info["files"].append(f)
    return info


def _hooks_in_worktree(root: str) -> bool:
    try:
        hp = _git(root, "config", "--get", "core.hooksPath", ok=(0, 1)).strip()
    except GitError:
        return False
    if not hp:
        return False
    full = os.path.realpath(hp if os.path.isabs(hp) else os.path.join(root, hp))
    return full == os.path.realpath(root) or full.startswith(os.path.realpath(root) + os.sep)


def _git_dir_hooks(root: str) -> bool:
    """Hooks in .git/hooks (not the shipped *.sample) - what fail-closed host git skips."""
    try:
        d = os.path.join(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", session=True).strip(), "hooks")
        return any(not n.endswith(".sample") for n in os.listdir(d))
    except (GitError, OSError):
        return False


def diff(path: str, file: str | None = None, untracked: bool = False) -> str:
    root = toplevel(path)
    if file is not None:
        _check_rel(root, file)
    if untracked and file:
        try:   # --no-index would read a FIFO / device until the timeout
            mode = os.lstat(os.path.join(root, file)).st_mode
        except OSError:
            mode = stat.S_IFREG
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            return "%s is not a regular file - no diff\n" % file
        out = _git(root, "diff", "--no-index", "--no-color", "--no-ext-diff", "--no-textconv", "--",
                   "/dev/null", file, ok=(0, 1))
    else:
        base = ["diff", "HEAD", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]   # strict_filter parses a/X b/X only
        spec = [literal(file)] if file else []
        try:
            out = _git(root, *base, "--", *spec)
        except GitError:   # no HEAD yet (fresh repository)
            out = _git(root, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-textconv", "--", *spec)
    return out[:MAX_DIFF] + ("\n[diff truncated at 1 MiB]\n" if len(out) > MAX_DIFF else "")


def _check_rel(root: str, rel: str) -> None:
    if not rel or rel.startswith("/") or ".." in rel.split("/") or "\0" in rel:
        raise GitError("invalid path %r" % rel)
    full = os.path.realpath(os.path.join(root, rel))
    if not (full == root or full.startswith(os.path.realpath(root) + os.sep)):
        raise GitError("path %r leaves the repository" % rel)


def refuse_exec_config(top: str) -> None:
    """Fail closed: host git never runs in a repository whose own config names a program (filter
    drivers, fsmonitor, hooksPath, sshCommand, aliases, credential helpers ... - a sandbox can write
    .git/config in git_mode "hole"). Checked BEFORE any git that could start one (status, add, push)."""
    if not FAIL_CLOSED:
        return
    keys = [k for k, v in _exec_config_entries(top) if k not in ("core.fsmonitor", "core.hookspath")   # _git neutralizes these
            and k != "core.pager"                                               # every host git runs with GIT_PAGER=cat
            and not k.startswith("http.")                                       # TLS / proxy settings run nothing
            and not (k.startswith("filter.") and not k.endswith((".clean", ".smudge", ".process")))
            and v.strip() not in LFS_FILTER.get(k, ())                          # the standard git-lfs filter
            and not any(extensions.call("git_trusted_config", k, v))]           # an extension's own host-run filter
    if keys:
        raise GitError("host git refused: the repository config names programs host git would run (%s) - remove "
                       "them from .git/config (or ask the user) first" % ", ".join(sorted(set(keys))[:5]))


def check_amend(top: str) -> None:
    """Amend only a commit no remote branch has: rewriting a pushed one is the terminal's job."""
    if not has_head(top):
        raise GitError("nothing to amend - no commit yet")
    if _git(top, "branch", "-r", "--format=%(refname)", "--contains", "HEAD").strip():
        raise GitError("HEAD is already pushed - amend it from a terminal if you really mean to rewrite it")


def commit(path: str, message: str, files: list[str], amend: bool = False) -> dict:
    """Commit exactly `files` (they must be in the current status) with `message`; amend: replace HEAD
    with them (no files: its message only)."""
    refuse_exec_config(toplevel(path))
    message = message.strip()
    if not message or len(message) > 10000:
        raise GitError("commit message must be 1..10000 characters")
    st = status(path)
    root = st["root"]
    known = {f["path"] for f in st["files"]} | {f.get("orig") for f in st["files"] if f.get("orig")}
    files = [f for f in files if f]
    if not files and not amend:
        raise GitError("no files selected")
    unknown = [f for f in files if f not in known]
    if unknown:
        raise GitError("not changed (refresh the panel): %s" % ", ".join(unknown[:5]))
    for f in files:
        _check_rel(root, f)
    spec = [":(literal)" + f for f in files]
    if spec:
        _git(root, "add", "-A", "--", *spec)
    hooks_off = _hooks_in_worktree(root) or (FAIL_CLOSED and _git_dir_hooks(root))
    extra = ["-c", "core.hooksPath=/dev/null"] if hooks_off and not FAIL_CLOSED else []
    _git(root, *extra, "commit", *(["--amend", "--only"] if amend else []), "-m", message, *(["--", *spec] if spec else []),
         timeout=120)
    head = _git(root, "log", "-1", "--format=%h %s").strip()
    return {"commit": head, "hooks_disabled": hooks_off}


def tree_versions(top: str, tree: str, paths: list[str], objects: str | None = None) -> dict:
    """Object and mode of exact paths in a pinned tree, including explicit absences."""
    out = {p: {"oid": None, "mode": None} for p in paths}
    if not paths:
        return out
    raw = _git(top, "ls-tree", "-z", tree, "--", *(literal(p) for p in paths), env=snap_env(top, objects))
    for rec in raw.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, kind, oid = meta.split()
        if path in out:
            out[path] = {"oid": oid, "mode": mode}
    return out


def tree_numstat(top: str, a: str, b: str, objects: str | None = None) -> dict:
    """Numstat by destination, retaining a rename as one entry and binary counts as None."""
    parts = _git(top, "diff-tree", "-r", "-M", "--numstat", "-z", "--no-commit-id", a, b,
                 env=snap_env(top, objects)).split("\0")
    out, i = {}, 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if not rec:
            continue
        added, deleted, path = rec.split("\t", 2)
        if not path:
            i += 1  # numstat -z rename: empty name, old path, new path
            path = parts[i]
            i += 1
        out[path] = {"add": int(added) if added.isdigit() else None,
                     "del": int(deleted) if deleted.isdigit() else None}
    return out


def tree_blob(top: str, tree: str, path: str, env: dict | None = None, limit: int = 2 << 20) -> dict:
    """An already-authorized tree path: resolve literally, check size, then read the blob id."""
    entry = _git(top, "ls-tree", "-z", tree, "--", literal(path), env=env)
    if not entry:
        return {"path": path, "tree": tree, "state": "missing"}
    meta, name = entry.rstrip("\0").split("\t", 1)
    mode, kind, oid = meta.split()
    if name != path:
        raise GitError("path does not identify one tree entry")
    out = {"path": path, "tree": tree, "oid": oid, "mode": mode}
    if kind != "blob":
        return dict(out, state="unsupported")
    size = int(_git(top, "cat-file", "-s", oid, env=env).strip())
    out["size"] = size
    if size > limit:
        return dict(out, state="too_large")
    raw = read_blob(top, oid, env, limit)
    if b"\0" in raw[:8192]:
        return dict(out, state="binary")
    return dict(out, state="text", text=raw.decode("utf-8", "replace"), final_newline=raw.endswith(b"\n"))


def stage_reviewed(top: str, path: str, version: dict, objects: str) -> None:
    """Stage a pinned blob rather than running filters on a possibly changed working file."""
    refuse_exec_config(top)
    _check_rel(top, path)
    oid, mode = version.get("oid"), version.get("mode")
    if mode not in ("100644", "100755", "120000") or not isinstance(oid, str) or not _TREE.fullmatch(oid):
        raise GitError("unsupported reviewed file mode or blob id")
    env = snap_env(top, objects)
    if int(_git(top, "cat-file", "-s", oid, env=env).strip()) > (2 << 20):
        raise GitError("file is too large to review (2 MiB)")
    copied = _git(top, "hash-object", "-w", "--stdin", input=read_blob(top, oid, env)).strip()
    if copied != oid:
        raise GitError("reviewed blob did not retain its object id")
    _git(top, "update-index", "-z", "--index-info", input=("%s %s\t%s\0" % (mode, oid, path)).encode("utf-8", "surrogateescape"))


def commit_reviewed(top: str, message: str, head: str, versions: dict, objects: str, index: str,
                    amend: bool = False) -> dict:
    """Commit pinned blobs, never reread working files. CAS on HEAD closes the ref race.

    A private index leaves unrelated staging intact. commit-tree bypasses hooks which could
    rewrite reviewed content. An agent writing after validation keeps its edit uncommitted.
    """
    message = message.strip()
    if not message or len(message) > 10000:
        raise GitError("commit message must be 1..10000 characters")
    refuse_exec_config(top)
    env = dict(pinned_env(top), GIT_INDEX_FILE=index)
    records = []
    try:
        _git(top, "read-tree", *([head] if head else ["--empty"]), env=env)
        source = snap_env(top, objects)
        for path, version in versions.items():
            _check_rel(top, path)
            oid, mode = version["oid"], version["mode"]
            if oid is None:
                records.append("0 %s\t%s\0" % ("0" * (len(head) or 40), path))
                continue
            if mode not in ("100644", "100755", "120000") or not _TREE.fullmatch(oid):
                raise GitError("unsupported reviewed file mode or blob id")
            size = int(_git(top, "cat-file", "-s", oid, env=source).strip())
            if size > (2 << 20):
                raise GitError("file is too large to review (2 MiB)")
            data = read_blob(top, oid, source)
            copied = _git(top, "hash-object", "-w", "--stdin", input=data).strip()
            if copied != oid:
                raise GitError("reviewed blob did not retain its object id")
            records.append("%s %s\t%s\0" % (mode, oid, path))
        raw = "".join(records).encode("utf-8", "surrogateescape")
        _git(top, "update-index", "-z", "--index-info", env=env, input=raw)
        tree = _git(top, "write-tree", env=env).strip()
        if amend:   # HEAD's own parents (check_amend: HEAD is on no remote branch)
            if not head:
                raise GitError("nothing to amend - no commit yet")
            parents = _git(top, "show", "-s", "--format=%P", head).split()
            who = _git(top, "show", "-s", "--format=%an%x00%ae%x00%aI", head).rstrip("\n").split("\0")
            author = dict(zip(("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE"), who))
        elif head and tree == _git(top, "rev-parse", head + "^{tree}").strip():
            raise GitError("reviewed files have no changes against HEAD")
        else:
            parents, author = [head] if head else [], {}
        commit_id = _git(top, "commit-tree", tree, *[x for p in parents for x in ("-p", p)], "-m", message,
                         env=author).strip()
        _git(top, "update-ref", "-m", "commit: " + message.splitlines()[0], "HEAD", commit_id,
             head or "0" * len(commit_id))
        warning = ""
        try:
            _git(top, "update-index", "-z", "--index-info", input=raw)
        except GitError:
            # HEAD already advanced atomically; report success with the index repair needed.
            warning = "Committed, but the selected index entries could not be updated; refresh the index."
        result = {"commit": _git(top, "show", "-s", "--format=%h %s", commit_id).strip(), "hooks_disabled": True}
        if warning:
            result["index_warning"] = warning
        return result
    finally:
        for name in (index, index + ".lock"):
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def unpushed_deletions(path: str) -> list[str]:
    """Repo-relative paths that commits on HEAD no remote branch has yet delete, that HEAD
    does not have, and that still exist in the host's working tree - a deletion the host
    did itself (rm, a rename) is gone from the disk too; one committed in a sandbox, where
    the file was hidden, is not. `-c`: a merge counts with what differs from ALL its parents
    (a conflict resolved by `git rm`), not with what a merged branch already deleted.
    --no-renames: a copy of a hidden file's old content elsewhere does not turn its deletion
    into a rename."""
    root = toplevel(path)
    if not _git(root, "remote").strip():
        return []   # nowhere to push to: nothing to check (and no walk through the whole history)
    out = _git(root, "log", "-z", "--format=", "--name-only", "--no-renames", "--diff-filter=D", "-c", "HEAD",
               "--not", "--remotes")
    gone = {p.strip("\n") for p in out.split("\0")} - {""}
    if not gone:
        return []
    have = set(_git(root, "ls-tree", "-r", "-z", "--name-only", "--full-tree", "HEAD").split("\0"))
    return sorted(p for p in gone if p not in have and os.path.lexists(os.path.join(root, p)))


def remote_refs_known(path: str) -> bool:
    """Whether anything was fetched from (or pushed to) a remote: without it every commit
    is unpushed, so unpushed_deletions checks the whole history."""
    return bool(_git(toplevel(path), "for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes").strip())


MAIN_LINES = ("main", "master", "develop", "dev")   # a direct push warns; an extension adds its own (MAIN_LINES)
FIXUP_RE = re.compile(r"^(fixup!|squash!|amend!|wip\b|WIP\b|tmp\b|temp\b)", re.I)
PUSH_COMMITS_MAX = 200


def main_lines() -> tuple:
    return MAIN_LINES + tuple(extensions.items("MAIN_LINES"))


def _cfg(root: str, key: str) -> str:
    try:
        return _git(root, "config", "--get", "--end-of-options", key).strip()
    except GitError:
        return ""


def push_plan(root: str) -> dict:
    """Where a push of HEAD goes: {branch, remote, target (the remote's branch), upstream, args, new (no such
    remote branch yet), ref (refs/remotes/... or "")}. The upstream when it has the branch's name; otherwise
    (none, or another name: plain `git push` refuses that with push.default=simple) <remote>/<branch>, set as
    the upstream by the push."""
    branch = current_branch(root)
    up = upstream(root)
    remote = _cfg(root, "branch.%s.remote" % branch) if branch != "HEAD" else ""
    merge = _cfg(root, "branch.%s.merge" % branch) if branch != "HEAD" else ""
    remotes = [r for r in _git(root, "remote").split() if r]
    if remote not in remotes:
        remote = "origin" if "origin" in remotes else (remotes[0] if remotes else "")
    same = bool(up) and merge == "refs/heads/" + branch
    args = ["push"] if same else ["push", "-u", remote, "HEAD"]
    ref = "refs/remotes/%s/%s" % (remote, branch)
    exists = bool(remote) and branch != "HEAD" and ref_exists(root, ref)
    return {"branch": branch, "remote": remote, "target": branch, "upstream": up, "args": args,
            "new": not exists, "ref": ref if exists else "", "remotes": remotes}


def push_preview(path: str, hidden=None, withheld=None) -> dict:
    """What a push would send (the GUI's Push dialog, IntelliJ's Push Commits): the plan, the commits not on the
    remote branch yet (all remotes for a new one), the files they change together, and checks
    [{level: block|warn|info, text}] - a block disables Push. hidden(path) = hidden from Claude."""
    root = toplevel(path)
    plan = push_plan(root)
    st = status(root)
    checks: list[dict] = []

    def add(level: str, text: str) -> None:
        checks.append({"level": level, "text": text})
    branch, remote = plan["branch"], plan["remote"]
    commits: list[dict] = []
    files: list[dict] = []
    base, behind, more = "", 0, False
    if branch == "HEAD":
        add("block", "HEAD is detached - switch to a branch to push.")
    elif not remote:
        add("block", "The repository has no remote to push to.")
    elif has_head(root):
        rng = [plan["ref"] + "..HEAD"] if plan["ref"] else ["HEAD", "--not", "--remotes"]
        out = _git(root, *VIEW, "log", "--no-color", "-n", str(PUSH_COMMITS_MAX + 1),
                   "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%P%x1e", *rng, "--", timeout=60)
        for rec in out.split("\x1e"):
            f = rec.strip("\n").split("\x1f")
            if len(f) < 6:
                continue
            more = len(commits) >= PUSH_COMMITS_MAX
            if more:
                break
            commits.append({"hash": f[0], "short": f[1], "author": f[2], "date": f[3], "subject": f[4],
                            "merge": len(f[5].split()) > 1})
        if plan["ref"]:
            behind = int(_git(root, "rev-list", "--count", "HEAD.." + plan["ref"]).strip() or 0)
            base = _git(root, "merge-base", "HEAD", plan["ref"]).strip() if commits else ""
        elif commits:
            oldest = _git(root, "rev-list", "--parents", "-n", "1", commits[-1]["hash"]).split()
            base = oldest[1] if len(oldest) > 1 else ""
        if commits:
            head = commits[0]["hash"]
            empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904" if len(head) == 40 else \
                "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321"
            files = _tree_files(root, [base or empty, head], withheld or (lambda _p: False))
        if plan["upstream"] and plan["args"] != ["push"]:
            add("warn", "The upstream is %s, a different name: git would refuse the push. It goes to %s/%s and "
                "becomes the upstream." % (plan["upstream"], remote, branch))
        if behind:
            add("block", "%s/%s has %d commit%s that %s not here: the push would be rejected. Pull first." % (
                remote, branch, behind, "" if behind == 1 else "s", "is" if behind == 1 else "are"))
        if not commits:
            if plan["new"]:
                add("info", "No new commits: the push only creates %s/%s." % (remote, branch))
            else:
                add("block", "Nothing to push: %s/%s already has every commit." % (remote, branch))
        if branch in main_lines() and commits:
            add("warn", "This pushes straight to %s/%s - normally a change goes there through a merge request." % (remote, branch))
        fix = [c["subject"] for c in commits if FIXUP_RE.match(c["subject"])]
        if fix:
            add("warn", "%s a fixup / WIP (%s) - squash before pushing?" % (
                "1 commit is" if len(fix) == 1 else "%d commits are" % len(fix), fix[0][:80]))
        merges = sum(1 for c in commits if c["merge"])
        if merges:
            add("info", "%d merge commit%s in the push." % (merges, "" if merges == 1 else "s"))
        if hidden:
            secret = [f["path"] for f in files if hidden(f["path"]) and f["status"][:1] != "D"]
            if secret:
                add("warn", "The push contains %d file%s hidden from Claude (%s) - meant for the server?" % (
                    len(secret), "" if len(secret) == 1 else "s", ", ".join(secret[:3])))
    if st["files"]:
        n = len(st["files"])
        add("info", "%d changed file%s not committed - not part of this push." % (n, "" if n == 1 else "s"))
    return {"branch": branch, "remote": remote, "target": plan["target"], "upstream": plan["upstream"],
            "new": plan["new"], "command": "git " + " ".join(plan["args"]), "base": base, "behind": behind,
            "commits": commits, "more": more, "files": files[:COMMIT_FILES_MAX],
            "truncated": len(files) > COMMIT_FILES_MAX, "checks": checks}


def push(path: str) -> dict:
    root = toplevel(path)
    refuse_exec_config(root)
    plan = push_plan(root)
    if plan["branch"] == "HEAD" or not plan["remote"]:
        raise GitError("nothing to push to: %s" % ("HEAD is detached" if plan["branch"] == "HEAD" else "no remote"))
    lfs = lfs_prepush(root, plan["remote"], ["HEAD"])
    out = _git(root, *plan["args"], timeout=180)
    return {"branch": plan["branch"], "output": (lfs + out)[-2000:]}


def _lfs_repo(root: str) -> bool:
    """Does the repository keep files in Git LFS (its .gitattributes, or an LFS object store)?"""
    try:
        gitdir = _git(root, "rev-parse", "--absolute-git-dir", session=True).strip()
    except GitError:
        return False
    if os.path.isdir(os.path.join(gitdir, "lfs", "objects")):
        return True
    for f in (os.path.join(root, ".gitattributes"), os.path.join(gitdir, "info", "attributes")):
        try:
            with open(f, "rb") as fh:
                if b"filter=lfs" in fh.read(1 << 20):
                    return True
        except OSError:
            pass
    return False


def lfs_prepush(root: str, remote: str, refs: list[str]) -> str:
    """git_fail_closed runs no hooks, so not git-lfs's pre-push either: upload the LFS objects of `refs`
    with the host's own git-lfs first (what that hook does), or the push leaves pointers without their
    objects on the server. A failed upload stops the push."""
    if not FAIL_CLOSED or not refs or not _lfs_repo(root):
        return ""
    if not shutil.which("git-lfs"):
        raise GitError("this repository uses Git LFS but git-lfs is not installed on the host - the push would "
                       "leave the LFS objects behind (brew install git-lfs)")
    return _git(root, "lfs", "push", remote, *refs, timeout=900)


def head_branch(gitdir: str) -> str:
    """Branch of a TRUSTED git dir (Manager.repo_top's record) read from its HEAD - no git
    process, the GUI lists it on every state poll. Never follows a link and never blocks
    (O_NONBLOCK: a FIFO is refused, not waited on); a detached HEAD gives its short sha,
    anything unreadable gives ""."""
    try:
        fd = os.open(os.path.join(gitdir, "HEAD"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        head = os.read(fd, 512).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    finally:
        os.close(fd)
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else head[:8]


def head_is_plain(path: str) -> bool:
    """False when `path/.git` is a directory whose HEAD is not a regular file (a FIFO or a
    link planted by a sandbox would make every git call there wait for its timeout). A
    `.git` file (worktree, submodule) or no `.git` (a subdirectory project) is left to
    Manager.repo_top."""
    gd = os.path.join(path, ".git")
    try:
        if not stat.S_ISDIR(os.lstat(gd).st_mode):
            return True
        return stat.S_ISREG(os.lstat(os.path.join(gd, "HEAD")).st_mode)
    except FileNotFoundError:
        return True
    except OSError:
        return False


def log(path: str, n: int = 20) -> list[str]:
    root = toplevel(path)
    return _git(root, "log", "--oneline", "--no-decorate", "-n", str(max(1, min(n, 100)))).splitlines()


# ------------------------------------------------------------------ the session's (filtered) view
def pathspec_excludes(patterns: list[str]) -> list[str]:
    out = []
    for c in patterns:
        c = c.strip("/")
        if c:
            out += [":(exclude,glob)" + c, ":(exclude,glob)" + c + "/**"]
    return out


def filter_diff(text: str, hidden) -> str:
    """Drop every diff block whose paths the sandbox must not see - the fail-closed
    strict_filter (an unparseable header is withheld too). The sandbox's diffs; the GUI's
    views call strict_filter with `.git` only."""
    return strict_filter(text, hidden)


def session_status(path: str, hidden) -> dict:
    """What a session may know: `hidden(repo-relative path)` decides (policy candidates, the
    effective hole excludes, and anything outside the session's project dir)."""
    st = status(path, hidden, session=True)
    shown = [{k: f[k] for k in ("path", "x", "y") if k in f} for f in st["files"] if not f["sensitive"]]
    return {"branch": st["branch"], "upstream": st["upstream"], "ahead": st["ahead"], "behind": st["behind"], "files": shown,
            "withheld": len(st["files"]) - len(shown)}


def pinned_env(top: str) -> dict:
    """GIT_DIR / GIT_WORK_TREE of a trusted root: git run in a subdirectory then never
    discovers a .git a sandbox planted in between."""
    return {"GIT_DIR": _git(top, "rev-parse", "--absolute-git-dir", session=True).strip(), "GIT_WORK_TREE": top}


def session_diff(path: str, hidden, patterns: list[str], file: str | None = None, top: str | None = None) -> str:
    """Runs in the PROJECT dir: `.` and the exclude pathspecs (hole patterns, relative to the
    project) apply there; output paths are repo-relative, as `hidden` expects. `top`: the
    trusted root (the repository is taken from it, not discovered from `path`)."""
    env = pinned_env(top) if top else None
    root = top or toplevel(path, session=True)
    if file:
        _check_rel(path, file)
        if hidden(os.path.relpath(os.path.realpath(os.path.join(path, file)), os.path.realpath(root))):
            raise GitError("%s is hidden from this sandbox" % file)
    spec = ([file] if file else ["."]) + pathspec_excludes(patterns)
    base = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--"]   # strict_filter parses a/X b/X only
    try:
        out = _git(path, "diff", "HEAD", *base, *spec, session=True, env=env)
    except GitError:
        out = _git(path, "diff", "--cached", *base, *spec, session=True, env=env)
    out = filter_diff(out, hidden)
    return out[:MAX_DIFF] + ("\n[diff truncated at 1 MiB]\n" if len(out) > MAX_DIFF else "")


def session_log(path: str, n: int = 20) -> list[str]:
    root = toplevel(path, session=True)
    return _git(root, "log", "--oneline", "--no-decorate", "-n", str(max(1, min(n, 100))),
                session=True).splitlines()


# ------------------------------------------------------------------ 9.7: the GUI's project-keyed views
# Everything below serves the human's GUI keyed by PROJECT (not by session), and still never
# returns the content of a file the project's session may not see: callers pass
# `hidden(repo-relative path)`, paths from the client are pathspec-literal, diffs run
# --no-renames (a rename pairs a hidden source with a visible target) and every patch goes
# through strict_filter(). A user's global config must not change the output format we parse.
VIEW = ["-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false",
        "-c", "core.quotePath=false", "-c", "log.showSignature=false", "-c", "color.ui=never"]
PATCH = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEX = re.compile(r"^[0-9a-f]{4,40}$")
_TREE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
MAX_EXCLUDES = 500


def literal(rel: str) -> str:
    return ":(literal)" + rel


def parse_u0(text: str) -> dict:
    """Changed-line marks from a `diff -U0` (new-file line numbers, IntelliJ-style gutter):
    a pure insertion is `added`, a pure deletion a `deleted` marker after that line (0 = top),
    a replaced run `changed` (its surplus new lines `added`, surplus old ones a `deleted`)."""
    added, changed, deleted = [], [], []
    for line in text.splitlines():
        m = _HUNK.match(line)
        if not m:
            continue
        b = 1 if m.group(2) is None else int(m.group(2))
        c = int(m.group(3))
        d = 1 if m.group(4) is None else int(m.group(4))
        if b == 0 and d > 0:
            added.append([c, c + d - 1])
        elif d == 0:
            deleted.append(c)
        else:
            k = min(b, d)
            changed.append([c, c + k - 1])
            if d > b:
                added.append([c + b, c + d - 1])
            elif b > d:
                deleted.append(c + d - 1)
    return {"added": added, "changed": changed, "deleted": deleted}


def file_marks(top: str, rel: str, nlines) -> dict:
    """Marks of one working-tree file vs HEAD; `nlines()` counts the lines of an untracked file."""
    st = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", literal(rel))
    if st.startswith("?? ") or (st[:1] == "A" and not has_head(top)):
        n = nlines()
        return {"added": [[1, n]] if n else [], "changed": [], "deleted": [], "untracked": True}
    try:
        out = _git(top, *VIEW, "diff", "HEAD", "-U0", *PATCH, "--", literal(rel))
    except GitError:
        out = ""
    return dict(parse_u0(out), untracked=False)


def has_head(top: str) -> bool:
    try:
        _git(top, "rev-parse", "--verify", "--quiet", "HEAD")
        return True
    except GitError:
        return False


def _unquote(s: str) -> tuple[str, str] | None:
    """One C-quoted git path at the start of `s` -> (path, rest), None when malformed."""
    if not s.startswith('"'):
        return None
    out, i = bytearray(), 1
    esc = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while i < len(s):
        ch = s[i]
        if ch == '"':
            return out.decode("utf-8", "replace"), s[i + 1:]
        if ch == "\\" and i + 1 < len(s):
            nx = s[i + 1]
            if nx in esc:
                out.append(esc[nx])
                i += 2
                continue
            if re.match(r"[0-7]{3}", s[i + 1:i + 4]):
                out.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
            return None
        out += ch.encode()
        i += 1
    return None


def header_paths(line: str) -> list[str] | None:
    """The paths of a `diff --git a/X b/X` header (no renames: X twice), None when unparseable."""
    rest = line[len("diff --git "):].rstrip("\n")
    if rest.startswith('"'):
        a = _unquote(rest)
        if a is None:
            return None
        rest2 = a[1].lstrip(" ")
        b = _unquote(rest2) if rest2.startswith('"') else (rest2, "")
        if b is None or not a[0].startswith("a/") or not b[0].startswith("b/"):
            return None
        return [a[0][2:], b[0][2:]]
    n = len(rest) - 5
    if n <= 0 or n % 2 or not rest.startswith("a/"):
        return None
    k = n // 2
    if rest[2 + k:5 + k] != " b/" or rest[2:2 + k] != rest[5 + k:]:
        return None
    return [rest[2:2 + k]]


def combined_path(line: str) -> list[str] | None:
    """The path of a combined-diff header (`diff --cc X` / `diff --combined X`: a merge in
    progress or a merge commit), None when unparseable. X is the whole rest, maybe C-quoted."""
    rest = line.split(" ", 2)[2].rstrip("\n") if line.count(" ") >= 2 else ""
    if rest.startswith('"'):
        q = _unquote(rest)
        return [q[0]] if q and not q[1] and q[0] else None
    return [rest] if rest else None


# a block header behind colour codes (--color-words: whatever color.ui says) or, from `log --graph`, behind
# graph lines ("| diff --git ...", "  diff --git ..." under the root commit) - N4
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HEADER = re.compile(r"^(diff --.*)", re.S)
_GRAPH_HEADER = re.compile(r"^[|*/\\_ ]*(diff --.*)", re.S)
_LINES = re.compile(r"[^\n]*\n|[^\n]+\Z")


def strict_filter(text: str, hidden, label: str = "a path hidden from Claude", graph: bool = False) -> str:
    """filter_diff that fails closed: a block whose header cannot be parsed exactly (odd
    quoting, an unknown diff format) is withheld like a hidden one. `label` says why a block
    with a parsed header is withheld (the GUI's views: "a .git path", L9). A header behind colour
    codes is a header too; `graph`: so is one behind graph lines (a context line that looks like
    a header there withholds its block - fail closed)."""
    out, keep = [], True
    rx = _GRAPH_HEADER if graph else _HEADER
    for line in _LINES.findall(text):   # git ends a line at \n only (splitlines also breaks at \r, \x1c, U+2028 - N5)
        m = rx.match(_ANSI.sub("", line) if "\x1b" in line else line)
        if m:
            hdr = m.group(1)
            paths = (header_paths(hdr) if hdr.startswith("diff --git ")
                     else combined_path(hdr) if hdr.startswith(("diff --cc ", "diff --combined ")) else None)
            keep = paths is not None and not any(hidden(p) for p in paths)
            if not keep:
                out.append("diff --git [withheld: %s]\n" % (label if paths else "unparseable header"))
        if keep:
            out.append(line)
    return "".join(out)


_CHECK_HEADER = re.compile(r"^(.+):(\d+): (\S[^\n]*)\n?\Z")


def check_filter(text: str, hidden, label: str = "a path hidden from Claude") -> str:
    """N6: `--check` prints `<path>:<n>: trailing whitespace.` and then the offending line (`+...`) - outside any
    `diff --git` block, so strict_filter keeps it. The line after a hidden (or unparseable) path's entry is
    withheld; a `+` line that follows no entry at all as well (fail closed)."""
    out, keep = [], False
    for line in _LINES.findall(text):
        m = _CHECK_HEADER.match(line)
        if m:
            keep = not hidden(m.group(1))
            out.append(line)
        elif line.startswith("+"):
            out.append(line if keep else "+[withheld: %s]\n" % label)
        else:
            keep = False
            out.append(line)
    return "".join(out)


def _excludes(names: list[str], hidden) -> tuple[list[str], int]:
    """(pathspec after `--`, number withheld) for a patch that must leave hidden files out."""
    withheld = sorted({p for p in names if p and hidden(p)})
    if not withheld:
        return [], 0
    if len(withheld) > MAX_EXCLUDES:   # too long an argv: strict_filter alone drops them
        return [], len(withheld)
    return ["."] + [":(exclude,literal)" + p for p in withheld], len(withheld)


def _bounded(out: str) -> str:
    return out[:MAX_DIFF] + ("\n[diff truncated at 1 MiB]\n" if len(out) > MAX_DIFF else "")


def file_diff(top: str, rel: str, hidden, label: str = "a path hidden from Claude", *, symlink_text: bool = False) -> str:
    """Working tree vs HEAD of one file (untracked: vs /dev/null); blocks `hidden` names withheld.
    symlink_text: an untracked symlink diffs as its target text."""
    st = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", literal(rel))
    if st.startswith("?? "):
        try:   # --no-index would read a FIFO / device until the timeout
            mode = os.lstat(os.path.join(top, rel)).st_mode
        except OSError:
            mode = stat.S_IFREG
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            return "%s is not a regular file - no diff\n" % rel
        if symlink_text and stat.S_ISLNK(mode):
            # --no-index treats a link to a directory as that directory, not its target text.
            import difflib
            import json
            a, b = (json.dumps(prefix + rel, ensure_ascii=False) for prefix in ("a/", "b/"))
            target = os.readlink(os.path.join(top, rel))
            hunks = difflib.unified_diff([], target.splitlines(keepends=True), "/dev/null", b)
            patch = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                            for line in hunks)
            out = "diff --git %s %s\nnew file mode 120000\n%s" % (a, b, patch)
        else:
            out = _git(top, *VIEW, "diff", "--no-index", *PATCH[:3], "--", "/dev/null", rel, ok=(0, 1))
    else:
        try:
            out = _git(top, *VIEW, "diff", "HEAD", *PATCH, "--", literal(rel))
        except GitError:
            out = _git(top, *VIEW, "diff", "--cached", *PATCH, "--", literal(rel))
    return _bounded(strict_filter(out, hidden, label))


LOG_FMT = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D%x1e"


def commits(top: str, n: int = 50, rel: str | None = None, follow: bool = False) -> list[dict]:
    args = ["log", "--no-color", "-n", str(max(1, min(int(n), 200))), "--format=" + LOG_FMT]
    if rel:
        args += (["--follow"] if follow else []) + ["--", literal(rel)]
    try:
        out = _git(top, *VIEW, *args)
    except GitError:
        if not has_head(top):
            return []
        raise
    rows = []
    for rec in out.split("\x1e"):
        f = rec.strip("\n").split("\x1f")
        if len(f) < 6:
            continue
        row = {"hash": f[0], "short": f[1], "author": f[2], "date": f[3], "subject": f[4]}
        if f[5]:
            row["refs"] = f[5]
        rows.append(row)
    return rows


def parse_blame(text: str) -> list[dict]:
    """`git blame --porcelain` -> BlameLine rows (commit "" = not committed yet)."""
    info: dict[str, dict] = {}
    rows, cur, final = [], None, 0
    for line in text.split("\n"):
        if cur is None:
            m = re.match(r"^([0-9a-f]{40,64}) \d+ (\d+)(?: \d+)?$", line)
            if m:
                cur, final = m.group(1), int(m.group(2))
                info.setdefault(cur, {})
            continue
        if line.startswith("\t"):
            d = info[cur]
            zero = not cur.strip("0")
            rows.append({"line": final, "commit": "" if zero else cur[:8], "author": d.get("author", ""),
                         "time": int(d.get("author-time") or 0), "summary": d.get("summary", ""),
                         "text": line[1:]})
            cur = None
            continue
        k, _, v = line.partition(" ")
        if k in ("author", "author-time", "summary"):
            info[cur][k] = v
    return rows


def blame(top: str, rel: str) -> list[dict]:
    return parse_blame(_git(top, *VIEW, "blame", "--porcelain", "--no-textconv", "--", rel, timeout=60))


def is_tracked(top: str, rel: str) -> bool:
    return bool(_git(top, "ls-files", "-z", "--", literal(rel)).strip("\0"))


def graph(top: str, n: int = 80) -> list[str]:
    try:
        out = _git(top, *VIEW, "log", "--graph", "--oneline", "--decorate", "--all", "--no-color",
                   "-n", str(max(1, min(int(n), 300))))
    except GitError:
        if not has_head(top):
            return []
        raise
    return [l[:400] for l in out.splitlines()]


def resolve_commit(top: str, c: str) -> str:
    """A commit id (never a blob or tree: `git show <blob>` prints its content)."""
    if not _HEX.match(c or ""):
        raise GitError("commit must be 4-40 hex characters")
    try:
        return _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", c + "^{commit}").strip()
    except GitError:
        raise GitError("%s is not a commit of this repository" % c) from None


def show(top: str, commit: str, hidden, label: str = "a path hidden from Claude") -> str:
    full = resolve_commit(top, commit)
    common = ["--diff-merges=first-parent", "--no-renames"]
    names = _git(top, *VIEW, "show", *common, "--name-only", "-z", "--format=", full).split("\0")
    spec, n = _excludes([x.strip("\n") for x in names], hidden)
    out = _git(top, *VIEW, "show", "--stat", "-p", *PATCH, *common, "--format=fuller", full,
               *(["--", *spec] if spec else []))
    out = strict_filter(out, hidden, label)
    note = "[%d file(s) withheld: %s]\n" % (n, label) if n else ""
    return _bounded(note + out)


# ------------------------------------------------------------------ turn snapshots (9.7 #2)
def snap_env(top: str, objects: str | None) -> dict:
    """Snapshot objects go to `objects` (a directory of the session under $PIPER_HOME), with
    the repository's own objects as an alternate: a snapshot holds files hidden from the
    sandbox (their current content), and the sandbox reads the project's .git since git is
    open there (2026-10-01) - not one byte of them may land in .git/objects. None: the
    repository's objects (trees recorded before that)."""
    if not objects:
        return {}
    os.makedirs(objects, mode=0o700, exist_ok=True)
    common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", session=True).strip()
    return {"GIT_OBJECT_DIRECTORY": objects, "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.path.join(common, "objects")}


def is_tree(top: str, h: str, objects: str | None = None) -> bool:
    if not _TREE.match(h or ""):
        return False
    try:
        return _git(top, "cat-file", "-t", h, env=snap_env(top, objects)).strip() == "tree"
    except GitError:
        return False


def snapshot(top: str, index_path: str, objects: str | None = None, *, strict: bool = False) -> str:
    """Tree id of the working tree NOW, without touching the user's index, HEAD or refs:
    a copy of the index (its stat cache keeps it fast), `add -A` into the copy, write-tree.
    New objects land in `objects` (snap_env), never in the repository's .git/objects.
    No global/system config, no fsmonitor, and `add` runs no hooks. A sandbox-written
    .gitattributes names drivers of the repository's own config - which the sandbox can
    write as well since git is open there (2026-10-01, see the module docstring): such a
    config refuses the snapshot instead (refuse_exec_config)."""
    refuse_exec_config(top)
    gitdir = _git(top, "rev-parse", "--absolute-git-dir", session=True).strip()
    src = os.path.join(gitdir, "index")
    try:
        os.unlink(index_path)
    except FileNotFoundError:
        pass
    if os.path.isfile(src) and not os.path.islink(src):
        with open(src, "rb") as f, open(index_path, "wb") as g:
            g.write(f.read())
            stamp = os.fstat(f.fileno())
        if strict:
            # Git's racy-index check needs the original timestamp; copying to "now"
            # otherwise hides equal-size edits on filesystems with second precision.
            os.utime(index_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    env = dict(snap_env(top, objects), GIT_INDEX_FILE=index_path)
    # --ignore-errors: one unreadable file must not lose the whole snapshot (rc 1 then)
    _git(top, "-c", "core.hooksPath=/dev/null", "add", "-A", "--ignore-errors", "--", ".", session=True, env=env,
         timeout=120, ok=(0, 1))
    return _git(top, "write-tree", session=True, env=env).strip()


def tree_files(top: str, a: str, b: str, sensitive, objects: str | None = None) -> list[dict]:
    out = _git(top, "diff-tree", "-r", "-M", "--name-status", "-z", "--no-commit-id", a, b, env=snap_env(top, objects))
    parts, rows, i = out.split("\0"), [], 0
    while i < len(parts):
        st = parts[i]
        i += 1
        if not st:
            continue
        row = {"status": st[0]}
        if st[0] in "RC" and i + 1 < len(parts):
            row["orig"], row["path"] = parts[i], parts[i + 1]
            i += 2
        elif i < len(parts):
            row["path"] = parts[i]
            i += 1
        else:
            break
        row["sensitive"] = bool(sensitive(row["path"]) or ("orig" in row and sensitive(row["orig"])))
        rows.append(row)
    return rows


def tree_diff(top: str, a: str, b: str, hidden, rel: str | None = None, objects: str | None = None,
              label: str = "a path hidden from Claude") -> str:
    if rel and hidden(rel):
        raise GitError("%s is withheld: %s" % (rel, label))
    env = snap_env(top, objects)
    names = _git(top, "diff-tree", "-r", "--no-renames", "--name-only", "-z", a, b,
                 *(["--", literal(rel)] if rel else []), env=env).split("\0")
    spec, n = _excludes(names, hidden)   # a directory `rel` may contain hidden files
    if rel:
        spec = [literal(rel)] + spec[1:]
    out = _git(top, *VIEW, "diff", *PATCH, a, b, *(["--", *spec] if spec else []), env=env)
    note = "[%d file(s) withheld: %s]\n" % (n, label) if n else ""
    return _bounded(note + strict_filter(out, hidden, label))


# ------------------------------------------------------------------ guard of a sandbox-written .git
# git is open in the sandbox (2026-10-01): what it writes into .git that host git would RUN -
# a config key naming a program (fsmonitor, filter/diff drivers, sshCommand, pager, `!` alias,
# include ...), a hook, a planted commondir - is checked against a baseline (Manager._git_guard)
# and reverted. Config keys that run nothing (branches, remotes, user, gc ...) are free.
GUARD_SAFE_RX = re.compile(
    r"^(core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode|symlinks|"
    r"autocrlf|eol|safecrlf|checkstat|trustctime|abbrev|compression|loosecompression|bigfilethreshold|"
    r"sparsecheckout|sparsecheckoutcone|splitindex|untrackedcache|commitgraph|multipackindex|preloadindex|"
    r"quotepath|longpaths|whitespace|warnambiguousrefs|excludesfile|attributesfile|commentchar|commentstring)"
    r"|remote\.[^\n]+\.(url|pushurl|fetch|push|tagopt|prune|prunetags|mirror|skipdefaultupdate|skipfetchall|"
    r"promisor|partialclonefilter)"
    r"|branch\.[^\n]+\.(remote|pushremote|merge|rebase|description|mergeoptions)"
    r"|user\.(name|email)|extensions\.[a-z]+|gc\.[a-z.]+|maintenance\.(auto|strategy)|index\.[a-z]+|feature\.[a-z]+"
    r"|pull\.(rebase|ff)|push\.(default|autosetupremote|followtags)|fetch\.(prune|prunetags|writecommitgraph)"
    r"|merge\.(ff|conflictstyle|log|renamelimit)|rebase\.(autosquash|autostash|updaterefs|stat)"
    r"|init\.defaultbranch|lfs\.(url|repositoryformatversion)|status\.[a-z]+|log\.[a-z]+|advice\.[a-z]+"
    r"|color\.[a-z.]+|i18n\.[a-z]+|commit\.(verbose|cleanup)"
    r"|submodule\.[^\n]+\.(url|active|branch|shallow|ignore|fetchrecursesubmodules))$")
# without a baseline (a writer already ran when piperd first looked): these at least are never kept
GUARD_DANGER_RX = re.compile(
    r"^(core\.(fsmonitor|hookspath|sshcommand|pager|editor|askpass|gitproxy)|sequence\.editor|include\.|includeif\."
    r"|filter\.|diff\.[^\n]+\.(command|textconv)|merge\.[^\n]+\.driver|alias\.|credential\.|gpg\."
    r"|uploadpack\.|receivepack\.|remote\.[^\n]+\.(uploadpack|receivepack|vcs)|pager\.|interactive\.difffilter"
    r"|tar\.[^\n]+\.command|web\.browser|man\.|difftool\.|mergetool\.|submodule\.[^\n]+\.update|core\.worktree)")
GUARD_ENV = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def _config_files(common: str, gitdir: str) -> list[str]:
    return [os.path.join(common, "config"), os.path.join(common, "config.worktree")] + \
        ([os.path.join(gitdir, "config.worktree")] if gitdir != common else [])


def _config_entries(path: str) -> list[list[str]]:
    """[[key, value], ...] of ONE config file - --file reads no include, runs nothing."""
    if not os.path.isfile(path) or os.path.islink(path):
        return [["(file)", "a symlink or not a file"]] if os.path.lexists(path) else []
    try:
        out = _git("/", "config", "--file", path, "--no-includes", "--list", "-z", env=GUARD_ENV)
    except GitError as e:
        return [["(unparsable)", str(e)[:200]]]
    return [list(e.split("\n", 1)) if "\n" in e else [e, ""] for e in out.split("\0") if e]


def _hooks(common: str) -> dict:
    d = os.path.join(common, "hooks")
    out: dict = {}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        if n.endswith(".sample"):
            continue   # git never runs those
        p = os.path.join(d, n)
        if os.path.islink(p):
            out[n] = "link:" + os.readlink(p)
            continue
        try:
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            out[n] = "unreadable"
            continue
        try:
            st = os.fstat(fd)
            out[n] = "sha256:" + hashlib.sha256(os.read(fd, 1 << 22)).hexdigest() if stat.S_ISREG(st.st_mode) \
                else "type:%o" % stat.S_IFMT(st.st_mode)
        finally:
            os.close(fd)
    return out


def guard_state(common: str, gitdir: str) -> dict:
    return {"config": {f: _config_entries(f) for f in _config_files(common, gitdir)},
            "hooks": _hooks(common), "commondir": os.path.lexists(os.path.join(common, "commondir"))}


def guard_signature(common: str, gitdir: str) -> tuple:
    """Cheap change detection (stat only) for the watcher and the hot paths."""
    def st(p):
        try:
            s = os.lstat(p)
            return s.st_mtime_ns, s.st_size, s.st_ino
        except OSError:
            return None
    hooks = os.path.join(common, "hooks")
    try:
        names = tuple(sorted((n, st(os.path.join(hooks, n))) for n in os.listdir(hooks)))
    except OSError:
        names = ()
    return tuple(st(f) for f in _config_files(common, gitdir)) + (st(hooks), names,
                                                                  st(os.path.join(common, "commondir")))


def guard_trusted(state: dict) -> dict:
    """A baseline taken while a sandbox may already have written: no program-naming key kept."""
    return dict(state, config={f: [e for e in es if not GUARD_DANGER_RX.match(e[0])]
                               for f, es in state["config"].items()})


def guard_violations(state: dict, base: dict) -> list[dict]:
    out = []
    for f, es in state["config"].items():
        known = base.get("config", {}).get(f) or []
        for k, v in es:
            if not GUARD_SAFE_RX.match(k) and [k, v] not in known:
                out.append({"kind": "config", "file": f, "key": k, "value": v})
    for n, h in state["hooks"].items():
        if base.get("hooks", {}).get(n) != h:
            out.append({"kind": "hook", "name": n})
    if state["commondir"] and not base.get("commondir"):
        out.append({"kind": "commondir"})
    return out


def guard_revert(common: str, violations: list[dict], quarantine: str) -> list[str]:
    """Undo `violations`: config entries unset (the file kept in `quarantine` first), hooks and
    a planted commondir moved there. Returns what could NOT be undone."""
    failed = []
    os.makedirs(quarantine, mode=0o700, exist_ok=True)
    saved = set()
    for v in violations:
        try:
            if v["kind"] == "config":
                f = v["file"]
                if f not in saved and os.path.isfile(f):
                    shutil.copy2(f, os.path.join(quarantine, os.path.basename(f)))
                    saved.add(f)
                if v["key"].startswith("("):   # a symlink / unparsable file: away with it
                    os.replace(f, os.path.join(quarantine, os.path.basename(f) + ".bad"))
                    continue
                _git("/", "config", "--file", f, "--fixed-value", "--unset-all", v["key"], v["value"],
                     env=GUARD_ENV, ok=(0, 5))
            elif v["kind"] == "hook":
                src = os.path.join(common, "hooks", v["name"])
                if os.path.lexists(src):
                    os.replace(src, os.path.join(quarantine, "hook-" + v["name"]))
            elif v["kind"] == "commondir":
                os.replace(os.path.join(common, "commondir"), os.path.join(quarantine, "commondir"))
        except (OSError, GitError) as e:
            failed.append("%s %s: %s" % (v["kind"], v.get("key") or v.get("name") or "", e))
    return failed


# ------------------------------------------------------------------ merge requests (9.7 #7)
def current_branch(top: str) -> str:
    return _git(top, "rev-parse", "--abbrev-ref", "HEAD").strip()


def upstream(top: str) -> str:
    try:
        return _git(top, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").strip()
    except GitError:
        return ""


def ref_exists(top: str, ref: str) -> bool:
    try:
        _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}")
        return True
    except GitError:
        return False


def check_branch(top: str, name: str) -> str:
    if not name or name.startswith("-") or len(name) > 250 or not re.match(r"^[A-Za-z0-9._/-]+$", name):
        raise GitError("invalid branch name %r" % name)
    _git(top, "check-ref-format", "--branch", name)
    return name


def subjects(top: str, base: str, n: int = 100) -> list[str]:
    """Subjects of base..HEAD, oldest first."""
    out = _git(top, *VIEW, "log", "--no-color", "--reverse", "--format=%s", "-n", str(n),
               "--end-of-options", "%s..HEAD" % base)
    return [l for l in out.splitlines() if l.strip()]


# ------------------------------------------------------------------ branches, worktrees, log (9.25)
# The GUI's own git (the human, K12): no hook ever runs - a post-checkout hook in a sandbox-written
# .git would run on the host - and nothing is forced: git's refusal (a switch that would lose changes,
# an unmerged branch) stands unless the GUI confirmed a named force.
NOHOOKS = ["-c", "core.hooksPath=/dev/null"]
REF_NAME_RX = re.compile(r"^[A-Za-z0-9._/-]{1,250}$")
GRAPH_FMT = "%x1f%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1f%D%x1f%P"
COMMIT_FILES_MAX = 2000
BLOB_MAX = 2 << 20


def check_ref(top: str, ref: str) -> str:
    """A branch / remote-branch / tag name the log may start from (never an option, never a range)."""
    if not REF_NAME_RX.match(ref or "") or ref.startswith("-") or ".." in ref or not ref_exists(top, ref):
        raise GitError("unknown branch %r" % ref)
    return ref


def _track(s: str) -> tuple[int, int, bool]:
    ahead = re.search(r"ahead (\d+)", s)
    behind = re.search(r"behind (\d+)", s)
    return int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0, s.strip() == "gone"


def worktrees(top: str) -> list[dict]:
    """`git worktree list --porcelain`: [{path, head, branch ("" detached), main, locked, prunable}]."""
    out, cur = [], None
    for line in _git(top, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if cur:
                out.append(cur)
            cur = None
            continue
        k, _, v = line.partition(" ")
        if k == "worktree":
            cur = {"path": os.path.realpath(v), "head": "", "branch": "", "main": not out and cur is None,
                   "locked": False, "prunable": False}
        elif cur is None:
            continue
        elif k == "HEAD":
            cur["head"] = v[:12]
        elif k == "branch":
            cur["branch"] = v[len("refs/heads/"):] if v.startswith("refs/heads/") else v
        elif k in ("locked", "prunable"):
            cur[k] = True
        elif k == "bare":
            cur["bare"] = True
    return out


def branches(top: str) -> dict:
    """{current, detached, local: [{name, upstream, ahead, behind, gone, head, subject, date, worktree}],
    remote: [{name, head, subject, date}], worktrees}."""
    fmt = "%(refname)%1f%(upstream:short)%1f%(upstream:track,nobracket)%1f%(objectname:short)%1f%(subject)%1f" \
          "%(committerdate:iso-strict)%1f%(worktreepath)%1f%(symref)"
    try:
        current = _git(top, "symbolic-ref", "--quiet", "--short", "HEAD").strip()
    except GitError:
        current = ""
    local, remote = [], []
    for line in _git(top, "for-each-ref", "--format=" + fmt, "refs/heads", "refs/remotes").splitlines():
        f = line.split("\x1f")
        if len(f) < 8 or f[7]:   # a symbolic ref (origin/HEAD) is no branch of its own
            continue
        ref = f[0]
        if ref.startswith("refs/heads/"):
            ahead, behind, gone = _track(f[2])
            local.append({"name": ref[11:], "upstream": f[1], "ahead": ahead, "behind": behind, "gone": gone,
                          "head": f[3], "subject": f[4], "date": f[5],
                          "worktree": os.path.realpath(f[6]) if f[6] else ""})
        elif ref.startswith("refs/remotes/"):
            remote.append({"name": ref[13:], "head": f[3], "subject": f[4], "date": f[5]})
    return {"current": current, "detached": not current and has_head(top), "local": local, "remote": remote,
            "worktrees": worktrees(top)}


def _has_changes(top: str) -> bool:
    return bool(_git(top, "status", "--porcelain=v1", "-z", "--untracked-files=normal").strip("\0"))


def switch(top: str, name: str) -> dict:
    """Switch the work tree to a local branch, or create the tracking branch of a remote one
    (`origin/x` -> local `x`). git refuses what would lose changes; nothing is forced."""
    refuse_exec_config(top)
    locals_ = {b["name"] for b in branches(top)["local"]}
    if name in locals_:
        _git(top, *NOHOOKS, "switch", "--no-guess", "--end-of-options", check_branch(top, name), timeout=120)
    else:
        check_ref(top, name)
        if not ref_exists(top, "refs/remotes/" + name):
            raise GitError("no branch %r" % name)
        local = name.split("/", 1)[1] if "/" in name else name
        if local in locals_:
            raise GitError("a local branch %r exists - switch to it" % local)
        _git(top, *NOHOOKS, "switch", "--track", "-c", check_branch(top, local), "--end-of-options", name, timeout=120)
    return {"branch": current_branch(top)}


def create_branch(top: str, name: str, start: str = "", checkout: bool = False) -> dict:
    refuse_exec_config(top)
    check_branch(top, name)
    if ref_exists(top, "refs/heads/" + name):
        raise GitError("branch %r already exists" % name)
    base = check_ref(top, start) if start else "HEAD"
    if checkout:
        _git(top, *NOHOOKS, "switch", "--no-track", "-c", name, "--end-of-options", base, timeout=120)
    else:
        _git(top, "branch", "--no-track", "--end-of-options", name, base)
    return {"branch": name, "current": current_branch(top)}


def unmerged(top: str, name: str, n: int = 20) -> list[str]:
    """Subjects of the branch's commits on neither HEAD nor its upstream - what `branch -D` would drop."""
    up = ""
    try:
        up = _git(top, "rev-parse", "--abbrev-ref", "--end-of-options", name + "@{upstream}").strip()
    except GitError:
        pass
    out = _git(top, *VIEW, "log", "--no-color", "--format=%h %s", "-n", str(n), "refs/heads/" + name,
               "--not", "HEAD", *([up] if up else []), "--")
    return [l for l in out.splitlines() if l.strip()]


def delete_branch(top: str, name: str, force: bool = False) -> dict:
    """`git branch -d` (merged only); `force` = -D, which the GUI asks for after naming the commits it drops.
    Never the current branch or one checked out in another worktree (git refuses those as well)."""
    check_branch(top, name)
    info = branches(top)
    b = next((x for x in info["local"] if x["name"] == name), None)
    if b is None:
        raise GitError("no local branch %r" % name)
    if name == info["current"] or b["worktree"]:
        raise GitError("%r is checked out%s - switch it away first" % (
            name, "" if name == info["current"] else " in worktree " + b["worktree"]))
    lost = unmerged(top, name)
    if lost and not force:
        return {"deleted": False, "unmerged": lost}
    _git(top, "branch", "-D" if force else "-d", "--end-of-options", name)
    return {"deleted": True, "unmerged": lost}


def fetch(top: str) -> dict:
    refuse_exec_config(top)
    out = _git(top, *NOHOOKS, "fetch", "--all", "--prune", "--no-recurse-submodules", timeout=180)
    return {"output": out[-2000:]}


def pull(top: str) -> dict:
    """Fast-forward only: a merge or rebase is the human's terminal."""
    refuse_exec_config(top)
    if not upstream(top):
        raise GitError("the branch has no upstream - push it first")
    out = _git(top, *NOHOOKS, "pull", "--ff-only", "--no-rebase", "--no-recurse-submodules", timeout=180)
    return {"output": out[-2000:], "branch": current_branch(top)}


def worktree_add(top: str, path: str, branch: str, new: bool = False, start: str = "") -> dict:
    """A linked worktree at path: a new branch (from start or HEAD), or an existing local branch
    not checked out elsewhere (git refuses that one itself)."""
    refuse_exec_config(top)
    check_branch(top, branch)
    if os.path.lexists(path):
        raise GitError("%s already exists" % path)
    if new:
        if ref_exists(top, "refs/heads/" + branch):
            raise GitError("branch %r already exists" % branch)
        base = check_ref(top, start) if start else "HEAD"
        _git(top, *NOHOOKS, "worktree", "add", "--no-track", "-b", branch, "--", path, base, timeout=180)
    else:
        if not ref_exists(top, "refs/heads/" + branch):
            raise GitError("no local branch %r" % branch)
        _git(top, *NOHOOKS, "worktree", "add", "--", path, branch, timeout=180)
    return {"path": os.path.realpath(path), "branch": branch}


def worktree_remove(top: str, path: str) -> dict:
    """Never forced: git refuses a worktree with changes or untracked files."""
    if _has_changes(path):
        raise GitError("the worktree has changes - commit or discard them first")
    _git(top, "worktree", "remove", "--", path, timeout=120)
    return {"removed": path}


def ensure_excluded(top: str, pattern: str) -> bool:
    """Append pattern to the repository's info/exclude unless git already ignores it (the main
    checkout must not list a worktree as untracked). True when it was added."""
    try:
        _git(top, "check-ignore", "-q", "--no-index", "--", pattern.strip("/") + "/x")
        return False
    except GitError:   # rc 1: not ignored
        pass
    common_dir = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    p = os.path.join(common_dir, "info", "exclude")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write("\n# Piper: linked worktrees\n%s\n" % pattern)
    return True


def graph_rows(top: str, n: int = 300, ref: str = "") -> list[dict]:
    """`git log --graph` as rows: {graph} for a lane-only line, else {graph, hash, short, author, date,
    subject, refs, parents}. ref: one branch (check_ref) instead of --all."""
    args = ["log", "--graph", "--no-color", "--date-order", "--format=" + GRAPH_FMT, "-n", str(max(1, min(int(n), 2000)))]
    args += ["--end-of-options", check_ref(top, ref)] if ref else ["--all"]
    try:
        out = _git(top, *VIEW, *args, timeout=60)
    except GitError:
        if not has_head(top):
            return []
        raise
    rows = []
    for line in out.splitlines():
        g, sep, rest = line.partition("\x1f")
        if not sep:
            rows.append({"graph": line[:200]})
            continue
        f = rest.split("\x1f")
        if len(f) < 7:
            continue
        rows.append({"graph": g[:200], "hash": f[0], "short": f[1], "author": f[2], "date": f[3],
                     "subject": f[4][:500], "refs": f[5], "parents": f[6].split()})
    return rows


def _numstat(text: str) -> dict:
    """`diff-tree --numstat -z -M` -> {new path: (add, del)} (None, None for a binary file)."""
    out, parts, i = {}, text.split("\0"), 0
    while i < len(parts):
        head = parts[i]
        if not head.strip("\n"):
            i += 1
            continue
        f = head.strip("\n").split("\t")
        if len(f) < 3:
            i += 1
            continue
        add, dele = (None, None) if f[0] == "-" else (int(f[0]), int(f[1]))
        if f[2]:   # no rename: the path is in the same field
            out[f[2]] = (add, dele)
            i += 1
        else:      # rename: old\0new follow
            if i + 2 < len(parts):
                out[parts[i + 2]] = (add, dele)
            i += 3
    return out


def line_counts(top: str) -> dict:
    """S2 (the Commit panel's +N -N): {path: (add, del)} of the working tree against HEAD, staged or not
    ((None, None) for a binary file; untracked files have none). A fresh repository counts the index;
    a failing git = no counts, the panel shows none."""
    for rng in (["HEAD"], ["--cached"]):
        try:
            return _numstat(_git(top, *VIEW, "diff", *rng, "--numstat", "-z", "-M", "--no-ext-diff", "--no-textconv", timeout=30))
        except GitError:
            continue
    return {}


def commit_detail(top: str, commit: str, withheld) -> dict:
    """One commit for the Git window: {hash, short, parents, author, email, date, committer, committed,
    refs, message, files: [{path, old_path?, status, add, del, binary, withheld}], truncated}.
    Merges: against the first parent (as `show --diff-merges=first-parent`). withheld(path): `.git`."""
    full = resolve_commit(top, commit)
    f = _git(top, *VIEW, "show", "-s", "--no-color",
             "--format=%H%x1f%h%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%cI%x1f%D%x1f%B", "--end-of-options", full).split("\x1f", 9)
    parents = f[2].split()
    files = _tree_files(top, [parents[0], full] if parents else ["--root", full], withheld)
    return {"hash": f[0], "short": f[1], "parents": parents, "author": f[3], "email": f[4], "date": f[5],
            "committer": f[6], "committed": f[7], "refs": f[8], "message": f[9].rstrip("\n")[:20000],
            "files": files[:COMMIT_FILES_MAX], "truncated": len(files) > COMMIT_FILES_MAX}


def _tree_files(top: str, rng: list[str], withheld) -> list[dict]:
    """diff-tree of `rng` (two commits, or --root and one) -> [{path, old_path?, status, add, del, binary, withheld}]."""
    common_args = ["diff-tree", "-r", "-z", "-M", "--no-ext-diff", "--no-textconv"]
    names = _git(top, *VIEW, *common_args, "--name-status", *rng, timeout=60).split("\0")
    stat = _numstat(_git(top, *VIEW, *common_args, "--numstat", *rng, timeout=60))
    files, i = [], 0
    if names and re.fullmatch(r"[0-9a-f]{40,64}\n?", names[0] or ""):
        i = 1   # --root prints the commit id first
    while i < len(names):
        st = names[i].strip("\n")
        if not st:
            i += 1
            continue
        code = st[0]
        if code in "RC" and i + 2 < len(names):
            old, new, i = names[i + 1], names[i + 2], i + 3
        else:
            old, new, i = None, names[i + 1] if i + 1 < len(names) else "", i + 2
        add, dele = stat.get(new, (None, None))
        row = {"path": new, "status": code, "add": add, "del": dele, "binary": add is None and new in stat,
               "withheld": bool(withheld(new) or (old and withheld(old)))}
        if old:
            row["old_path"] = old
        files.append(row)
    return files


def commit_blob(top: str, commit: str, path: str, side: str, withheld) -> dict:
    """A file of a commit (side "new") or of its first parent ("old"): {text, exists}. ≤ 2 MiB,
    binary refused, `.git` withheld."""
    if side not in ("old", "new") or not path or "\0" in path or withheld(path):
        raise GitError("invalid path %r" % path)
    full = resolve_commit(top, commit)
    if side == "old":
        parents = _git(top, "show", "-s", "--format=%P", "--end-of-options", full).split()
        if not parents:
            return {"text": "", "exists": False}
        full = parents[0]
    row = _git(top, "ls-tree", "-z", "--full-tree", "--end-of-options", full, "--", literal(path)).strip("\0")
    if not row:
        return {"text": "", "exists": False}
    meta, _, _ = row.partition("\t")
    mode, kind, oid = meta.split(" ")
    if kind != "blob":
        raise GitError("%s is not a file" % path)
    if mode == "120000":
        return {"text": read_blob(top, oid, limit=4096).decode("utf-8", "replace"), "exists": True, "symlink": True}
    if int(_git(top, "cat-file", "-s", oid).strip() or 0) > BLOB_MAX:
        raise GitError("%s is larger than 2 MiB" % path)
    raw = read_blob(top, oid, limit=BLOB_MAX)
    if b"\0" in raw[:8192]:
        raise GitError("%s looks binary" % path)
    return {"text": raw.decode("utf-8", "replace"), "exists": True}


# ------------------------------------------------------------------ unlocked full git (git_run)
# The user unlocked "Full git" for one session (GUI) and confirms EVERY call on a card: no
# command filter - the card with the exact argv is the boundary. What still holds (isolation
# invariants, not a filter): git runs in the trusted root with GIT_DIR/GIT_WORK_TREE pinned,
# hooks never run (a hook path may lie in the agent-writable tree, the card would not show
# what runs), no editor / terminal, and diff blocks of hidden files are dropped from the output.
# The human's global config applies (identity, credential helpers), like the GUI's own push.
RUN_ENV = dict(ENV, GIT_EDITOR="true", GIT_SEQUENCE_EDITOR="true", GIT_MERGE_AUTOEDIT="no")
RUN_TIMEOUT = 240
RUN_MAX_ARGS = 200

_W_CODE = ("can run a program on the host or permanently change the repository config "
           "(which then applies to piperd's git and your own too)")
_W_OUTSIDE = "an argument points outside the repository"
_W_DISCARD = "discards changes in the working tree - including hidden files the agent cannot see"


def check_run_args(args) -> list[str]:
    """The argv shape only (no filter): a non-empty list of strings; a leading "git" is dropped."""
    if isinstance(args, list) and args[:1] == ["git"]:
        args = args[1:]
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        raise GitError('args must be a non-empty list of strings, e.g. ["merge", "origin/master"]')
    if len(args) > RUN_MAX_ARGS or any("\0" in a or len(a) > 10000 for a in args):
        raise GitError("too many or too long arguments")
    return list(args)


def _sub(args: list[str]) -> tuple[str, list[str]]:
    """(subcommand, the rest) past global options like -c x=y / -C dir / --no-pager."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env") else 1
    return (args[i], args[i + 1:]) if i < len(args) else ("", [])


def push_targets(root: str, args: list[str]) -> list[str]:
    """Remote branch names a `git push` argv would update ("*" = every branch: --all/--mirror) - from
    the refspecs, else the upstream of the current branch, else the current branch (push.default
    current/simple). [] for anything that is not a push."""
    sub, rest = _sub(args)
    if sub not in ("push", "send-pack"):
        return []
    glob = args[:len(args) - len(rest) - 1]
    if any(a in ("--all", "--mirror", "--branches") for a in rest) or any(
            _PUSH_CFG_RX.match(a) for a in glob) or "--config-env" in glob:
        return ["*"]   # also a push configuration given on the command line (-c remote.x.push=...)
    pos, skip, repo = [], False, False
    for a in rest:
        if skip:
            skip = False
        elif a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip, repo = True, repo or a == "--repo"
        elif a.startswith("--repo="):
            repo = True
        elif not a.startswith("-"):
            pos.append(a)
    if repo:   # --repo names the remote: every positional is a refspec
        pos = ["<repo>"] + pos
    if any(r.lstrip("+") == ":" for r in pos[1:]):
        return ["*"]   # ":" = every matching branch
    st = None

    def current() -> str:
        nonlocal st
        st = st or status(root)
        return st["branch"]
    out = []
    for r in pos[1:]:
        dst = r.lstrip("+").split(":", 1)[-1]
        if "*" in dst or any(x in dst for x in ("//", "..", "@{", "\\")) or dst.startswith("/"):
            return ["*"]   # a pattern refspec may name any branch; an odd name is not worth resolving
        if dst in ("HEAD", "@", ""):
            dst = current()
        if dst.startswith("refs/heads/"):
            out.append(dst[len("refs/heads/"):])
        elif not dst.startswith("refs/"):
            # git resolves a short destination against the remote's refs ("heads/master" -> refs/heads/master)
            out.append(dst)
            if dst.startswith("heads/"):
                out.append(dst[len("heads/"):])
    if not pos[1:]:
        # configured push refspecs or push.default=matching decide more than the upstream: assume every branch
        try:
            cfg = _git(root, "config", "--get-regexp", r"^(remote\..*\.push|push\.default|branch\..*\.pushremote|remote\.pushdefault)$",
                       ok=(0, 1), session=True)
        except GitError:
            cfg = "x"
        if re.search(r"\.push |push\.default (matching|nothing)|pushremote |pushdefault ", cfg):
            return ["*"]
        st = st or status(root)
        up = st["upstream"]
        # push.default current pushes to the same name, upstream/simple to the upstream: check both
        out.append(current())
        if "/" in up:
            out.append(up.split("/", 1)[1])
    return out


_PUSH_CFG_RX = re.compile(r"(?i)^(remote\..*\.push|push\.default|branch\..*\.push(remote)?|remote\.pushdefault|"
                          r"url\..*\.pushinsteadof)=")


def refuse_global_config(args: list[str]) -> None:
    """git_fail_closed: no -c / --config-env / --exec-path in a git_run argv - a later -c beats the ones
    host git gets (core.hooksPath, protocol.file.allow), whatever the card showed."""
    if not FAIL_CLOSED:
        return
    sub, rest = _sub(args)
    glob = args[:len(args) - len(rest) - (1 if sub else 0)]
    bad = [a for a in glob if a in ("-c", "--config-env") or a.startswith(("--config-env=", "--exec-path"))]
    if bad:
        raise GitError("host git refused: global option %s (git_fail_closed) - set configuration in the "
                       "repository with `git config` instead" % bad[0])


def protected_hit(targets: list[str], protected: list[str]) -> str:
    """The first protected branch among the push targets ("" none)."""
    if not protected:
        return ""
    return next((t for t in targets if t == "*" or t in protected), "")


def _named_through(top: str, rest: list[str], hidden) -> list[str]:
    """N6: arguments that reach a hidden file another way than by its plain name: through a symlink in the work
    tree (`commit -F lnk`, `lnkdir/properties/x`), or as an option's value (`--exclude-from=<hidden>`,
    `-S<hidden>`) - git reads the REAL file on the host. Free text options (a message, a format) are no path."""
    root = os.path.realpath(top)
    out, text_next = [], False
    for a in rest:
        if text_next:
            text_next = False
            continue
        cands, lexical = [a], False
        if a.startswith("--"):
            name, eq, v = a.partition("=")
            if name in _AUTO_TEXT_OPT:
                text_next = not eq
                continue
            cands, lexical = ([v] if eq else []), True
        elif a.startswith("-") and len(a) > 1:
            if a in _AUTO_TEXT_OPT or a[1] == "m":
                text_next = a in _AUTO_TEXT_OPT or a == "-m"
                continue
            cands, lexical = [a[2:]], True
        for c in cands:
            if not c or c.startswith(("/", "~")) or ".." in c.split("/"):
                continue
            rel = os.path.normpath(c.rsplit(":", 1)[-1] if ":" in c and not c.startswith(":(") else c)
            if lexical and rel != "." and hidden(rel):
                out.append(c)
                continue
            full = os.path.join(root, rel)
            real = os.path.realpath(full)
            if real != full and (real == root or real.startswith(root + os.sep)) and \
                    real != root and hidden(os.path.relpath(real, root)):
                out.append(c)
    return out


def run_warnings(args: list[str], hidden, output_ok=None, top: str | None = None) -> list[str]:
    """What the card warns about - heuristics for the human, never a refusal.
    output_ok: as in auto_blockers - an `--output=<path>` inside the repository it accepts is no warning.
    top: the repository - an argument reaching a hidden file through a symlink or as an option value is named too."""
    sub, rest = _sub(args)
    if sub == "grep":
        rest = grep_args(rest)[0]
    flags = [a for a in rest if a.startswith("-")]
    w: list[str] = []
    glob = args[:len(args) - len(rest) - 1] if sub else args
    if sub in ("config", "submodule", "filter-branch", "difftool", "mergetool", "bisect") or \
            any(a == "-c" or a.startswith(("--config-env", "--exec-path")) for a in glob) or \
            any(re.match(r"^(--exec|--upload-pack|--receive-pack|--extcmd|--tool|-x$|--run)", a) for a in flags
                if not (sub == "cherry-pick" and a == "-x")) or \
            (sub == "rebase" and any(re.match(r"^-[a-wyzA-Z]*x", a) for a in flags)) or \
            any(a.startswith("!") for a in rest):
        w.append(_W_CODE)
    if sub == "push":
        w.append("sends commits to the server" + (" and REWRITES history on the server" if any(
            a in ("-f", "--force", "--mirror", "--delete", "-d") or a.startswith(("--force", "+")) for a in rest) else ""))
    if (sub == "reset" and "--hard" in flags) or sub == "clean" or \
            (sub in ("checkout", "restore") and ("--" in rest or "." in rest or "-f" in flags or "--force" in flags)) or \
            (sub == "stash" and rest[:1] and rest[0] in ("drop", "clear")) or \
            (sub == "branch" and any(a in ("-D", "--delete", "-d") for a in flags)) or \
            (sub == "switch" and any(a in ("-f", "--force", "--discard-changes") for a in flags)):
        w.append(_W_DISCARD)
    named = []
    for a in rest:
        for cand in (a, a.rsplit(":", 1)[-1] if ":" in a and not a.startswith(":(") else ""):
            p = os.path.normpath(cand) if cand and not cand.startswith("-") else ""
            if p and p != "." and not p.startswith(("/", "..")) and hidden(p):
                named.append(cand)
    if top:
        named += _named_through(top, rest, hidden)
    if named:
        w.append("names a hidden file (%s) - the agent gets whatever git prints" % ", ".join(sorted(set(named))[:3]))
    if any(a.startswith(("/", "~")) or ".." in a.replace("\\", "/").split("/") for a in rest if not a.startswith("-")) or \
            any(a in ("-C", "--git-dir", "--work-tree", "--namespace") or a.startswith(("--git-dir=", "--work-tree=", "--namespace="))
                for a in glob) or \
            any(a == "--no-index" or (a.startswith("--output") and not _output_inside(a, output_ok)) for a in flags):
        w.append(_W_OUTSIDE)
    return w


def _output_inside(flag: str, output_ok) -> bool:
    v = flag.partition("=")[2]
    return bool(output_ok and flag.startswith("--output=") and v and not v.startswith(("/", "~"))
                and ".." not in v.replace("\\", "/").split("/") and output_ok(v))


def risky_warnings(warns: list[str]) -> list[str]:
    """The warnings a git_mode "hole" run must still show on a card: running code, reading
    outside the repository or hidden files, rewriting the server's history. A plain push and
    discarding work-tree changes are what git in the sandbox may do anyway."""
    return [w for w in warns if w != _W_DISCARD and not (w.startswith("sends commits") and "REWRITES" not in w)]


# git_mode "hole": what git_run may run on the HOST without a card. An allowlist - the warnings
# above are heuristics, and review 2026-10-05 got past them (a symlink to / in the work tree,
# fetch file://, archive --remote). Everything else still runs, after a card.
AUTO_SUBS = frozenset(("status", "log", "diff", "show", "add", "rm", "mv", "commit", "push", "pull", "fetch",
                       "merge", "rebase", "cherry-pick", "revert", "checkout", "switch", "reset", "restore",
                       "branch", "stash", "tag", "apply"))
_NET_SUBS = ("push", "pull", "fetch")
# policy git_run_readonly_extra: read-only subcommands that run without a card too (a hidden path named in an
# argument still asks - run_warnings; ls-remote only to a configured remote, like fetch)
READONLY_SUBS = frozenset(("merge-base", "ls-remote", "rev-parse", "rev-list", "describe", "show-ref", "for-each-ref",
                           "ls-files", "ls-tree", "cat-file", "blame", "shortlog", "name-rev",
                           "check-attr", "check-ignore", "count-objects", "archive", "worktree", "symbolic-ref",
                           "grep", "diff-tree", "diff-index", "diff-files", "show-branch", "whatchanged", "cherry",
                           "range-diff", "check-ref-format", "version", "reflog", "remote"))
# a host file / program / another repository by option; a value after them is checked anyway
_AUTO_BAD_OPT = re.compile(r"^(--(file|template|pathspec-from-file|recurse-submodules(?!=no$)|upload-pack|receive-pack|"
                           r"exec|repo|remote|output|no-index|git-dir|work-tree|ext-diff|reference|separate-git-dir|"
                           r"server-option|config|"
                           # a working-tree file read as input (blame --contents / -S's long form): a hidden one leaks
                           r"contents|ignore-revs-file|"
                           # output strict_filter cannot parse (its block headers start a line, uncoloured)
                           r"color(?!=never$)|line-prefix|output-indicator-[a-z]+|"
                           # N4: a header coloured whatever color.ui says, or relative to a directory (its paths are
                           # no longer the repository's); graph lines before a header strict_filter reads itself
                           r"color-words|word-diff=color|relative)(=|$))")
# the next argument is free text (a message, an author, a date), never a path
_AUTO_TEXT_OPT = ("-m", "--message", "--author", "--format", "--pretty", "--grep", "--since", "--until",
                  "--after", "--before", "--date")
_NET_URL = re.compile(r"^((https?|ssh|git)://|[\w.-]+@[\w.-]+:(?!/)|[\w.-]+:(?![/\\]))")
# config keys a repository may not carry for a card-free run: what GUARD_DANGER_RX names, plus
# redirections of a remote (url.*.insteadOf to file:// / ext::) and transport / http settings
_RUN_DANGER_RX = re.compile(r"^(\(|url\.|protocol\.|http\.|core\.(sshcommand|askpass|gitproxy|worktree|hookspath))")


def exec_config_keys(top: str) -> list[str]:
    """Program-naming keys of the repository's own config files (lower-case, as git reports them)."""
    return [k for k, _v in _exec_config_entries(top)]


def _exec_config_entries(top: str) -> list[tuple[str, str]]:
    try:
        gitdir = _git(top, "rev-parse", "--absolute-git-dir", session=True).strip()
        common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", session=True).strip()
    except GitError:
        return []
    return [(k, v) for f in _config_files(common, gitdir) for k, v in _config_entries(f)
            if GUARD_DANGER_RX.match(k) or _RUN_DANGER_RX.match(k) or _LFS_EXEC_RX.match(k)]


def config_warnings(top: str) -> list[str]:
    """What in the repository's own config (agent-writable in git_mode "hole") would make host
    git RUN something: a program-naming key, a remote redirection. Shown on every git_run card."""
    try:
        gitdir = _git(top, "rev-parse", "--absolute-git-dir", session=True).strip()
        common = _git(top, "rev-parse", "--path-format=absolute", "--git-common-dir", session=True).strip()
    except GitError:
        return []
    keys, urls = [], []
    # fail-closed host git runs no hooks and no fsmonitor: these two keys then run nothing
    neutral = ("core.hookspath", "core.fsmonitor") if FAIL_CLOSED else ()
    for f in _config_files(common, gitdir):
        for k, v in _config_entries(f):
            if k in neutral:
                continue
            if GUARD_DANGER_RX.match(k) or _RUN_DANGER_RX.match(k):
                keys.append(k)
            elif re.match(r"^remote\..+\.(url|pushurl)$", k) and not _NET_URL.match(v):
                urls.append("%s=%s" % (k, v[:80]))
    w = []
    if keys:
        w.append("the repository config names programs or redirections host git would use (%s)"
                 % ", ".join(sorted(set(keys))[:5]))
    if urls:
        w.append("a remote is not a network URL (%s) - another repository or a program on the host"
                 % ", ".join(urls[:3]))
    return w


def _auto_value_bad(top: str, v: str) -> bool:
    """A positional / option value that may name something outside the repository."""
    if v.startswith(("/", "~")) or "://" in v or "::" in v or ".." in v.replace("\\", "/").split("/"):
        return True
    root = os.path.realpath(top)
    full = os.path.realpath(os.path.join(top, v))   # follows a planted symlink (lnk -> /)
    return not (full == root or full.startswith(root + os.sep)) or \
        full == os.path.join(root, ".git") or full.startswith(os.path.join(root, ".git") + os.sep)


def _output_through_link(top: str, v: str) -> bool:
    """An output path with a symlink in it (or at it): host git would write where the link points."""
    lexical = os.path.normpath(os.path.join(os.path.realpath(top), v))
    return os.path.realpath(lexical) != lexical


def grep_args(rest: list[str], execution=None, positions=None) -> tuple[list[str], list[str], list[str], list[str]]:
    """Arguments requiring path checks, pre-- operands, pathspecs and unsafe options; patterns are text."""
    checked, operands, paths, why = [], [], [], []
    execution = [] if execution is None else execution
    positions = {} if positions is None else positions
    positions.update(operands=[], files=[], separator=None)
    prefix = rest[:rest.index("--")] if "--" in rest else rest
    pattern = any(a.startswith("-") and not a.startswith("--") and any(c in "ef" for c in a[1:]) for a in prefix)
    flags = {"cached", "untracked", "exclude-standard", "invert-match", "ignore-case", "word-regexp", "text",
             "recursive", "extended-regexp", "basic-regexp", "fixed-strings", "perl-regexp", "line-number",
             "column", "full-name", "files-with-matches", "name-only", "files-without-match", "null",
             "only-matching", "count", "show-function", "function-context", "quiet", "all-match", "break", "heading", "color"}
    numbers = {"max-depth", "threads", "before-context", "after-context", "context", "max-count"}
    i, after = 0, False
    while i < len(rest):
        a = rest[i]
        if after:
            paths.append(a); checked.append(a)
            execution.append(a)
        elif a == "--":
            after = True; checked.append(a)
            positions["separator"] = len(execution)
            execution.append(a)
        elif a in ("(", ")", "--and", "--or", "--not"):
            execution.append(a)
        elif a.startswith("--"):
            name, eq, value = a[2:].partition("=")
            if name in numbers:
                if not eq and i + 1 < len(rest):
                    i += 1; value = rest[i]
                if not value.isascii() or not value.isdigit():
                    why.append("invalid grep option value " + a[:60])
                execution.append("--" + name + "=" + value)
            elif name == "color" and eq and value in ("always", "never", "auto"):
                execution.append(a)
            elif eq or name.removeprefix("no-") not in flags and a not in (
                    "--index", "--no-textconv", "--no-ext-grep", "--no-recurse-submodules", "--no-open-files-in-pager"):
                why.append("unchecked grep option " + a[:60])
            else:
                checked.append(a)
                execution.append(a)
        elif a.startswith("-") and a != "-":
            short = a[1:]
            if short.isascii() and short.isdigit():
                execution.append(a)
                i += 1; continue
            while short:
                c, short = short[0], short[1:]
                if c in "efABCm":
                    value = short
                    if not value and i + 1 < len(rest):
                        i += 1; value = rest[i]
                    if not value and c != "e":
                        why.append("missing grep option value -" + c)
                    if c == "f":
                        checked.extend(["-f", value])
                        positions["files"].append(len(execution))
                    elif c != "e" and (not value.isascii() or not value.isdigit()):
                        why.append("invalid grep numeric option -" + c)
                    execution.extend(["-" + c, value])
                    break
                if c not in "viwaIrEGFPnhHlLzocpWq":
                    why.append("unchecked grep option -" + c)
                else:
                    checked.append("-" + c)
                    execution.append("-" + c)
        elif not pattern:
            pattern = True
            execution.extend(["-e", a])
        else:
            operands.append(a); checked.append(a)
            positions["operands"].append(len(execution))
            execution.append(a)
        i += 1
    if not pattern:   # N6: git would take the first pathspec - grep_scope's :(exclude) of a hidden file - for it
        why.append("grep needs a pattern (the first argument or -e)")
    return checked, operands, paths, why


def _is_tree(top: str, rev: str, glob: tuple = ()) -> bool:
    """Does `rev` name a tree object itself (`HEAD:config`, a subtree's id, `X^{tree}`) - not a commit?
    glob: the run's own global options (-C <dir> moves `HEAD:./x`)."""
    try:
        oid = _git(top, *glob, "rev-parse", "--verify", "--quiet", "--end-of-options", rev, session=True,
                   ok=(0, 1)).strip()
        return bool(oid) and _git(top, *glob, "cat-file", "-t", oid, session=True).strip() == "tree"
    except GitError:
        return False


# subcommands whose output carries diff blocks of revisions named in the argv
_TREE_SUBS = ("diff", "diff-tree", "diff-index", "log", "show", "whatchanged", "reflog", "range-diff", "stash")


def tree_revs(top: str, args: list[str]) -> bool:
    """N5: a revision argument (before `--`, either side of a range) that is a tree object: a diff of it names
    every path relative to that tree (`diff HEAD:config HEAD~1:config` -> a/properties/db.properties), which no
    hidden rule can judge - like --relative."""
    sub, rest = _sub(args)
    if sub not in _TREE_SUBS:
        return False
    glob = tuple(args[:len(args) - len(rest) - 1])
    rest = rest[:rest.index("--")] if "--" in rest else rest
    for a in rest:
        if a.startswith("-") or a.startswith((":(", ":/")):
            continue
        for part in re.split(r"\.\.\.?", a):
            if part and _is_tree(top, part, glob):
                return True
    return False


_GREP_GLOBAL = ("git grep runs without global options before it (no -C / -c): its hidden-file scope "
                "needs the plain form - narrow it with a path after the pattern instead")


def grep_refusal(top: str, args: list[str], hidden) -> str:
    """N7 (B-N6-4): why session_run refuses this grep whatever a card says ('' = it does not) - asked before the
    card, so no card is shown for a run that then fails anyway (rev:path, a tree object, no pattern...)."""
    if _sub(args)[0] != "grep":
        return ""
    if args[:1] != ["grep"]:
        return _GREP_GLOBAL
    try:
        grep_scope(top, args, hidden)
    except GitError as e:
        return str(e)
    return ""


def grep_scope(top: str, args: list[str], hidden) -> tuple[list[str], bytes | None]:
    execution, positions = [], {}
    checked, operands, paths, why = grep_args(args[1:], execution, positions)
    if why:
        raise GitError("cannot safely scope grep: " + "; ".join(why))
    pattern_input = None
    file_indexes = positions["files"]
    if len(file_indexes) > 1:
        raise GitError("use one visible grep pattern file or repeated -e patterns")
    for i in file_indexes:
        rel = execution[i + 1]
        if _auto_value_bad(top, rel) or hidden(rel):
            raise GitError("grep pattern file is outside the visible repository")
        try:
            if common.safe_stat(top, rel).st_size > MAX_DIFF:
                raise GitError("grep pattern file exceeds 1 MiB")
            pattern_input = common.safe_read(top, rel, MAX_DIFF + 1)
        except (OSError, ValueError) as e:
            raise GitError("cannot read visible grep pattern file: " + str(e)) from None
        if len(pattern_input) > MAX_DIFF:
            raise GitError("grep pattern file exceeds 1 MiB")
        execution[i + 1] = "-"   # Git reads the authorized snapshot, never a swapped path.
    refs, path_indexes = [], set()
    for i, value in zip(positions["operands"], operands):
        if ":" in value:   # rev:path names paths relative to that tree - the hidden excludes would miss them
            raise GitError("grep of a revision:path (%s) is refused - name the commit and narrow with a path after "
                           "--: git grep <pattern> %s -- %s" % (value[:80], value.partition(":")[0][:40] or "HEAD",
                                                               value.partition(":")[2][:80] or "."))
        if _auto_value_bad(top, value):
            raise GitError("grep revision/path leaves the authorized tree")
        if _is_tree(top, value):   # N5: a subtree's names are relative to it - the hidden excludes would miss
            raise GitError("grep searches a commit, not a tree object - name a commit and narrow with a path after --")
        try:
            tree = _git(top, "rev-parse", "--verify", value + "^{tree}", session=True).strip()
            refs.append(tree)
            execution[i] = tree   # Search the tree whose hidden paths were checked even if HEAD moves.
        except GitError:
            paths.append(value)
            path_indexes.add(i)
    if refs:
        names = set(p for ref in refs for p in _git(top, "ls-files", "--cached", "--with-tree=" + ref, "-z", "--", *paths,
                                                   session=True).split("\0") if p)
    else:
        opts = ["--cached"]
        if "--untracked" in checked:
            opts += ["--others"] + ([] if "--no-exclude-standard" in checked else ["--exclude-standard"])
        names = set(p for p in _git(top, "ls-files", "-z", *opts, "--", *paths, session=True).split("\0") if p)
    excluded = sorted(p for p in names if hidden(p) or _auto_value_bad(top, p))
    if len(excluded) > MAX_EXCLUDES:
        raise GitError("too many hidden grep paths; narrow the search to a visible directory")
    prefix = execution[:positions["separator"]] if positions["separator"] is not None else execution
    scoped = [a for i, a in enumerate(prefix) if i not in path_indexes]
    return ["grep", *scoped, "--", *paths, *[":(exclude,literal)" + p for p in excluded]], pattern_input


def auto_blockers(top: str, args: list[str], output_ok=None, extra_subs: frozenset = frozenset(),
                  worktree_args: tuple[str, ...] = ()) -> list[str]:
    """git_mode "hole": why `args` may NOT run on the host without a card ([] = it may).
    output_ok(path) -> bool (policy git_run_output_in_repo): an `--output=<path>` it accepts is no blocker.
    worktree_args is the exact command the Manager verified against pinned checkouts and scoped destinations."""
    sub, rest = _sub(args)
    if not args or args[0] != sub:
        return ["global options before the subcommand"]
    if sub not in AUTO_SUBS and sub not in extra_subs:
        return ["git %s is not on the list that runs without a card" % sub]
    if sub == "worktree" and worktree_args == tuple(args):
        return []
    why: list[str] = []
    if sub == "grep":
        rest, operands, _, grep_why = grep_args(rest)
        why += grep_why
        if any(":" in p for p in operands):
            why.append("grep tree paths must use a revision followed by -- pathspecs")
    if sub == "reflog" and rest[:1] and rest[0] in ("expire", "delete", "drop", "write"):
        why.append("reflog changes can discard recovery history")
    if sub == "remote" and [a for a in rest if not a.startswith("-")][:1] not in ([], ["get-url"]):
        why.append("remote changes or network commands need approval")
    if sub == "worktree" and rest[:1] != ["list"]:
        why.append("worktree changes need verified checkouts and scoped destinations")
    if sub == "symbolic-ref":
        refs = [a for a in rest if not a.startswith("-")]
        if len(refs) != 1 or any(a.startswith("-") and a not in ("--", "-q", "--quiet", "--short", "--no-recurse") for a in rest):
            why.append("only reading one symbolic reference runs without a card")
    if sub == "archive":
        # Custom archive formats can execute tar.<format>.command on the host.
        output = next((a.partition("=")[2] for a in rest if a.startswith("--output=")), "")
        fmt = next((a.partition("=")[2] for a in rest if a.startswith("--format=")), "")
        if any(a in ("--format", "-o", "--output") or a.startswith(("--add-file", "--add-virtual-file")) for a in rest):
            why.append("archive uses an unchecked format, input or output option")
        for option in rest:
            if option.startswith("-") and option != "--" and not (
                    option in ("-v", "-l", "--verbose", "--list", "--worktree-attributes") or
                    re.fullmatch(r"-[0-9]", option) or option.startswith(("--format=", "--output=", "--prefix=", "--mtime="))):
                why.append("unchecked archive option " + option[:80])
        if (fmt and fmt not in ("tar", "zip")) or (not fmt and output and not output.endswith((".tar", ".zip"))):
            why.append("archive format may run a host program; use --format=tar or --format=zip")
    positional: list[str] = []
    text_next = after_dashdash = False
    for a in rest:
        if text_next:
            text_next = False
            continue
        if not after_dashdash and a == "--":
            after_dashdash = True
            continue
        if not after_dashdash and a.startswith("--"):
            name, eq, val = a.partition("=")
            if name == "--output" and eq and output_ok is not None and sub == "archive" and not _auto_value_bad(top, val) \
                    and not _output_through_link(top, val) and output_ok(val):
                # a file inside the repository the sandbox could write itself - only an archive: a diff / log
                # written to a file never passes strict_filter (a hidden file's working-tree change would land there)
                continue
            if _AUTO_BAD_OPT.match(a):
                why.append("option %s" % name)
            elif name in _AUTO_TEXT_OPT:
                text_next = not eq
            elif eq and _auto_value_bad(top, val):
                why.append("option value %s" % a[:80])
            continue
        if not after_dashdash and a.startswith("-") and a != "-":
            if re.match(r"^-(n?\d+)$", a):
                continue
            if not re.match(r"^-[A-Za-z]+$", a) or (len(a) > 2 and ("F" in a or "t" in a)):
                why.append("option %s" % a[:40])   # an attached value (-F/etc/x) is not checked here
            text_next = a[-1] == "m"
            continue
        positional.append(a)
        if _auto_value_bad(top, a):
            why.append("argument %s" % a[:80])
    if (sub in _NET_SUBS or sub == "ls-remote") and positional and not why:
        try:
            remotes = _git(top, "remote", session=True).split()
        except GitError:
            remotes = []
        if positional[0] not in remotes:
            why.append("%s names %s, not a configured remote" % (sub, positional[0][:80]))
    return why


# commit options that take the next argument as their value (never a pathspec)
_COMMIT_VALUE_OPT = ("-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message", "--author",
                     "--date", "--fixup", "--squash", "-t", "--template", "--cleanup", "--trailer", "--pathspec-from-file")
_STASH_KEEP = ("list", "show", "drop", "pop", "apply", "branch", "clear", "store")


def _dirty(top: str, untracked: bool = False, ignored: bool = False) -> list[str]:
    """Paths whose work-tree content differs from the index (and untracked / ignored files when asked)."""
    out = _git(top, "status", "--porcelain=v1", "-z", "--no-renames",
               "--untracked-files=" + ("all" if untracked or ignored else "no"), *(["--ignored"] if ignored else []),
               session=True)
    paths = []
    for e in out.split("\0"):
        if len(e) > 3 and e[2] == " ":
            xy = e[:2]
            if xy == "??" or xy == "!!" or xy[1] != " ":
                paths.append(e[3:])
    return paths


def _config_true(top: str, key: str) -> bool:
    """N9: read as the run reads it - session_run's git sees the user's global / system config too (user.name for a
    commit); the repository config alone missed a global merge.autoStash / rebase.autoStash / pull.rebase."""
    try:
        return _git(top, "config", "--type=bool", "--get", key, ok=(0, 1)).strip() == "true"
    except GitError:
        return True   # an unreadable value: assume it is on (fail closed)


def _merge_config_flags(top: str) -> list[str]:
    """N9: the options a plain `git merge` takes from the config - branch.<current>.mergeOptions, and merge.ff when
    those name no fast-forward mode (K1: the sandbox writes .git/config). merge.ff=false or mergeOptions --no-ff /
    --squash turn a fast-forward into a real merge (save_state: `stash create` of the dirty work tree)."""
    branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1), session=True).strip()
    try:
        flags = shlex.split(_config(top, "branch.%s.mergeOptions" % branch)) if branch else []
    except ValueError:
        raise GitError("branch.%s.mergeOptions cannot be read" % branch) from None
    if not any(a in ("--ff", "--no-ff", "--ff-only") for a in flags):
        v = _config(top, "merge.ff").lower()
        if v == "only":
            flags.append("--ff-only")
        elif v:
            flags.append("--ff" if _config(top, "merge.ff", "bool") == "true" else "--no-ff")
    return flags


def _stashes_work_tree(top: str, sub: str, rest: list[str]) -> bool:
    """N6: does this run hash the dirty work tree into .git first? A real (not fast-forward) merge always does -
    builtin/merge.c save_state() runs `stash create`; so do --autostash (merge / rebase / pull, or their
    *.autoStash config) and checkout / switch --merge. A pull's merge target is unknown before it fetches - N8: a
    pull pull_plan models is checked after its fetch on the merge / rebase it runs (Manager._git_pull)."""
    flags = [a for a in rest if a.startswith("-")]
    cluster_m = any(re.fullmatch(r"-[A-Za-z]*m[A-Za-z]*", a) for a in flags)
    if sub in ("checkout", "switch"):
        return "--merge" in flags or cluster_m
    if sub not in ("merge", "pull", "rebase"):
        return False
    if sub == "merge" and not any(a in ("--abort", "--continue", "--quit") for a in flags):
        try:   # N9: the config's options first - the command line's own still count (fail closed when both)
            flags = _merge_config_flags(top) + flags
        except GitError:
            return True
    if any(a in ("--abort", "--continue", "--quit", "--skip") for a in flags):
        return False
    autostash = "--autostash" in flags or ("--no-autostash" not in flags and _config_true(
        top, "rebase.autoStash" if sub == "rebase" or (sub == "pull" and ("--rebase" in flags or any(
            a.startswith("--rebase=") and a != "--rebase=false" for a in flags) or _config_true(top, "pull.rebase")))
        else "merge.autoStash"))
    if autostash:
        return True
    if sub == "rebase":
        return False   # a dirty work tree stops it before anything is written
    mode = [a for a in flags if a in ("--ff", "--no-ff", "--ff-only")]   # the last one wins (config before argv)
    if mode[-1:] == ["--ff-only"] and "--squash" not in flags:
        return False
    if sub == "pull":
        return not ("--rebase" in flags or _config_true(top, "pull.rebase") or _config(top, "pull.ff").lower() == "only")
    targets, skip = [], False
    for a in rest:
        if skip:
            skip = False
        elif a in ("-m", "--message", "-F", "--file", "-s", "--strategy", "-X", "--strategy-option", "--into-name"):
            skip = True
        elif not a.startswith("-"):
            targets.append(a)
    if len(targets) != 1 or mode[-1:] == ["--no-ff"] or "--squash" in flags:
        return True
    for left, right in (("HEAD", targets[0]), (targets[0], "HEAD")):   # a fast-forward / up to date: no merge
        try:
            _git(top, "merge-base", "--is-ancestor", "--end-of-options", left, right, session=True)
            return False
        except GitError:
            continue
    return True


def stages_hidden(top: str, args: list[str], hidden) -> list[str]:
    """N5: hidden files whose REAL work-tree content `add` / `commit -a|-i|-o|<path>` / a creating `stash` would
    write into .git's objects (the sandbox reads .git; a push ships them). In plain hole the sandbox's own git
    takes the stand-in. Fail closed: a dry run that fails answers ["?"]."""
    sub, rest = _sub(args)
    try:
        if sub == "add":
            out = _git(top, "add", "--dry-run", *rest, session=True)
            names = []
            for ln in out.split("\n"):   # `add '<raw name>'`: a name with a newline breaks the line - fail closed
                m = re.fullmatch(r"(?:add|remove) '(.*)'", ln)
                if ln and not m:
                    return ["?"]
                if m:
                    names.append(m.group(1))
            return [n for n in names if hidden(n) or '"' in n or "\\" in n]   # a C-quoted name: fail closed
        if sub == "commit":
            every, skip, after, paths = False, False, False, []
            for a in rest:
                if skip:
                    skip = False
                elif after or not a.startswith("-"):
                    paths.append(a)   # a pathspec: commits the work-tree content of what it matches
                elif a == "--":
                    after = True
                elif a in ("-a", "--all"):
                    every = True
                elif a in _COMMIT_VALUE_OPT:
                    skip = True
                elif re.match(r"^-[A-Za-z]+$", a):   # a cluster (-am, -qa): letters up to one that takes a value
                    for c in a[1:]:
                        every = every or c == "a"
                        if c in "mFCct":
                            skip = c == a[-1]
                            break
            if every:
                return [p for p in _dirty(top) if hidden(p)]
            if not paths:
                return []   # the index only - nothing from the work tree
            # N6: only what the pathspec matches (as commit matches it) - `commit -- README.md` takes no hidden file
            dirty = set(_dirty(top))
            matched = _git(top, "ls-files", "-z", "--", *paths, session=True).split("\0")
            return [p for p in matched if p in dirty and hidden(p)]
        if sub == "stash":
            first = next((a for a in rest if not a.startswith("-")), "")
            if first in _STASH_KEEP:
                return []
            every = any(a in ("-a", "--all") for a in rest)
            unt = every or any(a in ("-u", "--include-untracked") for a in rest)
            return [p for p in _dirty(top, unt, every) if hidden(p)]
        if _stashes_work_tree(top, sub, rest):   # N6: merge's save_state / --autostash run `stash create`
            return [p for p in _dirty(top) if hidden(p)]
    except GitError:
        return ["?"]
    return []


# N7: options of the work-tree writers that take the next argument as their value (never a revision / path)
_WT_VALUE_OPT = {"merge": ("-m", "--message", "-F", "--file", "-s", "--strategy", "-X", "--strategy-option", "--into-name"),
                 "checkout": ("-b", "-B", "--orphan", "--conflict", "--pathspec-from-file"),
                 "switch": ("-c", "-C", "--create", "--force-create", "--orphan", "--conflict"),
                 "restore": ("-s", "--source", "--conflict", "--pathspec-from-file"),
                 "rebase": ("--onto", "-s", "--strategy", "-X", "--strategy-option", "-x", "--exec", "-C"),
                 # N8: `-s` is --signoff here (no value) - taken for a strategy it swallowed the target (fuzz)
                 "cherry-pick": ("-m", "--mainline", "--strategy", "-X", "--strategy-option"),
                 "revert": ("-m", "--mainline", "--strategy", "-X", "--strategy-option"),
                 "reset": ("--pathspec-from-file",), "stash": ("-m", "--message", "--pathspec-from-file"),
                 "mv": (), "rm": ("--pathspec-from-file",)}


def _wt_args(sub: str, rest: list[str]) -> tuple[list[str], list[str], dict]:
    """(positionals before --, pathspecs after --, {option: value}) of a work-tree writer's argv."""
    takes = _WT_VALUE_OPT.get(sub, ())
    pos, paths, vals, after, nxt = [], [], {}, False, None
    for a in rest:
        if nxt:
            vals[nxt], nxt = a, None
        elif after:
            paths.append(a)
        elif a == "--":
            after = True
        elif a.startswith("--") and "=" in a:
            k, _, v = a.partition("=")
            vals[k] = v
        elif a in takes:
            nxt = a
        elif re.fullmatch(r"-[A-Za-z]+", a) and len(a) > 2 and any(c in "".join(t[1:] for t in takes if len(t) == 2)
                                                                    for c in a[1:]):
            for i, c in enumerate(a[1:], 1):   # a cluster up to the letter that takes a value (-qm msg, -sX)
                if "-" + c in takes:
                    if a[i + 1:]:
                        vals["-" + c] = a[i + 1:]
                    else:
                        nxt = "-" + c
                    break
        elif re.fullmatch(r"-[A-Za-z]\S+", a) and a[:2] in takes:
            vals[a[:2]] = a[2:]   # an attached value (-sours, -bfeat)
        elif not a.startswith("-") or a == "-":
            pos.append(a)
    return pos, paths, vals


def _hidden_below(top: str, rel: str, hidden) -> list[str]:
    """Hidden paths at or under `rel` in the work tree (a directory is walked; links are not followed)."""
    rel = os.path.normpath(rel)
    if rel.startswith(("/", "..")) or rel == ".git":
        return []
    if rel != "." and hidden(rel):
        return [rel]
    full = os.path.join(top, rel)
    if os.path.islink(full) or not os.path.isdir(full):
        return []
    out = []
    for d, dirs, files in os.walk(full):
        dirs[:] = [x for x in dirs if x != ".git"]
        for n in files + [x for x in dirs]:
            p = os.path.relpath(os.path.join(d, n), top)
            if hidden(p):
                out.append(p)
        dirs[:] = [x for x in dirs if not hidden(os.path.relpath(os.path.join(d, x), top))]
        if len(out) > 20:
            break
    return out


def _changed(top: str, a: str, b: str) -> list[str]:
    return [p for p in _git(top, "diff", "--no-renames", "--name-only", "-z", a, b, "--", session=True).split("\0") if p]


def _commit(top: str, rev: str) -> str:
    """The commit `rev` names ('' = none - a path, a typo)."""
    if not rev or rev.startswith("-"):
        return ""
    if ":" in rev:   # N10: `:/<text>`, `<rev>:<path>` take the rest as text / a path - peel the oid, not the string
        rev = _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", rev, session=True, ok=(0, 1)).strip()
        if not rev:
            return ""
    return _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", rev + "^{commit}", session=True,
                ok=(0, 1)).strip()


def _tree_only(top: str, rev: str) -> bool:
    """N9: `rev` names a tree but no commit (HEAD:config, <tree oid>) - restore / checkout take it as a source, and
    its paths are not the work tree's: no check maps them, so a card."""
    if not rev or rev.startswith("-"):
        return False
    o = _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", rev, session=True, ok=(0, 1)).strip()
    # peeled by its oid: "HEAD:config^{tree}" would name the path config^{tree}
    return bool(o) and bool(_git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", o + "^{tree}",
                                 session=True, ok=(0, 1)).strip())


def moves_hidden(top: str, args: list[str], hidden) -> list[str]:
    """N7: hidden work-tree files a card-free `mv` / `rm` would MOVE (out of hiding: `mv config cfg2` - the sandbox
    then reads them under the new name) or delete (`rm -rf config`). The argv names only their directory, so
    run_warnings' hidden-name check missed it. Fail closed: ["?"] when a pathspec cannot be judged."""
    sub, rest = _sub(args)
    if sub not in ("mv", "rm") or (sub == "rm" and any(a == "--cached" for a in rest)):
        return []
    pos, paths, vals = _wt_args(sub, rest)
    if vals:
        return ["?"]   # --pathspec-from-file: auto_blockers asks anyway
    flags = [a for a in rest[:rest.index("--")] if a.startswith("-")] if "--" in rest else [a for a in rest if a.startswith("-")]
    if any(a in ("-n", "--dry-run") or re.fullmatch(r"-[A-Za-z]*n[A-Za-z]*", a) for a in flags):
        return []   # N8: a dry run moves nothing (no card that allows nothing)
    recursive = sub == "mv" or any(a in ("--recursive",) or re.fullmatch(r"-[A-Za-z]*[rR][A-Za-z]*", a) for a in flags)
    srcs = (pos + paths)[:-1] if sub == "mv" else pos + paths
    out = []
    for src in srcs:
        if not recursive and not (src.startswith(":") or any(c in src for c in "*?[")) and \
                os.path.isdir(os.path.join(top, src)) and not os.path.islink(os.path.join(top, src)):
            continue   # N8: `rm <dir>` without -r - git refuses it before it deletes anything
        if sub == "rm" and (src.startswith(":") or any(c in src for c in "*?[")):
            try:   # a pathspec with magic or a glob: what it matches in the index
                out += [p for p in _git(top, "ls-files", "-z", "--", src, session=True).split("\0") if p and hidden(p)]
            except GitError:
                return ["?"]
            continue
        out += _hidden_below(top, src, hidden)
    return sorted(set(out))


def rewrites_hidden(top: str, args: list[str], hidden) -> list[str]:
    """N7: hidden work-tree files a card-free merge / checkout / switch / cherry-pick / revert / rebase / reset
    --merge|--keep / restore / stash apply would REWRITE with a commit's content. With .git open (K1) the sandbox
    writes such a commit itself (its hidden file is a stand-in it cannot change) - host git then replaces the real
    file, and a replaced file can lose the sandbox's over-mount. `pull` merges what it fetches: a card-free pull runs
    as fetch + this check on the fetched commit (N8: pull_plan / pull_step, Manager._git_pull).
    A target that names no commit adds nothing (git refuses before it touches the work tree); ["?"] (a card)
    when a check itself fails or a range is too long."""
    sub, rest = _sub(args)
    if sub not in ("merge", "checkout", "switch", "cherry-pick", "revert", "rebase", "reset", "restore", "stash"):
        return []
    if any(a in ("--abort", "--quit", "--skip", "--continue") for a in rest):
        return []
    pos, paths, vals = _wt_args(sub, rest)
    if sub in ("checkout", "switch", "merge") and pos[:1] == ["-"]:
        pos[0] = "@{-1}"   # the previous branch
    elif sub in ("cherry-pick", "revert", "rebase"):   # N8: `-` is @{-1} there too (cherry-pick - went card-free)
        pos = ["@{-1}" if p == "-" else p for p in pos]
    try:
        changed: list[str] = []
        if sub == "switch" and ("--orphan" in vals or any(a.startswith("--orphan=") for a in rest)):
            # an empty work tree: every tracked file goes, hidden ones too
            changed += [p for p in _git(top, "ls-files", "-z", session=True).split("\0") if p]
        elif sub == "merge":
            for t in pos or ["@{upstream}"]:
                c = _commit(top, t)
                if not c:
                    continue   # no such commit: git refuses the merge before it touches the work tree
                base = _git(top, "merge-base", "HEAD", c, session=True, ok=(0, 1)).strip()
                changed += _changed(top, base or _EMPTY_TREE, c)
        elif sub == "restore" and any(a in ("-S", "--staged") for a in rest) and \
                not any(a in ("-W", "--worktree") for a in rest):
            pass   # the index only - the work tree stays
        elif sub in ("checkout", "switch", "restore"):
            first = _commit(top, pos[0]) if pos and sub != "restore" else ""
            if sub == "restore" or (sub == "checkout" and (paths or len(pos) > 1 or (pos and not first))):
                # paths: from a source (restore --source, checkout <commit> <paths>) or from the index - a sandbox
                # with .git open stages what it likes there (update-index), so both are its content
                src = vals.get("-s") or vals.get("--source") or ""
                if sub == "checkout" and not first and pos and (paths or len(pos) > 1) and _tree_only(top, pos[0]):
                    return ["?"]   # N9 (fuzz): `checkout HEAD:config -- config` - a tree as the source (N10: no `--` too)
                specs = (pos[1:] if first else pos) + paths
                if first:
                    src = pos[0]
                if src:
                    c = _commit(top, src)
                    if not c and _tree_only(top, src):
                        return ["?"]   # N9 (fuzz): `restore --source=HEAD:config config` rewrote a hidden file card-free
                    if not c:
                        return []   # git refuses an unknown source
                    changed += [p for p in _git(top, "ls-tree", "-r", "--name-only", "-z", c, "--", *specs,
                                                session=True).split("\0") if p]
                elif specs:
                    changed += [p for p in _git(top, "diff", "--no-renames", "--name-only", "-z", "--", *specs,
                                                session=True).split("\0") if p]
            elif first:   # a branch switch (also -b/-B/-c/-C/--detach <start>): HEAD's tree -> that commit's
                changed += _changed(top, "HEAD", first)
        elif sub == "reset":
            if not any(a in ("--merge", "--keep") for a in rest) or paths:
                return []
            c = _commit(top, pos[0]) if pos else ""
            if c:   # an unknown target: git refuses
                changed += _changed(top, "HEAD", c)
        elif sub == "rebase":
            for t in [vals.get("--onto", "")] + (pos[:1] or ["@{upstream}"]):
                c = _commit(top, t) if t else ""
                if c:
                    base = _git(top, "merge-base", "HEAD", c, session=True, ok=(0, 1)).strip()
                    changed += _changed(top, base or _EMPTY_TREE, c)
            c = _commit(top, pos[1]) if pos[1:] else ""
            if c:
                changed += _changed(top, "HEAD", c)
        elif sub in ("cherry-pick", "revert"):
            if any(".." in t or t.startswith("^") for t in pos):   # a range: each of its commits (git walks them)
                rl = _git(top, "rev-list", "--max-count=201", "--end-of-options", *pos, session=True, ok=(0, 128, 129))
                if len(rl.split()) > 200:
                    return ["?"]
                todo = [rl.split()]
            else:   # a name that resolves to no commit: git refuses before it touches the work tree
                todo = [[c] for c in (_commit(top, t) for t in pos) if c]
            commits = [c for cs in todo for c in cs]
            if commits:   # N8: one diff-tree for the whole range (one per commit took ~2 s for 200 on a big repo)
                changed += [p for p in _git(top, "diff-tree", "--stdin", "--no-commit-id", "-r", "-m", "--root",
                                            "--name-only", "-z", session=True,
                                            input="\n".join(commits).encode() + b"\n").split("\0") if p]
        elif sub == "stash":
            verb = pos[0] if pos else ""
            if verb not in ("apply", "pop", "branch"):
                return []
            ref = (pos[2] if verb == "branch" and len(pos) > 2 else pos[1] if verb != "branch" and len(pos) > 1
                   else "stash@{0}")
            if re.fullmatch(r"\d+", ref):   # N8: `stash apply 1` is stash@{1}
                ref = "stash@{%s}" % ref
            c = _commit(top, ref)
            if not c:
                return []   # no such stash: git refuses
            changed += _changed(top, c + "^1", c)
            if _commit(top, c + "^3"):   # the untracked files of `stash -u`
                changed += [p for p in _git(top, "ls-tree", "-r", "--name-only", "-z", c + "^3", session=True).split("\0") if p]
    except GitError:
        return ["?"]
    return sorted({p for p in changed if hidden(p)})


_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def rewrite_blobs(top: str, args: list[str], paths: list[str]) -> set[str] | None:
    """The blob oids of hidden `paths` that a branch switch (switch / checkout <branch>) or a merge takes from its
    commit - a merge only when its result is that blob (HEAD's equals the merge base's or the target's). None:
    another form, an octopus, a target lacking a path, a local file HEAD does not track at a path (or a check
    failed) - the caller asks. Piper checks these on the GitLab server (2026-10-12): content the sandbox committed
    itself exists only locally."""
    sub, rest = _sub(args)
    pos, wt_paths, vals = _wt_args(sub, rest)
    if sub in ("checkout", "switch", "merge") and pos[:1] == ["-"]:
        pos[0] = "@{-1}"
    targets, also = [], []   # a hidden file a target lacks is deleted - the net would put it back: ask instead
    try:
        if sub == "switch" and not wt_paths and not ("--orphan" in vals or any(a.startswith("--orphan=") for a in rest)):
            targets = [_commit(top, pos[0])] if pos else []
        elif sub == "checkout" and not wt_paths and len(pos) == 1:
            targets = [_commit(top, pos[0])]
        elif sub == "merge":
            for t in pos or ["@{upstream}"]:
                c = _commit(top, t)
                targets.append(c)
                also += [_git(top, "merge-base", "HEAD", c, session=True, ok=(0, 1)).strip() if c else "", "HEAD"]
        else:
            return None
        if not targets or not all(targets) or len(targets) > 1:   # octopus: each head merges into the last result
            return None
        trees = {}
        for c in dict.fromkeys(targets + [x for x in also if x] + ["HEAD"]):
            trees[c] = {}
            for line in _git(top, "ls-tree", "-z", c, "--", *paths, session=True).split("\0"):
                meta, _, name = line.partition("\t")
                if meta.split(" ")[1:2] == ["blob"]:
                    trees[c][name] = meta.split(" ")[2]
        t = trees[targets[0]]
        if set(t) != set(paths) or any(p not in trees["HEAD"] and os.path.lexists(os.path.join(top, p)) for p in paths):
            return None   # review 2026-10-12: git overwrites an ignored local file the target tracks - its only copy
        if also:   # review 2026-10-12: the merged result must BE the target's blob (HEAD unchanged since the base,
            h, b = trees["HEAD"], trees.get(also[0], {})   # or equal) - both sides changed: a new blob / conflict
            if any(h.get(p) not in (t[p], b.get(p)) for p in paths):
                return None
        return set(t.values())
    except (GitError, IndexError):
        return None


def blob_oid(top: str, data: bytes) -> str:
    """What `git hash-object` names `data` in this repository (sha1, or sha256 for such a repository)."""
    fmt = _git(top, "rev-parse", "--show-object-format", session=True, ok=(0, 128, 129)).strip()
    h = hashlib.sha256() if fmt == "sha256" else hashlib.sha1()
    h.update(b"blob %d\0" % len(data) + data)
    return h.hexdigest()


# ------------------------------------------------------------------ N8: the hidden files a work-tree writer must leave
# rewrites_hidden judges the commits the argv names NOW; host git resolves those names again when it runs - with .git
# open (K1) the sandbox moves a ref in between (a loop of update-ref wins that race) and `merge good` merges its evil
# commit. Where no card announced a hidden-file rewrite, the hidden files are kept as they were: snapshotted before,
# compared after, put back IN PLACE (never a rename over a path the sandbox over-mounts) when the run changed them.
WT_WRITERS = frozenset(("merge", "checkout", "switch", "cherry-pick", "revert", "rebase", "reset", "restore", "stash",
                        "pull", "am", "apply", "mv", "rm", "worktree"))
_SNAP_MAX = (400, 32 << 20)   # files, bytes: a project's hidden config, not a hidden build tree


def _hidden_names(top: str, hidden) -> list[str]:
    """The hidden work-tree paths git may write: tracked ones, and ignored untracked ones (a checkout overwrites
    those) - the files below a hidden ignored directory one by one (no symlinked directory followed)."""
    names = [p for p in _git(top, "ls-files", "-z", "--cached", session=True).split("\0") if p]
    # ignored as the run sees it (N10): the user's global core.excludesFile too - git overwrites what that ignores
    names += [p for p in _git(top, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory").split("\0") if p]
    out: list[str] = []
    for p in dict.fromkeys(n.rstrip("/") for n in names):
        if not hidden(p):
            continue
        full = os.path.join(top, p)
        if os.path.isdir(full) and not os.path.islink(full):
            for d, dirs, files in os.walk(full):
                dirs[:] = [x for x in dirs if not os.path.islink(os.path.join(d, x))]
                out += [os.path.relpath(os.path.join(d, f), top) for f in files]
        else:
            out.append(p)
    return out


def hidden_snapshot(top: str, hidden) -> dict | None:
    """{rel: (mode, bytes) | None (absent / not a regular file)} of the hidden work-tree files git may write.
    None = too many to keep (no net: the caller asks first). N9: only entries with a file count - index entries
    alone (K1: `update-index --add --cacheinfo` needs no file) switched the net off silently."""
    snap: dict = {}
    files = size = 0
    for rel in _hidden_names(top, hidden):
        snap[rel] = _snap_one(top, rel)
        if snap[rel] is not None:
            files += 1
            size += len(snap[rel][1])
        if files > _SNAP_MAX[0] or size > _SNAP_MAX[1]:
            return None
    return snap


def _snap_one(top: str, rel: str):
    try:
        st = os.lstat(os.path.join(top, rel))
        if not stat.S_ISREG(st.st_mode):
            return ("other", b"")
        return (st.st_mode & 0o7777, common.safe_read(top, rel, _SNAP_MAX[1]))
    except FileNotFoundError:
        return None
    except OSError:
        return ("other", b"")


def _shield_fd(top: str, over: str, ident) -> int:
    """A directory fd of over-mount `over` below `top` - refused (OSError) unless it is the very directory pinned
    when the sandbox started (st_dev, st_ino): content written through this fd stays under the sandbox's
    over-mount wherever the sandbox moved the directory's parent (N9)."""
    fd = common._open_dir(top, common._parts(over))
    st = os.fstat(fd)
    if [st.st_dev, st.st_ino] != list(ident):
        os.close(fd)
        raise OSError("%s is not the over-mounted directory pinned at the start" % over)
    return fd


def _shielded_write(top: str, rel: str, shield, data: bytes | None, mode: int = 0o644) -> None:
    """Write `data` IN PLACE at hidden `rel` (None: unlink it) through the pinned over-mounted directory that
    covers it - shield(rel) -> (over-mount, identity) or None (nowhere the sandbox cannot see: OSError)."""
    where = shield(rel) if shield else None
    if not where:
        raise OSError("not inside an intact over-mounted directory - the sandbox could read it there")
    over, ident = where
    dfd = _shield_fd(top, over, ident)
    try:
        parts = common._parts(os.path.relpath(rel, over))
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=dfd)
            except FileExistsError:
                pass
            nfd = os.open(part, common._DIRFLAGS, dir_fd=dfd)
            os.close(dfd)
            dfd = nfd
        if data is None:
            os.unlink(parts[-1], dir_fd=dfd)   # never follows the final symlink
            return
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | common._NOFOLLOW, mode, dir_fd=dfd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("%s is not a regular file" % rel)
            os.ftruncate(fd, 0)
            os.fchmod(fd, mode & 0o7777)
            common._write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def hidden_restore(top: str, snap: dict, hidden, shield=None, keep=frozenset()) -> tuple[list[str], list[str], dict]:
    """Put the hidden files of `snap` back as they were (in place), remove a hidden file the run created.
    N9: only through a pinned, intact over-mounted directory (shield, _shielded_write) - by name the net wrote the
    real content into a directory the sandbox had put in place of the moved one, or into a file whose over-mount
    host git had just replaced. (changed, could not be put back, {rel: bytes}: content the net displaced or could
    not put back - the caller keeps it where the human finds it)."""
    names = set(snap)
    try:
        names |= set(_hidden_names(top, hidden))   # uncapped: 400 new hidden files are removed too
    except GitError:
        pass
    changed, failed, kept = [], [], {}
    for rel in sorted(names):
        was, now = snap.get(rel), _snap_one(top, rel)
        if was == now or now is not None and now[0] != "other" and blob_oid(top, now[1]) in keep:
            continue   # keep: content the run's check found on the GitLab server (a branch switch, a pull)
        changed.append(rel)
        if now is not None and now[0] != "other" and now[1] != (was or (0, b""))[1]:
            kept["now/" + rel] = now[1]   # what the run (or a human, in that window) left there
        try:
            if now is not None and (was is None or now[0] == "other"):
                if os.path.isdir(os.path.join(top, rel)) and not os.path.islink(os.path.join(top, rel)):
                    raise OSError("a directory now")
                _shielded_write(top, rel, shield, None)   # a symlink / a new file: gone
            if was is not None and was[0] != "other":
                _shielded_write(top, rel, shield, was[1], mode=was[0])
        except OSError:
            failed.append(rel)
            if was is not None and was[0] != "other":
                kept["was/" + rel] = was[1]   # the original: never lost, never where the sandbox reads it
    return changed, failed, kept


# ------------------------------------------------------------------ N8: a card-free pull, checked like a merge
# `pull` merges what it fetches - unknown before the fetch, so rewrites_hidden could not judge it (F-N7-2: with .git
# open the agent pushes a commit that changes a hidden file, then a card-free pull wrote it into the host work tree).
# A card-free pull now runs as pull runs it (builtin/pull.c): fetch, then merge / rebase of the fetched commit - by
# its oid, never FETCH_HEAD or a ref the sandbox can move between the check and the run - and that step goes through
# the same checks as a merge / rebase the agent named itself. A form not modelled here asks first (pull_plan None).
_PULL_VERBOSITY = ("-q", "--quiet", "-v", "--verbose")
_PULL_FETCH = ("-p", "--prune", "--no-prune", "--tags", "--no-tags", "-4", "-6", "--ipv4", "--ipv6", "--all",
               "--unshallow", "--update-shallow", "--set-upstream", "--progress", "--no-progress")
_PULL_FETCH_VALUE = ("--depth", "--deepen", "--shallow-since", "--shallow-exclude", "--jobs")   # =v or the next argument
_PULL_MERGE = ("--commit", "--no-commit", "--no-edit", "--verify", "--no-verify", "--signoff", "--no-signoff",
               "--allow-unrelated-histories", "--squash", "--no-squash")
_PULL_STAT = ("--stat", "-n", "--no-stat", "--summary", "--no-summary")
_PULL_DIVERGENT = (
    "hint: You have divergent branches and need to specify how to reconcile them.\n"
    "hint: You can do so by running one of the following commands sometime before\n"
    "hint: your next pull:\nhint:\n"
    "hint:   git config pull.rebase false  # merge\n"
    "hint:   git config pull.rebase true   # rebase\n"
    "hint:   git config pull.ff only       # fast-forward only\nhint:\n"
    "hint: You can replace \"git config\" with \"git config --global\" to set a default\n"
    "hint: preference for all repositories. You can also pass --rebase, --no-rebase,\n"
    "hint: or --ff-only on the command line to override the configured default per\n"
    "hint: invocation.\nfatal: Need to specify how to reconcile divergent branches.\n")
_PULL_NOT_FF = ("hint: Diverging branches can't be fast-forwarded, you need to either:\nhint:\n"
                "hint: \tgit merge --no-ff\nhint:\nhint: or:\nhint:\nhint: \tgit rebase\nhint:\n"
                "hint: Disable this message with \"git config set advice.diverging false\"\n"
                "fatal: Not possible to fast-forward, aborting.\n")


def _config(top: str, key: str, kind: str = "") -> str:
    """N9: as the run reads it (global / system config included - see _config_true)."""
    return _git(top, "config", *(["--type=" + kind] if kind else []), "--get", key, ok=(0, 1)).strip()


def _fails(top: str, *args: str) -> bool:
    """True when the git command exits non-zero (a check that cannot run counts as failed)."""
    try:
        _git(top, *args, session=True)
        return False
    except GitError:
        return True


def pull_plan(top: str, args: list[str]) -> dict | None:
    """N8: how pull would run `args` - {fetch, mode ...} - or None for a form this does not model (a card, then the
    real pull): an unknown option, a refspec with a destination, several refspecs, an interactive rebase, an
    unborn branch."""
    if args[:1] != ["pull"]:
        return None
    rest = args[1:]
    verb, fetch, merge, stat, strat, pos = [], [], [], [], [], []
    rebase_flag = ff_flag = autostash = log = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            pos += rest[i + 1:]
            break
        if a in _PULL_VERBOSITY:
            verb.append(a)
        elif a in _PULL_FETCH:
            fetch.append(a)
        elif a.partition("=")[0] in _PULL_FETCH_VALUE and ("=" in a or i + 1 < len(rest)):
            fetch += [a] if "=" in a else [a, rest[i + 1]]
            i += 0 if "=" in a else 1
        elif a in ("-r", "--rebase", "--no-rebase") or a.startswith("--rebase="):
            v = a.partition("=")[2] if "=" in a else ("false" if a == "--no-rebase" else "true")
            rebase_flag = {"true": True, "false": False, "merges": "merges", "m": "merges"}.get(v)
            if rebase_flag is None:
                return None   # interactive (or a value git refuses)
        elif a in ("--ff", "--no-ff", "--ff-only"):
            ff_flag = a
        elif a in ("--autostash", "--no-autostash"):
            autostash = a == "--autostash"
        elif a in ("--log", "--no-log"):
            log = a == "--log"
        elif a in _PULL_STAT:
            stat.append(a)
        elif a in _PULL_MERGE:
            merge.append(a)
        elif a in ("-s", "--strategy", "-X", "--strategy-option") and i + 1 < len(rest):
            strat += [a, rest[i + 1]]
            i += 1
        elif a.startswith(("--strategy=", "--strategy-option=")):
            strat.append(a)
        elif a.startswith("-") and a != "-":
            return None
        else:
            pos.append(a)
        i += 1
    if len(pos) > 2 or any(p.startswith(("-", "+")) or ":" in p for p in pos):
        return None   # a refspec with a destination (it may update a local branch), several refspecs
    try:
        if not _commit(top, "HEAD"):
            return None   # an unborn branch: pull checks the fetched commit out - asks
        branch = _git(top, "symbolic-ref", "--short", "-q", "HEAD", ok=(0, 1), session=True).strip()
        unspecified = False
        rebase = rebase_flag
        if rebase is None:   # --rebase > branch.<name>.rebase > pull.rebase
            key = "branch.%s.rebase" % branch if branch and _config(top, "branch.%s.rebase" % branch) else "pull.rebase"
            v = _config(top, key).lower()
            if not v:
                rebase, unspecified = False, True
            elif v in ("merges", "m"):
                rebase = "merges"
            elif v in ("interactive", "i"):
                return None
            else:
                rebase = _config(top, key, "bool") == "true"
        ff = ff_flag
        if ff is None:
            v = _config(top, "pull.ff")
            if v.lower() == "only":
                ff = None if rebase_flag is not None else "--ff-only"   # --[no-]rebase beats pull.ff=only
            elif v:
                ff = "--ff" if _config(top, "pull.ff", "bool") == "true" else "--no-ff"
        if rebase and ff != "--ff-only" and (merge or ff or log is not None):
            return None   # merge options with a rebase: not modelled
        if rebase and autostash is None:
            autostash = _config(top, "rebase.autoStash", "bool") == "true"
        remotes = _git(top, "remote", session=True).split()
        upstream_remote = _config(top, "branch.%s.remote" % branch) if branch else ""
    except GitError:
        return None
    return {"verb": verb, "fetch": ["fetch", *verb, *fetch, *pos], "pos": pos, "merge": merge, "stat": stat,
            "strat": strat, "rebase": rebase, "unspecified": unspecified, "ff": ff, "autostash": autostash, "log": log,
            "branch": branch, "remotes": remotes, "upstream_remote": upstream_remote,
            "reflog": " ".join(["pull", *rest])}


def pull_before_fetch(top: str, plan: dict) -> str:
    """What pull does before it fetches: a rebase without autostash wants a clean tree (pull's own refusal text, rc
    128 - '' = go on); the fork point of the upstream (rebase) into plan["fork"]."""
    plan["fork"] = ""
    if not plan["rebase"]:
        return ""
    if not plan["autostash"]:
        unstaged = _fails(top, "diff", "--quiet", "--ignore-submodules")   # refreshes the stat info, as pull does
        staged = _fails(top, "diff", "--cached", "--quiet", "--ignore-submodules", "HEAD", "--")
        if unstaged or staged:
            return "error: cannot pull with rebase: %s\n%serror: Please commit or stash them.\n" % (
                "You have unstaged changes." if unstaged else "Your index contains uncommitted changes.",
                "error: additionally, your index contains uncommitted changes.\n" if unstaged and staged else "")
    pos = plan["pos"]
    if len(pos) == 2:
        name = pos[1][len("refs/heads/"):] if pos[1].startswith("refs/heads/") else pos[1]
        ref = "refs/remotes/%s/%s" % (pos[0], name)
    elif not pos or pos[0] == plan["upstream_remote"]:
        ref = _git(top, "rev-parse", "--symbolic-full-name", "--verify", "-q", "@{upstream}", ok=(0, 1, 128),
                   session=True).strip()
    else:
        ref = ""
    if ref and _commit(top, ref):
        plan["fork"] = _git(top, "merge-base", "--fork-point", ref, "HEAD", ok=(0, 1), session=True).strip()
    return ""


def _pull_no_candidates(plan: dict) -> str:
    """pull's die_no_merge_candidates()"""
    what = "rebase against" if plan["rebase"] else "merge with"
    pos, branch = plan["pos"], plan["branch"]
    if len(pos) > 1:
        return ("There is no candidate for rebasing against among the refs that you just fetched.\n" if plan["rebase"]
                else "There are no candidates for merging among the refs that you just fetched.\n") + \
            "Generally this means that you provided a wildcard refspec which had no\nmatches on the remote end.\n"
    if pos and branch and pos[0] != plan["upstream_remote"]:
        return ("You asked to pull from the remote '%s', but did not specify\na branch. Because this is not the default "
                "configured remote\nfor your current branch, you must specify a branch on the command line.\n" % pos[0])
    if not branch:
        return ("You are not currently on a branch.\nPlease specify which branch you want to %s.\nSee git-pull(1) for "
                "details.\n\n    git pull <remote> <branch>\n\n" % what)
    remote = plan["remotes"][0] if len(plan["remotes"]) == 1 else "<remote>"
    return ("There is no tracking information for the current branch.\nPlease specify which branch you want to %s.\n"
            "See git-pull(1) for details.\n\n    git pull <remote> <branch>\n\nIf you wish to set tracking information "
            "for this branch you can do so with:\n\n    git branch --set-upstream-to=%s/<branch> %s\n\n" % (what, remote, branch))


def pull_step(top: str, plan: dict) -> dict:
    """After the fetch: {argv} - the merge / rebase pull runs, on the fetched commits' oids - or {rc, output} when
    pull stops here (nothing fetched to merge, divergent without a mode, --ff-only that cannot)."""
    try:
        path = _git(top, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD", session=True).strip()
        raw = common.safe_read(os.path.dirname(path), os.path.basename(path), 1 << 20).decode("utf-8")
    except (GitError, OSError, ValueError) as e:
        return {"rc": 128, "output": "fatal: could not read FETCH_HEAD: %s\n" % str(e)[:200]}
    lines = [ln for ln in raw.split("\n") if ln]
    heads = []
    for ln in lines:
        m = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})\t(not-for-merge)?\t.*", ln)
        if not m:   # not what fetch writes (the sandbox can write .git): merge nothing
            return {"rc": 128, "output": "fatal: FETCH_HEAD is not what the fetch wrote - nothing merged\n"}
        if not m.group(2):
            heads.append(m.group(1))
    if not heads:
        return {"rc": 1, "output": _pull_no_candidates(plan)}
    if any(not _commit(top, h) for h in heads):
        return {"rc": 128, "output": "fatal: FETCH_HEAD names an object that is not a commit - nothing merged\n"}
    rebase, ff = plan["rebase"], plan["ff"]
    if len(heads) > 1 and rebase and ff != "--ff-only":
        return {"rc": 128, "output": "fatal: Cannot rebase onto multiple branches.\n"}
    if len(heads) > 1 and ff == "--ff-only":
        return {"rc": 128, "output": "fatal: Cannot fast-forward to multiple branches.\n"}

    def ancestor(a, b):
        return not _fails(top, "merge-base", "--is-ancestor", a, b)
    can_ff = len(heads) == 1 and ancestor("HEAD", heads[0])
    divergent = not can_ff and not all(ancestor(h, "HEAD") for h in heads)
    if ff == "--ff-only":
        if divergent:
            return {"rc": 128, "output": _PULL_NOT_FF}
        rebase = False
    if ff is None and plan["unspecified"] and divergent:
        return {"rc": 128, "output": _PULL_DIVERGENT}
    stash = [] if plan["autostash"] is None else ["--autostash" if plan["autostash"] else "--no-autostash"]
    if rebase and can_ff:   # pull fast-forwards without invoking rebase
        return {"argv": ["merge", *plan["verb"], "--ff-only", *stash, heads[0]]}
    if rebase:
        base = _git(top, "merge-base", "HEAD", heads[0], ok=(0, 1), session=True).strip()
        upstream = plan["fork"] if plan["fork"] and plan["fork"] != base else heads[0]
        return {"argv": ["rebase", *plan["verb"], *(["--rebase-merges"] if rebase == "merges" else []), *plan["stat"],
                         *plan["strat"], *stash, "--onto", heads[0], upstream]}
    # the message `git merge FETCH_HEAD` writes: fmt-merge-msg of the FETCH_HEAD lines this check read (a fast-forward
    # or an up-to-date merge writes none - merge would print "-m option ignored")
    msg = []
    # N9: without a mode on the command line merge takes it from merge.ff / branch.<b>.mergeOptions - a
    # fast-forward that config turns into a real merge gets pull's message too
    cfg = _merge_config_flags(top) if ff is None else []
    eff = ff or ([a for a in cfg if a in ("--ff", "--no-ff", "--ff-only")] or [None])[-1]
    squash = "--squash" in plan["merge"] or ("--squash" in cfg and "--no-squash" not in plan["merge"])
    if (divergent or (can_ff and eff == "--no-ff")) and not squash:   # a squash commits nothing
        # merge.log / merge.branchdesc as the run reads them (N9: the user's global config too)
        msg = ["--no-log", "-m", _git(top, "fmt-merge-msg", *([] if plan["log"] is None else
                                                             ["--log" if plan["log"] else "--no-log"]),
                                      input=raw.encode()).rstrip("\n")]
        # merge credits people only in its editor (# By / # Via comment lines): never in the commit
        msg[2] = "\n".join(ln for ln in msg[2].split("\n") if not ln.startswith(("# By ", "# Via ")))
    return {"argv": ["merge", *plan["verb"], *plan["stat"], *plan["merge"], *([ff] if ff else []), *plan["strat"],
                     *stash, *msg, *heads]}


# ------------------------------------------------------------------ N6: where the sandbox cannot read that .git
# Another project the sandbox does not mount, a session whose .git is hidden: K1 does not apply there (the
# sandbox could not read those objects itself). Four review rounds (D4, R-3, R5-1, R5-2) each found another argv
# that carried a hidden file's content past a card-free git_run - colour, word diff, graph, --relative, tree
# revisions, %(raw) - so a list of what leaks does not converge. There a card-free run is an ALLOWLIST instead:
# the subcommand, EVERY option and every revision must be known to print only names, object ids, messages or
# plain `diff --git a/X b/X` blocks strict_filter judges. Anything else (an option this table does not know, a
# revision that is a tree or a blob) runs after a card. auto_blockers / run_warnings still apply on top.
_XD = ("-p -u --patch -s --no-patch --stat --numstat --shortstat --name-only --name-status --raw --summary "
       "--compact-summary --patch-with-stat --patch-with-raw -R -w -b --ignore-all-space --ignore-space-change "
       "--ignore-space-at-eol --ignore-cr-at-eol --ignore-blank-lines --no-renames -M --find-renames --minimal "
       "--patience --histogram --full-index --abbrev --no-abbrev --no-color --no-ext-diff --no-textconv -W "
       "--function-context --no-function-context --exit-code --quiet --indent-heuristic --no-indent-heuristic --binary --text "
       "--check")   # --check: its lines pass check_filter
_XD_RX = (r"-U\d+|--unified=\d+|--stat=\d+(,\d+){0,2}|--stat-(width|name-width|graph-width|count)=\d+|"
          r"--diff-filter=[ACDMRTUXBacdmrtuxb*]+|--abbrev=\d+|-M\d+%?|--find-renames=\d+%?|--inter-hunk-context=\d+|"
          r"--diff-algorithm=(myers|default|minimal|patience|histogram)|"
          # porcelain word diff prefixes every content line like a patch (plain / colour words do not)
          r"--word-diff=porcelain")
# ref listings: a format of these atoms only - %(raw) / %(contents) print the object a ref names (a tag of a blob)
_XREF_FMT = (r"--format=([^%]|%%|%[0-9a-fA-F]{2}|%\((refname|objectname|objecttype|upstream|push|HEAD|symref|"
             r"worktreepath|subject|authorname|authoremail|authordate|committername|committeremail|committerdate|"
             r"taggername|taggerdate|creatordate|creator)(:[a-z0-9=,]*)?\))*")
_XREF_SORT = r"--sort=-?(refname|committerdate|creatordate|authordate|taggerdate|objectname|version:refname|v:refname)"
_XL = ("--oneline --graph --decorate --no-decorate --all --branches --tags --remotes --first-parent --merges "
       "--no-merges --reverse --abbrev-commit --no-abbrev-commit --date-order --topo-order --author-date-order "
       "--boundary --left-right --cherry-pick --cherry-mark --follow --full-history --simplify-by-decoration --source "
       "--parents --children --no-walk --no-notes -i --regexp-ignore-case --all-match --invert-grep -E "
       "--extended-regexp --fixed-strings --basic-regexp -m -c --cc --sparse --remove-empty --relative-date "
       "--use-mailmap --no-use-mailmap --pretty --clear-decorations")
# a format with %G runs gpg on the host; every other placeholder prints commit metadata or a message
_XL_RX = (r"-n?\d+|--max-count=\d+|--skip=\d+|--decorate=(short|full|auto|no)|--(max|min)-parents=\d+|"
          r"--date=[a-z0-9-]+|--(since|after|until|before|author|committer|grep)=.*|--(format|pretty)=(?!.*%G)[^\n]*")
_XL_VAL = "-n --max-count --skip --author --committer --grep --since --until --after --before"


def _xs(flags: str = "", rx: str = "", values: str = "", pos: str = "revs", revvals: str = "") -> tuple:
    """One subcommand's allowlist: flags, a regex of option=value forms, options taking the next argument,
    what the positionals are (revs: commits or paths, type-checked; paths; free: names / ids only; none) and
    the options whose value is a revision (type-checked like a positional)."""
    return (frozenset(flags.split()), re.compile(r"(%s)\Z" % rx) if rx else None, frozenset(values.split()), pos,
            frozenset(revvals.split()))


_XLOG = _xs(_XL + " " + _XD, _XL_RX + "|" + _XD_RX, _XL_VAL)
# Unlike log, show prints a selected blob verbatim. Implicit ref selectors bypass _rev_kind's
# checks of explicit revisions, so they need a card where the session cannot read .git itself.
_XSHOW = " ".join(flag for flag in _XL.split() if flag not in ("--all", "--branches", "--tags", "--remotes"))
EXPOSED = {
    "status": _xs("-s --short -b --branch --porcelain --long -z --null -u --untracked-files --ignored --show-stash "
                  "--ahead-behind --no-ahead-behind --no-renames --renames",
                  r"--porcelain=v[12]|--untracked-files=(no|normal|all)|-u(no|normal|all)|"
                  r"--ignored=(traditional|matching|no)|--find-renames=\d+%?", pos="paths"),
    "log": _XLOG,
    "show": _xs(_XSHOW + " " + _XD, _XL_RX + "|" + _XD_RX),
    "diff": _xs(_XD + " --cached --staged --merge-base", _XD_RX),
    "diff-tree": _xs(_XD + " -r -t --root --no-commit-id -m -c --cc --always -v --merge-base", _XD_RX),
    "diff-index": _xs(_XD + " --cached -m --merge-base", _XD_RX),
    "diff-files": _xs(_XD + " -q -0 -1 -2 -3 --base --ours --theirs -c --cc", _XD_RX, pos="paths"),
    "reflog": _XLOG,
    "rev-list": _xs("--all --branches --tags --remotes --count --objects --objects-edge --no-object-names "
                    "--object-names --reverse --first-parent --no-merges --merges --topo-order --date-order --parents "
                    "--children --left-right --boundary --abbrev-commit --quiet --timestamp --in-commit-order",
                    r"-n?\d+|--max-count=\d+|--skip=\d+|--(since|after|until|before|author|committer|grep)=.*|"
                    r"--(max|min)-parents=\d+|--abbrev=\d+", "-n --max-count"),
    "shortlog": _xs("-s --summary -n --numbered -e --email -c --committer", r"--group=(author|committer)"),
    "blame": _xs("-l -t -s -e --show-email -f --show-name -n --show-number -p --porcelain --line-porcelain -w -M -C "
                 "--root --show-stats -c --minimal --progress --no-progress", r"-L\d+(,[+-]?\d+)?|-M\d+|-C\d+", "-L"),
    "merge-base": _xs("--all --octopus --independent --is-ancestor --fork-point"),
    "describe": _xs("--all --tags --contains --long --always --first-parent --exact-match",
                    r"--(abbrev|candidates)=\d+|--(match|exclude)=[^\n]*", "--match --exclude", pos="free"),
    "name-rev": _xs("--tags --all --name-only --no-undefined --always", r"--(refs|exclude)=[^\n]*", pos="free"),
    "show-ref": _xs("--head --heads --tags --branches -d --dereference -s --hash --abbrev --verify -q --quiet --exists",
                    r"--(hash|abbrev)=\d+", pos="free"),
    "rev-parse": _xs("--verify -q --quiet --short --abbrev-ref --symbolic --symbolic-full-name --show-toplevel "
                     "--show-prefix --show-cdup --git-dir --git-common-dir --absolute-git-dir --is-inside-work-tree "
                     "--is-inside-git-dir --is-bare-repository --is-shallow-repository --show-object-format "
                     "--all --branches --tags --remotes",
                     r"--short=\d+|--abbrev-ref=(strict|loose)|--path-format=(absolute|relative)", pos="free"),
    "ls-files": _xs("-c --cached -d --deleted -m --modified -o --others -s --stage -u --unmerged -k --killed -z "
                    "--exclude-standard --directory --no-empty-directory --full-name -t -v -f --eol --error-unmatch "
                    "--deduplicate --sparse --resolve-undo", r"--abbrev=\d+", pos="paths"),
    "ls-tree": _xs("-d -r -t -l --long -z --name-only --name-status --object-only --full-name --full-tree --abbrev",
                   r"--abbrev=\d+", pos="free"),
    "count-objects": _xs("-v --verbose -H --human-readable", pos="none"),
    "check-ignore": _xs("-q --quiet -v --verbose -n --non-matching -z", pos="paths"),
    "check-attr": _xs("-a --all --cached -z", pos="free"),
    "show-branch": _xs("-a --all -r --remotes --topo-order --date-order --current --sparse --list --independent "
                       "--merge-base --no-name --sha1-name --topics", r"--more=\d+"),
    "cherry": _xs("-v", r"--abbrev=\d+"),
    "check-ref-format": _xs("--branch --normalize --allow-onelevel --no-allow-onelevel --refspec-pattern", pos="free"),
    "version": _xs("--build-options", pos="none"),
    # writes (card-free only in a session's own project or a mounted library): names / ids / a summary out
    "add": _xs("-A --all -u --update --no-all --ignore-removal --no-ignore-removal -n --dry-run -v --verbose -N "
               "--intent-to-add --ignore-errors --refresh --ignore-missing --sparse", pos="paths"),
    "rm": _xs("--cached -r -q --quiet -n --dry-run --ignore-unmatch --sparse -f --force", pos="paths"),
    "mv": _xs("-k -n --dry-run -v --verbose --sparse", pos="paths"),
    "commit": _xs("-a --all -q --quiet --allow-empty --allow-empty-message --no-edit -s --signoff --no-signoff "
                  "--no-verify -n -i --include -o --only --dry-run --short --porcelain --branch --long -z --null "
                  "--status --no-status --reset-author --amend --no-post-rewrite",
                  r"--(message|author|date|reuse-message|fixup|squash|trailer)=[^\n]*|"
                  r"--cleanup=(strip|whitespace|verbatim|scissors|default)",
                  "-m --message --author --date -C --reuse-message --fixup --squash --trailer", pos="paths",
                  revvals="-C --reuse-message --fixup --squash"),
    "push": _xs("-u --set-upstream --tags --follow-tags --no-follow-tags --dry-run -n -q --quiet -v --verbose "
                "--porcelain --no-verify --atomic --progress --no-progress", pos="free"),
    "pull": _xs("--rebase --no-rebase --ff --no-ff --ff-only -q --quiet -v --verbose --stat --no-stat -n --autostash "
                "--no-autostash --no-edit --commit --no-commit --prune -p --tags --no-tags --progress --no-progress",
                r"--rebase=(true|false|merges)", pos="free"),
    "fetch": _xs("--all -p --prune -t --tags --no-tags -q --quiet -v --verbose --dry-run --unshallow --progress "
                 "--no-progress -P --prune-tags --multiple", r"--depth=\d+", pos="free"),
    "merge": _xs("--ff --no-ff --ff-only --no-edit --squash --no-squash --abort --continue --quit --no-commit --commit "
                 "--stat -n --no-stat --summary -q --quiet -v --verbose --log --no-log --autostash --no-autostash "
                 "--allow-unrelated-histories --no-verify",
                 r"--message=[^\n]*|--strategy=(ort|recursive|resolve|octopus|ours|subtree)|"
                 r"-s(ort|recursive|resolve|octopus|ours|subtree)", "-m --message"),
    "rebase": _xs("--continue --abort --skip --quit --autostash --no-autostash -q --quiet -v --verbose --stat -n "
                  "--no-stat --root --keep-base --update-refs --no-update-refs --no-verify --fork-point --no-fork-point",
                  r"--onto=[^\n]*", "--onto", revvals="--onto"),
    "cherry-pick": _xs("-n --no-commit -x --continue --abort --skip --quit --ff --allow-empty -s --signoff --no-edit "
                       "--rerere-autoupdate --no-rerere-autoupdate", r"--mainline=\d+|-m\d+", "-m --mainline"),
    "revert": _xs("-n --no-commit --continue --abort --skip --quit -s --signoff --no-edit --rerere-autoupdate "
                  "--no-rerere-autoupdate", r"--mainline=\d+|-m\d+", "-m --mainline"),
    "checkout": _xs("-q --quiet --detach --track -t --no-track --merge -m --ours --theirs --progress --no-progress "
                    "--ignore-other-worktrees --guess --no-guess --overlay --no-overlay -f --force",
                    "", "-b -B --orphan"),
    "switch": _xs("-q --quiet --detach -d --track -t --no-track --guess --no-guess --merge -m --progress "
                  "--no-progress --ignore-other-worktrees --discard-changes -f --force",
                  r"--(create|force-create|orphan)=[^\n]*", "-c --create -C --force-create --orphan"),
    "reset": _xs("--soft --mixed --hard --keep --merge -q --quiet -N --intent-to-add --no-refresh --refresh"),
    "restore": _xs("-S --staged -W --worktree -q --quiet --ours --theirs --merge -m --overlay --no-overlay "
                   "--ignore-unmerged --progress --no-progress", r"--source=[^\n]*", "-s --source", pos="paths",
                   revvals="-s --source"),
    "branch": _xs("-a --all -r --remotes -v -vv --verbose -l --list --show-current --no-color --no-abbrev -i "
                  "--ignore-case --no-column -t --track --no-track --unset-upstream -q --quiet -m -M -c -C -d -D "
                  "--delete -f --force",
                  r"--(contains|no-contains|merged|no-merged|points-at|set-upstream-to)=[^\n]*|--abbrev=\d+|"
                  + _XREF_SORT + "|" + _XREF_FMT,
                  "--contains --no-contains --merged --no-merged --points-at -u --set-upstream-to",
                  revvals="--contains --no-contains --merged --no-merged --points-at"),
    "tag": _xs("-l --list --no-column -i --ignore-case -a --annotate --no-sign -d --delete -n",
               r"-n\d+|--(contains|no-contains|merged|no-merged|points-at)=[^\n]*|--message=[^\n]*|"
               + _XREF_SORT + "|" + _XREF_FMT,
               "-m --message --contains --no-contains --merged --no-merged --points-at",
               revvals="--contains --no-contains --merged --no-merged --points-at"),
    "for-each-ref": _xs("--ignore-case --omit-empty", r"--count=\d+|--(contains|no-contains|merged|no-merged|points-at|"
                        r"exclude)=[^\n]*|" + _XREF_SORT + "|" + _XREF_FMT,
                        "--contains --no-contains --merged --no-merged --points-at --exclude --count", pos="free",
                        revvals="--contains --no-contains --merged --no-merged --points-at"),
    "stash list": _XLOG,
    "stash show": _xs(_XD + " -u --include-untracked --only-untracked --no-include-untracked", _XD_RX),
    "stash push": _xs("-u --include-untracked -k --keep-index --no-keep-index -q --quiet --staged -S",
                      r"--message=[^\n]*", "-m --message", pos="paths"),
    "stash save": _xs("-u --include-untracked -k --keep-index --no-keep-index -q --quiet", pos="free"),
    "stash pop": _xs("--index -q --quiet"),
    "stash apply": _xs("--index -q --quiet"),
    "stash drop": _xs("-q --quiet"),
    "stash clear": _xs(pos="none"),
    "stash branch": _xs(),
}
# subcommands with their own complete check elsewhere: grep (grep_args / grep_scope), apply (the Manager's
# _git_apply_input), worktree (_git_worktree_command), remote / symbolic-ref / ls-remote (auto_blockers)
EXPOSED_OWN = frozenset(("grep", "apply", "worktree", "remote", "symbolic-ref", "ls-remote"))


def _rev_kind(top: str, rev: str) -> bool | None:
    """A revision argument: True = a commit (or an annotated tag of one), False = another object (a tree, a
    blob, a tag of either: its content or its names relative to a subtree), None = not a revision (a path)."""
    for part in re.split(r"\.\.\.?", rev):
        part = re.sub(r"\^(!|@|-\d*)\Z", "", part.removeprefix("^"))
        if not part:
            continue   # `..B`: HEAD
        try:
            oid = _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", part, session=True, ok=(0, 1)).strip()
            if not oid:
                if ":" in part.lstrip(":") or part.startswith(":") and not part.startswith((":(", ":/", ":!", ":^")):
                    return False   # rev:path / :path that does not resolve now: never a plain path
                return None
            kind = _git(top, "cat-file", "-t", oid, session=True).strip()
            if kind == "commit":
                continue
            if kind == "tag" and _git(top, "rev-parse", "--verify", "--quiet", "--end-of-options", part + "^{commit}",
                                      session=True, ok=(0, 1)).strip():
                continue
            return False
        except GitError:
            return False
    return True


def exposed_blockers(top: str, args: list[str]) -> list[str]:
    """N6: why `args` may not run card-free where the sandbox cannot read the repository's .git ([] = it may).
    An allowlist (EXPOSED): an unknown subcommand, option or option form, a revision that is not a commit, a
    positional where none belongs - each is a reason. The caller adds auto_blockers / run_warnings on top."""
    sub, rest = _sub(args)
    if not args or args[0] != sub:
        return ["global options before the subcommand"]
    if sub in EXPOSED_OWN:
        return []
    key = sub
    if sub == "stash":
        first = next((a for a in rest if not a.startswith("-")), "")
        key = "stash " + (first if first in ("list", "show", "pop", "apply", "drop", "clear", "branch", "push", "save")
                          else "push" if not first else "?")
        if first and key != "stash ?":
            rest = rest[:rest.index(first)] + rest[rest.index(first) + 1:]
    elif sub == "reflog" and rest[:1] in (["show"], ["exists"]):
        rest = rest[1:]
    spec = EXPOSED.get(key)
    if spec is None:
        return ["git %s is not on the list that runs without a card where this session cannot read .git" % key]
    flags, rx, values, pos, revvals = spec
    why: list[str] = []
    revs: list[str] = []
    i, after = 0, False
    while i < len(rest):
        a = rest[i]
        i += 1
        if after or a == "-" or not a.startswith("-"):
            if after:
                if pos == "none":
                    why.append("argument %s" % a[:80])
                continue
            if pos == "none":
                why.append("argument %s" % a[:80])
            elif pos == "revs":
                revs.append(a)
            continue
        if a == "--":
            after = True
            continue
        if a in flags or (rx and rx.match(a)):
            if a.startswith("--") and "=" in a and a.split("=", 1)[0] in revvals:
                revs.append(a.split("=", 1)[1] or "-")
            continue
        name, eq, _v = a.partition("=")
        if a in values and i < len(rest):   # the next argument is this option's value
            if a in revvals:
                revs.append(rest[i])
            i += 1
            continue
        if not a.startswith("--") and re.fullmatch(r"-[A-Za-z]{2,}", a):   # a cluster: -am, -qa, -vv, -bnew
            ok = True
            for k, c in enumerate(a[1:], 1):
                if "-" + c in flags:
                    continue
                if "-" + c in values:
                    if k == len(a) - 1:
                        if i < len(rest):
                            if "-" + c in revvals:
                                revs.append(rest[i])
                            i += 1
                    elif "-" + c in revvals:
                        revs.append(a[k + 1:])
                    break
                ok = False
                break
            if ok:
                continue
        why.append("option %s is not on the list for git %s" % (name[:60], key))
    for r in revs:
        if r == "-" or r.startswith("-"):
            continue
        kind = _rev_kind(top, r)
        if kind is False or (kind is None and pos != "revs"):
            why.append("%s is not a commit" % r[:80])
    return why


def repo_state(top: str) -> str:
    """"merge" / "rebase" / "cherry-pick" / "revert" / "bisect" in progress, else ""."""
    try:
        gd = _git(top, "rev-parse", "--absolute-git-dir", session=True).strip()
    except GitError:
        return ""
    for name, what in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                       ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if os.path.exists(os.path.join(gd, name)):
            return what
    return ""


def session_run(top: str, args: list[str], hidden, input: bytes | None = None, env: dict | None = None) -> dict:
    """Run a confirmed git_run in the trusted root: {rc, output}. A non-zero rc (a merge
    conflict) is an answer, not an error. env: GIT_REFLOG_ACTION of a checked pull's steps (N8)."""
    if args[:1] == ["grep"]:
        args, input = grep_scope(top, args, hidden)
    elif _sub(args)[0] == "grep":   # N4: -C / -c / --no-pager before it skipped the hidden-file scope
        raise GitError(_GREP_GLOBAL)
    # no colour whatever the repository's config says (sandbox-writable): strict_filter parses plain headers
    cmd = ["git", *SAFE, "-c", "core.hooksPath=/dev/null", *NO_COLOR, *_closed(), "-C", top, *args]
    lfs = _run_lfs_prepush(top, args)
    if lfs is not None and lfs["rc"]:
        return dict(lfs, state=repo_state(top))
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=RUN_TIMEOUT,
                           env=_closed_env(dict(os.environ, **RUN_ENV, **pinned_env(top), **(env or {}))),
                           stdin=subprocess.DEVNULL if input is None else None, input=input)
    except subprocess.TimeoutExpired:
        raise GitError("git %s timed out after %d s" % (_sub(args)[0] or args[0], RUN_TIMEOUT)) from None
    except OSError as e:
        raise GitError("git not available: %s" % e) from None
    # --relative[=<dir>]: block paths relative to a directory - no hidden rule can judge them, every block is
    # withheld (N4)
    opts = args[:args.index("--")] if "--" in args else args
    # N7 (B-N6-3): the label says why - not "a path hidden from Claude" for a visible file of a tree revision
    rel = ("paths relative to a directory can't be checked"
           if any(a == "--relative" or a.startswith("--relative=") or "diff.relative" in a.lower() for a in opts) else
           "a tree revision's paths can't be checked" if tree_revs(top, args) else "")
    label = rel or "a path hidden from Claude"
    def judged(raw: bytes) -> str:
        out = strict_filter(raw.decode("utf-8", "replace"), (lambda p: True) if rel else hidden,
                            label=label, graph="--graph" in opts)
        if "--check" in opts and _sub(args)[0] != "apply":
            out = check_filter(out, (lambda p: True) if rel else hidden, label=label)
        if args[:1] == ["remote"]:
            out = common.scrub(out)
        return out
    out = judged(r.stdout + r.stderr)
    if lfs:
        out = lfs["output"] + out
    res = {"rc": r.returncode, "output": _bounded(out), "state": repo_state(top)}
    if env:   # a checked pull's step (N8): its streams apart - pull's own output is all stdout, then all stderr
        res["streams"] = (judged(r.stdout), judged(r.stderr))
    return res


def _run_lfs_prepush(top: str, args: list[str]) -> dict | None:
    """git_run push under git_fail_closed (no pre-push hook): the LFS objects of what it pushes first.
    None = nothing to upload; {rc, output} otherwise (rc != 0 stops the push)."""
    sub, rest = _sub(args)
    if sub != "push" or not FAIL_CLOSED or not _lfs_repo(top):
        return None
    pos, skip, every = [], False, False
    for a in rest:
        if skip:
            skip = False
        elif a in ("-o", "--push-option", "--repo", "--receive-pack", "--exec"):
            skip = True
        elif a in ("--all", "--mirror", "--branches", "--tags"):
            every = True
        elif a in ("-d", "--delete"):
            return None   # deletions upload nothing
        elif not a.startswith("-"):
            pos.append(a)
    try:
        st = status(top)
    except GitError as e:
        return {"rc": 1, "output": "git lfs: %s\n" % e}
    # git push [<remote> [<refspec>...]]: the remote (a name or a URL), else the upstream's, else origin
    remote = pos[0] if pos else (st["upstream"].split("/", 1)[0] if "/" in st["upstream"] else "origin")
    srcs = [x.lstrip("+").split(":", 1)[0] for x in pos[1:]]
    refs = ["--all"] if every else [x for x in srcs if x] or (["HEAD"] if not pos[1:] else [])
    if not refs:
        return None
    try:
        return {"rc": 0, "output": lfs_prepush(top, remote, refs)}
    except GitError as e:
        return {"rc": 1, "output": "the LFS upload before the push failed, nothing pushed: %s\n" % e}
