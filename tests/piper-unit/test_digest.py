"""digest.py directly: last turns and token usage from a Claude Code transcript (session_digest, Usage)."""
import json
import os
import shutil
import tempfile
import unittest

import _env  # noqa: F401
import digest


class DigestTest(unittest.TestCase):
    def test_title_reference_is_scoped_to_the_users_request(self):
        scopes = [("gitlab", "gitlab.example.com", "acme/shop")]
        for prompt, expected in (
                ("Fix issue #123", ("acme/shop", "issue", 123)),
                ("#123", ("acme/shop", "issue", 123)),
                ("Review MR !7", ("acme/shop", "mr", 7)),
                ("Fix https://gitlab.example.com/acme/other/-/issues/123#note_9", ("acme/other", "issue", 123)),
                ("Fix invoice export\n\nhttps://gitlab.example.com/acme/shop/-/issues/123", ("acme/shop", "issue", 123)),
                ("Fix https://gitlab.example.com/acme/shop/-/issues/123 and issue #123", ("acme/shop", "issue", 123)),
                ("Fix #123 and #456", None),
                ("Compare https://gitlab.example.com/acme/shop/-/issues/123 and https://gitlab.example.com/acme/other/-/issues/123", None),
                ("Fix https://untrusted.example.com/acme/shop/-/issues/123", None),
                ("Fix https://user:pass@gitlab.example.com/acme/shop/-/issues/123", None),
                ("Fix https://gitlab.example.com/acme/../shop/-/issues/123", None),
                ("Fix https://github.com/acme/shop/issues/123", None),
                ("Fix https://gitlab.example.com/acme/shop/-/issues/0", None),
                ("Use 123 records", None),
                ("Fix export\n\nPasted issue #123", None),
                ("Fix export\n```log\nissue #123\n```", None),
                ("Fix export\n> issue #123", None),
                ("Fix export\n<environment_context>issue #123</environment_context>", None),
                ("<task-notification>Fix issue #123</task-notification>", None)):
            with self.subTest(prompt=prompt):
                ref = digest.title_reference(prompt, scopes)
                self.assertEqual((ref["project"], ref["type"], ref["iid"]) if ref else None, expected)
        self.assertIsNone(digest.title_reference("Fix issue #123", scopes + [("github", "github.com", "acme/shop")]))
        self.assertEqual(digest.title_reference("https://github.com/acme/shop/pull/7",
                         [("github", "github.com", "acme/shop")])["type"], "mr")

    def test_title_labels_ignore_context_and_keep_readable_scrubbed_words(self):
        for prompt in ("Ahoj", "dobrý den", "hello!", "Otestuj to", "/compact", "https://example.com", "# AGENTS.md instructions\nFix export"):
            self.assertEqual(digest.title_label(prompt), "", prompt)
        self.assertEqual(digest.title_label("Fix export\n```\nWrong task\n```"), "Fix export")
        self.assertEqual(digest.title_label("Fix export\n\nPasted task"), "Fix export")
        title = digest.title_label("Fix ‮export to\x00ken=PLANTED_T " + "longer words " * 100)
        self.assertNotIn("PLANTED_T", title)
        self.assertNotIn("\x00", title)
        self.assertNotIn("‮", title)
        self.assertLessEqual(len(title), 70)
        self.assertTrue(title.endswith("…"))

    def test_codex_question_text_is_explicit_bounded_and_scrubbed(self):
        def row(name, arguments):
            return {"type": "response_item", "payload": {"type": "function_call", "name": name,
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments)}}
        async_question = row("request_user_input_async", {"questions": [{"title": "Choose an environment", "options": ["Test", "Dev"]}]})
        result = digest.readable_record(async_question, "codex-conversation", "codex")
        self.assertIsNotNone(result)
        self.assertEqual(result["role"], "assistant")
        self.assertIn("Choose an environment", result["text"])
        self.assertIn("- Test", result["text"])
        standard = row("request_user_input", {"questions": [{"question": "Proceed? token=PLANTED_T",
                       "options": [{"label": "Yes", "description": "Continue"}]}]})
        result = digest.readable_record(standard, "codex-conversation", "codex")
        self.assertIn("Yes", result["text"])
        self.assertIn("Continue", result["text"])
        self.assertNotIn("PLANTED_T", result["text"])
        for name, arguments in [("exec_command", {"questions": [{"title": "HIDDEN"}]}),
                                ("request_user_input_async", "{broken"), ("request_user_input_async", "[]"),
                                ("request_user_input_async", {"questions": "HIDDEN"}),
                                ("request_user_input_async", {"questions": [None, {"title": 1}]})]:
            self.assertIsNone(digest.readable_record(row(name, arguments), "codex-conversation", "codex"))
        large = row("request_user_input_async", {"questions": [{"title": "q" * 5000, "options": ["v" * 5000] * 100}] * 100})
        self.assertIsNone(digest.readable_record(large, "codex-conversation", "codex"))

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cfg, True)
        self.dir = os.path.join(self.cfg, "projects", "-work")
        os.makedirs(self.dir)

    def write(self, sid, recs, raw_tail=""):
        with open(os.path.join(self.dir, sid + ".jsonl"), "w") as f:
            f.write("\n".join(json.dumps(r) for r in recs) + "\n" + raw_tail)

    def test_conversation_pages_reach_all_4000_messages_and_keep_absolute_cursor(self):
        records = [{"type": "user", "message": {"content": "Message %d" % i}} for i in range(4000)]
        self.write("pages", records)
        page = digest.digest(self.cfg, "pages", turns=400, max_turns=400, page=True)
        self.assertEqual((page["total"], page["offset"], page["has_more"]), (4000, 3600, True))
        seen = list(page["turns"])
        # A new message cannot shift an older page's cursor and duplicate a turn.
        self.write("pages", records + [{"type": "assistant", "message": {"content": "New answer"}}])
        while page["has_more"]:
            page = digest.digest(self.cfg, "pages", turns=400, max_turns=400, before=page["offset"], page=True)
            seen = page["turns"] + seen
        self.assertEqual(len(seen), 4000)
        self.assertEqual([r["text"] for r in seen], ["Message %d" % i for i in range(4000)])
        self.assertEqual(page["total"], 4001)

    def test_turns_are_text_only_scrubbed_and_bounded(self):
        recs = [{"type": "user", "message": {"content": "fix it"}, "timestamp": "t1"},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"},
                                                              {"type": "tool_use", "name": "Edit"}]}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "secret file"}]}},
                {"type": "summary", "summary": "x"},
                {"type": "assistant", "message": {"content": "token=PLANTED_T " + "x" * 3000}}]
        self.write("s1", recs, raw_tail="{not json\n[1,2]\n")
        out = digest.digest(self.cfg, "s1")
        self.assertEqual([o["role"] for o in out], ["user", "assistant", "assistant"])
        self.assertEqual(out[1]["text"], "done\n[tool Edit]")
        self.assertNotIn("PLANTED_T", out[2]["text"])
        self.assertLessEqual(len(out[2]["text"]), digest.MAX_CHARS + 20)   # "[scrubbed]" may lengthen it
        self.assertEqual(len(digest.digest(self.cfg, "s1", turns=1)), 1)
        self.assertEqual(len(digest.digest(self.cfg, "s1", turns=10 ** 6)), 3)

    def test_tools_detail_names_args_time_and_line_count_never_the_result(self):
        """F05 (9.77): the Conversation's tool rows - argument, run time, result lines; the text keeps its markers."""
        recs = [{"type": "assistant", "timestamp": "2026-10-20T10:00:00Z", "message": {"content": [
                    {"type": "text", "text": "running"},
                    {"type": "tool_use", "id": "u1", "name": "Bash", "input": {"command": "mvn  -q test\n -pl core token=PLANTED_T"}},
                    {"type": "tool_use", "id": "u2", "name": "Read", "input": {"file_path": "/w/src/SearchIndex.java"}},
                    {"type": "tool_use", "id": "u3", "name": "mcp__x__y", "input": {"n": 1, "q": "find me"}},
                    {"type": "tool_use", "id": "u4", "name": "Edit", "input": {"file_path": "/w/a.py"}}]}},
                {"type": "user", "timestamp": "2026-10-20T10:00:01.200Z", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "u1", "content": "SECRET_RESULT\nok", "is_error": True},
                    {"type": "tool_result", "tool_use_id": "u2", "content": [{"type": "text", "text": "a\nb\nc"}]},
                    {"type": "tool_result", "tool_use_id": "u3", "content": "x"}]}}]
        self.write("s1", recs)
        out = digest.digest(self.cfg, "s1", tools=True)
        self.assertEqual(len(out), 1, "a record of several tool results is no turn")
        self.assertEqual(out[0]["text"], "running\n[tool Bash]\n[tool Read]\n[tool mcp__x__y]\n[tool Edit]")
        t = out[0]["tools"]
        self.assertEqual([x["name"] for x in t], ["Bash", "Read", "mcp__x__y", "Edit"])
        self.assertTrue(t[0]["arg"].startswith("mvn -q test -pl core"))
        self.assertNotIn("PLANTED_T", t[0]["arg"])
        self.assertEqual((t[0]["ms"], t[0]["lines"], t[0]["error"]), (1200, 2, True))
        self.assertEqual((t[1]["arg"], t[1]["lines"]), ("SearchIndex.java", 3))
        self.assertEqual(t[2]["arg"], "find me")
        self.assertEqual(t[3], {"name": "Edit", "arg": "a.py"})   # no result yet: no time, no internals
        self.assertNotIn("SECRET_RESULT", json.dumps(out))
        self.assertNotIn("tools", digest.digest(self.cfg, "s1")[0])   # the other callers are unchanged

    def test_transcript_choice_and_links(self):
        self.write("s1", [])
        self.write("s2", [])
        self.assertEqual(digest.transcript(self.cfg, "s1"), "projects/-work/s1.jsonl")
        self.assertIsNone(digest.transcript(self.cfg, "nope"))
        os.utime(os.path.join(self.dir, "s1.jsonl"), (1, 1))
        self.assertEqual(digest.transcript(self.cfg, None), "projects/-work/s2.jsonl")
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "x.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "PLANTED"}}) + "\n")
        os.symlink(os.path.join(outside, "x.jsonl"), os.path.join(self.dir, "s3.jsonl"))
        self.assertEqual(digest.digest(self.cfg, "s3"), [])
        os.symlink(outside, os.path.join(self.cfg, "projects", "-linked"))
        self.assertEqual(digest.digest(self.cfg, "x"), [])

    def test_usage_counts_each_message_once(self):
        u = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 1}
        self.write("s1", [{"type": "assistant", "timestamp": "2026-10-06T10:00:00Z", "message": {"id": "m1", "usage": u}},
                          {"type": "assistant", "timestamp": "2026-10-06T10:00:01Z", "message": {"id": "m1", "usage": u}},
                          {"type": "assistant", "timestamp": "bad", "message": {"id": "m2", "usage": dict(u, output_tokens="x")}}])
        self.assertEqual(digest.usage(self.cfg, "s1"),
                         {"input": 20, "output": 5, "cache_read": 200, "cache_write": 2, "messages": 2})
        seen = set()
        days = digest.usage_by_day(self.cfg, "s1", seen)
        self.assertEqual(sum(d["input"] for d in days.values()), 10)
        self.assertEqual(digest.usage_by_day(self.cfg, "s1", seen), {})   # shared `seen`: a resume repeats ids

    def test_codex_usage_uses_cumulative_deltas_and_counts_cached_input_once(self):
        sid = '12345678-1234-1234-1234-123456789abc'
        directory = os.path.join(self.cfg, 'sessions', '2026', '10', '06')
        os.makedirs(directory)
        records = [{'type': 'session_meta', 'payload': {'id': sid, 'source': 'exec'}}]
        for stamp, usage in (
                ('2026-10-06T12:00:00Z', {'input_tokens': 11, 'output_tokens': 7, 'cached_input_tokens': 2}),
                ('2026-10-06T12:00:00Z', {'input_tokens': 11, 'output_tokens': 7, 'cached_input_tokens': 2}),
                ('2026-10-07T12:00:00Z', {'input_tokens': 20, 'output_tokens': 12, 'cached_input_tokens': 5})):
            records.append({'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'token_count',
                'info': {'total_token_usage': usage, 'last_token_usage': usage}}})
        with open(os.path.join(directory, 'rollout-' + sid + '.jsonl'), 'w') as output:
            output.write('\n'.join(json.dumps(r) for r in records) + '\n')
        seen = set()
        days = digest.usage_by_day(self.cfg, sid, seen, agent='codex')
        self.assertEqual(days['2026-10-06'], {'input': 9, 'output': 7, 'cache_read': 2, 'cache_write': 0})
        self.assertEqual(days['2026-10-07'], {'input': 6, 'output': 5, 'cache_read': 3, 'cache_write': 0})
        self.assertEqual(digest.usage_by_day(self.cfg, sid, seen, agent='codex'), {})


if __name__ == "__main__":
    unittest.main()
