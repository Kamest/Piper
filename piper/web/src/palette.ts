// Command palette: one fuzzy list of everything the GUI
// can do - start a session in a project, switch, stop, resume, the advisor, the stacks
// dashboard. ↑/↓ + Enter, Esc closes (handled by the modal). D12: a session is one row, its actions behind →
// (← or Backspace in an empty field back); the ended sessions are a group at the end, folded to one row (9.136).

import { kbd } from './clipkeys.js';
import { clear, h } from './dom.js';
import { markBidi } from './text.js';
import type { Module } from './types.js';

export interface Command {
  label: string;
  hint?: string;
  run(): void;
  /** a feature module's command: left out while the module is off */
  module?: Module;
  /** D12: the row's own actions, listed by → (or a click on its ›) */
  sub?: Command[];
  /** a heading drawn above the first row of the group while the query is empty */
  group?: string;
  /** F18: the shortcut, a chip on the right (else taken from a hint that starts with one: "⌘, · features") */
  keys?: string;
  /** F18: more text to match that is not shown (a session's project and prompts) */
  search?: string;
  /** F18: a mark before the name (a session's state dot) */
  icon?: Node;
  /** F18: ⇧↵ - open it beside the active session (a 2-pane grid); else ⇧↵ = ↵ */
  beside?(): void;
  /** 9.136: its group stays one "N ended" row while the query is empty, until that row is opened (typing finds them) */
  folded?: boolean;
  /** 9.136: the "N ended" row itself - ↵ / a click unfolds the group instead of running something */
  unfold?: string;
}

/** 9.136: dead things wait folded - a folded group's rows become one row that opens it (only while nothing is typed) */
export function foldGroups(items: Command[], typed: boolean, open: ReadonlySet<string>): Command[] {
  if (typed) return items;
  const out: Command[] = [];
  for (const c of items) {
    if (!c.folded || !c.group || open.has(c.group)) { out.push(c); continue; }
    if (out.some((x) => x.unfold === c.group)) continue;
    const n = items.filter((x) => x.folded && x.group === c.group).length;
    out.push({ label: `${n} ${c.group.toLowerCase()}`, hint: 'show them (or type to search)', group: c.group, unfold: c.group, run: () => undefined });
  }
  return out;
}

/** F18: a row's shortcut and its short description - a hint "⌘B · fold" is both, "⌘K" only a key, "18 new" only text */
export function splitHint(c: Pick<Command, 'hint' | 'keys'>): { keys: string; desc: string } {
  if (c.keys) return { keys: c.keys, desc: c.hint ?? '' };
  const m = /^((?:[⌘⌥⇧⌃]+|Ctrl\+)\S*)(?: · (.*))?$/.exec(c.hint ?? '');
  return m ? { keys: m[1]!, desc: m[2] ?? '' } : { keys: '', desc: c.hint ?? '' };
}

/** subsequence match with a small bonus for word starts; null = no match */
export function score(query: string, text: string): number | null {
  const q = query.toLowerCase().trim();
  if (!q) return 0;
  const t = text.toLowerCase();
  let at = 0;
  let s = 0;
  for (const ch of q) {
    const i = t.indexOf(ch, at);
    if (i < 0) return null;
    s += i === at ? 3 : 1;
    if (i === 0 || /[\s/·:-]/.test(t[i - 1] ?? '')) s += 2;
    at = i + 1;
  }
  return s - t.length / 100;
}

/** what the palette lists: every command in its own order for an empty query (a first-40 cut hid the rest),
 *  else the matches best first */
export function pick(commands: Command[], query: string): Command[] {
  if (!query.trim()) return commands.slice();
  return commands
    .map((c) => ({ c, s: score(query, [c.label, kbd(c.hint ?? ''), c.search ?? ''].join(' ')) }))
    .filter((x): x is { c: Command; s: number } => x.s !== null)
    .sort((a, b) => b.s - a.s)
    .slice(0, 100)
    .map((x) => x.c);
}

export function openPalette(mount: (el: HTMLElement, focus: HTMLElement) => void, close: () => void, commands: Command[]): void {
  const input = h('input', { type: 'search', placeholder: 'Command, project or session', autocomplete: 'off' });
  const crumb = h('div', { class: 'pcrumb mut' });
  const list = h('div', { class: 'plist' });
  let items: Command[] = [];
  let sel = 0;
  let parent: Command | null = null;   // the row whose actions are listed
  const unfolded = new Set<string>();   // 9.136: folded groups opened in this palette
  const runIt = (c: Command, beside = false) => {
    if (c.unfold) { unfolded.add(c.unfold); draw(); return; }
    close(); if (beside && c.beside) c.beside(); else c.run();
  };
  const enter = (c: Command) => { parent = c; input.value = ''; sel = 0; draw(); };
  const back = () => { parent = null; input.value = ''; sel = 0; draw(); };
  const draw = () => {
    items = foldGroups(pick(parent?.sub ?? commands, input.value), !!input.value.trim(), unfolded);
    sel = Math.min(sel, Math.max(0, items.length - 1));
    clear(list);
    crumb.hidden = !parent;
    clear(crumb);
    if (parent) crumb.append(h('button', { class: 'mini', title: 'back (←)', onclick: back }, '←'), markBidi(` ${parent.label}`));
    let group: string | undefined;
    const typed = !!input.value.trim();
    items.forEach((c, i) => {
      if (!typed && c.group && c.group !== group) list.append(h('div', { class: 'psec' }, c.group));
      group = c.group;
      // F18: name, a short grey description, the group while typing (one list by match), the key as a chip on the right
      const { keys, desc } = splitHint(c);
      list.append(h('button', { class: 'pitem' + (i === sel ? ' active' : '') + (c.unfold ? ' punfold' : ''), 'aria-expanded': c.unfold ? 'false' : undefined, onclick: (e: Event) => runIt(c, (e as MouseEvent).shiftKey) },
        h('span', { class: 'name' }, c.icon ? c.icon.cloneNode(true) : null, h('span', { class: 'plabel' }, markBidi(c.label)), desc ? h('span', { class: 'pdesc', title: kbd(desc) }, markBidi(kbd(desc))) : null),
        typed && c.group ? h('span', { class: 'pgrp' }, c.group) : null,
        keys ? h('kbd', { class: 'pkey' }, kbd(keys)) : null,
        c.sub?.length ? h('span', { class: 'psub', role: 'button', title: 'its actions (→)', 'aria-label': `actions of ${c.label}`,
          onclick: (e: Event) => { e.stopPropagation(); enter(c); } }, h('i', { class: 'fi fi-chev-r', 'aria-hidden': 'true' })) : null));
    });
    if (!items.length) list.append(h('p', { class: 'mut' }, 'nothing found'));
  };
  input.addEventListener('input', () => { sel = 0; draw(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, items.length - 1); draw(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); draw(); e.preventDefault(); }
    else if (e.key === 'ArrowRight' && items[sel]?.sub?.length && input.selectionStart === input.value.length) { enter(items[sel]!); e.preventDefault(); }
    else if (parent && (e.key === 'ArrowLeft' || e.key === 'Backspace') && !input.value) { back(); e.preventDefault(); }
    else if (e.key === 'Enter') {
      const c = items[sel];
      if (c) runIt(c, e.shiftKey);
      e.preventDefault();
    }
  });
  draw();
  const foot = h('div', { class: 'pal-foot' }, h('span', {}, h('kbd', {}, '↵'), ' open'), h('span', {}, h('kbd', {}, '⇧↵'), ' beside'),
    h('span', {}, h('kbd', {}, 'esc'), ' close'));
  mount(h('div', { class: 'palette' }, crumb, input, list, foot), input);
}
