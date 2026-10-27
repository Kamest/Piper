#!/usr/bin/env python3
"""piper-event - Claude Code hook inside an orchestrated sandbox.

Appends one JSON line per hook event to ~/.claude/piper-events.jsonl (the session's own
config dir, read by piperd on the host), or to the file given as the 2nd argument.
Direct Codex uses PIPER_EVENT_FILE when no path argument is supplied, keeping the
hook definition stable across sessions for native hook trust.
Usage in settings.json: `python3 <this> <Event> [events-file]`.
The hook input arrives on stdin as JSON; only a few fields are kept. Never fails the hook.

As the PreToolUse hook of Bash it logs nothing: a git command that would drop the host's other
worktrees or objects (gc, prune, repack, worktree prune/remove - after global options, behind
env / sudo / xargs / sh -c too) gets "ask", which the permissions.ask prefix rules miss
(`git -C x gc`). Best effort, not a boundary: the sandbox can edit its own settings.

As PostToolUse (Claude: Edit|Write|MultiEdit|NotebookEdit; Codex: every tool) it keeps the edited
file's PATH only - tool_input.file_path / notebook_path, or the `*** Add|Update|Delete File:` /
`*** Move to:` names of an apply_patch - never the content, the patch or the tool's answer
(the GUI re-reads the open file).
"""
import hashlib
import json
import os
import re
import shlex
import sys
import time

KEEP = ("session_id", "hook_event_name", "notification_type", "message", "tool_name", "stop_hook_active", "source",
        "agent_id", "agent_type", "prompt")   # UserPromptSubmit: the turn label in the GUI (9.7 #2), cut to 300 chars below


GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix")
GIT_ASK_SUBS = ("gc", "prune", "repack", "prune-packed")


def _words(command: str) -> list[str]:
    try:
        lx = shlex.shlex(command.replace("`", " ; ").replace("$(", " ; "), posix=True, punctuation_chars=";&|()<>")
        lx.whitespace_split = True
        return list(lx)
    except ValueError:   # an unclosed quote: words as they stand
        return re.split(r"[\s;&|()<>`]+", command)


def risky_git(command: str, depth: int = 0) -> str:
    """The first git invocation in a shell command line that GIT_ASK_SUBS / worktree prune|remove names."""
    toks = _words(command)
    for i, t in enumerate(toks):
        if depth < 3 and re.search(r"\s", t):   # sh -c "git -C x gc"
            hit = risky_git(t, depth + 1)
            if hit:
                return hit
        if os.path.basename(t) != "git":
            continue
        j = i + 1
        while j < len(toks) and toks[j].startswith("-"):
            j += 2 if toks[j] in GIT_VALUE_OPTS else 1
        sub = toks[j] if j < len(toks) else ""
        if sub in GIT_ASK_SUBS or (sub == "worktree" and j + 1 < len(toks) and toks[j + 1] in ("prune", "remove")):
            return " ".join(toks[i:j + 2])
    return ""


def pre_tool_use(data) -> int:
    cmd = (data.get("tool_input") or {}).get("command") if isinstance(data, dict) else None
    hit = risky_git(cmd) if isinstance(cmd, str) else ""
    if hit:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": "Piper: `%s` can drop the host's other worktrees or git objects "
                                        "this sandbox does not see - the user decides." % hit[:120]}}))
    return 0


PATCH_NAMES = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M)


def tool_key(data) -> str:
    """Correlate approval/completion without retaining commands or tool arguments.

    Codex PermissionRequest does not supply tool_use_id. Bash requests can add
    a description absent from PostToolUse, so their command is the shared input.
    Session/turn IDs keep identical calls in other conversations separate.
    """
    if not isinstance(data.get("tool_name"), str) or "tool_input" not in data:
        return ""
    ti = data["tool_input"]
    if data["tool_name"] in ("Bash", "apply_patch") and isinstance(ti, dict) and "command" in ti:
        ti = ti["command"]
    raw = json.dumps([data.get("session_id"), data.get("turn_id"), data["tool_name"], ti], sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def edited_paths(data) -> list:
    ti = data.get("tool_input") if isinstance(data, dict) else None
    if isinstance(ti, dict):
        for k in ("file_path", "notebook_path"):
            if isinstance(ti.get(k), str) and ti[k]:
                return [ti[k]]
        texts = [v for v in ti.values() if isinstance(v, str)]
        for v in ti.values():
            if isinstance(v, list):
                texts += [x for x in v if isinstance(x, str)]
    elif isinstance(ti, str):
        texts = [ti]
    else:
        texts = []
    out = []
    for t in texts:
        if "*** Begin Patch" in t:
            out += PATCH_NAMES.findall(t)
    return [x[:1024] for x in out[:50]]


def quiet_checkpoint(data):
    """Local cooperative gate; no commands, tool inputs or terminal content are retained.

    A missing hint means this runtime predates the coordinator. Never claim full process
    coverage from this hook: users may change their hooks, and existing tools keep running.
    """
    name = data.get("tool_name", "") if isinstance(data, dict) else ""
    if name in ("TaskOutput", "TaskStop", "wait", "wait_agent"):
        return 0
    if name in ("write_stdin", "functions.write_stdin") and isinstance(data.get("tool_input"), dict) and data["tool_input"].get("chars", "") in ("", "\x03"):
        return 0
    if re.search(r"(?:^|__)quiet_(?:request|status|ack|wait|renew|work|release|cancel)$", name):
        return 0   # control must stay reachable during hold
    if re.search(r"(?:^|__)piper_session$", name):
        arguments = data.get("tool_input")
        control = arguments.get("quiet") if isinstance(arguments, dict) else None
        if isinstance(control, dict) and control.get("action") in (
                "status", "ack", "wait", "renew", "work", "release", "cancel", "request"):
            return 0   # the pre-existing tool's controls receive the same server validation
    events = (sys.argv[2] if len(sys.argv) > 2 else os.environ.get("PIPER_EVENT_FILE", ""))
    if not events:
        return 0
    hint = os.path.join(os.path.dirname(events), "piper", "quiet.json")
    try:
        with open(hint) as f:
            raw = f.read(4097)
        if len(raw) > 4096:
            raise ValueError("oversized quiet state")
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get("blocked"), str):
            raise ValueError("invalid quiet state")
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        state = {"blocked": "Quiet window state cannot be read. Check Piper before starting work."}
    if state.get("blocked"):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                         "permissionDecisionReason": state["blocked"]}}))
    return 0


def main():
    ev = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    if ev == "QuietCheckpoint":
        return quiet_checkpoint(data)
    if ev == "PreToolUse":
        return pre_tool_use(data)
    rec = {"ev": ev, "ts": time.time()}
    if isinstance(data, dict):
        for k in KEEP:
            if k in data:
                rec[k] = data[k] if not isinstance(data[k], str) else data[k][:300]
        if ev == "PostToolUse":
            rec["file_paths"] = edited_paths(data)
        if ev in ("PermissionRequest", "PostToolUse"):
            key = tool_key(data)
            if key:
                rec["tool_key"] = key
    path = sys.argv[2] if len(sys.argv) > 2 else (
        os.environ.get("PIPER_EVENT_FILE") or os.path.join(os.path.expanduser("~"), ".claude", "piper-events.jsonl"))
    try:
        with open(path, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
