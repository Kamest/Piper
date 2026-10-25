"""Host-collected approval facts. No action is authorised by evidence or by a suggestion."""
import json
import re
import threading
import time

import common
import dbsql
import gitlab
import gitops

SECONDS = 2.0
WEEK = 7 * 86400
SECRET_COLUMN = re.compile(r"pass|pwd|secret|token|credential|cookie|auth|private.?key|api.?key", re.I)
# Whole name parts (dbsql.masked): author_id, passengers or a word in a comment never hide a statement.
SECRET_WORDS = ("passw*", "passwd", "pass", "pwd", "secret*", "token*", "credential*", "cookie*", "auth", "authtoken",
                "apikey", "privatekey", "authenticationstring", "salt")


def secret_name(name):
    return dbsql.masked(str(name).strip('`"[]'), SECRET_WORDS)


def clean(value):
    """Use the existing log secret patterns on every string, including nested previews/diffs."""
    if isinstance(value, str):
        return common.show_bidi(gitlab.scrub_text(value))
    if isinstance(value, dict):
        return {clean(k): "[scrubbed]" if SECRET_COLUMN.search(str(k)) else clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def public_db_card(card):
    """Legacy DB card compatibility, with the same safe three-row sample as the evidence box."""
    out = {k: v for k, v in card.items() if k not in ("_evidence_job", "evidence", "sample")}
    preview = card.get("evidence", {}).get("preview")
    if preview:
        out["sample"] = dict(card["sample"], rows=preview["rows"])
    return clean(out)


def box(title="What it changes", rows=None, notes=None, state="ready", **extra):
    return clean(dict(title=title, rows=rows or [], notes=notes or [], state=state,
                      checked_at=time.time(), **extra))


def unavailable(reason, title="What it changes", state="unavailable"):
    return box(title, [{"text": "Could not check: " + reason}], state=state)


class Job:
    """A bounded wait before display; one background collection, then a polling card update.

    The callback must also bound its own I/O. Exceptions never expose driver/forge messages,
    which can contain credentials or data. No result is published after the card is answered.
    """
    def __init__(self, collect, title="What it changes"):
        self.title, self.done, self.value = title, threading.Event(), None
        self.started = time.monotonic()
        def run():
            try:
                self.value = clean(collect())
            except TimeoutError:
                self.value = unavailable("timed out", title)
            except Exception:
                self.value = unavailable("collector failed", title, "error")
            finally:
                self.done.set()
        threading.Thread(target=run, daemon=True, name="piper-evidence").start()

    def initial(self, timeout=None):
        timeout = max(0, SECONDS - (time.monotonic() - self.started)) if timeout is None else timeout
        if self.done.wait(timeout):
            return self.value
        return box(self.title, [{"text": "checking…"}], state="checking")


class Budget:
    def __init__(self, seconds=SECONDS):
        self.end = time.monotonic() + seconds

    def left(self):
        left = self.end - time.monotonic()
        if left <= 0:
            raise TimeoutError()
        return left


def git_push(root, forge=None, plan=None):
    """Local refs only: never fetch or execute repository hooks to prepare a card."""
    budget = Budget()
    def git(*args, **kw):
        return gitops._git(root, *gitops.VIEW, *args, timeout=budget.left(), **kw).strip()
    branch = git("symbolic-ref", "--short", "HEAD")
    remotes = git("remote").splitlines()
    configured = git("config", "--get", "branch.%s.remote" % branch, ok=(0, 1))
    remote = configured if configured in remotes else "origin" if "origin" in remotes else next(iter(remotes), "")
    if not remote:
        return unavailable("no push remote")
    # The approval and execution already chose this target with gitops.push_plan.
    # Keep that snapshot for the label; local refs still determine the outgoing commits.
    if plan:
        branch, remote = plan["branch"], plan["remote"]
    ref = "refs/remotes/%s/%s" % (remote, branch)
    exists = bool(git("show-ref", "--verify", "--hash", ref, ok=(0, 1, 128)))
    rng = [ref + "..HEAD"] if exists else ["HEAD", "--not", "--remotes"]
    count = int(git("rev-list", "--count", *rng) or 0)
    logs = git("log", "-3", "--format=%h %s", *rng, "--").splitlines()
    # The exact outgoing set's oldest parent, as in push_preview. All root commits use the empty tree.
    commits = git("rev-list", "--reverse", *rng).splitlines()
    base = ref if exists else ""
    if not base and commits:
        parents = git("rev-list", "--parents", "-n", "1", commits[0]).split()
        base = parents[1] if len(parents) > 1 else git("hash-object", "-t", "tree", "--stdin", input=b"")
    new = plan["new"] if plan else not exists
    rows = [{"text": "%s → %s branch %s" % (remote, "new" if new else "existing", branch)}]
    rows.extend({"code": line.split(" ", 1)[0], "text": line.split(" ", 1)[-1]} for line in logs)
    if count > 3:
        rows.append({"text": "%d more" % (count - 3)})
    extra = {}
    if base:
        stat = git("diff", "--shortstat", base, "HEAD", "--")
        size = git("diff", "--numstat", base, "HEAD", "--")
        # Do not copy arbitrarily large repository content into a card/audit record.
        if sum(int(n) for line in size.splitlines() for n in line.split("\t")[:2] if n.isdigit()) > 20000:
            rows += [{"text": stat}, {"text": "Could not check: diff too large for secret scan"}]
        else:
            diff = git("diff", "--no-ext-diff", "--no-textconv", "--unified=3", base, "HEAD", "--")
            if len(diff) > 256000:
                rows += [{"text": stat}, {"text": "Could not check: diff too large for secret scan and preview"}]
            else:
                scrubbed = gitlab.scrub_text(diff)
                names = sorted({m for m in re.findall(r"\b([A-Za-z_][\w.-]{0,80})\s*[:=]\s*[\"']?\[scrubbed\]", scrubbed)
                                if SECRET_COLUMN.search(m)})
                found = "Secret-like values found in diff%s (values hidden)" % (": " + ", ".join(names) if names else "")
                rows += [{"text": found if scrubbed != diff else "No secrets detected in the text diff",
                          "tone": "warn" if scrubbed != diff else "ok"}]
                files = re.search(r"(\d+) files? changed", stat)
                added = re.search(r"(\d+) insertions?", stat)
                removed = re.search(r"(\d+) deletions?", stat)
                if files:
                    extra["diffstat"] = {"files": int(files[1]), "added": int(added[1]) if added else 0, "removed": int(removed[1]) if removed else 0}
                else:
                    rows.append({"text": stat or "No file changes"})
                if any(line.startswith("-\t-\t") for line in size.splitlines()):
                    rows.append({"text": "Could not check: binary file contents were not scanned"})
                extra["diff"] = scrubbed[:30000]
                if len(scrubbed) > 30000:
                    rows.append({"text": "Diff preview truncated"})
    else:
        rows.append({"text": "No outgoing file changes"})
    try:
        facts = forge(remote, branch, budget) if forge else {}
    except Exception:
        facts = {}  # local facts remain useful even when the forge is unavailable
    notes = ["Commits and diff are based on local remote-tracking refs; the remote may have changed."]
    if isinstance(facts.get("exists"), bool):
        rows[0]["text"] = "%s → %s branch %s" % (remote, "existing" if facts["exists"] else "new", branch)
        extra["push"] = {"remote": remote, "branch": branch, "new": not facts["exists"], "verified": True}
    else:
        extra["push"] = {"remote": remote, "branch": branch, "new": new, "verified": False}
    if isinstance(facts.get("protected"), bool):
        rows[0]["chips"] = ["PROTECTED" if facts["protected"] else "NOT PROTECTED"]
    rows.append({"text": facts.get("ci") or "Could not check: base branch CI unavailable"})
    return box(rows=rows, notes=notes, **extra)


def db_write(c, card, ddl=False):
    rows = [{"text": "%s · %s" % (c["name"], c.get("engine", "mysql")),
             "chips": ["PRODUCTION" if c["cls"] == "prod" else c["cls"].upper()]}]
    notes = []
    extra = {}
    if card.get("preview_ok") and c["cls"] != "prod" and not ddl:
        total = card.get("table_total")
        count = card.get("affected")
        rows.append({"text": "%s of %s rows change" % (count, total) if total is not None else card["preview"]})
        if total is None:
            rows.append({"text": "Could not check: total row count unavailable"})
        smp = card.get("sample")
        if smp:
            columns = smp["columns"]
            # Secrets are never copied to the payload, even for a human's test preview.
            preview = [[{k: ("[scrubbed]" if SECRET_COLUMN.search(columns[i]) else clean(v))
                         for k, v in cell.items()} for i, cell in enumerate(row)] for row in smp["rows"][:3]]
            extra["preview"] = dict(columns=columns, rows=preview, more=max(0, smp["total"] - 3))
        else:
            rows.append({"text": "Could not check: row preview unavailable"})
        notes.append("Counted before you answered; may differ.")
    else:
        notes.append("No dry run for schema changes." if ddl else "No dry run for production writes."
                     if c["cls"] == "prod" else card.get("preview") or "Could not check: dry run unavailable")
        rows.extend(card.get("facts") or [{"text": "Could not check: row estimate and dependent tables unavailable"}])
    return box(rows=rows, notes=notes, **extra)


def db_read(c, columns, plan, limit):
    rows = [{"text": "%s · %s · read-only" % (c["name"], c.get("engine", "mysql")),
             "chips": ["PRODUCTION"] if c["cls"] == "prod" else [c["cls"].upper()]},
            {"text": plan or "Could not check: EXPLAIN unavailable"}]
    rows.extend({"text": name, "mono": True, "chips": ["PERSONAL"] if personal(name) else []} for name in columns)
    if not columns:
        rows.append({"text": "Could not check: column names unavailable"})
    return box("What it reads", rows, ["The agent gets at most %d rows." % limit])


def personal(name):
    """Evidence labels are a name heuristic, never a claim that all personal data was found."""
    return dbsql.masked(name, (*dbsql.MASK_WORDS, "name", "firstname", "lastname", "fullname", "address", "ip"))


def matching_scope(kind, project, payload):
    """Narrow temporary grants, independent of the legacy session kind/environment grant."""
    # Never grant generic settings or opaque tool actions with an unknown matching scope.
    cls = payload.get("cls", "")
    if kind == "git_push" and isinstance(payload.get("push_new"), bool):
        new = (payload["push_new"] and payload.get("push_verified") is True
               and payload.get("branch") not in gitops.main_lines())
        target = [payload.get("remote"), "new branches" if new else payload.get("branch")]
        desc = "pushes to %s in %s" % ("new branches" if new else payload.get("branch"), project)
    elif kind.startswith("http_") and payload.get("request_scope"):
        if not payload.get("content_sha"):
            return None
        target = [payload.get("env"), payload["request_scope"], payload["content_sha"]]
        desc = "this HTTP request content in %s (%s)" % (project, payload.get("env", ""))
    elif kind == "db_write_prod":
        return None   # every production write asks; no grant outlives the card
    elif kind.startswith("db_write_") or kind.startswith("db_read_"):
        if not payload.get("connection"):
            return None
        if kind.startswith("db_write_") and not payload.get("table_scope"):
            return None
        target = [payload["connection"], payload.get("statement_kinds", []), payload.get("table_scope", [])]
        tables = ", ".join(".".join(p for p in tab if p) for tab in payload.get("table_scope", []))
        desc = "%s%s on %s" % ("writes" if "write" in kind else "reads", " to " + tables if tables else "", payload["connection"])
    elif kind in ("edit_file", "write_file", "write_exec") and payload.get("path"):
        target = [payload["path"], payload.get("flagged") is True]
        desc = "edits to %s in %s" % (payload["path"], project)
    else:
        return None
    raw = json.loads(json.dumps([kind, project, cls, payload.get("danger") is True, target]))
    # the request digest is a hash, not a secret; everything else must survive scrubbing unchanged
    digest = raw[4][2] if kind.startswith("http_") else None
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
        return None
    check = raw[:4] + [raw[4][:2]] if digest is not None else raw
    if clean(check) != check:
        return None  # never store a secret-bearing scope or merge distinct redacted targets
    key = json.dumps(raw, separators=(",", ":"), sort_keys=True)
    return {"scope": key, "description": clean(desc)}


def suggestion(records, scope, now=None, description="similar actions"):
    now = time.time() if now is None else now
    count = sum(1 for r in records if r.get("event") == "confirm" and r.get("approved") is True
                and r.get("answered") is True and not r.get("auto") and r.get("matching_scope") == scope
                and now - WEEK <= r.get("decided_at", 0) <= now)
    return {"text": clean("You allowed %d %s this week." % (count, description)), "link": "Change the rule…"} if count >= 5 else None
