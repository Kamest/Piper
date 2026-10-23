"""SQL text rules of the DB client (dbtools.py), pure - no driver, no Manager.

Classification by text is never the boundary on its own:
the read path also runs every statement in a server-side READ ONLY transaction on a connection
whose session is read-only. What this module decides:

  - read_check(): may this text go to the READ connection at all? The first keyword of the
    statement must be on an allowlist (SELECT WITH SHOW DESCRIBE EXPLAIN TABLE VALUES HELP, for
    the GUI console also SET @var / SET NAMES / USE), a WITH must end in a SELECT/TABLE/VALUES,
    EXPLAIN never ANALYZE, one statement only, and no token of the denylist (INTO OUTFILE, FOR
    UPDATE, versioned comment /*! and optimizer hint /*+, file and lock functions);
  - kind(): select | dml | ddl | other for the write path - only dml gets a preview in a
    rolled-back transaction, and only over transactional tables (tables());
  - split(): a script into statements (`;` outside strings and comments, DELIMITER refused);
  - masked(): is a column name personal data (identifier words, not a substring: hotel_name and
    card_type are not, customer_email and password_hash are).

The tokenizer follows the server's lexer where it matters for hiding text: in MySQL `--` starts a
comment only before whitespace (`1--1` is arithmetic), `#` is a comment and a backslash escapes in
strings; in PostgreSQL `--` always comments, block comments nest and only E'' strings escape. The
denylist is checked on the token stream AND on the raw text, so a tokenizer mistake would have to
hide a word from both.
"""
from __future__ import annotations

import re

ENGINES = ("mysql", "postgres", "oracle", "mssql")
READ_FIRST = ("SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "TABLE", "VALUES", "HELP")
CONSOLE_FIRST = ("SET", "USE")   # the GUI console only, and SET only for @var / NAMES
DML_FIRST = ("INSERT", "UPDATE", "DELETE", "REPLACE", "MERGE")
DDL_FIRST = ("CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT", "GRANT", "REVOKE")
MAIN_KW = ("SELECT", "TABLE", "VALUES") + DML_FIRST
DENY_SEQ = (("INTO", "OUTFILE"), ("INTO", "DUMPFILE"), ("FOR", "UPDATE"), ("FOR", "SHARE"),
            ("FOR", "NO", "KEY", "UPDATE"), ("FOR", "KEY", "SHARE"), ("LOCK", "IN", "SHARE", "MODE"))
DENY_FUNCS = {
    "mysql": ("LOAD_FILE", "GET_LOCK", "SYS_EXEC", "SYS_EVAL", "SLEEP_UNTIL"),
    "postgres": ("PG_READ_FILE", "PG_READ_BINARY_FILE", "PG_LS_DIR", "PG_STAT_FILE", "LO_IMPORT", "LO_EXPORT",
                 "LO_GET", "LO_PUT", "DBLINK", "DBLINK_EXEC", "SET_CONFIG", "PG_TERMINATE_BACKEND",
                 "PG_CANCEL_BACKEND", "PG_ADVISORY_LOCK", "PG_ADVISORY_XACT_LOCK", "PG_TRY_ADVISORY_LOCK",
                 "PG_ADVISORY_LOCK_SHARED", "PG_TRY_ADVISORY_LOCK_SHARED", "PG_RELOAD_CONF",
                 "PG_ROTATE_LOGFILE", "TXID_CURRENT", "NEXTVAL", "SETVAL"),
    "oracle": ("DBMS_PIPE", "UTL_FILE", "UTL_HTTP", "DBMS_LOCK", "DBMS_SCHEDULER", "DBMS_JOB", "DBMS_SQL"),
    "mssql": ("XP_CMDSHELL", "OPENROWSET", "OPENDATASOURCE", "OPENQUERY"),
}
RAW_DENY = re.compile(r"into\s+(out|dump)file|/\*[!+]|\bfor\s+(no\s+key\s+)?(update|share)\b|\block\s+in\s+share\s+mode\b|"
                      r"\bload_file\s*\(|\bpg_read_(binary_)?file\b|\blo_(import|export)\b|\bxp_cmdshell\b|\butl_file\b",
                      re.I)
# words a masked column name is made of (identifier parts, lower case; `*` = prefix)
MASK_WORDS = ("email", "phone", "tel", "street", "iban", "birth*", "passw*", "pwd", "salt",
              "secret", "token", "cardnumber", "authenticationstring", "ssn")   # + an extension's (dbtools.mask_words)
NOT_MASKED: tuple = ()   # whole parts that only look like a mask word


class SqlError(ValueError):
    """A text the asked path refuses (the message tells the agent what to do instead)."""


# ------------------------------------------------------------------ tokens
WORD_RX = re.compile(r"[A-Za-z_\u0080-￿][A-Za-z0-9_$\u0080-￿]*")


def tokens(sql: str, engine: str = "mysql") -> list[tuple[str, str]]:
    """[(kind, text)] with kind in word num str ident op ; ( ) - comments dropped, a
    versioned comment / optimizer hint kept as kind 'hint' (it is executable in MySQL)."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(sql)
    mysql = engine == "mysql"
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
            continue
        if c == "-" and sql.startswith("--", i) and (not mysql or i + 2 >= n or sql[i + 2].isspace()
                                                      or ord(sql[i + 2]) < 32):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "#" and mysql:
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == "/" and sql.startswith("/*", i):
            if mysql and i + 2 < n and sql[i + 2] in "!+":
                j = sql.find("*/", i + 2)
                out.append(("hint", sql[i:n if j < 0 else j + 2]))
                i = n if j < 0 else j + 2
                continue
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                elif engine == "postgres" and sql.startswith("/*", j):
                    depth += 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise SqlError("unterminated /* comment")
            i = j
            continue
        if c in "'\"`" or (c in "eE" and engine == "postgres" and sql.startswith("'", i + 1)):
            esc = mysql or c in "eE"
            if c in "eE":
                i += 1
                c = "'"
            q, j, buf = c, i + 1, []
            while True:
                if j >= n:
                    raise SqlError("unterminated %s quote" % q)
                ch = sql[j]
                if esc and ch == "\\" and q != "`":
                    buf.append(sql[j:j + 2])
                    j += 2
                    continue
                if ch == q:
                    if sql.startswith(q + q, j):
                        buf.append(q)
                        j += 2
                        continue
                    break
                buf.append(ch)
                j += 1
            kind = "str" if q == "'" or (q == '"' and mysql) else "ident"
            out.append((kind, "".join(buf)))
            i = j + 1
            continue
        if c == "$" and engine == "postgres":   # $tag$ ... $tag$ dollar quoting
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                end = sql.find(m.group(0), i + len(m.group(0)))
                if end < 0:
                    raise SqlError("unterminated dollar quote")
                out.append(("str", sql[i + len(m.group(0)):end]))
                i = end + len(m.group(0))
                continue
        if c == "[" and engine == "mssql":
            j = sql.find("]", i)
            if j < 0:
                raise SqlError("unterminated [identifier]")
            out.append(("ident", sql[i + 1:j]))
            i = j + 1
            continue
        m = WORD_RX.match(sql, i)
        if m:
            out.append(("word", m.group(0)))
            i = m.end()
            continue
        if c.isdigit():
            m = re.match(r"\d+(\.\d*)?([eE][+-]?\d+)?", sql[i:])
            out.append(("num", m.group(0)))
            i += len(m.group(0))
            continue
        if c in ";()":
            out.append((c, c))
            i += 1
            continue
        if c == "@":
            m = re.match(r"@@?[A-Za-z0-9_.$]*", sql[i:])
            out.append(("var", m.group(0)))
            i += len(m.group(0))
            continue
        out.append(("op", c))
        i += 1
    return out


def _words(toks) -> list[str]:
    return [t.upper() for k, t in toks if k == "word"]


def statements(toks) -> list[list]:
    """Token lists of the statements (split at `;` - strings and comments are already tokens)."""
    out, cur = [], []
    for t in toks:
        if t[0] == ";":
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def first_keyword(toks) -> str:
    for k, t in toks:
        if k == "(":
            continue
        return t.upper() if k == "word" else ""
    return ""


def main_keyword(toks) -> str:
    """The statement keyword: past a WITH's CTE list (depth 0), past leading parentheses."""
    first = first_keyword(toks)
    if first != "WITH":
        return first
    depth = 0
    for k, t in toks[1:]:
        if k == "(":
            depth += 1
        elif k == ")":
            depth -= 1
        elif depth == 0 and k == "word" and t.upper() in MAIN_KW:
            return t.upper()
    return ""


# T-SQL needs no `;` between statements: one "statement" can be a batch (`SELECT 1 DELETE FROM t COMMIT`).
# A read on SQL Server therefore refuses these words ANYWHERE, not only first (they never belong to a SELECT).
MSSQL_BATCH_WORDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE", "COMMIT", "ROLLBACK", "BEGIN", "SAVE",
                     "TRAN", "TRANSACTION", "DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT", "REVOKE", "DENY", "USE",
                     "DECLARE", "SET", "WAITFOR", "BACKUP", "RESTORE", "DBCC", "KILL", "SHUTDOWN", "RECONFIGURE", "BULK",
                     "INTO", "GO", "RAISERROR", "THROW", "PRINT", "GOTO", "RETURN", "WHILE", "IF"}


def kind(sql: str, engine: str = "mysql") -> str:
    """select | dml | ddl | other of ONE statement (the write path's preview decision)."""
    st = statements(tokens(sql, engine))
    if len(st) != 1:
        raise SqlError("one statement per entry (got %d) - pass several in `statements`" % len(st))
    kw = main_keyword(st[0])
    t0 = st[0]
    if first_keyword(t0) == "WITH" and any(t0[i][0] == "word" and t0[i][1].upper() == "AS" and t0[i + 1][0] == "("
                                           and t0[i + 2][0] == "word" and t0[i + 2][1].upper() in ("INSERT", "UPDATE", "DELETE", "MERGE")
                                           for i in range(len(t0) - 2)):
        return "dml"   # PostgreSQL: WITH d AS (DELETE ... RETURNING id) SELECT ... writes (REPLACE() is a function)
    if kw in ("SELECT", "TABLE", "VALUES", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "HELP"):
        if engine == "mssql" and set(_words(st[0])) & MSSQL_BATCH_WORDS:
            return "other"   # a SELECT with a write behind it in the same batch: never "just a read"
        return "select"
    if kw in DML_FIRST:
        return "dml"
    if kw in DDL_FIRST:
        return "ddl"
    return "other"


RW = "switch the console to read-write to run it"   # the human's console (read_check console=True, L6)


def read_check(sql: str, engine: str = "mysql", console: bool = False, optimizer_hints: bool = False) -> str:
    """Refuse (SqlError) what may not go to the read connection; return the main keyword.
    console: the human's read-only console - the messages speak of its read-write switch, not of
    the agent's tools (db_execute and its card).
    optimizer_hints (the GUI console, policy console_optimizer_hints): MySQL `/*+ */` hints pass - they tune
    the statement (SET_VAR of a hint-updatable variable), never run one; `/*! */` stays refused."""
    path = "in a read-only console" if console else "on the read path"
    if RAW_DENY.search(sql.replace("/*+", "/* ") if optimizer_hints else sql):
        raise SqlError("refused %s: INTO OUTFILE/DUMPFILE, FOR UPDATE/SHARE, /*! */ and /*+ */ "
                       "comments and file functions are not allowed (%s)"
                       % (path, RW if console else "rewrite it, or use db_execute"))
    toks = tokens(sql, engine)
    st = statements(toks)
    if not st:
        raise SqlError("empty statement")
    if len(st) > 1:
        raise SqlError("the read-only console runs one statement at a time - run them one by one, or %s"
                       % RW.replace(" to run it", "") if console else
                       "one statement per call - send the others in separate calls")
    toks = st[0]
    if any(k == "hint" and not (optimizer_hints and t.startswith("/*+")) for k, t in toks):
        raise SqlError("refused %s: /*! */ and /*+ */ comments are executable in MySQL" % path)
    first = first_keyword(toks)
    allowed = READ_FIRST + (CONSOLE_FIRST if console else ())
    if first not in allowed:
        if console:   # the human's console: no agent tool names (F3 N-F3-k)
            raise SqlError("%s is not a read statement and the console is read-only - %s" % (first or "this", RW))
        raise SqlError("%s is not a read statement - db_query runs only %s; changes go through db_execute "
                       "(the user approves them)" % (first or "this", ", ".join(READ_FIRST[:-1])))
    words = _words(toks)
    if first == "SET":
        rest = [t for t in toks[1:]]
        if not rest or not (rest[0][0] == "var" and not rest[0][1].startswith("@@")
                            or rest[0][0] == "word" and rest[0][1].upper() in ("NAMES", "CHARACTER")):
            raise SqlError("only SET @variable and SET NAMES are allowed in a read-only console")
        if any(k == "var" and t.startswith("@@") for k, t in toks):
            raise SqlError("system variables cannot be set in a read-only console")
        # every item of the list, not only the first: SET NAMES utf8mb4, SESSION transaction_read_only = 0
        if any(k == "word" and t.upper() in ("SESSION", "GLOBAL", "PERSIST", "PERSIST_ONLY", "LOCAL", "TRANSACTION",
                                             "AUTOCOMMIT", "SQL_MODE") for k, t in toks[1:]):
            raise SqlError("only SET @variable and SET NAMES are allowed in a read-only console")
        for i in range(1, len(toks) - 1):   # `, name =` - a system variable without @@
            if toks[i - 1] == ("op", ",") and toks[i][0] == "word" and toks[i + 1] == ("op", "="):
                raise SqlError("only SET @variable and SET NAMES are allowed in a read-only console")
    if first == "WITH" and main_keyword(toks) not in ("SELECT", "TABLE", "VALUES"):
        raise SqlError("this WITH does not end in a SELECT - %s" % (RW if console else "changes go through db_execute"))
    if first in ("EXPLAIN", "DESCRIBE", "DESC") and ("ANALYZE" in words or "ANALYSE" in words):   # PG spells both
        raise SqlError("EXPLAIN ANALYZE executes the statement - %s" % (
            "use plain EXPLAIN, or " + RW if console else "use plain EXPLAIN (db_explain)"))
    for seq in DENY_SEQ:
        L = len(seq)
        if any(tuple(words[i:i + L]) == seq for i in range(len(words) - L + 1)):
            raise SqlError("refused %s: %s%s" % (path, " ".join(seq), " (%s)" % RW if console else ""))
    if engine == "mssql":
        hit = sorted(set(words) & MSSQL_BATCH_WORDS)
        if hit:
            raise SqlError("refused %s: %s - SQL Server runs a batch without `;`, so a read may contain "
                           "no other statement (%s)" % (path, ", ".join(hit), RW if console else "changes go through db_execute"))
    deny = set(DENY_FUNCS.get(engine, ()))
    for i, (k, t) in enumerate(toks):
        if k == "word" and t.upper() in deny:
            raise SqlError("refused %s: %s()%s" % (path, t.upper(), " (%s)" % RW if console else ""))
        if k == "word" and t.upper() == "INTO" and first in ("SELECT", "WITH", "TABLE", "VALUES") \
                and engine in ("postgres", "mssql"):
            raise SqlError("SELECT INTO creates a table in %s - %s" % (engine, RW if console else "use db_execute"))
    return main_keyword(toks) or first


def split(script: str, engine: str = "mysql") -> list[str]:
    """A .sql script into statement texts (comments kept inside a statement, empty ones dropped)."""
    if re.search(r"^\s*DELIMITER\s", script, re.I | re.M):
        raise SqlError("DELIMITER is a mysql client command - define routines one per entry of `statements`")
    if engine == "oracle":
        if re.search(r"^\s*(SET|DEFINE|SPOOL|PROMPT|WHENEVER|@)\b", script, re.I | re.M):
            raise SqlError("SQL*Plus commands (SET, DEFINE, SPOOL, @file) are not SQL - remove them")
        parts, buf = [], []
        for ln in script.splitlines():
            if ln.strip() == "/":
                parts.append("\n".join(buf))
                buf = []
            else:
                buf.append(ln)
        parts.append("\n".join(buf))
        block = re.compile(r"^\s*(DECLARE|BEGIN|CREATE\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE))\b",
                           re.I | re.M)
        out = []
        for p in parts:   # a PL/SQL block runs to the `/`; what precedes it splits at `;`
            m = block.search(p)
            if m:
                out += _split_semicolons(p[:m.start()], engine) if p[:m.start()].strip() else []
                out.append(p[m.start():].strip())
            else:
                out += _split_semicolons(p, engine)
        return [s for s in out if s.strip()]
    return _split_semicolons(script, engine)


def _split_semicolons(script: str, engine: str) -> list[str]:
    tokens(script, engine)   # raises on an unterminated quote/comment
    out, start, i, n = [], 0, 0, len(script)
    mysql = engine == "mysql"
    while i < n:
        c = script[i]
        if c == "-" and script.startswith("--", i) and (not mysql or i + 2 >= n or script[i + 2].isspace()):
            j = script.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "#" and mysql:
            j = script.find("\n", i)
            i = n if j < 0 else j + 1
        elif script.startswith("/*", i):
            j = script.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"`":
            j = i + 1
            while j < n:
                if mysql and script[j] == "\\" and c != "`":
                    j += 2
                    continue
                if script[j] == c:
                    if script.startswith(c + c, j):
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif c == "$" and engine == "postgres" and re.match(r"\$([A-Za-z_]\w*)?\$", script[i:]):
            tag = re.match(r"\$([A-Za-z_]\w*)?\$", script[i:]).group(0)
            end = script.find(tag, i + len(tag))
            i = n if end < 0 else end + len(tag)
        elif c == ";":
            out.append(script[start:i])
            start = i = i + 1
        else:
            i += 1
    out.append(script[start:])
    return [s.strip() for s in out if tokens(s, engine)]


# ------------------------------------------------------------------ tables (engine gate)
TABLE_AFTER = ("UPDATE", "INTO", "FROM", "JOIN", "USING", "TABLE")
SKIP_AFTER = ("LOW_PRIORITY", "IGNORE", "QUICK", "DELAYED", "HIGH_PRIORITY", "ONLY", "LATERAL")
STOP = {"SET", "WHERE", "VALUES", "VALUE", "SELECT", "ON", "USING", "ORDER", "LIMIT", "GROUP", "HAVING",
        "PARTITION", "AS", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "NATURAL", "JOIN", "STRAIGHT_JOIN",
        "WINDOW", "UNION", "RETURNING", "DEFAULT", "FROM", "WITH", "FOR", "LOCK", "ROW", "TABLE", "DUAL"}


def tables(sql: str, engine: str = "mysql") -> list[tuple[str, str]] | None:
    """[(schema or "", table)] a statement may touch - every name after UPDATE/INTO/FROM/JOIN/
    USING (comma lists too). None when a name cannot be read (a function in FROM, a variable):
    the caller then treats the engine as unknown - no preview."""
    toks = tokens(sql, engine)
    cte = set()
    if first_keyword(toks) == "WITH":   # CTE names are not tables
        depth = 0
        for i, (k, t) in enumerate(toks):
            if k == "(":
                depth += 1
            elif k == ")":
                depth -= 1
            elif depth == 0 and k in ("word", "ident") and i + 1 < len(toks) and \
                    (toks[i + 1][0] == "(" or toks[i + 1][0] == "word" and toks[i + 1][1].upper() == "AS") \
                    and t.upper() not in ("WITH", "RECURSIVE", "AS"):
                cte.add(t.lower())
    out: list[tuple[str, str]] = []
    i, n = 0, len(toks)
    while i < n:
        k, t = toks[i]
        if k == "word" and t.upper() in TABLE_AFTER:
            i += 1
            while True:
                while i < n and toks[i][0] == "word" and toks[i][1].upper() in SKIP_AFTER:
                    i += 1
                if i >= n:
                    break
                if toks[i][0] == "(":   # a derived table / subquery: its own FROM is scanned anyway
                    break
                if toks[i][0] not in ("word", "ident") or toks[i][0] == "word" and toks[i][1].upper() in STOP:
                    break
                name = [toks[i][1]]
                i += 1
                while i + 1 < n and toks[i] == ("op", ".") and toks[i + 1][0] in ("word", "ident"):
                    name.append(toks[i + 1][1])
                    i += 2
                if i < n and toks[i][0] == "(" and t.upper() in ("FROM", "JOIN", "USING"):
                    return None   # a function call in FROM (json_table, generate_series...); after INTO: a column list
                if len(name) > 2:
                    name = name[-2:]
                if name[-1].lower() not in cte:
                    out.append((name[0] if len(name) == 2 else "", name[-1]))
                # alias
                if i < n and toks[i][0] == "word" and toks[i][1].upper() == "AS":
                    i += 2
                elif i < n and toks[i][0] in ("word", "ident") and toks[i][1].upper() not in STOP | set(TABLE_AFTER):
                    i += 1
                if i < n and toks[i] == ("op", ","):
                    i += 1
                    continue
                break
            continue
        if k == "var":
            return None
        i += 1
    seen, uniq = set(), []
    for s, t in out:
        key = (s.lower(), t.lower())
        if key not in seen:
            seen.add(key)
            uniq.append((s, t))
    return uniq


def select_list(sql: str, engine: str = "mysql") -> list[list]:
    """The token lists of the outer SELECT list's items, in order (a subquery stays inside its item)."""
    toks = statements(tokens(sql, engine))
    toks = toks[0] if toks else []
    depth, start, items, cur = 0, None, [], []
    for k, t in toks:
        u = t.upper() if k == "word" else ""
        if start is None:
            if k == "word" and u == "SELECT" and depth == 0:
                start = True
            elif k == "(":
                depth += 1
            elif k == ")":
                depth -= 1
            continue
        if k == "(":
            depth += 1
        elif k == ")":
            depth -= 1
        if depth == 0 and (k == "word" and u in ("FROM", "INTO", "WHERE", "UNION", "ORDER", "GROUP", "LIMIT", "HAVING")):
            break
        if depth == 0 and k == "op" and t == ",":
            items.append(cur)
            cur = []
            continue
        if depth == 0 and k == "word" and u in ("DISTINCT", "ALL") and not cur:
            continue
        cur.append((k, t))
    if cur:
        items.append(cur)
    return items


def select_items(sql: str, engine: str = "mysql") -> tuple[set, bool, set]:
    """(plain column names, has *, alias names) of the outer SELECT list: a plain item is a bare
    column (`name`, `t.name`); `email AS e`, `email e`, CONCAT(...) AS c are aliases. Masking uses it
    where the driver cannot say which column a result column came from."""
    items = select_list(sql, engine)
    plain, aliases, star = set(), set(), False
    for it in items:
        names = [(k, t) for k, t in it]
        if names == [("op", "*")] or (len(names) == 3 and names[1] == ("op", ".") and names[2] == ("op", "*")):
            star = True
        elif len(names) == 1 and names[0][0] in ("word", "ident"):
            plain.add(names[0][1].lower())
        elif len(names) == 3 and names[1] == ("op", ".") and names[2][0] in ("word", "ident"):
            plain.add(names[2][1].lower())
        elif names and names[-1][0] in ("word", "ident"):
            aliases.add(names[-1][1].lower())
    return plain, star, aliases


def called(sql: str, engine: str = "mysql") -> set[str]:
    """Lower-case names used as a function call (word followed by `(`); a schema-qualified call
    (`other.f(`) comes back as "other.f" - the preview gate treats any of those as unknown."""
    toks = tokens(sql, engine)
    out = set()
    for i, (k, t) in enumerate(toks[:-1]):
        if k in ("word", "ident") and toks[i + 1][0] == "(":
            q = i >= 2 and toks[i - 1] == ("op", ".") and toks[i - 2][0] in ("word", "ident")
            out.add(("%s.%s" % (toks[i - 2][1], t) if q else t).lower())
    return out


def has_hint(sql: str, engine: str = "mysql") -> bool:
    """A MySQL executable comment (/*! */ or /*+ */): what is inside runs, but no rule here sees it."""
    return engine == "mysql" and any(k == "hint" for k, _ in tokens(sql, engine))


def has_where(sql: str, engine: str = "mysql") -> bool:
    """UPDATE/DELETE with a WHERE at depth 0 (the card warns without one; prod refuses)."""
    depth = 0
    for k, t in tokens(sql, engine):
        if k == "(":
            depth += 1
        elif k == ")":
            depth -= 1
        elif depth == 0 and k == "word" and t.upper() == "WHERE":
            return True
    return False


def where_clause(sql: str, engine: str = "mysql") -> str | None:
    """The text of the depth-0 WHERE of an UPDATE / DELETE (up to ORDER BY / LIMIT / RETURNING),
    or None - for the card's sample of the rows a write touches."""
    i, n, depth, start = 0, len(sql), 0, None
    mysql = engine == "mysql"
    low = sql.lower()
    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i) and (not mysql or i + 2 >= n or sql[i + 2].isspace()) or c == "#" and mysql:
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"`":
            j = i + 1
            while j < n:
                if mysql and sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if sql.startswith(c + c, j):
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            m = re.match(r"(where|order\s+by|limit|returning)\b", low[i:])
            if m:
                if m.group(1) == "where" and start is None:
                    start = i + 5
                elif start is not None and m.group(1) != "where":
                    return sql[start:i].strip() or None
                i += len(m.group(0))
                continue
        i += 1
    return sql[start:].strip().rstrip(";").strip() or None if start is not None else None


# ------------------------------------------------------------------ masking
def name_parts(col: str) -> list[str]:
    """customerEmail / customer_email / CUSTOMER-EMAIL -> ['customer', 'email']."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", col or "")
    return [p for p in re.split(r"[^A-Za-z0-9]+|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", s.lower()) if p]


def masked(col: str, words=MASK_WORDS) -> bool:
    parts = name_parts(col)
    cands = set(parts)
    for i in range(len(parts)):   # adjacent parts joined: e_mail, card_number, authentication_string
        for j in range(i + 2, min(len(parts), i + 3) + 1):
            cands.add("".join(parts[i:j]))
    cands -= set(NOT_MASKED)
    for w in words:
        w = w.lower()
        if w.endswith("*"):
            if any(c.startswith(w[:-1]) for c in cands):
                return True
        elif w in cands:
            return True
    return False


def quote_ident(name: str, engine: str = "mysql") -> str:
    if engine == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if engine == "mssql":
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'
