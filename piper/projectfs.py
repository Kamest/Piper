"""Project file access on the host, shared by the Global agent's tools (D14-B) and the GUI's
Files view (9.6).

The Global agent mounts no project at all (so it has no directory for hole to hide and is
immune to K18). It reads the whitelisted checkouts through these three tools instead:

  list_dir(project, path)                 entries of one directory
  read_file(project, path, offset, limit) lines of one text file
  grep(project, pattern, path, glob)      regex matches (bounded)

Every path is resolved on the host and must stay inside the checkout (symlinks included).
`hidden(rel)` is supplied by the caller: for the agent the project's rule (policy candidates +
effective hole excludes + `.git` - not listed, not readable, not searched), for the GUI only
`.git` (a file hidden from Claude is the human's like any other; sessions.Manager.fs marks it).
NOISE (build output, IDE metadata) is left out of quick open and search unless `all` is asked.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import stat
import sys
import threading

import common
import guard

MAX_READ = 2 << 20
MAX_LIST = 1000
MAX_MATCHES = 200
MAX_FILES = 20000
# The one list of "noise" names (K1): directories and files the GUI's tree collapses (the list
# entry's `noise`) and quick open, search and the symbol index leave out unless `all` is asked;
# the agent's grep never searches them. list_dir always shows them.
NOISE = frozenset({
    # build output, dependencies, caches
    "node_modules", "target", ".gradle", ".next", ".pnpm-store", ".yarn", ".turbo", ".cache", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".venv", "venv", ".nuxt", ".svelte-kit",
    # IDE and tool metadata
    ".idea", ".claude", ".cc-writes", ".settings", ".run", ".vscode", ".project", ".classpath", ".factorypath",
    ".sdkmanrc", ".DS_Store", "Thumbs.db"})
SKIP = NOISE   # the old name (httpclient.py)


def is_noise(name: str) -> bool:
    return name in NOISE


class FsError(ValueError):
    pass


class TooLarge(FsError):
    """A file over MAX_READ: the GUI answers 413, not 404 - it exists."""


class Binary(FsError):
    """A file that is not text: the GUI answers 415, not 404."""


def split_lines(text: str) -> list[str]:
    """Lines as git and the IDE count them: split on \n only (str.splitlines() also splits on
    \f, \x1c, U+2028 ... and line numbers would drift), a trailing \r dropped per line."""
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def eol_info(raw: bytes) -> tuple[str, bool]:
    """(dominant line ending "\n" | "\r\n", ends with a newline) of a file's bytes."""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return ("\r\n" if crlf > lf else "\n"), raw.endswith(b"\n")


def _resolve(root: str, rel: str, hidden) -> tuple[str, str]:
    rel = (rel or ".").strip()
    if rel.startswith("/") or "\0" in rel or ".." in rel.replace("\\", "/").split("/"):
        raise FsError("path must be relative to the project, without '..'")
    real_root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(real_root, rel))
    if not (full == real_root or full.startswith(real_root + os.sep)):
        raise FsError("%s leaves the project" % rel)
    real_rel = os.path.relpath(full, real_root)
    norm = os.path.normpath(rel)
    for r in (norm, real_rel):
        if r != "." and guard.is_git_path(r):
            raise FsError("%s is inside .git" % rel)
        if r != "." and hidden(r):
            raise FsError("%s is hidden by the isolation policy" % rel)
    return full, ("" if real_rel == "." else real_rel)


def _chain(full: str, child: str, hidden, limit: int = 24) -> str:
    """Compact packages (IntelliJ "compact middle packages"): the single-subdirectory chain
    below `child` - `com/acme/shop` for src/main/java/com/acme/shop - "" when it branches.
    A hidden, symlinked or only-child file ends the chain; names only, nothing is read."""
    chain: list[str] = []
    cur, rel = full, child
    for _ in range(limit):
        try:
            entries = [e for e in os.scandir(cur) if e.name != ".git"]
        except OSError:
            break
        if len(entries) != 1:
            break
        e = entries[0]
        nrel = rel + "/" + e.name
        if e.is_symlink() or not e.is_dir(follow_symlinks=False) or hidden(nrel):
            break
        chain.append(e.name)
        cur, rel = e.path, nrel
    return "/".join(chain)


def list_dir(root: str, rel: str, hidden, compact: bool = False) -> dict:
    """Entries of one directory; what `hidden` names, `.git` and links leaving the project are
    never listed. compact (GUI) adds `chain` to a directory whose only content is one more
    directory."""
    full, base = _resolve(root, rel, hidden)
    if not os.path.isdir(full):
        raise FsError("%s is not a directory" % rel)
    out = []
    for e in sorted(os.scandir(full), key=lambda x: x.name):
        child = os.path.join(base, e.name) if base else e.name
        if e.name == ".git" or hidden(child):
            continue
        try:
            _resolve(root, child, hidden)   # a link that leaves the project or hits a hidden path
        except FsError:
            continue
        kind = "dir" if e.is_dir() else "file"
        size = e.stat().st_size if kind == "file" else None
        item = {"name": e.name, "type": kind, "size": size, "link": e.is_symlink()}
        if compact and kind == "dir" and not e.is_symlink():
            ch = _chain(e.path, child, hidden)
            if ch:
                item["chain"] = ch
        out.append(item)
        if len(out) >= MAX_LIST:
            break
    return {"path": base or ".", "entries": out, "truncated": len(out) >= MAX_LIST}


def read_file(root: str, rel: str, hidden, offset: int = 0, limit: int = 2000) -> dict:
    full, base = _resolve(root, rel, hidden)
    st = os.stat(full)
    if not stat.S_ISREG(st.st_mode):
        raise FsError("%s is not a regular file" % rel)
    if st.st_size > MAX_READ:
        raise TooLarge("%s is larger than 2 MiB (%d bytes) - not opened" % (rel, st.st_size))
    try:   # the checked real path, read without following a link swapped in since the check
        raw = common.safe_read(os.path.realpath(root), base, MAX_READ + 1)
    except OSError as e:
        raise FsError("%s cannot be read: %s" % (rel, e)) from None
    if len(raw) > MAX_READ:
        raise TooLarge("%s is larger than 2 MiB - not opened" % rel)
    if b"\0" in raw[:8192]:
        raise Binary("%s looks binary - not opened as text" % rel)
    lines = split_lines(raw.decode("utf-8", "replace"))
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 5000))
    eol, final = eol_info(raw)
    return {"path": base, "total_lines": len(lines), "offset": offset,
            "lines": ["%d\t%s" % (offset + i + 1, l) for i, l in enumerate(lines[offset:offset + limit])],
            # the optimistic lock of the GUI's save (9.7 #6): sha256 of the whole file as read;
            # eol / final_newline: what the save writes back (the GUI edits with \n only)
            "sha": hashlib.sha256(raw).hexdigest(), "eol": eol, "final_newline": final}


def files(root: str, hidden, limit: int = MAX_FILES, all: bool = False) -> dict:
    """Every visible regular file (relative paths) for quick open; same walk rules as grep:
    NOISE left out unless `all`; a symlinked file is not listed (skipped_links counts them -
    the sandbox may re-point a link between the listing and the read)."""
    real_root = os.path.realpath(root)
    out, links = [], 0
    for dirpath, dirnames, filenames in os.walk(real_root, followlinks=False):
        drel = os.path.relpath(dirpath, real_root)
        drel = "" if drel == "." else drel
        dirnames[:] = sorted(d for d in dirnames if d.lower() != ".git" and (all or d not in NOISE)
                             and not hidden(os.path.join(drel, d) if drel else d))
        for fn in sorted(filenames):
            frel = os.path.join(drel, fn) if drel else fn
            # a `.git` FILE (a worktree's or submodule's gitlink) is git's, as in grep
            if guard.is_git_path(frel) or (not all and fn in NOISE) or hidden(frel):
                continue
            if os.path.islink(os.path.join(dirpath, fn)):
                links += 1
                continue
            out.append(frel)
            if len(out) >= limit:
                return {"files": out, "truncated": True, "skipped_links": links}
    return {"files": out, "truncated": False, "skipped_links": links}


def _compile(pattern: str, ignore_case: bool = False):
    if len(pattern) > 500:   # Python's re has no timeout: keep what an agent sends small
        raise FsError("pattern longer than 500 characters")
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise FsError("invalid regex: %s" % e) from None


# POSIX bracket classes whose meaning git -P (PCRE2 with UCP: Unicode) and Python's re share; the
# others (alpha, upper, punct …) are Unicode in git and would be ASCII ranges here
_POSIX = {"digit": "\\d", "space": "\\s", "word": "\\w"}


def posix_to_python(pattern: str) -> str | None:
    """The pattern for Python's re with git's POSIX classes ([[:digit:]] -> [\\d]); None when it
    holds something Python would read differently - a class other than digit / space / word,
    \\Q…\\E, [[:^x:]], [[.x.]], [[=x=]], an escaped "\\[:" - and the caller says its answer is partial."""
    if re.search(r"\\[QE]|\[:\^|\[[.=]|\\\[:", pattern):
        return None
    bad = []

    def sub(m: re.Match) -> str:
        if m.group(1) not in _POSIX:
            bad.append(m.group(1))
        return _POSIX.get(m.group(1), "")
    out = re.sub(r"\[:([a-z]+):\]", sub, pattern)
    return None if bad else out


def _walk_grep(real_root: str, full: str, hidden, rx, glob, out: dict, limit: int, deadline: float | None = None,
               all: bool = False) -> bool:
    """Matches below `full` (a dir or one regular file) into out["matches"]; out["files"] is the
    file budget shared by every pass of one search, out["skipped_large"] / ["skipped_links"]
    count what was not searched. True when the search must stop (truncated: the budget, the
    limit or the deadline). NOISE is not entered unless `all`."""
    import time
    matches = out["matches"]

    def one(dirpath: str, frel: str, fn: str) -> bool:
        if deadline is not None and time.monotonic() > deadline:
            return True
        # a `.git` FILE (a worktree's or submodule's gitlink) is git's, never a search hit
        if fn == ".git" or guard.is_git_path(frel) or hidden(frel) or (not all and fn in NOISE) \
                or (glob and not fnmatch.fnmatch(fn, glob) and not fnmatch.fnmatch(frel, glob)):
            return False
        p = os.path.join(dirpath, fn)
        if os.path.islink(p):
            out["skipped_links"] = out.get("skipped_links", 0) + 1
            return False
        out["files"] += 1
        if out["files"] > MAX_FILES:
            return True
        try:
            if os.path.getsize(p) > MAX_READ:
                out["skipped_large"] = out.get("skipped_large", 0) + 1
                return False
            raw = common.safe_read(real_root, frel, MAX_READ)   # no link swapped in during the walk
        except OSError:
            return False
        if b"\0" in raw[:8192]:
            return False
        for i, line in enumerate(split_lines(raw.decode("utf-8", "replace")), 1):
            if rx.search(line):
                matches.append({"path": frel, "line": i, "text": line[:400]})
                if len(matches) >= limit:
                    return True
        return False

    if not os.path.isdir(full):
        frel = os.path.relpath(full, real_root)
        return one(os.path.dirname(full), frel, os.path.basename(full))
    for dirpath, dirnames, filenames in os.walk(full, followlinks=False):
        drel = os.path.relpath(dirpath, real_root)
        drel = "" if drel == "." else drel
        dirnames[:] = [d for d in dirnames if d != ".git" and (all or d not in NOISE)
                       and not hidden(os.path.join(drel, d) if drel else d)]
        for fn in filenames:
            if one(dirpath, os.path.join(drel, fn) if drel else fn, fn):
                return True
    return False


def grep(root: str, pattern: str, hidden, rel: str = ".", glob: str | None = None,
         ignore_case: bool = False, limit: int = MAX_MATCHES, all: bool = False) -> dict:
    rx = _compile(pattern, ignore_case)
    full, base = _resolve(root, rel, hidden)
    out = {"matches": [], "files": 0}
    cut = _walk_grep(os.path.realpath(root), full, hidden, rx, glob, out, limit, all=all)
    return {"matches": out["matches"], "truncated": cut, "engine": "python",
            "skipped_large": out.get("skipped_large", 0), "skipped_links": out.get("skipped_links", 0)}


# ------------------------------------------------------------------ git grep
# The GUI's project search: `git grep` (tracked + untracked, .gitignore respected) in the
# project's TRUSTED repository root (Manager.repo_top - never a .git the sandbox planted), plus
# the Python walker over the ignored paths outside NOISE (development/ holds the user's analyses).
# The repository's config is sandbox-writable: every option that shapes the output is pinned
# on the command line, no textconv, no global/system config. Every match is re-checked here:
# outside the project, hidden, `.git`, a symlink anywhere in its path - dropped (fail closed).
GREP_TIMEOUT = 10.0
GREP_LIMIT = 2000
# a UTF-8 locale: under C, PCRE / ERE -i folds ASCII only (`öl` would miss `Öl`); the -z
# output is parsed by bytes, so the locale changes nothing else (macOS has no C.UTF-8)
GREP_LOCALE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
GIT_GREP = ["-c", "grep.column=false", "-c", "grep.fullName=false", "-c", "grep.lineNumber=true",
            "-c", "grep.extendedRegexp=false", "-c", "grep.patternType=default", "-c", "core.quotePath=false",
            "-c", "color.ui=never", "-c", "color.grep=never", "-c", "grep.fallbackToNoIndex=false"]


class GrepUnavailable(Exception):
    """git grep cannot answer (not a repository, no git, git failed): the Python walker does."""


def _git_records(proc, deadline: float):
    """(path, line, text) of `git grep -z -n` output: path NUL line NUL text LF (a path may hold
    ':' or even LF; the text ends at LF). Yields until EOF; raises TimeoutError at the deadline."""
    import time
    buf = b""
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = proc.stdout.read1(1 << 16)
        if not chunk:
            return
        buf += chunk
        while True:
            a = buf.find(b"\0")
            b = buf.find(b"\0", a + 1) if a >= 0 else -1
            c = buf.find(b"\n", b + 1) if b >= 0 else -1
            if c < 0:
                break
            path, num, text = buf[:a], buf[a + 1:b], buf[b + 1:c]
            buf = buf[c + 1:]
            try:
                n = int(num)
            except ValueError:
                continue
            yield path.decode("utf-8", "replace"), n, text.decode("utf-8", "replace")


def _git_grep_run(top: str, args: list[str], deadline: float):
    import subprocess
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", LC_ALL=GREP_LOCALE, GIT_OPTIONAL_LOCKS="0",
               GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    try:
        return subprocess.Popen(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                                 *GIT_GREP, "-C", top, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, env=env)
    except OSError as e:
        raise GrepUnavailable("git not available: %s" % e) from None


def _ls_files(top: str, args: list[str], spec: list[str], deadline: float) -> list[str]:
    import subprocess
    import time
    env = dict(os.environ, LC_ALL="C", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("search deadline")
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                            "-c", "core.quotePath=false", "-C", top, "ls-files", "-z", *args, "--", *spec],
                           capture_output=True, timeout=left, env=env, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise TimeoutError("git ls-files: search deadline") from None
    except OSError as e:
        raise GrepUnavailable("git ls-files: %s" % e) from None
    if r.returncode != 0:
        raise GrepUnavailable(r.stderr.decode("utf-8", "replace").strip()[:300])
    return [x for x in r.stdout.decode("utf-8", "replace").split("\0") if x]


def _nested_repos(top: str, d: str, deadline: float, limit: int = 5000, all: bool = False) -> tuple[list[str], bool]:
    """Repository roots (a .git dir or file) at or below the untracked directory d (repo-relative,
    ending in '/'), not below another one: git grep --untracked never enters them. The flag:
    the walk stopped early (directory limit or deadline) - the answer may miss some."""
    import time
    out, seen = [], 0
    base = os.path.join(top, d)
    for dirpath, dirnames, _ in os.walk(base, followlinks=False):
        seen += 1
        if seen > limit or time.monotonic() > deadline:
            return out, True
        if os.path.lexists(os.path.join(dirpath, ".git")):
            out.append(os.path.relpath(dirpath, top).replace(os.sep, "/") + "/")
            dirnames[:] = []
            continue
        dirnames[:] = [x for x in dirnames if (all or x not in NOISE) and x != ".git"
                       and not os.path.islink(os.path.join(dirpath, x))]
    return out, False


def _ignored(top: str, spec: list[str], deadline: float, all: bool = False) -> tuple[list[str], bool]:
    """Paths git grep does not search, below spec (repo-relative; a directory ends in '/'):
    ignored untracked paths, nested repositories (also deep inside an untracked directory) and
    submodules (gitlinks) - the Python walker covers them. Each path once, none below another
    (no match twice). The flag: the list may be incomplete. TimeoutError at the deadline."""
    out = _ls_files(top, ["-o", "-i", "--exclude-standard", "--directory"], spec, deadline)
    cut = False
    for d in _ls_files(top, ["-o", "--exclude-standard", "--directory"], spec, deadline):
        if d.endswith("/"):
            found, c = _nested_repos(top, d, deadline, all=all)
            out += found
            cut = cut or c
    for row in _ls_files(top, ["-s"], spec, deadline):
        meta, _, path = row.partition("\t")
        if meta.startswith("160000 ") and path:
            out.append(path.rstrip("/") + "/")
    keep: list[str] = []
    for x in sorted(set(out)):
        if not any(k.endswith("/") and x.startswith(k) for k in keep):
            keep.append(x)
    return keep, cut


def git_grep(root: str, top: str, pattern: str, hidden, rel: str = ".", glob: str | None = None,
             ignore_case: bool = False, limit: int = MAX_MATCHES, all: bool = False) -> dict:
    """grep() through `git grep -P` (or -E when this git has no PCRE) in the trusted repository
    root `top`, then the Python walker over the ignored paths. GrepUnavailable: use grep().
    NOISE is left out unless `all`; skipped_large: files over MAX_READ that matched nothing
    because they were not searched."""
    import time
    if len(pattern) > 500:
        raise FsError("pattern longer than 500 characters")
    limit = max(1, min(int(limit), GREP_LIMIT))
    full, base = _resolve(root, rel, hidden)
    real_root = os.path.realpath(root)
    top = os.path.realpath(top)
    if not (real_root == top or real_root.startswith(top + os.sep)):
        raise GrepUnavailable("the project is not inside its repository root")
    target = os.path.relpath(full, top)
    spec = [":(literal)" + target if target != "." else "."]
    if not all:
        spec += [x for d in sorted(NOISE) for x in (":(exclude,glob)**/%s/**" % d, ":(exclude,glob)**/%s" % d)]
    deadline = time.monotonic() + GREP_TIMEOUT
    matches: list[dict] = []
    seen: dict[str, bool] = {}   # repo path -> may be shown
    large: set[str] = set()
    truncated = False

    def visible(gpath: str) -> str | None:
        prel = os.path.relpath(os.path.join(top, gpath), real_root)
        if prel == ".." or prel.startswith("../") or prel.startswith("/"):
            return None
        if hidden(prel) or guard.is_git_path(prel):
            return None
        fn = os.path.basename(prel)
        if glob and not fnmatch.fnmatch(fn, glob) and not fnmatch.fnmatch(prel, glob):
            return None
        try:
            if common.safe_stat(real_root, prel).st_size > MAX_READ:   # a symlink in any component: refused
                large.add(prel)
                return None
        except OSError:
            return None
        return prel

    for mode in ("-P", "-E"):
        args = ["grep", "-z", "-n", "-I", "--untracked", "--no-textconv", "--no-color", mode]
        if ignore_case:
            args.append("-i")
        proc = _git_grep_run(top, args + ["-e", pattern, "--", *spec], deadline)
        killed = []   # a pattern that runs long without output: read1 blocks, the timer ends git
        timer = threading.Timer(max(0.1, deadline - time.monotonic()), lambda: (killed.append(1), proc.kill()))
        timer.daemon = True
        timer.start()
        got: list[dict] = []
        try:
            for gpath, n, text in _git_records(proc, deadline):
                if gpath not in seen:
                    seen[gpath] = visible(gpath)
                prel = seen[gpath]
                if prel is None:
                    continue
                got.append({"path": prel, "line": n, "text": (text[:-1] if text.endswith("\r") else text)[:400]})
                if len(got) >= limit:
                    truncated = True
                    break
        except TimeoutError:
            truncated = True
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            truncated = truncated or bool(killed)
            err = proc.stderr.read().decode("utf-8", "replace")
            proc.stdout.close()
            proc.stderr.close()
        if not truncated and proc.returncode not in (0, 1):
            if mode == "-P" and re.search(r"(?i)pcre|perl", err):
                continue   # built without PCRE: POSIX extended regexps
            if re.search(r"(?i)not a git repository", err):
                raise GrepUnavailable(err.strip()[:300])
            raise GrepUnavailable(err.strip()[:300] or "git grep failed (rc %d)" % proc.returncode)
        matches = got
        break
    out: dict = {}
    if not truncated:
        # the second pass: ignored paths (git grep --untracked skips them), Python's walker
        py = posix_to_python(pattern)
        try:
            rx = _compile(py, ignore_case) if py is not None else None
        except FsError:
            rx = None
        if rx is None:
            truncated = True   # syntax Python's re reads differently: the ignored paths are not searched - say so
        out = {"matches": [], "files": 0}
        try:
            extra, cut = _ignored(top, spec[:1], deadline, all) if rx is not None else ([], False)
            truncated = truncated or cut
        except (TimeoutError, GrepUnavailable):
            extra, truncated = [], True   # git grep's matches stay; the rest is cut
        for g in extra:
            if time.monotonic() > deadline or len(matches) + len(out["matches"]) >= limit:
                truncated = True
                break
            prel = os.path.relpath(os.path.join(top, g.rstrip("/")), real_root)
            if prel == ".." or prel.startswith("../") or any((not all and x in NOISE) or x == ".git" for x in prel.split("/")) \
                    or hidden(prel) or guard.is_git_path(prel):
                continue
            pfull = os.path.join(real_root, prel)
            if g.endswith("/"):
                if not common.safe_is_dir(real_root, prel):   # a symlink in the way: not walked
                    continue
            else:
                try:
                    common.safe_stat(real_root, prel)
                except OSError:
                    continue
            if _walk_grep(real_root, pfull, hidden, rx, glob, out, limit - len(matches), deadline, all):
                truncated = True
                break
        # git may have searched a path the walker did too (a .git file with a broken gitdir): once
        seen = {(m["path"], m["line"]) for m in matches}
        matches += [m for m in out["matches"] if (m["path"], m["line"]) not in seen]
    matches.sort(key=lambda m: (m["path"], m["line"]))
    if len(matches) > limit:
        matches, truncated = matches[:limit], True
    # regex: the dialect git used (-P PCRE, else POSIX ERE); the ignored paths go through Python's re
    return {"matches": matches, "truncated": truncated, "engine": "git", "regex": "pcre" if mode == "-P" else "ere",
            # git grep reports a large file only when it matched: the count is a lower bound
            "skipped_large": len(large) + out.get("skipped_large", 0), "skipped_links": out.get("skipped_links", 0)}
