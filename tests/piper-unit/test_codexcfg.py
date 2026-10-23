"""codexcfg: a Codex session's own CODEX_HOME, rebuilt on every start."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

import _env

import codexcfg  # noqa: E402

REAL_CODEX = os.path.join(_env.HOME, ".codex")
HOST_CONFIG = """model = "gpt-5.6-terra"
model_reasoning_effort = "medium"
approvals_reviewer = "auto_review"
notify = ["/Users/x/.codex/computer-use/Codex Computer Use.app/x"]

[mcp_servers.idea]
url = "http://127.0.0.1:64342/stream"

[projects."/Users/x"]
trust_level = "trusted"
"""


class CodexCfgTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(codexcfg.TEMPLATE_DIR, True)
        shutil.rmtree(REAL_CODEX, True)
        os.makedirs(os.path.join(REAL_CODEX, "sessions", "2026"))
        os.makedirs(os.path.join(REAL_CODEX, "skills", "s1"))
        for name, text in (("config.toml", HOST_CONFIG), ("AGENTS.md", "# mine\n"), ("auth.json", '{"t": "host"}'),
                           ("history.jsonl", "{}\n"), ("skills/s1/SKILL.md", "x")):
            with open(os.path.join(REAL_CODEX, name), "w") as f:
                f.write(text)
        self.dir = os.path.join(_env.ROOT, "codex-session")
        shutil.rmtree(self.dir, True)

    def build(self):
        codexcfg.build(self.dir, "/home/u/.piper", "session", "/p/proj", "piper-events.jsonl", extra_md="ISSUE\n")

    def test_template_is_configuration_only(self):
        self.assertTrue(codexcfg.template_empty())
        self.assertEqual(sorted(codexcfg.seed_template(REAL_CODEX)), ["AGENTS.md", "config.toml", "skills"])
        self.assertFalse(os.path.exists(os.path.join(codexcfg.TEMPLATE_DIR, "auth.json")))   # never the host's login
        self.assertFalse(os.path.exists(os.path.join(codexcfg.TEMPLATE_DIR, "sessions")))

    def test_build_writes_piper_pieces_and_whitelists_the_config(self):
        codexcfg.seed_template(REAL_CODEX)
        self.build()
        with open(os.path.join(self.dir, "config.toml")) as f:
            toml = f.read()
        import tomllib
        c = tomllib.loads(toml)
        self.assertEqual((c["model"], c["model_reasoning_effort"]), ("gpt-5.6-terra", "medium"))
        self.assertNotIn("notify", c)   # a macOS .app: fails in the sandbox
        self.assertEqual(list(c["mcp_servers"]), ["piper"])
        self.assertEqual(c["mcp_servers"]["piper"]["args"],
                         ["/home/u/.piper/piper/piper-mcp.py", "--role", "session", "--mailbox", "/home/u/.piper/piper-mailbox"])
        self.assertEqual(c["projects"], {"/p/proj": {"trust_level": "trusted"}})
        self.assertFalse(c["check_for_update_on_startup"])
        idea = tomllib.loads(codexcfg.config_toml("", "/home/u/.piper", "session", "/p/proj", ["--port", "64342"]))
        self.assertEqual(idea["mcp_servers"]["idea"]["args"], ["/home/u/.piper/piper/idea-bridge.py", "--port", "64342"])
        self.assertEqual(idea["projects"], {"/p/proj": {"trust_level": "trusted"}})
        with open(os.path.join(self.dir, "hooks.json")) as f:
            hooks = json.load(f)["hooks"]
        self.assertEqual(sorted(hooks), sorted(codexcfg.EVENTS))
        self.assertIn("SubagentStart", hooks)
        self.assertIn("SubagentStop", hooks)
        self.assertEqual(hooks["PermissionRequest"][0]["hooks"][0]["command"],
                         "python3 /home/u/.piper/piper/piper-event.py PermissionRequest /home/u/.piper/piper-events.jsonl")
        with open(os.path.join(self.dir, "AGENTS.md")) as f:
            md = f.read()
        self.assertTrue(md.startswith("# mine\n"))
        self.assertIn("Orchestrated hole session", md)
        self.assertIn("A missing native `Read` tool is not a Piper lock", md)
        self.assertIn("file locally before editing", md)
        self.assertIn("Honour explicit user/project restrictions", md)
        self.assertTrue(md.endswith("ISSUE\n"))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "skills", "s1", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "auth.json")))

    def test_rebuild_carries_rollouts_and_login_only(self):
        self.build()
        os.makedirs(os.path.join(self.dir, "sessions", "2026", "10"))
        with open(os.path.join(self.dir, "sessions", "2026", "10", "rollout.jsonl"), "w") as f:
            f.write("{}\n")
        with open(os.path.join(self.dir, "auth.json"), "w") as f:
            f.write('{"t": "session"}')
        with open(os.path.join(self.dir, "hooks.json"), "w") as f:
            f.write('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "planted"}]}]}}')
        self.build()
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "sessions", "2026", "10", "rollout.jsonl")))
        with open(os.path.join(self.dir, "auth.json")) as f:
            self.assertEqual(f.read(), '{"t": "session"}')
        with open(os.path.join(self.dir, "hooks.json")) as f:
            self.assertNotIn("planted", f.read())
        self.assertEqual([n for n in os.listdir(_env.ROOT) if n.startswith("codex-session.prev-")], [])

    def test_both_codex_homes_allow_authorized_git_without_requiring_a_worktree_pool(self):
        import tomllib
        codexcfg.seed_template(REAL_CODEX)
        self.build()
        with open(os.path.join(self.dir, "AGENTS.md")) as f:
            agents = f.read()
        argv = codexcfg.overrides("/home/u/.piper", "session", "/p/proj", "piper-events.jsonl")
        overrides = tomllib.loads("\n".join(argv[1::2]))
        for name, text in (("AGENTS.md", agents), ("developer_instructions", overrides["developer_instructions"])):
            with self.subTest(output=name):
                md = " ".join(text.split())
                self.assertIn("Branch switches and commits in this session's checkout", md)
                self.assertIn("follow the user's task, project instructions and Security", md)
                self.assertIn("Do not change another checkout's HEAD", md)
                self.assertIn("An unset `HOLE_WORKTREES_DIR` does not prohibit", md)
                self.assertIn("Piper defaults it to `.claude/worktrees` inside this session's mounted project", md)
                self.assertIn("`git.worktreeLinks: rw` and `git.worktreePool: true`", md)
                self.assertIn("Do not invent a pool path or use an unmounted sibling", md)
                self.assertNotIn("or change the main checkout's HEAD", md)

    def test_remote_mcp_survives_build_and_resume_with_its_tool_policy(self):
        import tomllib
        remote = '''
[mcp_servers."observability"]
url = "https://mcp-grafana.example.test/mcp"
tool_timeout_sec = 120
enabled_tools = ["loki_query", "resolve_project"]
disabled_tools = ["delete_dashboard"]
default_tools_approval_mode = "prompt"
[mcp_servers."observability".tools.loki_query]
approval_mode = "auto"
output_token_limit = 12000
[mcp_servers."observability".http_headers]
"X-Region" = "test"
[mcp_servers.context7]
url = "https://mcp.context7.example.test/mcp"
enabled = false
bearer_token_env_var = "CONTEXT7_TOKEN"
[mcp_servers.context7.env_http_headers]
"X-Tenant" = "CONTEXT7_TENANT"
[mcp_servers.local_java]
command = "java"
args = ["-jar", "/Users/x/tools/jdwp.jar"]
[mcp_servers.local_browser]
url = "http://127.0.0.1:9222/mcp"
[mcp_servers.local_ipv6]
url = "http://[::1]:9222/mcp"
[mcp_servers.local_name]
url = "http://localhost:9222/mcp"
[mcp_servers.unspecified]
url = "http://0.0.0.0:9222/mcp"
[mcp_servers.helper]
url = "https://mcp.example.test"
http_headers_helper = ["/Users/x/auth-helper"]
[mcp_servers.invalid]
url = "file:///Users/x/server"
'''
        with open(os.path.join(REAL_CODEX, "config.toml"), "a") as f:
            f.write(remote)
        original = tomllib.loads(HOST_CONFIG + remote)
        codexcfg.sync_template(REAL_CODEX)
        for _ in range(2):
            self.build()
            with open(os.path.join(self.dir, "config.toml"), "rb") as f:
                servers = tomllib.load(f)["mcp_servers"]
            self.assertEqual(set(servers), {"piper", "observability", "context7"})
            for name in ("observability", "context7"):
                self.assertEqual(servers[name], original["mcp_servers"][name])
        with open(os.path.join(REAL_CODEX, "config.toml"), "rb") as f:
            self.assertEqual(tomllib.load(f), original)

    def test_remote_mcp_never_carries_a_static_credential_or_a_private_address_into_the_sandbox(self):
        """R3 (PLAN-ADD 2026-10-09): http_headers of the host template were copied verbatim - a static
        Authorization / API key ended in the sandbox's config.toml, readable by the agent; and the URL filter
        let private, link-local (cloud metadata) and host-gateway addresses through."""
        import tomllib
        source = '''
[mcp_servers.grafana]
url = "https://mcp-grafana.example.test/mcp"
[mcp_servers.grafana.http_headers]
"X-Region" = "eu"
Authorization = "Bearer PLANTED_H1"
"X-Api-Key" = "PLANTED_H2"
Cookie = "sid=PLANTED_H3"
"X-Scope" = "PLANTED_H4xxxxxxxxxxxxxxxxxxxxxxxx"
[mcp_servers.meta]
url = "http://169.254.169.254/latest/meta-data"
[mcp_servers.ten]
url = "http://10.0.0.5/mcp"
[mcp_servers.lan]
url = "https://192.168.1.20:8443/mcp"
[mcp_servers.mapped]
url = "http://[::ffff:10.0.0.5]/mcp"
[mcp_servers.ula]
url = "http://[fd00::1]/mcp"
[mcp_servers.gateway]
url = "http://host.internal:17895/mcp"
[mcp_servers.dockerhost]
url = "http://host.docker.internal:8080/mcp"
[mcp_servers.gcpmeta]
url = "http://metadata.google.internal/computeMetadata/v1"
'''
        text = codexcfg.config_toml(source, "/box", "session", "/project")
        self.assertNotIn("PLANTED_H", text)
        servers = tomllib.loads(text)["mcp_servers"]
        self.assertEqual(set(servers), {"piper", "grafana"})
        self.assertEqual(servers["grafana"]["http_headers"], {"X-Region": "eu"})

    def test_remote_mcp_url_filter_fails_closed(self):
        """N4: a credential in the URL as userinfo (the query / fragment / path heuristics are off since
        2026-10-10, the user's decision - those rows are kept, now kept) and every odd spelling of an address the resolver
        accepts (integer, hex, octal, short dotted, IPv6 with an embedded IPv4, trailing dot, non-ASCII,
        parser-differential tricks) never reaches the sandbox's config.toml."""
        table = [
            # (url, kept)
            ("https://mcp.example.test/mcp", True),
            ("https://mcp.example.test:8443/mcp?region=eu&v=2", True),
            ("http://93.184.216.34/mcp", True),
            ("http://[2606:4700:4700::1111]/mcp", True),
            # credentials
            ("https://user:pw@mcp.example.test/mcp", False),
            ("https://user@mcp.example.test/mcp", False),
            ("https://:pw@mcp.example.test/mcp", False),
            ("https://@mcp.example.test/mcp", False),
            ("https://mcp.example.test/mcp?api_key=PLANTED", True),
            ("https://mcp.example.test/mcp?access_token=PLANTED", True),
            ("https://mcp.example.test/mcp?client_secret=PLANTED", True),
            ("https://mcp.example.test/mcp?password=PLANTED", True),
            ("https://mcp.example.test/mcp?auth=PLANTED", True),
            ("https://mcp.example.test/mcp?sv=2024&sig=PLANTED", True),
            ("https://mcp.example.test/mcp?X-Amz-Signature=PLANTED", True),
            ("https://mcp.example.test/mcp?a=1&%74oken=PLANTED", True),        # percent-encoded name
            ("https://mcp.example.test/mcp?k=PLANTEDxxxxxxxxxxxxxxxxxxxxxxxxx", True),   # an opaque value
            # numeric / odd host forms of loopback, private and metadata addresses
            ("http://2130706433/mcp", False),          # 127.0.0.1 as an integer
            ("http://2852039166/mcp", False),          # 169.254.169.254
            ("http://0x7f000001/mcp", False),
            ("http://0x7f.0.0.1/mcp", False),
            ("http://0177.0.0.1/mcp", False),          # octal
            ("http://017700000001/mcp", False),
            ("http://127.1/mcp", False),               # short dotted
            ("http://10.1/mcp", False),
            ("http://0/mcp", False),
            ("http://1.2.3/mcp", False),               # a short public form: still odd, refused
            ("http://093.184.216.34/mcp", False),      # a leading zero
            ("http://[::ffff:93.184.216.34]/mcp", False),   # IPv6-mapped, even public
            ("http://[::ffff:7f00:1]/mcp", False),
            ("http://[::127.0.0.1]/mcp", False),       # IPv4-compatible
            ("http://[64:ff9b::7f00:1]/mcp", False),   # NAT64
            ("http://[2002:7f00:1::]/mcp", False),     # 6to4
            ("http://[::1]/mcp", False),
            ("http://[fe80::1%25en0]/mcp", False),
            ("http://mcp.example.test./mcp", False),   # trailing dot
            ("http://localhost./mcp", False),
            ("http://LOCALHOST/mcp", False),
            ("http://127.0.0.1.nip.io.localhost/mcp", False),
            ("http://１２７.０.０.１/mcp", False),       # full-width digits (IDNA folds them)
            ("http://mcp.example.test\\@127.0.0.1/mcp", False),   # Python and WHATWG disagree on the host
            ("http://127.0.0.1\\@mcp.example.test/mcp", False),
            ("http://mcp.example.test/m\ncp", False),
            ("http://mcp.example.test /mcp", False),
            ("http://mcp_x.example.test/mcp", False),
            ("http://127%2e0%2e0%2e1/mcp", False),     # percent-encoded dots
            ("http://1.2.3.4.5/mcp", False),           # a numeric last label that is no address
            ("http://[fd00::1]/mcp", False),
            ("http://METADATA/computeMetadata", False),
            ("http://metadata.google.internal./x", False),
            ("ftp://mcp.example.test/mcp", False),
            # N5: a credential in the fragment, behind a ";" separator, as a path segment (an API key / a UUID)
            ("https://mcp.example.test/mcp#access_token=PLANTED", True),
            ("https://mcp.example.test/mcp#PLANTED", True),
            ("https://mcp.example.test/mcp?a=1;token=PLANTED", True),
            ("https://actions.example.test/mcp/sk-ak-PLANTED0123456789abcdef/sse", True),
            ("https://mcp.example.test/s/4f9c2e1a-0d3b-4b7e-9d21-6c3a8e5f0b12;PLANTED/sse", True),
            ("https://mcp.example.test/model-context-protocol-server/mcp", True),   # a long plain segment stays
            ("https://mcp.example.test/v1/mcp?region=eu-west-1", True),
            # N6: a path parameter is a parameter - its NAME counts like a query parameter's (a short secret)
            ("https://mcp.example.test/mcp;token=PLANTED1", True),
            ("https://mcp.example.test/mcp;jsessionid=PLANTED1/sse", True),
            ("https://mcp.example.test/v1;api_key=PLANTED/mcp", True),
            ("https://mcp.example.test/v1;version=2/mcp", True),
            # 2026-10-10 (the user): the query / fragment / path heuristics are off - they dropped real servers
            ("https://mcp.example.test/mcp?author=me&keyword=x", True),
            ("https://mcp.example.test/s/4f9c2e1a-0d3b-4b7e-9d21-6c3a8e5f0b12/sse", True),
        ]
        for url, kept in table:
            with self.subTest(url=url):
                lines = codexcfg._http_mcp({"mcp_servers": {"s": {"url": url}}})
                self.assertEqual(bool(lines), kept, lines)
                if not kept:
                    self.assertNotIn("PLANTED", "\n".join(lines))

    def test_generated_mcp_servers_cannot_be_overridden_by_the_host(self):
        import tomllib
        source = '''[mcp_servers.piper]
url = "https://wrong.example.test/mcp"
[mcp_servers.idea]
url = "https://wrong.example.test/idea"
'''
        config = tomllib.loads(codexcfg.config_toml(source, "/box", "session", "/project", ["--port", "1234"]))
        self.assertEqual(config["mcp_servers"]["piper"]["command"], "python3")
        self.assertEqual(config["mcp_servers"]["idea"]["args"], ["/box/piper/idea-bridge.py", "--port", "1234"])
        for source in ('mcp_servers = "bad"', 'mcp_servers = []', '[mcp_servers.bad]\nurl = 42'):
            with self.subTest(source=source):
                config = tomllib.loads(codexcfg.config_toml(source, "/box", "session", "/project"))
                self.assertEqual(set(config["mcp_servers"]), {"piper"})

    def test_nested_codex_uses_hole_isolation_without_changing_host_config(self):
        import tomllib
        host = os.path.join(REAL_CODEX, "config.toml")
        with open(host, "a") as f:
            f.write('\n[profiles.review]\nsandbox_mode = "read-only"\napproval_policy = "on-request"\n')
        with open(host) as f:
            original = f.read()
        codexcfg.seed_template(REAL_CODEX)
        self.build()
        for path in (host, os.path.join(codexcfg.TEMPLATE_DIR, "config.toml")):
            with open(path) as f:
                self.assertEqual(f.read(), original)
        with open(os.path.join(self.dir, "config.toml"), "rb") as f:
            config = tomllib.load(f)
        self.assertEqual(config["sandbox_mode"], "danger-full-access")
        self.assertEqual(config["approval_policy"], "never")
        self.assertNotIn("profiles", config)
        for source in ('sandbox_mode = "read-only"\napproval_policy = "on-request"\n', "broken [toml"):
            config = tomllib.loads(codexcfg.config_toml(source, "/box", "session", "/project"))
            self.assertEqual(config["sandbox_mode"], "danger-full-access")
            self.assertEqual(config["approval_policy"], "never")

    def test_the_login_goes_back_only_when_its_content_changed(self):
        """Continue re-saves nothing it did not change, and the template's login (the last one a session
        saved) wins over a carried one - whatever mtime the sandbox gave its file (a planted login dated
        2099 must not beat every later real login)."""
        self.build()
        auth = os.path.join(self.dir, "auth.json")
        tpl = os.path.join(codexcfg.TEMPLATE_DIR, "auth.json")
        self.assertEqual(codexcfg.seed_credentials(self.dir), "")   # no login anywhere yet
        with open(auth, "w") as f:
            f.write('{"t": "planted"}')
        os.utime(auth, (4102444800, 4102444800))   # 2100-01-01
        self.assertTrue(codexcfg.copy_back_credentials(self.dir, ""))   # a login appeared: saved
        with open(tpl, "w") as f:
            f.write('{"t": "real, later"}')
        os.utime(tpl, (1000, 1000))   # older on disk than the planted one
        self.build()   # Continue: the carried planted login ...
        seeded = codexcfg.seed_credentials(self.dir)   # ... is replaced by the template's
        with open(auth) as f:
            self.assertEqual(f.read(), '{"t": "real, later"}')
        self.assertFalse(codexcfg.copy_back_credentials(self.dir, seeded))   # unchanged: nothing saved
        with open(auth, "w") as f:
            f.write('{"t": "refreshed"}')
        self.assertTrue(codexcfg.copy_back_credentials(self.dir, seeded))   # changed in the session
        with open(tpl) as f:
            self.assertEqual(f.read(), '{"t": "refreshed"}')

    def test_a_custom_model_provider_is_not_copied_without_its_table(self):
        with open(os.path.join(REAL_CODEX, "config.toml"), "a") as f:
            f.write('\n[model_providers.proxy]\nbase_url = "http://x"\n')
        with open(os.path.join(REAL_CODEX, "config.toml")) as f:
            text = 'model_provider = "proxy"\n' + f.read()
        with open(os.path.join(REAL_CODEX, "config.toml"), "w") as f:
            f.write(text)
        codexcfg.seed_template(REAL_CODEX)
        self.build()
        import tomllib
        with open(os.path.join(self.dir, "config.toml"), "rb") as f:
            c = tomllib.load(f)
        self.assertNotIn("model_provider", c)
        self.assertNotIn("model_providers", c)

    def test_a_symlinked_login_is_neither_carried_nor_copied_back(self):
        self.build()
        secret = os.path.join(_env.ROOT, "secret.json")
        with open(secret, "w") as f:
            f.write('{"secret": 1}')
        os.symlink(secret, os.path.join(self.dir, "auth.json"))
        self.assertFalse(codexcfg.copy_back_credentials(self.dir, ""))
        self.build()
        self.assertFalse(os.path.exists(os.path.join(self.dir, "auth.json")))

    def test_host_hook_snapshot_preserves_execution_and_refreshes_only_references(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=_env.ROOT) as home, mock.patch.object(codexcfg.common, "REAL_HOME", home):
            hooks = os.path.join(home, ".claude", "hooks")
            os.makedirs(hooks)
            os.makedirs(os.path.join(home, ".codex"))
            script = os.path.join(hooks, "guard.sh")
            with open(script, "w") as f:
                f.write('#!/bin/sh\nread command\n[ "$command" = blocked ] && exit 2\nexit 0\n')
            os.chmod(script, 0o755)
            with open(os.path.join(hooks, "unrelated"), "w") as f:
                f.write("not referenced")
            source = json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": script}]}]}})
            config = os.path.join(home, ".codex", "hooks.json")
            with open(config, "w") as f:
                f.write(source)
            dest = os.path.join(home, "snapshot")
            codexcfg.copy_host_hooks(dest)
            self.assertEqual(os.listdir(dest), ["guard.sh"])
            for command, code in (("allowed", 0), ("blocked", 2)):
                r = subprocess.run([os.path.join(dest, "guard.sh")], input=command + "\n", text=True)
                self.assertEqual(r.returncode, code)
            with open(config) as f:
                self.assertEqual(f.read(), source)
            with open(config, "w") as f:
                f.write("{}")
            codexcfg.copy_host_hooks(dest)
            self.assertEqual(os.listdir(dest), [])
            with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
                f.write('[[hooks.PreToolUse]]\nhooks = [{command = ' + json.dumps(script) + '}]\n')
            codexcfg.copy_host_hooks(dest)
            self.assertTrue(os.path.isfile(os.path.join(dest, "guard.sh")))
            os.unlink(script)
            with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                codexcfg.copy_host_hooks(dest)
            os.symlink(config, script)
            with self.assertRaisesRegex(ValueError, "missing or unsafe"):
                codexcfg.copy_host_hooks(dest)

    def test_overrides_carry_what_build_writes(self):
        """codex_dir=host: build()'s config, hooks and block as `-c` args - every value valid TOML."""
        import tomllib
        a = codexcfg.overrides("/h/.piper", "session", '/w/.claude/p "x=y', "piper-events.jsonl", extra_md="\nISSUE", idea=["--port", "9"])
        self.assertEqual(a[::2], ["-c"] * (len(a) // 2))
        doc = tomllib.loads("\n".join(a[1::2]))
        self.assertEqual(doc["projects"]['/w/.claude/p "x=y']["trust_level"], "trusted")
        bridges = [v for k, v in doc["mcp_servers"].items() if k.startswith("piper_session_")]
        self.assertEqual(bridges[0]["tool_timeout_sec"], 960)
        self.assertFalse(doc["mcp_servers"]["idea"]["enabled"])
        idea = [v for k, v in doc["mcp_servers"].items() if k.startswith("piper_idea_")]
        self.assertEqual(idea[0]["args"][-2:], ["--port", "9"])
        self.assertEqual(doc["notify"], [])
        self.assertEqual(set(doc["hooks"]), set(codexcfg.EVENTS))
        self.assertEqual(doc["hooks"]["Stop"], codexcfg.hooks_json("/h/.piper", "piper-events.jsonl")["hooks"]["Stop"])
        self.assertIn("Orchestrated hole session", doc["developer_instructions"])
        self.assertIn("A missing native `Read` tool is not a Piper lock", doc["developer_instructions"])
        self.assertIn("user to allow Python file reads or unlock Piper's reader merely", doc["developer_instructions"])
        self.assertTrue(doc["developer_instructions"].endswith("ISSUE"))

    def test_host_mcp_collisions_keep_the_host_config_and_use_unused_names(self):
        import tomllib
        path = os.path.join(REAL_CODEX, "config.toml")
        first = codexcfg.overrides("/box", "session", "/project", "events", idea=["--port", "9"])
        first_servers = tomllib.loads("\n".join(first[1::2]))["mcp_servers"]
        bridge_names = [k for k in first_servers if k.startswith("piper_")]
        source = HOST_CONFIG + '''
[mcp_servers.piper]
url = "https://example.invalid/piper"
'''
        source += "\n".join('[mcp_servers.%s]\ncommand = "existing"\n' % k for k in bridge_names)
        with open(path, "w") as f:
            f.write(source)
        argv = codexcfg.overrides("/box", "session", "/project", "events", idea=["--port", "9"])
        servers = tomllib.loads("\n".join(argv[1::2]))["mcp_servers"]
        self.assertEqual(servers["idea"], {"enabled": False})
        self.assertEqual(servers["piper"], {"enabled": False})
        for k in bridge_names:
            self.assertNotIn(k, servers)
            self.assertEqual(servers[k + "_2"]["command"], "python3")
        with open(path) as f:
            self.assertEqual(f.read(), source)

    def test_host_overrides_keep_shared_history_but_use_container_sqlite(self):
        import tomllib
        path = os.path.join(REAL_CODEX, "config.toml")
        source = 'sqlite_home = "/host/shared-databases"\n' + HOST_CONFIG
        with open(path, "w") as f:
            f.write(source)
        args = codexcfg.overrides("/box", "session", "/project", "events")
        effective = {**tomllib.loads(source), **tomllib.loads("\n".join(args[1::2]))}
        self.assertEqual(effective["sqlite_home"], os.path.join(REAL_CODEX, "piper-sqlite-linux"))
        self.assertNotIn("codex_home", effective)
        with open(path) as f:
            self.assertEqual(f.read(), source)

    def test_host_overrides_work_without_tomllib_on_python310(self):
        with mock.patch.dict("sys.modules", {"tomllib": None}):
            argv = codexcfg.overrides("/box", "session", "/project", "events", idea=["--port", "9"])
        self.assertTrue(any(x.startswith("mcp_servers.piper_idea_") for x in argv))

    def test_bridge_names_are_stable_for_resume_and_separate_between_sessions(self):
        one = codexcfg.overrides("/box/one", "session", "/project", "events")
        self.assertEqual(one, codexcfg.overrides("/box/one", "session", "/project", "events"))
        self.assertNotEqual(one, codexcfg.overrides("/box/two", "session", "/project", "events"))

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI not installed")
    def test_host_http_idea_override_loads_in_the_real_codex_cli(self):
        argv = codexcfg.overrides("/box", "session", "/project", "events", idea=["--port", "9"])
        result = subprocess.run([shutil.which("codex"), *argv, "mcp", "list"],
                                env={**os.environ, "CODEX_HOME": REAL_CODEX},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("piper_idea", result.stdout)
        self.assertIn("disabled", result.stdout)

    @unittest.skipUnless(shutil.which("codex"), "Codex CLI not installed")
    def test_project_http_mcp_cannot_merge_into_the_bridges(self):
        project = os.path.join(_env.ROOT, "codex-project")
        os.makedirs(os.path.join(project, ".git"), exist_ok=True)
        os.makedirs(os.path.join(project, ".codex"), exist_ok=True)
        with open(os.path.join(project, ".codex", "config.toml"), "w") as f:
            f.write('[mcp_servers.idea]\nurl = "https://example.invalid/idea"\n'
                    '[mcp_servers.piper_idea]\nurl = "https://example.invalid/other"\n'
                    '[mcp_servers.piper]\nurl = "https://example.invalid/piper"\n')
        for host in ("", HOST_CONFIG):
            with self.subTest(host_idea=bool(host)):
                with open(os.path.join(REAL_CODEX, "config.toml"), "w") as f:
                    f.write(host)
                argv = codexcfg.overrides("/box", "session", project, "events", idea=["--port", "9"])
                result = subprocess.run([shutil.which("codex"), *argv, "mcp", "list"], cwd=project,
                                        env={**os.environ, "CODEX_HOME": REAL_CODEX},
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("piper_session_", result.stdout)
                self.assertIn("piper_idea_", result.stdout)
                self.assertIn("https://example.invalid/other", result.stdout)  # project layer really loaded

    def test_template_follows_host_codex(self):
        self.assertIn("AGENTS.md", codexcfg.sync_template(REAL_CODEX)[0])
        with open(os.path.join(REAL_CODEX, "AGENTS.md"), "w") as f:
            f.write("changed on the host")
        with open(os.path.join(REAL_CODEX, "auth.json"), "w") as f:
            f.write('{"host": "login"}')
        self.assertEqual(codexcfg.sync_template(REAL_CODEX), (["AGENTS.md"], [], []))
        self.build()
        with open(os.path.join(self.dir, "AGENTS.md")) as f:
            self.assertTrue(f.read().startswith("changed on the host"))
        self.assertFalse(os.path.exists(os.path.join(codexcfg.TEMPLATE_DIR, "auth.json")))   # never the host's login

    def test_login_goes_back_to_the_template_and_seeds_the_next_session(self):
        self.build()
        with open(os.path.join(self.dir, "auth.json"), "w") as f:
            f.write('{"t": "new"}')
        self.assertTrue(codexcfg.copy_back_credentials(self.dir, ""))
        self.assertFalse(codexcfg.copy_back_credentials(self.dir, codexcfg._digest(b'{"t": "new"}')))   # unchanged
        other = os.path.join(_env.ROOT, "codex-other")
        shutil.rmtree(other, True)
        codexcfg.build(other, "/home/u/.piper", "session", "/p/q", "piper-events.jsonl")
        codexcfg.seed_credentials(other)
        with open(os.path.join(other, "auth.json")) as f:
            self.assertEqual(f.read(), '{"t": "new"}')
        self.assertEqual(os.stat(os.path.join(other, "auth.json")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
