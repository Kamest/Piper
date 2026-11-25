"""digest.py directly: last turns and token usage from a Claude Code transcript (session_digest, Usage)."""
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_record_iteration_does_not_keep_decoded_copies_of_the_whole_transcript(self):
        import tracemalloc
        path = os.path.join(self.dir, 'large.jsonl')
        line = json.dumps({'type': 'user', 'message': {'content': 'x' * 4000}}).encode() + b'\n'
        with open(path, 'wb') as f:
            f.write(line * 4096)
        tracemalloc.start()
        records = digest._records(self.cfg, 'large')
        try:
            self.assertEqual(next(records)['message']['content'], 'x' * 4000)
            retained = tracemalloc.get_traced_memory()[0]
        finally:
            records.close()
            tracemalloc.stop()
        self.assertLess(retained, 20 << 20, 'the bounded raw buffer must not have a second whole-file text/line copy')

    def test_record_iteration_preserves_line_endings_unicode_and_invalid_utf8(self):
        raw = '{"n":1}\r\n{"text":"\u011b"}\r{"n":2}\n{"text":"a\u2028b"}\n'.encode() + b'{"text":"\xff"}\n{"n":3}'
        with open(os.path.join(self.dir, 'lines.jsonl'), 'wb') as f:
            f.write(raw)
        expected = []
        for line in raw.decode('utf-8', 'replace').splitlines():
            try:
                expected.append(json.loads(line))
            except ValueError:
                pass
        self.assertEqual(list(digest._records(self.cfg, 'lines')), expected)

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

    @staticmethod
    def zst(data: bytes) -> bytes:
        """A valid zstd frame of raw (stored) blocks: any decoder reads it, no compressor needed."""
        out = b"\x28\xb5\x2f\xfd" + bytes([0xa0]) + struct.pack("<I", len(data))   # single segment, 4-byte size
        chunks = [data[i:i + (128 << 10)] for i in range(0, len(data), 128 << 10)] or [b""]
        for n, chunk in enumerate(chunks):
            out += struct.pack("<I", (len(chunk) << 3) | (n == len(chunks) - 1))[:3] + chunk
        return out

    def rollout(self, sid, texts, compressed=True, name="rollout-2026-11-05T14-20-56-"):
        directory = os.path.join(self.cfg, "sessions", "2026", "11", "05")
        os.makedirs(directory, exist_ok=True)
        records = [{"type": "session_meta", "payload": {"id": sid, "source": "cli", "cwd": "/work"}}]
        records += [{"type": "response_item", "timestamp": "2026-11-05T14:21:00Z", "payload": {
            "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}}
            for role, text in texts]
        raw = ("\n".join(json.dumps(r) for r in records) + "\n").encode()
        path = os.path.join(directory, name + sid + ".jsonl" + (".zst" if compressed else ""))
        with open(path, "wb") as f:
            f.write(self.zst(raw) if compressed else raw)
        return os.path.relpath(path, self.cfg)

    def decoders(self):
        """Python 3.14's compression.zstd and the zstd command, each where this host has it."""
        out = []
        try:
            from compression import zstd   # noqa: F401
            out.append(("module", {}))
        except ImportError:
            pass
        if shutil.which("zstd"):
            out.append(("command", {"compression.zstd": None, "compression": None}))
        if not out:
            self.skipTest("neither compression.zstd nor the zstd command is available")
        return out

    def test_codex_compressed_rollout_is_found_validated_and_read(self):
        # 2026-11-25: Codex 0.167 compressed finished rollouts to .jsonl.zst; Resume said "The saved conversation
        # is missing" although the conversation was intact.
        sid = "01a1abde-9e42-7bb3-92a8-846cbdd0f8f3"
        rel = self.rollout(sid, [("user", "first question"), ("assistant", "first answer"), ("user", "second")])
        for label, modules in self.decoders():
            with self.subTest(decoder=label), mock.patch.dict(sys.modules, modules):
                shutil.rmtree(digest._zst_dir(), True)
                self.assertEqual(digest.session_file(self.cfg, sid, "codex"), rel)
                self.assertEqual(digest.validate_conversation(self.cfg, sid, "codex"), rel)
                self.assertEqual(digest.history_file_map(self.cfg, "codex"), {sid: rel})
                self.assertEqual([t["text"] for t in digest.codex_digest(self.cfg, sid)],
                                 ["first question", "first answer", "second"])
                newest = [digest.readable_record(r, sid, "codex") for r in digest.reverse_records(self.cfg, rel, 1)]
                self.assertEqual([r["text"] for r in newest if r][0], "second")
                ends = [end for end, _ in digest.iter_records(self.cfg, rel, 0, 1)]   # the file size does not bound it
                self.assertEqual(ends[-1], len(digest.read_bytes(self.cfg, rel, 1 << 30)))

    def test_codex_compressed_rollout_streams_every_frame_and_refuses_a_truncated_one(self):
        directory = os.path.join(self.cfg, "sessions")
        os.makedirs(directory, exist_ok=True)
        big = b"y" * (300 << 10)   # several raw blocks and more than one decoder chunk
        with open(os.path.join(directory, "two.jsonl.zst"), "wb") as f:
            f.write(self.zst(b"first\n") + self.zst(big + b"\n"))
        with open(os.path.join(directory, "cut.jsonl.zst"), "wb") as f:
            f.write(self.zst(b"first\n" * 1000)[:-100])
        for label, modules in self.decoders():
            with self.subTest(decoder=label), mock.patch.dict(sys.modules, modules):
                self.assertEqual(b"".join(digest._zst_chunks(self.cfg, "sessions/two.jsonl.zst")), b"first\n" + big + b"\n")
                self.assertEqual(digest.head_bytes(self.cfg, "sessions/two.jsonl.zst", 3), b"fir")
                with self.assertRaises(ValueError):
                    b"".join(digest._zst_chunks(self.cfg, "sessions/cut.jsonl.zst"))

    def test_codex_unreadable_compressed_rollout_is_an_oserror_and_decoded_once(self):
        # board, usage and feed skip unreadable transcripts with `except OSError`: one bad .zst must not stop them
        sid = "01a1abde-9e42-7bb3-92a8-846cbdd0f8f3"
        rel = self.rollout(sid, [("user", "question")])
        self.decoders()
        shutil.rmtree(digest._zst_dir(), True)
        digest._zst_failed.clear()
        with mock.patch.object(digest, "ZST_MAX", 16), \
                mock.patch.object(digest, "_zst_chunks", wraps=digest._zst_chunks) as chunks:
            for _ in range(3):
                with self.assertRaises(OSError):
                    list(digest.reverse_records(self.cfg, rel, 1))
                with self.assertRaisesRegex(ValueError, "too large"):
                    digest.validate_conversation(self.cfg, sid, "codex")
            self.assertEqual(chunks.call_count, 1)
            self.assertEqual(digest.codex_digest(self.cfg, sid), [])
        digest._zst_failed.clear()

    def test_codex_remembered_compressed_rollout_moves_to_its_plain_file(self):
        sid = "01a1abd5-5e93-7dc1-a8bb-fc5c5b5ed461"
        rel = self.rollout(sid, [("user", "old")])
        self.assertEqual(digest.current_rel(self.cfg, rel), rel)
        plain = self.rollout(sid, [("user", "old"), ("user", "resumed")], compressed=False)
        self.assertEqual(digest.current_rel(self.cfg, rel), plain)
        self.assertEqual(digest.current_rel(self.cfg, plain), plain)

    def test_codex_plain_rollout_wins_over_its_compressed_copy(self):
        sid = "01a1abd5-5e93-7dc1-a8bb-fc5c5b5ed461"
        self.rollout(sid, [("user", "old")])
        plain = self.rollout(sid, [("user", "old"), ("user", "resumed")], compressed=False)
        self.assertEqual(digest.session_file(self.cfg, sid, "codex"), plain)
        self.assertEqual(digest.history_file_map(self.cfg, "codex"), {sid: plain})
        self.assertEqual(sorted(digest._history_files(self.cfg, "codex")), [(plain, sid)])

    def test_codex_compressed_rollout_without_a_decoder_says_why(self):
        sid = "01a1abde-9e42-7bb3-92a8-846cbdd0f8f3"
        rel = self.rollout(sid, [("user", "question")])
        shutil.rmtree(digest._zst_dir(), True)
        with mock.patch.dict(sys.modules, {"compression.zstd": None, "compression": None}), \
                mock.patch.object(digest.shutil, "which", return_value=None):
            self.assertEqual(digest.session_file(self.cfg, sid, "codex"), rel)
            with self.assertRaisesRegex(ValueError, "compressed by Codex"):
                digest.validate_conversation(self.cfg, sid, "codex")

    def test_codex_compressed_rollout_is_bounded_and_never_followed_through_a_link(self):
        sid = "01a1abde-9e42-7bb3-92a8-846cbdd0f8f3"
        rel = self.rollout(sid, [("user", "question")])
        self.decoders()
        shutil.rmtree(digest._zst_dir(), True)
        with mock.patch.object(digest, "ZST_MAX", 16):
            with self.assertRaisesRegex(ValueError, "too large"):
                digest.read_bytes(self.cfg, rel)
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        target = os.path.join(outside, "planted.jsonl.zst")
        shutil.move(os.path.join(self.cfg, rel), target)
        os.symlink(target, os.path.join(self.cfg, rel))
        shutil.rmtree(digest._zst_dir(), True)
        with self.assertRaises(OSError):
            digest.read_bytes(self.cfg, rel)


if __name__ == "__main__":
    unittest.main()
