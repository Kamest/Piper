"""GUI workbench: file browser, quick open, grep, conversation,
board, audit, send, network allow - the Manager side (piperd routes: test_piperd)."""
import json
import os
import unittest

import _env
import common
import projectfs
import sessions


class WorkbenchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)
        cls.root = _env.make_project("bench", {
            "src/A.java": "class A { int visible; }\n", "config/secrets/db.properties": "db.password=x\n",
            "config/secrets.yml": "<secret/>", ".git/HEAD": "ref", "notes.txt": "private notes\n",
            "lib/.git/HEAD": "ref", ".hole/settings.json": '{"files": {"exclude": ["notes.txt"]}}'})
        os.symlink("/etc", os.path.join(cls.root, "etc-link"))
        cls.key = "team-x/bench"

    def _session(self, **kw):
        s = sessions.Session(**dict({"project": self.key, "path": self.root, "state": "exited"}, **kw))
        self.m.sessions[s.id] = s
        return s

    def test_artifact_download_respects_file_boundaries_and_size(self):
        self.assertEqual(self.m.fs_download(self.key, "src/A.java"), b"class A { int visible; }\n")
        self.assertEqual(self.m.fs_download(self.key, "notes.txt"), b"private notes\n")
        for path in (".git/HEAD", "../outside", "etc-link/passwd", "src"):
            with self.assertRaises((ValueError, OSError), msg=path):
                self.m.fs_download(self.key, path)
        pdf = os.path.join(self.root, "report.pdf")
        with open(pdf, "wb") as f:
            f.write(b"%PDF-1.7\x00binary")
        self.addCleanup(lambda: os.path.exists(pdf) and os.unlink(pdf))
        self.assertEqual(self.m.fs_download(self.key, "report.pdf"), b"%PDF-1.7\x00binary")
        with open(pdf, "wb") as f:
            f.truncate((10 << 20) + 1)
        with self.assertRaises(projectfs.TooLarge):
            self.m.fs_download(self.key, "report.pdf")

    def test_listing_shows_hidden_entries_marked(self):
        """2026-10-07: a file hidden from Claude is the GUI's like any other - listed in full, marked."""
        r = self.m.fs(self.key, "list", {"path": "."})
        by = {e["name"]: e for e in r["entries"]}
        self.assertNotIn(".git", by)
        self.assertNotIn("etc-link", by)                      # leaves the project
        self.assertTrue(by["notes.txt"]["hidden"])
        self.assertEqual(by["notes.txt"]["size"], len("private notes\n"))
        self.assertFalse(by["src"]["hidden"])
        cfg = {e["name"]: e for e in self.m.fs(self.key, "list", {"path": "config"})["entries"]}
        self.assertTrue(cfg["secrets"]["hidden"] and cfg["secrets.yml"]["hidden"])
        inner = self.m.fs(self.key, "list", {"path": "config/secrets"})["entries"]
        self.assertEqual([(e["name"], e["hidden"]) for e in inner], [("db.properties", True)])
        self.assertNotIn(".git", {e["name"] for e in self.m.fs(self.key, "list", {"path": "lib"})["entries"]})

    def test_unlock_gates_project_reading_at_runtime(self):
        other = _env.make_project("readhome", {"src/H.java": "class H {}\n"})
        s = self._session(kind="project", project="team-x/readhome", path=other)
        with self.assertRaisesRegex(ValueError, "unlock it in the Piper GUI"):
            self.m.tool(s, "list_dir", {"project": self.key})
        self.m.set_unlock(s.id, "projects", True)
        names = {e["name"] for e in self.m.tool(s, "list_dir", {"project": self.key})["entries"]}
        self.assertIn("src", names)
        self.assertFalse({".git", "notes.txt", "etc-link"} & names)   # the target project's own rule
        with self.assertRaises(projectfs.FsError):
            self.m.tool(s, "read_file", {"project": self.key, "path": "config/secrets.yml"})
        with self.assertRaisesRegex(ValueError, "only available to the advisor"):
            self.m.tool(s, "spawn_session", {"project": self.key, "task": "x"})
        self.assertEqual(sessions.Session(**s.to_json()).unlock, {"projects": True})   # kept across restarts
        self.m.set_unlock(s.id, "projects", False)
        with self.assertRaises(ValueError):
            self.m.tool(s, "list_projects", {})
        with self.assertRaises(ValueError):
            self.m.set_unlock(s.id, "host", True)
        with self.assertRaises(ValueError):
            self.m.set_unlock(self._session(kind="advisor").id, "git", True)

    def test_editing_another_project_is_locked_then_confirmed_per_write(self):
        """Locked -> refused before any card. Unlocked at runtime -> every write is a card with the
        exact diff; declined -> nothing written; a change meanwhile -> refused. Walls that stay:
        own project, .git, hidden files, symlinks, a session working in that tree."""
        other = _env.make_project("home", {"src/H.java": "class H {}\n"})
        s = self._session(kind="project", project="team-x/home", path=other)
        edit = {"project": self.key, "path": "src/A.java", "old_string": "int visible;", "new_string": "int edited;"}
        asked = []

        def ask(kind, sess, summary, payload, **k):
            asked.append((kind, summary, payload))
            return self.answer
        self.answer = False
        self.m.policy["write_guard"] = True   # opt-in: host-run files on a card of their own, infrastructure never
        self.addCleanup(self.m.policy.pop, "write_guard", None)
        orig = self.m.ask
        self.m.ask = ask
        self.addCleanup(setattr, self.m, "ask", orig)
        with self.assertRaisesRegex(ValueError, "unlock it in the Piper GUI"):
            self.m.tool(s, "edit_file", edit)
        self.assertEqual(asked, [])                                   # locked: no card
        self.m.set_unlock(s.id, "projects_write", True)
        self.assertEqual(s.unlock, {"projects_write": True, "projects": True})   # writing implies reading
        with self.assertRaisesRegex(ValueError, "declined"):
            self.m.tool(s, "edit_file", edit)
        kind, summary, payload = asked[-1]
        self.assertEqual((kind, payload), ("edit_file", {"project": self.key, "path": "src/A.java", "flagged": False}))
        self.assertIn("-class A { int visible; }", summary)           # the exact diff is on the card
        self.assertIn("+class A { int edited; }", summary)
        with open(os.path.join(self.root, "src/A.java")) as f:
            self.assertEqual(f.read(), "class A { int visible; }\n")    # declined: untouched
        self.answer = True
        self.m.tool(s, "edit_file", edit)
        with open(os.path.join(self.root, "src/A.java")) as f:
            self.assertEqual(f.read(), "class A { int edited; }\n")
        r = self.m.tool(s, "write_file", {"project": self.key, "path": "src/new/pkg/N.java", "content": "class N {}\n"})
        self.assertTrue(r["created"])
        self.assertIn("create a file", asked[-1][1])
        self.m.tool(s, "write_file", {"project": self.key, "path": ".envrc", "content": "export X=1\n"})
        self.assertIn("runs on its own", asked[-1][1])                     # warned, not refused
        self.assertEqual(asked[-1][0], "write_exec")                       # ...and never by auto_approve
        os.remove(os.path.join(self.root, ".envrc"))
        import workbench
        orig_infra = workbench.INFRA_ROOTS
        workbench.INFRA_ROOTS = orig_infra | {os.path.realpath(self.root)}
        try:
            with self.assertRaisesRegex(ValueError, "never written from a session"):
                self.m.tool(s, "write_file", {"project": self.key, "path": "src/Z.java", "content": "x"})
        finally:
            workbench.INFRA_ROOTS = orig_infra

        def meanwhile(kind, sess, summary, payload, **k):             # the file changes while the card waits
            with open(os.path.join(self.root, "src/A.java"), "w") as f:
                f.write("class A { int theirs; }\n")
            return True
        self.m.ask = meanwhile
        with self.assertRaisesRegex(ValueError, "changed while"):
            self.m.tool(s, "write_file", {"project": self.key, "path": "src/A.java", "content": "mine\n"})
        with open(os.path.join(self.root, "src/A.java")) as f:
            self.assertEqual(f.read(), "class A { int theirs; }\n")
        self.m.ask = ask
        n = len(asked)
        for path in ("config/secrets/db.properties", "config/secrets/new.properties", "notes.txt",
                     ".git/config", "lib/.git/hooks/pre-commit", "etc-link/passwd", "../escape.txt", "/etc/passwd"):
            with self.assertRaises((ValueError, OSError), msg=path):
                self.m.tool(s, "write_file", {"project": self.key, "path": path, "content": "x"})
        self.assertEqual(len(asked), n)                               # walls: refused before any card
        self.assertFalse(os.path.exists(os.path.join(self.root, "config/secrets/new.properties")))
        own = self._session(kind="project", unlock={"projects_write": True, "projects": True}, hidden=True)
        with self.assertRaisesRegex(ValueError, "your own project"):
            self.m.tool(own, "write_file", {"project": self.key, "path": "x.txt", "content": "x"})
        busy = self._session(kind="project", state="working")
        with self.assertRaisesRegex(ValueError, "is working in that tree"):
            self.m.tool(s, "write_file", {"project": self.key, "path": "src/B.java", "content": "x"})
        busy.hidden = True
        self.m.set_unlock(s.id, "projects", False)                    # locking reading locks writing too
        self.assertEqual(s.unlock, {})
        with open(os.path.join(self.root, "src/A.java"), "w") as f:
            f.write("class A { int visible; }\n")
        import shutil
        shutil.rmtree(os.path.join(self.root, "src/new"))

    def test_compact_packages_follow_single_directory_chains(self):
        root = _env.make_project("pkgs", {
            "src/main/java/com/acme/shop/A.java": "a", "src/main/java/com/acme/shop/B.java": "b",
            "only/secret/config/secrets/x": "p", "lone/dir/file.txt": "f", ".git/HEAD": "ref"})
        by = lambda path: {e["name"]: e for e in self.m.fs("team-x/pkgs", "list", {"path": path})["entries"]}  # noqa: E731
        self.assertEqual(by(".")["src"]["chain"], "main/java/com/acme/shop")
        self.assertEqual(by("src/main/java")["com"]["chain"], "acme/shop")
        self.assertEqual(by(".")["lone"]["chain"], "dir")                # stops before a file
        self.assertEqual(by(".")["only"]["chain"], "secret/config")      # never into a hidden dir
        self.assertNotIn("chain", by("src/main/java/com/acme")["shop"])

    def test_second_session_in_a_checkout_is_told_and_named_apart(self):
        for x in self.m.sessions.values():
            x.hidden = True                                   # sessions of the other tests
        p = self.m.gui_project(self.key)
        a = self._session(state="working", title=p["title"], task="refactor the price list\nmore")
        self.assertEqual(self.m._free_title(p), p["title"] + " · 2")
        b = self._session(state="attesting", title=p["title"] + " · 2")
        self.assertEqual([x.id for x in self.m.siblings(b)], [a.id])
        note = self.m._shared_tree_note(b)
        self.assertIn("\"%s\" (task: refactor the price list)" % p["title"], note)
        self.assertNotIn("more", note)
        a.state = "exited"                                    # not live: no sibling any more
        self.assertEqual(self.m._shared_tree_note(b), "")
        a.hidden = b.hidden = True

    def test_state_changes_record_since(self):
        s = self._session(state="ready", since=1.0)
        self.m._set(s, "ready")
        self.assertEqual(s.since, 1.0)
        self.m._set(s, "waiting")
        self.assertGreater(s.since, 1.0)
        self.assertIn("since", s.to_json())

    def test_gui_reads_hidden_files_but_never_git(self):
        """2026-10-07: read, search and the file index show files hidden from Claude (marked, the read
        audited); `.git` and paths leaving the project never. The session tools keep hiding them."""
        self.assertIn("class A", self.m.fs(self.key, "read", {"path": "src/A.java"})["lines"][0])
        self.assertFalse(self.m.fs(self.key, "read", {"path": "src/A.java"})["hidden"])
        r = self.m.fs(self.key, "read", {"path": "config/secrets.yml"})
        self.assertEqual((r["lines"], r["hidden"]), (["1\t<secret/>"], True))
        self.assertTrue(self.m.fs(self.key, "read", {"path": "notes.txt"})["hidden"])
        with open(common.AUDIT_FILE) as f:
            ev = [json.loads(x) for x in f if x.strip()]
        self.assertIn({"event": "gui-read-hidden", "project": self.key, "path": "config/secrets.yml"},
                      [{k: e.get(k) for k in ("event", "project", "path")} for e in ev])
        self.assertNotIn("<secret/>", json.dumps(ev))                # names only
        # N4 review: a negative offset reads from the start too (projectfs clamps it) - it must be audited
        n = len(ev)
        self.assertEqual(self.m.fs(self.key, "read", {"path": "config/secrets.yml", "offset": "-5"})["lines"], ["1\t<secret/>"])
        with open(common.AUDIT_FILE) as f:
            ev2 = [json.loads(x) for x in f if x.strip()][n:]
        self.assertIn("gui-read-hidden", [e.get("event") for e in ev2])
        for bad in (".git/HEAD", "lib/.git/HEAD", "../bench/notes.txt", "/etc/passwd", "etc-link/hosts", ".GIT/HEAD"):
            with self.assertRaises(projectfs.FsError, msg=bad):
                self.m.fs(self.key, "read", {"path": bad})
        hits = self.m.fs(self.key, "grep", {"q": "password|secret|private|visible"})["matches"]
        self.assertEqual({h["path"] for h in hits},
                         {"src/A.java", "notes.txt", "config/secrets.yml", "config/secrets/db.properties"})
        files = self.m.fs(self.key, "files", {})["files"]
        self.assertTrue({"src/A.java", "notes.txt", "config/secrets.yml"} <= set(files))
        for f in files:
            self.assertFalse(f.startswith((".git", "lib/.git")), f)
        s = self._session(kind="project", unlock={"projects": True})   # a session still sees none of it
        with self.assertRaises(projectfs.FsError):
            self.m.tool(s, "read_file", {"project": self.key, "path": "notes.txt"})
        hits = self.m.tool(s, "grep", {"project": self.key, "pattern": "password|secret|private|visible"})["matches"]
        self.assertEqual({h["path"] for h in hits}, {"src/A.java"})

    def test_unknown_project_and_operation(self):
        with self.assertRaises(KeyError):
            self.m.fs("team-x/nope", "list", {})
        with self.assertRaises(ValueError):
            self.m.fs(self.key, "write", {})
        with self.assertRaises(ValueError):
            self.m.fs(self.key, "grep", {"q": ""})

    def _transcript(self, cfg, sid, lines):
        d = os.path.join(cfg, "projects", "-x-bench")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            for rec in lines:
                f.write(json.dumps(rec) + "\n")

    def test_conversation_usage_and_search(self):
        s = self._session(claude_session="c1")
        self._transcript(s.cfg, "c1", [
            {"type": "user", "message": {"content": "please fix the price list"}, "timestamp": "t1"},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Fixed the café menu."}],
                                              "usage": {"input_tokens": 10, "output_tokens": 5}}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "name": "Edit"}],
                                              "usage": {"input_tokens": 10, "output_tokens": 5}}},
        ])
        c = self.m.conversation(s.id)
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "assistant", "assistant"])
        self.assertEqual(c["usage"]["input"], 10)             # one message id counts once
        self.assertEqual(c["usage"]["messages"], 1)
        hits = self.m.search_conversations("CAFÉ")
        self.assertEqual([(h["session"], h["role"]) for h in hits], [(s.id, "assistant")])
        with self.assertRaises(ValueError):
            self.m.search_conversations("x")
        card = [c for c in self.m.board() if c["id"] == s.id][0]
        self.assertEqual(card["last"], "Fixed the café menu.")            # a bare tool call is not "the last answer"
        self.assertEqual(card["asked"], "please fix the price list")
        self.assertEqual(card["tokens"], 15)

    def test_host_mode_without_a_session_id_reads_no_transcript(self):
        # the host's ~/.claude holds the user's own conversations: never pick "the newest"
        self._transcript(os.path.join(common.REAL_HOME, ".claude"), "users-own", [
            {"type": "user", "message": {"content": "my private host conversation"}}])
        s = self._session(claude_mode="host")
        self.assertEqual(self.m.conversation(s.id)["turns"], [])
        adv = sessions.Session(kind="advisor", project="advisor", path=common.ADVISOR_DIR, state="ready")
        self.m.sessions[adv.id] = adv
        self.assertEqual(self.m.tool(adv, "session_digest", {"id": s.id}), [])

    def test_send_needs_a_live_session(self):
        s = self._session(state="exited")
        with self.assertRaises(ValueError):
            self.m.send(s.id, "hello")
        s.state = "waiting"
        with self.assertRaises(ValueError):
            self.m.send(s.id, "   ")

    def test_allow_domain_writes_the_project_settings(self):
        s = self._session()
        r = self.m.allow_domain(s.id, "Repo.Example.com:443")
        self.assertEqual(r, {"domain": "repo.example.com", "written": True})
        with open(os.path.join(self.root, ".hole", "settings.json")) as f:
            doc = json.load(f)
        self.assertEqual(doc["network"]["allow"], ["repo.example.com"])
        self.assertEqual(doc["files"]["exclude"], ["notes.txt"])            # the rest is kept
        self.assertFalse(self.m.allow_domain(s.id, "repo.example.com")["written"])
        for bad in ("gitlab.com", "x.gitlab.com", "*", "not a domain", "", "http://x.example/"):
            with self.assertRaises(ValueError, msg=bad):
                self.m.allow_domain(s.id, bad)
        adv = sessions.Session(kind="advisor", project="advisor", path=common.ADVISOR_DIR)
        self.m.sessions[adv.id] = adv
        with self.assertRaises(ValueError):
            self.m.allow_domain(adv.id, "repo.example.com")
        ev = self.m.audit_log(s.id, 10)
        self.assertEqual(ev[0]["event"], "allow-domain")
        self.assertTrue(all(e.get("session") == s.id for e in ev))


class WorkbenchHelpersTest(unittest.TestCase):
    def test_rel_resolver_strips_only_the_dot_slash_prefix(self):
        from types import SimpleNamespace
        import workbench
        wb = SimpleNamespace(fs=lambda project, op, args: {"files": ["src/.env", "env"]})
        resolve = workbench.Workbench._rel_resolver(wb, SimpleNamespace(project="p", kind="project"))
        self.assertEqual(resolve("./.env"), "src/.env")   # not "env": the leading dot of the name stays
        self.assertEqual(resolve("././env"), "env")

    def test_turn_event_never_takes_a_non_finite_stamp(self):
        """The hook file is sandbox-written: NaN / inf must not become turn_seen (NaN compares false forever)."""
        import math
        import threading
        from types import SimpleNamespace
        import workbench
        wb = SimpleNamespace(_turn_lock=threading.Lock())
        for bad in (float("nan"), float("inf")):
            s = SimpleNamespace(id="s1", turns=[], turn_seen=0.0, turn_q=[], turn_busy=True)   # busy: no worker
            workbench.Workbench._turn_event(wb, s, "Stop", {"ts": bad})
            self.assertTrue(math.isfinite(s.turn_seen), bad)
            self.assertTrue(all(math.isfinite(q[1]) for q in s.turn_q), bad)


class ProtectedWhatTest(unittest.TestCase):
    """the blocked panel says what a protected dir is (sessions.protected_what)"""

    def test_names_piper_checkout_and_home(self):
        self.assertEqual(sessions.protected_what(common.SK), "Piper's own code")
        self.assertIn("Piper's state", sessions.protected_what(common.PIPER_HOME + "/"))
        self.assertEqual(sessions.protected_what("/nowhere/else"), "a directory Piper protects")


if __name__ == "__main__":
    unittest.main()
