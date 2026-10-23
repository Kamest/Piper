// node --test: W13 - one decision (decision.ts), the notification history (notices.ts), two send
// classes (sendkeys.ts). Pure helpers from web/dist; structure checks over web/src and app.css.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { decisionButtons, orderOps, opRole, cardModel, cardKey, moreLine, shownCard, shortPaths, KIND_SHORT } from '../../piper/web/dist/decision.js';
import { KIND_TEXT } from '../../piper/web/dist/feed.js';
import { NoticeLog, NOTICE_MAX, NOTICE_MAX_AGE } from '../../piper/web/dist/notices.js';
import { isSend, sendHint } from '../../piper/web/dist/sendkeys.js';
import { auditIcon, auditRisk, auditSentence, auditTime, confirmWord, mergeAudit, repeatText } from '../../piper/web/dist/board.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const sig = (bs) => bs.map((b) => `${b.role}:${b.label}${b.cls ? ':' + b.cls : ''}`);

test('W13 decisionButtons: Deny · [always] · Allow, the grant on the right', () => {
  assert.deepEqual(sig(decisionButtons()), ['deny:Deny', 'allow:Allow:primary']);
  assert.deepEqual(sig(decisionButtons({ always: 'Allow writes this session' })), ['deny:Deny', 'always:Allow writes this session', 'allow:Allow:primary'],
    'the DB card too: S3\'s always · Decline · Allow is gone');
  assert.deepEqual(sig(decisionButtons({ always: '', danger: '' })), ['deny:Deny', 'allow:Allow:primary'], 'an empty always is no button');
});

test('W13 decisionButtons: the keys only where they work, never on a red grant', () => {
  const q = decisionButtons({ always: 'Always for this session', keys: true });
  assert.deepEqual(sig(q), ['deny:Deny n', 'always:Always for this session', 'allow:Allow y:primary']);
  assert.deepEqual(q.map((b) => b.key), ['n', undefined, 'y']);
  const red = decisionButtons({ danger: 'Push to protected master', keys: true });
  assert.deepEqual(sig(red), ['deny:Deny n', 'allow:Push to protected master:danger']);
  assert.equal(red[1].key, undefined, 'a red Allow is a deliberate click');
  assert.match(red[1].title, /no y shortcut/);
  assert.ok(decisionButtons({ danger: 'Allow on PROD' }).every((b) => !b.key), 'the floating card writes no keys');
});

test('W13 orderOps / opRole: the Feed card in the same order, other ops after the decisions', () => {
  const ops = [
    { op: 'decide', label: 'Allow', args: { id: 'p1', ok: true } },
    { op: 'decide', label: 'Always', args: { id: 'p1', ok: true, always: true } },
    { op: 'decide', label: 'Deny', args: { id: 'p1', ok: false } },
    { op: 'open', label: 'Open', args: {} },
  ];
  assert.deepEqual(orderOps(ops).map((o) => o.label), ['Deny', 'Always', 'Allow', 'Open']);
  assert.deepEqual(ops.map((o) => o.label), ['Allow', 'Always', 'Deny', 'Open'], 'the data stays (y / n / Enter look ops up)');
  assert.deepEqual(ops.map(opRole), ['allow', 'always', 'deny', null]);
  assert.deepEqual(orderOps([{ op: 'reply', label: 'Reply', args: {} }, { op: 'open', label: 'Open', args: {} }]).map((o) => o.label), ['Reply', 'Open']);
});

test('W13 NoticeLog: newest first, a ring of 100, unread, mark read, clear', () => {
  const l = new NoticeLog(undefined, () => 10);
  l.add('a', 'toast', undefined, 1);
  const b = l.add('b', 'desktop', { label: 'Open', run() {} }, 2);
  assert.deepEqual(l.list().map((n) => n.text), ['b', 'a']);
  assert.equal(b.src, 'desktop');
  assert.equal(l.unread, 2);
  l.markRead();
  assert.equal(l.unread, 0);
  l.add('c');
  assert.equal(l.unread, 1);
  for (let i = 0; i < 150; i++) l.add(`n${i}`);
  assert.equal(l.list().length, NOTICE_MAX);
  assert.equal(l.list()[0].text, 'n149');
  assert.equal(l.list().at(-1).text, 'n50', 'the oldest go first');
  l.clear();
  assert.deepEqual(l.list(), []);
  assert.equal(l.unread, 0);
});

test('W13 isSend: chat ⏎, doc ⌘⏎, ⌘⏎ everywhere, never while an IME composes', () => {
  const k = (o) => ({ key: 'Enter', shiftKey: false, metaKey: false, ctrlKey: false, altKey: false, isComposing: false, ...o });
  assert.equal(isSend(k({}), 'chat'), true);
  assert.equal(isSend(k({ shiftKey: true }), 'chat'), false, '⇧⏎ a new line');
  assert.equal(isSend(k({ altKey: true }), 'chat'), false);
  assert.equal(isSend(k({ metaKey: true }), 'chat'), true, '⌘⏎ sends in a chat field too (before: nothing)');
  assert.equal(isSend(k({ ctrlKey: true }), 'chat'), true);
  assert.equal(isSend(k({}), 'doc'), false, '⏎ is a new line in a document');
  assert.equal(isSend(k({ metaKey: true }), 'doc'), true);
  assert.equal(isSend(k({ ctrlKey: true }), 'doc'), true);
  assert.equal(isSend(k({ metaKey: true, altKey: true }), 'doc'), false);
  assert.equal(isSend(k({ isComposing: true }), 'chat'), false);
  assert.equal(isSend(k({ isComposing: true, metaKey: true }), 'doc'), false);
  assert.equal(isSend(k({ key: 'a', metaKey: true }), 'doc'), false);
  assert.equal(sendHint('chat'), '⏎ sends · ⇧⏎ new line');
  assert.equal(sendHint('doc'), '⌘⏎ sends · ⏎ new line');
});

test('W13 wiring: every card from decisionButtons, one word, one notation', () => {
  const main = read('main.ts');
  assert.match(main, /const danger = dangerLabel\(p\);[^]*?decisionButtons\(\{ always: p\.always, sessionAlways: p\.session_always, danger, chips: true \}\)/, 'the floating card and the DB card');
  assert.match(main, /class: 'pending' \+ \(danger \? ' pdanger' : ''\)/, 'a red grant, a red edge (F17: 3 px left, not the whole frame)');
  assert.match(css, /\.pending\.pdanger \{ box-shadow: inset 3px 0 0 var\(--bad\)/);
  assert.doesNotMatch(main, /Show in queue/, 'F17: one whole card, no compact ones');
  assert.match(read('queue.ts'), /decisionButtons\(\{ always: always\?\.label, sessionAlways: session\?\.label, danger: allowDanger\(a, deps\.dangerOf\), keys: true \}\)/);
  assert.match(read('feed.ts'), /\.\.\.orderOps\(a\.actions\)\.map/);
  assert.match(readFileSync(new URL('../../piper/feed.py', import.meta.url), 'utf8'), /\{"op": "decide", "label": "Deny",/);
  for (const f of readdirSync(SRC).filter((x) => x.endsWith('.ts'))) {
    const src = read(f);
    assert.ok(!/'Decline'/.test(src), `${f}: "Decline" is gone (W13: Deny everywhere)`);
    assert.ok(!/⌘↵|⌘Enter|Enter sends|\(Enter |⇧Enter/.test(src), `${f}: one notation - ⏎ ⇧⏎ ⌘⏎`);
  }
});

test('W13 wiring: the send fields go through isSend', () => {
  const chat = { 'queue.ts': 0, 'feed.ts': 2, 'main.ts': 0 };   // D7: the queue's and Conversation's reply fields are the Composer
  const doc = { 'main.ts': 2, 'comment.ts': 1, 'inbox.ts': 1 };   // gitpanel.ts / files.ts: their old notes went with D14
  assert.match(read('composer.ts'), /isSend\(e, compact \? 'chat' : 'doc'\)/, 'collapsed ⏎, full ⌘⏎');
  for (const [f, n] of Object.entries(chat)) assert.equal([...read(f).matchAll(/isSend\(e, 'chat'\)/g)].length, n, `${f}: chat fields`);
  for (const [f, n] of Object.entries(doc)) assert.equal([...read(f).matchAll(/isSend\(e, 'doc'\)/g)].length, n, `${f}: document fields`);
  assert.match(read('inbox.ts'), /kbd\('Resume ⌘⏎', 'Ctrl'\)/, 'the inbox Resume task sends with ⌘⏎ (before: the button only)');
});

test('W13 wiring: toasts and desktop notices land in the list; the bell; × and hover', () => {
  const main = read('main.ts');
  assert.match(main, /notices\.add\(text, 'toast'/);
  assert.match(main, /notices\.add\(body \? `\$\{title\} · \$\{body\.split\('\\n'\)\[0\]\}` : title, 'desktop'/, 'logged before the permission check');
  assert.match(main, /notice: \(text\) => \{ notices\.add\(text, 'files'\); bellSoon\(\); \}/, 'the Files Undo toast too');
  assert.match(read('files.ts'), /deps\.notice\?\.\(text\);/);
  assert.match(main, /item\('bell', 'Recent alerts', '', !!closeNotices,/);
  assert.match(main, /badge\(notices\.unread, 'warn'/);
  assert.match(main, /h\('div', \{ id: 'notices', role: 'dialog'/, 'a list, not a menu');
  assert.match(main, /if \(t\.matches\(':hover'\)\) return;/, 'the mouse over a toast holds it');
  assert.match(main, /class: 'tclose'/);
  assert.match(main, /Notifications\)', undefined, 'notification-help'\)/);
  assert.match(main, /cls === 'notification-help' && \(document\.body\.classList\.contains\('pending-open'\) \|\| overlay\.classList\.contains\('open'\)\)/);
  assert.match(css, /body\.pending-open #toasts \.notification-help,\s*body:has\(#overlay\.open\) #toasts \.notification-help \{ display: none; \}/);
  assert.match(main, /label: 'Recent alerts', hint: 'the last toasts and desktop notices \(the bell\)'/, 'the palette');
  assert.match(main, /const bellSoon = \(\): void => \{ window\.setTimeout\(renderActbar, 0\); \};/, 'a toast before the queue exists must not throw');
  assert.match(css, /#notices \{ position: fixed;/);
  assert.match(css, /\.nt-item\.unread \.nt-dot \{ background: var\(--warn\); \}/);
});


test('session grants: independent scope label, production red, no keyboard grant', () => {
  const label = 'Allow for this session on PROD';
  const buttons = decisionButtons({ always: 'Allow this content', sessionAlways: label, danger: 'Allow on PROD', keys: true });
  assert.deepEqual(sig(buttons), ['deny:Deny n', 'always:Allow this content', `session:${label}:danger`, 'allow:Allow on PROD:danger']);
  assert.equal(buttons[2].key, undefined);
  assert.match(buttons[2].title, /only in the displayed environment/);
  assert.equal(decisionButtons({ sessionAlways: 'Allow for this session on TEST' })[1].cls, '');
  const ops = [
    { op: 'decide', args: { ok: true } },
    { op: 'decide', args: { ok: true, session_always: true } },
    { op: 'decide', args: { ok: false } },
    { op: 'decide', args: { ok: true, always: true } },
  ];
  assert.deepEqual(orderOps(ops).map(opRole), ['deny', 'always', 'session', 'allow']);
});


test('alerts expire on restore and while a page stays open, persisting the cleanup', () => {
  let now = NOTICE_MAX_AGE;
  let saved = JSON.stringify([{ text: 'expired', ts: 0 }, { text: 'recent', ts: 1 }]);
  const store = { get: () => saved, set: (v) => { saved = v; } };
  const log = new NoticeLog(store, () => now);
  assert.deepEqual(log.list().map(n => n.text), ['recent']);
  assert.equal(JSON.parse(saved).length, 1);
  now++;
  assert.equal(log.unread, 0);
  assert.deepEqual(log.list(), []);
  assert.deepEqual(JSON.parse(saved), []);
  log.add('fresh');
  assert.equal(log.unread, 1);
  log.clear();
  assert.deepEqual(new NoticeLog(store, () => now).list(), []);
});


test('alerts: another window cannot resurrect cleared history by adding or marking read', () => {
  let saved = null;
  const store = { get: () => saved, set: (v) => { saved = v; } };
  const a = new NoticeLog(store, () => 10);
  a.add('old');
  const b = new NoticeLog(store, () => 10);
  a.clear();
  b.markRead();
  b.add('new');
  assert.deepEqual(a.list().map(n => n.text), ['new']);
  assert.equal(a.unread, 1);
  assert.deepEqual(new NoticeLog(store, () => 10).list().map(n => n.text), ['new']);
});

test('alerts: closing dismisses shown entries and retains new arrivals and local actions', () => {
  let saved = null;
  const store = { get: () => saved, set: (v) => { saved = v; } };
  const a = new NoticeLog(store, () => 10);
  const action = { label: 'Open', run() {} };
  a.add('shown', 'toast', action);
  const shown = a.list().map(n => n.id);
  a.markRead();
  const b = new NoticeLog(store, () => 10);
  b.add('incoming');
  assert.equal(a.list().find(n => n.text === 'shown').action, action);
  a.dismiss(shown);
  assert.deepEqual(b.list().map(n => n.text), ['incoming']);
  assert.equal(b.unread, 1);
});


test('repeated identical alerts in a minute update one entry across windows', () => {
  let saved = null;
  const store = { get: () => saved, set: v => { saved = v; } };
  const a = new NoticeLog(store, () => 10);
  const b = new NoticeLog(store, () => 10);
  a.add('preview unavailable', 'toast', undefined, 10);
  b.add('preview unavailable', 'toast', undefined, 20);
  assert.equal(a.list().length, 1);
  assert.equal(a.list()[0].ts, 20);
  a.add('a different failure', 'toast', undefined, 21);
  assert.equal(a.list().length, 2);
  a.add('preview unavailable', 'toast', undefined, 60020);
  assert.equal(a.list().length, 3, 'a later occurrence is a new alert');
});

test('board previews retry brief failures silently, warn once on sustained failure, and recover', async () => {
  const source = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf("let boardSig = ''"), source.indexOf('const audit = createAudit('));
  const messages = [];
  let fail = true;
  const api = { board: async () => { if (fail) throw new Error('Load failed'); return { data: {cards: []} }; } };
  const run = new Function('api', 'token', 'toast', 'render', 'boardData', body + '; return loadBoard;')(api, () => 'test', text => messages.push(text), () => {}, new Map());
  await run(); await run();
  assert.deepEqual(messages, []);
  await run(); await run();
  assert.deepEqual(messages, ['Session previews are temporarily unavailable. Retrying automatically.']);
  fail = false; await run();
  fail = true; await run(); await run();
  assert.equal(messages.length, 1);
  await run();
  assert.equal(messages.length, 2, 'a new sustained outage is reported again');
});


// ---------------------------------------------------------------- F17
test('F17 decisionButtons chips: plain labels, the keys beside them, never on a red grant', () => {
  const b = decisionButtons({ always: 'Allow always', sessionAlways: 'Allow for this session', chips: true });
  assert.deepEqual(sig(b), ['deny:Deny', 'always:Allow always', 'session:Allow for this session', 'allow:Allow:primary']);
  assert.deepEqual(b.map((x) => x.key), ['n', undefined, undefined, 'y']);
  const red = decisionButtons({ danger: 'Allow on PROD', chips: true });
  assert.equal(red.at(-1).key, undefined);
  assert.match(red.at(-1).title, /no y shortcut/);
});

test('F17 KIND_SHORT: every kind the cards know has a short header word', () => {
  for (const k of Object.keys(KIND_TEXT)) assert.ok(KIND_SHORT[k], `KIND_SHORT.${k}`);
});

test('F17 cardModel: git push - one fact line, the rest as context, MEDIUM, the Security path', () => {
  const m = cardModel({ kind: 'git_push', summary: 'Push of branch orchestrator → origin/orchestrator (3 commits ahead):\n\nabc one\ndef two',
    payload: { branch: 'orchestrator' }, why: { group: 'Git', label: 'Push (git_push)' } });
  assert.equal(m.fact, 'git push origin orchestrator');
  assert.equal(m.context, '3 commits ahead · → origin/orchestrator');
  assert.deepEqual(m.risk, { word: 'MEDIUM', level: 'medium' });
  assert.equal(m.why, 'Security › Git › Push', 'the tool id stays out of the sentence');
  assert.equal(m.short, 'git push');
});

test('F17 cardModel: allow_domain - the domain is the fact, ⚠ lines stay visible, a red one is HIGH', () => {
  const summary = '⚠ host.docker.internal is a host gateway name - the sandbox would reach services on this machine\n\nSession x wants to allow the domain host.docker.internal in /private/tmp/very/long/path/projects/grp/demo/.hole/settings.json (network.allow).';
  const m = cardModel({ kind: 'allow_domain', summary, payload: { domain: 'host.docker.internal', danger: true } }, 'Allow, this machine');
  assert.equal(m.fact, 'host.docker.internal');
  assert.equal(m.warns.length, 1);
  assert.deepEqual(m.risk, { word: 'HIGH', level: 'high' });
  assert.equal(m.why, null, 'an older piperd without why: no line');
});

test('F17 cardModel: commit, git run, http, settings, a generic kind; PROD for production', () => {
  const c = cardModel({ kind: 'git_commit', summary: 'Commit on branch main (2 files). You approve these paths - x:\n\nFix the PDF\n\na.txt\nb.txt' });
  assert.equal(c.fact, 'Fix the PDF');
  assert.match(c.context, /^branch main · 2 files/);
  assert.equal(c.risk.word, 'LOW');
  const g = cardModel({ kind: 'git_run', summary: 'git reset --hard', payload: { args: ['reset', '--hard'], warnings: ['discards changes'] } });
  assert.equal(g.fact, 'git reset --hard');
  assert.equal(g.context, 'discards changes');
  const g2 = cardModel({ kind: 'git_run', summary: '⚠ discards changes\n\ngit reset --hard', payload: { args: ['reset', '--hard'], warnings: ['discards changes'] } });
  assert.deepEqual([g2.warns.length, g2.context], [1, ''], 'a ⚠ line is not repeated as the context');
  const ht = cardModel({ kind: 'http_prod', summary: 'HTTP requests on PROD', payload: { file: 'api/demo.http', env: 'prod', hosts: ['prod.example.com'] } }, 'Allow on PROD');
  assert.equal(ht.fact, 'api/demo.http');
  assert.equal(ht.context, 'environment prod · outside host prod.example.com');
  assert.deepEqual(ht.risk, { word: 'PROD', level: 'high' });
  const st = cardModel({ kind: 'settings_change', summary: 'Session s wants to change Piper\'s settings (git):\n\nPush: false → true\nX: 1 → 2', payload: { section: 'git' } });
  assert.equal(st.fact, 'Push: false → true');
  assert.equal(st.context, 'Settings › Git · 2 values');
  const o = cardModel({ kind: 'edit_file', summary: 'Edit /Users/x/www/projects/other/src/a.ts\nmore' });
  assert.equal(o.fact, 'Edit …/src/a.ts', 'an absolute path short - the whole one is in Details');
  assert.equal(cardModel({ kind: 'db_write_prod', summary: 'x', payload: { card: {} } }, 'Allow on PROD').fact, '', 'the DB card draws its statement');
  assert.equal(shortPaths('in /a/b/c/d.json (x)'), 'in …/c/d.json (x)');
});

test('F17 stack: the picked card, else the first red one, else the oldest; the bar line', () => {
  const l = [{ id: 'a' }, { id: 'b', red: true }, { id: 'c' }];
  assert.equal(shownCard(l, '', (p) => !!p.red).id, 'b');
  assert.equal(shownCard(l, 'c', (p) => !!p.red).id, 'c');
  assert.equal(shownCard(l, 'gone', () => false).id, 'a');
  assert.equal(moreLine(['question', 'allow domain']), '2 more waiting · question · allow domain');
  assert.equal(moreLine(['git push', 'git push']), '2 more waiting · git push');
  assert.equal(moreLine([]), '');
});

test('F17 cardKey: y / n only on the focused card - never in a field, with a modifier, or as a red grant', () => {
  const k = (key, o = {}) => ({ key, metaKey: false, ctrlKey: false, altKey: false, ...o });
  assert.equal(cardKey(k('y'), false, false), 'allow');
  assert.equal(cardKey(k('n'), false, false), 'deny');
  assert.equal(cardKey(k('y'), true, false), null, 'a red Allow is a click');
  assert.equal(cardKey(k('n'), true, false), 'deny', 'Deny is always safe');
  assert.equal(cardKey(k('y'), false, true), null);
  assert.equal(cardKey(k('y', { metaKey: true }), false, false), null);
  assert.equal(cardKey(k('x'), false, false), null);
});

test('F17 wiring: one card + the bar, keys on the card element (not the document), ⌘J to the card, why from piperd', () => {
  const main = read('main.ts');
  assert.match(main, /box\.append\(shown \? pendingCard\(shown\) : askCard\(ask!\)\);/, 'one whole card');
  assert.match(main, /class: 'pmore'/, 'the rest is one bar');
  assert.match(main, /card\.addEventListener\('keydown', \(e\) => \{\s*const t = e\.target as HTMLElement;\s*const act = cardKey\(e, !!danger,/);
  assert.doesNotMatch(main, /document\.addEventListener\('keydown'[^\n]*cardKey/, 'the terminal\'s y never approves');
  assert.match(main, /if \(document\.activeElement\?\.closest\('#pending'\) \|\| !focusPending\(\)\) nextWaiting\(\);/);
  assert.match(main, /if \(hadFocus\) focusPending\(\);/, 'a new card does not drop the keyboard');
  assert.match(main, /h\('span', \{ class: 'psplit' \}, btn\(b\), caret\(b\)\)/, 'Allow y ▾ - always / this session in the ▾ (changes 9.50)');
  assert.match(main, /h\('details', \{ class: 'pdetails' \}, h\('summary', \{\}, 'Details'\), summaryPre\(p\.summary\)\)/, 'the whole text one click away');
  assert.match(main, /'Asks because '/);
  assert.match(main, /gone: state\.pending_gone \?\? \[\]/);
  assert.match(css, /\.pending \.pbtns \{/);
  assert.match(css, /\.pending\.dbpending \.dbc-h \{ display: none; \}/, 'the DB card under the same header');
});

// ---- F20: the audit / History as one row per action
const ts = (min, sec = 0) => `2026-10-20T23:${String(min).padStart(2, '0')}:${String(sec).padStart(2, '0')}+0200`;

test('F20 audit sentences: push, git, hidden read, http, card answers', () => {
  assert.equal(auditSentence({ event: 'git-run', ts: '', args: ['push', 'origin', 'HEAD:release'], rc: 0 }), 'Push HEAD → release');
  assert.equal(auditSentence({ event: 'git-run', ts: '', args: ['push', '--force', 'origin', 'main'], rc: 0 }), 'Push main (force)');
  assert.equal(auditSentence({ event: 'git-run', ts: '', args: ['show', '--stat', '4d890c0'], rc: 0 }), 'git show --stat 4d890c0');
  assert.equal(auditSentence({ event: 'git-run', ts: '', args: ['fetch'], rc: 1 }), 'git fetch - failed (rc 1)');
  assert.equal(auditSentence({ event: 'gui-read-hidden', ts: '', path: 'config/settings-env.properties' }), 'Hidden file read · settings-env.properties');
  assert.equal(auditSentence({ event: 'http-run', ts: '', file: 'ping.http', env: 'prod' }), 'HTTP ping.http on prod');
  assert.equal(auditSentence({ event: 'settings-approved', ts: '' }), 'Settings approved');
  assert.equal(auditSentence({ event: 'git-run', ts: '', args: ['log', '--grep=\u202eevil'] }).includes('\u202e'), false, 'bidi controls shown, not obeyed');
  assert.equal(confirmWord({ event: 'confirm', ts: '', approved: true, auto: true }), 'auto-allowed');
  assert.equal(confirmWord({ event: 'confirm', ts: '', approved: false, answered: true }), 'declined');
  assert.equal(confirmWord({ event: 'confirm', ts: '', approved: false, answered: false }), 'timed out');
  assert.equal(auditRisk({ event: 'git-run', ts: '', args: ['push'] }), 'push');
  assert.equal(auditRisk({ event: 'http-run', ts: '', cls: 'prod' }), 'prod');
  assert.equal(auditRisk({ event: 'git-run', ts: '', args: ['show'] }), '');
  assert.equal(auditIcon({ event: 'git-run', ts: '', args: ['show'] }), 'branch');
});

test('F20 audit merge: Tool call · Git · Confirmed of one call = one row; repeats = ×n with the span', () => {
  const ev = [   // newest first, as /api/audit returns them
    { event: 'tool', ts: ts(34, 34), session: 's', tool: 'git_run', ok: true },
    { event: 'git-run', ts: ts(34, 34), session: 's', project: 'p', args: ['push', 'origin', 'HEAD:x'], rc: 0 },
    { event: 'confirm', ts: ts(34, 33), session: 's', kind: 'git_run', approved: true, auto: true },
    { event: 'confirm', ts: ts(30), session: 's', kind: 'write', approved: false, answered: true },
    ...Array.from({ length: 10 }, (_, i) => ({ event: 'gui-read-hidden', ts: ts(25 - i * 2), project: 'p', path: 'a/b.properties' })),
    { event: 'tool', ts: ts(1, 0), session: 's', tool: 'git_run', ok: true },
    { event: 'tool', ts: ts(0, 59), session: 't', tool: 'git_run', ok: true },   // another session: never merged
  ];
  const rows = mergeAudit(ev);
  assert.equal(rows.length, 5);
  assert.equal(rows[0].e.event, 'git-run');
  assert.equal(rows[0].parts.length, 3);
  assert.equal(confirmWord(rows[0].confirm), 'auto-allowed');
  assert.equal(rows[1].e.event, 'confirm');   // a card alone (another tool) stays its own row
  assert.equal(rows[2].n, 10);
  assert.equal(rows[2].parts.length, 10);
  assert.equal(repeatText(rows[2].n, rows[2].first, rows[2].e.ts), '×10 in 18 min');
  assert.equal(rows[3].e.session, 's');
  assert.equal(rows[4].e.session, 't');
  // a push repeated with another answer is not the same action
  const two = mergeAudit([{ event: 'confirm', ts: ts(2), session: 's', kind: 'w', approved: true, answered: true },
    { event: 'confirm', ts: ts(1), session: 's', kind: 'w', approved: false, answered: true }]);
  assert.equal(two.length, 2);
  const today = new Date(2026, 9, 20, 23, 50);
  assert.equal(auditTime(new Date(2026, 9, 20, 23, 34).toISOString(), today), '23:34');
  const before = auditTime(new Date(2026, 9, 19, 23, 34).toISOString(), today);
  assert.ok(before.endsWith(' 23:34') && before !== '23:34', before);
});

test('F20 audit structure: rows, not the raw table; the raw records unfold; Actions / All stays', () => {
  const src = read('board.ts');
  assert.match(src, /class: 'arows', role: 'list'/);
  assert.match(src, /mergeAudit\(visible\)/);
  assert.match(src, /class: 'araw mono small'/);
  assert.match(src, /\[\[true, 'Actions', [^\]]*\], \[false, 'All', 'every record'\]\]/);
  assert.doesNotMatch(src, /h\('table', \{ class: 'plain' \}\)/);
});

test('F17: the card stack never reaches over the rail (390 px: ⋯, Settings and the bell stay clickable)', () => {
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const block = css.slice(css.indexOf('/* ==== 9.63 F17'), css.indexOf('/* ==== end F17 ==== */'));
  assert.match(block, /#pending \{ width: min\(460px, calc\(100vw - var\(--ab-w, 64px\) - 32px\)\); \}/);
});


test('viewing a session removes only its alerts, persists across windows, and permits later events', () => {
  let saved = null;
  const store = { get: () => saved, set: v => { saved = v; } };
  const a = new NoticeLog(store, () => 10);
  a.add('same title', 'desktop', undefined, 10, false, 'session-a');
  a.add('same title', 'desktop', undefined, 10, false, 'session-b');
  a.add('global');
  const b = new NoticeLog(store, () => 10);
  assert.equal(b.list().length, 3, 'identical text from different sessions must not merge');
  assert.equal(b.dismissSessions(['session-a']), true);
  assert.equal(a.unread, 2);
  assert.deepEqual(a.list().map(n => n.sessionId), [undefined, 'session-b']);
  assert.equal(a.dismissSessions(['session-a']), false);
  a.add('next turn', 'desktop', undefined, 20, false, 'session-a');
  assert.equal(b.unread, 3);
  b.dismissSessions(['session-a', 'session-b']);
  assert.deepEqual(new NoticeLog(store, () => 20).list().map(n => n.text), ['global']);
});


test('session alert acknowledgement requires a visible session and foreground document', () => {
  const source = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const body = source.slice(source.indexOf('function dismissViewedNotices()'), source.indexOf("window.addEventListener('focus', dismissViewedNotices)"));
  const log = new NoticeLog();
  const doc = { hidden: false, hasFocus: () => true };
  const state = { sessions: [{ id: 'a' }, { id: 'b' }, { id: 'advisor', kind: 'advisor' }] };
  const run = (view, grid = false, drawer = false) => new Function('document', 'state', 'view', 'active', 'grid', 'gridIdsShown', 'drawer', 'notices', 'bellSoon', body + '; dismissViewedNotices();')(doc, state, view, 'a', grid, () => ['b'], drawer, log, () => {});
  const seed = () => { for (const id of ['a', 'b', 'advisor']) log.add(id, 'desktop', undefined, undefined, false, id); };
  seed(); run('files'); assert.equal(log.unread, 3);
  doc.hidden = true; run('term'); assert.equal(log.unread, 3);
  doc.hidden = false; doc.hasFocus = () => false; run('term'); assert.equal(log.unread, 3);
  doc.hasFocus = () => true; run('term', true);
  assert.deepEqual(log.list().map(n => n.sessionId), ['advisor', 'a'], 'zoom/grid acknowledges only shown panes');
  run('convo'); assert.deepEqual(log.list().map(n => n.sessionId), ['advisor']);
  run('files', false, true); assert.equal(log.unread, 0);
  seed(); run('term'); assert.equal(log.list().some(n => n.sessionId === 'a'), false);
});
