"""Install a real server release and exercise its daemon/PTY across controller restarts.

Systemd activation is stubbed on macOS. Runtime executes the matching read-only source
checkout: the mandatory sandbox refuses importing application code from writable fixtures.
PIPER_REMOTE_TEST_RELEASE optionally selects an externally prepared read-only release.
"""
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import _env
import common
import remotepeer
import remotes
from ptyclient import PtyClient


class RemoteServiceTest(unittest.TestCase):
    def test_installed_release_claude_survives_controller_restart(self):
        self.exercise_release("claude")

    @unittest.skipUnless(sys.version_info >= (3,11), "effective Codex config fixture requires tomllib")
    def test_installed_release_codex_survives_controller_restart(self):
        self.exercise_release("codex")

    def exercise_release(self, agentname):
        with tempfile.TemporaryDirectory(prefix='rs-') as directory:
            home=Path(directory)
            app=home/'app'; state=home/'state'; root=home/'projects'; project=root/'repo'
            project.mkdir(parents=True)
            subprocess.run(['git','init','-q',str(project)],check=True)
            (project/'hello.txt').write_text('server-owned file\n')
            subprocess.run(['git','-C',str(project),'add','hello.txt'],check=True)
            subprocess.run(['git','-C',str(project),'commit','-qm','initial'],check=True)
            binary=home/'bin';binary.mkdir()
            agent=binary/agentname
            agent.write_text('#!'+sys.executable+'\nimport json,os,shlex,subprocess,sys,time\n'
                'if "--help" in sys.argv: print("--no-daemon");sys.exit(0)\n'
                'if "--settings" in sys.argv: cfg=json.load(open(sys.argv[sys.argv.index("--settings")+1]))\n'
                'else:\n'
                ' cfg={"hooks":{}}\n'
                ' for i,arg in enumerate(sys.argv[:-1]):\n'
                '  if arg=="-c" and sys.argv[i+1].startswith("hooks."):\n'
                '   cfg["hooks"].update(__import__("tomllib").loads(sys.argv[i+1])["hooks"])\n'
                'for ev in ("SessionStart","UserPromptSubmit"):\n'
                ' command=cfg["hooks"][ev][0]["hooks"][0]["command"]\n'
                ' subprocess.run(shlex.split(command),input=json.dumps({"session_id":"12345678-abcd","source":"startup","prompt":"test"}).encode(),check=True)\n'
                'print("server-agent-running",flush=True)\ntime.sleep(60)\n')
            agent.chmod(0o700)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            raw,sha=remotes.bundle()
            if agentname == 'claude':
                state.mkdir()
                (state/'features.json').write_text(json.dumps({'v':2,'base':'basic','modules':{'codex':False},'onboarded':True}))
            real_run=remotepeer.run
            def systemd(argv,**kw):
                if argv[0] in ('systemctl','loginctl'):
                    return subprocess.CompletedProcess(argv,0,stdout=b'yes\n',stderr=b'')
                if argv[:2] == [sys.executable,'-c']:
                    argv=list(argv)
                    # Use the sandbox's read-only source for minting, not writable test code.
                    argv[2]=argv[2].replace(str(app/'current/piper'),common.PIPER_DIR)
                    for release in app.glob('release-*'):
                        argv[2]=argv[2].replace(str(release/'piper'),common.PIPER_DIR)
                result=real_run(argv,**kw)
                self.assertEqual(result.returncode,0,result.stderr.decode())
                return result
            env=dict(os.environ,PIPER_CLAUDE_BIN=str(binary/'claude'),PIPER_CODEX_BIN=str(binary/'codex'),HOME=str(home),PIPER_REAL_HOME=str(home),PATH=str(binary)+':'+os.environ['PATH'])
            processes=[]
            log=(home/'daemon.log').open('ab')
            self.addCleanup(log.close)
            def start():
                release=os.environ.get('PIPER_REMOTE_TEST_RELEASE') or common.SK
                serverenv=dict(env,PIPER_HOME=str(state),PIPER_PTY_SOCK=str(state/'ptyhost.sock'),PIPER_SERVICE_LABEL='piper-server',PIPER_DEFAULT_RUNTIME='direct',PIPER_PORT=str(port),PIPER_BIND='127.0.0.1',PROJECT_PATH=str(root))
                p=subprocess.Popen([sys.executable,os.path.join(release,'piper/piperd.py')],env=serverenv,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
                processes.append(p)
                deadline=time.time()+10
                while time.time()<deadline:
                    try:
                        r=remotepeer.http(dict(mode='service',project_path=str(root),method='GET',path='/api/state',body='',ctype='application/json'))
                        if r['status']==200:return p,json.loads(base64.b64decode(r['body']))
                    except (OSError,ValueError):pass
                    if p.poll() is not None:break
                    time.sleep(.1)
                self.fail('installed server did not start: '+(home/'daemon.log').read_text()[-3000:])
            def request(method,path,body=None):
                r=remotepeer.http(dict(mode='service',project_path=str(root),method=method,path=path,body=base64.b64encode(json.dumps(body or {}).encode()).decode(),ctype='application/json'))
                return r['status'],json.loads(base64.b64decode(r['body']))
            real_which=__import__('shutil').which
            with mock.patch.multiple(remotepeer,HOME=home,APP=app,STATE=state,PORT=port), \
                    mock.patch.dict(os.environ,env,clear=True), \
                    mock.patch.object(remotepeer.sys,'platform','linux'), \
                    mock.patch.object(remotepeer.shutil,'which',side_effect=lambda name: '/bin/'+name if name in ('systemctl','loginctl') else real_which(name)), \
                    mock.patch.object(remotepeer,'run',side_effect=systemd):
                result=remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(root)))
                self.assertTrue(result['installed'])
                self.assertTrue(result['autonomous_after_logout'])
                self.assertNotIn('token',result)
                try:
                    p,st=start()
                    self.assertEqual(st['default_runtime'],'direct')
                    self.assertFalse(st['features']['modules']['codex'], 'installation never rewrites module preferences')
                    self.assertTrue(st['agent_availability'][agentname])
                    agent.rename(binary / (agentname + '.aside'))
                    self.assertFalse(request('GET','/api/state')[1]['agent_availability'][agentname])
                    code, missing = request('POST','/api/sessions',dict(project='repo',runtime='direct',agent=agentname))
                    self.assertEqual(code,409,missing)
                    self.assertIn('not installed',missing['error'])
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    (binary / (agentname + '.aside')).rename(agent)
                    self.assertTrue(request('GET','/api/state')[1]['agent_availability'][agentname])
                    self.assertEqual(st['projects'][0]['path'],str(project))
                    self.assertTrue(remotepeer.probe({'project_path': str(root) + '/'})['project_root_matches'])
                    for method in ('GET', 'POST'):
                        with self.assertRaisesRegex(ValueError, 'Projects folder differs'):
                            remotepeer.http(dict(mode='service',project_path=str(home/'other-projects'),method=method,
                                path='/api/state' if method=='GET' else '/api/sessions',body='',ctype='application/json'))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'],[])
                    code,row=request('POST','/api/sessions',dict(project='repo',task='test native hooks',runtime='direct',agent=agentname,remember_for_project=True))
                    self.assertEqual(code,202,row)
                    self.assertEqual(row['project_defaults']['agent'],agentname)
                    sid=row['id']
                    deadline=time.time()+8
                    while time.time()<deadline:
                        st=request('GET','/api/state')[1]
                        s=next(x for x in st['sessions'] if x['id']==sid)
                        if s['state']=='working':break
                        if s['state']=='error':self.fail(s['detail'])
                        time.sleep(.1)
                    self.assertEqual(s['state'],'working',s)
                    self.assertEqual(s['runtime'],'direct')
                    self.assertEqual(s['claude_session'],'12345678-abcd')
                    original_pid=s['pid']
                    other = home / 'other-projects'; other.mkdir()
                    with self.assertRaisesRegex(remotepeer.RebindRefused, 'sessions are active'):
                        remotepeer.install(dict(archive=base64.b64encode(raw).decode(),sha=sha,project_path=str(other)))
                    self.assertEqual(request('GET','/api/state')[1]['sessions'][0]['pid'], original_pid)
                    code,data=request('GET','/api/fs/read?project=repo&path=hello.txt')
                    self.assertEqual(code,200,data)
                    self.assertIn('server-owned file','\n'.join(data['lines']))
                    self.assertEqual(request('GET',f'/api/sessions/{sid}/git')[0],200)
                    if agentname == 'claude':
                        self.exercise_local_provider(home, state, project, port, env, sid)
                    # Restart only the throwaway controller, never the user's daemon/ptyhost.
                    p.terminate();p.wait(timeout=5)
                    p,st=start()
                    restored=next(x for x in st['sessions'] if x['id']==sid)
                    self.assertEqual(restored['pid'],original_pid)
                    self.assertIn(restored['state'],('working','ready','waiting'))
                    os.kill(original_pid,0)
                    pc=PtyClient(str(state/'ptyhost.sock'))
                    try:
                        reply=pc.call('subscribe',id=sid)
                        self.assertTrue(reply['ok'])
                        self.assertTrue(reply['info']['alive'])
                    finally:pc.close()
                    self.exercise_terminal_bridge(state, port, sid, original_pid)
                    self.assertEqual(request('POST',f'/api/sessions/{sid}/stop')[0],200)
                finally:
                    # Terminate children through the private test PTY socket and reap its test host.
                    try:
                        pc=PtyClient(str(state/'ptyhost.sock'))
                        hostpid=pc.call('ping')['pid']
                        for row in pc.call('list').get('sessions',[]):
                            if row['alive']:pc.call('signal',id=row['id'],sig='KILL')
                        pc.close()
                        os.kill(hostpid,signal.SIGTERM)
                    except OSError:pass
                    for p in processes:
                        if p.poll() is None:p.terminate();p.wait(timeout=5)
                    pidfile=state/'ptyhost.pid'
                    if pidfile.exists():
                        pid=int(pidfile.read_text());os.kill(pid,signal.SIGTERM)
                    log.close()

    def exercise_terminal_bridge(self, state, port, sid, agent_pid):
        """Actual binary PTY protocol over peer stdio; SSH process creation alone is substituted."""
        identity=remotepeer.peer_identity()
        entry=dict(id='fixture',host='fixture',project_path=str(state.parent/'projects'),mode='service',identity=identity)
        source=remotes.peer_source().replace("STATE = HOME / '.piper-server'",'STATE = Path('+repr(str(state))+')').replace('PORT = 17896','PORT = '+str(port))
        def local_transport(entry,command,**kw):
            return [sys.executable,*command[1:]]
        with mock.patch.object(remotes,'get',return_value=entry),mock.patch.object(remotes,'peer_source',return_value=source),mock.patch.object(remotes,'ssh',side_effect=local_transport):
            bridge=remotes.terminal('fixture',sid,lambda event: None)
            try:
                reply=bridge.call('subscribe',id=sid)
                self.assertTrue(reply['ok'])
                self.assertTrue(reply['info']['alive'])
                self.assertTrue(bridge.call('resize',id=sid,cols=100,rows=30)['ok'])
            finally: bridge.close()
        # Closing the client/transport is independent of the server-owned agent lifetime.
        os.kill(agent_pid,0)

    def exercise_local_provider(self, home, state, project, port, env, sid):
        """Real stdio MCP -> session mailbox -> host capability HTTP -> existing project HTTP tools, no mocked Manager."""
        (project/'http-client.env.json').write_text(json.dumps({'test':{'host':'http://127.0.0.1:9'}}))
        (project/'http-client.private.env.json').write_text(json.dumps({'test':{'token':'PLANTED_PROVIDER_SECRET'}}))
        grant=dict(id='a'*32,token='test-capability-placeholder',enabled=True,environment='fixture',remote_project='server/repo',local_project='repo')
        path=state/'provider-grants.json'
        path.write_text(json.dumps({'grants':[grant]})); path.chmod(0o600)
        config=state/'sessions'/sid/'local-provider.json'
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        config.chmod(0o600)
        wrapper=os.path.join(common.PIPER_DIR,'sandbox/piper-local-mcp.py')
        def mcp(messages):
            result=subprocess.run([sys.executable,wrapper,'--mailbox',str(state/'sessions'/sid/'piper/piper-mailbox')],input=''.join(json.dumps(m)+'\n' for m in messages),text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('PLANTED_PROVIDER_SECRET',result.stdout)
            return [json.loads(line)['result'] for line in result.stdout.splitlines()]
        call=dict(jsonrpc='2.0',id=2,method='tools/call',params=dict(name='http_envs',arguments={}))
        listed,reply,denied=mcp([dict(jsonrpc='2.0',id=1,method='tools/list'),call,dict(call,id=3,params=dict(name='git_run',arguments={}))])
        names={t['name'] for t in listed['tools']}
        self.assertIn('http_run',names)
        self.assertIn('db_query',names)
        self.assertNotIn('git_run',names)
        self.assertFalse(reply['isError'],reply)
        self.assertIn('test',reply['content'][0]['text'])
        self.assertTrue(denied['isError'])
        # Reverse SSH preserves the tunnel port in HTTP Host. Forward real bytes
        # through a distinct loopback port, while the private config names the
        # controller's expected Host. Do not weaken its global Host/Origin gate.
        import socketserver, selectors, threading
        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection(('127.0.0.1',port)) as upstream, selectors.DefaultSelector() as selector:
                    selector.register(self.request,selectors.EVENT_READ,upstream)
                    selector.register(upstream,selectors.EVENT_READ,self.request)
                    while True:
                        for key,_ in selector.select(timeout=5):
                            data=key.fileobj.recv(65536)
                            if not data:return
                            key.data.sendall(data)
        proxy=socketserver.ThreadingTCPServer(('127.0.0.1',0),Forward)
        proxy.daemon_threads=True
        thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
        try:
            config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{proxy.server_address[1]}/api/local-provider/'+grant['id'],host=f'127.0.0.1:{port}',token=grant['token'])))
            tunneled=mcp([call])[0]
            self.assertFalse(tunneled['isError'],tunneled)
            self.assertIn('test',tunneled['content'][0]['text'])
        finally:
            proxy.shutdown();proxy.server_close();thread.join(5)
        records=list((state/'provider-calls'/grant['id']).glob('*.json'))
        self.assertEqual(len(records),2)
        self.assertEqual(json.loads(records[0].read_text())['state'],'completed')
        self.assertNotIn('PLANTED_PROVIDER_SECRET',records[0].read_text())
        config.write_text(json.dumps(dict(enabled=True,url=f'http://127.0.0.1:{port}/api/local-provider/'+grant['id'],token=grant['token'])))
        grant['enabled']=False
        path.write_text(json.dumps({'grants':[grant]}))
        refused=mcp([call])[0]
        self.assertTrue(refused['isError'])
        self.assertIn('revoked',refused['content'][0]['text'])
        self.assertIn('No operation was sent',refused['content'][0]['text'])
        self.assertEqual(len(list((state/'provider-calls'/grant['id']).glob('*.json'))),2)
