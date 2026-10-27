"""Extensions: optional code outside Piper's core, loaded at start.

$PIPER_EXTENSIONS names directories (os.pathsep-separated); each holds an `extension.py`. Without the
variable Piper runs its core alone - nothing in the core imports an extension by name
(tests/piper-unit/test_boundary.py keeps it so). An extension directory is host code piperd runs:
it is protected like Piper's own checkout (forbidden read-write, never written from a session).

An extension module may define any of these; whatever it leaves out simply contributes nothing:

  API             extension API version (currently 1); absent on older extensions means 1
  NAME            a short id (doctor, audit)
  MIXINS          classes mixed into sessions.Manager before the core bases
  MODULES         [(id, label, help)] feature modules (features.MODULES); PRESETS {preset: [ids]};
                  INTEGRATIONS [ids] (the Settings > Features group of services)
  DEFAULTS        {key: value} a core default replaced: gitlab_host, prod_mcp_server, prod_mcp_tools,
                  issue_files (inbox.ISSUE_FILES), issue_sections {older heading: section} and
                  issue_facts_markers (older names of the facts block, issuestate), policy_candidates (the
                  core's sensitive candidates replaced, guard.default_candidates), prod_tools (prodcheck)
  MAIN_LINES      [branch] more mainline branches: a direct push to one warns (gitops)
  POLICY_CANDIDATES [pattern] more default sensitive candidates (guard.default_candidates)
  ANALYSIS_DIRS   [dir/] more directories of notes, not code (inbox.analysis_dirs)
  PROJECT_SKIP    [name] directories never searched for projects (projects.SKIP)
  MASK_WORDS      [word] more masked column name words (dbtools.mask_words)
  PROD_WORDS      [word] more environment / connection names that mean production
  AUTO_PROMPT_PREFIXES [prefix] older prefixes of piperd's own prompts (timesheet)
  ASKING_DETAILS [prefix]       older session details that meant "the agent asks" (sessions)
  ADVISOR_OLD_TEMPLATES [(first line, None | copy name)] older global agent CLAUDE.md templates replaced
  init(manager) / start(manager)                Manager.__init__ (state) / its background services
  env_fields(settings) -> [settings.Field]      extra fields of Settings > Environment
  doctor_rows(modules) -> [doctor row | None]   extra rows of the doctor card
  stack_provider(manager) -> provider | None    the dev stack of the Stack section (stackview.py)
  forbidden(manager, session) -> [(path, "any"|"rw")]   mounts a sandbox never gets
  protected(manager, session) -> [path]         dirs no generated include may overlap
  protected_what() -> {path: why}               the blocked-start panel's words
  allowed_env(manager, session) -> [name]       extra environment names a sandbox may keep
  reserved_env() -> [prefix]                    names a project's sandbox_env may not set (the extension's own)
  daemon_env() -> [prefix]                      piperd's own environment a direct or SSH child never inherits
  base_rules(manager, session, rules)           the holehome rules of a sandbox (set_env, add_gateway)
  session_md(manager, session) -> str           text added to a session's instructions
  session_up(manager, session) / session_down(manager, session)   a sandbox started / ended
  infra_roots() -> [path]                       checkouts the host runs scripts of: never written
  project_roots() -> [path]                     where "Import registered projects" looks
  git_trusted_config(key, value) -> bool        a host-side git config entry Piper keeps (gitops)
  db_dev(root) -> [connection]                  dev database connections of a checkout (dbtools)
  db_links(root) -> [{label, url}]              extra links of a checkout's Database view
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

API = 1
ENV = "PIPER_EXTENSIONS"
LOADED: list = []          # the extension modules, in $PIPER_EXTENSIONS order
DIRS: list[str] = []       # their directories (realpath)
ERRORS: list[str] = []     # what failed to load (doctor shows it; the core runs on)


def _dirs() -> list[str]:
    return [os.path.realpath(os.path.expanduser(d)) for d in (os.environ.get(ENV) or "").split(os.pathsep) if d.strip()]


def load() -> None:
    """Import every extension once (idempotent). A broken one is reported in ERRORS, never fatal."""
    if LOADED or ERRORS:
        return
    for d in _dirs():
        path = os.path.join(d, "extension.py")
        name = "piper_extension_%d" % len(DIRS)
        try:
            if not os.path.isfile(path):
                raise ImportError("%s has no extension.py" % d)
            manifest = os.path.join(d, "piper-extension.json")
            if os.path.isfile(manifest):
                with open(manifest) as f:
                    version = json.load(f).get("api")
                if type(version) is not int or version != API:
                    raise ImportError("manifest API %s is incompatible with Piper API %s" % (version, API))
            if d not in sys.path:
                sys.path.append(d)   # after Piper's own modules: an extension never shadows the core
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            version = getattr(mod, "API", API)
            if type(version) is not int or version != API:
                raise ImportError("module API %s is incompatible with Piper API %s" % (version, API))
        except Exception as e:  # noqa: BLE001 - the core starts without it
            sys.modules.pop(name, None)
            ERRORS.append("%s: %s" % (d, e))
            print("piper: extension %s not loaded: %s" % (d, e), file=sys.stderr, flush=True)
            continue
        LOADED.append(mod)
        DIRS.append(d)


def names() -> list[str]:
    return [str(getattr(m, "NAME", "") or os.path.basename(d)) for m, d in zip(LOADED, DIRS)]


def each(attr: str) -> list:
    """The attribute `attr` of every extension that defines it."""
    load()
    return [getattr(m, attr) for m in LOADED if hasattr(m, attr)]


def items(attr: str) -> list:
    """The lists `attr` of every extension, concatenated."""
    return [x for v in each(attr) for x in v]


def call(attr: str, *args) -> list:
    """Call `attr(*args)` of every extension that defines it; the results in order."""
    return [f(*args) for f in each(attr)]


def collect(attr: str, *args) -> list:
    """Call `attr(*args)` everywhere and concatenate the returned lists."""
    return [x for r in call(attr, *args) for x in (r or [])]


def merged(attr: str, *args) -> dict:
    """Call `attr(*args)` everywhere and merge the returned dicts (a later extension wins)."""
    out: dict = {}
    for r in call(attr, *args):
        out.update(r or {})
    return out


def default(key: str, fallback):
    """A policy default an extension replaces (DEFAULTS), else `fallback`."""
    for d in each("DEFAULTS"):
        if key in d:
            return d[key]
    return fallback


def mixins() -> tuple:
    return tuple(items("MIXINS"))
