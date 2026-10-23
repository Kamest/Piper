"""9.7 #9: diagnostics parsed from the session's last Bash output (one test per tool)."""
import json
import os
import unittest

import _env
import common
import diagnostics


def one(text):
    items = diagnostics.parse(text)
    return [(i["tool"], i["severity"], i["path"], i["line"], i["col"], i["message"]) for i in items]


class ParserTest(unittest.TestCase):
    def test_mvn(self):
        self.assertEqual(one("[INFO] x\n[ERROR] /p/src/main/java/A.java:[12,5] cannot find symbol\n"
                             "[WARNING] /p/B.java:[3] deprecated\n\x1b[1;31m[ERROR]\x1b[m /p/C.java:[1,1] colored\n"),
                         [("mvn", "error", "/p/src/main/java/A.java", 12, 5, "cannot find symbol"),
                          ("mvn", "warning", "/p/B.java", 3, None, "deprecated"),
                          ("mvn", "error", "/p/C.java", 1, 1, "colored")])

    def test_javac(self):
        self.assertEqual(one("src/B.java:7: error: ';' expected\n/p/C.java:9: warning: [unchecked] x\n"),
                         [("javac", "error", "src/B.java", 7, None, "';' expected"),
                          ("javac", "warning", "/p/C.java", 9, None, "[unchecked] x")])

    def test_tsc_both_formats(self):
        self.assertEqual(one("src/a.ts(12,5): error TS2322: Type 'x' is not assignable\n"
                             "src/b.tsx:3:9 - error TS2304: Cannot find name 'y'.\n"),
                         [("tsc", "error", "src/a.ts", 12, 5, "TS2322: Type 'x' is not assignable"),
                          ("tsc", "error", "src/b.tsx", 3, 9, "TS2304: Cannot find name 'y'.")])

    def test_eslint_stylish(self):
        self.assertEqual(one("\n/p/web/src/c.ts\n  4:10  error  'z' is defined but never used  no-unused-vars\n"
                             "  5:1   warning  Unexpected console statement  no-console\n\n"
                             "✖ 2 problems (1 error, 1 warning)\n"),
                         [("eslint", "error", "/p/web/src/c.ts", 4, 10, "'z' is defined but never used (no-unused-vars)"),
                          ("eslint", "warning", "/p/web/src/c.ts", 5, 1, "Unexpected console statement (no-console)")])

    def test_gradle_kotlin(self):
        self.assertEqual(one("e: file:///p/app/A.kt:12:5 Unresolved reference: foo\n"
                             "w: /p/app/B.kt: (3, 1): Parameter 'x' is never used\n"),
                         [("gradle", "error", "/p/app/A.kt", 12, 5, "Unresolved reference: foo"),
                          ("gradle", "warning", "/p/app/B.kt", 3, 1, "Parameter 'x' is never used")])

    def test_python_traceback_innermost_own_frame(self):
        tb = ('Traceback (most recent call last):\n  File "/usr/lib/python3/runpy.py", line 1, in <module>\n'
              '  File "/p/tool.py", line 12, in main\n    raise ValueError("bad")\n'
              '  File "/usr/lib/python3.12/json/__init__.py", line 3, in loads\n    ^^^^^\n'
              'ValueError: bad password=hunter2\n  File "/p/not_a_traceback.py", line 1\n')
        self.assertEqual(one(tb), [("python", "error", "/p/tool.py", 12, None, "ValueError: bad password=[scrubbed]")])

    def test_junit_surefire(self):
        self.assertEqual(one("[ERROR] Failures: \n[ERROR]   PriceTest.testAdd:42 expected: <3> but was: <4>\n"
                             "[ERROR]   com.example.CalcTest.div:15->helper:3 boom\n[ERROR] Tests run: 3, Failures: 1\n"),
                         [("junit", "error", "PriceTest", 42, None, "testAdd: expected: <3> but was: <4>"),
                          ("junit", "error", "com.example.CalcTest", 15, None, "div: boom")])

    def test_nothing_in_plain_output(self):
        self.assertEqual(one("BUILD SUCCESS\nall good: 0 errors\nsrc/readme.txt:3: note\n"), [])

    def test_dedupe_bound_and_relativize(self):
        items = diagnostics.dedupe(diagnostics.parse("[ERROR] /p/A.java:[1,1] x\n" * 3 + "".join(
            "[ERROR] /p/A.java:[%d,1] y\n" % i for i in range(300))))
        self.assertEqual(len(items), diagnostics.MAX_ITEMS)
        self.assertEqual(len([i for i in items if i["message"] == "x"]), 1)
        rel = diagnostics.relativize(diagnostics.parse("[ERROR] /p/proj/src/A.java:[1,2] m\n[ERROR]   PriceTest.a:1 x\n"
                                                       "e: file:///p/proj/k/B.kt:1:1 z\n./src/c.ts(1,1): error TS1: q\n"
                                                       "[ERROR] /elsewhere/D.java:[1,1] d\n"),
                                     "/p/proj", lambda c: "src/test/%s.java" % c)
        self.assertEqual([i["path"] for i in rel], ["src/A.java", "src/test/PriceTest.java", "k/B.kt", "src/c.ts",
                                                    "/elsewhere/D.java"])


def rec_use(tid, cmd, name="Bash"):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name,
                                                          "input": {"command": cmd}}]}}


def rec_result(tid, text, ts="2026-09-24T10:00:00Z", as_list=False):
    content = [{"type": "text", "text": text}] if as_list else text
    return {"type": "user", "timestamp": ts,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}}


class FromRecordsTest(unittest.TestCase):
    def test_most_recent_bash_result_with_items(self):
        recs = [rec_use("a", "mvn -q compile\nmore"), rec_result("a", "[ERROR] /p/A.java:[1,1] old", "t1"),
                rec_use("b", "tsc --noEmit"), rec_result("b", "src/a.ts(2,3): error TS1: new", "t2", as_list=True),
                rec_use("c", "ls"), rec_result("c", "nothing here", "t3"),
                rec_use("d", "x", name="Read"), rec_result("d", "[ERROR] /p/Z.java:[9,9] a Read result", "t4")]
        r = diagnostics.from_records(recs)
        self.assertEqual((r["at"], r["source"], [i["message"] for i in r["items"]]), ("t2", "tsc --noEmit", ["TS1: new"]))
        self.assertEqual(diagnostics.from_records([rec_use("a", "ls"), rec_result("a", "ok")]),
                         {"at": None, "source": "", "items": []})

    def test_manager_reads_the_session_transcript(self):
        import sessions
        m = _env.manager(self)
        root = _env.make_project("diag", {"src/test/java/com/acme/PriceTest.java": "class PriceTest {}\n",
                                          "src/main/java/A.java": "class A {}\n", ".git/HEAD": "ref"})
        s = sessions.Session(project="team-x/diag", path=root, state="waiting", claude_session="c1")
        m.sessions[s.id] = s
        d = os.path.join(s.cfg, "projects", "-x")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "c1.jsonl"), "w") as f:
            for r in (rec_use("a", "mvn test"),
                      rec_result("a", "[ERROR] %s/src/main/java/A.java:[3,1] boom\n[ERROR]   PriceTest.adds:7 x\n" % root)):
                f.write(json.dumps(r) + "\n")
        r = m.diagnostics(s.id)
        self.assertEqual([(i["path"], i["line"]) for i in r["items"]],
                         [("src/main/java/A.java", 3), ("src/test/java/com/acme/PriceTest.java", 7)])
        self.assertEqual(r["source"], "mvn test")
        host = sessions.Session(project="team-x/diag", path=root, state="waiting", claude_mode="host")
        m.sessions[host.id] = host
        self.assertEqual(m.diagnostics(host.id)["items"], [])
        self.assertIn("note", m.diagnostics(host.id))
        self.assertTrue(os.path.isdir(common.SESSIONS_DIR))


if __name__ == "__main__":
    unittest.main()


class RelResolveTest(unittest.TestCase):
    def test_path_printed_from_a_subdirectory_is_resolved(self):
        items = [{"tool": "tsc", "severity": "error", "path": "src/a.ts", "line": 1, "col": 1, "message": "x"},
                 {"tool": "tsc", "severity": "error", "path": "src/b.ts", "line": 1, "col": 1, "message": "y"}]
        files = ["web/src/a.ts", "web/src/b.ts", "other/src/b.ts"]
        def resolve(r):   # the shape of Workbench._rel_resolver
            hits = [f for f in files if f.endswith("/" + r)]
            return r if r in files else hits[0] if len(hits) == 1 else None
        res = diagnostics.relativize(items, "/p", None, resolve)
        self.assertEqual(res[0]["path"], "web/src/a.ts")
        self.assertEqual(res[1]["path"], "src/b.ts")   # ambiguous: left as printed
