#!/usr/bin/env python3
"""docs/media/make_banner.py - banner.svg (README header) and social.svg (1280x640 social preview) from the GUI's
own icon (piper/web/icon.svg) and the dark theme tokens (piper/web/themes.json). No fonts are embedded: GitHub
renders README SVGs as images, so the text uses the system font stack.
    python3 docs/media/make_banner.py        # then: magick -density 144 docs/media/social.svg -resize 1280x640 social.png
"""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "..", "piper", "web")
T = json.load(open(os.path.join(WEB, "themes.json")))["themes"]["dark"]["tokens"]
GLYPH = re.findall(r'<path fill="#e9e9ea" d="([^"]+)"', open(os.path.join(WEB, "icon.svg")).read())[0]
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'SF Mono', Menlo, Consolas, monospace"


def row(x: float, y: float, w: float, dot: str, label: str, title: str, sub: str, accent: bool = False) -> str:
    """One queue row as the GUI draws it: a section label, a dot, a title and its state."""
    bar = '<rect x="%g" y="%g" width="3" height="46" rx="1.5" fill="%s"/>' % (x, y + 22, dot) if accent else ""
    return ('<text x="%g" y="%g" font-size="11" letter-spacing="1.4" fill="%s" font-weight="600">%s</text>'
            '<rect x="%g" y="%g" width="%g" height="46" rx="8" fill="%s"/>%s'
            '<circle cx="%g" cy="%g" r="4.5" fill="%s"/>'
            '<text x="%g" y="%g" font-size="14" fill="%s" font-weight="600">%s</text>'
            '<text x="%g" y="%g" font-size="12" fill="%s">%s</text>') % (
        x, y + 12, T["faint"], label, x, y + 22, w, T["tile"], bar, x + 20, y + 45, dot,
        x + 36, y + 41, T["fg"], title, x + 36, y + 58, T["mut"], sub)


def card(x: float, y: float) -> str:
    """The approval card: what the agent wants to do and Allow / Deny."""
    return ('<g><rect x="{x}" y="{y}" width="330" height="150" rx="12" fill="{card}" stroke="{line}"/>'
            '<circle cx="{a}" cy="{b}" r="4.5" fill="{warn}"/>'
            '<text x="{c}" y="{d}" font-size="14" font-weight="600" fill="{fg}">Idempotent charges'
            '<tspan fill="{mut}" font-weight="400" dx="8">git push</tspan></text>'
            '<text x="{e}" y="{d}" font-size="11" font-weight="700" letter-spacing="1" fill="{warn}" '
            'text-anchor="end">MEDIUM</text>'
            '<rect x="{f}" y="{g}" width="298" height="34" rx="6" fill="{well}"/>'
            '<text x="{h}" y="{i}" font-size="12" fill="{fg}" font-family="{mono}">git push origin fix/idempotent-charge'
            '</text>'
            '<rect x="{j}" y="{k}" width="62" height="30" rx="6" fill="{tilebtn}" stroke="{line}"/>'
            '<text x="{l}" y="{m}" font-size="13" fill="{fg}" text-anchor="middle">Deny</text>'
            '<rect x="{n}" y="{k}" width="70" height="30" rx="6" fill="{acc}"/>'
            '<text x="{o}" y="{m}" font-size="13" font-weight="600" fill="{accfg}" text-anchor="middle">Allow</text>'
            '</g>').format(x=x, y=y, a=x + 22, b=y + 27, c=x + 36, d=y + 32, e=x + 312, f=x + 16, g=y + 50,
                           h=x + 28, i=y + 72, j=x + 178, k=y + 104, l=x + 209, m=y + 124, n=x + 246, o=x + 281,
                           mono=MONO, accfg=T["acc-fg"], **{k: v for k, v in T.items() if "-" not in k})


def logo(x: float, y: float, size: float) -> str:
    s = size / 64
    return ('<rect x="%g" y="%g" width="%g" height="%g" rx="%g" fill="%s" stroke="%s"/>'
            '<path transform="translate(%g %g) scale(%g)" fill="%s" d="%s"/>') % (
        x, y, size, size, size * 0.24, T["tile"], T["line"], x, y, s, T["fg"], GLYPH)


def svg(w: int, h: int, body: str) -> str:
    glow = ('<defs><radialGradient id="g" cx="78%%" cy="30%%" r="60%%">'
            '<stop offset="0" stop-color="%s" stop-opacity=".22"/><stop offset="1" stop-color="%s" stop-opacity="0"/>'
            '</radialGradient><pattern id="d" width="22" height="22" patternUnits="userSpaceOnUse">'
            '<circle cx="1" cy="1" r="1" fill="%s"/></pattern></defs>') % (T["link"], T["link"], T["line"])
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="%s">'
            '%s<rect width="100%%" height="100%%" rx="16" fill="%s"/><rect width="100%%" height="100%%" rx="16" '
            'fill="url(#d)" opacity=".55"/><rect width="100%%" height="100%%" rx="16" fill="url(#g)"/>%s</svg>\n') % (
        w, h, w, h, SANS, glow, T["page"], body)


def queue(x: float, y: float) -> str:
    return (row(x, y, 330, T["bad"], "BLOCKED", "payments-api", "Push of fix/idempotent-charge · Allow / Deny", True)
            + row(x, y + 80, 330, T["warn"], "YOUR TURN", "Global agent", "Release plan ready · 3 sessions started")
            + row(x, y + 160, 330, T["ok"], "RUNNING", "storefront", "Express checkout for saved cards"))


def chips(x: float, y: float, items: list[str]) -> str:
    out, cx = [], x
    for t in items:
        w = 18 + 6.9 * len(t)
        out.append('<rect x="%g" y="%g" width="%g" height="28" rx="14" fill="%s" stroke="%s"/>'
                   '<text x="%g" y="%g" font-size="13" fill="%s">%s</text>' % (cx, y, w, T["card"], T["line"],
                                                                             cx + 9, y + 19, T["mut"], t))
        cx += w + 10
    return "".join(out)


BANNER = svg(1280, 360, logo(72, 74, 76)
             + '<text x="168" y="134" font-size="64" font-weight="700" letter-spacing="-1.5" fill="%s">Piper</text>'
               % T["fg"]
             + '<text x="74" y="212" font-size="26" font-weight="600" fill="%s">Run a team of coding agents.</text>'
               % T["fg"]
             + '<text x="74" y="246" font-size="26" font-weight="600" fill="%s">Approve what matters.</text>' % T["mut"]
             + chips(74, 282, ["Claude Code", "Codex", "one sandbox per session", "local web GUI"])
             + queue(860, 46))

SOCIAL = svg(1280, 640, logo(96, 112, 92)
             + '<text x="210" y="184" font-size="76" font-weight="700" letter-spacing="-2" fill="%s">Piper</text>'
               % T["fg"]
             + '<text x="98" y="290" font-size="40" font-weight="600" fill="%s">Run a team of coding agents.</text>'
               % T["fg"]
             + '<text x="98" y="340" font-size="40" font-weight="600" fill="%s">Approve what matters.</text>' % T["mut"]
             + '<text x="98" y="410" font-size="21" fill="%s">Claude Code and Codex sessions, each in its own sandbox,'
               '</text><text x="98" y="440" font-size="21" fill="%s">supervised from one local workspace.</text>'
               % (T["mut"], T["mut"])
             + chips(98, 500, ["Claude Code", "Codex", "hole sandboxes", "approval cards", "git · files · DB · HTTP"])
             + queue(850, 120) + card(850, 400))

for name, text in (("banner.svg", BANNER), ("social.svg", SOCIAL)):
    with open(os.path.join(HERE, name), "w") as f:
        f.write(text)
    print(os.path.join(HERE, name))
