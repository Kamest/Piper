// Markdown preview: an own small parser into a token tree and a
// renderer through h() - never innerHTML, so a hostile .md written in the sandbox can not
// inject markup. Links: http(s) open in a new tab (rel=noopener), relative ones open the
// project file, anything else (javascript:, data:) stays plain text. CommonMark-ish, not exact.

import { h } from './dom.js';
import { highlightLines, langOf } from './highlight.js';
import { markBidi } from './text.js';

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'b' | 'i' | 's'; c: Inline[] }
  | { t: 'a'; href: string; c: Inline[] }
  | { t: 'img'; src: string; alt: string }
  | { t: 'br' };

export interface ListItem {
  /** task list: null = plain item */
  check: boolean | null;
  blocks: MdBlock[];
}

export type MdBlock =
  | { t: 'h'; level: number; c: Inline[]; line: number }
  | { t: 'p'; c: Inline[]; line: number }
  | { t: 'list'; ordered: boolean; start: number; items: ListItem[]; line: number }
  | { t: 'code'; lang: string; text: string[]; line: number }
  | { t: 'quote'; c: MdBlock[]; line: number }
  | { t: 'table'; align: ('' | 'left' | 'center' | 'right')[]; head: Inline[][]; rows: Inline[][][]; line: number }
  | { t: 'hr'; line: number };

const FENCE = /^(\s*)(`{3,}|~{3,})\s*([^\s`]*)/;
const HEADING = /^\s{0,3}(#{1,6})(?:\s+(.*?))?\s*(?:\s#+\s*)?$/;
const HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const LIST_EMPTY = /^(\s*)([-*+]|\d{1,9}[.)])\s*$/;
const QUOTE = /^\s{0,3}>\s?(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

const width = (s: string): number => {
  let n = 0;
  for (const ch of s) {
    if (ch === ' ') n++;
    else if (ch === '\t') n += 4 - (n % 4);
    else break;
  }
  return n;
};

/** drop up to n columns of leading whitespace */
const dedent = (s: string, n: number): string => {
  let col = 0;
  let i = 0;
  while (i < s.length && col < n) {
    if (s[i] === ' ') col++;
    else if (s[i] === '\t') col += 4 - (col % 4);
    else break;
    i++;
  }
  return s.slice(i);
};

function startsBlock(l: string): boolean {
  return FENCE.test(l) || (HEADING.test(l) && /^\s{0,3}#/.test(l)) || HR.test(l) || QUOTE.test(l) || LIST.test(l);
}

/** split a table row into cells (a `\|` stays in the cell) */
export function tableCells(row: string): string[] {
  let s = row.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const out: string[] = [];
  let cur = '';
  let code = false;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i]!;
    if (ch === '\\' && s[i + 1] === '|') { cur += '|'; i++; continue; }
    if (ch === '`') code = !code;
    if (ch === '|' && !code) { out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  out.push(cur.trim());
  return out;
}

/** block structure of a Markdown document; `line` is 1-based in the source (+ base) */
export function parseMarkdown(src: string | string[], base = 0): MdBlock[] {
  const lines = typeof src === 'string' ? src.replace(/\r\n?/g, '\n').split('\n') : src;
  const out: MdBlock[] = [];
  let i = 0;
  while (i < lines.length) {
    const l = lines[i]!;
    const line = base + i + 1;
    if (!l.trim()) { i++; continue; }
    const f = FENCE.exec(l);
    if (f) {
      const mark = f[2]!;
      const ind = f[1]!.length;
      const text: string[] = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${mark[0] === '`' ? '`' : '~'}{${mark.length},}\\s*$`).test(lines[i]!)) {
        text.push(dedent(lines[i]!, ind));
        i++;
      }
      i++;
      out.push({ t: 'code', lang: f[3]!.toLowerCase(), text, line });
      continue;
    }
    if (/^\s{0,3}#/.test(l)) {
      const m = HEADING.exec(l);
      if (m) {
        out.push({ t: 'h', level: m[1]!.length, c: parseInline(m[2] ?? ''), line });
        i++;
        continue;
      }
    }
    if (HR.test(l)) { out.push({ t: 'hr', line }); i++; continue; }
    if (QUOTE.test(l)) {
      const inner: string[] = [];
      while (i < lines.length && lines[i]!.trim()) {
        const q = QUOTE.exec(lines[i]!);
        inner.push(q ? q[1]! : lines[i]!);
        i++;
      }
      out.push({ t: 'quote', c: parseMarkdown(inner, line - 1), line });
      continue;
    }
    const lm = LIST.exec(l) ?? LIST_EMPTY.exec(l);
    if (lm) {
      const ind = width(lm[1]!);
      const ordered = /\d/.test(lm[2]!);
      const items: ListItem[] = [];
      let cur: string[] | null = null;
      let curLine = line;
      let content = 0;
      const flush = () => {
        if (!cur) return;
        let check: boolean | null = null;
        const t = /^\[([ xX])\]\s+/.exec(cur[0] ?? '');
        if (t) { check = t[1] !== ' '; cur[0] = cur[0]!.slice(t[0].length); }
        items.push({ check, blocks: parseMarkdown(cur, curLine - 1) });
      };
      while (i < lines.length) {
        const s = lines[i]!;
        const m = LIST.exec(s) ?? LIST_EMPTY.exec(s);
        if (m && width(m[1]!) === ind && /\d/.test(m[2]!) === ordered) {
          flush();
          const marker = m[1]!.length + m[2]!.length;
          const rest = m[3] ?? '';
          content = width(m[1]!) + m[2]!.length + Math.max(1, Math.min(4, (s.length - marker) - rest.length || 1));
          cur = [rest];
          curLine = base + i + 1;
          i++;
          continue;
        }
        if (!s.trim()) {
          // a blank line: the list goes on when the next text is indented or another item
          let j = i + 1;
          while (j < lines.length && !lines[j]!.trim()) j++;
          const nx = lines[j];
          if (nx === undefined) break;
          const nm = LIST.exec(nx);
          if (width(nx) > ind && !(nm && width(nm[1]!) <= ind)) { cur?.push(''); i++; continue; }
          if (nm && width(nm[1]!) === ind && /\d/.test(nm[2]!) === ordered) { i++; continue; }
          break;
        }
        if (width(s) > ind) { cur?.push(dedent(s, content)); i++; continue; }
        // lazy continuation of the item's paragraph
        const prev = cur?.[cur.length - 1];
        if (prev && prev.trim() && !startsBlock(s)) { cur!.push(s.trim()); i++; continue; }
        break;
      }
      flush();
      out.push({ t: 'list', ordered, start: ordered ? Number(/\d+/.exec(lm[2]!)?.[0] ?? 1) : 1, items, line });
      continue;
    }
    if (l.includes('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]!) && lines[i + 1]!.includes('-')) {
      const head = tableCells(l);
      const align = tableCells(lines[i + 1]!).map((c) => {
        const a = c.startsWith(':');
        const b = c.endsWith(':');
        return a && b ? 'center' : b ? 'right' : a ? 'left' : '';
      }) as ('' | 'left' | 'center' | 'right')[];
      i += 2;
      const rows: Inline[][][] = [];
      while (i < lines.length && lines[i]!.trim() && lines[i]!.includes('|')) {
        rows.push(tableCells(lines[i]!).map(parseInline));
        i++;
      }
      out.push({ t: 'table', align, head: head.map(parseInline), rows, line });
      continue;
    }
    // paragraph
    const para: string[] = [];
    while (i < lines.length && lines[i]!.trim() && (!para.length || !startsBlock(lines[i]!))) {
      if (para.length && lines[i]!.includes('|') && TABLE_SEP.test(lines[i + 1] ?? '')) break;
      para.push(lines[i]!);
      i++;
    }
    const c: Inline[] = [];
    para.forEach((p, k) => {
      const hard = / {2,}$/.test(p) || /\\$/.test(p);
      const text = p.replace(/^\s+/, '').replace(/(?: {2,}|\\)$/, '');
      c.push(...parseInline(text));
      if (k < para.length - 1) c.push(hard ? { t: 'br' } : { t: 'text', v: ' ' });
    });
    out.push({ t: 'p', c: mergeText(c), line });
  }
  return out;
}

function mergeText(c: Inline[]): Inline[] {
  const out: Inline[] = [];
  for (const x of c) {
    const last = out[out.length - 1];
    if (x.t === 'text' && last?.t === 'text') last.v += x.v;
    else out.push(x);
  }
  return out;
}

/** the matching `]` / `)` from an opening one, nesting counted; -1 when missing */
function closing(s: string, at: number, open: string, close: string): number {
  let depth = 0;
  for (let i = at; i < s.length; i++) {
    if (s[i] === '\\') { i++; continue; }
    if (s[i] === open) depth++;
    else if (s[i] === close && --depth === 0) return i;
  }
  return -1;
}

const PUNCT = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/;
const alnum = (ch: string | undefined): boolean => !!ch && /[\p{L}\p{N}]/u.test(ch);

/** inline spans: `code`, **bold**, *italic*, ~~strike~~, [links](url), <autolinks>, bare https:// */
export function parseInline(s: string): Inline[] {
  const out: Inline[] = [];
  let text = '';
  const push = (x: Inline) => {
    if (text) { out.push({ t: 'text', v: text }); text = ''; }
    out.push(x);
  };
  let i = 0;
  while (i < s.length) {
    const ch = s[i]!;
    if (ch === '\\' && PUNCT.test(s[i + 1] ?? '')) { text += s[i + 1]; i += 2; continue; }
    if (ch === '`') {
      let n = 1;
      while (s[i + n] === '`') n++;
      const fence = '`'.repeat(n);
      const end = s.indexOf(fence, i + n);
      if (end > 0) {
        let v = s.slice(i + n, end);
        if (/^ .* $/.test(v)) v = v.slice(1, -1);
        push({ t: 'code', v });
        i = end + n;
        continue;
      }
      text += fence;
      i += n;
      continue;
    }
    if ((ch === '!' && s[i + 1] === '[') || ch === '[') {
      const img = ch === '!';
      const lb = img ? i + 1 : i;
      const rb = closing(s, lb, '[', ']');
      if (rb > 0 && s[rb + 1] === '(') {
        const rp = closing(s, rb + 1, '(', ')');
        if (rp > 0) {
          const label = s.slice(lb + 1, rb);
          const href = s.slice(rb + 2, rp).trim().replace(/\s+(?:"[^"]*"|'[^']*')$/, '').replace(/^<(.*)>$/, '$1');
          push(img ? { t: 'img', src: href, alt: label } : { t: 'a', href, c: parseInline(label) });
          i = rp + 1;
          // GitLab's image size attributes `{width=753 height=362}` are not text
          const attrs = img ? /^\{(?:\s*(?:width|height)=[\w.%]+)+\s*\}/.exec(s.slice(i)) : null;
          if (attrs) i += attrs[0].length;
          continue;
        }
      }
    }
    if (ch === '<') {
      const m = /^<(https?:\/\/[^\s<>]+)>/i.exec(s.slice(i));
      if (m) { push({ t: 'a', href: m[1]!, c: [{ t: 'text', v: m[1]! }] }); i += m[0].length; continue; }
    }
    if ((ch === 'h' || ch === 'H') && !alnum(s[i - 1])) {
      const m = /^https?:\/\/[^\s<>]+/i.exec(s.slice(i));
      if (m) {
        let url = m[0];
        while (/[.,;:!?)'"*_]$/.test(url) && !(url.endsWith(')') && url.split('(').length >= url.split(')').length)) url = url.slice(0, -1);
        push({ t: 'a', href: url, c: [{ t: 'text', v: url }] });
        i += url.length;
        continue;
      }
    }
    if (ch === '~' && s[i + 1] === '~') {
      const end = s.indexOf('~~', i + 2);
      if (end > i + 2) { push({ t: 's', c: parseInline(s.slice(i + 2, end)) }); i = end + 2; continue; }
    }
    if (ch === '*' || ch === '_') {
      const dbl = s[i + 1] === ch;
      const mark = dbl ? ch + ch : ch;
      const open = s[i + mark.length];
      // `_` inside a word (snake_case) is not emphasis; an opener must be followed by text
      const ok = open !== undefined && open !== ' ' && (ch === '*' || !alnum(s[i - 1]));
      if (ok) {
        let end = -1;
        for (let j = i + mark.length + 1; j <= s.length - mark.length; j++) {
          if (s[j] === '\\') { j++; continue; }
          if (s[j] === '`') { const e = s.indexOf('`', j + 1); if (e > 0) { j = e; continue; } }
          if (s.startsWith(mark, j) && s[j - 1] !== ' ' && (dbl || s[j + 1] !== ch) && (ch === '*' || !alnum(s[j + mark.length]))) { end = j; break; }
        }
        if (end > 0) {
          push({ t: dbl ? 'b' : 'i', c: parseInline(s.slice(i + mark.length, end)) });
          i = end + mark.length;
          continue;
        }
      }
    }
    text += ch;
    i++;
  }
  if (text) out.push({ t: 'text', v: text });
  return out;
}

/** what a link may become: web = new tab, rel = a project file, bad = plain text */
export function linkKind(href: string): 'web' | 'rel' | 'anchor' | 'bad' {
  const s = href.trim();
  if (/^https?:\/\//i.test(s)) return 'web';
  if (s.startsWith('#')) return 'anchor';
  if (!s || /^[a-z][a-z0-9+.-]*:/i.test(s) || s.startsWith('//') || s.startsWith('\\')) return 'bad';
  return 'rel';
}

/** heading anchors the GitHub way: lower case, spaces to dashes, punctuation dropped */
export function slug(text: string): string {
  return text.toLowerCase().trim().replace(/[^\p{L}\p{N}\s_-]/gu, '').replace(/\s/g, '-');
}

export function inlineText(c: Inline[]): string {
  return c.map((x) => (x.t === 'text' || x.t === 'code' ? x.v : x.t === 'br' ? ' ' : x.t === 'img' ? x.alt : inlineText(x.c))).join('');
}

// ---------------------------------------------------------------- render (DOM through h())

export interface MdRenderOpts {
  /** a relative link: open the project file */
  openRel(href: string): void;
}

function renderInline(c: Inline[], o: MdRenderOpts): (Node | string)[] {
  return c.map((x): Node | string => {
    switch (x.t) {
      case 'text': return markBidi(x.v);
      case 'code': return h('code', {}, x.v);
      case 'b': return h('strong', {}, ...renderInline(x.c, o));
      case 'i': return h('em', {}, ...renderInline(x.c, o));
      case 's': return h('del', {}, ...renderInline(x.c, o));
      case 'br': return h('br');
      case 'img': return h('span', { class: 'mut', title: x.src }, `[image: ${x.alt || x.src}]`);
      case 'a': {
        const kind = linkKind(x.href);
        const kids = renderInline(x.c, o);
        if (kind === 'web') return h('a', { href: x.href.trim(), target: '_blank', rel: 'noopener noreferrer' }, ...kids);
        if (kind === 'rel') return h('button', { class: 'link', title: x.href, onclick: () => o.openRel(x.href) }, ...kids);
        if (kind === 'anchor') {
          return h('button', {
            class: 'link', title: x.href,
            onclick: (e: Event) => {
              const root = (e.target as HTMLElement).closest('.mdv');
              root?.querySelector(`[data-slug="${CSS.escape(decodeURIComponent(x.href.slice(1)))}"]`)?.scrollIntoView({ block: 'start' });
            },
          }, ...kids);
        }
        return h('span', { title: 'link of a disallowed type' }, ...kids);
      }
    }
  });
}

function renderCode(b: { lang: string; text: string[] }): HTMLElement {
  const lang = langOf('x.' + (b.lang === 'javascript' ? 'js' : b.lang === 'typescript' ? 'ts' : b.lang === 'shell' || b.lang === 'bash' ? 'sh' : b.lang === 'python' ? 'py' : b.lang));
  const pre = h('pre', { class: 'mdcode mono' });
  highlightLines(b.text, lang).forEach((toks, k) => {
    if (k) pre.append('\n');
    for (const t of toks) pre.append(t.c ? h('span', { class: 'hl-' + t.c }, t.t) : markBidi(t.t));
  });
  return pre;
}

function renderBlocks(bs: MdBlock[], o: MdRenderOpts): HTMLElement[] {
  return bs.map((b): HTMLElement => {
    const at = String(b.line);
    switch (b.t) {
      case 'h': {
        const tag = (['h1', 'h2', 'h3', 'h4', 'h5', 'h6'] as const)[b.level - 1] ?? 'h6';
        return h(tag, { 'data-line': at, 'data-slug': slug(inlineText(b.c)) }, ...renderInline(b.c, o));
      }
      case 'p': return h('p', { 'data-line': at }, ...renderInline(b.c, o));
      case 'hr': return h('hr', { 'data-line': at });
      case 'code': { const pre = renderCode(b); pre.dataset.line = at; return pre; }
      case 'quote': return h('blockquote', { 'data-line': at }, ...renderBlocks(b.c, o));
      case 'list': {
        const list = b.ordered ? h('ol', { 'data-line': at, start: b.start }) : h('ul', { 'data-line': at });
        for (const it of b.items) {
          const li = h('li', { class: it.check === null ? '' : 'task' });
          if (it.check !== null) li.append(h('input', { type: 'checkbox', disabled: true, checked: it.check }));
          // a tight item: its single paragraph without the <p> margins
          const only = it.blocks.length === 1 && it.blocks[0]!.t === 'p' ? it.blocks[0] : null;
          if (only && only.t === 'p') li.append(...renderInline(only.c, o));
          else li.append(...renderBlocks(it.blocks, o));
          list.append(li);
        }
        return list;
      }
      case 'table': {
        const al = (k: number) => (b.align[k] ? `text-align:${b.align[k]}` : undefined);
        return h('div', { class: 'mdtable', 'data-line': at }, h('table', {},
          h('thead', {}, h('tr', {}, ...b.head.map((c, k) => h('th', { style: al(k) }, ...renderInline(c, o))))),
          h('tbody', {}, ...b.rows.map((r) => h('tr', {}, ...b.head.map((_, k) => h('td', { style: al(k) }, ...renderInline(r[k] ?? [], o))))))));
      }
    }
  });
}

export function renderMarkdown(blocks: MdBlock[], o: MdRenderOpts): HTMLElement {
  return h('div', { class: 'mdv' }, ...renderBlocks(blocks, o));
}
