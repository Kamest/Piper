// node --test: pure helpers of the GitLab inbox GUI, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  diffInbox, eventText, filterCounts, forgeCounts, isAssigned, matchesForge, isoAgoText, flowSteps, groupItems, groupOf, matchesQuery, primaryAction,
  projectLabel, rowReasons, sessionFlow, snapshot, sortedReasons, stampRules, targetNote, visibleItems,
} from '../../piper/web/dist/inbox.js';
import { COMMENT_MAX, DETAILS_TEMPLATE, fixComment, gitlabHref, lintComment, splitDetails } from '../../piper/web/dist/comment.js';
import { newNotesLabel, plural, taskFor } from '../../piper/web/dist/text.js';
import { readFileSync } from 'node:fs';

let n = 0;
function item(over = {}) {
  n += 1;
  return {
    key: `grp/acme.shop2024#issue#${n}`, gl: 'grp/acme.shop2024', type: 'issue', iid: n, title: `Issue ${n}`,
    state: 'opened', web_url: `https://gitlab.com/grp/acme.shop2024/-/issues/${n}`, labels: [], author: 'carol',
    updated_at: '2026-09-24T10:00:00Z', due_date: null, milestone: null, reasons: ['assigned'], todos: [],
    notes_count: 0, new_notes: 0, first_new_note: null, new: false, seen_at: null, project: 'shop2024',
    candidates: [], sessions: [], auto: [], ...over,
  };
}
const todo = (id, action) => ({ id, action, author: 'carol', created_at: '2026-09-24T10:00:00Z', note_id: null, body: '' });
const sess = (id, state, over = {}) => ({ id, state, title: `s-${id}`, hidden: false, pid: 1, ...over });

test('plural of new comments', () => {
  assert.equal(newNotesLabel(0), '');
  assert.equal(newNotesLabel(1), '1 new comment');
  assert.equal(newNotesLabel(2), '2 new comments');
  assert.equal(newNotesLabel(4), '4 new comments');
  assert.equal(newNotesLabel(5), '5 new comments');
  assert.equal(newNotesLabel(22), '22 new comments');
  assert.equal(plural(3, 'a', 'b', 'c'), 'b');
});

test('groups: new/new notes first, then pending todo, assigned, own MRs; server order kept', () => {
  const a = item({ reasons: ['authored'], type: 'mr' });
  const b = item({ todos: [todo(1, 'mentioned')], reasons: ['mentioned'] });
  const c = item({ new_notes: 2 });
  const d = item({});
  const e = item({ new: true, reasons: ['assigned'] });
  const f = item({ todos: [todo(2, 'directly_addressed')], reasons: ['directly_addressed'] });
  assert.deepEqual([a, b, c, d, e].map(groupOf), ['mine', 'todo', 'new', 'assigned', 'new']);
  const g = groupItems([a, b, c, d, e, f]);
  assert.deepEqual(g.new.map((x) => x.iid), [c.iid, e.iid]);
  assert.deepEqual(g.todo.map((x) => x.iid), [b.iid, f.iid]);
  assert.deepEqual(g.assigned.map((x) => x.iid), [d.iid]);
  assert.deepEqual(g.mine.map((x) => x.iid), [a.iid]);
  // an open preview keeps its row where it was after "seen"
  const seen = { ...c, new_notes: 0 };
  assert.deepEqual(groupItems([seen], new Map([[seen.key, 'new']])).new.map((x) => x.iid), [c.iid]);
});

test('assigned = what GitLab lists as assigned now; a stale "assigned" to-do alone is not (2026-10-11)', () => {
  const now = item({ listed: ['assigned'] });
  const stale = item({ reasons: ['assigned'], todos: [todo(9, 'assigned')], listed: [] });
  const old = item({ reasons: ['assigned'] });   // an older piperd without `listed`: the reasons decide
  assert.deepEqual([now, stale, old].map(isAssigned), [true, false, true]);
  assert.equal(filterCounts([now, stale], { project: '', query: '' }).assigned, 1);
});

test('filter counts follow the project and the search, the pinned rows stay visible', () => {
  const items = [
    item({ new: true, reasons: ['assigned'] }),
    item({ reasons: ['mentioned'], todos: [todo(3, 'mentioned')] }),
    item({ reasons: ['directly_addressed'], todos: [todo(4, 'directly_addressed')], gl: 'grp/zone', project: null, title: 'Café price list fix' }),
    item({ type: 'mr', reasons: ['authored'] }),
  ];
  assert.deepEqual(filterCounts(items, { project: '', query: '' }), { all: 4, new: 1, assigned: 1, mentions: 2, mr: 1 });
  assert.deepEqual(filterCounts(items, { project: 'grp/zone', query: '' }), { all: 1, new: 0, assigned: 0, mentions: 1, mr: 0 });
  assert.equal(filterCounts(items, { project: '', query: 'cafe' }).all, 1, 'diacritics-insensitive search');
  const v = { filter: 'new', project: '', query: '' };
  assert.deepEqual(visibleItems(items, v).map((x) => x.iid), [items[0].iid]);
  assert.deepEqual(visibleItems(items, v, new Set([items[1].key])).map((x) => x.iid), [items[0].iid, items[1].iid]);
});

test('search: #iid, !iid for MRs, project, labels; every word must hit', () => {
  const i = item({ iid: 86, title: 'Café order export', labels: ['Backend'], project: 'shop2024' });
  assert.ok(matchesQuery(i, '#86'));
  assert.ok(matchesQuery(i, 'shop cafe'));
  assert.ok(matchesQuery(i, 'backend'));
  assert.ok(!matchesQuery(i, 'shop invoices'));
  assert.ok(matchesQuery(item({ type: 'mr', iid: 5 }), '!5'));
});

test('project label: local key, else the last GitLab path segment', () => {
  assert.equal(projectLabel({ project: 'shop2024', gl: 'grp/acme.shop2024' }), 'shop2024');
  assert.equal(projectLabel({ project: null, gl: 'grp/zone.zone' }), 'zone.zone');
});

test('reasons sort by importance, unknown ones last', () => {
  assert.deepEqual(sortedReasons(['authored', 'weird', 'assigned', 'directly_addressed', 'assigned']),
    ['directly_addressed', 'assigned', 'authored', 'weird']);
});

test('primary action: continue a live linked session, else open / add / choose / none', () => {
  const s1 = sess('a', 'waiting');
  const s2 = sess('b', 'exited', { pid: null });
  const s3 = sess('c', 'working');
  assert.deepEqual(primaryAction(item({ sessions: ['a'], new_notes: 1 }), [s1]), { kind: 'continue', session: s1, followUp: true });
  assert.deepEqual(primaryAction(item({ sessions: ['a'] }), [s1]), { kind: 'continue', session: s1, followUp: false });
  assert.deepEqual(primaryAction(item({ sessions: ['c'], new_notes: 2 }), [s3]), { kind: 'continue', session: s3, followUp: false },
    'a working session cannot take a follow-up: just switch to it');
  const s4 = sess('d', 'attention', { at_prompt: false });
  assert.deepEqual(primaryAction(item({ sessions: ['d'], new_notes: 1 }), [s4]), { kind: 'continue', session: s4, followUp: false },
    'a permission dialog: piperd would refuse the follow-up, so it is not offered');
  assert.deepEqual(primaryAction(item({ sessions: ['b'] }), [s2]), { kind: 'open' }, 'an ended session without a conversation is not resumed');
  const s5 = sess('e', 'exited', { pid: null, claude_session: 'c-1' });
  assert.deepEqual(primaryAction(item({ sessions: ['e'] }), [s5]), { kind: 'resume', session: s5 }, 'W8: the session that worked on it goes on');
  assert.deepEqual(primaryAction(item({ sessions: ['a', 'e'] }), [s1, s5]).kind, 'continue', 'a live one wins over an ended one');
  assert.deepEqual(primaryAction(item({ sessions: ['e'] }), [{ ...s5, hidden: true }]), { kind: 'open' }, 'a hidden one is not resumed');
  assert.deepEqual(primaryAction(item({ sessions: ['e'] }), [{ ...s5, state: 'preflight' }]).kind, 'continue');
  assert.deepEqual(primaryAction(item({ sessions: ['a'] }), [{ ...s1, hidden: true }]), { kind: 'open' });
  const cand = { path: '/p/shop2024', key: 'shop2024' };
  assert.deepEqual(primaryAction(item({ project: null, candidates: [cand] }), []), { kind: 'add', candidate: cand });
  assert.equal(primaryAction(item({ project: null, candidates: [cand, { path: '/q/m', key: 'm' }] }), []).kind, 'choose');
  assert.deepEqual(primaryAction(item({ project: null }), []), { kind: 'none' });
});

test('notification diff: nothing on the first list, then new keys, new todos, more notes, automation', () => {
  const a = item({});
  const b = item({ reasons: ['authored'], type: 'mr' });
  assert.deepEqual(diffInbox(null, [a, b]), []);
  const prev = snapshot([a, b]);
  const c = item({ new: true, reasons: ['assigned'] });
  const d = item({ new: true, reasons: ['authored'], type: 'mr' });
  const a2 = { ...a, todos: [todo(9, 'mentioned')], reasons: ['assigned', 'mentioned'], new_notes: 1 };
  const b2 = { ...b, new_notes: 2 };
  const ev = diffInbox(prev, [a2, b2, c, d]);
  assert.deepEqual(ev.map((e) => [e.kind, e.item.iid, e.reason ?? e.added]),
    [['todo', a.iid, 'mentioned'], ['notes', b.iid, 2], ['new', c.iid, 'assigned']],
    'a mention comes with its note: one event; my own new MR is not news');
  const prev2 = snapshot([a2, b2, c, d]);
  assert.deepEqual(diffInbox(prev2, [a2, b2, c, d]), [], 'the same list twice is quiet');
  assert.deepEqual(diffInbox(prev2, [{ ...b2, new_notes: 1 }]), [], 'fewer unseen notes (read) is quiet');
  const auto = diffInbox(prev2, [{ ...c, auto: [{ event: 'assigned', session: 's9', at: 1 }] }]);
  assert.deepEqual(auto.map((e) => [e.kind, e.session]), [['auto', 's9']]);
  assert.equal(eventText(auto[0]).title, `Analysis of #${c.iid} started automatically`);
  assert.equal(eventText(ev[1]).title, `2 new comments in !${b.iid} ${b.title}`);
  assert.equal(eventText(ev[2]).title, `New assignment: #${c.iid} ${c.title}`);
});

test('target note: piperd\'s choice, else (older piperd) the first new comment', () => {
  assert.equal(targetNote({ target_note: 40, first_new_note: 30 }), 40);
  assert.equal(targetNote({ target_note: null, first_new_note: 30 }), null, 'piperd says no discussion');
  assert.equal(targetNote({ first_new_note: 30 }), 30);
});

test('task_for mirror: references only, note line, work vs analyze, MR wording', () => {
  const i = item({ iid: 86, title: 'Price list', web_url: 'https://g/x/-/issues/86', gl: 'grp/acme.shop2024' });
  const work = taskFor(i, 'work', 123).split('\n');
  assert.deepEqual(work.slice(0, 3), ['GitLab issue #86: Price list', 'https://g/x/-/issues/86', '']);
  assert.equal(work[3], 'Handle comment note_123 (and anything newer) - that is the task now. Read it with the tool gitlab_issue (project "grp/acme.shop2024", iid 86). The description and older comments are context only: do not re-analyse the whole issue, look back only where the comment needs it.',
    'W9: a discussion under way - its comment is the task, not the whole issue');
  assert.equal(work[4], 'Clarify the task first, then implement and verify.');
  assert.match(work[5], /development\/gitlab-86-comment\.md .*<details>.*Do not post it - the user sends it from the GUI\.$/);
  const an = taskFor(i, 'analyze').split('\n');
  assert.equal(an.length, 5, 'one read line either way');
  assert.equal(an[3], 'Read the task including all comments with the tool gitlab_issue (project "grp/acme.shop2024", iid 86) - the comments are part of the task.');
  assert.match(an[4], /^Analyze only: change no code, do not commit, send nothing\..*development\/gitlab-86-analysis\.md/);
  const mr = taskFor({ ...i, type: 'mr' }, 'work');
  assert.match(mr, /^GitLab merge request !86: Price list/);
  assert.match(mr, /the tool gitlab_mr \(project/);
  assert.match(mr, /development\/gitlab-mr-86-comment\.md/);
});

test('task_for mirror: the issue file names and comment style piperd sends (an extension renames them)', () => {
  const i = item({ iid: 86, title: 'T', web_url: 'https://g/x/-/issues/86', gl: 'grp/app' });
  const files = { status: 'state', analysis: 'notes', comment: 'reply', comment_style: 'formal' };
  assert.match(taskFor(i, 'work', null, files), /development\/gitlab-86-reply\.md \(formal\)\. Do not post it/);
  assert.match(taskFor(i, 'analyze', null, files), /development\/gitlab-86-notes\.md/);
});

test('comment lint: empty/too long block, @mentions warn (not in code, not e-mails), missing <details>', () => {
  assert.equal(lintComment('  ').blocked, true);
  assert.equal(lintComment('x'.repeat(20001)).blocked, true);
  const ok = lintComment('Hello, fixed.\n\n<details><summary>Detail</summary>\n\ncode\n\n</details>');
  assert.deepEqual(ok.hints, []);
  const m = lintComment('Thanks @alice, see `@Autowired` and mail a@b.example\n```\n@Override\n```\n<details>x</details>');
  assert.deepEqual(m.mentions, ['@alice']);
  assert.equal(m.blocked, false);
  assert.equal(m.hints.length, 1);
  assert.match(lintComment('Done.').hints[0].text, /No <details> block/);
  assert.match(lintComment('a\n<details>\nb').hints[0].text, /is not closed/);
});

test('<details> blocks become a tree (summary lifted, fences respected, stray close kept)', () => {
  const t = splitDetails('Summary\n\n<details>\n<summary>**Technical** detail</summary>\n\nA\n```\n</details>\n```\n</details>\nend\n</details>');
  assert.equal(t.length, 3);
  assert.deepEqual(t[0], { t: 'md', lines: ['Summary', ''] });
  assert.equal(t[1].t, 'details');
  assert.equal(t[1].summary, '**Technical** detail');
  assert.deepEqual(t[1].children, [{ t: 'md', lines: ['', 'A', '```', '</details>', '```'] }]);
  assert.deepEqual(t[2], { t: 'md', lines: ['end', '</details>'] });
  const inline = splitDetails('<details open><summary>S</summary>x</details>');
  assert.deepEqual(inline, [{ t: 'details', summary: 'S', open: true, children: [{ t: 'md', lines: ['x'] }] }]);
});

test('relative GitLab links resolve against the project, other schemes are refused', () => {
  const u = 'https://gitlab.com/grp/acme.shop2024/-/issues/86';
  assert.equal(gitlabHref('/uploads/ab/x.png', u), 'https://gitlab.com/grp/acme.shop2024/uploads/ab/x.png');
  assert.equal(gitlabHref('../87', u), 'https://gitlab.com/grp/acme.shop2024/-/87');
  assert.equal(gitlabHref('javascript:alert(1)', u), null);
});

test('workflow strip: done ticked, the step after the last done one is next, earlier gaps skipped', () => {
  const f = { analysis: false, impl: true, verify: 2, draft: { path: 'development/gitlab-86-comment.md', mtime: 1, sent: false }, commits: 0, ahead: null, pushed: false };
  const s = flowSteps(f);
  assert.deepEqual(s.map((x) => [x.id, x.state]), [
    ['analysis', 'skipped'], ['impl', 'done'], ['verify', 'done'], ['comment', 'next'], ['commit', 'todo'], ['push', 'todo']]);
  assert.equal(s[2].label, 'Review ×2');
  assert.equal(s[3].partial, true);
  const fresh = flowSteps({ analysis: false, impl: false, verify: 0, draft: null, commits: 0, ahead: null, pushed: false });
  assert.equal(fresh[0].state, 'next');
  const pushed = flowSteps({ analysis: true, impl: true, verify: 1, draft: { path: 'x', mtime: 1, sent: true }, commits: 2, ahead: 0, pushed: true });
  assert.ok(pushed.every((x) => x.state === 'done'));
  assert.match(flowSteps({ ...f, commits: 3, ahead: 3 })[5].title, /3 commits wait for a push/);
});

test('workflow of a session that never started: uncommitted files and commits are not its work', () => {
  const f = { analysis: true, impl: true, verify: 0, draft: null, commits: 1, ahead: 1, pushed: false };
  const blocked = flowSteps(sessionFlow(f, { pid: null, claude_session: null }));
  assert.equal(blocked.filter((x) => x.state === 'done').length, 0);
  assert.equal(blocked[0].state, 'next');
  assert.equal(sessionFlow(f, { pid: 42, claude_session: null }), f);
  assert.equal(sessionFlow(f, { pid: null, claude_session: 'abcdef12' }), f);
});

test('rules: since = now only when a rule becomes enabled', () => {
  const before = [{ id: 'a', gl: '*', on: ['assigned'], action: 'analyze', enabled: true, since: 100 },
    { id: 'b', gl: 'x', on: ['mentioned'], action: 'analyze', enabled: false, since: 50 }];
  const out = stampRules([{ ...before[0] }, { ...before[1], enabled: true }, { id: 'c', gl: 'y', on: ['new_note'], action: 'analyze', enabled: false, since: 0 }], before, 999);
  assert.deepEqual(out.map((r) => r.since), [100, 999, 0]);
});

test('N4: a GitLab comment / to-do written a moment ago reads "just now", never "now ago"', () => {
  assert.equal(isoAgoText(new Date().toISOString()), 'just now');
  assert.equal(isoAgoText(new Date(Date.now() - 5 * 60_000).toISOString()), '5 min ago');
  assert.equal(isoAgoText(null), '');
  assert.equal(isoAgoText('garbage'), '');
});

test('N10-D-10: "e" on a read row (mark unread) is not news - piperd\'s next list counts the old comments as new again', async () => {
  const mod = await import('../../piper/web/dist/inbox.js');
  assert.equal(typeof mod.markedUnread, 'function');
  const it = item({ new_notes: 0, notes_count: 2 });
  const prev = snapshot([it]);
  mod.markedUnread(prev, it);
  // piperd answers the unseen item with new_notes 1 (the colleague's comment of the two): no "1 new comment" toast
  assert.deepEqual(diffInbox(prev, [{ ...it, new: true, new_notes: 1 }]), []);
  // a comment that really arrives afterwards is still news
  assert.equal(diffInbox(prev, [{ ...it, new: true, new_notes: 3, notes_count: 3 }]).length, 1);
  const src = (await import('node:fs')).readFileSync(new URL('../../piper/web/src/inbox.ts', import.meta.url), 'utf8');
  assert.match(src, /patch\(i\.key, \(x\) => \{ x\.new = true; \}\);\s*if \(prev\) markedUnread\(prev, i\);/);
});

test('9.43 GitLab / GitHub segment: counts, rows, chips and the remembered view follow the forge', () => {
  const items = [
    item({ new: true }),
    item({ type: 'mr', reasons: ['authored'] }),
    item({ gl: 'gh:acme/tools', project: 'tools', key: 'gh:acme/tools#issue#41' }),
    item({ gl: 'gh:acme/tools', project: 'tools', key: 'gh:acme/tools#issue#42', new: true, title: 'Upgrade parent' }),
  ];
  assert.equal(matchesForge(items[2], 'github'), true);
  assert.equal(matchesForge(items[2], 'gitlab'), false);
  assert.equal(matchesForge(items[0], ''), true);
  assert.equal(matchesForge(items[0], undefined), true);
  assert.deepEqual(forgeCounts(items, { project: '', query: '' }), { all: 4, gitlab: 2, github: 2 });
  assert.deepEqual(forgeCounts(items, { project: '', query: 'upgrade' }), { all: 1, gitlab: 0, github: 1 });
  assert.deepEqual(filterCounts(items, { project: '', query: '', forge: 'github' }), { all: 2, new: 1, assigned: 2, mentions: 0, mr: 0 });
  assert.deepEqual(filterCounts(items, { project: '', query: '', forge: 'gitlab' }).all, 2);
  assert.deepEqual(visibleItems(items, { filter: 'all', project: '', query: '', forge: 'github' }).map((x) => x.gl), ['gh:acme/tools', 'gh:acme/tools']);
  assert.equal(visibleItems(items, { filter: 'new', project: '', query: '', forge: 'gitlab' }).length, 1);
  assert.equal(visibleItems(items, { filter: 'all', project: '', query: '' }).length, 4, 'no forge = both');
  const src = readFileSync(new URL('../../piper/web/src/inbox.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  // the segment: in front of the chips, only with both forges (or one picked), remembered with the chip
  assert.match(src, /h\('div', \{ class: 'ibx-tools' \}, searchBox, projSel, forgeSeg\)/);
  assert.match(src, /forgeSeg\.classList\.toggle\('hidden', !both && !view\.forge\);/);
  assert.match(src, /writeJson\(VIEW_KEY, \{ filter: view\.filter, forge: f \}\);/);
  assert.match(src, /writeJson\(VIEW_KEY, \{ filter: f, forge: view\.forge \}\);/);
  assert.match(src, /forge: saved\.forge === 'gitlab' \|\| saved\.forge === 'github' \? saved\.forge : ''/);
  // Mark shown as read, Clear filter, reveal and the project list follow it
  assert.match(src, /const narrowed = \(\) => view\.filter !== 'all' \|\| !!view\.forge \|\|/);
  assert.match(src, /view\.filter = 'all'; view\.forge = ''; view\.project = '';/);
  assert.match(src, /view\.filter = 'all';\n\s*view\.forge = '';/);
  assert.match(src, /items\.filter\(\(i\) => matchesForge\(i, view\.forge\)\)\.map\(\(i\) => \[i\.gl, projectLabel\(i\)\]/);
  assert.match(css, /^\.ibx-forge \{ display: flex;/m);
});

test('F15 a row = one thing: at most two reasons, one coloured action chip, the rest "+N"', () => {
  assert.deepEqual(rowReasons(['assigned']), { chip: null, text: ['assigned'], more: [] });
  assert.deepEqual(rowReasons(['authored', 'build_failed']), { chip: 'build_failed', text: ['authored'], more: [] });
  assert.deepEqual(rowReasons(['assigned', 'mentioned', 'directly_addressed']), { chip: 'directly_addressed', text: ['mentioned'], more: ['assigned'] });
  // two action reasons: only the most pressing is coloured, the other one is text
  assert.deepEqual(rowReasons(['unmergeable', 'build_failed', 'authored']), { chip: 'build_failed', text: ['unmergeable'], more: ['authored'] });
  assert.deepEqual(rowReasons(['marked', 'assigned', 'mentioned']), { chip: null, text: ['mentioned', 'assigned'], more: ['marked'] });
  assert.deepEqual(rowReasons([]), { chip: null, text: [], more: [] });
});

test('F15 row structure: actions on hover (ra-row / row-actions), no green row button, Add checkout… link, header ⋯', () => {
  const src = readFileSync(new URL('../../piper/web/src/inbox.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(src, /class: 'irow ra-row'/);
  assert.match(src, /class: 'irow-act row-actions'/);
  // the row's main action is a quiet button (Enter still runs it)
  assert.match(src, /class: `small trunc ibx-main \$\{cls\}`/);
  assert.doesNotMatch(src.slice(src.indexOf('function primaryEl'), src.indexOf('// ---------------------------------------------------------------- draw')), /'primary'/);
  assert.match(src, /case 'Enter': if \(!onRow\) return; primary\(i\); break;/);
  // no checkout: one grey link, the paths only in the dialog
  assert.match(src, /btn\('Add checkout…'/);
  assert.doesNotMatch(src, /class: 'ibx-cand'/);
  assert.match(src, /function openCheckout\(i: InboxItem, cands/);
  // new = bold title, no inline orange dot; labels in the tooltip and the preview
  assert.doesNotMatch(src, /class: 'ibx-dot'/);
  assert.match(css, /\.inbox \.irow\.fresh \.ititle \{ font-weight: 600; \}/);
  assert.match(src, /labels \$\{i\.labels\.join\(', '\)\}/);
  // the linked session chip keeps its state dot and names the state
  assert.match(src, /h\('span', \{ class: 'dot st-' \+ s\.state/);
  // Mark all as read and Automation… in the header ⋯; Automation dialog and Done in GitLab stay
  assert.match(src, /label: narrowed\(\) \? 'Mark shown as read' : 'Mark all as read'/);
  assert.match(src, /label: 'Automation…'.*run: \(\) => openRules\(\)/);
  assert.match(src, /deps\.modal\('GitLab automation'/);
  assert.match(src, /Done in GitLab/);
  assert.match(css, /\/\* ==== 9\.72 F15 Inbox[^]*\/\* ==== end F15 ==== \*\//);
});

test('F24 lint fixes: Remove @ (outside code, e-mails kept), Insert block (template + caret), Close block', () => {
  const m = lintComment('Thanks @alice and @bob.k, see `@Autowired`, mail a@b.example\n```\n@Override\n```\n<details>x</details>');
  assert.equal(m.hints[0].fix, 'unmention');
  const u = fixComment('Thanks @alice and @bob.k, see `@Autowired`, mail a@b.example\n```\n@Override\n```\n<details>x</details>', 'unmention');
  assert.equal(u.text, 'Thanks alice and bob.k, see `@Autowired`, mail a@b.example\n```\n@Override\n```\n<details>x</details>');
  assert.deepEqual(lintComment(u.text).mentions, []);
  assert.equal(lintComment('Done.').hints[0].fix, 'details');
  const d = fixComment('Done.  \n', 'details');
  assert.equal(d.text, 'Done.\n\n' + DETAILS_TEMPLATE);
  assert.match(DETAILS_TEMPLATE, /^<details><summary>.+<\/summary>\n[^]*<\/details>\n$/);
  assert.equal(d.text.slice(0, d.caret), 'Done.\n\n<details><summary>Technical detail</summary>\n\n', 'the caret lands inside the block');
  assert.deepEqual(lintComment(d.text).hints, [], 'the fixed comment passes');
  assert.equal(fixComment('', 'details').caret, '<details><summary>Technical detail</summary>\n\n'.length);
  const c = lintComment('a\n<details>\nb');
  assert.equal(c.hints[0].fix, 'close');
  assert.deepEqual(lintComment(fixComment('a\n<details>\nb', 'close').text).hints, []);
  // errors stay blocking and have no fix
  assert.equal(lintComment('x'.repeat(COMMENT_MAX + 1)).hints.find((x) => x.level === 'error').fix, undefined);
});

test('F24 dialog: 600 px, Write | Preview tabs, target in the header, count only above 80 %, fix buttons; mention two-step and draft restore stay', () => {
  const src = readFileSync(new URL('../../piper/web/src/comment.ts', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.modal:has\(\.cdlg\) \{ width: min\(600px, calc\(100vw - 32px\)\);/);
  assert.match(src, /role: 'tab', 'aria-selected': 'true' \}, 'Write'\)/);
  assert.match(src, /role: 'tab', 'aria-selected': 'false' \}, 'Preview'\)/);
  assert.match(src, /'Comment on'\),\s*h\('a', \{ href: o\.webUrl/);
  assert.match(src, /n > COMMENT_MAX \* 0\.8 \?/);
  assert.match(src, /unmention: 'Remove @', details: 'Insert block', close: 'Close block'/);
  assert.match(src, /'Really send with the mention'/);
  assert.match(src, /Restore the original text/);
  assert.match(src, /Mark to-do as done \(/);
  assert.match(src, /'Keep writing'/);
  assert.match(src, /send\.append\('Send to GitLab', h\('kbd', \{\}, kbd\('⌘⏎', 'Ctrl'\)\)\)/);
});


test('GitLab can stay enabled without toasts, Recent alerts or desktop notices; GitHub stays independent', async () => {
  const { notificationEvents } = await import('../../piper/web/dist/inbox.js');
  const events = [{kind:'new', item:{key:'gl'}}, {kind:'auto', item:{key:'auto', forge:'gitlab'}}, {kind:'new', item:{key:'gh', forge:'github'}}];
  assert.deepEqual(notificationEvents(events, forge => forge !== 'gitlab').map(x => x.item.key), ['gh']);
  assert.deepEqual(notificationEvents(events, () => true), events);
  const source = readFileSync(new URL('../../piper/web/src/inbox.ts', import.meta.url), 'utf8');
  const announce = source.slice(source.indexOf('function announce('), source.indexOf('async function refresh('));
  assert.ok(announce.indexOf('notificationEvents') < announce.indexOf('deps.notify'));
  assert.ok(announce.indexOf('notificationEvents') < announce.indexOf('deps.toast'));
});
