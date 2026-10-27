// Files redesign: the pure helpers behind ⌘P ranking, the tree, folding, the scope crumbs,
// the session-edit marks, inline blame and the usages panel.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { dirsOf, fileKind, fileRefs, importBlock, leftCut, matchSpans, outline, rankFiles, scopeAt } from '../../piper/web/dist/codenav.js';
import { AUTOSAVE_MS, blameText, changedLines, diffMetadata, emptyHub, groupHits, restoredFileFocus, scopePills } from '../../piper/web/dist/files.js';
import { readFileSync } from 'node:fs';
import { guessIndent } from '../../piper/web/dist/cmeditor.js';
import { HIDDEN_TITLE } from '../../piper/web/dist/gitpanel.js';

test('restored tabs preserve a newer explicit HTTP source navigation', () => {
  const paths = ['previous.http', 'source.http'];
  assert.equal(restoredFileFocus('source.http', 'previous.http', paths), 'source.http');
  assert.equal(restoredFileFocus('', 'previous.http', paths), 'previous.http');
  assert.equal(restoredFileFocus('', 'deleted.http', paths), 'source.http');
  assert.equal(restoredFileFocus('', 'deleted.http', []), '');
});

test('hidden mark describes policy without promising runtime sandbox coverage', () => {
  assert.match(HIDDEN_TITLE, /policy/);
  assert.match(HIDDEN_TITLE, /coverage may vary/);
  const files = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  assert.doesNotMatch(files, /sessions never see this file/);
  assert.match(files, /Manual Files operations remain available/);
});

test('file search routes Database pane results into visible Files, including Command-click', {
  skip: !process.env.PIPER_BROWSER_URL || !process.env.PIPER_BROWSER_SESSION,
}, () => {
  const name = `piper-file-search-${process.pid}`;
  const browser = (...args) => {
    try { return execFileSync('agent-browser', ['--session', name, ...args], { encoding: 'utf8', timeout: 45000, stdio: 'pipe' }).trim(); }
    catch { assert.fail(`agent-browser ${args[0]} failed`); }
  };
  const evaluate = script => browser('eval', script);
  // a boolean: a returned element (a CodeMirror node) is too deep for CDP to serialize - the wait then fails at once
  const wait = expression => browser('wait', '--fn', `!!(${expression})`);
  const file = process.env.PIPER_BROWSER_FILE || 'CLAUDE.md';
  const mac = '/mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent)';
  const commandP = `document.body.dispatchEvent(new KeyboardEvent("keydown", {key:"p",code:"KeyP",metaKey:${mac},altKey:!${mac},bubbles:true,cancelable:true})); true`;
  const sourceMod = `metaKey:${mac},ctrlKey:!${mac}`;
  const pickerFile = `Array.from(document.querySelectorAll('#overlay .pitem')).find(b=>b.querySelector('.name')?.textContent.trim()===${JSON.stringify(file)})`;
  try {
    browser('open', process.env.PIPER_BROWSER_URL);
    evaluate(`localStorage.setItem('piper.last', ${JSON.stringify(JSON.stringify({ active: process.env.PIPER_BROWSER_SESSION, view: 'term' }))}); true`);
    browser('reload');
    wait("Array.from(document.querySelectorAll('nav.tabs button')).some(b=>b.textContent.startsWith('DB pane'))");
    evaluate("Array.from(document.querySelectorAll('nav.tabs button')).find(b=>b.textContent.startsWith('DB pane')).click(); true");
    wait('document.querySelector(".dbpane:not(.hidden) .dbview")');
    evaluate(commandP);
    wait('document.querySelector("#overlay input")');
    assert.equal(evaluate('!!document.querySelector(".filespane:not(.hidden) .files") && !!document.querySelector(".dbpane:not(.hidden) .dbview")'), 'true');
    assert.equal(evaluate('document.querySelector("#overlay input").placeholder.startsWith("file,")'), 'true');
    browser('fill', '#overlay input', file);
    wait(pickerFile);
    evaluate(`${pickerFile}.click(); true`);
    wait('!document.querySelector("#overlay.open") && document.querySelector(".filespane:not(.hidden) .files .fbody :is(.cm-content,.fmd)")');
    assert.equal(evaluate(`document.querySelector('.files .crumbs strong')?.textContent===${JSON.stringify(file)}`), 'true');
    evaluate(commandP);
    browser('fill', '#overlay input', file);
    wait(pickerFile);
    evaluate(`${pickerFile}.dispatchEvent(new MouseEvent('click',{${sourceMod},bubbles:true,cancelable:true})); true`);
    wait(`Array.from(document.querySelectorAll('nav[aria-label="session view"] button[aria-pressed="true"]')).some(b=>b.textContent.trim()==='Files')`);
    assert.equal(evaluate('!!document.querySelector(".files .fbody :is(.cm-content,.fmd)") && !document.querySelector(".body.grid")'), 'true');
    evaluate(commandP);
    browser('fill', '#overlay input', file);
    wait(pickerFile);
    evaluate(`document.querySelector('#overlay input').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',${sourceMod},bubbles:true,cancelable:true})); true`);
    wait('!document.querySelector("#overlay.open") && document.querySelector(".files .fbody :is(.cm-content,.fmd)")');
  } finally { browser('close'); }
});

test('matchSpans: a contiguous hit, else word starts in order, else null', () => {
  assert.deepEqual(matchSpans('Controller', 'ProfileController.java'), [[7, 17]]);
  assert.deepEqual(matchSpans('profcon', 'ProfileController.java'), [[0, 4], [7, 10]]);
  assert.equal(matchSpans('xyz', 'ProfileController.java'), null);
  assert.deepEqual(matchSpans('', 'a'), []);
  // N7-2: a letter whose lower case is longer (İ → i̇) must not shift the paint off the match
  assert.deepEqual(matchSpans('needle', 'İstanbul-needle.md'), [[9, 15]]);
  assert.deepEqual(matchSpans('ndex', 'İndex.md'), [[1, 5]]);
});

test('rankFiles: name over path, main over test, changed / open / recent boosted', () => {
  const files = [
    'core/src/test/java/a/ProfileControllerTest.java',
    'core/src/main/java/a/ProfileController.java',
    'core/src/main/java/a/ProfileErrorControllerAdvice.java',
    'docs/profile/controller.md',
  ];
  const r = rankFiles('ProfileController', files);
  assert.equal(r[0].path, 'core/src/main/java/a/ProfileController.java');
  assert.ok(r.findIndex((x) => x.path.includes('Test')) > 0);
  // the spans point into the whole path (the name part)
  const top = r[0];
  assert.equal(top.path.slice(top.spans[0][0], top.spans[0][1]), 'ProfileController');
  // a changed file climbs
  const b = rankFiles('profcon', files, { changed: new Set(['core/src/main/java/a/ProfileErrorControllerAdvice.java']) });
  assert.equal(b[0].path, 'core/src/main/java/a/ProfileErrorControllerAdvice.java');
  // a slash in the query matches the path
  assert.equal(rankFiles('docs/prof', files)[0].path, 'docs/profile/controller.md');
});

test('N6: rankFiles finds a decomposed (NFD) name by what a keyboard types (NFC) and the other way round', () => {
  const nfd = 'docs/résumé-café.md'.normalize('NFD');   // combining marks, as macOS tools write it
  const files = ['docs/other.md', nfd, 'docs/código.md'.normalize('NFC')];
  const r = rankFiles('café', files);   // U+00E9 typed
  assert.equal(r[0]?.path, nfd, JSON.stringify(r));
  assert.deepEqual(r[0].spans, []);   // offsets of the NFC form would not fit the NFD path: no highlight, never a wrong one
  assert.equal(rankFiles('código'.normalize('NFD'), files)[0]?.path, 'docs/código.md'.normalize('NFC'));   // an NFD query, an NFC name
  assert.equal(rankFiles('docs/rés', files)[0]?.path, nfd);
  // a changed file climbs although git names it in NFC
  const two = ['docs/cafe\u0301-a.md', 'docs/cafe\u0301-b.md'];
  assert.equal(rankFiles('caf\u00e9', two, { changed: new Set(['docs/caf\u00e9-b.md']) })[0].path, two[1]);
});

test('importBlock: the leading import block of Java / TS / Python, ≥ 3 lines', () => {
  const java = ['package a;', '', 'import a.B;', 'import a.C;', '', 'import static a.D.E;', '', 'public class X {}'];
  assert.deepEqual(importBlock(java, 'java'), [3, 6]);
  assert.equal(importBlock(['import a.B;', 'class X {}'], 'java'), null);
  const ts = ["import { a,", "  b } from './x.js';", "import c from 'c';", "import d from 'd';", '', 'export const x = 1;'];
  assert.deepEqual(importBlock(ts, 'ts'), [1, 4]);
  assert.equal(importBlock(['x', 'y'], 'md'), null);
});

test('scopeAt: the class and method a line is in (breadcrumbs, sticky scope)', () => {
  const lines = ['package a;', 'public class Order {', '    private int x;', '    public int get() {', '        return x;', '    }', '}'];
  const items = outline(lines, 'a/Order.java');
  assert.deepEqual(scopeAt(items, 5).map((o) => o.name), ['Order', 'get']);
  assert.deepEqual(scopeAt(items, 3).map((o) => o.name), ['Order']);
  assert.deepEqual(scopeAt(items, 1), []);
});

test('tree helpers: left cut, file kinds (noise comes from the server: DirEntry.noise, K1)', () => {
  assert.equal(leftCut('a/b/c/d/eeeee', 9), '…/d/eeeee');
  assert.equal(leftCut('short', 9), 'short');
  assert.equal(fileKind('A.java'), 'code');
  assert.equal(fileKind('application.yaml'), 'conf');
  assert.equal(fileKind('pom.xml'), 'data');
  assert.equal(fileKind('README.md'), 'doc');
  assert.equal(fileKind('api.http'), 'http');
});

test('changedLines: the lines a session edit touched (1-based, in the new text)', () => {
  assert.deepEqual(changedLines(['a', 'b', 'c'], ['a', 'B', 'c']), [2]);
  assert.deepEqual(changedLines(['a', 'c'], ['a', 'b1', 'b2', 'c']), [2, 3]);
  assert.deepEqual(changedLines(['a', 'b', 'c'], ['a', 'c']), [2]);   // a deletion marks the line after it
  assert.deepEqual(changedLines(['a'], ['a']), []);
});

test('blameText: author · age · summary; uncommitted lines say so', () => {
  const now = 1_000_000;
  assert.equal(blameText({ line: 1, commit: 'abc', author: 'x', time: now - 3 * 86400, summary: 'fix', text: '' }, now), 'x · 3 d ago · fix');
  assert.equal(blameText({ line: 1, commit: '', author: '', time: 0, summary: '', text: '' }, now), 'not committed yet');
  assert.equal(blameText(undefined, now), null);
});

test('groupHits: Main before Test, by file', () => {
  const g = groupHits([
    { path: 'src/test/java/ATest.java', line: 1, text: '' },
    { path: 'src/main/java/A.java', line: 3, text: '' },
    { path: 'src/main/java/A.java', line: 9, text: '' },
  ]);
  assert.deepEqual(g.map((x) => x.group), ['Main', 'Test']);
  assert.equal(g[0].files[0].hits.length, 2);
});

test('guessIndent: tabs, 2 or 4 spaces', () => {
  assert.equal(guessIndent('a\n\tb\n\tc'), '\t');
  assert.equal(guessIndent('a\n  b\n    c\n  d'), '  ');
  assert.equal(guessIndent('a\n    b\n        c'), '    ');
});

test('fileRefs: path:line references of the terminal / transcript (K4), URLs are not files', () => {
  const t = 'see src/main/java/Foo.java:123 and (./a/b.ts:4:2), not https://x.example/a.js:5';
  const r = fileRefs(t);
  assert.deepEqual(r.map((x) => [x.path, x.line]), [['src/main/java/Foo.java', 123], ['./a/b.ts', 4]]);
  assert.equal(t.slice(r[0].start, r[0].end), 'src/main/java/Foo.java:123');
  assert.equal(t.slice(r[1].start, r[1].end), './a/b.ts:4');
  assert.deepEqual(fileRefs('/abs/x/Y.kt:9'), [{ start: 0, end: 13, path: '/abs/x/Y.kt', line: 9 }]);
  assert.deepEqual(fileRefs('no reference here: 12'), []);
  // the shared global regex keeps no state between calls
  assert.equal(fileRefs(t).length, 2);
});

test('dirsOf: every ancestor folder of the index plus listed (empty) folders, root first (C4)', () => {
  assert.deepEqual(dirsOf(['a/b/c/X.java', 'README.md', 'a/Y.md'], ['empty', 'z/q']),
    ['.', 'a', 'a/b', 'a/b/c', 'empty', 'z', 'z/q']);
  assert.deepEqual(dirsOf([]), ['.']);
});

import { decodeView } from '../../piper/web/dist/files.js';
import { historyRel } from '../../piper/web/dist/history.js';

test('decodeView: the persisted Files view survives junk, keeps only paths and known values (K14)', () => {
  assert.equal(decodeView(null), null);
  assert.equal(decodeView('{not json'), null);
  assert.equal(decodeView('42'), null);
  const v = decodeView(JSON.stringify({ tabs: ['a.ts', 3, '', 'b.ts'], active: 'b.ts', expanded: ['src'], scope: 'changed',
    panel: { open: true, tab: 'search', height: 99999 }, recent: ['a.ts'] }));
  assert.deepEqual(v, { tabs: ['a.ts', 'b.ts'], active: 'b.ts', expanded: ['src'], scope: 'changed',
    panel: { open: true, tab: 'search', height: 2000 }, recent: ['a.ts'] });
  const d = decodeView(JSON.stringify({ scope: 'nope', panel: { tab: 'x' } }));
  assert.equal(d.scope, 'all');
  assert.equal(decodeView(JSON.stringify({scope:'turn'})).scope, 'all', 'removed Last turn scope restores to All');
  assert.deepEqual(d.panel, { open: false, tab: 'usages', height: 240 });
  assert.deepEqual(d.tabs, []);
});

test('historyRel: a commit diff path maps into a project inside the repository (K3)', () => {
  // the file's own git path carries the prefix of the project inside the repository
  assert.equal(historyRel('web/src/a.ts', 'src/a.ts', 'web/src/a.ts'), 'src/a.ts');
  assert.equal(historyRel('web/src/b.ts', 'src/a.ts', 'web/src/a.ts'), 'src/b.ts');
  assert.equal(historyRel('other/c.ts', 'src/a.ts', 'web/src/a.ts'), '');   // outside the project
  // the project is the repository root, or the repository history: as is
  assert.equal(historyRel('src/b.ts', 'src/a.ts', 'src/a.ts'), 'src/b.ts');
  assert.equal(historyRel('x/y.ts', '', ''), 'x/y.ts');
});

test('diffMetadata: changes without hunks cannot look like an unchanged text file', () => {
  const text = { state: 'text', text: 'same\n', final_newline: true, mode: '100644' };
  assert.deepEqual(diffMetadata('old mode 100644\nnew mode 100755\n', text), ['old mode 100644', 'new mode 100755']);
  assert.deepEqual(diffMetadata('Binary files a/image.png and b/image.png differ\n', text), ['Binary files a/image.png and b/image.png differ']);
  assert.deepEqual(diffMetadata('', { state: 'binary' }), ['HEAD: binary file']);
  assert.deepEqual(diffMetadata('', { state: 'too_large' }), ['HEAD: too large to review']);
  assert.deepEqual(diffMetadata('', { state: 'missing' }), ['New file (absent from HEAD)']);
  assert.deepEqual(diffMetadata('', { ...text, mode: '120000' }), ['HEAD: symlink (target text)']);
  assert.deepEqual(diffMetadata('\\ No newline at end of file\n', { ...text, text: 'same', final_newline: false }),
    ['\\ No newline at end of file', 'HEAD: no final newline']);
  assert.deepEqual(diffMetadata('', text), []);
});

test('N9: IntelliJ info with an empty answer toasts instead of a modal holding the single word "nothing"', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  assert.match(src, /if \(!r\.info\?\.trim\(\)\) return deps\.toast\(`IntelliJ has no information on \$\{c\.word\}`\);/);
  assert.doesNotMatch(src, /r\.info \|\| 'nothing'/);
});

test('N10-B-2: after a delete / rename / create / move in the tree the keyboard stays in the tree, not on <body>', async () => {
  const { rowAfter } = await import('../../piper/web/dist/files.js');
  const rows = ['a', 'a/x.txt', 'a/y.txt', 'b', 'c.txt', 'd.txt'];
  assert.equal(rowAfter(rows, 'c.txt'), 'd.txt');                 // the row now in its place
  assert.equal(rowAfter(rows, 'd.txt'), 'c.txt');                 // the last row: the one before
  assert.equal(rowAfter(rows, 'a'), 'b');                         // a folder goes with its open rows
  assert.equal(rowAfter(rows, 'a/y.txt'), 'b');
  assert.equal(rowAfter(['only'], 'only'), null);
  assert.equal(rowAfter(rows, 'missing'), null);
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  const fn = (name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n  }\n', src.indexOf(`function ${name}(`)));
  // Enter (rename / new folder, or an empty name) puts the keyboard on the row; a new file opens in the editor
  assert.match(fn('commitEdit'), /if \(!typing\) focusRow\(target, target \? parentOf\(target\) : null, ed\.from \?\? ed\.at \?\? ed\.dir\);/);
  assert.match(fn('commitEdit'), /if \(!name\) \{ v\.edit = null; drawLeft\(\); return focusRow\(ed\.from \?\? ed\.at \?\? ed\.dir\); \}/);
  // delete: the row in its place, captured before the tree is redrawn
  assert.match(fn('del'), /const rows = \[\.\.\.leftEl\.querySelectorAll<HTMLElement>\('\.tree \[data-nav\]'\)\]/);
  assert.match(fn('del'), /focusRow\(rowAfter\(rows, path\), parentOf\(path\)\)/);
  // Move… (F6): the moved row
  assert.match(fn('moveTo'), /drawLeft\(\); focusRow\(to, d, parentOf\(path\)\);/);
});

test('N10-B-1 / B-4 / B-5: long file names end in an ellipsis (tab, search hit header), runs of spaces in a name stay visible', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // B-1: the tab button is a flex box - text-overflow does not reach a bare text node (cut at 217 px, the git dot pushed out)
  assert.match(src, /h\('button', \{ class: 'fname', onclick: \(\) => activate\(t\.path\) \}, icon\(t\.path\.split\('\/'\)\.pop\(\) \?\? '', false\), h\('span', \{ class: 'tl' \}, showBidi\(labels\[i\]!\)\),/);
  assert.match(css, /\.ftab \.fname \.tl \{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; \}/);
  assert.match(css, /\.ftab \.fname \{ display: flex; align-items: center; gap: 6px; min-width: 0;/);
  // B-4: a search hit's file header - the name ends in …, the folder gives way first, the count stays
  assert.match(src, /h\('div', \{ class: 'hfile', title: showBidi\(f\.path\) \}, icon\(f\.path\.slice\(slash \+ 1\), false\), h\('span', \{ class: 'hn' \}, showBidi\(f\.path\.slice\(slash \+ 1\)\)\),/);
  assert.match(src, /h\('span', \{ class: 'mut mono hd' \}, ' ' \+ showBidi\(leftCut\(f\.path\.slice\(0, Math\.max\(0, slash\)\), 50\)\)\), h\('span', \{ class: 'mut hc' \}, ` \$\{f\.hits\.length\}`\)\)\);/);
  assert.match(css, /\.hfile \.hn \{ flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; \}/);
  assert.match(css, /\.hfile \.hd \{ flex: none; min-width: 0; max-width: 40%; overflow: hidden; text-overflow: ellipsis; \}/);   // flex 0 100: "docs" became a 0-5 px sliver
  assert.match(css, /\.hfile \.hc, \.hfile \.fi \{ flex: none; \}/);
  // B-5: "name with  spaces .txt" showed as "name with spaces .txt" (two files could look the same)
  assert.match(css, /\.tnode \.nm \{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: pre; \}/);
  assert.match(css, /\.fhead \.crumbs strong \{ font-weight: 600; white-space: pre; /);
});

test('N10-B-3: Files beside the context panel (1024 px: editor 340 px) - the tree yields, the header wraps instead of hiding the path, the status bar fits', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.files \{ grid-template-columns: min\(var\(--tree-w, 264px\), 36%\) minmax\(0, 1fr\);/);
  assert.match(css, /\.fright \{ border-radius: 0; background: var\(--page\); container: fright \/ inline-size; \}/);
  const q = css.slice(css.indexOf('@container fright (max-width: 520px)'));
  assert.ok(css.includes('@container fright (max-width: 520px)'), 'a narrow editor has its own rules');
  // (.fright-qualified: the later one-line header rules .fhead { flex-wrap: nowrap } would win over a bare .fhead)
  assert.match(q.slice(0, q.indexOf('\n}')), /\.fright \.fhead \{ flex-wrap: wrap;[^}]*\}[\s\S]*\.fright \.fhead \.crumbs \{ flex-basis: 100%;[\s\S]*\.fright \.fstatus \.br, \.fright \.fstatus \.legend \{ display: none; \}/);
  assert.match(src, /if \(br\) bits\.push\(h\('span', \{ class: 'br', title: 'branch' \}, fi\('branch', 'gi'\), showBidi\(br\)\)\);/);
});

test('N10-B-8: Files beside the agent drawer at 1024 px (editor 325 px, tree 166 px) - no action and no scope cut off', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const q = css.slice(css.indexOf('@container fright (max-width: 520px)'));
  const block = q.slice(0, q.indexOf('\n}'));
  // the side pane's rule: the icon actions that are only extras go; what is left wraps instead of hiding "more"
  assert.match(block, /\.fright \.fhead \.ib\.wide \{ display: none; \}/);
  assert.match(block, /\.fright \.fhead \.actions \{ margin-left: auto; flex: 0 1 auto; min-width: 0; flex-wrap: wrap; justify-content: flex-end; \}/);
  // "Changed 14" / "Open 5" were cut in a narrow tree: the scope wraps
  assert.match(css, /\.scope \{ display: flex; flex-wrap: wrap; gap: 2px;/);
});

test('G-N11-2 / G-N11-3: the tree\'s project chip and its scope buttons end in an ellipsis in a narrow tree (768 px + the context panel cut "grp/demo" bare, 390 px "Changed 10")', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // the chip is a flex box: text-overflow does not reach its bare text node (as N10-B-1's tab label)
  assert.match(src, /'aria-label': project \? `Project \$\{rawProjectKey\(project\)\} – switch project`/);
  assert.match(src, /name: rawProjectKey\(x\.p\.key\)/);
  assert.match(src, /refHost\(x\.key\) === refHost\(project\)/);
  assert.match(css, /@container fleft \(max-width: 111px\)/);
  assert.match(css, /clip-path: inset\(50%\)/);
  assert.match(css, /\.ftop \.pchip \.pl \{ min-width: 0; overflow: hidden; text-overflow: ellipsis; \}/);
  assert.match(css, /\.ftop \.pchip \.fi, \.ftop \.pchip \.mut \{ flex: none; \}/);
  assert.match(css, /\.scope button \{[^}]*overflow: hidden; text-overflow: ellipsis; \}/);
});

test('G-N11-9: a new file (⌥N, a name, Enter) opens with the caret in the editor - the keyboard fell to <body> (open() focuses only with a line)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  const fn = (name) => src.slice(src.indexOf(`function ${name}(`), src.indexOf('\n  }\n', src.indexOf(`function ${name}(`)));
  assert.match(fn('commitEdit'), /if \(ed\.kind === 'file'\) \{ typing = true; void open\(path, 1\); \}/);
  assert.match(src, /if \(line\) \{ t\.ed!\.reveal\(line\); t\.ed!\.focus\(\); \}/);   // what line 1 buys
});

test('G-N11-10: ⌥N then Esc / an empty Enter puts the keyboard back on the row it started from (it went to the first row of the tree)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  assert.match(src, /edit: \{ kind: 'file' \| 'dir' \| 'rename'; dir: string; from\?: string; at\?: string \| undefined; value: string \} \| null;/);
  assert.match(src, /return startCreate\(isDir \? path : parentOf\(path\), e\.shiftKey \? 'dir' : 'file', path\);/);
  assert.match(src, /function startCreate\(dir: string, kind: 'file' \| 'dir', at\?: string\): void \{[\s\S]*?v\.edit = \{ kind, dir, at, value: '' \};/);
  assert.equal(src.match(/ed\.from \?\? ed\.at \?\? ed\.dir/g)?.length, 3);   // Esc, an empty name, the fallback after Enter
  assert.doesNotMatch(src, /ed\.from \?\? ed\.dir\)/);
});

test('9.41 autosave: a quiet edit saves itself, never over a change on disk or a failed save', () => {
  const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
  const files = src('files.ts');
  const cm = src('cmeditor.ts');
  const main = src('main.ts');
  assert.equal(AUTOSAVE_MS, 2000);
  // every text change reaches Files (not only the dirty flip), and restarts the quiet timer
  assert.match(cm, /h\.onDirty\(d\); \}\n\s*h\.onEdit\?\.\(\);/);
  assert.match(files, /onEdit: \(\) => schedule\(t\),/);
  assert.match(files, /function schedule\(t: Tab\): void \{\n\s*unschedule\(t\);\n\s*if \(!autoOk\(t\)\) return;/);
  // the guard: writable code tab, no change on disk, no conflict / error banner
  const ok = files.match(/const autoOk = \(t: Tab\): boolean => ([^;]+);/)[1];
  for (const part of ['t.dirty', 'writable(t)', "t.mode === 'code'", '!t.remote', "t.banner?.kind !== 'conflict'", "t.banner?.kind !== 'error'"])
    assert.ok(ok.includes(part), part);
  // the timer saves only the tab still shown in the same project, quietly (no toast per pause)
  assert.match(files, /if \(key === project && cur\(\) === t && autoOk\(t\)\) void save\(false, false, t, true\);/);
  assert.match(files, /if \(!auto\) deps\.toast\(`Saved \$\{t\.path\}`\);/);
  // the same write as ⌘S: save() cancels a pending timer, the sha lock path is unchanged
  assert.match(files, /async function save\(force = false, sure = false, t = cur\(\), auto = false\): Promise<void> \{\n\s*if \(t\) unschedule\(t\);/);
  // leaving the tab, the project, the Files view or the page saves at once; a closed tab drops its timer
  assert.match(files, /if \(v\.active === path\) return;\n\s*flush\(\);/);
  assert.match(files, /if \(v\.active !== path\) flush\(\);   \/\/ 9\.41/);   // open() of a new tab too
  // a failed save redraws the status: no promise of an autosave under a conflict banner
  assert.match(files, /if \(key === project\) \{ drawBanners\(\); drawStatus\(\); \}/);
  assert.match(files, /if \(key !== project\) \{\n\s*flush\(\);/);
  assert.match(files, /function forget\(t: Tab\): void \{\n\s*const v = view\(\);\n\s*unschedule\(t\);/);
  assert.match(main, /if \(view === 'files' && v !== 'files'\) files\.flush\(\);/);
  assert.match(main, /visibilitychange', \(\) => \{ if \(document\.hidden\) files\.flush\(\); \}/);
  // the status bar says what happens
  assert.ok(files.includes('`● unsaved · saves in ${AUTOSAVE_MS / 1000} s · ⌘S now`'));
  assert.ok(files.includes("'✎ editable · saved automatically'"));
});

// ---- F06: scope pills, the empty editor's hub, a quieter tree
test('F06 scopePills: All · Changed N · Last turn · Open, a count only when > 0 (no "Turn 35 0")', () => {
  assert.deepEqual(scopePills(69, 0, 0), [['all', 'All', null], ['changed', 'Changed', 69], ['open', 'Open', null], ['scratch', 'Scratches', null]]);
  assert.deepEqual(scopePills(0, 3, 2, 4).map(([, l, n]) => [l, n]), [['All', null], ['Changed', null], ['Open', 2], ['Scratches', 4]]);
  assert.equal(scopePills(5, null, 1)[2][2], 1, 'an unknown turn has no count');
  for (const [, label] of scopePills(1, 35, 1)) assert.doesNotMatch(label, /\d/, 'the turn number never sits in a label');
});

test('F06 emptyHub: at most 8 changed files and how many more, at most 5 recent', () => {
  const ch = Array.from({ length: 11 }, (_, i) => `src/F${i}.java`);
  const r = emptyHub(ch, ['a', 'b', 'c', 'd', 'e', 'f']);
  assert.deepEqual(r.changed, ch.slice(0, 8));
  assert.equal(r.more, 3);
  assert.deepEqual(r.recent, ['a', 'b', 'c', 'd', 'e']);
  assert.deepEqual(emptyHub([], []), { changed: [], more: 0, recent: [] });
});

test('F06 wiring: hub in the empty editor, no key strip under the tree, short filter, grey folder count', () => {
  const src = readFileSync(new URL('../../piper/web/src/files.ts', import.meta.url), 'utf8');
  assert.match(src, /'Changed in this session'/);
  assert.match(src, /'Recently opened'/);
  assert.match(src, /'Go to file', h\('kbd', \{\}, kbd\('⌘P'\)\)/);
  assert.match(src, /'Search', h\('kbd', \{\}, kbd\('⌘⇧F'\)\)/);
  assert.doesNotMatch(src, /class: 'tkeys/, 'the key strip under the tree is gone (keys are in the hub and ⌘K)');
  assert.match(src, /placeholder: 'Filter',/);
  assert.match(src, /h\('span', \{ class: 'cnt', title: `\$\{n\} changed` \}, String\(n\)\)/, 'a folder count without the dot');
  assert.match(src, /refreshMarks[\s\S]{0,400}fe-hub/, 'the hub redraws when git status arrives');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const blk = css.slice(css.indexOf('/* ==== 9.65 F06'), css.indexOf('/* ==== end F06 ==== */'));
  assert.ok(blk.length > 100, 'one bounded F06 block');
  assert.match(blk, /\.fleft \.tree \{ font-size: var\(--t-sm\); \}/, 'tree text 12 px');
  assert.match(blk, /\.fleft \.tfilter \{ min-width: 0;/, 'the filter shrinks in a narrow pane');
  assert.doesNotMatch(blk, /font-size: \d|border-radius: [1-9]/, 'scale tokens only');
});


test('session edits refresh new files, invalidate collapsed directories and the search index', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/files.js', import.meta.url), 'utf8');
  const start = source.indexOf('function sessionEdited(');
  const fn = source.slice(start, source.indexOf('\n    }', start) + 6);
  const v = { fsSeq: new Map(), dirs: new Map([['.', {}], ['src', {}], ['closed', {}]]),
    expanded: new Set(['.', 'src']), index: { files: ['old.txt'] }, tabs: [], scope: 'all' };
  const views = new Map([['demo', v]]);
  const loaded = [];
  const edit = new Function('views', 'view0', 'project', 'loadDir', 'reread', 'deps', 'drawLeft',
    `${fn}; return sessionEdited;`)(views, () => {}, 'demo', p => loaded.push(p), () => {}, {}, () => {});
  edit('demo', 's1', 1, []);
  assert.equal(v.dirs.size, 3, 'first sight establishes the sequence');
  edit('demo', 's1', 2, ['new.txt']);
  assert.deepEqual(loaded, ['.', 'src']);
  assert.equal(v.dirs.size, 0, 'collapsed caches must reload when expanded');
  assert.equal(v.index, null, 'quick-open must include the new file immediately');
  edit('demo', 's1', 2, ['new.txt']);
  assert.equal(loaded.length, 2, 'unchanged sequence does not fetch again');
  edit('demo', 's1', 3, [], 3);
  assert.equal(loaded.length, 4, 'unknown Codex paths also refresh the tree');
  const other = { ...v, fsSeq: new Map([['s2', 1]]), dirs: new Map([['.', {}]]), index: { files: [] } };
  views.set('other', other);
  edit('other', 's2', 2, ['new.txt']);
  assert.equal(other.dirs.size, 0, 'inactive project must not retain stale listings');
  assert.equal(other.index, null);
  assert.equal(loaded.length, 4, 'inactive project loads only when shown');
});
