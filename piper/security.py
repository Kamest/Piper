"""The security tree: every isolation Piper adds on top of a plain `hole start`,
one switch each (Settings -> Security).

policy.json `security` = {item id: true}. When that key is absent, DEFAULT_ON is the first-use
default; once present, it is the user's complete choice and missing IDs are off. `apply()` runs in
guard.load_policy after the user's keys are merged: an off item forces its `off` values (the plain
hole), one that is on forces its `on` values and otherwise keeps the user's / the default value of
its keys (lists like candidates or protected_branches are configured in their own Settings
sections and apply only while the item is on).

Approvals are items too (`ask.<kind>`): only the kinds switched on show a card, the rest is
approved on its own (audited). The effective list is policy `ask`; sessions.ask() reads it.

Not in the tree (the hole-level baseline): the project's own hole `files.exclude` stays hidden
from piperd's tools, and piperd never commits a file hole over-mounted.
"""
import copy
import os

# the test suite runs on the policy as it was before the tree (PIPER_SECURITY_TREE=0, _env.py);
# the tree's own tests switch it on
ENABLED = os.environ.get("PIPER_SECURITY_TREE", "1") != "0"

# The first-use default = the Minimal profile. An explicit security object in policy.json is complete
# and replaces this set, including an explicit empty `{}` (all guards off).
# 2026-10-16: production safeguards and approvals plus the host protections that block nothing in everyday
# work (a mount of Piper's own directories, Piper's / Docker's gateway port, hooks planted for host git,
# host-run files of other projects, other projects' dev stacks, a project's own agent image) -
DEFAULT_ON = {
    "db_prod_where",
    "ask.http_test", "ask.http_prod", "ask.http_host",
    "ask.db_write_test", "ask.db_read_prod", "ask.db_write_prod",
    "ask.settings_change", "ask.allow_domain",
    "protected_mounts", "piper_port", "docker_proxy", "git_fail_closed", "write_guard", "ask.write_exec",
    "stack_scope", "forbidden_keys",
}
# Minimal before 2026-10-16. A choice stored exactly as this profile (Settings saved Minimal) means the
# profile, not a custom set: it reads as today's Minimal (state()); anything else stays as stored.
# Settings writes `security_rev: REV` next to every saved choice, so the same set saved from now on is
# exactly what the user chose (e.g. Minimal with every new guard switched off again).
REV = 2
LEGACY_MINIMAL = {
    "db_prod_where",
    "ask.http_test", "ask.http_prod", "ask.http_host",
    "ask.db_write_test", "ask.db_read_prod", "ask.db_write_prod",
    "ask.settings_change", "ask.allow_domain",
}

TREE = [
    {"id": "start", "title": "Sandbox start", "help": "Checks Piper runs before and right after a sandbox starts.",
     "items": [
         {"id": "settings_review", "label": "Review .hole/settings.json changes",
          "help": "A changed project or library .hole/settings.json waits for your approval before the start (L1).",
          "risk": "Off: a changed settings file starts as written, like a plain hole.",
          "off": {"settings_review": False}, "on": {"settings_review": True}},
         {"id": "sensitive_scan", "label": "Scan for sensitive files",
          "help": "Configuration, keystores and CI files not hidden by .hole/settings.json block the start (L3); "
                  "the patterns are in Settings -> Scan.",
          "risk": "Off: the sandbox sees every file hole does not exclude.",
          "off": {"candidates": []}},
         {"id": "forbidden_keys", "label": "Forbidden settings keys",
          "help": "A project's .hole/settings.json may not set agents or container.baseImage.",
          "risk": "Off: a project may bring its own agent image.",
          "off": {"forbidden_project_keys": []}},
         {"id": "protected_mounts", "label": "Protect Piper's own directories",
          "help": "~/.piper, ~/.hole and Piper's own checkout (with its extensions) stay out. Host Claude/Codex access follows their own switches.",
          "risk": "Off: a personal hole include of these directories is mounted as configured.",
          "off": {"protected_mounts": False}, "on": {"protected_mounts": True}},
         {"id": "piper_port", "label": "Block Piper's port",
          "help": "No sandbox reaches piperd's port through the host gateway (K17).",
          "risk": "Off: hostGatewayDomains may name any port.",
          "off": {"forbidden_gateway_ports": [], "piper_port_blocked": False}, "on": {"piper_port_blocked": True}},
         {"id": "attestation", "label": "Verify the running sandbox",
          "help": "After the start Piper inspects the container: over-mounts in place, no secret env, no protected mount (L4).",
          "risk": "Off: the session is ready as soon as hole runs.",
          "off": {"attestation": False}, "on": {"attestation": True}},
         {"id": "l5_watch", "label": "Watch for new sensitive files",
          "help": "A sensitive file that appears while the session runs raises attention or stops it (L5, Settings -> Sandbox).",
          "risk": "Off: nothing is watched after the start.",
          "off": {"l5_action": "off"}},
     ]},
    {"id": "sandbox", "title": "Sandbox contents", "help": "What Piper takes out of your personal hole configuration.",
     "items": [
         {"id": "strip_host_config", "label": "Strip host config includes",
          "help": "~/.gemini, ~/.sage, ~/.gitconfig and ~/.config/glab-cli from your hole includes stay out.",
          "risk": "Off: your includes are mounted as in a plain hole.",
          "off": {"holehome": {"strip_includes": []}}},
         {"id": "strip_secrets", "label": "Strip secrets from the environment",
          "help": "GITLAB_TOKEN and $GITLAB_PAT values are removed; *TOKEN* / *SECRET* env fails the verification.",
          "risk": "Off: the sandbox gets the environment your hole settings give it.",
          "off": {"holehome": {"strip_env": [], "strip_env_values": []}, "secret_env": []}},
         {"id": "gitlab_via_piper", "label": "GitLab only through Piper",
          "help": "The GitLab host is removed from your hole settings' allowed domains (a custom agent's allow.txt "
                  "stays as you wrote it); agents use Piper's GitLab tools (K5).",
          "risk": "Off: GitLab is reachable directly when your hole settings allow it.",
          "off": {"holehome": {"strip_allow": []}, "gitlab_via_piper": False}, "on": {"gitlab_via_piper": True}},
         {"id": "docker_proxy", "label": "Block the host Docker port",
          "help": "Gateway port 2375 is removed from your hole settings.",
          "risk": "Off: the host's Docker API stays reachable when your hole settings allow it.",
          "off": {"holehome": {"strip_ports": []}}},
         {"id": "claude_isolated", "label": "Own ~/.claude per session",
          "help": "Each Claude session gets its own configuration directory instead of the host's ~/.claude.",
          "risk": "Off: sessions share the host's ~/.claude (memory, settings, history).",
          "off": {"claude_dir": "host"}, "on": {"claude_dir": "session"}},
         {"id": "codex_isolated", "label": "Own ~/.codex per session",
          "help": "Codex sessions get their own CODEX_HOME instead of the host's ~/.codex.",
          "risk": "Off: Codex sessions use the host's ~/.codex.",
          "off": {"codex_dir": "host"}, "on": {"codex_dir": "session"}},
         {"id": "git_guarded", "label": "Guarded git",
          "help": "Libraries read-only and no host ~/.gitconfig; Claude asks before Git cleanup. Tool unlocks have their own switch.",
          "risk": "Off: git as in a plain hole.",
          "off": {"git_mode": "hole"}, "on": {"git_mode": "guarded"}},
         {"id": "stack_scope", "label": "Dev stack only for the session's project",
          "help": "A project session reaches its own project's dev stack only.",
          "risk": "Off: a session reaches every dev stack.",
          "off": {"stack_scope_project": False}, "on": {"stack_scope_project": True}},
         {"id": "login_isolated", "label": "Keep logins per session",
          "help": "A refreshed login is not copied into the other running sessions.",
          "risk": "Off: a new login reaches every running session at once.",
          "off": {"login_spread": True}, "on": {"login_spread": False}},
         {"id": "automation_opts", "label": "Start options only from the GUI",
          "help": "Sessions started by an agent never get Docker, unrestricted network or IntelliJ.",
          "risk": "Off: they get the defaults of the New session dialog.",
          "off": {"automation_start_opts": True}, "on": {"automation_start_opts": False}},
     ]},
    {"id": "tools", "title": "Agent tools", "help": "Limits of the tools piperd gives the agents.",
     "items": [
         {"id": "unlocks", "label": "Other projects need an unlock",
          "help": "Reading or editing another project, full git and writes to another project's database are "
                  "locked until you unlock them for the session.",
          "risk": "Off: every session has them unlocked.",
          "off": {"unlocks_required": False, "git_run_projects": True}, "on": {"unlocks_required": True}},
         {"id": "gitlab_extra", "label": "Block merge / CI actions",
          "help": "Piper refuses gitlab_mr_merge and gitlab_job_play calls.",
          "risk": "Off: agents may merge and run CI jobs (each still per Approvals).",
          "off": {"gitlab_extra_tools": True}, "on": {"gitlab_extra_tools": False}},
         {"id": "hidden_files", "label": "Hidden files stay hidden",
          "help": "Piper's tools never show .git of another project; git steps touching hidden files are a red card.",
          "risk": "Off: only hole's own excludes are hidden; no red cards.",
          "off": {"hidden_git": False, "danger_cards": False, "holehome": {"force_exclude": []}}, "on": {"hidden_git": True, "danger_cards": True}},
         {"id": "git_fail_closed", "label": "Harden host git",
          "help": "Host git runs without hooks, without local-path remotes, never in a repository whose config names a program.",
          "risk": "Off: host git runs the repository's hooks and config.",
          "off": {"git_fail_closed": False}, "on": {"git_fail_closed": True}},
         {"id": "write_guard", "label": "Guard host-run files",
          "help": "Piper's own checkout is never written; another project's hooks, CI and build files are a card every time.",
          "risk": "Off: agents may edit them like any file.",
          "off": {"write_guard": False}, "on": {"write_guard": True}},
         {"id": "protected_branches", "label": "Protected branches",
          "help": "No push or merge into the branches listed in Settings -> Git.",
          "risk": "Off: the lists are ignored.",
          "off": {"protected_branches": [], "mr_target_protected": []}},
         {"id": "db_masking", "label": "Mask database values",
          "help": "Personal data in production (and test, when set) results is masked for agents.",
          "risk": "Off: agents read unmasked values.",
          "off": {"db_masking": False}, "on": {"db_masking": True}},
         {"id": "db_prod_where", "label": "Production writes need WHERE",
          "help": "UPDATE / DELETE on production without WHERE is refused.",
          "risk": "Off: allowed.",
          "off": {"db_prod_where": False}, "on": {"db_prod_where": True}},
     ]},
]

# approval cards: one switch per kind, grouped; on = a card, off = approved on its own (audited)
ASK_GROUPS = [
    {"id": "git", "title": "Git", "kinds": [
        ["git_commit", "Commit (git_commit)"], ["git_push", "Push (git_push)"],
        ["git_run", "Full git on the host (git_run)"], ["git_run_project", "git_run in another project"]]},
    {"id": "gitlab", "title": "GitLab", "kinds": [
        ["gitlab_comment", "Comment / thread reply"], ["gitlab_note_edit", "Edit an own comment"],
        ["gitlab_issue_write", "Create / edit an issue"], ["gitlab_mr_write", "Create / edit a merge request"],
        ["gitlab_upload_file", "Upload an attachment"], ["gitlab_job_log", "Read a failed job's log"],
        ["gitlab_mr_merge", "Merge a merge request"], ["gitlab_job_play", "Start / retry a CI job"],
        ["gitlab_job_protected", "CI job on a protected branch"], ["gitlab_read", "Read a project outside the scope"]]},
    {"id": "github", "title": "GitHub", "kinds": [
        ["github_comment", "Comment"], ["github_note_edit", "Edit an own comment"],
        ["github_issue_write", "Create / edit an issue"], ["github_pr_write", "Open / edit a pull request"],
        ["github_job_log", "Read a failed job's log"]]},
    {"id": "projects", "title": "Other projects", "kinds": [
        ["edit_file", "Edit a file"], ["write_file", "Write a file"], ["write_exec", "Edit a host-run file"]]},
    {"id": "http", "title": "HTTP requests", "kinds": [
        ["http_test", "Run on test"], ["http_dev", "Run on dev"], ["http_prod", "Run on production"],
        ["http_host", "Run to a host outside the environment"]]},
    {"id": "db", "title": "Databases", "kinds": [
        ["db_write_dev", "Write to dev"], ["db_read_test", "Read test"], ["db_write_test", "Write to test"],
        ["db_read_prod", "Read production"], ["db_write_prod", "Write to production"],
        ["db_unmask", "Unmask values"], ["db_other", "Another project's connection"]]},
    {"id": "agents", "title": "Agents and Piper", "kinds": [
        ["propose_session", "Propose a session"], ["spawn_session", "Start a session"],
        ["send_to_session", "Write to a session"], ["stop_session", "Stop a session"],
        ["settings_change", "Change Piper's settings"], ["allow_domain", "Allow a domain"]]},
]
ASK_KINDS = [k for g in ASK_GROUPS for k, _ in g["kinds"]]
# F26: Settings › Security says per group in words what Claude asks before and what it does on
# its own - a short phrase per kind (kinds that share one read once) and per guard ("Always protected: …");
# the GUI builds the sentences from the current choice, never from fixed text
ASK_TITLE = {"agents": "Agents & Piper", "http": "HTTP"}
ASK_SHORT = {
    "git_commit": "commits", "git_push": "pushing", "git_run": "git outside the sandbox",
    "git_run_project": "git outside the sandbox",
    "gitlab_comment": "comments", "gitlab_note_edit": "comments", "gitlab_issue_write": "issues",
    "gitlab_mr_write": "MR text", "gitlab_upload_file": "attachments", "gitlab_job_log": "CI logs",
    "gitlab_mr_merge": "merging", "gitlab_job_play": "running CI jobs", "gitlab_job_protected": "running CI jobs",
    "gitlab_read": "reading projects outside the session",
    "github_comment": "comments", "github_note_edit": "comments", "github_issue_write": "issues",
    "github_pr_write": "pull requests", "github_job_log": "CI logs",
    "edit_file": "editing their files", "write_file": "editing their files", "write_exec": "their hooks and build files",
    "http_test": "test", "http_dev": "dev", "http_prod": "production", "http_host": "unknown hosts",
    "db_write_dev": "writes to dev", "db_read_test": "reads of test", "db_write_test": "writes to test",
    "db_read_prod": "production", "db_write_prod": "production", "db_unmask": "unmasking",
    "db_other": "other projects' connections",
    "propose_session": "proposing sessions", "spawn_session": "starting sessions",
    "send_to_session": "writing to them", "stop_session": "stopping a session",
    "settings_change": "Piper settings", "allow_domain": "new domains",
}
TREE_SHORT = {
    "settings_review": "sandbox settings", "sensitive_scan": "sensitive files", "forbidden_keys": "the agent image",
    "protected_mounts": "Piper itself", "piper_port": "Piper itself", "attestation": "the running sandbox",
    "l5_watch": "sensitive files", "strip_host_config": "your host config", "strip_secrets": "secrets",
    "gitlab_via_piper": "the GitLab token", "docker_proxy": "Docker", "claude_isolated": "your ~/.claude",
    "codex_isolated": "your ~/.codex", "git_guarded": "libraries", "stack_scope": "other stacks",
    "login_isolated": "logins", "automation_opts": "start options", "unlocks": "other projects",
    "gitlab_extra": "merge and CI", "hidden_files": "hidden files", "git_fail_closed": "your Mac's git",
    "write_guard": "your Mac's files", "protected_branches": "protected branches", "db_masking": "personal data",
    "db_prod_where": "production without WHERE",
}
ITEMS = [it for g in TREE for it in g["items"]]
IDS = [it["id"] for it in ITEMS] + ["ask." + k for k in ASK_KINDS]


def state(pol: dict) -> dict:
    """{item id: bool}; absent settings use DEFAULT_ON, a stored setting is authoritative."""
    st = pol.get("security") if isinstance(pol.get("security"), dict) else {}
    if "security" not in pol:
        on = DEFAULT_ON
    else:
        on = {i for i in IDS if st.get(i) is True}
        if pol.get("security_rev") != REV:   # saved before REV: an old profile follows the profile
            on = {LEGACY_MINIMAL_KEY: DEFAULT_ON, LEGACY_RECOMMENDED_KEY: RECOMMENDED_ON}.get(frozenset(on), on)
    return {i: i in on for i in IDS}


def default_value() -> dict:
    """Compact first-use default suitable for the Settings field's `default`."""
    return {i: True for i in IDS if i in DEFAULT_ON}


def apply(pol: dict) -> dict:
    """Force the effective policy to the tree (in place; returns pol)."""
    st = state(pol)
    hh = dict(pol.get("holehome") or {}) if isinstance(pol.get("holehome"), dict) else {}
    pol["l5_candidates"] = copy.deepcopy(pol.get("candidates") or []) if st["l5_watch"] else []
    for it in ITEMS:
        for k, v in (it.get("on" if st[it["id"]] else "off") or {}).items():
            if k == "holehome":
                hh.update(copy.deepcopy(v))
            else:
                pol[k] = copy.deepcopy(v)
    pol["holehome"] = hh
    if st["l5_watch"] and pol.get("l5_action") not in ("attention", "stop"):
        pol["l5_action"] = "attention"
    pol["ask"] = [k for k in ASK_KINDS if st["ask." + k]]
    pol["security"] = {i: True for i, v in st.items() if v}
    return pol


def schema() -> list[dict]:
    """The tree for the GUI (Settings -> Security)."""
    return [{"id": g["id"], "title": g["title"], "help": g["help"],
             "items": [dict({k: it[k] for k in ("id", "label", "help", "risk")}, short=TREE_SHORT.get(it["id"], ""))
                       for it in g["items"]]} for g in TREE] + \
        [{"id": "approvals", "title": "Approvals",
          "help": "Agent actions that wait for your card. Off = approved on its own (still in the audit log).",
          "groups": [{"id": g["id"], "title": g["title"], "short": ASK_TITLE.get(g["id"], g["title"]),
                      "items": [{"id": "ask." + k, "label": lb, "kind": k, "short": ASK_SHORT.get(k, "")}
                                for k, lb in g["kinds"]]} for g in ASK_GROUPS]}]


# Everyday protection is an explicit selection, not Strict minus a few exceptions.
# Keep host/production boundaries and risky-write cards without blocking starts,
# separating agent configuration, locking tools or asking about routine collaboration.
_RECOMMENDED_EXTRA = {
    "protected_mounts", "piper_port", "strip_secrets", "docker_proxy", "stack_scope",
    "hidden_files", "git_fail_closed", "write_guard", "db_masking",
    "ask.git_push", "ask.git_run", "ask.git_run_project",
    "ask.gitlab_mr_merge", "ask.gitlab_job_play", "ask.gitlab_job_protected",
    "ask.edit_file", "ask.write_file", "ask.write_exec",
    "ask.db_unmask", "ask.db_other", "ask.stop_session",
}
RECOMMENDED_ON = DEFAULT_ON | _RECOMMENDED_EXTRA


# Recommended before 2026-10-16 (it is DEFAULT_ON | ..., so it grew by forbidden_keys): a stored copy
# follows the profile like LEGACY_MINIMAL does
LEGACY_RECOMMENDED_KEY = frozenset(LEGACY_MINIMAL | _RECOMMENDED_EXTRA)
LEGACY_MINIMAL_KEY = frozenset(LEGACY_MINIMAL)


# the one-time screen after the update to REV 2 (features.json `seen`): why Minimal grew, what to choose
NOTICE = "security-2"
_NOTICE_PROFILES = ("hole", "recommended", "guarded")


def notice(pol: dict) -> dict:
    """What the screen offers for the raw policy.json. kind "profile": no saved choice or a saved
    Minimal / Recommended / Strict - that profile is preselected; kind "custom": an own mix (Plain hole
    too) - only informed, nothing is preselected or changed."""
    on = {i for i, v in state(pol).items() if v}
    pr = presets()
    name = next((n for n in _NOTICE_PROFILES if on == {i for i, v in pr[n].items() if v}), None)
    return {"kind": "profile" if name else "custom", "profile": name or "custom", "on": len(on), "total": len(IDS)}


def presets() -> dict:
    """Minimal stays the first-use default; Recommended is always an explicit user choice."""
    return {"hole": default_value(), "plain": {}, "guarded": {i: True for i in IDS},
            "recommended": {i: True for i in IDS if i in RECOMMENDED_ON}}



# Settings (settings.py): the keys an item forces both ways are set only here (their old fields are hidden
# while the tree is on); the lists and options an item merely switches off stay in their sections, marked
GOVERNED = sorted({k for it in ITEMS for k in (it.get("on") or {})} | {"auto_approve"})
_SUB_EXTRA = {"settings_auto_tighten": "settings_review", "allow": "sensitive_scan", "skip_dirs": "sensitive_scan",
              "max_depth": "sensitive_scan", "symlink_visits": "sensitive_scan", "max_entries": "sensitive_scan",
              "rw_includes": "git_guarded", "rw_libraries": "git_guarded",
              "git_hidden_server_content": "hidden_files", "prod_read": "ask.db_read_prod",
              "scan_skip_roots": "sensitive_scan", "allowed_env": "strip_secrets",
              "default_unlocks": "unlocks", "prod_require_where": "db_prod_where", "mask_test": "db_masking",
              "mask_words": "db_masking"}


def governed_by(key: str) -> dict | None:
    """The item a Settings field only applies under ({id, label}), or None."""
    iid = _SUB_EXTRA.get(key)
    if iid is None:
        for it in ITEMS:
            off = dict(it.get("off") or {}, **((it.get("off") or {}).get("holehome") or {}))
            if key in off and key != "holehome" and key not in (it.get("on") or {}):
                iid = it["id"]
                break
    if iid and iid.startswith("ask."):
        return {"id": iid, "label": next(lb for g in ASK_GROUPS for k, lb in g["kinds"] if "ask." + k == iid)}
    it = next((x for x in ITEMS if x["id"] == iid), None)
    return {"id": it["id"], "label": it["label"]} if it else None


def validate(v) -> dict:
    """A stored `security` value: {known id: true}; false / missing = off."""
    if not isinstance(v, dict):
        raise ValueError("an object {item: true}")
    bad = sorted(k for k in v if k not in IDS)
    if bad:
        raise ValueError("unknown item(s) %s" % ", ".join(bad[:5]))
    if any(not isinstance(x, bool) for x in v.values()):
        raise ValueError("items are true / false")
    return {i: True for i in IDS if v.get(i) is True}
