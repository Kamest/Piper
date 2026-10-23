"""GitLab proxy: fixed operations only, per-session project scope, withheld policy files, scrub."""
import io
import json
import unittest
import urllib.error
import urllib.parse

import _env  # noqa: F401
import gitlab
import guard


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.calls, self.sent = routes, [], []

    def __call__(self, req, timeout=None):
        u = urllib.parse.urlsplit(req.full_url)
        self.calls.append((req.get_method(), u.path, req.headers.get("Private-token")))
        self.sent.append((req.get_method(), u.path, dict(urllib.parse.parse_qsl(u.query)),
                          ({"_multipart": req.data, "_content_type": req.headers.get("Content-type")}
                           if req.headers.get("Content-type", "").startswith("multipart/form-data")
                           else json.loads(req.data) if req.data else None)))
        body = self.routes.get((req.get_method(), u.path))
        if body is None:
            raise AssertionError("unexpected call %s %s" % (req.get_method(), u.path))
        if isinstance(body, Exception):
            raise body
        if u.path.endswith("/discussions") and int(dict(urllib.parse.parse_qsl(u.query)).get("page", 1)) > 1:
            body = []   # one page of threads: GitLab answers past the end with []
        if isinstance(body, tuple):   # (raw bytes, content type)
            r = io.BytesIO(body[0])
            r.headers = {"Content-Type": body[1]}
            return r
        return io.BytesIO(json.dumps(body).encode())


BASE = "/api/v4/projects/grp%2Fapp"
POLICY = guard.load_policy("/nonexistent")


def hidden(p):
    return guard.hidden_by(POLICY["candidates"], p)


class GitLabTest(unittest.TestCase):
    def gl(self, routes):
        self.op = FakeOpener(routes)
        return gitlab.GitLab("gitlab.example", "tok", opener=self.op)

    def test_upload_file_plan_sends_exact_bytes_as_multipart_and_returns_markdown(self):
        from email.parser import BytesParser
        from email.policy import default
        raw = '# Report\nUrsachen und Lösungen.\n'.encode()
        link = '/uploads/' + 'a' * 32 + '/REPORT.md'
        gl = self.gl({('POST', BASE + '/uploads'): {'url': link, 'markdown': '[REPORT.md](' + link + ')'}})
        p = gitlab.plan(gl, 'gitlab_upload_file', {}, ['grp/app'], upload=('REPORT.md', raw))
        self.assertEqual(self.op.calls, [])
        self.assertIn('REPORT.md', gitlab.card_text(p))
        self.assertIn('Ursachen', gitlab.card_text(p))
        out = gitlab.execute(gl, p)
        self.assertEqual(out['markdown'], '[REPORT.md](' + link + ')')
        self.assertEqual(out['size'], len(raw))
        sent = self.op.sent[0][3]
        message = BytesParser(policy=default).parsebytes(('Content-Type: ' + sent['_content_type'] + '\r\n\r\n').encode() + sent['_multipart'])
        part = list(message.iter_parts())[0]
        self.assertEqual(part.get_param('name', header='content-disposition'), 'file')
        self.assertEqual(part.get_filename(), 'REPORT.md')
        self.assertEqual(part.get_payload(decode=True), raw)

    def test_upload_file_refuses_unvalidated_input_and_cross_project_scope_before_post(self):
        gl = self.gl({})
        for upload in (None, ('bad\r\nname.md', b'text'), ('REPORT.md', b'x' * (gitlab.UPLOAD_MAX + 1))):
            with self.subTest(upload=None if upload is None else upload[0]), self.assertRaises(gitlab.GitLabError):
                gitlab.plan(gl, 'gitlab_upload_file', {}, ['grp/app'], upload=upload)
        with self.assertRaises(gitlab.GitLabError):
            gitlab.plan(gl, 'gitlab_upload_file', {'project':'foreign/app'}, ['grp/app'], upload=('REPORT.md', b'text'))
        self.assertEqual(self.op.calls, [])

    def issue_routes(self, extra=None):
        return {**{("GET", BASE + "/issues/7"): {"iid": 7, "title": "T", "description": "db password=hunter2\n"
                                                    "![s](/uploads/" + "a" * 32 + "/shot.png)",
                                                    "author": {"username": "u", "name": "U"}, "secret_field": 1,
                                                    "time_stats": {"total_time_spent": 3600}},
                     ("GET", BASE + "/issues/7/discussions"): [
                         {"id": "d1" * 4, "notes": [{"id": 1, "body": "ok", "system": False},
                                                    {"id": 3, "body": "see /uploads/" + "b" * 32 + "/log.txt",
                                                     "system": False}]},
                         {"id": "d2" * 4, "notes": [{"id": 2, "body": "moved", "system": True}]}],
                     ("GET", BASE + "/issues/7/links"): [{"iid": 9, "title": "L", "state": "opened",
                                                          "link_type": "blocks", "references": {"full": "grp/x#9"}}],
                     ("POST", "/api/graphql"): {"data": {"project": {"workItems": {"nodes": [{"widgets": [
                         {}, {"parent": {"iid": "5", "title": "Epic", "workItemType": {"name": "Issue"}},
                              "children": {"nodes": [{"iid": "8", "title": "Sub", "state": "OPEN",
                                                      "workItemType": {"name": "Task"}}]}}]}]}}}}}, **(extra or {})}

    def test_the_gui_reads_an_issue_unscrubbed(self):
        """L1 (2026-10-07): the human's issue picker shows the text as GitLab does; a session's call scrubs."""
        gl = self.gl(self.issue_routes())
        out = gitlab.call(gl, "gitlab_issue", {"iid": 7}, ["grp/app"], hidden, scrub=False)
        self.assertTrue(out["description"].startswith("db password=hunter2"))
        gl = self.gl({("GET", BASE + "/issues"): [{"iid": 7, "title": "rotate token=abc123", "state": "opened"}]})
        self.assertEqual(gitlab.call(gl, "gitlab_issues", {"page": 2}, ["grp/app"], hidden, scrub=False)["issues"][0]["title"],
                         "rotate token=abc123")
        gl = self.gl({("GET", BASE + "/issues"): [{"iid": 7, "title": "rotate token=abc123", "state": "opened"}]})
        self.assertEqual(gitlab.call(gl, "gitlab_issues", {"page": 2}, ["grp/app"], hidden)["issues"][0]["title"],
                         "rotate token=[scrubbed]")

    def test_issue_with_notes_scrubbed(self):
        gl = self.gl(self.issue_routes())
        out = gitlab.call(gl, "gitlab_issue", {"iid": 7}, ["grp/app"], hidden)
        self.assertTrue(out["description"].startswith("db password=[scrubbed]"))
        self.assertEqual(out["author"], "U")
        self.assertNotIn("secret_field", out)
        self.assertEqual([n["id"] for n in out["notes"]], [1, 3])
        self.assertEqual(out["notes"][1]["discussion_id"], "d1" * 4)          # reply in that thread
        self.assertTrue(out["notes"][1]["reply"])
        self.assertEqual(out["uploads"], ["/uploads/%s/shot.png" % ("a" * 32), "/uploads/%s/log.txt" % ("b" * 32)])
        self.assertEqual(out["links"], [{"iid": 9, "title": "L", "state": "opened", "link_type": "blocks",
                                         "project": "grp/x"}])
        self.assertEqual(out["parent"]["iid"], "5")
        self.assertEqual(out["children"][0]["type"], "Task")
        self.assertEqual(out["time_stats"], {"total_time_spent": 3600})
        self.assertEqual(self.op.calls[0][2], "tok")

    def test_issue_without_work_items_still_reads(self):
        gl = self.gl(self.issue_routes({("POST", "/api/graphql"): {"errors": [{"message": "no field workItems"}]}}))
        out = gitlab.call(gl, "gitlab_issue", {"iid": 7}, ["grp/app"], hidden)
        self.assertNotIn("parent", out)
        self.assertEqual(out["iid"], 7)

    def test_mr_review_threads_carry_position_and_resolved(self):
        gl = self.gl({("GET", BASE + "/merge_requests/3"): {"iid": 3, "reviewers": [{"username": "r", "name": "R"}]},
                      ("GET", BASE + "/merge_requests/3/discussions"): [
                          {"id": "abcdef12", "notes": [{"id": 1, "body": "nit", "resolvable": True, "resolved": False,
                                                        "position": {"new_path": "src/A.java", "new_line": 12}}]}]})
        out = gitlab.call(gl, "gitlab_mr", {"iid": 3}, ["grp/app"], hidden)
        n = out["notes"][0]
        self.assertEqual(n["position"], {"path": "src/A.java", "new_line": 12, "old_line": None})
        self.assertFalse(n["resolved"])
        self.assertEqual(out["reviewers"], ["R"])

    def test_reply_and_resolve(self):
        gl = self.gl({("POST", BASE + "/merge_requests/3/discussions/abcdef12/notes"): {"id": 5},
                      ("GET", BASE + "/merge_requests/3/discussions/abcdef12"): {"id": "abcdef12", "notes": [{"resolvable": True}]},
                      ("GET", BASE + "/merge_requests/3/discussions/abcdef99"): {"id": "abcdef99", "notes": [{"resolvable": False}]},
                      ("PUT", BASE + "/merge_requests/3/discussions/abcdef12"): {}})
        out = gitlab.call(gl, "gitlab_comment", {"kind": "mr", "iid": 3, "body": "Fixed.", "discussion_id": "abcdef12"},
                          ["grp/app"], hidden)
        self.assertEqual(out["note_id"], 5)
        gitlab.call(gl, "gitlab_resolve", {"iid": 3, "discussion_id": "abcdef12"}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][2], {"resolved": "true"})
        gitlab.call(gl, "gitlab_resolve", {"iid": 3, "discussion_id": "abcdef12", "resolved": "false"}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][2], {"resolved": "false"})                # a string "false" is not True
        for v in ("false", " False ", False):   # the card says what is done
            p = gitlab.plan(gl, "gitlab_resolve", {"iid": 3, "discussion_id": "abcdef12", "resolved": v}, ["grp/app"])
            self.assertTrue(gitlab.card_text(p).startswith("Unresolve"), v)
            self.assertEqual(p["steps"][0]["query"], {"resolved": "false"})
        for bad in ("../x", "abc", "abcdef99"):   # abcdef99: a plain comment, not resolvable - refused before any card
            with self.assertRaises(gitlab.GitLabError):
                gitlab.plan(gl, "gitlab_resolve", {"iid": 3, "discussion_id": bad}, ["grp/app"])
        missing = self.gl({("GET", BASE + "/merge_requests/3/discussions/abcdef12"): urllib.error.HTTPError("u", 404, "x", {}, None)})
        with self.assertRaises(gitlab.GitLabError) as e:   # a thread that does not exist: no card
            gitlab.plan(missing, "gitlab_resolve", {"iid": 3, "discussion_id": "abcdef12"}, ["grp/app"])
        self.assertEqual(e.exception.status, 404)

    def test_people_names_carry_no_bidi_controls(self):
        """E25: an author's display name is their own text - it must not flip the inbox line"""
        self.assertEqual(gitlab._people({"username": "u", "name": "Eve \u202etxt.exe"}), "Eve txt.exe")
        self.assertEqual(gitlab._people([{"username": "\u2066bob\u2069"}]), ["bob"])
        self.assertEqual(gitlab._people("plain"), "plain")

    def test_issue_create_with_people_milestone_and_parent(self):
        gl = self.gl({("GET", "/api/v4/users"): [{"id": 41}],
                      ("GET", BASE + "/milestones"): [{"id": 7}],
                      ("POST", BASE + "/issues"): {"id": 900, "iid": 12, "title": "T", "web_url": "u", "issue_type": "task"},
                      ("GET", BASE + "/issues/4"): {"id": 800, "iid": 4},
                      ("POST", "/api/graphql"): {"data": {"workItemUpdate": {"errors": []}}}})
        out = gitlab.call(gl, "gitlab_issue_create", {"title": " T ", "description": "D", "type": "task",
                                                      "labels": ["a", "b"], "assignees": ["@alice"], "milestone": "M1",
                                                      "parent_iid": 4}, ["grp/app"], hidden)
        post = next(x for x in self.op.sent if x[:2] == ("POST", BASE + "/issues"))[3]
        self.assertEqual(post, {"title": "T", "description": "D", "labels": "a,b", "assignee_ids": [41],
                                "milestone_id": 7, "issue_type": "task"})
        self.assertEqual(self.op.sent[0][2], {"username": "alice"})
        gq = self.op.sent[-1][3]["variables"]
        self.assertEqual(gq, {"id": "gid://gitlab/WorkItem/900", "parent": "gid://gitlab/WorkItem/800"})
        self.assertEqual((out["iid"], out["parent_iid"]), (12, 4))

    def test_issue_create_keeps_the_issue_when_the_parent_fails(self):
        gl = self.gl({("POST", BASE + "/issues"): {"id": 900, "iid": 12},
                      ("GET", BASE + "/issues/4"): {"id": 800, "iid": 4},
                      ("POST", "/api/graphql"): {"data": {"workItemUpdate": {"errors": ["not allowed"]}}}})
        out = gitlab.call(gl, "gitlab_issue_create", {"title": "T", "parent_iid": 4}, ["grp/app"], hidden)
        self.assertEqual(out["iid"], 12)
        self.assertTrue(out["partial"])
        self.assertEqual((out["done"], out["failed"]), (["create the issue"], "set the parent #4"))
        self.assertIn("not allowed", out["error"])

    def test_issue_update_parent_first_spent_time_last(self):
        """The step most likely to fail runs first (nothing happened yet: a plain error), the one that
        must not run twice last; a later failure says what was done."""
        routes = {("GET", BASE + "/issues/4"): {"id": 800, "iid": 4}, ("GET", BASE + "/issues/5"): {"id": 500, "iid": 5},
                  ("POST", "/api/graphql"): {"data": {"workItemUpdate": {"errors": ["task under a task"]}}},
                  ("PUT", BASE + "/issues/5"): {"iid": 5}, ("POST", BASE + "/issues/5/add_spent_time"): {}}
        gl = self.gl(routes)
        args = {"iid": 5, "add_labels": "x", "spend": "1h", "parent_iid": 4}
        p = gitlab.plan(gl, "gitlab_issue_update", args, ["grp/app"])
        self.assertEqual([st["what"] for st in p["steps"]], ["set the parent", "change the issue", "log the spent time"])
        with self.assertRaises(gitlab.GitLabError):
            gitlab.execute(gl, p)
        self.assertFalse(any(m in ("PUT", "POST") and "/issues/5" in path for m, path, _ in self.op.calls))
        routes[("POST", "/api/graphql")] = {"data": {"workItemUpdate": {"errors": []}}}
        routes[("POST", BASE + "/issues/5/add_spent_time")] = urllib.error.HTTPError("u", 500, "x", {}, None)
        out = gitlab.call(self.gl(routes), "gitlab_issue_update", args, ["grp/app"], hidden)
        self.assertEqual((out["done"], out["failed"]), (["set the parent", "change the issue"], "log the spent time"))

    def test_redirect_is_not_followed(self):
        """urllib would carry PRIVATE-TOKEN to the redirect target (object storage) - the real opener refuses."""
        import http.server
        import threading

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.assertIsNone(gitlab._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil/"))
        try:
            srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        except OSError:
            self.skipTest("no local port (sandbox)")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        gl = gitlab.GitLab("x", "tok")
        gl.base = "http://127.0.0.1:%d/api/v4" % srv.server_port
        with self.assertRaises(gitlab.GitLabError) as e:
            gl._req("GET", "/projects/1")
        self.assertIn("not followed", str(e.exception))

    def test_graphql_page_that_is_not_json_does_not_break_the_issue(self):
        gl = self.gl(self.issue_routes({("POST", "/api/graphql"): (b"<html>maintenance</html>", "text/html")}))
        out = gitlab.call(gl, "gitlab_issue", {"iid": 7}, ["grp/app"], hidden)
        self.assertEqual(out["iid"], 7)
        self.assertNotIn("parent", out)

    def test_notes_truncated_when_more_threads_than_read(self):
        def opener(req, timeout=None):
            path = urllib.parse.urlsplit(req.full_url).path
            if path.endswith("/discussions"):
                return io.BytesIO(json.dumps([{"id": "ab" * 4, "notes": [{"id": 1, "body": "x"}]}] * 100).encode())
            if path.endswith("/links"):
                return io.BytesIO(b"[]")
            if path.endswith("/graphql"):
                return io.BytesIO(b'{"data": {}}')
            return io.BytesIO(json.dumps({"iid": 7}).encode())

        out = gitlab.call(gitlab.GitLab("gitlab.example", "tok", opener=opener), "gitlab_issue", {"iid": 7},
                          ["grp/app"], hidden)
        self.assertIn("newest may be missing", out["notes_truncated"])

    def test_a_short_discussions_page_is_not_the_last(self):
        """2026-10-12: GitLab answered page 2 of an issue's 270 threads with 99 (it filters after paginating) - the
        short page counted as the last, page 3 with the newest comment never came and nothing said so."""
        sizes = {1: 100, 2: 99, 3: 71}

        def opener(req, timeout=None):
            u = urllib.parse.urlsplit(req.full_url)
            if u.path.endswith("/discussions"):
                page = int(urllib.parse.parse_qs(u.query)["page"][0])
                return io.BytesIO(json.dumps([{"id": "t%d-%d" % (page, i), "notes": [{"id": page * 1000 + i, "body": "x"}]}
                                              for i in range(sizes.get(page, 0))]).encode())
            if u.path.endswith("/links"):
                return io.BytesIO(b"[]")
            if u.path.endswith("/graphql"):
                return io.BytesIO(b'{"data": {}}')
            return io.BytesIO(json.dumps({"iid": 7}).encode())

        out = gitlab.call(gitlab.GitLab("gitlab.example", "tok", opener=opener), "gitlab_issue", {"iid": 7},
                          ["grp/app"], hidden)
        self.assertEqual(len(out["notes"]), 270)
        self.assertIn(3070, [n["id"] for n in out["notes"]])

    def test_scrub_text_catches_what_the_common_scrub_misses(self):
        text = ("DB_PASS=hunter2\nSMTP_PASSWORD: s3cr3t\nJWT_SIGNING_KEY=abc\n{\"password\": \"x y\", \"user\": \"u\"}\n"
                "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n" + "QUJD" * 20 + "\nplain line")
        out = gitlab.scrub_text(text)
        for secret in ("hunter2", "s3cr3t", "=abc", "x y", "MIIEow", "QUJDQUJD"):
            self.assertNotIn(secret, out, secret)
        self.assertIn('"user": "u"', out)
        self.assertIn("plain line", out)

    def test_log_tail_scrubs_before_the_cut(self):
        raw = ("x" * 10 + "\nAPI_PASSWORD=hunter2 tail\n" + "line\n" * 3).encode()
        self.assertNotIn("hunter2", gitlab._log_tail(raw, 4))
        big = ("A_PASSWORD=" + "h" * (gitlab.TRACE_KEEP) + "\nlast\n").encode()[-gitlab.TRACE_KEEP:]
        self.assertEqual(gitlab._log_tail(big, 5), "last")               # a kept tail's cut first line is dropped
        long_last = ("ok\n" + "error at " * 8000).encode()               # one last line over the byte cap
        out = gitlab._log_tail(long_last, 5)
        self.assertTrue(out.startswith("[cut] "))
        self.assertTrue(out.endswith("error at "))
        self.assertLessEqual(len(out.encode()), gitlab.LOG_MAX_BYTES + 6)

    def test_scrub_text_stays_linear_on_a_long_line(self):
        import time
        for line in ("ab-" * 300000, "a." * 500000, "token-" * 150000, "://a:" * 200000):
            t = time.time()
            gitlab.scrub_text(line)
            self.assertLess(time.time() - t, 3, line[:6])               # was minutes (quadratic backtracking)

    def test_scrubbing_stays_linear_on_hostile_input(self):
        """re holds the GIL: a quadratic pattern on one long line froze all of piperd (minutes)."""
        import time
        hostile = ["a-" * 300000, "a." * 300000, "key" * 200000, "-----BEGIN RSA PRIVATE KEY-----\n" * 20000,
                   '"' * 600000, "://a:" * 120000, "://a" * 150000, 'key="' * 120000, "Authorization:" * 40000,
                   "password:\n" * 60000, 'key="\\\\' * 100000, "mysql " * 100000, "://a:b" * 100000,
                   '\\"key\\":\\"' * 60000, "-----BEGIN PRIVATE KEY-----\n" * 20000, "-----BEGIN PRIVATE KEY-----" * 20000]
        for text in hostile:
            for f in (gitlab.scrub_text, gitlab.scrub_strict, gitlab._scrub_obj):
                t = time.time()
                f(text)
                self.assertLess(time.time() - t, 2, (f.__name__, text[:12]))
        t = time.time()
        gitlab._log_tail(("section_start:1:a[" * 100000).encode(), 200)
        self.assertLess(time.time() - t, 2)

    def test_scrub_strict_covers_common_scrub(self):
        import common
        for c in ('password = "a b"', "token: x1", "Bearer x2", "Authorization: Basic x3", "api-key=x4", "x://u:p5@h",
                  "db password=hunter2", "PRIVATE_KEY=abc6", "access_key: z7", "spring.datasource.password      = s8",
                  "some.secret.value.with.a.very.long.name.far.beyond.sixty.four.characters.for.sure=x9"):
            out = gitlab.scrub_strict(c)
            self.assertIn("[scrubbed]", out, c)
            for secret in ("a b", "x1", "x2", "x3", "x4", "p5", "hunter2", "abc6", "z7", "s8", "x9"):
                if secret in common.scrub(c) or secret in c.split()[-1]:
                    self.assertNotIn("=" + secret, out)
                    self.assertFalse(out.endswith(" " + secret), c)
        for keep in ("Session timeout: 30 min", "key: value", "see https://gitlab.com/a/b"):
            self.assertEqual(gitlab.scrub_strict(keep), keep)              # issue texts stay readable

    def test_scrub_edge_cases_from_review(self):
        strict = {"https://user:" + "t" * 300 + "@h/x": "https://user:[scrubbed]@h/x",   # a long token in a URL
                  "password:\n  hunter2": "password:\n  [scrubbed]",                 # YAML value on the next line
                  "Bearer\nabc123": "Bearer\n[scrubbed]",
                  "Passwords: are hashed": "Passwords: are hashed",                    # prose stays readable
                  "Secretary: Jana Nova": "Secretary: Jana Nova", "Tokenizer: broken": "Tokenizer: broken",
                  '{"password":"hun\\"ter 2"}': '{"password":[scrubbed]',          # an escaped quote
                  "{'password': 'x4'}": "{'password': [scrubbed]",                    # glued on: up to whitespace (\\S+) "pwd=\nnext": "pwd=\n[scrubbed]",   # as common.scrub
                  "https://user:ab/cd@host/x": "https://user:[scrubbed]@host/x",           # a "/" in the password
                  "Authorization: Bearer\nabc": "Authorization: [scrubbed]",
                  '{\\"password\\":\\"s3cr3t\\"}': '{\\"password\\":[scrubbed]',   # JSON encoded in a string
                  '"password": "a\\\\\\"bSECRET rest"': '"password": "[scrubbed]"',          # \\\" inside the value
                  "password:\n\n  hunter2": "password:\n\n  [scrubbed]", "password\n: x": "password\n: [scrubbed]",
                  "see https://gitlab.com/a/b and mail a@b.c": "see https://gitlab.com/a/b and mail a@b.c"}
        for text, want in strict.items():
            self.assertEqual(gitlab.scrub_strict(text), want, text)
        for text in ("password := 'x2'", "'password' => 'x3'"):                 # := and => (common takes "=" as the value)
            self.assertNotIn("x", gitlab.scrub_strict(text).replace("[scrubbed]", ""), text)
        out = gitlab.scrub_text("expected -----BEGIN RSA PRIVATE KEY----- header\nERROR: build failed because X")
        self.assertIn("ERROR: build failed because X", out)                    # a quoted header keeps the log
        out = gitlab.scrub_text("a\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234\nAAAABBBBCCCCDDDD==\nnext")
        self.assertNotIn("MIIEow", out)
        self.assertNotIn("AAAABBBB", out)
        self.assertTrue(out.endswith("next"))
        out = gitlab.scrub_text("-----BEGIN RSA PRIVATE KEY-----MIIEsecret\\nAAAA\nafter")      # a one-line PEM
        self.assertEqual(out, "[scrubbed: private key]\nafter")
        out = gitlab.scrub_text("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,AB\n\n"
                                "MIIEowIBAAKCAQEA1234\nshort==\nERROR next")                       # encrypted, short last line
        self.assertEqual(out, "[scrubbed: private key]\nERROR next")
        import common
        for c in ("https://user:" + "t" * 300 + "@h/x", "x://u:p5@h", "password = \"a b\"", "token: x1", "Bearer x2",
                  "Authorization: Basic x3", "api-key=x4", "db password=hunter2", "PRIVATE_KEY=abc6", "pwd=\nnext"):
            self.assertEqual(gitlab.scrub_strict(c).count("[scrubbed]"), max(1, common.scrub(c).count("[scrubbed]")), c)

    def test_scrub_strict_hides_what_common_scrub_hides(self):
        """Round 6: unicode whitespace, bearer after blank lines, a quoted value glued to more text, a
        long URL user, rule order - nothing common.scrub hides may stay visible."""
        import common
        for c in ("password:\xa0SECRET", "password\xa0=SECRET", "password=\u2003SECRET", "password:\x0bSECRET",
                  "password\u2028:SECRET", "Bearer\n\nSECRET", "Bearer\rSECRET", "Bearer\xa0SECRET",
                  "x@hbearer\nBearer SECRET", 'PASSWORD="$P"SECRET', "token='a'SECRET", "http://" + "u" * 600 + ":SECRET@h"):
            for f in (gitlab.scrub_strict, gitlab.scrub_text):
                self.assertNotIn("SECRET", f(c), (f.__name__, c))
        self.assertEqual(gitlab.scrub_strict('{"password":"x","user":"u"}'), '{"password":[scrubbed]')   # as common's \\S+
        self.assertEqual(gitlab.scrub_strict('{"password": "x y", "user": "u"}'), '{"password": [scrubbed] "user": "u"}')
        for c in ("> X-Auth: Bearer abc123", '"token": Bearer abc123', "Authorization : (_token_ = =SEKRET",
                  'token : "a",SEKRET', "password://SEKRET", "x:token: SEKRET", "a:x--password = SEKRET"):
            for f in (gitlab.scrub_strict, gitlab.scrub_text):          # round 7: rule order, glued values, names in values
                self.assertLessEqual(f(c).count("SEKRET") + f(c).count("abc123"),
                                     common.scrub(c).count("SEKRET") + common.scrub(c).count("abc123"), (f.__name__, c))
        import time
        t = time.time()
        gitlab.scrub_text("-----BEGIN PRIVATE KEY-----\n" + " " * 300000 + "x")   # was quadratic (KEY_LINE_RX)
        self.assertLess(time.time() - t, 1)

    def test_gitlab_scrubs_hide_every_rule_common_scrub_has(self):
        """Round 11: _common_spans mirrored only common.scrub's first four rules - a bare glpat-/ghp_ token
        (rule added 2026-10-06), PGPASSWORD=, a JSON "password": "a b", a key block reached the sandbox
        through GitLab issue texts and job logs unscrubbed."""
        for c in ("see glpat-AbCdEf1234567890xyz here", "PGPASSWORD=s3cr3tvalue psql", '{"password": "two s3cr3tvalue"}',
                  "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCs3cr3tvalue\n-----END RSA PRIVATE KEY-----",
                  "id AKIAIOSFODNN7EXAMPLE", "mysql -u r -ps3cr3tvalue db", "tool --token s3cr3tvalue"):
            for f in (gitlab.scrub_strict, gitlab.scrub_text):
                out = f(c)
                for secret in ("glpat-AbCdEf", "s3cr3tvalue", "AKIAIOSFODNN7EXAMPLE"):
                    self.assertNotIn(secret, out, (f.__name__, c))

    def test_common_linear_is_common_scrub(self):
        """_common_linear gives common.scrub's exact output (scrub_strict/scrub_text start from it, so they
        hide at least what it hides) - in linear time."""
        import common
        import random
        import time
        rnd = random.Random(5)
        toks = ["password", "db.password", "-password", ".token", "_token_", "x-api-key", "api_key", "Api-Key", "pwd",
                "Passwords", "mypassword", "token2", "Authorization", "authorization", "Bearer", "user", "a", "é", "1",
                "=", ":", " = ", ": ", ":=", "=>", "\n", "\r\n", "\xa0", "\u2028", " ", "\t", '"', "'", ",", "{", "--",
                "-", ".", "_", "@", "://", "u:p@", "SEKRET", "(",
                # round 11: common.scrub's newer rules - quoted values, env names, flags, mysql -p, keys, ids
                "PGPASSWORD", "--password ", "--token", "mysql ", " -p", "-pSEKRET", "glpat-", "ghp_", "AKIA",
                "IOSFODNN7EXAMPLE", "eyJ", "abcDEF123", '"SEKRET a"', "'x y'", "-----BEGIN RSA PRIVATE KEY-----",
                "MIIEow==", "-----END RSA PRIVATE KEY-----", "redis://", ":SEKRET@", "\\n"]
        for _ in range(20000):
            line = "".join(rnd.choice(toks) for _ in range(rnd.randint(1, 12)))
            self.assertEqual(gitlab._common_linear(line), common.scrub(line), line)
            for f in (gitlab.scrub_strict, gitlab.scrub_text):
                self.assertLessEqual(f(line).count("SEKRET"), common.scrub(line).count("SEKRET"), (f.__name__, line))
        for text in ("a-" * 300000, "-" * 600000, "://a:" * 120000, "authorization:" * 40000):
            t = time.time()
            gitlab._common_linear(text)
            self.assertLess(time.time() - t, 2, text[:12])

    def test_text_upload_in_any_encoding_is_scrubbed(self):
        """Round 9: a declared charset, NUL padding, UTF-16 without a BOM, UTF-32 - the scrub must see the text."""
        sec = "e" * 32
        cases = [("password=SEKRET\n".encode("utf-16-le"), "text/plain; charset=utf-8"),
                 ("password=SEKRET\n".encode("utf-16-le"), "text/plain; charset=iso-8859-1"),
                 ("password=SEKRET\n".encode("utf-16-be"), "text/plain; charset=utf-16"),
                 (b"\x00" * 2048 + b"password=SEKRET\n", "text/plain"),
                 (("\u011b\u0161\u010d" * 100 + "\npassword=SEKRET\n").encode("utf-16-le"), "text/plain"),
                 ("password=SEKRET\n".encode("utf-32"), "text/plain"), ("password=SEKRET\n".encode("utf-32-le"), "text/plain"),
                 ("password=SEKRET \u010d\n".encode("cp1250"), "text/plain"),
                 (b"-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234\n-----END RSA PRIVATE KEY-----\n", "application/octet-stream")]
        cases += [(b"ok\n\xff\ndb.password=SEKRET\n", "application/octet-stream"),               # round 10: no text ext
                  ("header ok\ndb.password=SEKRET\n".encode() + "PS appended".encode("utf-16-le") * 30, "text/plain"),
                  (b"<?php $db_password = 'SEKRET';", "application/x-httpd-php"),
                  (("\u65e5\u672c" * 200 + "\ndb.password=SEKRET\n").encode("utf-16-le"), "application/octet-stream")]
        for raw, ctype in cases:
            gl = self.gl({("GET", BASE + "/uploads/%s/id_rsa" % sec): (raw, ctype)})
            try:
                out = gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/id_rsa" % sec}, ["grp/app"], hidden)
            except gitlab.GitLabError as e:   # refusing it is fine, leaking it is not
                self.assertEqual(e.status, 415, (raw[:12], ctype))
                continue
            self.assertTrue(out["scrubbed"], ctype)
            for enc in ("utf-16-le", "utf-16-be"):   # nothing reads back as the secret in another encoding
                self.assertNotIn(b"SEKRET", out["_bytes"].decode("utf-8").encode(enc, "ignore"), (raw[:12], ctype))
            text = out["_bytes"].decode("utf-8")
            self.assertNotIn("SEKRET", text, (raw[:12], ctype))
            self.assertNotIn("MIIEow", text)
        for raw in (b"PK\x03\x04\x14\x00\x00\x00" + bytes(range(256)) * 4, b"\x1f\x8b\x08\x00" + bytes(range(256)) * 4):
            gl = self.gl({("GET", BASE + "/uploads/%s/a.zip" % sec): (raw, "text/plain")})   # declared text: content decides
            with self.assertRaises(gitlab.GitLabError) as e:
                gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/a.zip" % sec}, ["grp/app"], hidden)
            self.assertEqual(e.exception.status, 415)
        pdf = self.gl({("GET", BASE + "/uploads/%s/a.pdf" % sec): (b"%PDF-1.7\n...", "application/pdf")})
        self.assertEqual(gitlab.call(pdf, "gitlab_upload", {"url": "/uploads/%s/a.pdf" % sec}, ["grp/app"], hidden)["_bytes"],
                         b"%PDF-1.7\n...")
        import os
        import zlib
        tlv = b"\x0a\x0bDB_PASSWORD\x12\x0dhunter2Secret"                  # round 12: protobuf-like, valid UTF-8
        for raw in [os.urandom(n) for n in (64, 128, 512, 2048) for _ in range(50)] + [zlib.compress(b"db.password=SEKRET"),
                                                                                       tlv, tlv * 3, tlv * 6]:
            self.assertEqual(gitlab._upload_text(raw)[0], "binary", raw[:8])    # round 11: small binaries are not "text"
        for text in ("Zpr\u00e1va: heslo je \u0161patn\u011b\ndb.password=SEKRET\n", "\u041f\u0440\u0438\u0432\u0435\u0442\ndb.password=SEKRET\n"):
            text += "\u2713 \u2264 \u2212 \u2588 \u02c7 \U0001f600\n"                        # symbols PowerShell prints
            for raw in (("\ufeff" + text).encode("utf-16-le"), text.encode("utf-16-be")):   # PowerShell 5: readable, scrubbed
                kind, out = gitlab._upload_text(raw)
                self.assertEqual(kind, "utf16")
                self.assertIn(text.split("\n")[0].lstrip("\ufeff"), gitlab.scrub_text(out))
                self.assertNotIn("SEKRET", gitlab.scrub_text(out))
        cut = ("\u017elu\u0165ou\u010dk\u00fd k\u016f\u0148 db.password=SEKRET\n" * 3).encode()[:-2] + "\u017e".encode()[:1]
        kind, out = gitlab._upload_text(cut)                                       # a UTF-8 log cut inside a character
        self.assertEqual(kind, "utf8")
        self.assertNotIn("SEKRET", gitlab.scrub_text(out))
        self.assertEqual(gitlab._upload_text(b"\x1b[31mERR\x1b[0m " + "\u010desky\n".encode("cp1250") * 20)[0], "bytes")   # ANSI cp1250
        for raw in (b"\x83\xa8password\xb0hunter2-supersecr\xa4user\xa5alice\xa4host\xa9localhost",   # MessagePack
                    b"\xa3\x68password\x70hunter2-supersecr\x64user\x65alice\x64host\x69localhost",  # CBOR
                    b"x" * 40 + b"\xff"):                                                          # not a cut character
            self.assertEqual(gitlab._upload_text(raw)[0], "binary", raw[:12])
        gl = self.gl({("GET", BASE + "/uploads/%s/big.log" % sec): (b"x " * (gitlab.TEXT_SCRUB_MAX // 2 + 1), "text/plain")})
        with self.assertRaises(gitlab.GitLabError) as e:   # scrubbing it would hold piperd for seconds
            gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/big.log" % sec}, ["grp/app"], hidden)
        self.assertEqual(e.exception.status, 413)

    def test_trace_stream_stops_at_the_deadline(self):
        from unittest import mock

        class Drip(io.BytesIO):
            def read1(self, n=-1):
                return b"x" * 10

        gl = gitlab.GitLab("gitlab.example", "tok", opener=lambda req, timeout=None: Drip())
        clock = iter(range(0, 1000, 7))
        with mock.patch.object(gitlab.time, "monotonic", lambda: next(clock)):
            with self.assertRaises(gitlab.GitLabError) as e:
                gl._open("GET", gl.base + "/projects/1/jobs/1/trace", tail=100)
        self.assertEqual(e.exception.status, 504)

    def test_log_tail_without_a_newline_is_not_empty(self):
        out = gitlab._log_tail((b"progress 10% " * gitlab.TRACE_KEEP)[:gitlab.TRACE_KEEP], 200)   # one line, no newline
        self.assertTrue(out.startswith("[cut] "))
        self.assertIn("progress 10%", out)

    def test_scrub_text_cli_options(self):
        out = gitlab.scrub_text("mvn deploy --password hunter2zz\ndocker login -u ci -p hunter3zz reg\n"
                                "curl -u bob:hunter4 x\n--verbose ok\n-p 8080\n--password 'correct horse battery'\n"
                                "sshpass -p hunter5 ssh h\nmysql -phunter6 db")
        for secret in ("hunter2zz", "hunter3zz", "hunter4", "horse", "hunter5", "hunter6"):
            self.assertNotIn(secret, out)
        self.assertIn("--verbose ok", out)
        self.assertIn("-p 8080", out)

    def test_text_upload_is_scrubbed_binary_is_not(self):
        sec = "d" * 32
        gl = self.gl({("GET", BASE + "/uploads/%s/app.properties" % sec): (b"db.password=hunter2\nurl=x", "text/plain"),
                      ("GET", BASE + "/uploads/%s/a.png" % sec): (b"\x89PNG\r\n\x1a\n password=1", "image/png")})
        out = gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/app.properties" % sec}, ["grp/app"], hidden)
        self.assertNotIn(b"hunter2", out["_bytes"])
        self.assertTrue(out["scrubbed"])
        out = gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/a.png" % sec}, ["grp/app"], hidden)
        self.assertEqual(out["_bytes"], b"\x89PNG\r\n\x1a\n password=1")

    def test_issue_create_validation(self):
        gl = self.gl({("GET", "/api/v4/users"): []})
        for args in ({"title": " "}, {"title": "x" * 256}, {"title": "T", "type": "epic"},
                     {"title": "T", "assignees": ["u%d" % i for i in range(21)]},
                     {"title": "T", "due_date": "tomorrow"}, {"title": "T", "labels": ["a,b"]},
                     {"title": "T", "assignees": ["nobody"]}):
            with self.assertRaises(gitlab.GitLabError, msg=args):
                gitlab.call(gl, "gitlab_issue_create", args, ["grp/app"], hidden)
        self.assertFalse(any(m == "POST" for m, *_ in self.op.calls))      # nothing created

    def test_issue_update_fields_state_time(self):
        gl = self.gl({("PUT", BASE + "/issues/5"): {"iid": 5, "state": "closed"},
                      ("POST", BASE + "/issues/5/add_spent_time"): {"total_time_spent": 5400}})
        out = gitlab.call(gl, "gitlab_issue_update", {"iid": 5, "add_labels": "done", "state": "close",
                                                      "milestone": "", "spend": "1h30m"}, ["grp/app"], hidden)
        put = self.op.sent[0][3]
        self.assertEqual(put, {"add_labels": "done", "milestone_id": 0, "state_event": "close"})
        self.assertEqual(self.op.sent[1][2], {"duration": "1h30m"})
        self.assertEqual(out["time_stats"], {"total_time_spent": 5400})
        for args in ({"iid": 5}, {"iid": 5, "state": "delete"}, {"iid": 5, "spend": "an hour"}):
            with self.assertRaises(gitlab.GitLabError, msg=args):
                gitlab.call(gl, "gitlab_issue_update", args, ["grp/app"], hidden)

    def test_one_line_fields_cannot_push_card_rows_out_of_sight(self):
        """Round 14: a title / label / duration / milestone / username with a line break would push the next
        rows of the card (state: close, confidential: false) below its visible part - refused before the card."""
        gl = self.gl({})
        pad = "\n" * 40 + "x"
        for tool, args in (("gitlab_issue_update", {"iid": 5, "title": "Fix typo" + pad, "state": "close"}),
                           ("gitlab_issue_update", {"iid": 5, "estimate": "1h" + pad}),
                           ("gitlab_issue_update", {"iid": 5, "spend": "1h\n2h"}),
                           ("gitlab_issue_update", {"iid": 5, "add_labels": ["ok" + pad]}),
                           ("gitlab_issue_update", {"iid": 5, "milestone": "M1\r\nx"}),
                           ("gitlab_issue_create", {"title": "T", "assignees": ["bob\u2028"]}),
                           ("gitlab_mr_create", {"title": "T\rX", "source_branch": "a", "target_branch": "b"})):
            with self.assertRaises(gitlab.GitLabError, msg=(tool, args)):
                gitlab.plan(gl, tool, args, ["grp/app"])
        self.assertEqual(self.op.calls, [])
        p = gitlab.plan(gl, "gitlab_issue_update", {"iid": 5, "estimate": "1h 30m"}, ["grp/app"])   # a space is fine
        self.assertIn("estimate: 1h 30m", gitlab.card_text(p))

    def test_issue_link_stays_in_scope(self):
        gl = self.gl({("POST", BASE + "/issues/5/links"): {}, ("GET", BASE + "/issues/6"): {"iid": 6},
                      ("GET", BASE + "/issues/5"): {"iid": 5}, ("GET", BASE + "/issues/5/links"): [
                          {"iid": 7, "link_type": "blocks", "references": {"full": "grp/app#7"}}],
                      ("GET", BASE + "/issues/7"): {"iid": 7},
                      ("GET", BASE + "/issues/999"): urllib.error.HTTPError("u", 404, "x", {}, None)})
        for args in ({"iid": 999, "target_iid": 6}, {"iid": 6, "target_iid": 999}, {"iid": 5, "target_iid": 5},
                     {"iid": 5, "target_iid": 7}):   # 7: already linked
            with self.assertRaises(gitlab.GitLabError, msg=args):   # missing / self: refused before any card
                gitlab.plan(gl, "gitlab_issue_link", args, ["grp/app"])
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_issue_link", {"iid": 5, "target_iid": 6, "target_project": "grp/other"},
                        ["grp/app"], hidden)
        out = gitlab.call(gl, "gitlab_issue_link", {"iid": 5, "target_iid": 6, "type": "blocks"}, ["grp/app"], hidden)
        self.assertEqual(out["target"], "grp/app#6")
        self.assertEqual(self.op.sent[-1][3], {"target_project_id": "grp/app", "target_issue_iid": 6, "link_type": "blocks"})

    def test_mr_create_draft_and_default_target(self):
        gl = self.gl({("GET", BASE): {"default_branch": "master"},
                      ("POST", BASE + "/merge_requests"): {"iid": 2, "title": "Draft: X", "web_url": "u"}})
        out = gitlab.call(gl, "gitlab_mr_create", {"title": "X", "source_branch": "feat/a", "draft": True,
                                                   "remove_source_branch": True}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][3], {"source_branch": "feat/a", "target_branch": "master", "title": "Draft: X",
                                               "remove_source_branch": True})
        self.assertEqual(out["iid"], 2)
        for bad in ("", "a b", "-x", "a..b"):
            with self.assertRaises(gitlab.GitLabError, msg=bad):
                gitlab.call(gl, "gitlab_mr_create", {"title": "X", "source_branch": bad}, ["grp/app"], hidden)

    def test_mr_update_draft_toggles_the_title_prefix(self):
        gl = self.gl({("GET", BASE + "/merge_requests/2"): {"title": "Draft: X"},
                      ("PUT", BASE + "/merge_requests/2"): {"iid": 2}})
        gitlab.call(gl, "gitlab_mr_update", {"iid": 2, "draft": False, "reviewers": []}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][3], {"title": "X", "reviewer_ids": []})
        gitlab.call(gl, "gitlab_mr_update", {"iid": 2, "title": "Y", "draft": True}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][3], {"title": "Draft: Y"})
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_mr_update", {"iid": 2}, ["grp/app"], hidden)

    def test_pipeline_jobs_and_failed_job_log_tail(self):
        log = ("\x1b[0Ksection_start:1:prepare\r\x1b[0Kstep\n" + "".join("line %d\n" % i for i in range(300)) +
               "progress 10%\rprogress 100%\n2026-10-05T09:05:26.084609Z 01E DB_PASSWORD=hunter2\n\x1b[31;1mERROR: Job failed\x1b[0;m\n").encode()
        gl = self.gl({("GET", BASE + "/pipelines/77/jobs"): [{"id": 1, "name": "test", "stage": "test", "status": "failed",
                                                              "failure_reason": "script_failure", "token": "x"}],
                      ("GET", BASE + "/jobs/1"): {"id": 1, "status": "failed"},
                      ("GET", BASE + "/jobs/2"): {"id": 2, "status": "success"},
                      ("GET", BASE + "/jobs/1/trace"): (log, "text/plain")})
        jobs = gitlab.call(gl, "gitlab_pipeline_jobs", {"pipeline_id": 77}, ["grp/app"], hidden)["jobs"]
        self.assertEqual(jobs[0]["failure_reason"], "script_failure")
        self.assertNotIn("token", jobs[0])
        out = gitlab.call(gl, "gitlab_job_log", {"job_id": 1, "lines": 3}, ["grp/app"], hidden)
        self.assertEqual(out["log"].split("\n"), ["progress 100%", "DB_PASSWORD=[scrubbed]", "ERROR: Job failed"])
        with self.assertRaises(gitlab.GitLabError) as e:   # K5: only a failed job's log
            gitlab.call(gl, "gitlab_job_log", {"job_id": 2}, ["grp/app"], hidden)
        self.assertEqual(e.exception.status, 403)
        self.assertNotIn(("GET", BASE + "/jobs/2/trace", "tok"), self.op.calls)

    def test_upload_through_the_api_never_a_web_page(self):
        sec = "c" * 32
        api = BASE + "/uploads/%s/shot%%20a.png" % sec
        gl = self.gl({("GET", api): (b"\x89PNG\r\n\x1a\n", "image/png")})
        out = gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/shot%%20a.png" % sec}, ["grp/app"], hidden)
        self.assertEqual((out["_bytes"], out["filename"], out["content_type"]), (b"\x89PNG\r\n\x1a\n", "shot a.png", "image/png"))
        self.assertEqual(self.op.calls, [("GET", api, "tok")])          # a full web link works too, via the API
        gl = self.gl({("GET", api): (b"PDF", "application/pdf")})
        self.assertEqual(gitlab.call(gl, "gitlab_upload", {"url": "https://gitlab.example/grp/app/uploads/%s/shot%%20a.png"
                                                                  % sec}, ["grp/app"], hidden)["_bytes"], b"PDF")
        gl = self.gl({("GET", api): (b"<html>", "text/html")})
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/shot%%20a.png" % sec}, ["grp/app"], hidden)
        gl = self.gl({("GET", api): urllib.error.HTTPError("u", 404, "Not Found", {}, None)})
        with self.assertRaises(gitlab.GitLabError) as e:
            gitlab.call(gl, "gitlab_upload", {"url": "/uploads/%s/shot%%20a.png" % sec}, ["grp/app"], hidden)
        self.assertIn("17.4", str(e.exception))
        for bad in ("/etc/passwd", "/uploads/%s/.." % sec, "/uploads/xyz/a.png"):
            with self.assertRaises(gitlab.GitLabError, msg=bad):
                gitlab.call(self.gl({}), "gitlab_upload", {"url": bad}, ["grp/app"], hidden)

    def test_card_text_shows_every_field(self):
        gl = self.gl({})
        t = gitlab.card_text(gitlab.plan(gl, "gitlab_issue_create", {"title": "T", "labels": ["a"], "description": "long\nbody",
                                                                     "type": "task", "due_date": ""}, ["grp/app"]))
        self.assertTrue(t.startswith("New GitLab task in grp/app:"))
        self.assertIn("labels: a", t)
        self.assertIn("long\nbody", t)
        t = gitlab.card_text(gitlab.plan(gl, "gitlab_comment", {"kind": "mr", "iid": 3, "body": "hello",
                                                                "discussion_id": "ab12cd34"}, ["grp/app"]))
        self.assertIn("reply in thread ab12cd34", t)
        self.assertTrue(t.endswith("hello"))
        self.assertEqual(self.op.calls, [])                                # planning these needs no lookup
        self.assertEqual(set(gitlab.CARD_TOOLS) - set(gitlab.TOOLS), set())

    def test_scope(self):
        gl = self.gl({})
        with self.assertRaises(gitlab.GitLabError) as e:
            gitlab.call(gl, "gitlab_issue", {"project": "grp/other", "iid": 1}, ["grp/app"], hidden)
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_issue", {"iid": 1}, ["a/b", "c/d"], hidden)   # ambiguous
        self.assertEqual(self.op.calls, [])

    def test_mr_changes_withhold_policy_files(self):
        gl = self.gl({("GET", BASE + "/merge_requests/3/diffs"): [
            {"old_path": "src/A.java", "new_path": "src/A.java", "diff": "+a"},
            {"old_path": "config/secrets/app-production.properties",
             "new_path": "config/secrets/app-production.properties", "diff": "+pw=1"},
            {"old_path": "config/secrets/db.properties", "new_path": "config/secrets/db.properties",
             "diff": "+dbpw=1"}]})
        out = gitlab.call(gl, "gitlab_mr_changes", {"iid": 3}, ["grp/app"], hidden)
        self.assertEqual(out["files"][0]["diff"], "+a")
        self.assertIn("withheld", out["files"][1]["diff"])
        self.assertIn("withheld", out["files"][2]["diff"])   # inside a hidden dir, name matches nothing

    def test_unknown_tool_and_no_generic_path(self):
        gl = self.gl({})
        for tool in ("gitlab_files", "repository/files", "gitlab_raw"):
            with self.assertRaises(gitlab.GitLabError):
                gitlab.call(gl, tool, {"path": "config/secrets"}, ["grp/app"], hidden)

    def test_comment_validation(self):
        gl = self.gl({("POST", BASE + "/issues/5/notes"): {"id": 99}})
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_comment", {"kind": "wiki", "iid": 5, "body": "x"}, ["grp/app"], hidden)
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_comment", {"kind": "issue", "iid": 5, "body": " "}, ["grp/app"], hidden)
        out = gitlab.call(gl, "gitlab_comment", {"kind": "issue", "iid": 5, "body": "Done."}, ["grp/app"], hidden)
        self.assertEqual(out["note_id"], 99)

    def test_job_play_starts_manual_and_retries_failed(self):
        for status, verb in (("manual", "play"), ("scheduled", "play"), ("failed", "retry")):
            gl = self.gl({("GET", BASE + "/jobs/9"): {"id": 9, "name": "compile", "stage": "build", "status": status, "ref": "x"},
                          ("POST", BASE + "/jobs/9/" + verb): {"id": 10, "name": "compile", "status": "pending"}})
            p = gitlab.plan(gl, "gitlab_job_play", {"job_id": 9}, ["grp/app"])
            self.assertEqual([(x["m"], x["path"]) for x in p["steps"]], [("POST", "/projects/grp%2Fapp/jobs/9/" + verb)])
            self.assertEqual(gitlab.execute(gl, p)["status"], "pending")
        gl = self.gl({("GET", BASE + "/jobs/9"): {"id": 9, "status": "running"}})
        with self.assertRaisesRegex(gitlab.GitLabError, "only a manual"):
            gitlab.plan(gl, "gitlab_job_play", {"job_id": 9}, ["grp/app"])

    def test_mr_merge(self):
        gl = self.gl({("GET", BASE + "/merge_requests/3"): {"iid": 3, "state": "opened", "title": "t",
                                                            "source_branch": "f", "target_branch": "test", "sha": "abc"},
                      ("PUT", BASE + "/merge_requests/3/merge"): {"iid": 3, "state": "merged"}})
        p = gitlab.plan(gl, "gitlab_mr_merge", {"iid": 3, "squash": True}, ["grp/app"])
        self.assertEqual([(x["m"], x["path"]) for x in p["steps"]], [("GET", "/projects/grp%2Fapp/merge_requests/3"),
                                                                     ("PUT", "/projects/grp%2Fapp/merge_requests/3/merge")])
        self.assertEqual(gitlab.execute(gl, p)["state"], "merged")
        p2 = gitlab.plan(gl, "gitlab_mr_merge", {"iid": 3}, ["grp/app"])
        self.op.routes[("GET", BASE + "/merge_requests/3")] = dict(self.op.routes[("GET", BASE + "/merge_requests/3")],
                                                                   target_branch="master")
        gl2 = gl
        with self.assertRaisesRegex(gitlab.GitLabError, "now targets"):   # retargeted while the card waited
            gitlab.execute(gl2, p2)

    def test_comment_edit_in_place(self):
        gl = self.gl({("GET", BASE + "/issues/5/notes/7"): {"id": 7, "body": "old text"},
                      ("PUT", BASE + "/issues/5/notes/7"): {"id": 7}})
        p = gitlab.plan(gl, "gitlab_comment", {"kind": "issue", "iid": 5, "body": "new text", "note_id": 7}, ["grp/app"])
        self.assertEqual([(x["m"], x["path"], x["body"]) for x in p["steps"]],
                         [("PUT", "/projects/grp%2Fapp/issues/5/notes/7", {"body": "new text"})])
        self.assertIn("old text", gitlab.card_text(p))

    def test_issues_mine_first(self):
        me = {"username": "me", "name": "Me"}
        gl = self.gl({("GET", "/api/v4/user"): me,
                      ("GET", BASE + "/issues"): [
                          {"iid": 1, "project_id": 5, "title": "other", "assignees": [], "description": "@meadow"},
                          {"iid": 2, "project_id": 5, "title": "cc", "assignees": [], "description": "fyi @me."},
                          {"iid": 3, "project_id": 5, "title": "mine", "assignees": [me], "description": ""},
                          {"iid": 4, "project_id": 5, "title": "comment", "assignees": [], "description": ""}],
                      ("GET", "/api/v4/todos"): [{"action_name": "mentioned", "target": {"iid": 4}},
                                                 {"action_name": "assigned", "target": {"iid": 1}}]})
        out = gitlab.call(gl, "gitlab_issues", {}, ["grp/app"], hidden)["issues"]
        self.assertEqual([(r["iid"], r.get("mine")) for r in out],
                         [(2, ["mentioned"]), (3, ["assigned"]), (4, ["mentioned"]), (1, None)])
        self.assertNotIn("description", out[0])

    def test_issues_by_milestone_and_page(self):
        """The changelog recipe: every issue of a milestone, the closed ones too, page by page."""
        rows = [{"iid": i, "title": "t%d" % i, "state": "closed", "milestone": {"id": 7, "title": "10.17.1"}}
                for i in range(50)]
        gl = self.gl({("GET", "/api/v4/user"): {"message": "forbidden"}, ("GET", BASE + "/issues"): rows})
        out = gitlab.call(gl, "gitlab_issues", {"milestone": "10.17.1", "state": "all", "per_page": 50, "page": 2},
                          ["grp/app"], hidden)
        issues = lambda: next(q for m, path, q, _ in self.op.sent if path.endswith("/issues"))  # noqa: E731
        self.assertEqual(issues(), {"state": "all", "milestone": "10.17.1", "per_page": "50", "page": "2"})
        self.assertEqual(out["issues"][0], {"iid": 0, "title": "t0", "state": "closed", "milestone": "10.17.1"})
        self.assertEqual((out["page"], out["next_page"]), (2, 3))
        gl = self.gl({("GET", "/api/v4/user"): {"message": "forbidden"}, ("GET", BASE + "/issues"): rows[:3]})
        out = gitlab.call(gl, "gitlab_issues", {"labels": "Kritische Änderung"}, ["grp/app"], hidden)
        self.assertEqual(issues(), {"state": "opened", "labels": "Kritische Änderung", "per_page": "20", "page": "1"})
        self.assertNotIn("next_page", out)
        with self.assertRaises(gitlab.GitLabError):
            gitlab.call(gl, "gitlab_issues", {"milestone": "10.17.1\nx"}, ["grp/app"], hidden)
        # with a working token an enumeration keeps the page as it is: no assigned issues appended
        # (they would repeat on the next page), the `mine` flags stay
        me = {"username": "me", "name": "Me"}
        rows[1]["assignees"] = [me]
        gl = self.gl({("GET", "/api/v4/user"): me, ("GET", BASE + "/issues"): rows, ("GET", "/api/v4/todos"): []})
        out = gitlab.call(gl, "gitlab_issues", {"milestone": "10.17.1", "state": "all", "per_page": 50, "page": 2},
                          ["grp/app"], hidden)
        self.assertEqual(len(out["issues"]), 50)
        self.assertEqual([r["iid"] for r in out["issues"][:2]], [1, 0])
        self.assertEqual(out["issues"][0]["mine"], ["assigned"])
        self.assertFalse([q for _, path, q, _ in self.op.sent if path.endswith("/issues") and "assignee_username" in q])

    def test_a_non_numeric_page_is_a_clear_tool_error_before_any_request(self):
        """N3 (2026-10-09): page "x" answered the agent with Python's "invalid literal for int() with base 10"."""
        gl = self.gl({("GET", "/api/v4/user"): {"message": "forbidden"}, ("GET", BASE + "/issues"): [],
                      ("GET", BASE + "/milestones"): []})
        for tool, args in (("gitlab_issues", {"page": "x"}), ("gitlab_milestones", {"page": "x"}),
                           ("gitlab_issues", {"per_page": "many"}), ("gitlab_milestones", {"page": [2]}),
                           ("gitlab_issue", {"iid": "abc"})):
            with self.subTest(tool=tool, args=args):
                n = len(self.op.sent)
                with self.assertRaises(ValueError) as e:
                    gitlab.call(gl, tool, args, ["grp/app"], hidden)
                self.assertNotIn("invalid literal", str(e.exception))
                self.assertRegex(str(e.exception), r"^(page|per_page|iid) must be a (positive )?whole number")
                self.assertEqual(len(self.op.sent), n)

    def test_a_triage_page_with_appended_assigned_issues_offers_no_next_page(self):
        """Page 2 runs without triage and would repeat the appended issues; an enumeration starts with page 1."""
        me = {"username": "me", "name": "Me"}
        rows = [{"iid": i, "title": "t%d" % i, "assignees": [], "description": ""} for i in range(20)]
        routes = {("GET", "/api/v4/user"): me, ("GET", "/api/v4/todos"): []}
        op = FakeOpener(routes)

        def opener(req, timeout=None):
            u = urllib.parse.urlsplit(req.full_url)
            if u.path.endswith("/issues") and "assignee_username" in urllib.parse.parse_qs(u.query):
                return io.BytesIO(json.dumps([{"iid": 30, "title": "mine", "assignees": [me], "description": ""}]).encode())
            if u.path.endswith("/issues"):
                return io.BytesIO(json.dumps(rows).encode())
            return op(req, timeout)

        gl = gitlab.GitLab("gitlab.example", "tok", opener=opener)
        out = gitlab.call(gl, "gitlab_issues", {}, ["grp/app"], hidden)
        self.assertEqual(len(out["issues"]), 21)
        self.assertEqual(out["issues"][0]["iid"], 30)
        self.assertNotIn("next_page", out)
        out = gitlab.call(gl, "gitlab_issues", {"page": 1}, ["grp/app"], hidden)
        self.assertEqual((len(out["issues"]), out["next_page"]), (20, 2))

    def test_milestones_of_the_project_and_its_groups(self):
        gl = self.gl({("GET", BASE + "/milestones"): [
            {"id": 7, "iid": 3, "title": "10.8.3", "description": "Engine 2025.5", "state": "closed",
             "due_date": "2026-09-01", "web_url": "https://gitlab.example/m/3", "project_id": 95, "expired": True}]})
        out = gitlab.call(gl, "gitlab_milestones", {"search": "10.8", "state": "closed"}, ["grp/app"], hidden)
        self.assertEqual(self.op.sent[-1][2], {"state": "closed", "search": "10.8", "include_ancestors": "true",
                                               "per_page": "20", "page": "1"})
        self.assertEqual(out["milestones"], [{"id": 7, "iid": 3, "title": "10.8.3", "description": "Engine 2025.5",
                                              "state": "closed", "due_date": "2026-09-01",
                                              "web_url": "https://gitlab.example/m/3"}])
        self.assertEqual(out["page"], 1)
        self.assertNotIn("next_page", out)
        gl = self.gl({("GET", BASE + "/milestones"): [{"id": i, "title": "10.%d" % i} for i in range(2)]})
        out = gitlab.call(gl, "gitlab_milestones", {"per_page": 2, "page": 3}, ["grp/app"], hidden)
        self.assertEqual((self.op.sent[-1][2]["page"], out["page"], out["next_page"]), ("3", 3, 4))
        # a milestone title is the author's text as an issue title is: no bidi controls (review 4)
        gl = self.gl({("GET", BASE + "/milestones"): [{"id": 1, "title": "10.8 \u202egpj.exe\u202c"}]})
        out = gitlab.call(gl, "gitlab_milestones", {"per_page": -5}, ["grp/app"], hidden)
        self.assertEqual(out["milestones"][0]["title"], "10.8 gpj.exe")
        self.assertEqual(self.op.sent[-1][2]["per_page"], "1")

    def test_a_read_timeout_or_a_non_json_answer_is_a_gitlab_error(self):
        class Slow(io.BytesIO):
            def read(self, *a):
                raise TimeoutError("timed out")

        for body in (Slow(), io.BytesIO(b"<html>502 Bad Gateway</html>")):
            gl = gitlab.GitLab("gitlab.example", "tok", opener=lambda req, timeout=None, b=body: b)
            with self.assertRaises(gitlab.GitLabError):
                gitlab.call(gl, "gitlab_issues", {}, ["grp/app"], hidden)

    def test_an_answer_of_the_wrong_shape_is_a_gitlab_error(self):
        err = {"message": "403 Forbidden"}
        cases = [("gitlab_issues", ("GET", BASE + "/issues")), ("gitlab_mrs", ("GET", BASE + "/merge_requests")),
                 ("gitlab_milestones", ("GET", BASE + "/milestones")),
                 ("gitlab_pipelines", ("GET", BASE + "/pipelines")), ("gitlab_issue", ("GET", BASE + "/issues/1")),
                 ("gitlab_mr_changes", ("GET", BASE + "/merge_requests/1/diffs"))]
        for tool, route in cases:
            body = ["not an object"] if tool == "gitlab_issue" else err
            with self.assertRaises(gitlab.GitLabError, msg=tool):
                gitlab.call(self.gl({route: body, ("GET", BASE + "/issues/1/notes"): []}), tool, {"iid": 1},
                            ["grp/app"], hidden)
        for fn, route, extra in ((gitlab.latest_pipeline, "/pipelines", "main"),
                                 (gitlab.open_mr, "/merge_requests", "feat")):
            with self.assertRaises(gitlab.GitLabError, msg=fn.__name__):
                fn(self.gl({("GET", BASE + route): err}), "grp/app", extra, ["grp/app"])
            self.assertIsNone(fn(self.gl({("GET", BASE + route): ["x"]}), "grp/app", extra, ["grp/app"]))
        for d, notes in ((["x"], []), ({"iid": 1}, err)):
            with self.assertRaises(gitlab.GitLabError, msg=(d, notes)):
                gitlab.item_preview(self.gl({("GET", BASE + "/issues/1"): d, ("GET", BASE + "/issues/1/notes"): notes}),
                                    "grp/app", "issue", 1, ["grp/app"])

    def test_titles_and_labels_lose_bidi_controls(self):
        """Review 2026-10-07 (E25): issue/MR titles, labels and hierarchy rows reached the GitLab view and the
        tools with RLO/LRI intact."""
        out = gitlab._shape({"iid": 1, "title": "fix \u202elogin\u202c", "labels": ["a\u2066b"],
                             "milestone": {"title": "m\u200f1"}, "author": {"username": "u", "name": "x\u202ey"}},
                            gitlab.ISSUE_KEYS)
        self.assertEqual((out["title"], out["labels"], out["milestone"], out["author"]), ("fix login", ["ab"], "m1", "xy"))

        class G:
            def graphql(self, q, v):
                node = {"iid": "2", "title": "p\u202eq", "state": "OPEN", "workItemType": {"name": "Epic"}, "webUrl": "u"}
                return {"project": {"workItems": {"nodes": [{"widgets": [
                    {"parent": node, "children": {"nodes": [dict(node, title="c\u2067d")]}}]}]}}}
        h = gitlab._hierarchy(G(), "grp/app", 1)
        self.assertEqual((h["parent"]["title"], h["children"][0]["title"]), ("pq", "cd"))

    def test_mr_changes_reads_every_page(self):
        calls = []

        def opener(req, timeout=None):
            page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query).get("page", ["1"])[0])
            calls.append(page)
            n = 100 if page == 1 else 30 if page == 2 else 0
            return io.BytesIO(json.dumps([{"old_path": "f%d_%d" % (page, i), "new_path": "f%d_%d" % (page, i),
                                           "diff": "+"} for i in range(n)]).encode())

        out = gitlab.call(gitlab.GitLab("gitlab.example", "tok", opener=opener), "gitlab_mr_changes", {"iid": 3},
                          ["grp/app"], hidden)
        self.assertEqual(len(out["files"]), 130)
        self.assertEqual(calls, [1, 2])
        self.assertFalse(out.get("truncated"))

    def test_no_token(self):
        gl = gitlab.GitLab("gitlab.example", None, opener=FakeOpener({}))
        with self.assertRaises(gitlab.GitLabError) as e:
            gitlab.call(gl, "gitlab_issues", {}, ["grp/app"], hidden)
        self.assertEqual(e.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
