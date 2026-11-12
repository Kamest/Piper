// Quick note (⌘⇧J, module `notes`): a line at the end of the main note, or a pasted text as its own file in the
// notes folder. ⏎ saves, ⇧⏎ is a new line, ⌘⏎ saves and opens Notes, Esc closes and keeps the draft.

import { appMod, isMac, kbd } from './clipkeys.js';
import { fi, h } from './dom.js';
import { markBidi } from './text.js';
import {
  autoMode, canReveal, copyPath, deniedBox, deniedOf, entryPreview, looksJson, noteFileName, notesApi, revealLabel,
  revealPath, sizeText, type NotesStatus, type WriteResult,
} from './notes.js';

const DRAFT = 'piper.notes.draft';
interface Draft { text: string; mode: 'line' | 'file'; name: string; also: boolean }

function readDraft(): Draft | null {
  try { const d = JSON.parse(localStorage.getItem(DRAFT) || 'null') as Draft | null; return d && typeof d.text === 'string' ? d : null; } catch { return null; }
}
function saveDraft(d: Draft | null): void {
  try { if (d && (d.text || d.name)) localStorage.setItem(DRAFT, JSON.stringify(d)); else localStorage.removeItem(DRAFT); } catch { /* private browser */ }
}

export interface QuickDeps {
  modal(title: string, ...content: (Node | null)[]): void;
  closeModal(): void;
  toast(text: string, action?: { label: string; run(): void }[]): void;
  /** the Notes view's last folder status (null: not loaded yet) */
  status(): NotesStatus | null;
  beforeWrite(): Promise<void>;
  written(r: WriteResult): Promise<void>;
  /** the Notes view on a file */
  openNotes(name: string): void;
  setDir(dir: string): Promise<void>;
}

export function openQuickNote(deps: QuickDeps, want?: 'line' | 'file', given?: string): void {
  const draft = readDraft();
  let status = deps.status();
  let mode: 'line' | 'file' = want ?? draft?.mode ?? 'line';
  let manual = !!want;
  let busy = false;
  const text = h('textarea', { class: 'qn-text', rows: '5', 'aria-label': 'Note text', placeholder: 'A sentence, a link, a pasted export…' }) as HTMLTextAreaElement;
  text.value = given ?? (draft && (!want || draft.mode === want) ? draft.text : '');
  if (given) mode = 'file';
  const name = h('input', { class: 'qn-name', type: 'text', 'aria-label': 'File name', spellcheck: 'false' }) as HTMLInputElement;
  name.value = draft && mode === 'file' && !given ? draft.name : '';
  const ghost = h('span', { class: 'qn-ext mut' });
  const badge = h('span', { class: 'qn-badge small' });
  const also = h('input', { type: 'checkbox' }) as HTMLInputElement;
  also.checked = draft?.also ?? true;
  const alsoText = h('span', {});
  const info = h('div', { class: 'qn-info' });
  const dest = h('div', { class: 'qn-dest mut small' });
  const err = h('div', { class: 'qn-err' });
  const go = h('button', { class: 'primary', onclick: () => void submit(false) }) as HTMLButtonElement;
  const hints = h('div', { class: 'qn-hints mut small' });
  const segs = h('span', { class: 'db-segs qn-mode', role: 'group', 'aria-label': 'Save as' });
  const fileRow = h('div', { class: 'qn-file' },
    h('label', { class: 'qn-label mut small' }, 'FILE NAME'),
    h('div', { class: 'qn-name-wrap' }, name, ghost, badge));
  const alsoRow = h('label', { class: 'qn-also' }, also, alsoText);
  const box = h('div', { class: 'qn' }, info, text, fileRow, alsoRow, dest, err,
    h('div', { class: 'qn-foot' }, hints, h('span', { class: 'grow' }), go));

  const folder = () => status?.shown || status?.dir || '~/Piper Notes';
  const main = () => status?.main || 'notes.md';
  const keep = () => saveDraft({ text: text.value, mode, name: name.value, also: also.checked });

  function draw(): void {
    const now = new Date();
    segs.replaceChildren(...([['line', `Line in ${main()}`], ['file', 'New file']] as const).map(([v, label]) =>
      h('button', { class: 'db-seg' + (mode === v ? ' on' : ''), 'aria-pressed': String(mode === v),
        onclick: () => { mode = v; manual = true; keep(); draw(); text.focus(); } }, label)));
    const file = mode === 'file';
    box.classList.toggle('qn-is-file', file);
    fileRow.hidden = alsoRow.hidden = !file;
    const json = looksJson(text.value);
    const why = autoMode(text.value).why;
    info.hidden = !(file && why && !manual);
    info.replaceChildren(fi('doc'), why === 'json' ? 'The pasted text is JSON - it is saved as its own file, not as a line.'
      : 'A longer text - it is saved as its own file, not as a line.');
    const target = noteFileName(name.value, text.value, now);
    const typedExt = /\.[A-Za-z0-9]{1,10}$/.test(name.value.trim());
    ghost.textContent = markBidi(name.value.trim() && !typedExt ? target.slice(target.lastIndexOf('.')) : '');
    name.placeholder = markBidi(target);
    const bytes = new TextEncoder().encode(text.value).length;
    badge.replaceChildren(...(json ? [fi('check'), markBidi(`valid JSON · ${sizeText(bytes)}`)] : text.value ? [markBidi(sizeText(bytes))] : []));
    badge.classList.toggle('ok', json);
    const p = entryPreview(status?.stamp ?? 'time', now);
    alsoText.textContent = markBidi(`Also add a line to ${main()}: - ${p.prefix ? p.prefix + ' ' : ''}Stashed ${target}`);
    const first = text.value.split('\n')[0]!.trim();
    dest.textContent = markBidi(file ? `↳ ${folder()}/${target}`
      : `↳ ${folder()}/${main()}${p.heading ? ` under ${p.heading}` : ''} as - ${p.prefix ? p.prefix + ' ' : ''}${first ? (first.length > 40 ? first.slice(0, 40) + '…' : first) : '…'}`);
    go.textContent = file ? 'Save file' : 'Append';
    go.disabled = busy || !text.value.trim();
    hints.replaceChildren(
      h('kbd', {}, '⏎'), file ? ' save  ' : ' append  ', h('kbd', {}, '⇧⏎'), ' new line  ',
      h('kbd', {}, kbd('⌘⏎')), file ? ' save and open  ' : ' append and open Notes  ', h('kbd', {}, 'Esc'), ' close, draft kept');
  }

  // typing here is a draft that survives Esc (localStorage): no "discard?" step on the overlay
  const quiet = (e: Event) => { e.stopPropagation(); keep(); };
  text.addEventListener('input', (e) => {
    quiet(e);
    if (!manual && looksJson(text.value)) mode = 'file';
    draw();
  });
  text.addEventListener('paste', () => window.setTimeout(() => { if (!manual) { mode = autoMode(text.value).mode === 'file' ? 'file' : mode; draw(); } }));
  name.addEventListener('input', (e) => { quiet(e); draw(); });
  also.addEventListener('change', keep);
  box.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing || !(e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLInputElement) || e.target === also) return;
    const open = appMod(e, isMac(navigator.platform || navigator.userAgent));
    if (e.shiftKey && !open) return;   // ⇧⏎: a new line
    e.preventDefault();
    void submit(open);
  });

  async function submit(open: boolean): Promise<void> {
    const body = text.value.replace(/\s+$/, '');
    if (busy || !body.trim()) return;
    busy = true; err.replaceChildren(); draw();
    try {
      await deps.beforeWrite();
      const r = mode === 'file' ? await notesApi.file(body, name.value.trim(), also.checked) : await notesApi.append(body);
      saveDraft(null);
      deps.closeModal();
      await deps.written(r);
      const acts = [
        ...(canReveal() ? [{ label: revealLabel(), run: () => revealPath(r.path, (t) => deps.toast(t)) }] : []),
        { label: 'Copy path', run: () => copyPath(r.path, (t) => deps.toast(t)) },
      ];
      if (open) deps.openNotes(r.name);
      else deps.toast(mode === 'file' ? `✓ Saved ${r.name} to ${folder()}${r.renamed ? ' (the name was taken)' : ''}` : `✓ Added a line to ${r.name}`,
        mode === 'file' ? acts : [{ label: 'Open', run: () => deps.openNotes(r.name) }]);
    } catch (e) {
      const d = deniedOf(e);
      if (d) {
        status = { ...(status ?? d), ...d };
        err.replaceChildren(deniedBox(d, () => void submit(open), () => void deps.setDir(d.fallback).then(() => notesApi.status()).then((s) => { status = s; void submit(open); })));
      } else err.replaceChildren(h('div', { class: 'nt-refused', role: 'alert' }, fi('alert'), h('span', {}, e instanceof Error ? e.message : String(e))));
    } finally {
      busy = false;
      if (box.isConnected) draw();
    }
  }

  deps.modal('Quick note', box);
  const dialog = box.closest('.modal');
  dialog?.classList.add('qn-modal');
  // boards 02 / 03: the notebook icon before the title, the Line / New file switch at the right of the title row
  const title = dialog?.querySelector(':scope > h2');
  if (title) { title.classList.add('qn-title'); title.prepend(fi('notes')); title.append(h('span', { class: 'grow' }), segs); }
  else box.prepend(segs);
  draw();
  text.focus();
  text.setSelectionRange(text.value.length, text.value.length);
  if (!status) void notesApi.status().then((s) => { status = s; if (box.isConnected) draw(); }).catch(() => {});
}
