"""tests/piper-e2e/docker-scope.py: the stack suite's `docker` changes only its own compose project.

Every refused case here is a way a test bug would reach another
project's containers or images on the daemon the suite drives."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

import _env  # noqa: F401

UNIT = os.path.dirname(os.path.abspath(__file__))
SHIM = os.path.join(UNIT, "..", "piper-e2e", "docker-scope.py")
SCOPE = "piper-test-stack-4242"

# the fake real CLI: `inspect` answers from OBJECTS by ref, `compose ... config` prints CONFIG, anything else
# is logged to CALLS (the shim exec'd it)
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
fx = json.load(open(os.environ["FAKE_FX"]))
if len(a) >= 2 and a[1] == "inspect":
    o = fx["objects"].get(a[-1])
    if o is None:
        sys.exit(1)
    print(json.dumps(o)); sys.exit(0)
if a[:1] == ["compose"] and "config" in a:
    print(json.dumps(fx["config"])); sys.exit(0)
open(os.environ["FAKE_CALLS"], "a").write(json.dumps(a) + "\n")
'''


def _load():
    spec = importlib.util.spec_from_file_location("docker_scope", SHIM)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(name=SCOPE, **svc):
    s = {"image": "busybox:latest"}
    s.update(svc)
    return {"name": name, "services": {"ticker": s}}


class DockerScopeTest(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.real = os.path.join(self.t, "docker.real")
        with open(self.real, "w") as f:
            f.write(FAKE)
        os.chmod(self.real, 0o755)
        self.calls = os.path.join(self.t, "calls")
        self.fx = os.path.join(self.t, "fx.json")
        self.set_fx(_cfg())
        os.environ["PIPER_DOCKER_REAL"] = self.real
        self.m = _load()

    def tearDown(self):
        os.environ.pop("PIPER_DOCKER_REAL", None)

    def set_fx(self, config, objects=None):
        objs = {"abc123": {"Name": "/%s-ticker-1" % SCOPE}, "other1": {"Name": "/gitlab-runner-helper"},
                "%s-ticker-1" % SCOPE: {"Name": "/%s-ticker-1" % SCOPE},
                "%s-app:dev" % SCOPE: {"RepoTags": ["%s-app:dev" % SCOPE]},
                "busybox:latest": {"RepoTags": ["busybox:latest"]},
                "sha256:shared": {"RepoTags": ["%s-app:dev" % SCOPE, "busybox:latest"]},
                "%s_default" % SCOPE: {"Name": "%s_default" % SCOPE}, "bridge": {"Name": "bridge"}}
        objs.update(objects or {})
        with open(self.fx, "w") as f:
            json.dump({"config": config, "objects": objs}, f)
        os.environ["FAKE_FX"] = self.fx
        os.environ["FAKE_CALLS"] = self.calls

    def ok(self, *argv, scope=SCOPE, strict=True):
        self.m.check(list(argv), {"PIPER_DOCKER_SCOPE": scope} if scope is not None else {}, strict=strict)

    def no(self, *argv, scope=SCOPE, strict=True):
        with self.assertRaises(self.m.Refused, msg=" ".join(argv)):
            self.ok(*argv, scope=scope, strict=strict)

    def test_reads_are_always_fine(self):
        for argv in (["ps", "-a", "--filter", "label=x"], ["inspect", "other1"], ["images"], ["version"],
                     ["logs", "other1"], ["network", "ls"], ["network", "inspect", "bridge"], ["volume", "ls"],
                     ["--version"], ["image", "inspect", "busybox:latest"]):
            self.ok(*argv, scope="")

    def test_compose_in_scope(self):
        for verb in ("up -d", "down --remove-orphans", "down --remove-orphans --rmi local", "restart slow",
                     "build app", "stop", "start ticker", "exec ticker sh"):
            self.ok("compose", "-f", "docker-compose.yml", *verb.split())
        self.set_fx(_cfg(SCOPE + "-other"))
        self.ok("compose", "up", "-d")
        self.ok("compose", "config")   # read: no project check

    def test_compose_outside_the_scope(self):
        for name in ("piper-test-stack-4243", "piper-test-stack-42", "other", "piper-test-stack-4242x"):
            self.set_fx(_cfg(name))
            self.no("compose", "-f", "docker-compose.yml", "down")
        self.set_fx(_cfg())
        self.no("compose", "down", "--rmi", "all")
        self.no("compose", "down", "--rmi=all")
        self.no("compose", "push")
        self.no("compose")

    def test_compose_reaching_the_host(self):
        for svc in ({"volumes": [{"type": "bind", "source": "/", "target": "/host"}]},
                    {"build": {"context": "."}, "image": "busybox:latest"}, {"privileged": True},
                    {"pid": "host"}, {"network_mode": "host"}):
            self.set_fx(_cfg(**svc))
            self.no("compose", "up", "-d")
        self.set_fx(_cfg(volumes=[{"type": "volume", "source": "data", "target": "/d"}],
                         build={"context": "."}, image=SCOPE + "-app:dev"))
        self.ok("compose", "up", "-d")

    def test_containers_by_name_or_id(self):
        self.ok("rm", "-f", "abc123")
        self.ok("rm", "-f", "%s-ticker-1" % SCOPE)
        self.ok("exec", "-i", "-e", "X=1", "abc123", "sh", "-c", "rm -rf /")   # inside its own container
        self.ok("container", "stop", "-t", "1", "abc123")
        self.no("rm", "-f", "other1")
        self.no("rm", "-f", "abc123", "other1")
        self.no("kill", "gitlab-runner-helper")
        self.no("rm", "-f")                 # no target
        self.no("rm", "-f", "piper-test-stack-4242x-ticker-1")
        # -t is a tty for exec, not a value: the container is other1, not the in-scope name after it
        self.no("exec", "-t", "other1", "%s-ticker-1" % SCOPE)
        self.no("exec", "-it", "other1", "sh")
        self.ok("stop", "-t", "1", "abc123")
        self.no("stop", "-t", "1", "other1")

    def test_images_networks_volumes(self):
        self.ok("rmi", "-f", "%s-app:dev" % SCOPE)
        self.no("rmi", "busybox:latest")
        self.no("image", "rm", "busybox:latest")
        self.no("rmi", "sha256:shared")     # one tag in scope, one shared
        self.ok("network", "rm", "%s_default" % SCOPE)
        self.no("network", "rm", "bridge")
        self.no("volume", "rm", "runner-cache")

    def test_everything_else_is_refused(self):
        for argv in (["system", "prune", "-af"], ["image", "prune", "-a"], ["container", "prune"],
                     ["volume", "prune"], ["network", "prune"], ["run", "--name", SCOPE + "-x", "busybox"],
                     ["tag", "busybox:latest", "x"], ["push", "x"], ["login"], ["context", "use", "x"],
                     ["-H", "tcp://elsewhere", "ps"], ["--context", "x", "rm", "abc123"], ["pull", "alpine"], []):
            self.no(*argv)

    def test_pull_only_the_listed_images(self):
        env = {"PIPER_DOCKER_SCOPE": SCOPE, "PIPER_DOCKER_PULL": "busybox:latest redis:7-alpine"}
        self.m.check(["pull", "-q", "busybox:latest"], env)
        with self.assertRaises(self.m.Refused):
            self.m.check(["pull", "busybox:latest", "alpine"], env)

    def test_a_bad_scope_refuses_every_change(self):
        for scope in ("", None, "piper-test-stack-", "piper-test-stack-*", "piper", "piper-test-stack-1 x"):
            self.no("compose", "down", scope=scope)
            self.no("rm", "-f", "abc123", scope=scope)

    def test_not_strict_without_a_scope_passes(self):
        """the local dind runner (no marker): dbprod's docker run goes to its own throwaway daemon"""
        self.ok("run", "-d", "--name", "piper-test-mysql", "mysql:8.0", scope=None, strict=False)
        self.no("system", "prune", "-af", scope=SCOPE, strict=False)   # a scope is held either way

    def test_the_cli_refuses_with_99_and_execs_the_real_one(self):
        env = dict(os.environ, PIPER_DOCKER_SCOPE=SCOPE, PIPER_DOCKER_REAL=self.real)
        r = subprocess.run([sys.executable, SHIM, "system", "prune", "-af"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 99)
        self.assertIn("REFUSED docker system prune", r.stderr)
        self.assertFalse(os.path.exists(self.calls))
        r = subprocess.run([sys.executable, SHIM, "rm", "-f", "abc123"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.calls) as f:
            self.assertEqual([json.loads(x) for x in f], [["rm", "-f", "abc123"]])


class WiringTest(unittest.TestCase):
    def test_the_local_dind_puts_the_shim_in_front(self):
        sk = os.path.join(UNIT, "..", "..")
        with open(os.path.join(sk, "scripts", "piper-sandbox.py")) as f:
            sbx = f.read()
        self.assertIn("COPY --chmod=755 docker-scope.py /usr/local/bin/docker", sbx)
        self.assertNotIn("strict", sbx.split("def run_dind")[1].split("def ")[0].replace("no strict marker", ""))


if __name__ == "__main__":
    unittest.main()
