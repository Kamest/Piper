// A pane-local touch keyboard; no inferred answers or queued input.
import { h } from './dom.js';
import { markBidi } from './text.js';
const KEYS = [
    ['Escape', 'Esc', 'Escape'], ['Tab', 'Tab', 'Tab'], ['ArrowUp', '↑', 'Up arrow'], ['Space', 'Space', 'Space'], ['Type', 'Type', 'Show keyboard'],
    ['Shift', 'Shift', 'Shift, applies to next arrow or Tab'], ['ArrowLeft', '←', 'Left arrow'], ['ArrowDown', '↓', 'Down arrow'], ['ArrowRight', '→', 'Right arrow'], ['Enter', 'Enter', 'Enter'],
];
/** Match xterm's normal/application cursor modes and xterm's Shift modifier encoding. */
export function terminalKey(key, shift, application) {
    const arrow = { ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D' }[key];
    if (arrow)
        return shift ? `\x1b[1;2${arrow}` : `\x1b${application ? 'O' : '['}${arrow}`;
    if (key === 'Tab')
        return shift ? '\x1b[Z' : '\t';
    if (shift)
        return null;
    return { Escape: '\x1b', Space: ' ', Enter: '\r' }[key] ?? null;
}
let serial = 0;
/** Lifetime/selection comes from the session list; a connected replay of an ended PTY is still read-only. */
export class TerminalKeys {
    deps;
    el;
    closeButton;
    media = window.matchMedia('(pointer: coarse), (hover: none)');
    buttons = new Map();
    status = h('span', { class: 'termkeys-status', 'aria-live': 'polite' });
    keyboardStatus = h('span', { class: 'termkeys-keyboard-status', 'aria-live': 'polite' });
    toggles = new Set();
    state = { alive: false, active: false, connected: false, label: '' };
    shift = false;
    visible = false;
    opened = false;
    paneWidth = Infinity;
    paneFrame = 0;
    paneObserver;
    redraw = () => this.render();
    background = () => { if (document.hidden)
        this.opened = false; this.reset(); };
    constructor(deps, pane) {
        this.deps = deps;
        this.paneObserver = pane && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(entries => {
            const entry = entries[0];
            if (!entry || entry.contentRect.width === this.paneWidth)
                return;
            this.paneWidth = entry.contentRect.width;
            // Header controls can affect pane layout. Mutate after RO delivery, once per width.
            window.cancelAnimationFrame(this.paneFrame);
            this.paneFrame = window.requestAnimationFrame(() => { this.paneFrame = 0; this.render(); });
        }) : null;
        if (pane)
            this.paneObserver?.observe(pane);
        const statusId = `terminal-key-status-${++serial}`;
        this.status.id = statusId;
        this.el = h('div', { class: 'termkeys', role: 'toolbar', 'aria-label': 'Terminal keys', 'aria-describedby': statusId });
        this.el.id = `terminal-keys-${serial}`;
        this.el.hidden = true;
        // Panel gaps/title must not pass a tap through to xterm or change keyboard focus.
        this.el.addEventListener('pointerdown', e => e.preventDefault());
        this.closeButton = h('button', { type: 'button', class: 'termkeys-close', 'aria-label': 'Close terminal keys' }, h('span', { 'aria-hidden': 'true' }, '✕'), h('span', {}, 'Close'));
        this.closeButton.addEventListener('pointerdown', e => e.preventDefault());
        this.closeButton.addEventListener('click', () => {
            const focused = this.el.contains(document.activeElement);
            this.opened = false;
            this.reset();
            if (focused)
                [...this.toggles].find(button => button.isConnected && !button.hidden)?.focus({ preventScroll: true });
        });
        this.el.append(h('div', { class: 'termkeys-head' }, this.status, this.closeButton), this.keyboardStatus);
        const keys = h('div', { class: 'termkeys-grid' });
        for (let row = 0; row < 2; row++) {
            const line = h('div', { class: 'termkeys-row' });
            for (const [key, label, name] of KEYS.slice(row * 5, row * 5 + 5)) {
                const button = h('button', { type: 'button', 'aria-label': name }, label);
                if (key.startsWith('Arrow'))
                    button.classList.add('termkeys-arrow');
                // Keep focus until click, including Hide: otherwise pointerdown blurs xterm before the toggle reads it.
                button.addEventListener('pointerdown', e => e.preventDefault());
                button.addEventListener('click', () => this.press(key));
                this.buttons.set(key, button);
                line.append(button);
            }
            keys.append(line);
        }
        this.el.append(keys);
        this.media.addEventListener('change', this.redraw);
        document.addEventListener('visibilitychange', this.background);
    }
    update(state) {
        if (!state.alive || !state.active || state.connected !== this.state.connected)
            this.shift = false;
        this.state = state;
        this.render();
    }
    reset() { this.shift = false; this.render(); }
    /** Headers can be replaced by a poll; all retained controls still reflect their own pane. */
    createToggle() {
        for (const button of this.toggles)
            if (!button.isConnected)
                this.toggles.delete(button);
        const button = h('button', { type: 'button', class: 'termkeys-toggle', 'aria-label': 'Terminal keys',
            title: 'Show or hide special terminal keys', 'aria-controls': this.el.id });
        button.addEventListener('pointerdown', e => e.preventDefault());
        button.addEventListener('click', e => {
            e.stopPropagation();
            if (!this.available())
                return;
            this.opened = !this.opened;
            if (!this.opened)
                this.shift = false;
            this.render();
        });
        this.toggles.add(button);
        this.render();
        return button;
    }
    available() {
        return this.paneWidth >= 252 && this.media.matches && this.state.alive && this.state.active && !document.hidden;
    }
    press(key) {
        if (!this.visible || !this.state.connected || document.hidden)
            return;
        if (key === 'Shift')
            this.shift = !this.shift;
        else if (key === 'Type')
            this.deps.keyboard(!this.deps.typing());
        else {
            const data = terminalKey(key, this.shift, this.deps.application());
            if (data === null)
                return;
            this.deps.send(data);
            this.shift = false;
        }
        this.render();
    }
    render() {
        const available = this.available();
        if (!available)
            this.opened = false;
        const visible = available && this.opened;
        if (!visible)
            this.shift = false;
        this.el.hidden = !visible;
        this.visible = visible;
        for (const button of this.toggles) {
            button.hidden = !available;
            button.setAttribute('aria-expanded', String(visible));
            button.classList.toggle('on', visible);
            button.textContent = markBidi(`Keys ${visible ? '▴' : '▾'}`);
        }
        const connected = this.state.connected;
        const typing = this.deps.typing();
        this.el.classList.toggle('disconnected', !connected);
        this.el.classList.toggle('shifted', this.shift);
        this.el.setAttribute('aria-label', markBidi(`Terminal keys for ${this.state.label}`));
        this.keyboardStatus.textContent = connected && typing ? 'Keyboard open' : '';
        this.status.classList.toggle('idle', connected && !this.shift);
        this.status.setAttribute('aria-hidden', String(connected && !this.shift));
        this.status.textContent = this.shift ? 'Shift on · next arrow or Tab' : 'Terminal keys';
        if (!connected) {
            this.status.textContent = '';
            this.status.append(h('span', { class: 'termkeys-disconnected' }, 'Disconnected'), h('span', { class: 'termkeys-detail' }, 'Keys off until reconnected'));
        }
        for (const [key, label, name] of KEYS) {
            const button = this.buttons.get(key);
            const modified = this.shift && (key === 'Tab' || key.startsWith('Arrow'));
            const disabled = !connected || (this.shift && ['Escape', 'Space', 'Enter'].includes(key));
            button.setAttribute('aria-disabled', String(disabled));
            button.textContent = markBidi(key === 'Type' ? typing ? 'Hide' : 'Type' : key === 'Shift' && this.shift ? '⇧ Shift' : `${modified ? '⇧' : ''}${label}`);
            button.setAttribute('aria-label', markBidi(key === 'Type' ? typing ? 'Hide keyboard' : 'Show keyboard' : `${modified ? 'Shift ' : ''}${name}`));
            if (key === 'Shift' || key === 'Type') {
                const pressed = key === 'Shift' ? this.shift : typing;
                button.setAttribute('aria-pressed', String(pressed));
                button.classList.toggle('on', pressed);
            }
        }
    }
    dispose() {
        this.update({ ...this.state, active: false });
        this.paneObserver?.disconnect();
        window.cancelAnimationFrame(this.paneFrame);
        this.media.removeEventListener('change', this.redraw);
        document.removeEventListener('visibilitychange', this.background);
        for (const button of this.toggles)
            button.remove();
        this.toggles.clear();
        this.el.remove();
    }
}
//# sourceMappingURL=termkeys.js.map