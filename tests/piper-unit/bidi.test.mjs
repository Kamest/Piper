// node --test: N5 - bidi controls are neutralised centrally (text.ts markBidi, dom.ts h()), from web/dist;
// plus a lint over web/src: a text sink outside h() must take a literal or go through markBidi()/showBidi()
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { markBidi, showBidi } from '../../piper/web/dist/text.js';

const RLO = '‮';
const FAMILY = '\u{1F468}‍\u{1F469}‍\u{1F467}';   // ZWJ sequence
const HEART = '❤️';                                // variation selector

// --- a DOM just big enough for h(): elements with attributes and children, text nodes ----------------
class FakeText { constructor(s) { this.nodeType = 3; this.data = s; } get textContent() { return this.data; } }
class FakeEl {
  constructor(tag) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = new Map(); this.childNodes = []; this.className = ''; this.listeners = {}; }
  setAttribute(k, v) { this.attrs.set(k, String(v)); }
  getAttribute(k) { return this.attrs.has(k) ? this.attrs.get(k) : null; }
  addEventListener(k, f) { this.listeners[k] = f; }
  append(...xs) { for (const x of xs) this.childNodes.push(typeof x === 'string' ? new FakeText(x) : x); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(''); }
}
globalThis.document ??= { createElement: (t) => new FakeEl(t), createTextNode: (s) => new FakeText(s) };
const { h, rawText } = await import('../../piper/web/dist/dom.js');

test('markBidi: every direction control becomes a visible [U+XXXX]; emoji, ZWJ and variation selectors stay', () => {
  assert.equal(markBidi(`rtl-${RLO}txt.md`), 'rtl-[U+202E]txt.md');
  for (const c of ['‪', '‫', '‬', '‭', '‮', '⁦', '⁧', '⁨', '⁩', '‎', '‏', '؜']) {
    assert.equal(markBidi(`a${c}b`), `a[U+${c.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]b`);
  }
  assert.equal(markBidi(`${FAMILY} ${HEART} 🎉`), `${FAMILY} ${HEART} 🎉`);
  assert.equal(markBidi('plain <script> $(id)'), 'plain <script> $(id)');
});

test('markBidi is idempotent and a no-op on showBidi output (the scattered showBidi calls stay harmless)', () => {
  const s = `x${RLO}y⁦z​w`;
  assert.equal(markBidi(markBidi(s)), markBidi(s));
  assert.equal(markBidi(showBidi(s)), showBidi(s));
  assert.equal(showBidi(showBidi(s)), showBidi(s));
});

test('showBidi no longer splits emoji: ZWJ stays (N4 design item), invisible characters still show', () => {
  assert.equal(showBidi(FAMILY), FAMILY);
  assert.equal(showBidi(`a​b${RLO}`), 'a[U+200B]b[U+202E]');
});

test('h(): string children and title / aria-label / placeholder / alt are masked, nothing else', () => {
  const el = h('button', { title: `open ${RLO}evil`, 'aria-label': `x${RLO}`, placeholder: `p${RLO}`, alt: `a${RLO}`,
    'data-path': `d${RLO}`, value: `v${RLO}`, class: 'c' }, `name ${RLO}txt.md `, FAMILY);
  assert.equal(el.getAttribute('title'), 'open [U+202E]evil');
  assert.equal(el.getAttribute('aria-label'), 'x[U+202E]');
  assert.equal(el.getAttribute('placeholder'), 'p[U+202E]');
  assert.equal(el.getAttribute('alt'), 'a[U+202E]');
  assert.equal(el.getAttribute('data-path'), `d${RLO}`, 'data-* carries the real value');
  assert.equal(el.getAttribute('value'), `v${RLO}`, 'a value is what the user edits / sends');
  assert.equal(el.textContent, `name [U+202E]txt.md ${FAMILY}`);
});

test('h(): a textarea keeps its text raw (the user edits and saves it); a Node child and rawText() pass untouched', () => {
  assert.equal(h('textarea', {}, `prompt ${RLO}x`).textContent, `prompt ${RLO}x`);
  const inner = rawText(`sql ${RLO}`);
  assert.equal(h('span', {}, inner).textContent, `sql ${RLO}`);
  assert.equal(h('pre', {}, h('span', {}, `a${RLO}`), rawText(RLO)).textContent, `a[U+202E]${RLO}`);
});

// --- lint: text sinks outside h() ----------------------------------------------------------------------
const SRC = new URL('../../piper/web/src/', import.meta.url);

/** end of the expression starting at i: ; or , or a closing bracket at depth 0, or a // comment */
function exprEnd(s, i) {
  let d = 0;
  for (let j = i; j < s.length; j++) {
    const c = s[j];
    if (c === "'" || c === '"') { j++; while (s[j] !== c) { if (s[j] === '\\') j++; j++; } continue; }
    if (c === '`') {
      j++; let k = 0;
      for (;;) {
        if (s[j] === '\\') { j += 2; continue; }
        if (s[j] === '`' && !k) break;
        if (s[j] === '$' && s[j + 1] === '{') { k++; j += 2; continue; }
        if (s[j] === '}' && k) k--;
        j++;
      }
      continue;
    }
    if (c === '/' && s[j + 1] === '/') { if (!d) return j; while (s[j] !== '\n') j++; continue; }
    if ('([{'.includes(c)) d++;
    else if (')]}'.includes(c)) { if (!d) return j; d--; }
    else if ((c === ';' || c === ',') && !d) return j;
  }
  return s.length;
}
const LIT = /^\s*('([^'\\\n]|\\.)*'|"([^"\\\n]|\\.)*"|`[^`$\\]*`)\s*$/;
function wrapped(e) {   // the whole expression is markBidi(...) / showBidi(...)
  const t = e.trim();
  const m = /^(markBidi|showBidi)\(/.exec(t);
  if (!m) return false;
  let d = 0;
  for (let j = m[0].length - 1; j < t.length; j++) {
    if (t[j] === '(') d++;
    else if (t[j] === ')' && --d === 0) return j === t.length - 1;
  }
  return false;
}
function ternary(e) {   // top-level "c ? a : b"
  let d = 0, q = -1;
  for (let j = 0; j < e.length; j++) {
    const c = e[j];
    if (c === "'" || c === '"' || c === '`') { j++; while (j < e.length && e[j] !== c) { if (e[j] === '\\') j++; j++; } continue; }
    if ('([{'.includes(c)) d++;
    else if (')]}'.includes(c)) d--;
    else if (!d && c === '?' && q < 0 && !'.?'.includes(e[j + 1]) && e[j - 1] !== '?') q = j;
    else if (!d && c === ':' && q >= 0) return [e.slice(q + 1, j), e.slice(j + 1)];
  }
  return null;
}
// kbd('literal', 'Ctrl') (clipkeys.ts) only swaps ⌘ for the modifier of this OS: still a literal
const KBD_LIT = /^\s*kbd\(\s*('([^'\\\n]|\\.)*'|"([^"\\\n]|\\.)*")\s*(,\s*'[A-Za-z]+'\s*)?\)\s*$/;
function safe(e) {
  if (LIT.test(e) || KBD_LIT.test(e) || wrapped(e)) return true;
  const t = ternary(e);
  return !!t && safe(t[0]) && safe(t[1]);
}
const SINKS = [
  /\.(textContent|innerText|title|placeholder|ariaLabel)\s*=(?![=>])/g,
  /setAttribute\(\s*'(title|aria-label|placeholder|alt)'\s*,/g,
  /createTextNode\(/g,
  /insertAdjacentText\(\s*'[a-z]+'\s*,/g,
  /new Notification\(/g,
];
// not DOM, or a fixed literal the scanner can not see through - each with its reason
const ALLOW = [
  ['queue.ts', 'it.title =', 'QItem field, rendered through h() later'],
  ['dbview.ts', 'pane.title =', 'result-pane object field, rendered through h() later'],
  ['cmeditor.ts', 'button.textContent = label', 'label / title come from a literal tuple in the loop header'],
  ['cmeditor.ts', 'button.title = title', 'literal tuple'],
  ['cmeditor.ts', "button.setAttribute('aria-label', title", 'literal tuple'],
];

function sinkHits(file, s) {
  const out = [];
  for (const rx of SINKS) {
    rx.lastIndex = 0;
    let m;
    while ((m = rx.exec(s))) {
      const at = m.index + m[0].length;
      const expr = s.slice(at, exprEnd(s, at));
      const line = s.slice(s.lastIndexOf('\n', m.index) + 1, s.indexOf('\n', m.index));
      const head = s.slice(Math.max(0, m.index - 30), at);
      if (safe(expr) || ALLOW.some(([f, snip]) => f === file && (head + expr).includes(snip))) continue;
      out.push(`${file}:${s.slice(0, m.index).split('\n').length}: ${line.trim().slice(0, 140)}`);
    }
  }
  // .append / .prepend / .replaceChildren with a template literal that interpolates: text that skipped h()
  const ap = /\.(append|prepend|replaceChildren|before|after)\(/g;
  let m;
  while ((m = ap.exec(s))) {
    for (let k = m.index + m[0].length; k <= s.length;) {   // every argument
      const e = exprEnd(s, k);
      const arg = s.slice(k, e).trim();
      if (arg.startsWith('`') && arg.includes('${')) out.push(`${file}:${s.slice(0, m.index).split('\n').length}: append of ${arg.slice(0, 80)}`);
      if (s[e] !== ',') break;
      k = e + 1;
    }
  }
  return out;
}

test('lint: every text sink outside h() takes a literal or goes through markBidi()/showBidi()', () => {
  const files = readdirSync(SRC).filter((f) => f.endsWith('.ts') && !['dom.ts', 'text.ts'].includes(f));
  assert.ok(files.length > 30, 'web/src read');
  const bad = files.flatMap((f) => sinkHits(f, readFileSync(new URL(f, SRC), 'utf8')));
  assert.deepEqual(bad, [], 'wrap the value in markBidi() (text.ts), or add it to ALLOW with the reason');
});

test('lint self-check: the scanner catches the shapes it promises to catch', () => {
  const hits = sinkHits('x.ts', [
    'el.textContent = name;', "el.title = `open ${p}`;", "el.setAttribute('aria-label', who);", 'document.createTextNode(line);',
    "box.append(' · ', `${x} skipped`);", 'new Notification(title, { body });',
    "ok.textContent = 'fixed';", "ok.textContent = on ? 'a' : 'b';", 'ok.textContent = markBidi(name);', 'ok.title = showBidi(`x ${y}`);',
    'if (a.textContent === b) x();', 'ok.title = markBidi(a) + b;',
  ].join('\n'));
  assert.equal(hits.length, 7, hits.join('\n'));
  assert.ok(hits.some((x) => x.includes('markBidi(a) + b')), 'a call followed by more text is not wrapped');
});

test('CodeMirror shows the direction controls its default set misses (LRE, RLE, PDF, FSI)', () => {
  const js = readFileSync(new URL('../../piper/web/dist/cmeditor.js', import.meta.url), 'utf8');
  assert.match(js, /addSpecialChars: \/\[\\u202a-\\u202c\\u2068\]\//);
  assert.equal((js.match(/highlightSpecialChars\(SPECIALS\)/g) ?? []).length, 2, 'both the editor and the merge review');
});

test('N7: a case-insensitive highlight marks the matched characters, also after a letter whose lowercase is longer (İ)', async () => {
  const { matchRanges } = await import('../../piper/web/dist/text.js');
  const { marked } = await import('../../piper/web/dist/convo.js');
  const text = 'İstanbul uppercase dotted I then needle-after';
  assert.deepEqual(matchRanges(text, 'needle').map(([i, j]) => text.slice(i, j)), ['needle']);
  assert.deepEqual(matchRanges('a.b A.B', 'a.b').map(([i, j]) => [i, j]), [[0, 3], [4, 7]], 'the query is literal, not a regex');
  assert.deepEqual(matchRanges('x(1)$ [y]', '(1)$ [').length, 1);
  assert.deepEqual(matchRanges('abc', ''), []);
  const marks = (parts) => parts.filter((p) => typeof p !== 'string').map((p) => p.textContent);
  assert.deepEqual(marks(marked(text, 'needle')), ['needle']);
  assert.deepEqual(marks(marked('Needle and NEEDLE', 'needle')), ['Needle', 'NEEDLE']);
  const all = marked(`İ ${RLO}evil needle`, 'needle');
  assert.equal(all.map((p) => (typeof p === 'string' ? p : p.textContent)).join(''), 'İ [U+202E]evil needle', 'bidi stays masked around the marks');
});
