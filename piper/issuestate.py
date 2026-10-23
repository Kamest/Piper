"""Issue memory across days.

`development/gitlab-<iid>-status.md` (MR: `gitlab-mr-<iid>-status.md`; an extension renames
it, inbox.issue_files) in the project: an ordinary, unversioned Markdown file that survives
sessions and works without Piper too (an IDE, a manual `claude`).

- Claude keeps the sections Status / Done / Decisions / Remaining / Waiting on (told so by
  a CLAUDE.md block of every session opened from an issue); an extension may map older
  headings onto them (DEFAULTS issue_sections).
- piperd keeps only the block between FACTS_BEGIN and FACTS_END: git facts, pipeline, MR,
  links to the analysis and the comment draft, and - when a session ended without
  updating the file - a summary of its last turns (digest.py). It writes only under the
  Edit guard: the file did not change between read and write (sha) and no session of
  the project's tree is working. A hidden file (or a hidden development/) is never read
  or written, like the comment draft.
- A new session from the inbox gets a pointer to the file plus the GitLab notes newer
  than its last write, as references (the session reads them with gitlab_issue).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

import common
import digest
import extensions
import github
import gitlab
import gitops
from common import audit, scrub
from inbox import _ts, issue_file

MAX = 256 << 10
SECTIONS = ("Status", "Done", "Decisions", "Remaining", "Waiting on")
FACTS_BEGIN = "<!-- piperd:facts -->"
FACTS_END = "<!-- /piperd:facts -->"
_FACTS_RX: dict = {}


def legacy_sections() -> dict:
    """{older heading (lower case): section} an extension reads as SECTIONS (DEFAULTS issue_sections)."""
    return {str(k).lower(): v for k, v in extensions.default("issue_sections", {}).items() if v in SECTIONS}


def facts_rx() -> re.Pattern:
    """piperd's block; an extension adds older marker names (DEFAULTS issue_facts_markers). Only markers on
    their own lines (Claude may quote them in its prose - the CLAUDE.md block names them), and the last BEGIN
    before END (a stray BEGIN left by Claude must not swallow the prose up to piperd's block)."""
    names = ("piperd:facts",) + tuple(extensions.default("issue_facts_markers", ()))
    if names not in _FACTS_RX:
        alt = "|".join(re.escape(n) for n in names)
        begin, end = r"<!-- (?:%s) -->" % alt, r"<!-- /(?:%s) -->" % alt
        _FACTS_RX[names] = re.compile(r"^" + begin + r"[ \t]*\r?\n(?:(?!^" + begin + r").)*?^" + end
                                      + r"[ \t]*(?:\r?\n|\Z)", re.S | re.M)
    return _FACTS_RX[names]
DIGEST_TURNS = 6
STALE_SLACK = 5.0   # s: a prose change up to this long before the turn's start still counts as its update
SEEN_FILE = os.path.join(common.PIPER_HOME, "issuestate-seen.json")   # {root/rel: {h, at}}: when the prose last changed


def _gh(issue: dict) -> bool:
    """W23: an issue / PR of a GitHub repository (inbox path gh:owner/repo)."""
    return str(issue.get("gl") or "").startswith("gh:")


def stem(issue: dict) -> str:
    iid = int(issue["iid"])
    if _gh(issue):
        return "github-pr-%d" % iid if issue.get("type") == "mr" else "github-%d" % iid
    return "gitlab-mr-%d" % iid if issue.get("type") == "mr" else "gitlab-%d" % iid


def rel_path(issue: dict) -> str:
    return issue_file(stem(issue), "status")


def claude_md(issue: dict) -> str:
    """The CLAUDE.md block of a session opened from an issue."""
    ref = ("!%d" if issue.get("type") == "mr" and not _gh(issue) else "#%d") % int(issue["iid"])
    return """
## Work status of %s (Piper)

- Keep the running status of the work on %s in `%s` (unversioned). When it exists, read it first.
- At the end of the work on the issue, and before you finish, update it briefly: `## Status` (one sentence, where it is),
  `## Done`, `## Decisions` (what was decided and why, including corrections from the user), `## Remaining`,
  `## Waiting on` (client / colleague / CI).
- The block between `%s` and `%s` is maintained by piperd (facts from git and GitLab): do not change it.
""" % (ref, ref, rel_path(issue), FACTS_BEGIN, FACTS_END)


def template(issue: dict) -> str:
    ref = ("!%d" if issue.get("type") == "mr" and not _gh(issue) else "#%d") % int(issue["iid"])
    head = "# %s %s\n\n" % (ref, scrub(str(issue.get("title") or "")).strip())   # the file is the session's to read
    return head + "".join("## %s\n\n-\n\n" % s for s in SECTIONS)


def sections(text: str) -> dict[str, str]:
    """`## Heading` -> body (the piperd block removed), for the known sections only."""
    text = facts_rx().sub("", text)
    out: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf).strip()
            name = m.group(1).rstrip(":")
            cur = next((s for s in SECTIONS if s.lower() == name.lower()), legacy_sections().get(name.lower()))
            buf = []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf).strip()
    return out


def with_facts(text: str, block: str) -> str:
    """text with its piperd block replaced by `block` (appended when there is none)."""
    eol = "\r\n" if "\r\n" in text else "\n"   # a CRLF file keeps its line endings
    block = (FACTS_BEGIN + "\n" + block.strip() + "\n" + FACTS_END + "\n").replace("\r\n", "\n").replace("\n", eol)
    if facts_rx().search(text):
        return facts_rx().sub(lambda m: block, text, count=1)
    return text.rstrip("\r\n") + eol + eol + block


def facts_of(text: str) -> str:
    m = facts_rx().search(text)
    return m.group(0) if m else ""


class IssueState:
    """Manager mixin (sessions.Manager)."""

    # the facts refresh after a turn: one worker per session id (ids are unique across Managers)
    _istate_busy_lock = threading.Lock()
    _istate_busy: set = set()
    _istate_again: set = set()

    # ------------------------------------------------------------ when the prose (not piperd's facts) changed
    _istate_seen_lock = threading.Lock()

    def _istate_changed_at(self, key: str, text: str, mtime: float) -> float:
        """The time the file's content OUTSIDE piperd's facts block last changed: piperd's own
        facts write after every turn must not look like an update of Status / Done / ...
        A new hash (Claude or the human edited it) takes the file's mtime."""
        prose = re.sub(r"\s+", " ", facts_rx().sub("", text)).strip()   # inserting the block moves blank lines
        h = hashlib.sha256(prose.encode("utf-8")).hexdigest()
        with self._istate_seen_lock:
            try:
                seen = common.read_json(SEEN_FILE, {})
            except ValueError:   # broken by hand: start over rather than fail the whole board
                seen = {}
            seen = seen if isinstance(seen, dict) else {}
            rec = seen.get(key)
            if isinstance(rec, dict) and rec.get("h") == h and isinstance(rec.get("at"), (int, float)):
                return float(rec["at"])
            seen[key] = {"h": h, "at": mtime}
            if len(seen) > 2000:   # old issues: keep the newest
                seen = dict(sorted(seen.items(), key=lambda kv: -float((kv[1] or {}).get("at") or 0))[:1500])
            common.write_json(SEEN_FILE, seen)
            return mtime

    # ------------------------------------------------------------ the file
    def _istate_loc(self, p: dict, issue: dict):
        """(root, rel) of the issue's state file, or None when it (or development/) is hidden."""
        try:
            rel = rel_path(issue)
        except (KeyError, TypeError, ValueError):
            return None
        hidden = self.project_hidden(p)
        if hidden("development") or hidden(rel):
            return None
        return os.path.realpath(p["path"]), rel

    def _istate_read(self, p: dict, issue: dict):
        """(root, rel, stat, text) - stat/text None when the file does not exist; None when
        it is hidden or not a regular file (a symlink, a directory)."""
        loc = self._istate_loc(p, issue)
        if loc is None:
            return None
        root, rel = loc
        try:
            st = common.safe_stat(root, rel)
        except FileNotFoundError:
            return root, rel, None, None
        except OSError:
            return None
        if st.st_size > MAX:
            return None
        try:
            return root, rel, st, common.safe_read(root, rel, MAX + 1).decode("utf-8", "replace")
        except OSError:
            return None

    def issue_md(self, s) -> str:
        """The extra CLAUDE.md block of a project session opened from an issue ("" otherwise)."""
        if s.kind != "project" or not isinstance(s.issue, dict):
            return ""
        try:
            p = self.gui_project(s.project)
        except (KeyError, ValueError):
            return ""
        return claude_md(s.issue) if self._istate_loc(p, s.issue) else ""

    def issue_state_summary(self, p: dict, issue: dict) -> dict | None:
        """Status + Waiting on for the inbox preview (None: no file). Unscrubbed (L5): the human's
        view of a file in their project, which Files opens as it is."""
        r = self._istate_read(p, issue)
        if r is None or r[2] is None:
            return None
        sec = sections(r[3])
        return {"path": r[1], "mtime": r[2].st_mtime,
                "status": sec.get("Status", "")[:1500], "waiting": sec.get("Waiting on", "")[:1500]}

    def issue_state_card(self, s) -> dict | None:
        """Board card: {path, exists, mtime, stale, live, prompt}. stale = the last turn that is
        over did not change the prose outside piperd's facts block (or there is no file yet) and
        the session is not working; prompt = the button may type into it now."""
        if s.kind != "project" or not isinstance(s.issue, dict):
            return None
        try:
            p = self.gui_project(s.project)
        except (KeyError, ValueError):
            return None
        loc = self._istate_loc(p, s.issue)
        if loc is None:
            return None
        r = self._istate_read(p, s.issue)
        if r is None:
            return None
        root, rel, st, text = r
        mtime = st.st_mtime if st is not None else None
        changed = self._istate_changed_at(os.path.join(root, rel), text, mtime) if st is not None else None
        import sessions   # noqa: PLC0415 - sessions imports this module
        running = s.state in sessions.ACTIVE   # the pid stays set after an exit: the state tells
        turns = [t for t in s.turns or [] if isinstance(t, dict) and isinstance(t.get("started"), (int, float))]
        open_turn = any(not isinstance(t.get("ended"), (int, float)) for t in turns)
        # the last turn that is over: ended, or cut off (Stop, a crash) in a session that no longer runs
        done = [t for t in turns if isinstance(t.get("ended"), (int, float)) or not running]
        last = max(done, key=lambda t: t["started"]) if done else None
        # stale = that turn did not touch the prose (Claude writes it during the turn, before its Stop). A running
        # session with an open turn is not judged yet (mid-turn, or its Stop's snapshot is still being taken)
        stale = last is not None and not (running and open_turn) and s.state not in ("working", "stopping") and \
            (changed is None or changed + STALE_SLACK < last["started"])
        return {"path": loc[1], "exists": mtime is not None, "mtime": mtime, "stale": stale,
                "live": running, "prompt": self._istate_at_prompt(s)}   # starting after Continue is live too

    def _istate_at_prompt(self, s) -> bool:
        """May the canned prompt be typed now? After a turn (Stop; idle_prompt turns it into
        attention after ~60 s), or a resumed session that passed attestation with no task to
        type. "Continue in…" from the inbox uses it too."""
        if s.pid is None:
            return False
        if s.idle and s.state in ("waiting", "attention"):
            return True
        # screen_wait: a resumed session whose screen has not shown Claude's prompt yet (a dialog may be up)
        return s.state == "ready" and not (s.task and not s.task_sent) and not getattr(s, "screen_wait", False)

    # ------------------------------------------------------------ piperd's block
    def _istate_facts(self, s, p: dict, summary: str = "") -> str:
        """Git + GitLab facts, mechanically; hidden files are never named."""
        now = time.strftime("%Y-%m-%d %H:%M")
        lines = ["## Facts (piperd, %s)" % now, ""]
        try:
            top = self._top(p)
            rh = self._repo_hidden(p, top)
            st = gitops.status(top, rh)
            files = [f["path"] for f in st["files"] if not f["sensitive"]]
            lines.append("- Branch: `%s`%s" % (st["branch"] or "?", " (upstream `%s`)" % st["upstream"] if st["upstream"] else " (no upstream)"))
            if st["upstream"]:
                lines.append("- Unpushed commits: %d" % st["ahead"])
            lines.append("- Uncommitted files: %d%s" % (len(files), (": " + ", ".join("`%s`" % f for f in files[:10])
                                                                        + (" …" if len(files) > 10 else "")) if files else ""))
        except (gitops.GitError, OSError, ValueError):
            lines.append("- Git: unavailable")
        try:
            ci = self.git_ci(p["key"])
            pipe, mr = ci.get("pipeline"), ci.get("mr")
            if pipe:
                lines.append("- Last pipeline: %s (%s)" % (pipe.get("status"), pipe.get("web_url") or ""))
            if mr:
                lines.append(("- PR: #%s %s (%s)" if ci.get("github") else "- MR: !%s %s (%s)")   # W23: GitHub's words
                             % (mr.get("iid"), mr.get("title") or "", mr.get("web_url") or ""))
        except (gitlab.GitLabError, gitops.GitError, OSError, ValueError, KeyError):
            pass
        root = os.path.realpath(p["path"])
        hidden = self.project_hidden(p)
        for label, kind in (("Analysis", "analysis"), ("Comment draft", "comment")):
            rel = issue_file(stem(s.issue), kind)
            try:
                if not hidden(rel):
                    common.safe_stat(root, rel)
                    lines.append("- %s: `%s`" % (label, rel))
            except OSError:
                pass
        if s.issue.get("web_url"):
            lines.append("- Issue: %s" % s.issue["web_url"])
        if summary:
            lines += ["", "### Last turns (piperd's summary, session %s ended without updating the status)" % s.id, "", summary]
        return scrub("\n".join(lines))

    def _istate_digest(self, s) -> str:
        src = self._transcript_src(s)
        if not src:
            return ""
        out = []
        for t in digest.digest(src[0], src[1], turns=DIGEST_TURNS, max_chars=400):
            text = " ".join(t["text"].split())
            if not text or re.match(r"^<[a-z][\w-]*>", text):
                continue
            out.append("- %s: %s" % ("You" if t["role"] == "user" else "Claude", text))
        return "\n".join(out)

    def _istate_write(self, s, p: dict, summary: str = "", create: bool = False) -> str | None:
        """Write piperd's block (Edit guard). Returns the path written, None when nothing
        changed or there is no file (and create is False). Raises ValueError when refused."""
        r = self._istate_read(p, s.issue)
        if r is None:
            raise ValueError("the issue status is hidden by the isolation policy or is not a regular file")
        root, rel, st, cur = r
        if st is None and not create:
            return None
        if cur is not None:   # the prose as it was before this write keeps its time (see _istate_changed_at)
            self._istate_changed_at(os.path.join(root, rel), cur, st.st_mtime)
        base = cur if cur is not None else template(s.issue)
        facts = self._istate_facts(s, p, summary)
        new = with_facts(base, facts)
        if cur is not None and facts_of(new).split("\n", 2)[2:] == facts_of(cur).split("\n", 2)[2:]:
            return None   # only the timestamp line would change
        with self._write_lock:
            busy = [x for x in self._same_tree(root) if x.state == "working"]
            if busy:
                raise ValueError("session \"%s\" is working in the project - the status is written when it is done" % busy[0].title)
            try:
                now = common.safe_read(root, rel, MAX + 1)
            except FileNotFoundError:
                now = None
            if (now is None) != (cur is None) or (now is not None and now.decode("utf-8", "replace") != cur):
                raise ValueError("%s changed meanwhile - try again" % rel)
            if not common.safe_is_dir(root, "development"):
                if os.path.lexists(os.path.join(root, "development")):
                    raise ValueError("development/ is not a directory")
                os.mkdir(os.path.join(root, "development"), 0o755)
            data = new.encode("utf-8") if now is None else \
                with_facts(now.decode("utf-8", "surrogateescape"), facts).encode("utf-8", "surrogateescape")   # a cp1250 prose keeps its bytes
            common.safe_write(root, rel, data,
                              mode=(st.st_mode & 0o7777) if st is not None else 0o644, exact_mode=st is not None)
        audit({"event": "issue-state-write", "session": s.id, "project": p["key"], "path": rel, "summary": bool(summary)})
        self._files_cache.pop(p["key"], None)
        return rel

    def issue_state_refresh_soon(self, s) -> None:
        """After a turn (Stop): refresh the facts of an existing file in a worker thread."""
        if s.kind != "project" or not isinstance(s.issue, dict):
            return
        with self._istate_busy_lock:   # one worker per session: a burst of Stop lines (sandbox-written) is one refresh
            if s.id in self._istate_busy:
                self._istate_again.add(s.id)
                return
            self._istate_busy.add(s.id)

        def run():
            try:
                while True:
                    try:
                        self._istate_write(s, self.gui_project(s.project))
                    except (KeyError, ValueError, OSError):
                        pass
                    with self._istate_busy_lock:
                        if s.id not in self._istate_again:   # a Stop that came meanwhile: once more, then done
                            return
                        self._istate_again.discard(s.id)
            finally:   # also after an unexpected error: the next Stop gets a worker again
                with self._istate_busy_lock:
                    self._istate_busy.discard(s.id)
                    self._istate_again.discard(s.id)
        threading.Thread(target=run, daemon=True, name="piper-istate").start()

    def issue_state_update(self, sid: str) -> dict:
        """Board card "Update issue status": a live session at its prompt gets one prompt;
        an ended one gets piperd's summary of its last turns (under the Edit guard)."""
        s = self.get(sid)
        if s.kind != "project" or not isinstance(s.issue, dict):
            raise ValueError("session %s was not opened from an issue" % s.id)
        p = self.gui_project(s.project)
        loc = self._istate_loc(p, s.issue)
        if loc is None:
            raise ValueError("the issue status is hidden by the isolation policy")
        at_prompt = self._istate_at_prompt(s)
        if s.pid is not None and at_prompt:
            if not self.auto_type_ok(s):   # a canned prompt must not take the human's half-written one with it
                raise ValueError("you typed in the session's terminal since the last prompt - send or delete it, or click Send now on the card")
            self.type_text(s, "Update the work status in %s (sections Status, Done, Decisions, Remaining, Waiting on; "
                              "create it when it does not exist). Do not change the piperd:facts block." % loc[1])
            audit({"event": "gui-issue-state-prompt", "session": s.id})
            return {"sent": True, "path": loc[1]}
        import sessions   # noqa: PLC0415 - sessions imports this module
        if s.state in sessions.ACTIVE:   # starting (Continue), working, asking: piperd's summary would say "ended"
            raise ValueError("the session is %s - try again when it waits for you" % s.state)
        rel = self._istate_write(s, p, self._istate_digest(s) or "(the session transcript is not available)", create=True)
        return {"written": rel or loc[1], "path": loc[1]}

    # ------------------------------------------------------------ inbox
    def issue_state_hint(self, p: dict, issue: dict, gl_key: str | None = None) -> str:
        """Appended to the task of a session opened from the inbox: the pointer and the GitLab
        notes newer than the file's last write (references only; "" when there is no file)."""
        r = self._istate_read(p, issue)
        if r is None or r[2] is None:
            return ""
        mtime = r[2].st_mtime
        text = ("\n\nThe status of the previous work is in %s (last updated %s) - read it first and update it at the end."
                % (r[1], time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))))
        try:
            if _gh(issue):   # W23: GitHub's comments (GitHubError is a GitLabError)
                gh, repo = self._ib_gh(issue["gl"])
                prev = github.item_preview(gh, repo, issue["type"], int(issue["iid"]), [repo])
            else:
                tok = gitlab.token()
                if not tok:
                    return text
                prev = gitlab.item_preview(self._ib_gl(tok), issue["gl"], issue["type"], int(issue["iid"]), [issue["gl"]])
        except (gitlab.GitLabError, OSError, ValueError, KeyError):
            return text
        new = [n for n in prev.get("notes") or [] if _ts(n.get("created_at")) > mtime and n.get("id")]
        if new:
            text += (" New %s comments since then: %s."
                     % ("GitHub" if _gh(issue) else "GitLab",
                        ", ".join("note_%d (%s)" % (int(n["id"]), str(n.get("created_at") or "")[:10]) for n in new[-20:])))
        return text
