"""Shared plumbing for piperd: locations, tokens, scrubbing, audit.

Everything Piper keeps lives under $PIPER_HOME (default ~/.piper). That
directory must never sit under a hole library or include mount (Guard L3 checks it), so
nothing a sandbox can write decides what piperd does.
"""
from __future__ import annotations

import errno
import json
import os
import re
import secrets
import sys
import threading
import time


def gui_proxy_timeout(method: str, path: str, default: int = 28) -> int:
    """Keep proxy deadlines above the GUI's supported DB limit, including preview + write.

    Each phase permits 3600 s plus worker/metadata setup; cancellation stays a separate short request.
    """
    endpoint = path.partition('?')[0]
    if (method == 'POST' and endpoint in ('/api/db/query', '/api/db/execute', '/api/db/tx') or
            method == 'GET' and endpoint == '/api/db/meta'):
        return 2 * 3600 + 180
    return default


# Piper was "the hole orchestrator" before the rename: ORCH_* names and ~/.hole-orch. A machine
# that still has them keeps working (an existing ~/.hole-orch holds its running sessions).
for _k in [k for k in os.environ if k.startswith("ORCH_")]:
    os.environ.setdefault("PIPER_" + _k[len("ORCH_"):], os.environ[_k])

REAL_HOME = os.environ.get("PIPER_REAL_HOME") or os.path.expanduser("~")
LEGACY_HOME = os.path.join(REAL_HOME, ".hole-orch")


def _home() -> str:
    """$PIPER_HOME, else ~/.piper - or ~/.hole-orch while only that one exists (migrate_home)."""
    env = os.environ.get("PIPER_HOME")
    if env:   # an old service file names ~/.hole-orch, a symlink to ~/.piper once migrated
        new = os.path.join(REAL_HOME, ".piper")
        migrated = env == LEGACY_HOME and os.path.islink(env) and os.path.realpath(env) == os.path.realpath(new)
        return new if migrated else env
    new = os.path.join(REAL_HOME, ".piper")
    return LEGACY_HOME if not os.path.isdir(new) and os.path.isdir(LEGACY_HOME) else new


PIPER_HOME = _home()


def _testing() -> bool:
    """A test process: a test runner is loaded, or a test harness marked it (and its children) with
    PIPER_TEST=1 - tests/piper-unit/_env.py and the e2e harness (tests/piper-e2e/run.py ENV) do."""
    return os.environ.get("PIPER_TEST") == "1" or "unittest" in sys.modules or "pytest" in sys.modules


def _refuse_real_home_in_tests() -> None:
    """A test process never sees the user's real home: the unit tests rmtree REAL_HOME/.claude,
    .ssh, .m2... in their cleanups. Incident 2026-10-12: `unittest test_guard test_sessions` imported
    common (via guard) before tests/piper-unit/_env.py and wiped the real ~/.claude, ~/.ssh, ~/.m2."""
    if not _testing():
        return
    import pwd
    real = pwd.getpwuid(os.getuid()).pw_dir

    def same(a: str, b: str) -> bool:
        try:
            return os.path.samefile(a, b)   # letter case, firmlinks, symlinks
        except OSError:
            return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))

    if same(REAL_HOME, real) or same(PIPER_HOME, os.path.join(real, ".piper")) \
            or same(PIPER_HOME, os.path.join(real, ".hole-orch")):
        raise RuntimeError("Piper imported in a test with the REAL home (%s, PIPER_HOME %s) - import "
                           "tests/piper-unit/_env.py before any Piper module" % (REAL_HOME, PIPER_HOME))


def _require_os_sandbox() -> None:
    """A test process runs in the OS sandbox of scripts/piper-sandbox.py or not at all: the
    guards below cover this process only, not its children."""
    if not _testing():
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("testsandbox", os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "testsandbox.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.require()


_require_os_sandbox()
_refuse_real_home_in_tests()


def _test_write_roots() -> tuple:
    import tempfile
    roots = [tempfile.gettempdir(), "/tmp", "/dev"]
    roots += [p for p in os.environ.get("PIPER_TEST_ROOTS", "").split(os.pathsep) if p]
    return tuple(os.path.realpath(r).rstrip(os.sep) + os.sep for r in roots)


def _check_test_write(path, what: str, follow: bool = True) -> None:
    """PermissionError unless `path` lies under a test root: the name itself (its directory resolved), and
    with `follow` also what it resolves to - unlink / rename / rmdir act on the name, a symlink's target may
    lie anywhere (a test of a link pointing out of a project); open / chmod / truncate reach the target."""
    if isinstance(path, int):
        return   # an fd: opened (and checked) earlier
    p = os.path.abspath(os.fsdecode(os.fspath(path)))
    names = (os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p)),) \
        + ((os.path.realpath(p),) if follow else ())
    if not all((n.rstrip(os.sep) + os.sep).startswith(_TEST_ROOTS) for n in names):
        raise PermissionError(errno.EACCES, "test process: %s outside the test temp dirs ("
                              "PIPER_TEST_ROOTS adds one)" % what, p)


def _guard_test_writes() -> None:
    """Belt and braces under _refuse_real_home_in_tests: in a test process nothing is deleted, renamed,
    created or written outside the temp dirs (+ PIPER_TEST_ROOTS), whatever path a test or Piper computes.
    Covers shutil.rmtree, os.remove/unlink/rmdir/rename/replace/mkdir/symlink/chmod/truncate, os.open and
    open() for writing; dir_fd-relative calls (rmtree's own inner loop) pass, their top was checked."""
    import builtins
    import io
    import shutil
    w_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def guard(mod, name, paths, cond=lambda a, k: True, follow=False):
        orig = getattr(mod, name)
        if getattr(orig, "_piper_test_guard", False):
            return

        def wrapped(*a, **k):
            if k.get("dir_fd") is None and k.get("src_dir_fd") is None and k.get("dst_dir_fd") is None \
                    and cond(a, k):
                for i, kw in paths:
                    v = a[i] if len(a) > i else k.get(kw)
                    if v is not None:
                        _check_test_write(v, "%s.%s" % (mod.__name__, name), follow)
            return orig(*a, **k)
        wrapped._piper_test_guard = True
        wrapped.__wrapped__ = orig
        setattr(mod, name, wrapped)

    guard(shutil, "rmtree", [(0, "path")])
    for n in ("remove", "unlink", "rmdir", "mkdir"):
        guard(os, n, [(0, "path")])
    for n in ("chmod", "truncate"):
        guard(os, n, [(0, "path")], follow=True)
    for n in ("rename", "replace"):
        guard(os, n, [(0, "src"), (1, "dst")])
    guard(os, "symlink", [(1, "dst")])
    guard(os, "open", [(0, "path")], lambda a, k: (a[1] if len(a) > 1 else k.get("flags", 0)) & w_flags, True)

    def writing(a, k):
        mode = a[1] if len(a) > 1 else k.get("mode", "r")
        return any(c in mode for c in "wax+")
    guard(builtins, "open", [(0, "file")], writing, True)
    io.open = builtins.open


_TEST_ROOTS = _test_write_roots() if _testing() else ()
if _testing():
    _guard_test_writes()


def refuse_real_data_in_tests(names) -> None:
    """Incident 2026-10-12 15:51: a unit run with a live service's data dir in its environment rewrote that
    service's real files - the live sessions lost their access. A test process names such dirs (an
    extension lists its own) under the test dirs or not at all."""
    if not _testing():
        return
    for k in names:
        v = os.environ.get(k)
        if v and not (os.path.realpath(v).rstrip(os.sep) + os.sep).startswith(_TEST_ROOTS):
            raise RuntimeError("Piper imported in a test with the REAL %s (%s) - the extension's test environment "
                               "drops it, the e2e harness sets its own" % (k, v))



def is_wsl() -> bool:
    """Linux under WSL (Windows: the browser and IntelliJ live on the Windows side)."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False
SK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPER_DIR = os.path.join(SK, "piper")

TOKENS_FILE = os.path.join(PIPER_HOME, "tokens")
AUDIT_FILE = os.path.join(PIPER_HOME, "audit.log")
POLICY_FILE = os.path.join(PIPER_HOME, "policy.json")
PROJECTS_FILE = os.path.join(PIPER_HOME, "projects.json")
PROJECTS_LOCK = threading.RLock()   # every read-modify-write of projects.json (Settings, obs mapping, add checkout)
APPROVED_FILE = os.path.join(PIPER_HOME, "approved-settings.json")
SESSIONS_DIR = os.path.join(PIPER_HOME, "sessions")
HOLE_HOME = os.path.join(PIPER_HOME, "hole-home")
TEMPLATE_DIR = os.path.join(PIPER_HOME, "claude-template")
ADVISOR_DIR = os.path.join(PIPER_HOME, "advisor")
# 9.141: the boot piperd last saw ptyhost in + the interrupted-sessions notice the GUI has shown
INTERRUPTED_FILE = os.path.join(PIPER_HOME, "interrupted.json")


def boot_id() -> str:
    """This machine's current boot ("" unknown): Linux's boot_id, macOS's kern.boottime - another value than
    the one stored at the last connect means the sessions ptyhost lost went down with a restart."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"sec\s*=\s*(\d+)", out)
    return m.group(1) if m else ""


def _pty_sock() -> str:
    """$PIPER_HOME/ptyhost.sock, unless that exceeds the AF_UNIX path limit (104 bytes on
    macOS): then a private 0700 dir in the per-user temp dir, named after PIPER_HOME."""
    p = os.path.join(PIPER_HOME, "ptyhost.sock")
    if len(p.encode()) < 100:
        return p
    import hashlib
    import tempfile
    h = (os.getuid(), hashlib.sha1(PIPER_HOME.encode()).hexdigest()[:10])
    old = os.path.join(tempfile.gettempdir(), "orch-%d-%s" % h, "ptyhost.sock")   # a ptyhost started before the rename
    return old if os.path.exists(old) else os.path.join(tempfile.gettempdir(), "piper-%d-%s" % h, "ptyhost.sock")


PTY_SOCK = os.environ.get("PIPER_PTY_SOCK") or _pty_sock()
GITLAB_TOKEN_FILE = os.path.join(PIPER_HOME, "gitlab-token")


def _encoded(path: str) -> str:
    """Claude Code's name of a cwd's transcript dir under ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _rebased(v, old: str, new: str):
    if isinstance(v, str):
        return new + v[len(old):] if v == old or v.startswith(old + os.sep) else v
    if isinstance(v, list):
        return [_rebased(x, old, new) for x in v]
    if isinstance(v, dict):
        return {k: _rebased(x, old, new) for k, x in v.items()}
    return v


def migrate_home() -> str:
    """Move a pre-rename ~/.hole-orch to ~/.piper, once, and only when nothing runs
    from it: live sandboxes bind-mount paths under it and ptyhost's socket is in it. Returns ""
    (nothing to do), "busy" (ptyhost answers - it moves at a start after `task piper:stop -- all`
    or a reboot) or "moved"; the caller restarts then, every path above was computed from the
    old home. The old path stays as a symlink for whatever still names it (an old service file's
    log, hole's own state); session records and the transcript dirs of the Global agent and the
    production check (their cwd is under the home) are moved along, so Continue finds them."""
    new = os.path.join(REAL_HOME, ".piper")
    if PIPER_HOME != LEGACY_HOME or os.path.islink(LEGACY_HOME) or not os.path.isdir(LEGACY_HOME) \
            or os.path.lexists(new):
        return ""
    import glob
    import socket
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1)
        probe.connect(PTY_SOCK)
        return "busy"
    except OSError:
        pass
    finally:
        probe.close()
    os.rename(LEGACY_HOME, new)
    os.symlink(new, LEGACY_HOME)
    for meta in glob.glob(os.path.join(new, "sessions", "*", "meta.json")):
        try:
            write_json(meta, _rebased(read_json(meta, {}), LEGACY_HOME, new))
        except (OSError, ValueError):
            pass
    moves = {_encoded(os.path.join(LEGACY_HOME, d)): _encoded(os.path.join(new, d)) for d in ("advisor", "prodcheck")}
    for projects in [os.path.join(REAL_HOME, ".claude", "projects")] + \
            glob.glob(os.path.join(new, "sessions", "*", "claude", "projects")):
        own = projects.startswith(new + os.sep)
        for old_name, new_name in moves.items():
            src, dst = os.path.join(projects, old_name), os.path.join(projects, new_name)
            if os.path.isdir(src) and not os.path.islink(src) and not os.path.lexists(dst):
                try:   # the user's own ~/.claude (claude_dir=host) only gets a link, never a move
                    os.rename(src, dst) if own else os.symlink(old_name, dst)
                except OSError:
                    pass
    audit({"event": "home-migrated", "from": LEGACY_HOME, "to": new})   # AUDIT_FILE: through the symlink
    return "moved"


def ensure_home() -> None:
    os.makedirs(PIPER_HOME, mode=0o700, exist_ok=True)
    os.chmod(PIPER_HOME, 0o700)
    for d in (SESSIONS_DIR, HOLE_HOME, TEMPLATE_DIR, ADVISOR_DIR):
        os.makedirs(d, mode=0o700, exist_ok=True)
    for f in (TOKENS_FILE, AUDIT_FILE):
        if not os.path.exists(f):
            fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)
            if f == TOKENS_FILE:
                os.write(fd, b"# piperd tokens: <token> TAB <scope> TAB <label> (mode 600)\n")
            os.close(fd)
        os.chmod(f, 0o600)


# ------------------------------------------------------------------ tokens
def load_tokens() -> dict:
    """TSV `token scope label` per line, re-read on every request."""
    out = {}
    try:
        with open(TOKENS_FILE) as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0]:
                    out[parts[0]] = {"scope": parts[1],
                                     "label": parts[2] if len(parts) > 2 else ""}
    except FileNotFoundError:
        pass
    return out


def mint_token(label: str = "gui") -> str:
    tok = secrets.token_urlsafe(24)
    with open(TOKENS_FILE, "a") as f:
        f.write("%s\t*\t%s\n" % (tok, label))
    os.chmod(TOKENS_FILE, 0o600)
    return tok


def own_token(label: str) -> str:
    """The token in $PIPER_HOME/<label>-token (0600), minted once with that label: the GUI's
    (`gui`) and `piper attach`'s (`tty`) - read from a file, never from argv (visible in ps)."""
    path = os.path.join(PIPER_HOME, "%s-token" % label)
    tok = None
    if os.path.exists(path):
        with open(path) as f:
            tok = f.read().strip()
        if tok not in load_tokens():
            tok = None
    if not tok:
        tok = mint_token(label)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, tok.encode())
        os.close(fd)
    return tok


def revoke_token(label: str) -> int:
    """Drop every token with this label and its <label>-token file (9.56: the remote device's); the
    next request with it gets 401, load_tokens() is read per request. Returns how many went."""
    try:
        with open(TOKENS_FILE) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    keep = [ln for ln in lines if ln.startswith("#") or ln.rstrip("\n").split("\t")[2:3] != [label]]
    tmp = TOKENS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(keep)
    os.replace(tmp, TOKENS_FILE)
    try:
        os.unlink(os.path.join(PIPER_HOME, "%s-token" % label))
    except FileNotFoundError:
        pass
    return len(lines) - len(keep)


# ------------------------------------------------------------------ scrubbing
# The secret scrubber of everything Piper shows a sandbox or the GUI from logs and tools
# (an extension may keep its own copy in step with it - its tests compare them).
SCRUB = [
    # the value may be quoted ("password": "a b", as in JSON) - the whole quoted value goes
    re.compile(r"(?i)\b((?:[\w.-]{0,64}[_.-])?(?:password|passwd|pwd|secret|token|api[_-]?key"
               r"|access[_-]?key|private[_-]?key)(?:[_.-][\w.-]{0,64})?)([\"']?\s*[=:]\s*)"
               r"(?:\"[^\"\n]{0,4096}\"|'[^'\n]{0,4096}'|\S+)"),
    re.compile(r"(?i)\b(authorization)(\s*:\s*)\S+(\s+\S+)?"),
    re.compile(r"(?i)\b(bearer)(\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(://[^/\s:@]{0,256}:)[^@\s]{1,256}(@)"),  # scheme://user:pass@host (also redis://:pass@)
    # a bare token pasted without a key name: GitLab, GitHub, Slack prefixes
    re.compile(r"\b(glpat-|gldt-|glrt-|glptt-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|xox[abpr]-)[A-Za-z0-9_.-]{8,}"),
    # an env-style name glued to the key word without a separator: PGPASSWORD=, REDISPASSWORD=
    # (upper case only, so prose like "mysecret=" or camelCase stays as the [0] rule decides)
    re.compile(r"\b([A-Z][A-Z0-9_]{0,63}(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|APIKEY))(=)\S+"),
    # a CLI flag with the value after a space: --password foo, --token foo
    re.compile(r"(?i)(?<![\w-])(--(?:password|passwd|pass|token|secret|api[_-]?key|access[_-]?key)\s+)(?!-)\S+"),
    # a private key block: the header stays, the body goes - a run glued to it (one line, JSON-escaped \n)
    # or the base64 / Proc-Type / DEK-Info / blank lines under it; a log quoting the header keeps its text
    re.compile(r"(-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----)(?:[A-Za-z0-9+/=\\]+|(?:[ \t]*\r?\n"
               r"(?:(?:Proc-Type|DEK-Info): [^\n\[]{0,200}|[A-Za-z0-9+/]{16,256}={0,2}|[A-Za-z0-9+/]{1,15}={1,2}|)"
               r"(?=[ \t]*\r?(?:\n|$)))+)"),
    # AWS access key ids and JWTs, pasted bare
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # (?<!...) not \b: a start inside a run ("-eyJ-eyJ...") rescanned the rest of it - quadratic (review 3)
    re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # mysql/mariadb -pSECRET (only on a line that runs one of them: -p means a password there)
    re.compile(r"(\s-p)(?=[^\s-])\S+"),
]
KEY_WORD = re.compile(r"(?i)passw|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key")
MYSQL_LINE = re.compile(r"(?i)\b(?:mysql|mysqldump|mysqladmin|mariadb)\b")


# bidi controls (LRE..RLO, LRI..PDI, LRM/RLM, ALM): in a title from GitLab or a rename they can show
# text in another order than it is stored (an "RLO" spoof) - dropped from untrusted display strings
BIDI = dict.fromkeys(map(ord, "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"))


def no_bidi(text: str) -> str:
    return text.translate(BIDI)


# ...and what a card must not hide either: zero-width characters, word joiners, BOM, Unicode tags
# (U+E0000-E007F, invisible "ASCII smuggling" of text the human never sees but a reader model does).
# Not U+200D (ZWJ) nor the variation selectors: they join emoji (👨‍👩‍👧, ❤️) and reorder nothing (B-N5-1, as text.ts)
INVISIBLE = [0x180E, 0x200B, 0x200C, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF] + \
    list(range(0xE0000, 0xE0080))
SHOW_BIDI = {c: "[U+%04X]" % c for c in list(BIDI) + INVISIBLE}


def show_bidi(obj):
    """What a card shows: a bidi control (or an invisible character) the agent put into a command, SQL or
    comment becomes a visible [U+202E] - the human must see the text in the order it RUNS, not reordered (round 11). Strings in
    dicts / lists too; the action itself uses its own copy."""
    if isinstance(obj, str):
        return obj.translate(SHOW_BIDI)
    if isinstance(obj, dict):
        return {k: show_bidi(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [show_bidi(v) for v in obj]
    return obj


def prod_words_rx() -> str:
    """An extension's PROD_WORDS (more names that mean production) as `|a|b`, the tail of a regex group."""
    import extensions   # lazily: an extension's modules import common
    return "".join("|" + re.escape(str(w).lower()) for w in extensions.items("PROD_WORDS"))


def scrub(line: str) -> str:
    if KEY_WORD.search(line):   # [0] costs ~64 steps per word boundary - skip it when no key word is there
        line = SCRUB[0].sub(r"\1\2[scrubbed]", line)
    line = SCRUB[1].sub(r"\1\2[scrubbed]", line)
    line = SCRUB[2].sub(r"\1\2[scrubbed]", line)
    line = SCRUB[3].sub(r"\1[scrubbed]\2", line)
    line = SCRUB[4].sub(r"\1[scrubbed]", line)
    line = SCRUB[5].sub(r"\1\2[scrubbed]", line)
    line = SCRUB[6].sub(r"\1[scrubbed]", line)
    line = SCRUB[7].sub(r"\1[scrubbed]", line)
    line = SCRUB[8].sub("[scrubbed]", line)
    line = SCRUB[9].sub("[scrubbed]", line)
    if MYSQL_LINE.search(line):
        line = "\n".join(SCRUB[10].sub(r"\1[scrubbed]", ln) if MYSQL_LINE.search(ln) else ln
                          for ln in line.split("\n"))
    return line


# ------------------------------------------------------------------ audit
_AUDIT_LOCK = threading.Lock()


def audit(event: dict) -> None:
    """One JSON line per security-relevant decision (spawn, attestation, GitLab call, ...)."""
    rec = dict(event)
    rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n"
    with _AUDIT_LOCK:
        fd = os.open(AUDIT_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)


def audit_tail(limit: int = 300, session: str | None = None, max_bytes: int = 4 << 20) -> list[dict]:
    """The newest audit records first (the last max_bytes of the log), optionally of one session."""
    try:
        with open(AUDIT_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read()
    except FileNotFoundError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]   # the first line is cut
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or (session and rec.get("session") != session):
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ json files
_BROKEN: dict[str, tuple] = {}   # path -> (mtime_ns, size) of the broken version already reported


def read_json(path: str, default):
    """`default` for a missing file - and for a broken one (a hand edit, a full disk): it is kept as
    <path>.corrupt for the human and reported (once per version), rather than stopping piperd or every request."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        try:
            st = os.stat(path)
            ver = (st.st_mtime_ns, st.st_size)
        except OSError:
            ver = None
        if ver is not None and _BROKEN.get(path) == ver:
            return default
        _BROKEN[path] = ver
        try:
            with open(path, "rb") as src, open(path + ".corrupt", "wb") as dst:
                dst.write(src.read())
        except OSError:
            pass
        print("piperd: %s is not valid JSON (%s) - ignored, a copy kept as %s.corrupt" % (path, e, path), flush=True)
        return default


def write_json(path: str, data, mode: int = 0o600) -> None:
    """Atomic write (tmp + rename) so a reader never sees half a file."""
    tmp = "%s.tmp.%d.%s" % (path, os.getpid(), secrets.token_hex(4))   # unique per writer thread
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ sandbox-writable trees
# A session's config dir is mounted read-write into its sandbox, and Claude runs there with
# --dangerously-skip-permissions: after the first spawn that whole tree is hostile. piperd
# touches it only through these helpers. Every path component is opened with O_NOFOLLOW
# relative to the previous directory fd, so a planted symlink (a file or any parent dir)
# can never redirect a host-side read or write outside the tree.
import stat as _stat  # noqa: E402

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRFLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW


class UnsafePath(OSError):
    pass


def _parts(rel: str) -> list[str]:
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafePath("'..' in %r" % rel)
    return parts


def _open_dir(root: str, parts: list[str]) -> int:
    fd = os.open(root, _DIRFLAGS)
    try:
        for p in parts:
            nfd = os.open(p, _DIRFLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError:
        os.close(fd)
        raise
    return fd


def safe_read(root: str, rel: str, max_bytes: int = 1 << 20, offset: int = 0, *, single_link: bool = False) -> bytes:
    """Read a regular file below `root` without following any symlink."""
    parts = _parts(rel)
    dfd = _open_dir(root, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=dfd)
    finally:
        os.close(dfd)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise UnsafePath("%s is not a regular file" % rel)
        if single_link and st.st_nlink != 1:
            raise UnsafePath("multiple hard links are not allowed")
        if offset:
            os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, max_bytes)
    finally:
        os.close(fd)


def safe_stat(root: str, rel: str) -> os.stat_result:
    """lstat of a regular file below `root` (raises UnsafePath for links, dirs, devices)."""
    parts = _parts(rel)
    dfd = _open_dir(root, parts[:-1])
    try:
        st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)
    if not _stat.S_ISREG(st.st_mode):
        raise UnsafePath("%s is not a regular file" % rel)
    return st


def safe_size(root: str, rel: str) -> int:
    return safe_stat(root, rel).st_size


def safe_listdir(root: str, rel: str = "") -> list[str]:
    fd = _open_dir(root, _parts(rel))
    try:
        return os.listdir(fd)
    finally:
        os.close(fd)


def safe_is_dir(root: str, rel: str) -> bool:
    try:
        os.close(_open_dir(root, _parts(rel)))
        return True
    except OSError:
        return False


def safe_unlink(root: str, rel: str) -> None:
    parts = _parts(rel)
    dfd = _open_dir(root, parts[:-1])
    try:
        os.unlink(parts[-1], dir_fd=dfd)   # unlink never follows the final symlink
    finally:
        os.close(dfd)


class NotPinned(ValueError):
    """A write's `pin` refused the fd it would go through (N10): nothing was written."""


def safe_write(root: str, rel: str, data: bytes, mode: int = 0o644, exact_mode: bool = False,
               in_place: bool = False, create: bool = False, pin=None) -> None:
    """Atomically replace `rel` below `root`: random tmp name, O_EXCL|O_NOFOLLOW, rename in the dir fd.

    A symlink planted at the final name is replaced (rename does not follow it), never written through.
    exact_mode: chmod the new file to `mode` (the umask does not apply) - keeps an edited file's mode.
    in_place: no temporary file and no rename - the existing file (its inode) is truncated and written,
    a new one created under its own name. For a file hidden from Claude: a tmp file next to it would be
    visible to a sandbox that sees the directory, and a rename over the path a sandbox over-mounts can
    drop that over-mount on Linux (d_invalidate detaches mounts of the replaced dentry). Not atomic.
    create: the name must not exist (FileExistsError), it is never replaced.
    pin (in_place, a file hidden from Claude - N10): pin(dir fd, file fd) checks the very fds the write goes
    through, after they are open and before a byte is written (a new file: pin(dir fd, None) before it is
    created; an existing one is never created then); it raises NotPinned to refuse.
    """
    parts = _parts(rel)
    dfd = _open_dir(root, parts[:-1])
    try:
        if in_place:
            restore = None   # the original mode of a read-only file opened by _open_unwritable
            if pin is not None and create:
                pin(dfd, None)
            try:
                fd = os.open(parts[-1], os.O_WRONLY | _NOFOLLOW | (os.O_EXCL if create else 0) |
                             (os.O_CREAT if create or pin is None else 0), mode, dir_fd=dfd)
            except PermissionError:
                if create:
                    raise
                fd, restore = _open_unwritable(dfd, parts[-1], rel)
            try:
                if not _stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.EINVAL, "%s is not a regular file" % rel)
                if pin is not None and not create:
                    pin(dfd, fd)
                os.ftruncate(fd, 0)
                if exact_mode:
                    os.fchmod(fd, mode & 0o7777)
                    restore = None
                _write_all(fd, data)
                os.fsync(fd)
            finally:
                try:
                    if restore is not None:
                        os.fchmod(fd, restore)
                finally:
                    os.close(fd)
            return
        tmp = ".piperd-%s.tmp" % secrets.token_hex(8)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, mode, dir_fd=dfd)
            try:
                if exact_mode:
                    os.fchmod(fd, mode & 0o7777)
                _write_all(fd, data)
            finally:
                os.close(fd)
            if create:   # link, not rename: fails when the name appeared meanwhile
                os.link(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
                os.unlink(tmp, dir_fd=dfd)
            else:
                os.replace(tmp, parts[-1], src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    finally:
        os.close(dfd)


def _open_unwritable(dfd: int, name: str, rel: str) -> tuple[int, int]:
    """A write fd of an existing regular file of ours that lacks owner-write (0444): a hidden file is
    rewritten in place, so the tmp+rename that never needed the file's own write bit is out. The very
    inode throughout, never through a link: opened read-only (O_NOFOLLOW, O_NONBLOCK - a FIFO does not
    hang it) and checked (regular, ours, no owner-write), u+w set on that fd, opened for writing again
    and refused unless it is the same (st_dev, st_ino). Returns (fd, original mode) - the caller puts
    the mode back; on failure it is put back here. PermissionError when the file is not such a case."""
    ro = os.open(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    try:
        st = os.fstat(ro)
        if not _stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & _stat.S_IWUSR:
            raise PermissionError(errno.EACCES, "%s is not writable" % rel)
        orig = _stat.S_IMODE(st.st_mode)
        os.fchmod(ro, orig | _stat.S_IWUSR)
        try:
            fd = os.open(name, os.O_WRONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
            try:
                st2 = os.fstat(fd)
                if (st2.st_dev, st2.st_ino) != (st.st_dev, st.st_ino):
                    raise OSError(errno.EBUSY, "%s was replaced while being written" % rel)
            except BaseException:
                os.close(fd)
                raise
        except BaseException:
            os.fchmod(ro, orig)
            raise
        return fd, orig
    finally:
        os.close(ro)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# ------------------------------------------------------------------ crash-safe write of a hidden file
# A file hidden from Claude is over-mounted in running sandboxes: it is never replaced by a rename (that
# drops the over-mount, verified with Docker Desktop) and gets no temporary file next to it (a sandbox may
# see its directory). It is rewritten in place - and a redo journal under $PIPER_HOME (never mounted)
# makes that crash-safe: the new and the old content first, the commit record last, then the file.
JOURNAL_DIR = os.path.join(PIPER_HOME, "journal")
RECOVERED_DIR = os.path.join(PIPER_HOME, "recovered")


def _durable(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _journal_ident(root: str, rel: str, create: bool) -> list[int]:
    """(st_dev, st_ino) the redo journal pins: the file itself, or for a create its directory. A recovery
    writes only where that identity still holds - not into a stand-in a sandbox put at the same path
    (an empty file, a re-created directory: the move-parent trick)."""
    parts = _parts(rel)
    dfd = _open_dir(root, parts[:-1])
    try:
        st = os.fstat(dfd) if create else os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
    finally:
        os.close(dfd)
    return [st.st_dev, st.st_ino]


def overwrite_in_place(root: str, rel: str, data: bytes, old: bytes, mode: int = 0o644,
                       exact_mode: bool = False, create: bool = False, pin=None) -> None:
    """safe_write(in_place=True) with a redo journal: after a crash journal_recover() finishes the write
    (the file holds the old content or a beginning of the new one) or keeps both versions in
    RECOVERED_DIR (someone changed the file meanwhile). `old`: the content being replaced (b"" for create).
    pin: see safe_write - a refusal drops the journal (nothing was written, nothing to redo)."""
    os.makedirs(JOURNAL_DIR, mode=0o700, exist_ok=True)
    base = os.path.join(JOURNAL_DIR, time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(6))
    _durable(base + ".new", data)
    _durable(base + ".old", old)
    meta = {"root": os.path.realpath(root), "rel": rel, "new": _sha(data), "old": _sha(old), "mode": mode,
            "exact": bool(exact_mode), "at": time.time(), "create": bool(create),
            "id": _journal_ident(root, rel, create)}
    _durable(base + ".json", json.dumps(meta).encode())   # the commit record: from here on the write counts
    try:
        safe_write(root, rel, data, mode=mode, exact_mode=exact_mode, in_place=True, create=create, pin=pin)
    except NotPinned:
        for ext in (".json", ".new", ".old"):
            try:
                os.unlink(base + ext)
            except OSError:
                pass
        raise
    for ext in (".json", ".new", ".old"):
        try:
            os.unlink(base + ext)
        except OSError:
            pass


def journal_recover() -> list[dict]:
    """At piperd's start: finish or set aside the in-place writes a crash interrupted. Audited, names only."""
    out: list[dict] = []
    try:
        names = sorted(os.listdir(JOURNAL_DIR))
    except FileNotFoundError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        base = os.path.join(JOURNAL_DIR, n[:-5])
        meta = read_json(base + ".json", None)
        try:
            with open(base + ".new", "rb") as f:
                new = f.read()
            with open(base + ".old", "rb") as f:
                old = f.read()
        except OSError:
            new = old = None
        outcome = "kept"
        if isinstance(meta, dict) and new is not None and _sha(new) == meta.get("new"):
            root, rel = str(meta.get("root") or ""), str(meta.get("rel") or "")
            try:
                cur = safe_read(root, rel, max(len(new), len(old or b"")) + 1)
            except FileNotFoundError:
                cur = None
            except OSError:
                cur = b"\0unreadable"
            try:   # the same file (or, for a create, the same directory) as when the write began
                same = meta.get("id") == _journal_ident(root, rel, bool(meta.get("create")))
            except OSError:
                same = False
            if cur == new and same:
                outcome = "done"
            elif cur is not None and same and (cur == old or new.startswith(cur)):   # untouched or cut short: ours
                try:
                    safe_write(root, rel, new, mode=int(meta.get("mode") or 0o644),
                               exact_mode=bool(meta.get("exact")), in_place=True)
                    outcome = "completed"
                except OSError:
                    outcome = "kept"
        if outcome == "kept":   # changed by someone else, gone or unreadable: both versions for the human
            os.makedirs(RECOVERED_DIR, mode=0o700, exist_ok=True)
            for ext in (".json", ".new", ".old"):
                try:
                    os.replace(base + ext, os.path.join(RECOVERED_DIR, os.path.basename(base) + ext))
                except OSError:
                    pass
        else:
            for ext in (".json", ".new", ".old"):
                try:
                    os.unlink(base + ext)
                except OSError:
                    pass
        ev = {"event": "gui-write-recovered", "path": (meta or {}).get("rel") if isinstance(meta, dict) else None,
              "outcome": outcome, "journal": os.path.basename(base)}
        audit(ev)
        out.append(ev)
    for n in os.listdir(JOURNAL_DIR):   # no commit record: the write never started - the file is untouched
        if n.endswith((".new", ".old")) and not os.path.exists(os.path.join(JOURNAL_DIR, n[:-4] + ".json")):
            try:
                os.unlink(os.path.join(JOURNAL_DIR, n))
            except OSError:
                pass
    return out
