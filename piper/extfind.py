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
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

from extensions import API

MAX_DIRS = 3000
SCAN_SECONDS = 2.0
SKIP = {"node_modules"}


def manifest(path: str) -> dict:
    """Read data only; a wrapper must be a file within the extension directory."""
    directory = Path(path).expanduser().resolve()
    data = json.loads((directory / "piper-extension.json").read_text())
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    for key in ("name", "label", "wrapper"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError("manifest needs %s" % key)
    if type(data.get("api")) is not int:
        raise ValueError("manifest needs an integer api")
    wrapper = (directory / data["wrapper"]).resolve()
    if Path(data["wrapper"]).is_absolute() or not wrapper.is_relative_to(directory) or not wrapper.is_file():
        raise ValueError("wrapper must be a file inside the extension directory")
    return {"name": data["name"], "label": data["label"], "path": str(directory),
            "wrapper": str(wrapper), "api": data["api"], "compatible": data["api"] == API}


def trust_file() -> Path:
    return Path(os.environ.get("PIPER_HOME") or Path.home() / ".piper") / "trusted-extensions.json"


def trusted() -> dict:
    try:
        data = json.loads(trust_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_trust(path: str, allow: bool) -> None:
    """Persist only an explicit trust/untrust command, atomically and owner-readable only."""
    directory = str(Path(path).expanduser().resolve())
    data = trusted()
    if allow:
        info = manifest(directory)
        if not info["compatible"]:
            raise ValueError("extension API %s is incompatible with Piper API %s" % (info["api"], API))
        data[directory] = {"name": info["name"], "at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    else:
        data.pop(directory, None)
    target = trust_file()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".trusted-extensions-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
    remote = run("config", "--local", "--get", "remote.origin.url")
    if "://" in remote:
        try:
            url = urlsplit(remote)
            remote = urlunsplit((url.scheme, url.netloc.rsplit("@", 1)[-1], url.path, "", ""))
        except ValueError:
            remote = ""
    elif "@" in remote:
        remote = remote.rsplit("@", 1)[-1]   # scp-style SSH user, too
    return remote, run("rev-parse", "--short", "HEAD")


def discover(project_path: str | None = None, checkout: str | None = None) -> list[dict]:
    """Registrations first, then at most two directory levels; never walk linked or hidden dirs."""
    deadline = time.monotonic() + SCAN_SECONDS
    candidates = []
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "piper" / "extensions.d"
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
                        candidates.append((data["path"], "registered"))
                except (OSError, ValueError):
                    pass
    except OSError:
        pass
    # An explicit trust path remains discoverable even outside the scan roots.
    for path in trusted():
        candidates.append((path, "scan"))
    checkout = str(Path(checkout or Path(__file__).resolve().parents[1]).resolve())
    project = Path(project_path or os.environ.get("PROJECT_PATH") or Path(checkout).parent).expanduser()
    roots = dict.fromkeys((project, project.parent, Path(checkout).parent))
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
        info["source"] = source
        record = trust.get(info["path"])
        info["trusted"] = isinstance(record, dict) and record.get("name") == info["name"]
        info["remote"], info["commit"] = _git(info["path"], deadline)
        found[info["path"]] = info
    return list(found.values())


def trusted_found() -> list[dict]:
    """The trusted extensions whose manifest still matches its trust record (what delegation needs)."""
    out = []
    for path, record in trusted().items():
        try:
            info = manifest(path)
        except (OSError, ValueError, RuntimeError):
            continue
        info["trusted"] = isinstance(record, dict) and record.get("name") == info["name"]
        out.append(info)
    return out


def selected(found: list[dict]) -> list[dict]:
    return [e for e in found if e["trusted"] and e["compatible"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--delegate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs="?", choices=("trust", "untrust"))
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    if args.command:
        if not args.path:
            parser.error("%s needs a path" % args.command)
        try:
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
        if len(choices) == 1:
            print(choices[0]["wrapper"])
        elif len(choices) > 1:
            print("piper: multiple trusted extensions; running core alone:", file=sys.stderr)
            for e in choices:
                print("  %s: %s" % (e["label"], e["path"]), file=sys.stderr)
        return 0
    found = discover()
    if args.json:
        print(json.dumps(found))
    else:
        for e in found:
            print("%s: %s (%s, %s, API %s%s)" % (e["label"], e["path"], e["source"],
                  "trusted" if e["trusted"] else "untrusted", e["api"], "" if e["compatible"] else ", incompatible"))
            if e["compatible"] and not e["trusted"]:
                print("  piper extensions trust %s" % shlex.quote(e["path"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
