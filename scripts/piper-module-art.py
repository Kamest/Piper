#!/usr/bin/env python3
"""Module gallery art for the setup card: one stylised UI sketch per module, dummy data only.
Usage: python3 scripts/piper-module-art.py piper/web/img/modules   -> <out-dir>/<module>.svg (500x300)."""
import os
import sys

W, H = 500, 300
FONT = "font-family=\"-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif\""
MONO = "font-family=\"'JetBrains Mono','SF Mono',Menlo,monospace\""
FG, MUT, FAINT, LINE, CARD, WELL, TILE = "#e6edf3", "#939ba4", "#5c6570", "#363e4a", "#171b22", "#11151b", "#1f2630"
OK, WARN, BAD, LINK = "#47b954", "#ce9a2e", "#ea605a", "#64a5fd"


def frame(accent: str, body: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">
<defs>
 <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#161c25"/><stop offset="1" stop-color="#0c0f14"/></linearGradient>
 <radialGradient id="glow" cx="0.85" cy="0.1" r="0.75"><stop offset="0" stop-color="{accent}" stop-opacity="0.35"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient>
 <radialGradient id="glow2" cx="0.05" cy="1" r="0.6"><stop offset="0" stop-color="{accent}" stop-opacity="0.14"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient>
 <filter id="sh" x="-10%" y="-10%" width="120%" height="130%"><feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#000" flood-opacity="0.45"/></filter>
</defs>
<rect width="{W}" height="{H}" fill="url(#bg)"/><rect width="{W}" height="{H}" fill="url(#glow)"/><rect width="{W}" height="{H}" fill="url(#glow2)"/>
<g opacity="0.07" stroke="{FG}">{''.join(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}"/>' for x in range(0, W, 25))}{''.join(f'<line x1="0" y1="{y}" x2="{W}" y2="{y}"/>' for y in range(0, H, 25))}</g>
{body}
</svg>
"""


def win(x, y, w, h, title="", accent=LINE, r=10):
    """a window card with three dots and an optional title"""
    t = f'<text x="{x + 60}" y="{y + 17}" {FONT} font-size="10" fill="{MUT}">{title}</text>' if title else ""
    return (f'<g filter="url(#sh)"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{CARD}" stroke="{accent}" stroke-opacity="0.55"/></g>'
            f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="{r}" fill="{WELL}"/><rect x="{x}" y="{y + 16}" width="{w}" height="10" fill="{WELL}"/>'
            + "".join(f'<circle cx="{x + 14 + i * 12}" cy="{y + 13}" r="3.5" fill="{c}"/>' for i, c in enumerate(["#ff5f57", "#febc2e", "#28c840"]))
            + t)


def txt(x, y, s, size=11, fill=FG, weight="normal", mono=False, anchor="start"):
    return f'<text x="{x}" y="{y}" {MONO if mono else FONT} font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" xml:space="preserve">{s}</text>'


def bar(x, y, w, h=6, fill=FAINT, op=1.0, r=3):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" opacity="{op}"/>'


def pill(x, y, s, fill, text=None, size=9, pad=7):
    w = len(s) * size * 0.58 + pad * 2
    return (f'<rect x="{x}" y="{y}" width="{w:.0f}" height="{size + 7}" rx="{(size + 7) / 2}" fill="{fill}" fill-opacity="0.18" stroke="{fill}" stroke-opacity="0.6"/>'
            + txt(x + pad, y + size + 2, s, size, text or fill, "600"))


def button(x, y, s, fill=OK, color="#0b0e12", size=10):
    w = len(s) * size * 0.6 + 18
    return f'<rect x="{x}" y="{y}" width="{w:.0f}" height="{size + 10}" rx="5" fill="{fill}"/>' + txt(x + 9, y + size + 5, s, size, color, "600")


# ---------------------------------------------------------------- the sketches, one per module
def gitlab():
    a = "#fc6d26"
    b = win(40, 32, 420, 236, "Inbox · 3 new", a)
    rows = [("#1042", "Checkout fails for guest users", "2 new comments", a, True),
            ("#318", "Add CSV export to reports", "assigned", LINK, False),
            ("!77", "Review: cache warmup on deploy", "review", "#a371f7", False),
            ("#205", "Upgrade payment SDK", "mention", WARN, False)]
    for i, (iid, title, tag, c, hot) in enumerate(rows):
        y = 70 + i * 46
        if hot:
            b += f'<rect x="48" y="{y - 10}" width="404" height="42" rx="7" fill="{a}" fill-opacity="0.09" stroke="{a}" stroke-opacity="0.4"/>'
        b += f'<circle cx="64" cy="{y + 6}" r="4" fill="{c}"/>' + txt(76, y + 4, iid, 10, MUT, mono=True) + txt(112, y + 4, title, 11.5, FG, "600")
        b += pill(76, y + 11, tag, c)
        b += button(358, y - 2, "Open session") if hot else f'<rect x="376" y="{y - 2}" width="66" height="20" rx="5" fill="{TILE}" stroke="{LINE}"/>' + txt(386, y + 11, "Open", 10, MUT)
    return frame(a, b)



def github():
    """W23: GitHub like GitLab - issues, pull requests and Actions through the user's own gh login"""
    a = "#8b949e"
    b = win(40, 32, 420, 236, "GitHub · acme/shop", a)
    rows = [("#88", "Checkout fails for guest users", "new comment", WARN, True),
            ("#91", "Add CSV export to reports", "pull request", "#a371f7", False),
            ("#90", "CI: cache warmup on deploy", "Actions failed", BAD, False),
            ("#74", "Upgrade payment SDK", "assigned", LINK, False)]
    for i, (iid, title, tag, c, hot) in enumerate(rows):
        y = 70 + i * 46
        if hot:
            b += f'<rect x="48" y="{y - 10}" width="404" height="42" rx="7" fill="{FG}" fill-opacity="0.06" stroke="{a}" stroke-opacity="0.5"/>'
        b += f'<circle cx="64" cy="{y + 6}" r="4" fill="{c}"/>' + txt(76, y + 4, iid, 10, MUT, mono=True) + txt(104, y + 4, title, 11.5, FG, "600")
        b += pill(76, y + 11, tag, c)
        b += button(358, y - 2, "Open session") if hot else f'<rect x="376" y="{y - 2}" width="66" height="20" rx="5" fill="{TILE}" stroke="{LINE}"/>' + txt(386, y + 11, "Open", 10, MUT)
    return frame(a, b)

def agent():
    a = "#a371f7"
    b = win(30, 30, 250, 240, "Global agent", a)
    b += f'<rect x="110" y="66" width="158" height="34" rx="9" fill="{a}" fill-opacity="0.22"/>' + txt(120, 80, "Fix the checkout bug", 10, FG) + txt(120, 93, "and add the CSV export", 10, FG)
    b += f'<rect x="42" y="112" width="200" height="98" rx="9" fill="{TILE}"/>' + txt(52, 128, "Plan", 10, a, "700")
    for i, (s, done) in enumerate([("shop · reproduce + fix #1042", True), ("reports · CSV export #318", True), ("review the two diffs", False)]):
        y = 146 + i * 20
        b += f'<rect x="52" y="{y - 9}" width="11" height="11" rx="3" fill="{OK if done else "none"}" stroke="{OK if done else FAINT}"/>' + txt(70, y, s, 10, FG if done else MUT)
    b += f'<rect x="42" y="222" width="226" height="34" rx="8" fill="{WELL}" stroke="{LINE}"/>' + txt(52, 243, "Message the agent…", 10, FAINT)
    for i, (name, st, c) in enumerate([("shop", "working", OK), ("reports", "working", OK), ("docs", "waiting", WARN)]):
        y = 52 + i * 76
        b += f'<path d="M 280 {150} C 310 150, 300 {y + 26}, 320 {y + 26}" stroke="{a}" stroke-opacity="0.6" stroke-width="1.6" fill="none" stroke-dasharray="4 3"/>'
        b += f'<g filter="url(#sh)"><rect x="320" y="{y}" width="150" height="54" rx="9" fill="{CARD}" stroke="{c}" stroke-opacity="0.6"/></g>'
        b += f'<circle cx="336" cy="{y + 18}" r="4" fill="{c}"/>' + txt(346, y + 22, name, 11.5, FG, "600") + txt(346, y + 40, st, 10, c)
        b += bar(410, y + 16, 46, 5, FAINT, 0.6)
    return frame(a, b)


def grid():
    a = OK
    b = ""
    panes = [("shop", ["$ npm test", "✓ 128 passed", "▌"], OK), ("api", ["$ mvn verify", "BUILD SUCCESS", "▌"], OK),
             ("reports", ["> writing CSV export", "  ReportCsv.java +84", "▌"], LINK), ("docs", ["? Approve git push", "  [y] yes  [n] no", ""], WARN)]
    for i, (name, lines, c) in enumerate(panes):
        x, y = 34 + (i % 2) * 220, 30 + (i // 2) * 124
        b += f'<g filter="url(#sh)"><rect x="{x}" y="{y}" width="212" height="114" rx="8" fill="#0b0e12" stroke="{c}" stroke-opacity="{0.9 if i == 3 else 0.35}" stroke-width="{1.6 if i == 3 else 1}"/></g>'
        b += f'<rect x="{x}" y="{y}" width="212" height="22" rx="8" fill="{WELL}"/><rect x="{x}" y="{y + 14}" width="212" height="8" fill="{WELL}"/>'
        b += f'<circle cx="{x + 12}" cy="{y + 11}" r="3.5" fill="{c}"/>' + txt(x + 22, y + 15, name, 10, FG, "600")
        for j, s in enumerate(lines):
            col = c if j == 1 else FG
            b += txt(x + 12, y + 44 + j * 18, s, 10.5, col if s != "▌" else OK, mono=True)
        for j in range(2):
            b += bar(x + 12, y + 92 + j * 9, 60 + (i * 37 + j * 53) % 110, 4, FAINT, 0.5)
    return frame(a, b)


def http():
    a = LINK
    b = win(40, 30, 420, 240, "orders.http", a)
    b += f'<rect x="54" y="68" width="40" height="18" rx="4" fill="{OK}" fill-opacity="0.2"/>' + txt(60, 81, "POST", 10, OK, "700", True)
    b += txt(102, 81, "{{host}}/api/orders", 11, FG, mono=True) + button(372, 66, "▶ Run", LINK, "#0b0e12")
    b += txt(54, 108, "Content-Type: application/json", 10, MUT, mono=True)
    b += f'<line x1="54" y1="122" x2="446" y2="122" stroke="{LINE}"/>'
    b += pill(54, 134, "200 OK", OK, size=10) + txt(122, 145, "142 ms · 1.2 kB", 10, MUT)
    js = [("{", FG), ('  "id": 4821,', None), ('  "status": "paid",', None), ('  "items": 3,', None), ('  "total": 129.90', None), ("}", FG)]
    for i, (s, c) in enumerate(js):
        y = 172 + i * 15
        if c:
            b += txt(54, y, s, 10.5, c, mono=True)
        else:
            k, v = s.split(":", 1)
            b += txt(54, y, k + ":", 10.5, "#9cdcfe", mono=True) + txt(54 + len(k + ":") * 6.3, y, v, 10.5, "#e6c07b" if '"' in v else "#f78c6c", mono=True)
    return frame(a, b)


def db():
    a = "#39c5bb"
    b = win(36, 28, 428, 244, "shop · dev", a)
    b += f'<rect x="48" y="62" width="404" height="30" rx="6" fill="{WELL}" stroke="{LINE}"/>'
    b += txt(58, 81, "SELECT", 10.5, "#c678dd", "700", True) + txt(104, 81, "id, customer, total, status", 10.5, FG, mono=True)
    b += txt(296, 81, "FROM", 10.5, "#c678dd", "700", True) + txt(328, 81, "orders", 10.5, a, mono=True) + button(400, 67, "Run", a, "#0b0e12")
    cols = [("id", 48), ("customer", 100), ("total", 236), ("status", 320)]
    b += f'<rect x="48" y="102" width="404" height="22" fill="{TILE}"/>'
    for name, x in cols:
        b += txt(x + 8, 117, name, 10, MUT, "700")
    data = [("4821", "Alice Moore", "129.90", "paid", OK), ("4820", "Tom Berg", "54.00", "paid", OK), ("4819", "Eva Klein", "310.50", "pending", WARN),
            ("4818", "Jan Hruby", "18.20", "refunded", BAD), ("4817", "Mia Rossi", "76.40", "paid", OK)]
    for i, (iid, cust, tot, st, c) in enumerate(data):
        y = 124 + i * 26
        if i % 2:
            b += f'<rect x="48" y="{y}" width="404" height="26" fill="{WELL}" opacity="0.6"/>'
        b += txt(56, y + 17, iid, 10.5, MUT, mono=True) + txt(108, y + 17, cust, 10.5, FG) + txt(244, y + 17, tot, 10.5, FG, mono=True) + pill(328, y + 5, st, c)
    return frame(a, b)


def diag():
    a = BAD
    b = win(36, 30, 270, 240, "OrderService.java", a)
    code = [("public Order place(Cart cart) {", FG), ("  var total = cart.sum();", FG), ("  return repo.save(totl);", FG), ("}", FG), ("", FG), ("int count = items.size()", FG)]
    for i, (s, c) in enumerate(code):
        y = 66 + i * 19
        b += txt(48, y, str(i + 41), 10, FAINT, mono=True) + txt(72, y, s, 10.5, c, mono=True)
    for y, x1, x2 in ((66 + 2 * 19 + 4, 175, 205), (66 + 5 * 19 + 4, 72, 256)):
        d = " ".join(f"L {x} {y + (2 if (x // 4) % 2 else -1)}" for x in range(x1, x2, 4))
        b += f'<path d="M {x1} {y} {d}" stroke="{BAD}" stroke-width="1.4" fill="none"/>'
    b += f'<g filter="url(#sh)"><rect x="276" y="96" width="196" height="150" rx="10" fill="{CARD}" stroke="{a}" stroke-opacity="0.6"/></g>'
    b += txt(290, 120, "Diagnostics", 12, FG, "700") + pill(392, 109, "2 errors", BAD)
    for i, s in enumerate(["cannot find symbol: totl", "';' expected"]):
        y = 146 + i * 26
        b += f'<circle cx="294" cy="{y - 4}" r="4" fill="{BAD}"/>' + txt(304, y, s, 10, FG) + txt(304, y + 12, f"OrderService.java:{43 + i * 3}", 9, MUT, mono=True)
    b += button(290, 206, "Send to Claude  →", a, "#fff")
    return frame(a, b)


def stack():
    a = "#2fbfdf"
    b = win(36, 30, 428, 240, "Dev stack · shop", a)
    svcs = [("app", "up · 4 h", OK, "8080"), ("db", "up · 4 h", OK, "3306"), ("search", "restarting", WARN, "9200"), ("mail", "up · 4 h", OK, "1025")]
    for i, (name, st, c, port) in enumerate(svcs):
        y = 66 + i * 30
        b += f'<circle cx="56" cy="{y + 5}" r="5" fill="{c}"/>' + txt(70, y + 9, name, 11.5, FG, "600") + txt(140, y + 9, st, 10, c)
        b += pill(232, y - 2, ":" + port, LINK) + f'<rect x="300" y="{y - 3}" width="48" height="18" rx="4" fill="{TILE}" stroke="{LINE}"/>' + txt(312, y + 9, "Logs", 9.5, MUT)
        b += f'<rect x="354" y="{y - 3}" width="58" height="18" rx="4" fill="{TILE}" stroke="{LINE}"/>' + txt(364, y + 9, "Restart", 9.5, MUT)
    b += f'<rect x="48" y="190" width="404" height="68" rx="7" fill="#0b0e12"/>'
    for i, (s, c) in enumerate([("search  | starting node es-01", MUT), ("search  | recovered 3 indices", MUT), ("search  | started ✓", OK)]):
        b += txt(58, 208 + i * 17, s, 10, c, mono=True)
    return frame(a, b)


def prod():
    a = WARN
    b = win(36, 30, 428, 240, "Morning check · 07:30", a)
    vals = [3, 4, 3, 5, 4, 3, 4, 6, 5, 4, 3, 4, 5, 22, 17, 6, 4, 3]
    for i, v in enumerate(vals):
        h = v * 4
        b += f'<rect x="{56 + i * 12}" y="{150 - h}" width="8" height="{h}" rx="2" fill="{BAD if v > 10 else a}" opacity="{1 if v > 10 else 0.55}"/>'
    b += f'<line x1="52" y1="151" x2="276" y2="151" stroke="{LINE}"/>' + txt(56, 168, "errors / hour, last 24 h", 9.5, MUT)
    b += txt(292, 72, "New today", 11, FG, "700") + pill(370, 61, "3 new", a)
    for i, (n, s) in enumerate([("52×", "TimeoutException"), ("14×", "Connection refused"), ("3×", "NullPointerException")]):
        y = 92 + i * 30
        b += f'<rect x="290" y="{y}" width="164" height="24" rx="6" fill="{TILE}"/>' + txt(298, y + 16, n, 10, BAD, "700", True) + txt(326, y + 16, s, 10, FG)
    b += button(290, 188, "Investigate", OK) + f'<rect x="372" y="188" width="82" height="20" rx="5" fill="{TILE}" stroke="{LINE}"/>' + txt(382, 202, "Mute a week", 10, MUT)
    b += txt(56, 220, "Automatic Mon–Fri · 3 projects", 10, MUT)
    return frame(a, b)


def timesheet():
    a = "#58a6ff"
    b = win(36, 30, 428, 240, "This week", a)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    colors = ["#58a6ff", OK, "#a371f7", WARN]
    blocks = [[(0, 3), (1, 2), (3, 2)], [(1, 4), (2, 3)], [(0, 2), (2, 2), (3, 3)], [(1, 5), (0, 2)], [(2, 3), (3, 1), (0, 2)]]
    for i, d in enumerate(days):
        x = 60 + i * 62
        b += txt(x + 22, 62, d, 10, MUT, "600", anchor="middle")
        y = 72
        for c, hrs in blocks[i]:
            h = hrs * 18
            b += f'<rect x="{x}" y="{y}" width="44" height="{h - 3}" rx="5" fill="{colors[c]}" fill-opacity="0.75"/>'
            y += h
        b += txt(x + 22, 254, f"{sum(hh for _, hh in blocks[i])} h", 10, FG, "700", anchor="middle")
    for i, s in enumerate(["#1042 checkout", "#318 CSV export", "!77 review", "meetings"]):
        y = 76 + i * 24
        b += f'<rect x="376" y="{y - 9}" width="10" height="10" rx="3" fill="{colors[i]}"/>' + txt(392, y, s, 9.5, FG)
    b += button(376, 186, "Copy week", a, "#0b0e12")
    return frame(a, b)


def usage():
    a = "#e3b341"
    b = win(36, 30, 428, 240, "Usage · 30 days", a)
    b += txt(56, 74, "1.24 M", 24, FG, "800") + txt(56, 92, "tokens this month", 10, MUT) + pill(160, 60, "−18 % vs last", OK)
    vals = [(4, 9), (6, 14), (3, 7), (8, 20), (7, 16), (2, 5), (5, 11), (9, 24), (6, 15), (7, 18), (4, 10), (8, 21), (10, 26), (6, 14)]
    for i, (o, c) in enumerate(vals):
        x = 58 + i * 28
        b += f'<rect x="{x}" y="{246 - c * 5}" width="16" height="{c * 5 - o * 5}" rx="2" fill="{a}" opacity="0.8"/>'
        b += f'<rect x="{x}" y="{246 - o * 5}" width="16" height="{o * 5}" rx="2" fill="{OK}"/>'
    b += f'<line x1="52" y1="247" x2="448" y2="247" stroke="{LINE}"/>'
    for i, (s, c) in enumerate([("output", OK), ("cache", a)]):
        b += f'<rect x="{350 + i * 56}" y="66" width="9" height="9" rx="2" fill="{c}"/>' + txt(363 + i * 56, 75, s, 9.5, MUT)
    return frame(a, b)


def mattermost():
    a = "#1e6bd6"
    b = f'<g filter="url(#sh)"><rect x="170" y="18" width="160" height="276" rx="24" fill="#0b0e12" stroke="{LINE}" stroke-width="2"/></g>'
    b += f'<rect x="206" y="28" width="88" height="10" rx="5" fill="{TILE}"/>'
    b += txt(250, 64, "#dev-piper", 10, MUT, "600", anchor="middle")
    msgs = [("Piper", "shop needs you:", "Approve git push?", WARN), ("Piper", "reports finished a turn", "CSV export ready", OK), ("Piper", "docs is waiting", "since 3 min", LINK)]
    for i, (who, l1, l2, c) in enumerate(msgs):
        y = 80 + i * 62
        b += f'<rect x="182" y="{y}" width="136" height="52" rx="10" fill="{CARD}" stroke="{c}" stroke-opacity="0.5"/>'
        b += f'<circle cx="196" cy="{y + 15}" r="7" fill="{a}"/>' + txt(208, y + 18, who, 9.5, FG, "700") + txt(192, y + 33, l1, 9, FG) + txt(192, y + 45, l2, 9, c)
    b += f'<g filter="url(#sh)"><circle cx="350" cy="70" r="26" fill="{a}"/></g>'
    b += f'<path d="M 340 78 L 360 78 L 357 73 L 357 66 A 7 7 0 0 0 343 66 L 343 73 Z" fill="#fff"/><circle cx="350" cy="81" r="2.5" fill="#fff"/>'
    b += f'<circle cx="366" cy="52" r="8" fill="{BAD}"/>' + txt(366, 56, "3", 10, "#fff", "700", anchor="middle")
    for i, s in enumerate(["Quiet hours", "Weekends off", "Only when it waits"]):
        y = 130 + i * 30
        b += f'<rect x="352" y="{y}" width="120" height="22" rx="6" fill="{TILE}"/>' + txt(362, y + 15, s, 9.5, FG)
        b += f'<rect x="446" y="{y + 6}" width="18" height="10" rx="5" fill="{OK}"/><circle cx="459" cy="{y + 11}" r="3.5" fill="#0b0e12"/>'
    b += txt(34, 140, "Notifications", 13, FG, "700") + txt(34, 158, "where you already are", 10, MUT)
    return frame(a, b)


def codex():
    a = "#10a37f"
    b = win(36, 30, 428, 240, "New session", a)
    b += txt(56, 72, "Agent", 11, MUT, "600")
    for i, (name, on, c) in enumerate([("Claude", True, "#d97757"), ("Codex", False, a)]):
        x = 56 + i * 196
        b += f'<rect x="{x}" y="82" width="186" height="74" rx="10" fill="{c}" fill-opacity="{0.14 if on else 0.08}" stroke="{c}" stroke-opacity="{0.8 if on else 0.45}" stroke-width="{1.6 if on else 1}"/>'
        b += f'<circle cx="{x + 24}" cy="{108}" r="11" fill="{c}"/>' + txt(x + 24, 112, name[0], 11, "#fff", "800", anchor="middle")
        b += txt(x + 44, 112, name, 13, FG, "700") + txt(x + 16, 140, "same sandbox, same isolation", 9.5, MUT)
        b += f'<circle cx="{x + 168}" cy="104" r="7" fill="none" stroke="{c}" stroke-width="1.6"/>' + (f'<circle cx="{x + 168}" cy="104" r="3.5" fill="{c}"/>' if on else "")
    b += f'<rect x="56" y="170" width="382" height="54" rx="7" fill="{WELL}" stroke="{LINE}"/>' + txt(66, 190, "Task: compare both fixes for #1042", 10.5, FG) + bar(66, 202, 180, 5, FAINT, 0.6)
    b += button(372, 236, "Start ⌘↵", OK)
    return frame(a, b)


def composer():
    a = "#64a5fd"
    b = win(36, 30, 428, 240, "Terminal · fix-1042", a)
    for i, w in enumerate([300, 240, 330, 180]):
        b += bar(56, 60 + i * 16, w, 6, FAINT, 0.7)
    b += f'<rect x="52" y="146" width="396" height="108" rx="8" fill="{WELL}" stroke="{a}" stroke-opacity="0.7"/>'
    b += txt(66, 166, "Composer → fix-1042", 10, MUT) + pill(352, 156, "template", TILE, MUT)
    b += txt(66, 190, "Run the test suite again and summarise", 11, FG) + txt(66, 206, "which tests still fail and why.", 11, FG)
    b += button(372, 226, "Send ⌘↵", OK)
    return frame(a, b)


ART = {"gitlab": gitlab, "github": github, "agent": agent, "grid": grid, "http": http, "db": db, "diag": diag, "stack": stack, "prod": prod,
       "timesheet": timesheet, "usage": usage, "mattermost": mattermost, "codex": codex, "composer": composer}

if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for name, fn in ART.items():
        with open(os.path.join(out, name + ".svg"), "w") as f:
            f.write(fn())
    print("wrote", len(ART), "files to", out)
