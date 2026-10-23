"""Diagnostics from the last build a session ran.

The source is the session's own transcript: the most recent Bash tool_result whose text
yields at least one compiler / test / lint error. Parsers (best effort, never raise):

  mvn     [ERROR] /abs/F.java:[12,5] msg           (and [WARNING])
  javac   F.java:12: error: msg                    (and warning:)
  tsc     src/a.ts(12,5): error TS2322: msg  ·  src/a.ts:12:5 - error TS2322: msg
  eslint  stylish: a file line, then `  12:5  error  msg  rule`
  gradle  e: file:///abs/A.kt:12:5 msg  ·  e: /abs/A.kt: (12, 5): msg   (w: = warning)
  python  File "x.py", line 12, in f  ... ExcType: msg
  junit   [ERROR]   FooTest.bar:42 expected ...     (surefire / failsafe summary)

The text is Claude's own tool output (hostile input): only references and messages leave,
messages scrubbed and bounded.
"""
from __future__ import annotations

import os
import re

from common import scrub

MAX_ITEMS = 200
MAX_MSG = 500
_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

RX_MVN = re.compile(r"^\[(ERROR|WARNING)\]\s+((?:/|[A-Za-z]:[\\/])[^\s:\[]*?\.\w+):\[(\d+)(?:,(\d+))?\]\s*(.*)$")
RX_JAVAC = re.compile(r"^((?:/|\.{0,2}/?)?[^\s:]+\.(?:java|kt|groovy|scala)):(\d+):\s*(error|warning):\s*(.*)$")
RX_TSC1 = re.compile(r"^([^\s(][^()]*?\.(?:[cm]?tsx?|[cm]?jsx?|vue))\((\d+),(\d+)\):\s*(error|warning)\s+(TS\d+:\s*.*)$")
RX_TSC2 = re.compile(r"^([^\s:][^:]*?\.(?:[cm]?tsx?|[cm]?jsx?|vue)):(\d+):(\d+)\s+-\s+(error|warning)\s+(TS\d+:\s*.*)$")
RX_ESLINT_FILE = re.compile(r"^((?:/|[A-Za-z]:\\|\./)?[^\s].*\.(?:[cm]?[jt]sx?|vue|svelte|astro))\s*$")
RX_ESLINT_ROW = re.compile(r"^\s+(\d+):(\d+)\s+(error|warning)\s+(.+?)(?:\s{2,}(\S+))?\s*$")
RX_GRADLE1 = re.compile(r"^([ew]):\s+(?:file://)?(/[^:\s]+\.\w+):(\d+):(\d+)\s+(.*)$")
RX_GRADLE2 = re.compile(r"^([ew]):\s+(?:file://)?(/[^:\s]+\.\w+):\s*\((\d+),\s*(\d+)\):\s*(.*)$")
RX_PY_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:, in .*)?$')
RX_PY_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure)|AssertionError)(?::\s*(.*))?$")
RX_JUNIT = re.compile(r"^\[ERROR\]\s+(?:(?:Run \d+:\s+)?)([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.([A-Za-z_$][\w$]*)"
                      r"(?:\[\d+\])?:(\d+)(?:->\S+)*\s*(.*)$")


def _msg(s: str) -> str:
    return scrub(s.strip())[:MAX_MSG]


def parse(text: str) -> list[dict]:
    """Items {tool, severity, path, line, col, message} (path as printed, `class` for junit)."""
    items: list[dict] = []
    eslint_file = None
    tb: list[tuple[str, int]] | None = None   # frames of the Python traceback being read
    lines = _ANSI.sub("", text).replace("\r", "").split("\n")
    for raw in lines:
        line = raw.rstrip()
        if tb is not None:
            m = RX_PY_FRAME.match(line)
            if m:
                tb.append((m.group(1), int(m.group(2))))
                continue
            m = RX_PY_EXC.match(line.strip())
            if m and tb:
                # the innermost frame of the user's code (not the stdlib / site-packages)
                own = [f for f in tb if "/site-packages/" not in f[0] and "/lib/python" not in f[0]
                       and not f[0].startswith("<")]
                path, ln = (own or tb)[-1]
                items.append({"tool": "python", "severity": "error", "path": path, "line": ln, "col": None,
                              "message": _msg(line.strip())})
                tb = None
                continue
            if line.startswith(" ") or not line.strip():
                continue   # the source line / ^^^ marker under a frame
            tb = None
        if line.startswith("Traceback (most recent call last)"):
            tb = []
            continue
        m = RX_MVN.match(line)
        if m:
            items.append({"tool": "mvn", "severity": "error" if m.group(1) == "ERROR" else "warning",
                          "path": m.group(2), "line": int(m.group(3)),
                          "col": int(m.group(4)) if m.group(4) else None, "message": _msg(m.group(5))})
            continue
        m = RX_JUNIT.match(line)
        if m and not line[8:].lstrip().startswith(("Tests run", "Failures", "Errors", "Failed", "BUILD")):
            items.append({"tool": "junit", "severity": "error", "path": m.group(1), "line": int(m.group(3)),
                          "col": None, "message": _msg("%s: %s" % (m.group(2), m.group(4)) if m.group(4) else m.group(2))})
            continue
        m = RX_GRADLE1.match(line) or RX_GRADLE2.match(line)
        if m:
            items.append({"tool": "gradle", "severity": "error" if m.group(1) == "e" else "warning",
                          "path": m.group(2), "line": int(m.group(3)), "col": int(m.group(4)),
                          "message": _msg(m.group(5))})
            continue
        m = RX_JAVAC.match(line)
        if m:
            items.append({"tool": "javac", "severity": m.group(3), "path": m.group(1), "line": int(m.group(2)),
                          "col": None, "message": _msg(m.group(4))})
            continue
        m = RX_TSC1.match(line) or RX_TSC2.match(line)
        if m:
            items.append({"tool": "tsc", "severity": m.group(4), "path": m.group(1).strip(), "line": int(m.group(2)),
                          "col": int(m.group(3)), "message": _msg(m.group(5))})
            continue
        m = RX_ESLINT_ROW.match(line)
        if m and eslint_file:
            msg = m.group(4) + (" (%s)" % m.group(5) if m.group(5) else "")
            items.append({"tool": "eslint", "severity": m.group(3), "path": eslint_file, "line": int(m.group(1)),
                          "col": int(m.group(2)), "message": _msg(msg)})
            continue
        m = RX_ESLINT_FILE.match(line)
        if m and not line.startswith(" "):
            eslint_file = m.group(1)
            continue
        if not line.strip():
            eslint_file = None
    return items


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        k = (it["path"], it["line"], it["col"], it["message"])
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= MAX_ITEMS:
            break
    return out


def relativize(items: list[dict], project: str, resolve_class=None, resolve_rel=None) -> list[dict]:
    """Paths inside the project become project-relative (container path == host path);
    junit's class names become files through `resolve_class(name) -> rel | None`; a relative
    path printed from a sub-directory (tsc, eslint run in a module) through
    `resolve_rel(path) -> rel | None`."""
    root = os.path.realpath(project).rstrip("/") + "/"
    raw_root = project.rstrip("/") + "/"
    for it in items:
        p = it["path"]
        if it["tool"] == "junit":
            it["path"] = (resolve_class(p) if resolve_class else None) or p
            continue
        if p.startswith("file://"):
            p = p[7:]
        for r in (raw_root, root):
            if p.startswith(r):
                p = p[len(r):]
                break
        if p.startswith("./"):
            p = p[2:]
        if resolve_rel and not p.startswith("/"):
            p = resolve_rel(p) or p
        it["path"] = p
    return items


def tool_text(content) -> str:
    """The text of a tool_result's content (a string or a list of text blocks)."""
    if isinstance(content, str):
        return content
    out = []
    for c in content or []:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            out.append(c["text"])
    return "\n".join(out)


def from_records(records, max_text: int = 4 << 20) -> dict:
    """{at, source, items} of the most recent Bash tool_result that yields diagnostics."""
    commands: dict[str, str] = {}
    results: list[tuple[str | None, str, str]] = []   # (timestamp, tool_use_id, text)
    for rec in records:
        msg = rec.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash" and isinstance(c.get("id"), str):
                cmd = (c.get("input") or {}).get("command") if isinstance(c.get("input"), dict) else None
                commands[c["id"]] = str(cmd or "")
            elif c.get("type") == "tool_result" and isinstance(c.get("tool_use_id"), str):
                results.append((rec.get("timestamp"), c["tool_use_id"], tool_text(c.get("content"))[-max_text:]))
    for ts, tid, text in reversed(results):
        if tid not in commands:
            continue   # not a Bash call
        items = dedupe(parse(text))
        if items:
            src = (commands[tid].strip().splitlines() or [""])[0][:300]
            return {"at": ts if isinstance(ts, str) else None, "source": scrub(src), "items": items}
    return {"at": None, "source": "", "items": []}
