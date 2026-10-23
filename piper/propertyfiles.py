"""Blind access to Java .properties: names only, writes only after a fresh human card.

The parser keeps physical spans so unrelated comments and values are never rewritten.
No exception includes source text. Byte decoding is explicit, never guessed.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import common
import guard

MAX_FILE = 1 << 20
MAX_PROPOSAL = 16000
WS = " \t\f"
TOOLS = ("properties_keys", "properties_set")


class PropertiesError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    key: str
    start: int
    end: int
    eol: str


def unescape(text: str) -> str:
    """Decode Java property escapes, including surrogate pairs; fail without quoting input."""
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        i += 1
        if c != "\\":
            out.append(c)
            continue
        if i == len(text):
            raise PropertiesError("incomplete properties escape")
        c = text[i]
        i += 1
        if c == "u":
            digits = text[i:i + 4]
            if len(digits) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise PropertiesError("invalid properties Unicode escape")
            out.append(chr(int(digits, 16)))
            i += 4
        else:
            out.append({"t": "\t", "r": "\r", "n": "\n", "f": "\f"}.get(c, c))
    return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def escape(text: str) -> str:
    """ASCII-only representation: separators, whitespace and line breaks cannot inject a key."""
    out = []
    for c in text:
        if c in "\\ =:#!":
            out.append("\\" + c)
        elif c in "\t\r\n\f":
            out.append({"\t": r"\t", "\r": r"\r", "\n": r"\n", "\f": r"\f"}[c])
        elif not 32 <= ord(c) <= 126:
            b = c.encode("utf-16-be", "surrogatepass")
            out.extend("\\u" + b[i:i + 2].hex() for i in range(0, len(b), 2))
        else:
            out.append(c)
    return "".join(out)


def entries(text: str) -> list[Entry]:
    """Parse logical lines using only Java's CR/LF and space/tab/form-feed delimiters."""
    if text.startswith("\ufeff") or "\x00" in text:
        raise PropertiesError("unsupported properties encoding or binary content")
    lines = [m for m in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if m.group()]
    out = []
    i = 0
    while i < len(lines):
        start = lines[i].start()
        logical = ""
        while True:
            m = lines[i]
            physical = m.group()
            eol = "\r\n" if physical.endswith("\r\n") else physical[-1:] if physical.endswith(("\r", "\n")) else ""
            part = physical[:-len(eol)] if eol else physical
            part = part.lstrip(WS)
            i += 1
            # Even a continued line is a comment when the accumulated key is empty (Java load).
            if not logical and (not part or part.startswith(("#", "!"))):
                break
            logical += part
            slashes = len(logical) - len(logical.rstrip("\\"))
            if slashes % 2:
                if i == len(lines):
                    raise PropertiesError("dangling properties continuation; finish the line before editing")
                logical = logical[:-1]
                continue
            j = 0
            while j < len(logical):
                if logical[j] == "\\":
                    j += 2
                elif logical[j] in WS + "=:":
                    break
                else:
                    j += 1
            key = unescape(logical[:j])
            # Validate escapes in the value as well, without returning it or including it in errors.
            unescape(logical[j:])
            out.append(Entry(key, start, m.end(), eol))
            break
    return out


def update(text: str, values: dict[str, str]) -> str:
    """Update every occurrence of selected keys; preserve all unselected physical spans."""
    parsed = entries(text)
    newline = next((e.eol for e in parsed if e.eol), "\n")
    chunks, seen, last = [], set(), 0
    for e in parsed:
        if e.key not in values:
            continue
        chunks += [text[last:e.start], escape(e.key) + "=" + escape(values[e.key]) + e.eol]
        last = e.end
        seen.add(e.key)
    chunks.append(text[last:])
    out = "".join(chunks)
    for key, value in values.items():
        if key not in seen:
            if out and not out.endswith(("\r", "\n")):
                out += newline
            out += escape(key) + "=" + escape(value) + newline
    if len(out.encode("utf-8", "surrogatepass")) > MAX_FILE:
        raise PropertiesError("properties result is too large")
    return out


def path_arg(value) -> str:
    if not isinstance(value, str) or not value.endswith(".properties") or "\\" in value or "\x00" in value or any(
            p in ("", ".", "..") for p in value.split("/")) or guard.is_git_path(value):
        raise PropertiesError("a project-relative .properties file is required")
    return value


def read(root: str, rel: str) -> tuple[bytes, int]:
    try:
        st = common.safe_stat(root, rel)
        if st.st_nlink != 1:
            raise PropertiesError("hard-linked properties files are not supported")
        if st.st_size > MAX_FILE:
            raise PropertiesError("properties file is too large")
        raw = common.safe_read(root, rel, MAX_FILE + 1, single_link=True)
        if len(raw) > MAX_FILE:
            raise PropertiesError("properties file is too large")
        return raw, st.st_mode & 0o7777
    except OSError:
        raise PropertiesError("cannot access a regular properties file inside this project") from None


def tool(manager, session, name: str, args: dict) -> dict:
    """Project-only names/read-modify-write boundary; approval never reveals original content."""
    if session.kind != "project" or not session.path:
        raise PropertiesError("properties tools require a project session")
    if name not in TOOLS:
        raise PropertiesError("unknown properties tool")
    rel = path_arg(args.get("file"))
    encoding = args.get("encoding", "utf-8")
    if encoding not in ("utf-8", "iso-8859-1"):
        raise PropertiesError("encoding must be utf-8 or iso-8859-1")
    root = session.path
    raw, mode = read(root, rel)
    try:
        text = raw.decode(encoding)
    except UnicodeError:
        raise PropertiesError("file does not match the selected properties encoding") from None
    parsed = entries(text)
    if name == "properties_keys":
        return {"keys": list(dict.fromkeys(e.key for e in parsed))}
    values = args.get("values")
    if not isinstance(values, dict) or not values or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
        raise PropertiesError("values must be a nonempty object of string keys and string values")
    proposal = json.dumps(values, ensure_ascii=True)
    if len(proposal) > MAX_PROPOSAL:
        raise PropertiesError("proposed values are too large for an approval card")
    data = update(text, values).encode(encoding)
    summary = "Update properties in %s (project %s)\nNew values only; existing values stay private:\n%s" % (
        rel, session.project, proposal)
    if not manager.ask("properties_set", session, summary, {"file": rel}, timeout=600):
        raise PropertiesError("properties write was not approved; nothing changed")
    top = manager._session_top(session)
    hidden = top is None or manager.hidden_for(session)(os.path.relpath(os.path.join(os.path.realpath(root), rel), top))
    with manager._write_lock:
        current, current_mode = read(root, rel)
        if current != raw or current_mode != mode:
            raise PropertiesError("properties file changed while awaiting approval; retry")
        try:
            if hidden:
                manager._hidden_write_guard(root, rel)
                common.overwrite_in_place(root, rel, data, raw, mode=mode, exact_mode=True,
                                          pin=manager._ops_pin(root, rel))
            else:
                common.safe_write(root, rel, data, mode=mode, exact_mode=True)
        except (OSError, ValueError):
            raise PropertiesError("properties write refused; check file isolation in Piper") from None
    common.audit({"event": "properties-write", "project": session.project, "path": rel, "by": session.id})
    manager.bump()
    return {"ok": True}
