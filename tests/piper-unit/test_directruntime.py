"""Direct runtime keeps native login and permissions and never invokes Hole."""
import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from unittest import mock

import _env
import common
import codexcfg
import directruntime
import sessions


class DirectRuntimeTest(unittest.TestCase):
    def session(self, agent="claude", **kw):
        s = sessions.Session(project="direct", path=_env.PP, agent=agent, runtime="direct", **kw)
        s.claude_mode = "none" if agent == "codex" else "host"
        s.host_codex = agent == "codex"
        self.addCleanup(__import__('shutil').rmtree, s.dir, True)
        return s

    def prepare(self, s):
        with mock.patch.object(directruntime.shutil, "which", return_value="/usr/bin/fake-agent"):
            return directruntime.prepare(s, "project")

    def test_claude_uses_native_home_and_session_only_hooks(self):
        s = self.session(task="implement --help safely")
        argv, env = self.prepare(s)
        self.assertEqual(argv[-2:], ["--", s.task])
        self.assertEqual(env["HOME"], common.REAL_HOME)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        cfg = common.read_json(os.path.join(s.box, "piper/settings.json"), {})
        for event, entries in cfg["hooks"].items():
            cmd = shlex.split(entries[0]["hooks"][0]["command"])
            self.assertEqual(cmd[-2:], [event, os.path.join(s.box, s.names["events"])])
        self.assertFalse(os.path.exists(os.path.join(common.REAL_HOME, ".claude", "piper")))

    def test_codex_keeps_login_but_enforces_native_sandbox(self):
        s = self.session("codex", resume="12345678-abcd", fork_pending=True)
        argv, env = self.prepare(s)
        self.assertEqual(env["CODEX_HOME"], os.path.join(common.REAL_HOME, ".codex"))
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("--ask-for-approval") + 1], "on-request")
        self.assertEqual(argv[-2:], ["fork", s.resume])
        self.assertNotIn("--dangerously-bypass-hook-trust", argv)
        self.assertNotIn("danger-full-access", " ".join(argv))

    def test_codex_hook_trust_stays_stable_while_events_stay_session_scoped(self):
        import tomllib

        def hooks(argv):
            result = {}
            for i, value in enumerate(argv[:-1]):
                if value == '-c':
                    result.update(tomllib.loads(argv[i + 1]).get('hooks', {}))
            return result

        # The event destination must never be interpolated into a shell command.
        root = os.path.join(_env.ROOT, 'sessions with spaces " and $literal')
        with mock.patch.object(common, 'SESSIONS_DIR', root):
            first, second = self.session('codex'), self.session('codex')
            argv_a, env_a = self.prepare(first)
            argv_b, env_b = self.prepare(second)
            hooks_a, hooks_b = hooks(argv_a), hooks(argv_b)
            self.assertEqual(hooks_a, hooks_b)
            self.assertEqual(hooks_a, hooks(self.prepare(first)[0]))
            self.assertEqual(set(hooks_a), set(codexcfg.EVENTS))
            self.assertNotEqual(env_a['PIPER_EVENT_FILE'], env_b['PIPER_EVENT_FILE'])
            for s, env in ((first, env_a), (second, env_b)):
                path = os.path.join(s.box, s.names['events'])
                self.assertEqual(env['PIPER_EVENT_FILE'], path)
                for event, entries in hooks_a.items():
                    command = entries[-1]['hooks'][0]['command']
                    self.assertNotIn(s.dir, command)
                    subprocess.run(command, shell=True, env=env, check=True,
                                   input=json.dumps({'session_id': s.id}).encode(),
                                   capture_output=True, timeout=10)
                with open(path, encoding='utf-8') as stream:
                    events = [json.loads(line) for line in stream]
                self.assertEqual([event['ev'] for event in events], [event for event in hooks_a if event != 'PreToolUse'])
                self.assertEqual({event['session_id'] for event in events}, {s.id})

    def test_event_hook_preserves_explicit_path_and_legacy_default(self):
        script = os.path.join(common.PIPER_DIR, 'sandbox', 'piper-event.py')
        s = self.session('codex')
        _, env = self.prepare(s)
        explicit = os.path.join(s.box, 'explicit.jsonl')
        subprocess.run([sys.executable, script, 'Stop', explicit],
                       env=env, input=b'{}', check=True, capture_output=True, timeout=10)
        self.assertTrue(os.path.isfile(explicit))
        self.assertFalse(os.path.exists(env['PIPER_EVENT_FILE']))
        home = os.path.join(s.box, 'legacy-home')
        os.makedirs(os.path.join(home, '.claude'))
        env.update(HOME=home, PIPER_EVENT_FILE='')
        subprocess.run([sys.executable, script, 'Stop'],
                       env=env, input=b'{}', check=True, capture_output=True, timeout=10)
        self.assertTrue(os.path.isfile(os.path.join(home, '.claude', 'piper-events.jsonl')))

    def test_completed_codex_permissions_clear_only_the_matching_requests(self):
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'working'; s.claude_session = 'parent-1234'
        _, env = self.prepare(s)
        script = os.path.join(common.PIPER_DIR, 'sandbox', 'piper-event.py')

        def emit(event, command, **fields):
            payload = dict(session_id=s.claude_session, turn_id='turn-1234', tool_name='Bash',
                           tool_input={'command': command})
            payload.update(fields)
            if event == 'PermissionRequest':
                payload['tool_input']['description'] = 'Approval reason only on the request'
            subprocess.run([sys.executable, script, event], input=json.dumps(payload).encode(),
                           env=env, check=True, capture_output=True, timeout=10)
            m._events_read(s)

        emit('PermissionRequest', 'echo private-first')
        emit('PermissionRequest', 'echo private-second')
        self.assertEqual(s.state, 'attention')
        emit('PostToolUse', 'echo unrelated')
        self.assertEqual(s.state, 'attention')
        emit('PostToolUse', 'echo private-first')
        self.assertEqual(s.state, 'attention')
        # Replaying the complete event stream after a controller restart must
        # reconstruct the remaining request rather than lose or duplicate it.
        s = sessions.Session(**s.to_json())
        m._events_read(s)
        self.assertEqual(s.state, 'attention')
        emit('PostToolUse', 'echo private-second')
        self.assertEqual((s.state, s.detail, s.idle), ('working', '', False))
        self.assertFalse(m._dialog_up(s))
        with open(env['PIPER_EVENT_FILE'], encoding='utf-8') as stream:
            text = stream.read()
        self.assertNotIn('private-', text)
        self.assertNotIn('Approval reason', text)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(records[0]['tool_key'], records[3]['tool_key'])
        self.assertNotEqual(records[0]['tool_key'], records[1]['tool_key'])
        emit('PermissionRequest', 'echo identical')
        emit('PermissionRequest', 'echo identical')
        emit('PostToolUse', 'echo identical', session_id='another-parent')
        self.assertEqual(s.state, 'attention')
        emit('PostToolUse', 'echo identical')
        self.assertEqual(s.state, 'attention')
        emit('PostToolUse', 'echo identical')
        self.assertEqual(s.state, 'working')

    def test_codex_tool_completion_preserves_other_native_attention(self):
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'working'
        os.makedirs(s.box, exist_ok=True)
        key = 'a' * 64

        def emit(event, **fields):
            with open(os.path.join(s.box, s.names['events']), 'a') as stream:
                stream.write(json.dumps(dict(ev=event, ts=time.time(), **fields)) + '\n')
            m._events_read(s)

        emit('PermissionRequest', tool_name='Bash', tool_key=key)
        s.phase, s.detail = 'agent-auth', 'Agent authentication failed'
        emit('PostToolUse', tool_key=key)
        self.assertEqual((s.state, s.phase, s.detail),
                         ('attention', 'agent-auth', 'Agent authentication failed'))
        s.phase, s.detail = '', ''
        emit('PermissionRequest', tool_name='Bash')  # Older hook scripts cannot be correlated safely.
        emit('PostToolUse', tool_key=key)
        self.assertEqual(s.state, 'attention')

    def codex_rollout(self, s, reviewer='auto_review'):
        s.claude_session = 'parent-1234'
        folder = os.path.join(s.codex_home, 'sessions', '2026', '10', '25')
        os.makedirs(folder, exist_ok=True)
        self.addCleanup(__import__('shutil').rmtree, folder, True)
        path = os.path.join(folder, 'rollout-' + s.claude_session + '.jsonl')
        with open(path, 'w') as stream:
            for rec in ({'type': 'session_meta', 'payload': {'id': s.claude_session}},
                        {'type': 'turn_context', 'payload': {'approvals_reviewer': reviewer}}):
                stream.write(json.dumps(rec) + '\n')
        return path

    def test_codex_automatic_review_is_work_not_a_human_permission_prompt(self):
        import mobilepush
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'working'
        m.sessions[s.id] = s
        detector = mobilepush.Events()
        self.assertFalse(detector.changed(mobilepush.snapshot(m)))
        self.codex_rollout(s)
        os.makedirs(s.box, exist_ok=True)
        common.safe_write(s.box, s.names['events'], (json.dumps(dict(
            ev='PermissionRequest', tool_name='Bash', ts=time.time())) + '\n').encode())
        m._events_read(s)  # Denial/cancellation need not produce PostToolUse.
        self.assertEqual((s.state, s.detail, s.idle), ('working', '', False))
        self.assertEqual(s.notif_ts, 0)
        self.assertFalse(s.codex_permissions)
        self.assertFalse(detector.changed(mobilepush.snapshot(m)), 'No false mobile Push event')
        # Already-running sessions and older hook scripts recover on controller restart.
        s.state, s.detail = 'attention', 'Codex needs permission: Bash'
        s = sessions.Session(**s.to_json())
        m._events_read(s)
        self.assertEqual((s.state, s.detail, s.idle), ('working', '', False))

    def test_codex_actual_turn_reviewer_overrides_host_config_and_can_change(self):
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'working'
        path = self.codex_rollout(s, 'user')
        os.makedirs(s.box, exist_ok=True)
        event = json.dumps(dict(ev='PermissionRequest', tool_name='Bash', ts=time.time(), tool_key='a' * 64)) + '\n'
        common.safe_write(s.box, s.names['events'], event.encode())
        with mock.patch.object(directruntime, 'codex_settings', return_value={'approvals_reviewer': 'auto_review'}):
            m._events_read(s)
        self.assertEqual(s.state, 'attention')
        with open(path, 'a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {'approvals_reviewer': 'auto_review'}}) + '\n')
        m._events_read(s)  # No new hook is necessary to reconcile the saved state.
        self.assertEqual(s.state, 'working')
        with open(path, 'a') as stream:
            stream.write(json.dumps({'type': 'turn_context', 'payload': {'approvals_reviewer': 'user'}}) + '\n')
        with open(os.path.join(s.box, s.names['events']), 'a') as stream:
            stream.write(event)
        m._events_read(s)
        self.assertEqual(s.state, 'attention')

    def test_codex_auto_review_preserves_auth_startup_and_piper_attention(self):
        m = _env.manager(self, background=False)
        for phase, detail in (('agent-auth', 'Agent authentication failed'),
                              ('agent', 'Codex needs permission: Bash'), ('', 'L5: host approval')):
            s = self.session('codex'); self.codex_rollout(s)
            s.state, s.phase, s.detail = 'attention', phase, detail
            m._events_read(s)
            self.assertEqual((s.state, s.phase, s.detail), ('attention', phase, detail))

    def test_codex_reviewer_requires_own_complete_rollout_and_bounded_reads(self):
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'working'
        path = self.codex_rollout(s)
        with open(path, 'w') as stream:
            stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': 'foreign-1234'}}) + '\n')
            stream.write(json.dumps({'type': 'turn_context', 'payload': {'approvals_reviewer': 'auto_review'}}) + '\n')
        os.makedirs(s.box, exist_ok=True)
        common.safe_write(s.box, s.names['events'], (json.dumps(dict(
            ev='PermissionRequest', tool_name='Bash', ts=time.time())) + '\n').encode())
        m._events_read(s)
        self.assertEqual(s.state, 'attention')
        path = self.codex_rollout(s, 'user')  # Truncation resets the cursor and identity.
        m._events_read(s)
        with open(path, 'a') as stream:
            stream.write('x' * ((1 << 20) + 10) + '\n')
            stream.write('{"type":"turn_context","payload":{"approvals_reviewer":"auto_review"}}')
        with mock.patch.object(common, 'safe_read', wraps=common.safe_read) as read:
            m._events_read(s)
            self.assertTrue(all(call.args[2] <= 1 << 20 for call in read.call_args_list))
        m._events_read(s)
        self.assertEqual(s.state, 'attention', 'A partial context cannot hide a real permission prompt')
        with open(path, 'a') as stream:
            stream.write('\n')
        m._events_read(s)
        self.assertEqual(s.state, 'working')

    def test_both_agents_use_the_same_enabled_piper_tool_modules(self):
        for name in ('claude','codex'):
            s=self.session(name)
            with mock.patch.object(directruntime.sessioncfg.features,'on',return_value=True):
                argv,_=self.prepare(s)
            mcp=common.read_json(os.path.join(s.box,'piper/mcp.json'),{})['mcpServers']['piper']
            self.assertEqual(mcp['command'],__import__('sys').executable)
            self.assertIn('--github',mcp['args'])
            if name == 'codex': self.assertTrue(any('--github' in arg for arg in argv))

    def test_old_sessions_keep_hole_and_invalid_runtime_rejected(self):
        self.assertEqual(sessions.Session().runtime, "hole")
        for bad in ("ssh", "", [], True):
            with self.assertRaises(ValueError):
                directruntime.validate(bad)

    def test_missing_agent_and_hole_options_fail_before_spawn(self):
        s = self.session()
        with mock.patch.object(directruntime.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "not installed"):
                directruntime.prepare(s, "project")
        s.opts = {"docker": True}
        with self.assertRaisesRegex(ValueError, "Hole start options"):
            self.prepare(s)

    @unittest.skipUnless(__import__('sys').version_info >= (3,11), 'native TOML inspection requires Python 3.11')
    def test_codex_keeps_personal_instructions_hooks_and_mcp_namespace(self):
        import tomllib
        s=self.session('codex')
        native={'developer_instructions':'Keep native project rules.', 'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo personal-hook'}]}]}, 'mcp_servers':{'piper_session_'+s.id.replace('-','_'):{'command':'existing'}}}
        with mock.patch.object(directruntime,'codex_settings',return_value=native):
            argv,_=self.prepare(s)
        overrides=[tomllib.loads(argv[i+1]) for i,value in enumerate(argv[:-1]) if value=='-c']
        instructions=next(x['developer_instructions'] for x in overrides if 'developer_instructions' in x)
        self.assertTrue(instructions.startswith('Keep native project rules.'))
        hooks=next(x['hooks']['Stop'] for x in overrides if 'Stop' in x.get('hooks',{}))
        self.assertEqual(hooks[0]['hooks'][0]['command'],'echo personal-hook')
        self.assertEqual(len(hooks),2)
        mcp=next(x['mcp_servers'] for x in overrides if 'mcp_servers' in x)
        self.assertFalse(set(mcp)&set(native['mcp_servers']))

    def test_direct_lifecycle_uses_pty_without_hole_or_credential_copy(self):
        m = _env.manager(self, background=False)
        s = self.session(task="work")
        m.sessions[s.id] = s
        self.addCleanup(m.sessions.clear)
        m.pty = mock.Mock()
        m.pty.call.return_value = {"ok": True, "pid": 123, "sessions": []}
        with mock.patch.object(directruntime.shutil, "which", return_value="/usr/bin/fake-agent"), \
                mock.patch.object(sessions.doctor, "check_hole", side_effect=AssertionError("Hole used")):
            m._start(s)
        self.assertEqual(s.state, "starting", s.detail)
        spawn = m.pty.call.call_args_list[0]
        self.assertEqual(spawn.args, ("spawn",))
        self.assertFalse(spawn.kwargs["locked"])
        self.assertEqual(spawn.kwargs["cwd"], s.path)
        self.assertTrue(s.task_sent)
        event = {"ev": "UserPromptSubmit", "session_id": "12345678-abcd", "ts": time.time()}
        common.safe_write(s.box, s.names["events"], (json.dumps(event) + "\n").encode())
        with mock.patch.object(m, "_turn_event"), mock.patch.object(m, "ts_prompt_event"):
            m._events_read(s)
        self.assertEqual(s.state, "working")
        self.assertFalse(m._login_copy_back(s))
        saved = sessions.Session(**s.to_json())
        self.assertEqual(saved.runtime, "direct")

    def test_native_auth_failure_after_start_is_attention_and_is_not_replayed(self):
        import base64
        m = _env.manager(self, background=False)
        s = self.session('codex')
        s.state = 'working'
        m.sessions[s.id] = s
        self.addCleanup(m.sessions.clear)
        def output(text):
            m._on_pty_event(dict(ev='data',id=s.id,data=base64.b64encode(text.encode()).decode()))
        output('Your access token could not be ')
        self.assertEqual(s.state,'working')
        output('\x1b[31mrefreshed.\x1b[0m Please log out and sign in again.')
        self.assertEqual(s.state,'attention')
        self.assertEqual(s.phase,'agent-auth')
        self.assertIn('codex login',s.detail)
        # A subsequent real prompt/hook owns activity state. Old scrollback must not
        # make a recovered session demand authentication again.
        s.state,s.phase,s.detail='working','',''
        output('Working on the new prompt')
        self.assertEqual(s.state,'working')
        self.assertEqual(s.detail,'')

    def test_controller_reconnect_keeps_future_native_output_without_replaying_history(self):
        m = _env.manager(self, background=False)
        s = self.session('codex');s.state='working';s.pid=123
        m.sessions[s.id]=s
        self.addCleanup(m.sessions.clear)
        client=mock.Mock()
        client.call.side_effect=lambda op,**kw: {'sessions':[dict(id=s.id,alive=True,pid=123,bytes=987)]} if op=='list' else {'ok':True}
        m._connect_fn=lambda *args:client
        with mock.patch.object(m,'_boot_changed',return_value=False):m._connect()
        client.call.assert_any_call('subscribe',id=s.id,**{'from':987})
        self.assertFalse(any(c.kwargs.get('data') is False for c in client.call.call_args_list))
        self.assertEqual(s.state,'working')

    def test_codex_hook_review_is_attention_until_a_real_main_prompt(self):
        import base64
        m=_env.manager(self,background=False)
        s=self.session('codex');s.state='starting';s.phase='agent'
        m.sessions[s.id]=s;self.addCleanup(m.sessions.clear)
        def output(text):m._on_pty_event(dict(ev='data',id=s.id,data=base64.b64encode(text.encode()).decode()))
        output('loading · Ask Codex to do anything · ~/repo\n')
        self.assertEqual(s.state,'ready')
        output('Hooks need review\n11 hooks are new or changed.\n1. Review hooks\n2. Trust all and continue\nenter confirm · esc skip')
        self.assertEqual(s.state,'attention')
        self.assertIn('terminal',s.detail)
        self.assertTrue(m._dialog_up(s))
        m.pty=mock.Mock()
        with self.assertRaisesRegex(ValueError,'dialog'):m.send(s.id,'Do not submit this into hook trust')
        m.pty.call.assert_not_called()
        output('\nHooks\n11 hooks need review before they can run.\nt trust all · enter review · esc close')
        self.assertEqual(s.state,'attention')
        output('\nContext 0% used · codex · ~/repo')
        self.assertEqual(s.state,'ready')
        s.state='waiting'
        output('\nAsk Codex to do anything · GPT-6-Luna default · ~/repo')
        self.assertEqual(s.state,'waiting')

    def test_codex_0164_folder_access_blocks_input_after_loading_prompt(self):
        import base64
        m = _env.manager(self, background=False)
        s = self.session('codex'); s.state = 'starting'; s.phase = 'agent'
        m.sessions[s.id] = s; self.addCleanup(m.sessions.clear)
        def output(text):
            m._on_pty_event(dict(ev='data', id=s.id, data=base64.b64encode(text.encode()).decode()))
        output('loading  › Ask Codex to do anything  ~/repo')
        self.assertEqual(s.state, 'ready')
        output('Folder access  /srv/fixture  Trust this folder? Codex can read, edit, and run files here. '
               '› 1. Trust and continue  2. Quit  enter continue · esc quit')
        self.assertEqual(s.state, 'attention')
        self.assertTrue(m._dialog_up(s))
        m.pty = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'dialog'):
            m.send(s.id, 'Do not submit a prompt into folder trust')
        m.pty.call.assert_not_called()
        output('› Ask Codex to do anything  ~/repo')
        self.assertEqual(s.state, 'ready')
        self.assertFalse(m._dialog_up(s))

    def test_codex_0164_ready_prompt_after_hook_review(self):
        review='Hooks need review\n11 hooks are new or changed.\nenter confirm · esc skip'
        prompt='› Ask Codex to do anything\nGPT-6-Luna default · ~/repo'
        self.assertEqual(sessions.claude_screen(prompt+review,'codex'),'dialog')
        self.assertEqual(sessions.claude_screen(review+prompt,'codex'),'ready')

    def test_codex_late_hook_trust_binds_conversation_without_requiring_session_start(self):
        m = _env.manager(self, background=False)
        for first in ('UserPromptSubmit', 'Stop'):
            with self.subTest(first=first):
                s = self.session('codex'); s.state = 'ready'
                m.sessions[s.id] = s
                os.makedirs(s.box, exist_ok=True)
                event = dict(ev=first, session_id='12345678-abcd', ts=time.time())
                common.safe_write(s.box, s.names['events'], (json.dumps(event) + '\n').encode())
                with mock.patch.object(m, '_turn_event'), mock.patch.object(m, 'ts_prompt_event'):
                    m._events_read(s)
                self.assertEqual(s.claude_session, '12345678-abcd')
                # Later events from another thread cannot replace the bound conversation.
                event.update(ev='Stop', session_id='87654321-dcba')
                previous = common.safe_read(s.box, s.names['events'], 65536)
                common.safe_write(s.box, s.names['events'], previous + (json.dumps(event) + '\n').encode())
                with mock.patch.object(m, '_turn_event'), mock.patch.object(m, 'ts_prompt_event'):
                    m._events_read(s)
                self.assertEqual(s.claude_session, '12345678-abcd')
                m.sessions.pop(s.id)
        self.addCleanup(m.sessions.clear)

    def test_native_prompt_hooks_clear_startup_dialog_phase_before_idle_notification(self):
        m=_env.manager(self,background=False)
        s=self.session('codex');s.state='attention';s.phase='agent'
        m.sessions[s.id]=s;self.addCleanup(m.sessions.clear)
        events=[dict(ev=ev,session_id='12345678-abcd',ts=time.time(),**kw) for ev,kw in (
            ('UserPromptSubmit',{}),('Stop',{}),('Notification',dict(notification_type='idle_prompt',message='Waiting for your input')))]
        os.makedirs(s.box,exist_ok=True)
        common.safe_write(s.box,s.names['events'],(''.join(json.dumps(e)+'\n' for e in events)).encode())
        with mock.patch.object(m,'_turn_event'),mock.patch.object(m,'ts_prompt_event'):m._events_read(s)
        self.assertEqual(s.state,'attention')
        self.assertEqual(s.phase,'')
        self.assertFalse(m._dialog_up(s))
