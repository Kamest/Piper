"""Find extension manifests without importing code. Trust is an explicit, local choice."""
from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

from extensions import API, APIS, ENV as ENV_OVERRIDE

MAX_DIRS = 3000
SCAN_SECONDS = 2.0
SKIP = {"node_modules"}


NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
LABEL_RX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def manifest(path: str) -> dict:
    """Read data only; a wrapper must be a file within the extension directory. API 2 (9.167) adds the
    optional description, env_file, service_env and legacy_labels; an API 1 manifest keeps working."""
    directory = Path(path).expanduser().resolve()
    data = json.loads((directory / "piper-extension.json").read_text())
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    for key in ("name", "label"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError("manifest needs %s" % key)
    if type(data.get("api")) is not int:
        raise ValueError("manifest needs an integer api")
    wrapper = ""
    if data.get("wrapper") is not None or data["api"] < 2:   # API 1 always named one
        if not isinstance(data.get("wrapper"), str) or not data["wrapper"].strip():
            raise ValueError("manifest needs wrapper")
        resolved = (directory / data["wrapper"]).resolve()
        if Path(data["wrapper"]).is_absolute() or not resolved.is_relative_to(directory) or not resolved.is_file():
            raise ValueError("wrapper must be a file inside the extension directory")
        wrapper = str(resolved)
    env_file = data.get("env_file") or ""
    if not isinstance(env_file, str) or Path(env_file).is_absolute():
        raise ValueError("env_file must be a path relative to the extension directory")
    lists = {}
    for key, rx in (("service_env", NAME_RX), ("legacy_labels", LABEL_RX)):
        value = data.get(key) or []
        if not isinstance(value, list) or not all(isinstance(x, str) and rx.match(x) for x in value):
            raise ValueError("manifest %s must be a list of plain names" % key)
        lists[key] = value
    description = data.get("description") or ""
    if not isinstance(description, str):
        raise ValueError("manifest description must be text")
    return {"name": data["name"], "label": data["label"], "path": str(directory), "wrapper": wrapper,
            "api": data["api"], "compatible": data["api"] in APIS, "description": description.strip(),
            "env_file": str((directory / env_file).resolve()) if env_file else "", **lists}


def parse_env_file(path: str) -> dict:
    """KEY=VALUE and `export KEY=VALUE` lines with optional simple quotes; never sourced or expanded
    (except a leading ~ of the value). Comments, blank and other lines are skipped; a missing file is {}."""
    out = {}
    try:
        lines = Path(path).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
            if any(c in value for c in "'\"`$\\") or " " in value:
                continue   # shell syntax this parser does not evaluate: leave it out instead of guessing
        if "\0" in value or "\n" in value:
            continue
        out[m.group(1)] = os.path.expanduser(value) if value.startswith("~/") else value
    return out


def trust_file() -> Path:
    """$PIPER_HOME/trusted-extensions.json. Without the variable, inside piperd the home common.py resolved
    (the legacy ~/.hole-orch too), so the file lies in the directory every sandbox is forbidden."""
    home = os.environ.get("PIPER_HOME") or getattr(sys.modules.get("common"), "PIPER_HOME", None) \
        or Path.home() / ".piper"
    return Path(home) / "trusted-extensions.json"


def trusted() -> dict:
    try:
        data = json.loads(trust_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def declined_file() -> Path:
    return trust_file().with_name("declined-extensions.json")


def declined() -> dict:
    try:
        data = json.loads(declined_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_private(target: Path, data: dict) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + target.stem + "-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def set_declined(path: str, decline: bool) -> None:
    """A "no" (or an untrust) is remembered, so the first-run question is asked once per extension."""
    data = declined()
    if decline:
        data[path] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    elif path not in data:
        return
    else:
        data.pop(path)
    _write_private(declined_file(), data)


def set_trust(path: str, allow: bool) -> None:
    """Persist only an explicit trust/untrust command, atomically and owner-readable only. The record keeps
    the git origin: a later different origin is not loaded until trusted again (the commit is not pinned -
    the extension updates with its checkout)."""
    directory = str(Path(path).expanduser().resolve())
    data = trusted()
    if allow:
        info = manifest(directory)
        if not info["compatible"]:
            raise ValueError("extension API %s is incompatible with Piper API %s" % (info["api"], API))
        data[directory] = {"name": info["name"], "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           "remote": origin(directory)}
    else:
        data.pop(directory, None)
    _write_private(trust_file(), data)
    set_declined(directory, not allow)


def _clean_remote(remote: str) -> str:
    if "://" in remote:
        try:
            url = urlsplit(remote)
            return urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            return ""
    if "@" in remote:
        return remote.rsplit("@", 1)[-1]   # scp-style SSH user, too
    return remote


def _git(path: str, deadline: float) -> tuple[str, str]:
    def run(*args):
        left = deadline - time.monotonic()
        if left <= 0:
            return ""
        try:
            return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=min(left, .3)).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return _clean_remote(run("config", "--local", "--get", "remote.origin.url")), run("rev-parse", "--short", "HEAD")


def origin(path: str) -> str:
    """The extension checkout's git origin, cleaned like discover() shows it ("" outside git)."""
    try:
        return _clean_remote(subprocess.run(["git", "-C", path, "config", "--local", "--get", "remote.origin.url"],
                                            capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                            timeout=1).stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return ""


def registration_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "piper" / "extensions.d"


def registrations(deadline: float, sources: bool = False) -> list:
    """The registered paths (with `sources`: (path, "registered" | "added") - added in the GUI)."""
    config = registration_dir()
    out = []
    try:
        with os.scandir(config) as entries:
            for index, entry in enumerate(entries):
                if time.monotonic() >= deadline or index >= MAX_DIRS:
                    break
                if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    data = json.loads(Path(entry.path).read_text())
                    if isinstance(data, dict) and isinstance(data.get("path"), str) and data["path"].strip():
                        out.append((data["path"], "added" if data.get("added") is True else "registered"))
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    return out if sources else [p for p, _ in out]


def register(path: str) -> dict:
    """Settings > Extensions > Add folder: a valid manifest is registered (`added`) so discover() lists it
    from now on. Registering is no trust; trusting is a separate, explicit step."""
    info = manifest(path)
    for p in registrations(time.monotonic() + SCAN_SECONDS):
        try:
            if str(Path(p).expanduser().resolve()) == info["path"]:
                return info
        except (OSError, RuntimeError):
            continue
    config = registration_dir()
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", info["name"])[:40] or "extension"
    target = config / (safe + ".json")
    if target.exists():
        import hashlib
        target = config / ("%s-%s.json" % (safe, hashlib.sha256(info["path"].encode()).hexdigest()[:8]))
    _write_private(target, {"path": info["path"], "added": True})
    return info


def discover(project_path: str | None = None, checkout: str | None = None) -> list[dict]:
    """Registrations first, then at most two directory levels; never walk linked or hidden dirs."""
    deadline = time.monotonic() + SCAN_SECONDS
    candidates = []
    candidates += registrations(deadline, sources=True)
    # An explicit trust path remains discoverable even outside the scan roots.
    for path in trusted():
        candidates.append((path, "scan"))
    checkout = str(Path(checkout or Path(__file__).resolve().parents[1]).resolve())
    project = Path(project_path or os.environ.get("PROJECT_PATH") or Path(checkout).parent).expanduser()
    roots = dict.fromkeys((project, project.parent, Path(checkout).parent))
    # an extension shipped inside the core checkout (offerable() offers it before anything registers it)
    try:
        with os.scandir(checkout) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False) \
                        and os.path.isfile(os.path.join(entry.path, "piper-extension.json")):
                    candidates.append((str(Path(entry.path).resolve()), "scan"))
    except OSError:
        pass
    visited = set()
    count = 0

    def walk(root, depth):
        nonlocal count
        if depth >= 2 or root.is_symlink() or time.monotonic() >= deadline or count >= MAX_DIRS:
            return
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline or count >= MAX_DIRS:
                        return
                    if entry.name.startswith(".") or entry.name in SKIP or not entry.is_dir(follow_symlinks=False):
                        continue
                    count += 1
                    directory = Path(entry.path)
                    real = str(directory.resolve())
                    if (real, depth) in visited:
                        continue
                    visited.add((real, depth))
                    file = directory / "piper-extension.json"
                    if not file.is_symlink() and file.is_file():
                        candidates.append((real, "scan"))
                    walk(directory, depth + 1)
        except OSError:
            pass

    for root in roots:
        walk(root, 0)
    trust = trusted()
    found = {}
    for path, source in candidates:
        try:
            info = manifest(path)
        except (OSError, ValueError, RuntimeError):
            continue
        if info["path"] in found:
            continue
        info["source"] = "checkout" if source == "scan" and str(Path(info["path"]).parent) == checkout else source
        record = trust.get(info["path"])
        info["trusted"] = isinstance(record, dict) and record.get("name") == info["name"]
        info["remote"], info["commit"] = _git(info["path"], deadline)
        info["trusted_remote"] = str(record.get("remote") or "") if isinstance(record, dict) else ""
        info["origin_changed"] = bool(info["trusted"] and info["trusted_remote"] and info["remote"]
                                      and info["trusted_remote"] != info["remote"])
        found[info["path"]] = info
    return list(found.values())


def trusted_found() -> list[dict]:
    """The trusted extensions with `trusted` = the manifest still matches its trust record, and
    `origin_changed` = its git origin differs from the recorded one (both known). No scan."""
    out = []
    for path, record in trusted().items():
        try:
            info = manifest(path)
        except (OSError, ValueError, RuntimeError):
            continue
        info["trusted"] = isinstance(record, dict) and record.get("name") == info["name"]
        info["trusted_remote"] = str(record.get("remote") or "") if isinstance(record, dict) else ""
        info["remote"] = origin(info["path"]) if info["trusted_remote"] else ""
        info["origin_changed"] = bool(info["trusted_remote"] and info["remote"]
                                      and info["trusted_remote"] != info["remote"])
        out.append(info)
    return out


def checkout_dirs(checkout: str | None = None) -> list[str]:
    """The extension directories shipped inside the core checkout (its own subdirectories with a manifest;
    never a hidden or linked one)."""
    root = Path(checkout or Path(__file__).resolve().parents[1]).resolve()
    out = []
    try:
        with os.scandir(root) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False) \
                        and os.path.isfile(os.path.join(entry.path, "piper-extension.json")):
                    out.append(entry.path)
    except OSError:
        pass
    return out


def checkout_legacy_labels(checkout: str | None = None) -> list[str]:
    """The legacy_labels of the extensions inside the core checkout, trusted or not (9.172): such a label is a
    former name of this checkout's own service, so stopping and replacing that service needs no trust - a
    declined extension (or PIPER_NO_EXTENSIONS=1) must not leave the old job running next to `piperd`."""
    labels = []
    for d in checkout_dirs(checkout):
        try:
            labels += [n for n in manifest(d)["legacy_labels"] if n not in labels]
        except (OSError, ValueError, RuntimeError):
            continue
    return labels


def offerable(checkout: str | None = None) -> list[dict]:
    """Cheap first-run candidates: registrations and the checkout's own subdirectories (an extension
    shipped next to the core, which nothing has registered yet). Never trusted, declined or incompatible."""
    deadline = time.monotonic() + SCAN_SECONDS
    paths = registrations(deadline) + checkout_dirs(checkout)
    trust, no, out = trusted(), declined(), {}
    for path in paths:
        try:
            info = manifest(path)
        except (OSError, ValueError, RuntimeError):
            continue
        if info["path"] in out or info["path"] in trust or info["path"] in no or not info["compatible"]:
            continue
        info["remote"], info["commit"] = _git(info["path"], deadline)
        out[info["path"]] = info
    return list(out.values())


CGROUP = "/proc/self/cgroup"


def running_labels(env=None, cgroup: str | None = None) -> list[str]:
    """The login-service labels this process may run under (9.171): $PIPER_SERVICE_LABEL, macOS launchd's
    XPC_SERVICE_NAME, a systemd --user unit `<label>.service` in /proc/self/cgroup. Only an exact match
    with an extension's legacy_labels ever acts on one; noise like XPC_SERVICE_NAME=0 is harmless."""
    env = os.environ if env is None else env
    out = []
    for key in ("PIPER_SERVICE_LABEL", "XPC_SERVICE_NAME"):
        value = (env.get(key) or "").strip()
        if LABEL_RX.match(value) and value not in out:
            out.append(value)
    try:
        lines = Path(cgroup or CGROUP).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        m = re.match(r"(.+)\.service\Z", line.strip().rsplit("/", 1)[-1])
        if m and LABEL_RX.match(m.group(1)) and m.group(1) not in out:
            out.append(m.group(1))
    return out


def service_installed(label: str) -> bool:
    """A launchd agent plist or a systemd --user unit of that label exists (lib-service.sh's svc_installed)."""
    unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / (label + ".service")
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    return plist.is_file() or unit.is_file()


def service_main_pid(label: str) -> int:
    """The pid the service manager runs `label` with (0: not running / unknown)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)], capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, timeout=3).stdout
            m = re.search(r"^\s*pid = (\d+)", out, re.M)
            return int(m.group(1)) if m else 0
        out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", label + ".service"],
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=3).stdout.strip()
        return int(out) if out.isdigit() else 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0


def migrate_legacy(labels, checkout: str | None = None) -> list[dict]:
    """Extensions shipped inside the core checkout ran without a trust record before
    9.153: running (piperd) or having installed (the CLI) one of such an extension's legacy_labels is that
    user's consent. With nothing trusted, no PIPER_NO_EXTENSIONS (and with $PIPER_EXTENSIONS only one it names), the extension inside
    the checkout (forbidden to every sandbox, unlike a registration), compatible and not declined, whose
    legacy_labels name one of `labels`, is trusted once (the record of `piper extensions trust`). 9.171"""
    labels = set(labels or ())
    if not labels or os.environ.get("PIPER_NO_EXTENSIONS") == "1" or trusted():
        return []
    # 9.172: an older wrapper baked $PIPER_EXTENSIONS into the old service; the service that replaces it
    # (piperd) drops it, so the extension the old service loaded is trusted then - and only that one
    named = None
    if ENV_OVERRIDE in os.environ:
        named = {os.path.realpath(os.path.expanduser(d))
                 for d in os.environ[ENV_OVERRIDE].split(os.pathsep) if d.strip()}
    root = str(Path(checkout or Path(__file__).resolve().parents[1]).resolve())
    out = []
    for e in offerable(root):
        if str(Path(e["path"]).parent) != root or not labels & set(e["legacy_labels"]):
            continue
        if named is not None and os.path.realpath(e["path"]) not in named:
            continue
        try:
            set_trust(e["path"], True)
        except (OSError, ValueError, RuntimeError) as error:
            print("piper: %s" % error, file=sys.stderr)
            continue
        e["label_matched"] = sorted(labels & set(e["legacy_labels"]))[0]
        out.append(e)
    return out


def migrate_installed(checkout: str | None = None) -> list[dict]:
    """The CLI's half of migrate_legacy: the legacy labels installed as a login service (no terminal needed -
    the desktop app and scripts call the CLI too). Prints one line per trusted extension."""
    if trusted():
        return []
    root = checkout or str(Path(__file__).resolve().parents[1])
    labels = {label for e in offerable(root) for label in e["legacy_labels"] if service_installed(label)}
    done = migrate_legacy(labels, root)
    for e in done:
        audit({"event": "extension-migrated", "path": e["path"], "remote": e.get("remote", ""),
               "commit": e.get("commit", ""), "label": e["label_matched"], "who": "cli"})
        print("piper: Piper ran as the service %s - %s is now trusted and loads (turn it off: piper extensions "
              "untrust %s)" % (e["label_matched"], e["label"], shlex.quote(e["path"])), file=sys.stderr)
    return done


def ask_first_run() -> None:
    """With a terminal and nothing trusted yet, ask once per found extension; no terminal = no consent."""
    for e in offerable():
        origin = ", ".join(x for x in (e["remote"], e["commit"]) if x)
        print("piper: found the extension %s at %s%s." % (e["label"], e["path"], " (%s)" % origin if origin else ""),
              file=sys.stderr)
        print("  Its code runs with your rights in Piper, outside any sandbox.", file=sys.stderr)
        print("  Use it? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = sys.stdin.readline().strip().lower()
        except (OSError, KeyboardInterrupt):
            answer = ""
        try:
            if answer in ("y", "yes"):
                set_trust(e["path"], True)
                return
            set_declined(e["path"], True)
        except (OSError, ValueError, RuntimeError) as error:
            print("piper: %s" % error, file=sys.stderr)
            return
        print("  Not used. Later: piper extensions trust %s" % shlex.quote(e["path"]), file=sys.stderr)


def selected(found: list[dict]) -> list[dict]:
    return [e for e in found if e["trusted"] and e["compatible"] and not e.get("origin_changed")]


def service_info(checkout: str | None = None) -> dict:
    """What the login service needs from the trusted extensions (the core CLI's install-service, 9.167):
    their env_file values, the variable names to keep, the older service labels to remove - those of the
    extensions inside the checkout whatever their trust (9.172). PIPER_NO_EXTENSIONS=1: the labels only."""
    env, names, labels = {}, [], []
    chosen = [] if os.environ.get("PIPER_NO_EXTENSIONS") == "1" else selected(trusted_found())
    for e in sorted(chosen, key=lambda e: (e["name"], e["path"])):
        if e["env_file"]:
            for k, v in parse_env_file(e["env_file"]).items():
                env.setdefault(k, v)
        names += [n for n in e["service_env"] if n not in names]
        labels += [n for n in e["legacy_labels"] if n not in labels]
    labels += [n for n in checkout_legacy_labels(checkout) if n not in labels]
    return {"env": env, "service_env": names, "legacy_labels": labels}


def audit(event: dict) -> None:
    """One line in $PIPER_HOME/audit.log from a CLI process (common.audit's format; extfind never imports
    common): a trust the CLI set on its own (migrate_installed, an extension wrapper's migration)."""
    rec = dict(event, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    target = trust_file().with_name("audit.log")
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(fd)
    except OSError as error:
        print("piper: audit.log not written: %s" % error, file=sys.stderr)


def print_service_sh() -> None:
    """service_info() as shell assignments: env_file values only where the variable is unset (every value
    quoted, every name checked), EXT_SERVICE_ENV and EXT_LEGACY_LABELS as space-separated names."""
    info = service_info()
    for k, v in info["env"].items():
        if NAME_RX.match(k):
            print("[ -n \"${%s+x}\" ] || export %s=%s" % (k, k, shlex.quote(v)))
    print("EXT_SERVICE_ENV=%s" % shlex.quote(" ".join(info["service_env"])))
    print("EXT_LEGACY_LABELS=%s" % shlex.quote(" ".join(info["legacy_labels"])))


def delegate(choices: list[dict]) -> str:
    """Backward compatibility only: exactly one trusted extension that names a wrapper and is still API 1
    is entered by the core CLI (its wrapper sets the service's env itself). API 2 extensions are never
    entered: the core CLI reads their manifest (service_info) and piperd loads them from the trust file."""
    old = [e for e in choices if e["wrapper"] and e["api"] < 2]
    return old[0]["wrapper"] if len(old) == 1 and len(choices) == 1 else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--delegate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sh", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs="?", choices=("trust", "untrust", "decline", "service"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.command == "service":
        # `extensions service [--sh]`: JSON, or shell assignments the core CLI evals (every value quoted)
        if args.sh:
            print_service_sh()
        else:
            print(json.dumps(service_info()))
        return 0
    if args.command:
        if not args.path:
            parser.error("%s needs a path" % args.command)
        try:
            if args.command == "decline":   # "Not now" without a running service (desktop app, 9.169)
                set_declined(str(Path(args.path).expanduser().resolve()), True)
            else:
                set_trust(args.path, args.command == "trust")
        except (OSError, ValueError, RuntimeError) as e:
            print("piper: %s" % e, file=sys.stderr)
            return 1
        return 0
    if args.path:
        parser.error("unexpected path")
    if args.delegate:
        # every CLI call passes here: only the trusted entries, no scan and no git
        choices = selected(trusted_found())
        if not choices and migrate_installed():   # 9.171: an old service of an extension = consent, no question
            choices = selected(trusted_found())
        if not choices and sys.stdin.isatty() and sys.stderr.isatty():
            ask_first_run()
            choices = selected(trusted_found())
        wrapper = delegate(choices)
        if args.sh:   # the core CLI: one call for the wrapper (API 1) and what the service needs (9.167)
            print("EXT_WRAPPER=%s" % shlex.quote(wrapper))
            print_service_sh()
        elif wrapper:
            print(wrapper)
        return 0
    migrate_installed()   # the desktop app may list first: an old extension service is migrated, not offered
    found = discover()
    no = declined()
    for e in found:
        e["declined"] = e["path"] in no
    if args.json:
        print(json.dumps(found))
    else:
        for e in found:
            print("%s: %s (%s, %s, API %s%s%s)" % (e["label"], e["path"], e["source"],
                  "trusted" if e["trusted"] else "untrusted", e["api"], "" if e["compatible"] else ", incompatible",
                  ", origin changed from %s - trust it again" % e["trusted_remote"] if e["origin_changed"] else ""))
            if e["compatible"] and (not e["trusted"] or e["origin_changed"]):
                print("  piper extensions trust %s" % shlex.quote(e["path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
