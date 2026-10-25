"""The DB client's Manager side (dbtools.py) over a real dbworker.py child with the SQLite test
driver (dbfake_sqlite.py): registry and scope, cards per class, masking, the preview gate,
idempotence, the post-approval budget, server pinning, the audit, the GUI backend and the
piperd routes."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _env
import common
import dbsql
import dbtools

HERE = os.path.dirname(os.path.abspath(__file__))
# a long, non-periodic cell (a CMS text): a cut part of it never repeats further on
LONG = "".join(__import__("hashlib").sha1(str(i).encode()).hexdigest() for i in range(225))   # 9000 chars


def mkdb(path, server="srv-1"):
    if os.path.exists(path):
        os.unlink(path)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE _server (id TEXT); INSERT INTO _server VALUES ('%s');
        CREATE TABLE customer (id INTEGER PRIMARY KEY, name TEXT, email TEXT, note TEXT);
        INSERT INTO customer VALUES (1, 'Ann', 'ann@example.com', 'short'), (2, 'Bob', 'bob@example.com', '%s');
        CREATE TABLE mem_cache (k TEXT, v TEXT); INSERT INTO mem_cache VALUES ('a', '1');
    """ % (server, LONG))
    c.commit()
    c.close()


class DbBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["PIPER_DB_NO_REAPER"] = "1"
        dbtools.BOUNDARY["sqlite"] = "transaction"   # the test double behaves like MySQL / PostgreSQL here
        os.environ.update({"PIPER_DB_PYTHON": sys.executable,
                           "PIPER_DB_TEST_DRIVERS": os.path.join(HERE, "dbfake_sqlite.py")})
        cls.m = _env.manager(cls)
        cls.root = _env.make_project("dbx", {
            "sql/fix.sql": "UPDATE customer SET name='A' WHERE id=1;\nUPDATE customer SET name='B' WHERE id=2;\n",
            "src/A.java": "class A {}\n"})
        for a in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"]):
            subprocess.run(["git"] + a, cwd=cls.root, capture_output=True)   # a second class: nothing to commit
        cls.key = "team-x/dbx"
        _env.register(cls.key, cls.root)
        cls.other_root = _env.make_project("dbo", {"src/B.java": "class B {}\n"})
        _env.register("team-x/dbo", cls.other_root)
        cls.dbs = {n: os.path.join(_env.ROOT, "%s.sqlite" % n) for n in ("dev", "test", "prod", "other")}

    @classmethod
    def tearDownClass(cls):
        for w in list(cls.m._db_workers.values()):
            w.kill()
        for k in ("PIPER_DB_PYTHON", "PIPER_DB_TEST_DRIVERS"):
            os.environ.pop(k, None)

    def setUp(self):
        for n, p in self.dbs.items():
            mkdb(p)
        conns = [
            {"name": self.key + "/local", "project": self.key, "cls": "dev", "engine": "sqlite", "host": "localhost",
             "port": 1, "database": self.dbs["dev"], "user": "u"},
            {"name": self.key + "/test", "project": self.key, "cls": "test", "engine": "sqlite", "host": "test-db",
             "port": 1, "database": self.dbs["test"], "user": "u"},
            {"name": self.key + "/prod", "project": self.key, "cls": "prod", "engine": "sqlite", "host": "prod-db",
             "port": 1, "database": self.dbs["prod"], "user": "u"},
            {"name": "team-x/dbo/test", "project": "team-x/dbo", "cls": "test", "engine": "sqlite", "host": "o",
             "port": 1, "database": self.dbs["other"], "user": "u"},
        ]
        os.makedirs(dbtools.DB_DIR, exist_ok=True)
        common.write_json(dbtools.CONN_FILE, {"connections": conns, "pins": {}, "always": []})
        for c in conns:
            if c["cls"] != "dev":
                dbtools.write_secret(c["name"], "pw-" + c["cls"])
        if os.path.exists(dbtools.CONFIG_FILE):
            os.unlink(dbtools.CONFIG_FILE)
        if os.path.exists(dbtools.AUDIT_FILE):
            os.unlink(dbtools.AUDIT_FILE)
        for w in list(self.m._db_workers.values()):
            w.kill()
        self.m._db_workers.clear()
        self.m._db_ok.clear()
        self.m._db_recent.clear()
        self.m._db_meta.clear()
        self.asked, self.answer, self.always = [], True, False

        self.cards = []

        def ask(kind, s, summary, payload, timeout=840, always="", result=None):
            self.asked.append((kind, summary, always))
            if payload.get("card"):
                self.cards.append(payload["card"])
            if result is not None:
                result["always"] = self.always and bool(always)
                result["answered"] = True
            return self.answer or (self.always and bool(always))
        self.m.ask = ask

    def session(self, key=None, root=None):
        import sessions
        s = sessions.Session(kind="project", project=key or self.key, path=root or self.root, state="ready")
        self.m.sessions[s.id] = s
        self.addCleanup(self.m.sessions.pop, s.id, None)
        return s

    def q(self, s, tool, **args):
        self.m._db_req.t0 = time.time()
        return self.m.tool(s, tool, args)

    def rows(self, db, sql):
        c = sqlite3.connect(self.dbs[db])
        try:
            return c.execute(sql).fetchall()
        finally:
            c.close()



class DbManagerTest(DbBase):
    # ---------------------------------------------------------------- registry
    def test_connections_dev_stack_and_no_secrets(self):
        s = self.session()
        dev = {"instance": "shop_db", "engine": "mysql", "host": "127.0.0.1", "port": 13399, "user": "shop",
               "database": "shop_db", "password": "dev-pw-1"}
        real = dbtools.extensions.collect
        with mock.patch.object(dbtools.extensions, "collect",   # an extension's dev stack
                               side_effect=lambda attr, *a: [dev] if attr == "db_dev" else real(attr, *a)):
            r = self.q(s, "db_connections")
        names = {c["name"]: c for c in r["connections"]}
        self.assertIn(self.key + "/dev/shop_db", names)            # from the extension's dev stack
        dev = names[self.key + "/dev/shop_db"]
        self.assertEqual((dev["class"], dev["user"], dev["database"], dev["host"]), ("dev", "shop", "shop_db", "127.0.0.1:13399"))
        self.assertTrue(names[self.key + "/prod"]["masked"])
        self.assertFalse(names[self.key + "/test"]["masked"])
        self.assertNotIn("team-x/dbo/test", names)                    # another project's: not listed (invariant 6)
        text = json.dumps(r)
        for secret in ("pw-test", "pw-prod", "dev-pw-1", "prod-db"):
            self.assertNotIn(secret, text)

    def test_secret_file_mode(self):
        st = os.stat(dbtools.secret_path(self.key + "/prod"))
        self.assertEqual(st.st_mode & 0o777, 0o600)
        self.assertLessEqual(len(os.path.basename(dbtools.secret_path("A/B.c/" + "x" * 200))), 41)

    # ---------------------------------------------------------------- reads
    def test_dev_read_no_card(self):
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/local", sql="SELECT id, name FROM customer ORDER BY id")
        self.assertEqual(r["rows"], [[1, "Ann"], [2, "Bob"]])
        self.assertEqual(r["note"], dbtools.HEADER)
        self.assertEqual(self.asked, [])

    def test_read_path_refuses_writes(self):
        s = self.session()
        for sql in ("UPDATE customer SET name='x'", "DELETE FROM customer", "PRAGMA query_only=OFF",
                    "WITH c AS (SELECT 1) DELETE FROM customer", "SELECT 1; DELETE FROM customer"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.q(s, "db_query", connection=self.key + "/local", sql=sql)
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer")[0][0], 2)

    def test_read_only_connection_server_side(self):
        """Even past the text check (GUI console allows SET), the read connection cannot write."""
        with self.assertRaises(ValueError):
            self.m.db_gui_query(self.key, self.key + "/local", "SELECT 1 FROM customer WHERE (DELETE FROM customer)")
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer")[0][0], 2)

    def test_test_read_card_once_per_session(self):
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 2")
        self.assertEqual([a[0] for a in self.asked], ["db_read_test"])
        s2 = self.session()
        self.q(s2, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.assertEqual(len(self.asked), 2)

    def test_prod_read_declined(self):
        s = self.session()
        self.answer = False
        with self.assertRaises(ValueError):
            self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT 1")
        self.assertEqual(self.asked[0][0], "db_read_prod")

    def test_prod_read_each(self):
        common.write_json(dbtools.CONFIG_FILE, {"prod_read": "each"})
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT 1")
        self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT 1")
        self.assertEqual([a[0] for a in self.asked], ["db_read_prod", "db_read_prod"])

    def test_parser_boundary_asks_every_read(self):
        """SQL Server has no read-only transaction: on test every agent read asks (5.3)."""
        dbtools.BOUNDARY["sqlite"] = "parser"
        self.addCleanup(dbtools.BOUNDARY.__setitem__, "sqlite", "transaction")
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.assertEqual([a[0] for a in self.asked], ["db_read_test", "db_read_test"])
        self.q(s, "db_query", connection=self.key + "/local", sql="SELECT 1")   # dev: never
        self.assertEqual(len(self.asked), 2)

    def test_masking_on_prod_and_unmask(self):
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT name, email FROM customer ORDER BY id")
        self.assertEqual(r["rows"][0], ["Ann", dbtools.MASK])
        self.assertEqual(r["masked_columns"], ["email"])
        with self.assertRaises(ValueError):
            self.q(s, "db_cell", result_id=r["result_id"], row=0, column="email")
        r = self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT email FROM customer", unmask=True)
        self.assertEqual(r["rows"][0], ["ann@example.com"])
        self.assertIn("db_unmask", [a[0] for a in self.asked])
        # the GUI sees raw data
        g = self.m.db_gui_query(self.key, self.key + "/prod", "SELECT email FROM customer ORDER BY id")
        self.assertEqual(g["rows"][0], ["ann@example.com"])

    def test_cut_cells_and_db_cell_and_fetch(self):
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/local", sql="SELECT id, note FROM customer ORDER BY id",
                   max_rows=1)
        self.assertTrue(r["more"])
        r2 = self.q(s, "db_fetch", result_id=r["result_id"], offset=1)
        self.assertTrue(r2["rows"][0][1].endswith(dbtools.CUT))
        self.assertEqual(r2["truncated_cells"], [[1, "note"]])
        full = self.q(s, "db_cell", result_id=r["result_id"], row=1, column="note")
        self.assertEqual(len(full["value"]), 9000)
        # writing a cut value back is refused
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_execute", connection=self.key + "/local",
                   sql="UPDATE customer SET note='%s' WHERE id=2" % r2["rows"][0][1].replace("'", "''"))
        self.assertIn("TRUNCATED", str(e.exception))
        other = self.session()
        with self.assertRaises(ValueError):   # a result belongs to its session
            self.q(other, "db_fetch", result_id=r["result_id"], offset=0)

    def test_max_bytes(self):
        common.write_json(dbtools.CONFIG_FILE, {"max_bytes": 4096, "max_cell": 64})
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/local",
                   sql="WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<500) SELECT i, 'abcdefghij' FROM n",
                   max_rows=500)
        self.assertLess(len(json.dumps(r["rows"])), 4096 + 200)
        self.assertTrue(r["more"])

    # ---------------------------------------------------------------- writes
    def test_dev_write_preview_card_commit(self):
        s = self.session()
        r = self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name='Z' WHERE id > 0",
                   reason="rename")
        self.assertEqual(r["rowcount"], [2])
        self.assertEqual(r["preview_rowcount"], [2])
        self.assertTrue(r["committed"])
        self.assertTrue(r["audit_id"])
        kind, summary, always = self.asked[0]
        self.assertEqual(kind, "db_write_dev")
        self.assertIn("⚠ affects 2 rows", summary)
        self.assertIn("UPDATE customer SET name='Z' WHERE id > 0;", summary)
        self.assertIn("Why: rename", summary)
        self.assertEqual(always, "Allow writes for this session")
        self.assertEqual(self.rows("dev", "SELECT DISTINCT name FROM customer"), [("Z",)])
        card = self.cards[0]
        self.assertEqual(card["preview"], "Preview (rolled back): affects 2 rows · InnoDB"[:len("Preview (rolled back): affects 2 rows")]
                         + ("" if not card["preview"].endswith("InnoDB") else " · InnoDB"))
        self.assertEqual(card["sample"]["columns"][:2], ["id", "name"])
        self.assertEqual(card["sample"]["rows"][0][1], {"v": "Ann", "to": "Z"})   # before -> after, rolled back
        self.assertEqual(card["reason"], "rename")

    def test_evidence_sqlite_dry_run_is_rolled_back_before_asking(self):
        s = self.session()
        conn = self.m._db_find(self.m._db_defs(self.key, self.root), self.key + "/test")
        self.m._db_worker(conn, "mcp", s, slot="evidence")
        payloads = []
        def ask(kind, session, summary, payload, **kw):
            payloads.append(payload)
            self.assertEqual(self.rows("test", "SELECT name FROM customer WHERE id=1"), [("Ann",)])
            kw["result"].update(answered=True, always=False)
            return False
        self.m.ask = ask
        with self.assertRaisesRegex(ValueError, "declined"):
            self.q(s, "db_execute", connection=self.key + "/test", sql="UPDATE customer SET name='Changed' WHERE id=1")
        job = payloads[0]["_evidence_job"]
        self.assertTrue(job.done.wait(3))
        facts = job.value
        self.assertIn("1 of 2 rows change", json.dumps(facts))
        self.assertEqual(facts["preview"]["rows"][0][1], {"v": "Ann", "to": "Changed"})
        self.assertEqual(self.rows("test", "SELECT name FROM customer WHERE id=1"), [("Ann",)])

    def test_disabled_evidence_dry_run_never_executes_a_preview(self):
        with mock.patch.object(self.m, "policy", dict(self.m.policy, db_dry_run=False)):
            # Reject at the gate, independently of the worker. No execute request may be sent.
            w = mock.Mock()
            ok, why = self.m._db_gate(w, {"cls": "test", "engine": "sqlite"},
                                      [{"sql": "UPDATE customer SET name='X' WHERE id=1", "kind": "dml"}], None)
            self.assertFalse(ok)
            self.assertIn("off", why[0])
            w.call.assert_not_called()

    def test_preview_after_rows_follow_before_primary_key_order_and_rollback(self):
        s = self.session()
        conn = self.m._db_find(self.m._db_defs(self.key, self.root), self.key + "/test")
        w = self.m._db_worker(conn, "mcp", s, slot="evidence")
        sample = {"select": "SELECT id, name FROM customer ORDER BY id DESC", "table": "customer",
                  "pk_names": ["id"], "pk": ["id"], "update": True}
        result = w.call("execute", 5, owner=s.id, statements=[{"sql": "UPDATE customer SET name='Changed' WHERE id>0"}],
                        preview=True, sample=sample, timeout_s=2)
        before, after = result["sample"]["before"], result["sample"]["after"]
        self.assertEqual(before["table_total"], 2)
        self.assertEqual([r[0] for r in before["rows"]], [2, 1])
        self.assertEqual([r[0] for r in after["rows"]], [2, 1])
        self.assertEqual(self.rows("test", "SELECT name FROM customer ORDER BY id"), [("Ann",), ("Bob",)])

    def test_production_write_never_previews_or_sends_sample_values(self):
        s = self.session()
        r = self.q(s, "db_execute", connection=self.key + "/prod",
                   sql="UPDATE customer SET email='new@example.com' WHERE id = 1", reason="fix")
        card = self.cards[-1]
        self.assertFalse(card["preview_ok"])
        self.assertNotIn("sample", card)
        self.assertNotIn("preview_rows", r)
        self.assertNotIn("preview_rowcount", r)
        self.assertNotIn("example.com", json.dumps(r))

    LOOP = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT count(*) FROM n"   # never ends

    def test_the_gui_has_its_own_query_timeout(self):
        """L7 (2026-10-07): the Database view's timeout is gui_timeout_s, not the agents' max_timeout_s."""
        self.assertEqual(dbtools.DEFAULTS["gui_timeout_s"], 300)
        common.write_json(dbtools.CONFIG_FILE, {"gui_timeout_s": 1, "max_timeout_s": 120})
        t0 = time.time()
        with self.assertRaisesRegex(dbtools.DbError, "cancelled"):
            self.m.db_gui_query(self.key, self.key + "/local", self.LOOP)
        self.assertLess(time.time() - t0, 20)

    def test_the_human_cancels_a_sessions_running_query(self):
        """L7 (2026-10-07): db_gui_running lists a session's statement; db_gui_cancel(owner=<session>) stops it."""
        s = self.session()
        conn = self.key + "/local"
        out = {}

        def run():
            try:
                out["r"] = self.q(s, "db_query", connection=conn, sql=self.LOOP, timeout_s=60)
            except Exception as e:  # noqa: BLE001
                out["e"] = e
        th = threading.Thread(target=run)
        th.start()
        self.addCleanup(th.join, 5)
        deadline = time.time() + 15
        rows = []
        while time.time() < deadline and not rows:
            rows = [r for r in self.m.db_gui_running(self.key, conn)["running"] if r["owner"] == s.id]
            time.sleep(0.05)
        self.assertEqual((rows[0]["op"], rows[0]["cancellable"], rows[0]["title"]), ("query", True, s.title))
        self.assertEqual(self.m.db_gui_cancel(self.key, conn)["cancelled"], 0)    # the GUI's own only
        self.assertEqual(self.m.db_gui_cancel(self.key, conn, owner=s.id)["cancelled"], 1)
        th.join(15)
        self.assertIn("cancelled", str(out.get("e")))
        self.assertEqual(self.m.db_gui_running(self.key, conn)["running"], [])
        with self.assertRaises(KeyError):
            self.m.db_gui_cancel(self.key, conn, owner="nosuchsession")

    def test_declined_write_changes_nothing(self):
        s = self.session()
        self.answer = False
        with self.assertRaises(ValueError):
            self.q(s, "db_execute", connection=self.key + "/local", sql="DELETE FROM customer WHERE id=1")
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer")[0][0], 2)

    def test_allow_writes_for_session_dev_only(self):
        s = self.session()
        self.answer, self.always = False, True
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name='1' WHERE id=1")
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name='2' WHERE id=1")
        self.assertEqual(len(self.asked), 1)
        self.answer, self.always = True, False
        self.q(s, "db_execute", connection=self.key + "/test", sql="UPDATE customer SET name='1' WHERE id=1")
        self.q(s, "db_execute", connection=self.key + "/test", sql="UPDATE customer SET name='2' WHERE id=1")
        test_cards = [a for a in self.asked if a[0] == "db_write_test"]
        self.assertEqual(len(test_cards), 2)
        self.assertEqual(test_cards[0][2], "")   # no "for this session" button on test

    def test_idempotent_replay(self):
        s = self.session()
        a = self.q(s, "db_execute", connection=self.key + "/local", sql="INSERT INTO customer (name) VALUES ('C')")
        b = self.q(s, "db_execute", connection=self.key + "/local", sql="INSERT INTO customer (name) VALUES ('C')")
        self.assertIn("replay", b)
        self.assertEqual(b["audit_id"], a["audit_id"])
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer WHERE name='C'")[0][0], 1)
        self.assertEqual(len(self.asked), 1)

    def test_statements_one_transaction(self):
        s = self.session()
        with self.assertRaises(ValueError):
            self.q(s, "db_execute", connection=self.key + "/local",
                   statements=["UPDATE customer SET name='Q' WHERE id=1", "UPDATE nosuch SET a=1 WHERE id=1"])
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "Ann")
        r = self.q(s, "db_execute", connection=self.key + "/local",
                   statements=[{"sql": "UPDATE customer SET name=? WHERE id=?", "params": ["Q", 1]},
                               "UPDATE customer SET name='R' WHERE id=2"])
        self.assertEqual(r["rowcount"], [1, 1])
        # the failed run had its card too (an unknown table: no preview, the error comes from the run)
        self.assertEqual(len([a for a in self.asked if a[0] == "db_write_dev"]), 2)

    def test_script(self):
        s = self.session()
        r = self.q(s, "db_script", connection=self.key + "/local", path="sql/fix.sql")
        self.assertEqual(r["rowcount"], [1, 1])
        self.assertEqual(self.rows("dev", "SELECT name FROM customer ORDER BY id"), [("A",), ("B",)])
        with self.assertRaises(ValueError):
            self.q(s, "db_script", connection=self.key + "/local", path="../dbo/x.sql")

    def test_engine_gate_no_preview_on_memory(self):
        s = self.session()
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE mem_cache SET v='2' WHERE k='a'")
        summary = self.asked[0][1]
        self.assertIn("⚠ no preview: table mem_cache is MEMORY", summary)
        self.assertNotIn("affects", summary)

    def test_ddl_no_preview(self):
        s = self.session()
        self.answer = False
        with self.assertRaises(ValueError):
            self.q(s, "db_execute", connection=self.key + "/local", sql="CREATE TABLE t2 (a INT)")
        self.assertIn("No dry run for schema changes.", self.asked[0][1])
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM sqlite_master WHERE name='t2'")[0][0], 0)

    def test_prod_requires_where(self):
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_execute", connection=self.key + "/prod", sql="DELETE FROM customer")
        self.assertIn("without WHERE", str(e.exception))
        self.assertEqual([a for a in self.asked if a[0].startswith("db_write")], [])

    def test_prod_write_card_red(self):
        import sessions
        s = self.session()
        self.q(s, "db_execute", connection=self.key + "/prod", sql="UPDATE customer SET name='P' WHERE id=1")
        kind, summary, always = [a for a in self.asked if a[0] == "db_write_prod"][0]
        self.assertIn("⚠ PRODUCTION", summary)
        self.assertEqual(always, "")
        self.assertIn("db_write_prod", sessions.ALWAYS_ASK)

    def test_cut_value_without_marker_refused(self):
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/local", sql="SELECT note FROM customer WHERE id = 2")
        shown = r["rows"][0][0].replace(dbtools.CUT, "")
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET note='%s' WHERE id=2" % shown)
        self.assertIn("TRUNCATED", str(e.exception))
        full = self.q(s, "db_cell", result_id=r["result_id"], row=0, column="note")["value"]
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET note='%s' WHERE id=2" % full)

    def test_other_project_write_needs_unlock(self):
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_execute", connection="team-x/dbo/test", sql="UPDATE customer SET name='x' WHERE id=1")
        self.assertIn("locked", str(e.exception))
        self.assertEqual([a for a in self.asked if a[0] == "db_other"], [])   # refused before the "use it" card
        s.unlock = {"db_write_other": True}
        self.q(s, "db_execute", connection="team-x/dbo/test", sql="UPDATE customer SET name='x' WHERE id=1")
        self.assertEqual(self.rows("other", "SELECT name FROM customer WHERE id=1")[0][0], "x")

    def test_history_has_only_agent_calls(self):
        s = self.session()
        self.m.db_gui_query(self.key, self.key + "/local", "SELECT 'secret-literal'", session=s.id)
        self.q(s, "db_query", connection=self.key + "/local", sql="SELECT 1")
        h = json.dumps(self.q(s, "db_history"))
        self.assertNotIn("secret-literal", h)
        self.assertIn("SELECT 1", h)

    def test_card_shows_params(self):
        s = self.session()
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name=? WHERE id=?", params=["Pp", 1])
        self.assertIn('-- params: ["Pp", 1]', self.cards[0]["sql"])

    def test_empty_mask_words_means_builtin(self):
        common.write_json(dbtools.CONFIG_FILE, {"mask_words": []})
        self.assertIn("email", self.m.db_config()["mask_words"])

    def test_auto_approved_write_is_audited_as_auto(self):
        self.m.policy["auto_approve"] = ["db_write_dev"]
        self.addCleanup(self.m.policy.__setitem__, "auto_approve", [])
        s = self.session()
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name='A' WHERE id=1")
        with open(dbtools.AUDIT_FILE) as f:
            self.assertIn('"approved": "auto"', f.read())

    def test_budget_after_late_approval(self):
        s = self.session()
        self.m._db_req.t0 = time.time() - 890
        with self.assertRaises(ValueError) as e:
            self.m.tool(s, "db_execute", {"connection": self.key + "/local", "sql": "DELETE FROM customer WHERE id=1"})
        self.assertIn("approved too late", str(e.exception))
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer")[0][0], 2)

    # ---------------------------------------------------------------- scope and identity
    def test_other_project_card(self):
        s = self.session()
        r = self.q(s, "db_query", connection="team-x/dbo/test", sql="SELECT COUNT(*) FROM customer")
        self.assertEqual(r["rows"], [[2]])
        self.assertEqual(self.asked[0][0], "db_other")
        self.assertEqual(self.asked[0][2], "Allow always")

    def test_pin_mismatch_refused(self):
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.assertEqual(self.m._db_store()["pins"][self.key + "/test"]["server_id"], "srv-1")
        for w in list(self.m._db_workers.values()):
            w.kill()
        self.m._db_workers.clear()
        mkdb(self.dbs["test"], server="srv-OTHER")
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.assertIn("DIFFERENT server", str(e.exception))
        self.m.db_unpin(self.key + "/test")
        self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")

    def test_missing_password(self):
        dbtools.write_secret(self.key + "/test", None)
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/test", sql="SELECT 1")
        self.assertIn("no password", str(e.exception))
        self.assertEqual(self.asked, [])   # no card first

    # ---------------------------------------------------------------- audit and history
    def test_audit_has_sql_not_rows(self):
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/local", sql="SELECT email FROM customer")
        self.q(s, "db_execute", connection=self.key + "/local", sql="UPDATE customer SET name='Y' WHERE id=1")
        with open(dbtools.AUDIT_FILE) as f:
            text = f.read()
        self.assertIn("SELECT email FROM customer", text)
        self.assertNotIn("ann@example.com", text)
        self.assertEqual(os.stat(dbtools.AUDIT_FILE).st_mode & 0o777, 0o600)
        h = self.q(s, "db_history")["queries"]
        self.assertTrue(any("UPDATE customer" in (q["sql"] or "") for q in h))

    def test_the_history_time_is_the_statements_own_not_the_card_wait(self):
        """B-N4-2: a query approved on a card showed "52862 ms" in the GUI history (the card wait) while the
        agent got the execution time. Both carry the statement's own time; a declined / metadata call too."""
        orig = self.m.ask

        def slow(*a, **k):
            time.sleep(1.5)   # the user reads the card
            return orig(*a, **k)
        self.m.ask = slow
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/prod", sql="SELECT id FROM customer")
        w = self.q(s, "db_execute", connection=self.key + "/prod", sql="UPDATE customer SET name='Z' WHERE id=1",
                   preview=False)
        self.q(s, "db_describe", connection=self.key + "/test", table="customer")
        self.answer = False
        with self.assertRaises(ValueError):
            self.q(self.session(), "db_query", connection=self.key + "/prod", sql="SELECT 2")
        self.assertEqual(len(self.asked), 4)
        h = {(q["tool"], q["sql"]): q for q in self.m.db_gui_history(self.key)["queries"]}
        self.assertIn(("db_query", "SELECT 2"), h)
        q = h[("db_query", "SELECT id FROM customer")]
        self.assertEqual((q["approved"], q["elapsed_ms"]), ("card", r["elapsed_ms"]))
        self.assertLess(r["elapsed_ms"], 1000)
        q = h[("db_execute", "UPDATE customer SET name='Z' WHERE id=1")]
        self.assertEqual((q["approved"], q["elapsed_ms"]), ("card", w["elapsed_ms"]))
        self.assertLess(w["elapsed_ms"], 1000)
        for (tool, sql), q in h.items():
            with self.subTest(tool=tool, sql=sql):
                self.assertLess(q["elapsed_ms"], 1000)

    def test_the_history_time_stays_the_statements_own_under_concurrent_cards(self):
        """N6 review of B-N4-2: calls of one session run in their own mailbox threads - one call's card wait must
        not land in another call's time (dbtools._db_req is thread-local; a call answered meanwhile, a call with
        no card, a declined one)."""
        orig = self.m.ask
        waits = {"SELECT id FROM customer": 2.0, "SELECT name FROM customer": 0.6}

        def slow(kind, s, summary, *a, **k):
            time.sleep(next((v for q, v in waits.items() if q in summary), 0.6))
            return orig(kind, s, summary, *a, **k)
        self.m.ask = slow
        s, other = self.session(), self.session()
        out, errs = {}, []

        def run(name, sess, tool, **args):
            try:
                out[name] = self.q(sess, tool, **args)
            except ValueError as e:
                errs.append((name, str(e)))
        calls = [("a", s, "db_query", dict(connection=self.key + "/prod", sql="SELECT id FROM customer")),
                 ("b", s, "db_query", dict(connection=self.key + "/local", sql="SELECT id, name FROM customer")),
                 ("c", s, "db_query", dict(connection=self.key + "/test", sql="SELECT name FROM customer")),
                 ("d", other, "db_describe", dict(connection=self.key + "/test", table="customer"))]
        threads = []
        for name, sess, tool, args in calls:
            threads.append(threading.Thread(target=run, args=(name, sess, tool), kwargs=args))
            threads[-1].start()
            time.sleep(0.15)
        for t in threads:
            t.join(30)
        self.assertEqual(errs, [])
        h = self.m.db_gui_history(self.key)["queries"]
        self.assertGreaterEqual(len(h), 4)
        for q in h:
            with self.subTest(tool=q["tool"], sql=q.get("sql")):
                self.assertLess(q["elapsed_ms"], 500, q)
        for name in ("a", "b", "c"):
            self.assertLess(out[name]["elapsed_ms"], 500, (name, out[name]))

    # ---------------------------------------------------------------- GUI backend
    def test_gui_console_and_execute(self):
        r = self.m.db_gui_query(self.key, self.key + "/local", "SELECT name FROM customer ORDER BY id")
        self.assertEqual(r["rows"], [["Ann"], ["Bob"]])
        with self.assertRaises(ValueError):
            self.m.db_gui_query(self.key, self.key + "/local", "DELETE FROM customer")
        r = self.m.db_gui_execute(self.key, self.key + "/local", [{"sql": "UPDATE customer SET name=? WHERE id=?",
                                                                    "params": ["G", 1]}])
        self.assertEqual(r["rowcount"], [1])
        with self.assertRaises(ValueError):
            self.m.db_gui_execute(self.key, self.key + "/prod", ["UPDATE customer SET name='x' WHERE id=1"])
        r = self.m.db_gui_execute(self.key, self.key + "/prod", ["UPDATE customer SET name='x' WHERE id=1"],
                                  confirm="prod")
        self.assertEqual(r["rowcount"], [1])
        p = self.m.db_gui_execute(self.key, self.key + "/local", ["DELETE FROM customer WHERE id=2"], preview_only=True)
        self.assertEqual(p["preview"]["results"][0]["rowcount"], 1)
        self.assertEqual(self.rows("dev", "SELECT COUNT(*) FROM customer")[0][0], 2)
        self.assertEqual(self.asked, [])

    def test_gui_pages_fetch_only_the_page(self):
        # the GUI fetches its page (+1 to know `more`), never thousands of rows up front; the next page
        # runs the statement again with `offset` (as IDEA's result paging)
        sql = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<5000) SELECT i FROM n"
        r = self.m.db_gui_query(self.key, self.key + "/local", sql, max_rows=100)
        self.assertEqual([x[0] for x in r["rows"]], list(range(1, 101)))
        self.assertTrue(r["more"])
        self.assertEqual(len(self.m._db_results[r["result_id"]]["rows"]), 100)
        r2 = self.m.db_gui_query(self.key, self.key + "/local", sql, max_rows=100, offset=100)
        self.assertEqual([x[0] for x in r2["rows"]], list(range(101, 201)))
        self.assertEqual((r2["offset"], r2["more"]), (100, True))
        self.assertEqual(len(self.m._db_results[r2["result_id"]]["rows"]), 200)
        last = self.m.db_gui_query(self.key, self.key + "/local", sql, max_rows=100, offset=4950)
        self.assertEqual(([x[0] for x in last["rows"]][-1], last["more"]), (5000, False))
        with self.assertRaises(ValueError):   # a page past dbworker's 100 000-row cap is refused, never silently moved
            self.m.db_gui_query(self.key, self.key + "/local", sql, max_rows=100, offset=99950)
        r = self.m.db_gui_query(self.key, self.key + "/local", sql)   # the default page: 500 (IDEA), not 1000
        self.assertEqual((len(r["rows"]), len(self.m._db_results[r["result_id"]]["rows"])), (500, 500))
        tx = self.m.db_gui_tx(self.key, self.key + "/local", "exec", [sql], tx="pg", max_rows=100)
        self.assertEqual((len(tx["results"][0]["rows"]), tx["results"][0]["truncated"], tx["results"][0]["kind"]), (100, True, "select"))
        self.m.db_gui_tx(self.key, self.key + "/local", "rollback", tx="pg")

    def test_gui_console_transaction(self):
        r = self.m.db_gui_tx(self.key, self.key + "/local", "exec", ["UPDATE customer SET name='T' WHERE id=1",
                                                                       "SELECT name FROM customer WHERE id=1"])
        self.assertTrue(r["tx_open"])
        self.assertEqual(r["results"][1]["rows"], [["T"]])               # sees its own uncommitted change
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "Ann")
        # a session's write meanwhile runs on its own worker, not inside the human's transaction
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/local", sql="SELECT 1")
        self.m.db_gui_tx(self.key, self.key + "/local", "rollback")
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "Ann")
        self.m.db_gui_tx(self.key, self.key + "/local", "exec", ["UPDATE customer SET name='C' WHERE id=1"])
        self.assertTrue(self.m.db_gui_tx(self.key, self.key + "/local", "commit")["committed"])
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "C")
        with self.assertRaises(ValueError):
            self.m.db_gui_tx(self.key, self.key + "/prod", "exec", ["UPDATE customer SET name='x' WHERE id=1"])
        with self.assertRaises(ValueError):
            self.m.db_gui_tx(self.key, self.key + "/prod", "exec", ["DELETE FROM customer"], confirm="prod")
        refused = self.m.db_gui_history(self.key)["queries"][0]   # the history shows the refused SQL, not "gui_tx_exec"
        self.assertEqual((refused["sql"], bool(refused["error"])), ("DELETE FROM customer", True))
        with self.assertRaises(ValueError):   # a bidi control in the SQL shows as [U+202E] in the history, never reordered
            self.m.db_gui_tx(self.key, self.key + "/prod", "exec", ["DELETE FROM customer -- \u202eevil"], confirm="")
        self.assertEqual(self.m.db_gui_history(self.key)["queries"][0]["sql"], "DELETE FROM customer -- [U+202E]evil")
        self.m.db_gui_tx(self.key, self.key + "/prod", "exec", ["UPDATE customer SET name='x' WHERE id=1"], confirm="prod")
        with self.assertRaises(ValueError):
            self.m.db_gui_tx(self.key, self.key + "/prod", "commit")
        self.m.db_gui_tx(self.key, self.key + "/prod", "rollback")
        self.assertEqual(self.rows("prod", "SELECT name FROM customer WHERE id=1")[0][0], "Ann")

    def test_gui_consoles_do_not_share_a_transaction(self):
        # review 2026-10-06: a CSV import's / another console's Commit committed this console's open change
        self.m.db_gui_tx(self.key, self.key + "/local", "exec", ["UPDATE customer SET name='A1' WHERE id=1"], tx="pageA-c1")
        self.m.db_gui_tx(self.key, self.key + "/local", "rollback", tx="pageA-c2")        # the other console
        self.assertTrue(self.m.db_gui_tx(self.key, self.key + "/local", "state", tx="pageA-c1")["tx_open"])
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "Ann")
        self.m.db_gui_tx(self.key, self.key + "/local", "commit", tx="pageA-c1")
        self.assertEqual(self.rows("dev", "SELECT name FROM customer WHERE id=1")[0][0], "A1")
        with self.assertRaises(ValueError):
            self.m.db_gui_tx(self.key, self.key + "/local", "state", tx="../x")
        # a finished named transaction frees its connection (the bound counts open ones, N5)
        self.assertFalse(any(str(k[-1]) == "gui-tx:pageA-c1" for k in self.m._db_workers))
        # saving only an alias leaves an open transaction alone (N2)
        self.m.db_gui_tx(self.key, self.key + "/test", "exec", ["UPDATE customer SET name='T1' WHERE id=1"], tx="pageA-c3")
        d = [c for c in self.m._db_store()["connections"] if c["name"] == self.key + "/test"][0]
        old_engines = dbtools.ENGINES
        dbtools.ENGINES = old_engines + ("sqlite",)   # the editor's engines + the test double
        try:
            self.m.db_gui_save(self.key, dict(d, label="renamed alias"))
        finally:
            dbtools.ENGINES = old_engines
        self.assertTrue(self.m.db_gui_tx(self.key, self.key + "/test", "state", tx="pageA-c3")["tx_open"])
        self.m.db_gui_tx(self.key, self.key + "/test", "rollback", tx="pageA-c3")

    def test_card_never_cuts_a_parameter(self):
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_execute", connection=self.key + "/test",
                   statements=[{"sql": "UPDATE customer SET note = %s WHERE id = 1", "params": ["x" * 5000]}])
        self.assertIn("longer than the card shows", str(e.exception))
        self.assertEqual(self.asked, [])
        # the human's grid Submit has no card: a long text cell saves (second review, N1)
        self.m.db_gui_execute(self.key, self.key + "/local", [{"sql": "UPDATE customer SET note = ? WHERE id = 1", "params": ["y" * 5000]}])   # sqlite's placeholder
        self.assertEqual(len(self.rows("dev", "SELECT note FROM customer WHERE id=1")[0][0]), 5000)

    def test_mask_numbers_made_of_personal_data(self):
        c = {"name": "x", "cls": "prod", "engine": "mysql"}
        cols = [{"name": "id", "type": "long", "org": "id"}, {"name": "c1", "type": "long", "org": ""},
                {"name": "n", "type": "longlong", "org": ""}]
        sql = "SELECT id, ORD(SUBSTRING(email, 1, 1)) AS c1, COUNT(*) AS n FROM customer"
        self.assertEqual(self.m._db_mask_cols(c, sql, cols, "mysql", [[1, 97, 2]]), [1])   # COUNT stays readable
        cnt = "SELECT COUNT(*) AS n FROM customer WHERE email LIKE '%x%'"
        self.assertEqual(self.m._db_mask_cols(c, cnt, [{"name": "n", "type": "longlong", "org": ""}], "mysql", [[3]]), [])
        one = [{"name": "x", "type": "long", "org": ""}]
        for q in ("SELECT x FROM (SELECT ORD(SUBSTRING(email, 1, 1)) AS x FROM customer) d",   # N3: nested selects
                  "WITH d AS (SELECT ORD(SUBSTRING(email, 1, 1)) AS x FROM customer) SELECT x FROM d",
                  "SELECT 0 AS x UNION ALL SELECT ORD(SUBSTRING(email, 1, 1)) FROM customer"):
            self.assertEqual(self.m._db_mask_cols(c, q, one, "mysql", [[97]]), [0], q)

    def test_rename_keeps_password_and_pin(self):
        old, new = self.key + "/test", self.key + "/test2"
        d = self.m._db_store()
        d["pins"][old] = {"server_id": "srv-1", "hostname": "h", "at": 1}
        self.m._db_save(d)
        self.m.db_gui_save(self.key, {"name": new, "old_name": old, "cls": "test", "engine": "mysql", "host": "test-db",
                                      "port": 1, "database": "x", "user": "u"})
        self.assertEqual(dbtools.read_secret(new), "pw-test")
        self.assertFalse(dbtools.read_secret(old))
        d = self.m._db_store()
        self.assertIn(new, d["pins"])
        self.assertNotIn(old, d["pins"])

    def test_gui_connections_and_editor(self):
        r = self.m.db_gui_connections(self.key)
        self.assertIn(self.key + "/prod", [c["name"] for c in r["connections"]])
        self.assertNotIn("pw-prod", json.dumps(r))
        self.assertEqual(r["ssh_user"], "")                    # upstream: the editor pre-fills nothing
        common.write_json(dbtools.CONFIG_FILE, {"ssh_user": "projects"})
        self.assertEqual(self.m.db_gui_connections(self.key)["ssh_user"], "projects")
        with self.assertRaises(ValueError):   # test/prod on localhost: refused (13.2 #2)
            self.m.db_gui_save(self.key, {"name": self.key + "/test2", "cls": "test", "host": "localhost", "port": 3306})
        for host in ("127.1", "LOCALHOST", "localhost.", "0x7f000001", "2130706433", "127.0.0.2", "0.0.0.0", "db.localhost"):
            with self.assertRaises(ValueError, msg=host):   # every spelling of loopback, not only the three literals
                self.m.db_gui_save(self.key, {"name": self.key + "/test2", "cls": "test", "host": host, "port": 3306})
        self.m.db_gui_save(self.key, {"name": self.key + "/test4", "cls": "test", "host": "db127.example", "port": 3306})
        self.m.db_gui_delete(self.key, self.key + "/test4")
        with self.assertRaises(ValueError):   # tunnel env must match the class
            self.m.db_gui_save(self.key, {"name": self.key + "/t3", "cls": "prod", "tunnel": {"env": "test", "target": "mysql"}})
        self.m.db_gui_save(self.key, {"name": self.key + "/test3", "cls": "test", "database": "shop",
                                      "user": "shop", "tunnel": {"env": "test", "target": "mysql"}}, password="s3cret")
        self.assertEqual(dbtools.read_secret(self.key + "/test3"), "s3cret")
        names = [c["name"] for c in self.m.db_gui_connections(self.key)["connections"]]
        self.assertIn(self.key + "/test3", names)
        self.m.db_gui_delete(self.key, self.key + "/test3")
        self.assertEqual(dbtools.read_secret(self.key + "/test3"), "")

    def test_a_name_that_maps_to_loopback_is_loopback(self):
        """Review 2026-10-07: localhost6 / ip6-localhost / an /etc/hosts alias / a DNS name answering 127.0.0.1
        passed the test/prod localhost refusal."""
        import tempfile
        from unittest import mock
        with tempfile.NamedTemporaryFile("w", suffix="hosts", delete=False) as f:
            f.write("127.0.0.1 localhost\n::1 localhost6 ip6-localhost # v6\n127.0.1.1 mybox mybox.lan\n10.0.0.5 db.lan\n")
        self.addCleanup(os.unlink, f.name)
        names = dbtools._hosts_loopback_names(f.name)
        self.assertTrue({"localhost6", "ip6-localhost", "mybox", "mybox.lan"} <= names, names)
        self.assertNotIn("db.lan", names)
        with mock.patch.object(dbtools, "_hosts_loopback_names", lambda: names), \
                mock.patch.object(dbtools, "_resolves_to_loopback", lambda h: False):
            for host in ("localhost6", "ip6-localhost", "MyBox.lan."):
                self.assertTrue(dbtools.loopback_host(host), host)
            self.assertFalse(dbtools.loopback_host("db.lan"))
        answers = {"127.0.0.1.nip.io": [(2, 1, 6, "", ("127.0.0.1", 0))],
                   "v6.example": [(30, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))],
                   "db.example": [(2, 1, 6, "", ("10.1.2.3", 0))]}
        with mock.patch.object(dbtools.socket, "getaddrinfo", lambda h, p: answers[h]):
            self.assertTrue(dbtools._resolves_to_loopback("127.0.0.1.nip.io"))
            self.assertTrue(dbtools._resolves_to_loopback("v6.example"))
            self.assertFalse(dbtools._resolves_to_loopback("db.example"))

        def slow(h, p):
            time.sleep(1)
            return answers["127.0.0.1.nip.io"]
        with mock.patch.object(dbtools.socket, "getaddrinfo", slow):   # a DNS that does not answer in time
            t0 = time.time()
            self.assertFalse(dbtools._resolves_to_loopback("slow.example", timeout=0.2))
            self.assertLess(time.time() - t0, 0.8)

    def test_gui_all_lists_every_project_with_defs(self):
        r = self.m.db_gui_all()
        keys = [g["project"] for g in r["projects"]]
        self.assertIn(self.key, keys)
        self.assertIn("team-x/dbo", keys)   # another project's connections too: the view is not filtered
        prod = [c for g in r["projects"] for c in g["connections"] if c["name"] == self.key + "/prod"][0]
        self.assertEqual(prod["def"]["cls"], "prod")   # Edit has its starting point
        self.assertEqual(prod["project"], self.key)
        self.assertNotIn("pw-prod", json.dumps(r))

    def test_gui_lists_a_projects_connections_dev_test_prod_then_by_name(self):
        """F-DB-PROD G-5: the stored order (prod, dev, test, prod…) made the list jump around"""
        for n, cls in (("zz-prod", "prod"), ("b-test", "test"), ("a-prod", "prod")):
            self.m.db_gui_save(self.key, {"name": "%s/%s" % (self.key, n), "cls": cls, "host": "db.example", "port": 3306,
                                          "database": "x", "user": "u"})
        rank = {"dev": 0, "test": 1, "prod": 2}
        for conns in (self.m.db_gui_connections(self.key)["connections"],
                      [g for g in self.m.db_gui_all()["projects"] if g["project"] == self.key][0]["connections"]):
            got = [(rank[c["class"]], (c.get("label") or c["name"]).lower()) for c in conns]
            self.assertEqual(got, sorted(got))
            self.assertLess([c["name"] for c in conns].index(self.key + "/a-prod"), [c["name"] for c in conns].index(self.key + "/zz-prod"))

    def test_gui_meta(self):
        r = self.m.db_gui_meta(self.key, self.key + "/local", "objects")
        self.assertIn("customer", [o["name"] for o in r["objects"]])
        d = self.m.db_gui_meta(self.key, self.key + "/local", "describe", name="customer")
        self.assertEqual(d["primary_key"], ["id"])
        ix = self.m.db_gui_meta(self.key, self.key + "/local", "columns")   # "Find table or column"
        self.assertIn(["customer", "id"], [r[:2] for r in ix["columns"]])
        self.assertFalse(ix["truncated"])


FAKE_SSH = """#!/usr/bin/env python3
import os, socket, sys, time
a = sys.argv[1:]
with open(os.environ["FAKE_SSH_ARGS"], "w") as f:
    f.write("\\n".join(a))
if os.environ.get("FAKE_SSH_FAIL"):
    sys.stderr.write("projects@bastion.example: Permission denied (publickey).\\n")
    sys.exit(255)
port = int(a[a.index("-L") + 1].split(":")[1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(5)
while True:
    c, _ = s.accept()
    c.close()
"""


class DbSshTest(DbBase):
    """IDEA's SSH/SSL tab: Piper runs `ssh -N -L` itself (a fake ssh listens on the forwarded port)."""

    def setUp(self):
        so = socket.socket()   # the fake ssh listens on the forwarded port
        try:
            so.bind(("127.0.0.1", 0))
        except OSError as e:
            self.skipTest("cannot bind a socket here: %s" % e)
        finally:
            so.close()
        super().setUp()
        self.bin = os.path.join(_env.ROOT, "fake-ssh")
        with open(self.bin, "w") as f:
            f.write(FAKE_SSH)
        os.chmod(self.bin, 0o755)
        self.args = os.path.join(_env.ROOT, "fake-ssh.args")
        os.environ.update(PIPER_SSH_BIN=self.bin, FAKE_SSH_ARGS=self.args)
        os.environ.pop("FAKE_SSH_FAIL", None)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("PIPER_SSH_BIN", "FAKE_SSH_ARGS", "FAKE_SSH_FAIL")])
        self.name = self.key + "/test/ssh"
        self.m.db_gui_save(self.key, {"name": self.name, "cls": "test", "engine": "mysql", "database": self.dbs["test"],
                                      "user": "u", "ssh": {"host": "bastion.example", "user": "projects", "port": 2222},
                                      "host": "localhost", "port": 3306}, "pw-ssh")
        d = common.read_json(dbtools.CONN_FILE, {})   # the editor offers real engines; the test double is sqlite
        for c in d["connections"]:
            if c["name"] == self.name:
                c["engine"] = "sqlite"
        common.write_json(dbtools.CONN_FILE, d)
        self.addCleanup(lambda: self.m.db_gui_tunnel(self.key, self.name, "close"))

    def conn(self):
        return [c for g in self.m.db_gui_all()["projects"] for c in g["connections"] if c["name"] == self.name][0]

    def test_spec_refuses_injection_and_foreign_keys(self):
        for bad in ({"host": "-oProxyCommand=x"}, {"host": "a b"}, {"host": "h", "user": "-x"}, {"host": "h", "port": 0},
                    {"host": "h", "key": "/etc/passwd"}, {"host": "h", "key": "~/.ssh/../.bashrc"}):
            with self.assertRaises(dbtools.DbError, msg=bad):
                dbtools.ssh_spec(bad)
        self.assertEqual(dbtools.ssh_spec({"host": "bastion.example.com", "port": "22", "user": "projects", "key": "$USER_HOME$/.ssh/id_ed25519"}),
                         {"host": "bastion.example.com", "user": "projects", "key": "~/.ssh/id_ed25519"})

    def test_command(self):
        c = {"name": "x", "engine": "mysql", "host": "127.0.0.1", "port": 3306,
             "ssh": {"host": "bastion.example.com", "user": "projects", "key": "~/.ssh/id_ed25519"}}
        cmd = dbtools.ssh_command(c, 43400)
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("ExitOnForwardFailure=yes", cmd)
        self.assertEqual(cmd[-4:], ["-L", "127.0.0.1:43400:127.0.0.1:3306", "--", "bastion.example.com"])
        self.assertEqual(cmd[cmd.index("-i") + 1], os.path.expanduser("~/.ssh/id_ed25519"))

    def test_open_use_close(self):
        c = self.conn()
        self.assertEqual((c["reach"], c["tunnel"]["env"], c["tunnel"]["target"], c["tunnel"]["state"]),
                         ("ssh", "ssh", "projects@bastion.example:2222", "closed"))
        self.assertEqual(c["host"], "localhost:3306")   # as the SSH server sees it: allowed for test behind ssh
        self.assertEqual(c["def"]["ssh"], {"host": "bastion.example", "user": "projects", "port": 2222})   # Edit reopens it as ssh
        r = self.m.db_gui_meta(self.key, self.name, "objects")
        self.assertIn("customer", [o["name"] for o in r["objects"]])
        self.assertEqual(self.conn()["tunnel"]["state"], "open")
        with open(self.args) as f:
            argv = f.read().split("\n")
        self.assertIn("-p", argv)
        self.assertEqual(argv[-1], "bastion.example")
        port = int(argv[argv.index("-L") + 1].split(":")[1])
        self.assertFalse(dbtools.port_free(port))
        self.m.db_gui_tunnel(self.key, self.name, "close")
        self.assertEqual(self.conn()["tunnel"]["state"], "closed")
        for _ in range(20):
            if dbtools.port_free(port):
                break
            time.sleep(0.1)
        self.assertTrue(dbtools.port_free(port))   # Piper's ssh is gone

    def test_edit_closes_the_tunnel(self):
        self.m.db_gui_meta(self.key, self.name, "objects")
        self.assertEqual(self.conn()["tunnel"]["state"], "open")
        d = self.conn()["def"]
        self.m.db_gui_save(self.key, dict(d, engine="mysql", host="127.0.0.1"))   # the fixed DB host must take effect
        self.assertEqual(self.conn()["tunnel"]["state"], "closed")

    def test_concurrent_opens_start_one_ssh(self):
        import threading
        c = [x for x in self.m._db_defs(self.key, self.root) if x["name"] == self.name][0]
        ports, errs = [], []

        def go():
            try:
                ports.append(self.m._db_ssh_open(c, None))
            except Exception as e:   # noqa: BLE001
                errs.append(e)
        th = [threading.Thread(target=go) for _ in range(3)]
        for t in th:
            t.start()
        for t in th:
            t.join(30)
        self.assertEqual(errs, [])
        self.assertEqual(len(set(ports)), 1)
        self.assertEqual(len([p for p in self.m._db_ssh.values() if p.poll() is None]), 1)

    def test_failure_says_why(self):
        os.environ["FAKE_SSH_FAIL"] = "1"
        with self.assertRaises(dbtools.DbError) as e:
            self.m.db_gui_meta(self.key, self.name, "objects")
        self.assertIn("Permission denied", str(e.exception))
        self.assertIn("ssh-agent", str(e.exception))
        self.assertEqual(self.conn()["tunnel"]["state"], "closed")


class FakeStack:
    """A stack provider's tunnel API as dbtools uses it (stackview.py): worktree, tunnel, job, cancel, dash."""

    def __init__(self, root):
        self.root, self.posts, self.cancels, self.states, self.local = root, [], [], ["connecting", "open"], None

    def worktree(self, path):
        return {"root": self.root, "name": "wt"}

    def tunnel(self, root, env, target, port):
        self.posts.append((env, target, port))
        return {"id": "job%d" % len(self.posts), "state": "running"}

    def job(self, jid):
        st = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        local = self.local or self.posts[-1][2]
        return {"id": jid, "state": "running" if st != "failed" else "failed",
                "tunnel": {"state": st, "local": local, "url": "http://127.0.0.1:8888/x" if st == "signin" else ""}}

    def cancel(self, jid):
        self.cancels.append(jid)
        return {}

    def dash(self, root):
        return {"tunnels": [{"env": "test", "target": "mysql", "kind": "k8s", "available": True},
                            {"env": "test", "target": "acme-bridge", "kind": "app", "available": True}]}


class DbTunnelTest(DbBase):
    """Test/prod only through a tunnel Piper opened, on a port Piper chose (2c); closed when unused (7)."""

    def setUp(self):
        DbBase.setUp(self)
        self.td = FakeStack(self.root)
        self.m._stack = lambda: self.td
        self.addCleanup(vars(self.m).pop, "_stack", None)
        self.m._db_tunnels.clear()
        self.old_wait = dbtools.TUNNEL_WAIT_S
        dbtools.TUNNEL_WAIT_S = 3
        d = self.m._db_store()
        d["connections"].append({"name": self.key + "/tun", "project": self.key, "cls": "test", "engine": "sqlite",
                                 "database": self.dbs["test"], "user": "u", "tunnel": {"env": "test", "target": "mysql"}})
        self.m._db_save(d)
        dbtools.write_secret(self.key + "/tun", "pw")

    def tearDown(self):
        dbtools.TUNNEL_WAIT_S = self.old_wait

    def test_open_on_piper_port_and_reuse(self):
        s = self.session()
        r = self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT COUNT(*) FROM customer")
        self.assertEqual(r["rows"], [[2]])
        env, target, port = self.td.posts[0]
        self.assertEqual((env, target), ("test", "mysql"))
        self.assertTrue(dbtools.TUNNEL_PORT_BASE <= port < dbtools.TUNNEL_PORT_BASE + dbtools.TUNNEL_PORT_SPAN + 40)
        self.assertNotIn(port, (3306, 33006, 33007))
        self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.assertEqual(len(self.td.posts), 1)                          # reused
        saved = common.read_json(dbtools.TUNNELS_FILE, {})
        self.assertEqual(list(saved.values())[0]["port"], port)        # survives a piperd restart
        pub = [c for c in self.q(s, "db_connections")["connections"] if c["name"] == self.key + "/tun"][0]
        self.assertEqual(pub["tunnel"]["state"], "open")
        self.assertNotIn("host", pub)

    def test_signin_reported(self):
        self.td.states = ["signin"]
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.assertIn("sign-in", str(e.exception))
        st = list(self.m._db_tunnels.values())[0]
        self.assertEqual((st["state"], st["url"]), ("signin", "http://127.0.0.1:8888/x"))

    def test_app_target_refused(self):
        # acme-bridge is the application (deployment/acme-bridge:5001), not its database
        with self.assertRaises(dbtools.DbError) as e:
            self.m.db_gui_save(self.key, {"name": self.key + "/test/bridge", "cls": "test", "engine": "mysql", "database": "b",
                                          "tunnel": {"env": "test", "target": "acme-bridge"}}, "pw")
        self.assertIn("application", str(e.exception))
        self.m.db_gui_save(self.key, {"name": self.key + "/test/bridge", "cls": "test", "engine": "mysql", "database": "b",
                                      "tunnel": {"env": "test", "target": "mysql"}}, "pw")

    def test_signin_port_taken_named(self):
        self.td.states = ["failed"]
        self.td.job = lambda jid: {"id": jid, "state": "failed", "tunnel": {"state": "failed", "local": self.td.posts[-1][2]},
                                   "error": "error: get-token: authorization error: could not start a local server: no available "
                                            "port: could not listen: listen tcp 127.0.0.1:8888: bind: address already in use"}
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.assertIn("127.0.0.1:8888 is taken", str(e.exception))
        self.assertIn("token is cached", str(e.exception))

    def test_foreign_port_refused(self):
        self.td.local = 33006   # the provider says it listens elsewhere: never trusted
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.assertIn("not on Piper's port", str(e.exception))

    def test_failed_tunnel(self):
        self.td.states = ["failed"]
        s = self.session()
        with self.assertRaises(ValueError) as e:
            self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.assertIn("failed", str(e.exception))

    def test_closed_when_session_ends(self):
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.m._db_reap()
        self.assertEqual(self.td.cancels, [])                          # still in use
        s.state = "exited"
        self.m._db_reap()
        self.assertEqual(self.td.cancels, ["job1"])
        self.assertEqual(self.m._db_tunnels, {})

    def test_closed_when_idle(self):
        s = self.session()
        self.q(s, "db_query", connection=self.key + "/tun", sql="SELECT 1")
        self.m._db_reap(now=time.time() + 31 * 60)
        self.assertEqual(self.td.cancels, ["job1"])


class PureTest(unittest.TestCase):
    def test_idea_harvest(self):
        root = _env.make_project("dbidea", {".idea/dataSources.xml": """<?xml version="1.0"?>
<project><component name="DataSourceManagerImpl">
<data-source source="LOCAL" name="webstore PROD" uuid="1"><jdbc-url>jdbc:mysql://localhost:33007/webstore</jdbc-url>
<user-name>webstore</user-name></data-source>
<data-source source="LOCAL" name="local" uuid="2"><jdbc-url>jdbc:mysql://localhost:3306</jdbc-url></data-source>
</component></project>"""})
        r = dbtools.DbTools._db_idea_harvest(root)
        self.assertEqual(r[0], {"name": "webstore PROD", "engine": "mysql", "user": "webstore", "database": "webstore",
                                "cls_guess": "prod", "file": "dataSources.xml"})
        self.assertNotIn("33007", json.dumps(r))
        ssh = _env.make_project("dbidea3", {
            ".idea/sshConfigs.xml": '<?xml version="1.0"?><project><component name="SshConfigs"><configs>'
                                    '<sshConfig host="test-tiles.example.com" id="s1" keyPath="$USER_HOME$/.ssh/id_ed25519" port="22" '
                                    'username="projects" /></configs></component></project>',
            ".idea/dataSources.xml": '<?xml version="1.0"?><project><component name="DataSourceManagerImpl">'
                                     '<data-source name="TEST" uuid="u1"><jdbc-url>jdbc:mysql://localhost:3306</jdbc-url>'
                                     '</data-source></component></project>',
            ".idea/dataSources.local.xml": '<?xml version="1.0"?><project><component name="dataSourceStorageLocal">'
                                           '<data-source name="TEST" uuid="u1"><user-name>shop_app</user-name><schema-mapping>'
                                           '<introspection-scope><node kind="schema"><name qname="@" /><name qname="shop_app" />'
                                           '</node></introspection-scope></schema-mapping><ssh-properties><enabled>true</enabled>'
                                           '<ssh-config-id>s1</ssh-config-id></ssh-properties></data-source></component></project>'})
        r = dbtools.DbTools._db_idea_harvest(ssh)
        self.assertEqual(r, [{"name": "TEST", "engine": "mysql", "user": "shop_app", "database": "shop_app", "cls_guess": "test",
                              "file": "dataSources.xml", "ssh": {"host": "test-tiles.example.com", "user": "projects", "key": "~/.ssh/id_ed25519"},
                              "db_host": "127.0.0.1", "db_port": 3306}])
        bad = _env.make_project("dbidea2", {".idea/dataSources.xml": '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><x/>'})
        self.assertEqual(dbtools.DbTools._db_idea_harvest(bad), [])

    def test_default_schema(self):
        # PostgreSQL / SQL Server name the database in the identity; the tree must open public / dbo
        self.assertEqual(dbtools.default_schema("postgres", "e2e", ["app", "public"]), "public")
        self.assertEqual(dbtools.default_schema("postgres", "e2e", ["app"]), "app")
        self.assertEqual(dbtools.default_schema("mssql", "S5_Agenda", ["AppDb", "S5_Agenda"]), "S5_Agenda")   # its meta lists databases
        self.assertEqual(dbtools.default_schema("mysql", "shop_live", ["shop_live", "mysql"]), "shop_live")
        self.assertEqual(dbtools.default_schema("oracle", "APP", ["APP"]), "APP")


class NoLsofTest(unittest.TestCase):
    """A stock WSL Ubuntu has no lsof: the listener checks fall back to ss."""
    SS = ('LISTEN 0 128 127.0.0.1:43211 0.0.0.0:* users:(("ssh",pid=4242,fd=5))\n'
          'LISTEN 0 128 [::1]:43211 [::]:* users:(("ssh",pid=4243,fd=6))\n')

    def run_(self, argv, **kw):
        if argv[0] == "lsof":
            raise FileNotFoundError("lsof")
        self.assertEqual(argv[:2], ["ss", "-Hltnp"])
        return subprocess.CompletedProcess(argv, 0, self.SS, "")

    def test_ss_fallback(self):
        from unittest import mock
        with mock.patch.object(dbtools.subprocess, "run", side_effect=self.run_):
            self.assertEqual(dbtools.listener_pid(43211), 4242)
            self.assertEqual(dbtools.port_holder(43211), " by ssh (pid 4242)")
            self.assertIsNone(dbtools.listener_pid(43212))


class DriverInstallTest(unittest.TestCase):
    """Install drivers on Debian/Ubuntu without python3-venv: `python -m venv` fails after it made a venv
    with python and no pip, and every later try (python3-venv installed by then) died on "No module named
    pip" until someone deleted ~/.piper/dbvenv by hand. Offline: a local wheel stands in for the drivers."""

    def setUp(self):
        import base64
        import hashlib
        import tempfile
        import zipfile
        from unittest import mock
        self.tmp = tempfile.mkdtemp(dir=_env.ROOT)
        self.venv = os.path.join(self.tmp, "dbvenv")
        whl = os.path.join(self.tmp, "piperfakedrv-1.0-py3-none-any.whl")
        files = {"piperfakedrv.py": "OK = 1\n",
                 "piperfakedrv-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: piperfakedrv\nVersion: 1.0\n",
                 "piperfakedrv-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: t\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
        rec = "".join("%s,sha256=%s,%d\n" % (n, base64.urlsafe_b64encode(hashlib.sha256(b.encode()).digest()).rstrip(b"=").decode(),
                                             len(b.encode())) for n, b in files.items())
        with zipfile.ZipFile(whl, "w") as z:
            for n, b in files.items():
                z.writestr(n, b)
            z.writestr("piperfakedrv-1.0.dist-info/RECORD", rec + "piperfakedrv-1.0.dist-info/RECORD,,\n")
        for patch in (mock.patch.object(dbtools, "VENV", self.venv), mock.patch.object(dbtools, "DRIVER_PKGS", [whl])):
            patch.start()
            self.addCleanup(patch.stop)

    def install(self):
        class M:
            _db_install = {"state": "", "error": "", "log": ""}

            def bump(self):
                pass
        m = M()
        dbtools.DbTools._db_install_run(m)
        return m._db_install

    def test_pipless_venv_is_recreated(self):
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", self.venv], check=True, capture_output=True)
        st = self.install()
        self.assertEqual(st["state"], "done", st["error"])
        r = subprocess.run([os.path.join(self.venv, "bin", "python"), "-c", "import pip, piperfakedrv"], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_failed_venv_leaves_nothing_behind(self):
        from unittest import mock
        fake = os.path.join(self.tmp, "python-no-ensurepip")
        with open(fake, "w") as f:   # what Debian's python3 does without python3-venv: a venv with python, no pip
            f.write('#!/bin/sh\nmkdir -p "$3/bin" && cp %s "$3/bin/python"\n' % sys.executable
                    + "echo 'The virtual environment was not created successfully because ensurepip is not available.'\n"
                    + "for i in $(seq 40); do echo \"line $i of a long explanation\"; done\necho 'Failing command: x'\nexit 1\n")
        os.chmod(fake, 0o755)
        with mock.patch.object(dbtools.sys, "executable", fake):
            st = self.install()
        self.assertEqual(st["state"], "failed")
        self.assertFalse(os.path.exists(self.venv), "a half-made venv would break the next try")
        self.assertIn("sudo apt install python%d.%d-venv" % sys.version_info[:2], st["error"])
        self.assertIn("Failing command: x", st["error"][:400], "the GUI shows the first 400 characters: the cause's end too")
        st = self.install()   # python3-venv installed meanwhile: the next try just works
        self.assertEqual(st["state"], "done", st["error"])


class PiperdDbRoutesTest(unittest.TestCase):
    def test_routes_need_gui_scope(self):
        import piperd
        import test_piperd as tp

        class M(tp.FakeManager):
            def db_gui_connections(self, project, session=""):
                return {"project": project, "connections": []}

            def db_gui_query(self, *a):
                return {"rows": [[1]], "args": list(a)}

            def db_status(self):
                return {"ready": True}

            def db_install_start(self):
                return {"installing": True}

            def db_gui_running(self, *a):
                return {"running": [], "args": list(a)}

            def db_gui_cancel(self, *a):
                return {"cancelled": 1, "args": list(a)}
        old = piperd.MANAGER
        piperd.MANAGER = M()
        self.addCleanup(setattr, piperd, "MANAGER", old)
        tok, tty = common.mint_token("gui"), common.mint_token("tty")
        code, _, body = tp.get("/api/db/connections?project=p", token=tok)
        self.assertEqual((code, json.loads(body)["project"]), (200, "p"))
        self.assertEqual(tp.get("/api/db/connections?project=p", token=tty)[0], 403)
        code, _, body = tp.post("/api/db/query", {"project": "p", "connection": "c", "sql": "SELECT 1"}, token=tok)
        self.assertEqual((code, json.loads(body)["rows"]), (200, [[1]]))
        self.assertEqual(tp.post("/api/db/query", {"sql": "SELECT 1"}, token=tty)[0], 403)
        self.assertEqual(tp.post("/api/db/install", {}, token=tok)[0], 202)
        self.assertEqual(tp.post("/api/db/nope", {}, token=tok)[0], 404)
        self.assertEqual(tp.get("/api/db/status")[0], 401)
        # L7: the running statements and the cancel of a session's read
        code, _, body = tp.get("/api/db/running?project=p&connection=c", token=tok)
        self.assertEqual((code, json.loads(body)["args"]), (200, ["p", "c", ""]))
        self.assertEqual(tp.get("/api/db/running?project=p", token=tty)[0], 403)
        code, _, body = tp.post("/api/db/cancel", {"project": "p", "connection": "c", "owner": "s1"}, token=tok)
        self.assertEqual((code, json.loads(body)["args"]), (200, ["p", "c", "", "s1"]))



ORA = os.environ.get("PIPER_DB_E2E_ORACLE", "")      # host:port:user:password:service
MSSQL = os.environ.get("PIPER_DB_E2E_MSSQL", "")    # host:port:user:password:database (the seed of tests/piper-e2e/dbseed)


@unittest.skipUnless(ORA and MSSQL, "PIPER_DB_E2E_ORACLE / PIPER_DB_E2E_MSSQL not set (run with the drivers' venv python)")
class LiveOracleMssqlManagerTest(unittest.TestCase):
    """The session tools against live Oracle and SQL Server (saved dev connections): what an agent writes in lower case, a cut CLOB, the preview gate."""

    @classmethod
    def setUpClass(cls):
        oh, op, ou, opw, osvc = ORA.split(":", 4)
        cls.mh, mp, mu, mpw, cls.mdb = MSSQL.split(":", 4)
        cls.root = _env.make_project("dbtora", {"src/A.java": "class A {}\n"})
        cls.key = "team-x/dbtora"
        _env.register(cls.key, cls.root)
        os.environ["PIPER_DB_PYTHON"] = sys.executable
        cls.m = _env.manager(cls)
        cls.ora = cls.key + "/dev/oracle"
        cls.ms = cls.key + "/dev/mssql"
        cls.m.db_gui_save(cls.key, {"name": cls.ora, "cls": "dev", "engine": "oracle", "host": oh, "port": int(op),
                                    "user": ou, "database": osvc}, opw)
        cls.m.db_gui_save(cls.key, {"name": cls.ms, "cls": "dev", "engine": "mssql", "host": cls.mh, "port": int(mp),
                                    "user": mu, "database": cls.mdb}, mpw)

    @classmethod
    def tearDownClass(cls):
        for w in list(cls.m._db_workers.values()):
            w.kill()
        os.environ.pop("PIPER_DB_PYTHON", None)

    def setUp(self):
        import sessions
        self.asked, self.answer = [], True

        def ask(kind, s, summary, payload, timeout=840, always="", result=None):
            self.asked.append((kind, summary))
            if result is not None:
                result.update(always=False, answered=True)
            return self.answer
        self.m.ask = ask
        self.s = sessions.Session(kind="project", project=self.key, path=self.root, state="ready")
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(self.m.sessions.pop, self.s.id, None)

    def t(self, tool, **a):
        self.m._db_req.t0 = time.time()
        return self.m.tool(self.s, tool, a)

    def test_both_listed_and_readable(self):
        names = [c["name"] for c in self.t("db_connections")["connections"]]
        self.assertIn(self.ora, names)
        self.assertIn(self.ms, names)
        r = self.t("db_query", connection=self.ora, sql="SELECT name FROM customer WHERE id = 1")
        self.assertEqual(r["rows"], [["Jürgen Müller"]])
        r = self.t("db_query", connection=self.ms, sql="SELECT name FROM customer WHERE id = 4")
        self.assertEqual(r["rows"], [["Emoji 🚀 עברית"]])

    def test_oracle_lower_case_names_like_an_agent_writes_them(self):
        d = self.t("db_describe", connection=self.ora, schema="shop", name="orders")
        self.assertEqual([k.lower() for k in d["primary_key"]], ["id"])
        self.assertEqual(d["foreign_keys"][0]["ref_table"].lower(), "customer")
        objs = [o["name"] for o in self.t("db_objects", connection=self.ora, schema="shop")["objects"]]
        self.assertIn("CUSTOMER", objs)

    def test_oracle_cut_clob_by_its_lower_case_column(self):
        r = self.t("db_query", connection=self.ora, sql="SELECT big FROM orders WHERE id = 1")
        self.assertTrue(r["truncated_cells"], r)
        v = self.t("db_cell", result_id=r["result_id"], row=0, column="big")
        self.assertGreaterEqual(len(v["value"]), 256 * 1024 - 10)

    def test_oracle_schema_qualified_write_has_no_guaranteed_rollback(self):
        self.answer = False
        with self.assertRaises(ValueError):
            self.t("db_execute", connection=self.ora, sql="UPDATE shop.customer SET note = 'x' WHERE id = 3", reason="t")
        self.assertIn("No guaranteed rollback", self.asked[-1][1])
        self.assertNotIn("unknown to information_schema", self.asked[-1][1])

    def test_mssql_write_has_no_preview_and_says_why(self):
        self.answer = False
        with self.assertRaises(ValueError):
            self.t("db_execute", connection=self.ms, sql="UPDATE customer SET note = N'x' WHERE id = 3", reason="t")
        self.assertIn("No guaranteed rollback", self.asked[-1][1])


class HistoryParamsTest(unittest.TestCase):
    """GUI grid writes keep their values for the history (runnable in the console) - never a masked column's."""

    def test_values_kept_masked_columns_and_big_ones_not(self):
        st = [{"sql": 'UPDATE "CUSTOMER" SET "NOTE" = %s WHERE "ID" = %s', "params": ["x", 5]},
              {"sql": "UPDATE `customer` SET `email` = %s WHERE `id` = %s", "params": ["kun@example.org", 1]},
              {"sql": "UPDATE [users] SET [password_hash] = %s WHERE [id] = %s", "params": ["hunter2", 2]},
              {"sql": "DELETE FROM t WHERE id = 9"}]
        self.assertEqual(dbtools._hist_params(st, dbsql.MASK_WORDS), [["x", 5], None, None, None])
        self.assertIsNone(dbtools._hist_params([{"sql": "UPDATE t SET a = %s", "params": ["y" * 9000]}], dbsql.MASK_WORDS))
        self.assertIsNone(dbtools._hist_params([{"sql": "DELETE FROM t"}], dbsql.MASK_WORDS))
        self.assertNotIn("glpat-abcdefghijklmnopqrst", json.dumps(dbtools._hist_params(
            [{"sql": "UPDATE t SET note = %s", "params": ["glpat-abcdefghijklmnopqrst"]}], dbsql.MASK_WORDS)))


if __name__ == "__main__":
    unittest.main()


E2E = os.environ.get("PIPER_DB_E2E_MYSQL", "")


@unittest.skipUnless(E2E, "PIPER_DB_E2E_MYSQL=host:port:user:password:db not set (run with the drivers' venv python)")
class LiveMySQLManagerTest(unittest.TestCase):
    """The whole path against a throwaway MySQL 8: a saved dev connection,
    preview on InnoDB leaves the data unchanged while the card waits, MEMORY gets no preview and
    nothing runs without the card (2a), the read connection refuses writes (2)."""

    @classmethod
    def setUpClass(cls):
        import pymysql
        host, port, user, pw, db = E2E.split(":")
        cls.raw = pymysql.connect(host=host, port=int(port), user=user, password=pw, autocommit=True)
        k = cls.raw.cursor()
        for s in ("CREATE DATABASE IF NOT EXISTS shop_t", "DROP USER IF EXISTS 'shopt'@'%'",
                  "CREATE USER 'shopt'@'%' IDENTIFIED WITH mysql_native_password BY 'live-pw'",
                  "GRANT ALL PRIVILEGES ON `shop_t`.* TO 'shopt'@'%'",
                  "DROP TABLE IF EXISTS shop_t.item", "DROP TABLE IF EXISTS shop_t.mem",
                  "DROP TABLE IF EXISTS shop_t.cat",
                  "CREATE TABLE shop_t.cat (id INT PRIMARY KEY, title VARCHAR(50)) ENGINE=InnoDB",
                  "CREATE TABLE shop_t.item (id INT PRIMARY KEY, cat_id INT, price INT, email VARCHAR(80), "
                  "FOREIGN KEY (cat_id) REFERENCES shop_t.cat(id)) ENGINE=InnoDB",
                  "CREATE TABLE shop_t.mem (x INT) ENGINE=MEMORY",
                  "INSERT INTO shop_t.cat VALUES (1, 'a')",
                  "INSERT INTO shop_t.item VALUES (1, 1, 10, 'a@x.example'), (2, 1, 20, 'b@x.example')"):
            k.execute(s)
        cls.root = _env.make_project("dbtlive", {"src/A.java": "class A {}\n"})
        cls.key = "team-x/dbtlive"
        _env.register(cls.key, cls.root)
        os.environ["PIPER_DB_PYTHON"] = sys.executable
        cls.m = _env.manager(cls)
        cls.conn = cls.key + "/dev/shop_t"
        cls.m.db_gui_save(cls.key, {"name": cls.conn, "cls": "dev", "engine": "mysql", "host": host, "port": int(port),
                                    "user": "shopt", "database": "shop_t"}, "live-pw")

    @classmethod
    def tearDownClass(cls):
        for w in list(cls.m._db_workers.values()):
            w.kill()
        os.environ.pop("PIPER_DB_PYTHON", None)

    def setUp(self):
        import sessions
        self.asked, self.answer, self.seen = [], True, []
        self.m._db_recent.clear()

        def ask(kind, s, summary, payload, timeout=840, always="", result=None):
            self.asked.append((kind, summary))
            self.seen.append(self.x("SELECT SUM(price) FROM shop_t.item")[0][0])   # while the card waits
            if result is not None:
                result.update(always=False, answered=True)
            return self.answer
        self.m.ask = ask
        self.s = sessions.Session(kind="project", project=self.key, path=self.root, state="ready")
        self.m.sessions[self.s.id] = self.s
        self.addCleanup(self.m.sessions.pop, self.s.id, None)

    def x(self, sql):
        k = self.raw.cursor()
        k.execute(sql)
        return k.fetchall()

    def t(self, tool, **a):
        self.m._db_req.t0 = time.time()
        return self.m.tool(self.s, tool, a)

    def test_listing_and_read(self):
        names = [c["name"] for c in self.t("db_connections")["connections"]]
        self.assertIn(self.conn, names)
        r = self.t("db_query", sql="SELECT id, price FROM item ORDER BY id")   # the only dev connection
        self.assertEqual(r["rows"], [[1, 10], [2, 20]])
        d = self.t("db_describe", name="item")
        self.assertEqual(d["primary_key"], ["id"])
        self.assertEqual(d["foreign_keys"][0]["ref_table"], "cat")
        self.assertIn("CREATE TABLE", d["ddl"])
        self.assertEqual(self.t("db_describe", name="cat")["referenced_by"][0]["table"], "item")
        e = self.t("db_explain", sql="SELECT * FROM item WHERE id = 1")
        self.assertTrue(e["rows"])

    def test_read_path_cannot_write(self):
        with self.assertRaises(ValueError):
            self.t("db_query", sql="UPDATE item SET price = 0")
        # past the text check, the server's READ ONLY transaction holds (GUI console path)
        with self.assertRaises(ValueError) as e:
            self.m.db_gui_query(self.key, self.conn, "SELECT * FROM item WHERE id IN (SELECT 1) FOR UPDATE")
        self.assertEqual(self.x("SELECT SUM(price) FROM shop_t.item")[0][0], 30)

    def test_innodb_preview_then_commit(self):
        r = self.t("db_execute", sql="UPDATE item SET price = price + 1 WHERE cat_id = 1", reason="e2e")
        self.assertEqual(self.seen, [30])                 # the preview was rolled back before the card
        self.assertIn("⚠ affects 2 rows", self.asked[0][1])
        self.assertEqual(r["rowcount"], [2])
        self.assertEqual(self.x("SELECT SUM(price) FROM shop_t.item")[0][0], 32)
        self.x("UPDATE shop_t.item SET price = id * 10")

    def test_memory_table_no_preview_no_write_without_card(self):
        self.answer = False
        with self.assertRaises(ValueError):
            self.t("db_execute", sql="INSERT INTO mem VALUES (1)")
        self.assertIn("⚠ no preview: table mem is MEMORY", self.asked[0][1])
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.mem")[0][0], 0)   # nothing ran (2a)
        self.answer = True
        self.t("db_execute", sql="INSERT INTO mem VALUES (2)")
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.mem")[0][0], 1)
        self.x("DELETE FROM shop_t.mem")

    def test_stored_function_blocks_preview_and_explain(self):
        """A function writing to a MyISAM table would write for real in a rolled-back preview."""
        for q in ("DROP FUNCTION IF EXISTS shop_t.f_log", "DROP TABLE IF EXISTS shop_t.wlog",
                  "CREATE TABLE shop_t.wlog (x INT) ENGINE=MyISAM",
                  "CREATE FUNCTION shop_t.f_log() RETURNS INT DETERMINISTIC MODIFIES SQL DATA "
                  "BEGIN INSERT INTO shop_t.wlog VALUES (1); RETURN 1; END"):
            self.x(q)
        self.answer = False
        with self.assertRaises(ValueError):
            self.t("db_execute", sql="UPDATE item SET price = price WHERE f_log() = 1")
        self.assertIn("calls stored function f_log", self.asked[0][1])
        with self.assertRaises(ValueError):
            self.t("db_explain", sql="UPDATE item SET price = price WHERE f_log() = 1")
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.wlog")[0][0], 0)   # nothing ran
        # review finding: the same call hidden in a versioned comment
        self.asked.clear()
        with self.assertRaises(ValueError):
            self.t("db_execute", sql="UPDATE item SET price = price /*!50000 + f_log() - 1 */ WHERE id = 1")
        self.assertIn("executable comment", self.asked[0][1])
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.wlog")[0][0], 0)
        with self.assertRaises(ValueError):   # a schema-qualified call: unknown, no preview either
            self.t("db_execute", sql="UPDATE item SET price = price WHERE shop_t.f_log() = 1")
        self.assertIn("another schema", self.asked[1][1])
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.wlog")[0][0], 0)
        # the read path: the READ ONLY transaction refuses the function's write
        with self.assertRaises(ValueError):
            self.t("db_query", sql="SELECT f_log()")
        self.assertEqual(self.x("SELECT COUNT(*) FROM shop_t.wlog")[0][0], 0)

    def test_masking_alias_and_expression(self):
        """On a masked connection an alias or an expression does not unmask personal data."""
        d = self.m._db_store()
        for cc in d["connections"]:
            if cc["name"] == self.conn:
                cc["mask"] = True
        if not any(cc["name"] == self.conn for cc in d["connections"]):
            d["connections"].append({"name": self.conn, "project": self.key, "cls": "dev", "mask": True})
        self.m._db_save(d)
        self.addCleanup(lambda: self.m._db_save(dict(self.m._db_store(), connections=[
            cc for cc in self.m._db_store()["connections"] if cc["name"] != self.conn])))
        self.x("UPDATE shop_t.item SET email = 'a@x.example' WHERE id = 1")
        r = self.t("db_query", sql="SELECT id, email AS e, CONCAT('<', email, '>') AS c, price FROM item WHERE id = 1")
        self.assertEqual(r["rows"][0][1:3], [dbtools.MASK, dbtools.MASK])
        self.assertEqual(r["rows"][0][0], 1)          # a plain column with its own source stays
        self.assertNotEqual(r["rows"][0][3], dbtools.MASK)
        r = self.t("db_query", sql="SELECT COUNT(*) AS n, GROUP_CONCAT(email) AS g FROM item")
        self.assertNotEqual(r["rows"][0][0], dbtools.MASK)   # a number stays
        self.assertEqual(r["rows"][0][1], dbtools.MASK)      # computed text over the rows is masked
        with self.assertRaises(ValueError) as e:              # a quoted value in an error is masked too
            self.t("db_query", sql="SELECT CAST(email AS SIGNED) + (SELECT 1 FROM item WHERE email = 'a@x.example' AND x())")
        self.assertNotIn("a@x.example", str(e.exception))

    def test_full_scan_warning(self):
        self.answer = False
        with self.assertRaises(ValueError):
            self.t("db_execute", sql="UPDATE item SET price = price WHERE email LIKE '%x%'")
        self.assertIn("full table scan of item", self.asked[0][1])

    def test_gui_execute_and_kill(self):
        r = self.m.db_gui_execute(self.key, self.conn, [{"sql": "UPDATE item SET price=%s WHERE id=%s", "params": [11, 1]}])
        self.assertEqual(r["rowcount"], [1])
        self.x("UPDATE shop_t.item SET price = id * 10")
