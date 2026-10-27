#!/usr/bin/env python3
"""`docker` for the stack suite: it may only change its own compose project.

The suite runs only locally in piper-sandbox.py --dind (no CI job drives a shared runner's Docker): a test bug - an
empty job id, a cleanup by pattern, a prune - must still not hit other containers and images (incident class T3). This
stands at /usr/local/bin/docker in the suite's container, the real CLI moved aside, and lets through:
  - reads: ps, inspect, images, logs, version, info, network/volume/image ls|inspect, events;
  - `compose` whose project (from `compose config`) is the scope, with no host bind mount, no image tag outside the
    scope, no `down --rmi all`;
  - rm / stop / kill / start / restart / exec / network rm / volume rm / rmi on objects named inside the scope
    (ids resolved to names first);
  - network create for the scoped default project bridge, with exact ownership labels only (their prefix:
    PIPER_DOCKER_NETWORK_LABEL, default piper.stack - the stack's own network script names it);
  - pull of the images in PIPER_DOCKER_PULL.
Everything else exits 99 with the reason. The scope is PIPER_DOCKER_SCOPE = piper-test-stack-<digits>; a missing or
other value refuses every change - in an image with the STRICT marker. The local dind runner (piper-sandbox.py
--dind) has no marker: there a call without a scope (dbprod's `docker run`) goes to its own throwaway daemon, and the
stack suite always sets its scope. A file, not a variable: no env scrub turns it off.
It stops a test bug, not hostile code (that could speak to the socket itself).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

REAL = os.environ.get("PIPER_DOCKER_REAL", "/usr/local/libexec/piper/docker.real")
STRICT = "/usr/local/libexec/piper/strict"
SCOPE_RE = re.compile(r"^piper-test-stack-[0-9]+$")
READ = {"ps", "inspect", "images", "logs", "version", "info", "events", "top", "port", "stats"}
SUB_READ = {"ls", "inspect", "list"}
CONTAINER_VERBS = {"rm", "stop", "kill", "start", "restart", "exec", "pause", "unpause"}
COMPOSE_READ = {"config", "ps", "logs", "ls", "images", "top", "version", "port", "events"}
COMPOSE_WRITE = {"up", "down", "start", "stop", "restart", "build", "kill", "rm", "create", "exec", "pause", "unpause"}
# compose's global flags that take a value
COMPOSE_VALUE_FLAGS = {"-f", "--file", "-p", "--project-name", "--project-directory", "--profile", "--env-file",
                       "--progress", "--ansi", "--parallel"}
# per command: the flags that take a value, so the value is not read as a name (`rm -f` is --force, `exec -t` a tty)
OPERAND_VALUE_FLAGS = {
    "rm": set(), "pause": set(), "unpause": set(), "rmi": {"--platform"}, "network rm": set(), "volume rm": set(),
    "stop": {"-t", "--time", "-s", "--signal", "--timeout"}, "restart": {"-t", "--time", "-s", "--signal", "--timeout"},
    "kill": {"-s", "--signal"}, "start": {"--detach-keys", "--checkpoint", "--checkpoint-dir"},
    "exec": {"-e", "--env", "--env-file", "-w", "--workdir", "-u", "--user", "--detach-keys"},
    "pull": {"--platform"},
}


class Refused(Exception):
    pass


def in_scope(name: str, scope: str) -> bool:
    name = name.lstrip("/")
    return name == scope or name.startswith((scope + "-", scope + "_"))


def _real(args: list) -> subprocess.CompletedProcess:
    return subprocess.run([REAL] + args, capture_output=True, text=True)


def _names(kind: str, refs: list) -> list:
    """`docker <kind> inspect` names of ids or names (a missing object: its ref, the real CLI then reports it)"""
    out = []
    for ref in refs:
        r = _real([kind, "inspect", "--format", "{{json .}}", ref])
        if r.returncode:
            out.append(ref)
            continue
        o = json.loads(r.stdout.splitlines()[0])
        if kind == "image":
            out += o.get("RepoTags") or ["<untagged %s>" % ref]
        else:
            out.append(o.get("Name") or ref)
    return out


def _operands(args: list, verb: str) -> list:
    """positional args (names / ids) of `verb`, flags and their values skipped"""
    value_flags = OPERAND_VALUE_FLAGS[verb]
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i + 1:]
            break
        if a.startswith("-"):
            i += 2 if a in value_flags else 1
            continue
        out.append(a)
        i += 1
    return out


def _check_names(kind: str, refs: list, scope: str, verb: str) -> None:
    if not refs:
        raise Refused("%s without a target" % verb)
    for n in _names(kind, refs):
        repo = n.rsplit(":", 1)[0] if kind == "image" else n
        if not in_scope(repo, scope):
            raise Refused("%s %s: outside the scope %s" % (verb, n, scope))


def _compose(args: list, scope: str) -> None:
    pre, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        n = 2 if args[i] in COMPOSE_VALUE_FLAGS else 1
        pre += args[i:i + n]
        i += n
    verb, rest = (args[i], args[i + 1:]) if i < len(args) else ("", [])
    if verb in COMPOSE_READ:
        return
    if verb not in COMPOSE_WRITE:
        raise Refused("compose %s is not allowed" % (verb or "(no command)"))
    r = _real(["compose"] + pre + ["config", "--format", "json"])
    if r.returncode:
        raise Refused("compose config failed: %s" % r.stderr.strip()[:300])
    cfg = json.loads(r.stdout)
    if cfg.get("name") != scope and not str(cfg.get("name", "")).startswith(scope + "-"):
        raise Refused("compose project %r is outside the scope %s" % (cfg.get("name"), scope))
    for sname, svc in (cfg.get("services") or {}).items():
        for v in svc.get("volumes") or []:
            if v.get("type") == "bind":
                raise Refused("service %s binds the host path %s" % (sname, v.get("source")))
        if svc.get("build") and svc.get("image") and not in_scope(svc["image"].rsplit(":", 1)[0], scope):
            raise Refused("service %s would tag the image %s" % (sname, svc["image"]))
        if svc.get("privileged") or svc.get("pid") == "host" or svc.get("network_mode") == "host":
            raise Refused("service %s asks for the host (privileged / pid / network)" % sname)
    if verb == "down":
        for j, a in enumerate(rest):
            v = a.split("=", 1)[1] if a.startswith("--rmi=") else rest[j + 1] if a == "--rmi" and j + 1 < len(rest) else ""
            if v and v != "local":
                raise Refused("compose down --rmi %s would remove shared images" % v)


def _network_create(args: list, scope: str, prefix: str = "piper.stack") -> None:
    """Only the stack's default bridge, with its exact ownership labels (<prefix>.network / .project)."""
    labels, names, i = {}, [], 0
    while i < len(args):
        if args[i] == "--label" and i + 1 < len(args):
            key, sep, value = args[i + 1].partition("=")
            if not sep or key in labels:
                raise Refused("network create: missing or duplicate label")
            labels[key] = value
            i += 2
        elif args[i].startswith("-"):
            raise Refused("network create: only project ownership labels are allowed")
        else:
            names.append(args[i])
            i += 1
    project = labels.get(prefix + ".project", "")
    if not re.fullmatch(re.escape(scope) + r"(?:-[a-z0-9][a-z0-9-]*)?", project):
        raise Refused("network create: project outside the scope")
    if names != [project + "_default"] or labels != {
            prefix + ".network": "project", prefix + ".project": project,
            "com.docker.compose.network": "default", "com.docker.compose.project": project}:
        raise Refused("network create: expected only the scoped default project network")


def check(argv: list, env: dict, strict: bool = True) -> None:
    """raise Refused unless `docker <argv>` stays inside the scope"""
    scope = env.get("PIPER_DOCKER_SCOPE", "")
    if not scope and not strict:
        return
    if not argv:
        raise Refused("no command")
    if argv[0].startswith("-"):
        if argv in (["--version"], ["-v"]):
            return
        raise Refused("global docker flags (%s) are not allowed" % argv[0])
    cmd, rest = argv[0], argv[1:]
    if cmd in READ:
        return
    if cmd in ("network", "volume", "image", "container") and rest and rest[0] in SUB_READ:
        return
    if not SCOPE_RE.match(scope):
        raise Refused("PIPER_DOCKER_SCOPE %r is not piper-test-stack-<digits>: no change allowed" % scope)
    if cmd == "network" and rest[:1] == ["create"]:
        return _network_create(rest[1:], scope, env.get("PIPER_DOCKER_NETWORK_LABEL") or "piper.stack")
    if cmd == "compose":
        return _compose(rest, scope)
    if cmd == "container" and rest:
        cmd, rest = rest[0], rest[1:]
    if cmd in CONTAINER_VERBS:
        ops = _operands(rest, cmd)
        return _check_names("container", ops[:1] if cmd == "exec" else ops, scope, cmd)
    if cmd == "rmi" or (cmd == "image" and rest[:1] == ["rm"]):
        return _check_names("image", _operands(rest[1:] if cmd == "image" else rest, "rmi"), scope, "rmi")
    if cmd in ("network", "volume") and rest[:1] in (["rm"], ["remove"]):
        return _check_names(cmd, _operands(rest[1:], "%s rm" % cmd), scope, "%s rm" % cmd)
    if cmd == "pull" or (cmd == "image" and rest[:1] == ["pull"]):
        allowed = env.get("PIPER_DOCKER_PULL", "").split()
        imgs = _operands(rest[1:] if cmd == "image" else rest, "pull")
        if not imgs or any(i not in allowed for i in imgs):
            raise Refused("pull %s: only %s" % (" ".join(imgs), " ".join(allowed) or "nothing"))
        return
    raise Refused("docker %s is not allowed in the stack suite" % " ".join(argv[:2]))


def main() -> int:
    try:
        check(sys.argv[1:], os.environ, strict=os.path.exists(STRICT))
    except Refused as e:
        sys.stderr.write("docker-scope: REFUSED docker %s - %s (tests/piper-e2e/docker-scope.py)\n"
                         % (" ".join(sys.argv[1:])[:300], e))
        return 99
    os.execv(REAL, [REAL] + sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
