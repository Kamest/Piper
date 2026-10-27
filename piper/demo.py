"""Prepare a local, disposable first project and start an ordinary Piper session."""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import common

NAME = "piper-demo"
MARKER = ".piper-demo.json"
DOMAIN = "docs.python.org"
TASK = (
    "Fix the obvious bug in demo_math.add so the existing test passes; do not change the test. "
    "Run python3 -m unittest discover -s tests -v, commit the fix, and push to origin using "
    "Piper's git_push tool (origin is a harmless local bare repository outside the sandbox). "
    "Then use Piper's piper_allow_domain tool to request docs.python.org for optional Python "
    "documentation access on future starts of this demo, with that explanation as the reason. "
    "The push and this last step show approval cards: fresh installs commit on their own but ask "
    "before a push and before changing network permissions. Do not edit .hole/settings.json yourself or "
    "change Piper's policy. No download is needed, and declining the request is fine. "
    "On a repeat run, keep any existing fix and commits; report if the domain is already allowed."
)
FILES = {
    "demo_math/__init__.py": '"""A tiny arithmetic package for the Piper demo."""\n\n\n'
                             'def add(a, b):\n    return a - b\n',
    "tests/test_demo_math.py": 'import unittest\n\nfrom demo_math import add\n\n\n'
                               'class AddTest(unittest.TestCase):\n'
                               '    def test_adds_two_numbers(self):\n'
                               '        self.assertEqual(add(2, 3), 5)\n',
    ".gitignore": "__pycache__/\n*.pyc\n.hole/\n",
    "README.md": "# Piper demo\n\nA deliberately broken Python package with no dependencies.\n\n"
                 "Run `python3 -m unittest discover -s tests -v`. Fix `demo_math.add`, keep the\n"
                 "test, run it again, commit, and push with Piper's `git_push` tool. The remote\n"
                 "is a local bare repository managed by Piper, outside the project roots.\n\n"
                 "Finally request `docs.python.org` with `piper_allow_domain` for optional\n"
                 "Python documentation access on future starts. Fresh installs commit on their\n"
                 "own; the push and the network permission change ask on approval cards.\n"
                 "You may decline it. No download is needed. Custom policies can differ.\n",
}


def git(path: Path, *args: str) -> str:
    """Prepare only the demo repository, without inherited git paths, hooks or signing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                             "-c", "commit.gpgsign=false", "-C", str(path), *args],
                            env=env, capture_output=True, text=True, timeout=30)
    if result.returncode:
        # Do not echo git's stderr: inherited host configuration can contain sensitive values.
        raise ValueError("Could not prepare the demo: git %s failed in %s (exit %d). "
                         "Existing files were kept." % (args[0], path, result.returncode))
    return result.stdout.strip()


def project_roots(root: Path) -> list[Path]:
    """Include explicit checkouts and the static roots of configured discovery globs."""
    data = common.read_json(common.PROJECTS_FILE, {})
    roots = [root]
    for entry in (data.get("projects") or {}).values():
        if isinstance(entry, dict) and entry.get("path"):
            roots.append(Path(entry["path"]).expanduser().resolve())
    for pattern in data.get("globs") or []:
        path = Path(pattern).expanduser()
        prefix = Path(path.anchor)
        for part in path.parts[1:] if path.is_absolute() else path.parts:
            if glob.has_magic(part):
                break
            prefix /= part
        roots.append(prefix.resolve())
    return roots


def prepare() -> Path:
    """Create once; a marked rerun validates the local origin and never resets user work."""
    root = Path(os.environ["PROJECT_PATH"]).expanduser().resolve()
    project = root / NAME
    remotes = (Path(common.PIPER_HOME) / "demo-remotes").resolve()
    if any(remotes == r or r in remotes.parents for r in project_roots(root)):
        raise ValueError("PIPER_HOME/demo-remotes must be outside PROJECT_PATH and configured project roots.")
    if any((p / ".git").exists() for p in (root, *root.parents)):
        raise ValueError("PROJECT_PATH is inside a git project; choose a projects directory outside existing projects.")
    if os.path.lexists(project):
        marker = project / MARKER
        if project.is_symlink() or marker.is_symlink() or not marker.is_file():
            raise ValueError("Refusing to overwrite existing directory %s: it is not a marked Piper demo." % project)
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            origin = Path(data["origin"])
            valid = data["version"] == 1 and origin.parent == remotes and \
                re.fullmatch(r"[0-9a-f]{32}\.git", origin.name) and not origin.is_symlink() and \
                not (project / ".git").is_symlink() and (project / ".git").is_dir()
        except (ValueError, KeyError, TypeError):
            valid = False
        if not valid or git(project, "remote") != "origin" or \
                git(project, "remote", "get-url", "origin") != str(origin) or \
                git(project, "remote", "get-url", "--push", "--all", "origin") != str(origin) or \
                git(origin, "rev-parse", "--is-bare-repository") != "true":
            raise ValueError("Refusing to reuse %s: demo marker or local origin does not match. Existing files were kept." % project)
        git(project, "rev-parse", "--verify", "HEAD")
        if git(project, "rev-parse", "--show-toplevel") != str(project):
            raise ValueError("Refusing to reuse %s: git points to a different working tree." % project)
        return project

    root.mkdir(parents=True, exist_ok=True)
    project.mkdir()  # exclusive: never adopt a directory created by another process
    Path(common.PIPER_HOME).mkdir(mode=0o700, parents=True, exist_ok=True)
    remotes.mkdir(mode=0o700, parents=True, exist_ok=True)
    origin = remotes / (uuid.uuid4().hex + ".git")
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main", "--template=")
    git(project, "init", "--initial-branch=main", "--template=")
    git(project, "config", "user.name", "Piper Demo")
    git(project, "config", "user.email", "demo@example.invalid")
    git(project, "remote", "add", "origin", str(origin))
    for rel, content in FILES.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8") as f:
            f.write(content)
    with (project / MARKER).open("x", encoding="utf-8") as f:
        json.dump({"version": 1, "origin": str(origin)}, f)
        f.write("\n")
    git(project, "add", "--", *FILES, MARKER)
    git(project, "commit", "-m", "Add the deliberately failing Piper demo")
    return project


def request(base: str, token: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=("claude", "codex"), default="claude")
    parser.add_argument("--no-start", action="store_true", help="prepare the project without contacting piperd or opening the GUI")
    args = parser.parse_args(argv)
    try:
        project = prepare()
        print("Demo project: %s" % project)
        print("A fresh install commits on its own; the push and the final network permission request show approval cards.")
        print("First-time agent login and sandbox image creation can take longer than two minutes.")
        command = "piper demo" + (" --agent " + args.agent if args.agent != "claude" else "")
        if args.no_start:
            print("Prepared only. Next: piper open, then %s" % command)
            return 0
        # Reuse the terminal CLI's readiness probe and port, without its auto-start behaviour.
        import ttyattach
        if not ttyattach.piperd_up():
            print("piperd is not running. Next:\n  piper open\n  %s" % command)
            print("Keep PROJECT_PATH=%s, PIPER_HOME=%s and PIPER_PORT=%s for both commands." %
                  (shlex.quote(os.environ["PROJECT_PATH"]), shlex.quote(common.PIPER_HOME),
                   shlex.quote(os.environ.get("PIPER_PORT", "17895"))))
            return 0
        token = common.own_token("gui")
        state = request(ttyattach.BASE, token, "/api/state")
        # The API takes a registered key, not an arbitrary filesystem path. Resolve it on the daemon.
        entry = next((p for p in state["projects"] if os.path.realpath(p["path"]) == str(project)), None)
        if entry is None:
            # An empty GUI may have cached discovery for 60 seconds. Manager.create's find()
            # refreshes on a missing key; resolve that key locally using the daemon's actual root.
            import projects
            config = request(ttyattach.BASE, token, "/api/settings")
            pp = next((f["value"] for sec in config["sections"] if sec["id"] == "env"
                       for f in sec["fields"] if f["key"] == "project_path"), "")
            entry = next((p for p in projects.load(pp).values() if p["path"] == str(project)), None)
        if entry is None:
            raise ValueError("The running piperd does not list %s. Use the same PROJECT_PATH as its service, "
                             "or add this path in Settings > Projects, then rerun piper demo." % project)
        session = request(ttyattach.BASE, token, "/api/sessions",
                          {"project": entry["key"], "agent": args.agent, "task": TASK})
        # Same token fragment and platform opener as `piper open`; keep credentials out of stdout.
        url = "%s/?session=%s#token=%s" % (ttyattach.BASE, urllib.parse.quote(session["id"], safe=""), token)
        print("Session started: %s/?session=%s" % (ttyattach.BASE, urllib.parse.quote(session["id"], safe="")))
        print("Task: " + TASK)
        subprocess.run(["sh", os.path.join(common.SK, "scripts", "open-url.sh"), url], check=False)
        return 0
    except urllib.error.HTTPError as e:
        print("piper demo: piperd returned HTTP %d; open the GUI to check its settings. The demo was kept." % e.code,
              file=sys.stderr)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        # Handle local errors below without printing request objects (they can contain the GUI token).
        message = str(error) if isinstance(error, ValueError) else "Could not prepare or start the demo. Existing files were kept."
        print("piper demo: " + message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
