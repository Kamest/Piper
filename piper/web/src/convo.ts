// Conversation view: the session's Claude Code or Codex transcript as
// readable text (piperd reads it on the host, secrets scrubbed), filterable, with the token
// usage. Search across sessions goes through /api/conversations/search.

import { hosts, refHost, rawProjectKey, machineStorage } from './environment.js';
import { openMenu } from './menu.js';
import { refreshHost, api, ApiError } from './api.js';
import { clear, h } from './dom.js';
import { fileRefs } from './codenav.js';
import { showBidi, markBidi, matchRanges } from './text.js';
import { sessionDate } from './queue.js';
import { auditTime } from './board.js';
import { parseMarkdown, renderMarkdown } from './markdown.js';
import { references, type Reference } from './references.js';
import type { Conversation, ConvHit, HistoryItem, HistoryPreview, ToolCall } from './types.js';

export interface ConvoDeps {
  onReference?(session: string, ref: Reference): void;
  toast(text: string): void;
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  resumeConversations?(): void;
  /** open a session's conversation scrolled to one turn */
  jump(session: string, turn: number): void;
  /** a `path:line` in the transcript was clicked (path as printed: project-relative or the host path) - Piper's editor (K4) */
  openFile?(session: string, path: string, line: number): void;
}

const REFRESH_MS = 5000;

export interface ResumeDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  newSession(project: string, host?: string): void;
  /** Host captured by the launcher / requesting session; history pages never follow active selection. */
  host?: string;
  opened(session: string): void;
  /** W15: the picker goes into the Start work dialog (no header of its own; New conversation… only switches the kind) */
  mount?(root: HTMLElement): void;
  /** 9.140 (F29): the agent a listed Piper session's work can continue with ('Claude' / 'Codex'), null = none */
  switchTo?(session: string): string | null;
  /** 9.140: ask, then continue that session's work with the other agent (the picker is closed first) */
  switchAgent?(session: string): void;
}

function historyMarks(text: string, query: string): (Node | string)[] {
  const terms = query.match(/[\p{L}\p{N}]+/gu) ?? [];
  if (!terms.length) return [markBidi(text)];
  const rx = new RegExp(terms.join('|'), 'giu');
  const out: (Node | string)[] = [];
  let at = 0;
  for (const m of text.matchAll(rx)) {
    out.push(markBidi(text.slice(at, m.index)), h('mark', {}, m[0]));
    at = m.index! + m[0].length;
  }
  out.push(markBidi(text.slice(at)));
  return out;
}

/** R2: the hidden paths of a refused import (409 hidden_paths), null for any other error */
export function hiddenPaths(e: unknown): string[] | null {
  if (!(e instanceof ApiError) || e.status !== 409 || !e.json || typeof e.json !== 'object') return null;
  const paths = (e.json as { hidden_paths?: unknown }).hidden_paths;
  return Array.isArray(paths) ? paths.filter((x): x is string => typeof x === 'string') : null;
}

/** R2: the confirmation inside the picker (a second modal would close it) - names only, never content */
export function hiddenImportNotice(paths: string[], onImport: () => void, onCancel: () => void): HTMLElement {
  return h('div', { class: 'resume-hidden', role: 'alertdialog', 'aria-label': 'Transcript holds hidden file content' },
    h('p', {}, h('b', {}, 'The transcript holds content of files hidden from Claude. '),
      'Claude on the host read them; imported, the agent in the sandbox sees what the transcript keeps. Import anyway?'),
    paths.length ? h('ul', { class: 'st-warn' }, ...paths.map((p) => h('li', {}, h('code', {}, p))))
      : h('p', { class: 'small' }, 'The isolation rule cannot be read, so the hidden files cannot be named.'),
    h('div', { class: 'row' }, h('button', { onclick: onCancel }, 'Cancel'),
      h('button', { class: 'danger', onclick: onImport }, 'Import anyway')));
}

export function resumePicker(deps: ResumeDeps, initial = ''): void {
  const root = h('div', { class: 'resume-picker' });
  let selectedHost = deps.host ?? refHost(initial);
  const hostLabel = () => selectedHost ? hosts.get(selectedHost)?.host.label ?? 'Server' : 'This computer';
  const ssh = () => hosts.get(selectedHost)?.host.mode === 'ssh';
  const offline = () => !!selectedHost && hosts.get(selectedHost)?.online !== true;
  const hostButton = h('button', { class: 'resume-host', type: 'button', 'aria-label': 'History host', 'aria-haspopup': 'menu', onclick: () => openMenu(hostButton, [
    { label: 'This computer', on: !selectedHost, run: () => void chooseHost('') }, null,
    ...[...hosts.values()].map(row => ({ label: row.host.label, on: row.host.id === selectedHost, hint: !row.online ? 'Unavailable' : row.host.mode === 'ssh' ? 'SSH only · no history' : 'Piper service', run: () => void chooseHost(row.host.id) })),
  ], { label: 'History host' }) });
  const syncHost = () => {
    hostButton.replaceChildren(h('span', { class: 'server-dot' + (!offline() ? ' online' : '') }), hostLabel(), h('i', { class: 'fi fi-chev-d', 'aria-hidden': 'true' }));
    hostButton.disabled = busy;
    for (const control of [search, projectButton, projectClear, agent, period, external, sourcePiper, sourceAll]) control.disabled = busy || ssh();
    newConversation.textContent = markBidi(ssh() ? `New direct session on ${hostLabel()}…` : 'New conversation…');
    newConversation.classList.toggle('resume-new-direct', ssh());
    newConversation.disabled = busy;
    fork.hidden = resume.hidden = handOver.hidden = ssh();
  };

  const search = h('input', { type: 'search', placeholder: 'Search topics, issue numbers or conversation text…',
    'aria-label': 'Search saved conversations', autocomplete: 'off', maxlength: '300' }) as HTMLInputElement;
  const projectButton = h('button', { class: 'resume-project', 'aria-expanded': 'false', onclick: () => {
    const open = projectMenu.classList.toggle('hidden') === false;
    projectButton.setAttribute('aria-expanded', String(open));
    if (open) { projectSearch.value = ''; drawProjects(); projectSearch.focus(); }
  } }, h('span', { class: 'rp-label' }, 'All projects'), h('i', { class: 'fi fi-chev-d', 'aria-hidden': 'true' }));
  const projectClear = h('button', { class: 'icon hidden', 'aria-label': 'Clear project filter', title: 'All projects', onclick: () => chooseProject('') }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }));
  const projectSearch = h('input', { type: 'search', placeholder: 'Find a project…', 'aria-label': 'Find a history project' }) as HTMLInputElement;
  const projectOptions = h('div', { class: 'resume-project-options' });
  const projectMenu = h('div', { class: 'resume-project-menu hidden' }, projectSearch, projectOptions);
  const agent = h('select', { 'aria-label': 'Filter conversations by agent' },
    h('option', { value: '' }, 'All agents'), h('option', { value: 'claude' }, 'Claude'), h('option', { value: 'codex' }, 'Codex')) as HTMLSelectElement;
  const period = h('select', { 'aria-label': 'Filter conversations by date' },
    h('option', { value: '' }, 'All time'), h('option', { value: 'today' }, 'Today'),
    h('option', { value: 'yesterday' }, 'Yesterday'), h('option', { value: 'week' }, 'Last 7 days')) as HTMLSelectElement;
  const status = h('div', { class: 'resume-status mut small', role: 'status', 'aria-live': 'polite' });
  const external = h('input', { type: 'checkbox', 'aria-label': 'Include conversations outside Piper' }) as HTMLInputElement;
  try { external.checked = machineStorage(localStorage).getItem('piper.resume.source') !== 'piper'; } catch { external.checked = true; }
  const sourcePiper = h('button', { class: 'db-seg', onclick: () => chooseSource(false) }, 'Piper sessions');
  const sourceAll = h('button', { class: 'db-seg', title: 'Including Claude and Codex conversations outside Piper', onclick: () => chooseSource(true) }, 'Everywhere');
  const source = h('div', { class: 'resume-source row left small' }, 'Source', h('span', { class: 'db-segs' }, sourcePiper, sourceAll), h('span', { class: 'mut' }, 'incl. Claude / Codex outside Piper'));
  function drawSource(): void {
    sourcePiper.classList.toggle('on', !external.checked); sourceAll.classList.toggle('on', external.checked);
    sourcePiper.setAttribute('aria-pressed', String(!external.checked)); sourceAll.setAttribute('aria-pressed', String(external.checked));
  }
  function chooseSource(all: boolean): void {
    external.checked = all;
    try { machineStorage(localStorage).setItem('piper.resume.source', all ? 'all' : 'piper'); } catch { /* private browser */ }
    drawSource(); changed();
  }
  drawSource();
  const target = h('select', { 'aria-label': 'Project for imported conversation' }) as HTMLSelectElement;
  const confirmCheckout = h('input', { type: 'checkbox', 'aria-label': 'Confirm another checkout' }) as HTMLInputElement;
  const list = h('div', { class: 'resume-results', role: 'listbox', 'aria-label': 'Saved conversations', tabindex: '0' });
  const detail = h('div', { class: 'resume-detail', tabindex: '0', 'aria-label': 'Conversation preview' });
  const error = h('div', { class: 'bad small', role: 'alert' });
  const count = h('span', { class: 'mut small' });
  const resume = h('button', { class: 'primary', disabled: true, 'aria-label': 'Resume selected conversation', onclick: () => void launch(false) }, 'Resume ', h('kbd', {}, '⏎')) as HTMLButtonElement;
  const fork = h('button', { disabled: true, 'aria-label': 'Fork selected conversation', onclick: () => void launch(true),
    title: 'Start a separate conversation with this history. Uses the same project checkout.' }, 'Fork conversation') as HTMLButtonElement;
  const handOver = h('button', { class: 'hidden', onclick: () => { const id = selected; deps.closeModal(); deps.switchAgent?.(id); },
    title: 'Stops the session if it runs and hands its conversation to the other agent as a file.' }, 'Continue with…') as HTMLButtonElement;
  const more = h('button', { class: 'link small hidden', onclick: () => { page += 50; void load(true); } }, 'Load more');
  const close = h('button', { class: 'icon', 'aria-label': 'Close conversation picker', title: 'Close · Esc', onclick: () => deps.closeModal() }, h('i', { class: 'fi fi-x', 'aria-hidden': 'true' }));
  const newConversation = h('button', { onclick: () => { if (!deps.mount) deps.closeModal(); deps.newSession(project || rows.find((r) => r.session === selected)?.project || '', selectedHost); }, title: 'Start with an empty conversation context' }, 'New conversation…');
  const resumeBody = h('div', { class: 'resume-body' }, h('div', { class: 'resume-list' }, list, more), detail);
  const sshNote = h('div', { class: 'resume-no-history hidden', role: 'status' });
  root.append(...(deps.mount ? [] : [h('div', { class: 'resume-head' }, h('h2', {}, 'Resume conversation'), close)]),
    h('div', { class: 'resume-filters' + (hosts.size ? ' with-host' : '') }, ...(hosts.size ? [hostButton] : []), search, h('div', { class: 'resume-project-wrap' }, projectButton, projectClear, projectMenu), agent, period),
    source,
    status, resumeBody, sshNote, error,
    h('div', { class: 'resume-footer' }, count, h('span', { class: 'grow' }), newConversation, handOver, fork, resume));
  let project = '';
  let projects: { key: string; count: number }[] = [];
  let targets: { key: string; path: string }[] = [];
  let rows: HistoryItem[] = [];
  let selected = '';
  let page = 0;
  let generation = 0;
  let previewGeneration = 0;
  let timer = 0;
  let busy = false;
  let preview: HistoryPreview | null = null;
  let previewFor = '';
  let previewVersion = '';
  let loading = false;

  function drawProjects(): void {
    clear(projectOptions);
    projectOptions.append(h('button', { onclick: () => chooseProject('') }, 'All projects'));
    for (const p of projects.filter((p) => rawProjectKey(p.key).toLowerCase().includes(projectSearch.value.toLowerCase()))) {
      projectOptions.append(h('button', { onclick: () => chooseProject(p.key) }, rawProjectKey(p.key), h('span', { class: 'mut' }, String(p.count))));
    }
  }
  function chooseProject(value: string): void {
    project = value;
    projectButton.querySelector('.rp-label')!.textContent = markBidi(value ? rawProjectKey(value) : 'All projects');
    projectClear.classList.toggle('hidden', !value);
    projectMenu.classList.add('hidden');
    projectButton.setAttribute('aria-expanded', 'false');
    search.focus();
    changed();
  }
  function changed(): void {
    page = 0;
    window.clearTimeout(timer);
    ++generation;
    loading = true;
    actions();
    status.textContent = external.checked ? 'Searching Piper, ~/.claude and ~/.codex…' : 'Searching Piper conversations…';
    timer = window.setTimeout(() => void load(), 180);
  }
  function actions(): void {
    const row = rows.find((r) => r.session === selected);
    const valid = previewFor === selected && preview?.available;
    const live = !!row && ['preflight', 'preparing', 'spawning', 'trust', 'starting', 'attesting', 'ready', 'working', 'waiting', 'attention', 'stopping'].includes(row.state);
    const local = row?.source === 'local';
    const destination = targets.find(t => t.key === target.value);
    const importReady = !!destination && (destination.path === row?.cwd || confirmCheckout.checked);
    resume.disabled = busy || loading || offline() || ssh() || !row || (!live && !valid) || (local && !importReady);
    fork.disabled = busy || loading || offline() || ssh() || !row || !valid || local;
    const to = row && row.source === 'piper' && !row.closed ? deps.switchTo?.(row.session) ?? null : null;
    handOver.classList.toggle('hidden', !to);
    handOver.textContent = markBidi(`Continue with ${to ?? '…'}`);
    handOver.disabled = busy || loading || offline() || ssh() || !valid;
    syncHost();
    resume.replaceChildren(busy ? 'Opening…' : local ? 'Import and continue ' : live ? 'Open running session ' : 'Resume ', h('kbd', {}, '⏎'));
  }
  function drawDetail(row: HistoryItem): void {
    clear(detail);
    detail.append(h('h3', {}, row.title), h('p', { class: 'mut small' }, metadata(row)),
      h('p', { class: 'mut small' }, 'Last active: ' + sessionDate(row.activity, Date.now() / 1000)));
    if (row.import_from) detail.append(h('p', { class: 'mut small' }, 'Imported conversation · independent history'));
    else if (row.fork_from) detail.append(h('p', { class: 'mut small' }, 'Forked conversation · independent history'));
    if (row.source === 'local') {
      detail.append(h('p', { class: 'mut small' }, 'Outside Piper · ' + row.cwd), h('label', { class: 'small' }, 'Continue in project ', target));
      const destination = targets.find(t => t.key === target.value);
      if (destination && destination.path !== row.cwd) detail.append(h('p', { class: 'small' }, 'Target checkout: ' + destination.path),
        h('label', { class: 'resume-external small' }, confirmCheckout, 'Continue this history in the selected checkout'));
    }
    const text = (label: string, value: string) => {
      if (value) detail.append(h('h4', {}, label), h('pre', {}, ...historyMarks(value, search.value)));
    };
    text('Search match', row.snippet);
    text('Last answer', preview?.answer || row.answer);
    text('Last request', preview?.last || row.last);
    if (previewFor === row.session && preview) {
      if (!preview.available) detail.append(h('p', { class: 'bad small' }, preview.note || 'The saved conversation is unavailable. Start a new conversation explicitly.'));
      const transcript = h('details', {}, h('summary', {}, 'Recent conversation'));
      for (const m of preview.messages) transcript.append(h('div', { class: 'resume-message' },
        h('div', { class: 'mut small' }, m.role === 'user' ? 'You' : row.agent === 'codex' ? 'Codex' : 'Claude'), h('pre', {}, ...historyMarks(m.text, search.value))));
      detail.append(transcript);
      text('First request', preview.first);
    } else detail.append(h('p', { class: 'mut small' }, 'Checking saved conversation…'));
    detail.append(h('p', { class: 'mut small' }, row.source === 'local' ?
      'Import copies this history into a separate Piper conversation. The original conversation stays available in your CLI.' :
      `Resume continues this context${selectedHost ? ' on ' + hostLabel() : ''}. Fork copies it to a separate conversation. Both use the same project checkout${selectedHost ? ' there' : ''}.`));
  }
  function metadata(row: HistoryItem): string {
    return [selectedHost ? hostLabel() : '', row.project ? rawProjectKey(row.project) : 'Unregistered project', row.branch, row.agent === 'codex' ? 'Codex' : 'Claude',
      row.created ? 'Created ' + sessionDate(row.created, Date.now() / 1000) : 'Creation date unknown'].filter(Boolean).join(' · ');
  }
  async function select(id: string): Promise<void> {
    if (id !== selected) clear(error);   // R2: a hidden-files question belongs to the conversation it was asked for
    selected = id;
    preview = null;
    previewFor = '';
    const n = ++previewGeneration;
    const row = rows.find((r) => r.session === id);
    previewVersion = row ? JSON.stringify([row.messages, row.last, row.answer, row.title]) : '';
    for (const b of list.querySelectorAll<HTMLElement>('[data-session]')) b.setAttribute('aria-selected', String(b.dataset.session === id));
    list.querySelector(`[data-session="${id}"]`)?.scrollIntoView({ block: 'nearest' });
    actions();
    if (!row) { clear(detail); return; }
    target.replaceChildren(h('option', { value: '' }, 'Choose a project…'), ...targets.map(t => h('option', { value: t.key }, rawProjectKey(t.key))));
    target.value = row.project;
    confirmCheckout.checked = false;
    drawDetail(row);
    detail.scrollTop = 0;
    try {
      const r = await api.conversationPreview(id);
      if (n !== previewGeneration || !root.isConnected) return;
      preview = r.data;
      previewFor = id;
      drawDetail(row);
      actions();
    } catch (e) {
      if (n !== previewGeneration || !root.isConnected) return;
      detail.append(h('p', { class: 'bad small' }, String(e instanceof Error ? e.message : e)));
    }
  }
  async function chooseHost(id: string): Promise<void> {
    if (busy) return;
    selectedHost = id; selected = ''; rows = []; projects = []; targets = []; project = ''; page = 0;
    preview = null; previewFor = ''; ++previewGeneration;
    clear(list); clear(detail); clear(error); count.textContent = ''; more.classList.add('hidden');
    projectButton.querySelector('.rp-label')!.textContent = 'All projects'; projectClear.classList.add('hidden'); projectMenu.classList.add('hidden');
    await load();
  }
  async function load(append = false): Promise<void> {
    const n = ++generation;
    window.clearTimeout(timer);
    loading = true;
    actions();
    status.classList.remove('bad');
    resumeBody.classList.toggle('hidden', ssh()); sshNote.classList.toggle('hidden', !ssh());
    if (ssh()) {
      sshNote.replaceChildren(h('h3', {}, 'No conversation history on ' + hostLabel()), h('p', { class: 'mut small' }, `${hostLabel()} is connected over SSH only. Without the Piper service there, Piper keeps no saved conversations on it, so nothing can be resumed, forked or imported.`), h('p', { class: 'mut small' }, `You can start a new direct session on ${hostLabel()}. Your search stays and applies again when you choose another host.`));
      clear(status); loading = false; actions(); return;
    }
    if (!rows.length) status.textContent = 'Loading saved conversations…';
    try {
      const host = selectedHost;
      if (host && offline()) {
        await refreshHost(host);
        if (n !== generation || !root.isConnected) return;
        if (offline()) {
          status.replaceChildren('Server unavailable. Last known state; the agent may still be running. ', h('button', { class: 'link small', onclick: () => void load() }, 'Try again'));
          return;
        }
      }
      const r = await api.conversationHistory(search.value, project, agent.value, period.value, append ? page : 0, external.checked, host);
      if (n !== generation || !root.isConnected || !r.data) return;
      const data = r.data;
      projects = data.projects;
      targets = data.targets;
      rows = append ? [...rows, ...data.items] : data.items;
      if (!append) page = 0;
      clear(status);
      if (data.index.error) status.append(h('span', { class: 'bad' }, data.index.error));
      else if (data.index.busy) status.append(h('span', { class: 'spinner' }),
        markBidi(` Indexing history · ${data.index.done}/${data.index.total} conversations`), h('progress', { max: String(data.index.total || 1), value: String(data.index.done) }));
      else status.textContent = markBidi(`${data.total} conversation${data.total === 1 ? '' : 's'}${selectedHost ? ' on ' + hostLabel() : ''} · ↑↓ select · ⏎ resume · Esc close`);
      count.textContent = markBidi(`${rows.length} of ${data.total}`);
      more.classList.toggle('hidden', rows.length >= data.total);
      clear(list);
      for (const row of rows) {
        list.append(h('button', { class: 'resume-result', role: 'option', 'aria-selected': String(row.session === selected),
          'data-session': row.session, tabindex: '-1', onclick: () => void select(row.session), ondblclick: () => void launch(false, row.session) },
          h('span', { class: 'resume-title' }, ...historyMarks(row.title, search.value), row.source === 'local' ? h('span', { class: 'chip small' }, 'outside Piper') : null),
          h('span', { class: 'mut small' }, metadata(row)),
          h('span', { class: 'mut small' }, `Last active: ${sessionDate(row.activity, Date.now() / 1000)}` + (row.source === 'local' ? ' · Outside Piper' : '')),
          h('span', { class: 'resume-snippet small' }, ...historyMarks(row.snippet || row.last || row.first || 'No conversation text yet', search.value)),
          row.closed || row.fork_from ? h('span', { class: 'mut small' }, [row.closed ? 'Closed in Piper' : '', row.fork_from ? 'Fork' : ''].filter(Boolean).join(' · ')) : null));
      }
      if (!rows.length) list.append(h('p', { class: 'mut' }, data.index.busy ? 'Searching while history is indexed…' : 'No matching conversations. Try another project or search.'));
      if (!external.checked && rows.length < 3) list.append(h('button', { class: 'link', onclick: () => chooseSource(true) }, 'Search outside Piper too'));
      if (!rows.some((x) => x.session === selected)) {
        if (rows[0]) void select(rows[0].session);
        else { selected = ''; preview = null; previewFor = ''; ++previewGeneration; clear(detail); actions(); }
      } else {
        const row = rows.find((x) => x.session === selected)!;
        if (!preview || !preview.available && row.available || previewVersion !== JSON.stringify([row.messages, row.last, row.answer, row.title])) void select(selected);
        else if (previewFor === selected) drawDetail(row);   // a new search: this query's match and marks, not the last one's
        actions();
      }
      if (data.index.busy && !append) timer = window.setTimeout(() => void load(), 750);
    } catch (e) {
      if (n !== generation || !root.isConnected) return;
      status.textContent = markBidi(String(e instanceof Error ? e.message : e));
      status.classList.add('bad');
      status.append(' ', h('button', { class: 'mini', onclick: () => void load() }, 'Retry'));
    } finally {
      if (n === generation) { loading = false; actions(); }
    }
  }
  async function launch(asFork: boolean, id = selected, confirmHidden = false): Promise<void> {
    if (id !== selected) { await select(id); }
    if (busy || !id || (asFork ? fork.disabled : resume.disabled)) return;
    busy = true;
    clear(error);
    actions();
    try {
      const r = rows.find(r => r.session === id)?.source === 'local' ?
        await api.importConversation(id, target.value, confirmCheckout.checked, confirmHidden) : await api.resumeConversation(id, asFork);
      if (!r.data) throw new Error('No session was returned.');
      deps.closeModal();
      deps.opened(r.data.session);
    } catch (e) {
      const hidden = hiddenPaths(e);
      if (hidden) error.append(hiddenImportNotice(hidden, () => { clear(error); void launch(false, id, true); }, () => clear(error)));
      else error.textContent = markBidi(String(e instanceof Error ? e.message : e));
    } finally {
      busy = false;
      actions();
    }
  }
  search.addEventListener('input', changed);
  agent.addEventListener('change', changed);
  period.addEventListener('change', changed);
  external.addEventListener('change', changed);
  target.addEventListener('change', () => { confirmCheckout.checked = false; const row = rows.find(r => r.session === selected); if (row) drawDetail(row); actions(); });
  confirmCheckout.addEventListener('change', actions);
  projectSearch.addEventListener('input', drawProjects);
  root.addEventListener('keydown', (e) => {
    if (!projectMenu.classList.contains('hidden')) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); projectMenu.classList.add('hidden'); projectButton.setAttribute('aria-expanded', 'false'); projectButton.focus(); }
      return;
    }
    if (e.target !== search && e.target !== list && !(e.target as HTMLElement).closest('.resume-result')) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const at = rows.findIndex((r) => r.session === selected);
      const next = Math.min(rows.length - 1, Math.max(0, at + (e.key === 'ArrowDown' ? 1 : -1)));
      if (rows[next]) void select(rows[next]!.session);
    } else if (e.key === 'Enter' && !resume.disabled) { e.preventDefault(); void launch(false); }
  });
  if (deps.mount) deps.mount(root);
  else deps.modal('Resume conversation', root);
  syncHost();
  search.focus();
  if (initial) selected = initial;
  void load();
}

export function fmtTokens(n: number): string {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)} M` : n >= 1e3 ? `${Math.round(n / 1e3)} k` : String(n);
}

/** text with every (case-insensitive) occurrence of q wrapped in <mark> - no innerHTML */
export function marked(text: string, q: string): (Node | string)[] {
  if (!q) return [markBidi(text)];
  const out: (Node | string)[] = [];
  let at = 0;
  for (const [i, j] of matchRanges(text, q)) {
    out.push(markBidi(text.slice(at, i)), h('mark', {}, text.slice(i, j)));
    at = j;
  }
  out.push(markBidi(text.slice(at)));
  return out;
}

/** the tool names of a turn that is only `[tool X]` markers, else null */
export function toolOnly(text: string): string[] | null {
  const names = [...text.matchAll(/\[tool ([^\]]+)\]/g)].map((m) => m[1]!);
  return names.length && !text.replace(/\[tool [^\]]+\]/g, '').trim() ? names : null;
}

/** text with its `path:line` references as links (the terminal's FILE_LINK) */
function linked(text: string, open: ((path: string, line: number) => void) | null): (Node | string)[] {
  const refs = open ? fileRefs(text) : [];
  if (!refs.length) return [showBidi(text)];   // Claude's / the user's text: a bidi control is a mark
  const out: (Node | string)[] = [];
  let at = 0;
  for (const r of refs) {
    out.push(showBidi(text.slice(at, r.start)), h('button', { class: 'link', title: showBidi(`open ${r.path} at line ${r.line} in Piper's editor`),
      onclick: () => open!(r.path, r.line) }, showBidi(text.slice(r.start, r.end))));
    at = r.end;
  }
  out.push(showBidi(text.slice(at)));
  return out;
}

/** F05: the transcript's token use in one human line - the exact numbers are the tooltip */
export function usageLine(u: Conversation['usage']): { text: string; title: string } {
  if (!u) return { text: '', title: '' };
  const total = u.input + u.output + u.cache_read + u.cache_write;
  const cache = total ? Math.floor(((u.cache_read + u.cache_write) / total) * 100) : 0;
  const n = (x: number) => x.toLocaleString('en-US');
  return {
    text: `${u.messages} ${u.messages === 1 ? 'answer' : 'answers'}${total ? ` · ${fmtTokens(total)} tokens (${cache} % cache)` : ''}`,
    title: `tokens from the transcript: input ${n(u.input)} · output ${n(u.output)} · cache read ${n(u.cache_read)} · cache write ${n(u.cache_write)}`,
  };
}

/** F05: how long a tool call ran and what it gave ("1.2 s · 3 lines"; "failed" first) */
export function toolMeta(t: ToolCall): string {
  const ms = t.ms;
  const time = ms === undefined ? '' : ms < 10000 ? `${(ms / 1000).toFixed(1)} s` : ms < 60000 ? `${Math.round(ms / 1000)} s` : `${Math.round(ms / 60000)} min`;
  const lines = t.lines === undefined ? '' : `${t.lines} ${t.lines === 1 ? 'line' : 'lines'}`;
  return [t.error ? 'failed' : '', time, lines].filter(Boolean).join(' · ');
}

/** F05: a turn's text split at its `[tool X]` marker lines - text pieces (markdown) and the tool calls in order */
export function segments(text: string, tools: ToolCall[] = []): ({ md: string } | { tool: ToolCall })[] {
  const out: ({ md: string } | { tool: ToolCall })[] = [];
  let buf: string[] = [];
  let k = 0;
  const flush = () => { if (buf.join('\n').trim()) out.push({ md: buf.join('\n') }); buf = []; };
  for (const line of text.split('\n')) {
    const m = /^\s*\[tool ([^\]]+)\]\s*$/.exec(line);
    if (!m || m[1] === 'result') { buf.push(line); continue; }
    flush();
    const t = tools[k];
    out.push({ tool: t && t.name === m[1] ? t : { name: m[1]!, arg: '' } });
    if (t && t.name === m[1]) k++;
  }
  flush();
  return out;
}

/** F05: the transcript's markdown rendered (markdown.ts, never innerHTML); its `path:line` references stay links */
function markdown(text: string, open: ((path: string, line: number) => void) | null = null, reference?: (ref: Reference) => void): HTMLElement {
  const root = renderMarkdown(parseMarkdown(showBidi(text)), { openRel: (href) => {
    const m = /^(.*?):(\d+)$/.exec(href);
    open?.(m ? m[1]! : href, m ? Number(m[2]) : 1);
  } });
  if (reference) linkReferences(root, reference);
  else if (open) linkRefs(root, open);
  return root;
}

/** every text node's `path:line` (outside links and buttons) becomes a button into Piper's editor (K4) */
function linkRefs(el: Node, open: (path: string, line: number) => void): void {
  for (const c of [...el.childNodes]) {
    if (c.nodeType === 1) {
      if (!['A', 'BUTTON'].includes((c as Element).tagName)) linkRefs(c, open);
    } else if (c.nodeType === 3 && fileRefs(c.textContent ?? '').length) {
      const parts = linked(c.textContent ?? '', open);
      for (const x of parts) el.insertBefore(typeof x === 'string' ? document.createTextNode(markBidi(x)) : x, c);
      el.removeChild(c);
    }
  }
}

/** The transcript and terminal share reference recognition, including code / stacktrace text nodes. */
function linkReferences(el: Node, open: (ref: Reference) => void): void {
  for (const child of [...el.childNodes]) {
    if (child.nodeType === 1 && !['A', 'BUTTON'].includes((child as Element).tagName)) linkReferences(child, open);
    else if (child.nodeType === 3) {
      const text = child.textContent ?? '', hits = references(text);
      if (!hits.length) continue;
      let at = 0;
      for (const hit of hits) {
        el.insertBefore(document.createTextNode(markBidi(text.slice(at, hit.start))), child);
        el.insertBefore(h('button', { class: 'link', title: `Open ${hit.text}`, onclick: () => open(hit.ref) }, hit.text), child);
        at = hit.end;
      }
      el.insertBefore(document.createTextNode(markBidi(text.slice(at))), child); el.removeChild(child);
    }
  }
}

export function createConvo(deps: ConvoDeps) {
  const el = h('div', { class: 'convo' });
  const cache = new Map<string, { at: number; data: Conversation }>();
  const requests = new Set<string>();
  const positions = new Map<string, { end: boolean; turn: string | null; offset: number }>();
  let current = '';
  let rendered = '';
  let restoring = false;
  let restoreFrame = 0;
  let filter = '';
  let target = -1;
  let timer = 0;

  async function load(id: string, force = false): Promise<void> {
    // Refresh and session switching must not supersede a still-running request.
    // Otherwise responses slower than REFRESH_MS can be discarded forever.
    if (requests.has(id)) return;
    const hit = cache.get(id);
    if (!force && hit && Date.now() - hit.at < REFRESH_MS) return;
    requests.add(id);
    try {
      const r = await api.conversation(id);
      const prev = cache.get(id)?.data;
      if (r.data) cache.set(id, { at: Date.now(), data: r.data });
      // unchanged transcript: keep the DOM (text selection, scroll position)
      if (prev && r.data && JSON.stringify(prev) === JSON.stringify(r.data)) return;
    } catch (e) {
      // a session removed meanwhile (a reload restoring it, the poll not in yet) is no news: its tile goes with the next state
      if (!(e instanceof ApiError && e.status === 404)) deps.toast(String(e instanceof Error ? e.message : e));
    } finally { requests.delete(id); }
    if (id === current) draw();
  }

  // persistent header: the filter keeps its focus and caret while the turns re-render
  // F05: the filter is this conversation's ⌘F; "Search all" (every conversation, snippets) is the search icon beside it
  const input = h('input', { type: 'search', placeholder: 'Filter transcript', autocomplete: 'off', 'aria-label': 'Filter this transcript (⌘F)' }) as HTMLInputElement;
  input.addEventListener('input', () => { filter = input.value; draw(); });
  const usage = h('span', { class: 'mut cusage' });
  const turns = h('div', { class: 'turns' });
  turns.tabIndex = 0;
  turns.setAttribute('aria-label', 'Transcript');
  const content = h('div', { class: 'turn-list' });
  turns.append(content);
  const visible = () => turns.isConnected && turns.getClientRects().length > 0 && turns.clientHeight > 0;
  function capture() {
    const top = turns.getBoundingClientRect().top;
    const row = [...content.querySelectorAll<HTMLElement>('[data-turn]')].find(r => r.getBoundingClientRect().bottom > top);
    return { end: turns.scrollHeight - turns.scrollTop - turns.clientHeight < 40,
      turn: row?.dataset.turn ?? null, offset: row ? row.getBoundingClientRect().top - top : 0 };
  }
  function remember(): void {
    if (rendered && visible() && !restoring) positions.set(rendered, capture());
  }
  function restore(): void {
    if (!visible() || rendered !== current) return;
    if (target >= 0 && cache.has(current)) {
      const row = content.querySelector<HTMLElement>(`[data-turn="${target}"]`);
      if (row) {
        turns.scrollTop += row.getBoundingClientRect().top - turns.getBoundingClientRect().top - (turns.clientHeight - row.offsetHeight) / 2;
        row.tabIndex = -1;
        row.focus({ preventScroll: true });
      }
      target = -1;
      positions.set(current, capture());
      return;
    }
    const position = positions.get(current);
    if (!position || position.end) turns.scrollTop = turns.scrollHeight;
    else {
      const rows = [...content.querySelectorAll<HTMLElement>('[data-turn]')];
      const row = rows.find(r => r.dataset.turn === position.turn)
        ?? rows.filter(r => Number(r.dataset.turn) < Number(position.turn)).at(-1);
      turns.scrollTop = row ? turns.scrollTop + row.getBoundingClientRect().top - turns.getBoundingClientRect().top - position.offset : 0;
    }
  }
  function restoreSoon(): void {
    restoring = true;
    window.cancelAnimationFrame(restoreFrame);
    restore();
    restoreFrame = window.requestAnimationFrame(() => { restoreFrame = 0; restore(); restoring = false; });
  }
  turns.addEventListener('scroll', remember, { passive: true });
  if (typeof ResizeObserver !== 'undefined') {
    const observer = new ResizeObserver(() => { if (visible()) restoreSoon(); });
    observer.observe(turns); observer.observe(content);
  }
  const open = new Set<string>();   // expanded tool rows (turn:index) - draw() rebuilds the DOM
  el.append(h('div', { class: 'chead' }, input,
    h('button', { class: 'icon csearch', 'aria-label': 'Search all conversations', title: 'Search all conversations', onclick: () => searchAll() }, h('i', { class: 'fi fi-search', 'aria-hidden': 'true' })), usage), turns);
  document.addEventListener('keydown', (e) => {
    const shown = el.isConnected && !el.classList.contains('hidden') && el.getClientRects().length > 0;
    const here = !document.activeElement || document.activeElement === document.body || el.contains(document.activeElement);
    if (!shown || !here || !(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.key.toLowerCase() !== 'f') return;
    e.preventDefault();
    e.stopPropagation();   // capture: the global ⌘F (browser find here) never sees it
    input.focus();
    input.select();
  }, true);

  function toolRow(t: ToolCall, key: string): HTMLElement {
    const isOpen = open.has(key);
    const meta = toolMeta(t);
    const toggle = () => { if (!open.delete(key)) open.add(key); draw(); };
    return h('div', { class: `trow${t.error ? ' failed' : ''}${isOpen ? ' open' : ''}`, 'data-tool-key': key, role: 'button', tabindex: '0', 'aria-expanded': String(isOpen),
      title: isOpen ? 'fold' : 'show the whole call', onclick: toggle,
      onkeydown: (e: Event) => { const k = (e as KeyboardEvent).key; if (k === 'Enter' || k === ' ') { e.preventDefault(); toggle(); } } },
    h('span', { class: 'tcaret', 'aria-hidden': 'true' }),   // a chevron mask, turned by .open
    h('span', { class: 'tname' }, showBidi(t.name)),
    h('span', { class: 'targ' }, showBidi(t.arg)),
    meta ? h('span', { class: 'tmeta' }, meta) : null);
  }

  function draw(): void {
    remember();
    const focused = turns.contains(document.activeElement) ? (document.activeElement as HTMLElement)?.closest<HTMLElement>('[data-tool-key]')?.dataset.toolKey : undefined;
    if (input.value !== filter) input.value = filter;
    clear(content);
    rendered = current;
    const id = current;
    const opener = deps.openFile ? (path: string, line: number) => deps.openFile!(id, path, line) : null;
    const c = cache.get(current)?.data;
    const u = c?.usage;
    const ul = usageLine(u ?? null);
    usage.textContent = markBidi(ul.text);
    usage.title = markBidi(ul.title);
    if (!c) content.append(h('p', { class: 'mut' }, 'loading…'));
    else if (!c.turns.length) content.append(h('p', { class: 'mut' }, c.note === undefined ? 'The transcript is empty so far.' : 'The transcript is not available yet (the session has not reported its ID yet).'));
    c?.turns.forEach((t, i) => {
      if (filter && !t.text.toLowerCase().includes(filter.toLowerCase())) return;
      if (filter) {   // a match: the plain text with its marks
        content.append(h('div', { class: `turn ${t.role}${i === target ? ' target' : ''}`, 'data-turn': String(i) }, who(t, c.agent),
          h('pre', {}, ...marked(t.text, filter))));
        return;
      }
      // F05: markdown rendered, each tool call a grey one-line row (a tool-only turn has no label of its own)
      const parts = segments(t.text, t.tools).map((x, k) => ('md' in x ? h('div', { class: 'tmd' }, markdown(x.md, opener, deps.onReference ? ref => deps.onReference!(id, ref) : undefined)) : toolRow(x.tool, `${i}:${k}`)));
      const only = !!toolOnly(t.text) && i !== target;
      content.append(h('div', { class: `turn ${t.role}${only ? ' tonly' : ''}${i === target ? ' target' : ''}`, 'data-turn': String(i) },
        only ? null : who(t, c.agent), ...parts));
    });
    if (focused) content.querySelector<HTMLElement>(`[data-tool-key="${focused}"]`)?.focus({ preventScroll: true });
    restoreSoon();
  }

  function who(t: { role: string; ts?: string | null }, agent?: string): HTMLElement {
    return h('div', { class: 'who' }, t.role === 'user' ? 'You / task' : agent === 'codex' ? 'Codex' : 'Claude', t.ts ? h('span', { class: 'mut', title: new Date(t.ts).toLocaleString() }, ' ' + auditTime(t.ts)) : null);
  }

  function searchAll(): void {
    if (deps.resumeConversations) { deps.resumeConversations(); return; }
    const input = h('input', { type: 'search', placeholder: 'Text in the conversations of all sessions, Enter', value: filter, autocomplete: 'off' }) as HTMLInputElement;
    const list = h('div', { class: 'plist tall' });
    const run = async () => {
      clear(list);
      list.append(h('p', { class: 'mut' }, 'searching…'));
      let hits: ConvHit[] = [];
      try {
        hits = (await api.searchConversations(input.value)).data?.hits ?? [];
      } catch (e) {
        clear(list);
        list.append(h('p', { class: 'bad' }, String(e instanceof Error ? e.message : e)));
        return;
      }
      clear(list);
      for (const x of hits) {
        list.append(h('button', { class: 'pitem col', onclick: () => { deps.closeModal(); filter = input.value; deps.jump(x.session, x.turn); } },
          h('span', { class: 'name' }, `${x.title} · ${x.role === 'user' ? 'you' : 'Claude'}`),
          h('span', { class: 'mut snippet' }, ...marked(x.text, input.value))));
      }
      if (!hits.length) list.append(h('p', { class: 'mut' }, 'nothing found'));
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); void run(); } });
    deps.modal('Search conversations', input, list);
    input.focus();
  }

  return {
    el,
    searchAll,
    show(id: string, turn = -1): void {
      remember();
      if (id !== current) {
        current = id;
        filter = turn >= 0 ? filter : '';
      }
      target = turn;
      draw();
      void load(id, true);
      window.clearInterval(timer);
      timer = window.setInterval(() => { if (el.isConnected && !el.classList.contains('hidden')) void load(current); }, REFRESH_MS);
    },
    hide(): void {
      remember();
      window.cancelAnimationFrame(restoreFrame); restoreFrame = 0; restoring = false;
      window.clearInterval(timer);
    },
  };
}
