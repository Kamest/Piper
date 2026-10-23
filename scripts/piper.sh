#!/bin/sh
# piper.sh - run Piper, the AI agent orchestrator. Opt-in:
# nothing starts it until you do.
#
#   piper.sh serve            piperd in the foreground
#   piper.sh ensure           start piperd in the background if it is not running (pidfile)
#   piper.sh stop             stop piperd - sessions keep running in ptyhost
#   piper.sh stop-all         stop piperd AND ptyhost - this ends every session's sandbox
#   piper.sh restart          stop + ensure (after a pulled piperd update; sessions survive)
#   piper.sh open [--print]   ensure, then open the GUI (token in the URL fragment); the
#                            default with no command. ensure/open also link ~/.local/bin/piper
#                            here (when that dir is on PATH), so after the first `piper.sh open`
#                            a bare `piper` works from anywhere
#   piper.sh remote-url [--revoke]  the link for another tailnet device (`tailscale serve`): https://<first PIPER_ALLOWED_HOSTS>/ with its own `remote` token;
#                            --revoke drops that token (a lost device) - the next remote-url mints a new one
#   piper.sh doctor [--deep] [--json]  is this machine ready: ok / warn / FAIL, every line names its
#                            fix (the GUI's setup card shows the same); --deep adds the K18 mount test
#   piper.sh template         seed $PIPER_HOME/claude-template from ~/.claude (config only)
#   piper.sh gitlab-token     read a GitLab PAT from stdin into $PIPER_HOME/gitlab-token (0600);
#                            every other command copies GITLAB_TOKEN/GITLAB_PAT from env there
#   piper.sh mattermost-webhook  read a Mattermost incoming-webhook URL from stdin into
#                            $PIPER_HOME/mattermost-webhook (0600): notifications (9.12 E)
#   piper.sh attach [<id|project|.>] [--new] [--replay] [--take] [-l]
#                            a session in THIS terminal (Ghostty, ...): no argument = pick one;
#                            a project without a live session gets a new one; Ctrl-] detaches, the
#                            session keeps running. Needs ptyhost v8 (after `stop-all` once)
#   piper.sh status
#   piper.sh install-service  run piperd as a login service (macOS launchd LaunchAgent, Linux
#                            systemd --user): starts at login, restarted when it dies
#   piper.sh uninstall-service
#
# Env: PIPER_HOME (~/.piper) PIPER_PORT (17895) PIPER_ALLOWED_HOSTS PIPER_ALLOWED_TS_USERS (9.56;
# install-service bakes them in, re-run it after a change). PROJECT_PATH comes from the environment,
# else $PIPER_ENV_FILE (a shell file sourced first, when set), else the checkout's parent directory
# (the convention: $PROJECT_PATH/piper). Status lines go to stderr, so `open --print`
# prints the URL alone. With the service installed, ensure/stop/restart drive the service.
# A wrapper (an extension's own `piper` command) may set: PIPER_SERVICE_LABEL (piperd) names the
# service (tests use a throwaway one), PIPER_LEGACY_LABEL an older service install-service removes,
# PIPER_EXTENSIONS (piper/extensions.py) and PIPER_SERVICE_ENV (more variable names the service
# keeps), PIPER_CLI the command ~/.local/bin/piper links to (this script by default).
set -eu

# resolve a symlink (~/.local/bin/piper) to the checkout; POSIX sh has no readlink -f
self=$0
while [ -L "$self" ]; do
  t=$(readlink "$self")
  case $t in /*) self=$t ;; *) self=$(dirname -- "$self")/$t ;; esac
done
SK=$(CDPATH= cd -- "$(dirname -- "$self")/.." && pwd)
PIPER="$SK/piper"
# Piper was "the hole orchestrator" before the rename (ORCH_* names, ~/.hole-orch): the old
# names keep working, piper/common.py maps them the same way
for _v in $(env | sed -n 's/^ORCH_\([A-Za-z0-9_]*\)=.*/\1/p'); do
  eval "[ -n \"\${PIPER_$_v+x}\" ] || export PIPER_$_v=\"\$ORCH_$_v\""
done
HOME_SET=${PIPER_HOME:+1}
if [ -z "$HOME_SET" ] && [ ! -d "$HOME/.piper" ] && [ -d "$HOME/.hole-orch" ]; then
  HOME_DIR="$HOME/.hole-orch"   # until piperd moves it to ~/.piper (common.migrate_home)
else
  HOME_DIR="${PIPER_HOME:-$HOME/.piper}"
fi
PORT="${PIPER_PORT:-17895}"
BASE="http://127.0.0.1:$PORT"
PIDF="$HOME_DIR/piperd.pid"
LABEL="${PIPER_SERVICE_LABEL:-piperd}"
LEGACY_LABEL="${PIPER_LEGACY_LABEL:-}"


if [ -z "${PROJECT_PATH:-}" ] && [ -n "${PIPER_ENV_FILE:-}" ] && [ -f "$PIPER_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$PIPER_ENV_FILE"
fi
PROJECT_PATH="${PROJECT_PATH:-$(dirname -- "$SK")}"
export PROJECT_PATH

# $PIPER_HOME is created only by the commands that USE Piper: other tools may take its mere
# existence as "Piper is in use" (a dev stack dashboard's Sessions link)
home() { mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"; sync_token; }

# A login service does not inherit the shell env, so GITLAB_TOKEN / GITLAB_PAT (same order as
# piper/gitlab.py) is copied into $PIPER_HOME/gitlab-token; piperd reads it per request.
# Not baked into the service file: that one is world-readable.
sync_token() {
  _t="${GITLAB_TOKEN:-${GITLAB_PAT:-}}"
  [ -n "$_t" ] || return 0
  [ "$(cat "$HOME_DIR/gitlab-token" 2>/dev/null || true)" = "$_t" ] && return 0
  ( umask 077; printf '%s' "$_t" > "$HOME_DIR/gitlab-token" )
  chmod 600 "$HOME_DIR/gitlab-token"
  echo "GitLab token synced from env into $HOME_DIR/gitlab-token (mode 600)" >&2
}

pid_of() {   # pid_of <pidfile> -> pid when that process is alive
  _p=$(cat "$1" 2>/dev/null || true)
  case "$_p" in ''|*[!0-9]*) return 1 ;; esac
  kill -0 "$_p" 2>/dev/null && echo "$_p"
}

up() {   # an orchd from before the rename serves /orch/ only
  curl -fsS -o /dev/null --max-time 2 "$BASE/piper/icon.svg" 2>/dev/null \
    || curl -fsS -o /dev/null --max-time 2 "$BASE/orch/icon.svg" 2>/dev/null
}

# --- login service (scripts/lib-service.sh) ---------------------------------------------
. "$SK/scripts/lib-service.sh"
service_kind() { svc_kind; }
service_installed() { svc_installed "$LABEL"; }

wait_up() {
  i=0
  while [ "$i" -lt 30 ]; do up && return 0; sleep 1; i=$((i + 1)); done
  echo "piperd did not come up on $BASE - see $HOME_DIR/piperd.log" >&2
  return 1
}

install_service() {
  home
  py=$(command -v python3) || { echo "piper: python3 not on PATH" >&2; exit 1; }
  for tool in docker hole git; do
    command -v "$tool" >/dev/null 2>&1 || echo "piper: warning - '$tool' is not on PATH; the service inherits this PATH" >&2
  done
  [ "$(svc_kind)" != none ] || { echo "piper: no launchd or systemd --user here - use 'piper ensure'" >&2; exit 1; }
  stop_manual
  if [ -n "$LEGACY_LABEL" ] && [ "$LABEL" != "$LEGACY_LABEL" ] && svc_installed "$LEGACY_LABEL"; then
    svc_uninstall "$LEGACY_LABEL"   # the old service runs orchestrator/orchd.py, which is gone
    echo "removed the old service $LEGACY_LABEL" >&2
  fi
  # an own PIPER_HOME is baked in; the default is not, so the service follows the migration
  SVC_ENV="HOME USER PATH PROJECT_PATH PIPER_HOME PIPER_PORT PIPER_ALLOWED_HOSTS PIPER_ALLOWED_TS_USERS PIPER_EXTENSIONS DOCKER_CONFIG ${PIPER_SERVICE_ENV:-}" \
    svc_install "$LABEL" "$HOME_DIR/piperd.log" "$py" "$PIPER/piperd.py"
  echo "piperd now starts at login and is restarted when it dies" >&2
  wait_up && echo "piperd is up on $BASE" >&2
}

uninstall_service() {
  svc_uninstall "$LABEL"
  echo "service removed - piperd is no longer started at login (sessions in ptyhost keep running)" >&2
}

stop_manual() {   # a pidfile-started piperd would hold the port the service wants
  for _f in "$PIDF" "$HOME_DIR/orchd.pid"; do   # orchd.pid: started before the rename
    if p=$(pid_of "$_f"); then kill "$p" 2>/dev/null || true; sleep 1; fi
    rm -f "$_f"
  done
}

# `piper` on PATH without a setup step: ~/.local/bin (Claude Code's own install dir, so on
# PATH for everyone using it). Never replaces a file or a live link someone else owns.
link_cli() {
  _b="$HOME/.local/bin"; _l="$_b/piper"; _t="${PIPER_CLI:-$SK/scripts/piper.sh}"
  case ":$PATH:" in *":$_b:"*) ;; *) return 0 ;; esac
  if [ -L "$_l" ]; then
    [ "$(readlink "$_l")" = "$_t" ] && return 0
    # points at another live checkout - its owner's choice; at this script while a wrapper ($PIPER_CLI) runs: the wrapper
    [ -e "$_l" ] && [ "$(readlink "$_l")" != "$SK/scripts/piper.sh" ] && return 0
  elif [ -e "$_l" ]; then return 0; fi
  mkdir -p "$_b" && ln -sf "$_t" "$_l" 2>/dev/null \
    && echo "linked $_l -> $_t: 'piper' now opens the GUI from any directory" >&2 || true
}

ensure() {
  home
  link_cli
  if service_installed; then
    up && { echo "piperd (service $LABEL) is running" >&2; return 0; }
    svc_start "$LABEL"
    wait_up && echo "piperd (service $LABEL) started" >&2
    return
  fi
  if p=$(pid_of "$PIDF"); then echo "piperd already running (pid $p)" >&2; return 0; fi
  # started some other way (launchd, a foreground `serve`): do not start a second one
  if up; then echo "piperd already answers on $BASE (not started by this script - launchd?)" >&2; return 0; fi
  nohup python3 "$PIPER/piperd.py" >> "$HOME_DIR/piperd.log" 2>&1 &
  echo $! > "$PIDF"
  i=0
  while [ "$i" -lt 20 ]; do
    up && { echo "piperd started (pid $(cat "$PIDF"), log $HOME_DIR/piperd.log)" >&2; return 0; }
    sleep 1; i=$((i + 1))
  done
  echo "piperd did not come up on $BASE - see $HOME_DIR/piperd.log" >&2
  return 1
}

stop() {
  if service_installed; then
    svc_stop "$LABEL"   # until the next login / ensure; KeepAlive would restart a killed one
    echo "piperd (service $LABEL) stopped until the next login or 'piper ensure'; sessions keep running in ptyhost"
    return 0
  fi
  if p=$(pid_of "$PIDF") || p=$(pid_of "$HOME_DIR/orchd.pid"); then kill "$p" && echo "piperd stopped (pid $p); sessions keep running in ptyhost"
  elif up; then echo "piperd answers on $BASE but was not started by this script" >&2; return 1
  else echo "piperd not running"; fi
  rm -f "$PIDF" "$HOME_DIR/orchd.pid"
}

if [ -n "$LEGACY_LABEL" ] && [ "$LABEL" != "$LEGACY_LABEL" ] && svc_installed "$LEGACY_LABEL" && ! svc_installed "$LABEL"; then
  echo "piper: the old login service $LEGACY_LABEL runs orchestrator/orchd.py, which is gone - run 'piper install-service' (sessions survive)" >&2
fi

case "${1:-open}" in
  serve) home; exec python3 "$PIPER/piperd.py" ;;
  attach) shift; home; link_cli; exec env PIPER_HOME="$HOME_DIR" PIPER_PORT="$PORT" python3 "$PIPER/ttyattach.py" "$@" ;;
  ensure) ensure ;;
  stop) stop ;;
  stop-all)
    stop || true   # a piperd started elsewhere must not keep the sandboxes alive
    # ptyhost holds the PTYs: SIGTERM makes it hang up every hole process (= teardown);
    # one started before the rename runs from orchestrator/
    pkill -TERM -f "$SK/(piper|orchestrator)/ptyhost.py" 2>/dev/null && echo "ptyhost stopped - every session sandbox is being destroyed" || echo "ptyhost not running"
    ;;
  restart)
    if service_installed && [ -n "${PIPER_EXTENSIONS:-}" ] && ! grep -qF -- "$PIPER_EXTENSIONS" "$(svc_file "$LABEL")"; then
      echo "the service was installed without $PIPER_EXTENSIONS - installing it again (sessions survive)" >&2
      install_service
    elif service_installed; then
      svc_restart "$LABEL"
      wait_up && echo "piperd (service $LABEL) restarted; sessions survive" >&2
    else
      stop || exit 1; ensure
    fi
    ;;
  install-service) install_service ;;
  uninstall-service) uninstall_service ;;
  open)
    ensure
    url=$(PIPER_HOME="$HOME_DIR" python3 "$PIPER/piperd.py" --print-url)
    if [ "${2:-}" = "--print" ]; then echo "$url"; exit 0; fi
    echo "$BASE"
    exec sh "$SK/scripts/open-url.sh" "$url"
    ;;
  remote-url)
    home
    if [ "${2:-}" = "--revoke" ]; then
      PIPER_HOME="$HOME_DIR" python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import common; n = common.revoke_token("remote"); print("remote token revoked (%d) - a device with the old link gets 401" % n if n else "no remote token")' "$PIPER"
      exit 0
    fi
    PIPER_HOME="$HOME_DIR" python3 "$PIPER/piperd.py" --print-url --remote
    echo "open it once on the device (it keeps the token); never 'tailscale funnel'" >&2
    ;;
  template)
    home
    PIPER_HOME="$HOME_DIR" python3 -c 'import os,sys; sys.path.insert(0, sys.argv[1]); import sessioncfg; u, k, s = sessioncfg.sync_template(os.path.expanduser("~/.claude")); print("updated: " + (", ".join(u) or "nothing (up to date)")); k and print("kept (edited in the template): " + ", ".join(k)); s and print("skipped (unreadable or linked out of ~/.claude): " + ", ".join(s))' "$PIPER"
    echo "template: $HOME_DIR/claude-template - every session start syncs it the same way; transcripts/history are never copied"
    ;;
  gitlab-token)
    home
    ( umask 077; tr -d '\r\n' > "$HOME_DIR/gitlab-token" )
    chmod 600 "$HOME_DIR/gitlab-token"
    echo "GitLab token stored in $HOME_DIR/gitlab-token (mode 600)"
    ;;
  mattermost-webhook)
    home
    ( umask 077; tr -d '\r\n' > "$HOME_DIR/mattermost-webhook" )
    chmod 600 "$HOME_DIR/mattermost-webhook"
    echo "Mattermost webhook stored in $HOME_DIR/mattermost-webhook (mode 600) - test it in the GUI: ⌘K > Mattermost"
    ;;
  doctor)
    shift
    PIPER_HOME="$HOME_DIR" python3 "$PIPER/doctor.py" "$@" && rc=0 || rc=$?
    if service_installed; then
      echo "     (checked with this shell's PATH; the login service keeps the PATH it was installed with)"
    fi
    exit "$rc"
    ;;
  status)
    if service_installed; then s="service $LABEL installed ($(service_kind))"; else s="no login service"; fi
    if up; then echo "piperd answers on $BASE - $s"
    elif p=$(pid_of "$PIDF"); then echo "piperd running (pid $p) but not answering on $BASE - $s"
    else echo "piperd not running - $s"; fi
    ;;
  *) sed -n '2,34p' "$self" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
