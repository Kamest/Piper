"""session_digest: the last turns of a session from its Claude Code transcript (7.3).

The transcript lives in the session's own config dir on the host
($PIPER_HOME/sessions/<id>/claude/projects/<encoded cwd>/<session-id>.jsonl), so piperd reads
it without touching the sandbox. The JSONL format is Claude Code's internal one, not a
public contract - the parser keeps only what it recognises and never fails on the rest.
"""
from __future__ import annotations

import json
import re
import hashlib
import os
import sqlite3
import threading
import time
from datetime import datetime
from contextlib import contextmanager
from collections import deque

import common
from common import scrub


TASK_ID = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
TASK_DONE = {"completed", "failed", "killed", "stopped", "cancelled"}


def background_events(rec: dict, stops: dict) -> list[tuple[str, str]]:
    """Task IDs and kinds only; task notifications can arrive while the foreground prompt is idle."""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    content = content if isinstance(content, list) else []
    out = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if rec.get("type") == "assistant" and c.get("type") == "tool_use" and c.get("name") == "TaskStop":
            args = c.get("input")
            tid = args.get("task_id") or args.get("shell_id") if isinstance(args, dict) else None
            if isinstance(c.get("id"), str) and len(c["id"]) <= 160 and isinstance(tid, str) and TASK_ID.fullmatch(tid) and len(stops) < 256:
                stops[c["id"]] = tid
        if rec.get("type") == "user" and c.get("type") == "tool_result" and not c.get("is_error"):
            use = c.get("tool_use_id")
            stopped = stops.pop(use, None) if isinstance(use, str) else None
            if stopped:
                out.append((stopped, "done"))
    result = rec.get("toolUseResult")
    if rec.get("type") == "user" and isinstance(result, dict) and not any(
            isinstance(c, dict) and c.get("is_error") for c in content):
        for key, kind in (("backgroundTaskId", "shell"), ("taskId", "monitor")):
            tid = result.get(key)
            if key == "taskId" and "timeoutMs" not in result:
                continue
            if isinstance(tid, str) and TASK_ID.fullmatch(tid):
                out.append((tid, kind))
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"] if rec.get("type") == "user" else []
    if rec.get("type") == "queue-operation" and rec.get("operation") == "enqueue":
        texts.append(rec.get("content", ""))
    for text in texts:
        if not isinstance(text, str):
            continue
        for notification in re.findall(r"<task-notification>(.*?)</task-notification>", text, re.S):
            tid = re.search(r"<task-id>([^<]+)</task-id>", notification)
            status = re.search(r"<status>([^<]+)</status>", notification)
            if tid and status and TASK_ID.fullmatch(tid[1]) and status[1] in TASK_DONE:
                out.append((tid[1], "done"))
    return out

MAX_CHARS = 1500


def local_conversations() -> list[dict]:
    """Opt-in discovery of main CLI conversations, with no foreign settings or subagents."""
    out = []
    for agent in ("claude", "codex"):
        cfg = os.path.join(common.REAL_HOME, "." + agent)
        pending = [("projects", 0)] if agent == "claude" else [("sessions", 0), ("archived_sessions", 0)]
        visited = 0
        while pending and visited < 4096:
            folder, depth = pending.pop()
            visited += 1
            try:
                names = common.safe_listdir(cfg, folder)
            except OSError:
                continue
            for name in names:
                rel = folder + "/" + name
                if ((agent == "claude" and depth == 0) or
                        (agent == "codex" and depth < 3 and re.fullmatch(r"\d{2,4}", name))):
                    if common.safe_is_dir(cfg, rel):
                        pending.append((rel, depth + 1))
                    continue
                match = re.fullmatch(r"([a-fA-F0-9-]{36})\.jsonl", name) if agent == "claude" else re.search(r"-([a-fA-F0-9-]{36})\.jsonl$", name)
                if not match:
                    continue
                sid = match[1]
                try:
                    raw = common.safe_read(cfg, rel, 1 << 20)
                except OSError:
                    continue
                cwd, created, branch, valid = "", 0.0, "", False
                for line in raw.splitlines():
                    try:
                        rec = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if rec.get("isSidechain"):
                        break
                    meta = rec.get("payload") if agent == "codex" and rec.get("type") == "session_meta" else rec if agent == "claude" else None
                    if not isinstance(meta, dict):
                        continue
                    if agent == "codex" and (meta.get("parent_thread_id") or meta.get("source") not in (None, "cli", "vscode", "exec", "app-server")):
                        break
                    native = meta.get("sessionId") if agent == "claude" else meta.get("id") or meta.get("session_id")
                    if native != sid:
                        continue
                    cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
                    if not os.path.isabs(cwd):
                        continue
                    git = meta.get("git") or {}
                    branch = str(meta.get("gitBranch") or (git.get("branch") if isinstance(git, dict) else "") or "")[:200]
                    try:
                        created = datetime.fromisoformat(str(meta.get("timestamp") or rec.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                    valid = True
                    break
                if valid:
                    key = hashlib.sha256((agent + "\0" + cfg + "\0" + sid).encode()).hexdigest()
                    out.append({"key": key, "session": "local-" + key, "native": sid, "agent": agent,
                                "cfg": cfg, "rel": rel, "cwd": cwd, "created": created, "native_branch": branch})
    return list({s["key"]: s for s in out}.values())


def session_file(cfg: str, sid: str, agent: str = "claude") -> str | None:
    """Only the named main conversation, never a subagent or another session's newest file."""
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid):
        return None
    if agent == "claude":
        return transcript(cfg, sid)
    pending = [("sessions", 0), ("archived_sessions", 0)]
    visited = 0
    while pending and visited < 4096:
        folder, depth = pending.pop()
        visited += 1
        try:
            names = common.safe_listdir(cfg, folder)
        except OSError:
            continue
        for name in names:
            rel = folder + "/" + name
            if name.endswith("-" + sid + ".jsonl"):
                try:
                    common.safe_stat(cfg, rel)
                    return rel
                except OSError:
                    continue
            if depth < 3 and re.fullmatch(r"\d{2,4}", name) and common.safe_is_dir(cfg, rel):
                pending.append((rel, depth + 1))
    return None


def readable_record(rec: dict, sid: str, agent: str) -> dict | None:
    """Searchable conversation text only: no tools, thinking, injected context or sidechains."""
    if rec.get("isSidechain") or rec.get("isMeta"):
        return None
    if agent == "claude":
        if rec.get("sessionId", sid) != sid:
            return None
        kind = rec.get("type")
        if kind in ("ai-title", "custom-title"):
            title = rec.get("aiTitle" if kind == "ai-title" else "customTitle")
            return {"title": scrub(title[:200]), "custom": kind == "custom-title"} if isinstance(title, str) else None
        role, msg = kind, rec.get("message")
    else:
        if rec.get("type") != "response_item":
            return None
        msg = rec.get("payload")
        role = msg.get("role") if isinstance(msg, dict) and msg.get("type") == "message" else None
    if role not in ("user", "assistant") or not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(c["text"] for c in content if isinstance(c, dict)
                         and c.get("type") in ("text", "input_text", "output_text") and isinstance(c.get("text"), str))
    else:
        return None
    text = text.strip()
    if role == "user" and text.startswith("<command-message>"):
        name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        text = " ".join(x.strip() for x in (name[1] if name else "", args[1] if args else "") if x.strip())
    if not text or (role == "user" and re.match(
            r"(?:<task-notification>|<command-message>|<command-name>|<local-command|<environment_context>|# AGENTS\.md instructions|<INSTRUCTIONS>)", text)):
        return None
    try:
        ts = datetime.fromisoformat(str(rec.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        ts = 0
    return {"role": role, "text": scrub(text), "ts": ts, "branch": rec.get("gitBranch", "")}


def iter_records(cfg: str, rel: str, offset: int = 0, size: int | None = None):
    """Bounded reads, complete JSONL records; leave a partial last line for the next pass."""
    size = common.safe_size(cfg, rel) if size is None else size
    pending, start = b"", offset
    oversized = False
    while offset < size:
        raw = common.safe_read(cfg, rel, min(1 << 20, size - offset), offset)
        if not raw:
            break
        offset += len(raw)
        parts = (pending + raw).split(b"\n")
        pending = parts.pop()
        for line in parts:
            start += len(line) + 1
            if oversized:
                oversized = False
                yield start, None
                continue
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeError):
                rec = None
            yield start, rec if isinstance(rec, dict) else None
        if len(pending) > 8 << 20:
            start += len(pending)
            pending = b""
            oversized = True
        time.sleep(0)   # a large import must leave the daemon's other threads responsive


def validate_conversation(cfg: str, sid: str, agent: str) -> str:
    rel = session_file(cfg, sid, agent)
    if not rel:
        raise ValueError("The saved conversation is missing. Start a new conversation explicitly instead.")
    identity = False
    for _, rec in iter_records(cfg, rel):
        if rec is None:
            continue
        if agent == "claude" and rec.get("sessionId") == sid:
            identity = True
        if agent == "codex" and rec.get("type") == "session_meta":
            p = rec.get("payload")
            if not isinstance(p, dict) or (p.get("id") or p.get("session_id")) != sid:
                raise ValueError("The saved conversation ID does not match its transcript.")
            identity = True
        row = readable_record(rec, sid, agent)
        if identity and row and "role" in row:
            return rel
    raise ValueError("The saved conversation has no readable messages. Start a new conversation explicitly instead.")


class ConversationIndex:
    """GUI-only, rebuildable local fulltext; importing never happens in /api/state's poll."""
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.status = {"busy": False, "done": 0, "total": 0, "error": ""}
        self.refreshed = 0.0
        self.sources = ()
        self.local = []
        self.fts = True
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | common._NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS conversation (key TEXT PRIMARY KEY, source TEXT, inode INTEGER, "
                       "size INTEGER, mtime INTEGER, offset INTEGER, title TEXT DEFAULT '', custom INTEGER DEFAULT 0, "
                       "first TEXT DEFAULT '', last TEXT DEFAULT '', answer TEXT DEFAULT '', branch TEXT DEFAULT '', "
                       "activity REAL DEFAULT 0, messages INTEGER DEFAULT 0)")
            db.execute("CREATE TABLE IF NOT EXISTS message (key TEXT, ordinal INTEGER, role TEXT, text TEXT, ts REAL, "
                       "PRIMARY KEY(key,ordinal))")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS searchable USING fts5(key UNINDEXED, text, tokenize='unicode61 remove_diacritics 2')")
            except sqlite3.OperationalError:
                self.fts = False
                db.execute("CREATE TABLE IF NOT EXISTS searchable (key TEXT, text TEXT)")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def refresh(self, sources: list[dict], external: bool = False) -> dict:
        signature = (external, tuple(sorted((s["key"], s["cfg"], s["native"]) for s in sources)))
        with self.lock:
            if not self.status["busy"] and (signature != self.sources or time.monotonic() - self.refreshed > 5):
                self.sources = signature
                self.status = {"busy": True, "done": 0, "total": len(sources), "error": ""}
                threading.Thread(target=self._refresh, args=(sources, external), daemon=True, name="conversation-index").start()
            return dict(self.status)

    def _refresh(self, sources: list[dict], external: bool) -> None:
        try:
            if external:
                local = local_conversations()
                with self.lock:
                    self.local = local
                    self.status["total"] += len(local)
                sources = sources + local
            with self.db() as db:
                for n, s in enumerate(sources):
                    try:
                        self._update(db, s)
                    except OSError:
                        pass   # a removed / unsafe transcript stays visible with an unavailable action
                    db.commit()
                    with self.lock:
                        self.status["done"] = n + 1
        except (sqlite3.Error, ValueError) as e:
            with self.lock:
                self.status["error"] = "History indexing failed: " + str(e)[:200]
        finally:
            with self.lock:
                self.status["busy"] = False
                self.refreshed = time.monotonic()

    def _update(self, db, s: dict) -> None:
        cfg, sid, agent, key = s["cfg"], s["native"], s["agent"], s["key"]
        rel = s.get("rel") or session_file(cfg, sid, agent)
        if not rel:
            return
        st = common.safe_stat(cfg, rel)
        old = db.execute("SELECT * FROM conversation WHERE key=?", (key,)).fetchone()
        source = os.path.join(cfg, rel)
        if old and (old["source"] != source or old["inode"] != st.st_ino or old["size"] > st.st_size
                    or (old["size"] == st.st_size and old["mtime"] != st.st_mtime_ns)):
            for table in ("conversation", "message", "searchable"):
                db.execute("DELETE FROM " + table + " WHERE key=?", (key,))
            old = None
        if old and old["size"] == st.st_size and old["mtime"] == st.st_mtime_ns and agent != "codex":
            return
        db.execute("INSERT OR IGNORE INTO conversation(key,source,inode,size,mtime,offset) VALUES(?,?,?,?,?,0)",
                   (key, source, st.st_ino, 0, 0))
        meta = dict(db.execute("SELECT * FROM conversation WHERE key=?", (key,)).fetchone())
        offset = meta["offset"]
        identity = agent == "claude" or offset > 0
        for end, rec in iter_records(cfg, rel, offset, st.st_size):
            meta["offset"] = end
            if not rec:
                continue
            if agent == "codex" and rec.get("type") == "session_meta":
                p = rec.get("payload") or {}
                identity = isinstance(p, dict) and (p.get("id") or p.get("session_id")) == sid
                if not identity:
                    raise common.UnsafePath("conversation ID mismatch")
            row = readable_record(rec, sid, agent) if identity else None
            if not row:
                continue
            if "title" in row:
                if row["custom"] or not meta["custom"]:
                    meta["title"], meta["custom"] = row["title"], int(row["custom"])
                    db.execute("INSERT INTO searchable(key,text) VALUES(?,?)", (key, row["title"]))
                continue
            text = row["text"]
            meta["messages"] += 1
            if row["role"] == "user":
                meta["first"] = meta["first"] or text[:1000]
                meta["last"] = text[:2000]
            else:
                meta["answer"] = text[:6000]
            meta["branch"] = str(row["branch"] or meta["branch"])[:200]
            meta["activity"] = max(meta["activity"], row["ts"])
            db.execute("INSERT OR REPLACE INTO message VALUES(?,?,?,?,?)", (key, end, row["role"], text, row["ts"]))
            # Index all text in small overlapping chunks, including the middle of long messages.
            for at in range(0, len(text), 4000):
                chunk = text[max(0, at - 128):at + 4000]
                normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", chunk)
                db.execute("INSERT INTO searchable(key,text) VALUES(?,?)", (key, chunk + "\n" + normalized))
            if end - offset >= 1 << 20:
                cols = ("offset", "title", "custom", "first", "last", "answer", "branch", "activity", "messages")
                db.execute("UPDATE conversation SET " + ",".join(c + "=?" for c in cols) + ",size=0,mtime=0 WHERE key=?",
                           (*[meta[c] for c in cols], key))
                db.commit()
                offset = end
        if agent == "codex":
            try:
                raw = common.safe_read(cfg, "session_index.jsonl", 8 << 20)
                for line in raw.splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("id") == sid and isinstance(rec.get("thread_name"), str):
                        title = scrub(rec["thread_name"][:200])
                        if title != meta["title"]:
                            db.execute("INSERT INTO searchable(key,text) VALUES(?,?)", (key, title))
                        meta["title"] = title
            except OSError:
                pass
        cols = ("offset", "title", "custom", "first", "last", "answer", "branch", "activity", "messages")
        db.execute("UPDATE conversation SET " + ",".join(c + "=?" for c in cols) + ",size=?,mtime=? WHERE key=?",
                   (*[meta[c] for c in cols], st.st_size, st.st_mtime_ns, key))

    def search(self, sources: list[dict], query: str) -> tuple[list[dict], dict]:
        words = re.findall(r"[^\W_]+", query, re.UNICODE)[:12]
        with self.db() as db:
            found = None
            for word in words:
                if self.fts:
                    keys = {r[0] for r in db.execute("SELECT DISTINCT key FROM searchable WHERE searchable MATCH ?", ('"' + word + '"*',))}
                else:
                    keys = {r[0] for r in db.execute("SELECT DISTINCT key FROM searchable WHERE instr(lower(text),lower(?))>0", (word,))}
                keys.update(r[0] for r in db.execute("SELECT key FROM conversation WHERE instr(lower(title),lower(?))>0", (word,)))
                # Project / title / issue metadata is cheap and joins matches across messages.
                keys.update(s["key"] for s in sources if word.casefold() in s["find"].casefold())
                found = keys if found is None else found & keys
            out = []
            for s in sources:
                if found is not None and s["key"] not in found:
                    continue
                row = db.execute("SELECT * FROM conversation WHERE key=?", (s["key"],)).fetchone()
                meta = dict(row) if row else {}
                snippet = ""
                if words:
                    hit = db.execute("SELECT substr(text,max(1,instr(lower(text),lower(?))-100),340) FROM message "
                                     "WHERE key=? AND instr(lower(text),lower(?))>0 ORDER BY ordinal DESC LIMIT 1",
                                     (words[0], s["key"], words[0])).fetchone()
                    if hit:
                        snippet = hit[0]
                out.append({**s, **{k: meta.get(k, "") for k in ("title", "first", "last", "answer", "branch", "activity", "messages")}, "snippet": snippet})
        with self.lock:
            status = dict(self.status)
        return out, status


def transcript(cfg_dir: str, session_id: str | None) -> str | None:
    """Relative path (below cfg_dir) of the newest matching transcript; symlinks are ignored."""
    best, best_m = None, -1.0
    try:
        dirs = common.safe_listdir(cfg_dir, "projects")
    except OSError:
        return None
    for d in dirs:
        rel_dir = "projects/" + d
        if not common.safe_is_dir(cfg_dir, rel_dir):
            continue
        try:
            names = common.safe_listdir(cfg_dir, rel_dir)
        except OSError:
            continue
        for n in names:
            if not n.endswith(".jsonl") or (session_id and n != session_id + ".jsonl"):
                continue
            try:
                m = common.safe_stat(cfg_dir, rel_dir + "/" + n).st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = rel_dir + "/" + n, m
    return best


def _text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
        elif c.get("type") == "tool_use":
            parts.append("[tool %s]" % c.get("name", "?"))
        elif c.get("type") == "tool_result":
            parts.append("[tool result]")
    return "\n".join(p for p in parts if p)


def _records(cfg_dir: str, session_id: str | None):
    rel = transcript(cfg_dir, session_id)
    if not rel:
        return
    try:
        raw = common.safe_read(cfg_dir, rel, 32 << 20)   # the sandbox writes it: no symlink follows
    except OSError:
        return
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


# F05 (9.77): the argument a tool row shows - the one input field that says what the call did
_TOOL_ARG = {"Bash": "command", "Read": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
             "NotebookEdit": "notebook_path", "Grep": "pattern", "Glob": "pattern", "WebFetch": "url",
             "WebSearch": "query", "Task": "description", "Agent": "description", "TodoWrite": ""}
_PATH_TOOLS = ("Read", "Edit", "MultiEdit", "Write", "NotebookEdit")


def tool_arg(name: str, inp) -> str:
    """One line saying what a tool call did (scrubbed, cut): the command, the file's name, the pattern;
    an unknown tool (MCP) shows its first short string argument."""
    if not isinstance(inp, dict):
        return ""
    key = _TOOL_ARG.get(name)
    if key is None:
        key = next((k for k, v in inp.items() if isinstance(v, str) and v.strip()), "")
    v = inp.get(key) if key else None
    if not isinstance(v, str):
        return ""
    v = " ".join(v.split())
    if name in _PATH_TOOLS:
        v = v.rstrip("/").rsplit("/", 1)[-1]
    return scrub(v[:200] + (" …" if len(v) > 200 else ""))


def _ts(rec) -> float | None:
    try:
        return datetime.fromisoformat(str(rec.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_lines(content) -> int:
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return len(content.splitlines()) if isinstance(content, str) else 0


def digest(cfg_dir: str, session_id: str | None, turns: int = 6, max_chars: int = MAX_CHARS,
           max_turns: int = 40, tools: bool = False) -> list[dict]:
    """The transcript's text turns; `tools` (the Conversation view, F05) adds to each turn the
    `[tool X]` markers' details in their order: name, a one-line argument, the run time (until its
    result) and the result's line count - never the result itself (hidden content can be in it)."""
    out = []
    calls: dict[str, dict] = {}
    for rec in _records(cfg_dir, session_id):
        kind = rec.get("type")
        msg = rec.get("message") or {}
        if kind not in ("user", "assistant") or not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if tools and isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                    t = calls.pop(c["tool_use_id"])
                    t0, end = t.pop("_t0", None), _ts(rec)
                    if t0 is not None and end is not None:
                        t["ms"] = max(0, int((end - t0) * 1000))
                    t["lines"] = _result_lines(c.get("content"))
                    if c.get("is_error"):
                        t["error"] = True
        text = _text(content).strip()
        if not text or not text.replace("[tool result]", "").strip():   # results only (one per parallel call)
            continue
        if len(text) > max_chars:
            text = text[:max_chars] + " …"
        turn = {"role": kind, "text": scrub(text), "ts": rec.get("timestamp")}
        if tools and isinstance(content, list):
            uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
            if uses:
                turn["tools"] = []
                t0 = _ts(rec)
                for c in uses:
                    t = {"name": str(c.get("name", "?"))[:80], "arg": tool_arg(str(c.get("name", "")), c.get("input"))}
                    turn["tools"].append(t)
                    if isinstance(c.get("id"), str):
                        if t0 is not None:
                            t["_t0"] = t0
                        calls[c["id"]] = t
        out.append(turn)
    for t in calls.values():   # no result (yet): the call runs or was interrupted
        t.pop("_t0", None)
    return out[-max(1, min(int(turns), max_turns)):]


def codex_digest(cfg_dir: str, session_id: str | None, turns: int = 400) -> list[dict]:
    """Visible messages from this Codex rollout; ignore duplicate events and injected context."""
    rel = session_file(cfg_dir, session_id, "codex")
    if not rel:
        return []
    out = deque(maxlen=max(1, min(int(turns), 2000)))
    try:
        for _, rec in iter_records(cfg_dir, rel):
            row = readable_record(rec, session_id, "codex") if rec else None
            if row:
                text = row["text"]
                out.append({"role": row["role"], "text": text[:20000] + " …" if len(text) > 20000 else text,
                            "ts": rec.get("timestamp")})
    except OSError:
        pass
    return list(out)


def usage(cfg_dir: str, session_id: str | None) -> dict:
    """Token totals of the transcript's assistant messages (Claude Code records `usage` on each;
    a message split over several records repeats it, so each message id counts once)."""
    tot = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "messages": 0}
    seen = set()
    for rec in _records(cfg_dir, session_id):
        msg = rec.get("message") or {}
        if rec.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        mid = msg.get("id")
        if mid and mid in seen:
            continue
        seen.add(mid)
        u = msg["usage"]
        for k, src in (("input", "input_tokens"), ("output", "output_tokens"),
                       ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
            v = u.get(src)
            if isinstance(v, int):
                tot[k] += v
        tot["messages"] += 1
    return tot


def usage_by_day(cfg_dir: str, session_id: str | None, seen: set) -> dict:
    """{local date: token row} of the transcript's assistant messages; `seen` (message ids,
    shared across transcripts - a resumed conversation repeats them) keeps each once."""
    from datetime import datetime
    days: dict[str, dict] = {}
    for rec in _records(cfg_dir, session_id):
        msg = rec.get("message") or {}
        if rec.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        mid = msg.get("id")
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
        try:
            # "…Z": Python 3.10 (Ubuntu 22.04) reads no Z suffix
            day = datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00")).astimezone().date().isoformat()
        except (ValueError, TypeError, OverflowError):
            continue
        row = days.setdefault(day, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        u = msg["usage"]
        for k, src in (("input", "input_tokens"), ("output", "output_tokens"),
                       ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
            v = u.get(src)
            if isinstance(v, int) and v > 0:
                row[k] += v
    return days
