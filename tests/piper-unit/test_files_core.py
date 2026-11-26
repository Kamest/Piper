"""Files → IDE level, phase 1 core (backend): typed save errors (1a), POST /api/fs/stat and the change token of
list / read / write (1b), the symbol index updated per path and scanned in the background (B12)."""
import hashlib
import json
import os
import shutil
import threading
import time
import unittest
import urllib.error
import urllib.request

import _env
import common
import projectfs
import symbols
from test_workbench97 import write


def sha(root, rel):
    with open(os.path.join(root, rel), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class FilesCoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = os.path.realpath(_env.make_project("fcore", {
            "src/A.java": "class A {\n  int alpha;\n}\n",
            "src/B.java": "class B {}\n",
            "notes.txt": "hidden from Claude\n",
            ".hole/settings.json": '{"files": {"exclude": ["notes.txt"]}}',
            ".git/config": "[core]\n"}))
        cls.outside = os.path.realpath(_env.make_project("fcore-out", {"secret.txt": "x\n"}))
        os.symlink(os.path.join(cls.outside, "secret.txt"), os.path.join(cls.root, "src", "out.txt"))
        os.symlink(cls.outside, os.path.join(cls.root, "outdir"))
        cls.key = "team-x/fcore"
        _env.register(cls.key, cls.root)

    def setUp(self):
        write(self.root, "src/A.java", "class A {\n  int alpha;\n}\n")
        for rel in ("src/Gone.java", "src/New.java"):
            if os.path.exists(os.path.join(self.root, rel)):
                os.unlink(os.path.join(self.root, rel))

    # ------------------------------------------------------------ 1a: typed save errors
    def test_save_errors_are_typed_and_keep_their_text(self):
        with self.assertRaisesRegex(projectfs.WriteConflict, "changed since you opened it"):
            self.m.fs_write(self.key, "src/A.java", "x", "0" * 64)
        with self.assertRaisesRegex(projectfs.Gone, "does not exist"):
            self.m.fs_write(self.key, "src/Gone.java", "x", "0" * 64)
        with self.assertRaises(projectfs.TooLarge):
            self.m.fs_write(self.key, "src/A.java", "x" * (3 << 20), sha(self.root, "src/A.java"))
        os.symlink("A.java", os.path.join(self.root, "src", "alias.java"))
        self.addCleanup(os.unlink, os.path.join(self.root, "src", "alias.java"))
        with self.assertRaises(projectfs.Unsupported):
            self.m.fs_write(self.key, "src/alias.java", "x", sha(self.root, "src/A.java"))
        # each is still the ValueError older callers catch (review restore, external roots)
        for cls in (projectfs.WriteConflict, projectfs.Gone, projectfs.TooLarge, projectfs.Unsupported):
            self.assertTrue(issubclass(cls, ValueError))

    def test_external_save_errors_are_typed(self):
        ext = os.path.realpath(_env.make_project("fcore-ext", {"doc.txt": "one\n"}))
        key = projectfs.EXTERNAL_PREFIX + ext
        with self.assertRaises(projectfs.WriteConflict):
            self.m.ext_write(key, "doc.txt", "two", "0" * 64)
        with self.assertRaises(projectfs.Gone):
            self.m.ext_write(key, "nothere.txt", "two", "0" * 64)

    def test_write_answers_the_version_stat_and_read_report(self):
        r = self.m.fs_write(self.key, "src/A.java", "class A {}", sha(self.root, "src/A.java"))
        st = os.stat(os.path.join(self.root, "src/A.java"))
        self.assertEqual(r["version"], projectfs.version(st))
        self.assertEqual(self.m.fs_stat(self.key, [], ["src/A.java"])["files"]["src/A.java"], r["version"])
        self.assertEqual(self.m.fs(self.key, "read", {"path": "src/A.java"})["version"], r["version"])
        listing = self.m.fs(self.key, "list", {"path": "src"})
        self.assertEqual(listing["version"], projectfs.version(os.stat(os.path.join(self.root, "src"))))

    # ------------------------------------------------------------ 1b: POST /api/fs/stat
    def test_stat_versions_change_with_the_disk(self):
        before = self.m.fs_stat(self.key, ["src", "."], ["src/A.java"])
        self.assertTrue(all(before["dirs"].values()) and before["files"]["src/A.java"])
        time.sleep(0.01)
        write(self.root, "src/New.java", "class New {}\n")   # a new entry: the folder's token moves
        with open(os.path.join(self.root, "src/A.java"), "a") as f:
            f.write("// sed -i\n")
        after = self.m.fs_stat(self.key, ["src", "."], ["src/A.java"])
        self.assertNotEqual(after["dirs"]["src"], before["dirs"]["src"])
        self.assertEqual(after["dirs"]["."], before["dirs"]["."])
        self.assertNotEqual(after["files"]["src/A.java"], before["files"]["src/A.java"])
        os.unlink(os.path.join(self.root, "src/A.java"))
        self.assertIsNone(self.m.fs_stat(self.key, [], ["src/A.java"])["files"]["src/A.java"])

    def test_stat_never_tells_a_refused_path_from_a_missing_one(self):
        """The visibility rule of list / read: .git, a link out of the project, the wrong kind, '..' all read as
        absent - like a path that does not exist. A file hidden from Claude is the GUI's like any other."""
        files = [".git/config", "src/out.txt", "../fcore-out/secret.txt", "/etc/hosts", "src", "src/Nope.java", "notes.txt"]
        dirs = [".git", "outdir", "src/A.java", "nope"]
        r = self.m.fs_stat(self.key, dirs, files)
        self.assertEqual({k: v for k, v in r["files"].items() if v}, {"notes.txt": r["files"]["notes.txt"]})
        self.assertEqual(r["dirs"], {d: None for d in dirs})

    def test_stat_bounds_its_input(self):
        with self.assertRaises(ValueError):
            self.m.fs_stat(self.key, [], ["a"] * 1001)
        with self.assertRaises(ValueError):
            self.m.fs_stat(self.key, "src", [])
        with self.assertRaises(KeyError):
            self.m.fs_stat("nope/nope", [], [])

    def test_http_codes_of_save_errors_and_stat(self):
        import piperd
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        old = piperd.MANAGER, piperd.PORT
        piperd.MANAGER, piperd.PORT = self.m, server.server_port
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop():
            server.shutdown()
            server.server_close()
            piperd.MANAGER, piperd.PORT = old
        self.addCleanup(stop)

        def post(route, body, token=common.own_token("gui")):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (server.server_port, route), data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        w = lambda path, s: post("/api/fs/write", {"project": self.key, "path": path, "text": "x", "sha": s})
        self.assertEqual(w("src/A.java", "0" * 64)[0], 409)
        self.assertEqual(w("src/A.java", "0" * 64)[1]["code"], "conflict")
        self.assertEqual(w("src/Gone.java", "0" * 64), (410, {"error": "src/Gone.java does not exist (the GUI edits existing files "
                                                                 "only)", "code": "missing"}))
        self.assertEqual(w(".git/config", "0" * 64)[0], 404)   # refused stays a 404 without a code
        self.assertNotIn("code", w(".git/config", "0" * 64)[1])
        status, body = post("/api/fs/stat", {"project": self.key, "dirs": ["src", ".git"], "files": ["src/A.java", ".git/config"]})
        self.assertEqual(status, 200)
        self.assertIsNone(body["dirs"][".git"])
        self.assertIsNone(body["files"][".git/config"])
        self.assertTrue(body["files"]["src/A.java"])
        self.assertEqual(post("/api/fs/stat", {"project": self.key}, token=common.own_token("tty"))[0], 403)
        self.assertEqual(post("/api/fs/stat", {"project": projectfs.EXTERNAL_PREFIX + self.outside, "dirs": ["."]})[0], 404)


class SymbolIndexTest(unittest.TestCase):
    """B12: a save re-parses one file (no full rescan); the full scan runs outside the request."""

    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = os.path.realpath(_env.make_project("fsym", {
            "src/A.java": "class Alpha {}\n", "src/B.java": "class Beta {}\n", ".vscode/T.java": "class Tool {}\n"}))
        cls.key = "team-x/fsym"
        _env.register(cls.key, cls.root)

    def setUp(self):
        symbols._INDEXES.pop(self.key, None)
        write(self.root, "src/A.java", "class Alpha {}\n")

    def test_save_updates_the_index_without_a_rescan(self):
        self.assertEqual(len(self.m.fs_symbols(self.key, "Alpha")["symbols"]), 1)
        idx = symbols.index_for(self.key)
        checked, beta = idx.checked, idx.files["src/B.java"]
        scans = []
        real = idx.refresh_async
        idx.refresh_async = lambda *a: scans.append(1) or real(*a)
        self.m.fs_write(self.key, "src/A.java", "class Gamma {}", sha(self.root, "src/A.java"))
        self.assertEqual(idx.checked, checked)   # still fresh: no full scan on the next query
        self.assertEqual([h["path"] for h in self.m.fs_symbols(self.key, "Gamma")["symbols"]], ["src/A.java"])
        self.assertEqual(self.m.fs_symbols(self.key, "Alpha")["symbols"], [])
        self.assertIs(idx.files["src/B.java"], beta)
        self.assertEqual(scans, [])

    def test_update_skips_what_the_index_never_holds(self):
        idx = symbols.index_for(self.key)
        idx.update(self.root, ".vscode/T.java")   # NOISE, like quick open
        idx.update(self.root, "src/Missing.java")
        self.assertEqual(idx.files, {})

    def test_cold_query_answers_building_not_empty_and_does_not_block(self):
        gate = threading.Event()
        old = symbols.COLD_WAIT
        symbols.COLD_WAIT = 0.05
        self.addCleanup(setattr, symbols, "COLD_WAIT", old)
        real = self.m.fs

        def slow(key, op, args):
            if op == "files":
                gate.wait(5)
            return real(key, op, args)
        self.m.fs = slow
        self.addCleanup(delattr, self.m, "fs")
        t0 = time.monotonic()
        r = self.m.fs_symbols(self.key, "Alpha")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(r.get("building"))
        gate.set()
        idx = symbols.index_for(self.key)
        self.assertTrue(_wait(lambda: idx.built))
        r = self.m.fs_symbols(self.key, "Alpha")
        self.assertNotIn("building", r)
        self.assertEqual(len(r["symbols"]), 1)

    def test_a_save_during_a_scan_survives_the_swap(self):
        idx = symbols.index_for(self.key)
        gate, listed = threading.Event(), threading.Event()

        def listing():
            listed.set()
            gate.wait(5)
            return ["src/A.java", "src/B.java"]
        write(self.root, "src/A.java", "class Old {}\n")
        idx.refresh_async(self.root, listing)
        self.assertTrue(listed.wait(5))
        write(self.root, "src/A.java", "class Newer {}\n")
        idx.update(self.root, "src/A.java")
        write(self.root, "src/A.java", "class NotParsedByTheSave {}\n")   # what the scan reads after the save
        gate.set()
        self.assertTrue(_wait(lambda: idx.built))
        self.assertEqual([h["name"] for h in idx.files["src/A.java"][1]], ["Newer"])


def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


if __name__ == "__main__":
    unittest.main()
