"""Generate Piper's hole base settings from the personal global.

piperd passes each session's hole-settings.json through HOLE_SETTINGS_FILE (Hole 2.4+).
Hole 2.1–2.3 use generated ~/.hole/settings.json in the session's pinned isolated HOME.
New 2.4+ sessions use the real HOME. Legacy sessions retain
a pinned isolated HOME for agents, trust, registry and logs (sessions.py).
That file is regenerated before every spawn from the user's ~/.hole/settings.json, so
orchestrated sandboxes keep "the same possibilities as a manual hole" minus the channels
that defeat the exclusion requirement:

  - includes onto ~/.claude            (K2; the per-session config would silently lose, D9)
  - GITLAB_TOKEN and anything = $GITLAB_PAT, the GitLab host in network.allow   (K5)
  - port 2375 in hostGatewayDomains    (K6, the docker proxy)
  - read-write libraries -> read-only  (K15)
  - git.worktreeLinks "off" (K3) - unless rules["keep_worktree_links"] (git_mode "hole", 2026-10-09:
    the personal git.worktreeLinks / worktreePool stay, guard.worktree_layout scans what they mount)
  - `.git` is NOT excluded since 2026-10-01 (the user's decision: git works in the sandbox, its
    history is readable - K1 accepted). force_exclude [".git"] hides it again.
  - includes and libraries whose host path overlaps a protected dir (rules["protected"]:
    ~/.hole, $PIPER_HOME, what an extension protects ...) - guard.preflight would block the start on them
    (personal tokens in ~/.hole, a data dir holding an extension's service state)
  - environment entries the attestation would refuse (rules["secret_env_names"], e.g. NPM_TOKEN)
  - rules["set_env"] set in the base environment (e.g. an extension's service token for the session)
  - rules["add_gateway"] merged into hostGatewayDomains (e.g. an extension's host service port)
  - rules["add_includes"] {host file: container path} added to files.include after the
    sanitizing (the session's generated Maven settings under its box, m2settings)
  - rules["enable_agents"] added to container.enabledAgents (hole's default is ["claude"]; a
    Codex session needs "codex" in the shared agent image)

Agent definitions and allow.txt stay at their original paths, unchanged.
An isolated HOME links its agent registry to the original ~/.hole/agents.

Host-side `~` / $HOME (include and library keys, hook script paths) is rewritten to the
real home so legacy sessions with a different $HOME still resolve the same sources.
Merge semantics are additive (config/merge.go), so profiles are sanitized too.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import shlex

DEFAULTS = {
    # includes onto these container paths are dropped: they hold host transcripts, chat
    # history or OAuth credentials (K2 and the same class for ~/.gemini tmp/history,
    # ~/.sage audit.jsonl). ~/.claude is replaced by the per-session config.
    # ~/.gitconfig: hole includes are always read-write and the HOST git reads that file
    # (piperd's git panel) - a sandbox writing core.fsmonitor there would run code on the host
    # ~/.config/glab-cli: holds the GitLab token; GitLab is served by piperd only (K5), and
    # with "unrestricted_network" the sandbox would reach GitLab directly with it
    # ~/.codex: the host's Codex login and history - a Codex session gets its own (codexcfg)
    "strip_includes": ["~/.claude", "~/.gemini", "~/.sage", "~/.gitconfig", "~/.config/glab-cli", "~/.codex"],
    # every other DIRECTORY include becomes a read-only library (hole has no ro include):
    # a sandbox must not plant skills, hooks or config the host's own tools pick up later.
    # These stay read-write because the sandbox legitimately writes them (the maven cache).
    "rw_includes": ["~/.m2/repository", "~/.m2"],
    "strip_env": ["GITLAB_TOKEN"],
    "strip_env_values": ["$GITLAB_PAT", "${GITLAB_PAT}"],
    "strip_allow": [],   # + the GitLab host while it is served by piperd only (sessions._base_settings)
    "strip_ports": [2375],
    "force_exclude": [],   # [".git"] hides git from the sandbox again (K1)
    # libraries explicitly granted readwrite (host paths as in the personal settings, ~/$VARS allowed);
    # every other one becomes read-only in guarded mode (K15) - pair it with a library_allow if its .git is used
    # (a session can then plant .git/config or .git/hooks that host git - terminal, IDE - runs);
    # a worktree whose main repository is such a read-only library can read git there but not
    # commit (sessions._preflight tells the agent)
    "rw_libraries": [],
    # git_mode "hole": every library keeps the personal settings' readwrite (as a plain hole)
    "keep_rw_libraries": False,
    # git_mode "hole": directory includes stay read-write includes (as a plain hole), rw_includes moot
    "keep_rw_includes": False,
    # git_mode "hole": git.worktreeLinks / worktreePool as the personal settings say (as a plain hole)
    "keep_worktree_links": False,
}

_HOME_RX = re.compile(r"^(~|\$HOME|\$\{HOME\})(?=/|$)")


def _host_path(p: str, real_home: str) -> str:
    return _HOME_RX.sub(lambda _: real_home, p)


def _stripped_target(target: str, real_home: str, strip: list[str]) -> bool:
    t = _host_path(target.rstrip("/"), real_home)
    return any(t == _host_path(s.rstrip("/"), real_home) for s in strip)


def _overlaps(host: str, real_home: str, protected: list[str]) -> str | None:
    """The protected dir `host` lies in or contains (realpath, as guard._inside), else None."""
    h = os.path.realpath(os.path.expandvars(_host_path(host, real_home)))
    for p in protected:
        rp = os.path.realpath(p)
        if h == rp or h.startswith(rp.rstrip("/") + "/") or rp.startswith(h.rstrip("/") + "/"):
            return p
    return None


def _split_library(host: str, spec, real_home: str, protected: list[str]) -> list[tuple]:
    """[(host path, spec, protected hit)] for one library (W1): a library that CONTAINS a protected
    dir (e.g. a data dir holding a protected service home) is mounted as its subdirectories minus the
    protected one - recursively, so only the protected path is cut out. A library lying in a
    protected dir comes back whole with its hit (removed by the caller). Files directly in the
    split dir are left out (a hole library is a directory); the list is taken at generation,
    i.e. before every spawn."""
    hp = _host_path(host, real_home)
    hit = _overlaps(host, real_home, protected)
    if not hit:
        return [(hp, spec, None)]
    h = os.path.realpath(os.path.expandvars(hp))
    if not any(os.path.realpath(p).startswith(h.rstrip("/") + "/") for p in protected):
        return [(hp, spec, hit)]   # equal to or inside a protected dir
    try:
        names = sorted(os.listdir(os.path.expandvars(hp)))
    except OSError:
        return [(hp, spec, hit)]
    out = []
    for name in names:
        child = os.path.join(hp, name)
        if not os.path.isdir(os.path.expandvars(child)):
            continue
        sub = dict(spec, path=spec["path"].rstrip("/") + "/" + name) \
            if isinstance(spec, dict) and spec.get("path") else spec
        out += _split_library(child, sub, real_home, protected)
    return out


def _listed(host: str, paths, real_home: str) -> bool:
    def norm(p: str) -> str:
        return os.path.realpath(os.path.expandvars(_host_path(p, real_home)))
    return any(norm(host) == norm(p) for p in paths or [])


def _sanitize(doc: dict, real_home: str, rules: dict, changes: list, where: str) -> dict:
    doc = copy.deepcopy(doc)
    protected = list(rules.get("protected") or [])
    files = doc.get("files") or {}
    inc = files.get("include")
    if isinstance(inc, dict):
        new = {}
        for host, spec in inc.items():
            target = spec if isinstance(spec, str) else (spec or {}).get("path", "")
            if _stripped_target(str(target), real_home, rules["strip_includes"]):
                changes.append("%sfiles.include %s -> %s removed (host transcripts/credentials/config)"
                               % (where, host, target))
                continue
            hit = _overlaps(host, real_home, protected)
            if hit:
                changes.append("%sfiles.include %s -> %s removed (overlaps protected %s)"
                               % (where, host, target, hit))
                continue
            hp = _host_path(host, real_home)
            if (os.path.isdir(os.path.expandvars(hp)) and rules.get("container_home") and not rules.get("keep_rw_includes")
                    and not _stripped_target(str(target), real_home, rules["rw_includes"])):
                cpath = re.sub(r"^(~|\$HOME|\$\{HOME\})(?=/|$)", rules["container_home"], str(target))
                doc.setdefault("libraries", {})[hp] = {"path": cpath}
                changes.append("%sfiles.include %s -> %s is now a read-only library" % (where, host, target))
                continue
            new[hp] = spec
        files["include"] = new
    env = doc.get("environment")
    if isinstance(env, dict):
        for k in list(env):
            v = str(env[k])
            if k in rules["strip_env"] or any(s in v for s in rules["strip_env_values"]):
                changes.append("%senvironment.%s removed (GitLab is served by piperd)" % (where, k))
                del env[k]
            elif k in (rules.get("secret_env_names") or ()):
                # attestation (guard.env_failures) refuses a sandbox that carries it anyway -
                # only after the whole image build; keep the token on the host instead
                changes.append("%senvironment.%s removed (secret-looking - stays on the host)" % (where, k))
                del env[k]
    net = doc.get("network") or {}
    allow = net.get("allow")
    if isinstance(allow, list):
        kept = [a for a in allow if a not in rules["strip_allow"]]
        for a in allow:
            if a not in kept:
                changes.append("%snetwork.allow %s removed" % (where, a))
        net["allow"] = kept
    hgd = net.get("hostGatewayDomains")
    if isinstance(hgd, list):
        out = []
        strip = {str(p) for p in rules["strip_ports"]}
        for entry in hgd:
            host, _, ports = str(entry).partition(":")
            keep = [p for p in ports.split(",") if p and p not in strip]
            if len(keep) != len([p for p in ports.split(",") if p]):
                changes.append("%snetwork.hostGatewayDomains %s: port(s) %s removed"
                               % (where, entry, ",".join(sorted(strip & set(ports.split(","))))))
            if keep:
                out.append("%s:%s" % (host, ",".join(keep)))
        net["hostGatewayDomains"] = out
    libs = doc.get("libraries")
    if isinstance(libs, dict):
        new = {}
        for host, spec in libs.items():
            for lib, sub, hit in _split_library(host, spec, real_home, protected):
                if hit:
                    changes.append("%slibraries %s removed (overlaps protected %s)"
                                   % (where, host if lib == _host_path(host, real_home) else lib, hit))
                    continue
                if lib != _host_path(host, real_home):
                    changes.append("%slibraries %s mounted as %s (a protected dir lies inside it)" % (where, host, lib))
                if _listed(host, rules.get("rw_libraries"), real_home) or _listed(lib, rules.get("rw_libraries"), real_home):
                    if not isinstance(sub, dict) or not sub.get("readwrite"):
                        sub = dict(sub, readwrite=True) if isinstance(sub, dict) else {"path": sub, "readwrite": True}
                        changes.append("%slibraries %s -> read-write (explicit writable library)" % (where, lib))
                elif isinstance(sub, dict) and sub.get("readwrite") and not rules.get("keep_rw_libraries"):
                    sub = {k: v for k, v in sub.items() if k != "readwrite"}
                    changes.append("%slibraries %s -> read-only" % (where, lib))
                new[lib] = sub
        doc["libraries"] = new
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        for phase, items in hooks.items():
            for item in items or []:
                if isinstance(item, dict) and "script" in item:
                    item["script"] = _host_path(str(item["script"]), real_home)
                    if phase in HOST_HOOKS and rules.get("hook_wrap_dir"):
                        item["script"] = _wrap(item["script"], rules["hook_wrap_dir"], real_home)
    git = doc.get("git")
    if isinstance(git, dict):
        for k in ("worktreeLinks", "worktreePool"):
            if k in git and where and not rules.get("keep_worktree_links"):   # profiles must not re-enable what the base turned off
                changes.append("%sgit.%s removed" % (where, k))
                del git[k]
    return doc


HOST_HOOKS = ("setupHost", "cleanupHost")


def _wrap(script: str, wrap_dir: str, real_home: str) -> str:
    """Host hooks run with HOME=hole-home; give them the real HOME back (a personal
    setupHost that reads $HOME/.claude/... would fail otherwise). hole runs hooks as
    scripts, so a wrapper that execs the original is transparent."""
    os.makedirs(wrap_dir, mode=0o700, exist_ok=True)
    script = os.path.expandvars(script)   # hole would expand $VAR itself; quoting below must not freeze it
    name = "hook-%s.sh" % hashlib.sha1(script.encode()).hexdigest()[:12]
    path = os.path.join(wrap_dir, name)
    # hole runs hooks as `bash <script>` (hooks.go), so the target need not be executable
    body = ("#!/bin/sh\n# generated by piperd (holehome.py): run %s with the real HOME\n"
            "HOME=%s; export HOME\nexec bash %s \"$@\"\n" % (script, shlex.quote(real_home), shlex.quote(script)))
    current = None
    if os.path.exists(path):
        with open(path) as f:
            current = f.read()
    if current != body:
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, 0o700)
    return path


def generate(personal: dict, real_home: str, project_path: str | None, rules: dict | None = None
             ) -> tuple[dict, list[str]]:
    """Return (the session's HOLE_SETTINGS_FILE document, human-readable changes)."""
    r = dict(DEFAULTS, **(rules or {}))
    host_claude = r.get("claude_dir") == "host"
    changes: list[str] = []
    profiles = personal.get("profiles") or {}
    base = {k: v for k, v in personal.items() if k != "profiles"}
    out = _sanitize(base, real_home, r, changes, "")
    # Piper's hook scripts and both stdio MCP servers require Python even when
    # the server has no personal Hole configuration yet. Hole's base image does not.
    dependencies = out.setdefault("dependencies", [])
    if isinstance(dependencies, list) and "python3" not in dependencies:
        dependencies.append("python3")
        changes.append("dependencies python3 added (Piper hooks and MCP)")
    files = out.setdefault("files", {})
    if host_claude:   # the user's choice (policy claude_dir=host): every other ~/.claude include
        # is stripped as usual (profiles too), then the real one goes back in, once, rw - for
        # every sandbox of this base, a Codex one too (K2 accepted with the policy)
        files.setdefault("include", {})[real_home + "/.claude"] = "~/.claude"
        changes.append("files.include %s/.claude -> ~/.claude (policy claude_dir=host - K2 accepted)" % real_home)
    for host, target in (r.get("add_includes") or {}).items():   # piperd-written, under the session's box
        files.setdefault("include", {})[host] = target
        changes.append("files.include %s -> %s (generated by piperd)" % (host, target))
    set_env = r.get("set_env") or {}
    if set_env:
        env = out.setdefault("environment", {})
        for k, v in set_env.items():
            if env.get(k) != v:
                changes.append("environment.%s -> %s" % (k, v))
            env[k] = v
    for entry in r.get("add_gateway") or []:   # e.g. an extension's host service the session reaches
        hgd = out.setdefault("network", {}).setdefault("hostGatewayDomains", [])
        host, _, port = entry.partition(":")
        for i, e in enumerate(hgd):
            h, _, ps = str(e).partition(":")
            if h == host:
                if port not in ps.split(","):
                    hgd[i] = "%s:%s" % (h, ",".join([p for p in ps.split(",") if p] + [port]))
                    changes.append("network.hostGatewayDomains %s added" % entry)
                break
        else:
            hgd.append(entry)
            changes.append("network.hostGatewayDomains %s added" % entry)
    if r.get("enable_agents"):
        cont = out.setdefault("container", {})
        agents = list(cont.get("enabledAgents") or ["claude"])
        for a in r["enable_agents"]:
            if a not in agents:
                agents.append(a)
                changes.append("container.enabledAgents %s added" % a)
        cont["enabledAgents"] = agents
    exclude = list(files.get("exclude") or [])
    for e in r["force_exclude"]:
        if e not in exclude:
            exclude.append(e)
            changes.append("files.exclude %s added" % e)
    files["exclude"] = exclude
    if not r.get("keep_worktree_links"):
        git = out.setdefault("git", {})
        if git.get("worktreeLinks") != "off":
            changes.append("git.worktreeLinks -> off")
        git["worktreeLinks"] = "off"
        git.pop("worktreePool", None)
    out_profiles = {name: _sanitize(p or {}, real_home, r, changes, "profiles.%s." % name)
                    for name, p in profiles.items()}
    # Libraries (the personal global's, plus directory includes turned into libraries above)
    # belong to PROJECT sessions only. The Global agent must not mount them: it reads
    # projects through piperd's tools instead (D14-B), so it has no directory to hide and is
    # immune to K18. Profiles are additive, so the libraries move from the base into a
    # generated profile `project` that project sessions select and every other personal
    # profile extends - a project that names its own profile still gets them.
    libs = out.pop("libraries", None) or {}
    proj = out_profiles.setdefault(PROJECT_PROFILE, {})
    if libs:
        proj.setdefault("libraries", {}).update(libs)
        changes.append("libraries -> profile %r (project sessions only, not the Global agent)" % PROJECT_PROFILE)
    for name, prof in out_profiles.items():
        if name in (PROJECT_PROFILE, ADVISOR_PROFILE):
            continue
        ext = prof.get("extends") or []
        ext = [ext] if isinstance(ext, str) else list(ext)
        if PROJECT_PROFILE not in ext:
            prof["extends"] = ext + [PROJECT_PROFILE]
    adv = out_profiles.setdefault(ADVISOR_PROFILE, {})
    adv.pop("libraries", None)
    adv.pop("extends", None)
    out["profiles"] = out_profiles
    return out, changes


def link_agents(src: str, dst: str) -> list[str]:
    """Use the original agent registry from a pinned legacy HOME, without rewriting it.

    Replace only Piper's old generated copy/link. A missing source stays a dangling
    link so agents installed later are visible without another snapshot.
    """
    if os.path.islink(dst) and os.readlink(dst) == src:
        return []
    import shutil
    if os.path.isdir(dst) and not os.path.islink(dst):
        shutil.rmtree(dst)
    elif os.path.lexists(dst):
        os.unlink(dst)
    os.symlink(src, dst, target_is_directory=True)
    return ["agents: linked to %s (unchanged allow.txt)" % src]


PROJECT_PROFILE = "project"
ADVISOR_PROFILE = "advisor"


def container_home(hole_home: str) -> str:
    """AGENT_HOME is the hole process's $HOME (composegen.go), i.e. hole-home."""
    return os.path.abspath(hole_home)
