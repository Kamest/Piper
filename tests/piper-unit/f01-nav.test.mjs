// node --test: F01 navigation - Blocked / Your turn, "project — sentence" rows, the sessions in the rail.
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { agoAt, buildQueue, isTurn, monogram, projectMonogram, rowTitle } from '../../piper/web/dist/queue.js';
import { kbd } from '../../piper/web/dist/clipkeys.js';

const SRC = new URL('../../piper/web/src/', import.meta.url);
const read = (f) => readFileSync(new URL(f, SRC), 'utf8');
const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
const NOW = 1_800_000_000;
const sess = (id, extra = {}) => ({ id, kind: 'project', project: 'java/acme-app', title: 'java/acme-app', task: '', state: 'working', detail: '',
  created: NOW - 3600, since: NOW - 300, hidden: false, watch: [], ...extra });
const act = (key, level, sid, extra = {}) => ({ key, kind: 'action', level, source: 'session', title: key, body: '', quote: null,
  subject: { type: 'session', key: sid, session: sid, label: sid }, since: NOW - 300, actions: [], ...extra });
const input = (over = {}) => ({ sessions: [], actions: [], events: [], seen: 0, board: new Map(), unseen: new Set(), pins: new Set(), order: [],
  label: (s) => s, now: NOW, ...over });

test('F01 monogram: two letters of the project - the first letters of two words, else its first two', () => {
  assert.deepEqual(['acme-app', 'website', 'shop', 'bakery', 'acme_cms', '2024', 'x'].map(monogram), ['AA', 'WE', 'SH', 'BA', 'AC', '?', 'X']);
});

test('9.144 rail monogram: projects sharing a last segment take their parent folder (globex/eshop GL, initech/eshop IN)', () => {
  const keys = ['globex/eshop', 'initech/eshop', 'java/acme-app', 'eshop'];
  assert.deepEqual(['globex/eshop', 'initech/eshop'].map((k) => projectMonogram(k, keys)), ['GL', 'IN']);
  assert.equal(projectMonogram('java/acme-app', keys), 'AA', 'a unique name keeps its own letters');
  assert.equal(projectMonogram('eshop', keys), 'ES', 'no parent folder to tell it apart');
  assert.equal(projectMonogram('globex/eshop', ['globex/eshop']), 'ES', 'alone it stays ES');
});

test('F01 Needs you splits: a card, a permission, a failed start block; a finished turn is Your turn', () => {
  const q = buildQueue(input({
    sessions: [sess('a'), sess('b', { project: 'acme/website', title: 'acme/website', state: 'waiting' }), sess('c', { state: 'attention' })],
    board: new Map([['b', { last: 'Done.\nShall I delete the old endpoint?' }]]),
    actions: [act('p:1', 'confirm', 'a', { source: 'confirm', body: 'git push origin 412-fix\n⚠ protected' }), act('w:b', 'waiting', 'b'), act('perm:c', 'permission', 'c')],
  }));
  assert.deepEqual(q.needs.filter((x) => !isTurn(x)).map((x) => x.key), ['a:p:1', 'a:perm:c']);
  assert.deepEqual(q.needs.filter(isTurn).map((x) => x.key), ['a:w:b']);
  const [card, turn] = [q.needs.find((x) => x.key === 'a:p:1'), q.needs.find((x) => x.key === 'a:w:b')];
  assert.equal(card.title, 'java/acme-app — git push origin 412-fix');
  assert.equal(card.status, `asks 5 min · ${kbd('Allow / Deny ⌘J')}`, 'the key in this OS\'s words (⌘J on a Mac, Alt+J on Linux CI)');
  assert.equal(turn.title, 'acme/website — Shall I delete the old endpoint?');
  assert.equal(turn.status, 'question 5 min');
  assert.deepEqual([card.glyph, turn.glyph], ['stuck', 'wait'], 'Blocked red, Your turn amber');
});

test('F01 rowTitle: project — sentence, else the issue, an own title, the prompt, the branch, else the name', () => {
  assert.equal(rowTitle(sess('s'), 'Tests pass.'), 'java/acme-app — Tests pass.');
  assert.equal(rowTitle(sess('s', { issue: { iid: 7, type: 'mr', title: 'VAT' } })), 'java/acme-app — !7 VAT');
  assert.equal(rowTitle(sess('s', { title: 'java/acme-app · 2', last_prompt: 'Fix the feed' })), 'java/acme-app — Fix the feed');
  assert.equal(rowTitle(sess('s', { title: 'Import feed' })), 'java/acme-app — Import feed');
  assert.equal(rowTitle(sess('s', { branch: '412-fix' })), 'java/acme-app — 412-fix');
  assert.equal(rowTitle(sess('s')), 'java/acme-app');
  assert.equal(rowTitle(sess('g', { kind: 'advisor', title: 'x' })), 'Global agent', 'the Global agent: its sentence only');
  assert.deepEqual([agoAt(NOW - 30, NOW), agoAt(NOW - 300, NOW), agoAt(NOW - 7200, NOW)], ['now', '5 min', '2 h']);
});

test('F01 the queue renders Blocked and Your turn, Ended as one line that opens Find & resume', () => {
  const q = read('queue.ts');
  const body = q.slice(q.indexOf('function body('), q.indexOf('function item('));
  assert.match(body, /secHead\('Blocked', blocked\.length/);
  assert.match(body, /secHead\('Your turn', turn\.length/);
  assert.match(body, /`\$\{sec\.ended\.length\} ended`/);
  assert.match(body, /'Find & resume'\)/);
  assert.match(body, /onclick: \(\) => deps\.resumeConversations\?\.\(\) \}, 'Find & resume'/);
  assert.doesNotMatch(body, /'q-sec needs turn'/, 'never the conversation\'s .turn class');
});

test('F01 the rail: Blocked-only red badge, the session tiles under the pinned tools, folded = the rail alone', () => {
  const m = read('main.ts');
  const bar = m.slice(m.indexOf('function renderActbar('), m.indexOf('function hostItem('));
  assert.match(bar, /queue\.needs\(\)\.filter\(\(x\) => !isTurn\(x\)\)\.length/);
  assert.match(bar, /badge\(needs, 'bad'/);
  assert.match(bar, /\.\.\.pinned,[^]*?qmini,[^]*?h\('div', \{ class: 'grow' \}\)/);
  assert.match(bar, /queueShown\(\) && !stripOn\(\) \? null : limits\.tile/, 'the limits by the bell only while the bars are folded away');
  assert.doesNotMatch(m, /queue\.el\.after\(qmini\)/);
  assert.doesNotMatch(m, /function fitStrip/);
  assert.match(css, /body\.queue-mini \{ --q-w: 0px; \}/);
  assert.match(css, /\.ab-sess-sep:has\(\+ #qmini\[hidden\]\) \{ display: none; \}/);
});

test('F09 wiring (9.66): the session header offers HTTP only through httpTabWanted, asked per project', () => {
  const m = read('main.ts');
  assert.match(m, /proj && httpTab\(s\.project\) && shown\('http'\) \? tab\('http'/);
  assert.match(m, /return httpTabWanted\(httpFilesOf\.get\(project\)!\.has, on\('http'\), view === 'http'\)/);
  assert.match(m, /httpApi\.files\(project\)/);
});

test('F01: a Blocked row of the Global agent drops "all projects" like a project chip; its hover buttons float, no empty line', async () => {
  const { rowChips } = await import('../../piper/web/dist/queue.js');
  const chips = rowChips({ id: 'g', kind: 'advisor', project: '', watch: [] }, undefined);
  assert.deepEqual(chips.map((c) => [c.label, c.proj]), [['all projects', true]]);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const block = css.slice(css.indexOf('/* ==== 9.64 F01'), css.indexOf('/* ==== end F01 ==== */'));
  assert.match(block, /\.qi\.q2\.ra-row \.qi-ops\.row-actions \{ position: absolute;/);
  assert.match(block, /\.qi\.q2\.ra-row \.qi-chips:not\(:has\(\.qi-meta\)\) \{ margin-top: 0; \}/);
});

test('F01 / N7: only the rail\'s sessions scroll - the tools above them stay in place', () => {
  const block = css.slice(css.indexOf('/* ==== 9.64 F01'), css.indexOf('/* ==== end F01 ==== */'));
  assert.match(block, /#qmini \{ flex: 0 1 auto; min-height: 0; overflow-y: auto; align-self: stretch;/);
  assert.doesNotMatch(css, /\.qm-(open|count|more|new)\b/, 'the old strip\'s rules are gone');
  assert.match(read('main.ts'), /qmini\.querySelector<HTMLElement>\('button\.qm\.on'\)\?\.scrollIntoView\(\{ block: 'nearest' \}\);/);
});
