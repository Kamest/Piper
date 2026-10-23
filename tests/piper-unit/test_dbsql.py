"""SQL text rules of the DB client (dbsql.py): the read allowlist and denylist, statement kinds,
tables for the engine gate, script splitting and column masking."""
import unittest

import _env  # noqa: F401
import dbsql
from dbsql import SqlError


class ReadCheck(unittest.TestCase):
    OK = ["SELECT 1", "select * from t where a = 'x;y'", "  (SELECT 1) UNION (SELECT 2)", "WITH c AS (SELECT 1) SELECT * FROM c",
          "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<3) SELECT * FROM r",
          "SHOW TABLES", "DESCRIBE t", "desc t", "EXPLAIN SELECT 1", "EXPLAIN FORMAT=JSON SELECT 1", "TABLE t",
          "VALUES ROW(1,2)", "HELP 'select'", "SELECT 1;", "-- comment\nSELECT 1", "/* c */ SELECT 1",
          "SELECT 'INTO OUTFILE' AS x"[:0] + "SELECT 'a--b' FROM t", "SELECT `update` FROM t",
          "SELECT * FROM t WHERE note = 'it''s'", "select 1 # mysql comment"]

    def test_allowed(self):
        for sql in self.OK:
            with self.subTest(sql=sql):
                dbsql.read_check(sql)

    BAD = ["UPDATE t SET a=1", "DELETE FROM t", "INSERT INTO t VALUES (1)", "CALL p()", "SET SESSION transaction_read_only=OFF",
           "CREATE TEMPORARY TABLE x AS SELECT 1", "ANALYZE TABLE t", "CHECK TABLE t", "DO SLEEP(10)", "PREPARE s FROM 'x'",
           "HANDLER t OPEN", "LOAD DATA LOCAL INFILE 'x' INTO TABLE t", "LOCK TABLES t READ", "START TRANSACTION",
           "WITH c AS (SELECT 1 a) UPDATE t SET x=7",                    # the CTE list skipped to the real statement
           "WITH c AS (SELECT 1) DELETE FROM t",
           "SELECT 1; DROP TABLE t", "SELECT 1; SELECT 2",
           "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SELECT * INTO DUMPFILE '/tmp/x' FROM t",
           "SELECT 1--1 INTO OUTFILE '/tmp/x'",                          # `--` without a space is not a comment in MySQL
           "SELECT * FROM t FOR UPDATE", "SELECT * FROM t FOR SHARE", "SELECT * FROM t LOCK IN SHARE MODE",
           "SELECT /*+ MAX_EXECUTION_TIME(1000000) */ * FROM t", "SELECT /*!50000 SLEEP(1) */",
           "/*! UPDATE t SET a=1 */ SELECT 1", "SELECT LOAD_FILE('/etc/passwd')", "SELECT GET_LOCK('x', 10)",
           "EXPLAIN ANALYZE SELECT * FROM t", "DESCRIBE ANALYZE SELECT 1", "EXPLAIN ANALYSE SELECT 1", "SELECT * FROM t INTO/**/OUTFILE '/x'",
           "", "   ", "-- only a comment", "SET @@global.x = 1", "USE other"]

    def test_refused(self):
        for sql in self.BAD:
            with self.subTest(sql=sql):
                with self.assertRaises(SqlError):
                    dbsql.read_check(sql)

    def test_console_optimizer_hints_only_when_allowed(self):
        q = "SELECT /*+ SET_VAR(group_concat_max_len = 32768) */ GROUP_CONCAT(a) FROM t"
        with self.assertRaises(SqlError):                       # upstream: refused, even in the console
            dbsql.read_check(q, console=True)
        dbsql.read_check(q, console=True, optimizer_hints=True)
        for bad in ("SELECT /*!50000 SLEEP(1) */", "/*+ x */ SELECT 1 INTO OUTFILE '/tmp/x'",
                    "SELECT /*+ BKA(t) */ * FROM t FOR UPDATE", "SELECT /*+ x */ LOAD_FILE('/etc/passwd')"):
            with self.subTest(sql=bad), self.assertRaises(SqlError):
                dbsql.read_check(bad, console=True, optimizer_hints=True)

    def test_console_extras(self):
        with self.assertRaises(dbsql.SqlError) as cm:   # the human's console: no agent tool names (N-F3-k)
            dbsql.read_check("UPDATE t SET a = 1", console=True)
        self.assertIn("switch the console to read-write", str(cm.exception))   # orchestrator's dbsql.RW wording
        self.assertNotIn("db_execute", str(cm.exception))
        with self.assertRaises(dbsql.SqlError) as cm:
            dbsql.read_check("UPDATE t SET a = 1")
        self.assertIn("db_execute", str(cm.exception))
        dbsql.read_check("SET @id = 5", console=True)
        dbsql.read_check("SET NAMES utf8mb4", console=True)
        dbsql.read_check("USE shop", console=True)
        for sql in ("SET SESSION transaction_read_only=0", "SET autocommit=1", "SET TRANSACTION READ WRITE",
                    "SET NAMES utf8mb4, SESSION transaction_read_only = 0", "SET @a = 1, transaction_read_only = 0",
                    "SET @@session.transaction_read_only=0", "SET @a = 1, @@session.autocommit = 1"):
            with self.subTest(sql=sql), self.assertRaises(SqlError):
                dbsql.read_check(sql, console=True)

    def test_postgres(self):
        dbsql.read_check("SELECT $$a;b$$, E'x\\'y' FROM t", "postgres")
        dbsql.read_check("SELECT 1 /* outer /* inner */ still comment */", "postgres")
        for sql in ("SELECT set_config('default_transaction_read_only','off',false)", "SELECT pg_read_file('/etc/passwd')",
                    "SELECT * INTO newtab FROM t", "SELECT nextval('s')", "SELECT 1; UPDATE t SET a=1",
                    "SELECT 1 --x\n; DELETE FROM t"):
            with self.subTest(sql=sql), self.assertRaises(SqlError):
                dbsql.read_check(sql, "postgres")

    def test_unterminated(self):
        for sql in ("SELECT 'abc", "SELECT /* x", 'SELECT "x'):
            with self.subTest(sql=sql), self.assertRaises(SqlError):
                dbsql.read_check(sql)


class ConsoleWording(unittest.TestCase):
    def test_the_human_console_speaks_of_read_write_not_of_agent_tools(self):
        """L6 (2026-10-07): the read-only console's refusals point at its read-write switch."""
        cases = ["UPDATE t SET a = 1", "SELECT 1; SELECT 2", "SELECT * FROM t FOR UPDATE",
                 "EXPLAIN ANALYZE SELECT 1", "SELECT a INTO t2 FROM t"]
        for sql in cases:
            with self.subTest(sql=sql):
                with self.assertRaises(dbsql.SqlError) as human:
                    dbsql.read_check(sql, "postgres", console=True)
                self.assertIn("read-write", str(human.exception))
                self.assertNotRegex(str(human.exception), r"db_(execute|query|explain)|per call|read path")
                with self.assertRaises(dbsql.SqlError) as agent:
                    dbsql.read_check(sql, "postgres")
                self.assertNotIn("read-write", str(agent.exception))


class MssqlBatch(unittest.TestCase):
    """T-SQL needs no `;`: a write hidden behind a SELECT in one batch (review 2026-10-06)."""

    def test_read_refuses_batch(self):
        for q in ("SELECT 1 DELETE FROM users COMMIT", "SELECT 1 EXEC dbo.p", "SELECT * INTO x FROM t",
                  "SELECT 1 BEGIN TRAN UPDATE t SET a = 1", "SELECT 1 WAITFOR DELAY '00:01'"):
            with self.assertRaises(dbsql.SqlError, msg=q):
                dbsql.read_check(q, "mssql")
        self.assertEqual(dbsql.read_check("SELECT a FROM t WHERE x IN (SELECT y FROM u)", "mssql"), "SELECT")
        self.assertEqual(dbsql.read_check("SELECT TOP 5 * FROM [update]", "mssql"), "SELECT")

    def test_kind_of_batch(self):
        self.assertEqual(dbsql.kind("SELECT 1 DELETE FROM users COMMIT", "mssql"), "other")   # never a plain read
        self.assertEqual(dbsql.kind("SELECT 1 DELETE FROM users", "mysql"), "select")         # one statement there


class Kinds(unittest.TestCase):
    def test_kind(self):
        cases = {"SELECT 1": "select", "UPDATE t SET a=1": "dml", "delete from t": "dml", "INSERT INTO t VALUES(1)": "dml",
                 "REPLACE INTO t VALUES(1)": "dml", "WITH c AS (SELECT 1) UPDATE t SET a=1": "dml",
                 "CREATE TABLE x (a int)": "ddl", "ALTER TABLE t ADD c int": "ddl", "TRUNCATE t": "ddl",
                 "CALL p()": "other", "LOCK TABLES t WRITE": "other", "SET @a=1": "other"}
        for sql, k in cases.items():
            with self.subTest(sql=sql):
                self.assertEqual(dbsql.kind(sql), k)

    def test_cte_with_dml_writes(self):
        self.assertEqual(dbsql.kind("WITH d AS (DELETE FROM orders RETURNING id) SELECT count(*) FROM d", "postgres"), "dml")
        self.assertEqual(dbsql.kind("WITH x AS (SELECT 1) SELECT * FROM x", "postgres"), "select")
        self.assertEqual(dbsql.kind("WITH x AS (SELECT REPLACE(name, 'a', 'b') n FROM t) SELECT * FROM x", "mysql"), "select")
        with self.assertRaises(dbsql.SqlError):
            dbsql.read_check("SELECT pg_try_advisory_lock(42)", "postgres")   # a session lock outlives the rollback

    def test_kind_one_statement(self):
        with self.assertRaises(SqlError):
            dbsql.kind("UPDATE t SET a=1; DELETE FROM t")

    def test_where(self):
        self.assertTrue(dbsql.has_where("UPDATE t SET a=1 WHERE id=2"))
        self.assertFalse(dbsql.has_where("UPDATE t SET a=(SELECT b FROM u WHERE id=1)"))
        self.assertFalse(dbsql.has_where("DELETE FROM t -- WHERE id=1"))


class Tables(unittest.TestCase):
    def test_tables(self):
        T = dbsql.tables
        self.assertEqual(T("UPDATE product SET price=1 WHERE id=2"), [("", "product")])
        self.assertEqual(T("UPDATE LOW_PRIORITY shop.product p JOIN cat c ON c.id=p.cat SET p.x=1"),
                         [("shop", "product"), ("", "cat")])
        self.assertEqual(T("INSERT INTO `order` (a) SELECT a FROM cart WHERE x IN (SELECT y FROM z)"),
                         [("", "order"), ("", "cart"), ("", "z")])
        self.assertEqual(T("DELETE t1, t2 FROM t1 INNER JOIN t2 ON t1.id=t2.id"), [("", "t1"), ("", "t2")])
        self.assertEqual(T("DELETE FROM a USING a, b WHERE a.id=b.id"), [("", "a"), ("", "b")])
        self.assertEqual(T("WITH c AS (SELECT id FROM src) UPDATE dst SET x=1 WHERE id IN (SELECT id FROM c)"),
                         [("", "src"), ("", "dst")])
        self.assertEqual(T("REPLACE INTO t VALUES (1)"), [("", "t")])
        self.assertEqual(T("UPDATE t, u SET t.a=u.a WHERE t.id=u.id"), [("", "t"), ("", "u")])

    def test_unknown(self):
        self.assertIsNone(dbsql.tables("INSERT INTO t SELECT * FROM JSON_TABLE(@j, '$[*]' COLUMNS (a INT PATH '$')) j"))


class Split(unittest.TestCase):
    def test_split(self):
        s = "INSERT INTO t VALUES ('a;b');\n-- x; y\nUPDATE t SET a='c' /* ; */ WHERE id=1;\n\n"
        self.assertEqual(dbsql.split(s), ["INSERT INTO t VALUES ('a;b')", "-- x; y\nUPDATE t SET a='c' /* ; */ WHERE id=1"])
        with self.assertRaises(SqlError):
            dbsql.split("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //")
        self.assertEqual(dbsql.split("SELECT $$a;b$$; SELECT 2", "postgres"), ["SELECT $$a;b$$", "SELECT 2"])

    def test_oracle(self):
        s = "INSERT INTO t VALUES (1);\nBEGIN\n  UPDATE t SET a=1;\nEND;\n/\nSELECT 1 FROM dual"
        self.assertEqual(dbsql.split(s, "oracle"), ["INSERT INTO t VALUES (1)", "BEGIN\n  UPDATE t SET a=1;\nEND;",
                                                    "SELECT 1 FROM dual"])
        with self.assertRaises(SqlError):
            dbsql.split("SET SERVEROUTPUT ON\nSELECT 1 FROM dual", "oracle")


class GateHelpers(unittest.TestCase):
    def test_hint_and_qualified_calls(self):
        self.assertTrue(dbsql.has_hint("UPDATE t SET a = 1 /*!50000 + f(2) */ WHERE id = 1"))
        self.assertFalse(dbsql.has_hint("UPDATE t SET a = 1 WHERE id = 1"))
        self.assertEqual(dbsql.called("UPDATE t SET a = other.f(1), b = g(2) WHERE id = 1"), {"other.f", "g"})


class SelectItems(unittest.TestCase):
    def test_items(self):
        self.assertEqual(dbsql.select_items("SELECT id, t.name, email AS e, email x, CONCAT(a, b) AS c FROM t"),
                         ({"id", "name"}, False, {"e", "x", "c"}))
        self.assertEqual(dbsql.select_items("SELECT DISTINCT * FROM t WHERE (SELECT 1) = 1"), (set(), True, set()))
        self.assertEqual(dbsql.select_items("SELECT (SELECT email FROM u LIMIT 1) AS name FROM t")[2], {"name"})


class Masking(unittest.TestCase):
    def test_masked(self):
        for c in ("email", "customer_email", "customerEmail", "E_MAIL", "phone", "tel", "billing_tel", "password",
                  "password_hash", "passwd", "street", "iban", "birthdate", "birth_date", "api_token", "salt",
                  "card_number", "cardNumber", "authentication_string", "SECRET"):
            with self.subTest(col=c):
                self.assertTrue(dbsql.masked(c))

    def test_not_masked(self):
        for c in ("hotel_name", "card_type", "url_hash", "name", "id", "telemetry", "hotel", "title", "status",
                  "mail_template", "price", "description"):
            with self.subTest(col=c):
                self.assertFalse(dbsql.masked(c))

    def test_quote(self):
        self.assertEqual(dbsql.quote_ident("a`b"), "`a``b`")
        self.assertEqual(dbsql.quote_ident('a"b', "postgres"), '"a""b"')


if __name__ == "__main__":
    unittest.main()
