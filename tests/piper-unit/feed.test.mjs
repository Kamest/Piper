// node --test: the Feed's pure helpers, from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { bubbles, matches, refFor, sinceLine, timeline, badge, KIND_TEXT, DANGER_LABEL, chatSuggestions, cardLine, startedState, agentStatus } from '../../piper/web/dist/feed.js';

const ev = (key, ts, extra = {}) => ({ id: 'e' + key + ts, key, kind: 'event', source: 'session', title: key, body: '',
  subject: { type: 'session', key: 's1', label: 'shop' }, ts, n: 1, ...extra });

test('filters: For me = only actions, Agent = only the chat, GitLab / Sessions by source', () => {
  assert.equal(matches({ kind: 'action', source: 'gitlab' }, 'me'), true);
  assert.equal(matches({ kind: 'event', source: 'gitlab' }, 'me'), false);
  assert.equal(matches({ kind: 'chat', source: 'agent' }, 'agent'), true);
  assert.equal(matches({ kind: 'event', source: 'session' }, 'agent'), false);
  assert.equal(matches({ kind: 'event', source: 'gitlab' }, 'gitlab'), true);
  assert.equal(matches({ kind: 'event', source: 'ci' }, 'sessions'), true);
  assert.equal(matches({ kind: 'event', source: 'gitlab' }, 'sessions'), false);
});

test('a hand-over is a reference, never the notification text', () => {
  assert.equal(refFor({ type: 'issue', key: 'acme/shop#issue#412', label: 'shop #412' }), '(ref: gitlab acme/shop#412)');
  assert.equal(refFor({ type: 'mr', key: 'acme/shop#mr#9', label: '' }), '(ref: gitlab acme/shop!9)');
  assert.equal(refFor({ type: 'session', key: 'ab12cd34', label: 'shop · 2' }), '(ref: session ab12cd34 "shop · 2")');
  assert.equal(refFor({ type: 'project', key: 'shop', label: 'shop' }), '(ref: project shop)');
});

test('the transcript as bubbles: tool-only turns fold into one grey row, the queue follows', () => {
  const b = bubbles({
    on: true,
    turns: [
      { role: 'user', text: 'Status?', ts: '2026-09-29T10:00:00Z', chat: ['m1'], from_terminal: false },
      { role: 'assistant', text: '[tool list_sessions]', ts: '2026-09-29T10:00:01Z', chat: [], from_terminal: false },
      { role: 'assistant', text: '[tool session_digest]\n[tool list_sessions]', ts: null, chat: [], from_terminal: false },
      { role: 'assistant', text: '[tool x]\nTwo are working.', ts: '2026-09-29T10:00:05Z', chat: [], from_terminal: false },
      { role: 'user', text: 'by hand', ts: '2026-09-29T10:01:00Z', chat: [], from_terminal: true },
    ],
    queue: [{ id: 'm2', text: 'And web?', ts: 1790000000, state: 'queued' }],
  });
  assert.deepEqual(b.map((x) => [x.who, x.text]), [
    ['you', 'Status?'], ['tools', 'list_sessions, session_digest'], ['agent', 'Two are working.'], ['you', 'by hand'], ['queued', 'And web?']]);
  assert.equal(b[3].terminal, true);
  assert.equal(b[2].ts, Date.parse('2026-09-29T10:00:05Z') / 1000);
});

test('timeline: day separators, one "new" line after seen, chat only under All / Agent', () => {
  const now = Date.parse('2026-09-29T12:00:00') / 1000;
  const y = now - 86400;
  const events = [ev('turn:a', y), ev('turn:b', now - 60), ev('exit:c', now - 30)];
  const chat = [{ kind: 'chat', source: 'agent', who: 'agent', text: 'hi', ts: now - 45 }];
  const rows = timeline(events, chat, now - 50, 'all', now);
  assert.deepEqual(rows.map((r) => r.type === 'day' ? r.label : r.type === 'event' ? r.e.key : r.type), [
    'Yesterday', 'turn:a', 'Today', 'turn:b', 'chat', 'new', 'exit:c']);
  assert.deepEqual(timeline(events, chat, 0, 'agent', now).map((r) => r.type), ['day', 'chat']);
  assert.equal(timeline(events, chat, 0, 'gitlab', now).length, 0);
});

test('since line counts unread events by kind (a folded event by its count)', () => {
  const seen = Date.parse('2026-09-29T14:05:00') / 1000;
  const line = sinceLine([ev('turn:a', seen + 10, { n: 3 }), ev('done:p:1', seen + 20), ev('turn:b', seen - 5)], seen);
  assert.equal(line, 'Since 14:05: 3× turn, 1× resolved');
  assert.equal(sinceLine([ev('turn:a', seen - 1)], seen), '');
  assert.equal(sinceLine([ev('turn:a', 5)], 0), '');
});

test('badge only when on and something is open', () => {
  assert.equal(badge({ on: false }), '');
  assert.equal(badge({ on: true, open: 0 }), '');
  assert.equal(badge({ on: true, open: 2 }), '2');
});

test('G8: the floating card says a kind in the words of the queue row (feed.py KIND_TEXT), gitlab_note included', () => {
  const py = readFileSync(new URL('../../piper/feed.py', import.meta.url), 'utf8');
  const block = py.slice(py.indexOf('KIND_TEXT = {'), py.indexOf('}', py.indexOf('KIND_TEXT = {')) + 1);
  const pairs = Object.fromEntries([...block.matchAll(/"([a-z_]+)": "((?:[^"\\]|\\.)*)"/g)].map((m) => [m[1], m[2].replace(/\\'/g, "'")]));
  assert.ok(Object.keys(pairs).length > 20, 'parsed feed.py KIND_TEXT');
  assert.deepEqual(KIND_TEXT, pairs);
  assert.equal(KIND_TEXT.gitlab_note, 'wants to post a GitLab comment');
  assert.equal(KIND_TEXT.properties_set, 'wants to update properties');
  assert.equal(KIND_TEXT.http_env_set, 'wants to update HTTP environment values');
});

test('N3: every feed.py DANGER_KINDS kind has a red Allow label; a job on a protected branch is one of them', () => {
  const py = readFileSync(new URL('../../piper/feed.py', import.meta.url), 'utf8');
  const kinds = [...py.match(/DANGER_KINDS = \{([^}]*)\}/)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
  assert.ok(kinds.length >= 5, 'parsed feed.py DANGER_KINDS');
  for (const k of kinds) assert.ok(DANGER_LABEL[k], `no red label for ${k}`);
  assert.equal(DANGER_LABEL.gitlab_job_protected, 'Allow on protected branch');
});

test('N3: the queue row is red for the same kinds as the floating card (feed.py DANGER_KINDS ⊇ GUI)', () => {
  const py = readFileSync(new URL('../../piper/feed.py', import.meta.url), 'utf8');
  const kinds = new Set([...py.match(/DANGER_KINDS = \{([^}]*)\}/)[1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]));
  for (const k of Object.keys(DANGER_LABEL)) assert.ok(kinds.has(k), `feed.py DANGER_KINDS lacks ${k}`);
  // a weakening settings_change: feed.py _danger (test_feed.py test_every_client_gets_the_red_allow_of_a_dangerous_card)
});

test('N3: texts for a person name no agent tool (unlock rows, automation note)', () => {
  const js = ['main.js', 'inbox.js', 'dbview.js'].map((f) => readFileSync(new URL(`../../piper/web/dist/${f}`, import.meta.url), 'utf8')).join('\n');
  for (const bad of ['(list_dir / read_file / grep)', '(edit_file / write_file)', 'db_execute / db_script may', '(git_run) -',
    ' git_run runs ordinary', 'the gitlab_issue tool scoped',
    'through its db_* tools', 'Claude uses through db_*']) assert.ok(!js.includes(bad), bad);
});

test('N4: the Feed drawer Allow of a dangerous card is red with the card\'s words (never y / Enter); a 404 answer = decided elsewhere', async () => {
  const { dangerAllow, decidedElsewhere } = await import('../../piper/web/dist/feed.js');
  const { ApiError } = await import('../../piper/web/dist/api.js');
  const allow = (args) => ({ op: 'decide', label: 'Allow', args: { id: 'p1', ok: true, ...args } });
  assert.equal(dangerAllow(allow({ danger: 'gitlab_job_protected' })), 'Allow on protected branch');
  assert.equal(dangerAllow(allow({ danger: 'settings_change' })), 'Allow weakening');
  assert.equal(dangerAllow(allow({ danger: 'http_prod' })), 'Allow on PROD');
  assert.equal(dangerAllow(allow({})), '');
  assert.equal(dangerAllow({ op: 'decide', label: 'Deny', args: { id: 'p1', ok: false, danger: 'http_prod' } }), '');
  assert.equal(decidedElsewhere(new ApiError(404, 'unknown pending')), true);
  assert.equal(decidedElsewhere(new ApiError(500, 'x')), false);
  assert.equal(decidedElsewhere(new Error('x')), false);
});

test('N8 (B-N6-1): a git_run that takes a hidden file into git is red everywhere with a truthful word - card, queue row, Feed drawer', async () => {
  const { cardDanger, dangerAllow, dangerWord, warnLines } = await import('../../piper/web/dist/feed.js');
  const { allowDanger } = await import('../../piper/web/dist/queue.js');
  // the floating card: payload.danger (sessions._git_run), not the kind alone
  assert.equal(cardDanger({ kind: 'git_run', payload: { args: ['commit', '-am', 'x'], danger: true } }), 'Allow, hidden file');
  assert.equal(cardDanger({ kind: 'git_run_project', payload: { danger: true } }), 'Allow, hidden file');
  assert.equal(cardDanger({ kind: 'git_run', payload: { args: ['status'] } }), '');
  assert.equal(cardDanger({ kind: 'git_run', payload: { danger: 'yes' } }), '');   // feed.py: `is True`
  assert.equal(cardDanger({ kind: 'git_run' }), '');
  assert.equal(cardDanger({ kind: 'http_prod' }), 'Allow on PROD');
  assert.equal(cardDanger({ kind: 'settings_change', payload: { weakens: true } }), 'Allow weakening');
  assert.equal(cardDanger({ kind: 'settings_change', payload: {} }), '');
  assert.equal(cardDanger({ kind: 'edit_file', payload: { danger: true } }), '');
  // feed.py args.danger = the kind: the Feed drawer and the queue row say the same word, never "on PROD"
  const allow = (danger) => ({ op: 'decide', label: 'Allow', args: { id: 'p1', ok: true, danger } });
  for (const k of ['git_run', 'git_run_project']) {
    assert.equal(dangerAllow(allow(k)), 'Allow, hidden file');
    assert.equal(dangerWord(k), 'Allow, hidden file');
    const row = { level: 'confirm', actions: [allow(k), { op: 'decide', label: 'Deny', args: { id: 'p1', ok: false } }] };
    assert.equal(allowDanger(row), 'Allow, hidden file');
    assert.equal(allowDanger(row, () => ''), 'Allow, hidden file');
  }
  // the card's own rule alone (an older piperd without args.danger) still makes the row red
  const plain = { level: 'confirm', actions: [{ op: 'decide', label: 'Allow', args: { id: 'p1', ok: true } }] };
  assert.equal(allowDanger(plain, (id) => cardDanger(id === 'p1' ? { kind: 'git_run', payload: { danger: true } } : { kind: 'x' })), 'Allow, hidden file');
  assert.equal(dangerWord('something_new'), 'Allow on PROD');   // an unknown mark stays red
  // a folded card keeps the reason: the ⚠ lines of the summary (first after the command)
  const summary = 'git commit -am x\n⚠ takes the REAL content of a hidden file into git (secret.env) - the sandbox reads .git, a push ships it\n⚠ other\n\nin /p (main)';
  assert.deepEqual(warnLines(summary), [summary.split('\n')[1], '⚠ other']);
  assert.deepEqual(warnLines('git status\n\nin /p'), []);
});

test('N8: the floating card takes its red rule from feed.ts (main.ts dangerLabel = cardDanger) and keeps its ⚠ lines visible', () => {
  const src = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(src, /function dangerLabel\(p: Pending\): string \{\s*return cardDanger\(p\);/);
  assert.match(src, /\.\.\.m\.warns\.map\(\(w\) => h\('div', \{ class: 'pwarn' \}/, 'F17: the ⚠ lines stay visible, the rest under Details');
});

test('N9: an allow_domain of a name that reaches this machine (payload.danger, feed.py _danger) is red everywhere with its own word', async () => {
  const { cardDanger, dangerWord, dangerAllow } = await import('../../piper/web/dist/feed.js');
  assert.equal(cardDanger({ kind: 'allow_domain', payload: { domain: 'host.docker.internal', danger: true } }), 'Allow, this machine');
  assert.equal(cardDanger({ kind: 'allow_domain', payload: { domain: 'api.example.com' } }), '');
  assert.equal(cardDanger({ kind: 'allow_domain', payload: { domain: 'x', danger: 'yes' } }), '');
  assert.equal(dangerWord('allow_domain'), 'Allow, this machine', 'not the fallback "Allow on PROD"');
  assert.equal(dangerAllow({ op: 'decide', label: 'Allow', args: { id: 'x', ok: true, danger: 'allow_domain' } }), 'Allow, this machine');
});

// F21 (9.76): the Global agent's chat - suggestions, sessions it started, its waiting card, its state once
test('F21 suggestions come from real counts, at most three, plain fillers otherwise', () => {
  assert.deepEqual(chatSuggestions({ assigned: 4, waiting: 2, review: 3 }),
    ['Plan my 4 assigned issues', 'What is waiting for me? (2 sessions)', 'Start sessions for the 3 MRs to review']);
  assert.deepEqual(chatSuggestions({ assigned: 1, waiting: 1, review: 0 }),
    ['Plan my assigned issue', 'What is waiting for me? (1 session)', 'What did my sessions do today?']);
  assert.deepEqual(chatSuggestions(undefined),
    ['What is waiting for me?', 'What did my sessions do today?', 'Which of my issues is due soon?']);
});

test('F21 card line, started-session states and the agent state', () => {
  assert.equal(cardLine({ kind: 'spawn_session', project: 'demo/plain' }), 'Asked to start a session in demo/plain - card is in the queue (⌘J)');
  assert.equal(cardLine({ kind: 'git_push', project: '' }), 'Waits for your answer (git push) - card is in the queue (⌘J)');
  assert.deepEqual(startedState('exited'), { word: 'done', tone: 'ok' });
  assert.deepEqual(startedState('attention'), { word: 'needs you', tone: 'warn' });
  assert.deepEqual(startedState('error'), { word: 'error', tone: 'bad' });
  assert.deepEqual(startedState('ready'), { word: 'ready', tone: 'run' });
  assert.deepEqual(agentStatus({ on: true, agent: { id: 'a', state: 'ready', title: '', hold: '' } }), { live: true, text: 'ready' });
  assert.deepEqual(agentStatus({ on: true, agent: { id: 'a', state: 'exited', title: '', hold: '' } }), { live: false, text: 'not running' });
  assert.deepEqual(agentStatus({ on: true, agent: null }), { live: false, text: 'not running' });
});

test('F21 structure: no "→ Global agent" chip, keys as chips under the field, Send primary, started + cards drawn', () => {
  const src = readFileSync(new URL('../../piper/web/src/feed.ts', import.meta.url), 'utf8');
  assert.ok(!src.includes('feed-to-chip') && !src.includes('→ Global agent'), 'the target chip is gone');
  assert.match(src, /placeholder: 'Ask the agent…'/);
  assert.match(src, /class: 'feed-keys/);
  assert.match(src, /class: 'primary small feed-send'/);
  assert.match(src, /'Started by the agent'/);
  assert.match(src, /function drawCards\(\)/);
  assert.match(src, /status: \(\) => agentStatus\(chat\)/);
  assert.equal(src.split("isSend(e, 'chat')").length - 1, 2, 'W13 wiring keeps two send checks');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const blk = css.slice(css.indexOf('9.76 F21'), css.indexOf('end F21'));
  assert.ok(blk.includes('.feed-started') && blk.includes('.feed-sugg') && blk.includes('.feed-cardline'), 'F21 css block');
  assert.ok(!/font-size:\s*\d+px/.test(blk) && !/border-radius:\s*\d+px/.test(blk), 'scale tokens only');
});

test('F21 wiring: the drawer head says the state once - "Agent ● ready", redrawn with every render', () => {
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /h\('div', \{ class: 'drawer-head' \}, h\('h2', \{\}, 'Agent'\), drawerState,/);
  assert.match(main, /const st = feed\.status\(\);\n\s+drawerState\.className = 'dr-state' \+ \(st\.live \? ' live' : ''\);\n\s+drawerState\.textContent = markBidi\(st\.text\);/);
  assert.match(main, /renderPending\(\);\n\s+if \(drawer\) drawDrawerState\(\);/);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.drawer-head \.dr-state\.live::before \{ background: var\(--ok-dot\); \}/);
});

test('F21: the drawer is the chat only - no "Global agent opened session" rows, no toast for an agent-started session', () => {
  const src = (f) => readFileSync(new URL(`../../piper/web/src/${f}`, import.meta.url), 'utf8');
  assert.match(src('feed.ts'), /const rows = timeline\(deps\.chatOnly \? \[\] : data\.events \?\? \[\], bubbles\(chat\)/,
    'the events stay in the queue');
  assert.doesNotMatch(src('main.ts'), /Global agent started session/, 'the toast is gone');
});
