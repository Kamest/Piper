// Host facts on an approval card. Text nodes only, including diffs and row previews.
import { h } from './dom.js';

export interface Evidence {
  title: string;
  state: 'ready' | 'checking' | 'unavailable' | 'error';
  checked_at?: number;
  rows: { text: string; code?: string; chips?: string[]; mono?: boolean; tone?: string }[];
  notes?: string[];
  diff?: string;
  diffstat?: { files: number; added: number; removed: number };
  inline_diff?: boolean;
  preview?: { columns: string[]; rows: { v: unknown; to?: unknown }[][]; more?: number };
}

const value = (v: unknown): string => v === null ? 'NULL' : String(v ?? '');
const chips = (row: Evidence['rows'][number]) => (row.chips ?? []).map(c => h('span', {
  class: 'pe-chip ' + (['PRODUCTION', 'PROD', 'PROTECTED'].includes(c) ? 'bad' : c === 'NOT PROTECTED' ? 'ok' : 'warn'),
}, c));
const chipFirst = (row: Evidence['rows'][number]) => (row.chips ?? []).some(c => ['TEST', 'DEV', 'PROD', 'PRODUCTION', 'UNKNOWN HOST'].includes(c));

/** Missing facts still occupy the box, including cards from an older daemon. */
export function evidenceBox(input?: Evidence): HTMLElement {
  const e = input ?? { title: 'What it changes', state: 'unavailable', rows: [{ text: 'Could not check: evidence unavailable' }] };
  const checked = e.state === 'checking' ? 'checking…' : e.checked_at
    ? 'checked ' + new Date(e.checked_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
  let firstCheck: HTMLElement | null = null;
  const box = h('section', { class: `pevidence ${e.state}`, 'aria-label': e.title, 'aria-live': 'polite' },
    h('div', { class: 'pe-head' }, h('span', {}, e.title), h('time', { title: e.checked_at ? new Date(e.checked_at * 1000).toLocaleString() : '' }, checked)),
    ...e.rows.map(r => {
      const row = h('div', { class: `pe-row ${r.code ? 'pe-commit' : ''} ${r.mono ? 'mono' : ''} ${r.tone ?? ''}` },
      ...(chipFirst(r) ? chips(r) : []),
      r.code ? h('code', { class: 'pe-code' }, r.code) : null,
      h('span', { class: r.code ? 'pe-subject' : undefined, title: r.code ? r.text : undefined }, r.text), ...(chipFirst(r) ? [] : chips(r)));
      if (r.tone && !firstCheck) firstCheck = row;
      return row;
    }));
  if (!e.rows.length) box.append(h('div', { class: 'pe-row mut' }, e.state === 'checking' ? 'checking…' : 'Could not check: facts unavailable'));
  if (e.preview) {
    const p = e.preview;
    box.append(h('div', { class: 'pe-table' }, h('table', {},
      h('thead', {}, h('tr', {}, ...p.columns.map(c => h('th', {}, c)))),
      h('tbody', {}, ...p.rows.slice(0, 3).map(row => h('tr', {}, ...row.map(cell => h('td', {},
        h('span', { class: 'to' in cell ? 'del' : undefined }, value(cell.v)),
        'to' in cell ? h('span', { class: 'mut' }, ' → ') : null,
        'to' in cell ? h('span', { class: 'add' }, value(cell.to)) : null))))),
      p.more ? h('tfoot', {}, h('tr', {}, h('td', { colspan: p.columns.length }, `+ ${p.more} more rows`))) : null)));
  }
  if (e.diff) {
    const diff = h('pre', { class: 'pe-diff' }, ...e.diff.split('\n').map(line =>
      h('span', { class: line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : '' }, line + '\n')));
    const stat = e.diffstat;
    const block = e.inline_diff ? diff : h('details', { class: 'pe-diffstat' }, h('summary', {},
      stat ? h('span', { class: 'mut' }, `${stat.files} files `) : null,
      stat ? h('span', { class: 'ok mono' }, `+${stat.added} `) : null,
      stat ? h('span', { class: 'bad mono' }, `−${stat.removed} · `) : null,
      h('span', { class: 'pe-link' }, 'View diff')), diff);
    // Push: commits, diffstat, then checks. Other diffs stay below the facts.
    box.insertBefore(block, e.diffstat && !e.inline_diff ? firstCheck : null);
  }
  box.append(...(e.notes ?? []).map(n => h('div', { class: 'pe-note' }, n)));
  return box;
}
