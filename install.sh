#!/bin/sh
# install.sh - Piper in one command:
#
#   curl -fsSL https://raw.githubusercontent.com/Kamest/Piper/main/install.sh | sh
#   sh install.sh [--dir DIR] [--ref REF] [--yes] [--no-open] [--service]
#
# Checks git, Python >= 3.10 and a running Docker or Podman. Installs hole with hole's own installer and Task
# with Homebrew when they are missing, after asking (--yes answers yes). Clones Piper, or fast-forwards an
# existing checkout, copies examples/hole-settings.json to ~/.hole/settings.json when you have none, runs the
# doctor and opens the GUI. It never uses sudo, never installs Docker and never overwrites a file of yours.
#
# The checkout: the one this script runs from, else --dir, else $PROJECT_PATH/piper when PROJECT_PATH is set,
# else ~/.local/share/piper. Piper lists the git checkouts next to its own as projects, so it does not default
# to your home directory; add your projects folder in Settings > Projects or set PROJECT_PATH.
set -eu

REPO=https://github.com/Kamest/Piper.git
HOLE_INSTALL=https://raw.githubusercontent.com/lukashornych/hole/main/install.sh
DIR=${PIPER_DIR:-} REF=main YES=0 OPEN=1 SERVICE=0 PATH_HINT=

say() { printf 'install: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
usage() {
  printf '%s\n' 'install.sh [--dir DIR] [--ref REF] [--yes] [--no-open] [--service]' \
    '  --dir DIR    where the Piper checkout lives (default: $PROJECT_PATH/piper, else ~/.local/share/piper)' \
    '  --ref REF    branch or tag for a new clone (default: main)' \
    '  --yes        install missing hole and Task without asking' \
    '  --no-open    do not start Piper at the end' \
    '  --service    also run piperd as a login service (launchd / systemd --user)' >&2
}
while [ $# -gt 0 ]; do
  case $1 in
    --dir) [ $# -ge 2 ] || die "--dir needs a directory"; DIR=$2; shift 2 ;;
    --dir=*) DIR=${1#--dir=}; shift ;;
    --ref) [ $# -ge 2 ] || die "--ref needs a branch or tag"; REF=$2; shift 2 ;;
    --ref=*) REF=${1#--ref=}; shift ;;
    -y|--yes) YES=1; shift ;;
    --no-open) OPEN=0; shift ;;
    --service) SERVICE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option $1" ;;
  esac
done

# yes/no on the terminal, which works under `curl | sh` too; without a terminal only --yes says yes
ask() {
  [ "$YES" = 1 ] && return 0
  (: </dev/tty) 2>/dev/null || return 1
  printf 'install: %s [Y/n] ' "$1" >&2
  read -r answer </dev/tty || return 1
  case $answer in ''|y|Y|yes|Yes) return 0 ;; *) return 1 ;; esac
}
have() { command -v "$1" >/dev/null 2>&1; }

case $(uname -s) in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  *) die "Piper runs on macOS and Linux (WSL 2 works), not on $(uname -s)" ;;
esac

if ! have git; then
  [ "$OS" = macos ] && die "git is missing: xcode-select --install"
  die "git is missing: install it with your package manager"
fi
if ! have python3 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  [ "$OS" = macos ] && die "Piper needs Python 3.10 or newer as python3: brew install python"
  die "Piper needs Python 3.10 or newer as python3: install it with your package manager"
fi
if ! { have docker && docker info >/dev/null 2>&1; } && ! { have podman && podman info >/dev/null 2>&1; }; then
  [ "$OS" = macos ] && die "no running Docker or Podman: start Docker Desktop, OrbStack, Colima or Rancher Desktop, then run this again"
  die "no running Docker or Podman: Docker Engine 26+ with Compose 2.26+, or Podman; start it, then run this again"
fi

# hole installs into ~/.local/bin; this run and the piperd it starts get it on PATH either way
if ! have hole; then
  if [ ! -x "$HOME/.local/bin/hole" ]; then
    ask "hole (the sandbox) is missing. Install it with its own installer from github.com/lukashornych/hole?" \
      || die "Piper needs hole: curl -fsSL $HOLE_INSTALL | bash"
    have curl || die "curl is missing, and the hole installer needs it"
    have bash || die "bash is missing, and the hole installer needs it"
    curl -fsSL "$HOLE_INSTALL" | bash || die "the hole installer failed"
  fi
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  have hole || die "hole is not in ~/.local/bin after its installer; see github.com/lukashornych/hole"
  PATH_HINT=1
fi

# Task only adds the `task piper:*` shortcuts; scripts/piper.sh does the same without it
if ! have task && have brew && ask "Task (the task piper:* shortcuts) is missing. Install it with brew install go-task?"; then
  brew install go-task || say "brew install go-task failed; sh scripts/piper.sh <command> works without it"
fi

here=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || here=
if [ -z "$DIR" ] && [ -n "$here" ] && [ -f "$here/piper/piperd.py" ] && [ -f "$here/scripts/piper.sh" ]; then
  DIR=$here
else
  if [ -z "$DIR" ]; then
    if [ -n "${PROJECT_PATH:-}" ]; then DIR=$PROJECT_PATH/piper; else DIR=$HOME/.local/share/piper; fi
  fi
  if [ -d "$DIR/.git" ]; then
    [ -f "$DIR/piper/piperd.py" ] || die "$DIR is a git checkout, but not of Piper; pick another with --dir"
    say "updating $DIR"
    git -C "$DIR" pull --ff-only -q || die "git pull --ff-only failed in $DIR (local changes?); update it yourself"
  elif [ -e "$DIR" ]; then
    die "$DIR exists and is not a Piper checkout; pick another with --dir"
  else
    say "cloning Piper ($REF) into $DIR"
    mkdir -p "$(dirname -- "$DIR")"
    git clone -q --branch "$REF" "$REPO" "$DIR" || die "git clone of $REPO failed"
  fi
fi

if [ ! -e "$HOME/.hole/settings.json" ]; then
  mkdir -p "$HOME/.hole"
  cp "$DIR/examples/hole-settings.json" "$HOME/.hole/settings.json"
  say "wrote ~/.hole/settings.json from examples/hole-settings.json; edit it for your stack"
fi

[ -n "$PATH_HINT" ] && say "hole is in ~/.local/bin, which is not on your PATH: add  export PATH=\"\$HOME/.local/bin:\$PATH\"  to your shell profile"
say "checking this machine (piper doctor)"
sh "$DIR/scripts/piper.sh" doctor || die "fix the FAIL lines above, then run: sh $DIR/scripts/piper.sh open"
[ "$SERVICE" = 1 ] && sh "$DIR/scripts/piper.sh" install-service
if [ "$OPEN" = 1 ]; then
  sh "$DIR/scripts/piper.sh" open
  say "Piper is running. Next time: sh $DIR/scripts/piper.sh open (or piper, when ~/.local/bin is on PATH)"
else
  say "done. Start Piper with: sh $DIR/scripts/piper.sh open"
fi
