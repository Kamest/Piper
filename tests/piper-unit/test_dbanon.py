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

    def test_aliases_over_personal_data_are_personal(self):
        names, opaque = dbanon.personal("SELECT d.x, d.n FROM (SELECT login AS x, LENGTH(email) n, id FROM t_user) d",
                                        "mysql", lambda t: t.lower() in self.NAMES or dbsql.masked(t))
        self.assertTrue({"x", "n", "login", "email"} <= names)
        self.assertNotIn("id", names)
        self.assertFalse(opaque)
        self.assertTrue(dbanon.personal("SELECT * FROM (SELECT email FROM t) AS d (a)", "mysql", dbsql.masked)[1])


if __name__ == "__main__":
    unittest.main()
