// Syntax highlighting for the file browser. A small rule-based
// lexer instead of a vendored library: no innerHTML (tokens become textContent spans, the
// rule of dom.ts), no dependency, state carried across lines (block comments, text blocks,
// template strings, XML comments). Good enough to read code, not a parser.

export type Lang = 'java' | 'kotlin' | 'ts' | 'xml' | 'json' | 'yaml' | 'props' | 'sql' | 'sh' | 'py' | 'css'
  | 'md' | 'groovy' | 'http' | 'plain';

export interface Tok {
  t: string;
  /** '' plain · k keyword · s string · c comment · n number · t type · f function · a annotation/attribute · g tag · p key/property · v variable · h heading/emphasis */
  c: string;
}

interface Rule {
  re: RegExp;
  c: string;
  /** state entered after this token */
  to?: string;
}

type Def = Record<string, Rule[]> & { start: Rule[] };

const EXT: Record<string, Lang> = {
  java: 'java', kt: 'kotlin', kts: 'kotlin', groovy: 'groovy', gradle: 'groovy', scala: 'java', cs: 'java', go: 'java',
  ts: 'ts', tsx: 'ts', js: 'ts', jsx: 'ts', mjs: 'ts', cjs: 'ts', vue: 'xml', svelte: 'xml',
  xml: 'xml', html: 'xml', htm: 'xml', xhtml: 'xml', xsd: 'xml', xsl: 'xml', svg: 'xml', jsp: 'xml', ftl: 'xml', vm: 'xml', pom: 'xml',
  json: 'json', jsonc: 'json', webmanifest: 'json', map: 'json',
  yml: 'yaml', yaml: 'yaml', properties: 'props', ini: 'props', env: 'props', conf: 'props', toml: 'props', cfg: 'props',
  sql: 'sql', sh: 'sh', bash: 'sh', zsh: 'sh', py: 'py', css: 'css', scss: 'css', less: 'css', md: 'md', markdown: 'md',
  http: 'http', rest: 'http',
};

export function langOf(path: string): Lang {
  const name = path.split('/').pop() ?? '';
  if (/^(Dockerfile|Makefile|Taskfile|\.?env(\..*)?|.*rc)$/i.test(name)) return name.toLowerCase().includes('env') ? 'props' : 'sh';
  const ext = name.includes('.') ? name.split('.').pop()!.toLowerCase() : '';
  return EXT[ext] ?? 'plain';
}

const words = (s: string) => new Set(s.split(/\s+/).filter(Boolean));

const KW: Partial<Record<Lang, Set<string>>> = {
  java: words(`abstract assert boolean break byte case catch char class const continue default do double else enum
    extends final finally float for goto if implements import instanceof int interface long native new package private
    protected public return short static strictfp super switch synchronized this throw throws transient try void volatile
    while var record sealed permits yield non-sealed true false null`),
  kotlin: words(`as break class continue do else false for fun if in interface is null object package return super this
    throw true try typealias typeof val var when while by catch constructor delegate dynamic field file finally get import
    init param property receiver set setparam where actual abstract annotation companion const crossinline data enum
    expect external final infix inline inner internal lateinit noinline open operator out override private protected
    public reified sealed suspend tailrec vararg`),
  groovy: words(`abstract as assert boolean break byte case catch char class const continue def default do double else
    enum extends false final finally float for goto if implements import in instanceof int interface long native new null
    package private protected public return short static super switch synchronized this throw throws trait true try void
    volatile while var`),
  ts: words(`abstract any as async await boolean break case catch class const constructor continue debugger declare
    default delete do else enum export extends false finally for from function get if implements import in infer
    instanceof interface is keyof let module namespace never new null number object of package private protected public
    readonly require return satisfies set static string super switch symbol this throw true try type typeof undefined
    unique unknown var void while with yield`),
  sql: words(`select from where and or not insert into values update set delete create table view index drop alter add
    column primary key foreign references join left right inner outer full on as group by order having limit offset
    distinct union all case when then else end null is in like between exists default constraint unique check begin
    commit rollback transaction grant revoke with returning cascade truncate if replace function procedure trigger
    varchar integer int bigint text boolean date timestamp numeric decimal serial char true false asc desc count sum avg
    min max coalesce`),
  http: words(`const let var function return if else for while new true false null undefined typeof this client response
    request`),
  sh: words(`if then else elif fi case esac for while until do done in function return local export readonly set unset
    shift exit break continue trap source echo printf cd test true false`),
  py: words(`False None True and as assert async await break class continue def del elif else except finally for from
    global if import in is lambda nonlocal not or pass raise return try while with yield self print`),
};

const STR_D = { re: /"(?:[^"\\]|\\.)*"?/y, c: 's' };
const STR_S = { re: /'(?:[^'\\]|\\.)*'?/y, c: 's' };
const NUM = { re: /(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)[lLfFdDnuU]*\b/y, c: 'n' };
const ID = { re: /[A-Za-z_$][\w$]*/y, c: 'id' };

function cLike(extra: Rule[] = [], tpl = false): Def {
  const start: Rule[] = [
    { re: /\/\/.*/y, c: 'c' },
    { re: /\/\*/y, c: 'c', to: 'block' },
    { re: /"""/y, c: 's', to: 'tblock' },
    ...(tpl ? [{ re: /`/y, c: 's', to: 'tpl' }] : []),
    STR_D, STR_S,
    { re: /@[A-Za-z_][\w.]*/y, c: 'a' },
    ...extra,
    NUM, ID,
  ];
  return {
    start,
    block: [{ re: /.*?\*\//y, c: 'c', to: 'start' }, { re: /.+/y, c: 'c' }],
    tblock: [{ re: /.*?"""/y, c: 's', to: 'start' }, { re: /.+/y, c: 's' }],
    tpl: [{ re: /(?:[^`\\$]|\\.|\$(?!\{))*`/y, c: 's', to: 'start' }, { re: /\$\{[^}]*\}/y, c: 'v' },
      { re: /(?:[^`\\$]|\\.|\$(?!\{))+/y, c: 's' }],
  };
}

const VAR = { re: /\{\{[^}]*\}\}/y, c: 'v' };
// IntelliJ's HTTP Client format: ### names, METHOD URL, headers, {{variables}}, @file vars,
// `< file` / `> handler.js`, and JavaScript inside `> {% … %}` / `< {% … %}`
const HTTP: Def = {
  start: [
    { re: /^###.*/y, c: 'h' },
    { re: /^\s*(?:#|\/\/).*/y, c: 'c' },
    { re: /^@[\w.-]+(?=\s*=)/y, c: 'v' },
    { re: /^\s*[<>]\s*\{%/y, c: 'g', to: 'js' },
    { re: /^<>\s.*|^[<>]\s+\S.*/y, c: 'a' },
    { re: /^(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|TRACE|CONNECT)\b/y, c: 'k' },
    { re: /^[\w-]+(?=:\s)/y, c: 'p' },
    VAR,
    { re: /[a-z][a-z0-9+.-]*:\/\/[^\s{]+/y, c: 's' },
    { re: /\s+|[^\s{]+|\{/y, c: '' },
  ],
  js: [
    { re: /%\}/y, c: 'g', to: 'start' },
    { re: /\/\/.*?(?=%\}|$)/y, c: 'c' },
    STR_D, STR_S, NUM, ID,
    { re: /[^%"'\w/]+|%(?!\})|\//y, c: '' },
  ],
};

const DEFS: Partial<Record<Lang, Def>> = {
  java: cLike(), kotlin: cLike(), groovy: cLike(), ts: cLike([], true), http: HTTP,
  xml: {
    start: [
      { re: /<!--/y, c: 'c', to: 'xcom' },
      { re: /<!\[CDATA\[/y, c: 'c', to: 'cdata' },
      { re: /<\?[\w:-]+|\?>/y, c: 'g' },
      { re: /<!?\/?[\w:.-]+/y, c: 'g', to: 'tag' },
      { re: /&[#\w]+;/y, c: 'n' },
      { re: /\$\{[^}]*\}/y, c: 'v' },
      { re: /[^<&$]+/y, c: '' },
    ],
    tag: [
      { re: /\/?>/y, c: 'g', to: 'start' },
      { re: /[\w:.@-]+(?=\s*=)/y, c: 'a' },
      STR_D, STR_S,
      { re: /[\w:.@-]+/y, c: 'a' },
      { re: /\s+|=/y, c: '' },
    ],
    xcom: [{ re: /.*?-->/y, c: 'c', to: 'start' }, { re: /.+/y, c: 'c' }],
    cdata: [{ re: /.*?\]\]>/y, c: 'c', to: 'start' }, { re: /.+/y, c: 'c' }],
  },
  json: {
    start: [
      { re: /"(?:[^"\\]|\\.)*"(?=\s*:)/y, c: 'p' },
      STR_D,
      { re: /\/\/.*/y, c: 'c' },
      { re: /\b(?:true|false|null)\b/y, c: 'k' },
      { re: /-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y, c: 'n' },
    ],
  },
  yaml: {
    start: [
      { re: /#.*/y, c: 'c' },
      { re: /^\s*(?:-\s+)?[\w./${}-]+(?=\s*:(\s|$))/y, c: 'p' },
      { re: /^\s*-\s+/y, c: '' },
      STR_D, STR_S,
      { re: /[&*][\w-]+/y, c: 'v' },
      { re: /\$\{[^}]*\}/y, c: 'v' },
      { re: /\b(?:true|false|null|yes|no|on|off)\b/y, c: 'k' },
      NUM,
      { re: /[^#"'&*$\d]+/y, c: '' },
    ],
  },
  props: {
    start: [
      { re: /^\s*[#!;].*/y, c: 'c' },
      { re: /^\s*\[[^\]]*\]/y, c: 'g' },
      { re: /^\s*[^=:\s]+(?=\s*[=:])/y, c: 'p' },
      { re: /\$\{[^}]*\}/y, c: 'v' },
      { re: /[^$]+/y, c: 's' },
    ],
  },
  sql: {
    start: [
      { re: /--.*/y, c: 'c' },
      { re: /\/\*/y, c: 'c', to: 'block' },
      STR_S,
      { re: /"(?:[^"])*"?/y, c: 'p' },
      NUM,
      { re: /[:@$][\w]+/y, c: 'v' },
      ID,
    ],
    block: [{ re: /.*?\*\//y, c: 'c', to: 'start' }, { re: /.+/y, c: 'c' }],
  },
  sh: {
    start: [
      { re: /(?<![^\s])#.*/y, c: 'c' },
      STR_D, STR_S,
      { re: /\$\{[^}]*\}|\$[\w@#?*!$-]+|\$\(/y, c: 'v' },
      { re: /(?:^|(?<=\s))-{1,2}[\w-]+/y, c: 'a' },
      NUM, ID,
    ],
  },
  py: {
    start: [
      { re: /#.*/y, c: 'c' },
      { re: /[rbfu]*"""/y, c: 's', to: 'dq3' },
      { re: /[rbfu]*'''/y, c: 's', to: 'sq3' },
      { re: /[rbfu]*"(?:[^"\\]|\\.)*"?/y, c: 's' },
      { re: /[rbfu]*'(?:[^'\\]|\\.)*'?/y, c: 's' },
      { re: /@[\w.]+/y, c: 'a' },
      NUM, ID,
    ],
    dq3: [{ re: /.*?"""/y, c: 's', to: 'start' }, { re: /.+/y, c: 's' }],
    sq3: [{ re: /.*?'''/y, c: 's', to: 'start' }, { re: /.+/y, c: 's' }],
  },
  css: {
    start: [
      { re: /\/\*/y, c: 'c', to: 'block' },
      { re: /\/\/.*/y, c: 'c' },
      STR_D, STR_S,
      { re: /[\w-]+(?=\s*:[^:{}]*[;}]|\s*:\s*$)/y, c: 'p' },
      { re: /#[0-9a-fA-F]{3,8}\b/y, c: 'n' },
      { re: /-?\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?/y, c: 'n' },
      { re: /[.#][\w-]+/y, c: 't' },
      { re: /@[\w-]+/y, c: 'k' },
      { re: /\$[\w-]+|var\(--[\w-]+\)|--[\w-]+/y, c: 'v' },
      { re: /[\w-]+/y, c: '' },
    ],
    block: [{ re: /.*?\*\//y, c: 'c', to: 'start' }, { re: /.+/y, c: 'c' }],
  },
  md: {
    start: [
      { re: /^```.*/y, c: 'c', to: 'fence' },
      { re: /^#{1,6}\s.*/y, c: 'h' },
      { re: /^\s*(?:[-*+]|\d+\.)\s/y, c: 'k' },
      { re: /^>.*/y, c: 'c' },
      { re: /`[^`]*`/y, c: 's' },
      { re: /\*\*[^*]+\*\*|__[^_]+__/y, c: 'h' },
      { re: /\[[^\]]*\]\([^)]*\)/y, c: 'a' },
      { re: /[^`*_[\n]+|./y, c: '' },
    ],
    fence: [{ re: /^```\s*$/y, c: 'c', to: 'start' }, { re: /.+/y, c: 's' }],
  },
};

function classify(word: string, lang: Lang, line: string, end: number): string {
  const kw = KW[lang];
  if (kw && (kw.has(word) || (lang === 'sql' && kw.has(word.toLowerCase())))) return 'k';
  if (lang === 'sql' || lang === 'sh') return '';
  if (/^[A-Z][A-Za-z0-9_]*[a-z][A-Za-z0-9_]*$/.test(word)) return 't';
  if (/^\s*\(/.test(line.slice(end, end + 4))) return 'f';
  return '';
}

/** tokens of every line; `state` flows from one line to the next */
export function highlightLines(lines: string[], lang: Lang): Tok[][] {
  const def = DEFS[lang];
  if (!def) return lines.map((l) => [{ t: l, c: '' }]);
  let state = 'start';
  const out: Tok[][] = [];
  for (const line of lines) {
    const toks: Tok[] = [];
    const push = (t: string, c: string) => {
      const last = toks[toks.length - 1];
      if (last && last.c === c) last.t += t;
      else toks.push({ t, c });
    };
    let pos = 0;
    // line-anchored rules (^) only match at the start: they are tried with a sub-string
    while (pos < line.length) {
      const rules = def[state] ?? def.start;
      let hit = false;
      for (const r of rules) {
        let m: RegExpExecArray | null;
        if (r.re.source.startsWith('^')) {
          if (pos !== 0) continue;
          r.re.lastIndex = 0;
          m = r.re.exec(line);
        } else {
          r.re.lastIndex = pos;
          m = r.re.exec(line);
        }
        if (!m || m.index !== pos || !m[0].length) continue;
        const text = m[0];
        push(text, r.c === 'id' ? classify(text, lang, line, pos + text.length) : r.c);
        pos += text.length;
        if (r.to) state = r.to;
        hit = true;
        break;
      }
      if (!hit) {
        push(line[pos]!, state === 'start' ? '' : (def[state]?.[def[state]!.length - 1]?.c ?? ''));
        pos++;
      }
    }
    // a // comment or string that closes the line does not continue; block states do
    out.push(toks.length ? toks : [{ t: '', c: '' }]);
  }
  return out;
}
