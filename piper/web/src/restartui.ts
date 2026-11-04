import { RestartCheck, type RestartStatus } from './restartstatus.js';
import { createRestartView } from './restartview.js';
import { h } from './dom.js';
import { markBidi } from './text.js';

let layer: HTMLDialogElement | null = null, view: ReturnType<typeof createRestartView> | null = null;
let restore: HTMLElement | null = null, answered = false, hidden = false;
let inputSelection: { start: number; end: number; direction: 'forward' | 'backward' | 'none' } | null = null;
let selectionRanges: Range[] = [];
let notice: HTMLElement | null = null, noticeText: HTMLElement | null = null;
const controller = new RestartCheck(update);
// A desktop-owned check can finish while its local window is hidden. Reuse the existing toast style.
if (typeof window !== 'undefined') window.__piperRestart?.listen?.(text => {
  let box = document.getElementById('toasts');
  if (!box) { box = h('div', { id: 'toasts', role: 'status' }); document.body.append(box); }
  const result = h('div', { class: 'toast' }, text); box.append(result);
  setTimeout(() => result.remove(), 10000);
});
function showNotice(s: RestartStatus): void {
  if (!notice) {
    let box = document.getElementById('toasts');
    if (!box) { box = h('div', { id: 'toasts', role: 'status' }); document.body.append(box); }
    noticeText = h('span');
    notice = h('div', { class: 'toast ext-restart' }, noticeText, h('button', { class: 'tact', onclick: () => {
      hidden = false; notice?.remove(); notice = null; noticeText = null;
      if (controller.status) update(controller.status);
    } }, 'Show'));
    box.append(notice);
  }
  noticeText!.textContent = markBidi(s.checking ? 'Checking the Piper restart…' : s.finding);
}
function close(): void {
  hidden = true;
  layer?.close(); layer?.remove(); layer = null;
  view?.dispose(); view = null;
  window.__piperRestart?.visible(!!controller.status?.checking);
  notice?.remove(); notice = null; noticeText = null;
  if (controller.status?.checking) showNotice(controller.status);
  if (restore?.isConnected) {
    restore.focus({ preventScroll: true });
    if (inputSelection) (restore as HTMLInputElement).setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction);
    else if (selectionRanges.length) {
      const selection = document.getSelection(); selection?.removeAllRanges();
      for (const range of selectionRanges) if (range.commonAncestorContainer.isConnected) selection?.addRange(range);
    }
  }
}
function update(s: RestartStatus): void {
  if (s.phase === 'recovered' || s.phase === 'returned') answered = true;
  if (hidden) { window.__piperRestart?.visible(s.checking); showNotice(s); return; }
  if (!view) {
    // Reopening from the toast must retain the workspace target, rather than the removed Show button.
    if (!restore?.isConnected) {
      restore = document.activeElement as HTMLElement | null;
      const input = restore as HTMLInputElement | null;
      inputSelection = typeof input?.selectionStart === 'number' && typeof input.setSelectionRange === 'function'
        ? { start: input.selectionStart, end: input.selectionEnd ?? input.selectionStart, direction: input.selectionDirection || 'none' } : null;
      const selection = document.getSelection?.();
      selectionRanges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
    }
    view = createRestartView({ close, check: () => { void controller.check(); }, copy: text => navigator.clipboard.writeText(text) });
    layer = document.createElement('dialog'); layer.className = 'restart-layer';
    layer.setAttribute('aria-label', 'Piper restart diagnostics');
    layer.append(view.el); document.body.append(layer);
    layer.addEventListener('cancel', event => { event.preventDefault(); close(); });
    // Keep workspace shortcuts (including Escape) out of the retained renderer underneath the modal.
    layer.addEventListener('keydown', event => { event.stopPropagation(); });
    layer.showModal();
    window.__piperRestart?.visible(true);
  }
  view.update(s);
}
export const restartState = () => controller.status;
export const restartInProgress = () => !!controller.status?.checking;
export function restartDidAnswer(): boolean { const result = answered; answered = false; return result; }
export async function showRestart(route: '/api/restart' | '/api/service/reinstall' = '/api/restart'): Promise<void> {
  hidden = false;
  if (controller.status?.checking) { update(controller.status); return; }
  if (!view) { restore = null; inputSelection = null; selectionRanges = []; }
  answered = false; await controller.start(route);
}
