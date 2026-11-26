"""Regex definition index for the GUI's go-to-definition and symbol search.

No language server: one regex pass per line over the project's files as the GUI's quick open
lists them (projectfs.files: no `.git`, no NOISE, no symlinked file - a file hidden from Claude
is indexed like any other, the GUI is the human's), each read symlink-safe and bounded
(> 2 MiB like every other GUI read, or binary: skipped). The index is
cached per project and refreshed incrementally: at most every TTL seconds the file list is
re-read and only files whose (mtime, size) changed are parsed again.

Kinds: java/kotlin/scala/groovy types + methods (not calls, not control keywords); ts/js
class|interface|type|enum|function|exported const/let + methods; py class|def; go func|type;
xml `<bean id=` (and `id="…"` in Spring xml); .properties keys; yaml top-level keys;
sql create table|view|function|procedure; sh functions; md headings.
"""
from __future__ import annotations

import os
import re
import threading
import time

import common
from projectfs import MAX_READ, is_noise, split_lines

MAX_FILE = MAX_READ   # 2 MiB, the limit of every GUI read (K13)
MAX_HITS_PER_FILE = 2000
MAX_LINE = 400
TTL = 30.0
COLD_WAIT = 2.0    # a first query waits this long for the first scan, then answers `building`
STALE_WAIT = 0.15  # a later one this long for a rescan, then answers from the index as it is

EXT = {".java": "jvm", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala", ".groovy": "jvm", ".gradle": "jvm",
       ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts", ".js": "ts", ".jsx": "ts", ".mjs": "ts", ".cjs": "ts",
       ".py": "py", ".go": "go", ".xml": "xml", ".properties": "properties", ".yml": "yaml", ".yaml": "yaml",
       ".sql": "sql", ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".md": "md", ".markdown": "md"}

CONTROL = {"if", "for", "while", "switch", "catch", "return", "new", "else", "try", "synchronized", "throw",
           "do", "case", "assert", "yield", "super", "this", "function", "await", "typeof", "delete",
           "when", "with", "elif", "import", "package", "throws", "instanceof", "in", "of", "sizeof"}
_ID = r"[A-Za-z_$][\w$]*"
_ANN = r"(?:@[\w.]+(?:\([^)]*\))?\s+)*"
_JVM_MOD = (r"(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|open|data|inner|enum|"
            r"annotation|value|inline|internal|case|strictfp|implicit|override|expect|actual)\s+)*")
RX_TYPE = re.compile(r"^\s*" + _ANN + "(" + _JVM_MOD + r")(class|interface|enum|record|@interface|object|trait)\s+("
                     + _ID + ")")
RX_JAVA_METHOD = re.compile(r"^\s*" + _ANN + r"(?:(?:public|protected|private|static|final|abstract|synchronized|"
                            r"native|default|strictfp|override|def)\s+)*(?:<[^>()]+>\s+)?([\w$.<>\[\],?]+(?:\s*<[^()]*>)?"
                            r"(?:\[\])*)\s+(" + _ID + r")\s*\(")
RX_KT_FUN = re.compile(r"^\s*" + _ANN + r"(?:[a-z]+\s+)*fun\s+(?:<[^>]+>\s+)?(?:[\w.<>?]+\.)?(" + _ID + r")\s*[(<]")
RX_SCALA_DEF = re.compile(r"^\s*(?:[a-z]+\s+)*def\s+(" + _ID + r")")
RX_TS_DECL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
                        r"(class|interface|type|enum|const\s+enum|function\*?)\s+(" + _ID + ")")
RX_TS_CONST = re.compile(r"^\s*export\s+(?:declare\s+)?(const|let|var)\s+(" + _ID + ")")
RX_TS_METHOD = re.compile(r"^\s+(?:(?:public|private|protected|static|readonly|async|override|abstract|get|set)\s+)*"
                          r"\*?(#?" + _ID + r")\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^;={]+)?\{\s*(?:\}\s*)?$")
RX_PY = re.compile(r"^(\s*)(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
RX_GO_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
RX_GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\b")
RX_GO_BLOCK = re.compile(r"^\s+([A-Za-z_]\w*)\s+\S")
RX_BEAN = re.compile(r"<bean\b[^>]*?\bid\s*=\s*\"([^\"]+)\"")
RX_XML_ID = re.compile(r"\bid\s*=\s*\"([^\"]+)\"")
RX_PROP = re.compile(r"^\s*((?:[^\s#!=:\\]|\\.)(?:[^\s=:\\]|\\.)*)\s*(?:[=:]|\s)")
RX_YAML = re.compile(r"^([A-Za-z_][\w.-]*|\"[^\"]+\"|'[^']+')\s*:(?:\s|$)")
RX_SQL = re.compile(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:(?:temporary|temp|unlogged|global|local)\s+)*"
                    r"(table|view|materialized\s+view|function|procedure)\s+(?:if\s+not\s+exists\s+)?([\w.\"`\[\]]+)")
RX_SH = re.compile(r"^\s*(?:function\s+([A-Za-z_][\w:.-]*)\s*(?:\(\))?\s*\{?|([A-Za-z_][\w:.-]*)\s*\(\)\s*\{?)")
RX_MD = re.compile(r"^(#{1,6})\s+(.*)$")   # the closing #s are stripped in _md: a regex for them backtracks cubically
TYPE_KINDS = ("class", "interface", "enum", "record", "annotation", "object", "trait", "type")


def lang_of(path: str) -> str | None:
    return EXT.get(os.path.splitext(path)[1].lower())


def _jvm(lines, kotlin: bool, scala: bool):
    for i, line in enumerate(lines, 1):
        s = line.lstrip()
        if not s or s.startswith(("//", "*", "/*", "import ", "package ")) or len(line) > MAX_LINE:
            continue
        m = RX_TYPE.match(line)
        if m:
            mods, kw, name = m.group(1), m.group(2), m.group(3)
            kind = ("annotation" if kw == "@interface" or "annotation" in mods.split() else
                    "enum" if kw == "enum" or "enum" in mods.split() else kw)
            yield name, kind, i
            continue
        if kotlin:
            m = RX_KT_FUN.match(line)
            if m:
                yield m.group(1), "method", i
            continue
        if scala:
            m = RX_SCALA_DEF.match(line)
            if m:
                yield m.group(1), "method", i
            continue
        if "(" not in line:
            continue
        m = RX_JAVA_METHOD.match(line)
        if m:
            typ, name = m.group(1), m.group(2)
            if typ in CONTROL or name in CONTROL or "=" in line[:m.end()] or typ.endswith("."):
                continue
            yield name, "method", i


def _ts(lines):
    for i, line in enumerate(lines, 1):
        if len(line) > MAX_LINE or line.lstrip().startswith(("//", "*", "/*")):
            continue
        m = RX_TS_DECL.match(line)
        if m:
            kw = m.group(1)
            yield m.group(2), ("enum" if kw.endswith("enum") else "function" if kw.startswith("function") else kw), i
            continue
        m = RX_TS_CONST.match(line)
        if m:
            yield m.group(2), "const", i
            continue
        m = RX_TS_METHOD.match(line)
        if m and m.group(1).lstrip("#") not in CONTROL:
            yield m.group(1), "method", i


def _py(lines):
    for i, line in enumerate(lines, 1):
        m = RX_PY.match(line)
        if m:
            yield m.group(3), ("class" if m.group(2) == "class" else "method" if m.group(1) else "function"), i


def _go(lines):
    block = False   # inside `type ( ... )`
    for i, line in enumerate(lines, 1):
        m = RX_GO_FUNC.match(line)
        if m:
            yield m.group(1), "function", i
            continue
        if re.match(r"^type\s*\(\s*$", line):
            block = True
            continue
        if block:
            if line.startswith(")"):
                block = False
                continue
            m = RX_GO_BLOCK.match(line)
            if m:
                yield m.group(1), "type", i
            continue
        m = RX_GO_TYPE.match(line)
        if m:
            yield m.group(1), "type", i


def _xml(lines, text_head: str):
    spring = bool(re.search(r"springframework|<beans\b", text_head, re.I))   # Spring xml, also a framework's own schema
    for i, line in enumerate(lines, 1):
        if len(line) > 4000:
            continue
        seen = set()
        for m in RX_BEAN.finditer(line):
            seen.add(m.group(1))
            yield m.group(1), "bean", i
        if spring:
            for m in RX_XML_ID.finditer(line):
                if m.group(1) not in seen:
                    yield m.group(1), "id", i


def _props(lines):
    cont = False
    for i, line in enumerate(lines, 1):
        was = cont
        cont = line.endswith("\\") and not line.endswith("\\\\")
        if was:
            continue
        m = RX_PROP.match(line + " ")
        if m:
            yield m.group(1).replace("\\", ""), "property", i


def _yaml(lines):
    for i, line in enumerate(lines, 1):
        m = RX_YAML.match(line)
        if m:
            yield m.group(1).strip("\"'"), "key", i


def _sql(lines):
    for i, line in enumerate(lines, 1):
        m = RX_SQL.match(line)
        if m:
            name = re.sub(r"[\"`\[\]]", "", m.group(2)).split(".")[-1]
            if name:
                yield name, re.sub(r"\s+", " ", m.group(1).lower()), i


def _sh(lines):
    for i, line in enumerate(lines, 1):
        m = RX_SH.match(line)
        if m:
            name = m.group(1) or m.group(2)
            if name and name not in CONTROL:
                yield name, "function", i


def _md(lines):
    fence = False
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if not fence:
            m = RX_MD.match(line)
            name = m.group(2).rstrip().rstrip("#").rstrip() if m else ""
            if name:
                yield name, "heading", i


def parse(path: str, text: str) -> list[dict]:
    """Definitions of one file: [{name, kind, line, text}] (text = the stripped line, <= 200 chars)."""
    lang = lang_of(path)
    if lang is None:
        return []
    lines = split_lines(text)   # line numbers as git / the GUI count them
    # an overlong line (minified code, data) is skipped: no regex over it; xml keeps its own limit
    scan = lines if lang == "xml" else [ln if len(ln) <= MAX_LINE else "" for ln in lines]
    if lang in ("jvm", "kotlin", "scala"):
        it = _jvm(scan, lang == "kotlin", lang == "scala")
    elif lang == "ts":
        it = _ts(scan)
    elif lang == "py":
        it = _py(scan)
    elif lang == "go":
        it = _go(scan)
    elif lang == "xml":
        it = _xml(scan, text[:4096])
    elif lang == "properties":
        it = _props(scan)
    elif lang == "yaml":
        it = _yaml(scan)
    elif lang == "sql":
        it = _sql(scan)
    elif lang == "sh":
        it = _sh(scan)
    else:
        it = _md(scan)
    out = []
    for name, kind, line in it:
        out.append({"name": name[:200], "kind": kind, "path": path, "line": line,
                    "text": lines[line - 1].strip()[:200]})
        if len(out) >= MAX_HITS_PER_FILE:
            break
    return out


class Index:
    """One project's definitions: rel path -> ((mtime_ns, size), hits).

    B12: a full scan (the file list + an lstat per file, a parse of what changed) runs in a background thread,
    outside `lock`, and swaps its result in; a query never waits for it longer than the caller allows and answers
    from the index as it is. A GUI save re-parses just that file (`update`); a path updated while a scan runs is
    re-applied over the scan's result, so the swap never brings back the older parse."""

    def __init__(self):
        self.files: dict[str, tuple[tuple[int, int], list[dict]]] = {}
        self.checked = 0.0          # when the last full scan finished (0 = stale: the next query starts one)
        self.built = False          # a full scan finished at least once (before: queries answer `building`)
        self.lock = threading.Lock()
        self._scan: threading.Thread | None = None
        self._pending: set[str] | None = None   # paths touched while a scan runs (None = no scan)

    @staticmethod
    def _parse_one(real_root: str, rel: str, old=None):
        """(key, hits) of one file, `old` when its (mtime, size) is unchanged, None when it is not indexed."""
        if lang_of(rel) is None or any(is_noise(seg) for seg in rel.split("/")):
            return None
        try:
            st = common.safe_stat(real_root, rel)   # lstat: a link (planted since the listing) is skipped
        except OSError:
            return None
        if st.st_size > MAX_FILE:
            return None
        key = (st.st_mtime_ns, st.st_size)
        if old and old[0] == key:
            return old
        try:
            raw = common.safe_read(real_root, rel, MAX_FILE + 1)
        except OSError:
            return None
        if len(raw) > MAX_FILE or b"\0" in raw[:8192]:
            return None
        return key, parse(rel, raw.decode("utf-8", "replace"))

    def _build(self, root: str, rels: list[str]) -> None:
        real_root = os.path.realpath(root)
        with self.lock:
            old = dict(self.files)
        keep = {}
        for rel in rels:
            got = self._parse_one(real_root, rel, old.get(rel))
            if got is not None:
                keep[rel] = got
        with self.lock:
            for rel in self._pending or ():   # updated meanwhile: the newer state wins over this scan's
                if rel in self.files:
                    keep[rel] = self.files[rel]
                else:
                    keep.pop(rel, None)
            self.files = keep
            self.checked = time.time()
            self.built = True

    def refresh(self, root: str, rels: list[str], force: bool = False) -> None:
        """Re-parse what changed among `rels` (the caller's listing), in the caller's thread."""
        if not force and time.time() - self.checked < TTL:
            return
        with self.lock:
            if self._pending is None:
                self._pending = set()
        try:
            self._build(root, rels)
        finally:
            with self.lock:
                if self._scan is None:
                    self._pending = None

    def refresh_async(self, root: str, listing) -> threading.Thread:
        """Start a full scan in the background unless one runs (`listing()` = the GUI's file list, called there)."""
        with self.lock:
            if self._scan is not None and self._scan.is_alive():
                return self._scan
            self._pending = set()

            def run():
                try:
                    self._build(root, listing())
                except Exception:   # an unreadable project: the index stays as it was, the next query retries
                    pass
                finally:
                    with self.lock:
                        self._scan = None
                        self._pending = None
            self._scan = threading.Thread(target=run, name="symbols-scan", daemon=True)
            self._scan.start()
            return self._scan

    def update(self, root: str, rel: str) -> None:
        """One file changed (a GUI save): parse it now, nothing else - the scan stays as fresh as it was."""
        got = self._parse_one(os.path.realpath(root), rel)
        with self.lock:
            if got is None:
                self.files.pop(rel, None)
            else:
                self.files[rel] = got
            if self._pending is not None:
                self._pending.add(rel)

    def invalidate(self, rel: str | None = None) -> None:
        with self.lock:
            if rel:
                self.files.pop(rel, None)
                if self._pending is not None:
                    self._pending.add(rel)
            self.checked = 0.0

    def query(self, name: str = "", q: str = "") -> dict:
        name, q = (name or "").strip(), (q or "").strip()
        if not name and not q:
            raise ValueError("name or q is required")
        with self.lock:
            files = dict(self.files)
        if name:
            hits = [h for _, hs in files.values() for h in hs if h["name"] == name]
            hits.sort(key=lambda h: (0 if h["kind"] in TYPE_KINDS else 1 if h["kind"] in ("method", "function") else 2,
                                     h["path"], h["line"]))
            limit = 50
        else:
            low = q.lower()
            tiers: tuple[list, list, list] = ([], [], [])
            for _, hs in files.values():
                for h in hs:
                    n = h["name"].lower()
                    t = 0 if n == low else 1 if n.startswith(low) else 2 if low in n else -1
                    if t >= 0:
                        tiers[t].append(h)
            for t in tiers:
                t.sort(key=lambda h: (len(h["name"]), h["name"], h["path"], h["line"]))
            hits = tiers[0] + tiers[1] + tiers[2]
            limit = 100
        return {"symbols": hits[:limit], "truncated": len(hits) > limit, "indexed": len(files)}


_INDEXES: dict[str, Index] = {}
_LOCK = threading.Lock()


def index_for(key: str) -> Index:
    with _LOCK:
        return _INDEXES.setdefault(key, Index())
