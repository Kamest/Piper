#!/bin/sh
# piper-sandbox-attack.sh <real-home> <probe-dir> <foreign-pid> <checkout> - runs INSIDE the test sandbox (piper-sandbox.py
# --selftest), which judges the lines: "must" = the attempt itself has to fail; "effect" = it may succeed inside
# (Linux: into a private tmpfs) but must leave no trace outside - the wrapper checks that from the host.
H=$1; P=$2; FP=$3; CO=$4; W=$TEST_SANDBOX_WORK
t() { kind=$1; name=$2; shift 2; if "$@" >/dev/null 2>&1; then echo "$kind $name ESCAPED"; else echo "$kind $name BLOCKED"; fi; }
py() { python3 -c "$1"; }
t effect write-home        sh -c "echo x > '$H/.piper-sbx-new'"
t effect rm-probe-file     rm -f "$P/file"
t effect rm-probe-dir      rm -rf "$P"
t effect mv-probe          mv "$P/file" "$P/file2"
t effect mkdir-home        mkdir "$H/.piper-sbx-dir"
t effect write-tmp         sh -c "echo x > /tmp/piper-sbx-new-$FP"
t effect write-var-tmp     sh -c "echo x > /var/tmp/piper-sbx-new-$FP"
t effect symlink-escape    sh -c "ln -s '$P' '$W/lnk' && rm -rf '$W/lnk/'"
t must   hardlink          ln "$P/file" "$W/hl"
t must   hardlink-outside  ln /etc/hosts "$W/hl2"
t effect git-global        git config --global piper.sbx 1
t must   read-probe        cat "$P/file"
t must   read-ssh          sh -c "ls '$H/.ssh' | head -1 | grep ."
t must   write-checkout    sh -c "echo x > '$CO/.piper-sbx-new'"
t must   live-piperd       py "import socket; socket.create_connection(('127.0.0.1',17895),2)"
t must   live-17890        py "import socket; socket.create_connection(('127.0.0.1',17890),2)"
t must   internet          py "import socket; socket.create_connection(('1.1.1.1',443),3)"
t must   dns               py "import socket; socket.getaddrinfo('example.com',443)"
t must   docker-sock       py "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect('/var/run/docker.sock')"
t must   docker-sock-home  py "import socket; s=socket.socket(socket.AF_UNIX); s.settimeout(2); s.connect('$H/.docker/run/docker.sock')"
t must   docker-cli        docker ps
t must   open              /usr/bin/open -g https://example.invalid
t must   osascript         /usr/bin/osascript -e 'return 1'
t must   kill-foreign      kill -0 "$FP"
t works  loopback          py "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); socket.create_connection(s.getsockname())"
t works  unix-in-work      py "import socket; s=socket.socket(socket.AF_UNIX); s.bind('$W/s.sock'); s.listen(); c=socket.socket(socket.AF_UNIX); c.connect('$W/s.sock')"
t works  write-work        sh -c "echo x > '$W/ok'"
t works  write-tmpdir      sh -c "echo x > \"\$TMPDIR/ok\""
