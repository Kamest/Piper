"""W23 GitHub in the inbox (inbox.py): gh: keys, the session task, collecting notifications / issues /
search rows into items, and that a module that is off hides its forge's rows and counts."""
import threading
import unittest
from unittest import mock

import _env  # noqa: F401
import feed
import inbox


def mods(gitlab=True, github=True):
    return mock.patch.object(inbox.features, "on", side_effect=lambda m: {"gitlab": gitlab, "github": github}.get(m, True))


class KeyTest(unittest.TestCase):
    def test_parse_key_takes_github_paths(self):
        self.assertEqual(inbox.parse_key("gh:kamest/app#mr#7"), ("gh:kamest/app", "mr", 7))
        self.assertEqual(inbox.parse_key("gh:ghe.example.com/team/app#issue#3"), ("gh:ghe.example.com/team/app", "issue", 3))
        self.assertEqual(inbox.parse_key("grp/sub/app#issue#3"), ("grp/sub/app", "issue", 3))
        for bad in ("gh:a/b/c/d#issue#1", "gh:app#issue#1", "gh:../x#issue#1", "gh:a/b#pr#1"):
            with self.assertRaises(ValueError, msg=bad):
                inbox.parse_key(bad)

    def test_is_gh_and_entry(self):
        self.assertTrue(inbox.is_gh("gh:kamest/app"))
        self.assertFalse(inbox.is_gh("grp/app"))
        self.assertEqual(inbox.gh_entry("gh:ghe.example.com/team/app"), "ghe.example.com/team/app")


class TaskTest(unittest.TestCase):
    def test_github_pr_task_names_the_github_tool_and_draft(self):
        t = inbox.task_for({"type": "mr", "iid": 7, "gl": "gh:kamest/app", "title": "Fix", "web_url": "https://github.com/kamest/app/pull/7"},
                           "work")
        self.assertIn("GitHub pull request #7: Fix", t)
        self.assertIn('github_pr (repo "kamest/app", number 7)', t)
        self.assertIn("development/github-pr-7-comment.md", t)
        self.assertNotIn("gitlab", t.lower())

    def test_github_issue_analyze_and_note(self):
        it = {"type": "issue", "iid": 3, "gl": "gh:kamest/app", "title": "Bug", "web_url": ""}
        self.assertIn("development/github-3-analysis.md", inbox.task_for(it, "analyze"))
        t = inbox.task_for(dict(it, type="mr"), "work", note_id=55)
        self.assertIn("note_55", t)
        self.assertIn("pull request", t)

    def test_gitlab_task_is_unchanged(self):
        t = inbox.task_for({"type": "mr", "iid": 4, "gl": "grp/app", "title": "X", "web_url": ""}, "work")
        self.assertIn('gitlab_mr (project "grp/app", iid 4)', t)
        self.assertIn("gitlab-mr-4-comment.md", t)


class CollectTest(unittest.TestCase):
    def test_rows_and_notifications_merge_into_lower_case_items(self):
        assigned = [{"number": 3, "title": "Bug", "state": "open", "repository_url": "https://api.github.com/repos/Kamest/App",
                     "html_url": "https://github.com/Kamest/App/issues/3", "labels": [{"name": "bug"}], "comments": 2,
                     "user": {"login": "anna"}}]
        reviews = [{"number": 9, "title": "PR", "state": "open", "pull_request": {"url": "x"},
                    "repository_url": "https://api.github.com/repos/Kamest/App", "html_url": "https://github.com/Kamest/App/pull/9"}]
        notifs = [{"id": "77", "reason": "mention", "updated_at": "2026-10-12T10:00:00Z",
                   "subject": {"type": "Issue", "title": "Bug", "url": "https://api.github.com/repos/Kamest/App/issues/3",
                               "latest_comment_url": "https://api.github.com/repos/Kamest/App/issues/comments/555"}},
                  {"id": "78", "reason": "subscribed", "subject": {"type": "Release", "url": "https://api.github.com/repos/Kamest/App/releases/1"}},
                  {"id": "79", "reason": "author", "updated_at": "2026-10-12T11:00:00Z",
                   "subject": {"type": "PullRequest", "title": "Only here", "url": "https://api.github.com/repos/Kamest/App/pulls/12"}}]
        items = inbox.Inbox._ib_collect_gh("github.com", notifs, assigned, reviews, [])
        self.assertEqual(sorted(items), ["gh:kamest/app#issue#3", "gh:kamest/app#mr#12", "gh:kamest/app#mr#9"])
        bug = items["gh:kamest/app#issue#3"]
        self.assertEqual((bug["forge"], bug["reasons"], bug["notes_count"], bug["labels"]), ("github", ["assigned", "mentioned"], 2, ["bug"]))
        self.assertEqual(bug["todos"], [{"id": "77", "action": "mentioned", "author": None, "created_at": "2026-10-12T10:00:00Z",
                                         "note_id": 555, "body": ""}])
        self.assertEqual(items["gh:kamest/app#mr#9"]["reasons"], ["review_requested"])
        only = items["gh:kamest/app#mr#12"]   # a notification alone: its title, a PR url built from the repo
        self.assertEqual((only["title"], only["web_url"]), ("Only here", "https://github.com/Kamest/App/pull/12"))

    def test_ghe_items_carry_the_host_and_bad_rows_are_skipped(self):
        rows = [{"number": 1, "title": "A", "repository_url": "https://ghe.example.com/api/v3/repos/team/app",
                 "html_url": "javascript:alert(1)"},
                {"number": "x", "repository_url": "https://ghe.example.com/api/v3/repos/team/app"},
                {"number": 2, "title": "no repo"}]
        items = inbox.Inbox._ib_collect_gh("ghe.example.com", [], rows, [], [])
        self.assertEqual(list(items), ["gh:ghe.example.com/team/app#issue#1"])
        self.assertEqual(items["gh:ghe.example.com/team/app#issue#1"]["web_url"], "https://ghe.example.com/team/app/issues/1")


class ModuleTest(unittest.TestCase):
    def make(self):
        ib = object.__new__(inbox.Inbox)
        ib._ib_lock = threading.RLock()
        ib._ib_items = {"grp/app#issue#1": {"key": "grp/app#issue#1", "todos": [1]},
                        "gh:kamest/app#mr#2": {"key": "gh:kamest/app#mr#2", "forge": "github", "todos": []}}
        ib._ib_state = {"items": {}}
        ib._ib_status = {"status": "ok", "at": 1, "error": None, "me": "alice"}
        ib._ib_gh_status = {"status": "no-token", "at": None, "error": None, "me": None}
        ib._ib_v = 3
        ib._ib_rules = []
        ib._ib_map = lambda refresh=False: ({}, {})
        ib._ib_linked = lambda: {}
        ib._ib_view = lambda it, m, c, ln: dict(it)
        ib._ib_order = lambda v: v["key"]
        ib._ib_used_hour = lambda: 0
        return ib

    def test_both_on(self):
        ib = self.make()
        with mods():
            self.assertEqual([v["key"] for v in ib._ib_views()], ["gh:kamest/app#mr#2", "grp/app#issue#1"])
            s = ib.inbox_summary()
        self.assertEqual((s["status"], s["total"], s["needs"], s["github"]), ("ok", 2, 1, "no-token"))

    def test_gitlab_off_leaves_github_rows_and_says_off(self):
        ib = self.make()
        with mods(gitlab=False):
            self.assertEqual([v["key"] for v in ib._ib_views()], ["gh:kamest/app#mr#2"])
            s = ib.inbox_summary()
            full = ib.inbox()
        self.assertEqual((s["status"], s["total"], s["needs"]), ("off", 1, 0))
        self.assertEqual((full["status"], full["me"], full["github"]["status"]), ("off", None, "no-token"))

    def test_github_off_hides_its_rows_and_half(self):
        ib = self.make()
        with mods(github=False):
            self.assertEqual([v["key"] for v in ib._ib_views()], ["grp/app#issue#1"])
            s = ib.inbox_summary()
            full = ib.inbox()
        self.assertEqual((s["total"], s["github"], full["github"]), (1, None, None))


class MentionTest(unittest.TestCase):
    """W10 for GitHub: an issue / PR a prompt names links an unlinked session."""
    def test_gh_mention_key(self):
        sc = ["Kamest/App", "ghe.example.com/team/app"]
        k = inbox.gh_mention_key
        self.assertEqual(k("see https://github.com/kamest/app/pull/12 please", sc), ("gh:kamest/app#mr#12", True))
        self.assertEqual(k("https://ghe.example.com/team/app/issues/4", sc), ("gh:ghe.example.com/team/app#issue#4", True))
        self.assertIsNone(k("https://github.com/team/app/issues/4", sc), "github.com/team/app is not the GHE repo")
        self.assertIsNone(k("https://evil.example/kamest/app/issues/4", sc))
        self.assertEqual(k("fix Kamest/App#7 now", sc), ("gh:kamest/app#issue#7", False))
        self.assertIsNone(k("fix other/repo#7", sc))
        self.assertIsNone(k("work on #42", sc), "a bare #N needs exactly one repository")
        self.assertEqual(k("work on #42", ["Kamest/App"]), ("gh:kamest/app#issue#42", False))
        self.assertIsNone(k("work on #42", ["Kamest/App"], bare=False), "a GitLab project takes the bare #N")
        self.assertIsNone(k("step #2 and !42", ["Kamest/App"]), "a one-digit #N is prose, !N is GitLab's")

    def make(self, items=None, gitlab=(), gh_scope=("Kamest/App",)):
        import types
        ib = object.__new__(inbox.Inbox)
        ib._ib_lock = threading.RLock()
        ib._ib_items = items or {}
        ib._ib_probes = set()
        ib.gh_scope = lambda s: list(gh_scope)
        ib.linked = []
        ib._link_issue = lambda s, issue: ib.linked.append(issue)
        s = types.SimpleNamespace(id="s1", issue=None, kind="project", gitlab=list(gitlab))
        return ib, s

    def test_a_pr_the_inbox_knows_links_from_a_bare_number(self):
        it = {"key": "gh:kamest/app#mr#42", "gl": "gh:kamest/app", "type": "mr", "iid": 42, "title": "Fix", "web_url": None}
        ib, s = self.make({it["key"]: it})
        with mods(gitlab=False), mock.patch.object(threading, "Thread") as th:
            ib.issue_from_prompt(s, "review #42 please")
        self.assertEqual([x["key"] for x in ib.linked], ["gh:kamest/app#mr#42"])
        self.assertEqual(ib.linked[0]["forge"], "github")
        th.assert_not_called()

    def test_an_unknown_one_is_probed_and_github_decides_the_kind(self):
        ib, s = self.make()
        gh = mock.Mock()
        gh._req.return_value = {"number": 42, "title": "PR", "pull_request": {"url": "x"},
                                "html_url": "https://github.com/Kamest/App/pull/42"}
        ib._ib_gh = mock.Mock(return_value=(gh, "Kamest/App"))
        with mods(), mock.patch.object(threading, "Thread") as th:
            ib.issue_from_prompt(s, "https://github.com/Kamest/App/issues/42")
            ib.issue_from_prompt(s, "https://github.com/Kamest/App/issues/42")   # asked once
        self.assertEqual(th.call_count, 1)
        th.call_args.kwargs["target"](*th.call_args.kwargs["args"])
        gh._req.assert_called_once_with("GET", "/repos/Kamest/App/issues/42")
        self.assertEqual((ib.linked[0]["key"], ib.linked[0]["type"], ib.linked[0]["web_url"]),
                         ("gh:kamest/app#mr#42", "mr", "https://github.com/Kamest/App/pull/42"))

    def test_gitlab_first_and_module_off(self):
        ib, s = self.make(gitlab=["grp/app"])
        with mods(), mock.patch.object(threading, "Thread") as th:
            ib.issue_from_prompt(s, "do #42")   # GitLab's single project takes the bare number
        self.assertEqual(th.call_args.kwargs["args"][1], "grp/app#issue#42")
        ib, s = self.make()
        with mods(github=False), mock.patch.object(threading, "Thread") as th:
            ib.issue_from_prompt(s, "https://github.com/Kamest/App/issues/42")
        th.assert_not_called()


class FactsTest(unittest.TestCase):
    def facts(self, ci):
        import types
        import gitops
        import issuestate
        m = types.SimpleNamespace(_top=mock.Mock(side_effect=gitops.GitError("no git")), git_ci=lambda key: ci,
                                  project_hidden=lambda p: (lambda rel: False))
        s = types.SimpleNamespace(id="s1", issue={"key": "gh:kamest/app#issue#3", "gl": "gh:kamest/app", "type": "issue",
                                                  "iid": 3, "title": "Bug", "web_url": None})
        return issuestate.IssueState._istate_facts(m, s, {"key": "app", "path": _env.HOME})

    def test_pr_line_is_hash_on_github_and_bang_on_gitlab(self):
        mr = {"iid": 7, "title": "Fix", "web_url": "https://x/7"}
        self.assertIn("- PR: #7 Fix (https://x/7)", self.facts({"github": "kamest/app", "mr": mr}))
        self.assertIn("- MR: !7 Fix (https://x/7)", self.facts({"mr": mr}))


class FeedTest(unittest.TestCase):
    def test_subject_ref_is_hash_for_a_github_pr(self):
        self.assertEqual(feed._subject_issue({"key": "gh:kamest/app#mr#7", "type": "mr", "iid": 7, "forge": "github",
                                              "gl": "gh:kamest/app", "project": "app"})["label"], "app #7")
        self.assertEqual(feed._subject_issue({"key": "grp/app#mr#7", "type": "mr", "iid": 7, "gl": "grp/app",
                                              "project": "app"})["label"], "app !7")


if __name__ == "__main__":
    unittest.main()
