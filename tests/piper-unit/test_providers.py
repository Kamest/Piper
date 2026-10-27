"""Laptop provider capabilities, durable write reservations, revocation and local MCP scope."""
import json
import os
import tempfile
import unittest
from unittest import mock

import _env
import common
import providers
import remotes
import sessions
import directruntime


class ProvidersTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='provider-',dir=_env.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.file=mock.patch.object(providers,'FILE',os.path.join(self.tmp.name,'grants.json'))
        self.file.start();self.addCleanup(self.file.stop)
        self.project='provider-tests/project'
        self.path=_env.make_project('provider-project',{'file.http':'GET http://localhost\n'})
        _env.register(self.project,self.path)
        self.grant=dict(id=__import__('uuid').uuid4().hex,token='capability-placeholder',enabled=True,environment='dev',remote_project='server/repo',local_project=self.project)
        common.write_json(providers.FILE,{'grants':[self.grant]})
        self.manager=mock.Mock(sessions={}, pending_list=lambda **kwargs: [], pending_gone_list=lambda **kwargs: [])
        self.manager.lock=__import__('threading').RLock()
        self.manager.tool.return_value={'ran':True}
        self.body=dict(id='b'*32,session='12345678',tool='http_run',args={'file':'file.http','env':'test'})
        self.addCleanup(lambda: [__import__('shutil').rmtree(s.dir,ignore_errors=True) for s in self.manager.sessions.values()])

    def invoke(self,body=None,token='capability-placeholder'):
        return providers.invoke(self.manager,self.grant['id'],token,body or self.body)

    def test_duplicate_write_returns_cached_reply_and_durable_reservation_never_reexecutes(self):
        first=self.invoke()
        second=self.invoke()
        self.assertEqual(first,second)
        self.assertTrue(first['ok'])
        self.manager.tool.assert_called_once()
        self.assertEqual(self.manager.tool.call_args.args[0].project,self.project)
        self.assertEqual(self.manager.tool.call_args.args[0].runtime,'direct')
        providers.REPLIES.clear()
        self.assertFalse(self.invoke()['ok'])
        self.manager.tool.assert_called_once()
        with self.assertRaisesRegex(ValueError,'different arguments'):
            self.invoke(dict(self.body,args={'file':'other.http'}))

    def test_crash_after_reservation_yields_unknown_and_does_not_replay(self):
        self.manager.tool.side_effect=SystemExit('simulated daemon loss')
        with self.assertRaises(SystemExit): self.invoke()
        self.manager.tool.side_effect=None
        result=self.invoke()
        self.assertFalse(result['ok'])
        self.assertIn('not replayed',result['error'])
        self.manager.tool.assert_called_once()

    def test_unreadable_reservation_fails_closed_and_fsync_precedes_execution(self):
        self.manager.tool.side_effect=SystemExit('simulated crash')
        real_sync=os.fsync
        with mock.patch.object(providers.os,'fsync',wraps=real_sync) as sync:
            with self.assertRaises(SystemExit): self.invoke()
            self.assertGreaterEqual(sync.call_count,4)
        path=os.path.join(common.PIPER_HOME,'provider-calls',self.grant['id'],self.body['id']+'.json')
        with open(path,'w') as stream: stream.write('{broken')
        self.manager.tool.side_effect=None
        response=self.invoke()
        self.assertFalse(response['ok'])
        self.assertIn('not replayed',response['error'])
        self.manager.tool.assert_called_once()

    def test_failed_durable_reservation_never_executes_the_tool(self):
        with mock.patch.object(providers.os,'fsync',side_effect=OSError('storage failure')):
            with self.assertRaises(OSError): self.invoke()
        self.manager.tool.assert_not_called()

    def test_scope_rejects_other_projects_shell_tools_and_invalid_tokens(self):
        for body in (dict(self.body,tool='git_run'),dict(self.body,args={'project':'other/repo'}),dict(self.body,id='../x')):
            with self.assertRaises(ValueError):self.invoke(body)
        with self.assertRaises(PermissionError):self.invoke(token='invalid')
        self.manager.tool.assert_not_called()

    def test_revocation_is_immediate_even_if_remote_config_update_failed(self):
        entry=dict(id='dev',host='dev',project_path='/srv',mode='service')
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'request',side_effect=remotes.Unavailable('offline')):
            with self.assertRaises(remotes.Unavailable):
                providers.configure(dict(environment='dev',remote_project='server/repo',local_project=self.project,enabled=False))
        with self.assertRaises(PermissionError):self.invoke()

    def test_rotation_cancels_pending_old_grant_without_reusing_its_session_identity(self):
        self.invoke()
        session=next(iter(self.manager.sessions.values()))
        event=__import__('threading').Event()
        self.manager.pending={'approval':dict(session=session.id,_ev=event)}
        entry=dict(id='dev',host='dev',project_path='/srv',mode='service')
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'request',return_value={}),mock.patch.object(providers,'ensure_tunnel'):
            providers.configure(dict(environment='dev',remote_project='server/repo',local_project=self.project,enabled=True),self.manager)
        self.assertTrue(event.is_set())
        self.assertFalse(self.manager.pending['approval']['_ok'])
        self.assertFalse(session._provider_active)
        new=providers.grants()[0]
        self.assertNotEqual(new['id'],self.grant['id'])
        self.assertNotEqual(new['token'],self.grant['token'])
        with self.assertRaises(PermissionError):self.invoke()

    def test_offline_revoke_closes_only_the_last_provider_tunnel(self):
        entry=dict(id='dev',host='dev',project_path='/srv',mode='service')
        for another in (False,True):
            with self.subTest(another_mapping=another):
                rows=[self.grant]
                if another:
                    rows.append(dict(self.grant,id='other-grant',remote_project='other/repo'))
                common.write_json(providers.FILE,{'grants':rows})
                tunnel=mock.Mock()
                tunnel.poll.return_value=None
                with mock.patch.dict(providers.TUNNELS,{'dev':tunnel},clear=True), \
                     mock.patch.object(remotes,'get',return_value=entry), \
                     mock.patch.object(remotes,'request',side_effect=remotes.Unavailable('offline')) as request:
                    with self.assertRaises(remotes.Unavailable):
                        providers.configure(dict(environment='dev',remote_project='server/repo',local_project=self.project,enabled=False))
                    request.assert_called_once()
                    if another:
                        tunnel.terminate.assert_not_called()
                        self.assertIn('dev',providers.TUNNELS)
                    else:
                        tunnel.terminate.assert_called_once()
                        tunnel.wait.assert_called_once_with(timeout=5)
                        self.assertNotIn('dev',providers.TUNNELS)

    def test_removed_local_project_does_not_prevent_revocation(self):
        entry=dict(id='dev',host='dev',project_path='/srv',mode='service')
        body=dict(environment='dev',remote_project='server/repo',local_project=self.project)
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'request',return_value={}), \
             mock.patch.object(providers,'ensure_tunnel'),mock.patch.object(providers.projects,'find',return_value=None):
            self.assertFalse(providers.configure(dict(body,enabled=False))['enabled'])
            with self.assertRaisesRegex(ValueError,'registered local project'):
                providers.configure(dict(body,enabled=True))
        with self.assertRaises(PermissionError):self.invoke()

    def test_provider_approval_is_projected_once_without_a_local_duplicate(self):
        import time
        m = _env.manager(self, background=False)
        identity = 'provider-dev-' + self.grant['id'] + '-12345678'
        m.pending = {
            'own': dict(id='own', session=identity, title='Project', kind='http_host',
                        summary='Fixture request', created=time.time(), payload={}),
            'ordinary': dict(id='ordinary', session='local-session', title='Local', kind='http_host',
                             summary='Local request', created=time.time(), payload={}),
        }
        self.addCleanup(m.pending.clear)
        self.assertEqual([p['id'] for p in m.pending_list()], ['ordinary'])
        self.assertEqual(len(m.pending_list(include_providers=True)), 2)
        m.pending_gone = [dict(id='gone', session=identity, title='Project', kind='http_host', at=time.time()),
                          dict(id='ordinary-gone', session='local-session', title='Local', kind='http_host', at=time.time())]
        self.assertEqual([p['id'] for p in m.pending_gone_list()], ['ordinary-gone'])
        projected = {'pending': [], 'queue': {'actions': []}}
        providers.decorate_state(m, 'dev', projected)
        self.assertEqual([p['id'] for p in projected['pending_gone']], ['local-gone'])
        self.assertEqual(projected['pending_gone'][0]['session'], '12345678')
        self.assertEqual([p['id'] for p in providers.pending(m, 'dev')], ['local-own'])
        self.assertFalse(any(a.get('subject', {}).get('session', '').startswith('provider-')
                             for a in m.queue_summary()['actions']))

    def test_provider_execution_context_is_hidden_from_regular_session_feed(self):
        self.invoke()
        s = next(iter(self.manager.sessions.values()))
        self.assertTrue(s.hidden)
        import feed
        self.assertTrue(feed.session_snap(s)['hidden'])

    def test_pending_cards_are_scoped_to_machine_and_label_local_execution(self):
        self.manager.pending_list=lambda **kwargs:[dict(id='abc',session='provider-dev-'+self.grant['id']+'-12345678',title='Project'),dict(id='def',session='provider-other-'+self.grant['id']+'-12345678',title='Other')]
        rows=providers.pending(self.manager,'dev')
        self.assertEqual(rows,[dict(id='local-abc',session='12345678',title='Local tools · Project',local_provider=True)])

    def test_direct_agents_get_distinct_local_mcp_with_no_gui_token(self):
        file=os.path.join(common.PIPER_HOME,'local-providers.json')
        old=common.read_json(file,None)
        self.addCleanup(lambda: common.write_json(file,old or {}))
        common.write_json(file,{self.project:dict(enabled=True,url='http://127.0.0.1:17897/api/local-provider/grant',token='scoped-placeholder')})
        for agent in ('claude','codex'):
            s=sessions.Session(project=self.project,path=self.path,runtime='direct',agent=agent,claude_mode='host' if agent=='claude' else 'none',host_codex=agent=='codex')
            self.addCleanup(__import__('shutil').rmtree,s.dir,True)
            with mock.patch.object(directruntime.shutil,'which',return_value='/usr/bin/fake-agent'), mock.patch.object(directruntime,'codex_settings',return_value={'mcp_servers':{'piper_local':{'command':'personal'}}}):
                argv,env=directruntime.prepare(s,'session')
            cfg=common.read_json(os.path.join(s.box,'piper/mcp.json'),{})['mcpServers']
            self.assertIn('piper_local',cfg)
            if agent == 'codex':
                self.assertTrue(any(x.startswith('mcp_servers.piper_local_session=') for x in argv))
            self.assertNotIn('scoped-placeholder',' '.join(argv))
            self.assertNotIn('scoped-placeholder',str(env))
            self.assertEqual(os.stat(os.path.join(s.dir,'local-provider.json')).st_mode & 0o777,0o600)


    def test_linked_worktrees_inherit_only_their_base_project_provider(self):
        import shutil
        file = os.path.join(common.PIPER_HOME, 'local-providers.json')
        old = common.read_json(file, {})
        self.addCleanup(common.write_json, file, old)
        common.write_json(file, {self.project: dict(enabled=True,
            url='http://127.0.0.1:17897/api/local-provider/grant', token='scoped-placeholder')})
        for runtime in ('direct', 'hole'):
            with self.subTest(runtime=runtime):
                s = sessions.Session(project=self.project + '@feature', path=self.path, runtime=runtime)
                self.addCleanup(shutil.rmtree, s.dir, True)
                directruntime.sessioncfg.build_box(s.box, '/home/agent/.claude', '/home/agent/.piper', 'session')
                self.assertIsNotNone(directruntime.local_provider(s))
                self.assertEqual(common.read_json(os.path.join(s.dir, 'local-provider.json'), {})['token'], 'scoped-placeholder')
                s.project = self.project + '-other@feature'
                self.assertIsNone(directruntime.local_provider(s))
                self.assertFalse(os.path.exists(os.path.join(s.dir, 'local-provider.json')))

    def test_hole_provider_paths_are_inside_each_existing_mount_but_token_stays_outside(self):
        import codexcfg
        import shutil
        path=os.path.join(common.PIPER_HOME,'local-providers.json')
        old=common.read_json(path,{})
        self.addCleanup(common.write_json,path,old)
        common.write_json(path,{self.project:dict(enabled=True,url='http://127.0.0.1:17897/api/local-provider/grant',token='scoped-placeholder')})
        for agent,mode,host in [('claude','host',False),('claude','session',False),('codex','none',False),('codex','none',True)]:
            s=sessions.Session(project=self.project,path=self.path,runtime='hole',agent=agent,claude_mode=mode,host_codex=host)
            self.addCleanup(shutil.rmtree,s.dir,True)
            inside='/home/agent/.claude' if mode=='session' else '/home/agent/.piper'
            directruntime.sessioncfg.build_box(s.box,'/home/agent/.claude',inside,'session')
            cfg=directruntime.local_provider(s,inside)
            self.assertEqual(cfg['command'],'python3')
            self.assertEqual(cfg['args'],[inside+'/piper/piper-local-mcp.py','--mailbox',inside+'/piper-mailbox'])
            self.assertNotIn('scoped-placeholder',json.dumps(common.read_json(os.path.join(s.box,'piper/mcp.json'),{})))
            self.assertFalse(os.path.exists(os.path.join(s.box,'piper/local-provider.json')))
            self.assertEqual(common.read_json(os.path.join(s.dir,'local-provider.json'),{})['token'],'scoped-placeholder')
            if agent=='codex':
                import tomllib
                if host:
                    argv=codexcfg.overrides(inside,'session',s.path,'piper-events.jsonl',local=cfg)
                    docs=[tomllib.loads(argv[i+1]) for i,x in enumerate(argv[:-1]) if x=='-c']
                    local=next(v for doc in docs for k,v in doc.get('mcp_servers',{}).items() if k.startswith('piper_local_'))
                    self.assertEqual(local['args'],cfg['args'])
                else:
                    os.makedirs(s.codex_dir)
                    with open(os.path.join(s.codex_dir,'config.toml'),'w') as stream:
                        stream.write(codexcfg.config_toml('[mcp_servers.piper_local]\nurl="https://example.com"',inside,'session',s.path))
                    codexcfg.add_local_provider(s.codex_dir,cfg)
                    with open(os.path.join(s.codex_dir,'config.toml'),'rb') as stream: doc=tomllib.load(stream)
                    self.assertEqual(doc['mcp_servers']['piper_local']['args'],cfg['args'])
                    self.assertNotIn('url',doc['mcp_servers']['piper_local'])
        common.write_json(path,{})
        directruntime.local_provider(s,inside)
        self.assertFalse(os.path.exists(os.path.join(s.dir,'local-provider.json')))

    def test_host_relay_refuses_unconfigured_session_shell_tools_and_injected_provider(self):
        s=sessions.Session(project=self.project,path=self.path,runtime='hole')
        self.addCleanup(__import__('shutil').rmtree,s.dir,True)
        with self.assertRaisesRegex(ValueError,'No local tool provider'):
            providers.forward(s,dict(tool='http_envs',args={}))
        with self.assertRaisesRegex(ValueError,'disallowed tool'):
            providers.forward(s,dict(tool='git_run',args={}))
        os.makedirs(s.dir,exist_ok=True)
        common.write_json(os.path.join(s.dir,'local-provider.json'),dict(enabled=True,url='http://example.com/api/local-provider/x',token='placeholder'))
        with self.assertRaisesRegex(ValueError,'private SSH'):
            providers.forward(s,dict(tool='http_envs',args={}))

    def test_host_relay_lost_reply_never_retries_the_sent_operation(self):
        import http.server
        import threading
        import socket
        seen=[]
        class Drop(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','12');self.end_headers()
                self.wfile.write(b'{"ok": true}')
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Drop)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        s=sessions.Session(project=self.project,path=self.path,runtime='hole')
        self.addCleanup(__import__('shutil').rmtree,s.dir,True)
        os.makedirs(s.dir,exist_ok=True)
        common.write_json(os.path.join(s.dir,'local-provider.json'),dict(enabled=True,url='http://127.0.0.1:%d/api/local-provider/grant'%server.server_port,token='placeholder'))
        with self.assertRaisesRegex(ValueError,'may have completed'):
            providers.forward(s,dict(tool='http_run',args={'file':'file.http'}))
        self.assertEqual(len(seen),1)
        self.assertEqual(seen[0]['session'],s.id)
        self.assertEqual(seen[0]['tool'],'http_run')
        self.assertNotIn('token',seen[0])

    def test_silent_ssh_tunnel_fails_before_sending_an_operation(self):
        import http.client
        import http.server
        import threading
        seen=[]
        release=threading.Event()
        class Silent(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                seen.append('GET');release.wait(3)
            def do_POST(self):
                seen.append('POST');release.wait(3)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Silent)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join,5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        s=sessions.Session(project=self.project,path=self.path,runtime='hole')
        self.addCleanup(__import__('shutil').rmtree,s.dir,True)
        os.makedirs(s.dir,exist_ok=True)
        common.write_json(os.path.join(s.dir,'local-provider.json'),dict(enabled=True,url='http://127.0.0.1:%d/api/local-provider/grant'%server.server_port,token='placeholder'))
        real=http.client.HTTPConnection
        deadlines=[]
        def fast_connection(host,port,timeout):
            deadlines.append(timeout)
            return real(host,port,timeout=min(timeout,.15))
        with mock.patch.object(http.client,'HTTPConnection',side_effect=fast_connection):
            with self.assertRaisesRegex(ValueError,'No operation was sent'):
                providers.forward(s,dict(tool='http_run',args={'file':'file.http'}))
        self.assertEqual(seen,['GET'])
        self.assertLessEqual(deadlines[0],5)

    def test_real_hole_start_preparation_attaches_provider_in_all_config_modes(self):
        import shutil
        manager=_env.manager(self,background=False)
        path=os.path.join(common.PIPER_HOME,'local-providers.json')
        old=common.read_json(path,{})
        self.addCleanup(common.write_json,path,old)
        common.write_json(path,{self.project:dict(enabled=True,url='http://127.0.0.1:17897/api/local-provider/grant',token='scoped-placeholder')})
        for agent in ('claude','codex'):
            for mode in ('host','session'):
                with self.subTest(agent=agent,mode=mode), mock.patch.dict(manager.policy,{'claude_dir':mode,'codex_dir':mode,'codex_in_claude':False}), \
                        mock.patch.object(manager,'_base_settings',return_value=({},[])), \
                        mock.patch.object(manager,'_preflight',return_value={'ok':True,'coverage':[],'settings':{},'effective':{},'blocks':[]}), \
                        mock.patch.object(manager,'_collect_logins'), \
                        mock.patch.object(manager,'_argv',side_effect=RuntimeError('stop before spawning')):
                    s=sessions.Session(project=self.project,path=self.path,runtime='hole',agent=agent)
                    self.addCleanup(shutil.rmtree,s.dir,True)
                    manager._start(s)
                    self.assertIn('stop before spawning',s.detail)
                    cfg=common.read_json(os.path.join(s.box,'piper/mcp.json'),{})['mcpServers']['piper_local']
                    self.assertEqual(cfg['args'],[manager.box_in(s)+'/piper/piper-local-mcp.py','--mailbox',manager.box_in(s)+'/piper-mailbox'])
                    self.assertTrue(os.path.isfile(os.path.join(s.dir,'local-provider.json')))
                    if agent=='codex' and mode=='session':
                        import tomllib
                        with open(os.path.join(s.codex_dir,'config.toml'),'rb') as stream: doc=tomllib.load(stream)
                        self.assertEqual(doc['mcp_servers']['piper_local']['args'],cfg['args'])


    def test_notebook_approvals_reach_remote_queue_without_persistent_grant_buttons(self):
        self.manager.pending_list=lambda **kwargs:[dict(id='abc',kind='http_host',created=1,summary='Fixture request',session='provider-dev-'+self.grant['id']+'-12345678',title='Project',always='Always',session_always='Session',similar='this host')]
        state={'pending':[{'id':'server'}],'queue':{'actions':[{'key':'wait','level':'waiting','subject':{'key':'12345678'}},{'key':'other','level':'blocked'}]}}
        providers.decorate_state(self.manager,'dev',state)
        self.assertEqual([p['id'] for p in state['pending']],['server','local-abc'])
        self.assertTrue(state['pending'][1]['local_provider'])
        self.assertNotIn('always',state['pending'][1])
        self.assertNotIn('session_always',state['pending'][1])
        self.assertNotIn('similar',state['pending'][1])   # nor the hour grant of matching requests
        self.assertEqual([a['key'] for a in state['queue']['actions']],['other','p:local-abc'])
        self.assertEqual(state['queue']['actions'][1]['subject']['key'],'12345678')
        self.assertTrue(all(a['args']['id']=='local-abc' for a in state['queue']['actions'][1]['actions'] if a['op']=='decide'))

    def test_hyphenated_environment_ids_do_not_share_decisions_or_grant_prefixes(self):
        other=dict(self.grant,id='c'*32,environment='dev-west')
        common.write_json(providers.FILE,{'grants':[self.grant,other]})
        own='provider-dev-'+self.grant['id']+'-12345678'
        foreign='provider-dev-west-'+other['id']+'-87654321'
        self.manager.pending_list=lambda **kwargs:[dict(id='own',session=own,title='Own'),dict(id='other',session=foreign,title='Other')]
        self.assertEqual([p['id'] for p in providers.pending(self.manager,'dev')],['local-own'])
        self.assertEqual([p['id'] for p in providers.pending(self.manager,'dev-west')],['local-other'])
        self.assertEqual(providers.remote_session('dev',own),'12345678')
        self.assertIsNone(providers.remote_session('dev',foreign))
        self.assertIsNone(providers.remote_session('dev', 'provider-dev-'+'d'*32+'-12345678'))
