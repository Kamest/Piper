#!/bin/sh
# lib-service.sh - run a daemon as a LOGIN service: a launchd agent on macOS, a systemd --user
# unit on Linux. Sourced (not executed) by scripts/piper.sh (piperd) and by other daemons'
# service scripts. The daemon then starts at login and
# is restarted when it dies (launchd throttles a respawn to ~10 s).
#
# svc_kind                          launchd | systemd | none
# svc_installed <label>             rc 0 when the service file exists
# svc_file <label>                  that file's path
# svc_install <label> <log> <cmd…>  write + load it; env = $SVC_ENV (names, copied from the
#                                   current environment when set); stdout/stderr -> <log>
# svc_uninstall|svc_start|svc_stop|svc_restart <label>
# svc_pid <label>                   the running pid, or nothing
#
# Only the daemon's own process is signalled on stop/restart (systemd KillMode=process;
# launchd kills its job's process group only): children that started their own session
# (Piper's ptyhost) survive, like with the pidfile-started daemons.

_svc_gui() { echo "gui/$(id -u)"; }
_svc_plist() { echo "$HOME/Library/LaunchAgents/$1.plist"; }
_svc_unit() { echo "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$1.service"; }

svc_kind() {
  if [ "$(uname -s)" = Darwin ] && command -v launchctl >/dev/null 2>&1; then echo launchd
  elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then echo systemd
  else echo none; fi
}

svc_file() {
  case $(svc_kind) in
    launchd) _svc_plist "$1" ;;
    systemd) _svc_unit "$1" ;;
  esac
}

svc_installed() {
  case $(svc_kind) in
    launchd) [ -f "$(_svc_plist "$1")" ] ;;
    systemd) [ -f "$(_svc_unit "$1")" ] ;;
    *) return 1 ;;
  esac
}

svc_install() {   # svc_install <label> <log> <cmd> [args…]
  _l=$1 _log=$2; shift 2
  mkdir -p "$(dirname -- "$_log")"
  case $(svc_kind) in
    launchd)
      _p=$(_svc_plist "$_l")
      mkdir -p "$(dirname -- "$_p")"
      # plistlib writes valid XML whatever the paths contain
      SVC_ENV="${SVC_ENV:-}" python3 - "$_p" "$_l" "$_log" "$@" <<'PY'
import os, plistlib, sys
plist, label, log, argv = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
env = {k: os.environ[k] for k in os.environ.get("SVC_ENV", "").split() if os.environ.get(k)}
with open(plist, "wb") as f:
    plistlib.dump({"Label": label, "ProgramArguments": argv, "EnvironmentVariables": env,
                   "RunAtLoad": True, "KeepAlive": True, "ProcessType": "Interactive",
                   "StandardOutPath": log, "StandardErrorPath": log}, f)
PY
      launchctl bootout "$(_svc_gui)/$_l" 2>/dev/null || true
      launchctl bootstrap "$(_svc_gui)" "$_p"
      echo "installed launchd agent $_l ($_p)" >&2
      ;;
    systemd)
      _u=$(_svc_unit "$_l")
      mkdir -p "$(dirname -- "$_u")"
      {
        echo "[Unit]"
        echo "Description=$_l"
        echo "[Service]"
        printf 'ExecStart='
        for _a in "$@"; do printf '"%s" ' "$_a"; done
        echo
        for _k in ${SVC_ENV:-}; do
          eval "_v=\${$_k:-}"
          # % starts a systemd specifier: WSL appends the Windows PATH, which may hold one
          [ -n "$_v" ] && printf 'Environment="%s=%s"\n' "$_k" "$(printf '%s' "$_v" | sed 's/%/%%/g')"
        done
        echo "Restart=always"
        echo "RestartSec=2"
        echo "KillMode=process"
        echo "StandardOutput=append:$_log"
        echo "StandardError=append:$_log"
        echo "[Install]"
        echo "WantedBy=default.target"
      } > "$_u"
      systemctl --user daemon-reload
      systemctl --user enable --now "$_l.service"
      echo "installed systemd user unit $_l ($_u)" >&2
      echo "  (to keep it running without a login session: loginctl enable-linger ${USER:-$(id -un)})" >&2
      ;;
    *) echo "no launchd or systemd --user on this machine" >&2; return 1 ;;
  esac
}

svc_uninstall() {
  case $(svc_kind) in
    launchd)
      launchctl bootout "$(_svc_gui)/$1" 2>/dev/null || true
      rm -f "$(_svc_plist "$1")" ;;
    systemd)
      systemctl --user disable --now "$1.service" 2>/dev/null || true
      rm -f "$(_svc_unit "$1")"
      systemctl --user daemon-reload ;;
  esac
}

svc_start() {
  case $(svc_kind) in
    launchd) launchctl bootstrap "$(_svc_gui)" "$(_svc_plist "$1")" 2>/dev/null || launchctl kickstart "$(_svc_gui)/$1" ;;
    systemd) systemctl --user start "$1.service" ;;
  esac
}

svc_stop() {   # until the next login / svc_start: KeepAlive would restart a merely killed daemon
  case $(svc_kind) in
    launchd) launchctl bootout "$(_svc_gui)/$1" 2>/dev/null || true ;;
    systemd) systemctl --user stop "$1.service" ;;
  esac
}

svc_restart() {
  case $(svc_kind) in
    launchd) launchctl kickstart -k "$(_svc_gui)/$1" 2>/dev/null || launchctl bootstrap "$(_svc_gui)" "$(_svc_plist "$1")" ;;
    systemd) systemctl --user restart "$1.service" ;;
  esac
}

svc_pid() {
  case $(svc_kind) in
    launchd) launchctl print "$(_svc_gui)/$1" 2>/dev/null | awk '$1 == "pid" && $2 == "=" {print $3; exit}' ;;
    systemd) _p=$(systemctl --user show -p MainPID --value "$1.service" 2>/dev/null); [ "${_p:-0}" != 0 ] && echo "$_p" ;;
  esac
}
