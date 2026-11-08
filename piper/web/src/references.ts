// Session output is untrusted text. Recognition never runs a request or a command;
// project-relative targets are resolved by the GUI only after the user's click.
export type Reference =
  | { kind: 'file'; path: string; line: number; diagnostic?: boolean }
  | { kind: 'db'; name: string }
  | { kind: 'commit'; hash: string }
  | { kind: 'gitlab'; resource: 'issues' | 'merge_requests' | 'pipelines'; id: string; project?: string }
  | { kind: 'session'; id: string };
export interface ReferenceSpan { start: number; end: number; text: string; ref: Reference }
const EXT = /\.(?:[cm]?[jt]sx?|java|kt|kts|py|go|rs|rb|php|c|h|cpp|hpp|cs|swift|scala|sh|bash|zsh|fish|sql|http|rest|json|jsonl|ya?ml|xml|toml|ini|cfg|conf|properties|env|lock|md|txt|log|csv|tsv|html?|css|scss|vue|svelte|gradle|patch|diff|png|jpe?g|gif|webp|svg|ico|bmp|pdf|zip|gz|xlsx?|docx?|pptx?)$/i;

/** An explicit repository wins; otherwise a linked issue identifies its own number, never a same-number MR. */
export function referenceProjects(ref: Extract<Reference, { kind: 'gitlab' }>, session: {
  gitlab?: string[]; gitlab_extra?: string[]; issue?: { gl: string; iid: number; type: string } | null;
}): string[] {
  if (ref.project) return [ref.project];
  const issue = session.issue;
  if (issue && !issue.gl.startsWith('gh:') && String(issue.iid) === ref.id
    && (issue.type === 'mr' ? 'merge_requests' : 'issues') === ref.resource) return [issue.gl];
  return [...new Set([...(session.gitlab ?? []), ...(session.gitlab_extra ?? [])])];
}

/** TUI word wrapping emits real line breaks after '-' and '/', unlike xterm's soft wraps.
 * Keep displayed offsets while joining only a continuous reference token. Ordinary newlines,
 * paragraphs and independently rooted references retain their boundaries. */
function terminalText(text: string, fullWidthBreaks: ReadonlySet<number>): { value: string; offsets: number[] } {
  const chars: string[] = [], offsets: number[] = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] === '\n') {
      let end = chars.length, next = i + 1;
      while (end && /[ \t\r]/.test(chars[end - 1]!)) end--;
      while (/[ \t]/.test(text[next] ?? '') && next - i <= 17) next++;
      let start = end;
      while (start && /[^\s"'`<>()]/.test(chars[start - 1]!)) start--;
      const left = chars.slice(start, end).join('');
      const right = /^[^\s"'`<>()]+/.exec(text.slice(next))?.[0] ?? '';
      const url = /^(?:https?|file):\/\//i.test(left);
      const punctuation = /[-/]$/.test(left) || url && /[?&=#%]$/.test(left);
      const candidate = (left + right).replace(/[.,;!?]+$/, '');
      const continuous = url || references(candidate).some(hit => hit.ref.kind === 'file' && hit.start === 0 && hit.end === candidate.length);
      if (right && next - i <= 17 && !/^(?:\.{0,2}\/|~\/|(?:https?|file):\/\/)/i.test(right)
        && (punctuation || fullWidthBreaks.has(i) && (url || left.includes('/')) && !EXT.test(left)) && continuous) {
        chars.length = end; offsets.length = end;
        i = next - 1;
        continue;
      }
    }
    chars.push(text[i]!); offsets.push(i);
  }
  return { value: chars.join(''), offsets };
}

/** Terminal providers include whole soft wraps and bounded, explicit TUI hard-wrap continuations. */
export function terminalReferences(text: string, fullWidthBreaks: ReadonlySet<number> = new Set()): (Omit<ReferenceSpan, 'ref'> & { ref: Reference | { kind: 'url'; url: string } })[] {
  const { value, offsets } = terminalText(text, fullWidthBreaks);
  const out: (Omit<ReferenceSpan, 'ref'> & { ref: Reference | { kind: 'url'; url: string } })[] = references(value);
  for (const hit of value.matchAll(/(?:https?|file):\/\/[^\s<>"`]+/gi)) {
    let raw = hit[0].replace(/[.,;!?]+$/, '');
    while (raw.endsWith(')') && (raw.match(/\)/g)?.length ?? 0) > (raw.match(/\(/g)?.length ?? 0)) raw = raw.slice(0, -1);
    try {
      const url = new URL(raw);
      if (url.username || url.password) continue;
      const ref = url.protocol === 'file:' ? fileUrlReference(raw) : { kind: 'url' as const, url: raw };
      out.push({ start: hit.index!, end: hit.index! + raw.length, text: raw, ref });
    } catch { /* An invalid or non-local file URL never becomes a guessed local path. */ }
  }
  return out.map(hit => ({ ...hit, start: offsets[hit.start]!, end: offsets[hit.end - 1]! + 1 })).sort((a, b) => a.start - b.start);
}

/** Human references may prefix a file with a registered project key or checkout folder name.
 * Callers first check the emitting checkout and filter projects to the same host. */
export function projectFileTargets(path: string, projects: { key: string; path: string }[]): { project: string; path: string }[] {
  if (/^(?:\/|\.{1,2}\/|~\/)/.test(path)) return [];
  const found: { project: string; path: string; prefix: number }[] = [];
  for (const project of projects) {
    const root = project.path.replace(/\/$/, '');
    for (const prefix of new Set([project.key, root.split('/').at(-1)!])) {
      if (!prefix || !path.startsWith(prefix + '/')) continue;
      const relative = relativeFile(path.slice(prefix.length + 1), root);
      if (relative !== null) found.push({ project: project.key, path: root + '/' + relative, prefix: prefix.length });
    }
  }
  const longest = Math.max(0, ...found.map(x => x.prefix));
  return [...new Map(found.filter(x => x.prefix === longest).map(({ project, path }) => [path, { project, path }])).values()];
}

export function references(text: string): ReferenceSpan[] {
  const out: ReferenceSpan[] = [];
  const urls = [...text.matchAll(/(?:https?|file|piper):\/\/[^\s<>"`]+/gi)].map(m => [m.index!, m.index! + m[0].length]);
  const add = (start: number, value: string, ref: Reference) => {
    const end = start + value.length;
    if ([...urls, ...out.map(x => [x.start, x.end])].some(([a, b]) => start < b! && end > a!)) return;
    out.push({ start, end, text: value, ref });
  };
  for (const m of text.matchAll(/\b(issue|MR|merge request|pipeline)\s+([#!]?\d+)\b/gi)) {
    add(m.index!, m[0], { kind: 'gitlab', resource: /^pipeline$/i.test(m[1]!) ? 'pipelines' : /^(MR|merge request)$/i.test(m[1]!) ? 'merge_requests' : 'issues', id: m[2]!.replace(/^[#!]/, '') });
  }
  for (const m of text.matchAll(/(?:^|[\s(`])((?:[\w.-]+\/[\w./-]+)?([#!])(\d+))\b/g)) {
    const raw = m[1]!, project = raw.slice(0, raw.lastIndexOf(m[2]!));
    add(m.index! + m[0].indexOf(raw), raw, { kind: 'gitlab', resource: m[2] === '!' ? 'merge_requests' : 'issues', id: m[3]!, ...(project ? { project } : {}) });
  }
  for (const m of text.matchAll(/\bsession\s+([a-f\d]{6,64})\b/gi)) add(m.index!, m[0], { kind: 'session', id: m[1]! });
  for (const m of text.matchAll(/(?:^|[\s(`])([0-9a-f]{7,40})(?!\.[\w])(?=$|[\s)`.,;:])/g)) {
    if (/[a-f]/.test(m[1]!)) add(m.index! + m[0].indexOf(m[1]!), m[1]!, { kind: 'commit', hash: m[1]! });
  }
  // Quoted paths include spaces. Bare paths include Unicode names, dotfiles and extensionless build files.
  const file = (raw: string, start: number) => {
    const m = /^(.*?)(?::(\d+)(?::\d+)?|#L(\d+)|\((\d+)(?:,\d+)?\)|:\[(\d+),\d+\])?$/.exec(raw)!;
    const path = m[1]!;
    if (!path || /^(?:https?|file|piper):/i.test(path)) return;
    if (path.includes('/') || EXT.test(path) || /^(?:Dockerfile|Makefile|README|LICENSE|\.env(?:\.[\w-]+)?|\.gitignore)$/i.test(path)) {
      const line = Number(m[2] || m[3] || m[4] || m[5] || 0);
      const end = text.indexOf('\n', start);
      const context = text.slice(text.lastIndexOf('\n', start) + 1, end < 0 ? text.length : end);
      add(start, raw, { kind: 'file', path, line, ...(line && /\b(?:error|warning)\b/i.test(context) ? { diagnostic: true } : {}) });
    }
  };
  for (const m of text.matchAll(/(["'`])([^\n"'`]+)\1(?::\d+(?::\d+)?|, line \d+)?/g)) {
    const suffix = m[0].slice(m[2]!.length + 2).replace(/^, line (\d+)$/, ':$1');
    // Include quotes in the clickable span, but not in the resolved path.
    const before = out.length;
    file(m[2]! + suffix, m.index!);
    if (out.length > before) { out.at(-1)!.text = m[0]; out.at(-1)!.end = m.index! + m[0].length; }
    else if (m[1] === '`' && /^[\p{L}_][\p{L}\p{N}_$]*$/u.test(m[2]!)) add(m.index!, m[0], { kind: 'db', name: m[2]! });
  }
  for (const m of text.matchAll(/(?:^|[\s(["'`=])((?:\.{0,2}\/|\/)?[\p{L}\p{N}\p{M}\p{S}\u200D\uFE0F_@.~-]+(?:\/[\p{L}\p{N}\p{M}\p{S}\u200D\uFE0F_@.~-]+)*\/?(?::\d+(?::\d+)?|#L\d+|\(\d+(?:,\d+)?\)|:\[\d+,\d+\])?)/gu)) {
    const raw = m[1]!.replace(/[.,;]+$/, '');
    file(raw, m.index! + m[0].indexOf(m[1]!));
  }
  // Bare table names require an SQL/table cue; qualified schema.table is unambiguous enough to resolve on click.
  for (const m of text.matchAll(/\b(?:FROM|JOIN|UPDATE|INTO|TABLE|tabulka|tabulky)\s+([`"]?[\p{L}_][\p{L}\p{N}_$]*[`"]?(?:\.[`"]?[\p{L}_][\p{L}\p{N}_$]*[`"]?)?)/giu)) {
    const raw = m[1]!;
    add(m.index! + m[0].lastIndexOf(raw), raw, { kind: 'db', name: raw.replace(/[`"]+/g, '') });
  }
  for (const m of text.matchAll(/(?:^|[\s(`])([\p{L}_][\p{L}\p{N}_$]*\.[\p{L}_][\p{L}\p{N}_$]*)\b(?![.(])/gu))
    add(m.index! + m[0].indexOf(m[1]!), m[1]!, { kind: 'db', name: m[1]! });
  return out.sort((a, b) => a.start - b.start);
}

/** Exact origins only: localhost on another port, lookalike hosts and embedded credentials are not Piper. */
export function piperUrl(raw: string, here: string, origins: string[] = [], mobile = false): URL | null {
  try {
    const u = new URL(raw, here);
    if (u.username || u.password || !/^https?:$/.test(u.protocol)) return null;
    const current = new URL(here).origin;
    // The public mobile homepage is also an ordinary web link in agent answers.
    // Explicit routes (including pairing fragments) and desktop aliases stay internal.
    if (mobile && u.origin === current && u.pathname === '/' && !u.search && !u.hash) return null;
    return [current, ...origins].includes(u.origin) ? u : null;
  } catch { return null; }
}

export function relativeFile(path: string, root: string): string | null {
  const base = root.replace(/\/$/, '');
  if (path.startsWith('/')) {
    if (path === base) return '.';
    if (!path.startsWith(base + '/')) return null;
    path = path.slice(base.length + 1);
  }
  const parts: string[] = [];
  for (const part of path.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') { if (!parts.length) return null; parts.pop(); }
    else parts.push(part);
  }
  return parts.join('/') || '.';
}

export function fileMatches(files: string[], path: string): string[] {
  if (files.includes(path)) return [path];
  const n = path.normalize('NFC');
  return files.filter(f => f.normalize('NFC') === n || f.normalize('NFC').endsWith('/' + n));
}

export function gitlabUrl(host: string, project: string, resource: 'issues' | 'merge_requests' | 'pipelines', id: string): string {
  const base = new URL(host.includes('://') ? host : `https://${host}`);
  if (!/^https?:$/.test(base.protocol) || base.username || base.password || !/^\d+$/.test(id) || !/^[\w.-]+(?:\/[\w.-]+)+$/.test(project)) throw new Error('Invalid GitLab reference');
  return `${base.origin}/${project.split('/').map(encodeURIComponent).join('/')}/-/${resource}/${id}`;
}

export interface PiperTarget { session: string; view: string; path: string; line: number; connection: string; table: string; commit: string }
/** Stable shareable routes use /?session=<id>&view=files&path=...&line=...; path/hash forms are accepted too. */
export function piperTarget(url: URL): PiperTarget {
  const hash = url.hash.slice(1);
  const hashPath = hash.startsWith('/') ? new URL(hash, url.origin) : null;
  const q = new URLSearchParams(url.search);
  for (const [k, v] of (hashPath?.searchParams ?? new URLSearchParams(hash))) if (k !== 'token') q.set(k, v);
  const path = (hashPath ?? url).pathname;
  const match = /^\/(?:sessions?|s)\/([^/]+)(?:\/([^/]+))?\/?$/.exec(path);
  return { session: q.get('session') || q.get('sid') || (match ? decodeURIComponent(match[1]!) : ''),
    view: q.get('view') || match?.[2] || '', path: q.get('path') || q.get('file') || '',
    line: Math.max(0, Number(q.get('line')) || 0), connection: q.get('connection') || '',
    table: q.get('table') || '', commit: q.get('commit') || '' };
}


export function fileUrlReference(raw: string): Reference {
  const url = new URL(raw);
  if (url.protocol !== 'file:' || url.host && url.host !== 'localhost') throw new Error('Only local file links can be opened');
  const at = /^(.*?):(\d+)(?::\d+)?$/.exec(url.pathname);
  return { kind: 'file', path: decodeURIComponent(at?.[1] ?? url.pathname),
    line: Number(url.hash.replace(/^#L?/, '')) || Number(at?.[2]) || 0 };
}
