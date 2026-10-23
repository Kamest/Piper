"""9.140 Continue with the other agent: the handoff document (agentswitch.py), where the previous agent last
worked (checkout / worktree / a worktree that is gone), Manager.switch_agent (stop the source, R2 hidden-files
confirmation, the new session's record) and the handoff's placement into the sandbox at every start."""
import json
import os
import subprocess
import unittest
import uuid
from unittest import mock

import _env
import agentswitch
import common
import digest
import gitops
import sessions

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null"]


def git(root, *args):
    subprocess.run([*GIT, "-C", root, *args], check=True, capture_output=True)


def codex_rec(kind, **payload):
    if kind == "turn_context":
        return {"type": "turn_context", "timestamp": "2026-10-21T10:00:00Z", "payload": payload}
    return {"type": "response_item", "timestamp": payload.pop("ts", "2026-10-21T10:00:00Z"), "payload": dict(type=kind, **payload)}


def say(role, text, ts="2026-10-21T10:00:00Z"):
    return codex_rec("message", role=role, ts=ts,
                     content=[{"type": "input_text" if role == "user" else "output_text", "text": text}])


class AgentSwitchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def setUp(self):
        self.assertFalse(self.m.sessions, "previous test left sessions behind")
        self.addCleanup(_env.clear_sessions, self.m)
        _env.set_modules(self, codex=True)

    def project(self, name, repo=False):
        root = os.path.realpath(_env.make_project(name, {"README.md": "x\n", "src/App.java": "class App {}\n"}))
        _env.register(name, root)
        if repo and not os.path.isdir(os.path.join(root, ".git")):
            git(root, "init", "-q", "-b", "main")
            git(root, "add", "-A")
            git(root, "commit", "-qm", "init")
        return root

    def codex_session(self, name, records, state="exited", repo=False, **kw):
        root = self.project(name, repo)
        s = sessions.Session(project=name, path=root, state=state, agent="codex", claude_mode="none",
                             claude_session=str(uuid.uuid4()), title=name + " · 1", title_source="auto", **kw)
        rel = "sessions/2026/10/21/rollout-2026-10-21T10-00-00-%s.jsonl" % s.claude_session
        recs = [{"type": "session_meta", "payload": {"id": s.claude_session, "cwd": root}},
                codex_rec("turn_context", cwd=root)] + records
        os.makedirs(os.path.dirname(os.path.join(s.claude_src, rel)), exist_ok=True)
        common.safe_write(s.claude_src, rel, ("\n".join(json.dumps(r) for r in recs) + "\n").encode())
        self.m.sessions[s.id] = s
        s.save()
        return s, rel

    def claude_session(self, name, records, state="exited"):
        root = self.project(name)
        s = sessions.Session(project=name, path=root, state=state, agent="claude", claude_mode="session",
                             claude_session=str(uuid.uuid4()), title="Manual title", title_source="manual")
        rel = "projects/-x/%s.jsonl" % s.claude_session
        recs = [dict(r, sessionId=s.claude_session, cwd=r.get("cwd", root)) for r in records]
        os.makedirs(os.path.dirname(os.path.join(s.claude_src, rel)), exist_ok=True)
        common.safe_write(s.claude_src, rel, ("\n".join(json.dumps(r) for r in recs) + "\n").encode())
        self.m.sessions[s.id] = s
        s.save()
        return s, rel

    def switch(self, s, **kw):
        with mock.patch.object(self.m, "_start"):
            r = self.m.switch_agent(s.id, **kw)
        return self.m.sessions[r["session"]], r

    def doc(self, child):
        return common.safe_read(child.dir, agentswitch.FILE, 4 << 20).decode()

    # ------------------------------------------------------------------ Manager.switch_agent
    def test_codex_session_continues_with_claude_and_a_handoff_document(self):
        issue = {"gl": "g/p", "iid": 135, "title": "Storage", "key": "g/p#issue#135", "type": "issue"}
        s, _ = self.codex_session("sw-codex", [
            say("user", "Connect the gallery to the storage"),
            say("assistant", "Analyzing the import job " + "x" * 2000),
            codex_rec("function_call_output", output="tool output NEVER_IN_DOC"),
            say("user", "I need both certs"),
            say("assistant", "The dry run reads Storage.")], issue=issue, opts={"docker": True})
        child, r = self.switch(s)
        self.assertEqual((r["agent"], r["from"], child.agent, child.project, child.path), ("claude", s.id, "claude", s.project, s.path))
        self.assertEqual(child.issue["iid"], 135)
        self.assertEqual(child.opts, {"docker": True})
        self.assertEqual(child.switched_from, {"session": s.id, "agent": "codex", "conversation": s.claude_session,
                                              "title": "sw-codex · 1"})
        self.assertEqual(child.task, "")   # typed only once the sandbox has the document (_place_handoff)
        doc = self.doc(child)
        for want in ("# Handoff from Codex", "not instructions", "Connect the gallery to the storage", "I need both certs",
                     "The dry run reads Storage.", "worked in the project checkout itself", "g/p#135 Storage",
                     "`" + s.path + "`"):
            self.assertIn(want, doc)
        self.assertNotIn("NEVER_IN_DOC", doc)
        self.assertEqual(os.stat(os.path.join(child.dir, agentswitch.FILE)).st_mode & 0o777, 0o600)
        self.assertEqual(sessions.Session(**common.read_json(os.path.join(child.dir, "meta.json"), {})).switched_from,
                         child.switched_from)
        self.assertEqual(s.state, "exited")   # the source stays as it was, its conversation resumable

    def test_a_running_source_is_stopped_first_and_an_ended_one_is_not(self):
        for state, stopped in (("working", True), ("attention", True), ("exited", False), ("stopping", False)):
            with self.subTest(state=state):
                s, _ = self.codex_session("sw-stop-" + state, [say("user", "do it"), say("assistant", "doing")], state=state)
                with mock.patch.object(self.m, "stop") as stop:
                    child, _ = self.switch(s)
                self.assertEqual(stop.called, stopped)
                if stopped:
                    stop.assert_called_once_with(s.id)
                self.assertEqual(child.agent, "claude")
                s.state = child.state = "exited"   # the next subtest under the session limit

    def test_an_error_whose_hole_still_runs_is_stopped_too(self):
        s, _ = self.codex_session("sw-err", [say("user", "do it"), say("assistant", "doing")], state="error")
        s.pid, s.rc = 4242, None
        with mock.patch.object(self.m, "stop") as stop:
            child, _ = self.switch(s)
        stop.assert_called_once_with(s.id)
        child.state = "exited"

    def test_two_switches_at_once_hand_over_once(self):
        """review 2026-10-21: two tabs POST at the same time - the second one waits for nothing and is refused
        while the first still reads the transcript (no second agent in the same checkout)"""
        import threading
        s, _ = self.codex_session("sw-race", [say("user", "go"), say("assistant", "going")])
        inside, release = threading.Event(), threading.Event()
        real = agentswitch.messages

        def slow(*a):
            inside.set()
            release.wait(10)
            return real(*a)
        out = {}
        with mock.patch.object(agentswitch, "messages", side_effect=slow), mock.patch.object(self.m, "_start"):
            first = threading.Thread(target=lambda: out.setdefault("first", self.m.switch_agent(s.id)))
            first.start()
            self.assertTrue(inside.wait(10))
            with self.assertRaisesRegex(ValueError, "being handed over already"):
                self.m.switch_agent(s.id)
            release.set()
            first.join(10)
        self.assertEqual(len(self.m.sessions), 2, "one new session")
        self.m.sessions[out["first"]["session"]].state = "exited"
        again, _ = self.switch(s)   # the in-flight mark is gone after the first one finished
        self.assertNotEqual(again.id, out["first"]["session"])
        again.state = "exited"

    def test_the_stopped_source_frees_its_slot_and_a_full_limit_leaves_the_source_running(self):
        """the source is stopped for the switch: at the limit it must not block its own replacement; a limit full
        of OTHER sessions refuses before anything is stopped"""
        s, _ = self.codex_session("sw-limit", [say("user", "do it"), say("assistant", "doing")], state="working")
        with mock.patch.object(self.m, "max_sessions", return_value=1), mock.patch.object(self.m, "stop") as stop:
            child, _ = self.switch(s)
            stop.assert_called_once_with(s.id)
            s.state, child.state = "working", "exited"
            other = sessions.Session(project="sw-limit", path=s.path, state="working", title="other")
            self.m.sessions[other.id] = other   # another session now fills the only slot
            with self.assertRaisesRegex(ValueError, "limit of 1"):
                self.switch(s)
            stop.assert_called_once_with(s.id)   # not stopped a second time
        self.assertEqual(len(self.m.sessions), 3)

    def test_claude_session_continues_with_codex_unless_the_module_is_off(self):
        s, _ = self.claude_session("sw-claude", [
            {"type": "user", "message": {"content": "Fix the invoice PDF"}, "timestamp": "2026-10-21T09:00:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed PdfRenderer."},
                                                          {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}]}}])
        _env.set_modules(self, codex=False)
        with self.assertRaisesRegex(ValueError, "Codex module is off"):
            self.switch(s)
        self.assertEqual(len(self.m.sessions), 1)
        _env.set_modules(self, codex=True)
        child, r = self.switch(s)
        self.assertEqual((child.agent, child.claude_mode, child.title), ("codex", "none", "Manual title"))
        doc = self.doc(child)
        self.assertIn("# Handoff from Claude", doc)
        self.assertIn("Fix the invoice PDF", doc)
        self.assertIn("Fixed PdfRenderer.", doc)

    def test_nothing_to_hand_over_and_unknown_sessions_are_refused(self):
        s, rel = self.codex_session("sw-empty", [])
        with self.assertRaisesRegex(ValueError, "no saved conversation"):
            self.switch(s)
        s.claude_session = None
        with self.assertRaisesRegex(ValueError, "no saved conversation"):
            self.switch(s)
        with self.assertRaises(KeyError):
            self.m.switch_agent("nope")
        self.assertEqual(len(self.m.sessions), 1)

    def test_a_second_switch_while_the_first_one_runs_is_refused(self):
        """a double click: the first switch's session is starting - no second Claude in the same checkout"""
        s, _ = self.codex_session("sw-twice", [say("user", "go"), say("assistant", "going")])
        child, _ = self.switch(s)
        child.state = "starting"
        with self.assertRaisesRegex(ValueError, "already continues in"):
            self.switch(s)
        self.assertEqual(len(self.m.sessions), 2)
        child.state = "exited"   # that one ended: the work can be handed over again
        again, _ = self.switch(s)
        self.assertNotEqual(again.id, child.id)

    def test_a_conversation_that_touched_hidden_files_needs_a_confirmation(self):
        """R2 as an import: the document carries what the transcript keeps - names only in the refusal, nothing
        created before an explicit True"""
        root = self.project("sw-hidden")
        secret = os.path.join(root, "config", "secrets", "db.properties")
        s, _ = self.codex_session("sw-hidden", [
            say("user", "check the db"),
            codex_rec("function_call", name="shell", arguments=json.dumps({"command": ["bash", "-lc", "cat " + secret]})),
            codex_rec("function_call_output", output="password=PLANTED_SW"),
            say("assistant", "the password is in db.properties")])
        with self.assertRaises(sessions.HiddenInTranscript) as cm:
            self.switch(s)
        self.assertEqual(cm.exception.paths, ["config/secrets/db.properties"])
        self.assertNotIn("PLANTED", str(cm.exception))
        with self.assertRaises(sessions.HiddenInTranscript):
            self.switch(s, confirm_hidden="true")
        self.assertEqual(len(self.m.sessions), 1)
        child, _ = self.switch(s, confirm_hidden=True)
        self.assertNotIn("PLANTED", self.doc(child))   # tool output never goes into the document

    def test_injected_context_naming_hidden_patterns_is_not_a_touch(self):
        """Live finding 2026-10-21: Piper's developer instructions in every Codex rollout list the hidden
        patterns (config/secrets, secrets.yml) - R2 asked for every Codex conversation"""
        s, _ = self.codex_session("sw-injected", [
            codex_rec("message", role="developer", content=[{"type": "input_text", "text":
                      "<!-- Piper --> hidden: config/secrets config/secrets.yml .env"}]),
            codex_rec("message", role="user", content=[{"type": "input_text", "text":
                      "# AGENTS.md instructions\n<INSTRUCTIONS> never read config/secrets </INSTRUCTIONS>"}]),
            say("user", "list the files"), say("assistant", "README.md")])
        child, _ = self.switch(s)   # no HiddenInTranscript
        self.assertEqual(child.switched_from["session"], s.id)
        self.assertEqual(sessions.transcript_paths(s.claude_src, digest.session_file(s.claude_src, s.claude_session, "codex"),
                                                   "codex")[0] & {"config/secrets", ".env"}, set())

    # ------------------------------------------------------------------ where it worked
    def test_the_worktree_it_last_worked_in_goes_into_the_document_and_the_task(self):
        root = self.project("sw-wt", repo=True)
        wt = os.path.join(root, ".claude", "worktrees", "feat-x")
        if not os.path.isdir(wt):
            git(root, "worktree", "add", "-q", "-b", "feat-x", wt)
        with open(os.path.join(wt, "src", "App.java"), "a") as f:
            f.write("// changed in the worktree\n")
        s, _ = self.codex_session("sw-wt", [
            say("user", "implement it in a worktree"),
            codex_rec("custom_tool_call", name="exec", input='exec_command({cmd:"git status", workdir:"%s"})' % wt,
                      ts="2026-10-21T11:00:00Z"),
            codex_rec("custom_tool_call_output", output="listing %s/.claude/worktrees/other" % root),
            say("assistant", "done in the worktree")], repo=True)
        child, _ = self.switch(s)
        self.assertEqual(child.switched_from["worktree"], os.path.realpath(wt))
        self.assertEqual(child.switched_from["branch"], "feat-x")
        doc = self.doc(child)
        self.assertIn("last worked in the worktree `%s` on branch `feat-x`" % os.path.realpath(wt), doc)
        self.assertIn("Branch `feat-x`", doc)   # git facts of the worktree, not of the checkout
        self.assertIn("`src/App.java`", doc)
        child.claude_mode = "session"
        self.m._place_handoff(child)
        self.assertIn("Work in the worktree %s (branch feat-x)" % os.path.realpath(wt), child.task)

    def test_a_worktree_outside_the_project_is_named_as_not_mounted_unless_hole_mounts_it(self):
        root = self.project("sw-out", repo=True)
        wt = os.path.realpath(root) + "-worktrees/out"   # git.worktreePool's place: mounted only with worktreeLinks
        if not os.path.isdir(wt):
            git(root, "worktree", "add", "-q", "-b", "out", wt)
        s, _ = self.codex_session("sw-out", [
            say("user", "work in the pool"),
            codex_rec("function_call", name="shell", arguments=json.dumps({"command": ["make"], "workdir": wt})),
            say("assistant", "built")], repo=True)
        s.mount_roots = [root]   # links off (hole's default): the project only
        child, _ = self.switch(s)
        self.assertEqual(child.switched_from["worktree"], wt)
        self.assertIn("lies outside the project and is **not mounted in this sandbox**", self.doc(child))
        child.claude_mode, child.mount_roots = "session", [root]
        self.m._place_handoff(child)
        self.assertIn("Its worktree %s is outside the project and not mounted in your sandbox - ask me" % wt, child.task)
        self.assertNotIn("Work in the worktree", child.task)
        _env.clear_sessions(self.m)
        s, _ = self.codex_session("sw-out", [
            say("user", "work in the pool"),
            codex_rec("function_call", name="shell", arguments=json.dumps({"command": ["make"], "workdir": wt})),
            say("assistant", "built")], repo=True)
        s.mount_roots = [root, os.path.realpath(root) + "-worktrees"]   # worktreePool: the pool is a library
        child, _ = self.switch(s)
        self.assertIn("on branch `out` (last used", self.doc(child))
        self.assertNotIn("not mounted", self.doc(child))
        child.claude_mode, child.mount_roots = "session", list(s.mount_roots)
        self.m._place_handoff(child)
        self.assertIn("Work in the worktree %s (branch out)" % wt, child.task)

    def test_a_later_command_in_the_checkout_brings_it_back(self):
        root = self.project("sw-back", repo=True)
        wt = os.path.join(root, ".claude", "worktrees", "tmp")
        if not os.path.isdir(wt):
            git(root, "worktree", "add", "-q", "--detach", wt)
        rel_dir = "sessions/x"
        recs = [codex_rec("function_call", name="shell", arguments=json.dumps({"command": ["bash", "-lc", "cd %s && make" % wt]})),
                codex_rec("function_call", name="shell", arguments=json.dumps({"command": ["git", "status"], "workdir": root}))]
        cfg = os.path.join(root, "..", "sw-back-cfg")
        os.makedirs(os.path.join(cfg, rel_dir), exist_ok=True)
        with open(os.path.join(cfg, rel_dir, "r.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        wts = gitops.worktrees(root)
        self.assertEqual(agentswitch.last_worktree(cfg, rel_dir + "/r.jsonl", "codex", root, wts), {"where": "checkout"})
        with open(os.path.join(cfg, rel_dir, "r.jsonl"), "w") as f:
            f.write(json.dumps(recs[0]) + "\n")
        w = agentswitch.last_worktree(cfg, rel_dir + "/r.jsonl", "codex", root, wts)
        self.assertEqual((w["where"], w["path"], w["exists"], w["rel"]), ("worktree", os.path.realpath(wt), True, ".claude/worktrees/tmp"))
        # a file of the checkout or the checkout's name inside a longer word is not "back in the checkout"
        with open(os.path.join(cfg, rel_dir, "r.jsonl"), "w") as f:
            f.write(json.dumps(recs[0]) + "\n" + json.dumps(codex_rec("function_call", name="shell", arguments=json.dumps(
                {"command": ["cat", root + "/README.md", root + "-other"]}))) + "\n")
        self.assertEqual(agentswitch.last_worktree(cfg, rel_dir + "/r.jsonl", "codex", root, wts)["where"], "worktree")

    def test_a_worktree_that_is_gone_is_named_and_not_recreated(self):
        s, _ = self.claude_session("sw-gone", [
            {"type": "user", "message": {"content": "go on"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "cd .claude/worktrees/old-fix && git log"}}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}])
        child, _ = self.switch(s)
        gone = os.path.join(s.path, ".claude/worktrees/old-fix")
        self.assertEqual(child.switched_from["worktree"], gone)
        self.assertIn("which **no longer exists**", self.doc(child))
        self.assertIn("Unavailable: the worktree no longer exists", self.doc(child))
        child.claude_mode = "none"
        self.m._place_handoff(child)
        self.assertIn("Its worktree %s no longer exists - ask me" % gone, child.task)

    def test_a_claude_cwd_inside_a_worktree_counts_the_checkout_cwd_does_not(self):
        root = self.project("sw-cwd", repo=True)
        wt = os.path.join(root, ".claude", "worktrees", "c1")
        if not os.path.isdir(wt):
            git(root, "worktree", "add", "-q", "--detach", wt)
        cfg = os.path.join(root, "..", "sw-cwd-cfg")
        os.makedirs(os.path.join(cfg, "p"), exist_ok=True)
        recs = [{"type": "user", "cwd": wt, "message": {"content": "x"}}, {"type": "assistant", "cwd": root, "message": {"content": "y"}}]
        with open(os.path.join(cfg, "p", "r.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n")
        w = agentswitch.last_worktree(cfg, "p/r.jsonl", "claude", root, gitops.worktrees(root))
        self.assertEqual(w["path"], os.path.realpath(wt))

    # ------------------------------------------------------------------ placement at every start
    def test_every_start_places_the_document_the_first_one_types_the_task(self):
        s, _ = self.codex_session("sw-place", [say("user", "start"), say("assistant", "started")])
        child, _ = self.switch(s)
        for mode in ("session", "host"):
            with self.subTest(mode=mode):
                child.claude_mode, child.task, child.task_sent = mode, "", False
                self.m._place_handoff(child)
                placed = os.path.join(child.box, child.names["drop"], agentswitch.FILE)
                self.assertEqual(open(placed, "rb").read(), open(os.path.join(child.dir, agentswitch.FILE), "rb").read())
                self.assertEqual(os.stat(placed).st_mode & 0o777, 0o600)
                path_in = "%s/%s/%s" % (self.m.box_in(child), child.names["drop"], agentswitch.FILE)
                self.assertIn("First read %s" % path_in, child.task)
                self.assertIn("Codex started", child.task)
                self.assertTrue(child.task.startswith('Continue "sw-place · 1" (from Codex). '), child.task)   # History's title
                os.unlink(placed)
                child.task_sent, child.task = True, "already typed"
                self.m._place_handoff(child)   # a later start (resume): the file again, the task untouched
                self.assertTrue(os.path.exists(placed))
                self.assertEqual(child.task, "already typed")
        long = agentswitch.task("claude", "/x/handoff.md", {"where": "checkout"}, "a\nb  " + "c" * 200)
        self.assertTrue(long.startswith('Continue "a b %s" (from Claude). You continue work Claude' % ("c" * 76)), long)
        self.assertTrue(agentswitch.task("claude", "/x", {"where": "checkout"}).startswith("You continue work Claude"))
        plain = sessions.Session(project="sw-place", path=s.path, claude_mode="session")
        self.m._place_handoff(plain)   # not a switched session: nothing
        self.assertFalse(os.path.exists(os.path.join(plain.box, plain.names["drop"], agentswitch.FILE)))

    # ------------------------------------------------------------------ the document itself
    def test_document_shortens_old_agent_messages_and_bounds_its_size(self):
        rows = [{"role": "user", "text": "u%d" % i, "ts": 0} for i in range(3)]
        rows += [{"role": "assistant", "text": "A%d " % i + "y" * 1000, "ts": 1792500000} for i in range(30)]
        doc = agentswitch.document({"agent": "codex", "session": "s", "path": "/p"}, rows, {"where": "checkout"},
                                   {"error": "no repo"})
        self.assertIn("A0 " + "y" * (agentswitch.SHORT - 3) + " …", doc)   # older ones shortened
        self.assertIn("A29 " + "y" * 1000 + "\n", doc)   # the last FULL_TAIL whole
        self.assertIn("Unavailable: no repo", doc)
        self.assertIn("### Codex · 2026-10-20 ", doc)
        self.assertIn("### **User**\n\nu0", doc)
        big = [{"role": "user", "text": "z" * agentswitch.MSG_MAX, "ts": 0} for _ in range(60)]
        doc = agentswitch.document({"agent": "claude", "session": "s", "path": "/p"}, big, {"where": "checkout"}, {})
        self.assertLess(len(doc), agentswitch.DOC_MAX + 5000)
        self.assertIn("oldest messages left out for size", doc)

    def test_document_is_scrubbed(self):
        doc = agentswitch.document({"agent": "codex", "session": "s", "path": "/p"},
                                   [{"role": "assistant", "text": "token glpat-ABCDEFGHIJKLMNOPQRST1234", "ts": 0}],
                                   {"where": "checkout"}, {})
        self.assertNotIn("glpat-ABCDEFGHIJKLMNOPQRST1234", doc)

    def test_messages_skip_tools_and_injected_context(self):
        s, _ = self.codex_session("sw-msgs", [
            codex_rec("message", role="user", content="# AGENTS.md instructions hidden"),
            codex_rec("message", role="developer", content="injected"),
            say("user", "real request"),
            codex_rec("reasoning", summary=[]),
            say("assistant", "real answer")])
        rows = agentswitch.messages(s.claude_src, s.claude_session, "codex")
        self.assertEqual([(r["role"], r["text"]) for r in rows], [("user", "real request"), ("assistant", "real answer")])
        self.assertEqual(digest.session_file(s.claude_src, "../x", "codex"), None)

    def test_shell_runs_are_not_user_messages_and_a_compaction_summary_is_labelled(self):
        """review 2026-10-21 on 530e3ce6: Codex stores a `!` shell run (command + OUTPUT) as a user message; Claude's
        compaction summary is a user-role record - neither may read as the user's request in the document"""
        s, _ = self.codex_session("sw-shell", [
            say("user", "<user_shell_command>\n<command>\ncat x</command>\n<result>Exit code 0 Output: PLANTED_SHELL</result>"),
            say("user", "real request"), say("assistant", "real answer")])
        rows = agentswitch.messages(s.claude_src, s.claude_session, "codex")
        self.assertEqual([r["text"] for r in rows], ["real request", "real answer"])
        c, _ = self.claude_session("sw-compact", [
            {"type": "user", "isCompactSummary": True, "message": {"content": "This session is being continued ... SUMMARY_X"}},
            {"type": "user", "message": {"content": "go on"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}])
        rows = agentswitch.messages(c.claude_src, c.claude_session, "claude")
        self.assertEqual([r["role"] for r in rows], ["summary", "user", "assistant"])
        doc = agentswitch.document({"agent": "claude", "session": "x", "path": "/p"}, rows, {"where": "checkout"}, {})
        self.assertIn("### Claude's summary of the earlier conversation (context compaction)", doc)
        self.assertNotIn("**User**\n\nThis session", doc.replace(" · ", "\n\n"))
        self.assertEqual(doc.count("### **User**"), 1)
        self.assertEqual(agentswitch.messages(s.claude_src, "../x", "codex"), [])


if __name__ == "__main__":
    unittest.main()
