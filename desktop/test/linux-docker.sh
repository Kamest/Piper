#!/bin/sh
# Explicit opt-in: builds its own image, never mounts Docker's socket or the user's home. The test sandbox
# (bwrap) needs its own /proc in a new pid namespace: systempaths=unconfined lifts Docker's /proc masks.
# SHOTS=<dir> / OUT=<dir> mount the screenshots and the built packages out of the container.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
docker build --platform linux/amd64 -t piper-desktop-test:24.04 -f "$ROOT/desktop/test/linux.Dockerfile" "$ROOT"
docker run --platform linux/amd64 --rm --security-opt apparmor=unconfined --security-opt seccomp=unconfined --security-opt systempaths=unconfined --cap-add SYS_ADMIN \
  ${SHOTS:+-v "$SHOTS:/opt/piper/desktop/test/shots"} ${OUT:+-v "$OUT:/opt/piper/desktop/out"} piper-desktop-test:24.04
