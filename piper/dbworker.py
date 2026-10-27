#!/usr/bin/env python3
"""The DB client's driver process, one per connection.

piperd (dbtools.py) starts it with the drivers' venv python (~/.piper/dbvenv) and talks JSON
lines over stdin/stdout - the password arrives in the `open` request, never in argv or the
environment. Every request runs on its own thread, so `cancel` reaches a running statement.

Two server connections of the same user (the user decided: one account per connection, the
read/write boundary is Piper's):
  read   session read-only from the first packet (MySQL init_command, PG options), every statement
         in START TRANSACTION READ ONLY ... ROLLBACK, @@transaction_read_only checked after it
         (detection - dbsql.read_check's allowlist is the prevention); unbuffered cursor, at most
         max_rows+1 rows fetched, the rest of a big result killed, not drained;
  write  autocommit off; `execute` runs a list of statements in ONE transaction and either rolls
         it back (preview) or commits it; a rowcount that drifted from the approved preview rolls
         back too.
A third short-lived connection sends KILL QUERY (MySQL lets a user kill its own threads without
PROCESS); PostgreSQL cancels through the driver.

Requests: {"id", "op", ...}; answers: {"id", "ok": true, "result"} or {"id", "ok": false, "error", "code"}.
  open {spec}            spec: engine host port user password database program
  query {sql, params?, schema?, max_rows, max_cell, timeout_s, console?}
  execute {statements: [{sql, params?}], schema?, preview, timeout_s, expect?, tolerance?}
  explain {sql, schema?, timeout_s}
  meta {what, schema?, name?}   what: schemas objects columns ddl tables fks indexes triggers
  cancel {}   ping {}   close {}
"""
from __future__ import annotations

import base64
import datetime
import decimal
import json
import os
import re
import select
import sys
import threading
import time
import uuid

COUNT_MS = 500   # the dry run's optional "of M rows" count
MAX_CELL_FULL = 1 << 20   # a cell kept for db_cell / the GUI; longer is cut here already


class WorkerError(Exception):
    def __init__(self, msg: str, code: str = "error"):
        super().__init__(msg)
        self.code = code


def jsonable(v, limit: int):
    """A driver value -> (json value, cut?)."""
    if v is None or isinstance(v, (bool, int, float)):
        return v, False
    if isinstance(v, decimal.Decimal):
        return str(v), False
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat(sep=" ") if isinstance(v, datetime.datetime) else v.isoformat(), False
    if isinstance(v, datetime.timedelta):
        s = int(v.total_seconds())
        return "%s%02d:%02d:%02d" % ("-" if s < 0 else "", abs(s) // 3600, abs(s) % 3600 // 60, abs(s) % 60), False
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        try:
            t = b.decode("utf-8")
            if not any(ord(c) < 9 for c in t[:1000]):
                return (t[:limit], len(t) > limit)
        except UnicodeDecodeError:
            pass
        cut = len(b) > limit // 2
        return "0x" + b[:limit // 2].hex(), cut
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False, default=str)
    s = str(v)
    return (s[:limit], len(s) > limit)


def bit_value(v):
    """MySQL BIT(n) comes as bytes (b'\\x01'): the number it holds, not '0x01' (a boolean column is BIT(1))."""
    return int.from_bytes(bytes(v), "big") if isinstance(v, (bytes, bytearray, memoryview)) else v


# ------------------------------------------------------------------ drivers
class Driver:
    """One engine: how to connect the read / write / control connections and run things."""
    engine = ""
    ph = "%s"   # the driver's parameter placeholder

    def __init__(self, spec: dict, mod=None):
        self.spec = spec
        self.mod = mod

    # each returns a DB-API connection
    def connect(self, mode: str):
        raise NotImplementedError

    def begin_read(self, conn, schema: str | None) -> None:
        raise NotImplementedError

    def ro_ok(self, conn) -> bool:
        raise NotImplementedError

    def identity(self, conn) -> dict:
        raise NotImplementedError

    def kill(self, ctrl, conn) -> None:
        raise NotImplementedError

    def lost(self, exc) -> bool:
        """Did this error leave the connection unusable (killed by the server, network gone, protocol
        out of step)? Then the worker drops it - the next call connects again instead of failing forever."""
        return False


class MySQL(Driver):
    engine = "mysql"

    def __init__(self, spec, mod=None):
        if mod is None:
            import pymysql as mod   # noqa: N813 - the venv's driver
        super().__init__(spec, mod)

    def connect(self, mode: str):
        s, m = self.spec, self.mod
        kw = dict(host=s.get("host") or "127.0.0.1", port=int(s.get("port") or 3306), user=s.get("user") or "",
                  password=s.get("password") or "", database=s.get("database") or None, charset="utf8mb4",
                  connect_timeout=10, autocommit=mode != "write", local_infile=False,
                  program_name="piper-%s" % mode)
        if mode == "read":
            kw["init_command"] = "SET SESSION transaction_read_only=ON"
        clients = getattr(getattr(m, "constants", None), "CLIENT", None)
        if mode == "write" and clients is not None:   # rowcount = rows MATCHED (an UPDATE to the same value counts)
            kw["client_flag"] = clients.FOUND_ROWS
        c = m.connect(**kw)
        flags = getattr(c, "client_flag", 0)
        multi = getattr(getattr(m, "constants", None), "CLIENT", None)
        if multi is not None and flags & multi.MULTI_STATEMENTS:   # pinned: one statement per execute
            c.close()
            raise WorkerError("the driver enabled MULTI_STATEMENTS - refusing to use it", "driver")
        return c

    def cursor(self, conn, unbuffered: bool):
        if unbuffered:
            return conn.cursor(self.mod.cursors.SSCursor)
        return conn.cursor()

    def begin_read(self, conn, schema):
        cur = conn.cursor()
        # every request names its schema or gets the connection's default back: a USE of the
        # previous caller (the GUI, another session) never leaks into this one
        target = schema or self.spec.get("database")
        if target:
            cur.execute("USE " + quote(target, "mysql"))
        cur.execute("START TRANSACTION READ ONLY")
        cur.close()

    def end(self, conn):
        try:
            conn.rollback()
        except Exception:   # noqa: BLE001 - a killed / broken connection: dropped by the caller
            pass

    def ro_ok(self, conn) -> bool:
        cur = conn.cursor()
        cur.execute("SELECT @@session.transaction_read_only")
        v = cur.fetchone()
        cur.close()
        return bool(v and int(v[0]) == 1)

    def identity(self, conn) -> dict:
        cur = conn.cursor()
        cur.execute("SELECT @@server_uuid, @@hostname, VERSION(), DATABASE()")
        u, h, v, d = cur.fetchone()
        cur.close()
        return {"server_id": str(u), "hostname": str(h), "version": str(v), "database": d}

    def thread_id(self, conn):
        return conn.thread_id()

    def kill(self, ctrl, tid) -> None:
        cur = ctrl.cursor()
        try:
            cur.execute("KILL QUERY %d" % int(tid))
        finally:
            cur.close()

    # 2006 server has gone away, 2013 lost during query, 1927 connection killed, 4031 idle disconnect
    LOST = (2006, 2013, 1927, 4031)

    def lost(self, exc) -> bool:
        a = getattr(exc, "args", ())
        return isinstance(exc, (ConnectionError, BrokenPipeError)) or bool(a and a[0] in self.LOST)

    def begin_write(self, conn, schema):
        cur = conn.cursor()
        target = schema or self.spec.get("database")
        if target:
            cur.execute("USE " + quote(target, "mysql"))
        cur.execute("START TRANSACTION")
        cur.close()

    def warnings(self, cur) -> list:
        try:
            cur.execute("SHOW WARNINGS")
            return [{"level": str(r[0]), "code": int(r[1]), "message": str(r[2])[:300]} for r in cur.fetchall()[:20]]
        except Exception:   # noqa: BLE001
            return []

    def type_name(self, code) -> str:
        try:
            ft = self.mod.constants.FIELD_TYPE
            for k in dir(ft):
                if not k.startswith("_") and getattr(ft, k) == code:
                    return k.lower()
        except AttributeError:
            pass
        return str(code)

    # metadata (information_schema; a schema = a database)
    def meta(self, conn, what: str, schema: str | None, name: str | None):
        q = {
            "schemas": ("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY 1", ()),
            "objects": ("SELECT TABLE_NAME, LOWER(TABLE_TYPE), ENGINE, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) ORDER BY 1", (schema,)),
            "routines": ("SELECT ROUTINE_NAME, LOWER(ROUTINE_TYPE) FROM information_schema.ROUTINES "
                         "WHERE ROUTINE_SCHEMA = COALESCE(%s, DATABASE()) ORDER BY 1", (schema,)),
            "columns": ("SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
                        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s "
                        "ORDER BY ORDINAL_POSITION", (schema, name)),
            "fks": ("SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, "
                    "k.REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE k WHERE k.TABLE_SCHEMA = "
                    "COALESCE(%s, DATABASE()) AND k.TABLE_NAME = %s AND k.REFERENCED_TABLE_NAME IS NOT NULL "
                    "ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION", (schema, name)),
            "refs": ("SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME, k.REFERENCED_COLUMN_NAME, k.CONSTRAINT_NAME "
                     "FROM information_schema.KEY_COLUMN_USAGE k WHERE k.REFERENCED_TABLE_SCHEMA = COALESCE(%s, DATABASE()) "
                     "AND k.REFERENCED_TABLE_NAME = %s ORDER BY 1, 2", (schema, name)),
            "indexes": ("SELECT INDEX_NAME, NON_UNIQUE, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) FROM "
                        "information_schema.STATISTICS WHERE TABLE_SCHEMA = COALESCE(%s, DATABASE()) AND TABLE_NAME = %s "
                        "GROUP BY INDEX_NAME, NON_UNIQUE ORDER BY INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME", (schema, name)),
            "engines": ("SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = "
                        "COALESCE(%s, DATABASE())", (schema,)),
            "triggers": ("SELECT EVENT_OBJECT_TABLE, TRIGGER_NAME FROM information_schema.TRIGGERS WHERE "
                         "EVENT_OBJECT_SCHEMA = COALESCE(%s, DATABASE())", (schema,)),
            "database": ("SELECT DATABASE()", ()),
            "colindex": ("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = "
                         "COALESCE(%s, DATABASE()) ORDER BY 1, ORDINAL_POSITION", (schema,)),
        }
        if what == "ddl":
            cur = conn.cursor()
            sch = (quote(schema, "mysql") + ".") if schema else ""
            cur.execute("SHOW CREATE TABLE %s%s" % (sch, quote(name or "", "mysql")))
            r = cur.fetchone()
            cur.close()
            return [[r[1] if r else ""]]
        sql, args = q[what]
        cur = conn.cursor()
        cur.execute(sql, args)
        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        return rows


class Postgres(Driver):
    engine = "postgres"

    def __init__(self, spec, mod=None):
        if mod is None:
            import psycopg as mod   # noqa: N813
        super().__init__(spec, mod)

    # SQLSTATE 57P01 admin_shutdown (pg_terminate_backend), 57P02/57P03, 08xxx connection exceptions
    LOST = ("terminating connection", "server closed the connection", "connection is closed",
            "consuming input failed", "the connection is lost")

    def lost(self, exc) -> bool:
        state = str(getattr(exc, "sqlstate", "") or "")
        return (isinstance(exc, (ConnectionError, BrokenPipeError)) or state.startswith(("57P0", "08"))
                or any(k in str(exc) for k in self.LOST))

    def connect(self, mode: str):
        s = self.spec
        kw = dict(host=s.get("host") or "127.0.0.1", port=int(s.get("port") or 5432), user=s.get("user") or "",
                  password=s.get("password") or "", dbname=s.get("database") or "postgres",
                  application_name="piper-%s" % mode, connect_timeout=10, autocommit=mode != "write")
        if mode == "read":
            kw["options"] = "-c default_transaction_read_only=on"
        return self.mod.connect(**kw)

    def cursor(self, conn, unbuffered: bool):
        if unbuffered:
            return conn.cursor(name="piper_%s" % uuid.uuid4().hex[:12])
        return conn.cursor()

    def begin_read(self, conn, schema):
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("BEGIN READ ONLY")
        if schema:
            cur.execute("SELECT set_config('search_path', %s, true)", (schema,), prepare=True)
        cur.close()

    def end(self, conn):
        try:
            conn.execute("ROLLBACK")
        except Exception:   # noqa: BLE001
            pass

    def ro_ok(self, conn) -> bool:
        cur = conn.cursor()
        cur.execute("SHOW default_transaction_read_only")
        v = cur.fetchone()
        cur.close()
        return bool(v and str(v[0]).lower() == "on")

    def identity(self, conn) -> dict:
        cur = conn.cursor()
        cur.execute("SELECT (SELECT system_identifier::text FROM pg_control_system()), inet_server_addr()::text, "
                    "version(), current_database()")
        u, h, v, d = cur.fetchone()
        cur.close()
        return {"server_id": str(u), "hostname": str(h or ""), "version": str(v).split(",")[0], "database": d}

    def thread_id(self, conn):
        return conn

    def kill(self, ctrl, conn) -> None:
        conn.cancel()

    def begin_write(self, conn, schema):
        conn.autocommit = False
        if schema:
            conn.execute("SELECT set_config('search_path', %s, true)", (schema,))

    def warnings(self, cur) -> list:
        return []

    def type_name(self, code) -> str:
        try:
            t = self.mod.adapters.types.get(code)
            return t.name if t else str(code)
        except AttributeError:
            return str(code)

    def meta(self, conn, what, schema, name):
        sch = schema or "public"
        q = {
            "schemas": ("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%%' AND nspname <> "
                        "'information_schema' ORDER BY 1", ()),
            "objects": ("SELECT c.relname, CASE c.relkind WHEN 'r' THEN 'base table' WHEN 'v' THEN 'view' WHEN 'm' THEN "
                        "'materialized view' WHEN 'p' THEN 'base table' ELSE c.relkind::text END, NULL, c.reltuples::bigint, "
                        "obj_description(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE "
                        "n.nspname = %s AND c.relkind IN ('r','v','m','p') ORDER BY 1", (sch,)),
            "routines": ("SELECT p.proname, CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END FROM pg_proc p "
                         "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s ORDER BY 1", (sch,)),
            "columns": ("SELECT a.attname, format_type(a.atttypid, a.atttypmod), CASE WHEN a.attnotnull THEN 'NO' ELSE "
                        "'YES' END, CASE WHEN EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary "
                        "AND a.attnum = ANY(i.indkey)) THEN 'PRI' ELSE '' END, pg_get_expr(d.adbin, d.adrelid), '', "
                        "col_description(c.oid, a.attnum) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN "
                        "pg_namespace n ON n.oid = c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND "
                        "d.adnum = a.attnum WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT "
                        "a.attisdropped ORDER BY a.attnum", (sch, name)),
            "fks": ("SELECT con.conname, a.attname, fn.nspname, fc.relname, fa.attname FROM pg_constraint con "
                    "JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_class fc ON fc.oid = con.confrelid JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
                    "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(a, f) "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.a "
                    "JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = k.f "
                    "WHERE con.contype = 'f' AND n.nspname = %s AND c.relname = %s ORDER BY 1", (sch, name)),
            "refs": ("SELECT n.nspname, c.relname, a.attname, fa.attname, con.conname FROM pg_constraint con "
                     "JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                     "JOIN pg_class fc ON fc.oid = con.confrelid JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
                     "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(a, f) "
                     "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.a "
                     "JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = k.f "
                     "WHERE con.contype = 'f' AND fn.nspname = %s AND fc.relname = %s ORDER BY 1, 2", (sch, name)),
            "indexes": ("SELECT i.relname, NOT x.indisunique, pg_get_indexdef(x.indexrelid) FROM pg_index x JOIN pg_class i "
                        "ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = "
                        "c.relnamespace WHERE n.nspname = %s AND c.relname = %s ORDER BY x.indisprimary DESC, 1", (sch, name)),
            "engines": ("SELECT n.nspname, c.relname, 'transactional' FROM pg_class c JOIN pg_namespace n ON n.oid = "
                        "c.relnamespace WHERE n.nspname = %s AND c.relkind IN ('r','p')", (sch,)),
            "preview_unsafe": ("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                               "WHERE n.nspname=%s AND (c.relrowsecurity OR c.relkind <> 'r' OR "
                               "EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) OR "
                               "EXISTS (SELECT 1 FROM pg_rewrite r WHERE r.ev_class=c.oid AND r.rulename<>'_RETURN') OR "
                               "EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhparent=c.oid OR h.inhrelid=c.oid) OR "
                               "EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL)) OR "
                               "EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='c') OR "
                               "EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid=c.oid AND a.attgenerated<>''))", (sch,)),
            "triggers": ("SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN "
                         "pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND NOT t.tgisinternal", (sch,)),
            "database": ("SELECT current_database()", ()),
            "colindex": ("SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a JOIN pg_class c "
                         "ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND "
                         "c.relkind IN ('r','v','m','p') AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, a.attnum", (sch,)),
        }
        if what == "ddl":
            cols = self.meta(conn, "columns", schema, name)
            body = ",\n".join("  %s %s%s%s" % (quote(c[0], "postgres"), c[1], " NOT NULL" if c[2] == "NO" else "",
                                               " DEFAULT %s" % c[4] if c[4] else "") for c in cols)
            idx = [r[2] for r in self.meta(conn, "indexes", schema, name)]
            return [["CREATE TABLE %s.%s (\n%s\n);\n%s" % (quote(sch, "postgres"), quote(name or "", "postgres"), body,
                                                         ";\n".join(idx) + (";" if idx else ""))]]
        sql, args = q[what]
        cur = conn.cursor()
        cur.execute(sql, args, prepare=True)
        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        return rows


def _pyformat_to_numeric(sql: str) -> str:
    """%s placeholders (what dbtools and the GUI generate) -> Oracle's :1, :2 (outside quotes)."""
    out, n, q, i = [], 0, "", 0
    while i < len(sql):
        ch = sql[i]
        if q:
            if ch == q:
                q = ""
        elif ch in "'\"":
            q = ch
        elif sql.startswith("%s", i):
            n += 1
            out.append(":%d" % n)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class Oracle(Driver):
    """oracledb thin mode (no Instant Client; server 12.1+). A READ ONLY transaction per statement
    stops DML but neither DDL (it commits) nor PL/SQL with an autonomous transaction - so Oracle is
    `boundary: transaction-weak` in dbtools and the allowlist carries more weight.
    `database` is the service name (FREEPDB1, XEPDB1, ...)."""
    engine = "oracle"

    def __init__(self, spec, mod=None):
        if mod is None:
            import oracledb as mod   # noqa: N813
        mod.defaults.fetch_lobs = False   # CLOB/BLOB as str/bytes, not LOB locators
        super().__init__(spec, mod)

    def connect(self, mode: str):
        s = self.spec
        dsn = self.mod.makedsn(s.get("host") or "127.0.0.1", int(s.get("port") or 1521), service_name=s.get("database") or "FREEPDB1")
        c = self.mod.connect(user=s.get("user") or "", password=s.get("password") or "", dsn=dsn, tcp_connect_timeout=10)
        c.autocommit = False
        c.module = "piper-%s" % mode
        return c

    def cursor(self, conn, unbuffered: bool):
        cur = conn.cursor()
        cur.arraysize = 500
        return cur

    @staticmethod
    def owner(schema):
        """Oracle folds unquoted names to upper case: `shop` (what an agent or `UPDATE shop.t` writes) is the
        user SHOP. A name with an upper-case letter is taken as written (a quoted "MixedCase" user)."""
        return schema.upper() if isinstance(schema, str) and schema == schema.lower() else schema

    LOST = ("DPY-1001", "DPY-4011", "DPY-4024", "DPI-1010", "DPI-1080", "ORA-00028", "ORA-01012", "ORA-02396",
            "ORA-03113", "ORA-03114", "ORA-03135")

    def lost(self, exc) -> bool:
        return isinstance(exc, (ConnectionError, BrokenPipeError)) or any(k in str(exc) for k in self.LOST)

    def begin_read(self, conn, schema):
        conn.rollback()
        cur = conn.cursor()
        target = self.owner(schema) or (self.spec.get("user") or "").upper()   # back to the user's own schema: never sticky
        if target:
            cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + quote(target, "oracle"))
        cur.execute("SET TRANSACTION READ ONLY")   # must be the transaction's first statement
        cur.close()

    def end(self, conn):
        try:
            conn.rollback()
        except Exception:   # noqa: BLE001
            pass

    def ro_ok(self, conn) -> bool:
        return True   # no session read-only flag to read back (14.3 #2)

    def identity(self, conn) -> dict:
        cur = conn.cursor()
        cur.execute("SELECT sys_context('USERENV','DB_NAME'), sys_context('USERENV','SERVER_HOST'), "
                    "sys_context('USERENV','CON_NAME'), sys_context('USERENV','CURRENT_SCHEMA') FROM dual")
        dbn, host, con, sch = cur.fetchone()
        cur.close()
        return {"server_id": "%s/%s" % (dbn, con), "hostname": str(host or ""), "version": str(conn.version), "database": sch}

    def thread_id(self, conn):
        return conn

    def kill(self, ctrl, conn) -> None:
        conn.cancel()

    def begin_write(self, conn, schema):
        conn.rollback()
        target = self.owner(schema) or (self.spec.get("user") or "").upper()
        if target:
            cur = conn.cursor()
            cur.execute("ALTER SESSION SET CURRENT_SCHEMA = " + quote(target, "oracle"))
            cur.close()

    def warnings(self, cur) -> list:
        return []

    def type_name(self, code) -> str:
        return getattr(code, "name", str(code)).lower().replace("db_type_", "")

    def meta(self, conn, what, schema, name):
        own = "COALESCE(:1, sys_context('USERENV','CURRENT_SCHEMA'))"
        q = {
            "schemas": ("SELECT username FROM all_users WHERE oracle_maintained = 'N' ORDER BY 1", ()),
            "objects": ("SELECT o.object_name, CASE o.object_type WHEN 'VIEW' THEN 'view' ELSE 'base table' END, NULL, "
                        "t.num_rows, c.comments FROM all_objects o LEFT JOIN all_tables t ON t.owner = o.owner AND "
                        "t.table_name = o.object_name LEFT JOIN all_tab_comments c ON c.owner = o.owner AND c.table_name = "
                        "o.object_name WHERE o.owner = %s AND o.object_type IN ('TABLE','VIEW') ORDER BY 1" % own, (schema,)),
            "routines": ("SELECT object_name, LOWER(object_type) FROM all_objects WHERE owner = %s AND object_type IN "
                         "('PROCEDURE','FUNCTION','PACKAGE') ORDER BY 1" % own, (schema,)),
            "columns": ("SELECT c.column_name, c.data_type || CASE WHEN c.data_type IN ('VARCHAR2','NVARCHAR2','CHAR','RAW') "
                        "THEN '(' || CASE WHEN c.char_used = 'C' THEN c.char_length || ' CHAR' ELSE TO_CHAR(c.data_length) END || ')' WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL THEN '(' "
                        "|| c.data_precision || ',' || c.data_scale || ')' ELSE '' END, CASE c.nullable WHEN 'N' THEN 'NO' "
                        "ELSE 'YES' END, CASE WHEN EXISTS (SELECT 1 FROM all_constraints k JOIN all_cons_columns kc ON "
                        "kc.owner = k.owner AND kc.constraint_name = k.constraint_name WHERE k.owner = c.owner AND "
                        "k.table_name = c.table_name AND k.constraint_type = 'P' AND kc.column_name = c.column_name) THEN "
                        "'PRI' ELSE '' END, NULL, '', NULL FROM all_tab_columns c WHERE c.owner = %s AND c.table_name = :2 "
                        "ORDER BY c.column_id" % own, (schema, name)),
            "fks": ("SELECT k.constraint_name, kc.column_name, r.owner, r.table_name, rc.column_name FROM all_constraints k "
                    "JOIN all_cons_columns kc ON kc.owner = k.owner AND kc.constraint_name = k.constraint_name JOIN "
                    "all_constraints r ON r.owner = k.r_owner AND r.constraint_name = k.r_constraint_name JOIN "
                    "all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name AND "
                    "rc.position = kc.position WHERE k.owner = %s AND k.table_name = :2 AND k.constraint_type = 'R' "
                    "ORDER BY 1, kc.position" % own, (schema, name)),
            "refs": ("SELECT k.owner, k.table_name, kc.column_name, rc.column_name, k.constraint_name FROM all_constraints k "
                     "JOIN all_cons_columns kc ON kc.owner = k.owner AND kc.constraint_name = k.constraint_name JOIN "
                     "all_constraints r ON r.owner = k.r_owner AND r.constraint_name = k.r_constraint_name JOIN "
                     "all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name AND "
                     "rc.position = kc.position WHERE r.owner = %s AND r.table_name = :2 AND k.constraint_type = 'R' "
                     "ORDER BY 1, 2" % own, (schema, name)),
            "indexes": ("SELECT i.index_name, CASE i.uniqueness WHEN 'UNIQUE' THEN 0 ELSE 1 END, LISTAGG(c.column_name, ',') "
                        "WITHIN GROUP (ORDER BY c.column_position) FROM all_indexes i JOIN all_ind_columns c ON "
                        "c.index_owner = i.owner AND c.index_name = i.index_name WHERE i.table_owner = %s AND "
                        "i.table_name = :2 GROUP BY i.index_name, i.uniqueness ORDER BY 1" % own, (schema, name)),
            "engines": ("SELECT owner, table_name, 'transactional' FROM all_tables WHERE owner = %s" % own, (schema,)),
            "triggers": ("SELECT table_name, trigger_name FROM all_triggers WHERE table_owner = %s" % own, (schema,)),
            "database": ("SELECT sys_context('USERENV','CURRENT_SCHEMA') FROM dual", ()),
            "colindex": ("SELECT table_name, column_name, data_type FROM all_tab_columns WHERE owner = %s "
                         "ORDER BY 1, column_id" % own, (schema,)),
        }
        if what == "ddl":
            cur = conn.cursor()
            cur.execute("SELECT DBMS_METADATA.GET_DDL('TABLE', :1, %s) FROM dual" % own.replace(":1", ":2"), (self.owner(name), self.owner(schema)))
            r = cur.fetchone()
            cur.close()
            return [[str(r[0]) if r else ""]]
        sql, args = q[what]
        args = tuple(self.owner(a) for a in args)   # schema, table: `orders` is ORDERS like in SQL
        cur = conn.cursor()
        cur.execute(sql, list(args))
        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        return rows


class MSSQL(Driver):
    """python-tds. SQL Server has no READ ONLY transaction: the read connection runs every statement
    in BEGIN TRANSACTION ... ROLLBACK (DML and even DDL roll back; xp_cmdshell, linked servers and
    mail do not) - `boundary: parser` in dbtools: on test/prod every agent read asks."""
    engine = "mssql"

    def __init__(self, spec, mod=None):
        if mod is None:
            import pytds as mod   # noqa: N813
        super().__init__(spec, mod)
        self.cur_of: dict = {}

    LOCK_WAIT_MS = 5000   # a read waits this long for a row another transaction holds (no MVCC by default)

    def connect(self, mode: str):
        s = self.spec
        c = self.mod.connect(dsn=s.get("host") or "127.0.0.1", port=int(s.get("port") or 1433), database=s.get("database") or None,
                             user=s.get("user") or "", password=s.get("password") or "", login_timeout=10,
                             appname="piper-%s" % mode, autocommit=False, bytes_to_unicode=True)
        if mode in ("read", "meta"):   # without it a read of a row the console's open transaction changed hangs until its timeout
            cur = c.cursor()
            cur.execute("SET LOCK_TIMEOUT %d" % self.LOCK_WAIT_MS)
            cur.close()
            c.commit()
        return c

    def lost(self, exc) -> bool:
        # pytds after a cancel / a killed session: ClosedConnectionError, a broken socket, or a stream out of step
        return isinstance(exc, (ConnectionError, BrokenPipeError, EOFError)) or type(exc).__name__ == "ClosedConnectionError" \
            or "Invalid TDS marker" in str(exc) or "Broken pipe" in str(exc)

    def cursor(self, conn, unbuffered: bool):
        cur = conn.cursor()
        self.cur_of[id(conn)] = cur
        return cur

    def begin_read(self, conn, schema):
        conn.rollback()
        target = schema or self.spec.get("database")
        if target:
            cur = conn.cursor()
            cur.execute("USE " + quote(target, "mssql"))
            cur.close()

    def end(self, conn):
        try:
            conn.rollback()
        except Exception:   # noqa: BLE001
            pass

    def ro_ok(self, conn) -> bool:
        return True   # pytds opens the next implicit transaction itself; the ROLLBACK is the boundary

    def identity(self, conn) -> dict:
        cur = conn.cursor()
        cur.execute("SELECT CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)), CAST(SERVERPROPERTY('MachineName') AS "
                    "nvarchar(256)), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)), DB_NAME()")
        u, h, v, d = cur.fetchone()
        cur.close()
        return {"server_id": str(u), "hostname": str(h), "version": "SQL Server " + str(v), "database": d}

    def thread_id(self, conn):
        return conn

    def kill(self, ctrl, conn) -> None:
        """Only SEND the attention packet. pytds' cursor.cancel() also reads the reply - from this
        (timer / db_cancel) thread while the running statement's thread reads the same socket: both
        hung for good (live SQL Server 2022). The statement's own thread reads the reply; the worker
        drops the connection after a cancel either way (Worker._query / op_execute)."""
        cur = self.cur_of.get(id(conn))
        sess = getattr(cur, "_session", None)
        if sess is not None and hasattr(sess, "put_cancel"):
            sess.put_cancel()
        elif cur is not None:
            cur.cancel()

    def begin_write(self, conn, schema):
        conn.rollback()
        target = schema or self.spec.get("database")
        if target:
            cur = conn.cursor()
            cur.execute("USE " + quote(target, "mssql"))
            cur.close()

    def warnings(self, cur) -> list:
        return []

    def type_name(self, code) -> str:
        return str(code)

    def meta(self, conn, what, schema, name):
        q = {
            "schemas": ("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY 1", ()),
            "objects": ("SELECT t.TABLE_NAME, LOWER(t.TABLE_TYPE), NULL, NULL, '' FROM INFORMATION_SCHEMA.TABLES t "
                        "ORDER BY 1", ()),
            "routines": ("SELECT ROUTINE_NAME, LOWER(ROUTINE_TYPE) FROM INFORMATION_SCHEMA.ROUTINES ORDER BY 1", ()),
            "columns": ("SELECT c.COLUMN_NAME, c.DATA_TYPE + CASE WHEN c.CHARACTER_MAXIMUM_LENGTH = -1 THEN '(max)' "
                        "WHEN c.CHARACTER_MAXIMUM_LENGTH IS NOT NULL THEN '(' + CAST(c.CHARACTER_MAXIMUM_LENGTH AS varchar) + ')' "
                        "WHEN c.DATA_TYPE IN ('decimal', 'numeric') THEN '(' + CAST(c.NUMERIC_PRECISION AS varchar) + ',' + "
                        "CAST(c.NUMERIC_SCALE AS varchar) + ')' ELSE '' END, "
                        "c.IS_NULLABLE, CASE WHEN EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS k JOIN "
                        "INFORMATION_SCHEMA.KEY_COLUMN_USAGE u ON u.CONSTRAINT_NAME = k.CONSTRAINT_NAME WHERE k.TABLE_NAME = "
                        "c.TABLE_NAME AND k.CONSTRAINT_TYPE = 'PRIMARY KEY' AND u.COLUMN_NAME = c.COLUMN_NAME) THEN 'PRI' "
                        "ELSE '' END, c.COLUMN_DEFAULT, '', '' FROM INFORMATION_SCHEMA.COLUMNS c WHERE c.TABLE_NAME = %s "
                        "ORDER BY c.ORDINAL_POSITION", (name,)),
            "fks": ("SELECT f.name, pc.name, DB_NAME(), rt.name, rc.name FROM sys.foreign_keys f JOIN "
                    "sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id JOIN sys.columns pc ON "
                    "pc.object_id = fc.parent_object_id AND pc.column_id = fc.parent_column_id JOIN sys.tables rt ON "
                    "rt.object_id = fc.referenced_object_id JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id "
                    "AND rc.column_id = fc.referenced_column_id WHERE OBJECT_NAME(f.parent_object_id) = %s", (name,)),
            "refs": ("SELECT DB_NAME(), pt.name, pc.name, rc.name, f.name FROM sys.foreign_keys f JOIN "
                     "sys.foreign_key_columns fc ON fc.constraint_object_id = f.object_id JOIN sys.tables pt ON "
                     "pt.object_id = fc.parent_object_id JOIN sys.columns pc ON pc.object_id = fc.parent_object_id AND "
                     "pc.column_id = fc.parent_column_id JOIN sys.columns rc ON rc.object_id = fc.referenced_object_id AND "
                     "rc.column_id = fc.referenced_column_id WHERE OBJECT_NAME(f.referenced_object_id) = %s", (name,)),
            "indexes": ("SELECT i.name, CASE WHEN i.is_unique = 1 THEN 0 ELSE 1 END, STRING_AGG(c.name, ',') FROM sys.indexes "
                        "i JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id JOIN "
                        "sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id WHERE "
                        "OBJECT_NAME(i.object_id) = %s GROUP BY i.name, i.is_unique", (name,)),
            "engines": ("SELECT TABLE_SCHEMA, TABLE_NAME, 'transactional' FROM INFORMATION_SCHEMA.TABLES WHERE "
                        "TABLE_TYPE = 'BASE TABLE'", ()),
            "triggers": ("SELECT OBJECT_NAME(parent_id), name FROM sys.triggers WHERE parent_class = 1", ()),
            "database": ("SELECT DB_NAME()", ()),
            "colindex": ("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS ORDER BY 1, ORDINAL_POSITION", ()),
        }
        if what == "ddl":
            cols = self.meta(conn, "columns", schema, name)
            return [["CREATE TABLE %s (\n%s\n)" % (quote(name or "", "mssql"), ",\n".join(
                "  %s %s%s" % (quote(c[0], "mssql"), c[1], " NOT NULL" if c[2] == "NO" else "") for c in cols))]]
        sql, args = q[what]
        cur = conn.cursor()
        cur.execute(sql, args or None)
        rows = [list(r) for r in cur.fetchall()]
        cur.close()
        return rows


DRIVERS = {"mysql": MySQL, "mariadb": MySQL, "postgres": Postgres, "oracle": Oracle, "mssql": MSSQL}


def quote(name: str, engine: str) -> str:
    if engine == "mysql":
        return "`" + str(name).replace("`", "``") + "`"
    if engine == "mssql":
        return "[" + str(name).replace("]", "]]") + "]"
    return '"' + str(name).replace('"', '""') + '"'


# ------------------------------------------------------------------ the worker
class Worker:
    def __init__(self, drivers=None, out=None):
        self.drivers = drivers or DRIVERS
        self.drv: Driver | None = None
        self.conns: dict = {}                # mode -> connection
        self.locks = {"read": threading.Lock(), "write": threading.Lock(), "meta": threading.Lock()}
        self.running: dict = {}              # request id -> (mode, thread id / conn); (None, None) while preparing
        self.cancelled: set = set()
        self.timed_out: set = set()          # of `cancelled`: stopped by the time limit, not by a cancel request
        self.out = out or sys.stdout
        self.out_lock = threading.Lock()
        self.password = ""
        self.tx_open = False          # the GUI console's held transaction (tx_* ops, its own worker)
        self.tx_since = 0.0
        self.tx_schema = None
        self.transient_reads = False  # a held GUI transaction needs only its write connection at rest
        self._used = threading.local()   # per request thread: the connection it got from conn()

    def send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False, default=str)
        with self.out_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def scrub(self, text) -> str:
        a = getattr(text, "args", ())
        if len(a) > 1 and isinstance(a[0], str) and all(x is None for x in a[1:]):
            text = a[0]   # pytds: ProgrammingError('The DELETE statement conflicted ...', None) said "('...', None)"
        text = str(text)
        if self.password and len(self.password) >= 3:
            text = text.replace(self.password, "••••")
        return text[:2000]

    # connections ---------------------------------------------------
    def conn(self, mode: str):
        c = self.conns.get(mode)
        if c is not None:
            try:
                if hasattr(c, "ping") and self.drv.engine == "mysql":
                    c.ping(reconnect=False)
                elif getattr(c, "closed", False):
                    raise WorkerError("closed")
                elif not (mode == "write" and self.tx_open) and self._hung_up(c):
                    raise WorkerError("closed by the server")   # an open transaction's loss must show, never be papered over
                return self._use(mode, c)
            except Exception:   # noqa: BLE001 - reconnect below
                self.drop(mode)
        c = self.drv.connect(mode)
        self.conns[mode] = c
        return self._use(mode, c)

    def _use(self, mode: str, c):
        setattr(self._used, mode, c)
        return c

    @staticmethod
    def _hung_up(c) -> bool:
        """An idle connection whose socket is readable was closed by the server (restart, idle kill,
        pg_terminate_backend: its last words or EOF wait there). No round trip; engines without a
        file descriptor (oracledb thin, pytds) find out on the next statement as before."""
        try:
            fd = c.fileno()
        except Exception:   # noqa: BLE001
            return False
        try:
            return bool(select.select([fd], [], [], 0)[0])
        except (OSError, ValueError):
            return True

    def drop(self, mode: str) -> None:
        if mode == "write":
            self.tx_open = False
        c = self.conns.pop(mode, None)
        if c is not None:
            try:
                c.close()
            except Exception:   # noqa: BLE001
                pass

    def _timer(self, rid: str, timeout_s: float):
        t = threading.Timer(max(1.0, float(timeout_s)), self._timeout, (rid,))
        t.daemon = True
        t.start()
        return t

    def _timeout(self, rid: str) -> None:
        if rid in self.running:
            self.timed_out.add(rid)
            self.cancelled.add(rid)
            self._kill(rid)

    def _stopped(self, rid: str) -> str:
        """Why a cancelled statement stopped, for the person (the console's Cancel) and the agent
        (its own cancel or the time limit) alike - no tool names."""
        return "cancelled at the time limit" if rid in self.timed_out else "cancelled"

    def _kill(self, rid: str) -> None:
        mode, tid = self.running.get(rid, (None, None))
        if tid is None:
            return
        try:
            if self.drv.engine == "mysql":
                ctrl = self.drv.connect("control")
                try:
                    self.drv.kill(ctrl, tid)
                finally:
                    ctrl.close()
            else:
                self.drv.kill(None, tid)
        except Exception as e:   # noqa: BLE001 - reported on the statement itself
            self.send({"event": "kill-failed", "id": rid, "error": self.scrub(e)})

    # ops ------------------------------------------------------------
    def op_open(self, req):
        spec = dict(req.get("spec") or {})
        eng = str(spec.get("engine") or "mysql")
        if eng not in self.drivers:
            raise WorkerError("engine %s is not supported yet" % eng, "engine")
        self.password = str(spec.get("password") or "")
        for m in list(self.conns):
            self.drop(m)
        self.drv = self.drivers[eng](spec)
        self.transient_reads = bool(req.get("transient_reads"))
        with self.locks["read"]:
            c = self.conn("read")
            ident = self.drv.identity(c)
            ident["read_only"] = self.drv.ro_ok(c)
        return ident

    def _need(self):
        if self.drv is None:
            raise WorkerError("not open", "state")

    def op_query(self, req, rid):
        """A read whose connection the server dropped meanwhile (killed, restarted - engines without a
        socket to look at find out only now) runs once more on a new connection: reads are repeatable."""
        self._need()
        try:
            return self._query(req, rid)
        except Exception as e:   # noqa: BLE001
            if rid in self.cancelled or not self._lost(e, "read"):
                raise
        return self._query(req, rid)

    def _lost(self, e, mode: str) -> bool:
        """Drop a connection the error left unusable; True = it was dropped."""
        try:
            gone = self.drv.lost(e)
        except Exception:   # noqa: BLE001
            gone = False
        if gone and mode == "read":   # reads run in parallel: never drop the fresh connection another request just made
            with self.locks["read"]:
                if self.conns.get("read") is getattr(self._used, "read", None):
                    self.drop("read")
        elif gone:   # the write connection: its lock is held by the caller
            self.drop(mode)
        return gone

    def _query(self, req, rid):
        max_rows = max(1, min(int(req.get("max_rows") or 200), 100000))
        max_cell = max(64, min(int(req.get("max_cell") or MAX_CELL_FULL), MAX_CELL_FULL))
        sql, params = str(req["sql"]), req.get("params")
        unbuffered = bool(req.get("unbuffered", True))
        if self.drv.engine == "postgres" and not re.match(r"\s*\(*\s*(SELECT|WITH|VALUES|TABLE)\b", sql, re.I):
            unbuffered = False   # DECLARE CURSOR takes only a SELECT / VALUES (SHOW, EXPLAIN: a plain prepared cursor)
        with self.locks["read"]:
            c = self.conn("read")
            self.drv.begin_read(c, req.get("schema"))
            self.running[rid] = ("read", self.drv.thread_id(c))
            t = self._timer(rid, req.get("timeout_s") or 30)
            t0 = time.time()
            cur = None
            try:
                if rid in self.cancelled:
                    raise WorkerError(self._stopped(rid), "cancelled")
                cur = self.drv.cursor(c, unbuffered)
                self._exec(cur, sql, params)
                cols, rows, cut, more = self._fetch(cur, max_rows, max_cell)
            except Exception as e:   # noqa: BLE001
                self.drv.end(c)
                if rid in self.cancelled:
                    if self.drv.engine == "mssql":   # pytds after a cancel: the stream may be out of step - start clean
                        self.drop("read")
                    raise WorkerError("%s after %.1f s" % (self._stopped(rid), time.time() - t0), "cancelled")
                if self.drv.engine == "mssql" and "Lock request time out" in str(e):
                    raise WorkerError("the rows are locked by another open transaction (an uncommitted change - the "
                                      "console's?) - commit or roll it back, then read again", "locked") from None
                raise
            finally:
                t.cancel()
            if rid in self.cancelled:   # MySQL: a killed SLEEP() returns 1 instead of failing
                self.drv.end(c)
                if self.drv.engine == "mssql":   # the attention reply may still be on the wire
                    self.drop("read")
                raise WorkerError("%s after %.1f s" % (self._stopped(rid), time.time() - t0), "cancelled")
            elapsed = int((time.time() - t0) * 1000)
            if self.drv.engine == "postgres" and any(isinstance(c.get("org"), tuple) for c in cols):
                self._pg_origins(c, cols)
            if more:   # a big result: stop the server instead of draining it through the socket
                self.running[rid] = ("read", self.drv.thread_id(c))
                if self.drv.engine == "mysql":
                    self._kill(rid)
            try:
                if cur is not None:
                    cur.close()
            except Exception:   # noqa: BLE001 - the killed remainder
                pass
            self.drv.end(c)
            ok = True
            try:
                ok = self.drv.ro_ok(c)
            except Exception:   # noqa: BLE001 - a killed connection: drop it, a new one starts read-only
                self.drop("read")
            if not ok:
                self.drop("read")
            return {"columns": cols, "rows": rows, "truncated": more, "cut_cells": cut,
                    "elapsed_ms": elapsed, "read_only_held": ok}

    def _exec(self, cur, sql, params):
        if self.drv.engine == "oracle":
            if not re.match(r"\s*(BEGIN|DECLARE|CREATE\s+(OR\s+REPLACE\s+)?(PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE))\b", sql, re.I):
                sql = sql.rstrip().rstrip(";")   # a trailing ; is ORA-00933 outside PL/SQL
            cur.execute(_pyformat_to_numeric(sql) if params else sql, list(params) if isinstance(params, (list, tuple)) else params or [])
            return
        if self.drv.engine == "postgres" and not getattr(cur, "name", None):
            cur.execute(sql, params or None, prepare=True)   # extended protocol: one statement only
        elif params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)

    def _origins(self, cur) -> list:
        """The source column of each result column ("" = an expression) - masking must not be
        fooled by `SELECT email AS e`. MySQL sends it (org_name); PostgreSQL has table oid + attnum."""
        try:
            if self.drv.engine == "mysql":
                fields = getattr(getattr(cur, "_result", None), "fields", None) or []
                return [(f.org_name or "") if isinstance(f.org_name, str) else (f.org_name or b"").decode("utf-8", "replace")
                        for f in fields]
            if self.drv.engine == "postgres":
                pg = cur.pgresult
                return [(pg.ftable(i) or None, pg.ftablecol(i) or None) for i in range(pg.nfields)]
        except Exception:   # noqa: BLE001 - unknown origins count as expressions
            pass
        return []

    def _fetch(self, cur, max_rows, max_cell):
        if cur.description is None:
            return [], [], [], False
        cols = [{"name": str(d[0]), "type": self.drv.type_name(d[1])} for d in cur.description]
        orgs = self._origins(cur)
        if orgs and len(orgs) == len(cols):
            for c, o in zip(cols, orgs):
                c["org"] = o
        raw = cur.fetchmany(max_rows + 1)
        more = len(raw) > max_rows
        bits = {i for i, c in enumerate(cols) if c["type"] == "bit"}
        rows, cut = [], []
        for ri, r in enumerate(raw[:max_rows]):
            out = []
            for ci, v in enumerate(r):
                jv, was_cut = jsonable(bit_value(v) if ci in bits else v, max_cell)
                if was_cut:
                    cut.append([ri, ci])
                out.append(jv)
            rows.append(out)
        return cols, rows, cut, more

    def _pg_origins(self, conn, cols) -> None:
        pairs = [c["org"] for c in cols if isinstance(c.get("org"), tuple) and c["org"][0]]
        names = {}
        if pairs:
            try:
                cur = conn.cursor()
                cur.execute("SELECT attrelid, attnum, attname FROM pg_attribute WHERE (attrelid, attnum) IN (%s)"
                            % ", ".join("(%s, %s)" for _ in pairs), [v for p in pairs for v in p], prepare=True)
                names = {(r[0], r[1]): r[2] for r in cur.fetchall()}
                cur.close()
            except Exception:   # noqa: BLE001
                names = {}
        for c in cols:
            o = c.get("org")
            if isinstance(o, tuple):
                c["org"] = names.get(o, "") if o[0] else ""

    def _sample_before(self, c, sample):
        """Best effort: a sample that fails (an alias the WHERE uses, ...) returns None and must not
        break the write or - PostgreSQL aborts a transaction on any error - the held transaction."""
        sp = self.drv.engine == "postgres"
        cur = self.drv.cursor(c, False)
        try:
            if sp:
                cur.execute("SAVEPOINT piper_sample")
            self._exec(cur, str(sample["select"]), sample.get("params"))
            cols, rows, _, more = self._fetch(cur, 20, 300)
            total = None
            if sample.get("table"):
                if sp:
                    cur.execute("SAVEPOINT piper_sample_count")
                try:
                    # optional: a short limit of its own, so a slow count never costs the whole dry run
                    if self.drv.engine == "mysql":
                        self._exec(cur, "SELECT /*+ MAX_EXECUTION_TIME(%d) */ COUNT(*) FROM %s" % (COUNT_MS, sample["table"]), None)
                    elif sp:
                        cur.execute("SELECT current_setting('statement_timeout')")
                        before = cur.fetchone()[0]
                        cur.execute("SET LOCAL statement_timeout = %d" % COUNT_MS)
                        self._exec(cur, "SELECT COUNT(*) FROM " + str(sample["table"]), None)
                        total = cur.fetchone()[0]
                        cur.execute("SELECT set_config('statement_timeout', %s, true)", (before,))
                    else:
                        self._exec(cur, "SELECT COUNT(*) FROM " + str(sample["table"]), None)
                    if total is None:
                        total = cur.fetchone()[0]
                except Exception:   # noqa: BLE001 - a missing optional count never discards a usable sample
                    if sp:
                        cur.execute("ROLLBACK TO SAVEPOINT piper_sample_count")
                finally:
                    if sp:
                        cur.execute("RELEASE SAVEPOINT piper_sample_count")
            if sp:
                cur.execute("RELEASE SAVEPOINT piper_sample")
            return {"columns": cols, "rows": rows, "more": more, "table_total": total}
        except Exception:   # noqa: BLE001
            if sp:
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT piper_sample")
                except Exception:   # noqa: BLE001
                    pass
            return None
        finally:
            try:
                cur.close()
            except Exception:   # noqa: BLE001
                pass

    def _sample_after(self, c, sample, before):
        """The rows `before` named again by primary key (an UPDATE's new values)."""
        if not (before and before["rows"] and sample.get("pk") and sample.get("update")):
            return None
        names = [x["name"] for x in before["columns"]]
        pki = [names.index(k) for k in sample["pk_names"] if k in names]
        if len(pki) != len(sample["pk"]):
            return None
        keys = [[r[j] for j in pki] for r in before["rows"]]
        if len(pki) == 1:
            cond = "%s IN (%s)" % (sample["pk"][0], ", ".join([self.drv.ph] * len(keys)))
            params = [k[0] for k in keys]
        else:
            cond = "(%s) IN (%s)" % (", ".join(sample["pk"]), ", ".join(
                "(" + ", ".join([self.drv.ph] * len(pki)) + ")" for _ in keys))
            params = [v for k in keys for v in k]
        try:
            cur = self.drv.cursor(c, False)
            self._exec(cur, "SELECT * FROM %s WHERE %s" % (sample["table"], cond), params)
            cols, rows, _, _ = self._fetch(cur, 20, 300)
            cur.close()
            by_key = {tuple(row[j] for j in pki): row for row in rows}
            ordered = [by_key.get(tuple(key)) for key in keys]
            if any(row is None for row in ordered):
                return None  # a changed primary key cannot be paired honestly
            return {"columns": cols, "rows": ordered}
        except Exception:   # noqa: BLE001 - only a courtesy (PostgreSQL: the statement already ran fine)
            return None

    def op_execute(self, req, rid):
        self._need()
        stmts = req.get("statements") or []
        preview = bool(req.get("preview"))
        expect = req.get("expect")
        tol = float(req.get("tolerance") or 0)
        results = []
        with self.locks["write"]:
            c = self._begin_write(req.get("schema"))
            self.running[rid] = ("write", self.drv.thread_id(c))
            t = self._timer(rid, req.get("timeout_s") or 30)
            t0 = time.time()
            committed = False
            sample = req.get("sample") if preview and len(stmts) == 1 else None
            before = after = None
            try:
                if rid in self.cancelled:
                    raise WorkerError(self._stopped(rid), "cancelled")
                if preview and self.drv.engine == "postgres":
                    cur = self.drv.cursor(c, False)
                    self._exec(cur, "SET LOCAL statement_timeout = %s" % max(1, int(float(req.get("timeout_s") or 2) * 1000)), None)
                    self._exec(cur, "SET LOCAL lock_timeout = %s" % max(1, int(float(req.get("timeout_s") or 2) * 1000)), None)
                    cur.close()
                if sample:   # the rows the write touches, read inside the same (rolled back) transaction
                    before = self._sample_before(c, sample)
                for i, st in enumerate(stmts):
                    if rid in self.cancelled:
                        raise WorkerError(self._stopped(rid), "cancelled")
                    cur = self.drv.cursor(c, False)
                    self._exec(cur, str(st["sql"]), st.get("params"))
                    r = {"rowcount": cur.rowcount, "lastrowid": getattr(cur, "lastrowid", None)}
                    if cur.description is not None:
                        cols, rows, cut, more = self._fetch(cur, int(req.get("max_rows") or 50), 4096)
                        r.update(columns=cols, rows=rows, truncated=more)
                    w = self.drv.warnings(cur)
                    if w:
                        r["warnings"] = w
                    cur.close()
                    results.append(r)
                    if rid in self.cancelled:
                        raise WorkerError("cancelled", "cancelled")
                if not preview and isinstance(expect, list):
                    for i, (r, e) in enumerate(zip(results, expect)):
                        if e is None or r["rowcount"] is None or r["rowcount"] < 0:
                            continue
                        if abs(r["rowcount"] - e) > max(0, tol * max(e, 1)):
                            raise WorkerError("statement %d now affects %d rows, the approved preview said %d - "
                                              "rolled back, nothing changed" % (i + 1, r["rowcount"], e), "drift")
                if sample:
                    after = self._sample_after(c, sample, before)
                if preview:
                    self.drv.end(c)
                else:
                    self.running[rid] = ("write", self.drv.thread_id(c))
                    try:
                        c.commit()
                        committed = True
                    except Exception as e:   # noqa: BLE001 - sent, no answer: the server may have committed
                        self.drop("write")
                        raise WorkerError("COMMIT was interrupted (%s) - the outcome is UNKNOWN, verify the data"
                                          % self.scrub(e), "outcome_unknown") from None
            except WorkerError as e:
                if not committed:
                    self.drv.end(c)
                if e.code == "cancelled" and self.drv.engine == "mssql":   # the attention reply may be on the wire
                    self.drop("write")
                raise
            except Exception as e:   # noqa: BLE001
                self.drv.end(c)
                if self._lost(e, "write"):
                    raise WorkerError("statement %d failed: the connection was lost (%s) - the server rolled the "
                                      "transaction back, nothing changed" % (len(results) + 1, self.scrub(e)), "sql") from None
                if rid in self.cancelled:
                    if self.drv.engine == "mssql":
                        self.drop("write")
                    raise WorkerError("%s after %.1f s - rolled back"
                                      % (self._stopped(rid), time.time() - t0), "cancelled") from None
                raise WorkerError("statement %d failed: %s - rolled back, nothing changed"
                                  % (len(results) + 1, self.scrub(e)), "sql") from None
            finally:
                t.cancel()
            out = {"results": results, "committed": committed, "elapsed_ms": int((time.time() - t0) * 1000)}
            if before is not None:
                out["sample"] = {"before": before, "after": after, "pk_names": (sample or {}).get("pk_names") or []}
            return out

    def _begin_write(self, schema, c=None):
        """The write connection, a transaction begun. Nothing ran yet: a connection found dead here
        (oracledb thin / pytds have no socket to check beforehand) is replaced once."""
        c = c or self.conn("write")
        try:
            self.drv.begin_write(c, schema)
        except Exception as e:   # noqa: BLE001
            if not self._lost(e, "write"):
                raise
            c = self.conn("write")
            self.drv.begin_write(c, schema)
        return c

    def op_explain(self, req, rid):
        if not req.get("on_write"):
            q = dict(req, sql="EXPLAIN " + str(req["sql"]), unbuffered=False, max_rows=500)
            return self.op_query(q, rid)
        # MySQL refuses EXPLAIN <DML> in a READ ONLY transaction (1792): the write connection, in a
        # transaction that is always rolled back - EXPLAIN without ANALYZE executes nothing
        self._need()
        with self.locks["write"]:
            c = self.conn("write")
            self.drv.begin_write(c, req.get("schema"))
            try:
                cur = self.drv.cursor(c, False)
                self._exec(cur, "EXPLAIN " + str(req["sql"]), req.get("params"))
                cols, rows, cut, more = self._fetch(cur, 500, 4096)
                cur.close()
            finally:
                self.drv.end(c)
        return {"columns": cols, "rows": rows, "truncated": more, "cut_cells": cut, "elapsed_ms": 0,
                "read_only_held": True}

    def op_meta(self, req, rid):
        self._need()
        try:
            return self._meta(req)
        except Exception as e:   # noqa: BLE001
            if not self._lost(e, "read"):
                raise
        return self._meta(req)

    def _meta(self, req):
        with self.locks["read"]:
            c = self.conn("read")
            self.drv.begin_read(c, None)
            try:
                rows = self.drv.meta(c, str(req["what"]), req.get("schema") or None, req.get("name") or None)
            finally:
                self.drv.end(c)
        return {"rows": [[jsonable(v, 1 << 16)[0] for v in r] for r in rows]}

    # the GUI console in read-write mode: one transaction across requests until tx_commit / tx_rollback
    def op_tx_exec(self, req, rid):
        self._need()
        stmts = req.get("statements") or []
        with self.locks["write"]:
            if self.tx_open and "write" not in self.conns:
                self.tx_open = False
                raise WorkerError("the connection dropped - the server rolled the open transaction back", "tx_lost")
            was_open, old = self.tx_open, self.conns.get("write")
            c = self.conn("write")
            if was_open and (c is not old or not self.tx_open):   # conn()'s health check replaced the connection
                self.tx_open = False
                raise WorkerError("the connection dropped - the server rolled the open transaction back", "tx_lost")
            if not self.tx_open:
                c = self._begin_write(req.get("schema"), c)
                self.tx_open, self.tx_since = True, time.time()
                self.tx_schema = req.get("schema") or None
            elif (req.get("schema") or None) != self.tx_schema:
                raise WorkerError("the open transaction uses a different schema - commit or roll back before changing it", "tx_schema")
            self.running[rid] = ("write", self.drv.thread_id(c))
            t = self._timer(rid, req.get("timeout_s") or 120)
            t0 = time.time()
            results = []
            sample = req.get("sample") if len(stmts) == 1 else None
            smp = None
            try:
                if rid in self.cancelled:
                    raise WorkerError(self._stopped(rid), "cancelled")
                before = self._sample_before(c, sample) if sample else None
                for st in stmts:
                    if rid in self.cancelled:
                        raise WorkerError(self._stopped(rid), "cancelled")
                    cur = self.drv.cursor(c, False)
                    self._exec(cur, str(st["sql"]), st.get("params"))
                    r = {"rowcount": cur.rowcount, "lastrowid": getattr(cur, "lastrowid", None)}
                    if cur.description is not None:
                        cols, rows, cut, more = self._fetch(cur, int(req.get("max_rows") or 1000), MAX_CELL_FULL)
                        r.update(columns=cols, rows=rows, truncated=more)
                    w = self.drv.warnings(cur)
                    if w:
                        r["warnings"] = w
                    cur.close()
                    if rid in self.cancelled:   # MySQL KILL of SLEEP can return rows instead of an error.
                        raise WorkerError(self._stopped(rid), "cancelled")
                    results.append(r)
            except Exception as e:   # noqa: BLE001
                if rid in self.cancelled and self.drv.engine == "mssql":   # drop() = the server rolls it back
                    self.drop("write")
                    raise WorkerError("statement %d %s - the connection was reset, the open transaction rolled back"
                                      % (len(results) + 1, self._stopped(rid)), "tx_lost") from None
                if self._lost(e, "write"):   # drop() closes the transaction too
                    raise WorkerError("statement %d failed: the connection was lost (%s) - the server rolled the open "
                                      "transaction back" % (len(results) + 1, self.scrub(e)), "tx_lost") from None
                if self.drv.engine == "postgres":   # an error aborts a PostgreSQL transaction
                    self.drv.end(c)
                    self.tx_open = False
                msg = self._stopped(rid) if rid in self.cancelled else self.scrub(e)
                raise WorkerError("statement %d failed: %s%s" % (len(results) + 1, msg, " - the transaction was rolled back"
                                  if not self.tx_open else " - the transaction is still open"),
                                  "cancelled" if rid in self.cancelled else "sql") from None
            finally:
                t.cancel()
            if sample and before is not None:
                try:
                    smp = {"before": before, "after": self._sample_after(c, sample, before)}
                except Exception:   # noqa: BLE001 - the sample is a courtesy, the statement ran
                    smp = None
            out = {"results": results, "tx_open": self.tx_open, "since": self.tx_since, "schema": self.tx_schema,
                   "elapsed_ms": int((time.time() - t0) * 1000)}
            if smp:
                out["sample"] = smp
            return out

    def op_tx_commit(self, req, rid):
        with self.locks["write"]:
            if not self.tx_open:
                return {"tx_open": False, "committed": False}
            c = self.conns.get("write")
            try:
                c.commit()
            except Exception as e:   # noqa: BLE001
                self.drop("write")
                raise WorkerError("COMMIT was interrupted (%s) - the outcome is UNKNOWN, verify the data"
                                  % self.scrub(e), "outcome_unknown") from None
            self.tx_open = False
            return {"tx_open": False, "committed": True}

    def op_tx_rollback(self, req, rid):
        with self.locks["write"]:
            if self.tx_open and "write" in self.conns:
                self.drv.end(self.conns["write"])
            self.tx_open = False
            return {"tx_open": False}

    def op_tx_state(self, req, rid):
        return {"tx_open": self.tx_open, "since": self.tx_since, "schema": self.tx_schema}

    def op_cancel(self, req, rid):
        n = 0
        ids = req.get("ids")
        for other in list(self.running):
            if other != rid and (ids is None or other in ids):   # ids: only the caller's own requests
                self.cancelled.add(other)
                self._kill(other)
                n += 1
        return {"cancelled": n}

    def op_ping(self, req, rid):
        return {"pong": True, "open": self.drv is not None}

    def handle(self, req: dict) -> dict:
        rid = str(req.get("id") or uuid.uuid4().hex)
        op = str(req.get("op") or "")
        if op in ("query", "explain", "execute", "tx_exec"):
            self.running.setdefault(rid, (None, None))
        try:
            fn = getattr(self, "op_" + op, None)
            if fn is None:
                raise WorkerError("unknown op %r" % op, "op")
            res = fn(req) if op == "open" else fn(req, rid)
            return {"id": rid, "ok": True, "result": res}
        except WorkerError as e:
            return {"id": rid, "ok": False, "error": self.scrub(e), "code": e.code}
        except Exception as e:   # noqa: BLE001 - a driver error: scrubbed text back
            args = getattr(e, "args", ())
            code = str(args[0]) if args and isinstance(args[0], int) else type(e).__name__
            text = "%s: %s" % (args[0], args[1]) if len(args) == 2 and isinstance(args[0], int) else self.scrub(e)
            if not text.strip():   # pytds: a closed port retries until login_timeout, then TimeoutError("")
                text = "%s - no answer from the server (wrong host or port, the server is down, or a firewall drops it)" \
                    % type(e).__name__ if isinstance(e, (TimeoutError, OSError)) else type(e).__name__
            return {"id": rid, "ok": False, "error": self.scrub(text), "code": code}
        finally:
            self.running.pop(rid, None)
            self.cancelled.discard(rid)
            self.timed_out.discard(rid)
            if self.transient_reads and req.get("op") in ("open", "query", "meta", "explain"):
                with self.locks["read"]:
                    self.drop("read")

    def serve(self, inp=None) -> None:
        inp = inp or sys.stdin
        for line in inp:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                continue
            if req.get("op") == "close":
                break
            if req.get("op") in ("cancel", "ping"):
                self.send(self.handle(req))
                continue
            if req.get("op") in ("query", "explain", "execute", "tx_exec"):
                req["id"] = str(req.get("id") or uuid.uuid4().hex)
                self.running[req["id"]] = (None, None)   # cancel can arrive before the new thread starts
            threading.Thread(target=lambda r=req: self.send(self.handle(r)), daemon=True).start()
        for m in list(self.conns):
            self.drop(m)


def check_drivers() -> dict:
    """Which drivers import in this interpreter (the venv's): {engine: version or error}."""
    out = {}
    for eng, mod in (("mysql", "pymysql"), ("postgres", "psycopg"), ("oracle", "oracledb"), ("mssql", "pytds")):
        try:
            m = __import__(mod)
            out[eng] = getattr(m, "__version__", "ok")
        except Exception as e:   # noqa: BLE001
            out[eng] = "missing: %s" % e
    return out


if __name__ == "__main__":
    if os.environ.get("PIPER_DB_TEST_DRIVERS"):   # tests only: extra drivers (tests/piper-unit/dbfake_sqlite.py)
        import importlib.util
        _spec = importlib.util.spec_from_file_location("piper_db_test_drivers", os.environ["PIPER_DB_TEST_DRIVERS"])
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        DRIVERS.update(_mod.DRIVERS)
    if "--check" in sys.argv:
        print(json.dumps(check_drivers()))
        sys.exit(0)
    Worker().serve()
