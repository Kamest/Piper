"""A SQLite stand-in for dbworker's drivers in the Manager tests (PIPER_DB_TEST_DRIVERS):
`database` is the file; the read connection is PRAGMA query_only (really read-only); a table
named mem_* reports engine MEMORY so the preview gate can be tested; _server holds the identity."""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper"))
import dbworker as W  # noqa: E402


class SQLite(W.Driver):
    engine = "sqlite"
    ph = "?"

    def connect(self, mode):
        c = sqlite3.connect(self.spec["database"], isolation_level=None, check_same_thread=False, timeout=5)
        if mode == "read":
            c.execute("PRAGMA query_only=ON")
        return c

    def cursor(self, conn, unbuffered):
        return conn.cursor()

    def begin_read(self, conn, schema):
        conn.execute("BEGIN")

    def end(self, conn):
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def ro_ok(self, conn):
        return conn.execute("PRAGMA query_only").fetchone()[0] == 1

    def identity(self, conn):
        try:
            u = conn.execute("SELECT id FROM _server").fetchone()[0]
        except sqlite3.Error:
            u = "none"
        return {"server_id": u, "hostname": "sqlite", "version": sqlite3.sqlite_version, "database": "main"}

    def thread_id(self, conn):
        return conn

    def kill(self, ctrl, conn):
        conn.interrupt()

    def begin_write(self, conn, schema):
        conn.execute("BEGIN")

    def warnings(self, cur):
        return []

    def type_name(self, code):
        return "text"

    def meta(self, conn, what, schema, name):
        if what == "schemas":
            return [["main"]]
        if what in ("objects", "engines"):
            rows = conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE "
                                "'sqlite_%' ORDER BY name").fetchall()
            if what == "engines":
                return [["", n, "MEMORY" if n.startswith("mem_") else "InnoDB"] for n, t in rows]
            return [[n, "view" if t == "view" else "base table", "InnoDB", None, ""] for n, t in rows]
        if what == "routines":
            return []
        if what == "colindex":
            tabs = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE "
                                               "'sqlite_%' ORDER BY name")]
            return [[t, r[1], r[2]] for t in tabs for r in conn.execute("PRAGMA table_info(%s)" % W.quote(t, "postgres"))]
        if what == "triggers":
            return [[r[0], r[1]] for r in conn.execute("SELECT tbl_name, name FROM sqlite_master WHERE type='trigger'")]
        if what == "columns":
            return [[r[1], r[2], "NO" if r[3] else "YES", "PRI" if r[5] else "", r[4], "", ""]
                    for r in conn.execute("PRAGMA table_info(%s)" % W.quote(name, "postgres"))]
        if what in ("fks", "refs", "indexes"):
            return []
        if what == "ddl":
            r = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            return [[r[0] if r else ""]]
        if what == "database":
            return [["main"]]
        raise W.WorkerError("unknown meta %s" % what)


DRIVERS = {"sqlite": SQLite}
