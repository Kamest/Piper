"""piper/testsandbox.py: a test process outside the OS sandbox of scripts/piper-sandbox.py is refused."""
import ast
import importlib.util
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _env  # noqa: F401

import testsandbox as T  # noqa: E402

UNIT = os.path.dirname(os.path.abspath(__file__))

# /proc/self/mountinfo shapes: id parent major:minor root mountpoint options [optional] - fstype source superopts
BWRAP = """\
1 0 8:1 / / ro,nosuid - ext4 /dev/sda1 rw
2 1 0:20 / /proc rw,nosuid - proc proc rw
3 1 0:21 / /tmp rw,nosuid - tmpfs tmpfs rw
4 1 0:22 / /home/u rw,nosuid - tmpfs tmpfs rw
5 4 8:1 /home/u/projects/piper /home/u/projects/piper ro,nosuid - ext4 /dev/sda1 rw
6 1 0:21 /psbx-1 /scratch/psbx-1 rw,nosuid - tmpfs tmpfs rw
7 1 0:23 / /dev rw - devtmpfs udev rw
8 7 0:24 / /dev/pts rw - devpts devpts rw
9 7 0:25 /urandom /dev/urandom rw,nosuid - tmpfs tmpfs rw
10 7 0:23 /tty /dev/tty rw,nosuid - devtmpfs udev rw
"""
DOCKER = """\
1 0 0:50 / / rw,relatime - overlay overlay rw,lowerdir=/l,upperdir=/u,workdir=/w
2 1 0:51 / /proc rw - proc proc rw
3 1 0:52 / /work rw - tmpfs tmpfs rw
4 1 8:1 /var/lib/docker/containers/abc/hosts /etc/hosts rw - ext4 /dev/sda1 rw
5 1 8:1 /home/u/projects/piper /src ro - ext4 /dev/sda1 rw
6 1 0:53 / /sys ro - sysfs sysfs ro
7 2 0:54 /null /proc/kcore rw,nosuid - tmpfs tmpfs rw
"""
# a hole-like container: the project (and a host dir) bound read-write
HOLE = DOCKER + """\
17 1 8:1 /home/u/projects/piper /home/u/projects/piper rw - ext4 /dev/sda1 rw
18 1 8:1 /home/u/.m2 /home/u/.m2 rw - ext4 /dev/sda1 rw
19 1 0:21 /psbx-1 /tmp/x rw - tmpfs tmpfs rw
"""
# a GitLab docker-executor job: /builds and /cache are Docker volumes, a dind job's container its daemon's socket
GITLAB = DOCKER + """\
20 1 254:1 /var/lib/docker/volumes/runner-x-concurrent-0-cache-c33bcaa1/_data /builds rw - ext4 /dev/vda1 rw
21 1 254:1 /docker/volumes/runner-x-cache-3c3f060a/_data /cache rw - ext4 /dev/vda1 rw
22 1 0:30 /docker.sock /var/run/docker.sock rw,nosuid - tmpfs tmpfs rw
"""
# what a hole sandbox shows on Docker Desktop: the project bound from the host (virtiofs)
HOLE_MAC = DOCKER + """\
23 1 0:45 /Users/u/projects/piper /Users/u/projects/piper rw,nosuid - virtiofs virtiofs2 rw
"""


def _ro_dir():
    d = tempfile.mkdtemp()
    os.chmod(d, stat.S_IRUSR | stat.S_IXUSR)
    return d


class MountTest(unittest.TestCase):
    def test_bwrap_layout_is_clean(self):
        self.assertEqual(T.mount_problems(BWRAP, "/scratch/psbx-1"), [])

    def test_docker_layout_is_clean(self):
        self.assertEqual(T.mount_problems(DOCKER, "/work"), [])

    def test_host_binds_are_found(self):
        bad = T.mount_problems(HOLE, "/work")
        self.assertEqual(len(bad), 3, bad)
        self.assertTrue(any("/home/u/.m2" in b for b in bad), bad)
        self.assertTrue(any("bind of /psbx-1" in b for b in bad), bad)   # a tmpfs subdir bound elsewhere

    def test_a_tmpfs_dir_bound_outside_dev_is_a_problem(self):
        self.assertTrue(T.mount_problems("9 1 0:21 /x /devx rw - tmpfs tmpfs rw", "/work"))

    def test_overlay_elsewhere_than_root_is_a_problem(self):
        self.assertTrue(T.mount_problems("9 1 0:60 / /data rw - overlay overlay rw", "/work"))

    def test_escaped_mount_point(self):
        bad = T.mount_problems("9 1 8:1 /a\\040b /mnt/a\\040b rw - ext4 /dev/sda1 rw", "/work")
        self.assertEqual(bad, ["/mnt/a b (ext4, bind of /a b)"])

    def test_writable_under_work_is_fine(self):
        self.assertEqual(T.mount_problems("9 1 8:1 /x /work/x rw - ext4 /dev/sda1 rw", "/work"), [])
        self.assertTrue(T.mount_problems("9 1 8:1 /x /workx rw - ext4 /dev/sda1 rw", "/work"))


class ProblemsTest(unittest.TestCase):
    def setUp(self):
        self.home = _ro_dir()
        self.checkout = _ro_dir()
        self.work = tempfile.mkdtemp()
        self.env = {"TEST_SANDBOX": "seatbelt", "TEST_SANDBOX_WORK": self.work}

    def tearDown(self):
        for d in (self.home, self.checkout):
            os.chmod(d, 0o700)

    def p(self, **kw):
        a = dict(env=self.env, platform="darwin", home=self.home, checkout=self.checkout, mountinfo="",
                 dockerenv=False)
        a.update(kw)
        return T.problems(**a)

    def test_a_real_sandbox_passes(self):
        self.assertEqual(self.p(), [])

    def test_no_marker(self):
        self.assertIn("not started by", self.p(env={})[0])
        self.assertIn("not started by", self.p(env={"TEST_SANDBOX": "yes"})[0])

    def test_spoofed_marker_with_a_writable_home(self):
        os.chmod(self.home, 0o700)
        self.assertTrue(any("real home" in x for x in self.p()), self.p())

    def test_spoofed_marker_with_a_writable_checkout(self):
        os.chmod(self.checkout, 0o700)
        self.assertTrue(any("checkout" in x for x in self.p()), self.p())

    def test_work_must_not_hold_the_home_or_the_checkout(self):
        for w in ("/", os.path.dirname(self.home), os.path.dirname(self.checkout), ""):
            env = {"TEST_SANDBOX": "seatbelt", "TEST_SANDBOX_WORK": w}
            self.assertTrue(any("TEST_SANDBOX_WORK" in x for x in self.p(env=env)), w)

    def test_a_docker_socket_that_answers(self):
        d = os.path.join(self.work, "h")
        os.makedirs(os.path.join(d, ".docker", "run"))
        s = socket.socket(socket.AF_UNIX)
        s.bind(os.path.join(d, ".docker", "run", "docker.sock"))
        s.listen()
        try:
            os.chmod(d, 0o500)
            self.assertTrue(any("Docker answers" in x for x in self.p(home=d)))
        finally:
            os.chmod(d, 0o700)
            s.close()

    def test_linux_uses_the_mounts(self):
        env = {"TEST_SANDBOX": "bwrap", "TEST_SANDBOX_WORK": "/scratch/psbx-1"}
        self.assertEqual(self.p(env=env, platform="linux", mountinfo=BWRAP), [])
        bad = self.p(env=dict(env, TEST_SANDBOX_WORK="/work"), platform="linux", mountinfo=HOLE)
        self.assertEqual(len([x for x in bad if x.startswith("writable host mount")]), 3, bad)

    def test_ci_only_in_a_gitlab_container(self):
        ci = {"CI": "true", "GITLAB_CI": "true", "CI_JOB_ID": "42"}
        job = dict(env=ci, dockerenv=True, platform="linux", mountinfo=GITLAB)
        self.assertEqual(self.p(**job), [])
        self.assertTrue(self.p(**dict(job, dockerenv=False)))
        self.assertTrue(self.p(**dict(job, platform="darwin")))
        self.assertTrue(self.p(**dict(job, env={"CI": "true", "GITLAB_CI": "true"})))   # no CI_JOB_ID
        self.assertTrue(self.p(**dict(job, env={"TEST_SANDBOX": "ci"})))
        self.assertTrue(self.p(**dict(job, env={"CI": "true"})))

    def test_ci_spoofed_in_a_hole_sandbox_is_refused(self):
        """a hole / Piper sandbox is a container with /.dockerenv too: CI=true there must not skip the sandbox"""
        ci = {"CI": "true", "GITLAB_CI": "true", "CI_JOB_ID": "42"}
        for mi in (HOLE, HOLE_MAC):
            bad = self.p(env=ci, dockerenv=True, platform="linux", mountinfo=mi)
            self.assertTrue(bad and all(x.startswith("CI=true, but a writable host mount") for x in bad), bad)
        self.assertTrue(any("piper" in x for x in
                            self.p(env=ci, dockerenv=True, platform="linux", mountinfo=HOLE_MAC)))
        # a volume-looking path that is a host dir is not a volume
        fake = DOCKER + "24 1 8:1 /home/u/volumes/x/_datax /data rw - ext4 /dev/sda1 rw\n"
        self.assertTrue(self.p(env=ci, dockerenv=True, platform="linux", mountinfo=fake))

    def test_a_wrapper_mode_wins_over_ci_variables(self):
        """the wrapper's own sandbox checks still run when CI=true leaked into it"""
        env = {"TEST_SANDBOX": "bwrap", "TEST_SANDBOX_WORK": "/scratch/psbx-1", "CI": "true", "GITLAB_CI": "true"}
        self.assertEqual(self.p(env=env, platform="linux", mountinfo=BWRAP), [])
        bad = self.p(env=dict(env, TEST_SANDBOX_WORK="/work"), platform="linux", mountinfo=HOLE)
        self.assertEqual(len([x for x in bad if x.startswith("writable host mount")]), 3, bad)


class HarnessEnvTest(unittest.TestCase):
    def test_allow_project_waits_through_every_start_phase(self):
        """The GUI gate must not stop a healthy start in preparing/spawning/trust (pipeline 321532)."""
        with open(os.path.join(UNIT, "..", "piper-e2e", "gui.py")) as f:
            tree = ast.parse(f.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "allow_in_project")
        # Exercise the actual wait predicates for the confirmed start and the subsequent session.
        first = next(n.test for n in ast.walk(fn) if isinstance(n, ast.While) and "state()" in ast.unparse(n.test)
                     and isinstance(n.test, ast.BoolOp) and isinstance(n.test.values[-1], ast.Compare)
                     and isinstance(n.test.values[-1].ops[0], ast.In))
        later = next(n.test for n in ast.walk(fn) if isinstance(n, ast.If) and "ss['state']" in ast.unparse(n.test))
        clock = type("Clock", (), {"time": staticmethod(lambda: 0)})
        for phase in ("preflight", "preparing", "spawning", "trust", "starting", "attesting", "ready", "error", "exited"):
            ns = {"time": clock, "end": 20, "state": lambda: {"state": phase}, "ss": {"state": phase}}
            waiting = phase in ("preflight", "preparing", "spawning", "trust", "starting", "attesting")
            self.assertEqual(eval(compile(ast.Expression(first), "gui.py", "eval"), ns), waiting, phase)
            self.assertEqual(eval(compile(ast.Expression(later), "gui.py", "eval"), ns), not waiting, phase)

    def test_the_e2e_harness_keeps_the_ci_variables(self):
        """run.py's piperd env drops GITLAB_* (tokens); without GITLAB_CI piperd saw a lone CI=true and refused to
        start in every CI e2e job (pipeline 320044)"""
        with open(os.path.join(UNIT, "..", "piper-e2e", "run.py")) as f:
            src = f.read()
        start = src.index("\nENV = {") + 1
        expr = src[start:src.index("}\n", start) + 1]
        env = {"CI": "true", "GITLAB_CI": "true", "CI_JOB_ID": "7", "GITLAB_TOKEN": "x", "PIPER_HOME": "/h",
               "TEST_SANDBOX": "ci", "TEST_SANDBOX_WORK": "/w"}
        ns = {"os": type("O", (), {"environ": env})}
        exec(expr, ns)
        self.assertEqual(set(ns["ENV"]), {"CI", "GITLAB_CI", "CI_JOB_ID", "TEST_SANDBOX", "TEST_SANDBOX_WORK"})


def _wrapper():
    spec = importlib.util.spec_from_file_location("piper_sandbox", os.path.join(UNIT, "..", "..", "scripts",
                                                                                "piper-sandbox.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class WrapperCleanupTest(unittest.TestCase):
    """scripts/piper-sandbox.py removes WORK on the host, unsandboxed: it must touch nothing outside it"""

    def setUp(self):
        self.m = _wrapper()
        self.base = os.path.realpath(tempfile.mkdtemp())
        self.m.BASE = self.base
        self.w = os.path.join(self.base, "psbx-x")
        os.mkdir(self.w)
        self.out = os.path.realpath(tempfile.mkdtemp())
        self.target = os.path.join(self.out, "id_ed25519")
        with open(self.target, "w") as f:
            f.write("key\n")
        os.chmod(self.target, 0o600)

    def tearDown(self):
        for d in (self.base, self.out):
            for root, dirs, _ in os.walk(d):
                for x in dirs:
                    if not os.path.islink(os.path.join(root, x)):
                        os.chmod(os.path.join(root, x), 0o700)
            os.chmod(d, 0o700)
            shutil.rmtree(d)

    def test_a_symlink_out_of_a_read_only_dir_is_not_followed(self):
        ro = os.path.join(self.w, "ro")
        os.mkdir(ro)
        os.symlink(self.target, os.path.join(ro, "out"))
        os.chmod(ro, 0o555)
        self.m.remove_work(self.w, False)
        self.assertEqual(stat.S_IMODE(os.stat(self.target).st_mode), 0o600)
        self.assertFalse(os.path.lexists(self.w))

    def test_the_parent_of_work_is_never_chmodded(self):
        os.chmod(self.base, 0o750)
        os.chmod(self.w, 0)
        self.m.remove_work(self.w, False)
        self.assertEqual(stat.S_IMODE(os.stat(self.base).st_mode), 0o750)
        self.assertFalse(os.path.lexists(self.w))


class WrapperDockerTest(unittest.TestCase):
    """the wrapper's own docker calls (privileged dind, test containers) only on a local daemon"""

    def setUp(self):
        self.m = _wrapper()
        self.bin = tempfile.mkdtemp()
        with open(os.path.join(self.bin, "docker"), "w") as f:
            f.write('#!/bin/sh\n[ "$1" = context ] && { echo "$FAKE_EP"; exit 0; }\n[ "$1" = info ] && exit 0\nexit 9\n')
        os.chmod(os.path.join(self.bin, "docker"), 0o755)

    def tearDown(self):
        shutil.rmtree(self.bin)

    def ok(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        with mock.patch.dict(os.environ, dict(base, PATH=self.bin + ":" + os.environ["PATH"], **env), clear=True):
            return self.m.docker_ok()

    def test_local_daemons_pass(self):
        self.assertTrue(self.ok(FAKE_EP="unix:///var/run/docker.sock"))
        self.assertTrue(self.ok(FAKE_EP="unix:///Users/u/.docker/run/docker.sock"))
        self.assertTrue(self.ok(DOCKER_HOST="unix:///run/user/1000/docker.sock", FAKE_EP="ssh://x"))

    def test_a_remote_daemon_is_refused(self):
        for env in ({"DOCKER_HOST": "tcp://build.example:2375"}, {"DOCKER_HOST": "ssh://u@h"},
                    {"FAKE_EP": "ssh://u@build"}, {"FAKE_EP": "tcp://10.0.0.5:2376"}, {"FAKE_EP": ""}):
            with self.assertRaises(SystemExit, msg=env):
                self.ok(**env)


class WrapperX11Test(unittest.TestCase):
    """an Electron e2e under xvfb-run: bwrap's tmpfs /tmp hid the display ("Missing X server or $DISPLAY")"""

    def setUp(self):
        self.m = _wrapper()
        self.d = tempfile.mkdtemp()
        self.auth = os.path.join(self.d, "Xauthority")
        open(self.auth, "w").close()

    def tearDown(self):
        shutil.rmtree(self.d)

    def x11(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "XAUTHORITY", "PIPER_SANDBOX_X11")}
        with mock.patch.dict(os.environ, dict(base, **env), clear=True), \
                mock.patch("os.path.isdir", lambda p: p == "/tmp/.X11-unix"):
            return self.m.x11_display()

    def test_no_display_unless_opted_in(self):
        self.assertEqual(self.x11(DISPLAY=":99", XAUTHORITY=self.auth), ([], {}))
        self.assertEqual(self.x11(PIPER_SANDBOX_X11="1"), ([], {}))

    def test_opt_in_binds_socket_dir_and_cookie_read_only(self):
        binds, env = self.x11(PIPER_SANDBOX_X11="1", DISPLAY=":99", XAUTHORITY=self.auth)
        self.assertEqual(binds, ["--ro-bind", "/tmp/.X11-unix", "/tmp/.X11-unix", "--ro-bind", self.auth, self.auth])
        self.assertEqual(env, {"DISPLAY": ":99", "XAUTHORITY": self.auth})

    def test_a_missing_cookie_is_neither_bound_nor_passed(self):
        binds, env = self.x11(PIPER_SANDBOX_X11="1", DISPLAY=":99", XAUTHORITY=self.d + "/nope")
        self.assertNotIn("--bind", binds)
        self.assertEqual(env, {"DISPLAY": ":99"})


class RequireTest(unittest.TestCase):
    def test_outside_the_sandbox_exits_3(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TEST_SANDBOX") and k not in ("CI", "GITLAB_CI")}
        r = subprocess.run([sys.executable, os.path.join(UNIT, "..", "..", "piper", "testsandbox.py")],
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("NOT in the test sandbox", r.stderr)

    def test_inside_passes(self):
        r = subprocess.run([sys.executable, os.path.join(UNIT, "..", "..", "piper", "testsandbox.py")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
