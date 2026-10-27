import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

class Text { constructor(data) { this.data = data; } get textContent() { return this.data; } }
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this.className = ''; }
  append(...nodes) { this.children.push(...nodes.map(n => typeof n === 'string' ? new Text(n) : n)); }
  insertBefore(node, before) {
    const i = before ? this.children.indexOf(before) : this.children.length;
    this.children.splice(i, 0, node);
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  get textContent() { return this.children.map(c => c.textContent).join(''); }
}
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => new Text(text) };
const { evidenceBox } = await import('../../piper/web/dist/evidence.js');
const walk = n => [n, ...(n.children ?? []).flatMap(walk)];
const byTag = (n, tag) => walk(n).filter(e => e.tagName === tag);

test('evidence rows, chips, checked time and a folded View diff use real DOM text nodes', () => {
  const e = evidenceBox({ title: 'What it changes', state: 'ready', checked_at: 1234567890,
    rows: [{ text: 'origin → new branch feature/a', chips: ['NOT PROTECTED'] }, { text: 'abc123 Change', mono: true }],
    diff: '+<script>unsafe()</script>\n-password=[scrubbed]', notes: ['Counted before you answered; may differ.'] });
  assert.equal(e.attrs['aria-label'], 'What it changes');
  assert.equal(byTag(e, 'time').length, 1);
  assert.match(e.textContent, /checked/);
  assert.match(e.textContent, /NOT PROTECTED/);
  assert.match(e.textContent, /View diff/);
  assert.equal(byTag(e, 'details').length, 1);
  assert.equal(byTag(e, 'script').length, 0);
  assert.ok(e.textContent.includes('<script>unsafe()</script>'));
  assert.ok(walk(e).some(n => n.className === 'add'));
});

test('test/dev preview shows at most three before → after rows, headers and remaining count', () => {
  const e = evidenceBox({ title: 'What it changes', state: 'ready', rows: [],
    preview: { columns: ['id', 'status'], rows: [1, 2, 3, 4].map(id => [{ v: id }, { v: 'trial', to: 'expired' }]), more: 34 } });
  assert.equal(byTag(e, 'tbody')[0].children.length, 3);
  assert.equal(byTag(e, 'th').length, 2);
  assert.match(e.textContent, /trial → expired/);
  assert.match(e.textContent, /34 more rows/);
});

test('push diffstat is below commits and above checks as on the approved board', () => {
  const e = evidenceBox({ title: 'What it changes', state: 'ready',
    rows: [{ code: 'abc123', text: 'Change' }, { text: 'No secrets detected', tone: 'ok' }],
    diffstat: { files: 2, added: 10, removed: 3 }, diff: '+new' });
  assert.ok(e.textContent.indexOf('abc123') < e.textContent.indexOf('View diff'));
  assert.ok(e.textContent.indexOf('View diff') < e.textContent.indexOf('No secrets detected'));
});

test('commit hash and subject share one non-wrapping row, with only the subject ellipsized', () => {
  const subject = 'A long commit subject '.repeat(20);
  const e = evidenceBox({ title: 'What it changes', state: 'ready', rows: [{ code: 'abc1234', text: subject }] });
  const code = byTag(e, 'code')[0];
  const row = walk(e).find(n => n.children?.includes(code));
  assert.match(row.className, /pe-commit/);
  assert.equal(row.children[1].className, 'pe-subject');
  assert.equal(row.children[1].textContent, subject);
  assert.equal(row.children[1].attrs.title, subject);
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.pe-commit \{[^}]*flex-wrap: nowrap;[^}]*min-width: 0;/);
  assert.match(css, /\.pe-commit > \.pe-code \{ flex-shrink: 0;/);
  assert.match(css, /\.pe-subject \{[^}]*min-width: 0;[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/);
});

test('checking, unavailable, error and a missing payload keep the evidence box visible', () => {
  for (const state of ['checking', 'unavailable', 'error']) {
    const e = evidenceBox({ title: 'What it reads', state, rows: [{ text: state === 'checking' ? 'checking…' : 'Could not check: timed out' }] });
    assert.equal(e.tagName, 'section');
    assert.match(e.className, new RegExp(state));
    assert.equal(e.attrs['aria-live'], 'polite');
    assert.ok(e.textContent.length > 0);
  }
  assert.match(evidenceBox().textContent, /Could not check: evidence unavailable/);
});

test('cross-project diff is inline; production columns are PERSONAL chips without a value table', () => {
  const edit = evidenceBox({ title: 'What it changes', state: 'ready', rows: [{ text: 'Project acme/shop · branch main · 2 sessions use it' }], diff: '-before\n+after', inline_diff: true });
  assert.equal(byTag(edit, 'details').length, 0);
  assert.equal(byTag(edit, 'pre').length, 1);
  const prod = evidenceBox({ title: 'What it reads', state: 'ready', rows: [{ text: 'email', chips: ['PRODUCTION', 'PERSONAL'] }] });
  assert.equal(byTag(prod, 'table').length, 0);
  assert.match(prod.textContent, /PERSONAL/);
});

test('card places evidence before the rule; scoped grants and suggestions only open the exact rule', () => {
  const main = readFileSync(new URL('../../piper/web/src/main.ts', import.meta.url), 'utf8');
  assert.match(main, /evidenceBox\(p\.payload\?\.evidence[^]*?details, why/);
  for (const text of ['Allow once', 'Allow similar for 1 hour', 'Change the rule…', 'Revoke']) assert.ok(main.includes(text));
  // the session grant keeps its environment-specific label and id ("Allow for this session on PROD", gui.py session_cards)
  assert.match(main, /role === 'session'\)\.map\(x => \(\{ id: x\.label, label: menuLabel\(x\.label,/);
  assert.match(main, /settings\.showRule\(p\.kind\)/);
  // a DB write card with evidence still shows NO WHERE / full-scan warnings and the agent's reason
  assert.match(main, /: db \? h\('div', \{ class: 'pbody' \},\s*\.\.\.db\.warnings\.filter[^]*?db\.reason/);
  assert.match(main, /p\.suggestion\.link/);
  assert.match(main, /box\.dataset\.evidence !== evidenceKey[^]*?\.replaceWith\(evidenceBox/,
    'late facts replace only the evidence box, keeping the decision buttons stable');
  const settings = readFileSync(new URL('../../piper/web/src/settings.ts', import.meta.url), 'utf8');
  const rule = settings.slice(settings.indexOf('showRule(kind:'), settings.indexOf('/** section: open it;'));
  assert.match(rule, /const id = 'ask\.' \+ kind;[\s\S]*secFocus = id;/);
  assert.doesNotMatch(rule, /setVal|\.save\(|api\./);
});
