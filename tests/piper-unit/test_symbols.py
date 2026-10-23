"""9.7 #4: the regex definition index - parsers per language, incremental refresh, hidden files."""
import os
import shutil
import time
import unittest

import _env
import projectfs
import symbols


def write(root, rel, text):
    os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
    with open(os.path.join(root, rel), "w") as f:
        f.write(text)


def kinds(path, text):
    return [(h["kind"], h["name"]) for h in symbols.parse(path, text)]


class ParserTest(unittest.TestCase):
    def test_java(self):
        src = """package x;
import a.B;
@Service
public final class PriceList extends Base implements I {
    private static final int X = foo(1);
    @Override
    public List<String> getItems(int a) {
        if (a > 0) { return compute(a); }
        return new ArrayList<>(bar(a));
    }
    public PriceList(String s) {}
    void abstractOne();
    protected static <T> Map<String, List<T>> generic(T t) throws IOException {
    interface Inner {}
    enum Color { RED }
    record Point(int x, int y) {}
    public @interface Marker {}
    } else if (x) {
    foo.bar(x);
    String s = call(y);
    throw new IllegalStateException(msg);
}"""
        got = kinds("A.java", src)
        self.assertEqual(got, [("class", "PriceList"), ("method", "getItems"), ("method", "PriceList"),
                               ("method", "abstractOne"), ("method", "generic"), ("interface", "Inner"),
                               ("enum", "Color"), ("record", "Point"), ("annotation", "Marker")])
        hit = symbols.parse("A.java", src)[1]
        self.assertEqual((hit["line"], hit["text"]), (7, "public List<String> getItems(int a) {"))

    def test_kotlin_scala_groovy(self):
        self.assertEqual(kinds("A.kt", "data class Foo(val a: Int)\nenum class E { A }\nannotation class Ann\n"
                                       "object O\n  fun String.ext(x: Int) = 1\nfun <T> gen(t: T) {}\nif (x) foo()\n"
                                       "sealed interface S\n"),
                         [("class", "Foo"), ("enum", "E"), ("annotation", "Ann"), ("object", "O"),
                          ("method", "ext"), ("method", "gen"), ("interface", "S")])
        self.assertEqual(kinds("A.scala", "case class C(a: Int)\ntrait T\n  def run(x: Int): Unit = {}\n"),
                         [("class", "C"), ("trait", "T"), ("method", "run")])
        self.assertEqual(kinds("b.gradle", "def helper(x) {\n}\nclass G {}\n"), [("method", "helper"), ("class", "G")])

    def test_ts_js(self):
        src = ("export class A {\n  constructor(x) {\n  }\n  async load(id: string): Promise<void> {\n"
               "  if (x) {\n  }\n}\nexport interface I {}\nexport type T = 1\nexport const enum CE {}\n"
               "export function f() {}\nexport const k = 1\nconst local = 2\nexport default async function g() {}\n"
               "  private get v(): number {\n  foo(bar) {\n  foo(bar);\n")
        self.assertEqual(kinds("a.ts", src), [("class", "A"), ("method", "constructor"), ("method", "load"),
                                              ("interface", "I"), ("type", "T"), ("enum", "CE"), ("function", "f"),
                                              ("const", "k"), ("function", "g"), ("method", "v"), ("method", "foo")])
        self.assertEqual(kinds("a.mjs", "function plain() {}\nexport let x = 1\n"),
                         [("function", "plain"), ("const", "x")])

    def test_python_go(self):
        self.assertEqual(kinds("a.py", "class A:\n    def m(self):\n        pass\nasync def f():\n    x = 1\n"),
                         [("class", "A"), ("method", "m"), ("function", "f")])
        self.assertEqual(kinds("a.go", "package x\nfunc (r *R) M() {}\nfunc F() {}\ntype S struct {\n}\n"
                                       "type (\n  A int\n  B struct{}\n)\n"),
                         [("function", "M"), ("function", "F"), ("type", "S"), ("type", "A"), ("type", "B")])

    def test_xml_properties_yaml(self):
        spring = '<beans xmlns="http://www.springframework.org/schema/beans">\n<bean id="priceService" class="x"/>\n' \
                 '<util:list id="lst"/>\n'
        self.assertEqual(kinds("beans.xml", spring), [("bean", "priceService"), ("id", "lst")])
        self.assertEqual(kinds("pom.xml", '<a id="q"/>\n<bean id="b"/>'), [("bean", "b")])   # id= only in Spring/Acme
        props = "# comment\ndb.url=jdbc\nkey2 : v\nmulti=a\\\n  cont=x\n! bang\nesc\\:key=1\n"
        self.assertEqual(kinds("a.properties", props), [("property", "db.url"), ("property", "key2"),
                                                        ("property", "multi"), ("property", "esc:key")])
        self.assertEqual(kinds("a.yml", "server:\n  port: 1\nspring.x: 2\n---\n# c: 1\n"),
                         [("key", "server"), ("key", "spring.x")])

    def test_sql_sh_md(self):
        self.assertEqual(kinds("a.sql", 'CREATE TABLE IF NOT EXISTS public."T_price" (\ncreate or replace view v_x as\n'
                                        "CREATE FUNCTION fn()\ncreate procedure p1()\ninsert into t values (1);\n"),
                         [("table", "T_price"), ("view", "v_x"), ("function", "fn"), ("procedure", "p1")])
        self.assertEqual(kinds("a.sh", "foo() {\n}\nfunction bar {\n}\nif x; then\n"),
                         [("function", "foo"), ("function", "bar")])
        self.assertEqual(kinds("a.md", "# Title\n```\n# not a heading\n```\n## Sub ##\n"),
                         [("heading", "Title"), ("heading", "Sub")])
        self.assertEqual(symbols.parse("a.bin", "class X {}"), [])

    def test_bounds(self):
        many = "".join("def f%d():\n    pass\n" % i for i in range(symbols.MAX_HITS_PER_FILE + 50))
        self.assertEqual(len(symbols.parse("a.py", many)), symbols.MAX_HITS_PER_FILE)
        self.assertEqual(symbols.parse("A.java", "    public void " + "x" * 500 + "() {"), [])   # overlong line
        for path, line in (("a.py", "def " + "x" * 500 + "():"), ("a.md", "# " + "x" * 500), ("a.sh", "f" * 500 + "() {"),
                           ("a.yml", "k" * 500 + ":"), ("a.properties", "k" * 500 + "=1"), ("a.go", "func " + "x" * 500 + "()"),
                           ("a.sql", "create table " + "t" * 500)):
            self.assertEqual(symbols.parse(path, line), [], path)   # every language skips an overlong line

    def test_md_heading_is_linear(self):
        n = 1500
        line = "# " + " " * n + "#" * n + " " * n + "x"
        t = time.monotonic()
        self.assertEqual([h[0] for h in symbols._md([line])], ["#" * n + " " * n + "x"])
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual([h[0] for h in symbols._md(["## Sub ##", "# a#b #", "#  ", "#nospace"])], ["Sub", "a#b"])


class IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = _env.make_project("symbols", {
            "src/PriceList.java": "public class PriceList {\n  public int total() {\n    return 1;\n  }\n}\n",
            "src/Other.java": "class Other {\n  void totalize() {}\n}\n",
            "config/secrets/db.properties": "db.password=x\n",
            "notes/Secret.java": "class Secret {}\n",
            "app.properties": "total.limit=5\n",
            "big/Huge.java": "class Huge {}\n" + "//" * (symbols.MAX_FILE // 2),
            "bin/Bin.java": "class Bin {}\0",
            ".hole/settings.json": '{"files": {"exclude": ["notes"]}}', ".git/HEAD": "ref"})
        os.symlink(os.path.join(cls.root, "notes", "Secret.java"), os.path.join(cls.root, "src", "Link.java"))
        cls.key = "team-x/symbols"

    def test_exact_and_search_over_the_gui_files(self):
        r = self.m.fs_symbols(self.key, "PriceList")
        self.assertEqual([(h["path"], h["line"], h["kind"]) for h in r["symbols"]], [("src/PriceList.java", 1, "class")])
        self.assertGreaterEqual(r["indexed"], 3)
        for name in ("Huge", "Bin"):   # too big, binary
            self.assertEqual(self.m.fs_symbols(self.key, name)["symbols"], [], name)
        # hidden from Claude, the GUI's anyway (2026-10-07)
        self.assertEqual([h["path"] for h in self.m.fs_symbols(self.key, "Secret")["symbols"]], ["notes/Secret.java"])
        self.assertEqual(len(self.m.fs_symbols(self.key, "db.password")["symbols"]), 1)
        self.assertEqual(self.m.fs_symbols(self.key, "total.limit")["symbols"][0]["kind"], "property")
        q = self.m.fs_symbols(self.key, q="TOTAL")["symbols"]
        self.assertEqual([h["name"] for h in q], ["total", "totalize", "total.limit"])   # exact, then prefix (shorter first)
        with self.assertRaises(ValueError):
            self.m.fs_symbols(self.key, "", "")

    def test_incremental_refresh(self):
        idx = symbols.index_for(self.key)
        self.m.fs_symbols(self.key, "PriceList")
        before = dict(idx.files)
        p = os.path.join(self.root, "src", "Other.java")
        with open(p, "w") as f:
            f.write("class Other {\n  void renamed() {}\n}\n")
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.m.fs_symbols(self.key, "renamed")["symbols"], [])   # TTL: not yet
        idx.checked = 0.0
        self.assertEqual(len(self.m.fs_symbols(self.key, "renamed")["symbols"]), 1)
        self.assertIs(idx.files["src/PriceList.java"], before["src/PriceList.java"])   # unchanged: not re-parsed
        idx.invalidate("src/PriceList.java")
        self.assertEqual(idx.checked, 0.0)
        self.assertNotIn("src/PriceList.java", idx.files)

    def test_size_limit_is_the_gui_read_limit_and_noise_is_not_indexed(self):
        """K13: 2 MiB like every GUI read (was 1 MiB); K1: NOISE stays out like quick open."""
        self.assertEqual(symbols.MAX_FILE, projectfs.MAX_READ)
        write(self.root, "src/Mid.java", "class MidSized {}\n" + "//" * (3 << 18))   # 1.5 MiB
        write(self.root, ".vscode/Tool.java", "class NoiseTool {}\n")
        self.addCleanup(os.unlink, os.path.join(self.root, "src/Mid.java"))
        self.addCleanup(shutil.rmtree, os.path.join(self.root, ".vscode"), True)
        self.m._files_cache.pop(self.key, None)
        symbols.index_for(self.key).invalidate()
        self.assertEqual([h["path"] for h in self.m.fs_symbols(self.key, "MidSized")["symbols"]], ["src/Mid.java"])
        self.assertEqual(self.m.fs_symbols(self.key, "NoiseTool")["symbols"], [])


if __name__ == "__main__":
    unittest.main()
