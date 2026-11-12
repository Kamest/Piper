"""Notes (notes.py, module `notes`): the main note's append with one heading a day, files that never overwrite,
the sha lock, refused folders (Piper's state, agents' homes, extension paths, projects), no-follow I/O, a path
with a space, the OS refusing access (macOS privacy protection) as the state "denied", the routes behind the
full GUI token and the module switch, and the folder as a Files external root."""
import errno
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import _env
import common
import extensions
import features
import notes
import piperd
import projectfs
import settings

HOST = "127.0.0.1:%s" % os.environ["PIPER_PORT"]
DAY1 = datetime(2026, 11, 12, 14, 32)
DAY2 = datetime(2026, 11, 13, 9, 5)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(raw: bytes):
    a, b = socket.socketpair()
    b.settimeout(10)

    def serve():
        try:
            piperd.Handler(a, ("127.0.0.1", 1), None)
        finally:
            a.close()
    threading.Thread(target=serve, daemon=True).start()
    b.sendall(raw)
    try:
        b.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    data = b""
    while True:
        chunk = b.recv(65536)
        if not chunk:
            break
        data += chunk
    b.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body) if body.startswith((b"{", b"[")) else body


def get(path, token, extra=""):
    auth = "Authorization: Bearer %s\r\n" % token if token else ""
    return request(("GET %s HTTP/1.1\r\nHost: %s\r\n%s%sConnection: close\r\n\r\n" % (path, HOST, auth, extra)).encode())


def post(path, body, token, extra=""):
    raw = json.dumps(body).encode()
    hdr = "POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n%s" % (
        path, HOST, len(raw), extra)
    if token:
        hdr += "Authorization: Bearer %s\r\n" % token
    return request((hdr + "Connection: close\r\n\r\n").encode() + raw)


class NotesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        notes.bind(cls.m)
        cls.addClassCleanup(notes.bind, None)
        cls.proot = os.path.realpath(_env.make_project("notesproj", {"README.md": "# r\n"}))
        _env.register("team-x/notesproj", cls.proot)

    def setUp(self):
        tmp = os.path.realpath(tempfile.mkdtemp(dir=os.environ["TEST_SANDBOX_WORK"]))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        self.dir = os.path.join(tmp, "Documents", "Piper Notes")   # a space, and a parent that does not exist yet
        self.configure(self.dir)
        self.addCleanup(lambda: os.path.exists(notes.CONFIG_FILE) and os.unlink(notes.CONFIG_FILE))
        _env.set_modules(self, notes=True)   # off by default (NO_PRESET): these tests run with it switched on

    def configure(self, d, **kw):
        os.makedirs(os.path.dirname(notes.CONFIG_FILE), exist_ok=True)
        common.write_json(notes.CONFIG_FILE, dict({"dir": d}, **kw))

    def refuse(self, attr, match):
        """Patch notes.os / notes.common `attr` to raise EPERM (the macOS privacy refusal) for this test's folder only."""
        target = notes.common if attr == "_open_dir" else notes.os
        real = getattr(target, attr)

        def fake(*a, **kw):
            if match(a):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real(*a, **kw)
        return mock.patch.object(target, attr, side_effect=fake)

    def text(self, name="notes.md") -> str:
        with open(os.path.join(self.dir, name), encoding="utf-8", newline="") as f:
            return f.read()

    # ------------------------------------------------------------ defaults and configuration
    def test_defaults_per_platform_and_a_broken_config(self):
        self.assertEqual(notes.default_dir("darwin"), "~/Documents/Piper Notes")
        self.assertEqual(notes.default_dir("linux"), "~/Piper Notes")
        self.assertEqual(notes.expand("~/Documents/Piper Notes"), os.path.join(common.REAL_HOME, "Documents", "Piper Notes"))
        os.unlink(notes.CONFIG_FILE)
        self.assertEqual(notes.config(), {"dir": notes.DEFAULT_DIR, "main": "notes.md", "stamp": "time"})
        self.configure("", main="../x", stamp="weird")
        self.assertEqual(notes.config(), {"dir": notes.DEFAULT_DIR, "main": "notes.md", "stamp": "time"})
        self.assertEqual(notes.CONFIG_FILE, settings.store_file(notes.STORE))

    # ------------------------------------------------------------ append
    def test_append_day_heading_once_multiline_newest_at_the_bottom(self):
        st = notes.status(self.m)
        self.assertEqual((st["state"], st["files"], st["key"]), ("missing", [], None))
        self.assertFalse(os.path.exists(os.path.dirname(self.dir)), "nothing is created before the first note")
        a = notes.append(self.m, "First thought", now=DAY1)
        self.assertTrue(a["heading"])
        b = notes.append(self.m, "  second line\r\nwith a continuation\n\n  and more  \n\n", now=DAY1.replace(minute=40))
        self.assertFalse(b["heading"])
        notes.append(self.m, "Next day", now=DAY2)
        body = self.text()
        self.assertEqual(body, "## 2026-11-12\n\n- 14:32 First thought\n- 14:40 second line\n  with a continuation\n"
                               "\n    and more\n\n## 2026-11-13\n\n- 09:05 Next day\n")
        self.assertEqual(body.count("## 2026-11-12"), 1)
        self.assertEqual(notes.append(self.m, "x", now=DAY2)["sha"], sha(self.text().encode()))
        st = notes.status(self.m)
        self.assertEqual((st["state"], st["key"], [f["name"] for f in st["files"]]),
                         ("ok", projectfs.EXTERNAL_PREFIX + self.dir, ["notes.md"]))
        # the phone list's subtitle (board 07): the newest entry without its marker, never a heading or a blank line
        self.assertEqual(st["files"][0]["last"], "09:05 x")
        with open(os.path.join(self.dir, "notes.md"), "a") as f:
            f.write("\n" + "y" * 5000 + "\n\n## Later\n\n")   # longer than the tail read: cut, still found
        self.assertEqual(notes.status(self.m)["files"][0]["last"], "y" * 160)
        self.assertNotIn("last", notes.list_files(self.m)["files"][0], "agents' notes_list is unchanged")
        for bad in ("", "  \n ", None, "a\0b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                notes.append(self.m, bad)
        ev = [e for e in common.audit_tail(50) if e.get("event") == "notes-append"]
        self.assertTrue(ev)
        self.assertNotIn("First thought", json.dumps(ev))

    def test_append_keeps_a_hand_edited_file_and_its_line_endings(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "notes.md"), "wb") as f:
            f.write(b"# My notes\r\n\r\n## 2026-11-12\r\n- 08:00 old")   # CRLF, no final newline
        notes.append(self.m, "new", now=DAY1)
        self.assertEqual(self.text(), "# My notes\r\n\r\n## 2026-11-12\r\n- 08:00 old\r\n- 14:32 new\r\n")
        with open(os.path.join(self.dir, "notes.md"), "ab") as f:
            f.write(b"## Ideas\r\n- someday\r\n")   # a heading of the human's own after today's
        self.assertTrue(notes.append(self.m, "later", now=DAY1)["heading"])
        self.assertTrue(self.text().endswith("- someday\r\n\r\n## 2026-11-12\r\n\r\n- 14:32 later\r\n"))

    def test_stamp_formats_and_another_main_note(self):
        self.configure(self.dir, stamp="datetime", main="journal.md")
        notes.append(self.m, "dated", now=DAY1)
        self.configure(self.dir, stamp="none", main="journal.md")
        notes.append(self.m, "plain", now=DAY1)
        self.assertEqual(self.text("journal.md"), "- 2026-11-12 14:32 dated\n- plain\n")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "notes.md")))

    def test_concurrent_appends_lose_nothing_and_head_the_day_once(self):
        errors = []

        def one(i):
            try:
                notes.append(self.m, "line %02d" % i, now=DAY1)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        body = self.text()
        self.assertEqual(body.count("## 2026-11-12"), 1)
        self.assertEqual(sorted(l for l in body.splitlines() if l.startswith("- ")),
                         ["- 14:32 line %02d" % i for i in range(16)])

    def test_append_does_not_lose_a_line_against_a_files_save(self):
        """The Files editor saves notes.md through ext_write (sha lock, rename): an append in between is not lost -
        the save is refused because the sha changed."""
        r = notes.append(self.m, "one", now=DAY1)
        key = r["key"]
        notes.append(self.m, "two", now=DAY1)
        with self.assertRaisesRegex(ValueError, "changed since you opened it"):
            self.m.ext_write(key, "notes.md", "overwritten", r["sha"])
        self.assertIn("- 14:32 two", self.text())

    # ------------------------------------------------------------ files: never overwrite, the sha lock
    def test_create_never_overwrites_infers_the_extension_and_dedupes(self):
        a = notes.create(self.m, '{"order": 4812}', "order-export", now=DAY1)
        self.assertEqual((a["name"], a["renamed"]), ("order-export.json", False))
        b = notes.create(self.m, "other", "order-export.json", now=DAY1)
        c = notes.create(self.m, "third", "order-export.json", now=DAY1)
        self.assertEqual((b["name"], c["name"], b["renamed"]), ("order-export-2.json", "order-export-3.json", True))
        self.assertEqual(self.text("order-export.json"), '{"order": 4812}')
        self.assertEqual(notes.create(self.m, "# heading", "", now=DAY1)["name"], "note-2026-11-12-1432.md")
        self.assertEqual(notes.create(self.m, "plain log", "run", "txt", now=DAY1)["name"], "run.txt")
        self.assertEqual(notes.create(self.m, "x", "../../evil/name", now=DAY1)["name"], "-..-evil-name.md")
        self.assertEqual(notes.create(self.m, "[1, 2]", "  .hidden ", now=DAY1)["name"], "hidden.json")
        d = notes.create(self.m, "{}", "stash", now=DAY1, line=True)
        self.assertTrue(self.text().endswith("- 14:32 Stashed stash.json\n"))
        self.assertEqual(d["line"]["name"], "notes.md")
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.dir))), ["Piper Notes"], "nothing outside the folder")
        with self.assertRaises(ValueError):
            notes.create(self.m, "x", "n", "../js")
        with self.assertRaises(ValueError):
            notes.create(self.m, None)

    def test_write_only_with_the_sha_it_was_read_with(self):
        notes.create(self.m, "v1\n", "draft.md")
        r = notes.read(self.m, "draft.md")
        self.assertEqual((r["text"], r["sha"]), ("v1\n", sha(b"v1\n")))
        w = notes.write(self.m, "draft.md", "v2", r["sha"])
        self.assertEqual((self.text("draft.md"), w["sha"]), ("v2\n", sha(b"v2\n")))
        with self.assertRaisesRegex(ValueError, "changed since you opened it"):
            notes.write(self.m, "draft.md", "v3", r["sha"])
        self.assertEqual(self.text("draft.md"), "v2\n")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            notes.write(self.m, "missing.md", "x", r["sha"])
        with self.assertRaises(ValueError):
            notes.write(self.m, "../x.md", "x", r["sha"])

    def test_read_limits_and_list_order(self):
        os.makedirs(self.dir)
        for name, t in (("old.md", 100), ("notes.md", 50), ("new.json", 200)):
            p = os.path.join(self.dir, name)
            with open(p, "w") as f:
                f.write("x\n")
            os.utime(p, (t, t))
        os.makedirs(os.path.join(self.dir, "sub"))
        with open(os.path.join(self.dir, ".DS_Store"), "w") as f:
            f.write("x")
        files = notes.status(self.m)["files"]
        self.assertEqual([(f["name"], f["main"]) for f in files], [("notes.md", True), ("new.json", False), ("old.md", False)])
        self.assertEqual([f.get("last") for f in files], ["x", None, None], "only the main note has its newest entry")
        with mock.patch.object(notes, "READ_MAX", 1):
            with self.assertRaises(projectfs.TooLarge):
                notes.read(self.m, "old.md")
        with open(os.path.join(self.dir, "bin.dat"), "wb") as f:
            f.write(b"\0\1\2")
        with self.assertRaises(projectfs.Binary):
            notes.read(self.m, "bin.dat")
        with self.assertRaises(projectfs.FsError):
            notes.read(self.m, "nothing.md")

    def test_search_names_lines_and_limits(self):
        notes.append(self.m, "Ask Anna about the Export limit", now=DAY1)
        notes.create(self.m, '{"export": true}', "export-sample")
        notes.create(self.m, "nothing here", "other")
        out = notes.search(self.m, "export")
        self.assertEqual(out["names"], ["export-sample.json"])
        self.assertEqual(sorted((h["name"], h["line"]) for h in out["hits"]), [("export-sample.json", 1), ("notes.md", 3)])
        with mock.patch.object(notes, "SEARCH_HITS", 1):
            self.assertTrue(notes.search(self.m, "export")["truncated"])
        for bad in ("", " ", "x" * 201, None):
            with self.assertRaises(ValueError):
                notes.search(self.m, bad)

    # ------------------------------------------------------------ refused folders and links
    def test_refused_folders(self):
        ext = os.path.join(self.tmp, "extension-protected")
        os.makedirs(ext)
        link = os.path.join(self.tmp, "link-to-state")
        os.symlink(common.PIPER_HOME, link)
        orig = extensions.collect
        bad = {
            common.PIPER_HOME: "Piper's own data",
            os.path.join(common.PIPER_HOME, "notes"): "Piper's own data",
            os.path.join(common.REAL_HOME, ".claude", "notes"): "Piper's own data",
            link: "Piper's own data",
            os.path.join(ext, "notes"): "Piper's own data",
            self.proot: "inside the project team-x/notesproj",
            os.path.join(self.proot, "notes"): "inside the project team-x/notesproj",
            os.path.dirname(self.proot): "holds the project team-x/notesproj",
            common.REAL_HOME: "too wide",
            "~": "too wide",
            "/": "too wide",
            "relative/notes": "not an absolute path",
            os.path.join(self.tmp, "repo", ".git", "notes"): "a .git path",
        }
        with mock.patch.object(extensions, "collect", side_effect=lambda name, *a: [ext] if name == "protected" else orig(name, *a)):
            for d, why in bad.items():
                with self.subTest(d=d):
                    self.configure(d)
                    st = notes.status(self.m)
                    self.assertEqual(st["state"], "refused")
                    self.assertIn(why, st["message"])
                    with self.assertRaisesRegex(notes.Refused, why):
                        notes.append(self.m, "x")
                    self.assertIn(why, notes.check_setting_dir(d))
        self.assertFalse(os.path.exists(os.path.join(common.PIPER_HOME, "notes", "notes.md")))
        self.assertIsNone(notes.check_setting_dir(self.dir))
        self.assertIn("Choose a folder outside projects", notes.status(self.m, self.proot)["message"])

    def test_links_are_never_followed(self):
        os.makedirs(self.dir)
        outside = os.path.join(self.tmp, "outside.md")
        with open(outside, "w") as f:
            f.write("keep\n")
        os.symlink(outside, os.path.join(self.dir, "notes.md"))
        os.symlink(outside, os.path.join(self.dir, "taken.md"))
        with self.assertRaisesRegex(notes.Refused, "symbolic link"):
            notes.append(self.m, "x", now=DAY1)
        with self.assertRaises(projectfs.FsError):
            notes.read(self.m, "notes.md")
        self.assertEqual(notes.create(self.m, "new", "taken.md")["name"], "taken-2.md")
        with self.assertRaises(ValueError):
            notes.write(self.m, "taken.md", "x", sha(b"keep\n"))
        with open(outside) as f:
            self.assertEqual(f.read(), "keep\n")
        self.assertEqual([f["name"] for f in notes.status(self.m)["files"]], ["taken-2.md"])
        # a folder configured through a link resolves to its real path (and is checked there)
        real = os.path.join(self.tmp, "real notes")
        os.makedirs(real)
        os.symlink(real, os.path.join(self.tmp, "alias"))
        self.configure(os.path.join(self.tmp, "alias"))
        self.assertEqual(notes.append(self.m, "via alias", now=DAY1)["path"], os.path.join(real, "notes.md"))

    # ------------------------------------------------------------ the OS refuses (macOS privacy protection)
    def test_eperm_is_the_denied_state_never_bypassed(self):
        mine = lambda a: str(a[0]).startswith(self.tmp)   # noqa: E731
        with mock.patch.object(sys, "platform", "darwin"), self.refuse("scandir", mine):
            st = notes.status(self.m)   # the folder does not exist yet: its parent cannot be opened
        self.assertEqual((st["state"], st["privacy"], st["fallback"]), ("denied", True, "~/Piper Notes"))
        self.assertIn("Privacy & Security", st["message"])
        with mock.patch.object(sys, "platform", "darwin"), self.refuse("mkdir", lambda a: a[0] in ("Documents", "Piper Notes")):
            with self.assertRaises(notes.Denied) as cm:
                notes.append(self.m, "x")
        self.assertEqual(cm.exception.info["state"], "denied")
        notes.append(self.m, "x", now=DAY1)   # existing folder, listing refused
        with mock.patch.object(sys, "platform", "darwin"), self.refuse("_open_dir", lambda a: a[0] == self.dir):
            st = notes.status(self.m)
            self.assertEqual((st["state"], st["privacy"]), ("denied", True))
            with self.assertRaises(notes.Denied):
                notes.append(self.m, "y")
            with self.assertRaises(notes.Denied):
                notes.read(self.m, "notes.md")
        self.assertEqual(notes.status(self.m)["state"], "ok", "Try again: the next check reads the folder again")

    @unittest.skipIf(os.geteuid() == 0, "root reads every folder")
    def test_open_privacy_opens_only_the_files_and_folders_pane_on_a_mac(self):
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(notes.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(notes.open_privacy(), {"ok": True})
            self.assertEqual(run.call_args.args[0],
                             ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"])
            run.return_value.returncode = 1
            with self.assertRaises(OSError):
                notes.open_privacy()
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(notes.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                notes.open_privacy()
            run.assert_not_called()

    def test_eacces_folder_is_denied_too(self):
        os.makedirs(self.dir)
        os.chmod(self.dir, 0)
        self.addCleanup(os.chmod, self.dir, 0o755)
        st = notes.status(self.m)
        self.assertEqual((st["state"], st["privacy"]), ("denied", False))

    # ------------------------------------------------------------ the folder as a Files external root
    def test_the_folder_is_a_files_external_root_with_a_space(self):
        notes.append(self.m, "hello", now=DAY1)
        key = notes.status(self.m)["key"]
        self.assertIn(" ", key)
        self.assertEqual([e["name"] for e in self.m.ext_fs(key, "list", {})["entries"]], ["notes.md"])
        r = self.m.ext_fs(key, "read", {"path": "notes.md"})
        self.m.ext_write(key, "notes.md", "edited", r["sha"])
        self.assertEqual(self.m.ext_rename(key, "notes.md", "renamed note.md"), {"from": "notes.md", "to": "renamed note.md"})
        self.assertEqual(self.m.ext_download(key, "renamed note.md"), b"edited\n")
        self.m.ext_delete(key, "renamed note.md")
        self.assertEqual(notes.status(self.m)["files"], [])

    # ------------------------------------------------------------ Settings > Notes
    def test_settings_section_validates_on_the_server(self):
        features.save({"notes": True})
        sec = settings.section("notes")
        self.assertEqual((sec.group, sec.module, [f.key for f in sec.fields]), ("tools", "notes", ["dir", "main", "stamp"]))
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("notes", {"dir": self.proot})
        self.assertIn("inside the project", cm.exception.errors["dir"])
        with self.assertRaises(settings.SettingsError) as cm:
            settings.update("notes", {"main": "../x.md", "stamp": "never"})
        self.assertEqual(set(cm.exception.errors), {"main", "stamp"})
        settings.update("notes", {"dir": self.dir, "main": "log.md", "stamp": "datetime"})
        self.assertEqual(notes.config(), {"dir": self.dir, "main": "log.md", "stamp": "datetime"})
        self.assertIn(("notes", "dir"), settings.AGENT_DENY)
        with self.assertRaises(settings.SettingsError):
            settings.agent_preview("notes", {"dir": self.dir})
        _env.set_modules(self, notes=False)
        self.assertNotIn("notes", [s["id"] for s in settings.describe()["sections"]])

    # ------------------------------------------------------------ routes
    def test_routes_full_gui_token_module_switch_and_local_only(self):
        old = piperd.MANAGER
        piperd.MANAGER = self.m
        self.addCleanup(setattr, piperd, "MANAGER", old)
        gui, tty = common.mint_token("notes-test"), common.own_token("tty")
        self.assertEqual(get("/api/notes", None)[0], 401)
        self.assertEqual(get("/api/notes", tty)[0], 403)
        self.assertEqual(post("/api/notes/append", {"text": "x"}, tty)[0], 403)
        self.assertFalse(os.path.exists(self.dir))
        code, st = get("/api/notes", gui)
        self.assertEqual((code, st["state"]), (200, "missing"))
        code, a = post("/api/notes/append", {"text": "via http"}, gui)
        self.assertEqual(code, 200, a)
        code, f = post("/api/notes/file", {"text": '{"a": 1}', "name": "snippet"}, gui)
        self.assertEqual((code, f["name"]), (200, "snippet.json"))
        code, r = get("/api/notes/read?name=snippet.json", gui)
        self.assertEqual((code, r["text"]), (200, '{"a": 1}'))
        self.assertEqual(post("/api/notes/write", {"name": "snippet.json", "text": "{}", "sha": "0" * 64}, gui)[0], 409)
        self.assertEqual(post("/api/notes/write", {"name": "snippet.json", "text": "{}", "sha": r["sha"]}, gui)[0], 200)
        code, s = get("/api/notes/search?q=via%20http", gui)
        self.assertEqual((code, len(s["hits"])), (200, 1))
        self.assertEqual(get("/api/notes/read?name=missing.md", gui)[0], 404)
        code, c = post("/api/notes/check", {"dir": self.proot}, gui)
        self.assertEqual((code, c["state"]), (200, "refused"))
        # a remote environment's header never sends the notes elsewhere
        with mock.patch.object(piperd.remotes, "proxy") as proxy:
            self.assertEqual(get("/api/notes", gui, "X-Piper-Environment: dev\r\n")[0], 200)
            self.assertEqual(post("/api/notes/append", {"text": "local"}, gui, "X-Piper-Environment: dev\r\n")[0], 200)
            proxy.assert_not_called()
        with mock.patch.object(sys, "platform", "darwin"), self.refuse("_open_dir", lambda a: a[0] == self.dir):
            code, d = post("/api/notes/append", {"text": "x"}, gui)
        self.assertEqual((code, d["state"], d["privacy"]), (409, "denied", True))
        # Open Privacy settings: piperd opens that one pane on this Mac (an older desktop app blocks the URL itself)
        self.assertEqual(post("/api/notes/privacy", {}, tty)[0], 403)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(notes.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(post("/api/notes/privacy", {"url": "file:///etc"}, gui)[0], 200)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["open", notes.PRIVACY_URL])
        _env.set_modules(self, notes=False)
        for path in ("/api/notes", "/api/notes/read?name=notes.md", "/api/notes/search?q=x"):
            self.assertEqual(get(path, gui)[0], 404, path)
        for op in ("append", "file", "write", "check", "privacy"):
            self.assertEqual(post("/api/notes/" + op, {"text": "off", "name": "a.md", "sha": "0" * 64}, gui)[0], 404, op)
        self.assertNotIn("off", self.text())


class NotesAgentTest(unittest.TestCase):
    """Phase 4: notes_list / notes_read / notes_append / notes_write of a project session (Hole or Direct), served
    by piperd through notes.py: the first call of a session asks on a card (notes_access) whatever Security says,
    Allow grants that session until it ends or is revoked, other sessions ask again, Deny / no answer end the call,
    a refused folder never cards, nothing deletes."""

    def setUp(self):
        import sessions
        self.sessions = sessions
        self.m = _env.manager(self)
        _env.set_modules(self, notes=True)
        tmp = os.path.realpath(tempfile.mkdtemp(dir=os.environ["TEST_SANDBOX_WORK"]))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.tmp = tmp
        self.dir = os.path.join(tmp, "Documents", "Piper Notes")
        os.makedirs(os.path.dirname(notes.CONFIG_FILE), exist_ok=True)
        common.write_json(notes.CONFIG_FILE, {"dir": self.dir})
        self.addCleanup(lambda: os.path.exists(notes.CONFIG_FILE) and os.unlink(notes.CONFIG_FILE))
        self.addCleanup(self.cancel_cards)
        self.checkout = os.path.realpath(tempfile.mkdtemp(dir=os.environ["TEST_SANDBOX_WORK"]))   # not the notes' parent
        self.addCleanup(shutil.rmtree, self.checkout, True)
        self.s = self.session()

    def session(self, runtime="hole"):
        s = self.sessions.Session(kind="project", project="team-x/notesproj", path=self.checkout, state="ready",
                                  runtime=runtime)
        self.m.sessions[s.id] = s
        self.addCleanup(self.m.sessions.pop, s.id, None)
        return s

    def cancel_cards(self):
        for p in list(self.m.pending.values()):
            p["_cancelled"] = "test cleanup"
            p["_ev"].set()

    def call(self, s, tool, answer=None, **args):
        """(result, error, card); answer None: no card may appear, True / False: Allow / Deny on the one card."""
        out, err = [], []

        def run():
            try:
                out.append(self.m.tool(s, tool, args))
            except ValueError as e:
                err.append(str(e))
        th = threading.Thread(target=run)
        th.start()
        card = None
        if answer is not None:
            deadline = time.monotonic() + 5
            while not self.m.pending_list() and time.monotonic() < deadline:
                time.sleep(.01)
            cards = self.m.pending_list()
            self.assertEqual(len(cards), 1, "one card")
            card = cards[0]
            self.m.decide(card["id"], answer)
        th.join(5)
        self.assertFalse(th.is_alive(), "the call waits for a card it should not have")
        self.assertEqual(self.m.pending_list(), [])
        return (out[0] if out else None), (err[0] if err else None), card

    def granted(self, s) -> bool:
        return notes.access_scope(self.m) in s.approvals

    def test_first_call_cards_allow_grants_the_session_then_no_card(self):
        for runtime in ("hole", "direct"):
            with self.subTest(runtime=runtime):
                s = self.session(runtime)
                r, err, card = self.call(s, "notes_write", True, name="order export.json", text='{"id": 4812}\n')
                self.assertIsNone(err)
                self.assertEqual(card["kind"], "notes_access")
                self.assertEqual(card["allow_label"], "Allow for this session")
                self.assertNotIn("session_always", card)   # Allow itself is the session grant
                self.assertNotIn("always", card)
                self.assertEqual(card["payload"]["folder"], notes.display(self.dir))
                self.assertEqual(card["payload"]["rights"], "read and write · no delete")
                self.assertTrue(card["summary"].startswith(notes.display(self.dir) + " · read and write · no delete\n"))
                self.assertIn("Wants to write order export.json.", card["summary"])
                self.assertIn("until it ends", card["summary"])
                self.assertNotIn("4812", json.dumps(card), "the card never carries the note's text")
                self.assertEqual(card["why"]["rule"], None)
                self.assertIn("only with your consent", card["why"]["label"])
                self.assertTrue(self.granted(s))
                name = r["name"]
                # granted: every tool without a card
                listed, err, _ = self.call(s, "notes_list")
                self.assertIsNone(err)
                self.assertIn(name, [f["name"] for f in listed["files"]])
                self.assertNotIn("path", listed)
                got, _, _ = self.call(s, "notes_read", name=name)
                self.assertEqual(got["text"], '{"id": 4812}\n')
                a, err, _ = self.call(s, "notes_append", text="Mailed it")
                self.assertIsNone(err)
                self.assertEqual(a["name"], "notes.md")
                w, err, _ = self.call(s, "notes_write", name=name, text="{}\n", sha=got["sha"])
                self.assertIsNone(err)
                with open(os.path.join(self.dir, name)) as f:
                    self.assertEqual(f.read(), "{}\n", "read -> write keeps one final newline")
                _, err, _ = self.call(s, "notes_write", name=name, text="[]", sha=w["sha"])
                self.assertIsNone(err)
                with open(os.path.join(self.dir, name)) as f:
                    self.assertEqual(f.read(), "[]\n")

    def test_another_session_asks_again_and_revoke_ends_the_grant(self):
        self.call(self.s, "notes_list", True)
        other = self.session()
        _, err, card = self.call(other, "notes_list", False)
        self.assertIn("denied this session access to Notes", err)
        self.assertEqual(card["session"], other.id)
        self.assertFalse(self.granted(other))
        self.assertIsNone(self.call(self.s, "notes_list")[1])
        self.m.revoke_approval(self.s.id, notes.access_scope(self.m))
        self.assertFalse(self.granted(self.s))
        self.call(self.s, "notes_list", True)   # asks again

    def test_deny_and_no_answer_write_nothing(self):
        _, err, _ = self.call(self.s, "notes_append", False, text="secret line")
        self.assertIn("the user denied", err)
        self.assertIn("Do not retry", err)
        self.assertFalse(os.path.exists(self.dir), "nothing created")
        self.assertFalse(self.granted(self.s))
        with mock.patch.object(notes, "ASK_S", 0.2):
            _, err, _ = self.call(self.s, "notes_append", None, text="secret line")
        self.assertIn("nobody answered", err)
        self.assertFalse(os.path.exists(self.dir))
        self.assertFalse(self.granted(self.s))

    def test_security_off_or_scoped_still_cards_once_per_session(self):
        self.m.policy["ask"] = []   # every switchable card off
        self.m.policy["auto_approve"] = [notes.ACCESS]
        self.call(self.s, "notes_list", True)
        self.assertIsNone(self.call(self.s, "notes_list")[1])
        self.m.policy["security_scoped"] = True   # no legacy session grant in ask() - this one is notes.py's own
        s = self.session()
        self.call(s, "notes_list", True)
        self.assertIsNone(self.call(s, "notes_list")[1])

    def test_the_end_of_the_session_ends_the_grant(self):
        self.call(self.s, "notes_list", True)
        self.m._set(self.s, "exited")
        self.assertFalse(self.granted(self.s))
        self.m._set(self.s, "ready")
        self.call(self.s, "notes_list", True)

    def test_module_off_bad_arguments_and_other_sessions_never_card(self):
        _env.set_modules(self, notes=False)
        with self.assertRaisesRegex(ValueError, "Notes module is off"):
            self.m.tool(self.s, "notes_list", {})
        _env.set_modules(self, notes=True)
        for tool, args in (("notes_read", {"name": "../x"}), ("notes_read", {}), ("notes_append", {"text": " "}),
                           ("notes_write", {"name": "a.md"}), ("notes_write", {"name": "a.md", "text": "x", "sha": "1"})):
            with self.subTest(tool=tool, args=args), self.assertRaises(ValueError):
                self.m.tool(self.s, tool, args)
        adv = self.sessions.Session(kind="advisor", project="", path=self.checkout, state="ready")
        with self.assertRaisesRegex(ValueError, "project sessions"):
            self.m.tool(adv, "notes_list", {})
        self.assertEqual(self.m.pending_list(), [])

    def test_a_refused_or_denied_folder_returns_its_state_without_a_card(self):
        common.write_json(notes.CONFIG_FILE, {"dir": common.PIPER_HOME})
        _, err, _ = self.call(self.s, "notes_list")
        self.assertIn("Notes are not available", err)
        self.assertIn("Piper's own data", err)
        common.write_json(notes.CONFIG_FILE, {"dir": self.dir})
        real = os.scandir

        def refuse(*a, **kw):
            if str(a[0]).startswith(self.tmp):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real(*a, **kw)
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(notes.os, "scandir", side_effect=refuse):
            _, err, _ = self.call(self.s, "notes_list")
            self.assertIn("macOS did not allow Piper", err)
            self.assertIn("do not retry", err)
        self.assertFalse(self.granted(self.s))

    def test_no_delete_path_and_write_never_overwrites_without_the_sha(self):
        self.assertEqual(notes.TOOLS, ("notes_list", "notes_read", "notes_append", "notes_write"))
        self.call(self.s, "notes_append", True, text="keep me")
        before = open(os.path.join(self.dir, "notes.md")).read()
        r, err, _ = self.call(self.s, "notes_write", name="notes.md", text="replaced")
        self.assertIsNone(err)
        self.assertEqual((r["name"], r["renamed"]), ("notes-2.md", True))
        self.assertEqual(open(os.path.join(self.dir, "notes.md")).read(), before)
        got, _, _ = self.call(self.s, "notes_read", name="notes.md")
        notes.append(self.m, "the user wrote meanwhile")
        _, err, _ = self.call(self.s, "notes_write", name="notes.md", text="stale", sha=got["sha"])
        self.assertIn("changed since you read it", err)
        self.assertIn("the user wrote meanwhile", open(os.path.join(self.dir, "notes.md")).read())
        for gone in ("notes_delete", "notes_trash", "notes_rename"):
            with self.subTest(tool=gone), self.assertRaises(ValueError):
                self.m.tool(self.s, gone, {"name": "notes.md"})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "notes.md")))

    def test_extension_protected_paths_still_apply_after_the_grant(self):
        # the extension hook `protected` is unchanged: a folder that holds a protected path is refused, granted or not
        self.call(self.s, "notes_append", True, text="x")
        protected = os.path.join(self.dir, "secret.md")
        with open(protected, "w") as f:
            f.write("private")
        orig = extensions.collect
        with mock.patch.object(extensions, "collect",
                               side_effect=lambda name, *a: [protected] if name == "protected" else orig(name, *a)):
            for tool, args in (("notes_read", {"name": "secret.md"}), ("notes_list", {})):
                with self.subTest(tool=tool):
                    r, err, _ = self.call(self.s, tool, **args)
                    self.assertIsNone(r)
                    self.assertIn("Notes are not available", err)
        self.assertTrue(self.granted(self.s))
        self.assertIn("secret.md", [f["name"] for f in self.call(self.s, "notes_list")[0]["files"]])

    def test_session_instructions_mention_the_tools_only_with_the_module(self):
        self.assertIn("`notes_append`", self.m._notes_md(self.s))
        self.assertIn("never through the shell", self.m._notes_md(self.s))
        self.assertIn("`notes_list`", self.m._extra_md(self.s))
        _env.set_modules(self, notes=False)
        self.assertEqual(self.m._notes_md(self.s), "")
        self.assertNotIn("notes_list", self.m._extra_md(self.s))


if __name__ == "__main__":
    unittest.main()
