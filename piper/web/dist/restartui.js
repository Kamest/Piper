import { RestartCheck } from './restartstatus.js';
import { createRestartView, restartLogo } from './restartview.js';
import { h } from './dom.js';
import { markBidi } from './text.js';
let layer = null, view = null;
let restore = null, answered = false, hidden = false;
let inputSelection = null;
let selectionRanges = [];
let notice = null, noticeText = null;
let connectionNotice = null, connectionText = null;
let connectionTimer = 0, diagnosticTimer = 0, outagePending = false, outageShowing = false;
const controller = new RestartCheck(update);
function clearConnectionNotice() {
    window.clearTimeout(connectionTimer);
    window.clearTimeout(diagnosticTimer);
    connectionNotice?.remove();
    connectionNotice = null;
    connectionText = null;
}
function showConnectionNotice() {
    const status = controller.status;
    if (!status || status.kind !== 'outage' || status.reachable || layer)
        return;
    if (!connectionNotice) {
        connectionText = h('span');
        connectionNotice = h('div', { class: 'connection-notice', role: 'status', 'aria-live': 'polite' }, h('img', { class: 'restart-logo', src: restartLogo(), alt: '' }), connectionText, h('button', { onclick: () => { hidden = false; outageShowing = true; clearConnectionNotice(); if (controller.status)
                update(controller.status); } }, 'Show diagnostics'));
        document.body.prepend(connectionNotice);
    }
    connectionText.textContent = markBidi(status.phase === 'timeout' ? 'Piper did not come back'
        : hidden ? 'Piper is not answering' : 'Reconnecting to Piper…');
}
/** Delay disruption for a brief outage; health probes never start or restart the service. */
export function connectionLost() {
    if (outagePending || controller.status?.checking)
        return;
    // A second outage must retire a previously recovered card before starting a new grace period.
    if (view)
        close();
    controller.clearOutage();
    outagePending = true;
    outageShowing = false;
    hidden = false;
    connectionTimer = window.setTimeout(showConnectionNotice, 2000);
    diagnosticTimer = window.setTimeout(() => {
        outageShowing = true;
        clearConnectionNotice();
        if (controller.status?.kind === 'outage')
            update(controller.status);
    }, 5000);
    void controller.observeOutage();
}
export function connectionRestored() {
    if (!outagePending)
        return;
    clearConnectionNotice();
    outagePending = false;
    if (view && controller.status?.kind === 'outage')
        controller.answerOutage();
    else
        controller.clearOutage();
}
// A desktop-owned check can finish while its local window is hidden. Reuse the existing toast style.
if (typeof window !== 'undefined')
    window.__piperRestart?.listen?.(text => {
        let box = document.getElementById('toasts');
        if (!box) {
            box = h('div', { id: 'toasts', role: 'status' });
            document.body.append(box);
        }
        const result = h('div', { class: 'toast' }, text);
        box.append(result);
        setTimeout(() => result.remove(), 10000);
    });
function showNotice(s) {
    if (!notice) {
        let box = document.getElementById('toasts');
        if (!box) {
            box = h('div', { id: 'toasts', role: 'status' });
            document.body.append(box);
        }
        noticeText = h('span');
        notice = h('div', { class: 'toast ext-restart' }, noticeText, h('button', { class: 'tact', onclick: () => {
                hidden = false;
                notice?.remove();
                notice = null;
                noticeText = null;
                if (controller.status)
                    update(controller.status);
            } }, 'Show'));
        box.append(notice);
    }
    noticeText.textContent = markBidi(s.checking ? 'Checking the Piper restart…' : s.finding);
}
function close() {
    const outage = controller.status?.kind === 'outage';
    hidden = true;
    layer?.close();
    layer?.remove();
    layer = null;
    view?.dispose();
    view = null;
    window.__piperRestart?.visible(!!controller.status?.checking || (outage && !controller.status?.reachable));
    notice?.remove();
    notice = null;
    noticeText = null;
    if (outage && !controller.status?.reachable)
        showConnectionNotice();
    else if (outage) {
        controller.clearOutage();
        outagePending = false;
    }
    else if (controller.status?.checking)
        showNotice(controller.status);
    if (restore?.isConnected) {
        restore.focus({ preventScroll: true });
        if (inputSelection)
            restore.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction);
        else if (selectionRanges.length) {
            const selection = document.getSelection();
            selection?.removeAllRanges();
            for (const range of selectionRanges)
                if (range.commonAncestorContainer.isConnected)
                    selection?.addRange(range);
        }
    }
}
function update(s) {
    if (s.kind === 'outage') {
        if (s.reachable) {
            clearConnectionNotice();
            outagePending = false;
            if (!view) {
                controller.clearOutage();
                window.__piperRestart?.visible(false);
                return;
            }
        }
        else if (!outageShowing || hidden) {
            if (connectionNotice)
                showConnectionNotice();
            return;
        }
    }
    if (s.phase === 'recovered' || s.phase === 'returned')
        answered = true;
    if (hidden) {
        window.__piperRestart?.visible(s.checking);
        showNotice(s);
        return;
    }
    if (!view) {
        // Reopening from the toast must retain the workspace target, rather than the removed Show button.
        if (!restore?.isConnected) {
            restore = document.activeElement;
            const input = restore;
            inputSelection = typeof input?.selectionStart === 'number' && typeof input.setSelectionRange === 'function'
                ? { start: input.selectionStart, end: input.selectionEnd ?? input.selectionStart, direction: input.selectionDirection || 'none' } : null;
            const selection = document.getSelection?.();
            selectionRanges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
        }
        view = createRestartView({ close, check: () => { void controller.check(); }, copy: text => navigator.clipboard.writeText(text),
            ...(window.__piperRestart?.log ? { log: () => { void window.__piperRestart.log(); } } : {}),
            ...(window.__piperRestart?.start ? { start: () => {
                    const status = controller.status;
                    if (!status || status.starting)
                        return;
                    status.starting = true;
                    view?.update(status);
                    void window.__piperRestart.start().catch(() => {
                        if (controller.status !== status)
                            return;
                        status.finding = 'Piper could not be started. Show the log for details before trying again.';
                    }).finally(() => {
                        if (controller.status !== status)
                            return;
                        status.starting = false;
                        view?.update(status);
                        if (!status.reachable)
                            void controller.check();
                    });
                } } : {}) });
        layer = document.createElement('dialog');
        layer.className = 'restart-layer';
        layer.setAttribute('aria-label', 'Piper restart diagnostics');
        layer.append(view.el);
        document.body.append(layer);
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
export function restartDidAnswer() { const result = answered; answered = false; return result; }
export async function showRestart(route = '/api/restart') {
    if (controller.status?.kind === 'outage') {
        clearConnectionNotice();
        controller.clearOutage();
        outagePending = false;
    }
    hidden = false;
    if (controller.status?.checking) {
        update(controller.status);
        return;
    }
    if (!view) {
        restore = null;
        inputSelection = null;
        selectionRanges = [];
    }
    answered = false;
    await controller.start(route);
}
//# sourceMappingURL=restartui.js.map