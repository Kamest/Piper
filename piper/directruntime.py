"""Direct agent launches: personal login, Piper events/MCP, native agent permissions.

This module never modifies the personal Claude/Codex configuration or copies credentials.
All generated integration files belong to the Piper session.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import subprocess
from functools import lru_cache

import common
import extensions
import sessioncfg

RUNTIMES = ("hole", "direct")
INSTRUCTIONS = """You are running directly on this machine, in the session's project directory.
Piper manages this session. This is not a Hole container. The agent's native permissions
and sandbox apply; never describe Hole isolation or network filtering as active.
Use the Piper MCP tools for configured HTTP/database connections and approvals. Their
credentials stay with their provider. An unavailable provider is not permission to bypass
its controls or to substitute another environment. Report any verification not performed.
"""


def validate(value: str | None) -> str:
    """Validate the persisted runtime; old sessions retain Hole by default."""
    value = "hole" if value is None else value
    if value not in RUNTIMES:
        raise ValueError("runtime must be hole or direct")
    return value


def prepare(s, role: str, extra: str = "") -> tuple[list[str], dict]:
    """Build a direct CLI launch without disabling its native approval/sandbox settings."""
    binary = os.environ.get("PIPER_%s_BIN" % s.agent.upper(), s.agent)
    found = shutil.which(binary)
    if not found:
        raise ValueError("%s is not installed on this machine or is missing from PATH" % s.agent.capitalize())
    if not os.path.isdir(s.path):
        raise ValueError("project directory does not exist")
    if s.opts:
        raise ValueError("Hole start options do not apply to direct sessions")
    sessioncfg.build_box(s.box, common.REAL_HOME, s.box, role, guard_git=True)
    mcp_path = os.path.join(s.box, "piper", "mcp.json")
    mcp_doc = common.read_json(mcp_path, {})
    mcp_doc["mcpServers"]["piper"]["command"] = sys.executable
    common.write_json(mcp_path, mcp_doc)
    instructions = INSTRUCTIONS + "\n" + extra
    settings_path = os.path.join(s.box, "piper", "settings.json")
    # Hook commands are shell strings; quote every path, including spaces in HOME/worktrees.
    with open(settings_path, encoding="utf-8") as f:
        settings = json.load(f)
    for ev, entries in settings["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                action = "QuietCheckpoint" if "QuietCheckpoint" in hook["command"] else "PostToolUse" if ev == "PostToolUse" else ev
                hook["command"] = shlex.join([sys.executable, os.path.join(s.box, "piper", "piper-event.py"),
                                               action, os.path.join(s.box, s.names["events"])])
    common.write_json(settings_path, settings)
    local_provider(s)
    with open(os.path.join(s.box, "piper", "system-prompt.md"), "w", encoding="utf-8") as f:
        f.write(instructions)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PIPER_", "ORCH_", *extensions.collect("daemon_env"))) and k not in
           ("GITLAB_TOKEN", "GITLAB_PAT", "HOLE_SETTINGS_FILE")}
    env.update(HOME=common.REAL_HOME, TERM="xterm-256color", COLORTERM="truecolor")
    if s.agent == "claude":
        argv = [found, "--mcp-config", os.path.join(s.box, "piper", "mcp.json"),
                "--settings", settings_path, "--append-system-prompt", instructions]
        if s.resume:
            argv += ["--resume", s.resume]
            if s.fork_pending:
                argv += ["--fork-session"]
    else:
        import codexcfg
        personal = codex_settings(s.codex_home)
        # Keep the personal login/history, but set safe launch overrides even if a previous
        # Hole invocation used danger-full-access/never. No project trust is auto-granted.
        env["CODEX_HOME"] = s.codex_home
        # Codex stores trust for CLI hooks under shared session-flags keys. Keep
        # their commands identical across sessions; only the process environment
        # selects the event destination, so approving one session survives another.
        env["PIPER_EVENT_FILE"] = os.path.join(s.box, s.names["events"])
        argv = [found, *foreground_option(found), "--no-alt-screen", "--sandbox", "workspace-write", "--ask-for-approval", "on-request"]
        namespace = "piper_session_" + s.id.replace("-", "_")
        used = set(personal.get("mcp_servers", {}))
        while namespace in used:
            namespace += "_session"
        mcp = {"command": sys.executable, "args": mcp_doc["mcpServers"]["piper"]["args"], "tool_timeout_sec": 960}
        # JSON strings/arrays have the same representation in these TOML inline fields.
        inline = ",".join(k + "=" + json.dumps(v) for k, v in mcp.items())
        argv += ["-c", "mcp_servers.%s={%s}" % (namespace, inline),
                 "-c", "developer_instructions=" + json.dumps(str(personal.get("developer_instructions") or "") + "\n" + instructions)]
        local = common.read_json(os.path.join(s.box, "piper", "mcp.json"), {}).get("mcpServers", {}).get("piper_local")
        if local:
            local_namespace = "piper_local"
            while local_namespace in used:
                local_namespace += "_session"
            argv += ["-c", "mcp_servers.%s={%s}" % (local_namespace, ",".join(k + "=" + json.dumps(v) for k, v in dict(local, tool_timeout_sec=960).items()))]
        for event in codexcfg.EVENTS:
            command = shlex.join([sys.executable, os.path.join(common.PIPER_DIR, "sandbox", "piper-event.py"), "QuietCheckpoint" if event == "PreToolUse" else event])
            inherited = personal.get("hooks", {}).get(event, [])
            if not isinstance(inherited, list):
                raise ValueError("Codex hooks.%s must be an array; fix the native configuration" % event)
            combined = inherited + [{"hooks": [{"type": "command", "command": command}]}]
            argv += ["-c", "hooks.%s=%s" % (event, codexcfg._toml(combined))]
        if s.resume:
            argv += ["fork" if s.fork_pending else "resume", s.resume]
    if s.task and not s.task_sent:
        argv += ["--", s.task]
    return argv, env


@lru_cache(maxsize=16)
def foreground_option(binary: str) -> list[str]:
    """Keep Codex under Piper's process lifetime instead of its shared background daemon."""
    try:
        result = subprocess.run([binary, "--help"], capture_output=True, timeout=5)
        return ["--no-daemon"] if result.returncode == 0 and b"--no-daemon" in result.stdout else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def codex_settings(home: str) -> dict:
    """Preserve native instructions/hooks without ever writing the personal configuration."""
    path = os.path.join(home, "config.toml")
    if not os.path.exists(path):
        return {}
    try:
        import tomllib
    except ImportError as e:
        raise ValueError("Python 3.11+ is required to preserve the existing Codex configuration in direct mode") from e
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("Cannot read the native Codex configuration; repair config.toml before starting") from e


def local_provider(s, box_in: str | None = None) -> dict | None:
    """Attach the laptop tools over the existing mailbox, without exposing its capability.

    Hole needs no route to the notebook or host gateway exception: the server controller
    forwards the request. The private snapshot is outside every session mount.
    """
    config_path = os.path.join(s.dir, "local-provider.json")
    # Linked worktrees are the same registered project; mappings are configured
    # on its base key, just like the project HTTP/DB settings.
    project = s.project.partition("@")[0]
    config = common.read_json(os.path.join(common.PIPER_HOME, "local-providers.json"), {}).get(project)
    if not config or not config.get("enabled"):
        try:
            os.unlink(config_path)
        except FileNotFoundError:
            pass
        return None
    source = os.path.join(common.PIPER_DIR, "sandbox", "piper-local-mcp.py")
    target = os.path.join(s.box, "piper", "piper-local-mcp.py")
    shutil.copy2(source, target)
    common.write_json(config_path, config)
    os.chmod(config_path, 0o600)
    mcp_path = os.path.join(s.box, "piper", "mcp.json")
    doc = common.read_json(mcp_path, {})
    inside = box_in or s.box
    entry = {"command": "python3" if box_in else sys.executable,
             "args": [inside + "/piper/piper-local-mcp.py", "--mailbox", inside + "/" + s.names["mailbox"]],
             "tool_timeout_sec": 960}
    doc["mcpServers"]["piper_local"] = {k: v for k, v in entry.items() if k != "tool_timeout_sec"}
    common.write_json(mcp_path, doc)
    return entry
