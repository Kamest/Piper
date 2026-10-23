"""The DB driver process (dbworker.py): a fake driver for the protocol and the transaction
discipline, and - with PIPER_DB_E2E_MYSQL=host:port:user:password:db and pymysql importable (the
drivers' venv) - the live invariants against a throwaway MySQL 8:
UPDATE / CREATE through the read connection fail with 1792, a preview leaves InnoDB data
unchanged, KILL QUERY stops a long statement, a big result is cut, not drained."""
import io
import json
import os
import threading
import time
import unittest

import _env  # noqa: F401
import dbworker as W


class FakeCursor:
    def __init__(self, db):
        self.db, self.description, self.rowcount, self.lastrowid, self._rows = db, None, -1, None, []

    def execute(self, sql, params=None, prepare=None):
        self.db.log.append(sql)
        if "FAIL" in sql:
            raise RuntimeError("boom with secret-pw inside")
        if sql.startswith("SELECT @@session.transaction_read_only"):
            self.description, self._rows = [("ro", 1)], [(0 if self.db.escaped else 1,)]
        elif sql.startswith("SELECT @@server_uuid"):
            self.description, self._rows = [("u", 1)] * 4, [("uuid-1", "host", "8.0", "db")]
        elif sql.startswith("SELECT"):
            n = self.db.rows
            self.description, self._rows = [("a", 3), ("b", 253)], [(i, "x" * self.db.cell) for i in range(n)]
        elif sql.startswith("UPDATE"):
            self.description, self.rowcount = None, self.db.affected

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, n):
        return self._rows[:n]

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConn:
    def __init__(self, db, mode):
        self.db, self.mode = db, mode

    def cursor(self, *a, **k):
        return FakeCursor(self.db)

    def rollback(self):
        self.db.log.append("ROLLBACK")

    def commit(self):
        self.db.log.append("COMMIT")
        if self.db.commit_fails:
            raise ConnectionError("lost connection")

    def ping(self, reconnect=False):
        if self in self.db.dead:
            raise ConnectionError("(2013, 'Lost connection to MySQL server during query')")

    def thread_id(self):
        return 7

    def close(self):
        pass


class FakeDriver(W.MySQL):
    db = None

    def __init__(self, spec, mod=None):
        W.Driver.__init__(self, spec, None)

    def connect(self, mode):
        self.db.connects.append(mode)
        return FakeConn(self.db, mode)

    def cursor(self, conn, unbuffered):
        return conn.cursor()

    def kill(self, ctrl, tid):
        self.db.log.append("KILL QUERY %d" % tid)

    def warnings(self, cur):
        return []

    def type_name(self, code):
        return str(code)


class DB:
    def __init__(self):
        self.log, self.connects, self.rows, self.cell, self.affected = [], [], 3, 5, 2
        self.escaped, self.commit_fails = False, False
        self.dead = []   # connections the server killed: ping raises


class FakeWorker(unittest.TestCase):
    def setUp(self):
        self.db = DB()
        FakeDriver.db = self.db
        self.w = W.Worker(drivers={"mysql": FakeDriver}, out=io.StringIO())
        r = self.w.handle({"op": "open", "spec": {"engine": "mysql", "password": "secret-pw"}})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["server_id"], "uuid-1")

    def test_query_in_read_only_transaction(self):
        self.db.log.clear()
        r = self.w.handle({"op": "query", "sql": "SELECT a, b FROM t", "max_rows": 10})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.db.log[0], "START TRANSACTION READ ONLY")
        self.assertIn("ROLLBACK", self.db.log)
        self.assertEqual(self.db.log[-1], "SELECT @@session.transaction_read_only")
        self.assertTrue(r["result"]["read_only_held"])
        self.assertEqual(len(r["result"]["rows"]), 3)

    def test_truncation_and_cells(self):
        self.db.rows, self.db.cell = 50, 3000
        r = self.w.handle({"op": "query", "sql": "SELECT a, b FROM t", "max_rows": 10, "max_cell": 100})["result"]
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["rows"]), 10)
        self.assertEqual(len(r["rows"][0][1]), 100)
        self.assertIn([0, 1], r["cut_cells"])
        self.assertIn("KILL QUERY 7", self.db.log)   # the rest is killed, not drained

    def test_a_console_transaction_killed_while_idle_is_lost_never_silently_reopened(self):
        """Review 2026-10-07: KILL / wait_timeout between two ⌘↩ - the ping in conn() reconnected and a
        NEW transaction answered tx_open, so ⌘⇧↩ said committed while the first UPDATE was gone."""
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 1"}]})
        self.assertTrue(r["result"]["tx_open"], r)
        self.db.dead.append(self.w.conns["write"])
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 2"}]})
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], "tx_lost")
        r = self.w.handle({"op": "tx_commit"})
        self.assertFalse(r["result"]["committed"], r)   # nothing to commit: never "committed"
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 3"}]})
        self.assertTrue(r["ok"] and r["result"]["tx_open"], r)   # and the next one starts afresh

    def test_a_mysql_connection_lost_mid_statement_ends_the_transaction(self):
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 1"}]})
        self.assertTrue(r["result"]["tx_open"], r)
        e = RuntimeError(2013, "Lost connection to MySQL server during query")
        self.assertTrue(self.w.drv.lost(e))
        self.assertTrue(self.w.drv.lost(RuntimeError(2006, "MySQL server has gone away")))
        self.assertFalse(self.w.drv.lost(RuntimeError(1146, "Table 'x' doesn't exist")))

    def test_a_cancel_and_a_timeout_say_which_one_without_tool_names(self):
        # B-N3-2: the person clicked Cancel and read "cancelled (timeout or db_cancel)"; the agent still learns
        # whether its own cancel or the time limit stopped the statement (code "cancelled" either way)
        killed = threading.Event()
        orig_exec, orig_kill = FakeCursor.execute, FakeDriver.kill

        def execute(cur, sql, params=None, prepare=None):
            if "SLOW" in sql:
                killed.wait(10)
                raise RuntimeError("Query execution was interrupted")
            return orig_exec(cur, sql, params, prepare)

        def kill(drv, ctrl, tid):
            killed.set()
            return orig_kill(drv, ctrl, tid)
        FakeCursor.execute, FakeDriver.kill = execute, kill
        self.addCleanup(setattr, FakeCursor, "execute", orig_exec)
        self.addCleanup(setattr, FakeDriver, "kill", orig_kill)
        for how in ("cancel", "timeout"):
            killed.clear()
            res = {}
            req = {"id": "slow", "op": "query", "sql": "SELECT SLOW", "timeout_s": 60 if how == "cancel" else 1}
            th = threading.Thread(target=lambda: res.update(self.w.handle(req)))
            th.start()
            if how == "cancel":
                for _ in range(100):
                    if "slow" in self.w.running:
                        break
                    time.sleep(0.02)
                self.w.handle({"id": "c", "op": "cancel"})
            th.join(15)
            self.assertEqual(res.get("code"), "cancelled", res)
            self.assertNotIn("db_cancel", res["error"])
            if how == "cancel":
                self.assertRegex(res["error"], r"^cancelled after \d+\.\d s$")
            else:
                self.assertRegex(res["error"], r"^cancelled at the time limit after \d+\.\d s$")

    def test_escape_detected(self):
        self.db.escaped = True
        r = self.w.handle({"op": "query", "sql": "SELECT 1"})["result"]
        self.assertFalse(r["read_only_held"])
        self.assertNotIn("read", self.w.conns)   # the connection is dropped

    def test_error_scrubbed(self):
        r = self.w.handle({"op": "query", "sql": "SELECT FAIL"})
        self.assertFalse(r["ok"])
        self.assertNotIn("secret-pw", r["error"])

    def test_an_error_without_text_still_says_what_happened(self):
        # pytds on a closed port: TimeoutError("") after login_timeout - the GUI showed "cannot connect: failed"
        for exc, want in ((TimeoutError(), "no answer from the server"), (RuntimeError(), "RuntimeError")):
            self.w.op_boom = lambda req, rid, e=exc: (_ for _ in ()).throw(e)
            r = self.w.handle({"op": "boom"})
            self.assertFalse(r["ok"])
            self.assertIn(want, r["error"])

    def test_execute_preview_rolls_back(self):
        self.db.log.clear()
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a=1"}], "preview": True})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["results"][0]["rowcount"], 2)
        self.assertNotIn("COMMIT", self.db.log)
        self.assertIn("ROLLBACK", self.db.log)
        self.assertFalse(r["result"]["committed"])

    def test_execute_commit_and_drift(self):
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a=1"}], "expect": [2]})
        self.assertTrue(r["result"]["committed"])
        self.db.log.clear()
        self.db.affected = 9
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a=1"}], "expect": [2]})
        self.assertEqual(r["code"], "drift")
        self.assertNotIn("COMMIT", self.db.log)

    def test_failed_statement_rolls_back_all(self):
        self.db.log.clear()
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a=1"}, {"sql": "UPDATE FAIL"}]})
        self.assertFalse(r["ok"])
        self.assertIn("statement 2", r["error"])
        self.assertNotIn("COMMIT", self.db.log)
        self.assertNotIn("secret-pw", r["error"])

    def test_commit_interrupted_is_unknown(self):
        self.db.commit_fails = True
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a=1"}]})
        self.assertEqual(r["code"], "outcome_unknown")

    def test_serve_protocol(self):
        out = io.StringIO()
        w = W.Worker(drivers={"mysql": FakeDriver}, out=out)
        inp = io.StringIO("\n".join(json.dumps(x) for x in [
            {"id": "1", "op": "ping"}, {"id": "2", "op": "open", "spec": {"engine": "nope"}}]) + "\n")
        w.serve(inp)
        time.sleep(0.2)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual({x["id"] for x in lines}, {"1", "2"})
        self.assertEqual([x for x in lines if x["id"] == "2"][0]["code"], "engine")


class OracleMssqlDrivers(unittest.TestCase):
    """The Oracle / SQL Server drivers' own discipline without a server (no image here)."""

    def test_pyformat_to_numeric(self):
        self.assertEqual(W._pyformat_to_numeric("UPDATE t SET a = %s, b = '%s' WHERE id = %s"),
                         "UPDATE t SET a = :1, b = '%s' WHERE id = :2")

    def _oracle(self):
        log = []

        class Cur:
            description, rowcount, arraysize = None, 1, 0

            def execute(self, sql, params=None):
                log.append((sql, params))

            def fetchone(self):
                return ("FREE", "host", "FREEPDB1", "APP")

            def close(self):
                pass

        class Conn:
            version, autocommit, module = "23.0", True, ""

            def cursor(self):
                return Cur()

            def rollback(self):
                log.append(("ROLLBACK", None))

            def commit(self):
                log.append(("COMMIT", None))

            def cancel(self):
                log.append(("CANCEL", None))

            def close(self):
                pass

        class Mod:
            class defaults:
                fetch_lobs = True

            @staticmethod
            def makedsn(host, port, service_name):
                return "%s:%s/%s" % (host, port, service_name)

            @staticmethod
            def connect(**kw):
                return Conn()

        class Ora(W.Oracle):
            def __init__(self, spec, mod=None):
                W.Oracle.__init__(self, spec, Mod)
        w = W.Worker(drivers={"oracle": Ora}, out=io.StringIO())
        self.assertTrue(w.handle({"op": "open", "spec": {"engine": "oracle"}})["ok"])
        self.assertFalse(Mod.defaults.fetch_lobs)
        return w, log

    def test_oracle_read_transaction_and_semicolon(self):
        w, log = self._oracle()
        log.clear()
        r = w.handle({"op": "query", "sql": "SELECT 1 FROM dual;", "schema": "APP"})
        self.assertTrue(r["ok"], r)
        sqls = [x[0] for x in log]
        self.assertLess(sqls.index('ALTER SESSION SET CURRENT_SCHEMA = "APP"'), sqls.index("SET TRANSACTION READ ONLY"))
        self.assertIn(("SELECT 1 FROM dual", []), log)            # the trailing ; stripped
        self.assertEqual(sqls[-1], "ROLLBACK")
        log.clear()
        w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a = %s WHERE id = %s", "params": [1, 2]},
                                                  {"sql": "BEGIN p(1); END;"}]})
        self.assertIn(("UPDATE t SET a = :1 WHERE id = :2", [1, 2]), log)
        self.assertIn(("BEGIN p(1); END;", []), log)              # PL/SQL keeps its ;
        self.assertIn(("COMMIT", None), log)

    def test_oracle_a_lower_case_schema_is_the_upper_case_user(self):
        # live Oracle 23: `UPDATE shop.customer` / schema "shop" from an agent named nobody - owners are upper case
        w, log = self._oracle()
        log.clear()
        self.assertTrue(w.handle({"op": "query", "sql": "SELECT 1 FROM dual", "schema": "shop"})["ok"])
        self.assertIn(('ALTER SESSION SET CURRENT_SCHEMA = "SHOP"', None), log)
        log.clear()
        w.handle({"op": "meta", "what": "objects", "schema": "shop"})
        self.assertIn(["SHOP"], [x[1] for x in log])
        self.assertEqual(W.Oracle.owner("MixedCase"), "MixedCase")   # a quoted name stays as written


class ErrorText(unittest.TestCase):
    def test_a_driver_error_with_a_none_detail_says_only_its_message(self):
        # live SQL Server (pytds): "Not saved: statement 1 failed: ('The DELETE statement conflicted ...', None)"
        w = W.Worker(out=io.StringIO())
        e = RuntimeError("The DELETE statement conflicted with the REFERENCE constraint", None)
        self.assertEqual(w.scrub(e), "The DELETE statement conflicted with the REFERENCE constraint")
        self.assertEqual(w.scrub(RuntimeError("a", "b")), "('a', 'b')")   # a real second part stays


class LostConnection(unittest.TestCase):
    """Live Oracle / SQL Server: a session the server killed (DBA, idle limit, restart) failed every later
    call (DPY-1001 / Invalid TDS marker) - oracledb thin and pytds have no socket to look at beforehand."""

    def _worker(self, err):
        state = {"connects": 0, "dead": set(), "log": []}

        class Cur:
            description, rowcount = None, 1

            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, params=None):
                if self.conn.n in state["dead"]:
                    raise RuntimeError(err)
                state["log"].append((self.conn.n, sql))
                if sql.startswith("SELECT"):
                    self.description = [("a", 1)]

            def fetchone(self):
                return ("x", "h", "c", "s")

            def fetchmany(self, n):
                return [(1,)]

            def fetchall(self):
                return [("t", "table")]

            def close(self):
                pass

        class Conn:
            version, autocommit, module = "23", False, ""

            def __init__(self):
                state["connects"] += 1
                self.n = state["connects"]

            def cursor(self):
                return Cur(self)

            def rollback(self):
                if self.n in state["dead"]:
                    raise RuntimeError(err)

            def commit(self):
                pass

            def cancel(self):
                pass

            def close(self):
                pass

        class Mod:
            class defaults:
                fetch_lobs = True

            @staticmethod
            def makedsn(host, port, service_name):
                return ""

            @staticmethod
            def connect(**kw):
                return Conn()

        class Ora(W.Oracle):
            def __init__(self, spec, mod=None):
                W.Oracle.__init__(self, spec, Mod)
        w = W.Worker(drivers={"oracle": Ora}, out=io.StringIO())
        self.assertTrue(w.handle({"op": "open", "spec": {"engine": "oracle", "user": "app"}})["ok"])
        return w, state

    def test_a_read_after_the_server_killed_the_session_runs_on_a_new_connection(self):
        w, st = self._worker("DPY-4011: the database or network closed the connection")
        for op in ({"op": "query", "sql": "SELECT 1 FROM dual"}, {"op": "meta", "what": "objects"}):
            st["dead"].add(st["connects"])            # the server kills the open read session
            before = st["connects"]
            r = w.handle(op)
            self.assertTrue(r["ok"], (op, r))
            self.assertEqual(st["connects"], before + 1)
            r = w.handle(op)
            self.assertTrue(r["ok"], r)                # and keeps working (no DPY-1001 forever)
            self.assertEqual(st["connects"], before + 1)

    def test_a_lost_read_never_drops_the_fresh_connection_another_request_made(self):
        """Review 2026-10-07: reads run in parallel - B, failing on the dead connection after A had already
        reconnected, dropped A's new one under it and A's single retry failed too."""
        w, st = self._worker("DPY-4011: the database or network closed the connection")
        self.assertTrue(w.handle({"op": "query", "sql": "SELECT 1 FROM dual"})["ok"])
        dead = w.conns["read"]                     # this thread's last connection
        fresh = w.drv.connect("read")
        w.conns["read"] = fresh                    # another request reconnected meanwhile
        self.assertTrue(w._lost(RuntimeError("DPY-4011: closed"), "read"))
        self.assertIs(w.conns.get("read"), fresh)  # kept
        w.conns["read"] = dead
        self.assertTrue(w._lost(RuntimeError("DPY-4011: closed"), "read"))
        self.assertNotIn("read", w.conns)          # its own dead one is dropped

    def test_any_other_error_is_not_retried(self):
        w, st = self._worker("ORA-00942: table or view does not exist")
        st["dead"].add(st["connects"])
        r = w.handle({"op": "query", "sql": "SELECT 1 FROM nope"})
        self.assertFalse(r["ok"])
        self.assertIn("ORA-00942", r["error"])
        self.assertEqual(st["connects"], 1)

    def test_a_write_on_a_killed_session_starts_on_a_new_connection(self):
        w, st = self._worker("ORA-00028: your session has been killed")
        self.assertTrue(w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a = 1 WHERE id = 1"}]})["ok"])
        st["dead"].add(st["connects"])
        r = w.handle({"op": "execute", "statements": [{"sql": "UPDATE t SET a = 2 WHERE id = 1"}]})
        self.assertTrue(r["ok"], r)                    # dead before anything ran: a new connection, the write runs once
        self.assertEqual([x for x in st["log"] if x[1].startswith("UPDATE")][-1], (st["connects"], "UPDATE t SET a = 2 WHERE id = 1"))

    def test_an_open_console_transaction_lost_says_so_then_the_next_one_works(self):
        w, st = self._worker("DPY-4011: the database or network closed the connection")
        r = w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 1 WHERE id = 1"}]})
        self.assertTrue(r["result"]["tx_open"], r)
        st["dead"].add(st["connects"])
        r = w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 2 WHERE id = 1"}]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "tx_lost", r)       # never papered over: the first change is gone
        self.assertFalse(w.tx_open)
        r = w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE t SET a = 3 WHERE id = 1"}]})
        self.assertTrue(r["ok"], r)


E2E = os.environ.get("PIPER_DB_E2E_MYSQL", "")


@unittest.skipUnless(E2E, "PIPER_DB_E2E_MYSQL=host:port:user:password:db not set")
class LiveMySQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host, port, user, pw, db = E2E.split(":")
        cls.spec = {"engine": "mysql", "host": host, "port": int(port), "user": user, "password": pw, "database": db}
        import pymysql
        c = pymysql.connect(host=host, port=int(port), user=user, password=pw, database=db, autocommit=True)
        k = c.cursor()
        for s in ("DROP TABLE IF EXISTS w_inno", "DROP TABLE IF EXISTS w_mem", "DROP TABLE IF EXISTS w_big",
                  "CREATE TABLE w_inno (id INT PRIMARY KEY, x INT) ENGINE=InnoDB", "INSERT INTO w_inno VALUES (1,0),(2,0)",
                  "CREATE TABLE w_mem (x INT) ENGINE=MEMORY",
                  "CREATE TABLE w_big (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(200))"):
            k.execute(s)
        k.execute("INSERT INTO w_big (pad) VALUES " + ",".join(["('x')"] * 1000))
        for _ in range(7):
            k.execute("INSERT INTO w_big (pad) SELECT pad FROM w_big")
        cls.raw = c

    def setUp(self):
        self.w = W.Worker(out=io.StringIO())
        r = self.w.handle({"op": "open", "spec": self.spec})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["read_only"])

    def tearDown(self):
        for m in list(self.w.conns):
            self.w.drop(m)

    def x(self, sql):
        k = self.raw.cursor()
        k.execute(sql)
        return k.fetchall()

    def test_a_console_transaction_killed_between_statements_is_lost_never_reopened(self):
        """Review 2026-10-07: KILL between two ⌘↩ - the ping reconnected, a new transaction answered
        tx_open and ⌘⇧↩ said committed, the first UPDATE silently gone."""
        self.x("UPDATE w_inno SET x = 0 WHERE id = 1")
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE w_inno SET x = 1 WHERE id = 1"}]})
        self.assertTrue(r["result"]["tx_open"], r)
        self.x("KILL %d" % self.w.conns["write"].thread_id())
        time.sleep(0.3)
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE w_inno SET x = x + 10 WHERE id = 1"}]})
        self.assertEqual(r.get("code"), "tx_lost", r)
        self.assertFalse(self.w.handle({"op": "tx_commit"})["result"]["committed"])
        self.assertEqual(self.x("SELECT x FROM w_inno WHERE id = 1")[0][0], 0)

    def test_bit_is_a_number(self):
        # a boolean column is BIT(1): the grid showed 0x00 / 0x01
        self.x("DROP TABLE IF EXISTS w_bit")
        self.x("CREATE TABLE w_bit (id INT PRIMARY KEY, f BIT(1), m BIT(10))")
        self.x("INSERT INTO w_bit VALUES (1, b'0', b'0000000101'), (2, b'1', NULL)")
        try:
            r = self.w.handle({"op": "query", "sql": "SELECT f, m FROM w_bit ORDER BY id"})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["result"]["rows"], [[0, 5], [1, None]])
        finally:
            self.x("DROP TABLE w_bit")

    def test_write_through_read_fails(self):
        for sql in ("UPDATE w_inno SET x=5", "CREATE TABLE w_never (a INT)", "INSERT INTO w_mem VALUES (1)"):
            r = self.w.handle({"op": "query", "sql": sql})
            self.assertFalse(r["ok"], sql)
            self.assertEqual(r["code"], "1792", r)
        self.assertEqual(self.x("SELECT SUM(x) FROM w_inno")[0][0], 0)
        self.assertEqual(self.x("SELECT COUNT(*) FROM w_mem")[0][0], 0)

    def test_preview_then_commit(self):
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE w_inno SET x=x+1"}], "preview": True})
        self.assertEqual(r["result"]["results"][0]["rowcount"], 2)
        self.assertEqual(self.x("SELECT SUM(x) FROM w_inno")[0][0], 0)
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE w_inno SET x=x+1"}], "expect": [2]})
        self.assertTrue(r["result"]["committed"], r)
        self.assertEqual(self.x("SELECT SUM(x) FROM w_inno")[0][0], 2)
        self.x("UPDATE w_inno SET x=0")

    def test_kill_long_query(self):
        t0 = time.time()
        res = {}
        th = threading.Thread(target=lambda: res.update(self.w.handle(
            {"id": "slow", "op": "query", "sql": "SELECT SLEEP(30), (SELECT COUNT(*) FROM w_big)", "timeout_s": 60})))
        th.start()
        time.sleep(1)
        self.w.handle({"id": "c", "op": "cancel"})
        th.join(15)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(res.get("code"), "cancelled", res)

    def test_timeout(self):
        t0 = time.time()
        for sql in ("SELECT BENCHMARK(2000000000, MD5('x'))", "SELECT SLEEP(20)"):
            t0 = time.time()
            r = self.w.handle({"op": "query", "sql": sql, "timeout_s": 2})
            self.assertLess(time.time() - t0, 10)
            self.assertFalse(r["ok"], sql)
            self.assertEqual(r["code"], "cancelled", r)

    def test_big_result_cut(self):
        t0 = time.time()
        r = self.w.handle({"op": "query", "sql": "SELECT a.id, b.pad FROM w_big a, w_big b", "max_rows": 100})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["truncated"])
        self.assertEqual(len(r["result"]["rows"]), 100)
        self.assertLess(time.time() - t0, 10)
        r = self.w.handle({"op": "query", "sql": "SELECT COUNT(*) FROM w_inno"})   # the connection still works
        self.assertTrue(r["ok"], r)

    def test_meta(self):
        r = self.w.handle({"op": "meta", "what": "engines"})["result"]["rows"]
        eng = {t: e for _, t, e in r}
        self.assertEqual(eng["w_inno"], "InnoDB")
        self.assertEqual(eng["w_mem"], "MEMORY")
        cols = self.w.handle({"op": "meta", "what": "columns", "name": "w_inno"})["result"]["rows"]
        self.assertEqual(cols[0][0], "id")
        self.assertEqual(cols[0][3], "PRI")
        ddl = self.w.handle({"op": "meta", "what": "ddl", "name": "w_inno"})["result"]["rows"][0][0]
        self.assertIn("CREATE TABLE", ddl)

    def test_schema_does_not_stick(self):
        """Review finding: a USE of one request must not leak into the next one without a schema."""
        self.raw.cursor().execute("CREATE DATABASE IF NOT EXISTS e2e_other")
        r = self.w.handle({"op": "query", "sql": "SELECT DATABASE()", "schema": "e2e_other"})
        self.assertEqual(r["result"]["rows"], [["e2e_other"]])
        r = self.w.handle({"op": "query", "sql": "SELECT DATABASE()"})
        self.assertEqual(r["result"]["rows"], [["e2e"]])

    def test_cancel_only_named_requests(self):
        res = {}
        th = threading.Thread(target=lambda: res.update(self.w.handle(
            {"id": "mine", "op": "query", "sql": "SELECT SLEEP(3)", "timeout_s": 60})))
        th.start()
        time.sleep(0.5)
        self.assertEqual(self.w.handle({"id": "c1", "op": "cancel", "ids": ["someone-else"]})["result"]["cancelled"], 0)
        th.join(10)
        self.assertTrue(res.get("ok"), res)   # not cancelled

    def test_no_multi_statements(self):
        r = self.w.handle({"op": "query", "sql": "SELECT 1; SELECT 2"})
        self.assertFalse(r["ok"])


PG = os.environ.get("PIPER_DB_E2E_PG", "")


@unittest.skipUnless(PG, "PIPER_DB_E2E_PG=host:port:user:password:db not set")
class LivePostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host, port, user, pw, db = PG.split(":")
        cls.spec = {"engine": "postgres", "host": host, "port": int(port), "user": user, "password": pw, "database": db}
        import psycopg
        cls.raw = psycopg.connect(host=host, port=int(port), user=user, password=pw, dbname=db, autocommit=True)
        for s in ("DROP TABLE IF EXISTS p_item", "CREATE TABLE p_item (id INT PRIMARY KEY, x INT, email TEXT)",
                  "INSERT INTO p_item VALUES (1, 0, 'a@x'), (2, 0, 'b@x')"):
            cls.raw.execute(s)

    def setUp(self):
        self.w = W.Worker(out=io.StringIO())
        r = self.w.handle({"op": "open", "spec": self.spec})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["read_only"])
        self.assertTrue(r["result"]["server_id"])

    def tearDown(self):
        for m in list(self.w.conns):
            self.w.drop(m)

    def test_read_and_refused_writes(self):
        r = self.w.handle({"op": "query", "sql": "SELECT id, x FROM p_item ORDER BY id"})
        self.assertEqual(r["result"]["rows"], [[1, 0], [2, 0]], r)
        for sql in ("UPDATE p_item SET x = 5", "CREATE TABLE p_never (a int)"):
            r = self.w.handle({"op": "query", "sql": sql, "unbuffered": False})
            self.assertFalse(r["ok"], sql)
            self.assertIn("read-only", r["error"])
        # simple-query protocol would run two statements; prepare=True refuses (13.1 #6)
        r = self.w.handle({"op": "query", "sql": "SELECT 1; SET default_transaction_read_only = off", "unbuffered": False})
        self.assertFalse(r["ok"])
        self.assertEqual(self.raw.execute("SELECT SUM(x) FROM p_item").fetchone()[0], 0)

    def test_a_connection_the_server_dropped_is_replaced_before_the_next_read(self):
        # MySQL pings before each use; PostgreSQL must not fail the first read after a restart / idle kill
        for op in ({"op": "query", "sql": "SELECT 1"}, {"op": "meta", "what": "schemas"}):
            self.assertTrue(self.w.handle(op)["ok"])
            pid = self.w.conns["read"].info.backend_pid
            self.raw.execute("SELECT pg_terminate_backend(%s)", (pid,))
            time.sleep(0.3)
            r = self.w.handle(op)
            self.assertTrue(r["ok"], (op, r))
            self.assertNotEqual(self.w.conns["read"].info.backend_pid, pid)

    def test_a_console_transaction_whose_backend_was_terminated_is_lost(self):
        self.raw.execute("UPDATE p_item SET x = 0 WHERE id = 1")
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE p_item SET x = 1 WHERE id = 1"}]})
        self.assertTrue(r["result"]["tx_open"], r)
        self.raw.execute("SELECT pg_terminate_backend(%s)", (self.w.conns["write"].info.backend_pid,))
        time.sleep(0.3)
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE p_item SET x = x + 10 WHERE id = 1"}]})
        self.assertEqual(r.get("code"), "tx_lost", r)
        self.assertFalse(self.w.handle({"op": "tx_commit"})["result"]["committed"])
        self.assertEqual(self.raw.execute("SELECT x FROM p_item WHERE id = 1").fetchone()[0], 0)

    def test_show_and_explain(self):
        for sql in ("SHOW search_path", "EXPLAIN SELECT * FROM p_item"):
            r = self.w.handle({"op": "query", "sql": sql})
            self.assertTrue(r["ok"], (sql, r))
        r = self.w.handle({"op": "explain", "sql": "UPDATE p_item SET x = 1"})
        self.assertTrue(r["ok"], r)   # PostgreSQL allows EXPLAIN of DML in a read-only transaction

    def test_params_and_big_result(self):
        r = self.w.handle({"op": "query", "sql": "SELECT id FROM p_item WHERE id = %s", "params": [2]})
        self.assertEqual(r["result"]["rows"], [[2]], r)
        r = self.w.handle({"op": "query", "sql": "SELECT g FROM generate_series(1, 200000) g", "max_rows": 10})
        self.assertTrue(r["result"]["truncated"], r)

    def test_preview_commit_and_timeout(self):
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE p_item SET x = x + 1"}], "preview": True})
        self.assertEqual(r["result"]["results"][0]["rowcount"], 2, r)
        self.assertEqual(self.raw.execute("SELECT SUM(x) FROM p_item").fetchone()[0], 0)
        r = self.w.handle({"op": "execute", "statements": [{"sql": "CREATE TABLE p_ddl (a int)"}], "preview": True})
        self.assertTrue(r["ok"], r)   # transactional DDL: previewable, rolled back
        self.assertIsNone(self.raw.execute("SELECT to_regclass('p_ddl')").fetchone()[0])
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE p_item SET x = x + 1"}], "expect": [2]})
        self.assertTrue(r["result"]["committed"], r)
        self.assertEqual(self.raw.execute("SELECT SUM(x) FROM p_item").fetchone()[0], 2)
        self.raw.execute("UPDATE p_item SET x = 0")
        t0 = time.time()
        r = self.w.handle({"op": "query", "sql": "SELECT pg_sleep(20)", "timeout_s": 2})
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(r.get("code"), "cancelled", r)

    def test_failed_sample_keeps_the_held_transaction(self):
        """Review finding: a sample SELECT that fails (an alias) must not abort PostgreSQL's open transaction."""
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE p_item SET x = 7 WHERE id = 1"}]})
        self.assertTrue(r["ok"], r)
        bad = {"select": "SELECT * FROM p_item WHERE o.id = 2\nLIMIT 20", "table": '"p_item"', "pk": ['"id"'], "pk_names": ["id"], "update": True}
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "UPDATE p_item o SET x = 8 WHERE o.id = 2"}], "sample": bad})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["tx_open"])
        self.assertNotIn("sample", r["result"])
        r = self.w.handle({"op": "tx_exec", "statements": [{"sql": "SELECT x FROM p_item ORDER BY id"}]})
        self.assertEqual(r["result"]["results"][0]["rows"], [[7], [8]])   # both changes still in the transaction
        self.w.handle({"op": "tx_rollback"})
        self.assertEqual(self.raw.execute("SELECT SUM(x) FROM p_item").fetchone()[0], 0)

    def test_meta(self):
        cols = self.w.handle({"op": "meta", "what": "columns", "name": "p_item"})
        self.assertEqual(cols["result"]["rows"][0][0], "id", cols)
        self.assertEqual(cols["result"]["rows"][0][3], "PRI")
        objs = self.w.handle({"op": "meta", "what": "objects"})["result"]["rows"]
        self.assertIn("p_item", [o[0] for o in objs])
        ddl = self.w.handle({"op": "meta", "what": "ddl", "name": "p_item"})["result"]["rows"][0][0]
        self.assertIn("CREATE TABLE", ddl)



ORA = os.environ.get("PIPER_DB_E2E_ORACLE", "")      # host:port:user:password:service (gvenzl/oracle-free: FREEPDB1)
ORA_SYS = os.environ.get("PIPER_DB_E2E_ORACLE_SYS", "")   # system:password - only for killing the worker's session


@unittest.skipUnless(ORA, "PIPER_DB_E2E_ORACLE=host:port:user:password:service not set")
class LiveOracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host, port, user, pw, svc = ORA.split(":", 4)
        cls.spec = {"engine": "oracle", "host": host, "port": int(port), "user": user, "password": pw, "database": svc}
        import oracledb
        cls.dsn = oracledb.makedsn(host, int(port), service_name=svc)
        cls.raw = oracledb.connect(user=user, password=pw, dsn=cls.dsn)
        cls.raw.autocommit = True
        k = cls.raw.cursor()
        for s in ("DROP TABLE IF EXISTS o_item", "CREATE TABLE o_item (id NUMBER PRIMARY KEY, x NUMBER, email VARCHAR2(80), nick VARCHAR2(10 CHAR))",
                  "INSERT INTO o_item (id, x, email) VALUES (1, 0, 'a@x')", "INSERT INTO o_item (id, x, email) VALUES (2, 0, 'b@x')"):
            k.execute(s)
        time.sleep(4)   # a READ ONLY transaction cannot read a table younger than its snapshot (ORA-01466)

    def setUp(self):
        self.w = W.Worker(out=io.StringIO())
        r = self.w.handle({"op": "open", "spec": self.spec})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["result"]["server_id"])

    def tearDown(self):
        for m in list(self.w.conns):
            self.w.drop(m)

    def x(self, sql):
        k = self.raw.cursor()
        k.execute(sql)
        return k.fetchall() if k.description else None

    def test_read_and_refused_writes(self):
        r = self.w.handle({"op": "query", "sql": "SELECT id, x FROM o_item ORDER BY id"})
        self.assertEqual(r["result"]["rows"], [[1, 0], [2, 0]], r)
        r = self.w.handle({"op": "query", "sql": "UPDATE o_item SET x = 5"})
        self.assertFalse(r["ok"])
        self.assertIn("ORA-01456", r["error"])           # READ ONLY transaction
        self.assertEqual(self.x("SELECT SUM(x) FROM o_item")[0][0], 0)

    def test_a_lower_case_schema_is_the_user(self):
        me = self.spec["user"].lower()
        r = self.w.handle({"op": "query", "sql": "SELECT COUNT(*) FROM o_item", "schema": me})
        self.assertTrue(r["ok"], r)
        objs = self.w.handle({"op": "meta", "what": "objects", "schema": me})["result"]["rows"]
        self.assertIn("O_ITEM", [o[0] for o in objs])
        eng = self.w.handle({"op": "meta", "what": "engines", "schema": me})["result"]["rows"]
        self.assertIn("O_ITEM", [e[1] for e in eng])      # dbtools' preview gate reads this

    @unittest.skipUnless(ORA_SYS, "PIPER_DB_E2E_ORACLE_SYS=system:password not set")
    def test_a_session_the_server_killed_is_replaced(self):
        import oracledb
        u, p = ORA_SYS.split(":", 1)
        sysc = oracledb.connect(user=u, password=p, dsn=self.dsn)
        sysc.autocommit = True
        for op in ({"op": "query", "sql": "SELECT 1 FROM dual"}, {"op": "meta", "what": "schemas"}):
            self.assertTrue(self.w.handle(op)["ok"])
            k = sysc.cursor()
            k.execute("SELECT sid, serial# FROM v$session WHERE module = 'piper-read'")
            for sid, ser in k.fetchall():
                k.execute("ALTER SYSTEM KILL SESSION '%d,%d' IMMEDIATE" % (sid, ser))
            time.sleep(0.5)
            r = self.w.handle(op)
            self.assertTrue(r["ok"], (op, r))
            self.assertTrue(self.w.handle(op)["ok"])
        sysc.close()

    def test_preview_commit_and_timeout(self):
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE o_item SET x = x + 1"}], "preview": True})
        self.assertEqual(r["result"]["results"][0]["rowcount"], 2, r)
        self.assertEqual(self.x("SELECT SUM(x) FROM o_item")[0][0], 0)
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE o_item SET x = x + 1"}], "expect": [2]})
        self.assertTrue(r["result"]["committed"], r)
        self.assertEqual(self.x("SELECT SUM(x) FROM o_item")[0][0], 2)
        self.x("UPDATE o_item SET x = 0")
        t0 = time.time()
        r = self.w.handle({"op": "query", "sql": "SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c",
                           "timeout_s": 2})
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.get("code"), "cancelled", r)
        self.assertTrue(self.w.handle({"op": "query", "sql": "SELECT 1 FROM dual"})["ok"])   # still usable

    def test_meta_and_big_cell(self):
        cols = self.w.handle({"op": "meta", "what": "columns", "name": "O_ITEM"})["result"]["rows"]
        self.assertEqual((cols[0][0], cols[0][3]), ("ID", "PRI"))
        types = {c[0]: c[1] for c in cols}
        self.assertEqual(types["EMAIL"], "VARCHAR2(80)")
        self.assertEqual(types["NICK"], "VARCHAR2(10 CHAR)")   # was VARCHAR2(40): bytes of a 10-character column
        r = self.w.handle({"op": "query", "sql": "SELECT TO_CLOB(RPAD('x', 4000, 'x')) || RPAD('y', 4000, 'y') FROM dual"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["result"]["rows"][0][0]), 8000)   # a CLOB comes back as text, not a locator


MSSQL = os.environ.get("PIPER_DB_E2E_MSSQL", "")    # host:port:user:password:database


@unittest.skipUnless(MSSQL, "PIPER_DB_E2E_MSSQL=host:port:user:password:database not set")
class LiveMSSQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        host, port, user, pw, db = MSSQL.split(":", 4)
        cls.spec = {"engine": "mssql", "host": host, "port": int(port), "user": user, "password": pw, "database": db}
        import pytds
        cls.raw = pytds.connect(dsn=host, port=int(port), user=user, password=pw, database=db, autocommit=True,
                                bytes_to_unicode=True)
        k = cls.raw.cursor()
        for s in ("DROP TABLE IF EXISTS m_child", "DROP TABLE IF EXISTS m_item", "CREATE TABLE m_item (id INT PRIMARY KEY, x INT, email NVARCHAR(80))",
                  "INSERT INTO m_item VALUES (1, 0, N'a@x'), (2, 0, N'b@x')"):
            k.execute(s)

    def setUp(self):
        self.w = W.Worker(out=io.StringIO())
        r = self.w.handle({"op": "open", "spec": self.spec})
        self.assertTrue(r["ok"], r)

    def tearDown(self):
        for m in list(self.w.conns):
            self.w.drop(m)

    def x(self, sql):
        k = self.raw.cursor()
        k.execute(sql)
        return k.fetchall() if k.description else None

    def test_read_is_rolled_back(self):
        r = self.w.handle({"op": "query", "sql": "SELECT id, x FROM m_item ORDER BY id"})
        self.assertEqual(r["result"]["rows"], [[1, 0], [2, 0]], r)
        self.w.handle({"op": "query", "sql": "UPDATE m_item SET x = 5"})   # dbsql refuses it before; here: rolled back
        self.assertEqual(self.x("SELECT SUM(x) FROM m_item")[0][0], 0)

    def test_a_locked_row_is_a_quick_clear_error_not_a_hang(self):
        import pytds
        host, port, user, pw, db = MSSQL.split(":", 4)
        holder = pytds.connect(dsn=host, port=int(port), user=user, password=pw, database=db, autocommit=False)
        try:
            holder.cursor().execute("UPDATE m_item SET x = 9 WHERE id = 1")   # an open transaction holds the row
            t0 = time.time()
            r = self.w.handle({"op": "query", "sql": "SELECT x FROM m_item WHERE id = 1", "timeout_s": 60})
            self.assertLess(time.time() - t0, 15)
            self.assertEqual(r.get("code"), "locked", r)
        finally:
            holder.rollback()
            holder.close()
        r = self.w.handle({"op": "query", "sql": "SELECT x FROM m_item WHERE id = 1"})
        self.assertEqual(r["result"]["rows"], [[0]], r)    # the same worker reads again

    def test_cancel_then_the_next_call_works(self):
        t0 = time.time()
        r = self.w.handle({"op": "query", "sql": "WAITFOR DELAY '00:00:20'; SELECT 1", "timeout_s": 2})
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.get("code"), "cancelled", r)
        for _ in range(3):   # pytds after a cancel: the stream was out of step (Invalid TDS marker)
            r = self.w.handle({"op": "query", "sql": "SELECT id FROM m_item ORDER BY id"})
            self.assertEqual(r["result"]["rows"], [[1], [2]], r)

    def test_a_session_the_server_killed_is_replaced(self):
        for op in ({"op": "query", "sql": "SELECT 1"}, {"op": "meta", "what": "schemas"}):
            self.assertTrue(self.w.handle(op)["ok"])
            for (spid,) in self.x("SELECT session_id FROM sys.dm_exec_sessions WHERE program_name = 'piper-read'"):
                self.x("KILL %d" % spid)
            time.sleep(0.5)
            r = self.w.handle(op)
            self.assertTrue(r["ok"], (op, r))
            self.assertTrue(self.w.handle(op)["ok"])
        self.assertTrue(self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE m_item SET x = 0 WHERE id = 2"}]})["ok"])
        for (spid,) in self.x("SELECT session_id FROM sys.dm_exec_sessions WHERE program_name = 'piper-write'"):
            self.x("KILL %d" % spid)
        time.sleep(0.5)
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE m_item SET x = 0 WHERE id = 2"}]})
        self.assertTrue(r["ok"], r)

    def test_preview_commit_and_meta(self):
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE m_item SET x = x + 1"}], "preview": True})
        self.assertEqual(r["result"]["results"][0]["rowcount"], 2, r)
        self.assertEqual(self.x("SELECT SUM(x) FROM m_item")[0][0], 0)
        r = self.w.handle({"op": "execute", "statements": [{"sql": "UPDATE m_item SET x = x + 1"}], "expect": [2]})
        self.assertTrue(r["result"]["committed"], r)
        self.assertEqual(self.x("SELECT SUM(x) FROM m_item")[0][0], 2)
        self.x("UPDATE m_item SET x = 0")
        cols = self.w.handle({"op": "meta", "what": "columns", "name": "m_item"})["result"]["rows"]
        self.assertEqual((cols[0][0], cols[0][3]), ("id", "PRI"))
        self.x("DROP TABLE IF EXISTS m_child")
        self.x("CREATE TABLE m_child (id INT PRIMARY KEY, item_id INT REFERENCES m_item(id), d DECIMAL(10,2), t NVARCHAR(MAX))")
        types = {c[0]: c[1] for c in self.w.handle({"op": "meta", "what": "columns", "name": "m_child"})["result"]["rows"]}
        self.assertEqual((types["d"], types["t"]), ("decimal(10,2)", "nvarchar(max)"))   # was decimal / nvarchar(-1)
        # Piper's "schema" on SQL Server is the database: an FK jump opened schema "dbo" -> "Database 'dbo' does not exist"
        fk = self.w.handle({"op": "meta", "what": "fks", "name": "m_child"})["result"]["rows"][0]
        ref = self.w.handle({"op": "meta", "what": "refs", "name": "m_item"})["result"]["rows"][0]
        self.assertEqual((fk[2], ref[0]), (self.spec["database"], self.spec["database"]))
        self.assertIn(self.spec["database"], [r[0] for r in self.w.handle({"op": "meta", "what": "schemas"})["result"]["rows"]])


if __name__ == "__main__":
    unittest.main()
