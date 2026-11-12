"""piper-mcp.py (runs inside the sandbox): JSON-RPC over stdio, tool calls via the mailbox."""
import json
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import _env  # noqa: F401

MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "piper", "sandbox", "piper-mcp.py")


class McpTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.box = os.path.join(self.home, ".claude", "piper-mailbox")
        os.makedirs(os.path.join(self.box, "in"))

    def run_mcp(self, role, msgs, responder=None, extra=()):
        p = subprocess.Popen([sys.executable, MCP, "--role", role, *extra], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env={"HOME": self.home, "PATH": os.environ["PATH"]})
        if responder:
            threading.Thread(target=responder, daemon=True).start()
        out, _ = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=20)
        return [json.loads(l) for l in out.splitlines()]

    def test_initialize_and_role_tools(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        r = self.run_mcp("session", [init, note, lst])
        self.assertEqual(r[0]["result"]["protocolVersion"], "2025-03-26")
        names = {t["name"] for t in r[1]["result"]["tools"]}
        self.assertIn("gitlab_issue", names)
        self.assertLessEqual({"properties_keys", "properties_set", "http_env_set"}, names)
        self.assertIn("gitlab_upload_file", names)
        upload = next(t for t in r[1]["result"]["tools"] if t["name"] == "gitlab_upload_file")
        self.assertEqual(upload["inputSchema"]["required"], ["path"])
        self.assertIn("note_id", upload["description"])
        self.assertNotIn("spawn_session", names)
        self.assertLessEqual({"propose_session", "report_result", "handoff_result"}, names)
        self.assertLessEqual({"git_run", "edit_file", "write_file"}, names)   # listed; piperd refuses them while locked
        self.assertLessEqual({"piper_session", "piper_settings", "piper_settings_change", "piper_allow_domain"}, names)
        r = self.run_mcp("advisor", [lst])
        self.assertNotIn("gitlab_upload_file", {t["name"] for t in r[0]["result"]["tools"]})
        self.assertIn("piper_settings_change", {t["name"] for t in r[0]["result"]["tools"]})
        self.assertNotIn("piper_allow_domain", {t["name"] for t in r[0]["result"]["tools"]})   # no project of its own
        self.assertFalse({"git_run", "edit_file", "write_file"} & {t["name"] for t in r[0]["result"]["tools"]})
        self.assertIn("spawn_session", {t["name"] for t in r[0]["result"]["tools"]})
        self.assertNotIn("propose_session", {t["name"] for t in r[0]["result"]["tools"]})

    def test_notes_tools_only_with_the_module_flag_in_a_project_session(self):
        # phase 4: --notes (sessioncfg.notes_flag: the module on); no delete tool; a card-long wait (CONFIRMED)
        lst = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        want = {"notes_list", "notes_read", "notes_append", "notes_write"}
        for role, extra, listed in (("session", ["--notes"], True), ("session", [], False),
                                    ("advisor", ["--notes"], False), ("prodcheck", ["--notes"], False)):
            with self.subTest(role=role, extra=extra):
                tools = self.run_mcp(role, [lst], extra=extra)[0]["result"]["tools"]
                names = {t["name"] for t in tools}
                self.assertEqual(want <= names, listed)
                self.assertFalse({n for n in names if n.startswith("notes_")} - want, "no delete / rename tool")
                if listed:
                    desc = {t["name"]: t["description"] for t in tools}
                    for n in want:
                        self.assertIn("never the shell", desc[n])
                        self.assertIn("No tool deletes a note", desc[n])
                    write = next(t for t in tools if t["name"] == "notes_write")["inputSchema"]
                    self.assertEqual(write["required"], ["name", "text"])
                    self.assertIn("sha", write["properties"])
        spec = importlib.util.spec_from_file_location("piper_mcp_mod", MCP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertLessEqual(want, mod.CONFIRMED)

    def test_session_creation_tools_offer_runtime_without_hole_to_direct_escalation(self):
        for role, name in (("session", "propose_session"), ("advisor", "spawn_session")):
            with self.subTest(role=role):
                reply = self.run_mcp(role, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
                tool = next(t for t in reply[0]["result"]["tools"] if t["name"] == name)
                schema = tool["inputSchema"]
                self.assertEqual(schema["properties"]["runtime"]["enum"], ["hole", "direct"])
                self.assertNotIn("runtime", schema["required"])
                self.assertIn("inherits", schema["properties"]["runtime"]["description"])
                self.assertIn("Hole callers can only choose hole", schema["properties"]["runtime"]["description"])

    def test_propose_session_is_not_offered_as_a_subagent(self):
        reply = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        tool = next(t for t in reply[0]["result"]["tools"] if t["name"] == "propose_session")
        self.assertIn("never use it to split or parallelise your own task", tool["description"])
        self.assertIn("native subagents", tool["description"])
        self.assertEqual(tool["inputSchema"]["properties"]["user_requested"]["type"], "boolean")
        self.assertNotIn("user_requested", tool["inputSchema"]["required"])

    def test_properties_tools_direct_existing_changes_away_from_replacement_files(self):
        reply = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        desc = {t["name"]: t["description"] for t in reply[0]["result"]["tools"]}
        self.assertIn("Use properties_set", desc["properties_keys"])
        self.assertIn("instead of creating a replacement file", desc["properties_set"])
        self.assertIn("If denied or failed", desc["properties_set"])
        self.assertIn("do not bypass", desc["properties_set"])

    def test_quiet_request_requires_explicit_user_instruction(self):
        import sessioncfg
        reply = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        request = next(t for t in reply[0]["result"]["tools"] if t["name"] == "quiet_request")
        self.assertIn("user_requested", request["inputSchema"]["required"])
        self.assertEqual(request["inputSchema"]["properties"]["user_requested"]["enum"], [True])
        self.assertIn("Never request automatically", request["description"])
        self.assertIn("ONLY when the user explicitly", sessioncfg.CLAUDE_MD_BLOCK)

    def test_security_descriptions_do_not_impose_unconditional_locks_or_cards(self):
        reply = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        desc = {t["name"]: t["description"] for t in reply[0]["result"]["tools"]}
        for name in ("git_commit", "git_run", "gitlab_comment", "read_file", "edit_file", "write_file",
                     "db_query", "db_execute", "http_run", "piper_settings_change", "piper_allow_domain"):
            self.assertIn("Security", desc[name], name)
        self.assertNotIn("every call waits for approval", desc["git_run"])
        self.assertNotIn("No hooks", desc["git_run"])

    def test_http_schema_describes_explicit_no_environment_without_implicit_dev(self):
        reply = self.run_mcp('session', [{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])
        run = next(t for t in reply[0]['result']['tools'] if t['name'] == 'http_run')
        self.assertIn('env', run['inputSchema']['required'])
        self.assertIn('empty string', run['inputSchema']['properties']['env']['description'])
        self.assertIn('prod', run['inputSchema']['properties']['env']['description'])

    def test_http_tools_can_select_a_same_repository_checkout_without_expanding_env_writes(self):
        reply = self.run_mcp('session', [{'jsonrpc':'2.0', 'id':1, 'method':'tools/list'}])
        definitions = {tool['name']: tool for tool in reply[0]['result']['tools']}
        for name in ('http_envs', 'http_run', 'http_last_result'):
            schema = definitions[name]['inputSchema']
            self.assertEqual(schema['properties']['project']['type'], 'string')
            self.assertIn('same repository', schema['properties']['project']['description'])
            self.assertNotIn('project', schema.get('required', []))
        self.assertNotIn('project', definitions['http_env_set']['inputSchema']['properties'])

    def test_github_tools_only_with_the_flag_and_never_for_prodcheck(self):
        # W23: piperd passes --github while the module is on; the tools carry no token, piperd's gh login answers
        lst = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        names = lambda r: {t["name"] for t in r[0]["result"]["tools"]}
        self.assertFalse({n for n in names(self.run_mcp("session", [lst])) if n.startswith("github_")})
        on = names(self.run_mcp("session", [lst], extra=["--github"]))
        self.assertLessEqual({"github_issue", "github_pr", "github_inbox", "github_comment", "github_pr_create"}, on)
        self.assertIn("github_issue", names(self.run_mcp("advisor", [lst], extra=["--github"])))
        self.assertFalse({n for n in names(self.run_mcp("prodcheck", [lst], extra=["--github"])) if n.startswith("github_")})

    def test_pr_tool_descriptions_identify_the_forge_and_host_authentication(self):
        lst = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for enabled in (False, True):
            reply = self.run_mcp("session", [lst], extra=["--github"] if enabled else [])
            desc = {t["name"]: t["description"] for t in reply[0]["result"]["tools"]}
            self.assertIn("GitLab merge request (MR)", desc["gitlab_mr_create"])
            self.assertIn("github_pr_create", desc["gitlab_mr_create"])
            self.assertIn("GitHub module is disabled", desc["gitlab_mr_create"])
            self.assertIn("GitLab or GitHub", desc["git_run"])
            if enabled:
                self.assertIn("GitHub pull request (PR)", desc["github_pr_create"])
                self.assertIn("host authentication", desc["github_pr_create"])
                self.assertIn("gitlab_mr_create", desc["github_pr_create"])
            else:
                self.assertNotIn("github_pr_create", desc)

    def test_project_read_tools_explain_local_reads_and_project_keys(self):
        lst = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for role in ("session", "advisor"):
            tools = {t["name"]: t for t in self.run_mcp(role, [lst])[0]["result"]["tools"]}
            for name in ("list_dir", "read_file", "grep"):
                with self.subTest(role=role, tool=name):
                    self.assertIn("locally", tools[name]["description"])
                    self.assertIn("another checkout", tools[name]["description"])
                    project = tools[name]["inputSchema"]["properties"]["project"]["description"]
                    self.assertIn("list_projects", project)
                    self.assertIn("never a filesystem path", project)

    def test_tool_call_through_mailbox(self):
        def piperd():
            out = os.path.join(self.box, "out")
            for _ in range(200):
                names = [n for n in os.listdir(out) if n.endswith(".json") and not n.startswith(".")] if os.path.isdir(out) else []
                if names:
                    with open(os.path.join(out, names[0])) as f:
                        req = json.load(f)
                    assert req["tool"] == "gitlab_issue" and req["args"] == {"iid": 3}
                    with open(os.path.join(self.box, "in", req["id"] + ".json"), "w") as f:
                        json.dump({"id": req["id"], "ok": True, "result": {"iid": 3, "title": "T"}}, f)
                    return
                time.sleep(0.05)
        call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "gitlab_issue", "arguments": {"iid": 3}}}
        r = self.run_mcp("session", [call], responder=piperd)
        self.assertFalse(r[0]["result"]["isError"])
        self.assertEqual(json.loads(r[0]["result"]["content"][0]["text"])["title"], "T")

    def test_unknown_tool_and_method(self):
        r = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "spawn_session"}},
                                     {"jsonrpc": "2.0", "id": 2, "method": "resources/list"}])
        self.assertTrue(r[0]["result"]["isError"])
        self.assertEqual(r[1]["error"]["code"], -32601)

    def test_missing_commit_files_is_rejected_before_publishing_a_request(self):
        spec = importlib.util.spec_from_file_location("piper_mcp_mod", MCP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "git_commit", "arguments": {"message": "fix", "paths": '["a.txt"]'}}}
        output = io.StringIO()
        with mock.patch.object(mod, "call", return_value={"ok": False, "error": "host error"}) as call, \
                mock.patch.object(sys, "argv", [MCP]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
                mock.patch.object(sys, "stdout", output):
            mod.main()
        call.assert_not_called()
        result = json.loads(output.getvalue())["result"]
        self.assertTrue(result["isError"])
        self.assertIn("files", result["content"][0]["text"])

    def test_reply_is_reserved_before_request_and_partial_reply_is_retried(self):
        observed = {}

        def piperd():
            out = os.path.join(self.box, "out")
            for _ in range(200):
                names = [n for n in os.listdir(out) if n.endswith(".json")] if os.path.isdir(out) else []
                if names:
                    with open(os.path.join(out, names[0])) as f:
                        req = json.load(f)
                    reply = os.path.join(self.box, "in", req["id"] + ".json")
                    observed["reserved"] = os.path.isfile(reply) and os.path.getsize(reply) == 0
                    observed["in_place"] = req.get("reply_in_place")
                    with open(reply, "w") as f:
                        f.write('{"id":')
                    time.sleep(0.6)   # the client must tolerate a host write interrupted after truncate
                    with open(reply, "wb") as f:
                        f.write(b'{"error":"\xc4')   # interrupted UTF-8 in an ensure_ascii=False reply
                    time.sleep(0.6)
                    with open(reply, "w") as f:
                        json.dump({"id": req["id"], "ok": False, "error": "invalid files"}, f)
                        f.write("\n")
                    return
                time.sleep(0.01)
        call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
            "name": "git_commit", "arguments": {"message": "fix", "files": ["a.txt"]}}}
        replies = self.run_mcp("session", [call, {"jsonrpc": "2.0", "id": 6, "method": "ping"}], responder=piperd)
        self.assertEqual(observed, {"reserved": True, "in_place": True})
        self.assertEqual(replies[0]["result"]["content"][0]["text"], "invalid files")
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertEqual(replies[1], {"jsonrpc": "2.0", "id": 6, "result": {}})

    def test_prodcheck_role_has_report_and_quiet_participation_only(self):
        r = self.run_mcp("prodcheck", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        self.assertEqual({t["name"] for t in r[0]["result"]["tools"]}, {"report_production", "quiet_status", "quiet_ack", "quiet_wait"})

    def test_no_tool_of_another_role_can_be_called(self):
        r = self.run_mcp("advisor", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "git_run", "arguments": {"argv": ["status"]}}}])
        self.assertTrue(r[0]["result"]["isError"])
        self.assertIn("unknown tool", r[0]["result"]["content"][0]["text"])
        self.assertFalse(os.path.isdir(os.path.join(self.box, "out")) and os.listdir(os.path.join(self.box, "out")))

    def test_db_call_over_the_limit_is_refused_with_a_hint(self):
        big = {"name": "x", "sql": "select 1;" + "-" * 60001}
        r = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "db_execute", "arguments": big}}])
        self.assertTrue(r[0]["result"]["isError"])
        self.assertIn("db_script", r[0]["result"]["content"][0]["text"])
        self.assertFalse(os.path.isdir(os.path.join(self.box, "out")) and os.listdir(os.path.join(self.box, "out")))

    def test_any_call_piperd_would_drop_is_refused_at_once(self):
        # piperd reads at most 64 KiB of a request and drops the rest unanswered: without this the
        # sandbox waited the full 15 minutes of a confirmed tool for "Piper did not answer in time"
        t0 = time.time()
        r = self.run_mcp("session", [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "write_file", "arguments": {"project": "team-x/app", "path": "a.txt", "content": "x" * (70 << 10)}}}])
        self.assertLess(time.time() - t0, 10)
        self.assertTrue(r[0]["result"]["isError"])
        self.assertIn("larger than", r[0]["result"]["content"][0]["text"])
        self.assertFalse(os.path.isdir(os.path.join(self.box, "out")) and os.listdir(os.path.join(self.box, "out")))

    def test_wait_is_15_min_for_confirmed_tools_and_90_s_otherwise(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("piper_mcp_mod", MCP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.MAILBOX = self.box

        class Clock:   # sleep advances a fake clock, so the deadline is measured, not waited for
            now = 1000.0

            def time(self):
                return self.now

            def sleep(self, d):
                self.now += d
        for tool, wait in (("git_status", 90), ("git_push", 900), ("db_query", 900), ("gitlab_issue", 900)):
            mod.time = Clock()
            resp = mod.call(tool, {})
            self.assertFalse(resp["ok"])
            self.assertIn("did not answer", resp["error"])
            self.assertAlmostEqual(mod.time.now - 1000.0, wait, delta=1, msg=tool)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores the read-only mailbox directory")
    def test_unwritable_mailbox_fails_the_call_not_the_server(self):
        # an OSError while publishing a request (full disk, read-only mailbox) ended the stdio server,
        # and with it every Piper tool for the rest of the session
        out = os.path.join(self.box, "out")
        os.makedirs(out)
        for ro in ("in", "out"):   # the reply reservation fails / the request itself fails after it
            with self.subTest(read_only=ro):
                path = os.path.join(self.box, ro)
                os.chmod(path, 0o500)
                try:
                    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "git_status", "arguments": {}}}
                    ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
                    t0 = time.time()
                    r = self.run_mcp("session", [call, ping])
                finally:
                    os.chmod(path, 0o700)
                self.assertLess(time.time() - t0, 10)
                self.assertTrue(r[0]["result"]["isError"])
                self.assertIn("mailbox could not be written", r[0]["result"]["content"][0]["text"])
                self.assertEqual(r[1], {"jsonrpc": "2.0", "id": 2, "result": {}})   # the server still answers
                # nothing half-published for piperd, and no reply reservation left behind
                self.assertEqual(os.listdir(out), [])
                self.assertEqual(os.listdir(os.path.join(self.box, "in")), [])


if __name__ == "__main__":
    unittest.main()
