// Code navigation helpers of the file browser: pure functions
// only (no DOM at module level, node tests import them). No language server: regexes that are
// right for the common Java / TS / Python / Markdown shapes, like the backend's symbol index.

import { langOf } from './highlight.js';

export interface Ident {
  name: string;
  start: number;
  end: number;
}

/** the identifier under a character offset (or just before it: a click right after a word) */
export function identifierAt(text: string, offset: number, extra = ''): Ident | null {
  const esc = extra.replace(/[\]\\^-]/g, '\\$&');
  const word = new RegExp(`[A-Za-z0-9_$${esc}]`);
  let i = Math.max(0, Math.min(offset, text.length));
  if (!word.test(text[i] ?? '') && i > 0 && word.test(text[i - 1] ?? '')) i--;
  if (!word.test(text[i] ?? '')) return null;
  let a = i;
  let b = i;
  while (a > 0 && word.test(text[a - 1] ?? '')) a--;
  while (b < text.length && word.test(text[b] ?? '')) b++;
  // dots/dashes of property keys never start or end a name
  while (a < b && extra.includes(text[a] ?? '')) a++;
  while (b > a && extra.includes(text[b - 1] ?? '')) b--;
  const name = text.slice(a, b);
  if (!name || /^[\d.]/.test(name)) return null;
  return { name, start: a, end: b };
}

/** extra identifier characters per file type: property keys and XML ids keep their dots */
export function identChars(path: string): string {
  const l = langOf(path);
  return l === 'props' || l === 'yaml' || l === 'xml' ? '.-' : '';
}

export function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** grep pattern of "find usages": the whole word (\b does not work next to `$`) */
export function usagePattern(name: string): string {
  const e = escapeRegex(name);
  return `${/^[\w]/.test(name) ? '\\b' : ''}${e}${/[\w]$/.test(name) ? '\\b' : ''}`;
}

// ---------------------------------------------------------------- outline

export interface OutlineItem {
  name: string;
  /** class | interface | enum | record | object | type | method | function | const | heading | key | bean | table | … */
  kind: string;
  /** 1-based */
  line: number;
  depth: number;
}

const NOT_METHOD = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'new', 'else', 'throw', 'synchronized',
  'do', 'try', 'case', 'await', 'typeof', 'super', 'this', 'function', 'assert', 'yield', 'when', 'sizeof', 'delete']);
const MODS = '(?:(?:public|private|protected|internal|abstract|final|sealed|non-sealed|static|export|default|declare|data|open|inner|companion|annotation|value|inline|expect|actual|strictfp)\\s+)*';
const CLASS_RE = new RegExp(`^(\\s*)${MODS}(class|interface|enum|record|object|trait|@interface|namespace)\\s+([A-Za-z_$][\\w$]*)`);
const TS_TYPE_RE = /^(\s*)(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=/;
const JAVA_METHOD_RE = /^(\s*)(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp)\s+)*(?:<[^>]+>\s+)?([\w$][\w$<>[\],.? ]*?)\s+([a-zA-Z_$][\w$]*)\s*\([^;=]*$/;
const JAVA_CTOR_RE = /^(\s*)(?:public|private|protected)\s+([A-Z][\w$]*)\s*\([^;=]*$/;
const KT_FUN_RE = /^(\s*)(?:(?:public|private|protected|internal|override|open|abstract|final|suspend|inline|operator|infix|tailrec|external|actual|expect)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w$.<>?]+\.)?([A-Za-z_$][\w$]*)\s*\(/;
const TS_FUNC_RE = /^(\s*)(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)/;
const TS_CONST_RE = /^(\s*)(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>/;
const TS_EXPORT_CONST_RE = /^()export\s+(?:const|let)\s+([A-Za-z_$][\w$]*)/;
const TS_METHOD_RE = /^(\s+)(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^={;]+)?\{\s*(?:\}\s*)?$/;
const PY_RE = /^(\s*)(class|def|async\s+def)\s+([A-Za-z_]\w*)/;
const GO_RE = /^()(?:func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|type\s+([A-Za-z_]\w*))/;
const SQL_RE = /^\s*create\s+(?:or\s+replace\s+)?(?:temporary\s+|temp\s+|unique\s+|materialized\s+)?(table|view|function|procedure|index|trigger|sequence|type)\s+(?:if\s+not\s+exists\s+)?([\w."`[\]]+)/i;
const SH_RE = /^\s*(?:function\s+([\w:.-]+)\s*(?:\(\s*\))?|([\w:.-]+)\s*\(\s*\))\s*\{?/;

const indentOf = (s: string): number => {
  let n = 0;
  for (const ch of s) {
    if (ch === ' ') n++;
    else if (ch === '\t') n += 4;
    else break;
  }
  return n;
};

/** classes, methods, functions, headings … of one file, in order, with a nesting depth */
export function outline(lines: string[], path: string): OutlineItem[] {
  const lang = langOf(path);
  const ext = (path.split('.').pop() ?? '').toLowerCase();
  const out: OutlineItem[] = [];
  const add = (name: string, kind: string, i: number, depth: number) => { if (name) out.push({ name, kind, line: i + 1, depth }); };
  if (lang === 'md') {
    let fence = false;
    lines.forEach((l, i) => {
      if (/^\s*(```|~~~)/.test(l)) fence = !fence;
      const m = !fence && /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(l);
      if (m) add(m[2]!, 'heading', i, m[1]!.length - 1);
    });
    return out;
  }
  if (lang === 'py') {
    lines.forEach((l, i) => {
      const m = PY_RE.exec(l);
      if (m) add(m[3]!, m[2] === 'class' ? 'class' : 'function', i, Math.floor(indentOf(m[1]!) / 4));
    });
    return out;
  }
  if (lang === 'yaml') {
    lines.forEach((l, i) => { const m = /^([\w.$/-]+|"[^"]+"|'[^']+')\s*:(\s|$)/.exec(l); if (m) add(m[1]!, 'key', i, 0); });
    return out;
  }
  if (lang === 'props') {
    lines.forEach((l, i) => { const m = /^\s*([^#!;=:\s[][^=:\s]*)\s*[=:]/.exec(l); if (m) add(m[1]!, 'key', i, 0); });
    return out;
  }
  if (lang === 'sql') {
    lines.forEach((l, i) => { const m = SQL_RE.exec(l); if (m) add(m[2]!.replace(/["`[\]]/g, ''), m[1]!.toLowerCase(), i, 0); });
    return out;
  }
  if (lang === 'sh') {
    lines.forEach((l, i) => {
      const m = SH_RE.exec(l);
      const name = m?.[1] ?? m?.[2] ?? '';
      if (m && !NOT_METHOD.has(name) && !/^(if|then|elif)$/.test(name)) add(name, 'function', i, 0);
    });
    return out;
  }
  if (lang === 'xml') {
    lines.forEach((l, i) => {
      const bean = /<bean\b[^>]*\bid\s*=\s*"([^"]+)"/.exec(l);
      if (bean) return add(bean[1]!, 'bean', i, 0);
      const id = /<([\w:.-]+)\b[^>]*\bid\s*=\s*"([^"]+)"/.exec(l);
      if (id) add(id[2]!, id[1]!, i, 0);
    });
    return out;
  }
  if (lang === 'css') {
    lines.forEach((l, i) => { const m = /^([^\s{}/@][^{}]*?)\s*\{\s*(?:\}\s*)?$/.exec(l) ?? /^(@media[^{]*?)\s*\{/.exec(l); if (m) add(m[1]!, 'rule', i, 0); });
    return out;
  }
  if (lang === 'plain' || lang === 'json') return out;
  // C-like: a stack of open classes by indentation gives the depth
  const stack: number[] = [];
  let block = false;
  const isTs = lang === 'ts';
  const isKt = lang === 'kotlin';
  const isGo = ext === 'go';
  lines.forEach((l, i) => {
    const t = l.trim();
    if (block) {
      if (t.includes('*/')) block = false;
      return;
    }
    if (t.startsWith('/*') && !t.includes('*/')) { block = true; return; }
    if (!t || t.startsWith('//') || t.startsWith('*') || t.startsWith('/*') || t.startsWith('@') && !t.startsWith('@interface')) return;
    const ind = indentOf(l);
    while (stack.length && stack[stack.length - 1]! >= ind) stack.pop();
    const depth = stack.length;
    if (isGo) {
      const m = GO_RE.exec(l);
      if (m) add(m[2] ?? m[3] ?? '', m[2] ? 'function' : 'type', i, 0);
      return;
    }
    const c = CLASS_RE.exec(l);
    if (c) {
      add(c[3]!, c[2] === '@interface' ? 'annotation' : c[2]!, i, depth);
      stack.push(ind);
      return;
    }
    if (isTs) {
      const ty = TS_TYPE_RE.exec(l);
      if (ty) return add(ty[2]!, 'type', i, depth);
      const f = TS_FUNC_RE.exec(l);
      if (f) return add(f[2]!, 'function', i, depth);
      const k = TS_CONST_RE.exec(l);
      if (k) return add(k[2]!, 'function', i, depth);
      const e = TS_EXPORT_CONST_RE.exec(l);
      if (e) return add(e[2]!, 'const', i, depth);
      const m = TS_METHOD_RE.exec(l);
      if (m && depth > 0 && !NOT_METHOD.has(m[2]!)) add(m[2]!, 'method', i, depth);
      return;
    }
    if (isKt || lang === 'groovy') {
      const f = KT_FUN_RE.exec(l);
      if (f) return add(f[2]!, 'method', i, depth);
      if (lang === 'groovy') {
        const d = /^\s*(?:(?:public|private|protected|static)\s+)*def\s+([A-Za-z_$][\w$]*)\s*\(/.exec(l);
        if (d) return add(d[1]!, 'method', i, depth);
      }
    }
    const ctor = JAVA_CTOR_RE.exec(l);
    if (ctor) return add(ctor[2]!, 'method', i, depth);
    const m = JAVA_METHOD_RE.exec(l);
    if (m && !isKt) {
      const type = m[2]!.trim().split(/\s+/).pop() ?? '';
      if (!NOT_METHOD.has(m[3]!) && !NOT_METHOD.has(type) && !/^(else|return|new|throw|case|package|import)$/.test(m[2]!.trim().split(/\s+/)[0] ?? '')) {
        add(m[3]!, 'method', i, depth);
      }
    }
  });
  return out;
}

// ---------------------------------------------------------------- quick open

export interface QuickQuery {
  kind: 'file' | 'symbol';
  q: string;
  line: number;
}

/** ⌘P input: `Foo.java:120` = file + line, `#Foo` / `@Foo` = symbol search in the project */
export function quickQuery(input: string): QuickQuery {
  const s = input.trim();
  if (/^[#@]/.test(s)) return { kind: 'symbol', q: s.slice(1).trim(), line: 0 };
  const m = /^(.*?)(?::(\d+))?(?::\d+)?$/.exec(s);
  return { kind: 'file', q: m?.[1] ?? '', line: Number(m?.[2] ?? 0) };
}

/** `42`, `42:7` or `:42` of the go-to-line box; null when it is not a line */
export function parseLineCol(input: string): { line: number; col: number } | null {
  const m = /^\s*:?(\d+)(?:[:,](\d+))?\s*$/.exec(input);
  if (!m) return null;
  return { line: Number(m[1]), col: Number(m[2] ?? 0) };
}

// ---------------------------------------------------------------- tree keyboard

export interface TreeRow {
  depth: number;
  dir: boolean;
  open: boolean;
}

export type TreeAct = 'none' | 'toggle' | 'open';

/** arrow-key model of the tree: ↑↓ move, → expand / first child, ← collapse / parent, Enter open */
export function treeNav(rows: TreeRow[], i: number, key: string): { i: number; act: TreeAct } {
  const n = rows.length;
  if (!n) return { i: -1, act: 'none' };
  const cur = rows[i];
  if (!cur) return { i: key === 'ArrowUp' || key === 'End' ? n - 1 : 0, act: 'none' };
  switch (key) {
    case 'ArrowDown': return { i: Math.min(i + 1, n - 1), act: 'none' };
    case 'ArrowUp': return { i: Math.max(i - 1, 0), act: 'none' };
    case 'Home': return { i: 0, act: 'none' };
    case 'End': return { i: n - 1, act: 'none' };
    case 'PageDown': return { i: Math.min(i + 15, n - 1), act: 'none' };
    case 'PageUp': return { i: Math.max(i - 15, 0), act: 'none' };
    case 'ArrowRight':
      if (!cur.dir) return { i, act: 'none' };
      if (!cur.open) return { i, act: 'toggle' };
      return { i: (rows[i + 1]?.depth ?? -1) > cur.depth ? i + 1 : i, act: 'none' };
    case 'ArrowLeft': {
      if (cur.dir && cur.open) return { i, act: 'toggle' };
      for (let j = i - 1; j >= 0; j--) if (rows[j]!.depth < cur.depth) return { i: j, act: 'none' };
      return { i, act: 'none' };
    }
    case 'Enter': return { i, act: cur.dir ? 'toggle' : 'open' };
    default: return { i, act: 'none' };
  }
}

// ---------------------------------------------------------------- change markers

/** a relative link of a Markdown file, resolved against that file's directory; null when it leaves the project */
export function resolveRel(base: string, href: string): string | null {
  const clean = href.split('#')[0]!.split('?')[0]!;
  if (!clean) return null;
  const parts = clean.startsWith('/') ? [] : base.split('/').slice(0, -1);
  for (const p of clean.split('/')) {
    if (!p || p === '.') continue;
    if (p === '..') {
      if (!parts.length) return null;
      parts.pop();
    } else parts.push(p);
  }
  return parts.length ? parts.map((p) => { try { return decodeURIComponent(p); } catch { return p; } }).join('/') : null;
}

// ---------------------------------------------------------------- Files redesign helpers

/** the leading import block [first, last] (1-based) worth folding: ≥ 3 import lines, blank lines between allowed */
export function importBlock(lines: string[], lang: string): [number, number] | null {
  const re = lang === 'java' || lang === 'kotlin' || lang === 'groovy' ? /^\s*import\s/
    : lang === 'ts' ? /^\s*import[\s{*'"]/
      : lang === 'py' ? /^\s*(?:import|from)\s/ : null;
  if (!re) return null;
  let first = 0;
  let last = 0;
  let count = 0;
  for (let i = 0; i < lines.length && i < 3000; i++) {
    const l = lines[i]!;
    if (re.test(l)) {
      if (!first) first = i + 1;
      last = i + 1;
      count++;
      // a multi-line TS import: up to its closing `from '…'`
      if (lang === 'ts' && !/from\s+['"]|^\s*import\s+['"]/.test(l)) {
        while (i + 1 < lines.length && !/from\s+['"]/.test(lines[i]!)) i++;
        last = i + 1;
      }
    } else if (first && l.trim() && !/^\s*(\/\/|#)/.test(l)) break;
  }
  return count >= 3 && last > first ? [first, last] : null;
}

/** the outline items enclosing a line: a stack by depth (no end of a method is known - the next item closes it) */
export function scopeAt(items: OutlineItem[], line: number): OutlineItem[] {
  const stack: OutlineItem[] = [];
  for (const o of items) {
    if (o.line > line) break;
    if (o.kind === 'key' || o.kind === 'import') continue;
    while (stack.length && stack[stack.length - 1]!.depth >= o.depth) stack.pop();
    stack.push(o);
  }
  return stack;
}

// tooling noise (folded in the tree, left out of ⌘P / search unless asked) is one list on the server:
// projectfs.NOISE, served as DirEntry.noise

/** `src/main/java/Foo.java:123` (also `:123:4`) in output text: the path in group 1, the line in group 2.
 *  Terminal links and the conversation transcript share it (use matchAll: the regex is global). */
export const FILE_LINK = /(?:^|[\s('"`])((?:\/|\.{0,2}\/)?(?:[\w.@-]+\/)*[\w@-][\w.@-]*\.[A-Za-z0-9]{1,10}):(\d+)(?::\d+)?/g;

export interface FileRef { start: number; end: number; path: string; line: number }

/** the `path:line` references of a text (offsets cover `path:line`); URLs are not references */
export function fileRefs(text: string): FileRef[] {
  const out: FileRef[] = [];
  for (const m of text.matchAll(FILE_LINK)) {
    const file = m[1];
    if (!file || /^https?:/.test(file)) continue;
    const start = (m.index ?? 0) + m[0].indexOf(file);
    out.push({ start, end: start + file.length + 1 + m[2]!.length, path: file, line: Number(m[2]) });
  }
  return out;
}

/** every directory of a project known from its files and the listed folders, '.' first: the "Move to…" targets */
export function dirsOf(files: string[], dirs: Iterable<string> = []): string[] {
  const out = new Set<string>(['.']);
  const add = (d: string) => {
    const parts = d.split('/');
    for (let i = 1; i <= parts.length; i++) out.add(parts.slice(0, i).join('/'));
  };
  for (const f of files) if (f.includes('/')) add(f.slice(0, f.lastIndexOf('/')));
  for (const d of dirs) if (d && d !== '.') add(d);
  return ['.', ...[...out].filter((d) => d !== '.').sort()];
}

/** lower case that keeps every offset: a letter whose lower case is longer ('İ' → 'i̇') stays as it is -
 *  the spans index `text`, and a longer lower case shifted the paint off the match (N7-2) */
function lowerKeep(s: string): string {
  return Array.from(s, (c) => { const l = c.toLowerCase(); return l.length === c.length ? l : c; }).join('');
}

/** characters of `text` matched by a fuzzy query, in order (the ones ⌘P paints); null = no match */
export function matchSpans(query: string, text: string): [number, number][] | null {
  const q = lowerKeep(query).replace(/\s+/g, '');
  if (!q) return [];
  const t = lowerKeep(text);
  // a contiguous hit wins (Controller in ProfileController)
  const at = t.indexOf(q);
  if (at >= 0) return [[at, at + q.length]];
  // else word starts first: ProfCon -> Prof·ile Con·troller
  const out: [number, number][] = [];
  let i = 0;
  for (let k = 0; k < q.length;) {
    const ch = q[k]!;
    let j = t.indexOf(ch, i);
    // prefer the next word start (upper case / after a separator) holding the char
    for (let s = i; s < t.length; s++) {
      if (t[s] === ch && (s === 0 || /[A-Z]/.test(text[s]!) || /[^a-z0-9]/i.test(text[s - 1]!))) { j = s; break; }
    }
    if (j < 0) return null;
    let e = j + 1;
    k++;
    while (k < q.length && e < t.length && t[e] === q[k]) { e++; k++; }
    const last = out[out.length - 1];
    if (last && last[1] === j) last[1] = e;
    else out.push([j, e]);
    i = e;
  }
  return out;
}

export interface Ranked { path: string; score: number; spans: [number, number][]; inName: boolean }

/**
 * ⌘P ranking: the name over the path, a contiguous hit over scattered letters, then
 * changed > open > recent files, main code over tests, shorter paths first.
 */
export function rankFiles(query: string, files: string[], boost: { changed?: Set<string>; open?: Set<string>; recent?: string[] } = {}, limit = 60): Ranked[] {
  // N6: compared in NFC - a name written decomposed (NFD: macOS tools, archives) is found by what a keyboard types
  const q = query.trim().normalize('NFC');
  if (!q) return [];
  const recent = new Map((boost.recent ?? []).map((p, i) => [p, i]));
  const hasSlash = q.includes('/');
  const out: Ranked[] = [];
  for (const path of files) {
    const f = path.normalize('NFC');
    const slash = f.lastIndexOf('/');
    const name = f.slice(slash + 1);
    let spans = hasSlash ? null : matchSpans(q, name);
    let inName = !!spans;
    if (!spans) {
      spans = matchSpans(q, f);
      inName = false;
      if (!spans) continue;
    }
    const contiguous = spans.length === 1;
    let s = (inName ? 1000 : 300) + (contiguous ? 400 : 0) - spans.length * 30;
    if (inName && spans[0]?.[0] === 0) s += 150;
    if (inName && contiguous && spans[0]![1] - spans[0]![0] === name.replace(/\.[^.]+$/, '').length) s += 300;   // the whole base name
    if (boost.changed?.has(path) || boost.changed?.has(f)) s += 220;   // git reports the NFC name
    if (boost.open?.has(path) || boost.open?.has(f)) s += 160;
    const r = recent.get(path) ?? recent.get(f);
    if (r !== undefined) s += 140 - r * 8;
    if (/(^|\/)(src\/)?test(s)?\/|Test\.|\.test\.|\.spec\./.test(f)) s -= 180;
    s -= Math.min(120, f.length / 2);
    // offsets into the NFC form would not fit a decomposed path: no highlight rather than a wrong one
    const at = f !== path ? [] : inName ? spans.map(([a, b]) => [a + slash + 1, b + slash + 1] as [number, number]) : spans;
    out.push({ path, score: s, spans: at, inName });
  }
  return out.sort((a, b) => b.score - a.score || a.path.length - b.path.length).slice(0, limit);
}

/** a long path cut from the LEFT so the distinctive end (module, package) stays visible */
export function leftCut(path: string, max: number): string {
  if (path.length <= max) return path;
  const parts = path.split('/');
  let out = parts.pop()!;
  while (parts.length && out.length + parts[parts.length - 1]!.length + 2 <= max - 1) out = `${parts.pop()}/${out}`;
  return parts.length ? `…/${out}` : out;
}

/** the icon kind of a file name (CSS class fi-<kind>) */
export function fileKind(name: string): string {
  const n = name.toLowerCase();
  if (/\.(java|kt|kts|groovy|scala|go|py|ts|tsx|js|jsx|mjs|cjs|sh|bash|zsh)$/.test(n)) return 'code';
  if (/\.(ya?ml|properties|toml|ini|conf|cfg|env)$|^\.?env|^dockerfile|^makefile|^taskfile/.test(n)) return 'conf';
  if (/\.(json|jsonc|xml|xsd|pom|sql|csv)$/.test(n)) return 'data';
  if (/\.(md|markdown|txt|adoc|rst)$/.test(n)) return 'doc';
  if (/\.(http|rest)$/.test(n)) return 'http';
  if (/\.(png|jpe?g|gif|webp|svg|ico|bmp)$/.test(n)) return 'img';
  if (/\.(html?|css|scss|less|ftl|jsp|vue|svelte)$/.test(n)) return 'web';
  return 'file';
}
