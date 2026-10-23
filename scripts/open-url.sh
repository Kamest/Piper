#!/bin/sh
# open-url.sh <url> - hand a URL to this machine's browser, the same way on every
# platform (Piper's GUI and every dashboard that opens one).
#
# The first opener that exists wins; wslview comes before the linux ones
# because under WSL the browser lives on the Windows side. A machine with no
# opener at all is NOT an error - the callers echo the URL before calling this,
# so a headless/ssh session still learns where to look.
#
# Env: BROWSER - an explicit opener, tried first
set -eu

URL=${1:?usage: open-url.sh <url>}

for opener in "${BROWSER:-}" wslview xdg-open open; do
  [ -n "$opener" ] || continue
  command -v "$opener" >/dev/null 2>&1 || continue
  "$opener" "$URL" >/dev/null 2>&1 && exit 0
done
# WSL without wslview: hand it to Windows directly
if command -v powershell.exe >/dev/null 2>&1; then
  powershell.exe -NoProfile -Command "Start-Process '$URL'" >/dev/null 2>&1 && exit 0
fi
echo "no browser opener found - open the URL above manually"
