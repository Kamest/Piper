// node --test: pure helpers of the file browser / editor, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  identChars, identifierAt, outline, parseLineCol, quickQuery, resolveRel,
  treeNav, usagePattern,
} from '../../piper/web/dist/codenav.js';
import { inlineText, linkKind, parseInline, parseMarkdown, slug, tableCells } from '../../piper/web/dist/markdown.js';
import { diffStat, pairDiff, sideTokens } from '../../piper/web/dist/sidediff.js';
import { fileContent, indentUnit } from '../../piper/web/dist/editor.js';
import { IMAGE_RE, tabLabels } from '../../piper/web/dist/files.js';

// ---------------------------------------------------------------- side-by-side diff

const DIFF = [
  'diff --git a/A.java b/A.java', 'index 1..2 100644', '--- a/A.java', '+++ b/A.java',
  '@@ -1,5 +1,6 @@ class A',
  ' keep', '-old1', '-old2', '+new1', ' mid', '+added', ' tail', '-gone',
  '\\ No newline at end of file',
].join('\n');

test('a unified diff pairs removed runs with the added runs that replace them', () => {
  const rows = pairDiff(DIFF);
  const show = rows.map((r) => (r.kind === 'hunk' || r.kind === 'file' ? r.kind
    : `${r.kind} ${r.left ? r.left.no + ':' + r.left.text : '-'} | ${r.right ? r.right.no + ':' + r.right.text : '-'}`));
  assert.deepEqual(show, [
    'hunk',
    'ctx 1:keep | 1:keep',
    'chg 2:old1 | 2:new1',
    'del 3:old2 | -',
    'ctx 4:mid | 3:mid',
    'add - | 4:added',
    'ctx 5:tail | 5:tail',
    'del 6:gone | -',
  ]);
  assert.deepEqual(diffStat(rows), { add: 2, del: 3 });
});

test('a new file (vs /dev/null) and two files in one diff', () => {
  const nu = pairDiff(['diff --git a/n b/n', 'new file mode 100644', '--- /dev/null', '+++ b/n', '@@ -0,0 +1,2 @@', '+a', '+b', ''].join('\n'));
  assert.deepEqual(nu.filter((r) => r.kind === 'add').map((r) => r.right.no), [1, 2]);
  const two = pairDiff(DIFF + '\n' + ['diff --git a/B.ts b/B.ts', '--- a/B.ts', '+++ b/B.ts', '@@ -3 +3 @@', '-x', '+y'].join('\n'));
  assert.deepEqual(two.filter((r) => r.kind === 'file').map((r) => r.text), ['B.ts']);
  assert.equal(two.at(-1).kind, 'chg');
  assert.equal(two.at(-1).left.no, 3);
  assert.deepEqual(pairDiff(''), []);
});

test('a removed "--" line inside a hunk is content, not a header', () => {
  const rows = pairDiff(['--- a/x.sql', '+++ b/x.sql', '@@ -10,2 +10,2 @@', '--- old comment', '+-- new comment', ' tail'].join('\n'));
  assert.equal(rows[1].kind, 'chg');
  assert.equal(rows[1].left.text, '-- old comment');
  assert.equal(rows[1].right.text, '-- new comment');
});

test('highlighting per side keeps each cell text unchanged', () => {
  const rows = pairDiff(DIFF);
  const { left, right } = sideTokens(rows, 'java');
  rows.forEach((r, i) => {
    if (r.kind === 'hunk' || r.kind === 'file') return assert.equal(left[i], null);
    assert.equal(left[i] ? left[i].map((t) => t.t).join('') : null, r.left ? r.left.text : null);
    assert.equal(right[i] ? right[i].map((t) => t.t).join('') : null, r.right ? r.right.text : null);
  });
});

// ---------------------------------------------------------------- markdown

test('markdown blocks: headings, lists (nested, tasks), code fences, quotes, tables, hr', () => {
  const b = parseMarkdown([
    '# Title', '', 'Some *text* and **bold**', 'continued.', '',
    '- one', '- two', '  - nested', '- [x] done', '',
    '1. first', '2. second', '',
    '```java', 'int a = 1;', '```', '',
    '> quoted', '',
    '| A | B |', '|:--|--:|', '| 1 | `x|y` |', '',
    '---',
  ].join('\n'));
  assert.deepEqual(b.map((x) => x.t), ['h', 'p', 'list', 'list', 'code', 'quote', 'table', 'hr']);
  assert.equal(b[0].level, 1);
  assert.equal(b[0].line, 1);
  assert.equal(inlineText(b[1].c), 'Some text and bold continued.');
  const ul = b[2];
  assert.equal(ul.ordered, false);
  assert.equal(ul.items.length, 3);
  assert.equal(ul.items[1].blocks[1].t, 'list');   // "two" holds the nested list
  assert.equal(ul.items[2].check, true);
  assert.equal(b[3].ordered, true);
  assert.equal(b[3].items.length, 2);
  assert.deepEqual(b[4].text, ['int a = 1;']);
  assert.equal(b[4].lang, 'java');
  assert.equal(b[4].line, 14);
  assert.deepEqual(b[6].align, ['left', 'right']);
  assert.equal(b[6].rows[0][1][0].t, 'code');
  assert.equal(b[6].rows[0][1][0].v, 'x|y');
});

test('markdown inline: code, emphasis, snake_case, links, autolinks, escapes', () => {
  const kinds = (s) => parseInline(s).map((x) => x.t + (x.v !== undefined ? ':' + x.v : ''));
  assert.deepEqual(kinds('a `b*c` d'), ['text:a ', 'code:b*c', 'text: d']);
  assert.deepEqual(kinds('snake_case_name'), ['text:snake_case_name']);
  assert.deepEqual(kinds('\\*not\\*'), ['text:*not*']);
  const [a] = parseInline('[x](https://e.example/a_(b))');
  assert.equal(a.t, 'a');
  assert.equal(a.href, 'https://e.example/a_(b)');
  const auto = parseInline('see https://e.example/x. ok');
  assert.equal(auto[1].href, 'https://e.example/x');
  assert.equal(parseInline('**b *i* b**')[0].c[1].t, 'i');
});

test('markdown links: only http(s) become links, javascript: and data: stay text', () => {
  assert.equal(linkKind('https://x.example'), 'web');
  assert.equal(linkKind('HTTP://x.example'), 'web');
  assert.equal(linkKind('javascript:alert(1)'), 'bad');
  assert.equal(linkKind(' JavaScript:alert(1)'), 'bad');
  assert.equal(linkKind('data:text/html,x'), 'bad');
  assert.equal(linkKind('//evil.example/x'), 'bad');
  assert.equal(linkKind('../README.md'), 'rel');
  assert.equal(linkKind('#sekce'), 'anchor');
  const [a] = parseInline('[x](javascript:alert(1))');
  assert.equal(a.href, 'javascript:alert(1)');   // parsed, but the renderer shows it as text (linkKind 'bad')
  assert.equal(slug('Résumé: API (v2)'), 'résumé-api-v2');
  assert.deepEqual(tableCells('| a | b \\| c |'), ['a', 'b | c']);
});

test('relative Markdown links resolve inside the project only', () => {
  assert.equal(resolveRel('development/a/NOTE.md', '../README.md'), 'development/README.md');
  assert.equal(resolveRel('NOTE.md', './src/A.java#L3'), 'src/A.java');
  assert.equal(resolveRel('NOTE.md', '../../etc/passwd'), null);
  assert.equal(resolveRel('docs/x.md', '/README.md'), 'README.md');
  assert.equal(resolveRel('docs/x.md', 'my%20file.md'), 'docs/my file.md');
});

// ---------------------------------------------------------------- outline

test('outline: java classes, methods, constructors - not calls or control flow', () => {
  const o = outline([
    'package a;',
    '/** doc',
    ' * void fake() {',
    ' */',
    '@Service',
    'public class Foo extends Bar {',
    '  private final String x;',
    '  public Foo(String x) {',
    '    if (x != null) {',
    '      doIt(x);',
    '    }',
    '    return;',
    '  }',
    '  @Override',
    '  public List<String> names(int a,',
    '      int b) {',
    '    String s = call(a);',
    '    new Thread(() -> {});',
    '  }',
    '  enum Kind { A, B }',
    '  record R(int a) {}',
    '}',
  ], 'src/Foo.java');
  assert.deepEqual(o.map((x) => `${x.depth}:${x.kind}:${x.name}:${x.line}`), [
    '0:class:Foo:6', '1:method:Foo:8', '1:method:names:15', '1:enum:Kind:20', '1:record:R:21',
  ]);
});

test('outline: ts, python, markdown, sql, sh, xml, properties', () => {
  const ts = outline([
    'export interface A {}', 'export type B = string;', 'export function f() {}', 'const g = async (x: number) => x;',
    'export const H = 1;', 'class C {', '  method(a: string): void {', '  }', '  if (x) {', '}',
  ], 'a.ts');
  assert.deepEqual(ts.map((x) => `${x.kind}:${x.name}`), ['interface:A', 'type:B', 'function:f', 'function:g', 'const:H', 'class:C', 'method:method']);
  assert.deepEqual(outline(['class A:', '    def b(self):', 'async def c():'], 'x.py').map((x) => `${x.depth}${x.name}`), ['0A', '1b', '0c']);
  assert.deepEqual(outline(['# T', '```', '# not', '```', '## S'], 'R.md').map((x) => `${x.depth}${x.name}`), ['0T', '1S']);
  assert.deepEqual(outline(['CREATE OR REPLACE VIEW v_a AS', 'create table if not exists "t"'], 'x.sql').map((x) => x.kind + ':' + x.name), ['view:v_a', 'table:t']);
  assert.deepEqual(outline(['foo() {', 'function bar {', 'if [ x ]; then'], 'x.sh').map((x) => x.name), ['foo', 'bar']);
  assert.deepEqual(outline(['<bean id="svc" class="X"/>', '<div id="main">'], 'a.xml').map((x) => x.kind + ':' + x.name), ['bean:svc', 'div:main']);
  assert.deepEqual(outline(['# c', 'a.b=1', 'c : 2'], 'x.properties').map((x) => x.name), ['a.b', 'c']);
});

// ---------------------------------------------------------------- identifiers, find, quick open

test('identifier at an offset, property keys keep their dots', () => {
  assert.deepEqual(identifierAt('  foo.barBaz(x)', 8), { name: 'barBaz', start: 6, end: 12 });
  assert.equal(identifierAt('  foo.barBaz(x)', 12).name, 'barBaz');   // right after the word
  assert.equal(identifierAt('a + 42', 4), null);
  assert.equal(identifierAt('x  y', 2), null);
  assert.equal(identifierAt('${my.app.key}', 5, identChars('a.properties')).name, 'my.app.key');
  assert.equal(identifierAt('my.app.key.', 3, '.-').name, 'my.app.key');
  assert.equal(identChars('A.java'), '');
  assert.equal(usagePattern('Foo'), '\\bFoo\\b');
  assert.equal(usagePattern('$x'), '\\$x\\b');
});

test('quick open: file:line, #symbol, go-to-line input', () => {
  assert.deepEqual(quickQuery('Foo.java:120'), { kind: 'file', q: 'Foo.java', line: 120 });
  assert.deepEqual(quickQuery('src/Foo'), { kind: 'file', q: 'src/Foo', line: 0 });
  assert.deepEqual(quickQuery('#TokenKeeper'), { kind: 'symbol', q: 'TokenKeeper', line: 0 });
  assert.deepEqual(quickQuery('@bean'), { kind: 'symbol', q: 'bean', line: 0 });
  assert.deepEqual(parseLineCol('42'), { line: 42, col: 0 });
  assert.deepEqual(parseLineCol(':42:7'), { line: 42, col: 7 });
  assert.equal(parseLineCol('abc'), null);
});

// ---------------------------------------------------------------- tree keyboard

test('tree keys: move, expand, first child, collapse, parent, open', () => {
  const rows = [
    { depth: 0, dir: true, open: true },    // 0 src
    { depth: 1, dir: true, open: false },   // 1   main
    { depth: 1, dir: false, open: false },  // 2   a.ts
    { depth: 0, dir: false, open: false },  // 3 README
  ];
  assert.deepEqual(treeNav(rows, 0, 'ArrowDown'), { i: 1, act: 'none' });
  assert.deepEqual(treeNav(rows, 3, 'ArrowDown'), { i: 3, act: 'none' });
  assert.deepEqual(treeNav(rows, 0, 'ArrowUp'), { i: 0, act: 'none' });
  assert.deepEqual(treeNav(rows, 1, 'ArrowRight'), { i: 1, act: 'toggle' });
  assert.deepEqual(treeNav(rows, 0, 'ArrowRight'), { i: 1, act: 'none' });
  assert.deepEqual(treeNav(rows, 0, 'ArrowLeft'), { i: 0, act: 'toggle' });
  assert.deepEqual(treeNav(rows, 2, 'ArrowLeft'), { i: 0, act: 'none' });
  assert.deepEqual(treeNav(rows, 3, 'ArrowLeft'), { i: 3, act: 'none' });
  assert.deepEqual(treeNav(rows, 2, 'Enter'), { i: 2, act: 'open' });
  assert.deepEqual(treeNav(rows, 1, 'Enter'), { i: 1, act: 'toggle' });
  assert.deepEqual(treeNav(rows, 2, 'ArrowRight'), { i: 2, act: 'none' });
  assert.deepEqual(treeNav(rows, -1, 'ArrowDown'), { i: 0, act: 'none' });
  assert.deepEqual(treeNav(rows, 1, 'End'), { i: 3, act: 'none' });
  assert.deepEqual(treeNav([], 0, 'ArrowDown'), { i: -1, act: 'none' });
});

// ---------------------------------------------------------------- change markers, tabs

test('tab labels get the parent directory only when names clash; images by extension', () => {
  assert.deepEqual(tabLabels(['a/x/Foo.java', 'b/y/Foo.java', 'README.md']), ['Foo.java · x', 'Foo.java · y', 'README.md']);
  assert.ok(IMAGE_RE.test('img/logo.SVG'));
  assert.ok(!IMAGE_RE.test('a.svg.txt'));
});

// ---------------------------------------------------------------- editor

test('indentation unit of a file', () => {
  assert.equal(indentUnit(['class A {', '    int a;', '    void b() {', '        c();', '    }', '}']), '    ');
  assert.equal(indentUnit(['a:', '  b:', '    c: 1', '  d: 2']), '  ');
  assert.equal(indentUnit(['x', '\ty', '\t\tz']), '\t');
  assert.equal(indentUnit(['x', 'y'], '  '), '  ');
});

test('edited text becomes file content with LF and one final newline', () => {
  assert.equal(fileContent('a\nb'), 'a\nb\n');
  assert.equal(fileContent('a\r\nb'), 'a\nb\n');
  assert.equal(fileContent('a\n'), 'a\n\n');   // an empty last line stays (read_file dropped only the final newline)
  assert.equal(fileContent(''), '');
  assert.equal(fileContent('a\nb', '\r\n'), 'a\r\nb\r\n');
});

test('parseInline: GitLab image size attributes are dropped, not shown as text', () => {
  const r = parseInline('a ![image](/uploads/x.png){width=753 height=362} b');
  assert.deepEqual(r.map((x) => x.t), ['text', 'img', 'text']);
  assert.equal(r[2].v, ' b');
  assert.equal(parseInline('{width=1} plain')[0].v, '{width=1} plain');
});


test('review source caret follows inserted/deleted chunks instead of old line numbers', async () => {
  const {reviewSourcePosition}=await import('../../piper/web/dist/cmeditor.js');
  const chunks=[{fromA:10,toA:10,fromB:10,toB:20},{fromA:30,toA:40,fromB:40,toB:40}];
  assert.equal(reviewSourcePosition(5,chunks,100),5);
  assert.equal(reviewSourcePosition(15,chunks,100),25);
  assert.equal(reviewSourcePosition(35,chunks,100),40);
  assert.equal(reviewSourcePosition(45,chunks,100),45);
  assert.equal(reviewSourcePosition(500,chunks,100),100);
});
