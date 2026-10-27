import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { baseProjectRef, linkedSessionRef, hosts, projectHosts, projectName, rawProjectKey, reference, routeRequest, scoped, scopeResponse, scopeState, terminalQuery, assertHostWrite } from '../../piper/web/dist/environment.js';
import { signinBrowserError } from '../../piper/web/dist/environment.js';

const ref = (kind, raw, host='dev') => scoped(host, kind, raw);
const state = { sessions:[{id:'same',project:'group/repo',parent:'parent',kind:'project'}], projects:[{key:'group/repo',title:'Repo'}], pending:[{id:'decision',session:'same',kind:'db_execute',payload:{project:'group/repo',sql:'literal piper-ref:other:project:repo'}}], pending_gone:[{id:'gone',session:'same'}], project_session_defaults:{'group/repo':{agent:'claude',opts:[]}}, features:{preset:'server',modules:{db:false}}, gui_flags:{theme:'server'}, queue:{actions:[]} };

test('loopback OAuth Code explains unsupported cross-device callbacks while Device and notebook providers work', () => {
  const local = { id:'signin', payload:{grant:'Authorization Code'} };
  const remote = { ...local, id:ref('pending','signin') };
  assert.equal(signinBrowserError(local, false), '');
  assert.match(signinBrowserError(local, true), /Device Authorization/);
  assert.match(signinBrowserError(remote, false), /computer running HTTP/);
  assert.equal(signinBrowserError({...remote, local_provider:true}, false), '');
  assert.match(signinBrowserError({...remote, local_provider:true}, true), /Device Authorization/);
  assert.equal(signinBrowserError({...remote, payload:{grant:'Device Authorization'}}, true), '');
});

test('references preserve host, kind and unicode and reject reserved raw identities',()=>{
  assert.deepEqual(reference(ref('project','group/repo@wt é')), {host:'dev',kind:'project',raw:'group/repo@wt é'});
  assert.equal(rawProjectKey(ref('project','group/repo@wt')), 'group/repo@wt');
  assert.equal(baseProjectRef(ref('project','group/repo@wt')), ref('project','group/repo'));
  assert.equal(projectName(ref('project','group/repo')), 'repo');
  assert.throws(()=>reference(ref('session','same'),'project'),/Invalid/);
  assert.throws(()=>scoped('', 'project','piper-ref:dev:project:repo'), /Reserved/);
});

for (const [name,path,body,expected] of [
  ['session stop','/api/sessions/'+encodeURIComponent(ref('session','same'))+'/stop',{},'/api/sessions/same/stop'],
  ['pending decision','/api/pending/'+encodeURIComponent(ref('pending','decision')),{approve:true},'/api/pending/decision'],
  ['HTTP browser sign-in','/api/http/auth/'+encodeURIComponent(ref('pending','decision'))+'/open',{},'/api/http/auth/decision/open'],
  ['HTTP sign-in window close','/api/http/auth/'+encodeURIComponent(ref('pending','decision'))+'/closed',{},'/api/http/auth/decision/closed'],
  ['provider HTTP sign-in','/api/http/auth/'+encodeURIComponent(ref('pending','local-decision'))+'/open',{},'/api/http/auth/local-decision/open'],
  ['file read','/api/fs/read?project='+encodeURIComponent(ref('project','group/repo'))+'&path=src%2Fa.ts',undefined,'/api/fs/read?project=group%2Frepo&path=src%2Fa.ts'],
  ['start','/api/sessions',{project:ref('project','group/repo'),task:'piper-ref:other:project:literal'},'/api/sessions'],
  ['HTTP followup','/api/http/run?id='+encodeURIComponent(ref('http','same')),undefined,'/api/http/run?id=same'],
  ['DB fetch','/api/db/fetch',{result_id:ref('result','same'),offset:10},'/api/db/fetch'],
  ['DB transaction','/api/db/tx',{project:ref('project','group/repo'),tx:'same',connection:'prod',op:'commit'},'/api/db/tx'],
  ['stack followup','/api/stack/job?project='+encodeURIComponent(ref('project','group/repo'))+'&id=same',undefined,'/api/stack/job?project=group%2Frepo&id=same'],
  ['conversation resume','/api/conversations/'+encodeURIComponent(ref('session','same'))+'/resume',{},'/api/conversations/same/resume'],
  ['feed dismissal','/api/feed/dismiss',{key:ref('action','same')},'/api/feed/dismiss'],
]) test('manifest routes '+name+' without a selected host',()=>{
  globalThis.location={search:'?environment=wrong'};
  const routed=routeRequest(path,body);assert.equal(routed.host,'dev');assert.equal(routed.path,expected);
  if(body?.task)assert.equal(routed.body.task,body.task);
});

test('local plus remote and two different remote references fail closed',()=>{
  assert.throws(()=>routeRequest('/api/http/auth/'+encodeURIComponent(ref('session','same'))+'/open',{}),/Invalid/);
  assert.throws(()=>routeRequest('/api/http/auth/'+encodeURIComponent(ref('pending','same'))+'/open',{},'other'),/different hosts/);
  for(const session of ['same',ref('session','same','other')]) assert.throws(()=>routeRequest('/api/db/query',{project:ref('project','repo'),session,sql:'select 1'}),/different hosts/);
  assert.throws(()=>routeRequest('/api/db/query',{project:ref('project','repo')},''),/different hosts/);
  assert.throws(()=>routeRequest('/api/unknown',{project:ref('project','repo')}),/Unsupported/);
  assert.throws(()=>routeRequest('/api/unknown?project='+encodeURIComponent(ref('project','repo'))),/Unsupported/);
});

test('hostless control plane stays local regardless of URL or active UI state',()=>{
  globalThis.location={search:'?environment=dev'};
  for(const path of ['/api/state','/api/settings','/api/db/all','/api/environments'])assert.equal(routeRequest(path).host,'');
  assert.equal(routeRequest('/api/state',undefined,'dev').host,'dev');
});

test('terminal owner is immutable and independent of URL',()=>{
  globalThis.location={search:'?environment=wrong'};
  assert.equal(terminalQuery('same'),'');assert.equal(terminalQuery(ref('session','same')),'?environment=dev');
});

test('projection combines owned collections but keeps notebook settings and independent snapshots',()=>{
  hosts.clear();const a=scopeState('dev',state),b=scopeState('other',state);
  hosts.set('dev',{host:{id:'dev',label:'Dev',mode:'service'},snapshot:a,online:true});
  hosts.set('other',{host:{id:'other',label:'Other',mode:'service'},snapshot:b,online:false});
  const local=scopeState('',{...state,gui_flags:{theme:'local'},features:{preset:'local',modules:{db:true}}});
  const projection=projectHosts(local);
  assert.equal(new Set(projection.sessions.map(s=>s.id)).size,3);
  assert.equal(new Set(projection.projects.map(s=>s.key)).size,3);
  assert.equal(new Set(projection.pending.map(s=>s.id)).size,3);
  assert.equal(projection.gui_flags.theme,'local');assert.equal(projection.features.preset,'local');
  assert.equal(state.sessions[0].id,'same');assert.equal(a.sessions[0].id,ref('session','same'));
  assert.deepEqual(projection.project_session_defaults[ref('project','group/repo')],{agent:'claude',opts:[]});
  assert.equal(a.pending[0].payload.sql,state.pending[0].payload.sql);
});

test('notebook approvals keep local payload but remote decision and requesting session',()=>{
  const value={...state,pending:[{id:'local-request',session:'same',kind:'db_execute',payload:{project:'local/repo'}}]};
  const row=scopeState('dev',value).pending[0];
  assert.equal(row.id,ref('pending','local-request'));assert.equal(row.session,ref('session','same'));assert.equal(row.payload.project,'local/repo');
  assert.equal(routeRequest('/api/pending/'+encodeURIComponent(row.id),{approve:false}).host,'dev');
});

for(const [name,path,value,check] of [
  ['worktree','/api/git/worktree',{key:'group/repo@task',path:'/real/repo',branch:'task'},r=>assert.equal(r.key,ref('project','group/repo@task'))],
  ['new session','/api/sessions',{...state.sessions[0],project_defaults:{agent:'claude',opts:[]}},r=>{assert.equal(r.id,ref('session','same'));assert.equal(r.project,ref('project','group/repo'));}],
  ['HTTP run','/api/http/run',{id:'same',project:'repo',requests:[{body:'piper-ref:other:session:literal'}]},r=>{assert.equal(r.id,ref('http','same'));assert.equal(r.requests[0].body,'piper-ref:other:session:literal');}],
  ['DB result','/api/db/query',{result_id:'same',rows:[['piper-ref:other:project:literal']],columns:[{name:'session'}]},r=>{assert.equal(r.result_id,ref('result','same'));assert.deepEqual(r.rows,[['piper-ref:other:project:literal']]);}],
  ['history','/api/conversations',{items:[{session:'same',conversation:'native',project:'repo',import_from:{conversation:'old'},fork_from:{session:'parent',conversation:'base'},first:'unchanged'}],projects:[{key:'repo'}],targets:[{key:'repo'}]},r=>{assert.equal(r.items[0].conversation,ref('conversation','native'));assert.equal(r.items[0].fork_from.session,ref('session','parent'));assert.equal(r.targets[0].key,ref('project','repo'));assert.equal(r.items[0].first,'unchanged');}],
])test('response contract scopes '+name,()=>check(scopeResponse('dev',path,value)));

test('offline and SSH-only restrictions are per target, never global',()=>{
  hosts.clear();hosts.set('ssh',{host:{id:'ssh',mode:'ssh'},online:true});hosts.set('offline',{host:{id:'offline',mode:'service'},online:false});
  assert.doesNotThrow(()=>assertHostWrite('','POST','/api/settings'));
  assert.doesNotThrow(()=>assertHostWrite('ssh','POST','/api/sessions/same/stop'));
  assert.throws(()=>assertHostWrite('ssh','GET','/api/fs/read'),/SSH only/);
  assert.throws(()=>assertHostWrite('offline','POST','/api/sessions'),/unavailable/);
});

test('static conversation search is not mistaken for a conversation id',()=>{
  assert.equal(routeRequest('/api/conversations/search?q=hello',undefined,'dev').host,'dev');
  assert.equal(routeRequest('/api/db/running?project='+encodeURIComponent(ref('project','repo'))+'&owner=gui').host,'dev');
  assert.equal(routeRequest('/api/db/cancel?project='+encodeURIComponent(ref('project','repo'))+'&owner=gui').host,'dev');
});

test('nested DB groups and connections belong to the captured server, not their display name',()=>{
  const input={projects:[{project:'repo',connections:[{name:'dev',project:'repo'}]}],orphans:[],status:{ready:true}};
  const remote=scopeResponse('dev','/api/db/all',input);
  assert.equal(remote.projects[0].project,ref('project','repo'));
  assert.equal(remote.projects[0].connections[0].project,ref('project','repo'));
  assert.equal(input.projects[0].connections[0].project,'repo');
});


test('remote subject muting and focus never fall through to local control plane',()=>{
  assert.equal(routeRequest('/api/feed/mute',{key:ref('session','same'),minutes:60}).host,'dev');
  assert.equal(routeRequest('/api/feed/mute',{key:ref('action','ci/repo'),minutes:60}).host,'dev');
  assert.equal(routeRequest('/api/timesheet/focus',{session:ref('session','same')}).host,'dev');
  assert.throws(()=>routeRequest('/api/feed/mute',{key:ref('http','same')}),/Invalid mute/);
});


test('session links accept raw emitted IDs and explicit qualified IDs without losing their owner',()=>{
  assert.equal(linkedSessionRef('same','dev'),ref('session','same'));
  assert.equal(linkedSessionRef('same','',ref('session','emitter')),ref('session','same'));
  assert.equal(linkedSessionRef(ref('session','same')),ref('session','same'));
  assert.equal(linkedSessionRef(ref('session','same'),'dev'),ref('session','same'));
  assert.throws(()=>linkedSessionRef(ref('session','same'),'other'),/Conflicting/);
  assert.throws(()=>linkedSessionRef(ref('project','same')),/Invalid/);
});

test('session host terminal routes and scopes its owner, rejecting mixed hosts', () => {
  const r = routeRequest('/api/host-terminals', { project: ref('project', 'group/repo@topic'), session: ref('session', 'same') });
  assert.equal(r.host, 'dev'); assert.deepEqual(r.body, { project: 'group/repo@topic', session: 'same' });
  assert.throws(() => routeRequest('/api/host-terminals', { project: 'local/repo', session: ref('session', 'same') }), /different hosts/);
  const s = scopeState('dev', { ...state, host_terms: { items: [{ id: 'host-one', project: 'group/repo', session: 'same' }] } });
  assert.equal(s.host_terms.items[0].session, ref('session', 'same'));
  assert.equal(scopeResponse('dev', '/api/host-terminals', { id: 'host-one', project: 'group/repo', session: 'same' }).session, ref('session', 'same'));
});
