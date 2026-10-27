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
let viewportOwner = null;
/** Lifetime/selection comes from the session list; a connected replay of an ended PTY is still read-only. */
export class TerminalKeys {
    deps;
    el;
    media = window.matchMedia('(pointer: coarse), (hover: none)');
    buttons = new Map();
    target = h('span', { class: 'termkeys-target' });
    status = h('span', { class: 'termkeys-status', 'aria-live': 'polite' });
    state = { alive: false, active: false, connected: false, label: '' };
    shift = false;
    visible = false;
    viewportApp = null;
    redraw = () => this.render();
    background = () => { if (document.hidden)
        this.reset(); };
    viewport = () => {
        if (viewportOwner === this && this.viewportApp && window.visualViewport)
            this.viewportApp.style.setProperty('--term-viewport-height', `${window.visualViewport.height}px`);
    };
    constructor(deps) {
        this.deps = deps;
        const statusId = `terminal-key-status-${++serial}`;
        this.status.id = statusId;
        this.el = h('div', { class: 'termkeys', role: 'toolbar', 'aria-label': 'Terminal keys', 'aria-describedby': statusId });
        this.el.hidden = true;
        this.el.append(h('div', { class: 'termkeys-info' }, h('span', { class: 'termkeys-who' }, h('i', { class: 'termkeys-dot', 'aria-hidden': 'true' }), this.target), this.status));
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
            this.el.append(line);
        }
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
        const visible = this.media.matches && this.state.alive && this.state.active;
        if (!visible)
            this.shift = false;
        this.el.hidden = !visible;
        if (visible !== this.visible) {
            this.visible = visible;
            if (visible) {
                viewportOwner = this;
                this.viewportApp = document.getElementById('app');
                window.visualViewport?.addEventListener('resize', this.viewport);
                window.visualViewport?.addEventListener('scroll', this.viewport);
                this.viewport();
            }
            else {
                window.visualViewport?.removeEventListener('resize', this.viewport);
                window.visualViewport?.removeEventListener('scroll', this.viewport);
                if (viewportOwner === this) {
                    this.viewportApp?.style.removeProperty('--term-viewport-height');
                    viewportOwner = null;
                }
                this.viewportApp = null;
            }
            this.deps.resize();
        }
        const connected = this.state.connected;
        const typing = this.deps.typing();
        this.el.classList.toggle('disconnected', !connected);
        this.target.textContent = connected ? markBidi(this.state.label) : 'Disconnected';
        this.target.title = markBidi(this.state.label);
        this.el.setAttribute('aria-label', markBidi(`Terminal keys for ${this.state.label}`));
        this.status.textContent = !connected ? 'Keys off until reconnected' : this.shift ? 'Shift on · next arrow or Tab' : typing ? 'Keyboard open' : '';
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
        this.media.removeEventListener('change', this.redraw);
        document.removeEventListener('visibilitychange', this.background);
        this.el.remove();
    }
}
//# sourceMappingURL=termkeys.js.map