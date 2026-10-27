"""Per-session Codex home (CODEX_HOME), mounted as ~/.codex of exactly one sandbox - the
Codex counterpart of sessioncfg's per-session ~/.claude.

$PIPER_HOME/codex-template/ is the user-curated source, synced on every start from ~/.codex with
configuration only (AGENTS.md, config.toml, skills, rules, prompts, keybindings.json) -
never sessions, history, logs, databases or auth.json. Every start builds a fresh dir:

  AGENTS.md      the template's + the "orchestrated session" block (+ the issue block)
  config.toml    whitelisted scalar keys and remote HTTP MCP servers of the template's
                 (host processes, loopback endpoints and plugins need adaptation) + Piper's MCP server, the
                 project trusted, no update check
  hooks.json     -> piper-event.py, the same events file Claude's hooks write
  sessions/      carried over (codex resume)
  auth.json      carried over / seeded from the template

Login: never the host's ~/.codex/auth.json - OpenAI rotates refresh tokens, a sandbox refresh
would log the host's Codex out. The first login in a session ("Sign in with Device Code" - the
browser callback cannot reach the sandbox) is copied
back to the template on the session's next hook event, when it ends and when another Codex
session starts, and serves the next starts while it still runs (sessioncfg's variant B).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import time
from urllib.parse import urlsplit

import common
import sessioncfg

TEMPLATE_DIR = os.path.join(common.PIPER_HOME, "codex-template")
SEED = ["AGENTS.md", "config.toml", "skills", "rules", "prompts", "keybindings.json"]
# copied from the template as they are; config.toml is generated, auth.json is the login
COPY = [n for n in SEED if n != "config.toml"]
CARRY = ["sessions", "archived_sessions"]   # rollouts: `codex resume <id>` reads them
# no model_provider: it names a [model_providers.*] table (a URL, maybe a key) that is not copied
CONFIG_KEYS = ("model", "model_reasoning_effort", "model_reasoning_summary",
               "model_verbosity", "personality", "service_tier")
HTTP_MCP_KEYS = ("url", "auth", "bearer_token_env_var", "http_headers", "env_http_headers",
                 "startup_timeout_sec", "tool_timeout_sec", "enabled", "required", "enabled_tools",
                 "disabled_tools", "default_tools_approval_mode", "tools", "oauth")
# PermissionRequest stands in for Claude's Notification (Codex has none); hole runs Codex with
# --dangerously-bypass-approvals-and-sandbox, so it is rare
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop")
AUTH = "auth.json"
# exists once a Codex sandbox was attested: from then on hole's shared agent image includes codex
# (sessions._base_settings); delete it to drop codex from container.enabledAgents again
ENABLED_MARK = os.path.join(TEMPLATE_DIR, ".enabled")


def sqlite_home() -> str:
    """Persistent Linux-only SQLite state; host Codex keeps the databases at CODEX_HOME's root."""
    return os.path.join(common.REAL_HOME, ".codex", "piper-sqlite-linux")


def mark_enabled() -> None:
    os.makedirs(TEMPLATE_DIR, mode=0o700, exist_ok=True)
    with open(ENABLED_MARK, "a"):
        pass


def seed_template(real_codex: str) -> list[str]:
    """Create the template from ~/.codex (configuration only). Returns what was copied."""
    os.makedirs(TEMPLATE_DIR, mode=0o700, exist_ok=True)
    copied = []
    for name in SEED:
        src, dst = os.path.join(real_codex, name), os.path.join(TEMPLATE_DIR, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False, ignore=sessioncfg._skip)
        else:
            shutil.copy2(src, dst)
        copied.append(name)
    return copied


def sync_template(real_codex: str) -> tuple[list[str], list[str], list[str]]:
    """The template follows ~/.codex (SEED only - never auth.json, sessions, history), an item
    edited in the template kept: sessioncfg.sync_template. Returns (updated, kept, skipped)."""
    return sessioncfg.sync_template(real_codex, TEMPLATE_DIR, SEED, referenced=False)


def template_empty() -> bool:
    return not os.path.isdir(TEMPLATE_DIR) or not [n for n in os.listdir(TEMPLATE_DIR) if not n.startswith(".")]


def _toml(v) -> str:
    """JSON string escapes are also TOML's; dictionaries become inline tables."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(_toml(k) + " = " + _toml(x) for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)


# R3 (2026-10-09): a static credential in the host template's http_headers would land in the sandbox's
# config.toml, readable by the agent - only plain headers go (env_http_headers / bearer_token_env_var name an
# env var the sandbox does not have). Credential-looking names or values are dropped.
_CRED_NAME = re.compile(r"(?i)auth|token|secret|key|cookie|session|passw|cred|signature|bearer")
_CRED_VALUE = re.compile(r"(?i)^\s*(bearer|basic|token|digest)\s|^[A-Za-z0-9_\-.=+/:]{24,}$")
# hosts no sandbox may be pointed at by a template: the host gateway, cloud metadata, *.internal
_BLOCKED_HOST = re.compile(r"(?i)^(host\.internal|host\.docker\.internal|gateway\.docker\.internal|metadata)$|\.internal$")


def _plain_headers(v):
    if not isinstance(v, dict):
        return None
    return {k: x for k, x in v.items() if isinstance(k, str) and isinstance(x, str)
            and not _CRED_NAME.search(k) and not _CRED_VALUE.search(x)}


# a host name as DNS writes it (letters, digits, hyphens; no trailing dot, no "_", no non-ASCII the IDNA
# step could fold into an address, e.g. full-width digits)
_HOST_NAME = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _blocked_address(host: str) -> bool:
    """Fail closed: a remote MCP is a public endpoint written plainly, or nothing. Refused: loopback,
    unspecified, private, link-local (169.254.169.254), reserved, multicast; any IPv6 that embeds an IPv4
    (mapped, compatible, 6to4, Teredo, NAT64); every spelling of an IPv4 the resolver accepts besides the
    four-decimal one (2130706433, 0x7f000001, 0177.0.0.1, 127.1 - socket.inet_aton like dbtools.loopback_host);
    a trailing dot, a name DNS does not write; the gateway / metadata names."""
    h = str(host or "").lower()
    if not h.isascii():
        return True
    if ":" in h:   # an IPv6 literal (urlsplit took the brackets off)
        try:
            a = ipaddress.IPv6Address(h)
        except ValueError:
            return True
        if a.ipv4_mapped or a.sixtofour or a.teredo or a in _NAT64 or (int(a) >> 32 == 0) or not a.is_global or \
                a.is_multicast or a.scope_id:
            return True
        return False
    if not _HOST_NAME.match(h):
        return True
    if h == "localhost" or h.endswith(".localhost") or _BLOCKED_HOST.search(h):
        return True
    try:
        packed = socket.inet_aton(h)   # every form the C resolver reads as an IPv4
    except OSError:
        packed = None
    if packed is not None:
        a4 = ipaddress.IPv4Address(packed)
        if str(a4) != h:   # an integer, hex, octal, short or zero-padded spelling
            return True
        return not a4.is_global or a4.is_multicast
    if h.rsplit(".", 1)[-1].isdigit() or h.rsplit(".", 1)[-1].startswith("0x"):
        return True   # a numeric last label that is no address the resolver reads: no DNS name either
    return False


def _url_credential(url) -> bool:
    """A credential in the URL: userinfo (even an empty one). The query / fragment / path-segment heuristics
    (N4-N6) are off since 2026-10-10 (the user's decision): they silently dropped legitimate servers
    (`?author=`, `?keyword=`, a UUID in the path); a key in the URL of a remote MCP is the user's choice."""
    return "@" in url.netloc


def _http_mcp(base: dict, dropped: list | None = None, skipped: list | None = None) -> list[str]:
    """`dropped` collects "server: Header" for every http_headers entry _plain_headers left out (R3+:
    the doctor names them - the name only, never the value); `skipped` the names of remote servers whose URL
    no sandbox gets (2026-10-10: an address on this machine / a private network, userinfo, an odd form)."""
    lines = []
    servers = base.get("mcp_servers", {})
    if not isinstance(servers, dict):
        return lines
    for name, cfg in servers.items():
        if name in ("piper", "idea", "piper_local") or not isinstance(cfg, dict) or not isinstance(cfg.get("url"), str) or \
                "command" in cfg or "http_headers_helper" in cfg:
            continue
        raw = cfg["url"]
        if not raw.isascii() or any(c <= " " or c in "\\\x7f" for c in raw):   # whitespace, controls, a backslash:
            if skipped is not None:   # parsers disagree on where the host is (WHATWG drops tabs / newlines, reads \ as /)
                skipped.append(name)
            continue
        try:
            url = urlsplit(raw)
            host = url.hostname
            bad = url.scheme not in ("http", "https") or not host or url.username or url.password or \
                _url_credential(url) or _blocked_address(host)
        except (TypeError, ValueError):
            bad = True
        if bad:
            if skipped is not None:
                skipped.append(name)
            continue
        # Host processes and loopback endpoints cannot run unchanged inside Hole.
        lines += ["[mcp_servers.%s]" % _toml(name)]
        for k in HTTP_MCP_KEYS:
            if k not in cfg:
                continue
            v = _plain_headers(cfg[k]) if k == "http_headers" else cfg[k]
            if k == "http_headers" and dropped is not None and isinstance(cfg[k], dict):
                dropped += ["%s: %s" % (name, h) for h in cfg[k] if h not in (v or {})]
            if v:
                lines.append("%s = %s" % (k, _toml(v)))
            elif k != "http_headers":
                lines.append("%s = %s" % (k, _toml(v)))
        lines.append("")
    return lines


def dropped_headers(template_text: str) -> list[str]:
    """R3+: "server: Header" of every template http_headers entry no session gets (a credential-looking
    name or value) - such a remote MCP may fail in the sandbox, and the human should know why."""
    try:
        import tomllib
        base = tomllib.loads(template_text) if template_text else {}
    except (ImportError, ValueError):
        return []
    out: list[str] = []
    _http_mcp(base, out)
    return [common.no_bidi(x)[:120] for x in out]


def skipped_servers(template_text: str) -> list[str]:
    """2026-10-10 (the user): the names of the template's remote MCP servers no session gets - their URL points
    at this machine, a private network, holds userinfo or an odd address form (_blocked_address); the doctor
    names them so a missing server is not a silent mystery. Names only, never the URL."""
    try:
        import tomllib
        base = tomllib.loads(template_text) if template_text else {}
    except (ImportError, ValueError):
        return []
    out: list[str] = []
    _http_mcp(base, None, out)
    return [common.no_bidi(str(x))[:80] for x in out]


def config_toml(template_text: str, box_in: str, role: str, project_dir: str, idea: list | None = None) -> str:
    try:
        import tomllib
        base = tomllib.loads(template_text) if template_text else {}
    except (ImportError, ValueError):
        base = {}
    lines = ["# generated by piperd (codexcfg.py) for this session only"]
    lines += ["%s = %s" % (k, _toml(base[k])) for k in CONFIG_KEYS
              if isinstance(base.get(k), (str, bool, int, float))]
    # Hole supplies the isolation; nested bwrap cannot create a namespace in its container.
    lines += ['sandbox_mode = "danger-full-access"', 'approval_policy = "never"',
              "check_for_update_on_startup = false", "",
              "[notice]", "hide_full_access_warning = true", "",
              "[mcp_servers.piper]", 'command = "python3"',
              # a tool behind a card waits up to 15 min (piper-mcp CONFIRMED); Codex gives up after 60 s by
              # default and then retries a write the user approves meanwhile - a duplicate issue / MR
              "tool_timeout_sec = 960",
              "args = %s" % _toml([box_in + "/piper/piper-mcp.py", "--role", role,
                                   "--mailbox", box_in + "/piper-mailbox"]), ""]
    if idea:   # policy idea_in_sessions: idea-bridge.py is in the box (sessioncfg.build_box)
        lines += ["[mcp_servers.idea]", 'command = "python3"',
                  "args = %s" % _toml([box_in + "/piper/idea-bridge.py"] + list(idea)), ""]
    lines += _http_mcp(base)
    lines += ["[projects.%s]" % _toml(project_dir), 'trust_level = "trusted"', ""]
    return "\n".join(lines)


def hooks_json(box_in: str, events_name: str) -> dict:
    return {"hooks": {ev: [{"hooks": [{"type": "command", "command": "python3 %s/piper/piper-event.py %s %s/%s"
                                       % (box_in, "QuietCheckpoint" if ev == "PreToolUse" else ev, box_in, events_name)}]}]
                      for ev in EVENTS}}


def copy_host_hooks(destination: str) -> None:
    """Snapshot scripts referenced below ~/.claude/hooks for a read-only session mount.

    Keep the original paths: Codex combines hooks.json and CLI hooks, so rewriting an
    override would leave the original hook active too. Never copy unrelated Claude data.
    """
    sessioncfg.fresh_dir(destination)
    claude = os.path.join(common.REAL_HOME, ".claude")
    root = os.path.join(claude, "hooks")
    refs = set()
    for name in ("hooks.json", "config.toml"):
        refs.update(sessioncfg.referenced_files(os.path.join(common.REAL_HOME, ".codex", name), claude))
    for rel in sorted(refs):
        if not rel.startswith("hooks/"):
            continue
        content = sessioncfg._read_regular(os.path.join(claude, rel),
                                          ok=lambda path, st: path.startswith(root + os.sep))
        if content is None:
            raise ValueError("Codex hook script is missing or unsafe: " + rel)
        data, mode = content
        target = rel[len("hooks/"):]
        os.makedirs(os.path.dirname(os.path.join(destination, target)), exist_ok=True)
        common.safe_write(destination, target, data, mode=mode, exact_mode=True)


def overrides(box_in: str, role: str, project_dir: str, events_name: str, extra_md: str = "",
              idea: list | None = None, local: dict | None = None) -> list[str]:
    """`codex -c` args with what build() writes into a session CODEX_HOME, for a session on the
    real ~/.codex (policy codex_dir=host): Piper never writes there, the host Codex reads it too."""
    mcp = {"piper": {"command": "python3", "tool_timeout_sec": 960,   # 960: see config_toml
                     "args": [box_in + "/piper/piper-mcp.py", "--role", role, "--mailbox", box_in + "/piper-mailbox"]}}
    if idea:
        mcp["idea"] = {"command": "python3", "args": [box_in + "/piper/idea-bridge.py"] + list(idea)}
    if local:
        mcp["piper_local"] = dict(local, tool_timeout_sec=960)
    out = ["check_for_update_on_startup=false", "notice.hide_full_access_warning=true",
           "notify=[]",   # the host desktop notifier cannot execute in a Linux sandbox
           "sqlite_home=%s" % _toml(sqlite_home()),   # config wins over CODEX_SQLITE_HOME
           # one inline table: codex -c splits a dotted key on every "." (and the first "="), quotes or not -
           # a path with a dot (a worktree under .claude/) would be ignored as projects."<half>"
           "projects={%s={trust_level=\"trusted\"}}" % _toml(project_dir),
           "developer_instructions=%s" % _toml(sessioncfg.CLAUDE_MD_BLOCK.strip() + extra_md)]
    # Codex merges -c tables with the host's config: replacing an HTTP server with
    # command/args leaves its url behind and prevents the CLI from starting. Keep
    # the known host entry disabled for this invocation. Always use a separate,
    # stable session namespace: project/profile/system layers can define these
    # servers too, and their effective configuration is resolved inside Codex.
    try:
        import tomllib
        with open(os.path.join(common.REAL_HOME, ".codex", "config.toml"), "rb") as f:
            host_mcp = tomllib.load(f).get("mcp_servers", {})
    except (ImportError, OSError, ValueError):
        host_mcp = {}
    used = set(host_mcp) if isinstance(host_mcp, dict) else set()
    namespace = _digest(box_in.encode())[:16]
    for k, v in mcp.items():
        if k in used:
            out.append("mcp_servers.%s.enabled=false" % k)
        base = {"piper": "piper_session_", "idea": "piper_idea_", "piper_local": "piper_local_"}[k] + namespace
        name, suffix = base, 2
        while name in used:
            name = "%s_%d" % (base, suffix)
            suffix += 1
        used.add(name)
        out.append("mcp_servers.%s={%s}" % (name, ", ".join("%s=%s" % (kk, _toml(vv)) for kk, vv in v.items())))
    for ev, entries in hooks_json(box_in, events_name)["hooks"].items():
        cmd = entries[0]["hooks"][0]["command"]
        out.append('hooks.%s=[{hooks=[{type="command", command=%s}]}]' % (ev, _toml(cmd)))
    return [x for o in out for x in ("-c", o)]


def add_local_provider(codex_dir: str, config: dict) -> None:
    """Add the reserved notebook namespace to a freshly generated session configuration."""
    with open(os.path.join(codex_dir, "config.toml"), "a", encoding="utf-8") as stream:
        stream.write("\n[mcp_servers.piper_local]\n" + "\n".join("%s = %s" % (k, _toml(v)) for k, v in config.items()) + "\n")


def build(codex_dir: str, box_in: str, role: str, project_dir: str, events_name: str, extra_md: str = "",
          idea: list | None = None) -> None:
    """Create a FRESH session CODEX_HOME (on every start, including retry/resume).

    The previous dir was mounted read-write into the sandbox: it is renamed away and only the
    rollouts (by renaming the directory entries, never following them) and the login
    (through safe_read) are carried over - as sessioncfg.build does for ~/.claude.
    """
    prev = None
    if os.path.lexists(codex_dir):
        prev = "%s.prev-%d" % (codex_dir, int(time.time() * 1000))
        os.rename(codex_dir, prev)
    os.makedirs(codex_dir, mode=0o700)
    with sessioncfg.template_lock():   # sync_template replaces items of this template too
        for name in COPY:
            src, dst = os.path.join(TEMPLATE_DIR, name), os.path.join(codex_dir, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False, ignore=sessioncfg._skip)
            elif os.path.isfile(src):
                shutil.copy2(src, dst)
    if prev is not None:
        try:
            common.safe_write(codex_dir, "session_index.jsonl", common.safe_read(prev, "session_index.jsonl", 8 << 20), mode=0o600)
        except OSError:
            pass
        for name in CARRY:
            try:
                if stat.S_ISDIR(os.lstat(os.path.join(prev, name)).st_mode):
                    os.rename(os.path.join(prev, name), os.path.join(codex_dir, name))
            except FileNotFoundError:
                pass
        try:   # used only while the template has no login (seed_credentials)
            sessioncfg._write_private(os.path.join(codex_dir, AUTH), common.safe_read(prev, AUTH, 64 << 10))
        except OSError:
            pass
        shutil.rmtree(prev, ignore_errors=True)   # fd-based, does not follow symlinks
    try:
        with open(os.path.join(TEMPLATE_DIR, "config.toml")) as f:
            tpl = f.read()
    except OSError:
        tpl = ""
    with open(os.path.join(codex_dir, "config.toml"), "w") as f:
        f.write(config_toml(tpl, box_in, role, project_dir, idea))
    with open(os.path.join(codex_dir, "hooks.json"), "w") as f:
        json.dump(hooks_json(box_in, events_name), f, indent=1)
    md = os.path.join(codex_dir, "AGENTS.md")
    text = ""
    if os.path.exists(md):
        with open(md) as f:
            text = f.read()
    with open(md, "w") as f:
        f.write((text.rstrip() + "\n" if text.strip() else "") + sessioncfg.CLAUDE_MD_BLOCK + extra_md)


def _digest(raw: bytes) -> str:
    import hashlib
    return hashlib.sha256(raw).hexdigest()


def copy_back_credentials(codex_dir: str, seeded: str, dry: bool = False) -> str:
    """The session's login becomes the template's when it CHANGED in the session (a login, a token
    refresh) - compared by content with `seeded` (seed_credentials' digest), never by the mtime the
    sandbox controls. Sandbox-written: safe_read, small JSON. Returns the digest of the login
    saved ("" nothing saved); `dry` only returns the session's one."""
    try:
        raw = common.safe_read(codex_dir, AUTH, 64 << 10)
        json.loads(raw.decode())
    except (OSError, ValueError):
        return ""
    if dry:
        return _digest(raw)
    if _digest(raw) == seeded:
        return ""
    os.makedirs(TEMPLATE_DIR, mode=0o700, exist_ok=True)
    sessioncfg._write_private(os.path.join(TEMPLATE_DIR, AUTH), raw)
    return _digest(raw)


def seed_credentials(codex_dir: str) -> str:
    """The template's login (the last one a session saved) replaces the carried one; without it the
    carried login stays. Returns the digest of the login the session starts with ("" none)."""
    try:
        with open(os.path.join(TEMPLATE_DIR, AUTH), "rb") as f:
            sessioncfg._write_private(os.path.join(codex_dir, AUTH), f.read(64 << 10))
    except OSError:
        pass
    try:
        return _digest(common.safe_read(codex_dir, AUTH, 64 << 10))
    except OSError:
        return ""
