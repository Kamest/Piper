"""Approval evidence: host facts, bounded collection, safe previews, audit and grant scope."""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env
import common
import dbsql
import dbtools
import evidence
import github
import gitlab
import security
import sessions


class EvidenceTest(unittest.TestCase):
    def test_git_outgoing_commits_diff_scan_and_forge(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q", "-b", "feature/topic")
            git("config", "user.name", "Example")
            git("config", "user.email", "test@example.com")
            git("remote", "add", "origin", "https://example.com/acme/shop.git")
            for i in range(5):
                with open(os.path.join(root, "app.txt"), "a") as f:
                    f.write("line %d\n" % i)
                git("add", "app.txt")
                git("commit", "-qm", "Change %d" % i)
            forge = mock.Mock(return_value={"exists": False, "protected": False, "ci": "main: CI passed"})
            facts = evidence.git_push(root, forge)
            text = json.dumps(facts)
            self.assertIn("new branch feature/topic", text)
            self.assertIn("NOT PROTECTED", text)
            self.assertIn("2 more", text)
            self.assertEqual(sum(bool(r.get("code")) for r in facts["rows"]), 3)
            self.assertIn("main: CI passed", text)
            self.assertIn("diff", facts)
            git("update-ref", "refs/remotes/origin/feature/topic", "HEAD~1")
            facts = evidence.git_push(root, lambda *a: {})
            self.assertIn("existing branch feature/topic", facts["rows"][0]["text"])
            self.assertFalse(facts["push"]["verified"])
            self.assertNotIn("chips", facts["rows"][0])
            # Synthetic sentinel; assertion failures never print its value or the unsafe diff.
            sentinel = "synthetic-" + "credential-value"
            with open(os.path.join(root, "app.txt"), "a") as f:
                f.write("\nAPI_TOKEN=" + sentinel + "\n")
            git("add", "app.txt")
            git("commit", "-qm", "Configure token")
            facts = evidence.git_push(root, lambda *a: {})
            self.assertTrue(sentinel not in json.dumps(facts), "secret value escaped the diff scrubber")
            self.assertTrue(any("Secret-like" in r["text"] for r in facts["rows"]))

    def test_new_branch_without_upstream_has_one_outgoing_commit_not_zero(self):
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root:
            def git(*args):
                return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, check=True).stdout.strip()
            git("init", "-q", "-b", "feature/topic")
            git("config", "user.name", "Example")
            git("config", "user.email", "test@example.com")
            git("remote", "add", "origin", "https://example.com/acme/shop.git")
            with open(os.path.join(root, "app.txt"), "w") as f:
                f.write("base\n")
            git("add", "app.txt")
            git("commit", "-qm", "Base")
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            with open(os.path.join(root, "app.txt"), "a") as f:
                f.write("change\n")
            git("commit", "-qam", "One outgoing change")
            plan = sessions.gitops.push_plan(root)
            status = sessions.gitops.status(root)
            self.assertEqual(status["upstream"], "")
            self.assertEqual(status["ahead"], 0)  # no upstream count, not an outgoing count
            facts = evidence.git_push(root, lambda *a: {}, plan=plan)
            self.assertIn("new branch feature/topic", facts["rows"][0]["text"])
            self.assertNotIn("whether the remote branch is new or existing", json.dumps(facts))
            self.assertEqual([r["code"] for r in facts["rows"] if "code" in r], [git("rev-parse", "--short", "HEAD")])
            self.assertEqual(facts["diffstat"], {"files": 1, "added": 1, "removed": 0})
            self.assertFalse(facts["push"]["verified"])  # local facts never widen an hourly grant

    def test_push_collector_uses_the_cards_plan_snapshot_even_for_main(self):
        m = sessions.Manager.__new__(sessions.Manager)
        m._session_top = mock.Mock(return_value="/workspace/acme/shop")
        for new in (False, True):
            payload = {"branch": "main", "remote": "origin", "push_new": new}
            with mock.patch.object(evidence, "git_push", return_value=evidence.box()) as collect:
                m._card_evidence("git_push", None, payload, "Push")
            self.assertEqual(collect.call_args.kwargs["plan"], {"branch": "main", "remote": "origin", "new": new})

    def test_forge_protection_is_only_reported_when_known(self):
        class Forge:
            def _req(self, method, path, *args):
                if "/branches/" in path:
                    return {"protected": True}
                if path.endswith("/pipelines"):
                    return [{"status": "success"}]
                return {"default_branch": "main"}
        facts = gitlab.push_evidence(Forge(), "acme/shop", "feature/a", ["acme/shop"], evidence.Budget())
        self.assertEqual(facts, {"exists": True, "protected": True, "ci": "main: CI success"})
        class Unknown(Forge):
            def _req(self, *a):
                raise gitlab.GitLabError(403, "unavailable")
        self.assertEqual(gitlab.push_evidence(Unknown(), "acme/shop", "feature/a", ["acme/shop"], evidence.Budget()), {})
        class GH:
            def _req(self, method, path, *args):
                if "/branches/" in path:
                    return {"protected": False}
                if path.endswith("/actions/runs"):
                    return {"workflow_runs": []}
                return {"default_branch": "main"}
        self.assertEqual(github.push_evidence(GH(), "acme/shop", "feature/a", ["acme/shop"], evidence.Budget()), {"exists": True, "protected": False})
        class Hidden(GH):
            def _req(self, *a):
                raise github.GitHubError(404, "unavailable")
        self.assertEqual(github.push_evidence(Hidden(), "acme/shop", "feature/a", ["acme/shop"], evidence.Budget()), {})
        class New(GH):
            def _req(self, method, path, *args):
                if "/branches/" in path:
                    raise github.GitHubError(404, "unavailable")
                return super()._req(method, path, *args)
        self.assertEqual(github.push_evidence(New(), "acme/shop", "feature/a", ["acme/shop"], evidence.Budget()), {"exists": False})

    def test_timeout_checking_completion_and_error_never_copy_exception(self):
        release = threading.Event()
        job = evidence.Job(lambda: (release.wait(1), evidence.box(rows=[{"text": "Finished"}]))[1])
        self.assertEqual(job.initial(0)["state"], "checking")
        release.set()
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.initial()["state"], "ready")
        def fail():
            raise RuntimeError("unsafe driver message")
        job = evidence.Job(fail)
        self.assertEqual(job.initial()["state"], "error")
        self.assertNotIn("unsafe driver message", json.dumps(job.value))
        with self.assertRaises(TimeoutError):
            evidence.Budget(0).left()
        job = evidence.Job(lambda: evidence.Budget(0).left())
        self.assertEqual(job.initial()["state"], "unavailable")
        self.assertIn("Could not check: timed out", json.dumps(job.value))

    def test_prod_plan_has_no_values_and_personal_column_names(self):
        m = sessions.Manager.__new__(sessions.Manager)
        worker = mock.Mock()
        worker.call.return_value = {"columns": [{"name": "QUERY PLAN"}], "rows": [
            ["Index Scan using customer_email on customer (cost=0..20 rows=120 width=10)"],
            ["Filter: email = 'private@example.com'"]]}
        m._db_evidence_worker = mock.Mock(return_value=worker)
        c = {"name": "acme/shop/prod", "cls": "prod", "engine": "postgres"}
        facts = m._db_read_evidence(None, c, "SELECT email, plan FROM customer", None, None, 50)
        self.assertEqual(facts["title"], "What it reads")
        self.assertIn("Index Scan · estimated rows 120", [r["text"] for r in facts["rows"]])
        self.assertTrue(any(r["text"] == "email" and r["chips"] == ["PERSONAL"] for r in facts["rows"]))
        self.assertTrue("private@example.com" not in json.dumps(facts), "plan literals escaped")
        self.assertTrue(all(call.args[0] == "explain" for call in worker.call.call_args_list))
        self.assertIn("at most 50 rows", facts["notes"][0])
        self.assertTrue(all(evidence.personal(n) for n in ("email", "phone", "customer_name", "address", "birth_date", "iban", "ip")))

    def test_secret_columns_are_whole_names_not_words_in_comments(self):
        for sql in ("DELETE FROM passengers WHERE id = 5", "UPDATE users SET role='admin' WHERE id=9 -- bypass",
                    "UPDATE users SET role='admin' /* auth */ WHERE id=9", "UPDATE book SET author_id=3 WHERE id=1"):
            self.assertEqual(dbtools._card_statement({"sql": sql})[0], sql, "values hidden by a non-secret name")
        for sql in ("UPDATE account SET password='x' WHERE id=1", "UPDATE t SET authToken='x' WHERE id=1",
                    "INSERT INTO t (api_key) VALUES ('x')", "UPDATE t SET client_secret='x' WHERE id=1"):
            self.assertNotIn("'x'", dbtools._card_statement({"sql": sql})[0])

    def test_http_similar_scope_keeps_the_content_digest_and_prod_writes_get_none(self):
        sha = "ab" * 32
        scope = evidence.matching_scope("http_test", "acme/shop", {"env": "test", "request_scope": "POST api.example.com /v1",
                                                                    "content_sha": sha})
        self.assertIsNotNone(scope)
        self.assertIn(sha, scope["scope"])
        self.assertIsNone(evidence.matching_scope("http_test", "acme/shop", {"env": "test", "content_sha": "not-a-digest",
                                                                             "request_scope": "POST api.example.com /v1"}))
        self.assertIsNone(evidence.matching_scope("db_write_prod", "acme/shop", {"connection": "acme/shop/prod",
                          "statement_kinds": ["dml"], "table_scope": [("", "orders")]}))

    def test_closed_tunnel_says_so_instead_of_a_timeout(self):
        m = sessions.Manager.__new__(sessions.Manager)
        m._db_lock = threading.Lock()
        m._db_workers = {}
        c = {"name": "acme/shop/prod", "cls": "prod", "engine": "mysql", "reach": "ssh"}
        with self.assertRaises(dbtools.EvidenceClosed):
            m._db_evidence_worker(c, "mcp", None, evidence.Budget())
        facts = m._db_read_evidence(None, c, "SELECT id FROM customer", None, None, 50)
        texts = [r["text"] for r in facts["rows"]]
        self.assertIn(dbtools.CLOSED_FACT, texts)
        self.assertFalse(any("timed out" in t for t in texts))

    def test_secret_sql_literals_and_bound_values_never_reach_cards_or_db_audit(self):
        sentinel = "synthetic-" + "bound-secret"
        stmts = [{"sql": "UPDATE account SET password=%s WHERE id=1", "params": [sentinel], "kind": "dml"},
                 {"sql": "INSERT INTO account (api_token) VALUES ('%s')" % sentinel, "kind": "dml"},
                 {"sql": "UPDATE account SET password=$s$%s$s$ WHERE id=1" % sentinel, "kind": "dml"}]
        self.assertTrue(sentinel not in dbtools._card_sql(stmts), "SQL card exposed a secret")
        m = sessions.Manager.__new__(sessions.Manager)
        c = {"name": "acme/shop/test", "cls": "test", "engine": "postgres"}
        self.assertTrue(sentinel not in "\n".join(m._db_card(c, stmts, None, ["No preview"], "", [])), "text card exposed a secret")
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as root, \
                mock.patch.object(dbtools, "AUDIT_FILE", os.path.join(root, "audit.jsonl")), mock.patch.object(dbtools, "audit"):
            for st in stmts:
                m._db_audit(dict(st, error="Duplicate value '%s'" % sentinel))
            with open(dbtools.AUDIT_FILE) as f:
                self.assertTrue(sentinel not in f.read(), "DB audit exposed a secret")

    def test_new_branch_hour_scope_requires_forge_verification_and_excludes_main(self):
        def scope(**kw):
            return evidence.matching_scope("git_push", "acme/shop", dict(remote="origin", push_new=True, **kw))["scope"]
        self.assertNotEqual(scope(branch="feature/a"), scope(branch="feature/b"))
        self.assertEqual(scope(branch="feature/a", push_verified=True), scope(branch="feature/b", push_verified=True))
        self.assertNotEqual(scope(branch="main", push_verified=True), scope(branch="feature/a", push_verified=True))
        self.assertIsNone(evidence.matching_scope("git_push", "acme/shop", dict(remote="origin", push_new=True,
                         branch="API_TOKEN=synthetic-value")))
        self.assertIsNotNone(evidence.matching_scope("db_write_test", "acme/shop",
                             {"connection": "acme/shop/test", "statement_kinds": ["dml"], "table_scope": [("", "item")]}))

    def test_db_gates_do_not_touch_a_worker_for_prod_ddl_or_off(self):
        m = sessions.Manager.__new__(sessions.Manager)
        m.policy = {}
        c = {"cls": "test", "engine": "mysql"}
        worker = mock.Mock()
        dml = [{"sql": "UPDATE item SET count=1 WHERE id=1", "kind": "dml"}]
        for conn, stmts, policy in ((dict(c, cls="prod"), dml, {}),
                                   (c, [{"sql": "ALTER TABLE item ADD count INT", "kind": "ddl"}], {}),
                                   (c, dml, {"db_dry_run": False})):
            m.policy = policy
            self.assertTrue(m._db_card_no_dry_run(conn, stmts))
        m.policy = {}
        self.assertEqual(m._db_card_no_dry_run(c, dml), [])
        worker.call.assert_not_called()
        self.assertNotIn("db_dry_run", security.IDS)
        self.assertIs(security.apply({"security": {}, "db_dry_run": False})["db_dry_run"], False)

    def test_postgres_unsafe_catalog_table_is_never_previewed(self):
        m = sessions.Manager.__new__(sessions.Manager)
        m.policy = {}
        worker = mock.Mock()
        def meta(op, timeout, **kw):
            return {"rows": {"engines": [["public", "item", "transactional"]],
                             "triggers": [], "preview_unsafe": [["item"]]}[kw["what"]]}
        worker.call.side_effect = meta
        self.assertFalse(m._db_gate(worker, {"cls": "test", "engine": "postgres"},
                                     [{"sql": "UPDATE item SET count=1 WHERE id=1", "kind": "dml"}], None, card=True)[0])
        self.assertTrue(any(c.kwargs.get("what") == "preview_unsafe" for c in worker.call.call_args_list))
        # The human's own GUI preview keeps the old rule: every PostgreSQL table rolls back, triggers too.
        gui = mock.Mock()
        gui.call.return_value = {"rows": []}
        self.assertTrue(m._db_gate(gui, {"cls": "prod", "engine": "postgres"},
                                   [{"sql": "DELETE FROM item WHERE id=1", "kind": "dml"}], None)[0])
        self.assertFalse(any(c.kwargs.get("what") in ("triggers", "preview_unsafe") for c in gui.call.call_args_list))

    def test_db_preview_scrubs_secret_cells_and_limits_rows(self):
        sentinel = "synthetic-" + "secret-cell"
        sample = {"columns": ["id", "api_token"], "rows": [[{"v": i}, {"v": sentinel, "to": sentinel + "x"}] for i in range(5)], "total": 9}
        facts = evidence.db_write({"name": "acme/shop/test", "cls": "test", "engine": "mysql"},
                                  {"preview_ok": True, "preview": "9 rows change", "sample": sample, "table_total": 20, "affected": 9})
        self.assertTrue(sentinel not in json.dumps(facts), "a secret cell escaped")
        self.assertEqual(len(facts["preview"]["rows"]), 3)
        self.assertEqual(facts["preview"]["more"], 6)
        self.assertIn("9 of 20 rows change", json.dumps(facts))
        self.assertIn("Counted before you answered; may differ.", facts["notes"])
        prod = evidence.db_write({"name": "acme/shop/prod", "cls": "prod"}, {"preview_ok": True, "sample": sample})
        self.assertNotIn("preview", prod)
        self.assertNotIn("preview", evidence.db_write({"name": "acme/shop/test", "cls": "test"},
                                                     {"preview_ok": True, "sample": sample}, ddl=True))

    def test_other_project_evidence_uses_host_branch_sessions_and_inline_diff(self):
        m = sessions.Manager.__new__(sessions.Manager)
        m._same_tree = mock.Mock(return_value=[object(), object()])
        with mock.patch.object(sessions.projects, "find", return_value={"key": "acme/design", "path": "/workspace/acme/design", "exists": True}), \
                mock.patch.object(sessions.gitops, "_git", return_value="main\n"):
            facts = m._card_evidence("edit_file", None, {"project": "acme/design"}, "Change\n--- a/file\n+++ b/file\n-old\n+new")
        self.assertIn("2 sessions use it", facts["rows"][0]["text"])
        self.assertIn("branch main", facts["rows"][0]["text"])
        self.assertTrue(facts["inline_diff"])
        self.assertIn("+new", facts["diff"])

    def test_suggestion_threshold_time_and_matching_scope(self):
        now = time.time()
        rows = [{"event": "confirm", "approved": True, "answered": True, "matching_scope": "a", "decided_at": now}] * 4
        self.assertIsNone(evidence.suggestion(rows, "a", now))
        self.assertIsNotNone(evidence.suggestion(rows + rows[:1], "a", now))
        self.assertIsNone(evidence.suggestion(rows * 2, "b", now))
        self.assertIsNone(evidence.suggestion(rows * 2, "a", now + evidence.WEEK + 1))
        self.assertIsNone(evidence.suggestion([dict(r, auto=True) for r in rows * 2], "a", now))


class ApprovalEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.patch = mock.patch.multiple(sessions, APPROVAL_GRANTS_FILE=os.path.join(self.tmp.name, "grants.json"),
                                        APPROVAL_HISTORY_FILE=os.path.join(self.tmp.name, "history.json"))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.m = sessions.Manager.__new__(sessions.Manager)
        self.m.lock, self.m.pending, self.m.sessions = threading.RLock(), {}, {}
        self.m.policy = {"ask": ["git_push", "edit_file"]}
        self.m.bump = lambda: None
        self.m._card_evidence = lambda kind, s, payload, summary: evidence.box(rows=[{"text": "Collected on host"}],
            push={"remote": payload.get("remote", "origin"), "branch": payload.get("branch", "feature/a"),
                  "new": payload.get("push_new", True), "verified": True})
        self.s = sessions.Session(kind="project", project="acme/shop", title="Shop", state="ready")
        self.m.sessions[self.s.id] = self.s
        self.payload = {"branch": "feature/a", "remote": "origin", "push_new": True}

    def answer(self, payload=None, **decision):
        result = []
        t = threading.Thread(target=lambda: result.append(self.m.ask("git_push", self.s, "Push", payload or self.payload, timeout=3)))
        t.start()
        self.addCleanup(t.join, 4)
        deadline = time.monotonic() + 3
        while not self.m.pending_list() and time.monotonic() < deadline:
            time.sleep(.005)
        card = self.m.pending_list()[0]
        self.m.decide(card["id"], True, **decision)
        t.join(4)
        self.assertEqual(result, [True])
        return card

    def test_hour_grant_expires_scopes_project_and_can_be_revoked(self):
        self.answer(similar_hour=True)
        grants = self.m.approval_grants(self.s)
        self.assertEqual(len(grants), 1)
        self.assertLessEqual(abs(grants[0]["expires_at"] - time.time() - 3600), 2)
        self.assertTrue(self.m.ask("git_push", self.s, "Another new branch", dict(self.payload, branch="feature/b"), timeout=0))
        self.assertFalse(self.m.ask("git_push", self.s, "Existing main", dict(self.payload, branch="main", push_new=False), timeout=0))
        other = sessions.Session(kind="project", project="acme/other", state="ready")
        self.m.sessions[other.id] = other
        self.assertFalse(self.m.ask("git_push", other, "Other project", self.payload, timeout=0))
        with mock.patch.object(sessions.time, "time", return_value=grants[0]["expires_at"] + 1):
            self.assertFalse(self.m.ask("git_push", self.s, "Expired", self.payload, timeout=0))
            self.assertEqual(self.m.approval_grants(self.s), [])
        self.m.revoke_approval(self.s.id, grants[0]["scope"])
        self.assertEqual(self.m.approval_grants(self.s), [])

    def test_hour_grant_never_answers_a_forced_card_or_a_scoped_profile(self):
        self.answer(similar_hour=True)
        self.assertTrue(self.m.ask("git_push", self.s, "Granted", self.payload, timeout=0))
        self.assertFalse(self.m.ask("git_push", self.s, "Forced", dict(self.payload, _force_approval=True), timeout=0))
        self.m.policy = dict(self.m.policy, security_scoped=True)
        self.assertFalse(self.m.ask("git_push", self.s, "Scoped", self.payload, timeout=0))

    def test_a_forced_card_offers_no_hour_grant(self):
        card = self.answer(dict(self.payload, _force_approval=True))
        self.assertNotIn("similar", card)
        self.assertEqual(self.m.approval_grants(self.s), [])

    def test_evidence_is_next_to_decision_in_audit_and_suggestion_is_read_only(self):
        for _ in range(5):
            self.answer()
        card = self.answer()
        self.assertIn("5 pushes to new branches in acme/shop", card["suggestion"]["text"])
        self.assertEqual(self.m.approval_grants(self.s), [])
        events = [r for r in common.audit_tail(50, self.s.id) if r.get("event") == "confirm"]
        self.assertTrue(all(r["evidence"]["rows"][0]["text"] == "Collected on host" for r in events))
        self.assertEqual(self.m.policy["ask"], ["git_push", "edit_file"])

    def test_payload_and_audit_never_include_secret_values(self):
        sentinel = "synthetic-" + "approval-secret"
        self.m._card_evidence = lambda *a: evidence.box(rows=[{"text": "API_TOKEN=" + sentinel}])
        card = self.answer(dict(self.payload, credentials={"password": sentinel}))
        self.assertTrue(sentinel not in json.dumps(card), "secret value escaped to a card")
        self.assertTrue(sentinel not in json.dumps(common.audit_tail(20, self.s.id)), "secret value escaped to audit")

    def test_background_update_uses_same_card_and_never_changes_decision(self):
        release = threading.Event()
        job = evidence.Job(lambda: (release.wait(3), evidence.box(rows=[{"text": "Finished"}]))[1])
        job.started -= evidence.SECONDS  # the collection already spent its display budget
        payload = dict(self.payload, _evidence_job=job)
        answer = []
        t = threading.Thread(target=lambda: answer.append(self.m.ask("git_push", self.s, "Push", payload, timeout=3)))
        t.start()
        self.addCleanup(t.join, 4)
        deadline = time.monotonic() + 2
        while not self.m.pending_list() and time.monotonic() < deadline:
            time.sleep(.005)
        card = self.m.pending_list()[0]
        self.assertEqual(card["payload"]["evidence"]["state"], "checking")
        release.set()
        deadline = time.monotonic() + 2
        while self.m.pending_list()[0]["payload"]["evidence"]["state"] == "checking" and time.monotonic() < deadline:
            time.sleep(.005)
        updated = self.m.pending_list()[0]
        self.assertEqual(updated["id"], card["id"])
        self.assertEqual(updated["payload"]["evidence"]["rows"][0]["text"], "Finished")
        self.m.decide(card["id"], False)
        t.join(4)
        self.assertEqual(answer, [False])


if __name__ == "__main__":
    unittest.main()
