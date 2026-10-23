#!/bin/sh
# docs/media/tour.sh <showcase url> [out dir] - tour.mp4: a captioned walkthrough of a fresh showcase.py, recorded
# in real time by agent-browser (about 70 s), then compressed with ffmpeg. Run it on a showcase nothing else has
# touched: it allows the waiting push.
set -eu
URL=$1
OUT=${2:-$(dirname "$0")}
export AGENT_BROWSER_SESSION="piper-tour-$$"
ab() { agent-browser "$@" >/dev/null 2>&1 || true; }
ref() { agent-browser snapshot -i -c | grep -F "$1" | grep -o 'ref=e[0-9]*' | head -1 | cut -d= -f2; }
# every step hides the last caption first, so it never stands over the next screen
tap() { uncap; r=$(ref "$1"); [ -n "$r" ] && ab click "@$r"; ab wait "${2:-900}"; }
uncap() { ab eval "document.getElementById('tour-cap')?.remove()"; }
session() { tap "button \"$1"; }
# a caption bar at the bottom of the page; the text must not contain a single quote
cap() {
  ab eval "(() => { let d = document.getElementById('tour-cap'); if (!d) { d = document.createElement('div'); d.id = 'tour-cap';
    d.style.cssText = 'position:fixed;left:50%;bottom:30px;transform:translateX(-50%);z-index:2147483647;max-width:1100px;' +
      'background:rgba(12,13,15,.94);color:#ececed;border:1px solid #3a3c42;border-radius:12px;padding:12px 24px;' +
      'font:600 21px/1.35 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;text-align:center;' +
      'box-shadow:0 12px 40px rgba(0,0,0,.65);pointer-events:none'; document.body.append(d); }
    d.textContent = '$1'; })()"
  ab wait "${2:-5000}"
}

ab set viewport 1440 860
ab open "$URL"
ab wait 3000
tap 'button "Overview"' 1200
ab record start "$OUT/tour.webm"

cap "Piper: several coding agents at once, each in its own sandbox" 4500
cap "One queue: Blocked, Your turn and Running. What needs you is on top" 5500
cap "This agent wants to push. The card shows the exact command, the risk and the rule that asked" 7000
tap 'button "Allow' 1200
cap "Allow with one click or y. Everything is written to the audit log" 4500

session "Global agent"
tap 'button "Conversation"' 1000
cap "The Global agent planned the release and started one session per project" 6500

session "Idempotent charges"
tap 'button "Conversation"' 1000
cap "A finished turn, read as a conversation, with tool calls and token usage" 6000

session "Express checkout UI"
tap 'button "Files"' 1200
tap '"app/checkout"' 600
tap 'ExpressPay.tsx' 1000
cap "Edit the project while the agent works. Files hidden from the agent are marked" 6500

tap 'button "Terminal"' 600
tap 'button "Git window"' 1400
cap "Review the diff, then commit and push from the Git window" 6000
uncap
ab press Escape
ab wait 500

tap 'button "collapse to the strip"' 400
tap 'button "context panel"' 400
session "Express checkout UI"
session "Idempotent charges"
uncap
ab press Meta+Backslash
ab wait 1000
cap "Watch agents side by side, in their real terminals" 6000

tap 'button "Settings"' 1000
tap 'Security' 1000
cap "Choose Minimal, Recommended or Strict, or switch single rules" 6500
cap "Piper is open source: github.com/Kamest/Piper" 4000

ab record stop
ab close

# 1280 px, 24 fps, H.264 for every browser; the webm is only the recording
ffmpeg -loglevel error -y -i "$OUT/tour.webm" -vf "scale=1280:-2:flags=lanczos,fps=24" -c:v libx264 -preset slow \
  -crf 30 -pix_fmt yuv420p -movflags +faststart -an "$OUT/tour.mp4"
rm -f "$OUT/tour.webm"
echo "$OUT/tour.mp4"
