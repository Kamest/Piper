// node --test: pure helpers of the GUI workbench, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { notesMessage, numberDiff, projectRel, reviewGroup, reviewOrder, reviewNavigationOrder, reviewKey, sourceClick, reviewFileLabels, reviewListWidth } from '../../piper/web/dist/gitpanel.js';
import { reviewWhitespaceLines } from '../../piper/web/dist/cmeditor.js';
import { reference } from '../../piper/web/dist/files.js';
import { fmtTokens } from '../../piper/web/dist/convo.js';
import { askedText, auditDetail, auditEmptyLine } from '../../piper/web/dist/board.js';
import { pushHistory, enqueue, nextReady, takeQueued } from '../../piper/web/dist/composer.js';
import { blockGroupKey, startupStatus, startupElapsed, deniedDomain, issueTask, protectedText, showBidi } from '../../piper/web/dist/text.js';

test('diff lines carry old/new numbers; a removed "--" line is not a header', () => {
  const d = numberDiff([
    'diff --git a/x.sql b/x.sql', '--- a/x.sql', '+++ b/x.sql', '@@ -10,3 +10,3 @@ ctx',
    ' keep', '--- old comment', '+-- new comment', ' tail',
  ].join('\n'));
  assert.deepEqual(d.map((l) => [l.cls, l.oldNo, l.newNo]), [
    ['meta', null, null], ['meta', null, null], ['meta', null, null], ['hunk', null, null],
    ['', 10, 10], ['del', 11, null], ['add', null, 11], ['', 12, 12],
  ]);
});

test('git paths become project-relative for a project inside a repository', () => {
  assert.equal(projectRel('app/src/A.java', '/r', '/r/app'), 'src/A.java');
  assert.equal(projectRel('other/B.java', '/r', '/r/app'), '/r/other/B.java');
  assert.equal(projectRel('src/A.java', '/r', '/r'), 'src/A.java');
});

test('review notes become one sorted message', () => {
  const m = notesMessage([
    { path: 'b.ts', line: 3, old: false, text: 'z' },
    { path: 'a.ts', line: 9, old: true, text: 'y' },
    { path: 'a.ts', line: 2, old: false, text: 'x' },
  ]);
  const lines = m.split('\n');
  assert.equal(lines.length, 4);
  assert.equal(lines[1], '- a.ts:2: x');
  assert.match(lines[2], /^- a\.ts:9 \(removed line/);
  assert.equal(lines[3], '- b.ts:3: z');
});

test('file references', () => {
  assert.equal(reference('src/A.java', null), 'src/A.java');
  assert.equal(reference('src/A.java', [7, 7]), 'src/A.java:7');
  assert.equal(reference('src/A.java', [20, 12]), 'src/A.java:12-20');
});

test('denied network log lines yield the domain', () => {
  assert.equal(deniedDomain('DENIED repo1.Maven.org:443 (CONNECT)'), 'repo1.maven.org');
  assert.equal(deniedDomain('2026-09-24 DENIED api.example.co.uk'), 'api.example.co.uk');
  assert.equal(deniedDomain('DENIED 10.0.0.1:80'), '');
});

test('issue task only references the issue - the session fetches it itself', () => {
  const t = issueTask({ iid: 4, title: 'Price list', state: 'opened', web_url: 'https://g/x/-/issues/4' }, 'grp/app');
  assert.match(t, /^GitLab issue #4: Price list\nhttps:\/\/g\/x\/-\/issues\/4/);
  assert.match(t, /gitlab_issue \(project "grp\/app", iid 4\)/);
  assert.match(t, /including all comments/);
  const n = issueTask({ iid: 4, title: 'Price list', state: 'opened', web_url: 'https://g/x/-/issues/4' }, 'grp/app', 77);
  assert.match(n, /^Handle comment note_77 \(and anything newer\) - that is the task now\. Read it with the tool gitlab_issue \(project "grp\/app", iid 4\)\./m,
    'W9: with comments the picker\'s task targets the latest one');
  assert.doesNotMatch(n, /including all comments/);
});

test('N4: the audit row shows a bidi control in a file name / git argument as a mark, never reversing the row', () => {
  const d = auditDetail({ event: 'session-write', ts: 't', path: 'notes-\u202etxt.md', args: ['log', '--grep=\u202eevil'], ok: true });
  assert.ok(!/[\u202a-\u202e\u2066-\u2069]/.test(d), d);
  assert.ok(d.includes('path: notes-[U+202E]txt.md'), d);
  assert.ok(d.includes('[U+202E]evil'), d);
});

test('N6: the audit row names the whole git command a session ran on the host, not "4 items"', () => {
  const d = auditDetail({ event: 'git-run', ts: 't', project: 'grp/demo', args: ['push', '--force', 'origin', 'main'], rc: 0 });
  assert.ok(d.includes('args: ["push","--force","origin","main"]'), d);
  // other long lists (keys, rules) stay a count
  assert.ok(auditDetail({ event: 'settings', ts: 't', keys: ['a', 'b', 'c', 'd'] }).includes('keys: 4 items'));
});

test('audit detail and small formatters', () => {
  const d = auditDetail({ event: 'spawn', ts: 't', session: 's', argv: ['hole', 'start', 'multi\nline prompt'], ok: true });
  assert.equal(d, 'argv: hole start · ok: true');
  assert.equal(fmtTokens(950), '950');
  assert.equal(fmtTokens(12500), '13 k');
  assert.equal(fmtTokens(2_300_000), '2.3 M');
  assert.deepEqual(pushHistory(['a', 'b'], 'b'), ['b', 'a']);
  assert.equal(pushHistory(Array.from({ length: 60 }, (_, i) => String(i)), 'x').length, 50);
});

import { highlightLines, langOf } from '../../piper/web/dist/highlight.js';
import { chainLabel, markAt } from '../../piper/web/dist/files.js';

test('N6: a file listed with a decomposed (NFD) name gets the git mark git reports under the NFC name', () => {
  const marks = new Map([['docs/caf\u00e9-nfd.md', 'U'], ['a.txt', 'M']]);
  assert.equal(markAt(marks, 'docs/cafe\u0301-nfd.md'), 'U');
  assert.equal(markAt(marks, 'a.txt'), 'M');
  assert.equal(markAt(marks, 'b.txt'), undefined);
  assert.equal(markAt(undefined, 'a.txt'), undefined);
});

const cls = (toks) => toks.filter((t) => t.c).map((t) => `${t.c}:${t.t.trim()}`);

test('Review basenames lead and duplicate names retain compact distinguishing directory segments', () => {
  const labels = (paths) => reviewFileLabels(paths.map(path => ({ path })));
  const deep = ['module/src/main/java/example/operations/Operations.java', 'module/src/test/java/example/operations/Operations.java'];
  const pair = labels(deep);
  assert.deepEqual(pair.get(deep[0]), { base: 'Operations.java', dir: 'main/…/operations', key: 'main', tail: '/…/operations' });
  assert.deepEqual(pair.get(deep[1]), { base: 'Operations.java', dir: 'test/…/operations', key: 'test', tail: '/…/operations' });
  assert.equal(labels(['a/x/F.java', 'b/x/F.java']).get('a/x/F.java').dir, 'a/x');
  assert.equal(labels(['a/ai/F.java', 'a/model/F.java']).get('a/ai/F.java').dir, '…/ai');
  const three = labels(['a/x/F.java', 'a/y/F.java', 'b/x/F.java']);
  assert.equal(three.get('a/x/F.java').dir, 'a/x');
  assert.equal(new Set([...three.values()].map(x => x.dir)).size, 3);
  assert.equal(labels(['pom.xml', 'module/pom.xml']).get('pom.xml').key, '(root)');
  assert.equal(labels(['pom.xml', 'module/pom.xml']).get('module/pom.xml').key, 'module');
  assert.deepEqual(labels(['a/b/c/Result.java']).get('a/b/c/Result.java'), { base: 'Result.java', dir: '…/c' });
  assert.deepEqual(reviewFileLabels([{ orig: 'old/Before.java', path: 'new/After.java' }]).get('new/After.java'), { base: 'Before.java → After.java', dir: 'new' });
});

test('Review width respects saved values, body bounds, corrupt preferences and the narrow fixed column', () => {
  assert.equal(reviewListWidth(null, 1400, 1440).width, 248);
  assert.equal(reviewListWidth(null, 1000, 1024).width, 190);
  assert.equal(reviewListWidth(300, 1400, 1440).width, 300);
  assert.equal(reviewListWidth(480, 900, 1024).width, 405);
  for (const invalid of [0, 159, 481, NaN, Infinity]) assert.equal(reviewListWidth(invalid, 1400, 1440).width, 248);
  assert.deepEqual(reviewListWidth(480, 780, 800), { width: 168, max: 351, compact: true });
  assert.equal(reviewListWidth(480, 1400, 1440).width, 480, 'a temporary narrow layout does not overwrite the saved preference');
});

test('Review whitespace indicators preserve internal text changes, line additions and exact original bytes', () => {
  assert.deepEqual(reviewWhitespaceLines(['  call();', 'return 1;\t', 'unchanged'], ['\tcall();', 'return 1;', 'unchanged']), [0, 1]);
  assert.deepEqual(reviewWhitespaceLines(['return "a b";'], ['return "ab";']), []);
  assert.deepEqual(reviewWhitespaceLines(['a  + b'], ['a + b']), []);
  assert.deepEqual(reviewWhitespaceLines(['line'], ['line', '']), []);
  assert.deepEqual(reviewWhitespaceLines(['\u00a0code'], ['code']), []);
});

test('language by file name', () => {
  assert.equal(langOf('src/A.java'), 'java');
  assert.equal(langOf('pom.xml'), 'xml');
  assert.equal(langOf('a/b/settings.env.properties'), 'props');
  assert.equal(langOf('Dockerfile'), 'sh');
  assert.equal(langOf('README'), 'plain');
});

test('java: keywords, types, strings, annotations, block comment across lines', () => {
  const [l1, l2, l3, l4] = highlightLines([
    '@Service public class Foo extends Bar { /* start',
    '  still comment */ String s = "a\\"b"; int n = 42;',
    '  // line comment',
    '  var t = """',
  ], 'java');
  assert.deepEqual(cls(l1), ['a:@Service', 'k:public', 'k:class', 't:Foo', 'k:extends', 't:Bar', 'c:/* start']);
  assert.deepEqual(cls(l2), ['c:still comment */', 't:String', 's:"a\\"b"', 'k:int', 'n:42']);
  assert.deepEqual(cls(l3), ['c:// line comment']);
  assert.deepEqual(cls(l4), ['k:var', 's:"""']);
});

test('xml tags, attributes, comments; json keys vs values; yaml keys', () => {
  const [x1, x2] = highlightLines(['<bean id="a" class=\'b\'> <!-- c', '--> text</bean>'], 'xml');
  assert.deepEqual(cls(x1), ['g:<bean', 'a:id', 's:"a"', 'a:class', "s:'b'", 'g:>', 'c:<!-- c']);
  assert.deepEqual(cls(x2), ['c:-->', 'g:</bean>']);
  const [j] = highlightLines(['{"k": "v", "n": 1, "b": true}'], 'json');
  assert.deepEqual(cls(j), ['p:"k"', 's:"v"', 'p:"n"', 'n:1', 'p:"b"', 'k:true']);
  const [y] = highlightLines(['  name: "x" # c'], 'yaml');
  assert.deepEqual(cls(y), ['p:name', 's:"x"', 'c:# c']);
});

test('the text of every line survives highlighting unchanged', () => {
  const lines = ['a /* b */ "c" `d ${e} f`', '<x a="1">&amp;</x>', 'SELECT * FROM t -- x', "echo \"$HOME\" # hi", ''];
  for (const lang of ['ts', 'xml', 'sql', 'sh', 'java', 'py', 'css', 'md', 'yaml', 'json', 'props']) {
    const out = highlightLines(lines, lang);
    assert.deepEqual(out.map((t) => t.map((x) => x.t).join('')), lines, lang);
  }
});

test('compact package labels', () => {
  assert.equal(chainLabel('src/main/java', 'com', 'acme/shop'), 'com.acme.shop');
  assert.equal(chainLabel('bridge', 'java_web', 'lib_x'), 'java_web/lib_x');
  assert.equal(chainLabel('.', 'src', ''), 'src');
});

import { toolOnly } from '../../piper/web/dist/convo.js';

test('tool-only turns are recognised (the transcript folds them into one line)', () => {
  assert.deepEqual(toolOnly('[tool Bash]\n[tool Edit]'), ['Bash', 'Edit']);
  assert.equal(toolOnly('Done. [tool Bash]'), null);
  assert.equal(toolOnly('plain text'), null);
});

import { dragSplit, evenFr, fitFr, legacyFr, paneRects, presetOf, PRESETS, resetSplit, shapeKey, shapeOf, splitBars } from '../../piper/web/dist/gridlayout.js';

test('grid layouts (W3): every preset is a shape of groups, capped to its capacity', () => {
  assert.deepEqual(PRESETS.map((p) => p.id), ['auto', 'columns', 'rows', 'one-rest', 'big-top', '2x2', '2x3', '3x3', '3-visible']);   // 9.45: 'two' gone
  assert.deepEqual(shapeOf('auto', 1), { dir: 'col', groups: [1] });
  assert.deepEqual(shapeOf('auto', 3), { dir: 'col', groups: [1, 2] });   // the pre-W3 layouts stay
  assert.deepEqual(shapeOf('auto', 4), { dir: 'col', groups: [2, 2] });
  assert.deepEqual(shapeOf('auto', 6), { dir: 'col', groups: [2, 2, 2] });
  assert.deepEqual(shapeOf('columns', 3), { dir: 'col', groups: [1, 1, 1] });
  assert.deepEqual(shapeOf('rows', 2), { dir: 'row', groups: [1, 1] });
  assert.deepEqual(shapeOf('one-rest', 4), { dir: 'col', groups: [1, 3] });
  assert.deepEqual(shapeOf('big-top', 4), { dir: 'row', groups: [1, 3] });
  assert.deepEqual(shapeOf('2x2', 6), { dir: 'col', groups: [2, 2] });     // capacity 4
  assert.deepEqual(shapeOf('2x3', 5), { dir: 'row', groups: [3, 2] });
  assert.deepEqual(shapeOf('3-visible', 4), { dir: 'col', groups: [1, 1, 1] });
  assert.deepEqual(shapeOf('auto', 0).groups, []);
  assert.equal(presetOf('nonsense').id, 'auto');
  assert.equal(shapeKey(shapeOf('one-rest', 3)), 'col:1,2');
});

test('grid layouts: pane boxes and splitters follow the fractions', () => {
  const sh = shapeOf('auto', 3);
  const [a, b, c] = paneRects(sh, { main: [0.3], cross: [[], [0.7]] });
  assert.equal(a.width, 'calc(30.000% - 5px)');
  assert.equal(a.height, 'calc(100.000% - 0px)');
  assert.equal(b.left, 'calc(30.000% + 5px)');
  assert.equal(b.height, 'calc(70.000% - 5px)');
  assert.equal(c.top, 'calc(70.000% + 5px)');
  const bars = splitBars(sh, { main: [0.3], cross: [[], [0.7]] });
  assert.deepEqual(bars.map((x) => [x.key, x.bar]), [['m0', 'v'], ['c1.0', 'h']]);
  assert.equal(bars[0].rect.left, 'calc(30.000% - 7px)');
  assert.equal(bars[1].rect.left, 'calc(30.000% + 5px)');   // over the right column only
  const rows = paneRects(shapeOf('big-top', 3), evenFr(shapeOf('big-top', 3)));
  assert.equal(rows[0].width, 'calc(100.000% - 0px)');
  assert.equal(rows[1].top, 'calc(50.000% + 5px)');
  assert.equal(rows[2].left, 'calc(50.000% + 5px)');
});

test('grid layouts: drag keeps a cut between its neighbours, reset centres it, bad saved splits are even', () => {
  const sh = shapeOf('columns', 3);
  let fr = evenFr(sh);
  assert.equal(fr.main.length, 2);
  fr = dragSplit(fr, -1, 0, 0.9);
  assert.ok(Math.abs(fr.main[0] - (fr.main[1] - 0.1)) < 1e-9);
  fr = dragSplit(fr, -1, 0, -1);
  assert.equal(fr.main[0], 0.1);
  assert.ok(Math.abs(resetSplit(fr, -1, 0).main[0] - fr.main[1] / 2) < 1e-9);
  assert.deepEqual(fitFr(sh, { main: [0.5] }), evenFr(sh));                 // another shape's value
  assert.deepEqual(fitFr(sh, { main: [0.6, 0.3], cross: [] }).main, evenFr(sh).main);   // not ascending
  assert.deepEqual(legacyFr(shapeOf('auto', 4), { col: 0.4, left: 0.3, right: 0.6 }), { main: [0.4], cross: [[0.3], [0.6]] });
  assert.equal(legacyFr(shapeOf('rows', 2), { col: 0.4 }), null);
});

// W3's grid pins (arrange) are gone in 9.45: drag a title onto another pane = swap (gridpanes.test.mjs swapPanes)

test('git window helpers (W5/W7)', async () => {
  const g = await import('../../piper/web/dist/gitwindow.js');
  assert.equal(g.shortDir('src/main/java/com/acme/webstore/checkout'), 'src/main/java/…/checkout');
  assert.equal(g.shortDir('a/b/c'), 'a/b/c');
  assert.equal(g.branchNameProblem(''), 'Enter a name.');
  assert.equal(g.branchNameProblem('feat/x'), '');
  for (const bad of ['a b', 'a..b', '-x', 'x/', 'x.lock', 'a/.b', 'x~1']) assert.equal(g.branchNameProblem(bad), 'Not a valid branch name.', bad);
  assert.equal(g.branchNameProblem('main', ['main']), 'A branch of that name exists.');
  assert.equal(g.worktreeLabel({ path: '/p/.claude/worktrees/fix-1', main: false }), '@fix-1');
  assert.equal(g.worktreeLabel({ path: '/p', main: true }), 'main checkout');
  assert.equal(g.worktreeNameProblem('fix-1', []), '');
  assert.match(g.worktreeNameProblem('a/b', []), /Letters/);
  assert.match(g.worktreeNameProblem('.x', []), /Letters/);
  assert.equal(g.worktreeNameProblem('fix-1', [{ path: '/p/.claude/worktrees/fix-1', main: false }]), 'A worktree of that name exists.');
  // a bad worktree name is not masked by a valid branch (operator precedence: name || new ? branch : existing)
  assert.match(g.newWorktreeProblem('a/b', null, [], []), /Letters/);
  assert.match(g.newWorktreeProblem('a/b', 'feat/x', [], []), /Letters/);
  assert.equal(g.newWorktreeProblem('pay', null, [], ['feat/pay']), 'A branch of that name exists.');
  assert.equal(g.newWorktreeProblem('pay', '', [], []), 'No free local branch.');
  assert.equal(g.newWorktreeProblem('pay', 'fix/old', [], []), '');
  const tree = g.fileTree([{ path: 'b/z.ts' }, { path: 'a.md' }, { path: 'b/a.ts' }]);
  assert.deepEqual(tree.map((x) => [x.dir, x.files.map((f) => f.path)]), [['', ['a.md']], ['b', ['b/a.ts', 'b/z.ts']]]);
  assert.deepEqual(g.treeOrder([{ path: 'b/z.ts' }, { path: 'a.md' }], false).map((f) => f.path), ['b/z.ts', 'a.md']);
});

test('commit panel (W6): amend is off once HEAD is pushed, the drafted note names withheld files', async () => {
  const { amendBlock, generatedNote } = await import('../../piper/web/dist/gitpanel.js');
  assert.equal(amendBlock({ upstream: 'origin/x', ahead: 1, log: ['abc123 fix'] }), '');
  assert.equal(amendBlock({ upstream: '', ahead: 0, log: ['abc123 fix'] }), '');
  assert.equal(amendBlock({ upstream: 'origin/x', ahead: 0, log: ['abc123 fix'] }), 'Amend off: HEAD abc123 is already pushed.');
  assert.equal(amendBlock({ upstream: '', ahead: 0, log: [] }), 'Amend off: no commit yet.');
  assert.equal(generatedNote(3, [], false), 'Drafted by Haiku from 3 files.');
  assert.equal(generatedNote(1, ['conf/.env'], true), 'Drafted by Haiku from 1 file. .env is hidden and was not sent. The diff was cut to fit.');
});

test('askedText: a paste is a marker on the card, tags never show', () => {
  assert.equal(askedText('<pasted_content id="9f24">\nlong log\n</pasted_content> analyze'), '[pasted text]  analyze');
  assert.equal(askedText('<pasted_content id="1">unterminated'), '[pasted text]');
  assert.equal(askedText(' change List<String> to <details> '), 'change List<String> to <details>');
});

test('isJunk: OS droppings start unchecked in the commit list, real files do not', async () => {
  const { isJunk } = await import('../../piper/web/dist/gitpanel.js');
  assert.equal(isJunk('.DS_Store'), true);
  assert.equal(isJunk('java_web/.DS_Store'), true);
  assert.equal(isJunk('src/._Foo.java'), true);
  assert.equal(isJunk('src/Foo.java'), false);
  assert.equal(isJunk('docs/DS_Store.md'), false);
  assert.equal(isJunk('development/feeds/prod.xml'), true);   // the scratch directory is never committed
  assert.equal(isJunk('shop/development/x.sql'), true);
  assert.equal(isJunk('src/developmentTools.java'), false);
});

test('exitReason: the end bar says why, not just "rc 143"', async () => {
  const { exitReason } = await import('../../piper/web/dist/text.js');
  assert.equal(exitReason(143), 'ended by stop (code 143)');
  assert.equal(exitReason(137), 'ended forcibly (SIGKILL) (code 137)');
  assert.equal(exitReason(0), 'Claude finished');
  assert.equal(exitReason(1), 'ended with an error (code 1)');
  assert.equal(exitReason(null), 'reason unknown');
});

test('splitJunk: OS droppings and development/ scratch go to the folded group, order kept', async () => {
  const { splitJunk } = await import('../../piper/web/dist/gitpanel.js');
  const files = ['src/A.java', '.DS_Store', 'development/x.py', 'docs/b.md', 'java_web/.DS_Store'].map((path) => ({ path }));
  const [real, junk] = splitJunk(files);
  assert.deepEqual(real.map((f) => f.path), ['src/A.java', 'docs/b.md']);
  assert.deepEqual(junk.map((f) => f.path), ['.DS_Store', 'development/x.py', 'java_web/.DS_Store']);
});

test('lastParagraph: the last prose paragraph as plain text, code fences skipped', async () => {
  const { lastParagraph } = await import('../../piper/web/dist/text.js');
  assert.equal(lastParagraph('Intro.\n\n```sh\nls -la\n```\n\n**Done:** see [the log](http://x) and `a.ts`.\n'),
    'Done: see the log and a.ts.');
  // a tail cut inside a fence: the half fence is not prose
  assert.equal(lastParagraph('h -L 8443:192.0.2.1:443 host\n```\nOutput goes to Loki.\n\n1. **Run it** yourself.\n2. Ask ops.'),
    'Run it yourself. Ask ops.');
  assert.equal(lastParagraph('only code\n```\nx\n```'), 'only code');
  assert.equal(lastParagraph(''), '');
  assert.equal(lastParagraph('a'.repeat(500), 10), 'aaaaaaaaa…');
});

test('changeMark: one letter per porcelain XY or turn status, with what the index holds', async () => {
  const { changeMark } = await import('../../piper/web/dist/gitpanel.js');
  const m = (xy) => { const r = changeMark(xy); return `${r.letter} ${r.cls} ${r.word}`; };
  assert.equal(m(' M'), 'M mod modified, not staged');
  assert.equal(m('M '), 'M mod modified, staged');
  assert.equal(m('MM'), 'M mod modified, partly staged');
  assert.equal(m('??'), 'U new untracked');
  assert.equal(m('A '), 'A add added, staged');
  assert.equal(m(' D'), 'D del deleted, not staged');
  assert.equal(m('R '), 'R ren renamed, staged');
  assert.equal(m('UU'), '! bad conflict');
  assert.equal(m('AA'), '! bad conflict');
  assert.equal(m('A'), 'A add added');   // a turn's status letter
  assert.equal(m('R100'), 'R ren renamed');
});


test('review navigation uses Command-click on Mac and Control-click elsewhere', () => {
  assert.ok(sourceClick({metaKey: true, ctrlKey: false}, true));
  assert.ok(!sourceClick({metaKey: false, ctrlKey: true}, true));
  assert.ok(sourceClick({metaKey: false, ctrlKey: true}, false));
  assert.ok(!sourceClick({metaKey: false, ctrlKey: false}, false));
});

test('review groups tooling separately and pairs source with its regression test', () => {
  for (const [path, group] of [['package-lock.json','Lock files'],['dist/app.js','Generated'],['development/x.txt','Scratch'],['src/A.java','Changes']])
    assert.equal(reviewGroup({path}),group);
  // hidden from Claude is a mark only: the human reviews the file in its usual group (15.18); `withheld` has no content
  assert.equal(reviewGroup({path:'src/A.java',sensitive:true}),'Changes');
  assert.equal(reviewGroup({path:'../outside.java',withheld:true}),'Withheld');
  const paths=['src/main/java/x/B.java','src/test/java/x/ATest.java','src/main/java/x/A.java'];
  assert.deepEqual(reviewOrder(paths.map(path=>({path}))).map(x=>x.path),[paths[2],paths[1],paths[0]]);
  assert.deepEqual(reviewOrder(['b.py','tests/test_a.py','a.py'].map(path=>({path}))).map(x=>x.path),['a.py','tests/test_a.py','b.py']);
});

test('review traversal follows displayed groups before sorting paths within each group', () => {
  const files = [{path:'z.py'}, {path:'a-new.py',live_status:'??'}, {path:'package-lock.json'},
    {path:'b.py'}, {path:'dist/app.js'}, {path:'outside.txt',withheld:true}, {path:'private.txt',sensitive:true}];
  assert.deepEqual(reviewNavigationOrder(files).map(x=>x.path), ['b.py','private.txt','z.py','a-new.py','package-lock.json','dist/app.js','outside.txt']);
});

test('Viewed is invalidated by either blob, HEAD, mode, rename source or live bytes', () => {
  const file={path:'a',before:{oid:'old',mode:'100644'},after:{oid:'new',mode:'100644'},work_sha:'raw',work_mode:'100644'};
  const key=reviewKey(file,'head');
  for(const change of [{before:{oid:'other',mode:'100644'}},{after:{oid:'new',mode:'100755'}},{orig:'b'},{work_sha:'edited'},{orig_after:{oid:null,mode:null}}])
    assert.notEqual(reviewKey({...file,...change},'head'),key);
  assert.notEqual(reviewKey(file,'next-head'),key);
  assert.equal(reviewKey({...file},'head'),key);
});

test('the review notes message carries the code excerpt', () => {
  assert.match(notesMessage([{path:'a',line:3,old:false,text:'Fix',excerpt:'return 7;'}]), /Code: return 7;/);
});

test('N2: a file name with a bidi control or an invisible character shows it, never reorders (rtl-\u202Etxt.md is not rtl-dm.txt)', () => {
  assert.equal(showBidi('rtl-\u202Etxt.md'), 'rtl-[U+202E]txt.md');
  assert.equal(showBidi('a\u2066b\u2069c\u200Fd'), 'a[U+2066]b[U+2069]c[U+200F]d');
  assert.equal(showBidi('zero\u200Bwidth\uFEFF'), 'zero[U+200B]width[U+FEFF]');
  assert.equal(showBidi('tag\u{E0041}'), 'tag[U+E0041]');
  assert.equal(showBidi('Jürgen Müller 🎉 עברית.md'), 'Jürgen Müller 🎉 עברית.md');   // letters and emoji stay
});

test('N4: Files search hits, the tree filter, the @ tooltip and the Changes / diff modal show bidi controls as marks (render, over dist)', async () => {
  const { readFileSync } = await import('node:fs');
  const js = (f) => readFileSync(new URL(`../../piper/web/dist/${f}`, import.meta.url), 'utf8');
  const files = js('files.js');
  const git = js('gitpanel.js');
  for (const s of ['tx.append(showBidi(text))', "h('mark', {}, showBidi(hit[0]))", 'nm.append(showBidi(name.slice(at)))', 'insert ${showBidi(reference(',
    "title: showBidi(d)"]) assert.ok(files.includes(s), s);
  for (const s of ["class: 'link gf', title: showBidi(title)", "'Review ' + showBidi(file.path)"]) assert.ok(git.includes(s), s);
});

test('N4: repository history / graph / blame and the review items show git text and notes with bidi controls as marks (over dist)', async () => {
  const { readFileSync } = await import('node:fs');
  const js = (f) => readFileSync(new URL(`../../piper/web/dist/${f}`, import.meta.url), 'utf8');
  const hist = js('history.js');
  for (const s of ["'hsubj' }, showBidi(c.subject)", 'showBidi(c.author)', 'showBidi(r.name)', 'showBidi(l.author)', 'showBidi(d.text)']) assert.ok(hist.includes(s), s);
  const git = js('gitpanel.js');
  for (const s of ["'small' }, showBidi(item.text)", "'review-excerpt mono small' }, showBidi(target.code)", "'Select ' + showBidi(file.path)"]) assert.ok(git.includes(s), s);
});

test('N9: Audit - a filter that hides every record says so (was a blank page); no records at all keeps its own line', () => {
  assert.equal(auditEmptyLine(0, 0), 'No records.');
  assert.equal(auditEmptyLine(12, 0), 'Nothing matches the filter.');
  assert.equal(auditEmptyLine(12, 3), '');
});

test('N9: a shortened title / line never splits an emoji into a lone surrogate (�): cutText and every … truncation use it', async () => {
  const { cutText } = await import('../../piper/web/dist/text.js');
  const { firstLine } = await import('../../piper/web/dist/queue.js');
  const lone = (s) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);
  assert.equal(cutText('ab\u{1F600}c', 3), 'ab');
  assert.equal(cutText('ab\u{1F600}c', 4), 'ab\u{1F600}');
  assert.equal(cutText('abc', 10), 'abc');
  assert.equal(lone(firstLine('y'.repeat(138) + '\u{1F600}abc')), false);
  const { readFileSync } = await import('node:fs');
  for (const f of ['queue', 'turns', 'httpview', 'diag', 'inbox', 'board', 'files', 'composer']) {
    const src = readFileSync(new URL(`../../piper/web/src/${f}.ts`, import.meta.url), 'utf8');
    assert.doesNotMatch(src, /\.slice\(0, [\w -]+\) \+ '…'/, `${f}.ts cuts text with .slice(0, n) + '…'`);
  }
  assert.match(readFileSync(new URL('../../piper/web/src/composer.ts', import.meta.url), 'utf8'), /const name = cutText\(/);
});

test('S5: the Push dialog button - a block check disables it, the label counts the commits (N10-B-6: never "0 commits")', async () => {
  const { pushButton } = await import('../../piper/web/dist/gitwindow.js');
  const c = { hash: 'a', short: 'a', author: 'x', date: '', subject: 's', merge: false };
  assert.deepEqual(pushButton({ commits: [c], more: false, checks: [] }), { label: 'Push 1 commit', disabled: false });
  assert.deepEqual(pushButton({ commits: [c, c], more: false, checks: [{ level: 'warn', text: 'w' }] }), { label: 'Push 2 commits', disabled: false });
  assert.deepEqual(pushButton({ commits: [c, c], more: true, checks: [] }), { label: 'Push 2+ commits', disabled: false });
  assert.deepEqual(pushButton({ commits: [], more: false, checks: [{ level: 'info', text: 'creates the branch' }] }), { label: 'Push', disabled: false });
  assert.deepEqual(pushButton({ commits: [c], more: false, checks: [{ level: 'block', text: 'behind' }] }), { label: 'Push 1 commit', disabled: true });
});

test('N10-B-7: Review opened by Commit keeps the Changes panel\'s ticks, a hidden file ticked there by hand too ("Commit (1)" opened "0 selected")', async () => {
  const { reviewPreselect } = await import('../../piper/web/dist/gitpanel.js');
  const files = [
    { path: 'secret-notes.md', sensitive: true, committable: true },
    { path: 'src/Util.java', committable: true },
    { path: 'src/Other.java', committable: true },
    { path: 'gone.bin', committable: false },
    { path: 'new.txt', committable: true, live_status: '??' },
  ];
  // ticked in the panel: exactly those (the hidden one only because the human ticked it there)
  assert.deepEqual(reviewPreselect(files, new Set(['secret-notes.md', 'src/Util.java'])), ['secret-notes.md', 'src/Util.java']);
  assert.deepEqual(reviewPreselect(files, new Set(['secret-notes.md'])), ['secret-notes.md']);
  // no panel status yet: every committable change, never a hidden file
  assert.deepEqual(reviewPreselect(files, null), ['src/Util.java', 'src/Other.java']);
});

test('a protected mount names the mount to change: include, library, project', () => {
  const base = { kind: 'protected-mounted', path: '/u/sk', source: '/u/sk', rw: true, mode: 'rw', what: 'Piper\u2019s own code' };
  const inc = protectedText({ ...base, via: 'include' });
  assert.equal(inc.title, 'Cannot start: the sandbox would be able to write Piper’s own code');
  assert.match(inc.what, /^\/u\/sk is mounted read-write as an include \(files\.include\) – it is Piper’s own code\. A sandbox/);   // the source is the dir: its path once
  assert.match(inc.fix, /remove that files\.include entry .*, or make it read-only, then Retry\.$/);
  assert.match(protectedText({ ...base, via: 'library', source: '/u' }).what, /as a library – it contains Piper’s own code \(\/u\/sk\)\./);
  assert.match(protectedText({ ...base, via: 'project' }).fix, /another directory – to work on it, use a separate clone/);
  const any = protectedText({ ...base, via: 'library', mode: 'any', rw: false, path: '/h/.piper', source: '/h/.piper/x', what: 'Piper\u2019s state' });
  assert.match(any.title, /would see Piper’s state$/);
  assert.match(any.what, /read-only .* lies inside/);
  assert.doesNotMatch(any.fix, /read-only/);   // a read-only mount of an "any" dir is still forbidden
});

test('quiet startup shows hooks and Docker, with a clock independent of state polling', () => {
  const s = { state: 'starting', phase: 'hooks', detail: '', since: 10, phase_since: 50 };
  assert.equal(startupStatus(s).title, 'Running host setup');
  assert.equal(startupStatus({ ...s, phase: 'dind' }).title, 'Waiting for Docker');
  assert.equal(startupStatus({ ...s, phase: 'cache' }).step, 2);
  assert.equal(startupStatus({ ...s, phase: 'agent' }).step, 3);
  // 9.54: a start that took the image rebuild (hole -r) shows its own Image stage at the gateway
  const rb = { ...s, rebuild: true };
  assert.deepEqual(startupStatus(rb).stages, ['Prepare', 'Sandbox', 'Network', 'Image', 'Agent', 'Verify']);
  assert.equal(startupStatus(s).stages.length, 5);
  assert.deepEqual([startupStatus({ ...rb, phase: 'gateway' }).step, startupStatus({ ...rb, phase: 'gateway' }).title],
    [3, 'Rebuilding the sandbox image']);
  assert.match(startupStatus({ ...rb, phase: 'gateway' }).hint, /minutes/);
  assert.deepEqual(['cache', 'gateway', 'dind', 'agent', 'attach'].map((phase) => startupStatus({ ...rb, phase }).step), [2, 3, 3, 4, 5]);
  assert.equal(startupStatus({ ...rb, state: 'attesting' }).step, 5);
  assert.equal(startupStatus({ ...s, phase: 'unknown' }).step, 1);
  assert.equal(startupStatus({ ...s, state: 'preparing' }).step, 0);
  assert.equal(startupStatus({ ...s, state: 'attesting' }).since, 10);
  assert.equal(startupStatus(s).since, 50);
  assert.equal(startupElapsed(50, 52), '2s');
  assert.equal(startupElapsed(50, 121), '1m 11s');
  assert.equal(startupElapsed(50, 40), '0s');
  assert.equal(startupElapsed(undefined, 60), '0s');
  assert.equal(startupStatus({ ...s, phase_since: undefined }).since, 10);
});

test('a settings file never approved is a first review, not a change', () => {
  assert.equal(blockGroupKey({ kind: 'settings-changed', path: '/p/.hole/settings.json', previous: null }), 'settings-new');
  assert.equal(blockGroupKey({ kind: 'settings-changed', path: '/p/.hole/settings.json' }), 'settings-new');
  assert.equal(blockGroupKey({ kind: 'settings-changed', path: '/p/.hole/settings.json', previous: 'abc' }), 'settings-changed');
  assert.equal(blockGroupKey({ kind: 'uncovered', path: '/p/x', detail: '/p' }), 'uncovered|/p');
  assert.equal(blockGroupKey({ kind: 'forbidden-key', path: '/p/.hole/settings.json' }), 'forbidden-key');
});

test('F08: git window chrome is quiet: neutral ref chips with a type dot, Push carries the unpushed count', async () => {
  const { readFileSync } = await import('node:fs');
  const { pushLabel } = await import('../../piper/web/dist/gitwindow.js');
  assert.equal(pushLabel(2), '↑ Push 2');
  assert.equal(pushLabel(0), 'Push');
  assert.equal(pushLabel(undefined), 'Push');
  const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
  // both the window and the History view draw refs through the one shared chip
  assert.match(src('history.ts'), /export function refChip\(r: Ref\)[\s\S]*?ref-dot/);
  assert.match(src('gitwindow.ts'), /parseRefs\(text\)\.map\(refChip\)/);
  // Fetch / Pull are ghost buttons, Push stays a filled button (its dialog is untouched)
  const gw = src('gitwindow.ts');
  assert.match(gw, /mini ghost gw-act[^\n]*remote\('fetch'\)/);
  assert.match(gw, /mini ghost gw-act[^\n]*remote\('pull'\)/);
  assert.match(gw, /class: 'mini gw-push'/);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const block = css.slice(css.indexOf('F08 Git window'), css.indexOf('end F08'));
  assert.ok(block.length > 0);
  assert.match(block, /\.ref \{[^}]*border: 0[^}]*background: var\(--tile\)/);
  for (const k of ['local', 'remote', 'tag']) assert.match(block, new RegExp(`\\.ref-${k} \\.ref-dot`));
  assert.match(block, /\.ref\.ref-head \{[^}]*font-weight: 600/);
});

// F05 (9.77): a message sent while its session works is held as a card (Edit / ✕) and goes when the turn ends
test('F05 queue: one per session oldest first, busy / cooling / failed wait, Edit takes the text back', () => {
  let q = enqueue([], 's1', 'first', 'a');
  q = enqueue(q, 's1', 'second', 'b');
  q = enqueue(q, 's2', 'other', 'c');
  assert.deepEqual(q.map((x) => x.id), ['a', 'b', 'c']);
  assert.equal(nextReady(q, () => false)?.id, 'a');
  assert.equal(nextReady(q, (id) => id === 's1')?.id, 'c', 'a working session holds its messages, another goes');
  assert.equal(nextReady(q, () => false, (id) => id === 's1')?.id, 'c', 'just delivered: the next waits');
  assert.equal(nextReady([{ ...q[0], failed: 'x' }, q[1]], () => false), null, 'a failed head keeps the order');
  assert.equal(nextReady(q, () => true), null);
  const t = takeQueued(q, 'b');
  assert.equal(t.text, 'second');
  assert.deepEqual(t.q.map((x) => x.id), ['a', 'c']);
  assert.equal(takeQueued(q, 'zz').text, '');
});

test('F05 structure: the cards sit in the composer above the field, a busy send is held, not typed', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/composer.ts', import.meta.url), 'utf8');
  assert.match(src, /el\.append\(qEl, ta,/);
  assert.match(src, /if \(busy\) \{[^}]*enqueue\(/);
  assert.match(src, /if \(busy \|\| await deps\.sendTo\(to, text\)\)/);
  assert.match(src, /'Edit'\)/);
  assert.match(src, /QUEUE_KEY/);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const blk = css.slice(css.indexOf('9.77 F05'), css.indexOf('end F05'));
  assert.ok(blk.includes('.cq-card') && blk.includes('.trow'), 'F05 css block');
  assert.ok(!/font-size:\s*\d+px/.test(blk) && !/border-radius:\s*\d+px/.test(blk), 'scale tokens only');
});
