"""Anonymizing an agent's database results by an extension's rules, and the personal-data query guard.

Masking by column name (dbsql.MASK_WORDS) hides whole columns. A database may also carry its own
anonymization rules (the statements that anonymize a dump); an extension turns them into the rules here
(extension hook `db_anonymization(connection, read) -> rules | None`, dbtools). The query still runs
unchanged on the server; Piper rewrites the VALUES of the result before an agent sees them:

  rules = {"tables": {table: {"columns": {column: rule},
                              "drop_rows": [{"column": c, "equals": v} | {"all": True}],
                              "mask_all": bool}}}
  rule  = {"op": "const", "value": v} | {"op": "token", "prefix": p} | {"op": "email", "domain": d}
        | {"op": "column", "column": c, "prefix": p} | {"op": "json", "paths": {"$.a.b": rule}}
        | {"op": "mask"}  ;  optional "when": "notnull" | {"like": pattern}  (on the same value)

Table and column names are matched case-insensitively. A `token` / `email` is an HMAC of the value under a
key that lives only in this process: the same value gives the same pseudonym while piperd runs (joins and
GROUP BY over results stay consistent), nothing reverses it. `column` copies a sibling column of the same
table instance in the same row (masked when the result does not carry it). `drop_rows` rows (rows a dump
deletes) are masked whole; `mask_all` masks every column of the table (a view over personal data).

The guard: values are rewritten AFTER the server read them, so a WHERE / JOIN / ORDER BY over a personal
column still runs on the real values - an agent could learn them from which rows match
(`WHERE email LIKE 'a%'`). guard() names every such use; dbtools then asks the human on a card for that
one query. Plain `x IS [NOT] NULL` and `a.x = b.y` between two bare columns of a flat query reveal no value
and pass. Anything the scanner cannot read is a reason too (fail closed).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata

import dbsql

KEY = secrets.token_bytes(32)   # per process, never stored: pseudonyms are not stable across restarts
MASK = "••••"
OPS = ("const", "token", "email", "column", "json", "mask")
MAX_JSON = 1 << 20


class RulesError(ValueError):
    """An extension's rules are malformed - the caller fails closed."""


# ------------------------------------------------------------------ rules
def _rule(r, where: str, nested: bool = False) -> dict:
    if not isinstance(r, dict) or r.get("op") not in OPS:
        raise RulesError("%s: op must be one of %s" % (where, ", ".join(OPS)))
    op = r["op"]
    out: dict = {"op": op}
    if op == "const":
        v = r.get("value")
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise RulesError("%s: const value must be a string, number, boolean or null" % where)
        out["value"] = v
    elif op == "token":
        out["prefix"] = str(r.get("prefix") or "")
    elif op == "email":
        out["domain"] = str(r.get("domain") or "anon.invalid")
    elif op == "column":
        if not isinstance(r.get("column"), str) or not r["column"]:
            raise RulesError("%s: column rule needs a column" % where)
        out["column"] = r["column"].lower()
        out["prefix"] = str(r.get("prefix") or "")
    elif op == "json":
        if nested:
            raise RulesError("%s: json rules do not nest" % where)
        paths = r.get("paths")
        if not isinstance(paths, dict) or not paths:
            raise RulesError("%s: json rule needs paths" % where)
        out["paths"] = {}
        for p, sub in paths.items():
            if not isinstance(p, str) or not re.fullmatch(r"\$(\.[A-Za-z0-9_]+)+", p):
                raise RulesError("%s: unsupported JSON path %r ($.a.b only)" % (where, p))
            out["paths"][p] = _rule(sub, "%s %s" % (where, p), nested=True)
    when = r.get("when")
    if when is not None:
        if when == "notnull":
            pass
        elif isinstance(when, dict) and isinstance(when.get("like"), str):
            when = {"like": when["like"]}
        else:
            raise RulesError("%s: when must be \"notnull\" or {\"like\": pattern}" % where)
        out["when"] = when
    return out


def validate(raw) -> dict:
    """The rules in their normal form (lower-case names); RulesError when anything is off."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tables"), dict):
        raise RulesError("rules must be {\"tables\": {...}}")
    tables = {}
    for t, spec in raw["tables"].items():
        if not isinstance(t, str) or not t or not isinstance(spec, dict):
            raise RulesError("table %r: a name and an object are required" % (t,))
        cols = spec.get("columns") or {}
        if not isinstance(cols, dict):
            raise RulesError("table %s: columns must be an object" % t)
        drops = []
        for d in spec.get("drop_rows") or []:
            if isinstance(d, dict) and d.get("all") is True:
                drops.append({"all": True})
            elif isinstance(d, dict) and isinstance(d.get("column"), str) and \
                    (d.get("equals") is None or isinstance(d.get("equals"), (str, int, float))):
                drops.append({"column": d["column"].lower(), "equals": d.get("equals")})
            else:
                raise RulesError("table %s: drop_rows takes {column, equals} or {all: true}" % t)
        entry = {"columns": {}, "drop_rows": drops, "mask_all": spec.get("mask_all") is True}
        for c, r in cols.items():
            if not isinstance(c, str) or not c:
                raise RulesError("table %s: a column name is required" % t)
            entry["columns"][c.lower()] = _rule(r, "%s.%s" % (t, c))
        key = t.lower()
        if key in tables:   # the same table twice (two extensions, two spellings): the stricter wins
            old = tables[key]
            for c, r in entry["columns"].items():
                if c in old["columns"] and old["columns"][c] != r:
                    r = {"op": "mask"}
                old["columns"][c] = r
            old["drop_rows"] += entry["drop_rows"]
            old["mask_all"] = old["mask_all"] or entry["mask_all"]
        else:
            tables[key] = entry
    return {"tables": tables}


def merge(many: list) -> dict | None:
    """Several extensions' rules as one (a column both name differently is masked)."""
    found = [r for r in many if r is not None]
    if not found:
        return None
    out = validate(found[0])
    for r in found[1:]:
        v = validate(r)
        for t, spec in v["tables"].items():
            if t not in out["tables"]:
                out["tables"][t] = spec
                continue
            old = out["tables"][t]
            for c, rule in spec["columns"].items():
                old["columns"][c] = rule if c not in old["columns"] or old["columns"][c] == rule else {"op": "mask"}
            old["drop_rows"] += spec["drop_rows"]
            old["mask_all"] = old["mask_all"] or spec["mask_all"]
    return out


def column_names(rules: dict | None, tables: list | None) -> set:
    """Lower-case names of the rule columns of the tables a query reads (every table's when unknown)."""
    if not rules:
        return set()
    want = None if tables is None else {t.lower() for _, t in tables}
    return {c for t, spec in rules["tables"].items() if want is None or t in want for c in spec["columns"]}


def guarded_tables(rules: dict | None, tables: list | None) -> list:
    """The tables a query reads whose rules do not name their columns: mask_all (views over personal data)
    and those whose rows a dump deletes (drop_rows) - all of them when the tables are unknown. A filter over
    one selects rows by its data, and a computed column over one spells it out."""
    if not rules:
        return []
    want = None if tables is None else {t.lower() for _, t in tables}
    return sorted(t for t, spec in rules["tables"].items() if (spec["mask_all"] or spec["drop_rows"])
                  and (want is None or t in want))


def masked_tables(rules: dict | None, tables: list | None) -> list:
    """The mask_all tables (views over personal data) a query reads; all of them when unknown."""
    if not rules:
        return []
    want = None if tables is None else {t.lower() for _, t in tables}
    return sorted(t for t, spec in rules["tables"].items() if spec["mask_all"] and (want is None or t in want))


# ------------------------------------------------------------------ values
def _digest(kind: str, value) -> str:
    v = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hmac.new(KEY, ("%s\0%s" % (kind, v)).encode("utf-8", "replace"), hashlib.sha256).hexdigest()


def _fold(text: str) -> str:
    """Case and accents folded the way MySQL's default (accent- and case-insensitive) collations compare."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).casefold()


def _like(value, pattern: str) -> bool:
    """MySQL LIKE with the default case- and accent-insensitive collation (`\\` escapes). It may match more than
    MySQL (`_bin` columns): a rule then rewrites a value the dump kept - never the other way round."""
    return _like_raw(str(value), pattern) or _like_raw(_fold(str(value)), _fold(pattern))


def _like_raw(value: str, pattern: str) -> bool:
    rx, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            rx.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        rx.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(rx), value, re.S | re.I) is not None


def applies(rule: dict, value) -> bool:
    """Does the rule rewrite this value? NULL stays NULL (anonymizing keeps NULLs; IS NULL reveals nothing)."""
    if value is None:
        return False
    w = rule.get("when")
    if isinstance(w, dict):
        return _like(value, w["like"])
    return True


def transform(rule: dict, value, sibling=None):
    """The anonymized value. sibling(column) -> (found, value) of the same table instance's row."""
    if not applies(rule, value):
        return value
    op = rule["op"]
    if op == "const":
        return rule["value"]
    if op == "token":
        return rule["prefix"] + _digest("token", value)[:8]
    if op == "email":
        return _digest("email", value)[:16] + "@" + rule["domain"]
    if op == "column":
        found, v = sibling(rule["column"]) if sibling else (False, None)
        if not found or isinstance(v, (dict, list)):
            return MASK
        return rule["prefix"] + ("" if v is None else str(v))
    if op == "json":
        return _json(rule, value, sibling)
    return MASK


def _json(rule: dict, value, sibling):
    if not isinstance(value, str) or len(value) > MAX_JSON:
        return MASK
    try:
        doc = json.loads(value)
    except ValueError:
        return MASK
    for path, sub in rule["paths"].items():
        keys = path[2:].split(".")
        node = doc
        for k in keys[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(keys[-1]) is not None:
            old = node[keys[-1]]
            new = transform(sub, old if isinstance(old, str) else json.dumps(old), sibling)
            node[keys[-1]] = new
    return json.dumps(doc, ensure_ascii=False, separators=(", ", ": "))


# ------------------------------------------------------------------ the query scan
SELECT_END = {"FROM", "INTO", "WHERE", "UNION", "INTERSECT", "EXCEPT", "MINUS", "ORDER", "GROUP", "LIMIT",
              "HAVING", "WINDOW", "FOR", "OFFSET", "FETCH", "QUALIFY"}
NOT_ALIAS = {"END", "NULL", "TRUE", "FALSE", "DISTINCT", "ALL", "SELECT", "THEN", "ELSE", "AND", "OR", "NOT"}
CLAUSE = {"SELECT": "select", "FROM": "from", "JOIN": "from", "STRAIGHT_JOIN": "from", "WHERE": "cond",
          "ON": "cond", "USING": "cond", "HAVING": "cond", "QUALIFY": "cond", "GROUP": "group",
          "ORDER": "order", "PARTITION": "order", "WINDOW": "order", "LIMIT": "limit", "OFFSET": "limit",
          "FETCH": "limit", "UNION": "setop", "INTERSECT": "setop", "EXCEPT": "setop", "MINUS": "setop",
          "WITH": "with", "INTO": "limit", "FOR": "limit"}
BOUND_BEFORE = {"WHERE", "ON", "AND", "OR", "HAVING", "NOT", "WHEN"}
BOUND_AFTER = {"AND", "OR", "WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "CROSS",
               "FULL", "NATURAL", "STRAIGHT_JOIN", "UNION", "INTERSECT", "EXCEPT", "WINDOW", "THEN", "OFFSET", "FETCH"}
NOT_FUNC = set(CLAUSE) | {"AS", "IN", "EXISTS", "ANY", "ALL", "SOME", "NOT", "AND", "OR", "LATERAL", "BY", "THEN",
                          "ELSE", "WHEN", "IS", "VALUES", "OVER", "USING", "CASE", "BETWEEN", "LIKE", "INNER", "LEFT",
                          "RIGHT", "OUTER", "CROSS", "FULL", "NATURAL", "RECURSIVE", "DISTINCT", "RETURN"}
VALUE_WORDS = {"NULL", "TRUE", "FALSE", "CURRENT_USER", "USER", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
               "NOW", "DEFAULT", "UNKNOWN"}


def _statement(sql: str, engine: str) -> list:
    st = dbsql.statements(dbsql.tokens(sql, engine))
    if len(st) != 1:
        raise dbsql.SqlError("one statement expected")
    return st[0]


def select_items(toks: list) -> list[list]:
    """The items of EVERY select list (subqueries, CTEs, UNION branches) as token lists."""
    return [it for _, it in select_items_at(toks)]


def select_items_at(toks: list) -> list[tuple[int, list]]:
    """(parenthesis depth of its SELECT, item) of every select list item."""
    out = []
    depth_at = []
    d = 0
    for i, (k, t) in enumerate(toks):
        if k == "(":
            d += 1
        elif k == ")":
            d -= 1
        elif k == "word" and t.upper() == "SELECT":
            depth_at.append((i, d))
    for i, d0 in depth_at:
        d, cur = d0, []
        for k, t in toks[i + 1:]:
            if k == "(":
                d += 1
            elif k == ")":
                if d == d0:
                    break
                d -= 1
            if d == d0 and (k == "word" and t.upper() in SELECT_END or k == ";"):
                break
            if d == d0 and k == "op" and t == ",":
                out.append((d0, cur))
                cur = []
                continue
            cur.append((k, t))
        if cur:
            out.append((d0, cur))
    return out


def _alias(item: list) -> str | None:
    it = [x for x in item if not (x[0] == "word" and x[1].upper() in ("DISTINCT", "ALL"))]
    if len(it) >= 2 and it[-1][0] == "str":   # AS 'e' (MySQL also AS "e"): a quoted alias
        prev = it[-2]
        return it[-1][1].lower() if prev[0] in ("word", "ident", "num", "str", ")") else None
    if len(it) < 2 or it[-1][0] not in ("word", "ident") or it[-2] == ("op", "."):
        return None
    if it[-1][0] == "word" and it[-1][1].upper() in NOT_ALIAS:
        return None
    return it[-1][1].lower()


def _bare(item: list) -> bool:
    """Is the select item a plain (qualified) column, `t.*` or `*`?"""
    it = [x for x in item if not (x[0] == "word" and x[1].upper() in ("DISTINCT", "ALL"))]
    if not it:
        return False
    for n, x in enumerate(it):
        if n % 2:
            if x != ("op", "."):
                return False
        elif x[0] not in ("word", "ident") and x != ("op", "*"):
            return False
    return len(it) % 2 == 1


# a personal value inside these in a select list answers through NULL, an error or the time, not its (masked) value
SIDE_FUNCS = {"IF", "IIF", "NULLIF", "ELT", "CHOOSE", "DECODE", "SLEEP", "PG_SLEEP", "PG_SLEEP_FOR", "BENCHMARK",
              "EXP", "POW", "POWER"}
# functions that cannot fail, wait or branch on their arguments: a personal value computed only by these in a
# select list answers through its (masked) result and nothing else. Any other function, an operator, CASE or a
# comparison over it may answer through an error (`EXP(ASCII(email) * 9)`), a NULL or the time.
SAFE_FUNCS = {"COUNT", "MIN", "MAX", "ANY_VALUE", "GROUP_CONCAT", "STRING_AGG", "ARRAY_AGG", "CONCAT", "CONCAT_WS", "COALESCE", "IFNULL", "LOWER", "UPPER", "LCASE", "UCASE", "TRIM",
              "LTRIM", "RTRIM", "LENGTH", "CHAR_LENGTH", "CHARACTER_LENGTH", "OCTET_LENGTH", "SUBSTRING", "SUBSTR",
              "MID", "LEFT", "RIGHT", "REPLACE", "MD5", "SHA1", "SHA2", "HEX", "ASCII", "ORD", "YEAR", "MONTH",
              "DAY", "DAYOFMONTH", "DATE", "DATE_FORMAT", "EXTRACT", "DATE_PART", "DATE_TRUNC", "DISTINCT", "ALL"}
# (not the JSON functions, JSON aggregates or -> / ->>: they fail on text that is no JSON or a NULL key -
# `JSON_TYPE(REPLACE(LEFT(x, 2), 'jo', 1))`, `JSON_OBJECTAGG(DATE(…), 1)`;
# the date functions answer NULL, never an error, and a read's warnings never reach the agent)
BRANCH_WORDS = {"CASE", "WHEN", "AND", "OR", "XOR", "LIKE", "ILIKE", "REGEXP", "RLIKE", "IN",
                "BETWEEN", "SIMILAR", "GLOB", "SOUNDS", "DIV", "MOD", "ESCAPE"}


def _computed_unsafe(item: list) -> bool:
    """Does the select item compute with anything but SAFE_FUNCS (another function, an operator, CASE, a
    comparison)? A scalar subquery inside is skipped: its own select list and conditions are scanned apart."""
    ok_ops = set()   # || (a concatenation or a logical OR: neither fails)
    for j in range(len(item) - 1):
        if item[j] == ("op", "|") and item[j + 1] == ("op", "|"):
            ok_ops |= {j, j + 1}
    i, n = 0, len(item)
    while i < n:
        k, t = item[i]
        if k == "(" and i + 1 < n and item[i + 1][0] == "word" and item[i + 1][1].upper() in ("SELECT", "WITH"):
            d = 0
            while i < n:
                d += (item[i][0] == "(") - (item[i][0] == ")")
                if d == 0:
                    break
                i += 1
            i += 1
            continue
        nxt = item[i + 1] if i + 1 < n else ("", "")
        prev = item[i - 1] if i else ("", "")
        if k == "ident" and nxt[0] == "(":
            return True   # a stored function: anything
        if k == "word" and (nxt[0] == "(" and t.upper() not in SAFE_FUNCS or t.upper() in BRANCH_WORDS):
            return True
        if k == "word" and t.upper() in ("FROM", "SIMILAR") and nxt[0] == "str":
            return True   # SUBSTRING(x FROM '(a+)+$'): a regular expression's time
        if k == "op" and t not in (",", ".") and i not in ok_ops and \
                not (t == "*" and (prev[0] in ("(", "") or prev == ("op", "."))):   # COUNT(*), t.*
            return True
        i += 1
    return False
COMPARE_WORDS = {"LIKE", "ILIKE", "REGEXP", "RLIKE", "IN", "BETWEEN", "SIMILAR", "GLOB", "SOUNDS", "DIV", "MOD"}


def _compares(tok) -> bool:
    return tok[0] == "op" and any(ch in tok[1] for ch in "=<>!/%") or \
        tok[0] == "word" and tok[1].upper() in COMPARE_WORDS


def _names(item: list) -> set:
    return {t.lower() for k, t in item if k in ("word", "ident")}


def personal(sql: str, engine: str, is_personal) -> tuple[set, bool]:
    """(lower-case names that hold personal data, opaque): is_personal(name) names the source columns; an
    alias of a select item over one of them is personal too (`SELECT d.x FROM (SELECT email AS x ...) d` -
    MySQL then reports x as the source column). opaque: a column list renames a CTE / derived table
    (`WITH c(a) AS`, `) d(a)`) or the SQL does not scan - names are then unknown."""
    try:
        toks = _statement(sql, engine)
    except dbsql.SqlError:
        return set(), True
    names = {t.lower() for k, t in toks if k in ("word", "ident") and is_personal(t)}
    items = select_items(toks)
    for _ in range(8):
        grown = {a for a in (_alias(it) for it in items if _names(it) & names) if a} - names
        # a subquery's unaliased expression is named by its text (MySQL: `ASCII(nick)`) or its function (PostgreSQL)
        for d, it in select_items_at(toks):
            if d > 0 and _alias(it) is None and not _bare(it) and _names(it) & names and \
                    len(it) > 1 and it[0][0] == "word" and it[1][0] == "(":
                grown.add(it[0][1].lower())
        grown |= {t.lower() for k, t in toks if k == "ident" and set(re.findall(r"[\w$]+", t.lower())) & names}
        grown -= names
        if not grown:
            break
        names |= grown
    opaque = False
    for i, (k, t) in enumerate(toks[:-2]):
        if k == ")" and toks[i + 1][0] in ("word", "ident"):
            j = i + 2 if toks[i + 1] == ("word", "AS") or toks[i + 1][0] == "word" and toks[i + 1][1].upper() == "AS" else i + 1
            if j == i + 2 and j < len(toks) and toks[j][0] in ("word", "ident") and j + 1 < len(toks) and toks[j + 1][0] == "(":
                opaque = True   # ) AS d (a, b)
            elif j == i + 1 and toks[j][1].upper() not in CLAUSE and toks[j][1].upper() not in (
                    "OVER", "FILTER", "WITHIN", "AND", "OR", "ON", "IN", "IS", "AS", "NOT", "INNER", "LEFT", "RIGHT",
                    "CROSS", "FULL", "NATURAL", "THEN", "ELSE", "END", "WHEN", "ASC", "DESC") and \
                    j + 1 < len(toks) and toks[j + 1][0] == "(":
                opaque = True   # ) d (a, b)
    if toks and toks[0][0] == "word" and toks[0][1].upper() == "WITH":
        for i, (k, t) in enumerate(toks[:-1]):
            if k in ("word", "ident") and toks[i + 1][0] == "(" and i > 0 and \
                    (toks[i - 1] in (("op", ","),) or toks[i - 1][0] == "word" and toks[i - 1][1].upper() in ("WITH", "RECURSIVE")):
                j, d = i + 1, 0
                while j < len(toks):   # the matching ) and then AS: a column list
                    if toks[j][0] == "(":
                        d += 1
                    elif toks[j][0] == ")":
                        d -= 1
                        if d == 0:
                            break
                    j += 1
                if j + 1 < len(toks) and toks[j + 1][0] == "word" and toks[j + 1][1].upper() == "AS":
                    opaque = True
    return names, opaque


def nested(toks: list) -> bool:
    """A subquery, a CTE, VALUES or a table function (`JSON_TABLE('["a@x"]', …) g` - a list of guesses whose
    `u.email = g.v` is no join of two tables' columns)."""
    words = [t.upper() for k, t in toks if k == "word"]
    if words.count("SELECT") > 1 or "WITH" in words[:1] or "VALUES" in words or "LATERAL" in words:
        return True
    clause = None
    for i, (k, t) in enumerate(toks[:-1]):
        if k == "word" and t.upper() in CLAUSE:
            clause = CLAUSE[t.upper()]
        elif clause == "from" and k in ("word", "ident") and toks[i + 1][0] == "(" and \
                not (k == "word" and t.upper() in NOT_FUNC):
            return True
    return False


def hidden_star(toks: list) -> bool:
    """Does a `*` or `TABLE t` pass columns on under names the SQL never spells - into a set operation (the first
    branch names them: `SELECT ORD(b) FROM (SELECT 0 a, '' b FROM dual WHERE 0 UNION ALL SELECT * FROM t_user) d`)
    or into a subquery / CTE renamed by a column list (`WITH c (a, b) AS (TABLE t_user)`)? A `*` of a derived
    table or an EXISTS subquery keeps the column names, and the outer query has to spell them."""
    setop = any(k == "word" and t.upper() in ("UNION", "INTERSECT", "EXCEPT", "MINUS") for k, t in toks)
    renamed = []   # per frame: is this parenthesis a subquery a column list renames?
    stack = []
    for i, (k, t) in enumerate(toks):
        if k == "(":
            j, d = i, 0
            while j < len(toks):
                d += (toks[j][0] == "(") - (toks[j][0] == ")")
                if d == 0:
                    break
                j += 1
            nxt = toks[j + 1:j + 4]
            listed = len(nxt) >= 2 and nxt[0][0] in ("word", "ident") and (nxt[1][0] == "(" or len(nxt) == 3 and
                                                                         nxt[0][1].upper() == "AS" and nxt[2][0] == "(")
            prev = toks[i - 1] if i else ("", "")
            cte = prev[0] == "word" and prev[1].upper() == "AS" and i >= 2 and toks[i - 2][0] == ")"
            stack.append(listed or cte or bool(stack and stack[-1]))
            continue
        if k == ")":
            if stack:
                stack.pop()
            continue
        star = (k, t) == ("op", "*") and i >= 1 and (toks[i - 1] == ("op", ",") or toks[i - 1][0] == "word" and
                                                    toks[i - 1][1].upper() in ("SELECT", "DISTINCT", "ALL") or
                                                    toks[i - 1] == ("op", ".") and i >= 3 and
                                                    (toks[i - 3] == ("op", ",") or toks[i - 3][0] == "word" and
                                                     toks[i - 3][1].upper() in ("SELECT", "DISTINCT", "ALL")))
        table = k == "word" and t.upper() == "TABLE" and (i == 0 or toks[i - 1][0] == "(" or toks[i - 1][0] == "word"
                                                          and toks[i - 1][1].upper() in ("UNION", "INTERSECT",
                                                                                         "EXCEPT", "ALL", "DISTINCT"))
        if (star or table) and (setop or (stack and stack[-1])):
            return True
    return False


def _qualified_end(toks, i) -> int:
    """Index of the first token of the qualified name that ends at i (a.b.c)."""
    while i >= 2 and toks[i - 1] == ("op", ".") and toks[i - 2][0] in ("word", "ident"):
        i -= 2
    return i


def _qualified_after(toks, i) -> int:
    """Index just past the qualified name that starts at i, or -1 when none starts there."""
    if i >= len(toks) or toks[i][0] not in ("word", "ident"):
        return -1
    if toks[i][0] == "word" and toks[i][1].upper() in VALUE_WORDS:
        return -1
    j = i + 1
    while j + 1 < len(toks) and toks[j] == ("op", ".") and toks[j + 1][0] in ("word", "ident"):
        j += 2
    if j < len(toks) and toks[j][0] == "(":
        return -1   # a function call
    return j


def _bound_before(toks, i) -> bool:
    return i == 0 or toks[i - 1][0] == "(" or toks[i - 1][0] == "word" and toks[i - 1][1].upper() in BOUND_BEFORE


def _bound_after(toks, j) -> bool:
    return j >= len(toks) or toks[j][0] in (")", ";") or toks[j][0] == "word" and toks[j][1].upper() in BOUND_AFTER


def _eq_at(toks, j) -> int:
    """Length of `=` or `<=>` at j, 0 when neither."""
    if j < len(toks) and toks[j] == ("op", "="):
        return 1
    if toks[j:j + 3] == [("op", "<"), ("op", "="), ("op", ">")]:
        return 3
    return 0


def _safe_use(toks, start, end, flat, aliases=frozenset()) -> bool:
    """Is the column toks[start:end] used as `x IS [NOT] NULL` or `a.x = b.y` (two bare columns, flat query)?
    A select-list alias on the other side is no column (`SELECT 'abc' AS g ... HAVING nick = g`)."""
    j = end
    if j < len(toks) and toks[j][0] == "word" and toks[j][1].upper() == "IS":
        k = j + 1
        if k < len(toks) and toks[k][0] == "word" and toks[k][1].upper() == "NOT":
            k += 1
        if k < len(toks) and toks[k][0] == "word" and toks[k][1].upper() == "NULL" and _bound_after(toks, k + 1) \
                and _bound_before(toks, start):
            return True
    if not flat:
        return False
    n = _eq_at(toks, end)
    if n and _bound_before(toks, start):
        e2 = _qualified_after(toks, end + n)
        if e2 > 0 and _bound_after(toks, e2) and not (e2 == end + n + 1 and toks[end + n][1].lower() in aliases):
            return True
    # the right-hand side: <bound> a.y = <this>
    if _bound_after(toks, end) and start >= 2:
        for n in (1, 3):
            if start - n >= 1 and _eq_at(toks, start - n) == n:
                s2 = _qualified_end(toks, start - n - 1)
                if toks[start - n - 1][0] in ("word", "ident") and _qualified_after(toks, s2) == start - n and \
                        _bound_before(toks, s2) and not (s2 == start - n - 1 and toks[s2][1].lower() in aliases):
                    return True
    return False


# ------------------------------------------------------------------ the syntax the scan covers
# The guard reads a query by names: it is sound only for syntax it knows. A reserved word outside READ_WORDS
# (NATURAL, LATERAL, STRAIGHT_JOIN, index hints, INTO, TABLESAMPLE, …) or a function outside KNOWN_FUNCS (a
# stored function reads any table) makes an agent's read on a masked connection ask: unknown syntax fails closed.
RESERVED = {
    "mysql": set("""ACCESSIBLE ADD ALL ALTER ANALYZE AND AS ASC ASENSITIVE BEFORE BETWEEN BIGINT BINARY BLOB BOTH BY
        CALL CASCADE CASE CHANGE CHAR CHARACTER CHECK COLLATE COLUMN CONDITION CONSTRAINT CONTINUE CONVERT CREATE
        CROSS CUBE CUME_DIST CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASE DATABASES
        DAY_HOUR DAY_MICROSECOND DAY_MINUTE DAY_SECOND DEC DECIMAL DECLARE DEFAULT DELAYED DELETE DENSE_RANK DESC
        DESCRIBE DETERMINISTIC DISTINCT DISTINCTROW DIV DOUBLE DROP DUAL EACH ELSE ELSEIF EMPTY ENCLOSED ESCAPED
        EXCEPT EXISTS EXIT EXPLAIN FALSE FETCH FIRST_VALUE FLOAT FLOAT4 FLOAT8 FOR FORCE FOREIGN FROM FULLTEXT
        FUNCTION GENERATED GET GRANT GROUP GROUPING GROUPS HAVING HIGH_PRIORITY HOUR_MICROSECOND HOUR_MINUTE
        HOUR_SECOND IF IGNORE IN INDEX INFILE INNER INOUT INSENSITIVE INSERT INT INT1 INT2 INT3 INT4 INT8 INTEGER
        INTERSECT INTERVAL INTO IO_AFTER_GTIDS IO_BEFORE_GTIDS IS ITERATE JOIN JSON_TABLE KEY KEYS KILL LAG
        LAST_VALUE LATERAL LEAD LEADING LEAVE LEFT LIKE LIMIT LINEAR LINES LOAD LOCALTIME LOCALTIMESTAMP LOCK LONG
        LONGBLOB LONGTEXT LOOP LOW_PRIORITY MASTER_BIND MASTER_SSL_VERIFY_SERVER_CERT MATCH MAXVALUE MEDIUMBLOB
        MEDIUMINT MEDIUMTEXT MIDDLEINT MINUTE_MICROSECOND MINUTE_SECOND MOD MODIFIES NATURAL NOT NO_WRITE_TO_BINLOG
        NTH_VALUE NTILE NULL NUMERIC OF ON OPTIMIZE OPTIMIZER_COSTS OPTION OPTIONALLY OR ORDER OUT OUTER OUTFILE
        OVER PARTITION PERCENT_RANK PRECISION PRIMARY PROCEDURE PURGE RANGE RANK READ READS READ_WRITE REAL
        RECURSIVE REFERENCES REGEXP RELEASE RENAME REPEAT REPLACE REQUIRE RESIGNAL RESTRICT RETURN REVOKE RIGHT
        RLIKE ROW ROWS ROW_NUMBER SCHEMA SCHEMAS SECOND_MICROSECOND SELECT SENSITIVE SEPARATOR SET SHOW SIGNAL
        SMALLINT SPATIAL SPECIFIC SQL SQLEXCEPTION SQLSTATE SQLWARNING SQL_BIG_RESULT SQL_CALC_FOUND_ROWS
        SQL_SMALL_RESULT SSL STARTING STORED STRAIGHT_JOIN SYSTEM TABLE TERMINATED THEN TINYBLOB TINYINT TINYTEXT
        TO TRAILING TRIGGER TRUE UNDO UNION UNIQUE UNLOCK UNSIGNED UPDATE USAGE USE USING UTC_DATE UTC_TIME
        UTC_TIMESTAMP VALUES VARBINARY VARCHAR VARCHARACTER VARYING VIRTUAL WHEN WHERE WHILE WINDOW WITH WRITE XOR
        YEAR_MONTH ZEROFILL""".split()),
    "postgres": set("""ALL ANALYSE ANALYZE AND ANY ARRAY AS ASC ASYMMETRIC AUTHORIZATION BINARY BOTH CASE CAST CHECK
        COLLATE COLLATION COLUMN CONCURRENTLY CONSTRAINT CREATE CROSS CURRENT_CATALOG CURRENT_DATE CURRENT_ROLE
        CURRENT_SCHEMA CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER DEFAULT DEFERRABLE DESC DISTINCT DO ELSE END
        EXCEPT FALSE FETCH FOR FOREIGN FREEZE FROM FULL GRANT GROUP HAVING ILIKE IN INITIALLY INNER INTERSECT INTO
        IS ISNULL JOIN LATERAL LEADING LEFT LIKE LIMIT LOCALTIME LOCALTIMESTAMP NATURAL NOT NOTNULL NULL OFFSET ON
        ONLY OR ORDER OUTER OVERLAPS PLACING PRIMARY REFERENCES RETURNING RIGHT SELECT SESSION_USER SIMILAR SOME
        SYMMETRIC SYSTEM_USER TABLE TABLESAMPLE THEN TO TRAILING TRUE UNION UNIQUE USER USING VARIADIC VERBOSE
        WHEN WHERE WINDOW WITH""".split()),
}
READ_WORDS = set("""SELECT FROM WHERE AND OR NOT XOR IN IS NULL LIKE ILIKE BETWEEN JOIN INNER LEFT RIGHT OUTER FULL
    CROSS ON USING AS GROUP BY ORDER ASC DESC LIMIT OFFSET FETCH ONLY HAVING TRUE FALSE EXISTS CASE WHEN THEN ELSE
    END DISTINCT ALL ANY SOME UNION EXCEPT INTERSECT WITH RECURSIVE INTERVAL OVER PARTITION WINDOW ROWS RANGE ROW
    COLLATE BINARY DIV MOD REGEXP RLIKE SIMILAR TO SEPARATOR DUAL TABLE VALUES FOR LEADING TRAILING BOTH ARRAY
    ISNULL NOTNULL CAST CHAR CHARACTER VARCHAR DECIMAL NUMERIC INT INTEGER BIGINT SMALLINT TINYINT DOUBLE FLOAT REAL
    UNSIGNED PRECISION VARYING CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURRENT_SCHEMA SESSION_USER
    USER LOCALTIME LOCALTIMESTAMP UTC_DATE UTC_TIME UTC_TIMESTAMP DAY_HOUR DAY_MINUTE DAY_SECOND DAY_MICROSECOND
    HOUR_MINUTE HOUR_SECOND HOUR_MICROSECOND MINUTE_SECOND MINUTE_MICROSECOND SECOND_MICROSECOND YEAR_MONTH""".split())
KNOWN_FUNCS = set("""COUNT SUM AVG MIN MAX GROUP_CONCAT STRING_AGG ARRAY_AGG BIT_AND BIT_OR BIT_XOR STD STDDEV
    STDDEV_POP STDDEV_SAMP VAR_POP VAR_SAMP VARIANCE ANY_VALUE BOOL_AND BOOL_OR EVERY JSON_ARRAYAGG JSON_OBJECTAGG
    JSON_AGG JSONB_AGG ROW_NUMBER RANK DENSE_RANK PERCENT_RANK CUME_DIST NTILE LAG LEAD FIRST_VALUE LAST_VALUE
    NTH_VALUE CONCAT CONCAT_WS LENGTH CHAR_LENGTH CHARACTER_LENGTH OCTET_LENGTH BIT_LENGTH LOWER UPPER LCASE UCASE
    TRIM LTRIM RTRIM BTRIM SUBSTRING SUBSTR MID LEFT RIGHT REPLACE REVERSE LPAD RPAD REPEAT INSTR LOCATE POSITION
    STRPOS FIELD FIND_IN_SET FORMAT HEX UNHEX ASCII ORD CHAR CHR SPACE INSERT SUBSTRING_INDEX SPLIT_PART QUOTE
    SOUNDEX MD5 SHA1 SHA SHA2 CRC32 TO_BASE64 FROM_BASE64 REGEXP_LIKE REGEXP_REPLACE REGEXP_SUBSTR REGEXP_INSTR
    INITCAP TRANSLATE STRING_TO_ARRAY ARRAY_TO_STRING ABS CEIL CEILING FLOOR ROUND TRUNCATE TRUNC MOD POW POWER
    SQRT EXP LN LOG LOG2 LOG10 SIGN RAND RANDOM GREATEST LEAST PI NOW CURDATE CURTIME CURRENT_DATE
    CURRENT_TIMESTAMP SYSDATE UTC_TIMESTAMP DATE TIME TIMESTAMP DATETIME YEAR MONTH DAY DAYOFMONTH DAYOFWEEK
    DAYOFYEAR WEEK WEEKDAY WEEKOFYEAR YEARWEEK QUARTER HOUR MINUTE SECOND MICROSECOND DATE_FORMAT TIME_FORMAT
    STR_TO_DATE DATE_ADD DATE_SUB ADDDATE SUBDATE ADDTIME SUBTIME DATEDIFF TIMEDIFF TIMESTAMPDIFF TIMESTAMPADD
    UNIX_TIMESTAMP FROM_UNIXTIME LAST_DAY MAKEDATE EXTRACT DATE_PART DATE_TRUNC TO_CHAR TO_DATE TO_TIMESTAMP
    TO_NUMBER AGE MONTHNAME DAYNAME CONVERT_TZ IF IFNULL NULLIF COALESCE ISNULL NVL CAST CONVERT JSON_EXTRACT
    JSON_UNQUOTE JSON_VALUE JSON_CONTAINS JSON_CONTAINS_PATH JSON_KEYS JSON_LENGTH JSON_TYPE JSON_VALID JSON_OBJECT
    JSON_ARRAY JSON_SEARCH JSON_QUOTE JSON_EXTRACT_PATH_TEXT JSONB_EXTRACT_PATH_TEXT JSON_BUILD_OBJECT
    JSONB_BUILD_OBJECT TO_JSON TO_JSONB ROW_TO_JSON UUID VERSION DATABASE SCHEMA CONNECTION_ID LAST_INSERT_ID
    INET_ATON INET_NTOA BIN OCT CONV MATCH DECIMAL NUMERIC VARCHAR FLOAT DOUBLE BINARY INTERVAL ELT FIELD
    BIT_COUNT CHARSET COLLATION""".split())
# words a parenthesis follows that are no function call
PAREN_WORDS = set(NOT_FUNC) | {"FILTER", "WITHIN", "ROW", "KEY", "PARTITION", "WINDOW", "ARRAY"}


def syntax_reasons(toks: list, engine: str) -> list[str]:
    """Why a read's syntax is outside what the scan covers: a reserved word not in READ_WORDS, a function not in
    KNOWN_FUNCS (or a quoted / qualified one: a stored function), a column list (a CTE's or a derived table's
    `c (a, b) AS` / `) d (a, b)` is no call). [] = covered."""
    reserved = RESERVED.get(engine, RESERVED["mysql"] | RESERVED["postgres"])
    out: list[str] = []
    for i, (k, t) in enumerate(toks):
        u = t.upper() if k == "word" else ""
        call = i + 1 < len(toks) and toks[i + 1][0] == "("
        if k == "ident" and call and not _column_list(toks, i):
            out.append("a stored function (%s) may read any table" % t)
        elif k == "word" and call:
            if u in PAREN_WORDS or _column_list(toks, i):
                continue
            if i >= 1 and toks[i - 1] == ("op", "."):
                out.append("a stored function (%s) may read any table" % t)
            elif u not in KNOWN_FUNCS:
                out.append("a function the personal-data scan does not know (%s)" % t)
        elif k == "word" and u in reserved and u not in READ_WORDS:
            out.append("syntax the personal-data scan does not cover (%s)" % u)
    seen: list[str] = []
    return [r for r in out if not (r in seen or seen.append(r))]


def _column_list(toks: list, i: int) -> bool:
    """Is toks[i] ( … ) a column list - of a CTE (`c (a, b) AS (`) or a derived table (`) d (a, b)`)?"""
    j, d = i + 1, 0
    while j < len(toks):
        d += (toks[j][0] == "(") - (toks[j][0] == ")")
        if d == 0:
            break
        j += 1
    after = toks[j + 1] if j + 1 < len(toks) else ("", "")
    before = toks[i - 1] if i else ("", "")
    if after[0] == "word" and after[1].upper() == "AS":
        return True   # WITH c (a, b) AS (…)
    return before[0] == ")" or before[0] == "word" and before[1].upper() == "AS" and i >= 2 and toks[i - 2][0] == ")"


def read_tables(toks: list) -> list[tuple[str, str]] | None:
    """[(schema or "", table)] a read takes rows from - None when the FROM list holds anything else than names,
    subqueries and joins of them (`FROM (t_user)`, an index hint, PARTITION, a function): the tables are then
    unknown and the caller fails closed (dbsql.tables() is a best effort for previews)."""
    cte: set = set()
    if toks and toks[0][0] == "word" and toks[0][1].upper() == "WITH":
        d = 0
        for i, (k, t) in enumerate(toks):
            d += (k == "(") - (k == ")")
            if d == 0 and k in ("word", "ident") and i + 1 < len(toks) and t.upper() not in ("WITH", "RECURSIVE", "AS") \
                    and (toks[i + 1][0] == "(" or toks[i + 1][0] == "word" and toks[i + 1][1].upper() == "AS"):
                cte.add(t.lower())
    out: list[tuple[str, str]] = []
    in_from: dict = {}   # depth -> in a FROM list
    depth = 0
    after = {"WHERE", "JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "FULL", "OUTER", "ON", "USING", "GROUP", "ORDER",
             "LIMIT", "HAVING", "UNION", "INTERSECT", "EXCEPT", "WINDOW", "OFFSET", "FETCH", "FOR", "INTO", "SET",
             "SELECT", "VALUES", "NATURAL", "STRAIGHT_JOIN", "QUALIFY", "MINUS"}
    i, n = 0, len(toks)
    while i < n:
        k, t = toks[i]
        u = t.upper() if k == "word" else ""
        if k == "(":
            depth += 1
            i += 1
            continue
        if k == ")":
            in_from.pop(depth, None)
            depth -= 1
            i += 1
            continue
        start = False
        if u in ("FROM", "UPDATE") or u == "TABLE" and (i == 0 or toks[i - 1][0] == "(" or toks[i - 1][0] == "word"
                                                       and toks[i - 1][1].upper() in ("UNION", "INTERSECT",
                                                                                      "EXCEPT", "ALL", "DISTINCT")):
            if u == "FROM" and i >= 1 and toks[i - 1][0] == "word" and toks[i - 1][1].upper() == "DISTINCT":
                i += 1
                continue   # IS [NOT] DISTINCT FROM
            in_from[depth] = u != "TABLE"
            start = True
        elif u in ("JOIN", "STRAIGHT_JOIN"):
            start = True
        elif k == "op" and t == "," and in_from.get(depth):
            start = True
        elif u in ("WHERE", "GROUP", "ORDER", "LIMIT", "HAVING", "UNION", "INTERSECT", "EXCEPT", "WINDOW", "SET",
                   "ON", "USING"):
            in_from[depth] = False
        if not start:
            i += 1
            continue
        i += 1
        if i < n and toks[i][0] == "word" and toks[i][1].upper() == "LATERAL":
            return None
        if i < n and toks[i][0] == "(":
            if i + 1 < n and toks[i + 1][0] == "word" and toks[i + 1][1].upper() in ("SELECT", "WITH", "TABLE",
                                                                                         "VALUES"):
                continue   # a subquery: scanned on its own; its alias and a comma list follow its )
            return None    # FROM (t_user), FROM (a JOIN b)
        e = _qualified_after(toks, i)
        if e < 0:
            return None    # a function, a variable, a literal
        name = [x[1] for x in toks[i:e] if x[0] != "op"]
        if e < n and toks[e][0] == "(":
            return None
        if len(name) == 1 and name[0].lower() in cte:
            pass
        else:
            out.append((name[-2] if len(name) >= 2 else "", name[-1]))
        j = e
        if j < n and toks[j][0] == "word" and toks[j][1].upper() == "AS":
            j += 1
        if j < n and toks[j][0] in ("word", "ident") and not (toks[j][0] == "word" and toks[j][1].upper() in after):
            j += 1   # an alias
        nxt = toks[j] if j < n else ("", "")
        if not (nxt[0] in (")", ";", "") or nxt == ("op", ",") or nxt[0] == "word" and nxt[1].upper() in after):
            return None    # USE INDEX, PARTITION (p0), TABLESAMPLE, …: not read here
        i = j
    seen, uniq = set(), []
    for s, t in out:
        if (s.lower(), t.lower()) not in seen:
            seen.add((s.lower(), t.lower()))
            uniq.append((s, t))
    return uniq


def guard(sql: str, engine: str, names: set, opaque: bool, masked_views: list | None = None,
          blind: bool = False) -> list[str]:
    """Why this query needs the human's approval on a masked connection ([] = it may run): a personal
    column (names, from personal()) in a WHERE / ON / HAVING / GROUP BY / ORDER BY / OVER / FROM function,
    in a subquery of a condition, in a UNION / INTERSECT / EXCEPT, a positional ORDER / GROUP BY over
    a personal or `*` select list, any filter over a fully anonymized view, an unreadable query."""
    try:
        toks = _statement(sql, engine)
    except dbsql.SqlError:
        return ["the query cannot be scanned for personal data"]
    reasons: list[str] = []
    if toks and (toks[0][0] == "(" or toks[0][0] == "word" and toks[0][1].upper() in ("SELECT", "WITH", "TABLE",
                                                                                      "VALUES")):
        reasons += syntax_reasons(toks, engine)
    flat = not nested(toks)
    items_at = select_items_at(toks)
    aliases = frozenset(a for a in (_alias(it) for _, it in items_at) if a)
    mentioned = blind or any(k in ("word", "ident") and t.lower() in names for k, t in toks)
    if any(k == "var" and not t.startswith("@@") for k, t in toks):
        reasons.append("a user variable (@x) carries a value on to the next statement, past the masking")
    if opaque and mentioned:
        reasons.append("a column list renames a subquery over personal data")
    # frames: one per parenthesis; clause = the clause the frame is in; cond = inside a condition somewhere;
    # side = inside CASE or a SIDE_FUNCS call (a select-list value that answers through NULL, an error or time)
    frames = [{"clause": None, "cond": False, "sel_personal": False, "star": False, "case": 0, "side": False}]
    filters = False
    setop = False
    for i, (k, t) in enumerate(toks):
        f = frames[-1]
        if k == "(":
            prev = toks[i - 1] if i else ("", "")
            pu = prev[1].upper() if prev[0] == "word" else ""
            sub = i + 1 < len(toks) and toks[i + 1][0] == "word" and toks[i + 1][1].upper() in ("SELECT", "WITH")
            qexpr = sub and (i == 0 or pu in ("UNION", "INTERSECT", "EXCEPT", "MINUS", "ALL", "DISTINCT") or
                             prev[0] == "(" and f.get("qexpr"))
            func = prev[0] == "ident" or prev[0] == "word" and pu not in NOT_FUNC
            infunc = f.get("infunc") or f["clause"] == "from" and func   # JSON_TABLE(… (SELECT …) …)
            frames.append({"clause": "using" if pu == "USING" and not sub else f["clause"], "qexpr": qexpr,
                           "cond": f["cond"] or f["clause"] in ("cond", "group", "order") or infunc,
                           "sel_personal": False, "star": False, "case": 0, "infunc": infunc,
                           "distinct": f.get("distinct") and not sub,
                           "side": f["side"] or f["case"] > 0 or pu in SIDE_FUNCS, "func": func})
            continue
        if k == ")":
            if len(frames) > 1:
                done = frames.pop()
                if done.get("qexpr"):   # (SELECT id, nick FROM t) ORDER BY 2: the outer order is the inner list's
                    frames[-1]["sel_personal"] |= done["sel_personal"]
                    frames[-1]["star"] |= done["star"]
            continue
        if k == "word":
            u = t.upper()
            if u == "TABLE" and (i == 0 or toks[i - 1][0] == "(" or toks[i - 1][0] == "word" and
                                 toks[i - 1][1].upper() in ("UNION", "INTERSECT", "EXCEPT", "ALL", "DISTINCT")):
                f["star"] = True   # TABLE t = SELECT * FROM t (MySQL 8)
                f["clause"] = "from"
                continue
            if u == "CASE":
                f["case"] += 1
            elif u == "END" and f["case"]:
                f["case"] -= 1
            elif u == "DISTINCT" and i >= 1 and toks[i - 1][0] == "word" and toks[i - 1][1].upper() == "SELECT":
                f["distinct"] = True   # which rows collapse answers about the values
            elif u == "FROM" and i >= 1 and toks[i - 1][0] == "word" and toks[i - 1][1].upper() == "DISTINCT":
                continue   # x IS [NOT] DISTINCT FROM y: a comparison, no FROM clause
            if u in CLAUSE and f["clause"] != "using":
                c = CLAUSE[u]
                if c == "setop":
                    setop = True
                if c in ("cond", "group", "order"):
                    filters = True
                f["clause"] = c
                continue
        clause = f["clause"]
        if clause == "select" and k == "op" and t == "*":
            f["star"] = True
        if clause in ("group", "order") and k == "num" and (f["sel_personal"] or f["star"]) and \
                (toks[i - 1][0] == "word" and toks[i - 1][1].upper() in ("BY",) or toks[i - 1] == ("op", ",")):
            reasons.append("ORDER / GROUP BY a position of a personal or * select list")
            continue
        if k not in ("word", "ident") or t.lower() not in names:
            continue
        if i + 1 < len(toks) and (toks[i + 1] == ("op", ".") or toks[i + 1][0] == "("):
            continue   # a qualifier (t.col) or a function name
        start = _qualified_end(toks, i)
        if clause == "select":
            if f["cond"]:
                reasons.append("%s in a subquery of a condition" % t)
            else:
                f["sel_personal"] = True
                if f.get("distinct"):
                    reasons.append("%s under SELECT DISTINCT (how many rows collapse answers)" % t)
                if f["side"] or f["case"] or (i + 1 < len(toks) and _compares(toks[i + 1])) or \
                        (start > 0 and _compares(toks[start - 1])):
                    reasons.append("%s in a condition of the select list (CASE / IF / a comparison: its NULL, an "
                                   "error or the time answers)" % t)
            continue
        if clause == "from":
            if f.get("func"):
                reasons.append("%s in a FROM function" % t)
            continue   # a table name or alias
        if clause == "using":
            if not flat:
                reasons.append("%s in a USING join of a nested query" % t)
            continue   # USING (col) of two tables: a.col = b.col
        if clause in ("with", "limit", None):
            continue
        if clause == "cond" and _safe_use(toks, start, i + 1, flat, aliases):
            continue
        reasons.append("%s in %s" % (t, {"cond": "a condition (WHERE / ON / HAVING)", "group": "GROUP BY",
                                          "order": "ORDER BY / PARTITION BY"}.get(clause, clause)))
    if blind and filters:
        reasons.append("a * of a subquery / UNION hides which of its columns hold personal data - and the query "
                       "filters or sorts")
    if setop and mentioned:
        reasons.append("UNION / INTERSECT / EXCEPT over personal data (duplicates removed reveal values)")
    if masked_views and filters:
        reasons.append("filters over the anonymized table(s) / view(s) %s" % ", ".join(masked_views))
    for _, it in items_at:
        hit = sorted(_names(it) & names)
        if hit and not _bare(it) and _computed_unsafe(it):
            reasons.append("%s computed in the select list by a function or an operator that may answer through an "
                           "error, a NULL or the time" % ", ".join(hit))
    if filters and any(d > 0 and _alias(it) is None and not _bare(it) and _names(it) & names for d, it in items_at):
        reasons.append("a subquery's unnamed expression over personal data (its implicit name) may be filtered")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# words before TABLE that make it DDL (CREATE TABLE t), not the TABLE t source of a write (INSERT INTO x TABLE t)
DDL_TABLE = {"CREATE", "TEMPORARY", "TEMP", "ALTER", "DROP", "TRUNCATE", "RENAME", "LOCK", "OPTIMIZE", "ANALYZE",
             "REPAIR", "CHECK", "CHECKSUM", "FLUSH", "SHOW", "GLOBAL", "LOCAL", "UNLOGGED", "FOREIGN", "COMMENT", "ON"}


def _set_targets(toks: list, start: int, ends: tuple) -> set:
    """Indexes of the `col` of `SET col = …, col = …` from start (the token after SET) to a top-level end word."""
    out: set = set()
    d, at_target = 0, True
    for i in range(start, len(toks)):
        k, t = toks[i]
        if k == "(":
            d += 1
        elif k == ")":
            d -= 1
            if d < 0:
                break
        elif d == 0 and k == "word" and t.upper() in ends:
            break
        elif d == 0 and k == "op" and t == ",":
            at_target = True
            continue
        elif d == 0 and k == "op" and t == "=":
            at_target = False
        if at_target:
            out.add(i)
    return out


def _renames(toks: list, is_personal, rule_tables) -> bool:
    """Does RENAME TABLE / ALTER … RENAME / CHANGE / EXCHANGE PARTITION move personal data out of its rules (they
    name the table and the column)? A rule table renamed, a personal column renamed, a partition swapped."""
    words = [(i, t.upper()) for i, (k, t) in enumerate(toks) if k == "word"]
    first = words[0][1] if words else ""
    if rule_tables is None:
        return first == "RENAME" or first == "ALTER" and any(w in ("RENAME", "CHANGE", "EXCHANGE") for _, w in words)
    if first == "RENAME":
        return any(k in ("word", "ident") and t.lower() in rule_tables for k, t in toks)
    if first != "ALTER":
        return False
    j = 2
    while j < len(toks) and toks[j][0] == "word" and toks[j][1].upper() in ("IF", "EXISTS", "ONLY"):
        j += 1
    end = _qualified_after(toks, j) if len(words) > 1 and words[1][1] == "TABLE" else -1
    table = toks[end - 1][1].lower() if end > 0 else None
    for i, (k, t) in enumerate(toks):
        w = t.upper() if k == "word" else ""
        nxt = toks[i + 1] if i + 1 < len(toks) else ("", "")
        if w == "EXCHANGE":
            return True
        if w == "RENAME":
            nw = nxt[1].upper() if nxt[0] == "word" else ""
            if nw in ("TO", "AS") or nxt[0] == "":
                if table is None or table in rule_tables:
                    return True
            elif nw in ("INDEX", "KEY", "CONSTRAINT"):
                continue
            else:
                j = i + 2 if nw == "COLUMN" else i + 1
                if j < len(toks) and toks[j][0] in ("word", "ident") and is_personal(toks[j][1]):
                    return True
        if w == "CHANGE":
            j = i + 2 if nxt[0] == "word" and nxt[1].upper() == "COLUMN" else i + 1
            if j < len(toks) and toks[j][0] in ("word", "ident") and is_personal(toks[j][1]):
                return True
    return False


def write_copies(sql: str, engine: str, is_personal, rule_tables: set | None = None) -> list[str]:
    """Why a write puts personal data where the agent may read it unmasked: `UPDATE t SET note = (email LIKE
    'a%')`, `INSERT … SELECT email`, `CREATE TABLE … AS SELECT name`, a generated column or view over one.
    The target of `SET col =` and an INSERT's column list are no read; conditions are guard()'s.
    rule_tables: lower-case tables the anonymization rules name (None: unknown - every rename counts)."""
    try:
        toks = _statement(sql, engine)
    except dbsql.SqlError:
        return ["the write cannot be scanned for personal data"]
    words = [t.upper() for k, t in toks if k == "word"]
    first = words[0] if words else ""
    if first in ("PREPARE", "EXECUTE", "DEALLOCATE"):
        return ["a statement hidden in a string or a prepared statement cannot be scanned for personal data"]
    skip: set = set()
    if first == "CREATE" and "INDEX" in words[:4] and "TABLE" not in words[:4]:
        return []   # CREATE [UNIQUE] INDEX i ON t (email): no data leaves the table
    if first == "CREATE" and "TABLE" in words[:4] and not ({"SELECT", "AS"} & set(words)) \
            and words.count("TABLE") == 1 and "LIKE" not in words:
        d, at_name = 0, False   # CREATE TABLE x (email varchar(9), …): the column definitions are no read
        for i, (k, t) in enumerate(toks):
            if k == "(":
                d += 1
                at_name = d == 1
                continue
            if k == ")":
                d -= 1
            elif d == 1 and k == "op" and t == ",":
                at_name = True
                continue
            if at_name and d == 1 and k in ("word", "ident"):
                skip.add(i)
            at_name = False
    if first == "UPDATE":
        d, in_set, at_target = 0, False, False
        for i, (k, t) in enumerate(toks):
            if k == "(":
                d += 1
            elif k == ")":
                d -= 1
            elif d == 0 and k == "word" and t.upper() == "SET":
                in_set, at_target = True, True
                continue
            elif d == 0 and k == "word" and t.upper() in ("WHERE", "ORDER", "LIMIT", "RETURNING", "FROM"):
                in_set = False
            if not in_set:
                if k in ("word", "ident") and is_personal(t):
                    skip.add(i)   # outside SET: a table name, or guard()'s conditions
                continue
            if d == 0 and k == "op" and t == ",":
                at_target = True
            elif d == 0 and k == "op" and t == "=":
                at_target = False
            elif at_target:
                skip.add(i)
    elif first in ("INSERT", "REPLACE"):
        j = 1   # INSERT [LOW_PRIORITY | DELAYED | HIGH_PRIORITY] [IGNORE] [INTO] t: the target is no read
        while j < len(toks) and toks[j][0] == "word" and toks[j][1].upper() in (
                "LOW_PRIORITY", "DELAYED", "HIGH_PRIORITY", "IGNORE", "INTO"):
            j += 1
        end = _qualified_after(toks, j)
        skip |= set(range(j, end if end > 0 else j + 1))
        d, opened, cols_done = 0, False, False
        for i, (k, t) in enumerate(toks):
            if i < j:
                continue
            if k == "(":
                if d == 0 and not opened and not cols_done:
                    opened = True
                d += 1
            elif k == ")":
                d -= 1
                if d == 0 and opened:
                    opened, cols_done = False, True
            elif k == "word" and t.upper() in ("VALUES", "SELECT", "SET", "VALUE"):
                cols_done = True
            if opened and not cols_done:
                skip.add(i)   # INSERT INTO t (email, …)
        for i, (k, t) in enumerate(toks):
            u = t.upper() if k == "word" else ""
            if i >= j and u == "SET" and (i == 0 or toks[i - 1][0] != "word" or toks[i - 1][1].upper() != "CHARACTER"):
                skip |= _set_targets(toks, i + 1, ("ON", "RETURNING"))   # INSERT INTO t SET email = …
            elif u == "UPDATE" and i >= 2 and toks[i - 1][1].upper() == "KEY":
                skip |= _set_targets(toks, i + 1, ("RETURNING",))   # ON DUPLICATE KEY UPDATE email = …
            elif u == "VALUES" and i + 3 < len(toks) and toks[i + 1][0] == "(" and toks[i + 3][0] == ")" \
                    and any(x[0] == "word" and x[1].upper() == "DUPLICATE" for x in toks[:i]):
                skip.add(i + 2)   # VALUES(email) in ON DUPLICATE KEY: the inserted value, no read
    elif first == "DELETE":
        return []
    out = []
    if _renames(toks, is_personal, rule_tables):
        out.append("a table or column renamed out of its anonymization rules (they name the table and the column)")
    if first in ("INSERT", "REPLACE", "CREATE", "UPDATE") and (
            any(_star_item(it) for _, it in select_items_at(toks)) or _bare_star(toks) or _table_source(toks)):
        out.append("whole rows (*) copied into the written data (the agent could read them back unmasked)")
    found = sorted({t for i, (k, t) in enumerate(toks) if i not in skip and k in ("word", "ident") and is_personal(t)
                    and not (i + 1 < len(toks) and toks[i + 1] == ("op", "."))})
    if found:
        out.append("%s copied or computed into the written data (the agent could read it back unmasked)"
                   % ", ".join(found))
    return out


def _bare_star(toks: list) -> bool:
    """A * that stands for columns anywhere in a write (`JSON_OBJECT(t.*)`, a nested `SELECT *`) - not COUNT(*)
    nor a multiplication."""
    for i, (k, t) in enumerate(toks):
        if k != "op" or t != "*" or i == 0:
            continue
        pk, pt = toks[i - 1]
        if pk == "(" and i >= 2 and toks[i - 2][0] == "word" and toks[i - 2][1].upper() == "COUNT":
            continue
        if pk == "(" or (pk == "op" and pt in (",", ".")) or (pk == "word" and pt.upper() in ("SELECT", "DISTINCT", "ALL")):
            return True
    return False


def _table_source(toks: list) -> bool:
    """`TABLE t` as the rows of a write (INSERT INTO x TABLE t, CREATE TABLE x AS TABLE t) - not CREATE TABLE."""
    for i, (k, t) in enumerate(toks):
        if k == "word" and t.upper() == "TABLE" and i > 0 and not (
                toks[i - 1][0] == "word" and toks[i - 1][1].upper() in DDL_TABLE):
            if toks[i - 1][0] == "word" and toks[i - 1][1].upper() in ("EXCHANGE", "WITH"):
                continue   # EXCHANGE PARTITION … WITH TABLE: _renames()
            return True
    return False


def _star_item(item: list) -> bool:
    it = [x for x in item if not (x[0] == "word" and x[1].upper() in ("DISTINCT", "ALL"))]
    return bool(it) and it[-1] == ("op", "*") and _bare(it)
