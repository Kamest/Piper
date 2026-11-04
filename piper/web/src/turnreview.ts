// Claude's Review lane (boards 03–05): permanent session tab, backed by the pinned review API.
import { api, reviewDiff } from './api.js';
import { clear, h } from './dom.js';
import { notesMessage, numberDiff, projectRel, type Note } from './gitpanel.js';
import { diffPaths, filesLabel, turnLabel } from './turns.js';
import type { RevertPreview, ReviewFiles, ReviewSummary, Session, TurnList, TurnSnap } from './types.js';
import { markBidi } from './text.js';

interface LaneNote extends Note { turn: number | null; sha: string | null }
interface ReviewDeps {
  session(id: string): Session | undefined;
  modal(title: string, ...nodes: (Node | null)[]): void;
  closeModal(): void;
  toast(text: string, action?: { label: string; run(): void }): void;
  changed(): void;
}

export function revertDisabled(s: Session | undefined, turns: TurnSnap[]): boolean {
  return !s || !['ready', 'waiting', 'attention', 'exited', 'error', 'blocked'].includes(s.state)
    || !!Object.keys(s.subagents ?? {}).length || !!Object.keys(s.background_tasks ?? {}).length
    || (!['exited', 'error', 'blocked'].includes(s.state) && !!turns.length
      && !turns[turns.length - 1]?.to && !turns[turns.length - 1]?.ended);
}

export function reviewFoot(summary: ReviewSummary | undefined, notes: number): string {
  return [summary?.tests ? `Tests ${summary.tests.status} after turn ${summary.tests.turn}` : '',
    notes ? `${notes} ${notes === 1 ? 'note' : 'notes'}` : ''].filter(Boolean).join(' · ');
}

export function failedLabel(turn: TurnSnap): string {
  return turn.failed ? ` · ${turn.failed === 1 ? 'snapshot' : `${turn.failed} snapshots`} failed` : '';
}

export function revertFileLabel(file: RevertPreview['files'][number], n: number): string {
  return file.removed ? `removed${file.added_in ? ` (added in turn ${file.added_in})` : ''}` : `back to turn ${n}`;
}

export function createTurnReview(deps: ReviewDeps) {
  const el = h('div', { class: 'turn-review' });
  const list = h('aside', { class: 'tr-turns', 'aria-label': 'Turns' });
  const detail = h('div', { class: 'tr-detail' });
  const foot = h('div', { class: 'tr-foot' });
  el.append(h('div', { class: 'tr-body' }, list, detail), foot);
  let id = '', selected: number | null = null, generation = 0, stamp = '', edits = '', controls = '', fetched = 0, busy = false;
  let data: ReviewFiles | null = null, turns: TurnList = { turns: [] }, notes: LaneNote[] = [];
  let history: Promise<TurnList> | null = null, loading = false, again = false, shown = '';
  const comparisons = new Map<number | null, Promise<{ data: ReviewFiles; text: string }>>();
  const compared = new Map<number | null, string[]>();   // files of a loaded comparison (their work_sha ages with edits)
  let unknownEdit = 0;
  // A turn change (state, summary) refreshes the history; the agent's edits (fsedit) only change the
  // live comparisons - All turns and a running turn. A finished turn's trees are pinned: kept as is.
  const version = () => { const s = deps.session(id); return JSON.stringify([s?.state, s?.review_summary]); };
  const editVersion = () => JSON.stringify(deps.session(id)?.fsedit ?? null);
  const live = (n: number | null) => { const turn = turns.turns.find(t => t.n === n); return n === null || (!turn?.to && !turn?.ended); };
  const dropLive = () => { for (const n of [...comparisons.keys()]) if (live(n)) comparisons.delete(n); edits = editVersion(); };
  /** Edits drop the live comparisons and a finished turn whose files the agent touched: a note
   * carries the file's current version (work_sha), which the server checks before sending. */
  const dropEdited = () => {
    const f = deps.session(id)?.fsedit, all = !!f?.unknown && f.unknown !== unknownEdit, paths = f?.paths ?? [];
    unknownEdit = f?.unknown ?? 0;
    const hit = (n: number | null) => all || (compared.get(n) ?? []).some(p => paths.some(x => p === x || p.endsWith('/' + x) || x.endsWith('/' + p)));
    let selectedHit = false;
    for (const n of [...comparisons.keys()]) if (!live(n) && hit(n)) { comparisons.delete(n); selectedHit ||= n === selected; }
    dropLive();
    return selectedHit;
  };
  const invalidate = () => { history = null; comparisons.clear(); compared.clear(); unknownEdit = deps.session(id)?.fsedit?.unknown ?? 0; data = null; stamp = version(); edits = editVersion(); fetched = Date.now(); };
  const message = (e: unknown) => e instanceof Error ? e.message : String(e);
  const key = () => 'piper.turn-review.notes.' + id;
  const save = () => { try { localStorage.setItem(key(), JSON.stringify(notes)); } catch { /* keep the draft in memory */ } };

  function drawTurns(): void {
    clear(list);
    list.append(h('div', { class: 'tr-list-head' }, 'TURNS', h('span', { class: 'mut' }, 'newest first')));
    const summary = deps.session(id)?.review_summary;
    const stats = (s?: ReviewSummary) => s?.files === 0 ? h('small', { class: 'mut' }, 'no files') : typeof s?.files === 'number' ? h('small', { class: 'mut' }, filesLabel(s.files), ' ',
      h('span', { class: 'ok' }, `+${s.add}`), ' ', h('span', { class: 'bad' }, `−${s.del}`)) : null;
    list.append(h('button', { class: 'tr-all' + (selected === null ? ' on' : ''), 'aria-pressed': String(selected === null),
      onclick: () => choose(null) }, 'All turns since the session started', stats(summary)));
    const disabled = revertDisabled(deps.session(id), turns.turns);
    for (const turn of [...turns.turns].reverse()) {
      const row = h('div', { class: 'tr-turn' + (selected === turn.n ? ' on' : '') + (turn.reverted ? ' reverted' : ''), 'data-turn': turn.n });
      const pick = h('button', { class: 'tr-pick', 'aria-pressed': String(selected === turn.n), onclick: () => choose(turn.n) },
        h('span', { class: 'tr-meta mut' }, `turn ${turn.n}${turn.reverted ? ' · reverted' : ''}${turn.unavailable ? ' · snapshot unavailable' : !turn.to && !turn.ended ? ' · running' : ''}${failedLabel(turn)}`,
          h('time', {}, new Date((turn.ended ?? turn.started) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }))),
        h('span', {}, turn.prompt || 'No prompt recorded'), turn.unavailable ? h('small', { class: 'warn' }, 'Could not check: snapshot is unavailable') : stats(turn.summary));
      row.append(pick);
      if (turn.to || turn.unavailable) row.append(h('button', { class: 'mini tr-revert', disabled: disabled || !!turn.unavailable || busy,
        title: turn.unavailable || (disabled ? 'Stop the turn first (Esc)' : 'Restore files to the end of this turn'),
        onclick: () => void confirmRevert(turn.n) }, turn.reverted ? 'Restore' : '↶ Revert to here'));
      list.append(row);
    }
  }

  /** A click marks the new turn at once; the previous comparison does not stay on screen while loading. */
  function choose(n: number | null): void {
    selected = n; shown = '';
    drawTurns(); clear(detail); detail.append(h('p', { class: 'mut' }, 'Loading…'));
    void load();
  }

  function drawFoot(): void {
    clear(foot);
    const s = deps.session(id), disabled = revertDisabled(s, turns.turns) || !s?.at_prompt;
    foot.append(h('span', { class: 'mut grow' }, reviewFoot(s?.review_summary, notes.length)),
      h('button', { disabled: !notes.length || !data || disabled || busy, title: disabled ? 'Send when the session is idle' : 'Send the review notes as one prompt',
        onclick: () => void sendNotes() }, notes.length ? `Send ${notes.length} ${notes.length === 1 ? 'note' : 'notes'} to the agent` : 'Send notes to the agent'),
      h('button', { class: 'primary', disabled: busy, onclick: () => void done() }, 'Done reviewing'));
  }

  async function sendNotes(): Promise<void> {
    const sid = id, sending = [...notes];
    busy = true; drawFoot();
    try {
      const project = deps.session(sid)?.path ?? '';
      const targets = sending.map(n => ({ ...n, path: projectRel(n.path, data?.root ?? project, project) }));
      const r = await api.reviewNotes(sid, notesMessage(targets), targets.map(n => ({ path: n.path, sha: n.sha })));
      if (r.data?.sent && sid === id) { notes = notes.filter(n => !sending.includes(n)); save(); drawFoot(); void load(); }
      deps.changed();
    } catch (e) { deps.toast('Not sent: ' + message(e)); }
    finally { busy = false; drawFoot(); }
  }

  async function done(): Promise<void> {
    try { await api.reviewDone(id); deps.changed(); deps.toast('Done reviewing'); }
    catch (e) { deps.toast(message(e)); }
  }

  function noteAt(path: string, line: number, old: boolean, anchor: HTMLElement): void {
    const file = data?.files.find(f => f.path === path);
    if (!file || file.sensitive || file.withheld) return;
    const text = h('textarea', { rows: 2, placeholder: 'Note for the agent', 'aria-label': `Note on ${path} line ${line}` });
    const editor = h('div', { class: 'tr-note' }, h('small', { class: 'mut' }, `You · ${path} line ${line}`), text,
      h('div', { class: 'row' }, h('button', { onclick: () => editor.remove() }, 'Cancel'),
        h('button', { onclick: () => {
          if (!text.value.trim()) return;
          notes.push({ path, line, old, text: text.value.trim(), turn: selected, sha: file.work_sha ?? null });
          save(); editor.remove(); drawNotes(); drawFoot();
        } }, 'Add note')));
    detail.querySelector('.tr-note-editor')?.remove(); editor.classList.add('tr-note-editor');
    anchor.after(editor); text.focus();
  }

  function drawNotes(): void {
    for (const old of detail.querySelectorAll('.tr-saved-note')) old.remove();
    for (const note of notes.filter(n => selected === null || n.turn === selected)) {
      const block = h('div', { class: 'tr-note tr-saved-note' }, h('small', { class: 'mut' }, `You · ${note.path} line ${note.line}`),
        h('p', {}, note.text), h('button', { class: 'link small', onclick: () => { notes = notes.filter(n => n !== note); save(); drawNotes(); drawFoot(); } }, 'Remove'));
      (Array.from(detail.querySelectorAll<HTMLElement>('[data-file]')).find(e => e.dataset.file === note.path) ?? detail).append(block);
    }
  }

  function drawDiff(text: string): void {
    clear(detail);
    const turn = turns.turns.find(t => t.n === selected);
    detail.append(h('strong', { class: 'tr-diff-title' }, turn ? turnLabel(turn, 160) : 'All turns since the session started'));
    if (data?.note || turns.note) detail.append(h('p', { class: 'mut small' }, data?.note || turns.note || ''));
    if (!data?.files.length) detail.append(h('p', { class: 'mut' }, 'No files changed.'));
    const lines = numberDiff(text), paths = diffPaths(lines), info = new Map((data?.files ?? []).map(f => [f.path, f]));
    let file = '', code = h('div'), section = h('section');
    lines.forEach((line, i) => {
      if (i === lines.length - 1 && !line.text) return;
      const path = paths[i] ?? '';
      if (!path) return;
      if (path !== file) {
        file = path;
        const row = info.get(path);
        section = h('section', { class: 'tr-file', 'data-file': path }, h('div', { class: 'tr-file-head mono' }, path,
          row ? h('span', { class: 'mut' }, ` +${row.add ?? '–'} −${row.del ?? '–'}`) : null));
        code = h('div', { class: 'tr-code mono' }); section.append(code); detail.append(section);
      }
      if (line.cls === 'meta' && !/^(Binary|Submodule|\[)/.test(line.text)) return;
      const n = line.newNo ?? line.oldNo;
      const row = h('div', { class: 'tr-line ' + line.cls }, h('span', { class: 'tr-line-text' }, line.text));
      const meta = info.get(path);
      if (n !== null) row.prepend(h('button', { class: 'tr-line-no', title: meta?.sensitive ? 'Hidden files cannot be sent to the agent' : `Note on ${path} line ${n}`,
        disabled: !meta || meta.sensitive || meta.withheld, onclick: () => noteAt(path, n, line.newNo === null, section) }, String(n)));
      code.append(row);
    });
    drawNotes();
  }

  function drawUnavailable(turn?: TurnSnap): void {
    clear(detail);
    detail.append(h('strong', { class: 'tr-diff-title' }, turn ? turnLabel({ ...turn, unavailable: '' }, 160) : 'All turns since the session started'));
    if (turn) detail.append(h('p', { class: 'mut small' },
      `${new Date(turn.started * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}–${turn.ended ? new Date(turn.ended * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '…'} · snapshot unavailable`));
    detail.append(h('p', { class: 'bad' }, 'Could not check: snapshot is unavailable'),
      h('p', { class: 'mut' }, turn ? 'This turn cannot be compared or reverted. Other turns keep their own snapshots and times.'
        : 'The session comparison is incomplete. Select an available turn to review its changes.'));
  }

  async function load(): Promise<void> {
    if (Date.now() - fetched > 15000) invalidate();
    const sid = id, gen = ++generation, pick = selected;
    loading = true; again = false;
    try {
      // History and the comparison are independent. Share in-flight reads and keep a
      // small per-session cache until a turn/state/file change invalidates it.
      if (!history) {
        const request = api.turns(sid).then(r => r.data ?? { turns: [] }).catch(e => {
          if (history === request) history = null;
          throw e;
        });
        history = request;
      }
      let comparison = comparisons.get(pick);
      if (!comparison) {
        comparison = (async () => {
          const r = (await api.reviewFiles(sid, pick === null ? 'session' : 'turn', pick ?? 0)).data;
          if (!r) throw new Error('Review comparison is unavailable');
          return { data: r, text: await reviewDiff(sid, r.id) };
        })();
        comparisons.set(pick, comparison);
        if (comparisons.size > 8) comparisons.delete(comparisons.keys().next().value!);
        const request = comparison;
        void request.catch(() => { if (comparisons.get(pick) === request) comparisons.delete(pick); });
      }
      const [r, result] = await Promise.allSettled([history, comparison]);
      if (gen !== generation || sid !== id) return;
      if (r.status === 'rejected') throw r.reason;
      turns = r.value; data = null; drawTurns(); drawFoot();
      const turn = turns.turns.find(t => t.n === pick);
      if (turn?.unavailable || (pick === null && turns.turns.some(t => t.unavailable))) { shown = ''; drawUnavailable(turn); return; }
      if (result.status === 'rejected') throw result.reason;
      const resolved = result.value;
      compared.set(pick, resolved.data.files.flatMap(f => f.orig ? [f.path, f.orig] : [f.path]));
      data = resolved.data; drawFoot();
      // The same pinned comparison keeps its nodes while file versions and history refresh.
      const view = JSON.stringify([pick, data.from, data.to, data.note, turns.note, resolved.text.length]);
      if (view !== shown || !detail.querySelector('.tr-diff-title')) { drawDiff(resolved.text); shown = view; }
    } catch (e) {
      if (gen !== generation || sid !== id) return;
      clear(detail); detail.append(h('p', { class: 'bad' }, 'Could not check: ' + message(e))); shown = '';
    } finally {
      if (gen === generation) {
        loading = false;
        if (again && sid === id) void load();   // changes seen meanwhile: one more refresh, never a pile-up
      }
    }
  }

  /** Background refresh: at most one in flight; later changes coalesce into one more. */
  function refresh(): void {
    if (loading) again = true;
    else void load();
  }

  async function confirmRevert(n: number, undo = '', sid = id): Promise<void> {
    const restore = !undo && sid === id && !!turns.turns.find(t => t.n === n)?.reverted;
    try {
      const preview = (await api.revertPreview(sid, n, undo)).data;
      if (!preview) throw new Error('Revert preview is unavailable');
      const title = undo ? 'Undo revert?' : restore ? `Restore turn ${n}?` : `Revert ${deps.session(sid)?.title ?? 'session'} to the end of turn ${n}?`;
      const box = h('div', { class: 'tr-confirm' },
        h('p', { class: 'mut' }, undo ? 'The saved files are restored in the session’s worktree:' : restore ? `These files come back from turn ${n}:` : 'Later changes are undone in the session’s worktree:'),
        h('div', { class: 'tr-confirm-files' }, ...preview.files.map(f => h('div', {}, h('span', { class: 'mono' }, f.path), ' ',
          h('small', { class: f.removed ? 'bad' : 'mut' }, undo ? (f.removed ? 'removed' : 'restored') : restore && !f.removed ? `from turn ${n}` : revertFileLabel(f, n))))),
        h('p', { class: 'mut small' }, h('span', { class: 'ok' }, '✓ '), 'Your current files are saved first: Undo revert stays available in History.'),
        h('p', { class: 'mut small' }, h('span', { class: 'ok' }, '✓ '), 'Commits and the index are not touched. Reverted turns stay in the list, greyed out.'),
        h('p', { class: 'mut small' }, h('span', { class: 'ok' }, '✓ '), 'The agent gets a note with the reverted files when idle, so its next turn starts from them.'));
      const apply = h('button', { class: 'primary', disabled: !preview.files.length }, undo ? 'Undo revert' : restore ? 'Restore files' : '↶ Revert files');
      const error = h('p', { class: 'bad small', role: 'alert' });
      apply.onclick = async () => {
        apply.disabled = true;
        try {
          const r = (await api.revertTurn(sid, preview)).data;
          if (!r) throw new Error('Revert did not return a result');
          deps.closeModal();
          deps.toast(undo ? 'Restored files' : restore ? `Restored turn ${n}` : `Reverted to turn ${n}`, { label: 'Undo', run: () => void confirmRevert(0, r.undo, sid) });
          deps.changed(); if (sid === id) { invalidate(); void load(); }
        } catch (e) { error.textContent = markBidi(message(e)); apply.disabled = false; }
      };
      box.append(error, h('div', { class: 'row right' }, h('button', { onclick: () => deps.closeModal() }, 'Cancel'), apply));
      deps.modal(title, box);
    } catch (e) { deps.toast(message(e)); }
  }

  return {
    el,
    show(sid: string): void {
      if (sid !== id) {
        id = sid; selected = null; data = null; turns = { turns: [] };
        invalidate();
        try { const stored: unknown = JSON.parse(localStorage.getItem(key()) ?? '[]'); notes = Array.isArray(stored) ? stored.filter(n => n && typeof n.path === 'string' && typeof n.text === 'string' && typeof n.line === 'number') : []; }
        catch { notes = []; }
      }
      if (stamp !== version() || Date.now() - fetched > 15000) invalidate();
      clear(detail); detail.append(h('p', { class: 'mut' }, 'Loading…')); void load();
    },
    sync(): void {
      const s = deps.session(id), next = JSON.stringify([s?.state, s?.at_prompt, s?.subagents, s?.background_tasks]);
      if (stamp !== version()) { history = null; dropLive(); stamp = version(); fetched = Date.now(); refresh(); }
      else if (edits !== editVersion()) { if (dropEdited() || live(selected)) refresh(); }
      else if (controls !== next) { drawTurns(); drawFoot(); }
      controls = next;
    },
    undo(sid: string, rid: string): void { void confirmRevert(0, rid, sid); },
  };
}
