#!/usr/bin/env python3
"""Serialize local Piper test entrypoints across worktrees, without leaving stale locks."""
import fcntl
import os
import stat
import sys
import tempfile


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ['--']:
        command.pop(0)
    if not command:
        raise SystemExit('usage: piper-test-lock.py -- command [args...]')
    path = os.path.join(tempfile.gettempdir(), 'piper-tests-%d.lock' % os.getuid())
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
        raise SystemExit('piper-test-lock: invalid lock file')
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('Piper tests: another run is active; waiting to keep the machine usable.', flush=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
    # The shell owns the lock for its whole run. Exit/signals release it automatically.
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
