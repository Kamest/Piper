"""Anonymization rules and the personal-data query guard (dbanon.py)."""
import json
import unittest

import _env  # noqa: F401
import dbanon
import dbsql

RULES = {"tables": {
    "T_User": {"columns": {"firstName": {"op": "token", "prefix": "A_", "when": "notnull"},
                           "login": {"op": "email", "domain": "anon.example", "when": {"like": "%@%"}},
                           "phone": {"op": "const", "value": "555000000"},
                           "socialId": {"op": "column", "column": "id"},
                           "data": {"op": "json", "paths": {"$.customer.zip": {"op": "const", "value": 10000},
                                                            "$.customer.name": {"op": "column", "column": "id", "prefix": "name"},
                                                            "$.customer.email": {"op": "email", "domain": "anon.example"}}}}},
    "t_order_note": {"columns": {}, "drop_rows": [{"column": "name", "equals": "draft"}]},
    "v_people": {"mask_all": True},
}}


class RulesTest(unittest.TestCase):
    def test_validate_lowercases_and_keeps_shape(self):
        r = dbanon.validate(RULES)
        self.assertEqual(sorted(r["tables"]), ["t_order_note", "t_user", "v_people"])
        self.assertEqual(r["tables"]["t_user"]["columns"]["socialid"], {"op": "column", "column": "id", "prefix": ""})
        self.assertTrue(r["tables"]["v_people"]["mask_all"])

    def test_malformed_rules_are_refused(self):
        for bad in (None, {}, {"tables": []}, {"tables": {"t": {"columns": {"c": {"op": "drop"}}}}},
                    {"tables": {"t": {"columns": {"c": {"op": "const", "value": [1]}}}}},
                    {"tables": {"t": {"columns": {"c": {"op": "json", "paths": {"$[0]": {"op": "mask"}}}}}}},
                    {"tables": {"t": {"columns": {"c": {"op": "token", "when": "sometimes"}}}}},
                    {"tables": {"t": {"drop_rows": [{"where": "x"}]}}}):
            with self.subTest(bad=bad), self.assertRaises(dbanon.RulesError):
                dbanon.validate(bad)

    def test_two_extensions_disagreeing_on_a_column_mask_it(self):
        a = {"tables": {"t": {"columns": {"c": {"op": "const", "value": "x"}}}}}
        b = {"tables": {"T": {"columns": {"c": {"op": "token"}, "d": {"op": "mask"}}}}}
        m = dbanon.merge([a, None, b])
        self.assertEqual(m["tables"]["t"]["columns"]["c"], {"op": "mask"})
        self.assertEqual(m["tables"]["t"]["columns"]["d"], {"op": "mask"})
        self.assertIsNone(dbanon.merge([None, None]))

    def test_names_of_the_tables_a_query_reads(self):
        r = dbanon.validate(RULES)
        self.assertEqual(dbanon.column_names(r, [("", "t_user")]), {"firstname", "login", "phone", "socialid", "data"})
        self.assertEqual(dbanon.column_names(r, [("", "other")]), set())
        self.assertIn("login", dbanon.column_names(r, None))   # unknown tables: every rule column
        self.assertEqual(dbanon.masked_tables(r, [("x", "V_PEOPLE")]), ["v_people"])


class TransformTest(unittest.TestCase):
    def setUp(self):
        self.t = dbanon.validate(RULES)["tables"]["t_user"]["columns"]

    def test_pseudonyms_are_consistent_and_hide_the_value(self):
        a = dbanon.transform(self.t["firstname"], "Jan")
        self.assertRegex(a, r"^A_[0-9a-f]{8}$")
        self.assertEqual(a, dbanon.transform(self.t["firstname"], "Jan"))
        self.assertNotEqual(a, dbanon.transform(self.t["firstname"], "Eva"))
        e = dbanon.transform(self.t["login"], "jan@shop.example")
        self.assertRegex(e, r"^[0-9a-f]{16}@anon\.example$")
        self.assertNotIn("jan", e)
        self.assertIsNone(dbanon.transform(self.t["firstname"], None))   # NULL stays NULL

    def test_like_condition_leaves_other_values(self):
        self.assertEqual(dbanon.transform(self.t["login"], "admin"), "admin")
        self.assertFalse(dbanon.applies(self.t["login"], "admin"))
        self.assertTrue(dbanon._like("A@B", "%@%"))
        self.assertTrue(dbanon._like("50%", "50\\%"))
        self.assertTrue(dbanon._like("Jan.Novák@x", "%novak@%"))   # accents and case: MySQL's default collation
        self.assertTrue(dbanon._like("ＪＡＮ@x", "jan@%"))
        self.assertFalse(dbanon._like("500", "50\\%"))

    def test_const_and_column_copy(self):
        self.assertEqual(dbanon.transform(self.t["phone"], "+420 601 000 001"), "555000000")
        self.assertEqual(dbanon.transform(self.t["socialid"], "sn-1", lambda c: (True, 42)), "42")
        self.assertEqual(dbanon.transform(self.t["socialid"], "sn-1", lambda c: (False, None)), dbanon.MASK)
        self.assertEqual(dbanon.transform(self.t["socialid"], "sn-1"), dbanon.MASK)

    def test_json_paths(self):
        doc = json.dumps({"customer": {"zip": "11000", "name": "Jan Novak", "email": "jan@x.example", "city": "Brno"},
                          "total": 3})
        out = json.loads(dbanon.transform(self.t["data"], doc, lambda c: (True, 7)))
        self.assertEqual(out["customer"]["zip"], 10000)
        self.assertEqual(out["customer"]["name"], "name7")
        self.assertRegex(out["customer"]["email"], r"@anon\.example$")
        self.assertEqual(out["customer"]["city"], "Brno")   # not a rule path: kept, as in the anonymized dump
        self.assertEqual(out["total"], 3)
        missing = json.loads(dbanon.transform(self.t["data"], json.dumps({"customer": {"zip": None}}), lambda c: (True, 7)))
        self.assertEqual(missing, {"customer": {"zip": None}})   # absent / null keys stay so
        self.assertEqual(dbanon.transform(self.t["data"], "{not json"), dbanon.MASK)
        self.assertEqual(dbanon.transform(self.t["data"], "x" * (dbanon.MAX_JSON + 1)), dbanon.MASK)


class GuardTest(unittest.TestCase):
    NAMES = {"login", "firstname", "contactphone"}

    def reasons(self, sql, names=None, views=None):
        names = self.NAMES if names is None else names
        found, opaque = dbanon.personal(sql, "mysql", lambda t: t.lower() in names or dbsql.masked(t))
        return dbanon.guard(sql, "mysql", found, opaque, views)

    def test_queries_that_select_rows_by_personal_data_ask(self):
        for sql in ("SELECT id FROM t_user WHERE email LIKE 'a%'",
                    "SELECT id FROM t_user u WHERE u.login = %s",
                    "SELECT id FROM t_user WHERE (email = 'x' OR id = 1)",
                    "SELECT id FROM t_user WHERE LENGTH(firstName) > 3",
                    "SELECT id FROM t_user WHERE email IN ('a@x.example', 'b@x.example')",
                    "SELECT id FROM t_user ORDER BY email",
                    "SELECT COUNT(*) FROM t_user GROUP BY phone",
                    "SELECT email, id FROM t_user ORDER BY 1",
                    "SELECT * FROM t_user ORDER BY 2",
                    "SELECT id, ROW_NUMBER() OVER (PARTITION BY email) FROM t_user",
                    "SELECT id FROM t WHERE 'a@x.example' IN (SELECT email FROM t_user)",
                    "SELECT id FROM t WHERE EXISTS (SELECT 1 FROM t_user u WHERE u.login = 'a')",
                    "SELECT email FROM t_user UNION SELECT 'a@x.example'",
                    "SELECT d.x FROM (SELECT login AS x FROM t_user) d WHERE d.x = 'a'",
                    "SELECT firstName AS f, COUNT(*) FROM t_user GROUP BY id HAVING f = 'Jan'",
                    "SELECT * FROM t_user u JOIN (SELECT 'a@x.example' AS v) l ON l.v = u.email",
                    "WITH c(a) AS (SELECT email FROM t_user) SELECT * FROM c WHERE a = 'x'",
                    "SELECT jt.v FROM t_user, JSON_TABLE(t_user.login, '$' COLUMNS(v TEXT PATH '$')) jt",
                    "SELECT id FROM t_user WHERE email <> ''",
                    "SELECT id FROM t_user WHERE email = NULL"):
            with self.subTest(sql=sql):
                self.assertTrue(self.reasons(sql))

    def test_queries_that_reveal_no_value_run(self):
        for sql in ("SELECT password, firstName, lastName, CONCAT(firstName, ' - ', lastName), o.number, o.contactPhone "
                    "FROM t_user u LEFT JOIN t_order o ON o.customer = u.login WHERE u.id IN (6866, 6242)",
                    "SELECT id FROM t_user WHERE email IS NOT NULL AND id = 3",
                    "SELECT id FROM t_user WHERE email IS NULL",
                    "SELECT u.id FROM t_user u JOIN t_mail m ON m.address = u.email",
                    "SELECT u.id FROM t_user u JOIN t_mail m ON m.address <=> u.email",
                    "SELECT u.id FROM t_user u JOIN t_mail m USING (email)",
                    "SELECT TRIM(email), COUNT(*) FROM t_user WHERE id = 1",
                    "SELECT d.id FROM (SELECT login AS id FROM t_user) d",   # no filter (the column is masked, dbtools)
                    "SELECT id, name FROM t_product WHERE name LIKE 'a%' ORDER BY name"):
            with self.subTest(sql=sql):
                self.assertEqual(self.reasons(sql), [])

    def test_join_equality_needs_a_flat_query(self):
        self.assertTrue(self.reasons("SELECT id FROM (SELECT id FROM t_user u WHERE u.email = m.address) d"))

    def test_unreadable_sql_and_masked_views_ask(self):
        self.assertTrue(self.reasons("SELECT 'unterminated FROM t"))
        self.assertTrue(self.reasons("SELECT 1; SELECT 2"))
        self.assertTrue(self.reasons("SELECT id FROM v_people WHERE note = 'x'", views=["v_people"]))
        self.assertEqual(self.reasons("SELECT * FROM v_people", views=["v_people"]), [])

    def test_guard_bypasses_of_the_independent_review_ask(self):
        """2026-11-21 review: a quoted alias, a subquery's implicit expression name, a select-list condition
        (NULL / error / time oracle), an alias of a literal as the other side of `=`, a USING subquery."""
        for sql, eng in (("SELECT id, email AS 'e' FROM t_user ORDER BY e DESC", "mysql"),
                         ('SELECT id, login AS "e" FROM t_user HAVING e', "mysql"),
                         ("SELECT id FROM (SELECT id, LEFT(login, 1) FROM t_user) d WHERE d.`LEFT(login, 1)` = 'a'", "mysql"),
                         ("SELECT id FROM (SELECT id, LEFT(login, 1) FROM t_user) d WHERE d.left = 'a'", "postgres"),
                         ("SELECT id, CASE WHEN email LIKE 'a%' THEN 1 END FROM t_user", "mysql"),
                         ("SELECT id, NULLIF(login LIKE 'a%', 0) FROM t_user", "mysql"),
                         ("SELECT IF(login LIKE 'a%', EXP(710), 0) FROM t_user", "mysql"),
                         ("SELECT SLEEP(ASCII(login)) FROM t_user", "mysql"),
                         ("SELECT id, login = 'jan' FROM t_user", "mysql"),
                         ("SELECT id, login, 'abc' AS g FROM t_user HAVING login = g", "mysql"),
                         ("SELECT id, 'abc' AS g, login FROM t_user HAVING g = login", "mysql"),
                         ("DELETE FROM t_x USING (SELECT id FROM t_user WHERE login LIKE 'a%') s WHERE t_x.id = s.id",
                          "postgres")):
            with self.subTest(sql=sql):
                found, opaque = dbanon.personal(sql, eng, lambda t: t.lower() in self.NAMES or dbsql.masked(t))
                self.assertTrue(dbanon.guard(sql, eng, found, opaque, None))
        self.assertEqual(self.reasons("SELECT CONCAT(firstName, ' ', lastName), email IS NULL FROM t_user"), [])
        self.assertEqual(self.reasons("SELECT * FROM (SELECT id, LEFT(login, 1) FROM t_user) d"), [])

    def test_computing_over_personal_data_in_the_select_list_asks_unless_it_cannot_fail(self):
        """2026-11-22 review: an error / time oracle by arithmetic (`EXP(ASCII(SUBSTR(email, 1, 1)) * 9)` fails
        for some letters) and `TABLE t ORDER BY 3` (a positional order over every column)."""
        for sql in ("SELECT EXP(ASCII(SUBSTR(email, 1, 1)) * 9) FROM t_user WHERE id = 5",
                    "SELECT ASCII(login) + 18446744073709551515 FROM t_user",
                    "SELECT CAST(email AS JSON) FROM t_user",
                    "SELECT -email, id FROM t_user",
                    "SELECT my_func(email) FROM t_user",
                    "SELECT COUNT(email) OVER () FROM t_user",
                    "SELECT * FROM (SELECT REPEAT(login, 9) AS r FROM t_user) d",
                    "SELECT id, JSON_TYPE(REPLACE(LEFT(login, 2), 'jo', '1')) FROM t_user",   # Invalid JSON text
                    "SELECT JSON_EXTRACT(email, '$.a') FROM t_user", "SELECT email->>'$.b' FROM t_user",
                    "TABLE t_user ORDER BY 3",
                    "SELECT 1, 2, 3 UNION TABLE t_user ORDER BY 2"):
            with self.subTest(sql=sql):
                self.assertTrue(self.reasons(sql))
        for sql in ("SELECT id, LOWER(email), CONCAT(firstName, ' ', lastName) AS n, LENGTH(login) FROM t_user",
                    "SELECT COUNT(*), COUNT(DISTINCT email), GROUP_CONCAT(login SEPARATOR ', ') FROM t_user",
                    "SELECT email || login, id + 1, YEAR(login), DATE_FORMAT(login, '%Y') FROM t_user",
                    "SELECT id, (SELECT MAX(id) FROM t_order) FROM t_user",
                    "TABLE t_user", "TABLE t_user ORDER BY id"):
            with self.subTest(sql=sql):
                self.assertEqual(self.reasons(sql), [])

    def guard(self, sql, eng="mysql"):
        found, opaque = dbanon.personal(sql, eng, lambda t: t.lower() in self.NAMES or dbsql.masked(t))
        return dbanon.guard(sql, eng, found, opaque, None, dbanon.hidden_star(dbsql.tokens(sql, eng)))

    def test_bypasses_of_the_second_review_ask(self):
        """2026-11-22 review: a * of a UNION / derived table / CTE renames personal columns so that no personal name
        appears; a user variable carries a value to the next query; a table function makes a list of guesses
        look like a join; a positional ORDER BY outside the parentheses of the query."""
        union = "SELECT 0 AS a, '' AS b FROM dual WHERE 0 UNION ALL SELECT * FROM t_user"
        for sql in ("SELECT a FROM (%s) d WHERE b LIKE 'jo%%'" % union,
                    "SELECT a FROM (%s) d ORDER BY b" % union,
                    "WITH c (a, b) AS (SELECT * FROM t_user) SELECT a FROM c WHERE b LIKE 'a%'",
                    "SELECT d.a FROM (SELECT u.* FROM t_user u) d (a, b) WHERE d.b = 'jan'",
                    "WITH c AS (SELECT * FROM t_user) SELECT id FROM c WHERE c.login LIKE 'a%'",
                    "SELECT login FROM t_user LIMIT 1 INTO @a", "SELECT ORD(SUBSTR(@a, 1, 1))",
                    "SELECT u.id FROM t_user u JOIN JSON_TABLE('[\"x@y.example\"]', '$[*]' COLUMNS(v VARCHAR(99) "
                    "PATH '$')) g ON u.login = g.v",
                    "(SELECT id, login FROM t_user) ORDER BY 2 LIMIT 5",
                    "(SELECT * FROM t_user) ORDER BY 2"):
            with self.subTest(sql=sql):
                self.assertTrue(self.guard(sql))
        for sql in ("SELECT id FROM t_user u WHERE EXISTS (SELECT * FROM t_order o WHERE o.u = u.id) ORDER BY 1",
                    "SELECT COUNT(*) FROM (SELECT * FROM t_user) d",   # no filter: dbtools masks every column
                    "SELECT @@version", "SELECT * FROM t_user WHERE id = 3",
                    # a * of a derived table / CTE keeps the names: a personal column has to be spelled
                    "WITH c AS (SELECT * FROM t_user) SELECT id FROM c WHERE c.x LIKE 'a%'",
                    "SELECT d.id FROM (SELECT u.* FROM t_user u) d WHERE d.x = 'jan'"):
            with self.subTest(sql=sql):
                self.assertEqual(self.guard(sql), [])
        self.assertTrue(dbanon.hidden_star(dbsql.tokens("SELECT ORD(b) FROM (%s) d" % union, "mysql")))
        self.assertTrue(dbanon.hidden_star(dbsql.tokens("SELECT a FROM (SELECT * FROM t_user) d (a, b)", "mysql")))
        self.assertFalse(dbanon.hidden_star(dbsql.tokens("SELECT a * b, COUNT(*), t.* FROM t", "mysql")))

    def test_bypasses_of_the_third_review_ask(self):
        """2026-11-22 third review: syntax and functions the scan does not know fail closed; TABLE t in a set
        operation or a renamed CTE, NATURAL / USING joins of nested queries, SELECT DISTINCT over personal data, a
        JSON aggregate, the implicit name of an unaliased expression and a regex SUBSTRING all ask."""
        for sql, eng in (("WITH c (a, b) AS (TABLE t_user) SELECT a FROM c WHERE b LIKE 'a%'", "mysql"),
                         ("SELECT id FROM t_x UNION TABLE t_user", "mysql"),
                         ("SELECT d.id FROM (SELECT id, login FROM t_user) d NATURAL JOIN t_guess g", "mysql"),
                         ("SELECT d.id FROM (SELECT id, login AS v FROM t_user) d JOIN t_guess g USING (v)", "mysql"),
                         ("SELECT DISTINCT LEFT(login, 1) FROM t_user", "mysql"),
                         ("SELECT JSON_OBJECTAGG(login, id) FROM t_user", "mysql"),
                         ("SELECT x FROM (SELECT ASCII(login) FROM t_user) d WHERE `ASCII(login)` > 100", "mysql"),
                         ("SELECT x FROM (SELECT ascii(login) FROM t_user) d WHERE ascii > 100", "postgres"),
                         ("SELECT id FROM t_user WHERE login IS NOT DISTINCT FROM 'jan'", "mysql"),
                         ("SELECT shop.my_func(id) FROM t_user", "mysql"), ("SELECT my_func(login) FROM t_user", "mysql"),
                         ("SELECT unknown_builtin(id) FROM t_user", "mysql"),
                         ("SELECT u.id FROM t_user u STRAIGHT_JOIN t_guess g ON u.login = g.v", "mysql"),
                         ("SELECT id FROM t_user USE INDEX (ix) WHERE login = 'a'", "mysql"),
                         ("SELECT SUBSTRING(login FROM '^(a+)+$') FROM t_user", "postgres"),
                         ("SELECT id FROM t_user u, LATERAL (SELECT 1) l", "mysql")):
            with self.subTest(sql=sql):
                found, opaque = dbanon.personal(sql, eng, lambda t: t.lower() in self.NAMES or dbsql.masked(t))
                toks = dbsql.tokens(sql, eng)
                self.assertTrue(dbanon.guard(sql, eng, found, opaque, None, dbanon.hidden_star(toks))
                                or dbanon.read_tables(toks) is None)

    def test_everyday_reads_run_without_a_card(self):
        """The fail-closed scan must not card ordinary work: joins, aggregates, windows, CTEs, subqueries, casts."""
        for sql, eng in (("SELECT COUNT(*) FROM (SELECT * FROM t_user) d", "mysql"),
                         ("WITH c AS (SELECT * FROM t_user) SELECT COUNT(*) FROM c", "mysql"),
                         ("SELECT u.id, o.total FROM t_user u JOIN t_order o ON o.user_id = u.id WHERE o.total > 5",
                          "mysql"),
                         ("SELECT status, COUNT(*) FROM t_order GROUP BY status HAVING COUNT(*) > 1 ORDER BY 2 DESC",
                          "mysql"),
                         ("SELECT id FROM t_user WHERE id IN (SELECT user_id FROM t_order) AND EXISTS "
                          "(SELECT 1 FROM t_log l WHERE l.uid = t_user.id)", "mysql"),
                         ("SELECT CASE WHEN total > 9 THEN 'big' ELSE 'small' END, CAST(total AS DECIMAL(9, 2)) "
                          "FROM t_order", "mysql"),
                         ("SELECT COUNT(DISTINCT login) FROM t_user", "mysql"),
                         ("SELECT id, ROW_NUMBER() OVER (PARTITION BY status ORDER BY created) FROM t_order", "mysql"),
                         ("SELECT o.id FROM t_order o JOIN t_item i USING (order_id)", "mysql"),
                         ("SELECT JSON_EXTRACT(meta, '$.a') FROM t_order", "mysql"),
                         ("SELECT id FROM t_order WHERE created BETWEEN '2026-01-01' AND '2026-02-01' LIMIT 9 OFFSET 9",
                          "mysql"),
                         ("SELECT TRIM(BOTH ' ' FROM code) FROM t_order", "mysql"), ("SELECT 1 FROM dual", "mysql"),
                         ("SELECT login, firstname FROM t_user WHERE id = 3", "mysql"),
                         ("SELECT id::text, created + INTERVAL '1 day' FROM t_order WHERE code ILIKE 'a%'", "postgres"),
                         ("SELECT date_trunc('day', created), count(*) FROM t_order GROUP BY 1", "postgres")):
            with self.subTest(sql=sql):
                self.assertEqual(self.guard(sql, eng), [])
                self.assertIsNotNone(dbanon.read_tables(dbsql.tokens(sql, eng)))

    def test_writes_that_copy_personal_data_say_so(self):
        def copies(sql):
            return dbanon.write_copies(sql, "mysql", lambda t: t.lower() in self.NAMES or dbsql.masked(t))
        for sql in ("INSERT scratch SELECT login FROM t_user", "REPLACE scratch SELECT login FROM t_user",
                    "CREATE TABLE c AS SELECT * FROM t_user", "INSERT INTO s SELECT u.* FROM t_user u",
                    "RENAME TABLE t_user TO t_u2", "ALTER TABLE t_user RENAME COLUMN login TO l",
                    "ALTER TABLE t_user CHANGE login l TEXT",
                    "UPDATE t_user SET note = (login LIKE 'A%') WHERE id > 0",
                    "UPDATE a JOIN t_user u ON a.id = u.id SET a.note = u.email",
                    "INSERT INTO z (n) SELECT login FROM t_user",
                    "INSERT INTO z (id, note) VALUES (1, 'a') ON DUPLICATE KEY UPDATE note = email",
                    "CREATE TABLE z AS SELECT login AS n FROM t_user",
                    "CREATE VIEW v AS SELECT email FROM t_user",
                    "ALTER TABLE t_user ADD c VARCHAR(9) AS (LEFT(email, 9))"):
            with self.subTest(sql=sql):
                self.assertTrue(copies(sql))
        for sql in ("UPDATE t_user SET email = NULL, login = 'x' WHERE id = 1",
                    "UPDATE t_user u SET u.email = 'x' WHERE email LIKE 'a%'",   # the condition is guard()'s
                    "INSERT INTO t_user (id, email) VALUES (9, 'z@x.example')",
                    "DELETE FROM t_user WHERE email LIKE 'a%'",
                    "CREATE TABLE z (id INT)"):
            with self.subTest(sql=sql):
                self.assertEqual(copies(sql), [])

    def test_writes_of_the_third_review(self):
        """2026-11-22 third review: TABLE t and a nested * copy whole rows, a partition swap and a prepared
        statement hide what moves; INSERT … SET / ON DUPLICATE KEY targets, column definitions and an index are no
        read, and a rename moves personal data only when it names a rule table or a personal column."""
        def copies(sql, rule_tables=frozenset({"t_user"})):
            return dbanon.write_copies(sql, "mysql", lambda t: t.lower() in self.NAMES or dbsql.masked(t), rule_tables)
        for sql in ("INSERT INTO x TABLE t_user", "CREATE TABLE x AS TABLE t_user",
                    "INSERT INTO x SELECT JSON_OBJECT(u.*) FROM t_user u",
                    "INSERT INTO x SELECT a FROM (SELECT * FROM t_user) d",
                    "ALTER TABLE t_user EXCHANGE PARTITION p WITH TABLE x", "PREPARE s FROM @q", "EXECUTE s",
                    "INSERT INTO t_log SET note = (SELECT email FROM t_user LIMIT 1)",
                    "CREATE TABLE x (id INT, e2 VARCHAR(9) AS (email))",
                    "ALTER TABLE t_user RENAME COLUMN login TO l", "ALTER TABLE t_user CHANGE login l TEXT",
                    "ALTER TABLE t_user RENAME TO t_x", "ALTER TABLE IF EXISTS t_user RENAME TO t_x",
                    "RENAME TABLE t_user TO t_x"):
            with self.subTest(sql=sql):
                self.assertTrue(copies(sql))
        for sql in ("INSERT INTO t_user SET email = 'x', login = 'y'",
                    "INSERT INTO t_user (email) VALUES ('a') ON DUPLICATE KEY UPDATE email = VALUES(email)",
                    "CREATE TABLE x (email VARCHAR(9), login TEXT, id INT)", "CREATE INDEX i ON t_user (email)",
                    "ALTER TABLE t_user RENAME COLUMN note TO n2", "ALTER TABLE t_user CHANGE COLUMN note n2 TEXT",
                    "ALTER TABLE t_order RENAME TO t_x", "RENAME TABLE t_order TO t_x",
                    "UPDATE t_order SET total = total * 2", "INSERT INTO x SELECT COUNT(*) FROM t_user",
                    "CREATE TABLE x LIKE t_user"):
            with self.subTest(sql=sql):
                self.assertEqual(copies(sql), [])
        self.assertTrue(copies("RENAME TABLE t_order TO t_x", None))   # rules unknown: every rename counts

    def test_tables_without_column_rules_are_guarded(self):
        rules = dbanon.validate({"tables": {"t_log": {"drop_rows": [{"all": True}]}, "v_people": {"mask_all": True},
                                            "t_user": {"columns": {"login": {"op": "mask"}}}}})
        self.assertEqual(dbanon.guarded_tables(rules, [("", "t_log"), ("", "t_user")]), ["t_log"])
        self.assertEqual(dbanon.guarded_tables(rules, None), ["t_log", "v_people"])
        self.assertTrue(self.reasons("SELECT COUNT(*) FROM t_log WHERE msg LIKE '%jan%'", views=["t_log"]))

    def test_aliases_over_personal_data_are_personal(self):
        names, opaque = dbanon.personal("SELECT d.x, d.n FROM (SELECT login AS x, LENGTH(email) n, id FROM t_user) d",
                                        "mysql", lambda t: t.lower() in self.NAMES or dbsql.masked(t))
        self.assertTrue({"x", "n", "login", "email"} <= names)
        self.assertNotIn("id", names)
        self.assertFalse(opaque)
        self.assertTrue(dbanon.personal("SELECT * FROM (SELECT email FROM t) AS d (a)", "mysql", dbsql.masked)[1])


if __name__ == "__main__":
    unittest.main()
