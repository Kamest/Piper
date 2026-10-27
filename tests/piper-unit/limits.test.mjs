// node --test: the Claude limits block's pure helpers (limits.ts), from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { countdown, createLimits, detailNodes, rolledOver, tone, weekRow, weekText } from '../../piper/web/dist/limits.js';

const NOW = 1_800_000_000;

test('tone: link below 75 %, amber to 99 %, red at 100 % or when the API flags it', () => {
  assert.equal(tone(4), 'ok');
  assert.equal(tone(74), 'ok');
  assert.equal(tone(75), 'warn');
  assert.equal(tone(99), 'warn');
  assert.equal(tone(100), 'bad');
  assert.equal(tone(120), 'bad');
  assert.equal(tone(10, 'warning'), 'warn');
  assert.equal(tone(null), 'ok');
});

test('countdown to a reset, and "resetting…" once it passed', () => {
  assert.equal(countdown(NOW + 3 * 3600 + 41 * 60 + 5, NOW), 'in 3h 41m');
  assert.equal(countdown(NOW + 42 * 60 + 59, NOW), 'in 42m');
  assert.equal(countdown(NOW + 30, NOW), 'in 30s');
  assert.equal(countdown(NOW + 33 * 3600, NOW), 'in 1d 9h');
  assert.equal(countdown(NOW, NOW), 'resetting…');
  assert.equal(countdown(NOW - 5, NOW), 'resetting…');
  assert.equal(countdown(null, NOW), '');
});

test('the weekly reset reads weekday + local time · distance', () => {
  const at = NOW + 33 * 3600 + 20 * 60;
  const d = new Date(at * 1000);
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const day = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
  assert.equal(weekText(at, NOW), `${day} ${hhmm} · 1d 9h`);
  assert.equal(weekText(NOW + 2 * 3600 + 5 * 60, NOW).endsWith('· 2h 05m'), true);
  assert.equal(weekText(NOW - 1, NOW), 'resetting…');
});

test('the Week row always uses the all-model limit, reset and severity; scoped limits stay for the tooltip', () => {
  const s = { v: 1, status: 'ok', fetched_at: NOW, session: { percent: 4, resets_at: NOW + 100, severity: 'normal' },
    week: { percent: 53, resets_at: NOW + 1000, severity: 'normal' },
    scoped: [{ label: 'Fable', percent: 90, resets_at: NOW + 2000, severity: 'warning' }] };
  const w = weekRow(s);
  assert.equal(w.percent, 53);
  assert.equal(w.resetsAt, NOW + 1000);
  assert.equal(w.severity, 'normal');
  assert.equal(w.all, 53);
  assert.deepEqual(w.scoped, [{ label: 'Fable', percent: 90 }]);
  assert.equal(weekRow({ ...s, scoped: [] }).percent, 53);
  const missing = weekRow({ ...s, week: null });
  assert.equal(missing.percent, null);
  assert.equal(missing.resetsAt, null);
  assert.equal(missing.severity, 'normal');
  assert.equal(weekRow({ ...s, week: { ...s.week, percent: null } }).percent, null);
  assert.equal(weekRow({ ...s, week: { ...s.week, percent: 0 } }).percent, 0);
  assert.equal(weekRow({ ...s, week: null, scoped: [] }).percent, null);
  assert.equal(weekRow({ ...s, scoped: [{ label: 'Opus', percent: 10, resets_at: NOW + 1000, severity: 'normal' }] }).percent, 53);
});

test('detects a window that rolled over since piperd last asked', () => {
  assert.equal(rolledOver(NOW - 10, NOW - 100, NOW), true);
  assert.equal(rolledOver(NOW - 10, NOW - 5, NOW), false);
  assert.equal(rolledOver(NOW + 10, NOW - 100, NOW), false);
  assert.equal(rolledOver(null, NOW - 100, NOW), false);
});

class TextNode {
  constructor(text) { this.textContent = text; }
}
class Element {
  constructor(tag) {
    this.tagName = tag; this.childNodes = []; this.attrs = {}; this.dataset = {}; this.style = {}; this.listeners = {};
    this.classList = { toggle: (name, on) => { this[name] = on; } };
    this.isConnected = true;
  }
  append(...children) { this.childNodes.push(...children); }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  contains(node) { return node === this || this.childNodes.some(c => c.contains?.(node)); }
  getClientRects() { return this.hidden ? [] : [{}]; }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  set textContent(text) { this.childNodes = [new TextNode(text)]; }
}
const documentEvents = {};
let touch = false;
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => new TextNode(text),
  addEventListener: (name, fn) => { documentEvents[name] = fn; }, removeEventListener: name => { delete documentEvents[name]; } };
globalThis.window = { setInterval() {}, addEventListener() {}, removeEventListener() {}, matchMedia: () => ({ matches: touch }) };
const walk = el => [el, ...(el.childNodes ?? []).flatMap(walk)];
const byClass = (el, cls) => walk(el).filter(n => (n.className ?? '').split(' ').includes(cls));
const sample = () => ({ v: 1, status: 'ok', plan: 'pro', fetched_at: Date.now() / 1000,
  session: { percent: 24, resets_at: Date.now() / 1000 + 7200, severity: 'normal' },
  week: { percent: 61, resets_at: Date.now() / 1000 + 200000, severity: 'normal' }, scoped: [] });
const setup = () => {
  const calls = { peeks: [], hidden: 0, refits: 0, opened: 0 };
  return { calls, limits: createLimits({ peek: nodes => calls.peeks.push(nodes), hidePeek: () => calls.hidden++,
    refit: () => calls.refits++, open: () => calls.opened++ }) };
};

test('touch limits tile opens its existing detail instead of expanding the queue', () => {
  touch = true;
  try {
    const { limits, calls } = setup(); limits.sync(sample());
    const mini = byClass(limits.tile, 'qm-lim')[0];
    mini.listeners.pointerdown({ pointerType: 'touch' });
    mini.listeners.focus(); // a real tap may focus before click
    mini.listeners.click();
    assert.equal(calls.opened, 0);
    assert.match(calls.peeks.at(-1).map(n => n.textContent).join(''), /Claude limits/);
    assert.equal(mini.attrs['data-pinned'], '');
    mini.listeners.click();
    assert.equal(mini.attrs['data-pinned'], undefined);
    mini.listeners.click();
    documentEvents.pointerdown({ target: new Element('div') });
    assert.equal(mini.attrs['data-pinned'], undefined);
  } finally { touch = false; }
});

test('peek placement uses measured size and the actual visible viewport', async () => {
  const { readFileSync } = await import('node:fs');
  const { runInNewContext } = await import('node:vm');
  const { peekPosition } = await import('../../piper/web/dist/menu.js');
  const src = readFileSync(new URL('../../piper/web/dist/main.js', import.meta.url), 'utf8');
  const fn = src.match(/function placePeek\(tile\) \{[\s\S]*?\n\}/)?.[0];
  assert.ok(fn);
  const qpeek = { style: {}, hidden: true, getBoundingClientRect: () => ({ width: 300, height: 240 }) };
  const context = { qpeek, peekPosition, window: { innerWidth: 390, innerHeight: 844,
    visualViewport: { width: 320, height: 500, offsetLeft: 0, offsetTop: 80 } } };
  runInNewContext(fn + ';globalThis.place = placePeek;', context);
  context.place({ getBoundingClientRect: () => ({ left: 250, right: 310, top: 750, bottom: 790 }) });
  assert.ok(parseFloat(qpeek.style.left) >= 8);
  assert.ok(parseFloat(qpeek.style.left) + 300 <= 312);
  assert.ok(parseFloat(qpeek.style.top) >= 88);
  assert.ok(parseFloat(qpeek.style.top) + 240 <= 572);
});

test('peek positions match the existing mobile design at 390/320 and wide queue rows', async () => {
  const { peekPosition } = await import('../../piper/web/dist/menu.js');
  assert.deepEqual(peekPosition({ left: 6, right: 46, top: 676, bottom: 720 }, 290, 106, 390, 844), { left: 56, top: 676 });
  assert.deepEqual(peekPosition({ left: 6, right: 46, top: 430, bottom: 474 }, 290, 106, 320, 568), { left: 22, top: 316 });
  for (const rect of [{ left: 54, right: 380, top: 30, bottom: 70 }, { left: 54, right: 300, top: 500, bottom: 540 }]) {
    const p = peekPosition(rect, 290, 220, 320, 568);
    assert.ok(p.left >= 8 && p.left + 290 <= 312);
    assert.ok(p.top >= 8 && p.top + 220 <= 560);
  }
});

test('touch switches providers, ignores emulated hover, and Escape clears the pinned detail', () => {
  const { limits, calls } = setup(); limits.sync(sample(), sample());
  const [claude, codex] = byClass(limits.tile, 'qm-lim');
  claude.listeners.pointerdown({ pointerType: 'touch' }); claude.listeners.click();
  const peeks = calls.peeks.length;
  claude.listeners.mouseenter(); claude.listeners.mouseleave();
  assert.equal(calls.peeks.length, peeks);
  assert.equal(claude.attrs['data-pinned'], '');
  codex.listeners.pointerdown({ pointerType: 'touch' }); codex.listeners.click();
  assert.equal(claude.attrs['data-pinned'], undefined);
  assert.equal(codex.attrs['data-pinned'], '');
  assert.match(calls.peeks.at(-1).map(n => n.textContent).join(''), /Codex limits/);
  documentEvents.keydown({ key: 'Escape' });
  assert.equal(codex.attrs['data-pinned'], undefined);
  codex.listeners.pointerdown({ pointerType: 'mouse' }); codex.listeners.click();
  assert.equal(calls.opened, 1, 'desktop retains queue expansion');
});

test('providers and labelled strip tiles independently appear and disappear; no-login stays hidden', () => {
  const { limits, calls } = setup(), rows = byClass(limits.el, 'ql-provider'), minis = byClass(limits.tile, 'qm-lim');
  limits.sync(sample(), sample());
  assert.deepEqual(rows.map(r => r.hidden), [false, false]);
  assert.deepEqual(minis.map(r => r.hidden), [false, false]);
  assert.equal(byClass(minis[0], 'qml-name')[0].textContent, 'CL');
  assert.equal(byClass(minis[1], 'qml-name')[0].textContent, 'CX');
  limits.sync(sample(), null);
  assert.deepEqual(rows.map(r => r.hidden), [false, true]);
  assert.deepEqual(minis.map(r => r.hidden), [false, true]);
  limits.sync(null, sample());
  assert.deepEqual(rows.map(r => r.hidden), [true, false]);
  limits.sync(null, { status: 'no-login' });
  assert.equal(limits.el.hidden, true);
  assert.equal(limits.tile.hidden, true);
  assert.equal(calls.refits, 3);
});

test('unknown quotas use dashes, stale or elapsed quotas retain last values rather than guessing zero', () => {
  const { limits } = setup(), codex = byClass(limits.el, 'ql-provider')[1];
  limits.sync(null, { ...sample(), status: 'unavailable', fetched_at: 0, session: null, week: null });
  assert.deepEqual(byClass(codex, 'ql-pct').map(n => n.textContent), ['– %', '– %']);
  assert.equal(codex.dim, true);
  const s = sample();
  s.session.resets_at = s.fetched_at + 10;
  limits.sync(null, s);
  assert.equal(codex.dim, false);
  const realNow = Date.now;
  try { Date.now = () => (s.fetched_at + 20) * 1000; limits.tick(); }
  finally { Date.now = realNow; }
  assert.equal(codex.dim, true);
  assert.deepEqual(byClass(codex, 'ql-pct').map(n => n.textContent), ['24 %', '61 %']);
  limits.sync(null, { ...sample(), status: 'expired' });
  assert.equal(codex.dim, true);
  assert.match(codex.attrs['aria-label'], /login expired/);
  limits.sync(null, { ...sample(), fetched_at: Date.now() / 1000 - 2401 });
  assert.equal(codex.dim, true);
});

test('a weekly-only plan shows the weekly value in both the Week column and the strip tile', () => {
  const { limits } = setup();
  limits.sync(null, { ...sample(), session: null });
  const codex = byClass(limits.el, 'ql-provider')[1], mini = byClass(limits.tile, 'qm-lim')[1];
  assert.deepEqual(byClass(codex, 'ql-pct').map(n => n.textContent), ['– %', '61 %']);
  assert.equal(byClass(mini, 'qml-pct')[0].textContent, '61%');
});

test('Claude uses Weekly in the footer and strip even with a higher Fable quota; hover keeps both', () => {
  const { limits, calls } = setup(), s = sample();
  s.scoped = [{ label: 'Fable', percent: 90, resets_at: s.fetched_at - 10, severity: 'warning' }];
  limits.sync(s);
  const claude = byClass(limits.el, 'ql-provider')[0], mini = byClass(limits.tile, 'qm-lim')[0];
  const weekly = byClass(claude, 'ql-pct')[1];
  assert.equal(weekly.textContent, '61 %');
  assert.equal(weekly.dataset.tone, 'ok');
  assert.equal(claude.dim, false, 'an elapsed Fable window must not dim the all-model quota');
  assert.equal(byClass(claude, 'ql-fill')[1].style.width, '61%');
  assert.equal(byClass(mini, 'qml-bar')[1].childNodes[0].style.width, '61%');
  assert.match(claude.attrs['aria-label'], /Week 61 %/);
  claude.listeners.mouseenter();
  const detail = calls.peeks.at(-1).map(n => n.textContent).join(' ');
  assert.match(detail, /Week61 %/);
  assert.match(detail, /Week · Fable90 %.*resetting…/);
  mini.listeners.mouseenter();
  assert.match(calls.peeks.at(-1).map(n => n.textContent).join(' '), /Week · Fable90 %/);
  limits.sync({ ...s, session: null });
  assert.equal(byClass(mini, 'qml-pct')[0].textContent, '61%');
  limits.sync({ ...s, session: null, week: null });
  assert.equal(weekly.textContent, '– %');
  assert.equal(byClass(mini, 'qml-pct')[0].textContent, '–%');
});

test('keyboard focus exposes resets and each scoped weekly reset; Escape dismisses; strip opens queue', () => {
  const { limits, calls } = setup(), s = sample();
  s.scoped = [{ label: 'Opus', percent: 90, resets_at: NOW + 86400, severity: 'normal' }];
  limits.sync(s, sample());
  const claude = byClass(limits.el, 'ql-provider')[0];
  claude.listeners.focus();
  const detail = calls.peeks.at(-1).map(n => n.textContent).join(' ');
  assert.match(detail, /Claude limits · pro/);
  assert.match(detail, /Week · Opus90 %/);
  const rows = detailNodes('Claude', s, NOW);
  assert.match(rows[3].textContent, new RegExp(weekText(NOW + 86400, NOW).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  limits.sync({ ...s, status: 'expired' }, sample());
  assert.match(calls.peeks.at(-1).map(n => n.textContent).join(' '), /login expired/);
  const hidden = calls.hidden;
  claude.listeners.keydown({ key: 'Escape' });
  assert.equal(calls.hidden, hidden + 1);
  const peekCount = calls.peeks.length;
  limits.tick();
  assert.equal(calls.peeks.length, peekCount, 'Escape must keep the detail closed on subsequent ticks');
  byClass(limits.tile, 'qm-lim')[1].listeners.click();
  assert.equal(calls.opened, 1);
});
