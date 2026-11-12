"""Notes (module `notes`): the human's own plain files in one folder on this computer.

A quick line goes into the main note (`notes.md`) under a heading per day; a snippet (JSON, a log, a
request) becomes a named file next to it. The folder is a human-readable path (default
~/Documents/Piper Notes on macOS, ~/Piper Notes elsewhere) so a file can be found in any file dialog and
sent by mail. It is never a project, never a session's path and never in a sandbox; piperd serves it to
the full GUI token only (and, from phase 4, to an agent a human allowed on a card - through these
functions, never through the filesystem).

Rules:
  - configuration in $PIPER_HOME/notes/config.json (Settings > Notes): `dir`, `main`, `stamp`;
  - the folder is refused inside Piper's state, the agents' homes, extension `protected` / `forbidden`
    paths, `.git`, a registered project or a live session's checkout - and when it holds one of them
    (the Files protection of workbench: _gui_protected, _refused, _ext_holds, _ext_unowned), checked on
    every operation, not only when saved;
  - the folder is created with the first note, never when the module is switched on;
  - I/O without following a link (common.safe_*, O_NOFOLLOW below a directory fd); a new file is created
    with O_EXCL and never replaces one; a file is rewritten only with the sha it was read with;
  - a permission refusal (macOS privacy protection of ~/Documents for a background process: EPERM) is the
    distinct state "denied" (Denied) - Piper never tries to get around it;
  - the audit records names and byte counts, never the text.

Every function takes the Manager `m` (sessions.Manager: the workbench mixin's protection helpers and its
`_write_lock`, which Files' ext_write holds too - an append never lands between its read and rename).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime

import common
import projectfs

STORE = "json:notes/config"   # settings.py's store of the Notes section
CONFIG_FILE = os.path.join(common.PIPER_HOME, "notes", "config.json")
FALLBACK_DIR = "~/Piper Notes"


def default_dir(platform: str | None = None) -> str:
    """macOS: Documents is in the sidebar of every file dialog; elsewhere a folder in the home."""
    return "~/Documents/Piper Notes" if (platform or sys.platform) == "darwin" else FALLBACK_DIR


DEFAULT_DIR = default_dir()
MAIN = "notes.md"
STAMPS = ("time", "datetime", "none")   # time: `## YYYY-MM-DD` once a day + `- HH:MM text`

TEXT_MAX = 64 << 10            # one appended note
FILE_MAX = 2 << 20             # a created / rewritten file, and the main note an append still extends
READ_MAX = projectfs.MAX_READ  # read
LIST_MAX = projectfs.MAX_LIST  # names inspected by a listing
TAIL = 64 << 10                # the end of the main note an append reads for the day heading
SEARCH_FILES = 500
SEARCH_FILE_BYTES = 1 << 20
SEARCH_TOTAL = 16 << 20
SEARCH_HITS = 200
SEARCH_SECONDS = 1.0           # latency budget of the GUI
Q_MAX = 200

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
HEADING_RX = re.compile(rb"^## ([^\r\n]*?)[ \t]*\r?$", re.M)
EXT_RX = re.compile(r"^[A-Za-z0-9]{1,10}$")
REFUSE_TAIL = "Choose a folder outside projects and Piper's data."

_MANAGER = None   # piperd binds its Manager: Settings' field checks need the protection helpers


def bind(manager) -> None:
    global _MANAGER
    _MANAGER = manager


class Refused(ValueError):
    """The folder or a name is not allowed (409 with the reason)."""


class Denied(OSError):
    """The OS refused access (EPERM / EACCES): `info` is the state the GUI shows (Settings board 09)."""

    def __init__(self, info: dict):
        super().__init__(errno.EPERM, info["message"])
        self.info = info


# ------------------------------------------------------------------ paths and names
def expand(path) -> str:
    """The absolute, normalised folder of a configured value (`~` = the user's home); ValueError otherwise."""
    if not isinstance(path, str) or not path.strip() or "\0" in path or "\n" in path or len(path) > 1000:
        raise Refused("a folder path is required")
    path = path.strip()
    if path == "~" or path.startswith("~/"):
        path = os.path.join(common.REAL_HOME, path[2:])
    if not os.path.isabs(path):
        raise Refused("%s is not an absolute path (start it with / or ~/)" % path)
    return os.path.normpath(path)


def display(path: str) -> str:
    home = os.path.realpath(common.REAL_HOME).rstrip(os.sep)
    for h in {home, common.REAL_HOME.rstrip(os.sep)}:
        if path == h:
            return "~"
        if path.startswith(h + os.sep):
            return "~/" + path[len(h) + 1:]
    return path


def check_name(name) -> str:
    """One file name directly in the folder: no path, no leading dot, no control characters."""
    if not isinstance(name, str):
        raise Refused("a file name is required")
    name = name.strip()
    if not name or len(name) > 120 or len(name.encode("utf-8")) > 255:
        raise Refused("a file name of 1-120 characters is required")
    if name.startswith(".") or "/" in name or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise Refused("%r: a plain file name, without a folder, a leading dot or control characters" % name)
    return name


def _stem(name: str) -> str:
    """A file name made safe: path separators and control characters become '-', no leading dots."""
    name = re.sub(r"[/\\\x00-\x1f\x7f]+", "-", name or "").strip().lstrip(".").strip()
    return re.sub(r"\s+", " ", name)[:100].rstrip(" .")


def infer_ext(text: str) -> str:
    """json for a JSON object or array, otherwise md."""
    s = (text or "").strip()
    if s[:1] in ("{", "[") and len(s) <= FILE_MAX:
        try:
            if isinstance(json.loads(s), (dict, list)):
                return "json"
        except ValueError:
            pass
    return "md"


# ------------------------------------------------------------------ configuration
def config() -> dict:
    """{dir, main, stamp} - a broken or hand-edited value falls back to its default."""
    raw = common.read_json(CONFIG_FILE, {})
    raw = raw if isinstance(raw, dict) else {}
    d = raw.get("dir") if isinstance(raw.get("dir"), str) and raw.get("dir").strip() else DEFAULT_DIR
    try:
        main = check_name(raw.get("main", MAIN))
    except Refused:
        main = MAIN
    stamp = raw.get("stamp") if raw.get("stamp") in STAMPS else "time"
    return {"dir": d.strip(), "main": main, "stamp": stamp}


def check_dir(m, path) -> str:
    """The real folder of `path` when notes may live there; Refused with the reason otherwise."""
    full = expand(path)
    real = os.path.realpath(full)
    shown = display(real)
    if real == os.sep or real == os.path.realpath(common.REAL_HOME):
        raise Refused("%s is too wide for notes. %s" % (shown, REFUSE_TAIL))
    g = m._gui_protected()
    why = m._refused(real, g)
    if why:
        raise Refused("%s is %s. %s" % (shown, "a .git path" if ".git" in why else
                                        "Piper's own data, an agent's home or a protected folder", REFUSE_TAIL))
    try:
        m._ext_holds(real, g)
    except projectfs.FsError:
        raise Refused("%s holds Piper's own data or an agent's home. %s" % (shown, REFUSE_TAIL)) from None
    try:
        m._ext_unowned(real, m._ext_owners(), tree=True)
    except ValueError as e:
        msg = str(e).replace(real, shown).replace(" - change it there", "").replace(" - not changed from outside it", "")
        raise Refused("%s. %s" % (msg.replace("inside project", "inside the project").replace("holds project", "holds the project"),
                                  REFUSE_TAIL)) from None
    return real


def check_setting_dir(value: str) -> str | None:
    """Settings > Notes folder: the error text or None (piperd's Manager bound)."""
    if _MANAGER is None:
        return "the folder can be checked only while Piper runs"
    try:
        check_dir(_MANAGER, value)
    except ValueError as e:
        return str(e)
    return None


def check_setting_name(value: str) -> str | None:
    try:
        check_name(value)
    except ValueError as e:
        return str(e)
    return None


def _denied(real: str, e: OSError) -> Denied:
    tcc = sys.platform == "darwin" and e.errno == errno.EPERM
    shown = display(real)
    msg = ("macOS did not allow Piper to use %s. Piper runs in the background, so macOS cannot ask you: allow %s in "
           "System Settings > Privacy & Security > Files and Folders, then try again - or keep your notes in %s."
           % (shown, os.path.basename(sys.executable) or "python3", FALLBACK_DIR)) if tcc else \
        "No permission to use %s (%s)." % (shown, os.strerror(e.errno or errno.EACCES))
    return Denied({"state": "denied", "dir": shown, "path": real, "message": msg, "privacy": tcc,
                   "process": os.path.basename(sys.executable) or "python3", "fallback": FALLBACK_DIR})


def _ancestor(real: str) -> str:
    p = real
    while not os.path.lexists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


PRIVACY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"


def open_privacy() -> dict:
    """POST /api/notes/privacy: open System Settings > Privacy & Security > Files and Folders on this Mac (where
    piperd runs and macOS refused the folder) - that one fixed pane, nothing taken from the request. The host opens it,
    so it works from any client (a desktop app that predates the URL in its own policy dropped it silently)."""
    if sys.platform != "darwin":
        raise ValueError("Privacy settings exist only on macOS")
    if subprocess.run(["open", PRIVACY_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode:
        raise OSError(errno.EIO, "macOS could not open System Settings")
    return {"ok": True}


def _probe(real: str) -> bool:
    """True when the folder exists. A missing one: the nearest existing folder above must be readable
    (macOS refuses opening ~/Documents to a background process: the state shows before the first note)."""
    try:
        st = os.stat(real, follow_symlinks=False)
    except FileNotFoundError:
        try:
            with os.scandir(_ancestor(os.path.dirname(real))) as it:
                next(it, None)
        except PermissionError as e:
            raise _denied(real, e) from None
        except OSError:
            pass
        return False
    except PermissionError as e:
        raise _denied(real, e) from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused("%s is not a folder. %s" % (display(real), REFUSE_TAIL))
    try:
        os.close(os.open(real, _DIRFLAGS))
    except PermissionError as e:
        raise _denied(real, e) from None
    return True


def _mkdirs(real: str) -> None:
    """Create the folder (and missing parents) below the nearest existing one, never through a link."""
    base = _ancestor(real)
    parts = [p for p in os.path.relpath(real, base).split(os.sep) if p not in ("", ".")]
    try:
        fd = os.open(base, _DIRFLAGS)
        try:
            for name in parts:
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nfd = os.open(name, _DIRFLAGS, dir_fd=fd)
                os.close(fd)
                fd = nfd
        finally:
            os.close(fd)
    except PermissionError as e:
        raise _denied(real, e) from None
    common.audit({"event": "notes-folder-created", "path": display(real)})


def _root(m, create: bool = False) -> tuple[str, dict, bool]:
    """(real folder, config, exists); create: make the folder now (the first note)."""
    cfg = config()
    real = check_dir(m, cfg["dir"])
    exists = _probe(real)
    if not exists and create:
        _mkdirs(real)
        exists = _probe(real)
    return real, cfg, exists


def _target(m, real: str, name) -> str:
    name = check_name(name)
    if name.lower() == ".git" or m._refused(os.path.join(real, name), m._gui_protected(), ancestors=False):
        raise Refused("%s is a protected name" % name)
    return name


def key_of(real: str) -> str:
    """The Files key of the folder (workbench external roots: open, rename, trash, Save as...)."""
    return projectfs.EXTERNAL_PREFIX + real


# ------------------------------------------------------------------ listing and status
def _list(m, real: str, main: str) -> tuple[list[dict], bool]:
    g = m._gui_protected()
    try:
        fd = common._open_dir(real, [])
    except PermissionError as e:
        raise _denied(real, e) from None
    rows = []
    try:
        try:
            names = os.listdir(fd)
        except PermissionError as e:
            raise _denied(real, e) from None
        for name in sorted(names)[:LIST_MAX]:
            if name.startswith("."):   # .DS_Store, a write's temporary file, hidden files
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or m._refused(os.path.join(real, name), g, ancestors=False, st=st):
                continue   # a link, a folder, a protected identity
            rows.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "main": name == main})
    finally:
        os.close(fd)
    rows.sort(key=lambda r: (not r["main"], -r["mtime"], r["name"]))
    return rows, len(names) > LIST_MAX


def status(m, path: str | None = None) -> dict:
    """GET /api/notes (and POST /api/notes/check with a candidate `path`, nothing saved or created):
    state ok | missing (no note yet) | denied (the OS refused) | refused (not allowed), the folder, the files."""
    cfg = config()
    d = cfg["dir"] if path is None else path
    out = {"dir": d, "main": cfg["main"], "stamp": cfg["stamp"], "default": DEFAULT_DIR, "fallback": FALLBACK_DIR,
           "path": None, "key": None, "files": [], "count": 0, "truncated": False, "message": ""}
    try:
        real = check_dir(m, d)
    except ValueError as e:
        return dict(out, state="refused", message=str(e))
    out.update(path=real, shown=display(real))
    try:
        if not _probe(real):
            return dict(out, state="missing", message="Created when you save the first note.")
        files, truncated = _list(m, real, cfg["main"])
    except Denied as e:
        return dict(out, **e.info, shown=display(real))
    except Refused as e:
        return dict(out, state="refused", message=str(e))
    for f in files:
        if f["main"]:
            f["last"] = _last_line(real, f)
    return dict(out, state="ok", key=key_of(real), files=files, count=len(files), truncated=truncated)


LAST_TAIL = 4096


def _last_line(real: str, row: dict) -> str:
    """The main note's newest entry for the phone list (board 07: `14:32 Capture keeps…`): the last line that is not
    blank or a heading, from the file's last 4 KiB (no-follow), its list marker dropped; "" when unreadable."""
    try:
        raw = common.safe_read(real, row["name"], LAST_TAIL, offset=max(0, row["size"] - LAST_TAIL))
    except OSError:
        return ""
    for line in reversed(raw.decode("utf-8", "replace").splitlines()):
        s = line.strip()
        if s and not s.startswith("#"):
            return re.sub(r"^[-*+]\s+", "", s)[:160]
    return ""


def list_files(m) -> dict:
    """{path, key, files, truncated} for an agent tool (phase 4): Denied / Refused raised."""
    real, cfg, exists = _root(m)
    files, truncated = _list(m, real, cfg["main"]) if exists else ([], False)
    return {"path": real, "key": key_of(real) if exists else None, "files": files, "truncated": truncated}


# ------------------------------------------------------------------ read
def read(m, name) -> dict:
    """{name, text, sha, size, mtime} of one note (READ_MAX); FsError / TooLarge / Binary like Files."""
    real, _, exists = _root(m)
    name = _target(m, real, name)
    if not exists:
        raise projectfs.FsError("%s does not exist (no note yet)" % name)
    try:
        st = common.safe_stat(real, name)
        if st.st_size > READ_MAX:
            raise projectfs.TooLarge("%s is larger than 2 MiB - not opened" % name)
        raw = common.safe_read(real, name, READ_MAX + 1)
    except PermissionError as e:
        raise _denied(real, e) from None
    except FileNotFoundError:
        raise projectfs.FsError("%s does not exist" % name) from None
    except OSError:
        raise projectfs.FsError("%s is not a regular file (a link or a folder)" % name) from None
    if len(raw) > READ_MAX:
        raise projectfs.TooLarge("%s is larger than 2 MiB - not opened" % name)
    return {"name": name, "text": projectfs.editable_text(raw, name), "sha": hashlib.sha256(raw).hexdigest(),
            "size": len(raw), "mtime": st.st_mtime, "key": key_of(real)}


# ------------------------------------------------------------------ append
def _note_text(text) -> str:
    if not isinstance(text, str):
        raise ValueError("text is required")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\0" in text:
        raise ValueError("a note cannot contain a NUL character")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise ValueError("the note is empty")
    lines[0] = lines[0].lstrip()
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > TEXT_MAX:
        raise ValueError("a note larger than 64 KiB - save it as a file")
    return out


def _entry(text: str, stamp: str, now: datetime) -> list[str]:
    """`- HH:MM first line` and the following lines indented as its continuation."""
    prefix = {"time": now.strftime("%H:%M "), "datetime": now.strftime("%Y-%m-%d %H:%M "), "none": ""}[stamp]
    lines = text.split("\n")
    return ["- " + prefix + lines[0]] + [("  " + line) if line else "" for line in lines[1:]]


def append(m, text, name: str | None = None, now: datetime | None = None) -> dict:
    """POST /api/notes/append: one entry at the end of the main note (or `name`), newest at the bottom; with the
    stamp `time` the heading `## YYYY-MM-DD` first when the last heading of the file is not today's. Creates the
    folder and the file when missing. Returns {name, sha, size, heading, line, key, path}."""
    text = _note_text(text)
    now = now or datetime.now()
    with m._write_lock:   # Files' ext_write holds it from its read to its rename
        real, cfg, _ = _root(m, create=True)
        name = _target(m, real, name if name else cfg["main"])
        try:
            dfd = common._open_dir(real, [])
            try:
                fd = os.open(name, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW | os.O_NONBLOCK, 0o644, dir_fd=dfd)
            finally:
                os.close(dfd)
        except PermissionError as e:
            raise _denied(real, e) from None
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.EMLINK):
                raise Refused("%s is a symbolic link - notes are not written through it" % name) from None
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise Refused("%s is not a regular file" % name)
            size = st.st_size
            if size > FILE_MAX:
                raise Refused("%s is larger than 2 MiB - choose a new main note in Settings" % name)
            tail = os.pread(fd, min(size, TAIL), size - min(size, TAIL)) if size else b""
            eol = "\r\n" if b"\r\n" in tail else "\n"
            day = now.strftime("%Y-%m-%d")
            heads = HEADING_RX.findall(tail)
            heading = cfg["stamp"] == "time" and (not heads or heads[-1].decode("utf-8", "replace").strip() != day)
            out = ""
            if size and not tail.endswith(b"\n"):
                out += eol
            if heading:
                if size and not tail.endswith(b"\n\n") and not tail.endswith(b"\r\n\r\n"):
                    out += eol
                out += "## " + day + eol + eol
            entry = _entry(text, cfg["stamp"], now)
            out += eol.join(entry) + eol
            data = out.encode("utf-8")
            common._write_all(fd, data)
            os.fsync(fd)
            total = size + len(data)
            sha = hashlib.sha256(os.pread(fd, total, 0)).hexdigest() if total <= READ_MAX else None
        finally:
            os.close(fd)
    common.audit({"event": "notes-append", "name": name, "bytes": len(data), "heading": heading})
    return {"name": name, "sha": sha, "size": total,
            "heading": heading, "line": entry[0], "key": key_of(real), "path": os.path.join(real, name)}


# ------------------------------------------------------------------ create / write
def create(m, text, name: str = "", ext: str = "", now: datetime | None = None, line: bool = False) -> dict:
    """POST /api/notes/file: a new file from `text`, never over an existing one (O_EXCL): `name` taken -> name-2,
    name-3 ... The extension: `ext`, else the name's own, else json for a JSON object/array, else md. No name:
    note-YYYY-MM-DD-HHMM. line: also append "Stashed <file>" to the main note. Returns {name, sha, size, key, path,
    renamed[, line]}."""
    if not isinstance(text, str):
        raise ValueError("text is required")
    data = text.encode("utf-8")
    if len(data) > FILE_MAX:
        raise ValueError("text larger than 2 MiB")
    now = now or datetime.now()
    if ext:
        ext = str(ext).lstrip(".")
        if not EXT_RX.match(ext):
            raise ValueError("ext: 1-10 letters or digits")
    stem = _stem(str(name or ""))
    base, dot, own = stem.rpartition(".")
    if dot and base and EXT_RX.match(own) and not ext:
        stem, ext = base, own
    ext = ext or infer_ext(text)
    stem = stem or now.strftime("note-%Y-%m-%d-%H%M")
    with m._write_lock:
        real, cfg, _ = _root(m, create=True)
        for i in range(1, 101):
            cand = _target(m, real, "%s%s.%s" % (stem, "" if i == 1 else "-%d" % i, ext))
            try:
                common.safe_write(real, cand, data, create=True)
                break
            except FileExistsError:
                continue
            except PermissionError as e:
                raise _denied(real, e) from None
        else:
            raise ValueError("%s.%s and 99 numbered names are taken - choose another name" % (stem, ext))
    common.audit({"event": "notes-create", "name": cand, "bytes": len(data)})
    out = {"name": cand, "sha": hashlib.sha256(data).hexdigest(), "size": len(data), "key": key_of(real),
           "path": os.path.join(real, cand), "renamed": cand != "%s.%s" % (stem, ext)}
    if line:
        out["line"] = append(m, "Stashed " + cand, now=now)
    return out


def write(m, name, text, sha) -> dict:
    """POST /api/notes/write: rewrite an existing note only when `sha` is the sha256 it was read with (Files'
    ext_write: the lock, its line endings and mode, atomic). Returns {name, sha, total_lines}."""
    real, _, exists = _root(m)
    name = _target(m, real, name)
    if not exists:
        raise projectfs.FsError("%s does not exist (no note yet)" % name)
    if isinstance(text, str) and len(text.encode("utf-8")) > FILE_MAX:
        raise ValueError("text larger than 2 MiB")
    try:
        out = m.ext_write(key_of(real), name, text, sha)
    except PermissionError as e:
        raise _denied(real, e) from None
    return dict(out, name=name)


# ------------------------------------------------------------------ search
def search(m, q) -> dict:
    """GET /api/notes/search: case-insensitive text in file names and lines, bounded (SEARCH_*): {q, names, hits:
    [{name, line, text}], truncated, skipped}."""
    if not isinstance(q, str) or not q.strip() or len(q) > Q_MAX:
        raise ValueError("a search text of 1-%d characters is required" % Q_MAX)
    q = q.strip()
    needle = q.casefold()
    real, cfg, exists = _root(m)
    out = {"q": q, "names": [], "hits": [], "truncated": False, "skipped": 0}
    if not exists:
        return out
    rows, truncated = _list(m, real, cfg["main"])
    out["truncated"] = truncated or len(rows) > SEARCH_FILES
    deadline, total = time.monotonic() + SEARCH_SECONDS, 0
    for r in rows[:SEARCH_FILES]:
        if needle in r["name"].casefold():
            out["names"].append(r["name"])
        if r["size"] > SEARCH_FILE_BYTES:
            out["skipped"] += 1
            continue
        total += r["size"]
        if total > SEARCH_TOTAL or time.monotonic() > deadline:
            out["truncated"] = True
            break
        try:
            raw = common.safe_read(real, r["name"], SEARCH_FILE_BYTES + 1)
        except PermissionError as e:
            raise _denied(real, e) from None
        except OSError:
            continue
        if b"\0" in raw[:8192]:
            continue
        for i, text in enumerate(raw.decode("utf-8", "replace").splitlines()):
            if needle in text.casefold():
                out["hits"].append({"name": r["name"], "line": i + 1, "text": text.strip()[:300]})
                if len(out["hits"]) >= SEARCH_HITS:
                    out["truncated"] = True
                    return out
    return out


# ------------------------------------------------------------------ agent tools (phase 4)
# MCP tools of a project session (piper-mcp lists them with --notes, while the module is on). piperd serves them
# through the functions above - an agent never gets the folder through its filesystem or sandbox. The first call
# of a session asks the human on a card (kind ACCESS); Allow grants this session read and write until it ends
# (Session.approvals, Revoke in the session inspector). Nothing here deletes: the trash is the GUI's.
TOOLS = ("notes_list", "notes_read", "notes_append", "notes_write")
ACCESS = "notes_access"
RIGHTS = "read and write · no delete"
ASK_S = 780   # within piper-mcp's 15 min wait for a carded tool (CONFIRMED)
ACCESS_WHY = {"group": "", "label": "Notes are yours: a session reads them only with your consent", "rule": None,
              "detail": "This card is always asked, whatever Settings › Security says. Allow covers this session "
                        "until it ends or you revoke it (session inspector › Approval grants); other sessions ask "
                        "again. Agents never delete a note."}
_ASKING: dict = {}   # session id -> lock: concurrent first calls of one session wait for the one card


def access_scope(m) -> str:
    """The Session.approvals entry of a granted session (approval_scope of ACCESS)."""
    return m.approval_scope(ACCESS, {})


def drop_access(m, s) -> bool:
    """End a session's grant (the session ended). True when there was one; the caller saves the session."""
    scope = access_scope(m)
    if scope in (s.approvals or []):
        s.approvals.remove(scope)
        return True
    return False


def _state_error(e: Exception) -> ValueError:
    """The folder's state for the agent: macOS refused / not allowed - said once, never a card."""
    if isinstance(e, Denied):
        return ValueError("Notes are not available: %s Nothing was read or written; tell the user - do not retry or "
                          "use the shell." % e.info.get("message", ""))
    return ValueError("Notes are not available: %s Nothing was read or written; the user can change the folder in "
                      "Piper's Settings › Notes." % e)


def _wants(tool: str, args: dict, main: str) -> str:
    name = str(args.get("name") or "").strip()
    if tool == "notes_list":
        return "Wants to list your notes."
    if tool == "notes_read":
        return "Wants to read %s." % name
    if tool == "notes_append":
        return "Wants to add a line to %s." % (name or main)
    return ("Wants to change %s." if args.get("sha") else "Wants to write %s.") % name


def _grant(m, s, tool: str, args: dict, shown: str, main: str) -> None:
    """A card unless this session holds the grant. Always the human's: no Security switch, scoped profile or hour
    grant approves it (_force_approval); Allow keeps the grant for the session, Deny ends the call."""
    import evidence
    scope = access_scope(m)
    with m.lock:
        lock = _ASKING.setdefault(s.id, threading.Lock())
    with lock:
        with m.lock:
            if scope in s.approvals:
                return
        summary = "%s · %s\n%s Allowing lets this session list, read, add and change files in your Notes until it " \
                  "ends. Other sessions still ask." % (shown, RIGHTS, _wants(tool, args, main))
        payload = {"folder": shown, "rights": RIGHTS, "tool": tool, "_force_approval": True,
                   "_approval_reason": dict(ACCESS_WHY),
                   "evidence": evidence.box("What it gets", rows=[
                       {"text": "Folder %s · %s" % (shown, RIGHTS)},
                       {"text": "Tools: notes_list, notes_read, notes_append, notes_write · this session only"}])}
        res: dict = {}
        if not m.ask(ACCESS, s, summary, payload, timeout=ASK_S, result=res):
            if res.get("answered") is False:
                raise ValueError("nobody answered the Notes access card within %d minutes - nothing was read or "
                                 "written. Ask the user to watch the Piper GUI and call it again."
                                 % max(1, round(ASK_S / 60)))
            raise ValueError("the user denied this session access to Notes - nothing was read or written. Do not "
                             "retry, and never reach the folder another way (shell, file tools).")
        with m.lock:
            if scope not in s.approvals:
                s.approvals.append(scope)
                s.approvals.sort()
                try:
                    s.save()
                except Exception:
                    s.approvals.remove(scope)
                    raise
        common.audit({"event": "notes-access-granted", "session": s.id})
        m.bump()


def tool(m, s, name: str, args: dict):
    """One notes_* call of session `s` (sessions.Manager.tool). Module off, a wrong session kind or bad arguments:
    an error before any card; a folder macOS refuses or Piper does not allow: its state, never a card."""
    import features
    if not features.on("notes"):
        raise ValueError("the Notes module is off (Settings › Features) - there are no notes tools")
    if s.kind != "project":
        raise ValueError("notes tools are for project sessions")
    if name == "notes_read":
        check_name(args.get("name"))
    elif name == "notes_append":
        if not isinstance(args.get("text"), str) or not args["text"].strip():
            raise ValueError("text is required")
        if args.get("name") not in (None, ""):
            check_name(args.get("name"))
    elif name == "notes_write":
        check_name(args.get("name"))
        if not isinstance(args.get("text"), str):
            raise ValueError("text is required")
        if args.get("sha") not in (None, "") and not re.fullmatch(r"[0-9a-f]{64}", str(args["sha"])):
            raise ValueError("sha must be the sha256 notes_read returned")
    elif name != "notes_list":
        raise ValueError("unknown tool %r" % name)
    cfg = config()
    try:
        real = check_dir(m, cfg["dir"])
        _probe(real)
    except (Denied, Refused) as e:
        raise _state_error(e) from None
    shown = display(real)
    _grant(m, s, name, args, shown, cfg["main"])
    try:
        if name == "notes_list":
            out = list_files(m)
            return {"folder": shown, "files": out["files"], "truncated": out["truncated"]}
        if name == "notes_read":
            r = read(m, args["name"])
            return {k: r[k] for k in ("name", "text", "sha", "size", "mtime")}
        if name == "notes_append":
            r = append(m, args["text"], str(args.get("name") or "") or None)
            return {k: r[k] for k in ("name", "sha", "size", "heading", "line")}
        if args.get("sha"):
            text = args["text"]   # Files' ext_write keeps the file's own final newline and line endings:
            text = text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text   # read -> write
            try:
                return write(m, args["name"], text, str(args["sha"]))
            except ValueError as e:
                if "changed since" not in str(e):
                    raise
                raise ValueError("%s changed since you read it (the user or another session wrote it) - nothing was "
                                 "written. Read it again with notes_read and reapply your change." % args["name"]) from None
        r = create(m, args["text"], args["name"])
        return {k: r[k] for k in ("name", "sha", "size", "renamed")}
    except Denied as e:
        raise _state_error(e) from None
