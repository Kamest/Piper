"""Review lane and file rewind: real temporary repositories, no live daemon or agent."""
import json
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
import unittest
from unittest import mock

import _env
import common
import gitops
import sessions
from test_workbench97 import git, wait, write


class TurnReviewTest(unittest.TestCase):
    def setUp(self):
        name = "review-" + secrets.token_hex(4)
        self.root = os.path.realpath(_env.make_project(name, {
            "src/app.txt": "base\n", "index.txt": "base index\n",
            ".gitattributes": "*.txt text eol=lf\n", ".gitignore": "scratch/\n"}))
        self.key = "acme/" + name
        _env.register(self.key, self.root)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.name", "Tester")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "Initial files")
        self.m = _env.manager(self)
        self.s = sessions.Session(project=self.key, path=self.root, state="waiting", idle=True)
        self.m.sessions[self.s.id] = self.s
        self.ts = time.time() - 60

    def event(self, ev):
        self.ts += 1
        self.s.state = "working" if ev == "UserPromptSubmit" else "waiting"
        self.s.idle = ev != "UserPromptSubmit"
        self.m._turn_event(self.s, ev, {"ts": self.ts, "prompt": "Update the example"})
        self.assertTrue(wait(lambda: not self.s.turn_busy), "snapshot worker did not finish")

    def turn(self, data, **files):
        self.event("UserPromptSubmit")
        write(self.root, "src/app.txt", data, "wb")
        for path, content in files.items():
            write(self.root, path, content, "wb")
        self.event("Stop")
        self.assertTrue(self.s.turns[-1]["to"], "finished turn has no snapshot")

    def preview(self, n=1, undo=""):
        return self.m.turn_revert_preview(self.s.id, n, undo)

    def apply(self, preview):
        return self.m.turn_revert(self.s.id, preview["n"], preview["current"], preview["undo"])

    def read(self, path):
        with open(os.path.join(self.root, path), "rb") as f:
            return f.read()

    def git_state(self):
        return (git(self.root, "rev-parse", "HEAD"), self.read(".git/index"), git(self.root, "show-ref"))

    def test_turn_refs_are_private_and_survive_both_gcs(self):
        self.turn(b"first\r\n\x00\xff")
        tree = self.s.turns[-1]["to"]
        private = self.m._snap_gitdir(self.s)
        ref = "refs/piper/turns/" + self.s.id + "/1"
        self.assertEqual(git(self.root, "--git-dir=" + private, "rev-parse", ref).strip(), tree)
        self.assertNotIn("refs/piper/", git(self.root, "show-ref"))
        self.assertNotIn(tree, git(self.root, "cat-file", "--batch-all-objects", "--batch-check"))
        git(self.root, "gc", "--prune=now")
        git(self.root, "--git-dir=" + private, "gc", "--prune=now")
        env = gitops.snap_env(self.root, self.m._snap_objects(self.s))
        oid = gitops.tree_versions(self.root, tree, ["src/app.txt"], self.m._snap_objects(self.s))["src/app.txt"]["oid"]
        self.assertEqual(gitops.read_blob(self.root, oid, env), b"first\r\n\x00\xff")
        self.assertTrue(gitops.is_tree(self.root, self.s.turns[0]["from"], self.m._snap_objects(self.s)))

    def test_revert_exact_bytes_modes_added_files_index_head_and_durable_undo(self):
        self.turn(b"one\r\n\x00\xff")
        os.chmod(os.path.join(self.root, "src/app.txt"), 0o755)
        # Capture the executable bit in the target turn too.
        self.turn(b"two\r\n\x00\xfe")
        self.turn(b"three\n", **{"src/later.txt": b"added later\n"})
        os.chmod(os.path.join(self.root, "src/app.txt"), 0o644)
        write(self.root, "index.txt", "staged by the user\n")
        git(self.root, "add", "index.txt")
        write(self.root, "unrelated.txt", "not part of any turn\n")
        before = self.git_state()
        p = self.preview(2)
        self.assertEqual({r["path"] for r in p["files"]}, {"src/app.txt", "src/later.txt", "index.txt"})
        removed = next(r for r in p["files"] if r["path"] == "src/later.txt")
        self.assertEqual((removed["removed"], removed["added_in"]), (True, 3))
        result = self.apply(p)
        self.assertIn("src/app.txt", self.m.fsedit_state(self.s)["paths"])
        private = self.m._snap_gitdir(self.s)
        self.assertEqual(git(self.root, "--git-dir=" + private, "rev-parse", "refs/piper/turns/" + self.s.id + "/undo-" + result["undo"]).strip(), self.s.reverts[-1]["before"])
        git(self.root, "--git-dir=" + private, "gc", "--prune=now")
        self.assertEqual(self.read("src/app.txt"), b"two\r\n\x00\xfe")
        self.assertTrue(os.stat(os.path.join(self.root, "src/app.txt")).st_mode & stat.S_IXUSR)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/later.txt")))
        self.assertEqual(self.read("unrelated.txt"), b"not part of any turn\n")
        self.assertEqual(self.git_state(), before)
        self.assertEqual([t["n"] for t in self.s.turns if t.get("reverted")], [3])
        self.assertIn("src/later.txt", self.s.revert_note)
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            self.s = sessions.Session(**json.load(f))   # daemon restart
        self.m.sessions[self.s.id] = self.s
        self.apply(self.preview(undo=result["undo"]))
        self.assertEqual(self.read("src/app.txt"), b"three\n")
        self.assertFalse(os.stat(os.path.join(self.root, "src/app.txt")).st_mode & stat.S_IXUSR)
        self.assertEqual(self.read("src/later.txt"), b"added later\n")
        self.assertEqual(self.read("index.txt"), b"staged by the user\n")
        self.assertEqual(self.git_state(), before)
        self.assertFalse(any(t.get("reverted") for t in self.s.turns))

    def test_unchanged_files_are_not_read_again(self):
        # Strict snapshots stream every file; one that did not change since the last turn is reused.
        real_ns, real_obj = time.time_ns, gitops.zlib.compressobj
        with mock.patch.object(gitops.time, "time_ns", lambda: real_ns() + 10 ** 10):
            self.turn(b"one\n")
            made = []
            with mock.patch.object(gitops.zlib, "compressobj", lambda *a: made.append(1) or real_obj(*a)):
                self.turn(b"two\n")
        self.assertEqual(len(made), 1, "only the changed file is read again")
        self.assertEqual({r["path"] for r in self.preview(1)["files"]}, {"src/app.txt"})
        gitops._BLOB_CACHE.clear()

    def test_revert_refuses_running_turn_and_stale_confirmation(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        write(self.root, "src/app.txt", "changed after preview\n")
        with self.assertRaisesRegex(ValueError, "Files changed since the preview"):
            self.apply(p)
        self.s.state = "working"
        for action in (lambda: self.preview(), lambda: self.apply(p)):
            with self.assertRaisesRegex(ValueError, "Stop the turn first"):
                action()
        self.assertEqual(self.read("src/app.txt"), b"changed after preview\n")
        self.assertFalse(self.s.reverts)

    def test_sibling_running_and_symlink_guards(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        other = sessions.Session(project=self.key, path=self.root, state="working")
        self.m.sessions[other.id] = other
        with self.assertRaisesRegex(ValueError, "Stop the turn first"):
            self.apply(p)
        other.hidden = True
        os.unlink(os.path.join(self.root, "src/app.txt"))
        os.symlink("../index.txt", os.path.join(self.root, "src/app.txt"))
        with self.assertRaises(ValueError):
            self.preview()
        self.assertEqual(self.read("index.txt"), b"base index\n")

    def test_oversized_target_refused_before_any_file_write(self):
        self.turn(b"x" * ((2 << 20) + 1))
        self.turn(b"small\n")
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            self.apply(self.preview())
        self.assertEqual(self.read("src/app.txt"), b"small\n")
        self.assertFalse(self.s.reverts)

    def test_partial_write_has_a_pinned_undo_and_no_false_applied_status(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        p = self.preview()
        with mock.patch.object(common, "safe_write", side_effect=OSError("fixture write refusal")):
            with self.assertRaises(OSError):
                self.apply(p)
        self.assertEqual(self.s.reverts[-1]["status"], "saved")
        self.assertEqual(common.audit_tail(1, self.s.id)[0]["status"], "saved")

    def test_review_every_changed_turn_done_prompt_push_and_persistence(self):
        self.turn(b"one\n")
        self.assertTrue(self.s.review_pending)
        self.assertEqual(self.s.review_summary["files"], 1)
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            restored = sessions.Session(**json.load(f))
        self.assertTrue(restored.review_pending)
        self.assertEqual(restored.review_summary, self.s.review_summary)
        self.m.review_done(self.s.id)
        self.assertFalse(self.s.review_pending)
        self.turn(b"two\n")
        self.assertTrue(self.s.review_pending)
        self.event("UserPromptSubmit")
        self.assertFalse(self.s.review_pending)
        self.event("Stop")   # unchanged turn does not enter
        self.assertFalse(self.s.review_pending)
        self.turn(b"three\n")
        with mock.patch.object(self.m, "hidden_deletions", return_value=[]), mock.patch.object(gitops, "push", return_value={"branch": "main"}):
            self.m.git_push(self.s.id)
        self.assertFalse(self.s.review_pending)

    def test_refs_removed_with_session_or_worktree(self):
        self.turn(b"one\n")
        private = self.m._snap_gitdir(self.s)
        self.m.forget_root(self.root)
        self.assertFalse(os.path.exists(private))
        self.assertFalse(self.s.turns)
        self.assertFalse(self.s.review_pending)
        # Same lifecycle method used by Remove, with the PTY explicitly stubbed.
        self.turn(b"new turn\n")
        self.assertTrue(os.path.isdir(private))
        self.s.state = "exited"
        self.m.pty = mock.Mock()
        self.m.pty.call.return_value = {"ok": True}
        self.m.forget(self.s.id)
        self.assertTrue(self.s.hidden)
        self.assertFalse(os.path.exists(private))

    def test_queued_note_uses_send_only_at_idle_and_has_no_file_contents(self):
        self.turn(b"one\n")
        self.turn(b"two\n")
        self.apply(self.preview())
        self.assertNotIn("one\n", self.s.revert_note)
        self.s.pid = 123
        self.s.state = "working"
        with mock.patch.object(self.m, "type_text", return_value=True) as typed:
            self.assertFalse(self.m.send(self.s.id, self.s.revert_note, idle_only=True))
            typed.assert_not_called()
            self.s.state, self.s.idle = "waiting", True
            with mock.patch.object(self.m, "auto_type_ok", return_value=True):
                self.assertTrue(self.m.send(self.s.id, self.s.revert_note, idle_only=True))
            self.assertEqual(typed.call_args.kwargs, {"idle_only": True})
        self.assertEqual(self.s.revert_note, "")

    def test_old_turns_are_pinned_before_revert_is_offered(self):
        tree = self.m._snapshot(self.s)
        self.s.turns = [{"n": 1, "from": tree, "to": tree, "started": 1, "ended": 2, "prompt": "Example"}]
        self.m.turns(self.s.id)
        self.assertEqual(git(self.root, "--git-dir=" + self.m._snap_gitdir(self.s), "rev-parse",
                             "refs/piper/turns/" + self.s.id + "/1").strip(), tree)

    def test_long_history_batches_snapshot_checks_and_reuses_saved_summaries(self):
        self.turn(b"one\n")
        turn = self.s.turns[0]
        self.s.turns = [dict(turn, n=n) for n in range(1, 61)]
        with mock.patch.object(gitops, "_git", wraps=gitops._git) as calls:
            self.m.turns(self.s.id)   # migrate the legacy refs once, as a batch
        self.assertLess(calls.call_count, 30, "legacy refs must also migrate without Git processes per turn")
        with mock.patch.object(gitops, "_git", wraps=gitops._git) as calls, \
                mock.patch.object(self.m, "_turn_summary", wraps=self.m._turn_summary) as summary:
            result = self.m.turns(self.s.id)
        self.assertEqual(len(result["turns"]), 60)
        self.assertFalse(any(t.get("unavailable") for t in result["turns"]))
        summary.assert_not_called()
        self.assertLess(calls.call_count, 30, "history must not start Git processes per saved turn")
        # A missing object must still be reported even when its ref is unchanged.
        oid = turn["to"]
        os.unlink(os.path.join(self.m._snap_objects(self.s), oid[:2], oid[2:]))
        # pinning may have packed the tree as well; remove those private packs too.
        shutil.rmtree(os.path.join(self.m._snap_objects(self.s), "pack"))
        result = self.m.turns(self.s.id)
        self.assertTrue(all(t.get("unavailable") for t in result["turns"]))

    def test_legacy_summary_is_computed_once_and_persisted(self):
        self.turn(b"one\n")
        self.s.turns[0].pop("summary")
        with mock.patch.object(self.m, "_turn_summary", wraps=self.m._turn_summary) as summary:
            self.assertEqual(self.m.turns(self.s.id)["turns"][0]["summary"]["files"], 1)
            self.m.turns(self.s.id)
        self.assertEqual(summary.call_count, 1)
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            self.assertEqual(json.load(f)["turns"][0]["summary"]["files"], 1)

    REFUSED = ("host git refused: the repository config names programs host git would run "
               "(filter.example.clean) - remove them from .git/config (or ask the user) first")

    def failing(self, ev, error=None):
        with mock.patch.object(self.m, "_snapshot", side_effect=error or gitops.GitError(self.REFUSED)):
            self.event(ev)

    def test_failed_snapshots_mark_the_turn_that_absorbs_several_prompts(self):
        # Regression: a refused snapshot dropped the hook silently, so one recorded turn held the
        # changes of every prompt during the refusal and Review presented it as a single turn.
        self.turn(b"one\n")
        self.event("UserPromptSubmit")                    # turn 2 starts normally
        write(self.root, "src/app.txt", b"two\n", "wb")
        self.failing("Stop")                              # ...its end is refused
        self.failing("UserPromptSubmit")                  # ...and so is the next prompt
        write(self.root, "index.txt", b"three\n", "wb")
        self.event("Stop")                                # the next snapshot works again
        self.assertEqual(len(self.s.turns), 2, "the refused prompt has no turn of its own")
        result = self.m.turns(self.s.id)
        first, second = result["turns"]
        self.assertNotIn("failed", first)
        self.assertEqual(second["failed"], 2)
        self.assertEqual(second["summary"]["files"], 2, "both prompts' changes land in the marked turn")
        self.assertIn("2 turn snapshots could not be taken", result["note"])
        self.assertIn("filter.example.clean", result["note"])
        with open(os.path.join(self.s.dir, "meta.json")) as f:
            restored = sessions.Session(**json.load(f))
        self.assertEqual([f["event"] for f in restored.turn_failures], ["Stop", "UserPromptSubmit"])
        self.assertNotIn("turn_failures", sessions.Session.LISTED)

    def test_failed_prompt_between_turns_marks_the_next_turn_and_hides_paths(self):
        self.turn(b"one\n")
        self.failing("UserPromptSubmit", OSError("cannot read " + self.root + "/src/app.txt"))
        write(self.root, "src/app.txt", b"between\n", "wb")
        self.failing("Stop", OSError("cannot read " + self.root + "/src/app.txt"))
        self.turn(b"after\n")
        result = self.m.turns(self.s.id)
        self.assertEqual([t.get("failed") for t in result["turns"]], [None, 2])
        self.assertNotEqual(result["turns"][1]["from"], result["turns"][0]["to"],
                            "the changes made meanwhile are in no recorded turn")
        self.assertIn("the snapshot could not be taken (OSError)", result["note"])
        self.assertNotIn(self.root, json.dumps(result))
        self.m.forget_root(self.root)
        self.assertEqual(self.s.turn_failures, [])

    def test_running_turn_claims_no_file_counts(self):
        # Regression: an open turn was summarized from-vs-from, so a long turn read "no files"
        # until its Stop, then showed everything at once.
        self.turn(b"one\n")
        self.event("UserPromptSubmit")
        write(self.root, "src/app.txt", b"still running\n", "wb")
        running = self.m.turns(self.s.id)["turns"][-1]
        self.assertIsNone(running["to"])
        self.assertNotIn("summary", running)
        self.event("Stop")
        self.assertEqual(self.m.turns(self.s.id)["turns"][-1]["summary"]["files"], 1)

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_review_is_only_a_tab_and_long_history_loads_quickly(self):
        import piperd
        self.turn(b"reviewed change\n")
        turn = self.s.turns[0]
        self.s.turns = [dict(turn, n=n, prompt="Example change %d" % n) for n in range(1, 121)]
        refs = "".join("update refs/piper/turns/%s/%s %s\n" % (self.s.id, name, tree)
                       for t in self.s.turns for name, tree in ((str(t["n"]) + "-start", t["from"]),
                                                              (str(t["n"]), t["to"])))
        subprocess.run(["git", "-C", self.root, "--git-dir=" + self.m._snap_gitdir(self.s),
                        "update-ref", "--stdin"], input=refs, text=True, check=True, capture_output=True)
        self.s.review_summary["turns"] = len(self.s.turns)
        self.s.save()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "review-tab-" + self.s.id

        def cli(*args):
            r = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(r.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            cli("wait", "--fn", code)

        def screenshot(name):
            path = os.path.join(os.environ["TEST_SANDBOX_WORK"], name)
            cli("screenshot", path)
            print("REVIEW_SCREENSHOT " + path, flush=True)

        with mock.patch.object(piperd, "MANAGER", self.m), mock.patch.object(piperd, "PORT", server.server_port):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1440", "1000")
                ready('!!document.querySelector(".tabs button[title=\\"turn changes, line notes and Revert to here\\"]")')
                cli("click", 'button[aria-label="Overview"]')
                ready('document.querySelector(".queue")?.getBoundingClientRect().width>200')
                self.assertFalse(cli("eval", 'document.querySelector(".queue").textContent.includes("To review")'))
                screenshot("review-overview.png")
                cli("eval", '''window.reviewRequests=[];const originalFetch=window.fetch;
                    window.fetch=(url,init)=>{if(/\\/(turns|review)([/?]|$)/.test(String(url)))window.reviewRequests.push(String(url));return originalFetch(url,init)};
                    window.reviewOpened=performance.now();document.querySelector('.tabs button[title="turn changes, line notes and Revert to here"]').click();true''')
                ready('document.querySelectorAll(".tr-turn").length===120 && document.querySelector(".tr-code")?.textContent.includes("reviewed change")')
                elapsed = cli("eval", 'performance.now()-window.reviewOpened')
                self.assertLess(elapsed, 2000, "Review must show a 120-turn history in under two seconds")
                self.assertEqual(cli("eval", 'window.reviewRequests.filter(u=>u.endsWith("/turns")).length'), 1)
                self.assertEqual(cli("eval", 'window.reviewRequests.filter(u=>/\\/review\\?/.test(u)).length'), 1)
                cli("eval", 'document.querySelector(".tr-pick").click();true')
                ready('document.querySelector(".tr-diff-title")?.textContent.startsWith("turn 120")')
                cli("eval", 'window.reviewOpened=performance.now();document.querySelector(".tr-all").click();true')
                ready('document.querySelector(".tr-diff-title")?.textContent==="All turns since the session started"')
                cached = cli("eval", 'performance.now()-window.reviewOpened')
                self.assertLess(cached, 1000)
                self.assertEqual(cli("eval", 'window.reviewRequests.filter(u=>u.endsWith("/turns")).length'), 1)
                self.assertEqual(cli("eval", 'window.reviewRequests.filter(u=>/\\/review\\?/.test(u)).length'), 2)
                screenshot("review-tab.png")
                print("REVIEW_TIMING 120 turns: %.0f ms; cached comparison: %.0f ms" % (elapsed, cached), flush=True)
                cli("eval", 'Array.from(document.querySelectorAll(".tr-foot button")).find(b=>b.textContent==="Done reviewing").click();true')
                ready('!document.querySelector(".tr-tab-new")')
                self.assertFalse(self.s.review_pending)
                self.assertFalse(cli("eval", 'document.querySelector(".queue").textContent.includes("To review")'))
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()

    @unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                         "set PIPER_BROWSER_TESTS=1 for the agent-browser regression")
    def test_browser_distinct_turns_and_a_large_diff_stay_responsive(self):
        # Regression: each turn must show its own files; a ~1 MiB / 600-file turn must not freeze the
        # GUI; a click must mark the new turn at once (not leave the previous "No files changed.");
        # the agent's edits must not reload and redraw a finished turn's diff.
        import piperd
        self.turn(b"first change\n")                                            # turn 1: src/app.txt
        big = {"gen/file%03d.txt" % i: "".join("generated line %03d of file %03d with some text\n" % (j, i)
                                               for j in range(30)).encode() for i in range(600)}
        self.turn(b"first change\n", **big)                                     # turn 2: 600 files
        self.turn(b"first change\n")                                            # turn 3: no files
        self.turn(b"first change\n", **{"index.txt": b"fourth turn only\n"})    # turn 4: index.txt
        self.assertEqual([t["summary"]["files"] for t in self.s.turns], [1, 600, 0, 1])
        self.s.turn_failures = [{"ts": self.s.turns[3]["ended"], "event": "Stop", "reason": self.REFUSED}]
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "review-large-" + self.s.id

        def cli(*args):
            r = subprocess.run(["agent-browser", "--session", browser, "--json", *args],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(r.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            cli("wait", "--fn", code)

        def files():
            return cli("eval", 'Array.from(document.querySelectorAll(".tr-file")).map(e=>e.dataset.file)')

        def pick(n):   # click, then read the GUI in the same task: before any response can arrive
            return cli("eval", '''(()=>{window.longTasks=[];document.querySelector('.tr-turn[data-turn="%d"] .tr-pick').click();
                const row=document.querySelector('.tr-turn[data-turn="%d"] .tr-pick');
                return {pressed:row?.getAttribute("aria-pressed"),detail:document.querySelector(".tr-detail").textContent}})()''' % (n, n))

        last = '!!document.querySelector(\'[data-file="gen/file599.txt"]\')'
        reviews = 'window.reviewRequests.filter(u=>/\\/review\\?/.test(u)).length'
        with mock.patch.object(piperd, "MANAGER", self.m), mock.patch.object(piperd, "PORT", server.server_port):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1440", "1000")
                ready('!!document.querySelector(".tabs button[title=\\"turn changes, line notes and Revert to here\\"]")')
                cli("eval", '''window.reviewRequests=[];window.longTasks=[];const originalFetch=window.fetch;
                    let frame=performance.now();const tickFrame=now=>{if(now-frame>50)window.longTasks.push(now-frame);frame=now;requestAnimationFrame(tickFrame)};requestAnimationFrame(tickFrame);
                    window.fetch=(url,init)=>{if(/\\/(turns|review)([/?]|$)/.test(String(url)))window.reviewRequests.push(String(url));return originalFetch(url,init)};true''')
                cli("eval", 'const end=performance.now()+120;while(performance.now()<end){};true')   # the probe: a known long task
                ready('window.longTasks.length>0')
                cli("eval", '''window.longTasks=[];window.reviewOpened=performance.now();
                    document.querySelector('.tabs button[title="turn changes, line notes and Revert to here"]').click();true''')
                ready('document.querySelectorAll(".tr-turn").length===4 && ' + last)
                opened = cli("eval", 'performance.now()-window.reviewOpened')
                open_tasks = cli("eval", 'window.longTasks.slice()')
                print("REVIEW_TIMING open All turns (602 files): %.0f ms, longest frame gap %.0f ms (%d over 50 ms)"
                      % (opened, max(open_tasks, default=0), len(open_tasks)), flush=True)
                self.assertEqual(len(files()), 602, "All turns lists every changed file")

                state = pick(1)
                self.assertEqual(state["pressed"], "true", "the clicked turn is marked before its diff arrives")
                self.assertNotIn("generated line", state["detail"], "the previous comparison stays on screen while loading")
                ready('document.querySelector(".tr-diff-title")?.textContent.startsWith("turn 1")')
                self.assertEqual(files(), ["src/app.txt"])
                pick(3)
                ready('document.querySelector(".tr-diff-title")?.textContent.startsWith("turn 3")')
                self.assertEqual(files(), [])
                self.assertIn("No files changed.", cli("eval", 'document.querySelector(".tr-detail").textContent'))
                state = pick(4)
                self.assertNotIn("No files changed.", state["detail"], "turn 3's empty result must not stay under turn 4")
                ready('document.querySelector(".tr-diff-title")?.textContent.startsWith("turn 4")')
                self.assertEqual(files(), ["index.txt"])
                self.assertTrue(cli("eval", 'document.querySelector(\'.tr-turn[data-turn="4"] .tr-meta\').textContent'
                                            '.startsWith("turn 4 · snapshot failed")'))
                self.assertIn("1 turn snapshot could not be taken", cli("eval", 'document.querySelector(".tr-detail").textContent'))
                shot = os.path.join(os.environ["TEST_SANDBOX_WORK"], "review-snapshot-failed.png")
                cli("screenshot", shot)
                print("REVIEW_SCREENSHOT " + shot, flush=True)
                cli("eval", 'window.reviewOpened=performance.now();true')
                pick(2)
                ready('document.querySelector(".tr-diff-title")?.textContent.startsWith("turn 2") && ' + last)
                turn2 = cli("eval", 'performance.now()-window.reviewOpened')
                turn2_tasks = cli("eval", 'window.longTasks.slice()')
                self.assertEqual(len(files()), 600)
                lines = cli("eval", 'document.querySelectorAll(".tr-line").length')
                print("REVIEW_TIMING turn 2 (600 files/%d lines): %.0f ms, longest frame gap %.0f ms (%d over 50 ms)"
                      % (lines, turn2, max(turn2_tasks, default=0), len(turn2_tasks)), flush=True)
                screenshot = os.path.join(os.environ["TEST_SANDBOX_WORK"], "review-large-turn.png")
                cli("screenshot", screenshot)
                print("REVIEW_SCREENSHOT " + screenshot, flush=True)
                self.assertLess(max(open_tasks + turn2_tasks, default=0), 400, "rendering must not freeze the GUI (frame gap)")

                # The agent keeps editing (fsedit) while a finished turn is shown: no reload, no redraw.
                cli("eval", 'window.shownNode=document.querySelector(".tr-file");true')
                before = cli("eval", reviews)
                for _ in range(3):
                    self.m._fsedit_event(self.s, {"ts": time.time(), "tool_name": "Edit",
                                                  "file_path": os.path.join(self.root, "src/app.txt")})
                    time.sleep(1.7)
                self.assertEqual(cli("eval", reviews), before, "a finished turn's comparison is not refetched")
                self.assertTrue(cli("eval", 'document.contains(window.shownNode)'), "a finished turn is not redrawn")
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()

    def test_subproject_notes_use_the_agents_relative_paths(self):
        self.key = "acme/subproject"
        _env.register(self.key, os.path.join(self.root, "src"))
        self.s.project, self.s.path = self.key, os.path.join(self.root, "src")
        self.turn(b"one\n")
        self.turn(b"two\n")
        self.assertEqual(self.m.review_files(self.s.id)["root"], self.root)
        self.apply(self.preview())
        self.assertIn("- app.txt", self.s.revert_note)
        self.assertNotIn("- src/app.txt", self.s.revert_note)

    def test_hidden_file_guard_and_notes_keep_hidden_names_from_agent(self):
        self.turn(b"one\n", **{"config/secrets/config.txt": b"fixture one\n"})
        self.turn(b"two\n", **{"config/secrets/config.txt": b"fixture two\n"})
        p = self.preview()
        with mock.patch.object(self.m, "_hidden_write_guard", side_effect=ValueError("hidden write refused")):
            with self.assertRaisesRegex(ValueError, "hidden write refused"):
                self.apply(p)
        self.assertFalse(self.s.reverts)
        self.s.state = "exited"   # no sandbox can see the restored bytes
        self.apply(self.preview())
        self.assertNotIn("config/secrets/config.txt", self.s.revert_note)
        events = json.dumps(common.audit_tail(20, self.s.id))
        self.assertNotIn("fixture one", events)
        self.assertNotIn("fixture two", events)


if __name__ == "__main__":
    unittest.main()
