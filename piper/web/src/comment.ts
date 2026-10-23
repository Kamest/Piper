// GitLab comment dialog: the draft a session wrote to
// the issue's comment file in development/ (or an empty editor) side by side with a live Markdown
// preview, lint hints for the user's rules (no @mentions of colleagues, a client summary on top
// and the technical detail in <details>), and one explicit "Send to GitLab" click
// (POST /api/inbox/comment). Nothing is ever sent without that click. Edits live in this
// browser (localStorage) until sent, so closing the dialog loses nothing.
// Rendering goes through markdown.ts (h() only, never innerHTML); <details>/<summary> lines
// become real <details> elements built with h(), any other HTML stays literal text.

import { isSend } from './sendkeys.js';
import { api } from './api.js';
import { kbd } from './clipkeys.js';
import { agoText, clear, fi, h } from './dom.js';
import { inlineText, parseInline, parseMarkdown, renderMarkdown } from './markdown.js';
import { markBidi } from './text.js';

export const COMMENT_MAX = 20000;

// ---------------------------------------------------------------- pure helpers (unit-tested)

export type RichNode =
  | { t: 'md'; lines: string[] }
  | { t: 'details'; summary: string; open: boolean; children: RichNode[] };

const FENCE_LINE = /^\s*(`{3,}|~{3,})/;
const DETAILS_OPEN = /^\s*<details(\s+open)?\s*>\s*(.*)$/i;
const DETAILS_CLOSE = /^\s*<\/details>\s*(.*)$/i;
const SUMMARY = /^\s*<summary>(.*?)<\/summary>\s*(.*)$/i;
const DETAILS_TAIL = /^(.*\S)\s*<\/details>\s*$/i;

/** split Markdown at <details> / </details> lines (outside code fences) into a tree; an unclosed
 *  <details> runs to the end, a stray </details> is kept as text */
export function splitDetails(src: string): RichNode[] {
  type Det = Extract<RichNode, { t: 'details' }>;
  const root: RichNode[] = [];
  const owners: Det[] = [];
  const cur = () => (owners.length ? owners[owners.length - 1]!.children : root);
  let md: string[] = [];
  let fence = '';
  let awaiting: Det | null = null;   // just opened: a <summary> may follow
  const flush = () => {
    if (md.length) cur().push({ t: 'md', lines: md });
    md = [];
  };
  const close = () => {
    flush();
    owners.pop();
    awaiting = null;
  };
  const feed = (line: string): void => {
    const f = FENCE_LINE.exec(line);
    if (fence) {
      if (f && f[1]![0] === fence[0] && f[1]!.length >= fence.length) fence = '';
      md.push(line);
      return;
    }
    if (f) {
      fence = f[1]!;
      awaiting = null;
      md.push(line);
      return;
    }
    const o = DETAILS_OPEN.exec(line);
    if (o) {
      flush();
      const node: Det = { t: 'details', summary: '', open: !!o[1], children: [] };
      cur().push(node);
      owners.push(node);
      awaiting = node;
      if (o[2]!.trim()) feed(o[2]!);
      return;
    }
    if (owners.length) {
      const c = DETAILS_CLOSE.exec(line);
      if (c) {
        close();
        if (c[1]!.trim()) feed(c[1]!);
        return;
      }
    }
    if (awaiting) {
      if (!line.trim()) return;
      const node = awaiting;
      awaiting = null;
      const s = SUMMARY.exec(line);
      if (s) {
        node.summary = s[1]!.trim();
        if (s[2]!.trim()) feed(s[2]!);
        return;
      }
    }
    const tail = owners.length ? DETAILS_TAIL.exec(line) : null;
    if (tail) {
      md.push(tail[1]!);
      close();
      return;
    }
    md.push(line);
  };
  for (const line of src.replace(/\r\n?/g, '\n').split('\n')) feed(line);
  flush();
  return root;
}

/** F24: a one-click fix of a hint (fixComment) */
export type LintFix = 'unmention' | 'details' | 'close';

export interface LintHint {
  level: 'error' | 'warn';
  text: string;
  fix?: LintFix;
}

export interface Lint {
  hints: LintHint[];
  /** @handles found outside code (the user's rule: never mention colleagues) */
  mentions: string[];
  /** sending is impossible (empty, too long) */
  blocked: boolean;
}

/** strip fenced blocks and inline code: a `@Autowired` in code is not a mention */
function withoutCode(src: string): string {
  const out: string[] = [];
  let fence = '';
  for (const line of src.split('\n')) {
    const f = FENCE_LINE.exec(line);
    if (fence) {
      if (f && f[1]![0] === fence[0] && f[1]!.length >= fence.length) fence = '';
      continue;
    }
    if (f) {
      fence = f[1]!;
      continue;
    }
    out.push(line.replace(/`[^`\n]*`/g, ' '));
  }
  return out.join('\n');
}

/** the user's rules for a GitLab comment (CLAUDE.md "Issue comments") */
export function lintComment(body: string): Lint {
  const hints: LintHint[] = [];
  const text = body.trim();
  if (!text) return { hints: [{ level: 'error', text: 'The comment is empty.' }], mentions: [], blocked: true };
  if (body.length > COMMENT_MAX) hints.push({ level: 'error', text: `It has ${body.length.toLocaleString()} characters, GitLab accepts at most ${COMMENT_MAX.toLocaleString()}.` });
  const mentions = [...new Set([...withoutCode(body).matchAll(/(^|[^\w.@/-])@([a-z0-9_][a-z0-9_.-]*[a-z0-9_]|[a-z0-9_])/gi)].map((m) => '@' + m[2]!))];
  if (mentions.length) hints.push({ level: 'warn', fix: 'unmention', text: `Mentions ${mentions.join(', ')} - your rules say no @ (it notifies).` });
  const plain = withoutCode(body);
  const opened = (plain.match(/<details(\s[^>]*)?>/gi) ?? []).length;
  const closed = (plain.match(/<\/details>/gi) ?? []).length;
  if (!opened) hints.push({ level: 'warn', fix: 'details', text: 'No <details> block - client summary on top, technical detail inside.' });
  else if (opened > closed) hints.push({ level: 'warn', fix: 'close', text: 'The <details> block is not closed - in GitLab it would swallow the rest of the comment.' });
  else if (opened !== closed) hints.push({ level: 'warn', text: 'A </details> has no <details> - check the blocks.' });
  return { hints, mentions, blocked: hints.some((x) => x.level === 'error') };
}

/** the template "Insert block" adds; the caret goes to its empty line (DETAILS_CARET characters from its start) */
export const DETAILS_TEMPLATE = '<details><summary>Technical detail</summary>\n\n\n\n</details>\n';
export const DETAILS_CARET = '<details><summary>Technical detail</summary>\n\n'.length;

/** F24: the one-click fix of a lint hint. unmention = every @handle outside code loses its @ (the
 *  name stays, nobody is notified; e-mails and code untouched); details = the template appended under the text;
 *  close = the missing </details> appended. `caret` = where the cursor goes after it. */
export function fixComment(body: string, fix: LintFix): { text: string; caret: number } {
  if (fix === 'details') {
    const head = body.trimEnd();
    const text = (head ? head + '\n\n' : '') + DETAILS_TEMPLATE;
    return { text, caret: (head ? head.length + 2 : 0) + DETAILS_CARET };
  }
  if (fix === 'close') {
    const text = body.trimEnd() + '\n\n</details>\n';
    return { text, caret: text.length };
  }
  const out: string[] = [];
  let fence = '';
  for (const line of body.split('\n')) {
    const f = FENCE_LINE.exec(line);
    if (fence || f) {
      if (fence) { if (f && f[1]![0] === fence[0] && f[1]!.length >= fence.length) fence = ''; }
      else fence = f![1]!;
      out.push(line);
      continue;
    }
    // odd parts are inline code
    out.push(line.split(/(`[^`\n]*`)/).map((part, k) => (k % 2 ? part
      : part.replace(/(^|[^\w.@/-])@([a-z0-9_][a-z0-9_.-]*[a-z0-9_]|[a-z0-9_])/gi, '$1$2'))).join(''));
  }
  const text = out.join('\n');
  return { text, caret: -1 };
}

// ---------------------------------------------------------------- rendering

/** a relative link in GitLab text: against the issue's project (/uploads/… lives under it) */
export function gitlabHref(href: string, webUrl: string): string | null {
  try {
    const base = webUrl.includes('/-/') ? webUrl.slice(0, webUrl.indexOf('/-/')) : webUrl;
    const url = href.startsWith('/uploads/') ? base + href : new URL(href, webUrl).href;
    return /^https?:\/\//i.test(url) ? url : null;
  } catch {
    return null;
  }
}

/** Markdown with real <details> blocks; relative links resolve against the GitLab URL */
export function renderRich(src: string, webUrl: string): HTMLElement {
  const openRel = (href: string) => {
    const url = gitlabHref(href, webUrl);
    if (url) window.open(url, '_blank', 'noopener');
  };
  const build = (nodes: RichNode[], into: HTMLElement) => {
    for (const n of nodes) {
      if (n.t === 'md') {
        const md = renderMarkdown(parseMarkdown(n.lines), { openRel });
        into.append(...[...md.childNodes]);
      } else {
        const d = h('details', { class: 'mddetails', open: n.open },
          h('summary', {}, n.summary ? inlineText(parseInline(n.summary)) : 'Details'));
        const body = h('div', { class: 'mddbody' });
        build(n.children, body);
        d.append(body);
        into.append(d);
      }
    }
  };
  const root = h('div', { class: 'mdv' });
  build(splitDetails(src), root);
  return root;
}

// ---------------------------------------------------------------- dialog

export interface CommentDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  toast(text: string, action?: { label: string; run(): void }): void;
}

export interface CommentOpts {
  key: string;
  /** "acme/shop #86 – title" */
  target: string;
  webUrl: string;
  text: string;
  /** where the text came from (the session's draft file) */
  source?: { path: string; mtime: number | null } | null;
  /** pending todos of the item: offered to be marked done after sending */
  todos: { action: string; author: string }[];
  /** POST /api/inbox/done for the key; answers how many were closed */
  markDone?: () => Promise<number>;
  /** after a successful send (the inbox refreshes its item) */
  onSent?: () => void;
}

function store(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** open the editor; Esc with unsent changes asks first (the text stays in this browser anyway) */
export function openComment(deps: CommentDeps, o: CommentOpts): void {
  const skey = `piper.comment.${o.key}`;
  let saved: { base: string; text: string } | null = null;
  try {
    saved = JSON.parse(store()?.getItem(skey) ?? 'null') as { base: string; text: string } | null;
  } catch {
    saved = null;
  }
  // an unsent edit is offered back even when the draft under it changed meanwhile (a session
  // rewrote the file): dropping it silently would contradict "the text stays in this browser"
  const restored = !!saved && saved.text !== o.text && !!saved.text.trim();
  const baseChanged = restored && saved!.base !== o.text;
  const ta = h('textarea', {
    class: 'mono', spellcheck: 'true', 'aria-label': 'comment text (Markdown)',
    placeholder: 'Client summary on top (address the client formally, no colleagues\' names).\n\n<details><summary>Technical detail</summary>\n\n…\n\n</details>',
  }) as HTMLTextAreaElement;
  ta.value = restored ? saved!.text : o.text;
  const preview = h('div', { class: 'cpreview hidden', 'aria-label': 'preview', 'aria-live': 'off', tabindex: '0' });
  const count = h('span', { class: 'ccount mut small' });
  const hints = h('ul', { class: 'chints' });
  const hintCount = h('span', { class: 'chcount mut small' });
  const status = h('div', { class: 'cstatus', role: 'status' });
  const guard = h('div', { class: 'cguard hidden', role: 'alert' });
  const send = h('button', { class: 'primary' }, 'Send to GitLab') as HTMLButtonElement;
  const cancel = h('button', { class: 'ghost', title: 'Esc' }, 'Cancel', h('kbd', {}, 'esc')) as HTMLButtonElement;
  let sending = false;
  let sent = false;
  let armedMention = false;
  let timer = 0;
  const dirty = () => !sent && ta.value !== o.text;

  // F24: one hint = one row with its fix (Remove @, Insert block, Close block); the count only near the limit
  const applyFix = (fix: LintFix) => {
    const r = fixComment(ta.value, fix);
    ta.value = r.text;
    ta.dispatchEvent(new Event('input'));
    showTab('write');
    ta.focus();
    if (r.caret >= 0) ta.setSelectionRange(r.caret, r.caret);
  };
  const FIX_LABEL: Record<LintFix, string> = { unmention: 'Remove @', details: 'Insert block', close: 'Close block' };
  let hintSig = '';
  const update = () => {
    const lint = lintComment(ta.value);
    const n = ta.value.length;
    count.textContent = markBidi(n > COMMENT_MAX * 0.8 ? `${n.toLocaleString()} / ${COMMENT_MAX.toLocaleString()} characters` : '');
    count.classList.toggle('bad', n > COMMENT_MAX);
    const sig = JSON.stringify(lint.hints);
    if (sig !== hintSig) {   // rebuilt only when it changes: typing must not drop a focused fix button
      hintSig = sig;
      clear(hints);
      for (const x of lint.hints) {
        hints.append(h('li', { class: x.level },
          h('span', { class: 'chdot', 'aria-hidden': 'true' }), h('span', { class: 'chtext' }, x.text),
          x.fix ? h('button', { class: 'small', onclick: () => applyFix(x.fix!) }, FIX_LABEL[x.fix]) : null));
      }
    }
    const warns = lint.hints.length;
    hintCount.textContent = markBidi(warns ? `${warns} rule ${warns === 1 ? 'hint' : 'hints'}` : '✓ by your rules');
    hintCount.classList.toggle('ok', !warns);
    send.disabled = sending || lint.blocked;
    if (!lint.mentions.length) armedMention = false;
    clear(send);
    if (sending) send.append('Sending…');
    else if (armedMention) send.append('Really send with the mention');
    else send.append('Send to GitLab', h('kbd', {}, kbd('⌘⏎', 'Ctrl')));
    send.className = armedMention ? 'danger' : 'primary';
  };
  // Write | Preview (GitHub's pattern); side by side only as an option in a wide window
  const tabWrite = h('button', { class: 'ctab', role: 'tab', 'aria-selected': 'true' }, 'Write') as HTMLButtonElement;
  const tabPreview = h('button', { class: 'ctab', role: 'tab', 'aria-selected': 'false' }, 'Preview') as HTMLButtonElement;
  const sideBtn = h('button', { class: 'ghost small cside', title: 'editor and preview side by side', 'aria-pressed': 'false' }, 'Side by side') as HTMLButtonElement;
  const tabNote = h('span', { class: 'ctabnote mut small' });
  let wide = false;
  const showTab = (t: 'write' | 'preview') => {
    tabWrite.setAttribute('aria-selected', String(t === 'write'));
    tabPreview.setAttribute('aria-selected', String(t === 'preview'));
    ta.classList.toggle('hidden', t === 'preview' && !wide);
    preview.classList.toggle('hidden', t === 'write' && !wide);
    if (t === 'preview') redraw();
    tabNote.textContent = markBidi(t === 'preview' ? 'as GitLab shows it' : `Markdown · ${o.source?.path ? `from ${o.source.path}${o.source.mtime ? ` · saved ${agoText(o.source.mtime)}` : ''}` : 'new comment'}`);
  };
  tabWrite.addEventListener('click', () => { showTab('write'); ta.focus(); });
  tabPreview.addEventListener('click', () => showTab('preview'));
  sideBtn.addEventListener('click', () => {
    wide = !wide;
    sideBtn.setAttribute('aria-pressed', String(wide));
    root.classList.toggle('wide', wide);
    showTab('write');
    redraw();
  });
  // the editor grows with the text (a two-line comment is a small dialog)
  const grow = () => {
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(Math.max(ta.scrollHeight + 2, 140), Math.round(window.innerHeight * 0.55))}px`;
  };
  const redraw = () => {
    const top = preview.scrollTop;
    clear(preview);
    preview.append(ta.value.trim() ? renderRich(ta.value, o.webUrl) : h('p', { class: 'mut' }, 'The preview shows up once you start typing.'));
    preview.scrollTop = top;
  };
  const persist = () => {
    try {
      if (ta.value === o.text || !ta.value.trim()) store()?.removeItem(skey);
      else store()?.setItem(skey, JSON.stringify({ base: o.text, text: ta.value }));
    } catch {
      /* private window: the edit lasts while the dialog is open */
    }
  };
  ta.addEventListener('input', () => {
    guard.classList.add('hidden');
    update();
    grow();
    clearTimeout(timer);
    timer = window.setTimeout(() => { redraw(); persist(); }, 150);
  });

  const close = () => {
    clearTimeout(timer);
    if (!sent) persist();
    deps.closeModal();
  };
  const askClose = () => {
    if (!dirty() || !guard.classList.contains('hidden')) return close();
    clear(guard);
    guard.append(h('span', {}, 'The comment is not sent. The unfinished text stays in this browser and is offered the next time you open it.'),
      h('button', { onclick: () => { guard.classList.add('hidden'); ta.focus(); } }, 'Keep writing'),
      h('button', { onclick: close }, 'Close'));
    guard.classList.remove('hidden');
    (guard.querySelector('button') as HTMLButtonElement | null)?.focus();
  };

  const doneBox = h('div', { class: 'cdone hidden' });
  const submit = async () => {
    const lint = lintComment(ta.value);
    if (sending || sent || lint.blocked) return;
    if (lint.mentions.length && !armedMention) {
      armedMention = true;
      update();
      send.focus();
      return;
    }
    sending = true;
    ta.readOnly = true;
    update();
    clear(status);
    status.append(h('span', { class: 'spinner' }), ' sending to GitLab…');
    try {
      const r = (await api.inboxComment(o.key, ta.value)).data;
      sent = true;
      try { store()?.removeItem(skey); } catch { /* ok */ }
      clear(status);
      showDone(r?.web_url ?? o.webUrl);
      deps.toast('Comment sent to GitLab', r?.web_url ? { label: 'Open ↗', run: () => window.open(r.web_url, '_blank', 'noopener') } : undefined);
      o.onSent?.();
    } catch (e) {
      clear(status);
      status.append(h('span', { class: 'bad' }, `Not sent: ${e instanceof Error ? e.message : String(e)}`), ' - the text stayed in the editor, try again.');
      ta.readOnly = false;
    } finally {
      sending = false;
      update();
    }
  };
  const showDone = (url: string) => {
    restoreNote?.remove();   // nothing left to restore once it is in GitLab
    editor.classList.add('hidden');
    foot.classList.add('hidden');
    tabsEl.classList.add('hidden');
    clear(doneBox);
    doneBox.classList.remove('hidden');
    doneBox.append(h('p', { class: 'ok big' }, '✓ The comment is in GitLab.'),
      h('p', {}, h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, 'Open the comment in GitLab ↗')));
    const row = h('div', { class: 'row' });
    if (o.todos.length && o.markDone) {
      const list = h('ul', { class: 'plain small mut' }, ...o.todos.map((t) => h('li', {}, `${t.action} · ${t.author}`)));
      const btn = h('button', { class: 'primary' }, `Mark to-do as done (${o.todos.length})`) as HTMLButtonElement;
      btn.addEventListener('click', () => {
        btn.disabled = true;
        btn.textContent = 'Closing…';
        o.markDone!().then((n) => {
          btn.replaceWith(h('span', { class: 'ok' }, `✓ Closed ${n} to-do in GitLab`));
          list.remove();
        }).catch((e: unknown) => {
          btn.disabled = false;
          btn.textContent = markBidi(`Try again: mark to-do as done (${o.todos.length})`);
          deps.toast(`Could not close the to-do: ${e instanceof Error ? e.message : String(e)}`);
        });
      });
      doneBox.append(h('p', { class: 'small' }, 'The issue still waits for you in a GitLab to-do. Close it now?'), list);
      row.append(btn);
    }
    const closeBtn = h('button', { onclick: close }, 'Close') as HTMLButtonElement;
    row.append(closeBtn);
    doneBox.append(row);
    (row.querySelector('button') as HTMLButtonElement | null)?.focus();
  };
  send.addEventListener('click', () => void submit());
  cancel.addEventListener('click', askClose);
  ta.addEventListener('keydown', (e) => {
    if (isSend(e, 'doc')) {
      e.preventDefault();
      void submit();
    }
  });

  const restoreNote = restored
    ? h('p', { class: 'warn small' }, baseChanged ? 'Restored your unfinished text from last time - the draft has changed since. ' : 'Restored your unfinished text from last time. ',
      h('button', { class: 'link', onclick: (e: Event) => { ta.value = o.text; (e.target as HTMLElement).parentElement?.remove(); ta.dispatchEvent(new Event('input')); } }, 'Restore the original text'))
    : null;
  const editor = h('div', { class: 'cbody' }, ta, preview);
  const tabsEl = h('div', { class: 'ctabs', role: 'tablist' }, tabWrite, tabPreview, h('span', { class: 'grow' }), tabNote,
    window.innerWidth >= 1200 ? sideBtn : null);
  const foot = h('div', { class: 'cfoot' }, hints, status,
    h('div', { class: 'row' }, hintCount, h('span', { class: 'grow' }), count, cancel, send));
  const xBtn = h('button', { class: 'icon ghost cx', 'aria-label': 'close', title: 'Esc', onclick: askClose }, fi('x', 'gi'));
  // F24: the target in the header, one line: "Comment on <issue> ↗"
  const root = h('div', { class: 'cdlg' },
    h('div', { class: 'ctarget' }, h('span', { class: 'mut' }, 'Comment on'),
      h('a', { href: o.webUrl, target: '_blank', rel: 'noopener noreferrer', title: 'open in GitLab' }, o.target + ' ↗'), h('span', { class: 'grow' }), xBtn),
    tabsEl,
    restoreNote, editor, foot, guard, doneBox);
  // Esc belongs to the dialog while there is something unsent (globalKeys would close it)
  root.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (sent || dirty() || !guard.classList.contains('hidden')) {   // sent: nothing to lose, one Esc
      e.preventDefault();
      e.stopPropagation();
      askClose();
    }
  });
  deps.modal(`Comment on ${o.target}`, root);
  update();
  showTab('write');
  grow();
  ta.focus();
  ta.setSelectionRange(0, 0);
}
