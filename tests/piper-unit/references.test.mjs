import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { references, terminalReferences, referenceProjects, piperUrl, piperTarget, relativeFile, fileMatches, gitlabUrl, fileUrlReference } from '../../piper/web/dist/references.js';

import { projectFileTargets } from '../../piper/web/dist/references.js';

test('terminal hard wraps reconstruct only continuous file and URL tokens, with displayed offsets', () => {
  const text = 'Open helper-\n  kit/.gitlab-ci.yml and development/feedback-\n  companion-ci.patch.\nSee https://example.com/long-\n  reference?q=one&\n  page=2.';
  const hits = terminalReferences(text);
  assert.deepEqual(hits.map(x => x.ref), [
    {kind:'file',path:'helper-kit/.gitlab-ci.yml',line:0},
    {kind:'file',path:'development/feedback-companion-ci.patch',line:0},
    {kind:'url',url:'https://example.com/long-reference?q=one&page=2'},
  ]);
  assert.equal(text.slice(hits[0].start,hits[0].end),'helper-\n  kit/.gitlab-ci.yml');
  assert.equal(hits[0].text,'helper-kit/.gitlab-ci.yml');
  assert.deepEqual(terminalReferences('src/first.ts\n  src/second.ts').map(x=>x.ref.path),['src/first.ts','src/second.ts']);
  assert.deepEqual(terminalReferences('A well-\n  known sentence.\n\nNext paragraph.').map(x=>x.ref),[]);
  assert.deepEqual(terminalReferences('src/\n\n  next.ts').map(x=>x.ref.path),['src/','next.ts']);
  assert.deepEqual(terminalReferences('https://example.com/first\n  https://other.example/second').map(x=>x.ref.url),['https://example.com/first','https://other.example/second']);
});

test('a terminal-width hard break can split inside a token, but ordinary newlines cannot', () => {
  const text = 'src/VeryLongCompo\n  nent.java:12';
  assert.deepEqual(terminalReferences(text, new Set([text.indexOf('\n')])).map(x=>x.ref),[{kind:'file',path:'src/VeryLongComponent.java',line:12}]);
  assert.notDeepEqual(terminalReferences(text).map(x=>x.ref),[{kind:'file',path:'src/VeryLongComponent.java',line:12}]);
  const prose = 'Please open the\n  src/second.ts';
  assert.deepEqual(terminalReferences(prose, new Set([prose.indexOf('\n')])).map(x=>x.ref.path), ['src/second.ts']);
  const separate = 'src/first.ts\n  src/second.ts';
  assert.deepEqual(terminalReferences(separate, new Set([separate.indexOf('\n')])).map(x=>x.ref.path), ['src/first.ts','src/second.ts']);
});

test('registered project names resolve relative sibling references without escaping or choosing ambiguous roots', () => {
  const projects = [{key:'acme/helper-kit',path:'/projects/helper-kit'},{key:'other/helper-kit',path:'/other/helper-kit'}];
  assert.deepEqual(projectFileTargets('helper-kit/.gitlab-ci.yml',projects),[
    {project:'acme/helper-kit',path:'/projects/helper-kit/.gitlab-ci.yml'},
    {project:'other/helper-kit',path:'/other/helper-kit/.gitlab-ci.yml'},
  ]);
  assert.deepEqual(projectFileTargets('acme/helper-kit/src/a.ts',projects),[{project:'acme/helper-kit',path:'/projects/helper-kit/src/a.ts'}]);
  for(const text of ['helper-kit/../private.txt','../helper-kit/a.ts','/helper-kit/a.ts','helper-kit-other/a.ts']) assert.deepEqual(projectFileTargets(text,projects),[]);
});

test('terminal references include full web and local file URLs without parsing embedded issue numbers', () => {
  const text = 'See https://example.com/acme/base/-/issues/12 and file:///fixtures/my%20report.md#L8.';
  assert.deepEqual(terminalReferences(text).map(x => x.ref), [
    {kind:'url',url:'https://example.com/acme/base/-/issues/12'},
    {kind:'file',path:'/fixtures/my report.md',line:8},
  ]);
  assert.deepEqual(terminalReferences('file://other-host/secret.txt'), []);
});

test('issue project resolution respects explicit references, the linked issue and other granted repositories', () => {
  const session = {gitlab:['acme/app'],gitlab_extra:['acme/base'],issue:{gl:'acme/base',iid:12,type:'issue'}};
  const ref = {kind:'gitlab',resource:'issues',id:'12'};
  assert.deepEqual(referenceProjects({...ref,project:'other/repo'}, session), ['other/repo']);
  assert.deepEqual(referenceProjects(ref, session), ['acme/base']);
  assert.deepEqual(referenceProjects({...ref,id:'13'}, session), ['acme/app','acme/base']);
  assert.deepEqual(referenceProjects({...ref,resource:'merge_requests'}, session), ['acme/app','acme/base']);
  assert.deepEqual(referenceProjects(ref,{gitlab:['acme/app'],gitlab_extra:['acme/app']}), ['acme/app']);
});

const refs = text => references(text).map(x => x.ref);
test('paths without line numbers, stacktraces, quotes and Unicode are references', () => {
  assert.deepEqual(refs('src/A.java A.java:42:8 "docs/my report.md":7 src/français.ts Dockerfile .env src/'), [
    {kind:'file',path:'src/A.java',line:0}, {kind:'file',path:'A.java',line:42},
    {kind:'file',path:'docs/my report.md',line:7}, {kind:'file',path:'src/français.ts',line:0},
    {kind:'file',path:'Dockerfile',line:0}, {kind:'file',path:'.env',line:0}, {kind:'file',path:'src/',line:0},
  ]);
  assert.deepEqual(refs('at app.Foo.run(Foo.java:25) main.ts(12,3) README.md#L8'), [
    {kind:'file',path:'Foo.java',line:25}, {kind:'file',path:'main.ts',line:12}, {kind:'file',path:'README.md',line:8},
  ]);
  const text = 'See `src/a.ts:3` and docs/a.http';
  for (const span of references(text)) assert.equal(text.slice(span.start,span.end),span.text);
});
test('web URLs never yield misleading path, commit or issue references', () => {
  assert.deepEqual(refs('https://gitlab.test/g/r/-/issues/12 https://example.test/src/file.ts#L2'), []);
  assert.deepEqual(refs('file:///work/project/a.ts'), []);
});
test('GitLab short references retain resource and explicit project; pipelines are not issues', () => {
  assert.deepEqual(refs('#12 !34 pipeline #56 group/repo!7 issue 8 MR !9'), [
    {kind:'gitlab',resource:'issues',id:'12'}, {kind:'gitlab',resource:'merge_requests',id:'34'},
    {kind:'gitlab',resource:'pipelines',id:'56'}, {kind:'gitlab',resource:'merge_requests',id:'7',project:'group/repo'},
    {kind:'gitlab',resource:'issues',id:'8'}, {kind:'gitlab',resource:'merge_requests',id:'9'},
  ]);
  assert.equal(gitlabUrl('gitlab.test','g/sub/repo','pipelines','123'),'https://gitlab.test/g/sub/repo/-/pipelines/123');
  assert.throws(()=>gitlabUrl('javascript:alert(1)','g/r','issues','1'));
});
test('DB refs are qualified names or table/SQL cues; commit and session refs stay distinct', () => {
  assert.deepEqual(refs('SELECT * FROM public.users JOIN orders ON 1=1; tabulka inventory'), [
    {kind:'db',name:'public.users'}, {kind:'db',name:'orders'}, {kind:'db',name:'inventory'},
  ]);
  assert.deepEqual(refs('session ab123456 commit a12bc34'), [{kind:'session',id:'ab123456'},{kind:'commit',hash:'a12bc34'}]);
  assert.deepEqual(refs('ordinary text and 12345678'), []);
});
test('Piper URLs match exact trusted origins, including aliases, never unrelated localhost apps', () => {
  const here = 'https://piper.example/';
  const origins = ['http://localhost:17895', 'http://127.0.0.1:17895'];
  for (const url of ['/?session=ab12',here+'?view=files','http://localhost:17895/?view=db']) assert.ok(piperUrl(url,here,origins));
  for (const url of ['https://piper.example.evil/', 'https://piper.example@evil.test/', 'http://localhost:8080/', 'javascript:alert(1)', 'https://evil.test/?next='+here]) assert.equal(piperUrl(url,here,origins),null);
});
test('internal routes resolve query, hash and session paths without adopting a token', () => {
  assert.deepEqual(piperTarget(new URL('http://localhost:17895/?session=s1&view=files&path=src%2Fa.ts&line=23#token=ignored')), {
    session:'s1',view:'files',path:'src/a.ts',line:23,connection:'',table:'',commit:'',
  });
  assert.equal(piperTarget(new URL('https://piper.test/#/sessions/s2/http?path=a.http')).session,'s2');
  assert.equal(piperTarget(new URL('https://piper.test/sessions/s3/db?table=public.users')).table,'public.users');
});
test('mobile homepage is a web link while explicit Piper destinations and desktop aliases stay internal', () => {
  const here = 'https://piper.kamest.dev/?session=current', aliases = ['http://localhost:17895'];
  for (const url of ['https://piper.kamest.dev', 'https://piper.kamest.dev/', 'https://piper.kamest.dev/#'])
    assert.equal(piperUrl(url, here, aliases, true), null);
  for (const suffix of ['?session=fixture', '?view=convo', '#session=fixture', '#token=fixture', 'sessions/fixture/convo'])
    assert.ok(piperUrl('https://piper.kamest.dev/' + suffix, here, aliases, true));
  assert.ok(piperUrl('http://localhost:17895/', here, aliases, true));
  assert.ok(piperUrl('https://piper.kamest.dev/', here, aliases));
});
test('file resolution stays inside the emitting checkout and preserves ambiguous stacktrace matches', () => {
  assert.equal(relativeFile('/work/project/src/../a.ts','/work/project'),'a.ts');
  for (const path of ['../other/a.ts','/work/project-other/a.ts','/work/project/../../a.ts']) assert.equal(relativeFile(path,'/work/project'),null);
  assert.deepEqual(fileMatches(['src/A.java','test/A.java'],'A.java'),['src/A.java','test/A.java']);
  assert.deepEqual(fileMatches(['A.java','src/A.java'],'A.java'),['A.java']);
});


test('hash-like file names, emoji paths and quoted table names do not overlap', () => {
  assert.deepEqual(refs('src/deadbeef12.ts deadbeef12.ts docs/emoji-🎉.md'), [
    {kind:'file',path:'src/deadbeef12.ts',line:0}, {kind:'file',path:'deadbeef12.ts',line:0}, {kind:'file',path:'docs/emoji-🎉.md',line:0},
  ]);
  assert.deepEqual(refs('`users` commit deadbeef'), [{kind:'db',name:'users'},{kind:'commit',hash:'deadbeef'}]);
});


test('compiler and Python stacktrace references preserve diagnostic source positions', () => {
  assert.deepEqual(refs('[ERROR] src/App.java:[12,3] cannot find symbol'), [{kind:'file',path:'src/App.java',line:12,diagnostic:true}]);
  assert.deepEqual(refs('main.ts(8,2): error TS1001'), [{kind:'file',path:'main.ts',line:8,diagnostic:true}]);
  assert.deepEqual(refs('File "/work/app.py", line 32, in run'), [{kind:'file',path:'/work/app.py',line:32}]);
  assert.deepEqual(fileUrlReference('file:///work/app.js:12:3'),{kind:'file',path:'/work/app.js',line:12});
  assert.deepEqual(fileUrlReference('file:///work/my%20file.md#L8'),{kind:'file',path:'/work/my file.md',line:8});
  assert.throws(()=>fileUrlReference('file://other-host/work/a.js'));
});
