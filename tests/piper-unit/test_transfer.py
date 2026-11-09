"""Move to server (transfer.py): the handoff request (wait / now, a card, the human's half-written prompt, a
manual Stop), Stop -> WIP commit with an ignored HANDOFF.md -> push card -> the peer's transfer -> moved, the
phase record across a piperd restart, a lost server answer (remote_unknown, check, never a replay), and the
server side creating the continuing session from the unpacked transfer."""
import base64
import io
import json
import os
import subprocess
import tarfile
import time
import unittest
from unittest import mock

import _env
import agentswitch
import common
import gitops
import remotes
import sessions
import transfer

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "core.hooksPath=/dev/null"]


def git(root, *args):
    return subprocess.run([*GIT, "-C", root, *args], check=True, capture_output=True, text=True).stdout


class TransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _env.manager(cls)

    def setUp(self):
        self.assertFalse(self.m.sessions, "previous test left sessions behind")
        self.addCleanup(_env.clear_sessions, self.m)
        self.addCleanup(setattr, gitops, "FAIL_CLOSED", gitops.FAIL_CLOSED)
        gitops.FAIL_CLOSED = False   # the fixture pushes into a local bare repository: the file transport
        self.env = remotes.save(dict(id="srv", label="Server", host="srv-alias", project_path="/srv/projects", mode="service"))
        self.addCleanup(remotes.remove, "srv")
        self.sent = []
        self.remote = mock.Mock(side_effect=lambda eid, body: self.sent.append(body) or {"session": "srv00001"})
        for target, attr, value in ((remotes, "transfer", self.remote), (self.m, "_transfer_spawn", mock.Mock()),
                                    (self.m, "ask", mock.Mock(return_value=True)),
                                    (self.m, "auto_type_ok", mock.Mock(return_value=True)),
                                    (self.m, "type_text", mock.Mock(return_value=True))):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def session(self, name, state="working"):
        root = os.path.realpath(_env.make_project(name, {"README.md": "x\n", ".gitignore": "HANDOFF.md\n.env\n"}))
        _env.register(name, root)
        bare = root + ".remote.git"
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        git(root, "init", "-q", "-b", "feature/move")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        git(root, "remote", "add", "origin", bare)
        git(root, "push", "-q", "-u", "origin", "feature/move")
        self.assertEqual(self.m.repo_top(root, record="start"), root)   # recorded before any sandbox ran
        s = sessions.Session(project=name, path=root, state=state, pid=4242, agent="claude", title="Shop work",
                             title_source="manual")
        self.m.sessions[s.id] = s
        s.save()
        self.stop = mock.patch.object(self.m, "stop", side_effect=lambda sid: setattr(s, "state", "stopping"))
        self.stop.start()
        self.addCleanup(self.stop.stop)
        return s, root, bare

    def run_until(self, s, cond, limit=20):
        for _ in range(limit):
            if cond():
                return
            if not self.m._transfer_step(s):
                break
        self.assertTrue(cond(), s.transfer)

    def agent_finishes(self, s, root, handoff=True):
        if handoff:
            with open(os.path.join(root, transfer.HANDOFF), "w") as f:
                f.write("# Handoff\ngoal\n")
        with open(os.path.join(root, "README.md"), "a") as f:
            f.write("work in progress\n")
        with open(os.path.join(root, ".env"), "w") as f:
            f.write("SECRET=never-moved\n")
        s.state, s.idle, s.stop_ts = "waiting", True, time.time() + 1

    # ------------------------------------------------------------------ the whole move
    def test_wait_mode_waits_for_the_turn_then_moves_the_work(self):
        s, root, bare = self.session("mv-wait")
        t = self.m.move(s.id, "srv", "wait")
        self.assertEqual((t["phase"], t["status"], t["mode"]), ("requested", "running", "wait"))
        self.m._transfer_spawn.assert_called_once()
        self.assertTrue(self.m._transfer_step(s))   # the turn still runs: nothing typed
        self.m.type_text.assert_not_called()
        self.assertEqual(s.transfer["phase"], "requested")
        s.state, s.idle = "waiting", True
        self.assertTrue(self.m._transfer_step(s))
        self.m.type_text.assert_called_once_with(s, transfer.PROMPT, idle_only=True)
        self.assertEqual(s.transfer["phase"], "handoff_sent")
        self.assertTrue(self.m._transfer_step(s))   # the agent has not answered yet (no Stop since the request)
        self.m.stop.assert_not_called()
        self.agent_finishes(s, root)
        self.run_until(s, lambda: s.state == "stopping")
        self.m.stop.assert_called_once_with(s.id)
        self.assertTrue(self.m._transfer_step(s))   # waits for the exit
        self.assertNotIn("stopped", s.transfer["at"])
        s.state = "exited"
        self.run_until(s, lambda: s.transfer["status"] != "running")
        t = s.transfer
        self.assertEqual((t["phase"], t["status"], t["remote_session"], t["pushed"]), ("moved", "done", "srv00001", True))
        self.assertEqual(list(t["at"]), list(transfer.PHASES))
        self.assertFalse(t["manual_stop"])
        self.assertEqual(s.moved["environment"], "srv")
        self.assertEqual(s.moved["session"], "srv00001")
        # the WIP commit holds the work and the ignored HANDOFF.md, never the hidden/ignored .env
        self.assertEqual(git(bare, "log", "-1", "--format=%s", "feature/move").strip(), transfer.message(s.id))
        files = git(bare, "show", "--name-only", "--format=", "feature/move").split()
        self.assertEqual(sorted(files), ["HANDOFF.md", "README.md"])
        self.m.ask.assert_called_once()
        self.assertEqual(self.m.ask.call_args.args[0], "git_push")
        # the archive: Piper's document and the facts, branch and commit of the pushed work
        body = self.sent[0]
        raw = base64.b64decode(body["archive"])
        self.assertEqual(body["sha"], __import__("hashlib").sha256(raw).hexdigest())
        with tarfile.open(fileobj=io.BytesIO(raw)) as tf:
            self.assertEqual(sorted(tf.getnames()), [agentswitch.FILE, transfer.META])
            meta = json.loads(tf.extractfile(transfer.META).read())
            doc = tf.extractfile(agentswitch.FILE).read().decode()
        self.assertEqual((meta["id"], meta["branch"], meta["source"], meta["title"], meta["project"]),
                         (t["id"], "feature/move", s.id, "Shop work", s.project))
        self.assertTrue(meta["commit"].endswith(transfer.message(s.id)))
        self.assertTrue(meta["handoff_file"])
        self.assertIn("# Handoff from Claude", doc)
        self.assertNotIn("never-moved", raw.decode("latin-1") + doc)
        # the record of truth on disk, the moved session locked for send / start, never removed
        self.assertEqual(common.read_json(os.path.join(s.dir, transfer.FILE), {})["phase"], "moved")
        self.assertEqual(sessions.Session(**common.read_json(os.path.join(s.dir, "meta.json"), {})).moved, s.moved)
        with self.assertRaisesRegex(ValueError, "moved to srv"):
            self.m.send(s.id, "hello")
        with self.assertRaisesRegex(ValueError, "moved to srv"):
            self.m.retry(s.id)
        with self.assertRaisesRegex(ValueError, "already moved"):
            self.m.move(s.id, "srv")
        self.assertIn(s.id, self.m.sessions)

    def test_now_mode_types_into_a_running_turn(self):
        s, _, _ = self.session("mv-now")
        self.m.move(s.id, "srv", "now")
        self.assertTrue(self.m._transfer_step(s))
        self.m.type_text.assert_called_once_with(s, transfer.PROMPT, idle_only=False)
        self.assertEqual(s.transfer["phase"], "handoff_sent")

    def test_a_card_is_never_typed_into(self):
        for mode in transfer.MODES:
            with self.subTest(mode=mode):
                s, root, _ = self.session("mv-card-" + mode, state="attention")
                s.idle = False   # a permission prompt
                self.m.move(s.id, "srv", mode)
                self.assertTrue(self.m._transfer_step(s))
                self.assertEqual(s.transfer["phase"], "blocked_on_card")
                self.m.type_text.assert_not_called()
                # a Piper card of this session blocks it too
                s.state, s.idle = "working", False
                with mock.patch.dict(self.m.pending, {"p1": {"session": s.id}}):
                    self.assertTrue(self.m._transfer_step(s))
                self.assertEqual(s.transfer["phase"], "blocked_on_card")
                self.m.type_text.assert_not_called()
                s.state = "exited"
                self.m.sessions.pop(s.id)

    def test_a_half_written_prompt_waits_for_send_now(self):
        s, _, _ = self.session("mv-held", state="waiting")
        s.idle = True
        self.m.move(s.id, "srv", "wait")
        self.m.auto_type_ok.return_value = False
        self.assertTrue(self.m._transfer_step(s))
        self.assertEqual(s.transfer["phase"], "waiting_send")
        self.m.type_text.assert_not_called()
        self.m.auto_type_ok.return_value = True   # "Send now" (input-clear)
        self.m.type_text.return_value = False     # ... but the human typed in the same instant
        with mock.patch.object(self.m, "input_pending", return_value=True):
            self.assertTrue(self.m._transfer_step(s))
        self.assertEqual(s.transfer["phase"], "waiting_send")
        self.m.type_text.return_value = True
        self.assertTrue(self.m._transfer_step(s))
        self.assertEqual(s.transfer["phase"], "handoff_sent")

    def test_a_manual_stop_moves_on_without_handoff_and_says_so(self):
        s, root, bare = self.session("mv-manual", state="working")
        self.m.move(s.id, "srv", "now")
        self.m._transfer_step(s)   # handoff_sent
        with open(os.path.join(root, "README.md"), "a") as f:
            f.write("half done\n")
        s.state = "stopping"   # the human's ordinary Stop
        self.assertTrue(self.m._transfer_step(s))
        s.state = "exited"
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual(s.transfer["phase"], "moved")
        self.assertTrue(s.transfer["manual_stop"])
        self.m.stop.assert_not_called()
        meta = json.loads(tarfile.open(fileobj=io.BytesIO(base64.b64decode(self.sent[0]["archive"]))).extractfile(transfer.META).read())
        self.assertTrue(meta["manual_stop"])
        self.assertFalse(meta["handoff_file"])
        self.assertIn(transfer.MANUAL_NOTE, transfer.task(meta, "/drop/handoff.md"))
        self.assertNotIn(transfer.MANUAL_NOTE, transfer.task(dict(meta, manual_stop=False), "/drop/handoff.md"))

    def test_a_declined_push_card_stops_the_move_without_replaying(self):
        s, root, bare = self.session("mv-declined", state="exited")
        with open(os.path.join(root, "README.md"), "a") as f:
            f.write("work\n")
        self.m.ask.return_value = False
        self.m.move(s.id, "srv")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        t = s.transfer
        self.assertEqual((t["phase"], t["status"], t["pushed"]), ("committed", "halted", False))
        self.assertIn("declined", t["error"])
        self.remote.assert_not_called()
        self.assertFalse(s.moved)
        # explicitly again: the commit is not repeated, the push card is asked again
        self.m.ask.return_value = True
        self.m.move(s.id, "srv")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual(s.transfer["phase"], "moved")
        self.assertEqual(git(root, "log", "--format=%s").splitlines().count(transfer.message(s.id)), 1)

    def test_a_stopped_move_can_be_cancelled_and_the_session_is_free_again(self):
        # Regression: a move halted at Commit (a content check refused it) left the session with no way back.
        s, root, _ = self.session("mv-cancel", state="exited")
        with open(os.path.join(root, "README.md"), "a") as f:
            f.write("work\n")
        with mock.patch.object(self.m, "git_commit", side_effect=gitops.GitError("refused by a pre-commit hook")):
            self.m.move(s.id, "srv")
            self.m._transfer_loop(s)   # the loop halts with the reason, as in piperd
        self.assertEqual((s.transfer["phase"], s.transfer["status"]), ("stopped", "halted"))
        self.assertIn("pre-commit hook", s.transfer["error"])
        self.assertEqual(self.m.move_cancel(s.id), {"transfer": None, "moved": None})
        self.assertIsNone(s.transfer)
        self.assertFalse(os.path.exists(os.path.join(s.dir, transfer.FILE)))
        self.assertIsNone(json.load(open(os.path.join(s.dir, "meta.json"))).get("transfer"))
        self.assertIn("work", open(os.path.join(root, "README.md")).read())   # the work stays uncommitted here
        self.remote.assert_not_called()
        with self.assertRaisesRegex(ValueError, "no move to cancel"):
            self.m.move_cancel(s.id)
        self.m.move(s.id, "srv")   # a new move starts from the beginning
        self.assertEqual(s.transfer["phase"], "requested")

    def test_a_running_or_possibly_created_move_cannot_be_cancelled(self):
        s, root, _ = self.session("mv-nocancel", state="exited")
        self.m.move(s.id, "srv")
        with self.assertRaisesRegex(ValueError, "still running"):
            self.m.move_cancel(s.id)
        self.remote.side_effect = remotes.Unavailable("SSH unavailable; the request was not retried.")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual(s.transfer["phase"], "remote_unknown")
        with self.assertRaisesRegex(ValueError, "check the server first"):
            self.m.move_cancel(s.id)

    def test_a_lost_server_answer_is_checked_never_replayed(self):
        s, root, _ = self.session("mv-lost", state="exited")
        self.remote.side_effect = remotes.Unavailable("SSH unavailable; the request was not retried.")
        self.m.move(s.id, "srv")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual((s.transfer["phase"], s.transfer["status"]), ("remote_unknown", "halted"))
        self.assertEqual(self.remote.call_count, 1)
        with self.assertRaisesRegex(ValueError, "check the server first"):
            self.m.move(s.id, "srv")
        state = {"sessions": [{"id": "other", "transferred_from": {"id": "f" * 16}}]}
        reply = {"status": 200, "body": base64.b64encode(json.dumps(state).encode()).decode()}
        with mock.patch.object(remotes, "proxy", return_value=reply) as proxy:
            r = self.m.move_check(s.id)
        proxy.assert_called_once_with("srv", "GET", "/api/state")
        self.assertFalse(r["found"])
        self.assertEqual((s.transfer["phase"], s.transfer["status"]), ("pushed", "halted"))
        self.assertEqual(self.remote.call_count, 1)   # the check itself sends nothing
        state["sessions"].append({"id": "srv00009", "transferred_from": {"id": s.transfer["id"]}})
        reply["body"] = base64.b64encode(json.dumps(state).encode()).decode()
        with mock.patch.object(remotes, "proxy", return_value=reply):
            r = self.m.move_check(s.id)
        self.assertEqual((r["found"], r["session"], s.moved["session"], s.transfer["phase"]), (True, "srv00009", "srv00009", "moved"))
        self.assertEqual(self.remote.call_count, 1)

    def test_a_refusal_before_creating_stops_after_push_for_an_explicit_retry(self):
        s, _, _ = self.session("mv-rejected", state="exited")
        self.remote.side_effect = remotes.Rejected("This server service cannot take moved work yet.")
        self.m.move(s.id, "srv")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual((s.transfer["phase"], s.transfer["status"]), ("pushed", "halted"))
        self.remote.side_effect = lambda eid, body: {"session": "srv00002"}
        self.m.move(s.id, "srv")
        self.run_until(s, lambda: s.transfer["status"] != "running")
        self.assertEqual(s.moved["session"], "srv00002")

    def test_a_restart_continues_after_the_last_phase_and_never_resends(self):
        s, _, _ = self.session("mv-restart", state="exited")
        self.m.move(s.id, "srv")
        self.m._transfer_step(s)   # the session had ended: no handoff
        self.m._transfer_step(s)   # stopped
        on_disk = common.read_json(os.path.join(s.dir, transfer.FILE), {})
        self.assertIn("stopped", on_disk["at"])
        s.transfer = None   # a new piperd: the meta.json copy is gone, transfer.json is the truth
        self.m._transfer_spawn.reset_mock()
        self.m._transfer_resume()
        self.assertEqual(s.transfer["at"], on_disk["at"])
        self.m._transfer_spawn.assert_called_once_with(s)
        self.m._transfer_set(s, phase="remote_sending")   # piperd died while the server was asked
        self.m._transfer_spawn.reset_mock()
        self.m._transfer_resume()
        self.assertEqual((s.transfer["phase"], s.transfer["status"]), ("remote_unknown", "halted"))
        self.m._transfer_spawn.assert_not_called()
        self.remote.assert_not_called()

    def test_refusals_at_the_start(self):
        s, _, _ = self.session("mv-refuse")
        remotes.save(dict(id="plain", label="SSH", host="srv-alias", project_path="/srv/projects", mode="ssh"))
        self.addCleanup(remotes.remove, "plain")
        with self.assertRaisesRegex(ValueError, "service mode"):
            self.m.move(s.id, "plain")
        with self.assertRaisesRegex(ValueError, "mode must be"):
            self.m.move(s.id, "srv", "kill")
        with self.assertRaises(ValueError):
            self.m.move(s.id, "missing")
        self.m.move(s.id, "srv")
        with self.assertRaisesRegex(ValueError, "being moved already"):
            self.m.move(s.id, "srv")

    # ------------------------------------------------------------------ the server side
    def test_the_server_creates_the_continuing_session_from_the_transfer(self):
        root = os.path.realpath(_env.make_project("mv-server", {"README.md": "x\n"}))
        _env.register("mv-server", root)
        tid = "d" * 16
        target = os.path.join(common.PIPER_HOME, transfer.DIR, tid)
        os.makedirs(target, exist_ok=True)
        meta = {"id": tid, "source": "abcd1234", "title": "Shop work", "branch": "feature/move",
                "commit": "1234567 wip: handoff abcd1234", "manual_stop": False,
                "issue": {"gl": "acme/shop", "iid": 7, "title": "Cart"}}
        common.write_json(os.path.join(target, transfer.META), meta)
        with open(os.path.join(target, agentswitch.FILE), "wb") as f:
            f.write(b"# Handoff from Claude\n")
        with mock.patch.object(self.m, "_start"):
            child = self.m.create("mv-server", transfer_in=tid, agent="claude")
            with self.assertRaisesRegex(ValueError, "already has a session"):
                self.m.create("mv-server", transfer_in=tid)
            with self.assertRaises(ValueError):
                self.m.create("mv-server", transfer_in="../x")
        self.assertEqual((child.transferred_from["id"], child.transferred_from["branch"]), (tid, "feature/move"))
        self.assertEqual((child.title, child.issue["iid"]), ("Shop work", 7))
        self.assertEqual(common.safe_read(child.dir, agentswitch.FILE, 1 << 20), b"# Handoff from Claude\n")
        with mock.patch.object(self.m, "box_in", return_value="/sandbox/.piper"):
            self.m._place_handoff(child)
        self.assertIn("feature/move", child.task)
        self.assertIn("HANDOFF.md", child.task)
        self.assertIn("/sandbox/.piper/%s/%s" % (child.names["drop"], agentswitch.FILE), child.task)
        self.assertEqual(common.safe_read(child.box, child.names["drop"] + "/" + agentswitch.FILE, 1 << 20),
                         b"# Handoff from Claude\n")
        self.assertIn("transferred_from", sessions.Session.LISTED)   # /api/state: move/check finds it
        child.state = "exited"


if __name__ == "__main__":
    unittest.main()
