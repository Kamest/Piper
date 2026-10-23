#!/usr/bin/env python3
"""fake-docker.py - stands in for `docker inspect|exec|image ls|ps` (agent images) in Piper tests (PIPER_DOCKER_BIN).

`inspect <name>` prints the JSON stored in $FAKE_DOCKER_DIR/<name>.json (exit 1 when
absent, like docker for an unknown container). `exec [-i] <name> <cmd...>` runs the command
on THIS machine - a container whose mounts mirror the host exactly, i.e. one where no
over-mount took effect (what Docker Desktop 4.90 does to nested directory binds, K18). With $FAKE_DOCKER_TMP its
/tmp is that dir (a container's private /tmp; the test sandbox never lets a test write the host's).
"""
import os
import subprocess
import sys

d = os.environ.get("FAKE_DOCKER_DIR", "/nonexistent")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "inspect":
    path = os.path.join(d, sys.argv[2] + ".json")
    if not os.path.exists(path):
        print("error: no such object: %s" % sys.argv[2], file=sys.stderr)   # docker 29 wording
        sys.exit(1)
    sys.stdout.write(open(path).read())
elif cmd == "logs":   # `logs -f <name>`: $FAKE_DOCKER_DIR/<name>.log, as a container that already stopped
    path = os.path.join(d, sys.argv[-1] + ".log")
    if not os.path.exists(path):
        print("Error response from daemon: No such container: %s" % sys.argv[-1], file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(open(path).read())
elif cmd == "exec":
    args = sys.argv[2:]
    if args and args[0] == "-i":
        args = args[1:]
    t = os.environ.get("FAKE_DOCKER_TMP")
    if t:
        args = [t + a[4:] if a.startswith("/tmp/") else a for a in args]
    sys.exit(subprocess.run(args[1:]).returncode)   # args[0] is the container name
elif cmd == "image" or (cmd == "ps" and "label=com.hole.image=agent" in sys.argv):
    # 9.54 Setup image row: $FAKE_DOCKER_DIR/images.tsv (`image ls` lines) and agent-ps.txt (one image per line)
    path = os.path.join(d, "images.tsv" if cmd == "image" else "agent-ps.txt")
    if os.path.exists(path):
        sys.stdout.write(open(path).read())
elif cmd == "info" and os.path.exists(os.path.join(d, "info.txt")):   # F25: "Docker started" (doctor's check_docker)
    sys.stdout.write(open(os.path.join(d, "info.txt")).read())
else:
    print("fake-docker: unsupported %r" % cmd, file=sys.stderr)
    sys.exit(2)
