#!/bin/sh
# fake-hole.sh - stands in for `hole start claude <dir> ...` in tests (PIPER_HOLE_BIN seam).
# Prints hole 2.1.0's startup lines on stderr in the colored form hole uses on a
# terminal, optionally the trust prompt (FAKE_HOLE_TRUST=1), records an instance file
# like ~/.hole/instances/<name>.json with cliPID, then behaves like a tiny REPL:
# every input line is echoed back as "got:<line>". SIGTERM/SIGHUP end it the way hole
# ends a sandbox.
set -u
[ "${1:-}" != version ] || { echo "hole ${FAKE_HOLE_VERSION:-2.4.0}"; exit 0; }
[ "${1:-}" = start ] || { echo "fake-hole: only 'start' is supported" >&2; exit 2; }
if [ "${FAKE_HOLE_VERSION:-2.4.0}" != 2.4.0 ]; then
  [ -z "${HOLE_SETTINGS_FILE+x}" ] || { echo 'fake-hole: legacy override leaked' >&2; exit 2; }
  [ -f "$HOME/.hole/settings.json" ] || { echo 'fake-hole: missing legacy settings' >&2; exit 2; }
fi
case "${HOLE_SETTINGS_FILE:-}" in
  '') :;;   # low-level ptyhost tests do not select a settings document
  /*) [ -f "$HOLE_SETTINGS_FILE" ] || { echo 'fake-hole: missing explicit settings' >&2; exit 2; };;
  *) echo 'fake-hole: expected absolute HOLE_SETTINGS_FILE' >&2; exit 2;;
esac
agent=$2; dir=$3; shift 3
printf '%s\n' "$@" > "${FAKE_HOLE_ARGS:-/dev/null}"
info() { printf '\033[36m[INFO]\033[0m %s\n' "$1" >&2; }
base=${agent%%:*}   # claude | codex (hole start <agent>[:<profile>])
bye() { info "Exited $base CLI. Sandbox destroyed."; exit 0; }
trap bye TERM HUP
if [ "${FAKE_HOLE_TRUST:-0}" = 1 ]; then
  printf 'Trust this project? [y/N] ' >&2
  read -r ans
  [ "$ans" = y ] || { echo "not trusted" >&2; exit 1; }
fi
id=abc123
name="hole-sandbox-$(basename "$dir")-00000000-$id"
mkdir -p "$HOME/.hole/instances"
printf '{"instanceName":"%s","instanceID":"%s","projectPath":"%s","agent":"%s","cliPID":%s}\n' \
  "$name" "$id" "$dir" "$agent" "$$" > "$HOME/.hole/instances/$name.json"
info "Launching sandbox for: $dir"
info "Project name: $(basename "$dir")-00000000"
info "Instance ID: $id"
info "Agent image: hole-sandbox/agent-global:fake (shared)"
if [ "${FAKE_HOLE_UPDATE:-0}" = 1 ]; then
  info "Agent updates available: claude 1.0.0 → 1.0.1"
  printf 'Update agents now? [Y/n/s = skip these versions] ' >&2
  read -r ans
  info "Update answer: $ans"
fi
info "Creating sandbox networks..."
info "Starting network gateway..."
info "Starting $base agent..."
info ""
info "Attaching to $base agent..."
printf '\033[2J\033[H'
printf 'fake %s ready\n' "$base"
if [ "$base" = codex ]; then
  printf '>_ OpenAI Codex (v0.0.0-fake)\n? for shortcuts   100%% context left\n'   # Codex's main screen
else
  printf 'Claude Code v0.0.0-fake\n'   # the main screen piperd waits for before typing a task
fi
while IFS= read -r l; do printf 'got:%s\n' "$l"; done
bye
