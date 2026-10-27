"""The GUI workbench, part two: editor, code navigation, git
history, CI and merge requests, the dev stack, turn snapshots, diagnostics, IntelliJ, usage.

A mixin of sessions.Manager (it uses gui_project, project_hidden, hidden_for, _gl_projects,
_transcript_src, siblings ...). Everything here serves the human's GUI (9.6, 2026-10-07): a file
hidden from Claude by project_hidden(p) (policy candidates + effective hole excludes) is shown,
diffed, searched and edited like any other, and the git views show the whole repository (K12) -
only `.git` is never served. Non-Files operations retain the isolation guards (_ops_guard);
manual Files operations are independent of sandbox coverage. The working tree is sandbox-writable: reads are
symlink-safe (common.safe_*) and bounded, git runs with gitops.SAFE, --no-ext-diff --no-textconv,
never hooks.
"""
from __future__ import annotations

import errno
import hashlib
import math
import os
import re
import secrets
import shutil
import stat
import threading
import time

import common
import extensions
import quietwindow
import diagnostics
import digest
import features
import github
import gitlab
import gitops
import guard
import ideamcp
import projectfs
import projects
import stackview
import symbols
from common import audit, scrub

RAW_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
             ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".bmp": "image/bmp"}
RAW_MAX = 10 << 20
WRITE_MAX = 2 << 20
# a session's write into another checkout is confirmed on a card; these paths get a warning there
# (they run on the host without anyone starting them, or when a build does)
AUTO_RUN = {".envrc", ".vscode", ".idea", ".hole", ".husky", ".githooks", ".mvn", ".github", ".gitlab-ci.yml", ".claude"}
# the checkouts whose code the HOST runs (piperd itself, its extensions, what they name): never written from a
# session, whatever is auto-approved
INFRA_ROOTS = {os.path.realpath(x) for x in [os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), common.SK]
               + extensions.DIRS + extensions.collect("infra_roots") if x}
BUILD_FILES = re.compile(r"(^|/)(pom\.xml|package\.json|Taskfile\.ya?ml|Makefile|build\.gradle(\.kts)?|settings\.gradle(\.kts)?"
                         r"|gradlew|mvnw|Dockerfile|docker-compose[^/]*\.ya?ml|[^/]*\.(sh|bash|zsh|py|mjs|cjs))$")
CARD_DIFF_MAX = 60_000
GUI_WITHHELD = "a .git path"   # what the GUI's git views withhold (L9) - never "hidden from Claude"
BLAME_MAX = 2 << 20
CI_TTL = 30.0
EVENT_MAX_AGE = 120.0   # older hook events are a replay (piperd restarted): no snapshot
TRASH_DIR = os.path.join(common.PIPER_HOME, "trash")   # fs_delete: never unlinked, moved here
TRASH_DAYS = 7
TRASH_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
NAME_MAX = 255
FSEDITS_MAX = 50   # Session.fsedits: the files a session's agent edited
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "applypatch"}
# Files > New scratch file (IDEA Scratches): the checkout's unversioned development/scratch, one starter per type
SCRATCH_DIR = "development/scratch"
SCRATCH_TYPES = {"sql": "-- scratch\n", "http": "GET {{host}}/\n\n###\n", "json": "{\n}\n", "yaml": "", "xml": '<?xml version="1.0" encoding="UTF-8"?>\n',
                 "md": "# Scratch\n", "java": "class Scratch {\n}\n", "sh": "#!/bin/sh\n", "properties": "", "txt": ""}


def scratch_name(taken, ext: str) -> str:
    """scratch.<ext>, else scratch_2.<ext>, scratch_3.<ext> … - the first name not in `taken` (any letter case)."""
    low = {t.lower() for t in taken}
    n = 1
    while ("scratch.%s" % ext if n == 1 else "scratch_%d.%s" % (n, ext)).lower() in low:
        n += 1
    return "scratch.%s" % ext if n == 1 else "scratch_%d.%s" % (n, ext)
_DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class Workbench:
    def _wb_init(self) -> None:
        self._stack_seen: dict = {}   # stackview.stack_jobs: job id -> when it was first seen finished
        self.idea_opener = None
        self.gitlab_opener = None
        self._write_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._review_lock = threading.RLock()
        self._reviews: dict = {}
        self._ci_cache: dict = {}
        self._usage_cache = None
        self._diag_cache: dict = {}
        self._idea_status = None      # (time, status)
        self._idea_client = None
        self._idea_lock = threading.Lock()
        common.journal_recover()   # in-place writes of hidden files a crash interrupted

    # ------------------------------------------------------------ helpers
    def _pfile(self, key: str, rel: str):
        """(project, real project root, real relative path, full path) of one path of a project
        for the GUI; FsError (404) when it leaves the project or is `.git` - a file hidden from
        Claude is the GUI's like any other."""
        p = self.gui_project(key)
        full, base = projectfs._resolve(p["path"], str(rel or ""), guard.is_git_path)
        if not base:
            raise projectfs.FsError("a path inside the project is required")
        return p, os.path.realpath(p["path"]), base, full

    def _top(self, p: dict) -> str:
        """The trusted repository root of project p (Manager.repo_top) - never what git
        discovers from a path inside the sandbox-written tree."""
        top = self.repo_top(p["path"])
        if top is None:
            raise gitops.GitError("no trusted repository for %s: %s" % (p.get("key", p["path"]), self.repo_top_why(p["path"])))
        return top

    def _repo_hidden(self, p: dict, top: str, hidden=None):
        """`hidden(repo-relative path)` for project p: `hidden` (the project rule by default - what a
        session reads), plus everything of the repository outside the project directory (like
        Manager.hidden_for). Not for the GUI's git views (K12: only `.git` is withheld there)."""
        hidden = hidden or self.project_hidden(p)
        proj = os.path.realpath(p["path"])

        def h(rel: str) -> bool:
            r = os.path.relpath(os.path.join(top, rel), proj)
            if r == ".." or r.startswith("../"):
                return True
            return r != "." and (hidden(r) or guard.is_git_path(r))
        return h

    def _in_repo(self, p: dict, full: str) -> tuple[str, str, object]:
        """(trusted root, repo-relative path, the GUI's diff filter) of a path of project p: the
        human owns the whole repository - only `.git` is withheld (K12)."""
        top = self._top(p)
        grel = os.path.relpath(full, top)
        if grel == ".." or grel.startswith("../") or guard.is_git_path(grel):
            raise projectfs.FsError("%s is outside the repository (or .git)" % grel)
        return top, grel, guard.is_git_path

    def _same_tree(self, root: str) -> list:
        """Live project sessions whose checkout is (inside / around) this tree - or whose sandbox mounts a tree
        (inside / around) it: a sibling worktree, a library (2026-10-10, the net row's Restore into <repo>-work)."""
        out = []
        for s in list(self.sessions.values()):
            if s.kind != "project" or s.hidden:
                continue
            rs = [os.path.realpath(s.path)] + [os.path.realpath(c["root"]) for c in s.coverage or []
                                               if isinstance(c, dict) and c.get("kind") != "include"
                                               and isinstance(c.get("root"), str) and c["root"]]
            if any(r == root or root.startswith(r + os.sep) or r.startswith(root + os.sep) for r in rs):
                out.append(s)
        return out

    # ------------------------------------------------------------ files (9.7 #3 #4 #5 #6)
    def fs_blob(self, key: str, path: str, tree: str = "HEAD", orig: str = "") -> dict:
        """One Git file of the project, including metadata; snapshots use their host-only object store.

        The GUI's rule (K12): a file hidden from Claude is the human's too - only `.git` and what lies
        outside the project or repository are refused, for both rename paths before object access. Keep
        the lexical file name after _pfile checks its real target so a symlink blob is target text,
        never target content.
        """
        p, root, rel, full = self._pfile(key, path)
        top, _, _ = self._in_repo(p, full)
        grel = os.path.relpath(os.path.join(root, os.path.normpath(path.strip())), top)
        if grel == ".." or grel.startswith("../") or guard.is_git_path(grel):
            raise projectfs.FsError("%s is outside the repository (or .git)" % grel)
        if orig:
            op, _, _, of = self._pfile(key, orig)
            self._in_repo(op, of)
        env = None
        if tree == "HEAD":
            tree = gitops._git(top, "rev-parse", "--verify", "HEAD^{tree}").strip()
        else:
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree or ""):
                raise ValueError("tree must be HEAD or a tree id")
            if not gitops.is_tree(top, tree):
                for session in self._same_tree(root):
                    if session.project == p["key"] and gitops.is_tree(top, tree, self._snap_objects(session)):
                        env = gitops.snap_env(top, self._snap_objects(session))
                        break
                else:
                    raise ValueError("unknown snapshot tree")
        return dict(gitops.tree_blob(top, tree, grel, env, projectfs.MAX_READ), path=path)

    def fs_marks(self, key: str, path: str) -> dict:
        p, root, rel, full = self._pfile(key, path)
        st = common.safe_stat(root, rel)
        if st.st_size > projectfs.MAX_READ:
            raise projectfs.TooLarge("%s is larger than 2 MiB - no change marks" % path)
        out = {"path": os.path.normpath(path), "added": [], "changed": [], "deleted": [], "untracked": False}
        try:
            top, grel, _ = self._in_repo(p, full)
        except gitops.GitError:
            return out   # not in a repository

        def nlines() -> int:
            raw = common.safe_read(root, rel, projectfs.MAX_READ + 1)
            return 0 if b"\0" in raw[:8192] else len(projectfs.split_lines(raw.decode("utf-8", "replace")))
        try:
            return dict(out, **gitops.file_marks(top, grel, nlines))
        except gitops.GitError:
            return out

    def fs_symbols(self, key: str, name: str = "", q: str = "") -> dict:
        p = self.gui_project(key)
        idx = symbols.index_for(p["key"])
        if time.time() - idx.checked >= symbols.TTL:
            idx.refresh(p["path"], self.fs(p["key"], "files", {})["files"])
        return idx.query(name, q)

    def fs_download(self, key: str, path: str) -> bytes:
        """GUI-only artifact download: project file, no .git, no symlink traversal, bounded to 10 MiB."""
        _, root, rel, _ = self._pfile(key, path)
        info = common.safe_stat(root, rel)
        if not stat.S_ISREG(info.st_mode):
            raise projectfs.FsError("a regular project file is required")
        if info.st_size > RAW_MAX:
            raise projectfs.TooLarge("artifact is larger than 10 MiB")
        data = common.safe_read(root, rel, RAW_MAX + 1)
        if len(data) > RAW_MAX:
            raise projectfs.TooLarge("artifact is larger than 10 MiB")
        return data

    def fs_raw(self, key: str, path: str) -> tuple[str, bytes]:
        p, root, rel, full = self._pfile(key, path)
        ctype = RAW_TYPES.get(os.path.splitext(rel)[1].lower())
        if ctype is None:
            raise ValueError("%s is not an image (png jpg gif webp svg ico bmp)" % path)
        if common.safe_stat(root, rel).st_size > RAW_MAX:
            raise ValueError("%s is larger than 10 MiB" % path)
        data = common.safe_read(root, rel, RAW_MAX + 1)
        if len(data) > RAW_MAX:
            raise ValueError("%s is larger than 10 MiB" % path)
        return ctype, data

    def fs_write(self, key: str, path: str, text: str, sha: str, exact: bool = False) -> dict:
        """Save from the GUI (9.7 #6): an existing, regular file only, never through a symlink or into
        `.git`; refused when it changed since it was read (sha). Allowed while a session of the tree works
        (2026-10-07, audited `while_working`) - best-effort: the sha lock catches a change made before the save, and Claude Code's
        Edit refuses a file changed since its Read - but a whole-file write by the agent after the save
        (Claude Code's Write, a `sed -i` in Bash, Codex's apply_patch) can still replace it. A visible
        file is replaced atomically; one marked hidden is rewritten in place with a redo journal
        (common.overwrite_in_place), preserving any intact over-mount. Missing or stale sandbox coverage
        never blocks the human's save (2026-10-23); it is not proof that the file is unreadable to the agent.
        Keeps the file's mode. exact (review): `text` is the whole file, line endings included."""
        p = self.gui_project(key)   # an unknown project first: 404, not a complaint about the body
        if not isinstance(text, str):
            raise ValueError("text is required")
        if not isinstance(sha, str) or not re.match(r"^[0-9a-fA-F]{64}$", sha):
            raise ValueError("sha must be the sha256 (64 hex digits) of the file as it was read")
        if len(text) > WRITE_MAX:
            raise ValueError("text larger than 2 MiB")
        rule = self.project_hidden(p)
        full, rel = projectfs._resolve(p["path"], str(path or ""), guard.is_git_path)
        if not rel:
            raise projectfs.FsError("a file path is required")
        if os.path.normpath(str(path)) != rel:
            raise ValueError("%s goes through a symbolic link - edit the file it points to" % path)
        root = os.path.realpath(p["path"])
        with self._write_lock:
            try:
                st = common.safe_stat(root, rel)   # lstat: a symlink, a dir or a device is refused
            except FileNotFoundError:
                raise ValueError("%s does not exist (the GUI edits existing files only)" % path) from None
            if st.st_size > WRITE_MAX:
                raise ValueError("%s is larger than 2 MiB" % path)
            cur = common.safe_read(root, rel, WRITE_MAX + 1)
            if hashlib.sha256(cur).hexdigest() != sha.lower():
                raise ValueError("%s changed since you opened it - reload it" % path)
            # the GUI edits lines joined by \n (no final newline); the file keeps its own
            # line ending and final-newline state, detected from its current bytes
            eol, final = projectfs.eol_info(cur)
            body = text.replace("\r\n", "\n") if exact else text.replace("\r\n", "\n") + ("\n" if final else "")
            data = text.encode("utf-8") if exact else (body.replace("\n", "\r\n") if eol == "\r\n" else body).encode("utf-8")
            if len(data) > WRITE_MAX:
                raise ValueError("text larger than 2 MiB")
            busy = [s.id for s in self._same_tree(root) if s.state == "working"]
            if rule(rel):   # no tmp file next to it, no rename over its over-mount; crash-safe by the journal
                common.overwrite_in_place(root, rel, data, cur, mode=st.st_mode & 0o7777, exact_mode=True)
            else:
                common.safe_write(root, rel, data, mode=st.st_mode & 0o7777, exact_mode=True)
        audit({"event": "gui-write", "project": p["key"], "path": rel, "bytes": len(data), "hidden": bool(rule(rel)),
               "while_working": busy})
        self._files_cache.pop(p["key"], None)
        symbols.index_for(p["key"]).invalidate(rel)
        return {"sha": hashlib.sha256(data).hexdigest(), "total_lines": len(projectfs.split_lines(body))}

    # ------------------------------------------------------------ file operations
    # create / rename / delete / restore from the GUI's tree, under fs_write's rules: a path
    # inside the checkout spelled exactly as on disk (APFS would resolve CONFIG to config/),
    # no symlink in any component (every step O_NOFOLLOW from a directory fd), never `.git` -
    # for a directory not one entry inside it, at the source AND at the destination. A session of the
    # tree may work meanwhile (2026-10-08): only a path its agent edited in the current turn is refused
    # (_ops_busy). Files hidden from Claude are the GUI's too, but their
    # isolation coverage never blocks human Files operations. Delete only moves into
    # $PIPER_HOME/trash/<id>/ (restore moves it back). Audited, names only.
    @staticmethod
    def _ops_parts(path, hidden) -> list[str]:
        raw = str(path if path is not None else "")
        if not raw or "\0" in raw or raw.startswith("/"):
            raise projectfs.FsError("a path relative to the project is required")
        parts = raw.split("/")
        for seg in parts:
            if seg in ("", ".", ".."):
                raise projectfs.FsError("%s: empty, '.' or '..' path segment" % raw)
            if len(seg.encode("utf-8", "surrogatepass")) > NAME_MAX:
                raise ValueError("%s: a name longer than %d bytes" % (raw, NAME_MAX))
        for i in range(1, len(parts) + 1):   # an excluded directory covers what is below it
            r = "/".join(parts[:i])
            if hidden(r) or guard.is_git_path(r):
                raise projectfs.FsError("%s is inside .git" % raw)
        return parts

    @staticmethod
    def _ops_dir(root: str, parts: list[str]) -> int:
        """A directory fd of root/parts: each component exactly as listed (no case variant),
        never through a symlink. FsError (404) when it is missing or not a plain directory."""
        fd = os.open(root, _DIRFLAGS)
        try:
            for seg in parts:
                if seg not in os.listdir(fd):
                    raise projectfs.FsError("%s does not exist" % "/".join(parts))
                try:
                    nfd = os.open(seg, _DIRFLAGS, dir_fd=fd)
                except OSError:
                    raise projectfs.FsError("%s is not a directory (or a symbolic link)" % "/".join(parts)) from None
                os.close(fd)
                fd = nfd
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _ops_lstat(dfd: int, name: str):
        """lstat of an entry (any letter case APFS resolves), None when there is none."""
        try:
            return os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    @staticmethod
    def _ops_tree(dfd: int, name: str, src: str, dst: str | None, hidden) -> list[tuple[str, str | None]]:
        """A directory to move: refused when any entry inside is `.git` where it is now or where
        it would land; the (source, destination) pairs of its entries otherwise (_ops_guard).
        Bounded: a tree too large to check is refused (fail closed)."""
        n, pairs = 0, []
        for dirpath, dirnames, filenames, _ in os.fwalk(name, dir_fd=dfd, follow_symlinks=False):
            sub = os.path.relpath(dirpath, name)
            sub = "" if sub == "." else sub + "/"
            for e in dirnames + filenames:
                n += 1
                if n > projectfs.MAX_FILES:
                    raise ValueError("%s holds more than %d entries" % (src, projectfs.MAX_FILES))
                pair = (src + "/" + sub + e, dst + "/" + sub + e if dst else None)
                for r in pair:
                    if r and (hidden(r) or guard.is_git_path(r)):
                        raise projectfs.FsError("%s contains a .git entry" % src)
                pairs.append(pair)
        return pairs

    # the states in which a sandbox of the tree exists (or is about to): its over-mounts were fixed at the start
    SANDBOX_STATES = ("preflight", "preparing", "spawning", "trust", "starting", "attesting", "ready", "working",
                      "waiting", "attention", "stopping")

    # a start before hole runs: s.coverage may still be the previous run's (_start resets it only when
    # its preflight returns), and this run's over-mounts are not fixed yet - nothing about them is known
    PRE_SANDBOX_STATES = ("preflight", "preparing", "spawning")

    def _hidden_write_guard(self, root: str, rel: str) -> None:
        """Non-Files writes of hidden content (HTTP/property helpers): refused while a sandbox of
        the tree sees it - it appeared after that sandbox started (L5), so no over-mount covers it - and
        while a session's isolation check has not finished (which files it over-mounts is not known yet;
        _ops_guard refuses the same)."""
        for s in self._same_tree(root):
            if s.state not in self.SANDBOX_STATES:
                continue
            covered, _, known = self._box_view(s, root)
            if not known:
                raise ValueError("%s is hidden from Claude, but the isolation check of session \"%s\" has not "
                                 "finished - Piper cannot tell yet whether it sees the file; not written. Retry "
                                 "once the session is ready, or stop it first" % (rel, s.title))
            if not covered(rel):
                raise ValueError("%s is hidden from Claude, but session \"%s\" sees it (it is not over-mounted "
                                 "there) - not written; stop the session first" % (rel, s.title))

    def _box_view(self, s, root: str):
        """(covered, shielded, known) for project-relative paths of tree `root` in the sandbox of session s:
        covered = the sandbox does not see its content now (an over-mount at it or above it); shielded =
        strictly inside an over-mounted DIRECTORY - whatever happens there on the host stays invisible to
        the sandbox (the mountpoint is that directory, not the entries). Only INTACT over-mounts count
        (guard.intact_overmounts): not detached by a GUI operation, and the host entry at the path is the
        one pinned at the start - a sandbox can move a mountpoint's parent away (the mount goes with it)
        and create a plain directory at the old path. No pinned identities (a session started before
        they were recorded): nothing counts. known: False before the preflight recorded the coverage, and
        for a start that has not run hole yet (PRE_SANDBOX_STATES) - a stale coverage of the previous run
        says nothing about the coming sandbox."""
        roots = []
        for c in ([] if s.state in self.PRE_SANDBOX_STATES else s.coverage or []):
            if c.get("kind") == "include" or not c.get("root"):
                continue
            roots.append((os.path.realpath(c["root"]), guard.intact_overmounts(c) or set()))

        def rels(rel: str):
            for croot, over in roots:
                r = os.path.relpath(os.path.join(root, rel), croot)
                if not (r == ".." or r.startswith("../")):
                    yield croot, over, r

        def covered(rel: str) -> bool:
            return any(guard.covered(r, over) for _, over, r in rels(rel))

        def shielded(rel: str) -> bool:   # intact: the very directory pinned at the start (no link, no stand-in)
            return any(r.startswith(o + "/") and os.path.isdir(os.path.join(croot, o))
                       and not os.path.islink(os.path.join(croot, o))
                       for croot, over, r in rels(rel) for o in over)
        return covered, shielded, bool(roots)

    def _ops_guard(self, p: dict, root: str, pairs: list[tuple[str | None, str | None]]) -> list:
        """Guard non-Files writes against exposing hidden content to a live session.
        Manual Files operations use _ops_detachments directly. `pairs`: (from, to) of every entry;
        from None = new content (create,
        restore), to None = gone (delete). Refused:
          - content hidden from Claude moved to a path the isolation rule does not hide (any time);
          - hidden content (new, or covered in a running sandbox) landing on a hidden path that sandbox
            does not shield: over-mounts are fixed at the start, only an over-mounted DIRECTORY hides
            what appears inside it later.
        Deleting is always fine: the content goes to Piper's trash. Returns the (session, coverage root,
        mountpoint) over-mounts the operation detaches (a mountpoint deleted or moved away - on Linux its
        mount goes with it), for _ops_detached once it is done. An unreadable isolation policy (S2): no
        move, and nothing new while a sandbox of the tree exists - which files are hidden is unknown."""
        rule = self.project_hidden(p)
        why = getattr(rule, "unreadable", "")
        live = [s for s in self._same_tree(root) if s.state in self.SANDBOX_STATES]
        moves = any(frm is not None and to is not None for frm, to in pairs)
        if why and (moves or live and any(to is not None for _, to in pairs)):
            raise ValueError("isolation policy unreadable (%s) - Piper cannot tell which files are hidden from "
                             "Claude, so it does not %s now" % (why, "move files" if moves
                                                                  else "add files while a session runs"))
        for frm, to in pairs:
            if frm is not None and to is not None and rule(frm) and not rule(to):
                raise ValueError("%s is hidden from Claude - at %s it would be shown to every session, the future "
                                 "ones too (the exclude does not match the new path)" % (frm, to))
        for s in live:
            covered, shielded, known = self._box_view(s, root)
            for frm, to in pairs:
                if to is not None and rule(to) and (frm is None or covered(frm)) and not shielded(to):
                    why = ("its isolation check has not finished" if not known else
                           "its over-mounts were fixed at its start and %s is not inside an over-mounted directory" % to)
                    raise ValueError("%s is hidden from Claude, but session \"%s\" would see it there - %s. Stop the "
                                     "session first, or exclude the whole directory" % (to, s.title, why))
        return self._ops_detachments(root, pairs)

    def _ops_detachments(self, root: str, pairs: list[tuple[str | None, str | None]]) -> list:
        """Track mounts moved by human Files operations without enforcing sandbox isolation on the human.

        Isolation warnings and agent-side guards remain separate from manual filesystem access.
        """
        detach = []
        for s in self._same_tree(root):
            if s.state not in self.SANDBOX_STATES:
                continue
            for frm, _ in pairs:
                if frm is None:
                    continue
                for c in s.coverage or []:
                    if c.get("kind") == "include" or not c.get("root"):
                        continue
                    r = os.path.relpath(os.path.join(root, frm), os.path.realpath(c["root"]))
                    if r in (guard.intact_overmounts(c) or set()):
                        detach.append((s, c, r))
        return detach

    def _ops_pinned(self, root: str, rel: str, dfd: int, fst=None, need=None) -> None:
        """N10: content hidden from Claude is written / moved through `dfd` (rel's directory) - and `fst`, the
        fstat of the opened file - which were opened by path, apart from the path checks of _ops_guard /
        _hidden_write_guard. A sandbox that swaps a stand-in in (moves the mount's parent, mkdir) and the real
        directory back around those checks would get it where it reads it. So for every live sandbox of the
        tree the fds themselves must be shielded: dfd is (or lies below) an over-mounted directory reached from
        its pinned identity (gitops._shield_fd, O_NOFOLLOW descent), or fst is the over-mounted FILE pinned at
        the start (an existing file rewritten in place). need(s) False: s needs no shield for this operation.
        Raises common.NotPinned (a ValueError) - fail closed."""
        st = os.fstat(dfd)
        for s in self._same_tree(root):
            if s.state not in self.SANDBOX_STATES or need is not None and not need(s):
                continue
            ok = False
            for c in [] if s.state in self.PRE_SANDBOX_STATES else s.coverage or []:
                if ok or c.get("kind") == "include" or not c.get("root") or not isinstance(c.get("ids"), dict):
                    continue
                croot = os.path.realpath(c["root"])
                r = os.path.relpath(os.path.join(root, rel), croot)
                for o in guard.intact_overmounts(c) or set():
                    ident = c["ids"].get(o)
                    if ok or not isinstance(ident, list):
                        continue
                    if r == o:   # the over-mounted file itself, as opened
                        ok = fst is not None and [fst.st_dev, fst.st_ino] == ident
                        continue
                    if not r.startswith(o + "/"):
                        continue
                    try:
                        fd = gitops._shield_fd(croot, o, ident)
                        try:
                            for part in common._parts(os.path.relpath(os.path.dirname(r), o)):
                                nfd = os.open(part, _DIRFLAGS, dir_fd=fd)
                                os.close(fd)
                                fd = nfd
                            pin = os.fstat(fd)
                        finally:
                            os.close(fd)
                        ok = (pin.st_dev, pin.st_ino) == (st.st_dev, st.st_ino)
                    except OSError:
                        pass
            if not ok:
                raise common.NotPinned("%s is hidden from Claude, but session \"%s\" would see it there - the "
                                       "over-mounted directory changed while Piper looked. Stop the session first"
                                       % (rel, s.title))

    def _ops_pin(self, root: str, rel: str):
        """The `pin` of common.safe_write / overwrite_in_place for hidden `rel` (_ops_pinned on the write's fds)."""
        return lambda dfd, fd: self._ops_pinned(root, rel, dfd, None if fd is None else os.fstat(fd))

    def _ops_detached(self, detach: list) -> None:
        """Record over-mounts an operation removed: L5 no longer counts them as covering, and nothing
        is shielded by them any more."""
        for s, c, r in detach:
            c["detached"] = sorted(set(c.get("detached") or []) | {r})
            audit({"event": "overmount-detached", "session": s.id, "path": os.path.join(c["root"], r)})
            s.save()

    @staticmethod
    def _ops_held(detach: list) -> None:
        """Docker Desktop (macOS) refuses to move a directory a running container over-mounts (EACCES):
        say so instead of a bare "Permission denied"."""
        if detach:
            s, c, r = detach[0]
            raise ValueError("%s is over-mounted by the sandbox of session \"%s\" - the host refuses to move it "
                             "while that runs; stop the session first" % (r, s.title)) from None

    def _ops_busy(self, root: str, paths: list[str]) -> None:
        """A session of the tree may work (2026-10-08): refused only when its agent edited one of these
        paths (or a file below one) in the current turn - the operation would pull it from under Claude."""
        for s in self._same_tree(root):
            if s.state != "working":
                continue
            base = os.path.realpath(s.path)
            mine = {os.path.relpath(os.path.join(root, x), base) for x in paths if x}
            for e in list(s.fsedits):
                if e.get("ts", 0) < s.since:
                    continue
                hit = next((x for x in mine if e["path"] == x or e["path"].startswith(x + "/")), None)
                if hit is not None:
                    raise ValueError("Claude edited %s in its current turn (session \"%s\") - try again when the "
                                     "turn ends" % (e["path"], s.title))

    def _ops_done(self, p: dict) -> None:
        self._files_cache.pop(p["key"], None)
        symbols.index_for(p["key"]).invalidate()

    def fs_create(self, key: str, path: str, kind: str, text: str | None = None, *, mode: int | None = None) -> dict:
        """Create without replacing an existing path; review restoration may retain a Git file mode."""
        p = self.gui_project(key)
        if kind not in ("file", "dir"):
            raise ValueError("kind must be file or dir")
        if text is not None and (kind != "file" or not isinstance(text, str)):
            raise ValueError("text must be a string (files only)")
        if mode is not None and (kind != "file" or mode not in (0o644, 0o755)):
            raise ValueError("restored file mode must be 644 or 755")
        data = (text or "").encode("utf-8")
        if len(data) > WRITE_MAX:
            raise ValueError("text larger than 2 MiB")
        parts = self._ops_parts(path, guard.is_git_path)
        rel, name = "/".join(parts), parts[-1]
        root = os.path.realpath(p["path"])
        with self._write_lock:
            self._ops_busy(root, [rel])
            dfd = self._ops_dir(root, parts[:-1])
            try:
                if self._ops_lstat(dfd, name) is not None:
                    raise ValueError("%s already exists" % rel)
                if kind == "dir":
                    try:
                        os.mkdir(name, 0o755, dir_fd=dfd)
                    except FileExistsError:
                        raise ValueError("%s already exists" % rel) from None
                else:   # never over an existing file; a hidden one without a tmp file next to it (safe_write)
                    try:
                        keep = {"mode": mode, "exact_mode": True} if mode is not None else {}
                        if self.project_hidden(p)(rel):
                            common.overwrite_in_place(root, rel, data, b"", create=True, **keep)
                        else:
                            common.safe_write(root, rel, data, create=True, **keep)
                    except FileExistsError:
                        raise ValueError("%s already exists" % rel) from None
            finally:
                os.close(dfd)
        audit({"event": "gui-create", "project": p["key"], "path": rel, "kind": kind,
               "hidden": bool(self.project_hidden(p)(rel))})
        self._ops_done(p)
        out = {"path": rel}
        if kind == "file":
            out["sha"] = hashlib.sha256(data).hexdigest()
        return out

    def fs_scratch(self, key: str, ext: str) -> dict:
        """A new scratch file of type `ext` in the checkout's development/scratch (made when missing),
        kept out of git: info/exclude gets the folder unless git already ignores it. fs_create's rules apply."""
        if ext not in SCRATCH_TYPES:
            raise ValueError("unknown scratch type %r" % ext)
        p = self.gui_project(key)
        root = os.path.realpath(p["path"])
        top = self.repo_top(root)
        if top:   # never committed; a subdirectory project anchors the pattern at its own folder
            sub = os.path.relpath(root, top).replace(os.sep, "/")
            gitops.ensure_excluded(top, "/" + ("" if sub == "." else sub + "/") + SCRATCH_DIR + "/",
                                   "scratch files (Files > New scratch file)")
        here = ""
        for part in SCRATCH_DIR.split("/"):
            here = here + "/" + part if here else part
            if os.path.islink(os.path.join(root, here)):
                raise ValueError("%s is a symbolic link - Piper does not create scratch files through it" % here)
            if not os.path.isdir(os.path.join(root, here)):
                self.fs_create(key, here, "dir")
        taken = os.listdir(os.path.join(root, SCRATCH_DIR))
        return self.fs_create(key, SCRATCH_DIR + "/" + scratch_name(taken, ext), "file", SCRATCH_TYPES[ext])

    def fs_rename(self, key: str, src: str, dst: str) -> dict:
        """Rename or move a file or directory into an existing visible directory; the
        destination must not exist (a change of letter case only is allowed)."""
        p = self.gui_project(key)
        hidden = guard.is_git_path
        sp, dp = self._ops_parts(src, hidden), self._ops_parts(dst, hidden)
        srel, drel = "/".join(sp), "/".join(dp)
        if srel == drel:
            raise ValueError("the new name is the same")
        if drel.startswith(srel + "/"):
            raise ValueError("%s cannot move into itself" % srel)
        root = os.path.realpath(p["path"])
        detach = []
        with self._write_lock:
            self._ops_busy(root, [srel, drel])
            sfd = self._ops_dir(root, sp[:-1])
            try:
                dfd = self._ops_dir(root, dp[:-1])
                try:
                    if sp[-1] not in os.listdir(sfd):
                        raise projectfs.FsError("%s does not exist" % srel)
                    st = os.stat(sp[-1], dir_fd=sfd, follow_symlinks=False)
                    link = stat.S_ISLNK(st.st_mode)   # the link itself moves, its target stays (K13)
                    have = self._ops_lstat(dfd, dp[-1])
                    case_only = have is not None and os.path.samestat(have, st) and sp[:-1] == dp[:-1]
                    if have is not None and not case_only:
                        raise ValueError("%s already exists" % drel)
                    is_dir = stat.S_ISDIR(st.st_mode)
                    pairs = [(srel, drel)]
                    if is_dir:
                        pairs += self._ops_tree(sfd, sp[-1], srel, drel, hidden)
                    elif not (stat.S_ISREG(st.st_mode) or link):
                        raise ValueError("%s is not a regular file, directory or symbolic link" % srel)
                    detach = self._ops_detachments(root, pairs)
                    try:
                        if is_dir or case_only or link:   # link + unlink of a symlink is not portable (macOS follows it)
                            os.rename(sp[-1], dp[-1], src_dir_fd=sfd, dst_dir_fd=dfd)
                        else:   # a file: link + unlink never replaces what appeared meanwhile
                            os.link(sp[-1], dp[-1], src_dir_fd=sfd, dst_dir_fd=dfd, follow_symlinks=False)
                            os.unlink(sp[-1], dir_fd=sfd)
                    except FileExistsError:
                        raise ValueError("%s already exists" % drel) from None
                    except PermissionError:
                        self._ops_held(detach)
                        raise
                finally:
                    os.close(dfd)
            finally:
                os.close(sfd)
        self._ops_detached(detach)
        rule = self.project_hidden(p)
        audit({"event": "gui-rename", "project": p["key"], "from": srel, "to": drel,
               "hidden": bool(rule(srel) or rule(drel)), "hidden_from": bool(rule(srel)), "hidden_to": bool(rule(drel))})
        self._ops_done(p)
        return {"from": srel, "to": drel}

    def fs_delete(self, key: str, path: str, *, expected_sha: str | None = None) -> dict:
        """Move into trash; review deletion additionally checks a regular file's raw SHA under the write lock."""
        if expected_sha is not None and (not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha)):
            raise ValueError("sha must be the sha256 (64 hex digits) of the file as it was read")
        p = self.gui_project(key)
        hidden = guard.is_git_path
        parts = self._ops_parts(path, hidden)
        rel = "/".join(parts)
        root = os.path.realpath(p["path"])
        self._trash_prune()
        tid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        tdir = os.path.join(TRASH_DIR, tid)
        with self._write_lock:
            self._ops_busy(root, [rel])
            dfd = self._ops_dir(root, parts[:-1])
            try:
                if parts[-1] not in os.listdir(dfd):
                    raise projectfs.FsError("%s does not exist" % rel)
                st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
                gone = [rel]
                if stat.S_ISDIR(st.st_mode):
                    gone += [frm for frm, _ in self._ops_tree(dfd, parts[-1], rel, None, hidden)]
                elif not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):   # a link: the link, never its target
                    raise ValueError("%s is not a regular file, directory or symbolic link" % rel)
                if expected_sha is not None:
                    if not stat.S_ISREG(st.st_mode):
                        raise ValueError("%s is not a regular file" % rel)
                    if st.st_size > WRITE_MAX:
                        raise ValueError("%s is larger than 2 MiB" % rel)
                    current = common.safe_read(root, rel, WRITE_MAX + 1)
                    if len(current) > WRITE_MAX or hashlib.sha256(current).hexdigest() != expected_sha.lower():
                        raise ValueError("%s changed since you opened it - reload it" % rel)
                detach = self._ops_detachments(root, [(x, None) for x in gone])
                os.makedirs(TRASH_DIR, mode=0o700, exist_ok=True)
                os.mkdir(tdir, 0o700)
                try:
                    os.rename(parts[-1], os.path.join(tdir, "item"), src_dir_fd=dfd)
                except OSError as e:
                    shutil.rmtree(tdir, ignore_errors=True)
                    if isinstance(e, PermissionError):
                        self._ops_held(detach)
                    if e.errno == errno.EXDEV:
                        raise ValueError("%s is on another volume than %s - not moved to the trash"
                                         % (rel, TRASH_DIR)) from None
                    raise
                common.write_json(os.path.join(tdir, "meta.json"),
                                  {"project": p["key"], "path": rel, "at": time.time(),
                                   "kind": "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file"})
            finally:
                os.close(dfd)
        self._ops_detached(detach)
        audit({"event": "gui-delete", "project": p["key"], "path": rel, "trash": tid,
               "hidden": bool(self.project_hidden(p)(rel))})
        self._ops_done(p)
        return {"trash": tid}

    def fs_restore(self, key: str, trash: str, replace: bool = False) -> dict:
        """Move a trashed entry back to its path - when that is free and still allowed. replace (2026-10-10, the
        net-failed queue row): the original the git_run net could not put back goes OVER the file at its path -
        only a regular file, rewritten in place like fs_write's hidden save (redo
        journal, no sandbox-coverage requirement); a free path is restored as without replace."""
        p = self.gui_project(key)
        if not isinstance(trash, str) or not TRASH_ID.match(trash):
            raise projectfs.FsError("unknown trash entry")
        tdir = os.path.join(TRASH_DIR, trash)
        meta = common.read_json(os.path.join(tdir, "meta.json"), None) if os.path.isdir(tdir) else None
        if not isinstance(meta, dict) or meta.get("project") != p["key"] or \
                not os.path.lexists(os.path.join(tdir, "item")):
            raise projectfs.FsError("unknown trash entry")
        hidden = guard.is_git_path
        root, parts = self._trash_target(p, meta, hidden)
        rel = "/".join(parts)
        if replace:
            done = self._restore_over(p, root, rel, tdir, trash)
            if done is not None:
                return done
        with self._write_lock:
            self._ops_busy(root, [rel])
            dfd = self._ops_dir(root, parts[:-1])
            try:
                if self._ops_lstat(dfd, parts[-1]) is not None:
                    raise ValueError("%s exists again - rename or delete it first" % rel)
                tfd = os.open(tdir, _DIRFLAGS)
                try:
                    st = os.stat("item", dir_fd=tfd, follow_symlinks=False)
                    pairs = [(None, rel)]   # back where it was: new content for a running sandbox
                    if stat.S_ISDIR(st.st_mode):
                        pairs += [(None, to) for _, to in self._ops_tree(tfd, "item", rel, rel, hidden)]
                    elif not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                        raise ValueError("the trash entry is not a regular file, directory or symbolic link")
                    try:
                        os.rename("item", parts[-1], src_dir_fd=tfd, dst_dir_fd=dfd)
                    except OSError as e:
                        if e.errno == errno.EXDEV:
                            raise ValueError("the trash is on another volume than %s" % rel) from None
                        raise
                finally:
                    os.close(tfd)
            finally:
                os.close(dfd)
        shutil.rmtree(tdir, ignore_errors=True)
        audit({"event": "gui-restore", "project": p["key"], "path": rel, "trash": trash,
               "hidden": bool(self.project_hidden(p)(rel))})
        self._ops_done(p)
        if hasattr(self, "feed_net_restored"):
            self.feed_net_restored(trash)
        return {"path": rel}

    def _trash_target(self, p: dict, meta: dict, hidden) -> tuple[str, list[str]]:
        """(tree root, path parts) a trash entry goes back to. An entry of the git_run net records the tree it ran
        on (`root` + `rel`): inside the project that is the project's own path; a linked sibling worktree
        (<repo>-work, git_run -C) is that tree - only while it is a trusted work tree of the project's repository
        (repo_top recorded and unchanged, the same common dir, beside the project's checkout). Anything else: refused."""
        proj = os.path.realpath(p["path"])
        tree, trel = meta.get("root"), meta.get("rel")
        if not isinstance(tree, str) or not isinstance(trel, str) or not tree:
            return proj, self._ops_parts(meta.get("path"), hidden)
        tree = os.path.realpath(tree)
        full = os.path.normpath(os.path.join(tree, trel))
        if full == proj or full.startswith(proj + os.sep):   # in the project's own checkout
            return proj, self._ops_parts(os.path.relpath(full, proj), hidden)
        repo_top = getattr(self, "repo_top", None)
        top = repo_top(proj, record="") if repo_top else None
        ptop = os.path.realpath(top) if top else ""
        if not ptop or tree == ptop or os.path.dirname(tree) != os.path.dirname(ptop) or \
                (repo_top(tree, record="") or "") != tree:
            raise ValueError("the trash entry belongs to %s, which is not a trusted work tree of %s now - not restored; "
                             "it stays in the trash" % (tree, p["key"]))
        with self._roots_lock:
            roots = self._roots or {}
            same = isinstance(roots.get(tree), dict) and isinstance(roots.get(ptop), dict) and \
                roots[tree].get("common") and roots[tree].get("common") == roots[ptop].get("common")
        if not same:
            raise ValueError("the trash entry belongs to %s, which is not a work tree of %s's repository - not "
                             "restored; it stays in the trash" % (tree, p["key"]))
        return tree, self._ops_parts(trel, hidden)

    def _restore_over(self, p: dict, root: str, rel: str, tdir: str, trash: str) -> dict | None:
        """fs_restore(replace): the trash item's bytes over the existing file at rel, in place. None: nothing is
        at rel - the caller restores as usual. Both item and target must be regular files.
        Isolation policy and sandbox coverage do not block a manual restore."""
        with self._write_lock:
            self._ops_busy(root, [rel])
            try:
                st = common.safe_stat(root, rel)
            except FileNotFoundError:
                return None
            except common.UnsafePath:   # a directory, a symlink, a device
                raise ValueError("%s is not a regular file - rename or delete it first" % rel) from None
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("%s is not a regular file - rename or delete it first" % rel)
            try:
                tfd = os.open(tdir, _DIRFLAGS)
            except FileNotFoundError:   # restored meanwhile (a second click)
                raise projectfs.FsError("unknown trash entry") from None
            try:
                try:
                    fd = os.open("item", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=tfd)
                except OSError:
                    raise ValueError("the trash entry is not a regular file") from None
                try:
                    ist = os.fstat(fd)
                    if not stat.S_ISREG(ist.st_mode) or ist.st_size > RAW_MAX:
                        raise ValueError("the trash entry is not a regular file of at most 10 MiB")
                    chunks = []
                    while True:
                        b = os.read(fd, 1 << 20)
                        if not b:
                            break
                        chunks.append(b)
                    data = b"".join(chunks)
                finally:
                    os.close(fd)
            finally:
                os.close(tfd)
            cur = common.safe_read(root, rel, RAW_MAX + 1)
            common.overwrite_in_place(root, rel, data, cur, mode=st.st_mode & 0o7777, exact_mode=True)
        shutil.rmtree(tdir, ignore_errors=True)
        audit({"event": "gui-restore", "project": p["key"], "path": rel, "trash": trash,
               "hidden": bool(self.project_hidden(p)(rel)),
               "replace": True, "bytes": len(data),
               **({"tree": root} if root != os.path.realpath(p["path"]) else {})})
        self._files_cache.pop(p["key"], None)
        symbols.index_for(p["key"]).invalidate(rel)
        self._ops_done(p)
        if hasattr(self, "feed_net_restored"):
            self.feed_net_restored(trash)
        return {"path": rel, "replaced": True}

    @staticmethod
    def _trash_prune(now: float | None = None) -> None:
        """Opportunistic: trash entries older than TRASH_DAYS (by meta.json "at") are removed."""
        now = time.time() if now is None else now
        try:
            names = os.listdir(TRASH_DIR)
        except OSError:
            return
        for n in names:
            if not TRASH_ID.match(n):
                continue
            d = os.path.join(TRASH_DIR, n)
            meta = common.read_json(os.path.join(d, "meta.json"), None)
            at = meta.get("at") if isinstance(meta, dict) else None
            if not isinstance(at, (int, float)):
                try:
                    at = os.lstat(d).st_mtime
                except OSError:
                    continue
            if now - at > TRASH_DAYS * 86400:
                shutil.rmtree(d, ignore_errors=True)

    def agent_write(self, session, key: str, path: str, content: str | None = None,
                    old: str | None = None, new: str | None = None, replace_all: bool = False,
                    confirm=None) -> dict:
        """A project session writes into ANOTHER checkout (unlock "projects_write"): edit_file
        (exact old -> new, like Claude's Edit) or write_file (whole content, creates the file and
        its directories). `confirm(summary, payload) -> bool` shows the exact change on a card
        first; what is written is what was shown (the file changed meanwhile -> refused). Walls
        that stay (isolation, not a filter): not the session's own project, not .git, not a file
        hidden by that project's rule, never through a symlink, not while another session works there."""
        p = self.gui_project(key)
        root = os.path.realpath(p["path"])
        if root == os.path.realpath(session.path):
            raise ValueError("this is your own project - edit it directly in the sandbox")
        if self.policy.get("write_guard") and any(root == r or root.startswith(r + os.sep) or r.startswith(root + os.sep)
                                                  for r in INFRA_ROOTS):
            raise ValueError("%s holds code the host runs (Piper, its extensions) - never written from a session"
                             % p["key"])
        hidden = self.project_hidden(p)
        full, rel = projectfs._resolve(p["path"], str(path or ""), hidden)
        if not rel:
            raise projectfs.FsError("a file path is required")
        if os.path.normpath(str(path)) != rel:
            raise ValueError("%s goes through a symbolic link - write the file it points to" % path)
        parts = rel.split(os.sep)
        if guard.is_git_path(rel):
            raise ValueError("%s: .git is never written from a session" % path)

        def current():
            try:
                return common.safe_stat(root, rel), common.safe_read(root, rel, WRITE_MAX + 1)
            except FileNotFoundError:
                return None, None
        st, cur = current()
        if content is None:   # edit_file
            if cur is None:
                raise ValueError("%s does not exist - use write_file to create it" % path)
            if len(cur) > WRITE_MAX:
                raise ValueError("%s is larger than 2 MiB" % path)
            try:
                text = cur.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("%s is not UTF-8 text - use write_file" % path) from None
            if not old or old == new:
                raise ValueError("old_string must be non-empty and differ from new_string")
            n = text.count(old)
            if n == 0:
                raise ValueError("old_string not found in %s" % path)
            if n > 1 and not replace_all:
                raise ValueError("old_string occurs %d times in %s - make it unique or set replace_all" % (n, path))
            data = text.replace(old, new or "").encode("utf-8")
        else:
            data = str(content).encode("utf-8")
        if len(data) > WRITE_MAX:
            raise ValueError("text larger than 2 MiB")
        if cur is not None and data == cur:
            return {"project": p["key"], "path": rel, "created": False, "bytes": len(data), "unchanged": True}
        self._agent_write_busy(session, root)
        if confirm is not None:
            # a file the host runs on its own or in a build: always a card, even when writes are auto-approved
            flagged = bool(AUTO_RUN & set(parts)) or bool(BUILD_FILES.search(rel.replace(os.sep, "/")))
            if not confirm(self._agent_write_card(session, p["key"], rel, cur, data),
                           {"project": p["key"], "path": rel, "flagged": flagged}):
                raise ValueError("the user declined the change of %s in %s" % (rel, p["key"]))
        with self._write_lock:
            st2, cur2 = current()
            if cur2 != cur:
                raise ValueError("%s changed while the change waited for approval - read it and try again" % path)
            self._agent_write_busy(session, root)
            for i in range(1, len(parts)):   # write_file: missing directories, created without following links
                d = os.sep.join(parts[:i])
                if not common.safe_is_dir(root, d):
                    fd = common._open_dir(root, parts[:i - 1])
                    try:
                        os.mkdir(parts[i - 1], 0o755, dir_fd=fd)
                    finally:
                        os.close(fd)
            common.safe_write(root, rel, data, mode=(st2.st_mode & 0o7777) if st2 else 0o644, exact_mode=bool(st2))
        audit({"event": "session-write", "session": session.id, "project": p["key"], "path": rel,
               "bytes": len(data), "created": st2 is None})
        self._files_cache.pop(p["key"], None)
        symbols.index_for(p["key"]).invalidate(rel)
        return {"project": p["key"], "path": rel, "created": st2 is None, "bytes": len(data)}

    def _agent_write_busy(self, session, root: str) -> None:
        busy = [x for x in self._same_tree(root) if x.state == "working" and x.id != session.id]
        if busy:
            raise ValueError("session \"%s\" is working in that tree - try again when it stops" % busy[0].title)

    @staticmethod
    def _agent_write_card(session, key: str, rel: str, cur: bytes | None, data: bytes) -> str:
        """The card: what changes, as a unified diff (a new file: all of it), and warnings."""
        import difflib
        a = [] if cur is None else cur.decode("utf-8", "replace").splitlines(keepends=True)
        b = data.decode("utf-8", "replace").splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(a, b, "a/" + rel if cur is not None else "/dev/null", "b/" + rel, n=3))
        warn = []
        if AUTO_RUN & set(rel.split(os.sep)):
            warn.append("⚠ a file the host runs on its own (direnv, IDE, hooks, CI)")
        elif BUILD_FILES.search(rel.replace(os.sep, "/")):
            warn.append("⚠ build / executable file - it runs when you or another session use it")
        head = "Session %s wants to %s in project %s:\n%s (%d B)" % (
            session.title, "create a file" if cur is None else "change a file", key, rel, len(data))
        if len(diff) > CARD_DIFF_MAX:
            diff = diff[:CARD_DIFF_MAX] + "\n… preview truncated (the whole diff has %d characters) - the whole content is written\n" % len(diff)
        return "\n".join([head, *warn, "", diff])

    # ------------------------------------------------------------ git history (9.7 #3 #7)
    def git_file_diff(self, key: str, path: str) -> str:
        p, root, rel, full = self._pfile(key, path)
        lexical = os.path.join(root, os.path.normpath(path.strip()))
        if os.path.isdir(full) and not os.path.islink(lexical):
            raise ValueError("%s is a directory" % path)
        top, grel, rh = self._in_repo(p, full)
        # _pfile authorized the target; Git must compare the link itself, not that target.
        grel = os.path.relpath(lexical, top)
        if grel == ".." or grel.startswith("../") or rh(grel):
            raise projectfs.FsError("%s is outside the repository (or .git)" % grel)
        return gitops.file_diff(top, grel, rh, GUI_WITHHELD, symlink_text=True)

    def git_log(self, key: str, path: str = "", n: int = 50) -> dict:
        p = self.gui_project(key)
        if path and os.path.normpath(path) != ".":
            full, _ = projectfs._resolve(p["path"], path, guard.is_git_path)
            top, grel, _ = self._in_repo(p, full)
            return {"commits": gitops.commits(top, n, grel, follow=not os.path.isdir(full))}
        return {"commits": gitops.commits(self._top(p), n)}

    def git_blame(self, key: str, path: str) -> dict:
        p, root, rel, full = self._pfile(key, path)
        st = common.safe_stat(root, rel)
        if st.st_size > BLAME_MAX:
            raise projectfs.TooLarge("%s is larger than 2 MiB - no blame" % path)
        top, grel, _ = self._in_repo(p, full)
        raw = common.safe_read(root, rel, BLAME_MAX + 1)
        if b"\0" in raw[:8192]:
            raise projectfs.Binary("%s looks binary - no blame" % path)
        if not gitops.is_tracked(top, grel):
            return {"lines": [{"line": i, "commit": "", "author": "", "time": 0, "summary": "", "text": t}
                              for i, t in enumerate(projectfs.split_lines(raw.decode("utf-8", "replace")), 1)]}
        return {"lines": gitops.blame(top, grel)}

    def git_graph(self, key: str, n: int = 80) -> dict:
        return {"lines": gitops.graph(self._top(self.gui_project(key)), n)}

    def git_show(self, key: str, commit: str) -> str:
        """A commit of the project's repository - all of it (the human owns the repository, K12);
        only a `.git` path (or an unparseable diff header) is withheld."""
        p = self.gui_project(key)
        return gitops.show(self._top(p), commit, guard.is_git_path, GUI_WITHHELD)

    # ------------------------------------------------------------ Git window (9.25): branches, worktrees, log
    def _git_base(self, p: dict) -> dict:
        """The project whose `.claude/worktrees` holds the worktrees: p itself, or the base of a worktree project."""
        return self.gui_project(p["base"]) if p.get("base") else p

    def _live_in(self, path: str) -> list:
        """Live sessions whose checkout IS this work tree (switch / pull / remove would change it under them)."""
        real = os.path.realpath(path)
        return [s for s in list(self.sessions.values()) if s.kind == "project" and s.path and not s.hidden
                and s.state in self.SANDBOX_STATES and os.path.realpath(s.path) == real]

    def _refuse_live(self, path: str, what: str) -> None:
        live = self._live_in(path)
        if live:
            raise ValueError("not %s: session %s works in this tree - stop it first" % (
                what, ", ".join(s.title or s.id for s in live[:3])))

    def git_branches(self, key: str) -> dict:
        """GET /api/git/branches: gitops.branches + per worktree its project key (a worktree in
        `<base>/.claude/worktrees/<name>` opens as `<base>@<name>`) and the live sessions in it."""
        p = self.gui_project(key)
        top = self._top(p)
        out = gitops.branches(top)
        base = self._git_base(p)
        wroot = os.path.realpath(os.path.join(base["path"], projects.WORKTREES))
        for w in out["worktrees"]:
            name = os.path.basename(w["path"])
            w["key"] = base["key"] if w["path"] == os.path.realpath(base["path"]) else (
                "%s@%s" % (base["key"], name) if os.path.dirname(w["path"]) == wroot else "")
            w["sessions"] = [s.id for s in self._live_in(w["path"])]
        return dict(out, project=p["key"], base=base["key"], root=top)

    def git_branch_op(self, key: str, body: dict) -> dict:
        """POST /api/git/branch {project, action: switch|create|delete, name, start?, checkout?, force?}."""
        p = self.gui_project(key)
        top = self._top(p)
        action, name = str(body.get("action") or ""), str(body.get("name") or "").strip()
        if action == "switch":
            self._refuse_live(top, "switched")
            r = gitops.switch(top, name)
        elif action == "create":
            if body.get("checkout"):
                self._refuse_live(top, "switched")
            r = gitops.create_branch(top, name, str(body.get("start") or "").strip(), bool(body.get("checkout")))
        elif action == "delete":
            r = gitops.delete_branch(top, name, bool(body.get("force")))
        else:
            raise ValueError("unknown branch action %r" % action)
        audit({"event": "git-branch", "project": p["key"], "action": action, "name": name,
               "force": bool(body.get("force")), "done": r.get("deleted", True)})
        self.bump()
        return r

    def git_remote_op(self, key: str, action: str) -> dict:
        """POST /api/git/fetch | /api/git/pull {project}: fetch --all --prune, pull --ff-only."""
        p = self.gui_project(key)
        top = self._top(p)
        if action == "fetch":
            r = gitops.fetch(top)
        elif action == "pull":
            self._refuse_live(top, "pulled")
            r = gitops.pull(top)
        else:
            raise ValueError("unknown git action %r" % action)
        audit({"event": "git-" + action, "project": p["key"]})
        self.bump()
        return r

    @quietwindow.managed("worktree")
    def git_worktree_op(self, key: str, body: dict) -> dict:
        """POST /api/git/worktree {project, action: add|remove, name, branch?, new?, start?}: worktrees live in
        `<base>/.claude/worktrees/<name>` (the repo convention), opened as project `<base>@<name>`."""
        base = self._git_base(self.gui_project(key))
        top = self._top(base)
        action, name = str(body.get("action") or ""), str(body.get("name") or "").strip()
        path = projects.worktree_path(base, name)
        if action == "add":
            # Old sandboxes have per-file wildcard matches only. A new checkout would expose
            # its hidden content through their parent mount before the next watch tick.
            if self.policy.get("candidates"):
                for session in list(self.sessions.values()):
                    if session.hidden or session.kind != "project" or session.state not in self.SANDBOX_STATES:
                        continue
                    views = [c for c in session.coverage or [] if isinstance(c.get("root"), str)
                             and guard._inside(path, os.path.realpath(c["root"]))]
                    possible = [session.path] + list(session.mount_roots or [])
                    if not views and not any(r and guard._inside(path, os.path.realpath(r)) for r in possible):
                        continue   # e.g. a sibling worktree, with no mount of the parent checkout
                    safe = bool(views) and session.state not in self.PRE_SANDBOX_STATES
                    for c in views:   # every alias must hide it, including a broad directory include
                        root = os.path.realpath(c["root"])
                        rel = os.path.relpath(path, root)
                        safe = safe and any(rel.startswith(o + "/") and os.path.isdir(os.path.join(root, o))
                                            and not os.path.islink(os.path.join(root, o))
                                            for o in guard.intact_overmounts(c) or [])
                    if not safe:
                        raise ValueError('not created: restart session "%s" first so its sandbox shields new worktrees' %
                                         (session.title or session.id))
            branch = str(body.get("branch") or name).strip()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            gitops.ensure_excluded(top, "/" + projects.WORKTREES.replace(os.sep, "/") + "/")
            r = gitops.worktree_add(top, path, branch, bool(body.get("new")), str(body.get("start") or "").strip())
            self.forget_root(path)
            if not self.record_worktree_root(base["path"], path):
                raise gitops.GitError("the worktree was created, but its repository could not be verified - "
                                      "Piper does not open it (see the audit log)")
            r["key"] = "%s@%s" % (base["key"], name)
        elif action == "remove":
            wt = projects.worktree(base, name)
            if wt is None:
                raise ValueError("no worktree %r of %s" % (name, base["key"]))
            self._refuse_live(wt["path"], "removed")
            r = gitops.worktree_remove(top, wt["path"])
            self.forget_root(wt["path"])
            self.forget_worktree_stack(wt["path"])
        else:
            raise ValueError("unknown worktree action %r" % action)
        audit({"event": "git-worktree", "project": base["key"], "action": action, "name": name,
               "branch": r.get("branch", "")})
        self.bump()
        return r

    def git_graph_rows(self, key: str, n: int = 300, ref: str = "") -> dict:
        return {"rows": gitops.graph_rows(self._top(self.gui_project(key)), n, ref)}

    def git_commit_detail(self, key: str, commit: str) -> dict:
        return gitops.commit_detail(self._top(self.gui_project(key)), commit, guard.is_git_path)

    def git_blob(self, key: str, commit: str, path: str, side: str) -> dict:
        return gitops.commit_blob(self._top(self.gui_project(key)), commit, path, side, guard.is_git_path)

    # ------------------------------------------------------------ CI + merge requests (9.7 #7)
    def _gl_pick(self, p: dict) -> tuple[str | None, list[str]]:
        if not features.on("gitlab"):
            return None, []
        allowed = self._gl_projects(p)
        own = self.origin_gl(p["path"])
        return (own if own in allowed else (allowed[0] if allowed else None)), allowed

    def _gh_pick(self, p: dict) -> tuple[github.GitHub | None, str | None, list[str]]:
        """W23: (client, scope entry, allowed) of a checkout on GitHub - its origin's repository
        first; (None, None, allowed) while the module is off, without a repository or a `gh` login."""
        if not features.on("github"):
            return None, None, []
        allowed = self._gh_projects(p)
        own = (self.origin_gh(p["path"]) or "").lower()
        entry = next((a for a in allowed if a.lower() == own), allowed[0] if allowed else None)
        if not entry:
            return None, None, allowed
        host, _ = github.split(entry)
        if host not in self.github_hosts():
            return None, None, allowed
        tok = github.token(host)
        return (github.GitHub(host, tok, opener=getattr(self, "github_opener", None)) if tok else None), entry, allowed

    def git_ci(self, key: str) -> dict:
        p = self.gui_project(key)
        forges = (features.on("gitlab"), features.on("github"))
        if not any(forges):
            self._ci_cache.pop(p["key"], None)
            return {"project": p["key"], "pipeline": None, "mr": None}
        hit = self._ci_cache.get(p["key"])
        if hit and len(hit) == 3 and hit[2] == forges and time.time() - hit[0] < CI_TTL:
            return hit[1]
        top = self._top(p)
        branch = gitops.current_branch(top)
        out = {"project": p["key"], "branch": branch, "pipeline": None, "mr": None}
        glp, allowed = self._gl_pick(p)
        tok = gitlab.token()
        if glp and tok and branch and branch != "HEAD":
            out["gitlab"] = glp
            gl = gitlab.GitLab(self.gitlab_host(), tok, opener=self.gitlab_opener)
            try:
                out["pipeline"] = gitlab.latest_pipeline(gl, glp, branch, allowed)
                out["mr"] = gitlab.open_mr(gl, glp, branch, allowed)
            except gitlab.GitLabError as e:
                out["error"] = str(e)
        elif not glp and branch and branch != "HEAD" and self._gh_pick(p)[1]:   # W23: Actions + the open PR
            gh, entry, allowed = self._gh_pick(p)
            out["github"] = entry
            if gh is None:
                out["note"] = "piperd has no GitHub login (gh auth login --hostname %s)" % github.split(entry)[0]
            else:
                try:
                    out["pipeline"] = github.latest_pipeline(gh, github.split(entry)[1], branch, allowed)
                    out["mr"] = github.open_mr(gh, github.split(entry)[1], branch, allowed)
                except gitlab.GitLabError as e:   # GitHubError
                    out["error"] = str(e)
        elif features.on("gitlab") and not tok:
            out["note"] = "piperd has no GitLab token (sh scripts/piper.sh gitlab-token < pat.txt)"
        elif features.on("gitlab") and not glp:
            out["note"] = "the checkout has no GitLab project (projects.json / origin)"
        self._ci_cache[p["key"]] = (time.time(), out, forges)
        return out

    def mr_draft(self, key: str, target: str = "") -> dict:
        p = self.gui_project(key)
        top = self._top(p)
        branch = gitops.current_branch(top)
        target = (target or "").strip()
        if not target:
            glp, allowed = self._gl_pick(p)
            tok = gitlab.token()
            if glp and tok:
                try:
                    gl = gitlab.GitLab(self.gitlab_host(), tok, opener=self.gitlab_opener)
                    target = gitlab.default_branch(gl, glp, allowed) or ""
                except gitlab.GitLabError:
                    target = ""
            elif not glp:   # W23: the GitHub repository's default branch
                gh, entry, allowed = self._gh_pick(p)
                if gh is not None:
                    try:
                        target = github.default_branch(gh, github.split(entry)[1], allowed) or ""
                    except gitlab.GitLabError:
                        target = ""
            if not target:
                target = next((c for c in ("master", "main") if gitops.ref_exists(top, "origin/" + c)
                               or gitops.ref_exists(top, c)), "master")
        gitops.check_branch(top, target)
        base = next((r for r in ("origin/" + target, target) if gitops.ref_exists(top, r)), None)
        subs = gitops.subjects(top, base) if base else []
        return {"project": p["key"], "source": branch, "target": target,
                "title": (subs[0] if subs else branch)[:255],
                "description": "\n".join("- " + s for s in subs)}

    def create_mr(self, key: str, title: str, description: str, target: str, draft: bool) -> dict:
        p = self.gui_project(key)
        top = self._top(p)
        branch = gitops.current_branch(top)
        up = gitops.upstream(top)
        if not up:
            raise ValueError("push first: %s has no upstream branch" % branch)
        ahead = gitops.status(top)["ahead"]
        if ahead:
            raise ValueError("push first: %d commit(s) of %s are not pushed" % (ahead, branch))
        gitops.check_branch(top, target)
        source = up.split("/", 1)[1] if "/" in up else branch
        glp, allowed = self._gl_pick(p)
        title = str(title or "").strip()
        if not glp:
            gh, entry, gh_allowed = self._gh_pick(p)
            if not entry:
                raise ValueError("the checkout has no GitLab project / GitHub repository (projects.json / origin)")
            if gh is None:
                raise ValueError("piperd has no GitHub login (gh auth login --hostname %s)" % github.split(entry)[0])
            # W23: a pull request - GitHub has a real draft flag, the title stays as written
            mr = github.create_mr(gh, github.split(entry)[1], gh_allowed, source, target, title,
                                  str(description or ""), draft=bool(draft))
            audit({"event": "gui-pr", "project": p["key"], "github": entry, "iid": mr.get("iid"), "source": source,
                   "target": target})
            self._ci_cache.pop(p["key"], None)
            return mr
        if draft and not re.match(r"(?i)^(draft:|\[draft\]|wip:)", title):
            title = "Draft: " + title
        gl = gitlab.GitLab(self.gitlab_host(), gitlab.token(), opener=self.gitlab_opener)
        mr = gitlab.create_mr(gl, glp, allowed, source, target, title, str(description or ""))
        audit({"event": "gui-mr", "project": p["key"], "gitlab": glp, "iid": mr.get("iid"), "source": source,
               "target": target})
        self._ci_cache.pop(p["key"], None)
        return mr

    # ------------------------------------------------------------ dev stack through a provider (9.7 #8)
    def _stack(self):
        """The stack provider an extension supplies (stackview.py), else StackError."""
        for make in extensions.each("stack_provider"):
            prov = make(self)
            if prov is not None:
                return prov
        raise stackview.StackError("no dev stack provider (an extension supplies one)")

    def _stack_up(self) -> bool:
        """Whether the provider answers (the Stack section and the Stacks link only make sense then)."""
        try:
            return bool(self._stack().up())
        except Exception:  # noqa: BLE001 - no provider, or it failed: no stack
            return False

    def _stack_dash(self) -> str:
        try:
            return str(self._stack().dashboard_url() or "")
        except stackview.StackError:
            return ""

    def _stack_wt(self, p: dict) -> dict:
        prov = self._stack()
        w = self.stack_worktree(p["path"])
        if not w:
            hint = str(getattr(prov, "NO_STACK", "") or "")
            raise stackview.StackError("no dev stack for %s%s" % (p["path"], " (%s)" % hint if hint else ""))
        return w

    def stack_status(self, key: str) -> dict:
        p = self.gui_project(key)
        dash = self._stack_dash()
        try:
            w = self._stack_wt(p)
            st = self._stack().status(w["root"])
        except stackview.StackError as e:
            return {"stack": None, "services": [], "dashboard": dash, "error": str(e)}
        out = {"stack": str(w.get("name") or w["root"]), "services": stackview.service_rows(st), "dashboard": dash}
        if st.get("ps_err"):
            out["error"] = scrub(str(st["ps_err"]))[:500]
        return out

    def stack_view(self, key: str) -> dict:
        """stack_status() with what the panel acts on: the provider's card (per-service verbs,
        primary action, links) and the stack's running or lately ended job. A provider without
        the card falls back to the plain status (no actions but Logs and Restart).
        A linked worktree adds `worktree` {mode main|own, main}: whose stack this is; "own" with
        no stack yet is the panel's "Set up and start" state (its setup job in `jobs`)."""
        p = self.gui_project(key)
        out = self._stack_view(p)
        link = self.worktree_link(p["path"])
        if link:
            out["worktree"] = {"mode": self.worktree_stack_mode(link["top"]), "main": link["main_top"]}
            if out["worktree"]["mode"] == "own" and not out.get("stack"):
                out.pop("error", None)   # not a failure: nothing set up yet
                try:
                    out["jobs"] = stackview.stack_jobs(self._stack().jobs(), link["top"], time.time(), self._stack_seen)
                except stackview.StackError as e:
                    out["error"], out["jobs"] = str(e), []
                for j in out["jobs"]:
                    j["error"] = scrub(j["error"])
        return out

    def _stack_view(self, p: dict) -> dict:
        key = p["key"]
        dash = self._stack_dash()
        try:
            td = self._stack()
            w = self._stack_wt(p)
        except stackview.StackError as e:
            return {"stack": None, "services": [], "dashboard": dash, "error": str(e)}
        try:
            card = td.dash(w["root"])
        except stackview.StackError:
            return self.stack_status(key)
        if card is None:
            return self.stack_status(key)
        if card.get("error") and not card.get("services"):
            # the provider could not build the card (a broken stack definition, ...) - its
            # empty placeholder would hide running containers; the plain status still lists them
            out = self.stack_status(key)
            out["error"] = scrub("the provider's card failed: %s%s" % (
                card["error"], " - " + out["error"] if out.get("error") else ""))[:500]
            return out
        out = dict(stackview.dash_view(card), stack=str(w.get("name") or w["root"]), dashboard=dash)
        if out.get("error"):
            out["error"] = scrub(out["error"])[:500]
        try:
            out["jobs"] = stackview.stack_jobs(td.jobs(), w["root"], time.time(), self._stack_seen)
        except stackview.StackError:
            out["jobs"] = []
        for j in out["jobs"]:
            j["error"] = scrub(j["error"])
        return out

    def _worktree_of(self, key: str) -> tuple[dict, dict]:
        p = self.gui_project(key)
        link = self.worktree_link(p["path"])
        if not link:
            raise ValueError("%s is not a linked worktree of a recorded main checkout" % p["key"])
        return p, link

    def stack_mode(self, key: str, mode: str) -> dict:
        """The worktree's choice: the main checkout's stack or its own - the Stack panel, the DB view's
        dev connection and new sessions' stack access follow it; nothing is started or stopped"""
        p, link = self._worktree_of(key)
        self.set_worktree_stack_mode(link["top"], mode)
        audit({"event": "gui-stack-mode", "project": p["key"], "mode": mode})
        return {"ok": True, "mode": mode}

    @quietwindow.managed("stack")
    def stack_setup(self, key: str) -> dict:
        """"Set up and start": the provider's adopt job (verb worktree_adopt) for the worktree checkout,
        then the worktree uses its own stack"""
        p, link = self._worktree_of(key)
        r = self._stack().adopt(link["top"])
        self.set_worktree_stack_mode(link["top"], "own")
        audit({"event": "gui-stack-setup", "project": p["key"], "job": r.get("id")})
        return {"ok": True, "job": {"id": str(r.get("id") or ""), "verb": "worktree_adopt", "state": "running",
                                    "stage": "", "rc": None, "error": "", "started": int(time.time()), "services": []}}

    @quietwindow.managed("stack")
    def stack_action(self, key: str, verb: str, service: str = "") -> dict:
        p = self.gui_project(key)
        stackview.check_service(service)
        if verb not in stackview.VERBS:
            raise ValueError("verb %r is not offered here (the stack's own dashboard has it)" % verb)
        w = self._stack_wt(p)
        r = self._stack().verb(w["root"], verb, service)
        audit({"event": "gui-stack-" + verb, "project": p["key"], "service": service or "*", "job": r.get("id")})
        return {"ok": True, "job": stackview.job_view(r) if r.get("id") else None}

    def _stack_own_job(self, key: str, jid: str) -> tuple[object, dict]:
        """the job, only when it belongs to this project's stack (a 404 otherwise)"""
        p = self.gui_project(key)
        stackview.check_job(jid)
        link = self.worktree_link(p["path"])
        try:
            roots = {os.path.realpath(self._stack_wt(p)["root"])}
        except stackview.StackError:   # a worktree setting up its own stack has none registered yet
            if not link or self.worktree_stack_mode(link["top"]) != "own":
                raise
            roots = set()
        td = self._stack()
        j = td.job(jid)
        jroot = os.path.realpath(str(j.get("root") or ""))
        # the worktree's own setup stays reachable after "Use main" switched the panel away from it
        mine = jroot in roots or (link and j.get("verb") == "worktree_adopt" and jroot == os.path.realpath(link["top"]))
        # the panel's own kinds only: an exec transcript or a restore stays the dashboard's
        if not mine or j.get("verb") not in stackview.PANEL_JOBS:
            raise KeyError("no such job of this stack")
        return td, j

    def stack_job_log(self, key: str, jid: str, offset: int = 0) -> dict:
        td, j = self._stack_own_job(key, jid)
        r = td.job_log(jid, offset)
        state = str(r.get("state") or j.get("state") or "")
        if state != "running" and j.get("state") == "running":
            j = td.job(jid)   # it ended between the two reads: rc and the error tail are in the new JSON
        # the human's own job log, never sent to a session: not scrubbed (L4), like stack_logs
        return {"job": stackview.job_view(j), "offset": int(r.get("offset") or 0), "chunk": str(r.get("chunk") or ""),
                "state": state}

    def stack_job_cancel(self, key: str, jid: str) -> dict:
        td, _ = self._stack_own_job(key, jid)
        r = td.cancel(jid)
        audit({"event": "gui-stack-cancel", "project": self.gui_project(key)["key"], "job": jid})
        return {"ok": True, "state": str(r.get("state") or ""), "signalled": bool(r.get("signalled"))}

    def stack_logs(self, key: str, service: str, tail: int = 300) -> str:
        p = self.gui_project(key)
        stackview.check_service(service)
        return self._stack().logs(self._stack_wt(p)["root"], service, tail)

    @quietwindow.managed("stack")
    def stack_restart(self, key: str, service: str) -> dict:
        p = self.gui_project(key)
        stackview.check_service(service)
        w = self._stack_wt(p)
        r = self._stack().restart(w["root"], service)
        audit({"event": "gui-stack-restart", "project": p["key"], "service": service or "*", "job": r.get("id")})
        return {"ok": True, "job": r.get("id")} if r.get("id") else {"ok": True}

    # ------------------------------------------------------------ turn snapshots (9.7 #2)
    def _turn_event(self, s, ev: str, e: dict) -> None:
        """A hook event of a project session (untrusted: only WHEN to snapshot, never what)."""
        ts = e.get("ts")
        ok = isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)   # NaN would stick in turn_seen
        ts = float(ts) if ok else time.time()
        now = time.time()
        if now - ts > EVENT_MAX_AGE:
            return
        ts = min(ts, now)   # a future stamp (the sandbox writes it) must not block every later turn
        last = s.turns[-1] if s.turns else None
        stamp = max(float(last.get("started") or 0), float(last.get("ended") or 0)) if last else 0.0
        with self._turn_lock:
            if ts <= max(stamp, s.turn_seen):
                return
            s.turn_seen = ts
            if ev == "UserPromptSubmit":
                s.review_pending = False
                s.save()
            prompt = common.no_bidi(scrub(str(e.get("prompt") or "")[:8192]))[:300] if ev == "UserPromptSubmit" else ""   # shown as Last turn (E25)
            s.turn_q.append((ev, ts, prompt, e.get("_human")))   # _human: set by piperd (ptyhost keystrokes), never the file
            if s.turn_busy:
                return
            s.turn_busy = True
        threading.Thread(target=self._turn_worker, args=(s,), daemon=True, name="snap-" + s.id).start()

    def _fsedit_event(self, s, e: dict) -> None:
        """PostToolUse of an editing tool: the GUI re-reads what the agent wrote.
        Untrusted, a hint only: a path outside the session's checkout or `.git` is dropped; a known editing tool without a path (Codex apply_patch whose
        patch named none) counts as "unknown" - the GUI re-reads every open tab. Never touches
        the session state (working / waiting)."""
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts) \
                or time.time() - ts > EVENT_MAX_AGE:
            return   # a replay after a piperd restart
        raw = e.get("file_paths")
        paths = [x for x in raw if isinstance(x, str) and x][:50] if isinstance(raw, list) else []
        if isinstance(e.get("file_path"), str) and e["file_path"]:
            paths.insert(0, e["file_path"])
        tool = str(e.get("tool_name") or "").lower()
        if not paths and tool not in EDIT_TOOLS:
            return   # another tool (Codex sends PostToolUse for every tool)
        roots = {s.path.rstrip("/") + "/", os.path.realpath(s.path).rstrip("/") + "/"}
        rels = []
        for x in paths:
            if "\0" in x or len(x) > 4096:
                continue
            if x.startswith("/"):
                hit = [r for r in roots if x.startswith(r)]
                if not hit:
                    continue
                x = x[len(hit[0]):]
            x = os.path.normpath(x)
            if x in (".", "") or x == ".." or x.startswith("../") or x.startswith("/") \
                    or guard.is_git_path(x):
                continue
            rels.append(x)
        if paths and not rels:
            return   # every path outside the checkout or `.git`: nothing the GUI may show
        now = time.time()
        with s.lock:
            s.fsedit_seq += 1
            if rels:
                for x in rels:
                    s.fsedits.append({"path": x, "ts": now})
            else:
                s.fsedit_unknown = s.fsedit_seq
        self.bump()

    @staticmethod
    def fsedit_state(s) -> dict | None:
        """/api/state "fsedit" of a session: {seq, paths (last 20 distinct, newest first),
        unknown (the seq of the last edit without a path, 0: none)}; None before the first edit."""
        with s.lock:
            seq = s.fsedit_seq
            if not seq:
                return None
            items = list(s.fsedits)
            unknown = s.fsedit_unknown
        out: list[str] = []
        for it in reversed(items):
            if it["path"] not in out:
                out.append(it["path"])
                if len(out) >= 20:
                    break
        return {"seq": seq, "paths": out, "unknown": unknown}

    def _snapshot(self, s, name: str = "snap.index", raw: bool = True) -> str:
        """raw: the disk bytes (turn snapshots, revert); else Git's clean/eol conversion, as a commit sees it."""
        with s.snap_lock:
            top = self._turn_top(s)
            os.makedirs(s.dir, mode=0o700, exist_ok=True)
            return gitops.snapshot(top, os.path.join(s.dir, name), self._snap_objects(s), strict=True, raw=raw)

    @staticmethod
    def _snap_objects(s) -> str:
        """The session's own object store for its snapshots (gitops.snap_env): in its
        directory, beside - never inside - the box the sandbox mounts."""
        return os.path.join(s.dir, "snap-objects")

    @staticmethod
    def _snap_gitdir(s) -> str:
        return os.path.join(s.dir, "snap.git")

    def _pin_turn(self, s, tree: str, name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", s.id) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid snapshot reference")
        with s.snap_lock:
            gitops.pin_snapshot(self._turn_top(s), self._snap_objects(s), self._snap_gitdir(s),
                                "refs/piper/turns/" + s.id + "/" + name, tree)

    def _drop_turn_snapshots(self, s) -> None:
        """Remove private refs and their objects when their session/worktree is forgotten."""
        with s.turn_action_lock, s.snap_lock:
            shutil.rmtree(self._snap_gitdir(s), ignore_errors=True)
            shutil.rmtree(self._snap_objects(s), ignore_errors=True)
            s.turns, s.reverts, s.revert_note = [], [], ""
            s.review_pending, s.review_summary = False, {}
        s.save()

    @staticmethod
    def _turn_running(s) -> bool:
        return (s.state == "working" or bool(s.subagents or s.background_tasks)
                or bool(s.state not in ("exited", "error", "blocked") and s.turns and not s.turns[-1].get("to"))
                or any(ev == "UserPromptSubmit" for ev, *_ in s.turn_q))

    def review_done(self, sid: str) -> dict:
        s = self.get(sid)
        s.review_pending = False
        s.save()
        self.bump()
        return {"ok": True}

    def _turn_summary(self, s, frm: str, to: str) -> dict:
        counts = gitops.tree_numstat(self._turn_top(s), frm, to, self._snap_objects(s))
        rows = [v for p, v in counts.items() if not guard.is_git_path(p)]
        return {"files": len(rows), "add": sum(r["add"] or 0 for r in rows),
                "del": sum(r["del"] or 0 for r in rows), "turns": len(s.turns)}

    def _turn_worker(self, s) -> None:
        while True:
            with self._turn_lock:
                if not s.turn_q:
                    s.turn_busy = False
                    return
                ev, ts, prompt, human = s.turn_q.pop(0)
            with s.turn_action_lock:
                if s.hidden:
                    continue
                try:
                    tree = self._snapshot(s)
                except (gitops.GitError, OSError) as e:
                    print("piperd: snapshot %s failed (%s)" % (s.id, type(e).__name__), flush=True)
                    continue
                try:
                    with s.lock:
                        turns = [dict(t) for t in s.turns]
                        last = turns[-1] if turns else None
                        is_open = last is not None and not last.get("to")
                        if ev == "UserPromptSubmit":
                            if is_open:   # the previous turn never saw its Stop (interrupted)
                                self._pin_turn(s, tree, str(last["n"]))
                                last["to"], last["ended"] = tree, ts
                            n = (int(last["n"]) + 1) if last else 1
                            self._pin_turn(s, tree, str(n) + "-start")
                            turns.append({"n": n, "prompt": prompt, "started": ts,
                                          "ended": None, "from": tree, "to": None,
                                          "human": human})   # the timesheet counts only a turn the human typed (None: cannot tell)
                        elif is_open:
                            self._pin_turn(s, tree, str(last["n"]))
                            last["to"], last["ended"] = tree, ts
                        else:
                            continue
                        s.turns = turns
                        if ev != "UserPromptSubmit":
                            last["summary"] = self._turn_summary(s, last["from"], tree)
                            s.review_summary = self._turn_summary(s, turns[0]["from"], tree)
                            s.review_pending = bool(last["summary"]["files"]) and not any(
                                queued == "UserPromptSubmit" for queued, *_ in s.turn_q)
                except (gitops.GitError, OSError, ValueError):
                    # No unpinned turn is offered for revert. The next hook can retry.
                    print("piperd: could not pin turn snapshot for %s" % s.id, flush=True)
                    continue
                try:
                    s.save()
                except OSError:
                    pass
                self.bump()

    def _turn_top(self, s) -> str:
        if s.kind != "project":
            raise ValueError("only a project session has a working tree")
        # the root recorded before any sandbox ran (Manager.repo_top); git answering differently
        # now means a repository was planted in the project - snapshotting it would copy hidden
        # files into a .git the sandbox reads - refused
        top = self._session_top(s)
        if top is None:
            raise gitops.GitError("no trusted repository for this project: %s" % self.repo_top_why(s.path))
        return top

    def turns(self, sid: str) -> dict:
        s = self.get(sid)
        if s.kind != "project":
            return {"turns": [], "note": "%s has no working tree" % ("The Global agent" if s.kind == "advisor"
                                                                     else "The production check")}
        notes = []
        sib = self.siblings(s)
        if sib:
            notes.append("%s also works in the same tree: the turn diff includes their changes too."
                         % ", ".join("\"%s\"" % x.title for x in sib))
        if not s.turns:
            notes.append("A snapshot is taken when a prompt is sent and when the turn ends.")
        out = {"turns": [dict(t) for t in s.turns]}
        # Also migrate snapshots made by older daemons before offering Revert.
        for t in out["turns"]:
            try:
                self._pin_turn(s, t["from"], str(t["n"]) + "-start")
                if t.get("to"):
                    self._pin_turn(s, t["to"], str(t["n"]))
                t.setdefault("summary", self._turn_summary(s, t["from"], t.get("to") or t["from"]))
            except (gitops.GitError, OSError, ValueError):
                t["unavailable"] = "Could not check: snapshot is unavailable"
        out["reverts"] = [dict(r) for r in s.reverts]
        if notes:
            out["note"] = " ".join(notes)
        return out

    def _turn_trees(self, s, frm: str, to: str) -> tuple[str, str, str]:
        top = self._turn_top(s)
        known = {t.get("from") for t in s.turns} | {t.get("to") for t in s.turns if t.get("to")}
        if frm not in known or not gitops.is_tree(top, frm, self._snap_objects(s)):
            raise ValueError("unknown snapshot %r" % frm)
        if to:
            if to not in known or not gitops.is_tree(top, to, self._snap_objects(s)):
                raise ValueError("unknown snapshot %r" % to)
        else:
            to = self._snapshot(s, "snap-now.index")   # "now": fresh, not stored
        return top, frm, to

    def _session_hidden(self, s):
        """sensitive(repo-relative) for a session's turn views - a mark only, the GUI's one
        definition (K11: Manager.gui_sensitive, project_hidden); hidden_for when the project is gone."""
        try:
            return self.gui_sensitive(self.gui_project(s.project), self._turn_top(s))
        except (KeyError, ValueError, gitops.GitError):
            return self.hidden_for(s)

    def _turn_gui_hidden(self, s):
        """What the turn views never show (repository-relative): `.git` only (K12 - the human owns
        the whole repository; a file hidden from Claude is the GUI's like any other)."""
        self._turn_top(s)   # the trusted root, or refused
        return guard.is_git_path

    def turn_files(self, sid: str, frm: str, to: str = "", *, pinned: bool = False) -> dict:
        s = self.get(sid)
        if pinned:
            top = self._turn_top(s)
            if not all(gitops.is_tree(top, tree, self._snap_objects(s)) for tree in (frm, to)):
                raise ValueError("unknown pinned tree")
        else:
            top, frm, to = self._turn_trees(s, frm, to)
        out = gitops.tree_files(top, frm, to, self._session_hidden(s), self._snap_objects(s))
        gone = self._turn_gui_hidden(s)
        try:
            flag = self.policy_flag(self.gui_project(s.project))
        except KeyError:
            flag = {"policy_unreadable": False}
        return dict({"from": frm, "to": to, "files": [f for f in out if not gone(f["path"])]}, **flag)

    def turn_diff(self, sid: str, frm: str, to: str = "", path: str = "") -> str:
        s = self.get(sid)
        rel = None if path in ("", ".") else path
        if rel is not None and (rel.startswith("/") or "\0" in rel or ".." in rel.split("/")):
            raise ValueError("invalid path %r" % path)
        hidden = self._turn_gui_hidden(s)
        if rel is not None and hidden(rel):
            raise projectfs.FsError("%s is inside .git" % rel)
        top, frm, to = self._turn_trees(s, frm, to)
        return gitops.tree_diff(top, frm, to, hidden, rel, self._snap_objects(s), GUI_WITHHELD)

    def turn_file_history(self, sid: str, path: str, *, live_to: str = "") -> dict:
        """The turns of a session in which one file changed, newest first, with
        +/- lines (None for a binary file). `path` is relative to the session's checkout;
        `diff_path` (repository-relative) is what GET .../turn ?diff= takes. An open turn is
        compared with the working tree now (a fresh snapshot, not stored)."""
        s = self.get(sid)
        if s.kind != "project":
            return {"turns": [], "diff_path": ""}
        rel = str(path or "")
        if not rel or rel.startswith("/") or "\0" in rel or ".." in rel.split("/"):
            raise ValueError("invalid path %r" % path)
        rel = os.path.normpath(rel)
        top = self._turn_top(s)
        grel = os.path.relpath(os.path.join(os.path.realpath(s.path), rel), top)
        hidden = self._turn_gui_hidden(s)
        if grel == ".." or grel.startswith("../") or hidden(grel):
            raise projectfs.FsError("%s is outside the repository (or .git)" % rel)
        objs = self._snap_objects(s)
        env = gitops.snap_env(top, objs)
        now = None
        out = []
        for i, t in enumerate(list(s.turns)):
            frm, to = t.get("from"), t.get("to")
            if not frm or not gitops.is_tree(top, frm, objs):
                continue
            b = to
            if not to:
                if i != len(s.turns) - 1:
                    continue
                if now is None:
                    now = live_to or self._snapshot(s, "snap-now.index")
                b = now
            elif not gitops.is_tree(top, to, objs):
                continue
            rows = gitops._git(top, "diff-tree", "-r", "--no-renames", "--numstat", "-z", frm, b, "--",
                               gitops.literal(grel), env=env)
            # exactly this file: a directory path would list (and leak) every file below it
            rec = [x.split("\t", 2) for x in rows.split("\0") if x]
            hit = next((r for r in rec if len(r) == 3 and r[2] == grel), None)
            if hit is None:
                continue
            a, d = hit[0], hit[1]
            out.append({"index": i, "n": t.get("n"), "prompt": t.get("prompt") or "", "from": frm, "to": to,
                        "at": t.get("ended") or t.get("started"),
                        "add": int(a) if a.isdigit() else None, "del": int(d) if d.isdigit() else None})
        out.reverse()
        return {"turns": out, "diff_path": grel}

    def _revert_target(self, s, n: int, undo: str = "") -> tuple[str, dict | None]:
        if self._turn_running(s) or s.turn_busy or s.state in self.PRE_SANDBOX_STATES or s.state == "stopping":
            raise ValueError("Stop the turn first (Esc)")
        if undo:
            entry = next((r for r in s.reverts if r["id"] == undo and not r.get("undone")), None)
            if not entry:
                raise ValueError("Undo revert is no longer available")
            return entry["before"], entry
        turn = next((t for t in s.turns if t["n"] == n and t.get("to")), None)
        if not turn:
            raise ValueError("no finished snapshot for this turn")
        self._pin_turn(s, turn["to"], str(n))
        return turn["to"], None

    def _revert_files(self, s, current: str, target: str, entry: dict | None = None) -> list[dict]:
        """Only historical paths: never remove an unrelated, newly untracked file."""
        top, objects = self._turn_top(s), self._snap_objects(s)
        changed = [p for p in gitops._git(top, "diff-tree", "-r", "--no-renames", "--name-only", "-z",
                                        current, target, env=gitops.snap_env(top, objects)).split("\0") if p]
        if entry:
            allowed = set(entry["paths"])
        else:
            allowed = set()
            for tree in {t[k] for t in s.turns for k in ("from", "to") if t.get(k)}:
                allowed.update(p for p in gitops._git(top, "ls-tree", "-r", "--name-only", "-z", tree,
                                                    env=gitops.snap_env(top, objects)).split("\0") if p)
        paths = sorted(set(changed) & allowed)
        before = gitops.tree_versions(top, current, paths, objects)
        after = gitops.tree_versions(top, target, paths, objects)
        rows = []
        for path in paths:
            self._review_path(s, top, path)
            for version in (before[path], after[path]):
                if version["mode"] not in (None, "100644", "100755"):
                    raise ValueError("Could not check: %s is a symlink, submodule or directory; restore it manually" % path)
            version = self._working_version(s, top, path)
            if version["mode"] not in (None, "100644", "100755"):
                raise ValueError("Could not check: %s is not a regular file of at most 2 MiB" % path)
            row = {"path": path, "removed": after[path]["oid"] is None, "before": before[path], "after": after[path]}
            if row["removed"]:
                for turn in s.turns:
                    if turn.get("to") and gitops.tree_versions(top, turn["to"], [path], objects)[path]["oid"]:
                        row["added_in"] = turn["n"]
                        break
            rows.append(row)
        return rows

    def turn_revert_preview(self, sid: str, n: int = 0, undo: str = "") -> dict:
        s = self.get(sid)
        with s.turn_action_lock, self._write_lock:
            target, entry = self._revert_target(s, n, undo)
            current = self._snapshot(s, "revert-preview.index")
            return {"n": n, "undo": undo, "current": current, "files": self._revert_files(s, current, target, entry)}

    def turn_revert(self, sid: str, n: int = 0, current: str = "", undo: str = "") -> dict:
        """Back up first, then restore bytes/modes through GUI guards; HEAD/index never change."""
        s = self.get(sid)
        with s.turn_action_lock, self._write_lock:
            target, entry = self._revert_target(s, n, undo)
            top, objects = self._turn_top(s), self._snap_objects(s)
            before = self._snapshot(s, "revert-before.index")
            if not current or current != before:
                raise ValueError("Files changed since the preview; open Revert to here again")
            rows = self._revert_files(s, before, target, entry)
            p, root = self.gui_project(s.project), os.path.realpath(s.path)
            paths = [r["path"] for r in rows]
            if not paths:
                raise ValueError("No files need to be reverted")
            self._ops_busy(top, paths)
            # A sibling may be writing paths absent from its PostToolUse hints: refuse the whole operation.
            if any(self._turn_running(x) for x in self._same_tree(top)):
                raise ValueError("Stop the turn first (Esc)")
            rels = {path: os.path.relpath(os.path.join(top, path), root) for path in paths}
            rule = self.project_hidden(p)
            writes = []
            for row in rows:
                path, rel = row["path"], rels[row["path"]]
                data = None
                if not row["removed"]:
                    size = int(gitops._git(top, "cat-file", "-s", row["after"]["oid"], env=gitops.snap_env(top, objects)))
                    if size > WRITE_MAX:
                        raise ValueError("Could not check: %s exceeds the 2 MiB file-write limit" % path)
                    data = gitops.read_blob(top, row["after"]["oid"], gitops.snap_env(top, objects), WRITE_MAX)
                if rule(rel) and data is not None:
                    self._hidden_write_guard(root, rel)
                pairs = [(rel, None)] if data is None else [(None, rel)]
                if data is not None and row["before"]["oid"] is not None:
                    pairs = []   # existing hidden files are rewritten in place, preserving their mount
                detach = self._ops_guard(p, root, pairs)
                self._ops_held(detach)
                writes.append((row, rel, data))
            rid = secrets.token_hex(8)
            self._pin_turn(s, before, "undo-" + rid)
            history = {"id": rid, "n": n, "at": time.time(), "before": before, "target": target,
                       "paths": paths, "reverted": [t["n"] for t in s.turns if t.get("reverted")],
                       "status": "saved", "undone": False}
            s.reverts.append(history)
            s.save()   # even a partial filesystem failure has a durable Undo
            audit({"event": "turn-revert", "session": sid, "turn": n, "undo": rid, "files": paths, "status": "saved"})
            try:
                for row, rel, data in writes:
                    if self._turn_running(s):
                        raise ValueError("Stop the turn first (Esc)")
                    if data is None:
                        common.safe_unlink(root, rel)
                    else:
                        # Create parents without traversing symlinks; no recursive deletion of directories.
                        fd = common._open_dir(root, [])
                        try:
                            for part in common._parts(rel)[:-1]:
                                try:
                                    os.mkdir(part, mode=0o755, dir_fd=fd)
                                except FileExistsError:
                                    pass
                                nfd = os.open(part, _DIRFLAGS, dir_fd=fd)
                                os.close(fd)
                                fd = nfd
                        finally:
                            os.close(fd)
                        mode = 0o755 if row["after"]["mode"] == "100755" else 0o644
                        if rule(rel) and row["before"]["oid"] is not None:
                            old = common.safe_read(root, rel, WRITE_MAX + 1)
                            common.overwrite_in_place(root, rel, data, old, mode=mode, exact_mode=True,
                                                      pin=self._ops_pin(root, rel))
                        else:
                            common.safe_write(root, rel, data, mode=mode, exact_mode=True,
                                              create=row["before"]["oid"] is None,
                                              in_place=bool(rule(rel)),
                                              pin=self._ops_pin(root, rel) if rule(rel) else None)
                if entry:
                    entry["undone"] = True
                for turn in s.turns:
                    turn["reverted"] = turn["n"] in entry["reverted"] if entry else turn["n"] > n
                history["status"] = "applied"
            finally:
                hidden = self._session_hidden(s)
                shown = [x for x in paths if not hidden(x)]
                prior = s.revert_note
                lead = ("[piper] The user restored worktree files" + (" (Undo revert)" if undo else " to the end of turn %d" % n)
                        if history["status"] == "applied" else "[piper] A file revert was interrupted; some of these files may have changed (Undo is available in History)")
                s.revert_note = (lead
                                 + ". Commits and the index were left unchanged. Re-read these files before continuing:\n"
                                 + "\n".join("- " + common.show_bidi(os.path.relpath(os.path.join(top, x), root))
                                             .replace("\n", "\\n").replace("\r", "\\r") for x in shown)
                                 + ("\nSome hidden files were also restored; their names and contents are withheld." if len(shown) < len(paths) else ""))
                if prior:
                    s.revert_note = prior + "\n\n" + s.revert_note
                s.save()
                # Reuse the editor's change notification so other tabs reload restored files too.
                self._fsedit_event(s, {"ts": time.time(), "file_paths": [rels[path] for path in paths]})
                self._ops_done(p)
                self.bump()
            return {"ok": True, "n": n, "undo": rid, "files": paths}

    # ------------------------------------------------------------ pinned review (9.21)
    def _review_path(self, session, top: str, path: str, *, project: dict | None = None) -> None:
        """Authorize a repository path for the GUI like its diffs (K12): inside the session's project and
        repository, never `.git`. A file hidden from Claude is the human's too - review_dispatch keeps it
        away from the agent. `project` is a request-local, already repository-checked context;
        the batch caller must revalidate it before publishing. Path resolution is never cached."""
        if not path or path.startswith("/") or "\0" in path or ".." in path.replace("\\", "/").split("/"):
            raise projectfs.FsError("invalid review path")
        rel = os.path.relpath(os.path.join(top, path), os.path.realpath(session.path))
        if project is None:
            p, _, _, full = self._pfile(session.project, rel)
            repo, _, _ = self._in_repo(p, full)
        else:
            full, base = projectfs._resolve(project["path"], rel, guard.is_git_path)
            if not base:
                raise projectfs.FsError("a path inside the project is required")
            repo = top
        grel = os.path.relpath(full, top)
        if repo != top or grel == ".." or grel.startswith("../") or guard.is_git_path(grel) or guard.is_git_path(path):
            raise projectfs.FsError("%s is outside the repository (or .git)" % path)

    def _working_version(self, session, top: str, path: str, *, canonical: bool = False,
                         project: dict | None = None, expected_oid: str | None = None) -> dict:
        """Bounded bytes and mode of a live path, without following a link or its parents."""
        self._review_path(session, top, path, project=project)
        parts = common._parts(path)
        try:
            dfd = common._open_dir(top, parts[:-1])
        except FileNotFoundError:
            return {"sha": None, "mode": None, "oid": None} if canonical else {"sha": None, "mode": None}
        try:
            try:
                st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            except FileNotFoundError:
                return {"sha": None, "mode": None, "oid": None} if canonical else {"sha": None, "mode": None}
            if stat.S_ISLNK(st.st_mode):
                raw = os.readlink(parts[-1], dir_fd=dfd).encode("utf-8", "surrogateescape")
                mode = "120000"
            elif stat.S_ISREG(st.st_mode):
                if st.st_size > projectfs.MAX_READ:
                    return {"sha": None, "mode": "too_large"}
                fd = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dfd)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode):
                        raise projectfs.FsError("path is no longer a regular file")
                    raw = os.read(fd, projectfs.MAX_READ + 1)
                    if len(raw) > projectfs.MAX_READ:
                        return {"sha": None, "mode": "too_large"}
                finally:
                    os.close(fd)
                mode = "100755" if st.st_mode & 0o111 else "100644"
            else:
                return {"sha": None, "mode": "unsupported"}
        finally:
            os.close(dfd)
        out = {"sha": hashlib.sha256(raw).hexdigest(), "mode": mode}
        if canonical:
            # Exact raw equality with the pinned blob needs neither attributes nor a Git process.
            # A mismatch may only be CRLF/encoding/a clean filter: let Git canonicalize those bytes.
            # Commit/stage still revalidate the live canonical version before writing anything.
            if expected_oid and gitops.blob_oid(top, raw, object_format=(
                    "sha256" if len(expected_oid) == 64 else "sha1")) == expected_oid:
                out["oid"] = expected_oid
            else:
                if project is not None:
                    self._review_path(session, top, path)  # recheck repository identity before invoking Git
                out["oid"] = gitops._git(top, "hash-object", "--path=" + path, "--stdin", input=raw).strip()
        return out

    def _review(self, sid: str, rid: str):
        session = self.get(sid)
        with self._review_lock:
            review = self._reviews.get(str(rid))
        if not review or review["session"] != sid or review["top"] != self._turn_top(session):
            raise ValueError("review expired or belongs to another session; refresh")
        return session, review

    def _review_branch_base(self, session, top: str, head: str) -> tuple[str, str]:
        """Prefer the known MR target; otherwise retain the branch's recorded starting commit."""
        branch = gitops.current_branch(top)
        hit = self._ci_cache.get(session.project)
        ci = hit[1] if hit and hit[1].get("branch") == branch else {}
        mr = ci.get("mr") or {}
        target = mr.get("target_branch") if mr.get("source_branch") == branch and mr.get("state") == "opened" else None
        def commit(ref):
            return gitops._git(top, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", ok=(0, 128)).strip()
        if target:
            for ref in ("refs/remotes/origin/" + target, "refs/heads/" + target):
                oid = commit(ref)
                if gitops._TREE.fullmatch(oid):
                    base = gitops._git(top, "merge-base", head, oid, ok=(0, 1)).strip()
                    if gitops._TREE.fullmatch(base):
                        return base, "Merge request against " + target
            if features.on("github" if ci.get("github") else "gitlab"):
                raise ValueError("MR target " + target + " is unavailable locally; fetch its branch and refresh")
        # A branch created from HEAD still records its original commit, even without a remote.
        if branch and branch != "HEAD":
            entries = gitops._git(top, "reflog", "show", "--format=%H%x00%gs", "refs/heads/" + branch, ok=(0, 128)).splitlines()
            if entries:
                oid, _, message = entries[-1].partition("\0")
                source = message.removeprefix("branch: Created from ")
                same_branch = source in (branch, "origin/" + branch, "refs/heads/" + branch, "refs/remotes/origin/" + branch)
                if message.startswith("branch: Created from ") and not same_branch and gitops._TREE.fullmatch(oid):
                    return oid, "Branch start (" + source + ")"
        remote = gitops._git(top, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", ok=(0, 1, 128)).strip()
        for ref in dict.fromkeys([remote, "refs/heads/dev", "refs/heads/main", "refs/heads/master"]):
            if not ref or ref == "refs/heads/" + branch or ref == "refs/remotes/origin/" + branch:
                continue
            oid = commit(ref)
            if gitops._TREE.fullmatch(oid) and head:
                base = gitops._git(top, "merge-base", head, oid, ok=(0, 1)).strip()
                if gitops._TREE.fullmatch(base):
                    return base, "Common base with " + ref.removeprefix("refs/heads/").removeprefix("refs/remotes/")
        raise ValueError("Branch base unavailable; use Uncommitted changes or This session’s changes")

    def review_files(self, sid: str, scope: str = "session", n: int = 0) -> dict:
        """Pin one comparison. Only a live scope takes a fresh snapshot; blob reads never do."""
        if scope not in ("session", "head", "turn", "branch", "index", "unstaged"):
            raise ValueError("unknown review scope")
        session = self.get(sid)
        top, objects = self._turn_top(session), self._snap_objects(session)
        head = gitops._git(top, "rev-parse", "--verify", "HEAD", ok=(0, 128)).strip()
        if not gitops._TREE.fullmatch(head):
            head = ""
        base = (gitops._git(top, "rev-parse", head + "^{tree}").strip() if head else
                gitops._git(top, "hash-object", "-w", "-t", "tree", "--stdin", input=b"").strip())
        turns = list(session.turns)
        note = ""
        # a turn's own trees are raw disk bytes (revert): its live side matches them; every other live side is
        # clean/eol-converted, as a commit sees it. review_commit re-snapshots the same way.
        raw = scope == "turn" or (scope == "session" and bool(turns))
        if scope == "turn":
            turn = next((t for t in turns if t["n"] == n), None) if n else (turns[-1] if turns else None)
            if not turn:
                raise ValueError("the session has no such turn")
            frm = turn["from"]
            to = turn.get("to") or self._snapshot(session, "review-now.index", raw=raw)
        else:
            frm = turns[0]["from"] if scope == "session" and turns else base
            if scope == "branch":
                base_commit, note = self._review_branch_base(session, top, head)
                frm = gitops._git(top, "rev-parse", base_commit + "^{tree}").strip()
                note += " → current files (includes uncommitted edits)."
            if scope == "session" and not turns:
                note = "No session snapshot yet; comparing HEAD with the working tree."
            if scope == "unstaged":
                frm = gitops.snapshot(top, os.path.join(session.dir, "review-unstaged-base.index"), objects, staged=True)
            to = (gitops.snapshot(top, os.path.join(session.dir, "review-staged.index"), objects, staged=True)
                  if scope == "index" else
                  self._snapshot(session, "review-now.index", raw=raw))
        rows = self.turn_files(sid, frm, to, pinned=True)["files"]
        counts = gitops.tree_numstat(top, frm, to, objects)
        hidden = self._session_hidden(session)
        live = gitops.status(top, hidden)["files"]
        live_status = {r["path"]: r["x"] + r["y"] for r in live}
        changed = {r["path"] for r in live} | {r["orig"] for r in live if r.get("orig")}
        # `sensitive` is the "hidden from Claude" mark only (K11): the human reviews those files too;
        # `withheld` = no content (outside the session's project or repository, unreadable)
        paths = list(dict.fromkeys(p for r in rows for p in (r.get("orig"), r["path"]) if p))
        before = gitops.tree_versions(top, frm, paths, objects)
        after = gitops.tree_versions(top, to, paths, objects)
        project = self.gui_project(session.project)
        if self._top(project) != top:
            raise gitops.GitError("review repository changed; refresh")
        working = {}
        files = []
        for row in rows:
            row = dict(row, withheld=False)
            needed = [row["path"]] + ([row["orig"]] if row.get("orig") else [])
            can_commit = any(p in changed for p in needed)
            try:
                for path in needed:
                    version = self._working_version(session, top, path, project=project,
                                                    canonical=scope != "index" and can_commit and path == row["path"],
                                                    expected_oid=after[path]["oid"])
                    working[path] = {k: version[k] for k in ("sha", "mode")}
                    if path == row["path"]:
                        current_oid = version.get("oid")
                row.update(live_status=live_status.get(row["path"], ""), before=before[row.get("orig") or row["path"]], after=after[row["path"]],
                           **counts.get(row["path"], {"add": 0, "del": 0}), work_sha=working[row["path"]]["sha"], work_mode=working[row["path"]]["mode"],
                           committable=scope != "unstaged" and can_commit and (scope == "index" or current_oid == after[row["path"]]["oid"]))
                if row.get("orig"):
                    row["orig_after"] = after[row["orig"]]
            except (projectfs.FsError, OSError):
                row["withheld"] = True
            files.append(row)
        # No request-local context escapes if the project mapping or repository changed mid-scan.
        current_project = self.gui_project(session.project)
        if current_project["path"] != project["path"] or self._top(current_project) != top or self._turn_top(session) != top:
            raise gitops.GitError("review repository changed; refresh")
        rid = secrets.token_hex(16)
        public = {"id": rid, "scope": scope, "head": head, "from": frm, "to": to,
                  "project": session.project, "root": top, "files": files, "turns": [dict(t) for t in turns], "note": note}
        with self._review_lock:
            self._reviews[rid] = {"session": sid, "top": top, "objects": objects, "public": public, "raw": raw,
                                  "versions": after, "working": working, "index_before": before if scope == "unstaged" else None}
            while len(self._reviews) > 32:
                self._reviews.pop(next(iter(self._reviews)))
        return public

    def review_blob(self, sid: str, rid: str, path: str, side: str) -> dict:
        """A cached comparison row authorizes both rename paths before reading either blob."""
        session, review = self._review(sid, rid)
        row = next((r for r in review["public"]["files"] if r["path"] == path), None)
        if not row or row["withheld"] or side not in ("left", "right"):
            raise projectfs.FsError("file is not visible in this review")
        for name in (row["path"], row.get("orig")):
            if name:
                self._review_path(session, review["top"], name)
        name = (row.get("orig") or path) if side == "left" else path
        tree = review["public"]["from" if side == "left" else "to"]
        return gitops.tree_blob(review["top"], tree, name, gitops.snap_env(review["top"], review["objects"]), projectfs.MAX_READ)

    def review_diff(self, sid: str, rid: str) -> str:
        session, review = self._review(sid, rid)
        return gitops.tree_diff(review["top"], review["public"]["from"], review["public"]["to"],
                                self._turn_gui_hidden(session), objects=review["objects"], label=GUI_WITHHELD)

    def review_check(self, sid: str, rid: str, path: str = "") -> dict:
        """Invalidate viewed versions without replacing the comparison or creating snapshots."""
        session, review = self._review(sid, rid)
        top = review["top"]
        head = gitops._git(top, "rev-parse", "--verify", "HEAD", ok=(0, 128)).strip()
        head = head if gitops._TREE.fullmatch(head) else ""
        changed = []
        versions = review["working"]
        if path:
            row = next((r for r in review["public"]["files"] if r["path"] == path and not r["withheld"]), None)
            if not row:
                raise projectfs.FsError("file is not visible in this review")
            names = {row["path"], row.get("orig")}
            versions = {name: version for name, version in versions.items() if name in names}
        if review["public"]["scope"] == "index":
            current = gitops.snapshot(top, os.path.join(session.dir, "review-staged-check.index"), review["objects"], staged=True)
            actual = gitops.tree_versions(top, current, list(versions), review["objects"])
            return {"head_changed": head != review["public"]["head"],
                    "changed": [p for p in versions if actual[p] != review["versions"][p]]}
        if review["public"]["scope"] == "unstaged":
            current = gitops.snapshot(top, os.path.join(session.dir, "review-unstaged-check.index"), review["objects"], staged=True)
            actual = gitops.tree_versions(top, current, list(versions), review["objects"])
            changed.extend(p for p in versions if actual[p] != review["index_before"][p])
        for path, expected in versions.items():
            try:
                actual = self._working_version(session, top, path)
            except (OSError, projectfs.FsError):
                actual = None
            if actual != expected:
                changed.append(path)
        return {"head_changed": head != review["public"]["head"], "changed": changed}

    def review_stage(self, sid: str, rid: str, path: str) -> dict:
        """Add the exact reviewed bytes to Git; unrelated index entries remain intact."""
        session, review = self._review(sid, rid)
        # Porcelain exposes an unstaged rename as a deletion plus an untracked path.
        # Index-mode Stage acts on the selected status path, not its unselected counterpart.
        index_stage = review["public"]["scope"] == "unstaged"
        row = next((r for r in review["public"]["files"] if r["path"] == path or index_stage and r.get("orig") == path), None)
        if not row or row["withheld"] or review["public"]["scope"] == "index":
            raise projectfs.FsError("file cannot be staged from this review")
        names = [path] if index_stage else [path] + ([row["orig"]] if row.get("orig") else [])
        for name in names:
            if name:
                self._review_path(session, review["top"], name)
        with self._write_lock:
            self._ops_busy(review["top"], [path])   # the GUI's rule: only a file Claude edited in this turn
            check = self.review_check(sid, rid)
            if check["head_changed"] or set(names) & set(check["changed"]):
                raise ValueError("file or HEAD changed since review; refresh")
            current = self._working_version(session, review["top"], path, canonical=True)
            version = review["versions"][path]
            if current.get("oid") != version["oid"] or current["mode"] != review["working"][path]["mode"]:
                raise ValueError("working file differs from the pinned comparison; refresh")
            if row.get("orig") and not index_stage:
                gitops.stage_versions(review["top"], {p: review["versions"][p] for p in names}, review["objects"])
            else:
                gitops.stage_reviewed(review["top"], path, version, review["objects"])
        audit({"event": "review-stage", "session": sid, "path": path})
        return {"staged": path}

    def review_unstage(self, sid: str, rid: str, path: str) -> dict:
        """Restore selected index entries to reviewed HEAD; working files and unrelated staging stay intact."""
        session, review = self._review(sid, rid)
        row = next((r for r in review["public"]["files"] if r["path"] == path), None)
        if review["public"]["scope"] != "index" or not row or row["withheld"]:
            raise projectfs.FsError("file is not visible in this staged review")
        paths = [path] + ([row["orig"]] if row.get("orig") else [])
        for name in paths:
            self._review_path(session, review["top"], name)
        with self._write_lock:
            check = self.review_check(sid, rid)
            if check["head_changed"] or set(paths) & set(check["changed"]):
                raise ValueError("index or HEAD changed since review; refresh")
            versions = gitops.tree_versions(review["top"], review["public"]["from"], paths, review["objects"])
            gitops.stage_versions(review["top"], versions, review["objects"])
        audit({"event": "review-unstage", "session": sid, "path": path})
        self.bump()
        return {"unstaged": path}

    def review_write(self, sid: str, rid: str, path: str, text: str | None, sha: str | None) -> dict:
        """Replace exact bytes or restore an added/deleted file; every apply and Undo checks content or absence."""
        session, review = self._review(sid, rid)
        row = next((r for r in review["public"]["files"] if r["path"] == path), None)
        if not row or row["withheld"]:
            raise projectfs.FsError("file is not visible in this review")
        for name in (path, row.get("orig")):
            if name:
                self._review_path(session, review["top"], name)
        rel = os.path.relpath(os.path.join(review["top"], path), os.path.realpath(session.path))
        if text is None or sha is None:
            version = row.get("before") if row.get("before", {}).get("oid") else row.get("after")
            if row.get("orig") or row.get("status") not in ("A", "D") or not version or version.get("mode") not in ("100644", "100755"):
                raise ValueError("whole-file restoration requires an added or deleted regular file")
            if text is None:
                if sha is None:
                    raise ValueError("deleting a reviewed file requires its current sha")
                return dict(self.fs_delete(session.project, rel, expected_sha=sha), sha=None)
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            return self.fs_create(session.project, rel, "file", text, mode=int(version["mode"], 8) & 0o777)
        return self.fs_write(session.project, rel, text, sha, exact=True)

    def review_dispatch(self, sid: str, body: dict) -> dict:
        """Recheck every target before sending one batch to the current or a child session. The agent
        rule applies here (not the GUI's): a target hidden from the session is refused - its path, ranges
        and code never reach a prompt."""
        session = self.get(sid)
        text, target, items = body.get("text"), body.get("target"), body.get("items")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError("review prompt must be 1..20000 characters; split the batch by file")
        if target not in ("current", "new") or not isinstance(items, list) or not items or len(items) > 200:
            raise ValueError("choose a session and 1..200 review targets")
        top = self._turn_top(session)
        agent_hidden, mark = self.hidden_for(session), self._session_hidden(session)
        live = gitops.status(top, mark)["files"]   # a rename from a hidden file: its old side is hidden too
        # ...and a rename committed on the branch (scope Entire branch / MR, R5 2026-10-09): not in the live
        # status, only in this session's pinned reviews - the GUI's "original side" code comes from those rows
        with self._review_lock:
            pinned = [r for v in self._reviews.values() if v["session"] == sid and v["top"] == top
                      for r in v["public"]["files"]]
        hidden_names = {n for r in live + pinned for n in (r.get("path"), r.get("orig"))
                        if n and (agent_hidden(n) or mark(n))}
        # a path boundary on both sides: src/notes.txt does not name a hidden notes.txt at the top
        named = sorted(n for n in hidden_names if re.search(r"(?<![\w./-])%s(?![\w./-])" % re.escape(n), text))
        if named:   # the prompt itself (a template's {{file}} / {{code}}) must not carry a hidden file either
            raise ValueError("the review prompt names %s, which is hidden from Claude - review it yourself, it "
                             "cannot go to a session" % named[0])
        with self._write_lock:
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str) or "sha" not in item:
                    raise ValueError("each review target needs a path and its working content version")
                _, root, _, _ = self._pfile(session.project, item["path"])
                # the lexical path, like review_files' work_sha (a symlink row hashes its link text)
                name = os.path.relpath(os.path.join(root, os.path.normpath(item["path"].strip())), top)
                self._review_path(session, top, name)
                origs = {r.get("orig") for r in live + pinned if r.get("path") == name and r.get("orig")}
                if agent_hidden(name) or mark(name) or any(agent_hidden(o) or mark(o) for o in origs):
                    raise ValueError("%s is hidden from Claude - review it yourself, it cannot go to a session"
                                     % item["path"])
                if self._working_version(session, top, name)["sha"] != item["sha"]:
                    raise ValueError("%s changed since review; refresh this target" % item["path"])
            if body.get("edit") is not False:   # an editing task must not race another agent in this tree
                busy = [x for x in self._same_tree(top) if x.state == "working" and x.id != sid]
                if busy:
                    raise ValueError("session \"%s\" is working in this tree - send the task when it waits" % busy[0].title)
            if target == "current":
                if session.state == "working":
                    raise ValueError("session is working; keep the draft and send when it waits")
                self.send(sid, text)
                return {"session": sid, "sent": True}
            child = self.create(session.project, text, "Review: " + session.title[:150],
                                parent=sid, agent=session.agent, opts=session.opts)
            return {"session": child.id, "sent": True, "agent": child.agent}

    def review_history(self, sid: str, rid: str, path: str) -> dict:
        session, review = self._review(sid, rid)
        row = next((r for r in review["public"]["files"] if r["path"] == path), None)
        if not row or row["withheld"]:
            raise projectfs.FsError("file is not visible in this review")
        for name in (row["path"], row.get("orig")):
            if name:
                self._review_path(session, review["top"], name)
        rel = os.path.relpath(os.path.join(review["top"], path), os.path.realpath(session.path))
        return self.turn_file_history(sid, rel, live_to=review["public"]["to"])

    def review_commit(self, sid: str, message: str, files: list[str], reviewed: dict, amend: bool = False) -> dict:
        """Refuse stale HEAD, path, content or mode, then commit only the pinned selected blobs."""
        if not isinstance(reviewed, dict):
            raise ValueError("reviewed versions are required; open the review pane")
        session, review = self._review(sid, reviewed.get("review", ""))
        public, top = review["public"], review["top"]
        requested = reviewed.get("versions")
        if reviewed.get("head") != public["head"] or not isinstance(requested, dict) or set(requested) != set(files) \
                or not (files or amend):
            raise ValueError("reviewed versions must match every selected path, including both rename paths")
        allowed = set()
        for row in public["files"]:
            if row["path"] in files:
                if row["withheld"] or not row.get("committable"):
                    raise ValueError("file cannot be committed from this review; refresh")
                allowed.add(row["path"])
                if row.get("orig"):
                    if row["orig"] not in files:
                        raise ValueError("a rename needs both reviewed paths")
                    allowed.add(row["orig"])
        if allowed != set(files) or any(requested[p] != review["versions"].get(p) for p in files):
            raise ValueError("selected paths do not match their reviewed versions")
        for path in files:
            self._review_path(session, top, path)
        with self._write_lock:
            check = self.review_check(sid, public["id"])
            if check["head_changed"]:
                raise ValueError("HEAD changed since review; refresh")
            if set(files) & set(check["changed"]):
                raise ValueError("selected file content or mode changed since review; refresh")
            # Snapshot and compare clean-filtered Git blobs as well as raw bytes/mode above.
            current = (gitops.snapshot(top, os.path.join(session.dir, "review-staged-verify.index"), review["objects"], staged=True)
                       if public["scope"] == "index" else self._snapshot(session, "review-verify.index", raw=review["raw"]))
            actual = gitops.tree_versions(top, current, files, review["objects"])
            if actual != requested:
                raise ValueError("selected file versions changed since review; refresh")
            return gitops.commit_reviewed(top, message, public["head"], requested, review["objects"],
                                          os.path.join(session.dir, "review-commit-" + public["id"] + ".index"), amend=amend,
                                          keep_index=public["scope"] == "index")

    # ------------------------------------------------------------ diagnostics (9.7 #9)
    def diagnostics(self, sid: str) -> dict:
        s = self.get(sid)
        src = self._transcript_src(s)
        if src is None:
            return {"at": None, "source": "", "items": [], "note": "the Claude session ID is not known yet"}
        path = self._transcript_file(*src)
        try:
            key = (path, os.path.getmtime(path) if path else 0.0)
        except OSError:
            key = (path, 0.0)
        hit = self._diag_cache.get(s.id)
        if hit and hit[0] == key:
            return hit[1]
        res = diagnostics.from_records(digest._records(*src))
        res["items"] = diagnostics.relativize(res["items"], s.path, self._class_resolver(s), self._rel_resolver(s))
        if not res["items"]:
            res["note"] = "the session's last Bash output has no compile or test errors"
        self._diag_cache[s.id] = (key, res)
        return res

    def _rel_resolver(self, s):
        """A relative path as printed (maybe from a sub-directory of the project) -> the project
        file it names (quick open's list): as is when it exists, else the one file ending in /<path>."""
        cache: dict = {}

        def resolve(rel: str) -> str | None:
            if "f" not in cache:
                try:
                    cache["f"] = self.fs(s.project, "files", {})["files"] if s.kind == "project" else []
                except (KeyError, ValueError, OSError):
                    cache["f"] = []
            files = cache["f"]
            while rel.startswith("./"):
                rel = rel[2:]
            if rel in files:
                return rel
            hits = [f for f in files if f.endswith("/" + rel)]
            return hits[0] if len(hits) == 1 else None
        return resolve

    def _class_resolver(self, s):
        """junit class name -> a source file of the project (quick open's list; shortest match, tests first)."""
        cache: dict = {}

        def files() -> list[str]:
            if "f" not in cache:
                try:
                    cache["f"] = self.fs(s.project, "files", {})["files"] if s.kind == "project" else []
                except (KeyError, ValueError, OSError):
                    cache["f"] = []
            return cache["f"]

        def resolve(name: str) -> str | None:
            base = name.split("$")[0]
            suffix = base.replace(".", "/")
            hits = [f for f in files() if re.search(r"(^|/)%s\.(java|kt|groovy|scala)$" % re.escape(suffix), f)]
            hits.sort(key=lambda f: (0 if "/test/" in "/" + f else 1, len(f)))
            return hits[0] if hits else None
        return resolve

    # ------------------------------------------------------------ IntelliJ (9.7 #10)
    def idea_status(self, fresh: bool = False) -> dict:
        hit = self._idea_status
        if hit and not fresh and time.time() - hit[0] < ideamcp.STATUS_TTL:
            return hit[1]
        conf = str(self.policy.get("idea_mcp_url") or "").strip()
        urls = [conf] if conf else list(ideamcp.DEFAULT_URLS)
        note = ""
        with self._idea_lock:
            self._idea_client = None
            for u in urls:
                if not ideamcp.loopback(u):
                    note = "policy idea_mcp_url must be http://127.0.0.1:<port>/…"
                    continue
                try:
                    c = ideamcp.Client(u, opener=self.idea_opener)
                    c.initialize()
                except ideamcp.McpError as e:
                    note = "IntelliJ MCP server not reachable at %s (%s) - IDEA 2025.2+, Settings > Tools > " \
                           "MCP Server" % (u, e)
                    continue
                self._idea_client = c
                st = {"available": True, "url": u, "note": c.server}
                break
            else:
                if not conf and any(ideamcp.legacy_only(u, self.idea_opener) for u in ideamcp.LEGACY_URLS):
                    note = "IntelliJ offers only the legacy SSE transport; piperd needs streamable HTTP (/stream)"
                st = {"available": False, "url": urls[0], "note": note}
        self._idea_status = (time.time(), st)
        return st

    def _idea_call(self, tool: str, args: dict) -> dict | None:
        if not self.idea_status()["available"]:
            return None
        with self._idea_lock:
            c = self._idea_client
            if c is None:
                return None
            try:
                return c.call(tool, args)
            except ideamcp.McpError as e:
                self._idea_status = (time.time(), {"available": False, "url": c.url, "note": str(e)})
                self._idea_client = None
                return None

    def idea_problems(self, key: str, path: str) -> dict:
        p, root, rel, full = self._pfile(key, path)   # `.git` / outside: 404 before IDEA hears of it
        r = self._idea_call("get_file_problems", {"filePath": rel, "errorsOnly": False, "timeout": 2000,
                                                  "projectPath": p["path"]})
        if r is None:
            return {"available": False, "problems": [], "note": self.idea_status()["note"]}
        if r["error"]:
            return {"available": True, "problems": [], "note": r["text"].strip().splitlines()[0][:300]
                    if r["text"].strip() else "IntelliJ refused"}
        out = {"available": True, "problems": ideamcp.problems(r["data"], r["text"])}
        if (r["data"] or {}).get("timedOut"):
            out["note"] = "IntelliJ timed out - the list may be incomplete"
        return out

    def idea_symbol(self, key: str, path: str, line: int, col: int) -> dict:
        """The GUI's view: a symbol declared in a file hidden from Claude is shown like any other."""
        p, root, rel, full = self._pfile(key, path)
        line, col = int(line), int(col)
        if line < 1 or col < 1:
            raise ValueError("line and col are 1-based")
        r = self._idea_call("get_symbol_info", {"filePath": rel, "line": line, "column": col,
                                                "projectPath": p["path"]})
        if r is None:
            return {"available": False, "info": "", "note": self.idea_status()["note"]}
        if r["error"]:
            return {"available": True, "info": "", "note": r["text"].strip()[:300]}
        data = r["data"] or {}
        info = data.get("symbolInfo") if isinstance(data.get("symbolInfo"), dict) else {}
        text = str(data.get("documentation") or (r["text"] if not data else "") or "")
        decl_text = str(info.get("declarationText") or "")
        if decl_text and decl_text not in text:
            text = (text + "\n\n" + decl_text).strip()
        return {"available": True, "info": text[:20000]}

    # ------------------------------------------------------------ usage (9.7 #11)
    def usage(self) -> dict:
        srcs = []
        for s in sorted(list(self.sessions.values()), key=lambda x: x.created):
            if s.hidden and s.kind != "prodcheck":   # the morning check hides itself when done, its tokens still count
                continue
            src = self._transcript_src(s)
            path = self._transcript_file(*src) if src else None
            if not path:
                continue
            try:
                m = os.path.getmtime(path)
            except OSError:
                continue
            srcs.append((s, src, (s.id, path, m)))
        key = tuple(x[2] for x in srcs)
        if self._usage_cache and self._usage_cache[0] == key:
            return self._usage_cache[1]
        zero = lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}   # noqa: E731
        seen: set = set()
        days: dict[str, dict] = {}
        projs: dict[str, dict] = {}
        total = zero()
        for s, src, _ in srcs:
            per = digest.usage_by_day(src[0], src[1], seen)
            pr = projs.setdefault(s.project, dict(zero(), project=s.project, sessions=0))
            pr["sessions"] += 1
            for day, row in per.items():
                d = days.setdefault(day, dict(zero(), day=day))
                for k, v in row.items():
                    d[k] += v
                    pr[k] += v
                    total[k] += v
        tok = lambda r: r["input"] + r["output"] + r["cache_read"] + r["cache_write"]   # noqa: E731
        out = {"days": sorted(days.values(), key=lambda r: r["day"], reverse=True)[:60],
               "projects": sorted(projs.values(), key=lambda r: -tok(r)), "total": total}
        self._usage_cache = (key, out)
        return out
