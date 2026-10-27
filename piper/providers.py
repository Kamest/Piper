"""Optional laptop HTTP/DB provider for a remote project, carried by a reverse SSH tunnel.

Grants are scoped to one registered local project and HTTP/DB tools. They are never GUI
credentials. A durable request reservation prevents writes replaying after a disconnect.
"""
from __future__ import annotations
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import urlsplit

import common
import projects
import remotes

LOCK = threading.RLock()
FILE = os.path.join(common.PIPER_HOME, 'provider-grants.json')
TUNNELS: dict[str, subprocess.Popen] = {}
REPLIES: dict[tuple[str, str], dict] = {}
PORT = 17897
STOP = threading.Event()
WORKER: threading.Thread | None = None


def forward(session, body):
    """Forward a session-bound notebook call once; credentials never enter its sandbox."""
    import sessions
    tool, args = body.get('tool'), body.get('args')
    if tool not in set(sessions.HTTP_TOOLS) | set(__import__('dbtools').SESSION_TOOLS) or not isinstance(args, dict):
        raise ValueError('Invalid local provider request or disallowed tool')
    config = common.read_json(os.path.join(session.dir, 'local-provider.json'), {})
    if not config.get('enabled'):
        raise ValueError('No local tool provider was enabled for this session')
    url = urlsplit(config.get('url', ''))
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.path.startswith('/api/local-provider/') or url.query or url.fragment or url.username:
        raise ValueError('Local provider must use the private SSH loopback tunnel')
    headers = {'Authorization': 'Bearer ' + config['token'],
               'Content-Type': 'application/json', 'Host': config.get('host', url.netloc)}
    # A suspended notebook can leave the reverse listener alive on the server.
    # Prove end-to-end availability before sending an operation, without spending
    # the approval deadline on a silent tunnel. A failed probe never submits it.
    probe = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    try:
        probe.request('GET', url.path, headers=headers)
        result = probe.getresponse()
        if result.status != 200 or json.loads(result.read(1024)) != {'ok': True}:
            raise ValueError('provider unavailable')
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise ValueError('Local computer is unavailable or its provider was revoked. No operation was sent. Continue with independent server work or wait for the user.') from e
    finally:
        probe.close()
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=910)
    try:
        payload = json.dumps(dict(id=secrets.token_hex(16), session=session.id, tool=tool, args=args))
        conn.request('POST', url.path, payload, headers)
        result = conn.getresponse()
        raw = result.read((8 << 20) + 1)
        if result.status != 200:
            raise ValueError('Local provider refused the request or is unavailable. No request was retried.')
        if len(raw) > 8 << 20:
            raise ValueError('Local provider response is too large. No request was retried.')
        response = json.loads(raw)
    except (OSError, http.client.HTTPException) as e:
        raise ValueError('Local computer disconnected or its Piper is unavailable. A sent operation may have completed; inspect history before repeating a write. Continue with independent server work or wait for the user.') from e
    finally:
        conn.close()
    if not isinstance(response, dict) or not response.get('ok'):
        raise ValueError(str(response.get('error') or 'Local provider failed') if isinstance(response, dict) else 'Invalid local provider response')
    return response.get('result')


def grants():
    return common.read_json(FILE, {}).get('grants', [])


def active_grant(grantid, token):
    """Authenticate a scoped provider capability for a probe or a tool operation."""
    with LOCK:
        grant = next((g for g in grants() if g['id'] == grantid and g['enabled'] and hmac.compare_digest(g['token'], token)), None)
    if not grant:
        raise PermissionError('Local tool provider is revoked or unavailable')
    return grant


def configure(body, manager=None):
    eid, remote_project, local_project = (body.get(k, '') for k in ('environment','remote_project','local_project'))
    entry = remotes.get(eid)
    if entry['mode'] != 'service':
        raise ValueError('Local Piper tools need the server service; SSH-only retains native agent tools.')
    enabled = body.get('enabled') is True
    project = projects.find(local_project, os.environ.get('PROJECT_PATH','')) if enabled else None
    if not isinstance(local_project,str) or not isinstance(remote_project,str) or not projects.KEY_RX.fullmatch(remote_project) or enabled and (not project or not project['exists']):
        raise ValueError('Choose a registered local project and a valid remote project key')
    with LOCK:
        rows = grants()
        old = next((g for g in rows if g['environment']==eid and g['remote_project']==remote_project),None)
        grant = dict(id=secrets.token_hex(16), token=secrets.token_urlsafe(32),
                     environment=eid,remote_project=remote_project,local_project=local_project,enabled=enabled)
        # Rotate/revoke locally first. A failed remote update must never leave an old grant live.
        common.write_json(FILE, {'grants':[g for g in rows if g is not old]+[grant]})
        os.chmod(FILE,0o600)
        if old:
            cancel_grant(manager, old)
    if not enabled:
        ensure_tunnel(eid)  # release the last local tunnel even when remote delivery fails
    remotes.request(entry, dict(op='provider',project=remote_project,enabled=enabled,
                               url=f'http://127.0.0.1:{PORT}/api/local-provider/{grant["id"]}',
                               host=f'127.0.0.1:{int(os.environ.get("PIPER_PORT","17895"))}', token=grant['token']))
    ensure_tunnel(eid)
    return {'enabled':enabled, 'notice':'New server sessions get piper_local tools. The laptop and its Piper must stay online; credentials remain local.' if enabled else 'Local provider revoked. Existing server sessions keep their server-owned tools.'}


def ensure_tunnel(eid):
    with LOCK:
        active = any(g['environment']==eid and g['enabled'] for g in grants())
        p = TUNNELS.get(eid)
        if p is not None and p.poll() is not None:
            p.wait(); TUNNELS.pop(eid,None); p=None
        if not active:
            if p:
                p.terminate(); p.wait(timeout=5); TUNNELS.pop(eid,None)
            return
        if p:
            return
        entry=remotes.get(eid)
        remotes.request(entry, {'op':'probe'})  # verify the pinned target before opening a new reverse tunnel
        # Fail on a collision instead of attaching to another machine's provider.
        argv=remotes.ssh(entry, [])
        argv=argv[:argv.index('--')]+['-N','-oExitOnForwardFailure=yes','-R',f'127.0.0.1:{PORT}:127.0.0.1:{int(os.environ.get("PIPER_PORT","17895"))}','--',entry['host']]
        try:
            TUNNELS[eid]=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        except OSError:
            return  # Optional provider availability must never block server-owned work.


def start():
    global WORKER
    STOP.clear()
    def loop():
        while not STOP.is_set():
            for eid in {g['environment'] for g in grants() if g['enabled']}:
                try: ensure_tunnel(eid)
                except (ValueError, OSError): pass
            STOP.wait(5)
    WORKER=threading.Thread(target=loop,name='piper-local-providers',daemon=True)
    WORKER.start()


def close():
    STOP.set()
    if WORKER is not None:
        WORKER.join(5)
    with LOCK:
        for p in TUNNELS.values():
            if p.poll() is None:
                p.terminate()
                try: p.wait(timeout=3)
                except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=3)
        TUNNELS.clear()


def cancel_grant(manager, grant):
    if manager is None:
        return
    prefix = 'provider-' + grant['environment'] + '-' + grant['id'] + '-'
    with manager.lock:
        for session in manager.sessions.values():
            if session.id.startswith(prefix):
                session._provider_active = False
        for item in manager.pending.values():
            if str(item.get('session','')).startswith(prefix):
                item['_cancelled'] = 'local provider revoked'
                item['_ok'] = False
                item['_ev'].set()


def revoke_environment(eid, manager=None):
    with LOCK:
        for grant in grants():
            if grant['environment']==eid:
                cancel_grant(manager, grant)
        common.write_json(FILE, {'grants':[dict(g,enabled=False) if g['environment']==eid else g for g in grants()]})
    ensure_tunnel(eid)


def remote_session(eid, identity):
    """Match the full environment + grant, including IDs such as dev and dev-west."""
    for grant in grants():
        if grant['environment'] == eid and grant['enabled']:
            prefix = 'provider-' + eid + '-' + grant['id'] + '-'
            if isinstance(identity, str) and identity.startswith(prefix):
                return identity[len(prefix):]
    return None


def pending(manager,eid):
    rows = [dict(row,id='local-'+row['id'],session=sid,
                 title='Local tools · '+row['title'],local_provider=True)
            for row in manager.pending_list(include_providers=True) if (sid := remote_session(eid, row.get('session'))) is not None]
    for row in rows:
        # The remote decision endpoint grants this request only. Never promise
        # a persistent/session grant belonging to a different execution machine.
        row.pop('always', None)
        row.pop('session_always', None)
        row.pop('similar', None)   # nor an hour grant for similar requests
    return rows


def decorate_state(manager, eid, state):
    """Notebook decisions stay actionable even with the server's HTTP/DB modules off."""
    import feed
    rows = pending(manager, eid)
    state['pending'] = state.get('pending', []) + rows
    gone = [dict(row, id='local-'+row['id'], session=sid,
                 title='Local tools · '+row['title'])
            for row in manager.pending_gone_list(include_providers=True)
            if (sid := remote_session(eid, row.get('session'))) is not None]
    state['pending_gone'] = state.get('pending_gone', []) + gone
    queue = state.setdefault('queue', {})
    confirming = {p['session'] for p in rows}
    queue['actions'] = [a for a in queue.get('actions', []) if not
                        (a.get('level') == 'waiting' and a.get('subject', {}).get('key') in confirming)] + \
        feed.derive_actions([], rows, [], {}, {}, time.time(), 0)


def record_call(path, value):
    """Persist the reservation before execution, including newly created directory entries."""
    common.write_json(path, value)
    with open(path, 'rb') as stream:
        os.fsync(stream.fileno())
    for directory in dict.fromkeys((os.path.dirname(path), os.path.dirname(os.path.dirname(path)), common.PIPER_HOME)):
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def invoke(manager,grantid,token,body):
    grant = active_grant(grantid, token)
    import sessions
    tool,args,rid,sid=(body.get(k) for k in ('tool','args','id','session'))
    allowed=set(sessions.HTTP_TOOLS) | set(__import__('dbtools').SESSION_TOOLS)
    if tool not in allowed or not isinstance(args,dict) or not isinstance(rid,str) or not re.fullmatch(r'[0-9a-f]{32}',rid) or not isinstance(sid,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',sid):
        raise ValueError('Invalid provider request or disallowed tool')
    if args.get('project') not in (None,'',grant['local_project']):
        raise ValueError('This provider is restricted to its mapped local project')
    project=projects.find(grant['local_project'],os.environ.get('PROJECT_PATH',''))
    if not project or not project['exists']:
        raise ValueError('Mapped local project is unavailable')
    identity='provider-'+grant['environment']+'-'+grantid+'-'+sid
    digest=hashlib.sha256(json.dumps({'project':grant['local_project'],'request':body},sort_keys=True).encode()).hexdigest()
    directory=os.path.join(common.PIPER_HOME,'provider-calls',grantid)
    path=os.path.join(directory,rid+'.json')
    with LOCK:
        if not any(g['id']==grantid and g['enabled'] and hmac.compare_digest(g['token'],token) for g in grants()):
            raise PermissionError('Local tool provider was revoked')
        old=common.read_json(path,None)
        if os.path.lexists(path):
            if not isinstance(old, dict) or not old.get('hash'):
                return {'ok':False,'error':'Operation journal is unreadable. It was not replayed; inspect HTTP/DB history.'}
            if old.get('hash')!=digest:
                raise ValueError('Request id was reused with different arguments')
            return REPLIES.get((grantid,rid)) or {'ok':False,'error':'Operation outcome is unknown or still running. It was not replayed; inspect HTTP/DB history before continuing.'}
        os.makedirs(directory,mode=0o700,exist_ok=True)
        record_call(path,{'hash':digest,'at':time.time(),'state':'started'})
        with manager.lock:
            s=manager.sessions.get(identity)
            if s is None:
                s=sessions.Session(id=identity,project=project['key'],path=project['path'],title=project['title'],runtime='direct',state='exited',claude_mode='host',hidden=True)
                manager.repo_top(s.path,record='start')
                manager.sessions[identity]=s
    try:
        result=manager.tool(s,tool,args)
        response={'ok':True,'result':result}
    except Exception as e:
        response={'ok':False,'error':str(e)}
    with LOCK:
        # Results can contain credentials/PII. Keep only bounded, process-local replies;
        # the durable journal records no tool arguments or result contents.
        if len(REPLIES) >= 128:
            REPLIES.pop(next(iter(REPLIES)))
        REPLIES[(grantid,rid)] = response
        record_call(path,{'hash':digest,'at':time.time(),'state':'completed'})
    return response
