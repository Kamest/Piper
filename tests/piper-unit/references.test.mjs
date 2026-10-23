import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { references, piperUrl, piperTarget, relativeFile, fileMatches, gitlabUrl, fileUrlReference } from '../../piper/web/dist/references.js';

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
