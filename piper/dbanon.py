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


def _like(value, pattern: str) -> bool:
    """MySQL LIKE with the default case-insensitive collation (`\\` escapes)."""
    rx, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            rx.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        rx.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(rx), str(value), re.S | re.I) is not None


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
    words = [t.upper() for k, t in toks if k == "word"]
    return words.count("SELECT") > 1 or "WITH" in words[:1] or "VALUES" in words or "LATERAL" in words


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


def guard(sql: str, engine: str, names: set, opaque: bool, masked_views: list | None = None) -> list[str]:
    """Why this query needs the human's approval on a masked connection ([] = it may run): a personal
    column (names, from personal()) in a WHERE / ON / HAVING / GROUP BY / ORDER BY / OVER / FROM function,
    in a subquery of a condition, in a UNION / INTERSECT / EXCEPT, a positional ORDER / GROUP BY over
    a personal or `*` select list, any filter over a fully anonymized view, an unreadable query."""
    try:
        toks = _statement(sql, engine)
    except dbsql.SqlError:
        return ["the query cannot be scanned for personal data"]
    reasons: list[str] = []
    flat = not nested(toks)
    items_at = select_items_at(toks)
    aliases = frozenset(a for a in (_alias(it) for _, it in items_at) if a)
    mentioned = any(k in ("word", "ident") and t.lower() in names for k, t in toks)
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
            frames.append({"clause": "using" if pu == "USING" and not sub else f["clause"],
                           "cond": f["cond"] or f["clause"] in ("cond", "group", "order"),
                           "sel_personal": False, "star": False, "case": 0,
                           "side": f["side"] or f["case"] > 0 or pu in SIDE_FUNCS,
                           "func": prev[0] == "ident" or prev[0] == "word" and pu not in NOT_FUNC})
            continue
        if k == ")":
            if len(frames) > 1:
                frames.pop()
            continue
        if k == "word":
            u = t.upper()
            if u == "CASE":
                f["case"] += 1
            elif u == "END" and f["case"]:
                f["case"] -= 1
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
                if f["side"] or f["case"] or (i + 1 < len(toks) and _compares(toks[i + 1])) or \
                        (start > 0 and _compares(toks[start - 1])):
                    reasons.append("%s in a condition of the select list (CASE / IF / a comparison: its NULL, an "
                                   "error or the time answers)" % t)
            continue
        if clause == "from":
            if f.get("func"):
                reasons.append("%s in a FROM function" % t)
            continue   # a table name or alias
        if clause in ("with", "limit", "using", None):
            continue
        if clause == "cond" and _safe_use(toks, start, i + 1, flat, aliases):
            continue
        reasons.append("%s in %s" % (t, {"cond": "a condition (WHERE / ON / HAVING)", "group": "GROUP BY",
                                          "order": "ORDER BY / PARTITION BY"}.get(clause, clause)))
    if setop and mentioned:
        reasons.append("UNION / INTERSECT / EXCEPT over personal data (duplicates removed reveal values)")
    if masked_views and filters:
        reasons.append("filters over the anonymized table(s) / view(s) %s" % ", ".join(masked_views))
    if filters and any(d > 0 and _alias(it) is None and not _bare(it) and _names(it) & names for d, it in items_at):
        reasons.append("a subquery's unnamed expression over personal data (its implicit name) may be filtered")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def write_copies(sql: str, engine: str, is_personal) -> list[str]:
    """Why a write puts personal data where the agent may read it unmasked: `UPDATE t SET note = (email LIKE
    'a%')`, `INSERT … SELECT email`, `CREATE TABLE … AS SELECT name`, a generated column or view over one.
    The target of `SET col =` and an INSERT's column list are no read; conditions are guard()'s."""
    try:
        toks = _statement(sql, engine)
    except dbsql.SqlError:
        return ["the write cannot be scanned for personal data"]
    words = [t.upper() for k, t in toks if k == "word"]
    first = words[0] if words else ""
    skip: set = set()
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
        d, opened, cols_done = 0, False, False
        for i, (k, t) in enumerate(toks):
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
            elif i <= 3 and k in ("word", "ident"):
                skip.add(i)   # INSERT INTO t / REPLACE t
    elif first == "DELETE":
        return []
    found = sorted({t for i, (k, t) in enumerate(toks) if i not in skip and k in ("word", "ident") and is_personal(t)
                    and not (i + 1 < len(toks) and toks[i + 1] == ("op", "."))})
    if not found:
        return []
    return ["%s copied or computed into the written data (the agent could read it back unmasked)" % ", ".join(found)]
