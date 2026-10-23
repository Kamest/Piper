"""W23 GitHub proxy: per-session repo scope, carded writes, withheld policy files, token handling."""
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import _env  # noqa: F401
import github
import guard


class Resp(io.BytesIO):
    def __init__(self, raw: bytes, headers: dict | None = None):
        super().__init__(raw)
        self.headers = headers or {"Content-Type": "application/json"}


class FakeOpener:
    """routes: (method, path) -> JSON body | Exception | (raw bytes, headers) | callable(req) -> any of those."""

    def __init__(self, routes):
        self.routes, self.sent = routes, []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        self.sent.append({"m": req.get_method(), "host": u.hostname, "path": u.path,
                          "query": dict(urllib.parse.parse_qsl(u.query)),
                          "auth": req.headers.get("Authorization"),
                          "ims": req.headers.get("If-modified-since"),
                          "body": json.loads(req.data) if req.data else None})
        body = self.routes.get((req.get_method(), u.path))
        if callable(body):
            body = body(req)
        if body is None:
            raise AssertionError("unexpected call %s %s" % (req.get_method(), u.path))
        if isinstance(body, Exception):
            raise body
        if isinstance(body, tuple):
            return Resp(body[0], body[1])
        return Resp(json.dumps(body).encode())


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", headers or {}, None)


POLICY = guard.load_policy("/nonexistent")


def hidden(p):
    return guard.hidden_by(POLICY["candidates"], p)


def gh(routes, host="github.com", tok="tok-1"):
    op = FakeOpener(routes)
    return github.GitHub(host, tok, opener=op), op


ISSUE = {"number": 7, "title": "Bug", "state": "open", "body": "desc", "user": {"login": "anna"},
         "assignees": [{"login": "me"}], "labels": [{"name": "bug"}], "html_url": "https://github.com/Kamest/app/issues/7"}
PR = {"number": 9, "title": "Fix", "state": "open", "body": "", "user": {"login": "me"}, "draft": True,
      "head": {"ref": "fix-1"}, "base": {"ref": "main"}, "requested_reviewers": [{"login": "bob"}],
      "html_url": "https://github.com/Kamest/app/pull/9"}


class ScopeTest(unittest.TestCase):
    def test_split_names_github_com_or_a_ghe_host(self):
        self.assertEqual(github.split("Kamest/app"), ("github.com", "Kamest/app"))
        self.assertEqual(github.split("ghe.example.com/team/app"), ("ghe.example.com", "team/app"))

    def test_api_base(self):
        self.assertEqual(github.api_base("github.com"), "https://api.github.com")
        self.assertEqual(github.api_base("ghe.example.com"), "https://ghe.example.com/api/v3")

    def test_target_is_case_insensitive_and_confined(self):
        self.assertEqual(github._target({"repo": "kamest/APP"}, ["Kamest/app"]), "Kamest/app")
        self.assertEqual(github._target({}, ["Kamest/app"]), "Kamest/app")   # one repo: the default
        with self.assertRaises(github.GitHubError) as e:
            github._target({"repo": "other/app"}, ["Kamest/app"])
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(github.GitHubError) as e:
            github._target({}, ["Kamest/app", "Kamest/lib"])
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(github.GitHubError):
            github._target({"repo": "x"}, [])

    def test_same_name_on_two_hosts_needs_the_host(self):
        allowed = ["Kamest/app", "ghe.example.com/Kamest/app"]
        with self.assertRaises(github.GitHubError) as e:
            github._target({"repo": "Kamest/app"}, allowed)
        self.assertIn("ambiguous", str(e.exception))
        self.assertEqual(github._target({"repo": "ghe.example.com/kamest/app"}, allowed), "ghe.example.com/Kamest/app")

    def test_path_tricks_are_refused(self):
        for bad in ("Kamest/../x", "Kamest/app/../../y", "Kamest/app?x=1", "../app"):
            with self.assertRaises(github.GitHubError, msg=bad):
                github._target({"repo": bad}, ["Kamest/app"])


class ReadTest(unittest.TestCase):
    def test_issue_with_comments_in_gitlab_words(self):
        g, op = gh({("GET", "/repos/Kamest/app/issues/7"): ISSUE,
                    ("GET", "/repos/Kamest/app/issues/7/comments"): [{"id": 1, "body": "hi", "user": {"login": "bob"}}]})
        r = github.call(g, "github_issue", {"number": 7}, ["Kamest/app"], hidden)
        self.assertEqual((r["iid"], r["state"], r["description"], r["author"], r["labels"]), (7, "opened", "desc", "anna", ["bug"]))
        self.assertEqual(r["comments"][0]["author"], "bob")
        self.assertEqual(op.sent[0]["auth"], "Bearer tok-1")

    def test_issue_tool_refuses_a_pull_request(self):
        g, _ = gh({("GET", "/repos/Kamest/app/issues/9"): dict(ISSUE, number=9, pull_request={"url": "x"})})
        with self.assertRaises(github.GitHubError):
            github.call(g, "github_issue", {"number": 9}, ["Kamest/app"], hidden)

    def test_pr_shape_has_branches_draft_reviewers(self):
        g, _ = gh({("GET", "/repos/Kamest/app/pulls/9"): PR,
                   ("GET", "/repos/Kamest/app/issues/9/comments"): [],
                   ("GET", "/repos/Kamest/app/pulls/9/comments"): [{"id": 3, "body": "nit", "path": "a.py", "line": 4, "user": {"login": "bob"}}],
                   ("GET", "/repos/Kamest/app/pulls/9/reviews"): [{"id": 5, "state": "COMMENTED", "body": ""},
                                                                  {"id": 6, "state": "APPROVED", "body": "", "user": {"login": "bob"}}]})
        r = github.call(g, "github_pr", {"number": 9}, ["Kamest/app"], hidden)
        self.assertEqual((r["source_branch"], r["target_branch"], r["draft"], r["reviewers"]), ("fix-1", "main", True, ["bob"]))
        self.assertEqual(r["review_comments"][0]["path"], "a.py")
        self.assertEqual([x["id"] for x in r["reviews"]], [6])   # an empty COMMENTED review is noise

    def test_merged_pr_says_merged(self):
        self.assertEqual(github._shape(dict(PR, state="closed", merged_at="2026-10-01T00:00:00Z"))["state"], "merged")

    def test_pr_files_withhold_policy_files_on_either_side_of_a_rename(self):
        g, _ = gh({("GET", "/repos/Kamest/app/pulls/9/files"): [
            {"filename": "src/a.py", "status": "modified", "patch": "@@ x"},
            {"filename": "config/secrets/db.properties", "status": "modified", "patch": "SECRET=1"},
            {"filename": "notes.txt", "previous_filename": "certs/server.key", "status": "renamed", "patch": "S=2"},
            {"filename": "img.png", "status": "added"}]})
        files = github.call(g, "github_pr_files", {"number": 9}, ["Kamest/app"], hidden)["files"]
        self.assertEqual(files[0]["diff"], "@@ x")
        self.assertIn("withheld", files[1]["diff"])
        self.assertIn("withheld", files[2]["diff"])
        self.assertIn("no text diff", files[3]["diff"])
        self.assertNotIn("SECRET", json.dumps(files))
        self.assertNotIn("S=2", json.dumps(files))

    def test_out_of_scope_repo_never_reaches_github(self):
        g, op = gh({})
        with self.assertRaises(github.GitHubError):
            github.call(g, "github_issue", {"repo": "evil/repo", "number": 1}, ["Kamest/app"], hidden)
        self.assertEqual(op.sent, [])

    def test_runs_speak_pipeline_words(self):
        g, _ = gh({("GET", "/repos/Kamest/app/actions/runs"): {"workflow_runs": [
            {"id": 1, "status": "completed", "conclusion": "failure"}, {"id": 2, "status": "in_progress"}]}})
        r = github.call(g, "github_runs", {"branch": "main"}, ["Kamest/app"], hidden)
        self.assertEqual([x["pipeline_status"] for x in r["runs"]], ["failed", "running"])

    def test_ghe_host_uses_api_v3(self):
        g, op = gh({("GET", "/api/v3/repos/team/app/issues/7"): ISSUE,
                    ("GET", "/api/v3/repos/team/app/issues/7/comments"): []}, host="ghe.example.com")
        github.call(g, "github_issue", {"number": 7}, ["ghe.example.com/team/app"], hidden)
        self.assertEqual(op.sent[0]["host"], "ghe.example.com")


class CardTest(unittest.TestCase):
    def test_comment_plan_shows_the_body_and_executes_one_post(self):
        g, op = gh({("POST", "/repos/Kamest/app/issues/7/comments"): {"id": 11, "html_url": "https://github.com/c/11"}})
        p = github.plan(g, "github_comment", {"number": 7, "body": "Done."}, ["Kamest/app"])
        self.assertEqual(op.sent, [])   # the plan itself writes nothing
        self.assertIn("Kamest/app issue #7", p["head"])
        self.assertEqual(p["long"]["body"], "Done.")
        r = github.execute(g, p)
        self.assertEqual((r["comment_id"], op.sent[0]["body"]), (11, {"body": "Done."}))

    def test_comment_edit_must_belong_to_the_number(self):
        g, _ = gh({("GET", "/repos/Kamest/app/issues/comments/5"): {"id": 5, "body": "old",
                                                                   "issue_url": "https://api.github.com/repos/Kamest/app/issues/8"}})
        with self.assertRaises(github.GitHubError):
            github.plan(g, "github_comment", {"number": 7, "comment_id": 5, "body": "x"}, ["Kamest/app"])

    def test_pr_create_defaults_the_base_to_the_default_branch(self):
        g, _ = gh({("GET", "/repos/Kamest/app"): {"default_branch": "main"}})
        p = github.plan(g, "github_pr_create", {"title": "T", "source_branch": "7-fix", "draft": True}, ["Kamest/app"])
        self.assertEqual(p["steps"][0]["body"], {"title": "T", "head": "7-fix", "base": "main", "draft": True})

    def test_pr_update_cannot_flip_draft(self):
        g, _ = gh({})
        with self.assertRaises(github.GitHubError):
            github.plan(g, "github_pr_update", {"number": 9, "draft": False}, ["Kamest/app"])

    def test_job_log_only_of_a_failed_job(self):
        g, _ = gh({("GET", "/repos/Kamest/app/actions/jobs/4"): {"id": 4, "conclusion": "success"}})
        with self.assertRaises(github.GitHubError) as e:
            github.plan(g, "github_job_log", {"job_id": 4}, ["Kamest/app"])
        self.assertEqual(e.exception.status, 403)

    def test_partial_failure_says_what_happened(self):
        g, _ = gh({("PATCH", "/repos/Kamest/app/issues/7"): ISSUE,
                   ("POST", "/repos/Kamest/app/issues/7/labels"): http_error(422)})
        p = github.plan(g, "github_issue_update", {"number": 7, "title": "New", "add_labels": "x"}, ["Kamest/app"])
        r = github.execute(g, p)
        self.assertTrue(r["partial"])
        self.assertEqual(r["done"], ["change the issue"])


class TransportTest(unittest.TestCase):
    def test_no_token_says_how_to_log_in(self):
        g, op = gh({}, tok=None)
        with self.assertRaises(github.GitHubError) as e:
            github.call(g, "github_issue", {"number": 7}, ["Kamest/app"], hidden)
        self.assertEqual(e.exception.status, 503)
        self.assertIn("gh auth login", str(e.exception))
        self.assertEqual(op.sent, [])

    def test_rate_limit_and_401_forgets_the_token(self):
        g, _ = gh({("GET", "/user"): http_error(403, {"X-RateLimit-Remaining": "0"})})
        with self.assertRaises(github.GitHubError) as e:
            g._req("GET", "/user")
        self.assertEqual(e.exception.status, 429)
        github._TOKENS["github.com"] = (1e18, "stale")
        g, _ = gh({("GET", "/user"): http_error(401)})
        with self.assertRaises(github.GitHubError):
            g._req("GET", "/user")
        self.assertNotIn("github.com", github._TOKENS)

    def test_job_log_redirect_followed_only_to_github_storage_without_the_token(self):
        loc = "https://pipelines.actions.githubusercontent.com/logs/4?sig=x"
        routes = {("GET", "/repos/Kamest/app/actions/jobs/4/logs"): http_error(302, {"Location": loc}),
                  ("GET", "/logs/4"): (b"2026-10-01T10:00:00.000Z step failed\n", {"Content-Type": "text/plain"})}
        g, op = gh(routes)
        self.assertIn(b"step failed", g.log("/repos/Kamest/app/actions/jobs/4/logs", 1000))
        self.assertEqual(op.sent[1]["host"], "pipelines.actions.githubusercontent.com")
        self.assertIsNone(op.sent[1]["auth"])
        routes[("GET", "/repos/Kamest/app/actions/jobs/4/logs")] = http_error(302, {"Location": "https://evil.example/logs/4"})
        g, op = gh(routes)
        with self.assertRaises(github.GitHubError):
            g.log("/repos/Kamest/app/actions/jobs/4/logs", 1000)
        self.assertEqual(len(op.sent), 1)

    def test_notifications_304_means_unchanged(self):
        g, op = gh({("GET", "/notifications"): http_error(304)})
        self.assertEqual(github.notifications(g, "Mon, 12 Oct 2026 10:00:00 GMT"), (None, "Mon, 12 Oct 2026 10:00:00 GMT"))
        self.assertEqual(op.sent[0]["ims"], "Mon, 12 Oct 2026 10:00:00 GMT")
        g, _ = gh({("GET", "/notifications"): (b"[]", {"Last-Modified": "Tue"})})
        self.assertEqual(github.notifications(g), ([], "Tue"))

    def test_token_comes_from_gh_and_is_cached(self):
        github._TOKENS.clear()
        run = mock.Mock(return_value=mock.Mock(returncode=0, stdout="gho_x\n"))
        with mock.patch.object(github, "gh_bin", return_value="/usr/bin/gh"), mock.patch.object(github.subprocess, "run", run):
            self.assertEqual(github.token("github.com"), "gho_x")
            self.assertEqual(github.token("github.com"), "gho_x")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][1:], ["auth", "token", "--hostname", "github.com"])
        github._TOKENS.clear()
        with mock.patch.object(github, "gh_bin", return_value=None):
            self.assertIsNone(github.token("github.com"))
        github._TOKENS.clear()


class GuiTest(unittest.TestCase):
    def test_latest_pipeline_folds_one_commits_runs(self):
        runs = [{"id": 3, "status": "completed", "conclusion": "success", "head_sha": "aaa", "head_branch": "main", "updated_at": "2"},
                {"id": 2, "status": "completed", "conclusion": "failure", "head_sha": "aaa", "head_branch": "main", "updated_at": "1"},
                {"id": 1, "status": "completed", "conclusion": "failure", "head_sha": "old", "head_branch": "main"}]
        g, _ = gh({("GET", "/repos/Kamest/app/actions/runs"): {"workflow_runs": runs}})
        p = github.latest_pipeline(g, "Kamest/app", "main", ["Kamest/app"])
        self.assertEqual((p["id"], p["status"], p["sha"]), (3, "failed", "aaa"))
        self.assertIn("/actions?query=branch", p["web_url"])   # two runs: the branch's list

    def test_run_status_words(self):
        self.assertEqual(github.run_status({"status": "queued"}), "pending")
        self.assertEqual(github.run_status({"status": "completed", "conclusion": "cancelled"}), "canceled")
        self.assertEqual(github.run_status({"status": "completed", "conclusion": "action_required"}), "manual")

    def test_create_mr_is_scoped_and_sends_draft(self):
        g, op = gh({("POST", "/repos/Kamest/app/pulls"): PR})
        r = github.create_mr(g, "kamest/app", ["Kamest/app"], "fix-1", "main", "Fix", "d", draft=True)
        self.assertEqual((r["iid"], r["source_branch"]), (9, "fix-1"))
        self.assertTrue(op.sent[0]["body"]["draft"])
        with self.assertRaises(github.GitHubError):
            github.create_mr(g, "evil/x", ["Kamest/app"], "a", "b", "t", "")

    def test_open_mr_asks_for_the_head_in_the_same_repo(self):
        g, op = gh({("GET", "/repos/Kamest/app/pulls"): [PR]})
        self.assertEqual(github.open_mr(g, "Kamest/app", "fix-1", ["Kamest/app"])["iid"], 9)
        self.assertEqual(op.sent[0]["query"]["head"], "Kamest:fix-1")

    def test_repo_of(self):
        self.assertEqual(github.repo_of({"repository": {"full_name": "Kamest/app"}}), "Kamest/app")
        self.assertEqual(github.repo_of({"repository_url": "https://api.github.com/repos/Kamest/app"}), "Kamest/app")
        self.assertIsNone(github.repo_of({}))

    def test_post_note_refuses_outside_scope_and_empty(self):
        g, _ = gh({})
        with self.assertRaises(github.GitHubError):
            github.post_note(g, "evil/x", "issue", 1, "hi", ["Kamest/app"])
        with self.assertRaises(github.GitHubError):
            github.post_note(g, "Kamest/app", "issue", 1, "  ", ["Kamest/app"])

    def test_item_preview_is_inbox_shaped(self):
        g, _ = gh({("GET", "/repos/Kamest/app/pulls/9"): PR,
                   ("GET", "/repos/Kamest/app/issues/9/comments"): [{"id": 1, "body": "x", "user": {"login": "bob"}}],
                   ("GET", "/repos/Kamest/app/actions/runs"): {"workflow_runs": []}})
        r = github.item_preview(g, "Kamest/app", "mr", 9, ["Kamest/app"])
        self.assertEqual((r["type"], r["forge"], r["mr"]["draft"], r["notes"][0]["user"]), ("mr", "github", True, "bob"))


if __name__ == "__main__":
    unittest.main()
