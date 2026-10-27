#!/bin/sh
# docs/media/capture.sh <showcase url> [out dir] - the README / site screenshots from a running showcase.py
# (agent-browser, a fresh headless browser per run), then demo.gif from them (ffmpeg).
set -eu
URL=$1
OUT=${2:-$(dirname "$0")}
export AGENT_BROWSER_SESSION="piper-shot-$$"
ab() { agent-browser "$@" >/dev/null 2>&1 || true; }
ref() { agent-browser snapshot -i -c | grep -F "$1" | grep -o 'ref=e[0-9]*' | head -1 | cut -d= -f2; }
tap() { r=$(ref "$1"); [ -n "$r" ] && ab click "@$r"; ab wait "${2:-900}"; }
shot() { ab mouse move 1430 850; ab wait 300; agent-browser screenshot "$OUT/$1.png" >/dev/null; echo "$OUT/$1.png"; }
session() { tap "button \"$1"; }

# the phone: the queue with the waiting push card at phone width (phone.png, 585 px wide for the site)
ab set device "iPhone 14"
ab open "$URL"
ab wait 3000
tap 'button "Overview"' 1500
agent-browser screenshot "$OUT/phone.png" >/dev/null && { sips -Z 1266 "$OUT/phone.png" >/dev/null 2>&1 || true; }
ab close

ab set viewport 1440 860
ab open "$URL"
ab wait 3000

# the queue next to a running session, its card waiting
tap 'button "Overview"' 1200
session "Express checkout UI"
tap 'button "Terminal"'
shot queue
# the push goes through: the card leaves the screen for the rest of the tour
tap 'button "Allow' 1500

# the Global agent's plan
session "Global agent"
tap 'button "Conversation"' 1300
shot plan

# a finished turn, read as a conversation, with the session's context panel
session "Idempotent charges"
tap 'button "Conversation"' 1300
shot conversation

# Files: the agent's change, the hidden .env.local
session "Express checkout UI"
tap 'button "Files"' 1500
tap '"app/checkout"' 600
tap 'ExpressPay.tsx' 1300
shot files

# Git window
tap 'button "Terminal"' 600
tap 'button "Git window"' 1600
shot git
ab press Escape
ab wait 600

# two agents side by side, the queue folded to the strip and no context panel
tap 'button "collapse to the strip"' 500
tap 'button "context panel"' 500
session "Express checkout UI"
session "Idempotent charges"
ab press Meta+Backslash
ab wait 1400
shot grid

# Settings > Security
tap 'button "Settings"' 1200
tap 'Security' 1200
shot security

ab close

# demo.gif: the tour, 2.4 s a screen with a 0.4 s crossfade, 1200 px wide
set -- queue plan conversation grid files git security
args="" filter="" i=0
for n in "$@"; do args="$args -loop 1 -t 2.8 -i $OUT/$n.png"; i=$((i + 1)); done
prev="[0:v]" off=2.4 j=1
while [ $j -lt $i ]; do
  filter="$filter${prev}[$j:v]xfade=transition=fade:duration=0.4:offset=$off[v$j];"
  prev="[v$j]" off=$(echo "$off + 2.4" | bc) j=$((j + 1))
done
# shellcheck disable=SC2086
ffmpeg -loglevel error -y $args -filter_complex \
  "${filter}${prev}fps=8,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" \
  "$OUT/demo.gif"
# demo.mp4 + demo-poster.jpg: the same tour for the project page's <video>, sharper and smaller than the GIF
# shellcheck disable=SC2086
ffmpeg -loglevel error -y $args -filter_complex "${filter}${prev}fps=24,scale=1440:-2:flags=lanczos,format=yuv420p" \
  -c:v libx264 -preset slow -crf 28 -movflags +faststart -an "$OUT/demo.mp4"
ffmpeg -loglevel error -y -i "$OUT/queue.png" -q:v 3 "$OUT/demo-poster.jpg"
echo "$OUT/demo.gif"
