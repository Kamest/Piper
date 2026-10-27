"""Guard - filesystem, configuration and network isolation layers.

Principle: fail-closed, and verify the result rather than the configuration.

  L1  policy + approved hashes of every .hole/settings.json a start reads (project and
      each library), plus keys a project may never set (agents.*, container.baseImage; the
      git.worktree* keys only with git_mode "guarded").
  L3  host preflight: scan every mount root for sensitive candidates and simulate hole's
      exclusion (same glob semantics as hole v2.1.0 internal/config/glob.go); an
      uncovered candidate blocks the start. Also refuses mounts that would expose
      $PIPER_HOME, Piper's own checkout or what an extension protects (K10, K11).
  L4  attestation after "Attaching to claude agent...": `docker inspect` the agent and
      DinD containers and check that every candidate is over-mounted, that ~/.claude is
      the session's own directory and that no forbidden source is mounted; then a
      `docker exec` spot check (no secret-looking env; no git repository only while
      `.git` is excluded - since 2026-10-01 git is open in the sandbox by default).

Nothing here trusts the sandbox: inputs are host files and the docker daemon.
"""
from __future__ import annotations

import copy
import difflib
import errno
import hashlib
import functools
import glob
import json
import os
import re
import subprocess
import unicodedata

import common
import extensions
import gitops
import security

DEFAULT_POLICY = {
    # Sensitive candidates, matched against paths relative to each mount root.
    # `**` = any number of segments, `*` never crosses `/` (as in hole).
    # An extension adds its own (POLICY_CANDIDATES, default_candidates()).
    "candidates": [
        # no "**/.git": git is open in the sandbox (K1 accepted)
        "**/secrets", "**/secrets.*", "**/.env",
        "**/http-client/http-client.env.json", "**/http-client/.vscode/settings.json",
        "**/*.jks", "**/*.p12", "**/*.pem", "**/*.key",
    ],
    # Accepted false positives (same pattern syntax), e.g. a committed test keystore.
    "allow": [],
    # not walked: huge, and never where a checkout keeps its configuration. `target/`
    # IS walked - a host build copies resources (config included) into it. `.pnpm-store`:
    # package files, and since pnpm 10.27 v10/projects/<hash> links back to every project
    # using the store - a checkout inside the root is scanned on its own path anyway.
    "skip_dirs": ["node_modules", ".idea", ".gradle", ".next", ".pnpm-store"],
    "max_depth": 40,
    # Keys a project/library .hole/settings.json must not set (dotted; `*` = any key).
    # git.worktreeLinks / git.worktreePool are allowed since 2026-10-09 (git_mode "hole" = as a plain
    # hole; the derived mounts are scanned like libraries, worktree_layout); "guarded" forbids them
    "forbidden_project_keys": ["agents", "container.baseImage"],
    # host-gateway ports no sandbox may reach (K17); piperd adds its own port at runtime
    "forbidden_gateway_ports": [],
    # directory includes NOT scanned by L3/L5 (still mounted and allowed as sources)
    "scan_skip_roots": ["~/.m2"],
    # Env names inside the agent container that fail attestation, unless allowed below.
    "secret_env": ["*TOKEN*", "*SECRET*", "*PASSWORD*", "*_PAT"],
    # An extension adds the names it sets itself (extensions.py allowed_env).
    # GH_TOKEN: the user's decision (2026-09-28) - GitHub stays reachable from the sandbox
    # until piperd serves it through MCP like GitLab
    "allowed_env": ["CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN"],
    # Exceptions for ONE mount root: {host path: [patterns]}. (Before git was opened in the
    # sandbox on 2026-10-01 this let a session commit into one library's .git; with **/.git no
    # longer a candidate it is for other candidates only.)
    "library_allow": {},
    # Confirmation kinds (git_commit, git_push, ...) approved without a card; still audited.
    # Settings -> Approvals. Commit and push by default (user's decision 2026-10-04): git is open
    # in the sandbox, so the agent commits there anyway; a push deleting hidden files is refused first.
    "auto_approve": ["git_commit", "git_push"],
    # "hole" (user's decision 2026-10-05): git in a session as in a plain `hole start` - read-write
    # libraries, the host's ~/.gitconfig (a copy), git_run without unlock or card, no git guard;
    # git worktree prune/remove, gc, prune ask first. "guarded": the 2026-10-04 behaviour.
    "git_mode": "hole",
    # opt-in (empty / false = the behaviour before these keys existed):
    # never from a session, whatever is auto-approved: pushes / merges into these, MRs targeting the second
    "protected_branches": [], "mr_target_protected": [],
    # hardening of the host against what a sandbox writes - on by default (2026-10-06), Settings can turn it off
    "git_fail_closed": True,         # host git: no hooks, no local-path remotes, refused in a repo whose config names a program
    "stack_scope_project": True,     # a project session reaches its own dev stack only (the stack provider)
    "worktree_stack": "main",        # a linked worktree uses the main checkout's dev stack + DB ("own": its own)
    "write_guard": True,             # never write Piper's checkout; host-run files of another project always a card
    "settings_auto_tighten": False,  # a .hole/settings.json change that only adds excludes passes the preflight
    "login_spread": False,           # a refreshed login reaches the other running sessions at once
    "codex_in_claude": False,        # Claude sessions get a Codex home (codex as a subagent): Piper's login, the real ~/.codex under codex_dir=host
    "feed_auto_actions": False,      # auto-approved actions appear in the feed
    "git_run_projects": False,       # git_run project=<key>: another checkout Piper knows
    "gitlab_extra_tools": False,     # gitlab_mr_merge, gitlab_job_play
    "host_link_browser": "default",  # GUI: system browser or Chrome for host-opened terminal URLs
    "open_links_on_host": False,     # GUI: terminal links opened by piperd with the host's `open`
    "clipboard_arm_enter": False,    # GUI: Enter / a release outside the terminal arms the OSC 52 clipboard write
    # the second round of opt-in keys (15.20): on by default since the merge (2026-10-09)
    "nav_history": True,             # GUI: back / forward through sessions, views and files (mouse side buttons, ⌘[ ⌘])
    "term_click_opens_files": True,  # GUI: ⌘/Ctrl+click on path:line in the terminal opens Piper's Files view
    "restore_view": True,            # GUI: a reload comes back to the last session and view
    "theme": "dark",                 # GUI: auto (the OS) / dark / light / indigo (N1)
    "git_host_hint": False,          # sessions are told to switch / merge branches through git_run (masked files)
    "commit_requires_review": False, # GUI: Commit only after every checked file's current version was viewed in Review
    "git_run_output_in_repo": True,  # git_run archive --output=<file inside the repository> needs no card (not hidden, not host-run)
    "git_run_readonly_extra": True,  # git_run grep / merge-base / ls-remote / rev-parse / blame ... (read-only) need no card
    # opt-in (2026-10-12): a switch / merge / pull rewriting a hidden file with content on GitLab asks nothing, nor does
    # every later git_run after host git replaced a file over-mount (N10 "replaced") - off: the cards as before
    "git_hidden_server_content": False,
    # GitLab projects (group/repo) every session may READ without a card, outside its own scope
    # (2026-10-05; Settings -> GitLab, or "Allow always" on a gitlab_read card). Never writes.
    "gitlab_read_always": [],
    # W23: the GitHub hosts piperd talks to through the host's `gh auth token --hostname` - github.com,
    # plus a GitHub Enterprise host (its API at https://<host>/api/v3). An origin elsewhere is not GitHub.
    "github_hosts": ["github.com"],
    # holehome.generate rules (11.3.4)
    "holehome": {},
}


def default_candidates() -> list[str]:
    """The default sensitive candidates: the core's (an extension may replace them, DEFAULTS
    policy_candidates) and every extension's POLICY_CANDIDATES."""
    out = list(extensions.default("policy_candidates", DEFAULT_POLICY["candidates"]))
    return out + [c for c in extensions.items("POLICY_CANDIDATES") if c not in out]


def load_policy(path: str, tree: bool | None = None) -> dict:
    """DEFAULT_POLICY (with default_candidates()), the user's policy.json over it, then the security tree
    (security.py; `tree` None = security.ENABLED)."""
    pol = copy.deepcopy(DEFAULT_POLICY)
    pol["candidates"] = default_candidates()
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    for k, v in user.items():
        pol[k] = v
    if tree if tree is not None else security.ENABLED:   # 9.46: every isolation off unless switched on
        security.apply(pol)
    if not git_excluded((pol.get("holehome") or {}).get("force_exclude") or []):
        # git is open (2026-10-01): a candidate list saved before still names .git and would
        # block every start as "uncovered" - drop it (force_exclude [".git"] hides git again)
        pol["candidates"] = [c for c in pol.get("candidates") or [] if not git_excluded([c])]
        if "l5_candidates" in pol:
            pol["l5_candidates"] = [c for c in pol["l5_candidates"] if not git_excluded([c])]
    return pol


def git_excluded(patterns: list[str]) -> bool:
    """Do these exclude patterns (hole semantics) hide a project's top-level `.git`?"""
    return hidden_by([str(p) for p in patterns], ".git")


# ------------------------------------------------------------------ globbing (hole semantics)
@functools.lru_cache(maxsize=None)
def _seg_rx(seg: str) -> re.Pattern:
    return re.compile("^" + _seg_body(seg) + "$")


def _seg_body(seg: str) -> str:
    """Regex for one path segment; never matches '/' (a class is guarded)."""
    out, i = "", 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = seg.find("]", i + 1)
            if j == -1:
                out += re.escape(c)
            else:
                body = seg[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out += r"\[" if body == "[" else "(?:(?!/)[" + body.replace("\\", "\\\\") + "])"
                i = j
        else:
            out += re.escape(c)
        i += 1
    return out


def has_glob(p: str) -> bool:
    return any(c in p for c in "*?[")


@functools.lru_cache(maxsize=None)
def _pattern_rx(pattern: str) -> re.Pattern:
    """`pattern` as one regex over the path with a '/' after EVERY segment ("a/b/"):
    a segment is `body/`, `**` is `(?:[^/]+/)*` (zero or more whole segments)."""
    ps = [s for s in pattern.strip("/").split("/") if s not in ("", ".")]
    return re.compile("".join("(?:[^/]+/)*" if seg == "**" else _seg_body(seg) + "/" for seg in ps))


def _union_rx(patterns: tuple) -> re.Pattern:
    """One regex matching what any of `patterns` matches (same "a/b/" form); none = never."""
    return re.compile("|".join("(?:%s)" % _pattern_rx(p).pattern for p in patterns) or "(?!)")


def match(pattern: str, rel: str) -> bool:
    """Does relative path `rel` match `pattern` (segment-wise, `**` = zero or more segments)?"""
    rs = [s for s in rel.strip("/").split("/") if s]
    return _pattern_rx(pattern).fullmatch("".join(r + "/" for r in rs)) is not None


def expand_exclude(root: str, entry: str) -> list[str]:
    """What hole over-mounts for one files.exclude entry under `root` (relative paths).

    Literal entries must exist (Lstat); globs are expanded segment by segment without
    descending into symlinked directories (hole: os.ReadDir + entry.IsDir).
    """
    entry = os.path.expandvars(entry.rstrip("/"))
    if entry.startswith("./"):
        entry = entry[2:]
    if not entry:
        return []
    if not has_glob(entry):
        return [entry] if os.path.lexists(os.path.join(root, entry)) else []
    segs = [s for s in entry.split("/") if s not in ("", ".")]
    out: list[str] = []

    def walk(rel: str, i: int) -> None:
        if i == len(segs):
            if rel:
                out.append(rel)
            return
        try:
            entries = list(os.scandir(os.path.join(root, rel) if rel else root))
        except OSError:
            return
        if segs[i] == "**":
            walk(rel, i + 1)
            for e in entries:
                child = e.name if not rel else rel + "/" + e.name
                if e.is_dir(follow_symlinks=False):
                    walk(child, i)
                else:
                    walk(child, i + 1)
            return
        rx = _seg_rx(segs[i])
        for e in entries:
            if not rx.match(e.name):
                continue
            child = e.name if not rel else rel + "/" + e.name
            if i + 1 == len(segs):
                out.append(child)
            elif e.is_dir(follow_symlinks=False):
                walk(child, i + 1)

    walk("", 0)
    return sorted(set(out))


@functools.lru_cache(maxsize=4096)
def _under_rx(pattern: str) -> re.Pattern:
    """One regex for "the path, or a directory above it, matches `pattern`" - the same
    answer as match() over every prefix (tested against it), without the recursion."""
    res, star = "", False
    for seg in (s for s in pattern.strip("/").split("/") if s not in ("", ".")):
        if seg == "**":
            star = True
            continue
        body = _seg_body(seg)
        if star:
            res += ("(?:[^/]+/)*" if not res else "/(?:[^/]+/)*") + body
        else:
            res += ("/" if res else "") + body
        star = False
    if not res:
        return re.compile("^[^/].*$" if star else "^(?!)$")
    # case-insensitive: APFS opens Config/Properties/x for config/properties/x (over-hiding is fine)
    return re.compile("^" + res + "(?:/.*)?$", re.IGNORECASE)


def _nfc(s: str) -> str:
    """APFS resolves NFC and NFD spellings to one file - compare both sides in NFC."""
    return unicodedata.normalize("NFC", s)


def hidden_by(candidates: list[str], rel: str) -> bool:
    """Is `rel` - or any directory above it - a policy candidate? A file inside a hidden
    directory (config/properties/x.properties) is hidden too. Case and Unicode normalization
    are ignored: the host filesystem (APFS) does, so `Config/Properties/x` is hidden as well."""
    path = "/".join(p for p in _nfc(rel).split("/") if p not in ("", "."))
    return bool(path) and any(_under_rx(_nfc(c)).match(path) for c in candidates)


def is_git_path(rel: str) -> bool:
    """Does relative path `rel` go through a `.git` (any letter case, as APFS resolves it)?"""
    return any(p.lower() == ".git" for p in rel.replace(os.sep, "/").split("/"))


def covered(rel: str, over: set[str]) -> bool:
    parts = rel.split("/")
    return any("/".join(parts[:k]) in over for k in range(1, len(parts) + 1))


# ------------------------------------------------------------------ L3 scan
def root_allow(root: str, policy: dict) -> list[str]:
    """policy library_allow patterns of this mount root (keys may use ~ and $VARS)."""
    real = os.path.realpath(root)
    out: list[str] = []
    for host, pats in (policy.get("library_allow") or {}).items():
        if os.path.realpath(os.path.expandvars(os.path.expanduser(host))) == real:
            out += list(pats or [])
    return out


def scan_candidates(root: str, policy: dict, hidden: set[str] | None = None,
                    truncated: list[str] | None = None) -> list[str]:
    """Relative paths under `root` that look sensitive (a hit is not descended into).

    `hidden` = paths hole over-mounts anyway; they are not walked. Symlinked directories
    that resolve INSIDE the root are followed (hole's globs never descend them, K7, yet the
    container resolves them, so a secret reachable only through a link must be found). A
    link resolving outside the root points at another mount (scanned as its own root) or
    at nothing the sandbox has; either way it is not walked here. Cycles are cut by realpath.
    """
    hidden = hidden or set()
    pats = policy["candidates"]
    allow = list(policy.get("allow") or []) + root_allow(root, policy)
    # .git: open to the sandbox (2026-10-01, K1 accepted) - its content (submodule git dirs,
    # refs named like a key) is no candidate; a candidate `**/.git` still matches it (hit first)
    skip = set(policy.get("skip_dirs") or []) | {".git"}
    maxd = int(policy.get("max_depth", 40))
    per_dir = int(policy.get("symlink_visits", 4))      # visits of one real dir (via links)
    budget = [int(policy.get("max_entries", 2_000_000))]  # entries looked at, in total
    real_root = os.path.realpath(root)
    visits: dict[str, int] = {}
    found: list[str] = []

    cand_rx, allow_rx = _union_rx(tuple(pats)), _union_rx(tuple(allow))

    def hit(rel: str) -> bool:   # rel is built below: no empty, "." or edge "/" segments
        k = rel + "/"
        return cand_rx.fullmatch(k) is not None and allow_rx.fullmatch(k) is None

    def walk(rel: str, depth: int, chain: frozenset = frozenset()) -> None:
        # Real directories are ALWAYS walked. A cycle is cut only when a directory's
        # realpath is already on the chain of its own ancestors - never because a sibling
        # symlink reached it first (that would hide the real path, whose name the policy
        # patterns need, depending on the order scandir happens to return).
        here = os.path.join(root, rel) if rel else root
        real = os.path.realpath(here)
        if real in chain:
            return
        # a symlink DAG (d1/l0..l2 -> d2, d2/l0..l2 -> d3, ...) is not a cycle but multiplies
        # the walk; past a few visits of one real dir, or past the entry budget, stop and
        # say so - fail-closed like max_depth (the start is blocked as scan-incomplete)
        visits[real] = visits.get(real, 0) + 1
        if visits[real] > per_dir or budget[0] <= 0:
            if truncated is not None:
                truncated.append(rel or ".")
            return
        chain = chain | {real}
        try:
            entries = list(os.scandir(here))
        except OSError:
            return
        budget[0] -= len(entries)
        for e in entries:
            child = e.name if not rel else rel + "/" + e.name
            if hit(child):
                found.append(child)
                continue   # covering the directory covers what is inside
            if child in hidden or e.name in skip:
                continue
            if depth >= maxd:
                if truncated is not None and e.is_dir():
                    truncated.append(child)   # fail-closed: an unscanned subtree blocks
                continue
            if e.is_dir(follow_symlinks=False):
                walk(child, depth + 1, chain)
            elif e.is_symlink():
                target = os.path.realpath(os.path.join(root, child))
                parts = os.path.relpath(target, real_root).split(os.sep)
                if os.path.isdir(target) and _inside(target, real_root) and not skip.intersection(parts):
                    walk(child, depth + 1, chain)

    walk("", 0)
    return sorted(found)


def real_rel(root: str, rel: str) -> str:
    """`rel` with every symlink resolved, relative to the resolved root (what hole mounts on)."""
    r = os.path.relpath(os.path.realpath(os.path.join(root, rel)), os.path.realpath(root))
    return rel if r.startswith("..") else r


def uncovered(root: str, candidates: list[str], over: set[str]) -> list[str]:
    """Candidates whose RESOLVED path is not over-mounted (the one rule L3 and L5 share)."""
    return [c for c in candidates if not covered(real_rel(root, c), over)]


def coverage(root: str, excludes: list[str], policy: dict) -> dict:
    """{root, candidates, overmounts, uncovered} for one mount root and its exclude list.

    A candidate reached through a symlink is covered only when its RESOLVED path is
    over-mounted (docker mounts on the resolved path; the glob that named the link did
    not expand, K7).
    """
    root = root.rstrip("/") or "/"
    over: set[str] = set()
    for e in excludes:
        over.update(expand_exclude(root, e))
    truncated: list[str] = []
    cands = scan_candidates(root, policy, over, truncated) if policy.get("candidates") else []   # 9.46: scan off
    return {"root": root, "candidates": cands, "overmounts": sorted(over), "truncated": truncated[:20],
            "uncovered": uncovered(root, cands, over), "ids": overmount_ids(root, over),
            "pids": overmount_ids(root, over, parent=True)}


def overmount_ids(root: str, over, parent: bool = False) -> dict:
    """{over-mount: [st_dev, st_ino]} as on the host before the sandbox starts - its identity. A running
    sandbox can move the PARENT of a mountpoint (the mount travels with it) and put a plain file or
    directory at the old path (verified with Docker Desktop): only the pinned identity tells them apart.
    parent (F-N11-1): the identity of the directory holding each over-mount - a file at the path in the same
    directory is a host editor's safe write, in another one a stand-in after the parent moved."""
    out = {}
    for o in over:
        try:
            st = os.lstat(os.path.dirname(os.path.join(root, o)) if parent else os.path.join(root, o))
        except OSError:
            continue
        out[o] = [st.st_dev, st.st_ino]
    return out


def intact_overmounts(c: dict) -> set[str] | None:
    """The over-mounts of coverage entry c still at their path: not detached by a GUI operation, and the
    host entry there is the one pinned at the start (overmount_ids). None: no identities recorded (a
    session started before they were) - the caller decides how strict to be."""
    over = set(c.get("overmounts") or []) - set(c.get("detached") or [])
    ids = c.get("ids")
    if not isinstance(ids, dict):
        return None
    out = set()
    for o in over:
        try:
            st = os.lstat(os.path.join(c["root"], o))
        except OSError:
            continue
        if ids.get(o) == [st.st_dev, st.st_ino] and direct_path(c["root"], o):
            out.add(o)
    return out


def direct_path(root: str, rel: str) -> bool:
    """No symbolic link in rel's directories below root (F-N11-1): lstat follows them - a sandbox that moved an
    over-mount's parent away and put a link to it back at the old name kept the pinned identity "at its path"
    while the real files sat under the moved name."""
    real = os.path.realpath(root)
    d = os.path.dirname(rel.strip("/"))
    return os.path.realpath(os.path.join(real, d)) == (os.path.join(real, d) if d else real)


# ------------------------------------------------------------------ settings (hole merge)
def merge(lo: dict, hi: dict) -> dict:
    """hole config/merge.go: objects recurse, arrays concatenate + dedupe, scalars overwrite."""
    out = copy.deepcopy(lo)
    for k, v in hi.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            seen, merged = [], []
            for x in out[k] + v:
                key = json.dumps(x, sort_keys=True)
                if key not in seen:
                    seen.append(key)
                    merged.append(x)
            out[k] = merged
        else:
            out[k] = copy.deepcopy(v)
    return out


def _extends(doc: dict, name: str) -> list[str]:
    v = ((doc.get("profiles") or {}).get(name) or {}).get("extends")
    return [v] if isinstance(v, str) else [x for x in (v or []) if isinstance(x, str)]


def resolve_chain(base: dict, project: dict, profile: str | None) -> list[str]:
    """hole profiles.go:ResolveChain - `extends` across BOTH documents, parents first.
    Raises ValueError for an unknown profile or a cycle (hole refuses to start then too)."""
    if not profile:
        return []
    defined = lambda n: n in (base.get("profiles") or {}) or n in (project.get("profiles") or {})  # noqa: E731
    chain: list[str] = []

    def expand(name: str, path: list[str]) -> None:
        if name in path:
            raise ValueError("profile inheritance cycle: " + " -> ".join(path + [name]))
        if name in chain:
            return
        if not defined(name):
            raise ValueError("unknown profile %r" % name)
        parents = []
        for p in _extends(base, name) + _extends(project, name):
            if p not in parents:
                parents.append(p)
        for p in parents:
            expand(p, path + [name])
        if name not in chain:
            chain.append(name)

    expand(profile, [])
    return chain


def _overlay(doc: dict, name: str) -> dict:
    return {k: v for k, v in ((doc.get("profiles") or {}).get(name) or {}).items() if k != "extends"}


def effective(base: dict, project: dict, profile: str | None) -> dict:
    """profiles.go:MergeWithProfile - global base, global overlays (chain order), project
    base, project overlays."""
    chain = resolve_chain(base, project, profile)
    eff = {k: v for k, v in base.items() if k != "profiles"}
    for n in chain:
        eff = merge(eff, _overlay(base, n))
    eff = merge(eff, {k: v for k, v in project.items() if k != "profiles"})
    for n in chain:
        eff = merge(eff, _overlay(project, n))
    return eff


def read_settings(path: str) -> tuple[dict, str, str]:
    """(parsed, sha256, text) of a settings file; missing file = ({}, "absent", "")."""
    try:
        with open(path, "rb") as f:
            raw = f.read(1 << 20)
    except FileNotFoundError:
        return {}, "absent", ""
    text = raw.decode("utf-8", "replace")
    return json.loads(text or "{}"), hashlib.sha256(raw).hexdigest(), text


def repair_blank(root: str) -> bool:
    """Rewrite a blank root/.hole/settings.json to `{}` (same meaning) and say whether it did.

    piperd reads a blank file as {}, hole refuses it ("unexpected end of JSON input") after the
    whole preflight - a start must not halt on that. A missing file stays missing."""
    try:
        with open(os.path.join(root, ".hole", "settings.json"), "rb") as f:
            if f.read(1 << 20).strip():
                return False
    except OSError:
        return False
    try:
        common.safe_write(root, ".hole/settings.json", b"{}\n")
    except OSError:
        return False   # unwritable: hole reports the blank file as before
    return True


# git_mode "guarded": a sandbox gets no checkout but its own (holehome turns the links off in the base)
GUARDED_KEYS = ("git.worktreeLinks", "git.worktreePool")


def forbidden_keys(doc: dict, policy: dict) -> list[str]:
    bad = []
    keys = list(policy.get("forbidden_project_keys") or [])
    if policy.get("git_mode") == "guarded":
        keys += [k for k in GUARDED_KEYS if k not in keys]
    for dotted in keys:
        for scope, d in [("", doc)] + [("profiles.%s." % n, p or {})
                                       for n, p in (doc.get("profiles") or {}).items()]:
            cur, ok = d, True
            for part in dotted.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    ok = False
                    break
                cur = cur[part]
            if ok and not (dotted == "git.worktreeLinks" and cur == "off"):
                bad.append(scope + dotted)
    return bad


# removed in hole 2.0 (replaced by network.allow): hole refuses to start with them
LEGACY_KEYS = ("network.domainWhitelist", "network.allowedPorts")


def legacy_keys(doc: dict) -> list[str]:
    """Hole 1.x keys in a settings file (top level or a profile) - hole 2 exits on them."""
    return forbidden_keys(doc, {"forbidden_project_keys": LEGACY_KEYS})


def _inside(child: str, parent: str) -> bool:
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def _host(p: str, real_home: str) -> str:
    p = os.path.expandvars(p)
    if p == "~" or p.startswith("~/"):
        p = real_home + p[1:]
    # `..` cleaned as docker does in .Mounts (filepath.Clean), or L4 misses the mount
    return os.path.normpath(p) if p.startswith("/") else (p.rstrip("/") or "/")


def _dest(p: str, container_home: str | None, project_dir: str) -> str:
    """hostenv.go:ResolveContainerPath exactly: $VAR expanded with the HOLE process's
    environment (HOME = the sandbox home), `~` = the sandbox home, empty = the project
    dir, relative = below the project dir. `..` is cleaned (docker's .Mounts shows it so)."""
    p = _dest_raw(p, container_home, project_dir)
    return os.path.normpath(p) if p.startswith("/") else p


def _dest_raw(p: str, container_home: str | None, project_dir: str) -> str:
    env = dict(os.environ)
    if container_home:
        env["HOME"] = container_home
    p = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), p or "")
    p = p.rstrip("/") if p != "/" else p
    if p == "~" and container_home:
        return container_home
    if p.startswith("~/") and container_home:
        return os.path.join(container_home, p[2:])
    if not p:
        return project_dir
    if not p.startswith("/"):
        return os.path.join(project_dir, p)
    return p


def library_roots(eff: dict, real_home: str, container_home: str | None = None,
                  project_dir: str = "") -> list[tuple[str, bool, str]]:
    """(host path, readwrite, container path) of every library."""
    out = []
    for host, spec in (eff.get("libraries") or {}).items():
        rw = isinstance(spec, dict) and bool(spec.get("readwrite"))
        hp = _host(host, real_home)
        path = spec.get("path") if isinstance(spec, dict) else spec
        out.append((hp, rw, _dest(str(path or ""), container_home, project_dir)))
    return out


def include_sources(eff: dict, real_home: str, container_home: str | None = None,
                    project_dir: str = "") -> list[tuple[str, str]]:
    """(host path, container path) of every files.include entry."""
    out = []
    for h, spec in ((eff.get("files") or {}).get("include") or {}).items():
        hp = _host(h, real_home)
        target = spec if isinstance(spec, str) else (spec or {}).get("path", "")
        out.append((hp, _dest(str(target), container_home, project_dir)))
    return out


def _wt_git(cwd: str, *args: str) -> str | None:
    try:
        return gitops._git(cwd, *args, session=True).strip()
    except gitops.GitError:
        return None


def _points_back(checkout: str, admin_root: str) -> bool:
    """Is `checkout` a linked worktree whose `.git` file names a dir under `admin_root`
    (`<common>/worktrees`) that names this checkout back? A sandbox writes the repository's
    admin files (git is open), so a `gitdir` it planted can name any host directory - but not
    the `.git` file in a directory it cannot write."""
    try:
        if not os.path.isfile(os.path.join(checkout, ".git")) or os.path.islink(os.path.join(checkout, ".git")):
            return False
        with open(os.path.join(checkout, ".git")) as f:
            line = f.read(4096).strip()
        if not line.startswith("gitdir:"):
            return False
        admin = os.path.realpath(os.path.join(checkout, line[len("gitdir:"):].strip()))
        if os.path.dirname(admin) != os.path.realpath(admin_root):
            return False
        with open(os.path.join(admin, "gitdir")) as f:
            back = f.read(4096).strip()
        return os.path.realpath(os.path.join(admin, back)) == os.path.realpath(os.path.join(checkout, ".git"))
    except OSError:
        return False


def worktree_layout(eff: dict, project_dir: str, known_common: str | None) -> dict:
    """What hole 2.1.0 mounts for git.worktreeLinks / git.worktreePool (internal/worktree/
    worktree.go:Derive), re-derived on the host the same way, plus what Piper refuses of it:
    {links: [(host path, readwrite)], pool, pool_worktrees, blocks}.

    A link is mounted at its own path as a library (its own files.exclude, like any library);
    the pool `<project>-worktrees` read-write, with the files.exclude of every checkout git
    lists inside it (addPoolWorktreeExclusions). The repository's admin files are sandbox-
    writable, so a link is accepted only when it is verifiably a checkout of THIS repository:
    the main repository of a linked worktree must be the recorded one (`known_common`, the
    record repo_top keeps - never the project's own `.git` file), and every other linked
    worktree must point back at the repository's admin dir (_points_back)."""
    out: dict = {"links": [], "pool": None, "pool_worktrees": [], "blocks": []}
    git = eff.get("git") or {}
    mode = git.get("worktreeLinks")
    if mode not in ("ro", "rw"):   # hole: unset = off
        return out
    rw = mode == "rw"
    project = os.path.realpath(project_dir)
    common_dir = _wt_git(project, "rev-parse", "--git-common-dir")
    if not common_dir:
        return out
    common_dir = os.path.realpath(os.path.join(project, common_dir))
    bare = _wt_git(project, "config", "--get", "core.bare") == "true"
    top = _wt_git(project, "rev-parse", "--show-toplevel")
    if (top is None and not bare) or (top is not None and os.path.realpath(top) != project):
        return out
    main = common_dir if bare else os.path.dirname(common_dir)
    if main != project:   # a linked worktree: hole mounts its main repository
        if _inside(main, project):
            return out
        if not known_common or os.path.realpath(known_common) != common_dir:
            out["blocks"].append({"kind": "worktree-link-unverified", "path": main,
                                  "detail": "the main repository is not the recorded one of %s" % project})
            return out
        out["links"].append((main, rw))
        return out
    if git.get("worktreePool") and not bare:
        out["pool"] = project + "-worktrees"
    listing = _wt_git(project, "worktree", "list", "--porcelain")
    for line in (listing or "").splitlines():
        if not line.startswith("worktree "):
            continue
        path = os.path.realpath(line[len("worktree "):].strip())
        if _inside(path, project):
            continue
        if out["pool"] and _inside(path, out["pool"]):
            out["pool_worktrees"].append(path)
            continue
        if not os.path.isdir(path):
            continue   # hole skips a missing library too
        if not _points_back(path, os.path.join(common_dir, "worktrees")):
            out["blocks"].append({"kind": "worktree-link-unverified", "path": path,
                                  "detail": "git lists it as a worktree of %s, but its .git does not point back" % project})
            continue
        out["links"].append((path, rw))
    return out


WORKTREES_IN = ".claude/worktrees"   # HOLE_WORKTREES_DIR default (15.44) and Claude Code's agent worktrees


def worktree_excludes(excludes: list) -> list[str]:
    """`excludes` repeated in every in-checkout worktree (WORKTREES_IN/<name>: one more full checkout of
    the same repository inside a mount). hole resolves an exclude against the mount root only, so
    `config/properties` does not hide `.claude/worktrees/x/config/properties`; a `**/` pattern, an
    absolute or variable path, and one already naming the worktrees reach them as they are."""
    out: list[str] = []
    for e in excludes:
        e = str(e).strip()
        rel = e[2:] if e.startswith("./") else e
        if not rel or rel.startswith(("/", "~", "$", "**")) or rel.rstrip("/") in (".claude", WORKTREES_IN) \
                or rel.startswith(WORKTREES_IN + "/"):
            continue
        m = "%s/*/%s" % (WORKTREES_IN, rel)
        if m not in out and m not in excludes:
            out.append(m)
    return out


def worktree_mirror(eff: dict, project_dir: str, real_home: str, container_home: str | None = None) -> list[str]:
    """Global excludes for one session (its generated settings file): the effective excludes and each
    library's own ones repeated in the in-checkout worktrees of the project and of every library, kept
    only where they match now (hole hides at the start and warns about each pattern matching nothing).
    Global excludes reach every mount, so one checkout's entry can hide the same path in another
    checkout's worktree - more hidden, never less."""
    roots = [(project_dir, list((eff.get("files") or {}).get("exclude") or []))]
    for lib, _rw, _dest_path in library_roots(eff, real_home, container_home, project_dir):
        try:
            own = read_settings(os.path.join(lib, ".hole", "settings.json"))[0]
        except (ValueError, OSError):
            own = {}
        roots.append((lib, list((own.get("files") or {}).get("exclude") or [])))
    glob = list((eff.get("files") or {}).get("exclude") or [])
    out: list[str] = []
    for root, own_excl in roots:
        if not os.path.isdir(os.path.join(root, WORKTREES_IN)):
            continue
        for m in worktree_excludes(glob + own_excl):
            if m not in out and expand_exclude(root, m):
                out.append(m)
    return out


def shield_worktree_pools(eff: dict, project_dir: str, real_home: str,
                          container_home: str | None = None, *, prepare: bool = False) -> list[str]:
    """Pin the whole nested-worktree directory before Hole starts: future checkouts must not
    appear through a parent mount with only the old wildcard matches hidden. The selected
    worktree is still mounted independently as the session's project. Agent-created nested
    worktrees inside the masked placeholder are not the persistent host pool; use Piper on the host.
    Planning is read-only; start prepares even an empty pool using no-follow directory fds."""
    roots = [project_dir] + [lib for lib, _rw, _dest in
                            library_roots(eff, real_home, container_home, project_dir)]
    if prepare:
        for root in dict.fromkeys(roots):
            try:
                fd = common._open_dir(root, [])
                try:
                    for part in WORKTREES_IN.split("/"):
                        try:
                            os.mkdir(part, dir_fd=fd)
                        except FileExistsError:
                            pass
                        nxt = os.open(part, common._DIRFLAGS, dir_fd=fd)
                        os.close(fd)
                        fd = nxt
                finally:
                    os.close(fd)
            except OSError as e:
                if root == project_dir or e.errno not in (errno.EACCES, errno.EROFS, errno.EPERM):
                    raise
                # Read-only SDK/library sources need not accept a new host pool. Any future
                # GUI worktree creation still requires an actual intact shield in every view.
    excludes = [WORKTREES_IN]
    # A checkout also appears through an enclosing library. Cover that relative alias
    # explicitly, without a recursive ** scan of every file in a broad library.
    for parent in roots:
        for child in roots:
            if os.path.realpath(child) == os.path.realpath(parent) or not _inside(child, parent):
                continue
            alias = os.path.join(glob.escape(os.path.relpath(child, parent)), WORKTREES_IN)
            if not covered(alias, set(excludes)):
                excludes.append(alias)
    return excludes


def hook_problems(eff: dict, real_home: str, env: dict | None = None) -> list[str]:
    """Why a prestart hook would kill the agent: the entrypoint runs them under `set -e` and
    executes each copy directly, so a missing script or a CRLF shebang ("bad interpreter",
    a checkout made by Windows git) stops the container before Claude starts. Diagnostics
    only - shown when the agent dies on start, never a block."""
    env = dict(os.environ, **(env or {}))
    out = []
    for item in ((eff.get("hooks") or {}).get("prestart") or []):
        script = str((item or {}).get("script", "")) if isinstance(item, dict) else ""
        if not script:
            continue
        p = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env.get(m.group(1) or m.group(2), m.group(0)), script)
        p = real_home + p[1:] if p == "~" or p.startswith("~/") else p
        try:
            with open(p, "rb") as f:
                head = f.read(4096)
        except OSError as e:
            out.append("prestart hook %s: cannot be read (%s)" % (p, e.strerror or e))
            continue
        if b"\r\n" in head:
            out.append("prestart hook %s: CRLF line endings (a checkout from Windows git?) - fails with "
                       "\"bad interpreter\"; fix with `git config core.autocrlf input` and check out again" % p)
    return out


def gateway_ports(eff: dict) -> set[str]:
    ports = set()
    for entry in ((eff.get("network") or {}).get("hostGatewayDomains") or []):
        ports.update(p for p in str(entry).partition(":")[2].split(",") if p)
    return ports


def _diff(old: str, new: str, path: str, label: str = "approved") -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        label, "now", n=3))[:20000] or "(no content change)"


def only_tightens(old_text, new_text: str, policy: dict) -> bool:
    """True when a changed settings file cannot weaken isolation against the approved one: it differs
    only by MORE files.exclude entries, or - while every session runs unfiltered anyway
    (unrestricted_network) - by its egress domain lists. Anything else, unparsable text included, is
    reviewed: hole merges additively (K3), so any other new key can open something."""
    try:
        old, new = json.loads(old_text or ""), json.loads(new_text or "")
    except (TypeError, ValueError):
        return False
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    old, new = copy.deepcopy(old), copy.deepcopy(new)
    oex = (old.get("files") or {}).pop("exclude", []) if isinstance(old.get("files"), dict) else []
    nex = (new.get("files") or {}).pop("exclude", []) if isinstance(new.get("files"), dict) else []
    if not isinstance(oex, list) or not isinstance(nex, list) or not set(map(str, oex)) <= set(map(str, nex)):
        return False
    for d in (old, new):
        if d.get("files") == {}:
            d.pop("files")
        net = d.get("network")
        if policy.get("unrestricted_network") is True and isinstance(net, dict):
            for k in ("allow", "domainWhitelist"):   # egress lists only - hostGatewayDomains opens host ports
                net.pop(k, None)
            if not net:
                d.pop("network")
    return old == new


def preflight(project_dir: str, base: dict, profile: str | None, policy: dict, approved: dict,
              forbidden: list[tuple[str, str]], real_home: str,
              extra_libraries: list[tuple[str, bool]] = (), allowed_sources: list[str] = (),
              container_home: str | None = None, progress=None, known_common: str | None = None,
              global_library_excludes: bool = True) -> dict:
    """L1 + L3 for one start. Returns {ok, blocks, settings: {path: sha}, texts, coverage,
    effective, mount_roots, libraries, inherited: {path: approved twin in this repository}}. Every
    coverage entry carries `kind` (project|library|include) and `dest` (where the root appears inside the container). `known_common`: the recorded common
    git dir of a linked worktree (worktree_layout)."""
    blocks: list[dict] = []
    settings_hashes: dict[str, str] = {}
    texts: dict[str, str] = {}
    proj_file = os.path.join(project_dir, ".hole", "settings.json")
    repaired = [proj_file] if repair_blank(project_dir) else []
    try:
        proj, sha, text = read_settings(proj_file)
        eff = effective(base, proj, profile)
        global_eff = {k: v for k, v in base.items() if k != "profiles"}
        for name in resolve_chain(base, proj, profile):
            global_eff = merge(global_eff, _overlay(base, name))
        global_excludes = list((global_eff.get("files") or {}).get("exclude") or []) if global_library_excludes else []
    except (ValueError, OSError) as e:
        return {"ok": False, "blocks": [{"kind": "settings-invalid", "path": proj_file, "detail": str(e)}],
                "settings": {}, "texts": {}, "coverage": [], "effective": {}, "mount_roots": []}
    settings_hashes[proj_file], texts[proj_file] = sha, text
    for key in forbidden_keys(proj, policy):
        blocks.append({"kind": "forbidden-key", "path": proj_file, "detail": key})
    for key in legacy_keys(proj):
        blocks.append({"kind": "settings-legacy", "path": proj_file, "detail": key})
    pd = project_dir.rstrip("/")
    # (host root, readwrite, excludes, kind, container path)
    roots = [(pd, True, list((eff.get("files") or {}).get("exclude") or []), "project", pd)]
    libs = list(library_roots(eff, real_home, container_home, pd)) + [(l, rw, l) for l, rw in extra_libraries]
    # git.worktreeLinks / worktreePool: hole mounts these as libraries at their own path, an
    # explicit library of the same directory wins (mounts.go:mergeLibraries)
    layout = worktree_layout(eff, pd, known_common)
    blocks += layout["blocks"]
    have = {os.path.realpath(l[0]) for l in libs}
    derived = [(p, rw) for p, rw in layout["links"]] + ([(layout["pool"], True)] if layout["pool"] else [])
    libs = [(p, rw, p) for p, rw in derived if os.path.realpath(p) not in have] + libs
    for lib, rw, dest in libs:
        lib_file = os.path.join(lib, ".hole", "settings.json")
        if repair_blank(lib):
            repaired.append(lib_file)
        try:
            own, lsha, ltext = read_settings(lib_file)
        except (ValueError, OSError) as e:
            blocks.append({"kind": "settings-invalid", "path": lib_file, "detail": str(e)})
            continue
        settings_hashes[lib_file], texts[lib_file] = lsha, ltext
        for key in forbidden_keys(own, policy):
            blocks.append({"kind": "forbidden-key", "path": lib_file, "detail": key})
        for key in legacy_keys(own):
            blocks.append({"kind": "settings-legacy", "path": lib_file, "detail": key})
        excl = global_excludes + list((own.get("files") or {}).get("exclude") or [])
        if lib == layout["pool"]:   # one mount, and every checkout git lists in it hides its own files
            for wt in layout["pool_worktrees"]:
                try:
                    wown = read_settings(os.path.join(wt, ".hole", "settings.json"))[0]
                except (ValueError, OSError) as e:
                    blocks.append({"kind": "settings-invalid", "path": os.path.join(wt, ".hole", "settings.json"),
                                   "detail": str(e)})
                    continue
                rel = os.path.relpath(wt, os.path.realpath(lib))
                excl += [rel + "/" + str(e).lstrip("/").removeprefix("./")
                         for e in global_excludes + list((wown.get("files") or {}).get("exclude") or [])]
        roots.append((lib, rw, excl, "library", dest))
    # directory includes are mounts without any exclusion: scanned like a library, except the
    # maven cache & co. (policy scan_skip_roots) - artifacts only, and a scan of them costs
    # half a minute on a developer machine
    includes = include_sources(eff, real_home, container_home, pd)
    skip_roots = [_host(x, real_home) for x in policy.get("scan_skip_roots") or []]
    for inc, dest in includes:
        if os.path.isdir(inc) and not any(_inside(inc, sk) for sk in skip_roots):
            roots.append((inc, True, [], "include", dest))
    # checkouts of THIS repository, each verified: the project, the worktrees git links to it (worktree_layout:
    # pointing back / the recorded main) and the recorded main checkout of a linked worktree. Identical
    # settings bytes approved in one of them are approved in the others (one review per repository, not
    # per worktree); any other difference, or a checkout of another repository, is still reviewed.
    same_repo = {os.path.realpath(pd)} | {os.path.realpath(p) for p, _ in layout["links"]}
    if known_common and os.path.basename(known_common.rstrip("/")) == ".git":
        same_repo.add(os.path.realpath(os.path.dirname(known_common.rstrip("/"))))
    by_dir = {os.path.realpath(os.path.dirname(os.path.dirname(k))): v for k, v in approved.items()
              if k.endswith("/.hole/settings.json") and isinstance(v, dict)}
    inherited: dict[str, str] = {}
    for path, sha in settings_hashes.items():
        prev = approved.get(path) or {}
        if sha == "absent" and not prev:
            continue   # nothing there and nothing approved before: nothing to review
        was = prev.get("text")
        if (path in repaired and isinstance(was, str) and not was.strip()
                and prev.get("sha") == hashlib.sha256(was.encode()).hexdigest()):
            continue   # approved blank, repaired to {}: the same settings (see repair_blank)
        if prev.get("sha") != sha and sha != "absent":
            here = os.path.realpath(os.path.dirname(os.path.dirname(path)))
            twin = next((d for d in sorted(same_repo) if d != here and here in same_repo
                         and (by_dir.get(d) or {}).get("sha") == sha), None)
            if twin:
                inherited[path] = os.path.join(twin, ".hole", "settings.json")
                continue
        if prev.get("sha") != sha and policy.get("settings_review", True) is not False and not (policy.get("settings_auto_tighten")
                                           and only_tightens(prev.get("text"), texts[path], policy)):
            blocks.append({"kind": "settings-changed", "path": path, "detail": sha,
                           "previous": prev.get("sha"), "diff": _diff(prev.get("text", ""), texts[path], path,
                                                                      "approved" if prev else "(never approved)")})
    cov = []
    for i, (root, rw, excludes, kind, dest) in enumerate(roots, 1):
        if not os.path.isdir(root):
            continue   # hole skips a missing library too (warn): nothing is mounted
        if progress:
            progress("scanning %s %s (%d/%d)" % (kind, root, i, len(roots)))
        c = coverage(root, excludes, policy)
        c.update(readwrite=rw, kind=kind, dest=dest)
        cov.append(c)
        for u in c["uncovered"]:
            blocks.append({"kind": "uncovered", "path": os.path.join(root, u), "detail": root})
        for u in c.get("truncated") or []:
            blocks.append({"kind": "scan-incomplete", "path": os.path.join(root, u),
                           "detail": "deeper than max_depth=%s - raise it in policy.json or skip the dir"
                           % policy.get("max_depth", 40)})
    allowed = {os.path.realpath(a) for a in allowed_sources}
    seen = set()   # a scanned include is in roots too: one block per mount, not two
    for src, rw, via in [(r[0], r[1], r[3]) for r in roots] + [(i, True, "include") for i, _ in includes]:
        if os.path.realpath(src) in allowed or (os.path.realpath(src), via) in seen:
            continue
        seen.add((os.path.realpath(src), via))
        for path, mode in forbidden:
            if path and (_inside(path, src) or _inside(src, path)) and (mode == "any" or rw):
                # source/via/rw: the GUI says which mount (project, library, include) to fix
                blocks.append({"kind": "protected-mounted", "path": path,
                               "detail": "%s (%s)" % (src, "rw" if rw else "ro"),
                               "source": src, "via": via, "rw": rw, "mode": mode})
    bad_ports = gateway_ports(eff) & {str(p) for p in policy.get("forbidden_gateway_ports") or []}
    for port in sorted(bad_ports):
        blocks.append({"kind": "forbidden-port", "path": "network.hostGatewayDomains",
                       "detail": "port %s would let the sandbox reach Piper (K17)" % port})
    if policy.get("security_strict"):
        # Check the fully merged project/profile settings too, not just sanitized personal defaults.
        net = eff.get("network") or {}
        if any(str(d).strip() in ("*", "0.0.0.0/0", "::/0") for d in net.get("allow") or []):
            blocks.append({"kind": "strict-network", "path": "network.allow", "detail": "Strict requires an explicit network allow-list"})
        env = eff.get("environment") or {}
        for name in env_failures([str(k) + "=" for k in env] if isinstance(env, dict) else [], policy):
            blocks.append({"kind": "strict-secret", "path": "environment", "detail": "Strict refuses service token " + name})
        for lib, rw, _dest in libs:
            if rw and (not known_common or os.path.realpath(lib) != os.path.realpath(known_common)):
                blocks.append({"kind": "strict-library", "path": lib, "detail": "Strict requires read-only libraries"})
    mount_roots = [r[0] for r in roots] + [i for i, _ in includes]
    return {"ok": not blocks, "blocks": blocks, "settings": settings_hashes, "texts": texts, "repaired": repaired,
            "inherited": inherited,
            "coverage": cov, "effective": eff, "mount_roots": mount_roots,
            "libraries": [(l, rw) for l, rw, _ in libs],
            "hook_problems": hook_problems(eff, real_home)}


# ------------------------------------------------------------------ L4 attestation
class _Failed:
    def __init__(self, why: str):
        self.returncode, self.stdout, self.stderr = 125, "", why


def _docker(docker_bin: str, *args: str, timeout: int = 30, stdin: str | None = None):
    try:
        return subprocess.run([docker_bin, *args], capture_output=True, text=True, timeout=timeout,
                              input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return _Failed("docker %s timed out after %ss" % (args[0], timeout))
    except OSError as e:
        return _Failed("docker not runnable (%s): %s" % (docker_bin, e))


def _norm(src: str) -> str:
    return src[len("/host_mnt"):] if src.startswith("/host_mnt/") else src


def inspect_mounts(info: dict) -> list[dict]:
    """Docker keeps compose's tmpfs mounts in HostConfig.Tmpfs, outside .Mounts."""
    return list(info.get("Mounts") or []) + [
        {"Type": "tmpfs", "Destination": path}
        for path in (info.get("HostConfig") or {}).get("Tmpfs") or {}]


def attest_mounts(mounts: list[dict], cov: list[dict], expect_claude: tuple[str, str] | None,
                  forbidden: list[tuple[str, str]], allowed_sources: list[str], run_tmp_root: str,
                  allowed_roots: list[str] | None = None, instance: str | None = None) -> list[str]:
    """Check one container's `.Mounts` (docker's view) against the L3 result. Returns failures.

    Docker's view is NOT proof of what the container sees (Docker Desktop 4.90 drops nested
    directory binds right after start, K18) - `inside_check` is. This keeps the checks that
    only the host view can make: every candidate has an over-mount entry, ~/.claude is the
    session's own dir, every bind source is expected (allowed_roots) and none is forbidden.
    """
    fails: list[str] = []
    binds = [m for m in mounts if m.get("Type", "bind") == "bind"]
    by_dest = {m.get("Destination", "").rstrip("/"): m for m in mounts}
    run_tmp = run_tmp_root.rstrip("/") + "/"
    for c in cov:
        root = c["root"].rstrip("/")
        dest = (c.get("dest") or root).rstrip("/")   # where the root appears in the container
        if dest not in by_dest or by_dest[dest].get("Type", "bind") != "bind":
            if expect_claude:   # the agent container: every scanned root must be where L3 expects it
                fails.append("%s root %s is not mounted at %s - its checks cannot be trusted"
                             % (c.get("kind", "mount"), root, dest))
            continue   # the DinD sidecar mounts only a subset
        # every candidate AND every exclusion hole should have over-mounted (6.5)
        wanted = [real_rel(root, x) for x in c["candidates"]] + list(c.get("overmounts") or [])
        for rel in dict.fromkeys(wanted):
            full = os.path.join(dest, rel)
            parts = full.split("/")
            ancestors = ["/".join(parts[:k]) for k in range(len(parts), 1, -1)]
            hidden = False
            for a in ancestors:
                if a == dest:
                    break
                m = by_dest.get(a)
                if m is None:
                    continue
                src = _norm(m.get("Source", ""))
                kind = m.get("Type", "bind")
                if (kind == "bind" and (src == "/dev/null" or src.startswith(run_tmp))) or \
                        kind == "tmpfs" or (kind == "volume" and instance and
                                            m.get("Name") == "hole-sandbox-masks-" + instance):
                    hidden = True
                    break
            if not hidden:
                fails.append("not over-mounted: %s" % full)
    if expect_claude:
        dest, src = expect_claude
        m = by_dest.get(dest.rstrip("/"))
        if m is None or m.get("Type", "bind") != "bind":
            fails.append("the agent's config dir is not mounted at %s" % dest)
        elif os.path.realpath(_norm(m.get("Source", ""))) != os.path.realpath(src):
            fails.append("%s comes from %s, expected the session's %s" % (dest, m.get("Source"), src))
    allowed = {os.path.realpath(a) for a in allowed_sources}
    for m in binds:
        src = _norm(m.get("Source", ""))
        if not src or src == "/dev/null" or os.path.realpath(src) in allowed:
            continue
        if src.startswith(run_tmp) or os.path.realpath(src).startswith(os.path.realpath(run_tmp_root) + "/"):
            continue   # hole's own run dir: exclusion placeholders, prestart scripts, gateway conf
        if allowed_roots is not None and not any(_inside(src, r) for r in allowed_roots):
            fails.append("unexpected mount source %s (not the project, a library, an include or the session)" % src)
            continue
        for path, mode in forbidden:
            if not path or not (_inside(src, path) or _inside(path, src)):
                continue
            if mode == "any" or m.get("RW", True):
                fails.append("forbidden mount source %s (covers %s)" % (src, path))
    return fails


def env_failures(env: list[str], policy: dict) -> list[str]:
    bad = []
    allowed = set(policy.get("allowed_env") or [])
    for kv in env:
        name = kv.split("=", 1)[0]
        if name in allowed:
            continue
        if any(_seg_rx(p).match(name) for p in policy.get("secret_env") or []):
            bad.append(name)
    return bad


# Runs INSIDE the container: one line per path from stdin -
# "<mounted 0|1> <visible 0|N> <path>". Pure sh for the visibility test (a missing `ls`
# must not read as "empty"); mountinfo (field 5, a space escaped as \040) only annotates.
INSIDE_SCRIPT = r"""
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH   # never the image PATH (~/.local/bin first, agent-writable)
while IFS= read -r p; do
  [ -n "$p" ] || continue
  e=$(printf '%s' "$p" | sed 's/ /\\040/g' 2>/dev/null)
  if P="$e" awk '$5==ENVIRON["P"]{f=1} END{exit !f}' /proc/self/mountinfo 2>/dev/null; then m=1; else m=0; fi
  n=0
  if [ -d "$p" ]; then
    for f in "$p"/* "$p"/.[!.]* "$p"/..?*; do
      # Git can leave an empty config.worktree in the exclusion placeholder. It exposes no bytes.
      # Require the actual over-mount, the exact git path and a regular zero-byte file, never a link.
      case "$p" in */.git)
        if [ "$m" = 1 ] && [ "$f" = "$p/config.worktree" ] && [ -f "$f" ] && [ ! -s "$f" ] && [ ! -L "$f" ]; then continue; fi
      esac
      if [ -e "$f" ] || [ -L "$f" ]; then n=$((n + 1)); fi
    done
  elif [ -c "$p" ] || { [ ! -e "$p" ] && [ ! -L "$p" ]; }; then n=0
  elif [ -s "$p" ] || [ -L "$p" ]; then n=1
  fi
  printf '%s %s %s\n' "$m" "$n" "$p"
done
"""


def inside_failures(output: str, expected: list[str]) -> list[str]:
    """Parse INSIDE_SCRIPT output: a candidate that is readable (entries or bytes) fails."""
    seen, fails = set(), []
    for line in output.splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        mounted, n, path = parts
        seen.add(path)
        if n != "0":
            fails.append("visible inside the container: %s (%s %s, over-mount %s)" % (
                path, n, "entries/bytes", "present" if mounted == "1" else "MISSING"))
    for p in expected:
        if p not in seen:
            fails.append("inside check did not report %s" % p)
    return fails


def visible_paths(output: str) -> list[str]:
    """Paths INSIDE_SCRIPT reported readable."""
    return [p[2] for p in (line.split(" ", 2) for line in output.splitlines()) if len(p) == 3 and p[1] != "0"]


AGENT_NOT_RUNNING = "container is not running"


def attest(docker_bin: str, instance: str, cov: list[dict], claude_mount: tuple[str, str],
           forbidden: list[tuple[str, str]], allowed_sources: list[str], run_tmp_root: str,
           project_dir: str, policy: dict, allowed_roots: list[str] | None = None,
           git_hidden: bool = True) -> dict:
    """L4 against the live containers of one hole instance: docker's view AND the container's."""
    fails: list[str] = []
    checks = 0
    paths = sorted({os.path.join((c.get("dest") or c["root"]).rstrip("/"), x)
                    for c in cov
                    for x in [real_rel(c["root"], y) for y in c["candidates"]] + list(c.get("overmounts") or [])})
    for suffix, want_claude in (("-agent-1", True), ("-docker-1", False)):
        name = instance + suffix
        r = _docker(docker_bin, "inspect", name)
        if r.returncode != 0:
            if suffix == "-docker-1" and "no such" in (r.stderr or "").lower():
                continue   # no DinD sidecar for this sandbox
            fails.append("docker inspect %s failed: %s" % (name, (r.stderr or "").strip()[:200]))
            continue
        try:
            info = json.loads(r.stdout)[0]
            mounts = inspect_mounts(info)
            env = (info.get("Config") or {}).get("Env") or []
        except (ValueError, IndexError) as e:
            fails.append("docker inspect %s: unreadable (%s)" % (name, e))
            continue
        tag = suffix[1:-2]
        state = info.get("State") or {}
        if want_claude and state.get("Running") is False:
            fails.append("%s: %s (exit code %s)" % (tag, AGENT_NOT_RUNNING, state.get("ExitCode")))
        checks += 1
        fails += ["%s: %s" % (tag, f) for f in
                  attest_mounts(mounts, cov, claude_mount if want_claude else None,
                                forbidden, allowed_sources, run_tmp_root, allowed_roots, instance=instance)]
        fails += ["%s: secret-looking env %s" % (tag, n) for n in env_failures(env, policy)]
        if paths:
            ri = _docker(docker_bin, "exec", "-i", name, "/bin/sh", "-c", INSIDE_SCRIPT,
                         stdin="\n".join(paths) + "\n", timeout=60)
            checks += 1
            if ri.returncode != 0:
                fails.append("%s: inside check failed: %s" % (tag, (ri.stderr or "").strip()[:200]))
            else:
                fails += ["%s: %s" % (tag, f) for f in inside_failures(ri.stdout, paths)]
    if not git_hidden:
        # `.git` is not excluded: git working in the sandbox is the intent (user's decision
        # 2026-10-01), so a repository there is no failure
        return {"ok": not fails, "failures": fails, "checks": checks}
    # says WHERE git found the repository: the mount checks above passed, so it is not the
    # project's own .git (a GIT_* variable, or a repository in a directory above the project)
    r = _docker(docker_bin, "exec", instance + "-agent-1", "/bin/sh", "-c",
                'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; '   # not the agent-writable image PATH
                'if cd "$1" 2>/dev/null; then d=$(git rev-parse --absolute-git-dir 2>/dev/null) && echo "GIT $d"; fi; '
                '[ -e "$1/.git/HEAD" ] && echo HEAD; env | sed -n "s/^\\(GIT_[A-Za-z0-9_]*\\)=.*/ENV \\1/p"; exit 0',
                "sh", project_dir)
    checks += 1
    lines = (r.stdout or "").splitlines()
    gitdir = next((ln[4:] for ln in lines if ln.startswith("GIT ")), None)
    if r.returncode != 0:
        fails.append("docker exec spot check failed: %s" % (r.stderr or "").strip()[:200])
    elif gitdir is not None or "HEAD" in lines:
        env = [ln[4:] for ln in lines if ln.startswith("ENV ")]
        fails.append("agent: the project is a git repository inside the sandbox (.git visible)"
                     + ("; git dir: %s" % gitdir[:300] if gitdir is not None else "")
                     + ("; GIT_* environment: %s" % ", ".join(env)[:300] if env else ""))
    return {"ok": not fails, "failures": fails, "checks": checks}
