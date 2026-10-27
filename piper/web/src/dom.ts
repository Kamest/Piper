// Tiny DOM helpers. No innerHTML anywhere: every text goes through textContent,
// so session titles, tasks and GitLab text can never become markup. Every string child and
// title / aria-label / placeholder / alt goes through markBidi: a direction control (U+202E…)
// from a repo, an agent, GitLab or a database shows as [U+202E] instead of reordering the line.
// Raw on purpose: a <textarea>'s content (the user edits it), other attributes (value, href,
// data-*), and Node children - rawText() for text that must stay character-aligned with an input.

import { markBidi } from './text.js';

type Attrs = Record<string, string | boolean | number | ((ev: Event) => void) | undefined>;
type Child = Node | string | null | undefined | false;

/** attributes a person reads */
const SHOWN_ATTRS = new Set(['title', 'aria-label', 'placeholder', 'alt']);

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === false) continue;
    if (typeof v === 'function') el.addEventListener(k.replace(/^on/, ''), v);
    else if (k === 'class') el.className = String(v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, SHOWN_ATTRS.has(k) ? markBidi(String(v)) : String(v));
  }
  const raw = tag === 'textarea';   // its text is the value the user edits and saves
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === 'string' ? document.createTextNode(raw ? c : markBidi(c)) : c);
  }
  return el;
}

/** text exactly as it is, bidi controls included: only for a layer that must line up character by
 *  character with an input the user edits (the SQL console's highlight under its textarea) */
export function rawText(s: string): Text {
  return document.createTextNode(s);
}

/** a mask icon from app.css (.fi-*) in place of a text glyph (pages polish): decorative, the control carries the name */
export function fi(name: string, cls = ''): HTMLElement {
  return h('i', { class: `fi fi-${name}${cls ? ' ' + cls : ''}`, 'aria-hidden': 'true' });
}

export function clear(el: Element): void {
  while (el.firstChild) el.firstChild.remove();
}

/** the user's own fold choice per context-panel section key; it outlives a re-render and a session switch */
const userFold = new Map<string, boolean>();

export interface Fold {
  el: HTMLElement;
  /** the state words next to the title ("5 of 11", "1 error", "clean") */
  status: HTMLElement;
  /** buttons on the right of the header; they never toggle the section */
  actions: HTMLElement;
  body: HTMLElement;
  /** open when the state wants it, unless the user folded or unfolded it */
  auto(want: boolean): void;
}

/**
 * A context-panel section: one header line (chevron · title · status · actions) and a body.
 * Not a <details>: the actions stay clickable, and the key - not the header text with its
 * live counts - remembers the fold.
 */
export function fold(key: string, title: string | Node): Fold {
  const status = h('span', { class: 'sec-s' });
  const actions = h('span', { class: 'sec-a' });
  const body = h('div', { class: 'sec-b' });
  const toggle = h('button', { class: 'sec-tg', 'aria-expanded': 'false' }, h('span', { class: 'sec-t' }, title), status);
  const el = h('section', { class: 'sec', 'data-k': key }, h('div', { class: 'sec-h' }, toggle, actions), body);
  const set = (open: boolean) => {
    el.classList.toggle('open', open);
    toggle.setAttribute('aria-expanded', String(open));
  };
  toggle.addEventListener('click', () => {
    const open = !el.classList.contains('open');
    userFold.set(key, open);
    set(open);
  });
  return { el, status, actions, body, auto: (want) => set(userFold.get(key) ?? want) };
}

/** open a section as if the user had (the "isolation" link in the header) */
export function unfold(key: string): void {
  userFold.set(key, true);
  document.querySelector(`.sec[data-k="${key}"]`)?.classList.add('open');
  document.querySelector(`.sec[data-k="${key}"] .sec-tg`)?.setAttribute('aria-expanded', 'true');
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

/** "5 min ago" / "just now" - ago() reads "now", never "now ago". */
export function agoText(ts: number): string {
  const a = ago(ts);
  return a === 'now' ? 'just now' : `${a} ago`;
}

/** Shared mouse/keyboard splitter; the handle is outside the panel's redrawn content. */
export function sideSplitter(root: HTMLElement, panel: HTMLElement, variable: string, key: string, min: number, max: number, initial: number | (() => number), edge: 'left' | 'right' = 'right', room: number | (() => number) = 280, watch: HTMLElement[] = [], style: CSSStyleDeclaration = root.style): HTMLElement {
  const def = () => typeof initial === 'function' ? initial() : initial;
  let preferred: number | null = null;
  try { const raw = localStorage.getItem(key); if (raw !== null && Number.isFinite(Number(raw))) preferred = Number(raw); } catch { /* private browser */ }
  const limit = () => Math.max(min, Math.min(max, root.clientWidth - (typeof room === 'function' ? room() : room)));
  const handle = h('div', { class: 'side-splitter', role: 'separator', tabindex: '0', 'aria-label': `Resize ${panel.id || key.split('.').at(-1)} panel`, 'aria-orientation': 'vertical', 'aria-valuemin': min });
  const tip = h('span', { class: 'splitter-tip' }); handle.append(tip); root.append(handle);
  const apply = (value: number, save = true) => {
    // A restored panel is often not mounted yet: zero layout width must not
    // overwrite its saved preference with the minimum before ResizeObserver runs.
    preferred = Math.min(root.clientWidth ? limit() : max, Math.max(min, Math.round(value)));
    style.setProperty(variable, `${preferred}px`);
    if (save) try { localStorage.setItem(key, String(preferred)); } catch { /* private browser */ }
    position();
  };
  function position(): void {
    const a = root.getBoundingClientRect(), b = panel.getBoundingClientRect();
    handle.hidden = !b.width || !b.height || getComputedStyle(panel).visibility === 'hidden';
    handle.style.left = `${(edge === 'right' ? b.right : b.left) - a.left + root.scrollLeft - 5}px`;
    handle.style.top = `${b.top - a.top + root.scrollTop}px`;
    handle.style.height = `${b.height}px`;
    const value = Math.round(b.width);
    handle.setAttribute('aria-valuenow', String(value)); handle.setAttribute('aria-valuemax', String(limit()));
    tip.textContent = `${value}px`;
  }
  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault(); handle.setPointerCapture(event.pointerId);
    const start = event.clientX, width = panel.getBoundingClientRect().width;
    handle.classList.add('dragging');
    const move = (ev: PointerEvent) => apply(width + (ev.clientX - start) * (edge === 'right' ? 1 : -1));
    const end = () => { handle.classList.remove('dragging'); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end); };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', end); handle.addEventListener('pointercancel', end);
  });
  handle.addEventListener('dblclick', () => apply(def()));
  handle.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) return;
    event.preventDefault();
    apply(event.key === 'Home' ? def() : panel.getBoundingClientRect().width + (event.key === 'ArrowRight' ? 1 : -1) * (edge === 'right' ? 1 : -1) * (event.shiftKey ? 40 : 10));
  });
  const observer = new ResizeObserver(() => {
    if (preferred !== null && root.clientWidth > 800) style.setProperty(variable, `${Math.max(min, Math.min(limit(), preferred))}px`);
    position();
  });
  observer.observe(root); observer.observe(panel);
  for (const other of watch) observer.observe(other);
  if (preferred !== null) apply(preferred, false);
  return handle;
}
