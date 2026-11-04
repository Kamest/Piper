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
#                            every other command copies GITLAB_TOKEN/GITLAB_PAT from env there while none is stored
#   piper.sh mattermost-webhook  read a Mattermost incoming-webhook URL from stdin into
#                            $PIPER_HOME/mattermost-webhook (0600): notifications (9.12 E)
#   piper.sh attach [<id|project|.>] [--new] [--replay] [--take] [-l]
#                            a session in THIS terminal (Ghostty, ...): no argument = pick one;
#                            a project without a live session gets a new one; Ctrl-] detaches, the
#                            session keeps running. Needs ptyhost v8 (after `stop-all` once)
#   piper.sh extensions [--json] | trust <path> | untrust <path> | decline <path> | service  (piperd loads the trusted ones)
#   piper.sh status [--json]
#   piper.sh update           fast-forward this checkout (git pull --ff-only) and restart a running piperd onto
#                            it; sessions survive. Local changes or a non-git install: it says so, changes nothing
#   piper.sh demo [--agent claude|codex] [--no-start]  prepare a small project and try an approval
#   piper.sh install-service  run piperd as a login service (macOS launchd LaunchAgent, Linux
#                            systemd --user): starts at login, restarted when it dies
#   piper.sh uninstall-service
#
# Env: PIPER_HOME (~/.piper) PIPER_PORT (17895) PIPER_ALLOWED_HOSTS PIPER_ALLOWED_TS_USERS (9.56;
# install-service bakes them in, re-run it after a change). PROJECT_PATH comes from the environment,
# else $PIPER_ENV_FILE (a shell file sourced first, when set), else a trusted extension's env_file
# (parsed, never sourced), else the checkout's parent directory (the convention: $PROJECT_PATH/piper).
# Status lines go to stderr, so `open --print` prints the URL alone. With the service installed,
# ensure/stop/restart drive the service.
# Extensions: piperd loads the trusted ones itself; the service is always `piperd`.
# A trusted extension's manifest names the variables the service keeps (service_env) and older services
# install-service removes (legacy_labels; those of an extension inside this checkout whatever its trust,
# 9.172) - `restart` and `ensure` install `piperd` once while one of those is installed, keeping that
# service's python3 and baked variables this shell does not set.
# Overrides: PIPER_SERVICE_LABEL (piperd; tests use a throwaway one), PIPER_LEGACY_LABEL / PIPER_SERVICE_ENV
# (more labels / names), PIPER_NO_EXTENSIONS=1, PIPER_CLI the command ~/.local/bin/piper links to (this
# script by default; an API 1 extension's wrapper sets it), PIPER_NO_AUTO_RESTART=1 (open / ensure / status
# never restart a piperd that runs older code than this checkout, 9.171).
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


if [ -z "${PROJECT_PATH:-}" ] && [ -n "${PIPER_ENV_FILE:-}" ] && [ -f "$PIPER_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$PIPER_ENV_FILE"
fi

# Management commands never enter a wrapper. Otherwise one call reads the trusted extensions (a terminal
# run with nothing trusted asks once per found extension): an API 1 extension's wrapper is
# entered (backward compatibility), an API 2 one only lends its env_file values (unset ones), service_env
# names and legacy_labels (9.167) - the output is quoted shell assignments only.
EXT_WRAPPER="" EXT_SERVICE_ENV="" EXT_LEGACY_LABELS=""
if [ "${1:-}" != extensions ]; then
  if [ "${PIPER_NO_EXTENSIONS:-}" = 1 ]; then
    # 9.172: no extension, but the old service labels of the ones inside this checkout (its own former names)
    _ext=$(PIPER_HOME="$HOME_DIR" python3 "$PIPER/extfind.py" service --sh) || _ext=""
  elif [ -z "${PIPER_CLI:-}" ]; then
    _ext=$(PIPER_HOME="$HOME_DIR" python3 "$PIPER/extfind.py" --delegate --sh) || _ext=""
  else
    _ext=$(PIPER_HOME="$HOME_DIR" python3 "$PIPER/extfind.py" service --sh) || _ext=""
  fi
  eval "$_ext"
  if [ -n "$EXT_WRAPPER" ]; then
    exec env PIPER_SRC="$SK" "$EXT_WRAPPER" "$@"
  fi
fi
LEGACY_LABEL="${PIPER_LEGACY_LABEL:-} $EXT_LEGACY_LABELS"
# 9.172: a defaulted PROJECT_PATH yields to the one an old service had baked in (old_service_env)
[ -n "${PROJECT_PATH:-}" ] || PIPER_PROJECT_PATH_DEFAULT=1
PROJECT_PATH="${PROJECT_PATH:-$(dirname -- "$SK")}"
export PROJECT_PATH

if [ "${1:-}" = extensions ]; then
  shift
  exec env PIPER_HOME="$HOME_DIR" python3 "$PIPER/extfind.py" "$@"
fi

# $PIPER_HOME is created only by the commands that USE Piper: other tools may take its mere
# existence as "Piper is in use" (a dev stack dashboard's Sessions link)
home() { mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"; sync_token; }

# A login service does not inherit the shell env, so GITLAB_TOKEN / GITLAB_PAT (same order as
# piper/gitlab.py) is copied into $PIPER_HOME/gitlab-token; piperd reads it per request.
# Not baked into the service file: that one is world-readable.
# Only into an empty home: a stored token (GUI, `piper.sh gitlab-token`) wins - a stale env
# token (e.g. GITLAB_PAT from ~/.zshenv in a non-interactive shell) must never replace it.
sync_token() {
  _t="${GITLAB_TOKEN:-${GITLAB_PAT:-}}"
  [ -n "$_t" ] || return 0
  _s="$(cat "$HOME_DIR/gitlab-token" 2>/dev/null || true)"
  [ "$_s" = "$_t" ] && return 0
  if [ -n "$_s" ]; then
    echo "GitLab token in env (GITLAB_TOKEN/GITLAB_PAT) differs from $HOME_DIR/gitlab-token - keeping the stored one (Settings > GitLab to change it)" >&2
    return 0
  fi
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

# 9.171: the desktop app (a short launchd PATH) may be what replaces an old service: without `hole` on this
# PATH the old service's PATH is baked instead of this one. 9.172: its python3 is kept too (the desktop's
# bundled runtime must not become the service's interpreter)
old_service_value() {   # old_service_value <service file> path|python -> its PATH / interpreter, or nothing
  python3 - "$1" "$2" <<'PY_OLD' 2>/dev/null || true
import plistlib, re, shlex, sys
path, want = sys.argv[1], sys.argv[2]
try:
    with open(path, "rb") as f:
        data = plistlib.load(f)
    if want == "path":
        value = data.get("EnvironmentVariables", {}).get("PATH", "")
    else:
        value = (data.get("ProgramArguments") or [""])[0]
except Exception:
    with open(path, errors="replace") as f:
        text = f.read()
    if want == "path":
        m = re.search(r'^Environment="PATH=(.*)"$', text, re.M)
        value = m.group(1).replace("%%", "%") if m else ""
    else:   # systemd: ExecStart="<python3>" "<piperd.py>" (lib-service.sh), maybe with a -@:+! prefix
        m = re.search(r'^ExecStart=[-@:+!]*(.*)$', text, re.M)
        try:
            value = shlex.split(m.group(1))[0] if m else ""
        except (ValueError, IndexError):
            value = ""
print(value if isinstance(value, str) else "")
PY_OLD
}

# Linux-B1 (9.172): the old service's baked-in variables (the extension's, PIPER_PORT, PIPER_ALLOWED_HOSTS,
# DOCKER_CONFIG, ...) are not in the env of the desktop app or a non-login shell - the CLI that replaces it keeps
# every one (a value this shell sets wins - PROJECT_PATH only when set, not this script's default; PATH, HOME, USER, PIPER_HOME and PIPER_EXTENSIONS stay this script's)
old_service_env() {   # old_service_env <service file> -> `K=v; export K` for the unset ones, SVC_CARRIED=<all names>
  PIPER_PROJECT_PATH_DEFAULT="${PIPER_PROJECT_PATH_DEFAULT:-}" python3 - "$1" <<'PY_ENV' 2>/dev/null || true
import os, plistlib, re, shlex, sys
path = sys.argv[1]
try:
    with open(path, "rb") as f:
        env = plistlib.load(f).get("EnvironmentVariables", {})
except Exception:
    with open(path, errors="replace") as f:
        env = {m.group(1): m.group(2).replace("%%", "%")
               for m in re.finditer(r'^Environment="([A-Za-z_][A-Za-z0-9_]*)=(.*)"$', f.read(), re.M)}
skip = {"PATH", "HOME", "USER", "PIPER_HOME", "PIPER_EXTENSIONS", "PIPER_SERVICE_LABEL"}
names = []
for k, v in (env.items() if isinstance(env, dict) else ()):
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) and k not in skip and isinstance(v, str) and v:
        if not os.environ.get(k) or (k == "PROJECT_PATH" and os.environ.get("PIPER_PROJECT_PATH_DEFAULT") == "1"):
            print("%s=%s; export %s" % (k, shlex.quote(v), k))
        names.append(k)
print("SVC_CARRIED=%s" % shlex.quote(" ".join(names)))
PY_ENV
}

legacy_installed() {   # the first old service label (not $LABEL) installed here, or rc 1
  for _o in $LEGACY_LABEL; do
    if [ "$LABEL" != "$_o" ] && svc_installed "$_o"; then echo "$_o"; return 0; fi
  done
  return 1
}

install_service() {
  python3 "$PIPER/codebuild.py" validate || return 1
  home
  if ! command -v hole >/dev/null 2>&1; then
    for old_label in $LEGACY_LABEL; do
      if [ "$LABEL" != "$old_label" ] && svc_installed "$old_label"; then
        _op=$(old_service_value "$(svc_file "$old_label")" path)
        if [ -n "$_op" ]; then
          PATH=$_op; export PATH
          echo "piper: 'hole' is not on this PATH - the service keeps the PATH of $old_label" >&2
          break
        fi
      fi
    done
  fi
  py=""
  # F-D1b: a reinstall of piperd itself (Settings, the desktop) keeps its interpreter too, not the caller's python3
  if _old=$(legacy_installed) || { service_installed && _old=$LABEL; }; then
    _opy=$(old_service_value "$(svc_file "$_old")" python)
    case ${_opy##*/} in
      python*) if [ -x "$_opy" ]; then py=$_opy; echo "piper: the service keeps the python3 of $_old ($py)" >&2; fi ;;
    esac
  fi
  [ -n "$py" ] || py=$(command -v python3) || { echo "piper: python3 not on PATH" >&2; exit 1; }
  for tool in docker hole git; do
    command -v "$tool" >/dev/null 2>&1 || echo "piper: warning - '$tool' is not on PATH; the service inherits this PATH" >&2
  done
  [ "$(svc_kind)" != none ] || { echo "piper: no launchd or systemd --user here - use 'piper ensure'" >&2; exit 1; }
  stop_manual
  _carried=""
  for old_label in $LEGACY_LABEL; do
    if [ "$LABEL" != "$old_label" ] && svc_installed "$old_label"; then
      SVC_CARRIED=""
      eval "$(old_service_env "$(svc_file "$old_label")")"
      if [ -n "$SVC_CARRIED" ]; then
        _carried="$_carried $SVC_CARRIED"
        echo "piper: the service keeps $SVC_CARRIED of $old_label" >&2
      fi
      svc_uninstall "$old_label"   # its piperd ends; ptyhost and the sessions keep running
      echo "removed the old service $old_label" >&2
    fi
  done
  PORT="${PIPER_PORT:-17895}"; BASE="http://127.0.0.1:$PORT"   # the old service's own port, when it had one
  # 9.172: a piperd that no service known here runs (an old login service under a label nobody named, a
  # foreground `serve`) holds the port - a new job next to it would only crash-loop: never two jobs
  _w=0
  while up && [ -z "$(svc_pid "$LABEL")" ] && [ "$_w" -lt 10 ]; do sleep 0.5; _w=$((_w + 1)); done
  if up && [ -z "$(svc_pid "$LABEL")" ]; then
    echo "piper: piperd answers on $BASE, but no service known here runs it - stop that one first (launchctl list / systemctl --user list-units, look for piper); nothing installed" >&2
    exit 1
  fi
  # an own PIPER_HOME is baked in; the default is not, so the service follows the migration.
  # PIPER_EXTENSIONS is never baked in (9.167): piperd reads the trust file, so the GUI can change it
  SVC_ENV="HOME USER PATH PROJECT_PATH PIPER_HOME PIPER_PORT PIPER_ALLOWED_HOSTS PIPER_ALLOWED_TS_USERS DOCKER_CONFIG ${PIPER_SERVICE_ENV:-} $EXT_SERVICE_ENV $_carried" \
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
  # 9.172: only an old service is installed (stopped, or running under its old label): install $LABEL instead
  # of a piperd nothing restarts - except a running one with PIPER_NO_AUTO_RESTART=1
  if ! service_installed && _old=$(legacy_installed); then
    if [ "${PIPER_NO_AUTO_RESTART:-}" = 1 ] && up; then
      echo "piper: the old login service $_old is installed - run 'piper install-service' (sessions survive)" >&2
    else
      echo "the old login service $_old is installed - installing $LABEL instead (sessions survive)" >&2
      install_service
      return
    fi
  fi
  if service_installed; then
    up && { echo "piperd (service $LABEL) is running" >&2; return 0; }
    python3 "$PIPER/codebuild.py" validate || return 1
    svc_start "$LABEL"
    wait_up && echo "piperd (service $LABEL) started" >&2
    return
  fi
  if p=$(pid_of "$PIDF"); then echo "piperd already running (pid $p)" >&2; return 0; fi
  # started some other way (launchd, a foreground `serve`): do not start a second one
  if up; then echo "piperd already answers on $BASE (not started by this script - launchd?)" >&2; return 0; fi
  python3 "$PIPER/codebuild.py" validate || return 1
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
  _svc=$LABEL
  service_installed || _svc=$(legacy_installed) || _svc=""   # 9.172: an old service of this checkout
  if [ -n "$_svc" ]; then
    svc_stop "$_svc"   # until the next login / ensure; KeepAlive would restart a killed one
    echo "piperd (service $_svc) stopped until the next login or 'piper ensure'; sessions keep running in ptyhost"
    return 0
  fi
  if p=$(pid_of "$PIDF") || p=$(pid_of "$HOME_DIR/orchd.pid"); then kill "$p" && echo "piperd stopped (pid $p); sessions keep running in ptyhost"
  elif up; then echo "piperd answers on $BASE but was not started by this script" >&2; return 1
  else echo "piperd not running"; fi
  rm -f "$PIDF" "$HOME_DIR/orchd.pid"
}

restart() {
  python3 "$PIPER/codebuild.py" validate || return 1
  old=$(legacy_installed) || old=""
  if [ -n "$old" ]; then
    # once, e.g. an older extension-specific service becoming piperd: install-service removes it
    echo "the old login service $old is installed - installing $LABEL instead (sessions survive)" >&2
    install_service
  elif service_installed && grep -q PIPER_EXTENSIONS "$(svc_file "$LABEL")" 2>/dev/null; then
    echo "the service still names its extensions itself - installing it again so piperd follows the trust (sessions survive)" >&2
    install_service
  elif service_installed; then
    if [ "$(svc_kind)" = launchd ]; then
      # kickstart retains the loaded job, including a superseded preview launcher.
      # Reload the installed plist; ptyhost is a separate service/process.
      svc_stop "$LABEL"
      svc_start "$LABEL"
    else
      svc_restart "$LABEL"
    fi
    wait_up && echo "piperd (service $LABEL) restarted; sessions survive" >&2
  else
    stop || exit 1; ensure
  fi
}

# 9.171: after a `git pull` the running piperd still runs its old Python (it says so in /api/state, or is
# from before 9.171 and says nothing): open / ensure / status restart it once - the old login service of a
# trusted extension becomes `piperd` on the way. Everything goes to stderr (`open --print` prints the URL
# only); codebuild.py's marker keeps a restart that did not help from repeating. PIPER_NO_AUTO_RESTART=1: off.
update_daemon() {
  [ "${PIPER_NO_AUTO_RESTART:-}" != 1 ] || return 0
  up || return 0
  PIPER_HOME="$HOME_DIR" python3 "$PIPER/codebuild.py" check "$BASE" 2>/dev/null || return 0
  echo "Piper was updated - restarting the service (sessions keep running)" >&2
  ( restart ) >&2 || echo "piper: the restart did not finish - run 'piper restart'" >&2
}

old_hint() {
  if ! service_installed && _old=$(legacy_installed); then
    echo "piper: the old login service $_old is installed - run 'piper install-service' (sessions survive)" >&2
  fi
}
# the commands that replace an old service themselves do not ask for it (ensure / open say so when they do
# not, status after its update check, 9.172) - piperd's own `install-service` wrote this into piperd.log
case "${1:-open}" in
  install-service|restart|ensure|open|status) ;;
  *) old_hint ;;
esac

case "${1:-open}" in
  serve) home; exec python3 "$PIPER/piperd.py" ;;
  demo) shift; exec env PIPER_HOME="$HOME_DIR" PIPER_PORT="$PORT" python3 "$PIPER/demo.py" "$@" ;;
  attach) shift; home; link_cli; exec env PIPER_HOME="$HOME_DIR" PIPER_PORT="$PORT" python3 "$PIPER/ttyattach.py" "$@" ;;
  ensure) update_daemon; ensure ;;
  stop) stop ;;
  stop-all)
    stop || true   # a piperd started elsewhere must not keep the sandboxes alive
    # ptyhost holds the PTYs: SIGTERM makes it hang up every hole process (= teardown);
    # one started before the rename runs from orchestrator/
    pkill -TERM -f "$SK/(piper|orchestrator)/ptyhost.py" 2>/dev/null && echo "ptyhost stopped - every session sandbox is being destroyed" || echo "ptyhost not running"
    ;;
  restart) restart ;;
  update)
    # the desktop app's "Update Piper" and a terminal's `piper update` (9.173): the checkout install.sh made
    git -C "$SK" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
      || { echo "piper: $SK is not a git checkout - update Piper the way it was installed" >&2; exit 1; }
    before=$(git -C "$SK" rev-parse --short HEAD)
    git -C "$SK" pull --ff-only -q \
      || { echo "piper: git pull --ff-only failed in $SK (local changes or another branch?) - nothing was changed; update it yourself" >&2; exit 1; }
    after=$(git -C "$SK" rev-parse --short HEAD)
    if [ "$before" = "$after" ]; then echo "Piper is up to date ($after)"; exit 0; fi
    echo "Piper updated $before -> $after"
    if up; then
      ( restart ) >&2 || { echo "piper: the restart did not finish - run 'piper restart'" >&2; exit 1; }
    else
      echo "piperd is not running - the next start uses the update" >&2
    fi
    ;;
  install-service) install_service ;;
  uninstall-service) uninstall_service ;;
  open)
    update_daemon
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
  mobile)
    home
    shift
    if [ "${1:-}" = "install" ]; then
      python3 -m venv "$HOME_DIR/mobile-venv"
      "$HOME_DIR/mobile-venv/bin/python" -m pip install --disable-pip-version-check -r "$PIPER/mobile-requirements.txt"
      exit 0
    fi
    py=${PIPER_MOBILE_PYTHON:-$HOME_DIR/mobile-venv/bin/python}
    if [ ! -x "$py" ]; then
      echo 'Install the optional runtime first: task piper:mobile -- install' >&2
      exit 1
    fi
    PIPER_HOME="$HOME_DIR" exec "$py" "$PIPER/mobile.py" "$@"
    ;;
  template)
    home
    PIPER_HOME="$HOME_DIR" python3 -c 'import os,sys; sys.path.insert(0, sys.argv[1]); import sessioncfg; u, k, s = sessioncfg.sync_template(os.path.expanduser("~/.claude")); print("updated: " + (", ".join(u) or "nothing (up to date)")); k and print("kept (edited in the template): " + ", ".join(k)); s and print("skipped (unreadable or linked out of ~/.claude): " + ", ".join(s))' "$PIPER"
    echo "template: $HOME_DIR/claude-template - every session start syncs it the same way; transcripts/history are never copied"
    ;;
  gitlab-token)
    mkdir -p "$HOME_DIR" && chmod 700 "$HOME_DIR"   # not home(): the token from stdin replaces any stored one
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
    update_daemon
    old_hint
    if [ "${2:-}" = --json ]; then
      if up; then running=true; else running=false; fi
      PIPER_HOME="$HOME_DIR" python3 - "$PIPER" "$running" "$BASE/" "$LABEL" <<'PY_STATUS'
import json, sys
sys.path.insert(0, sys.argv[1])
import extensions
extensions.load()
print(json.dumps({"up": sys.argv[2] == "true", "url": sys.argv[3], "label": sys.argv[4], "extensions": extensions.names()}))
PY_STATUS
      exit 0
    fi
    if service_installed; then s="service $LABEL installed ($(service_kind))"
    elif _old=$(legacy_installed); then s="the old service $_old installed ($(service_kind)) - 'piper restart' installs $LABEL"
    else s="no login service"; fi
    if up; then echo "piperd answers on $BASE - $s"
    elif p=$(pid_of "$PIDF"); then echo "piperd running (pid $p) but not answering on $BASE - $s"
    else echo "piperd not running - $s"; fi
    ;;
  *) sed -n '2,37p' "$self" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
