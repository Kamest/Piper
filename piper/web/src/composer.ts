// Composer: a multi-line input under the terminal with
// templates and history. ⌘⏎ sends to the session (piperd types it as a bracketed paste
// + Enter, audited). Templates and history are per-browser conveniences (localStorage).
// D7: the only text input for a session - collapsed (target, 2 rows, ⏎ sends) it is the Conversation's reply
// bar and the queue's Reply r; the expand icon / ⌘E unfold it with the templates and To all….

import { isSend, sendHint } from './sendkeys.js';
import { kbd } from './clipkeys.js';
import { clear, fi, h } from './dom.js';
import { cutText, markBidi } from './text.js';

const HIST_KEY = 'piper.composer.history';
const DRAFT_KEY = 'piper.composer.draft';   // sessionStorage: survives a reload of this tab, never shows in another
const TPL_KEY = 'piper.composer.templates';
const HIST_MAX = 50;
const QUEUE_KEY = 'piper.composer.queue';   // sessionStorage, like the draft: a reload keeps (and still delivers) it
const QUEUE_TICK = 1000;
const QUEUE_COOL = 3000;   // after a delivery: the session has time to start working before the next one goes

/** F05: a message held while its session works - a card above the field (Edit / ✕), delivered when the turn ends */
export interface Queued { id: string; to: string; text: string; failed?: string }

export function enqueue(q: Queued[], to: string, text: string, id = String(Date.now()) + Math.random().toString(36).slice(2, 6)): Queued[] {
  return [...q, { id, to, text }];
}

/** the next message to deliver: the oldest not failed one whose session is idle (one per session, oldest first) */
export function nextReady(q: Queued[], busy: (id: string) => boolean, cooling: (id: string) => boolean = () => false): Queued | null {
  const seen = new Set<string>();
  for (const x of q) {
    if (seen.has(x.to)) continue;
    seen.add(x.to);
    if (!x.failed && !busy(x.to) && !cooling(x.to)) return x;
  }
  return null;
}

/** Edit: the message leaves the queue, its text goes back into the field */
export function takeQueued(q: Queued[], id: string): { q: Queued[]; text: string } {
  const x = q.find((y) => y.id === id);
  return { q: q.filter((y) => y.id !== id), text: x?.text ?? '' };
}

export const DEFAULT_TEMPLATES: { name: string; text: string }[] = [
  { name: 'Run tests', text: 'Run the whole relevant test suite and summarize the result (what passed, what did not and why).' },
  { name: 'Verify and summarize', text: 'Verify that the change does what it should (build + tests), and briefly summarize what you changed and what remains unverified.' },
  { name: 'Diff review', text: 'Go through the git_diff of your changes critically as a reviewer: bugs, missing tests, unnecessary changes.' },
  { name: 'Status', text: 'Where are you? What is done, what remains and what are you waiting on from me?' },
];

function load<T>(key: string, dflt: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : dflt;
  } catch {
    return dflt;
  }
}

function save(key: string, v: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(v));
  } catch {
    /* private window: history just is not kept */
  }
}

/** newest first, no duplicates, at most HIST_MAX */
export function pushHistory(list: string[], text: string): string[] {
  return [text, ...list.filter((x) => x !== text)].slice(0, HIST_MAX);
}

export interface ComposerDeps {
  /** the session shown now: the composer binds to it when opened (or while empty) */
  current(): string;
  title(id: string): string;
  /** live sessions other than `id`, for "send to all" */
  others(id: string): { id: string; title: string }[];
  sendTo(id: string, text: string): Promise<boolean>;
  confirm(title: string, text: string, ok: () => void): void;
  toast(text: string): void;
  /** the session works now: the text waits in its prompt until the turn ends */
  busy?(id: string): boolean;
  /** the session still takes text (a pinned reply whose session ended lets go of it) */
  live?(id: string): boolean;
  /** Esc / a sent reply closed it: the keyboard goes back where it was */
  closed?(): void;
}

/** the placeholder: where the text goes and that a working session queues it; full also says what sends it (D7) */
export function composerHint(title: string, compact: boolean, busy: boolean): string {
  if (compact) return busy ? 'Claude is working - queued until the turn ends' : `Reply to ${title}…`;
  return `Message for the session${busy ? ' - Claude is working: queued until the turn ends' : ''} · ${sendHint('doc')} · ⌥↑/⌥↓ history`;
}

/** the collapsed composer's key line under the text */
export const COMPACT_KEYS = `${sendHint('chat')} · Esc close`;

/** the unsent text and its target session (a reload must not lose a written message) */
function draftStore(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function createComposer(deps: ComposerDeps) {
  const el = h('div', { class: 'composer hidden' });
  const ta = h('textarea', { rows: 4 }) as HTMLTextAreaElement;
  const tpl = h('select', { title: 'templates' }) as HTMLSelectElement;
  const target = h('span', { class: 'ctarget-label small' });
  let hist = load<string[]>(HIST_KEY, []);
  let hpos = -1;
  let draft = '';      // what was typed before ⌥↑ started walking the history: ⌥↓ past the newest gives it back
  let bound = '';      // the session a draft is written for: switching the view does not retarget it
  let pinned = false;  // Reply r: bound to that session even while empty, until sent / closed / it ends
  let compact = false;
  let stash: { to: string; text: string } | null = null;   // the draft for another session a reply put aside
  let sending = false;
  let queue: Queued[] = [];
  const sentAt = new Map<string, number>();
  try {
    const raw = JSON.parse(draftStore()?.getItem(QUEUE_KEY) ?? '[]') as unknown;
    if (Array.isArray(raw)) queue = raw.filter((x): x is Queued => !!x && typeof x.id === 'string' && typeof x.to === 'string' && typeof x.text === 'string');
  } catch { /* no queue */ }
  try {
    const d = JSON.parse(draftStore()?.getItem(DRAFT_KEY) ?? 'null') as { text?: string; to?: string } | null;
    if (d?.text) {
      ta.value = d.text;
      bound = d.to ?? '';
      el.classList.remove('hidden');
    }
  } catch { /* no draft */ }
  const keepDraft = () => {
    try {
      if (ta.value.trim()) draftStore()?.setItem(DRAFT_KEY, JSON.stringify({ text: ta.value, to: bound }));
      else draftStore()?.removeItem(DRAFT_KEY);
    } catch { /* private window: the draft is just not kept */ }
  };

  /** show where ⌘⏎ sends; an empty draft follows the shown session */
  function sync(): void {
    const cur = deps.current();
    if (pinned && deps.live && !deps.live(bound)) pinned = false;
    if (!pinned && (!bound || !ta.value.trim())) bound = cur;
    target.textContent = markBidi(`To ${deps.title(bound)}${pinned && bound !== cur ? ' (reply)' : bound !== cur ? ' (not the shown session)' : ''}`);
    target.classList.toggle('warn', bound !== cur && !pinned);
    target.classList.toggle('pin', pinned);
    ta.placeholder = markBidi(kbd(composerHint(deps.title(bound), compact, !!deps.busy?.(bound)), 'Ctrl'));
    keepDraft();
    drawQueue();
  }

  /** F05: the held messages of the session written to, as cards above the field */
  const qEl = h('div', { class: 'cqueue', role: 'list', 'aria-label': 'Queued messages' });
  function keepQueue(): void {
    try {
      if (queue.length) draftStore()?.setItem(QUEUE_KEY, JSON.stringify(queue));
      else draftStore()?.removeItem(QUEUE_KEY);
    } catch { /* private window: the queue lives until the tab closes */ }
  }
  function drawQueue(): void {
    clear(qEl);
    for (const x of queue.filter((y) => y.to === (bound || deps.current()))) {
      qEl.append(h('div', { class: `cq-card${x.failed ? ' failed' : ''}`, role: 'listitem' },
        h('span', { class: 'cq-label' }, x.failed ? 'Not sent' : 'Queued'),
        h('span', { class: 'cq-text', title: x.failed ? markBidi(x.failed) : 'sent when the turn ends' }, markBidi(cutText(x.text.replace(/\s+/g, ' '), 200))),
        x.failed ? h('button', { class: 'ghost small', onclick: () => { delete x.failed; keepQueue(); drawQueue(); } }, 'Retry') : null,
        h('button', { class: 'ghost small', title: 'back into the field', onclick: () => {
          const t = takeQueued(queue, x.id);
          queue = t.q;
          keepQueue();
          ta.value = ta.value.trim() ? `${t.text}\n${ta.value}` : t.text;
          sync();
          ta.focus();
        } }, 'Edit'),
        h('button', { class: 'icon small', 'aria-label': 'Delete the queued message', title: 'delete', onclick: () => {
          queue = takeQueued(queue, x.id).q;
          keepQueue();
          drawQueue();
        } }, fi('x', 'gi'))));
    }
    qEl.classList.toggle('hidden', !qEl.childNodes.length);
  }

  /** deliver one held message whose session no longer works (a session that ended keeps it as Not sent) */
  async function deliver(): Promise<void> {
    if (!queue.length || sending) return;
    for (const x of queue) if (!x.failed && deps.live && !deps.live(x.to)) x.failed = 'the session ended';
    const x = nextReady(queue, (id) => !!deps.busy?.(id), (id) => Date.now() - (sentAt.get(id) ?? 0) < QUEUE_COOL);
    if (x) {
      sending = true;
      try {
        if (await deps.sendTo(x.to, x.text)) {
          queue = queue.filter((y) => y.id !== x.id);
          sentAt.set(x.to, Date.now());
        } else x.failed = 'not sent - Retry';
      } finally {
        sending = false;
      }
    }
    keepQueue();
    drawQueue();
  }
  window.setInterval(() => void deliver(), QUEUE_TICK);
  ta.addEventListener('input', sync);

  const templates = () => load<{ name: string; text: string }[]>(TPL_KEY, DEFAULT_TEMPLATES);
  const fillTemplates = () => {
    clear(tpl);
    tpl.append(h('option', { value: '' }, 'template…'));
    templates().forEach((t, i) => tpl.append(h('option', { value: String(i) }, t.name)));
  };
  tpl.addEventListener('change', () => {
    const t = templates()[Number(tpl.value)];
    if (t) ta.value = ta.value ? `${ta.value}\n${t.text}` : t.text;
    tpl.value = '';
    sync();
    ta.focus();
  });

  async function send(): Promise<void> {
    const text = ta.value.trim();
    if (!text || sending) return;   // a second ⌘⏎ while the first is on its way
    sending = true;
    sendBtn.disabled = true;
    const to = bound || deps.current();
    const busy = !!deps.busy?.(to);
    try {
      if (busy) {   // F05: held as a card until the turn ends (Edit / ✕ while it waits)
        queue = enqueue(queue, to, text);
        keepQueue();
      }
      if (busy || await deps.sendTo(to, text)) {
        hist = pushHistory(hist, text);
        save(HIST_KEY, hist);
        hpos = -1;
        draft = '';
        ta.value = '';
        if (compact) deps.toast(busy ? `Queued for ${deps.title(to)} - sent when the turn ends` : `Sent to ${deps.title(to)}`);
        if (pinned) close();   // a reply from the queue is done
        else sync();
      }
    } finally {
      sending = false;
      sendBtn.disabled = false;
    }
  }

  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !e.isComposing) {
      e.preventDefault();
      e.stopPropagation();   // the global Esc (palette, grid focus) never sees it
      close();
    } else if (isSend(e, compact ? 'chat' : 'doc')) {
      e.preventDefault();
      void send();
    } else if (e.altKey && (e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      e.preventDefault();
      if (hpos === -1) draft = ta.value;
      hpos = Math.max(-1, Math.min(hist.length - 1, hpos + (e.key === 'ArrowUp' ? 1 : -1)));
      ta.value = hpos >= 0 ? hist[hpos]! : draft;
      sync();
    }
  });

  const saveTpl = () => {
    const text = ta.value.trim();
    if (!text) return deps.toast('Write the template text first');
    const name = cutText(text.split('\n')[0]!, 40);
    save(TPL_KEY, [...templates(), { name, text }]);
    fillTemplates();
    deps.toast(`Template "${name}" saved`);
  };
  const toAll = () => {
    const text = ta.value.trim();
    const to = bound || deps.current();
    const others = deps.others(to);
    if (!text) return;
    if (!others.length) return deps.toast('There is no other live session');
    deps.confirm(`Send to ${others.length + 1} sessions?`, `${others.map((o) => o.title).join(', ')} + ${deps.title(to)}:\n\n${text.slice(0, 800)}`, () => {
      void (async () => {
        let ok = await deps.sendTo(to, text) ? 1 : 0;
        for (const o of others) if (await deps.sendTo(o.id, text)) ok++;
        deps.toast(`Sent to ${ok} ${ok === 1 ? 'session' : 'sessions'}`);
        hist = pushHistory(hist, text);
        save(HIST_KEY, hist);
        ta.value = '';
        sync();
      })();
    });
  };

  fillTemplates();
  const sendBtn = h('button', { class: 'primary', onclick: () => void send() }, 'Send') as HTMLButtonElement;
  const sizeBtn = h('button', { class: 'csize', onclick: () => { setCompact(!compact); ta.focus(); } }) as HTMLButtonElement;
  el.append(qEl, ta, h('div', { class: 'crow' }, target, tpl, h('span', { class: 'ckeys small' }, markBidi(kbd(COMPACT_KEYS))),
    h('button', { class: 'cfull', title: 'save the text as a template', onclick: saveTpl }, '+ template'),
    h('button', { class: 'cfull', title: 'send the same to all live sessions', onclick: toAll }, 'To all…'),
    sizeBtn, sendBtn));

  /** the reply is over: a draft it put aside is back (closed, with its own target) */
  function unpin(): void {
    pinned = false;
    if (stash && !ta.value.trim()) {
      ta.value = stash.text;
      bound = stash.to;
    } else if (stash) {   // an unsent reply stays the draft: the one put aside is in the history (⌥↑)
      hist = pushHistory(hist, stash.text);
      save(HIST_KEY, hist);
    }
    stash = null;
  }

  function setCompact(on: boolean): void {
    compact = on;
    el.classList.toggle('compact', on);
    ta.rows = on ? 2 : 4;
    sizeBtn.replaceChildren(h('i', { class: `fi fi-${on ? 'expand' : 'shrink'}`, 'aria-hidden': 'true' }), on ? markBidi(kbd('⌘E')) : '');
    sizeBtn.title = markBidi(kbd(on ? 'the full composer: templates, To all… (⌘E)' : 'collapse (⌘E)'));
    sendBtn.title = markBidi(kbd(sendHint(on ? 'chat' : 'doc'), 'Ctrl'));
    sync();
  }
  setCompact(false);

  function close(): void {
    unpin();
    el.classList.add('hidden');
    sync();
    deps.closed?.();
  }

  return {
    el,
    open: () => !el.classList.contains('hidden'),
    compact: () => compact,
    setCompact,
    /** show (`on`) or hide; `opts.compact` picks the form, `opts.focus` false leaves the keyboard where it is */
    toggle(on?: boolean, opts: { compact?: boolean; focus?: boolean } = {}): void {
      const show = on ?? el.classList.contains('hidden');
      el.classList.toggle('hidden', !show);
      if (!show) unpin();
      if (show) {
        if (opts.compact !== undefined) setCompact(opts.compact);
        sync();
        if (opts.focus !== false) ta.focus();
      }
    },
    /** D7: Reply r in the queue - collapsed, pinned to `id`, the keyboard in it */
    replyTo(id: string): void {
      if (bound !== id && ta.value.trim()) {   // a draft for another session is not this reply: it comes back afterwards
        stash = { to: bound, text: ta.value };
        ta.value = '';
        draft = '';
      }
      bound = id;
      pinned = true;
      el.classList.remove('hidden');
      setCompact(true);
      ta.focus();
    },
    /** the shown session changed (select): an empty draft follows it, a written one keeps its target */
    sync,
    /** prepend text (review notes etc.) and open */
    put(text: string): void {
      unpin();
      if (!ta.value.trim()) bound = deps.current();
      ta.value = ta.value ? `${text}\n${ta.value}` : text;
      el.classList.remove('hidden');
      sync();
      ta.focus();
    },
  };
}
