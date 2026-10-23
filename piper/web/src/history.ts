// File history, blame and the branch graph, read-only.
// One modal each, list on the left and the commit's diff on the right; opened from the Git window it is a
// layer over it (W11, deps.modal = the caller's). ↑↓ in the list walks the commits.
// Files hidden from Claude are shown here too (the GUI is the human's); everything here is
// textContent only.

import { api, commitDiff } from './api.js';
import { clear, h } from './dom.js';
import { numberDiff } from './gitpanel.js';
import { showBidi, markBidi } from './text.js';
import { highlightLines, langOf } from './highlight.js';
import { diffPaths } from './turns.js';
import type { BlameLine, Commit } from './types.js';

export interface HistoryDeps {
  toast(text: string): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  /** open a project-relative file at a line in Piper's editor (K3/K4: a path or line number of a commit diff) */
  openFile?(project: string, path: string, line: number): void;
}

const CHUNK = 400;           // blame rows per frame
const MAX_DIFF = 20000;      // lines of a commit diff drawn

/** 'now', '5 min', '3 h', '4 d', '2 mo', '3 y' */
export function relAge(time: number, now = Date.now() / 1000): string {
  const s = Math.max(0, now - time);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  if (s < 86400 * 60) return `${Math.floor(s / 86400)} d`;
  if (s < 86400 * 730) return `${Math.floor(s / (86400 * 30.4))} mo`;
  return `${Math.floor(s / (86400 * 365))} y`;
}

/** blame colour bucket: 0 < 1 week · 1 < 1 month · 2 < 3 months · 3 < 1 year · 4 < 3 years · 5 older */
export function ageBucket(time: number, now = Date.now() / 1000): number {
  const d = Math.max(0, now - time) / 86400;
  return d < 7 ? 0 : d < 30 ? 1 : d < 90 ? 2 : d < 365 ? 3 : d < 365 * 3 ? 4 : 5;
}

/** true where a blame line starts a run of the same commit (the gutter shows it once) */
export function blameRuns(lines: Pick<BlameLine, 'commit'>[]): boolean[] {
  return lines.map((l, i) => i === 0 || lines[i - 1]!.commit !== l.commit);
}

export interface Ref {
  name: string;
  kind: 'head' | 'local' | 'remote' | 'tag';
}

/** `HEAD -> main, origin/main, tag: v1` -> refs; remotes are the names seen as `<r>/HEAD` plus origin */
export function parseRefs(text: string, remotes: Set<string> = new Set(['origin'])): Ref[] {
  const out: Ref[] = [];
  for (const raw of text.split(',').map((x) => x.trim()).filter(Boolean)) {
    if (raw === 'HEAD') out.push({ name: 'HEAD', kind: 'head' });
    else if (raw.startsWith('HEAD -> ')) out.push({ name: raw.slice(8), kind: 'head' });
    else if (raw.startsWith('tag: ')) out.push({ name: raw.slice(5), kind: 'tag' });
    else if (raw.includes('/') && remotes.has(raw.slice(0, raw.indexOf('/')))) out.push({ name: raw, kind: 'remote' });
    else out.push({ name: raw, kind: 'local' });
  }
  return out;
}

export interface GraphRow {
  /** the lane drawing before the hash (`| * `) or the whole line when it has no commit */
  graph: string;
  hash: string;
  refs: string;
  subject: string;
}

/** one line of `git log --graph --oneline --decorate` */
export function parseGraphLine(line: string): GraphRow {
  const m = /^([*|\\/_. -]*?)([0-9a-f]{7,40})(?: \(([^)]*)\))?(?: (.*))?$/.exec(line);
  if (!m) return { graph: line, hash: '', refs: '', subject: '' };
  return { graph: m[1]!, hash: m[2]!, refs: m[3] ?? '', subject: m[4] ?? '' };
}

/** remote names of a graph: every `<r>/HEAD` ref, plus origin */
export function graphRemotes(rows: GraphRow[]): Set<string> {
  const out = new Set(['origin']);
  for (const r of rows) for (const m of r.refs.matchAll(/(?:^|, )([^\s,/]+)\/HEAD\b/g)) out.add(m[1]!);
  return out;
}

/** the lane drawing split into runs coloured by column (2 characters per lane, 6 colours) */
export function laneRuns(graph: string): { t: string; lane: number }[] {
  const out: { t: string; lane: number }[] = [];
  for (let i = 0; i < graph.length; i++) {
    const ch = graph[i]!;
    const lane = ch === ' ' ? -1 : Math.floor(i / 2) % 6;
    const last = out[out.length - 1];
    if (last && last.lane === lane) last.t += ch;
    else out.push({ t: ch, lane });
  }
  return out;
}

/** 9.111: the graph cell by cell - a commit (* = dot) and a passing lane (|) draw a line through the whole row, the
 *  diagonals (/ \\ _) stay glyphs; spaces keep their width */
export function laneCells(graph: string): { t: string; lane: number; kind: 'node' | 'v' | 'g' | 'sp' }[] {
  return [...graph].map((ch, i) => {
    const lane = Math.floor(i / 2) % 6;
    if (ch === '*' || ch === '●') return { t: '', lane, kind: 'node' as const };
    if (ch === '|') return { t: '', lane, kind: 'v' as const };
    if (ch === ' ') return { t: ' ', lane: -1, kind: 'sp' as const };
    return { t: ch, lane, kind: 'g' as const };
  });
}

/** F08: one neutral chip per ref, the kind as a coloured dot (HEAD = the checked-out branch, bold) */
export function refChip(r: Ref): HTMLElement {
  return h('span', { class: `ref ref-${r.kind}`, title: r.kind === 'head' ? 'HEAD (checked out)' : `${r.kind} ${r.kind === 'tag' ? '' : 'branch'}`.trim() },
    h('i', { class: 'ref-dot' }), showBidi(r.name));
}

function refChips(text: string, remotes?: Set<string>): HTMLElement[] {
  return parseRefs(text, remotes).map(refChip);
}

/**
 * a coloured unified diff into `pre` (plain view, no notes); with `open` the file headers and the new-side
 * line numbers open the file in Piper's editor (git paths; the caller maps them into the project)
 */
export function drawDiff(pre: HTMLElement, text: string, open: ((path: string, line: number, files: string[]) => void) | null = null): void {
  clear(pre);
  const lines = numberDiff(text);
  const paths = open ? diffPaths(lines) : [];
  lines.slice(0, MAX_DIFF).forEach((d, i) => {
    const file = paths[i] ?? '';
    // git's text (subjects, authors, code): a bidi control is a mark, not a reordering (Trojan Source)
    if (!open) return void pre.append(h('span', { class: d.cls }, (showBidi(d.text) || ' ') + '\n'));
    const no = file && d.newNo !== null ? d.newNo : null;
    const lno = no !== null
      ? h('button', { class: 'lno link', title: showBidi(`open ${file} at line ${no} (the file as it is now)`), onclick: () => open(file, no, paths) }, String(no))
      : h('span', { class: 'lno' });
    const body = file && d.text.startsWith('diff ')
      ? h('button', { class: 'link', title: showBidi(`open ${file} in Piper's editor`), onclick: () => open(file, 0, paths) }, showBidi(d.text))
      : showBidi(d.text) || ' ';
    pre.append(h('span', { class: d.cls }, lno, body, '\n'));
  });
  if (lines.length > MAX_DIFF) pre.append(h('span', { class: 'mut' }, `… truncated (${lines.length} lines)`));
  if (!text.trim()) pre.append('(no difference)');
}

/** list + diff pane with ↑↓ selection; `rows` are the list's selectable buttons */
function browser(project: string, open: ((path: string, line: number, files: string[]) => void) | null = null) {
  const list = h('div', { class: 'hlist', tabindex: 0 });
  const pre = h('pre', { class: 'diff hdiff' }, 'pick a commit');
  const cache = new Map<string, Promise<string>>();
  let rows: { el: HTMLElement; hash: string }[] = [];
  let at = -1;
  let timer = 0;
  const load = (hash: string) => {
    if (!cache.has(hash)) cache.set(hash, commitDiff(project, hash));
    pre.textContent = 'loading…';
    cache.get(hash)!.then((t) => { if (rows[at]?.hash === hash) drawDiff(pre, t, open); })
      .catch((e: unknown) => { cache.delete(hash); pre.textContent = markBidi(String(e instanceof Error ? e.message : e)); });
  };
  const select = (i: number, now = true) => {
    if (i < 0 || i >= rows.length) return;
    rows[at]?.el.classList.remove('active');
    at = i;
    const r = rows[i]!;
    r.el.classList.add('active');
    r.el.scrollIntoView({ block: 'nearest' });
    window.clearTimeout(timer);
    // walking the list with the keyboard fetches only where it stops
    if (now) load(r.hash);
    else timer = window.setTimeout(() => load(r.hash), 180);
  };
  list.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    e.preventDefault();
    select(Math.max(0, Math.min(rows.length - 1, at + (e.key === 'ArrowDown' ? 1 : -1))), false);
  });
  return {
    list, pre,
    setRows(r: { el: HTMLElement; hash: string }[]): void {
      rows = r;
      at = -1;
      r.forEach((x, i) => x.el.addEventListener('click', () => { select(i); list.focus({ preventScroll: true }); }));
    },
    /** select a commit by (short or full) hash; not in the list -> just show its diff */
    show(hash: string): void {
      const i = rows.findIndex((x) => x.hash.startsWith(hash) || hash.startsWith(x.hash));
      if (i >= 0) select(i);
      else {
        rows[at]?.el.classList.remove('active');
        at = -1;
        if (!cache.has(hash)) cache.set(hash, commitDiff(project, hash));
        pre.textContent = 'loading…';
        cache.get(hash)!.then((t) => drawDiff(pre, t, open)).catch((e: unknown) => { pre.textContent = markBidi(String(e instanceof Error ? e.message : e)); });
      }
    },
    first(): void { if (rows.length) select(0); },
  };
}

function commitRow(c: Commit): HTMLElement {
  return h('button', { class: 'hrow', title: showBidi(`${c.hash}\n${c.author} · ${c.date}`) },
    h('span', { class: 'hline' }, h('code', { class: 'hhash' }, c.short), ...(c.refs ? refChips(c.refs) : []), h('span', { class: 'hsubj' }, showBidi(c.subject))),
    h('span', { class: 'mut small' }, `${showBidi(c.author)} · ${relAge(Date.parse(c.date) / 1000)}`));
}

/** a commit diff's git path -> the project-relative path: a file's history knows the repository prefix
 *  (git path ending in `path`); the repository history assumes the project is the repository root */
export function historyRel(gitPath: string, path: string, gitPathOfFile: string): string {
  if (!path || !(gitPathOfFile === path || gitPathOfFile.endsWith('/' + path))) return gitPath;
  const prefix = gitPathOfFile.slice(0, gitPathOfFile.length - path.length);
  if (!prefix) return gitPath;
  return gitPath.startsWith(prefix) ? gitPath.slice(prefix.length) : '';
}

/** the opener of a history / graph diff, null without deps.openFile */
function opener(deps: HistoryDeps, project: string, path: string): ((gitPath: string, line: number, files: string[]) => void) | null {
  if (!deps.openFile) return null;
  return (gitPath, line, files) => {
    // the git path of the file whose history this is: it carries the repository prefix of the project
    const own = path ? files.find((f) => f === path || f.endsWith('/' + path)) ?? path : '';
    const rel = historyRel(gitPath, path, own);
    if (!rel) return deps.toast(`${gitPath} is outside the project`);
    deps.closeModal();
    deps.openFile!(project, rel, line);
  };
}

/**
 * history of one file (path) or the whole repository (path ''), with a Blame tab for a file;
 * `back` (opened from a review diff) puts a "← back to diff" button in the footer (C3)
 */
export function openHistory(deps: HistoryDeps, project: string, path: string, opts: { back?: () => void; commit?: string } = {}): void {
  const b = browser(project, opener(deps, project, path));
  const tabs = h('div', { class: 'row left htabs' });
  const commitsPane = h('div', { class: 'hist' }, b.list, b.pre);
  const blamePane = h('div', { class: 'blame hidden', tabindex: 0 }, 'loading…');
  let blameLoaded = false;
  const tab = (which: 'log' | 'blame') => {
    commitsPane.classList.toggle('hidden', which !== 'log');
    blamePane.classList.toggle('hidden', which !== 'blame');
    for (const x of tabs.children) x.classList.toggle('on', (x as HTMLElement).dataset.tab === which);
    if (which === 'blame' && !blameLoaded) {
      blameLoaded = true;
      loadBlame();
    }
  };
  if (path) {
    tabs.append(h('button', { class: 'tab on', 'data-tab': 'log', onclick: () => tab('log') }, 'Commits'),
      h('button', { class: 'tab', 'data-tab': 'blame', onclick: () => tab('blame') }, 'Blame'));
  }
  deps.modal(path ? `History: ${path}` : 'Repository history', path ? tabs : null, commitsPane, blamePane,
    h('div', { class: 'row' }, opts.back ? h('button', { onclick: opts.back }, '← back to diff') : null,
      h('span', { class: 'mut small grow' }, `↑↓ in the list · click a hash in blame to show the commit${deps.openFile ? ' · a line number opens the file' : ''}`),
      h('button', { onclick: deps.closeModal }, 'Close')));
  b.list.append(h('p', { class: 'mut' }, 'loading…'));
  api.fileLog(project, path, 100).then((r) => {
    const commits = r.data?.commits ?? [];
    clear(b.list);
    const rows = commits.map((c) => ({ el: commitRow(c), hash: c.hash }));
    b.list.append(...rows.map((x) => x.el));
    if (!commits.length) b.list.append(h('p', { class: 'mut' }, 'No commits.'));
    b.setRows(rows);
    if (opts.commit) b.show(opts.commit); else b.first();
    b.list.focus({ preventScroll: true });
  }).catch((e: unknown) => {
    clear(b.list);
    b.list.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
  });

  function loadBlame(): void {
    api.blame(project, path).then((r) => {
      const lines = r.data?.lines ?? [];
      clear(blamePane);
      if (!lines.length) {
        blamePane.append(h('p', { class: 'mut' }, 'Empty file.'));
        return;
      }
      const runs = blameRuns(lines);
      const toks = highlightLines(lines.map((l) => l.text), langOf(path));
      const now = Date.now() / 1000;
      let i = 0;
      // drawn in chunks: a long file never blocks the UI
      const step = () => {
        if (!blamePane.isConnected) return;
        const end = Math.min(i + CHUNK, lines.length);
        const frag = document.createDocumentFragment();
        for (; i < end; i++) {
          const l = lines[i]!;
          const age = l.commit ? `age-${ageBucket(l.time, now)}` : 'age-new';
          const gut = h('span', { class: `bg ${age}${runs[i] ? ' first' : ''}`, title: l.commit ? showBidi(`${l.commit} · ${l.author} · ${l.summary}`) : 'not committed' });
          if (runs[i]) {
            gut.append(l.commit
              ? h('button', { class: 'link hhash', onclick: () => { tab('log'); b.show(l.commit); } }, l.commit.slice(0, 8))
              : h('span', { class: 'mut' }, 'not committed'),
            h('span', { class: 'bauth' }, l.commit ? showBidi(l.author) : ''), h('span', { class: 'bage' }, l.commit ? relAge(l.time, now) : ''));
          }
          const code = h('span', { class: 'tx' });
          for (const t of toks[i] ?? []) code.append(t.c ? h('span', { class: 'hl-' + t.c }, showBidi(t.t)) : showBidi(t.t));
          frag.append(h('div', { class: 'bl' }, gut, h('span', { class: 'no' }, String(l.line)), code));
        }
        blamePane.append(frag);
        if (i < lines.length) window.requestAnimationFrame(step);
      };
      step();
    }).catch((e: unknown) => {
      clear(blamePane);
      blamePane.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
    });
  }
}

