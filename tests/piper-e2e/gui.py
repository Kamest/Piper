#!/usr/bin/env python3
"""tests/piper-e2e/gui.py - the GUI regression gate: every view, dialog and the palette under every feature preset, at
two widths, with the DOM scans of tests/piper-e2e/scans on each screen. Self-checking: exit 0 = no finding, 1 = findings,
2 = the harness could not run (no agent-browser, serve.py did not come up).

Starts tests/piper-e2e/serve.py (own PIPER_HOME, fake-hole, fake GitLab; killed by PID at the end) unless --url names a
running one, and drives a headless Chromium with `agent-browser` (AGENT_BROWSER=<path> overrides the one on PATH).
Per preset (basic, dev, all; POST /api/features) and width (1024, 1920) it opens the session tabs, the global views,
the grid, the agent drawer, the main dialogs, the palette, the session menu and every Settings section, and fails on:
  - a scan finding: rlo.js (raw bidi / split ZWJ), clip.js (text cut without an ellipsis), reach.js (a control that
    cannot be clicked) - see scans/*.js
  - a JS error, unhandled rejection, console.error/warn, or an HTTP answer >= 400 / a failed request (scans/errors.js)
  - a view that does not open, or a module's command / icon / tab that is shown while the module is off (or vice versa)
Known, accepted findings go to scans/allow.json ({"scan", "match" (regex), "why", optional "screen"
(regex) and "width_max"}) - the one place
to look for them; the summary still lists every allowed hit.
Prints one line per finding, then a JSON summary (also <out>/gui.json); screenshots of failing screens go to <out>.

Run OUTSIDE any sandbox:  python3 tests/piper-e2e/gui.py [--port 17961] [--url URL] [--presets basic,dev,all]
    [--widths 1024,1920] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SCANS = os.path.join(HERE, "scans")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "piper"))
import testsandbox  # noqa: E402  only inside the OS sandbox of scripts/piper-sandbox.py
testsandbox.require()
sys.path.pop(0)
ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--port", type=int, default=17961, help="serve.py's port (it also takes +1, +2, +7, +9)")
ap.add_argument("--url", default="", help="a running serve.py's GUI url with #token=... (no serve.py started)")
ap.add_argument("--presets", default="basic,dev,all")
ap.add_argument("--widths", default="1024,1920")
ap.add_argument("--profile", action="store_true", help="report browser command and settling counts/times (no command payloads)")
ap.add_argument("--out", default="", help="summary + screenshots of failing screens (default: a temp dir)")
ap.add_argument("--security-tree", action="store_true", help="verify Security profiles and direct details with the tree enabled")
ap.add_argument("--approvals-only", action="store_true", help="exercise session approval cards only (also part of the full walk)")
ap.add_argument("--grid-only", action="store_true", help="exercise grid interactions only (also part of the full walk)")
ap.add_argument("--project-options-only", action="store_true", help="exercise project start preferences only (also part of the full gate)")
ap.add_argument("--clipboard-only", action="store_true", help="exercise terminal clipboard interactions only (also part of the full gate)")
ap.add_argument("--alerts-only", action="store_true", help="exercise alert expiry and Clear all positioning")
ap.add_argument("--database-only", action="store_true", help="exercise database overview search and grid chooser")
ap.add_argument("--overview-only", action="store_true", help="exercise the Overview icon folding and reopening the queue")
ap.add_argument("--limits-only", action="store_true", help="exercise compact plan usage, stale data and feature gates")
ap.add_argument("--sidebar-only", action="store_true", help="exercise compact sessions, search, header and Last turn")
ap.add_argument("--inspector-only", action="store_true", help="exercise Last turn placement, default fold and manual toggles")
ap.add_argument("--navigation-only", action="store_true", help="exercise Halo navigation, all tools, pins and keyboard control")
ap.add_argument("--walk-only", action="store_true", help="only the preset walks (a shard of the gate: piper:e2e runs --presets dev / all this way beside the rest)")
ap.add_argument("--settings-only", action="store_true", help="Settings A: every section, themes, search, drafts, validation and saving")
A = ap.parse_args()
if A.port == 17895 or ":17895/" in A.url:
    sys.exit("gui.py: never the live piperd (:17895)")
if A.url:   # it POSTs presets: only a serve.py on this machine's loopback, never the live ports
    import urllib.parse
    _u = urllib.parse.urlsplit(A.url)
    if _u.hostname not in ("127.0.0.1", "localhost") or (_u.port or 80) in (80, 17890, 17895, 17896):
        sys.exit("gui.py: --url must be a serve.py on 127.0.0.1:<its port>, not %s" % A.url.split("#")[0])
HEIGHT = {1024: 768}
SESSION = "piper-gui-%d" % os.getpid()
OUT = os.path.abspath(A.out) if A.out else tempfile.mkdtemp(prefix="piper-gui-")
os.makedirs(OUT, exist_ok=True)


def find_browser() -> str:
    ab = os.environ.get("AGENT_BROWSER") or "agent-browser"
    path = ab if os.path.isfile(ab) and os.access(ab, os.X_OK) else shutil.which(ab)
    if not path:
        print("gui.py: agent-browser not found (%s). Install it (npm i -g agent-browser && agent-browser install) "
              "or set AGENT_BROWSER=<path>." % ab, file=sys.stderr)
        sys.exit(2)
    return path


AB = find_browser()
TIMINGS: dict = {}


def record_timing(key: str, start: float) -> None:
    if A.profile:
        item = TIMINGS.setdefault(key, {"calls": 0, "seconds": 0.0})
        item["calls"] += 1
        item["seconds"] += time.perf_counter() - start


def ab(*args, timeout=60, session=SESSION) -> dict:
    env = dict(os.environ)
    if session != SESSION:   # the OS test sandbox otherwise gives all sessions the same Chrome profile
        env["AGENT_BROWSER_PROFILE"] = os.path.join(OUT, session + "-profile")
    started = time.perf_counter()
    try:
        r = subprocess.run([AB, "--session", session, "--json", *args], env=env, capture_output=True, text=True, timeout=timeout)
    finally:
        record_timing("browser:" + args[0], started)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"success": False, "error": (r.stdout + r.stderr).strip()[:500]}


def js(expr: str):
    """Evaluates expr in the page; a string result that is JSON comes back parsed."""
    r = ab("eval", expr)
    if not r.get("success"):
        raise RuntimeError("eval failed: %s" % r.get("error"))
    res = (r.get("data") or {}).get("result")
    if isinstance(res, str) and res[:1] in "{[":
        try:
            return json.loads(res)
        except ValueError:
            pass
    return res


def jq(s: str) -> str:
    return json.dumps(s)


def scan_src(name: str) -> str:
    with open(os.path.join(SCANS, name)) as f:
        return "\n".join(ln for ln in f.read().splitlines() if not ln.startswith("//"))


SETTLED = scan_src("settled.js")

SCAN_ALL = "JSON.stringify({rlo: %s, clip: %s, reach: %s, err: JSON.parse(%s)})" % (
    scan_src("rlo.js"), scan_src("clip.js"), scan_src("reach.js"), scan_src("errors.js"))
with open(os.path.join(SCANS, "allow.json")) as _f:
    ALLOW = [dict(a, rx=re.compile(a["match"])) for a in json.load(_f)]

KEY = """(k) => { const mac = /mac|iphone|ipad|ipod/i.test(navigator.platform);
  document.body.dispatchEvent(new KeyboardEvent('keydown', {key: k, code: k === 'Escape' ? 'Escape' : 'Key' + k.toUpperCase(),
    metaKey: k !== 'Escape' && mac, altKey: k !== 'Escape' && !mac, bubbles: true, cancelable: true})); }"""


def key(k: str) -> None:
    """Piper's ⌘-key (Alt off a Mac) as a synthetic keydown: real modifier chords are unreliable in headless Chromium."""
    js("(%s)(%s)" % (KEY, jq(k)))


def settle(extra=0.0) -> None:
    """Wait for rendered UI; explicit delays still test real polling/time behaviour."""
    started = time.perf_counter()
    try:
        js(SETTLED)
        if extra:
            time.sleep(extra)
            js(SETTLED)
    finally:
        record_timing("settle (includes browser calls)", started)


def reload() -> None:
    """ab reload, then wait for the NEW document: reload can return while the old page is still there, and a
    settle() / palette on that page raced its replacement (CI: the palette lacked demo/plain, the wrong session was used)"""
    js("window.__e2eOldPage = true")
    ab("reload")
    ab("wait", "--fn", "!window.__e2eOldPage && document.readyState === 'complete'")


def overlay_open() -> bool:
    return bool(js("!!document.querySelector('#overlay.open')"))


def close_overlay() -> None:
    for _ in range(3):
        if not overlay_open():
            return
        key("Escape")
        time.sleep(0.15)
    if overlay_open():
        ab("press", "Escape")


def palette_items() -> list:
    close_overlay()
    key("k")
    time.sleep(0.25)
    # 9.136: the Ended group is one "N ended" row - opened here so an ended session's row can be run
    js("document.querySelector('#overlay.open .pitem.punfold')?.click()")
    return js("JSON.stringify([...document.querySelectorAll('#overlay.open .pitem .name')].map(x => (x.querySelector('.plabel') ?? x).textContent))") or []


def palette_run(label: str) -> bool:
    """Runs a palette command by its exact label; False when the palette does not offer it."""
    items = palette_items()
    if label not in items:   # a second look: a state poll can move a row (live -> Ended) while the palette opens
        settle()
        items = palette_items()
    if label not in items:
        close_overlay()
        return False
    js("[...document.querySelectorAll('#overlay.open .pitem')].find(b => (b.querySelector('.plabel') ?? b.querySelector('.name')).textContent === %s).click()"
       % jq(label))
    settle()
    return True


def click(selector: str, text: str | None = None, attr: str = "") -> bool:
    cond = "true" if text is None else ("e.getAttribute(%s) === %s" % (jq(attr), jq(text)) if attr
                                        else "e.textContent.trim() === %s" % jq(text))
    ok = js("(() => { const e = [...document.querySelectorAll(%s)].find(e => %s && e.getBoundingClientRect().width > 0); "
            "if (!e) return false; e.click(); return true; })()" % (jq(selector), cond))
    if ok:
        settle()
    return bool(ok)


def click_start(selector: str, text: str) -> bool:
    """click() for a label with a count after it (a filter pill "All 2")"""
    ok = js("(() => { const e = [...document.querySelectorAll(%s)].find(e => e.textContent.trim().startsWith(%s) "
            "&& e.getBoundingClientRect().width > 0); if (!e) return false; e.click(); return true; })()" % (jq(selector), jq(text)))
    if ok:
        settle()
    return bool(ok)


def whole_queue(on: bool) -> bool:
    """N10-A: the whole queue instead of the strip (gui.py scanned the strip only); True = it changed.
    F01: the strip's "open" button is gone - the rail's Overview icon unfolds a folded queue."""
    if on:
        return bool(js("document.body.classList.contains('queue-mini')")) and click('#actbar button[aria-label="Overview"]') \
            and not js("document.body.classList.contains('queue-mini')")
    return click("[aria-label='collapse to the strip']")


def view() -> str:
    return (js("JSON.stringify(JSON.parse(localStorage.getItem('piper.last') || '{}'))") or {}).get("view", "")


def api(path: str, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


# ------------------------------------------------------------------ findings
FINDINGS: list = []
ALLOWED: list = []
SCREENS: list = []
CTX = {"preset": "", "width": 0}


def finding(screen: str, scan: str, line: str) -> None:
    rec = {"preset": CTX["preset"], "width": CTX["width"], "screen": screen, "scan": scan, "finding": line}
    for a in ALLOW:
        if a["scan"] == scan and a["rx"].search(line) and CTX["width"] <= a.get("width_max", 99999) \
                and re.search(a.get("screen", ""), screen):
            ALLOWED.append(dict(rec, why=a["why"]))
            return
    FINDINGS.append(rec)
    print("FAIL [%s %d] %s: %s %s" % (CTX["preset"], CTX["width"], screen, scan, line), flush=True)


def scan(screen: str) -> None:
    settle()
    r = js(SCAN_ALL)
    SCREENS.append("%s/%d/%s" % (CTX["preset"], CTX["width"], screen))
    bad = len(FINDINGS)
    if not str(r["rlo"]).startswith("clean"):
        for ln in str(r["rlo"]).split(" | ")[0].splitlines():
            finding(screen, "rlo", ln)
    for name, ok in (("clip", "noclip"), ("reach", "reach-ok")):
        if r[name] != ok:
            for ln in str(r[name]).splitlines():
                finding(screen, name, ln)
    err = r["err"]
    if not isinstance(err, dict):
        finding(screen, "errors", "the error collector is not installed (init script did not run): %r" % err)
    else:
        for ln in err["errors"]:
            finding(screen, "errors", ln)
    if len(FINDINGS) > bad:
        shot(screen)


def shot(screen: str) -> None:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", "%s-%d-%s" % (CTX["preset"], CTX["width"], screen)) + ".png"
    ab("screenshot", os.path.join(OUT, name))


def expect(screen: str, cond: bool, what: str) -> bool:
    if not cond:
        finding(screen, "nav", what)
        shot(screen)
    return cond


def grid(on: bool) -> bool:
    """⌘G from the palette. Its label follows the grid (9.45): "Grid: up to n panes at once" / "Grid: its n panes"
    turns it on, "Grid: off (Single, ...)" off - matched by prefix, not by a copy of the label."""
    starts = ("Grid: up to ", "Grid: its ") if on else ("Grid: off",)
    label = next((x for x in palette_items() if x.startswith(starts)), None)
    if label is None:
        close_overlay()
        return False
    return palette_run(label)


# F03: the header's one "Beside ▾" replaced the Single | Grid segment, "+ Add pane", the layout menu
# button and the tabs' "Files pane" / "DB pane" toggles. Its menu (besideMenu in main.ts): .bitem rows Files, Database,
# HTTP, Host shell, "Another session… / Grid n" (closed grid; ⌘G restores n panes) or "Another session…" (open grid:
# an Add cell), Composer; then the Layout presets (.lcard) and, while the grid is open, .lfoot "Choose panes…" / "Single".
BESIDE_MENU = ".layoutmenu.besidemenu"
GRID_ITEM = "Another session… / Grid"   # + " n" when ⌘G would restore more than one pane
ADD_ITEM = "Another session…"           # the same row while the grid is open: an Add cell


def beside_close() -> None:
    if js("!!document.querySelector(%s)" % jq(BESIDE_MENU)):
        click(".layoutctl button.beside")   # the anchor toggles the menu (closeBeside)


def beside_menu() -> bool:
    """opens Beside ▾ afresh (a stale one closed first); False when it did not open"""
    beside_close()
    return click(".layoutctl button.beside") and bool(js("!!document.querySelector(%s)" % jq(BESIDE_MENU)))


def beside_items() -> list:
    """Beside ▾'s rows as {label, checked, disabled}; the menu is closed again"""
    if not beside_menu():
        return []
    rows = js("JSON.stringify([...document.querySelectorAll(%s)].map(b => ({label: b.querySelector('.grow').textContent.trim(), "
              "checked: b.getAttribute('aria-checked') === 'true', disabled: b.disabled})))" % jq(BESIDE_MENU + " button.bitem")) or []
    beside_close()
    return rows


def beside_item(label: str, start: bool = False):
    """one Beside ▾ row by its exact label (start: by its prefix, "Another session… / Grid 2"); None = not offered"""
    return next((r for r in beside_items() if (r["label"].startswith(label) if start else r["label"] == label)), None)


def beside_click(label: str, start: bool = False) -> bool:
    """clicks an enabled Beside ▾ row (Files, Database, HTTP, Host shell, Another session… / Grid n, Composer)"""
    if not beside_menu():
        return False
    cond = "t.startsWith(%s)" % jq(label) if start else "t === %s" % jq(label)
    ok = js("(() => { const b = [...document.querySelectorAll(%s)].find(b => { const t = b.querySelector('.grow').textContent.trim(); "
            "return %s && !b.disabled; }); if (!b) return false; b.click(); return true; })()" % (jq(BESIDE_MENU + " button.bitem"), cond))
    if ok:
        settle()
    else:
        beside_close()
    return bool(ok)


def beside_layout(preset: str, start: bool = False) -> bool:
    """a Layout preset (Rows, Columns, Auto, …) from Beside ▾; a disabled one (does not fit) leaves the menu closed"""
    if not beside_menu():
        return False
    sel = BESIDE_MENU + " button[role=menuitemradio]:not(:disabled)"
    ok = click_start(sel, preset) if start else click(sel, preset)
    if not ok:
        beside_close()
    return ok


def layout_cards() -> list:
    """the Layout cards Beside ▾ offers now (9.135: open grid with 2+ panes only, one per shape), enabled ones"""
    if not beside_menu():
        return []
    cards = js("JSON.stringify([...document.querySelectorAll(%s)].map(b => b.querySelector('.llabel').textContent.trim()))"
               % jq(BESIDE_MENU + " button[role=menuitemradio]:not(:disabled)")) or []
    beside_close()
    return cards


def grid_shown() -> bool:
    """the grid is on screen (Beside ▾ carries .on: grid, not zoomed, Terminal view)"""
    return bool(js("!!document.querySelector('.layoutctl button.beside.on')"))


def layout_grid() -> bool:
    """the old "Grid" segment: Beside ▾ > Another session… / Grid n (⌘G's restore); True when the grid is already shown"""
    return grid_shown() or beside_click(GRID_ITEM, start=True)


def layout_single() -> bool:
    """the old "Single" segment: Beside ▾ > Single ⌘G (only offered while the grid is shown; elsewhere nothing to do)"""
    if not grid_shown():
        return False
    if not beside_menu():
        return False
    ok = click_start(BESIDE_MENU + " .lfoot button", "Single")
    if not ok:
        beside_close()
    return ok


def add_pane() -> bool:
    """the old "+ Add pane": in the shown grid Beside ▾ > Another session… (an Add cell); from Single the palette's
    "Grid: add a pane…" (Beside ▾'s row restores the grid there instead - the same addCell is only in the palette)"""
    return beside_click(ADD_ITEM) if grid_shown() else palette_run(GRID_ADD)


# F14: the Add cell (.term.slot) is one "+ Add pane" (.slot-add) whose popover (.cmenu.addpane, openMenu)
# lists Sessions / Host shells / Beside the active session / Database; it opens by itself when the cell appears. × = .slot-x
ADD_MENU = ".cmenu.addpane"


def slot_menu() -> bool:
    """the Add cell's popover, opened by "+ Add pane" when it is not open already"""
    if js("!!document.querySelector(%s)" % jq(ADD_MENU)):
        return True
    return click("#main .term.slot .slot-add") and bool(js("!!document.querySelector(%s)" % jq(ADD_MENU)))


def slot_pick(text: str, contains: bool = False) -> bool:
    """an enabled row of the Add cell's popover by the start of its label (contains: anywhere - a session row is
    "<dot> <title> — <state>")"""
    if not slot_menu():
        return False
    cond = "t.includes(%s)" % jq(text) if contains else "t.startsWith(%s)" % jq(text)
    ok = js("(() => { const b = [...document.querySelectorAll(%s)].find(b => { const t = b.querySelector('.grow').textContent.trim(); "
            "return %s && b.getAttribute('aria-disabled') !== 'true'; }); if (!b) return false; b.click(); return true; })()"
            % (jq(ADD_MENU + " button[role^=menuitem]"), cond))
    if ok:
        settle()
    return bool(ok)


def slot_close() -> bool:
    """× of the Add cell; an open popover closes first (a real click's mousedown would close it)"""
    if js("!!document.querySelector(%s)" % jq(ADD_MENU)):
        key("Escape")
    return click("#main .term.slot .slot-x")


def pane_menu_items(pane: str) -> dict:
    """F14: a grid pane's ⋯ menu ({label: disabled}); ◀ ▶ are its Move left / Move right now. The menu is closed again"""
    for _ in range(2):   # a second try: the menu closes on a window blur / resize, which CI's browser can send
        if not click(pane + " .tlabel button[aria-label='pane menu']"):
            return {}
        rows = js("JSON.stringify(Object.fromEntries([...document.querySelectorAll('.cmenu[aria-label=\"pane menu\"] button[role^=menuitem]')]"
                  ".map(b => [b.querySelector('.grow').textContent.trim(), b.getAttribute('aria-disabled') === 'true'])))") or {}
        if rows:
            break
    key("Escape")
    return rows


def pane_menu(pane: str, label: str) -> bool:
    """runs an enabled item of a grid pane's ⋯ menu (Move left / Move right / Remove from grid …)"""
    if not click(pane + " .tlabel button[aria-label='pane menu']"):
        return False
    ok = js("(() => { const b = [...document.querySelectorAll('.cmenu[aria-label=\"pane menu\"] button[role^=menuitem]')]"
            ".find(b => b.querySelector('.grow').textContent.trim() === %s && b.getAttribute('aria-disabled') !== 'true'); "
            "if (!b) return false; b.click(); return true; })()" % jq(label))
    if ok:
        settle()
    else:
        key("Escape")
    return bool(ok)


# ------------------------------------------------------------------ the walk
# the grid module's palette command whose label does not follow the grid's state (9.45)
GRID_ADD = "Grid: add a pane…"
# the inbox rows are named by the forge (inboxName, D9) - added per preset in walk(), gated on gitlab or github
MODULE_COMMANDS = {"db": "Database", "usage": "Token usage", "timesheet": "Timesheet",
                   "grid": GRID_ADD, "http": "HTTP requests", "prod": "Production check"}
# the grid has no rail icon since D1-D3: its Beside ▾ row "Another session… / Grid" is checked in walk() (F03, 9.69)
MODULE_ICONS = {"agent": "Agent", "db": "Database", "usage": "Usage", "timesheet": "Time"}
VIEWS = [("Database", "db", "db"), ("Token usage", "usage", "usage"),
         ("Timesheet", "timesheet", "timesheet"), ("Piper audit", "audit", None), ("Settings", "settings", None),
         ("Production check", "prod", "prod"), ("Setup: installation check & features", "setup", None)]
# "Git: repository history" became the Git window; "from an issue" is added per preset in walk()
DIALOGS = [("New session…", None), ("Resume conversation…", None),
           ("Keyboard shortcuts", None), ("Git window", None), ("GitLab automation…", "gitlab"),
           ("Mattermost: notifications and test", "mattermost")]
SESSION_TABS = [("Terminal", "term", None), ("Conversation", "convo", None), ("Files", "files", None),
                ("HTTP", "http", "http"), ("History", "history", None)]


def load_preset(preset: str) -> dict:
    api("/api/features", {"preset": preset, "modules": {"host": preset == "all"}, "onboarded": True})
    mods = api("/api/state")["features"]["modules"]
    js("localStorage.setItem('piper.last', JSON.stringify({view: 'term', active: %s}))" % jq(DEMO_SID))
    reload()
    end = time.time() + 20
    while time.time() < end:
        try:
            if js("document.querySelectorAll('#actbar button').length > 0 && !!document.querySelector('nav.tabs')"):
                break
        except RuntimeError:
            pass
        time.sleep(0.3)
    settle(0.6)
    return mods


def forge_name(inbox) -> str:
    """inboxName() of inbox.ts: GitLab alone, GitHub alone (GitLab off), Inbox with both"""
    if not (inbox or {}).get("github"):
        return "GitLab"
    return "GitHub" if inbox.get("status") == "off" else "Inbox"


def walk(preset: str, width: int, mods: dict) -> None:
    CTX.update(preset=preset, width=width)
    # D9: the inbox (view, palette rows, "from an issue") follows the forge modules, named by inboxName()
    forge_on = bool(mods.get("gitlab") or mods.get("github"))
    forge = forge_name(api("/api/state").get("inbox"))
    inbox_row = "Inbox" if forge == "Inbox" else forge + " inbox"
    from_issue = "New session from %s issue…" % ("an" if forge == "Inbox" else "a " + forge)
    commands = dict(MODULE_COMMANDS, forge=inbox_row)
    views = [(inbox_row, "gitlab", "forge")] + VIEWS
    dialogs = DIALOGS[:1] + [(from_issue, "forge")] + DIALOGS[1:]
    mods = dict(mods, forge=forge_on)
    ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
    close_overlay()
    click("nav.tabs button", "Terminal")
    scan("overview")
    overview_toggle()
    # The whole session list has no source filters; text search covers every section.
    opened = whole_queue(True)
    scan("queue whole")
    if opened:
        whole_queue(False)
    click("nav.tabs button", "Terminal")
    # the module switches: icons, palette commands, tabs follow the preset
    icons = js("JSON.stringify([...document.querySelectorAll('#actbar button')].map(b => b.getAttribute('aria-label')))") or []
    click('#actbar button[aria-label="All tools"]')
    icons += js("[...document.querySelectorAll('.nav-tools .nav-tool-label > span:first-of-type')].map(e => e.textContent)") or []
    ab("press", "Escape")
    for m, label in dict(MODULE_ICONS, forge=forge).items():
        expect("actbar", (label in icons) == bool(mods.get(m)), "icon %s shown=%s with module %s=%s"
               % (label, label in icons, m, mods.get(m)))
    # F03: the Single / Grid segment is Beside ▾'s "Another session… / Grid" row; Database / HTTP rows follow their modules
    rows = [r["label"] for r in beside_items()]
    expect("layout", bool(rows), "Beside ▾ did not open")
    seg = any(r.startswith(ADD_ITEM) for r in rows)   # "Another session… / Grid n" (closed grid) or "Another session…" (open)
    expect("layout", seg == bool(mods.get("grid")), "Beside ▾ grid row shown=%s with module grid=%s: %r" % (seg, mods.get("grid"), rows))
    for m, label in (("db", "Database"), ("http", "HTTP")):
        expect("layout", (label in rows) == bool(mods.get(m)), "Beside ▾ row %s shown=%s with module %s=%s"
               % (label, label in rows, m, mods.get(m)))
    items = palette_items()
    scan("palette")
    close_overlay()
    for m, label in commands.items():
        expect("palette", (label in items) == bool(mods.get(m)), "command %r offered=%s with module %s=%s"
               % (label, label in items, m, mods.get(m)))
    tabs = js("JSON.stringify([...document.querySelectorAll('nav.tabs button')].map(b => b.textContent.trim()))") or []
    for label, v, m in SESSION_TABS:
        if m and not mods.get(m):
            expect("tabs", label not in tabs, "tab %s shown with module %s off" % (label, m))
            continue
        if expect("tab " + label, click("nav.tabs button", label) and view() == v, "tab %s did not open (view %r)" % (label, view())):
            scan("tab " + label)
    click("nav.tabs button", "Terminal")
    grid_ux(mods)
    files_deep(mods)
    http_deep(mods)
    # the session's menu
    if expect("session menu", click("[aria-label='more actions']"), "the session's ⋯ menu did not open"):
        scan("session menu")
        ab("press", "Escape")
        js("document.body.click()")
    # global views
    for label, v, m in views:
        if m and not mods.get(m):
            continue
        if expect(label, palette_run(label) and view() == v, "%r did not open view %s (view %r)" % (label, v, view())):
            scan(label)
            if v == "timesheet":   # N10-D-6: a long unbroken manual-row label ran past the card (no scan sees it)
                expect(label + " / long row", js("(() => { const li = document.querySelector('.tsheet ul.small li'); "
                                                 "return !li || li.getBoundingClientRect().right <= document.querySelector('.tsheet')"
                                                 ".getBoundingClientRect().right + 1; })()"), "a manual row runs past the timesheet card")
            if v == "settings":
                # the Advanced fold is a .st-gfold (its caret is a .fi icon since the pages polish, no ▾/▸ text)
                names = js("JSON.stringify([...document.querySelectorAll('nav.st-side button:not(.st-gfold)')]"
                           ".map(b => b.textContent.trim()).filter(t => !/^[▾▸]/.test(t)))") or []
                for n in names:
                    if expect("settings " + n, click("nav.st-side button", n), "settings section %r did not open" % n):
                        scan("settings " + n)
    palette_run(DEMO_TITLE) or click("nav.tabs button", "Terminal")
    click("nav.tabs button", "Terminal")
    # the grid and the agent drawer
    if mods.get("grid") and expect("grid", grid(True), "Grid did not open"):
        scan("grid")
        # G-N11-6: a global view opened from the grid - the grid's splitter (z-index 3) stayed over it (Database's Add)
        if palette_run("Settings") and view() == "settings":
            scan("grid -> Settings")
            expect("grid -> Settings", js("document.querySelectorAll('#main .gsplit, #main .term.slot').length === 0"),
                   "the grid's splitter / Add cell stayed over Settings")
            # G-N11-7: Grid from a global view shows the grid (it turned the unseen grid off: a second click showed it)
            expect("grid from Settings", grid(True) and view() == "term"
                   and js("document.querySelector('#main .body').classList.contains('grid')"), "Grid from Settings did not show the grid")
        grid(False)
    if mods.get("agent") and api("/api/state").get("feed", {}).get("on"):
        if expect("agent drawer", click("#actbar button", "Agent", attr="aria-label"), "the Agent icon did not react"):
            scan("agent drawer")
            # G-N11-4 / 5: the grid beside the drawer (1024: the isolation chip squeezed to 0 px; 768: tile names 0 px)
            if mods.get("grid") and grid(True):
                scan("agent drawer + grid")
                grid(False)
            if whole_queue(True):   # N10-A: the drawer beside the whole queue (1024: the detail keeps 271 px)
                scan("agent drawer + whole queue")
                whole_queue(False)
            click("#actbar button", "Agent", attr="aria-label")
    # G-N11-1: a phone-width window (390 px) with the context panel open (the detail was 0 px, the panel's toggle under it)
    if width == min(int(w) for w in A.widths.split(",")):
        ab("set", "viewport", "390", "844")
        settle(0.3)
        was_off = js("document.body.classList.contains('no-inspector')")
        if not was_off or click("[aria-label='context panel']"):
            scan("phone 390 + context panel")
            expect("phone 390 commit hint", js("(() => { const hints = Array.from(document.querySelectorAll('#inspector .gfoot > .grow'))"
                                              ".filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height)"
                                              "; return hints.length > 0 && hints.every(e => e.scrollWidth <= e.clientWidth + 1); })()"),
                   "commit hint is clipped in the narrow context panel")
            shot("phone 390 + context panel")
            expect("phone 390 + context panel", js("document.documentElement.scrollWidth <= innerWidth && "
                                                   "document.querySelector('#main').getBoundingClientRect().width >= 200"),
                   "the detail is under 200 px or the page scrolls sideways at 390 px")
            if was_off:
                click("[aria-label='context panel']")
        # the whole queue at 390 px left the detail 46 px: it takes the detail's place, a pick folds it to the strip
        if whole_queue(True):
            scan("phone 390 + whole queue")
            shot("phone 390 whole queue")
            expect("phone 390 + whole queue", js("document.documentElement.scrollWidth <= innerWidth && "
                                                 "document.querySelector('#queue').getBoundingClientRect().width >= 300 && "
                                                 "getComputedStyle(document.querySelector('#main')).display === 'none'"),
                   "the whole queue does not take the detail's place at 390 px")
            # the demo session's row: the first row is serve.py's Global agent card now (9.76) - picking it left the
            # advisor active and the walk's dialogs (Git window) without a project
            if expect("phone 390 queue pick", click("#queue .qi[data-sid=%s] .qi-title" % jq(DEMO_SID)), "no queue item to pick"):
                settle(0.3)
                expect("phone 390 queue pick", js("document.body.classList.contains('queue-mini') && "
                                                  "document.querySelector('#main').getBoundingClientRect().width >= 200"),
                       "picking an item left the whole queue over the detail")
                scan("phone 390 queue pick")
            else:
                whole_queue(False)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        settle(0.3)
    # dialogs
    for label, m in dialogs:
        if m and not mods.get(m):
            continue
        if expect(label, palette_run(label) and overlay_open(), "%r opened no dialog" % label):
            scan(label)
            # N10-A: the issue picker open with an issue picked (1024 x 768 squeezed the task box to 14 px)
            if label == from_issue and click("#overlay .wk-pane > .plist .pitem") \
                    and click("#overlay .issues .pitem"):
                scan(label + " / issue picked")
                expect(label + " / issue picked", (js("Math.round(document.querySelector('#overlay .wk-pane > textarea')"
                                                     ".getBoundingClientRect().height)") or 0) >= 60,
                       "the task box of the start dialog is squeezed under 60 px")
        close_overlay()
    expect("dialogs", not overlay_open(), "a dialog stayed open after Escape")
    # N10-D-7: the morning check's session (no checkout): its context panel asked for Git / CI of project "prodcheck"
    # (piperd's handler crashed, CI 404) - started once, then looked at under every preset that has Production
    if mods.get("prod") and prodcheck_session():
        # the API has the session before the GUI's next poll does: on a slow runner the palette lacked it (CI flake)
        end, opened = time.time() + 15, False
        while not opened and time.time() < end:
            opened = palette_run("Morning production check")   # D12: a session's row is its title
            if not opened:
                time.sleep(1.0)
        if expect("prodcheck session", opened, "the morning check's session did not open"):
            scan("prodcheck session")
        palette_run(DEMO_TITLE)


def grid_ux(mods: dict) -> None:
    """Visible-workspace regressions: Single/tools, split, add/cancel, focus, zoom, order, resize and presets."""
    # A fresh Grid must open beside the active agent, also after the agent ends.
    js("localStorage.setItem('piper.grid', '[]'); localStorage.setItem('piper.gridlayout', 'rows')")
    reload()
    settle()
    expect("grid ended agent", palette_run("demo/plain"), "the palette did not offer demo/plain (every ended-agent check below would test another session)")
    click("nav.tabs button", "Terminal")
    ended = js("document.querySelector('#main > header').dataset.sid")

    def beside(label, selector, sid=None):
        r = js("""(() => {
            const agent = [...document.querySelectorAll('#main .body > .term:not(.hidden)')]
                .find(e => e.dataset.sid === %s);
            const tool = document.querySelector(%s);
            if (!agent || !tool) return null;
            const box = (e) => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map(Math.round); };
            const a = agent.getBoundingClientRect(), t = tool.getBoundingClientRect();
            return {ok: a.width > 80 && t.width > 80 && Math.abs(a.y - t.y) < 2
                && a.right <= t.x + 1 && Math.abs(a.height - t.height) < 2, agent: box(agent), tool: box(tool)};
        })()""" % (jq(sid or ended), jq(selector))) or {}
        expect(label, bool(r.get("ok")), "agent must remain on the left, tool on the right at full workspace height"
               " (agent x,y,w,h %s, tool %s)" % (r.get("agent"), r.get("tool")))
        if "slot" not in selector:
            # F14 (9.78): ◀ ▶ are the pane menu's Move left / Move right - disabled in the fixed pair
            n = js("""(() => {
                const panes = [...document.querySelectorAll('#main .body > .term:not(.hidden)')];
                panes.forEach((p, i) => p.dataset.e2ePane = String(i));
                return panes.length === 2 && panes.every(p => !p.querySelector('.tlabel').draggable) ? panes.length : 0;
            })()""")
            moves = [pane_menu_items('#main .body > .term[data-e2e-pane="%d"]' % i) for i in range(n or 0)]
            expect(label + " / fixed sides", bool(n) and all(m.get("Move left") is True and m.get("Move right") is True for m in moves),
                   "the simple pair must not offer swapping its agent to the right: %r" % moves)
        scan(label)

    if mods.get("grid"):
        expect("grid first opening", layout_grid(), "Beside ▾ > Another session… / Grid did not open the grid")
        beside("grid first opening after exit", "#main .term.slot")
        # An exited session can still have a terminal transcript, so statePanel's exit bar is absent.
        js(r"""(() => {
            const realFetch = window.fetch.bind(window);
            window.__gridRetries = [];
            window.__gridConversation = '00000000-0000-4000-8000-000000000001';
            window.fetch = async (input, init) => {
                const path = new URL(String(input), location.href).pathname;
                if (/^\/api\/sessions\/[^/]+\/retry$/.test(path)) {
                    window.__gridRetries.push(path);
                    return new Response('{}', {status: 200, headers: {'Content-Type': 'application/json'}});
                }
                if (path === '/api/state') {
                    const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                    const r = await realFetch(input, {...init, headers}), data = await r.json();
                    const s = data.sessions.find(s => s.id === %s);
                    if (s) s.claude_session = window.__gridConversation;
                    return new Response(JSON.stringify(data), {status: r.status, headers: r.headers});
                }
                return realFetch(input, init);
            };
            window.__gridRestoreFetch = () => { window.fetch = realFetch; };
        })()""" % jq(ended))
        for action in ("Continue", "Retry"):
            if action == "Retry":
                js("window.__gridConversation = null")
            if not ab("wait", "--fn", "[...document.querySelectorAll('#main > header .actions button')].some(b => b.textContent === %s)" % jq(action)).get("success"):
                finding("grid %s / wait" % action, "nav", "no %s in the header: %r, state %r" % (action, js(
                    "JSON.stringify([...document.querySelectorAll('#main > header .actions button')].map(b => b.textContent))"),
                    js("document.querySelector('#main > header').dataset.state")))
            for label, selector in [("header", "#main > header .actions button"),
                                    ("pane", "#main .term[data-sid=%s]:not(.hidden) .tlabel button" % jq(ended))]:
                expect("grid %s / %s" % (action, label), click(selector, action), "exited session must offer %s above the terminal" % action)
            scan("grid %s after exit" % action)
            shot("grid %s after exit" % action)
        expect("grid Continue target", js("window.__gridRetries") == ["/api/sessions/%s/retry" % ended] * 4,
               "both Continue buttons must resume their own session")
        js("window.__gridRestoreFetch()")
        scan("grid Continue after exit")
        shot("grid first opening after exit")
        slot_close()
        expect("grid first opening cancelled", js("!document.querySelector('#main .body.grid')"), "Cancel must return to Single")
        layout_grid()
        expect("grid first Add popover", js("!!document.querySelector(%s)" % jq(ADD_MENU)), "the Add cell's popover did not open by itself")
        expect("grid first Add session", slot_pick(DEMO_TITLE, contains=True), "session choice missing in first Grid")
        expect("grid first Add session", js("document.querySelectorAll('#main .body > .term:not(.hidden)').length === 2 && !document.querySelector('#main .term.slot') && !!document.querySelector('#main .term[data-sid=%s]:not(.hidden)')" % jq(ended)),
               "choosing a session must create two panes and keep the original agent")
        scan("grid first Add session")
        layout_single()
    # F03: the tabs' "Files pane" / "DB pane" toggles are Beside ▾'s Files / Database rows (labels kept for the screens)
    for label, row, selector in [("Files pane", "Files", "#main .filespane:not(.hidden)"), ("DB pane", "Database", "#main .dbpane:not(.hidden)")]:
        if label == "DB pane" and not mods.get("db"):
            continue
        expect(label + " after exit", beside_click(row), "Beside ▾ row %s missing" % row)
        beside(label + " after exit", selector)
        shot(label + " after exit")
        beside_click(row)
        expect(label + " after exit closed", js("!document.querySelector('#main .body.grid') && document.querySelector('#main > header').dataset.sid === %s" % jq(ended)),
               "closing a tool must restore the ended agent alone")
    if CTX["preset"] == "all" and CTX["width"] == min(int(w) for w in A.widths.split(",")):
        # Browser-only incoming state fixtures: exercise no-PTY rendering without mutating real session lifecycle.
        js("""(() => {
            const realFetch = window.fetch.bind(window);
            window.__gridState = 'preflight';
            window.fetch = async (input, init) => {
                if (!String(input).includes('/api/state')) return realFetch(input, init);
                const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                const r = await realFetch(input, {...init, headers});
                const data = await r.json(), s = data.sessions.find(s => s.id === %s);
                if (s) { s.state = window.__gridState; s.pid = null; s.detail = 'Grid state fixture'; s.blocks = []; }
                return new Response(JSON.stringify(data), {status: r.status, headers: r.headers});
            };
        })()""" % jq(ended))
        for status in ("preflight", "blocked", "error", "exited"):
            js("window.__gridState = %s" % jq(status))
            ab("wait", "--fn", "!!document.querySelector('#main > header[data-state=%s]')" % status)
            layout_single()
            layout_grid()
            beside("grid no PTY / " + status, "#main .term.slot")
            expect("grid status readable / " + status, js("""(() => {
                const s = document.querySelector('#main .sessionpane:not(.hidden) .sessionstatus');
                const label = s?.querySelector('h2, .endbar, [role=status]');
                if (!label) return false;
                const p = s.getBoundingClientRect(), r = label.getBoundingClientRect();
                return r.height > 10 && r.y >= p.y && r.bottom <= p.bottom && r.x >= p.x && r.right <= p.right;
            })()"""), "session status / start controls must remain readable inside the left pane")
            if status == "preflight":
                slot_pick(DEMO_TITLE, contains=True)
                ab("focus", "#main .term[data-sid=%s] .xterm-helper-textarea" % jq(DEMO_SID))
                expect("grid focus live partner", js("document.querySelector('#main > header').dataset.sid") == DEMO_SID,
                       "focus in the live terminal must activate its session")
                ab("focus", "#main .sessionpane[data-sid=%s]" % jq(ended))
                expect("grid focus status pane", js("document.querySelector('#main > header').dataset.sid") == ended,
                       "focus in the status pane must return to its own session")
                scan("grid focus status pane")
                layout_single()
            else:
                slot_close()
            for label, row, selector in [("Files pane", "Files", "#main .filespane:not(.hidden)"), ("DB pane", "Database", "#main .dbpane:not(.hidden)")]:
                beside_click(row)
                beside(label + " no PTY / " + status, selector)
                shot(label + " no PTY / " + status)
                beside_click(row)
    js("localStorage.setItem('piper.gridlayout', 'auto')")
    reload()
    settle()
    palette_run(DEMO_TITLE)
    click("nav.tabs button", "Terminal")
    cells = "[...document.querySelectorAll('#main .body > .term')].filter(e => !e.classList.contains('hidden') && e.getBoundingClientRect().width > 0 && !e.classList.contains('slot'))"

    def check(label, count, terminals=None):
        settle(0.1)
        counts = js("(() => { const panes = %s; return {all: panes.length, terminals: panes.filter(e => !!e.querySelector('.xterm')).length}; })()" % cells)
        actual = counts["all"]
        expect(label, actual == count, "expected %d visible panes, got %s" % (count, actual))
        if terminals is not None:
            expect(label, counts["terminals"] == terminals,
                   "expected %d terminals" % terminals)
        scan(label)

    def single():
        if mods.get("grid"):
            layout_single()

    def split() -> bool:   # ⌘\ / Alt+\ - the toolbar's Split button is gone (2026-10-15 toolbar cleanup)
        return ab("press", "Meta+Backslash" if js("/mac|iphone|ipad|ipod/i.test(navigator.platform)") else "Alt+Backslash").get("success", False)

    def tool(label, pane):
        if mods.get("grid"):
            single()
            expect(label, split(), "the split key did not reach the page")
            check(label + " / split", 2, 2)
            beside_layout("Rows")
            single()  # remembered two-terminal grid is the original bug trigger
        expect(label, pane(), "tool did not open")
        check(label + " / Single + tool", 2, 1)
        beside(label + " / right column over remembered Rows", {"grid Files": "#main .filespane:not(.hidden)", "grid DB": "#main .dbpane:not(.hidden)", "grid HTTP": "#main .httppane:not(.hidden)"}[label], DEMO_SID)
        if label == "grid Files":
            if not click("#main .filespane .tnode", "README.md"):
                click("#main .filespane button", "tree")
                click("#main .filespane .tnode", "README.md")
            expect("grid Files editor width", js("(() => { const f=document.querySelector('#main .filespane .files'); const r=f.querySelector('.fright'); return !f.classList.contains('tree-off') || r.getBoundingClientRect().width >= f.getBoundingClientRect().width - 3; })()"),
                   "hidden tree left the editor in a narrow column")
            scan("grid Files open file")
        expect(label, pane(), "tool did not close")
        check(label + " / tool closed", 1, 1)
        expect(label, js("!document.querySelector('#main .body.grid') && !document.querySelector('#main .gsplit')"),
               "closing the tool left a grid / splitter over Single")

    tool("grid Files", lambda: beside_click("Files"))
    if mods.get("db"):
        tool("grid DB", lambda: beside_click("Database"))
    if mods.get("http"):
        tool("grid HTTP", lambda: palette_run("HTTP pane: close") or palette_run("HTTP pane beside the session (this project's .http requests)"))
    # Beside is one full-height tool slot, including Basic (without the grid module).
    expect("beside switching / Files", beside_click("Files"), "Files missing")
    js("window.__besideFiles = document.querySelector('#main .filespane > .files')")
    for row, selector in [("Database", ".dbpane"), ("HTTP", ".httppane"), ("Files", ".filespane")]:
        if row == "Database" and not mods.get("db") or row == "HTTP" and not mods.get("http"):
            continue
        if row == "Files" and not (mods.get("db") or mods.get("http")):
            continue
        expect("beside switching / " + row, beside_click(row), "tool missing")
        check("beside switching / " + row, 2, 1)
        beside("beside switching / " + row, "#main " + selector + ":not(.hidden)", DEMO_SID)
        shot("beside switching / " + row)
    expect("beside switching / Files retained", js("window.__besideFiles === document.querySelector('#main .filespane > .files')"),
           "switching tools must retain the Files instance and its editor state")
    if mods.get("grid") and mods.get("db"):
        add_pane()
        expect("explicit tools / add Database", slot_pick("Database · this project"), "project Database missing in Add")
        check("explicit tools / three panes", 3, 1)
        expect("explicit tools / Rows", beside_layout("Rows"), "explicit tool grid must offer Rows")
        expect("explicit tools / Rows shape", js("""(() => {
            const r = %s.map(e => e.getBoundingClientRect()).sort((a,b)=>a.y-b.y);
            return r.length === 3 && r.every(p=>Math.abs(p.x-r[0].x)<2 && Math.abs(p.width-r[0].width)<2)
                && r.every((p,i)=>!i || r[i-1].bottom<=p.y+1)
                && r.every(p=>p.y>=0 && p.bottom<=innerHeight);
        })()""" % cells), "tools ignored Rows or escaped the viewport")
        scan("explicit tools / Rows")
        shot("explicit tools / Rows")
        if CTX["width"] >= 1920:
            expect("explicit tools / Columns", beside_layout("Columns"), "wide explicit tool grid must offer Columns")
            expect("explicit tools / Columns shape", js("""(() => {
                const r = %s.map(e => e.getBoundingClientRect()).sort((a,b)=>a.x-b.x);
                return r.length === 3 && r.every(p=>Math.abs(p.y-r[0].y)<2 && Math.abs(p.height-r[0].height)<2)
                    && r.every((p,i)=>!i || r[i-1].right<=p.x+1)
                    && r.every(p=>p.x>=0 && p.right<=innerWidth);
            })()""" % cells), "tools ignored Columns or escaped the viewport")
            scan("explicit tools / Columns")
            shot("explicit tools / Columns")
        # Return to a single tool through Beside (including old stacked layouts).
        if mods.get("http"):
            beside_click("HTTP")
            check("beside switching / old stack", 2, 1)
            beside_click("HTTP")
        else:
            beside_click("Database")
            beside_click("Files")
    else:
        beside_click("Files")
    if not mods.get("grid"):
        return
    single()
    palette_run(DEMO_TITLE)
    split()
    check("grid before removing idle session", 2, 2)
    click("#main .term[data-sid=%s] .tlabel button" % jq(DEMO_SID), "remove", "data-act")
    single()
    palette_run(DEMO_TITLE)
    check("removed session reopened in Single", 1, 1)
    expect("removed session fills Single", js("""(() => {
        const body = document.querySelector('#main .body');
        const pane = body.querySelector('.term:not(.hidden)');
        const style = getComputedStyle(body);
        const width = body.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
        return !body.classList.contains('grid') && !pane.classList.contains('gcell') &&
            !pane.style.width && Math.abs(pane.getBoundingClientRect().width - width) < 2;
    })()"""), "a removed terminal kept its old grid width after reopening in Single")
    shot("removed session reopened in Single")
    split()
    beside_layout("Auto")
    single()
    # F03: Beside ▾ in Single restores the grid (⌘G); an Add cell beside Single is the palette's "Grid: add a pane…"
    expect("grid Add from Single", add_pane(), "Grid: add a pane… missing")
    check("grid Add from Single", 1, 1)
    expect("grid Add from Single", js("document.querySelectorAll('#main .term.slot').length === 1"), "Add cell missing")
    slot_close()
    check("grid Add cancelled", 1, 1)
    expect("grid Add cancelled", js("!document.querySelector('#main .body.grid') && !document.querySelector('#main .gsplit')"),
           "cancelling Add left a grid / splitter")
    grid(True)
    check("grid Add cancelled / remembered grid", 2, 2)
    single()
    split()
    check("grid keyboard split", 2, 2)
    # Arrow order is CSS order; the terminal DOM keeps the order of first mount.
    names = "JSON.stringify(%s.sort((a,b) => Number(a.style.order) - Number(b.style.order)).map(e => e.querySelector('.tlabel .name')?.textContent))" % cells
    before = js(names)
    # F14 (9.78): ▶ is the pane menu's Move right (of the left pane)
    js("(() => { document.querySelectorAll('[data-e2e-left]').forEach(e => delete e.dataset.e2eLeft); "
       "%s.sort((a,b) => Number(a.style.order) - Number(b.style.order))[0].dataset.e2eLeft = '1'; })()" % cells)
    expect("grid reorder", pane_menu("#main .term[data-e2e-left]", "Move right"), "the pane menu offers no Move right")
    expect("grid reorder", js(names) == list(reversed(before)), "arrow did not swap the two panes")
    scan("grid reorder")
    ab("drag", "#main .term.focused .tlabel .name", "#main .term:not(.focused):not(.hidden) .tlabel .name")
    expect("grid title drag", js(names) == before, "dragging the title did not swap the panes back")
    scan("grid title drag")
    # Real pointer events: drag a splitter, then double-click to reset it.
    bar = js("(() => { const r = document.querySelector('#main .gsplit.v').getBoundingClientRect(); return {x:r.x+r.width/2, y:r.y+r.height/2}; })()")
    ab("mouse", "move", str(round(bar["x"])), str(round(bar["y"])))
    ab("mouse", "down")
    ab("mouse", "move", str(round(bar["x"] + 60)), str(round(bar["y"])), "--steps", "8")
    ab("mouse", "up")
    after = js("document.querySelector('#main .gsplit.v').getBoundingClientRect().x + 7")
    expect("grid resize", abs(after - bar["x"] - 60) < 3, "drag did not resize the split")
    ab("dblclick", "#main .gsplit.v")
    reset = js("(() => { const b=document.querySelector('#main .body').getBoundingClientRect(); const r=document.querySelector('#main .gsplit.v').getBoundingClientRect(); return Math.abs(r.x+7-b.x-b.width/2)<2; })()")
    expect("grid reset split", reset, "double-click did not centre the split")
    # Session zoom and explicit Grid return keep the original pick.
    click("#main .term:not(.hidden) .tlabel button", "zoom", "data-act")
    check("grid session zoom", 1, 1)
    grid(True)
    check("grid session unzoom", 2, 2)
    palette_run(DEMO_TITLE)  # title dragging / zoom may focus the other (non-project) terminal
    expect("grid existing split + Files", beside_click("Files"), "Beside ▾ Files missing for the demo project")
    expect("grid existing split + Files / checked", (beside_item("Files") or {}).get("checked"), "Beside ▾ Files is not ticked while the pane is shown")
    check("grid existing split + Files", 3, 2)
    click("#main .filespane .tlabel button", "zoom", "data-act")
    check("grid Files zoom", 1, 0)
    click("#main .filespane .tlabel button", "unzoom", "data-act")
    check("grid Files unzoom", 3, 2)
    click("#main .filespane .tlabel button", "remove", "data-act")
    check("grid Files remove", 2, 2)
    expect("grid Files remove", js("document.querySelector('#main > header').dataset.sid") == DEMO_SID,
           "closing Files switched to a neighbouring session instead of the project terminal")
    # Multi-agent tools are ordinary cells: preserve Rows / Columns, insert after focus,
    # allow reordering, and retain both agents across zoom and close.
    for preset in ("Rows", "Columns"):
        for label, row, selector in [("Files pane", "Files", ".filespane"), ("DB pane", "Database", ".dbpane"), ("HTTP pane", "HTTP", ".httppane")]:
            if label == "DB pane" and not mods.get("db") or label == "HTTP pane" and not mods.get("http"):
                continue
            palette_run(DEMO_TITLE)
            ordered = "[...document.querySelectorAll('#main .body > .term:not(.hidden)')].sort((a,b)=>Number(a.style.order)-Number(b.style.order))"
            agents = js("%s.map(p=>p.dataset.sid)" % ordered)
            # 9.135: two agents offer one card per shape - Columns is Auto's two columns there, so it may be absent;
            # the pick below, with the tool in, is the one the shape check needs
            beside_layout(preset) or beside_layout("Auto")
            if label == "HTTP pane":
                expect("grid multi-agent HTTP open", palette_run("HTTP pane beside the session (this project's .http requests)"), "HTTP pane did not open")
            else:
                expect("grid multi-agent tool open", beside_click(row), "project tool did not open")
            check("grid multi-agent " + preset + " / " + label, 3, 2)
            fits = beside_layout(preset)
            screen = "grid multi-agent " + preset + " / " + label
            expect(screen + " / insertion", js("""(() => {
                const panes = %s, a = panes.findIndex(p=>p.dataset.sid === %s);
                return panes[a+1]?.matches(%s) && panes.filter(p=>p.dataset.sid).map(p=>p.dataset.sid).join() === %s;
            })()""" % (ordered, jq(DEMO_SID), jq(selector), jq(",".join(agents)))), "tool must join after focused agent without changing the agents' order")
            if fits:
                expect(screen + " / shape", js("""(() => {
                    const r = %s.map(p=>p.getBoundingClientRect());
                    return %s ? r.every(p=>Math.abs(p.x-r[0].x)<2 && Math.abs(p.width-r[0].width)<2)
                        && r.every((p,i)=>!i || r[i-1].bottom<=p.y+1)
                        : r.every(p=>Math.abs(p.y-r[0].y)<2 && Math.abs(p.height-r[0].height)<2)
                        && r.every((p,i)=>!i || r[i-1].right<=p.x+1);
                })()""" % (ordered, "true" if preset == "Rows" else "false")), "project tool overrode the selected multi-agent layout")
            expect(screen + " / movable", js("document.querySelector('#main %s .tlabel').draggable" % selector), "ordinary grid tool must allow title reordering")
            scan(screen)
            shot(screen)
            click("#main %s .tlabel button" % selector, "zoom", "data-act")
            check(screen + " / zoom", 1, 0)
            click("#main %s .tlabel button" % selector, "unzoom", "data-act")
            check(screen + " / unzoom", 3, 2)
            click("#main %s .tlabel button" % selector, "remove", "data-act")
            check(screen + " / close", 2, 2)
            expect(screen + " / agents", js("%s.map(p=>p.dataset.sid)" % ordered) == agents, "closing tool changed the agent pick")
            expect(screen + " / preset", js("localStorage.getItem('piper.gridlayout')") == preset.lower(), "zoom or closing tool changed the selected preset")
    # The Add cell lists tools too; exercise every available pane kind and every layout without losing panes.
    if mods.get("db"):
        beside_click("Database")
        for _ in range(3):
            add_pane()
            expect("grid independent databases", slot_pick("+ Database pane"), "project DB consumed an independent DB slot")
    if mods.get("http"):
        add_pane()
        slot_pick("HTTP · ")
    beside_click("Files")
    n = 3 + (4 if mods.get("db") else 0) + (1 if mods.get("http") else 0)
    check("grid all tool panes", n, 2)
    host_id = ""
    if mods.get("host"):
        add_pane()
        expect("grid host", slot_pick("+ New host shell", contains=True), "host shell did not start")
        ab("wait", "#main .hostpane:not(.hidden)")
        settle()
        host_id = next((h["id"] for h in api("/api/state").get("host_terms", {}).get("items", []) if h.get("alive")), "")
        n += 1
        check("grid nine panes with host", n, 3)
        # F03: Split / + Add pane are Beside ▾'s Another session… and Host shell rows (Split is only ⌘\ now)
        full = {r["label"]: r["disabled"] for r in beside_items()}
        expect("grid full", full.get(ADD_ITEM) is True and full.get("Host shell") is True,
               "full grid still offers Another session… / Host shell: %r" % full)
        # A full remembered grid must not silently switch away from a different Single session.
        single()
        palette_run("demo/plain")
        layout_grid()
        expect("grid full restore preserves current", js("!document.querySelector('#main .body.grid') && document.querySelector('#main > header').dataset.sid === %s" % jq(ended)),
               "full restore silently replaced the current session")
        expect("grid full restore explanation", js("document.body.textContent.includes('Remove a pane before adding this session.')"), "capacity refusal must explain how to continue")
        palette_run(DEMO_TITLE)
        layout_grid()
        check("grid full restore existing member", n, 3)
        # On a different tab, Add capacity is the editable grid, not the smaller restore preview.
        other_id = js("%s.find(p=>p.dataset.sid && p.dataset.sid !== %s).dataset.sid" % (cells, jq(DEMO_SID)))
        js("""(() => {
            const realFetch = window.fetch.bind(window);
            window.__capacityEnded = true; window.__capacitySeen = false;
            window.fetch = async (input, init) => {
                if (!String(input).includes('/api/state')) return realFetch(input, init);
                const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                const r = await realFetch(input, {...init, headers}), data = await r.json();
                const s = data.sessions.find(s => s.id === %s);
                if (s && window.__capacityEnded) s.state = 'exited';
                window.__capacitySeen = true;
                return new Response(JSON.stringify(data), {status: r.status, headers: r.headers});
            };
            window.__capacityRestore = () => { window.fetch = realFetch; };
        })()""" % jq(other_id))
        ab("wait", "--fn", "window.__capacitySeen")
        click("nav.tabs button", "Files")
        expect("grid full Add from Files disabled", (beside_item(GRID_ITEM, start=True) or {}).get("disabled") is True,
               "Another session… became enabled because restore preview excluded an ended pane")
        click("nav.tabs button", "Terminal")
        js("window.__capacityEnded = false; window.__capacitySeen = false")
        ab("wait", "--fn", "window.__capacitySeen")
        js("window.__capacityRestore()")
        click("#main .hostpane .tlabel button", "zoom", "data-act")
        check("grid host zoom", 1, 1)
        click("#main .hostpane .tlabel button", "unzoom", "data-act")
        check("grid host unzoom", n, 3)
    for label in ("Auto", "Columns", "Rows", "1 + rest", "Big top", "2 × 2", "2 × 3", "3 × 3", "3 visible"):
        beside_layout(label, start=True)
        expect("grid preset " + label, js("%s.length" % cells) == n, "preset hid a pane")
        scan("grid preset " + label)
    # 9.135: every Layout card Beside ▾ offers is another arrangement - a click on one always changes the screen
    offered = layout_cards()
    seen = {}
    for label in offered:
        beside_layout(label)
        seen.setdefault(js("%s.map(p => { const r = p.getBoundingClientRect(); return [p.dataset.sid || p.className, Math.round(r.x), Math.round(r.y), Math.round(r.width)].join(); }).sort().join(' | ')" % cells), []).append(label)
    expect("grid preset cards distinct", len(offered) > 1 and len(seen) == len(offered),
           "Layout cards with the same arrangement: %s" % [v for v in seen.values() if len(v) > 1])
    beside_layout("Auto")
    # Queue focus must keep pane count; tools follow the newly active project.
    palette_run("demo/hidden")
    expect("grid queue focus", js("%s.length" % cells) == n, "focusing another session changed grid size")
    scan("grid queue focus")
    palette_run(DEMO_TITLE)
    if host_id:
        click("#main .hostpane .tlabel button", "remove", "data-act")
        check("grid host remove", n - 1, 2)
        expect("grid host remove", any(h["id"] == host_id and h.get("alive") for h in api("/api/state")["host_terms"]["items"]),
               "removing a host pane stopped the shell")
        api("/api/host-terminals/%s/close" % host_id, {})  # only the shell this isolated fixture started
    single()
    add_pane()
    slot_close()  # leave a clean Single for the rest of the walk
    check("grid clean Single", 1, 1)
    grid_choose_ux(mods)



def grid_choose_ux(mods: dict) -> None:
    """Choose panes applies exactly the draft, without reopening hidden sessions or changing focus on open."""
    if not mods.get("grid"):
        return
    composer_was_on = mods.get("composer", False)
    if not composer_was_on:
        api("/api/features", {"modules": {"composer": True}})
        reload()
        settle()
    mods = dict(mods, composer=True)  # Composer is opt-in, even in All; exercise its grid context too.
    sid = api("/api/sessions", {"project": "demo/http", "title": "Grid chooser third project"})["id"]
    try:
        end = time.time() + 30
        while time.time() < end:
            third = next(x for x in api("/api/state")["sessions"] if x["id"] == sid)
            if third["state"] == "blocked":
                api("/api/sessions/%s/approve" % sid, {})
            if third["state"] in ("ready", "waiting"):
                break
            time.sleep(0.2)
        expect("chooser third project", third["state"] in ("ready", "waiting"), "third project did not start")
        sessions = api("/api/state")["sessions"]
        other = next(x["id"] for x in sessions if x["project"] == "demo/hidden")
        ended = next(x["id"] for x in sessions if x["project"] == "demo/plain")
        palette_run(DEMO_TITLE)
        layout_single()
        cells = "[...document.querySelectorAll('#main .body > .term:not(.hidden):not(.slot)')].sort((a,b)=>Number(a.style.order)-Number(b.style.order))"
        def shown():
            return js("%s.map(e=>e.dataset.sid || (e.classList.contains('filespane') ? 'files' : 'tool'))" % cells)
        def current():
            ab("wait", "--fn", "!!document.querySelector('#main > header')")   # a reload can catch #main between renders
            return js("document.querySelector('#main > header').dataset.sid")
        def open_chooser(menu=False):
            before, panes = current(), shown()
            if menu:
                expect("chooser menu open", ab("click", ".layoutctl button.beside").get("success"), "Beside ▾ did not open")
                expect("chooser menu", ab("click", BESIDE_MENU + " .lfoot button:first-child").get("success"), "Choose panes missing from Beside ▾")
            else:
                expect("chooser palette", palette_run("Grid: choose panes…"), "Choose panes command missing")
            expect("chooser open focus", current() == before and shown() == panes, "opening chooser changed session or panes")
            expect("chooser excludes ended", js("window.__pastGridLive || !document.querySelector('#overlay input[data-id=%s]')" % jq(ended)),
                   "ended sessions must not be offered in Choose panes")
        def draft(ids):
            available = js("[...document.querySelectorAll('#overlay input[data-id]')].map(e=>e.dataset.id)")
            expect("chooser available panes", all(x in available for x in ids), "a selected session is missing from the chooser")
            # Each change redraws the list. Resolve a fresh checkbox for every user action.
            for ident in available:
                selector = '#overlay input[data-id="%s"]' % ident
                checked = js("document.querySelector(%s).checked" % jq(selector))
                if checked != (ident in ids):
                    ab("scrollintoview", selector)
                    # Toggle by pointer: an unchecked draft-only DB panel removes its row, so
                    # check/uncheck's post-action element lookup cannot verify it. The final
                    # draft assertion below verifies the resulting selection after every redraw.
                    result = ab("click", selector)
                    expect("chooser checkbox reachable", result.get("success"), result.get("error") or "checkbox pointer action failed")
            expect("chooser draft", sorted(js("[...document.querySelectorAll('#overlay input:checked')].map(e=>e.dataset.id)")) == sorted(ids), "checkbox draft differs from requested panes")
        def apply(ids, menu=False):
            open_chooser(menu)
            before = current()
            draft(ids)
            expect("chooser editing focus", current() == before, "editing checkboxes changed the active session")
            expect("chooser Show", ab("click", "#overlay button.primary").get("success"), "Show did not respond to a pointer click")
            settle()
            label = "chooser exact %d panes" % len(ids)
            state = js("""({
                shown: %s.map(e=>e.dataset.sid || (e.classList.contains('filespane') ? 'files' : 'tool')),
                noAdd: !document.querySelector('#main .slot:not(.hidden)'),
                active: document.querySelector('#main > header').dataset.sid,
                saved: JSON.parse(localStorage.getItem('piper.grid')),
                focus: document.activeElement?.closest('#main .term')?.dataset.sid
            })""" % cells)
            expect(label, sorted(state["shown"]) == sorted(ids), "Show must display exactly the checked panes; got %r, wanted %r" % (state["shown"], ids))
            expect(label + " / no Add", state["noAdd"], "Show unexpectedly opened an Add cell")
            chosen_sessions = [x for x in ids if any(v["id"] == x for v in sessions)]
            expect(label + " / active", state["active"] == before if before in ids or not chosen_sessions else state["active"] in chosen_sessions,
                   "active session must stay when selected, otherwise move to a selected session")
            expect(label + " / saved", sorted(state["saved"]) == sorted(ids), "saved pick differs from the chosen panes")
            if chosen_sessions:
                expect(label + " / keyboard focus", state.get("focus") == state["active"],
                       "Show must focus the selected active terminal so typing cannot stay in the old session")
            scan(label)
            shot(label)
        def past_grid(ids):
            # Model a pane selected while running and ending later; ended sessions are no longer selectable.
            js("""(() => {
                const real = window.fetch.bind(window);
                window.__pastGridPolls = 0; window.__pastGridLive = true;
                window.fetch = async (input, init) => {
                    if (!String(input).includes('/api/state')) return real(input, init);
                    const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                    const r = await real(input, {...init, headers}), data = await r.json();
                    const s = data.sessions.find(s => s.id === %s);
                    if (s && window.__pastGridLive) s.state = 'ready';
                    window.__pastGridPolls++;
                    return new Response(JSON.stringify(data), {status:r.status, headers:r.headers});
                };
                window.__pastGridRestore = () => { window.fetch = real; };
            })()""" % jq(ended))
            ab("wait", "--fn", "window.__pastGridPolls >= 2")
            apply(ids)
            js("window.__pastGridLive = false; window.__pastGridPolls = 0")
            ab("wait", "--fn", "window.__pastGridPolls >= 2")
            js("window.__pastGridRestore()")

        def follow_focus(ids):
            composer_target = js("JSON.parse(sessionStorage.getItem('piper.composer.draft')).to") if mods.get("composer") else ""
            inspector_was_open = js("document.querySelector('[aria-label=\"context panel\"]').getAttribute('aria-pressed') === 'true'")
            if not inspector_was_open:
                ab("click", "[aria-label='context panel']")
            beside_click("Files")
            pick = js("localStorage.getItem('piper.grid')")
            projects = {s["id"]: s["project"] for s in sessions}
            for ident in ids + list(reversed(ids)):
                selector = '#main .term[data-sid="%s"] .xterm-screen' % ident
                expect("chooser focus pointer", ab("click", selector).get("success"), "terminal click failed")
                settle()
                expect("chooser context header", current() == ident, "header did not follow the focused session")
                expect("chooser context inspector", js("document.querySelector('#inspector').dataset.session") == ident,
                       "visible inspector did not follow the focused session")
                expect("chooser context Files", projects[ident] in js("document.querySelector('#main .filespane .tlabel').textContent"),
                       "Files did not follow the focused project")
                expect("chooser context grid", js("localStorage.getItem('piper.grid')") == pick, "focusing a session changed the grid pick")
                if mods.get("composer"):
                    expect("chooser context composer", js("document.querySelector('.ctarget-label').classList.contains('warn')") == (ident != composer_target),
                           "the original draft target must stay, with a warning when another session is active")
                    expect("chooser focus keeps draft", js("JSON.parse(sessionStorage.getItem('piper.composer.draft')).to") == composer_target,
                           "terminal focus silently retargeted the draft")
                scan("chooser context follows focus")
            shot("chooser context follows focus")
            click("#main .filespane .tlabel button", "remove", "data-act")
            if not inspector_was_open:
                ab("click", "[aria-label='context panel']")
        # Restoring a remembered grid must drop its ended session and count the active newcomer.
        past_grid([other, ended])
        layout_single()
        palette_run(DEMO_TITLE)
        expect("restore grid preview", (beside_item(GRID_ITEM, start=True) or {}).get("label") == GRID_ITEM + " 2",
               "Grid preview must include the current session and exclude the ended one")
        layout_grid()
        expect("restore grid without ended", sorted(shown()) == sorted([other, DEMO_SID]),
               "restoring Grid 2 resurrected the ended session or omitted the current one")
        scan("restore grid without ended")
        shot("restore grid without ended")
        # Exact reported lifecycle: old remembered pair, a finished session, current one still starting.
        js("""(() => {
            const realFetch = window.fetch.bind(window);
            window.__restoreStage = 'starting';
            window.fetch = async (input, init) => {
                if (!String(input).includes('/api/state')) return realFetch(input, init);
                const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                const r = await realFetch(input, {...init, headers}), data = await r.json();
                const s = data.sessions.find(s => s.id === %s);
                if (s && window.__restoreStage) {
                    s.state = window.__restoreStage; s.pid = null; s.phase = 'hooks';
                    s.detail = 'Grid lifecycle fixture'; s.blocks = [];
                }
                return new Response(JSON.stringify(data), {status: r.status, headers: r.headers});
            };
            window.__restoreRealFetch = () => { window.fetch = realFetch; };
        })()""" % jq(DEMO_SID))
        for stage, entry in [("starting", "button"), ("preflight", "palette"), ("blocked", "keyboard")]:
            js("window.__restoreStage = %s" % jq(stage))
            past_grid([other, ended])
            layout_single()
            palette_run(DEMO_TITLE)
            ab("wait", "--fn", "!!document.querySelector('#main > header[data-state=%s]')" % stage)
            expect("restore lifecycle preview / " + entry, (beside_item(GRID_ITEM, start=True) or {}).get("label") == GRID_ITEM + " 2", "preview must count the starting session")
            if entry == "button":
                layout_grid()
            elif entry == "palette":
                expect("restore lifecycle palette", palette_run("Grid: its 2 panes"), "palette restore count differs from button")
            else:
                js("document.body.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', code:'Enter', shiftKey:true, metaKey:/mac|iphone|ipad|ipod/i.test(navigator.platform), altKey:!/mac|iphone|ipad|ipod/i.test(navigator.platform), bubbles:true, cancelable:true}))")
                settle()
            expect("restore lifecycle panes / " + entry, sorted(shown()) == sorted([other, DEMO_SID]), "restore must keep the current no-PTY session and exclude the ended one")
            expect("restore lifecycle active / " + entry, current() == DEMO_SID, "restore switched away from the current session")
            expect("restore lifecycle status / " + entry, js("!!document.querySelector('#main .sessionpane[data-sid=%s]:not(.hidden)')" % jq(DEMO_SID)), "the starting session must occupy its own pane")
            scan("restore lifecycle / " + entry)
            shot("restore lifecycle / " + entry)
        js("window.__restoreRealFetch()")
        ab("wait", "--fn", "!document.querySelector('#main > header[data-state=blocked]')")
        expect("restore lifecycle PTY attaches", sorted(shown()) == sorted([other, DEMO_SID]), "PTY attachment changed pane membership")
        reload()
        settle()
        expect("restore lifecycle reload pick", sorted(js("JSON.parse(localStorage.getItem('piper.grid'))")) == sorted([other, DEMO_SID]), "reload resurrected the ended pane")
        palette_run(DEMO_TITLE)
        if not js("!!document.querySelector('#main .body.grid')"):
            layout_grid()
        expect("restore lifecycle reload panes", sorted(shown()) == sorted([other, DEMO_SID]), "restoring after reload changed the selected pair")
        layout_single()
        reload()
        settle()
        expect("restore lifecycle reload Single", js("!document.querySelector('#main .body.grid')"), "reload lost the explicit Single choice")
        layout_grid()
        expect("restore lifecycle Single reload pair", sorted(shown()) == sorted([other, DEMO_SID]), "Single reload resurrected a stale session")
        shot("restore lifecycle after reload")
        apply([DEMO_SID, other, sid])
        # Cancel and Escape discard edits; opening from a pane menu must also preserve the active session.
        for escape in (False, True):
            before, panes = current(), shown()
            open_chooser(menu=True)
            draft([other])
            if escape:
                ab("press", "Escape")
            else:
                click("#overlay button", "Cancel")
            expect("chooser cancel", current() == before and shown() == panes, "Cancel / Escape changed the session or grid")
        apply([DEMO_SID], menu=True)
        expect("chooser one is Single", js("!document.querySelector('#main .body.grid')"), "one chosen session must be Single")
        if mods.get("composer"):
            palette_run("Composer")
            ab("fill", "#main .composer textarea", "Grid context draft")
        apply([other, sid])  # neither is the previously active session
        if mods.get("composer"):
            expect("chooser draft target", js("document.querySelector('.ctarget-label').classList.contains('warn')"),
                   "Show switched sessions but did not warn that the draft still belongs to the original session")
            expect("chooser keeps draft", js("JSON.parse(sessionStorage.getItem('piper.composer.draft')).to") == DEMO_SID,
                   "changing focus must not retarget an existing draft")
            ab("fill", "#main .composer textarea", "")
            ab("fill", "#main .composer textarea", "Grid focus draft")  # now belongs to one of the visible terminals
        follow_focus([other, sid])
        if mods.get("composer"):
            ab("fill", "#main .composer textarea", "")
        apply([DEMO_SID])
        if mods.get("composer"):
            expect("chooser empty composer target", DEMO_TITLE in js("document.querySelector('.ctarget-label').textContent")
                   and not js("document.querySelector('.ctarget-label').classList.contains('warn')"), "empty composer did not follow Show")
            ab("focus", "#main .composer textarea")
            ab("press", "Escape")
        apply([DEMO_SID, other, sid])
        apply([other], menu=True)  # a different single session, from a three-project grid
        apply([DEMO_SID, other])
        past_grid([DEMO_SID, ended])
        open_chooser(menu=True)
        expect("chooser drops ended draft", js("[...document.querySelectorAll('#overlay input:checked')].map(e=>e.dataset.id)") == [DEMO_SID],
               "ended saved picks must not count towards Show or the pane limit")
        # Long real-world titles/paths plus a non-overlay scrollbar must stay inside the list.
        js("""(() => {
            const list = document.querySelector('#overlay .gridchooser');
            const name = list.querySelector('.pitem .name');
            name.lastChild.textContent = ' #4821 Split the ordering API into a frontend / backend part and implement a very long name';
            const notes = [...list.querySelectorAll('.pitem > .mut')];
            notes[0].textContent = 'waiting for your approval';
            notes.at(-1).textContent = '/Users/developer/projects/a-very-long-project-directory/checkout';
        })()""")
        expect("chooser columns contained", js("""(() => {
            const list = document.querySelector('#overlay .gridchooser'), box = list.getBoundingClientRect();
            return list.scrollWidth <= list.clientWidth + 1 && [...list.querySelectorAll('.pitem')].every(row => {
                const r = row.getBoundingClientRect(), cb = row.querySelector('input').getBoundingClientRect();
                const note = row.querySelector(':scope > .mut'), n = note.getBoundingClientRect();
                return r.left >= box.left && r.right <= box.left + list.clientWidth + 1 &&
                    cb.width > 8 && cb.left >= r.left && n.width > 40 && n.right <= r.right + 1 &&
                    (note.scrollWidth <= note.clientWidth + 1 || getComputedStyle(note).textOverflow === 'ellipsis');
            });
        })()"""), "chooser columns or checkboxes extend beyond the scrollport")
        scan("chooser long labels")
        shot("chooser without ended sessions")
        ab("click", "#overlay button.primary")
        settle()
        expect("chooser Show drops ended", shown() == [DEMO_SID], "Show retained an invisible ended pick")
        apply(["files"])  # a tool alone must remain selected without adding its agent back
        apply([DEMO_SID])
        # Empty draft is not a hidden "show all": keep Show disabled until a pane is selected.
        open_chooser()
        draft([])
        expect("chooser empty", js("[...document.querySelectorAll('#overlay button')].find(b=>b.textContent==='Show').disabled"), "empty selection must not apply an unrelated grid")
        click("#overlay button", "Cancel")
        expect("chooser empty cancelled", shown() == [DEMO_SID], "cancelling empty draft changed the workspace")
        reload()
        settle()
        expect("chooser remembered exact pick", js("JSON.parse(localStorage.getItem('piper.grid'))") == [DEMO_SID], "reload resurrected unselected panes")
        # Browser-only incoming state fixture: a blocked session can be chosen before a PTY exists.
        js("""(() => {
            const realFetch = window.fetch.bind(window);
            window.__chooserBlocked = false; window.__chooserPolls = 0;
            window.fetch = async (input, init) => {
                if (!String(input).includes('/api/state')) return realFetch(input, init);
                const headers = new Headers(init?.headers); headers.delete('If-None-Match');
                const r = await realFetch(input, {...init, headers});
                const data = await r.json(), s = data.sessions.find(s => s.id === %s);
                if (s) { s.state = 'blocked'; s.pid = null; s.detail = 'Chooser focus fixture'; s.blocks = []; }
                window.__chooserBlocked = true; window.__chooserPolls++;
                return new Response(JSON.stringify(data), {status: r.status, headers: r.headers});
            };
        })()""" % jq(other))
        ab("wait", "--fn", "window.__chooserBlocked")
        open_chooser()
        draft([other])
        ab("click", "#overlay button.primary")
        settle()
        expect("chooser blocked Single focus", js("!!document.activeElement?.closest('#main .body')"),
               "a chosen session without a PTY must receive keyboard focus in its status panel")
        js("window.__chooserPolls = 0")
        ab("wait", "--fn", "window.__chooserPolls >= 2")
        expect("chooser blocked Single focus after polling", js("!!document.activeElement?.closest('#main .body')"),
               "polling replaced the focused status panel and lost keyboard focus")
        scan("chooser blocked Single")
        apply([other, DEMO_SID])
        expect("chooser blocked grid focus", js("document.activeElement?.closest('#main .sessionpane')?.dataset.sid") == other,
               "a chosen grid session without a PTY must focus its own status panel")
        shot("chooser blocked grid")
        apply([DEMO_SID])
        reload()  # remove only the incoming-state fixture
        settle()
        palette_run(DEMO_TITLE)
        layout_single()
    finally:
        api("/api/sessions/%s/stop" % sid, {})
        if not composer_was_on:
            api("/api/features", {"modules": {"composer": False}})
            reload()
            settle()


def prodcheck_session() -> bool:
    """the morning check's session (fake claude: it stays at the prompt); started by the first call"""
    has = lambda: any(x.get("kind") == "prodcheck" for x in api("/api/state")["sessions"])  # noqa: E731
    if not has():
        try:
            api("/api/prod/run", {"mode": "check"})
        except urllib.error.HTTPError:
            return False
        end = time.time() + 30
        while time.time() < end and not has():
            time.sleep(0.5)
        settle(1.0)
    return has()


def files_deep(mods: dict) -> None:
    """N10-B: Files with a long-named file open (tab, breadcrumbs, status bar), the search panel with hits, the same
    beside the agent drawer, and the Review dialog - the gate scanned only the empty Files tab (B-1/B-3/B-4/B-8 were
    outside it)"""
    if not (click("nav.tabs button", "Files") and view() == "files"):
        return
    js("(() => { const d = document.querySelector('.tree [data-nav=\"docs\"]'); if (d && d.dataset.open !== '1') d.click(); })()")
    settle()
    if not expect("files: long file", click(".tree [data-nav=%s]" % jq(LONG)), "the long-named file is not in the tree"):
        return
    scan("files: long file")
    # "Search 3" once it has hits; a click on the open tab folds the panel
    if click_start(".fpanel .ptab", "Search") and (js("!!document.querySelector('.fpanel input.sq')")
                                                   or click_start(".fpanel .ptab", "Search")):
        js("(() => { const i = document.querySelector('.fpanel input.sq'); i.value = 'long-name-hit|PLANTED'; "
           "i.dispatchEvent(new Event('input', {bubbles: true})); "
           "i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()")
        time.sleep(1.2)
        settle()
        expect("files: search hits", bool(js("document.querySelectorAll('.fpanel .hfile').length > 1")), "the search found no files")
        scan("files: search hits")
    if mods.get("agent") and api("/api/state").get("feed", {}).get("on") and click("#actbar button", "Agent", attr="aria-label"):
        scan("files: long file + agent drawer")
        click("#actbar button", "Agent", attr="aria-label")
    if click("#inspector button", "Review"):
        time.sleep(1)
        settle()
        scan("review")
        close_overlay()
    click("nav.tabs button", "Terminal")


def http_deep(mods: dict) -> None:
    """N10-C: the HTTP tab after a run (results, a request's detail), Environment settings (N10-C-9: the focus on the
    tunnel choice), the same beside the agent drawer - the gate scanned only the HTTP tab as it opens"""
    if not mods.get("http") or not (click("nav.tabs button", "HTTP") and view() == "http"):
        return
    if click_start("button", "Run all on dev"):
        time.sleep(1.5)
        settle()
        scan("http: run results")
    if click(".hpreqname", "ping"):
        scan("http: request detail")
    if click_start("button", "Environment"):
        time.sleep(0.8)
        settle()
        expect("http: environment settings", bool(js("document.activeElement?.getAttribute('role') === 'radio'")),
               "the dialog did not put the focus on the tunnel choice")
        scan("http: environment settings")
        close_overlay()
    if mods.get("agent") and api("/api/state").get("feed", {}).get("on") and click("#actbar button", "Agent", attr="aria-label"):
        scan("http: results + agent drawer")
        click("#actbar button", "Agent", attr="aria-label")
    click("nav.tabs button", "Terminal")


def security_notice(widths):
    """The one-time Security screen for a user onboarded before the update:
    Minimal preselected, shown once, an own mix only informed. Needs the harness's files (SERVE_WORK)."""
    if not SERVE_WORK:
        return
    ff, pf = os.path.join(SERVE_WORK, "ph", "features.json"), os.path.join(SERVE_WORK, "ph", "policy.json")
    with open(pf) as f:
        pol = f.read()
    def unseen(security=None):
        with open(ff) as f:
            d = json.load(f)
        d.pop("seen", None)
        with open(ff, "w") as f:
            json.dump(dict(d, onboarded=True), f)
        with open(pf, "w") as f:
            f.write(pol if security is None else json.dumps(dict(json.loads(pol), security=security)))
        reload()
        settle(1)
    try:
        for w in widths:
            CTX.update(preset="security-notice", width=w)
            ab("set", "viewport", str(w), str(HEIGHT.get(w, 1000)))
            unseen()
            expect("notice: minimal", js("document.querySelector('.sn input[name=sn-profile]:checked')?.value === 'hole' && document.querySelector('[data-notice=apply]')?.textContent === 'Keep Minimal'"),
                   "an onboarded user without a choice must see the notice with Minimal preselected")
            scan("notice: minimal")
            shot("notice: minimal")
            click("[data-notice=apply]")
            reload()
            settle(1)
            expect("notice: once", js("!document.querySelector('.sn')"), "the notice must not come back after it was shown")
        unseen({"db_prod_where": True, "piper_port": True})
        expect("notice: custom", js("!document.querySelector('.sn input') && document.querySelector('.sn-own')?.textContent.includes('Custom, 2 of')"),
               "an own mix must only be informed, nothing preselected")
        scan("notice: custom")
        shot("notice: custom")
        click("[data-notice=dismiss]")
        expect("notice: dismissed", not overlay_open(), "Got it must close the notice")
    finally:
        with open(pf, "w") as f:
            f.write(pol)


def security_profiles(widths):
    """Real GUI saves, confirmation cancellation and optional details, against a tree-enabled piperd."""
    sec = next(s for s in api("/api/settings")["sections"] if s["id"] == "security")
    field = next(f for f in sec["fields"] if f["type"] == "security")
    minimal = {k: True for k, on in field["value"].items() if on}
    recommended = field["presets"]["recommended"]
    def selected():
        s = next(s for s in api("/api/settings")["sections"] if s["id"] == "security")
        return {k: True for k, on in s["fields"][0]["value"].items() if on}
    def active(profile):
        expect("security: active " + profile, js("""(() => {
            const cards = [...document.querySelectorAll('.sc-profile')];
            const selected = cards.filter(c => c.classList.contains('active'));
            const pressed = cards.filter(c => c.getAttribute('aria-pressed') === 'true');
            const card = cards.find(c => c.dataset.profile === %s);
            const other = cards.find(c => c !== card);
            return selected.length === 1 && selected[0] === card && pressed.length === 1 && pressed[0] === card
                && !!card.querySelector('.sc-current')
                && getComputedStyle(card).backgroundColor !== getComputedStyle(other).backgroundColor;
        })()""" % jq(profile)), "only the saved profile must have the blue highlight, CURRENT and pressed state")
    first_choice = True
    for w in widths:
        CTX.update(preset="security", width=w)
        ab("set", "viewport", str(w), str(HEIGHT.get(w, 1000)))
        palette_run("Settings")
        click("button[data-nav='security']")
        expect("security: profiles", js("document.querySelectorAll('.sc-profile').length === 3 && !document.querySelector('.sc-group') && !document.querySelector('.st-save')"), "overview must have only three profiles, no tree or redundant Save")
        expect("security: minimal current", selected() == minimal, "absent Security must keep the minimal choices")
        active("hole")
        if first_choice:
            click("button[data-profile='hole']")
            click("#overlay.open button", "Apply Minimal")
            saved = next(s for s in api("/api/settings")["sections"] if s["id"] == "security")
            expect("security: explicit minimal", saved["rev"] != sec["rev"] and selected() == minimal,
                   "confirming the unchanged first-use default must persist an explicit Security choice")
            first_choice = False
        scan("security: profiles")
        shot("security: profiles")
        click("button[data-profile='recommended']")
        scan("security: confirmation")
        click("#overlay.open button", "Cancel")
        expect("security: cancel", selected() == minimal, "canceling must not change rules")
        active("hole")
        click("button[data-profile='recommended']")
        click("#overlay.open button", "Apply Recommended")
        expect("security: applied", selected() == recommended, "two clicks must persist Recommended")
        expect("security: timing", js("document.querySelector('.st-note')?.textContent.includes('sandbox at next start')"), "saved note must distinguish sandbox timing from live tools")
        expect("security: current", js("document.querySelector('[data-profile=\"recommended\"] .sc-current')?.textContent === 'CURRENT'"), "current profile must reflect saved state")
        active("recommended")
        shot("security: recommended")
        reload()
        settle()
        palette_run("Settings")
        click("button[data-nav='security']")
        active("recommended")
        click("button.sc-customize")
        if js("document.querySelector(\".sc-group[data-group='approvals'] .sc-fold\")?.getAttribute('aria-expanded') !== 'true'"):
            click(".sc-group[data-group='approvals'] .sc-fold")
        expect("security: direct details", js("document.querySelectorAll('.sc-kind').length === 39 && !document.querySelector('.sc-subfold')"), "approval switches must be directly visible without intermediate folds")
        scan("security: details")
        shot("security: details")
        if js("document.querySelector(\".sc-group[data-group='tools'] .sc-fold\")?.getAttribute('aria-expanded') !== 'true'"):
            click(".sc-group[data-group='tools'] .sc-fold")
        click("[data-sec='db_prod_where'] .sc-sw")
        click("button.sc-back")
        active("recommended")
        expect("security: unsaved change", selected() == recommended, "a draft must not replace the active profile")
        click("button.sc-customize")
        click("button.st-save")
        if overlay_open():
            click("#overlay.open button", "Save anyway")
        click("button.sc-back")
        expect("security: custom", js("!document.querySelector('.sc-profile.active, .sc-profile[aria-pressed=true], .sc-current') && document.querySelector('.sc-profiles')?.textContent.includes('Current: Custom')"), "saved custom rules must not claim any profile is active")
        click("button[data-profile='guarded']")
        click("#overlay.open button", "Apply Strict")
        expect("security: strict", len(selected()) == 64, "Strict must enable all guards")
        active("guarded")
        shot("security: strict")
        click("button[data-profile='hole']")
        click("#overlay.open button", "Apply Minimal")
        expect("security: minimal restored", selected() == minimal, "Minimal must persist the original defaults")
        active("hole")


def session_cards(widths):
    """Real mailbox -> card -> browser decision -> persisted session grant (never live piperd)."""
    if not SERVE_WORK:  # --url may point at a harness whose filesystem is unavailable
        return
    load_preset("all")

    def until(pred, seconds=12):
        end = time.time() + seconds
        while time.time() < end:
            hit = pred()
            if hit:
                return hit
            time.sleep(0.1)
        raise RuntimeError("session approval scenario timed out")

    def request(sid, cls):
        rid = os.urandom(16).hex()
        s = next(x for x in api("/api/state")["sessions"] if x["id"] == sid)
        cfg = "claude" if s.get("claude_mode", "session") == "session" else "piper"
        mb = os.path.join(SERVE_WORK, "ph", "sessions", sid, cfg, "piper-mailbox")
        dest = os.path.join(mb, "out", rid + ".json")
        with open(dest + ".tmp", "w") as f:
            json.dump({"id": rid, "tool": "http_run", "args": {"file": "http-client/ping.http", "env": cls}}, f)
        os.rename(dest + ".tmp", dest)
        return os.path.join(mb, "in", rid + ".json")

    def show_card(card):
        """F17 (9.63): #pending shows one card (a red one, else the oldest - serve.py's Global agent card waits
        all along); its bar "N more waiting" (.pmore) shows the next one in its place"""
        shown = "(() => { const t = document.querySelector('#pending .pending .phead strong'); return !!t && t.textContent === %s; })()" % jq(card["title"])
        return until(lambda: js(shown) or (click("#pending .pmore") and False))

    for width in widths:
        CTX.update(preset="all", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        sid = api("/api/sessions", {"project": "demo/http", "title": "Session approvals %s" % width})["id"]
        try:
            state = until(lambda: next((x for x in api("/api/state")["sessions"] if x["id"] == sid and x["state"] in ("blocked", "ready", "waiting")), None))
            if state["state"] == "blocked":
                api("/api/sessions/%s/approve" % sid, {})
            until(lambda: any(x["id"] == sid and x["state"] in ("ready", "waiting") for x in api("/api/state")["sessions"]))
            whole_queue(True)
            for cls in ("prod", "test"):
                label = "Allow for this session on " + cls.upper()
                if cls == "prod":
                    # Regress the long notification-help toast that covered grants at 1024 px.
                    js("(() => { let box=document.getElementById('toasts'); if(!box){box=document.createElement('div');box.id='toasts';document.body.append(box);} const t=document.createElement('div');t.className='toast notification-help';t.dataset.approvalFixture='1';const text=document.createElement('span');text.textContent=\"Desktop notifications are blocked for Piper - allow them in the browser's site settings (and macOS System Settings → Notifications)\";t.append(text);box.append(t); })()")
                response = request(sid, cls)
                card = until(lambda: next((x for x in api("/api/state")["pending"] if x["session"] == sid), None))
                # F17: the floating card's session grant is in Allow's ▾ menu (.pcaret -> .cmenu)
                grant = ".cmenu button[data-menu-id=%s]" % jq(label)
                show_card(card)
                until(lambda: click("#pending .pcaret") and js("!!document.querySelector(%s)" % jq(grant)))
                screen = "session approval " + cls
                expect(screen, card.get("session_always") == label, "card has no environment-specific session grant")
                expect(screen, js("document.querySelector(%s).classList.contains('danger')" % jq(grant)) == (cls == "prod"), "production session grant must be red")
                ab("press", "Escape")   # the ▾ menu: scanned without it
                settle(0.2)
                scan(screen)
                shot(screen)
                if cls == "prod":
                    expect(screen, js("[...document.querySelectorAll(\".notification-help[data-approval-fixture]\")].every(t => !t.getBoundingClientRect().width)"), "notification setup help covers the approval")
                # Queue and floating cards send the same session-only decision.
                if cls == "prod":
                    hit = click("#queue .qi[data-key='a:p:%s'] button" % card["id"], label)
                else:
                    hit = show_card(card) and click("#pending .pcaret") and click(grant)
                expect(screen, hit, "session grant button did not react")
                until(lambda: os.path.exists(response))
                with open(response) as f:
                    expect(screen, json.load(f).get("ok"), "approved HTTP request did not run")
                until(lambda: not any(x["id"] == card["id"] for x in api("/api/state")["pending"]))
                js("document.querySelectorAll(\"[data-approval-fixture]\").forEach(t => t.remove())")
                reload()
                settle()
                response = request(sid, cls)
                until(lambda: os.path.exists(response))
                with open(response) as f:
                    expect(screen + " remembered", json.load(f).get("ok"), "next request did not reuse the session grant after reload")
                expect(screen + " remembered", not any(x["session"] == sid for x in api("/api/state")["pending"]), "remembered action opened another card")
        finally:
            api("/api/sessions/%s/stop" % sid, {})
            until(lambda: any(x["id"] == sid and x["state"] in ("exited", "error") for x in api("/api/state")["sessions"]))


def allow_in_project() -> None:
    """Real blocked preflight: cancelling has no effect, confirming persists for this project."""
    sid = api("/api/sessions", {"project": "demo/allow"})["id"]
    def state():
        return next(s for s in api("/api/state")["sessions"] if s["id"] == sid)
    end = time.time() + 20
    while time.time() < end and state()["state"] != "blocked":
        time.sleep(.2)
    expect("allow blocked", state()["state"] == "blocked", "fixture must block")
    js("localStorage.setItem('piper.last', JSON.stringify({view:'term', active:%s}))" % jq(sid))
    reload()
    settle(2)
    expect("allow dialog", click_start("button", "Allow in this project ("), "Allow button missing")
    scan("Allow in this project confirmation")
    shot("Allow in this project confirmation")
    print("ALLOW-SCREENSHOT " + OUT, flush=True)
    expect("allow cancel", click("#overlay.open .modal button", "Cancel"), "Cancel missing")
    expect("allow cancel", state()["state"] == "blocked", "Cancel changed session state")
    click_start("button", "Allow in this project (")
    expect("allow confirm", click("#overlay.open .modal button", "Allow"), "Allow confirmation missing")
    end = time.time() + 20
    while time.time() < end and state()["state"] in ("blocked", "preflight", "preparing", "spawning", "trust", "starting", "attesting"):
        time.sleep(.2)
    current = state()
    expect("allow started", current["state"] in ("ready", "waiting", "working", "attention"),
           str({k: current.get(k) for k in ("state", "detail", "blocks")}))
    api("/api/sessions/%s/stop" % sid, {})
    end = time.time() + 20
    while time.time() < end and state()["state"] not in ("exited", "error"):
        time.sleep(.2)
    later = api("/api/sessions", {"project": "demo/allow"})["id"]
    end = time.time() + 20
    while time.time() < end:
        ss = next(s for s in api("/api/state")["sessions"] if s["id"] == later)
        if ss["state"] not in ("preflight", "preparing", "spawning", "trust", "starting", "attesting"):
            break
        time.sleep(.2)
    expect("allow later session", ss["state"] in ("ready", "waiting", "working", "attention"),
           str({k: ss.get(k) for k in ("state", "detail", "blocks")}))
    api("/api/sessions/%s/stop" % later, {})


def http_install_resume():
    """An unfinished installation pauses while hidden and resumes in the same Files/HTTP pane."""
    api("/api/features", {"modules": {"http": True, "grid": True}})
    settle(2)
    js("""(async () => {
        const {httpApi} = await import('/piper/dist/httpview.js');
        const original = httpApi.envs;
        window.__installing = true;
        window.__installReads = 0;
        httpApi.envs = async (...args) => {
            const r = await original(...args);
            window.__installReads++;
            if (r.data && window.__installing) r.data = {...r.data, ijhttp: null, installing: true};
            return r;
        };
        window.__restoreInstall = () => { httpApi.envs = original; };
    })()""")
    try:
        for mode in ("Files tab", "Files pane", "HTTP pane"):
            js("window.__installing = true")
            if mode == "HTTP pane":
                palette_run("HTTP pane beside the session (this project's .http requests)")
            else:
                click("nav.tabs button", "Files")
                js("(() => { const d = document.querySelector('.tree [data-nav=\"api\"]'); if (d && d.dataset.open !== '1') d.click(); })()")
                settle()
                click(".tree [data-nav=\"api/demo.http\"]")
            ab("wait", "--fn", "[...document.querySelectorAll('.httpbar')].some(b => b.offsetParent && b.textContent.includes('Installing'))")
            js("window.__retainedHttpBar = [...document.querySelectorAll('.httpbar')].find(b => b.offsetParent && b.textContent.includes('Installing'))")
            if mode == "HTTP pane":
                palette_run("HTTP pane: close")
            else:
                click("nav.tabs button", "Terminal")
            settle(4)
            before = js("window.__installReads")
            settle(3.3)
            expect(mode + " hidden install", js("window.__installReads") == before, "hidden HTTP view kept polling")
            js("window.__installing = false")
            if mode == "Files tab":
                click("nav.tabs button", "Files")
            else:
                palette_run("Files pane beside the session" if mode == "Files pane"
                            else "HTTP pane beside the session (this project's .http requests)")
            ab("wait", "--fn", "window.__installReads > %s && window.__retainedHttpBar.offsetParent && !window.__retainedHttpBar.textContent.includes('Installing')" % before)
            expect(mode + " resumed install", js("window.__retainedHttpBar.isConnected && !!window.__retainedHttpBar.offsetParent"), "return replaced or hid the retained HTTP view")
            scan(mode + " installation resumed")
            shot(mode + " installation resumed")
            if mode != "Files tab":
                palette_run(mode + ": close")
            else:
                click("nav.tabs button", "Terminal")
    finally:
        js("window.__restoreInstall()")


def disabled_controls():
    """Exercise the real L5 worker lifecycle and optional polls through the running test piperd."""
    CTX.update(preset="disabled-controls", width=1920)
    ab("set", "viewport", "1920", "1080")
    sec = next(s for s in api("/api/settings")["sections"] if s["id"] == "security")
    minimal = sec["fields"][0]["presets"]["hole"]
    def save(value):
        api("/api/settings", {"section": "security", "values": {"security": value}, "confirm": ["security"]})
    def watched():
        return next(s for s in api("/api/state")["sessions"] if s["id"] == DEMO_SID).get("watch") or []
    def await_watch(on):
        end = time.time() + 15
        while time.time() < end:
            if bool(watched()) == on:
                return True
            time.sleep(0.25)
        return False
    save(minimal)
    api("/api/fs/create", {"project": "grp/demo", "path": "audit-disabled-key.pem", "kind": "file", "text": "synthetic test fixture"})
    palette_run(DEMO_TITLE)
    if js("document.body.classList.contains('no-inspector')"):
        click("[aria-label='context panel']")
    save(dict(minimal, l5_watch=True))
    expect("L5 enabled finds fixture", await_watch(True), "enabled watcher did not find the uncovered fixture")
    settle(2)
    expect("L5 enabled inspector", js("document.querySelector('#inspector')?.textContent.includes('New sensitive files (L5)')"), "enabled finding missing in inspector")
    save(minimal)
    expect("L5 disabled clears findings", await_watch(False), "disabled watcher kept findings")
    settle(2)
    expect("L5 disabled inspector", js("!document.querySelector('#inspector')?.textContent.includes('New sensitive files (L5)')"), "disabled L5 still shows warning")
    expect("attestation off inspector", js("!document.querySelector('#inspector')?.textContent.includes('Isolation check')"), "old verification still displayed")
    shot("Minimal without active isolation warnings")
    save(dict(minimal, l5_watch=True))
    expect("L5 re-enabled", await_watch(True), "watcher did not restart")
    save(minimal)
    http_install_resume()
    api("/api/features", {"modules": {"gitlab": False, "github": False, "stack": False, "timesheet": False}})
    settle(2)
    js("""(() => {
        const original = window.fetch;
        window.__disabledRequests = [];
        window.fetch = (...args) => {
            window.__disabledRequests.push(String(args[0]));
            return original(...args);
        };
        window.__restoreDisabledFetch = () => { window.fetch = original; };
    })()""")
    palette_run(DEMO_TITLE)
    settle(4)
    requests = js("window.__disabledRequests") or []
    expect("optional polls off", not any('/api/git/ci' in p or '/api/timesheet/' in p for p in requests), "disabled CI/Timesheet requests: %s" % requests)
    expect("Stack off state", api("/api/state").get("stack_up") is False, "disabled Stack still reports a live stack provider")
    shot("Features off without optional polls")
    js("window.__restoreDisabledFetch()")


def project_options(widths) -> None:
    """Actual dialog + piperd: drafts, Cancel, one-time choices, server persistence and worktrees."""
    load_preset("all")

    def until(pred, seconds=15):
        end = time.time() + seconds
        while time.time() < end:
            if pred():
                return True
            time.sleep(.15)
        return False

    def stop(sid):
        api("/api/sessions/%s/stop" % sid, {})
        until(lambda: next(s for s in api("/api/state")["sessions"] if s["id"] == sid)["state"] in ("exited", "error", "blocked"))

    def seed(project, opts, agent="claude"):
        r = api("/api/sessions", {"project": project, "opts": opts, "agent": agent, "remember_for_project": True})
        expect("project seed", not r.get("defaults_warning"), str(r))
        stop(r["id"])

    def choose(project):
        ab("fill", "#overlay.open .wk-search > input[type=search]", project)
        settle()
        if not js("document.querySelector('.startopts').open"):
            click(".startopts > summary")

    def open_for(project):
        expect("project dialog ready", until(lambda: js("document.querySelectorAll('#actbar button').length > 0")), "GUI did not finish loading")
        # A real notification-denied toast after reload covered Cancel at 1024 px in the full gate.
        js("(() => { let box=document.getElementById('toasts'); if(!box){box=document.createElement('div');box.id='toasts';document.body.append(box);} if(box.querySelector('[data-project-options-fixture]'))return; const t=document.createElement('div');t.className='toast notification-help';t.dataset.projectOptionsFixture='1';const text=document.createElement('span');text.textContent=\"Desktop notifications are blocked for Piper - allow them in the browser's site settings (and macOS System Settings → Notifications)\";t.append(text);box.append(t); })()")
        expect("project dialog", palette_run("New session…"), "New session command missing")
        choose(project)
        expect("project dialog no rebuild button",
               js("[...document.querySelectorAll('.startopts button')].every(b => !/rebuild image|image rebuilds|rebuilding now/i.test(b.textContent))"),
               "the removed image rebuild button is still present")
        expect("project dialog controls", js("[...document.querySelectorAll('#toasts .notification-help')].every(t => !t.getBoundingClientRect().width)"), "notification setup help covers the dialog")

    def choice(label):
        return js("(() => { const l = [...document.querySelectorAll('.startopts label')].find(l => l.textContent.trim().startsWith(%s)); "
                  "const e = l?.querySelector('input'); return e ? {checked:e.checked,disabled:e.disabled} : null; })()" % jq(label))

    def set_choice(label, checked):
        js("(() => { const l = [...document.querySelectorAll('.startopts label')].find(l => l.textContent.trim().startsWith(%s)); "
           "const e = l?.querySelector('input'); if (!e || e.disabled) throw new Error('missing enabled choice'); "
           "if (e.checked !== %s) e.click(); })()" % (jq(label), jq(checked)))
        settle()

    def cancel():
        expect("project cancel", click("#overlay.open button", "Cancel"), "Cancel missing")

    def launch():
        before = {s["id"] for s in api("/api/state")["sessions"]}
        expect("project launch", click_start("#overlay.open button.primary", "Start"), "Start missing")
        expect("project launch", until(lambda: not overlay_open()), "dialog stayed open")
        fresh = [s for s in api("/api/state")["sessions"] if s["id"] not in before]
        expect("project launch", len(fresh) == 1, "start must create exactly one session")
        if fresh:
            stop(fresh[0]["id"])
            return fresh[0]
        raise RuntimeError("no created session")

    for width in widths:
        CTX.update(preset="project-options", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        seed("demo/http", {"docker": True})
        seed("demo/plain", {})
        reload()
        settle(1)
        # A legacy browser-wide choice must not seed any project anymore.
        js("localStorage.setItem('piper.startopts', JSON.stringify({agent:'codex',opts:['idea']}))")
        open_for("demo/http")
        expect("project A", choice("Docker") == {"checked": True, "disabled": False}, "A did not load its saved Docker")
        set_choice("Unfiltered network", True)
        choose("demo/plain")
        expect("project B", choice("Docker")["checked"] is False and choice("IntelliJ")["checked"] is False, "A or browser-wide defaults leaked to B")
        choose("demo/http")
        expect("project draft", choice("Unfiltered network")["checked"], "A → B → A lost the draft")
        scan("Project options draft")
        shot("Project options draft")
        cancel()
        open_for("demo/http")
        expect("project cancel", not choice("Unfiltered network")["checked"], "Cancel persisted a draft")
        set_choice("Docker", False)
        set_choice("Remember", False)
        once = launch()
        expect("project once", once.get("opts") == {}, "one-time session did not use its own choices")
        open_for("demo/http")
        expect("project once", choice("Docker")["checked"], "one-time session changed project defaults")
        set_choice("Docker", False)
        launch()
        reload()
        settle(1)
        open_for("demo/http")
        expect("project empty", not choice("Docker")["checked"] and choice("Remember")["checked"], "empty preferences or Remember did not survive reload")
        scan("Project options saved empty")
        shot("Project options saved empty")
        cancel()

    # A new browser context has no piper.startopts or draft, but loads the same server profile.
    other = SESSION + "-project-options"
    seed("demo/http", {"docker": True})
    try:
        expect("project second browser", ab("open", BASE + "/#token=" + TOKEN, session=other).get("success"), "second browser did not open")
        ab("wait", "--fn", "document.querySelectorAll('#actbar button').length > 0", session=other)
        ab("eval", "(%s)('k')" % KEY, session=other)
        ab("wait", "--fn", "[...document.querySelectorAll('#overlay.open .pitem .plabel')].some(e => e.textContent === 'New session…')", session=other)
        ab("eval", "[...document.querySelectorAll('#overlay.open .pitem')].find(e => e.querySelector('.plabel')?.textContent === 'New session…').click()", session=other)
        ab("fill", "#overlay.open .wk-search > input[type=search]", "demo/http", session=other)
        result = ab("eval", "(() => { const l = [...document.querySelectorAll('.startopts label')].find(e => e.textContent.trim().startsWith('Docker')); return l?.querySelector('input').checked; })()", session=other)
        expect("project second browser", (result.get("data") or {}).get("result") is True, str(result))
    finally:
        ab("close", session=other)

    seed("demo/http", {}, "codex")
    api("/api/features", {"modules": {"codex": False}})
    reload()
    settle(1)
    open_for("demo/http")
    expect("project unavailable agent", "Codex is switched off" in js("document.querySelector('.startopts').textContent"), "missing unavailable-agent explanation")
    fallback = launch()
    expect("project unavailable agent", fallback["agent"] == "claude", "disabled Codex was used for the session")
    expect("project unavailable agent", api("/api/state")["project_session_defaults"]["demo/http"]["agent"] == "codex", "temporary Claude overwrote the saved Codex")
    api("/api/features", {"modules": {"codex": True}})

    # A linked worktree shares the base preferences and has no independent saved record.
    seed("grp/demo", {"idea": True})
    wt = api("/api/git/worktree", {"project": "grp/demo", "action": "add", "name": "start-options", "branch": "feat/start-options", "new": True})
    seed(wt["key"], {"docker": True})
    st = api("/api/state")
    expect("project worktree", st["project_session_defaults"]["grp/demo"]["opts"] == ["docker"], "worktree did not update its base")
    expect("project worktree", wt["key"] not in st["project_session_defaults"], "worktree got its own defaults")
    reload()
    settle(1)
    open_for("grp/demo")
    expect("project worktree", choice("Docker")["checked"] and not choice("IntelliJ")["checked"], "base dialog did not load worktree's choices")
    cancel()
    # Global enforcement is displayed as effective, but it is not saved as an explicit choice.
    api("/api/settings", {"section": "sandbox", "values": {"unrestricted_network": True}, "confirm": ["unrestricted_network"]})
    reload()
    settle(1)
    open_for("demo/http")
    expect("project enforced", choice("Unfiltered network") == {"checked": True, "disabled": True}, "global enforcement is hidden or editable")
    scan("Project options globally enabled")
    shot("Project options globally enabled")
    cancel()
    api("/api/settings", {"section": "sandbox", "values": {"unrestricted_network": False}})
    js("document.querySelectorAll('[data-project-options-fixture]').forEach(t => t.remove())")


def terminal_clipboard():
    """Real xterm, browser clipboard and keyboard; emulate only the TUI's asynchronous OSC52 reply."""
    close_overlay()
    # Only this disposable browser gets clipboard access. Headless Chrome has no permission UI.
    cdp = ab("get", "cdp-url")["data"]["cdpUrl"]
    grant = subprocess.Popen(["node", "-e", """
      const ws = new WebSocket(process.argv[1]);
      const timer = setTimeout(() => process.exit(1), 5000);
      ws.onopen = () => ws.send(JSON.stringify({id: 1, method: 'Target.getTargets'}));
      ws.onmessage = e => { const reply = JSON.parse(e.data);
        if (reply.id === 1) {
          const target = reply.result.targetInfos.find(t => t.type === 'page' && t.url.startsWith(process.argv[2]));
          ws.send(JSON.stringify({id: 2, method: 'Browser.grantPermissions', params: {
            origin: process.argv[2], browserContextId: target.browserContextId,
            permissions: ['clipboardReadWrite', 'clipboardSanitizedWrite']}}));
        } else if (reply.id === 2) {
          clearTimeout(timer);
          if (reply.error) { console.error(reply.error); process.exit(1); }
          // Chrome resets these overrides when the granting CDP connection closes.
          console.log('READY');
        }
      };
    """, cdp, BASE], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if grant.stdout.readline().strip() != "READY":
        raise RuntimeError("clipboard permission setup failed: " + grant.communicate(timeout=10)[1])
    js("""(async () => {
      const {TermView} = await import('/piper/dist/term.js');
      const fixture = document.createElement('section'); fixture.id = 'clipboard-fixture';
      fixture.style.cssText = 'position:fixed;inset:40px auto auto 40px;width:720px;height:320px;z-index:99999;background:#13171e;padding:20px;display:flex;flex-direction:column';
      const title = document.createElement('h3'); title.textContent = 'Terminal clipboard regression'; fixture.append(title);
      const notices = [];
      const view = new TermView('clipboard-test', '/demo', {onRefused(message, retry) {
        notices.push(message);
        const button = document.createElement('button'); button.textContent = retry.label;
        button.onclick = retry.run; fixture.append(button);
      }});
      fixture.append(view.el); document.body.append(fixture);
      // Opening xterm directly avoids a fake WebSocket and cannot type into a running session.
      view.term.open(view.host); view.term.resize(65, 8);
      await new Promise(done => view.term.write('Native terminal selection\\r\\nCodex TUI selection via OSC 52', done));
      const inputs = []; view.term.onData(data => inputs.push(data));
      window.__copyProbe = {view, fixture, notices, inputs, respond: true, text: 'Grüße aus Köln Codex', write: navigator.clipboard.write, writeText: navigator.clipboard.writeText};
      view.host.addEventListener('mouseup', () => {
        const probe = window.__copyProbe;
        if (probe.respond) setTimeout(() => view.term.write('\\x1b]52;c;' + btoa(unescape(encodeURIComponent(probe.text))) + '\\x07'), 0);
      });
      return true;
    })()""")
    mod = "Meta" if js("/mac|iphone|ipad|ipod/i.test(navigator.platform)") else "Control"
    chord = mod + "+c"

    def clipboard():
        return js("navigator.clipboard.readText()")

    try:
        ab("clipboard", "write", "unrelated clipboard")
        ab("click", "#clipboard-fixture .xterm-screen")
        settle(0.1)
        expect("clipboard automatic", clipboard() == "Grüße aus Köln Codex", "OSC52 after mouse release did not copy UTF-8 text")
        ab("clipboard", "write", "another app replaced the clipboard")
        ab("press", chord)
        expect("clipboard repeat", clipboard() == "Grüße aus Köln Codex", "copy shortcut did not repeat the TUI selection")
        expect("clipboard no input", js("window.__copyProbe.inputs.length") == 0, "copy shortcut typed into the terminal")
        js("window.__copyProbe.view.term.select(0, 0, 6)")
        ab("press", chord)
        expect("clipboard native", clipboard() == "Native", "xterm selection must take priority over cached TUI text")
        js("""(() => {
          const probe = window.__copyProbe; probe.view.term.clearSelection(); probe.text = 'Retry selected text';
          navigator.clipboard.write = navigator.clipboard.writeText = () => Promise.reject(new DOMException('test denial', 'NotAllowedError'));
        })()""")
        ab("click", "#clipboard-fixture .xterm-screen")
        settle(0.1)
        expect("clipboard refusal", js("window.__copyProbe.notices.length") == 1, "early denial was swallowed")
        ab("click", "#clipboard-fixture button")
        expect("clipboard retry", clipboard() == "Retry selected text", "Copy retry did not use the selected text")
        js("window.__copyProbe.respond = false; navigator.clipboard.writeText = window.__copyProbe.writeText; navigator.clipboard.write = window.__copyProbe.write")
        ab("click", "#clipboard-fixture .xterm-screen")
        ab("clipboard", "write", "new selection pending")
        ab("press", chord)
        expect("clipboard invalidation", clipboard() == "new selection pending", "a new mouse interaction restored obsolete text")
        ab("press", "Control+c")
        expect("clipboard interrupt", js("window.__copyProbe.inputs.at(-1)") == "\x03", "Ctrl+C without a selection must remain the interrupt")
        ab("screenshot", os.path.join(OUT, "terminal-clipboard.png"))
    finally:
        js("navigator.clipboard.writeText = window.__copyProbe.writeText; navigator.clipboard.write = window.__copyProbe.write; window.__copyProbe.view.dispose(); window.__copyProbe.fixture.remove(); delete window.__copyProbe")
        grant.terminate()
        grant.communicate(timeout=10)


def start_serve():
    global SERVE_WORK
    SERVE_WORK = os.path.join(OUT, "serve")
    p = subprocess.Popen([sys.executable, os.path.join(HERE, "serve.py"), "--port", str(A.port), "--work", SERVE_WORK] + (["--security-tree"] if A.security_tree else []), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, start_new_session=True)
    log = []
    end = time.time() + 180
    while time.time() < end:
        ln = p.stdout.readline()
        if not ln:
            break
        log.append(ln)
        if ln.startswith("SERVE "):
            return p, ln.split()[1], log
    p.poll() is None and os.kill(p.pid, signal.SIGTERM)
    print("gui.py: serve.py did not come up:\n" + "".join(log[-30:]), file=sys.stderr)
    sys.exit(2)


def recent_alerts(widths):
    for width in widths:
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        js("localStorage.setItem('piper.alerts', JSON.stringify([{text:'expired alert probe',ts:Date.now()-86400001}, ...Array.from({length:30},(_,i)=>({text:'recent alert probe '+i,ts:Date.now(),src:'toast'}))]))")
        reload()
        settle(1)
        expect("alerts open", click('button[aria-label="Recent alerts"]'), "bell missing")
        settle()
        expect("alerts expiry", js("!document.querySelector('#notices').textContent.includes('expired alert probe')"), "expired alert remained")
        before = js("(() => {const r=document.querySelector('#notices').getBoundingClientRect(); return [r.x,r.y,r.width,r.height]})()")
        ab("screenshot", os.path.join(OUT, "alerts-before-%s.png" % width))
        foot_before = js("document.querySelector('#notices .nt-foot').getBoundingClientRect().y")
        expect("alerts clear", click('#notices button', 'Clear all'), "Clear all missing")
        settle()
        after = js("(() => {const r=document.querySelector('#notices').getBoundingClientRect(); return [r.x,r.y,r.width,r.height]})()")
        expect("alerts position", before == after, "Clear all moved panel: %s -> %s" % (before, after))
        expect("alerts footer", foot_before == js("document.querySelector('#notices .nt-foot').getBoundingClientRect().y"), "Clear all moved the footer")
        expect("alerts empty", js("document.querySelector('#notices .nt-empty') !== null && document.querySelector('#notices button').disabled"), "list was not cleared")
        expect("alerts storage", js("JSON.parse(localStorage.getItem('piper.alerts')).length === 0"), "cleared history remains stored")
        ab("screenshot", os.path.join(OUT, "alerts-after-%s.png" % width))
        ab("press", "Escape")
        click('button[aria-label="Recent alerts"]')
        settle()
        expect("alerts reopen", js("document.querySelector('#notices .nt-empty') !== null"), "cleared alerts returned")
        ab("press", "Escape")

    # Two browser tabs share storage but retain independent in-memory NoticeLogs.
    js("localStorage.setItem('piper.alerts', JSON.stringify([{text:'cross-window probe',ts:Date.now(),src:'toast'}]))")
    reload()
    settle()
    url = BASE + '/'
    expect("alerts new tab", ab("tab", "new", "--label", "alerts-peer", url).get("success"), "cannot open second tab")
    settle(1)
    click('button[aria-label="Recent alerts"]')
    settle()
    expect("alerts first tab", ab("tab", "t1").get("success"), "cannot switch to first tab")
    click('button[aria-label="Recent alerts"]')
    settle()
    click('#notices button', 'Clear all')
    settle()
    expect("alerts second tab", ab("tab", "alerts-peer").get("success"), "cannot switch to second tab")
    settle()
    expect("alerts cross-window", js("document.querySelector('#notices .nt-empty') !== null"), "other tab retained cleared rows")
    ab("press", "Escape")
    expect("alerts no resurrection", js("JSON.parse(localStorage.getItem('piper.alerts')).length === 0"), "other tab resurrected cleared history")
    ab("tab", "close", "alerts-peer")
    expect("alerts first tab", ab("tab", "t1").get("success"), "cannot switch to first tab")
    ab("press", "Escape")
    js("localStorage.setItem('piper.alerts', JSON.stringify([{text:'read probe',ts:Date.now(),src:'toast'}]))")
    reload()
    settle()
    click('button[aria-label="Recent alerts"]')
    settle()
    ab("press", "Escape")
    settle()
    expect("alerts dismiss read", js("JSON.parse(localStorage.getItem('piper.alerts')).length === 0"), "read alerts survived closing")

    expect("settings opens", palette_run("Settings"), "Settings did not open")
    expect("features opens", click('button[data-nav="features"]'), "Features did not open")
    settle()
    expect("features toggle", click('[data-module="timesheet"] button[role="switch"]'), "Timesheet toggle missing")
    expect("features save", click('section[data-sid="features"] button.st-save'), "Save missing")
    settle(1)
    expect("features inline confirmation", js("document.querySelector('section[data-sid=features] .st-note')?.textContent?.includes('Saved:')"), "inline saved feedback missing")
    expect("features no alert", js("!JSON.parse(localStorage.getItem('piper.alerts') || '[]').some(n => n.text === 'Features: saved')"), "own save added a persistent alert")
    ab("screenshot", os.path.join(OUT, "features-saved-inline.png"))



def database_search_and_chooser(widths):
    mods = load_preset("all")
    for width in widths:
        CTX.update(preset="all", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        expect("database overview", palette_run("Database"), "Database did not open")
        settle()
        search = '.db-connpage input[type=search]'
        expect("database search exists", js("!!document.querySelector(%s)" % jq(search)), "overview search missing")
        total = js("document.querySelectorAll('.db-ctable .db-crow:not(.db-chead)').length")
        expect("database search fixtures", total > 0, "no test connections")
        ab("fill", search, "no-such-connection-xyz")
        expect("database search no results", js("document.querySelector('.db-connpage [role=status]')?.textContent") == "No matching connections.", "missing empty state")
        expect("database search focus", js("document.activeElement === document.querySelector(%s)" % jq(search)), "typing lost focus")
        shot("database-search-empty")
        ab("fill", search, "DEMO")
        expect("database search matching", js("document.querySelectorAll('.db-ctable .db-crow:not(.db-chead)').length") > 0, "case-insensitive project search failed")
        ab("fill", search, "demo MYSQL")
        expect("database search narrows", js("document.querySelectorAll('.db-ctable .db-crow:not(.db-chead)').length") == 1, "combined project and engine search did not narrow results")
        scan("database search")
        shot("database-search")
        ab("click", '[aria-label="Clear database search"]')
        expect("database search clear", js("document.querySelectorAll('.db-ctable .db-crow:not(.db-chead)').length") == total, "clearing did not restore connections")
        expect("database clear focus", js("document.activeElement === document.querySelector(%s)" % jq(search)), "clear did not return focus")
        expect("database compact search", js("document.querySelector('.db-cp-head .db-find').getBoundingClientRect().width <= 420"), "overview search stretches across the table")
        # IDEA metadata only: no connection is saved and no database is contacted.
        js("""(async () => {
          const {dbApi} = await import('/piper/dist/dbview.js');
          window.__ideaConnections = dbApi.connections;
          dbApi.connections = async project => ({data:{project,idea:[
            {name:'Reporting',engine:'postgres',user:'reader',database:'catalog',cls_guess:'test'},
            {name:'Local',engine:'mysql',user:'local',database:'dev',cls_guess:'dev'}
          ],tunnel_targets:[]}});
        })()""")
        try:
            # F10: Scan stacks and Import from IDEA live in one Import ▾ (the caret a mask icon since the pages polish)
            expect("IDEA import opens", click(".db-cp-head button", "Import") and click(".cmenu button[role=menuitem]", "Import from IDEA…"),
                   "import button missing")
            project = '#overlay button[aria-label="Project"]'
            find = '#overlay input[aria-label="Find project"]'
            ab("click", project)
            expect("IDEA project search focus", js("document.activeElement === document.querySelector(%s)" % jq(find)), "search should focus when project picker opens")
            ab("fill", find, "no-such-project-xyz")
            expect("IDEA project no matches", js("document.querySelector('.db-project-options [role=status]')?.textContent") == "No matching projects.", "missing project empty state")
            ab("press", "Enter")
            expect("IDEA no accidental selection", js("document.querySelector('.db-project-trigger').getAttribute('aria-expanded')") == "true", "Enter selected a nonexistent project")
            ab("fill", find, "demo/plain")
            expect("IDEA search matches project key", js("document.querySelectorAll('.db-project-option').length") == 1, "project key search failed")
            expect("IDEA picker contained", js("(() => {const m=document.querySelector('#overlay .modal'); return m.scrollWidth <= m.clientWidth;})()"), "project picker overflows modal")
            scan("IDEA project search")
            shot("idea-project-search")
            ab("press", "Escape")
            expect("IDEA Escape closes picker only", js("!!document.querySelector('.db-idea-import') && document.querySelector('.db-project-popup').classList.contains('hidden')"), "Escape closed the whole import dialog")
            ab("press", "ArrowDown")
            ab("fill", find, "PLAIN")
            ab("press", "ArrowDown")
            ab("press", "Enter")
            ab("wait", "--fn", "document.querySelectorAll('.db-ider').length === 2")
            expect("IDEA no connection search", js("document.querySelectorAll('.db-idea-import input[type=search]').length") == 1, "unexpected connections search field")
            expect("IDEA project selected", js("document.querySelector('.db-project-trigger').textContent.includes('plain')"), "project selection missing")
            expect("IDEA sources contained", js("(() => {const m=document.querySelector('#overlay .modal'),r=m.getBoundingClientRect(); return m.scrollWidth<=m.clientWidth && [...m.querySelectorAll('.db-ider button')].every(b=>b.getBoundingClientRect().right<=r.right-20);})()"), "Use buttons clipped by modal")
            scan("IDEA selected project")
            shot("idea-selected-project")
            ab("click", '#overlay button[aria-label="Use Reporting"]')
            expect("IDEA preset project", js("document.querySelector('#overlay select[name=project]').value") == "demo/plain", "Use lost project key")
            expect("IDEA preset database", js("document.querySelector('#overlay input[name=database]').value") == "catalog", "Use lost database")
            ab("press", "Escape")
        finally:
            js("import('/piper/dist/dbview.js').then(({dbApi})=>{dbApi.connections=window.__ideaConnections;})")
        palette_run(DEMO_TITLE)
        palette_run("Grid: choose panes…")
        initial = js("[...document.querySelectorAll('#overlay input[data-id]')].filter(e=>/^db:[1-3]$/.test(e.dataset.id)).length")
        expect("database unused slots hidden", initial == 0, "uncreated database slots shown")
        add = '#overlay .grid-db-add'
        ab("scrollintoview", add)
        expect("database add row size", js("document.querySelector(%s).getBoundingClientRect().height >= 36" % jq(add)), "add action shrank inside the scrolling list")
        shot("database-add-default")
        ab("hover", add)
        shot("database-add-hover")
        js("document.querySelector(%s).focus()" % jq(add))
        ab("press", "Tab")
        ab("press", "Shift+Tab")
        expect("database add keyboard focus", js("document.activeElement.matches(%s)" % jq(add)), "add action not reachable by keyboard")
        shot("database-add-focus")
        expect("database add draft", ab("press", "Enter").get("success"), "add keyboard action failed")
        expect("database new draft checked", js("document.querySelector('#overlay input[data-id=\"db:1\"]')?.checked"), "new panel not selected")
        shot("database-panel-chooser")
        click("#overlay button", "Cancel")
        palette_run("Grid: choose panes…")
        expect("database cancel", not js("!!document.querySelector('#overlay input[data-id=\"db:1\"]')"), "Cancel created a panel")
        click("#overlay button", "Cancel")


def compact_sessions(widths: list[int]) -> None:
    """Real queue interactions, including an obsolete persisted filter and hidden ended results."""
    for width in widths:
        CTX.update(preset="all", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        js("localStorage.setItem('piper.queue.filter', 'ci'); localStorage.setItem('piper.queue.ended', '0')")
        load_preset("all")
        grid(False)
        whole_queue(True)
        expect("sessions no filters", js("!document.querySelector('#queue .q-filters')"), "filter pills remain")
        expect("sessions compact toolbar", js("document.querySelector('#queue .q-title h2')?.textContent === 'Overview' && !!document.querySelector('#queue .q-title .q-cmdk') && !!document.querySelector('#queue .q-title .q-collapse')"), "Overview toolbar is incomplete")
        expect("sessions visible collapse icon", js("(() => { const icon = document.querySelector('#queue .q-collapse svg'); return !!icon && icon.getBoundingClientRect().width >= 16 && getComputedStyle(icon).stroke !== 'none'; })()"), "collapse icon is not drawn")
        expect("sessions no workflow labels", js("![...document.querySelectorAll('#queue .qi-meta')].some(x => x.title.startsWith('next step:'))"), "workflow next-step labels remain")
        expect("sessions persistent search", js("document.querySelector('#queue .q-search')?.getBoundingClientRect().width > 80"), "search is not visible")
        expect("sessions ignore old filter", js("!!document.querySelector('#queue .qi[data-sid=%s]')" % jq(DEMO_SID)), "old CI filter hid sessions")
        # F01 (9.64): a running row's second line is its state + time ("ready 4 min", working: the time alone), never the message
        expect("sessions no preview", js("[...document.querySelectorAll('#queue .running .qi:not(.g-stuck) .qi-sub')].every(x => /^([a-z][a-z ]*)?(now|\\d+ (min|h|d))$/.test(x.textContent.trim()))"), "ordinary session still shows its last message")
        # F01: the project chip is gone from these rows - the title starts with the project's last segment ("demo — …")
        proj = {x["id"]: x["project"].split("/")[-1] for x in api("/api/state")["sessions"]}
        rows = js("JSON.stringify([...document.querySelectorAll('#queue .running .qi[data-sid]')].map(r => [r.dataset.sid, r.querySelector('.qi-title').textContent.trim()]))") or []
        expect("sessions flat metadata", js("!document.querySelector('#queue .q-group')") and bool(rows) and all(t.startswith(proj.get(sid, "?")) for sid, t in rows),
               "project missing from a running row's title: %r" % rows)
        expect("header no status summary", js("!document.querySelector('#main > header .dh-state') && !!document.querySelector('#main > header .st[aria-label]')"), "header still duplicates metadata or lacks accessible status")
        scan("compact sessions")
        shot("compact-sessions")
        terminal_app = api("/api/state").get("terminal_app")
        attach_label = "Attach in " + terminal_app if terminal_app else "Open in terminal"
        active_sid = js("JSON.parse(localStorage.getItem('piper.last')).active")
        expect("attach absent from header", js("![...document.querySelectorAll('#main > header button')].some(b => b.textContent === %s)" % jq(attach_label)), "attach is still a separate header button")
        js(r"""(() => {
          window.__attachOriginalFetch = window.fetch;
          window.__attachCalls = [];
          window.fetch = (url, opts) => {
            if (new URL(typeof url === 'string' ? url : url.url, location.href).pathname.endsWith('/open-terminal')) {
              window.__attachCalls.push({url: String(url), method: opts?.method});
              return Promise.resolve(new Response(JSON.stringify({ok:true, terminal:'test terminal'}), {status:200, headers:{'Content-Type':'application/json'}}));
            }
            return window.__attachOriginalFetch(url, opts);
          };
        })()""")
        try:
            expect("attach overflow menu", ab("click", '#main > header [aria-label="more actions"]').get("success"), "more actions did not open")
            expect("attach visible in menu", js("[...document.querySelectorAll('[role=menu] [role=menuitem] .grow')].some(b => b.textContent.trim() === %s)" % jq(attach_label)), "attach is missing from overflow menu")
            shot("attach-overflow")
            expect("attach menu invokes action", click('[role=menu] [role=menuitem] .grow', attach_label), "attach item could not be clicked")   # F28 (9.80): an icon column before the label
            calls = js("window.__attachCalls")
            expect("attach correct session request", calls == [{"url": "/api/sessions/%s/open-terminal" % active_sid, "method": "POST"}], "attach invoked the wrong request")
        finally:
            js("window.fetch = window.__attachOriginalFetch; delete window.__attachOriginalFetch")
            ab("press", "Escape")
        expect("sessions search", ab("fill", "#queue .q-search", "grp/demo").get("success"), "search input failed")
        expect("sessions query", js("[...document.querySelectorAll('#queue .qi')].every(r => r.textContent.includes('grp/demo')) && !!document.querySelector('#queue .qi')"), "query did not filter rows")
        expect("sessions search retains focus", js("document.activeElement.classList.contains('q-search') && document.activeElement.value === 'grp/demo'"), "render lost search focus/value")
        ab("press", "Escape")
        expect("sessions Escape clears", js("document.querySelector('#queue .q-search').value === ''"), "Escape did not reset search")
        expect("sessions keyboard search", ab("press", "/").get("success") and js("document.activeElement.classList.contains('q-search')"), "/ did not focus search")
        ended = next((x for x in api("/api/state")["sessions"] if x["state"] == "exited" and not x.get("hidden")), None)
        expect("sessions ended fixture", bool(ended), "no ended session in fixture")
        if ended:
            ab("fill", "#queue .q-search", ended["id"])
            expect("sessions search ended", js("!!document.querySelector('#queue .ended .qi[data-sid=%s]')" % jq(ended["id"])), "search did not reveal a folded ended session")
            scan("sessions ended search")
            shot("sessions-ended-search")
            ab("press", "Escape")
        # 9.136: dead things wait folded - the queue's ended sessions and the palette's Ended group, until a click
        expect("sessions ended folded", js("document.querySelector('#queue .ended .q-fold').getAttribute('aria-expanded') === 'false'"), "Ended was not folded")
        expect("sessions palette ended folded", ab("click", "#queue .q-cmdk").get("success") and js(
            "!!document.querySelector('#overlay.open .pitem.punfold') && ![...document.querySelectorAll('#overlay.open .pitem .plabel')].some(x => x.textContent === %s)"
            % jq(ended["title"] if ended else "")), "the palette listed the ended sessions unfolded")
        if ended:
            js("document.querySelector('#overlay.open .pitem.punfold').click()")
            expect("sessions palette ended unfold", js("[...document.querySelectorAll('#overlay.open .pitem .plabel')].some(x => x.textContent === %s)" % jq(ended["title"])),
                   "the \"N ended\" row did not open the group")
        close_overlay()
        ab("click", "#queue .ended .q-fold")
        expect("sessions expand ended", js("document.querySelector('#queue .ended .q-fold').getAttribute('aria-expanded') === 'true'"), "Ended did not expand")
        expect("sessions resume picker", js("!!document.querySelector('#queue [aria-label=\"Find ended conversations\"]')"), "resume picker disappeared")
        ab("click", "#queue .ended .q-fold")
        expect("sessions new button", ab("click", "#queue .q-new").get("success"), "new-session button failed")
        expect("sessions new dialog", overlay_open(), "new-session dialog did not open")
        close_overlay()
        expect("sessions commands", ab("click", "#queue .q-cmdk").get("success") and overlay_open(), "commands did not open")
        close_overlay()
        expect("sessions toolbar collapse", ab("click", "#queue .q-collapse").get("success") and js("document.body.classList.contains('queue-mini')"), "toolbar did not fold the list")
        ab("click", '#actbar button[aria-label="Overview"]')
        expect("sessions toolbar reopen", js("!document.body.classList.contains('queue-mini')"), "list did not reopen")
        ab("fill", "#queue .q-search", DEMO_TITLE)
        ab("press", "Enter")
        expect("sessions keyboard opens", js("JSON.parse(localStorage.getItem('piper.last')).active") == DEMO_SID, "Enter opened the wrong session")
        ab("fill", "#queue .q-search", "")
        row = '#queue .qi[data-sid="%s"]' % DEMO_SID
        ab("scrollintoview", row)
        js("document.querySelector(%s).dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true, clientX:150, clientY:200}))" % jq(row))
        expect("sessions context menu", js("!!document.querySelector('[role=menu]')"), "session context menu failed")
        ab("press", "Escape")
        icon = '#actbar button[aria-label="Overview"]'
        ab("click", icon)
        expect("sessions fold", js("document.body.classList.contains('queue-mini')"), "Overview did not fold the list")
        ab("click", icon)
        expect("sessions reopen", js("!document.body.classList.contains('queue-mini')"), "Overview did not reopen the list")
        # Render edge cases through the normal polling path, without mutating the test daemon.
        js(r"""(() => {
          const real = window.fetch.bind(window);
          window.__sidebarConfirm = false;
          window.fetch = async (input, init) => {
            if (String(input).includes('/api/board')) {   // F01: a row's title is "project — sentence" - no sentence, so the titles show
              const headers = new Headers(init?.headers); headers.delete('If-None-Match');
              const response = await real(input, {...init, headers}), data = await response.json();
              for (const c of data.cards || []) { c.last = ''; c.asked = ''; }
              return new Response(JSON.stringify(data), {status: response.status, headers: response.headers});
            }
            if (!String(input).includes('/api/state')) return real(input, init);
            const headers = new Headers(init?.headers); headers.delete('If-None-Match');
            const response = await real(input, {...init, headers}), data = await response.json();
            const demo = data.sessions.find(x => x.id === %s);
            const waiting = data.sessions.find(x => x.project === 'demo/hidden');
            demo.title = 'Split the ordering api into a frontend / backend part';
            demo.since = Date.now() / 1000 - 18000;
            demo.issue = {iid: 4821, type: 'issue', title: demo.title, gl: 'demo', web_url: '#'};
            waiting.title = 'Configurator: validate the personal questionnaire'; waiting.state = 'waiting';
            const action = (session, level, body) => ({key: 'fixture:' + session.id, kind: 'action', level, source: level === 'confirm' ? 'confirm' : 'session',
              title: session.title, body, since: Date.now() / 1000 - 21600, actions: [],
              subject: {type: 'session', key: session.id, session: session.id, label: session.title}});
            data.queue.actions = data.queue.actions.filter(a => ![demo.id, waiting.id].includes(a.subject.session || a.subject.key));
            data.queue.actions.push(action(waiting, 'waiting', 'Last message must not be visible'));
            if (window.__sidebarConfirm) {
              const confirmation = action(demo, 'confirm', 'edit_file src/App.java\n- old text\n+ new text');
              confirmation.title = 'Allow edit_file: src/App.java';
              confirmation.actions = [false, true].map(ok => ({op:'decide', label: ok ? 'Allow' : 'Deny', args: {id: 'fixture-only', ok}}));
              data.queue.actions.push(confirmation);
            }
            return new Response(JSON.stringify(data), {status: response.status, headers: response.headers});
          };
        })()""" % jq(DEMO_SID))
        expect("sessions wrapped title", ab("wait", "--fn", "[...document.querySelectorAll('#queue .qi-title')].some(x => x.textContent.includes('Split the ordering'))").get("success"), "fixture title did not render")
        # F01: a waiting row's second line is its state + time, never the last message
        expect("sessions no waiting preview", js("[...document.querySelectorAll('#queue .needs .qi:not(.confirm) .qi-sub')].every(x => /^(turn done|question|asks) /.test(x.textContent))"), "waiting row still shows a preview")
        expect("sessions title two lines", js("(() => { const t = [...document.querySelectorAll('#queue .qi-title')].find(x => x.textContent.includes('Split the ordering')); return t.clientHeight <= 2 * parseFloat(getComputedStyle(t).lineHeight) + 1; })()"), "title exceeded two lines")
        ab("mouse", "move", "600", "400")
        ab("click", "#main > header .title")
        scan("sessions long titles")
        shot("sessions-long-titles")
        js("window.__sidebarConfirm = true")
        # F01: the row is "project — the card's fact" + "asks … · Allow / Deny ⌘J"; the whole body (the diff) is only the
        # floating card's (⌘J) - which this queue-only fixture (no /api/state pending) does not get, so it is not looked for
        expect("sessions confirmation body", ab("wait", "--fn", "(() => { const r = document.querySelector('#queue .qi.confirm'); return !!r && r.querySelector('.qi-title').textContent.includes('edit_file src/App.java') && /^asks /.test(r.querySelector('.qi-sub')?.textContent ?? '') && !r.textContent.includes('+ new text'); })()").get("success"), "confirmation row lost its fact / status or repeats the body")
        expect("sessions confirmation buttons", js("document.querySelectorAll('#queue .qi.confirm .qi-ops button').length === 2"), "decision buttons were lost")
        scan("sessions confirmation")
        shot("sessions-confirmation")


def inspector_last_turn(widths: list[int]) -> None:
    """A populated Last turn stays folded at the bottom; manual choices survive polling."""
    section = '#inspector .sec[data-k="turn"]'
    toggle = section + ' .sec-tg'
    for width in widths:
        CTX.update(preset="all", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        load_preset("all")  # fresh page: a previous manual open must not become the default
        js("""(() => {
          const real = window.fetch.bind(window);
          window.fetch = (input, init) => {
            if (!String(input).includes('/api/state')) return real(input, init);
            const headers = new Headers(init?.headers); headers.delete('If-None-Match');
            return real(input, {...init, headers});
          };
        })()""")  # force fresh state: an unchanged fixture otherwise correctly returns 304
        if js("document.body.classList.contains('no-inspector')"):
            expect("context panel opens", ab("click", "[aria-label='context panel']").get("success"), "context toggle failed")
        expect("Last turn has content", ab("wait", "--fn", "!!document.querySelector(%s + ' .wk-asked') && !!document.querySelector(%s + ' .wk-last')" % (jq(section), jq(section))).get("success"), "demo prompt/answer did not load")

        def check(opened):
            expect("Last turn position", js("document.querySelector('#inspector').lastElementChild === document.querySelector(%s)" % jq(section)), "Last turn is not the final section")
            expect("Last turn fold", js("document.querySelector(%s).getAttribute('aria-expanded')" % jq(toggle)) == str(opened).lower(), "unexpected fold state")

        def next_poll():
            js("window.__lastTurnNode = document.querySelector(%s)" % jq(section))
            expect("Last turn poll", ab("wait", "--fn", "window.__lastTurnNode !== document.querySelector(%s)" % jq(section)).get("success"), "inspector did not refresh")

        check(False)
        next_poll()
        check(False)
        ab("scrollintoview", toggle)
        scan("Last turn collapsed")
        shot("last-turn-collapsed")
        expect("Last turn manual open", ab("click", toggle).get("success"), "manual open failed")
        check(True)
        next_poll()
        check(True)
        expect("Last turn content visible", js("document.querySelector(%s + ' .wk-asked').getBoundingClientRect().height > 0" % jq(section)), "expanded prompt is hidden")
        ab("scrollintoview", toggle)
        shot("last-turn-open")
        expect("Last turn manual close", ab("click", toggle).get("success"), "manual close failed")
        next_poll()
        check(False)
        other = next(s for s in api("/api/state")["sessions"] if s.get("project") == "demo/hidden")
        expect("Last turn session switch", palette_run(other.get("title") or other["project"]), "second session did not open")
        check(False)
        expect("Last turn return", palette_run(DEMO_TITLE), "demo session did not reopen")
        check(False)


def plan_limits(widths: list[int]) -> None:
    """Real piperd + GUI, deterministic usage responses; never contact provider services."""
    load_preset("all")
    js(r"""(() => {
      const real = window.fetch.bind(window), now = Date.now() / 1000;
      const quota = (session, week) => ({v: 1, status: 'ok', fetched_at: now, plan: 'pro',
        session: {percent: session, resets_at: now + 7200, severity: 'normal'},
        week: {percent: week, resets_at: now + 200000, severity: 'normal'}, scoped: []});
      window.__planLimits = {claude: quota(4, 54), codex: quota(24, 61)};
      window.__planLimits.claude.scoped = [{label: 'Fable', percent: 90, resets_at: now + 86400, severity: 'warning'}];
      window.__planDefaultCodex = structuredClone(window.__planLimits.codex);
      window.fetch = async (input, init) => {
        if (new URL(String(input), location.href).pathname !== '/api/state') return real(input, init);
        const headers = new Headers(init?.headers); headers.delete('If-None-Match');
        const response = await real(input, {...init, headers}), state = await response.json();
        state.limits = window.__planLimits.claude;
        state.codex_limits = window.__planLimits.codex;
        return new Response(JSON.stringify(state), {status: 200, headers: {'Content-Type': 'application/json'}});
      };
      window.__restorePlanLimits = () => {window.fetch = real;};
    })()""")
    def wait_for(expression):
        expect("plan limits state", ab("wait", "--fn", expression).get("success"), expression)
    visible = "e => !e.hidden && e.getBoundingClientRect().width > 0"
    codex_row = ".ql-provider[data-provider=codex]"
    try:
        for width in widths:
            CTX.update(preset="all", width=width)
            ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
            palette_run("Queue")   # F18 (9.79): the four "Queue: …" rows are one, its run = the whole queue
            wait_for("[...document.querySelectorAll('.ql-provider')].filter(%s).length === 2" % visible)
            wait_for("document.querySelector('.ql-provider[data-provider=claude] .ql-cell:last-child .ql-pct').textContent === '54 %'")
            expect("compact limits height", js("document.querySelector('.q-limits').getBoundingClientRect().height <= 84"), "usage block exceeds 84 px")
            scan("plan limits expanded")
            shot("plan-limits-expanded")
            expect("Claude limits hover", ab("hover", ".ql-provider[data-provider=claude]").get("success"), "Claude hover failed")
            wait_for("!document.querySelector('.qm-peek').hidden && document.querySelector('.qm-peek').textContent.includes('Week · Fable90 %')")
            expect("Claude Weekly hover", js("document.querySelector('.qm-peek').textContent.includes('Week54 %')"), "all-model Weekly missing in detail")
            shot("plan-limits-claude-hover")
            ab("hover", "#actbar")
            js("document.activeElement?.blur(); document.querySelector(%s).focus()" % jq(codex_row))
            wait_for("!document.querySelector('.qm-peek').hidden")
            settle(2)  # a state poll must not dismiss a focused provider's detail
            expect("limits focus survives poll", js("!document.querySelector('.qm-peek').hidden"), "poll dismissed the focused detail")
            expect("limits keyboard detail", js("document.querySelector('.qm-peek').textContent.includes('Codex limits · pro') && document.querySelector('.qm-peek').textContent.includes('in 1h')"), "Codex detail/reset missing")
            shot("plan-limits-focus")
            ab("press", "Escape")
            wait_for("document.querySelector('.qm-peek').hidden")
            whole_queue(False)
            wait_for("[...document.querySelectorAll('.qm-lim')].filter(%s).length === 2" % visible)
            expect("Claude Weekly strip", js("document.querySelector('.qm-lim[data-provider=claude] .qml-bar:last-child b').style.width === '54%'"), "strip uses scoped quota instead of Weekly")
            scan("plan limits strip")
            shot("plan-limits-strip")
            # A stale payload still contains Codex: the frontend must enforce Features itself.
            api("/api/features", {"modules": {"codex": False}, "onboarded": True})
            wait_for("document.querySelector('.qm-lim[data-provider=codex]').hidden")
            expect("Codex API off", api("/api/state")["codex_limits"] is None, "backend still exposes limits")
            whole_queue(True)
            expect("Codex row off", js("document.querySelector(%s).hidden" % jq(codex_row)), "Codex row visible with Features off")
            shot("plan-limits-codex-off")
            api("/api/features", {"modules": {"codex": True}, "onboarded": True})
            wait_for("!document.querySelector(%s).hidden" % jq(codex_row))
            js("window.__planLimits.codex.status = 'expired'")
            wait_for("document.querySelector(%s).classList.contains('dim')" % jq(codex_row))
            expect("stale keeps numbers", js("document.querySelector(%s).textContent.includes('24 %%')" % jq(codex_row)), "stale values disappeared")
            shot("plan-limits-stale")
            js("window.__planLimits.codex.status = 'ok'")
            js("window.__planLimits.codex.session = null")
            wait_for("document.querySelector('.ql-provider[data-provider=codex] .ql-pct').textContent === '– %'")
            whole_queue(False)
            wait_for("document.querySelector('.qm-lim[data-provider=codex] .qml-pct').textContent === '61%'")
            scan("plan limits weekly-only strip")
            shot("plan-limits-weekly-only")
            whole_queue(True)
            js("window.__planLimits.codex = structuredClone(window.__planDefaultCodex)")
            api("/api/features", {"modules": {"usage": False}, "onboarded": True})
            wait_for("document.querySelector('.q-limits').hidden")
            whole_queue(False)
            wait_for("document.querySelector('.qm-limits').hidden")
            whole_queue(True)
            api("/api/features", {"modules": {"usage": True}, "onboarded": True})
    finally:
        js("window.__restorePlanLimits()")


def navigation_pins(widths: list[int]) -> None:
    """Actual UI clicks on a sandboxed piperd; only deterministic badge/availability responses are patched."""
    trigger = '#actbar button[aria-label="All tools"]'
    def wait_for(expr):
        return expect("navigation state", ab("wait", "--fn", expr).get("success"), expr)
    def tool(tool_id):
        return '.nav-tools button[data-menu-id="%s"]:not(.msecondary)' % tool_id
    def pin(tool_id):
        return '.nav-tools .msecondary[data-menu-id="%s"]' % tool_id
    def menu():
        # the picker closes on a window blur, and a freshly opened / switched tab can still get one after the click
        # (CI: the peer tab's picker never showed, its pin was lost) - click again while it is not there
        for _ in range(3):
            if not js("!!document.querySelector('.nav-tools')"):
                expect("navigation open", ab("click", trigger).get("success"), "All tools did not open")
            end = time.time() + 2
            while time.time() < end and not js("!!document.querySelector('.nav-tools')"):
                time.sleep(0.1)
            if js("!!document.querySelector('.nav-tools')"):
                break
        wait_for("!!document.querySelector('.nav-tools')")
        expect("expanded tools trigger", js("document.querySelector(%s).matches('.expanded[aria-expanded=true]')" % jq(trigger)), "open menu lost its trigger state")
    def pinned():
        return js("JSON.parse(localStorage.getItem('piper.navigation.pins') || '[\"db\",\"host\"]')")
    for width in widths:
        CTX.update(preset="all", width=width)
        load_preset("all")
        js("localStorage.removeItem('piper.navigation.pins')")
        reload()
        wait_for("!!document.querySelector(%s)" % jq(trigger))
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        # the rail renders before /api/state brings the modules (CI: the second width read it too early) - wait for it
        expect("default navigation", ab("wait", "--fn", "!!document.querySelector('#actbar [aria-label=Database]') && !!document.querySelector('#actbar [aria-label=\"Host shells\"]') && !document.querySelector('#actbar [aria-label=Usage]')").get("success"), "default pins differ from Database + Host shells")
        # F01 (9.64): the folded queue's plan limits (.qm-limits) sit by the bell, between All tools and Settings
        expect("overflow beside settings", js("(() => {const all=[...document.querySelector('.ab-foot').children], f=all.filter(e=>!e.matches('.qm-limits')), l=all.findIndex(e=>e.matches('.qm-limits')); return f.map(e=>e.getAttribute('aria-label')).join('|')==='All tools|Settings|Recent alerts' && (l < 0 || l === 1) && f[0].getBoundingClientRect().top > innerHeight/2})()"), "overflow is not at the bottom above Settings")
        menu()
        expect("separate controls", js("!document.querySelector('.nav-tools button button') && document.querySelectorAll('.nav-tools .msecondary').length >= 7"), "pin is nested or a tool is missing")
        scan("navigation all tools")
        shot("navigation-all-tools")
        ab("click", pin("audit"))
        wait_for("!!document.querySelector('#actbar [aria-label=Audit]')")
        expect("pin keeps menu", js("!!document.querySelector('.nav-tools') && document.activeElement.getAttribute('aria-label')==='Unpin Audit'"), "pin closed the menu or lost focus")
        settle(2)
        expect("pin survives poll", js("!!document.querySelector('.nav-tools')"), "poll closed the tools picker")
        ab("click", pin("db"))
        expect("pin persists", pinned() == ["host", "audit"], "wrong saved pins/order: %r" % pinned())
        ab("press", "Escape")
        expect("escape focus", js("!document.querySelector('.nav-tools') && document.activeElement.getAttribute('aria-label')==='All tools' && document.activeElement.getAttribute('aria-expanded')==='false'"), "Escape did not restore trigger focus")
        reload()
        wait_for("!!document.querySelector('#actbar [aria-label=Audit]')")
        expect("reload pins", not js("!!document.querySelector('#actbar [aria-label=Database]')"), "reload restored an unpinned tool")
        # Real same-origin tab: the browser emits storage events to the other document.
        ab("tab", "new", "--label", "navigation-peer", BASE + "/")
        wait_for("!!document.querySelector('#actbar [aria-label=Audit]')")
        menu(); ab("click", pin("usage")); ab("press", "Escape")
        ab("tab", "t1")
        wait_for("!!document.querySelector('#actbar [aria-label=Usage]')")
        menu(); ab("click", pin("usage")); ab("press", "Escape")
        ab("tab", "navigation-peer")
        wait_for("!document.querySelector('#actbar [aria-label=Usage]')")
        ab("tab", "close", "navigation-peer")
        ab("tab", "t1")
        menu()
        ab("press", "a"); ab("press", "u"); ab("press", "d")
        expect("tools search", js("document.querySelectorAll('.nav-tools button:not(.msecondary)').length===1 && document.activeElement.dataset.menuId==='audit'"), "speed search did not filter Audit")
        ab("press", "ArrowRight")
        expect("keyboard pin column", js("document.activeElement.classList.contains('msecondary')"), "Right did not focus pin")
        ab("press", "Space")
        expect("keyboard unpin", "audit" not in pinned() and js("document.activeElement.getAttribute('aria-label')==='Pin Audit'"), "Space did not unpin a filtered tool")
        ab("press", "ArrowLeft"); ab("press", "Enter")
        wait_for("JSON.parse(localStorage.getItem('piper.last')).view==='audit'")
        expect("hidden active destination", js("document.querySelector(%s).getAttribute('aria-pressed')==='true' && !document.querySelector('.nav-tools')" % jq(trigger)), "hidden Audit did not light All tools")
        menu()
        ab("click", pin("db"))
        api("/api/features", {"modules": {"db": False}, "onboarded": True})
        wait_for("!document.querySelector('#actbar [aria-label=Database]') && !document.querySelector('.nav-tools [data-menu-id=db]')")
        expect("disabled pin retained", "db" in pinned(), "module disable erased the pin")
        api("/api/features", {"modules": {"db": True}, "onboarded": True})
        wait_for("!!document.querySelector('#actbar [aria-label=Database]') && !!document.querySelector('.nav-tools [data-menu-id=db]')")
        ab("press", "Escape")
        # Storage events use the same path as another tab; reload separately checks durable storage.
        js("localStorage.setItem('piper.navigation.pins','[]'); window.dispatchEvent(new StorageEvent('storage',{key:'piper.navigation.pins'}))")
        expect("empty pin set", js("!document.querySelector('.ab-main [aria-label=Database]') && !document.querySelector('.ab-main [aria-label=\"Host shells\"]')"), "empty choice reverted to defaults")
        menu()
        ab("click", tool("host"))
        wait_for("JSON.parse(localStorage.getItem('piper.last')).view==='host'")
        expect("hidden host danger", js("document.querySelector(%s).matches('.host.on')" % jq(trigger)), "hidden host lost its danger indication")
        # Walk every tool's real action. External stacks is intercepted at the browser launch boundary.
        for tool_id, target in [("db", "db"), ("usage", "usage"), ("time", "timesheet"), ("audit", "audit"), ("prod", "prod")]:
            menu()
            expect("navigation " + tool_id, ab("click", tool(tool_id)).get("success"), "destination missing")
            wait_for("JSON.parse(localStorage.getItem('piper.last')).view===%s" % jq(target))
            expect("navigation active " + tool_id, js("document.querySelector(%s).getAttribute('aria-pressed')==='true'" % jq(trigger)), "hidden destination is not selected")
        menu(); ab("click", tool("keys"))
        expect("keys destination", overlay_open(), "Keys did not open shortcuts")
        close_overlay()
        # Poll deterministic stack availability and production badge. Never open a real dashboard.
        js(r"""(() => {const real=window.fetch.bind(window); window.__navRestore=()=>{window.fetch=real};
          window.__navBadge=7; window.__navStack=true;
          window.__PIPER__.dashboardUrl=window.__PIPER__.dashboardUrl||'http://127.0.0.1:9/stacks';   /* an extension's provider page */
          window.fetch=async(input,init)=>{if(new URL(String(input),location.href).pathname!=='/api/state')return real(input,init);
          const headers=new Headers(init?.headers);headers.delete('If-None-Match');
          const r=await real(input,{...init,headers}),s=await r.json();s.prod={...s.prod,on:true,new:window.__navBadge};s.stack_up=window.__navStack;
          s.features={...s.features,modules:{...s.features?.modules,stack:true}};   /* the stack module comes from an extension (9.150) */
          return new Response(JSON.stringify(s),{status:200,headers:{'Content-Type':'application/json'}})};})()""")
        wait_for("document.querySelector(%s+' .ab-badge')?.textContent==='7'" % jq(trigger))
        menu()
        wait_for("!!document.querySelector(%s)" % jq(tool("stacks")))
        js("window.__navOpen=window.open; window.__navOpened=null; window.open=(...args)=>{window.__navOpened=args; return null}")
        ab("click", tool("stacks"))
        expect("stacks destination", js("window.__navOpened?.[0]===window.__PIPER__.dashboardUrl && window.__navOpened?.[1]==='dev-stacks' && window.__navOpened?.[2]==='noopener'"), "Dev stacks did not open the configured dashboard safely")
        js("window.open=window.__navOpen")
        menu(); ab("click", pin("stacks"))
        expect("pinned stacks link", js("(()=>{const a=document.querySelector('#actbar a[aria-label=\"Dev stacks\"]');return a?.target==='dev-stacks'&&a.rel==='noopener'&&a.href===window.__PIPER__.dashboardUrl})()"), "pinned dashboard link lost its target or URL")
        js("window.__navStack=false")
        wait_for("!document.querySelector('#actbar [aria-label=\"Dev stacks\"]') && !document.querySelector('.nav-tools [data-menu-id=stacks]')")
        expect("unavailable stacks pin retained", "stacks" in pinned(), "stack availability erased the pin")
        js("window.__navStack=true")
        wait_for("!!document.querySelector('#actbar [aria-label=\"Dev stacks\"]')")
        ab("click", pin("prod"))
        wait_for("!document.querySelector(%s+' .ab-badge') && document.querySelector('#actbar [aria-label=Production] .ab-badge')?.textContent==='7'" % jq(trigger))
        ab("press", "Escape")
        menu(); ab("click", trigger)
        expect("second click closes", not js("!!document.querySelector('.nav-tools')"), "second click left menu open")
        menu(); ab("click", "#actbar .ab-brand")
        expect("outside closes", not js("!!document.querySelector('.nav-tools')"), "outside click left menu open")
        menu(); ab("click", '#actbar button[aria-label="Settings"]')
        wait_for("JSON.parse(localStorage.getItem('piper.last')).view==='settings'")
        expect("outside navigation closes", not js("!!document.querySelector('.nav-tools')"), "Settings click left menu open")
        menu(); ab("click", '#actbar button[aria-label="Overview"]')
        wait_for("JSON.parse(localStorage.getItem('piper.last')).view==='term'")
        # Pin every available destination and ensure fixed utilities remain reachable in a short viewport.
        menu()
        ids = js("[...document.querySelectorAll('.nav-tools .msecondary[aria-checked=false]')].map(e=>e.dataset.menuId)") or []
        for tool_id in ids:
            ab("click", pin(tool_id))
        ab("press", "Escape")
        ab("set", "viewport", "390", "480")
        js("document.querySelector('.ab-main').scrollTop=999; window.__navScroll=document.querySelector('.ab-main').scrollTop; window.__navBadge=8")
        wait_for("document.querySelector('#actbar [aria-label=Production] .ab-badge')?.textContent==='8'")
        expect("poll preserves rail scroll", js("window.__navScroll>0 && document.querySelector('.ab-main').scrollTop===window.__navScroll"), "badge update reset the scrolled pins")
        expect("short rail utility reach", js("[...document.querySelectorAll('.ab-foot button')].every(e=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight&&(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===e||e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)))})"), "fixed utilities are unreachable with all pins: " + str(js("JSON.stringify([...document.querySelectorAll('.ab-foot button')].map(e=>{const r=e.getBoundingClientRect(),x=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return [e.getAttribute('aria-label'),Math.round(r.top),Math.round(r.bottom),innerHeight,x===e||e.contains(x)?'ok':(x?.id||x?.className||x?.tagName)]}))")))
        menu()
        expect("short popup bounds", js("(()=>{const r=document.querySelector('.nav-tools').getBoundingClientRect();return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})()"), "tools popup outside short window")
        shot("navigation-short-all-pins")
        js("window.__navRestore()")
        ab("press", "Escape")
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
        js("localStorage.removeItem('piper.navigation.pins'); window.dispatchEvent(new StorageEvent('storage',{key:'piper.navigation.pins'}))")
        palette_run(DEMO_TITLE)
        menu()
        js("import('/piper/dist/theme.js').then(m=>m.applyTheme(window.__PIPER__?.themes,'light'))")
        settle(); scan("navigation light"); shot("navigation-light")
        js("import('/piper/dist/theme.js').then(m=>m.applyTheme(window.__PIPER__?.themes,'dark'))")
        ab("press", "Escape")
        palette_run(DEMO_TITLE)
        whole_queue(True)
        scan("navigation default"); shot("navigation-default")


def overview_toggle() -> None:
    palette_run(DEMO_TITLE)
    grid(False)
    palette_run("Queue")   # F18 (9.79): the four "Queue: …" rows are one, its run = the whole queue
    icon = '#actbar button[aria-label="Overview"]'
    expect("overview active", js("document.querySelector(%s).getAttribute('aria-pressed') === 'true'" % jq(icon)), "Overview not active")
    shot("overview-expanded")
    expect("overview collapse click", ab("click", icon).get("success"), "icon click failed")
    settle()
    expect("overview collapsed", js("document.body.classList.contains('queue-mini') && !document.body.classList.contains('no-queue') && document.querySelector(%s).getAttribute('aria-pressed') === 'false'" % jq(icon)), "active Overview did not fold to the session strip")
    scan("overview collapsed")
    shot("overview-collapsed")
    expect("overview reopen click", ab("click", icon).get("success"), "icon click failed")
    settle()
    expect("overview reopened", js("!document.body.classList.contains('queue-mini') && !document.body.classList.contains('no-queue') && document.querySelector(%s).getAttribute('aria-pressed') === 'true'" % jq(icon)), "Overview did not reopen the whole queue")
    palette_run("Queue")   # F18 (9.79): the four "Queue: …" rows are one, its run = the whole queue
    expect("overview palette stays expanded", js("!document.body.classList.contains('queue-mini')"), "palette folded the queue")
    scan("overview reopened")
    mods = api("/api/state")["features"]["modules"]
    sources = [("Project files", "files"), ("Conversation", "convo"), ("Settings", "settings")]
    if mods.get("db"):
        sources.append(("Database", "db"))
    if mods.get("gitlab") or mods.get("github"):
        forge = forge_name(api("/api/state").get("inbox"))
        sources.append(("Inbox" if forge == "Inbox" else forge + " inbox", "gitlab"))
    if mods.get("grid"):
        sources.append((None, "grid"))
    for label, source in sources:
        grid_on = "document.querySelector('#main .body').classList.contains('grid')"
        # the palette's Grid row can miss right after another view (seen at 1024 px): retry once, then the grid must show
        opened = palette_run(label) and view() == source if label else (grid(True) and js(grid_on)) or (grid(True) and js(grid_on))
        expect("overview source " + source, opened, "source view did not open")
        expect("overview return click " + source, ab("click", icon).get("success"), "icon click failed")
        settle()
        expect("overview return " + source, view() == "term" and js("document.querySelector(%s).getAttribute('aria-pressed') === 'true'" % jq(icon)), "Overview did not return to the session with the whole queue")
        if source == "grid":
            expect("overview opens the queue beside the grid", js(grid_on), "Overview turned the grid off")
        expect("overview selected session " + source, js("JSON.parse(localStorage.getItem('piper.last')).active") == DEMO_SID, "Overview changed the selected session")
        expect("overview collapse after " + source, ab("click", icon).get("success"), "second click failed")
        settle()
        expect("overview folded after " + source, view() == "term" and js("document.body.classList.contains('queue-mini')"), "second click did not fold the queue")
        if source == "grid":
            expect("overview keeps the grid", js(grid_on), "Overview turned the grid off")
        scan("overview return and fold " + source)
        shot("overview-return-and-fold-" + source)
    # ends in the strip, as the walk found it (a whole queue left open hid later checks' state, e.g. phone 390)


def settings_redesign(widths) -> None:
    """Exercise real settings without changing the user's Piper home or configuration."""
    load_preset("all")
    expect("settings opens", palette_run("Settings"), "Settings did not open")
    # Decline the fixture's agent-start request so it does not cover settings screenshots.
    click_start('#pending button', 'Deny')
    for width in widths:
        CTX.update(preset="settings-A", width=width)
        ab("set", "viewport", str(width), str(HEIGHT.get(width, 900)))
        for theme in ("dark", "light"):
            api('/api/settings', {"section": "interface", "values": {"theme": theme}})
            reload()
            settle()
            palette_run('Settings')
            if js("document.querySelector('.st-gfold')?.getAttribute('aria-expanded') === 'false'"):
                click('.st-gfold')
            ids = js("[...document.querySelectorAll('[data-nav]')].map(e=>e.dataset.nav)")
            for sid in ids:
                click('[data-nav="%s"]' % sid)
                if sid == 'review':
                    expect('settings review help', js("document.querySelector('.st-sec').textContent.includes('Numbers 1–9 toggle actions')"),
                           'the full-width review editor lost its keyboard instructions')
                scan("settings A %s %s" % (sid, theme))
                if sid in ("sessions", "security", "projects", "databases"):
                    shot("settings-A-%s-%s" % (sid, theme))
    click('[data-nav="sessions"]')
    expect("settings groups", js("[...document.querySelectorAll('.st-fieldgroup h3')].some(e=>e.textContent==='Tools')"), "missing Tools heading")
    ab("fill", "#st-sessions-language", "Settings regression")
    ab("fill", "#st-sessions-max_sessions", "999")
    expect("settings validation", js("document.querySelector('.st-save').disabled && document.querySelector('#st-sessions-max_sessions').getAttribute('aria-invalid')==='true'"), "invalid input was saveable")
    click('[data-nav="interface"]')
    click('[data-nav="sessions"]')
    expect("settings retained draft", js("document.querySelector('#st-sessions-language').value==='Settings regression'"), "navigation discarded draft")
    click('.st-undo')
    expect("settings discard", js("document.querySelector('.st-savebar').hidden && !document.querySelector('.st-field.modified')"), "discard kept dirty markers")
    ab("fill", ".st-search", "maximum number of active")
    expect("settings new copy search", js("!!document.querySelector('#st-sessions-max_sessions')"), "visible explanation not searchable")
    ab("fill", ".st-search", "no-such-setting-123")
    expect("settings empty search", click('.st-found button', 'Clear search'), "missing clear-search action")
    click('[data-nav="sessions"]')
    ab("fill", "#st-sessions-language", "Settings regression")
    expect("settings editor above save bar", js("document.querySelector('#st-sessions-language').getBoundingClientRect().bottom <= document.querySelector('.st-savebar').getBoundingClientRect().top"),
           "the save bar covers the focused input after the first edit")
    shot('settings-A-unsaved')
    click('.st-save')
    expect("settings saved", js("document.querySelector('.st-savebar').hidden && !!document.querySelector('.st-note')"), "save did not finish")
    saved = next(s for s in api('/api/settings')['sections'] if s['id'] == 'sessions')
    expect("settings persisted", any(f['key'] == 'language' and f.get('value') == 'Settings regression' for f in saved['fields']), "API did not persist language")
    shot('settings-A-saved')


def main() -> int:
    global BASE, TOKEN, DEMO_SID, DEMO_TITLE
    t0 = time.time()
    serve, log = None, []
    if A.database_only and not A.url:
        # Metadata-only fixtures: these checks never connect to either database.
        os.environ.setdefault("SERVE_MYSQL", "127.0.0.1:9:demo::orders")
        os.environ.setdefault("SERVE_PG", "127.0.0.1:9:reader::reporting")
    if A.url:
        url = A.url
    else:
        serve, url, log = start_serve()
    BASE, TOKEN = url.split("/#token=")[0], url.split("#token=")[1]
    rc = 2
    try:
        st = api("/api/state")
        demo = [s for s in st["sessions"] if s.get("project") == "grp/demo" and s["state"] != "exited"]
        if not demo:
            print("gui.py: no live grp/demo session (is this serve.py?)", file=sys.stderr)
            return 2
        DEMO_SID, DEMO_TITLE = demo[0]["id"], demo[0].get("title") or "grp/demo"
        try:   # N10-B: a long name with a run of spaces for the Files screens (an existing one from a reused serve.py is fine)
            api("/api/fs/create", {"project": "grp/demo", "path": LONG, "kind": "file", "text": "long-name-hit\n"})
        except urllib.error.HTTPError:
            pass   # it exists already; anything else shows as "the long-named file is not in the tree"
        try:   # N10-D-6: a manual timesheet row with a long unbroken label (once per serve.py)
            ts = api("/api/timesheet")
            if not any(m["label"].startswith("N10-D ") for m in ts.get("manual", [])):
                api("/api/timesheet/manual", {"week": ts["week"], "day": ts["days"][0], "label": "N10-D " + "W" * 150,
                                              "minutes": 30, "text": "long label"})
        except (urllib.error.HTTPError, KeyError):
            pass   # no timesheet: the check finds no row and passes
        r = ab("--init-script", os.path.join(SCANS, "errors.js"), "open", url, timeout=120)
        if not r.get("success"):
            print("gui.py: agent-browser could not open the GUI: %s" % r.get("error"), file=sys.stderr)
            return 2
        presets = [p for p in A.presets.split(",") if p]
        widths = [int(w) for w in A.widths.split(",") if w]
        if A.settings_only:
            settings_redesign(widths)
            rc = 1 if FINDINGS else 0
            print(json.dumps({"ok": rc == 0, "scenario": "Settings A", "screens": len(SCREENS), "findings": FINDINGS, "out": OUT}), flush=True)
            return rc
        if A.navigation_only:
            navigation_pins(widths)
            rc = 1 if FINDINGS else 0
            print(json.dumps({"ok": rc == 0, "scenario": "Halo navigation", "widths": widths, "findings": FINDINGS, "out": OUT}), flush=True)
            return rc
        if A.sidebar_only or A.inspector_only:
            if A.sidebar_only:
                compact_sessions(widths)
            inspector_last_turn(widths)
            rc = 1 if FINDINGS else 0
            print(json.dumps({"ok": rc == 0, "scenario": "Sessions and Last turn" if A.sidebar_only else "Last turn inspector", "widths": widths,
                              "findings": FINDINGS, "out": OUT}), flush=True)
            return rc
        if A.limits_only:
            plan_limits(widths)
            rc = 1 if FINDINGS else 0
            print(json.dumps({"ok": rc == 0, "scenario": "plan limits", "widths": widths,
                              "findings": FINDINGS, "out": OUT}), flush=True)
            return rc
        if A.overview_only:
            for preset in presets:
                load_preset(preset)
                for width in widths:
                    CTX.update(preset=preset, width=width)
                    ab("set", "viewport", str(width), str(HEIGHT.get(width, 1080)))
                    overview_toggle()
            rc = 1 if FINDINGS else 0
            return rc
        if A.database_only:
            database_search_and_chooser(widths)
            rc = 1 if FINDINGS else 0
            return rc
        if A.alerts_only:
            recent_alerts(widths)
            rc = 1 if FINDINGS else 0
            return rc
        only = A.grid_only or A.approvals_only or A.security_tree or A.project_options_only or A.clipboard_only or A.walk_only
        if not only:
            navigation_pins(widths)
            recent_alerts(widths)
        if not only or A.approvals_only:
            allow_in_project()
        print("== gui scenarios before the walks: %d s" % (time.time() - t0), flush=True)
        if A.security_tree:
            security_notice(widths)
            security_profiles(widths)
            disabled_controls()
        for preset in ([] if A.approvals_only or A.project_options_only or A.clipboard_only else presets):
            mods = load_preset(preset)
            for w in widths:
                if A.grid_only:
                    CTX.update(preset=preset, width=w)
                    ab("set", "viewport", str(w), str(HEIGHT.get(w, 1080)))
                    grid_ux(mods)
                else:
                    tw = time.time()
                    walk(preset, w, mods)
                    print("== gui walk %s %d: %d s" % (preset, w, time.time() - tw), flush=True)   # shard balance
        if not A.walk_only:
            if not A.grid_only and not A.project_options_only and not A.clipboard_only:
                session_cards(widths)
            if not A.grid_only and not A.approvals_only and not A.security_tree and not A.clipboard_only:
                project_options(widths)
            if A.clipboard_only or not (A.grid_only or A.approvals_only or A.security_tree or A.project_options_only):
                terminal_clipboard()
        rc = 1 if FINDINGS else 0
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        finding("harness", "error", "%s: %s" % (type(e).__name__, e))
        rc = 2
    finally:
        try:
            if TOKEN:
                api("/api/features", {"preset": "all", "onboarded": True})
        except Exception:  # noqa: BLE001
            pass
        ab("close", timeout=30)
        if serve:
            if serve.poll() is None:
                os.kill(serve.pid, signal.SIGTERM)   # by PID only; serve.py tears its own children down
            try:
                rest, _ = serve.communicate(timeout=60)
                log.append(rest or "")
            except subprocess.TimeoutExpired:
                os.kill(serve.pid, signal.SIGKILL)
                log.append("serve.py did not end in 60 s - killed\n")
            if "TORN DOWN" not in "".join(log):
                print("gui.py: serve.py did not report its teardown:\n" + "".join(log[-20:]), file=sys.stderr)
    if A.profile:
        print("GUI_PROFILE " + json.dumps(TIMINGS, sort_keys=True), flush=True)
    summary = {"ok": rc == 0, "rc": rc, "screens": len(SCREENS), "findings": FINDINGS, "allowed": ALLOWED,
               "runtime_s": round(time.time() - t0), "presets": A.presets, "widths": A.widths, "out": OUT}
    with open(os.path.join(OUT, "gui.json"), "w") as f:
        json.dump(dict(summary, screen_list=SCREENS), f, indent=1, ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False))
    print("\n== gui.py: %d screens, %d findings, %d allowed, %d s  (%s)" % (len(SCREENS), len(FINDINGS), len(ALLOWED),
                                                                             summary["runtime_s"], OUT))
    return rc


BASE = TOKEN = DEMO_SID = DEMO_TITLE = SERVE_WORK = ""
LONG = "docs/" + "very-long-file-name-" * 9 + "end  two spaces.md"
if __name__ == "__main__":
    sys.exit(main())
