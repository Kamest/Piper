"""Feature modules: which optional parts of Piper a user sees.

$PIPER_HOME/features.json = {"base": "basic"|"all", "onboarded": bool, "modules": {id: bool}, "seen": [notice]}.
A module that is off is gone, not empty: the GUI hides its icon, tab, panel section, palette
commands, shortcuts and Settings section, and its background work stops (the inbox poll, the
Mattermost thread, the morning check, the Feed). Data and configuration stay; turning it back
on loses nothing.

The core is never a module: sessions, the terminal, the Overview queue, approvals, Changes,
Files, Conversation/History and everything of the isolation (guard, preflight, attestation,
watcher). A module only hides, with one exception: `host` (W2's host shells, no sandbox) is the
one module that gives you more than a sandbox - it is in no preset, its switch carries the
security warning (settings.py) and an agent can never propose it (settings.AGENT_DENY).

One switch per function: a module on = the function runs. The Feed follows
`agent`, the timesheet records under `timesheet`, the morning check runs under `prod` - their old
`enabled` keys are read once by the migration (MIGRATION) and ignored after it.

The file is created on first use: a new install (no sessions yet, no policy.json) starts
with the Basic preset and the setup card; an existing one gets every module on, so nothing an
existing user relies on disappears, and sees the setup card once too. A module missing in the
file (one added later) takes its value from `base`.
"""
from __future__ import annotations

import os
import threading

import common
import extensions

# one-time screens after an update (`seen` in the file): finishing the setup card marks them all, a new
# user never needs the news of an update
NOTICES = ("security-2",)

# id, label, what it hides / stops (shown in Settings and on the setup card)
# the grid's pane cap, the same number as GRID_MAX in web/src/gridlayout.ts (a node test keeps them equal)
GRID_MAX = 9

MODULES: list[tuple[str, str, str]] = [
    ("gitlab", "GitLab inbox", "Issues, MRs and comments assigned to you, the issue workflow of a session, the "
                               "GitLab automation; the inbox poll."),
    ("github", "GitHub", "Issues and pull requests of your GitHub repositories in the same inbox, Actions in the CI "
                         "badge, and sessions reading them - through your own gh login."),
    ("agent", "Global agent & Feed", "The agent that plans work and starts sessions (⌘0), the Feed with its chat "
                                     "and the Recently list."),
    ("grid", "Grid", f"Up to {GRID_MAX} panes side by side - sessions, host shells, Files, databases "
     "(⌘G; ⌘\\ split, ⌘O Files, ⌘D Database)."),
    ("http", "HTTP requests", "The HTTP tab: .http files run on the host with ijhttp."),
    ("db", "Databases", "The Database view, a session's Database section and the list of Claude's database queries."),
    ("diag", "Diagnostics", "Maven / TypeScript / Java errors of a session's checkout, sent to Claude in one click."),
    ("prod", "Production check", "The morning check of production logs and the Production view."),
    ("timesheet", "Timesheet", "Your time by issue and day (the Time view)."),
    ("composer", "Composer", "A multi-line prompt box with templates (⌘E). Off: Reply and → Claude go to the terminal."),
    ("usage", "Usage & limits", "Tokens per day and project, and plan limits under the sessions (Codex when enabled)."),
    ("mattermost", "Mattermost", "Notifications into a Mattermost channel."),
    ("codex", "Codex", "OpenAI Codex as a second agent in the New session dialog."),
    ("host", "Host shells", "Login shells on your Mac in a project's folder (⌥F12), with no sandbox: everything typed "
                            "there runs as you. Only you open one, never a session."),
] + extensions.items("MODULES")   # an extension's modules
IDS = [m[0] for m in MODULES]
# Settings › Features in two groups (D10): what Piper itself does, and what talks to a service or your host
INTEGRATIONS = ["gitlab", "github", "mattermost", "codex", "prod", "host"] + extensions.items("INTEGRATIONS")
CORE = [m for m in IDS if m not in INTEGRATIONS]
# never in a preset, not even "all": off until switched on by itself. host - the setup card's Everything must not
# open an unsandboxed shell; composer (9.44) - the terminal is the default way to type, the Composer an opt-in
NO_PRESET = {"host", "composer"}

PRESETS: dict[str, list[str]] = {
    "basic": ["diag"] + [m for p in extensions.each("PRESETS") for m in p.get("basic", [])],
    "dev": ["diag", "gitlab", "grid", "http", "db"] + [m for p in extensions.each("PRESETS") for m in p.get("dev", [])],
    "all": [m for m in IDS if m not in NO_PRESET],
}
# what you gain, not what is hidden
PRESET_LABELS = [["basic", "Basic", "Just the work: sessions, files, changes. Plus what needs nothing from you."],
                 ["dev", "Developer", "The daily toolbox: your GitLab inbox, a grid of sessions side by side, .http requests "
                                      "and your databases."],
                 ["all", "Everything", f"All {len(IDS) - len(NO_PRESET)} modules, including the Global agent that plans work for you. Switch any "
                                       "off later."]]

FILE = os.path.join(common.PIPER_HOME, "features.json")
# features.json `v`: 2 = the D10 migration ran (one switch per function, the host module)
MIGRATION = 2
_LOCK = threading.Lock()
_CACHE: dict = {}


def _existing_install() -> bool:
    """A Piper home in use before modules existed: a session dir or a policy.json."""
    if os.path.isfile(common.POLICY_FILE):
        return True
    try:
        return any(not n.startswith(".") for n in os.listdir(common.SESSIONS_DIR))
    except OSError:
        return False


def preset(name: str) -> dict:
    on = set(PRESETS[name])
    return {m: m in on for m in IDS}


def _cfg(path: str) -> dict:
    """A feature's own config.json read raw (not through its module: they import this one)."""
    try:
        d = common.read_json(path, {})
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _migrate(mods: dict) -> dict:
    """D10: the second switches become the module's. The timesheet recorded only with
    `enabled` true, the morning check stopped with `enabled` false - such a module goes off so nothing
    starts that did not run. The host shells were policy host_terminal. The Feed is not migrated: it
    follows the Global agent module from now on. Returns the changed ids -> new value."""
    out: dict = {}
    if mods.get("timesheet") and _cfg(os.path.join(common.PIPER_HOME, "timesheet", "config.json")).get("enabled") is not True:
        out["timesheet"] = False
    if mods.get("prod") and _cfg(os.path.join(common.PIPER_HOME, "obs", "config.json")).get("enabled") is False:
        out["prod"] = False
    host = _cfg(common.POLICY_FILE).get("host_terminal") is True
    if mods.get("host") is not host:
        out["host"] = host
    return out


def _normalise(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    base = raw.get("base") if raw.get("base") in PRESETS else "all"
    mods = raw.get("modules") if isinstance(raw.get("modules"), dict) else {}
    out = preset(base)
    out.update({k: v for k, v in mods.items() if k in out and isinstance(v, bool)})
    seen = raw.get("seen") if isinstance(raw.get("seen"), list) else []
    return {"base": base, "onboarded": raw.get("onboarded") is not False, "modules": out,
            "seen": [n for n in NOTICES if n in seen]}


def load() -> dict:
    """{base, onboarded, modules: {id: bool}} - every id present. Creates the file on first use.
    Cached on the file's mtime: the loops ask every tick."""
    with _LOCK:
        try:
            st = os.stat(FILE)
        except FileNotFoundError:
            st = None
        if st is None:
            # an existing home keeps every module but sees the setup card once (the checks, and where to choose less)
            base = "all" if _existing_install() else "basic"
            mods = preset(base)
            mods.update(_migrate(mods))   # an old home's timesheet / morning check / host shells keep their state
            doc = {"base": base, "onboarded": False, "modules": mods, "v": MIGRATION}
            try:
                common.write_json(FILE, doc)
                common.audit({"event": "features-init", "base": base})
            except OSError:
                pass   # a read-only home: defaults in memory, asked again next time
            return _normalise(doc)
        sig = (st.st_mtime_ns, st.st_size)
        if _CACHE.get("sig") == sig:
            return _CACHE["doc"]
        raw = common.read_json(FILE, {})   # hand-broken: {} = everything on, never a 500 of /api/state
        if isinstance(raw, dict) and isinstance(raw.get("modules"), dict) and raw.get("v") != MIGRATION:
            # D10 once: the module takes over the feature's own switch (a module added later stays base-valued)
            mods0 = _normalise(raw)["modules"]   # host is in no preset: off here, on from the policy below
            changed = _migrate(mods0)
            raw = dict(raw, modules=dict(mods0, **changed), v=MIGRATION)
            try:
                common.write_json(FILE, raw)
                common.audit({"event": "features-migrate", "v": MIGRATION, "changed": changed})
                st = os.stat(FILE)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass   # a read-only home: migrated in memory, again next time
        doc = _normalise(raw)
        mods = raw.get("modules") if isinstance(raw, dict) else None
        if isinstance(mods, dict) and any(m not in mods for m in IDS):
            # a module added later, or one Settings reset: written out with its base value, so the
            # Settings page (which reads the file) and the GUI never disagree
            try:
                common.write_json(FILE, dict(raw, modules=doc["modules"]))
                st = os.stat(FILE)
                sig = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        _CACHE.update(sig=sig, doc=doc)
        return doc


def on(module: str) -> bool:
    return module in IDS and bool(load()["modules"].get(module, True))   # an extension's module without it: off


def matching_preset(mods: dict) -> str:
    for name in PRESETS:
        if all(preset(name)[m] == bool(mods.get(m)) for m in IDS if m not in NO_PRESET):
            return name
    return "custom"


def summary() -> dict:
    """/api/state `features`: the GUI's one source of what to show."""
    d = load()
    return {"modules": d["modules"], "onboarded": d["onboarded"], "preset": matching_preset(d["modules"]),
            "list": [list(m) for m in MODULES], "presets": PRESET_LABELS,
            "preset_modules": {k: list(v) for k, v in PRESETS.items()}}   # the chips on a preset card


def save(modules: dict | None = None, onboarded: bool | None = None, preset_name: str | None = None,
         seen: str | None = None) -> dict:
    """POST /api/features: a preset, single modules on top, the setup card's Done (marks every notice
    seen) and/or one notice dismissed. Unknown ids or non-bool values raise ValueError. Audited."""
    if preset_name is not None and preset_name not in PRESETS:
        raise ValueError("unknown preset %r" % preset_name)
    if seen is not None and seen not in NOTICES:
        raise ValueError("unknown notice %r" % seen)
    modules = modules or {}
    if not isinstance(modules, dict) or any(k not in IDS or not isinstance(v, bool) for k, v in modules.items()):
        raise ValueError("modules must be {%s: true/false}" % "|".join(IDS))
    cur = load()
    with _LOCK:
        try:
            raw = common.read_json(FILE, {})
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        mods = preset(preset_name) if preset_name else dict(cur["modules"])
        mods.update({m: cur["modules"][m] for m in NO_PRESET})   # a preset never turns the host shells / Composer on or off
        mods.update(modules)
        raw["base"] = preset_name or cur["base"]   # a preset also decides modules added later
        raw["modules"] = mods
        if onboarded is not None:
            raw["onboarded"] = bool(onboarded)
        marks = set(cur["seen"]) | ({seen} if seen else set()) | (set(NOTICES) if onboarded else set())
        if marks:
            raw["seen"] = [n for n in NOTICES if n in marks]
        common.write_json(FILE, raw)
        _CACHE.clear()
    changed = sorted(m for m in IDS if mods[m] != cur["modules"][m])
    common.audit({"event": "features-change", "on": [m for m in changed if mods[m]],
                  "off": [m for m in changed if not mods[m]], "onboarded": raw.get("onboarded"),
                  **({"seen": seen} if seen else {})})
    return summary()
