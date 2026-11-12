// node --test: Notes (module notes, phase 3) - the desktop view, the quick note and Settings › Notes.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { ACCESS_TEXT, autoMode, entryPreview, filterNotes, isQuickNoteKey, looksJson, noteFileName, noteKind, relTime, sizeText, whereText } from '../../piper/web/dist/notes.js';
import { keyGate, SHORTCUTS } from '../../piper/web/dist/shortcuts.js';
import { NAV_TOOLS } from '../../piper/web/dist/navigation.js';

const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
const main = src('main.ts');
const files = src('files.ts');
const quick = src('quicknote.ts');
const settings = src('settings.ts');
const now = new Date(2026, 10, 12, 14, 32, 10);   // Thu 12 Nov 2026 14:32
const at = (d) => d.getTime() / 1000;
const key = (o) => ({ key: 'j', code: 'KeyJ', shiftKey: false, metaKey: false, altKey: false, ctrlKey: false, ...o });

test('a JSON object or array is a file, a sentence a line, more than 8 lines a file', () => {
  assert.equal(looksJson('{"order": 4812}'), true);
  assert.equal(looksJson('  [1, 2]\n'), true);
  assert.equal(looksJson('"just a string"'), false, 'a JSON scalar is a sentence');
  assert.equal(looksJson('{not json'), false);
  assert.equal(looksJson('42'), false);
  assert.deepEqual(autoMode('{"a":1}'), { mode: 'file', why: 'json' });
  assert.deepEqual(autoMode('call the client back'), { mode: 'line', why: '' });
  assert.deepEqual(autoMode('a\nb\nc\nd\ne\nf\ng\nh'), { mode: 'line', why: '' }, '8 lines are still a line');
  assert.deepEqual(autoMode('a\nb\nc\nd\ne\nf\ng\nh\ni'), { mode: 'file', why: 'long' });
});

test('the file name mirrors the backend: sanitized, own extension kept, json / md, a dated default', () => {
  assert.equal(noteFileName('order-export-4812', '{"a":1}', now), 'order-export-4812.json');
  assert.equal(noteFileName('', 'plain text', now), 'note-2026-11-12-1432.md');
  assert.equal(noteFileName('', '[1]', now), 'note-2026-11-12-1432.json');
  assert.equal(noteFileName('report.csv', 'a,b', now), 'report.csv', 'a typed extension wins');
  assert.equal(noteFileName('../etc/passwd', 'x', now), '-etc-passwd.md', 'no folder, no leading dots');
  assert.equal(noteFileName('.hidden', 'x', now), 'hidden.md');
  assert.equal(noteFileName('a   b. ', 'x', now), 'a b.md');
});

test('the quick line preview follows the stamp setting (notes.py _entry)', () => {
  assert.deepEqual(entryPreview('time', now), { heading: '## 2026-11-12', prefix: '14:32' });
  assert.deepEqual(entryPreview('datetime', now), { heading: '', prefix: '2026-11-12 14:32' });
  assert.deepEqual(entryPreview('none', now), { heading: '', prefix: '' });
});

test('the list: the main note first, then newest first; its time column', () => {
  const rows = [{ name: 'a.md', size: 1, mtime: 10, main: false }, { name: 'notes.md', size: 1, mtime: 1, main: true }, { name: 'b.json', size: 1, mtime: 20, main: false }];
  assert.deepEqual(filterNotes(rows, '').map((r) => r.name), ['notes.md', 'b.json', 'a.md']);
  assert.deepEqual(filterNotes(rows, 'JSON').map((r) => r.name), ['b.json']);
  assert.equal(relTime(at(now) - 20, now), 'now');
  assert.equal(relTime(at(new Date(2026, 10, 12, 10, 42)), now), '10:42');
  assert.equal(relTime(at(new Date(2026, 10, 11, 23, 0)), now), 'Yesterday');
  assert.equal(relTime(at(new Date(2026, 10, 10, 9, 0)), now), 'Tue');
  assert.equal(relTime(at(new Date(2026, 10, 3, 9, 0)), now), '3 Nov');
  assert.equal(relTime(at(new Date(2025, 9, 28, 9, 0)), now), '28 Oct 2025');
  assert.equal(sizeText(612), '612 B');
  assert.equal(sizeText(2150), '2.1 KiB');
});

test('⌘⇧J (Alt+Shift+J off a Mac) is the quick note; ⌘J, ⌃⌘⇧J and other layouts by position', () => {
  assert.equal(isQuickNoteKey(key({ key: 'J', metaKey: true, shiftKey: true }), true), true);
  assert.equal(isQuickNoteKey(key({ key: 'j', metaKey: true }), true), false, '⌘J stays the next waiting item');
  assert.equal(isQuickNoteKey(key({ key: 'J', metaKey: true, shiftKey: true, ctrlKey: true }), true), false);
  assert.equal(isQuickNoteKey(key({ key: 'Й', metaKey: true, shiftKey: true }), true), true, 'a Cyrillic layout: the J key by its position');
  assert.equal(isQuickNoteKey(key({ key: 'K', code: 'KeyJ', metaKey: true, shiftKey: true }), true), false, 'Dvorak: the letter, not the position');
  assert.equal(isQuickNoteKey(key({ key: 'J', altKey: true, shiftKey: true }), false), true);
  assert.equal(isQuickNoteKey(key({ key: 'J', ctrlKey: true, shiftKey: true }), false), false, 'Ctrl+Shift+J stays the browser\'s');
});

test('module off: no key, no rail button, no palette rows, no view; the key falls to nobody else', () => {
  assert.ok(SHORTCUTS[0].rows.some((r) => r.k === '⌘⇧J'), 'the Navigation group lists it (keyOn reads only that group)');
  assert.equal(keyGate('⌘⇧J'), 'notes');
  assert.ok(NAV_TOOLS.includes('notes'));
  assert.match(main, /notes: \['notes'\] \}/, 'VIEW_MODULE gates the view');
  assert.match(main, /on\('notes'\) && \{ id: 'notes'/, 'the rail button (and the phone More row) needs the module');
  assert.match(main, /\} else if \(isQuickNoteKey\(e, MAC\)\) \{[^\n]*\n\s+if \(keyOn\('⌘⇧J'\)\) \{ quickNote\(\); e\.preventDefault\(\); \}\n\s+\} else if/,
    'with the module off the branch neither prevents the default nor reaches ⌘J');
  assert.ok(main.indexOf('isQuickNoteKey(e, MAC)') < main.indexOf("} else if (e.key === 'j') {"), 'checked before ⌘J');
  for (const label of ['Notes', 'Notes: quick note', 'Notes: new file', 'Notes: paste as file', 'Notes: reveal folder'])
    assert.match(main, new RegExp(`label: '${label}'[^\\n]*module: 'notes'`), label);
  assert.match(main, /function quickNote\([^)]*\): void \{\n\s+if \(!on\('notes'\)\) return;/);
});

test('the code editor and the terminal let ⌘⇧J through', () => {
  assert.match(main, /const CODE_PASS = new Set\(\[[^\]]*'⇧j'/);
  assert.match(src('term.ts'), /APP_KEYS[^\n]*'j'/, 'the terminal hands ⌘J / ⌘⇧J to the app');
});

test('the hosted editor: no project tree, no External banner, its own chip and capture bar', () => {
  assert.match(files, /if \(xo\) el\.classList\.add\('ext-only'\)/);
  assert.match(files, /function drawLeft\(\): void \{\n\s+if \(xo\) return;/);
  assert.match(files, /\.\.\.\(xo \? \[\] : \[null, \['Remove from External'/);
  assert.match(files, /reloadClean\(id: string\): Promise<boolean>/);
  assert.match(src('notes.ts'), /chip: 'AGENTS ONLY BY CARD'/);
  assert.match(main, /if \(document\.hidden\) notes\.flush\(\);/, 'a hidden window saves a pending Notes edit');
  assert.match(main, /files\.dirty\(\) \|\| notes\.dirty\(\)/, 'unsaved Notes edits ask before the page unloads');
  assert.match(readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8'), /\.files\.ext-only \.ftab \.fext-glyph \{ display: none; \}/);
});

test('quick note: Esc keeps the draft (no discard warning), ⌘⏎ opens Notes, a pending autosave goes first', () => {
  assert.match(quick, /const quiet = \(e: Event\) => \{ e\.stopPropagation\(\); keep\(\); \};/, 'typing never marks the overlay dirty');
  assert.match(quick, /localStorage\.setItem\(DRAFT/);
  assert.match(quick, /if \(open\) deps\.openNotes\(r\.name\)/);
  assert.match(quick, /await deps\.beforeWrite\(\);\n\s+const r = mode === 'file'/);
  assert.match(quick, /\{ label: 'Copy path'/);
  assert.match(main, /const acts = Array\.isArray\(action\)/, 'a toast takes several actions (Reveal in Finder · Copy path)');
});

test('Settings › Notes: Choose…, the folder check, the macOS refusal with its way out', () => {
  assert.match(settings, /notes: \[\['', \['dir'\]\], \['Quick note', \['main', 'stamp'\]\]\]/);
  assert.match(settings, /'Choose…'/);
  assert.match(settings, /notesApi\.check\(dir\)/);
  assert.match(settings, /deniedBox\(r, /);
  const n = src('notes.ts');
  for (const s of ['Open Privacy settings', 'Try again', 'Use ${s.fallback']) assert.ok(n.includes(s), s);
  assert.match(n, /PRIVACY_URL = 'x-apple\.systempreferences:com\.apple\.preference\.security\?Privacy_FilesAndFolders'/);
});

test('phone (board 07): More › Notes, its own bar, the capture bar, a note as its own screen, no Reveal', () => {
  const n = src('notes.ts');
  assert.doesNotMatch(main, /!phoneShell\(\)[^\n]*id: 'notes'/, 'More lists Notes on the phone');
  assert.doesNotMatch(main, /if \(!phoneShell\(\)\) \{\s+\/\/ Notes/, 'the palette rows are on the phone too');
  assert.match(main, /\(view === 'notes' && phoneShell\(\)\) \? h\('header', \{ class: 'head hidden' \}\)/, 'Notes draws its own phone bar');
  assert.match(main, /back: \(\) => \{ dispatch\(\{ t: 'overview', on: true \}\); openMoreSheet\(\); \}/, '‹ goes back to More');
  assert.match(main, /const notes = createNotes\(\{[\s\S]*?phone: \(\) => phoneShell\(\),[\s\S]*?menuSheet: \(items\) => phoneShell\(\) && \(menuSheet\(items\), true\),[\s\S]*?\}\);/);
  assert.match(n, /if \(phone\(\) && phoneFile !== name\) \{ phoneFile = name; draw\(\); \}/);
  assert.match(n, /'aria-label': 'Append the line'/);
  assert.match(n, /'aria-label': 'Paste as file'/);
  assert.match(n, /!phone\(\) && !document\.activeElement/, 'the keyboard does not pop up over the phone list');
  assert.match(files, /xo && !isDir && x\.rel \? \['Save as…'/, 'a note can be downloaded from its ⋯ (the phone has no Reveal)');
  const mobile = readFileSync(new URL('../../piper/web/mobile.css', import.meta.url), 'utf8');
  assert.match(mobile, /\.nt-phone \.nt-ph-field \.nt-cap-in \{[^}]*font-size: 16px/, 'a 16 px field: iOS does not zoom in');
  assert.match(mobile, /\.nt-phone \.nt-ph-send \{[^}]*width: 44px; height: 44px/);
  assert.match(mobile, /\.nt-phone \.nt-ph-row \{[^}]*min-height: 44px/);
});

test('the phone list: kind and size under a file, where the folder is', () => {
  assert.deepEqual(noteKind('order-export-4812.json'), { label: 'JSON', icon: 'conf' });
  assert.deepEqual(noteKind('Standup.MD'), { label: 'Markdown', icon: 'doc' });
  assert.deepEqual(noteKind('orders.http'), { label: 'HTTP', icon: 'http' });
  assert.deepEqual(noteKind('dump.tar'), { label: 'TAR', icon: 'file' });
  assert.deepEqual(noteKind('README'), { label: 'File', icon: 'file' });
  assert.deepEqual(noteKind('.env'), { label: 'File', icon: 'file' }, 'a dot file has no extension');
  assert.equal(whereText({ default: '~/Documents/Piper Notes' }), 'on your Mac');
  assert.equal(whereText({ default: '~/piper-notes' }), 'on your computer');
});

test('the access wording (boards 02 / 06 / 08): a session sees notes only after a card', () => {
  assert.equal(ACCESS_TEXT, 'A session sees them only after you allow it on a card');
  const n = src('notes.ts');
  assert.match(n, /h\('span', \{ class: 'mut small' \}, ACCESS_TEXT\)/, 'the first run says it');
  for (const f of ['notes.ts', 'quicknote.ts', 'settings.ts', 'main.ts', 'decision.ts']) assert.doesNotMatch(src(f), /[Nn]ever shared/, f);
  assert.match(n, /Everything is a plain file in one folder you can find/);
  assert.match(n, /'Paste becomes a file'/);
  assert.match(n, /'Easy to find again'/);
});

test('quick note (board 02): the notebook icon and the Line / New file switch on the title row', () => {
  assert.match(quick, /title\.classList\.add\('qn-title'\); title\.prepend\(fi\('notes'\)\); title\.append\(h\('span', \{ class: 'grow' \}\), segs\);/);
  assert.doesNotMatch(quick, /const box = h\('div', \{ class: 'qn[^\n]*segs/, 'the switch is not in the body');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.modal\.qn-modal > h2\.qn-title \{ display: flex;/);
  assert.match(css, /^\.fi-notes \{ -webkit-mask-image:/m, 'the rail’s notebook-pen as a file icon');
});

test('the Notes grant row (board 08): the notebook icon before "Notes · read and write", never cut off', () => {
  assert.match(main, /if \(kind === 'notes_access'\) row\.firstElementChild\?\.prepend\(fi\('notes', 'prop-ic'\)\);/);
  assert.match(src('decision.ts'), /GRANT_LABEL: Record<string, string> = \{ notes_access: 'Notes · read and write' \}/);
  assert.match(readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8'), /\.prop-l:has\(> \.prop-ic\) \{ white-space: normal; \}/);
});
